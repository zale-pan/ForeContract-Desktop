"""S25–S30: Extension HTTP API security, start fingerprint, proposal decisions, reload/restart, malicious output safety, and quotas/retention."""

from __future__ import annotations

import asyncio
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from app_helpers import (
    CREDENTIAL_ENV_CANARY,
    LiveTcpTestServer,
    auth_headers,
    tree_hashes,
)
from extension_helpers import (
    CallbackProjectProvider,
    LocalHttpMcpServer,
    ext_forecast,
    make_extension_host_config,
    make_skill,
    read_stdio_log,
    write_extension_config_file,
    write_stdio_mcp_script,
)
from forecontract_llm import ModelRequest
from project_helpers import create_buggy_calc_project


def test_s25_api_security(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", CREDENTIAL_ENV_CANARY)
    proj = create_buggy_calc_project(tmp_path / "proj")
    make_skill(proj, "s25", "# Safe\n")
    log_s = tmp_path / "logs" / "s25.jsonl"
    sc_s = write_stdio_mcp_script(tmp_path / "scripts", name="s25.py", log_path=log_s)
    ext_cfg = write_extension_config_file(
        tmp_path / "ext25.json",
        servers=[{"id": "srv-25", "transport": "stdio", "executable": sys.executable, "argv": ["-B", str(sc_s)], "cwd": str(tmp_path)}],
    )
    cfg = make_extension_host_config(tmp_path / "host", allowed_roots=(proj,), extension_config_path=ext_cfg)

    with LiveTcpTestServer(config=cfg) as srv:
        reg = httpx.post(
            f"{srv.base_url}/api/v1/projects/register",
            headers=auth_headers(cfg),
            json={"name": "s25-proj", "root": str(proj), "confirmed_read": True},
        )
        assert reg.status_code == 201
        pid = reg.json()["project_id"]

        # GET servers and GET skills succeed and NEVER spawn processes
        r_srv = httpx.get(f"{srv.base_url}/api/v1/extensions/servers", headers=auth_headers(cfg))
        r_sk = httpx.get(f"{srv.base_url}/api/v1/projects/{pid}/skills", headers=auth_headers(cfg))
        assert r_srv.status_code == 200 and len(r_srv.json()) == 1
        assert r_sk.status_code == 200 and [x["id"] for x in r_sk.json()] == ["s25"]
        assert len(read_stdio_log(log_s)) == 0
        assert CREDENTIAL_ENV_CANARY not in r_srv.text and CREDENTIAL_ENV_CANARY not in r_sk.text

        # Missing token, bad Origin, oversized body, bad JSON rejected
        assert httpx.get(f"{srv.base_url}/api/v1/extensions/servers").status_code == 401
        bad_origin = {"Host": f"127.0.0.1:{cfg.bind_port}", "Authorization": f"Bearer {cfg.access_token}", "Origin": "https://evil.example"}
        assert httpx.post(f"{srv.base_url}/api/v1/sessions/s/runs/r/extensions/proposals/p/decision", headers=bad_origin, json={"digest": "a" * 64, "decision": "approve"}).status_code == 403
        big_body = httpx.post(f"{srv.base_url}/api/v1/sessions/s/runs/r/extensions/proposals/p/decision", headers=auth_headers(cfg), content=b"x" * (65 * 1024))
        assert big_body.status_code == 413
        bad_json = httpx.post(f"{srv.base_url}/api/v1/sessions/s/runs/r/extensions/proposals/p/decision", headers={**auth_headers(cfg), "Content-Type": "application/json"}, content=b"{bad")
        assert bad_json.status_code == 422


def test_s26_start_fingerprint(tmp_path: Path) -> None:
    proj = create_buggy_calc_project(tmp_path / "proj")
    make_skill(proj, "sk-a", "A" * 100)
    make_skill(proj, "sk-b", "B" * 100)
    make_skill(proj, "sk-huge", "H" * 17000)
    cfg = make_extension_host_config(tmp_path / "host", allowed_roots=(proj,))

    calls = 0

    def _fac(*, project: Any, run_id: str) -> CallbackProjectProvider:
        _ = project, run_id

        class _P(CallbackProjectProvider):
            async def complete(self, request: ModelRequest) -> Any:
                nonlocal calls
                calls += 1
                return await super().complete(request)

        return _P(lambda s, req, st: ext_forecast(action_type="list_directory", payload={}))

    with LiveTcpTestServer(config=cfg, provider_factory=_fac) as srv:
        reg = httpx.post(
            f"{srv.base_url}/api/v1/projects/register",
            headers=auth_headers(cfg),
            json={"name": "s26-reg", "root": str(proj), "confirmed_read": True},
        )
        pid = reg.json()["project_id"]
        sess = httpx.post(
            f"{srv.base_url}/api/v1/sessions",
            headers=auth_headers(cfg),
            json={"project_id": pid, "model": {"provider": "offline", "model": "offline-project-v1", "thinking": "disabled"}},
        ).json()
        sid = sess["session_id"]

        # Controlled demo project rejects non-empty skill_ids, allows empty []
        ctrl = httpx.post(f"{srv.base_url}/api/v1/projects", headers=auth_headers(cfg), json={"name": "s26-demo"}).json()
        csess = httpx.post(
            f"{srv.base_url}/api/v1/sessions",
            headers=auth_headers(cfg),
            json={"project_id": ctrl["project_id"], "model": {"provider": "offline", "model": "offline-metrics-v1", "thinking": "disabled"}},
        ).json()
        bad_ctrl = httpx.post(
            f"{srv.base_url}/api/v1/sessions/{csess['session_id']}/runs",
            headers=auth_headers(cfg),
            json={"client_request_id": str(uuid.uuid4()), "goal": "demo", "confirmed_scope": True, "skill_ids": ["sk-a"]},
        )
        assert bad_ctrl.status_code in (400, 403)

        # Missing or oversized skill rejected before model execution
        assert httpx.post(
            f"{srv.base_url}/api/v1/sessions/{sid}/runs",
            headers=auth_headers(cfg),
            json={"client_request_id": str(uuid.uuid4()), "goal": "missing", "confirmed_scope": True, "skill_ids": ["no-such"]},
        ).status_code == 400
        assert httpx.post(
            f"{srv.base_url}/api/v1/sessions/{sid}/runs",
            headers=auth_headers(cfg),
            json={"client_request_id": str(uuid.uuid4()), "goal": "over", "confirmed_scope": True, "skill_ids": ["sk-huge"]},
        ).status_code == 400
        assert calls == 0

        # Idempotent replay vs 409 conflict on differing skill_ids
        cid = str(uuid.uuid4())
        r1 = httpx.post(
            f"{srv.base_url}/api/v1/sessions/{sid}/runs",
            headers=auth_headers(cfg),
            json={"client_request_id": cid, "goal": "g1", "confirmed_scope": True, "skill_ids": ["sk-a"]},
        )
        assert r1.status_code == 202
        r2 = httpx.post(
            f"{srv.base_url}/api/v1/sessions/{sid}/runs",
            headers=auth_headers(cfg),
            json={"client_request_id": cid, "goal": "g1", "confirmed_scope": True, "skill_ids": ["sk-a"]},
        )
        assert r2.status_code == 200 and r2.json()["run_id"] == r1.json()["run_id"]
        r3 = httpx.post(
            f"{srv.base_url}/api/v1/sessions/{sid}/runs",
            headers=auth_headers(cfg),
            json={"client_request_id": cid, "goal": "g1", "confirmed_scope": True, "skill_ids": ["sk-b"]},
        )
        assert r3.status_code == 409


def test_s27_decision_api(tmp_path: Path) -> None:
    proj = create_buggy_calc_project(tmp_path / "proj")
    with LocalHttpMcpServer(label="s27") as http_mcp:
        ext_cfg = write_extension_config_file(
            tmp_path / "ext27.json",
            servers=[{"id": "srv-27", "transport": "http", "url": http_mcp.url}],
        )
        cfg = make_extension_host_config(tmp_path / "host", allowed_roots=(proj,), extension_config_path=ext_cfg)

        def _decide(step: int, req: ModelRequest, st: dict[str, Any]) -> str:
            _ = req, st
            if step == 1:
                return ext_forecast(action_type="connect_mcp", payload={"server_id": "srv-27"})
            if step == 2:
                return ext_forecast(action_type="list_mcp_tools", payload={"server_id": "srv-27"})
            if step == 3:
                return ext_forecast(action_type="call_mcp", payload={"server_id": "srv-27", "tool_name": "echo_tool", "arguments": {"msg": "s27"}})
            return ext_forecast(action_type="list_directory", payload={})

        with LiveTcpTestServer(config=cfg, provider_factory=lambda *, project, run_id: CallbackProjectProvider(_decide)) as srv:
            pid = httpx.post(f"{srv.base_url}/api/v1/projects/register", headers=auth_headers(cfg), json={"name": "p27", "root": str(proj), "confirmed_read": True}).json()["project_id"]
            sid = httpx.post(f"{srv.base_url}/api/v1/sessions", headers=auth_headers(cfg), json={"project_id": pid, "model": {"provider": "offline", "model": "offline-project-v1", "thinking": "disabled"}}).json()["session_id"]
            rid = httpx.post(f"{srv.base_url}/api/v1/sessions/{sid}/runs", headers=auth_headers(cfg), json={"client_request_id": str(uuid.uuid4()), "goal": "s27", "confirmed_scope": True}).json()["run_id"]

            seen_decided: set[str] = set()
            for _ in range(150):
                ext_r = httpx.get(f"{srv.base_url}/api/v1/sessions/{sid}/runs/{rid}/extensions", headers=auth_headers(cfg)).json()
                for prop in ext_r["proposals"]:
                    if prop["state"] == "pending" and prop["id"] not in seen_decided:
                        eid = prop["id"]
                        dig = prop["digest"]
                        # 422 on malformed digest
                        assert httpx.post(
                            f"{srv.base_url}/api/v1/sessions/{sid}/runs/{rid}/extensions/proposals/{eid}/decision",
                            headers=auth_headers(cfg),
                            json={"digest": "bad", "decision": "approve"},
                        ).status_code == 422
                        # 409 on wrong 64-hex digest
                        assert httpx.post(
                            f"{srv.base_url}/api/v1/sessions/{sid}/runs/{rid}/extensions/proposals/{eid}/decision",
                            headers=auth_headers(cfg),
                            json={"digest": "0" * 64, "decision": "approve"},
                        ).status_code == 409
                        # 404 on wrong run id
                        assert httpx.post(
                            f"{srv.base_url}/api/v1/sessions/{sid}/runs/{uuid.uuid4()}/extensions/proposals/{eid}/decision",
                            headers=auth_headers(cfg),
                            json={"digest": dig, "decision": "approve"},
                        ).status_code == 404
                        # 200 valid approve + 200 idempotent replay + 409 conflicting reject
                        ok1 = httpx.post(
                            f"{srv.base_url}/api/v1/sessions/{sid}/runs/{rid}/extensions/proposals/{eid}/decision",
                            headers=auth_headers(cfg),
                            json={"digest": dig, "decision": "approve"},
                        )
                        ok2 = httpx.post(
                            f"{srv.base_url}/api/v1/sessions/{sid}/runs/{rid}/extensions/proposals/{eid}/decision",
                            headers=auth_headers(cfg),
                            json={"digest": dig, "decision": "approve"},
                        )
                        conf = httpx.post(
                            f"{srv.base_url}/api/v1/sessions/{sid}/runs/{rid}/extensions/proposals/{eid}/decision",
                            headers=auth_headers(cfg),
                            json={"digest": dig, "decision": "reject"},
                        )
                        assert ok1.status_code == 200 and ok2.status_code == 200 and conf.status_code == 409
                        seen_decided.add(eid)
                run_st = httpx.get(f"{srv.base_url}/api/v1/sessions/{sid}/runs/{rid}", headers=auth_headers(cfg)).json()["state"]
                if run_st in ("succeeded", "failed", "cancelled", "unknown"):
                    break
                time.sleep(0.05)

            assert len(seen_decided) == 2
            assert http_mcp.call_count == 1


def test_s28_reload_restart(tmp_path: Path) -> None:
    import json
    import shutil
    import sqlite3

    from forecontract_app.session_models import SESSION_DB_FILENAME

    proj = create_buggy_calc_project(tmp_path / "proj")
    make_skill(proj, "s28-skill", "Restart check skill")

    def _snapshot_db(data_root: Path, dest: Path) -> None:
        src = sqlite3.connect(str(data_root / SESSION_DB_FILENAME))
        dst = sqlite3.connect(str(dest))
        src.backup(dst)
        dst.close()
        src.close()

    with LocalHttpMcpServer(label="s28") as http_mcp:
        ext_cfg_http = write_extension_config_file(
            tmp_path / "ext28.json",
            servers=[{"id": "srv-28", "transport": "http", "url": http_mcp.url}],
        )

        def _decide(step: int, req: ModelRequest, st: dict[str, Any]) -> str:
            _ = req, st
            if step == 1:
                return ext_forecast(action_type="connect_mcp", payload={"server_id": "srv-28"})
            if step == 2:
                return ext_forecast(action_type="list_mcp_tools", payload={"server_id": "srv-28"})
            if step == 3:
                return ext_forecast(
                    action_type="call_mcp",
                    payload={"server_id": "srv-28", "tool_name": "echo_tool", "arguments": {"msg": "s28"}},
                )
            return ext_forecast(action_type="list_directory", payload={})

        # ---- Normal terminal positive: a completed run keeps its bounded result after restart. ----
        cfg1 = make_extension_host_config(tmp_path / "host", allowed_roots=(proj,), extension_config_path=ext_cfg_http)
        with LiveTcpTestServer(config=cfg1, provider_factory=lambda *, project, run_id: CallbackProjectProvider(_decide)) as srv1:
            pid = httpx.post(
                f"{srv1.base_url}/api/v1/projects/register", headers=auth_headers(cfg1),
                json={"name": "p28", "root": str(proj), "confirmed_read": True},
            ).json()["project_id"]
            sid = httpx.post(
                f"{srv1.base_url}/api/v1/sessions", headers=auth_headers(cfg1),
                json={"project_id": pid, "model": {"provider": "offline", "model": "offline-project-v1", "thinking": "disabled"}},
            ).json()["session_id"]
            rid = httpx.post(
                f"{srv1.base_url}/api/v1/sessions/{sid}/runs", headers=auth_headers(cfg1),
                json={"client_request_id": str(uuid.uuid4()), "goal": "s28 normal", "confirmed_scope": True, "skill_ids": ["s28-skill"]},
            ).json()["run_id"]
            for _ in range(150):
                ext_r = httpx.get(f"{srv1.base_url}/api/v1/sessions/{sid}/runs/{rid}/extensions", headers=auth_headers(cfg1)).json()
                for pr in ext_r.get("proposals", []):
                    if pr["state"] == "pending":
                        httpx.post(
                            f"{srv1.base_url}/api/v1/sessions/{sid}/runs/{rid}/extensions/proposals/{pr['id']}/decision",
                            headers=auth_headers(cfg1), json={"digest": pr["digest"], "decision": "approve"},
                        )
                if httpx.get(f"{srv1.base_url}/api/v1/sessions/{sid}/runs/{rid}", headers=auth_headers(cfg1)).json()["state"] in ("succeeded", "failed", "cancelled", "unknown"):
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("S28 normal run did not reach terminal state before service shutdown")

        init_before = http_mcp.methods.count("initialize")
        calls_before = http_mcp.call_count
        assert calls_before == 1

        # ---- Normal terminal positive restart: read the completed run's history + bounded result on the SAME root. ----
        cfg2 = make_extension_host_config(tmp_path / "host", allowed_roots=(proj,), extension_config_path=ext_cfg_http)
        with LiveTcpTestServer(config=cfg2) as srv2:
            hist = httpx.get(f"{srv2.base_url}/api/v1/sessions/{sid}/history", headers=auth_headers(cfg2))
            assert hist.status_code == 200
            assert any("s28-skill" in it["text"] for it in hist.json()["items"])
            ext_after = httpx.get(f"{srv2.base_url}/api/v1/sessions/{sid}/runs/{rid}/extensions", headers=auth_headers(cfg2))
            assert ext_after.status_code == 200
            assert len(ext_after.json()["calls"]) == 1
        assert http_mcp.call_count == calls_before
        assert http_mcp.methods.count("initialize") == init_before

        # ---- Pending scenario: capture a legal pending proposal (never approved) with its own identity. ----
        cfg_p = make_extension_host_config(tmp_path / "host_p", allowed_roots=(proj,), extension_config_path=ext_cfg_http)
        with LiveTcpTestServer(config=cfg_p, provider_factory=lambda *, project, run_id: CallbackProjectProvider(_decide)) as srv_p:
            pid_p = httpx.post(
                f"{srv_p.base_url}/api/v1/projects/register", headers=auth_headers(cfg_p),
                json={"name": "p28", "root": str(proj), "confirmed_read": True},
            ).json()["project_id"]
            sid_p = httpx.post(
                f"{srv_p.base_url}/api/v1/sessions", headers=auth_headers(cfg_p),
                json={"project_id": pid_p, "model": {"provider": "offline", "model": "offline-project-v1", "thinking": "disabled"}},
            ).json()["session_id"]
            rid_p = httpx.post(
                f"{srv_p.base_url}/api/v1/sessions/{sid_p}/runs", headers=auth_headers(cfg_p),
                json={"client_request_id": str(uuid.uuid4()), "goal": "s28 pending", "confirmed_scope": True, "skill_ids": ["s28-skill"]},
            ).json()["run_id"]
            pending_prop = None
            for _ in range(150):
                ext_r = httpx.get(f"{srv_p.base_url}/api/v1/sessions/{sid_p}/runs/{rid_p}/extensions", headers=auth_headers(cfg_p)).json()
                for pr in ext_r.get("proposals", []):
                    if pr["state"] == "pending":
                        pending_prop = pr
                        break
                if pending_prop is not None:
                    break
                time.sleep(0.05)
            assert pending_prop is not None
            # Identity consistency: the proposal is bound to its own session/run/project (not migrated).
            assert pending_prop["session_id"] == sid_p
            assert pending_prop["run_id"] == rid_p
            assert pending_prop["project_id"] == pid_p
            assert pending_prop["state"] == "pending"
            assert pending_prop["action_id"]
            prop_id_p = pending_prop["id"]
            prop_digest_p = pending_prop["digest"]
            run_p = httpx.get(f"{srv_p.base_url}/api/v1/sessions/{sid_p}/runs/{rid_p}", headers=auth_headers(cfg_p)).json()
            assert run_p["state"] == "running"
            snap_p = tmp_path / "snap_p.db"
            _snapshot_db(cfg_p.data_root, snap_p)

        cfg_p2 = make_extension_host_config(tmp_path / "host_p2", allowed_roots=(proj,), extension_config_path=ext_cfg_http)
        shutil.copyfile(snap_p, cfg_p2.data_root / SESSION_DB_FILENAME)
        from unittest.mock import patch

        from forecontract_app.mcp_process import McpJobProcess

        real_start = McpJobProcess.start
        spawn_calls_p: list[object] = []

        def _counted_start(self: Any) -> Any:
            spawn_calls_p.append(self)
            return real_start(self)

        with (
            patch.object(McpJobProcess, "start", _counted_start),
            LiveTcpTestServer(config=cfg_p2) as srv_p2,
        ):
            run_int = httpx.get(f"{srv_p2.base_url}/api/v1/sessions/{sid_p}/runs/{rid_p}", headers=auth_headers(cfg_p2)).json()
            assert run_int["state"] == "unknown"
            assert run_int["verification"] != "passed"
            # Old decision with the original id+digest and original session/run is rejected (not consumable).
            dec = httpx.post(
                f"{srv_p2.base_url}/api/v1/sessions/{sid_p}/runs/{rid_p}/extensions/proposals/{prop_id_p}/decision",
                headers=auth_headers(cfg_p2), json={"digest": prop_digest_p, "decision": "approve"},
            )
            assert dec.status_code == 404
            # Old bounded history still readable.
            hist = httpx.get(f"{srv_p2.base_url}/api/v1/sessions/{sid_p}/history", headers=auth_headers(cfg_p2))
            assert hist.status_code == 200
            assert any("s28 pending" in it["text"] for it in hist.json()["items"])
        assert len(spawn_calls_p) == 0
        assert http_mcp.methods.count("initialize") == init_before
        assert http_mcp.call_count == calls_before

        # ---- Active scenario: a stdio call reaches the server call_started barrier and is still active. ----
        ready_file = tmp_path / "ready_s28.json"
        log_a = tmp_path / "logs" / "s28a.jsonl"
        sc_a = write_stdio_mcp_script(
            tmp_path / "scripts", name="s28a.py", log_path=log_a, mode="hang_with_child", ready_file=ready_file
        )
        ext_cfg_a = write_extension_config_file(
            tmp_path / "ext28a.json",
            servers=[{"id": "srv-28a", "transport": "stdio", "executable": sys.executable, "argv": ["-B", str(sc_a)], "cwd": str(tmp_path)}],
        )

        def _decide_a(step: int, req: ModelRequest, st: dict[str, Any]) -> str:
            _ = req, st
            if step == 1:
                return ext_forecast(action_type="connect_mcp", payload={"server_id": "srv-28a"})
            if step == 2:
                return ext_forecast(action_type="list_mcp_tools", payload={"server_id": "srv-28a"})
            return ext_forecast(action_type="call_mcp", payload={"server_id": "srv-28a", "tool_name": "echo_tool", "arguments": {"msg": "hang"}})

        cfg_a = make_extension_host_config(tmp_path / "host_a", allowed_roots=(proj,), extension_config_path=ext_cfg_a)
        with LiveTcpTestServer(config=cfg_a, provider_factory=lambda *, project, run_id: CallbackProjectProvider(_decide_a)) as srv_a:
            pid_a = httpx.post(
                f"{srv_a.base_url}/api/v1/projects/register", headers=auth_headers(cfg_a),
                json={"name": "p28", "root": str(proj), "confirmed_read": True},
            ).json()["project_id"]
            sid_a = httpx.post(
                f"{srv_a.base_url}/api/v1/sessions", headers=auth_headers(cfg_a),
                json={"project_id": pid_a, "model": {"provider": "offline", "model": "offline-project-v1", "thinking": "disabled"}},
            ).json()["session_id"]
            rid_a = httpx.post(
                f"{srv_a.base_url}/api/v1/sessions/{sid_a}/runs", headers=auth_headers(cfg_a),
                json={"client_request_id": str(uuid.uuid4()), "goal": "s28 active", "confirmed_scope": True, "skill_ids": ["s28-skill"]},
            ).json()["run_id"]
            approved_a: set[str] = set()
            for _ in range(150):
                ext_r = httpx.get(f"{srv_a.base_url}/api/v1/sessions/{sid_a}/runs/{rid_a}/extensions", headers=auth_headers(cfg_a)).json()
                for pr in ext_r.get("proposals", []):
                    if pr["state"] == "pending" and pr["id"] not in approved_a:
                        httpx.post(
                            f"{srv_a.base_url}/api/v1/sessions/{sid_a}/runs/{rid_a}/extensions/proposals/{pr['id']}/decision",
                            headers=auth_headers(cfg_a), json={"digest": pr["digest"], "decision": "approve"},
                        )
                        approved_a.add(pr["id"])
                if ready_file.exists():
                    try:
                        pids = json.loads(ready_file.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        pids = {}
                    if pids.get("call_started") is True:
                        break
                time.sleep(0.05)
            assert ready_file.exists()
            pids = json.loads(ready_file.read_text(encoding="utf-8"))
            assert pids.get("call_started") is True
            run_a = httpx.get(f"{srv_a.base_url}/api/v1/sessions/{sid_a}/runs/{rid_a}", headers=auth_headers(cfg_a)).json()
            assert run_a["state"] == "running"
            # Capture the active call/process identity (the child process handling the call at the barrier).
            parent_pid_a = pids["parent_pid"]
            child_pid_a = pids["child_pid"]
            assert parent_pid_a and child_pid_a
            snap_a = tmp_path / "snap_a.db"
            _snapshot_db(cfg_a.data_root, snap_a)

        spawns_before = len([e for e in read_stdio_log(log_a) if e.get("event") == "started"])
        inits_a_before = len([e for e in read_stdio_log(log_a) if e.get("method") == "initialize"])
        calls_a_before = len([e for e in read_stdio_log(log_a) if e.get("method") == "tools/call"])

        cfg_a2 = make_extension_host_config(tmp_path / "host_a2", allowed_roots=(proj,), extension_config_path=ext_cfg_a)
        shutil.copyfile(snap_a, cfg_a2.data_root / SESSION_DB_FILENAME)
        with LiveTcpTestServer(config=cfg_a2) as srv_a2:
            run_a_int = httpx.get(f"{srv_a2.base_url}/api/v1/sessions/{sid_a}/runs/{rid_a}", headers=auth_headers(cfg_a2)).json()
            assert run_a_int["state"] == "unknown"
            assert run_a_int["verification"] != "passed"
            assert parent_pid_a and child_pid_a
        assert len([e for e in read_stdio_log(log_a) if e.get("event") == "started"]) == spawns_before
        assert len([e for e in read_stdio_log(log_a) if e.get("method") == "initialize"]) == inits_a_before
        assert len([e for e in read_stdio_log(log_a) if e.get("method") == "tools/call"]) == calls_a_before


def test_s29_malicious_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    monkeypatch.setenv("DEEPSEEK_API_KEY", CREDENTIAL_ENV_CANARY)
    proj = create_buggy_calc_project(tmp_path / "proj")
    log_m = tmp_path / "logs" / "s29.jsonl"

    with (
        LocalHttpMcpServer(label="never-fetch-canary") as canary_http,
        LocalHttpMcpServer(malformed_structured_content=None, label="bad-sc-http") as bad_sc_http,
    ):
        unrequested_uri = f"{canary_http.url}/unrequested-resource-canary"
        sc_m = write_stdio_mcp_script(
            tmp_path / "scripts",
            name="s29.py",
            log_path=log_m,
            mode="normal",
            resource_uri=unrequested_uri,
        )
        ext_cfg = write_extension_config_file(
            tmp_path / "ext29.json",
            servers=[
                {
                    "id": "srv-29",
                    "transport": "stdio",
                    "executable": sys.executable,
                    "argv": ["-B", str(sc_m)],
                    "cwd": str(tmp_path),
                },
                {
                    "id": "srv-29-badsc",
                    "transport": "http",
                    "url": bad_sc_http.url,
                },
            ],
        )
        cfg = make_extension_host_config(tmp_path / "host", allowed_roots=(proj,), extension_config_path=ext_cfg)
        captured_model_texts: list[str] = []

        def _decide(step: int, req: ModelRequest, st: dict[str, Any]) -> str:
            captured_model_texts.append(json.dumps(req.model_dump(mode="json"), ensure_ascii=False))
            _ = st
            if step == 1:
                return ext_forecast(action_type="connect_mcp", payload={"server_id": "srv-29"})
            if step == 2:
                return ext_forecast(action_type="list_mcp_tools", payload={"server_id": "srv-29"})
            if step == 3:
                return ext_forecast(
                    action_type="call_mcp",
                    payload={"server_id": "srv-29", "tool_name": "malicious_tool", "arguments": {"q": "xss"}},
                )
            if step == 4:
                return ext_forecast(action_type="connect_mcp", payload={"server_id": "srv-29-badsc"})
            if step == 5:
                return ext_forecast(action_type="list_mcp_tools", payload={"server_id": "srv-29-badsc"})
            if step == 6:
                return ext_forecast(
                    action_type="call_mcp",
                    payload={"server_id": "srv-29-badsc", "tool_name": "echo_tool", "arguments": {"msg": "null-sc"}},
                )
            return ext_forecast(action_type="list_directory", payload={})

        with LiveTcpTestServer(config=cfg, provider_factory=lambda *, project, run_id: CallbackProjectProvider(_decide)) as srv:
            pid = httpx.post(f"{srv.base_url}/api/v1/projects/register", headers=auth_headers(cfg), json={"name": "p29", "root": str(proj), "confirmed_read": True}).json()["project_id"]
            sid = httpx.post(f"{srv.base_url}/api/v1/sessions", headers=auth_headers(cfg), json={"project_id": pid, "model": {"provider": "offline", "model": "offline-project-v1", "thinking": "disabled"}}).json()["session_id"]
            rid = httpx.post(f"{srv.base_url}/api/v1/sessions/{sid}/runs", headers=auth_headers(cfg), json={"client_request_id": str(uuid.uuid4()), "goal": "s29", "confirmed_scope": True}).json()["run_id"]
            for _ in range(200):
                ext_r = httpx.get(f"{srv.base_url}/api/v1/sessions/{sid}/runs/{rid}/extensions", headers=auth_headers(cfg)).json()
                for p in ext_r["proposals"]:
                    if p["state"] == "pending":
                        httpx.post(f"{srv.base_url}/api/v1/sessions/{sid}/runs/{rid}/extensions/proposals/{p['id']}/decision", headers=auth_headers(cfg), json={"digest": p["digest"], "decision": "approve"})
                if httpx.get(f"{srv.base_url}/api/v1/sessions/{sid}/runs/{rid}", headers=auth_headers(cfg)).json()["state"] in ("succeeded", "failed", "cancelled", "unknown"):
                    break
                time.sleep(0.05)

            ext_final = httpx.get(f"{srv.base_url}/api/v1/sessions/{sid}/runs/{rid}/extensions", headers=auth_headers(cfg))
            assert ext_final.status_code == 200
            assert CREDENTIAL_ENV_CANARY not in ext_final.text
            calls = ext_final.json()["calls"]
            assert len(calls) == 2
            assert any(
                it.get("type") == "resource_link" and it.get("displayed") is False
                for it in calls[0]["content_items"]
            )
            # resource_link must never be auto-fetched and its target body must never enter model requests
            assert len(canary_http.requests_log) == 0
            # F02 HTTP-level check: structuredContent=null is recorded as MCP_PROTOCOL_ERROR (status=unknown, effect=unknown)
            assert calls[1]["status"] == "unknown"
            assert calls[1]["is_error"] is True
            assert calls[1]["effect"] == "unknown"
            assert calls[1]["error_code"] == "MCP_PROTOCOL_ERROR"
            assert calls[1]["structured_content"] is None


def test_s30_retention(tmp_path: Path) -> None:
    import json

    proj = create_buggy_calc_project(tmp_path / "proj")
    make_skill(proj, "s30-skill", "# Skill 30\nRetention check.\n")
    with LocalHttpMcpServer(label="s30") as http_mcp:
        from forecontract.models.actions import ActionSpec
        from forecontract_app.events import AppEvent
        from forecontract_app.extension_models import (
            ExtensionConfig,
            HttpMcpServerConfig,
        )
        from forecontract_app.extension_runtime import ExtensionRuntime
        from forecontract_app.models import ModelSelection, StartRunRequest
        from forecontract_app.project_models import (
            PROJECT_SCHEMA_VERSION,
            RegisterProjectRequest,
        )
        from forecontract_app.service import AppService
        from forecontract_app.session_models import (
            MAX_PERSISTED_EVENTS_BYTES_PER_RUN,
            MAX_PERSISTED_EVENTS_PER_RUN,
        )

        cfg = ExtensionConfig(version=1, servers=[HttpMcpServerConfig(id="srv-30", transport="http", url=http_mcp.url)])

        async def _check_quotas() -> None:
            rt = ExtensionRuntime(
                project_id="p30",
                session_id="s30",
                run_id="r30",
                project_root=proj,
                config=cfg,
                selected_skill_ids=(),
                scratch_root=tmp_path / "sc30",
                emit=lambda k, p: None,
            )

            async def _auto() -> None:
                for _ in range(1000):
                    for p in rt.list_proposals():
                        if p.state == "pending":
                            await rt.decide(proposal_id=p.id, digest=p.digest, decision="approve")
                    await asyncio.sleep(0.005)

            ta = asyncio.create_task(_auto())
            try:
                await rt.execute(ActionSpec(schema_version=PROJECT_SCHEMA_VERSION, action_type="connect_mcp", payload={"server_id": "srv-30"}, metadata={"action_id": "c"}))
                await rt.execute(ActionSpec(schema_version=PROJECT_SCHEMA_VERSION, action_type="list_mcp_tools", payload={"server_id": "srv-30"}, metadata={"action_id": "l"}))
                for idx in range(32):
                    r = await rt.execute(
                        ActionSpec(
                            schema_version=PROJECT_SCHEMA_VERSION,
                            action_type="call_mcp",
                            payload={"server_id": "srv-30", "tool_name": "echo_tool", "arguments": {"msg": f"m{idx}"}},
                            metadata={"action_id": f"call-{idx}"},
                        )
                    )
                    assert r["tool_succeeded"] is True
                # 33rd call exceeds 32-call quota
                r33 = await rt.execute(
                    ActionSpec(
                        schema_version=PROJECT_SCHEMA_VERSION,
                        action_type="call_mcp",
                        payload={"server_id": "srv-30", "tool_name": "echo_tool", "arguments": {"msg": "m32"}},
                        metadata={"action_id": "call-32"},
                    )
                )
                assert r33["tool_succeeded"] is False and r33["error_code"] == "MCP_CALL_QUOTA_EXCEEDED"
            finally:
                ta.cancel()
                cleanup = await rt.aclose()
                assert cleanup.cleanup_ok and cleanup.remaining_active == 0

        asyncio.run(_check_quotas())
        assert http_mcp.call_count == 32

        # Also enforce run-level byte quota before reaching 32 calls
        async def _check_byte_quota() -> None:
            rt2 = ExtensionRuntime(
                project_id="p30b",
                session_id="s30b",
                run_id="r30b",
                project_root=proj,
                config=cfg,
                selected_skill_ids=(),
                scratch_root=tmp_path / "sc30b",
                emit=lambda k, p: None,
            )

            async def _auto2() -> None:
                for _ in range(500):
                    for p in rt2.list_proposals():
                        if p.state == "pending":
                            await rt2.decide(proposal_id=p.id, digest=p.digest, decision="approve")
                    await asyncio.sleep(0.005)

            tb = asyncio.create_task(_auto2())
            try:
                await rt2.execute(ActionSpec(schema_version=PROJECT_SCHEMA_VERSION, action_type="connect_mcp", payload={"server_id": "srv-30"}, metadata={"action_id": "c"}))
                await rt2.execute(ActionSpec(schema_version=PROJECT_SCHEMA_VERSION, action_type="list_mcp_tools", payload={"server_id": "srv-30"}, metadata={"action_id": "l"}))
                big_arg = "B" * 100_000
                over_hit = False
                for idx in range(10):
                    r = await rt2.execute(
                        ActionSpec(
                            schema_version=PROJECT_SCHEMA_VERSION,
                            action_type="call_mcp",
                            payload={"server_id": "srv-30", "tool_name": "bulk_tool", "arguments": {"data": big_arg}},
                            metadata={"action_id": f"bcall-{idx}"},
                        )
                    )
                    if not r["tool_succeeded"] and r.get("error_code") == "MCP_DATA_QUOTA_EXCEEDED":
                        over_hit = True
                        break
                assert over_hit is True
            finally:
                tb.cancel()
                cleanup = await rt2.aclose()
                assert cleanup.cleanup_ok and cleanup.remaining_active == 0

        asyncio.run(_check_byte_quota())

        # Session-level and store-level quota trimming under extension summaries/events while preserving idempotency
        ext30_cfg = write_extension_config_file(
            tmp_path / "ext30.json",
            servers=[{"id": "srv-30", "transport": "http", "url": http_mcp.url}],
        )
        host_cfg = make_extension_host_config(
            tmp_path / "host30", allowed_roots=(proj,), extension_config_path=ext30_cfg
        )

        def _decide_s30(step: int, req: ModelRequest, st: dict[str, Any]) -> str:
            _ = req, st
            if step == 1:
                return ext_forecast(action_type="connect_mcp", payload={"server_id": "srv-30"})
            if step == 2:
                return ext_forecast(action_type="list_mcp_tools", payload={"server_id": "srv-30"})
            if step == 3:
                return ext_forecast(
                    action_type="call_mcp",
                    payload={"server_id": "srv-30", "tool_name": "echo_tool", "arguments": {"msg": "keep-idem"}},
                )
            return "DONE_FAST"

        async def _check_session_and_store_retention() -> None:
            svc = AppService(
                config=host_cfg,
                provider_factory=lambda *, project, run_id: CallbackProjectProvider(_decide_s30),
            )
            try:
                pview = await svc.register_project(
                    request=RegisterProjectRequest(name="p30-store", root=str(proj), confirmed_read=True)
                )
                sview = svc.create_session(
                    project_id=pview.project_id,
                    model=ModelSelection(provider="offline", model="offline-project-v1"),
                )
                idem_key = uuid.uuid4()
                rview, replayed0 = await svc.start_run_with_replay(
                    session_id=sview.session_id,
                    request=StartRunRequest(
                        client_request_id=idem_key,
                        goal="extension retention run",
                        confirmed_scope=True,
                        skill_ids=["s30-skill"],
                    ),
                )
                assert replayed0 is False
                rec = svc._runs_by_id[rview.run_id]
                for _ in range(200):
                    if rec.extension_runtime is not None:
                        for ep in rec.extension_runtime.list_proposals():
                            if ep.state == "pending":
                                await svc.decide_extension_proposal(
                                    session_id=sview.session_id,
                                    run_id=rview.run_id,
                                    proposal_id=ep.id,
                                    request={"digest": ep.digest, "decision": "approve"},  # type: ignore[arg-type]
                                )
                    if rec.settled_event.is_set():
                        break
                    await asyncio.sleep(0.01)
                await asyncio.wait_for(rec.settled_event.wait(), timeout=8.0)

                # Flood >64 extension tool_result events (>512 KiB total) into the run's persisted event log
                base_seq = rec.view.last_seq + 10
                for idx in range(70):
                    ev = AppEvent(
                        schema_version="1",
                        project_id=pview.project_id,
                        session_id=sview.session_id,
                        run_id=rview.run_id,
                        seq=base_seq + idx,
                        kind="tool_result",
                        payload={
                            "extension": True,
                            "call_id": f"c-trim-{idx}",
                            "text": "T" * 9000,
                        },
                    )
                    svc._store.append_run_event(event=ev)

                loaded = svc._store.load()
                persisted_evs = loaded["run_events"][rview.run_id]  # type: ignore[index]
                assert len(persisted_evs) <= MAX_PERSISTED_EVENTS_PER_RUN
                total_ev_bytes = sum(len(json.dumps(e).encode("utf-8")) for e in persisted_evs)
                assert total_ev_bytes <= MAX_PERSISTED_EVENTS_BYTES_PER_RUN

                # History items (with extension summary) and run_idempotency record remain intact
                hist = svc.get_session_history(session_id=sview.session_id)
                assert len(hist.items) == 2
                assert any("s30-skill" in it.text for it in hist.items)
                replay_view, replayed1 = await svc.start_run_with_replay(
                    session_id=sview.session_id,
                    request=StartRunRequest(
                        client_request_id=idem_key,
                        goal="extension retention run",
                        confirmed_scope=True,
                        skill_ids=["s30-skill"],
                    ),
                )
                assert replayed1 is True
                assert replay_view.run_id == rview.run_id
                used_bytes, limit_bytes = svc._store.storage_stats()
                assert used_bytes <= limit_bytes

                # F04-S30: non-empty registered-project file fingerprint and persisted semantic content,
                # captured before close for the reopen comparison below.
                proj_fingerprint = tree_hashes(proj)
                hist_before = [it.text for it in svc.get_session_history(session_id=sview.session_id).items]
            finally:
                await svc.aclose()
            return proj_fingerprint, hist_before, sview.session_id

        proj_fingerprint, hist_before, sess_id = asyncio.run(_check_session_and_store_retention())

        # F04-S30: reopen on the same data_root and confirm the project fingerprint is unchanged and the
        # persisted session semantic content is still readable and equal (without comparing DB bytes).
        async def _reopen_verify() -> None:
            svc2 = AppService(
                config=host_cfg,
                provider_factory=lambda *, project, run_id: CallbackProjectProvider(_decide_s30),
            )
            try:
                assert tree_hashes(proj) == proj_fingerprint
                hist2 = svc2.get_session_history(session_id=sess_id)
                assert [it.text for it in hist2.items] == hist_before
            finally:
                await svc2.aclose()

        asyncio.run(_reopen_verify())

        # F04-S30: inventory this run's persistent output relative paths and verify no permanent RPC log
        # by CONTENT (not filename), pruning reparse points (junctions/symlinks) before traversal.
        def _is_reparse(path: Path) -> bool:
            try:
                st = os.lstat(path)
            except OSError:
                return False
            return bool(getattr(st, "st_file_attributes", 0) & 0x400)

        run_roots = [host_cfg.data_root, host_cfg.workspace_parent]
        rel_outputs: list[tuple[Path, str]] = []

        def _walk_out(d: Path, base: Path) -> None:
            for entry in sorted(d.iterdir(), key=lambda e: str(e)):
                if _is_reparse(entry):
                    continue
                if entry.is_dir():
                    _walk_out(entry, base)
                elif entry.is_file():
                    rel_outputs.append((base, str(entry.relative_to(base))))

        for root in run_roots:
            if root.exists():
                _walk_out(root, root)

        assert rel_outputs, "no persistent output paths inventoried"
        rpc_markers = (b'"jsonrpc"', b'"tools/call"', b'"notifications/initialized"')
        for base, rel in rel_outputs:
            head = (base / rel).read_bytes()[:8192]
            if b"SQLite format" in head:
                continue
            if any(m in head for m in rpc_markers):
                raise AssertionError(f"permanent RPC log found by content: {rel}")
