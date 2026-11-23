"""C15–C20: Opt-in ask_user native contract lifecycle, validation, TTL, permission isolation & persistence."""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import project_helpers
import pytest
from app_helpers import make_host_config
from forecontract.tracing import JsonlTraceStore
from forecontract_app.models import (
    AppError,
    HostConfig,
    ModelSelection,
    StartRunRequest,
)
from forecontract_app.project_adapter import ProjectCodingAdapter
from forecontract_app.project_commands import ProjectCommandRunner
from forecontract_app.project_demo import build_registered_project_task
from forecontract_app.project_models import (
    SUPPORTED_PROJECT_ACTIONS,
    ProjectError,
    ProposalDecision,
    RegisterProjectRequest,
)
from forecontract_app.project_runtime import run_project_task
from forecontract_app.project_workspace import ProjectWorkspace
from forecontract_app.service import AppService
from forecontract_app.session_context import ConversationRuntime, build_context_frame
from forecontract_app.session_models import AnswerQuestionRequest, QuestionView
from forecontract_app.session_questions import QuestionBroker
from forecontract_app.session_store import SessionStore
from forecontract_llm import ModelRequest
from pydantic import ValidationError
from session_helpers import (
    SpyUsageProvider,
    forecast_json,
    make_reopened_host_config,
    wait_for_run_terminal_async,
)


def test_c15_real_core_ask_user_contract_trace_and_opt_in_gating(tmp_path: Path) -> None:
    repo = project_helpers.create_buggy_calc_project(tmp_path / "repo")
    data_root = tmp_path / "data"
    out_root = tmp_path / "out"
    data_root.mkdir()
    out_root.mkdir()
    ws = ProjectWorkspace(root=repo, data_root=data_root)
    broker = PermissionBroker_or = None
    del broker, PermissionBroker_or

    from forecontract_app.project_permissions import PermissionBroker

    # 1. Without conversation -> strictly original 13 actions; ask_user rejected
    task0, _ = build_registered_project_task(
        project_id=str(uuid.uuid4()),
        project_name="calc",
        root_identity=ws.root_identity,
        data_root=data_root,
        output_root=out_root,
        run_id=str(uuid.uuid4()),
        session_id=str(uuid.uuid4()),
        goal="No ask_user without conversation",
        python_executable=Path(sys.executable),
    )
    b0 = PermissionBroker(run_id=task0.run_id, project_root=repo, root_identity=ws.root_identity)
    r0 = ProjectCommandRunner(run_id=task0.run_id, project_root=repo, data_root=data_root, broker=b0)
    adapter_off = ProjectCodingAdapter(task=task0, workspace=ws, broker=b0, command_runner=r0)
    assert adapter_off.capabilities().supported_actions == list(SUPPORTED_PROJECT_ACTIONS)
    from forecontract.models.actions import ActionSpec
    from forecontract.protocols.adapter import UnsupportedActionError

    with pytest.raises(UnsupportedActionError):
        adapter_off.validate_action(
            ActionSpec(
                schema_version="0.1",
                action_type="ask_user",
                payload={"question": "Should fail"},
                metadata={"action_id": "a0"},
            )
        )

    # 2. With conversation + QuestionBroker -> opt-in 14th action via full Core contract chain
    pid = str(uuid.uuid4())
    sid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    task, pcfg = build_registered_project_task(
        project_id=pid,
        project_name="calc",
        root_identity=ws.root_identity,
        data_root=data_root,
        output_root=out_root,
        run_id=rid,
        session_id=sid,
        goal="Ask user then complete",
        python_executable=Path(sys.executable),
    )
    b1 = PermissionBroker(run_id=rid, project_root=repo, root_identity=ws.root_identity)
    r1 = ProjectCommandRunner(run_id=rid, project_root=repo, data_root=data_root, broker=b1)
    persisted: list[QuestionView] = []
    events: list[tuple[str, dict[str, Any]]] = []
    q_broker = QuestionBroker(
        project_id=pid,
        session_id=sid,
        run_id=rid,
        persist=lambda q: persisted.append(q),
        emit=lambda k, p: events.append((k, p)),
    )
    frame = build_context_frame(
        project_id=pid, session_id=sid, run_id=rid, goal=task.goal_text, constraints="", history=[]
    )
    conv = ConversationRuntime(context_frame=frame, question_broker=q_broker)

    observed_answers: list[str] = []
    trace_file = data_root / "trace_c15.jsonl"

    def _build(idx: int, req: ModelRequest) -> str:
        st = req.observation.state
        last = st.get("last_tool_result") or {}
        if idx == 0:
            # First step only asks user (and attempts task_completion_hint=True -> CANNOT mark passed!)
            return forecast_json(
                action_type="ask_user",
                payload={"question": "Which branch strategy?", "options": ["minimal", "refactor"]},
                task_hint=True,
            )
        observed_answers.append(str(last.get("answer")))
        assert last.get("question_id") == persisted[0].id
        assert last.get("action_id") == persisted[0].action_id
        # Terminate honestly via verify_project failure since plan isn't passed (ask_user didn't mark passed)
        return forecast_json(action_type="list_directory", payload={})

    # Verify ask_user alone never passes task oracle: limit max_steps=1 so run ends after ask_user
    one_step_task = task.model_copy(update={"max_steps": 1})

    async def _run() -> Any:
        async def _auto_answer() -> None:
            while not q_broker.list_questions():
                await asyncio.sleep(0.01)
            q = q_broker.list_questions()[0]
            q_broker.answer(
                question_id=q.id,
                request=AnswerQuestionRequest(
                    client_request_id=uuid.uuid4(),
                    question_digest=q.digest,
                    text="minimal",
                ),
            )

        ans_task = asyncio.create_task(_auto_answer())
        try:
            return await run_project_task(
                task=one_step_task,
                provider=SpyUsageProvider(_build),
                planner_config=pcfg,
                permissions=b1,
                trace_writer=JsonlTraceStore(trace_file),
                emit=lambda _k, _p: None,
                workspace=ws,
                command_runner=r1,
                conversation=conv,
            )
        finally:
            await ans_task

    res = asyncio.run(_run())
    assert res.terminal_status != "succeeded"
    assert res.verification != "passed"
    assert any(k == "question_requested" for k, _ in events)
    assert any(k == "question_answered" for k, _ in events)
    trace_lines = [json.loads(ln) for ln in trace_file.read_text("utf-8").splitlines() if ln.strip()]
    kinds = {obj.get("kind") for obj in trace_lines}
    assert {"contract_committed", "attempt_finished"}.issubset(kinds)
    assert any(
        obj.get("kind") == "verification_recorded" and obj.get("verification_status") == "pass"
        for obj in trace_lines
    )


