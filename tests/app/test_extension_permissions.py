"""S13–S18: Extension connect/call approvals, tamper rejection, run/action isolation, idempotency, cancellation, and config/loopback limits."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from extension_helpers import (
    LocalHttpMcpServer,
    read_stdio_log,
    write_stdio_mcp_script,
)
from forecontract.models.actions import ActionSpec
from forecontract_app.extension_models import (
    ExtensionConfig,
    ExtensionError,
    HttpMcpServerConfig,
    StdioMcpServerConfig,
    load_extension_config,
)
from forecontract_app.extension_runtime import ExtensionRuntime
from forecontract_app.models import PROJECT_NAME_PATTERN
from forecontract_app.project_models import PROJECT_SCHEMA_VERSION
from project_helpers import create_buggy_calc_project


def _make_runtime(
    tmp_path: Path,
    *,
    servers: list[Any],
    run_id: str = "run-1",
    ttl_seconds: float = 120.0,
    clock: Any = None,
) -> tuple[ExtensionRuntime, list[tuple[str, dict[str, Any]]]]:
    proj = create_buggy_calc_project(tmp_path / f"proj_{run_id}")
    cfg = ExtensionConfig(version=1, servers=servers)
    emitted: list[tuple[str, dict[str, Any]]] = []
    rt = ExtensionRuntime(
        project_id="proj-1",
        session_id="sess-1",
        run_id=run_id,
        project_root=proj,
        config=cfg,
        selected_skill_ids=(),
        scratch_root=tmp_path / f"scratch_{run_id}",
        emit=lambda k, p: emitted.append((k, p)),
        ttl_seconds=ttl_seconds,
        monotonic_clock=clock,
    )
    return rt, emitted


def _action(at: str, payload: dict[str, Any], action_id: str) -> ActionSpec:
    return ActionSpec(
        schema_version=PROJECT_SCHEMA_VERSION,
        action_type=at,
        payload=payload,
        metadata={"action_id": action_id},
    )


def test_s13_connect_approval(tmp_path: Path) -> None:
    log_s = tmp_path / "logs" / "s13.jsonl"
    sc_s = write_stdio_mcp_script(tmp_path / "scripts", name="s13.py", log_path=log_s)
    srv = StdioMcpServerConfig(
        id="srv-s13", transport="stdio", executable=sys.executable, argv=["-B", str(sc_s)], cwd=str(tmp_path)
    )
    rt, _ = _make_runtime(tmp_path, servers=[srv])

    async def _run() -> None:
        try:
            # 1. list_mcp_servers never spawns
            listed = await rt.execute(_action("list_mcp_servers", {}, "act-list"))
            assert listed["tool_succeeded"] is True
            assert len(read_stdio_log(log_s)) == 0

            # 2. Rejecting connect_mcp keeps spawn count == 0
            conn_task_rej = asyncio.create_task(
                rt.execute(_action("connect_mcp", {"server_id": "srv-s13"}, "act-conn-rej"))
            )
            while not [p for p in rt.list_proposals() if p.state == "pending"]:
                await asyncio.sleep(0.01)
            p_rej = rt.list_proposals()[-1]
            await rt.decide(proposal_id=p_rej.id, digest=p_rej.digest, decision="reject")
            res_rej = await conn_task_rej
            assert res_rej["tool_succeeded"] is False
            assert res_rej["effect_applied"] is False
            assert len(read_stdio_log(log_s)) == 0

            # 3. Approving exact connect_mcp spawns cleanly once
            conn_task_ok = asyncio.create_task(
                rt.execute(_action("connect_mcp", {"server_id": "srv-s13"}, "act-conn-ok"))
            )
            while not [p for p in rt.list_proposals() if p.state == "pending"]:
                await asyncio.sleep(0.01)
            p_ok = next(p for p in rt.list_proposals() if p.state == "pending")
            assert p_ok.trusted_local_execution is True
            assert p_ok.executable_sha256 == srv.executable_sha256
            await rt.decide(proposal_id=p_ok.id, digest=p_ok.digest, decision="approve")
            res_ok = await conn_task_ok
            assert res_ok["tool_succeeded"] is True
            started = [e for e in read_stdio_log(log_s) if e.get("event") == "started"]
            assert len(started) == 1
        finally:
            await rt.aclose()

    asyncio.run(_run())


def test_s14_exact_call(tmp_path: Path) -> None:
    # Check tampering args, schema, config digest, or executable bytes invalidates grant (0 calls sent)
    with LocalHttpMcpServer(label="s14-http") as http_srv:
        srv_h = HttpMcpServerConfig(id="srv-h14", transport="http", url=http_srv.url)
        rt, _ = _make_runtime(tmp_path, servers=[srv_h])

        async def _run_http() -> None:
            try:
                ct = asyncio.create_task(
                    rt.execute(_action("connect_mcp", {"server_id": "srv-h14"}, "act-c14"))
                )
                while not [p for p in rt.list_proposals() if p.state == "pending"]:
                    await asyncio.sleep(0.01)
                pc = rt.list_proposals()[-1]
                await rt.decide(proposal_id=pc.id, digest=pc.digest, decision="approve")
                assert (await ct)["tool_succeeded"] is True
                assert (await rt.execute(_action("list_mcp_tools", {"server_id": "srv-h14"}, "act-l14")))["tool_succeeded"] is True

                # Direct broker tamper checks before consuming call grant
                tool_info = rt._clients["srv-h14"].get_cached_tool("echo_tool")
                assert tool_info is not None
                prop = rt._broker.create_call_proposal(
                    action_id="act-tamper",
                    server=srv_h,
                    tool_name="echo_tool",
                    schema_sha256=tool_info.schema_sha256,
                    args_sha256="a" * 64,
                    arguments={"msg": "orig"},
                )
                rt._broker.decide(
                    proposal_id=prop.id,
                    request={"digest": prop.digest, "decision": "approve"},  # type: ignore[arg-type]
                )
                # Mutated args digest fails
                with pytest.raises(ExtensionError) as e_args:
                    rt._broker.consume_call_grant(
                        proposal_id=prop.id,
                        action_id="act-tamper",
                        server=srv_h,
                        current_config_sha256=rt._config.config_sha256,
                        tool_name="echo_tool",
                        schema_sha256=tool_info.schema_sha256,
                        args_sha256="b" * 64,
                    )
                assert e_args.value.code == "ARGS_MODIFIED"
                # Mutated schema digest fails
                with pytest.raises(ExtensionError) as e_schema:
                    rt._broker.consume_call_grant(
                        proposal_id=prop.id,
                        action_id="act-tamper",
                        server=srv_h,
                        current_config_sha256=rt._config.config_sha256,
                        tool_name="echo_tool",
                        schema_sha256="f" * 64,
                        args_sha256="a" * 64,
                    )
                assert e_schema.value.code == "SCHEMA_MODIFIED"
                # Mutated config digest fails
                with pytest.raises(ExtensionError) as e_cfg:
                    rt._broker.consume_call_grant(
                        proposal_id=prop.id,
                        action_id="act-tamper",
                        server=srv_h,
                        current_config_sha256="c" * 64,
                        tool_name="echo_tool",
                        schema_sha256=tool_info.schema_sha256,
                        args_sha256="a" * 64,
                    )
                assert e_cfg.value.code == "CONFIG_MODIFIED"
                assert http_srv.call_count == 0

                # Exact approved call executes once cleanly
                call_t = asyncio.create_task(
                    rt.execute(
                        _action(
                            "call_mcp",
                            {"server_id": "srv-h14", "tool_name": "echo_tool", "arguments": {"msg": "exact"}},
                            "act-call-exact",
                        )
                    )
                )
                while not [p for p in rt.list_proposals() if p.state == "pending"]:
                    await asyncio.sleep(0.01)
                pcall = next(p for p in rt.list_proposals() if p.state == "pending")
                await rt.decide(proposal_id=pcall.id, digest=pcall.digest, decision="approve")
                call_res = await call_t
                assert call_res["tool_succeeded"] is True
                assert call_res["args_sha256"] == pcall.args_sha256
                assert http_srv.call_count == 1
            finally:
                await rt.aclose()

        asyncio.run(_run_http())

    # Stdio binary swap check after approval (both broker-level and end-to-end runtime.execute)
    fake_exe = tmp_path / "dummy_mcp.exe"
    fake_exe.write_bytes(b"ORIGINAL_BINARY_BYTES")
    srv_exe = StdioMcpServerConfig(
        id="srv-exe", transport="stdio", executable=str(fake_exe), argv=[], cwd=str(tmp_path)
    )
    rt_exe, _ = _make_runtime(tmp_path, servers=[srv_exe], run_id="run-exe")
    p_conn = rt_exe._broker.create_connect_proposal(action_id="act-e", server=srv_exe)
    rt_exe._broker.decide(
        proposal_id=p_conn.id,
        request={"digest": p_conn.digest, "decision": "approve"},  # type: ignore[arg-type]
    )
    fake_exe.write_bytes(b"SWAPPED_EVIL_BYTES")
    with pytest.raises(ExtensionError) as exc_swap:
        rt_exe._broker.consume_connect_grant(
            proposal_id=p_conn.id,
            action_id="act-e",
            server=srv_exe,
            current_config_sha256=rt_exe._config.config_sha256,
        )
    assert exc_swap.value.code == "EXECUTABLE_MODIFIED"

    # End-to-end F01: modifying disk extension_config or executable while connect_mcp / call_mcp is pending
    with LocalHttpMcpServer(label="s14-f01-http") as f01_srv:
        # Case A: extension_config modified on disk while connect_mcp proposal is pending
        cfg_file_a = tmp_path / "f01_connect_cfg.json"
        cfg_file_a.write_text(
            json.dumps(
                {
                    "version": 1,
                    "servers": [{"id": "srv-f01-a", "transport": "http", "url": f01_srv.url}],
                }
            ),
            encoding="utf-8",
        )
        loaded_a = load_extension_config(path=cfg_file_a)
        proj_a = create_buggy_calc_project(tmp_path / "proj_f01_a")
        rt_a = ExtensionRuntime(
            project_id="proj-f01",
            session_id="sess-f01",
            run_id="run-f01-a",
            project_root=proj_a,
            config=loaded_a,
            selected_skill_ids=(),
            scratch_root=tmp_path / "scratch_f01_a",
            emit=lambda _k, _p: None,
        )

        async def _run_f01_connect() -> None:
            try:
                ct_a = asyncio.create_task(
                    rt_a.execute(_action("connect_mcp", {"server_id": "srv-f01-a"}, "act-f01-conn"))
                )
                while not [p for p in rt_a.list_proposals() if p.state == "pending"]:
                    await asyncio.sleep(0.01)
                prop_a = rt_a.list_proposals()[-1]
                # Mutate disk config file before approving
                cfg_file_a.write_text(
                    json.dumps(
                        {
                            "version": 1,
                            "servers": [
                                {"id": "srv-f01-a", "transport": "http", "url": "http://127.0.0.1:59999/mcp"}
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                await rt_a.decide(proposal_id=prop_a.id, digest=prop_a.digest, decision="approve")
                res_a = await ct_a
                assert res_a["tool_succeeded"] is False
                assert res_a["error_code"] == "CONFIG_MODIFIED"
                assert res_a["effect_applied"] is False
                assert len(f01_srv.requests_log) == 0
            finally:
                await rt_a.aclose()

        asyncio.run(_run_f01_connect())

        # Case D: extension_config file deleted on disk while connect_mcp proposal is pending
        cfg_file_d = tmp_path / "f01_delete_cfg.json"
        cfg_file_d.write_text(
            json.dumps(
                {
                    "version": 1,
                    "servers": [{"id": "srv-f01-d", "transport": "http", "url": f01_srv.url}],
                }
            ),
            encoding="utf-8",
        )
        loaded_d = load_extension_config(path=cfg_file_d)
        original_sha_d = loaded_d.config_sha256
        proj_d = create_buggy_calc_project(tmp_path / "proj_f01_d")
        rt_d = ExtensionRuntime(
            project_id="proj-f01",
            session_id="sess-f01",
            run_id="run-f01-d",
            project_root=proj_d,
            config=loaded_d,
            selected_skill_ids=(),
            scratch_root=tmp_path / "scratch_f01_d",
            emit=lambda _k, _p: None,
        )

        async def _run_f01_delete() -> None:
            try:
                ct_d = asyncio.create_task(
                    rt_d.execute(_action("connect_mcp", {"server_id": "srv-f01-d"}, "act-f01-del"))
                )
                while not [p for p in rt_d.list_proposals() if p.state == "pending"]:
                    await asyncio.sleep(0.01)
                prop_d = rt_d.list_proposals()[-1]
                # Delete the config file on disk before approving
                cfg_file_d.unlink()
                await rt_d.decide(proposal_id=prop_d.id, digest=prop_d.digest, decision="approve")
                res_d = await ct_d
                assert res_d["tool_succeeded"] is False
                assert res_d["error_code"] == "CONFIG_MODIFIED"
                assert res_d["effect_applied"] is False
                assert len(f01_srv.requests_log) == 0
                assert rt_d._config.config_sha256 == original_sha_d
            finally:
                await rt_d.aclose()

        asyncio.run(_run_f01_delete())

        # Case B: extension_config modified on disk while call_mcp proposal is pending
        cfg_file_b = tmp_path / "f01_call_cfg.json"
        cfg_file_b.write_text(
            json.dumps(
                {
                    "version": 1,
                    "servers": [{"id": "srv-f01-b", "transport": "http", "url": f01_srv.url}],
                }
            ),
            encoding="utf-8",
        )
        loaded_b = load_extension_config(path=cfg_file_b)
        proj_b = create_buggy_calc_project(tmp_path / "proj_f01_b")
        rt_b = ExtensionRuntime(
            project_id="proj-f01",
            session_id="sess-f01",
            run_id="run-f01-b",
            project_root=proj_b,
            config=loaded_b,
            selected_skill_ids=(),
            scratch_root=tmp_path / "scratch_f01_b",
            emit=lambda _k, _p: None,
        )

        async def _run_f01_call() -> None:
            try:
                ct_b = asyncio.create_task(
                    rt_b.execute(_action("connect_mcp", {"server_id": "srv-f01-b"}, "act-f01-b-conn"))
                )
                while not [p for p in rt_b.list_proposals() if p.state == "pending"]:
                    await asyncio.sleep(0.01)
                prop_conn_b = rt_b.list_proposals()[-1]
                await rt_b.decide(
                    proposal_id=prop_conn_b.id, digest=prop_conn_b.digest, decision="approve"
                )
                assert (await ct_b)["tool_succeeded"] is True
                assert (
                    await rt_b.execute(
                        _action("list_mcp_tools", {"server_id": "srv-f01-b"}, "act-f01-b-list")
                    )
                )["tool_succeeded"] is True

                call_task_b = asyncio.create_task(
                    rt_b.execute(
                        _action(
                            "call_mcp",
                            {
                                "server_id": "srv-f01-b",
                                "tool_name": "echo_tool",
                                "arguments": {"msg": "blocked-by-config-drift"},
                            },
                            "act-f01-b-call",
                        )
                    )
                )
                while not [p for p in rt_b.list_proposals() if p.state == "pending"]:
                    await asyncio.sleep(0.01)
                prop_call_b = next(p for p in rt_b.list_proposals() if p.state == "pending")
                # Mutate disk config file while call_mcp proposal is pending
                cfg_file_b.write_text(
                    json.dumps(
                        {
                            "version": 1,
                            "servers": [
                                {"id": "srv-f01-b", "transport": "http", "url": "http://127.0.0.1:59998/mcp"}
                            ],
                        }
                    ),
                    encoding="utf-8",
                )
                await rt_b.decide(
                    proposal_id=prop_call_b.id, digest=prop_call_b.digest, decision="approve"
                )
                res_call_b = await call_task_b
                assert res_call_b["tool_succeeded"] is False
                assert res_call_b["error_code"] == "CONFIG_MODIFIED"
                assert res_call_b["effect_applied"] is False
                assert f01_srv.call_count == 0
            finally:
                await rt_b.aclose()

        asyncio.run(_run_f01_call())

    # Case C: executable bytes modified on disk while connect_mcp proposal is pending via rt.execute
    swap_bin = tmp_path / "swap_e2e.exe"
    swap_bin.write_bytes(b"ORIG_EXE_BYTES_E2E")
    srv_swap_e2e = StdioMcpServerConfig(
        id="srv-swap-e2e", transport="stdio", executable=str(swap_bin), argv=[], cwd=str(tmp_path)
    )
    rt_swap_e2e, _ = _make_runtime(tmp_path, servers=[srv_swap_e2e], run_id="run-swap-e2e")

    async def _run_swap_e2e() -> None:
        try:
            ct_swap = asyncio.create_task(
                rt_swap_e2e.execute(
                    _action("connect_mcp", {"server_id": "srv-swap-e2e"}, "act-swap-e2e")
                )
            )
            while not [p for p in rt_swap_e2e.list_proposals() if p.state == "pending"]:
                await asyncio.sleep(0.01)
            prop_swap = rt_swap_e2e.list_proposals()[-1]
            swap_bin.write_bytes(b"MODIFIED_EXE_BYTES_E2E")
            await rt_swap_e2e.decide(
                proposal_id=prop_swap.id, digest=prop_swap.digest, decision="approve"
            )
            res_swap = await ct_swap
            assert res_swap["tool_succeeded"] is False
            assert res_swap["error_code"] == "EXECUTABLE_MODIFIED"
            assert res_swap["effect_applied"] is False
        finally:
            await rt_swap_e2e.aclose()

    asyncio.run(_run_swap_e2e())


def test_s15_isolation(tmp_path: Path) -> None:
    with LocalHttpMcpServer(label="s15-a") as srv_a, LocalHttpMcpServer(label="s15-b") as srv_b:
        cfg_a = HttpMcpServerConfig(id="srv-a", transport="http", url=srv_a.url)
        cfg_b = HttpMcpServerConfig(id="srv-b", transport="http", url=srv_b.url)
        rt1, _ = _make_runtime(tmp_path, servers=[cfg_a, cfg_b], run_id="run-iso-1")
        rt2, _ = _make_runtime(tmp_path, servers=[cfg_a, cfg_b], run_id="run-iso-2")

        p1 = rt1._broker.create_connect_proposal(action_id="act-1", server=cfg_a)
        rt1._broker.decide(
            proposal_id=p1.id,
            request={"digest": p1.digest, "decision": "approve"},  # type: ignore[arg-type]
        )

        # Cross-run proposal replay rejected
        with pytest.raises(ExtensionError) as exc_cross_run:
            rt2._broker.decide(
                proposal_id=p1.id,
                request={"digest": p1.digest, "decision": "approve"},  # type: ignore[arg-type]
            )
        assert exc_cross_run.value.code == "NOT_FOUND"

        # Same-run cross-action_id consume rejected
        with pytest.raises(ExtensionError) as exc_act:
            rt1._broker.consume_connect_grant(
                proposal_id=p1.id,
                action_id="wrong-action",
                server=cfg_a,
                current_config_sha256=rt1._config.config_sha256,
            )
        assert exc_act.value.code == "ACTION_ID_MISMATCH"

        # Same-name tool cannot use grant bound to srv-a on srv-b
        pc = rt1._broker.create_call_proposal(
            action_id="act-call",
            server=cfg_a,
            tool_name="echo_tool",
            schema_sha256="1" * 64,
            args_sha256="2" * 64,
            arguments={"msg": "x"},
        )
        rt1._broker.decide(
            proposal_id=pc.id,
            request={"digest": pc.digest, "decision": "approve"},  # type: ignore[arg-type]
        )
        with pytest.raises(ExtensionError) as exc_srv:
            rt1._broker.consume_call_grant(
                proposal_id=pc.id,
                action_id="act-call",
                server=cfg_b,
                current_config_sha256=rt1._config.config_sha256,
                tool_name="echo_tool",
                schema_sha256="1" * 64,
                args_sha256="2" * 64,
            )
        assert exc_srv.value.code == "SERVER_MISMATCH"
        assert srv_a.call_count == 0 and srv_b.call_count == 0


def test_s16_idempotency(tmp_path: Path) -> None:
    now_holder = [100.0]

    def _clock() -> float:
        return now_holder[0]

    with LocalHttpMcpServer(label="s16") as http_srv:
        srv = HttpMcpServerConfig(id="srv-s16", transport="http", url=http_srv.url)
        rt, _ = _make_runtime(tmp_path, servers=[srv], ttl_seconds=10.0, clock=_clock)

        async def _run() -> None:
            try:
                ct = asyncio.create_task(
                    rt.execute(_action("connect_mcp", {"server_id": "srv-s16"}, "act-c16"))
                )
                while not [p for p in rt.list_proposals() if p.state == "pending"]:
                    await asyncio.sleep(0.01)
                pc = rt.list_proposals()[-1]
                # Repeated identical decide returns original approved view
                d1 = await rt.decide(proposal_id=pc.id, digest=pc.digest, decision="approve")
                d2 = await rt.decide(proposal_id=pc.id, digest=pc.digest, decision="approve")
                assert d1.state == "approved" and d2.state == "approved"
                # Conflicting decision or wrong digest raises 409
                with pytest.raises(ExtensionError) as e_conf:
                    await rt.decide(proposal_id=pc.id, digest=pc.digest, decision="reject")
                assert e_conf.value.status_code == 409
                with pytest.raises(ExtensionError) as e_dig:
                    await rt.decide(proposal_id=pc.id, digest="0" * 64, decision="approve")
                assert e_dig.value.status_code == 409

                assert (await ct)["tool_succeeded"] is True
                await rt.execute(_action("list_mcp_tools", {"server_id": "srv-s16"}, "act-l16"))

                # Approved call executed once, repeated same action_id replays cached record with 1 server call
                call_task = asyncio.create_task(
                    rt.execute(
                        _action(
                            "call_mcp",
                            {"server_id": "srv-s16", "tool_name": "echo_tool", "arguments": {"msg": "idem"}},
                            "act-call-idem",
                        )
                    )
                )
                while not [p for p in rt.list_proposals() if p.state == "pending"]:
                    await asyncio.sleep(0.01)
                pcall = next(p for p in rt.list_proposals() if p.state == "pending")
                await rt.decide(proposal_id=pcall.id, digest=pcall.digest, decision="approve")
                first = await call_task
                replay = await rt.execute(
                    _action(
                        "call_mcp",
                        {"server_id": "srv-s16", "tool_name": "echo_tool", "arguments": {"msg": "idem"}},
                        "act-call-idem",
                    )
                )
                assert first["call_id"] == replay["call_id"]
                assert http_srv.call_count == 1

                # Expired proposal cannot be approved
                p_exp = rt._broker.create_connect_proposal(action_id="act-exp", server=srv)
                now_holder[0] += 11.0
                with pytest.raises(ExtensionError) as e_exp:
                    await rt.decide(proposal_id=p_exp.id, digest=p_exp.digest, decision="approve")
                assert e_exp.value.status_code == 409 and e_exp.value.code == "PROPOSAL_EXPIRED"
            finally:
                await rt.aclose()

        asyncio.run(_run())


def test_s17_cancelled_wait(tmp_path: Path) -> None:
    log_s = tmp_path / "logs" / "s17.jsonl"
    sc_s = write_stdio_mcp_script(tmp_path / "scripts", name="s17.py", log_path=log_s)
    srv = StdioMcpServerConfig(
        id="srv-s17", transport="stdio", executable=sys.executable, argv=["-B", str(sc_s)], cwd=str(tmp_path)
    )
    rt, _ = _make_runtime(tmp_path, servers=[srv])

    async def _run() -> None:
        conn_t = asyncio.create_task(
            rt.execute(_action("connect_mcp", {"server_id": "srv-s17"}, "act-c17"))
        )
        while not [p for p in rt.list_proposals() if p.state == "pending"]:
            await asyncio.sleep(0.01)
        p_pend = rt.list_proposals()[-1]
        cleanup = await rt.aclose()
        res = await conn_t
        assert cleanup.cleanup_ok and cleanup.remaining_active == 0
        assert res["tool_succeeded"] is False
        assert res["effect_applied"] is False

        # Late approve arrives after cancellation/close -> 409, zero spawns
        with pytest.raises(ExtensionError) as exc_late:
            await rt.decide(proposal_id=p_pend.id, digest=p_pend.digest, decision="approve")
        assert exc_late.value.status_code == 409
        assert len(read_stdio_log(log_s)) == 0

    asyncio.run(_run())


def test_s18_config_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import patch

    _ = PROJECT_NAME_PATTERN
    bad_configs: list[dict[str, Any]] = [
        {"version": 1, "servers": [{"id": "bad1", "transport": "http", "url": "http://8.8.8.8:8080/mcp"}]},
        {"version": 1, "servers": [{"id": "bad2", "transport": "http", "url": "http://localhost:8080/mcp"}]},
        {"version": 1, "servers": [{"id": "bad3", "transport": "http", "url": "https://127.0.0.1:8080/mcp"}]},
        {"version": 1, "servers": [{"id": "bad4", "transport": "http", "url": "http://user@127.0.0.1:8080/mcp"}]},
        {"version": 1, "servers": [{"id": "bad5", "transport": "http", "url": "http://127.0.0.1:8080/mcp?x=1"}]},
        {"version": 1, "servers": [{"id": "bad6", "transport": "http", "url": "http://127.0.0.1/mcp"}]},
        {"version": 1, "servers": [{"id": "bad7", "transport": "http", "url": "http://127.0.0.1:8080/mcp", "headers": {"X": "Y"}}]},
        {"version": 1, "servers": [{"id": "bad8", "transport": "stdio", "executable": sys.executable, "argv": [], "cwd": str(tmp_path), "env": {"API_KEY": "1"}}]},
        {"version": 1, "servers": [{"id": "bad9", "transport": "stdio", "executable": sys.executable, "argv": ["x"] * 65, "cwd": str(tmp_path)}]},
        {"version": 1, "servers": [{"id": f"s{i}", "transport": "http", "url": f"http://127.0.0.1:{8000+i}/mcp"} for i in range(9)]},
        {"version": 1, "servers": [], "unknown_field": True},
    ]
    for idx, cfg_obj in enumerate(bad_configs):
        cfg_file = tmp_path / f"bad_{idx}.json"
        cfg_file.write_text(json.dumps(cfg_obj), encoding="utf-8")
        with pytest.raises(ExtensionError):
            load_extension_config(path=cfg_file)

    # Config file > 64 KiB (65,536 bytes) must be rejected with INVALID_EXTENSION_CONFIG
    big_cfg_file = tmp_path / "bad_over_64k.json"
    big_cfg_file.write_text(
        '{"version": 1, "servers": [' + (" " * 66000) + "]}",
        encoding="utf-8",
    )
    assert big_cfg_file.stat().st_size > 65536
    with pytest.raises(ExtensionError) as exc_big_cfg:
        load_extension_config(path=big_cfg_file)
    assert exc_big_cfg.value.code == "INVALID_EXTENSION_CONFIG"

    # F06 rollback stdio argv boundaries: empty argv, a single 4097-byte item, and 64 items are all legal.
    stdio_pos = [
        {"version": 1, "servers": [{"id": "ok-empty", "transport": "stdio", "executable": sys.executable, "argv": [], "cwd": str(tmp_path)}]},
        {"version": 1, "servers": [{"id": "ok-4097", "transport": "stdio", "executable": sys.executable, "argv": ["x" * 4097], "cwd": str(tmp_path)}]},
        {"version": 1, "servers": [{"id": "ok-64", "transport": "stdio", "executable": sys.executable, "argv": ["x"] * 64, "cwd": str(tmp_path)}]},
        {"version": 1, "servers": [{"id": "ok-16k", "transport": "stdio", "executable": sys.executable, "argv": ["x" * 16384], "cwd": str(tmp_path)}]},
    ]
    for idx, cfg_obj in enumerate(stdio_pos):
        f = tmp_path / f"stdio_ok_{idx}.json"
        f.write_text(json.dumps(cfg_obj), encoding="utf-8")
        loaded_pos = load_extension_config(path=f)
        assert loaded_pos.servers[0].id == cfg_obj["servers"][0]["id"]

    # 65 items and total > 16 KiB are rejected.
    stdio_bad = [
        {"version": 1, "servers": [{"id": "bad65", "transport": "stdio", "executable": sys.executable, "argv": ["x"] * 65, "cwd": str(tmp_path)}]},
        {"version": 1, "servers": [{"id": "bad-total", "transport": "stdio", "executable": sys.executable, "argv": ["y" * 8000, "z" * 8000, "w" * 1000], "cwd": str(tmp_path)}]},
    ]
    for idx, cfg_obj in enumerate(stdio_bad):
        f = tmp_path / f"stdio_bad_{idx}.json"
        f.write_text(json.dumps(cfg_obj), encoding="utf-8")
        with pytest.raises(ExtensionError):
            load_extension_config(path=f)

    # Proxy canary (F04-S18): isolate the environment per variant, delete NO_PROXY so the proxy would be
    # honored if trust_env were enabled, and observe httpx.AsyncClient trust_env=False as co-evidence.
    import socket as _socket
    import threading as _threading
    import time as _time

    import httpx

    canary_srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    canary_srv.bind(("127.0.0.1", 0))
    canary_srv.listen(8)
    canary_port = canary_srv.getsockname()[1]
    canary_srv.settimeout(0.25)
    canary_hits = [0]
    canary_stop = [False]

    def _count_proxy() -> None:
        while not canary_stop[0]:
            try:
                conn, _addr = canary_srv.accept()
                canary_hits[0] += 1
                conn.close()
            except TimeoutError:
                continue
            except OSError:
                break

    canary_thread = _threading.Thread(target=_count_proxy, daemon=True)
    canary_thread.start()
    canary_url = f"http://127.0.0.1:{canary_port}"
    observed_trust_env: list[object] = []
    real_asyncclient = httpx.AsyncClient

    def _spy_asyncclient(*args: Any, **kwargs: Any) -> Any:
        observed_trust_env.append(kwargs.get("trust_env"))
        return real_asyncclient(*args, **kwargs)

    async def _run_connect(rt: Any) -> None:
        try:
            t = asyncio.create_task(rt.execute(_action("connect_mcp", {"server_id": "ok-proxy"}, "act-proxy")))
            while not [pp for pp in rt.list_proposals() if pp.state == "pending"]:
                await asyncio.sleep(0.01)
            pp = next(pp for pp in rt.list_proposals() if pp.state == "pending")
            await rt.decide(proposal_id=pp.id, digest=pp.digest, decision="approve")
            r = await t
            assert r["tool_succeeded"] is True
        finally:
            await rt.aclose()

    with patch.object(httpx, "AsyncClient", side_effect=_spy_asyncclient):
        # Baseline: prove the canary counter actually observes a direct connection, then reset.
        with _socket.create_connection(("127.0.0.1", canary_port), timeout=1.0):
            pass
        _time.sleep(0.05)
        assert canary_hits[0] >= 1, "canary counter did not observe a baseline connection"
        canary_hits[0] = 0

        for proxy_name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            with monkeypatch.context() as mp:
                for name in (
                    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                    "http_proxy", "https_proxy", "all_proxy",
                    "NO_PROXY", "no_proxy",
                ):
                    mp.delenv(name, raising=False)
                mp.setenv(proxy_name, canary_url)
                with LocalHttpMcpServer(label=f"proxy-{proxy_name}") as s_proxy:
                    proxy_file = tmp_path / f"proxy_{proxy_name}.json"
                    proxy_file.write_text(
                        json.dumps(
                            {
                                "version": 1,
                                "servers": [{"id": "ok-proxy", "transport": "http", "url": s_proxy.url}],
                            }
                        ),
                        encoding="utf-8",
                    )
                    proxy_loaded = load_extension_config(path=proxy_file)
                    rt_proxy, _ = _make_runtime(tmp_path, servers=proxy_loaded.servers, run_id=f"run-{proxy_name}")
                    asyncio.run(_run_connect(rt_proxy))
            assert canary_hits[0] == 0, f"{proxy_name} canary was hit"

    canary_stop[0] = True
    canary_thread.join(timeout=2.0)
    canary_srv.close()
    assert observed_trust_env and all(te is False for te in observed_trust_env), (
        f"httpx.AsyncClient trust_env not observed False: {observed_trust_env}"
    )

    # Positive controls: literal IPv4 127.0.0.1 and literal IPv6 [::1] connect cleanly
    with LocalHttpMcpServer(host="127.0.0.1", label="ipv4") as s4, LocalHttpMcpServer(host="::1", label="ipv6") as s6:
        ok_file = tmp_path / "ok.json"
        ok_file.write_text(
            json.dumps(
                {
                    "version": 1,
                    "servers": [
                        {"id": "ok-v4", "transport": "http", "url": s4.url},
                        {"id": "ok-v6", "transport": "http", "url": s6.url},
                    ],
                }
            ),
            encoding="utf-8",
        )
        loaded = load_extension_config(path=ok_file)
        assert [s.id for s in loaded.servers] == ["ok-v4", "ok-v6"]
        rt, _ = _make_runtime(tmp_path, servers=loaded.servers, run_id="run-v4v6")

        async def _check_pos() -> None:
            try:
                for sid in ("ok-v4", "ok-v6"):
                    t = asyncio.create_task(rt.execute(_action("connect_mcp", {"server_id": sid}, f"act-{sid}")))
                    while not [p for p in rt.list_proposals() if p.state == "pending"]:
                        await asyncio.sleep(0.01)
                    pp = next(p for p in rt.list_proposals() if p.state == "pending")
                    await rt.decide(proposal_id=pp.id, digest=pp.digest, decision="approve")
                    r = await t
                    assert r["tool_succeeded"] is True
            finally:
                await rt.aclose()

        asyncio.run(_check_pos())
