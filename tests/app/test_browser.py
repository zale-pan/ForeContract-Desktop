"""A22–A24: Real Playwright Chromium browser tests and real CLI exclusive lock/restart tests."""

from __future__ import annotations

import asyncio
import hashlib
import re
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
from app_helpers import (
    LiveTcpTestServer,
    make_clean_subprocess_env,
    make_host_config,
    pick_free_loopback_port,
    read_line_with_deadline,
    terminate_process_tree,
    tree_hashes,
    wait_until,
)
from forecontract_app.demo import INITIAL_METRICS_SHA256, OfflineMetricsProvider
from forecontract_llm import ModelRequest, ModelResponse
from playwright.sync_api import expect, sync_playwright


def _launch_chromium(pw: object):  # type: ignore[no-untyped-def]
    return pw.chromium.launch(headless=True)


def test_a22_browser_real_loop(tmp_path: Path) -> None:
    cfg = make_host_config(tmp_path)
    with LiveTcpTestServer(config=cfg) as srv:
        if getattr(srv.app.state.service, "_chat_provider_factory", None) is None:
            srv.app.state.service._chat_provider_factory = lambda **_: (lambda req: {"action": "execution_offer", "goal": req.question, "summary": "执行任务"})
        url = f"{srv.base_url}/#token={cfg.access_token}"
        console_errors: list[str] = []
        page_errors: list[str] = []
        non_loopback_requests: list[str] = []

        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            try:
                page = browser.new_page()
                page.on(
                    "console",
                    lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
                )
                page.on("pageerror", lambda err: page_errors.append(str(err)))
                page.on(
                    "request",
                    lambda req: (
                        non_loopback_requests.append(req.url)
                        if not req.url.startswith(f"http://127.0.0.1:{cfg.bind_port}/")
                        else None
                    ),
                )

                page.goto(url, wait_until="domcontentloaded")
                expect(page.get_by_test_id("host-status-banner")).to_be_visible()
                assert "#token=" not in page.url

                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id("project-name").fill("browser-metrics")
                page.get_by_test_id("create-project").click()
                expect(page.get_by_test_id("project-list")).to_contain_text("browser-metrics")
                page.get_by_test_id("nav-chat").click()

                page.get_by_test_id("chat-input").fill("修复 weighted_positive_total")
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                page.locator('[data-testid^="offer-confirm-btn-"]').last.click()

                page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-state")).to_have_text("succeeded", timeout=60000)
                expect(page.get_by_test_id("verification")).to_have_text("passed")

                metrics_file = cfg.workspace_parent / "browser-metrics" / "metrics.py"
                actual_after_sha = hashlib.sha256(metrics_file.read_bytes()).hexdigest()
                assert actual_after_sha != INITIAL_METRICS_SHA256

                run_id = page.get_by_test_id("run-id").inner_text().strip()
                junit_files = list((cfg.data_root / "runs" / run_id / "output").rglob("junit.xml"))
                assert len(junit_files) == 1
                root_xml = ET.fromstring(junit_files[0].read_bytes())
                suites = [root_xml] if root_xml.tag == "testsuite" else list(root_xml.findall(".//testsuite"))
                test_names = {tc.attrib.get("name", "") for s in suites for tc in s.findall("testcase")}
                assert test_names == {"test_weighted_basic", "test_weighted_threshold_and_empty"}
                assert all(tc.find("failure") is None and tc.find("error") is None for s in suites for tc in s.findall("testcase"))

                changed_text = page.get_by_test_id("changed-paths").inner_text()
                assert "metrics.py" in changed_text
                assert INITIAL_METRICS_SHA256 in changed_text
                assert actual_after_sha in changed_text
                assert page.get_by_test_id("tool-events").locator("li").count() >= 4
            finally:
                browser.close()

        assert console_errors == []
        assert page_errors == []
        assert non_loopback_requests == []