def test_c16_question_validation_idempotency_conflict_and_free_text() -> None:
    pid = str(uuid.uuid4())
    sid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    persisted: list[QuestionView] = []
    emitted: list[str] = []
    qb = QuestionBroker(
        project_id=pid,
        session_id=sid,
        run_id=rid,
        persist=lambda q: persisted.append(q),
        emit=lambda k, _p: emitted.append(k),
    )

    async def _scenario() -> None:
        wait_task = asyncio.create_task(
            qb.ask(action_id="act-1", question="Choose format", options=["json", "yaml"])
        )
        await asyncio.sleep(0.01)
        q = qb.list_questions()[0]

        # Wrong digest -> 409 QUESTION_DIGEST_MISMATCH, waiter NOT woken
        with pytest.raises(AppError) as dig_err:
            qb.answer(
                question_id=q.id,
                request=AnswerQuestionRequest(
                    client_request_id=uuid.uuid4(),
                    question_digest="f" * 64,
                    text="custom text",
                ),
            )
        assert dig_err.value.status_code == 409
        assert not wait_task.done()

        # Unknown qid -> 404 NOT_FOUND
        with pytest.raises(AppError) as nf_err:
            qb.answer(
                question_id=str(uuid.uuid4()),
                request=AnswerQuestionRequest(
                    client_request_id=uuid.uuid4(),
                    question_digest=q.digest,
                    text="custom text",
                ),
            )
        assert nf_err.value.status_code == 404

        # Extra fields & wrong scalar types rejected by strict DTO
        with pytest.raises(ValidationError):
            AnswerQuestionRequest.model_validate(
                {
                    "client_request_id": str(uuid.uuid4()),
                    "question_digest": q.digest,
                    "text": 123,
                }
            )
        with pytest.raises(ValidationError):
            AnswerQuestionRequest.model_validate(
                {
                    "client_request_id": str(uuid.uuid4()),
                    "question_digest": q.digest,
                    "text": "ok",
                    "extra_field": "bad",
                }
            )

        # Free text NOT in options is accepted, idempotent on same client_request_id, 409 on diff payload
        ans_key = uuid.uuid4()
        res1 = qb.answer(
            question_id=q.id,
            request=AnswerQuestionRequest(
                client_request_id=ans_key,
                question_digest=q.digest,
                text="Free-form answer not listed in options",
            ),
        )
        assert res1.state == "answered"
        assert res1.answer == "Free-form answer not listed in options"
        persisted_count = len(persisted)

        # Idempotent retry returns same answer without re-persisting or re-emitting
        res2 = qb.answer(
            question_id=q.id,
            request=AnswerQuestionRequest(
                client_request_id=ans_key,
                question_digest=q.digest,
                text="Free-form answer not listed in options",
            ),
        )
        assert res2.answer == res1.answer
        assert len(persisted) == persisted_count

        with pytest.raises(AppError) as conf_err:
            qb.answer(
                question_id=q.id,
                request=AnswerQuestionRequest(
                    client_request_id=ans_key,
                    question_digest=q.digest,
                    text="Mutated answer",
                ),
            )
        assert conf_err.value.status_code == 409

        # Different key after answered -> QUESTION_CLOSED 409
        with pytest.raises(AppError) as closed_err:
            qb.answer(
                question_id=q.id,
                request=AnswerQuestionRequest(
                    client_request_id=uuid.uuid4(),
                    question_digest=q.digest,
                    text="Late answer",
                ),
            )
        assert closed_err.value.code == "QUESTION_CLOSED"

        done_view = await wait_task
        assert done_view.state == "answered"

    asyncio.run(_scenario())


