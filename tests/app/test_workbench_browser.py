"""Offline Chromium workbench end-to-end acceptance tests for UI-T01 (6 fixed nodes)."""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from app_helpers import LiveTcpTestServer, auth_headers
from extension_helpers import (
    make_extension_host_config,
    make_skill,
)
from forecontract_app.extension_models import ExtensionConfig, StdioMcpServerConfig
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, expect, sync_playwright
from project_helpers import (
    create_buggy_calc_project,
    make_project_host_config,
)


def _launch_chromium(pw: Any) -> Any:
    try:
        return pw.chromium.launch(headless=True)
    except (OSError, PlaywrightError):
        return pw.chromium.launch(headless=True, channel="chromium")


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return proc.stdout.strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "offline@example.invalid")
    _git(repo, "config", "user.name", "Offline Test")


def _goto_and_register(
    page: Page, url: str, proj_dir: Path, *, name: str = "proj-a", confirm_read: bool = True
) -> str:
    page.goto(url, wait_until="domcontentloaded")
    expect(page.get_by_test_id("host-status-banner")).to_be_visible()
    assert "#token=" not in page.url
    page.get_by_test_id("nav-projects").click()
    page.get_by_test_id("project-name").fill(name)
    page.get_by_test_id("register-root").fill(str(proj_dir))
    if confirm_read:
        page.get_by_test_id("confirm-read").check()
    page.get_by_test_id("register-project").click()
    expect(page.get_by_test_id("current-project")).not_to_have_text("-", timeout=10000)
    return page.get_by_test_id("current-project").inner_text().strip()


def test_pages_and_identity(tmp_path: Path) -> None:
    """Register two projects, visit all 6 pages, verify responsive layout, Esc focus, and reload identity."""
    proj_a = tmp_path / "proj-a"
    proj_b = tmp_path / "proj-b"
    create_buggy_calc_project(proj_a)
    create_buggy_calc_project(proj_b)

    cfg = make_project_host_config(tmp_path, allowed_roots=(proj_a, proj_b))
    with LiveTcpTestServer(config=cfg) as srv:
        if getattr(srv.app.state.service, "_chat_provider_factory", None) is None:
            srv.app.state.service._chat_provider_factory = lambda **_: (lambda req: {"action": "execution_offer", "goal": req.question, "summary": "执行任务"})
        url = f"{srv.base_url}/#token={cfg.access_token}"
        non_loopback: list[str] = []
        start_posts: list[str] = []

        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                page.on(
                    "request",
                    lambda req: (
                        non_loopback.append(req.url)
                        if not req.url.startswith(srv.base_url)
                        else None
                    ),
                )
                page.on(
                    "request",
                    lambda req: (
                        start_posts.append(req.url)
                        if req.method == "POST" and (req.url.endswith("/runs") or req.url.endswith("/confirm"))
                        else None
                    ),
                )

                pid_a = _goto_and_register(page, url, proj_a)
                page.get_by_test_id("new-session").click()
                expect(page.get_by_test_id("current-session")).not_to_have_text("-", timeout=10000)
                sid_a = page.get_by_test_id("current-session").inner_text().strip()

                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id("project-name").fill("proj-b")
                page.get_by_test_id("register-root").fill(str(proj_b))
                page.get_by_test_id("confirm-read").check()
                page.get_by_test_id("register-project").click()
                expect(page.get_by_test_id("current-project")).not_to_have_text(pid_a, timeout=10000)
                pid_b = page.get_by_test_id("current-project").inner_text().strip()
                page.get_by_test_id("new-session").click()
                expect(page.get_by_test_id("current-session")).not_to_have_text(sid_a, timeout=10000)

                # Switch back to proj_a and open all 6 pages with zero horizontal overflow
                page.get_by_test_id("nav-projects").click()
                page.locator(f'[data-testid="project-item-{pid_a}"] button').click()
                page.locator(f'[data-testid="session-item-{sid_a}"] button').click()

                for nav_id in (
                    "nav-projects",
                    "nav-inbox",
                    "nav-history",
                    "nav-extensions",
                    "nav-settings",
                    "nav-chat",
                ):
                    page.get_by_test_id(nav_id).focus()
                    page.keyboard.press("Enter")
                    overflow = page.evaluate(
                        "document.documentElement.scrollWidth > window.innerWidth"
                    )
                    assert overflow is False

                # Narrow viewport (390x844): toggle drawer, close with Esc, verify focus restored
                page.set_viewport_size({"width": 390, "height": 844})
                toggle_btn = page.get_by_test_id("navigation-toggle")
                toggle_btn.click()
                expect(toggle_btn).to_have_attribute("aria-expanded", "true")
                page.keyboard.press("Escape")
                expect(toggle_btn).to_have_attribute("aria-expanded", "false")
                assert page.evaluate("document.activeElement?.dataset?.testid") == "navigation-toggle"
                assert (
                    page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
                    is False
                )

                # Reload restores exact identity with no start POSTs and no external requests
                start_posts.clear()
                page.reload(wait_until="domcontentloaded")
                expect(page.get_by_test_id("current-project")).to_have_text(pid_a, timeout=10000)
                expect(page.get_by_test_id("current-session")).to_have_text(sid_a, timeout=10000)
                assert pid_b != pid_a
                assert start_posts == []
                assert non_loopback == []
            finally:
                browser.close()


