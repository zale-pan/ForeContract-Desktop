"""Run-bound immutable proposal broker for MCP connect and single-call approvals."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Literal

from forecontract_app.extension_models import (
    EXTENSION_PROPOSAL_TTL_SECONDS,
    ExtensionError,
    ExtensionProposalDecision,
    ExtensionProposalKind,
    ExtensionProposalView,
    HttpMcpServerConfig,
    McpServerConfig,
    StdioMcpServerConfig,
    compute_file_sha256,
)
from forecontract_app.session_models import sha256_canonical_json, utc_now


@dataclass
class _ExtensionProposalRecord:
    view: ExtensionProposalView
    expires_monotonic: float
    consumed: bool = False
    decision_event: asyncio.Event = field(default_factory=asyncio.Event)


class ExtensionPermissionBroker:
    """In-run single-use, digest-bound approval broker for MCP connections and tool calls."""

    def __init__(
        self,
        *,
        project_id: str,
        session_id: str,
        run_id: str,
        config_sha256: str,
        ttl_seconds: float = EXTENSION_PROPOSAL_TTL_SECONDS,
        monotonic_clock: Callable[[], float] | None = None,
        on_proposal_changed: Callable[[str, ExtensionProposalView], None] | None = None,
    ) -> None:
        self._project_id = project_id
        self._session_id = session_id
        self._run_id = run_id
        self._config_sha256 = config_sha256
        self._ttl_seconds = float(ttl_seconds)
        self._clock = monotonic_clock or time.monotonic
        self._on_changed = on_proposal_changed
        self._proposals: dict[str, _ExtensionProposalRecord] = {}
        self._ordered_ids: list[str] = []
        self._closed = False

    def _emit_changed(self, event_kind: str, view: ExtensionProposalView) -> None:
        if self._on_changed is not None:
            self._on_changed(event_kind, view.model_copy(deep=True))

    def _refresh_expiry(self, rec: _ExtensionProposalRecord) -> None:
        if rec.view.state == "pending" and self._clock() >= rec.expires_monotonic:
            rec.view = rec.view.model_copy(
                update={"state": "expired", "decided_at": utc_now()}
            )
            rec.decision_event.set()
            self._emit_changed("proposal_decided", rec.view)

    def _compute_digest(self, data: dict[str, Any]) -> str:
        return sha256_canonical_json(data)

    def list_proposals(self) -> list[ExtensionProposalView]:
        out: list[ExtensionProposalView] = []
        for pid in self._ordered_ids:
            rec = self._proposals[pid]
            self._refresh_expiry(rec)
            out.append(rec.view.model_copy(deep=True))
        return out

    def create_connect_proposal(
        self,
        *,
        action_id: str,
        server: McpServerConfig,
    ) -> ExtensionProposalView:
        if self._closed:
            raise ExtensionError("BROKER_CLOSED", "run extension broker is closed", status_code=409)
        now_dt = utc_now()
        exp_dt = now_dt + timedelta(seconds=self._ttl_seconds)
        exp_mono = self._clock() + self._ttl_seconds
        pid = f"extp-{uuid.uuid4().hex[:16]}"

        is_stdio = isinstance(server, StdioMcpServerConfig)
        exe = server.executable if is_stdio else None
        argv = list(server.argv) if is_stdio else None
        cwd = server.cwd if is_stdio else None
        exe_sha = server.executable_sha256 if is_stdio else None
        url = server.url if isinstance(server, HttpMcpServerConfig) else None
        summary = (
            f"connect stdio MCP '{server.id}' exe={exe} argv={argv} cwd={cwd} (trusted local execution)"
            if is_stdio
            else f"connect http MCP '{server.id}' url={url} (trusted local execution)"
        )

        digest = self._compute_digest(
            {
                "project_id": self._project_id,
                "session_id": self._session_id,
                "run_id": self._run_id,
                "action_id": action_id,
                "kind": "extension_connect",
                "server_id": server.id,
                "transport": server.transport,
                "config_sha256": self._config_sha256,
                "executable": exe,
                "argv": argv,
                "cwd": cwd,
                "executable_sha256": exe_sha,
                "url": url,
                "expires_at": exp_dt.isoformat(),
            }
        )
        view = ExtensionProposalView(
            id=pid,
            kind="extension_connect",
            project_id=self._project_id,
            session_id=self._session_id,
            run_id=self._run_id,
            action_id=action_id,
            server_id=server.id,
            tool_name=None,
            transport=server.transport,
            executable=exe,
            argv=argv,
            cwd=cwd,
            url=url,
            config_sha256=self._config_sha256,
            executable_sha256=exe_sha,
            schema_sha256=None,
            args_sha256=None,
            arguments=None,
            digest=digest,
            state="pending",
            trusted_local_execution=True,
            summary=summary,
            created_at=now_dt,
            expires_at=exp_dt,
        )
        rec = _ExtensionProposalRecord(view=view, expires_monotonic=exp_mono)
        self._proposals[pid] = rec
        self._ordered_ids.append(pid)
        self._emit_changed("proposal_requested", view)
        return view.model_copy(deep=True)

    def create_call_proposal(
        self,
        *,
        action_id: str,
        server: McpServerConfig,
        tool_name: str,
        schema_sha256: str,
        args_sha256: str,
        arguments: dict[str, Any],
    ) -> ExtensionProposalView:
        if self._closed:
            raise ExtensionError("BROKER_CLOSED", "run extension broker is closed", status_code=409)
        now_dt = utc_now()
        exp_dt = now_dt + timedelta(seconds=self._ttl_seconds)
        exp_mono = self._clock() + self._ttl_seconds
        pid = f"extp-{uuid.uuid4().hex[:16]}"

        is_stdio = isinstance(server, StdioMcpServerConfig)
        exe = server.executable if is_stdio else None
        argv = list(server.argv) if is_stdio else None
        cwd = server.cwd if is_stdio else None
        exe_sha = server.executable_sha256 if is_stdio else None
        url = server.url if isinstance(server, HttpMcpServerConfig) else None
        summary = (
            f"call MCP tool {server.id}.{tool_name} args_sha={args_sha256[:12]} schema_sha={schema_sha256[:12]}"
        )

        digest = self._compute_digest(
            {
                "project_id": self._project_id,
                "session_id": self._session_id,
                "run_id": self._run_id,
                "action_id": action_id,
                "kind": "extension_call",
                "server_id": server.id,
                "tool_name": tool_name,
                "transport": server.transport,
                "config_sha256": self._config_sha256,
                "executable": exe,
                "argv": argv,
                "cwd": cwd,
                "executable_sha256": exe_sha,
                "url": url,
                "schema_sha256": schema_sha256,
                "args_sha256": args_sha256,
                "expires_at": exp_dt.isoformat(),
            }
        )
        view = ExtensionProposalView(
            id=pid,
            kind="extension_call",
            project_id=self._project_id,
            session_id=self._session_id,
            run_id=self._run_id,
            action_id=action_id,
            server_id=server.id,
            tool_name=tool_name,
            transport=server.transport,
            executable=exe,
            argv=argv,
            cwd=cwd,
            url=url,
            config_sha256=self._config_sha256,
            executable_sha256=exe_sha,
            schema_sha256=schema_sha256,
            args_sha256=args_sha256,
            arguments=dict(arguments),
            digest=digest,
            state="pending",
            trusted_local_execution=True,
            summary=summary,
            created_at=now_dt,
            expires_at=exp_dt,
        )
        rec = _ExtensionProposalRecord(view=view, expires_monotonic=exp_mono)
        self._proposals[pid] = rec
        self._ordered_ids.append(pid)
        self._emit_changed("proposal_requested", view)
        return view.model_copy(deep=True)

    async def wait_for_decision(self, proposal_id: str) -> ExtensionProposalView:
        rec = self._proposals.get(proposal_id)
        if rec is None:
            raise ExtensionError("NOT_FOUND", "extension proposal not found", status_code=404)
        while True:
            self._refresh_expiry(rec)
            if rec.view.state != "pending":
                return rec.view.model_copy(deep=True)
            remaining = max(0.01, rec.expires_monotonic - self._clock())
            try:
                await asyncio.wait_for(rec.decision_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                self._refresh_expiry(rec)
                return rec.view.model_copy(deep=True)

    def decide(
        self,
        *,
        proposal_id: str,
        request: ExtensionProposalDecision,
    ) -> ExtensionProposalView:
        validated = ExtensionProposalDecision.model_validate(request)
        rec = self._proposals.get(proposal_id)
        if rec is None:
            raise ExtensionError("NOT_FOUND", "extension proposal not found", status_code=404)
        if (
            rec.view.project_id != self._project_id
            or rec.view.session_id != self._session_id
            or rec.view.run_id != self._run_id
        ):
            raise ExtensionError("NOT_FOUND", "extension proposal belongs to another run", status_code=404)

        self._refresh_expiry(rec)
        if validated.digest != rec.view.digest:
            raise ExtensionError("PROPOSAL_DIGEST_MISMATCH", "proposal digest mismatch", status_code=409)

        cur_state = rec.view.state
        target_state: Literal["approved", "rejected"] = (
            "approved" if validated.decision == "approve" else "rejected"
        )
        if cur_state == target_state:
            return rec.view.model_copy(deep=True)
        if cur_state in ("expired", "revoked"):
            raise ExtensionError(
                f"PROPOSAL_{cur_state.upper()}",
                f"extension proposal is already {cur_state}",
                status_code=409,
            )
        if cur_state in ("approved", "rejected"):
            raise ExtensionError(
                "PROPOSAL_CONFLICT",
                f"extension proposal already decided as {cur_state}",
                status_code=409,
            )
        if self._closed:
            raise ExtensionError("PROPOSAL_REVOKED", "run already closed", status_code=409)

        rec.view = rec.view.model_copy(
            update={"state": target_state, "decided_at": utc_now()}
        )
        rec.decision_event.set()
        self._emit_changed("proposal_decided", rec.view)
        return rec.view.model_copy(deep=True)

    def _validate_consumable_record(
        self,
        *,
        proposal_id: str,
        expected_kind: ExtensionProposalKind,
        expected_action_id: str,
        server: McpServerConfig,
        current_config_sha256: str,
        expected_digest: str | None,
    ) -> _ExtensionProposalRecord:
        rec = self._proposals.get(proposal_id)
        if rec is None or rec.view.kind != expected_kind:
            raise ExtensionError("GRANT_REQUIRED", "extension proposal not found", status_code=403)
        if (
            rec.view.project_id != self._project_id
            or rec.view.session_id != self._session_id
            or rec.view.run_id != self._run_id
        ):
            raise ExtensionError("CROSS_RUN_GRANT", "extension proposal belongs to another run", status_code=403)
        if rec.view.action_id != expected_action_id:
            raise ExtensionError(
                "ACTION_ID_MISMATCH", "extension proposal belongs to another action", status_code=409
            )
        if rec.consumed:
            raise ExtensionError("GRANT_ALREADY_USED", "extension grant already consumed", status_code=409)
        self._refresh_expiry(rec)
        if rec.view.state != "approved":
            raise ExtensionError(
                "GRANT_NOT_APPROVED",
                f"extension proposal state is '{rec.view.state}'",
                status_code=403,
            )
        if expected_digest is not None and expected_digest != rec.view.digest:
            raise ExtensionError("PROPOSAL_DIGEST_MISMATCH", "proposal digest mismatch", status_code=409)
        if rec.view.server_id != server.id or rec.view.transport != server.transport:
            raise ExtensionError("SERVER_MISMATCH", "server specification mismatch", status_code=409)
        if rec.view.config_sha256 != current_config_sha256:
            raise ExtensionError("CONFIG_MODIFIED", "extension config digest changed since approval", status_code=409)

        if isinstance(server, StdioMcpServerConfig):
            if (
                rec.view.executable != server.executable
                or rec.view.argv != list(server.argv)
                or rec.view.cwd != server.cwd
                or rec.view.executable_sha256 != server.executable_sha256
            ):
                raise ExtensionError("SERVER_MISMATCH", "stdio server arguments/path changed", status_code=409)
            live_exe_sha = compute_file_sha256(Path(server.executable))
            if live_exe_sha != rec.view.executable_sha256:
                raise ExtensionError(
                    "EXECUTABLE_MODIFIED",
                    "stdio server executable changed on disk since approval",
                    status_code=409,
                )
        elif isinstance(server, HttpMcpServerConfig):
            if rec.view.url != server.url:
                raise ExtensionError("SERVER_MISMATCH", "HTTP server URL changed since approval", status_code=409)

        return rec

    def consume_connect_grant(
        self,
        *,
        proposal_id: str,
        action_id: str,
        server: McpServerConfig,
        current_config_sha256: str,
        expected_digest: str | None = None,
    ) -> ExtensionProposalView:
        rec = self._validate_consumable_record(
            proposal_id=proposal_id,
            expected_kind="extension_connect",
            expected_action_id=action_id,
            server=server,
            current_config_sha256=current_config_sha256,
            expected_digest=expected_digest,
        )
        rec.consumed = True
        return rec.view.model_copy(deep=True)

    def consume_call_grant(
        self,
        *,
        proposal_id: str,
        action_id: str,
        server: McpServerConfig,
        current_config_sha256: str,
        tool_name: str,
        schema_sha256: str,
        args_sha256: str,
        expected_digest: str | None = None,
    ) -> ExtensionProposalView:
        rec = self._validate_consumable_record(
            proposal_id=proposal_id,
            expected_kind="extension_call",
            expected_action_id=action_id,
            server=server,
            current_config_sha256=current_config_sha256,
            expected_digest=expected_digest,
        )
        if rec.view.tool_name != tool_name:
            raise ExtensionError("TOOL_MISMATCH", "tool_name does not match approval", status_code=409)
        if rec.view.schema_sha256 != schema_sha256:
            raise ExtensionError("SCHEMA_MODIFIED", "tool schema changed since approval", status_code=409)
        if rec.view.args_sha256 != args_sha256:
            raise ExtensionError("ARGS_MODIFIED", "tool arguments changed since approval", status_code=409)
        rec.consumed = True
        return rec.view.model_copy(deep=True)

    def revoke_server_calls(self, server_id: str) -> None:
        """Invalidate pending or unconsumed call proposals for ``server_id`` on list_changed."""
        for pid in self._ordered_ids:
            rec = self._proposals[pid]
            if (
                rec.view.server_id == server_id
                and rec.view.kind == "extension_call"
                and not rec.consumed
                and rec.view.state in ("pending", "approved")
            ):
                rec.view = rec.view.model_copy(
                    update={"state": "revoked", "decided_at": utc_now()}
                )
                rec.decision_event.set()
                self._emit_changed("proposal_decided", rec.view)

    def cancel_pending(self) -> None:
        """Revoke all pending and unconsumed approved proposals on cancellation or shutdown."""
        self._closed = True
        now_dt = utc_now()
        for pid in self._ordered_ids:
            rec = self._proposals[pid]
            if not rec.consumed and rec.view.state in ("pending", "approved"):
                rec.view = rec.view.model_copy(
                    update={"state": "revoked", "decided_at": now_dt}
                )
                rec.decision_event.set()
                self._emit_changed("proposal_decided", rec.view)
