"""JUnit XML verifier, pure project completion oracle, and Core domain protocol implementations."""

from __future__ import annotations

import math
import os
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Final

from forecontract.contracts.models import (
    ActionContract,
    RecoveryPolicy,
    VerificationBinding,
    VerificationPolicy,
)
from forecontract.contracts.validation import ContractValidationHooks
from forecontract.models.actions import (
    ActionSpec,
    ExpectedOutcome,
    Forecast,
    Precondition,
)
from forecontract.models.observations import Observation
from forecontract.models.outcomes import (
    ActualOutcome,
    ExecutionStatus,
    OutcomeVerification,
    VerificationResult,
    VerificationStatus,
)
from forecontract.protocols.adapter import InvalidActionPayloadError
from forecontract.recovery.models import (
    EFFECT_APPLIED,
    EFFECT_NOT_APPLIED,
    EFFECT_UNKNOWN,
    SAFETY_NON_IDEMPOTENT,
    SAFETY_READ_ONLY,
    ActionSafetyDeclaration,
)
from forecontract.runtime.models import ContractPreparation
from forecontract.verification.errors import VerificationError
from forecontract.verification.models import (
    TaskCompletionCriteria,
    VerificationEvidence,
)

from forecontract_app.project_models import (
    PROJECT_SCHEMA_VERSION,
    SUPPORTED_PROJECT_ACTIONS,
    CheckExecutionRecord,
    ExpectedTestCaseView,
    ProjectError,
    ProjectRunResult,
    ProjectTask,
    ProjectVerificationEvidence,
)
from forecontract_app.security import is_reparse_or_symlink

PROJECT_ADAPTER_NAME: Final[str] = "project_coding_adapter"
PROJECT_ENVIRONMENT_TYPE: Final[str] = "local_project_workspace"
PROJECT_VERIFIER_TYPE: Final[str] = "project_verifier"
PROJECT_STEP_VERIFIER: Final[str] = "project_step_verifier"
PROJECT_TASK_ORACLE: Final[str] = "project_task_completion_oracle"

SUPPORTED_PROJECT_CONDITIONS: Final[frozenset[str]] = frozenset(
    {
        "project_scope_valid",
        "project_write_grant_valid",
        "project_plan_approved",
        "project_command_grant_valid",
    }
)

SUPPORTED_PROJECT_OUTCOMES: Final[frozenset[str]] = frozenset(
    {
        "project_tool_result",
        "project_file_written",
        "project_command_result",
        "project_task_verified",
    }
)

SUPPORTED_EXTENSION_ACTIONS: Final[tuple[str, ...]] = (
    "list_skills",
    "read_skill",
    "list_mcp_servers",
    "connect_mcp",
    "list_mcp_tools",
    "call_mcp",
)

_READ_ONLY_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "list_directory",
        "find_paths",
        "read_file",
        "search_text",
        "poll_command",
        "list_skills",
        "read_skill",
        "list_mcp_servers",
        "list_mcp_tools",
    }
)

_MAX_JUNIT_XML_BYTES: Final[int] = 4 * 1024 * 1024


