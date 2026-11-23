"""Bounded stdio and loopback Streamable HTTP transports for MCP 2025-11-25."""

from __future__ import annotations

import asyncio
import codecs
import contextlib
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from forecontract_app.extension_models import (
    MAX_JSONRPC_FRAME_BYTES,
    MCP_STOP_TIMEOUT_SECONDS,
    ExtensionCleanup,
    ExtensionError,
    HttpMcpServerConfig,
    McpServerConfig,
    StdioMcpServerConfig,
    validate_loopback_http_url,
    validate_no_nonfinite_numbers,
)
from forecontract_app.mcp_process import McpFrameTooLargeSentinel, McpJobProcess


def _jsonrpc_id_key(req_id: Any) -> tuple[type, str | int]:
    if isinstance(req_id, bool) or not isinstance(req_id, (str, int)):
        raise ExtensionError("MCP_PROTOCOL_ERROR", "JSON-RPC id must be str or int", status_code=502)
    return (type(req_id), req_id)


def _reject_nonfinite(tok: str) -> Any:
    raise ValueError(f"non-finite JSON constant: {tok}")


def parse_jsonrpc_frame(raw: bytes) -> dict[str, Any]:
    """Parse and size-check a single JSON-RPC 2.0 UTF-8 object frame."""
    if len(raw) > MAX_JSONRPC_FRAME_BYTES:
        raise ExtensionError(
            "MCP_FRAME_TOO_LARGE",
            f"JSON-RPC frame exceeds {MAX_JSONRPC_FRAME_BYTES} bytes",
            status_code=502,
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExtensionError("MCP_PROTOCOL_ERROR", f"invalid UTF-8 frame: {exc}", status_code=502) from exc
    if "\x00" in text:
        raise ExtensionError("MCP_PROTOCOL_ERROR", "JSON-RPC frame contains NUL byte", status_code=502)
    try:
        obj = json.loads(text, parse_constant=_reject_nonfinite)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ExtensionError("MCP_PROTOCOL_ERROR", f"invalid JSON-RPC frame: {exc}", status_code=502) from exc
    if not isinstance(obj, dict) or obj.get("jsonrpc") != "2.0":
        raise ExtensionError("MCP_PROTOCOL_ERROR", "frame must be JSON-RPC 2.0 object", status_code=502)
    validate_no_nonfinite_numbers(obj)
    return obj


def encode_jsonrpc_message(obj: dict[str, Any]) -> bytes:
    """Serialize outgoing JSON-RPC 2.0 message within the 256 KiB limit."""
    validate_no_nonfinite_numbers(obj)
    try:
        raw = json.dumps(obj, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
            "utf-8"
        )
    except (TypeError, ValueError) as exc:
        raise ExtensionError("MCP_INVALID_ARGS", f"cannot encode JSON-RPC payload: {exc}", status_code=400) from exc
    if len(raw) > MAX_JSONRPC_FRAME_BYTES:
        raise ExtensionError(
            "MCP_FRAME_TOO_LARGE",
            f"outgoing JSON-RPC payload exceeds {MAX_JSONRPC_FRAME_BYTES} bytes",
            status_code=400,
        )
    return raw


class McpJsonRpcError(ExtensionError):
    """Structured error carrying remote JSON-RPC error code and message."""

    def __init__(self, rpc_code: int, rpc_message: str) -> None:
        self.rpc_code = rpc_code
        self.rpc_message = rpc_message
        super().__init__(
            "MCP_JSONRPC_ERROR",
            f"MCP JSON-RPC error {rpc_code}: {rpc_message}",
            status_code=502,
        )


NotificationCallback = Callable[[str, dict[str, Any] | None], None]
ServerRequestCallback = Callable[[str | int, str, dict[str, Any] | None], None]


class StdioMcpTransport:
    """Newline-delimited UTF-8 JSON-RPC 2.0 transport over ``McpJobProcess``."""

    def __init__(
        self,
        *,
        server: StdioMcpServerConfig,
        scratch_root: Path,
        on_notification: NotificationCallback | None = None,
        on_server_request: ServerRequestCallback | None = None,
    ) -> None:
        self._server = server
        self._proc = McpJobProcess(server=server, scratch_root=scratch_root)
        self._on_notification = on_notification
        self._on_server_request = on_server_request
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[tuple[type, str | int], asyncio.Future[dict[str, Any]]] = {}
        self._seen_response_keys: set[tuple[type, str | int]] = set()
        self._fault_exc: ExtensionError | None = None
        self._closed = False
        self._active_readers = 0
        self._peak_readers = 0

    @property
    def session_id(self) -> str | None:
        return None

    @property
    def active_readers_peak(self) -> int:
        return self._peak_readers

    def stderr_text(self) -> str:
        return self._proc.stderr_text()

    async def start(self) -> None:
        self._proc.start()
        self._active_readers = 1
        self._peak_readers = 1
        self._reader_task = asyncio.create_task(self._read_loop())

    def _fail_all_pending(self, exc: ExtensionError) -> None:
        if self._fault_exc is None:
            self._fault_exc = exc
        for key, fut in list(self._pending.items()):
            self._pending.pop(key, None)
            if not fut.done():
                fut.set_exception(exc)

    async def _send_method_not_found(self, req_id: str | int) -> None:
        with contextlib.suppress(ExtensionError, OSError):
            payload = encode_jsonrpc_message(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": "Method not found"},
                }
            )
            await self._proc.write_line(payload)

    async def _read_loop(self) -> None:
        q = self._proc.stdout_queue
        try:
            while True:
                item = await q.get()
                if item is None:
                    self._fail_all_pending(
                        ExtensionError("MCP_TRANSPORT_EOF", "stdio server closed stdout", status_code=502)
                    )
                    return
                if isinstance(item, McpFrameTooLargeSentinel):
                    self._fail_all_pending(
                        ExtensionError(
                            "MCP_FRAME_TOO_LARGE",
                            f"stdio stdout line exceeded {MAX_JSONRPC_FRAME_BYTES} bytes",
                            status_code=502,
                        )
                    )
                    return
                try:
                    msg = parse_jsonrpc_frame(item)
                except ExtensionError as exc:
                    self._fail_all_pending(exc)
                    return

                has_method = isinstance(msg.get("method"), str)
                has_id = "id" in msg and msg.get("id") is not None
                if has_method and has_id:
                    srv_id = msg["id"]
                    if not isinstance(srv_id, bool) and isinstance(srv_id, (str, int)):
                        if self._on_server_request is not None:
                            self._on_server_request(
                                srv_id,
                                msg["method"],
                                msg.get("params") if isinstance(msg.get("params"), dict) else None,
                            )
                        await self._send_method_not_found(srv_id)
                    continue
                if has_method and not has_id:
                    if self._on_notification is not None:
                        params = msg.get("params")
                        self._on_notification(
                            msg["method"], params if isinstance(params, dict) else None
                        )
                    continue

                if "id" not in msg or msg["id"] is None:
                    self._fail_all_pending(
                        ExtensionError(
                            "MCP_PROTOCOL_ERROR",
                            "JSON-RPC response missing valid id",
                            status_code=502,
                        )
                    )
                    return
                try:
                    key = _jsonrpc_id_key(msg["id"])
                except ExtensionError as exc:
                    self._fail_all_pending(exc)
                    return

                if key in self._seen_response_keys:
                    # Ignore duplicate frames for already-resolved IDs.
                    continue
                fut = self._pending.pop(key, None)
                if fut is None:
                    # Unmatched or late ID is dropped without delivering to any active caller.
                    continue
                self._seen_response_keys.add(key)
                if not fut.done():
                    fut.set_result(msg)
        finally:
            self._active_readers = max(0, self._active_readers - 1)

    async def send_request(
        self,
        *,
        req_id: str | int,
        method: str,
        params: dict[str, Any] | None,
        timeout_seconds: float,
        protocol_version: str | None = None,
    ) -> dict[str, Any]:
        _ = protocol_version
        if self._closed:
            raise ExtensionError("MCP_TRANSPORT_CLOSED", "stdio transport is closed", status_code=409)
        if self._fault_exc is not None:
            raise self._fault_exc
        key = _jsonrpc_id_key(req_id)
        if key in self._pending or key in self._seen_response_keys:
            raise ExtensionError("MCP_DUPLICATE_REQUEST_ID", f"duplicate request id {req_id!r}", status_code=400)

        body: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            body["params"] = params
        raw = encode_jsonrpc_message(body)

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[key] = fut
        try:
            await self._proc.write_line(raw)
            msg = await asyncio.wait_for(fut, timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            self._pending.pop(key, None)
            raise ExtensionError(
                "MCP_TIMEOUT", f"MCP stdio request '{method}' timed out", status_code=504
            ) from exc
        except asyncio.CancelledError:
            self._pending.pop(key, None)
            raise
        except Exception:
            self._pending.pop(key, None)
            raise

        if "error" in msg and msg["error"] is not None:
            err = msg["error"]
            if not isinstance(err, dict) or not isinstance(err.get("code"), int) or isinstance(err.get("code"), bool) or not isinstance(err.get("message"), str):
                raise ExtensionError("MCP_PROTOCOL_ERROR", "malformed JSON-RPC error object", status_code=502)
            raise McpJsonRpcError(int(err["code"]), err["message"])
        if "result" not in msg or not isinstance(msg["result"], dict):
            raise ExtensionError("MCP_PROTOCOL_ERROR", "JSON-RPC response missing object result", status_code=502)
        return msg["result"]

    async def send_notification(
        self,
        *,
        method: str,
        params: dict[str, Any] | None = None,
        timeout_seconds: float = 5.0,
        protocol_version: str | None = None,
    ) -> None:
        _ = timeout_seconds, protocol_version
        if self._closed or self._fault_exc is not None:
            return
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        raw = encode_jsonrpc_message(body)
        await self._proc.write_line(raw)

    async def start_optional_get_stream(
        self, *, protocol_version: str | None = None, timeout_seconds: float = 1.0
    ) -> bool:
        _ = protocol_version, timeout_seconds
        return False

    async def aclose(self, *, timeout_seconds: float = MCP_STOP_TIMEOUT_SECONDS) -> ExtensionCleanup:
        if self._closed:
            return await self._proc.close(timeout_seconds=timeout_seconds)
        self._closed = True
        deadline = time.monotonic() + max(0.2, float(timeout_seconds))
        self._fail_all_pending(
            ExtensionError("MCP_TRANSPORT_CLOSED", "stdio transport closed", status_code=409)
        )
        rem_for_proc = max(0.15, deadline - time.monotonic())
        cleanup = await self._proc.close(timeout_seconds=rem_for_proc)
        if self._reader_task is not None and not self._reader_task.done():
            self._reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError, ExtensionError, OSError):
                await asyncio.wait_for(
                    self._reader_task, timeout=max(0.05, deadline - time.monotonic())
                )
        return cleanup


