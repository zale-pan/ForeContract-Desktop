"""In-run orchestrator for Skill reading, MCP lifecycle, approval binding, and quota tracking."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from forecontract.models.actions import ActionSpec
from forecontract.protocols.adapter import (
    InvalidActionPayloadError,
    UnsupportedActionError,
)
from forecontract_coding.models import thaw_json_value

from forecontract_app.extension_models import (
    DEFAULT_MCP_CALL_TIMEOUT_MS,
    DEFAULT_SKILL_PAGE_BYTES,
    EXTENSION_PROPOSAL_TTL_SECONDS,
    MAX_MCP_CALL_DATA_BYTES_PER_RUN,
    MAX_MCP_CALL_TIMEOUT_MS,
    MAX_MCP_CALLS_PER_RUN,
    MAX_MCP_SERVERS,
    MIN_MCP_CALL_TIMEOUT_MS,
    ExtensionCleanup,
    ExtensionConfig,
    ExtensionError,
    ExtensionProposalView,
    McpServerView,
    McpToolInfo,
    McpToolResult,
    RunExtensionsView,
    SelectedSkillContext,
    SkillInfo,
    _assert_config_identity,
    sanitize_server_summary,
)
from forecontract_app.extension_permissions import ExtensionPermissionBroker
from forecontract_app.mcp_client import McpClient, validate_tool_arguments
from forecontract_app.session_models import canonical_json_dumps, truncate_utf8_safe
from forecontract_app.skills import SkillCatalog, load_selected_skills

EXTENSION_ACTION_NAMES: tuple[str, ...] = (
    "list_skills",
    "read_skill",
    "list_mcp_servers",
    "connect_mcp",
    "list_mcp_tools",
    "call_mcp",
)

_EXTENSION_ACTION_GUIDANCE: dict[str, dict[str, Any]] = {
    "list_skills": {
        "description": "List available local Skills under .forecontract/skills (single level, metadata only).",
        "required": [],
        "optional": [],
        "properties": {},
        "additionalProperties": False,
    },
    "read_skill": {
        "description": "Read a bounded UTF-8 page (<=8192 bytes) of a Skill file as untrusted reference content.",
        "required": ["skill_id"],
        "optional": ["relative_path", "byte_offset", "max_bytes", "expected_sha256"],
        "properties": {
            "skill_id": {"type": "string"},
            "relative_path": {"type": "string"},
            "byte_offset": {"type": "integer", "minimum": 0},
            "max_bytes": {"type": "integer", "minimum": 1, "maximum": 8192},
            "expected_sha256": {"type": ["string", "null"]},
        },
        "additionalProperties": False,
    },
    "list_mcp_servers": {
        "description": "List sanitized configured MCP servers and connection status (never spawns or connects).",
        "required": [],
        "optional": [],
        "properties": {},
        "additionalProperties": False,
    },
    "connect_mcp": {
        "description": "Request user approval and connect to a configured MCP 2025-11-25 server.",
        "required": ["server_id"],
        "optional": [],
        "properties": {"server_id": {"type": "string"}},
        "additionalProperties": False,
    },
    "list_mcp_tools": {
        "description": "Enumerate tools from an already-connected MCP server.",
        "required": ["server_id"],
        "optional": [],
        "properties": {"server_id": {"type": "string"}},
        "additionalProperties": False,
    },
    "call_mcp": {
        "description": "Request per-call user approval and invoke one validated MCP tool.",
        "required": ["server_id", "tool_name", "arguments"],
        "optional": ["timeout_ms"],
        "properties": {
            "server_id": {"type": "string"},
            "tool_name": {"type": "string"},
            "arguments": {"type": "object"},
            "timeout_ms": {
                "type": "integer",
                "minimum": MIN_MCP_CALL_TIMEOUT_MS,
                "maximum": MAX_MCP_CALL_TIMEOUT_MS,
            },
        },
        "additionalProperties": False,
    },
}


def _exact_keys(payload: dict[str, Any], *, required: set[str], optional: set[str]) -> None:
    allowed = required | optional
    extra = set(payload.keys()) - allowed
    if extra:
        raise InvalidActionPayloadError(f"unexpected payload keys: {sorted(extra)}")
    missing = required - set(payload.keys())
    if missing:
        raise InvalidActionPayloadError(f"missing required payload keys: {sorted(missing)}")


class ExtensionRuntime:
    """Per-run manager coordinating local Skills, MCP connections, approvals, and cleanup."""

    def __init__(
        self,
        *,
        project_id: str,
        session_id: str,
        run_id: str,
        project_root: Path,
        config: ExtensionConfig,
        selected_skill_ids: tuple[str, ...],
        scratch_root: Path,
        emit: Callable[[str, dict[str, Any]], None],
        ttl_seconds: float = EXTENSION_PROPOSAL_TTL_SECONDS,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        self._project_id = project_id
        self._session_id = session_id
        self._run_id = run_id
        self._project_root = project_root
        self._config = ExtensionConfig.model_validate(config)
        self._selected_skill_ids = tuple(selected_skill_ids)
        self._scratch_root = scratch_root
        self._emit = emit
        self._skill_catalog = SkillCatalog(project_root)
        self._selected_skills: list[SelectedSkillContext] = load_selected_skills(
            project_root=project_root,
            skill_ids=self._selected_skill_ids,
        )

        def _on_proposal_changed(kind: str, prop: ExtensionProposalView) -> None:
            data = prop.model_dump(mode="json")
            data["extension"] = True
            if isinstance(data.get("arguments"), dict):
                raw_args = canonical_json_dumps(data["arguments"])
                if len(raw_args.encode("utf-8")) > 2048:
                    preview, _ = truncate_utf8_safe(raw_args, 1024)
                    data["arguments"] = {"_truncated_preview": preview}
            self._emit(kind, data)

        self._broker = ExtensionPermissionBroker(
            project_id=project_id,
            session_id=session_id,
            run_id=run_id,
            config_sha256=self._config.config_sha256,
            ttl_seconds=ttl_seconds,
            monotonic_clock=monotonic_clock,
            on_proposal_changed=_on_proposal_changed,
        )
        self._clients: dict[str, McpClient] = {}
        self._calls: list[McpToolResult] = []
        self._total_call_bytes = 0
        self._call_seq = 0
        self._executed_actions: dict[str, dict[str, Any]] = {}
        self._inflight_actions: set[str] = set()
        self._cleanup_result: ExtensionCleanup | None = None
        self._closed = False
        self._mutation_seq = 0

    @property
    def selected_skills(self) -> list[SelectedSkillContext]:
        return [s.model_copy(deep=True) for s in self._selected_skills]

    @property
    def mutation_seq(self) -> int:
        """Monotonic counter incremented whenever an MCP call crosses the transport wire."""
        return self._mutation_seq

    @property
    def has_unknown_side_effects(self) -> bool:
        return any(
            c.status == "unknown" or c.status in ("timeout", "cancelled") and c.effect == "unknown"
            for c in self._calls
        )

    @property
    def cleanup_result(self) -> ExtensionCleanup | None:
        return self._cleanup_result

    @staticmethod
    def action_guidance() -> dict[str, dict[str, Any]]:
        """Return planner-facing action schemas for the 6 extension actions."""
        return {k: dict(v) for k, v in _EXTENSION_ACTION_GUIDANCE.items()}

    def validate_action(self, action: ActionSpec) -> None:
        """Pure synchronous schema validation without performing disk/process/network I/O."""
        if not isinstance(action, ActionSpec):
            raise InvalidActionPayloadError("ActionSpec is required")
        at = action.action_type
        if at not in EXTENSION_ACTION_NAMES:
            raise UnsupportedActionError(f"unsupported extension action_type: {at}")
        if not isinstance(action.payload, dict):
            # Support immutable mappings thawed identically to ProjectCodingAdapter
            try:
                raw_payload = thaw_json_value(action.payload)
            except Exception as exc:
                raise InvalidActionPayloadError("action payload must be a mapping") from exc
        else:
            raw_payload = thaw_json_value(action.payload)
        if not isinstance(raw_payload, dict):
            raise InvalidActionPayloadError("action payload must be a JSON object")

        if at == "list_skills":
            _exact_keys(raw_payload, required=set(), optional=set())
            return

        if at == "read_skill":
            _exact_keys(
                raw_payload,
                required={"skill_id"},
                optional={"relative_path", "byte_offset", "max_bytes", "expected_sha256"},
            )
            sid = raw_payload["skill_id"]
            if not isinstance(sid, str) or not sid.strip() or "\x00" in sid:
                raise InvalidActionPayloadError("skill_id must be non-blank string")
            if "relative_path" in raw_payload:
                rp = raw_payload["relative_path"]
                if not isinstance(rp, str) or not rp.strip() or "\x00" in rp:
                    raise InvalidActionPayloadError("relative_path must be non-blank string")
            if "byte_offset" in raw_payload:
                bo = raw_payload["byte_offset"]
                if isinstance(bo, bool) or not isinstance(bo, int) or bo < 0:
                    raise InvalidActionPayloadError("byte_offset must be int >= 0")
            if "max_bytes" in raw_payload:
                mb = raw_payload["max_bytes"]
                if isinstance(mb, bool) or not isinstance(mb, int) or not (1 <= mb <= DEFAULT_SKILL_PAGE_BYTES):
                    raise InvalidActionPayloadError(f"max_bytes must be int in 1..{DEFAULT_SKILL_PAGE_BYTES}")
            if "expected_sha256" in raw_payload and raw_payload["expected_sha256"] is not None:
                sha = raw_payload["expected_sha256"]
                if not isinstance(sha, str) or len(sha) != 64 or any(c not in "0123456789abcdef" for c in sha):
                    raise InvalidActionPayloadError("expected_sha256 must be 64 lowercase hex")
            return

        if at == "list_mcp_servers":
            _exact_keys(raw_payload, required=set(), optional=set())
            return

        if at in ("connect_mcp", "list_mcp_tools"):
            _exact_keys(raw_payload, required={"server_id"}, optional=set())
            srv_id = raw_payload["server_id"]
            if not isinstance(srv_id, str) or not srv_id.strip() or "\x00" in srv_id:
                raise InvalidActionPayloadError("server_id must be non-blank string")
            return

        if at == "call_mcp":
            _exact_keys(
                raw_payload,
                required={"server_id", "tool_name", "arguments"},
                optional={"timeout_ms"},
            )
            srv_id = raw_payload["server_id"]
            tname = raw_payload["tool_name"]
            args = raw_payload["arguments"]
            if not isinstance(srv_id, str) or not srv_id.strip() or "\x00" in srv_id:
                raise InvalidActionPayloadError("server_id must be non-blank string")
            if not isinstance(tname, str) or not tname.strip() or "\x00" in tname:
                raise InvalidActionPayloadError("tool_name must be non-blank string")
            if not isinstance(args, dict):
                raise InvalidActionPayloadError("arguments must be JSON object")
            if "timeout_ms" in raw_payload:
                tm = raw_payload["timeout_ms"]
                if (
                    isinstance(tm, bool)
                    or not isinstance(tm, int)
                    or not (MIN_MCP_CALL_TIMEOUT_MS <= tm <= MAX_MCP_CALL_TIMEOUT_MS)
                ):
                    raise InvalidActionPayloadError(
                        f"timeout_ms must be int in {MIN_MCP_CALL_TIMEOUT_MS}..{MAX_MCP_CALL_TIMEOUT_MS}"
                    )

    def list_proposals(self) -> list[ExtensionProposalView]:
        return self._broker.list_proposals()

    def cancel_pending(self) -> None:
        """Synchronously revoke pending/unconsumed extension proposals during run termination."""
        self._broker.cancel_pending()

    async def decide(
        self,
        *,
        proposal_id: str,
        digest: str,
        decision: Literal["approve", "reject"],
    ) -> ExtensionProposalView:
        return self._broker.decide(
            proposal_id=proposal_id,
            request={"digest": digest, "decision": decision},  # type: ignore[arg-type]
        )

    def _server_views(self) -> list[McpServerView]:
        views: list[McpServerView] = []
        for srv in self._config.servers:
            client = self._clients.get(srv.id)
            if client is not None:
                views.append(client.view())
            else:
                summary = sanitize_server_summary(srv, config_sha256=self._config.config_sha256)
                views.append(
                    McpServerView(
                        id=summary.id,
                        transport=summary.transport,
                        executable=summary.executable,
                        argv=summary.argv,
                        cwd=summary.cwd,
                        url=summary.url,
                        config_sha256=summary.config_sha256,
                        executable_sha256=summary.executable_sha256,
                        state="not_connected",
                    )
                )
        return views

    def build_run_extensions_view(self) -> RunExtensionsView:
        """Build bounded read-only state view for the HTTP extensions endpoint."""
        selected_infos = [
            SkillInfo(id=s.id, path=s.path, sha256=s.sha256, total_bytes=s.total_bytes)
            for s in self._selected_skills
        ]
        return RunExtensionsView(
            project_id=self._project_id,
            session_id=self._session_id,
            run_id=self._run_id,
            selected_skills=selected_infos,
            servers=self._server_views(),
            proposals=self.list_proposals(),
            calls=[c.model_copy(deep=True) for c in self._calls],
            total_call_bytes=self._total_call_bytes,
            total_calls=len(self._calls),
        )

    def _emit_call_result_event(self, res: McpToolResult) -> None:
        payload = res.model_dump(mode="json")
        payload["extension"] = True
        payload["action_type"] = "call_mcp"
        payload["tool_succeeded"] = res.status == "success" and not res.is_error
        payload["effect_applied"] = False if res.effect == "not_applied" else None
        if isinstance(payload.get("text"), str) and len(payload["text"].encode("utf-8")) > 2048:
            txt_cut, _ = truncate_utf8_safe(payload["text"], 2048)
            payload["text"] = txt_cut
            payload["truncated"] = True
        self._emit("tool_result", payload)

    async def execute(self, action: ActionSpec) -> dict[str, Any]:
        """Execute one validated Skill or MCP extension action and return structured result."""
        self.validate_action(action)
        action_id = str(action.metadata.get("action_id") or f"ext-{uuid.uuid4().hex[:12]}")
        at = action.action_type
        payload = dict(thaw_json_value(action.payload))

        if action_id in self._executed_actions:
            return dict(self._executed_actions[action_id])
        if action_id in self._inflight_actions:
            return {
                "action_id": action_id,
                "action_type": at,
                "tool_succeeded": False,
                "error_code": "DUPLICATE_INFLIGHT_ACTION",
                "error_message": f"action_id '{action_id}' is already executing",
                "effect_applied": False,
            }

        self._inflight_actions.add(action_id)
        try:
            result = await self._execute_inner(
                action_id=action_id, action_type=at, payload=payload
            )
            self._executed_actions[action_id] = dict(result)
            return result
        finally:
            self._inflight_actions.discard(action_id)

    async def _execute_inner(
        self, *, action_id: str, action_type: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if self._closed:
            return {
                "action_id": action_id,
                "action_type": action_type,
                "tool_succeeded": False,
                "error_code": "EXTENSION_RUNTIME_CLOSED",
                "error_message": "extension runtime is closed",
                "effect_applied": False,
            }

        try:
            if action_type == "list_skills":
                skills = self._skill_catalog.list_skills()
                return {
                    "action_id": action_id,
                    "action_type": "list_skills",
                    "tool_succeeded": True,
                    "project_id": self._project_id,
                    "run_id": self._run_id,
                    "skills": [s.model_dump(mode="json") for s in skills],
                    "count": len(skills),
                }

            if action_type == "read_skill":
                page = self._skill_catalog.read(
                    skill_id=payload["skill_id"],
                    relative_path=payload.get("relative_path", "SKILL.md"),
                    byte_offset=payload.get("byte_offset", 0),
                    max_bytes=payload.get("max_bytes", DEFAULT_SKILL_PAGE_BYTES),
                    expected_sha256=payload.get("expected_sha256"),
                )
                return {
                    **page.model_dump(mode="json"),
                    "action_id": action_id,
                    "action_type": "read_skill",
                    "tool_succeeded": True,
                    "project_id": self._project_id,
                    "run_id": self._run_id,
                    "source_trust": "untrusted_skill",
                }

            if action_type == "list_mcp_servers":
                srv_views = self._server_views()
                return {
                    "action_id": action_id,
                    "action_type": "list_mcp_servers",
                    "tool_succeeded": True,
                    "project_id": self._project_id,
                    "run_id": self._run_id,
                    "servers": [v.model_dump(mode="json") for v in srv_views],
                    "count": len(srv_views),
                }

            if action_type == "connect_mcp":
                server_id = payload["server_id"]
                srv = self._config.get_server(server_id)
                if srv is None:
                    raise ExtensionError("MCP_SERVER_NOT_FOUND", f"MCP server '{server_id}' not configured", status_code=404)
                existing = self._clients.get(server_id)
                if existing is not None and existing.state == "connected":
                    return {
                        "action_id": action_id,
                        "action_type": "connect_mcp",
                        "tool_succeeded": True,
                        "server": existing.view().model_dump(mode="json"),
                    }
                if len(self._clients) >= MAX_MCP_SERVERS:
                    raise ExtensionError("MCP_CONNECTION_LIMIT", f"cannot exceed {MAX_MCP_SERVERS} connections", status_code=400)

                _assert_config_identity(config=self._config)
                prop = self._broker.create_connect_proposal(action_id=action_id, server=srv)
                decided = await self._broker.wait_for_decision(prop.id)
                if decided.state != "approved":
                    return {
                        "action_id": action_id,
                        "action_type": "connect_mcp",
                        "tool_succeeded": False,
                        "error_code": "PROPOSAL_DENIED",
                        "error_message": f"connect proposal ended in state '{decided.state}'",
                        "proposal_id": decided.id,
                        "digest": decided.digest,
                        "state": decided.state,
                        "effect_applied": False,
                    }

                _assert_config_identity(config=self._config)
                self._broker.consume_connect_grant(
                    proposal_id=decided.id,
                    action_id=action_id,
                    server=srv,
                    current_config_sha256=self._config.config_sha256,
                    expected_digest=decided.digest,
                )
                client = McpClient(
                    srv,
                    scratch_root=self._scratch_root,
                    config_sha256=self._config.config_sha256,
                    on_tools_list_changed=self._broker.revoke_server_calls,
                )
                self._clients[server_id] = client
                srv_view = await client.connect()
                return {
                    "action_id": action_id,
                    "action_type": "connect_mcp",
                    "tool_succeeded": True,
                    "proposal_id": decided.id,
                    "server": srv_view.model_dump(mode="json"),
                }

            if action_type == "list_mcp_tools":
                server_id = payload["server_id"]
                client = self._clients.get(server_id)
                if client is None or client.state != "connected":
                    raise ExtensionError("MCP_NOT_CONNECTED", f"MCP server '{server_id}' is not connected", status_code=409)
                tools = await client.list_tools()
                return {
                    "action_id": action_id,
                    "action_type": "list_mcp_tools",
                    "tool_succeeded": True,
                    "server_id": server_id,
                    "tools": [
                        {
                            "server_id": t.server_id,
                            "name": t.name,
                            "title": t.title,
                            "description": t.description,
                            "schema_sha256": t.schema_sha256,
                            "supported": t.supported,
                            "unsupported_reason": t.unsupported_reason,
                            "annotations_untrusted": t.annotations,
                            "input_schema": t.input_schema,
                        }
                        for t in tools
                    ],
                    "count": len(tools),
                }

            if action_type == "call_mcp":
                server_id = payload["server_id"]
                tool_name = payload["tool_name"]
                arguments = dict(payload["arguments"])
                timeout_ms = payload.get("timeout_ms", DEFAULT_MCP_CALL_TIMEOUT_MS)

                if len(self._calls) >= MAX_MCP_CALLS_PER_RUN:
                    raise ExtensionError(
                        "MCP_CALL_QUOTA_EXCEEDED",
                        f"run exceeded max {MAX_MCP_CALLS_PER_RUN} MCP tool calls",
                        status_code=429,
                    )
                if self._total_call_bytes >= MAX_MCP_CALL_DATA_BYTES_PER_RUN:
                    raise ExtensionError(
                        "MCP_DATA_QUOTA_EXCEEDED",
                        f"run exceeded max {MAX_MCP_CALL_DATA_BYTES_PER_RUN} bytes of MCP call data",
                        status_code=429,
                    )

                srv = self._config.get_server(server_id)
                client = self._clients.get(server_id)
                if srv is None:
                    raise ExtensionError("MCP_SERVER_NOT_FOUND", f"MCP server '{server_id}' not configured", status_code=404)
                if client is None or client.state != "connected":
                    raise ExtensionError("MCP_NOT_CONNECTED", f"MCP server '{server_id}' is not connected", status_code=409)
                if not client.tools_listed:
                    raise ExtensionError(
                        "MCP_TOOLS_STALE",
                        "tools/list must be executed (or refreshed after list_changed) before call_mcp",
                        status_code=409,
                    )
                tool: McpToolInfo | None = client.get_cached_tool(tool_name)
                if tool is None:
                    raise ExtensionError("MCP_TOOL_NOT_FOUND", f"tool '{tool_name}' not found on '{server_id}'", status_code=404)

                args_sha = validate_tool_arguments(tool, arguments)
                _assert_config_identity(config=self._config)
                prop = self._broker.create_call_proposal(
                    action_id=action_id,
                    server=srv,
                    tool_name=tool_name,
                    schema_sha256=tool.schema_sha256,
                    args_sha256=args_sha,
                    arguments=arguments,
                )
                decided = await self._broker.wait_for_decision(prop.id)
                if decided.state != "approved":
                    return {
                        "action_id": action_id,
                        "action_type": "call_mcp",
                        "tool_succeeded": False,
                        "error_code": "PROPOSAL_DENIED",
                        "error_message": f"call_mcp proposal ended in state '{decided.state}'",
                        "proposal_id": decided.id,
                        "digest": decided.digest,
                        "state": decided.state,
                        "server_id": server_id,
                        "tool_name": tool_name,
                        "args_sha256": args_sha,
                        "schema_sha256": tool.schema_sha256,
                        "status": "error",
                        "is_error": True,
                        "effect": "not_applied",
                        "effect_applied": False,
                    }

                _assert_config_identity(config=self._config)
                self._broker.consume_call_grant(
                    proposal_id=decided.id,
                    action_id=action_id,
                    server=srv,
                    current_config_sha256=self._config.config_sha256,
                    tool_name=tool_name,
                    schema_sha256=tool.schema_sha256,
                    args_sha256=args_sha,
                    expected_digest=decided.digest,
                )

                self._call_seq += 1
                call_id = f"mcpc-{self._call_seq:04d}-{uuid.uuid4().hex[:8]}"
                # Mark potentially applied / unknown effect before crossing wire so any prior verification invalidates.
                self._mutation_seq += 1
                call_res = await client.call_tool(
                    call_id=call_id,
                    tool_name=tool_name,
                    arguments=arguments,
                    timeout_ms=timeout_ms,
                    action_id=action_id,
                )
                self._calls.append(call_res)
                args_bytes = len(canonical_json_dumps(arguments).encode("utf-8"))
                res_bytes = max(
                    len(call_res.model_dump_json().encode("utf-8")),
                    call_res.original_bytes,
                )
                self._total_call_bytes += args_bytes + res_bytes
                self._emit_call_result_event(call_res)

                ok = call_res.status == "success" and not call_res.is_error
                res_dict = call_res.model_dump(mode="json")
                res_dict["action_type"] = "call_mcp"
                res_dict["proposal_id"] = decided.id
                res_dict["tool_succeeded"] = ok
                res_dict["effect_applied"] = True if ok else None
                return res_dict

        except ExtensionError as exc:
            return {
                "action_id": action_id,
                "action_type": action_type,
                "tool_succeeded": False,
                "error_code": exc.code,
                "error_message": exc.message,
                "effect_applied": False,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "action_id": action_id,
                "action_type": action_type,
                "tool_succeeded": False,
                "error_code": "EXTENSION_ERROR",
                "error_message": str(exc)[:512],
                "effect_applied": None,
            }
        return {
            "action_id": action_id,
            "action_type": action_type,
            "tool_succeeded": False,
            "error_code": "UNSUPPORTED_ACTION",
            "error_message": f"unsupported action {action_type}",
            "effect_applied": False,
        }

    def observation_payload(self, *, compact: bool = False) -> dict[str, Any]:
        """Return bounded extension state preserving selected Skills verbatim even under compaction."""
        skills_payload = [
            {
                "id": s.id,
                "path": s.path,
                "sha256": s.sha256,
                "total_bytes": s.total_bytes,
                "source_trust": "untrusted_skill",
                "content": s.content,
            }
            for s in self._selected_skills
        ]
        proposals_summary = [
            {
                "id": p.id,
                "kind": p.kind,
                "server_id": p.server_id,
                "tool_name": p.tool_name,
                "digest": p.digest,
                "state": p.state,
            }
            for p in self.list_proposals()
            if p.state == "pending"
        ]
        recent_calls = (
            [
                {
                    "call_id": c.call_id,
                    "action_id": c.action_id,
                    "server_id": c.server_id,
                    "tool_name": c.tool_name,
                    "status": c.status,
                    "is_error": c.is_error,
                    "effect": c.effect,
                    "sha256": c.sha256,
                }
                for c in self._calls[-3:]
            ]
            if compact
            else [
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
                    "error_message": c.error_message[:256] if c.error_message else None,
                }
                for c in self._calls[-6:]
            ]
        )
        return {
            "enabled": True,
            "selected_skills": skills_payload,
            "mcp_servers": [v.model_dump(mode="json") for v in self._server_views()],
            "pending_proposals": proposals_summary,
            "recent_calls": recent_calls,
            "calls_used": len(self._calls),
            "max_calls": MAX_MCP_CALLS_PER_RUN,
            "call_bytes_used": self._total_call_bytes,
            "max_call_bytes": MAX_MCP_CALL_DATA_BYTES_PER_RUN,
            "unknown_side_effects": self.has_unknown_side_effects,
            "mutation_seq": self._mutation_seq,
        }

    async def aclose(self) -> ExtensionCleanup:
        """Revoke unconsumed approvals and close all connected MCP clients within bounded 2s total."""
        if self._cleanup_result is not None:
            return self._cleanup_result
        self._closed = True
        self._broker.cancel_pending()
        if not self._clients:
            self._cleanup_result = ExtensionCleanup(
                cleanup_ok=True,
                remaining_active=0,
                details={"clients_closed": 0},
            )
            return self._cleanup_result

        deadline = time.monotonic() + 2.0
        clients = list(self._clients.values())
        tasks = [asyncio.create_task(c.aclose()) for c in clients]
        rem = max(0.2, deadline - time.monotonic())
        done, pending = await asyncio.wait(tasks, timeout=rem)
        for p in pending:
            p.cancel()

        cleanup_ok = len(pending) == 0
        total_remaining = len(pending)
        per_server: dict[str, Any] = {}
        for t in done:
            try:
                res = t.result()
                if not res.cleanup_ok:
                    cleanup_ok = False
                total_remaining += res.remaining_active
                srv_id = str(res.details.get("server_id", "unknown"))
                per_server[srv_id] = res.model_dump(mode="json")
            except Exception as exc:  # noqa: BLE001
                cleanup_ok = False
                total_remaining += 1
                per_server[f"err-{len(per_server)}"] = {"error": str(exc)}

        self._cleanup_result = ExtensionCleanup(
            cleanup_ok=cleanup_ok and total_remaining == 0,
            remaining_active=total_remaining,
            details={"clients_closed": len(clients), "servers": per_server},
        )
        return self._cleanup_result
