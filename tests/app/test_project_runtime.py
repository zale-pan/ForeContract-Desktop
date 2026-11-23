"""D01 & B19–B24: Native Core RuntimeEngine, 13-action contract, JUnit strict verification, tamper stale, controllable clock, and multifile run."""

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from forecontract.models.actions import ActionSpec
from forecontract.models.outcomes import ActualOutcome, ExecutionStatus
from forecontract.tracing import JsonlTraceStore
from forecontract_app.project_adapter import ProjectCodingAdapter
from forecontract_app.project_commands import ProjectCommandRunner
from forecontract_app.project_demo import (
    OfflineProjectProvider,
    build_registered_project_task,
)
from forecontract_app.project_models import (
    PROJECT_SCHEMA_VERSION,
    SUPPORTED_PROJECT_ACTIONS,
    ExpectedTestCaseView,
    ProjectError,
    ProjectRunResult,
    ProjectTask,
    ProposalDecision,
    ScopeProposalPayload,
    VerificationCheck,
    VerificationPlan,
)
from forecontract_app.project_permissions import PermissionBroker
from forecontract_app.project_runtime import run_project_task
from forecontract_app.project_verification import (
    evaluate_project_result,
    parse_junit_xml_in_result_dir,
)
from forecontract_app.project_workspace import ProjectWorkspace
from forecontract_llm import ModelRequest, ModelResponse
from project_helpers import create_buggy_calc_project, create_multifile_project


def test_d01_l11_compatible_identity_and_delegation(tmp_path, monkeypatch):
    import asyncio
    import sys
    from typing import Any

    import test_coding_api_demo as legacy
    coding_api_demo = legacy.coding_api_demo
    deepseek_provider = legacy.deepseek_provider
    llm_api_smoke = legacy.llm_api_smoke
    coding_agent_demo = legacy.coding_agent_demo
    _FROZEN_EXISTING_SHA256 = legacy._FROZEN_EXISTING_SHA256
    _sha256_bytes = legacy._sha256_bytes
    REPO_ROOT = legacy.REPO_ROOT
    run_coding_task = legacy.run_coding_task
    prepare_demo = legacy.prepare_demo
    build_task = legacy.build_task
    run_session = legacy.run_session
    _make_offline_client = legacy._make_offline_client
    _ScriptedCodingTransport = legacy._ScriptedCodingTransport
    ALLOWED_MODEL = legacy.ALLOWED_MODEL
    PlannerConfig = legacy.PlannerConfig
    assert coding_api_demo.DeepSeekProvider is deepseek_provider.DeepSeekProvider
    assert coding_api_demo._ProviderRecorder is llm_api_smoke._ProviderRecorder
    assert coding_api_demo._bounded_close is llm_api_smoke._bounded_close
    assert coding_api_demo.ALLOWED_ENDPOINT == deepseek_provider.ENDPOINT
    assert coding_api_demo._ACCEPTANCE_TEST == coding_agent_demo._ACCEPTANCE_TEST

    approved_extensions = {
        "packages/adapters/coding/src/forecontract_coding/process.py",
    }
    assert approved_extensions <= set(_FROZEN_EXISTING_SHA256)
    for rel, expected_sha in _FROZEN_EXISTING_SHA256.items():
        if rel in approved_extensions:
            continue
        assert _sha256_bytes((REPO_ROOT / rel).read_bytes()) == expected_sha, rel
    assert _sha256_bytes((REPO_ROOT / "packages/adapters/coding/src/forecontract_coding/process.py").read_bytes()) == "115fa4fee0961c24474158e100d151fed185c35c6920d06ad2c5ccdb13c592db"

    # Verify run_session delegates directly to existing forecontract_coding.run_coding_task
    called: list[bool] = []

    async def _spy_run_coding_task(**kwargs: Any) -> Any:
        called.append(True)
        return await run_coding_task(**kwargs)

    monkeypatch.setattr(coding_api_demo, "run_coding_task", _spy_run_coding_task)
    paths = prepare_demo(tmp_path / "l11_delegate")
    task, cfg, pcfg = build_task(paths, python_executable=sys.executable)
    res = asyncio.run(
        run_session(
            task=task,
            config=cfg,
            provider=deepseek_provider.DeepSeekProvider(
                client=_make_offline_client(_ScriptedCodingTransport()),
                model=ALLOWED_MODEL,
            ),
            planner_config=PlannerConfig(
                schema_version="0.1",
                max_calls=pcfg.max_calls,
                call_timeout_ms=pcfg.call_timeout_ms,
                cleanup_timeout_ms=pcfg.cleanup_timeout_ms,
                max_request_bytes=pcfg.max_request_bytes,
                max_response_bytes=pcfg.max_response_bytes,
                max_output_tokens=pcfg.max_output_tokens,
            ),
            trace_writer=coding_api_demo.JsonlTraceStore(paths["trace"]),
        )
    )
    assert called == [True]
    assert res.terminal_status == "completed"


def _forecast(
    *,
    action_type: str,
    payload: dict[str, Any],
    outcome_type: str = "project_tool_result",
    outcome_payload: dict[str, Any] | None = None,
    task_hint: bool = False,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "schema_version": PROJECT_SCHEMA_VERSION,
        "intent": f"test {action_type}",
        "decision_summary": f"run {action_type}",
        "proposed_action": {
            "schema_version": PROJECT_SCHEMA_VERSION,
            "action_type": action_type,
            "payload": payload,
            "metadata": {},
        },
        "expected_outcomes": [
            {
                "schema_version": PROJECT_SCHEMA_VERSION,
                "outcome_type": outcome_type,
                "payload": outcome_payload
                if outcome_payload is not None
                else {"action_type": action_type, "require_success": False},
                "required": True,
            }
        ],
    }
    if task_hint:
        out["task_completion_hint"] = True
    return out


async def _auto_decide_proposals(
    broker: PermissionBroker,
    *,
    decision: str = "approve",
    trusted_local_execution: bool = True,
    stop_event: asyncio.Event,
) -> None:
    seen: set[str] = set()
    while not stop_event.is_set():
        for prop in broker.pending_proposals():
            if prop.id in seen:
                continue
            seen.add(prop.id)
            trust = trusted_local_execution if prop.kind == "command" else False
            broker.decide(
                proposal_id=prop.id,
                request=ProposalDecision(
                    digest=prop.digest,
                    decision=decision,  # type: ignore[arg-type]
                    trusted_local_execution=trust,
                ),
            )
        await asyncio.sleep(0.02)


def test_b19_13_action_contract_and_rejected_proposal_prevents_writes(tmp_path: Path) -> None:
    repo = create_buggy_calc_project(tmp_path / "repo")
    data_root = tmp_path / "data"
    out_root = tmp_path / "out"
    data_root.mkdir()
    out_root.mkdir()
    before_calc = (repo / "calc.py").read_text(encoding="utf-8")

    ws = ProjectWorkspace(root=repo, data_root=data_root)
    broker = PermissionBroker(run_id="run-b19", project_root=repo, root_identity=ws.root_identity)
    runner = ProjectCommandRunner(run_id="run-b19", project_root=repo, data_root=data_root, broker=broker)
    task, planner_cfg = build_registered_project_task(
        project_id="proj-b19",
        project_name="calc",
        project_root=repo,
        root_identity=ws.root_identity,
        data_root=data_root,
        output_root=out_root,
        run_id="run-b19",
        goal="Reject write scope",
        python_executable=Path(sys.executable),
    )
    adapter = ProjectCodingAdapter(task=task, workspace=ws, broker=broker, command_runner=runner)
    caps = adapter.capabilities()
    assert tuple(caps.supported_actions) == SUPPORTED_PROJECT_ACTIONS
    assert len(caps.supported_actions) == 13
    for legacy_act in ("request_write_grant", "request_command_grant", "run_command", "finalize_task"):
        assert legacy_act not in SUPPORTED_PROJECT_ACTIONS

    trace = JsonlTraceStore(data_root / "trace_b19.jsonl")

    async def _scenario() -> ProjectRunResult:
        stop_ev = asyncio.Event()
        decider = asyncio.create_task(
            _auto_decide_proposals(broker, decision="reject", stop_event=stop_ev)
        )
        try:
            return await run_project_task(
                task=task,
                provider=OfflineProjectProvider(),
                planner_config=planner_cfg,
                permissions=broker,
                trace_writer=trace,
                emit=lambda _k, _p: None,
                workspace=ws,
                command_runner=runner,
            )
        finally:
            stop_ev.set()
            await decider

    res = asyncio.run(_scenario())
    assert res.terminal_status != "succeeded"
    assert res.verification != "passed"
    assert (repo / "calc.py").read_text(encoding="utf-8") == before_calc


class _HintWithoutVerifyProvider:
    def __init__(self, target_sha: str) -> None:
        self._sha = target_sha
        self._step = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self._step += 1
        if self._step == 1:
            body = _forecast(
                action_type="propose_scope",
                payload={
                    "exact_paths": ["calc.py"],
                    "operations": ["modify"],
                    "protected_paths": ["tests/test_calc.py"],
                    "verification_plan_summary": "run calc",
                },
            )
        elif self._step == 2:
            body = _forecast(
                action_type="propose_verification",
                payload={
                    "plan_id": "plan-hint",
                    "summary": "unit check",
                    "checks": [
                        {
                            "check_id": "u1",
                            "kind": "junit_exact",
                            "executable": sys.executable,
                            "argv": ["-B", "-m", "pytest", "--junitxml={result_dir}/u.xml", "tests/test_calc.py"],
                            "cwd": ".",
                            "timeout_ms": 15000,
                            "protected_paths": ["tests/test_calc.py"],
                            "expected_tests": [{"classname": "tests.test_calc", "name": "test_add_basic"}],
                        }
                    ],
                },
            )
        else:
            body = _forecast(
                action_type="edit_file",
                payload={
                    "path": "calc.py",
                    "expected_sha256": self._sha,
                    "old_text": "return a - b",
                    "new_text": "return a + b",
                },
                outcome_type="project_file_written",
                outcome_payload={"path": "calc.py"},
                task_hint=True,
            )
        return ModelResponse(
            schema_version="0.1",
            request_id=request.request_id,
            status="ok",
            content=json.dumps(body),
        )