def parse_junit_xml_in_result_dir(
    *,
    result_dir: Path,
    expected_tests: list[ExpectedTestCaseView],
) -> tuple[bool, list[ExpectedTestCaseView], str | None, str | None]:
    """Safely parse isolated ``{result_dir}/*.xml`` JUnit report and match exact expected set."""
    if not result_dir.exists() or not result_dir.is_dir() or is_reparse_or_symlink(result_dir):
        return False, [], None, "junit_result_dir_invalid"
    xml_files = sorted(
        [
            p
            for p in result_dir.iterdir()
            if p.suffix.lower() == ".xml" and not is_reparse_or_symlink(p) and p.is_file()
        ],
        key=lambda p: p.name.casefold(),
    )
    if len(xml_files) != 1:
        return False, [], None, "junit_xml_missing_or_multiple"

    xml_path = xml_files[0]
    try:
        st = xml_path.stat(follow_symlinks=False)
        if st.st_size > _MAX_JUNIT_XML_BYTES:
            return False, [], str(xml_path), "junit_xml_too_large"
        raw = xml_path.read_bytes()
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return False, [], str(xml_path), "junit_xml_unreadable"

    upper_head = text[:4096].upper()
    if "<!DOCTYPE" in upper_head or "<!ENTITY" in upper_head:
        return False, [], str(xml_path), "junit_xml_forbidden_dtd"

    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return False, [], str(xml_path), "junit_xml_parse_error"

    testcases: list[ET.Element] = list(root.iter("testcase"))
    if not testcases:
        return False, [], str(xml_path), "junit_empty_testcases"

    observed: list[ExpectedTestCaseView] = []
    seen: set[tuple[str, str]] = set()
    has_bad_status = False
    for tc in testcases:
        cls_name = (tc.attrib.get("classname") or "").strip()
        case_name = (tc.attrib.get("name") or "").strip()
        if not cls_name or not case_name:
            return False, observed, str(xml_path), "junit_missing_testcase_identity"
        if (
            tc.find("failure") is not None
            or tc.find("error") is not None
            or tc.find("skipped") is not None
        ):
            has_bad_status = True
        key = (cls_name, case_name)
        if key in seen:
            return False, observed, str(xml_path), "junit_duplicate_testcase"
        seen.add(key)
        observed.append(ExpectedTestCaseView(classname=cls_name, name=case_name))

    if has_bad_status:
        return False, observed, str(xml_path), "junit_testcase_failed_or_skipped"

    expected_keys = {(t.classname, t.name) for t in expected_tests}
    if seen != expected_keys:
        missing = expected_keys - seen
        extra = seen - expected_keys
        if missing:
            return False, observed, str(xml_path), f"junit_missing_expected:{sorted(missing)}"
        return False, observed, str(xml_path), f"junit_extra_unexpected:{sorted(extra)}"

    return True, observed, str(xml_path), None


