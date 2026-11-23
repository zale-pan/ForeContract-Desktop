"""Browser end-to-end tests for CHAT-T01 read-only timeline, refresh, layout, and execution entry."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import project_helpers
from forecontract_app.session_models import SESSION_DB_FILENAME
from playwright.sync_api import expect, sync_playwright
from test_session_browser import LiveTcpTestServer, _launch_chromium


def test_chat_refresh_and_followup(tmp_path: Path) -> None:
    proj_dir = project_helpers.create_buggy_calc_project(tmp_path / "chat-browser-proj")
    before_calc_sha = project_helpers.sha256_file(proj_dir / "calc.py")
    cfg = project_helpers.make_project_host_config(tmp_path, allowed_roots=(proj_dir,))

    with LiveTcpTestServer(config=cfg) as srv:
        from forecontract_app.chat_runtime import OfflineReadonlyChatProvider

        srv.app.state.service._chat_provider_factory = lambda **_: OfflineReadonlyChatProvider()
        url = f"{srv.base_url}/#token={cfg.access_token}"
        console_errors: list[dict[str, str]] = []
        page_errors: list[str] = []
        run_posts: list[str] = []

        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 800})
                page.on(
                    "console",
                    lambda m: console_errors.append(
                        {"text": m.text, "url": str((m.location or {}).get("url", ""))}
                    )
                    if m.type == "error"
                    else None,
                )
                page.on("pageerror", lambda err: page_errors.append(str(err)))
                page.on(
                    "request",
                    lambda r: run_posts.append(r.url)
                    if r.method == "POST" and r.url.endswith("/runs")
                    else None,
                )

                page.goto(url, wait_until="domcontentloaded")
                expect(page.get_by_test_id("host-status-banner")).to_be_visible()

                # Register project and create one session explicitly
                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id("project-name").fill("chat-proj")
                page.get_by_test_id("register-root").fill(str(proj_dir))
                page.get_by_test_id("confirm-read").check()
                page.get_by_test_id("register-project").click()
                expect(page.get_by_test_id("project-list")).to_contain_text("chat-proj")
                page.get_by_test_id("new-session").click()
                expect(page.get_by_test_id("current-session")).not_to_have_text("-", timeout=10000)
                page.get_by_test_id("nav-chat").click()

                # Turn 1: ask about calc.py without checking confirm-scope
                page.get_by_test_id("chat-input").fill("calc.py 为什么会算错？")
                page.get_by_test_id("chat-send").click()

                turn1 = page.get_by_test_id("chat-turn-1")
                expect(turn1).to_be_visible(timeout=15000)
                expect(turn1).to_contain_text("return a - b")

                # Expand collapsible tool card and open calc.py in right drawer
                tool_card = page.get_by_test_id("chat-tool-card-1")
                tool_card.locator("summary").click()
                calc_btn = tool_card.locator("button", has_text="calc.py").first
                calc_btn.click()
                expect(page.get_by_test_id("chat-inspector")).to_be_visible()
                expect(page.get_by_test_id("workspace-file-preview")).to_contain_text(
                    "return a - b", timeout=10000
                )

                # Reload page -> history replayed, zero runs, follow-up works in same session
                page.reload(wait_until="domcontentloaded")
                expect(page.get_by_test_id("chat-turn-1")).to_be_visible(timeout=10000)
                expect(page.get_by_test_id("chat-turn-1")).to_contain_text("return a - b")

                page.get_by_test_id("chat-input").fill("追问 tests/test_calc.py 的测试覆盖范围")
                page.get_by_test_id("chat-send").click()
                turn2 = page.get_by_test_id("chat-turn-2")
                expect(turn2).to_be_visible(timeout=15000)
                expect(turn2).to_contain_text("test_calc.py")
                expect(turn2).to_contain_text("尚未")

                # Verify zero console/page errors during normal chat + reload + follow-up
                assert console_errors == []
                assert page_errors == []

                # Create session 2 & session 3 to verify late error (after readErrorPayload enters resp.json)
                # and late success do not leak into newly selected session
                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id("new-session").click()
                expect(page.locator('[data-testid^="session-item-"]')).to_have_count(2, timeout=10000)
                page.get_by_test_id("new-session").click()
                expect(page.locator('[data-testid^="session-item-"]')).to_have_count(3, timeout=10000)

                sess_items = page.locator('[data-testid^="session-item-"]')
                s2_id = (sess_items.nth(1).get_attribute("data-testid") or "").removeprefix("session-item-")
                s3_id = (sess_items.nth(2).get_attribute("data-testid") or "").removeprefix("session-item-")
                page.get_by_test_id("nav-chat").click()

                # Switch to session 2, wrap Response.prototype.json once for 409 so readErrorPayload suspends
                page.get_by_test_id(f"chat-session-item-{s2_id}").click()
                expect(page.get_by_test_id("current-session")).to_have_text(s2_id, timeout=10000)
                page.evaluate(
                    """() => {
                      window.__errJsonEntered = false;
                      window.__errJsonSettled = false;
                      const origJson = Response.prototype.json;
                      let armed = true;
                      Response.prototype.json = function() {
                        if (armed && this.status === 409) {
                          armed = false;
                          Response.prototype.json = origJson;
                          window.__errJsonEntered = true;
                          const p = origJson.call(this);
                          return new Promise((resolve, reject) => {
                            window.__releaseErrJson = () => {
                              p.then((v) => {
                                setTimeout(() => { window.__errJsonSettled = true; }, 0);
                                resolve(v);
                              }, (e) => {
                                setTimeout(() => { window.__errJsonSettled = true; }, 0);
                                reject(e);
                              });
                            };
                          });
                        }
                        return origJson.call(this);
                      };
                    }"""
                )
                expected_err_url = f"{srv.base_url}/api/v1/sessions/{s2_id}/chat/turns"
                err_409_responses: list[str] = []

                def _watch_409(resp):
                    if resp.request.method == "POST" and resp.url == expected_err_url and resp.status == 409:
                        err_409_responses.append(resp.url)

                page.on("response", _watch_409)
                page.route(
                    f"**/api/v1/sessions/{s2_id}/chat/turns",
                    lambda route: route.fulfill(
                        status=409,
                        json={"error": {"code": "IDEMPOTENCY_CONFLICT", "message": "LATE_ERR_FROM_A"}},
                    )
                    if route.request.method == "POST"
                    else route.continue_(),
                )
                page.get_by_test_id("chat-input").fill("Late error from S2")
                page.get_by_test_id("chat-send").click()
                page.wait_for_function("() => window.__errJsonEntered === true", timeout=10000)

                # Switch to session 3 before releasing readErrorPayload's resp.json()
                page.get_by_test_id(f"chat-session-item-{s3_id}").click()
                expect(page.get_by_test_id("current-session")).to_have_text(s3_id, timeout=10000)
                page.get_by_test_id("chat-input").fill("Keep S3 text")
                page.evaluate("() => window.__releaseErrJson()")
                page.wait_for_function("() => window.__errJsonSettled === true", timeout=10000)
                expect(page.get_by_test_id("error")).not_to_contain_text("LATE_ERR_FROM_A")
                expect(page.get_by_test_id("chat-input")).to_have_value("Keep S3 text")
                expect(page.get_by_test_id("chat-composer-status")).to_have_text("")
                expect(page.get_by_test_id("chat-turn-1")).to_have_count(0)
                page.unroute(f"**/api/v1/sessions/{s2_id}/chat/turns")
                assert len(err_409_responses) == 1
                allowed_409_texts = {
                    "Failed to load resource: the server responded with a status of 409 (Conflict)",
                    "Failed to load resource: the server responded with a status of 409 ()",
                }
                attributed_409 = [
                    e
                    for e in console_errors
                    if e["url"] == expected_err_url and e["text"].strip() in allowed_409_texts
                ]
                unexpected_errs = [e for e in console_errors if e not in attributed_409]
                assert len(attributed_409) <= 1
                assert unexpected_errs == []
                console_errors.clear()

                # Now test late success: hold POST on session 2, switch to session 3, then fulfill
                page.get_by_test_id(f"chat-session-item-{s2_id}").click()
                expect(page.get_by_test_id("current-session")).to_have_text(s2_id, timeout=10000)
                held_ok_routes = []

                def _hold_ok(route):
                    if route.request.method == "POST":
                        held_ok_routes.append(route)
                    else:
                        route.continue_()

                page.route(f"**/api/v1/sessions/{s2_id}/chat/turns", _hold_ok)
                page.evaluate(
                    """() => {
                      window.__lateOkDone = false;
                      const origFetch = window.fetch;
                      window.fetch = function(url, opts) {
                        const p = origFetch.apply(this, arguments);
                        if (typeof url === 'string' && url.includes('/chat/turns') && opts && opts.method === 'POST') {
                          p.finally(() => { setTimeout(() => { window.__lateOkDone = true; }, 0); });
                          window.fetch = origFetch;
                        }
                        return p;
                      };
                    }"""
                )
                page.get_by_test_id("chat-input").fill("Late ok from S2")
                page.get_by_test_id("chat-send").click()
                deadline = 100
                while deadline > 0 and len(held_ok_routes) == 0:
                    page.wait_for_timeout(30)
                    deadline -= 1
                assert len(held_ok_routes) == 1

                page.get_by_test_id(f"chat-session-item-{s3_id}").click()
                expect(page.get_by_test_id("current-session")).to_have_text(s3_id, timeout=10000)
                page.get_by_test_id("chat-input").fill("Keep S3 ok")
                held_ok_routes.pop(0).fulfill(
                    status=201,
                    json={
                        "turn_id": "00000000-0000-4000-8000-000000000099",
                        "project_id": "00000000-0000-4000-8000-000000000001",
                        "session_id": s2_id,
                        "client_request_id": "00000000-0000-4000-8000-000000000098",
                        "seq": 1,
                        "status": "completed",
                        "user_message": "Late ok from S2",
                        "tool_reads": [],
                        "assistant_message": "LATE_REPLY_FROM_S2",
                        "error_code": None,
                        "error_message": None,
                        "provider_mode": "offline",
                        "context_truncated": False,
                        "created_at": "2026-11-20T00:00:00+00:00",
                        "updated_at": "2026-11-20T00:00:00+00:00",
                    },
                )
                page.wait_for_function("() => window.__lateOkDone === true", timeout=10000)
                expect(page.get_by_test_id("chat-input")).to_have_value("Keep S3 ok")
                expect(page.get_by_test_id("chat-timeline")).not_to_contain_text("LATE_REPLY_FROM_S2")
                expect(page.get_by_test_id("error")).to_have_text("")
                expect(page.get_by_test_id("chat-composer-status")).to_have_text("")

                # Switching back to session 2: original sent draft must now be empty
                page.get_by_test_id(f"chat-session-item-{s2_id}").click()
                expect(page.get_by_test_id("current-session")).to_have_text(s2_id, timeout=10000)
                expect(page.get_by_test_id("chat-input")).to_have_value("")

                # Also verify: if user types new text in S2 after sending, switching to S3 and releasing
                # S2's success preserves the new unsent text when returning to S2
                page.evaluate(
                    """() => {
                      window.__lateOk2Done = false;
                      const origFetch = window.fetch;
                      window.fetch = function(url, opts) {
                        const p = origFetch.apply(this, arguments);
                        if (typeof url === 'string' && url.includes('/chat/turns') && opts && opts.method === 'POST') {
                          p.finally(() => { setTimeout(() => { window.__lateOk2Done = true; }, 0); });
                          window.fetch = origFetch;
                        }
                        return p;
                      };
                    }"""
                )
                page.get_by_test_id("chat-input").fill("First sent from S2")
                page.get_by_test_id("chat-send").click()
                deadline2 = 100
                while deadline2 > 0 and len(held_ok_routes) == 0:
                    page.wait_for_timeout(30)
                    deadline2 -= 1
                assert len(held_ok_routes) == 1
                page.get_by_test_id("chat-input").fill("New unsent edit in S2")
                page.get_by_test_id(f"chat-session-item-{s3_id}").click()
                expect(page.get_by_test_id("current-session")).to_have_text(s3_id, timeout=10000)
                held_ok_routes.pop(0).fulfill(
                    status=201,
                    json={
                        "turn_id": "00000000-0000-4000-8000-000000000100",
                        "project_id": "00000000-0000-4000-8000-000000000001",
                        "session_id": s2_id,
                        "client_request_id": "00000000-0000-4000-8000-000000000101",
                        "seq": 2,
                        "status": "completed",
                        "user_message": "First sent from S2",
                        "tool_reads": [],
                        "assistant_message": "LATE_REPLY_2_FROM_S2",
                        "error_code": None,
                        "error_message": None,
                        "provider_mode": "offline",
                        "context_truncated": False,
                        "created_at": "2026-11-20T00:00:00+00:00",
                        "updated_at": "2026-11-20T00:00:00+00:00",
                    },
                )
                page.wait_for_function("() => window.__lateOk2Done === true", timeout=10000)
                page.get_by_test_id(f"chat-session-item-{s2_id}").click()
                expect(page.get_by_test_id("current-session")).to_have_text(s2_id, timeout=10000)
                expect(page.get_by_test_id("chat-input")).to_have_value("New unsent edit in S2")
                page.unroute(f"**/api/v1/sessions/{s2_id}/chat/turns")

                assert run_posts == []
                assert project_helpers.sha256_file(proj_dir / "calc.py") == before_calc_sha
                assert console_errors == []
                assert page_errors == []
            finally:
                browser.close()

    # Verify SQLite has 2 chat_turns in session 1 and 0 runs
    conn = sqlite3.connect(str(cfg.data_root / SESSION_DB_FILENAME))
    try:
        turn_count = conn.execute("SELECT COUNT(*) FROM chat_turns").fetchone()[0]
        run_count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        assert turn_count == 2
        assert run_count == 0
    finally:
        conn.close()

    # Independent fixture for F01 multi-page timeline (50 seeded + 1 browser-sent = 51 turns)
    import asyncio
    import uuid

    from forecontract_app.models import ModelSelection
    from forecontract_app.project_models import RegisterProjectRequest
    from forecontract_app.service import AppService

    pag_root = tmp_path / "pag_fixture"
    pag_proj_dir = project_helpers.create_buggy_calc_project(pag_root / "pag-proj")
    pag_cfg = project_helpers.make_project_host_config(pag_root, allowed_roots=(pag_proj_dir,))

    async def _seed_50() -> None:
        svc = AppService(config=pag_cfg)
        try:
            proj = await svc.register_project(
                request=RegisterProjectRequest(
                    name="pag-proj", root=str(pag_proj_dir), confirmed_read=True
                )
            )
            sess = svc.create_session(
                project_id=proj.project_id,
                model=ModelSelection(provider="offline", model="offline-project-v1"),
            )
            for idx in range(1, 51):
                t = svc._store.insert_chat_turn_running(
                    turn_id=str(uuid.uuid4()),
                    project_id=proj.project_id,
                    session_id=sess.session_id,
                    client_request_id=str(uuid.uuid4()),
                    payload_digest=f"{idx:064x}",
                    user_message=f"Seeded turn #{idx}",
                    provider_mode="offline",
                    created_at="2026-11-20T00:00:00+00:00",
                )
                svc._store.finalize_chat_turn(
                    turn=t.model_copy(
                        update={
                            "status": "completed",
                            "assistant_message": f"Seeded reply #{idx}",
                            "updated_at": "2026-11-20T00:00:01+00:00",
                        }
                    )
                )
        finally:
            await svc.aclose()

    asyncio.run(_seed_50())

    with LiveTcpTestServer(config=pag_cfg) as pag_srv:
        pag_requests: list[str] = []
        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 800})
                page.on(
                    "request",
                    lambda r: pag_requests.append(r.url)
                    if "/chat/turns?" in r.url
                    else None,
                )
                page.goto(f"{pag_srv.base_url}/#token={pag_cfg.access_token}", wait_until="domcontentloaded")
                expect(page.get_by_test_id("chat-turn-1")).to_be_visible(timeout=10000)
                expect(page.get_by_test_id("chat-turn-50")).to_be_visible(timeout=10000)

                page.get_by_test_id("chat-input").fill("Final 51st browser turn")
                page.get_by_test_id("chat-send").click()
                expect(page.get_by_test_id("chat-turn-51")).to_be_visible(timeout=15000)
                expect(page.locator('[data-testid^="chat-turn-"]')).to_have_count(51)
                assert any("after_seq=50" in u for u in pag_requests)

                page.reload(wait_until="domcontentloaded")
                expect(page.get_by_test_id("chat-turn-1")).to_be_visible(timeout=10000)
                expect(page.get_by_test_id("chat-turn-51")).to_be_visible(timeout=10000)
                expect(page.locator('[data-testid^="chat-turn-"]')).to_have_count(51)
                seqs = page.evaluate(
                    "() => Array.from(document.querySelectorAll('[data-testid^=\"chat-turn-\"]')).map(el => el.getAttribute('data-testid'))"
                )
                assert seqs == [f"chat-turn-{i}" for i in range(1, 52)]
            finally:
                browser.close()


def test_chat_layout_and_execution_entry(tmp_path: Path) -> None:
    proj_dir = project_helpers.create_buggy_calc_project(tmp_path / "chat-layout-proj")
    cfg = project_helpers.make_project_host_config(tmp_path, allowed_roots=(proj_dir,))

    with LiveTcpTestServer(config=cfg) as srv:
        from forecontract_app.chat_runtime import OfflineReadonlyChatProvider

        srv.app.state.service._chat_provider_factory = lambda **_: OfflineReadonlyChatProvider()
        url = f"{srv.base_url}/#token={cfg.access_token}"
        console_errors: list[str] = []
        page_errors: list[str] = []
        run_or_cancel_posts: list[str] = []

        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 800})
                page.on(
                    "console",
                    lambda m: console_errors.append(m.text) if m.type == "error" else None,
                )
                page.on("pageerror", lambda err: page_errors.append(str(err)))
                page.on(
                    "request",
                    lambda r: run_or_cancel_posts.append(r.url)
                    if r.method == "POST" and (r.url.endswith("/runs") or r.url.endswith("/cancel"))
                    else None,
                )

                page.goto(url, wait_until="domcontentloaded")
                expect(page.get_by_test_id("brand-logo")).to_be_visible()

                # Drawer collapsed by default
                expect(page.get_by_test_id("chat-inspector")).to_be_hidden()

                # Register project and create session, send 1 turn for readable title
                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id("project-name").fill("chat-layout-proj")
                page.get_by_test_id("register-root").fill(str(proj_dir))
                page.get_by_test_id("confirm-read").check()
                page.get_by_test_id("register-project").click()
                expect(page.get_by_test_id("project-list")).to_contain_text("chat-layout-proj")
                page.get_by_test_id("new-session").click()
                expect(page.get_by_test_id("current-session")).not_to_have_text("-", timeout=10000)
                page.get_by_test_id("nav-chat").click()

                page.get_by_test_id("chat-input").fill("Readable Session Title Here")
                page.get_by_test_id("chat-send").click()
                expect(page.get_by_test_id("chat-turn-1")).to_be_visible(timeout=15000)
                expect(page.get_by_test_id("chat-session-list")).to_contain_text(
                    "Readable Session Title Here", timeout=10000
                )
                assert run_or_cancel_posts == []

                # Keyboard open & Escape close with focus restoration
                toggle_btn = page.get_by_test_id("toggle-inspector")
                toggle_btn.focus()
                page.keyboard.press("Enter")
                expect(page.get_by_test_id("chat-inspector")).to_be_visible()
                page.keyboard.press("Escape")
                expect(page.get_by_test_id("chat-inspector")).to_be_hidden()
                focused_testid = page.evaluate(
                    "() => document.activeElement && document.activeElement.getAttribute('data-testid')"
                )
                assert focused_testid == "toggle-inspector"

                # Desktop horizontal overflow check
                desktop_overflow = page.evaluate(
                    "() => document.documentElement.scrollWidth > window.innerWidth"
                )
                assert desktop_overflow is False

                # Narrow viewport check (no horizontal overflow, input reachable, drawer overlay)
                page.set_viewport_size({"width": 760, "height": 720})
                expect(page.get_by_test_id("chat-input")).to_be_visible()
                narrow_overflow = page.evaluate(
                    "() => document.documentElement.scrollWidth > window.innerWidth"
                )
                assert narrow_overflow is False

                assert console_errors == []
                assert page_errors == []

                # Existing execution entry still requires confirm-scope
                from forecontract_app.chat_runtime import OfflineReadonlyChatProvider

                ro_p = OfflineReadonlyChatProvider()
                srv.app.state.service._chat_provider_factory = lambda **_: (
                    lambda req: (
                        {"action": "execution_offer", "goal": req.question, "summary": "执行任务"}
                        if "Attempt unconfirmed run" in req.question or "Active run for F01" in req.question
                        else ro_p(req)
                    )
                )
                page.get_by_test_id("chat-input").fill("Attempt unconfirmed run")
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-btn-"]').last.click()
                expect(page.get_by_test_id("error")).to_contain_text("SCOPE_NOT_CONFIRMED")

                # Restore desktop viewport and verify F01 per-session drafts during active run
                page.set_viewport_size({"width": 1280, "height": 800})
                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id("new-session").click()
                expect(page.locator('[data-testid^="session-item-"]')).to_have_count(2, timeout=10000)
                sess_items = page.locator('[data-testid^="session-item-"]')
                sa_id = (sess_items.nth(0).get_attribute("data-testid") or "").removeprefix("session-item-")
                sb_id = (sess_items.nth(1).get_attribute("data-testid") or "").removeprefix("session-item-")

                # Start real active run on session A, then test draft isolation & zero POSTs on plain send
                page.get_by_test_id("nav-chat").click()
                page.get_by_test_id(f"chat-session-item-{sa_id}").click()
                expect(page.get_by_test_id("current-session")).to_have_text(sa_id, timeout=10000)
                page.get_by_test_id("chat-input").fill("Active run for F01 draft check")
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                page.locator('[data-testid^="offer-confirm-btn-"]').last.click()
                page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-state")).to_have_text("running", timeout=10000)

                chat_posts_before: list[str] = []
                run_or_cancel_during_draft: list[str] = []

                def _watch_draft(req):
                    if req.method == "POST":
                        if req.url.endswith("/chat/turns"):
                            chat_posts_before.append(req.url)
                        elif req.url.endswith("/runs") or req.url.endswith("/cancel"):
                            run_or_cancel_during_draft.append(req.url)

                page.on("request", _watch_draft)
                page.get_by_test_id("chat-input").fill("Unique draft A")
                page.get_by_test_id("chat-send").click()
                expect(page.get_by_test_id("chat-composer-status")).to_contain_text("草稿已保留")
                assert chat_posts_before == []
                assert run_or_cancel_during_draft == []

                # Switch to session B, type draft B, switch back and forth
                page.get_by_test_id(f"chat-session-item-{sb_id}").click()
                expect(page.get_by_test_id("current-session")).to_have_text(sb_id, timeout=10000)
                expect(page.get_by_test_id("chat-input")).to_have_value("")
                page.get_by_test_id("chat-input").fill("Unique draft B")

                page.get_by_test_id(f"chat-session-item-{sa_id}").click()
                expect(page.get_by_test_id("chat-input")).to_have_value("Unique draft A")
                page.get_by_test_id(f"chat-session-item-{sb_id}").click()
                expect(page.get_by_test_id("chat-input")).to_have_value("Unique draft B")

                # Switch to a project with no active session -> chat-input must be empty
                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id("project-name").fill("empty-proj-f01")
                page.get_by_test_id("create-project").click()
                expect(page.get_by_test_id("project-list")).to_contain_text("empty-proj-f01", timeout=10000)
                page.get_by_test_id("nav-chat").click()
                expect(page.get_by_test_id("current-session")).to_have_text("-")
                expect(page.get_by_test_id("chat-input")).to_have_value("")

                # Cancel the active run on session A cleanly
                page.get_by_test_id("chat-project-select").select_option(label="chat-layout-proj")
                page.get_by_test_id(f"chat-session-item-{sa_id}").click()
                expect(page.get_by_test_id("run-state")).to_have_text("running", timeout=10000)
                page.get_by_test_id("cancel-run").click()
                expect(page.get_by_test_id("run-state")).to_have_text("cancelled", timeout=10000)

                for nav_id, page_id in (
                    ("nav-inbox", "page-inbox"),
                    ("nav-history", "page-history"),
                    ("nav-extensions", "page-extensions"),
                    ("nav-settings", "page-settings"),
                ):
                    page.get_by_test_id(nav_id).click()
                    expect(page.locator(f"#{page_id}")).to_be_visible()

                assert page_errors == []
            finally:
                browser.close()
