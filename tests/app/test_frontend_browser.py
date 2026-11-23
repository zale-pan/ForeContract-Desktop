"""U13–U20: Real Chromium end-to-end Playwright tests against real LiveTcpTestServer."""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import project_helpers
from app_helpers import LiveTcpTestServer, auth_headers, make_host_config, tree_hashes
from forecontract_app.models import ProjectView
from forecontract_app.project_demo import OfflineProjectProvider
from forecontract_app.project_models import PROJECT_SCHEMA_VERSION
from forecontract_llm import ModelRequest
from playwright.sync_api import expect, sync_playwright
from session_helpers import SpyUsageProvider, forecast_json

_STATIC_DIR = (
    Path(__file__).resolve().parents[2] / "apps" / "local" / "forecontract_app" / "static"
)
_README_PATH = Path(__file__).resolve().parents[2] / "apps" / "local" / "README.zh-CN.md"


def _attach_watchers(
    page: Any, *, base_url: str, allow_error_substrings: tuple[str, ...] = ()
) -> tuple[list[str], list[str], list[str], list[dict[str, Any]]]:
    page_errors: list[str] = []
    console_errors: list[str] = []
    non_loopback: list[str] = []
    requests: list[dict[str, Any]] = []

    page.on("pageerror", lambda err: page_errors.append(str(err)))

    def _on_console(msg: Any) -> None:
        if msg.type == "error":
            text = msg.text
            if not any(tok in text for tok in allow_error_substrings):
                console_errors.append(text)

    page.on("console", _on_console)

    def _on_req(req: Any) -> None:
        if not req.url.startswith(f"{base_url}/"):
            non_loopback.append(req.url)
        requests.append(
            {
                "method": req.method,
                "url": req.url,
                "headers": dict(req.headers),
            }
        )

    page.on("request", _on_req)
    return page_errors, console_errors, non_loopback, requests


def _approve_all_pending(page: Any) -> None:
    import time

    deadline = time.monotonic() + 25.0
    clicked: set[str] = set()
    while time.monotonic() < deadline:
        st = page.get_by_test_id("run-state").text_content()
        if st in ("succeeded", "failed", "cancelled", "unknown"):
            return
        pending = page.locator('[data-testid^="proposal-item-"]', has_text="[pending] ")
        for i in range(pending.count()):
            item = pending.nth(i)
            btn = item.locator('[data-testid^="proposal-approve-"]')
            tid = btn.get_attribute("data-testid")
            if not tid or tid in clicked:
                continue
            clicked.add(tid)
            if "[pending] command " in (item.text_content() or ""):
                trust = item.locator('[data-testid^="proposal-trust-"]')
                if trust.count() > 0 and not trust.first.is_checked():
                    trust.first.check()
            btn.click()
        time.sleep(0.05)
    raise AssertionError("timed out approving pending proposals")


def test_u13_responsive_layout(tmp_path: Path) -> None:
    proj_dir = project_helpers.create_buggy_calc_project(tmp_path / "u13-proj")
    cfg = project_helpers.make_project_host_config(tmp_path, allowed_roots=(proj_dir,))
    scratch_root = Path(os.environ.get("FORECONTRACT_TEST_SCRATCH", str(tmp_path)))
    shot_dir = scratch_root / "u13-shots"
    shot_dir.mkdir(parents=True, exist_ok=True)

    with LiveTcpTestServer(config=cfg) as srv, sync_playwright() as pw:
        if getattr(srv.app.state.service, "_chat_provider_factory", None) is None:
            srv.app.state.service._chat_provider_factory = lambda **_: (lambda req: {"action": "execution_offer", "goal": req.question, "summary": "执行任务"})
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page_errors, console_errors, non_loopback, _ = _attach_watchers(page, base_url=srv.base_url)
            page.goto(f"{srv.base_url}/#token={cfg.access_token}", wait_until="domcontentloaded")

            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id("project-name").fill("u13-proj")
            page.get_by_test_id("register-root").fill(str(proj_dir))
            page.get_by_test_id("confirm-read").check()
            page.get_by_test_id("register-project").click()
            expect(page.get_by_test_id("project-list")).to_contain_text("u13-proj")
            page.get_by_test_id("nav-chat").click()

            page.get_by_test_id("chat-input").fill("Fix calc bug and verify diff formatting with long path coverage")
            page.get_by_test_id("chat-send").click()
            page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
            page.locator('[data-testid^="offer-confirm-btn-"]').last.click()
            _approve_all_pending(page)
            page.get_by_test_id("toggle-inspector").click()
            expect(page.get_by_test_id("run-state")).to_have_text("succeeded", timeout=20000)

            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
            nav_box = page.get_by_test_id("workspace-nav").bounding_box()
            run_box = page.locator("#page-chat .chat-thread").bounding_box()
            assert nav_box is not None and run_box is not None
            assert nav_box["x"] + nav_box["width"] <= run_box["x"] + 2
            page.screenshot(path=str(shot_dir / "u13-desktop-1440.png"))
            page.get_by_test_id("close-inspector").click()

            # Narrow screen 390x844: navigation-toggle switches visibility & zero horizontal overflow
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
            expect(page.get_by_test_id("navigation-toggle")).to_be_visible()
            expect(page.get_by_test_id("workspace-nav")).to_be_hidden()
            page.screenshot(path=str(shot_dir / "u13-mobile-390.png"))

            page.get_by_test_id("navigation-toggle").click()
            expect(page.get_by_test_id("workspace-nav")).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
            page.keyboard.press("Escape")
            expect(page.get_by_test_id("workspace-nav")).to_be_hidden()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")

            assert page_errors == []
            assert console_errors == []
            assert non_loopback == []
        finally:
            browser.close()


