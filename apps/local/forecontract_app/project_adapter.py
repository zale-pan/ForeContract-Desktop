"""``ProjectCodingAdapter`` implementing Core ``Adapter`` for existing local projects."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from forecontract.models.actions import ActionSpec, AdapterCapabilities
from forecontract.models.observations import Observation
from forecontract.models.outcomes import ActualOutcome, ErrorInfo, ExecutionStatus
from forecontract.protocols.adapter import (
    InvalidActionPayloadError,
    UnsupportedActionError,
)
from forecontract_coding.errors import CodingError
from forecontract_coding.models import (
    is_valid_sha256_hex,
    thaw_json_value,
    utf8_byte_length,
)

from forecontract_app.project_commands import ProjectCommandRunner
from forecontract_app.project_models import (
    PROJECT_MAX_COMMAND_ARG_BYTES,
    PROJECT_MAX_COMMAND_ARGS,
    PROJECT_MAX_COMMAND_TIMEOUT_MS,
    PROJECT_MAX_OBSERVATION_BYTES,
    PROJECT_SCHEMA_VERSION,
    SUPPORTED_PROJECT_ACTIONS,
    CheckExecutionRecord,
    CommandProposal,
    ProjectError,
    ProjectInstructions,
    ProjectTask,
    ProjectVerificationEvidence,
    ScopeProposalPayload,
    VerificationPlan,
    WorkspaceRevision,
)
from forecontract_app.project_permissions import PermissionBroker
from forecontract_app.project_verification import (
    PROJECT_ADAPTER_NAME,
    PROJECT_ENVIRONMENT_TYPE,
    SUPPORTED_PROJECT_CONDITIONS,
    SUPPORTED_PROJECT_OUTCOMES,
    evaluate_project_result,
    parse_junit_xml_in_result_dir,
)
from forecontract_app.project_workspace import ProjectWorkspace

if TYPE_CHECKING:
    from forecontract_app.extension_models import ExtensionCleanup
    from forecontract_app.extension_runtime import ExtensionRuntime
    from forecontract_app.session_context import ConversationRuntime

_SCOPE_OPERATIONS: tuple[str, ...] = ("create", "modify")
_SCOPE_FIELDS: tuple[str, ...] = (
    "exact_paths",
    "write_prefixes",
    "operations",
    "protected_paths",
    "verification_plan_summary",
)
_SCOPE_SUMMARY_MAX_LENGTH: int = 1024
_VERIFICATION_PLAN_FIELDS: frozenset[str] = frozenset({"plan_id", "summary", "checks"})
_VERIFICATION_CHECK_FIELDS: frozenset[str] = frozenset(
    {
        "check_id",
        "kind",
        "executable",
        "argv",
        "cwd",
        "timeout_ms",
        "protected_paths",
        "expected_tests",
    }
)
_VERIFICATION_EXPECTED_TEST_FIELDS: frozenset[str] = frozenset({"classname", "name"})
_VERIFICATION_KNOWN_ALIASES: frozenset[str] = frozenset({"type", "path", "test", "command"})
_VERIFICATION_KINDS: tuple[str, ...] = ("junit_exact", "exit_code")

_ACTION_GUIDANCE: dict[str, dict[str, Any]] = {
    "list_directory": {"optional": ["path", "cursor", "limit"], "required": []},
    "find_paths": {"optional": ["pattern", "cursor", "limit"], "required": []},
    "read_file": {
        "required": ["path"],
        "optional": ["start_line", "max_lines", "byte_offset", "expected_sha256"],
    },
    "search_text": {"required": ["query"], "optional": ["paths", "cursor", "max_matches"]},
    "propose_scope": {
        "required": ["operations", "verification_plan_summary"],
        "optional": ["exact_paths", "write_prefixes", "protected_paths"],
        "operations_enum": ["create", "modify"],
        "target_requirement": "at least one exact_paths or write_prefixes entry must contain the target",
        "verification_plan_summary_max_length": 1024,
    },
    "propose_verification": {
        "required": ["checks"],
        "optional": ["plan_id", "summary"],
        "checks_min_items": 1,
        "check_required": ["check_id", "kind", "executable", "timeout_ms"],
        "check_optional": ["argv", "cwd", "protected_paths", "expected_tests"],
        "kind_enum": ["junit_exact", "exit_code"],
        "unique_check_id": True,
        "junit_exact_requirements": (
            "non-empty expected_tests with unique (classname, name) pairs "
            "and at least one argv entry containing '{result_dir}'"
        ),
        "exit_code_requirements": "expected_tests omitted or empty",
        "timeout_ms_range": [1, PROJECT_MAX_COMMAND_TIMEOUT_MS],
        "argv_max_items": PROJECT_MAX_COMMAND_ARGS,
        "argv_item_max_bytes": PROJECT_MAX_COMMAND_ARG_BYTES,
    },
    "create_file": {"required": ["path", "content"], "optional": []},
    "edit_file": {"required": ["path", "expected_sha256", "old_text", "new_text"], "optional": []},
    "propose_command": {
        "required": ["executable", "argv", "cwd", "timeout_ms", "purpose", "explanation"],
        "optional": ["command_id", "executable_sha256", "check_id"],
    },
    "start_command": {"required": ["proposal_id", "proposal_digest"], "optional": []},
    "poll_command": {"required": ["command_id"], "optional": ["after"]},
    "stop_command": {"required": ["command_id"], "optional": []},
    "verify_project": {"required": ["plan_id", "plan_digest"], "optional": []},
}

_ASK_USER_ADAPTER_GUIDANCE: dict[str, Any] = {
    "required": ["question"],
    "optional": ["options"],
}


def _truncate_utf8(value: str, limit: int) -> tuple[str, bool]:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value, False
    cut = encoded[:limit]
    while cut:
        try:
            return cut.decode("utf-8"), True
        except UnicodeDecodeError:
            cut = cut[:-1]
    return "", True


class ProjectCodingAdapter:
    """Bounded Core ``Adapter`` with lazy revisions, two-stage grants, and verification plan orchestration."""

    def __init__(
        self,
        *,
        task: ProjectTask,
        workspace: ProjectWorkspace,
        broker: PermissionBroker,
        command_runner: ProjectCommandRunner,
        conversation: ConversationRuntime | None = None,
        extensions: ExtensionRuntime | None = None,
    ) -> None:
        self._task = task
        self._workspace = workspace
        self._broker = broker
        self._commands = command_runner
        self._conversation = conversation
        self._extensions = extensions
        self._extension_cleanup: ExtensionCleanup | None = None
        self._verified_ext_mutation_seq: int = 0

        self._instructions: ProjectInstructions = ProjectInstructions(present=False)
        self._instructions_valid: bool = True
        self._instruction_error: str | None = None
        self._baseline: WorkspaceRevision | None = None
        self._current_revision: str = ""
        self._scope_complete: bool = True
        self._last_tool_result: dict[str, Any] | None = None
        self._last_error: dict[str, str] | None = None

        self._latest_checks: list[CheckExecutionRecord] = []
        self._verified_end_revision: str | None = None
        self._verified_plan_id: str | None = None
        self._verified_plan_digest: str | None = None
        self._verified_plan_rev_seen: int = -1
        self._verification_status: str = "not_run"
        self._stale_reason: str | None = None
        self._unverified_reasons: list[str] = []

    @property
    def name(self) -> str:
        return PROJECT_ADAPTER_NAME

    @property
    def baseline_revision(self) -> str:
        return self._baseline.revision if self._baseline is not None else ""

    def _refresh_instructions(self, target_path: str | None = None) -> ProjectInstructions:
        try:
            inst = self._workspace.load_instructions(target_path=target_path)
            self._instructions = inst
            self._instructions_valid = True
            self._instruction_error = None
            self._broker.update_instruction_sha256(inst.sha256 or "")
            return inst
        except ProjectError as exc:
            self._instructions_valid = False
            self._instruction_error = f"{exc.code}: {exc.message}"
            raise

    def _refresh_instructions_for_targets(
        self,
        *,
        target_paths: Iterable[str] = (),
        target_dirs: Iterable[str] = (),
    ) -> tuple[ProjectInstructions, dict[str, str], tuple[str, ...]]:
        try:
            dirs = self._workspace.ancestor_dirs_for_targets(
                target_paths=target_paths, target_dirs=target_dirs
            )
            inst = self._workspace.load_instructions_for_targets(
                target_paths=target_paths, target_dirs=target_dirs
            )
            self._instructions = inst
            self._instructions_valid = True
            self._instruction_error = None
            self._broker.update_instruction_sha256(inst.sha256 or "")
            return inst, {f.path: f.sha256 for f in inst.files}, dirs
        except ProjectError as exc:
            self._instructions_valid = False
            self._instruction_error = f"{exc.code}: {exc.message}"
            raise

    def _allow_user_questions(self) -> bool:
        return self._conversation is not None and self._conversation.question_broker is not None

    def _allow_extensions(self) -> bool:
        return self._extensions is not None

    def capabilities(self) -> AdapterCapabilities:
        actions = list(SUPPORTED_PROJECT_ACTIONS)
        guidance = dict(_ACTION_GUIDANCE)
        if self._allow_user_questions():
            actions.append("ask_user")
            guidance["ask_user"] = _ASK_USER_ADAPTER_GUIDANCE
        if self._extensions is not None:
            ext_guidance = self._extensions.action_guidance()
            actions.extend(ext_guidance.keys())
            guidance.update(ext_guidance)
        return AdapterCapabilities(
            schema_version=PROJECT_SCHEMA_VERSION,
            adapter_name=PROJECT_ADAPTER_NAME,
            environment_type=PROJECT_ENVIRONMENT_TYPE,
            supported_actions=actions,
            supported_observation_types=["project_workspace_state"],
            supported_condition_types=sorted(SUPPORTED_PROJECT_CONDITIONS),
            supported_outcome_types=sorted(SUPPORTED_PROJECT_OUTCOMES),
            metadata={"action_guidance": guidance},
        )

    def _validate_exact_keys(
        self, payload: dict[str, Any], *, required: set[str], optional: set[str]
    ) -> None:
        allowed = required | optional
        extra = set(payload.keys()) - allowed
        if extra:
            raise InvalidActionPayloadError(f"unexpected payload keys: {sorted(extra)}")
        missing = required - set(payload.keys())
        if missing:
            raise InvalidActionPayloadError(f"missing required payload keys: {sorted(missing)}")

    def _record_scope_diagnostic(self, *, field: str) -> None:
        """Run-scoped, sanitized propose_scope rejection diagnostic (whitelisted fields only)."""
        hint = (
            "exact_paths or write_prefixes must contain the target; "
            if field == "exact_paths"
            else ""
        )
        diagnostic = (
            f"propose_scope: field {field} rejected; "
            f"{hint}allowed fields: {', '.join(_SCOPE_FIELDS)}; "
            f"allowed operations: {', '.join(_SCOPE_OPERATIONS)}"
        )
        if diagnostic not in self._unverified_reasons:
            self._unverified_reasons = [diagnostic]

    @staticmethod
    def _scope_error_field(payload: dict[str, Any], exc: Exception) -> str:
        """Map a strict-model rejection to a whitelisted field path; never echo raw input."""
        errors = getattr(exc, "errors", None)
        if callable(errors):
            try:
                entries = errors()
            except Exception:  # noqa: BLE001 - defensive: never surface the raw error
                entries = []
            for entry in entries:
                loc = entry.get("loc") if isinstance(entry, dict) else None
                if not isinstance(loc, tuple) or not loc:
                    continue
                head = loc[0]
                if head in _SCOPE_FIELDS:
                    if len(loc) > 1 and isinstance(loc[1], int):
                        return f"{head}[{loc[1]}]"
                    return str(head)
                # Any unexpected/malicious field name is never echoed.
                return "payload"
        exact = payload.get("exact_paths")
        write = payload.get("write_prefixes")
        if not exact and not write:
            return "exact_paths"
        return "payload"

    def _validate_propose_scope(self, payload: dict[str, Any]) -> None:
        """Strict, model-backed propose_scope validation; reject before the action contract."""
        try:
            ScopeProposalPayload.model_validate(payload)
        except Exception as exc:  # noqa: BLE001 - any invalid payload is rejected before contract
            field = self._scope_error_field(payload, exc)
            self._record_scope_diagnostic(field=field)
            raise InvalidActionPayloadError(f"propose_scope: field {field} rejected") from None

    def _record_verification_diagnostics(self, *, diagnostics: list[str]) -> None:
        """Run-scoped, sanitized propose_verification rejection diagnostics (whitelisted labels only)."""
        self._unverified_reasons = list(diagnostics[:16])

    @staticmethod
    def _verification_loc_to_safe_path(loc: tuple[Any, ...]) -> str:
        """Map a Pydantic error loc tuple to a whitelisted path; never echo arbitrary keys."""
        if not loc:
            return "payload"
        head = loc[0]
        if head == "checks":
            if len(loc) == 1:
                return "checks"
            idx = loc[1]
            prefix = f"checks[{idx}]" if isinstance(idx, int) and 0 <= idx < 1000 else "checks[?]"
            if len(loc) == 2:
                return prefix
            sub = loc[2]
            if sub in _VERIFICATION_CHECK_FIELDS or sub in _VERIFICATION_KNOWN_ALIASES:
                if sub == "expected_tests" and len(loc) >= 4:
                    et_idx = loc[3]
                    et_prefix = (
                        f"{prefix}.expected_tests[{et_idx}]"
                        if isinstance(et_idx, int) and 0 <= et_idx < 1000
                        else f"{prefix}.expected_tests"
                    )
                    if len(loc) >= 5:
                        leaf = loc[4]
                        if leaf in _VERIFICATION_EXPECTED_TEST_FIELDS:
                            return f"{et_prefix}.{leaf}"
                        return f"{et_prefix}.unexpected_field"
                    return et_prefix
                if sub == "argv" and len(loc) >= 4 and isinstance(loc[3], int) and 0 <= loc[3] < 1000:
                    return f"{prefix}.argv[{loc[3]}]"
                return f"{prefix}.{sub}"
            return f"{prefix}.unexpected_field"
        if head in _VERIFICATION_PLAN_FIELDS:
            return str(head)
        return "payload.unexpected_field"

    @classmethod
    def _verification_error_diagnostics(cls, exc: Exception) -> list[str]:
        """Convert strict VerificationPlan ValidationError entries into sanitized diagnostics."""
        diagnostics: list[str] = []
        seen: set[str] = set()
        errors = getattr(exc, "errors", None)
        if callable(errors):
            try:
                entries = errors()
            except Exception:  # noqa: BLE001 - defensive: never surface the raw error
                entries = []
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                loc = entry.get("loc")
                path_str = (
                    cls._verification_loc_to_safe_path(loc)
                    if isinstance(loc, tuple)
                    else "payload"
                )
                err_type = str(entry.get("type") or "")
                if err_type == "missing":
                    reason = "missing required field"
                elif err_type == "extra_forbidden":
                    reason = "unexpected field not allowed"
                elif err_type == "literal_error":
                    reason = (
                        f"invalid value (allowed kind: {', '.join(_VERIFICATION_KINDS)})"
                        if path_str.endswith(".kind")
                        else "invalid literal value"
                    )
                elif err_type.startswith("int_") or err_type in (
                    "greater_than_equal",
                    "less_than_equal",
                ):
                    reason = "invalid integer range or type"
                elif err_type.startswith("string_") or err_type in ("too_short", "too_long"):
                    reason = "invalid string or list length"
                elif err_type == "value_error":
                    reason = "constraint validation failed"
                else:
                    reason = "invalid value"
                diag = f"propose_verification: {path_str}: {reason}"
                if diag not in seen:
                    seen.add(diag)
                    diagnostics.append(diag)
        if not diagnostics:
            diagnostics.append("propose_verification: payload: constraint validation failed")
        return diagnostics

    def _validate_propose_verification(self, payload: dict[str, Any]) -> None:
        """Strict, model-backed propose_verification validation; reject before the action contract."""
        try:
            VerificationPlan.model_validate(payload)
        except Exception as exc:  # noqa: BLE001 - any invalid payload is rejected before contract
            diagnostics = self._verification_error_diagnostics(exc)
            self._record_verification_diagnostics(diagnostics=diagnostics)
            raise InvalidActionPayloadError("; ".join(diagnostics)) from None

    def validate_action(self, action: ActionSpec) -> None:
        """Pure schema validation of the 13 frozen actions (plus opt-in ask_user / extensions) without I/O."""
        if not isinstance(action, ActionSpec):
            raise InvalidActionPayloadError("ActionSpec is required")
        if self._extensions is not None:
            from forecontract_app.extension_runtime import EXTENSION_ACTION_NAMES

            if action.action_type in EXTENSION_ACTION_NAMES:
                self._extensions.validate_action(action)
                return
        allowed_actions = (
            (*SUPPORTED_PROJECT_ACTIONS, "ask_user")
            if self._allow_user_questions()
            else SUPPORTED_PROJECT_ACTIONS
        )
        if action.action_type not in allowed_actions:
            raise UnsupportedActionError(f"unsupported action_type: {action.action_type}")
        if not isinstance(action.payload, Mapping):
            raise InvalidActionPayloadError("action payload must be a mapping")
        payload = thaw_json_value(action.payload)
        if not isinstance(payload, dict):
            raise InvalidActionPayloadError("action payload must be a JSON object")

        at = action.action_type
        if at == "list_directory":
            self._validate_exact_keys(payload, required=set(), optional={"path", "cursor", "limit"})
            if "path" in payload and not isinstance(payload["path"], str):
                raise InvalidActionPayloadError("path must be string")
            if "cursor" in payload and payload["cursor"] is not None and not isinstance(payload["cursor"], str):
                raise InvalidActionPayloadError("cursor must be string or null")
            if "limit" in payload:
                lim = payload["limit"]
                if isinstance(lim, bool) or not isinstance(lim, int) or not (1 <= lim <= 100):
                    raise InvalidActionPayloadError("limit must be int in 1..100")
            return

        if at == "find_paths":
            self._validate_exact_keys(payload, required=set(), optional={"pattern", "cursor", "limit"})
            if "pattern" in payload and (not isinstance(payload["pattern"], str) or not payload["pattern"].strip()):
                raise InvalidActionPayloadError("pattern must be non-blank string")
            if "cursor" in payload and payload["cursor"] is not None and not isinstance(payload["cursor"], str):
                raise InvalidActionPayloadError("cursor must be string or null")
            if "limit" in payload:
                lim = payload["limit"]
                if isinstance(lim, bool) or not isinstance(lim, int) or not (1 <= lim <= 100):
                    raise InvalidActionPayloadError("limit must be int in 1..100")
            return

        if at == "read_file":
            self._validate_exact_keys(
                payload,
                required={"path"},
                optional={"start_line", "max_lines", "byte_offset", "expected_sha256"},
            )
            if not isinstance(payload["path"], str) or not payload["path"].strip():
                raise InvalidActionPayloadError("path must be non-blank string")
            if "start_line" in payload:
                sl = payload["start_line"]
                if isinstance(sl, bool) or not isinstance(sl, int) or sl < 1:
                    raise InvalidActionPayloadError("start_line must be >= 1")
            if "max_lines" in payload:
                ml = payload["max_lines"]
                if isinstance(ml, bool) or not isinstance(ml, int) or not (1 <= ml <= 200):
                    raise InvalidActionPayloadError("max_lines must be in 1..200")
            if "byte_offset" in payload:
                bo = payload["byte_offset"]
                if isinstance(bo, bool) or not isinstance(bo, int) or bo < 0:
                    raise InvalidActionPayloadError("byte_offset must be >= 0")
            if (
                "expected_sha256" in payload
                and payload["expected_sha256"] is not None
                and not is_valid_sha256_hex(payload["expected_sha256"])
            ):
                raise InvalidActionPayloadError("expected_sha256 must be 64-hex")
            return

        if at == "search_text":
            self._validate_exact_keys(
                payload, required={"query"}, optional={"paths", "cursor", "max_matches"}
            )
            if not isinstance(payload["query"], str) or not payload["query"]:
                raise InvalidActionPayloadError("query must be non-empty string")
            if (
                "paths" in payload
                and payload["paths"] is not None
                and (
                    not isinstance(payload["paths"], list)
                    or not all(isinstance(p, str) for p in payload["paths"])
                )
            ):
                raise InvalidActionPayloadError("paths must be list of strings")
            if "max_matches" in payload:
                mm = payload["max_matches"]
                if isinstance(mm, bool) or not isinstance(mm, int) or not (1 <= mm <= 50):
                    raise InvalidActionPayloadError("max_matches must be in 1..50")
            return

        if at == "propose_scope":
            self._validate_propose_scope(payload)
            return

        if at == "propose_verification":
            self._validate_propose_verification(payload)
            return

        if at == "create_file":
            self._validate_exact_keys(payload, required={"path", "content"}, optional=set())
            if not isinstance(payload["path"], str) or not payload["path"].strip():
                raise InvalidActionPayloadError("path must be non-blank")
            if not isinstance(payload["content"], str):
                raise InvalidActionPayloadError("content must be string")
            return

        if at == "edit_file":
            self._validate_exact_keys(
                payload,
                required={"path", "expected_sha256", "old_text", "new_text"},
                optional=set(),
            )
            if not isinstance(payload["path"], str) or not payload["path"].strip():
                raise InvalidActionPayloadError("path must be non-blank")
            if not is_valid_sha256_hex(payload["expected_sha256"]):
                raise InvalidActionPayloadError("expected_sha256 must be 64-hex")
            if not isinstance(payload["old_text"], str) or not payload["old_text"]:
                raise InvalidActionPayloadError("old_text must be non-empty string")
            if not isinstance(payload["new_text"], str):
                raise InvalidActionPayloadError("new_text must be string")
            return

        if at == "propose_command":
            if "env" in payload or "env_keys" in payload:
                raise InvalidActionPayloadError("env and env_keys are forbidden on propose_command")
            try:
                CommandProposal.model_validate(payload)
            except Exception as exc:
                raise InvalidActionPayloadError(f"invalid propose_command payload: {exc}") from exc
            return

        if at == "start_command":
            self._validate_exact_keys(
                payload, required={"proposal_id", "proposal_digest"}, optional=set()
            )
            if not isinstance(payload["proposal_id"], str) or not payload["proposal_id"].strip():
                raise InvalidActionPayloadError("proposal_id must be non-blank")
            if not is_valid_sha256_hex(payload["proposal_digest"]):
                raise InvalidActionPayloadError("proposal_digest must be 64-hex")
            return

        if at == "poll_command":
            self._validate_exact_keys(payload, required={"command_id"}, optional={"after"})
            if not isinstance(payload["command_id"], str) or not payload["command_id"].strip():
                raise InvalidActionPayloadError("command_id must be non-blank")
            if "after" in payload:
                after = payload["after"]
                if isinstance(after, bool) or not isinstance(after, int) or after < 0:
                    raise InvalidActionPayloadError("after must be >= 0 integer")
            return

        if at == "stop_command":
            self._validate_exact_keys(payload, required={"command_id"}, optional=set())
            if not isinstance(payload["command_id"], str) or not payload["command_id"].strip():
                raise InvalidActionPayloadError("command_id must be non-blank")
            return

        if at == "verify_project":
            self._validate_exact_keys(payload, required={"plan_id", "plan_digest"}, optional=set())
            if not isinstance(payload["plan_id"], str) or not payload["plan_id"].strip():
                raise InvalidActionPayloadError("plan_id must be non-blank")
            if not is_valid_sha256_hex(payload["plan_digest"]):
                raise InvalidActionPayloadError("plan_digest must be 64-hex")
            return

        if at == "ask_user":
            self._validate_exact_keys(payload, required={"question"}, optional={"options"})
            q_val = payload["question"]
            if (
                not isinstance(q_val, str)
                or not q_val.strip()
                or "\x00" in q_val
                or len(q_val) > 4096
            ):
                raise InvalidActionPayloadError("question must be non-blank <= 4096 chars without NUL")
            if "options" in payload:
                opts = payload["options"]
                if not isinstance(opts, list) or len(opts) > 4:
                    raise InvalidActionPayloadError("options must be list of 0..4 strings")
                seen: set[str] = set()
                for opt in opts:
                    if (
                        not isinstance(opt, str)
                        or not opt.strip()
                        or "\x00" in opt
                        or len(opt) > 128
                    ):
                        raise InvalidActionPayloadError("each option must be non-blank <= 128 chars")
                    if opt in seen:
                        raise InvalidActionPayloadError("options must be distinct")
                    seen.add(opt)
            return

    async def start(self) -> None:
        try:
            self._refresh_instructions()
        except ProjectError:
            # Record incomplete instruction state; observation surfaces it and writes are blocked.
            pass
        baseline = self._workspace.capture_baseline()
        self._baseline = baseline
        self._current_revision = baseline.revision
        self._scope_complete = baseline.scope_complete

    async def stop(self) -> None:
        self._broker.cancel_pending()
        if self._conversation is not None and self._conversation.question_broker is not None:
            self._conversation.question_broker.cancel_pending(reason_code="ADAPTER_STOPPED")
        await self._commands.stop_all()
        if self._extensions is not None:
            self._extension_cleanup = await self._extensions.aclose()

    async def observe(self) -> Observation:
        """Build bounded observation lazily without full-repository scan on each step."""
        try:
            self._refresh_instructions()
        except ProjectError:
            pass

        proposals = self._broker.list_proposals()
        scope_view = self._broker.active_scope()
        plan_tuple = self._broker.active_plan()
        cleanup_ok, rem_active = self._commands.cleanup_summary()
        active_cmds = self._commands.active_command_ids()

        # If a new verification plan was approved after previous verification, mark previous verification stale.
        if (
            self._verification_status == "passed"
            and plan_tuple is not None
            and (
                self._verified_plan_id != plan_tuple[1].plan_id
                or self._verified_plan_digest != plan_tuple[0].digest
                or self._verified_plan_rev_seen != self._broker.plan_revision
            )
        ):
            self._verification_status = "unknown"
            self._stale_reason = "verification_plan_changed"

        ver_status = self._verification_status
        stale_reason = self._stale_reason
        if (
            ver_status == "passed"
            and self._verified_end_revision is not None
            and self._verified_end_revision != self._current_revision
        ):
            ver_status = "unknown"
            stale_reason = stale_reason or "workspace_modified_after_verification"
        if (
            ver_status == "passed"
            and self._extensions is not None
            and self._extensions.mutation_seq != self._verified_ext_mutation_seq
        ):
            ver_status = "unknown"
            stale_reason = stale_reason or "extension_call_after_verification"

        inst_info = {
            "present": self._instructions.present,
            "sha256": self._instructions.sha256,
            "total_bytes": self._instructions.total_bytes,
            "files": [f.model_dump(mode="json") for f in self._instructions.files],
            "content": self._instructions.content[:8192],
        }

        state: dict[str, Any] = {
            "scope_ok": True,
            "scope_complete": self._scope_complete,
            "root_identity": self._workspace.root_identity,
            "scope_approved": scope_view is not None and scope_view.state == "approved",
            "plan_approved": plan_tuple is not None and plan_tuple[0].state == "approved",
            "plan_id": plan_tuple[1].plan_id if plan_tuple is not None else None,
            "plan_digest": plan_tuple[0].digest if plan_tuple is not None else None,
            "approved_command_ids": [
                p.id for p in proposals if p.kind == "command" and p.state == "approved"
            ],
            "pending_proposals": [
                {"id": p.id, "kind": p.kind, "digest": p.digest, "state": p.state}
                for p in proposals
                if p.state == "pending"
            ],
            "instructions_valid": self._instructions_valid,
            "instruction_error": self._instruction_error,
            "instruction_sha256": self._instructions.sha256,
            "instructions": inst_info,
            "workspace_revision": self._current_revision,
            "baseline_revision": self.baseline_revision,
            "remaining_active_processes": rem_active + len(active_cmds),
            "cleanup_ok": cleanup_ok,
            "verification": {
                "status": ver_status,
                "plan_id": self._verified_plan_id,
                "plan_digest": self._verified_plan_digest,
                "end_revision": self._verified_end_revision,
                "checks_count": len(self._latest_checks),
                "stale_reason": stale_reason,
                "checks": [
                    {
                        "check_id": c.check_id,
                        "kind": c.kind,
                        "command_id": c.command_id,
                        "exit_code": c.exit_code,
                        "passed": c.passed,
                        "failure_reason": c.failure_reason,
                    }
                    for c in self._latest_checks
                ],
            },
            "last_tool_result": self._last_tool_result,
            "last_error": self._last_error,
            "runtime_capabilities": {
                "python_executable": str(self._task.python_executable),
                "max_observation_bytes": PROJECT_MAX_OBSERVATION_BYTES,
                "action_guidance": _ACTION_GUIDANCE,
            },
            "state_complete": True,
            "observation_overflow": False,
        }
        if self._conversation is not None:
            state["conversation"] = self._conversation.observation_payload()
        if self._extensions is not None:
            state["extensions"] = self._extensions.observation_payload(compact=False)

        obs = Observation(
            schema_version=PROJECT_SCHEMA_VERSION,
            observation_id=str(uuid4()),
            environment_type=PROJECT_ENVIRONMENT_TYPE,
            timestamp=datetime.now(timezone.utc),
            state=state,
        )

        raw = obs.model_dump_json().encode("utf-8")
        if len(raw) > PROJECT_MAX_OBSERVATION_BYTES and self._conversation is not None:
            orig_len = len(self._conversation.context_frame.recent_items)
            for keep in range(orig_len - 1, -1, -1):
                state["conversation"] = self._conversation.observation_payload(max_recent_items=keep)
                obs = Observation(
                    schema_version=PROJECT_SCHEMA_VERSION,
                    observation_id=obs.observation_id,
                    environment_type=PROJECT_ENVIRONMENT_TYPE,
                    timestamp=obs.timestamp,
                    state=state,
                )
                raw = obs.model_dump_json().encode("utf-8")
                if len(raw) <= PROJECT_MAX_OBSERVATION_BYTES:
                    break

        if len(raw) > PROJECT_MAX_OBSERVATION_BYTES:
            # Compact non-authoritative payload fields first, flagging incomplete state explicitly.
            compacted = dict(state)
            compacted["observation_overflow"] = True
            compacted["state_complete"] = False
            inst_copy = dict(inst_info)
            inst_copy["content"] = ""
            compacted["instructions"] = inst_copy
            if self._extensions is not None:
                ext_compact = self._extensions.observation_payload(compact=True)
                ext_compact["extensions_truncated"] = True
                compacted["extensions"] = ext_compact
            if isinstance(compacted.get("last_tool_result"), dict):
                ltr = dict(compacted["last_tool_result"])
                for key in (
                    "content",
                    "text",
                    "error_message",
                    "stderr_or_stdout",
                    "output_excerpt",
                    "chunks",
                    "matches",
                    "paths",
                    "entries",
                ):
                    if key in ltr:
                        if isinstance(ltr[key], str):
                            ltr[key], _ = _truncate_utf8(ltr[key], 512)
                        elif isinstance(ltr[key], list):
                            ltr[key] = ltr[key][:5]
                if "structured_content" in ltr and ltr["structured_content"] is not None:
                    ltr["structured_content"] = {"_truncated": True}
                ltr["truncated"] = True
                compacted["last_tool_result"] = ltr
            compacted["runtime_capabilities"] = {
                "python_executable": str(self._task.python_executable),
                "max_observation_bytes": PROJECT_MAX_OBSERVATION_BYTES,
            }
            obs = Observation(
                schema_version=PROJECT_SCHEMA_VERSION,
                observation_id=obs.observation_id,
                environment_type=PROJECT_ENVIRONMENT_TYPE,
                timestamp=obs.timestamp,
                state=compacted,
            )
            if utf8_byte_length(obs.model_dump_json()) > PROJECT_MAX_OBSERVATION_BYTES:
                orig_ltr = self._last_tool_result or {}
                fallback_ltr: dict[str, Any] = {
                    "action_id": orig_ltr.get("action_id"),
                    "action_type": orig_ltr.get("action_type"),
                    "tool_succeeded": orig_ltr.get("tool_succeeded"),
                    "error_code": orig_ltr.get("error_code"),
                    "truncated": True,
                }
                for keep_k in (
                    "command_id",
                    "exit_code",
                    "call_id",
                    "server_id",
                    "tool_name",
                    "status",
                    "is_error",
                    "effect",
                    "sha256",
                    "original_bytes",
                ):
                    if keep_k in orig_ltr:
                        fallback_ltr[keep_k] = orig_ltr.get(keep_k)
                for txt_k in ("stderr_or_stdout", "text", "error_message"):
                    if isinstance(orig_ltr.get(txt_k), str):
                        fallback_ltr[txt_k], _ = _truncate_utf8(orig_ltr[txt_k], 512)
                compacted["last_tool_result"] = fallback_ltr
                obs = Observation(
                    schema_version=PROJECT_SCHEMA_VERSION,
                    observation_id=obs.observation_id,
                    environment_type=PROJECT_ENVIRONMENT_TYPE,
                    timestamp=obs.timestamp,
                    state=compacted,
                )
            if utf8_byte_length(obs.model_dump_json()) > PROJECT_MAX_OBSERVATION_BYTES:
                raise ProjectError(
                    "OBSERVATION_BUDGET_EXCEEDED",
                    f"observation exceeds {PROJECT_MAX_OBSERVATION_BYTES} bytes even after compaction",
                    status_code=400,
                )
        return obs

    def _error_outcome(
        self,
        *,
        action_id: str,
        action_type: str,
        code: str,
        message: str,
        effect_applied: bool | None = False,
        extra: dict[str, Any] | None = None,
    ) -> ActualOutcome:
        result_record: dict[str, Any] = {
            "action_id": action_id,
            "action_type": action_type,
            "tool_succeeded": False,
            "error_code": code,
            "error_message": message,
            "effect_applied": effect_applied,
        }
        if extra:
            result_record.update(extra)
        self._last_tool_result = result_record
        self._last_error = {"code": code, "message": message}
        return ActualOutcome(
            schema_version=PROJECT_SCHEMA_VERSION,
            outcome_id=f"out-{action_id}",
            status=ExecutionStatus.FAILED,
            changes=result_record,
            error=ErrorInfo(schema_version=PROJECT_SCHEMA_VERSION, code=code, message=message),
            metadata={"action_id": action_id, "action_type": action_type},
        )

    def _ok_outcome(
        self, *, action_id: str, action_type: str, data: dict[str, Any]
    ) -> ActualOutcome:
        rec = dict(data)
        rec["action_id"] = action_id
        rec["action_type"] = action_type
        rec["tool_succeeded"] = True
        self._last_tool_result = rec
        self._last_error = None
        return ActualOutcome(
            schema_version=PROJECT_SCHEMA_VERSION,
            outcome_id=f"out-{action_id}",
            status=ExecutionStatus.SUCCEEDED,
            changes=rec,
            metadata={"action_id": action_id, "action_type": action_type},
        )

    def _fail_if_command_running(self, action_id: str, action_type: str) -> ActualOutcome | None:
        if self._commands.has_running_command():
            return self._error_outcome(
                action_id=action_id,
                action_type=action_type,
                code="COMMAND_IN_PROGRESS",
                message="cannot run writes, new commands, or verification while another command is running",
            )
        return None

    async def execute(self, action: ActionSpec) -> ActualOutcome:
        action_id = str(action.metadata.get("action_id") or f"act-{uuid4()}")
        at = action.action_type
        try:
            self.validate_action(action)
            payload = dict(thaw_json_value(action.payload))
        except (InvalidActionPayloadError, UnsupportedActionError) as exc:
            return self._error_outcome(
                action_id=action_id,
                action_type=at,
                code="INVALID_ACTION",
                message=str(exc),
            )

        try:
            if at == "list_directory":
                res = self._workspace.list_directory(
                    path=payload.get("path", "."),
                    cursor=payload.get("cursor"),
                    limit=payload.get("limit", 100),
                )
                return self._ok_outcome(
                    action_id=action_id, action_type=at, data=res.model_dump(mode="json")
                )

            if at == "find_paths":
                res_fp = self._workspace.find_paths(
                    pattern=payload.get("pattern", "*"),
                    cursor=payload.get("cursor"),
                    limit=payload.get("limit", 100),
                )
                return self._ok_outcome(
                    action_id=action_id, action_type=at, data=res_fp.model_dump(mode="json")
                )

            if at == "read_file":
                res_rf = self._workspace.read_file(
                    path=payload["path"],
                    start_line=payload.get("start_line", 1),
                    max_lines=payload.get("max_lines", 200),
                    byte_offset=payload.get("byte_offset", 0),
                    expected_sha256=payload.get("expected_sha256"),
                )
                return self._ok_outcome(
                    action_id=action_id,
                    action_type=at,
                    data=res_rf.model_dump(mode="json"),
                )

            if at == "search_text":
                res_st = self._workspace.search_text(
                    query=payload["query"],
                    paths=payload.get("paths"),
                    cursor=payload.get("cursor"),
                    max_matches=payload.get("max_matches", 50),
                )
                return self._ok_outcome(
                    action_id=action_id, action_type=at, data=res_st.model_dump(mode="json")
                )

            if at == "propose_scope":
                parsed_scope = ScopeProposalPayload.model_validate(payload)
                inst, inst_files, inst_dirs = self._refresh_instructions_for_targets(
                    target_paths=[*parsed_scope.exact_paths, *parsed_scope.protected_paths],
                    target_dirs=[".", *parsed_scope.write_prefixes],
                )
                prop = self._broker.create_scope_proposal(
                    scope=parsed_scope,
                    current_instruction_sha256=inst.sha256 or "",
                    current_instruction_files=inst_files,
                    checked_instruction_dirs=inst_dirs,
                )
                decided = await self._broker.wait_for_decision(prop.id)
                if decided.state != "approved":
                    return self._error_outcome(
                        action_id=action_id,
                        action_type=at,
                        code="PROPOSAL_DENIED",
                        message=f"scope proposal ended in state '{decided.state}'",
                        extra={"proposal_id": decided.id, "digest": decided.digest, "state": decided.state},
                    )
                self._workspace.set_extra_protected_paths(self._broker.protected_paths())
                return self._ok_outcome(
                    action_id=action_id,
                    action_type=at,
                    data={
                        "proposal_id": decided.id,
                        "digest": decided.digest,
                        "state": decided.state,
                    },
                )

            if at == "propose_verification":
                plan = VerificationPlan.model_validate(payload)
                prot_paths = [p for chk in plan.checks for p in chk.protected_paths]
                inst, inst_files, inst_dirs = self._refresh_instructions_for_targets(
                    target_paths=prot_paths,
                    target_dirs=[".", *[chk.cwd for chk in plan.checks]],
                )
                prot_shas = self._workspace.hash_protected_paths(prot_paths)
                prop = self._broker.create_verification_proposal(
                    plan=plan,
                    protected_shas=prot_shas,
                    current_instruction_sha256=inst.sha256 or "",
                    current_instruction_files=inst_files,
                    checked_instruction_dirs=inst_dirs,
                )
                decided = await self._broker.wait_for_decision(prop.id)
                if decided.state != "approved":
                    return self._error_outcome(
                        action_id=action_id,
                        action_type=at,
                        code="PROPOSAL_DENIED",
                        message=f"verification plan proposal ended in state '{decided.state}'",
                        extra={"proposal_id": decided.id, "digest": decided.digest, "state": decided.state},
                    )
                self._workspace.set_extra_protected_paths(self._broker.protected_paths())
                # Any new approved plan invalidates earlier passed verification.
                self._latest_checks = []
                self._verified_end_revision = None
                self._verification_status = "not_run"
                self._stale_reason = "verification_plan_changed"
                plan_id = decided.payload.get("plan", {}).get("plan_id") or decided.id
                return self._ok_outcome(
                    action_id=action_id,
                    action_type=at,
                    data={
                        "proposal_id": decided.id,
                        "plan_id": plan_id,
                        "plan_digest": decided.digest,
                        "digest": decided.digest,
                        "state": decided.state,
                    },
                )

            if at in ("create_file", "edit_file"):
                blocked = self._fail_if_command_running(action_id, at)
                if blocked is not None:
                    return blocked
                if not self._scope_complete:
                    return self._error_outcome(
                        action_id=action_id,
                        action_type=at,
                        code="SCOPE_INCOMPLETE",
                        message="workspace exceeded 10,000 files or 64 MiB limit; writes disabled",
                    )
                target_path = str(payload["path"])
                target_dirs = self._workspace.ancestor_dirs_for_targets(target_paths=[target_path])
                inst, inst_files, _ = self._refresh_instructions_for_targets(
                    target_paths=[target_path],
                    target_dirs=self._broker.active_instruction_dirs(),
                )
                # Refresh self._instructions to the exact target ancestor chain for observability.
                target_only_inst = self._workspace.load_instructions(target_path=target_path)
                self._instructions = target_only_inst
                op = "create" if at == "create_file" else "modify"
                self._broker.verify_write_allowed(
                    path=target_path,
                    operation=op,
                    current_run_id=self._task.run_id,
                    current_root_identity=self._workspace.root_identity,
                    current_instruction_sha256=target_only_inst.sha256 or "",
                    current_instruction_files=inst_files,
                    target_instruction_dirs=target_dirs,
                )
                self._workspace.set_extra_protected_paths(self._broker.protected_paths())
                if at == "create_file":
                    w_out = self._workspace.create_file(target_path, str(payload["content"]))
                else:
                    w_out = self._workspace.edit_file(
                        path=target_path,
                        expected_sha256=str(payload["expected_sha256"]),
                        old_text=str(payload["old_text"]),
                        new_text=str(payload["new_text"]),
                    )
                self._current_revision = f"dirty:{w_out.read_back_sha256[:16]}"
                if self._verification_status == "passed":
                    self._verification_status = "unknown"
                    self._stale_reason = "workspace_modified_after_verification"
                return self._ok_outcome(
                    action_id=action_id,
                    action_type=at,
                    data=w_out.model_dump(mode="json"),
                )

            if at == "propose_command":
                cmd_cwd = str(payload.get("cwd", "."))
                inst, inst_files, inst_dirs = self._refresh_instructions_for_targets(
                    target_dirs=[".", cmd_cwd, *self._broker.active_instruction_dirs()],
                )
                prop = self._broker.create_command_proposal(
                    command=payload,
                    current_instruction_sha256=inst.sha256 or "",
                    current_instruction_files=inst_files,
                    checked_instruction_dirs=inst_dirs,
                )
                decided = await self._broker.wait_for_decision(prop.id)
                cmd_info = decided.payload.get("command", {})
                if decided.state != "approved":
                    return self._error_outcome(
                        action_id=action_id,
                        action_type=at,
                        code="PROPOSAL_DENIED",
                        message=f"command proposal ended in state '{decided.state}'",
                        extra={"proposal_id": decided.id, "digest": decided.digest, "state": decided.state},
                    )
                return self._ok_outcome(
                    action_id=action_id,
                    action_type=at,
                    data={
                        "proposal_id": decided.id,
                        "digest": decided.digest,
                        "state": decided.state,
                        "command_id": cmd_info.get("command_id"),
                        "executable_sha256": cmd_info.get("executable_sha256"),
                    },
                )

            if at == "start_command":
                blocked = self._fail_if_command_running(action_id, at)
                if blocked is not None:
                    return blocked
                proposal_id = str(payload["proposal_id"])
                proposal_digest = str(payload["proposal_digest"])
                cmd_cwd = self._broker.get_command_cwd(proposal_id)
                check_dirs: list[str] = [".", *self._broker.active_instruction_dirs()]
                if cmd_cwd is not None:
                    check_dirs.append(cmd_cwd)
                inst, inst_files, _ = self._refresh_instructions_for_targets(target_dirs=check_dirs)
                self._broker.verify_plan_approved(
                    current_run_id=self._task.run_id,
                    current_root_identity=self._workspace.root_identity,
                    current_instruction_sha256=inst.sha256 or "",
                    current_instruction_files=inst_files,
                )
                view = self._commands.start_approved_command(
                    proposal_id=proposal_id,
                    proposal_digest=proposal_digest,
                    current_root_identity=self._workspace.root_identity,
                    current_instruction_sha256=inst.sha256 or "",
                    current_instruction_files=inst_files,
                )
                return self._ok_outcome(
                    action_id=action_id,
                    action_type=at,
                    data=view.model_dump(mode="json"),
                )

            if at == "poll_command":
                view = self._commands.get_command(
                    str(payload["command_id"]), after=int(payload.get("after", 0))
                )
                data = view.model_dump(mode="json")
                out_text = "".join(c.text for c in view.chunks if c.stream == "stdout")
                err_text = "".join(c.text for c in view.chunks if c.stream == "stderr")
                data["stdout_excerpt"], _ = _truncate_utf8(out_text, 4096)
                data["stderr_excerpt"], _ = _truncate_utf8(err_text, 4096)
                return self._ok_outcome(action_id=action_id, action_type=at, data=data)

            if at == "stop_command":
                view, _ = await self._commands.stop_command(str(payload["command_id"]))
                return self._ok_outcome(
                    action_id=action_id,
                    action_type=at,
                    data=view.model_dump(mode="json"),
                )

            if at == "verify_project":
                return await self._execute_verify_project(action_id=action_id, payload=payload)

            if at == "ask_user":
                if not self._allow_user_questions():
                    raise UnsupportedActionError("unsupported action_type: ask_user")
                assert self._conversation is not None and self._conversation.question_broker is not None
                q_view = await self._conversation.question_broker.ask(
                    action_id=action_id,
                    question=str(payload["question"]),
                    options=list(payload.get("options", [])),
                )
                if q_view.state == "answered":
                    return self._ok_outcome(
                        action_id=action_id,
                        action_type="ask_user",
                        data={
                            "action_id": q_view.action_id,
                            "question_id": q_view.id,
                            "digest": q_view.digest,
                            "state": q_view.state,
                            "answer": q_view.answer,
                        },
                    )
                return self._error_outcome(
                    action_id=action_id,
                    action_type="ask_user",
                    code=q_view.reason_code or f"QUESTION_{q_view.state.upper()}",
                    message=f"question ended in state '{q_view.state}'",
                    effect_applied=False,
                    extra={
                        "action_id": q_view.action_id,
                        "question_id": q_view.id,
                        "digest": q_view.digest,
                        "state": q_view.state,
                    },
                )

            if self._extensions is not None:
                from forecontract_app.extension_runtime import EXTENSION_ACTION_NAMES

                if at in EXTENSION_ACTION_NAMES:
                    ext_res = await self._extensions.execute(action)
                    if ext_res.get("tool_succeeded") is True:
                        return self._ok_outcome(action_id=action_id, action_type=at, data=ext_res)
                    err_code = str(ext_res.get("error_code") or "EXTENSION_ERROR")
                    err_msg = str(ext_res.get("error_message") or "extension execution failed")
                    eff = ext_res.get("effect_applied")
                    return self._error_outcome(
                        action_id=action_id,
                        action_type=at,
                        code=err_code,
                        message=err_msg,
                        effect_applied=False if eff is False else None,
                        extra=ext_res,
                    )

            raise UnsupportedActionError(f"unsupported action_type: {at}")
        except (ProjectError, CodingError) as exc:
            code = getattr(exc, "code", "PROJECT_ERROR")
            msg = getattr(exc, "message", str(exc))
            return self._error_outcome(action_id=action_id, action_type=at, code=str(code), message=msg)

    async def _execute_verify_project(
        self, *, action_id: str, payload: dict[str, Any]
    ) -> ActualOutcome:
        blocked = self._fail_if_command_running(action_id, "verify_project")
        if blocked is not None:
            return blocked
        if not self._scope_complete:
            return self._error_outcome(
                action_id=action_id,
                action_type="verify_project",
                code="SCOPE_INCOMPLETE",
                message="workspace exceeded 10,000 files or 64 MiB limit",
            )

        inst, inst_files, _ = self._refresh_instructions_for_targets(
            target_dirs=[".", *self._broker.active_instruction_dirs()],
        )
        plan_view, plan_spec, expected_prot_shas = self._broker.verify_plan_approved(
            plan_id=str(payload["plan_id"]),
            plan_digest=str(payload["plan_digest"]),
            current_run_id=self._task.run_id,
            current_root_identity=self._workspace.root_identity,
            current_instruction_sha256=inst.sha256 or "",
            current_instruction_files=inst_files,
        )

        # Verify protected paths were not mutated prior to running checks.
        cur_prot_before = self._workspace.hash_protected_paths(expected_prot_shas.keys())
        if cur_prot_before != expected_prot_shas:
            self._verification_status = "unknown"
            self._stale_reason = "protected_sources_modified"
            return self._error_outcome(
                action_id=action_id,
                action_type="verify_project",
                code="VERIFICATION_STALE",
                message="protected test/check sources changed since verification plan approval",
            )

        start_rev = self._workspace.capture_revision()
        if not start_rev.scope_complete:
            self._scope_complete = False
            self._verification_status = "unknown"
            self._stale_reason = "scope_incomplete"
            return self._error_outcome(
                action_id=action_id,
                action_type="verify_project",
                code="SCOPE_INCOMPLETE",
                message="workspace exceeded revision scan limits at verification start",
            )

        records: list[CheckExecutionRecord] = []
        denied_reason: str | None = None
        first_fail_cmd_id: str | None = None
        first_fail_exit: int | None = None
        first_fail_output: str = ""

        for chk in plan_spec.checks:
            chk_dirs = [".", chk.cwd, *self._broker.active_instruction_dirs()]
            inst_chk, inst_chk_files, inst_chk_dirs = self._refresh_instructions_for_targets(
                target_paths=chk.protected_paths,
                target_dirs=chk_dirs,
            )
            cmd_prop = self._broker.create_command_proposal(
                command=CommandProposal(
                    command_id=f"{chk.check_id}-{len(records) + 1}",
                    executable=chk.executable,
                    argv=list(chk.argv),
                    cwd=chk.cwd,
                    timeout_ms=chk.timeout_ms,
                    purpose="verification",
                    explanation=f"Verification check '{chk.check_id}' ({chk.kind}) for plan {plan_spec.plan_id}",
                    check_id=chk.check_id,
                ),
                plan_id=plan_view.id,
                plan_digest=plan_view.digest,
                current_instruction_sha256=inst_chk.sha256 or "",
                current_instruction_files=inst_chk_files,
                checked_instruction_dirs=inst_chk_dirs,
            )
            decided = await self._broker.wait_for_decision(cmd_prop.id)
            if decided.state != "approved" or not decided.trusted_local_execution:
                denied_reason = f"check '{chk.check_id}' proposal ended in state '{decided.state}'"
                break

            try:
                inst_now, inst_now_files, _ = self._refresh_instructions_for_targets(
                    target_paths=chk.protected_paths,
                    target_dirs=chk_dirs,
                )
                cur_prot_now = self._workspace.hash_protected_paths(expected_prot_shas.keys())
                if cur_prot_now != expected_prot_shas:
                    self._verification_status = "unknown"
                    self._stale_reason = "protected_sources_modified"
                    return self._error_outcome(
                        action_id=action_id,
                        action_type="verify_project",
                        code="VERIFICATION_STALE",
                        message="protected test/check sources changed while waiting for command approval",
                    )
                cmd_view = await self._commands.run_approved_command(
                    proposal_id=cmd_prop.id,
                    proposal_digest=cmd_prop.digest,
                    check_kind=chk.kind,
                    expected_tests=list(chk.expected_tests),
                    current_root_identity=self._workspace.root_identity,
                    current_instruction_sha256=inst_now.sha256 or "",
                    current_instruction_files=inst_now_files,
                )
            except ProjectError as exc:
                self._verification_status = "unknown"
                self._stale_reason = exc.code
                raise
            stdout_full, stderr_full = self._commands.get_captured_output(cmd_view.command_id)
            res_dir = self._commands.get_result_dir(cmd_view.command_id)
            prot_after = self._workspace.hash_protected_paths(expected_prot_shas.keys())

            junit_ok = False
            observed_tests: list[Any] = []
            xml_path_str: str | None = None
            fail_reason: str | None = None

            if not cmd_view.cleanup_ok or cmd_view.remaining_active != 0 or cmd_view.output_delivery_failed:
                fail_reason = "cleanup_or_output_incomplete"
            elif cmd_view.timed_out or cmd_view.state == "cancelled":
                fail_reason = f"command_{cmd_view.state}"
            elif cmd_view.exit_code != 0:
                fail_reason = f"nonzero_exit:{cmd_view.exit_code}"
            elif chk.kind == "junit_exact":
                assert res_dir is not None
                junit_ok, observed_tests, xml_path_str, fail_reason = parse_junit_xml_in_result_dir(
                    result_dir=res_dir,
                    expected_tests=list(chk.expected_tests),
                )

            passed = (
                fail_reason is None
                and cmd_view.cleanup_ok
                and cmd_view.remaining_active == 0
                and not cmd_view.output_delivery_failed
                and cmd_view.exit_code == 0
                and (chk.kind == "exit_code" or junit_ok)
            )
            if not passed and first_fail_cmd_id is None:
                first_fail_cmd_id = cmd_view.command_id
                first_fail_exit = cmd_view.exit_code
                combined = (stdout_full + "\n" + stderr_full).strip()
                first_fail_output, _ = _truncate_utf8(combined, 4096)

            records.append(
                CheckExecutionRecord(
                    check_id=chk.check_id,
                    kind=chk.kind,
                    command_id=cmd_view.command_id,
                    proposal_id=cmd_prop.id,
                    proposal_digest=cmd_prop.digest,
                    executable=cmd_prop.payload["command"]["executable"],
                    executable_sha256=cmd_prop.payload["command"]["executable_sha256"],
                    argv=list(chk.argv),
                    cwd=chk.cwd,
                    result_dir=str(res_dir) if res_dir is not None else None,
                    junit_xml_path=xml_path_str,
                    exit_code=cmd_view.exit_code,
                    timed_out=cmd_view.timed_out,
                    cancelled=(cmd_view.state == "cancelled"),
                    cleanup_ok=cmd_view.cleanup_ok,
                    remaining_active=cmd_view.remaining_active,
                    output_bytes_seen=cmd_view.bytes_seen,
                    output_delivery_failed=cmd_view.output_delivery_failed,
                    start_revision=start_rev.revision,
                    end_revision=start_rev.revision,  # patched cleanly once end_rev captured below
                    protected_shas_before=cur_prot_before,
                    protected_shas_after=prot_after,
                    expected_tests=list(chk.expected_tests),
                    observed_tests=observed_tests,
                    junit_valid=junit_ok if chk.kind == "junit_exact" else True,
                    passed=passed,
                    failure_reason=fail_reason,
                )
            )

        end_rev = self._workspace.capture_revision()
        self._current_revision = end_rev.revision
        self._scope_complete = end_rev.scope_complete and self._scope_complete
        records = [r.model_copy(update={"end_revision": end_rev.revision}) for r in records]
        self._latest_checks = records
        self._verified_end_revision = end_rev.revision
        self._verified_plan_id = plan_spec.plan_id or plan_view.id
        self._verified_plan_digest = plan_view.digest
        self._verified_plan_rev_seen = self._broker.plan_revision
        self._verified_ext_mutation_seq = (
            self._extensions.mutation_seq if self._extensions is not None else 0
        )

        if denied_reason is not None:
            self._verification_status = "failed"
            self._stale_reason = denied_reason
            self._unverified_reasons = [denied_reason]
            return self._error_outcome(
                action_id=action_id,
                action_type="verify_project",
                code="VERIFICATION_DENIED",
                message=denied_reason,
                extra={
                    "verification_status": "failed",
                    "plan_id": self._verified_plan_id,
                    "plan_digest": self._verified_plan_digest,
                },
            )

        ev = self.build_evidence(core_completed=True)
        eval_res = evaluate_project_result(task=self._task, evidence=ev)
        self._verification_status = eval_res.verification
        self._unverified_reasons = list(eval_res.unverified_reasons)
        self._stale_reason = None if eval_res.verification == "passed" else eval_res.reason_code

        if eval_res.verification != "passed":
            return self._error_outcome(
                action_id=action_id,
                action_type="verify_project",
                code=eval_res.reason_code,
                message="; ".join(eval_res.unverified_reasons) or eval_res.reason_code,
                extra={
                    "verification_status": eval_res.verification,
                    "plan_id": self._verified_plan_id,
                    "plan_digest": self._verified_plan_digest,
                    "command_id": first_fail_cmd_id,
                    "exit_code": first_fail_exit,
                    "stderr_or_stdout": first_fail_output,
                    "output_excerpt": first_fail_output,
                    "checks": {c.check_id: c.model_dump(mode="json") for c in records},
                },
            )

        return self._ok_outcome(
            action_id=action_id,
            action_type="verify_project",
            data={
                "verification_status": "passed",
                "plan_id": self._verified_plan_id,
                "plan_digest": self._verified_plan_digest,
                "end_revision": end_rev.revision,
                "checks": {c.check_id: c.model_dump(mode="json") for c in records},
            },
        )

    def build_evidence(self, *, core_completed: bool) -> ProjectVerificationEvidence:
        """Collect fresh disk fingerprints & bounded diff to feed pure ``evaluate_project_result``."""
        diff = self._workspace.diff_from_baseline()
        self._current_revision = diff.final_revision
        scope_complete = self._scope_complete and diff.scope_complete

        plan_tuple = self._broker.active_plan()
        approved_plan = plan_tuple[1] if plan_tuple is not None else None
        plan_id = self._verified_plan_id or (approved_plan.plan_id if approved_plan is not None else None)
        plan_digest = (
            plan_tuple[0].digest if plan_tuple is not None else self._verified_plan_digest
        )
        expected_prot = plan_tuple[2] if plan_tuple is not None else {}

        prot_final: dict[str, str] = {}
        stale_flag = False
        if expected_prot:
            try:
                prot_final = self._workspace.hash_protected_paths(expected_prot.keys())
            except ProjectError:
                stale_flag = True

        if (
            self._verified_end_revision is not None
            and self._verified_end_revision != diff.final_revision
        ):
            stale_flag = True
        if (
            plan_tuple is not None
            and self._verified_plan_digest is not None
            and plan_tuple[0].digest != self._verified_plan_digest
        ):
            stale_flag = True
        if (
            self._extensions is not None
            and self._verified_end_revision is not None
            and self._extensions.mutation_seq != self._verified_ext_mutation_seq
        ):
            stale_flag = True

        cleanup_ok, rem_active = self._commands.cleanup_summary()
        active_now = len(self._commands.active_command_ids())
        total_rem = rem_active + active_now
        if (
            self._extensions is not None
            and self._extension_cleanup is not None
            and (
                not self._extension_cleanup.cleanup_ok
                or self._extension_cleanup.remaining_active > 0
            )
        ):
            cleanup_ok = False
            total_rem += self._extension_cleanup.remaining_active
        if total_rem > 0:
            cleanup_ok = False

        changed_paths = [
            {
                "path": e.path,
                "operation": e.operation,
                "before_sha256": e.before_sha256,
                "after_sha256": e.after_sha256,
                "source": e.source,
            }
            for e in diff.entries
        ]
        return ProjectVerificationEvidence(
            baseline_revision=diff.baseline_revision,
            final_revision=diff.final_revision,
            scope_complete=scope_complete,
            core_completed=core_completed,
            plan_id=plan_id,
            plan_digest=plan_digest,
            approved_plan=approved_plan,
            protected_shas_expected=expected_prot,
            protected_shas_final=prot_final,
            checks=list(self._latest_checks),
            changed_paths=changed_paths,
            diff=diff,
            diff_truncated=diff.diff_truncated,
            cleanup_ok=cleanup_ok,
            remaining_active=total_rem,
            unverified_reasons=list(self._unverified_reasons),
            stale_after_verification=stale_flag,
        )
