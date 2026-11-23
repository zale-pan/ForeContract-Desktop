"""C28–C32: Real Playwright Chromium + CLI subprocess tests for continuation, ask_user, restart durability, Job-kill unknown, and compressed context/usage."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import project_helpers
from app_helpers import (
    LiveTcpTestServer,
    make_clean_subprocess_env,
    pick_free_loopback_port,
    tree_hashes,
    wait_until,
)
from forecontract_app.models import ProjectView
from forecontract_app.project_demo import OfflineProjectProvider
from forecontract_llm import ModelRequest, ModelUsage
from playwright.sync_api import expect, sync_playwright
from session_helpers import (
    OwnJobHandle,
    SpyUsageProvider,
    forecast_json,
    start_cli_host_subprocess,
)


def _launch_chromium(pw: Any) -> Any:
    return pw.chromium.launch(headless=True)


def _approve_all_proposals_until_terminal(page: Any, *, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    clicked: set[str] = set()
    while time.monotonic() < deadline:
        state_text = page.get_by_test_id("run-state").text_content()
        if state_text in ("succeeded", "failed", "cancelled", "unknown"):
            return
        pending = page.locator('[data-testid^="proposal-item-"]', has_text="[pending] ")
        count = pending.count()
        for idx in range(count):
            item = pending.nth(idx)
            approve_btn = item.locator('[data-testid^="proposal-approve-"]')
            btn_tid = approve_btn.get_attribute("data-testid")
            if not btn_tid or btn_tid in clicked:
                continue
            clicked.add(btn_tid)
            text = item.text_content() or ""
            if "[pending] command " in text:
                trust = item.locator('[data-testid^="proposal-trust-"]')
                if trust.count() > 0 and not trust.first.is_checked():
                    trust.first.check()
            approve_btn.click()
        time.sleep(0.05)
    raise AssertionError("timed out approving browser proposals")


def test_c28_browser_explicit_continuation_history_and_session_isolation(tmp_path: Path) -> None:
    proj_dir = project_helpers.create_buggy_calc_project(tmp_path / "c28-proj")
    before_sha = project_helpers.sha256_file(proj_dir / "calc.py")
    cfg = project_helpers.make_project_host_config(tmp_path, allowed_roots=(proj_dir,))

    recorded_requests: list[ModelRequest] = []
    round_num = 0

    def _factory(*, project: ProjectView, run_id: str) -> SpyUsageProvider:
        nonlocal round_num
        _ = (project, run_id)
        round_num += 1
        current_round = round_num
        inner = OfflineProjectProvider()

        def _build(idx: int, req: ModelRequest) -> str:
            recorded_requests.append(req)
            if current_round == 2 and idx == 0:
                req_json = req.model_dump_json()
                assert "never touch tests/" in req_json
                assert "Round1 fix calc" in req_json
            body = inner._next_step(req)
            assert body is not None
            return json.dumps(body, ensure_ascii=False)

        return SpyUsageProvider(
            _build,
            usage_fn=lambda i, _r: ModelUsage(
                schema_version="0.1", input_tokens=20 + i, output_tokens=10 + i
            ),
        )

    with LiveTcpTestServer(config=cfg, provider_factory=_factory) as srv:
        if getattr(srv.app.state.service, "_chat_provider_factory", None) is None:
            srv.app.state.service._chat_provider_factory = lambda **_: (lambda req: {"action": "execution_offer", "goal": req.question, "summary": "执行任务"})
        url = f"{srv.base_url}/#token={cfg.access_token}"
        console_errors: list[str] = []
        page_errors: list[str] = []
        non_loopback: list[str] = []
        run_post_events: list[str] = []

        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            try:
                page = browser.new_page()
                page.on(
                    "console",
                    lambda m: console_errors.append(m.text) if m.type == "error" else None,
                )
                page.on("pageerror", lambda err: page_errors.append(str(err)))

                def _on_request(req: Any) -> None:
                    if not req.url.startswith(f"http://127.0.0.1:{cfg.bind_port}/"):
                        non_loopback.append(req.url)
                    if req.method == "POST" and req.url.endswith(("/runs", "/confirm")):
                        run_post_events.append(req.url)

                page.on("request", _on_request)
                page.goto(url, wait_until="domcontentloaded")
                expect(page.get_by_test_id("host-status-banner")).to_be_visible()

                # Register project
                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id("project-name").fill("c28-proj")
                page.get_by_test_id("register-root").fill(str(proj_dir))
                page.get_by_test_id("confirm-read").check()
                page.get_by_test_id("register-project").click()
                expect(page.get_by_test_id("project-list")).to_contain_text("c28-proj")
                page.get_by_test_id("nav-chat").click()

                # Start explicit Round 1
                page.get_by_test_id("chat-input").fill("Round1 fix calc")
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                page.locator('[data-testid^="offer-confirm-btn-"]').last.click()

                _approve_all_proposals_until_terminal(page)
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-state")).to_have_text("succeeded", timeout=15000)
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("verification")).to_have_text("passed")
                assert len(run_post_events) == 1
                assert project_helpers.sha256_file(proj_dir / "calc.py") != before_sha

                # Reload -> ZERO automatic POST /runs, history item restored cleanly
                run_posts_before_reload = len(run_post_events)
                page.reload(wait_until="domcontentloaded")
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-state")).to_have_text("succeeded", timeout=10000)
                page.get_by_test_id("nav-history").click()
                expect(page.get_by_test_id("session-history")).to_contain_text("Round1 fix calc")
                expect(page.get_by_test_id("session-history")).to_contain_text("run_result")
                page.wait_for_timeout(300)
                assert len(run_post_events) == run_posts_before_reload

                # Capture session 1 id from session-list item testid
                page.get_by_test_id("nav-projects").click()
                s1_item = page.locator('[data-testid^="session-item-"]').first
                s1_testid = s1_item.get_attribute("data-testid")
                assert s1_testid is not None
                sid1 = s1_testid.removeprefix("session-item-")

                # Set durable constraints & run explicit Round 2
                page.get_by_test_id("nav-settings").click()
                page.get_by_test_id("settings-sec-constraints").click()
                page.get_by_test_id("constraints-input").fill("never touch tests/")
                page.get_by_test_id("save-constraints").click()
                expect(page.get_by_test_id("context-revision")).to_contain_text("rev=1")

                page.get_by_test_id("nav-chat").click()
                r1_id = page.get_by_test_id("run-id").text_content() or ""
                page.get_by_test_id("chat-input").fill("Round2 verify clean")
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                page.locator('[data-testid^="offer-confirm-btn-"]').last.click()
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-id")).not_to_have_text(r1_id, timeout=10000)
                _approve_all_proposals_until_terminal(page)
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-state")).to_have_text("succeeded", timeout=15000)
                expect(page.get_by_test_id("session-history")).to_contain_text("Round1 fix calc")
                expect(page.get_by_test_id("session-history")).to_contain_text("Round2 verify clean")
                assert len(run_post_events) == 2

                # Create second session -> isolated empty history & rev=0
                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id("new-session").click()
                expect(page.get_by_test_id("context-revision")).to_contain_text("rev=0")
                page.get_by_test_id("nav-chat").click()
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-state")).to_have_text("idle")
                assert page.locator('[data-testid^="history-item-"]').count() == 0

                # Switch back to Session 1 -> restores Round 1 + Round 2 & constraints without triggering POST /runs
                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id(f"session-item-{sid1}").click()
                expect(page.get_by_test_id("context-revision")).to_contain_text("rev=1")
                expect(page.get_by_test_id("session-history")).to_contain_text("Round2 verify clean")
                assert page.get_by_test_id("constraints-input").input_value() == "never touch tests/"
                assert len(run_post_events) == 2

                assert console_errors == []
                assert page_errors == []
                assert non_loopback == []
            finally:
                browser.close()


def test_c29_browser_ask_user_reload_recovery_free_text_and_approval_boundary(
    tmp_path: Path,
) -> None:
    proj_dir = project_helpers.create_buggy_calc_project(tmp_path / "c29-proj")
    before_calc = (proj_dir / "calc.py").read_text(encoding="utf-8")
    cfg = project_helpers.make_project_host_config(tmp_path, allowed_roots=(proj_dir,))

    seen_answer_in_prompt = False
    mode = "answer"

    def _factory(*, project: ProjectView, run_id: str) -> SpyUsageProvider:
        _ = (project, run_id)
        inner = OfflineProjectProvider()

        def _build(idx: int, req: ModelRequest) -> str:
            nonlocal seen_answer_in_prompt
            if idx == 0:
                return forecast_json(
                    action_type="ask_user",
                    payload={
                        "question": "Choose fix mode?",
                        "options": ["inline add", "helper function"],
                    },
                )
            if mode == "cancel":
                raise AssertionError("model should never advance without user answer")
            if "FREE TEXT OVERRIDE" in req.model_dump_json():
                seen_answer_in_prompt = True
            body = inner._next_step(req)
            assert body is not None
            return json.dumps(body, ensure_ascii=False)

        return SpyUsageProvider(_build)

    with LiveTcpTestServer(config=cfg, provider_factory=_factory) as srv:
        if getattr(srv.app.state.service, "_chat_provider_factory", None) is None:
            srv.app.state.service._chat_provider_factory = lambda **_: (lambda req: {"action": "execution_offer", "goal": req.question, "summary": "执行任务"})
        url = f"{srv.base_url}/#token={cfg.access_token}"
        console_errors: list[str] = []
        page_errors: list[str] = []
        non_loopback: list[str] = []

        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            try:
                page = browser.new_page()
                page.on(
                    "console",
                    lambda m: console_errors.append(m.text) if m.type == "error" else None,
                )
                page.on("pageerror", lambda err: page_errors.append(str(err)))
                page.on(
                    "request",
                    lambda req: (
                        non_loopback.append(req.url)
                        if not req.url.startswith(f"http://127.0.0.1:{cfg.bind_port}/")
                        else None
                    ),
                )
                page.goto(url, wait_until="domcontentloaded")

                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id("project-name").fill("c29-proj")
                page.get_by_test_id("register-root").fill(str(proj_dir))
                page.get_by_test_id("confirm-read").check()
                page.get_by_test_id("register-project").click()
                expect(page.get_by_test_id("project-list")).to_contain_text("c29-proj")
                page.get_by_test_id("nav-chat").click()

                page.get_by_test_id("chat-input").fill("Fix with interactive ask_user")
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                page.locator('[data-testid^="offer-confirm-btn-"]').last.click()

                q_card = page.locator('[data-testid^="question-item-"]').first
                expect(q_card).to_be_visible(timeout=15000)
                q_testid = q_card.get_attribute("data-testid")
                assert q_testid is not None
                qid = q_testid.removeprefix("question-item-")
                expect(q_card).to_contain_text("Choose fix mode?")
                expect(page.get_by_test_id(f"question-option-{qid}-0")).to_be_visible()
                expect(page.get_by_test_id(f"question-option-{qid}-1")).to_be_visible()

                # Before user answers, calc.py stays untouched
                assert (proj_dir / "calc.py").read_text(encoding="utf-8") == before_calc

                # Reload page -> same qid and question card restored cleanly
                page.reload(wait_until="domcontentloaded")
                q_after_reload = page.get_by_test_id(f"question-item-{qid}")
                expect(q_after_reload).to_be_visible(timeout=10000)
                expect(q_after_reload).to_contain_text("[pending]")

                # Submit arbitrary free-text even though options exist
                page.get_by_test_id(f"question-answer-input-{qid}").fill("FREE TEXT OVERRIDE")
                page.get_by_test_id(f"question-submit-{qid}").click()
                expect(q_after_reload).to_contain_text("[answered]")

                # Still requires explicit scope/verification/command approvals before modifying & succeeding
                _approve_all_proposals_until_terminal(page)
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-state")).to_have_text("succeeded", timeout=15000)
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("verification")).to_have_text("passed")
                assert seen_answer_in_prompt is True
                assert "return a + b" in (proj_dir / "calc.py").read_text(encoding="utf-8")

                # Second session: blocked on ask_user, cancel ends cleanly without proceeding
                mode = "cancel"
                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id("new-session").click()
                page.get_by_test_id("nav-chat").click()
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-state")).to_have_text("idle")
                page.get_by_test_id("chat-input").fill("Cancel while waiting answer")
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                page.locator('[data-testid^="offer-confirm-btn-"]').last.click()
                q2 = page.locator('[data-testid^="question-item-"]').first
                expect(q2).to_be_visible(timeout=15000)
                page.get_by_test_id("cancel-run").click()
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-state")).to_have_text("cancelled", timeout=15000)
                expect(q2).to_contain_text("[cancelled]")

                assert console_errors == []
                assert page_errors == []
                assert non_loopback == []
            finally:
                browser.close()


def test_c30_cli_exclusive_restart_preserves_durable_session_state(tmp_path: Path) -> None:
    policy_before = asyncio.get_event_loop_policy()
    data_root = tmp_path / "cli_data"
    ws_parent = tmp_path / "cli_ws"
    data_root.mkdir()
    ws_parent.mkdir()
    port1 = pick_free_loopback_port()

    proc1 = None
    proc4 = None
    try:
        proc1, base1, token1, _ = start_cli_host_subprocess(
            data_root=data_root, workspace_parent=ws_parent, port=port1
        )
        headers1 = {
            "Host": f"127.0.0.1:{port1}",
            "Authorization": f"Bearer {token1}",
            "Origin": base1,
        }

        # Second process with SAME data_root must exit non-zero and create no project
        ws_other = tmp_path / "cli_ws_other"
        ws_other.mkdir()
        proc2 = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "forecontract_app",
                "--data-root",
                str(data_root),
                "--workspace-parent",
                str(ws_other),
                "--port",
                str(pick_free_loopback_port()),
                "--offline",
            ],
            capture_output=True,
            text=True,
            env=make_clean_subprocess_env(),
            timeout=10,
            check=False,
        )
        assert proc2.returncode != 0
        assert not any(ws_other.iterdir())

        # Occupied port must also exit non-zero without polluting data_root2
        data_root2 = tmp_path / "cli_data2"
        ws_parent2 = tmp_path / "cli_ws2"
        data_root2.mkdir()
        ws_parent2.mkdir()
        proc3 = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "forecontract_app",
                "--data-root",
                str(data_root2),
                "--workspace-parent",
                str(ws_parent2),
                "--port",
                str(port1),
                "--offline",
            ],
            capture_output=True,
            text=True,
            env=make_clean_subprocess_env(),
            timeout=10,
            check=False,
        )
        assert proc3.returncode != 0
        assert not any(ws_parent2.iterdir())

        # Reject unknown flags like --live cleanly
        proc_live = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "forecontract_app",
                "--data-root",
                str(data_root2),
                "--workspace-parent",
                str(ws_parent2),
                "--port",
                str(pick_free_loopback_port()),
                "--live",
            ],
            capture_output=True,
            text=True,
            env=make_clean_subprocess_env(),
            timeout=10,
            check=False,
        )
        assert proc_live.returncode != 0

        # Create real project, session, run via proc1 before graceful stop
        proj_resp = httpx.post(
            f"{base1}/api/v1/projects", headers=headers1, json={"name": "cli-proj"}, timeout=5.0
        )
        assert proj_resp.status_code == 201
        proj = proj_resp.json()
        sess = httpx.post(
            f"{base1}/api/v1/sessions",
            headers=headers1,
            json={
                "project_id": proj["project_id"],
                "model": {"provider": "offline", "model": "offline-metrics-v1"},
            },
            timeout=5.0,
        ).json()
        old_sid = sess["session_id"]
        run = httpx.post(
            f"{base1}/api/v1/sessions/{old_sid}/runs",
            headers=headers1,
            json={
                "client_request_id": "11111111-1111-4111-8111-111111111111",
                "goal": "cli run",
                "confirmed_scope": True,
            },
            timeout=5.0,
        ).json()
        old_rid = run["run_id"]
        term = wait_until(
            lambda: (
                res := httpx.get(
                    f"{base1}/api/v1/sessions/{old_sid}/runs/{old_rid}",
                    headers={"Host": f"127.0.0.1:{port1}", "Authorization": f"Bearer {token1}"},
                    timeout=5.0,
                ).json()
            )["state"]
            in ("succeeded", "failed", "cancelled", "unknown")
            and res
        )
        assert term["state"] == "succeeded"
        assert term["verification"] == "passed"

        ws_before_restart = tree_hashes(ws_parent / "cli-proj")
        runs_before_restart = tree_hashes(data_root / "runs")

        # Graceful shutdown via --control-stdin
        assert proc1.stdin is not None
        t0 = time.monotonic()
        proc1.stdin.write("stop\n")
        proc1.stdin.flush()
        rc1 = proc1.wait(timeout=8.5)
        graceful_elapsed = time.monotonic() - t0
        assert rc1 == 0
        assert graceful_elapsed <= 8.5
        proc1 = None

        # C30 replaces old restart-404: restart on same data_root returns 200 with durable state & old token rejected
        port4 = pick_free_loopback_port()
        proc4, base4, token4, _ = start_cli_host_subprocess(
            data_root=data_root, workspace_parent=ws_parent, port=port4
        )
        h4_old = {"Host": f"127.0.0.1:{port4}", "Authorization": f"Bearer {token1}"}
        assert httpx.get(f"{base4}/api/v1/sessions", headers=h4_old, timeout=5.0).status_code == 401

        h4_get = {"Host": f"127.0.0.1:{port4}", "Authorization": f"Bearer {token4}"}
        sess_list = httpx.get(f"{base4}/api/v1/sessions", headers=h4_get, timeout=5.0)
        assert sess_list.status_code == 200
        assert [s["session_id"] for s in sess_list.json()] == [old_sid]

        old_get = httpx.get(
            f"{base4}/api/v1/sessions/{old_sid}/runs/{old_rid}", headers=h4_get, timeout=5.0
        )
        assert old_get.status_code == 200
        old_data = old_get.json()
        assert old_data["run_id"] == old_rid
        assert old_data["state"] == "succeeded"
        assert old_data["verification"] == "passed"

        hist_get = httpx.get(
            f"{base4}/api/v1/sessions/{old_sid}/history", headers=h4_get, timeout=5.0
        )
        assert hist_get.status_code == 200
        assert len(hist_get.json()["items"]) == 2

        assert tree_hashes(ws_parent / "cli-proj") == ws_before_restart
        assert tree_hashes(data_root / "runs") == runs_before_restart

        assert proc4.stdin is not None
        proc4.stdin.write("stop\n")
        proc4.stdin.flush()
        assert proc4.wait(timeout=8.5) == 0
        proc4 = None
        assert asyncio.get_event_loop_policy() is policy_before
    finally:
        for p in (proc1, proc4):
            if p is not None and p.poll() is None:
                p.kill()
                p.wait(timeout=5.0)


def test_c31_real_job_kill_recovery_in_browser(tmp_path: Path) -> None:
    proj_dir = project_helpers.create_buggy_calc_project(tmp_path / "c31-proj")
    calc_before_sha = project_helpers.sha256_file(proj_dir / "calc.py")
    data_root = tmp_path / "data"
    ws_parent = tmp_path / "workspaces"
    data_root.mkdir()
    ws_parent.mkdir()

    proc1: subprocess.Popen[str] | None = None
    proc2: subprocess.Popen[str] | None = None
    job1: OwnJobHandle | None = None
    try:
        proc1, base1, token1, port1 = start_cli_host_subprocess(
            data_root=data_root,
            workspace_parent=ws_parent,
            project_roots=(proj_dir,),
        )
        job1 = OwnJobHandle(proc1)
        h1 = {
            "Host": f"127.0.0.1:{port1}",
            "Authorization": f"Bearer {token1}",
            "Origin": base1,
        }
        reg = httpx.post(
            f"{base1}/api/v1/projects/register",
            headers=h1,
            json={"name": "c31-proj", "root": str(proj_dir), "confirmed_read": True},
            timeout=5.0,
        )
        assert reg.status_code == 201
        pid = reg.json()["project_id"]
        sess = httpx.post(
            f"{base1}/api/v1/sessions",
            headers=h1,
            json={
                "project_id": pid,
                "model": {"provider": "offline", "model": "offline-project-v1", "thinking": "disabled"},
            },
            timeout=5.0,
        ).json()
        sid = sess["session_id"]

        run = httpx.post(
            f"{base1}/api/v1/sessions/{sid}/runs",
            headers=h1,
            json={"client_request_id": str(uuid.uuid4()), "goal": "Fix calc under kill", "confirmed_scope": True},
            timeout=5.0,
        ).json()
        rid = run["run_id"]

        # Wait until run reaches real active "running" state waiting on scope proposal
        wait_until(
            lambda: len(
                httpx.get(
                    f"{base1}/api/v1/sessions/{sid}/runs/{rid}/proposals",
                    headers={"Host": f"127.0.0.1:{port1}", "Authorization": f"Bearer {token1}"},
                    timeout=5.0,
                ).json()
            )
            >= 1,
            timeout=15.0,
        )
        runs_before_kill = tree_hashes(data_root / "runs")

        # Hard-kill ONLY our spawned CLI subprocess Job Object while run is active
        job1.terminate_tree()
        job1 = None
        assert proc1.poll() is not None
        proc1 = None

        # Start fresh CLI host on same data_root
        proc2, base2, token2, port2 = start_cli_host_subprocess(
            data_root=data_root,
            workspace_parent=ws_parent,
            project_roots=(proj_dir,),
        )

        url2 = f"{base2}/#token={token2}"
        run_post_events: list[str] = []
        page_errors: list[str] = []
        non_loopback: list[str] = []

        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            try:
                page = browser.new_page()
                page.on("pageerror", lambda err: page_errors.append(str(err)))

                def _on_req(req: Any) -> None:
                    if not req.url.startswith(f"http://127.0.0.1:{port2}/"):
                        non_loopback.append(req.url)
                    if req.method == "POST" and req.url.endswith(("/runs", "/confirm")):
                        run_post_events.append(req.url)

                page.on("request", _on_req)
                page.goto(url2, wait_until="domcontentloaded")
                expect(page.get_by_test_id("run-state")).to_have_text("unknown", timeout=15000)
                expect(page.get_by_test_id("reason-code")).to_have_text("HOST_RESTARTED")
                expect(page.get_by_test_id("session-notice")).to_contain_text("HOST_RESTARTED")
                page.wait_for_timeout(300)

                assert run_post_events == []
                assert page_errors == []
                assert non_loopback == []
            finally:
                browser.close()

        assert project_helpers.sha256_file(proj_dir / "calc.py") == calc_before_sha
        for rel, sha in runs_before_kill.items():
            assert (data_root / "runs" / rel).exists()
            assert project_helpers.sha256_file(data_root / "runs" / rel) == sha

        assert proc2.stdin is not None
        proc2.stdin.write("stop\n")
        proc2.stdin.flush()
        assert proc2.wait(timeout=8.5) == 0
        proc2 = None
    finally:
        if job1 is not None:
            job1.terminate_tree()
        for p in (proc1, proc2):
            if p is not None and p.poll() is None:
                p.kill()
                p.wait(timeout=5.0)


def test_c32_browser_compressed_context_usage_quota_and_no_delete_ui(tmp_path: Path) -> None:
    proj_dir = project_helpers.create_buggy_calc_project(tmp_path / "c32-proj")
    cfg = project_helpers.make_project_host_config(tmp_path, allowed_roots=(proj_dir,))

    call_counter = 0

    def _factory(*, project: ProjectView, run_id: str) -> SpyUsageProvider:
        _ = (project, run_id)

        def _build(_idx: int, _req: ModelRequest) -> str:
            return forecast_json(
                action_type="inspect_file",
                payload={"path": "calc.py"},
                outcome_type="project_tool_result",
                outcome_payload={"action_type": "inspect_file", "require_success": False},
                task_hint=True,
            )

        def _usage(_idx: int, _req: ModelRequest) -> ModelUsage:
            nonlocal call_counter
            call_counter += 1
            if call_counter == 1:
                return ModelUsage(schema_version="0.1", input_tokens=42, output_tokens=None)
            return ModelUsage(schema_version="0.1", input_tokens=10, output_tokens=5)

        return SpyUsageProvider(_build, usage_fn=_usage)

    xss_constraint = "<script>window.__xss=1</script> preserve budget"
    with LiveTcpTestServer(config=cfg, provider_factory=_factory) as srv:
        if getattr(srv.app.state.service, "_chat_provider_factory", None) is None:
            srv.app.state.service._chat_provider_factory = lambda **_: (lambda req: {"action": "execution_offer", "goal": req.question, "summary": "执行任务"})
        headers = {
            "Host": f"127.0.0.1:{cfg.bind_port}",
            "Authorization": f"Bearer {cfg.access_token}",
            "Origin": srv.base_url,
        }
        with httpx.Client(base_url=srv.base_url, timeout=10.0) as client:
            reg = client.post(
                "/api/v1/projects/register",
                headers=headers,
                json={"name": "c32-proj", "root": str(proj_dir), "confirmed_read": True},
            )
            assert reg.status_code == 201
            pid = reg.json()["project_id"]

            s1 = client.post(
                "/api/v1/sessions",
                headers=headers,
                json={
                    "project_id": pid,
                    "model": {"provider": "offline", "model": "offline-project-v1", "thinking": "disabled"},
                },
            ).json()["session_id"]
            s2 = client.post(
                "/api/v1/sessions",
                headers=headers,
                json={
                    "project_id": pid,
                    "model": {"provider": "offline", "model": "offline-project-v1", "thinking": "disabled"},
                },
            ).json()["session_id"]

            put_ctx = client.put(
                f"/api/v1/sessions/{s1}/context",
                headers=headers,
                json={
                    "client_request_id": str(uuid.uuid4()),
                    "expected_revision": 0,
                    "constraints": xss_constraint,
                },
            )
            assert put_ctx.status_code == 200

            # Seed > 16 KiB across 6 large rounds on s1
            for i in range(6):
                big_goal = f"round-{i + 1}-" + ("x" * 3500)
                rr = client.post(
                    f"/api/v1/sessions/{s1}/runs",
                    headers=headers,
                    json={
                        "client_request_id": str(uuid.uuid4()),
                        "goal": big_goal,
                        "confirmed_scope": True,
                    },
                )
                assert rr.status_code == 202
                rid = rr.json()["run_id"]
                wait_until(
                    lambda target_rid=rid: (
                        res := client.get(
                            f"/api/v1/sessions/{s1}/runs/{target_rid}", headers=headers
                        ).json()
                    )["state"]
                    in ("succeeded", "failed", "cancelled", "unknown")
                    and res
                )

            # Reset s1's active first run with unknown output_tokens to be the latest usage
            # Note: call_counter == 1 happened on round 1; let's inspect round 1 or verify s1 latest run usage
            # Actually let's check both round 1 via API and add one unknown run via call_counter reset:
            call_counter = 0
            rr_last = client.post(
                f"/api/v1/sessions/{s1}/runs",
                headers=headers,
                json={
                    "client_request_id": str(uuid.uuid4()),
                    "goal": "final unknown usage check",
                    "confirmed_scope": True,
                },
            )
            assert rr_last.status_code == 202
            last_rid = rr_last.json()["run_id"]
            wait_until(
                lambda: client.get(
                    f"/api/v1/sessions/{s1}/runs/{last_rid}", headers=headers
                ).json()["state"]
                in ("succeeded", "failed", "cancelled", "unknown")
            )

        url = f"{srv.base_url}/#token={cfg.access_token}"
        console_errors: list[str] = []
        page_errors: list[str] = []
        non_loopback: list[str] = []
        run_post_events: list[str] = []

        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            try:
                page = browser.new_page()
                page.on(
                    "console",
                    lambda m: console_errors.append(m.text) if m.type == "error" else None,
                )
                page.on("pageerror", lambda err: page_errors.append(str(err)))

                def _on_req(req: Any) -> None:
                    if not req.url.startswith(f"http://127.0.0.1:{cfg.bind_port}/"):
                        non_loopback.append(req.url)
                    if req.method == "POST" and req.url.endswith(("/runs", "/confirm")):
                        run_post_events.append(req.url)

                page.on("request", _on_req)
                page.goto(url, wait_until="domcontentloaded")
                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id(f"session-item-{s1}").click()

                page.get_by_test_id("nav-settings").click()
                page.get_by_test_id("settings-sec-constraints").click()
                expect(page.get_by_test_id("constraints-input")).to_have_value(
                    xss_constraint, timeout=10000
                )
                assert page.evaluate("() => window.__xss || null") is None
                hist_items: list[dict[str, object]] = []
                after_seq = 0
                while True:
                    h_page = httpx.get(
                        f"{srv.base_url}/api/v1/sessions/{s1}/history",
                        headers=headers,
                        params={"after": after_seq, "limit": 50},
                        timeout=5.0,
                    ).json()
                    page_list = h_page.get("items") or []
                    hist_items.extend(page_list)
                    if not h_page.get("has_more") or not page_list:
                        break
                    after_seq = int(h_page["cursor"])
                actual_hist_bytes = sum(len(str(it.get("text") or "").encode("utf-8")) for it in hist_items)
                summary_text = page.get_by_test_id("context-summary").text_content() or ""
                assert "16 KiB" not in summary_text
                assert f"原文累计约 {actual_hist_bytes}B" in summary_text
                assert "omitted_count" in summary_text
                assert "any_item_truncated=" in summary_text
                expect(page.get_by_test_id("session-history")).to_contain_text("final unknown usage check")

                usage_text = page.get_by_test_id("usage-summary").text_content() or ""
                assert "unknown" in usage_text
                assert "offline_not_billed" in usage_text
                assert "estimated=null billed=null" in usage_text

                page.get_by_test_id("settings-sec-storage").click()
                ctx_json = httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{s1}/context",
                    headers=headers,
                    timeout=5.0,
                ).json()
                quota_text = page.get_by_test_id("storage-quota").text_content() or ""
                assert str(ctx_json["storage_used_bytes"]) in quota_text
                assert "sessions.sqlite3" in quota_text

                # Verify zero delete/GC/VACUUM controls in DOM
                forbidden_tokens = ("delete", "purge", "vacuum", "删除", "清空", "清理")
                buttons_json = page.evaluate(
                    "() => Array.from(document.querySelectorAll('button')).map(b => (b.textContent || '') + ' ' + (b.getAttribute('data-testid') || ''))"
                )
                for label in json.loads(json.dumps(buttons_json)):
                    lowered = str(label).lower()
                    for tok in forbidden_tokens:
                        assert tok not in lowered, f"forbidden control found: {label}"

                # Switch to s2, back to s1, and reload -> ZERO POST /runs triggered
                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id(f"session-item-{s2}").click()
                expect(page.get_by_test_id("context-revision")).to_contain_text("rev=0")
                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id(f"session-item-{s1}").click()
                expect(page.get_by_test_id("context-revision")).to_contain_text("rev=1")
                page.reload(wait_until="domcontentloaded")
                expect(page.get_by_test_id("host-status-banner")).to_be_visible()
                page.wait_for_timeout(200)

                assert run_post_events == []
                assert console_errors == []
                assert page_errors == []
                assert non_loopback == []
            finally:
                browser.close()
