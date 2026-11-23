"""Native Core ``RuntimeEngine`` + ``LLMPlanner`` bridge for existing local projects."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from forecontract.models.actions import ExpectedOutcome, Forecast
from forecontract.models.observations import Observation
from forecontract.recovery import RecoveryEngine
from forecontract.runtime import (
    MonotonicBudgetSource,
    RunRequest,
    RuntimeEngine,
    RuntimeTimingPolicy,
)
from forecontract.verification.models import TaskCompletionCriteria
from forecontract_llm import LLMPlanner, PlannerConfig, PlannerContext
from forecontract_llm.provider import ModelProvider

from forecontract_app.project_adapter import ProjectCodingAdapter
from forecontract_app.project_commands import ProjectCommandRunner
from forecontract_app.project_models import (
    PROJECT_MAX_COMMAND_ARG_BYTES,
    PROJECT_MAX_COMMAND_ARGS,
    PROJECT_MAX_COMMAND_ARGV_TOTAL_BYTES,
    PROJECT_MAX_COMMAND_TIMEOUT_MS,
    PROJECT_SCHEMA_VERSION,
    ProjectRunResult,
    ProjectTask,
)
from forecontract_app.project_permissions import PermissionBroker
from forecontract_app.project_verification import (
    PROJECT_STEP_VERIFIER,
    ProjectContractHooksFactory,
    ProjectDomainSupport,
    ProjectStepVerifier,
    ProjectTaskCompletionOracle,
    ProjectTaskCriteriaValidator,
    evaluate_project_result,
)
from forecontract_app.project_workspace import ProjectWorkspace

if TYPE_CHECKING:
    from forecontract_app.extension_runtime import ExtensionRuntime
    from forecontract_app.session_context import ConversationRuntime
    from forecontract_app.session_usage import UsageAccumulator

_PROJECT_ACTION_GUIDANCE: dict[str, dict[str, Any]] = {
    "list_directory": {
        "description": "List non-excluded directory entries with deterministic pagination cursor.",
        "required": [],
        "properties": {
            "path": {"type": "string"},
            "cursor": {"type": ["string", "null"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        "additionalProperties": False,
    },
    "find_paths": {
        "description": "Find non-excluded workspace paths matching a glob pattern (resumable batch).",
        "required": [],
        "properties": {
            "pattern": {"type": "string"},
            "cursor": {"type": ["string", "null"]},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200},
        },
        "additionalProperties": False,
    },
    "read_file": {
        "description": "Read bounded UTF-8 file chunk (<=16 KiB) with SHA-256 and byte continuation.",
        "required": ["path"],
        "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer", "minimum": 1},
            "max_lines": {"type": "integer", "minimum": 1, "maximum": 200},
            "byte_offset": {"type": "integer", "minimum": 0},
            "expected_sha256": {"type": ["string", "null"]},
        },
        "additionalProperties": False,
    },
    "search_text": {
        "description": "Deterministic bounded literal search across non-excluded UTF-8 files.",
        "required": ["query"],
        "properties": {
            "query": {"type": "string"},
            "paths": {"type": ["array", "null"]},
            "cursor": {"type": ["string", "null"]},
            "max_matches": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "additionalProperties": False,
    },
    "propose_scope": {
        "description": (
            "Request user approval for explicit write scope, protected paths, and verification summary. "
            "operations must be non-empty and every element must be exactly 'create' or 'modify'; "
            "at least one exact_paths or write_prefixes entry must contain the target; "
            "verification_plan_summary is required and limited to 1..1024 characters "
            "(it is only a summary and does not grant plan_approved—after scope_approved=true and "
            "plan_approved=false, call propose_verification and wait for actual approval before edit_file)."
        ),
        "required": ["operations", "verification_plan_summary"],
        "properties": {
            "exact_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Exact file paths to write; at least one must contain the target when "
                    "write_prefixes is empty."
                ),
            },
            "write_prefixes": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Directory prefixes to write under; at least one must contain the target "
                    "when exact_paths is empty."
                ),
            },
            "operations": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string", "enum": ["create", "modify"]},
                "description": "Non-empty list; each element must be exactly 'create' or 'modify'.",
            },
            "protected_paths": {"type": "array", "items": {"type": "string"}},
            "verification_plan_summary": {
                "type": "string",
                "minLength": 1,
                "maxLength": 1024,
            },
        },
        "example_forecast": {
            "schema_version": PROJECT_SCHEMA_VERSION,
            "intent": "request modify scope for calc.py before editing",
            "decision_summary": "propose_scope for calc.py (host handles approval)",
            "proposed_action": {
                "schema_version": PROJECT_SCHEMA_VERSION,
                "action_type": "propose_scope",
                "payload": {
                    "exact_paths": ["calc.py"],
                    "operations": ["modify"],
                    "verification_plan_summary": "run calc tests",
                },
                "metadata": {},
            },
            "expected_outcomes": [
                {
                    "schema_version": PROJECT_SCHEMA_VERSION,
                    "outcome_type": "project_tool_result",
                    "payload": {"action_type": "propose_scope", "require_success": True},
                    "required": True,
                }
            ],
        },
        "additionalProperties": False,
    },
    "propose_verification": {
        "description": (
            "Request user approval for a concrete non-empty VerificationPlan. "
            "After scope_approved=true while plan_approved=false, call propose_verification (read-only exploration "
            "remains allowed) and wait for actual approval (plan_approved=true) before edit_file; submitting a "
            "proposal or verification_plan_summary is not approval. "
            "checks must contain at least 1 check object with unique check_id values across the plan. "
            "Each check requires check_id, kind ('junit_exact' or 'exit_code'), executable, and timeout_ms. "
            "When kind='junit_exact', expected_tests must be non-empty with unique (classname, name) pairs "
            "and at least one argv entry must include literal '{result_dir}'. "
            "When kind='exit_code', expected_tests must be omitted or empty."
        ),
        "required": ["checks"],
        "properties": {
            "plan_id": {
                "type": ["string", "null"],
                "minLength": 1,
                "x-non-blank-when-non-null": True,
                "x-no-nul": True,
                "description": "Optional non-blank, NUL-free plan identifier when non-null.",
            },
            "summary": {"type": "string", "maxLength": 1024},
            "checks": {
                "type": "array",
                "minItems": 1,
                "description": "Non-empty list of verification checks; check_id must be unique in the plan.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["check_id", "kind", "executable", "timeout_ms"],
                    "properties": {
                        "check_id": {
                            "type": "string",
                            "minLength": 1,
                            "maxLength": 64,
                            "pattern": "^[A-Za-z0-9._-]{1,64}$",
                            "description": "Unique check identifier within the VerificationPlan.",
                        },
                        "kind": {
                            "type": "string",
                            "enum": ["junit_exact", "exit_code"],
                            "description": (
                                "'junit_exact' requires non-empty expected_tests and '{result_dir}' in argv; "
                                "'exit_code' requires expected_tests omitted or empty."
                            ),
                        },
                        "executable": {
                            "type": "string",
                            "minLength": 1,
                            "x-non-blank": True,
                            "x-no-nul": True,
                            "description": "Required non-blank, NUL-free executable path or name.",
                        },
                        "argv": {
                            "type": "array",
                            "maxItems": PROJECT_MAX_COMMAND_ARGS,
                            "x-unit": "utf8_bytes",
                            "x-item-max-utf8-bytes": PROJECT_MAX_COMMAND_ARG_BYTES,
                            "x-total-max-utf8-bytes": PROJECT_MAX_COMMAND_ARGV_TOTAL_BYTES,
                            "x-no-nul": True,
                            "x-allowed-brace-placeholders": ["{result_dir}"],
                            "x-result-dir-forbids-dotdot-after-slash": True,
                            "items": {
                                "type": "string",
                                "x-unit": "utf8_bytes",
                                "x-max-utf8-bytes": PROJECT_MAX_COMMAND_ARG_BYTES,
                                "x-no-nul": True,
                                "description": (
                                    f"Argument string <= {PROJECT_MAX_COMMAND_ARG_BYTES} UTF-8 bytes, "
                                    "NUL-free, only {result_dir} brace placeholder allowed, and suffix after "
                                    "{result_dir} split by '/' or '\\' must not contain '..'."
                                ),
                            },
                            "description": (
                                f"Command arguments: max {PROJECT_MAX_COMMAND_ARGS} items; each item <= "
                                f"{PROJECT_MAX_COMMAND_ARG_BYTES} UTF-8 bytes and sum across all items <= "
                                f"{PROJECT_MAX_COMMAND_ARGV_TOTAL_BYTES} UTF-8 bytes; no NUL bytes; only "
                                "{result_dir} brace placeholder allowed with no '..' segment after '/' or '\\'; "
                                "for kind='junit_exact', at least one entry must include literal '{result_dir}'."
                            ),
                        },
                        "cwd": {
                            "type": "string",
                            "minLength": 1,
                            "default": ".",
                            "x-non-blank": True,
                            "x-no-nul": True,
                            "description": "Non-blank, NUL-free working directory (default '.').",
                        },
                        "timeout_ms": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": PROJECT_MAX_COMMAND_TIMEOUT_MS,
                        },
                        "protected_paths": {
                            "type": "array",
                            "items": {
                                "type": "string",
                                "minLength": 1,
                                "x-non-blank": True,
                                "x-no-nul": True,
                                "description": "Non-blank, NUL-free protected path.",
                            },
                        },
                        "expected_tests": {
                            "type": "array",
                            "description": (
                                "Required non-empty with unique (classname, name) pairs when kind='junit_exact'; "
                                "must be omitted or empty when kind='exit_code'."
                            ),
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["classname", "name"],
                                "properties": {
                                    "classname": {
                                        "type": "string",
                                        "minLength": 1,
                                        "x-non-blank": True,
                                        "x-no-nul": True,
                                        "description": "Non-blank, NUL-free test class name.",
                                    },
                                    "name": {
                                        "type": "string",
                                        "minLength": 1,
                                        "x-non-blank": True,
                                        "x-no-nul": True,
                                        "description": "Non-blank, NUL-free test case name.",
                                    },
                                },
                            },
                        },
                    },
                    "allOf": [
                        {
                            "if": {
                                "properties": {"kind": {"const": "junit_exact"}},
                                "required": ["kind"],
                            },
                            "then": {
                                "required": ["expected_tests", "argv"],
                                "properties": {
                                    "expected_tests": {"minItems": 1},
                                    "argv": {
                                        "contains": {"type": "string", "pattern": r"\{result_dir\}"}
                                    },
                                },
                            },
                        },
                        {
                            "if": {
                                "properties": {"kind": {"const": "exit_code"}},
                                "required": ["kind"],
                            },
                            "then": {
                                "properties": {
                                    "expected_tests": {"maxItems": 0},
                                },
                            },
                        },
                    ],
                },
            },
        },
        "additionalProperties": False,
    },
    "create_file": {
        "description": "Atomically create a new UTF-8 file inside approved write scope.",
        "required": ["path", "content"],
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "additionalProperties": False,
    },
    "edit_file": {
        "description": (
            "Atomically edit an existing UTF-8 file matching exact SHA-256 and old_text inside approved write scope. "
            "Requires both scope_approved=true and plan_approved=true before editing: when scope_approved=true and "
            "plan_approved=false, do not call edit_file yet—call propose_verification first and wait for actual "
            "plan approval (verification_plan_summary or a pending proposal is not approval; read-only exploration "
            "remains allowed)."
        ),
        "required": ["path", "expected_sha256", "old_text", "new_text"],
        "properties": {
            "path": {"type": "string"},
            "expected_sha256": {"type": "string"},
            "old_text": {"type": "string"},
            "new_text": {"type": "string"},
        },
        "additionalProperties": False,
    },
    "propose_command": {
        "description": "Request single-use user approval (+ trusted_local_execution) for an exact local command.",
        "required": ["executable", "argv", "cwd", "timeout_ms", "purpose", "explanation"],
        "properties": {
            "command_id": {"type": ["string", "null"]},
            "executable": {"type": "string"},
            "argv": {"type": "array"},
            "cwd": {"type": "string"},
            "timeout_ms": {"type": "integer", "minimum": 1},
            "purpose": {"type": "string", "enum": ["diagnostic", "verification"]},
            "explanation": {"type": "string"},
            "executable_sha256": {"type": ["string", "null"]},
            "check_id": {"type": ["string", "null"]},
        },
        "additionalProperties": False,
    },
    "start_command": {
        "description": "Start one pre-approved command asynchronously inside a Windows Job Object.",
        "required": ["proposal_id", "proposal_digest"],
        "properties": {
            "proposal_id": {"type": "string"},
            "proposal_digest": {"type": "string"},
        },
        "additionalProperties": False,
    },
    "poll_command": {
        "description": "Poll bounded incremental stdout/stderr output of a started command.",
        "required": ["command_id"],
        "properties": {
            "command_id": {"type": "string"},
            "after": {"type": "integer", "minimum": 0},
        },
        "additionalProperties": False,
    },
    "stop_command": {
        "description": "Terminate a running command and its child process tree via Job Object.",
        "required": ["command_id"],
        "properties": {"command_id": {"type": "string"}},
        "additionalProperties": False,
    },
    "verify_project": {
        "description": "Execute every check in the approved VerificationPlan and validate JUnit XML & revision integrity.",
        "required": ["plan_id", "plan_digest"],
        "properties": {
            "plan_id": {"type": "string"},
            "plan_digest": {"type": "string"},
        },
        "additionalProperties": False,
    },
}

_ASK_USER_SCHEMA_GUIDANCE: dict[str, Any] = {
    "description": "Ask user one bounded clarifying question (does not grant write or command permissions).",
    "required": ["question"],
    "properties": {
        "question": {"type": "string", "minLength": 1, "maxLength": 4096},
        "options": {"type": "array", "maxItems": 4, "items": {"type": "string", "maxLength": 128}},
    },
    "additionalProperties": False,
}

_PROJECT_OUTCOME_GUIDANCE: dict[str, dict[str, Any]] = {
    "project_tool_result": {
        "description": "Correlates tool execution with contracted action_id.",
        "required": ["action_type"],
        "properties": {"action_type": {"type": "string"}, "require_success": {"type": "boolean"}},
        "additionalProperties": False,
    },
    "project_file_written": {
        "description": "Confirms atomic file write & read-back match.",
        "required": ["path"],
        "properties": {"path": {"type": "string"}},
        "additionalProperties": False,
    },
    "project_command_result": {
        "description": "Confirms approved command execution outcome.",
        "required": ["command_id"],
        "properties": {"command_id": {"type": "string"}},
        "additionalProperties": False,
    },
    "project_task_verified": {
        "description": "Confirms approved VerificationPlan checks, JUnit XML cases, and live disk revision match.",
        "required": [],
        "properties": {},
        "additionalProperties": False,
    },
}


class _HostProjectActionPlanner:
    """Binds a host-generated unique ``action_id`` onto each decoded LLM ``Forecast``."""

    def __init__(self, *, inner: LLMPlanner, run_id: str) -> None:
        self._inner = inner
        self._run_digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:12]
        self._seq = 0

    async def plan(self, observation: Observation) -> Forecast:
        forecast = await self._inner.plan(observation)
        self._seq += 1
        action_id = f"proj-{self._run_digest}-act-{self._seq:04d}"
        bound = forecast.proposed_action.model_copy(
            update={"metadata": {"action_id": action_id}}
        )
        return forecast.model_copy(update={"proposed_action": bound})


class _ToolEventProjectorProvider:
    """Optional provider wrapper projecting observation tool_result, planning, and usage events cleanly."""

    def __init__(
        self,
        *,
        inner: ModelProvider,
        emit: Callable[[str, dict], None],
        usage_accumulator: UsageAccumulator | None = None,
    ) -> None:
        self._inner = inner
        self._emit = emit
        self._usage = usage_accumulator

    async def complete(self, request: Any) -> Any:
        obs = getattr(request, "observation", None)
        state = getattr(obs, "state", None)
        if isinstance(state, dict):
            last = state.get("last_tool_result")
            if isinstance(last, dict):
                self._emit("tool_result", dict(last))
        req_id = getattr(request, "request_id", "")
        self._emit("planning_started", {"request_id": req_id})
        if self._usage is not None and isinstance(req_id, str) and req_id:
            self._usage.begin_call(request_id=req_id)
        resp = await self._inner.complete(request)
        if self._usage is not None and isinstance(req_id, str) and req_id and hasattr(resp, "usage"):
            self._usage.finish_call(request_id=req_id, usage=getattr(resp, "usage", None))
        return resp


class _CallableMonotonicClock:
    """Adapt a callable ``() -> int`` nanosecond supplier to Core's ``now_ns()`` clock protocol."""

    def __init__(self, fn: Callable[[], int]) -> None:
        self._fn = fn

    def now_ns(self) -> int:
        return self._fn()


