"""Application service managing controlled projects, sessions, runs, locks, and cancellation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal

from forecontract_coding import CodingRunResult
from forecontract_coding.errors import CodingError
from forecontract_llm import PlannerError
from pydantic import ValidationError

from forecontract_app.chat_models import (
    ChatExecutionOfferView,
    ChatReferenceItem,
    ChatTurnsPage,
    ChatTurnView,
    CommandReference,
    ConfirmChatOfferRequest,
    CreateChatTurnRequest,
    DiffHunkReference,
    ProjectSessionTitlesView,
    ResolvedChatEvidence,
)
from forecontract_app.chat_runtime import (
    ChatProviderFactory,
    OfflineReadonlyChatProvider,
    execute_conversation_chat_turn,
    sanitize_evidence_text,
)
from forecontract_app.demo import (
    ControlledProjectSpec,
    OfflineMetricsProvider,
    build_controlled_coding_task,
    initialize_controlled_project,
)
from forecontract_app.events import RunEventHub
from forecontract_app.extension_models import (
    ExtensionConfig,
    ExtensionError,
    ExtensionProposalDecision,
    ExtensionProposalView,
    McpServerSummary,
    McpServerView,
    McpToolResult,
    RunExtensionsView,
    SkillInfo,
    load_extension_config,
    sanitize_server_summary,
)
from forecontract_app.extension_runtime import ExtensionRuntime
from forecontract_app.live_chat_provider import LiveChatProviderFactory
from forecontract_app.live_provider import LiveProvider, LiveProviderFactory
from forecontract_app.models import (
    AppError,
    AppEvent,
    CreateProjectRequest,
    HostConfig,
    ModelSelection,
    ProjectKind,
    ProjectView,
    ProviderFactory,
    RunSnapshot,
    RunState,
    RunView,
    SessionView,
    StartRunRequest,
    VerificationState,
)
from forecontract_app.project_commands import ProjectCommandRunner
from forecontract_app.project_demo import (
    OfflineProjectProvider,
    build_registered_project_task,
)
from forecontract_app.project_models import (
    CommandView,
    ListDirectoryResult,
    ProjectError,
    ProjectRunResult,
    ProposalDecision,
    ProposalView,
    ReadFileResult,
    RegisterProjectRequest,
)
from forecontract_app.project_permissions import PermissionBroker
from forecontract_app.project_review import (
    ProjectGitDiffView,
    ProjectGitStatusView,
    ProjectReviewService,
)
from forecontract_app.project_workspace import ProjectWorkspace
from forecontract_app.runtime import (
    _AppTraceWriter,
    _ObservationEmittingProvider,
    evaluate_project_run_outcome,
    evaluate_run_outcome,
    execute_project_run,
    execute_run,
)
from forecontract_app.security import (
    DataRootFileLock,
    _validate_raw_local_dir_path,
    check_no_reparse_ancestors,
    ensure_clean_directory,
    paths_overlap,
    validate_host_config,
)
from forecontract_app.session_context import ConversationRuntime, build_context_frame
from forecontract_app.session_models import (
    MAX_RUN_RESULT_SUMMARY_BYTES,
    AnswerQuestionRequest,
    HistoryItem,
    HistoryPage,
    QuestionView,
    SessionContextView,
    SetConstraintsRequest,
    UsageView,
    canonical_json_dumps,
    compute_history_item_sha256,
    sha256_canonical_json,
    truncate_utf8_safe,
    utc_now,
)
from forecontract_app.session_questions import QuestionBroker
from forecontract_app.session_store import SessionStore
from forecontract_app.session_usage import UsageAccumulator
from forecontract_app.skills import SkillCatalog, load_selected_skills

CANCEL_CONFIRMATION_SECONDS: Final[float] = 5.0
SHUTDOWN_CONFIRMATION_SECONDS: Final[float] = 8.0
_TERMINAL_STATES: Final[frozenset[str]] = frozenset({"succeeded", "failed", "cancelled", "unknown"})


@dataclass
class _RegisteredProjectSpec:
    view: ProjectView
    root: Path
    canonical_root: str
    read_grant_id: str


@dataclass
class _RunRecord:
    view: RunView
    hub: RunEventHub
    session: _SessionRecord
    project: _ProjectRecord
    usage: UsageAccumulator | None = None
    persisted_usage: UsageView | None = None
    question_broker: QuestionBroker | None = None
    persisted_questions: list[QuestionView] = field(default_factory=list)
    answer_idempotency: dict[str, tuple[str, str]] = field(default_factory=dict)
    pending_answer_ctx: tuple[str, str] | None = None
    question_seq_counter: int = 0
    task: asyncio.Task[None] | None = None
    exec_task: asyncio.Task[CodingRunResult] | asyncio.Task[ProjectRunResult] | None = None
    wrapped_provider: _ObservationEmittingProvider | None = None
    live_provider: LiveProvider | None = None
    permissions: PermissionBroker | None = None
    command_runner: ProjectCommandRunner | None = None
    workspace: ProjectWorkspace | None = None
    extension_runtime: ExtensionRuntime | None = None
    selected_skill_ids: tuple[str, ...] = ()
    selected_skills_info: list[SkillInfo] = field(default_factory=list)
    persisted_extensions: RunExtensionsView | None = None
    cancel_requested: bool = False
    terminal_finalized: bool = False
    settled_event: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class _SessionRecord:
    view: SessionView
    seq_order: int = 0
    runs: dict[str, _RunRecord] = field(default_factory=dict)
    idempotency: dict[str, tuple[str, str]] = field(default_factory=dict)
    constraint_idempotency: dict[str, tuple[str, int]] = field(default_factory=dict)
    history: list[HistoryItem] = field(default_factory=list)


@dataclass
class _ProjectRecord:
    spec: ControlledProjectSpec | _RegisteredProjectSpec
    kind: ProjectKind
    canonical_root: str
    binding_valid: bool = True


def _canonical_root_key(path: Path | str) -> str:
    return os.path.normcase(os.path.realpath(path))


def validate_project_roots(
    config: HostConfig,
    *,
    raw_strings: Sequence[str] | None = None,
) -> tuple[Path, ...]:
    """Validate host-configured allowlisted exact project roots without creating directories."""
    raw_items: list[object] = (
        list(raw_strings) if raw_strings is not None else list(config.project_roots)
    )
    resolved_roots: list[Path] = []
    seen_keys: set[str] = set()
    for raw_item in raw_items:
        validated = _validate_raw_local_dir_path(raw_item, label="project_root")
        if not validated.exists() or not validated.is_dir():
            raise AppError(
                "INVALID_HOST_ROOT",
                "project_root must be an existing directory",
                status_code=400,
            )
        check_no_reparse_ancestors(validated, label="project_root")
        if paths_overlap(validated, config.data_root) or paths_overlap(
            validated, config.workspace_parent
        ):
            raise AppError(
                "OVERLAPPING_ROOTS",
                "project_root must not overlap data_root or workspace_parent",
                status_code=400,
            )
        canonical = Path(os.path.realpath(validated))
        check_no_reparse_ancestors(canonical, label="project_root")
        key = os.path.normcase(str(canonical))
        if key not in seen_keys:
            seen_keys.add(key)
            resolved_roots.append(canonical)
    return tuple(resolved_roots)


def _payload_fingerprint(request: StartRunRequest) -> str:
    canonical = json.dumps(
        {
            "client_request_id": str(request.client_request_id),
            "goal": request.goal,
            "confirmed_scope": request.confirmed_scope,
            "skill_ids": list(request.skill_ids),
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _attach_background_consumer(task: asyncio.Task[Any]) -> None:
    def _consume(done: asyncio.Task[Any]) -> None:
        if not done.cancelled():
            done.exception()

    task.add_done_callback(_consume)


def _bounded_proposal_event_payload(prop: ProposalView) -> dict[str, Any]:
    """Build a bounded JSON payload for proposal events within the 16 KiB event envelope limit."""
    dumped = prop.model_dump(mode="json")
    raw = json.dumps(dumped, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(raw) <= 12 * 1024:
        return dumped
    inner = dumped.get("payload")
    if isinstance(inner, dict):
        if isinstance(inner.get("exact_paths"), list) and len(inner["exact_paths"]) > 32:
            inner["exact_paths"] = inner["exact_paths"][:32]
        cmd = inner.get("command")
        if isinstance(cmd, dict) and isinstance(cmd.get("argv"), list):
            cmd["argv"] = [str(arg)[:128] for arg in cmd["argv"][:16]]
        plan = inner.get("plan")
        if isinstance(plan, dict) and isinstance(plan.get("checks"), list) and len(plan["checks"]) > 8:
            plan["checks"] = plan["checks"][:8]
    return dumped


def _bounded_run_result_summary(
    *,
    state: RunState,
    verification: VerificationState,
    reason_code: str,
    result_dict: dict[str, Any] | None,
) -> tuple[str, bool]:
    """Serialize bounded structured run_result summary JSON strictly within 32 KiB."""
    was_truncated = False
    changed_summary: list[dict[str, Any]] = []
    if isinstance(result_dict, dict):
        raw_changed = result_dict.get("changed_paths")
        if isinstance(raw_changed, list):
            if len(raw_changed) > 64:
                was_truncated = True
            for entry in raw_changed[:64]:
                if isinstance(entry, dict):
                    changed_summary.append(
                        {
                            "path": entry.get("path"),
                            "operation": entry.get("operation"),
                            "before_sha256": entry.get("before_sha256"),
                            "after_sha256": entry.get("after_sha256"),
                        }
                    )
        raw_checks = result_dict.get("checks")
    else:
        raw_checks = None

    checks_summary: dict[str, Any] = {}
    if isinstance(raw_checks, dict):
        check_items = list(raw_checks.items())
        if len(check_items) > 16:
            was_truncated = True
        for cid, rec in check_items[:16]:
            if isinstance(rec, dict):
                checks_summary[str(cid)] = {
                    "passed": rec.get("passed"),
                    "exit_code": rec.get("exit_code"),
                    "command_id": rec.get("command_id"),
                    "failure_reason": rec.get("failure_reason"),
                }

    reasons: list[str] = []
    if isinstance(result_dict, dict) and isinstance(result_dict.get("unverified_reasons"), list):
        raw_reasons = result_dict["unverified_reasons"]
        if len(raw_reasons) > 8:
            was_truncated = True
        for r in raw_reasons[:8]:
            excerpt, excerpt_truncated = truncate_utf8_safe(str(r), 512)
            if excerpt_truncated:
                was_truncated = True
            reasons.append(excerpt)

    summary_obj: dict[str, Any] = {
        "state": state,
        "verification": verification,
        "reason_code": reason_code,
        "changed_paths": changed_summary,
        "checks": checks_summary,
        "unverified_reasons": reasons,
    }
    if isinstance(result_dict, dict) and isinstance(result_dict.get("extensions"), dict):
        ext_src = result_dict["extensions"]
        ext_summary: dict[str, Any] = {
            "selected_skills": [
                {"id": s.get("id"), "sha256": s.get("sha256")}
                for s in (ext_src.get("selected_skills") or [])[:4]
                if isinstance(s, dict)
            ],
            "calls": [
                {
                    "call_id": c.get("call_id"),
                    "server_id": c.get("server_id"),
                    "tool_name": c.get("tool_name"),
                    "status": c.get("status"),
                    "effect": c.get("effect"),
                    "sha256": c.get("sha256"),
                }
                for c in (ext_src.get("calls") or [])[:16]
                if isinstance(c, dict)
            ],
            "total_calls": ext_src.get("total_calls", 0),
        }
        summary_obj["extensions"] = ext_summary
    encoded = canonical_json_dumps(summary_obj)
    if len(encoded.encode("utf-8")) <= MAX_RUN_RESULT_SUMMARY_BYTES:
        return encoded, was_truncated

    minimal = canonical_json_dumps(
        {
            "state": state,
            "verification": verification,
            "reason_code": reason_code,
            "changed_paths_count": len(changed_summary),
            "truncated": True,
        }
    )
    safe_text, _ = truncate_utf8_safe(minimal, MAX_RUN_RESULT_SUMMARY_BYTES)
    return safe_text, True


class AppService:
    """Persistent project, session, and single-writer run coordinator over native Coding."""

    def __init__(
        self,
        *,
        config: HostConfig,
        provider_factory: ProviderFactory | None = None,
        live_transport: object | None = None,
        chat_provider_factory: ChatProviderFactory | None = None,
    ) -> None:
        validate_host_config(config)
        self._project_roots: tuple[Path, ...] = validate_project_roots(config)
        self._extension_config: ExtensionConfig = load_extension_config(
            path=config.extension_config
        )
        self._config = config
        self._provider_factory = provider_factory
        self._chat_provider_factory = chat_provider_factory
        self._live_factory = (
            LiveProviderFactory(
                config=config.live,
                transport=live_transport,
                state_path=config.data_root / "live_authorization.json",
            )
            if config.live is not None
            else None
        )
        self._live_chat_factory = (
            LiveChatProviderFactory(
                config=config.live,
                quota=self._live_factory.quota,
                transport=live_transport,
            )
            if self._live_factory is not None and config.live is not None
            else None
        )
        self._lock = DataRootFileLock(config.data_root / ".host.lock")
        self._lock.acquire()

        self._projects: dict[str, _ProjectRecord] = {}
        self._projects_by_name: dict[str, str] = {}
        self._sessions: dict[str, _SessionRecord] = {}
        self._runs_by_id: dict[str, _RunRecord] = {}
        self._active_workspaces: set[str] = set()
        self._poisoned_workspaces: set[str] = set()
        self._active_root_keys: set[str] = set()
        self._poisoned_root_keys: set[str] = set()
        self._active_chat_projects: set[str] = set()
        self._active_chat_sessions: set[str] = set()
        self._closed = False

        try:
            self._store = SessionStore(data_root=config.data_root)
            loaded = self._store.reconcile_after_restart()
            self._hydrate_from_store(loaded)
        except Exception:
            if hasattr(self, "_store"):
                self._store.close()
            self._lock.release()
            raise

    def _is_persisted_project_valid(self, *, kind: ProjectKind, spec_obj: dict[str, Any]) -> bool:
        try:
            view = ProjectView.model_validate(spec_obj["view"])
            if kind == "existing":
                ws_raw = Path(view.workspace_root)
                if not ws_raw.exists() or not ws_raw.is_dir():
                    return False
                check_no_reparse_ancestors(ws_raw, label="project_root")
                canonical = Path(os.path.realpath(ws_raw))
                check_no_reparse_ancestors(canonical, label="project_root")
                if paths_overlap(canonical, self._config.data_root) or paths_overlap(
                    canonical, self._config.workspace_parent
                ):
                    return False
                return any(canonical == allowed for allowed in self._project_roots)
            ws_dir = Path(str(spec_obj["workspace_dir"]))
            acc_file = Path(str(spec_obj["acceptance_file"]))
            if not ws_dir.exists() or not ws_dir.is_dir() or not acc_file.is_file():
                return False
            check_no_reparse_ancestors(ws_dir, label="workspace_root")
            check_no_reparse_ancestors(acc_file, label="acceptance_file")
            canon_ws = Path(os.path.realpath(ws_dir))
            canon_parent = Path(os.path.realpath(self._config.workspace_parent))
            if canon_ws.parent != canon_parent:
                return False
            return not paths_overlap(canon_ws, self._config.data_root)
        except (AppError, ProjectError, OSError, ValueError):
            return False

    def _hydrate_from_store(self, data: dict[str, object]) -> None:
        for proj_row in data["projects"]:  # type: ignore[attr-defined]
            spec_obj = dict(proj_row["spec"])
            view = ProjectView.model_validate(proj_row["view"])
            kind: ProjectKind = proj_row["kind"]
            canonical_root = str(proj_row["canonical_root"])
            valid = self._is_persisted_project_valid(kind=kind, spec_obj=spec_obj)
            if kind == "existing":
                spec: ControlledProjectSpec | _RegisteredProjectSpec = _RegisteredProjectSpec(
                    view=view,
                    root=Path(view.workspace_root),
                    canonical_root=canonical_root,
                    read_grant_id=str(proj_row.get("read_grant_id") or view.read_grant_id or ""),
                )
            else:
                spec = ControlledProjectSpec(
                    view=view,
                    workspace_dir=Path(str(spec_obj["workspace_dir"])),
                    acceptance_file=Path(str(spec_obj["acceptance_file"])),
                    acceptance_sha256=str(spec_obj["acceptance_sha256"]),
                    initial_source_sha256=str(spec_obj["initial_source_sha256"]),
                )
            self._projects[view.project_id] = _ProjectRecord(
                spec=spec,
                kind=kind,
                canonical_root=canonical_root,
                binding_valid=valid,
            )
            self._projects_by_name[view.name] = view.project_id

        history_map: dict[str, list[dict[str, Any]]] = data["history_items"]  # type: ignore[assignment]
        for sess_row in data["sessions"]:  # type: ignore[attr-defined]
            s_view = SessionView.model_validate(sess_row["view"])
            hist_items = [HistoryItem.model_validate(it) for it in history_map.get(s_view.session_id, [])]
            self._sessions[s_view.session_id] = _SessionRecord(
                view=s_view,
                seq_order=int(sess_row["seq_order"]),
                history=hist_items,
            )

        for idem in data["run_idempotency"]:  # type: ignore[attr-defined]
            sess = self._sessions.get(str(idem["session_id"]))
            if sess is not None:
                sess.idempotency[str(idem["client_request_id"])] = (
                    str(idem["payload_digest"]),
                    str(idem["run_id"]),
                )

        for cidem in data["constraint_idempotency"]:  # type: ignore[attr-defined]
            sess = self._sessions.get(str(cidem["session_id"]))
            if sess is not None:
                sess.constraint_idempotency[str(cidem["client_request_id"])] = (
                    str(cidem["payload_digest"]),
                    int(cidem["revision"]),
                )

        events_map: dict[str, list[dict[str, Any]]] = data["run_events"]  # type: ignore[assignment]
        questions_map: dict[str, list[dict[str, Any]]] = data["questions"]  # type: ignore[assignment]
        ans_idem_map: dict[str, dict[str, tuple[str, str]]] = data["answer_idempotency"]  # type: ignore[assignment]
        for run_row in data["runs"]:  # type: ignore[attr-defined]
            r_view = RunView.model_validate(run_row["view"])
            sess = self._sessions[r_view.session_id]
            proj = self._projects[r_view.project_id]
            ev_list = [AppEvent.model_validate(e) for e in events_map.get(r_view.run_id, [])]
            q_list: list[QuestionView] = []
            for raw_q in questions_map.get(r_view.run_id, []):
                q_obj = dict(raw_q)
                for ts_key in ("created_at", "expires_at"):
                    if isinstance(q_obj.get(ts_key), str):
                        q_obj[ts_key] = datetime.fromisoformat(q_obj[ts_key])
                q_list.append(QuestionView.model_validate(q_obj))
            q_ids = {q.id for q in q_list}
            run_ans_idem = {
                k: v for k, v in ans_idem_map.get(r_view.run_id, {}).items() if v[0] in q_ids
            }
            hub = RunEventHub.from_persisted(
                project_id=r_view.project_id,
                session_id=r_view.session_id,
                run_id=r_view.run_id,
                last_seq=r_view.last_seq,
                truncated_before=r_view.truncated_before,
                events=ev_list,
                finished=r_view.state in _TERMINAL_STATES,
                max_events=self._config.max_events_per_run,
                persist_cb=lambda ev: self._store.append_run_event(event=ev),
            )
            if hub.truncated_before > r_view.truncated_before:
                r_view = r_view.model_copy(update={"truncated_before": hub.truncated_before})
            usage_view = UsageView.model_validate(r_view.usage) if r_view.usage is not None else None
            persisted_ext: RunExtensionsView | None = None
            selected_infos: list[SkillInfo] = []
            if isinstance(r_view.result, dict) and isinstance(r_view.result.get("extensions"), dict):
                ext_raw = r_view.result["extensions"]
                try:
                    selected_infos = [
                        SkillInfo.model_validate(s)
                        for s in (ext_raw.get("selected_skills") or [])
                        if isinstance(s, dict)
                    ]
                    srv_views: list[McpServerView] = []
                    for s in (ext_raw.get("servers") or []):
                        if isinstance(s, dict):
                            try:
                                srv_views.append(McpServerView.model_validate(s))
                            except (ValidationError, ValueError, TypeError):
                                continue
                    restored_calls: list[McpToolResult] = []
                    for c in (ext_raw.get("calls") or []):
                        if isinstance(c, dict):
                            try:
                                restored_calls.append(McpToolResult.model_validate(c))
                            except (ValidationError, ValueError, TypeError):
                                continue
                    persisted_ext = RunExtensionsView(
                        project_id=r_view.project_id,
                        session_id=r_view.session_id,
                        run_id=r_view.run_id,
                        selected_skills=selected_infos,
                        servers=srv_views,
                        proposals=[],
                        calls=restored_calls,
                        total_call_bytes=int(ext_raw.get("total_call_bytes") or 0),
                        total_calls=int(ext_raw.get("total_calls") or 0),
                    )
                except (ValidationError, ValueError, TypeError):
                    persisted_ext = None
            rec = _RunRecord(
                view=r_view,
                hub=hub,
                session=sess,
                project=proj,
                persisted_usage=usage_view,
                persisted_questions=q_list,
                answer_idempotency=run_ans_idem,
                question_seq_counter=len(q_list),
                selected_skills_info=selected_infos,
                persisted_extensions=persisted_ext,
                terminal_finalized=r_view.state in _TERMINAL_STATES,
            )
            if r_view.state in _TERMINAL_STATES:
                rec.settled_event.set()
            sess.runs[r_view.run_id] = rec
            self._runs_by_id[r_view.run_id] = rec
            if bool(run_row["poisoned_workspace"]) or not bool(run_row["cleanup_confirmed"]):
                pid = proj.spec.view.project_id
                self._poisoned_workspaces.add(pid)
                self._active_workspaces.add(pid)
                self._poisoned_root_keys.add(proj.canonical_root)
                self._active_root_keys.add(proj.canonical_root)

    def _require_open(self) -> None:
        if self._closed:
            raise AppError("SERVICE_CLOSED", "application service is closed", status_code=409)

    def _get_session_and_run(self, *, session_id: str, run_id: str) -> tuple[_SessionRecord, _RunRecord]:
        session = self._sessions.get(session_id)
        if session is None:
            raise AppError("NOT_FOUND", "session not found", status_code=404)
        run_rec = session.runs.get(run_id)
        if run_rec is None or run_rec.view.session_id != session_id:
            raise AppError("NOT_FOUND", "run not found in session", status_code=404)
        return session, run_rec

    def list_projects(self) -> list[ProjectView]:
        return [rec.spec.view.model_copy(deep=True) for rec in self._projects.values()]

    def list_sessions(self) -> list[SessionView]:
        return [rec.view.model_copy(deep=True) for rec in self._sessions.values()]

    def _make_history_item(
        self,
        *,
        session: _SessionRecord,
        run_id: str,
        kind: str,
        text: str,
        source_ids: list[str] | tuple[str, ...] = (),
        truncated: bool = False,
    ) -> HistoryItem:
        next_seq = (session.history[-1].seq + 1) if session.history else 1
        item_id = str(uuid.uuid4())
        src_list = list(source_ids)
        digest = compute_history_item_sha256(
            seq=next_seq,
            item_id=item_id,
            project_id=session.view.project_id,
            session_id=session.view.session_id,
            run_id=run_id,
            kind=kind,  # type: ignore[arg-type]
            text=text,
            source_ids=src_list,
            truncated=truncated,
        )
        return HistoryItem(
            seq=next_seq,
            item_id=item_id,
            project_id=session.view.project_id,
            session_id=session.view.session_id,
            run_id=run_id,
            kind=kind,  # type: ignore[arg-type]
            text=text,
            source_ids=src_list,
            truncated=truncated,
            sha256=digest,
        )

    async def create_project(self, *, name: str) -> ProjectView:
        self._require_open()
        parsed = CreateProjectRequest.model_validate({"name": name})
        if parsed.name in self._projects_by_name:
            raise AppError("PROJECT_EXISTS", "project name already exists", status_code=409)
        project_id = str(uuid.uuid4())
        spec = initialize_controlled_project(
            project_id=project_id,
            name=parsed.name,
            data_root=self._config.data_root,
            workspace_parent=self._config.workspace_parent,
        )
        canonical = _canonical_root_key(spec.workspace_dir)
        self._store.insert_project(
            view=spec.view,
            kind="controlled",
            workspace_root=spec.view.workspace_root,
            canonical_root=canonical,
            read_grant_id=None,
            spec={
                "view": spec.view.model_dump(mode="json"),
                "workspace_dir": str(spec.workspace_dir),
                "acceptance_file": str(spec.acceptance_file),
                "acceptance_sha256": spec.acceptance_sha256,
                "initial_source_sha256": spec.initial_source_sha256,
            },
        )
        self._projects[project_id] = _ProjectRecord(
            spec=spec,
            kind="controlled",
            canonical_root=canonical,
            binding_valid=True,
        )
        self._projects_by_name[parsed.name] = project_id
        return spec.view.model_copy(deep=True)

    async def register_project(self, *, request: RegisterProjectRequest) -> ProjectView:
        """Register an existing local project root as read-only first without writing files."""
        self._require_open()
        req = RegisterProjectRequest.model_validate(request)
        if not req.confirmed_read:
            raise AppError(
                "READ_NOT_CONFIRMED",
                "user must confirm read access before registering a project root",
                status_code=403,
            )
        if req.name in self._projects_by_name:
            raise AppError("PROJECT_EXISTS", "project name already exists", status_code=409)

        raw_path = _validate_raw_local_dir_path(req.root, label="project_root")
        if not raw_path.exists() or not raw_path.is_dir():
            raise AppError(
                "INVALID_PROJECT_ROOT",
                "project root must be an existing directory",
                status_code=400,
            )
        canonical_path = Path(os.path.realpath(raw_path))
        check_no_reparse_ancestors(canonical_path, label="project_root")
        if paths_overlap(canonical_path, self._config.data_root) or paths_overlap(
            canonical_path, self._config.workspace_parent
        ):
            raise AppError(
                "OVERLAPPING_ROOTS",
                "project root must not overlap data_root or workspace_parent",
                status_code=409,
            )

        for existing_rec in self._projects.values():
            existing_root = Path(existing_rec.spec.view.workspace_root)
            if paths_overlap(canonical_path, existing_root):
                raise AppError(
                    "PROJECT_ROOT_OVERLAP",
                    "project root duplicates or overlaps an already-registered project",
                    status_code=409,
                )

        is_allowed = any(
            canonical_path == allowed
            for allowed in self._project_roots
        )
        if not is_allowed:
            raise AppError(
                "PROJECT_ROOT_NOT_ALLOWED",
                "project root is not an exact host-configured allowlisted project root",
                status_code=403,
            )

        project_id = str(uuid.uuid4())
        canonical_str = os.path.normcase(str(canonical_path))
        read_grant_id = str(uuid.uuid4())
        view = ProjectView(
            project_id=project_id,
            name=req.name,
            workspace_root=str(canonical_path),
            read_paths=[],
            write_paths=[],
            commands_summary=[],
            expected_tests=[],
            kind="existing",
            read_grant_id=read_grant_id,
        )
        spec = _RegisteredProjectSpec(
            view=view,
            root=canonical_path,
            canonical_root=canonical_str,
            read_grant_id=read_grant_id,
        )
        self._store.insert_project(
            view=view,
            kind="existing",
            workspace_root=view.workspace_root,
            canonical_root=canonical_str,
            read_grant_id=read_grant_id,
            spec={"view": view.model_dump(mode="json")},
        )
        self._projects[project_id] = _ProjectRecord(
            spec=spec,
            kind="existing",
            canonical_root=canonical_str,
            binding_valid=True,
        )
        self._projects_by_name[req.name] = project_id
        return view.model_copy(deep=True)

    def create_session(self, *, project_id: str, model: ModelSelection) -> SessionView:
        self._require_open()
        validated_model = ModelSelection.model_validate(model)
        if project_id not in self._projects:
            raise AppError("NOT_FOUND", "project not found", status_code=404)
        if len(self._sessions) >= self._config.max_sessions:
            raise AppError("SESSION_QUOTA_EXCEEDED", "session quota reached", status_code=429)
        session_id = str(uuid.uuid4())
        seq_order = len(self._sessions) + 1
        view = SessionView(
            session_id=session_id,
            project_id=project_id,
            model=validated_model,
            active_run_id=None,
            constraints_revision=0,
            constraints="",
            history_cursor=0,
        )
        self._store.insert_session(view=view, seq_order=seq_order)
        self._sessions[session_id] = _SessionRecord(view=view, seq_order=seq_order)
        return view.model_copy(deep=True)

    async def start_run(self, *, session_id: str, request: StartRunRequest) -> RunView:
        view, _ = await self.start_run_with_replay(session_id=session_id, request=request)
        return view

    async def start_run_with_replay(
        self, *, session_id: str, request: StartRunRequest
    ) -> tuple[RunView, bool]:
        """Start a run or replay an idempotent identical request synchronously in one section."""
        self._require_open()
        session = self._sessions.get(session_id)
        if session is None:
            raise AppError("NOT_FOUND", "session not found", status_code=404)
        validated_req = StartRunRequest.model_validate(request)
        idem_key = str(validated_req.client_request_id)
        fp = _payload_fingerprint(validated_req)

        existing = session.idempotency.get(idem_key)
        if existing is not None:
            prev_fp, prev_run_id = existing
            if prev_fp != fp:
                raise AppError(
                    "IDEMPOTENCY_CONFLICT",
                    "client_request_id reused with different payload",
                    status_code=409,
                )
            return session.runs[prev_run_id].view.model_copy(deep=True), True

        if not validated_req.confirmed_scope:
            raise AppError(
                "SCOPE_NOT_CONFIRMED",
                "user must explicitly confirm project scope before execution",
                status_code=403,
            )
        if len(session.runs) >= self._config.max_runs_per_session:
            raise AppError("RUN_QUOTA_EXCEEDED", "session run quota reached", status_code=429)

        project = self._projects.get(session.view.project_id)
        if project is None:
            raise AppError("NOT_FOUND", "project not found", status_code=404)
        if not project.binding_valid:
            raise AppError(
                "PROJECT_BINDING_INVALID",
                "persisted project root is no longer permitted by current HostConfig",
                status_code=403,
            )
        if session.view.model.provider == "offline":
            if project.kind == "existing" and session.view.model.model != "offline-project-v1":
                raise AppError(
                    "MODEL_PROJECT_MISMATCH",
                    "existing project requires offline-project-v1 in offline mode",
                    status_code=409,
                )
            if project.kind == "controlled" and session.view.model.model != "offline-metrics-v1":
                raise AppError(
                    "MODEL_PROJECT_MISMATCH",
                    "controlled project requires offline-metrics-v1 in offline mode",
                    status_code=409,
                )
        else:
            if self._live_factory is None:
                raise AppError(
                    "LIVE_DISABLED",
                    "live provider execution is disabled in offline mode",
                    status_code=409,
                )
            if project.kind != "existing":
                raise AppError(
                    "MODEL_PROJECT_MISMATCH",
                    "live model requires an existing registered project",
                    status_code=409,
                )
            if session.view.model.model != self._config.live.model:
                raise AppError(
                    "MODEL_NOT_ALLOWED",
                    "requested live model is not the host-configured model",
                    status_code=409,
                )

        pid = project.spec.view.project_id
        root_key = project.canonical_root
        if (
            pid in self._poisoned_workspaces
            or pid in self._active_workspaces
            or pid in self._active_chat_projects
            or root_key in self._poisoned_root_keys
            or root_key in self._active_root_keys
        ):
            raise AppError(
                "WORKSPACE_BUSY",
                "workspace already has an active or unconfirmed run",
                status_code=409,
            )
        if session.view.active_run_id is not None:
            raise AppError(
                "SESSION_BUSY",
                "session already has an active run",
                status_code=409,
            )

        selected_skill_ids = tuple(validated_req.skill_ids)
        selected_skills_info: list[SkillInfo] = []
        if selected_skill_ids:
            if project.kind != "existing":
                raise ExtensionError(
                    "SKILLS_UNSUPPORTED_FOR_CONTROLLED",
                    "skill_ids are only supported for existing registered projects",
                    status_code=400,
                )
            reg_for_skills = project.spec
            assert isinstance(reg_for_skills, _RegisteredProjectSpec)
            selected_ctx = load_selected_skills(
                project_root=reg_for_skills.root,
                skill_ids=selected_skill_ids,
            )
            selected_skills_info = [
                SkillInfo(id=s.id, path=s.path, sha256=s.sha256, total_bytes=s.total_bytes)
                for s in selected_ctx
            ]

        run_id = str(uuid.uuid4())
        live = session.view.model.provider == "deepseek"
        if project.kind == "existing":
            usage_acc = UsageAccumulator(max_calls=64, max_steps=120, run_timeout_ms=600_000, live=live)
        else:
            usage_acc = UsageAccumulator(max_calls=12, max_steps=12, run_timeout_ms=60_000, live=live)
        initial_usage_view = usage_acc.snapshot()
        goal_item = self._make_history_item(
            session=session,
            run_id=run_id,
            kind="user_goal",
            text=validated_req.goal,
            source_ids=[idem_key],
            truncated=False,
        )
        hub = RunEventHub(
            project_id=pid,
            session_id=session_id,
            run_id=run_id,
            max_events=self._config.max_events_per_run,
            persist_cb=lambda ev: self._store.append_run_event(event=ev),
        )
        queued_ev = hub.prepare_event("run_queued", {"state": "queued"})
        run_view = RunView(
            run_id=run_id,
            session_id=session_id,
            project_id=pid,
            client_request_id=idem_key,
            goal=validated_req.goal,
            state="queued",
            verification="not_run",
            reason_code=None,
            last_seq=queued_ev.seq,
            result=None,
            truncated_before=0,
            usage=initial_usage_view.model_dump(mode="json"),
        )
        run_seq_order = len(session.runs) + 1
        self._store.create_run_initial(
            run_view=run_view,
            run_seq_order=run_seq_order,
            payload_digest=fp,
            goal_history_item=goal_item,
            queued_event=queued_ev,
            usage_view=initial_usage_view,
        )

        run_rec = _RunRecord(
            view=run_view,
            hub=hub,
            session=session,
            project=project,
            usage=usage_acc,
            persisted_usage=initial_usage_view,
            selected_skill_ids=selected_skill_ids,
            selected_skills_info=selected_skills_info,
        )
        self._active_workspaces.add(pid)
        self._active_root_keys.add(root_key)
        session.runs[run_id] = run_rec
        self._runs_by_id[run_id] = run_rec
        session.idempotency[idem_key] = (fp, run_id)
        session.history.append(goal_item)
        session.view = session.view.model_copy(
            update={"active_run_id": run_id, "history_cursor": goal_item.seq}
        )

        hub.commit_prepared_event(queued_ev)
        run_rec.task = asyncio.create_task(self._execute_worker(run_rec))
        return run_rec.view.model_copy(deep=True), False

    def _emit_event(self, run_rec: _RunRecord, kind: str, payload: dict[str, Any]) -> None:
        if run_rec.hub.finished:
            return
        try:
            ev = run_rec.hub.emit(kind, payload)  # type: ignore[arg-type]
        except AppError:
            # Non-terminal event store failure must not fake persisted delivery.
            return
        run_rec.view = run_rec.view.model_copy(
            update={"last_seq": ev.seq, "truncated_before": run_rec.hub.truncated_before}
        )

    async def _close_run_live_client(self, run_rec: _RunRecord) -> bool:
        """Close and release the run's live client; return False when the close is unconfirmed."""
        provider = run_rec.live_provider
        run_rec.live_provider = None
        run_rec.wrapped_provider = None
        if provider is None:
            return True
        try:
            await provider.aclose()
        except Exception:  # noqa: BLE001 - an unconfirmed close becomes an honest terminal
            return False
        return True

    async def _finalize_terminal(
        self,
        run_rec: _RunRecord,
        *,
        state: RunState,
        verification: VerificationState,
        reason_code: str,
        result_dict: dict[str, Any] | None,
        release_workspace_lock: bool,
    ) -> None:
        if run_rec.terminal_finalized:
            return
        run_rec.terminal_finalized = True
        if not await self._close_run_live_client(run_rec):
            state = "unknown"
            verification = "unknown"
            reason_code = "CLEANUP_UNCONFIRMED"
            release_workspace_lock = False
        if run_rec.permissions is not None:
            run_rec.permissions.cancel_pending()
        if run_rec.question_broker is not None:
            run_rec.question_broker.cancel_pending(reason_code=reason_code)
        if run_rec.extension_runtime is not None:
            run_rec.extension_runtime.cancel_pending()
            if run_rec.persisted_extensions is None:
                run_rec.persisted_extensions = run_rec.extension_runtime.build_run_extensions_view()

        pid = run_rec.project.spec.view.project_id
        root_key = run_rec.project.canonical_root

        final_usage = run_rec.usage.finalize() if run_rec.usage is not None else run_rec.persisted_usage
        summary_text, summary_trunc = _bounded_run_result_summary(
            state=state,
            verification=verification,
            reason_code=reason_code,
            result_dict=result_dict,
        )
        result_item = self._make_history_item(
            session=run_rec.session,
            run_id=run_rec.view.run_id,
            kind="run_result",
            text=summary_text,
            source_ids=[run_rec.view.run_id],
            truncated=summary_trunc,
        )
        acceptance = result_dict.get("acceptance") if isinstance(result_dict, dict) else None
        finish_payload: dict[str, Any] = {
            "state": state,
            "verification": verification,
            "reason_code": reason_code,
            "final_workspace_revision": (
                (result_dict.get("final_workspace_revision") or result_dict.get("final_revision"))
                if isinstance(result_dict, dict)
                else None
            ),
            "acceptance": (
                {
                    "status": acceptance.get("status"),
                    "revision": acceptance.get("revision"),
                    "command_ids": acceptance.get("command_ids"),
                }
                if isinstance(acceptance, dict)
                else None
            ),
            "changed_paths": (
                result_dict.get("changed_paths") if isinstance(result_dict, dict) else []
            ),
        }

        _SCOPE_DIAG_PREFIX = "propose_scope: field "
        _SCOPE_DIAG_MID = " rejected; "
        _SCOPE_DIAG_HINT = "exact_paths or write_prefixes must contain the target; "
        _SCOPE_DIAG_TAIL = (
            "allowed fields: exact_paths, write_prefixes, operations, protected_paths, "
            "verification_plan_summary; allowed operations: create, modify"
        )

        def _whitelisted_scope_field(raw: str) -> str | None:
            """Rebuild one trusted propose_scope field path; never echo raw input."""
            if raw in ("payload", "exact_paths", "write_prefixes", "operations",
                       "protected_paths", "verification_plan_summary"):
                return raw
            for base in ("exact_paths", "write_prefixes", "operations", "protected_paths"):
                prefix = f"{base}["
                if raw.startswith(prefix) and raw.endswith("]"):
                    inner = raw[len(prefix):-1]
                    if (
                        inner.isascii()
                        and inner.isdigit()
                        and (inner == "0" or not inner.startswith("0"))
                        and len(inner) <= 19
                        and (len(inner) < 19 or inner <= "9223372036854775807")
                    ):
                        return f"{base}[{inner}]"
            return None

        def _scope_diagnostic() -> dict[str, Any] | None:
            """Derive the current run's propose_scope rejection only for a strict final rejection."""
            if (
                state != "failed"
                or verification != "not_run"
                or reason_code != "EVIDENCE_INVALID"
            ):
                return None
            reasons = (
                result_dict.get("unverified_reasons") if isinstance(result_dict, dict) else None
            )
            if not isinstance(reasons, list):
                return None
            for reason in reasons:
                if not isinstance(reason, str) or not reason.startswith(_SCOPE_DIAG_PREFIX):
                    continue
                rest = reason[len(_SCOPE_DIAG_PREFIX):]
                idx = rest.find(_SCOPE_DIAG_MID)
                if idx < 0:
                    continue
                field = _whitelisted_scope_field(rest[:idx])
                if field is None:
                    continue
                tail = rest[idx + len(_SCOPE_DIAG_MID):]
                expected_tail = (
                    _SCOPE_DIAG_HINT + _SCOPE_DIAG_TAIL
                    if field == "exact_paths"
                    else _SCOPE_DIAG_TAIL
                )
                if tail != expected_tail:
                    continue
                return {
                    "action": "propose_scope",
                    "field": field,
                    "allowed_operations": ["create", "modify"],
                }
            return None

        current_scope_diagnostic = _scope_diagnostic()
        if current_scope_diagnostic is not None:
            finish_payload["scope_diagnostic"] = current_scope_diagnostic
        finished_ev = run_rec.hub.prepare_event("run_finished", finish_payload)
        candidate_view = run_rec.view.model_copy(
            update={
                "state": state,
                "verification": verification,
                "reason_code": reason_code,
                "result": result_dict,
                "last_seq": finished_ev.seq,
                "usage": final_usage.model_dump(mode="json") if final_usage is not None else None,
            }
        )

        try:
            trunc_before = self._store.finalize_run(
                run_view=candidate_view,
                usage_view=final_usage,
                result_history_item=result_item,
                finished_event=finished_ev,
                cleanup_confirmed=release_workspace_lock,
                poisoned_workspace=not release_workspace_lock,
            )
        except AppError:
            # Terminal persistence failed: keep workspace locked/poisoned, preserve modified files,
            # report unknown / STORE_UNAVAILABLE honestly without claiming durable success.
            self._poisoned_workspaces.add(pid)
            self._active_workspaces.add(pid)
            self._poisoned_root_keys.add(root_key)
            self._active_root_keys.add(root_key)
            if run_rec.session.view.active_run_id == run_rec.view.run_id:
                run_rec.session.view = run_rec.session.view.model_copy(update={"active_run_id": None})
            run_rec.persisted_usage = final_usage
            if not run_rec.hub.finished:
                fallback_payload = {
                    **finish_payload,
                    "state": "unknown",
                    "verification": "unknown",
                    "reason_code": "STORE_UNAVAILABLE",
                    "store_persisted": False,
                }
                fallback_payload.pop("scope_diagnostic", None)
                unpersisted_ev = run_rec.hub.emit(
                    "run_finished",
                    fallback_payload,
                    persist=False,
                )
                last_seq = unpersisted_ev.seq
            else:
                last_seq = run_rec.view.last_seq
            run_rec.view = candidate_view.model_copy(
                update={
                    "state": "unknown",
                    "verification": "unknown",
                    "reason_code": "STORE_UNAVAILABLE",
                    "last_seq": last_seq,
                    "truncated_before": run_rec.hub.truncated_before,
                }
            )
            run_rec.settled_event.set()
            return

        if release_workspace_lock:
            self._active_workspaces.discard(pid)
            self._active_root_keys.discard(root_key)
        else:
            self._poisoned_workspaces.add(pid)
            self._active_workspaces.add(pid)
            self._poisoned_root_keys.add(root_key)
            self._active_root_keys.add(root_key)

        if run_rec.session.view.active_run_id == run_rec.view.run_id:
            run_rec.session.view = run_rec.session.view.model_copy(update={"active_run_id": None})
        run_rec.session.history.append(result_item)
        run_rec.session.view = run_rec.session.view.model_copy(update={"history_cursor": result_item.seq})
        run_rec.persisted_usage = final_usage
        committed_ev = run_rec.hub.commit_prepared_event(finished_ev, truncated_before=trunc_before)
        run_rec.view = candidate_view.model_copy(
            update={
                "last_seq": committed_ev.seq,
                "truncated_before": run_rec.hub.truncated_before,
            }
        )
        run_rec.settled_event.set()

    async def _execute_worker(self, run_rec: _RunRecord) -> None:
        # Yield once so start_run returns queued view before transitioning if needed.
        await asyncio.sleep(0)
        if run_rec.cancel_requested:
            await self._finalize_terminal(
                run_rec,
                state="cancelled",
                verification="not_run",
                reason_code="USER_CANCELLED",
                result_dict=None,
                release_workspace_lock=True,
            )
            return

        run_rec.view = run_rec.view.model_copy(update={"state": "running"})
        try:
            self._store.update_run_progress(
                run_id=run_rec.view.run_id,
                state="running",
                usage_view=run_rec.usage.snapshot() if run_rec.usage is not None else None,
            )
        except AppError as exc:
            await self._finalize_terminal(
                run_rec,
                state="failed",
                verification="unknown",
                reason_code=exc.code,
                result_dict=None,
                release_workspace_lock=True,
            )
            return
        self._emit_event(run_rec, "run_started", {"state": "running"})

        try:
            run_dir = self._config.data_root / "runs" / run_rec.view.run_id
            ensure_clean_directory(run_dir, label="run_dir")
            out_dir = run_dir / "output"
            ensure_clean_directory(out_dir, label="run_output")
            trace_path = run_dir / "trace.jsonl"

            def _emit(kind: str, payload: dict[str, Any]) -> None:
                if kind in ("provider_close_started", "provider_close_finished"):
                    # Internal provider-close lifecycle signals are not app events.
                    return
                if not run_rec.terminal_finalized:
                    self._emit_event(run_rec, kind, payload)

            if run_rec.project.kind == "existing":
                reg_spec = run_rec.project.spec
                if not isinstance(reg_spec, _RegisteredProjectSpec):
                    raise AppError("INTERNAL_ERROR", "invalid project spec", status_code=500)
                history_before = [
                    it for it in run_rec.session.history if it.run_id != run_rec.view.run_id
                ]
                context_frame = build_context_frame(
                    project_id=reg_spec.view.project_id,
                    session_id=run_rec.view.session_id,
                    run_id=run_rec.view.run_id,
                    goal=run_rec.view.goal,
                    constraints=run_rec.session.view.constraints,
                    history=history_before,
                )

                def _persist_question(q_view: QuestionView) -> None:
                    hist_item: HistoryItem | None = None
                    ans_cid: str | None = None
                    ans_dig: str | None = None
                    existing_idx = next(
                        (i for i, pq in enumerate(run_rec.persisted_questions) if pq.id == q_view.id),
                        None,
                    )
                    if existing_idx is None:
                        seq_in_run = run_rec.question_seq_counter + 1
                        hist_item = self._make_history_item(
                            session=run_rec.session,
                            run_id=run_rec.view.run_id,
                            kind="question",
                            text=q_view.question,
                            source_ids=[q_view.id, q_view.action_id],
                            truncated=False,
                        )
                    else:
                        seq_in_run = existing_idx + 1
                        if q_view.state == "answered" and q_view.answer is not None:
                            if run_rec.pending_answer_ctx is not None:
                                ans_cid, ans_dig = run_rec.pending_answer_ctx
                            hist_item = self._make_history_item(
                                session=run_rec.session,
                                run_id=run_rec.view.run_id,
                                kind="answer",
                                text=q_view.answer,
                                source_ids=[q_view.id, q_view.action_id],
                                truncated=False,
                            )
                    self._store.upsert_question(
                        question=q_view,
                        seq_in_run=seq_in_run,
                        history_item=hist_item,
                        answer_client_request_id=ans_cid,
                        answer_payload_digest=ans_dig,
                    )
                    if existing_idx is None:
                        run_rec.question_seq_counter = seq_in_run
                        run_rec.persisted_questions.append(q_view.model_copy(deep=True))
                    else:
                        run_rec.persisted_questions[existing_idx] = q_view.model_copy(deep=True)
                    if hist_item is not None:
                        run_rec.session.history.append(hist_item)
                        run_rec.session.view = run_rec.session.view.model_copy(
                            update={"history_cursor": hist_item.seq}
                        )

                project_task, planner_cfg = build_registered_project_task(
                    project_id=reg_spec.view.project_id,
                    project_name=reg_spec.view.name,
                    project_root=reg_spec.root,
                    root_identity=reg_spec.canonical_root,
                    read_grant_id=reg_spec.read_grant_id,
                    data_root=self._config.data_root,
                    output_root=out_dir,
                    run_id=run_rec.view.run_id,
                    goal=run_rec.view.goal,
                    python_executable=self._config.python_executable,
                    session_id=run_rec.view.session_id,
                )
                q_broker = QuestionBroker(
                    project_id=reg_spec.view.project_id,
                    session_id=run_rec.view.session_id,
                    run_id=run_rec.view.run_id,
                    persist=_persist_question,
                    emit=_emit,
                    ttl_seconds=project_task.proposal_ttl_seconds,
                )
                run_rec.question_broker = q_broker
                conversation = ConversationRuntime(
                    context_frame=context_frame,
                    question_broker=q_broker,
                    usage_accumulator=run_rec.usage,
                )

                def _on_proposal_changed(event_kind: str, prop_view: ProposalView) -> None:
                    if not run_rec.terminal_finalized:
                        self._emit_event(
                            run_rec,
                            event_kind,
                            _bounded_proposal_event_payload(prop_view),
                        )

                broker = PermissionBroker(
                    run_id=run_rec.view.run_id,
                    project_root=reg_spec.root,
                    project_id=reg_spec.view.project_id,
                    session_id=run_rec.view.session_id,
                    root_identity=reg_spec.canonical_root,
                    ttl_seconds=project_task.proposal_ttl_seconds,
                    on_proposal_changed=_on_proposal_changed,
                )
                ws = ProjectWorkspace(root=reg_spec.root, data_root=self._config.data_root)
                cmd_runner = ProjectCommandRunner(
                    run_id=run_rec.view.run_id,
                    project_root=reg_spec.root,
                    data_root=self._config.data_root,
                    broker=broker,
                    emit=_emit,
                )
                run_rec.permissions = broker
                run_rec.workspace = ws
                run_rec.command_runner = cmd_runner
                ext_runtime: ExtensionRuntime | None = None
                if run_rec.selected_skill_ids or self._config.extension_config is not None:
                    ext_scratch = run_dir / "ext_scratch"
                    ensure_clean_directory(ext_scratch, label="ext_scratch")
                    ext_runtime = ExtensionRuntime(
                        project_id=reg_spec.view.project_id,
                        session_id=run_rec.view.session_id,
                        run_id=run_rec.view.run_id,
                        project_root=reg_spec.root,
                        config=self._extension_config,
                        selected_skill_ids=run_rec.selected_skill_ids,
                        scratch_root=ext_scratch,
                        emit=_emit,
                    )
                    run_rec.extension_runtime = ext_runtime
                if run_rec.session.view.model.provider == "deepseek":
                    assert self._live_factory is not None
                    provider = self._live_factory(
                        project=reg_spec.view.model_copy(deep=True),
                        run_id=run_rec.view.run_id,
                        usage_accumulator=run_rec.usage,
                    )
                    run_rec.live_provider = provider
                else:
                    provider = (
                        self._provider_factory(
                            project=reg_spec.view.model_copy(deep=True),
                            run_id=run_rec.view.run_id,
                        )
                        if self._provider_factory is not None
                        else OfflineProjectProvider()
                    )
                wrapped = _ObservationEmittingProvider(
                    inner=provider, emit=_emit, usage_accumulator=run_rec.usage
                )
                run_rec.wrapped_provider = wrapped
                exec_task: asyncio.Task[Any] = asyncio.create_task(
                    execute_project_run(
                        task=project_task,
                        provider=wrapped,
                        planner_config=planner_cfg,
                        permissions=broker,
                        trace_path=trace_path,
                        emit=_emit,
                        workspace=ws,
                        command_runner=cmd_runner,
                        conversation=conversation,
                        extensions=ext_runtime,
                    )
                )
            else:
                controlled_spec = run_rec.project.spec
                if not isinstance(controlled_spec, ControlledProjectSpec):
                    raise AppError("INTERNAL_ERROR", "invalid controlled project spec", status_code=500)
                task, coding_cfg, planner_cfg = build_controlled_coding_task(
                    project=controlled_spec,
                    run_id=run_rec.view.run_id,
                    goal=run_rec.view.goal,
                    output_root=out_dir,
                    python_executable=self._config.python_executable,
                )
                provider = (
                    self._provider_factory(
                        project=controlled_spec.view.model_copy(deep=True),
                        run_id=run_rec.view.run_id,
                    )
                    if self._provider_factory is not None
                    else OfflineMetricsProvider()
                )

                async def _call_native() -> CodingRunResult:
                    if self._is_native_execute_run():
                        from forecontract.tracing import JsonlTraceStore
                        from forecontract_coding.agent import run_coding_task

                        store = JsonlTraceStore(trace_path)
                        trace_writer = _AppTraceWriter(
                            store=store, emit=_emit, usage_accumulator=run_rec.usage
                        )
                        wrapped_ctrl = _ObservationEmittingProvider(
                            inner=provider, emit=_emit, usage_accumulator=run_rec.usage
                        )
                        run_rec.wrapped_provider = wrapped_ctrl
                        c_res = await run_coding_task(
                            task=task,
                            config=coding_cfg,
                            provider=wrapped_ctrl,
                            planner_config=planner_cfg,
                            trace_writer=trace_writer,
                        )
                        if run_rec.usage is not None:
                            run_rec.usage.set_steps_used(int(c_res.run_result.steps_started))
                        return c_res
                    return await execute_run(
                        task=task,
                        config=coding_cfg,
                        provider=provider,
                        planner_config=planner_cfg,
                        trace_path=trace_path,
                        emit=_emit,
                    )

                exec_task = asyncio.create_task(_call_native())
        except AppError as exc:
            await self._finalize_terminal(
                run_rec,
                state="failed",
                verification="unknown",
                reason_code=exc.code,
                result_dict=None,
                release_workspace_lock=True,
            )
            return
        except Exception:  # noqa: BLE001 - bounded internal init failure
            await self._finalize_terminal(
                run_rec,
                state="failed",
                verification="unknown",
                reason_code="INTERNAL_ERROR",
                result_dict=None,
                release_workspace_lock=True,
            )
            return

        run_rec.exec_task = exec_task
        def _runner_incomplete() -> bool:
            if run_rec.command_runner is None:
                return False
            ok, rem = run_rec.command_runner.cleanup_summary()
            return (not ok) or rem > 0 or bool(run_rec.command_runner.active_command_ids())

        async def _cleanup_extensions_and_enrich(
            base_result: dict[str, Any] | None,
        ) -> tuple[bool, dict[str, Any] | None]:
            if run_rec.extension_runtime is None:
                return False, base_result
            cleanup = await run_rec.extension_runtime.aclose()
            ext_incomplete = (not cleanup.cleanup_ok) or cleanup.remaining_active > 0
            ext_view = run_rec.extension_runtime.build_run_extensions_view()
            run_rec.persisted_extensions = ext_view
            out_res = dict(base_result) if isinstance(base_result, dict) else {}
            out_res["extensions"] = {
                "selected_skills": [s.model_dump(mode="json") for s in ext_view.selected_skills],
                "servers": [
                    {
                        "id": srv.id,
                        "transport": srv.transport,
                        "url": srv.url,
                        "executable": srv.executable,
                        "argv": srv.argv,
                        "cwd": srv.cwd,
                        "config_sha256": srv.config_sha256,
                        "executable_sha256": srv.executable_sha256,
                        "state": srv.state,
                        "tools_count": srv.tools_count,
                    }
                    for srv in ext_view.servers[:8]
                ],
                "calls": [
                    {
                        "call_id": c.call_id,
                        "action_id": c.action_id,
                        "server_id": c.server_id,
                        "tool_name": c.tool_name,
                        "args_sha256": c.args_sha256,
                        "schema_sha256": c.schema_sha256,
                        "status": c.status,
                        "is_error": c.is_error,
                        "effect": c.effect,
                        "truncated": c.truncated,
                        "original_bytes": c.original_bytes,
                        "sha256": c.sha256,
                        "error_code": c.error_code,
                        "error_message": (c.error_message[:256] if c.error_message else None),
                    }
                    for c in ext_view.calls[:32]
                ],
                "total_calls": ext_view.total_calls,
                "total_call_bytes": ext_view.total_call_bytes,
            }
            return ext_incomplete, out_res

        try:
            raw_result = await exec_task
        except asyncio.CancelledError:
            # Cancellation might have been triggered via cancel_run watchdog or external cancel.
            if not run_rec.terminal_finalized:
                unsettled_ok = True
                if run_rec.wrapped_provider is not None:
                    unsettled_ok = await run_rec.wrapped_provider.wait_unsettled(
                        CANCEL_CONFIRMATION_SECONDS
                    )
                ext_incomplete, ext_result = await _cleanup_extensions_and_enrich(None)
                if unsettled_ok and not _runner_incomplete() and not ext_incomplete:
                    await self._finalize_terminal(
                        run_rec,
                        state="cancelled",
                        verification="unknown",
                        reason_code="USER_CANCELLED",
                        result_dict=ext_result,
                        release_workspace_lock=True,
                    )
                else:
                    await self._finalize_terminal(
                        run_rec,
                        state="unknown",
                        verification="unknown",
                        reason_code="CANCEL_CONFIRMATION_TIMEOUT" if not unsettled_ok else "CLEANUP_INCOMPLETE",
                        result_dict=ext_result,
                        release_workspace_lock=False,
                    )
            return
        except (CodingError, PlannerError, ProjectError) as exc:
            code = getattr(exc, "code", "EXECUTION_ERROR")
            ext_incomplete, ext_result = await _cleanup_extensions_and_enrich(None)
            incomplete = _runner_incomplete() or ext_incomplete or code == "CODING_PROCESS_CLEANUP_FAILED"
            await self._finalize_terminal(
                run_rec,
                state="unknown" if incomplete else "failed",
                verification="unknown" if incomplete else "failed",
                reason_code="CLEANUP_INCOMPLETE" if incomplete else str(code),
                result_dict=ext_result,
                release_workspace_lock=not incomplete,
            )
            return
        except Exception:  # noqa: BLE001 - never surface raw tracebacks or secrets
            ext_incomplete, ext_result = await _cleanup_extensions_and_enrich(None)
            incomplete = _runner_incomplete() or ext_incomplete
            await self._finalize_terminal(
                run_rec,
                state="unknown" if incomplete else "failed",
                verification="unknown",
                reason_code="CLEANUP_INCOMPLETE" if incomplete else "EXECUTION_ERROR",
                result_dict=ext_result,
                release_workspace_lock=not incomplete,
            )
            return

        if run_rec.terminal_finalized:
            return
        release_lock = True
        if isinstance(raw_result, ProjectRunResult):
            state, verification, reason_code = evaluate_project_run_outcome(raw_result)
            result_dict = raw_result.model_dump(mode="json")
            ext_incomplete, result_dict = await _cleanup_extensions_and_enrich(result_dict)
            cleanup = raw_result.cleanup if isinstance(raw_result.cleanup, dict) else {}
            if (
                state == "unknown"
                or ext_incomplete
                or not bool(cleanup.get("cleanup_ok", True))
                or int(cleanup.get("remaining_active", 0)) != 0
                or _runner_incomplete()
            ):
                if ext_incomplete:
                    state = "unknown"
                    verification = "unknown"
                    reason_code = "CLEANUP_INCOMPLETE"
                release_lock = False
        else:
            state, verification, reason_code = evaluate_run_outcome(raw_result)
            result_dict = raw_result.model_dump(mode="json")
        await self._finalize_terminal(
            run_rec,
            state=state,
            verification=verification,
            reason_code=reason_code,
            result_dict=result_dict,
            release_workspace_lock=release_lock,
        )

    @staticmethod
    def _is_native_execute_run() -> bool:
        import forecontract_app.runtime as rt

        return globals().get("execute_run") is rt.execute_run

    def get_run(self, *, session_id: str, run_id: str) -> RunView:
        _, run_rec = self._get_session_and_run(session_id=session_id, run_id=run_id)
        return self._run_view_with_live_usage(run_rec)

    async def cancel_run(self, *, session_id: str, run_id: str) -> RunView:
        view, _ = await self.cancel_run_with_status(session_id=session_id, run_id=run_id)
        return view

    async def cancel_run_with_status(
        self, *, session_id: str, run_id: str
    ) -> tuple[RunView, bool]:
        """Request bounded cancellation; returns ``(RunView, already_terminal)``."""
        _, run_rec = self._get_session_and_run(session_id=session_id, run_id=run_id)
        if run_rec.view.state in _TERMINAL_STATES:
            return run_rec.view.model_copy(deep=True), True

        already_cancelling = run_rec.cancel_requested
        run_rec.cancel_requested = True
        run_rec.view = run_rec.view.model_copy(update={"state": "cancelling"})
        if not already_cancelling:
            watchdog = asyncio.create_task(self._cancel_watchdog(run_rec))
            _attach_background_consumer(watchdog)
        return run_rec.view.model_copy(deep=True), False

    async def _cancel_watchdog(self, run_rec: _RunRecord) -> None:
        """Enforce 5-second cancellation confirmation window and lock retention on unknown."""
        deadline = time.monotonic() + CANCEL_CONFIRMATION_SECONDS
        if run_rec.permissions is not None:
            run_rec.permissions.cancel_pending()
        if run_rec.question_broker is not None:
            run_rec.question_broker.cancel_pending(reason_code="USER_CANCELLED")
        if run_rec.extension_runtime is not None:
            run_rec.extension_runtime.cancel_pending()
            try:
                await asyncio.wait_for(
                    run_rec.extension_runtime.aclose(),
                    timeout=max(0.5, deadline - time.monotonic()),
                )
            except asyncio.TimeoutError:
                pass
        if run_rec.command_runner is not None:
            try:
                await asyncio.wait_for(
                    run_rec.command_runner.stop_all(),
                    timeout=max(0.5, deadline - time.monotonic()),
                )
            except asyncio.TimeoutError:
                await self._finalize_terminal(
                    run_rec,
                    state="unknown",
                    verification="unknown",
                    reason_code="CANCEL_CONFIRMATION_TIMEOUT",
                    result_dict=None,
                    release_workspace_lock=False,
                )
                return
        exec_task = run_rec.exec_task
        if exec_task is not None and not exec_task.done():
            exec_task.cancel()
        elif run_rec.view.state == "queued":
            # Worker has not entered execute_run yet; wait briefly for its queue check.
            try:
                await asyncio.wait_for(run_rec.settled_event.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
            if not run_rec.terminal_finalized:
                await self._finalize_terminal(
                    run_rec,
                    state="cancelled",
                    verification="not_run",
                    reason_code="USER_CANCELLED",
                    result_dict=None,
                    release_workspace_lock=True,
                )
            return

        if exec_task is not None and not exec_task.done():
            remaining = max(0.0, deadline - time.monotonic())
            await asyncio.wait({exec_task}, timeout=remaining)
            if not exec_task.done():
                _attach_background_consumer(exec_task)
                await self._finalize_terminal(
                    run_rec,
                    state="unknown",
                    verification="unknown",
                    reason_code="CANCEL_CONFIRMATION_TIMEOUT",
                    result_dict=None,
                    release_workspace_lock=False,
                )
                return

        if run_rec.wrapped_provider is not None:
            remaining = max(0.0, deadline - time.monotonic())
            settled = await run_rec.wrapped_provider.wait_unsettled(remaining)
            if not settled:
                await self._finalize_terminal(
                    run_rec,
                    state="unknown",
                    verification="unknown",
                    reason_code="CANCEL_CONFIRMATION_TIMEOUT",
                    result_dict=None,
                    release_workspace_lock=False,
                )
                return

        remaining = max(0.0, deadline - time.monotonic())
        if not run_rec.settled_event.is_set() and remaining > 0:
            try:
                await asyncio.wait_for(run_rec.settled_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                await self._finalize_terminal(
                    run_rec,
                    state="unknown",
                    verification="unknown",
                    reason_code="CANCEL_CONFIRMATION_TIMEOUT",
                    result_dict=None,
                    release_workspace_lock=False,
                )

    def snapshot(self, *, session_id: str, run_id: str) -> RunSnapshot:
        _, run_rec = self._get_session_and_run(session_id=session_id, run_id=run_id)
        snap = run_rec.hub.snapshot(self._run_view_with_live_usage(run_rec))
        updates: dict[str, Any] = {}
        if run_rec.permissions is not None:
            updates["pending_proposals"] = [
                p.model_dump(mode="json") for p in run_rec.permissions.pending_proposals()
            ]
        q_source = (
            run_rec.question_broker.list_questions()
            if run_rec.question_broker is not None
            else run_rec.persisted_questions
        )
        updates["pending_questions"] = [
            q.model_dump(mode="json") for q in q_source if q.state == "pending"
        ]
        return snap.model_copy(update=updates)

    def _run_view_with_live_usage(self, run_rec: _RunRecord) -> RunView:
        usage_view = (
            run_rec.usage.snapshot()
            if run_rec.usage is not None and not run_rec.terminal_finalized
            else (run_rec.persisted_usage or (run_rec.usage.snapshot() if run_rec.usage is not None else None))
        )
        return run_rec.view.model_copy(
            deep=True,
            update={"usage": usage_view.model_dump(mode="json") if usage_view is not None else None},
        )

    def get_session_history(
        self, *, session_id: str, after: int = 0, limit: int = 50
    ) -> HistoryPage:
        """Return monotonically ordered paginated history for one session."""
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise AppError("INVALID_CURSOR", "after must be a non-negative integer", status_code=422)
        if isinstance(limit, bool) or not isinstance(limit, int) or not (1 <= limit <= 100):
            raise AppError("INVALID_INPUT", "limit must be an integer in 1..100", status_code=422)
        session = self._sessions.get(session_id)
        if session is None:
            raise AppError("NOT_FOUND", "session not found", status_code=404)
        max_seq = session.history[-1].seq if session.history else 0
        if after > max_seq:
            raise AppError(
                "INVALID_CURSOR",
                "after cursor cannot exceed latest session history seq",
                status_code=422,
            )
        matching = [item for item in session.history if item.seq > after]
        page_items = [item.model_copy(deep=True) for item in matching[:limit]]
        has_more = len(matching) > limit
        next_cursor = page_items[-1].seq if page_items else after
        return HistoryPage(
            session_id=session_id,
            items=page_items,
            cursor=next_cursor,
            has_more=has_more,
        )

    def get_session_context(self, *, session_id: str) -> SessionContextView:
        """Return verbatim fixed constraints, revision, cursor, and storage quota usage."""
        session = self._sessions.get(session_id)
        if session is None:
            raise AppError("NOT_FOUND", "session not found", status_code=404)
        used_bytes, limit_bytes = self._store.storage_stats()
        return SessionContextView(
            session_id=session.view.session_id,
            project_id=session.view.project_id,
            revision=session.view.constraints_revision,
            constraints=session.view.constraints,
            history_cursor=session.view.history_cursor,
            storage_used_bytes=used_bytes,
            storage_limit_bytes=limit_bytes,
        )

    def set_session_constraints(
        self, *, session_id: str, request: SetConstraintsRequest
    ) -> SessionContextView:
        """Persist session fixed constraints atomically with optimistic revision and idempotency."""
        self._require_open()
        session = self._sessions.get(session_id)
        if session is None:
            raise AppError("NOT_FOUND", "session not found", status_code=404)
        validated = SetConstraintsRequest.model_validate(request)
        req_key = str(validated.client_request_id)
        payload_digest = sha256_canonical_json(
            {
                "expected_revision": validated.expected_revision,
                "constraints": validated.constraints,
            }
        )
        prior = session.constraint_idempotency.get(req_key)
        if prior is not None:
            prev_digest, _ = prior
            if prev_digest != payload_digest:
                raise AppError(
                    "IDEMPOTENCY_CONFLICT",
                    "client_request_id reused with different constraints payload",
                    status_code=409,
                )
            return self.get_session_context(session_id=session_id)

        if session.view.active_run_id is not None:
            raise AppError(
                "SESSION_BUSY",
                "cannot modify session constraints while a run is active",
                status_code=409,
            )
        if validated.expected_revision != session.view.constraints_revision:
            raise AppError(
                "REVISION_CONFLICT",
                "expected_revision does not match current session constraints revision",
                status_code=409,
            )

        new_rev = session.view.constraints_revision + 1
        self._store.update_session_constraints(
            session_id=session_id,
            constraints=validated.constraints,
            revision=new_rev,
            client_request_id=req_key,
            payload_digest=payload_digest,
        )
        session.constraint_idempotency[req_key] = (payload_digest, new_rev)
        session.view = session.view.model_copy(
            update={"constraints": validated.constraints, "constraints_revision": new_rev}
        )
        return self.get_session_context(session_id=session_id)

    def list_questions(self, *, session_id: str, run_id: str) -> list[QuestionView]:
        """Return ordered QuestionViews (pending and closed) bound to ``(session_id, run_id)``."""
        _, run_rec = self._get_session_and_run(session_id=session_id, run_id=run_id)
        if run_rec.question_broker is not None:
            return run_rec.question_broker.list_questions()
        return [q.model_copy(deep=True) for q in run_rec.persisted_questions]

    def answer_question(
        self,
        *,
        session_id: str,
        run_id: str,
        question_id: str,
        request: AnswerQuestionRequest,
    ) -> QuestionView:
        """Persist answer before waking any waiting ask_user call in ``(session_id, run_id)``."""
        self._require_open()
        _, run_rec = self._get_session_and_run(session_id=session_id, run_id=run_id)
        validated = AnswerQuestionRequest.model_validate(request)
        req_key = str(validated.client_request_id)
        payload_digest = sha256_canonical_json(
            {
                "question_digest": validated.question_digest,
                "text": validated.text,
            }
        )

        if run_rec.question_broker is not None:
            run_rec.pending_answer_ctx = (req_key, payload_digest)
            try:
                answered = run_rec.question_broker.answer(
                    question_id=question_id,
                    request=validated,
                )
            finally:
                run_rec.pending_answer_ctx = None
            run_rec.answer_idempotency[req_key] = (question_id, payload_digest)
            return answered

        prior = run_rec.answer_idempotency.get(req_key)
        if prior is not None:
            prior_qid, prior_digest = prior
            if prior_qid != question_id or prior_digest != payload_digest:
                raise AppError(
                    "IDEMPOTENCY_CONFLICT",
                    "client_request_id reused with different answer payload",
                    status_code=409,
                )
            matched = next((q for q in run_rec.persisted_questions if q.id == question_id), None)
            if matched is None:
                raise AppError("NOT_FOUND", "question not found", status_code=404)
            return matched.model_copy(deep=True)

        matched = next((q for q in run_rec.persisted_questions if q.id == question_id), None)
        if matched is None:
            raise AppError("NOT_FOUND", "question not found", status_code=404)
        raise AppError(
            "QUESTION_CLOSED",
            f"question is already closed ({matched.state})",
            status_code=409,
        )

    def get_run_usage(self, *, session_id: str, run_id: str) -> UsageView:
        """Return honest UsageView bound to ``(session_id, run_id)``."""
        _, run_rec = self._get_session_and_run(session_id=session_id, run_id=run_id)
        if run_rec.usage is not None and not run_rec.terminal_finalized:
            return run_rec.usage.snapshot()
        if run_rec.persisted_usage is not None:
            return run_rec.persisted_usage.model_copy(deep=True)
        if run_rec.usage is not None:
            return run_rec.usage.snapshot()
        return UsageView(
            calls_started=0,
            calls_completed=0,
            usage_known_calls=0,
            usage_unknown_calls=0,
            known_input_tokens=0,
            known_output_tokens=0,
            input_tokens=0,
            output_tokens=0,
            max_calls=64 if run_rec.project.kind == "existing" else 12,
            steps_used=0,
            max_steps=120 if run_rec.project.kind == "existing" else 12,
            elapsed_ms=0,
            run_timeout_ms=600_000 if run_rec.project.kind == "existing" else 60_000,
        )

    def get_proposals(self, *, session_id: str, run_id: str) -> list[ProposalView]:
        """Return ordered proposal views for an existing-project run."""
        _, run_rec = self._get_session_and_run(session_id=session_id, run_id=run_id)
        if run_rec.permissions is None:
            return []
        return [p.model_copy(deep=True) for p in run_rec.permissions.list_proposals()]

    async def decide_proposal(
        self,
        *,
        session_id: str,
        run_id: str,
        proposal_id: str,
        request: ProposalDecision,
    ) -> ProposalView:
        """Apply a digest-bound approve/reject decision for one proposal."""
        self._require_open()
        _, run_rec = self._get_session_and_run(session_id=session_id, run_id=run_id)
        if run_rec.permissions is None:
            raise ProjectError("NOT_FOUND", "proposal not found", status_code=404)
        validated = ProposalDecision.model_validate(request)
        return run_rec.permissions.decide(proposal_id=proposal_id, request=validated)

    def get_command(
        self,
        *,
        session_id: str,
        run_id: str,
        command_id: str,
        after: int = 0,
    ) -> CommandView:
        """Return bounded incremental command state and chunks after ``after``."""
        _, run_rec = self._get_session_and_run(session_id=session_id, run_id=run_id)
        if run_rec.command_runner is None:
            raise ProjectError("NOT_FOUND", "command not found", status_code=404)
        return run_rec.command_runner.get_command(command_id, after=after)

    async def stop_command(self, *, session_id: str, run_id: str, command_id: str) -> CommandView:
        """Terminate one active command cleanly within the run's Job object."""
        view, _ = await self.stop_command_with_status(
            session_id=session_id, run_id=run_id, command_id=command_id
        )
        return view

    async def stop_command_with_status(
        self, *, session_id: str, run_id: str, command_id: str
    ) -> tuple[CommandView, bool]:
        """Stop an active command; returns ``(CommandView, already_terminal)``."""
        self._require_open()
        _, run_rec = self._get_session_and_run(session_id=session_id, run_id=run_id)
        if run_rec.command_runner is None:
            raise ProjectError("NOT_FOUND", "command not found", status_code=404)
        return await run_rec.command_runner.stop_command(command_id)

    def list_project_skills(self, *, project_id: str) -> list[SkillInfo]:
        """Return read-only single-level Skill catalog for one project without spawning."""
        project = self._projects.get(project_id)
        if project is None:
            raise AppError("NOT_FOUND", "project not found", status_code=404)
        if project.kind != "existing":
            return []
        if not project.binding_valid:
            raise AppError(
                "PROJECT_BINDING_INVALID",
                "persisted project root is no longer permitted by current HostConfig",
                status_code=403,
            )
        reg_spec = project.spec
        assert isinstance(reg_spec, _RegisteredProjectSpec)
        return SkillCatalog(reg_spec.root).list_skills()

    def _resolve_existing_project_root(self, *, project_id: str) -> Path:
        """Verify project existence, existing kind, read grant, and current HostConfig root binding."""
        project = self._projects.get(project_id)
        if project is None:
            raise AppError("NOT_FOUND", "project not found", status_code=404)
        if project.kind != "existing":
            raise AppError(
                "PROJECT_REVIEW_UNAVAILABLE",
                "file and git review require a registered existing project",
                status_code=403,
            )
        if not project.binding_valid:
            raise AppError(
                "PROJECT_BINDING_INVALID",
                "persisted project root is no longer permitted by current HostConfig",
                status_code=403,
            )
        reg_spec = project.spec
        if not isinstance(reg_spec, _RegisteredProjectSpec) or not reg_spec.read_grant_id:
            raise AppError(
                "PROJECT_BINDING_INVALID",
                "registered project lacks a valid read grant",
                status_code=403,
            )
        if not reg_spec.root.is_dir():
            raise AppError(
                "PROJECT_BINDING_INVALID",
                "registered project root directory no longer exists",
                status_code=403,
            )
        try:
            check_no_reparse_ancestors(reg_spec.root, label="project_root")
            canonical = Path(os.path.realpath(reg_spec.root))
            check_no_reparse_ancestors(canonical, label="project_root")
        except (AppError, OSError) as exc:
            raise AppError(
                "PROJECT_BINDING_INVALID",
                "persisted project root is no longer permitted by current HostConfig",
                status_code=403,
            ) from exc
        if (
            paths_overlap(canonical, self._config.data_root)
            or paths_overlap(canonical, self._config.workspace_parent)
            or not any(canonical == allowed for allowed in self._project_roots)
        ):
            raise AppError(
                "PROJECT_BINDING_INVALID",
                "persisted project root is no longer permitted by current HostConfig",
                status_code=403,
            )
        return canonical

    def list_project_files(
        self,
        *,
        project_id: str,
        path: str = ".",
        cursor: str | None = None,
        limit: int = 100,
    ) -> ListDirectoryResult:
        """List directory entries for one bound existing project."""
        root = self._resolve_existing_project_root(project_id=project_id)
        return ProjectReviewService(data_root=self._config.data_root).list_files(
            root=root, path=path, cursor=cursor, limit=limit
        )

    def read_project_file(
        self,
        *,
        project_id: str,
        path: str,
        start_line: int = 1,
        byte_offset: int = 0,
        expected_sha256: str | None = None,
    ) -> ReadFileResult:
        """Read bounded UTF-8 file content for one bound existing project."""
        root = self._resolve_existing_project_root(project_id=project_id)
        return ProjectReviewService(data_root=self._config.data_root).read_file(
            root=root,
            path=path,
            start_line=start_line,
            byte_offset=byte_offset,
            expected_sha256=expected_sha256,
        )

    def get_project_git_status(self, *, project_id: str) -> ProjectGitStatusView:
        """Inspect bounded read-only local Git status for one bound existing project."""
        root = self._resolve_existing_project_root(project_id=project_id)
        return ProjectReviewService(data_root=self._config.data_root).git_status(
            project_id=project_id, root=root
        )

    def get_project_git_diff(
        self,
        *,
        project_id: str,
        path: str,
        area: Literal["index", "worktree"],
    ) -> ProjectGitDiffView:
        """Inspect bounded read-only Git diff for one path and review area."""
        root = self._resolve_existing_project_root(project_id=project_id)
        return ProjectReviewService(data_root=self._config.data_root).git_diff(
            project_id=project_id, root=root, path=path, area=area
        )

    def list_extension_servers(self) -> list[McpServerSummary]:
        """Return sanitized host-configured MCP servers without spawning or network I/O."""
        return [
            sanitize_server_summary(srv, config_sha256=self._extension_config.config_sha256)
            for srv in self._extension_config.servers
        ]

    def get_run_extensions(self, *, session_id: str, run_id: str) -> RunExtensionsView:
        """Return bounded run-level Skill/MCP server, proposal, and call summary view."""
        _, run_rec = self._get_session_and_run(session_id=session_id, run_id=run_id)
        if run_rec.extension_runtime is not None:
            return run_rec.extension_runtime.build_run_extensions_view()
        if run_rec.persisted_extensions is not None:
            return run_rec.persisted_extensions.model_copy(deep=True)
        return RunExtensionsView(
            project_id=run_rec.view.project_id,
            session_id=session_id,
            run_id=run_id,
            selected_skills=list(run_rec.selected_skills_info),
        )

    async def decide_extension_proposal(
        self,
        *,
        session_id: str,
        run_id: str,
        proposal_id: str,
        request: ExtensionProposalDecision,
    ) -> ExtensionProposalView:
        """Apply a digest-bound approve/reject decision for one in-run extension proposal."""
        self._require_open()
        _, run_rec = self._get_session_and_run(session_id=session_id, run_id=run_id)
        if run_rec.extension_runtime is None:
            raise AppError("NOT_FOUND", "extension proposal not found", status_code=404)
        validated = ExtensionProposalDecision.model_validate(request)
        return await run_rec.extension_runtime.decide(
            proposal_id=proposal_id,
            digest=validated.digest,
            decision=validated.decision,
        )

    def _resolve_project_workspace_root_for_chat(self, *, project_id: str) -> Path:
        project = self._projects.get(project_id)
        if project is None:
            raise AppError("NOT_FOUND", "project not found", status_code=404)
        if project.kind == "existing":
            return self._resolve_existing_project_root(project_id=project_id)
        if not project.binding_valid:
            raise AppError(
                "PROJECT_BINDING_INVALID",
                "persisted project root is no longer permitted by current HostConfig",
                status_code=403,
            )
        spec = project.spec
        if not isinstance(spec, ControlledProjectSpec) or not spec.workspace_dir.is_dir():
            raise AppError(
                "PROJECT_BINDING_INVALID",
                "controlled workspace directory no longer exists",
                status_code=403,
            )
        check_no_reparse_ancestors(spec.workspace_dir, label="workspace_root")
        return Path(os.path.realpath(spec.workspace_dir))

    @staticmethod
    def _parse_hunks(diff_excerpt: str) -> list[tuple[int, int, int, int, list[str]]]:
        hunk_re = re.compile(r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@")
        hunks: list[tuple[int, int, int, int, list[str]]] = []
        cur_header: tuple[int, int, int, int] | None = None
        cur_lines: list[str] = []
        for line in diff_excerpt.splitlines():
            m = hunk_re.match(line)
            if m:
                if cur_header is not None:
                    hunks.append((*cur_header, cur_lines))
                old_s = int(m.group(1))
                old_c = int(m.group(2)) if m.group(2) is not None else 1
                new_s = int(m.group(3))
                new_c = int(m.group(4)) if m.group(4) is not None else 1
                cur_header = (old_s, old_c, new_s, new_c)
                cur_lines = [line]
            elif cur_header is not None:
                cur_lines.append(line)
        if cur_header is not None:
            hunks.append((*cur_header, cur_lines))
        return hunks

    def _resolve_diff_hunk_evidence(
        self, *, run_rec: _RunRecord, ref: DiffHunkReference
    ) -> ResolvedChatEvidence:
        result = run_rec.view.result if isinstance(run_rec.view.result, dict) else {}
        diff_obj = result.get("diff") if isinstance(result.get("diff"), dict) else {}
        entries = diff_obj.get("entries") if isinstance(diff_obj.get("entries"), list) else []
        matched_entry: dict[str, Any] | None = None
        for ent in entries:
            if isinstance(ent, dict) and ent.get("path") == ref.path:
                matched_entry = ent
                break
        if matched_entry is None:
            raise AppError(
                "NOT_FOUND",
                f"referenced diff path '{ref.path}' not found in run",
                status_code=404,
            )
        if (
            matched_entry.get("before_sha256") != ref.before_sha256
            or matched_entry.get("after_sha256") != ref.after_sha256
        ):
            raise AppError(
                "EVIDENCE_MISMATCH",
                "diff reference SHA-256 does not match run evidence",
                status_code=409,
            )
        diff_excerpt = str(matched_entry.get("diff_excerpt") or "")
        hunks = self._parse_hunks(diff_excerpt)
        matched_hunk_lines: list[str] | None = None
        for h_old_s, h_old_c, h_new_s, h_new_c, h_lines in hunks:
            old_ok = True
            if ref.before_sha256 is not None and ref.old_start is not None:
                req_old_end = ref.old_start + max((ref.old_count or 1) - 1, 0)
                h_old_end = h_old_s + max(h_old_c - 1, 0)
                old_ok = h_old_c > 0 and ref.old_start >= h_old_s and req_old_end <= h_old_end
            new_ok = True
            if ref.after_sha256 is not None and ref.new_start is not None:
                req_new_end = ref.new_start + max((ref.new_count or 1) - 1, 0)
                h_new_end = h_new_s + max(h_new_c - 1, 0)
                new_ok = h_new_c > 0 and ref.new_start >= h_new_s and req_new_end <= h_new_end
            if old_ok and new_ok:
                matched_hunk_lines = h_lines
                break
        if matched_hunk_lines is None:
            raise AppError(
                "EVIDENCE_OUT_OF_RANGE",
                "requested diff hunk range is outside recorded run diff",
                status_code=422,
            )
        snippet = sanitize_evidence_text("\n".join(matched_hunk_lines))
        bounded_snippet, was_cut = truncate_utf8_safe(snippet, 1536)
        line_part = (
            f"L{ref.new_start}"
            if ref.new_start is not None and ref.new_start >= 1
            else f"old L{ref.old_start}"
        )
        return ResolvedChatEvidence(
            kind="diff_hunk",
            run_id=ref.run_id,
            availability="available",
            label=f"{ref.path} ({line_part}) · Run {ref.run_id[:8]}",
            path=ref.path,
            before_sha256=ref.before_sha256,
            after_sha256=ref.after_sha256,
            old_start=ref.old_start,
            old_count=ref.old_count,
            new_start=ref.new_start,
            new_count=ref.new_count,
            excerpt=bounded_snippet,
            truncated=was_cut,
        )

    def _resolve_command_evidence(
        self, *, run_rec: _RunRecord, ref: CommandReference
    ) -> ResolvedChatEvidence:
        result = run_rec.view.result if isinstance(run_rec.view.result, dict) else {}
        checks = result.get("checks") if isinstance(result.get("checks"), dict) else {}
        check_rec: dict[str, Any] | None = None
        for val in checks.values():
            if isinstance(val, dict) and val.get("command_id") == ref.command_id:
                check_rec = val
                break

        cmd_view = None
        if run_rec.command_runner is not None:
            try:
                cmd_view = run_rec.command_runner.get_command(ref.command_id, after=0)
            except (AppError, ProjectError):
                cmd_view = None

        if check_rec is None and cmd_view is None:
            raise AppError(
                "NOT_FOUND",
                f"referenced command '{ref.command_id}' not found in run",
                status_code=404,
            )

        exit_code = (
            check_rec.get("exit_code")
            if check_rec is not None
            else (cmd_view.exit_code if cmd_view is not None else None)
        )
        passed = check_rec.get("passed") if check_rec is not None else None
        raw_failure_reason = (
            str(check_rec["failure_reason"])
            if check_rec is not None and check_rec.get("failure_reason") is not None
            else None
        )
        failure_reason = (
            sanitize_evidence_text(raw_failure_reason)
            if raw_failure_reason is not None
            else None
        )
        raw_argv = (
            [str(a) for a in (check_rec.get("argv") or [])]
            if check_rec is not None
            else ([str(a) for a in cmd_view.argv] if cmd_view is not None else [])
        )
        argv = [sanitize_evidence_text(a) for a in raw_argv]

        raw_chunks = (
            "".join(ch.text for ch in cmd_view.chunks)
            if cmd_view is not None and cmd_view.chunks
            else ""
        )
        if raw_chunks.strip():
            sanitized = sanitize_evidence_text(raw_chunks)
            bounded, was_cut = truncate_utf8_safe(sanitized, 1536)
            return ResolvedChatEvidence(
                kind="command",
                run_id=ref.run_id,
                availability="available",
                label=f"{ref.command_id} · Run {ref.run_id[:8]} · exit={exit_code}",
                command_id=ref.command_id,
                exit_code=exit_code,
                passed=passed,
                failure_reason=failure_reason,
                argv=argv,
                excerpt=bounded,
                truncated=was_cut,
            )

        return ResolvedChatEvidence(
            kind="command",
            run_id=ref.run_id,
            availability="raw_output_unavailable",
            label=f"{ref.command_id} · Run {ref.run_id[:8]} · exit={exit_code}",
            command_id=ref.command_id,
            exit_code=exit_code,
            passed=passed,
            failure_reason=failure_reason,
            argv=argv,
            excerpt="",
            truncated=False,
            note="原始输出不可用",
        )

    def _resolve_chat_references(
        self, *, session: _SessionRecord, references: list[ChatReferenceItem]
    ) -> list[ResolvedChatEvidence]:
        resolved: list[ResolvedChatEvidence] = []
        for ref in references:
            run_rec = session.runs.get(ref.run_id)
            if run_rec is None:
                raise AppError(
                    "NOT_FOUND",
                    f"referenced run '{ref.run_id}' not found in session",
                    status_code=404,
                )
            if isinstance(ref, DiffHunkReference):
                resolved.append(self._resolve_diff_hunk_evidence(run_rec=run_rec, ref=ref))
            else:
                resolved.append(self._resolve_command_evidence(run_rec=run_rec, ref=ref))
        return resolved

    def _auto_select_session_offer_diff_evidence(
        self, *, session: _SessionRecord, limit: int = 4
    ) -> list[ResolvedChatEvidence]:
        run_ids = self._store.list_started_offer_run_ids_desc(
            session_id=session.view.session_id
        )
        seen_runs: set[str] = set()
        selected: list[ResolvedChatEvidence] = []
        for run_id in run_ids:
            if not run_id or run_id in seen_runs:
                continue
            seen_runs.add(run_id)
            run_rec = session.runs.get(run_id)
            if run_rec is None or run_rec.view.state not in {"succeeded", "failed", "cancelled"}:
                continue
            result = run_rec.view.result if isinstance(run_rec.view.result, dict) else {}
            diff_obj = result.get("diff") if isinstance(result.get("diff"), dict) else {}
            entries = diff_obj.get("entries") if isinstance(diff_obj.get("entries"), list) else []
            for ent in entries:
                if not isinstance(ent, dict) or not ent.get("path"):
                    continue
                hunks = self._parse_hunks(str(ent.get("diff_excerpt") or ""))
                if not hunks:
                    continue
                h_old_s, h_old_c, h_new_s, h_new_c, _ = hunks[0]
                before_sha = ent.get("before_sha256")
                after_sha = ent.get("after_sha256")
                ref = DiffHunkReference(
                    kind="diff_hunk",
                    run_id=run_id,
                    path=str(ent["path"]),
                    before_sha256=before_sha,
                    after_sha256=after_sha,
                    old_start=h_old_s if (before_sha is not None and h_old_c > 0) else None,
                    old_count=h_old_c if (before_sha is not None and h_old_c > 0) else None,
                    new_start=h_new_s if (after_sha is not None and h_new_c > 0) else None,
                    new_count=h_new_c if (after_sha is not None and h_new_c > 0) else None,
                )
                try:
                    selected.append(self._resolve_diff_hunk_evidence(run_rec=run_rec, ref=ref))
                except AppError:
                    continue
                if len(selected) >= limit:
                    return selected
            if selected:
                return selected
        return selected

    async def start_chat_turn(
        self, *, session_id: str, request: CreateChatTurnRequest
    ) -> tuple[ChatTurnView, int]:
        """Execute one read-only chat turn with strict session/project binding and idempotency."""
        self._require_open()
        session = self._sessions.get(session_id)
        if session is None:
            raise AppError("NOT_FOUND", "session not found", status_code=404)
        validated = CreateChatTurnRequest.model_validate(request)
        idem_key = str(validated.client_request_id)
        digest_payload: dict[str, Any] = {"text": validated.text}
        if validated.references:
            digest_payload["references"] = [
                r.model_dump(mode="json") for r in validated.references
            ]
        if validated.intent_mode != "readonly":
            digest_payload["intent_mode"] = validated.intent_mode
        payload_digest = sha256_canonical_json(digest_payload)

        existing = self._store.get_chat_turn_by_client_request_id(
            session_id=session_id,
            client_request_id=idem_key,
        )
        if existing is not None:
            prev_digest, prev_turn = existing
            if prev_digest != payload_digest:
                raise AppError(
                    "IDEMPOTENCY_CONFLICT",
                    "client_request_id reused with different payload",
                    status_code=409,
                )
            if prev_turn.status == "running":
                return prev_turn, 202
            return prev_turn, 200

        project = self._projects.get(session.view.project_id)
        if project is None:
            raise AppError("NOT_FOUND", "project not found", status_code=404)
        pid = project.spec.view.project_id
        root_key = project.canonical_root
        project_root = self._resolve_project_workspace_root_for_chat(project_id=pid)

        if (
            pid in self._poisoned_workspaces
            or pid in self._active_workspaces
            or root_key in self._poisoned_root_keys
            or root_key in self._active_root_keys
        ):
            raise AppError(
                "WORKSPACE_BUSY",
                "workspace already has an active or unconfirmed run",
                status_code=409,
            )
        if pid in self._active_chat_projects or session_id in self._active_chat_sessions:
            raise AppError(
                "CHAT_TURN_BUSY",
                "another read-only chat turn is already in progress",
                status_code=409,
            )

        # Verify all referenced evidence BEFORE selecting/invoking any provider.
        if validated.references:
            resolved_evidence = self._resolve_chat_references(
                session=session, references=validated.references
            )
        elif validated.intent_mode == "conversation":
            resolved_evidence = self._auto_select_session_offer_diff_evidence(session=session)
        else:
            resolved_evidence = []

        if self._chat_provider_factory is not None:
            provider_mode = "offline" if session.view.model.provider == "offline" else session.view.model.provider
        elif session.view.model.provider == "offline":
            provider_mode = "offline"
        else:
            if self._live_chat_factory is None or self._config.live is None:
                raise AppError(
                    "LIVE_DISABLED",
                    "live chat provider is disabled in offline mode",
                    status_code=409,
                )
            if session.view.model.model != self._config.live.model:
                raise AppError(
                    "MODEL_NOT_ALLOWED",
                    "requested live model is not the host-configured model",
                    status_code=409,
                )
            provider_mode = "live"

        self._active_chat_projects.add(pid)
        self._active_chat_sessions.add(session_id)
        try:
            turn_id = str(uuid.uuid4())
            now_iso = utc_now().isoformat()
            running_turn = self._store.insert_chat_turn_running(
                turn_id=turn_id,
                project_id=pid,
                session_id=session_id,
                client_request_id=idem_key,
                payload_digest=payload_digest,
                user_message=validated.text,
                provider_mode=provider_mode,
                created_at=now_iso,
                references=validated.references,
                resolved_evidence=resolved_evidence,
                intent_mode=validated.intent_mode,
            )
            completed_turns = self._store.list_recent_completed_chat_turns(
                session_id=session_id,
            )
            if self._chat_provider_factory is not None:
                provider = self._chat_provider_factory(
                    project=project.spec.view,
                    session_id=session_id,
                    turn_id=turn_id,
                )
            elif session.view.model.provider == "offline":
                provider = OfflineReadonlyChatProvider()
            else:
                assert self._live_chat_factory is not None
                provider = self._live_chat_factory(
                    project=project.spec.view,
                    session_id=session_id,
                    turn_id=turn_id,
                )

            tool_reads, assistant_msg, offer_tuple, ctx_cut, err_code, err_msg = await execute_conversation_chat_turn(
                project_id=pid,
                session_id=session_id,
                turn_id=turn_id,
                project_root=project_root,
                data_root=self._config.data_root,
                constraints=session.view.constraints,
                question=validated.text,
                completed_turns=completed_turns,
                provider=provider,
                resolved_evidence=resolved_evidence,
                intent_mode=validated.intent_mode,
            )
            finished_at = utc_now().isoformat()
            final_status = "completed" if err_code is None else "failed"
            offer_view: ChatExecutionOfferView | None = None
            if offer_tuple is not None and err_code is None:
                offer_goal, offer_summary = offer_tuple
                offer_id = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"forecontract:offer:{session_id}:{turn_id}",
                    )
                )
                run_idem = str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"forecontract:offer-run:{session_id}:{turn_id}:{offer_id}",
                    )
                )
                offer_view = ChatExecutionOfferView(
                    offer_id=offer_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    client_request_id=idem_key,
                    goal=offer_goal,
                    summary=offer_summary,
                    status="pending_confirm",
                    run_id=None,
                    run_client_request_id=run_idem,
                    created_at=finished_at,
                    updated_at=finished_at,
                )
            final_turn = running_turn.model_copy(
                update={
                    "status": final_status,
                    "tool_reads": tool_reads,
                    "assistant_message": assistant_msg,
                    "execution_offer": offer_view,
                    "error_code": err_code,
                    "error_message": err_msg,
                    "context_truncated": ctx_cut,
                    "updated_at": finished_at,
                }
            )
            self._store.finalize_chat_turn(turn=final_turn)
            return final_turn, 201
        finally:
            self._active_chat_projects.discard(pid)
            self._active_chat_sessions.discard(session_id)

    def list_chat_turns(
        self, *, session_id: str, after_seq: int = 0, limit: int = 50
    ) -> ChatTurnsPage:
        self._require_open()
        if session_id not in self._sessions:
            raise AppError("NOT_FOUND", "session not found", status_code=404)
        return self._store.list_chat_turns(
            session_id=session_id, after_seq=after_seq, limit=limit
        )

    async def confirm_chat_execution_offer(
        self,
        *,
        session_id: str,
        turn_id: str,
        request: ConfirmChatOfferRequest,
    ) -> tuple[RunView, bool]:
        """Confirm a persisted execution offer and start/replay its bound run."""
        self._require_open()
        session = self._sessions.get(session_id)
        if session is None:
            raise AppError("NOT_FOUND", "session not found", status_code=404)
        validated = ConfirmChatOfferRequest.model_validate(request)
        if not validated.confirmed_scope:
            raise AppError(
                "SCOPE_NOT_CONFIRMED",
                "user must explicitly confirm project scope before execution",
                status_code=403,
            )
        locks = getattr(self, "_offer_confirm_locks", None)
        if locks is None:
            locks = {}
            self._offer_confirm_locks = locks
        lock_key = (session_id, turn_id)
        lock = locks.get(lock_key)
        if lock is None:
            lock = asyncio.Lock()
            locks[lock_key] = lock

        async with lock:
            offer = self._store.get_chat_execution_offer(
                session_id=session_id, turn_id=turn_id
            )
            if offer is None or offer.offer_id != validated.offer_id:
                raise AppError(
                    "NOT_FOUND",
                    "execution offer not found for session turn",
                    status_code=404,
                )
            if offer.run_id is not None:
                existing_run = session.runs.get(offer.run_id)
                if existing_run is not None:
                    return existing_run.view.model_copy(deep=True), True
            run_view, replayed = await self.start_run_with_replay(
                session_id=session_id,
                request=StartRunRequest(
                    client_request_id=offer.run_client_request_id,
                    goal=offer.goal,
                    confirmed_scope=True,
                    skill_ids=validated.selected_skill_ids,
                ),
            )
            self._store.mark_chat_execution_offer_started(
                session_id=session_id,
                turn_id=turn_id,
                offer_id=offer.offer_id,
                run_id=run_view.run_id,
                updated_at=utc_now().isoformat(),
            )
            return run_view, replayed

    def get_chat_turn_by_client_request_id(
        self, *, session_id: str, client_request_id: str
    ) -> ChatTurnView:
        self._require_open()
        if session_id not in self._sessions:
            raise AppError("NOT_FOUND", "session not found", status_code=404)
        res = self._store.get_chat_turn_by_client_request_id(
            session_id=session_id, client_request_id=client_request_id
        )
        if res is None:
            raise AppError("NOT_FOUND", "chat turn not found", status_code=404)
        return res[1]

    def list_project_session_titles(self, *, project_id: str) -> ProjectSessionTitlesView:
        self._require_open()
        if project_id not in self._projects:
            raise AppError("NOT_FOUND", "project not found", status_code=404)
        titles = self._store.list_project_session_titles(project_id=project_id)
        return ProjectSessionTitlesView(project_id=project_id, titles=titles)

    def open_event_subscription(
        self, *, session_id: str, run_id: str, after: int = 0
    ) -> tuple[RunEventHub, list[AppEvent], Any]:
        _, run_rec = self._get_session_and_run(session_id=session_id, run_id=run_id)
        replay, sub = run_rec.hub.open_subscription(after=after)
        return run_rec.hub, replay, sub

    async def events(
        self, *, session_id: str, run_id: str, after: int = 0
    ) -> AsyncIterator[AppEvent]:
        _, run_rec = self._get_session_and_run(session_id=session_id, run_id=run_id)
        async for ev in run_rec.hub.stream_events(after=after):
            yield ev

    async def aclose(self) -> None:
        """Idempotently cancel active runs within an 8s window and release data lock."""
        if self._closed:
            return
        self._closed = True
        try:
            active_runs = [
                rec for rec in self._runs_by_id.values() if rec.view.state not in _TERMINAL_STATES
            ]
            for rec in active_runs:
                await self.cancel_run(session_id=rec.view.session_id, run_id=rec.view.run_id)
            if active_runs:
                waiters = [asyncio.create_task(rec.settled_event.wait()) for rec in active_runs]
                _, pending = await asyncio.wait(waiters, timeout=SHUTDOWN_CONFIRMATION_SECONDS)
                for w in pending:
                    w.cancel()
                    _attach_background_consumer(w)
                for rec in active_runs:
                    if not rec.terminal_finalized:
                        await self._finalize_terminal(
                            rec,
                            state="unknown",
                            verification="unknown",
                            reason_code="SHUTDOWN_CONFIRMATION_TIMEOUT",
                            result_dict=None,
                            release_workspace_lock=False,
                        )
        finally:
            try:
                if self._live_factory is not None:
                    await self._live_factory.aclose()
            finally:
                self._store.close()
                self._lock.release()