def evaluate_project_result(
    *,
    task: ProjectTask,
    evidence: ProjectVerificationEvidence,
) -> ProjectRunResult:
    """Pure completion oracle evaluating plan completeness, check results, revisions, and cleanup."""
    _ = task
    reasons = list(evidence.unverified_reasons)
    cleanup_info = {
        "cleanup_ok": evidence.cleanup_ok,
        "remaining_active": evidence.remaining_active,
    }
    diff_obj = evidence.diff
    changed_list = list(evidence.changed_paths)

    def _finish(
        *,
        terminal_status: Any,
        verification: Any,
        reason_code: str,
        extra_reasons: list[str] | None = None,
        acc_status: str | None = None,
    ) -> ProjectRunResult:
        all_reasons = list(dict.fromkeys(reasons + (extra_reasons or [])))
        cmd_ids = [c.command_id for c in evidence.checks]
        checks_map: dict[str, CheckExecutionRecord] = {}
        for rec in evidence.checks:
            if rec.check_id not in checks_map:
                checks_map[rec.check_id] = rec
        return ProjectRunResult(
            terminal_status=terminal_status,
            verification=verification,
            reason_code=reason_code,
            scope_complete=evidence.scope_complete,
            plan_id=evidence.plan_id,
            plan_digest=evidence.plan_digest,
            baseline_revision=evidence.baseline_revision,
            final_revision=evidence.final_revision,
            checks=checks_map,
            changed_paths=changed_list,
            diff=diff_obj,
            diff_truncated=evidence.diff_truncated,
            unverified_reasons=all_reasons,
            cleanup=cleanup_info,
            core_terminal_status="completed" if evidence.core_completed else "incomplete",
            core_terminal_reason="TASK_ACHIEVED" if evidence.core_completed else "INCOMPLETE",
            acceptance={
                "status": acc_status or verification,
                "revision": evidence.final_revision,
                "command_ids": cmd_ids,
            },
        )

    if not evidence.cleanup_ok or evidence.remaining_active != 0:
        return _finish(
            terminal_status="unknown",
            verification="unknown",
            reason_code="CLEANUP_INCOMPLETE",
            extra_reasons=["active processes remaining or job cleanup failed"],
        )

    if not evidence.scope_complete:
        return _finish(
            terminal_status="failed",
            verification="unknown",
            reason_code="SCOPE_INCOMPLETE",
            extra_reasons=["workspace exceeded 10,000 files or 64 MiB revision budget"],
        )

    plan = evidence.approved_plan
    if plan is None or not plan.checks or not evidence.plan_id or not evidence.plan_digest:
        return _finish(
            terminal_status="failed",
            verification="not_run",
            reason_code="VERIFICATION_PLAN_MISSING",
            extra_reasons=["no approved non-empty verification plan"],
        )

    if evidence.stale_after_verification or (
        evidence.protected_shas_expected != evidence.protected_shas_final
    ):
        return _finish(
            terminal_status="failed",
            verification="unknown",
            reason_code="VERIFICATION_STALE",
            extra_reasons=["protected sources or workspace changed after verification"],
        )

    if not evidence.checks:
        return _finish(
            terminal_status="failed",
            verification="not_run",
            reason_code="VERIFICATION_NOT_RUN",
            extra_reasons=["no verification check records produced"],
        )

    by_id: dict[str, CheckExecutionRecord] = {}
    any_unknown = False
    any_failed = False
    check_reasons: list[str] = []
    for rec in evidence.checks:
        if rec.check_id in by_id:
            any_unknown = True
            check_reasons.append(f"duplicate check execution: {rec.check_id}")
        else:
            by_id[rec.check_id] = rec
    planned_ids = {planned.check_id for planned in plan.checks}
    extra_ids = sorted(set(by_id) - planned_ids)
    if extra_ids:
        any_unknown = True
        check_reasons.append(f"unexpected check records: {extra_ids}")

    for planned in plan.checks:
        rec = by_id.get(planned.check_id)
        if rec is None:
            any_unknown = True
            check_reasons.append(f"missing check execution: {planned.check_id}")
            continue
        if rec.kind != planned.kind or rec.executable != planned.executable or list(rec.argv) != list(planned.argv) or rec.cwd != planned.cwd:
            any_unknown = True
            check_reasons.append(f"check definition mismatch: {planned.check_id}")
            continue
        if not rec.cleanup_ok or rec.remaining_active != 0 or rec.output_delivery_failed:
            any_unknown = True
            check_reasons.append(f"check cleanup/output incomplete: {planned.check_id}")
            continue
        if rec.start_revision != rec.end_revision or rec.end_revision != evidence.final_revision:
            any_unknown = True
            check_reasons.append(f"check revision stale: {planned.check_id}")
            continue
        if rec.protected_shas_before != rec.protected_shas_after:
            any_unknown = True
            check_reasons.append(f"protected source changed during check: {planned.check_id}")
            continue
        if rec.timed_out or rec.cancelled:
            any_failed = True
            check_reasons.append(f"check interrupted: {planned.check_id}")
            continue
        if rec.exit_code != 0:
            any_failed = True
            check_reasons.append(f"check nonzero exit ({rec.exit_code}): {planned.check_id}")
            continue
        if planned.kind == "junit_exact" and (
            not rec.junit_valid
            or {(t.classname, t.name) for t in rec.observed_tests}
            != {(t.classname, t.name) for t in planned.expected_tests}
            or len(rec.observed_tests) != len(planned.expected_tests)
        ):
            any_failed = True
            check_reasons.append(rec.failure_reason or f"junit mismatch: {planned.check_id}")
            continue
        if not rec.passed:
            any_failed = True
            check_reasons.append(rec.failure_reason or f"check failed: {planned.check_id}")

    if any_unknown:
        return _finish(
            terminal_status="failed",
            verification="unknown",
            reason_code="VERIFICATION_STALE",
            extra_reasons=check_reasons,
        )
    if any_failed:
        return _finish(
            terminal_status="failed",
            verification="failed",
            reason_code="VERIFICATION_FAILED",
            extra_reasons=check_reasons,
        )
    if not evidence.core_completed:
        return _finish(
            terminal_status="failed",
            verification="unknown",
            reason_code="CORE_INCOMPLETE",
            extra_reasons=["Core RuntimeEngine did not reach completed state"],
        )

    return _finish(
        terminal_status="succeeded",
        verification="passed",
        reason_code="TASK_COMPLETED",
        acc_status="passed",
    )


def _observation_state_is_complete(obs: Observation) -> bool:
    if obs.environment_type != PROJECT_ENVIRONMENT_TYPE:
        return False
    state = obs.state
    if not isinstance(state, dict):
        return False
    if state.get("observation_overflow") is True:
        return False
    if state.get("state_complete") is False:
        return False
    if state.get("scope_ok") is not True:
        return False
    rev = state.get("workspace_revision")
    if not isinstance(rev, str) or not rev.strip():
        return False
    return isinstance(state.get("verification"), dict)