def _tab_until_testid(page: Any, target_testid: str, *, max_tabs: int = 80) -> None:
    seen_steps = 0
    for _ in range(max_tabs):
        page.keyboard.press("Tab")
        seen_steps += 1
        info = page.evaluate(
            """() => {
                const a = document.activeElement;
                if (!a) return { tid: null, visible: false, outlineStyle: 'none', outlineWidth: '0px', outlineColor: 'rgba(0, 0, 0, 0)' };
                const cs = getComputedStyle(a);
                const rect = a.getBoundingClientRect();
                const ownTid = a.getAttribute('data-testid');
                const navLi = ownTid ? null : a.closest('li[data-testid^="project-item-"], li[data-testid^="session-item-"]');
                return {
                    tid: ownTid || (navLi ? navLi.getAttribute('data-testid') : null),
                    tag: a.tagName,
                    visible: rect.width > 0 && rect.height > 0 && cs.visibility !== 'hidden' && cs.display !== 'none',
                    outlineStyle: cs.outlineStyle,
                    outlineWidth: cs.outlineWidth,
                    outlineColor: cs.outlineColor,
                };
            }"""
        )
        if info["tid"] == target_testid:
            assert info["visible"], f"focused element {target_testid!r} not visible: {info}"
            assert info["outlineStyle"] not in {"none", "hidden"}, f"missing focus outline on {target_testid!r}: {info}"
            assert info["outlineWidth"].endswith("px") and float(info["outlineWidth"].removesuffix("px")) > 0, (
                f"zero outlineWidth on {target_testid!r}: {info}"
            )
            assert info["outlineColor"] not in {"transparent", "rgba(0, 0, 0, 0)"}, (
                f"transparent outlineColor on {target_testid!r}: {info}"
            )
            return
    raise AssertionError(f"failed to reach data-testid={target_testid!r} within {seen_steps} Tabs")


