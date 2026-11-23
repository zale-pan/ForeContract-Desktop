"""C24–C27: HTTP pagination/validation/context conflicts, security/redaction, SSE tail, and terminal DB failure."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import project_helpers
import pytest
from app_helpers import (
    CREDENTIAL_ENV_CANARY,
    FAKE_TEST_TOKEN,
    LiveTcpTestServer,
    auth_headers,
    make_host_config,
)
from forecontract_app.models import ProjectView
from forecontract_app.project_demo import OfflineProjectProvider
from forecontract_llm import ModelRequest, ModelUsage
from session_helpers import (
    SpyUsageProvider,
    forecast_json,
    make_reopened_host_config,
)


def _auto_approve_project_run_until_terminal(
    client: httpx.Client,
    headers: dict[str, str],
    *,
    sid: str,
    rid: str,
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Synchronously approve pending scope/verification/command proposals and return terminal RunView."""
    approved: set[str] = set()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rv = client.get(f"/api/v1/sessions/{sid}/runs/{rid}", headers=headers)
        assert rv.status_code == 200, rv.text
        run_data = rv.json()
        props = client.get(f"/api/v1/sessions/{sid}/runs/{rid}/proposals", headers=headers)
        assert props.status_code == 200, props.text
        for prop in props.json():
            pid = prop["id"]
            if prop["state"] == "pending" and pid not in approved:
                approved.add(pid)
                dec = client.post(
                    f"/api/v1/sessions/{sid}/runs/{rid}/proposals/{pid}/decision",
                    headers=headers,
                    json={
                        "digest": prop["digest"],
                        "decision": "approve",
                        "trusted_local_execution": prop["kind"] == "command",
                    },
                )
                assert dec.status_code == 200, dec.text
        if run_data["state"] in ("succeeded", "failed", "cancelled", "unknown"):
            return run_data
        time.sleep(0.03)
    raise AssertionError(f"timed out waiting for run {rid}")