def test_c17_question_ttl_run_budget_cancel_with_controllable_clock() -> None:
    now_holder = [1000.0]
    persisted: list[QuestionView] = []
    emitted: list[str] = []
    qb = QuestionBroker(
        project_id=str(uuid.uuid4()),
        session_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
        persist=lambda q: persisted.append(q),
        emit=lambda k, _p: emitted.append(k),
        ttl_seconds=120.0,
        monotonic=lambda: now_holder[0],
        now_utc=lambda: datetime.now(timezone.utc),
    )

    async def _scenario() -> None:
        # 1. Controllable clock 120s expiry + repeat same action_id does NOT reset TTL
        task_ask = asyncio.create_task(
            qb.ask(action_id="act-ttl", question="Expire after 120s?", options=[])
        )
        await asyncio.sleep(0.02)
        q1 = qb.list_questions()[0]
        assert q1.state == "pending"
        orig_exp = q1.expires_at

        # Advance by 60s and ensure same action_id wait does not extend expires_at
        now_holder[0] += 60.0
        assert qb.list_questions()[0].expires_at == orig_exp

        # Advance past 120s -> expires quickly and closes cleanly
        now_holder[0] += 61.0
        exp_view = await asyncio.wait_for(task_ask, timeout=2.0)
        assert exp_view.state == "expired"
        assert qb.list_questions()[0].state == "expired"

        # 2. Same action_id repeated after expiry returns closed state without creating another question
        dup_view = await qb.ask(action_id="act-ttl", question="Expire after 120s?", options=[])
        assert dup_view.id == q1.id
        assert len(qb.list_questions()) == 1

        # 3. Cancel while pending unlocks waiter immediately
        task_cancel = asyncio.create_task(
            qb.ask(action_id="act-cancel", question="Cancel test?", options=[])
        )
        await asyncio.sleep(0.02)
        qb.cancel_pending(reason_code="USER_CANCELLED")
        cancel_view = await asyncio.wait_for(task_cancel, timeout=2.0)
        assert cancel_view.state == "cancelled"

        # 4. Run budget expiry (task cancellation) closes pending immediately
        task_budget = asyncio.create_task(
            qb.ask(action_id="act-budget", question="Run budget expiry?", options=[])
        )
        await asyncio.sleep(0.02)
        task_budget.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task_budget
        assert qb.list_questions()[-1].state == "cancelled"

    asyncio.run(_scenario())


