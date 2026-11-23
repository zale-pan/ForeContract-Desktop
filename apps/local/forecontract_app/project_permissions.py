"""Two-stage immutable proposal broker with strict identity, non-refreshing TTL, and revocation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any, Final

from forecontract_app.project_models import (
    PROJECT_PROPOSAL_TTL_SECONDS,
    CommandProposal,
    ProjectError,
    ProposalDecision,
    ProposalKind,
    ProposalView,
    ScopeProposalPayload,
    VerificationPlan,
)
from forecontract_app.project_workspace import (
    _is_excluded_rel_parts,
    _is_write_protected_rel,
    normalize_relative_dir,
    normalize_relative_file,
    sha256_bytes,
)
from forecontract_app.security import check_no_reparse_ancestors, is_reparse_or_symlink

_FORBIDDEN_SHELL_NAMES: Final[frozenset[str]] = frozenset(
    {"cmd.exe", "powershell.exe", "pwsh.exe", "bash.exe", "sh.exe", "bash", "sh"}
)


def _canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _validate_host_executable(raw_exe: str) -> tuple[Path, str]:
    """Validate an explicit absolute non-shell executable path and compute its SHA-256."""
    if not isinstance(raw_exe, str) or not raw_exe.strip() or "\x00" in raw_exe:
        raise ProjectError("INVALID_EXECUTABLE", "executable must be non-blank", status_code=400)
    if any(ord(ch) < 32 for ch in raw_exe):
        raise ProjectError("INVALID_EXECUTABLE", "executable contains control bytes", status_code=400)
    if raw_exe.startswith(("\\\\", "//")):
        raise ProjectError("INVALID_EXECUTABLE", "UNC executable paths are forbidden", status_code=400)
    win = PureWindowsPath(raw_exe)
    if not win.is_absolute():
        raise ProjectError(
            "INVALID_EXECUTABLE",
            "relative executable names/paths are forbidden; require explicit absolute executable",
            status_code=400,
        )
    if any(part in (".", "..") for part in win.parts):
        raise ProjectError("INVALID_EXECUTABLE", "traversal segments forbidden in executable", status_code=400)
    if win.name.lower() in _FORBIDDEN_SHELL_NAMES:
        raise ProjectError(
            "SHELL_EXECUTABLE_FORBIDDEN",
            f"shell executable '{win.name}' is forbidden",
            status_code=403,
        )
    exe_path = Path(raw_exe)
    try:
        check_no_reparse_ancestors(exe_path, label="executable")
    except Exception as exc:
        raise ProjectError("INVALID_EXECUTABLE", "executable or ancestor is a reparse point", status_code=400) from exc
    if not exe_path.exists() or not exe_path.is_file():
        raise ProjectError("INVALID_EXECUTABLE", "executable does not exist on disk", status_code=400)
    real_exe = Path(os.path.realpath(exe_path))
    if not real_exe.is_file() or is_reparse_or_symlink(real_exe):
        raise ProjectError("INVALID_EXECUTABLE", "executable cannot be resolved safely", status_code=400)
    try:
        exe_sha = sha256_bytes(real_exe.read_bytes())
    except OSError as exc:
        raise ProjectError("INVALID_EXECUTABLE", "failed to hash executable", status_code=400) from exc
    return real_exe, exe_sha


def validate_safe_relative_cwd(project_root: Path, rel_cwd: str) -> Path:
    """Validate relative cwd segment-by-segment before resolving inside ``project_root``."""
    norm = normalize_relative_dir(rel_cwd)
    real_root = Path(os.path.realpath(project_root))
    check_no_reparse_ancestors(real_root, label="project_root")
    if norm == ".":
        return real_root
    parts = norm.split("/")
    cur = real_root
    for part in parts:
        cur = cur / part
        if os.path.lexists(cur) and is_reparse_or_symlink(cur):
            raise ProjectError("INVALID_CWD", "cwd contains symlink/reparse point", status_code=403)
        if not cur.exists() or not cur.is_dir():
            raise ProjectError("INVALID_CWD", f"cwd '{norm}' is not an existing directory", status_code=400)
    real_cwd = Path(os.path.realpath(cur))
    if real_cwd != real_root and real_root not in real_cwd.parents:
        raise ProjectError("INVALID_CWD", "cwd escapes project root", status_code=403)
    return real_cwd


def _normalize_scope_prefix(raw: str) -> str:
    norm = normalize_relative_dir(raw)
    if norm == ".":
        raise ProjectError("INVALID_SCOPE", "root-wide '.' write prefix is forbidden; name subdirectories or exact files", status_code=400)
    parts = tuple(norm.split("/"))
    if _is_excluded_rel_parts(parts, is_dir=True):
        raise ProjectError("PROTECTED_PATH_FORBIDDEN", f"write prefix '{norm}' targets excluded directory", status_code=403)
    return norm + "/"


def _instruction_file_dir(rel_file: str) -> str:
    return rel_file.rsplit("/", 1)[0] if "/" in rel_file else "."


@dataclass
class _ProposalRecord:
    view: ProposalView
    bound_root_identity: str
    bound_instruction_sha256: str
    bound_instruction_files: dict[str, str] | None = None
    bound_instruction_dirs: frozenset[str] = frozenset()
    scope_spec: ScopeProposalPayload | None = None
    normalized_exact_paths: frozenset[str] = frozenset()
    normalized_prefixes: tuple[str, ...] = ()
    normalized_protected: frozenset[str] = frozenset()
    plan_spec: VerificationPlan | None = None
    protected_shas: dict[str, str] = field(default_factory=dict)
    command_spec: CommandProposal | None = None
    consumed: bool = False
    event: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass(frozen=True)
class CommandGrantReceipt:
    proposal_id: str
    proposal_digest: str
    command: CommandProposal
    resolved_cwd: Path
    plan_id: str | None = None
    plan_digest: str | None = None


class PermissionBroker:
    """Thread/loop-local immutable human approval registry for APP-T02 existing projects."""

    def __init__(
        self,
        *,
        run_id: str,
        project_root: Path,
        project_id: str = "default-project",
        session_id: str = "default-session",
        root_identity: str | None = None,
        instruction_sha256: str = "",
        ttl_seconds: float = PROJECT_PROPOSAL_TTL_SECONDS,
        clock: Callable[[], float] | None = None,
        on_proposal_changed: Callable[[str, ProposalView], None] | None = None,
    ) -> None:
        self._run_id = run_id
        self._project_id = project_id
        self._session_id = session_id
        self._root = Path(os.path.realpath(project_root))
        self._root_identity = root_identity or os.path.normcase(str(self._root))
        self._instruction_sha256 = instruction_sha256
        self._ttl_seconds = float(ttl_seconds)
        self._clock = clock or time.monotonic
        self._on_proposal_changed = on_proposal_changed

        self._proposals: dict[str, _ProposalRecord] = {}
        self._order: list[str] = []
        self._cmd_seq = 0
        self._revoked = False
        self._active_scope_id: str | None = None
        self._active_plan_id: str | None = None
        self._plan_revision = 0

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def root_identity(self) -> str:
        return self._root_identity

    @property
    def plan_revision(self) -> int:
        return self._plan_revision

    def update_instruction_sha256(self, sha256_value: str) -> None:
        self._instruction_sha256 = sha256_value

    def _emit(self, kind: str, view: ProposalView) -> None:
        if self._on_proposal_changed is not None:
            self._on_proposal_changed(kind, view.model_copy(deep=True))

    def _compute_digest(
        self,
        *,
        prop_id: str,
        kind: ProposalKind,
        instruction_sha256: str,
        payload: dict[str, Any],
    ) -> str:
        body = {
            "id": prop_id,
            "kind": kind,
            "project_id": self._project_id,
            "session_id": self._session_id,
            "run_id": self._run_id,
            "root_identity": self._root_identity,
            "instruction_sha256": instruction_sha256,
            "payload": payload,
        }
        return hashlib.sha256(_canonical_json(body).encode("utf-8")).hexdigest()

    def _expire_if_needed(self, rec: _ProposalRecord) -> None:
        now = self._clock()
        if rec.view.state in ("pending", "approved") and not rec.consumed and now > rec.view.expires_at:
            rec.view = rec.view.model_copy(update={"state": "expired"})
            rec.event.set()
            self._emit("proposal_decided", rec.view)

    def _expire_all(self) -> None:
        for pid in self._order:
            self._expire_if_needed(self._proposals[pid])

    def _new_record(
        self,
        *,
        prop_id: str,
        kind: ProposalKind,
        payload: dict[str, Any],
        instruction_sha256: str,
        bound_instruction_files: dict[str, str] | None = None,
        bound_instruction_dirs: Iterable[str] = (),
        scope_spec: ScopeProposalPayload | None = None,
        normalized_exact: frozenset[str] = frozenset(),
        normalized_prefixes: tuple[str, ...] = (),
        normalized_protected: frozenset[str] = frozenset(),
        plan_spec: VerificationPlan | None = None,
        protected_shas: dict[str, str] | None = None,
        command_spec: CommandProposal | None = None,
    ) -> _ProposalRecord:
        if self._revoked:
            raise ProjectError("PROPOSAL_CANCELLED", "broker has been cancelled", status_code=409)
        now = self._clock()
        expires_at = now + self._ttl_seconds
        digest = self._compute_digest(
            prop_id=prop_id,
            kind=kind,
            instruction_sha256=instruction_sha256,
            payload=payload,
        )
        view = ProposalView(
            id=prop_id,
            kind=kind,
            project_id=self._project_id,
            session_id=self._session_id,
            run_id=self._run_id,
            digest=digest,
            payload=payload,
            state="pending",
            expires_at=expires_at,
            trusted_local_execution=False,
        )
        rec = _ProposalRecord(
            view=view,
            bound_root_identity=self._root_identity,
            bound_instruction_sha256=instruction_sha256,
            bound_instruction_files=(
                dict(bound_instruction_files) if bound_instruction_files is not None else None
            ),
            bound_instruction_dirs=frozenset(bound_instruction_dirs),
            scope_spec=scope_spec,
            normalized_exact_paths=normalized_exact,
            normalized_prefixes=normalized_prefixes,
            normalized_protected=normalized_protected,
            plan_spec=plan_spec,
            protected_shas=dict(protected_shas or {}),
            command_spec=command_spec,
        )
        self._proposals[prop_id] = rec
        self._order.append(prop_id)
        self._emit("proposal_requested", rec.view)
        return rec

    def create_scope_proposal(
        self,
        *,
        scope: ScopeProposalPayload,
        current_instruction_sha256: str | None = None,
        current_instruction_files: dict[str, str] | None = None,
        checked_instruction_dirs: Iterable[str] = (),
    ) -> ProposalView:
        validated = ScopeProposalPayload.model_validate(scope)
        exact_norm = [normalize_relative_file(p) for p in validated.exact_paths]
        for ep in exact_norm:
            if _is_write_protected_rel(ep):
                raise ProjectError(
                    "PROTECTED_PATH_FORBIDDEN",
                    f"protected path '{ep}' cannot be in write scope",
                    status_code=403,
                )
        prefixes_norm = [_normalize_scope_prefix(p) for p in validated.write_prefixes]
        protected_norm = [normalize_relative_file(p) for p in validated.protected_paths]
        clean_scope = ScopeProposalPayload(
            exact_paths=exact_norm,
            write_prefixes=prefixes_norm,
            operations=list(dict.fromkeys(validated.operations)),
            protected_paths=protected_norm,
            verification_plan_summary=validated.verification_plan_summary,
        )
        inst_sha = (
            current_instruction_sha256
            if current_instruction_sha256 is not None
            else self._instruction_sha256
        )
        prop_id = str(uuid.uuid4())
        payload = clean_scope.model_dump(mode="json")
        payload["root_identity"] = self._root_identity
        payload["instruction_sha256"] = inst_sha
        rec = self._new_record(
            prop_id=prop_id,
            kind="scope",
            payload=payload,
            instruction_sha256=inst_sha,
            bound_instruction_files=current_instruction_files,
            bound_instruction_dirs=checked_instruction_dirs,
            scope_spec=clean_scope,
            normalized_exact=frozenset(os.path.normcase(p) for p in exact_norm),
            normalized_prefixes=tuple(os.path.normcase(p) for p in prefixes_norm),
            normalized_protected=frozenset(os.path.normcase(p) for p in protected_norm),
        )
        return rec.view.model_copy(deep=True)

    def create_verification_proposal(
        self,
        *,
        plan: VerificationPlan,
        protected_shas: dict[str, str],
        current_instruction_sha256: str | None = None,
        current_instruction_files: dict[str, str] | None = None,
        checked_instruction_dirs: Iterable[str] = (),
    ) -> ProposalView:
        validated = VerificationPlan.model_validate(plan)
        prop_id = str(uuid.uuid4())
        plan_id = validated.plan_id or prop_id
        normalized_checks = []
        for chk in validated.checks:
            _validate_host_executable(chk.executable)
            validate_safe_relative_cwd(self._root, chk.cwd)
            norm_cwd = normalize_relative_dir(chk.cwd)
            norm_prot = sorted({normalize_relative_file(p) for p in chk.protected_paths})
            normalized_checks.append(
                chk.model_copy(update={"cwd": norm_cwd, "protected_paths": norm_prot})
            )
        clean_plan = VerificationPlan(
            plan_id=plan_id,
            summary=validated.summary,
            checks=normalized_checks,
        )
        norm_shas = {normalize_relative_file(k): v for k, v in sorted(protected_shas.items())}
        inst_sha = (
            current_instruction_sha256
            if current_instruction_sha256 is not None
            else self._instruction_sha256
        )
        payload = {
            "plan": clean_plan.model_dump(mode="json"),
            "protected_shas": norm_shas,
            "root_identity": self._root_identity,
            "instruction_sha256": inst_sha,
        }
        prot_set = frozenset(os.path.normcase(p) for p in norm_shas)
        rec = self._new_record(
            prop_id=prop_id,
            kind="verification",
            payload=payload,
            instruction_sha256=inst_sha,
            bound_instruction_files=current_instruction_files,
            bound_instruction_dirs=checked_instruction_dirs,
            normalized_protected=prot_set,
            plan_spec=clean_plan,
            protected_shas=norm_shas,
        )
        return rec.view.model_copy(deep=True)

    def create_command_proposal(
        self,
        *,
        command: CommandProposal | dict[str, Any],
        plan_id: str | None = None,
        plan_digest: str | None = None,
        current_instruction_sha256: str | None = None,
        current_instruction_files: dict[str, str] | None = None,
        checked_instruction_dirs: Iterable[str] = (),
    ) -> ProposalView:
        if isinstance(command, dict):
            if "env" in command or "env_keys" in command:
                raise ProjectError(
                    "CREDENTIAL_ENV_FORBIDDEN",
                    "command env/env_keys input is forbidden; host uses minimal env",
                    status_code=400,
                )
            parsed = CommandProposal.model_validate(command)
        else:
            parsed = CommandProposal.model_validate(command)

        real_exe, exe_sha = _validate_host_executable(parsed.executable)
        if parsed.executable_sha256 is not None and parsed.executable_sha256 != exe_sha:
            raise ProjectError(
                "EXECUTABLE_SHA_MISMATCH",
                "supplied executable_sha256 does not match disk executable",
                status_code=409,
            )
        validate_safe_relative_cwd(self._root, parsed.cwd)
        norm_cwd = normalize_relative_dir(parsed.cwd)
        self._cmd_seq += 1
        cmd_id = parsed.command_id or f"cmd-{self._cmd_seq:04d}"
        bound_cmd = CommandProposal(
            command_id=cmd_id,
            executable=str(real_exe),
            argv=list(parsed.argv),
            cwd=norm_cwd,
            timeout_ms=parsed.timeout_ms,
            purpose=parsed.purpose,
            explanation=parsed.explanation,
            executable_sha256=exe_sha,
            check_id=parsed.check_id,
        )
        inst_sha = (
            current_instruction_sha256
            if current_instruction_sha256 is not None
            else self._instruction_sha256
        )
        prop_id = str(uuid.uuid4())
        payload: dict[str, Any] = {
            "command": bound_cmd.model_dump(mode="json"),
            "root_identity": self._root_identity,
            "instruction_sha256": inst_sha,
            "warning": "受信任本机执行：进程可访问本机文件与网络",
        }
        if plan_id is not None:
            payload["plan_id"] = plan_id
        if plan_digest is not None:
            payload["plan_digest"] = plan_digest
        rec = self._new_record(
            prop_id=prop_id,
            kind="command",
            payload=payload,
            instruction_sha256=inst_sha,
            bound_instruction_files=current_instruction_files,
            bound_instruction_dirs=checked_instruction_dirs,
            command_spec=bound_cmd,
        )
        return rec.view.model_copy(deep=True)

    def list_proposals(self) -> list[ProposalView]:
        self._expire_all()
        return [self._proposals[pid].view.model_copy(deep=True) for pid in self._order]

    def pending_proposals(self) -> list[ProposalView]:
        self._expire_all()
        return [
            self._proposals[pid].view.model_copy(deep=True)
            for pid in self._order
            if self._proposals[pid].view.state == "pending"
        ]

    def get_proposal(self, proposal_id: str) -> ProposalView:
        rec = self._proposals.get(proposal_id)
        if rec is None:
            raise ProjectError("NOT_FOUND", "proposal not found", status_code=404)
        self._expire_if_needed(rec)
        return rec.view.model_copy(deep=True)

    def get_command_cwd(self, proposal_id: str) -> str | None:
        rec = self._proposals.get(proposal_id)
        if rec is None or rec.command_spec is None:
            return None
        return rec.command_spec.cwd

    def decide(self, *, proposal_id: str, request: ProposalDecision) -> ProposalView:
        rec = self._proposals.get(proposal_id)
        if rec is None:
            raise ProjectError("NOT_FOUND", "proposal not found", status_code=404)
        validated = ProposalDecision.model_validate(request)
        if rec.view.state == "revoked":
            raise ProjectError("PROPOSAL_CANCELLED", "proposal has been revoked", status_code=409)
        self._expire_if_needed(rec)
        if rec.view.state == "expired":
            raise ProjectError("PROPOSAL_EXPIRED", "proposal has expired", status_code=409)
        if validated.digest != rec.view.digest:
            raise ProjectError(
                "PROPOSAL_DIGEST_MISMATCH",
                "proposal digest mismatch",
                status_code=409,
            )

        if rec.view.state == "approved":
            if validated.decision == "reject":
                raise ProjectError(
                    "PROPOSAL_CONFLICT",
                    "cannot reject an already-approved proposal",
                    status_code=409,
                )
            if rec.view.kind == "command" and not validated.trusted_local_execution:
                raise ProjectError(
                    "TRUSTED_LOCAL_EXECUTION_REQUIRED",
                    "command approval requires trusted_local_execution=true",
                    status_code=403,
                )
            return rec.view.model_copy(deep=True)

        if rec.view.state == "rejected":
            if validated.decision == "approve":
                raise ProjectError(
                    "PROPOSAL_CONFLICT",
                    "cannot approve an already-rejected proposal",
                    status_code=409,
                )
            return rec.view.model_copy(deep=True)

        if validated.decision == "approve":
            if rec.view.kind == "command" and not validated.trusted_local_execution:
                raise ProjectError(
                    "TRUSTED_LOCAL_EXECUTION_REQUIRED",
                    "command execution approval requires trusted_local_execution=true",
                    status_code=403,
                )
            # CRITICAL: Do NOT extend or refresh expires_at on approval.
            rec.view = rec.view.model_copy(
                update={
                    "state": "approved",
                    "trusted_local_execution": bool(validated.trusted_local_execution),
                }
            )
            if rec.view.kind == "scope":
                self._active_scope_id = rec.view.id
            elif rec.view.kind == "verification":
                self._active_plan_id = rec.view.id
                self._plan_revision += 1
        else:
            rec.view = rec.view.model_copy(
                update={"state": "rejected", "trusted_local_execution": False}
            )

        rec.event.set()
        self._emit("proposal_decided", rec.view)
        return rec.view.model_copy(deep=True)

    async def wait_for_decision(self, proposal_id: str) -> ProposalView:
        rec = self._proposals.get(proposal_id)
        if rec is None:
            raise ProjectError("NOT_FOUND", "proposal not found", status_code=404)
        while rec.view.state == "pending":
            self._expire_if_needed(rec)
            if rec.view.state != "pending":
                break
            remaining = max(0.01, rec.view.expires_at - self._clock())
            try:
                await asyncio.wait_for(rec.event.wait(), timeout=min(remaining, 0.25))
            except asyncio.TimeoutError:
                pass
        return rec.view.model_copy(deep=True)

    def cancel_pending(self) -> None:
        """Revoke all pending AND unconsumed approved proposals and wake waiters."""
        self._revoked = True
        for pid in self._order:
            rec = self._proposals[pid]
            if rec.view.state in ("pending", "approved") and not rec.consumed:
                rec.view = rec.view.model_copy(update={"state": "revoked"})
                rec.event.set()
                self._emit("proposal_decided", rec.view)
            elif not rec.event.is_set():
                rec.event.set()

    def _validate_active_record(
        self,
        rec: _ProposalRecord,
        *,
        current_run_id: str,
        current_root_identity: str,
        current_instruction_sha256: str,
        current_instruction_files: dict[str, str] | None = None,
        extra_instruction_dirs: Iterable[str] = (),
    ) -> None:
        if self._revoked or rec.view.state == "revoked":
            raise ProjectError("PROPOSAL_CANCELLED", "proposal has been revoked", status_code=409)
        self._expire_if_needed(rec)
        if rec.view.state == "expired":
            raise ProjectError("PROPOSAL_EXPIRED", "proposal has expired", status_code=409)
        if rec.view.state != "approved":
            raise ProjectError("GRANT_REQUIRED", "proposal is not approved", status_code=403)
        if current_run_id != self._run_id:
            raise ProjectError("PROPOSAL_CONFLICT", "cross-run proposal use is forbidden", status_code=409)
        if current_root_identity != rec.bound_root_identity:
            rec.view = rec.view.model_copy(update={"state": "revoked"})
            raise ProjectError("PROPOSAL_CONFLICT", "root identity changed", status_code=409)
        if rec.bound_instruction_files is not None and current_instruction_files is not None:
            relevant_dirs = set(rec.bound_instruction_dirs) | set(extra_instruction_dirs) | {"."}
            observed = {
                path: sha
                for path, sha in current_instruction_files.items()
                if _instruction_file_dir(path) in relevant_dirs
            }
            inst_mismatch = observed != rec.bound_instruction_files
        else:
            inst_mismatch = current_instruction_sha256 != rec.bound_instruction_sha256
        if inst_mismatch:
            rec.view = rec.view.model_copy(update={"state": "revoked"})
            self._emit("proposal_decided", rec.view)
            raise ProjectError(
                "INSTRUCTIONS_CHANGED",
                "AGENTS.md instructions changed since proposal approval; re-confirm required",
                status_code=409,
            )

    def active_instruction_dirs(self) -> tuple[str, ...]:
        """Return union of bound instruction directories across active scope & plan."""
        dirs: set[str] = {"."}
        if self._active_scope_id and self._active_scope_id in self._proposals:
            dirs.update(self._proposals[self._active_scope_id].bound_instruction_dirs)
        if self._active_plan_id and self._active_plan_id in self._proposals:
            dirs.update(self._proposals[self._active_plan_id].bound_instruction_dirs)
        return tuple(sorted(dirs))

    def active_scope(self) -> ProposalView | None:
        if self._active_scope_id is None:
            return None
        rec = self._proposals.get(self._active_scope_id)
        if rec is None:
            return None
        self._expire_if_needed(rec)
        return rec.view.model_copy(deep=True)

    def active_plan(self) -> tuple[ProposalView, VerificationPlan, dict[str, str]] | None:
        if self._active_plan_id is None:
            return None
        rec = self._proposals.get(self._active_plan_id)
        if rec is None or rec.plan_spec is None:
            return None
        self._expire_if_needed(rec)
        return rec.view.model_copy(deep=True), rec.plan_spec, dict(rec.protected_shas)

    def protected_paths(self) -> frozenset[str]:
        combined: set[str] = set()
        if self._active_scope_id and self._active_scope_id in self._proposals:
            srec = self._proposals[self._active_scope_id]
            if srec.scope_spec is not None:
                combined.update(srec.scope_spec.protected_paths)
        if self._active_plan_id and self._active_plan_id in self._proposals:
            prec = self._proposals[self._active_plan_id]
            combined.update(prec.protected_shas.keys())
            if prec.plan_spec is not None:
                for chk in prec.plan_spec.checks:
                    combined.update(chk.protected_paths)
        return frozenset(combined)

    def verify_write_allowed(
        self,
        *,
        path: str,
        operation: str,
        current_run_id: str | None = None,
        current_root_identity: str | None = None,
        current_instruction_sha256: str | None = None,
        current_instruction_files: dict[str, str] | None = None,
        target_instruction_dirs: Iterable[str] = (),
    ) -> str:
        """Verify approved scope, approved plan, fresh instructions, and non-protected path."""
        run_id = current_run_id if current_run_id is not None else self._run_id
        root_id = current_root_identity if current_root_identity is not None else self._root_identity
        inst_sha = (
            current_instruction_sha256
            if current_instruction_sha256 is not None
            else self._instruction_sha256
        )

        if self._active_scope_id is None:
            raise ProjectError("SCOPE_REQUIRED", "propose_scope must be approved before writing", status_code=403)
        scope_rec = self._proposals[self._active_scope_id]
        self._validate_active_record(
            scope_rec,
            current_run_id=run_id,
            current_root_identity=root_id,
            current_instruction_sha256=inst_sha,
            current_instruction_files=current_instruction_files,
            extra_instruction_dirs=target_instruction_dirs,
        )

        if self._active_plan_id is None:
            raise ProjectError(
                "VERIFICATION_PLAN_REQUIRED",
                "propose_verification must be approved before writing",
                status_code=403,
            )
        plan_rec = self._proposals[self._active_plan_id]
        self._validate_active_record(
            plan_rec,
            current_run_id=run_id,
            current_root_identity=root_id,
            current_instruction_sha256=inst_sha,
            current_instruction_files=current_instruction_files,
        )

        norm = normalize_relative_file(path)
        norm_case = os.path.normcase(norm)
        extra_prot = frozenset(
            set(scope_rec.normalized_protected) | set(plan_rec.normalized_protected)
        )
        if _is_write_protected_rel(norm, extra_prot):
            raise ProjectError(
                "PROTECTED_PATH_FORBIDDEN",
                f"path '{norm}' is write-protected",
                status_code=403,
            )
        assert scope_rec.scope_spec is not None
        if operation not in scope_rec.scope_spec.operations:
            raise ProjectError(
                "SCOPE_VIOLATION",
                f"operation '{operation}' not allowed by approved scope",
                status_code=403,
            )
        exact_hit = norm_case in scope_rec.normalized_exact_paths
        prefix_hit = any(norm_case.startswith(pref) for pref in scope_rec.normalized_prefixes)
        if not exact_hit and not prefix_hit:
            raise ProjectError(
                "SCOPE_VIOLATION",
                f"path '{norm}' is outside approved write scope",
                status_code=403,
            )
        return scope_rec.view.id

    def verify_plan_approved(
        self,
        *,
        plan_id: str | None = None,
        plan_digest: str | None = None,
        current_run_id: str | None = None,
        current_root_identity: str | None = None,
        current_instruction_sha256: str | None = None,
        current_instruction_files: dict[str, str] | None = None,
    ) -> tuple[ProposalView, VerificationPlan, dict[str, str]]:
        run_id = current_run_id if current_run_id is not None else self._run_id
        root_id = current_root_identity if current_root_identity is not None else self._root_identity
        inst_sha = (
            current_instruction_sha256
            if current_instruction_sha256 is not None
            else self._instruction_sha256
        )
        if self._active_plan_id is None:
            raise ProjectError(
                "VERIFICATION_PLAN_REQUIRED",
                "propose_verification must be approved first",
                status_code=403,
            )
        plan_rec = self._proposals[self._active_plan_id]
        self._validate_active_record(
            plan_rec,
            current_run_id=run_id,
            current_root_identity=root_id,
            current_instruction_sha256=inst_sha,
            current_instruction_files=current_instruction_files,
        )
        assert plan_rec.plan_spec is not None
        if plan_id is not None and plan_id not in (plan_rec.view.id, plan_rec.plan_spec.plan_id):
            raise ProjectError("PLAN_CONFLICT", "plan_id does not match active approved plan", status_code=409)
        if plan_digest is not None and plan_digest != plan_rec.view.digest:
            raise ProjectError("PLAN_DIGEST_MISMATCH", "plan_digest does not match active plan", status_code=409)
        return plan_rec.view.model_copy(deep=True), plan_rec.plan_spec, dict(plan_rec.protected_shas)

    def consume_command_grant(
        self,
        *,
        proposal_id: str,
        proposal_digest: str | None = None,
        current_run_id: str | None = None,
        current_root_identity: str | None = None,
        current_instruction_sha256: str | None = None,
        current_instruction_files: dict[str, str] | None = None,
        executable: str | None = None,
        argv: list[str] | None = None,
        cwd: str | None = None,
        timeout_ms: int | None = None,
    ) -> CommandGrantReceipt:
        run_id = current_run_id if current_run_id is not None else self._run_id
        root_id = current_root_identity if current_root_identity is not None else self._root_identity
        inst_sha = (
            current_instruction_sha256
            if current_instruction_sha256 is not None
            else self._instruction_sha256
        )
        rec = self._proposals.get(proposal_id)
        if rec is None or rec.view.kind != "command" or rec.command_spec is None:
            raise ProjectError("GRANT_REQUIRED", "command proposal not found", status_code=403)
        if rec.consumed:
            raise ProjectError("GRANT_ALREADY_USED", "command grant already consumed", status_code=409)
        self._validate_active_record(
            rec,
            current_run_id=run_id,
            current_root_identity=root_id,
            current_instruction_sha256=inst_sha,
            current_instruction_files=current_instruction_files,
        )
        if not rec.view.trusted_local_execution:
            raise ProjectError(
                "TRUSTED_LOCAL_EXECUTION_REQUIRED",
                "command approval missing trusted_local_execution",
                status_code=403,
            )
        if proposal_digest is not None and proposal_digest != rec.view.digest:
            raise ProjectError("PROPOSAL_DIGEST_MISMATCH", "proposal_digest mismatch", status_code=409)

        cmd = rec.command_spec
        if executable is not None:
            req_real, _ = _validate_host_executable(executable)
            if os.path.normcase(str(req_real)) != os.path.normcase(cmd.executable):
                raise ProjectError("COMMAND_MISMATCH", "executable does not match approved proposal", status_code=409)
        if argv is not None and list(argv) != list(cmd.argv):
            raise ProjectError("COMMAND_MISMATCH", "argv does not match approved proposal", status_code=409)
        if cwd is not None and normalize_relative_dir(cwd) != cmd.cwd:
            raise ProjectError("COMMAND_MISMATCH", "cwd does not match approved proposal", status_code=409)
        if timeout_ms is not None and timeout_ms != cmd.timeout_ms:
            raise ProjectError("COMMAND_MISMATCH", "timeout_ms does not match approved proposal", status_code=409)

        # Require an active approved VerificationPlan even for diagnostic commands before spawn.
        self.verify_plan_approved(
            plan_id=rec.view.payload.get("plan_id"),
            plan_digest=rec.view.payload.get("plan_digest"),
            current_run_id=run_id,
            current_root_identity=root_id,
            current_instruction_sha256=inst_sha,
            current_instruction_files=current_instruction_files,
        )

        # Re-hash disk executable right before spawning to block TOCTOU binary swap.
        _, current_exe_sha = _validate_host_executable(cmd.executable)
        if current_exe_sha != cmd.executable_sha256:
            raise ProjectError(
                "EXECUTABLE_MODIFIED",
                "executable bytes on disk changed since command approval",
                status_code=409,
            )
        resolved_cwd = validate_safe_relative_cwd(self._root, cmd.cwd)

        rec.consumed = True
        return CommandGrantReceipt(
            proposal_id=rec.view.id,
            proposal_digest=rec.view.digest,
            command=cmd,
            resolved_cwd=resolved_cwd,
            plan_id=rec.view.payload.get("plan_id"),
            plan_digest=rec.view.payload.get("plan_digest"),
        )