def test_session_review_loop(tmp_path: Path) -> None:
    """Complete a real project run from goal -> scope -> write -> plan -> trusted command -> JUnit & file view."""
    proj = tmp_path / "calc-proj"
    create_buggy_calc_project(proj)
    before_bytes = (proj / "calc.py").read_bytes()

    cfg = make_project_host_config(tmp_path, allowed_roots=(proj,))
    with LiveTcpTestServer(config=cfg) as srv:
        if getattr(srv.app.state.service, "_chat_provider_factory", None) is None:
            srv.app.state.service._chat_provider_factory = lambda **_: (lambda req: {"action": "execution_offer", "goal": req.question, "summary": "执行任务"})
        url = f"{srv.base_url}/#token={cfg.access_token}"
        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                _goto_and_register(page, url, proj)
                page.get_by_test_id("nav-chat").click()
                page.get_by_test_id("chat-input").fill("修复 calc.py 中的 add_numbers 并运行 pytest")
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                page.locator('[data-testid^="offer-confirm-btn-"]').last.click()

                # 1. Scope approval
                scope_item = page.locator('[data-testid^="proposal-item-"]', has_text="[pending] scope ").first
                expect(scope_item).to_be_visible(timeout=20000)
                assert (proj / "calc.py").read_bytes() == before_bytes
                scope_item.get_by_role("button", name="批准提案").click()

                # 2. Verification plan approval
                plan_item = page.locator('[data-testid^="proposal-item-"]', has_text="[pending] verification ").first
                expect(plan_item).to_be_visible(timeout=20000)
                plan_item.get_by_role("button", name="批准提案").click()

                # 3. Command approval requires trusted_local_execution checkbox
                cmd_item = page.locator('[data-testid^="proposal-item-"]', has_text="[pending] command ").first
                expect(cmd_item).to_be_visible(timeout=20000)
                approve_cmd_btn = cmd_item.get_by_role("button", name="批准提案")
                approve_cmd_btn.click()
                expect(page.get_by_test_id("error")).to_contain_text("trusted_local_execution", timeout=10000)
                expect(cmd_item).to_be_visible()
                cmd_item.locator('input[type="checkbox"]').check()
                approve_cmd_btn.click()

                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-state")).to_have_text("succeeded", timeout=30000)
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("verification")).to_have_text("passed", timeout=10000)
                assert (proj / "calc.py").read_bytes() != before_bytes

                # Bind command stdout and verification revision back to the exact host run record
                import httpx

                sid = page.get_by_test_id("current-session").inner_text().strip()
                rid = page.get_by_test_id("run-id").inner_text().strip()
                run_rec = httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{sid}/runs/{rid}",
                    headers=auth_headers(cfg),
                    timeout=10.0,
                ).json()
                acc = run_rec["result"]["acceptance"]
                cmd_ids = acc.get("command_ids") or []
                assert cmd_ids, f"expected command_ids in acceptance: {acc}"
                cmd_id = cmd_ids[0]
                cmd_out = httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{sid}/runs/{rid}/commands/{cmd_id}",
                    headers=auth_headers(cfg),
                    params={"after": 0},
                    timeout=10.0,
                ).json()
                chunks = cmd_out.get("chunks") or cmd_out.get("items") or []
                stdout_text = "".join(str(ch.get("text", "")) for ch in chunks if ch.get("stream") == "stdout")
                assert stdout_text.strip()
                expected_stdout_line = next(ln.strip() for ln in stdout_text.splitlines() if ln.strip())
                expected_rev = str(acc["revision"])

                # Check run diff, commands, and checks tabs
                page.get_by_test_id("tab-diff").click()
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("diff-detail")).to_contain_text("calc.py")
                page.get_by_test_id("tab-commands").click()
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("command-list")).to_contain_text("passed")
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("command-list")).to_contain_text(expected_stdout_line)
                page.get_by_test_id("tab-checks").click()
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("coverage-summary")).to_contain_text("junit_exact")
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("coverage-summary")).to_contain_text(expected_rev[:12])

                # Open disk file tab and verify current disk text
                page.get_by_test_id("tab-files").click()
                calc_btn = page.get_by_test_id("workspace-files-list").get_by_role("button", name="calc.py", exact=False).first
                expect(calc_btn).to_be_visible(timeout=10000)
                calc_btn.click()
                expect(page.get_by_test_id("workspace-file-preview")).to_contain_text("return a + b")

                # Re-select from history page preserves same run diff
                page.get_by_test_id("nav-history").click()
                view_btn = page.get_by_test_id("session-history").get_by_role("button", name="查看运行").first
                expect(view_btn).to_be_visible(timeout=10000)
                view_btn.click()
                page.get_by_test_id("tab-diff").click()
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("diff-detail")).to_contain_text("calc.py")
            finally:
                browser.close()


