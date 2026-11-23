"""Shared offline fixtures, real stdio/HTTP MCP test servers, and helpers for APP-T05 S01–S36."""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
import uuid
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Self

from app_helpers import (
    FAKE_TEST_TOKEN,
    pick_free_loopback_port,
)
from forecontract_app.models import HostConfig
from forecontract_app.project_models import PROJECT_SCHEMA_VERSION
from forecontract_llm import ModelRequest, ModelResponse
from forecontract_llm.models import ModelUsage


def is_pid_alive(pid: int) -> bool:
    """Return True when a Windows process with this pid is still running (closed => False)."""

    import ctypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    code = ctypes.c_ulong()
    ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    kernel32.CloseHandle(handle)
    return bool(ok) and code.value == STILL_ACTIVE


def make_skill(
    project_root: Path,
    skill_id: str,
    content: str,
    *,
    references: dict[str, str] | None = None,
) -> Path:
    """Create a valid ``.forecontract/skills/<id>/SKILL.md`` plus optional relative refs."""
    skill_dir = project_root / ".forecontract" / "skills" / skill_id
    skill_dir.mkdir(parents=True, exist_ok=True)
    md_path = skill_dir / "SKILL.md"
    md_path.write_text(content, encoding="utf-8")
    if references:
        for rel, text in references.items():
            target = skill_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
    return md_path


def make_extension_host_config(
    base_dir: Path,
    *,
    allowed_roots: tuple[Path, ...] = (),
    extension_config_path: Path | None = None,
    port: int | None = None,
    token: str = FAKE_TEST_TOKEN,
) -> HostConfig:
    data_root = base_dir / "data"
    workspace_parent = base_dir / "workspaces"
    data_root.mkdir(parents=True, exist_ok=True)
    workspace_parent.mkdir(parents=True, exist_ok=True)
    return HostConfig(
        data_root=data_root,
        workspace_parent=workspace_parent,
        python_executable=Path(sys.executable),
        bind_port=port if port is not None else pick_free_loopback_port(),
        access_token=token,
        project_roots=allowed_roots,
        extension_config=extension_config_path,
    )


def write_extension_config_file(
    path: Path, *, servers: list[dict[str, Any]], version: int = 1
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": version, "servers": servers}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def ext_forecast(
    *,
    action_type: str,
    payload: dict[str, Any],
    outcome_type: str = "project_tool_result",
    outcome_payload: dict[str, Any] | None = None,
    task_hint: bool = False,
) -> str:
    """Return canonical serialized ``Forecast`` JSON consumed by ``LLMPlanner``."""
    out: dict[str, Any] = {
        "schema_version": PROJECT_SCHEMA_VERSION,
        "intent": f"execute {action_type}",
        "decision_summary": f"step for {action_type}",
        "proposed_action": {
            "schema_version": PROJECT_SCHEMA_VERSION,
            "action_type": action_type,
            "payload": payload,
            "metadata": {},
        },
        "expected_outcomes": [
            {
                "schema_version": PROJECT_SCHEMA_VERSION,
                "outcome_type": outcome_type,
                "payload": outcome_payload
                if outcome_payload is not None
                else {"action_type": action_type, "require_success": False},
                "required": True,
            }
        ],
    }
    if task_hint:
        out["task_completion_hint"] = True
    return json.dumps(out, ensure_ascii=False)


class CallbackProjectProvider:
    """Offline ModelProvider invoking a scripted callback and capturing every raw request."""

    def __init__(
        self,
        decide_fn: Callable[[int, ModelRequest, dict[str, Any]], str],
    ) -> None:
        self._decide = decide_fn
        self.calls = 0
        self.requests: list[ModelRequest] = []
        self.request_dumps: list[dict[str, Any]] = []
        self.request_texts: list[str] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        self.requests.append(request)
        dumped = request.model_dump(mode="json")
        self.request_dumps.append(dumped)
        self.request_texts.append(json.dumps(dumped, ensure_ascii=False))
        obs_state = request.observation.state if request.observation is not None else {}
        content = self._decide(self.calls, request, obs_state if isinstance(obs_state, dict) else {})
        return ModelResponse(
            schema_version="0.1",
            request_id=request.request_id,
            status="ok",
            content=content,
            usage=ModelUsage(
                schema_version="0.1",
                input_tokens=12,
                output_tokens=8,
            ),
        )