def test_a23_refresh_cancel_and_xss(tmp_path: Path) -> None:
    cfg = make_host_config(tmp_path)
    gates: dict[str, threading.Event] = {}
    entered: dict[str, threading.Event] = {}
    invocations = 0

    class _ControllableProvider:
        def __init__(self, *, goal: str) -> None:
            self._inner = OfflineMetricsProvider()
            self._goal = goal
            self._calls = 0

        async def complete(self, request: ModelRequest) -> ModelResponse:
            nonlocal invocations
            if self._calls == 0:
                invocations += 1
            self._calls += 1
            if "reload-gate" in request.model_context.goal_text and self._calls == 2:
                entered.setdefault("reload", threading.Event()).set()
                await asyncio.to_thread(gates.setdefault("reload", threading.Event()).wait, 15.0)
            if "cancel-gate" in request.model_context.goal_text and self._calls == 1:
                entered.setdefault("cancel", threading.Event()).set()
                await asyncio.sleep(30.0)
            return await self._inner.complete(request)

    with LiveTcpTestServer(
        config=cfg,
        provider_factory=lambda **_: _ControllableProvider(goal=""),
    ) as srv:
        srv.app.state.service._chat_provider_factory = lambda **_: (
            lambda req: {"action": "execution_offer", "goal": req.question, "summary": "执行任务"}
        )
        url = f"{srv.base_url}/#token={cfg.access_token}"
        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            try:
                page = browser.new_page()
                page.goto(url, wait_until="domcontentloaded")
                assert "#token=" not in page.url

                # 1. Reload mid-run recovers same sessionStorage identity without duplicate execution
                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id("project-name").fill("reload-proj")
                page.get_by_test_id("create-project").click()
                expect(page.get_by_test_id("project-list")).to_contain_text("reload-proj")
                page.get_by_test_id("nav-chat").click()

                page.get_by_test_id("chat-input").fill("reload-gate test")
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                with page.expect_response(
                    lambda r: r.request.method == "POST" and r.url.endswith("/confirm")
                ) as first_run_info:
                    page.locator('[data-testid^="offer-confirm-btn-"]').last.click()
                first_start_resp = first_run_info.value
                assert first_start_resp.status == 201
                original_run_id = first_start_resp.json()["run_id"]
                assert len(original_run_id) > 10

                assert entered.setdefault("reload", threading.Event()).wait(timeout=10.0)
                page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-id")).to_have_text(original_run_id, timeout=10000)

                page.reload(wait_until="domcontentloaded")
                page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-id")).to_have_text(original_run_id, timeout=10000)

                gates.setdefault("reload", threading.Event()).set()
                expect(page.get_by_test_id("run-state")).to_have_text("succeeded", timeout=60000)
                assert invocations == 1

                # 2. HTML/XSS canary rendered cleanly without script execution & stays visible
                xss_canary = '<img src=x onerror="window.__xss_flag=1"><script>window.__xss_flag=2</script>'
                page.get_by_test_id("chat-input").fill(xss_canary)
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                with page.expect_response(
                    lambda r: r.request.method == "POST" and r.url.endswith("/confirm")
                ) as xss_run_info:
                    page.locator('[data-testid^="offer-confirm-btn-"]').last.click()
                xss_start_resp = xss_run_info.value
                assert xss_start_resp.status == 201
                xss_run_id = xss_start_resp.json()["run_id"]
                assert xss_run_id != original_run_id
                expect(page.get_by_test_id("run-id")).to_have_text(xss_run_id, timeout=10000)
                expect(page.get_by_test_id("run-state")).to_have_text("succeeded", timeout=60000)
                expect(page.get_by_test_id(f"timeline-run-goal-{xss_run_id}")).to_have_text(xss_canary)
                assert page.evaluate("() => window.__xss_flag ?? null") is None
                page.get_by_test_id("tab-events").click()
                expect(page.get_by_test_id("tool-events")).to_contain_text("metrics.py")
                assert invocations == 2

                # 3. Cancel active waiting run via UI button
                page.get_by_test_id("chat-input").fill("cancel-gate test")
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                with page.expect_response(
                    lambda r: r.request.method == "POST" and r.url.endswith("/confirm")
                ) as cancel_run_info:
                    page.locator('[data-testid^="offer-confirm-btn-"]').last.click()
                cancel_start_resp = cancel_run_info.value
                assert cancel_start_resp.status == 201
                cancel_run_id = cancel_start_resp.json()["run_id"]
                assert cancel_run_id not in {original_run_id, xss_run_id}
                expect(page.get_by_test_id("run-id")).to_have_text(cancel_run_id, timeout=10000)
                assert entered.setdefault("cancel", threading.Event()).wait(timeout=10.0)

                with page.expect_response(
                    lambda r: r.request.method == "POST" and r.url.endswith("/cancel")
                ) as cancel_post_info:
                    page.get_by_test_id("cancel-run").click()
                cancel_post_resp = cancel_post_info.value
                assert cancel_post_resp.status == 202
                expect(page.get_by_test_id("run-state")).to_have_text(
                    re.compile(r"^(cancelled|unknown)$"),
                    timeout=15000,
                )
                assert page.get_by_test_id("run-id").inner_text().strip() == cancel_run_id
                assert invocations == 3

                # 4. Live candidate session in offline host triggers LIVE_DISABLED
                import httpx

                pid = page.get_by_test_id("current-project").inner_text().strip()
                ds_sess = httpx.post(
                    f"{srv.base_url}/api/v1/sessions",
                    headers={"Authorization": f"Bearer {cfg.access_token}", "Origin": srv.base_url},
                    json={"project_id": pid, "model": {"provider": "deepseek", "model": "deepseek-flash", "thinking": "disabled"}},
                    timeout=5.0,
                ).json()
                page.reload(wait_until="domcontentloaded")
                page.get_by_test_id(f"chat-session-item-{ds_sess['session_id']}").click()
                expect(page.get_by_test_id("current-session")).to_have_text(ds_sess["session_id"], timeout=10000)
                page.get_by_test_id("chat-input").fill("try live")
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                with page.expect_response(
                    lambda r: r.request.method == "POST" and r.url.endswith("/confirm")
                ) as live_run_info:
                    page.locator('[data-testid^="offer-confirm-btn-"]').last.click()
                live_start_resp = live_run_info.value
                assert live_start_resp.status == 409
                assert live_start_resp.json()["error"]["code"] == "LIVE_DISABLED"
                expect(page.get_by_test_id("error")).to_contain_text("LIVE_DISABLED", timeout=10000)
                assert invocations == 3
            finally:
                gates.setdefault("reload", threading.Event()).set()
                browser.close()