class _FeedbackRepairProvider:
    def __init__(self, initial_sha: str) -> None:
        self._sha = initial_sha
        self._step = 0
        self.observed_failures: list[dict[str, Any]] = []
        self.observed_cmd_ids: list[str] = []
        self.actions_seen: list[str] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        state = request.observation.state
        last = state.get("last_tool_result") or {}
        if last.get("action_type") == "edit_file" and last.get("read_back_sha256"):
            self._sha = str(last["read_back_sha256"])
        if last.get("action_type") == "verify_project" and last.get("tool_succeeded") is False:
            cmd_id = last.get("command_id")
            exit_code = last.get("exit_code")
            err_text = str(last.get("stderr_or_stdout") or "")
            assert isinstance(cmd_id, str) and cmd_id
            assert isinstance(exit_code, int) and exit_code != 0
            assert "FAILED" in err_text or "assert" in err_text
            assert len(err_text.encode("utf-8")) <= 4096
            self.observed_cmd_ids.append(cmd_id)
            self.observed_failures.append(dict(last))

        self._step += 1
        plan_id = str(state.get("plan_id") or "plan-repair")
        plan_digest = str(state.get("plan_digest") or "")

        if self._step == 1:
            chosen = "propose_scope"
            body = _forecast(
                action_type="propose_scope",
                payload={
                    "exact_paths": ["calc.py"],
                    "operations": ["modify"],
                    "protected_paths": ["tests/test_calc.py"],
                    "verification_plan_summary": "pytest calc",
                },
            )
        elif self._step == 2:
            chosen = "propose_verification"
            body = _forecast(
                action_type="propose_verification",
                payload={
                    "plan_id": "plan-repair",
                    "summary": "junit calc",
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
        elif self._step == 3:
            # First edit is intentionally wrong (a * b instead of a + b)
            chosen = "edit_file"
            body = _forecast(
                action_type="edit_file",
                payload={
                    "path": "calc.py",
                    "expected_sha256": self._sha,
                    "old_text": "return a - b",
                    "new_text": "return a * b",
                },
                outcome_type="project_file_written",
                outcome_payload={"path": "calc.py"},
            )
        elif self._step == 4:
            # First verify_project fails cleanly and surfaces real exit code & output in observation
            chosen = "verify_project"
            body = _forecast(
                action_type="verify_project",
                payload={"plan_id": plan_id, "plan_digest": plan_digest},
                outcome_type="project_tool_result",
                outcome_payload={"action_type": "verify_project", "require_success": False},
            )
        elif self._step == 5:
            # Second edit fixes calc.py ONLY AFTER observing failing verification output
            assert len(self.observed_failures) == 1
            chosen = "edit_file"
            body = _forecast(
                action_type="edit_file",
                payload={
                    "path": "calc.py",
                    "expected_sha256": self._sha,
                    "old_text": "return a * b",
                    "new_text": "return a + b",
                },
                outcome_type="project_file_written",
                outcome_payload={"path": "calc.py"},
            )
        else:
            chosen = "verify_project"
            body = _forecast(
                action_type="verify_project",
                payload={"plan_id": plan_id, "plan_digest": plan_digest},
                outcome_type="project_task_verified",
                outcome_payload={},
                task_hint=True,
            )
        self.actions_seen.append(chosen)
        return ModelResponse(
            schema_version="0.1",
            request_id=request.request_id,
            status="ok",
            content=json.dumps(body),
        )


def test_b20_hint_without_verify_and_feedback_repair(tmp_path: Path) -> None:
    repo = create_buggy_calc_project(tmp_path / "repo")
    data_root = tmp_path / "data"
    out_root = tmp_path / "out"
    data_root.mkdir()
    out_root.mkdir()
    ws = ProjectWorkspace(root=repo, data_root=data_root)
    init_sha = ws.read_file("calc.py").sha256

    broker1 = PermissionBroker(run_id="run-b20-a", project_root=repo, root_identity=ws.root_identity)
    runner1 = ProjectCommandRunner(run_id="run-b20-a", project_root=repo, data_root=data_root, broker=broker1)
    task1, pcfg1 = build_registered_project_task(
        project_id="p-b20-a",
        project_name="calc",
        root_identity=ws.root_identity,
        data_root=data_root,
        output_root=out_root,
        run_id="run-b20-a",
        goal="Hint must not bypass verify_project",
        python_executable=Path(sys.executable),
        max_steps=4,
        max_planning_calls=6,
    )

    async def _run_hint() -> ProjectRunResult:
        stop_ev = asyncio.Event()
        decider = asyncio.create_task(_auto_decide_proposals(broker1, stop_event=stop_ev))
        try:
            return await run_project_task(
                task=task1,
                provider=_HintWithoutVerifyProvider(init_sha),
                planner_config=pcfg1,
                permissions=broker1,
                trace_writer=JsonlTraceStore(data_root / "t_hint.jsonl"),
                emit=lambda _k, _p: None,
                workspace=ws,
                command_runner=runner1,
            )
        finally:
            stop_ev.set()
            await decider

    hint_res = asyncio.run(_run_hint())
    assert hint_res.terminal_status != "succeeded"
    assert hint_res.verification != "passed"

    # Second scenario: feedback-driven repair succeeds ONLY after verify_project fails then passes
    repo2 = create_buggy_calc_project(tmp_path / "repo2")
    ws2 = ProjectWorkspace(root=repo2, data_root=data_root)
    sha2 = ws2.read_file("calc.py").sha256
    repair_events: list[tuple[str, dict[str, Any]]] = []
    broker2 = PermissionBroker(run_id="run-b20-b", project_root=repo2, root_identity=ws2.root_identity)
    runner2 = ProjectCommandRunner(
        run_id="run-b20-b",
        project_root=repo2,
        data_root=data_root,
        broker=broker2,
        emit=lambda k, p: repair_events.append((k, p)),
    )
    task2, pcfg2 = build_registered_project_task(
        project_id="p-b20-b",
        project_name="calc",
        root_identity=ws2.root_identity,
        data_root=data_root,
        output_root=out_root,
        run_id="run-b20-b",
        goal="Repair after verification failure feedback",
        python_executable=Path(sys.executable),
    )

    repair_provider = _FeedbackRepairProvider(sha2)

    async def _run_repair() -> ProjectRunResult:
        stop_ev = asyncio.Event()
        decider = asyncio.create_task(_auto_decide_proposals(broker2, stop_event=stop_ev))
        try:
            return await run_project_task(
                task=task2,
                provider=repair_provider,
                planner_config=pcfg2,
                permissions=broker2,
                trace_writer=JsonlTraceStore(data_root / "t_repair.jsonl"),
                emit=lambda k, p: repair_events.append((k, p)),
                workspace=ws2,
                command_runner=runner2,
            )
        finally:
            stop_ev.set()
            await decider

    rep_res = asyncio.run(_run_repair())
    assert rep_res.terminal_status == "succeeded"
    assert rep_res.verification == "passed"
    assert len(repair_provider.observed_failures) == 1
    assert repair_provider.actions_seen == [
        "propose_scope",
        "propose_verification",
        "edit_file",
        "verify_project",
        "edit_file",
        "verify_project",
    ]
    started_cmds = [p for k, p in repair_events if k == "command_started"]
    assert len(started_cmds) == 2
    failed = runner2.get_command(started_cmds[0]["command_id"])
    observed = repair_provider.observed_failures[0]
    assert observed["command_id"] == failed.command_id
    assert observed["exit_code"] == failed.exit_code != 0
    assert "FAILED" in observed["stderr_or_stdout"] or "assert" in observed["stderr_or_stdout"]
    assert len(observed["stderr_or_stdout"].encode("utf-8")) <= 4096
    assert isinstance(rep_res.checks, dict)
    assert set(rep_res.checks) == {"u1"}
    assert rep_res.checks["u1"].passed is True
    assert rep_res.checks["u1"].command_id != repair_provider.observed_cmd_ids[0]
    repair_trace = (data_root / "t_repair.jsonl").read_text(encoding="utf-8")
    trace_objs = [json.loads(ln) for ln in repair_trace.splitlines() if ln.strip()]
    attempt_statuses = [
        o.get("execution_status") for o in trace_objs if o.get("kind") == "attempt_finished"
    ]
    assert attempt_statuses == [
        "succeeded",
        "succeeded",
        "succeeded",
        "failed",
        "succeeded",
        "succeeded",
    ]

    # Direct-correct baseline path
    repo3 = create_buggy_calc_project(tmp_path / "repo3")
    ws3 = ProjectWorkspace(root=repo3, data_root=data_root)
    broker3 = PermissionBroker(run_id="run-b20-c", project_root=repo3, root_identity=ws3.root_identity)
    runner3 = ProjectCommandRunner(run_id="run-b20-c", project_root=repo3, data_root=data_root, broker=broker3)
    task3, pcfg3 = build_registered_project_task(
        project_id="p-b20-c",
        project_name="calc",
        root_identity=ws3.root_identity,
        data_root=data_root,
        output_root=out_root,
        run_id="run-b20-c",
        goal="Fix calc.py directly",
        python_executable=Path(sys.executable),
    )

    async def _run_direct() -> ProjectRunResult:
        stop_ev = asyncio.Event()
        decider = asyncio.create_task(_auto_decide_proposals(broker3, stop_event=stop_ev))
        try:
            return await run_project_task(
                task=task3,
                provider=OfflineProjectProvider(),
                planner_config=pcfg3,
                permissions=broker3,
                trace_writer=JsonlTraceStore(data_root / "t_direct.jsonl"),
                emit=lambda _k, _p: None,
                workspace=ws3,
                command_runner=runner3,
            )
        finally:
            stop_ev.set()
            await decider

    direct_res = asyncio.run(_run_direct())
    assert direct_res.terminal_status == "succeeded"
    assert direct_res.verification == "passed"
    assert isinstance(direct_res.checks, dict)
    assert set(direct_res.checks) >= {"junit_1"}


def test_b21_junit_strict_matching_and_mixed_exit_code_gate(tmp_path: Path) -> None:
    # Part 1: Direct JUnit parser rejection cases
    expected = [
        ExpectedTestCaseView(classname="tests.test_calc", name="test_add_basic"),
    ]
    cases: list[tuple[str, str]] = [
        ("missing_test", "<testsuite tests='0'></testsuite>"),
        (
            "extra_test",
            "<testsuite><testcase classname='tests.test_calc' name='test_add_basic'/><testcase classname='tests.test_calc' name='test_extra'/></testsuite>",
        ),
        (
            "duplicate_test",
            "<testsuite><testcase classname='tests.test_calc' name='test_add_basic'/><testcase classname='tests.test_calc' name='test_add_basic'/></testsuite>",
        ),
        (
            "skipped_test",
            "<testsuite><testcase classname='tests.test_calc' name='test_add_basic'><skipped/></testcase></testsuite>",
        ),
        (
            "failed_test",
            "<testsuite><testcase classname='tests.test_calc' name='test_add_basic'><failure message='boom'/></testcase></testsuite>",
        ),
        (
            "xxe_doctype",
            "<!DOCTYPE foo [<!ENTITY xxe 'bad'>]><testsuite><testcase classname='tests.test_calc' name='test_add_basic'/></testsuite>",
        ),
    ]
    for label, xml_body in cases:
        rdir = tmp_path / label
        rdir.mkdir()
        (rdir / "out.xml").write_text(xml_body, encoding="utf-8")
        ok, _, _, reason = parse_junit_xml_in_result_dir(result_dir=rdir, expected_tests=expected)
        assert ok is False, f"{label} should be rejected (got reason={reason})"

    # >4 MiB JUnit XML rejected
    huge_dir = tmp_path / "huge_xml"
    huge_dir.mkdir()
    (huge_dir / "huge.xml").write_bytes(b"<testsuite><!-- " + (b"X" * (4 * 1024 * 1024 + 100)) + b" --></testsuite>")
    ok_big, _, _, _ = parse_junit_xml_in_result_dir(result_dir=huge_dir, expected_tests=expected)
    assert ok_big is False

    # Part 2: F03 mixed plan: unit (junit_exact) fails + build (exit_code) exits 0 -> MUST NOT pass!
    repo = create_buggy_calc_project(tmp_path / "repo_mix")
    (repo / "check_build.py").write_text("import sys; print('build ok'); sys.exit(0)\n", encoding="utf-8")
    data_root = tmp_path / "data"
    out_root = tmp_path / "out"
    data_root.mkdir()
    out_root.mkdir()
    ws = ProjectWorkspace(root=repo, data_root=data_root)
    broker = PermissionBroker(run_id="run-b21", project_root=repo, root_identity=ws.root_identity)
    runner = ProjectCommandRunner(run_id="run-b21", project_root=repo, data_root=data_root, broker=broker)
    task = ProjectTask(
        run_id="run-b21",
        goal_id="g-b21",
        goal_text="Mixed plan fail gate",
        project_id="p-b21",
        root_identity=ws.root_identity,
        read_grant_id="rg-b21",
        data_root=data_root,
        output_root=out_root,
        python_executable=Path(sys.executable),
    )
    adapter = ProjectCodingAdapter(task=task, workspace=ws, broker=broker, command_runner=runner)

    async def _scenario() -> ProjectRunResult:
        await adapter.start()
        stop_ev = asyncio.Event()
        decider = asyncio.create_task(_auto_decide_proposals(broker, stop_event=stop_ev))
        try:
            plan_res = await adapter.execute(
                ActionSpec(
                    schema_version="0.1",
                    action_type="propose_verification",
                    payload={
                        "plan_id": "mix-plan",
                        "summary": "unit fails + build 0",
                        "checks": [
                            {
                                "check_id": "unit",
                                "kind": "junit_exact",
                                "executable": sys.executable,
                                "argv": [
                                    "-B", "-m", "pytest", "-p", "no:cacheprovider",
                                    "--junitxml={result_dir}/u.xml", "tests/test_calc.py",
                                ],
                                "cwd": ".",
                                "timeout_ms": 15000,
                                "protected_paths": ["tests/test_calc.py"],
                                "expected_tests": [{"classname": "tests.test_calc", "name": "test_add_basic"}],
                            },
                            {
                                "check_id": "build",
                                "kind": "exit_code",
                                "executable": sys.executable,
                                "argv": ["-B", "check_build.py"],
                                "cwd": ".",
                                "timeout_ms": 15000,
                                "protected_paths": ["check_build.py"],
                            },
                        ],
                    },
                    metadata={"action_id": "m1"},
                )
            )
            assert plan_res.error is None
            p_view, _, _ = broker.verify_plan_approved()
            # calc.py still has bug (a - b), so unit fails while build returns exit 0
            v_res = await adapter.execute(
                ActionSpec(
                    schema_version="0.1",
                    action_type="verify_project",
                    payload={"plan_id": "mix-plan", "plan_digest": p_view.digest},
                    metadata={"action_id": "m2"},
                )
            )
            assert v_res.error is not None
            ev = adapter.build_evidence(core_completed=True)
            return evaluate_project_result(task=task, evidence=ev)
        finally:
            stop_ev.set()
            await decider
            await adapter.stop()

    res = asyncio.run(_scenario())
    assert res.terminal_status == "failed"
    assert res.verification == "failed"
    assert isinstance(res.checks, dict)
    assert set(res.checks) == {"unit", "build"}
    assert res.checks["unit"].passed is False
    assert res.checks["build"].passed is True


def test_b22_protected_source_or_external_tamper_yields_unknown_stale(tmp_path: Path) -> None:
    repo = create_buggy_calc_project(tmp_path / "repo")
    # Fix calc.py up front so verification passes initially, then tamper on disk before final evaluation
    (repo / "calc.py").write_text(
        "def add_numbers(a: int, b: int) -> int:\n    return a + b\n",
        encoding="utf-8",
    )
    data_root = tmp_path / "data"
    out_root = tmp_path / "out"
    data_root.mkdir()
    out_root.mkdir()
    ws = ProjectWorkspace(root=repo, data_root=data_root)
    broker = PermissionBroker(run_id="run-b22", project_root=repo, root_identity=ws.root_identity)
    runner = ProjectCommandRunner(run_id="run-b22", project_root=repo, data_root=data_root, broker=broker)
    task = ProjectTask(
        run_id="run-b22",
        goal_id="g-b22",
        goal_text="Tamper after verify yields unknown",
        project_id="p-b22",
        root_identity=ws.root_identity,
        read_grant_id="rg-b22",
        data_root=data_root,
        output_root=out_root,
        python_executable=Path(sys.executable),
    )
    adapter = ProjectCodingAdapter(task=task, workspace=ws, broker=broker, command_runner=runner)

    async def _scenario() -> ProjectRunResult:
        await adapter.start()
        stop_ev = asyncio.Event()
        decider = asyncio.create_task(_auto_decide_proposals(broker, stop_event=stop_ev))
        try:
            await adapter.execute(
                ActionSpec(
                    schema_version="0.1",
                    action_type="propose_verification",
                    payload={
                        "plan_id": "tamper-plan",
                        "summary": "junit unit",
                        "checks": [
                            {
                                "check_id": "unit",
                                "kind": "junit_exact",
                                "executable": sys.executable,
                                "argv": [
                                    "-B", "-m", "pytest", "-p", "no:cacheprovider",
                                    "--junitxml={result_dir}/u.xml", "tests/test_calc.py",
                                ],
                                "cwd": ".",
                                "timeout_ms": 15000,
                                "protected_paths": ["tests/test_calc.py"],
                                "expected_tests": [{"classname": "tests.test_calc", "name": "test_add_basic"}],
                            }
                        ],
                    },
                    metadata={"action_id": "t1"},
                )
            )
            p_view, _, _ = broker.verify_plan_approved()
            v_res = await adapter.execute(
                ActionSpec(
                    schema_version="0.1",
                    action_type="verify_project",
                    payload={"plan_id": "tamper-plan", "plan_digest": p_view.digest},
                    metadata={"action_id": "t2"},
                )
            )
            assert v_res.error is None
            # Mutate protected test file outside adapter AFTER verify_project passed
            (repo / "tests" / "test_calc.py").write_text(
                "def test_add_basic() -> None:\n    assert True\n", encoding="utf-8"
            )
            ev = adapter.build_evidence(core_completed=True)
            return evaluate_project_result(task=task, evidence=ev)
        finally:
            stop_ev.set()
            await decider
            await adapter.stop()

    res = asyncio.run(_scenario())
    assert res.terminal_status == "failed"
    assert res.verification == "unknown"
    assert res.reason_code == "VERIFICATION_STALE"


def test_b23_default_budgets_and_controllable_clock_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = create_buggy_calc_project(tmp_path / "repo")
    data_root = tmp_path / "data"
    out_root = tmp_path / "out"
    data_root.mkdir()
    out_root.mkdir()
    ws = ProjectWorkspace(root=repo, data_root=data_root)

    default_task, _ = build_registered_project_task(
        project_id="p-def",
        project_name="calc",
        root_identity=ws.root_identity,
        data_root=data_root,
        output_root=out_root,
        run_id="r-def",
        goal="Check defaults",
        python_executable=Path(sys.executable),
    )
    assert default_task.max_steps == 120
    assert default_task.max_planning_calls == 64
    assert default_task.max_replans == 32
    assert default_task.run_timeout_ms == 600_000
    assert default_task.proposal_ttl_seconds == 120.0

    # Controllable monotonic clock jumping past 600s without sleeping 10 real minutes
    ns_now = 1_000_000_000

    def _mono_ns() -> int:
        return ns_now

    class _ClockAdvanceProvider:
        async def complete(self, request: ModelRequest) -> ModelResponse:
            nonlocal ns_now
            # Advance clock 650 seconds on second call -> exceeds 600,000 ms budget immediately
            ns_now += 650_000_000_000
            body = _forecast(action_type="list_directory", payload={"path": ".", "limit": 10})
            return ModelResponse(
                schema_version="0.1",
                request_id=request.request_id,
                status="ok",
                content=json.dumps(body),
            )

    broker = PermissionBroker(run_id="r-clk", project_root=repo, root_identity=ws.root_identity)
    runner = ProjectCommandRunner(run_id="r-clk", project_root=repo, data_root=data_root, broker=broker)
    task_clk, pcfg_clk = build_registered_project_task(
        project_id="p-clk",
        project_name="calc",
        root_identity=ws.root_identity,
        data_root=data_root,
        output_root=out_root,
        run_id="r-clk",
        goal="Budget expiry under controllable clock",
        python_executable=Path(sys.executable),
    )
    res = asyncio.run(
        run_project_task(
            task=task_clk,
            provider=_ClockAdvanceProvider(),
            planner_config=pcfg_clk,
            permissions=broker,
            trace_writer=JsonlTraceStore(data_root / "t_clk.jsonl"),
            emit=lambda _k, _p: None,
            workspace=ws,
            command_runner=runner,
            monotonic_clock=_mono_ns,
        )
    )
    assert res.terminal_status == "failed"
    assert res.verification != "passed"

    class _ListDirLoopProvider:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, request: ModelRequest) -> ModelResponse:
            self.calls += 1
            body = _forecast(action_type="list_directory", payload={"path": ".", "limit": 5})
            return ModelResponse(
                schema_version="0.1",
                request_id=request.request_id,
                status="ok",
                content=json.dumps(body),
            )

    # Exact max_steps=120 cutoff (step 121 never executes)
    broker_ms = PermissionBroker(run_id="r-steps", project_root=repo, root_identity=ws.root_identity)
    runner_ms = ProjectCommandRunner(run_id="r-steps", project_root=repo, data_root=data_root, broker=broker_ms)
    task_ms, pcfg_ms = build_registered_project_task(
        project_id="p-steps",
        project_name="calc",
        root_identity=ws.root_identity,
        data_root=data_root,
        output_root=out_root,
        run_id="r-steps",
        goal="Exact max steps cutoff",
        python_executable=Path(sys.executable),
        max_steps=120,
        max_planning_calls=121,
        max_replans=0,
    )
    prov_ms = _ListDirLoopProvider()
    res_ms = asyncio.run(
        run_project_task(
            task=task_ms,
            provider=prov_ms,
            planner_config=pcfg_ms,
            permissions=broker_ms,
            trace_writer=JsonlTraceStore(data_root / "t_steps.jsonl"),
            emit=lambda _k, _p: None,
            workspace=ws,
            command_runner=runner_ms,
        )
    )
    assert res_ms.steps_executed == 120
    assert prov_ms.calls == 120
    assert res_ms.core_terminal_reason == "MAX_STEPS_EXHAUSTED"

    # Exact max_planning_calls=64 cutoff (call 65 never runs)
    broker_mp = PermissionBroker(run_id="r-plan", project_root=repo, root_identity=ws.root_identity)
    runner_mp = ProjectCommandRunner(run_id="r-plan", project_root=repo, data_root=data_root, broker=broker_mp)
    task_mp, pcfg_mp = build_registered_project_task(
        project_id="p-plan",
        project_name="calc",
        root_identity=ws.root_identity,
        data_root=data_root,
        output_root=out_root,
        run_id="r-plan",
        goal="Exact max planning calls cutoff",
        python_executable=Path(sys.executable),
        max_steps=121,
        max_planning_calls=64,
        max_replans=0,
    )
    prov_mp = _ListDirLoopProvider()
    res_mp = asyncio.run(
        run_project_task(
            task=task_mp,
            provider=prov_mp,
            planner_config=pcfg_mp,
            permissions=broker_mp,
            trace_writer=JsonlTraceStore(data_root / "t_plan.jsonl"),
            emit=lambda _k, _p: None,
            workspace=ws,
            command_runner=runner_mp,
        )
    )
    assert res_mp.planning_calls_used == 64
    assert prov_mp.calls == 64
    assert res_mp.core_terminal_reason == "MAX_PLANNING_EXHAUSTED"

    # Pending proposal auto-closes cleanly when run budget expires while waiting for decision
    class _ScopeWaitProvider:
        async def complete(self, request: ModelRequest) -> ModelResponse:
            body = _forecast(
                action_type="propose_scope",
                payload={
                    "exact_paths": ["calc.py"],
                    "operations": ["modify"],
                    "protected_paths": ["tests/test_calc.py"],
                    "verification_plan_summary": "wait timeout",
                },
            )
            return ModelResponse(
                schema_version="0.1",
                request_id=request.request_id,
                status="ok",
                content=json.dumps(body),
            )

    broker_wait = PermissionBroker(run_id="r-wait", project_root=repo, root_identity=ws.root_identity)
    runner_wait = ProjectCommandRunner(
        run_id="r-wait", project_root=repo, data_root=data_root, broker=broker_wait
    )
    task_wait, pcfg_wait = build_registered_project_task(
        project_id="p-wait",
        project_name="calc",
        root_identity=ws.root_identity,
        data_root=data_root,
        output_root=out_root,
        run_id="r-wait",
        goal="Expire run while proposal pending",
        python_executable=Path(sys.executable),
        max_steps=5,
        max_planning_calls=5,
        max_replans=0,
        run_timeout_ms=1500,
    )
    res_wait = asyncio.run(
        run_project_task(
            task=task_wait,
            provider=_ScopeWaitProvider(),
            planner_config=pcfg_wait,
            permissions=broker_wait,
            trace_writer=JsonlTraceStore(data_root / "t_wait.jsonl"),
            emit=lambda _k, _p: None,
            workspace=ws,
            command_runner=runner_wait,
        )
    )
    assert res_wait.terminal_status == "failed"
    pending_list = broker_wait.list_proposals()
    assert len(pending_list) >= 1
    assert pending_list[0].state == "revoked"
    with pytest.raises(ProjectError) as rev_err:
        broker_wait.decide(
            proposal_id=pending_list[0].id,
            request=ProposalDecision(digest=pending_list[0].digest, decision="approve"),
        )
    assert rev_err.value.code == "PROPOSAL_CANCELLED"

    # Bounded provider.aclose() under cleanup timeout (hanging 60s close interrupted in <= ~2s)
    class _HangingCloseProvider:
        async def complete(self, request: ModelRequest) -> ModelResponse:
            body = _forecast(action_type="list_directory", payload={"path": ".", "limit": 5})
            return ModelResponse(
                schema_version="0.1",
                request_id=request.request_id,
                status="ok",
                content=json.dumps(body),
            )

        async def aclose(self) -> None:
            await asyncio.sleep(60.0)

    close_events: list[tuple[str, dict[str, Any]]] = []
    broker_cl = PermissionBroker(run_id="r-close", project_root=repo, root_identity=ws.root_identity)
    runner_cl = ProjectCommandRunner(run_id="r-close", project_root=repo, data_root=data_root, broker=broker_cl)
    task_cl, pcfg_cl = build_registered_project_task(
        project_id="p-close",
        project_name="calc",
        root_identity=ws.root_identity,
        data_root=data_root,
        output_root=out_root,
        run_id="r-close",
        goal="Bounded provider close",
        python_executable=Path(sys.executable),
        max_steps=1,
        max_planning_calls=2,
        max_replans=0,
    )
    t_start = time.monotonic()
    asyncio.run(
        run_project_task(
            task=task_cl,
            provider=_HangingCloseProvider(),
            planner_config=pcfg_cl,
            permissions=broker_cl,
            trace_writer=JsonlTraceStore(data_root / "t_close.jsonl"),
            emit=lambda k, p: close_events.append((k, p)),
            workspace=ws,
            command_runner=runner_cl,
        )
    )
    elapsed_close = time.monotonic() - t_start
    assert elapsed_close < 6.0
    assert any(k == "provider_close_started" for k, _ in close_events)
    finished_close = [p for k, p in close_events if k == "provider_close_finished"]
    assert len(finished_close) == 1
    assert finished_close[0]["timed_out"] is True
    assert finished_close[0]["ok"] is False

    # Non-idempotent UNKNOWN outcome is NOT auto-retried (stays 1 step / 1 call)
    calc_sha = ws.read_file("calc.py").sha256

    class _SingleEditUnknownProvider:
        async def complete(self, request: ModelRequest) -> ModelResponse:
            body = _forecast(
                action_type="edit_file",
                payload={
                    "path": "calc.py",
                    "expected_sha256": calc_sha,
                    "old_text": "return a - b",
                    "new_text": "return a + b",
                },
                outcome_type="project_file_written",
                outcome_payload={"path": "calc.py"},
            )
            return ModelResponse(
                schema_version="0.1",
                request_id=request.request_id,
                status="ok",
                content=json.dumps(body),
            )

    broker_unk = PermissionBroker(run_id="r-unk", project_root=repo, root_identity=ws.root_identity)
    scope_unk = broker_unk.create_scope_proposal(
        scope=ScopeProposalPayload(
            exact_paths=["calc.py"],
            operations=["modify"],
            protected_paths=[],
            verification_plan_summary="edit calc",
        ),
        current_instruction_sha256="",
    )
    broker_unk.decide(
        proposal_id=scope_unk.id,
        request=ProposalDecision(digest=scope_unk.digest, decision="approve"),
    )
    plan_unk = broker_unk.create_verification_proposal(
        plan=VerificationPlan(
            plan_id="plan-unk",
            summary="verify calc",
            checks=[
                VerificationCheck(
                    check_id="u1",
                    kind="exit_code",
                    executable=sys.executable,
                    argv=["-B", "-c", "print('ok')"],
                    cwd=".",
                    timeout_ms=5000,
                )
            ],
        ),
        protected_shas={},
        current_instruction_sha256="",
    )
    broker_unk.decide(
        proposal_id=plan_unk.id,
        request=ProposalDecision(digest=plan_unk.digest, decision="approve"),
    )
    orig_exec = ProjectCodingAdapter.execute
    edit_exec_calls = 0

    async def _unk_exec(self_ad: ProjectCodingAdapter, action: Any) -> ActualOutcome:
        nonlocal edit_exec_calls
        if getattr(action, "action_type", "") == "edit_file":
            edit_exec_calls += 1
            return ActualOutcome(
                schema_version=PROJECT_SCHEMA_VERSION,
                outcome_id=f"out-{getattr(action, 'metadata', {}).get('action_id', 'unk-1')}",
                status=ExecutionStatus.UNKNOWN,
                changes={"effect_applied": None},
            )
        return await orig_exec(self_ad, action)

    monkeypatch.setattr(ProjectCodingAdapter, "execute", _unk_exec)
    runner_unk = ProjectCommandRunner(run_id="r-unk", project_root=repo, data_root=data_root, broker=broker_unk)
    task_unk, pcfg_unk = build_registered_project_task(
        project_id="p-unk",
        project_name="calc",
        root_identity=ws.root_identity,
        data_root=data_root,
        output_root=out_root,
        run_id="r-unk",
        goal="Non-idempotent unknown no auto-retry",
        python_executable=Path(sys.executable),
        max_steps=5,
        max_planning_calls=5,
        max_replans=0,
    )
    res_unk = asyncio.run(
        run_project_task(
            task=task_unk,
            provider=_SingleEditUnknownProvider(),
            planner_config=pcfg_unk,
            permissions=broker_unk,
            trace_writer=JsonlTraceStore(data_root / "t_unk.jsonl"),
            emit=lambda _k, _p: None,
            workspace=ws,
            command_runner=runner_unk,
        )
    )
    monkeypatch.undo()
    assert edit_exec_calls == 1
    assert res_unk.steps_executed == 1
    assert res_unk.terminal_status != "succeeded"


def test_b24_multifile_exploration_create_two_edits_junit_and_build(tmp_path: Path) -> None:
    repo = create_multifile_project(tmp_path / "multifile", include_large_notes=True, doc_count=20)
    data_root = tmp_path / "data"
    out_root = tmp_path / "out"
    data_root.mkdir()
    out_root.mkdir()

    ws = ProjectWorkspace(root=repo, data_root=data_root)
    broker = PermissionBroker(
        run_id="run-b24",
        project_root=repo,
        project_id="proj-b24",
        session_id="sess-b24",
        root_identity=ws.root_identity,
    )
    runner = ProjectCommandRunner(run_id="run-b24", project_root=repo, data_root=data_root, broker=broker)
    task, planner_cfg = build_registered_project_task(
        project_id="proj-b24",
        project_name="multifile",
        root_identity=ws.root_identity,
        data_root=data_root,
        output_root=out_root,
        run_id="run-b24",
        goal="Fix mathops and formatting, create src/constants.py, verify tests + check_build.py",
        python_executable=Path(sys.executable),
    )
    events: list[tuple[str, dict[str, Any]]] = []

    async def _scenario() -> ProjectRunResult:
        stop_ev = asyncio.Event()
        decider = asyncio.create_task(_auto_decide_proposals(broker, stop_event=stop_ev))
        try:
            return await run_project_task(
                task=task,
                provider=OfflineProjectProvider(),
                planner_config=planner_cfg,
                permissions=broker,
                trace_writer=JsonlTraceStore(data_root / "t_b24.jsonl"),
                emit=lambda k, p: events.append((k, p)),
                workspace=ws,
                command_runner=runner,
            )
        finally:
            stop_ev.set()
            await decider

    res = asyncio.run(_scenario())
    assert res.terminal_status == "succeeded"
    assert res.verification == "passed"
    assert res.reason_code == "TASK_COMPLETED"
    assert res.plan_digest is not None
    assert (repo / "src" / "constants.py").exists()
    changed = {c["path"]: c["operation"] for c in res.changed_paths}
    assert changed == {
        "src/constants.py": "create",
        "src/formatting.py": "modify",
        "src/mathops.py": "modify",
    }
    assert isinstance(res.checks, dict)
    assert set(res.checks) == {"junit_1", "build_check"}
    check_kinds = {cid: c.kind for cid, c in res.checks.items()}
    assert check_kinds["build_check"] == "exit_code"
    assert any(k == "junit_exact" for k in check_kinds.values())
    assert all(cid == c.check_id and c.passed for cid, c in res.checks.items())


def test_trial_scope_guidance_and_diagnostics(tmp_path: Path) -> None:
    """T21: strict propose_scope rejection with sanitized, run-scoped diagnostics on one host."""
    import hashlib
    import uuid as _uuid

    from forecontract_app.models import ModelSelection, StartRunRequest
    from forecontract_app.project_models import RegisterProjectRequest
    from forecontract_app.service import AppService
    from project_helpers import make_project_host_config

    class _TrialScopeProvider:
        """Synthetic provider: valid read_file, then the supplied propose_scope payload."""

        def __init__(self, *, payload: dict[str, Any]) -> None:
            self._payload = payload
            self._step = 0
            self.guidance_seen: list[dict[str, Any]] = []

        async def complete(self, request: ModelRequest) -> ModelResponse:
            self._step += 1
            self.guidance_seen.append(
                dict(request.model_context.action_guidance.get("propose_scope") or {})
            )
            if self._step == 1:
                body = _forecast(action_type="read_file", payload={"path": "calc.py"})
            else:
                body = _forecast(action_type="propose_scope", payload=self._payload)
            return ModelResponse(
                schema_version="0.1",
                request_id=request.request_id,
                status="ok",
                content=json.dumps(body),
            )

    marker_op = "synthetic-sensitive-operation-7f3a"
    marker_field = "synthetic-unexpected-field-9c2b"
    repo = create_buggy_calc_project(tmp_path / "host" / "repo")
    cfg = make_project_host_config(tmp_path / "host", allowed_roots=(repo,))
    providers: list[Any] = [
        _TrialScopeProvider(
            payload={
                "exact_paths": ["calc.py"],
                "operations": ["edit"],
                "verification_plan_summary": "run calc tests",
            }
        ),
        _TrialScopeProvider(
            payload={
                "exact_paths": ["calc.py"],
                "operations": [marker_op],
                "verification_plan_summary": "run calc tests",
            }
        ),
        _TrialScopeProvider(
            payload={
                "exact_paths": ["calc.py"],
                "operations": ["modify"],
                "verification_plan_summary": "run calc tests",
                marker_field: "ignored",
            }
        ),
        OfflineProjectProvider(),
        _TrialScopeProvider(
            payload={
                "exact_paths": [],
                "write_prefixes": [],
                "operations": ["modify"],
                "verification_plan_summary": "run calc tests",
            }
        ),
    ]

    created_providers: list[Any] = []

    def _factory(**_: Any) -> Any:
        provider = providers.pop(0)
        created_providers.append(provider)
        return provider

    async def _drive(svc: AppService, sid: str, rid: str) -> Any:
        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            view = svc.get_run(session_id=sid, run_id=rid)
            if view.state in {"succeeded", "failed", "cancelled", "unknown"}:
                return view
            for prop in svc.get_proposals(session_id=sid, run_id=rid):
                if prop.state == "pending":
                    await svc.decide_proposal(
                        session_id=sid,
                        run_id=rid,
                        proposal_id=prop.id,
                        request=ProposalDecision(
                            digest=prop.digest,
                            decision="approve",
                            trusted_local_execution=(prop.kind == "command"),
                        ),
                    )
            await asyncio.sleep(0.05)
        raise AssertionError("timed out driving run")

    async def _host_scenario() -> dict[str, Any]:
        svc = AppService(config=cfg, provider_factory=_factory)
        data: dict[str, Any] = {}
        original_finalize = svc._finalize_terminal
        oversized_inner = "0" * 4300 + "1"
        oversized_field = f"operations[{oversized_inner}]"
        oversized_reason = (
            f"propose_scope: field {oversized_field} rejected; "
            "allowed fields: exact_paths, write_prefixes, operations, protected_paths, "
            "verification_plan_summary; allowed operations: create, modify"
        )
        oversized_run_id: str | None = None

        async def _inject_oversized_reason(run_rec: Any, **kwargs: Any) -> None:
            if oversized_run_id == run_rec.view.run_id:
                assert kwargs["state"] == "failed"
                assert kwargs["verification"] == "not_run"
                assert kwargs["reason_code"] == "EVIDENCE_INVALID"
                result_dict = kwargs["result_dict"]
                assert isinstance(result_dict, dict)
                kwargs["result_dict"] = {
                    **result_dict,
                    "unverified_reasons": [oversized_reason],
                }
                data["oversized_injected_run_id"] = run_rec.view.run_id
            await original_finalize(run_rec, **kwargs)

        svc._finalize_terminal = _inject_oversized_reason
        try:
            proj = await svc.register_project(
                request=RegisterProjectRequest(name="trial", root=str(repo), confirmed_read=True)
            )
            sid = svc.create_session(
                project_id=proj.project_id,
                model=ModelSelection(provider="offline", model="offline-project-v1"),
            ).session_id
            for key in ("lit", "op", "field", "succeed", "unrelated"):
                run = await svc.start_run(
                    session_id=sid,
                    request=StartRunRequest(
                        client_request_id=_uuid.uuid4(),
                        goal="Trial invalid write scope",
                        confirmed_scope=True,
                    ),
                )
                data[key] = (run.run_id, await _drive(svc, sid, run.run_id))
                if key == "lit":
                    data["lit_bytes"] = (repo / "calc.py").read_bytes()
                    data["lit_proposals"] = list(
                        svc.get_proposals(session_id=sid, run_id=run.run_id)
                    )
                snap = svc.snapshot(session_id=sid, run_id=run.run_id)
                finished = [ev for ev in snap.events if ev.kind == "run_finished"]
                data[f"{key}_events"] = finished
            # Separate offline run: inject one malformed internal rejection reason only
            # at the original host terminal boundary, then read back that exact run.
            providers.append(
                _TrialScopeProvider(
                    payload={
                        "exact_paths": ["calc.py"],
                        "operations": ["edit"],
                        "verification_plan_summary": "run calc tests",
                    }
                )
            )
            oversized_run = await svc.start_run(
                session_id=sid,
                request=StartRunRequest(
                    client_request_id=_uuid.uuid4(),
                    goal="Trial malformed internal scope index",
                    confirmed_scope=True,
                ),
            )
            oversized_run_id = oversized_run.run_id
            data["oversized"] = (oversized_run_id, await _drive(svc, sid, oversized_run_id))
            data["oversized_events"] = [
                ev
                for ev in svc.snapshot(session_id=sid, run_id=oversized_run_id).events
                if ev.kind == "run_finished"
            ]
            data["oversized_session_id"] = sid
            data["oversized_reason"] = oversized_reason
            data["lit_reselect"] = svc.get_run(
                session_id=sid, run_id=data["lit"][0]
            )
            return data
        finally:
            await svc.aclose()

    baseline_bytes = (repo / "calc.py").read_bytes()
    data = asyncio.run(_host_scenario())

    # The oversized reason was actually passed to the original terminal method
    # for this run; read its persisted view and unique event using the same IDs.
    oversized_id, oversized_view = data["oversized"]
    assert data["oversized_injected_run_id"] == oversized_id
    assert data["oversized_session_id"] == oversized_view.session_id
    assert oversized_view.run_id == oversized_id
    assert (oversized_view.state, oversized_view.verification, oversized_view.reason_code) == (
        "failed", "not_run", "EVIDENCE_INVALID"
    )
    assert data["oversized_reason"] in (oversized_view.result or {}).get(
        "unverified_reasons", []
    )
    oversized_events = data["oversized_events"]
    assert len(oversized_events) == 1
    assert oversized_events[0].run_id == oversized_id
    assert "scope_diagnostic" not in oversized_events[0].payload

    def _reasons(view: Any) -> list[str]:
        result = getattr(view, "result", None) or {}
        raw = result.get("unverified_reasons") if isinstance(result, dict) else None
        return [str(r) for r in raw] if isinstance(raw, list) else []

    # literal "edit" is rejected before the action contract and keeps the file unchanged
    lit_view = data["lit"][1]
    assert lit_view.state == "failed"
    assert lit_view.reason_code == "EVIDENCE_INVALID"
    assert lit_view.verification == "not_run"
    lit_joined = " | ".join(_reasons(lit_view))
    assert "propose_scope" in lit_joined
    assert "operations[0]" in lit_joined
    assert "create" in lit_joined and "modify" in lit_joined

    # marker A as an invalid operations value -> whitelisted operations[0], marker never echoed
    op_view = data["op"][1]
    assert op_view.state == "failed"
    op_joined = " | ".join(_reasons(op_view))
    assert "operations[0]" in op_joined
    assert marker_op not in op_joined
    assert marker_op not in json.dumps(op_view.result or {})

    # marker B as an unexpected field name -> whitelisted payload, marker never echoed
    field_view = data["field"][1]
    assert field_view.state == "failed"
    field_joined = " | ".join(_reasons(field_view))
    assert "field payload rejected" in field_joined
    assert marker_field not in field_joined
    assert marker_field not in json.dumps(field_view.result or {})

    # the genuinely succeeded run and the unrelated failure carry no previous rejection diagnostic
    succeed_view = data["succeed"][1]
    assert succeed_view.state == "succeeded"
    assert all("field operations[0]" not in r for r in _reasons(succeed_view))
    unrelated_view = data["unrelated"][1]
    assert unrelated_view.state == "failed"
    unrelated_joined = " | ".join(_reasons(unrelated_view))
    assert "exact_paths or write_prefixes" in unrelated_joined
    assert "operations[0]" not in unrelated_joined

    # re-selecting the original rejection run keeps its original diagnostic
    reselect = data["lit_reselect"]
    assert reselect.run_id == data["lit"][0]
    assert "operations[0]" in " | ".join(_reasons(reselect))

    # the actual ModelRequest guidance constrains propose_scope
    guidance = created_providers[0].guidance_seen[0]
    props = guidance.get("properties") or {}
    operations = props.get("operations") or {}
    assert (operations.get("items") or {}).get("enum") == ["create", "modify"]
    assert operations.get("minItems") == 1
    assert (props.get("verification_plan_summary") or {}).get("maxLength") == 1024

    # literal edit: calc.py bytes unchanged, no changed paths, no approval, one read_file contract
    assert data["lit_bytes"] == baseline_bytes
    assert (lit_view.result or {}).get("changed_paths") == []
    assert data["lit_proposals"] == []
    lit_trace = cfg.data_root / "runs" / data["lit"][0] / "trace.jsonl"
    assert lit_trace.exists()
    lit_trace_text = lit_trace.read_text(encoding="utf-8")
    lit_trace_records = [
        json.loads(line) for line in lit_trace_text.splitlines() if line.strip()
    ]
    lit_committed = [r for r in lit_trace_records if r.get("kind") == "contract_committed"]
    assert len(lit_committed) == 1
    lit_step_starts = [r for r in lit_trace_records if r.get("kind") == "step_started"]
    assert len(lit_step_starts) == 1
    # the only started and only committed contract is the first (read_file) step; scope never committed
    assert lit_committed[0]["step_id"] == lit_step_starts[0]["step_id"]
    assert len([r for r in lit_trace_records if r.get("kind") == "attempt_started"]) == 1

    # the unique run_finished carries a trusted, sanitized scope_diagnostic only for rejections
    def _diag(key: str) -> Any:
        events = data[f"{key}_events"]
        assert len(events) == 1
        return events[0].payload.get("scope_diagnostic")

    for key, expected_field in (
        ("lit", "operations[0]"),
        ("op", "operations[0]"),
        ("field", "payload"),
    ):
        assert _diag(key) == {
            "action": "propose_scope",
            "field": expected_field,
            "allowed_operations": ["create", "modify"],
        }
    assert _diag("succeed") is None
    assert _diag("unrelated") == {
        "action": "propose_scope",
        "field": "exact_paths",
        "allowed_operations": ["create", "modify"],
    }
    serialized_events = json.dumps(
        [
            ev.model_dump(mode="json")
            for key in ("lit", "op", "field")
            for ev in data[f"{key}_events"]
        ]
    )
    assert marker_op not in serialized_events
    assert marker_field not in serialized_events
    assert "ValidationError" not in serialized_events
    assert "Traceback" not in serialized_events

    # legal modify: the proposal binds calc.py/modify and the file bytes stay identical while pending
    legal_repo = create_buggy_calc_project(tmp_path / "legal" / "repo")
    (tmp_path / "legal" / "data").mkdir(parents=True)
    (tmp_path / "legal" / "out").mkdir(parents=True)
    legal_before = hashlib.sha256((legal_repo / "calc.py").read_bytes()).hexdigest()
    ws = ProjectWorkspace(root=legal_repo, data_root=tmp_path / "legal" / "data")
    broker = PermissionBroker(
        run_id="run-legal", project_root=legal_repo, root_identity=ws.root_identity
    )
    runner = ProjectCommandRunner(
        run_id="run-legal", project_root=legal_repo, data_root=tmp_path / "legal" / "data", broker=broker
    )
    task, _cfg = build_registered_project_task(
        project_id="proj-legal",
        project_name="calc",
        project_root=legal_repo,
        root_identity=ws.root_identity,
        data_root=tmp_path / "legal" / "data",
        output_root=tmp_path / "legal" / "out",
        run_id="run-legal",
        goal="Trial legal modify",
        python_executable=Path(sys.executable),
    )
    adapter = ProjectCodingAdapter(task=task, workspace=ws, broker=broker, command_runner=runner)
    scope_action = ActionSpec(
        schema_version=PROJECT_SCHEMA_VERSION,
        action_type="propose_scope",
        payload={
            "exact_paths": ["calc.py"],
            "operations": ["modify"],
            "verification_plan_summary": "run calc tests",
        },
    )
    observed: dict[str, Any] = {}

    async def _legal_modify() -> Any:
        exec_task = asyncio.create_task(adapter.execute(scope_action))
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            scope_props = [p for p in broker.pending_proposals() if p.kind == "scope"]
            if scope_props:
                observed["pending"] = scope_props[0]
                observed["sha_while_pending"] = hashlib.sha256(
                    (legal_repo / "calc.py").read_bytes()
                ).hexdigest()
                broker.decide(
                    proposal_id=scope_props[0].id,
                    request=ProposalDecision(digest=scope_props[0].digest, decision="reject"),
                )
                break
            await asyncio.sleep(0.02)
        return await asyncio.wait_for(exec_task, timeout=20.0)

    outcome = asyncio.run(_legal_modify())
    pending = observed.get("pending")
    assert pending is not None
    payload_json = json.dumps(pending.payload)
    assert "calc.py" in payload_json
    assert "modify" in payload_json
    assert observed["sha_while_pending"] == legal_before
    assert outcome.status != ExecutionStatus.SUCCEEDED
    assert hashlib.sha256((legal_repo / "calc.py").read_bytes()).hexdigest() == legal_before


def test_trial_verification_guidance_and_diagnostics(tmp_path: Path) -> None:
    """APP-T06 round-027: propose_verification model guidance, positive approval, and sanitized diagnostics."""
    import uuid as _uuid

    from forecontract.protocols.adapter import InvalidActionPayloadError
    from forecontract_app.models import ModelSelection, StartRunRequest
    from forecontract_app.project_models import (
        PROJECT_MAX_COMMAND_ARG_BYTES,
        PROJECT_MAX_COMMAND_ARGS,
        PROJECT_MAX_COMMAND_ARGV_TOTAL_BYTES,
        PROJECT_MAX_COMMAND_TIMEOUT_MS,
        RegisterProjectRequest,
        VerificationPlan,
    )
    from forecontract_app.service import AppService
    from project_helpers import make_project_host_config
    from pydantic import ValidationError

    class _TrialVerificationProvider:
        """Offline provider: valid read_file, then supplied propose_verification payload."""

        def __init__(self, *, payload: dict[str, Any]) -> None:
            self._payload = payload
            self._step = 0
            self.guidance_seen: list[dict[str, Any]] = []

        async def complete(self, request: ModelRequest) -> ModelResponse:
            self._step += 1
            self.guidance_seen.append(
                dict(request.model_context.action_guidance.get("propose_verification") or {})
            )
            if self._step == 1:
                body = _forecast(action_type="read_file", payload={"path": "calc.py"})
            else:
                body = _forecast(action_type="propose_verification", payload=self._payload)
            return ModelResponse(
                schema_version="0.1",
                request_id=request.request_id,
                status="ok",
                content=json.dumps(body),
            )

    class _RecordingOfflineProvider(OfflineProjectProvider):
        def __init__(self) -> None:
            super().__init__()
            self.guidance_seen: list[dict[str, Any]] = []

        async def complete(self, request: ModelRequest) -> ModelResponse:
            self.guidance_seen.append(
                dict(request.model_context.action_guidance.get("propose_verification") or {})
            )
            return await super().complete(request)

    marker_val = "synth-val-marker-8b4d"
    marker_key = "synth-arbitrary-key-3e7f"
    marker_arb_val = "synth-arb-val-6a1c"

    repo = create_buggy_calc_project(tmp_path / "host" / "repo")
    cfg = make_project_host_config(tmp_path / "host", allowed_roots=(repo,))

    live_review_002_payload: dict[str, Any] = {
        "plan_id": "plan-neg-002",
        "summary": "negative control matching live-review-002 shape",
        "checks": [
            {
                "check_id": "unit_tests",
                "type": marker_val,
                "path": f"tests/{marker_val}.py",
                "test": f"test_{marker_val}",
                "command": f"pytest {marker_val}",
            }
        ],
    }
    arbitrary_key_payload: dict[str, Any] = {
        "plan_id": "plan-neg-arb",
        "checks": [
            {
                "check_id": "unit_arb",
                "kind": "exit_code",
                "executable": sys.executable,
                "timeout_ms": 5000,
                marker_key: marker_arb_val,
            }
        ],
    }

    pos_provider = _RecordingOfflineProvider()
    neg_provider = _TrialVerificationProvider(payload=live_review_002_payload)
    arb_provider = _TrialVerificationProvider(payload=arbitrary_key_payload)
    providers: list[Any] = [pos_provider, neg_provider, arb_provider]

    def _factory(**_: Any) -> Any:
        return providers.pop(0)

    async def _drive(
        svc: AppService, sid: str, rid: str, *, capture_verification_pending: bool = False
    ) -> tuple[Any, list[Any], list[Any]]:
        deadline = time.monotonic() + 120.0
        pending_verifications: list[Any] = []
        seen_prop_ids: set[str] = set()
        all_props: list[Any] = []
        while time.monotonic() < deadline:
            view = svc.get_run(session_id=sid, run_id=rid)
            props = svc.get_proposals(session_id=sid, run_id=rid)
            for p in props:
                if p.id not in seen_prop_ids:
                    seen_prop_ids.add(p.id)
                    all_props.append(p)
            if view.state in {"succeeded", "failed", "cancelled", "unknown"}:
                return view, pending_verifications, all_props
            for prop in props:
                if prop.state == "pending":
                    if capture_verification_pending and prop.kind == "verification":
                        pending_verifications.append(prop)
                    await svc.decide_proposal(
                        session_id=sid,
                        run_id=rid,
                        proposal_id=prop.id,
                        request=ProposalDecision(
                            digest=prop.digest,
                            decision="approve",
                            trusted_local_execution=(prop.kind == "command"),
                        ),
                    )
            await asyncio.sleep(0.05)
        raise AssertionError("timed out driving run")

    async def _host_scenario() -> dict[str, Any]:
        svc = AppService(config=cfg, provider_factory=_factory)
        try:
            proj = await svc.register_project(
                request=RegisterProjectRequest(name="trial-ver", root=str(repo), confirmed_read=True)
            )
            sid = svc.create_session(
                project_id=proj.project_id,
                model=ModelSelection(provider="offline", model="offline-project-v1"),
            ).session_id

            pos_run = await svc.start_run(
                session_id=sid,
                request=StartRunRequest(
                    client_request_id=_uuid.uuid4(),
                    goal="Fix calc.py",
                    confirmed_scope=True,
                ),
            )
            pos_view, pos_pending_ver, pos_all_props = await _drive(
                svc, sid, pos_run.run_id, capture_verification_pending=True
            )

            neg_run = await svc.start_run(
                session_id=sid,
                request=StartRunRequest(
                    client_request_id=_uuid.uuid4(),
                    goal="Trial invalid verification payload",
                    confirmed_scope=True,
                ),
            )
            neg_view, _, neg_all_props = await _drive(svc, sid, neg_run.run_id)
            neg_snap = svc.snapshot(session_id=sid, run_id=neg_run.run_id)

            arb_run = await svc.start_run(
                session_id=sid,
                request=StartRunRequest(
                    client_request_id=_uuid.uuid4(),
                    goal="Trial arbitrary verification key",
                    confirmed_scope=True,
                ),
            )
            arb_view, _, arb_all_props = await _drive(svc, sid, arb_run.run_id)

            return {
                "pos_view": pos_view,
                "pos_pending_ver": pos_pending_ver,
                "pos_all_props": pos_all_props,
                "neg_view": neg_view,
                "neg_all_props": neg_all_props,
                "neg_snap": neg_snap,
                "arb_view": arb_view,
                "arb_all_props": arb_all_props,
            }
        finally:
            await svc.aclose()

    data = asyncio.run(_host_scenario())

    # 1. Verify captured ModelRequest.model_context.action_guidance["propose_verification"]
    guidance = neg_provider.guidance_seen[0]
    assert guidance == pos_provider.guidance_seen[0]
    assert guidance.get("additionalProperties") is False
    assert guidance.get("required") == ["checks"]
    g_props = guidance.get("properties") or {}
    assert (g_props.get("summary") or {}).get("maxLength") == 1024
    checks_schema = g_props.get("checks") or {}
    assert checks_schema.get("type") == "array"
    assert checks_schema.get("minItems") == 1
    assert "unique" in (checks_schema.get("description") or "").lower()
    item_schema = checks_schema.get("items") or {}
    assert isinstance(item_schema, dict)
    assert item_schema.get("type") == "object"
    assert item_schema.get("additionalProperties") is False
    assert set(item_schema.get("required") or []) == {
        "check_id",
        "kind",
        "executable",
        "timeout_ms",
    }
    item_props = item_schema.get("properties") or {}
    assert set(item_props.keys()) == {
        "check_id",
        "kind",
        "executable",
        "argv",
        "cwd",
        "timeout_ms",
        "protected_paths",
        "expected_tests",
    }
    assert (item_props.get("kind") or {}).get("enum") == ["junit_exact", "exit_code"]
    assert (item_props.get("timeout_ms") or {}).get("minimum") == 1
    assert (item_props.get("timeout_ms") or {}).get("maximum") == PROJECT_MAX_COMMAND_TIMEOUT_MS
    argv_schema = item_props.get("argv") or {}
    assert argv_schema.get("maxItems") == PROJECT_MAX_COMMAND_ARGS
    assert argv_schema.get("x-unit") == "utf8_bytes"
    assert argv_schema.get("x-item-max-utf8-bytes") == PROJECT_MAX_COMMAND_ARG_BYTES
    assert argv_schema.get("x-total-max-utf8-bytes") == PROJECT_MAX_COMMAND_ARGV_TOTAL_BYTES
    assert argv_schema.get("x-no-nul") is True
    assert argv_schema.get("x-allowed-brace-placeholders") == ["{result_dir}"]
    assert argv_schema.get("x-result-dir-forbids-dotdot-after-slash") is True
    argv_item = argv_schema.get("items") or {}
    assert "maxLength" not in argv_item
    assert argv_item.get("x-unit") == "utf8_bytes"
    assert argv_item.get("x-max-utf8-bytes") == PROJECT_MAX_COMMAND_ARG_BYTES
    assert argv_item.get("x-no-nul") is True
    assert "utf-8 bytes" in (argv_schema.get("description") or "").lower()
    assert str(PROJECT_MAX_COMMAND_ARG_BYTES) in (argv_schema.get("description") or "")
    assert str(PROJECT_MAX_COMMAND_ARGV_TOTAL_BYTES) in (argv_schema.get("description") or "")
    assert "{result_dir}" in (argv_schema.get("description") or "")
    assert ".." in (argv_schema.get("description") or "")

    # Non-blank / NUL-free constraints apply only to plan_id (when non-null), executable, cwd, protected_paths, expected_tests.{classname,name}
    assert (g_props.get("plan_id") or {}).get("x-non-blank-when-non-null") is True
    assert (g_props.get("plan_id") or {}).get("x-no-nul") is True
    assert (item_props.get("executable") or {}).get("x-non-blank") is True
    assert (item_props.get("executable") or {}).get("x-no-nul") is True
    assert (item_props.get("cwd") or {}).get("x-non-blank") is True
    assert (item_props.get("cwd") or {}).get("x-no-nul") is True
    assert ((item_props.get("protected_paths") or {}).get("items") or {}).get("x-non-blank") is True
    assert ((item_props.get("protected_paths") or {}).get("items") or {}).get("x-no-nul") is True

    # summary and ordinary argv items must NOT carry non-blank restrictions
    assert "x-non-blank" not in (g_props.get("summary") or {})
    assert "minLength" not in (g_props.get("summary") or {})
    assert "x-non-blank" not in argv_item
    assert "minLength" not in argv_item

    et_schema = item_props.get("expected_tests") or {}
    et_item = et_schema.get("items") or {}
    assert et_item.get("additionalProperties") is False
    assert set(et_item.get("required") or []) == {"classname", "name"}
    et_props = et_item.get("properties") or {}
    assert set(et_props.keys()) == {"classname", "name"}
    assert (et_props.get("classname") or {}).get("x-non-blank") is True
    assert (et_props.get("classname") or {}).get("x-no-nul") is True
    assert (et_props.get("name") or {}).get("x-non-blank") is True
    assert (et_props.get("name") or {}).get("x-no-nul") is True

    # Conditional constraints expressed in both structured allOf (if/then) and visible descriptions
    all_of = item_schema.get("allOf") or []
    assert len(all_of) >= 2
    cond_dump = json.dumps(all_of)
    assert "junit_exact" in cond_dump and "exit_code" in cond_dump
    assert "expected_tests" in cond_dump and "result_dir" in cond_dump
    full_guidance_text = json.dumps(guidance)
    assert "{result_dir}" in full_guidance_text
    assert "unique" in full_guidance_text.lower()

    # 2. Positive control: junit_exact plan enters verification pending approval and succeeds
    pos_view = data["pos_view"]
    assert pos_view.state == "succeeded"
    assert pos_view.verification == "passed"
    assert len(data["pos_pending_ver"]) == 1
    pending_ver = data["pos_pending_ver"][0]
    assert pending_ver.kind == "verification"
    assert pending_ver.state == "pending"
    plan_checks = (pending_ver.payload.get("plan") or {}).get("checks") or []
    assert len(plan_checks) == 1
    assert plan_checks[0]["kind"] == "junit_exact"
    assert plan_checks[0]["expected_tests"] == [
        {"classname": "tests.test_calc", "name": "test_add_basic"}
    ]
    assert any("{result_dir}" in arg for arg in plan_checks[0]["argv"])

    # Native VerificationPlan.model_validate enforces conditional rules, UTF-8 byte limits, placeholders & non-blank rules
    VerificationPlan.model_validate(
        {
            "checks": [
                {
                    "check_id": "ok_junit",
                    "kind": "junit_exact",
                    "executable": sys.executable,
                    "argv": ["-B", "-m", "pytest", "--junitxml={result_dir}/u.xml"],
                    "timeout_ms": 5000,
                    "expected_tests": [{"classname": "c", "name": "t"}],
                }
            ]
        }
    )

    # Compare against frozen VerificationPlan.model_validate using one valid exit_code base check,
    # replacing only the field under test
    base_exit_check: dict[str, Any] = {
        "check_id": "exit_base",
        "kind": "exit_code",
        "executable": sys.executable,
        "argv": ["-B", "-c", "print('ok')"],
        "cwd": ".",
        "timeout_ms": 5000,
    }

    # Exact UTF-8 byte boundary checks for argv
    # 1. Single item "中" * 3000 = 3000 chars, 9000 UTF-8 bytes (> 8192) -> rejected
    with pytest.raises(ValidationError):
        VerificationPlan.model_validate({"checks": [{**base_exit_check, "argv": ["中" * 3000]}]})
    # 2. 5 items * 7000 ASCII bytes = 35000 UTF-8 bytes (> 32768) -> rejected
    with pytest.raises(ValidationError):
        VerificationPlan.model_validate({"checks": [{**base_exit_check, "argv": ["a" * 7000] * 5}]})
    # 3. 4 items * 8192 ASCII bytes = 32768 UTF-8 bytes (== 32768) -> accepted
    VerificationPlan.model_validate({"checks": [{**base_exit_check, "argv": ["a" * 8192] * 4}]})

    # Placeholder & NUL rules on argv
    VerificationPlan.model_validate({"checks": [{**base_exit_check, "argv": ["{result_dir}/ok"]}]})
    VerificationPlan.model_validate({"checks": [{**base_exit_check, "argv": ["", "   "]}]})
    for bad_argv in (
        ["{other}"],
        ["{result_dir}/../out"],
        ["{result_dir}\\..\\out"],
        ["ok\x00arg"],
    ):
        with pytest.raises(ValidationError):
            VerificationPlan.model_validate({"checks": [{**base_exit_check, "argv": bad_argv}]})

    # summary allows empty / whitespace without non-blank rejection
    VerificationPlan.model_validate({"summary": "", "checks": [dict(base_exit_check)]})
    VerificationPlan.model_validate({"summary": "   ", "checks": [dict(base_exit_check)]})

    # Non-blank & NUL-free checks on applicable fields
    VerificationPlan.model_validate({"plan_id": None, "checks": [dict(base_exit_check)]})
    for bad_plan in (
        {"plan_id": "   ", "checks": [dict(base_exit_check)]},
        {"plan_id": "p\x001", "checks": [dict(base_exit_check)]},
        {"checks": [{**base_exit_check, "executable": "   "}]},
        {"checks": [{**base_exit_check, "executable": "py\x00"}]},
        {"checks": [{**base_exit_check, "cwd": "   "}]},
        {"checks": [{**base_exit_check, "cwd": ".\x00"}]},
        {"checks": [{**base_exit_check, "protected_paths": ["   "]}]},
        {"checks": [{**base_exit_check, "protected_paths": ["a\x00b"]}]},
    ):
        with pytest.raises(ValidationError):
            VerificationPlan.model_validate(bad_plan)

    base_junit_check: dict[str, Any] = {
        "check_id": "junit_base",
        "kind": "junit_exact",
        "executable": sys.executable,
        "argv": ["-B", "-m", "pytest", "--junitxml={result_dir}/u.xml"],
        "cwd": ".",
        "timeout_ms": 5000,
        "expected_tests": [{"classname": "pkg.mod", "name": "test_ok"}],
    }
    for bad_et in (
        [{"classname": "   ", "name": "test_ok"}],
        [{"classname": "pkg\x00mod", "name": "test_ok"}],
        [{"classname": "pkg.mod", "name": "   "}],
        [{"classname": "pkg.mod", "name": "test\x00ok"}],
    ):
        with pytest.raises(ValidationError):
            VerificationPlan.model_validate({"checks": [{**base_junit_check, "expected_tests": bad_et}]})

    # 3. Negative control: live-review-002 shape rejected before approval with 7 distinct diagnostics
    neg_view = data["neg_view"]
    assert neg_view.state == "failed"
    assert neg_view.verification == "not_run"
    assert neg_view.reason_code == "EVIDENCE_INVALID"
    assert all(p.kind != "verification" for p in data["neg_all_props"])

    neg_reasons = [str(r) for r in (neg_view.result or {}).get("unverified_reasons", [])]
    ver_diags = [r for r in neg_reasons if r.startswith("propose_verification:")]
    assert len(ver_diags) == 7
    for missing_field in ("checks[0].kind", "checks[0].executable", "checks[0].timeout_ms"):
        assert any(missing_field in r and "missing" in r for r in ver_diags)
    for bad_field in ("checks[0].type", "checks[0].path", "checks[0].test", "checks[0].command"):
        assert any(bad_field in r and "not allowed" in r for r in ver_diags)

    neg_serialized = json.dumps(neg_view.result or {}) + json.dumps(
        [ev.model_dump(mode="json") for ev in data["neg_snap"].events]
    )
    assert marker_val not in neg_serialized
    assert "ValidationError" not in neg_serialized
    assert "Traceback" not in neg_serialized
    assert "extra_forbidden" not in neg_serialized

    # Arbitrary extra key maps only to safe placeholder path; never echoes key or value
    arb_view = data["arb_view"]
    assert arb_view.state == "failed"
    assert arb_view.verification == "not_run"
    assert arb_view.reason_code == "EVIDENCE_INVALID"
    assert all(p.kind != "verification" for p in data["arb_all_props"])
    arb_reasons = [str(r) for r in (arb_view.result or {}).get("unverified_reasons", [])]
    assert any("checks[0].unexpected_field" in r for r in arb_reasons)
    arb_serialized = json.dumps(arb_view.result or {})
    assert marker_key not in arb_serialized
    assert marker_arb_val not in arb_serialized
    assert "ValidationError" not in arb_serialized
    assert "Traceback" not in arb_serialized

    # Direct adapter.validate_action exception also stays sanitized
    ws_dir = create_buggy_calc_project(tmp_path / "direct" / "repo")
    (tmp_path / "direct" / "data").mkdir(parents=True)
    (tmp_path / "direct" / "out").mkdir(parents=True)
    ws = ProjectWorkspace(root=ws_dir, data_root=tmp_path / "direct" / "data")
    broker = PermissionBroker(
        run_id="run-direct-ver", project_root=ws_dir, root_identity=ws.root_identity
    )
    runner = ProjectCommandRunner(
        run_id="run-direct-ver",
        project_root=ws_dir,
        data_root=tmp_path / "direct" / "data",
        broker=broker,
    )
    task, _ = build_registered_project_task(
        project_id="proj-direct-ver",
        project_name="calc",
        project_root=ws_dir,
        root_identity=ws.root_identity,
        data_root=tmp_path / "direct" / "data",
        output_root=tmp_path / "direct" / "out",
        run_id="run-direct-ver",
        goal="Direct verification validation",
        python_executable=Path(sys.executable),
    )
    adapter = ProjectCodingAdapter(task=task, workspace=ws, broker=broker, command_runner=runner)
    with pytest.raises(InvalidActionPayloadError) as exc_info:
        adapter.validate_action(
            ActionSpec(
                schema_version=PROJECT_SCHEMA_VERSION,
                action_type="propose_verification",
                payload=arbitrary_key_payload,
            )
        )
    exc_text = str(exc_info.value)
    assert "checks[0].unexpected_field" in exc_text
    assert marker_key not in exc_text
    assert marker_arb_val not in exc_text
    assert "ValidationError" not in exc_text
    assert "Traceback" not in exc_text


def test_chat_fourth_planning_precommit_diagnostics(tmp_path: Path) -> None:
    """Verify sanitized pre-commit probe distinguishes positive, decode, adapter, and host-mutation rejections."""
    import hashlib

    import forecontract_app.project_runtime as pr
    from forecontract.runtime.engine import RuntimeError as CoreRuntimeError
    from forecontract_llm.errors import PlannerError

    from tools.chat_precommit_probe import PrecommitProbe, _safe_location

    marker_key = "secret_synth_key_x99"
    marker_val = "secret_synth_val_z88"

    class _PositiveProbeDone(Exception):
        """Fixture-local bounded stop once step 4 tool_result is projected."""

    def _run_branch(branch: str) -> dict[str, Any]:
        run_id = f"run-probe-{branch}"
        b_dir = tmp_path / branch
        repo = create_buggy_calc_project(b_dir / "repo")
        data_root = b_dir / "data"
        out_root = b_dir / "out"
        data_root.mkdir(parents=True)
        out_root.mkdir(parents=True)
        calc_before = (repo / "calc.py").read_bytes()
        test_before = (repo / "tests" / "test_calc.py").read_bytes()

        proposal_snapshots: list[tuple[str, str, str, str, dict[str, Any]]] = []
        ordered_timeline: list[tuple[str, dict[str, Any]]] = []

        def _on_prop(ev_name: str, prop: Any) -> None:
            proposal_snapshots.append(
                (ev_name, str(prop.id), str(prop.kind), str(prop.state), dict(prop.payload))
            )
            ordered_timeline.append(
                (
                    ev_name,
                    {
                        "proposal_id": str(prop.id),
                        "kind": str(prop.kind),
                        "state": str(prop.state),
                    },
                )
            )

        ws = ProjectWorkspace(root=repo, data_root=data_root)
        broker = PermissionBroker(
            run_id=run_id,
            project_root=repo,
            root_identity=ws.root_identity,
            on_proposal_changed=_on_prop,
        )
        runner = ProjectCommandRunner(
            run_id=run_id, project_root=repo, data_root=data_root, broker=broker
        )
        task, planner_cfg = build_registered_project_task(
            project_id=f"proj-probe-{branch}",
            project_name="calc",
            project_root=repo,
            root_identity=ws.root_identity,
            data_root=data_root,
            output_root=out_root,
            run_id=run_id,
            goal=f"Fourth planning probe {branch}",
            python_executable=Path(sys.executable),
        )
        trace_path = data_root / "trace.jsonl"
        trace = JsonlTraceStore(trace_path)
        fourth_req_ids: list[str] = []
        host_events: list[tuple[str, dict[str, Any]]] = []
        positive_stop_requested = [False]

        def _on_host_event(kind: str, payload: dict[str, Any]) -> None:
            host_events.append((kind, payload))
            ordered_timeline.append((kind, payload))

        class _FourStepProvider:
            def __init__(self) -> None:
                self._step = 0

            async def complete(self, request: ModelRequest) -> ModelResponse:
                self._step += 1
                if self._step == 1:
                    body = _forecast(action_type="read_file", payload={"path": "calc.py"})
                elif self._step == 2:
                    body = _forecast(
                        action_type="propose_scope",
                        payload={
                            "exact_paths": ["calc.py"],
                            "operations": ["modify"],
                            "protected_paths": ["tests/test_calc.py"],
                            "verification_plan_summary": "run calc",
                        },
                    )
                elif self._step == 3:
                    body = _forecast(action_type="read_file", payload={"path": "calc.py"})
                elif self._step == 4:
                    fourth_req_ids.append(str(request.request_id))
                    if branch == "decode_reject":
                        body = _forecast(action_type="read_file", payload={"path": "calc.py"})
                        body["expected_outcomes"][0]["outcome_type"] = "synthetic_unknown_outcome"
                    elif branch == "adapter_reject":
                        body = _forecast(
                            action_type="read_file",
                            payload={"path": "calc.py", marker_key: marker_val},
                        )
                    else:
                        body = _forecast(action_type="read_file", payload={"path": "calc.py"})
                else:
                    positive_stop_requested[0] = True
                    raise _PositiveProbeDone("step 4 tool_result already projected")
                return ModelResponse(
                    schema_version="0.1",
                    request_id=request.request_id,
                    status="ok",
                    content=json.dumps(body),
                )

        orig_host_plan = pr._HostProjectActionPlanner.plan
        host_plan_calls = [0]
        host_bound_actions: list[tuple[int, str | None]] = []

        async def _observe_host_plan(self_planner: Any, observation: Any) -> Any:
            forecast = await orig_host_plan(self_planner, observation)
            host_plan_calls[0] += 1
            bound_action_id = (forecast.proposed_action.metadata or {}).get("action_id")
            host_bound_actions.append(
                (host_plan_calls[0], str(bound_action_id) if bound_action_id is not None else None)
            )
            if branch == "host_fault" and host_plan_calls[0] == 4:
                mutated_payload = dict(forecast.proposed_action.payload)
                mutated_payload[marker_key] = marker_val
                mutated_action = forecast.proposed_action.model_copy(
                    update={"payload": mutated_payload}
                )
                return forecast.model_copy(update={"proposed_action": mutated_action})
            return forecast

        async def _exec() -> tuple[ProjectRunResult | None, BaseException | None, list[dict[str, Any]]]:
            stop_ev = asyncio.Event()
            decider = asyncio.create_task(
                _auto_decide_proposals(broker, decision="approve", stop_event=stop_ev)
            )
            result: ProjectRunResult | None = None
            raised: BaseException | None = None
            try:
                pr._HostProjectActionPlanner.plan = _observe_host_plan  # type: ignore[method-assign]
                with PrecommitProbe(run_id=run_id) as probe:
                    try:
                        result = await run_project_task(
                            task=task,
                            provider=_FourStepProvider(),
                            planner_config=planner_cfg,
                            permissions=broker,
                            trace_writer=trace,
                            emit=_on_host_event,
                            workspace=ws,
                            command_runner=runner,
                        )
                    except (PlannerError, ProjectError, CoreRuntimeError) as exc:
                        if branch == "positive":
                            if not (
                                positive_stop_requested[0]
                                and isinstance(exc, CoreRuntimeError)
                                and exc.code == "DEPENDENCY_FAILURE"
                                and exc.stage == "plan"
                            ):
                                raise
                            raised = exc
                        elif branch == "decode_reject":
                            raised = exc
                        else:
                            raise
                    return result, raised, probe.events
            finally:
                pr._HostProjectActionPlanner.plan = orig_host_plan  # type: ignore[method-assign]
                stop_ev.set()
                await decider

        result, raised, events = asyncio.run(_exec())
        trace_records = [
            json.loads(ln)
            for ln in trace_path.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        # Verify common first-3-step evidence on real host events, proposal snapshots, & Core JSONL kinds
        commits = [t for t in trace_records if t.get("kind") == "contract_committed"]
        attempts_started = [t for t in trace_records if t.get("kind") == "attempt_started"]
        attempts_finished = [t for t in trace_records if t.get("kind") == "attempt_finished"]
        assert len(commits) >= 3
        assert len(attempts_started) >= 3
        assert len(attempts_finished) >= 3
        assert all(t.get("run_id") == run_id for t in commits + attempts_started + attempts_finished)
        decided_scope = [
            s
            for s in proposal_snapshots
            if s[0] == "proposal_decided" and s[2] == "scope" and s[3] == "approved"
        ]
        assert len(decided_scope) == 1
        scope_prop_id = decided_scope[0][1]
        assert bool(scope_prop_id)
        assert decided_scope[0][4]["exact_paths"] == ["calc.py"]
        assert decided_scope[0][4]["operations"] == ["modify"]
        assert "tests/test_calc.py" in decided_scope[0][4]["protected_paths"]
        scope_tool_results = [
            (idx, p)
            for idx, (k, p) in enumerate(ordered_timeline)
            if k == "tool_result"
            and p.get("action_type") == "propose_scope"
            and p.get("tool_succeeded") is True
            and bool(p.get("action_id"))
        ]
        assert len(scope_tool_results) == 1
        assert "proposal_id" in scope_tool_results[0][1]
        assert scope_tool_results[0][1]["proposal_id"] == scope_prop_id
        read_timeline = [
            (idx, p)
            for idx, (k, p) in enumerate(ordered_timeline)
            if k == "tool_result"
            and p.get("action_type") == "read_file"
            and p.get("tool_succeeded") is True
            and p.get("path") == "calc.py"
            and bool(p.get("action_id"))
        ]
        assert len(read_timeline) >= 2
        assert read_timeline[0][0] < scope_tool_results[0][0] < read_timeline[1][0]
        read_results = [p for _, p in read_timeline]
        assert len(runner.list_commands()) == 0

        return {
            "run_id": run_id,
            "result": result,
            "raised": raised,
            "positive_stop_requested": positive_stop_requested[0],
            "events": events,
            "trace": trace_records,
            "host_events": host_events,
            "commits": commits,
            "attempts_started": attempts_started,
            "attempts_finished": attempts_finished,
            "read_results": read_results,
            "host_bound_actions": host_bound_actions,
            "ordered_timeline": ordered_timeline,
            "cmd_count": len(runner.list_commands()),
            "fourth_req_id": fourth_req_ids[0],
            "calc_ok": (repo / "calc.py").read_bytes() == calc_before,
            "test_ok": (repo / "tests" / "test_calc.py").read_bytes() == test_before,
        }

    failures: list[tuple[str, BaseException]] = []

    def _check_positive() -> None:
        pos = _run_branch("positive")
        assert pos["positive_stop_requested"] is True
        assert isinstance(pos["raised"], CoreRuntimeError)
        assert pos["raised"].code == "DEPENDENCY_FAILURE"
        assert pos["raised"].stage == "plan"
        pos_req4 = pos["fourth_req_id"]
        pos_ev4 = [e for e in pos["events"] if e.get("request_id") == pos_req4]
        for st in ("decode", "host_bind", "domain_prepare", "validate_action", "builder_build", "commit"):
            assert any(e["stage"] == st and e["status"] == "ok" and e["run_id"] == pos["run_id"] for e in pos_ev4)
        assert all(e["status"] != "error" for e in pos_ev4)
        assert len(pos["host_bound_actions"]) == 4
        expected_action_id_4 = (
            f"proj-{hashlib.sha256(pos['run_id'].encode('utf-8')).hexdigest()[:12]}-act-0004"
        )
        assert pos["host_bound_actions"][3] == (4, expected_action_id_4)
        assert len(pos["commits"]) == 4
        assert len(pos["attempts_started"]) == 4
        assert len(pos["attempts_finished"]) == 4
        c4 = pos["commits"][3]
        a4_s = pos["attempts_started"][3]
        a4_f = pos["attempts_finished"][3]
        assert c4["contract_id"] == a4_s["contract_id"] == a4_f["contract_id"]
        assert a4_s["attempt_id"] == a4_f["attempt_id"]
        assert a4_s["step_id"] == a4_f["step_id"]
        assert c4["sequence"] < a4_s["sequence"] < a4_f["sequence"]
        assert a4_f.get("execution_status") == "succeeded"
        assert len(pos["read_results"]) == 3
        read4 = pos["read_results"][2]
        assert read4["action_type"] == "read_file"
        assert read4["tool_succeeded"] is True
        assert read4["path"] == "calc.py"
        assert read4["action_id"] == expected_action_id_4
        req4_plan_idx = next(
            idx
            for idx, (k, p) in enumerate(pos["ordered_timeline"])
            if k == "planning_started" and p.get("request_id") == pos_req4
        )
        read4_idx = next(
            idx
            for idx, (k, p) in enumerate(pos["ordered_timeline"])
            if k == "tool_result" and p.get("action_id") == expected_action_id_4
        )
        assert req4_plan_idx < read4_idx
        assert pos["cmd_count"] == 0
        assert pos["calc_ok"] and pos["test_ok"]

    def _check_decode_reject() -> None:
        dec = _run_branch("decode_reject")
        dec_req4 = dec["fourth_req_id"]
        dec_ev4 = [e for e in dec["events"] if e.get("request_id") == dec_req4]
        dec_errs = [e for e in dec_ev4 if e["status"] == "error"]
        assert dec_errs and dec_errs[0]["stage"] == "decode"
        assert dec_errs[0]["code"] == "UNSUPPORTED_OUTCOME"
        assert dec_errs[0]["location"] == "expected_outcomes[0].outcome_type"
        assert not any(e["stage"] == "host_bind" and e["status"] == "ok" for e in dec_ev4)
        assert not any(e["stage"] in ("core_build", "builder_build") for e in dec_ev4)
        assert not any(e["stage"] == "commit" for e in dec_ev4)
        assert len(dec["commits"]) == 3
        assert len(dec["attempts_started"]) == 3
        assert len(dec["attempts_finished"]) == 3
        assert len(dec["read_results"]) == 2
        assert dec["cmd_count"] == 0
        assert dec["calc_ok"] and dec["test_ok"]

    def _check_adapter_reject() -> None:
        adp = _run_branch("adapter_reject")
        adp_req4 = adp["fourth_req_id"]
        adp_ev4 = [e for e in adp["events"] if e.get("request_id") == adp_req4]
        assert any(e["stage"] == "decode" and e["status"] == "ok" for e in adp_ev4)
        adp_bind = next(e for e in adp_ev4 if e["stage"] == "host_bind" and e["status"] == "ok")
        assert adp_bind["payload_changed"] is False
        assert adp_bind["action_changed"] is False
        adp_errs = [e for e in adp_ev4 if e["status"] == "error"]
        assert adp_errs and adp_errs[0]["stage"] == "validate_action"
        assert adp_errs[0]["code"] == "INVALID_DOMAIN_PAYLOAD"
        assert adp_errs[0]["location"] == "unexpected_field"
        adp_bld_err = next(e for e in adp_ev4 if e["stage"] == "builder_build" and e["status"] == "error")
        assert adp_bld_err["code"] == "INVALID_DOMAIN_PAYLOAD"
        assert adp["result"] is not None
        assert adp["result"].core_terminal_reason == "EVIDENCE_INVALID"
        assert not any(e["stage"] == "commit" for e in adp_ev4)
        assert len(adp["commits"]) == 3
        assert len(adp["attempts_started"]) == 3
        assert len(adp["attempts_finished"]) == 3
        assert len(adp["read_results"]) == 2
        assert adp["cmd_count"] == 0
        assert adp["calc_ok"] and adp["test_ok"]
        adp_dump = json.dumps(adp["events"], ensure_ascii=False) + json.dumps(adp["result"].model_dump(), default=str)
        assert marker_key not in adp_dump and marker_val not in adp_dump

    def _check_host_fault() -> None:
        hst = _run_branch("host_fault")
        hst_req4 = hst["fourth_req_id"]
        hst_ev4 = [e for e in hst["events"] if e.get("request_id") == hst_req4]
        assert any(e["stage"] == "decode" and e["status"] == "ok" for e in hst_ev4)
        hst_bind = next(e for e in hst_ev4 if e["stage"] == "host_bind" and e["status"] == "ok")
        assert hst_bind["action_changed"] is False
        assert hst_bind["payload_changed"] is True
        hst_errs = [e for e in hst_ev4 if e["status"] == "error"]
        assert hst_errs and hst_errs[0]["stage"] == "validate_action"
        assert hst_errs[0]["code"] == "INVALID_DOMAIN_PAYLOAD"
        assert hst_errs[0]["location"] == "unexpected_field"
        hst_bld_err = next(e for e in hst_ev4 if e["stage"] == "builder_build" and e["status"] == "error")
        assert hst_bld_err["code"] == "INVALID_DOMAIN_PAYLOAD"
        assert hst["result"] is not None
        assert hst["result"].core_terminal_reason == "EVIDENCE_INVALID"
        assert not any(e["stage"] == "commit" for e in hst_ev4)
        assert len(hst["commits"]) == 3
        assert len(hst["attempts_started"]) == 3
        assert len(hst["attempts_finished"]) == 3
        assert len(hst["read_results"]) == 2
        assert hst["cmd_count"] == 0
        assert hst["calc_ok"] and hst["test_ok"]
        hst_dump = json.dumps(hst["events"], ensure_ascii=False) + json.dumps(hst["result"].model_dump(), default=str)
        assert marker_key not in hst_dump and marker_val not in hst_dump

    def _check_sanitized_location() -> None:
        assert (
            _safe_location(
                None,
                code="INVALID_DOMAIN_PAYLOAD",
                exc=ValueError("unrelated prose mentioning unexpected_field"),
            )
            == "other"
        )
        assert _safe_location(("weird_field",), code="UNSUPPORTED_OUTCOME") == "other"

    for b_name, b_fn in (
        ("positive", _check_positive),
        ("decode_reject", _check_decode_reject),
        ("adapter_reject", _check_adapter_reject),
        ("host_fault", _check_host_fault),
        ("sanitized_location", _check_sanitized_location),
    ):
        try:
            b_fn()
        except (
            AssertionError,
            AttributeError,
            KeyError,
            StopIteration,
            ValueError,
            CoreRuntimeError,
            ProjectError,
            PlannerError,
        ) as exc:
            failures.append((b_name, exc))

    if failures:
        first_name, first_exc = failures[0]
        summary = [(name, f"{type(err).__name__}: {err}") for name, err in failures]
        raise AssertionError(f"first_failure={first_name}; all_failures={summary}") from first_exc


def test_chat_scope_plan_edit_order(tmp_path: Path) -> None:
    """Verify scope->plan->edit ordering guidance in real serialized requests plus negative and positive runs."""
    import hashlib

    import httpx
    from forecontract.contracts.builder import ContractBuilder
    from forecontract.contracts.errors import ContractBuildError
    from forecontract_app.deepseek_transport import DeepSeekProvider
    from forecontract_app.project_verification import (
        ProjectContractValidationHooks,
        ProjectDomainSupport,
    )

    neg_calc_before = (create_buggy_calc_project(tmp_path / "ref_neg" / "repo") / "calc.py").read_bytes()
    pos_calc_before_ref = (create_buggy_calc_project(tmp_path / "ref_pos" / "repo") / "calc.py").read_bytes()
    assert neg_calc_before == pos_calc_before_ref

    def _neg_scenario() -> None:
        neg_dir = tmp_path / "neg"
        neg_repo = create_buggy_calc_project(neg_dir / "repo")
        neg_data = neg_dir / "data"
        neg_out = neg_dir / "out"
        neg_data.mkdir(parents=True)
        neg_out.mkdir(parents=True)
        calc_before = (neg_repo / "calc.py").read_bytes()
        test_before = (neg_repo / "tests" / "test_calc.py").read_bytes()
        neg_run_id = "run-order-neg"
        neg_digest = hashlib.sha256(neg_run_id.encode("utf-8")).hexdigest()[:12]

        neg_proposals: list[tuple[str, str, str, str]] = []
        neg_ws = ProjectWorkspace(root=neg_repo, data_root=neg_data)
        neg_broker = PermissionBroker(
            run_id=neg_run_id,
            project_root=neg_repo,
            root_identity=neg_ws.root_identity,
            on_proposal_changed=lambda ev, p: neg_proposals.append(
                (ev, str(p.id), str(p.kind), str(p.state))
            ),
        )
        neg_runner = ProjectCommandRunner(
            run_id=neg_run_id, project_root=neg_repo, data_root=neg_data, broker=neg_broker
        )
        neg_task, neg_cfg = build_registered_project_task(
            project_id="proj-order-neg",
            project_name="calc",
            project_root=neg_repo,
            root_identity=neg_ws.root_identity,
            data_root=neg_data,
            output_root=neg_out,
            run_id=neg_run_id,
            goal="Fix calc.py after scope approval without plan approval",
            python_executable=Path(sys.executable),
        )
        neg_trace_path = neg_data / "trace.jsonl"
        neg_trace = JsonlTraceStore(neg_trace_path)
        neg_host_events: list[tuple[str, dict[str, Any]]] = []
        captured_wire: dict[str, Any] = {}

        class _NegWireProvider:
            def __init__(self) -> None:
                self._step = 0
                self._calc_sha = ""

            async def complete(self, request: ModelRequest) -> ModelResponse:
                self._step += 1
                last = request.observation.state.get("last_tool_result")
                if isinstance(last, dict) and last.get("action_type") == "read_file":
                    self._calc_sha = str(last.get("sha256") or last.get("file_sha256") or "")

                if self._step == 1:
                    body = _forecast(action_type="read_file", payload={"path": "calc.py"})
                elif self._step == 2:
                    body = _forecast(
                        action_type="propose_scope",
                        payload={
                            "exact_paths": ["calc.py"],
                            "operations": ["modify"],
                            "protected_paths": ["tests/test_calc.py"],
                            "verification_plan_summary": "run calc tests",
                        },
                    )
                elif self._step == 3:
                    body = _forecast(
                        action_type="edit_file",
                        payload={
                            "path": "calc.py",
                            "expected_sha256": self._calc_sha,
                            "old_text": "return a - b",
                            "new_text": "return a + b",
                        },
                    )
                else:
                    raise AssertionError(f"unexpected step {self._step} in negative run")

                envelope = {
                    "id": f"cmpl-neg-{self._step}",
                    "model": "deepseek-flash",
                    "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(body)}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 10},
                }
                transport = httpx.MockTransport(
                    lambda _req: httpx.Response(200, json=envelope)
                )
                async with httpx.AsyncClient(transport=transport) as client:
                    ds = DeepSeekProvider(client=client, model="deepseek-flash")
                    resp = await ds.complete(request)
                    if self._step == 3:
                        assert ds.last_wire is not None
                        captured_wire["wire"] = dict(ds.last_wire)
                        captured_wire["request_id"] = str(request.request_id)
                    return resp

        obs_validate: list[tuple[str, str, str]] = []
        obs_preconditions: list[tuple[str, str, list[tuple[int, str, bool]]]] = []
        obs_checks: list[tuple[str, str, str]] = []
        obs_build_errors: list[tuple[str, str, tuple[Any, ...]]] = []
        active_build_action_id = [""]

        orig_validate = ProjectCodingAdapter.validate_action
        orig_prepare = ProjectDomainSupport.prepare_contract
        orig_check = ProjectContractValidationHooks.check_precondition
        orig_build = ContractBuilder.build

        def _wrap_validate(self_adp: Any, action: Any) -> Any:
            act_id = str((action.metadata or {}).get("action_id") or "")
            if self_adp._task.run_id == neg_run_id:
                try:
                    res = orig_validate(self_adp, action)
                    obs_validate.append((str(action.action_type), act_id, "ok"))
                    return res
                except Exception as exc:
                    obs_validate.append((str(action.action_type), act_id, f"{type(exc).__name__}:{exc}"))
                    raise
            return orig_validate(self_adp, action)

        def _wrap_prepare(self_supp: Any, *, forecast: Any, evidence: Any) -> Any:
            prep = orig_prepare(self_supp, forecast=forecast, evidence=evidence)
            if self_supp._task is not None and self_supp._task.run_id == neg_run_id:
                act = forecast.proposed_action
                act_id = str((act.metadata or {}).get("action_id") or "")
                if str(act.action_type) == "edit_file":
                    obs_preconditions.append(
                        (
                            str(act.action_type),
                            act_id,
                            [(i, str(c.condition_type), bool(c.required)) for i, c in enumerate(prep.preconditions)],
                        )
                    )
            return prep

        def _wrap_check(self_hooks: Any, condition: Any, observation: Any) -> Any:
            verdict = orig_check(self_hooks, condition, observation)
            if observation.state.get("root_identity") == neg_ws.root_identity:
                obs_checks.append((active_build_action_id[0], str(condition.condition_type), str(verdict.value)))
            return verdict

        def _wrap_build(self_builder: Any, *args: Any, **kwargs: Any) -> Any:
            fc = kwargs.get("forecast")
            act_id = str((fc.proposed_action.metadata or {}).get("action_id") or "") if fc is not None else ""
            is_neg = act_id.startswith(f"proj-{neg_digest}-act-")
            prev = active_build_action_id[0]
            if is_neg:
                active_build_action_id[0] = act_id
            try:
                return orig_build(self_builder, *args, **kwargs)
            except ContractBuildError as exc:
                if is_neg:
                    obs_build_errors.append((act_id, str(exc.code), tuple(exc.location)))
                raise
            finally:
                active_build_action_id[0] = prev

        async def _run_neg() -> ProjectRunResult:
            stop_ev = asyncio.Event()
            decider = asyncio.create_task(
                _auto_decide_proposals(neg_broker, decision="approve", stop_event=stop_ev)
            )
            try:
                ProjectCodingAdapter.validate_action = _wrap_validate  # type: ignore[method-assign]
                ProjectDomainSupport.prepare_contract = _wrap_prepare  # type: ignore[method-assign]
                ProjectContractValidationHooks.check_precondition = _wrap_check  # type: ignore[method-assign]
                ContractBuilder.build = _wrap_build  # type: ignore[method-assign]
                return await asyncio.wait_for(
                    run_project_task(
                        task=neg_task,
                        provider=_NegWireProvider(),
                        planner_config=neg_cfg,
                        permissions=neg_broker,
                        trace_writer=neg_trace,
                        emit=lambda k, p: neg_host_events.append((k, p)),
                        workspace=neg_ws,
                        command_runner=neg_runner,
                    ),
                    timeout=30.0,
                )
            finally:
                ProjectCodingAdapter.validate_action = orig_validate  # type: ignore[method-assign]
                ProjectDomainSupport.prepare_contract = orig_prepare  # type: ignore[method-assign]
                ProjectContractValidationHooks.check_precondition = orig_check  # type: ignore[method-assign]
                ContractBuilder.build = orig_build  # type: ignore[method-assign]
                stop_ev.set()
                await decider

        neg_result = asyncio.run(_run_neg())
        assert neg_result.terminal_status != "succeeded"

        # Verify serialized ModelRequest wire when scope_approved=True, plan_approved=False
        wire_req = json.loads(captured_wire["wire"]["messages"][1]["content"])
        assert wire_req["model_context"]["run_id"] == neg_run_id
        assert wire_req["request_id"] == captured_wire["request_id"]
        assert wire_req["observation"]["state"]["scope_approved"] is True
        assert wire_req["observation"]["state"]["plan_approved"] is False
        ag = wire_req["model_context"]["action_guidance"]
        scope_desc = ag["propose_scope"]["description"]
        verif_desc = ag["propose_verification"]["description"]
        edit_desc = ag["edit_file"]["description"]
        assert "does not grant plan_approved" in scope_desc
        assert "after scope_approved=true and plan_approved=false, call propose_verification and wait for actual approval before edit_file" in scope_desc
        assert "After scope_approved=true while plan_approved=false, call propose_verification (read-only exploration remains allowed) and wait for actual approval (plan_approved=true) before edit_file" in verif_desc
        assert "submitting a proposal or verification_plan_summary is not approval" in verif_desc
        assert "Requires both scope_approved=true and plan_approved=true before editing" in edit_desc
        assert "do not call edit_file yet" in edit_desc
        assert "verification_plan_summary or a pending proposal is not approval; read-only exploration remains allowed" in edit_desc

        # Verify negative run rejection: validate_action passed, scope/write_grant PASS, project_plan_approved FAIL
        expected_edit_act_id = f"proj-{neg_digest}-act-0003"
        assert ("edit_file", expected_edit_act_id, "ok") in obs_validate
        assert len(obs_preconditions) == 1
        assert obs_preconditions[0][0] == "edit_file"
        assert obs_preconditions[0][1] == expected_edit_act_id
        assert len(obs_build_errors) == 1
        err_act_id, err_code, err_loc = obs_build_errors[0]
        assert err_act_id == expected_edit_act_id
        assert err_code == "PRECONDITION_UNSATISFIED"
        assert len(err_loc) == 2 and err_loc[0] == "preconditions"
        pre_idx = int(err_loc[1])
        cond_idx, cond_type, cond_req = obs_preconditions[0][2][pre_idx]
        assert cond_idx == pre_idx
        assert cond_type == "project_plan_approved"
        assert cond_req is True
        assert (expected_edit_act_id, "project_scope_valid", "pass") in obs_checks
        assert (expected_edit_act_id, "project_write_grant_valid", "pass") in obs_checks
        assert (expected_edit_act_id, "project_plan_approved", "fail") in obs_checks

        neg_trace_records = [
            json.loads(ln)
            for ln in neg_trace_path.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        assert len([t for t in neg_trace_records if t.get("kind") == "contract_committed"]) == 2
        assert len([t for t in neg_trace_records if t.get("kind") == "attempt_started"]) == 2
        assert len([t for t in neg_trace_records if t.get("kind") == "attempt_finished"]) == 2
        assert not any(
            k == "tool_result" and p.get("action_type") == "edit_file"
            for k, p in neg_host_events
        )
        assert not any(p[2] == "verification" for p in neg_proposals)
        assert (neg_repo / "calc.py").read_bytes() == calc_before
        assert (neg_repo / "tests" / "test_calc.py").read_bytes() == test_before
        assert len(neg_runner.list_commands()) == 0

    def _pos_scenario() -> None:
        pos_dir = tmp_path / "pos"
        pos_repo = create_buggy_calc_project(pos_dir / "repo")
        pos_data = pos_dir / "data"
        pos_out = pos_dir / "out"
        pos_data.mkdir(parents=True)
        pos_out.mkdir(parents=True)
        pos_calc_before = (pos_repo / "calc.py").read_bytes()
        pos_test_before = (pos_repo / "tests" / "test_calc.py").read_bytes()
        pos_run_id = "run-order-pos"
        pos_digest = hashlib.sha256(pos_run_id.encode("utf-8")).hexdigest()[:12]

        pos_host_events: list[tuple[str, dict[str, Any]]] = []
        pos_proposals: list[tuple[str, str, str, str, str, bool, dict[str, Any]]] = []
        pending_verif_checks: list[tuple[str, bool, bool]] = []

        def _on_pos_prop(ev_name: str, prop: Any) -> None:
            pos_proposals.append(
                (
                    ev_name,
                    str(prop.id),
                    str(prop.kind),
                    str(prop.state),
                    str(prop.digest),
                    bool(prop.trusted_local_execution),
                    dict(prop.payload),
                )
            )
            if str(prop.kind) == "verification" and ev_name == "proposal_requested":
                pending_verif_checks.append(
                    (
                        str(prop.id),
                        (pos_repo / "calc.py").read_bytes() == pos_calc_before,
                        not any(
                            k == "tool_result" and p.get("action_type") == "edit_file"
                            for k, p in pos_host_events
                        ),
                    )
                )

        pos_ws = ProjectWorkspace(root=pos_repo, data_root=pos_data)
        pos_broker = PermissionBroker(
            run_id=pos_run_id,
            project_root=pos_repo,
            root_identity=pos_ws.root_identity,
            on_proposal_changed=_on_pos_prop,
        )
        pos_runner = ProjectCommandRunner(
            run_id=pos_run_id, project_root=pos_repo, data_root=pos_data, broker=pos_broker
        )
        pos_task, pos_cfg = build_registered_project_task(
            project_id="proj-order-pos",
            project_name="calc",
            project_root=pos_repo,
            root_identity=pos_ws.root_identity,
            data_root=pos_data,
            output_root=pos_out,
            run_id=pos_run_id,
            goal="Fix calc.py after scope and verification plan approval",
            python_executable=Path(sys.executable),
        )
        pos_trace_path = pos_data / "trace.jsonl"
        pos_trace = JsonlTraceStore(pos_trace_path)

        class _ScopedPosProvider(OfflineProjectProvider):
            def __init__(self) -> None:
                super().__init__()
                self.states_seen: list[dict[str, Any]] = []

            def _analyze_repo(self, python_exe: str) -> None:
                super()._analyze_repo(python_exe)
                self._plan_checks = [
                    {
                        "check_id": "junit_add_basic",
                        "kind": "junit_exact",
                        "executable": python_exe,
                        "argv": [
                            "-B",
                            "-m",
                            "pytest",
                            "-p",
                            "no:cacheprovider",
                            "--junitxml={result_dir}/junit.xml",
                            "tests/test_calc.py::test_add_basic",
                        ],
                        "cwd": ".",
                        "timeout_ms": 30000,
                        "protected_paths": ["tests/test_calc.py"],
                        "expected_tests": [
                            {"classname": "tests.test_calc", "name": "test_add_basic"}
                        ],
                    }
                ]

            async def complete(self, request: ModelRequest) -> ModelResponse:
                self.states_seen.append(dict(request.observation.state))
                return await super().complete(request)

        pos_provider = _ScopedPosProvider()
        pos_built_contracts: list[tuple[str, str, str]] = []
        orig_pos_build = ContractBuilder.build

        def _wrap_pos_build(self_builder: Any, *args: Any, **kwargs: Any) -> Any:
            contract = orig_pos_build(self_builder, *args, **kwargs)
            act_id = str((contract.action.metadata or {}).get("action_id") or "")
            if act_id.startswith(f"proj-{pos_digest}-act-"):
                pos_built_contracts.append(
                    (str(contract.action.action_type), act_id, str(contract.contract_id))
                )
            return contract

        async def _run_pos() -> ProjectRunResult:
            stop_ev = asyncio.Event()
            decider = asyncio.create_task(
                _auto_decide_proposals(pos_broker, decision="approve", stop_event=stop_ev)
            )
            try:
                ContractBuilder.build = _wrap_pos_build  # type: ignore[method-assign]
                return await asyncio.wait_for(
                    run_project_task(
                        task=pos_task,
                        provider=pos_provider,
                        planner_config=pos_cfg,
                        permissions=pos_broker,
                        trace_writer=pos_trace,
                        emit=lambda k, p: pos_host_events.append((k, p)),
                        workspace=pos_ws,
                        command_runner=pos_runner,
                    ),
                    timeout=60.0,
                )
            finally:
                ContractBuilder.build = orig_pos_build  # type: ignore[method-assign]
                stop_ev.set()
                await decider

        pos_result = asyncio.run(_run_pos())
        assert pos_result.terminal_status == "succeeded"
        assert pos_result.verification == "passed"

        # While verification proposal was pending, calc.py was unchanged and no edit_file executed
        assert len(pending_verif_checks) == 1
        req_prop_id, calc_ok_at_req, no_edit_at_req = pending_verif_checks[0]
        assert calc_ok_at_req is True and no_edit_at_req is True

        approved_verif = [
            p
            for p in pos_proposals
            if p[0] == "proposal_decided" and p[2] == "verification" and p[3] == "approved"
        ]
        assert len(approved_verif) == 1
        assert approved_verif[0][1] == req_prop_id
        verif_digest = approved_verif[0][4]
        verif_plan = approved_verif[0][6]["plan"]
        verif_plan_id = verif_plan["plan_id"]
        assert bool(verif_plan_id) and bool(verif_digest)
        assert any(
            s.get("plan_approved") is True
            and s.get("plan_id") == verif_plan_id
            and s.get("plan_digest") == verif_digest
            for s in pos_provider.states_seen
        )

        edit_results = [
            p
            for k, p in pos_host_events
            if k == "tool_result" and p.get("action_type") == "edit_file"
        ]
        assert len(edit_results) == 1
        assert edit_results[0].get("tool_succeeded") is True
        edit_act_id = str(edit_results[0].get("action_id") or "")
        assert bool(edit_act_id)

        edit_contracts = [
            c for c in pos_built_contracts if c[0] == "edit_file" and c[1] == edit_act_id
        ]
        assert len(edit_contracts) == 1
        edit_contract_id = edit_contracts[0][2]

        pos_trace_records = [
            json.loads(ln)
            for ln in pos_trace_path.read_text(encoding="utf-8").splitlines()
            if ln.strip()
        ]
        edit_commit = next(
            t
            for t in pos_trace_records
            if t.get("kind") == "contract_committed"
            and t.get("contract_id") == edit_contract_id
        )
        edit_attempt_start = next(
            t
            for t in pos_trace_records
            if t.get("kind") == "attempt_started"
            and t.get("contract_id") == edit_contract_id
        )
        edit_attempt_finish = next(
            t
            for t in pos_trace_records
            if t.get("kind") == "attempt_finished"
            and t.get("contract_id") == edit_contract_id
            and t.get("attempt_id") == edit_attempt_start["attempt_id"]
            and t.get("step_id") == edit_attempt_start["step_id"]
        )
        assert edit_commit["sequence"] < edit_attempt_start["sequence"] < edit_attempt_finish["sequence"]
        assert edit_attempt_finish.get("execution_status") == "succeeded"

        pos_calc_after = (pos_repo / "calc.py").read_bytes()
        assert pos_calc_after == pos_calc_before.replace(b"return a - b", b"return a + b")
        assert (pos_repo / "tests" / "test_calc.py").read_bytes() == pos_test_before

        approved_cmds = [
            p
            for p in pos_proposals
            if p[0] == "proposal_decided" and p[2] == "command" and p[3] == "approved"
        ]
        assert len(approved_cmds) == 1
        cmd_prop_id = approved_cmds[0][1]
        cmd_digest = approved_cmds[0][4]
        cmd_trusted = approved_cmds[0][5]
        cmd_payload = approved_cmds[0][6]["command"]
        cmd_argv = list(cmd_payload["argv"])
        cmd_check_id = cmd_payload["check_id"]
        assert bool(cmd_prop_id) and bool(cmd_digest)
        assert cmd_trusted is True
        assert cmd_check_id == "junit_add_basic"

        plan_checks = [c for c in verif_plan["checks"] if c.get("check_id") == cmd_check_id]
        assert len(plan_checks) == 1
        plan_check = plan_checks[0]

        expected_argv = [
            "-B",
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "--junitxml={result_dir}/junit.xml",
            "tests/test_calc.py::test_add_basic",
        ]
        assert list(pos_result.checks.keys()) == [cmd_check_id]
        check_rec = pos_result.checks[cmd_check_id]
        assert list(plan_check["argv"]) == expected_argv
        assert cmd_argv == expected_argv
        assert list(check_rec.argv) == expected_argv

        assert check_rec.check_id == cmd_check_id
        assert check_rec.proposal_id == cmd_prop_id
        assert check_rec.proposal_digest == cmd_digest
        assert bool(check_rec.command_id)
        assert check_rec.exit_code == 0
        assert check_rec.passed is True
        assert check_rec.junit_valid is True

        cmds = pos_runner.list_commands()
        assert len(cmds) == 1
        assert cmds[0].run_id == pos_run_id
        assert cmds[0].command_id == check_rec.command_id
        assert cmds[0].proposal_digest == cmd_digest
        assert cmds[0].exit_code == 0

        res_dir = pos_runner.get_result_dir(cmds[0].command_id)
        assert res_dir is not None
        junit_path = res_dir / "junit.xml"
        assert junit_path.is_file()
        assert Path(check_rec.result_dir).resolve() == res_dir.resolve()
        assert check_rec.junit_xml_path is not None
        assert Path(check_rec.junit_xml_path).resolve() == junit_path.resolve()

        import xml.etree.ElementTree as ET

        junit_root = ET.fromstring(junit_path.read_text(encoding="utf-8"))
        testcases = junit_root.findall(".//testcase")
        assert len(testcases) == 1
        tc = testcases[0]
        assert tc.attrib.get("classname") == "tests.test_calc"
        assert tc.attrib.get("name") == "test_add_basic"
        assert tc.find("failure") is None
        assert tc.find("error") is None
        assert tc.find("skipped") is None
        assert len(check_rec.observed_tests) == 1
        obs_tc = check_rec.observed_tests[0]
        assert obs_tc.classname == "tests.test_calc"
        assert obs_tc.name == "test_add_basic"

    from forecontract.runtime.engine import RuntimeError as CoreRuntimeError
    from forecontract_llm.errors import PlannerError

    failures: list[tuple[str, BaseException]] = []
    for s_name, s_fn in (("negative", _neg_scenario), ("positive", _pos_scenario)):
        try:
            s_fn()
        except (
            AssertionError,
            AttributeError,
            KeyError,
            StopIteration,
            ValueError,
            TypeError,
            TimeoutError,
            OSError,
            CoreRuntimeError,
            ProjectError,
            PlannerError,
            ContractBuildError,
        ) as exc:
            failures.append((s_name, exc))

    if failures:
        first_name, first_exc = failures[0]
        summary = [(name, f"{type(err).__name__}: {err}") for name, err in failures]
        raise AssertionError(f"first_failure={first_name}; all_failures={summary}") from first_exc