class ProjectDomainSupport:
    """Pure synchronous ``RuntimeDomainSupport`` reading only detached ``Observation`` & config."""

    def __init__(
        self,
        *,
        task: ProjectTask | None = None,
        allow_user_questions: bool = False,
        allow_extensions: bool = False,
    ) -> None:
        self._task = ProjectTask.model_validate(task) if task is not None else None
        self._allow_user_questions = bool(allow_user_questions)
        self._allow_extensions = bool(allow_extensions)

    def _is_action_supported(self, action_type: object) -> bool:
        if action_type in SUPPORTED_PROJECT_ACTIONS:
            return True
        if self._allow_user_questions and action_type == "ask_user":
            return True
        return bool(self._allow_extensions and action_type in SUPPORTED_EXTENSION_ACTIONS)

    def to_evidence(self, observation: Observation) -> VerificationEvidence:
        if not isinstance(observation, Observation):
            raise ProjectError("INVALID_INPUT", "Observation instance is required")
        rebuilt = Observation.model_validate(observation.model_dump(mode="python"))
        complete = _observation_state_is_complete(rebuilt)
        return VerificationEvidence(
            schema_version=PROJECT_SCHEMA_VERSION,
            observation=rebuilt,
            state_complete=complete,
        )

    def _step_timeout_ms(self, action: ActionSpec) -> float:
        run_cap = float(self._task.run_timeout_ms) if self._task is not None else 600_000.0
        ttl_ms = (
            float(self._task.proposal_ttl_seconds) * 1000.0 + 10_000.0
            if self._task is not None
            else 130_000.0
        )
        if action.action_type in (
            "propose_scope",
            "propose_verification",
            "propose_command",
            "ask_user",
        ):
            desired = ttl_ms
        elif action.action_type in ("connect_mcp", "call_mcp"):
            desired = max(ttl_ms + 30_000.0, 155_000.0)
        elif action.action_type == "verify_project":
            desired = max(ttl_ms, 300_000.0)
        elif action.action_type == "start_command":
            desired = 15_000.0
        else:
            desired = 10_000.0
        usable_cap = max(200.0, run_cap - max(250.0, run_cap * 0.1)) if run_cap > 250.0 else run_cap
        val = min(desired, usable_cap)
        if not math.isfinite(val) or val <= 0:
            return 1000.0
        return val

    def prepare_contract(
        self, *, forecast: Forecast, evidence: VerificationEvidence
    ) -> ContractPreparation:
        if not isinstance(forecast, Forecast) or not isinstance(evidence, VerificationEvidence):
            raise ProjectError("INVALID_INPUT", "Forecast and VerificationEvidence required")
        action = forecast.proposed_action
        if not self._is_action_supported(action.action_type):
            raise ProjectError("ACTION_INVALID", f"unsupported action_type: {action.action_type}")
        outcomes = list(forecast.expected_outcomes)
        if not outcomes or not any(o.required for o in outcomes):
            raise ProjectError("VERIFICATION_INVALID", "at least one required expected outcome is needed")
        for idx, outcome in enumerate(outcomes):
            if outcome.outcome_type not in SUPPORTED_PROJECT_OUTCOMES:
                raise ProjectError(
                    "VERIFICATION_INVALID",
                    f"unsupported outcome_type at {idx}: {outcome.outcome_type}",
                )
        bindings = [
            VerificationBinding(
                schema_version=PROJECT_SCHEMA_VERSION,
                outcome_index=idx,
                verifier_type=PROJECT_VERIFIER_TYPE,
                config={},
            )
            for idx in range(len(outcomes))
        ]
        preconditions: list[Precondition] = [
            Precondition(
                schema_version=PROJECT_SCHEMA_VERSION,
                condition_type="project_scope_valid",
                payload=(
                    {"root_identity": self._task.root_identity} if self._task is not None else {}
                ),
                required=True,
            )
        ]
        if action.action_type in ("create_file", "edit_file"):
            preconditions.append(
                Precondition(
                    schema_version=PROJECT_SCHEMA_VERSION,
                    condition_type="project_write_grant_valid",
                    payload={"path": str(action.payload.get("path", ""))},
                    required=True,
                )
            )
            preconditions.append(
                Precondition(
                    schema_version=PROJECT_SCHEMA_VERSION,
                    condition_type="project_plan_approved",
                    payload={},
                    required=True,
                )
            )
        elif action.action_type == "start_command":
            preconditions.append(
                Precondition(
                    schema_version=PROJECT_SCHEMA_VERSION,
                    condition_type="project_plan_approved",
                    payload={},
                    required=True,
                )
            )
            preconditions.append(
                Precondition(
                    schema_version=PROJECT_SCHEMA_VERSION,
                    condition_type="project_command_grant_valid",
                    payload={
                        "proposal_id": str(action.payload.get("proposal_id", "")),
                        "proposal_digest": str(action.payload.get("proposal_digest", "")),
                    },
                    required=True,
                )
            )
        elif action.action_type == "verify_project":
            preconditions.append(
                Precondition(
                    schema_version=PROJECT_SCHEMA_VERSION,
                    condition_type="project_plan_approved",
                    payload={
                        "plan_id": str(action.payload.get("plan_id", "")),
                        "plan_digest": str(action.payload.get("plan_digest", "")),
                    },
                    required=True,
                )
            )

        return ContractPreparation(
            schema_version=PROJECT_SCHEMA_VERSION,
            preconditions=preconditions,
            verification_policy=VerificationPolicy(
                schema_version=PROJECT_SCHEMA_VERSION,
                bindings=bindings,
            ),
            recovery_policy=RecoveryPolicy(
                schema_version=PROJECT_SCHEMA_VERSION,
                mode="bounded",
                max_retries=0,
                allow_replan=True,
            ),
            timeout_ms=self._step_timeout_ms(action),
        )

    def declare_action_safety(self, *, contract: object) -> ActionSafetyDeclaration:
        contract_id = getattr(contract, "contract_id", None)
        adapter_name = getattr(contract, "adapter_name", None)
        action = getattr(contract, "action", None)
        action_type = getattr(action, "action_type", None)
        if (
            not isinstance(contract_id, str)
            or not contract_id.strip()
            or adapter_name != PROJECT_ADAPTER_NAME
            or not isinstance(action_type, str)
            or not self._is_action_supported(action_type)
        ):
            raise ProjectError("INVALID_INPUT", "invalid contract for safety declaration")
        safety = (
            SAFETY_READ_ONLY
            if action_type in _READ_ONLY_ACTIONS
            else SAFETY_NON_IDEMPOTENT
        )
        return ActionSafetyDeclaration(
            schema_version=PROJECT_SCHEMA_VERSION,
            contract_id=contract_id,
            adapter_name=adapter_name,
            action_type=action_type,
            retry_safety=safety,
        )

    def assess_effect(
        self,
        *,
        contract: object,
        actual_outcome: ActualOutcome,
        evidence: VerificationEvidence,
    ) -> str:
        if not evidence.state_complete or actual_outcome.status is ExecutionStatus.UNKNOWN:
            return EFFECT_UNKNOWN
        action = getattr(contract, "action", None)
        action_type = getattr(action, "action_type", None)
        if actual_outcome.status is ExecutionStatus.FAILED:
            if action_type in _READ_ONLY_ACTIONS:
                return EFFECT_NOT_APPLIED
            data = actual_outcome.changes if isinstance(actual_outcome.changes, dict) else {}
            if data.get("effect_applied") is False:
                return EFFECT_NOT_APPLIED
            return EFFECT_UNKNOWN
        if actual_outcome.status is ExecutionStatus.SUCCEEDED:
            return EFFECT_APPLIED
        return EFFECT_UNKNOWN