class HttpMcpTransport:
    """Loopback Streamable HTTP (MCP 2025-11-25) transport with chunked SSE & single reader."""

    def __init__(
        self,
        *,
        server: HttpMcpServerConfig,
        on_notification: NotificationCallback | None = None,
        on_server_request: ServerRequestCallback | None = None,
    ) -> None:
        validate_loopback_http_url(server.url)
        self._server = server
        self._on_notification = on_notification
        self._on_server_request = on_server_request
        self._client: httpx.AsyncClient | None = None
        self._session_id: str | None = None
        self._session_invalidated = False
        self._get_downgraded = False
        self._delete_downgraded = False
        self._closed = False
        self._reader_lock = asyncio.Lock()
        self._active_readers = 0
        self._peak_readers = 0
        self._active_stream_tasks: set[asyncio.Task[Any]] = set()

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def session_invalidated(self) -> bool:
        return self._session_invalidated

    @property
    def get_stream_downgraded(self) -> bool:
        return self._get_downgraded

    @property
    def delete_downgraded(self) -> bool:
        return self._delete_downgraded

    @property
    def active_readers_peak(self) -> int:
        return self._peak_readers

    @property
    def active_readers_current(self) -> int:
        return self._active_readers

    def stderr_text(self) -> str:
        return ""

    async def start(self) -> None:
        parsed = urlsplit(self._server.url)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "::1"):
            raise ExtensionError("FORBIDDEN_URL", "HTTP MCP server must be literal loopback", status_code=403)
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
        )

    def _build_headers(
        self,
        *,
        accept: str,
        protocol_version: str | None,
        content_type: str | None = "application/json",
    ) -> dict[str, str]:
        hdrs: dict[str, str] = {"Accept": accept}
        if content_type is not None:
            hdrs["Content-Type"] = content_type
        if self._session_id is not None:
            hdrs["Mcp-Session-Id"] = self._session_id
        if protocol_version is not None:
            hdrs["MCP-Protocol-Version"] = protocol_version
        return hdrs

    def _capture_session_header(self, resp: httpx.Response) -> None:
        sid = resp.headers.get("mcp-session-id")
        if sid is not None:
            cleaned = sid.strip()
            if cleaned and "\x00" not in cleaned and "\r" not in cleaned and "\n" not in cleaned:
                self._session_id = cleaned

    def _enter_reader(self) -> None:
        self._active_readers += 1
        self._peak_readers = max(self._peak_readers, self._active_readers)

    def _exit_reader(self) -> None:
        self._active_readers = max(0, self._active_readers - 1)

    async def _send_error_post(
        self, *, req_id: str | int, protocol_version: str | None
    ) -> None:
        if self._client is None or self._closed:
            return
        with contextlib.suppress(ExtensionError, httpx.HTTPError, TimeoutError, OSError):
            raw = encode_jsonrpc_message(
                {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": "Method not found"},
                }
            )
            await self._client.post(
                self._server.url,
                content=raw,
                headers=self._build_headers(
                    accept="application/json, text/event-stream",
                    protocol_version=protocol_version,
                ),
                timeout=2.0,
            )

    async def _read_bounded_body(self, resp: httpx.Response) -> bytes:
        buf = bytearray()
        async for chunk in resp.aiter_bytes():
            buf.extend(chunk)
            if len(buf) > MAX_JSONRPC_FRAME_BYTES:
                raise ExtensionError(
                    "MCP_FRAME_TOO_LARGE",
                    f"HTTP JSON body exceeds {MAX_JSONRPC_FRAME_BYTES} bytes",
                    status_code=502,
                )
        return bytes(buf)

    async def _consume_sse_for_request(
        self,
        *,
        resp: httpx.Response,
        target_key: tuple[type, str | int],
        protocol_version: str | None,
    ) -> dict[str, Any]:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
        text_buf = ""
        event_name = ""
        data_lines: list[str] = []
        event_data_bytes = 0
        matched_response: dict[str, Any] | None = None

        async def _dispatch_sse_event() -> dict[str, Any] | None:
            nonlocal event_name, data_lines, event_data_bytes, matched_response
            if not data_lines:
                event_name = ""
                event_data_bytes = 0
                return None
            payload_str = "\n".join(data_lines)
            cur_event = event_name
            event_name = ""
            data_lines = []
            event_data_bytes = 0
            if cur_event and cur_event != "message":
                return None
            if not payload_str.strip():
                return None
            msg = parse_jsonrpc_frame(payload_str.encode("utf-8"))
            has_method = isinstance(msg.get("method"), str)
            has_id = "id" in msg and msg.get("id") is not None
            if has_method and has_id:
                srv_id = msg["id"]
                if not isinstance(srv_id, bool) and isinstance(srv_id, (str, int)):
                    if self._on_server_request is not None:
                        self._on_server_request(
                            srv_id,
                            msg["method"],
                            msg.get("params") if isinstance(msg.get("params"), dict) else None,
                        )
                    await self._send_error_post(req_id=srv_id, protocol_version=protocol_version)
                return None
            if has_method and not has_id:
                if self._on_notification is not None:
                    p = msg.get("params")
                    self._on_notification(msg["method"], p if isinstance(p, dict) else None)
                return None
            if "id" not in msg or msg["id"] is None:
                raise ExtensionError("MCP_PROTOCOL_ERROR", "SSE JSON-RPC response missing id", status_code=502)
            key = _jsonrpc_id_key(msg["id"])
            if key == target_key and matched_response is None:
                matched_response = msg
                return msg
            # Ignore non-matching IDs or duplicate response frames cleanly.
            return None

        async for chunk in resp.aiter_bytes():
            try:
                decoded = decoder.decode(chunk, final=False)
            except UnicodeDecodeError as exc:
                raise ExtensionError(
                    "MCP_PROTOCOL_ERROR", f"invalid UTF-8 in SSE stream: {exc}", status_code=502
                ) from exc
            text_buf += decoded
            if len(text_buf.encode("utf-8")) > MAX_JSONRPC_FRAME_BYTES * 2:
                raise ExtensionError("MCP_FRAME_TOO_LARGE", "SSE line buffer exceeded limit", status_code=502)
            while True:
                idx_n = text_buf.find("\n")
                idx_r = text_buf.find("\r")
                if idx_n == -1 and idx_r == -1:
                    break
                if idx_r != -1 and (idx_n == -1 or idx_r < idx_n):
                    line = text_buf[:idx_r]
                    if idx_r + 1 < len(text_buf) and text_buf[idx_r + 1] == "\n":
                        text_buf = text_buf[idx_r + 2 :]
                    else:
                        text_buf = text_buf[idx_r + 1 :]
                else:
                    line = text_buf[:idx_n]
                    text_buf = text_buf[idx_n + 1 :]

                if line == "":
                    found = await _dispatch_sse_event()
                    if found is not None:
                        return found
                    continue
                if line.startswith(":"):
                    continue
                if ":" in line:
                    field_name, raw_val = line.split(":", 1)
                    val = raw_val.removeprefix(" ")
                else:
                    field_name, val = line, ""
                if field_name == "event":
                    event_name = val
                elif field_name == "data":
                    event_data_bytes += len(val.encode("utf-8")) + 1
                    if event_data_bytes > MAX_JSONRPC_FRAME_BYTES:
                        raise ExtensionError(
                            "MCP_FRAME_TOO_LARGE",
                            f"SSE event exceeds {MAX_JSONRPC_FRAME_BYTES} bytes",
                            status_code=502,
                        )
                    data_lines.append(val)

        # Flush final decoder state and any trailing SSE block without blank terminator.
        try:
            text_buf += decoder.decode(b"", final=True)
        except UnicodeDecodeError as exc:
            raise ExtensionError(
                "MCP_PROTOCOL_ERROR", f"incomplete UTF-8 at SSE EOF: {exc}", status_code=502
            ) from exc
        if text_buf:
            for rem_line in text_buf.splitlines():
                if rem_line.startswith(":"):
                    continue
                if ":" in rem_line:
                    fn, rv = rem_line.split(":", 1)
                    v = rv.removeprefix(" ")
                else:
                    fn, v = rem_line, ""
                if fn == "event":
                    event_name = v
                elif fn == "data":
                    data_lines.append(v)
        found = await _dispatch_sse_event()
        if found is not None:
            return found
        raise ExtensionError("MCP_STREAM_ENDED", "SSE stream ended without matching JSON-RPC response", status_code=502)

    async def send_request(
        self,
        *,
        req_id: str | int,
        method: str,
        params: dict[str, Any] | None,
        timeout_seconds: float,
        protocol_version: str | None = None,
    ) -> dict[str, Any]:
        if self._closed or self._client is None:
            raise ExtensionError("MCP_TRANSPORT_CLOSED", "HTTP transport is closed", status_code=409)
        if self._session_invalidated:
            raise ExtensionError("MCP_SESSION_EXPIRED", "HTTP MCP session was invalidated (404)", status_code=409)

        target_key = _jsonrpc_id_key(req_id)
        body: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            body["params"] = params
        raw = encode_jsonrpc_message(body)

        try:
            async with asyncio.timeout(max(0.001, float(timeout_seconds))):
                async with self._reader_lock:
                    self._enter_reader()
                    try:
                        async with self._client.stream(
                            "POST",
                            self._server.url,
                            content=raw,
                            headers=self._build_headers(
                                accept="application/json, text/event-stream",
                                protocol_version=protocol_version,
                            ),
                            timeout=timeout_seconds,
                        ) as resp:
                            if 300 <= resp.status_code < 400:
                                raise ExtensionError(
                                    "HTTP_REDIRECT_FORBIDDEN",
                                    f"HTTP redirect {resp.status_code} forbidden",
                                    status_code=502,
                                )
                            if resp.status_code == 404:
                                self._session_invalidated = True
                                self._session_id = None
                                raise ExtensionError(
                                    "MCP_SESSION_EXPIRED",
                                    "HTTP server returned 404; session invalidated without retry",
                                    status_code=409,
                                )
                            if resp.status_code != 200:
                                raise ExtensionError(
                                    "MCP_HTTP_ERROR",
                                    f"HTTP request failed with status {resp.status_code}",
                                    status_code=502,
                                )
                            if method == "initialize":
                                self._capture_session_header(resp)

                            ct = (resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
                            if ct == "application/json":
                                resp_bytes = await self._read_bounded_body(resp)
                                msg = parse_jsonrpc_frame(resp_bytes)
                                if "id" not in msg or _jsonrpc_id_key(msg["id"]) != target_key:
                                    raise ExtensionError(
                                        "MCP_PROTOCOL_ERROR",
                                        "HTTP JSON response id did not match request",
                                        status_code=502,
                                    )
                            elif ct == "text/event-stream":
                                msg = await self._consume_sse_for_request(
                                    resp=resp,
                                    target_key=target_key,
                                    protocol_version=protocol_version,
                                )
                            else:
                                raise ExtensionError(
                                    "MCP_PROTOCOL_ERROR",
                                    f"unsupported HTTP response Content-Type: {ct!r}",
                                    status_code=502,
                                )
                    finally:
                        self._exit_reader()
        except (TimeoutError, asyncio.TimeoutError, httpx.TimeoutException) as exc:
            raise ExtensionError(
                "MCP_TIMEOUT", f"HTTP MCP request '{method}' timed out", status_code=504
            ) from exc
        except httpx.HTTPError as exc:
            raise ExtensionError(
                "MCP_HTTP_ERROR", f"HTTP transport error: {exc}", status_code=502
            ) from exc

        if "error" in msg and msg["error"] is not None:
            err = msg["error"]
            if (
                not isinstance(err, dict)
                or not isinstance(err.get("code"), int)
                or isinstance(err.get("code"), bool)
                or not isinstance(err.get("message"), str)
            ):
                raise ExtensionError("MCP_PROTOCOL_ERROR", "malformed JSON-RPC error object", status_code=502)
            raise McpJsonRpcError(int(err["code"]), err["message"])
        if "result" not in msg or not isinstance(msg["result"], dict):
            raise ExtensionError("MCP_PROTOCOL_ERROR", "JSON-RPC response missing object result", status_code=502)
        return msg["result"]

    async def send_notification(
        self,
        *,
        method: str,
        params: dict[str, Any] | None = None,
        timeout_seconds: float = 5.0,
        protocol_version: str | None = None,
    ) -> None:
        if self._closed or self._client is None or self._session_invalidated:
            return
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        raw = encode_jsonrpc_message(body)
        try:
            resp = await self._client.post(
                self._server.url,
                content=raw,
                headers=self._build_headers(
                    accept="application/json, text/event-stream",
                    protocol_version=protocol_version,
                ),
                timeout=timeout_seconds,
            )
            if 300 <= resp.status_code < 400:
                raise ExtensionError("HTTP_REDIRECT_FORBIDDEN", "redirect forbidden", status_code=502)
            if resp.status_code == 404:
                self._session_invalidated = True
                self._session_id = None
                raise ExtensionError("MCP_SESSION_EXPIRED", "HTTP 404 invalidated session", status_code=409)
            if resp.status_code not in (200, 202, 204):
                raise ExtensionError(
                    "MCP_HTTP_ERROR",
                    f"HTTP notification failed with status {resp.status_code}",
                    status_code=502,
                )
        except httpx.TimeoutException as exc:
            raise ExtensionError("MCP_TIMEOUT", f"notification '{method}' timed out", status_code=504) from exc
        except httpx.HTTPError as exc:
            raise ExtensionError("MCP_HTTP_ERROR", f"HTTP notification failed: {exc}", status_code=502) from exc

    async def start_optional_get_stream(
        self, *, protocol_version: str | None = None, timeout_seconds: float = 1.0
    ) -> bool:
        """Probe optional Streamable HTTP GET SSE endpoint; downgrade cleanly on 405."""
        if self._closed or self._client is None or self._session_invalidated:
            return False
        async with self._reader_lock:
            self._enter_reader()
            try:
                async with self._client.stream(
                    "GET",
                    self._server.url,
                    headers=self._build_headers(
                        accept="text/event-stream",
                        protocol_version=protocol_version,
                        content_type=None,
                    ),
                    timeout=timeout_seconds,
                ) as resp:
                    if 300 <= resp.status_code < 400:
                        raise ExtensionError("HTTP_REDIRECT_FORBIDDEN", "GET redirect forbidden", status_code=502)
                    if resp.status_code == 405:
                        self._get_downgraded = True
                        return False
                    if resp.status_code == 404:
                        self._session_invalidated = True
                        self._session_id = None
                        return False
                    return resp.status_code == 200
            except httpx.TimeoutException:
                return True
            except httpx.HTTPError:
                return False
            finally:
                self._exit_reader()

    async def aclose(self, *, timeout_seconds: float = MCP_STOP_TIMEOUT_SECONDS) -> ExtensionCleanup:
        if self._closed:
            return ExtensionCleanup(
                cleanup_ok=(self._active_readers == 0),
                remaining_active=self._active_readers,
                details={
                    "transport": "http",
                    "server_id": self._server.id,
                    "delete_downgraded": self._delete_downgraded,
                },
            )
        self._closed = True
        deadline = time.monotonic() + max(0.2, float(timeout_seconds))
        for task in list(self._active_stream_tasks):
            if not task.done():
                task.cancel()
        if self._active_stream_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._active_stream_tasks, return_exceptions=True),
                    timeout=max(0.05, deadline - time.monotonic()),
                )
            except asyncio.TimeoutError:
                pass

        if self._client is not None:
            rem = deadline - time.monotonic()
            if self._session_id is not None and not self._session_invalidated and rem > 0.1:
                with contextlib.suppress(httpx.HTTPError, TimeoutError, OSError):
                    del_resp = await asyncio.wait_for(
                        self._client.delete(
                            self._server.url,
                            headers=self._build_headers(
                                accept="application/json",
                                protocol_version="2025-11-25",
                                content_type=None,
                            ),
                            timeout=max(0.1, min(1.0, rem)),
                        ),
                        timeout=max(0.1, min(1.0, rem)),
                    )
                    if del_resp.status_code == 405:
                        self._delete_downgraded = True
            with contextlib.suppress(httpx.HTTPError, TimeoutError, OSError):
                await asyncio.wait_for(
                    self._client.aclose(),
                    timeout=max(0.05, deadline - time.monotonic()),
                )
            self._client = None

        return ExtensionCleanup(
            cleanup_ok=(self._active_readers == 0),
            remaining_active=self._active_readers,
            details={
                "transport": "http",
                "server_id": self._server.id,
                "get_downgraded": self._get_downgraded,
                "delete_downgraded": self._delete_downgraded,
            },
        )


McpTransport = StdioMcpTransport | HttpMcpTransport


def create_mcp_transport(
    *,
    server: McpServerConfig,
    scratch_root: Path,
    on_notification: NotificationCallback | None = None,
    on_server_request: ServerRequestCallback | None = None,
) -> McpTransport:
    """Create the appropriate bounded local MCP transport for ``server``."""
    if isinstance(server, StdioMcpServerConfig):
        return StdioMcpTransport(
            server=server,
            scratch_root=scratch_root,
            on_notification=on_notification,
            on_server_request=on_server_request,
        )
    return HttpMcpTransport(
        server=server,
        on_notification=on_notification,
        on_server_request=on_server_request,
    )