def test_c18_answer_never_grants_scope_plan_or_command_permissions(tmp_path: Path) -> None:
    repo = project_helpers.create_buggy_calc_project(tmp_path / "repo")
    data_root = tmp_path / "data"
    out_root = tmp_path / "out"
    data_root.mkdir()
    out_root.mkdir()
    ws = ProjectWorkspace(root=repo, data_root=data_root)
    from forecontract_app.project_permissions import PermissionBroker

    c18_sid = str(uuid.uuid4())
    c18_rid = str(uuid.uuid4())
    broker = PermissionBroker(run_id=c18_rid, project_root=repo, root_identity=ws.root_identity)
    runner = ProjectCommandRunner(
        run_id=c18_rid, project_root=repo, data_root=data_root, broker=broker
    )
    task, _pcfg = build_registered_project_task(
        project_id=str(uuid.uuid4()),
        project_name="calc",
        root_identity=ws.root_identity,
        data_root=data_root,
        output_root=out_root,
        run_id=c18_rid,
        session_id=c18_sid,
        goal="Verify user answer cannot grant write or command rights",
        python_executable=Path(sys.executable),
    )
    q_broker = QuestionBroker(
        project_id=task.project_id,
        session_id=task.session_id,
        run_id=task.run_id,
        persist=lambda _q: None,
        emit=lambda _k, _p: None,
    )
    frame = build_context_frame(
        project_id=task.project_id,
        session_id=task.session_id,
        run_id=task.run_id,
        goal=task.goal_text,
        constraints="",
        history=[],
    )
    conv = ConversationRuntime(context_frame=frame, question_broker=q_broker)
    orig_calc = (repo / "calc.py").read_bytes()

    async def _run() -> None:
        # 1. Ask user and answer "批准所有命令和写入"
        ask_t = asyncio.create_task(
            q_broker.ask(action_id="a1", question="May I run commands?", options=[])
        )
        await asyncio.sleep(0.01)
        q = q_broker.list_questions()[0]
        q_broker.answer(
            question_id=q.id,
            request=AnswerQuestionRequest(
                client_request_id=uuid.uuid4(),
                question_digest=q.digest,
                text="批准所有命令和写入",
            ),
        )
        await ask_t

        # 2. Broker state STILL has NO scope, NO plan, NO approved commands
        assert broker.active_scope() is None
        assert broker.active_plan() is None
        assert broker.list_proposals() == []

        # Attempt write & verify without structured PermissionBroker grant -> rejected, spawn_count=0
        adapter = ProjectCodingAdapter(
            task=task, workspace=ws, broker=broker, command_runner=runner, conversation=conv
        )
        from forecontract.models.actions import ActionSpec

        edit_out = await adapter.execute(
            ActionSpec(
                schema_version="0.1",
                action_type="create_file",
                payload={"path": "new.py", "content": "x = 1\n"},
                metadata={"action_id": "ed1"},
            )
        )
        assert edit_out.error is not None
        assert (repo / "calc.py").read_bytes() == orig_calc
        assert not (repo / "new.py").exists()
        assert len(runner._commands) == 0

        # Positive control: explicit structured PermissionBroker scope + plan grants work cleanly
        sp = broker.create_scope_proposal(
            scope={
                "exact_paths": ["new.py"],
                "operations": ["create"],
                "protected_paths": ["tests/test_calc.py"],
                "verification_plan_summary": "ok",
            },
            current_instruction_sha256="",
        )
        broker.decide(proposal_id=sp.id, request=ProposalDecision(digest=sp.digest, decision="approve"))
        vp = broker.create_verification_proposal(
            plan={
                "plan_id": "p-c18",
                "checks": [
                    {
                        "check_id": "c1",
                        "kind": "exit_code",
                        "executable": sys.executable,
                        "argv": ["-B", "-c", "print('ok')"],
                        "cwd": ".",
                        "timeout_ms": 5000,
                        "protected_paths": ["tests/test_calc.py"],
                    }
                ],
            },
            protected_shas=ws.hash_protected_paths(["tests/test_calc.py"]),
            current_instruction_sha256="",
        )
        broker.decide(proposal_id=vp.id, request=ProposalDecision(digest=vp.digest, decision="approve"))
        ok_out = await adapter.execute(
            ActionSpec(
                schema_version="0.1",
                action_type="create_file",
                payload={"path": "new.py", "content": "x = 1\n"},
                metadata={"action_id": "ed2"},
            )
        )
        assert ok_out.error is None
        assert (repo / "new.py").read_text(encoding="utf-8") == "x = 1\n"

    asyncio.run(_run())


