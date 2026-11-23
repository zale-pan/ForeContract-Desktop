"""A21: Real HTTP contract routes, error envelope sanitization, real TCP SSE, and redaction."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from app_helpers import (
    CREDENTIAL_ENV_CANARY,
    CountingMetricsProvider,
    LiveTcpTestServer,
    auth_headers,
    ensure_shared_child_env_observation,
    make_host_config,
    tree_hashes,
    wait_until,
)
from forecontract_app.models import AppError
from forecontract_app.server import create_app
from forecontract_llm import ModelRequest, ModelResponse


def test_d01_body_preservation(tmp_path: Path) -> None:
    cfg = make_host_config(tmp_path)
    with LiveTcpTestServer(config=cfg) as srv:
        h = auth_headers(cfg)
        h_get = auth_headers(cfg, with_origin=False)

        health = httpx.get(f"{srv.base_url}/api/v1/health", headers=h_get, timeout=5.0)
        assert health.status_code == 200

        created = httpx.post(
            f"{srv.base_url}/api/v1/projects",
            json={"name": "body-positive"},
            headers=h,
            timeout=5.0,
        )
        assert created.status_code == 201
        assert (cfg.workspace_parent / "body-positive" / "metrics.py").is_file()

        # Verify padded goal within 64 KiB JSON limit preserves raw whitespace text end-to-end
        padded_proj = httpx.post(
            f"{srv.base_url}/api/v1/projects",
            json={"name": "body-padded"},
            headers=h,
            timeout=5.0,
        )
        assert padded_proj.status_code == 201
        padded_sid = httpx.post(
            f"{srv.base_url}/api/v1/sessions",
            json={
                "project_id": padded_proj.json()["project_id"],
                "model": {"provider": "offline", "model": "offline-metrics-v1"},
            },
            headers=h,
            timeout=5.0,
        ).json()["session_id"]

        goal = " " * 9000 + "x"
        assert len(goal.strip()) == 1
        padded_goal_http = httpx.post(
            f"{srv.base_url}/api/v1/sessions/{padded_sid}/runs",
            json={
                "client_request_id": str(uuid.uuid4()),
                "goal": goal,
                "confirmed_scope": True,
            },
            headers=h,
            timeout=5.0,
        )
        assert padded_goal_http.status_code == 202
        assert padded_goal_http.json()["goal"] == goal
        padded_rid = padded_goal_http.json()["run_id"]
        wait_until(
            lambda: httpx.get(
                f"{srv.base_url}/api/v1/sessions/{padded_sid}/runs/{padded_rid}",
                headers=h_get,
                timeout=5.0,
            ).json()["state"]
            in ("succeeded", "failed", "cancelled", "unknown"),
            timeout=30.0,
        )

        def _snapshot_ids() -> tuple[list[str], dict[str, list[str]]]:
            projs = [p["project_id"] for p in httpx.get(f"{srv.base_url}/api/v1/projects", headers=h_get, timeout=5.0).json()]
            sess_runs: dict[str, list[str]] = {}
            for s in httpx.get(f"{srv.base_url}/api/v1/sessions", headers=h_get, timeout=5.0).json():
                sid_item = s["session_id"]
                snap_r = httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{sid_item}/runs/{s['active_run_id']}",
                    headers=h_get,
                    timeout=5.0,
                )
                sess_runs[sid_item] = [snap_r.json()["run_id"]] if snap_r.status_code == 200 else []
            return sorted(projs), sess_runs

        ids_before_invalid_requests = _snapshot_ids()

        bad_uuid_http = httpx.post(
            f"{srv.base_url}/api/v1/sessions/{padded_sid}/runs",
            json={"client_request_id": 12345, "goal": "bad uuid", "confirmed_scope": True},
            headers=h,
            timeout=5.0,
        )
        assert bad_uuid_http.status_code == 422

        extra_field_request = httpx.post(
            f"{srv.base_url}/api/v1/projects",
            json={"name": "extra-rejected", "unexpected": True},
            headers=h,
            timeout=5.0,
        )
        assert extra_field_request.status_code == 422

        oversized_declared_body = httpx.post(
            f"{srv.base_url}/api/v1/projects",
            headers=h | {"Content-Type": "application/json"},
            content=json.dumps({"name": "a" * 70000}).encode("utf-8"),
            timeout=5.0,
        )
        assert oversized_declared_body.status_code == 413

        project_and_run_ids_after_invalid_requests = _snapshot_ids()
        assert project_and_run_ids_after_invalid_requests == ids_before_invalid_requests

    # Real ASGI http.request chunked message stream WITHOUT Content-Length exceeding 64 KiB on same host
    async def _run_chunked_asgi_negative() -> None:
        app = create_app(config=cfg)
        async with app.router.lifespan_context(app):
            service = app.state.service
            ids_before = (
                sorted(p.project_id for p in service.list_projects()),
                {
                    s.session_id: ([s.active_run_id] if s.active_run_id else [])
                    for s in service.list_sessions()
                },
            )
            assert (cfg.data_root / ".host.lock").is_file()
            files_before = tree_hashes(cfg.data_root, exclude_relative={".host.lock"})
            ws_files_before = tree_hashes(cfg.workspace_parent)

            chunk_body = b'{"name":"' + (b"b" * 70000) + b'"}'
            chunk_size = 4096
            chunks = [chunk_body[i : i + chunk_size] for i in range(0, len(chunk_body), chunk_size)]
            consumed = 0
            sent_messages: list[dict[str, Any]] = []

            async def receive() -> dict[str, Any]:
                nonlocal consumed
                if consumed < len(chunks):
                    idx = consumed
                    consumed += 1
                    return {
                        "type": "http.request",
                        "body": chunks[idx],
                        "more_body": idx < len(chunks) - 1,
                    }
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message: dict[str, Any]) -> None:
                sent_messages.append(dict(message))

            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/api/v1/projects",
                "raw_path": b"/api/v1/projects",
                "root_path": "",
                "query_string": b"",
                "server": ("127.0.0.1", cfg.bind_port),
                "client": ("127.0.0.1", 1234),
                "headers": [
                    (b"host", f"127.0.0.1:{cfg.bind_port}".encode("ascii")),
                    (b"origin", f"http://127.0.0.1:{cfg.bind_port}".encode("ascii")),
                    (b"authorization", f"Bearer {cfg.access_token}".encode("ascii")),
                    (b"content-type", b"application/json"),
                    (b"transfer-encoding", b"chunked"),
                ],
            }
            await app(scope, receive, send)

            response_start = next(m for m in sent_messages if m["type"] == "http.response.start")
            response_body = b"".join(
                m.get("body", b"") for m in sent_messages if m["type"] == "http.response.body"
            )
            assert response_start["status"] == 413
            assert json.loads(response_body)["error"]["code"] == "PAYLOAD_TOO_LARGE"

            ids_after = (
                sorted(p.project_id for p in service.list_projects()),
                {
                    s.session_id: ([s.active_run_id] if s.active_run_id else [])
                    for s in service.list_sessions()
                },
            )
            assert ids_after == ids_before
            assert (cfg.data_root / ".host.lock").is_file()
            assert tree_hashes(cfg.data_root, exclude_relative={".host.lock"}) == files_before
            assert tree_hashes(cfg.workspace_parent) == ws_files_before

    asyncio.run(_run_chunked_asgi_negative())


def test_a21_routes_and_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Ensure credential env canary exists only in parent env and is never leaked to child/HTTP/events
    monkeypatch.setenv("OPENAI_API_KEY", CREDENTIAL_ENV_CANARY)
    assert os.environ.get("OPENAI_API_KEY") == CREDENTIAL_ENV_CANARY
    cfg = make_host_config(tmp_path)

    gate = threading.Event()
    entered_second_step = threading.Event()

    class _StreamingGateProvider:
        def __init__(self) -> None:
            self._inner = CountingMetricsProvider()
            self._count = 0

        async def complete(self, request: ModelRequest) -> ModelResponse:
            self._count += 1
            if self._count == 2:
                entered_second_step.set()
                await asyncio.to_thread(gate.wait, 10.0)
            return await self._inner.complete(request)

    with LiveTcpTestServer(config=cfg, provider_factory=lambda **_: _StreamingGateProvider()) as srv:
        h = auth_headers(cfg)
        h_get = auth_headers(cfg, with_origin=False)

        # GET /projects initially empty
        p_list = httpx.get(f"{srv.base_url}/api/v1/projects", headers=h_get, timeout=5.0)
        assert p_list.status_code == 200 and p_list.json() == []

        # 422 invalid project name
        bad_proj = httpx.post(
            f"{srv.base_url}/api/v1/projects",
            headers=h,
            json={"name": "../BadName"},
            timeout=5.0,
        )
        assert bad_proj.status_code == 422
        assert bad_proj.json()["error"]["code"] == "INVALID_INPUT"

        # 422 unknown extra HTTP field
        extra_field_resp = httpx.post(
            f"{srv.base_url}/api/v1/projects",
            headers=h,
            json={"name": "http-extra", "extra_key": "forbidden"},
            timeout=5.0,
        )
        assert extra_field_resp.status_code == 422
        assert extra_field_resp.json()["error"]["code"] == "INVALID_INPUT"

        # 201 valid project
        proj = httpx.post(
            f"{srv.base_url}/api/v1/projects",
            headers=h,
            json={"name": "http-demo"},
            timeout=5.0,
        )
        assert proj.status_code == 201
        pid = proj.json()["project_id"]

        # 404 unknown project / session / run
        assert (
            httpx.post(
                f"{srv.base_url}/api/v1/sessions",
                headers=h,
                json={
                    "project_id": str(uuid.uuid4()),
                    "model": {"provider": "offline", "model": "offline-metrics-v1"},
                },
                timeout=5.0,
            ).status_code
            == 404
        )
        assert (
            httpx.get(
                f"{srv.base_url}/api/v1/sessions/{uuid.uuid4()}/runs/{uuid.uuid4()}",
                headers=h_get,
                timeout=5.0,
            ).status_code
            == 404
        )

        # 201 session + GET /sessions
        sess = httpx.post(
            f"{srv.base_url}/api/v1/sessions",
            headers=h,
            json={"project_id": pid, "model": {"provider": "offline", "model": "offline-metrics-v1"}},
            timeout=5.0,
        )
        assert sess.status_code == 201
        sid = sess.json()["session_id"]
        assert len(httpx.get(f"{srv.base_url}/api/v1/sessions", headers=h_get, timeout=5.0).json()) == 1

        # 500 internal error sanitization via failing internal handler hook
        def _boom_snapshot(*args: object, **kwargs: object) -> object:
            raise RuntimeError("SENSITIVE_STACK_PATH_C:/secret/token")

        orig_snap = srv.app.state.service.snapshot
        monkeypatch.setattr(srv.app.state.service, "snapshot", _boom_snapshot)
        r_500 = httpx.get(
            f"{srv.base_url}/api/v1/sessions/{sid}/runs/{uuid.uuid4()}/snapshot",
            headers=h_get,
            timeout=5.0,
        )
        assert r_500.status_code == 500
        assert r_500.json() == {"error": {"code": "INTERNAL_ERROR", "message": "internal server error"}}
        monkeypatch.setattr(srv.app.state.service, "snapshot", orig_snap)
        _ = AppError  # keep symbol imported cleanly

        # Start run and verify REAL TCP streaming SSE delivers early frames BEFORE run_finished arrives!
        start_resp = httpx.post(
            f"{srv.base_url}/api/v1/sessions/{sid}/runs",
            headers=h,
            json={"client_request_id": str(uuid.uuid4()), "goal": "stream test", "confirmed_scope": True},
            timeout=5.0,
        )
        assert start_resp.status_code == 202
        rid = start_resp.json()["run_id"]
        assert entered_second_step.wait(timeout=10.0)

        received_events: list[dict[str, object]] = []
        progress_before_finished = False
        with (
            httpx.Client(timeout=15.0) as client,
            client.stream(
                "GET",
                f"{srv.base_url}/api/v1/sessions/{sid}/runs/{rid}/events?after=0",
                headers=h_get,
            ) as stream_resp,
        ):
                assert stream_resp.status_code == 200
                cur_event = ""
                for line in stream_resp.iter_lines():
                    if line.startswith("event:"):
                        cur_event = line[6:].strip()
                    elif line.startswith("data:"):
                        data_obj = json.loads(line[5:].strip())
                        received_events.append(data_obj)
                        if cur_event in ("run_started", "planning_started", "tool_result", "core_trace"):
                            progress_before_finished = True
                            gate.set()
                        if cur_event == "run_finished":
                            break

        assert progress_before_finished is True
        assert received_events[-1]["kind"] == "run_finished"
        snap_resp = httpx.get(
            f"{srv.base_url}/api/v1/sessions/{sid}/runs/{rid}/snapshot",
            headers=h_get,
            timeout=5.0,
        )
        assert snap_resp.status_code == 200
        assert snap_resp.json()["run"]["state"] == "succeeded"

        # 200 idempotent terminal cancel returns original succeeded state (never cancelled)
        cancel_term = httpx.post(
            f"{srv.base_url}/api/v1/sessions/{sid}/runs/{rid}/cancel",
            headers=h,
            json={},
            timeout=5.0,
        )
        assert cancel_term.status_code == 200
        assert cancel_term.json()["state"] == "succeeded"

        # Redaction check across all persisted data_root files and received HTTP/SSE payloads
        assert (cfg.data_root / ".host.lock").is_file()
        all_texts = [snap_resp.text, cancel_term.text, json.dumps(received_events)]
        for rel in tree_hashes(cfg.data_root, exclude_relative={".host.lock"}):
            p = cfg.data_root / rel
            all_texts.append(p.read_bytes().decode("utf-8", errors="ignore"))
        assert (cfg.data_root / ".host.lock").is_file()
        joined = "\n".join(all_texts)
        assert cfg.access_token not in joined
        assert CREDENTIAL_ENV_CANARY not in joined

    child_env_obs = ensure_shared_child_env_observation()
    assert child_env_obs["OPENAI_API_KEY_present"] is False