def test_project_inbox(tmp_path: Path) -> None:
    """Verify real host inbox aggregation, cross-project isolation, stale 409 re-read, extension decision, and partial failure."""
    import time

    import httpx
    from extension_helpers import (
        CallbackProjectProvider,
        ext_forecast,
        make_extension_host_config,
        write_extension_config_file,
        write_stdio_mcp_script,
    )

    proj_a = tmp_path / "inbox-a"
    proj_b = tmp_path / "inbox-b"
    create_buggy_calc_project(proj_a)
    create_buggy_calc_project(proj_b)
    log_b = tmp_path / "logs" / "inbox_b.jsonl"
    sc_b = write_stdio_mcp_script(tmp_path / "scripts", name="inbox_b.py", log_path=log_b)
    ext_cfg = write_extension_config_file(
        tmp_path / "inbox_ext.json",
        servers=[{"id": "srv-inbox", "transport": "stdio", "executable": sys.executable, "argv": ["-B", str(sc_b)], "cwd": str(tmp_path)}],
    )

    cfg = make_extension_host_config(tmp_path, allowed_roots=(proj_a, proj_b), extension_config_path=ext_cfg)

    def _ext_decide(step: int, req: Any, state: dict[str, Any]) -> str:
        _ = req, state
        if step == 1:
            return ext_forecast(action_type="connect_mcp", payload={"server_id": "srv-inbox"})
        return ext_forecast(action_type="ask_user", payload={"question": "next step?", "options": ["ok"]})

    with LiveTcpTestServer(config=cfg, provider_factory=lambda *, project, run_id: CallbackProjectProvider(_ext_decide)) as srv:
        if getattr(srv.app.state.service, "_chat_provider_factory", None) is None:
            srv.app.state.service._chat_provider_factory = lambda **_: (lambda req: {"action": "execution_offer", "goal": req.question, "summary": "执行任务"})
        url = f"{srv.base_url}/#token={cfg.access_token}"
        hdrs = auth_headers(cfg)
        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                pid_a = _goto_and_register(page, url, proj_a)

                # Start real run in session 1 of proj_a -> pauses at real pending extension proposal
                page.get_by_test_id("nav-chat").click()
                page.get_by_test_id("chat-input").fill("sess1 ext goal")
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                page.locator('[data-testid^="offer-confirm-btn-"]').last.click()
                expect(page.get_by_role("button", name="批准扩展操作").first).to_be_visible(timeout=20000)
                sid1 = page.get_by_test_id("current-session").inner_text().strip()
                rid1 = page.get_by_test_id("run-id").inner_text().strip()

                # Register proj_b and start an independent active run in proj_b
                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id("project-name").fill("inbox-b")
                page.get_by_test_id("register-root").fill(str(proj_b))
                page.get_by_test_id("confirm-read").check()
                page.get_by_test_id("register-project").click()
                expect(page.get_by_test_id("current-project")).not_to_have_text(pid_a, timeout=10000)
                page.get_by_test_id("nav-chat").click()
                page.get_by_test_id("chat-input").fill("proj-b ext goal")
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                page.locator('[data-testid^="offer-confirm-btn-"]').last.click()
                expect(page.get_by_role("button", name="批准扩展操作").first).to_be_visible(timeout=20000)
                sid_b = page.get_by_test_id("current-session").inner_text().strip()
                rid_b = page.get_by_test_id("run-id").inner_text().strip()

                # Switch back to proj_a: inbox shows only proj_a's 1 real pending extension item (ignoring proj_b)
                page.get_by_test_id("nav-projects").click()
                page.locator(f'[data-testid="project-item-{pid_a}"] button').click()
                expect(page.get_by_test_id("current-project")).to_have_text(pid_a, timeout=10000)
                page.get_by_test_id("nav-inbox").click()
                expect(page.get_by_test_id("inbox-badge")).to_have_text("1", timeout=10000)
                expect(page.locator('[data-testid^="inbox-item-"]')).to_have_count(1)

                # Partial load failure with populated cache (error injection only)
                page.route(
                    "**/proposals",
                    lambda route: route.fulfill(status=500, body='{"error":{"code":"ERR","message":"boom"}}'),
                )
                page.get_by_test_id("refresh-inbox").click()
                expect(page.get_by_test_id("inbox-partial-warning")).to_be_visible(timeout=10000)
                expect(page.get_by_test_id("inbox-badge")).to_have_text("1+")
                expect(page.get_by_test_id("inbox-summary")).to_contain_text("计数不完整")
                page.unroute("**/proposals")

                # Empty-cache /api/v1/sessions non-2xx and network abort error injection
                page2 = browser.new_page(viewport={"width": 1440, "height": 900})
                page2.route("**/api/v1/sessions", lambda r: r.fulfill(status=503, body='{"error":{"code":"DOWN","message":"down"}}'))
                page2.goto(url, wait_until="domcontentloaded")
                page2.get_by_test_id("nav-projects").click()
                page2.locator(f'[data-testid="project-item-{pid_a}"] button').click()
                page2.get_by_test_id("nav-inbox").click()
                expect(page2.get_by_test_id("inbox-partial-warning")).to_be_visible(timeout=10000)
                expect(page2.get_by_test_id("inbox-badge")).to_have_text("?")
                page2.unroute("**/api/v1/sessions")
                page2.route("**/api/v1/sessions", lambda r: r.abort())
                page2.get_by_test_id("refresh-inbox").click()
                expect(page2.get_by_test_id("inbox-partial-warning")).to_be_visible(timeout=10000)
                page2.unroute("**/api/v1/sessions")
                page2.get_by_test_id("refresh-inbox").click()
                expect(page2.get_by_test_id("inbox-badge")).to_have_text("1", timeout=10000)
                expect(page2.get_by_test_id("inbox-partial-warning")).to_be_hidden()
                page2.close()

                # Recover exact count on main page
                page.get_by_test_id("refresh-inbox").click()
                expect(page.get_by_test_id("inbox-badge")).to_have_text("1", timeout=10000)
                expect(page.get_by_test_id("inbox-partial-warning")).to_be_hidden()

                # Jump back to active session preserves original session & run identity
                page.locator('[data-testid^="inbox-item-"]').first.get_by_role("button", name="定位到会话").click()
                expect(page.get_by_test_id("current-session")).to_have_text(sid1, timeout=10000)
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-id")).to_have_text(rid1, timeout=10000)

                # Reject real extension proposal from inbox -> double-click sends 1 POST with valid decision enum, 200 OK, host readback
                page.get_by_test_id("nav-inbox").click()
                ext_item = page.locator('[data-testid^="inbox-item-"]').first
                ext_testid = ext_item.get_attribute("data-testid")
                assert ext_testid is not None
                ext_pid = ext_testid.removeprefix("inbox-item-")
                ext_decisions: list[tuple[str, dict[str, Any]]] = []
                ext_statuses: list[int] = []
                page.on(
                    "request",
                    lambda r: ext_decisions.append((r.url, json.loads(r.post_data or "{}")))
                    if r.method == "POST" and r.url.endswith("/decision")
                    else None,
                )
                page.on(
                    "response",
                    lambda r: ext_statuses.append(r.status)
                    if r.request.method == "POST" and r.url.endswith("/decision")
                    else None,
                )
                ext_item.get_by_role("button", name="拒绝").dblclick()
                expect(page.get_by_test_id(ext_testid)).to_be_hidden(timeout=10000)
                assert len(ext_decisions) == 1
                assert f"/sessions/{sid1}/runs/{rid1}/extensions/proposals/{ext_pid}/decision" in ext_decisions[0][0]
                assert ext_decisions[0][1].get("decision") in ("approve", "reject")
                assert ext_statuses == [200]
                ext_host = httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{sid1}/runs/{rid1}/extensions",
                    headers=hdrs,
                    timeout=10.0,
                ).json()
                assert any(p["id"] == ext_pid and p["state"] == "rejected" for p in ext_host["proposals"])

                # Wait for sid1 run to reach terminal state AND release workspace lock before starting sid2 on proj_a
                deadline = time.monotonic() + 15.0
                released = False
                r1_state = "unknown"
                while time.monotonic() < deadline:
                    r1_state = httpx.get(
                        f"{srv.base_url}/api/v1/sessions/{sid1}/runs/{rid1}",
                        headers=hdrs,
                        timeout=10.0,
                    ).json()["state"]
                    if (
                        r1_state in ("succeeded", "failed", "cancelled", "unknown")
                        and pid_a not in srv.app.state.service._active_workspaces
                    ):
                        released = True
                        break
                    if r1_state not in ("succeeded", "failed", "cancelled", "unknown"):
                        c1_resp = httpx.post(
                            f"{srv.base_url}/api/v1/sessions/{sid1}/runs/{rid1}/cancel",
                            headers=hdrs,
                            json={},
                            timeout=10.0,
                        )
                        assert c1_resp.status_code in (200, 202), c1_resp.text
                    time.sleep(0.2)
                assert released, f"pid_a not released: r1_state={r1_state} active={srv.app.state.service._active_workspaces} poisoned={srv.app.state.service._poisoned_workspaces}"

                # Create second session sid2 in proj_a with a real pending extension proposal, then test stale 409 via another client
                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id("new-session").click()
                expect(page.get_by_test_id("current-session")).not_to_have_text(sid1, timeout=10000)
                expect(page.get_by_test_id("current-session")).not_to_have_text("-", timeout=10000)
                sid2 = page.get_by_test_id("current-session").inner_text().strip()
                page.get_by_test_id("nav-chat").click()
                page.get_by_test_id("chat-input").fill("sess2 stale test")
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/confirm")) as run2_info:
                    page.locator('[data-testid^="offer-confirm-btn-"]').last.click()
                run2_resp = run2_info.value
                assert run2_resp.status == 201, f"start-run on sid2 failed: {run2_resp.status} {run2_resp.text()}"
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-id")).not_to_have_text("-", timeout=10000)
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-id")).not_to_have_text(rid1, timeout=10000)
                expect(page.get_by_role("button", name="批准扩展操作").first).to_be_visible(timeout=20000)
                rid2 = page.get_by_test_id("run-id").inner_text().strip()

                page.get_by_test_id("nav-inbox").click()
                stale_item = page.locator('[data-testid^="inbox-item-"]').first
                stale_testid = stale_item.get_attribute("data-testid")
                assert stale_testid is not None
                stale_pid = stale_testid.removeprefix("inbox-item-")

                # Another client decides stale_pid first -> clicking stale item in UI receives host 409, re-reads, and converges
                ext2_resp = httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{sid2}/runs/{rid2}/extensions",
                    headers=hdrs,
                    timeout=10.0,
                )
                assert ext2_resp.status_code == 200, f"sid2={sid2} rid2={rid2} resp={ext2_resp.text}"
                ext2_list = ext2_resp.json()["proposals"]
                stale_digest = next(p["digest"] for p in ext2_list if p["id"] == stale_pid)
                other_resp = httpx.post(
                    f"{srv.base_url}/api/v1/sessions/{sid2}/runs/{rid2}/extensions/proposals/{stale_pid}/decision",
                    headers=hdrs,
                    json={"digest": stale_digest, "decision": "reject"},
                    timeout=10.0,
                )
                assert other_resp.status_code == 200
                stale_statuses: list[int] = []
                stale_gets: list[str] = []
                page.on("response", lambda r: stale_statuses.append(r.status) if r.request.method == "POST" and r.url.endswith("/decision") else None)
                page.on("request", lambda r: stale_gets.append(r.url) if r.method == "GET" else None)
                stale_item.get_by_role("button", name="批准").click()
                expect(page.get_by_test_id(stale_testid)).to_be_hidden(timeout=10000)
                assert 409 in stale_statuses
                assert any("/extensions" in u or "/sessions" in u for u in stale_gets)
                expect(page.get_by_test_id("inbox-badge")).to_be_hidden(timeout=10000)

                # Clean up active runs on host by reading actual run state first; cancel only if still active
                for s_chk, r_chk in ((sid1, rid1), (sid2, rid2), (sid_b, rid_b)):
                    cur_state = httpx.get(
                        f"{srv.base_url}/api/v1/sessions/{s_chk}/runs/{r_chk}",
                        headers=hdrs,
                        timeout=10.0,
                    ).json()["state"]
                    if cur_state not in ("succeeded", "failed", "cancelled", "unknown"):
                        c_end = httpx.post(
                            f"{srv.base_url}/api/v1/sessions/{s_chk}/runs/{r_chk}/cancel",
                            headers=hdrs,
                            json={},
                            timeout=10.0,
                        )
                        assert c_end.status_code in (200, 202), c_end.text
                        dl = time.monotonic() + 10.0
                        while time.monotonic() < dl:
                            st_now = httpx.get(
                                f"{srv.base_url}/api/v1/sessions/{s_chk}/runs/{r_chk}",
                                headers=hdrs,
                                timeout=10.0,
                            ).json()["state"]
                            if st_now in ("succeeded", "failed", "cancelled", "unknown"):
                                break
                            time.sleep(0.2)
                        assert st_now in ("succeeded", "failed", "cancelled", "unknown")
            finally:
                browser.close()


