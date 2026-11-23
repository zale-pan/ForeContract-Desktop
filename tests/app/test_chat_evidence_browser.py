"""Browser end-to-end tests for CHAT-T02 evidence chips, refresh recovery, and jump-back."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import project_helpers
from forecontract_app.models import HostConfig
from forecontract_app.session_models import SESSION_DB_FILENAME
from playwright.sync_api import expect, sync_playwright
from test_chat_evidence import seed_evidence_demo
from test_session_browser import LiveTcpTestServer, _launch_chromium


def test_evidence_chips_refresh_and_jump(tmp_path: Path) -> None:
    proj_dir = tmp_path / "ev_browser_proj"
    data_dir = tmp_path / "ev_browser_data"
    seed = seed_evidence_demo(proj_dir, data_dir)
    calc_sha_before = project_helpers.sha256_file(proj_dir / "calc.py")

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
                expect(page.get_by_test_id("current-session")).to_have_text(
                    seed["session_id"], timeout=10000
                )

                # Select Run #1 on History page, then return to Chat and open inspector
                page.get_by_test_id("nav-history").click()
                page.locator("#run-history-select").select_option(seed["run1_id"])
                expect(page.get_by_test_id("run-id")).to_have_text(seed["run1_id"], timeout=10000)
                page.get_by_test_id("nav-chat").click()
                page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("chat-inspector")).to_be_visible()

                # Quote Run #1 diff into chat, then also quote Run #1 command and remove command chip
                page.get_by_test_id("quote-diff-in-chat").click()
                expect(page.get_by_test_id("chat-ref-chip-0")).to_be_visible()
                page.get_by_test_id("tab-commands").click()
                page.get_by_test_id(f"quote-cmd-{seed['run1_command_id']}").click()
                expect(page.get_by_test_id("chat-ref-chip-1")).to_be_visible()
                page.get_by_test_id("chat-ref-remove-1").click()
                expect(page.get_by_test_id("chat-ref-chip-1")).to_have_count(0)

                # Send chat turn with diff_hunk reference
                page.get_by_test_id("chat-input").fill("Explain Run1 diff bug")
                page.get_by_test_id("chat-send").click()
                turn1 = page.get_by_test_id("chat-turn-1")
                expect(turn1).to_be_visible(timeout=15000)
                expect(turn1).to_contain_text(seed["diff_marker"])
                expect(page.get_by_test_id("chat-turn-ev-1-0")).to_be_visible()

                # Switch to Run #2 after reload, then click evidence chip to jump back to Run #1
                page.get_by_test_id("close-inspector").click()
                expect(page.get_by_test_id("chat-inspector")).to_be_hidden()

                page.reload(wait_until="domcontentloaded")
                expect(page.get_by_test_id("chat-turn-ev-1-0")).to_be_visible(timeout=10000)
                page.get_by_test_id("nav-history").click()
                page.locator("#run-history-select").select_option(seed["run2_id"])
                expect(page.get_by_test_id("run-id")).to_have_text(seed["run2_id"], timeout=10000)
                page.get_by_test_id("nav-chat").click()

                page.get_by_test_id("chat-turn-ev-1-0").click()
                expect(page.get_by_test_id("chat-inspector")).to_be_visible()
                expect(page.get_by_test_id("run-id")).to_have_text(seed["run1_id"], timeout=10000)
                expect(page.get_by_test_id("diff-detail")).to_be_visible()
                expect(page.get_by_test_id("diff-detail")).to_contain_text(seed["diff_marker"])

                # Quote Run #1 command (raw output unavailable after reopen) -> shows "原始输出不可用"
                page.get_by_test_id("tab-commands").click()
                page.get_by_test_id(f"quote-cmd-{seed['run1_command_id']}").click()
                page.get_by_test_id("chat-input").fill("Explain Run1 command failure")
                page.get_by_test_id("chat-send").click()
                turn2 = page.get_by_test_id("chat-turn-2")
                expect(turn2).to_be_visible(timeout=15000)
                expect(turn2).to_contain_text("原始输出不可用")

                assert run_posts == []
                assert project_helpers.sha256_file(proj_dir / "calc.py") == calc_sha_before
                unexpected_errors = [
                    e
                    for e in console_errors
                    if not ("/commands/" in e["url"] and "404" in e["text"])
                ]
                assert unexpected_errors == []
                assert page_errors == []
            finally:
                browser.close()

    conn = sqlite3.connect(str(data_dir / SESSION_DB_FILENAME))
    try:
        run_count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        turn_count = conn.execute("SELECT COUNT(*) FROM chat_turns").fetchone()[0]
        assert run_count == 2
        assert turn_count == 2
    finally:
        conn.close()