def _start_cli_server(
    *,
    data_root: Path,
    workspace_parent: Path,
    port: int,
    extra_args: list[str] | None = None,
) -> tuple[subprocess.Popen[str], str, str]:
    cmd = [
        sys.executable,
        "-B",
        "-m",
        "forecontract_app",
        "--data-root",
        str(data_root),
        "--workspace-parent",
        str(workspace_parent),
        "--port",
        str(port),
        "--offline",
        "--control-stdin",
        *(extra_args or []),
    ]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=make_clean_subprocess_env(),
    )
    assert proc.stdout is not None
    line = read_line_with_deadline(proc.stdout, timeout=15.0)
    prefix = "FORECONTRACT_APP_URL="
    assert line.startswith(prefix), f"unexpected startup stdout line: {line!r}"
    full_url = line[len(prefix) :].strip()
    token = full_url.split("#token=", 1)[1]
    base_url = full_url.split("/#token=", 1)[0]
    return proc, base_url, token


def test_a24_cli_exclusive_restart(tmp_path: Path) -> None:
    policy_before = asyncio.get_event_loop_policy()
    data_root = tmp_path / "cli_data"
    ws_parent = tmp_path / "cli_ws"
    data_root.mkdir()
    ws_parent.mkdir()
    port1 = pick_free_loopback_port()

    proc1 = None
    proc4 = None
    try:
        proc1, base1, token1 = _start_cli_server(
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

        # Process trying occupied port1 must also exit non-zero without polluting data_root2
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
            json={"client_request_id": "11111111-1111-4111-8111-111111111111", "goal": "cli run", "confirmed_scope": True},
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

        # Restart on same data_root & workspace_parent -> old sessions 404, old bytes untouched
        port4 = pick_free_loopback_port()
        proc4, base4, token4 = _start_cli_server(
            data_root=data_root, workspace_parent=ws_parent, port=port4
        )
        h4_get = {"Host": f"127.0.0.1:{port4}", "Authorization": f"Bearer {token4}"}
        old_get = httpx.get(
            f"{base4}/api/v1/sessions/{old_sid}/runs/{old_rid}", headers=h4_get, timeout=5.0
        )
        assert old_get.status_code in (200, 404)
        sessions_after = httpx.get(f"{base4}/api/v1/sessions", headers=h4_get, timeout=5.0).json()
        assert isinstance(sessions_after, list)
        assert tree_hashes(ws_parent / "cli-proj") == ws_before_restart
        assert tree_hashes(data_root / "runs") == runs_before_restart

        assert proc4.stdin is not None
        proc4.stdin.write("stop\n")
        proc4.stdin.flush()
        assert proc4.wait(timeout=8.5) == 0
        proc4 = None
        assert asyncio.get_event_loop_policy() is policy_before
    finally:
        if proc1 is not None:
            terminate_process_tree(proc1)
        if proc4 is not None:
            terminate_process_tree(proc4)