def test_u14_keyboard_navigation(tmp_path: Path) -> None:
    p1_dir = project_helpers.create_buggy_calc_project(tmp_path / "u14-p1")
    proj_dir = project_helpers.create_buggy_calc_project(tmp_path / "u14-kbd")
    cfg = project_helpers.make_project_host_config(tmp_path, allowed_roots=(p1_dir, proj_dir))
    with LiveTcpTestServer(config=cfg) as srv, sync_playwright() as pw:
        if getattr(srv.app.state.service, "_chat_provider_factory", None) is None:
            srv.app.state.service._chat_provider_factory = lambda **_: (lambda req: {"action": "execution_offer", "goal": req.question, "summary": "执行任务"})
        # Pre-seed at least 2 existing projects & 2 same-project sessions on p2
        p1_resp = httpx.post(
            f"{srv.base_url}/api/v1/projects/register",
            headers=auth_headers(cfg),
            json={"name": "u14-p1", "root": str(p1_dir), "confirmed_read": True},
            timeout=5.0,
        )
        assert p1_resp.status_code == 201
        p2_resp = httpx.post(
            f"{srv.base_url}/api/v1/projects/register",
            headers=auth_headers(cfg),
            json={"name": "u14-kbd", "root": str(proj_dir), "confirmed_read": True},
            timeout=5.0,
        )
        assert p2_resp.status_code == 201
        p2_id = p2_resp.json()["project_id"]

        s2a_resp = httpx.post(
            f"{srv.base_url}/api/v1/sessions",
            headers=auth_headers(cfg),
            json={"project_id": p2_id, "model": {"provider": "offline", "model": "offline-project-v1", "thinking": "disabled"}},
            timeout=5.0,
        )
        assert s2a_resp.status_code == 201
        s2b_resp = httpx.post(
            f"{srv.base_url}/api/v1/sessions",
            headers=auth_headers(cfg),
            json={"project_id": p2_id, "model": {"provider": "offline", "model": "offline-project-v1", "thinking": "disabled"}},
            timeout=5.0,
        )
        assert s2b_resp.status_code == 201
        s2b_id = s2b_resp.json()["session_id"]

        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page_errors, console_errors, non_loopback, _ = _attach_watchers(page, base_url=srv.base_url)
            page.goto(f"{srv.base_url}/#token={cfg.access_token}", wait_until="domcontentloaded")
            _tab_until_testid(page, "nav-projects", max_tabs=40)
            page.keyboard.press("Enter")
            expect(page.get_by_test_id("project-list")).to_contain_text("u14-kbd")

            # Verify every input/select/textarea has an accessible label or aria-label
            unlabelled = page.evaluate(
                """() => Array.from(document.querySelectorAll('input, select, textarea')).filter(el => {
                    const id = el.getAttribute('id');
                    const hasFor = id && document.querySelector(`label[for="${id}"]`);
                    const wrapped = el.closest('label');
                    const aria = el.getAttribute('aria-label');
                    return !hasFor && !wrapped && !aria;
                }).map(el => el.outerHTML)"""
            )
            assert unlabelled == []

            # Pure keyboard selection of second project and second same-project session via Tab + Enter
            _tab_until_testid(page, f"project-item-{p2_id}", max_tabs=120)
            page.keyboard.press("Enter")
            expect(page.get_by_test_id("current-project")).to_have_text(p2_id)
            assert page.evaluate("sessionStorage.getItem('forecontract_active_project_id')") == p2_id

            _tab_until_testid(page, f"session-item-{s2b_id}", max_tabs=120)
            page.keyboard.press("Enter")
            expect(page.get_by_test_id("current-session")).to_have_text(s2b_id)
            assert page.evaluate("sessionStorage.getItem('forecontract_active_session_id')") == s2b_id

            # Pure keyboard flow: chat-input -> Enter -> offer-confirm-scope -> offer-confirm-btn
            _tab_until_testid(page, "chat-input", max_tabs=120)
            page.keyboard.type("Keyboard fix calc and diff")
            page.keyboard.press("Enter")
            expect(page.locator('[data-testid^="offer-confirm-scope-"]').last).to_be_visible(timeout=15000)
            offer_scope_tid = page.locator('[data-testid^="offer-confirm-scope-"]').last.get_attribute("data-testid") or ""
            offer_btn_tid = page.locator('[data-testid^="offer-confirm-btn-"]').last.get_attribute("data-testid") or ""
            _tab_until_testid(page, offer_scope_tid, max_tabs=120)
            page.keyboard.press("Space")
            assert page.get_by_test_id(offer_scope_tid).is_checked()
            _tab_until_testid(page, offer_btn_tid, max_tabs=120)
            page.keyboard.press("Enter")

            # Approve pending proposals via keyboard Tab + Space/Enter until succeeded
            import time

            deadline = time.monotonic() + 25.0
            handled_tids: set[str] = set()
            while time.monotonic() < deadline:
                if page.get_by_test_id("run-state").text_content() in ("succeeded", "failed", "cancelled", "unknown"):
                    break
                pending_items = page.evaluate(
                    """() => Array.from(document.querySelectorAll('[data-testid^="proposal-item-"]'))
                        .filter(li => (li.textContent || '').includes('[pending] '))
                        .map(li => ({
                            isCmd: (li.textContent || '').includes('[pending] command '),
                            approveTid: li.querySelector('[data-testid^="proposal-approve-"]')?.getAttribute('data-testid') || null,
                            trustTid: li.querySelector('[data-testid^="proposal-trust-"]')?.getAttribute('data-testid') || null,
                        }))"""
                )
                acted = False
                for entry in pending_items:
                    btn_tid = entry.get("approveTid")
                    if not btn_tid or btn_tid in handled_tids:
                        continue
                    handled_tids.add(btn_tid)
                    if entry.get("isCmd") and entry.get("trustTid"):
                        _tab_until_testid(page, entry["trustTid"], max_tabs=120)
                        page.keyboard.press("Space")
                    _tab_until_testid(page, btn_tid, max_tabs=120)
                    page.keyboard.press("Enter")
                    acted = True
                    break
                if not acted:
                    time.sleep(0.05)

            expect(page.get_by_test_id("run-state")).to_have_text("succeeded", timeout=20000)

            # Verify Tab reaches enabled diff-file-select and history page run-history-select cleanly without trap
            _tab_until_testid(page, "toggle-inspector", max_tabs=120)
            page.keyboard.press("Enter")
            _tab_until_testid(page, "diff-file-select", max_tabs=120)
            assert page.evaluate("document.activeElement.getAttribute('data-testid')") == "diff-file-select"
            _tab_until_testid(page, "nav-history", max_tabs=120)
            page.keyboard.press("Enter")
            _tab_until_testid(page, "run-history-select", max_tabs=120)
            assert page.evaluate("document.activeElement.getAttribute('data-testid')") == "run-history-select"

            assert page_errors == []
            assert console_errors == []
            assert non_loopback == []
        finally:
            browser.close()