def test_history_and_extensions(tmp_path: Path) -> None:
    """Verify durable host >50 cross-page same-run history aggregation, state/goal filter, GET-only reselect, and MCP 0-spawn."""
    import httpx

    proj_a = tmp_path / "ext-a"
    proj_b = tmp_path / "ext-b"
    create_buggy_calc_project(proj_a)
    create_buggy_calc_project(proj_b)
    make_skill(proj_a, "review-skill", "# Review Skill\nOffline skill.")

    mcp_spawn_log = tmp_path / "mcp_spawn.log"
    mcp_script = tmp_path / "ro_srv.py"
    mcp_script.write_text(
        f"from pathlib import Path\nPath({str(mcp_spawn_log)!r}).write_text('spawned\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    ext_cfg = ExtensionConfig(
        servers=[
            StdioMcpServerConfig(
                id="ro-srv",
                transport="stdio",
                executable=sys.executable,
                argv=["-B", str(mcp_script)],
                cwd=str(tmp_path),
            ),
        ]
    )
    ext_cfg_path = tmp_path / "ext-config.json"
    ext_cfg_path.write_text(ext_cfg.model_dump_json(), encoding="utf-8")
    cfg = make_extension_host_config(
        tmp_path, allowed_roots=(proj_a, proj_b), extension_config_path=ext_cfg_path
    )

    with LiveTcpTestServer(config=cfg) as srv:
        if getattr(srv.app.state.service, "_chat_provider_factory", None) is None:
            srv.app.state.service._chat_provider_factory = lambda **_: (lambda req: {"action": "execution_offer", "goal": req.question, "summary": "执行任务"})
        url = f"{srv.base_url}/#token={cfg.access_token}"
        hdrs = auth_headers(cfg)
        captured_starts: list[dict[str, Any]] = []

        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                page.on(
                    "request",
                    lambda req: (
                        captured_starts.append(json.loads(req.post_data or "{}"))
                        if req.method == "POST" and (req.url.endswith("/runs") or req.url.endswith("/confirm"))
                        else None
                    ),
                )

                pid_a = _goto_and_register(page, url, proj_a)
                page.get_by_test_id("nav-extensions").click()
                expect(page.get_by_test_id("skill-select-review-skill")).to_be_visible(timeout=10000)
                page.get_by_test_id("skill-select-review-skill").check()
                page.get_by_test_id("ext-tab-mcp").click()
                expect(page.get_by_test_id("mcp-server-list")).to_contain_text("ro-srv")
                assert not mcp_spawn_log.exists()

                # Start real run and cancel it -> real durable history in SQLite
                page.get_by_test_id("nav-chat").click()
                page.get_by_test_id("chat-input").fill("run with skill")
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/confirm")) as run2_info:
                    page.locator('[data-testid^="offer-confirm-btn-"]').last.click()
                run2_resp = run2_info.value
                assert run2_resp.status == 201, f"start-run on sid2 failed: {run2_resp.status} {run2_resp.text()}"
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-id")).not_to_have_text("-", timeout=10000)
                sid_hist = page.get_by_test_id("current-session").inner_text().strip()
                active_rid = page.get_by_test_id("run-id").inner_text().strip()
                assert captured_starts and captured_starts[0].get("selected_skill_ids", captured_starts[0].get("skill_ids")) == ["review-skill"]
                page.get_by_test_id("cancel-run").click()
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-state")).to_have_text("cancelled", timeout=15000)

                # Persist >50 durable history items for the SAME active_rid across the 50-item page boundary,
                # plus bare "succeeded" and corrupted JSON negative-control runs.
                import sqlite3

                from forecontract_app.session_store import compute_history_item_sha256

                app_svc = srv.app.state.service
                sess_rec = app_svc._sessions[sid_hist]
                template_item = sess_rec.history[-1]
                next_seq = max(it.seq for it in sess_rec.history) + 1
                bare_rid = str(uuid.uuid4())
                corrupt_rid = str(uuid.uuid4())
                missing_state_rid = str(uuid.uuid4())
                conflict_state_rid = str(uuid.uuid4())
                extra_items: list[Any] = []
                specs: list[tuple[str, str, str]] = [
                    ("answer", active_rid, f"unrelated_answer_needle_xyz_{idx}") for idx in range(52)
                ] + [
                    ("user_goal", bare_rid, "bare text run"),
                    ("run_result", bare_rid, "succeeded"),
                    ("user_goal", corrupt_rid, "corrupt json run"),
                    ("run_result", corrupt_rid, "{not_valid_json"),
                    ("user_goal", missing_state_rid, "alpha missing field goal"),
                    ("run_result", missing_state_rid, json.dumps({"terminal_status": "succeeded"})),
                    ("user_goal", conflict_state_rid, "beta conflict field goal"),
                    ("run_result", conflict_state_rid, json.dumps({"state": "cancelled", "terminal_status": "succeeded"})),
                ]
                for kind_i, rid_i, text_i in specs:
                    item_id = str(uuid.uuid4())
                    sha = compute_history_item_sha256(
                        seq=next_seq,
                        item_id=item_id,
                        project_id=template_item.project_id,
                        session_id=sid_hist,
                        run_id=rid_i,
                        kind=kind_i,
                        text=text_i,
                        source_ids=list(template_item.source_ids),
                        truncated=False,
                    )
                    extra_items.append(
                        template_item.model_copy(
                            update={
                                "seq": next_seq,
                                "item_id": item_id,
                                "kind": kind_i,
                                "run_id": rid_i,
                                "text": text_i,
                                "truncated": False,
                                "sha256": sha,
                            }
                        )
                    )
                    next_seq += 1

                db_conn = sqlite3.connect(str(app_svc._store.db_path), timeout=10.0)
                try:
                    with db_conn:
                        for item in extra_items:
                            db_conn.execute(
                                """
                                INSERT INTO history_items (
                                    session_id, seq, item_id, project_id, run_id,
                                    kind, text, source_ids_json, truncated, sha256
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    item.session_id,
                                    item.seq,
                                    item.item_id,
                                    item.project_id,
                                    item.run_id,
                                    item.kind,
                                    item.text,
                                    json.dumps(list(item.source_ids)),
                                    0,
                                    item.sha256,
                                ),
                            )
                finally:
                    db_conn.close()
                sess_rec.history.extend(extra_items)

                # Verify via host HTTP API that active_rid appears on BOTH page 1 (after=0) and page 2 (after=50)
                h_p1_resp = httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{sid_hist}/history",
                    headers=hdrs,
                    params={"after": "0", "limit": "50"},
                    timeout=10.0,
                )
                assert h_p1_resp.status_code == 200, f"sid_hist={sid_hist} resp={h_p1_resp.text}"
                h_p1 = h_p1_resp.json()
                assert h_p1.get("has_more") is True, f"unexpected h_p1 keys={list(h_p1.keys())} body={h_p1}"
                assert any(it["run_id"] == active_rid for it in h_p1["items"])
                h_p2_resp = httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{sid_hist}/history",
                    headers=hdrs,
                    params={"after": str(h_p1["cursor"]), "limit": "50"},
                    timeout=10.0,
                )
                assert h_p2_resp.status_code == 200, h_p2_resp.text
                h_p2 = h_p2_resp.json()
                assert any(it["run_id"] == active_rid for it in h_p2["items"])

                # Reload page with cleared active_run_id -> loads >50 items across 2 pages from SQLite -> same active_rid aggregated into 1 run group
                page.evaluate("sessionStorage.removeItem('forecontract_active_run_id')")
                page.reload(wait_until="domcontentloaded")
                page.get_by_test_id("nav-history").click()
                expect(page.get_by_test_id(f"history-run-{active_rid}")).to_be_visible(timeout=10000)
                expect(page.locator(f'[data-testid="history-run-{active_rid}"]')).to_have_count(1)
                assert page.locator('[data-testid^="history-item-"]').count() > 50
                expect(page.get_by_test_id("context-summary")).to_contain_text("any_item_truncated=")
                expect(page.get_by_test_id("session-history")).to_contain_text("sources=[")

                # Confirm negative/conflict runs are actually loaded, then verify strict JSON.state filtering
                expect(page.get_by_test_id(f"history-run-{missing_state_rid}")).to_be_visible(timeout=10000)
                expect(page.get_by_test_id(f"history-run-{conflict_state_rid}")).to_be_visible(timeout=10000)
                page.get_by_test_id("history-filter").fill("unrelated_answer_needle_xyz")
                expect(page.get_by_test_id("session-history")).to_contain_text("没有匹配")
                page.get_by_test_id("history-filter").fill("succeeded")
                expect(page.get_by_test_id(f"history-run-{bare_rid}")).to_have_count(0)
                expect(page.get_by_test_id(f"history-run-{corrupt_rid}")).to_have_count(0)
                expect(page.get_by_test_id(f"history-run-{missing_state_rid}")).to_have_count(0)
                expect(page.get_by_test_id(f"history-run-{conflict_state_rid}")).to_have_count(0)

                # Filter by durable terminal state ("cancelled") and by real goal text ("run with skill")
                page.get_by_test_id("history-filter").fill("cancelled")
                expect(page.get_by_test_id(f"history-run-{active_rid}")).to_be_visible()
                expect(page.get_by_test_id(f"history-run-{conflict_state_rid}")).to_be_visible()
                page.get_by_test_id("history-filter").fill("run with skill")
                expect(page.get_by_test_id(f"history-run-{active_rid}")).to_be_visible()

                # Re-select from history -> ALL requests during re-selection must be GET
                reselect_methods: list[str] = []
                page.on("request", lambda r: reselect_methods.append(r.method))
                page.get_by_test_id(f"history-run-{active_rid}").get_by_role("button", name="查看运行").click()
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-id")).to_have_text(active_rid, timeout=10000)
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-state")).to_have_text("cancelled", timeout=10000)
                assert reselect_methods and all(m == "GET" for m in reselect_methods), f"non-GET requests: {reselect_methods}"
                assert not mcp_spawn_log.exists()

                # Switch project clears selected skills
                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id("project-name").fill("proj-b")
                page.get_by_test_id("register-root").fill(str(proj_b))
                page.get_by_test_id("confirm-read").check()
                page.get_by_test_id("register-project").click()
                expect(page.get_by_test_id("current-project")).not_to_have_text(pid_a, timeout=10000)
                page.get_by_test_id("nav-extensions").click()
                expect(page.get_by_test_id("skill-list")).not_to_contain_text("review-skill")
            finally:
                browser.close()


def test_settings_and_usage(tmp_path: Path) -> None:
    """Verify theme/density persistence, constraint revision conflicts, no test quota strings, and no token in prefs."""
    proj = tmp_path / "settings-proj"
    create_buggy_calc_project(proj)
    cfg = make_project_host_config(tmp_path, allowed_roots=(proj,))
    with LiveTcpTestServer(config=cfg) as srv:
        if getattr(srv.app.state.service, "_chat_provider_factory", None) is None:
            srv.app.state.service._chat_provider_factory = lambda **_: (lambda req: {"action": "execution_offer", "goal": req.question, "summary": "执行任务"})
        url = f"{srv.base_url}/#token={cfg.access_token}"
        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                _goto_and_register(page, url, proj)
                page.get_by_test_id("new-session").click()
                expect(page.get_by_test_id("current-session")).not_to_have_text("-", timeout=10000)
                sid = page.get_by_test_id("current-session").inner_text().strip()

                # Verify all 6 pages and all 5 settings sub-sections have no test budget/quota/frame limits or budget inputs
                import httpx

                forbidden_ui_tokens = ("8 次请求", "¥1", "128 MiB", "16 KiB", "上限")
                for nav_tid in ("nav-chat", "nav-projects", "nav-inbox", "nav-history", "nav-extensions", "nav-settings"):
                    page.get_by_test_id(nav_tid).click()
                    body_text = page.locator("body").inner_text()
                    for forbidden in forbidden_ui_tokens:
                        assert forbidden not in body_text, f"forbidden token {forbidden!r} on {nav_tid}"
                for sec_tid in (
                    "settings-sec-appearance",
                    "settings-sec-model",
                    "settings-sec-constraints",
                    "settings-sec-permissions",
                    "settings-sec-storage",
                ):
                    page.get_by_test_id(sec_tid).click()
                    sec_text = page.locator("body").inner_text()
                    for forbidden in forbidden_ui_tokens:
                        assert forbidden not in sec_text, f"forbidden token {forbidden!r} on {sec_tid}"
                form_labels = page.evaluate(
                    "() => Array.from(document.querySelectorAll('input,textarea,select')).map(el => (el.id || '') + ' ' + (el.name || '') + ' ' + (el.placeholder || ''))"
                )
                for fl in form_labels:
                    low = str(fl).lower()
                    assert "budget" not in low and "quota" not in low and "max_requests" not in low

                # Verify real storage_used_bytes matches /sessions/{sid}/context
                ctx_json = httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{sid}/context",
                    headers=auth_headers(cfg),
                    timeout=10.0,
                ).json()
                quota_text = page.get_by_test_id("storage-quota").inner_text()
                assert str(ctx_json["storage_used_bytes"]) in quota_text
                page.get_by_test_id("settings-sec-appearance").click()

                # Appearance persistence across reload + no token in fc-workbench-prefs
                page.get_by_test_id("nav-settings").click()
                page.get_by_test_id("theme-select").select_option("dark")
                page.get_by_test_id("density-select").select_option("compact")
                prefs_raw = page.evaluate("localStorage.getItem('fc-workbench-prefs')")
                assert prefs_raw is not None
                assert cfg.access_token not in prefs_raw

                page.reload(wait_until="domcontentloaded")
                expect(page.locator("html")).to_have_attribute("data-theme", "dark")
                expect(page.locator("html")).to_have_attribute("data-density", "compact")

                # Constraints save and revision conflict preserves user draft
                page.get_by_test_id("nav-settings").click()
                page.get_by_test_id("settings-sec-constraints").click()
                page.get_by_test_id("constraints-input").fill("只修改 src/")
                page.get_by_test_id("save-constraints").click()
                expect(page.get_by_test_id("context-revision")).to_contain_text("rev=1", timeout=10000)

                # External revision bump creates conflict -> draft kept
                import httpx

                put_resp = httpx.put(
                    f"{srv.base_url}/api/v1/sessions/{sid}/context",
                    headers=auth_headers(cfg),
                    json={
                        "constraints": "外部覆盖",
                        "expected_revision": 1,
                        "client_request_id": str(uuid.uuid4()),
                    },
                    timeout=10.0,
                )
                assert put_resp.status_code == 200
                page.get_by_test_id("constraints-input").fill("保留未保存草稿")
                page.get_by_test_id("save-constraints").click()
                expect(page.get_by_test_id("session-notice")).not_to_be_empty(timeout=10000)
                expect(page.get_by_test_id("constraints-input")).to_have_value("保留未保存草稿")
            finally:
                browser.close()


def test_git_review_and_files(tmp_path: Path) -> None:
    """Verify run diff vs HEAD->index vs index->worktree, explicit refresh, and switching to non-repo."""
    repo = tmp_path / "git-proj"
    create_buggy_calc_project(repo)
    _init_repo(repo)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")

    plain = tmp_path / "plain-proj"
    create_buggy_calc_project(plain)

    cfg = make_project_host_config(tmp_path, allowed_roots=(repo, plain))
    with LiveTcpTestServer(config=cfg) as srv:
        if getattr(srv.app.state.service, "_chat_provider_factory", None) is None:
            srv.app.state.service._chat_provider_factory = lambda **_: (lambda req: {"action": "execution_offer", "goal": req.question, "summary": "执行任务"})
        url = f"{srv.base_url}/#token={cfg.access_token}"
        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                _goto_and_register(page, url, repo)
                page.get_by_test_id("nav-chat").click()
                page.get_by_test_id("chat-input").fill("fix calc")
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                page.locator('[data-testid^="offer-confirm-btn-"]').last.click()

                scope_item = page.locator('[data-testid^="proposal-item-"]', has_text="[pending] scope ").first
                expect(scope_item).to_be_visible(timeout=20000)
                scope_item.get_by_role("button", name="批准提案").click()

                plan_item = page.locator('[data-testid^="proposal-item-"]', has_text="[pending] verification ").first
                expect(plan_item).to_be_visible(timeout=20000)
                plan_item.get_by_role("button", name="批准提案").click()

                cmd_item = page.locator('[data-testid^="proposal-item-"]', has_text="[pending] command ").first
                expect(cmd_item).to_be_visible(timeout=20000)
                cmd_item.locator('input[type="checkbox"]').check()
                cmd_item.get_by_role("button", name="批准提案").click()
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-state")).to_have_text("succeeded", timeout=30000)

                # Create distinct staged and worktree edits outside the run
                (repo / "calc.py").write_text(
                    "def add_numbers(a: int, b: int) -> int:\n    # staged_tag\n    return a + b\n",
                    encoding="utf-8",
                )
                _git(repo, "add", "calc.py")
                (repo / "calc.py").write_text(
                    "def add_numbers(a: int, b: int) -> int:\n    # staged_tag\n    # worktree_tag\n    return a + b\n",
                    encoding="utf-8",
                )

                # Create second file so we can verify out-of-order stale response cannot overwrite latest file/diff
                (repo / "other.py").write_text("# other_file_v1\n", encoding="utf-8")
                _git(repo, "add", "other.py")
                _git(repo, "commit", "-m", "add other", "--", "other.py")
                (repo / "other.py").write_text("# other_staged\n", encoding="utf-8")
                _git(repo, "add", "other.py")
                (repo / "other.py").write_text("# other_staged\n# other_worktree\n", encoding="utf-8")

                # Compare 3 tabs: run diff vs git index vs git worktree + tab-files current disk text
                page.get_by_test_id("tab-diff").click()
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("diff-detail")).to_contain_text("return a + b")
                assert "worktree_tag" not in page.get_by_test_id("diff-detail").inner_text()

                page.get_by_test_id("tab-files").click()
                other_btn = page.get_by_test_id("workspace-files-list").get_by_role("button", name="other.py", exact=False).first
                calc_file_btn = page.get_by_test_id("workspace-files-list").get_by_role("button", name="calc.py", exact=False).first
                expect(other_btn).to_be_visible(timeout=10000)

                # Wrap window.fetch with unique per-call IDs and resp.json() finally-microtask settlement (no timers, no URL history matching)
                page.evaluate(
                    """() => {
                        if (window.__fcReadTrackerInstalled) return;
                        window.__fcReadTrackerInstalled = true;
                        window.__readCallSeq = 0;
                        window.__readCalls = {};
                        const orig = window.fetch.bind(window);
                        window.fetch = async (...args) => {
                            const url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
                            const isFile = url.includes('/file?');
                            const isDiff = url.includes('/git/diff?');
                            if (!isFile && !isDiff) {
                                return orig(...args);
                            }
                            const callId = ++window.__readCallSeq;
                            const u = new URL(url, window.location.origin);
                            window.__readCalls[callId] = {
                                id: callId,
                                kind: isFile ? 'file' : 'diff',
                                path: u.searchParams.get('path') || '',
                                area: u.searchParams.get('area') || '',
                                settled: false,
                            };
                            let jsonWrapped = false;
                            try {
                                const resp = await orig(...args);
                                const origJson = resp.json.bind(resp);
                                resp.json = async () => {
                                    jsonWrapped = true;
                                    try {
                                        return await origJson();
                                    } finally {
                                        Promise.resolve().then(() => {
                                            window.__readCalls[callId].settled = true;
                                        });
                                    }
                                };
                                return resp;
                            } finally {
                                if (!jsonWrapped) {
                                    Promise.resolve().then(() => {
                                        window.__readCalls[callId].settled = true;
                                    });
                                }
                            }
                        };
                    }"""
                )

                # Delay calc.py file read -> switch to other.py -> confirm old call still unsettled -> release -> wait for exact old call ID settled -> other.py wins
                delayed_file_routes: list[Any] = []
                page.route("**/file?path=calc.py*", lambda r: delayed_file_routes.append(r))
                with page.expect_request("**/file?path=calc.py*"):
                    calc_file_btn.click()
                assert len(delayed_file_routes) == 1
                old_file_call_id = page.evaluate(
                    "() => { const vals = Object.values(window.__readCalls).filter(c => c.kind === 'file' && c.path === 'calc.py'); return vals[vals.length - 1].id; }"
                )
                other_btn.click()
                expect(page.get_by_test_id("workspace-file-preview")).to_contain_text("# other_worktree", timeout=10000)
                assert page.evaluate("id => window.__readCalls[id].settled", old_file_call_id) is False
                with page.expect_response(lambda r: "/file?path=calc.py" in r.url and r.status == 200):
                    delayed_file_routes[0].continue_()
                page.unroute("**/file?path=calc.py*")
                page.wait_for_function(
                    "id => Boolean(window.__readCalls[id] && window.__readCalls[id].settled)",
                    arg=old_file_call_id,
                    timeout=5000,
                )
                expect(page.get_by_test_id("workspace-file-preview")).to_contain_text("other.py")
                expect(page.get_by_test_id("workspace-file-preview")).to_contain_text("# other_worktree")
                assert "worktree_tag" not in page.get_by_test_id("workspace-file-preview").inner_text()

                # Switch back to calc.py, externally write a brand-new disk marker, explicit refresh shows it
                calc_file_btn.click()
                expect(page.get_by_test_id("workspace-file-preview")).to_contain_text("calc.py", timeout=10000)
                (repo / "calc.py").write_text(
                    "def add_numbers(a: int, b: int) -> int:\n    # staged_tag\n    # worktree_tag\n    # calc_disk_fresh_marker_991\n    return a + b\n",
                    encoding="utf-8",
                )
                page.get_by_test_id("refresh-files").click()
                expect(page.get_by_test_id("workspace-file-preview")).to_contain_text("calc.py", timeout=10000)
                expect(page.get_by_test_id("workspace-file-preview")).to_contain_text("# calc_disk_fresh_marker_991", timeout=10000)

                page.get_by_test_id("tab-git").click()
                expect(page.get_by_test_id("git-meta-bar")).to_contain_text("分支 main", timeout=10000)
                page.get_by_test_id("git-entries-list").get_by_role("button", name="calc.py", exact=False).first.click()
                page.get_by_test_id("git-area-index").click()
                expect(page.get_by_test_id("git-diff-detail")).to_contain_text("staged_tag", timeout=10000)
                assert "worktree_tag" not in page.get_by_test_id("git-diff-detail").inner_text()

                # Delay index diff of calc.py -> switch to worktree -> confirm old call unsettled -> release -> wait for exact old call ID settled -> worktree wins
                delayed_git_routes: list[Any] = []
                page.route("**/git/diff?*area=index*", lambda r: delayed_git_routes.append(r))
                with page.expect_request("**/git/diff?*area=index*"):
                    page.get_by_test_id("git-area-index").click()
                assert len(delayed_git_routes) == 1
                old_git_call_id = page.evaluate(
                    "() => { const vals = Object.values(window.__readCalls).filter(c => c.kind === 'diff' && c.area === 'index'); return vals[vals.length - 1].id; }"
                )
                page.get_by_test_id("git-area-worktree").click()
                expect(page.get_by_test_id("git-diff-detail")).to_contain_text("worktree_tag", timeout=10000)
                assert page.evaluate("id => window.__readCalls[id].settled", old_git_call_id) is False
                with page.expect_response(lambda r: "/git/diff?" in r.url and "area=index" in r.url and r.status == 200):
                    delayed_git_routes[0].continue_()
                page.unroute("**/git/diff?*area=index*")
                page.wait_for_function(
                    "id => Boolean(window.__readCalls[id] && window.__readCalls[id].settled)",
                    arg=old_git_call_id,
                    timeout=5000,
                )
                expect(page.get_by_test_id("git-diff-detail")).to_contain_text("calc.py [worktree]")
                expect(page.get_by_test_id("git-diff-detail")).to_contain_text("worktree_tag")
                expect(page.get_by_test_id("git-diff-area-note")).to_contain_text("工作树")

                # Externally write a brand-new marker to calc.py, explicit refresh-git shows new patch on same path & area while run & index stay distinct
                (repo / "calc.py").write_text(
                    "def add_numbers(a: int, b: int) -> int:\n    # staged_tag\n    # worktree_tag\n    # calc_git_fresh_marker_992\n    return a + b\n",
                    encoding="utf-8",
                )
                _git(repo, "checkout", "-b", "feature-ui")
                page.get_by_test_id("refresh-git").click()
                expect(page.get_by_test_id("git-meta-bar")).to_contain_text("feature-ui", timeout=10000)
                expect(page.get_by_test_id("git-diff-detail")).to_contain_text("calc.py [worktree]", timeout=10000)
                expect(page.get_by_test_id("git-diff-detail")).to_contain_text("# calc_git_fresh_marker_992", timeout=10000)
                page.get_by_test_id("git-area-index").click()
                expect(page.get_by_test_id("git-diff-detail")).to_contain_text("calc.py [index]", timeout=10000)
                expect(page.get_by_test_id("git-diff-detail")).to_contain_text("staged_tag", timeout=10000)
                assert "calc_git_fresh_marker_992" not in page.get_by_test_id("git-diff-detail").inner_text()
                page.get_by_test_id("tab-diff").click()
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("diff-detail")).to_contain_text("return a + b")
                assert "calc_git_fresh_marker_992" not in page.get_by_test_id("diff-detail").inner_text()
                page.get_by_test_id("tab-git").click()

                # Switch to plain non-repo project clears old branch and shows not_repository
                old_pid = page.get_by_test_id("current-project").inner_text().strip()
                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id("project-name").fill("plain-proj")
                page.get_by_test_id("register-root").fill(str(plain))
                page.get_by_test_id("confirm-read").check()
                page.get_by_test_id("register-project").click()
                expect(page.get_by_test_id("current-project")).not_to_have_text(old_pid, timeout=10000)
                page.get_by_test_id("nav-chat").click()
                page.get_by_test_id("tab-git").click()
                expect(page.get_by_test_id("git-meta-bar")).to_contain_text("not_repository", timeout=10000)
                assert "feature-ui" not in page.get_by_test_id("git-meta-bar").inner_text()
            finally:
                browser.close()
