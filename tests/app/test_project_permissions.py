"""B10–B13: Non-refreshing 120s TTL, cancel revocation, digest binding, exe/instruction tamper checks, and verification plans."""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
from forecontract_app.project_commands import ProjectCommandRunner
from forecontract_app.project_models import (
    CommandProposal,
    ExpectedTestCaseView,
    ProjectError,
    ProposalDecision,
    ScopeProposalPayload,
    VerificationCheck,
    VerificationPlan,
)
from forecontract_app.project_permissions import PermissionBroker
from forecontract_app.project_workspace import ProjectWorkspace, sha256_bytes
from project_helpers import create_buggy_calc_project
from pydantic import ValidationError


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def test_b10_non_refreshing_120s_ttl_and_digest_binding(tmp_path: Path) -> None:
    repo = create_buggy_calc_project(tmp_path / "repo")
    data_root = tmp_path / "data"
    data_root.mkdir()
    clock = _FakeClock(start=0.0)

    ws = ProjectWorkspace(root=repo, data_root=data_root)
    broker = PermissionBroker(
        run_id="run-b10",
        project_root=repo,
        project_id="proj-b10",
        session_id="sess-b10",
        root_identity=ws.root_identity,
        instruction_sha256="a" * 64,
        ttl_seconds=120.0,
        clock=clock,
    )

    scope_prop = broker.create_scope_proposal(
        scope=ScopeProposalPayload(
            exact_paths=["calc.py"],
            operations=["modify"],
            protected_paths=["tests/test_calc.py"],
            verification_plan_summary="run pytest",
        ),
        current_instruction_sha256="a" * 64,
    )
    assert scope_prop.expires_at == 120.0

    # Approve at t=100.0 -> expires_at MUST NOT extend to 220.0 (F04 non-refreshing TTL)
    clock.now = 100.0
    approved = broker.decide(
        proposal_id=scope_prop.id,
        request=ProposalDecision(digest=scope_prop.digest, decision="approve"),
    )
    assert approved.state == "approved"
    assert approved.expires_at == 120.0

    plan_prop = broker.create_verification_proposal(
        plan=VerificationPlan(
            plan_id="plan-b10",
            summary="run pytest",
            checks=[
                VerificationCheck(
                    check_id="u1",
                    kind="exit_code",
                    executable=sys.executable,
                    argv=["-B", "-c", "print('ok')"],
                    cwd=".",
                    timeout_ms=5000,
                    protected_paths=["tests/test_calc.py"],
                )
            ],
        ),
        protected_shas=ws.hash_protected_paths(["tests/test_calc.py"]),
        current_instruction_sha256="a" * 64,
    )
    broker.decide(
        proposal_id=plan_prop.id,
        request=ProposalDecision(digest=plan_prop.digest, decision="approve"),
    )

    # Wrong digest / wrong run / expired / wrong root_identity rejected cleanly
    with pytest.raises(ProjectError) as wrong_dig:
        broker.decide(
            proposal_id=scope_prop.id,
            request=ProposalDecision(digest="0" * 64, decision="approve"),
        )
    assert wrong_dig.value.code == "PROPOSAL_DIGEST_MISMATCH"

    with pytest.raises(ProjectError) as wrong_run:
        broker.verify_write_allowed(
            path="calc.py",
            operation="modify",
            current_run_id="other-run",
            current_root_identity=ws.root_identity,
            current_instruction_sha256="a" * 64,
        )
    assert wrong_run.value.code == "PROPOSAL_CONFLICT"

    # Control: at t=110.0 valid matching identity succeeds
    clock.now = 110.0
    broker.verify_write_allowed(
        path="calc.py",
        operation="modify",
        current_run_id="run-b10",
        current_root_identity=ws.root_identity,
        current_instruction_sha256="a" * 64,
    )

    # At t=121.0 previously approved grant expires and fails 409 PROPOSAL_EXPIRED
    clock.now = 121.0
    with pytest.raises(ProjectError) as expired_exc:
        broker.verify_write_allowed(
            path="calc.py",
            operation="modify",
            current_run_id="run-b10",
            current_root_identity=ws.root_identity,
            current_instruction_sha256="a" * 64,
        )
    assert expired_exc.value.status_code == 409
    assert expired_exc.value.code == "PROPOSAL_EXPIRED"

    # Wrong root identity on a fresh active scope grant raises PROPOSAL_CONFLICT
    fresh_scope = broker.create_scope_proposal(
        scope=ScopeProposalPayload(
            exact_paths=["calc.py"],
            operations=["modify"],
            protected_paths=["tests/test_calc.py"],
            verification_plan_summary="run pytest",
        ),
        current_instruction_sha256="a" * 64,
    )
    broker.decide(
        proposal_id=fresh_scope.id,
        request=ProposalDecision(digest=fresh_scope.digest, decision="approve"),
    )
    with pytest.raises(ProjectError) as wrong_root:
        broker.verify_write_allowed(
            path="calc.py",
            operation="modify",
            current_run_id="run-b10",
            current_root_identity="d:/other_root",
            current_instruction_sha256="a" * 64,
        )
    assert wrong_root.value.code == "PROPOSAL_CONFLICT"


