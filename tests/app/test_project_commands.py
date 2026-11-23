"""B14–B18: Streaming UTF-8/1MiB command ring buffer, Job-tree stop/timeout cleanup, busy lock, JUnit CommandSpec."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from forecontract_app.project_commands import (
    ProjectCommandRunner,
    build_command_spec_for_check,
)
from forecontract_app.project_models import (
    CommandProposal,
    ExpectedTestCaseView,
    ProjectError,
    ProjectTask,
    ProposalDecision,
    VerificationCheck,
    VerificationPlan,
)
from forecontract_app.project_permissions import PermissionBroker
from forecontract_app.project_verification import evaluate_project_result
from forecontract_app.project_workspace import ProjectWorkspace
from forecontract_coding.errors import CodingError
from project_helpers import (
    create_buggy_calc_project,
    is_pid_alive,
    write_barrier_script,
)


def _approve_dummy_plan(broker: PermissionBroker) -> None:
    plan_p = broker.create_verification_proposal(
        plan=VerificationPlan(
            plan_id="dummy-plan",
            summary="dummy verification plan",
            checks=[
                VerificationCheck(
                    check_id="dummy-u1",
                    kind="exit_code",
                    executable=sys.executable,
                    argv=["-B", "-c", "print('ok')"],
                    cwd=".",
                    timeout_ms=5000,
                    protected_paths=[],
                )
            ],
        ),
        protected_shas={},
        current_instruction_sha256="",
    )
    broker.decide(
        proposal_id=plan_p.id,
        request=ProposalDecision(digest=plan_p.digest, decision="approve"),
    )


async def _wait_for_file(path: Path, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not path.exists():
        if asyncio.get_running_loop().time() > deadline:
            raise TimeoutError(f"timed out waiting for ready file {path}")
        await asyncio.sleep(0.02)


def test_b14_streaming_utf8_incremental_poll_and_1mib_ring(tmp_path: Path) -> None:
    repo = create_buggy_calc_project(tmp_path / "repo")
    data_root = tmp_path / "data"
    data_root.mkdir()
    ws = ProjectWorkspace(root=repo, data_root=data_root)
    broker = PermissionBroker(run_id="run-b14", project_root=repo, root_identity=ws.root_identity)
    _approve_dummy_plan(broker)
    events: list[tuple[str, dict[str, Any]]] = []
    runner = ProjectCommandRunner(
        run_id="run-b14",
        project_root=repo,
        data_root=data_root,
        broker=broker,
        emit=lambda k, p: events.append((k, p)),
    )

    script = repo / "emit_big.py"
    script.write_text(
        "import sys\n"
        "chunk = ('中文流式测试-0123456789ABCDEF-' * 300) + '\\n'\n"
        "for _ in range(180):\n"
        "    sys.stdout.write(chunk)\n"
        "sys.stdout.flush()\n"
        "sys.stderr.write('ERR_UTF8_尾巴\\n')\n"
        "sys.stderr.flush()\n",
        encoding="utf-8",
    )

    prop = broker.create_command_proposal(
        command=CommandProposal(
            command_id="big-stream",
            executable=sys.executable,
            argv=["-B", "emit_big.py"],
            cwd=".",
            timeout_ms=15000,
            purpose="diagnostic",
            explanation="stream >1 MiB UTF-8",
        ),
        current_instruction_sha256="",
    )
    broker.decide(
        proposal_id=prop.id,
        request=ProposalDecision(digest=prop.digest, decision="approve", trusted_local_execution=True),
    )

    async def _scenario() -> None:
        view = await runner.run_approved_command(proposal_id=prop.id, proposal_digest=prop.digest)
        assert view.state == "exited"
        assert view.exit_code == 0
        assert view.bytes_seen > 1_048_576
        assert view.truncated is True
        assert view.truncated_stdout_bytes > 0
        assert view.cleanup_ok is True
        assert view.remaining_active == 0

        # Invalid cursor rejected with 422 INVALID_CURSOR
        with pytest.raises(ProjectError) as neg_exc:
            runner.get_command(view.command_id, after=-1)
        assert neg_exc.value.status_code == 422

        # Requesting evicted seq 0 when chunks were dropped raises 409 CURSOR_EXPIRED
        if view.first_seq > 1:
            with pytest.raises(ProjectError) as exp_exc:
                runner.get_command(view.command_id, after=0)
            assert exp_exc.value.status_code == 409
            assert exp_exc.value.code == "CURSOR_EXPIRED"

        # Bounded <=16 KiB paging from first_seq - 1 drains cleanly
        cursor = max(0, view.first_seq - 1)
        collected_text: list[str] = []
        pages = 0
        while cursor < view.last_seq:
            page = runner.get_command(view.command_id, after=cursor)
            page_bytes = sum(len(c.text.encode("utf-8")) for c in page.chunks)
            assert page_bytes <= 16 * 1024 + 512
            collected_text.extend(c.text for c in page.chunks)
            if not page.chunks:
                break
            cursor = page.chunks[-1].seq
            pages += 1
        assert pages >= 2
        joined = "".join(collected_text)
        assert "中文流式测试" in joined
        assert "ERR_UTF8_尾巴" in joined

    asyncio.run(_scenario())
    assert any(k == "command_started" for k, _ in events)
    assert any(k == "command_output" for k, _ in events)
    assert any(k == "command_finished" for k, _ in events)


def test_b15_start_poll_stop_ready_barrier_and_job_tree_cleanup(tmp_path: Path) -> None:
    outside_sentinel = tmp_path / "outside.txt"
    outside_sentinel.write_text("KEEP_ME", encoding="utf-8")
    repo = create_buggy_calc_project(tmp_path / "repo")
    data_root = tmp_path / "data"
    data_root.mkdir()
    ws = ProjectWorkspace(root=repo, data_root=data_root)
    broker = PermissionBroker(run_id="run-b15", project_root=repo, root_identity=ws.root_identity)
    _approve_dummy_plan(broker)
    runner = ProjectCommandRunner(run_id="run-b15", project_root=repo, data_root=data_root, broker=broker)

    script = write_barrier_script(repo / "barrier.py", spawn_child=True)
    ready_file = data_root / "ready.pid"
    release_file = data_root / "release.flag"
    child_pid_file = data_root / "child.pid"

    prop = broker.create_command_proposal(
        command=CommandProposal(
            command_id="barrier-cmd",
            executable=sys.executable,
            argv=["-B", str(script.name), str(ready_file), str(release_file), str(child_pid_file)],
            cwd=".",
            timeout_ms=15000,
            purpose="diagnostic",
            explanation="ready/release child tree stop",
        ),
        current_instruction_sha256="",
    )
    broker.decide(
        proposal_id=prop.id,
        request=ProposalDecision(digest=prop.digest, decision="approve", trusted_local_execution=True),
    )

    sentinel_proc = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(60)"])
    try:
        async def _scenario() -> None:
            started = runner.start_approved_command(proposal_id=prop.id, proposal_digest=prop.digest)
            assert started.state in ("starting", "running")
            await _wait_for_file(ready_file)
            await _wait_for_file(child_pid_file)
            parent_pid = int(ready_file.read_text(encoding="utf-8").strip())
            child_pid = int(child_pid_file.read_text(encoding="utf-8").strip())
            assert parent_pid > 0
            assert child_pid > 0
            assert child_pid != parent_pid
            assert is_pid_alive(parent_pid) and is_pid_alive(child_pid)
            assert is_pid_alive(sentinel_proc.pid) is True
            assert runner.has_running_command() is True
            assert runner.get_command(started.command_id).state == "running"

            stopped_view, already_terminal = await runner.stop_command(started.command_id)
            assert already_terminal is False
            assert stopped_view.state == "cancelled"
            assert stopped_view.cleanup_ok is True
            assert stopped_view.remaining_active == 0
            assert runner.has_running_command() is False
            assert is_pid_alive(parent_pid) is False
            assert is_pid_alive(child_pid) is False
            assert is_pid_alive(sentinel_proc.pid) is True

        asyncio.run(_scenario())
    finally:
        sentinel_proc.terminate()
        sentinel_proc.wait(timeout=5)
    assert outside_sentinel.read_text(encoding="utf-8") == "KEEP_ME"


def test_b16_timeout_kills_process_tree_and_bounds_cleanup(tmp_path: Path) -> None:
    outside_sentinel = tmp_path / "outside.txt"
    outside_sentinel.write_text("KEEP_TIMEOUT", encoding="utf-8")
    repo = create_buggy_calc_project(tmp_path / "repo")
    data_root = tmp_path / "data"
    data_root.mkdir()
    ws = ProjectWorkspace(root=repo, data_root=data_root)
    broker = PermissionBroker(run_id="run-b16", project_root=repo, root_identity=ws.root_identity)
    _approve_dummy_plan(broker)
    runner = ProjectCommandRunner(run_id="run-b16", project_root=repo, data_root=data_root, broker=broker)

    script = write_barrier_script(repo / "hang.py", spawn_child=True)
    ready_file = data_root / "ready_hang.pid"
    release_file = data_root / "never_released.flag"
    child_pid_file = data_root / "child_hang.pid"

    prop = broker.create_command_proposal(
        command=CommandProposal(
            command_id="timeout-cmd",
            executable=sys.executable,
            argv=["-B", str(script.name), str(ready_file), str(release_file), str(child_pid_file)],
            cwd=".",
            timeout_ms=5000,
            purpose="diagnostic",
            explanation="timeout cleanup test",
        ),
        current_instruction_sha256="",
    )
    broker.decide(
        proposal_id=prop.id,
        request=ProposalDecision(digest=prop.digest, decision="approve", trusted_local_execution=True),
    )

    sentinel_proc = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(60)"])
    try:
        async def _scenario() -> None:
            started = runner.start_approved_command(proposal_id=prop.id, proposal_digest=prop.digest)
            assert started.state in ("starting", "running")
            await _wait_for_file(ready_file)
            await _wait_for_file(child_pid_file)
            parent_pid = int(ready_file.read_text(encoding="utf-8").strip())
            child_pid = int(child_pid_file.read_text(encoding="utf-8").strip())
            assert parent_pid > 0
            assert child_pid > 0
            assert child_pid != parent_pid
            assert is_pid_alive(parent_pid) and is_pid_alive(child_pid)
            assert is_pid_alive(sentinel_proc.pid) is True
            assert runner.get_command(started.command_id).state == "running"

            view = await runner.wait_command(started.command_id)
            assert view.timed_out is True
            assert view.state == "timed_out"
            assert view.cleanup_ok is True
            assert view.remaining_active == 0
            assert is_pid_alive(parent_pid) is False
            assert is_pid_alive(child_pid) is False
            assert is_pid_alive(sentinel_proc.pid) is True

        asyncio.run(_scenario())
    finally:
        sentinel_proc.terminate()
        sentinel_proc.wait(timeout=5)
    assert outside_sentinel.read_text(encoding="utf-8") == "KEEP_TIMEOUT"


def test_b17_command_in_progress_blocks_writes_and_concurrent_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from forecontract.models.actions import ActionSpec
    from forecontract.models.outcomes import ExecutionStatus
    from forecontract_app.project_adapter import ProjectCodingAdapter

    repo = create_buggy_calc_project(tmp_path / "repo")
    data_root = tmp_path / "data"
    data_root.mkdir()
    ws = ProjectWorkspace(root=repo, data_root=data_root)
    broker = PermissionBroker(run_id="run-b17", project_root=repo, root_identity=ws.root_identity)
    runner = ProjectCommandRunner(run_id="run-b17", project_root=repo, data_root=data_root, broker=broker)
    task = ProjectTask(
        run_id="run-b17",
        goal_id="g-b17",
        goal_text="Busy command guard",
        project_id="p-b17",
        root_identity=ws.root_identity,
        read_grant_id="rg-b17",
        data_root=data_root,
        output_root=tmp_path / "out",
        python_executable=Path(sys.executable),
    )
    adapter = ProjectCodingAdapter(task=task, workspace=ws, broker=broker, command_runner=runner)

    write_barrier_script(repo / "wait_busy.py", spawn_child=False)
    ready_file = data_root / "busy_ready.pid"
    release_file = data_root / "busy_release.flag"

    cmd1 = broker.create_command_proposal(
        command=CommandProposal(
            command_id="busy-1",
            executable=sys.executable,
            argv=["-B", "wait_busy.py", str(ready_file), str(release_file)],
            cwd=".",
            timeout_ms=10000,
            purpose="diagnostic",
            explanation="hold command running",
        ),
        current_instruction_sha256="",
    )
    broker.decide(
        proposal_id=cmd1.id,
        request=ProposalDecision(digest=cmd1.digest, decision="approve", trusted_local_execution=True),
    )

    cmd2 = broker.create_command_proposal(
        command=CommandProposal(
            command_id="busy-2",
            executable=sys.executable,
            argv=["-B", "-c", "print('second')"],
            cwd=".",
            timeout_ms=5000,
            purpose="diagnostic",
            explanation="blocked second start",
        ),
        current_instruction_sha256="",
    )
    broker.decide(
        proposal_id=cmd2.id,
        request=ProposalDecision(digest=cmd2.digest, decision="approve", trusted_local_execution=True),
    )

    async def _scenario() -> None:
        await adapter.start()
        try:
            # Before any VerificationPlan is approved, start_command MUST reject without spawning
            no_plan_res = await adapter.execute(
                ActionSpec(
                    schema_version="0.1",
                    action_type="start_command",
                    payload={"proposal_id": cmd1.id, "proposal_digest": cmd1.digest},
                    metadata={"action_id": "a0"},
                )
            )
            assert no_plan_res.status == ExecutionStatus.FAILED
            assert no_plan_res.error is not None
            assert no_plan_res.error.code == "VERIFICATION_PLAN_REQUIRED"

            _approve_dummy_plan(broker)

            start_res = await adapter.execute(
                ActionSpec(
                    schema_version="0.1",
                    action_type="start_command",
                    payload={"proposal_id": cmd1.id, "proposal_digest": cmd1.digest},
                    metadata={"action_id": "a1"},
                )
            )
            assert start_res.error is None
            await _wait_for_file(ready_file)

            # While command is running, write / second start_command / verify_project MUST return COMMAND_IN_PROGRESS
            for action_type, payload, aid in (
                ("create_file", {"path": "new_x.py", "content": "X=1\n"}, "a2"),
                (
                    "edit_file",
                    {"path": "calc.py", "expected_sha256": "a" * 64, "old_text": "a", "new_text": "b"},
                    "a3",
                ),
                ("start_command", {"proposal_id": cmd2.id, "proposal_digest": cmd2.digest}, "a4"),
                ("verify_project", {"plan_id": "p1", "plan_digest": "a" * 64}, "a5"),
            ):
                blocked = await adapter.execute(
                    ActionSpec(
                        schema_version="0.1",
                        action_type=action_type,
                        payload=payload,
                        metadata={"action_id": aid},
                    )
                )
                assert blocked.error is not None
                assert blocked.error.code == "COMMAND_IN_PROGRESS"

            release_file.write_text("go", encoding="utf-8")
            finished = await runner.wait_command("busy-1")
            assert finished.state == "exited"
            assert finished.exit_code == 0
        finally:
            await adapter.stop()

    asyncio.run(_scenario())

    # Cleanup failure injection -> command view becomes unknown(cleanup_ok=False, remaining_active=1)
    # and evaluate_project_result resolves terminal_status="unknown", reason_code="CLEANUP_INCOMPLETE"
    import forecontract_app.project_commands as pc

    def _boom_cleanup(**kwargs: Any) -> Any:
        raise CodingError("CODING_PROCESS_CLEANUP_FAILED", "simulated residual process")

    monkeypatch.setattr(pc, "run_command_in_job", _boom_cleanup)
    broker_cf = PermissionBroker(run_id="run-b17-cf", project_root=repo, root_identity=ws.root_identity)
    _approve_dummy_plan(broker_cf)
    runner_cf = ProjectCommandRunner(
        run_id="run-b17-cf", project_root=repo, data_root=data_root, broker=broker_cf
    )
    adapter_cf = ProjectCodingAdapter(
        task=task.model_copy(update={"run_id": "run-b17-cf"}),
        workspace=ws,
        broker=broker_cf,
        command_runner=runner_cf,
    )
    cmd_cf = broker_cf.create_command_proposal(
        command=CommandProposal(
            command_id="cleanup-fail-cmd",
            executable=sys.executable,
            argv=["-V"],
            cwd=".",
            timeout_ms=5000,
            purpose="diagnostic",
            explanation="cleanup failure injection",
        ),
        current_instruction_sha256="",
    )
    broker_cf.decide(
        proposal_id=cmd_cf.id,
        request=ProposalDecision(digest=cmd_cf.digest, decision="approve", trusted_local_execution=True),
    )

    async def _run_cleanup_fail() -> None:
        await adapter_cf.start()
        try:
            view = await runner_cf.run_approved_command(
                proposal_id=cmd_cf.id, proposal_digest=cmd_cf.digest
            )
            assert view.state == "unknown"
            assert view.cleanup_ok is False
            assert view.remaining_active == 1
            ev = adapter_cf.build_evidence(core_completed=False)
            res = evaluate_project_result(task=adapter_cf._task, evidence=ev)
            assert res.terminal_status == "unknown"
            assert res.reason_code == "CLEANUP_INCOMPLETE"
        finally:
            await adapter_cf.stop()

    asyncio.run(_run_cleanup_fail())


def test_b18_junit_command_spec_mapping_and_credential_env_scrub(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = create_buggy_calc_project(tmp_path / "repo")
    data_root = tmp_path / "data"
    data_root.mkdir()
    ws = ProjectWorkspace(root=repo, data_root=data_root)
    broker = PermissionBroker(run_id="run-b18", project_root=repo, root_identity=ws.root_identity)
    _approve_dummy_plan(broker)
    runner = ProjectCommandRunner(run_id="run-b18", project_root=repo, data_root=data_root, broker=broker)

    # Verify F02 CommandSpec mapping rules directly:
    # junit_exact -> purpose="acceptance", non-empty expected_tests; {result_dir} expanded
    cmd_junit = CommandProposal(
        command_id="j1",
        executable=sys.executable,
        argv=["-B", "-m", "pytest", "--junitxml={result_dir}/out.xml", "tests/test_calc.py"],
        cwd=".",
        timeout_ms=5000,
        purpose="verification",
        explanation="junit exact",
        check_id="j1",
    )
    res_dir = tmp_path / "res_j1"
    res_dir.mkdir()
    spec_acc, expanded_acc = build_command_spec_for_check(
        command_id="j1",
        proposal=cmd_junit,
        resolved_cwd=repo,
        check_kind="junit_exact",
        expected_tests=[ExpectedTestCaseView(classname="tests.test_calc", name="test_add_basic")],
        result_dir=res_dir,
    )
    assert spec_acc.purpose == "acceptance"
    assert len(spec_acc.expected_tests) == 1
    assert any(str(res_dir) in arg for arg in expanded_acc)

    # exit_code & diagnostic -> purpose="diagnostic", expected_tests=()
    spec_exit, _ = build_command_spec_for_check(
        command_id="b1",
        proposal=cmd_junit.model_copy(update={"command_id": "b1", "argv": ["-B", "-c", "print('ok')"]}),
        resolved_cwd=repo,
        check_kind="exit_code",
        expected_tests=[],
        result_dir=res_dir,
    )
    assert spec_exit.purpose == "diagnostic"
    assert spec_exit.expected_tests == []

    # Credential env canary in parent process MUST NOT leak into child subprocess
    canary = "sk-canary-never-inherit-secret"
    monkeypatch.setenv("DEEPSEEK_API_KEY", canary)
    monkeypatch.setenv("OPENAI_API_KEY", canary)
    env_probe = broker.create_command_proposal(
        command=CommandProposal(
            command_id="env-probe",
            executable=sys.executable,
            argv=[
                "-B",
                "-c",
                "import os; print('LEAK' if os.getenv('DEEPSEEK_API_KEY') or os.getenv('OPENAI_API_KEY') else 'CLEAN')",
            ],
            cwd=".",
            timeout_ms=5000,
            purpose="diagnostic",
            explanation="verify credential env scrub",
        ),
        current_instruction_sha256="",
    )
    broker.decide(
        proposal_id=env_probe.id,
        request=ProposalDecision(digest=env_probe.digest, decision="approve", trusted_local_execution=True),
    )

    async def _scenario() -> None:
        v = await runner.run_approved_command(proposal_id=env_probe.id, proposal_digest=env_probe.digest)
        assert v.exit_code == 0
        out, _ = runner.get_captured_output(v.command_id)
        assert "CLEAN" in out
        assert canary not in out

    asyncio.run(_scenario())