def test_c19_refresh_preserves_qid_and_reopen_closes_pending_as_cancelled(tmp_path: Path) -> None:
    repo = project_helpers.create_buggy_calc_project(tmp_path / "repo")
    base_cfg = make_host_config(tmp_path / "host")
    cfg = HostConfig(
        data_root=base_cfg.data_root,
        workspace_parent=base_cfg.workspace_parent,
        python_executable=base_cfg.python_executable,
        bind_port=base_cfg.bind_port,
        access_token=base_cfg.access_token,
        project_roots=(repo,),
    )

    def _ask_factory(**_: Any) -> Any:
        def _build(idx: int, req: ModelRequest) -> str:
            if idx == 0:
                return forecast_json(
                    action_type="ask_user",
                    payload={"question": "Confirm continuation?", "options": ["yes", "no"]},
                )
            return forecast_json(action_type="list_directory", payload={})

        return SpyUsageProvider(_build)

    async def _phase1() -> tuple[str, str, str, str]:
        svc = AppService(config=cfg, provider_factory=_ask_factory)
        proj = await svc.register_project(
            request=RegisterProjectRequest(name="c19-proj", root=str(repo), confirmed_read=True)
        )
        sess = svc.create_session(
            project_id=proj.project_id,
            model=ModelSelection(provider="offline", model="offline-project-v1"),
        )
        run = await svc.start_run(
            session_id=sess.session_id,
            request=StartRunRequest(client_request_id=uuid.uuid4(), goal="C19 test", confirmed_scope=True),
        )
        # Wait until pending question is persisted
        for _ in range(200):
            qs = svc.list_questions(session_id=sess.session_id, run_id=run.run_id)
            if qs and qs[0].state == "pending":
                break
            await asyncio.sleep(0.02)
        snap1 = svc.snapshot(session_id=sess.session_id, run_id=run.run_id)
        snap2 = svc.snapshot(session_id=sess.session_id, run_id=run.run_id)
        assert len(snap1.pending_questions) == 1
        assert snap1.pending_questions[0]["id"] == snap2.pending_questions[0]["id"]
        assert snap1.pending_questions[0]["digest"] == snap2.pending_questions[0]["digest"]
        qid = snap1.pending_questions[0]["id"]
        dig = snap1.pending_questions[0]["digest"]
        await svc.aclose()
        return sess.session_id, run.run_id, qid, dig

    sid, rid, qid, q_digest = asyncio.run(_phase1())

    # Simulate unclosed pending row on crash reopen by forcing DB pending before restart reconcile
    store = SessionStore(data_root=cfg.data_root)
    with store.transaction() as conn:
        conn.execute(
            "UPDATE questions SET state='pending', reason_code=NULL WHERE question_id = ?",
            (qid,),
        )
        conn.execute("UPDATE runs SET state='running' WHERE run_id = ?", (rid,))
    store.close()

    svc2 = AppService(config=make_reopened_host_config(cfg), provider_factory=_ask_factory)
    try:
        qs_reopened = svc2.list_questions(session_id=sid, run_id=rid)
        assert len(qs_reopened) == 1
        assert qs_reopened[0].id == qid
        assert qs_reopened[0].state == "cancelled"
        assert qs_reopened[0].reason_code == "HOST_RESTARTED"
        with pytest.raises(AppError) as err:
            svc2.answer_question(
                session_id=sid,
                run_id=rid,
                question_id=qid,
                request=AnswerQuestionRequest(
                    client_request_id=uuid.uuid4(),
                    question_digest=q_digest,
                    text="Too late after restart",
                ),
            )
        assert err.value.status_code == 409
        assert err.value.code == "QUESTION_CLOSED"
    finally:
        asyncio.run(svc2.aclose())


