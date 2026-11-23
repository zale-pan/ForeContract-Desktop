"""Browser end-to-end tests for CHAT-T03 unified conversation, execution, approval, and follow-up."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import project_helpers
from forecontract_app.models import HostConfig
from forecontract_app.project_demo import OfflineProjectProvider
from forecontract_app.session_models import SESSION_DB_FILENAME
from playwright.sync_api import expect, sync_playwright
from test_session_browser import LiveTcpTestServer, _launch_chromium


def test_discuss_execute_approve_and_follow_up(tmp_path: Path) -> None:
    import httpx
    from app_helpers import auth_headers

    proj_dir = tmp_path / "conv_proj"
    data_dir = tmp_path / "conv_data"
    project_helpers.create_buggy_calc_project(proj_dir)
    calc_before_sha = project_helpers.sha256_file(proj_dir / "calc.py")

    base_cfg = project_helpers.make_project_host_config(tmp_path, allowed_roots=(proj_dir,))
    cfg = HostConfig(
        data_root=data_dir,
        workspace_parent=data_dir.parent / "workspaces",
        python_executable=base_cfg.python_executable,
        bind_port=base_cfg.bind_port,
        access_token=base_cfg.access_token,
        project_roots=(proj_dir,),
    )

    def _provider_factory(*, project: Any, run_id: str) -> Any:
        _ = project, run_id
        return OfflineProjectProvider()

    with LiveTcpTestServer(config=cfg, provider_factory=_provider_factory) as srv:
        if getattr(srv.app.state.service, "_chat_provider_factory", None) is None:
            srv.app.state.service._chat_provider_factory = lambda **_: (lambda req: {"action": "execution_offer", "goal": req.question, "summary": "执行任务"})
        url = f"{srv.base_url}/#token={cfg.access_token}"
        console_errors: list[str] = []
        page_errors: list[str] = []
        chat_posts: list[str] = []
        run_posts: list[str] = []
        decision_bodies: list[tuple[str, dict[str, Any]]] = []

        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 800})
                page.on(
                    "console",
                    lambda m: console_errors.append(m.text) if m.type == "error" else None,
                )
                page.on("pageerror", lambda err: page_errors.append(str(err)))

                decision_responses: dict[str, dict[str, Any]] = {}

                def _on_request(req) -> None:
                    if req.method != "POST":
                        return
                    if req.url.endswith("/chat/turns"):
                        chat_posts.append(req.url)
                    elif req.url.endswith(("/runs", "/confirm")):
                        run_posts.append(req.url)
                    elif "/proposals/" in req.url and req.url.endswith("/decision"):
                        decision_bodies.append((req.url, json.loads(req.post_data or "{}")))

                def _on_response(resp) -> None:
                    if resp.request.method != "POST":
                        return
                    if "/proposals/" in resp.url and resp.url.endswith("/decision"):
                        pid = resp.url.split("/proposals/")[1].split("/decision")[0]
                        assert resp.status == 200
                        decision_responses[pid] = resp.json()

                page.on("request", _on_request)
                page.on("response", _on_response)
                page.goto(url, wait_until="domcontentloaded")

                # Register local project with valid name and create one session
                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id("project-name").fill("conv-proj")
                page.get_by_test_id("register-root").fill(str(proj_dir))
                page.get_by_test_id("confirm-read").check()
                page.get_by_test_id("register-project").click()
                expect(page.get_by_test_id("project-list")).to_contain_text("conv-proj", timeout=10000)

                page.get_by_test_id("new-session").click()
                expect(page.get_by_test_id("current-session")).not_to_have_text("-", timeout=10000)
                sid = page.get_by_test_id("current-session").inner_text().strip()

                # 1. Read-only discussion turn in the single composer
                page.get_by_test_id("nav-chat").click()
                expect(page.get_by_test_id("pending-approvals-card")).to_be_hidden()
                page.get_by_test_id("chat-input").fill("为什么算错，先别改")
                page.get_by_test_id("chat-send").click()
                expect(page.get_by_test_id("chat-turn-1")).to_be_visible(timeout=15000)
                assert len(chat_posts) == 1
                assert len(run_posts) == 0
                assert project_helpers.sha256_file(proj_dir / "calc.py") == calc_before_sha

                # 2. Type task goal -> send -> offer card appears (0 runs started yet)
                goal_text = "帮我改：修复 calc.py 的加法，并运行项目验证"
                page.get_by_test_id("chat-input").fill(goal_text)
                assert len(run_posts) == 0

                # 3. Send and confirm offer in the SAME session
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                page.locator('[data-testid^="offer-confirm-btn-"]').last.click()
                expect(page.locator('[data-testid^="timeline-run-"]').first).to_be_visible(timeout=15000)
                assert len(run_posts) == 1
                assert f"/sessions/{sid}/" in run_posts[0]

                rid = page.locator('[data-testid^="timeline-run-id-"]').first.get_attribute("data-run-id") or ""
                assert rid
                expect(page.get_by_test_id(f"timeline-run-goal-{rid}")).to_have_text(goal_text)

                # Before approving write scope, calc.py must still be unmodified
                expect(page.get_by_test_id("pending-approvals-card")).to_be_visible(timeout=15000)
                assert project_helpers.sha256_file(proj_dir / "calc.py") == calc_before_sha

                # 4. Approve pending scope, verification, and trusted command sequentially
                pending_snapshots: dict[str, dict[str, Any]] = {}
                approved_pids: set[str] = set()
                for _ in range(120):
                    state_attr = page.get_by_test_id(f"timeline-run-state-{rid}").get_attribute("data-run-state") or ""
                    if state_attr == "succeeded":
                        break
                    host_props = httpx.get(
                        f"{srv.base_url}/api/v1/sessions/{sid}/runs/{rid}/proposals",
                        headers=auth_headers(cfg),
                        timeout=5.0,
                    ).json()
                    pending_by_id = {hp["id"]: hp for hp in host_props if hp.get("state") == "pending"}
                    approve_btns = page.locator('[data-testid^="proposal-approve-"]')
                    count = approve_btns.count()
                    acted = False
                    for i in range(count):
                        btn = approve_btns.nth(i)
                        if not btn.is_visible() or not btn.is_enabled():
                            continue
                        pid = (btn.get_attribute("data-testid") or "").removeprefix("proposal-approve-")
                        if pid in approved_pids or pid not in pending_by_id:
                            continue
                        pending_snapshots[pid] = pending_by_id[pid]
                        trust_cb = page.get_by_test_id(f"proposal-trust-{pid}")
                        if trust_cb.count() > 0 and trust_cb.is_visible() and not trust_cb.is_checked():
                            trust_cb.check()
                        btn.click()
                        approved_pids.add(pid)
                        acted = True
                        break
                    if not acted:
                        page.wait_for_timeout(150)

                expect(page.get_by_test_id(f"timeline-run-state-{rid}")).to_have_attribute(
                    "data-run-state", "succeeded", timeout=20000
                )
                expect(page.get_by_test_id(f"timeline-run-state-{rid}")).to_contain_text(
                    "已完成 · 验证通过", timeout=20000
                )
                final_props = {
                    hp["id"]: hp
                    for hp in httpx.get(
                        f"{srv.base_url}/api/v1/sessions/{sid}/runs/{rid}/proposals",
                        headers=auth_headers(cfg),
                        timeout=5.0,
                    ).json()
                }
                db_path = data_dir / "sessions.sqlite3"
                conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
                try:
                    ev_rows = conn.execute(
                        "SELECT seq, kind, payload_json FROM run_events WHERE session_id = ? AND run_id = ? ORDER BY seq ASC",
                        (sid, rid),
                    ).fetchall()
                finally:
                    conn.close()
                decided_by_pid: dict[str, list[dict[str, Any]]] = {}
                for _, ev_kind, payload_txt in ev_rows:
                    if ev_kind != "proposal_decided":
                        continue
                    ev_payload = json.loads(payload_txt)
                    ev_pid = ev_payload.get("id")
                    if isinstance(ev_pid, str) and ev_pid:
                        decided_by_pid.setdefault(ev_pid, []).append(ev_payload)
                assert approved_pids
                assert set(approved_pids).issubset(set(decided_by_pid.keys()))

                seen_kinds = {p["kind"] for p in pending_snapshots.values()}
                assert {"scope", "verification", "command"}.issubset(seen_kinds)
                assert len(decision_bodies) == len(approved_pids)
                for dec_url, dec_body in decision_bodies:
                    pid = dec_url.split("/proposals/")[1].split("/decision")[0]
                    assert f"/sessions/{sid}/runs/{rid}/proposals/{pid}/decision" in dec_url
                    assert dec_body.get("decision") == "approve"
                    assert dec_body.get("digest") == pending_snapshots[pid]["digest"]
                    assert decision_responses[pid]["state"] == "approved"
                    pid_events = decided_by_pid[pid]
                    assert any(ev.get("state") == "approved" for ev in pid_events)
                    assert pid_events[-1].get("state") == final_props[pid]["state"]
                    assert final_props[pid]["state"] in ("approved", "revoked")
                    if pending_snapshots[pid]["kind"] == "command":
                        assert dec_body.get("trusted_local_execution") is True

                calc_after_sha = project_helpers.sha256_file(proj_dir / "calc.py")
                assert calc_after_sha != calc_before_sha

                # 5. Completed timeline card shows changed_paths (actual path), checks, and jump-back
                files_txt = page.get_by_test_id(f"timeline-run-files-{rid}").inner_text()
                assert "calc.py" in files_txt
                assert "已修改" in files_txt
                assert "changed_paths(" not in files_txt
                assert "[object Object]" not in files_txt
                checks_txt = page.get_by_test_id(f"timeline-run-checks-{rid}").inner_text()
                assert "验证通过" in checks_txt
                assert "passed=true" not in checks_txt

                page.get_by_test_id(f"timeline-run-diff-{rid}").click()
                expect(page.get_by_test_id("chat-inspector")).to_be_visible()
                expect(page.get_by_test_id("run-id")).to_have_text(rid, timeout=10000)
                expect(page.get_by_test_id("diff-detail")).to_contain_text("return a + b")

                page.get_by_test_id(f"timeline-run-cmd-{rid}").click()
                expect(page.get_by_test_id("command-list")).to_be_visible()
                page.get_by_test_id("close-inspector").click()

                # 6. Follow-up read-only question after completion & verify exact DOM order
                page.get_by_test_id(f"timeline-run-followup-{rid}").click()
                page.get_by_test_id("chat-input").fill("请确认修复后的结果")
                page.get_by_test_id("chat-send").click()
                expect(page.get_by_test_id("chat-turn-3")).to_be_visible(timeout=15000)
                assert len(chat_posts) == 3
                assert len(run_posts) == 1
                assert project_helpers.sha256_file(proj_dir / "calc.py") == calc_after_sha
                order_before = page.evaluate(
                    "() => Array.from(document.querySelectorAll('#chat-timeline > *')).map(e => e.getAttribute('data-testid'))"
                )
                assert order_before == ["chat-turn-1", "chat-turn-2", f"timeline-run-{rid}", "chat-turn-3"]

                # 7. Reload restores same sid, exact order, 3 turns, 1 run card, no duplicates
                page.reload(wait_until="domcontentloaded")
                expect(page.get_by_test_id("current-session")).to_have_text(sid, timeout=10000)
                expect(page.get_by_test_id("chat-turn-1")).to_be_visible(timeout=10000)
                expect(page.get_by_test_id("chat-turn-2")).to_be_visible(timeout=10000)
                expect(page.get_by_test_id("chat-turn-3")).to_be_visible(timeout=10000)
                expect(page.locator(f'[data-testid="timeline-run-{rid}"]')).to_have_count(1)
                order_after = page.evaluate(
                    "() => Array.from(document.querySelectorAll('#chat-timeline > *')).map(e => e.getAttribute('data-testid'))"
                )
                assert order_after == ["chat-turn-1", "chat-turn-2", f"timeline-run-{rid}", "chat-turn-3"]
                assert len(run_posts) == 1
                assert console_errors == []
                assert page_errors == []
            finally:
                browser.close()

    with sqlite3.connect(data_dir / SESSION_DB_FILENAME) as conn:
        runs = conn.execute("SELECT session_id, run_id, state FROM runs").fetchall()
        turns = conn.execute("SELECT session_id, seq, status FROM chat_turns ORDER BY seq").fetchall()
    assert len(runs) == 1 and runs[0][0] == sid and runs[0][2] == "succeeded"
    assert len(turns) == 3 and all(t[0] == sid and t[2] == "completed" for t in turns)


def test_single_composer_session_and_refresh(tmp_path: Path) -> None:
    import httpx
    from app_helpers import auth_headers

    proj_dir = tmp_path / "single_proj"
    data_dir = tmp_path / "single_data"
    project_helpers.create_buggy_calc_project(proj_dir)

    base_cfg = project_helpers.make_project_host_config(tmp_path, allowed_roots=(proj_dir,))
    cfg = HostConfig(
        data_root=data_dir,
        workspace_parent=data_dir.parent / "workspaces",
        python_executable=base_cfg.python_executable,
        bind_port=base_cfg.bind_port,
        access_token=base_cfg.access_token,
        project_roots=(proj_dir,),
    )

    with LiveTcpTestServer(config=cfg) as srv:
        from forecontract_app.chat_runtime import OfflineReadonlyChatProvider

        ro_sc = OfflineReadonlyChatProvider()
        srv.app.state.service._chat_provider_factory = lambda **_: (
            lambda req: (
                {"action": "execution_offer", "goal": req.question, "summary": "执行任务"}
                if ("Historical run" in req.question or "Draft B" in req.question or "帮我改" in req.question or "Active run" in req.question or "Selected run" in req.question)
                else ro_sc(req)
            )
        )
        import re
        import time
        from urllib.parse import urlparse

        url = f"{srv.base_url}/#token={cfg.access_token}"
        t0 = time.monotonic()

        def _ms() -> int:
            return int((time.monotonic() - t0) * 1000)

        _ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
        _STATIC_SEGMENTS = frozenset(
            {
                "api",
                "v1",
                "projects",
                "sessions",
                "runs",
                "proposals",
                "decision",
                "questions",
                "usage",
                "commands",
                "events",
                "snapshot",
                "context",
                "history",
                "chat",
                "turns",
                "cancel",
                "extensions",
                "app.js",
                "index.html",
            }
        )
        _KNOWN_ERR_KINDS = frozenset({"AbortError", "TypeError", "ReferenceError", "SyntaxError", "RangeError", "DOMException"})

        def _classify_err(name: str, raw_msg: str) -> str:
            if name in _KNOWN_ERR_KINDS:
                return name
            low = (raw_msg or "").lower()
            if "aborterror" in low or "signal is aborted" in low or "aborted" in low:
                return "AbortError"
            return "unknown"

        def _classify_method(method: str) -> str:
            up = (method or "").upper()
            return up if up in _ALLOWED_METHODS else "unknown"

        _KNOWN_FAILURES = {
            "net::err_failed": "net_failed",
            "net::err_aborted": "net_aborted",
            "net::err_timed_out": "net_timed_out",
        }
        _host_parsed = urlparse(srv.base_url)
        _host_scheme = (_host_parsed.scheme or "").lower()
        _host_name = (_host_parsed.hostname or "").lower()
        _host_port = _host_parsed.port or (443 if _host_scheme == "https" else 80)

        def _classify_failure(raw_failure: Any) -> str:
            key = str(raw_failure or "").strip().lower()
            return _KNOWN_FAILURES.get(key, "unknown")

        def _normalize_path(raw_url: str) -> str:
            raw_path = urlparse(raw_url or "").path or "/"
            parts = []
            for seg in raw_path.split("/"):
                if not seg:
                    continue
                parts.append(seg if seg in _STATIC_SEGMENTS else "{id}")
            norm = "/" + "/".join(parts)
            return norm[:128]

        def _extract_stack_frames(raw_stack: str) -> list[str]:
            frames = []
            for raw_line in (raw_stack or "").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                if "(" in line and line.endswith(")"):
                    loc = line[line.rfind("(") + 1 : -1].strip()
                elif line.startswith("at "):
                    loc = line[3:].strip()
                else:
                    loc = line
                m = re.match(r"^(https?://\S+?):(\d+)(?::(\d+))?$", loc)
                if not m:
                    continue
                parsed_loc = urlparse(m.group(1))
                loc_scheme = (parsed_loc.scheme or "").lower()
                loc_host = (parsed_loc.hostname or "").lower()
                loc_port = parsed_loc.port or (443 if loc_scheme == "https" else 80)
                if (
                    loc_scheme == _host_scheme
                    and loc_host == _host_name
                    and loc_port == _host_port
                    and parsed_loc.path == "/app.js"
                    and not parsed_loc.query
                    and not parsed_loc.fragment
                ):
                    ln, col = m.group(2), m.group(3)
                    frames.append(f"app.js:{ln}:{col}" if col else f"app.js:{ln}")
                    if len(frames) >= 4:
                        break
            return frames if frames else ["stack_unavailable"]

        def _project_req(method: str, raw_url: str, req_phase: str, t_ms: int) -> dict[str, Any]:
            return {
                "t_ms": t_ms,
                "phase": req_phase,
                "method": _classify_method(method),
                "path": _normalize_path(raw_url),
            }

        def _project_abort(method: str, raw_url: str, raw_failure: Any, req_phase: str, t_ms: int) -> dict[str, Any]:
            return {
                "t_ms": t_ms,
                "phase": req_phase,
                "method": _classify_method(method),
                "path": _normalize_path(raw_url),
                "failure_kind": _classify_failure(raw_failure),
            }

        def _project_error(
            err_name: str,
            err_msg: str,
            err_stack: str,
            err_phase: str,
            t_ms: int,
            aborts_snap: list[dict[str, Any]],
            reqs_snap: list[dict[str, Any]],
        ) -> tuple[str, dict[str, Any]]:
            kind = _classify_err(err_name, err_msg)
            detail = {
                "t_ms": t_ms,
                "phase": err_phase,
                "error_kind": kind,
                "stack_frames": _extract_stack_frames(err_stack),
                "request_attribution": "request_unconfirmed",
                "aborts": aborts_snap[-5:],
                "reqs": reqs_snap[-5:],
            }
            return kind, detail

        def _format_diag(details: list[dict[str, Any]]) -> str:
            return json.dumps(details[:3], ensure_ascii=True, separators=(",", ":"))[:4096]

        # Synthetic self-check on the exact projection & formatting path (isolated from live page_errors)
        _adv_req = _project_req(
            "WEIRD_METHOD",
            "https://user:pass@127.0.0.1:8000/api/v1/sessions/sess-123/" + ("very_long_dynamic_segment_" * 10) + "?token=DEMO_Q#DEMO_FRAG",
            "s1_cancel_active",
            11,
        )
        _adv_abort = _project_abort(
            "WEIRD_METHOD",
            "https://user:pass@evil.example/api/v1/runs/" + ("very_long_dynamic_segment_" * 10) + "?token=DEMO_Q#DEMO_FRAG",
            "DEMO_FAIL_BODY",
            "s1_cancel_active",
            12,
        )
        _adv_kind, _adv_detail = _project_error(
            "CustomSecretHeader",
            "Bearer DEMO_SECRET_TOKEN_XYZ\nX-Auth-Header: leak-body",
            "at Authorization: Bearer DEMO_SECRET_TOKEN_XYZ\nhttps://user:pass@evil.example/secret?token=DEMO_Q#DEMO_FRAG",
            "s1_cancel_active",
            13,
            [_adv_abort],
            [_adv_req],
        )
        _adv_out = _format_diag([_adv_detail, _adv_detail, _adv_detail, _adv_detail])
        assert _adv_kind == "unknown"
        assert _adv_detail["stack_frames"] == ["stack_unavailable"]
        assert _adv_detail["request_attribution"] == "request_unconfirmed"
        assert _adv_req["method"] == "unknown"
        assert _adv_abort["failure_kind"] == "unknown"
        assert len(_adv_req["path"]) <= 128 and len(_adv_abort["path"]) <= 128
        assert len(_adv_out) <= 4096
        for _secret_marker in (
            "DEMO_SECRET_TOKEN_XYZ",
            "X-Auth-Header",
            "leak-body",
            "DEMO_Q",
            "DEMO_FRAG",
            "user:pass",
            "evil.example",
            "DEMO_FAIL_BODY",
            "very_long_dynamic_segment",
            "WEIRD_METHOD",
        ):
            assert _secret_marker not in _adv_out
        _ext_kind, _ext_detail = _project_error(
            "AbortError",
            "signal is aborted without reason",
            "at handleCancelRun (https://evil.example/app.js:12:34)",
            "s1_cancel_active",
            14,
            [],
            [],
        )
        _local_unk_kind, _local_unk_detail = _project_error(
            "TypeError",
            "DEMO_SECRET_TOKEN_XYZ",
            f"at leak ({srv.base_url}/DEMO_MARKER.js:56:78)",
            "s1_cancel_active",
            15,
            [],
            [],
        )
        _neg_out = _format_diag([_ext_detail, _local_unk_detail])
        assert _ext_kind == "AbortError"
        assert _ext_detail["stack_frames"] == ["stack_unavailable"]
        assert _local_unk_kind == "TypeError"
        assert _local_unk_detail["stack_frames"] == ["stack_unavailable"]
        for _neg_marker in ("evil.example", "12:34", "DEMO_MARKER", "56:78", "DEMO_SECRET_TOKEN_XYZ"):
            assert _neg_marker not in _neg_out
        _q_kind, _q_detail = _project_error(
            "AbortError",
            "signal is aborted without reason",
            f"at f (https://other.invalid/bundle.js?source={srv.base_url}/app.js:7:8)",
            "s1_cancel_active",
            16,
            [],
            [],
        )
        _q_out = _format_diag([_q_detail])
        assert _q_kind == "AbortError"
        assert _q_detail["stack_frames"] == ["stack_unavailable"]
        for _q_marker in ("other.invalid", "source=", "app.js:7:8"):
            assert _q_marker not in _q_out
        assert _project_abort("GET", f"{srv.base_url}/api/v1/sessions", "net::ERR_FAILED", "s1_cancel_active", 21)["failure_kind"] == "net_failed"
        assert _project_abort("GET", f"{srv.base_url}/api/v1/sessions", "net::ERR_ABORTED", "s1_cancel_active", 22)["failure_kind"] == "net_aborted"
        assert _project_abort("GET", f"{srv.base_url}/api/v1/sessions", "net::ERR_TIMED_OUT", "s1_cancel_active", 23)["failure_kind"] == "net_timed_out"
        _ok_req = _project_req(
            "GET",
            f"{srv.base_url}/api/v1/sessions/123e4567-e89b-12d3-a456-426614174000/runs/98765432/proposals?after=1#frag",
            "s1_cancel_active",
            42,
        )
        _ok_kind, _ok_detail = _project_error(
            "AbortError",
            "signal is aborted without reason",
            f"at handleCancelRun ({srv.base_url}/app.js:3150:12)",
            "s1_cancel_active",
            43,
            [],
            [_ok_req],
        )
        _ok_out = _format_diag([_ok_detail])
        _ok_parsed = json.loads(_ok_out)
        assert _ok_kind == "AbortError"
        assert _ok_parsed[0]["stack_frames"] == ["app.js:3150:12"]
        assert _ok_parsed[0]["phase"] == "s1_cancel_active"
        assert _ok_parsed[0]["request_attribution"] == "request_unconfirmed"
        assert _ok_parsed[0]["reqs"][0] == {
            "t_ms": 42,
            "phase": "s1_cancel_active",
            "method": "GET",
            "path": "/api/v1/sessions/{id}/runs/{id}/proposals",
        }

        run_posts: list[tuple[str, dict[str, Any]]] = []
        created_run_ids: list[str] = []
        cancel_posts: list[tuple[str, str]] = []
        page_errors: list[str] = []
        page_error_details: list[dict[str, Any]] = []
        recent_aborts: list[dict[str, Any]] = []
        recent_reqs: list[dict[str, Any]] = []
        phase = ["init"]
        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 800})

                def _on_pageerror(err: Any) -> None:
                    kind, detail = _project_error(
                        str(getattr(err, "name", "") or ""),
                        str(err),
                        str(getattr(err, "stack", "") or ""),
                        phase[0],
                        _ms(),
                        recent_aborts,
                        recent_reqs,
                    )
                    page_errors.append(kind)
                    if len(page_error_details) < 3:
                        page_error_details.append(detail)

                page.on("pageerror", _on_pageerror)
                page.on(
                    "requestfailed",
                    lambda r: recent_aborts.append(_project_abort(r.method, r.url, r.failure, phase[0], _ms())),
                )
                page.add_init_script(
                    """(() => {
                      const origFetch = window.fetch.bind(window);
                      window.__fcHoldPropSession = null;
                      window.__fcExcludeRunId = null;
                      window.__fcHeldTriggered = false;
                      window.__fcHeldReady = false;
                      window.__fcHeldRunId = null;
                      window.__fcHeldPids = [];
                      window.__fcHeldResponses = new WeakSet();
                      window.__fcReleaseHeld = null;
                      window.fetch = async (input, init) => {
                        const url = typeof input === 'string' ? input : (input && input.url) || '';
                        const method = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();
                        const targetSess = window.__fcHoldPropSession;
                        const excludeRun = window.__fcExcludeRunId;
                        const propMatch = targetSess
                          ? url.match(new RegExp(`/api/v1/sessions/${targetSess}/runs/([^/]+)/proposals$`))
                          : null;
                        const realResp = await origFetch(input, init);
                        if (
                          !window.__fcHeldTriggered &&
                          propMatch &&
                          method === 'GET' &&
                          propMatch[1] !== excludeRun &&
                          realResp.ok
                        ) {
                          const cloned = await realResp.clone().json();
                          if (Array.isArray(cloned) && cloned.length > 0 && typeof cloned[0].id === 'string' && !window.__fcHeldTriggered) {
                            window.__fcHeldTriggered = true;
                            window.__fcHeldRunId = propMatch[1];
                            window.__fcHeldPids = cloned.map((p) => p.id);
                            window.__fcHeldResponses.add(realResp);
                            const origJson = realResp.json.bind(realResp);
                            realResp.json = async () => {
                              const data = await origJson();
                              window.__fcHeldReady = true;
                              await new Promise((resolve) => {
                                window.__fcReleaseHeld = resolve;
                              });
                              return data;
                            };
                          }
                        }
                        return realResp;
                      };
                    })();"""
                )

                def _on_req(r) -> None:
                    recent_reqs.append(_project_req(r.method, r.url, phase[0], _ms()))
                    if len(recent_reqs) > 25:
                        del recent_reqs[0]
                    if r.method != "POST":
                        return
                    if r.url.endswith(("/runs", "/confirm")):
                        run_posts.append((r.url, json.loads(r.post_data or "{}")))
                    elif r.url.endswith("/cancel"):
                        cancel_posts.append((r.method, r.url))

                def _on_resp(resp) -> None:
                    if resp.request.method == "POST" and resp.request.url.endswith(("/runs", "/confirm")) and resp.status in (201, 202):
                        data = resp.json()
                        if isinstance(data, dict) and isinstance(data.get("run_id"), str):
                            created_run_ids.append(data["run_id"])

                page.on("request", _on_req)
                page.on("response", _on_resp)
                page.goto(url, wait_until="domcontentloaded")

                # Single composer on default home: no second goal input, no empty approvals/config open
                expect(page.get_by_test_id("chat-input")).to_be_visible()
                expect(page.get_by_test_id("goal")).to_have_count(0)
                expect(page.get_by_test_id("pending-approvals-card")).to_be_hidden()

                # Create project with valid name + 2 sessions
                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id("project-name").fill("single-proj")
                page.get_by_test_id("register-root").fill(str(proj_dir))
                page.get_by_test_id("confirm-read").check()
                page.get_by_test_id("register-project").click()
                expect(page.get_by_test_id("project-list")).to_contain_text("single-proj", timeout=10000)
                page.get_by_test_id("new-session").click()
                expect(page.locator('[data-testid^="session-item-"]')).to_have_count(1, timeout=10000)
                page.get_by_test_id("new-session").click()
                expect(page.locator('[data-testid^="session-item-"]')).to_have_count(2, timeout=10000)
                s1 = (
                    page.locator('[data-testid^="session-item-"]').nth(0).get_attribute("data-testid") or ""
                ).removeprefix("session-item-")
                s2 = (
                    page.locator('[data-testid^="session-item-"]').nth(1).get_attribute("data-testid") or ""
                ).removeprefix("session-item-")

                page.get_by_test_id("nav-chat").click()
                page.get_by_test_id(f"chat-session-item-{s1}").click()
                expect(page.get_by_test_id("current-session")).to_have_text(s1, timeout=10000)

                # 1. Establish one real terminal historical run on s1 first
                page.get_by_test_id("chat-input").fill("Historical run on s1")
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                page.locator('[data-testid^="offer-confirm-btn-"]').last.click()
                expect(page.locator('[data-testid^="timeline-run-"]').first).to_be_visible(timeout=15000)
                rid_hist = page.locator('[data-testid^="timeline-run-id-"]').first.get_attribute("data-run-id") or ""
                assert rid_hist
                page.get_by_test_id(f"timeline-run-cancel-{rid_hist}").click()
                expect(page.get_by_test_id(f"timeline-run-state-{rid_hist}")).to_have_attribute(
                    "data-run-state", "cancelled", timeout=15000
                )
                expect(page.get_by_test_id(f"timeline-run-state-{rid_hist}")).to_contain_text(
                    "已取消", timeout=15000
                )
                hist_state_before = httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{s1}/runs/{rid_hist}",
                    headers=auth_headers(cfg),
                    timeout=5.0,
                ).json()["state"]

                # 2. Send Draft B -> offer -> arm pre-start proposal observation -> double-click confirm
                run_posts.clear()
                created_run_ids.clear()
                page.get_by_test_id("chat-input").fill("Draft B")
                assert run_posts == []

                # 3a. Arm pre-start proposal observation on s1 excluding rid_hist before starting rid_active
                phase[0] = "late_prop_hold"
                page.evaluate(
                    "([sessId, histId]) => { window.__fcHoldPropSession = sessId; window.__fcExcludeRunId = histId; }",
                    [s1, rid_hist],
                )
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                page.locator('[data-testid^="offer-confirm-btn-"]').last.dblclick()
                expect(page.locator('article[data-testid^="timeline-run-"]')).to_have_count(2, timeout=15000)
                assert len(run_posts) == 1
                assert len(created_run_ids) == 1
                assert f"/sessions/{s1}/" in run_posts[0][0]
                assert run_posts[0][1].get("confirmed_scope") is True
                rid_active = created_run_ids[0]
                assert rid_active != rid_hist
                assert (page.locator('[data-testid^="timeline-run-id-"]').nth(1).get_attribute("data-run-id") or "") == rid_active

                # 3b. Ensure real GET /proposals for rid_active reaches json hold barrier; if initial read was empty, re-select s1 via UI
                if not page.evaluate("() => window.__fcHeldReady === true"):
                    expect(page.get_by_test_id("pending-approvals-card")).to_be_visible(timeout=15000)
                    page.get_by_test_id(f"chat-session-item-{s1}").click()
                page.wait_for_function("() => window.__fcHeldReady === true", timeout=15000)
                assert page.evaluate("() => window.__fcHeldRunId") == rid_active
                held_pids = page.evaluate("() => window.__fcHeldPids")
                assert len(held_pids) > 0

                # Attach CDP observation at app.js applyRunSideResources post-json epoch guard (line 1915, 0-indexed 1914)
                cdp = page.context.new_cdp_session(page)
                cdp_watch: dict[str, Any] = {"armed": False, "hit": False, "matched": None}
                bp_id_holder: list[str] = []

                def _on_paused(ev: dict[str, Any]) -> None:
                    try:
                        hit_bps = ev.get("hitBreakpoints") or []
                        if (
                            cdp_watch["armed"]
                            and bp_id_holder
                            and bp_id_holder[0] in hit_bps
                            and ev.get("callFrames")
                        ):
                            res = cdp.send(
                                "Debugger.evaluateOnCallFrame",
                                {
                                    "callFrameId": ev["callFrames"][0]["callFrameId"],
                                    "expression": (
                                        "({"
                                        "propMatched: typeof window.__fcHeldResponses !== 'undefined' && Boolean(propResp) && window.__fcHeldResponses.has(propResp),"
                                        "sid: guard ? guard.sessionId : null,"
                                        "rid: guard ? guard.runId : null,"
                                        "pids: Array.isArray(items) ? items.map((p) => p && p.id) : [],"
                                        "epochCurrent: isEpochCurrent(guard)"
                                        "})"
                                    ),
                                    "returnByValue": True,
                                },
                            )
                            val = res.get("result", {}).get("value")
                            if (
                                isinstance(val, dict)
                                and val.get("propMatched") is True
                                and val.get("sid") == s1
                                and val.get("rid") == rid_active
                                and val.get("pids") == held_pids
                            ):
                                cdp_watch["matched"] = val
                                cdp_watch["hit"] = True
                    finally:
                        cdp.send("Debugger.resume")

                cdp.on("Debugger.paused", _on_paused)
                cdp.send("Debugger.enable")
                bp = cdp.send("Debugger.setBreakpointByUrl", {"lineNumber": 1927, "urlRegex": r".*/app\.js$"})
                bp_id_holder.append(bp["breakpointId"])
                try:
                    phase[0] = "cdp_s2_switch"
                    page.get_by_test_id(f"chat-session-item-{s2}").click()
                    expect(page.get_by_test_id("current-session")).to_have_text(s2, timeout=10000)
                    phase[0] = "late_prop_release"
                    cdp_watch["armed"] = True
                    page.evaluate("() => { if (window.__fcReleaseHeld) window.__fcReleaseHeld(); }")
                    for _ in range(250):
                        if cdp_watch["hit"]:
                            break
                        page.wait_for_timeout(20)
                    assert cdp_watch["hit"] is True
                    assert cdp_watch["matched"] is not None
                    assert cdp_watch["matched"]["epochCurrent"] is False
                finally:
                    cdp.send("Debugger.removeBreakpoint", {"breakpointId": bp["breakpointId"]})
                    cdp.send("Debugger.disable")
                    cdp.detach()

                expect(page.locator(f'[data-testid="timeline-run-{rid_active}"]')).to_have_count(0)
                expect(page.get_by_test_id("pending-approvals-card")).to_be_hidden()

                # 4. F03: Switch back to s1, select historical run rid_hist, then stop rid_active via card button
                phase[0] = "s1_cancel_active"
                page.get_by_test_id(f"chat-session-item-{s1}").click()
                expect(page.get_by_test_id("current-session")).to_have_text(s1, timeout=10000)
                page.get_by_test_id("nav-history").click()
                page.get_by_test_id("run-history-select").select_option(rid_hist)
                expect(page.get_by_test_id("run-id")).to_have_text(rid_hist, timeout=10000)
                page.get_by_test_id("nav-chat").click()

                cancel_posts.clear()
                expect(page.get_by_test_id(f"timeline-run-cancel-{rid_active}")).to_be_visible(timeout=10000)
                page.get_by_test_id(f"timeline-run-cancel-{rid_active}").click()
                expect(page.get_by_test_id(f"timeline-run-state-{rid_active}")).to_have_attribute(
                    "data-run-state", "cancelled", timeout=15000
                )
                expect(page.get_by_test_id(f"timeline-run-state-{rid_active}")).to_contain_text(
                    "已取消", timeout=15000
                )
                expect(page.get_by_test_id("run-id")).to_have_text(rid_hist)
                assert page.get_by_test_id("run-history-select").input_value() == rid_hist
                assert cancel_posts == [("POST", f"{srv.base_url}/api/v1/sessions/{s1}/runs/{rid_active}/cancel")]
                active_state_after = httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{s1}/runs/{rid_active}",
                    headers=auth_headers(cfg),
                    timeout=5.0,
                ).json()["state"]
                assert active_state_after == "cancelled"
                hist_state_after = httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{s1}/runs/{rid_hist}",
                    headers=auth_headers(cfg),
                    timeout=5.0,
                ).json()["state"]
                assert hist_state_after == hist_state_before

                # 5. Now that workspace is released, switch to s2 for normal chat + narrow viewport 760x720
                phase[0] = "s2_chat"
                page.get_by_test_id(f"chat-session-item-{s2}").click()
                expect(page.get_by_test_id("current-session")).to_have_text(s2, timeout=10000)
                page.get_by_test_id("chat-input").fill("Session 2 question")
                page.get_by_test_id("chat-send").click()
                expect(page.get_by_test_id("chat-turn-1")).to_be_visible(timeout=15000)
                assistant_reply = page.get_by_test_id("chat-turn-1").locator(".chat-msg-assistant .chat-msg-text")
                expect(assistant_reply).to_be_visible(timeout=15000)
                assert assistant_reply.inner_text().strip() != ""

                page.set_viewport_size({"width": 760, "height": 720})
                expect(page.get_by_test_id("chat-input")).to_be_visible()
                assert (
                    page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth")
                    is False
                )

                phase[0] = "reload_s1"
                page.set_viewport_size({"width": 1280, "height": 800})
                page.get_by_test_id(f"chat-session-item-{s1}").click()
                expect(page.get_by_test_id("current-session")).to_have_text(s1, timeout=10000)
                page.reload(wait_until="domcontentloaded")
                expect(page.get_by_test_id("current-session")).to_have_text(s1, timeout=10000)
                expect(page.get_by_test_id(f"timeline-run-{rid_active}")).to_be_visible(timeout=10000)
                assert page_errors == [], _format_diag(page_error_details)
            finally:
                browser.close()



def test_cancel_card_readback_deadline(tmp_path: Path) -> None:
    proj_dir = tmp_path / "deadline_proj"
    data_dir = tmp_path / "deadline_data"
    project_helpers.create_buggy_calc_project(proj_dir)

    base_cfg = project_helpers.make_project_host_config(tmp_path, allowed_roots=(proj_dir,))
    cfg = HostConfig(
        data_root=data_dir,
        workspace_parent=data_dir.parent / "workspaces",
        python_executable=base_cfg.python_executable,
        bind_port=base_cfg.bind_port,
        access_token=base_cfg.access_token,
        project_roots=(proj_dir,),
    )

    with LiveTcpTestServer(config=cfg) as srv:
        from forecontract_app.chat_runtime import OfflineReadonlyChatProvider

        ro_sc = OfflineReadonlyChatProvider()
        srv.app.state.service._chat_provider_factory = lambda **_: (
            lambda req: (
                {"action": "execution_offer", "goal": req.question, "summary": "执行任务"}
                if ("Historical run" in req.question or "Draft B" in req.question or "帮我改" in req.question or "Active run" in req.question or "Selected run" in req.question)
                else ro_sc(req)
            )
        )
        import re
        import time
        from urllib.parse import urlparse

        url = f"{srv.base_url}/#token={cfg.access_token}"
        t0 = time.monotonic()

        def _ms() -> int:
            return int((time.monotonic() - t0) * 1000)

        _ALLOWED_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})
        _STATIC_SEGMENTS = frozenset(
            {
                "api",
                "v1",
                "projects",
                "sessions",
                "runs",
                "proposals",
                "decision",
                "questions",
                "usage",
                "commands",
                "events",
                "snapshot",
                "context",
                "history",
                "chat",
                "turns",
                "cancel",
                "extensions",
                "app.js",
                "index.html",
            }
        )
        _KNOWN_ERR_KINDS = frozenset({"AbortError", "TypeError", "ReferenceError", "SyntaxError", "RangeError", "DOMException"})

        def _classify_err(name: str, raw_msg: str) -> str:
            if name in _KNOWN_ERR_KINDS:
                return name
            low = (raw_msg or "").lower()
            if "aborterror" in low or "signal is aborted" in low or "aborted" in low:
                return "AbortError"
            return "unknown"

        def _classify_method(method: str) -> str:
            up = (method or "").upper()
            return up if up in _ALLOWED_METHODS else "unknown"

        _KNOWN_FAILURES = {
            "net::err_failed": "net_failed",
            "net::err_aborted": "net_aborted",
            "net::err_timed_out": "net_timed_out",
        }
        _host_parsed = urlparse(srv.base_url)
        _host_scheme = (_host_parsed.scheme or "").lower()
        _host_name = (_host_parsed.hostname or "").lower()
        _host_port = _host_parsed.port or (443 if _host_scheme == "https" else 80)

        def _classify_failure(raw_failure: Any) -> str:
            key = str(raw_failure or "").strip().lower()
            return _KNOWN_FAILURES.get(key, "unknown")

        def _normalize_path(raw_url: str) -> str:
            raw_path = urlparse(raw_url or "").path or "/"
            parts = []
            for seg in raw_path.split("/"):
                if not seg:
                    continue
                parts.append(seg if seg in _STATIC_SEGMENTS else "{id}")
            norm = "/" + "/".join(parts)
            return norm[:128]

        def _extract_stack_frames(raw_stack: str) -> list[str]:
            frames = []
            for raw_line in (raw_stack or "").splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                if "(" in line and line.endswith(")"):
                    loc = line[line.rfind("(") + 1 : -1].strip()
                elif line.startswith("at "):
                    loc = line[3:].strip()
                else:
                    loc = line
                m = re.match(r"^(https?://\S+?):(\d+)(?::(\d+))?$", loc)
                if not m:
                    continue
                parsed_loc = urlparse(m.group(1))
                loc_scheme = (parsed_loc.scheme or "").lower()
                loc_host = (parsed_loc.hostname or "").lower()
                loc_port = parsed_loc.port or (443 if loc_scheme == "https" else 80)
                if (
                    loc_scheme == _host_scheme
                    and loc_host == _host_name
                    and loc_port == _host_port
                    and parsed_loc.path == "/app.js"
                    and not parsed_loc.query
                    and not parsed_loc.fragment
                ):
                    ln, col = m.group(2), m.group(3)
                    frames.append(f"app.js:{ln}:{col}" if col else f"app.js:{ln}")
                    if len(frames) >= 4:
                        break
            return frames if frames else ["stack_unavailable"]

        def _project_req(method: str, raw_url: str, req_phase: str, t_ms: int) -> dict[str, Any]:
            return {
                "t_ms": t_ms,
                "phase": req_phase,
                "method": _classify_method(method),
                "path": _normalize_path(raw_url),
            }

        def _project_abort(method: str, raw_url: str, raw_failure: Any, req_phase: str, t_ms: int) -> dict[str, Any]:
            return {
                "t_ms": t_ms,
                "phase": req_phase,
                "method": _classify_method(method),
                "path": _normalize_path(raw_url),
                "failure_kind": _classify_failure(raw_failure),
            }

        def _project_error(
            err_name: str,
            err_msg: str,
            err_stack: str,
            err_phase: str,
            t_ms: int,
            aborts_snap: list[dict[str, Any]],
            reqs_snap: list[dict[str, Any]],
        ) -> tuple[str, dict[str, Any]]:
            kind = _classify_err(err_name, err_msg)
            detail = {
                "t_ms": t_ms,
                "phase": err_phase,
                "error_kind": kind,
                "stack_frames": _extract_stack_frames(err_stack),
                "request_attribution": "request_unconfirmed",
                "aborts": aborts_snap[-5:],
                "reqs": reqs_snap[-5:],
            }
            return kind, detail

        def _format_diag(details: list[dict[str, Any]]) -> str:
            return json.dumps(details[:3], ensure_ascii=True, separators=(",", ":"))[:4096]

        # Synthetic self-check on the exact projection & formatting path (isolated from live page_errors)
        _adv_req = _project_req(
            "WEIRD_METHOD",
            "https://user:pass@127.0.0.1:8000/api/v1/sessions/sess-123/" + ("very_long_dynamic_segment_" * 10) + "?token=DEMO_Q#DEMO_FRAG",
            "hang_headers",
            11,
        )
        _adv_abort = _project_abort(
            "WEIRD_METHOD",
            "https://user:pass@evil.example/api/v1/runs/" + ("very_long_dynamic_segment_" * 10) + "?token=DEMO_Q#DEMO_FRAG",
            "DEMO_FAIL_BODY",
            "hang_headers",
            12,
        )
        _adv_kind, _adv_detail = _project_error(
            "CustomSecretHeader",
            "Bearer DEMO_SECRET_TOKEN_XYZ\nX-Auth-Header: leak-body",
            "at Authorization: Bearer DEMO_SECRET_TOKEN_XYZ\nhttps://user:pass@evil.example/secret?token=DEMO_Q#DEMO_FRAG",
            "hang_headers",
            13,
            [_adv_abort],
            [_adv_req],
        )
        _adv_out = _format_diag([_adv_detail, _adv_detail, _adv_detail, _adv_detail])
        assert _adv_kind == "unknown"
        assert _adv_detail["stack_frames"] == ["stack_unavailable"]
        assert _adv_detail["request_attribution"] == "request_unconfirmed"
        assert _adv_req["method"] == "unknown"
        assert _adv_abort["failure_kind"] == "unknown"
        assert len(_adv_req["path"]) <= 128 and len(_adv_abort["path"]) <= 128
        assert len(_adv_out) <= 4096
        for _secret_marker in (
            "DEMO_SECRET_TOKEN_XYZ",
            "X-Auth-Header",
            "leak-body",
            "DEMO_Q",
            "DEMO_FRAG",
            "user:pass",
            "evil.example",
            "DEMO_FAIL_BODY",
            "very_long_dynamic_segment",
            "WEIRD_METHOD",
        ):
            assert _secret_marker not in _adv_out
        _ext_kind, _ext_detail = _project_error(
            "AbortError",
            "signal is aborted without reason",
            "at handleCancelRun (https://evil.example/app.js:12:34)",
            "hang_body",
            14,
            [],
            [],
        )
        _local_unk_kind, _local_unk_detail = _project_error(
            "TypeError",
            "DEMO_SECRET_TOKEN_XYZ",
            f"at leak ({srv.base_url}/DEMO_MARKER.js:56:78)",
            "hang_body",
            15,
            [],
            [],
        )
        _neg_out = _format_diag([_ext_detail, _local_unk_detail])
        assert _ext_kind == "AbortError"
        assert _ext_detail["stack_frames"] == ["stack_unavailable"]
        assert _local_unk_kind == "TypeError"
        assert _local_unk_detail["stack_frames"] == ["stack_unavailable"]
        for _neg_marker in ("evil.example", "12:34", "DEMO_MARKER", "56:78", "DEMO_SECRET_TOKEN_XYZ"):
            assert _neg_marker not in _neg_out
        _q_kind, _q_detail = _project_error(
            "AbortError",
            "signal is aborted without reason",
            f"at f (https://other.invalid/bundle.js?source={srv.base_url}/app.js:7:8)",
            "hang_body",
            16,
            [],
            [],
        )
        _q_out = _format_diag([_q_detail])
        assert _q_kind == "AbortError"
        assert _q_detail["stack_frames"] == ["stack_unavailable"]
        for _q_marker in ("other.invalid", "source=", "app.js:7:8"):
            assert _q_marker not in _q_out
        assert _project_abort("GET", f"{srv.base_url}/api/v1/sessions", "net::ERR_FAILED", "hang_body", 21)["failure_kind"] == "net_failed"
        assert _project_abort("GET", f"{srv.base_url}/api/v1/sessions", "net::ERR_ABORTED", "hang_body", 22)["failure_kind"] == "net_aborted"
        assert _project_abort("GET", f"{srv.base_url}/api/v1/sessions", "net::ERR_TIMED_OUT", "hang_body", 23)["failure_kind"] == "net_timed_out"
        _ok_req = _project_req(
            "GET",
            f"{srv.base_url}/api/v1/sessions/123e4567-e89b-12d3-a456-426614174000/runs/98765432?after=1#frag",
            "hang_body",
            55,
        )
        _ok_kind, _ok_detail = _project_error(
            "AbortError",
            "signal is aborted without reason",
            f"at handleCancelRun ({srv.base_url}/app.js:3188:9)",
            "hang_body",
            56,
            [],
            [_ok_req],
        )
        _ok_out = _format_diag([_ok_detail])
        _ok_parsed = json.loads(_ok_out)
        assert _ok_kind == "AbortError"
        assert _ok_parsed[0]["stack_frames"] == ["app.js:3188:9"]
        assert _ok_parsed[0]["phase"] == "hang_body"
        assert _ok_parsed[0]["request_attribution"] == "request_unconfirmed"
        assert _ok_parsed[0]["reqs"][0] == {
            "t_ms": 55,
            "phase": "hang_body",
            "method": "GET",
            "path": "/api/v1/sessions/{id}/runs/{id}",
        }

        page_errors: list[str] = []
        page_error_details: list[dict[str, Any]] = []
        recent_aborts: list[dict[str, Any]] = []
        recent_reqs: list[dict[str, Any]] = []
        phase = ["init"]
        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 800})

                def _on_pageerror(err: Any) -> None:
                    kind, detail = _project_error(
                        str(getattr(err, "name", "") or ""),
                        str(err),
                        str(getattr(err, "stack", "") or ""),
                        phase[0],
                        _ms(),
                        recent_aborts,
                        recent_reqs,
                    )
                    page_errors.append(kind)
                    if len(page_error_details) < 3:
                        page_error_details.append(detail)

                def _on_req(r: Any) -> None:
                    recent_reqs.append(_project_req(r.method, r.url, phase[0], _ms()))
                    if len(recent_reqs) > 25:
                        del recent_reqs[0]

                page.on("pageerror", _on_pageerror)
                page.on("request", _on_req)
                page.on(
                    "requestfailed",
                    lambda r: recent_aborts.append(_project_abort(r.method, r.url, r.failure, phase[0], _ms())),
                )
                page.add_init_script(
                    """(() => {
                      const origFetch = window.fetch.bind(window);
                      window.__fcTestMode = 'off';
                      window.__fcCountCancels = false;
                      window.__fcCancelPosts = [];
                      window.__fcTargetSid = null;
                      window.__fcTargetRid = null;
                      window.__fcWindowStartMs = null;
                      window.__fcHasSignal = false;
                      window.__fcHeadersWaitEntered = false;
                      window.__fcHeadersAbortedBySignal = false;
                      window.__fcHeadersAbortedAtMs = null;
                      window.__fcHasSignalBody = false;
                      window.__fcBodyWaitEntered = false;
                      window.__fcBodyAbortedBySignal = false;
                      window.__fcBodyAbortedAtMs = null;
                      window.fetch = async (input, init) => {
                        const reqUrl = typeof input === 'string' ? input : (input && input.url) || '';
                        const method = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();
                        let resolvedUrl = null;
                        try {
                          resolvedUrl = new URL(reqUrl, window.location.href);
                        } catch (_) {
                          resolvedUrl = null;
                        }
                        if (
                          window.__fcCountCancels &&
                          method === 'POST' &&
                          resolvedUrl &&
                          resolvedUrl.origin === window.location.origin &&
                          resolvedUrl.pathname.endsWith('/cancel')
                        ) {
                          window.__fcCancelPosts.push([method, resolvedUrl.origin + resolvedUrl.pathname]);
                        }
                        const mode = window.__fcTestMode;
                        const sid = window.__fcTargetSid;
                        const rid = window.__fcTargetRid;
                        if (mode !== 'off' && sid && rid) {
                          if (method === 'POST' && reqUrl.endsWith(`/api/v1/sessions/${sid}/runs/${rid}/cancel`)) {
                            const body = JSON.stringify({
                              run_id: rid,
                              session_id: sid,
                              state: 'cancelling',
                              verification: 'not_run',
                            });
                            window.__fcWindowStartMs = performance.now();
                            return new Response(body, {
                              status: 202,
                              headers: { 'Content-Type': 'application/json' },
                            });
                          }
                          if (method === 'GET' && reqUrl.endsWith(`/api/v1/sessions/${sid}/runs/${rid}`)) {
                            const sig = init && init.signal;
                            if (mode === 'headers') {
                              window.__fcHasSignal = Boolean(sig);
                              window.__fcHeadersWaitEntered = true;
                              return await new Promise((_, reject) => {
                                if (sig) {
                                  if (sig.aborted) {
                                    window.__fcHeadersAbortedBySignal = true;
                                    window.__fcHeadersAbortedAtMs = performance.now();
                                    reject(new DOMException('Aborted', 'AbortError'));
                                    return;
                                  }
                                  sig.addEventListener('abort', () => {
                                    window.__fcHeadersAbortedBySignal = true;
                                    window.__fcHeadersAbortedAtMs = performance.now();
                                    reject(new DOMException('Aborted', 'AbortError'));
                                  }, { once: true });
                                }
                              });
                            }
                            if (mode === 'body') {
                              window.__fcHasSignalBody = Boolean(sig);
                              const resp = new Response(
                                JSON.stringify({ run_id: rid, session_id: sid, state: 'cancelling' }),
                                { status: 200, headers: { 'Content-Type': 'application/json' } }
                              );
                              resp.json = () => new Promise((_, reject) => {
                                window.__fcBodyWaitEntered = true;
                                if (sig) {
                                  if (sig.aborted) {
                                    window.__fcBodyAbortedBySignal = true;
                                    window.__fcBodyAbortedAtMs = performance.now();
                                    reject(new DOMException('Aborted', 'AbortError'));
                                    return;
                                  }
                                  sig.addEventListener('abort', () => {
                                    window.__fcBodyAbortedBySignal = true;
                                    window.__fcBodyAbortedAtMs = performance.now();
                                    reject(new DOMException('Aborted', 'AbortError'));
                                  }, { once: true });
                                }
                              });
                              return resp;
                            }
                          }
                        }
                        return origFetch(input, init);
                      };
                    })();"""
                )

                page.goto(url, wait_until="domcontentloaded")

                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id("project-name").fill("deadline-proj")
                page.get_by_test_id("register-root").fill(str(proj_dir))
                page.get_by_test_id("confirm-read").check()
                page.get_by_test_id("register-project").click()
                expect(page.get_by_test_id("project-list")).to_contain_text("deadline-proj", timeout=10000)
                page.get_by_test_id("new-session").click()
                expect(page.locator('[data-testid^="session-item-"]')).to_have_count(1, timeout=10000)
                s1 = (
                    page.locator('[data-testid^="session-item-"]').nth(0).get_attribute("data-testid") or ""
                ).removeprefix("session-item-")

                page.get_by_test_id("nav-chat").click()
                page.get_by_test_id(f"chat-session-item-{s1}").click()
                expect(page.get_by_test_id("current-session")).to_have_text(s1, timeout=10000)

                # Historical run rid_hist
                page.get_by_test_id("chat-input").fill("Historical run for deadline test")
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                page.locator('[data-testid^="offer-confirm-btn-"]').last.click()
                expect(page.locator('[data-testid^="timeline-run-"]').first).to_be_visible(timeout=15000)
                rid_hist = page.locator('[data-testid^="timeline-run-id-"]').first.get_attribute("data-run-id") or ""
                assert rid_hist
                page.get_by_test_id(f"timeline-run-cancel-{rid_hist}").click()
                expect(page.get_by_test_id(f"timeline-run-state-{rid_hist}")).to_have_attribute(
                    "data-run-state", "cancelled", timeout=15000
                )
                expect(page.get_by_test_id(f"timeline-run-state-{rid_hist}")).to_contain_text(
                    "已取消", timeout=15000
                )

                # Active run rid_active
                page.get_by_test_id("chat-input").fill("Active run for deadline test")
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                page.locator('[data-testid^="offer-confirm-btn-"]').last.click()
                expect(page.locator('article[data-testid^="timeline-run-"]')).to_have_count(2, timeout=15000)
                rid_active = page.locator('[data-testid^="timeline-run-id-"]').nth(1).get_attribute("data-run-id") or ""
                assert rid_active
                assert rid_active != rid_hist

                # Select rid_hist while rid_active card remains visible
                page.get_by_test_id("nav-history").click()
                page.get_by_test_id("run-history-select").select_option(rid_hist)
                expect(page.get_by_test_id("run-id")).to_have_text(rid_hist, timeout=10000)
                page.get_by_test_id("nav-chat").click()

                import httpx
                from app_helpers import auth_headers

                expected_cancel_url = f"{srv.base_url}/api/v1/sessions/{s1}/runs/{rid_active}/cancel"

                # Case 1: GET hangs before headers -> signal aborts within single 12s monotonic window, lock released, no fake cancelled
                phase[0] = "hang_headers"
                page.evaluate(
                    "([sid, rid]) => { window.__fcCountCancels = true; window.__fcCancelPosts = []; window.__fcTargetSid = sid; window.__fcTargetRid = rid; window.__fcTestMode = 'headers'; }",
                    [s1, rid_active],
                )
                page.get_by_test_id(f"timeline-run-cancel-{rid_active}").click()
                page.wait_for_function("() => window.__fcWindowStartMs !== null && window.__fcHeadersWaitEntered === true", timeout=12000)
                assert page.evaluate("() => window.__fcHasSignal") is True
                rem_ms_1 = max(500, int(12000 - page.evaluate("() => performance.now() - window.__fcWindowStartMs")))
                page.wait_for_function("() => window.__fcHeadersAbortedBySignal === true", timeout=rem_ms_1)
                phase[0] = "signal_abort_headers"
                assert page.evaluate("() => window.__fcHeadersAbortedAtMs - window.__fcWindowStartMs") <= 12000
                expect(page.get_by_test_id(f"timeline-run-state-{rid_active}")).to_have_attribute("data-run-state", "cancelling")
                expect(page.get_by_test_id(f"timeline-run-state-{rid_active}")).to_contain_text("正在停止")
                assert "已取消" not in page.get_by_test_id(f"timeline-run-state-{rid_active}").inner_text()
                expect(page.get_by_test_id("run-id")).to_have_text(rid_hist)
                assert page.get_by_test_id("run-history-select").input_value() == rid_hist
                assert [tuple(x) for x in page.evaluate("() => window.__fcCancelPosts")] == [("POST", expected_cancel_url)]

                # Case 2: 2nd explicit click proves lock released; GET returns headers then hangs json() -> signal aborts within single 12s monotonic window
                phase[0] = "hang_body"
                page.evaluate("() => { window.__fcWindowStartMs = null; window.__fcTestMode = 'body'; }")
                page.get_by_test_id(f"timeline-run-cancel-{rid_active}").click()
                page.wait_for_function("() => window.__fcWindowStartMs !== null && window.__fcBodyWaitEntered === true", timeout=12000)
                assert page.evaluate("() => window.__fcHasSignalBody") is True
                assert [tuple(x) for x in page.evaluate("() => window.__fcCancelPosts")] == [("POST", expected_cancel_url)] * 2
                rem_ms_2 = max(500, int(12000 - page.evaluate("() => performance.now() - window.__fcWindowStartMs")))
                page.wait_for_function("() => window.__fcBodyAbortedBySignal === true", timeout=rem_ms_2)
                phase[0] = "signal_abort_body"
                assert page.evaluate("() => window.__fcBodyAbortedAtMs - window.__fcWindowStartMs") <= 12000
                expect(page.get_by_test_id(f"timeline-run-state-{rid_active}")).to_have_attribute("data-run-state", "cancelling")
                expect(page.get_by_test_id(f"timeline-run-state-{rid_active}")).to_contain_text("正在停止")
                assert "已取消" not in page.get_by_test_id(f"timeline-run-state-{rid_active}").inner_text()
                expect(page.get_by_test_id("run-id")).to_have_text(rid_hist)
                assert page.get_by_test_id("run-history-select").input_value() == rid_hist

                # 3rd explicit click proves Case 2 also released lock; real network reaches host cancelled state
                phase[0] = "real_cancel"
                page.evaluate("() => { window.__fcTestMode = 'off'; }")
                page.get_by_test_id(f"timeline-run-cancel-{rid_active}").click()
                expect(page.get_by_test_id(f"timeline-run-state-{rid_active}")).to_have_attribute(
                    "data-run-state", "cancelled", timeout=15000
                )
                expect(page.get_by_test_id(f"timeline-run-state-{rid_active}")).to_contain_text(
                    "已取消", timeout=15000
                )
                assert [tuple(x) for x in page.evaluate("() => window.__fcCancelPosts")] == [("POST", expected_cancel_url)] * 3
                assert (
                    httpx.get(
                        f"{srv.base_url}/api/v1/sessions/{s1}/runs/{rid_active}",
                        headers=auth_headers(cfg),
                        timeout=5.0,
                    ).json()["state"]
                    == "cancelled"
                )
                assert page_errors == [], _format_diag(page_error_details)
            finally:
                browser.close()



def test_cancel_resync_pending_read(tmp_path: Path) -> None:
    import httpx
    from app_helpers import auth_headers

    proj_dir = tmp_path / "resync_proj"
    data_dir = tmp_path / "resync_data"
    project_helpers.create_buggy_calc_project(proj_dir)

    base_cfg = project_helpers.make_project_host_config(tmp_path, allowed_roots=(proj_dir,))
    cfg = HostConfig(
        data_root=data_dir,
        workspace_parent=data_dir.parent / "workspaces",
        python_executable=base_cfg.python_executable,
        bind_port=base_cfg.bind_port,
        access_token=base_cfg.access_token,
        project_roots=(proj_dir,),
    )

    with LiveTcpTestServer(config=cfg) as srv:
        from forecontract_app.chat_runtime import OfflineReadonlyChatProvider

        ro_sc = OfflineReadonlyChatProvider()
        srv.app.state.service._chat_provider_factory = lambda **_: (
            lambda req: (
                {"action": "execution_offer", "goal": req.question, "summary": "执行任务"}
                if ("Historical run" in req.question or "Draft B" in req.question or "帮我改" in req.question or "Active run" in req.question or "Selected run" in req.question)
                else ro_sc(req)
            )
        )
        url = f"{srv.base_url}/#token={cfg.access_token}"
        created_run_ids: list[str] = []
        page_errors: list[str] = []
        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 800})
                page.on("pageerror", lambda err: page_errors.append(str(err)))

                def _on_resp(resp: Any) -> None:
                    if resp.request.method == "POST" and resp.request.url.endswith(("/runs", "/confirm")) and resp.status in (201, 202):
                        data = resp.json()
                        if isinstance(data, dict) and isinstance(data.get("run_id"), str):
                            created_run_ids.append(data["run_id"])

                page.on("response", _on_resp)
                page.add_init_script(
                    """(() => {
                      const origFetch = window.fetch.bind(window);
                      window.__fcArmOverlap = false;
                      window.__fcTargetSid = null;
                      window.__fcTargetRid = null;
                      window.__fcBatch1Signal = null;
                      window.__fcSameBatchSignal = false;
                      window.__fcReplacementSignalDistinct = false;
                      window.__fcOldExtHasSignal = false;
                      window.__fcOldExtBodyWaiting = false;
                      window.__fcOldExtAbortedBySignal = false;
                      window.__fcOldPropBodyWaiting = false;
                      window.__fcOldPropAbortedBySignal = false;
                      window.__fcUnhandledRejections = [];
                      window.addEventListener('unhandledrejection', (ev) => {
                        const r = ev.reason;
                        window.__fcUnhandledRejections.push(String((r && r.name) || r || 'unknown'));
                      });
                      window.fetch = async (input, init) => {
                        const reqUrl = typeof input === 'string' ? input : (input && input.url) || '';
                        const method = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();
                        let parsedUrl = null;
                        try {
                          parsedUrl = new URL(reqUrl, window.location.href);
                        } catch (_) {
                          parsedUrl = null;
                        }
                        const sid = window.__fcTargetSid;
                        if (
                          window.__fcArmOverlap
                          && sid
                          && parsedUrl
                          && parsedUrl.origin === window.location.origin
                          && method === 'POST'
                          && (parsedUrl.pathname === `/api/v1/sessions/${sid}/runs`
                              || (parsedUrl.pathname.startsWith(`/api/v1/sessions/${sid}/chat/turns/`) && parsedUrl.pathname.endsWith('/confirm')))
                        ) {
                          const createResp = await origFetch(input, init);
                          if (createResp.status === 201 || createResp.status === 202) {
                            const created = await createResp.clone().json();
                            if (created && typeof created.run_id === 'string') {
                              window.__fcTargetRid = created.run_id;
                            }
                          }
                          return createResp;
                        }
                        const rid = window.__fcTargetRid;
                        if (
                          window.__fcArmOverlap
                          && sid
                          && rid
                          && parsedUrl
                          && parsedUrl.origin === window.location.origin
                          && method === 'GET'
                        ) {
                          const extPath = `/api/v1/sessions/${sid}/runs/${rid}/extensions`;
                          const propPath = `/api/v1/sessions/${sid}/runs/${rid}/proposals`;
                          const sig = (init && init.signal) || null;
                          if (parsedUrl.pathname === extPath) {
                            const realResp = await origFetch(input, init);
                            if (sig && !window.__fcBatch1Signal) {
                              window.__fcBatch1Signal = sig;
                            }
                            if (sig && sig === window.__fcBatch1Signal) {
                              window.__fcOldExtHasSignal = true;
                              realResp.json = () => new Promise((_, reject) => {
                                window.__fcOldExtBodyWaiting = true;
                                if (sig.aborted) {
                                  window.__fcOldExtAbortedBySignal = true;
                                  reject(new DOMException('Aborted', 'AbortError'));
                                  return;
                                }
                                sig.addEventListener('abort', () => {
                                  window.__fcOldExtAbortedBySignal = true;
                                  reject(new DOMException('Aborted', 'AbortError'));
                                }, { once: true });
                              });
                              return realResp;
                            }
                            if (sig && window.__fcBatch1Signal && sig !== window.__fcBatch1Signal && realResp.ok) {
                              window.__fcReplacementSignalDistinct = (
                                window.__fcBatch1Signal.aborted === true && sig.aborted === false
                              );
                              const origJson = realResp.json.bind(realResp);
                              realResp.json = async () => {
                                const baseData = await origJson();
                                return {
                                  ...(baseData && typeof baseData === 'object' ? baseData : {}),
                                  selected_skills: [
                                    {
                                      id: 'resync-fresh-skill-v2',
                                      path: 'skills/resync_v2.md',
                                      sha256: 'abcdef1234567890',
                                    },
                                  ],
                                };
                              };
                              return realResp;
                            }
                            return realResp;
                          }
                          if (parsedUrl.pathname === propPath) {
                            const realResp = await origFetch(input, init);
                            if (sig && !window.__fcBatch1Signal) {
                              window.__fcBatch1Signal = sig;
                            }
                            if (sig && sig === window.__fcBatch1Signal) {
                              window.__fcSameBatchSignal = true;
                              realResp.json = () => new Promise((_, reject) => {
                                window.__fcOldPropBodyWaiting = true;
                                if (sig.aborted) {
                                  window.__fcOldPropAbortedBySignal = true;
                                  reject(new DOMException('Aborted', 'AbortError'));
                                  return;
                                }
                                sig.addEventListener('abort', () => {
                                  window.__fcOldPropAbortedBySignal = true;
                                  reject(new DOMException('Aborted', 'AbortError'));
                                }, { once: true });
                              });
                            }
                            return realResp;
                          }
                        }
                        return origFetch(input, init);
                      };
                    })();"""
                )

                page.goto(url, wait_until="domcontentloaded")
                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id("project-name").fill("resync-proj")
                page.get_by_test_id("register-root").fill(str(proj_dir))
                page.get_by_test_id("confirm-read").check()
                page.get_by_test_id("register-project").click()
                expect(page.get_by_test_id("project-list")).to_contain_text("resync-proj", timeout=10000)
                page.get_by_test_id("new-session").click()
                expect(page.locator('[data-testid^="session-item-"]')).to_have_count(1, timeout=10000)
                s1 = (
                    page.locator('[data-testid^="session-item-"]').nth(0).get_attribute("data-testid") or ""
                ).removeprefix("session-item-")

                page.get_by_test_id("nav-chat").click()
                page.get_by_test_id(f"chat-session-item-{s1}").click()
                expect(page.get_by_test_id("current-session")).to_have_text(s1, timeout=10000)

                # Arm controlled batch-1 body holds on extensions + sibling proposals for the same session/run
                page.evaluate(
                    "(sid) => { window.__fcTargetSid = sid; window.__fcArmOverlap = true; }",
                    s1,
                )
                page.get_by_test_id("chat-input").fill("Selected run cancel resync overlap")
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                page.locator('[data-testid^="offer-confirm-btn-"]').last.click()
                expect(page.locator('[data-testid^="timeline-run-"]').first).to_be_visible(timeout=15000)
                rid = page.locator('[data-testid^="timeline-run-id-"]').first.get_attribute("data-run-id") or ""
                assert rid
                assert len(created_run_ids) == 1
                assert rid == created_run_ids[0]
                assert page.evaluate("() => window.__fcTargetRid") == rid

                # Wait until both batch-1 extensions body and sibling proposals body are actively suspended on the same signal
                page.wait_for_function(
                    "() => window.__fcOldExtBodyWaiting === true && window.__fcOldPropBodyWaiting === true && window.__fcSameBatchSignal === true",
                    timeout=15000,
                )
                assert page.evaluate("() => window.__fcOldExtHasSignal") is True

                # Trigger public cancel on the selected run -> handleCancelRun -> syncSnapshotAndStream -> abortPendingReads
                page.get_by_test_id(f"timeline-run-cancel-{rid}").click()
                page.wait_for_function(
                    "() => window.__fcOldExtAbortedBySignal === true && window.__fcOldPropAbortedBySignal === true",
                    timeout=10000,
                )
                expect(page.get_by_test_id(f"timeline-run-state-{rid}")).to_have_attribute(
                    "data-run-state", "cancelled", timeout=15000
                )
                expect(page.get_by_test_id(f"timeline-run-state-{rid}")).to_contain_text(
                    "已取消", timeout=15000
                )
                expect(page.locator("#extension-result")).to_contain_text(
                    "resync-fresh-skill-v2(skills/resync_v2.md, sha=abcdef123456)",
                    timeout=10000,
                )
                assert page.evaluate("() => window.__fcReplacementSignalDistinct") is True
                assert (
                    httpx.get(
                        f"{srv.base_url}/api/v1/sessions/{s1}/runs/{rid}",
                        headers=auth_headers(cfg),
                        timeout=5.0,
                    ).json()["state"]
                    == "cancelled"
                )
                final_ext_text = page.locator("#extension-result").inner_text()
                assert "resync-fresh-skill-v2(skills/resync_v2.md, sha=abcdef123456)" in final_ext_text
                assert "stale-batch-skill" not in final_ext_text
                assert page.evaluate("() => window.__fcUnhandledRejections") == []
                assert page_errors == []
            finally:
                browser.close()


def test_one_send_followup_approval_refresh(tmp_path: Path) -> None:
    """Verify single-entry composer, IME/Enter/Shift+Enter, offer refresh zero-start, confirm->approve->followup."""
    import json
    import time

    import httpx
    from forecontract_app.live_chat_provider import LiveChatProvider
    from forecontract_app.live_provider import LiveQuota
    from forecontract_app.models import LiveConfig

    t0 = time.monotonic()

    def _log_stage(name: str, extra: str = "") -> None:
        msg = f"[stage] {name} elapsed={time.monotonic() - t0:.3f}s"
        if extra:
            msg = f"{msg} {extra}"
        print(msg, flush=True)

    proj_dir = project_helpers.create_buggy_calc_project(tmp_path / "one_send_proj")
    before_calc = (proj_dir / "calc.py").read_bytes()
    cfg = project_helpers.make_project_host_config(tmp_path, allowed_roots=(proj_dir,))
    live_cfg = LiveConfig(
        api_key="sk-synth-local-test",
        model="deepseek-flash",
        max_requests=20,
        budget_cny=1.0,
    )
    quota = LiveQuota(config=live_cfg)
    provider_calls: list[dict[str, Any]] = []

    def _mock_transport_handler(http_req: httpx.Request) -> httpx.Response:
        wire = json.loads(http_req.content.decode("utf-8"))
        user_payload = json.loads(wire["messages"][1]["content"])
        ev_list = list(user_payload.get("evidence") or [])
        question = str(user_payload.get("question") or "")
        provider_calls.append({"question": question, "evidence": ev_list})
        if "修好" in question:
            reply_obj = {
                "action": "execution_offer",
                "goal": "Fix add_numbers in calc.py and run pytest",
                "summary": "将把 calc.py 的减法修复为加法并运行测试",
            }
        elif "刚才改了哪行" in question:
            diff_evs = [ev for ev in ev_list if isinstance(ev, dict) and ev.get("kind") == "diff_hunk"]
            if not diff_evs:
                reply_obj = {"action": "final", "assistant_message": "当前会话没有可用的历史运行差异证据。"}
            else:
                ev = diff_evs[0]
                changed = [
                    ln
                    for ln in str(ev.get("excerpt") or "").splitlines()
                    if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))
                ]
                reply_obj = {
                    "action": "final",
                    "assistant_message": f"根据 {ev['path']} 差异证据：{' | '.join(changed)}",
                }
        else:
            reply_obj = {"action": "final", "assistant_message": "calc.py 的 add_numbers 错误使用了减法。"}
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {"content": json.dumps(reply_obj, ensure_ascii=False)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 40},
            },
        )

    transport = httpx.MockTransport(_mock_transport_handler)

    def _chat_factory(*, project, session_id: str, turn_id: str):
        _ = (project, session_id, turn_id)
        return LiveChatProvider(config=live_cfg, quota=quota, transport=transport)

    with LiveTcpTestServer(config=cfg) as srv:
        srv.app.state.service._chat_provider_factory = _chat_factory
        url = f"{srv.base_url}/#token={cfg.access_token}"
        hdrs = {"Authorization": f"Bearer {cfg.access_token}"}
        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            page = None
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 800})
                post_reqs: list[str] = []
                page.on(
                    "request",
                    lambda req: (
                        post_reqs.append(req.url)
                        if req.method == "POST"
                        and (
                            req.url.endswith("/chat/turns")
                            or req.url.endswith("/confirm")
                            or req.url.endswith("/runs")
                        )
                        else None
                    ),
                )
                page.goto(url, wait_until="domcontentloaded")
                expect(page.get_by_test_id("chat-input")).to_have_attribute(
                    "placeholder", "给 ForeContract 发消息…"
                )
                assert page.get_by_test_id("offline-banner").count() == 0
                assert page.get_by_test_id("model").count() == 0
                assert page.get_by_test_id("composer-execute").count() == 0

                # Register project & create session
                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id("project-name").fill("one-send")
                page.get_by_test_id("register-root").fill(str(proj_dir))
                page.get_by_test_id("confirm-read").check()
                page.get_by_test_id("register-project").click()
                expect(page.get_by_test_id("current-project")).not_to_have_text("-", timeout=10000)
                page.get_by_test_id("new-session").click()
                expect(page.get_by_test_id("current-session")).not_to_have_text("-", timeout=10000)
                sid = page.get_by_test_id("current-session").inner_text().strip()
                page.get_by_test_id("nav-chat").click()

                # IME composing Enter must NOT submit; Shift+Enter inserts newline; Enter submits
                chat_in = page.get_by_test_id("chat-input")
                chat_in.click()
                chat_in.fill("为什么 calc.py 会算错")
                chat_in.dispatch_event("keydown", {"key": "Enter", "isComposing": True})
                assert page.locator('[data-testid^="chat-turn-"]').count() == 0
                chat_in.press("Shift+Enter")
                assert "\n" in chat_in.input_value()
                chat_in.fill("为什么 calc.py 会算错，先别改")
                chat_in.press("Enter")
                expect(page.locator('[data-testid^="chat-turn-"]').first).to_contain_text(
                    "错误使用了减法", timeout=10000
                )
                assert (proj_dir / "calc.py").read_bytes() == before_calc

                # Follow-up -> execution_offer -> reload before confirm must NOT start any run or reserve quota
                assert quota.requests_sent == 1
                reserved_after_turn1 = quota.reserved_cny
                assert reserved_after_turn1 > 0.0
                chat_in.fill("那就修好它，再跑测试")
                page.get_by_test_id("chat-send").click()
                offer_card = page.locator('[data-testid^="timeline-offer-"]').first
                expect(offer_card).to_be_visible(timeout=10000)
                assert quota.requests_sent == 2
                assert quota.reserved_cny > reserved_after_turn1
                pre_reload_posts = len(post_reqs)
                pre_reload_calls = len(provider_calls)
                pre_reload_sent = quota.requests_sent
                pre_reload_reserved = quota.reserved_cny
                page.reload(wait_until="domcontentloaded")
                expect(page.get_by_test_id("current-session")).to_have_text(sid, timeout=10000)
                expect(page.locator('[data-testid^="timeline-offer-"]').first).to_be_visible(timeout=10000)
                assert len(post_reqs) == pre_reload_posts
                assert len(provider_calls) == pre_reload_calls
                assert quota.requests_sent == pre_reload_sent
                assert quota.reserved_cny == pre_reload_reserved
                assert httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{sid}/history",
                    headers=hdrs,
                    timeout=5.0,
                ).json()["items"] == []

                # Confirm offer -> approve scope/verification/command -> verify calc.py fixed -> followup
                page.locator('[data-testid^="offer-confirm-scope-"]').first.check()
                page.locator('[data-testid^="offer-confirm-btn-"]').first.click()
                scope_item = page.locator('[data-testid^="proposal-item-"]', has_text="写入范围").first
                expect(scope_item).to_be_visible(timeout=15000)
                scope_item.locator('[data-testid^="proposal-approve-"]').click()
                verif_item = page.locator('[data-testid^="proposal-item-"]', has_text="验证计划").first
                expect(verif_item).to_be_visible(timeout=15000)
                verif_item.locator('[data-testid^="proposal-approve-"]').click()
                cmd_item = page.locator('[data-testid^="proposal-item-"]', has_text="本机命令").first
                expect(cmd_item).to_be_visible(timeout=15000)
                cmd_item.locator('[data-testid^="proposal-trust-"]').check()
                cmd_item.locator('[data-testid^="proposal-approve-"]').click()

                expect(page.locator('[data-testid^="timeline-run-state-"]').first).to_have_attribute(
                    "data-run-state", "succeeded", timeout=30000
                )
                assert "return a + b" in (proj_dir / "calc.py").read_text("utf-8")
                hist_done = httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{sid}/history",
                    headers=hdrs,
                    timeout=5.0,
                ).json()["items"]
                rid = next(it["run_id"] for it in hist_done if it.get("run_id"))
                usage_before = httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{sid}/runs/{rid}/usage",
                    headers=hdrs,
                    timeout=5.0,
                ).json()

                # Refresh and verify zero delta in POSTs, model calls, run set, and quota reservations
                pre_done_posts = len(post_reqs)
                pre_done_calls = len(provider_calls)
                pre_done_sent = quota.requests_sent
                pre_done_reserved = quota.reserved_cny
                page.reload(wait_until="domcontentloaded")
                expect(page.get_by_test_id("current-session")).to_have_text(sid, timeout=10000)
                expect(page.get_by_test_id(f"timeline-run-state-{rid}")).to_have_attribute(
                    "data-run-state", "succeeded", timeout=10000
                )
                assert len(post_reqs) == pre_done_posts
                assert len(provider_calls) == pre_done_calls
                assert quota.requests_sent == pre_done_sent
                assert quota.reserved_cny == pre_done_reserved
                hist_after = httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{sid}/history",
                    headers=hdrs,
                    timeout=5.0,
                ).json()["items"]
                assert sorted({it["run_id"] for it in hist_after if it.get("run_id")}) == [rid]
                usage_after = httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{sid}/runs/{rid}/usage",
                    headers=hdrs,
                    timeout=5.0,
                ).json()
                assert usage_after["calls_started"] == usage_before["calls_started"]
                assert usage_after["calls_completed"] == usage_before["calls_completed"]

                # Ask follow-up without manual references -> service auto-selects persisted offer-bound diff_hunk evidence
                turns_before = httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{sid}/chat/turns",
                    headers=hdrs,
                    timeout=5.0,
                ).json()["items"]
                prev_req_ids = {t["client_request_id"] for t in turns_before}
                assert len(turns_before) == 2

                req_key = "unknown"
                resp_status: int | str = "none"
                err_summary = ""
                stage = "before_send"
                captured: dict[str, Any] = {
                    "req": None,
                    "req_body": {},
                    "req_key": "unknown",
                    "resp": None,
                    "status": "none",
                    "failed": None,
                }
                target_url_suffix = f"/api/v1/sessions/{sid}/chat/turns"

                def _on_followup_req(req: Any) -> None:
                    if req.method == "POST" and req.url.endswith(target_url_suffix) and captured["req"] is None:
                        captured["req"] = req
                        try:
                            body = req.post_data_json or {}
                        except (ValueError, RuntimeError):
                            body = {}
                        captured["req_body"] = body if isinstance(body, dict) else {}
                        captured["req_key"] = str(captured["req_body"].get("client_request_id") or "unknown")
                        _log_stage("followup_request_seen", f"key={captured['req_key']}")

                def _on_followup_resp(resp: Any) -> None:
                    if captured["req"] is not None and resp.request == captured["req"]:
                        captured["resp"] = resp
                        captured["status"] = resp.status
                        _log_stage("followup_response_seen", f"status={resp.status}")

                def _on_followup_fail(req: Any) -> None:
                    if captured["req"] is not None and req == captured["req"]:
                        captured["failed"] = req.failure or "requestfailed"
                        _log_stage("followup_requestfailed_seen", f"err={captured['failed']}")

                page.on("request", _on_followup_req)
                page.on("response", _on_followup_resp)
                page.on("requestfailed", _on_followup_fail)
                try:
                    _log_stage("followup_click_send_enter")
                    stage = "click_send"
                    page.get_by_test_id("chat-input").fill("刚才改了哪行")
                    page.get_by_test_id("chat-send").click()
                    _log_stage("followup_click_send_exit")

                    stage = "await_network"
                    deadline = time.monotonic() + 15.0
                    while time.monotonic() < deadline:
                        if captured["resp"] is not None or captured["failed"] is not None:
                            break
                        page.wait_for_timeout(50)

                    req_key = captured["req_key"]
                    resp_status = captured["status"]
                    req_body = captured["req_body"]
                    followup_resp = captured["resp"]

                    by_req: dict[str, Any] | None = None
                    by_req_http: int | str = "none"
                    if req_key != "unknown":
                        _log_stage("followup_by_request_enter", f"key={req_key} post_status={resp_status}")
                        stage = "by_request_readback"
                        poll_deadline = time.monotonic() + 10.0
                        by_req_r = httpx.get(
                            f"{srv.base_url}/api/v1/sessions/{sid}/chat/turns/by-request/{req_key}",
                            headers=hdrs,
                            timeout=5.0,
                        )
                        by_req_http = by_req_r.status_code
                        if by_req_r.status_code == 200:
                            by_req = by_req_r.json()
                            while by_req.get("status") == "running" and time.monotonic() < poll_deadline:
                                page.wait_for_timeout(100)
                                poll_r = httpx.get(
                                    f"{srv.base_url}/api/v1/sessions/{sid}/chat/turns/by-request/{req_key}",
                                    headers=hdrs,
                                    timeout=5.0,
                                )
                                by_req_http = poll_r.status_code
                                if poll_r.status_code == 200:
                                    by_req = poll_r.json()
                                else:
                                    break
                        _log_stage(
                            "followup_by_request_exit",
                            f"http={by_req_http} status={(by_req or {}).get('status')} seq={(by_req or {}).get('seq')}",
                        )

                    stage = "assert_post_and_persisted"
                    if captured["req"] is None:
                        in_val = page.get_by_test_id("chat-input").input_value()
                        btn_dis = page.get_by_test_id("chat-send").is_disabled()
                        raise AssertionError(f"no_post_sent input={in_val!r} btn_disabled={btn_dis} sid={sid}")
                    if captured["failed"] is not None:
                        raise AssertionError(f"request_failed key={req_key} by_req_http={by_req_http} err={captured['failed']}")
                    if followup_resp is None:
                        raise AssertionError(f"post_response_timeout key={req_key} by_req_http={by_req_http} sid={sid}")
                    assert followup_resp.request == captured["req"]
                    assert req_body.get("text") == "刚才改了哪行"
                    assert req_body.get("intent_mode") == "conversation"
                    assert not req_body.get("references")
                    assert req_key != "unknown" and req_key not in prev_req_ids
                    assert followup_resp.status in (200, 201, 202), f"post_status={followup_resp.status} by_req_http={by_req_http}"
                    assert by_req is not None
                    assert by_req["turn_id"]
                    assert by_req["session_id"] == sid
                    assert by_req["client_request_id"] == req_key
                    assert by_req["seq"] == 3
                    assert by_req["status"] == "completed"
                    assert by_req["user_message"] == "刚才改了哪行"
                    assistant_txt = str(by_req.get("assistant_message") or "")
                    assert assistant_txt

                    _log_stage("followup_bubble_enter", f"seq={by_req['seq']}")
                    stage = "ui_bubble_and_quota"
                    turn_seq = by_req["seq"]
                    new_bubble = page.get_by_test_id(f"chat-turn-{turn_seq}")
                    expect(page.get_by_test_id("current-session")).to_have_text(sid)
                    expect(new_bubble).to_be_visible(timeout=10000)
                    expect(new_bubble).to_contain_text(assistant_txt, timeout=10000)
                    expect(new_bubble).to_contain_text("calc.py", timeout=10000)
                    expect(new_bubble).to_contain_text("+    return a + b", timeout=10000)
                    assert (
                        len(
                            [
                                u
                                for u in post_reqs[pre_done_posts:]
                                if u.endswith(target_url_suffix)
                            ]
                        )
                        == 1
                    )
                    assert quota.requests_sent == pre_done_sent + 1
                    assert quota.reserved_cny > pre_done_reserved
                    _log_stage("followup_bubble_exit")
                except Exception as exc:
                    print(
                        "[diag:followup]",
                        f"stage={stage}",
                        f"sid={sid}",
                        f"req_key={req_key}",
                        f"status={resp_status}",
                        f"err={err_summary or type(exc).__name__}",
                        flush=True,
                    )
                    raise
                finally:
                    page.remove_listener("request", _on_followup_req)
                    page.remove_listener("response", _on_followup_resp)
                    page.remove_listener("requestfailed", _on_followup_fail)

                _log_stage("followup_wire_check_enter")
                run_json = httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{sid}/runs/{rid}",
                    headers=hdrs,
                    timeout=5.0,
                ).json()
                assert run_json["state"] == "succeeded"
                entries = run_json["result"]["diff"]["entries"]
                host_ent = next(e for e in entries if e["path"] == "calc.py")
                host_changed = [
                    ln
                    for ln in str(host_ent["diff_excerpt"]).splitlines()
                    if ln.startswith(("+", "-")) and not ln.startswith(("+++", "---"))
                ]
                sent_evs = provider_calls[-1]["evidence"]
                assert len(sent_evs) >= 1
                ev = sent_evs[0]
                assert isinstance(ev, dict)
                assert ev["kind"] == "diff_hunk"
                assert ev["run_id"] == rid
                assert ev["path"] == host_ent["path"]
                assert ev["before_sha256"] == host_ent["before_sha256"]
                assert ev["after_sha256"] == host_ent["after_sha256"]
                for ch_ln in host_changed:
                    assert ch_ln in ev["excerpt"]
                _log_stage("followup_wire_check_exit")
            finally:
                _log_stage("browser_close_enter")
                if page is not None and not page.is_closed():
                    page.close()
                browser.close()
                _log_stage("browser_close_exit")
            _log_stage("playwright_exit_enter")
        _log_stage("playwright_exit_exit")
        _log_stage("server_exit_enter")
    _log_stage("server_exit_exit")
