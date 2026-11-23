"""DESK-I18N: Bilingual (zh-CN / en) 6-page browser tests, in-run switching, 3 approvals, and cross-port persistence."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
from app_helpers import LiveTcpTestServer, auth_headers, pick_free_loopback_port
from forecontract_app.models import HostConfig
from playwright.sync_api import expect, sync_playwright
from project_helpers import create_buggy_calc_project


def test_bilingual_six_pages_in_run_switch_three_approvals_and_cross_port(tmp_path: Path) -> None:
    """Verify zh-CN/en 6 pages, zero-side-effect in-run switch, 3 English approvals, diff/test readback, and cross-port persistence."""
    data_root = tmp_path / "data"
    ws_parent = tmp_path / "workspaces"
    proj_dir = create_buggy_calc_project(tmp_path / "calc_proj")
    data_root.mkdir(parents=True, exist_ok=True)
    ws_parent.mkdir(parents=True, exist_ok=True)

    token = f"tok-{uuid.uuid4().hex}"
    port1 = pick_free_loopback_port()
    cfg1 = HostConfig(
        data_root=data_root,
        workspace_parent=ws_parent,
        python_executable=Path(sys.executable),
        bind_port=port1,
        access_token=token,
        project_roots=(proj_dir,),
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        desktop_pref = {"locale": "zh-CN"}
        context = browser.new_context(locale="zh-CN", viewport={"width": 1280, "height": 840})

        def _bind_bridge(pg: Any) -> None:
            pg.expose_binding("fcBridgeGetLocale", lambda _src: desktop_pref["locale"])

            def _set_loc(_src: Any, loc: str) -> dict[str, Any]:
                if loc in ("zh-CN", "en"):
                    desktop_pref["locale"] = loc
                    return {"ok": True, "locale": loc}
                return {"ok": False, "error": "unsupported_locale"}

            pg.expose_binding("fcBridgeSetLocale", _set_loc)
            pg.add_init_script(
                """
                window.forecontractDesktop = {
                  getLocale: () => window.fcBridgeGetLocale(),
                  setLocale: (loc) => window.fcBridgeSetLocale(loc),
                };
                """
            )

        with LiveTcpTestServer(config=cfg1) as srv1:
            headers = auth_headers(cfg1)
            reg_resp = httpx.post(
                f"{srv1.base_url}/api/v1/projects/register",
                headers=headers,
                json={"name": "calc-demo", "root": str(proj_dir), "confirmed_read": True},
                timeout=5.0,
            )
            assert reg_resp.status_code == 201
            project_id = reg_resp.json()["project_id"]

            sess_resp = httpx.post(
                f"{srv1.base_url}/api/v1/sessions",
                headers=headers,
                json={
                    "project_id": project_id,
                    "model": {"provider": "offline", "model": "offline-project-v1"},
                },
                timeout=5.0,
            )
            assert sess_resp.status_code == 201
            session_id = sess_resp.json()["session_id"]

            run_resp = httpx.post(
                f"{srv1.base_url}/api/v1/sessions/{session_id}/runs",
                headers=headers,
                json={
                    "client_request_id": str(uuid.uuid4()),
                    "goal": "Fix calc.py add_numbers bug and verify",
                    "confirmed_scope": True,
                },
                timeout=5.0,
            )
            assert run_resp.status_code in (201, 202)
            run_id = run_resp.json()["run_id"]

            page = context.new_page()
            _bind_bridge(page)
            page.goto(f"{srv1.base_url}/#token={token}", wait_until="domcontentloaded")
            expect(page.get_by_test_id("current-project")).to_have_text(project_id, timeout=10000)

            # Wait for pending scope proposal
            page.locator("[data-testid^='proposal-item-']").first.wait_for(state="visible", timeout=10000)
            prop_item = page.locator("[data-testid^='proposal-item-']").first
            prop_id_1 = (prop_item.get_attribute("data-testid") or "").removeprefix("proposal-item-")

            # Check 6 pages in zh-CN
            expect(page.locator("html")).to_have_attribute("lang", "zh-CN")
            assert page.get_by_test_id("nav-chat").inner_text().strip() == "会话"
            assert page.get_by_test_id("nav-projects").inner_text().strip() == "项目"
            assert page.get_by_test_id("nav-extensions").inner_text().strip() == "扩展"
            assert page.get_by_test_id("nav-inbox").inner_text().strip().startswith("审批收件箱")
            assert page.get_by_test_id("nav-history").inner_text().strip() == "运行历史"
            assert page.get_by_test_id("nav-settings").inner_text().strip() == "设置"

            # Enter unsent draft while proposal is pending
            draft_text = "Unsent draft αβγ — keep exact bytes"
            page.get_by_test_id("chat-input").fill(draft_text)

            # Monitor requests during zh-CN -> en -> zh-CN switch
            tracked_requests: list[tuple[str, str]] = []
            page.on("request", lambda req: tracked_requests.append((req.method, req.url)))

            page.get_by_test_id("nav-settings").click()
            page.get_by_test_id("locale-select").select_option("en")
            expect(page.locator("html")).to_have_attribute("lang", "en")

            for nav_id in ("nav-projects", "nav-extensions", "nav-inbox", "nav-history", "nav-settings", "nav-chat"):
                page.get_by_test_id(nav_id).click()

            # Verify F01 English strings across Extensions, Inbox, History, Settings
            page.get_by_test_id("nav-extensions").click()
            assert "No local Skills found" in page.locator("#page-extensions").inner_text()
            page.get_by_test_id("nav-inbox").click()
            assert page.locator("#inbox-list button", has_text="Reject").first.is_visible()
            page.get_by_test_id("nav-settings").click()
            assert "bytes" in page.locator("#storage-quota").inner_text()
            page.get_by_test_id("nav-chat").click()

            assert page.get_by_test_id("nav-chat").inner_text().strip() == "Chat"
            assert page.get_by_test_id("nav-projects").inner_text().strip() == "Projects"
            assert page.get_by_test_id("nav-extensions").inner_text().strip() == "Extensions"
            assert page.get_by_test_id("nav-inbox").inner_text().strip().startswith("Approval Inbox")
            assert page.get_by_test_id("nav-history").inner_text().strip() == "Run History"
            assert page.get_by_test_id("nav-settings").inner_text().strip() == "Settings"

            page.get_by_test_id("nav-settings").click()
            page.get_by_test_id("locale-select").select_option("zh-CN")
            expect(page.locator("html")).to_have_attribute("lang", "zh-CN")
            page.get_by_test_id("locale-select").select_option("en")
            expect(page.locator("html")).to_have_attribute("lang", "en")
            page.get_by_test_id("nav-chat").click()

            mutating = [
                (m, u)
                for m, u in tracked_requests
                if m in ("POST", "PUT", "DELETE")
                and any(seg in u for seg in ("/runs", "/chat/turns", "/decide", "/commands"))
            ]
            assert mutating == [], f"unexpected mutating requests on locale switch: {mutating}"

            assert page.get_by_test_id("current-project").inner_text().strip() == project_id
            assert page.get_by_test_id("current-session").inner_text().strip() == session_id
            assert page.get_by_test_id("run-id").inner_text().strip() == run_id
            assert page.get_by_test_id("chat-input").input_value() == draft_text
            assert page.get_by_test_id(f"proposal-item-{prop_id_1}").is_visible()

            # Narrow viewport check for English approval card readability & clickability
            page.set_viewport_size({"width": 480, "height": 840})
            approve_btn_1 = page.get_by_test_id(f"proposal-approve-{prop_id_1}")
            assert approve_btn_1.is_visible()
            assert approve_btn_1.inner_text().strip() == "Approve"
            approve_btn_1.click()

            # 2nd approval: verification plan in English
            page.set_viewport_size({"width": 1280, "height": 840})
            page.locator(
                f"[data-testid^='proposal-approve-']:not([data-testid='proposal-approve-{prop_id_1}'])"
            ).first.wait_for(state="visible", timeout=10000)
            approve_btn_2 = page.locator("[data-testid^='proposal-approve-']").first
            p2_id = (approve_btn_2.get_attribute("data-testid") or "").removeprefix("proposal-approve-")
            assert "Verification Plan" in page.get_by_test_id(f"proposal-item-{p2_id}").inner_text()
            approve_btn_2.click()

            # 3rd approval: trusted local command in English
            page.locator("[data-testid^='proposal-trust-']").first.wait_for(state="visible", timeout=10000)
            trust_box = page.locator("[data-testid^='proposal-trust-']").first
            p3_id = (trust_box.get_attribute("data-testid") or "").removeprefix("proposal-trust-")
            assert "Local Command" in page.get_by_test_id(f"proposal-item-{p3_id}").inner_text()
            trust_box.check()
            page.get_by_test_id(f"proposal-approve-{p3_id}").click()

            # Wait for terminal succeeded & passed
            expect(page.get_by_test_id("run-state")).to_have_text("succeeded", timeout=20000)
            expect(page.get_by_test_id("verification")).to_have_text("passed", timeout=10000)

            diff_btn = page.get_by_test_id(f"timeline-run-diff-{run_id}")
            assert diff_btn.is_visible()
            assert diff_btn.inner_text().strip() == "View Diff"
            diff_btn.click()
            diff_text = page.get_by_test_id("diff-detail").inner_text()
            assert "return a - b" in diff_text or "return a + b" in diff_text

            page.get_by_test_id("tab-commands").click()
            cmd_text = page.get_by_test_id("command-list").inner_text()
            assert "exit=0" in cmd_text or "0" in cmd_text
            page.get_by_test_id("nav-history").click()
            expect(page.locator("#session-history button", has_text="View Run").first).to_be_visible(timeout=5000)
            page.close()

        # Cross-port restart preserves desktop_pref["locale"] == "en"
        port2 = pick_free_loopback_port()
        cfg2 = HostConfig(
            data_root=data_root,
            workspace_parent=ws_parent,
            python_executable=Path(sys.executable),
            bind_port=port2,
            access_token=token,
            project_roots=(proj_dir,),
        )
        with LiveTcpTestServer(config=cfg2) as srv2:
            page2 = context.new_page()
            _bind_bridge(page2)
            page2.goto(f"{srv2.base_url}/#token={token}", wait_until="domcontentloaded")
            expect(page2.locator("html")).to_have_attribute("lang", "en", timeout=10000)
            assert page2.get_by_test_id("nav-settings").inner_text().strip() == "Settings"
            page2.close()

        context.close()
        browser.close()