def test_c20_question_quota_single_pending_and_commit_failure_no_wake() -> None:
    fail_persist = [False]
    persisted: list[QuestionView] = []
    emitted: list[tuple[str, dict[str, Any]]] = []

    def _persist(q: QuestionView) -> None:
        if fail_persist[0]:
            raise AppError("STORE_UNAVAILABLE", "db write failed", status_code=503)
        persisted.append(q)

    qb = QuestionBroker(
        project_id=str(uuid.uuid4()),
        session_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
        persist=_persist,
        emit=lambda k, p: emitted.append((k, p)),
        max_questions=8,
    )

    async def _scenario() -> None:
        # 1. Fail question persist -> no question_requested event, quota unconsumed
        fail_persist[0] = True
        with pytest.raises(AppError):
            await qb.ask(action_id="a-fail", question="Should not persist", options=[])
        assert len(emitted) == 0
        fail_persist[0] = False

        # 2. Second concurrent pending rejected while first is still pending
        t_pend = asyncio.create_task(
            qb.ask(action_id="a-hold", question="Hold pending", options=[])
        )
        await asyncio.sleep(0.01)
        with pytest.raises(ProjectError) as pend_err:
            await qb.ask(action_id="a-second", question="Second concurrent", options=[])
        assert pend_err.value.code == "QUESTION_ALREADY_PENDING"

        # 3. Answer persist failure keeps state pending, emits NO question_answered, does NOT wake waiter
        q_hold = qb.list_questions()[0]
        fail_persist[0] = True
        with pytest.raises(AppError) as ans_fail:
            qb.answer(
                question_id=q_hold.id,
                request=AnswerQuestionRequest(
                    client_request_id=uuid.uuid4(), question_digest=q_hold.digest, text="fail commit"
                ),
            )
        assert ans_fail.value.code == "STORE_UNAVAILABLE"
        assert qb.list_questions()[0].state == "pending"
        assert not t_pend.done()
        assert not any(k == "question_answered" for k, _ in emitted)

        fail_persist[0] = False
        qb.cancel_pending(reason_code="RUN_CANCELLED")
        await t_pend
        # Answering after cancel -> 409 QUESTION_CLOSED
        with pytest.raises(AppError) as post_cancel:
            qb.answer(
                question_id=q_hold.id,
                request=AnswerQuestionRequest(
                    client_request_id=uuid.uuid4(), question_digest=q_hold.digest, text="after cancel"
                ),
            )
        assert post_cancel.value.code == "QUESTION_CLOSED"

        # 4. Ask 7 more questions (total 8) then 9th rejected with QUESTION_QUOTA_EXCEEDED
        for idx in range(2, 9):
            t_q = asyncio.create_task(
                qb.ask(action_id=f"a-{idx}", question=f"Question {idx}", options=[])
            )
            await asyncio.sleep(0.01)
            cur = qb.list_questions()[-1]
            qb.answer(
                question_id=cur.id,
                request=AnswerQuestionRequest(
                    client_request_id=uuid.uuid4(), question_digest=cur.digest, text=f"ok-{idx}"
                ),
            )
            await t_q

        with pytest.raises(ProjectError) as quota_err:
            await qb.ask(action_id="a-9", question="Question 9", options=[])
        assert quota_err.value.code == "QUESTION_QUOTA_EXCEEDED"

    asyncio.run(_scenario())