def test_b11_cancel_revokes_approved_and_instruction_tamper_blocks_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = create_buggy_calc_project(tmp_path / "repo")
    data_root = tmp_path / "data"
    data_root.mkdir()
    agents_file = repo / "AGENTS.md"
    agents_file.write_text("# v1\n", encoding="utf-8")

    ws = ProjectWorkspace(root=repo, data_root=data_root)
    inst1 = ws.load_instructions()
    assert inst1.sha256 is not None

    broker = PermissionBroker(
        run_id="run-b11",
        project_root=repo,
        root_identity=ws.root_identity,
        instruction_sha256=inst1.sha256,
        ttl_seconds=120.0,
    )
    runner = ProjectCommandRunner(
        run_id="run-b11",
        project_root=repo,
        data_root=data_root,
        broker=broker,
    )

    spawn_calls: list[Any] = []
    import forecontract_app.project_commands as pc

    orig_run_job = pc.run_command_in_job

    def _spy_run_job(**kwargs: Any) -> Any:
        spawn_calls.append(kwargs)
        return orig_run_job(**kwargs)

    monkeypatch.setattr(pc, "run_command_in_job", _spy_run_job)

    scope_p = broker.create_scope_proposal(
        scope=ScopeProposalPayload(
            exact_paths=["calc.py"],
            operations=["modify"],
            protected_paths=[],
            verification_plan_summary="check",
        ),
        current_instruction_sha256=inst1.sha256,
    )
    broker.decide(
        proposal_id=scope_p.id,
        request=ProposalDecision(digest=scope_p.digest, decision="approve"),
    )

    plan_p = broker.create_verification_proposal(
        plan=VerificationPlan(
            plan_id="plan-b11-1",
            summary="check",
            checks=[
                VerificationCheck(
                    check_id="u1",
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
        current_instruction_sha256=inst1.sha256,
    )
    broker.decide(
        proposal_id=plan_p.id,
        request=ProposalDecision(digest=plan_p.digest, decision="approve"),
    )

    cmd_p = broker.create_command_proposal(
        command=CommandProposal(
            command_id="cmd-cancel-1",
            executable=sys.executable,
            argv=["-B", "-c", "print('hi')"],
            cwd=".",
            timeout_ms=5000,
            purpose="diagnostic",
            explanation="print hi",
        ),
        current_instruction_sha256=inst1.sha256,
    )
    broker.decide(
        proposal_id=cmd_p.id,
        request=ProposalDecision(
            digest=cmd_p.digest, decision="approve", trusted_local_execution=True
        ),
    )

    # F04: cancel_pending MUST revoke BOTH pending and already-approved unconsumed grants!
    broker.cancel_pending()
    assert broker.get_proposal(scope_p.id).state == "revoked"
    assert broker.get_proposal(plan_p.id).state == "revoked"
    assert broker.get_proposal(cmd_p.id).state == "revoked"

    before_calc_sha = sha256_bytes((repo / "calc.py").read_bytes())
    with pytest.raises(ProjectError) as revoked_w:
        broker.verify_write_allowed(
            path="calc.py",
            operation="modify",
            current_run_id="run-b11",
            current_root_identity=ws.root_identity,
            current_instruction_sha256=inst1.sha256,
        )
    assert revoked_w.value.code == "PROPOSAL_CANCELLED"

    async def _attempt_revoked_cmd() -> None:
        with pytest.raises(ProjectError) as revoked_c:
            await runner.run_approved_command(
                proposal_id=cmd_p.id,
                proposal_digest=cmd_p.digest,
                current_instruction_sha256=inst1.sha256,
            )
        assert revoked_c.value.code == "PROPOSAL_CANCELLED"

    asyncio.run(_attempt_revoked_cmd())
    assert spawn_calls == []
    assert sha256_bytes((repo / "calc.py").read_bytes()) == before_calc_sha

    # AGENTS.md modification invalidates previously approved grant before execution
    broker2 = PermissionBroker(
        run_id="run-b11-b",
        project_root=repo,
        root_identity=ws.root_identity,
        instruction_sha256=inst1.sha256,
        ttl_seconds=120.0,
    )
    runner2 = ProjectCommandRunner(
        run_id="run-b11-b",
        project_root=repo,
        data_root=data_root,
        broker=broker2,
    )
    plan_p2 = broker2.create_verification_proposal(
        plan=VerificationPlan(
            plan_id="plan-b11-2",
            summary="check",
            checks=[
                VerificationCheck(
                    check_id="u1",
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
        current_instruction_sha256=inst1.sha256,
    )
    broker2.decide(
        proposal_id=plan_p2.id,
        request=ProposalDecision(digest=plan_p2.digest, decision="approve"),
    )
    cmd_p2 = broker2.create_command_proposal(
        command=CommandProposal(
            command_id="cmd-inst-tamper",
            executable=sys.executable,
            argv=["-B", "-c", "print('ok')"],
            cwd=".",
            timeout_ms=5000,
            purpose="diagnostic",
            explanation="print ok",
        ),
        current_instruction_sha256=inst1.sha256,
    )
    broker2.decide(
        proposal_id=cmd_p2.id,
        request=ProposalDecision(
            digest=cmd_p2.digest, decision="approve", trusted_local_execution=True
        ),
    )
    agents_file.write_text("# v2 modified!\n", encoding="utf-8")
    inst2 = ws.load_instructions()
    assert inst2.sha256 != inst1.sha256

    async def _attempt_tampered_inst() -> None:
        with pytest.raises(ProjectError) as stale_exc:
            await runner2.run_approved_command(
                proposal_id=cmd_p2.id,
                proposal_digest=cmd_p2.digest,
                current_instruction_sha256=inst2.sha256,
            )
        assert stale_exc.value.status_code == 409
        assert stale_exc.value.code == "INSTRUCTIONS_CHANGED"

    asyncio.run(_attempt_tampered_inst())
    assert spawn_calls == []

    # Q03: verify_project reloads disk AGENTS.md & protected files AFTER wait_for_decision before spawn
    from forecontract.models.actions import ActionSpec
    from forecontract.models.outcomes import ExecutionStatus
    from forecontract_app.project_adapter import ProjectCodingAdapter
    from forecontract_app.project_models import ProjectTask

    inst_q03 = ws.load_instructions()
    broker3 = PermissionBroker(
        run_id="run-b11-q03",
        project_root=repo,
        root_identity=ws.root_identity,
        instruction_sha256=inst_q03.sha256,
        ttl_seconds=120.0,
    )
    runner3 = ProjectCommandRunner(
        run_id="run-b11-q03",
        project_root=repo,
        data_root=data_root,
        broker=broker3,
    )
    task3 = ProjectTask(
        run_id="run-b11-q03",
        goal_id="g-q03",
        goal_text="Q03 reload during decision",
        project_id="p-q03",
        root_identity=ws.root_identity,
        read_grant_id="rg-q03",
        data_root=data_root,
        output_root=tmp_path / "out-q03",
        python_executable=Path(sys.executable),
    )
    adapter3 = ProjectCodingAdapter(
        task=task3, workspace=ws, broker=broker3, command_runner=runner3
    )

    async def _run_q03() -> None:
        await adapter3.start()

        async def _approve_next(*, trust: bool = False) -> None:
            for _ in range(500):
                if broker3.pending_proposals():
                    break
                await asyncio.sleep(0.01)
            else:
                raise TimeoutError("timed out waiting for pending proposal in test_b11 Q03")
            for p in broker3.pending_proposals():
                broker3.decide(
                    proposal_id=p.id,
                    request=ProposalDecision(
                        digest=p.digest, decision="approve", trusted_local_execution=trust
                    ),
                )

        t_scope = asyncio.create_task(_approve_next())
        r_q03_scope = await adapter3.execute(
            ActionSpec(
                schema_version="0.1",
                action_type="propose_scope",
                payload={
                    "exact_paths": ["calc.py"],
                    "operations": ["modify"],
                    "protected_paths": ["tests/test_calc.py"],
                    "verification_plan_summary": "run u1",
                },
                metadata={"action_id": "q03-scope"},
            )
        )
        assert r_q03_scope.status == ExecutionStatus.SUCCEEDED
        await t_scope

        t_plan = asyncio.create_task(_approve_next())
        r_q03_plan = await adapter3.execute(
            ActionSpec(
                schema_version="0.1",
                action_type="propose_verification",
                payload={
                    "plan_id": "plan-q03",
                    "summary": "run u1",
                    "checks": [
                        {
                            "check_id": "u1",
                            "kind": "exit_code",
                            "executable": sys.executable,
                            "argv": ["-B", "-c", "print('q03')"],
                            "cwd": ".",
                            "timeout_ms": 5000,
                            "protected_paths": ["tests/test_calc.py"],
                        }
                    ],
                },
                metadata={"action_id": "q03-plan"},
            )
        )
        assert r_q03_plan.status == ExecutionStatus.SUCCEEDED
        await t_plan

        p_view3, _, _ = broker3.verify_plan_approved()

        async def _tamper_then_approve() -> None:
            for _ in range(500):
                if broker3.pending_proposals():
                    break
                await asyncio.sleep(0.01)
            else:
                raise TimeoutError("timed out waiting for pending command proposal in test_b11 Q03")
            # Mutate AGENTS.md AFTER CommandProposal was created and BEFORE spawn
            agents_file.write_text("# v3 modified while awaiting approval!\n", encoding="utf-8")
            for p in broker3.pending_proposals():
                broker3.decide(
                    proposal_id=p.id,
                    request=ProposalDecision(
                        digest=p.digest, decision="approve", trusted_local_execution=True
                    ),
                )

        t_cmd = asyncio.create_task(_tamper_then_approve())
        res_vp = await adapter3.execute(
            ActionSpec(
                schema_version="0.1",
                action_type="verify_project",
                payload={"plan_id": "plan-q03", "plan_digest": p_view3.digest},
                metadata={"action_id": "q03-vp"},
            )
        )
        await t_cmd
        assert res_vp.status == ExecutionStatus.FAILED
        assert res_vp.error is not None
        assert res_vp.error.code == "INSTRUCTIONS_CHANGED"

    asyncio.run(_run_q03())
    assert spawn_calls == []


def test_b12_scope_prefix_exe_tamper_env_forbidden_and_single_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = create_buggy_calc_project(tmp_path / "repo")
    (repo / "src").mkdir()
    (repo / "src2").mkdir()
    (repo / "src" / "a.py").write_text("A = 1\n", encoding="utf-8")
    (repo / "src2" / "b.py").write_text("B = 1\n", encoding="utf-8")
    data_root = tmp_path / "data"
    data_root.mkdir()

    ws = ProjectWorkspace(root=repo, data_root=data_root)
    broker = PermissionBroker(
        run_id="run-b12",
        project_root=repo,
        root_identity=ws.root_identity,
        ttl_seconds=120.0,
    )

    # Prefix boundary: src/ allows src/a.py, NOT src2/b.py
    scope_p = broker.create_scope_proposal(
        scope=ScopeProposalPayload(
            write_prefixes=["src/"],
            operations=["create", "modify"],
            protected_paths=[],
            verification_plan_summary="src fixes",
        ),
        current_instruction_sha256="",
    )
    broker.decide(
        proposal_id=scope_p.id,
        request=ProposalDecision(digest=scope_p.digest, decision="approve"),
    )
    plan_b12 = broker.create_verification_proposal(
        plan=VerificationPlan(
            plan_id="plan-b12",
            summary="verify calc",
            checks=[
                VerificationCheck(
                    check_id="u1",
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
        proposal_id=plan_b12.id,
        request=ProposalDecision(digest=plan_b12.digest, decision="approve"),
    )
    broker.verify_write_allowed(path="src/a.py", operation="modify")
    with pytest.raises(ProjectError):
        broker.verify_write_allowed(path="src2/b.py", operation="modify")

    # env / env_keys strictly forbidden in CommandProposal
    for bad_field in ("env", "env_keys"):
        with pytest.raises(ValidationError):
            CommandProposal.model_validate(
                {
                    "executable": sys.executable,
                    "argv": ["-V"],
                    "cwd": ".",
                    "timeout_ms": 5000,
                    "purpose": "diagnostic",
                    "explanation": "check version",
                    bad_field: {"FOO": "BAR"} if bad_field == "env" else ["FOO"],
                }
            )

    # Relative exe and cmd.exe shell rejected
    with pytest.raises(ProjectError):
        broker.create_command_proposal(
            command=CommandProposal(
                executable="python.exe",
                argv=["-V"],
                cwd=".",
                timeout_ms=5000,
                purpose="diagnostic",
                explanation="rel exe",
            ),
            current_instruction_sha256="",
        )
    with pytest.raises(ProjectError):
        broker.create_command_proposal(
            command=CommandProposal(
                executable=r"C:\Windows\System32\cmd.exe",
                argv=["/c", "echo", "hi"],
                cwd=".",
                timeout_ms=5000,
                purpose="diagnostic",
                explanation="shell forbidden",
            ),
            current_instruction_sha256="",
        )

    # Missing trusted_local_execution returns 403
    runner = ProjectCommandRunner(run_id="run-b12", project_root=repo, data_root=data_root, broker=broker)
    trust_test_p = broker.create_command_proposal(
        command=CommandProposal(
            command_id="cmd-trust-check",
            executable=sys.executable,
            argv=["-B", "-c", "print('single')"],
            cwd=".",
            timeout_ms=5000,
            purpose="diagnostic",
            explanation="single use",
        ),
        current_instruction_sha256="",
    )
    with pytest.raises(ProjectError) as no_trust:
        broker.decide(
            proposal_id=trust_test_p.id,
            request=ProposalDecision(
                digest=trust_test_p.digest, decision="approve", trusted_local_execution=False
            ),
        )
    assert no_trust.value.status_code == 403
    assert no_trust.value.code == "TRUSTED_LOCAL_EXECUTION_REQUIRED"

    broker.decide(
        proposal_id=trust_test_p.id,
        request=ProposalDecision(
            digest=trust_test_p.digest, decision="approve", trusted_local_execution=True
        ),
    )
    import forecontract_app.project_commands as pc

    spawn_count = 0
    orig_run_job = pc.run_command_in_job

    def _counting_spawn(**kwargs: Any) -> Any:
        nonlocal spawn_count
        spawn_count += 1
        return orig_run_job(**kwargs)

    monkeypatch.setattr(pc, "run_command_in_job", _counting_spawn)

    async def _double_consume() -> None:
        v1 = await runner.run_approved_command(
            proposal_id=trust_test_p.id,
            proposal_digest=trust_test_p.digest,
        )
        assert v1.exit_code == 0
        with pytest.raises(ProjectError) as reuse:
            await runner.run_approved_command(
                proposal_id=trust_test_p.id,
                proposal_digest=trust_test_p.digest,
            )
        assert reuse.value.status_code == 409
        assert reuse.value.code == "GRANT_ALREADY_USED"

    asyncio.run(_double_consume())
    assert spawn_count == 1

    # Exe modified on disk between approval and spawn -> 409 EXECUTABLE_MODIFIED, spawn_count unchanged
    fake_exe_dir = tmp_path / "local_bin"
    fake_exe_dir.mkdir()
    copied_exe = fake_exe_dir / Path(sys.executable).name
    shutil.copy2(sys.executable, copied_exe)

    tamper_p = broker.create_command_proposal(
        command=CommandProposal(
            command_id="cmd-exe-tamper",
            executable=str(copied_exe),
            argv=["-V"],
            cwd=".",
            timeout_ms=5000,
            purpose="diagnostic",
            explanation="exe tamper check",
        ),
        current_instruction_sha256="",
    )
    broker.decide(
        proposal_id=tamper_p.id,
        request=ProposalDecision(
            digest=tamper_p.digest, decision="approve", trusted_local_execution=True
        ),
    )
    # Overwrite copied binary bytes on disk
    copied_exe.write_bytes(copied_exe.read_bytes() + b"\x00TAMPERED")

    async def _run_tampered_exe() -> None:
        with pytest.raises(ProjectError) as exe_mod:
            await runner.run_approved_command(
                proposal_id=tamper_p.id,
                proposal_digest=tamper_p.digest,
            )
        assert exe_mod.value.status_code == 409
        assert exe_mod.value.code == "EXECUTABLE_MODIFIED"

    asyncio.run(_run_tampered_exe())
    assert spawn_count == 1

    # Revoking the VerificationPlan blocks any approved CommandProposal before spawn
    cmd_rev_plan = broker.create_command_proposal(
        command=CommandProposal(
            command_id="cmd-after-plan-revoke",
            executable=sys.executable,
            argv=["-V"],
            cwd=".",
            timeout_ms=5000,
            purpose="diagnostic",
            explanation="blocked by plan revoke",
        ),
        current_instruction_sha256="",
    )
    broker.decide(
        proposal_id=cmd_rev_plan.id,
        request=ProposalDecision(
            digest=cmd_rev_plan.digest, decision="approve", trusted_local_execution=True
        ),
    )
    broker.cancel_pending()

    async def _run_revoked_plan_cmd() -> None:
        with pytest.raises(ProjectError) as rev_exc:
            await runner.run_approved_command(
                proposal_id=cmd_rev_plan.id,
                proposal_digest=cmd_rev_plan.digest,
            )
        assert rev_exc.value.code in {"PROPOSAL_CANCELLED", "VERIFICATION_PLAN_REQUIRED"}

    asyncio.run(_run_revoked_plan_cmd())
    assert spawn_count == 1


def test_b13_verification_plan_validation_protection_and_per_check_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = create_buggy_calc_project(tmp_path / "repo")
    (repo / "src").mkdir()
    (repo / "src" / "helper.py").write_text("X = 1\n", encoding="utf-8")
    (repo / "src" / "protected_check.py").write_text("import sys; sys.exit(0)\n", encoding="utf-8")
    data_root = tmp_path / "data"
    data_root.mkdir()

    # Empty checks or duplicate check_id or duplicate expected test rejected
    with pytest.raises(ValidationError):
        VerificationPlan(plan_id="p-empty", summary="bad", checks=[])
    with pytest.raises(ValidationError):
        VerificationPlan(
            plan_id="p-dup",
            summary="dup",
            checks=[
                VerificationCheck(
                    check_id="c1", kind="exit_code", executable=sys.executable, argv=["-V"], cwd="."
                ),
                VerificationCheck(
                    check_id="c1", kind="exit_code", executable=sys.executable, argv=["-V"], cwd="."
                ),
            ],
        )
    with pytest.raises(ValidationError):
        VerificationCheck(
            check_id="j1",
            kind="junit_exact",
            executable=sys.executable,
            argv=["-B", "-m", "pytest"],
            cwd=".",
            expected_tests=[
                ExpectedTestCaseView(classname="tests.test_calc", name="test_add_basic"),
                ExpectedTestCaseView(classname="tests.test_calc", name="test_add_basic"),
            ],
        )

    ws = ProjectWorkspace(root=repo, data_root=data_root)
    broker = PermissionBroker(run_id="run-b13", project_root=repo, root_identity=ws.root_identity)

    scope_p = broker.create_scope_proposal(
        scope=ScopeProposalPayload(
            write_prefixes=["src/"],
            operations=["modify"],
            protected_paths=[],
            verification_plan_summary="edit src/helper.py",
        ),
        current_instruction_sha256="",
    )
    broker.decide(
        proposal_id=scope_p.id,
        request=ProposalDecision(digest=scope_p.digest, decision="approve"),
    )

    plan = VerificationPlan(
        plan_id="plan-1",
        summary="run junit and protected build helper",
        checks=[
            VerificationCheck(
                check_id="unit",
                kind="junit_exact",
                executable=sys.executable,
                argv=["-B", "-m", "pytest", "-p", "no:cacheprovider", "--junitxml={result_dir}/u.xml", "tests/test_calc.py"],
                cwd=".",
                timeout_ms=5000,
                protected_paths=["tests/test_calc.py"],
                expected_tests=[ExpectedTestCaseView(classname="tests.test_calc", name="test_add_basic")],
            ),
            VerificationCheck(
                check_id="build",
                kind="exit_code",
                executable=sys.executable,
                argv=["-B", "src/protected_check.py"],
                cwd=".",
                timeout_ms=5000,
                protected_paths=["src/protected_check.py"],
            ),
        ],
    )
    prot_shas = ws.hash_protected_paths(["tests/test_calc.py", "src/protected_check.py"])
    plan_p = broker.create_verification_proposal(
        plan=plan, protected_shas=prot_shas, current_instruction_sha256=""
    )
    broker.decide(
        proposal_id=plan_p.id,
        request=ProposalDecision(digest=plan_p.digest, decision="approve"),
    )

    # Protected check script inside write_prefixes=["src/"] MUST STILL be blocked from writing!
    ws.set_extra_protected_paths(broker.protected_paths())
    with pytest.raises(ProjectError) as prot_w:
        broker.verify_write_allowed(path="src/protected_check.py", operation="modify")
    assert prot_w.value.code == "PROTECTED_PATH_FORBIDDEN"
    with pytest.raises(ProjectError) as prot_ws:
        ws.edit_file(
            path="src/protected_check.py",
            expected_sha256=prot_shas["src/protected_check.py"],
            old_text="sys.exit(0)",
            new_text="sys.exit(1)",
        )
    assert prot_ws.value.code == "PROTECTED_PATH_FORBIDDEN"

    # Approving VerificationPlan does NOT auto-approve any CommandProposal
    runner = ProjectCommandRunner(run_id="run-b13", project_root=repo, data_root=data_root, broker=broker)
    cmd_prop = broker.create_command_proposal(
        command=CommandProposal(
            command_id="unit-1",
            executable=sys.executable,
            argv=plan.checks[0].argv,
            cwd=".",
            timeout_ms=5000,
            purpose="verification",
            explanation="unit check",
            check_id="unit",
        ),
        plan_id=plan_p.id,
        plan_digest=plan_p.digest,
        current_instruction_sha256="",
    )
    assert cmd_prop.state == "pending"

    async def _unapproved_fails() -> None:
        with pytest.raises(ProjectError) as req_exc:
            await runner.run_approved_command(
                proposal_id=cmd_prop.id,
                proposal_digest=cmd_prop.digest,
            )
        assert req_exc.value.code == "GRANT_REQUIRED"

    asyncio.run(_unapproved_fails())

    # Q02: start_command (including diagnostic) requires active approved VerificationPlan at Core contract & runner boundaries
    import forecontract_app.project_commands as pc
    from forecontract.models.actions import (
        ActionSpec,
        ExpectedOutcome,
        Forecast,
        Precondition,
    )
    from forecontract.models.observations import Observation
    from forecontract.models.outcomes import ExecutionStatus, VerificationStatus
    from forecontract.verification.models import VerificationEvidence
    from forecontract_app.project_adapter import ProjectCodingAdapter
    from forecontract_app.project_models import ProjectTask
    from forecontract_app.project_verification import (
        ProjectContractValidationHooks,
        ProjectDomainSupport,
    )

    q02_spawns: list[Any] = []
    orig_q02_job = pc.run_command_in_job

    def _spy_q02_job(**kwargs: Any) -> Any:
        q02_spawns.append(kwargs)
        return orig_q02_job(**kwargs)

    monkeypatch.setattr(pc, "run_command_in_job", _spy_q02_job)

    q02_clock = _FakeClock(start=0.0)
    broker_q02 = PermissionBroker(
        run_id="run-b13-q02",
        project_root=repo,
        root_identity=ws.root_identity,
        ttl_seconds=20.0,
        clock=q02_clock,
    )
    runner_q02 = ProjectCommandRunner(
        run_id="run-b13-q02", project_root=repo, data_root=data_root, broker=broker_q02
    )
    task_q02 = ProjectTask(
        run_id="run-b13-q02",
        goal_id="g-q02",
        goal_text="Q02 plan gate",
        project_id="p-q02",
        root_identity=ws.root_identity,
        read_grant_id="rg-q02",
        data_root=data_root,
        output_root=tmp_path / "out-q02",
        python_executable=Path(sys.executable),
    )
    adapter_q02 = ProjectCodingAdapter(
        task=task_q02, workspace=ws, broker=broker_q02, command_runner=runner_q02
    )

    q02_obs_no_plan = Observation(
        schema_version="0.1",
        observation_id="obs-q02-0",
        environment_type="local_project_workspace",
        timestamp="2026-11-15T00:00:00Z",
        state={"scope_ok": True, "plan_approved": False},
    )
    q02_obs_plan = q02_obs_no_plan.model_copy(
        update={
            "observation_id": "obs-q02-1",
            "state": {"scope_ok": True, "plan_approved": True},
        }
    )
    q02_forecast = Forecast(
        schema_version="0.1",
        forecast_id="fc-q02",
        proposed_action=ActionSpec(
            schema_version="0.1",
            action_type="start_command",
            payload={"proposal_id": "cmd-x", "proposal_digest": "a" * 64},
        ),
        expected_outcomes=[
            ExpectedOutcome(
                schema_version="0.1",
                outcome_type="project_tool_result",
                payload={"action_type": "start_command", "require_success": False},
                required=True,
            )
        ],
    )
    domain_support = ProjectDomainSupport(task=task_q02)
    contract = domain_support.prepare_contract(
        forecast=q02_forecast,
        evidence=VerificationEvidence(
            schema_version="0.1",
            observation=q02_obs_no_plan,
            state_complete=True,
        ),
    )
    plan_conds = [c for c in contract.preconditions if c.condition_type == "project_plan_approved"]
    assert len(plan_conds) == 1
    assert isinstance(plan_conds[0], Precondition)
    hooks = ProjectContractValidationHooks(state_complete=True)
    assert hooks.check_precondition(plan_conds[0], q02_obs_no_plan) == VerificationStatus.FAIL
    assert hooks.check_precondition(plan_conds[0], q02_obs_plan) == VerificationStatus.PASS

    async def _run_q02() -> None:
        await adapter_q02.start()

        async def _approve_next(*, trust: bool = False) -> None:
            for _ in range(500):
                if broker_q02.pending_proposals():
                    break
                await asyncio.sleep(0.01)
            else:
                raise TimeoutError("timed out waiting for pending proposal in test_b13 Q02")
            for p in broker_q02.pending_proposals():
                broker_q02.decide(
                    proposal_id=p.id,
                    request=ProposalDecision(
                        digest=p.digest, decision="approve", trusted_local_execution=trust
                    ),
                )

        # Diagnostic CommandProposal approved WITHOUT any VerificationPlan -> start_command MUST reject before spawn
        t_cmd1 = asyncio.create_task(_approve_next(trust=True))
        r_cmd1 = await adapter_q02.execute(
            ActionSpec(
                schema_version="0.1",
                action_type="propose_command",
                payload={
                    "command_id": "diag-no-plan",
                    "executable": sys.executable,
                    "argv": ["-B", "-c", "print('diag')"],
                    "cwd": ".",
                    "timeout_ms": 5000,
                    "purpose": "diagnostic",
                    "explanation": "diag without plan",
                },
                metadata={"action_id": "q02-cmd1"},
            )
        )
        await t_cmd1
        assert r_cmd1.status == ExecutionStatus.SUCCEEDED
        r_start_no_plan = await adapter_q02.execute(
            ActionSpec(
                schema_version="0.1",
                action_type="start_command",
                payload={
                    "proposal_id": r_cmd1.changes["proposal_id"],
                    "proposal_digest": r_cmd1.changes["digest"],
                },
                metadata={"action_id": "q02-start1"},
            )
        )
        assert r_start_no_plan.status == ExecutionStatus.FAILED
        assert r_start_no_plan.error is not None
        assert r_start_no_plan.error.code == "VERIFICATION_PLAN_REQUIRED"
        assert len(q02_spawns) == 0

        # Expired VerificationPlan ALSO blocks start_command before spawn
        t_plan_exp = asyncio.create_task(_approve_next())
        r_plan_exp = await adapter_q02.execute(
            ActionSpec(
                schema_version="0.1",
                action_type="propose_verification",
                payload={
                    "plan_id": "plan-q02-exp",
                    "summary": "expiring plan",
                    "checks": [
                        {
                            "check_id": "u1",
                            "kind": "exit_code",
                            "executable": sys.executable,
                            "argv": ["-B", "-c", "print('ok')"],
                            "cwd": ".",
                            "timeout_ms": 5000,
                        }
                    ],
                },
                metadata={"action_id": "q02-plan-exp"},
            )
        )
        await t_plan_exp
        assert r_plan_exp.status == ExecutionStatus.SUCCEEDED
        q02_clock.now += 15.0
        t_cmd_exp = asyncio.create_task(_approve_next(trust=True))
        r_cmd_exp = await adapter_q02.execute(
            ActionSpec(
                schema_version="0.1",
                action_type="propose_command",
                payload={
                    "command_id": "diag-exp-plan",
                    "executable": sys.executable,
                    "argv": ["-B", "-c", "print('diag-exp')"],
                    "cwd": ".",
                    "timeout_ms": 5000,
                    "purpose": "diagnostic",
                    "explanation": "diag when plan expires",
                },
                metadata={"action_id": "q02-cmd-exp"},
            )
        )
        await t_cmd_exp
        assert r_cmd_exp.status == ExecutionStatus.SUCCEEDED
        q02_clock.now += 6.0  # t=21: plan expired at 20, cmd grant still active until 35
        r_start_exp = await adapter_q02.execute(
            ActionSpec(
                schema_version="0.1",
                action_type="start_command",
                payload={
                    "proposal_id": r_cmd_exp.changes["proposal_id"],
                    "proposal_digest": r_cmd_exp.changes["digest"],
                },
                metadata={"action_id": "q02-start-exp"},
            )
        )
        assert r_start_exp.status == ExecutionStatus.FAILED
        assert r_start_exp.error is not None
        assert r_start_exp.error.code == "PROPOSAL_EXPIRED"
        assert len(q02_spawns) == 0

        # Active approved VerificationPlan + active approved diagnostic command succeeds
        t_plan_ok = asyncio.create_task(_approve_next())
        r_plan_ok = await adapter_q02.execute(
            ActionSpec(
                schema_version="0.1",
                action_type="propose_verification",
                payload={
                    "plan_id": "plan-q02-ok",
                    "summary": "active plan",
                    "checks": [
                        {
                            "check_id": "u1",
                            "kind": "exit_code",
                            "executable": sys.executable,
                            "argv": ["-B", "-c", "print('ok')"],
                            "cwd": ".",
                            "timeout_ms": 5000,
                        }
                    ],
                },
                metadata={"action_id": "q02-plan-ok"},
            )
        )
        await t_plan_ok
        assert r_plan_ok.status == ExecutionStatus.SUCCEEDED
        t_cmd2 = asyncio.create_task(_approve_next(trust=True))
        r_cmd2 = await adapter_q02.execute(
            ActionSpec(
                schema_version="0.1",
                action_type="propose_command",
                payload={
                    "command_id": "diag-with-plan",
                    "executable": sys.executable,
                    "argv": ["-B", "-c", "print('diag-ok')"],
                    "cwd": ".",
                    "timeout_ms": 5000,
                    "purpose": "diagnostic",
                    "explanation": "diag with plan",
                },
                metadata={"action_id": "q02-cmd2"},
            )
        )
        await t_cmd2
        assert r_cmd2.status == ExecutionStatus.SUCCEEDED
        r_start_ok = await adapter_q02.execute(
            ActionSpec(
                schema_version="0.1",
                action_type="start_command",
                payload={
                    "proposal_id": r_cmd2.changes["proposal_id"],
                    "proposal_digest": r_cmd2.changes["digest"],
                },
                metadata={"action_id": "q02-start-ok"},
            )
        )
        assert r_start_ok.status == ExecutionStatus.SUCCEEDED
        waited_ok = await runner_q02.wait_command("diag-with-plan")
        assert waited_ok.exit_code == 0
        poll_ok = await adapter_q02.execute(
            ActionSpec(
                schema_version="0.1",
                action_type="poll_command",
                payload={"command_id": "diag-with-plan"},
                metadata={"action_id": "q02-poll-ok"},
            )
        )
        assert poll_ok.status == ExecutionStatus.SUCCEEDED
        assert poll_ok.changes["exit_code"] == 0

    asyncio.run(_run_q02())
    assert len(q02_spawns) == 1
