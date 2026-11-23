"""Native Core + Coding execution bridge and sanitized event projection."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from forecontract.tracing import JsonlTraceStore
from forecontract.tracing.models import TraceRecord
from forecontract_coding import CodingConfig, CodingRunResult, CodingTask
from forecontract_coding.agent import run_coding_task
from forecontract_llm import ModelRequest, ModelResponse, PlannerConfig
from forecontract_llm.provider import ModelProvider

from forecontract_app.models import RunState, VerificationState
from forecontract_app.project_commands import ProjectCommandRunner
from forecontract_app.project_models import ProjectRunResult, ProjectTask
from forecontract_app.project_permissions import PermissionBroker
from forecontract_app.project_runtime import run_project_task
from forecontract_app.project_workspace import ProjectWorkspace

if TYPE_CHECKING:
    from forecontract_app.extension_runtime import ExtensionRuntime
    from forecontract_app.session_context import ConversationRuntime
    from forecontract_app.session_usage import UsageAccumulator


def _project_tool_result(raw: object) -> dict[str, Any] | None:
    """Return a bounded, sanitized projection of ``observation.state.last_tool_result``."""
    if not isinstance(raw, dict):
        return None
    projected: dict[str, Any] = {}
    for key in (
        "action_id",
        "action_type",
        "path",
        "operation",
        "command_id",
        "tool_succeeded",
        "exit_code",
        "timed_out",
        "cancelled",
        "read_back_match",
        "before_revision",
        "after_revision",
        "before_sha256",
        "written_sha256",
        "read_back_sha256",
        "file_sha256",
        "match_count",
        "sequence",
        "proposal_id",
        "proposal_digest",
        "plan_id",
        "plan_digest",
        "command_index",
        "status",
        "state",
        "verification_status",
        "bytes_seen",
        "remaining_active",
        "call_id",
        "server_id",
        "tool_name",
        "args_sha256",
        "schema_sha256",
        "is_error",
        "effect",
        "sha256",
        "error_code",
        "id",
        "byte_offset",
        "next_offset",
        "eof",
        "total_bytes",
        "count",
    ):
        if key in raw:
            projected[key] = raw[key]
    for excerpt_key in ("stdout", "stderr", "content", "text", "error_message"):
        val = raw.get(excerpt_key)
        if isinstance(val, str):
            raw_bytes = val.encode("utf-8")
            if len(raw_bytes) > 1024:
                projected[excerpt_key] = raw_bytes[:1024].decode("utf-8", errors="ignore")
                projected["truncated"] = True
                projected["original_bytes"] = len(raw_bytes)
            else:
                projected[excerpt_key] = val
    return projected


def _project_trace_record(record: TraceRecord) -> dict[str, Any]:
    """Return a sanitized summary projection of a committed Core trace record."""
    data: dict[str, Any] = {
        "record_id": record.record_id,
        "kind": record.kind,
        "sequence": record.sequence,
    }
    for attr in (
        "step_id",
        "contract_id",
        "attempt_id",
        "execution_status",
        "verification_status",
        "terminal_status",
        "reason_code",
        "error_code",
        "scope",
    ):
        val = getattr(record, attr, None)
        if val is not None:
            data[attr] = val.value if hasattr(val, "value") else val
    return data


class _AppTraceWriter:
    """Persist Core trace records via ``JsonlTraceStore`` first, then publish summaries."""

    def __init__(
        self,
        *,
        store: JsonlTraceStore,
        emit: Callable[[str, dict[str, Any]], None],
        usage_accumulator: UsageAccumulator | None = None,
    ) -> None:
        self._store = store
        self._emit = emit
        self._usage = usage_accumulator
        self._steps_seen: set[str] = set()

    def append(self, record: TraceRecord) -> None:
        self._store.append(record)
        if self._usage is not None:
            step_id = getattr(record, "step_id", None)
            if isinstance(step_id, str) and step_id:
                self._steps_seen.add(step_id)
                self._usage.set_steps_used(len(self._steps_seen))
        self._emit("core_trace", _project_trace_record(record))


class _ObservationEmittingProvider:
    """Transparent ``ModelProvider`` wrapper that emits real observation tool feedback."""

    def __init__(
        self,
        *,
        inner: ModelProvider,
        emit: Callable[[str, dict[str, Any]], None],
        usage_accumulator: UsageAccumulator | None = None,
    ) -> None:
        self._inner = inner
        self._emit = emit
        self._usage = usage_accumulator
        self.pending_inner_tasks: set[asyncio.Task[ModelResponse]] = set()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        state = request.observation.state if request.observation is not None else {}
        if isinstance(state, dict):
            projected = _project_tool_result(state.get("last_tool_result"))
            if projected is not None:
                self._emit("tool_result", projected)
        self._emit("planning_started", {"request_id": request.request_id})
        if self._usage is not None:
            self._usage.begin_call(request_id=request.request_id)
        task = asyncio.create_task(self._inner.complete(request))
        self.pending_inner_tasks.add(task)
        try:
            resp = await task
            if self._usage is not None and isinstance(resp, ModelResponse):
                self._usage.finish_call(request_id=request.request_id, usage=resp.usage)
            return resp
        finally:
            if task.done():
                self.pending_inner_tasks.discard(task)

    async def wait_unsettled(self, timeout_seconds: float) -> bool:
        """Wait boundedly for any orphaned inner provider tasks after planner cancellation."""
        active = [t for t in self.pending_inner_tasks if not t.done()]
        if not active:
            self.pending_inner_tasks.clear()
            return True
        if timeout_seconds <= 0:
            return False
        _, pending = await asyncio.wait(active, timeout=timeout_seconds)
        for t in list(self.pending_inner_tasks):
            if t.done():
                self.pending_inner_tasks.discard(t)
                if not t.cancelled():
                    t.exception()
        return len(pending) == 0


def evaluate_run_outcome(result: CodingRunResult) -> tuple[RunState, VerificationState, str]:
    """Map real ``CodingRunResult`` onto strict ``(state, verification, reason_code)``."""
    acc = result.acceptance
    core_terminal = result.terminal_status
    acc_passed = (
        acc.status == "passed"
        and acc.revision is not None
        and acc.revision == result.final_workspace_revision
        and acc.stale_reason is None
    )

    if core_terminal == "completed":
        if acc_passed:
            return "succeeded", "passed", result.reason_code
        if acc.status == "failed":
            return "failed", "failed", "VERIFICATION_FAILED"
        if acc.revision != result.final_workspace_revision or acc.stale_reason is not None:
            return "failed", "unknown", "ACCEPTANCE_STALE"
        return "failed", "unknown", "VERIFICATION_NOT_PASSED"

    if core_terminal == "cancelled":
        ver: VerificationState = "failed" if acc.status == "failed" else "unknown"
        return "cancelled", ver, result.reason_code

    ver_failed: VerificationState = "failed" if acc.status == "failed" else "unknown"
    return "failed", ver_failed, result.reason_code


async def execute_run(
    *,
    task: CodingTask,
    config: CodingConfig,
    provider: ModelProvider,
    planner_config: PlannerConfig,
    trace_path: Path,
    emit: Callable[[str, dict[str, Any]], None],
    usage_accumulator: UsageAccumulator | None = None,
) -> CodingRunResult:
    """Execute one bounded run directly through ``forecontract_coding.agent.run_coding_task``."""
    store = JsonlTraceStore(trace_path)
    trace_writer = _AppTraceWriter(store=store, emit=emit, usage_accumulator=usage_accumulator)
    wrapped_provider = _ObservationEmittingProvider(
        inner=provider, emit=emit, usage_accumulator=usage_accumulator
    )
    res = await run_coding_task(
        task=task,
        config=config,
        provider=wrapped_provider,
        planner_config=planner_config,
        trace_writer=trace_writer,
    )
    if usage_accumulator is not None and isinstance(getattr(res, "steps_started", None), int):
        usage_accumulator.set_steps_used(int(res.steps_started))
    return res


def evaluate_project_run_outcome(
    result: ProjectRunResult,
) -> tuple[RunState, VerificationState, str]:
    """Map ``ProjectRunResult`` onto strict ``(state, verification, reason_code)``."""
    return result.terminal_status, result.verification, result.reason_code


async def execute_project_run(
    *,
    task: ProjectTask,
    provider: ModelProvider,
    planner_config: PlannerConfig,
    permissions: PermissionBroker,
    trace_path: Path,
    emit: Callable[[str, dict[str, Any]], None],
    workspace: ProjectWorkspace | None = None,
    command_runner: ProjectCommandRunner | None = None,
    conversation: ConversationRuntime | None = None,
    extensions: ExtensionRuntime | None = None,
) -> ProjectRunResult:
    """Execute one bounded existing-project run through Core ``RuntimeEngine``."""
    store = JsonlTraceStore(trace_path)
    usage_acc = conversation.usage_accumulator if conversation is not None else None
    trace_writer = _AppTraceWriter(store=store, emit=emit, usage_accumulator=usage_acc)
    wrapped_provider: ModelProvider = (
        provider
        if isinstance(provider, _ObservationEmittingProvider)
        else _ObservationEmittingProvider(inner=provider, emit=emit, usage_accumulator=usage_acc)
    )
    return await run_project_task(
        task=task,
        provider=wrapped_provider,
        planner_config=planner_config,
        permissions=permissions,
        trace_writer=trace_writer,
        emit=emit,
        workspace=workspace,
        command_runner=command_runner,
        conversation=conversation,
        extensions=extensions,
    )
