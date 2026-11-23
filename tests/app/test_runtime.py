"""A05 & A16–A20: Native Core/Coding execution, truth mapping, failures, child cancel, timeout."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
from app_helpers import (
    CREDENTIAL_ENV_CANARY,
    CountingMetricsProvider,
    async_wait_until,
    is_pid_alive,
    make_clean_subprocess_env,
    make_factory_spy,
    make_host_config,
    record_shared_child_env_observation,
    tree_hashes,
    verify_production_child_env_equivalence,
    wait_until,
)
from forecontract.runtime.errors import RuntimeError as CoreRuntimeError
from forecontract.runtime.models import RunResult, RuntimeTerminalStatus
from forecontract.tracing import JsonlTraceStore
from forecontract.tracing.errors import TraceStorageError
from forecontract_app import runtime as app_runtime
from forecontract_app.demo import INITIAL_METRICS_SHA256, METRICS_ACCEPTANCE_SHA256
from forecontract_app.models import AppError, ModelSelection, StartRunRequest
from forecontract_app.runtime import evaluate_run_outcome, execute_run
from forecontract_app.service import AppService
from forecontract_coding import (
    AcceptanceSummary,
    CodingConfig,
    CodingRunResult,
    CodingTask,
    CommandSpec,
    WriteTarget,
)
from forecontract_coding import agent as coding_agent
from forecontract_coding.process import build_minimal_windows_env
from forecontract_llm import ModelRequest, ModelResponse, PlannerConfig


def test_a05_real_positive_and_user_scope(tmp_path: Path) -> None:
    cfg = make_host_config(tmp_path)
    providers: list[CountingMetricsProvider] = []

    async def _scenario() -> None:
        svc = AppService(config=cfg, provider_factory=make_factory_spy(providers))
        try:
            proj = await svc.create_project(name="positive-metrics")
            metrics_file = cfg.workspace_parent / "positive-metrics" / "metrics.py"
            acc_file = cfg.data_root / "projects" / proj.project_id / "acceptance" / "test_metrics_acceptance.py"
            assert metrics_file.read_bytes()
            acc_sha_before = hashlib.sha256(acc_file.read_bytes()).hexdigest()
            assert acc_sha_before == METRICS_ACCEPTANCE_SHA256

            sess = svc.create_session(
                project_id=proj.project_id,
                model=ModelSelection(provider="offline", model="offline-metrics-v1"),
            )
            with pytest.raises(AppError) as e_scope:
                await svc.start_run(
                    session_id=sess.session_id,
                    request=StartRunRequest(
                        client_request_id=uuid.uuid4(),
                        goal="实现 weighted_positive_total",
                        confirmed_scope=False,
                    ),
                )
            assert e_scope.value.code == "SCOPE_NOT_CONFIRMED"
            assert e_scope.value.status_code == 403
            assert len(providers) == 0
            assert hashlib.sha256(metrics_file.read_bytes()).hexdigest() == INITIAL_METRICS_SHA256

            run = await svc.start_run(
                session_id=sess.session_id,
                request=StartRunRequest(
                    client_request_id=uuid.uuid4(),
                    goal="实现 weighted_positive_total",
                    confirmed_scope=True,
                ),
            )
            terminal = await async_wait_until(
                lambda: (
                    r := svc.get_run(session_id=sess.session_id, run_id=run.run_id)
                ).state
                in ("succeeded", "failed", "cancelled", "unknown")
                and r
            )
            assert terminal.state == "succeeded"
            assert terminal.verification == "passed"
            assert terminal.result is not None
            assert terminal.result["acceptance"]["revision"] == terminal.result["final_workspace_revision"]

            junit_files = list((cfg.data_root / "runs" / run.run_id / "output").rglob("junit.xml"))
            assert len(junit_files) == 1
            tree = ET.fromstring(junit_files[0].read_bytes())
            suites = [tree] if tree.tag == "testsuite" else list(tree.findall(".//testsuite"))
            cases: set[tuple[str, str]] = set()
            failures = errors = skips = 0
            for suite in suites:
                for tc in suite.findall("testcase"):
                    cases.add((tc.attrib.get("classname", ""), tc.attrib.get("name", "")))
                    if tc.find("failure") is not None:
                        failures += 1
                    if tc.find("error") is not None:
                        errors += 1
                    if tc.find("skipped") is not None:
                        skips += 1
            assert cases == {
                ("test_metrics_acceptance", "test_weighted_basic"),
                ("test_metrics_acceptance", "test_weighted_threshold_and_empty"),
            }
            assert failures == errors == skips == 0
            assert hashlib.sha256(metrics_file.read_bytes()).hexdigest() != INITIAL_METRICS_SHA256
            assert hashlib.sha256(acc_file.read_bytes()).hexdigest() == acc_sha_before
        finally:
            await svc.aclose()

    asyncio.run(_scenario())


def test_a16_native_entry_and_feedback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    native_entry_calls = 0
    outer_request_dumps: list[dict[str, Any]] = []
    outer_response_dumps: list[dict[str, Any]] = []
    orig_run_coding_task = coding_agent.run_coding_task

    class _OuterSpyProvider:
        def __init__(self, target: Any) -> None:
            self._target = target

        async def complete(self, request: ModelRequest) -> ModelResponse:
            outer_request_dumps.append(request.model_dump(mode="json"))
            resp = await self._target.complete(request)
            outer_response_dumps.append(resp.model_dump(mode="json"))
            return resp

    async def _spy_native(**kwargs: Any) -> CodingRunResult:
        nonlocal native_entry_calls
        native_entry_calls += 1
        kwargs["provider"] = _OuterSpyProvider(kwargs["provider"])
        return await orig_run_coding_task(**kwargs)

    monkeypatch.setattr(app_runtime, "run_coding_task", _spy_native)

    inner = CountingMetricsProvider()
    cfg = make_host_config(tmp_path / "native")
    from forecontract_app.demo import (
        build_controlled_coding_task,
        initialize_controlled_project,
    )

    spec = initialize_controlled_project(
        project_id=str(uuid.uuid4()),
        name="a16-native",
        data_root=cfg.data_root,
        workspace_parent=cfg.workspace_parent,
    )
    out_dir = cfg.data_root / "runs" / "run-a16" / "output"
    out_dir.mkdir(parents=True)
    trace_path = cfg.data_root / "runs" / "run-a16" / "trace.jsonl"
    task, coding_cfg, planner_cfg = build_controlled_coding_task(
        project=spec,
        run_id="run-a16",
        goal="check passthrough",
        output_root=out_dir,
        python_executable=cfg.python_executable,
    )
    emitted: list[tuple[str, dict[str, Any]]] = []

    result = asyncio.run(
        execute_run(
            task=task,
            config=coding_cfg,
            provider=inner,
            planner_config=planner_cfg,
            trace_path=trace_path,
            emit=lambda kind, payload: emitted.append((kind, payload)),
        )
    )
    assert native_entry_calls == 1
    assert result.terminal_status == "completed"
    # Verify outer ObservationEmittingProvider forwarded identical requests and returned identical responses
    assert outer_request_dumps == inner.request_dumps
    assert outer_response_dumps == inner.response_dumps
    assert len(inner.request_dumps) == 3
    write_req = inner.request_dumps[1]
    observed_sha = {e["path"]: e["sha256"] for e in write_req["observation"]["state"]["files"]}["metrics.py"]
    write_resp_body = json.loads(inner.response_dumps[1]["content"])
    assert write_resp_body["proposed_action"]["payload"]["expected_sha256"] == observed_sha
    assert result.acceptance.revision == result.final_workspace_revision
    assert any(k == "tool_result" for k, _ in emitted)

    monkeypatch.undo()

    async def _verify_service_final_event() -> None:
        svc = AppService(config=make_host_config(tmp_path / "svc_a16"))
        try:
            proj = await svc.create_project(name="a16-svc-pos")
            sess = svc.create_session(
                project_id=proj.project_id,
                model=ModelSelection(provider="offline", model="offline-metrics-v1"),
            )
            run = await svc.start_run(
                session_id=sess.session_id,
                request=StartRunRequest(
                    client_request_id=uuid.uuid4(),
                    goal="verify final event revision",
                    confirmed_scope=True,
                ),
            )
            term = await async_wait_until(
                lambda: (cur := svc.get_run(session_id=sess.session_id, run_id=run.run_id)).state
                in ("succeeded", "failed", "cancelled", "unknown")
                and cur,
                timeout=30.0,
            )
            snap = svc.snapshot(session_id=sess.session_id, run_id=run.run_id)
            final_event = [e for e in snap.events if e.kind == "run_finished"]
            assert len(final_event) == 1
            assert term.result is not None
            assert final_event[0].payload["acceptance"]["revision"] == term.result["final_workspace_revision"]
            assert final_event[0].payload["acceptance"]["status"] == term.result["acceptance"]["status"] == "passed"
        finally:
            await svc.aclose()

    asyncio.run(_verify_service_final_event())


def test_a17_false_success_and_stale_revision(tmp_path: Path) -> None:
    cfg = make_host_config(tmp_path)
    from forecontract_app.demo import initialize_controlled_project

    spec = initialize_controlled_project(
        project_id=str(uuid.uuid4()),
        name="a17-truth",
        data_root=cfg.data_root,
        workspace_parent=cfg.workspace_parent,
    )
    _ = spec

    base_rr = RunResult(
        schema_version="0.1",
        run_id="r-a17",
        goal_id="g-a17",
        terminal_status=RuntimeTerminalStatus.COMPLETED,
        reason_code="TASK_ACHIEVED",
        terminal_record_id="rec-end",
        verification_record_id="rec-ver",
        steps_started=1,
        attempts_started=1,
        planning_calls=1,
        retries_used=0,
        replans_used=0,
    )
    digest = "a" * 64
    rev_final = "b" * 64
    rev_stale = "c" * 64

    completed_and_passed = CodingRunResult(
        schema_version="0.1",
        run_result=base_rr,
        baseline_workspace_revision=rev_stale,
        final_workspace_revision=rev_final,
        changed_paths=[],
        acceptance=AcceptanceSummary(
            status="passed", revision=rev_final, command_ids=["pytest_acc"], commands_digest=digest
        ),
        terminal_status="completed",
        reason_code="TASK_ACHIEVED",
    )
    pos_state, pos_ver, _ = evaluate_run_outcome(completed_and_passed)
    assert (pos_state, pos_ver) == ("succeeded", "passed")

    completed_but_not_run = CodingRunResult(
        schema_version="0.1",
        run_result=base_rr,
        baseline_workspace_revision=rev_final,
        final_workspace_revision=rev_final,
        changed_paths=[],
        acceptance=AcceptanceSummary(
            status="not_run", revision=rev_final, command_ids=["pytest_acc"], commands_digest=digest
        ),
        terminal_status="completed",
        reason_code="TASK_ACHIEVED",
    )
    completed_but_failed = CodingRunResult(
        schema_version="0.1",
        run_result=base_rr,
        baseline_workspace_revision=rev_final,
        final_workspace_revision=rev_final,
        changed_paths=[],
        acceptance=AcceptanceSummary(
            status="failed", revision=rev_final, command_ids=["pytest_acc"], commands_digest=digest
        ),
        terminal_status="completed",
        reason_code="TASK_ACHIEVED",
    )
    passed_but_stale = CodingRunResult(
        schema_version="0.1",
        run_result=base_rr,
        baseline_workspace_revision=rev_stale,
        final_workspace_revision=rev_final,
        changed_paths=[],
        acceptance=AcceptanceSummary(
            status="passed",
            revision=rev_stale,
            command_ids=["pytest_acc"],
            commands_digest=digest,
            stale_reason="workspace_modified_after_verify",
        ),
        terminal_status="completed",
        reason_code="TASK_ACHIEVED",
    )

    for fake_res in (completed_but_not_run, completed_but_failed, passed_but_stale):
        state, verification, _ = evaluate_run_outcome(fake_res)
        assert state != "succeeded"
        assert verification != "passed"

    # Model claiming task_completion_hint=True after invalid action never yields succeeded/passed
    class _HintOnlyProvider:
        async def complete(self, request: ModelRequest) -> ModelResponse:
            body = {
                "schema_version": "0.1",
                "task_completion_hint": True,
                "proposed_action": {
                    "schema_version": "0.1",
                    "action_type": "read_file",
                    "payload": {"path": "metrics.py", "start_line": 1, "max_lines": 10},
                    "metadata": {},
                },
                "expected_outcomes": [
                    {
                        "schema_version": "0.1",
                        "outcome_type": "coding_tool_result",
                        "payload": {"action_type": "read_file", "path": "metrics.py"},
                        "required": True,
                    }
                ],
            }
            return ModelResponse(
                schema_version="0.1",
                request_id=request.request_id,
                status="ok",
                content=json.dumps(body),
            )

    async def _hint_run() -> None:
        svc = AppService(config=make_host_config(tmp_path / "hint"), provider_factory=lambda **_: _HintOnlyProvider())
        try:
            proj = await svc.create_project(name="hint-only")
            sess = svc.create_session(
                project_id=proj.project_id,
                model=ModelSelection(provider="offline", model="offline-metrics-v1"),
            )
            run = await svc.start_run(
                session_id=sess.session_id,
                request=StartRunRequest(client_request_id=uuid.uuid4(), goal="hint", confirmed_scope=True),
            )
            term = await async_wait_until(
                lambda: (
                    cur := svc.get_run(session_id=sess.session_id, run_id=run.run_id)
                ).state
                in ("succeeded", "failed", "cancelled", "unknown")
                and cur
            )
            assert term.state != "succeeded"
            assert term.verification != "passed"
        finally:
            await svc.aclose()

    asyncio.run(_hint_run())


def test_a18_failure_and_trace_io(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    canary = "PRIVATE_PROVIDER_EXCEPTION_CANARY_DO_NOT_SURFACE_987654"
    calls = 0

    class _ExplodingProvider:
        async def complete(self, request: ModelRequest) -> ModelResponse:
            nonlocal calls
            _ = request
            calls += 1
            raise RuntimeError(canary)

    async def _scenario_provider_fail() -> None:
        svc = AppService(config=make_host_config(tmp_path / "pfail"), provider_factory=lambda **_: _ExplodingProvider())
        try:
            proj = await svc.create_project(name="fail-prov")
            ws_before = tree_hashes(svc._config.workspace_parent / "fail-prov")
            sess = svc.create_session(
                project_id=proj.project_id,
                model=ModelSelection(provider="offline", model="offline-metrics-v1"),
            )
            run = await svc.start_run(
                session_id=sess.session_id,
                request=StartRunRequest(client_request_id=uuid.uuid4(), goal="boom", confirmed_scope=True),
            )
            term = await async_wait_until(
                lambda: (
                    cur := svc.get_run(session_id=sess.session_id, run_id=run.run_id)
                ).state
                in ("succeeded", "failed", "cancelled", "unknown")
                and cur
            )
            assert term.state in ("failed", "unknown")
            assert term.verification != "passed"
            assert calls == 1  # max_retries=0, no silent retry
            snap = svc.snapshot(session_id=sess.session_id, run_id=run.run_id)
            assert sum(1 for e in snap.events if e.kind == "run_finished") == 1
            serialized = snap.model_dump_json() + term.model_dump_json()
            assert canary not in serialized
            assert tree_hashes(svc._config.workspace_parent / "fail-prov") == ws_before
        finally:
            await svc.aclose()

    asyncio.run(_scenario_provider_fail())
    captured = capsys.readouterr()
    assert canary not in captured.out + captured.err

    # Corrupt/unwritable trace path fails cleanly via JsonlTraceStore without swallowing error
    async def _scenario_trace_fail() -> None:
        cfg = make_host_config(tmp_path / "tfail")
        from forecontract_app.demo import (
            build_controlled_coding_task,
            initialize_controlled_project,
        )

        spec = initialize_controlled_project(
            project_id=str(uuid.uuid4()),
            name="trace-io",
            data_root=cfg.data_root,
            workspace_parent=cfg.workspace_parent,
        )
        out_dir = cfg.data_root / "runs" / "r-trace" / "output"
        out_dir.mkdir(parents=True)
        ws_before = tree_hashes(spec.workspace_dir)
        trace_path = cfg.data_root / "runs" / "r-trace" / "trace.jsonl"
        corrupt_bytes = b"{corrupt-json-line\n"
        trace_path.write_bytes(corrupt_bytes)
        task, coding_cfg, planner_cfg = build_controlled_coding_task(
            project=spec,
            run_id="r-trace",
            goal="fail trace",
            output_root=out_dir,
            python_executable=cfg.python_executable,
        )

        emitted: list[str] = []
        with pytest.raises(CoreRuntimeError) as exc_info:
            await execute_run(
                task=task,
                config=coding_cfg,
                provider=CountingMetricsProvider(),
                planner_config=planner_cfg,
                trace_path=trace_path,
                emit=lambda kind, _: emitted.append(kind),
            )
        assert exc_info.value.code == "TRACE_FAILURE"
        assert "core_trace" not in emitted
        assert trace_path.read_bytes() == corrupt_bytes
        assert tree_hashes(spec.workspace_dir) == ws_before

    asyncio.run(_scenario_trace_fail())

    # Public AppService trace corruption & write-rejection scenarios (D05)
    async def _scenario_service_trace_fail() -> None:
        svc_cfg = make_host_config(tmp_path / "svc_tfail")
        corrupt_bytes = b"{corrupt-service-trace\n"
        orig_append = JsonlTraceStore.append
        seeded_paths: list[Path] = []

        def _seed_corrupt_then_real_append(store_self: JsonlTraceStore, record: Any) -> None:
            if not seeded_paths:
                store_self.path.write_bytes(corrupt_bytes)
                seeded_paths.append(store_self.path)
            orig_append(store_self, record)

        monkeypatch.setattr(JsonlTraceStore, "append", _seed_corrupt_then_real_append)
        svc = AppService(config=svc_cfg)
        try:
            proj = await svc.create_project(name="svc-trace-corrupt")
            ws_before = tree_hashes(svc_cfg.workspace_parent / "svc-trace-corrupt")
            sess = svc.create_session(
                project_id=proj.project_id,
                model=ModelSelection(provider="offline", model="offline-metrics-v1"),
            )
            run = await svc.start_run(
                session_id=sess.session_id,
                request=StartRunRequest(
                    client_request_id=uuid.uuid4(),
                    goal="corrupt trace via service",
                    confirmed_scope=True,
                ),
            )
            term = await async_wait_until(
                lambda: (cur := svc.get_run(session_id=sess.session_id, run_id=run.run_id)).state
                in ("succeeded", "failed", "cancelled", "unknown")
                and cur,
            )
            snap = svc.snapshot(session_id=sess.session_id, run_id=run.run_id)
            assert term.state in ("failed", "unknown")
            assert term.verification != "passed"
            assert sum(1 for e in snap.events if e.kind == "run_finished") == 1
            assert all(e.kind != "core_trace" for e in snap.events)
            assert tree_hashes(svc_cfg.workspace_parent / "svc-trace-corrupt") == ws_before
            assert len(seeded_paths) == 1
            assert seeded_paths[0].read_bytes() == corrupt_bytes
        finally:
            await svc.aclose()
            monkeypatch.undo()

        # Injected write error with exception canary never surfaces sensitive text
        trace_canary = "PRIVATE_TRACE_WRITE_CANARY_556677"

        def _reject_append(store_self: JsonlTraceStore, record: Any) -> None:
            _ = (store_self, record)
            raise TraceStorageError(trace_canary)

        monkeypatch.setattr(JsonlTraceStore, "append", _reject_append)
        svc2_cfg = make_host_config(tmp_path / "svc_treject")
        svc2 = AppService(config=svc2_cfg)
        try:
            proj2 = await svc2.create_project(name="svc-trace-reject")
            ws_before2 = tree_hashes(svc2_cfg.workspace_parent / "svc-trace-reject")
            sess2 = svc2.create_session(
                project_id=proj2.project_id,
                model=ModelSelection(provider="offline", model="offline-metrics-v1"),
            )
            run2 = await svc2.start_run(
                session_id=sess2.session_id,
                request=StartRunRequest(
                    client_request_id=uuid.uuid4(),
                    goal="reject trace write",
                    confirmed_scope=True,
                ),
            )
            term2 = await async_wait_until(
                lambda: (cur := svc2.get_run(session_id=sess2.session_id, run_id=run2.run_id)).state
                in ("succeeded", "failed", "cancelled", "unknown")
                and cur,
            )
            snap2 = svc2.snapshot(session_id=sess2.session_id, run_id=run2.run_id)
            assert term2.state in ("failed", "unknown")
            assert term2.verification != "passed"
            assert sum(1 for e in snap2.events if e.kind == "run_finished") == 1
            assert all(e.kind != "core_trace" for e in snap2.events)
            assert tree_hashes(svc2_cfg.workspace_parent / "svc-trace-reject") == ws_before2
            assert trace_canary not in snap2.model_dump_json() + term2.model_dump_json()
        finally:
            await svc2.aclose()
            monkeypatch.undo()

    asyncio.run(_scenario_service_trace_fail())


def test_a19_real_command_cancel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", CREDENTIAL_ENV_CANARY)
    assert os.environ["OPENAI_API_KEY"] == CREDENTIAL_ENV_CANARY
    cfg = make_host_config(tmp_path)
    ws = tmp_path / "ws_a19"
    out = tmp_path / "out_a19"
    ws.mkdir()
    out.mkdir()
    (ws / "metrics.py").write_text("x = 1\n", encoding="utf-8")
    acc_file = tmp_path / "test_acc_a19.py"
    acc_file.write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    pid_file = tmp_path / "child_pid.txt"
    obs_file = tmp_path / "child_env_obs.json"
    late_marker = tmp_path / "late_write.txt"
    child_code = (
        "import json, os, pathlib, time\n"
        f"pathlib.Path({str(obs_file)!r}).write_text("
        "json.dumps(dict(OPENAI_API_KEY_present=('OPENAI_API_KEY' in os.environ))), encoding='utf-8')\n"
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        "time.sleep(60)\n"
        f"pathlib.Path({str(late_marker)!r}).write_text('late', encoding='utf-8')\n"
    )
    sentinel_proc = subprocess.Popen(
        [sys.executable, "-B", "-c", "import time; time.sleep(60)"],
        env=make_clean_subprocess_env(),
    )
    try:
        slow_spec = CommandSpec(
            command_id="slow_diag",
            executable=sys.executable,
            args=["-B", "-c", child_code],
            cwd=str(ws),
            env={"PYTHONPATH": str(ws), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
            timeout_ms=15000,
            purpose="diagnostic",
        )
        prod_env = verify_production_child_env_equivalence(tmp_path / "prod_env_check")
        slow_env = build_minimal_windows_env(spec=slow_spec, temp_dir=tmp_path / "prod_env_check")
        assert sorted(slow_env.keys()) == sorted(prod_env.keys())
        assert "OPENAI_API_KEY" not in slow_env

        task = CodingTask(
            schema_version="0.1",
            run_id="run-a19",
            goal_id="goal-a19",
            goal_text="run slow diagnostic command then cancel",
            workspace_root=str(ws),
            read_paths=["metrics.py"],
            write_paths=[WriteTarget(path="metrics.py", operation="modify")],
            commands={
                "slow_diag": slow_spec,
                "acc_ok": CommandSpec(
                    command_id="acc_ok",
                    executable=sys.executable,
                    args=["-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--junitxml={result_dir}/junit.xml", str(acc_file)],
                    cwd=str(ws),
                    env={"PYTHONPATH": str(ws), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
                    timeout_ms=15000,
                    purpose="acceptance",
                    expected_tests=[{"classname": "test_acc_a19", "name": "test_ok"}],
                ),
            },
            verification_command_ids=["acc_ok"],
            max_steps=4,
            max_planning_calls=4,
            max_replans=1,
            run_timeout_ms=60000,
        )
        coding_cfg = CodingConfig(schema_version="0.1", output_root=str(out), process_cleanup_ms=2000)
        planner_cfg = PlannerConfig(schema_version="0.1", call_timeout_ms=15000, max_calls=4)

        provider_calls = 0

        class _SpawnDiagProvider:
            async def complete(self, request: ModelRequest) -> ModelResponse:
                nonlocal provider_calls
                provider_calls += 1
                body = {
                    "schema_version": "0.1",
                    "proposed_action": {
                        "schema_version": "0.1",
                        "action_type": "run_command",
                        "payload": {"command_id": "slow_diag"},
                        "metadata": {},
                    },
                    "expected_outcomes": [
                        {
                            "schema_version": "0.1",
                            "outcome_type": "coding_tool_result",
                            "payload": {"action_type": "run_command", "command_id": "slow_diag"},
                            "required": True,
                        }
                    ],
                }
                return ModelResponse(
                    schema_version="0.1",
                    request_id=request.request_id,
                    status="ok",
                    content=json.dumps(body),
                )

        async def _run_and_cancel() -> None:
            run_task = asyncio.create_task(
                execute_run(
                    task=task,
                    config=coding_cfg,
                    provider=_SpawnDiagProvider(),
                    planner_config=planner_cfg,
                    trace_path=cfg.data_root / "a19_trace.jsonl",
                    emit=lambda *_: None,
                )
            )
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                if pid_file.exists() and obs_file.exists():
                    pid_txt = pid_file.read_text(encoding="utf-8").strip()
                    obs_txt = obs_file.read_text(encoding="utf-8").strip()
                    if pid_txt and obs_txt:
                        break
                if run_task.done():
                    early_res = run_task.result()
                    raise AssertionError(
                        f"run ended before child ready: status={early_res.terminal_status} reason={early_res.reason_code}"
                    )
                await asyncio.sleep(0.02)
            else:
                raise AssertionError("timed out waiting for A19 child ready files")

            child_pid = int(pid_txt)
            obs = json.loads(obs_txt)
            assert obs["OPENAI_API_KEY_present"] is False
            assert os.environ.get("OPENAI_API_KEY") == CREDENTIAL_ENV_CANARY
            assert not run_task.done()
            assert is_pid_alive(child_pid)
            record_shared_child_env_observation(obs | {"prod_env_keys_match": True})

            calls_before_cancel = provider_calls
            run_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await run_task

            wait_until(lambda: not is_pid_alive(child_pid), timeout=5.0)
            assert not is_pid_alive(child_pid)
            assert not late_marker.exists()
            assert is_pid_alive(sentinel_proc.pid)
            assert provider_calls == calls_before_cancel

        asyncio.run(_run_and_cancel())
    finally:
        sentinel_proc.terminate()
        sentinel_proc.wait(timeout=5.0)


def test_a20_deadline_and_teardown(tmp_path: Path) -> None:
    cfg = make_host_config(tmp_path)
    out = tmp_path / "out_a20"
    out.mkdir()
    ws = tmp_path / "ws_a20"
    ws.mkdir()
    (ws / "metrics.py").write_text("x = 1\n", encoding="utf-8")
    acc_file = tmp_path / "test_acc_a20.py"
    acc_file.write_text("def test_ok():\n    assert True\n", encoding="utf-8")

    task = CodingTask(
        schema_version="0.1",
        run_id="run-a20",
        goal_id="goal-a20",
        goal_text="deadline test",
        workspace_root=str(ws),
        read_paths=["metrics.py"],
        write_paths=[WriteTarget(path="metrics.py", operation="modify")],
        commands={
            "acc_ok": CommandSpec(
                command_id="acc_ok",
                executable=sys.executable,
                args=["-B", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--junitxml={result_dir}/junit.xml", str(acc_file)],
                cwd=str(ws),
                env={"PYTHONPATH": str(ws), "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
                timeout_ms=15000,
                purpose="acceptance",
                expected_tests=[{"classname": "test_acc_a20", "name": "test_ok"}],
            ),
        },
        verification_command_ids=["acc_ok"],
        max_steps=12,
        max_planning_calls=12,
        max_replans=4,
        run_timeout_ms=250,
    )
    coding_cfg = CodingConfig(schema_version="0.1", output_root=str(out), process_cleanup_ms=1000)
    planner_cfg = PlannerConfig(
        schema_version="0.1", call_timeout_ms=1500, cleanup_timeout_ms=200, max_calls=12
    )

    provider_calls = 0
    finished = False
    late_calls = 0

    class _SlowTimeoutProvider:
        async def complete(self, request: ModelRequest) -> ModelResponse:
            nonlocal provider_calls, late_calls
            _ = request
            if finished:
                late_calls += 1
            provider_calls += 1
            await asyncio.sleep(2.0)
            raise AssertionError("unreachable")

    async def _scenario() -> None:
        nonlocal finished
        t0 = time.monotonic()
        res = await execute_run(
            task=task,
            config=coding_cfg,
            provider=_SlowTimeoutProvider(),
            planner_config=planner_cfg,
            trace_path=cfg.data_root / "a20_trace.jsonl",
            emit=lambda *_: None,
        )
        elapsed = time.monotonic() - t0
        finished = True
        state, verification, _ = evaluate_run_outcome(res)
        assert state in ("failed", "unknown")
        assert verification != "passed"
        assert elapsed <= 2.5
        assert provider_calls <= 12
        await asyncio.sleep(0.1)
        assert late_calls == 0

        svc = AppService(config=make_host_config(tmp_path / "svc"))
        assert await svc.aclose() is None
        assert await svc.aclose() is None

    asyncio.run(_scenario())
