"""MCP 2025-11-25 tools-subset client with strict schema validation and bounded calls."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import math
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

from forecontract_app.extension_models import (
    MAX_MCP_CALL_TIMEOUT_MS,
    MAX_MCP_RESULT_PAGE_BYTES,
    MAX_MCP_TOOL_PAGES,
    MAX_MCP_TOOLS_PER_SERVER,
    MAX_SCHEMA_DEPTH,
    MCP_CONNECT_TIMEOUT_SECONDS,
    MCP_LIST_TIMEOUT_SECONDS,
    MCP_PROTOCOL_VERSION,
    MCP_STOP_TIMEOUT_SECONDS,
    MIN_MCP_CALL_TIMEOUT_MS,
    ExtensionCleanup,
    ExtensionError,
    HttpMcpServerConfig,
    McpCallStatus,
    McpEffectStatus,
    McpServerConfig,
    McpServerState,
    McpServerView,
    McpToolInfo,
    McpToolResult,
    StdioMcpServerConfig,
    validate_no_nonfinite_numbers,
)
from forecontract_app.mcp_transport import (
    HttpMcpTransport,
    McpJsonRpcError,
    McpTransport,
    create_mcp_transport,
)
from forecontract_app.session_models import (
    canonical_json_dumps,
    sha256_canonical_json,
    truncate_utf8_safe,
)

_ALLOWED_SCHEMA_KEYS: Final[frozenset[str]] = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "title",
        "description",
        "default",
        "$schema",
    }
)

_ALLOWED_SCHEMA_TYPES: Final[frozenset[str]] = frozenset(
    {"object", "array", "string", "integer", "number", "boolean", "null"}
)


def _strict_equals(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return type(a) is type(b) and a == b
    return a == b


def analyze_input_schema(schema: Any) -> tuple[bool, str | None, str]:
    """Validate bounded JSON Schema subset for MCP tool arguments (depth <= 8)."""
    if not isinstance(schema, dict):
        return False, "malformed_schema", sha256_canonical_json({"invalid": True})
    try:
        validate_no_nonfinite_numbers(schema)
        schema_sha = sha256_canonical_json(schema)
    except ExtensionError:
        return False, "malformed_schema", sha256_canonical_json({"invalid": True})

    def _check_node(node: Any, depth: int) -> str | None:
        if depth > MAX_SCHEMA_DEPTH:
            return "unsupported_schema: depth exceeds 8"
        if not isinstance(node, dict):
            return "unsupported_schema: schema node must be object"
        unknown_keys = sorted(set(node.keys()) - _ALLOWED_SCHEMA_KEYS)
        if unknown_keys:
            return f"unsupported_schema: unsupported keywords {unknown_keys}"
        if depth > 0 and "$schema" in node:
            return "unsupported_schema: nested $schema not supported"
        stype = node.get("type")
        if stype is not None and (not isinstance(stype, str) or stype not in _ALLOWED_SCHEMA_TYPES):
            return f"unsupported_schema: unsupported type {stype!r}"
        if depth == 0 and stype != "object":
            return "unsupported_schema: root inputSchema must be object"

        if "enum" in node:
            ev = node["enum"]
            if not isinstance(ev, list) or not ev:
                return "unsupported_schema: enum must be non-empty list"

        for bkey in ("minimum", "maximum"):
            if bkey in node:
                bv = node[bkey]
                if isinstance(bv, bool) or not isinstance(bv, (int, float)) or not math.isfinite(bv):
                    return f"unsupported_schema: invalid {bkey}"

        for lkey in ("minLength", "maxLength", "minItems", "maxItems"):
            if lkey in node:
                lv = node[lkey]
                if isinstance(lv, bool) or not isinstance(lv, int) or lv < 0:
                    return f"unsupported_schema: invalid {lkey}"

        if "required" in node:
            req = node["required"]
            if not isinstance(req, list) or not all(isinstance(r, str) for r in req):
                return "unsupported_schema: required must be string list"

        if "properties" in node:
            props = node["properties"]
            if not isinstance(props, dict):
                return "unsupported_schema: properties must be object"
            for prop_name, sub in props.items():
                if not isinstance(prop_name, str):
                    return "unsupported_schema: property key must be string"
                err = _check_node(sub, depth + 1)
                if err is not None:
                    return err

        if "additionalProperties" in node:
            ap = node["additionalProperties"]
            if isinstance(ap, bool):
                pass
            elif isinstance(ap, dict):
                err = _check_node(ap, depth + 1)
                if err is not None:
                    return err
            else:
                return "unsupported_schema: additionalProperties must be bool or schema object"

        if "items" in node:
            items_schema = node["items"]
            if not isinstance(items_schema, dict):
                return "unsupported_schema: items must be single schema object"
            err = _check_node(items_schema, depth + 1)
            if err is not None:
                return err

        return None

    reason = _check_node(schema, 0)
    if reason is not None:
        return False, reason, schema_sha
    return True, None, schema_sha


def validate_value_against_schema(value: Any, schema: dict[str, Any], *, path: str = "$", depth: int = 0) -> None:
    """Strictly validate ``value`` against the verified MCP tool schema subset."""
    if depth > MAX_SCHEMA_DEPTH + 1:
        raise ExtensionError("MCP_INVALID_ARGS", f"argument nesting exceeds max depth at {path}", status_code=400)
    stype = schema.get("type")
    if stype == "null":
        if value is not None:
            raise ExtensionError("MCP_INVALID_ARGS", f"expected null at {path}", status_code=400)
    elif stype == "boolean":
        if not isinstance(value, bool):
            raise ExtensionError("MCP_INVALID_ARGS", f"expected boolean at {path}", status_code=400)
    elif stype == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ExtensionError("MCP_INVALID_ARGS", f"expected integer (not bool) at {path}", status_code=400)
    elif stype == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ExtensionError("MCP_INVALID_ARGS", f"expected finite number at {path}", status_code=400)
    elif stype == "string":
        if not isinstance(value, str):
            raise ExtensionError("MCP_INVALID_ARGS", f"expected string at {path}", status_code=400)
        if "\x00" in value:
            raise ExtensionError("MCP_INVALID_ARGS", f"NUL byte forbidden at {path}", status_code=400)
    elif stype == "array":
        if not isinstance(value, list):
            raise ExtensionError("MCP_INVALID_ARGS", f"expected array at {path}", status_code=400)
    elif stype == "object" and not isinstance(value, dict):
        raise ExtensionError("MCP_INVALID_ARGS", f"expected object at {path}", status_code=400)

    if "enum" in schema and not any(_strict_equals(value, option) for option in schema["enum"]):
        raise ExtensionError("MCP_INVALID_ARGS", f"value not in enum at {path}", status_code=400)

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise ExtensionError("MCP_INVALID_ARGS", f"value < minimum at {path}", status_code=400)
        if "maximum" in schema and value > schema["maximum"]:
            raise ExtensionError("MCP_INVALID_ARGS", f"value > maximum at {path}", status_code=400)

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise ExtensionError("MCP_INVALID_ARGS", f"string shorter than minLength at {path}", status_code=400)
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise ExtensionError("MCP_INVALID_ARGS", f"string longer than maxLength at {path}", status_code=400)

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise ExtensionError("MCP_INVALID_ARGS", f"array shorter than minItems at {path}", status_code=400)
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise ExtensionError("MCP_INVALID_ARGS", f"array longer than maxItems at {path}", status_code=400)
        if isinstance(schema.get("items"), dict):
            item_schema = schema["items"]
            for idx, elem in enumerate(value):
                validate_value_against_schema(elem, item_schema, path=f"{path}[{idx}]", depth=depth + 1)

    if isinstance(value, dict):
        props = schema.get("properties") or {}
        req = schema.get("required") or []
        for rkey in req:
            if rkey not in value:
                raise ExtensionError("MCP_INVALID_ARGS", f"missing required argument '{rkey}' at {path}", status_code=400)
        ap = schema.get("additionalProperties", True)
        for k, v in value.items():
            if k in props and isinstance(props[k], dict):
                validate_value_against_schema(v, props[k], path=f"{path}.{k}", depth=depth + 1)
            elif ap is False:
                raise ExtensionError("MCP_INVALID_ARGS", f"unexpected argument '{k}' at {path}", status_code=400)
            elif isinstance(ap, dict):
                validate_value_against_schema(v, ap, path=f"{path}.{k}", depth=depth + 1)


def validate_tool_arguments(tool: McpToolInfo, arguments: Any) -> str:
    """Validate ``arguments`` strictly against ``tool`` before sending and return canonical SHA-256."""
    if not tool.supported:
        raise ExtensionError(
            "MCP_UNSUPPORTED_SCHEMA",
            tool.unsupported_reason or f"tool '{tool.name}' has unsupported inputSchema",
            status_code=400,
        )
    if not isinstance(arguments, dict):
        raise ExtensionError("MCP_INVALID_ARGS", "tool arguments must be a JSON object", status_code=400)
    validate_no_nonfinite_numbers(arguments)
    validate_value_against_schema(arguments, tool.input_schema)
    return sha256_canonical_json(arguments)


class McpClient:
    """Protocol client implementing MCP 2025-11-25 tools subset over one server config."""

    def __init__(
        self,
        config: McpServerConfig,
        *,
        scratch_root: Path,
        config_sha256: str = "",
        on_tools_list_changed: Callable[[str], None] | None = None,
    ) -> None:
        self._server = config
        self._scratch_root = scratch_root
        self._config_sha256 = config_sha256
        self._on_list_changed = on_tools_list_changed
        self._transport: McpTransport | None = None
        self._state: McpServerState = "not_connected"
        self._protocol_version: str | None = None
        self._server_name: str | None = None
        self._server_version: str | None = None
        self._tools: dict[str, McpToolInfo] = {}
        self._tools_listed: bool = False
        self._list_changed_stale: bool = False
        self._last_error: str | None = None
        self._rpc_seq = 0
        self._inflight: dict[str, tuple[str | int, asyncio.Task[dict[str, Any]]]] = {}
        self._cancelled_calls: set[str] = set()
        self._server_requests_rejected: list[tuple[str | int, str]] = []
        self._closed = False

    @property
    def server(self) -> McpServerConfig:
        return self._server

    @property
    def state(self) -> McpServerState:
        return self._state

    @property
    def tools_listed(self) -> bool:
        return self._tools_listed and not self._list_changed_stale

    @property
    def active_readers_peak(self) -> int:
        return self._transport.active_readers_peak if self._transport is not None else 0

    @property
    def get_stream_downgraded(self) -> bool:
        return (
            self._transport.get_stream_downgraded
            if isinstance(self._transport, HttpMcpTransport)
            else False
        )

    @property
    def delete_downgraded(self) -> bool:
        return (
            self._transport.delete_downgraded
            if isinstance(self._transport, HttpMcpTransport)
            else False
        )

    @property
    def rejected_server_requests(self) -> list[tuple[str | int, str]]:
        return list(self._server_requests_rejected)

    def get_cached_tool(self, tool_name: str) -> McpToolInfo | None:
        return self._tools.get(tool_name)

    def _next_rpc_id(self) -> int:
        self._rpc_seq += 1
        return self._rpc_seq

    def _handle_notification(self, method: str, params: dict[str, Any] | None) -> None:
        _ = params
        if method == "notifications/tools/list_changed":
            self._list_changed_stale = True
            self._tools_listed = False
            self._tools.clear()
            if self._on_list_changed is not None:
                self._on_list_changed(self._server.id)

    def _handle_server_request(
        self, req_id: str | int, method: str, params: dict[str, Any] | None
    ) -> None:
        _ = params
        self._server_requests_rejected.append((req_id, method))

    def view(self) -> McpServerView:
        is_stdio = isinstance(self._server, StdioMcpServerConfig)
        sess_present = (
            self._transport is not None and self._transport.session_id is not None
        )
        stderr_ex, _ = truncate_utf8_safe(
            self._transport.stderr_text() if self._transport is not None else "",
            2048,
        )
        return McpServerView(
            id=self._server.id,
            transport=self._server.transport,
            executable=self._server.executable if is_stdio else None,
            argv=list(self._server.argv) if is_stdio else None,
            cwd=self._server.cwd if is_stdio else None,
            url=self._server.url if isinstance(self._server, HttpMcpServerConfig) else None,
            config_sha256=self._config_sha256 or sha256_canonical_json(self._server.model_dump(mode="json")),
            executable_sha256=self._server.executable_sha256 if is_stdio else None,
            state=self._state,
            protocol_version=self._protocol_version,
            server_name=self._server_name,
            server_version=self._server_version,
            tools_count=len(self._tools),
            tools_listed=self.tools_listed,
            list_changed_stale=self._list_changed_stale,
            session_id_present=sess_present,
            last_error=self._last_error,
            stderr_excerpt=stderr_ex,
        )

    async def connect(self) -> McpServerView:
        """Connect, enforce exact MCP ``2025-11-25`` tools capability, and emit ``notifications/initialized``."""
        if self._closed:
            raise ExtensionError("MCP_CLIENT_CLOSED", "client is closed", status_code=409)
        if self._state == "connected" and self._transport is not None:
            return self.view()

        deadline = time.monotonic() + MCP_CONNECT_TIMEOUT_SECONDS
        transport = create_mcp_transport(
            server=self._server,
            scratch_root=self._scratch_root,
            on_notification=self._handle_notification,
            on_server_request=self._handle_server_request,
        )
        self._transport = transport
        try:
            await transport.start()
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise ExtensionError("MCP_TIMEOUT", "MCP connect timed out", status_code=504)
            init_res = await transport.send_request(
                req_id=self._next_rpc_id(),
                method="initialize",
                params={
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "forecontract-app", "version": "1.0.0"},
                },
                timeout_seconds=rem,
                protocol_version=None,
            )
            proto_ver = init_res.get("protocolVersion")
            if proto_ver != MCP_PROTOCOL_VERSION:
                raise ExtensionError(
                    "MCP_UNSUPPORTED_PROTOCOL_VERSION",
                    f"expected protocolVersion '{MCP_PROTOCOL_VERSION}', got {proto_ver!r}",
                    status_code=400,
                )
            caps = init_res.get("capabilities")
            if not isinstance(caps, dict) or not isinstance(caps.get("tools"), dict):
                raise ExtensionError(
                    "MCP_TOOLS_UNSUPPORTED",
                    "MCP server capabilities must include object 'tools'",
                    status_code=400,
                )
            srv_info = init_res.get("serverInfo")
            if isinstance(srv_info, dict):
                if isinstance(srv_info.get("name"), str):
                    self._server_name = srv_info["name"][:128]
                if isinstance(srv_info.get("version"), str):
                    self._server_version = srv_info["version"][:128]

            rem = deadline - time.monotonic()
            if rem <= 0:
                raise ExtensionError("MCP_TIMEOUT", "MCP connect timed out before initialized", status_code=504)
            await transport.send_notification(
                method="notifications/initialized",
                params={},
                timeout_seconds=rem,
                protocol_version=MCP_PROTOCOL_VERSION,
            )

            if isinstance(transport, HttpMcpTransport):
                rem = deadline - time.monotonic()
                if rem > 0.05:
                    await transport.start_optional_get_stream(
                        protocol_version=MCP_PROTOCOL_VERSION,
                        timeout_seconds=min(0.4, rem),
                    )

            self._protocol_version = MCP_PROTOCOL_VERSION
            self._state = "connected"
            self._last_error = None
            return self.view()
        except Exception as exc:
            self._state = "error"
            self._last_error = str(exc)[:256]
            await transport.aclose(timeout_seconds=MCP_STOP_TIMEOUT_SECONDS)
            self._transport = None
            raise

    async def list_tools(self) -> list[McpToolInfo]:
        """Paginate bounded ``tools/list`` (<=16 pages, <=128 tools, loop/dup-free) within 5s."""
        if self._closed or self._transport is None or self._state != "connected":
            raise ExtensionError("MCP_NOT_CONNECTED", "MCP server is not connected", status_code=409)
        if self._protocol_version != MCP_PROTOCOL_VERSION:
            raise ExtensionError("MCP_PROTOCOL_ERROR", "initialize must complete first", status_code=409)

        deadline = time.monotonic() + MCP_LIST_TIMEOUT_SECONDS
        cursor: str | None = None
        seen_cursors: set[str] = set()
        seen_names: set[str] = set()
        discovered: list[McpToolInfo] = []

        for _page_idx in range(MAX_MCP_TOOL_PAGES):
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise ExtensionError("MCP_TIMEOUT", "tools/list exceeded 5s total budget", status_code=504)
            params: dict[str, Any] | None = {"cursor": cursor} if cursor is not None else None
            res = await self._transport.send_request(
                req_id=self._next_rpc_id(),
                method="tools/list",
                params=params,
                timeout_seconds=rem,
                protocol_version=MCP_PROTOCOL_VERSION,
            )
            raw_tools = res.get("tools")
            if not isinstance(raw_tools, list):
                raise ExtensionError("MCP_PROTOCOL_ERROR", "tools/list result missing list 'tools'", status_code=502)

            for item in raw_tools:
                if not isinstance(item, dict):
                    raise ExtensionError("MCP_PROTOCOL_ERROR", "tool descriptor must be object", status_code=502)
                name = item.get("name")
                if not isinstance(name, str) or not name or "\x00" in name:
                    raise ExtensionError("MCP_PROTOCOL_ERROR", "tool missing valid string name", status_code=502)
                if name in seen_names:
                    raise ExtensionError(
                        "MCP_DUPLICATE_TOOL_NAME",
                        f"server '{self._server.id}' returned duplicate tool '{name}'",
                        status_code=502,
                    )
                seen_names.add(name)
                if len(discovered) >= MAX_MCP_TOOLS_PER_SERVER:
                    raise ExtensionError(
                        "MCP_TOOL_LIMIT_EXCEEDED",
                        f"server '{self._server.id}' exceeded max {MAX_MCP_TOOLS_PER_SERVER} tools",
                        status_code=502,
                    )

                raw_title = item.get("title")
                title = raw_title if isinstance(raw_title, str) else ""
                raw_desc = item.get("description")
                description = raw_desc if isinstance(raw_desc, str) else ""
                if "inputSchema" not in item or not isinstance(item["inputSchema"], dict):
                    raise ExtensionError(
                        "MCP_PROTOCOL_ERROR",
                        f"tool '{name}' missing object inputSchema",
                        status_code=502,
                    )
                input_schema = dict(item["inputSchema"])
                raw_ann = item.get("annotations")
                if raw_ann is not None and not isinstance(raw_ann, dict):
                    raise ExtensionError("MCP_PROTOCOL_ERROR", f"tool '{name}' malformed annotations", status_code=502)
                annotations = dict(raw_ann) if isinstance(raw_ann, dict) else {}
                supported, unsupported_reason, schema_sha = analyze_input_schema(input_schema)
                if not supported and unsupported_reason == "malformed_schema":
                    raise ExtensionError(
                        "MCP_PROTOCOL_ERROR",
                        f"tool '{name}' inputSchema contains malformed/non-finite JSON",
                        status_code=502,
                    )

                title_cut, _ = truncate_utf8_safe(title, 256)
                desc_cut, _ = truncate_utf8_safe(description, 1024)
                discovered.append(
                    McpToolInfo(
                        server_id=self._server.id,
                        name=name,
                        title=title_cut,
                        description=desc_cut,
                        input_schema=input_schema,
                        schema_sha256=schema_sha,
                        supported=supported,
                        unsupported_reason=unsupported_reason,
                        annotations=annotations,
                    )
                )

            next_cursor = res.get("nextCursor")
            if next_cursor is None or next_cursor == "":
                break
            if not isinstance(next_cursor, str):
                raise ExtensionError("MCP_PROTOCOL_ERROR", "nextCursor must be string", status_code=502)
            if next_cursor in seen_cursors:
                raise ExtensionError("MCP_CURSOR_LOOP", "tools/list nextCursor loop detected", status_code=502)
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            raise ExtensionError(
                "MCP_TOOL_LIMIT_EXCEEDED",
                f"tools/list exceeded {MAX_MCP_TOOL_PAGES} pages",
                status_code=502,
            )

        self._tools = {t.name: t for t in discovered}
        self._tools_listed = True
        self._list_changed_stale = False
        return list(discovered)

    def _parse_tool_call_result(
        self,
        *,
        call_id: str,
        action_id: str,
        tool_name: str,
        args_sha256: str,
        schema_sha256: str,
        raw_result: dict[str, Any],
    ) -> McpToolResult:
        def _protocol_error(msg: str) -> McpToolResult:
            return McpToolResult(
                call_id=call_id,
                action_id=action_id,
                server_id=self._server.id,
                tool_name=tool_name,
                args_sha256=args_sha256,
                schema_sha256=schema_sha256,
                status="unknown",
                is_error=True,
                effect="unknown",
                text="",
                structured_content=None,
                content_items=[],
                truncated=False,
                original_bytes=0,
                sha256=sha256_canonical_json(raw_result),
                error_code="MCP_PROTOCOL_ERROR",
                error_message=msg,
            )

        raw_content = raw_result.get("content")
        raw_is_error = raw_result.get("isError", False)
        if not isinstance(raw_content, list) or not isinstance(raw_is_error, bool):
            return _protocol_error(
                "tools/call response missing valid 'content' list or boolean 'isError'"
            )
        if "structuredContent" in raw_result and not isinstance(
            raw_result["structuredContent"], dict
        ):
            return _protocol_error(
                "tools/call 'structuredContent' must be a JSON object when present"
            )
        if "_meta" in raw_result and not isinstance(raw_result["_meta"], dict):
            return _protocol_error("tools/call '_meta' must be a JSON object when present")

        text_parts: list[str] = []
        content_items: list[dict[str, Any]] = []
        for entry in raw_content:
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("type"), str)
                or not entry["type"].strip()
            ):
                return _protocol_error(
                    "tools/call content item must be object with non-empty string type"
                )
            for str_field in ("text", "mimeType", "uri", "data"):
                if str_field in entry and not isinstance(entry[str_field], str):
                    return _protocol_error(
                        f"tools/call content item '{str_field}' must be string"
                    )
            for dict_field in ("annotations", "_meta"):
                if dict_field in entry and not isinstance(entry[dict_field], dict):
                    return _protocol_error(
                        f"tools/call content item '{dict_field}' must be object"
                    )
            if "resource" in entry:
                res_obj = entry["resource"]
                if not isinstance(res_obj, dict):
                    return _protocol_error("tools/call content item 'resource' must be object")
                for r_str in ("uri", "mimeType", "text", "blob"):
                    if r_str in res_obj and not isinstance(res_obj[r_str], str):
                        return _protocol_error(
                            f"tools/call resource field '{r_str}' must be string"
                        )
            ctype = entry["type"]
            if ctype == "text" and isinstance(entry.get("text"), str):
                text_parts.append(entry["text"])
                content_items.append({"type": "text", "displayed": True})
            elif ctype == "text":
                return _protocol_error("text content item missing string 'text'")
            else:
                summary_item: dict[str, Any] = {"type": ctype, "displayed": False}
                if isinstance(entry.get("mimeType"), str):
                    summary_item["mimeType"] = entry["mimeType"][:64]
                if isinstance(entry.get("uri"), str):
                    uri_cut, _ = truncate_utf8_safe(entry["uri"], 256)
                    summary_item["uri"] = uri_cut
                content_items.append(summary_item)

        full_text = "\n".join(text_parts)
        raw_struct: dict[str, Any] | None = raw_result.get("structuredContent")
        full_canon = canonical_json_dumps(
            {
                "content": raw_content,
                "structuredContent": raw_struct,
                "isError": raw_is_error,
            }
        )
        full_bytes = full_canon.encode("utf-8")
        full_sha = hashlib.sha256(full_bytes).hexdigest()

        bounded_text, text_cut = truncate_utf8_safe(full_text, MAX_MCP_RESULT_PAGE_BYTES)
        bounded_struct = raw_struct
        struct_cut = False
        if raw_struct is not None:
            struct_json = canonical_json_dumps(raw_struct)
            if len(struct_json.encode("utf-8")) > MAX_MCP_RESULT_PAGE_BYTES // 2:
                excerpt, _ = truncate_utf8_safe(struct_json, 1024)
                bounded_struct = {"_truncated_preview": excerpt}
                struct_cut = True

        status: McpCallStatus = "error" if raw_is_error else "success"
        return McpToolResult(
            call_id=call_id,
            action_id=action_id,
            server_id=self._server.id,
            tool_name=tool_name,
            args_sha256=args_sha256,
            schema_sha256=schema_sha256,
            status=status,
            is_error=raw_is_error,
            effect="possibly_applied",
            text=bounded_text,
            structured_content=bounded_struct,
            content_items=content_items[:16],
            truncated=bool(text_cut or struct_cut),
            original_bytes=len(full_bytes),
            sha256=full_sha,
            error_code="MCP_TOOL_ERROR" if raw_is_error else None,
            error_message=bounded_text if raw_is_error and bounded_text else None,
        )

    async def call_tool(
        self,
        *,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        timeout_ms: int = 10_000,
        action_id: str = "",
    ) -> McpToolResult:
        """Invoke one validated MCP tool with strict single in-flight call & bounded output."""
        if not isinstance(call_id, str) or not call_id.strip() or "\x00" in call_id:
            raise ExtensionError("INVALID_INPUT", "call_id must be non-blank string", status_code=400)
        if (
            isinstance(timeout_ms, bool)
            or not isinstance(timeout_ms, int)
            or not (MIN_MCP_CALL_TIMEOUT_MS <= timeout_ms <= MAX_MCP_CALL_TIMEOUT_MS)
        ):
            raise ExtensionError(
                "INVALID_INPUT",
                f"timeout_ms must be int in {MIN_MCP_CALL_TIMEOUT_MS}..{MAX_MCP_CALL_TIMEOUT_MS}",
                status_code=400,
            )
        if self._closed or self._transport is None:
            raise ExtensionError("MCP_NOT_CONNECTED", "MCP server is not connected", status_code=409)
        if isinstance(self._transport, HttpMcpTransport) and self._transport.session_invalidated:
            self._state = "error"
            raise ExtensionError("MCP_SESSION_EXPIRED", "HTTP session already invalidated", status_code=409)
        if self._state != "connected":
            raise ExtensionError("MCP_NOT_CONNECTED", "MCP server is not connected", status_code=409)
        if self._list_changed_stale or not self._tools_listed:
            raise ExtensionError(
                "MCP_TOOLS_STALE",
                "tools/list must be executed (or refreshed after list_changed) before calling tools",
                status_code=409,
            )
        tool = self._tools.get(tool_name)
        if tool is None:
            raise ExtensionError("MCP_TOOL_NOT_FOUND", f"tool '{tool_name}' not found on '{self._server.id}'", status_code=404)
        args_sha = validate_tool_arguments(tool, arguments)
        if self._inflight:
            raise ExtensionError(
                "MCP_CALL_IN_PROGRESS",
                f"server '{self._server.id}' already has an active tool call",
                status_code=409,
            )

        rpc_id = self._next_rpc_id()
        req_task = asyncio.create_task(
            self._transport.send_request(
                req_id=rpc_id,
                method="tools/call",
                params={"name": tool_name, "arguments": arguments},
                timeout_seconds=timeout_ms / 1000.0,
                protocol_version=MCP_PROTOCOL_VERSION,
            )
        )
        self._inflight[call_id] = (rpc_id, req_task)

        try:
            raw_res = await req_task
            return self._parse_tool_call_result(
                call_id=call_id,
                action_id=action_id,
                tool_name=tool_name,
                args_sha256=args_sha,
                schema_sha256=tool.schema_sha256,
                raw_result=raw_res,
            )
        except asyncio.CancelledError:
            was_explicit = call_id in self._cancelled_calls
            await self._send_cancelled_best_effort(rpc_id, reason="cancelled" if was_explicit else "aborted")
            if was_explicit:
                return McpToolResult(
                    call_id=call_id,
                    action_id=action_id,
                    server_id=self._server.id,
                    tool_name=tool_name,
                    args_sha256=args_sha,
                    schema_sha256=tool.schema_sha256,
                    status="cancelled",
                    is_error=True,
                    effect="unknown",
                    sha256=hashlib.sha256(b"cancelled").hexdigest(),
                    error_code="MCP_CALL_CANCELLED",
                    error_message="tool call cancelled after dispatch; remote effect unknown",
                )
            raise
        except McpJsonRpcError as exc:
            return McpToolResult(
                call_id=call_id,
                action_id=action_id,
                server_id=self._server.id,
                tool_name=tool_name,
                args_sha256=args_sha,
                schema_sha256=tool.schema_sha256,
                status="error",
                is_error=True,
                effect="possibly_applied",
                text="",
                sha256=hashlib.sha256(exc.message.encode("utf-8")).hexdigest(),
                error_code=exc.code,
                error_message=exc.message[:1024],
            )
        except ExtensionError as exc:
            st: McpCallStatus = (
                "timeout"
                if exc.code == "MCP_TIMEOUT"
                else ("unknown" if exc.code in ("MCP_SESSION_EXPIRED", "MCP_TRANSPORT_EOF", "MCP_STREAM_ENDED", "MCP_PROTOCOL_ERROR", "MCP_FRAME_TOO_LARGE") else "error")
            )
            eff: McpEffectStatus = "unknown"
            if exc.code == "MCP_TIMEOUT":
                await self._send_cancelled_best_effort(rpc_id, reason="timeout")
            if exc.code in ("MCP_SESSION_EXPIRED", "MCP_TRANSPORT_EOF", "MCP_FRAME_TOO_LARGE", "MCP_PROTOCOL_ERROR"):
                self._state = "error"
                self._last_error = exc.message[:256]
            return McpToolResult(
                call_id=call_id,
                action_id=action_id,
                server_id=self._server.id,
                tool_name=tool_name,
                args_sha256=args_sha,
                schema_sha256=tool.schema_sha256,
                status=st,
                is_error=True,
                effect=eff,
                sha256=hashlib.sha256(exc.message.encode("utf-8")).hexdigest(),
                error_code=exc.code,
                error_message=exc.message[:1024],
            )
        finally:
            self._inflight.pop(call_id, None)
            self._cancelled_calls.discard(call_id)

    async def _send_cancelled_best_effort(self, rpc_id: str | int, *, reason: str) -> None:
        if self._transport is None:
            return
        with contextlib.suppress(ExtensionError, TimeoutError, OSError):
            await asyncio.wait_for(
                self._transport.send_notification(
                    method="notifications/cancelled",
                    params={"requestId": rpc_id, "reason": reason},
                    timeout_seconds=0.3,
                    protocol_version=MCP_PROTOCOL_VERSION,
                ),
                timeout=0.3,
            )

    async def cancel(self, *, call_id: str) -> None:
        """Request best-effort cancellation of one active ``call_id`` without asserting rollback."""
        entry = self._inflight.get(call_id)
        if entry is None:
            return
        rpc_id, task = entry
        self._cancelled_calls.add(call_id)
        await self._send_cancelled_best_effort(rpc_id, reason="user_cancelled")
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError, ExtensionError, OSError):
                await asyncio.wait_for(asyncio.shield(task), timeout=0.3)

    async def aclose(self) -> ExtensionCleanup:
        """Cancel active calls, terminate child Job/HTTP client within 2s, and return cleanup proof."""
        if self._closed:
            if self._transport is not None:
                return await self._transport.aclose(timeout_seconds=MCP_STOP_TIMEOUT_SECONDS)
            return ExtensionCleanup(cleanup_ok=True, remaining_active=0, details={"server_id": self._server.id})

        self._closed = True
        deadline = time.monotonic() + MCP_STOP_TIMEOUT_SECONDS
        for cid, (rpc_id, task) in list(self._inflight.items()):
            self._cancelled_calls.add(cid)
            if not task.done():
                await self._send_cancelled_best_effort(rpc_id, reason="aclose")
                task.cancel()
        if self._inflight:
            tasks = [t for _, t in self._inflight.values() if not t.done()]
            if tasks:
                rem = max(0.05, min(0.35, deadline - time.monotonic()))
                try:
                    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=rem)
                except asyncio.TimeoutError:
                    pass
        if self._transport is not None:
            rem = max(0.2, deadline - time.monotonic())
            cleanup = await self._transport.aclose(timeout_seconds=rem)
            self._state = "closed" if cleanup.cleanup_ok else "unknown"
            return cleanup
        self._state = "closed"
        return ExtensionCleanup(cleanup_ok=True, remaining_active=0, details={"server_id": self._server.id})