class ProjectContractValidationHooks(ContractValidationHooks):
    """Per-boundary contract validation hooks reading only detached ``Observation`` evidence."""

    def __init__(self, *, state_complete: bool) -> None:
        self._state_complete = state_complete

    def validate_precondition(self, condition: Precondition) -> None:
        if not isinstance(condition, Precondition):
            raise InvalidActionPayloadError("Precondition required")
        rebuilt = Precondition.model_validate(condition.model_dump(mode="python"))
        if rebuilt.condition_type not in SUPPORTED_PROJECT_CONDITIONS:
            raise InvalidActionPayloadError("unsupported condition_type")

    def validate_expected_outcome(self, outcome: ExpectedOutcome) -> None:
        if not isinstance(outcome, ExpectedOutcome):
            raise InvalidActionPayloadError("ExpectedOutcome required")
        rebuilt = ExpectedOutcome.model_validate(outcome.model_dump(mode="python"))
        if rebuilt.outcome_type not in SUPPORTED_PROJECT_OUTCOMES:
            raise InvalidActionPayloadError("unsupported outcome_type")

    def validate_verification_binding(
        self, binding: VerificationBinding, outcome: ExpectedOutcome
    ) -> None:
        if not isinstance(binding, VerificationBinding):
            raise InvalidActionPayloadError("VerificationBinding required")
        if binding.verifier_type != PROJECT_VERIFIER_TYPE or len(binding.config) != 0:
            raise InvalidActionPayloadError("invalid verifier binding")
        self.validate_expected_outcome(outcome)

    def check_precondition(
        self, condition: Precondition, observation: Observation
    ) -> VerificationStatus:
        self.validate_precondition(condition)
        if observation.environment_type != PROJECT_ENVIRONMENT_TYPE:
            return VerificationStatus.FAIL
        if not self._state_complete:
            return VerificationStatus.INCONCLUSIVE
        state = observation.state
        if not isinstance(state, dict) or state.get("observation_overflow") is True:
            return VerificationStatus.INCONCLUSIVE

        ctype = condition.condition_type
        if ctype == "project_scope_valid":
            if state.get("scope_ok") is not True:
                return VerificationStatus.FAIL
            expected_root = condition.payload.get("root_identity")
            if expected_root and os.path.normcase(str(state.get("root_identity", ""))) != os.path.normcase(str(expected_root)):
                return VerificationStatus.FAIL
            return VerificationStatus.PASS

        if ctype == "project_write_grant_valid":
            ok = (
                state.get("scope_approved") is True
                and state.get("scope_complete") is True
                and state.get("instructions_valid") is True
            )
            return VerificationStatus.PASS if ok else VerificationStatus.FAIL

        if ctype == "project_plan_approved":
            if state.get("plan_approved") is not True:
                return VerificationStatus.FAIL
            req_id = condition.payload.get("plan_id")
            req_dig = condition.payload.get("plan_digest")
            if req_id and state.get("plan_id") != req_id:
                return VerificationStatus.FAIL
            if req_dig and state.get("plan_digest") != req_dig:
                return VerificationStatus.FAIL
            return VerificationStatus.PASS

        if ctype == "project_command_grant_valid":
            pid = str(condition.payload.get("proposal_id", ""))
            approved_ids = state.get("approved_command_ids", [])
            if not pid or not isinstance(approved_ids, list) or pid not in approved_ids:
                return VerificationStatus.FAIL
            return VerificationStatus.PASS

        return VerificationStatus.FAIL