def test_u15_drafts_and_revision_conflict(tmp_path: Path) -> None:
    cfg = make_host_config(tmp_path)
    with LiveTcpTestServer(config=cfg) as srv, sync_playwright() as pw:
        if getattr(srv.app.state.service, "_chat_provider_factory", None) is None:
            srv.app.state.service._chat_provider_factory = lambda **_: (lambda req: {"action": "execution_offer", "goal": req.question, "summary": "执行任务"})
        proj = httpx.post(
            f"{srv.base_url}/api/v1/projects",
            headers=auth_headers(cfg),
            json={"name": "draft-proj"},
            timeout=5.0,
        ).json()
        s1 = httpx.post(
            f"{srv.base_url}/api/v1/sessions",
            headers=auth_headers(cfg),
            json={"project_id": proj["project_id"], "model": {"provider": "offline", "model": "offline-metrics-v1", "thinking": "disabled"}},
            timeout=5.0,
        ).json()
        s2 = httpx.post(
            f"{srv.base_url}/api/v1/sessions",
            headers=auth_headers(cfg),
            json={"project_id": proj["project_id"], "model": {"provider": "offline", "model": "offline-metrics-v1", "thinking": "disabled"}},
            timeout=5.0,
        ).json()

        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page_errors, _, non_loopback, reqs = _attach_watchers(
                page, base_url=srv.base_url, allow_error_substrings=("409",)
            )
            page.goto(f"{srv.base_url}/#token={cfg.access_token}", wait_until="domcontentloaded")

            page.get_by_test_id("nav-settings").click()
            page.get_by_test_id("settings-sec-constraints").click()
            expect(page.get_by_test_id("draft-status")).to_contain_text("草稿未保存，刷新会丢失")

            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id(f"session-item-{s1['session_id']}").click()
            page.get_by_test_id("chat-input").fill("goal draft S1")
            page.get_by_test_id("nav-settings").click()
            page.get_by_test_id("settings-sec-constraints").click()
            page.get_by_test_id("constraints-input").fill("constraint draft S1")
            expect(page.get_by_test_id("draft-status")).to_contain_text("未保存草稿")

            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id(f"session-item-{s2['session_id']}").click()
            expect(page.get_by_test_id("chat-input")).to_have_value("")
            page.get_by_test_id("chat-input").fill("goal draft S2")
            page.get_by_test_id("nav-settings").click()
            page.get_by_test_id("settings-sec-constraints").click()
            page.get_by_test_id("constraints-input").fill("constraint draft S2")

            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id(f"session-item-{s1['session_id']}").click()
            expect(page.get_by_test_id("chat-input")).to_have_value("goal draft S1")
            page.get_by_test_id("nav-settings").click()
            page.get_by_test_id("settings-sec-constraints").click()
            expect(page.get_by_test_id("constraints-input")).to_have_value("constraint draft S1")

            # Advance revision out-of-band on backend so UI's expected_revision=0 returns 409 CONFLICT
            bump = httpx.put(
                f"{srv.base_url}/api/v1/sessions/{s1['session_id']}/context",
                headers=auth_headers(cfg),
                json={"client_request_id": str(uuid.uuid4()), "expected_revision": 0, "constraints": "SERVER_REV1"},
                timeout=5.0,
            )
            assert bump.status_code == 200

            puts_before = len([r for r in reqs if r["method"] == "PUT"])
            page.get_by_test_id("save-constraints").click()
            expect(page.get_by_test_id("session-notice")).to_contain_text("REVISION_CONFLICT")
            page.wait_for_timeout(120)
            assert len([r for r in reqs if r["method"] == "PUT"]) == puts_before + 1
            expect(page.get_by_test_id("constraints-input")).to_have_value("constraint draft S1")

            # Sync latest revision by clicking s2 then s1 -> keeps dirty draft until reload clears draft -> valid save clears constraints dirty (goal stays dirty)
            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id(f"session-item-{s2['session_id']}").click()
            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id(f"session-item-{s1['session_id']}").click()
            page.get_by_test_id("nav-settings").click()
            page.get_by_test_id("settings-sec-constraints").click()
            expect(page.get_by_test_id("context-revision")).to_contain_text("rev=1")
            expect(page.get_by_test_id("constraints-input")).to_have_value("constraint draft S1")
            page.get_by_test_id("save-constraints").click()
            expect(page.get_by_test_id("context-revision")).to_contain_text("rev=2")
            expect(page.get_by_test_id("draft-status")).to_contain_text("未保存草稿（输入）")

            # On a clean third session where only constraints is edited, saving clears dirty -> "草稿已同步"
            s3 = httpx.post(
                f"{srv.base_url}/api/v1/sessions",
                headers=auth_headers(cfg),
                json={"project_id": proj["project_id"], "model": {"provider": "offline", "model": "offline-metrics-v1", "thinking": "disabled"}},
                timeout=5.0,
            ).json()
            page.reload(wait_until="domcontentloaded")
            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id(f"session-item-{s3['session_id']}").click()
            page.get_by_test_id("nav-settings").click()
            page.get_by_test_id("settings-sec-constraints").click()
            page.get_by_test_id("constraints-input").fill("clean constraints S3")
            expect(page.get_by_test_id("draft-status")).to_contain_text("未保存草稿（固定约束）")
            page.get_by_test_id("save-constraints").click()
            expect(page.get_by_test_id("context-revision")).to_contain_text("rev=1")
            expect(page.get_by_test_id("draft-status")).to_contain_text("草稿已同步")
            assert page_errors == []
            assert non_loopback == []
        finally:
            browser.close()


def test_u16_offline_model_and_scope_labels(tmp_path: Path) -> None:
    proj_dir = project_helpers.create_buggy_calc_project(tmp_path / "u16-existing")
    cfg = project_helpers.make_project_host_config(tmp_path, allowed_roots=(proj_dir,))

    with LiveTcpTestServer(config=cfg) as srv, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page_errors, _, non_loopback, _ = _attach_watchers(
                page, base_url=srv.base_url, allow_error_substrings=("409",)
            )
            page.goto(f"{srv.base_url}/#token={cfg.access_token}", wait_until="domcontentloaded")

            # Verify zero password/API-key inputs and zero offline model dropdown options in formal UI
            key_inputs = page.evaluate(
                "() => Array.from(document.querySelectorAll('input')).filter(i => i.type === 'password' || (i.placeholder + i.id).toLowerCase().includes('api_key')).length"
            )
            assert key_inputs == 0
            assert page.get_by_test_id("model").count() == 0
            assert page.get_by_test_id("toggle-composer-config").count() == 0

            # Settings shows host-configured model status (not offline stub options)
            page.get_by_test_id("nav-settings").click()
            expect(page.get_by_test_id("settings-overview-model-val")).to_have_text("未配置在线模型")

            # Register existing project -> create session -> sending in unconfigured host returns LIVE_DISABLED & 0 external requests
            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id("project-name").fill("u16-exist")
            page.get_by_test_id("register-root").fill(str(proj_dir))
            page.get_by_test_id("confirm-read").check()
            page.get_by_test_id("register-project").click()
            expect(page.get_by_test_id("project-list")).to_contain_text("u16-exist")
            page.get_by_test_id("new-session").click()
            expect(page.get_by_test_id("current-session")).not_to_have_text("-", timeout=10000)
            page.get_by_test_id("nav-chat").click()
            page.get_by_test_id("chat-input").fill("Attempt candidate run")
            page.get_by_test_id("chat-send").click()
            expect(page.get_by_test_id("error")).to_contain_text("LIVE_DISABLED")
            assert page_errors == []
            assert non_loopback == []
        finally:
            browser.close()