def test_d03_answer_retry_isolated_after_restart(tmp_path: Path) -> None:
    repo = project_helpers.create_buggy_calc_project(tmp_path / "repo")
    base_cfg = make_host_config(tmp_path / "host")
    cfg = HostConfig(
        data_root=base_cfg.data_root,
        workspace_parent=base_cfg.workspace_parent,
        python_executable=base_cfg.python_executable,
        bind_port=base_cfg.bind_port,
        access_token=base_cfg.access_token,
        project_roots=(repo,),
    )
    provider_calls = [0]

    def _ask_factory(**_: Any) -> Any:
        def _build(idx: int, req: ModelRequest) -> str:
            provider_calls[0] += 1
            if idx == 0:
                return forecast_json(
                    action_type="ask_user",
                    payload={"question": "Select branch label?", "options": ["label-0", "label-1"]},
                )
            return "not-json-deterministic-stop"

        return SpyUsageProvider(_build)

    shared_req_id = uuid.uuid4()

    async def _phase1() -> tuple[str, str, str, str, str, str, str, str, list[Any], list[Any], int]:
        svc = AppService(config=cfg, provider_factory=_ask_factory)
        try:
            proj = await svc.register_project(
                request=RegisterProjectRequest(name="d03-proj", root=str(repo), confirmed_read=True)
            )
            s0 = svc.create_session(
                project_id=proj.project_id,
                model=ModelSelection(provider="offline", model="offline-project-v1"),
            )
            s1 = svc.create_session(
                project_id=proj.project_id,
                model=ModelSelection(provider="offline", model="offline-project-v1"),
            )

            async def _run_and_answer(session_id: str, label: str) -> tuple[str, str, str]:
                run = await svc.start_run(
                    session_id=session_id,
                    request=StartRunRequest(
                        client_request_id=uuid.uuid4(),
                        goal=f"Ask user {label}",
                        confirmed_scope=True,
                    ),
                )
                q_pending = None
                for _ in range(200):
                    qs = svc.list_questions(session_id=session_id, run_id=run.run_id)
                    if qs and qs[0].state == "pending":
                        q_pending = qs[0]
                        break
                    await asyncio.sleep(0.02)
                assert q_pending is not None
                answered = svc.answer_question(
                    session_id=session_id,
                    run_id=run.run_id,
                    question_id=q_pending.id,
                    request=AnswerQuestionRequest(
                        client_request_id=shared_req_id,
                        question_digest=q_pending.digest,
                        text=label,
                    ),
                )
                assert answered.state == "answered"
                assert answered.answer == label
                term = await wait_for_run_terminal_async(svc, session_id=session_id, run_id=run.run_id)
                assert term.state in {"succeeded", "unverified", "failed"}
                return run.run_id, q_pending.id, q_pending.digest

            r0_id, q0_id, q0_digest = await _run_and_answer(s0.session_id, "label-0")
            r1_id, q1_id, q1_digest = await _run_and_answer(s1.session_id, "label-1")
            hist0 = svc.get_session_history(session_id=s0.session_id).items
            hist1 = svc.get_session_history(session_id=s1.session_id).items
            return (
                s0.session_id,
                r0_id,
                q0_id,
                q0_digest,
                s1.session_id,
                r1_id,
                q1_id,
                q1_digest,
                hist0,
                hist1,
                provider_calls[0],
            )
        finally:
            await svc.aclose()

    s0_id, r0_id, q0_id, q0_digest, s1_id, r1_id, q1_id, q1_digest, hist0_before, hist1_before, calls_before = (
        asyncio.run(_phase1())
    )

    svc2 = AppService(config=make_reopened_host_config(cfg), provider_factory=_ask_factory)
    try:
        retry0 = svc2.answer_question(
            session_id=s0_id,
            run_id=r0_id,
            question_id=q0_id,
            request=AnswerQuestionRequest(
                client_request_id=shared_req_id,
                question_digest=q0_digest,
                text="label-0",
            ),
        )
        assert retry0.state == "answered"
        assert retry0.id == q0_id
        assert retry0.answer == "label-0"

        retry1 = svc2.answer_question(
            session_id=s1_id,
            run_id=r1_id,
            question_id=q1_id,
            request=AnswerQuestionRequest(
                client_request_id=shared_req_id,
                question_digest=q1_digest,
                text="label-1",
            ),
        )
        assert retry1.state == "answered"
        assert retry1.id == q1_id
        assert retry1.answer == "label-1"

        assert svc2.get_session_history(session_id=s0_id).items == hist0_before
        assert svc2.get_session_history(session_id=s1_id).items == hist1_before
        assert provider_calls[0] == calls_before

        with pytest.raises(AppError) as conflict_err:
            svc2.answer_question(
                session_id=s0_id,
                run_id=r0_id,
                question_id=q0_id,
                request=AnswerQuestionRequest(
                    client_request_id=shared_req_id,
                    question_digest=q0_digest,
                    text="label-1",
                ),
            )
        assert conflict_err.value.status_code == 409

        with pytest.raises(AppError) as cross_err:
            svc2.answer_question(
                session_id=s0_id,
                run_id=r1_id,
                question_id=q1_id,
                request=AnswerQuestionRequest(
                    client_request_id=shared_req_id,
                    question_digest=q1_digest,
                    text="label-1",
                ),
            )
        assert cross_err.value.status_code == 404
    finally:
        asyncio.run(svc2.aclose())