class ProjectContractHooksFactory:
    def create(self, *, evidence: VerificationEvidence) -> ProjectContractValidationHooks:
        if not isinstance(evidence, VerificationEvidence):
            raise InvalidActionPayloadError("VerificationEvidence required")
        rebuilt = VerificationEvidence.model_validate(evidence.model_dump(mode="python"))
        if type(rebuilt.state_complete) is not bool:
            raise InvalidActionPayloadError("state_complete boolean required")
        return ProjectContractValidationHooks(state_complete=rebuilt.state_complete)


class ProjectTaskCriteriaValidator:
    def validate(self, *, criteria: TaskCompletionCriteria) -> None:
        if not isinstance(criteria, TaskCompletionCriteria):
            raise VerificationError(
                "VERIFICATION_INPUT_INVALID", ("criteria",), "TaskCompletionCriteria required"
            )
        rebuilt = TaskCompletionCriteria.model_validate(criteria.model_dump(mode="python"))
        if not rebuilt.conditions or not any(c.required for c in rebuilt.conditions):
            raise VerificationError(
                "VERIFICATION_INPUT_INVALID",
                ("criteria", "conditions"),
                "at least one required condition is needed",
            )
        for idx, cond in enumerate(rebuilt.conditions):
            if cond.outcome_type != "project_task_verified":
                raise VerificationError(
                    "VERIFICATION_INPUT_INVALID",
                    ("criteria", "conditions", idx, "outcome_type"),
                    "only project_task_verified allowed",
                )