async def _close_provider_bounded(
    provider: Any,
    *,
    timeout_seconds: float,
    emit: Callable[[str, dict], None],
) -> None:
    target = provider
    seen: set[int] = set()
    while target is not None and id(target) not in seen:
        seen.add(id(target))
        close_fn = getattr(target, "aclose", None)
        if callable(close_fn):
            emit("provider_close_started", {"provider_type": type(target).__name__})
            try:
                res = close_fn()
                if inspect.isawaitable(res):
                    await asyncio.wait_for(res, timeout=timeout_seconds)
                emit("provider_close_finished", {"ok": True, "timed_out": False})
            except asyncio.TimeoutError:
                emit("provider_close_finished", {"ok": False, "timed_out": True})
            except Exception as exc:  # noqa: BLE001
                emit("provider_close_finished", {"ok": False, "timed_out": False, "error": str(exc)})
            return
        target = getattr(target, "_inner", None)


async def run_project_task(
    *,
    task: ProjectTask,
    provider: ModelProvider,
    planner_config: PlannerConfig,
    permissions: PermissionBroker,
    trace_writer: object,
    emit: Callable[[str, dict], None],
    workspace: ProjectWorkspace | None = None,
    command_runner: ProjectCommandRunner | None = None,
    clock: object | None = None,
    monotonic_clock: object | None = None,
    conversation: ConversationRuntime | None = None,
    extensions: ExtensionRuntime | None = None,
) -> ProjectRunResult:
    """Execute one existing local project coding run through Core ``RuntimeEngine`` & ``LLMPlanner``."""
    validated_task = ProjectTask.model_validate(task)
    validated_planner_config = PlannerConfig.model_validate(planner_config)
    adapted_monotonic = (
        _CallableMonotonicClock(monotonic_clock)
        if callable(monotonic_clock) and not hasattr(monotonic_clock, "now_ns")
        else monotonic_clock
    )

    root_path = Path(validated_task.root_identity)
    ws = workspace or ProjectWorkspace(
        root=root_path,
        data_root=validated_task.data_root,
    )
    runner = command_runner or ProjectCommandRunner(
        run_id=validated_task.run_id,
        project_root=ws.root,
        data_root=validated_task.data_root,
        broker=permissions,
        emit=emit,
    )
    adapter = ProjectCodingAdapter(
        task=validated_task,
        workspace=ws,
        broker=permissions,
        command_runner=runner,
        conversation=conversation,
        extensions=extensions,
    )
    allow_questions = conversation is not None and conversation.question_broker is not None
    allow_extensions = extensions is not None
    usage_acc = conversation.usage_accumulator if conversation is not None else None

    criteria = TaskCompletionCriteria(
        schema_version=PROJECT_SCHEMA_VERSION,
        goal_id=validated_task.goal_id,
        conditions=[
            ExpectedOutcome(
                schema_version=PROJECT_SCHEMA_VERSION,
                outcome_type="project_task_verified",
                payload={},
                required=True,
            )
        ],
    )
    run_request = RunRequest(
        schema_version=PROJECT_SCHEMA_VERSION,
        run_id=validated_task.run_id,
        goal_id=validated_task.goal_id,
        task_criteria=criteria,
        max_steps=validated_task.max_steps,
        max_planning_calls=validated_task.max_planning_calls,
        max_retries=0,
        max_replans=validated_task.max_replans,
    )

    wrapped_provider: ModelProvider = (
        provider
        if hasattr(provider, "wait_unsettled")
        else _ToolEventProjectorProvider(inner=provider, emit=emit, usage_accumulator=usage_acc)
    )
    action_guidance = dict(_PROJECT_ACTION_GUIDANCE)
    if allow_questions:
        action_guidance["ask_user"] = _ASK_USER_SCHEMA_GUIDANCE
    if extensions is not None:
        action_guidance.update(extensions.action_guidance())
    planner_context = PlannerContext(
        schema_version=PROJECT_SCHEMA_VERSION,
        run_id=validated_task.run_id,
        goal_id=validated_task.goal_id,
        goal_text=validated_task.goal_text,
        task_criteria=criteria,
        capabilities=adapter.capabilities(),
        action_guidance=action_guidance,
        outcome_guidance=_PROJECT_OUTCOME_GUIDANCE,
    )
    llm_planner = LLMPlanner(
        provider=wrapped_provider,
        context=planner_context,
        config=validated_planner_config,
    )
    bound_planner = _HostProjectActionPlanner(inner=llm_planner, run_id=validated_task.run_id)

    domain_support = ProjectDomainSupport(
        task=validated_task,
        allow_user_questions=allow_questions,
        allow_extensions=allow_extensions,
    )
    hooks_factory = ProjectContractHooksFactory()
    criteria_validator = ProjectTaskCriteriaValidator()
    step_verifier = ProjectStepVerifier()
    task_oracle = ProjectTaskCompletionOracle(task=validated_task)
    recovery_engine = RecoveryEngine(step_verifier_name=PROJECT_STEP_VERIFIER)
    budget_source = MonotonicBudgetSource(
        total_budget_ms=float(validated_task.run_timeout_ms),
        monotonic_clock=adapted_monotonic,
    )
    timing_policy = RuntimeTimingPolicy(
        schema_version=PROJECT_SCHEMA_VERSION,
        run_timeout_ms=float(validated_task.run_timeout_ms),
        cleanup_timeout_ms=2000.0,
        finalize_timeout_ms=1000.0,
    )

    engine = RuntimeEngine(
        adapter=adapter,
        planner=bound_planner,
        domain_support=domain_support,
        budget_source=budget_source,
        trace_writer=trace_writer,
        step_verifier=step_verifier,
        task_oracle=task_oracle,
        recovery_engine=recovery_engine,
        clock=clock,
        timing_policy=timing_policy,
        monotonic_clock=adapted_monotonic,
        contract_hooks_factory=hooks_factory,
        task_criteria_validator=criteria_validator,
    )

    try:
        run_result = await engine.run(request=run_request)
    finally:
        try:
            try:
                await llm_planner.aclose()
            finally:
                await _close_provider_bounded(
                    provider,
                    timeout_seconds=timing_policy.cleanup_timeout_ms / 1000.0,
                    emit=emit,
                )
        finally:
            await adapter.stop()

    if usage_acc is not None:
        usage_acc.set_steps_used(int(run_result.steps_started))

    core_terminal = run_result.terminal_status.value
    core_reason = run_result.reason_code.value
    evidence = adapter.build_evidence(core_completed=(core_terminal == "completed"))
    evaluated = evaluate_project_result(task=validated_task, evidence=evidence)

    common_meta = {
        "core_terminal_status": core_terminal,
        "core_terminal_reason": core_reason,
        "steps_executed": run_result.steps_started,
        "planning_calls_used": run_result.planning_calls,
    }

    if core_terminal == "completed":
        return evaluated.model_copy(update=common_meta)

    if evaluated.terminal_status == "unknown":
        return evaluated.model_copy(update=common_meta)

    if core_terminal == "cancelled":
        ver_cancel = "failed" if evaluated.verification == "failed" else "unknown"
        return evaluated.model_copy(
            update={
                **common_meta,
                "terminal_status": "cancelled",
                "verification": ver_cancel,
                "reason_code": core_reason,
            }
        )

    ver_fail = (
        "not_run"
        if evaluated.verification == "not_run"
        else ("failed" if evaluated.verification == "failed" else "unknown")
    )
    preserved_failure_reasons = {
        "VERIFICATION_STALE",
        "VERIFICATION_FAILED",
        "VERIFICATION_PLAN_MISSING",
        "SCOPE_INCOMPLETE",
        "CLEANUP_INCOMPLETE",
    }
    generic_termination_reasons = {
        "EXECUTION_ERROR",
        "INTERNAL_ERROR",
        "MAX_STEPS_EXCEEDED",
        "MAX_PLANNING_EXHAUSTED",
        "MAX_REPLANS_EXHAUSTED",
        "POLICY_ABORT",
    }
    final_reason = (
        evaluated.reason_code
        if (
            evaluated.reason_code in preserved_failure_reasons
            and core_reason in generic_termination_reasons
        )
        else core_reason
    )
    return evaluated.model_copy(
        update={
            **common_meta,
            "terminal_status": "failed",
            "verification": ver_fail,
            "reason_code": final_reason,
        }
    )
