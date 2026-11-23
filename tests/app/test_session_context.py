"""C09–C14: Deterministic bounded ContextFrame, LLMPlanner budget gates, and multi-round isolation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import uuid
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
from forecontract_app.project_commands import ProjectCommandRunner
from forecontract_app.project_demo import build_registered_project_task
from forecontract_app.project_models import (
    PROJECT_MAX_OBSERVATION_BYTES,
    ProposalDecision,
    RegisterProjectRequest,
)
from forecontract_app.project_permissions import PermissionBroker
from forecontract_app.project_runtime import run_project_task
from forecontract_app.project_workspace import ProjectWorkspace
from forecontract_app.service import AppService, _bounded_run_result_summary
from forecontract_app.session_context import ConversationRuntime, build_context_frame
from forecontract_app.session_models import (
    MAX_CONSTRAINTS_UTF8_BYTES,
    HistoryItem,
    SetConstraintsRequest,
)
from forecontract_app.session_store import SessionStore
from forecontract_llm import ModelRequest, PlannerConfig
from session_helpers import (
    SpyUsageProvider,
    build_history_item_record,
    forecast_json,
    wait_for_run_terminal_async,
)


def test_c09_short_history_deterministic_verbatim_and_session_isolated() -> None:
    pid = str(uuid.uuid4())
    sid = str(uuid.uuid4())
    other_sid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    r_prev = str(uuid.uuid4())

    items = [
        build_history_item_record(
            seq=1,
            project_id=pid,
            session_id=sid,
            run_id=r_prev,
            kind="user_goal",
            text="First goal verbatim",
            source_ids=[r_prev, "req-1"],
        ),
        build_history_item_record(
            seq=2,
            project_id=pid,
            session_id=sid,
            run_id=r_prev,
            kind="run_result",
            text='{"state":"succeeded","changed_paths":["calc.py"]}',
            source_ids=[r_prev],
        ),
    ]
    goal = "Second round goal"
    constraints = "Never touch tests/"

    f1 = build_context_frame(
        project_id=pid,
        session_id=sid,
        run_id=rid,
        goal=goal,
        constraints=constraints,
        history=items,
    )
    f2 = build_context_frame(
        project_id=pid,
        session_id=sid,
        run_id=rid,
        goal=goal,
        constraints=constraints,
        history=list(items),
    )
    assert f1.model_dump(mode="json") == f2.model_dump(mode="json")
    assert f1.schema_version == 1
    assert f1.project_id == pid
    assert f1.session_id == sid
    assert f1.run_id == rid
    assert f1.goal_sha256 == hashlib.sha256(goal.encode("utf-8")).hexdigest()
    assert f1.constraints == constraints
    assert f1.history_cursor == 2
    assert f1.omitted_count == 0
    assert f1.truncated is False
    assert [it.seq for it in f1.recent_items] == [1, 2]
    assert f1.recent_items[0].text == "First goal verbatim"
    assert f1.recent_items[0].sha256 == items[0].sha256
    assert f1.source_item_ids == [items[0].item_id, items[1].item_id]

    foreign = build_history_item_record(
        seq=3,
        project_id=pid,
        session_id=other_sid,
        run_id=str(uuid.uuid4()),
        kind="user_goal",
        text="Cross session leak",
    )
    with pytest.raises(AppError):
        build_context_frame(
            project_id=pid,
            session_id=sid,
            run_id=rid,
            goal=goal,
            constraints=constraints,
            history=[*items, foreign],
        )


def test_c10_chinese_emoji_compaction_16kib_valid_utf8_preserves_sha_and_db(
    tmp_path: Path,
) -> None:
    d_root = tmp_path / "db"
    d_root.mkdir()
    store = SessionStore(data_root=d_root)
    try:
        from forecontract_app.models import ModelSelection, ProjectView, SessionView

        pid = str(uuid.uuid4())
        sid = str(uuid.uuid4())
        rid = str(uuid.uuid4())
        pv = ProjectView(
            project_id=pid,
            name="emoji-p",
            workspace_root=str(tmp_path / "ws"),
            read_paths=[],
            write_paths=[],
            commands_summary=[],
            expected_tests=[],
        )
        store.insert_project(
            view=pv,
            kind="existing",
            workspace_root=pv.workspace_root,
            canonical_root="ws",
            read_grant_id=str(uuid.uuid4()),
            spec={"view": pv.model_dump(mode="json")},
        )
        sv = SessionView(
            session_id=sid,
            project_id=pid,
            model=ModelSelection(provider="offline", model="offline-project-v1"),
        )
        store.insert_session(view=sv, seq_order=1)

        items: list[HistoryItem] = []
        with store.transaction() as conn:
            for seq in range(1, 31):
                long_cjk_emoji = f"轮次{seq:02d}-" + ("测试🔥中文🚀" * 180)
                it = build_history_item_record(
                    seq=seq,
                    project_id=pid,
                    session_id=sid,
                    run_id=str(uuid.uuid4()),
                    kind="answer",
                    text=long_cjk_emoji,
                    source_ids=[f"src-{seq}"],
                )
                items.append(it)
                store._insert_history_row(conn, it)

        frame = build_context_frame(
            project_id=pid,
            session_id=sid,
            run_id=rid,
            goal="最终目标",
            constraints="保留约束✅",
            history=items,
            max_bytes=16384,
        )
        encoded = frame.model_dump_json().encode("utf-8")
        assert len(encoded) <= 16384
        # Valid UTF-8 round-trip
        encoded.decode("utf-8")
        assert frame.truncated is True
        assert frame.omitted_count > 0
        # Recent items kept in ascending seq order at the newest tail
        seqs = [it.seq for it in frame.recent_items]
        assert seqs == sorted(seqs)
        assert seqs[-1] == 30
        assert len(seqs) == len(items) - frame.omitted_count
        # Exact original sha256 and source_ids preserved despite <=1024B text truncation
        orig_by_seq = {it.seq: it for it in items}
        for kept in frame.recent_items:
            assert len(kept.text.encode("utf-8")) <= 1024
            assert kept.truncated is True
            assert kept.sha256 == orig_by_seq[kept.seq].sha256
            assert kept.source_ids == orig_by_seq[kept.seq].source_ids

        # Original full text in DB was NOT truncated or mutated
        db_items = store.load()["history_items"][sid]  # type: ignore[index]
        assert len(db_items) == 30
        assert db_items[0]["text"] == items[0].text
    finally:
        store.close()


def test_c11_max_constraints_emoji_goal_rejections_and_budget_exceeded(tmp_path: Path) -> None:
    pid = str(uuid.uuid4())
    sid = str(uuid.uuid4())
    rid = str(uuid.uuid4())
    max_constraints = "约束A" + ("x" * (MAX_CONSTRAINTS_UTF8_BYTES - len("约束A".encode())))
    assert len(max_constraints.encode("utf-8")) == MAX_CONSTRAINTS_UTF8_BYTES
    emoji_goal = "🚀" * 4096
    assert len(emoji_goal) == 4096

    frame = build_context_frame(
        project_id=pid,
        session_id=sid,
        run_id=rid,
        goal=emoji_goal,
        constraints=max_constraints,
        history=[],
        max_bytes=16384,
    )
    assert frame.constraints == max_constraints
    assert frame.goal_sha256 == hashlib.sha256(emoji_goal.encode("utf-8")).hexdigest()

    # 8193 bytes constraints & NUL rejected
    with pytest.raises(AppError):
        build_context_frame(
            project_id=pid,
            session_id=sid,
            run_id=rid,
            goal="ok",
            constraints="a" * 8193,
            history=[],
        )
    with pytest.raises(AppError):
        build_context_frame(
            project_id=pid,
            session_id=sid,
            run_id=rid,
            goal="ok",
            constraints="bad\x00nul",
            history=[],
        )

    # Tiny max_bytes below fixed fields raises CONTEXT_BUDGET_EXCEEDED without calling provider
    with pytest.raises(AppError) as budget_err:
        build_context_frame(
            project_id=pid,
            session_id=sid,
            run_id=rid,
            goal=emoji_goal,
            constraints=max_constraints,
            history=[],
            max_bytes=256,
        )
    assert budget_err.value.code == "CONTEXT_BUDGET_EXCEEDED"


def test_c12_real_planner_request_size_gate_no_wrapper_bypass(tmp_path: Path) -> None:
    repo = project_helpers.create_buggy_calc_project(tmp_path / "repo")
    data_root = tmp_path / "data"
    out_root = tmp_path / "out"
    data_root.mkdir()
    out_root.mkdir()
    ws = ProjectWorkspace(root=repo, data_root=data_root)
    c12_sid = str(uuid.uuid4())
    c12_rid = str(uuid.uuid4())
    broker = PermissionBroker(run_id=c12_rid, project_root=repo, root_identity=ws.root_identity)
    task, base_pcfg = build_registered_project_task(
        project_id=str(uuid.uuid4()),
        project_name="calc",
        root_identity=ws.root_identity,
        data_root=data_root,
        output_root=out_root,
        run_id=c12_rid,
        session_id=c12_sid,
        goal="🚀" * 100 + " fix calc.py",
        python_executable=Path(sys.executable),
    )

    hist_item = build_history_item_record(
        seq=1,
        project_id=task.project_id,
        session_id=task.session_id,
        run_id=str(uuid.uuid4()),
        kind="user_goal",
        text="Prior user turn",
    )
    frame = build_context_frame(
        project_id=task.project_id,
        session_id=task.session_id,
        run_id=task.run_id,
        goal=task.goal_text,
        constraints="Prioritize calc.py only",
        history=[hist_item],
    )
    conv = ConversationRuntime(context_frame=frame)

    seen_requests: list[ModelRequest] = []

    def _spy_builder(idx: int, req: ModelRequest) -> str:
        seen_requests.append(req)
        return forecast_json(
            action_type="list_directory",
            payload={},
            outcome_type="project_tool_result",
        )

    spy = SpyUsageProvider(_spy_builder)
    # Tight max_request_bytes forces real LLMPlanner REQUEST_TOO_LARGE rejection with 0 provider calls
    from forecontract.runtime.errors import RuntimeError as CoreRuntimeError

    tight_cfg = PlannerConfig(schema_version="0.1", max_request_bytes=512)
    with pytest.raises(CoreRuntimeError) as p_err:
        asyncio.run(
            run_project_task(
                task=task,
                provider=spy,
                planner_config=tight_cfg,
                permissions=broker,
                trace_writer=JsonlTraceStore(data_root / "t_tight.jsonl"),
                emit=lambda _k, _p: None,
                workspace=ws,
                conversation=conv,
            )
        )
    assert p_err.value.code == "DEPENDENCY_FAILURE"
    assert p_err.value.stage == "plan"
    assert len(spy.requests) == 0

    # Normal max_request_bytes delivers exact full goal + conversation unchanged into ModelRequest
    res = asyncio.run(
        run_project_task(
            task=task,
            provider=spy,
            planner_config=base_pcfg,
            permissions=broker,
            trace_writer=JsonlTraceStore(data_root / "t_ok.jsonl"),
            emit=lambda _k, _p: None,
            workspace=ws,
            conversation=ConversationRuntime(context_frame=frame),
        )
    )
    assert res.terminal_status in ("succeeded", "failed")
    assert len(spy.requests) >= 1
    first_req = spy.requests[0]
    assert first_req.model_context.goal_text == task.goal_text
    assert first_req.observation.state["conversation"] == frame.model_dump(mode="json")


def test_c13_native_observation_fields_preserved_and_overflow_blocks_execution(tmp_path: Path) -> None:
    repo = project_helpers.create_buggy_calc_project(tmp_path / "repo")
    data_root = tmp_path / "data"
    out_root = tmp_path / "out"
    data_root.mkdir()
    out_root.mkdir()
    ws = ProjectWorkspace(root=repo, data_root=data_root)
    c13_sid = str(uuid.uuid4())
    c13_rid = str(uuid.uuid4())
    broker = PermissionBroker(run_id=c13_rid, project_root=repo, root_identity=ws.root_identity)
    runner = ProjectCommandRunner(
        run_id=c13_rid, project_root=repo, data_root=data_root, broker=broker
    )
    task, _pcfg = build_registered_project_task(
        project_id=str(uuid.uuid4()),
        project_name="calc",
        root_identity=ws.root_identity,
        data_root=data_root,
        output_root=out_root,
        run_id=c13_rid,
        session_id=c13_sid,
        goal="Verify native observation fields survive large history",
        python_executable=Path(sys.executable),
    )

    from forecontract.models.actions import ActionSpec
    from forecontract_app.project_adapter import ProjectCodingAdapter

    # Approve scope & verification plan and record one failed command
    large_hist = [
        build_history_item_record(
            seq=i,
            project_id=task.project_id,
            session_id=task.session_id,
            run_id=str(uuid.uuid4()),
            kind="answer",
            text=f"turn-{i}-" + ("H" * 900),
        )
        for i in range(1, 20)
    ]
    frame = build_context_frame(
        project_id=task.project_id,
        session_id=task.session_id,
        run_id=task.run_id,
        goal=task.goal_text,
        constraints="Preserve verify failure feedback",
        history=large_hist,
    )
    conv = ConversationRuntime(context_frame=frame)

    adapter_plain = ProjectCodingAdapter(task=task, workspace=ws, broker=broker, command_runner=runner)
    adapter_conv = ProjectCodingAdapter(
        task=task, workspace=ws, broker=broker, command_runner=runner, conversation=conv
    )

    async def _run_compare() -> None:
        await adapter_conv.start()
        # Approve scope + verification plan + execute failing verify_project
        from forecontract_app.project_models import VerificationPlan

        sp = broker.create_scope_proposal(
            scope={
                "exact_paths": ["calc.py"],
                "operations": ["modify"],
                "protected_paths": ["tests/test_calc.py"],
                "verification_plan_summary": "pytest",
            },
            current_instruction_sha256="",
        )
        broker.decide(
            proposal_id=sp.id,
            request=ProposalDecision(digest=sp.digest, decision="approve"),
        )
        vp = broker.create_verification_proposal(
            plan=VerificationPlan(
                plan_id="p-c13",
                checks=[
                    {
                        "check_id": "u1",
                        "kind": "junit_exact",
                        "executable": sys.executable,
                        "argv": [
                            "-B",
                            "-m",
                            "pytest",
                            "-p",
                            "no:cacheprovider",
                            "--junitxml={result_dir}/u.xml",
                            "tests/test_calc.py",
                        ],
                        "cwd": ".",
                        "timeout_ms": 15000,
                        "protected_paths": ["tests/test_calc.py"],
                        "expected_tests": [{"classname": "tests.test_calc", "name": "test_add_basic"}],
                    }
                ],
            ),
            protected_shas=ws.hash_protected_paths(["tests/test_calc.py"]),
            current_instruction_sha256="",
        )
        broker.decide(
            proposal_id=vp.id,
            request=ProposalDecision(digest=vp.digest, decision="approve"),
        )
        # Execute failing verify_project asynchronously while approving command
        v_task = asyncio.create_task(
            adapter_conv.execute(
                ActionSpec(
                    schema_version="0.1",
                    action_type="verify_project",
                    payload={"plan_id": "p-c13", "plan_digest": vp.digest},
                    metadata={"action_id": "act-c13-v"},
                )
            )
        )
        for _ in range(100):
            pend = broker.pending_proposals()
            if pend:
                broker.decide(
                    proposal_id=pend[0].id,
                    request=ProposalDecision(
                        digest=pend[0].digest, decision="approve", trusted_local_execution=True
                    ),
                )
                break
            await asyncio.sleep(0.02)
        out = await v_task
        adapter_plain._last_tool_result = adapter_conv._last_tool_result
        adapter_plain._latest_checks = adapter_conv._latest_checks
        obs_conv = await adapter_conv.observe()
        obs_plain = await adapter_plain.observe()
        assert len(obs_conv.model_dump_json().encode("utf-8")) <= PROJECT_MAX_OBSERVATION_BYTES
        for key in ("scope_approved", "plan_approved", "plan_id", "plan_digest"):
            assert obs_conv.state[key] == obs_plain.state[key]
        assert obs_conv.state["verification"]["status"] == "failed"
        assert obs_conv.state["last_tool_result"]["command_id"] == out.changes["command_id"]
        assert obs_conv.state["last_tool_result"]["exit_code"] == out.changes["exit_code"] != 0

        # Force overall observation overflow (>64 KiB) -> state_complete=False, precondition fails / refused
        adapter_conv._last_tool_result = {
            "action_id": "big",
            "action_type": "read_file",
            "tool_succeeded": True,
            "content": "M" * 90_000,
        }
        obs_over = await adapter_conv.observe()
        assert obs_over.state["observation_overflow"] is True
        assert obs_over.state["state_complete"] is False

    asyncio.run(_run_compare())


def test_c14_two_round_existing_project_reads_history_requires_fresh_approvals(
    tmp_path: Path,
) -> None:
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
    round_observations: list[dict[str, Any]] = []
    round_scopes_approved: list[bool] = []

    def _factory(*, project: Any, run_id: str) -> Any:
        step = 0
        sha_now: str | None = None

        def _build(_idx: int, req: ModelRequest) -> str:
            nonlocal step, sha_now
            st = req.observation.state
            last = st.get("last_tool_result") or {}
            if last.get("action_type") == "read_file":
                sha_val = last.get("sha256") or last.get("file_sha256")
                if sha_val:
                    sha_now = str(sha_val)
            if step == 0:
                round_observations.append(dict(st.get("conversation") or {}))
                round_scopes_approved.append(bool(st.get("scope_approved")))
            step += 1
            if "ROUND1" in req.model_context.goal_text:
                if step == 1:
                    return forecast_json(action_type="read_file", payload={"path": "calc.py"})
                if step == 2:
                    return forecast_json(
                        action_type="propose_scope",
                        payload={
                            "exact_paths": ["calc.py"],
                            "operations": ["modify"],
                            "protected_paths": ["tests/test_calc.py"],
                            "verification_plan_summary": "pytest calc",
                        },
                    )
                if step == 3:
                    return forecast_json(
                        action_type="propose_verification",
                        payload={
                            "plan_id": "plan-r1",
                            "summary": "pytest",
                            "checks": [
                                {
                                    "check_id": "u1",
                                    "kind": "junit_exact",
                                    "executable": sys.executable,
                                    "argv": ["-B", "-m", "pytest", "-p", "no:cacheprovider", "--junitxml={result_dir}/u.xml", "tests/test_calc.py"],
                                    "cwd": ".",
                                    "timeout_ms": 15000,
                                    "protected_paths": ["tests/test_calc.py"],
                                    "expected_tests": [{"classname": "tests.test_calc", "name": "test_add_basic"}],
                                }
                            ],
                        },
                    )
                if step == 4:
                    return forecast_json(
                        action_type="edit_file",
                        payload={
                            "path": "calc.py",
                            "expected_sha256": sha_now,
                            "old_text": "return a - b",
                            "new_text": "return a + b",
                        },
                        outcome_type="project_file_written",
                        outcome_payload={"path": "calc.py"},
                    )
                return forecast_json(
                    action_type="verify_project",
                    payload={"plan_id": str(st.get("plan_id")), "plan_digest": str(st.get("plan_digest"))},
                    outcome_type="project_task_verified",
                    outcome_payload={},
                    task_hint=True,
                )
            # Round 2 proves old scope/plan were NOT inherited (step 0 saw scope_approved=False) and acquires fresh approvals
            if step == 1:
                return forecast_json(action_type="read_file", payload={"path": "calc.py"})
            if step == 2:
                return forecast_json(
                    action_type="propose_scope",
                    payload={
                        "exact_paths": ["calc.py"],
                        "operations": ["modify"],
                        "protected_paths": ["tests/test_calc.py"],
                        "verification_plan_summary": "pytest calc r2",
                    },
                )
            if step == 3:
                return forecast_json(
                    action_type="propose_verification",
                    payload={
                        "plan_id": "plan-r2",
                        "summary": "pytest",
                        "checks": [
                            {
                                "check_id": "u1",
                                "kind": "junit_exact",
                                "executable": sys.executable,
                                "argv": ["-B", "-m", "pytest", "-p", "no:cacheprovider", "--junitxml={result_dir}/u.xml", "tests/test_calc.py"],
                                "cwd": ".",
                                "timeout_ms": 15000,
                                "protected_paths": ["tests/test_calc.py"],
                                "expected_tests": [{"classname": "tests.test_calc", "name": "test_add_basic"}],
                            }
                        ],
                    },
                )
            return forecast_json(
                action_type="verify_project",
                payload={"plan_id": str(st.get("plan_id")), "plan_digest": str(st.get("plan_digest"))},
                outcome_type="project_task_verified",
                outcome_payload={},
                task_hint=True,
            )

        return SpyUsageProvider(_build)

    async def _run_two_rounds() -> None:
        svc = AppService(config=cfg, provider_factory=_factory)
        try:
            proj = await svc.register_project(
                request=RegisterProjectRequest(name="two-round", root=str(repo), confirmed_read=True)
            )
            sess = svc.create_session(
                project_id=proj.project_id,
                model=ModelSelection(provider="offline", model="offline-project-v1"),
            )
            svc.set_session_constraints(
                session_id=sess.session_id,
                request=SetConstraintsRequest(
                    client_request_id=uuid.uuid4(),
                    expected_revision=0,
                    constraints="CONSTRAINT_KEEP_CALC_TESTS_IMMUTABLE",
                ),
            )

            async def _approve_all_for(rid: str, stop_ev: asyncio.Event) -> None:
                while not stop_ev.is_set():
                    for prop in svc.get_proposals(session_id=sess.session_id, run_id=rid):
                        if prop.state == "pending":
                            await svc.decide_proposal(
                                session_id=sess.session_id,
                                run_id=rid,
                                proposal_id=prop.id,
                                request=ProposalDecision(
                                    digest=prop.digest, decision="approve", trusted_local_execution=True
                                ),
                            )
                    await asyncio.sleep(0.02)

            r1 = await svc.start_run(
                session_id=sess.session_id,
                request=StartRunRequest(client_request_id=uuid.uuid4(), goal="ROUND1 fix calc", confirmed_scope=True),
            )
            e1 = asyncio.Event()
            a1 = asyncio.create_task(_approve_all_for(r1.run_id, e1))
            try:
                res1 = await wait_for_run_terminal_async(svc, session_id=sess.session_id, run_id=r1.run_id)
                assert res1.state == "succeeded"
            finally:
                e1.set()
                await a1

            r2 = await svc.start_run(
                session_id=sess.session_id,
                request=StartRunRequest(client_request_id=uuid.uuid4(), goal="ROUND2 verify again", confirmed_scope=True),
            )
            assert r2.run_id != r1.run_id
            e2 = asyncio.Event()
            a2 = asyncio.create_task(_approve_all_for(r2.run_id, e2))
            try:
                res2 = await wait_for_run_terminal_async(svc, session_id=sess.session_id, run_id=r2.run_id)
                assert res2.state == "succeeded"
            finally:
                e2.set()
                await a2

            assert len(round_observations) == 2
            assert round_scopes_approved == [False, False]
            r2_conv = round_observations[1]
            assert r2_conv["constraints"] == "CONSTRAINT_KEEP_CALC_TESTS_IMMUTABLE"
            recent_texts = [it["text"] for it in r2_conv["recent_items"]]
            assert any("ROUND1 fix calc" in t for t in recent_texts)
            assert any("succeeded" in t for t in recent_texts)
            assert len(r2_conv["source_item_ids"]) >= 2
        finally:
            await svc.aclose()

    asyncio.run(_run_two_rounds())


@pytest.mark.parametrize(
    "case_kind",
    ["short", "65_paths", "17_checks", "9_reasons", "513_utf8_reason"],
)
def test_d02_run_summary_marks_all_truncation(tmp_path: Path, case_kind: str) -> None:
    import copy

    base_short: dict[str, Any] = {
        "changed_paths": [
            {"path": "a.py", "operation": "modify", "before_sha256": "0" * 64, "after_sha256": "1" * 64},
        ],
        "checks": {
            "c1": {"passed": True, "exit_code": 0, "command_id": "cmd-1", "failure_reason": None},
        },
        "unverified_reasons": ["short reason"],
    }
    source = copy.deepcopy(base_short)
    if case_kind == "short":
        target_key = "changed_paths"
        expected_truncated = False
    elif case_kind == "65_paths":
        target_key = "changed_paths"
        source["changed_paths"] = [
            {
                "path": f"file_{i}.py",
                "operation": "modify",
                "before_sha256": "0" * 64,
                "after_sha256": "1" * 64,
            }
            for i in range(65)
        ]
        expected_truncated = True
    elif case_kind == "17_checks":
        target_key = "checks"
        source["checks"] = {
            f"chk_{i:02d}": {"passed": True, "exit_code": 0, "command_id": f"cmd-{i}", "failure_reason": None}
            for i in range(17)
        }
        expected_truncated = True
    elif case_kind == "9_reasons":
        target_key = "unverified_reasons"
        source["unverified_reasons"] = [f"reason-{i}" for i in range(9)]
        expected_truncated = True
    else:
        assert case_kind == "513_utf8_reason"
        target_key = "unverified_reasons"
        reason_513 = "界" * 171
        assert len(reason_513.encode("utf-8")) == 513
        source["unverified_reasons"] = [reason_513]
        expected_truncated = True

    original_source = copy.deepcopy(source)
    text, was_truncated = _bounded_run_result_summary(
        state="succeeded",
        verification="passed",
        reason_code="OK",
        result_dict=source,
    )
    assert source == original_source
    assert len(text.encode("utf-8")) <= 32768
    stored = json.loads(text)
    assert stored["state"] == "succeeded"
    assert stored["verification"] == "passed"
    assert stored["reason_code"] == "OK"
    assert was_truncated is expected_truncated
    assert stored[target_key] == original_source[target_key] or was_truncated is True

    if case_kind == "short":
        assert all(
            stored[k] == original_source[k] for k in ("changed_paths", "checks", "unverified_reasons")
        )
        assert was_truncated is False
    elif case_kind == "65_paths":
        assert len(stored["changed_paths"]) == 64
    elif case_kind == "17_checks":
        assert len(stored["checks"]) == 16
    elif case_kind == "9_reasons":
        assert len(stored["unverified_reasons"]) == 8
    else:
        assert len(stored["unverified_reasons"]) == 1
        excerpt = stored["unverified_reasons"][0]
        assert len(excerpt.encode("utf-8")) <= 512
        assert excerpt.encode("utf-8").decode("utf-8") == excerpt

    # Verify HistoryItem persistence preserves the exact truncated flag via SessionStore reload
    store_root = tmp_path / f"store_{case_kind}"
    store_root.mkdir()
    store = SessionStore(data_root=store_root)
    try:
        from forecontract_app.models import ProjectView, SessionView

        p_view = ProjectView(
            project_id=str(uuid.uuid4()),
            name=f"d02-{case_kind}",
            workspace_root=str(tmp_path / f"w_{case_kind}"),
            read_paths=[],
            write_paths=[],
            commands_summary=[],
            expected_tests=[],
        )
        store.insert_project(
            view=p_view,
            kind="controlled",
            workspace_root=p_view.workspace_root,
            canonical_root=f"w_{case_kind}",
            read_grant_id=None,
            spec={"view": p_view.model_dump(mode="json")},
        )
        s_view = SessionView(
            session_id=str(uuid.uuid4()),
            project_id=p_view.project_id,
            model=ModelSelection(provider="offline", model="offline-project-v1"),
        )
        store.insert_session(view=s_view, seq_order=1)
        rid = str(uuid.uuid4())
        item = build_history_item_record(
            seq=1,
            project_id=p_view.project_id,
            session_id=s_view.session_id,
            run_id=rid,
            kind="run_result",
            text=text,
            truncated=was_truncated,
        )
        with store.transaction() as conn:
            store._insert_history_row(conn, item)

        reloaded = store.load()["history_items"][s_view.session_id]  # type: ignore[index]
        assert len(reloaded) == 1
        assert reloaded[0]["truncated"] is expected_truncated
    finally:
        store.close()