def _evaluate_task_state(state: dict[str, Any]) -> tuple[VerificationStatus, str]:
    if state.get("scope_ok") is not True or state.get("scope_complete") is not True:
        return VerificationStatus.FAIL, "scope_incomplete_or_invalid"
    if state.get("plan_approved") is not True or not state.get("plan_id") or not state.get("plan_digest"):
        return VerificationStatus.FAIL, "verification_plan_not_approved"
    if int(state.get("remaining_active_processes", 0)) != 0 or state.get("cleanup_ok") is not True:
        return VerificationStatus.INCONCLUSIVE, "cleanup_incomplete"
    live_rev = state.get("workspace_revision")
    ver = state.get("verification")
    if not isinstance(ver, dict) or not isinstance(live_rev, str) or not live_rev:
        return VerificationStatus.INCONCLUSIVE, "missing_verification_evidence"
    status = ver.get("status")
    if status != "passed":
        return (
            VerificationStatus.INCONCLUSIVE if status == "unknown" else VerificationStatus.FAIL,
            f"verification_{status or 'not_run'}",
        )
    if ver.get("end_revision") != live_rev or ver.get("stale_reason") is not None:
        return VerificationStatus.INCONCLUSIVE, "verification_stale"
    if ver.get("plan_digest") != state.get("plan_digest") or int(ver.get("checks_count", 0)) < 1:
        return VerificationStatus.FAIL, "verification_plan_mismatch"
    return VerificationStatus.PASS, "project_task_verified"


class ProjectStepVerifier:
    """Stateless step verifier correlating contracted action_id, side effects, and state."""

    def verify_step(
        self, *, contract: ActionContract, evidence: VerificationEvidence
    ) -> VerificationResult:
        if not isinstance(contract, ActionContract) or not isinstance(evidence, VerificationEvidence):
            raise VerificationError("VERIFICATION_INPUT_INVALID", ("input",), "invalid contract or evidence")
        if contract.adapter_name != PROJECT_ADAPTER_NAME:
            raise VerificationError("VERIFICATION_INPUT_INVALID", ("contract", "adapter_name"), "adapter mismatch")
        rebuilt_ev = VerificationEvidence.model_validate(evidence.model_dump(mode="python"))
        state = rebuilt_ev.observation.state
        if not isinstance(state, dict):
            raise VerificationError("VERIFICATION_INPUT_INVALID", ("evidence", "state"), "state must be dict")
        action_id = contract.action.metadata.get("action_id")
        if not isinstance(action_id, str) or not action_id.strip():
            raise VerificationError("VERIFICATION_INPUT_INVALID", ("action", "metadata"), "missing action_id")

        last = state.get("last_tool_result")
        results: list[OutcomeVerification] = []
        statuses: list[VerificationStatus] = []
        req_flags = [o.required for o in contract.expected_outcomes]

        for idx, outcome in enumerate(contract.expected_outcomes):
            if not rebuilt_ev.state_complete or state.get("observation_overflow") is True:
                st, detail = VerificationStatus.INCONCLUSIVE, "incomplete_evidence"
            elif not isinstance(last, dict) or last.get("action_id") != action_id:
                st, detail = VerificationStatus.FAIL, "action_id_mismatch"
            elif outcome.outcome_type == "project_task_verified":
                if contract.action.action_type != "verify_project":
                    st, detail = VerificationStatus.FAIL, "verify_project_required"
                elif last.get("tool_succeeded") is not True:
                    st, detail = VerificationStatus.FAIL, "verify_project_failed"
                else:
                    st, detail = _evaluate_task_state(state)
            elif outcome.outcome_type == "project_file_written":
                if (
                    last.get("tool_succeeded") is not True
                    or last.get("path") != outcome.payload.get("path", contract.action.payload.get("path"))
                    or not last.get("read_back_sha256")
                    or last.get("read_back_sha256") != last.get("written_sha256")
                ):
                    st, detail = VerificationStatus.FAIL, "write_verification_mismatch"
                else:
                    st, detail = VerificationStatus.PASS, "file_write_verified"
            elif outcome.outcome_type == "project_command_result":
                if (
                    (outcome.payload.get("require_success", True) and last.get("tool_succeeded") is not True)
                    or not last.get("command_id")
                    or not last.get("proposal_digest")
                ):
                    st, detail = VerificationStatus.FAIL, "command_outcome_mismatch"
                else:
                    st, detail = VerificationStatus.PASS, "command_verified"
            else:
                if outcome.payload.get("require_success", True) and last.get("tool_succeeded") is not True:
                    st, detail = VerificationStatus.FAIL, "tool_execution_failed"
                elif contract.action.action_type == "ask_user" and outcome.payload.get("require_success", True) and (
                    last.get("action_type") != "ask_user"
                    or not last.get("question_id")
                    or last.get("state") != "answered"
                ):
                    st, detail = VerificationStatus.FAIL, "ask_user_not_answered"
                else:
                    st, detail = VerificationStatus.PASS, "step_verified"

            statuses.append(st)
            results.append(
                OutcomeVerification(
                    schema_version=PROJECT_SCHEMA_VERSION,
                    outcome_index=idx,
                    status=st,
                    has_actual_value=False,
                    actual_value=None,
                    reason=detail,
                )
            )

        req_statuses = [s for s, req in zip(statuses, req_flags, strict=True) if req]
        if any(s is VerificationStatus.FAIL for s in req_statuses):
            agg, reason = VerificationStatus.FAIL, "REQUIRED_FAILED"
        elif any(s is VerificationStatus.INCONCLUSIVE for s in req_statuses):
            agg, reason = VerificationStatus.INCONCLUSIVE, "REQUIRED_INCONCLUSIVE"
        else:
            agg, reason = VerificationStatus.PASS, "REQUIRED_PASSED"

        return VerificationResult(
            schema_version=PROJECT_SCHEMA_VERSION,
            status=agg,
            verifier=PROJECT_STEP_VERIFIER,
            outcome_results=results,
            reason=reason,
            metadata={
                "scope": "step",
                "contract_id": contract.contract_id,
                "goal_id": contract.goal_id,
                "observation_id": rebuilt_ev.observation.observation_id,
                "state_complete": rebuilt_ev.state_complete,
            },
        )