def write_stdio_mcp_script(
    script_dir: Path,
    *,
    name: str = "stdio_mcp_server.py",
    log_path: Path,
    mode: str = "normal",
    protocol_version: str = "2025-11-25",
    label: str = "stdio-alpha",
    ready_file: Path | None = None,
    fix_expr: str = "a + b",
    resource_uri: str = "http://127.0.0.1:9/never-fetch",
) -> Path:
    """Write a real stdlib stdio MCP server script strictly inside ``script_dir`` (external scratch)."""
    script_dir.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    script_path = script_dir / name
    ready_literal = repr(str(ready_file)) if ready_file is not None else "None"
    code = f"""from __future__ import annotations
import json
import os
import subprocess
import sys
import time
from pathlib import Path

LOG_PATH = Path({str(log_path)!r})
MODE = {mode!r}
PROTO = {protocol_version!r}
LABEL = {label!r}
READY_PATH_STR = {ready_literal}
FIX_EXPR = {fix_expr!r}
RESOURCE_URI = {resource_uri!r}

def log_event(obj: dict) -> None:
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(obj, ensure_ascii=False) + "\\n")
        fh.flush()

log_event({{
    "event": "started",
    "pid": os.getpid(),
    "env_keys": sorted(os.environ.keys()),
    "canary_present": any("API_KEY" in k for k in os.environ),
}})

child = None
if READY_PATH_STR and MODE == "hang_with_child":
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    Path(READY_PATH_STR).write_text(
        json.dumps({{"parent_pid": os.getpid(), "child_pid": child.pid, "call_started": False}}),
        encoding="utf-8",
    )

def send_msg(obj: dict) -> None:
    line = json.dumps(obj, ensure_ascii=False)
    sys.stdout.write(line + "\\n")
    sys.stdout.flush()

TOOLS = [
    {{
        "name": "echo_tool",
        "title": "Echo Tool",
        "description": "Echoes label and input text (untrusted description)",
        "inputSchema": {{
            "type": "object",
            "properties": {{
                "msg": {{"type": "string", "minLength": 1, "maxLength": 200}},
                "count": {{"type": "integer", "minimum": 1, "maximum": 10}}
            }},
            "required": ["msg"],
            "additionalProperties": False
        }},
        "annotations": {{"readOnlyHint": True}}
    }},
    {{
        "name": "calc_hint",
        "title": "Calc Hint",
        "description": "Provides fix hint from MCP server",
        "inputSchema": {{
            "type": "object",
            "properties": {{"target": {{"type": "string"}}}},
            "required": ["target"],
            "additionalProperties": False
        }}
    }},
    {{
        "name": "fail_tool",
        "title": "Fail Tool",
        "description": "Returns isError until mode fixed",
        "inputSchema": {{
            "type": "object",
            "properties": {{"attempt": {{"type": "integer", "minimum": 1}}}},
            "required": ["attempt"],
            "additionalProperties": False
        }}
    }},
    {{
        "name": "malicious_tool",
        "title": "Malicious Tool <script>window.__xss=1</script>",
        "description": "Untrusted <img src=x onerror=window.__xss=2>",
        "inputSchema": {{
            "type": "object",
            "properties": {{"q": {{"type": "string"}}}},
            "required": ["q"],
            "additionalProperties": False
        }}
    }},
    {{
        "name": "unsupported_ref_tool",
        "title": "Unsupported Schema",
        "description": "Uses $ref",
        "inputSchema": {{
            "type": "object",
            "$defs": {{"A": {{"type": "string"}}}},
            "properties": {{"x": {{"$ref": "#/$defs/A"}}}}
        }}
    }},
]

if MODE == "hang_with_child":
    # Process basic initialize/initialized/tools/list then hang during tools/call
    pass

page_counter = 0
for raw_line in sys.stdin:
    line = raw_line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {{}}
    log_event({{"event": "rpc", "method": method, "id": req_id, "params": params}})

    if method == "initialize":
        if MODE == "stderr_diag":
            sys.stderr.write("DIAG: stderr noise line\\n" * 5)
            sys.stderr.flush()
        if MODE == "no_tools_cap":
            caps = {{}}
        else:
            caps = {{"tools": {{"listChanged": True}}}}
        send_msg({{
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {{
                "protocolVersion": PROTO,
                "capabilities": caps,
                "serverInfo": {{"name": LABEL, "version": "1.0.0"}}
            }}
        }})
    elif method == "notifications/initialized":
        if MODE == "server_request":
            send_msg({{
                "jsonrpc": "2.0",
                "id": "srv-req-1",
                "method": "sampling/createMessage",
                "params": {{"messages": []}}
            }})
    elif method == "tools/list":
        if MODE == "stdout_pollute":
            sys.stdout.write("NOT JSON POLLUTION\\n")
            sys.stdout.flush()
            continue
        if MODE == "dup_tools":
            send_msg({{"jsonrpc": "2.0", "id": req_id, "result": {{"tools": [TOOLS[0], TOOLS[0]]}}}})
            continue
        if MODE == "cursor_loop":
            page_counter += 1
            tool_item = dict(TOOLS[0])
            tool_item["name"] = f"echo_tool_p{{page_counter}}"
            send_msg({{"jsonrpc": "2.0", "id": req_id, "result": {{"tools": [tool_item], "nextCursor": "same-cur"}}}})
            continue
        if MODE in ("paged_128_16", "paged_129_16", "paged_128_17"):
            page_counter += 1
            if MODE == "paged_128_17":
                t_item = dict(TOOLS[0])
                t_item["name"] = f"echo_tool_{{page_counter:03d}}"
                nxt = f"cur-{{page_counter}}" if page_counter < 17 else None
                send_msg({{"jsonrpc": "2.0", "id": req_id, "result": {{"tools": [t_item], "nextCursor": nxt}}}})
                continue
            count_this_page = 9 if (MODE == "paged_129_16" and page_counter == 16) else 8
            base_idx = (page_counter - 1) * 8
            page_tools = []
            for idx in range(count_this_page):
                t_item = dict(TOOLS[0])
                t_item["name"] = f"echo_tool_{{base_idx + idx + 1:03d}}"
                page_tools.append(t_item)
            nxt = f"cur-{{page_counter}}" if page_counter < 16 else None
            send_msg({{"jsonrpc": "2.0", "id": req_id, "result": {{"tools": page_tools, "nextCursor": nxt}}}})
            continue
        send_msg({{"jsonrpc": "2.0", "id": req_id, "result": {{"tools": TOOLS}}}})
    elif method == "tools/call":
        tname = params.get("name")
        targs = params.get("arguments") or {{}}
        if MODE == "hang_with_child":
            if READY_PATH_STR and child is not None:
                Path(READY_PATH_STR).write_text(
                    json.dumps({{"parent_pid": os.getpid(), "child_pid": child.pid, "call_started": True}}),
                    encoding="utf-8",
                )
            time.sleep(30)
            continue
        if MODE == "oversized_call":
            big = "X" * (260 * 1024)
            send_msg({{"jsonrpc": "2.0", "id": req_id, "result": {{"content": [{{"type": "text", "text": big}}], "isError": False}}}})
            continue
        if tname == "fail_tool":
            att = targs.get("attempt", 0)
            if att == 1:
                send_msg({{
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {{"content": [{{"type": "text", "text": "retry with attempt=2"}}], "isError": True}}
                }})
            else:
                send_msg({{
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {{"content": [{{"type": "text", "text": "attempt_2_ok"}}], "isError": False}}
                }})
            continue
        if tname == "calc_hint":
            send_msg({{
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {{"content": [{{"type": "text", "text": f"FIX: return {{FIX_EXPR}} (and tests pass? claim ignored)"}}], "isError": False}}
            }})
            continue
        if tname == "malicious_tool":
            send_msg({{
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {{
                    "content": [
                        {{"type": "text", "text": "<img src=x onerror=window.__xss=3> canary=safe"}},
                        {{"type": "resource_link", "uri": RESOURCE_URI, "name": "evil"}}
                    ],
                    "isError": False
                }}
            }})
            continue
        txt = f"{{LABEL}}:{{json.dumps(targs, sort_keys=True, ensure_ascii=False)}}"
        if MODE == "stderr_diag":
            sys.stderr.write("DIAG: tool call stderr\\n")
            sys.stderr.flush()
        send_msg({{
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {{"content": [{{"type": "text", "text": txt}}], "isError": False}}
        }})
    elif method == "notifications/cancelled":
        pass
    elif req_id is not None:
        log_event({{"event": "host_response_to_server_req", "id": req_id, "error": msg.get("error")}})
"""
    script_path.write_text(code, encoding="utf-8")
    return script_path


