"""Offline Playwright visual and interaction acceptance tests for CHAT-T04 (2 fixed nodes)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import project_helpers
from app_helpers import LiveTcpTestServer, auth_headers
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, expect, sync_playwright


def _launch_chromium(pw: Any) -> Any:
    try:
        return pw.chromium.launch(headless=True)
    except (OSError, PlaywrightError):
        return pw.chromium.launch(headless=True, channel="chromium")


def _register_and_open_chat(page: Page, url: str, proj_dir: Path, name: str = "visual-proj") -> str:
    page.goto(url, wait_until="domcontentloaded")
    expect(page.locator("#page-chat")).to_be_visible()
    expect(page.get_by_test_id("chat-inspector")).to_be_hidden()
    expect(page.get_by_test_id("brand-logo")).to_be_visible()

    page.get_by_test_id("nav-projects").click()
    page.get_by_test_id("project-name").fill(name)
    page.get_by_test_id("register-root").fill(str(proj_dir))
    page.get_by_test_id("confirm-read").check()
    page.get_by_test_id("register-project").click()
    expect(page.get_by_test_id("current-project")).not_to_have_text("-", timeout=10000)
    return page.get_by_test_id("current-project").inner_text().strip()


def _assert_focusable(page: Page, testid: str) -> None:
    loc = page.get_by_test_id(testid)
    expect(loc).to_be_visible()
    expect(loc).to_be_enabled()
    loc.focus()
    focused_tid = page.evaluate("() => document.activeElement?.getAttribute('data-testid') || ''")
    assert focused_tid == testid, f"expected focus on {testid!r}, got {focused_tid!r}"


def _assert_wide_rail_and_composer(page: Page, *, stage: str) -> None:
    """Assert 1280x800 left rail bounds/vertical order and composer/send visibility at bottom."""
    nav_box = page.get_by_test_id("workspace-nav").bounding_box()
    thread_box = page.locator("#page-chat .chat-thread").bounding_box()
    assert nav_box is not None and thread_box is not None, f"missing boxes at {stage}"
    assert 180 <= nav_box["width"] <= 260, f"unexpected rail width {nav_box['width']} at {stage}"
    assert nav_box["x"] + nav_box["width"] <= thread_box["x"] + 2, f"rail overlaps thread at {stage}"

    brand_box = page.get_by_test_id("brand-logo").bounding_box()
    proj_box = page.get_by_test_id("chat-project-select").bounding_box()
    sess_box = page.get_by_test_id("chat-session-list").bounding_box()
    inbox_box = page.get_by_test_id("nav-inbox").bounding_box()
    hist_box = page.get_by_test_id("nav-history").bounding_box()
    set_box = page.get_by_test_id("nav-settings").bounding_box()
    for name, b in (
        ("brand-logo", brand_box),
        ("chat-project-select", proj_box),
        ("chat-session-list", sess_box),
        ("nav-inbox", inbox_box),
        ("nav-history", hist_box),
        ("nav-settings", set_box),
    ):
        assert b is not None, f"{name} missing box at {stage}"
        assert b["x"] >= nav_box["x"] - 1 and b["x"] + b["width"] <= nav_box["x"] + nav_box["width"] + 1, (
            f"{name} outside rail horizontal bounds at {stage}: {b} vs {nav_box}"
        )
    assert brand_box["y"] < proj_box["y"] < sess_box["y"]
    assert sess_box["y"] + sess_box["height"] <= inbox_box["y"] + 2
    assert inbox_box["y"] < hist_box["y"] < set_box["y"]
    assert (nav_box["y"] + nav_box["height"]) - (set_box["y"] + set_box["height"]) <= 96

    for scroll_label, scroll_js in (
        ("initial", "() => { const s = document.getElementById('chat-scroll-area'); if (s) s.scrollTop = 0; }"),
        ("scrolled", "() => { const s = document.getElementById('chat-scroll-area'); if (s) s.scrollTop = s.scrollHeight; }"),
    ):
        page.evaluate(scroll_js)
        vh = int(page.evaluate("() => window.innerHeight"))
        comp_box = page.get_by_test_id("chat-composer").bounding_box()
        send_box = page.get_by_test_id("chat-send").bounding_box()
        assert comp_box is not None and send_box is not None
        bottom_gap = vh - (comp_box["y"] + comp_box["height"])
        assert 0 <= bottom_gap <= 40, f"composer bottom gap {bottom_gap}px > 40px at {stage}/{scroll_label}"
        assert 0 <= send_box["y"] and send_box["y"] + send_box["height"] <= vh, (
            f"send button outside viewport at {stage}/{scroll_label}: {send_box} vh={vh}"
        )


def test_workbench_visual_identity(tmp_path: Path) -> None:
    """Verify two-session read-only chat & title switching, single composer, evidence drawer binding, and 1280x800 / 760x720 responsiveness."""
    proj_dir = tmp_path / "workbench_visual_proj"
    project_helpers.create_buggy_calc_project(proj_dir)
    cfg = project_helpers.make_project_host_config(tmp_path, allowed_roots=(proj_dir,))

    with LiveTcpTestServer(config=cfg) as srv:
        from forecontract_app.chat_runtime import OfflineReadonlyChatProvider

        ro_provider = OfflineReadonlyChatProvider()

        def _chat_factory(*, project, session_id: str, turn_id: str):
            _ = (project, session_id, turn_id)

            def _p(req):
                if "修复 calc.py" in req.question or "weighted_positive_total" in req.question:
                    return {
                        "action": "execution_offer",
                        "goal": req.question,
                        "summary": "将执行任务",
                    }
                return ro_provider(req)

            return _p

        srv.app.state.service._chat_provider_factory = _chat_factory
        url = f"{srv.base_url}/#token={cfg.access_token}"
        hdrs = auth_headers(cfg)
        page_errors: list[str] = []
        console_errors: list[str] = []
        non_loopback: list[str] = []

        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 800})
                page.on("pageerror", lambda err: page_errors.append(str(err)))
                page.on(
                    "console",
                    lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
                )
                page.on(
                    "request",
                    lambda req: (
                        non_loopback.append(req.url)
                        if not req.url.startswith(srv.base_url)
                        else None
                    ),
                )

                _register_and_open_chat(page, url, proj_dir, name="wb-proj")

                # Create two sessions under the same project
                page.get_by_test_id("new-session").click()
                expect(page.get_by_test_id("current-session")).not_to_have_text("-", timeout=10000)
                s1 = page.get_by_test_id("current-session").inner_text().strip()

                page.get_by_test_id("new-session").click()
                expect(page.get_by_test_id("current-session")).not_to_have_text(s1, timeout=10000)
                s2 = page.get_by_test_id("current-session").inner_text().strip()
                assert s1 != s2

                page.get_by_test_id("nav-chat").click()

                # Empty session s2 shows recognizable fallback title, single visible composer, and bottom-anchored composer
                s2_fallback = f"会话 {s2[:8]}"
                expect(page.get_by_test_id(f"chat-session-item-{s2}")).to_contain_text(s2_fallback)
                expect(page.get_by_test_id("chat-session-title")).to_have_text(s2_fallback)
                _assert_wide_rail_and_composer(page, stage="empty_session")
                visible_composers = page.evaluate(
                    """() => Array.from(document.querySelectorAll('#page-chat textarea, #page-chat input[type="text"]'))
                        .filter(el => {
                            const cs = getComputedStyle(el);
                            const r = el.getBoundingClientRect();
                            return cs.display !== 'none' && cs.visibility !== 'hidden' && r.width > 0 && r.height > 0;
                        }).map(el => el.getAttribute('data-testid'))"""
                )
                assert visible_composers == ["chat-input"]

                # Switch to s1 and send a real read-only question
                page.get_by_test_id(f"chat-session-item-{s1}").locator("button").click()
                expect(page.get_by_test_id("current-session")).to_have_text(s1, timeout=10000)
                q1_text = "请检查 calc.py 中 add_numbers 的返回值"
                _assert_focusable(page, "chat-input")
                page.get_by_test_id("chat-input").fill(q1_text)
                _assert_focusable(page, "chat-send")
                page.get_by_test_id("chat-send").click()

                turn1 = page.get_by_test_id("chat-turn-1")
                expect(turn1).to_be_visible(timeout=15000)
                expect(turn1.locator(".chat-msg-user .chat-msg-text")).to_have_text(q1_text)
                expect(turn1.locator(".chat-msg-assistant .chat-msg-text")).not_to_be_empty(
                    timeout=15000
                )
                expect(page.get_by_test_id(f"chat-session-item-{s1}")).to_contain_text(
                    q1_text, timeout=10000
                )
                expect(page.get_by_test_id("chat-session-title")).to_contain_text(q1_text)
                _assert_wide_rail_and_composer(page, stage="after_reply")

                # Switch to s2 -> verifies empty timeline & s2 fallback title, then ask s2 question
                page.get_by_test_id(f"chat-session-item-{s2}").locator("button").click()
                expect(page.get_by_test_id("current-session")).to_have_text(s2, timeout=10000)
                expect(page.get_by_test_id("chat-session-title")).to_have_text(s2_fallback)
                expect(page.locator("#chat-empty-hint")).to_be_hidden()

                q2_text = "请读取 tests/test_calc.py 的测试内容"
                page.get_by_test_id("chat-input").fill(q2_text)
                page.get_by_test_id("chat-send").click()
                expect(page.get_by_test_id("chat-turn-1")).to_be_visible(timeout=15000)
                expect(
                    page.get_by_test_id("chat-turn-1").locator(".chat-msg-user .chat-msg-text")
                ).to_have_text(q2_text)
                expect(page.get_by_test_id(f"chat-session-item-{s2}")).to_contain_text(
                    q2_text, timeout=10000
                )

                # Switch back to s1 -> restores s1 readable title, s1 content, and single visible composer
                page.get_by_test_id(f"chat-session-item-{s1}").locator("button").click()
                expect(page.get_by_test_id("current-session")).to_have_text(s1, timeout=10000)
                expect(page.get_by_test_id("chat-session-title")).to_contain_text(q1_text)
                expect(
                    page.get_by_test_id("chat-turn-1").locator(".chat-msg-user .chat-msg-text")
                ).to_have_text(q1_text)
                assert q2_text not in page.get_by_test_id("chat-timeline").inner_text()

                # Start a real run in s1 to produce evidence and inline pending approvals
                page.get_by_test_id("chat-input").fill("修复 calc.py 中的 add_numbers 并运行 pytest")
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                page.locator('[data-testid^="offer-confirm-btn-"]').last.click()

                scope_item = page.locator(
                    '[data-testid^="proposal-item-"]', has_text="[pending] scope "
                ).first
                expect(scope_item).to_be_visible(timeout=20000)
                expect(page.get_by_test_id("pending-approvals-card")).to_be_visible()
                active_run_card = page.locator('article[data-testid^="timeline-run-"]').first
                expect(active_run_card.get_by_test_id("pending-approvals-card")).to_be_visible()
                scope_pid = (scope_item.get_attribute("data-testid") or "").removeprefix("proposal-item-")
                scope_visible_txt = scope_item.inner_text()
                assert "calc.py" in scope_visible_txt
                assert "尚未写入磁盘" in scope_visible_txt
                for raw_tok in (scope_pid, "digest=", "ops=[", "exact=[", "[pending]"):
                    assert raw_tok not in scope_visible_txt, f"unexpected raw token {raw_tok!r} in visible proposal"
                scope_item.locator(".proposal-tech-details summary").click()
                assert scope_pid in scope_item.inner_text()
                assert "digest=" in scope_item.inner_text()
                scope_item.locator(".proposal-tech-details summary").click()
                scope_approve_tid = (
                    scope_item.locator('[data-testid^="proposal-approve-"]').get_attribute(
                        "data-testid"
                    )
                    or ""
                )
                assert scope_approve_tid

                # Verify 1280x800 and 760x720 layout: zero horizontal overflow, composer & pending approval reachable
                _assert_wide_rail_and_composer(page, stage="pending_approval")
                comp_box_pa = page.get_by_test_id("chat-composer").bounding_box()
                approve_box_pa = page.get_by_test_id(scope_approve_tid).bounding_box()
                assert comp_box_pa is not None and approve_box_pa is not None
                assert approve_box_pa["y"] + approve_box_pa["height"] <= comp_box_pa["y"] + 2
                for vp_w, vp_h in ((1280, 800), (760, 720)):
                    page.set_viewport_size({"width": vp_w, "height": vp_h})
                    overflow = page.evaluate(
                        "() => document.documentElement.scrollWidth > window.innerWidth"
                    )
                    assert overflow is False, f"horizontal overflow at {vp_w}x{vp_h}"
                    _assert_focusable(page, "chat-input")
                    _assert_focusable(page, "chat-send")
                    _assert_focusable(page, scope_approve_tid)

                # On narrow screen 760x720, open evidence drawer and close it back to same session
                page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("chat-inspector")).to_be_visible()
                assert (
                    page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth")
                    is False
                )
                _assert_focusable(page, "close-inspector")
                page.get_by_test_id("close-inspector").click()
                expect(page.get_by_test_id("chat-inspector")).to_be_hidden()
                expect(page.get_by_test_id("current-session")).to_have_text(s1)

                # Restore 1280x800 and complete the 3 approval stages (scope -> verification -> command)
                page.set_viewport_size({"width": 1280, "height": 800})
                page.get_by_test_id(scope_approve_tid).click()

                plan_item = page.locator(
                    '[data-testid^="proposal-item-"]', has_text="[pending] verification "
                ).first
                expect(plan_item).to_be_visible(timeout=20000)
                plan_item.locator('[data-testid^="proposal-approve-"]').click()

                cmd_item = page.locator(
                    '[data-testid^="proposal-item-"]', has_text="[pending] command "
                ).first
                expect(cmd_item).to_be_visible(timeout=20000)
                cmd_item.locator('[data-testid^="proposal-trust-"]').check()
                cmd_item.locator('[data-testid^="proposal-approve-"]').click()

                run_card = page.locator('article[data-testid^="timeline-run-"]').first
                expect(run_card).to_be_visible(timeout=15000)
                rid = (run_card.get_attribute("data-testid") or "").removeprefix("timeline-run-")
                expect(page.get_by_test_id(f"timeline-run-state-{rid}")).to_have_attribute(
                    "data-run-state", "succeeded", timeout=30000
                )
                expect(page.get_by_test_id(f"timeline-run-state-{rid}")).to_contain_text(
                    "已完成 · 验证通过", timeout=30000
                )
                expect(page.get_by_test_id(f"timeline-run-state-{rid}")).to_have_attribute(
                    "data-status-tone", "succeeded"
                )
                expect(page.get_by_test_id("pending-approvals-card")).to_be_hidden()
                assert "等待批准" not in page.locator("#page-chat").inner_text()
                files_txt = page.get_by_test_id(f"timeline-run-files-{rid}").inner_text()
                assert "calc.py" in files_txt and "已修改" in files_txt
                assert "changed_paths(" not in files_txt
                checks_txt = page.get_by_test_id(f"timeline-run-checks-{rid}").inner_text()
                assert "验证通过" in checks_txt
                assert "passed=true" not in checks_txt
                prop_summary = page.get_by_test_id(f"timeline-run-proposals-{rid}")
                expect(prop_summary).to_be_visible()
                prop_summary_txt = prop_summary.inner_text()
                assert "已批准" in prop_summary_txt
                host_props = httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{s1}/runs/{rid}/proposals",
                    headers=hdrs,
                    timeout=10.0,
                ).json()
                decided_host_props = [
                    p
                    for p in host_props
                    if (p.get("state") or p.get("status")) in ("approved", "rejected", "revoked")
                ]
                assert len(decided_host_props) >= 1
                assert prop_summary.locator("[data-proposal-id]").count() == len(decided_host_props)

                def _expected_proposal_text(hp: dict[str, Any]) -> str:
                    st = hp.get("state") or hp.get("status") or ""
                    st_label = {"approved": "已批准", "rejected": "已拒绝", "revoked": "已撤销"}[st]
                    payload = hp.get("payload") or {}
                    kind = hp.get("kind")
                    if kind == "scope":
                        exact = ",".join(payload.get("exact_paths") or [])
                        prefixes = ",".join(payload.get("write_prefixes") or [])
                        target = f"写入范围（{exact or prefixes or '指定路径'}）"
                    elif kind == "verification":
                        plan = payload.get("plan") or {}
                        checks = plan.get("checks")
                        checks_txt = (
                            "、".join((c.get("check_id") or "check") for c in checks)
                            if isinstance(checks, list)
                            else "验证计划"
                        )
                        target = f"验证计划（{checks_txt}）"
                    elif kind == "command":
                        cmd = payload.get("command") or hp.get("command") or {}
                        argv_txt = " ".join(cmd["argv"]) if isinstance(cmd.get("argv"), list) else ""
                        cmd_str = " ".join(part for part in (cmd.get("executable") or "", argv_txt) if part).strip()
                        target = f"命令（{cmd_str or '本机命令'}）"
                    else:
                        target = str(kind or "提案")
                    return f"{st_label} · {target}"

                for hp in decided_host_props:
                    pid = hp.get("id") or hp["proposal_id"]
                    pill = prop_summary.locator(f'[data-proposal-id="{pid}"]')
                    st = hp.get("state") or hp.get("status")
                    expect(pill).to_have_count(1)
                    expect(pill).to_have_attribute("data-decision", st)
                    expect(pill).to_have_text(_expected_proposal_text(hp))
                home_visible_text = page.locator("#page-chat").inner_text()
                assert rid not in home_visible_text
                assert s1 not in home_visible_text

                # Snapshot run set on host before opening and closing evidence drawer
                hist_before = httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{s1}/history",
                    headers=hdrs,
                    params={"after": "0", "limit": "50"},
                    timeout=10.0,
                ).json()["items"]
                run_ids_before = sorted({it["run_id"] for it in hist_before if it.get("run_id")})
                assert run_ids_before == [rid]

                # Open evidence drawer from run card -> verifies 3-column layout, bound sid/rid, diff source & content
                _assert_focusable(page, f"timeline-run-diff-{rid}")
                page.get_by_test_id(f"timeline-run-diff-{rid}").click()
                expect(page.get_by_test_id("chat-inspector")).to_be_visible()
                nav_b = page.get_by_test_id("workspace-nav").bounding_box()
                thr_b = page.locator("#page-chat .chat-thread").bounding_box()
                insp_b = page.get_by_test_id("chat-inspector").bounding_box()
                assert nav_b is not None and thr_b is not None and insp_b is not None
                assert nav_b["x"] + nav_b["width"] <= thr_b["x"] + 2
                assert thr_b["x"] + thr_b["width"] <= insp_b["x"] + 2
                expect(page.get_by_test_id("inspector-session-id")).to_have_text(s1)
                expect(page.get_by_test_id("run-id")).to_have_text(rid)
                expect(page.get_by_test_id("inspector-binding-meta")).to_contain_text(
                    f"session={s1} · run={rid}"
                )
                expect(page.get_by_test_id("diff-detail")).to_contain_text("path=calc.py")
                expect(page.get_by_test_id("diff-detail")).to_contain_text("source=ai")
                expect(page.get_by_test_id("diff-detail")).to_contain_text("return a + b")
                # Round-004/006/007: Diff target file, -/+ lines, and quote button sourced from host result.diff and inside viewport ∩ inspector clip rect
                host_run = httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{s1}/runs/{rid}",
                    headers=hdrs,
                    timeout=10.0,
                ).json()
                calc_ent = next(e for e in host_run["result"]["diff"]["entries"] if e["path"] == "calc.py")
                diff_lines = [ln for ln in str(calc_ent["diff_excerpt"]).splitlines() if not ln.startswith(("+++", "---"))]
                del_line = next(ln[1:].strip() for ln in diff_lines if ln.startswith("-"))
                add_line = next(ln[1:].strip() for ln in diff_lines if ln.startswith("+"))
                vp_w = page.viewport_size["width"]
                vp_h = page.viewport_size["height"]
                clip_x0 = max(0.0, insp_b["x"])
                clip_y0 = max(0.0, insp_b["y"])
                clip_x1 = min(float(vp_w), insp_b["x"] + insp_b["width"])
                clip_y1 = min(float(vp_h), insp_b["y"] + insp_b["height"])
                for first_screen_loc in (
                    page.locator("#diff-detail .diff-file-head", has_text=calc_ent["path"]).first,
                    page.locator("#diff-detail .diff-line-del", has_text=del_line).first,
                    page.locator("#diff-detail .diff-line-add", has_text=add_line).first,
                    page.get_by_test_id("quote-diff-in-chat"),
                ):
                    expect(first_screen_loc).to_be_visible()
                    box = first_screen_loc.bounding_box()
                    assert box is not None
                    assert box["x"] >= clip_x0 - 1 and box["x"] + box["width"] <= clip_x1 + 1
                    assert box["y"] >= clip_y0 - 1 and box["y"] + box["height"] <= clip_y1 + 1
                assert (
                    page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth")
                    is False
                )

                # Close evidence drawer -> returns to same session and run set is unchanged
                _assert_focusable(page, "close-inspector")
                page.get_by_test_id("close-inspector").click()
                expect(page.get_by_test_id("chat-inspector")).to_be_hidden()
                expect(page.get_by_test_id("current-session")).to_have_text(s1)

                # Switch session and reload -> verify decided proposals still match 1:1 on original run card
                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id("new-session").click()
                expect(page.get_by_test_id("current-session")).not_to_have_text(s1, timeout=10000)
                page.get_by_test_id(f"session-item-{s1}").click()
                expect(page.get_by_test_id("current-session")).to_have_text(s1, timeout=10000)
                page.get_by_test_id("nav-chat").click()
                switched_summary = page.get_by_test_id(f"timeline-run-proposals-{rid}")
                expect(switched_summary.locator("[data-proposal-id]")).to_have_count(
                    len(decided_host_props), timeout=10000
                )
                for hp in decided_host_props:
                    pid = hp.get("id") or hp["proposal_id"]
                    st = hp.get("state") or hp.get("status")
                    sw_pill = switched_summary.locator(f'[data-proposal-id="{pid}"]')
                    expect(sw_pill).to_have_attribute("data-decision", st)
                    expect(sw_pill).to_have_text(_expected_proposal_text(hp))
                page.reload(wait_until="domcontentloaded")
                expect(page.get_by_test_id("current-session")).to_have_text(s1, timeout=10000)
                reloaded_summary = page.get_by_test_id(f"timeline-run-proposals-{rid}")
                expect(reloaded_summary.locator("[data-proposal-id]")).to_have_count(
                    len(decided_host_props), timeout=10000
                )
                for hp in decided_host_props:
                    pid = hp.get("id") or hp["proposal_id"]
                    st = hp.get("state") or hp.get("status")
                    rl_pill = reloaded_summary.locator(f'[data-proposal-id="{pid}"]')
                    expect(rl_pill).to_have_attribute("data-decision", st)
                    expect(rl_pill).to_have_text(_expected_proposal_text(hp))

                hist_after = httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{s1}/history",
                    headers=hdrs,
                    params={"after": "0", "limit": "50"},
                    timeout=10.0,
                ).json()["items"]
                run_ids_after = sorted({it["run_id"] for it in hist_after if it.get("run_id")})
                assert run_ids_after == run_ids_before

                assert page_errors == []
                assert console_errors == []
                assert non_loopback == []
            finally:
                browser.close()


def test_settings_visual_navigation(tmp_path: Path) -> None:
    """Verify 6 settings sections, Ctrl+K search/keyboard/empty/Esc focus, pill jump, storage_used_bytes, theme/density persistence, and read-only boundaries."""
    proj_dir = tmp_path / "settings_visual_proj"
    project_helpers.create_buggy_calc_project(proj_dir)
    cfg = project_helpers.make_project_host_config(tmp_path, allowed_roots=(proj_dir,))

    with LiveTcpTestServer(config=cfg) as srv:
        from forecontract_app.chat_runtime import OfflineReadonlyChatProvider

        ro_provider = OfflineReadonlyChatProvider()

        def _chat_factory(*, project, session_id: str, turn_id: str):
            _ = (project, session_id, turn_id)

            def _p(req):
                if "修复 calc.py" in req.question or "weighted_positive_total" in req.question:
                    return {
                        "action": "execution_offer",
                        "goal": req.question,
                        "summary": "将执行任务",
                    }
                return ro_provider(req)

            return _p

        srv.app.state.service._chat_provider_factory = _chat_factory
        url = f"{srv.base_url}/#token={cfg.access_token}"
        hdrs = auth_headers(cfg)
        page_errors: list[str] = []
        console_errors: list[str] = []
        non_loopback: list[str] = []

        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 800})
                page.on("pageerror", lambda err: page_errors.append(str(err)))
                page.on(
                    "console",
                    lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
                )
                page.on(
                    "request",
                    lambda req: (
                        non_loopback.append(req.url)
                        if not req.url.startswith(srv.base_url)
                        else None
                    ),
                )

                _register_and_open_chat(page, url, proj_dir, name="set-proj")
                page.get_by_test_id("new-session").click()
                expect(page.get_by_test_id("current-session")).not_to_have_text("-", timeout=10000)
                sid = page.get_by_test_id("current-session").inner_text().strip()

                # 1. Traverse all 6 settings sections, verify 4-group 2-col overview on 1280x800, compact rail, and mutually exclusive panels
                page.get_by_test_id("nav-settings").click()
                assert page.locator("#page-settings .settings-nav-title").count() == 0
                expect(page.locator("#settings-heading")).to_be_visible()
                expect(page.locator("#settings-heading")).to_have_text("设置")
                assert page.locator("#page-settings .settings-main-head .sub").count() == 0
                assert page.locator("#settings-overview-grid .settings-kv-sub").count() == 0
                health_json = httpx.get(
                    f"{srv.base_url}/api/v1/health",
                    headers=hdrs,
                    timeout=10.0,
                ).json()
                ctx_init_json = httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{sid}/context",
                    headers=hdrs,
                    timeout=10.0,
                ).json()
                expect(page.get_by_test_id("settings-overview-desktop")).to_contain_text("关闭窗口")
                expect(page.get_by_test_id("settings-overview-desktop")).to_contain_text("收起到托盘")
                expect(page.get_by_test_id("settings-overview-diff")).to_contain_text("展示位置")
                expect(page.get_by_test_id("settings-overview-diff")).to_contain_text("右侧详情抽屉")
                expected_mode_label = (
                    "已连接"
                    if health_json.get("live_enabled")
                    else ("测试注入" if health_json.get("test_provider_injected") else "未连接")
                )
                expected_model_label = (
                    (health_json.get("model") or "deepseek-flash")
                    if health_json.get("live_enabled")
                    else ("测试替身" if health_json.get("test_provider_injected") else "未配置在线模型")
                )
                expect(page.get_by_test_id("settings-overview-host-mode")).to_contain_text(expected_mode_label)
                expect(page.get_by_test_id("settings-overview-model-val")).to_have_text(expected_model_label)
                expect(page.get_by_test_id("settings-overview-storage-bytes")).to_have_text(
                    f"{ctx_init_json['storage_used_bytes']} 字节"
                )
                assert "live_enabled" not in page.locator("#page-settings").inner_text()
                nav_b_set = page.get_by_test_id("workspace-nav").bounding_box()
                assert nav_b_set is not None and nav_b_set["width"] <= 90, f"expected compact icon rail on settings, got {nav_b_set}"
                ov_desk = page.get_by_test_id("settings-overview-desktop").bounding_box()
                ov_diff = page.get_by_test_id("settings-overview-diff").bounding_box()
                ov_mod = page.get_by_test_id("settings-overview-model").bounding_box()
                ov_sto = page.get_by_test_id("settings-overview-storage").bounding_box()
                for name, b in (("desktop", ov_desk), ("diff", ov_diff), ("model", ov_mod), ("storage", ov_sto)):
                    assert b is not None, f"missing overview card {name}"
                    assert b["y"] + b["height"] <= 800, f"overview card {name} not in initial 1280x800 viewport: {b}"
                assert ov_desk["x"] + ov_desk["width"] < ov_mod["x"]
                assert ov_diff["x"] + ov_diff["width"] < ov_sto["x"]
                assert ov_desk["y"] + ov_desk["height"] < ov_diff["y"]
                assert ov_mod["y"] + ov_mod["height"] < ov_sto["y"]
                ov_controls = page.locator(
                    "#settings-overview-grid input, #settings-overview-grid select, #settings-overview-grid textarea, #settings-overview-grid button, #settings-overview-grid progress"
                ).count()
                assert ov_controls == 0, f"expected 0 writable controls in settings-overview-grid, got {ov_controls}"

                # Narrow viewport 760x720 on settings: zero horizontal overflow and return to original session
                page.set_viewport_size({"width": 760, "height": 720})
                assert page.evaluate("() => document.documentElement.scrollWidth > window.innerWidth") is False
                page.get_by_test_id("navigation-toggle").click()
                page.get_by_test_id("nav-chat").click()
                expect(page.locator("#page-chat")).to_be_visible()
                expect(page.get_by_test_id("current-session")).to_have_text(sid)
                page.set_viewport_size({"width": 1280, "height": 800})
                page.get_by_test_id("nav-settings").click()
                assert page.locator("#page-settings .settings-nav-title").count() == 0
                expect(page.locator("#settings-heading")).to_be_visible()
                expect(page.locator("#settings-heading")).to_have_text("设置")
                assert page.locator("#page-settings .settings-main-head .sub").count() == 0
                assert page.locator("#settings-overview-grid .settings-kv-sub").count() == 0
                health_json = httpx.get(
                    f"{srv.base_url}/api/v1/health",
                    headers=hdrs,
                    timeout=10.0,
                ).json()
                ctx_init_json = httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{sid}/context",
                    headers=hdrs,
                    timeout=10.0,
                ).json()
                expect(page.get_by_test_id("settings-overview-desktop")).to_contain_text("关闭窗口")
                expect(page.get_by_test_id("settings-overview-desktop")).to_contain_text("收起到托盘")
                expect(page.get_by_test_id("settings-overview-diff")).to_contain_text("展示位置")
                expect(page.get_by_test_id("settings-overview-diff")).to_contain_text("右侧详情抽屉")
                expected_mode_label = (
                    "已连接"
                    if health_json.get("live_enabled")
                    else ("测试注入" if health_json.get("test_provider_injected") else "未连接")
                )
                expected_model_label = (
                    (health_json.get("model") or "deepseek-flash")
                    if health_json.get("live_enabled")
                    else ("测试替身" if health_json.get("test_provider_injected") else "未配置在线模型")
                )
                expect(page.get_by_test_id("settings-overview-host-mode")).to_contain_text(expected_mode_label)
                expect(page.get_by_test_id("settings-overview-model-val")).to_have_text(expected_model_label)
                expect(page.get_by_test_id("settings-overview-storage-bytes")).to_have_text(
                    f"{ctx_init_json['storage_used_bytes']} 字节"
                )
                assert "live_enabled" not in page.locator("#page-settings").inner_text()

                all_sections = (
                    "appearance",
                    "desktop",
                    "model",
                    "permissions",
                    "constraints",
                    "storage",
                )
                forbidden_tokens = (
                    "8 次请求",
                    "¥1",
                    "128 MiB",
                    "16 KiB",
                    "上限",
                    "Test Connection",
                    "测试连接",
                    "Archive & Clean",
                    "Retry Verification",
                )
                for sec in all_sections:
                    _assert_focusable(page, f"settings-sec-{sec}")
                    page.get_by_test_id(f"settings-sec-{sec}").click()
                    expect(page.locator(f"#settings-panel-{sec}")).to_be_visible()
                    for other in all_sections:
                        if other != sec:
                            expect(page.locator(f"#settings-panel-{other}")).to_be_hidden()
                    panel_text = page.locator(f"#settings-panel-{sec}").inner_text()
                    for tok in forbidden_tokens:
                        assert tok not in panel_text, f"forbidden token {tok!r} in section {sec}"

                # Confirm desktop, model, permissions, storage panels are strictly read-only (zero form controls / buttons)
                expect(page.locator("#settings-panel-desktop")).to_contain_text("系统托盘")
                expect(page.locator("#settings-panel-desktop")).to_contain_text("退出 ForeContract")
                for ro_sec in ("desktop", "model", "permissions", "storage"):
                    ro_controls = page.locator(
                        f"#settings-panel-{ro_sec} input, "
                        f"#settings-panel-{ro_sec} select, "
                        f"#settings-panel-{ro_sec} textarea, "
                        f"#settings-panel-{ro_sec} button, "
                        f"#settings-panel-{ro_sec} progress"
                    ).count()
                    assert ro_controls == 0, f"expected 0 writable controls in {ro_sec}, got {ro_controls}"

                # 2. Ctrl+K search -> matches constraints -> Enter enters constraints -> 0-match feedback -> Esc restores focus
                page.get_by_test_id("settings-sec-appearance").click()
                _assert_focusable(page, "settings-sec-appearance")
                page.keyboard.press("Control+k")
                assert (
                    page.evaluate("() => document.activeElement?.getAttribute('data-testid')")
                    == "settings-search"
                )
                page.get_by_test_id("settings-search").fill("约束")
                expect(page.get_by_test_id("settings-search-results")).to_be_visible()
                expect(page.get_by_test_id("settings-search-item-constraints")).to_be_visible()
                expect(page.get_by_test_id("settings-search-empty")).to_be_hidden()

                page.keyboard.press("Enter")
                expect(page.locator("#settings-panel-constraints")).to_be_visible()
                expect(page.locator("#settings-panel-appearance")).to_be_hidden()

                # Zero-result search feedback and Esc focus restoration back to the trigger button
                _assert_focusable(page, "settings-sec-constraints")
                page.keyboard.press("Control+k")
                assert (
                    page.evaluate("() => document.activeElement?.getAttribute('data-testid')")
                    == "settings-search"
                )
                page.get_by_test_id("settings-search").fill("不存在的设置关键字_xyz")
                expect(page.get_by_test_id("settings-search-empty")).to_be_visible()
                expect(page.get_by_test_id("settings-search-empty")).to_contain_text("未找到")
                expect(page.get_by_test_id("settings-search-results")).to_be_hidden()

                page.keyboard.press("Escape")
                expect(page.get_by_test_id("settings-search-empty")).to_be_hidden()
                assert (
                    page.evaluate("() => document.activeElement?.getAttribute('data-testid')")
                    == "settings-sec-constraints"
                )

                # 3. Jump from current chat session header pill directly to Session Constraints section
                page.get_by_test_id("settings-sec-appearance").click()
                expect(page.locator("#settings-panel-appearance")).to_be_visible()
                page.get_by_test_id("nav-chat").click()
                _assert_focusable(page, "chat-constraints-pill")
                page.get_by_test_id("chat-constraints-pill").click()
                expect(page.locator("#page-settings")).to_be_visible()
                expect(page.locator("#settings-panel-constraints")).to_be_visible()
                expect(page.locator("#settings-panel-appearance")).to_be_hidden()
                expect(page.get_by_test_id("context-revision")).to_contain_text(f"session={sid}")

                # 4. Storage & Retention displays current sid's context.storage_used_bytes
                ctx_json = httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{sid}/context",
                    headers=hdrs,
                    timeout=10.0,
                ).json()
                page.get_by_test_id("settings-sec-storage").click()
                expect(page.locator("#settings-panel-storage")).to_be_visible()
                expect(page.get_by_test_id("storage-quota")).to_contain_text("sessions.sqlite3")
                expect(page.get_by_test_id("storage-quota")).to_contain_text(
                    str(ctx_json["storage_used_bytes"])
                )

                # 5. Modify theme and density, reload, and verify actual computed styles persist
                page.get_by_test_id("settings-sec-appearance").click()
                expect(page.get_by_test_id("appearance-diff-note")).to_contain_text("Diff")
                before_styles = page.evaluate(
                    """() => ({
                        bg: getComputedStyle(document.body).backgroundColor,
                        pad: getComputedStyle(document.querySelector('#settings-panel-appearance')).paddingTop,
                    })"""
                )
                page.get_by_test_id("theme-select").select_option("dark")
                page.get_by_test_id("density-select").select_option("compact")

                page.reload(wait_until="domcontentloaded")
                expect(page.locator("html")).to_have_attribute("data-theme", "dark")
                expect(page.locator("html")).to_have_attribute("data-density", "compact")
                page.get_by_test_id("nav-settings").click()
                assert page.locator("#page-settings .settings-nav-title").count() == 0
                expect(page.locator("#settings-heading")).to_be_visible()
                expect(page.locator("#settings-heading")).to_have_text("设置")
                assert page.locator("#page-settings .settings-main-head .sub").count() == 0
                assert page.locator("#settings-overview-grid .settings-kv-sub").count() == 0
                health_json = httpx.get(
                    f"{srv.base_url}/api/v1/health",
                    headers=hdrs,
                    timeout=10.0,
                ).json()
                ctx_init_json = httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{sid}/context",
                    headers=hdrs,
                    timeout=10.0,
                ).json()
                expect(page.get_by_test_id("settings-overview-desktop")).to_contain_text("关闭窗口")
                expect(page.get_by_test_id("settings-overview-desktop")).to_contain_text("收起到托盘")
                expect(page.get_by_test_id("settings-overview-diff")).to_contain_text("展示位置")
                expect(page.get_by_test_id("settings-overview-diff")).to_contain_text("右侧详情抽屉")
                expected_mode_label = (
                    "已连接"
                    if health_json.get("live_enabled")
                    else ("测试注入" if health_json.get("test_provider_injected") else "未连接")
                )
                expected_model_label = (
                    (health_json.get("model") or "deepseek-flash")
                    if health_json.get("live_enabled")
                    else ("测试替身" if health_json.get("test_provider_injected") else "未配置在线模型")
                )
                expect(page.get_by_test_id("settings-overview-host-mode")).to_contain_text(expected_mode_label)
                expect(page.get_by_test_id("settings-overview-model-val")).to_have_text(expected_model_label)
                expect(page.get_by_test_id("settings-overview-storage-bytes")).to_have_text(
                    f"{ctx_init_json['storage_used_bytes']} 字节"
                )
                assert "live_enabled" not in page.locator("#page-settings").inner_text()
                after_styles = page.evaluate(
                    """() => ({
                        bg: getComputedStyle(document.body).backgroundColor,
                        pad: getComputedStyle(document.querySelector('#settings-panel-appearance')).paddingTop,
                    })"""
                )
                assert after_styles["bg"] != before_styles["bg"]
                assert after_styles["pad"] != before_styles["pad"]

                assert page_errors == []
                assert console_errors == []
                assert non_loopback == []
            finally:
                browser.close()


def test_controlled_result_summary(tmp_path: Path) -> None:
    """Verify controlled metrics demo run card displays metrics.py and 验证通过 matching persisted acceptance.results."""
    cfg = project_helpers.make_project_host_config(tmp_path)
    with LiveTcpTestServer(config=cfg) as srv:
        from forecontract_app.chat_runtime import OfflineReadonlyChatProvider

        ro_provider = OfflineReadonlyChatProvider()

        def _chat_factory(*, project, session_id: str, turn_id: str):
            _ = (project, session_id, turn_id)

            def _p(req):
                if "修复 calc.py" in req.question or "weighted_positive_total" in req.question:
                    return {
                        "action": "execution_offer",
                        "goal": req.question,
                        "summary": "将执行任务",
                    }
                return ro_provider(req)

            return _p

        srv.app.state.service._chat_provider_factory = _chat_factory
        url = f"{srv.base_url}/#token={cfg.access_token}"
        hdrs = auth_headers(cfg)
        page_errors: list[str] = []
        console_errors: list[str] = []
        non_loopback: list[str] = []

        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 800})
                page.on("pageerror", lambda err: page_errors.append(str(err)))
                page.on(
                    "console",
                    lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
                )
                page.on(
                    "request",
                    lambda req: (
                        non_loopback.append(req.url)
                        if not req.url.startswith(srv.base_url)
                        else None
                    ),
                )

                page.goto(url, wait_until="domcontentloaded")
                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id("project-name").fill("ctrl-metrics")
                page.get_by_test_id("create-project").click()
                expect(page.get_by_test_id("current-project")).not_to_have_text("-", timeout=10000)

                if page.get_by_test_id("current-session").inner_text().strip() == "-":
                    page.get_by_test_id("new-session").click()
                expect(page.get_by_test_id("current-session")).not_to_have_text("-", timeout=10000)
                sid = page.get_by_test_id("current-session").inner_text().strip()

                page.get_by_test_id("nav-chat").click()
                page.get_by_test_id("chat-input").fill(
                    "实现 weighted_positive_total 仅汇总 weight >= min_weight 且 value > 0 的权重乘积"
                )
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                page.locator('[data-testid^="offer-confirm-btn-"]').last.click()

                run_card = page.locator('article[data-testid^="timeline-run-"]').first
                expect(run_card).to_be_visible(timeout=15000)
                rid = (run_card.get_attribute("data-testid") or "").removeprefix("timeline-run-")
                expect(page.get_by_test_id(f"timeline-run-state-{rid}")).to_have_attribute(
                    "data-run-state", "succeeded", timeout=30000
                )
                expect(page.get_by_test_id(f"timeline-run-state-{rid}")).to_contain_text(
                    "已完成 · 验证通过", timeout=30000
                )

                # Verify host persisted result.acceptance.results
                run_rec = httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{sid}/runs/{rid}",
                    headers=hdrs,
                    timeout=10.0,
                ).json()
                acc_results = run_rec["result"]["acceptance"]["results"]
                assert any(
                    r.get("command_id") == "pytest_acc"
                    and r.get("status") == "passed"
                    and r.get("exit_code") == 0
                    for r in acc_results
                )

                files_txt = page.get_by_test_id(f"timeline-run-files-{rid}").inner_text()
                checks_txt = page.get_by_test_id(f"timeline-run-checks-{rid}").inner_text()
                assert "metrics.py" in files_txt and "已修改" in files_txt
                assert "待核对" not in files_txt and "changed_paths(" not in files_txt
                assert "验证通过" in checks_txt and "pytest_acc" in checks_txt
                assert "待核对" not in checks_txt and "passed=true" not in checks_txt

                # Evidence drawer shows pytest_acc detail
                page.get_by_test_id(f"timeline-run-diff-{rid}").click()
                expect(page.get_by_test_id("chat-inspector")).to_be_visible()
                page.get_by_test_id("tab-checks").click()
                expect(page.get_by_test_id("coverage-summary")).to_contain_text("pytest_acc")
                expect(page.get_by_test_id("coverage-summary")).to_contain_text("status=passed")
                expect(page.get_by_test_id("coverage-summary")).to_contain_text("exit=0")

                assert page_errors == []
                assert console_errors == []
                assert non_loopback == []
            finally:
                browser.close()