def test_u17_quota_and_retention_readonly(tmp_path: Path) -> None:
    cfg = make_host_config(tmp_path)
    with LiveTcpTestServer(config=cfg) as srv, sync_playwright() as pw:
        if getattr(srv.app.state.service, "_chat_provider_factory", None) is None:
            srv.app.state.service._chat_provider_factory = lambda **_: (lambda req: {"action": "execution_offer", "goal": req.question, "summary": "执行任务"})
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page_errors, console_errors, non_loopback, reqs = _attach_watchers(page, base_url=srv.base_url)
            page.goto(f"{srv.base_url}/#token={cfg.access_token}", wait_until="domcontentloaded")

            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id("project-name").fill("u17-quota")
            page.get_by_test_id("create-project").click()
            expect(page.get_by_test_id("project-list")).to_contain_text("u17-quota")
            page.get_by_test_id("nav-chat").click()
            page.get_by_test_id("chat-input").fill("Check usage and quota boundaries")
            page.get_by_test_id("chat-send").click()
            page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
            page.locator('[data-testid^="offer-confirm-btn-"]').last.click()
            page.get_by_test_id("toggle-inspector").click()
            expect(page.get_by_test_id("run-state")).to_have_text("succeeded", timeout=20000)

            expect(page.get_by_test_id("usage-summary")).to_contain_text("offline_not_billed")
            sid = page.get_by_test_id("current-session").inner_text().strip()
            ctx_json = httpx.get(
                f"{srv.base_url}/api/v1/sessions/{sid}/context",
                headers=auth_headers(cfg),
                timeout=5.0,
            ).json()
            page.get_by_test_id("nav-settings").click()
            page.get_by_test_id("settings-sec-storage").click()
            expect(page.get_by_test_id("storage-quota")).to_contain_text("sessions.sqlite3")
            expect(page.get_by_test_id("storage-quota")).to_contain_text(
                str(ctx_json["storage_used_bytes"])
            )

            ws_snapshot_before = tree_hashes(cfg.workspace_parent)
            page.reload(wait_until="domcontentloaded")
            page.get_by_test_id("nav-settings").click()
            page.get_by_test_id("settings-sec-storage").click()
            expect(page.get_by_test_id("storage-quota")).to_contain_text("sessions.sqlite3")
            assert tree_hashes(cfg.workspace_parent) == ws_snapshot_before
            assert not [r for r in reqs if r["method"] == "DELETE"]

            forbidden = ("delete", "purge", "vacuum", "删除", "清空", "清理")
            btns = page.evaluate(
                "() => Array.from(document.querySelectorAll('button')).map(b => (b.textContent || '') + ' ' + (b.getAttribute('data-testid') || ''))"
            )
            for lbl in btns:
                for tok in forbidden:
                    assert tok not in lbl.lower(), f"forbidden delete control: {lbl}"

            readme_text = _README_PATH.read_text(encoding="utf-8")
            assert "用户项目/会话持久数据与开发/验收临时证据严格分离" in readme_text
            assert page_errors == []
            assert console_errors == []
            assert non_loopback == []
        finally:
            browser.close()