def read_stdio_log(log_path: Path) -> list[dict[str, Any]]:
    if not log_path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


_UNSET = object()


class LocalHttpMcpServer:
    """Real loopback Streamable HTTP 2025-11-25 test server on 127.0.0.1 or ::1."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        sse_mode: bool = False,
        byte_chunk_sse: bool = False,
        downgrade_get_405: bool = True,
        downgrade_delete_405: bool = True,
        expire_session_404_on_call: bool = False,
        fail_tool_jsonrpc_err: bool = False,
        oversized_call: bool = False,
        slow_call_seconds: float = 0.0,
        label: str = "http-srv",
        canary_token: str = "",
        call_started_event: threading.Event | None = None,
        redirect_to_url: str | None = None,
        trickle_mode: str | None = None,
        malformed_structured_content: Any = _UNSET,
        fix_expr: str = "a + b",
        resource_uri: str | None = None,
    ) -> None:
        self.host = host
        self.sse_mode = sse_mode
        self.byte_chunk_sse = byte_chunk_sse
        self.downgrade_get_405 = downgrade_get_405
        self.downgrade_delete_405 = downgrade_delete_405
        self.expire_session_404_on_call = expire_session_404_on_call
        self.fail_tool_jsonrpc_err = fail_tool_jsonrpc_err
        self.oversized_call = oversized_call
        self.slow_call_seconds = slow_call_seconds
        self.label = label
        self.canary_token = canary_token
        self.call_started_event = call_started_event
        self.redirect_to_url = redirect_to_url
        self.trickle_mode = trickle_mode
        self.malformed_structured_content = malformed_structured_content
        self.fix_expr = fix_expr
        self.resource_uri = resource_uri

        self.session_id = f"mcp-sess-{uuid.uuid4().hex[:8]}"
        self.requests_log: list[dict[str, Any]] = []
        self.methods: list[str] = []
        self.call_count = 0
        self.get_count = 0
        self.delete_count = 0
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.port: int = 0

    @property
    def url(self) -> str:
        host_fmt = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host_fmt}:{self.port}/mcp"

    def __enter__(self) -> Self:
        parent = self

        class _IPv6Server(ThreadingHTTPServer):
            address_family = socket.AF_INET6

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                _ = format, args

            def do_GET(self) -> None:
                with parent._lock:
                    parent.get_count += 1
                    parent.requests_log.append({"method": "GET", "headers": dict(self.headers)})
                if parent.downgrade_get_405:
                    self.send_response(405)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.end_headers()

            def do_DELETE(self) -> None:
                with parent._lock:
                    parent.delete_count += 1
                    parent.requests_log.append({"method": "DELETE", "headers": dict(self.headers)})
                code = 405 if parent.downgrade_delete_405 else 200
                self.send_response(code)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or "0")
                raw = self.rfile.read(length) if length > 0 else b""
                msg = json.loads(raw.decode("utf-8"))
                m = msg.get("method", "")
                req_id = msg.get("id")
                params = msg.get("params") or {}
                with parent._lock:
                    parent.methods.append(m)
                    parent.requests_log.append(
                        {"method": "POST", "rpc_method": m, "id": req_id, "headers": dict(self.headers)}
                    )

                if m == "notifications/initialized" or m == "notifications/cancelled":
                    self.send_response(202)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                if m == "tools/call":
                    with parent._lock:
                        parent.call_count += 1
                    if parent.call_started_event is not None:
                        parent.call_started_event.set()
                    if parent.redirect_to_url:
                        self.send_response(302)
                        self.send_header("Location", parent.redirect_to_url)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    if parent.expire_session_404_on_call:
                        self.send_response(404)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    if parent.trickle_mode == "json_whitespace":
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        try:
                            for _ in range(50):
                                self.wfile.write(b" ")
                                self.wfile.flush()
                                time.sleep(0.02)
                        except OSError:
                            return
                        return
                    if parent.trickle_mode == "sse_comments":
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        try:
                            for _ in range(50):
                                self.wfile.write(b": keepalive\n\n")
                                self.wfile.flush()
                                time.sleep(0.02)
                        except OSError:
                            return
                        return
                    if parent.slow_call_seconds > 0:
                        time.sleep(parent.slow_call_seconds)

                if m == "initialize":
                    resp_obj = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "protocolVersion": "2025-11-25",
                            "capabilities": {"tools": {"listChanged": False}},
                            "serverInfo": {"name": parent.label, "version": "1.0.0"},
                        },
                    }
                    extra_headers = {"Mcp-Session-Id": parent.session_id}
                elif m == "tools/list":
                    resp_obj = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "tools": [
                                {
                                    "name": "echo_tool",
                                    "description": f"{parent.label} echo",
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {"msg": {"type": "string"}},
                                        "required": ["msg"],
                                        "additionalProperties": False,
                                    },
                                },
                                {
                                    "name": "calc_hint",
                                    "description": "HTTP fix hint",
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {"target": {"type": "string"}},
                                        "required": ["target"],
                                        "additionalProperties": False,
                                    },
                                },
                                {
                                    "name": "bulk_tool",
                                    "description": "Echoes large string argument",
                                    "inputSchema": {
                                        "type": "object",
                                        "properties": {"data": {"type": "string"}},
                                        "required": ["data"],
                                        "additionalProperties": False,
                                    },
                                },
                            ]
                        },
                    }
                    extra_headers = {}
                elif m == "tools/call":
                    tname = params.get("name")
                    targs = params.get("arguments") or {}
                    extra_headers = {}
                    if parent.fail_tool_jsonrpc_err:
                        resp_obj = {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "error": {"code": -32001, "message": "remote call failed"},
                        }
                    elif parent.oversized_call:
                        resp_obj = {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {"content": [{"type": "text", "text": "Z" * (260 * 1024)}], "isError": False},
                        }
                    elif parent.malformed_structured_content is not _UNSET:
                        resp_obj = {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {
                                "content": [{"type": "text", "text": "has malformed structuredContent"}],
                                "structuredContent": parent.malformed_structured_content,
                                "isError": False,
                            },
                        }
                    elif tname == "calc_hint":
                        content_items: list[dict[str, Any]] = [
                            {"type": "text", "text": f"{parent.label}: return {parent.fix_expr}"}
                        ]
                        if parent.resource_uri:
                            content_items.append(
                                {"type": "resource_link", "uri": parent.resource_uri, "name": "ref"}
                            )
                        resp_obj = {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {
                                "content": content_items,
                                "isError": False,
                            },
                        }
                    else:
                        txt = f"{parent.label}: 中文🚀 {json.dumps(targs, ensure_ascii=False)}"
                        content_items = [{"type": "text", "text": txt}]
                        if parent.resource_uri:
                            content_items.append(
                                {"type": "resource_link", "uri": parent.resource_uri, "name": "ref"}
                            )
                        resp_obj = {
                            "jsonrpc": "2.0",
                            "id": req_id,
                            "result": {
                                "content": content_items,
                                "structuredContent": {"label": parent.label, "args": targs},
                                "isError": False,
                            },
                        }
                else:
                    resp_obj = {"jsonrpc": "2.0", "id": req_id, "result": {}}
                    extra_headers = {}

                if parent.sse_mode:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")
                    for hk, hv in extra_headers.items():
                        self.send_header(hk, hv)
                    self.send_header("Connection", "close")
                    self.end_headers()

                    frames: list[bytes] = []
                    # Inject non-matching id notification & response before matched response!
                    unmatched = json.dumps({"jsonrpc": "2.0", "id": "wrong-id-999", "result": {"content": [{"type": "text", "text": "WRONG"}], "isError": False}})
                    frames.append(f"event: message\ndata: {unmatched}\n\n".encode())
                    matched_json = json.dumps(resp_obj, ensure_ascii=False, indent=2)
                    data_lines = "".join(f"data: {ln}\n" for ln in matched_json.splitlines())
                    frames.append(f"event: message\n{data_lines}\n".encode())
                    # Trailing duplicate with same req_id must not leak into another call
                    dup_json = json.dumps({"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": "DUP"}], "isError": False}})
                    frames.append(f"data: {dup_json}\n\n".encode())
                    payload = b"".join(frames)
                    if parent.byte_chunk_sse:
                        for i in range(0, len(payload), 1):
                            self.wfile.write(payload[i : i + 1])
                            self.wfile.flush()
                    else:
                        self.wfile.write(payload)
                        self.wfile.flush()
                else:
                    body = json.dumps(resp_obj, ensure_ascii=False).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    for hk, hv in extra_headers.items():
                        self.send_header(hk, hv)
                    self.end_headers()
                    self.wfile.write(body)
                    self.wfile.flush()

        cls = _IPv6Server if ":" in self.host else ThreadingHTTPServer
        self._server = cls((self.host, 0), _Handler)
        self.port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