class ProjectTaskCompletionOracle:
    """Pure task completion oracle evaluated after every Core step."""

    def __init__(self, *, task: ProjectTask) -> None:
        self._task = ProjectTask.model_validate(task)

    def evaluate_completion(
        self, *, criteria: TaskCompletionCriteria, evidence: VerificationEvidence
    ) -> VerificationResult:
        if not isinstance(criteria, TaskCompletionCriteria) or not isinstance(evidence, VerificationEvidence):
            raise VerificationError("VERIFICATION_INPUT_INVALID", ("input",), "invalid criteria or evidence")
        rebuilt_ev = VerificationEvidence.model_validate(evidence.model_dump(mode="python"))
        state = rebuilt_ev.observation.state
        if not isinstance(state, dict):
            raise VerificationError("VERIFICATION_INPUT_INVALID", ("evidence", "state"), "state must be dict")

        results: list[OutcomeVerification] = []
        statuses: list[VerificationStatus] = []
        for idx, _ in enumerate(criteria.conditions):
            if not rebuilt_ev.state_complete or state.get("observation_overflow") is True:
                st, detail = VerificationStatus.INCONCLUSIVE, "incomplete_evidence"
            else:
                st, detail = _evaluate_task_state(state)
            statuses.append(st)
            results.append(
                OutcomeVerification(
                    schema_version=PROJECT_SCHEMA_VERSION,
                    outcome_index=idx,
                    status=st,
                    has_actual_value=False,
                    actual_value=None,
                    reason=detail,
                )
            )

        req_statuses = [
            s for s, c in zip(statuses, criteria.conditions, strict=True) if c.required
        ]
        if any(s is VerificationStatus.FAIL for s in req_statuses):
            agg, reason = VerificationStatus.FAIL, "REQUIRED_FAILED"
        elif any(s is VerificationStatus.INCONCLUSIVE for s in req_statuses):
            agg, reason = VerificationStatus.INCONCLUSIVE, "REQUIRED_INCONCLUSIVE"
        else:
            agg, reason = VerificationStatus.PASS, "REQUIRED_PASSED"

        return VerificationResult(
            schema_version=PROJECT_SCHEMA_VERSION,
            status=agg,
            verifier=PROJECT_TASK_ORACLE,
            outcome_results=results,
            reason=reason,
            metadata={
                "scope": "task",
                "goal_id": criteria.goal_id,
                "observation_id": rebuilt_ev.observation.observation_id,
                "state_complete": rebuilt_ev.state_complete,
            },
        )