def test_u18_safe_render_and_network_boundary(tmp_path: Path) -> None:
    proj_dir = project_helpers.create_buggy_calc_project(tmp_path / "u18-xss")
    xss_goal = '<img src=x onerror="window.__xss=1">'
    xss_constraint = '<script>window.__xss=2</script>'
    xss_answer = '<a href="javascript:window.__xss=3">click</a>'
    diff_canary = '<img src=y onerror="window.__xss=4">'
    command_canary = '<img src=z onerror="window.__xss=5">'
    xss_question = '<svg onload="window.__xss=6">'
    # Put diff_canary directly in calc.py adjacent to `return a - b` so real backend WorkspaceDiff emits it
    (proj_dir / "calc.py").write_text(
        '"""Simple calculator module."""\n\n\n'
        "def add_numbers(a: int, b: int) -> int:\n"
        f"    # {diff_canary}\n"
        "    return a - b\n",
        encoding="utf-8",
    )
    cfg = project_helpers.make_project_host_config(tmp_path, allowed_roots=(proj_dir,))

    def _factory(*, project: ProjectView, run_id: str) -> SpyUsageProvider:
        _ = (project, run_id)
        inner = OfflineProjectProvider()
        cmd_id = "cmd-u18-xss"
        cmd_stage = "idle"

        def _build(idx: int, req: ModelRequest) -> str:
            nonlocal cmd_stage
            state = req.observation.state
            last = state.get("last_tool_result") or {}
            if idx == 0:
                return forecast_json(
                    action_type="ask_user",
                    payload={"question": f"Confirm {xss_question}?", "options": ["ok"]},
                )
            if cmd_stage == "idle" and last.get("action_type") == "propose_verification":
                inner._ingest_last_result(state)
                cmd_stage = "proposed"
                return json.dumps(
                    {
                        "schema_version": PROJECT_SCHEMA_VERSION,
                        "intent": "propose local command emitting XSS canary literal",
                        "decision_summary": "propose_command canary",
                        "proposed_action": {
                            "schema_version": PROJECT_SCHEMA_VERSION,
                            "action_type": "propose_command",
                            "payload": {
                                "command_id": cmd_id,
                                "executable": sys.executable,
                                "argv": ["-B", "-c", f"print({command_canary!r})"],
                                "cwd": ".",
                                "timeout_ms": 10000,
                                "purpose": "diagnostic",
                                "explanation": "emit XSS canary to stdout safely",
                            },
                            "metadata": {},
                        },
                        "expected_outcomes": [
                            {
                                "schema_version": PROJECT_SCHEMA_VERSION,
                                "outcome_type": "project_tool_result",
                                "payload": {"action_type": "propose_command", "require_success": False},
                                "required": True,
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
            if cmd_stage == "proposed":
                cmd_stage = "started"
                return json.dumps(
                    {
                        "schema_version": PROJECT_SCHEMA_VERSION,
                        "intent": "start approved canary command",
                        "decision_summary": "start_command canary",
                        "proposed_action": {
                            "schema_version": PROJECT_SCHEMA_VERSION,
                            "action_type": "start_command",
                            "payload": {
                                "proposal_id": str(last.get("proposal_id")),
                                "proposal_digest": str(last.get("digest")),
                            },
                            "metadata": {},
                        },
                        "expected_outcomes": [
                            {
                                "schema_version": PROJECT_SCHEMA_VERSION,
                                "outcome_type": "project_tool_result",
                                "payload": {"action_type": "start_command", "require_success": False},
                                "required": True,
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
            if cmd_stage == "started" or (
                cmd_stage == "polling"
                and last.get("action_type") == "poll_command"
                and last.get("state") in ("running", "starting", "pending")
            ):
                cmd_stage = "polling"
                return json.dumps(
                    {
                        "schema_version": PROJECT_SCHEMA_VERSION,
                        "intent": "poll canary command until exited",
                        "decision_summary": "poll_command canary",
                        "proposed_action": {
                            "schema_version": PROJECT_SCHEMA_VERSION,
                            "action_type": "poll_command",
                            "payload": {"command_id": cmd_id, "after": 0},
                            "metadata": {},
                        },
                        "expected_outcomes": [
                            {
                                "schema_version": PROJECT_SCHEMA_VERSION,
                                "outcome_type": "project_tool_result",
                                "payload": {"action_type": "poll_command", "require_success": False},
                                "required": True,
                            }
                        ],
                    },
                    ensure_ascii=False,
                )
            if cmd_stage in ("started", "polling"):
                cmd_stage = "done"
            body = inner._next_step(req)
            assert body is not None
            return json.dumps(body, ensure_ascii=False)

        return SpyUsageProvider(_build)

    with LiveTcpTestServer(config=cfg, provider_factory=_factory) as srv, sync_playwright() as pw:
        if getattr(srv.app.state.service, "_chat_provider_factory", None) is None:
            srv.app.state.service._chat_provider_factory = lambda **_: (lambda req: {"action": "execution_offer", "goal": req.question, "summary": "执行任务"})
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page_errors, console_errors, non_loopback, reqs = _attach_watchers(page, base_url=srv.base_url)
            page.goto(f"{srv.base_url}/#token={cfg.access_token}", wait_until="domcontentloaded")
            assert "#token=" not in page.url

            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id("project-name").fill("u18-xss-safe")
            page.get_by_test_id("register-root").fill(str(proj_dir))
            page.get_by_test_id("confirm-read").check()
            page.get_by_test_id("register-project").click()
            expect(page.get_by_test_id("project-list")).to_contain_text("u18-xss-safe")

            page.get_by_test_id("nav-settings").click()
            page.get_by_test_id("settings-sec-constraints").click()
            page.get_by_test_id("constraints-input").fill(xss_constraint)
            page.get_by_test_id("save-constraints").click()
            expect(page.get_by_test_id("context-revision")).to_contain_text("rev=1")

            page.get_by_test_id("nav-chat").click()
            page.get_by_test_id("chat-input").fill(xss_goal)
            page.get_by_test_id("chat-send").click()
            page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
            page.locator('[data-testid^="offer-confirm-btn-"]').last.click()

            q_card = page.locator('[data-testid^="question-item-"]').first
            expect(q_card).to_be_visible(timeout=20000)
            expect(q_card).to_contain_text(xss_question)
            qid = (q_card.get_attribute("data-testid") or "").removeprefix("question-item-")
            page.get_by_test_id(f"question-answer-input-{qid}").fill(xss_answer)
            page.get_by_test_id(f"question-submit-{qid}").click()

            _approve_all_pending(page)
            page.get_by_test_id("toggle-inspector").click()
            expect(page.get_by_test_id("run-state")).to_have_text("succeeded", timeout=25000)

            expect(page.get_by_test_id("session-history")).to_contain_text(xss_goal)
            expect(page.get_by_test_id("question-list")).to_contain_text(xss_answer)
            expect(page.get_by_test_id("command-list")).to_contain_text(command_canary)
            page.get_by_test_id("diff-file-select").select_option("calc.py")
            expect(page.get_by_test_id("diff-detail")).to_contain_text(diff_canary)
            assert page.evaluate("() => window.__xss || null") is None
            assert page.evaluate("document.querySelectorAll('a[href^=\"javascript:\"]').length") == 0
            assert cfg.access_token not in page.locator("body").inner_text()
            assert cfg.access_token not in page.content()

            api_reqs = [r for r in reqs if "/api/" in r["url"]]
            assert api_reqs
            for r in api_reqs:
                assert r["headers"].get("authorization") == f"Bearer {cfg.access_token}"
            assert page_errors == []
            assert console_errors == []
            assert non_loopback == []
        finally:
            browser.close()


def test_u19_reload_readonly_current_identity(tmp_path: Path) -> None:
    proj_dir = project_helpers.create_buggy_calc_project(tmp_path / "u19-reload")
    cfg = project_helpers.make_project_host_config(tmp_path, allowed_roots=(proj_dir,))

    def _factory(*, project: ProjectView, run_id: str) -> SpyUsageProvider:
        _ = (project, run_id)
        inner = OfflineProjectProvider()

        def _build(idx: int, req: ModelRequest) -> str:
            if idx == 0:
                return forecast_json(
                    action_type="ask_user",
                    payload={"question": "Hold active run across reload?", "options": ["proceed"]},
                )
            body = inner._next_step(req)
            assert body is not None
            return json.dumps(body, ensure_ascii=False)

        return SpyUsageProvider(_build)

    with LiveTcpTestServer(config=cfg, provider_factory=_factory) as srv, sync_playwright() as pw:
        if getattr(srv.app.state.service, "_chat_provider_factory", None) is None:
            srv.app.state.service._chat_provider_factory = lambda **_: (lambda req: {"action": "execution_offer", "goal": req.question, "summary": "执行任务"})
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page_errors, console_errors, non_loopback, reqs = _attach_watchers(page, base_url=srv.base_url)
            # Observe-only SSE reader lifecycle telemetry (no app state mutation)
            page.add_init_script(
                """(() => {
                  const realFetch = window.fetch;
                  window.__sseObs = { totalStarts: 0, activeReaders: 0, maxActiveReaders: 0 };
                  window.fetch = async (input, init = {}) => {
                    const url = typeof input === 'string' ? input : (input && input.url) || '';
                    let isEvents = false;
                    try {
                      isEvents = new URL(url, window.location.href).pathname.endsWith('/events');
                    } catch {}
                    const resp = await realFetch(input, init);
                    if (isEvents && resp.ok && resp.body) {
                      window.__sseObs.totalStarts += 1;
                      const origGetReader = resp.body.getReader.bind(resp.body);
                      resp.body.getReader = () => {
                        const reader = origGetReader();
                        let open = true;
                        const obs = window.__sseObs;
                        obs.activeReaders += 1;
                        if (obs.activeReaders > obs.maxActiveReaders) obs.maxActiveReaders = obs.activeReaders;
                        const closeReader = () => {
                          if (open) {
                            open = false;
                            obs.activeReaders -= 1;
                          }
                        };
                        if (init && init.signal) {
                          if (init.signal.aborted) closeReader();
                          else init.signal.addEventListener('abort', closeReader, { once: true });
                        }
                        const origRead = reader.read.bind(reader);
                        reader.read = async (...args) => {
                          try {
                            const chunk = await origRead(...args);
                            if (chunk && chunk.done) closeReader();
                            return chunk;
                          } catch (err) {
                            closeReader();
                            throw err;
                          }
                        };
                        const origCancel = reader.cancel.bind(reader);
                        reader.cancel = async (...args) => {
                          closeReader();
                          return await origCancel(...args);
                        };
                        return reader;
                      };
                    }
                    return resp;
                  };
                })();"""
            )
            page.goto(f"{srv.base_url}/#token={cfg.access_token}", wait_until="domcontentloaded")

            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id("project-name").fill("u19-reload")
            page.get_by_test_id("register-root").fill(str(proj_dir))
            page.get_by_test_id("confirm-read").check()
            page.get_by_test_id("register-project").click()
            expect(page.get_by_test_id("project-list")).to_contain_text("u19-reload")
            page.get_by_test_id("nav-chat").click()

            page.get_by_test_id("chat-input").fill("Hold active run cleanly before reload")
            page.get_by_test_id("chat-send").click()
            page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
            page.locator('[data-testid^="offer-confirm-btn-"]').last.click()

            q_card = page.locator('[data-testid^="question-item-"]').first
            expect(q_card).to_be_visible(timeout=20000)
            # Settle setup history cleanly (outside the measured read-only reload window)
            page.reload(wait_until="domcontentloaded")
            q_card = page.locator('[data-testid^="question-item-"]').first
            expect(q_card).to_be_visible(timeout=20000)
            expect(page.get_by_test_id("connection-state")).to_have_text("connected")
            page.wait_for_function("() => document.querySelectorAll('[data-testid^=\"history-item-\"]').length >= 2")
            qid = (q_card.get_attribute("data-testid") or "").removeprefix("question-item-")

            page.get_by_test_id("toggle-inspector").click()
            pid_before = page.get_by_test_id("current-project").inner_text().strip()
            sid_before = page.get_by_test_id("current-session").inner_text().strip()
            rid_before = page.get_by_test_id("run-id").inner_text().strip()
            hist_before = page.locator('[data-testid^="history-item-"]').count()
            project_before = tree_hashes(proj_dir)
            assert {"calc.py", "tests/test_calc.py"} <= set(project_before)
            assert project_before
            ws_before = tree_hashes(cfg.workspace_parent)

            event_requests_before_reload = len([r for r in reqs if urlparse(r["url"]).path.endswith("/events")])
            assert event_requests_before_reload > 0
            page.wait_for_function("() => window.__sseObs && window.__sseObs.activeReaders === 1")
            obs_before = page.evaluate("() => window.__sseObs")
            assert obs_before["maxActiveReaders"] <= 1 and obs_before["activeReaders"] == 1

            # Reload while active run is held at ask_user (strictly read-only window)
            mark = len(reqs)
            page.reload(wait_until="domcontentloaded")
            expect(page.get_by_test_id("current-project")).to_have_text(pid_before)
            expect(page.get_by_test_id("current-session")).to_have_text(sid_before)
            expect(page.get_by_test_id("run-id")).to_have_text(rid_before)
            expect(page.get_by_test_id(f"question-item-{qid}")).to_be_visible(timeout=15000)
            expect(page.get_by_test_id("connection-state")).to_have_text("connected")
            expect(page.locator('[data-testid^="history-item-"]')).to_have_count(hist_before)

            page.wait_for_function("() => window.__sseObs && window.__sseObs.activeReaders === 1")
            obs_after = page.evaluate("() => window.__sseObs")
            event_requests_after_reload = len([r for r in reqs[mark:] if urlparse(r["url"]).path.endswith("/events")])
            mutation_requests_during_reload = [
                r for r in reqs[mark:] if r["method"] in {"POST", "PUT", "DELETE"}
            ]
            assert event_requests_after_reload > 0
            assert obs_after["maxActiveReaders"] <= 1 and obs_after["activeReaders"] == 1
            assert not mutation_requests_during_reload
            assert tree_hashes(proj_dir) == project_before
            assert tree_hashes(cfg.workspace_parent) == ws_before

            # Setup completion (outside read-only reload window) & keep terminal reload check
            page.get_by_test_id(f"question-answer-input-{qid}").fill("proceed")
            page.get_by_test_id(f"question-submit-{qid}").click()
            _approve_all_pending(page)
            expect(page.get_by_test_id("run-state")).to_have_text("succeeded", timeout=20000)
            page.wait_for_function("() => document.querySelectorAll('[data-testid^=\"history-item-\"]').length >= 4")

            hist_terminal = page.locator('[data-testid^="history-item-"]').count()
            project_terminal = tree_hashes(proj_dir)
            assert {"calc.py", "tests/test_calc.py"} <= set(project_terminal)
            assert project_terminal
            ws_terminal = tree_hashes(cfg.workspace_parent)
            mark_term = len(reqs)
            page.reload(wait_until="domcontentloaded")
            expect(page.get_by_test_id("current-project")).to_have_text(pid_before)
            expect(page.get_by_test_id("current-session")).to_have_text(sid_before)
            expect(page.get_by_test_id("run-id")).to_have_text(rid_before)
            expect(page.get_by_test_id("run-state")).to_have_text("succeeded")
            expect(page.locator('[data-testid^="history-item-"]')).to_have_count(hist_terminal)
            assert not [r for r in reqs[mark_term:] if r["method"] in {"POST", "PUT", "DELETE"}]
            assert len([r for r in reqs[mark_term:] if urlparse(r["url"]).path.endswith("/events")]) <= 1
            assert tree_hashes(proj_dir) == project_terminal
            assert tree_hashes(cfg.workspace_parent) == ws_terminal
            assert page_errors == []
            assert console_errors == []
            assert non_loopback == []
        finally:
            browser.close()


def test_u20_documented_capability_boundary(tmp_path: Path) -> None:
    readme_text = _README_PATH.read_text(encoding="utf-8")
    index_html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
    app_js = (_STATIC_DIR / "app.js").read_text(encoding="utf-8")

    # Required documented capabilities & honest boundaries match implementation
    for expected in (
        "offline-metrics-v1",
        "offline-project-v1",
        "LIVE_DISABLED",
        "1s / 2s / 4s",
        "草稿未保存，刷新会丢失",
        "非 OS 沙箱",
        "trusted_local_execution",
        "Authorization: Bearer <token>",
        "textContent",
        "--project-root",
        "128 MiB",
    ):
        assert expected in readme_text, f"missing in README: {expected}"

    for tid in (
        "current-project",
        "current-session",
        "connection-state",
        "run-history-select",
        "diff-file-select",
        "diff-detail",
        "navigation-toggle",
        "draft-status",
        "host-status-banner",
        "project-list",
        "session-list",
        "storage-quota",
        "usage-summary",
        "coverage-summary",
        "diff-list",
    ):
        assert f'data-testid="{tid}"' in index_html, f"missing data-testid: {tid}"

    assert 'data-testid="offline-banner"' not in index_html
    assert "innerHTML" not in app_js
    assert "http://" not in index_html and "https://" not in index_html

    cfg = make_host_config(tmp_path)
    with LiveTcpTestServer(config=cfg) as srv, sync_playwright() as pw:
        if getattr(srv.app.state.service, "_chat_provider_factory", None) is None:
            srv.app.state.service._chat_provider_factory = lambda **_: (lambda req: {"action": "execution_offer", "goal": req.question, "summary": "执行任务"})
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page_errors, console_errors, non_loopback, _ = _attach_watchers(page, base_url=srv.base_url)
            page.goto(f"{srv.base_url}/#token={cfg.access_token}", wait_until="domcontentloaded")
            page.get_by_test_id("nav-settings").click()
            page.get_by_test_id("settings-sec-constraints").click()
            expect(page.get_by_test_id("draft-status")).to_contain_text("草稿未保存，刷新会丢失")
            page.get_by_test_id("settings-sec-permissions").click()
            expect(page.locator("#settings-panel-permissions")).to_contain_text("非 OS 沙箱")
            page.get_by_test_id("settings-sec-storage").click()
            expect(page.locator("#settings-panel-storage")).to_contain_text("真实用量")
            assert page_errors == []
            assert console_errors == []
            assert non_loopback == []
        finally:
            browser.close()