def _wait_for_run_terminal_sync(
    client: httpx.Client,
    headers: dict[str, str],
    *,
    sid: str,
    rid: str,
    timeout: float = 15.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rv = client.get(f"/api/v1/sessions/{sid}/runs/{rid}", headers=headers)
        assert rv.status_code == 200, rv.text
        data = rv.json()
        if data["state"] in ("succeeded", "failed", "cancelled", "unknown"):
            return data
        time.sleep(0.03)
    raise AssertionError(f"timed out waiting for run {rid}")


def _calc_fix_provider(*, project: ProjectView, run_id: str) -> SpyUsageProvider:
    _ = (project, run_id)
    inner = OfflineProjectProvider()

    def _build(_idx: int, req: ModelRequest) -> str:
        body = inner._next_step(req)
        assert body is not None
        return json.dumps(body, ensure_ascii=False)

    return SpyUsageProvider(
        _build,
        usage_fn=lambda i, _r: ModelUsage(
            schema_version="0.1", input_tokens=10 + i, output_tokens=5 + i
        ),
    )


def test_c24_http_history_pagination_validation_and_constraints(tmp_path: Path) -> None:
    proj_dir = project_helpers.create_buggy_calc_project(tmp_path / "c24-proj")
    cfg = project_helpers.make_project_host_config(tmp_path, allowed_roots=(proj_dir,))

    # Quick offline provider finishing in 1 step for fast multi-run history generation,
    # plus one scripted ask_user run to verify SESSION_BUSY while waiting for user input.
    ask_started = False
    ask_q_text = "Clarify formatting?"

    def _factory(*, project: ProjectView, run_id: str) -> SpyUsageProvider:
        _ = (project, run_id)
        if ask_started:

            def _ask_build(idx: int, req: ModelRequest) -> str:
                _ = req
                if idx == 0:
                    return forecast_json(
                        action_type="ask_user",
                        payload={"question": ask_q_text, "options": ["keep", "trim"]},
                    )
                return forecast_json(
                    action_type="inspect_file",
                    payload={"path": "calc.py"},
                    outcome_type="project_tool_result",
                    outcome_payload={"action_type": "inspect_file", "require_success": False},
                    task_hint=True,
                )

            return SpyUsageProvider(_ask_build)
        return SpyUsageProvider(
            lambda _i, _r: forecast_json(
                action_type="inspect_file",
                payload={"path": "calc.py"},
                outcome_type="project_tool_result",
                outcome_payload={"action_type": "inspect_file", "require_success": False},
                task_hint=True,
            )
        )

    with LiveTcpTestServer(config=cfg, provider_factory=_factory) as srv:
        headers = auth_headers(cfg)
        with httpx.Client(base_url=srv.base_url, timeout=10.0) as client:
            reg = client.post(
                "/api/v1/projects/register",
                headers=headers,
                json={"name": "c24-proj", "root": str(proj_dir), "confirmed_read": True},
            )
            assert reg.status_code == 201, reg.text
            pid = reg.json()["project_id"]

            sess = client.post(
                "/api/v1/sessions",
                headers=headers,
                json={
                    "project_id": pid,
                    "model": {"provider": "offline", "model": "offline-project-v1", "thinking": "disabled"},
                },
            )
            assert sess.status_code == 201, sess.text
            sid = sess.json()["session_id"]

            # Execute 4 short rounds -> 8 durable history items (user_goal + run_result each)
            for i in range(4):
                run_resp = client.post(
                    f"/api/v1/sessions/{sid}/runs",
                    headers=headers,
                    json={
                        "client_request_id": str(uuid.uuid4()),
                        "goal": f"round-{i + 1}-inspect",
                        "confirmed_scope": True,
                    },
                )
                assert run_resp.status_code == 202, run_resp.text
                rid = run_resp.json()["run_id"]
                _wait_for_run_terminal_sync(client, headers, sid=sid, rid=rid)

            # Page size 3 across 8 items -> 3 + 3 + 2
            p1a = client.get(f"/api/v1/sessions/{sid}/history?after=0&limit=3", headers=headers)
            p1b = client.get(f"/api/v1/sessions/{sid}/history?after=0&limit=3", headers=headers)
            assert p1a.status_code == 200 and p1b.status_code == 200
            assert p1a.json() == p1b.json()
            assert len(p1a.json()["items"]) == 3
            assert p1a.json()["has_more"] is True

            collected = list(p1a.json()["items"])
            cursor = p1a.json()["cursor"]
            while True:
                page = client.get(
                    f"/api/v1/sessions/{sid}/history?after={cursor}&limit=3", headers=headers
                )
                assert page.status_code == 200, page.text
                body = page.json()
                collected.extend(body["items"])
                cursor = body["cursor"]
                if not body["has_more"]:
                    break

            assert len(collected) == 8
            seqs = [item["seq"] for item in collected]
            assert seqs == list(range(1, 9))

            # Validation rejections (422)
            for bad_query in ("after=-1", "after=%2B1", "after=true", "after=99", "limit=0", "limit=101", "limit=false"):
                bad = client.get(f"/api/v1/sessions/{sid}/history?{bad_query}", headers=headers)
                assert bad.status_code == 422, f"{bad_query} returned {bad.status_code}: {bad.text}"

            # NaN / Infinity JSON rejected with 422
            for raw_bad in (
                '{"client_request_id": "' + str(uuid.uuid4()) + '", "expected_revision": NaN, "constraints": "a"}',
                '{"client_request_id": "' + str(uuid.uuid4()) + '", "expected_revision": Infinity, "constraints": "a"}',
            ):
                bad_json = client.put(
                    f"/api/v1/sessions/{sid}/context",
                    headers={**headers, "Content-Type": "application/json"},
                    content=raw_bad.encode("utf-8"),
                )
                assert bad_json.status_code == 422, bad_json.text

            # PUT /context revision + idempotency semantics
            req1 = str(uuid.uuid4())
            c1 = client.put(
                f"/api/v1/sessions/{sid}/context",
                headers=headers,
                json={"client_request_id": req1, "expected_revision": 0, "constraints": "only calc.py"},
            )
            assert c1.status_code == 200, c1.text
            assert c1.json()["revision"] == 1
            assert c1.json()["constraints"] == "only calc.py"

            # Identical retry -> 200 without incrementing revision
            c1_retry = client.put(
                f"/api/v1/sessions/{sid}/context",
                headers=headers,
                json={"client_request_id": req1, "expected_revision": 0, "constraints": "only calc.py"},
            )
            assert c1_retry.status_code == 200
            assert c1_retry.json() == c1.json()

            # Same client_request_id with different body -> 409 IDEMPOTENCY_CONFLICT
            c1_diff = client.put(
                f"/api/v1/sessions/{sid}/context",
                headers=headers,
                json={"client_request_id": req1, "expected_revision": 0, "constraints": "mutated"},
            )
            assert c1_diff.status_code == 409
            assert c1_diff.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

            # Stale expected_revision -> 409 REVISION_CONFLICT
            stale = client.put(
                f"/api/v1/sessions/{sid}/context",
                headers=headers,
                json={"client_request_id": str(uuid.uuid4()), "expected_revision": 0, "constraints": "stale"},
            )
            assert stale.status_code == 409
            assert stale.json()["error"]["code"] == "REVISION_CONFLICT"

            # Start run waiting on ask_user -> PUT /context rejected 409 SESSION_BUSY
            ask_started = True
            active_run = client.post(
                f"/api/v1/sessions/{sid}/runs",
                headers=headers,
                json={
                    "client_request_id": str(uuid.uuid4()),
                    "goal": "wait on ask_user",
                    "confirmed_scope": True,
                },
            )
            assert active_run.status_code == 202
            active_rid = active_run.json()["run_id"]

            q_list: list[dict[str, Any]] = []
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                qr = client.get(
                    f"/api/v1/sessions/{sid}/runs/{active_rid}/questions", headers=headers
                )
                assert qr.status_code == 200
                q_list = qr.json()
                if q_list and q_list[0]["state"] == "pending":
                    break
                time.sleep(0.03)
            assert q_list and q_list[0]["state"] == "pending"

            busy = client.put(
                f"/api/v1/sessions/{sid}/context",
                headers=headers,
                json={"client_request_id": str(uuid.uuid4()), "expected_revision": 1, "constraints": "busy"},
            )
            assert busy.status_code == 409
            assert busy.json()["error"]["code"] == "SESSION_BUSY"

            # Cleanly answer and finish
            q0 = q_list[0]
            ans = client.post(
                f"/api/v1/sessions/{sid}/runs/{active_rid}/questions/{q0['id']}/answer",
                headers=headers,
                json={
                    "client_request_id": str(uuid.uuid4()),
                    "question_digest": q0["digest"],
                    "text": "keep",
                },
            )
            assert ans.status_code == 200, ans.text
            assert ans.json()["answer"] == "keep"
            _wait_for_run_terminal_sync(client, headers, sid=sid, rid=active_rid)


def test_c25_http_security_boundaries_xss_and_canary_redaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", CREDENTIAL_ENV_CANARY)
    proj_dir = project_helpers.create_buggy_calc_project(tmp_path / "c25-proj")
    cfg = project_helpers.make_project_host_config(tmp_path, allowed_roots=(proj_dir,))
    xss_goal = '<script>alert("xss-goal")</script>'
    xss_constraint = '<img src=x onerror="alert(2)">'
    xss_answer = '<svg onload=alert(3)>user answer'

    def _factory(*, project: ProjectView, run_id: str) -> SpyUsageProvider:
        _ = (project, run_id)

        def _build(idx: int, req: ModelRequest) -> str:
            _ = req
            if idx == 0:
                return forecast_json(
                    action_type="ask_user",
                    payload={"question": '<b onclick="xss()">Which?</b>', "options": ["A", "B"]},
                )
            return forecast_json(
                action_type="inspect_file",
                payload={"path": "calc.py"},
                outcome_type="project_tool_result",
                outcome_payload={"action_type": "inspect_file", "require_success": False},
                task_hint=True,
            )

        return SpyUsageProvider(
            _build,
            usage_fn=lambda i, _r: ModelUsage(schema_version="0.1", input_tokens=15, output_tokens=5),
        )

    with LiveTcpTestServer(config=cfg, provider_factory=_factory) as srv:
        headers = auth_headers(cfg)
        with httpx.Client(base_url=srv.base_url, timeout=10.0) as client:
            reg = client.post(
                "/api/v1/projects/register",
                headers=headers,
                json={"name": "c25-proj", "root": str(proj_dir), "confirmed_read": True},
            )
            assert reg.status_code == 201
            pid = reg.json()["project_id"]

            sess = client.post(
                "/api/v1/sessions",
                headers=headers,
                json={
                    "project_id": pid,
                    "model": {"provider": "offline", "model": "offline-project-v1", "thinking": "disabled"},
                },
            )
            assert sess.status_code == 201
            sid = sess.json()["session_id"]

            ctx_put = client.put(
                f"/api/v1/sessions/{sid}/context",
                headers=headers,
                json={
                    "client_request_id": str(uuid.uuid4()),
                    "expected_revision": 0,
                    "constraints": xss_constraint,
                },
            )
            assert ctx_put.status_code == 200
            assert ctx_put.json()["constraints"] == xss_constraint

            run_resp = client.post(
                f"/api/v1/sessions/{sid}/runs",
                headers=headers,
                json={
                    "client_request_id": str(uuid.uuid4()),
                    "goal": xss_goal,
                    "confirmed_scope": True,
                },
            )
            assert run_resp.status_code == 202
            rid = run_resp.json()["run_id"]

            q0: dict[str, Any] | None = None
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                ql = client.get(f"/api/v1/sessions/{sid}/runs/{rid}/questions", headers=headers).json()
                if ql:
                    q0 = ql[0]
                    break
                time.sleep(0.03)
            assert q0 is not None

            ans_url = f"/api/v1/sessions/{sid}/runs/{rid}/questions/{q0['id']}/answer"

            # Check security boundaries on all new routes
            protected_gets = [
                f"/api/v1/sessions/{sid}/history",
                f"/api/v1/sessions/{sid}/context",
                f"/api/v1/sessions/{sid}/runs/{rid}/questions",
                f"/api/v1/sessions/{sid}/runs/{rid}/usage",
            ]
            error_bodies: list[str] = []
            for path in protected_gets:
                no_auth = client.get(path, headers={"Host": f"127.0.0.1:{cfg.bind_port}"})
                assert no_auth.status_code == 401
                error_bodies.append(no_auth.text)

                wrong_auth = client.get(
                    path,
                    headers={
                        "Host": f"127.0.0.1:{cfg.bind_port}",
                        "Authorization": "Bearer wrong-token",
                    },
                )
                assert wrong_auth.status_code == 401
                error_bodies.append(wrong_auth.text)

                bad_host = client.get(
                    path,
                    headers={
                        "Host": f"evil.example:{cfg.bind_port}",
                        "Authorization": f"Bearer {FAKE_TEST_TOKEN}",
                    },
                )
                assert bad_host.status_code == 403
                error_bodies.append(bad_host.text)

                bad_origin = client.get(
                    path,
                    headers={**headers, "Origin": "http://evil.example"},
                )
                assert bad_origin.status_code == 403
                error_bodies.append(bad_origin.text)

                cross_site = client.get(
                    path,
                    headers={**headers, "Sec-Fetch-Site": "cross-site"},
                )
                assert cross_site.status_code == 403
                error_bodies.append(cross_site.text)

            # 413 oversized body on PUT /context & POST .../answer
            oversized = client.put(
                f"/api/v1/sessions/{sid}/context",
                headers={**headers, "Content-Type": "application/json"},
                content=(b'{"constraints":"' + b"x" * (70 * 1024) + b'"}'),
            )
            assert oversized.status_code == 413
            oversized_ans = client.post(
                ans_url,
                headers={**headers, "Content-Type": "application/json"},
                content=(b'{"text":"' + b"y" * (70 * 1024) + b'"}'),
            )
            assert oversized_ans.status_code == 413

            # Answer with XSS payload -> returned verbatim as JSON data
            ans = client.post(
                ans_url,
                headers=headers,
                json={
                    "client_request_id": str(uuid.uuid4()),
                    "question_digest": q0["digest"],
                    "text": xss_answer,
                },
            )
            assert ans.status_code == 200
            assert ans.json()["answer"] == xss_answer
            _wait_for_run_terminal_sync(client, headers, sid=sid, rid=rid)

            # Check XSS strings persisted verbatim as data
            hist = client.get(f"/api/v1/sessions/{sid}/history", headers=headers).json()
            texts = [it["text"] for it in hist["items"]]
            assert any(xss_goal in t for t in texts)
            assert any(xss_answer in t for t in texts)
            ctx_now = client.get(f"/api/v1/sessions/{sid}/context", headers=headers).json()
            assert ctx_now["constraints"] == xss_constraint

            usage_resp = client.get(f"/api/v1/sessions/{sid}/runs/{rid}/usage", headers=headers)
            assert usage_resp.status_code == 200
            usage_text = usage_resp.text

            # Verify canary/token never appear in SQLite, history, error responses, or usage
            raw_db = (cfg.data_root / "sessions.sqlite3").read_bytes()
            for needle in (FAKE_TEST_TOKEN, CREDENTIAL_ENV_CANARY):
                assert needle.encode("utf-8") not in raw_db
                assert needle not in json.dumps(hist)
                assert needle not in usage_text
                for eb in error_bodies:
                    assert needle not in eb


def test_c26_durable_event_tail_cursor_expired_and_single_run_finished(tmp_path: Path) -> None:
    proj_dir = project_helpers.create_buggy_calc_project(tmp_path / "c26-proj")
    cfg = make_host_config(tmp_path, max_events_per_run=5)
    # Add allowed_roots by reconstructing with project_roots
    cfg = make_reopened_host_config(
        cfg,
        port=cfg.bind_port,
        token=cfg.access_token,
        project_roots=(proj_dir,),
    )
    created_providers: list[SpyUsageProvider] = []

    def _factory(*, project: ProjectView, run_id: str) -> SpyUsageProvider:
        prov = _calc_fix_provider(project=project, run_id=run_id)
        created_providers.append(prov)
        return prov

    sid: str
    rid: str
    snap_before: dict[str, Any]
    with LiveTcpTestServer(config=cfg, provider_factory=_factory) as srv:
        headers = auth_headers(cfg)
        with httpx.Client(base_url=srv.base_url, timeout=15.0) as client:
            reg = client.post(
                "/api/v1/projects/register",
                headers=headers,
                json={"name": "c26-proj", "root": str(proj_dir), "confirmed_read": True},
            )
            assert reg.status_code == 201
            pid = reg.json()["project_id"]
            sess = client.post(
                "/api/v1/sessions",
                headers=headers,
                json={
                    "project_id": pid,
                    "model": {"provider": "offline", "model": "offline-project-v1", "thinking": "disabled"},
                },
            )
            assert sess.status_code == 201
            sid = sess.json()["session_id"]
            run_resp = client.post(
                f"/api/v1/sessions/{sid}/runs",
                headers=headers,
                json={"client_request_id": str(uuid.uuid4()), "goal": "Fix calc", "confirmed_scope": True},
            )
            assert run_resp.status_code == 202
            rid = run_resp.json()["run_id"]
            fin = _auto_approve_project_run_until_terminal(client, headers, sid=sid, rid=rid)
            assert fin["state"] == "succeeded"

            snap_resp = client.get(
                f"/api/v1/sessions/{sid}/runs/{rid}/snapshot", headers=headers
            )
            assert snap_resp.status_code == 200
            snap_before = snap_resp.json()
            assert snap_before["run"]["last_seq"] == snap_before["cursor"]
            assert snap_before["run"]["truncated_before"] > 1

            # Old cursor < truncated_before -> 409 CURSOR_EXPIRED
            expired = client.get(
                f"/api/v1/sessions/{sid}/runs/{rid}/events?after=1", headers=headers
            )
            assert expired.status_code == 409
            assert expired.json()["error"]["code"] == "CURSOR_EXPIRED"

    assert len(created_providers) == 1

    # Reopen host on same data_root with zero provider calls
    reopen_providers: list[SpyUsageProvider] = []

    def _no_call_factory(*, project: ProjectView, run_id: str) -> SpyUsageProvider:
        prov = _calc_fix_provider(project=project, run_id=run_id)
        reopen_providers.append(prov)
        return prov

    cfg2 = make_reopened_host_config(cfg)
    with LiveTcpTestServer(config=cfg2, provider_factory=_no_call_factory) as srv2:
        headers2 = auth_headers(cfg2)
        with httpx.Client(base_url=srv2.base_url, timeout=10.0) as client2:
            snap_after = client2.get(
                f"/api/v1/sessions/{sid}/runs/{rid}/snapshot", headers=headers2
            )
            assert snap_after.status_code == 200
            s_after = snap_after.json()
            assert s_after["run"]["last_seq"] == s_after["cursor"] == snap_before["cursor"]
            trunc = s_after["run"]["truncated_before"]
            assert trunc == snap_before["run"]["truncated_before"]

            # Still 409 CURSOR_EXPIRED for old cursor after restart
            still_exp = client2.get(
                f"/api/v1/sessions/{sid}/runs/{rid}/events?after={trunc - 1}",
                headers=headers2,
            )
            assert still_exp.status_code == 409
            assert still_exp.json()["error"]["code"] == "CURSOR_EXPIRED"

            # Reconnect at truncated_before -> 200 SSE tail with single run_finished & monotonic ids
            sse = client2.get(
                f"/api/v1/sessions/{sid}/runs/{rid}/events?after={trunc}",
                headers=headers2,
            )
            assert sse.status_code == 200
            lines = sse.text.splitlines()
            ids = [int(ln.split(": ", 1)[1]) for ln in lines if ln.startswith("id: ")]
            events_kinds = [ln.split(": ", 1)[1] for ln in lines if ln.startswith("event: ")]
            assert ids == sorted(ids)
            assert len(set(ids)) == len(ids)
            assert ids[-1] == s_after["run"]["last_seq"]
            assert events_kinds.count("run_finished") == 1

            # Reconnect at cursor -> empty stream, no replay
            sse_empty = client2.get(
                f"/api/v1/sessions/{sid}/runs/{rid}/events?after={s_after['cursor']}",
                headers=headers2,
            )
            assert sse_empty.status_code == 200
            assert sse_empty.text == ""

    assert len(reopen_providers) == 0


def test_c27_terminal_db_failure_returns_unknown_and_keeps_modified_files(tmp_path: Path) -> None:
    proj_dir = project_helpers.create_buggy_calc_project(tmp_path / "c27-proj")
    before_calc = (proj_dir / "calc.py").read_text(encoding="utf-8")
    cfg = project_helpers.make_project_host_config(tmp_path, allowed_roots=(proj_dir,))

    sid: str
    rid: str
    pid: str
    with LiveTcpTestServer(config=cfg, provider_factory=_calc_fix_provider) as srv:
        service = srv.app.state.service
        real_finalize = service._store.finalize_run

        def _failing_finalize(*args: Any, **kwargs: Any) -> int:
            # Put real SQLite connection into query_only mode so SessionStore._write_tx hits genuine SQLite failure
            conn: sqlite3.Connection = service._store._conn
            conn.execute("PRAGMA query_only = ON")
            return real_finalize(*args, **kwargs)

        service._store.finalize_run = _failing_finalize  # type: ignore[method-assign]

        headers = auth_headers(cfg)
        with httpx.Client(base_url=srv.base_url, timeout=15.0) as client:
            reg = client.post(
                "/api/v1/projects/register",
                headers=headers,
                json={"name": "c27-proj", "root": str(proj_dir), "confirmed_read": True},
            )
            assert reg.status_code == 201
            pid = reg.json()["project_id"]

            sess = client.post(
                "/api/v1/sessions",
                headers=headers,
                json={
                    "project_id": pid,
                    "model": {"provider": "offline", "model": "offline-project-v1", "thinking": "disabled"},
                },
            )
            assert sess.status_code == 201
            sid = sess.json()["session_id"]

            sess2 = client.post(
                "/api/v1/sessions",
                headers=headers,
                json={
                    "project_id": pid,
                    "model": {"provider": "offline", "model": "offline-project-v1", "thinking": "disabled"},
                },
            )
            assert sess2.status_code == 201
            sid2 = sess2.json()["session_id"]

            run_resp = client.post(
                f"/api/v1/sessions/{sid}/runs",
                headers=headers,
                json={"client_request_id": str(uuid.uuid4()), "goal": "Fix calc", "confirmed_scope": True},
            )
            assert run_resp.status_code == 202
            rid = run_resp.json()["run_id"]

            fin = _auto_approve_project_run_until_terminal(client, headers, sid=sid, rid=rid)
            assert fin["state"] == "unknown"
            assert fin["verification"] == "unknown"
            assert fin["reason_code"] == "STORE_UNAVAILABLE"

            # Disk file modification was preserved (not rolled back!)
            after_calc = (proj_dir / "calc.py").read_text(encoding="utf-8")
            assert after_calc != before_calc
            assert "return a + b" in after_calc

            # Workspace remains blocked on pre-created session2 while store writes fail
            blocked = client.post(
                f"/api/v1/sessions/{sid2}/runs",
                headers=headers,
                json={"client_request_id": str(uuid.uuid4()), "goal": "blocked", "confirmed_scope": True},
            )
            assert blocked.status_code == 409
            assert blocked.json()["error"]["code"] == "WORKSPACE_BUSY"

    # Reopen on same data_root -> sees only last committed DB facts (never succeeded) & workspace stays blocked
    cfg2 = make_reopened_host_config(cfg)
    with LiveTcpTestServer(config=cfg2, provider_factory=_calc_fix_provider) as srv2:
        headers2 = auth_headers(cfg2)
        with httpx.Client(base_url=srv2.base_url, timeout=10.0) as client2:
            after_run = client2.get(f"/api/v1/sessions/{sid}/runs/{rid}", headers=headers2)
            assert after_run.status_code == 200
            ar = after_run.json()
            assert ar["state"] == "unknown"
            assert ar["verification"] == "unknown"
            assert ar["reason_code"] == "HOST_RESTARTED"

            hist = client2.get(f"/api/v1/sessions/{sid}/history", headers=headers2)
            assert hist.status_code == 200
            kinds = [it["kind"] for it in hist.json()["items"]]
            assert "user_goal" in kinds
            assert "run_result" not in kinds

            sess3 = client2.post(
                "/api/v1/sessions",
                headers=headers2,
                json={
                    "project_id": pid,
                    "model": {"provider": "offline", "model": "offline-project-v1", "thinking": "disabled"},
                },
            )
            assert sess3.status_code == 201
            blocked2 = client2.post(
                f"/api/v1/sessions/{sess3.json()['session_id']}/runs",
                headers=headers2,
                json={"client_request_id": str(uuid.uuid4()), "goal": "still blocked", "confirmed_scope": True},
            )
            assert blocked2.status_code == 409
            assert blocked2.json()["error"]["code"] == "WORKSPACE_BUSY"
