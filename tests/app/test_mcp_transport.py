"""S07–S12: Stdio/HTTP MCP 2025-11-25 handshake, chunked SSE, schema identity, failures, and bounded Job teardown."""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest
from app_helpers import CREDENTIAL_ENV_CANARY, is_pid_alive
from extension_helpers import (
    LocalHttpMcpServer,
    read_stdio_log,
    write_stdio_mcp_script,
)
from forecontract_app.extension_models import (
    ExtensionError,
    HttpMcpServerConfig,
    StdioMcpServerConfig,
)
from forecontract_app.mcp_client import McpClient


def test_s07_stdio_handshake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", CREDENTIAL_ENV_CANARY)
    monkeypatch.setenv("OPENAI_API_KEY", CREDENTIAL_ENV_CANARY)

    log_ok = tmp_path / "logs" / "ok.jsonl"
    script_ok = write_stdio_mcp_script(tmp_path / "scripts", name="ok.py", log_path=log_ok, mode="stderr_diag")
    srv_ok = StdioMcpServerConfig(
        id="stdio-ok",
        transport="stdio",
        executable=sys.executable,
        argv=["-B", str(script_ok)],
        cwd=str(tmp_path),
    )

    async def _run_ok() -> None:
        client = McpClient(srv_ok, scratch_root=tmp_path / "scratch_ok")
        try:
            view = await client.connect()
            assert view.state == "connected"
            assert view.protocol_version == "2025-11-25"
            tools = await client.list_tools()
            assert any(t.name == "echo_tool" for t in tools)
            res = await client.call_tool(
                call_id="c-s07",
                tool_name="echo_tool",
                arguments={"msg": "hello"},
                timeout_ms=5000,
            )
            assert res.status == "success"
            assert not res.is_error
            assert "DIAG:" not in res.text
            assert "DIAG:" in client.view().stderr_excerpt
        finally:
            cleanup = await client.aclose()
            assert cleanup.cleanup_ok and cleanup.remaining_active == 0

    asyncio.run(_run_ok())

    entries = read_stdio_log(log_ok)
    started = [e for e in entries if e.get("event") == "started"]
    assert started and started[0]["canary_present"] is False
    assert all(k not in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY") for k in started[0]["env_keys"])
    methods = [e["method"] for e in entries if e.get("event") == "rpc"]
    assert methods[:3] == ["initialize", "notifications/initialized", "tools/list"]

    # Wrong protocolVersion must be rejected
    log_badv = tmp_path / "logs" / "badv.jsonl"
    script_badv = write_stdio_mcp_script(
        tmp_path / "scripts", name="badv.py", log_path=log_badv, protocol_version="2024-11-05"
    )
    srv_badv = StdioMcpServerConfig(
        id="stdio-badv",
        transport="stdio",
        executable=sys.executable,
        argv=["-B", str(script_badv)],
        cwd=str(tmp_path),
    )
    async def _run_badv() -> None:
        c = McpClient(srv_badv, scratch_root=tmp_path / "scratch_badv")
        with pytest.raises(ExtensionError) as exc:
            await c.connect()
        assert exc.value.code == "MCP_UNSUPPORTED_PROTOCOL_VERSION"
        await c.aclose()

    asyncio.run(_run_badv())

    # Stdout pollution fails protocol instead of masking as valid JSON-RPC
    log_pol = tmp_path / "logs" / "pol.jsonl"
    script_pol = write_stdio_mcp_script(
        tmp_path / "scripts", name="pol.py", log_path=log_pol, mode="stdout_pollute"
    )
    srv_pol = StdioMcpServerConfig(
        id="stdio-pol",
        transport="stdio",
        executable=sys.executable,
        argv=["-B", str(script_pol)],
        cwd=str(tmp_path),
    )
    async def _run_pol() -> None:
        c = McpClient(srv_pol, scratch_root=tmp_path / "scratch_pol")
        try:
            await c.connect()
            with pytest.raises(ExtensionError) as exc:
                await c.list_tools()
            assert exc.value.code == "MCP_PROTOCOL_ERROR"
        finally:
            await c.aclose()

    asyncio.run(_run_pol())

    # Server-initiated request returns -32601 Method not found cleanly without executing
    log_sreq = tmp_path / "logs" / "sreq.jsonl"
    script_sreq = write_stdio_mcp_script(
        tmp_path / "scripts", name="sreq.py", log_path=log_sreq, mode="server_request"
    )
    srv_sreq = StdioMcpServerConfig(
        id="stdio-sreq",
        transport="stdio",
        executable=sys.executable,
        argv=["-B", str(script_sreq)],
        cwd=str(tmp_path),
    )
    async def _run_sreq() -> None:
        c = McpClient(srv_sreq, scratch_root=tmp_path / "scratch_sreq")
        try:
            await c.connect()
            await asyncio.sleep(0.1)
            tools = await c.list_tools()
            assert len(tools) >= 1
        finally:
            await c.aclose()

    asyncio.run(_run_sreq())
    sreq_entries = read_stdio_log(log_sreq)
    rejected = [e for e in sreq_entries if e.get("event") == "host_response_to_server_req"]
    assert rejected and rejected[0]["error"]["code"] == -32601


def test_s08_http_json(tmp_path: Path) -> None:
    with LocalHttpMcpServer(sse_mode=False, downgrade_get_405=True, downgrade_delete_405=True) as http_srv:
        srv = HttpMcpServerConfig(id="http-json", transport="http", url=http_srv.url)

        async def _run() -> None:
            client = McpClient(srv, scratch_root=tmp_path / "scratch")
            try:
                view = await client.connect()
                assert view.session_id_present is True
                assert client.get_stream_downgraded is True
                tools = await client.list_tools()
                assert any(t.name == "echo_tool" for t in tools)
                res = await client.call_tool(
                    call_id="c-http-json",
                    tool_name="echo_tool",
                    arguments={"msg": "ok"},
                    timeout_ms=5000,
                )
                assert res.status == "success" and not res.is_error
            finally:
                cleanup = await client.aclose()
                assert cleanup.cleanup_ok and cleanup.remaining_active == 0
                assert client.delete_downgraded is True

        asyncio.run(_run())

        assert http_srv.methods[:3] == ["initialize", "notifications/initialized", "tools/list"]
        post_reqs = [r for r in http_srv.requests_log if r["method"] == "POST"]
        for r in post_reqs:
            hdrs = {k.lower(): v for k, v in r["headers"].items()}
            assert "application/json" in hdrs["accept"]
            assert "text/event-stream" in hdrs["accept"]
            if r["rpc_method"] != "initialize":
                assert hdrs.get("mcp-session-id") == http_srv.session_id
                assert hdrs.get("mcp-protocol-version") == "2025-11-25"
        assert http_srv.get_count == 1 and http_srv.delete_count == 1


def test_s09_http_sse(tmp_path: Path) -> None:
    with LocalHttpMcpServer(sse_mode=True, byte_chunk_sse=True, label="sse-byte") as http_srv:
        srv = HttpMcpServerConfig(id="http-sse", transport="http", url=http_srv.url)

        async def _run() -> None:
            client = McpClient(srv, scratch_root=tmp_path / "scratch")
            try:
                await client.connect()
                await client.list_tools()
                res1 = await client.call_tool(
                    call_id="c-sse-1",
                    tool_name="echo_tool",
                    arguments={"msg": "你好世界🌟"},
                    timeout_ms=10000,
                )
                res2 = await client.call_tool(
                    call_id="c-sse-2",
                    tool_name="echo_tool",
                    arguments={"msg": "second"},
                    timeout_ms=10000,
                )
                assert res1.status == "success" and "你好世界🌟" in res1.text
                assert "WRONG" not in res1.text and "DUP" not in res1.text
                assert res2.status == "success" and "second" in res2.text
                assert "WRONG" not in res2.text and "DUP" not in res2.text
                assert client.active_readers_peak <= 1
            finally:
                cleanup = await client.aclose()
                assert cleanup.cleanup_ok and cleanup.remaining_active == 0

        asyncio.run(_run())


def test_s10_discovery_identity(tmp_path: Path) -> None:
    log_a = tmp_path / "logs" / "a.jsonl"
    script_a = write_stdio_mcp_script(tmp_path / "scripts", name="a.py", log_path=log_a, label="alpha-srv")
    srv_a = StdioMcpServerConfig(
        id="srv-alpha", transport="stdio", executable=sys.executable, argv=["-B", str(script_a)], cwd=str(tmp_path)
    )

    with LocalHttpMcpServer(sse_mode=False, label="beta-http") as http_b:
        srv_b = HttpMcpServerConfig(id="srv-beta", transport="http", url=http_b.url)

        async def _run_dual() -> None:
            ca = McpClient(srv_a, scratch_root=tmp_path / "sa")
            cb = McpClient(srv_b, scratch_root=tmp_path / "sb")
            try:
                await ca.connect()
                await cb.connect()
                ta = await ca.list_tools()
                tb = await cb.list_tools()
                assert any(t.name == "echo_tool" and t.server_id == "srv-alpha" for t in ta)
                assert any(t.name == "echo_tool" and t.server_id == "srv-beta" for t in tb)

                ra = await ca.call_tool(call_id="ca", tool_name="echo_tool", arguments={"msg": "one"})
                rb = await cb.call_tool(call_id="cb", tool_name="echo_tool", arguments={"msg": "two"})
                assert "alpha-srv:" in ra.text
                assert "beta-http:" in rb.text

                # Unsupported schema ($ref) is marked unsupported and refused BEFORE wire send
                unsup = next(t for t in ta if t.name == "unsupported_ref_tool")
                assert unsup.supported is False and "unsupported_schema" in (unsup.unsupported_reason or "")
                with pytest.raises(ExtensionError) as exc_unsup:
                    await ca.call_tool(call_id="c-unsup", tool_name="unsupported_ref_tool", arguments={"x": "v"})
                assert exc_unsup.value.code == "MCP_UNSUPPORTED_SCHEMA"

                # Invalid arg type (bool as int) rejected BEFORE wire send
                with pytest.raises(ExtensionError) as exc_arg:
                    await ca.call_tool(call_id="c-bool", tool_name="echo_tool", arguments={"msg": "hi", "count": True})
                assert exc_arg.value.code == "MCP_INVALID_ARGS"
            finally:
                await ca.aclose()
                await cb.aclose()

        asyncio.run(_run_dual())

    # 128 tools across 16 pages succeeds; 129 tools or 17 pages fails with MCP_TOOL_LIMIT_EXCEEDED
    log_128 = tmp_path / "logs" / "paged_128_16.jsonl"
    sc_128 = write_stdio_mcp_script(
        tmp_path / "scripts", name="paged_128_16.py", log_path=log_128, mode="paged_128_16"
    )
    srv_128 = StdioMcpServerConfig(
        id="srv-128",
        transport="stdio",
        executable=sys.executable,
        argv=["-B", str(sc_128)],
        cwd=str(tmp_path),
    )

    async def _run_128() -> None:
        c128 = McpClient(srv_128, scratch_root=tmp_path / "sc_128")
        try:
            await c128.connect()
            tools_128 = await c128.list_tools()
            assert len(tools_128) == 128
            assert tools_128[0].name == "echo_tool_001"
            assert tools_128[-1].name == "echo_tool_128"
        finally:
            await c128.aclose()

    asyncio.run(_run_128())

    # Duplicate tool names, cursor loops, 129 tools, and 17 pages must fail cleanly
    for mode, expected_code in [
        ("dup_tools", "MCP_DUPLICATE_TOOL_NAME"),
        ("cursor_loop", "MCP_CURSOR_LOOP"),
        ("paged_129_16", "MCP_TOOL_LIMIT_EXCEEDED"),
        ("paged_128_17", "MCP_TOOL_LIMIT_EXCEEDED"),
    ]:
        log_m = tmp_path / "logs" / f"{mode}.jsonl"
        sc_m = write_stdio_mcp_script(tmp_path / "scripts", name=f"{mode}.py", log_path=log_m, mode=mode)
        srv_m = StdioMcpServerConfig(
            id=f"srv-{mode}", transport="stdio", executable=sys.executable, argv=["-B", str(sc_m)], cwd=str(tmp_path)
        )

        async def _run_err(
            s: StdioMcpServerConfig = srv_m,
            code: str = expected_code,
            m: str = mode,
        ) -> None:
            cm = McpClient(s, scratch_root=tmp_path / f"sc_{m}")
            try:
                await cm.connect()
                with pytest.raises(ExtensionError) as exc:
                    await cm.list_tools()
                assert exc.value.code == code
            finally:
                await cm.aclose()

        asyncio.run(_run_err())


def test_s11_protocol_failure(tmp_path: Path) -> None:
    # 1. stdio isError=True & >256KiB frame
    log_s = tmp_path / "logs" / "s11.jsonl"
    sc_s = write_stdio_mcp_script(tmp_path / "scripts", name="s11.py", log_path=log_s, mode="normal")
    srv_s = StdioMcpServerConfig(
        id="s11-stdio", transport="stdio", executable=sys.executable, argv=["-B", str(sc_s)], cwd=str(tmp_path)
    )
    async def _run_is_err() -> None:
        c = McpClient(srv_s, scratch_root=tmp_path / "sc1")
        try:
            await c.connect()
            await c.list_tools()
            failed_result = await c.call_tool(call_id="c-iserr", tool_name="fail_tool", arguments={"attempt": 1})
            assert failed_result.status != "success" and failed_result.is_error
            assert failed_result.call_id == "c-iserr"
        finally:
            await c.aclose()

    asyncio.run(_run_is_err())

    log_big = tmp_path / "logs" / "big.jsonl"
    sc_big = write_stdio_mcp_script(tmp_path / "scripts", name="big.py", log_path=log_big, mode="oversized_call")
    srv_big = StdioMcpServerConfig(
        id="s11-big", transport="stdio", executable=sys.executable, argv=["-B", str(sc_big)], cwd=str(tmp_path)
    )
    async def _run_big() -> None:
        c = McpClient(srv_big, scratch_root=tmp_path / "sc2")
        try:
            await c.connect()
            await c.list_tools()
            failed_result = await c.call_tool(call_id="c-big", tool_name="echo_tool", arguments={"msg": "big"})
            assert failed_result.status != "success" and failed_result.is_error
            assert failed_result.error_code == "MCP_FRAME_TOO_LARGE"
        finally:
            await c.aclose()

    asyncio.run(_run_big())

    # 2. HTTP JSON-RPC error + 404 session expiration without automatic retry or re-initialize
    with LocalHttpMcpServer(fail_tool_jsonrpc_err=True) as srv_err:
        srv = HttpMcpServerConfig(id="http-rpc-err", transport="http", url=srv_err.url)

        async def _run_rpc_err() -> None:
            c = McpClient(srv, scratch_root=tmp_path / "sc3")
            try:
                await c.connect()
                await c.list_tools()
                failed_result = await c.call_tool(call_id="c-rpc", tool_name="echo_tool", arguments={"msg": "x"})
                assert failed_result.status != "success" and failed_result.is_error
                assert failed_result.error_code == "MCP_JSONRPC_ERROR"
            finally:
                await c.aclose()

        asyncio.run(_run_rpc_err())
        assert srv_err.call_count == 1

    with LocalHttpMcpServer(expire_session_404_on_call=True) as srv_404:
        srv = HttpMcpServerConfig(id="http-404", transport="http", url=srv_404.url)

        async def _run_404() -> None:
            c = McpClient(srv, scratch_root=tmp_path / "sc4")
            try:
                await c.connect()
                await c.list_tools()
                failed_result = await c.call_tool(call_id="c-404", tool_name="echo_tool", arguments={"msg": "x"})
                assert failed_result.status == "unknown" and failed_result.is_error
                assert failed_result.effect == "unknown"
                assert failed_result.error_code == "MCP_SESSION_EXPIRED"
                # Subsequent attempt refuses cleanly without re-sending tools/call
                with pytest.raises(ExtensionError) as exc2:
                    await c.call_tool(call_id="c-404-b", tool_name="echo_tool", arguments={"msg": "x"})
                assert exc2.value.code == "MCP_SESSION_EXPIRED"
            finally:
                await c.aclose()

        asyncio.run(_run_404())
        assert srv_404.call_count == 1
        assert srv_404.methods.count("initialize") == 1

    # 3. F02: Non-object structuredContent (null / [] / "str" / 123 / true) rejected as MCP_PROTOCOL_ERROR
    for idx, bad_sc in enumerate([None, ["list"], "str", 123, True]):
        with LocalHttpMcpServer(malformed_structured_content=bad_sc) as srv_bad_sc:
            srv_sc = HttpMcpServerConfig(id=f"http-bad-sc-{idx}", transport="http", url=srv_bad_sc.url)

            async def _run_bad_sc(
                s: HttpMcpServerConfig = srv_sc,
                i: int = idx,
                srv_ref: object = srv_bad_sc,
            ) -> None:
                c = McpClient(s, scratch_root=tmp_path / f"sc_bad_{i}")
                try:
                    await c.connect()
                    await c.list_tools()
                    res = await c.call_tool(
                        call_id=f"c-bad-sc-{i}", tool_name="echo_tool", arguments={"msg": "x"}
                    )
                    assert res.status == "unknown"
                    assert res.is_error is True
                    assert res.effect == "unknown"
                    assert res.error_code == "MCP_PROTOCOL_ERROR"
                    assert res.structured_content is None
                    # Each malformed variant reaches tools/call exactly once (no automatic retry).
                    assert srv_ref.call_count == 1
                finally:
                    await c.aclose()

            asyncio.run(_run_bad_sc())

    # 4. F03: HTTP slow trickle (JSON whitespace & SSE keepalive comments) times out within total deadline
    for tmode in ("json_whitespace", "sse_comments"):
        import threading as _threading

        call_started_ev = _threading.Event()
        with LocalHttpMcpServer(trickle_mode=tmode, call_started_event=call_started_ev) as srv_trickle:
            srv_t = HttpMcpServerConfig(id=f"http-trickle-{tmode}", transport="http", url=srv_trickle.url)

            async def _run_trickle(
                s: HttpMcpServerConfig = srv_t,
                tm: str = tmode,
                started_ev: object = call_started_ev,
                srv_ref: object = srv_trickle,
            ) -> None:
                c = McpClient(s, scratch_root=tmp_path / f"sc_trickle_{tm}")
                try:
                    await c.connect()
                    await c.list_tools()
                    t0 = time.monotonic()
                    res = await c.call_tool(
                        call_id=f"c-trickle-{tm}",
                        tool_name="echo_tool",
                        arguments={"msg": "drip"},
                        timeout_ms=100,
                    )
                    elapsed = time.monotonic() - t0
                    assert res.status == "timeout"
                    assert res.is_error is True
                    assert res.effect == "unknown"
                    assert res.error_code == "MCP_TIMEOUT"
                    assert elapsed < 0.6
                    # Server-ready: the real server entered tools/call exactly once for this variant.
                    assert started_ev.is_set()
                    assert srv_ref.call_count == 1
                finally:
                    cleanup = await c.aclose()
                    assert cleanup.cleanup_ok and cleanup.remaining_active == 0

            asyncio.run(_run_trickle())

    # 5. S09/S11: HTTP 3xx redirect forbidden and redirect canary never contacted
    with (
        LocalHttpMcpServer(label="redirect-canary") as canary_srv,
        LocalHttpMcpServer(redirect_to_url=canary_srv.url) as redir_srv,
    ):
        srv_redir = HttpMcpServerConfig(id="http-redir", transport="http", url=redir_srv.url)

        async def _run_redir() -> None:
            c = McpClient(srv_redir, scratch_root=tmp_path / "sc_redir")
            try:
                await c.connect()
                await c.list_tools()
                res = await c.call_tool(call_id="c-redir", tool_name="echo_tool", arguments={"msg": "no-follow"})
                assert res.is_error is True
                assert res.error_code == "HTTP_REDIRECT_FORBIDDEN"
            finally:
                await c.aclose()

        asyncio.run(_run_redir())
        assert len(canary_srv.requests_log) == 0


def test_s12_bounded_shutdown(tmp_path: Path) -> None:
    import threading

    ready_file = tmp_path / "ready_child.json"
    log_hang = tmp_path / "logs" / "hang.jsonl"
    sc_hang = write_stdio_mcp_script(
        tmp_path / "scripts",
        name="hang.py",
        log_path=log_hang,
        mode="hang_with_child",
        ready_file=ready_file,
    )
    srv_hang = StdioMcpServerConfig(
        id="s12-hang", transport="stdio", executable=sys.executable, argv=["-B", str(sc_hang)], cwd=str(tmp_path)
    )

    async def _run_stdio_cancel() -> None:
        c = McpClient(srv_hang, scratch_root=tmp_path / "sc_hang")
        await c.connect()
        await c.list_tools()
        assert ready_file.exists()
        pids = json.loads(ready_file.read_text(encoding="utf-8"))
        call_task = asyncio.create_task(
            c.call_tool(call_id="c-hang", tool_name="echo_tool", arguments={"msg": "wait"}, timeout_ms=5000)
        )
        # Wait deterministically for child server to enter tools/call before cancelling
        deadline = time.monotonic() + 3.0
        call_started = False
        while time.monotonic() < deadline:
            data = json.loads(ready_file.read_text(encoding="utf-8"))
            if data.get("call_started") is True:
                call_started = True
                break
            await asyncio.sleep(0.01)
        assert call_started is True

        await c.cancel(call_id="c-hang")
        res = await call_task
        assert res.status == "cancelled" and res.is_error and res.effect == "unknown"

        t_close_start = time.monotonic()
        cleanup = await c.aclose()
        close_elapsed = time.monotonic() - t_close_start
        assert cleanup.remaining_active == 0 and cleanup.cleanup_ok
        assert close_elapsed <= 2.0
        assert not is_pid_alive(int(pids["parent_pid"]))
        assert not is_pid_alive(int(pids["child_pid"]))

    asyncio.run(_run_stdio_cancel())

    # Stdio natural timeout: the server is ready, then hangs on tools/call and the call times out on its own
    # (no cancel), with Job/PID/reader all closed to zero.
    async def _run_stdio_timeout() -> None:
        c = McpClient(srv_hang, scratch_root=tmp_path / "sc_hang_timeout")
        await c.connect()
        await c.list_tools()
        assert ready_file.exists()
        t0 = time.monotonic()
        res = await c.call_tool(call_id="c-hang-timeout", tool_name="echo_tool", arguments={"msg": "wait"}, timeout_ms=500)
        elapsed = time.monotonic() - t0
        assert elapsed < 2.0
        assert res.status == "timeout" and res.is_error and res.effect == "unknown"
        # This run's process actually entered tools/call before the natural timeout (fresh ready, not stale).
        pids_to = json.loads(ready_file.read_text(encoding="utf-8"))
        assert pids_to.get("call_started") is True
        # Timing covers the cleanup: aclose must stay within the 2s budget plus a 0.5s tolerance.
        t_close_start = time.monotonic()
        cleanup = await c.aclose()
        close_elapsed = time.monotonic() - t_close_start
        assert cleanup.cleanup_ok and cleanup.remaining_active == 0
        assert close_elapsed <= 2.5
        assert not is_pid_alive(int(pids_to["parent_pid"]))
        assert not is_pid_alive(int(pids_to["child_pid"]))

    asyncio.run(_run_stdio_timeout())

    # HTTP slow call cancelled after deterministic call_started_event -> notifications/cancelled sent, server effect remains unknown
    call_started_ev = threading.Event()
    with LocalHttpMcpServer(slow_call_seconds=1.5, call_started_event=call_started_ev) as http_slow:
        srv_h = HttpMcpServerConfig(id="s12-http", transport="http", url=http_slow.url)

        async def _run_http_cancel() -> None:
            c = McpClient(srv_h, scratch_root=tmp_path / "sc_hslow")
            await c.connect()
            await c.list_tools()
            task = asyncio.create_task(
                c.call_tool(call_id="c-hslow", tool_name="echo_tool", arguments={"msg": "slow"}, timeout_ms=10000)
            )
            started_ok = await asyncio.to_thread(call_started_ev.wait, 3.0)
            assert started_ok is True
            await c.cancel(call_id="c-hslow")
            res = await task
            assert res.status == "cancelled" and res.is_error and res.effect == "unknown"
            t_close = time.monotonic()
            cleanup = await c.aclose()
            assert cleanup.cleanup_ok and cleanup.remaining_active == 0
            assert (time.monotonic() - t_close) <= 2.0

        asyncio.run(_run_http_cancel())
        assert "notifications/cancelled" in http_slow.methods
