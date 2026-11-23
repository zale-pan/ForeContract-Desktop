"""B28–B30: Real Playwright Chromium tests for multi-file JUnit+build approvals, stop/reload, XSS & tamper unknown."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

from app_helpers import LiveTcpTestServer
from forecontract_app.project_models import PROJECT_SCHEMA_VERSION
from forecontract_llm import ModelRequest, ModelResponse
from playwright.sync_api import expect, sync_playwright
from project_helpers import (
    create_buggy_calc_project,
    create_multifile_project,
    make_project_host_config,
    sha256_file,
    write_barrier_script,
)


def _launch_chromium(pw: Any) -> Any:
    return pw.chromium.launch(headless=True)


def test_b28_multifile_project_approval_two_junit_build_and_diff(tmp_path: Path) -> None:
    proj_dir = create_multifile_project(
        tmp_path / "ui-multifile", include_large_notes=False, doc_count=0
    )
    # Add second independent test module so verify_project executes TWO real JUnit checks + check_build.py
    (proj_dir / "tests" / "test_formatting_extra.py").write_text(
        "from src.formatting import format_status\n\n\n"
        "def test_format_extra_ok() -> None:\n"
        '    assert format_status("ui") == "ui: OK"\n',
        encoding="utf-8",
    )
    before_math_sha = sha256_file(proj_dir / "src" / "mathops.py")
    before_fmt_sha = sha256_file(proj_dir / "src" / "formatting.py")
    cfg = make_project_host_config(tmp_path, allowed_roots=(proj_dir,))

    with LiveTcpTestServer(config=cfg) as srv:
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
                expect(page.get_by_test_id("host-status-banner")).to_be_visible()
                assert "#token=" not in page.url

                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id("project-name").fill("ui-multifile")
                page.get_by_test_id("register-root").fill(str(proj_dir))
                page.get_by_test_id("confirm-read").check()
                page.get_by_test_id("register-project").click()
                expect(page.get_by_test_id("project-list")).to_contain_text("ui-multifile")
                page.get_by_test_id("nav-chat").click()

                page.get_by_test_id("chat-input").fill("修复多文件模块并运行两项 JUnit 与 build 检查")
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                page.locator('[data-testid^="offer-confirm-btn-"]').last.click()

                # 1) Scope proposal
                scope_item = page.locator('[data-testid^="proposal-item-"]', has_text="] scope ")
                expect(scope_item).to_be_visible(timeout=20000)
                scope_item.locator('[data-testid^="proposal-approve-"]').click()

                # 2) Verification plan proposal
                plan_item = page.locator('[data-testid^="proposal-item-"]', has_text="] verification ")
                expect(plan_item).to_be_visible(timeout=20000)
                expect(plan_item).to_contain_text("exit_code:仅检查退出码0，非测试通过")
                plan_item.locator('[data-testid^="proposal-approve-"]').click()

                # 3) First pending command proposal: verify missing trusted checkbox returns error
                first_cmd = page.locator(
                    '[data-testid^="proposal-item-"]', has_text="[pending] command "
                ).first
                expect(first_cmd).to_be_visible(timeout=20000)
                expect(first_cmd).to_contain_text("受信任本机执行")
                first_cmd.locator('[data-testid^="proposal-approve-"]').click()
                expect(page.get_by_test_id("error")).to_contain_text("TRUSTED_LOCAL_EXECUTION_REQUIRED")

                # Approve each sequential command proposal (2x junit_exact + 1x build_check)
                approved_cmds = 0
                deadline = time.monotonic() + 30.0
                while time.monotonic() < deadline:
                    if page.get_by_test_id("run-state").text_content() in (
                        "succeeded",
                        "failed",
                        "cancelled",
                        "unknown",
                    ):
                        break
                    pending_cmd = page.locator(
                        '[data-testid^="proposal-item-"]', has_text="[pending] command "
                    )
                    if pending_cmd.count() > 0:
                        item = pending_cmd.first
                        trust = item.locator('[data-testid^="proposal-trust-"]')
                        if not trust.is_checked():
                            trust.check()
                        item.locator('[data-testid^="proposal-approve-"]').click()
                        approved_cmds += 1
                    time.sleep(0.08)

                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-state")).to_have_text("succeeded", timeout=20000)
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("verification")).to_have_text("passed")
                assert approved_cmds >= 3

                after_math_sha = sha256_file(proj_dir / "src" / "mathops.py")
                after_fmt_sha = sha256_file(proj_dir / "src" / "formatting.py")
                after_const_sha = sha256_file(proj_dir / "src" / "constants.py")
                assert after_math_sha != before_math_sha
                assert after_fmt_sha != before_fmt_sha

                diff_el = page.get_by_test_id("diff-list")
                expect(diff_el).to_contain_text("src/mathops.py")
                expect(diff_el).to_contain_text("src/formatting.py")
                expect(diff_el).to_contain_text("src/constants.py")
                expect(diff_el).to_contain_text(after_math_sha)
                expect(diff_el).to_contain_text(after_const_sha)
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("coverage-summary")).to_contain_text("仅检查退出码=0")

                # Deliberately submitting command approval without trusted checkbox triggers 403 once.
                unexpected_console = [m for m in console_errors if "status of 403 (Forbidden)" not in m]
                assert unexpected_console == []
                assert len(console_errors) <= 1
                assert page_errors == []
                assert non_loopback == []
            finally:
                browser.close()


def test_b29_live_command_stop_and_reload_without_replay(tmp_path: Path) -> None:
    proj_dir = create_buggy_calc_project(tmp_path / "ui-stop")
    script = write_barrier_script(proj_dir / "hold.py", spawn_child=True)
    ready_file = tmp_path / "ui_ready.pid"
    release_file = tmp_path / "ui_release.flag"
    cfg = make_project_host_config(tmp_path, allowed_roots=(proj_dir,))
    spawn_counter = {"count": 0}

    class _BarrierCmdProvider:
        def __init__(self) -> None:
            self._step = 0

        async def complete(self, request: ModelRequest) -> ModelResponse:
            state = request.observation.state
            last = state.get("last_tool_result") or {}
            self._step += 1
            if self._step == 1:
                body = {
                    "schema_version": PROJECT_SCHEMA_VERSION,
                    "intent": "propose verification plan before command",
                    "decision_summary": "propose verification",
                    "proposed_action": {
                        "schema_version": PROJECT_SCHEMA_VERSION,
                        "action_type": "propose_verification",
                        "payload": {
                            "plan_id": "plan-ui-stop",
                            "summary": "diagnostic hold check",
                            "checks": [
                                {
                                    "check_id": "u1",
                                    "kind": "exit_code",
                                    "executable": sys.executable,
                                    "argv": ["-B", "-c", "print('ok')"],
                                    "cwd": ".",
                                    "timeout_ms": 5000,
                                }
                            ],
                        },
                        "metadata": {},
                    },
                    "expected_outcomes": [
                        {
                            "schema_version": PROJECT_SCHEMA_VERSION,
                            "outcome_type": "project_tool_result",
                            "payload": {"action_type": "propose_verification", "require_success": False},
                            "required": True,
                        }
                    ],
                }
            elif self._step == 2:
                body = {
                    "schema_version": PROJECT_SCHEMA_VERSION,
                    "intent": "propose long barrier cmd",
                    "decision_summary": "propose barrier command",
                    "proposed_action": {
                        "schema_version": PROJECT_SCHEMA_VERSION,
                        "action_type": "propose_command",
                        "payload": {
                            "command_id": "hold-ui",
                            "executable": sys.executable,
                            "argv": ["-B", str(script.name), str(ready_file), str(release_file)],
                            "cwd": ".",
                            "timeout_ms": 20000,
                            "purpose": "diagnostic",
                            "explanation": "barrier UI stop test",
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
                }
            elif self._step == 3:
                spawn_counter["count"] += 1
                body = {
                    "schema_version": PROJECT_SCHEMA_VERSION,
                    "intent": "start approved barrier cmd",
                    "decision_summary": "start_command",
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
                }
            else:
                body = {
                    "schema_version": PROJECT_SCHEMA_VERSION,
                    "intent": "poll running command",
                    "decision_summary": "poll_command",
                    "proposed_action": {
                        "schema_version": PROJECT_SCHEMA_VERSION,
                        "action_type": "poll_command",
                        "payload": {"command_id": "hold-ui", "after": 0},
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
                }
            return ModelResponse(
                schema_version="0.1",
                request_id=request.request_id,
                status="ok",
                content=json.dumps(body),
            )

    with LiveTcpTestServer(config=cfg, provider_factory=lambda **_: _BarrierCmdProvider()) as srv:
        if getattr(srv.app.state.service, "_chat_provider_factory", None) is None:
            srv.app.state.service._chat_provider_factory = lambda **_: (lambda req: {"action": "execution_offer", "goal": req.question, "summary": "执行任务"})
        url = f"{srv.base_url}/#token={cfg.access_token}"
        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            try:
                page = browser.new_page()
                page.goto(url, wait_until="domcontentloaded")
                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id("project-name").fill("ui-stop")
                page.get_by_test_id("register-root").fill(str(proj_dir))
                page.get_by_test_id("confirm-read").check()
                page.get_by_test_id("register-project").click()
                expect(page.get_by_test_id("project-list")).to_contain_text("ui-stop")
                page.get_by_test_id("nav-chat").click()

                page.get_by_test_id("chat-input").fill("Run barrier command and stop in UI")
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                page.locator('[data-testid^="offer-confirm-btn-"]').last.click()

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

                # Wait for real process ready signal & live streaming chunk
                deadline = time.monotonic() + 15.0
                while not ready_file.exists() and time.monotonic() < deadline:
                    time.sleep(0.03)
                assert ready_file.exists()

                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                page.get_by_test_id("tab-commands").click()
                stop_cmd_btn = page.get_by_test_id("stop-command-hold-ui")
                expect(stop_cmd_btn).to_be_visible(timeout=15000)
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("command-list")).to_contain_text("READY_SIGNAL")
                stop_cmd_btn.click()

                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("command-list")).to_contain_text("state=cancelled", timeout=15000)
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("command-list")).to_contain_text("cleanup_ok=true")
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("command-list")).to_contain_text("remaining=0")

                # Cancel run and verify page reload restores state cleanly without replaying command
                page.get_by_test_id("cancel-run").click()
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-state")).to_have_text("cancelled", timeout=15000)
                count_before_reload = spawn_counter["count"]
                assert count_before_reload == 1

                page.reload(wait_until="domcontentloaded")
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-state")).to_have_text("cancelled", timeout=15000)
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                page.get_by_test_id("tab-events").click()
                expect(page.get_by_test_id("tool-events")).to_contain_text("command_started")
                assert spawn_counter["count"] == 1
            finally:
                browser.close()


def test_b30_xss_warning_excluded_scope_and_protected_tamper_unknown(tmp_path: Path) -> None:
    proj_dir = create_buggy_calc_project(tmp_path / "ui-tamper")
    xss_payload = '<img src=x onerror="window.__xss=1"><script>window.__xss=2</script>'
    (proj_dir / "xss.py").write_text(f'LABEL = {xss_payload!r}\n', encoding="utf-8")
    cfg = make_project_host_config(tmp_path, allowed_roots=(proj_dir,))

    class _TamperProvider:
        def __init__(self) -> None:
            self._step = 0
            self._sha_xss: str | None = None

        async def complete(self, request: ModelRequest) -> ModelResponse:
            state = request.observation.state
            last = state.get("last_tool_result") or {}
            if last.get("action_type") == "read_file" and (last.get("sha256") or last.get("file_sha256")):
                self._sha_xss = str(last.get("sha256") or last.get("file_sha256"))

            self._step += 1
            if self._step == 1:
                body = {
                    "schema_version": PROJECT_SCHEMA_VERSION,
                    "intent": "read xss.py",
                    "decision_summary": "read xss.py",
                    "proposed_action": {
                        "schema_version": PROJECT_SCHEMA_VERSION,
                        "action_type": "read_file",
                        "payload": {"path": "xss.py", "start_line": 1, "max_lines": 50},
                        "metadata": {},
                    },
                    "expected_outcomes": [
                        {
                            "schema_version": PROJECT_SCHEMA_VERSION,
                            "outcome_type": "project_tool_result",
                            "payload": {"action_type": "read_file", "require_success": True},
                            "required": True,
                        }
                    ],
                }
            elif self._step == 2:
                body = {
                    "schema_version": PROJECT_SCHEMA_VERSION,
                    "intent": "propose scope",
                    "decision_summary": "scope for xss.py",
                    "proposed_action": {
                        "schema_version": PROJECT_SCHEMA_VERSION,
                        "action_type": "propose_scope",
                        "payload": {
                            "exact_paths": ["xss.py"],
                            "operations": ["modify"],
                            "protected_paths": ["tests/test_calc.py"],
                            "verification_plan_summary": "run exit check",
                        },
                        "metadata": {},
                    },
                    "expected_outcomes": [
                        {
                            "schema_version": PROJECT_SCHEMA_VERSION,
                            "outcome_type": "project_tool_result",
                            "payload": {"action_type": "propose_scope", "require_success": False},
                            "required": True,
                        }
                    ],
                }
            elif self._step == 3:
                body = {
                    "schema_version": PROJECT_SCHEMA_VERSION,
                    "intent": "propose verification",
                    "decision_summary": "plan with protected test file",
                    "proposed_action": {
                        "schema_version": PROJECT_SCHEMA_VERSION,
                        "action_type": "propose_verification",
                        "payload": {
                            "plan_id": "plan-xss",
                            "summary": "run python -c ok",
                            "checks": [
                                {
                                    "check_id": "smoke",
                                    "kind": "exit_code",
                                    "executable": sys.executable,
                                    "argv": ["-B", "-c", "print('OK')"],
                                    "cwd": ".",
                                    "timeout_ms": 15000,
                                    "protected_paths": ["tests/test_calc.py"],
                                }
                            ],
                        },
                        "metadata": {},
                    },
                    "expected_outcomes": [
                        {
                            "schema_version": PROJECT_SCHEMA_VERSION,
                            "outcome_type": "project_tool_result",
                            "payload": {"action_type": "propose_verification", "require_success": False},
                            "required": True,
                        }
                    ],
                }
            elif self._step == 4:
                assert self._sha_xss is not None
                body = {
                    "schema_version": PROJECT_SCHEMA_VERSION,
                    "intent": "edit xss.py leaving XSS literal in diff",
                    "decision_summary": "edit xss.py",
                    "proposed_action": {
                        "schema_version": PROJECT_SCHEMA_VERSION,
                        "action_type": "edit_file",
                        "payload": {
                            "path": "xss.py",
                            "expected_sha256": self._sha_xss,
                            "old_text": "LABEL =",
                            "new_text": "SAFE_COMMENT = 'edited'\nLABEL =",
                        },
                        "metadata": {},
                    },
                    "expected_outcomes": [
                        {
                            "schema_version": PROJECT_SCHEMA_VERSION,
                            "outcome_type": "project_file_written",
                            "payload": {"path": "xss.py"},
                            "required": True,
                        }
                    ],
                }
            else:
                # Tamper protected test file on disk BEFORE verify_project runs -> UI sees unknown/stale
                (proj_dir / "tests" / "test_calc.py").write_text(
                    "def test_add_basic() -> None:\n    assert False\n", encoding="utf-8"
                )
                body = {
                    "schema_version": PROJECT_SCHEMA_VERSION,
                    "intent": "verify after protected source tamper",
                    "decision_summary": "verify_project",
                    "proposed_action": {
                        "schema_version": PROJECT_SCHEMA_VERSION,
                        "action_type": "verify_project",
                        "payload": {
                            "plan_id": str(state.get("plan_id") or "plan-xss"),
                            "plan_digest": str(state.get("plan_digest") or ""),
                        },
                        "metadata": {},
                    },
                    "expected_outcomes": [
                        {
                            "schema_version": PROJECT_SCHEMA_VERSION,
                            "outcome_type": "project_task_verified",
                            "payload": {},
                            "required": True,
                        }
                    ],
                }
            return ModelResponse(
                schema_version="0.1",
                request_id=request.request_id,
                status="ok",
                content=json.dumps(body),
            )

    with LiveTcpTestServer(config=cfg, provider_factory=lambda **_: _TamperProvider()) as srv:
        if getattr(srv.app.state.service, "_chat_provider_factory", None) is None:
            srv.app.state.service._chat_provider_factory = lambda **_: (lambda req: {"action": "execution_offer", "goal": req.question, "summary": "执行任务"})
        url = f"{srv.base_url}/#token={cfg.access_token}"
        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            try:
                page = browser.new_page()
                page.goto(url, wait_until="domcontentloaded")
                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id("project-name").fill("ui-tamper")
                page.get_by_test_id("register-root").fill(str(proj_dir))
                page.get_by_test_id("confirm-read").check()
                page.get_by_test_id("register-project").click()
                expect(page.get_by_test_id("project-list")).to_contain_text("ui-tamper")
                page.get_by_test_id("nav-chat").click()

                page.get_by_test_id("chat-input").fill("XSS safe render and protected source tamper check")
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                page.locator('[data-testid^="offer-confirm-btn-"]').last.click()

                # Approve scope & verification plan
                scope_item = page.locator('[data-testid^="proposal-item-"]', has_text="] scope ")
                expect(scope_item).to_be_visible(timeout=20000)
                scope_item.locator('[data-testid^="proposal-approve-"]').click()

                plan_item = page.locator('[data-testid^="proposal-item-"]', has_text="] verification ")
                expect(plan_item).to_be_visible(timeout=20000)
                plan_item.locator('[data-testid^="proposal-approve-"]').click()

                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-state")).to_have_text("failed", timeout=25000)
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("verification")).to_have_text("unknown")
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("reason-code")).to_have_text("VERIFICATION_STALE")
                expect(page.get_by_test_id("diff-list")).to_contain_text("xss.py")
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("coverage-summary")).to_contain_text(".git")
                assert page.evaluate("window.__xss") is None
            finally:
                browser.close()


def test_trial_invalid_scope_visible(tmp_path: Path) -> None:
    """T21: literal edit and two marker payloads are visible, survive reload, and never echo."""
    from forecontract_app.project_demo import OfflineProjectProvider

    def _forecast(*, action_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": PROJECT_SCHEMA_VERSION,
            "intent": f"test {action_type}",
            "decision_summary": f"run {action_type}",
            "proposed_action": {
                "schema_version": PROJECT_SCHEMA_VERSION,
                "action_type": action_type,
                "payload": payload,
                "metadata": {},
            },
            "expected_outcomes": [
                {
                    "schema_version": PROJECT_SCHEMA_VERSION,
                    "outcome_type": "project_tool_result",
                    "payload": {"action_type": action_type, "require_success": False},
                    "required": True,
                }
            ],
        }

    class _TrialInvalidScopeProvider:
        def __init__(self, *, marker: str, as_field: bool = False) -> None:
            self._marker = marker
            self._as_field = as_field
            self._step = 0

        async def complete(self, request: ModelRequest) -> ModelResponse:
            self._step += 1
            if self._step == 1:
                body = _forecast(action_type="read_file", payload={"path": "calc.py"})
            elif self._as_field:
                body = _forecast(
                    action_type="propose_scope",
                    payload={
                        "exact_paths": ["calc.py"],
                        "operations": ["modify"],
                        "verification_plan_summary": "run calc tests",
                        self._marker: "ignored",
                    },
                )
            else:
                body = _forecast(
                    action_type="propose_scope",
                    payload={
                        "exact_paths": ["calc.py"],
                        "operations": [self._marker],
                        "verification_plan_summary": "run calc tests",
                    },
                )
            return ModelResponse(
                schema_version="0.1",
                request_id=request.request_id,
                status="ok",
                content=json.dumps(body),
            )

    marker_op = "synthetic-sensitive-operation-7f3a"
    marker_field = "synthetic-unexpected-field-9c2b"
    proj_dir = create_buggy_calc_project(tmp_path / "ui-trial")
    cfg = make_project_host_config(tmp_path, allowed_roots=(proj_dir,))
    providers: list[Any] = [
        _TrialInvalidScopeProvider(marker="edit"),
        _TrialInvalidScopeProvider(marker=marker_op),
        _TrialInvalidScopeProvider(marker=marker_field, as_field=True),
        OfflineProjectProvider(),
    ]

    def _factory(**_: Any) -> Any:
        return providers.pop(0)

    def _wait_terminal(page: Any) -> str:
        deadline = time.monotonic() + 90.0
        while time.monotonic() < deadline:
            state = page.get_by_test_id("run-state").text_content() or ""
            if state in {"succeeded", "failed", "cancelled", "unknown"}:
                return state
            pending = page.locator('[data-testid^="proposal-item-"]', has_text="[pending]")
            if pending.count() > 0:
                item = pending.first
                approve = item.locator('[data-testid^="proposal-approve-"]')
                if approve.count() > 0:
                    trust = item.locator('[data-testid^="proposal-trust-"]')
                    if trust.count() > 0 and not trust.is_checked():
                        trust.check()
                    approve.click()
            page.wait_for_timeout(100)
        return page.get_by_test_id("run-state").text_content() or ""

    def _start_run(page: Any, goal: str, previous_id: str = "") -> str:
        page.get_by_test_id("chat-input").fill(goal)
        page.get_by_test_id("chat-send").click()
        page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
        page.locator('[data-testid^="offer-confirm-btn-"]').last.click()
        if previous_id:
            if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
            expect(page.get_by_test_id("run-id")).not_to_have_text(
                previous_id, timeout=15000
            )
        page.wait_for_timeout(600)
        return (page.get_by_test_id("run-id").text_content() or "").strip()

    def _assert_rejection(page: Any, rid: str) -> None:
        if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
        expect(page.get_by_test_id("run-id")).to_have_text(rid, timeout=15000)
        if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
        expect(page.get_by_test_id("run-state")).to_have_text("failed", timeout=15000)
        if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
        expect(page.get_by_test_id("reason-code")).to_have_text("EVIDENCE_INVALID")
        summary = page.get_by_test_id("coverage-summary")
        expect(summary).to_contain_text("propose_scope")
        expect(summary).to_contain_text("create")
        expect(summary).to_contain_text("modify")
        body_text = page.locator("body").inner_text()
        assert marker_op not in body_text
        assert marker_field not in body_text
        assert "ValidationError" not in body_text
        assert "Traceback" not in body_text

    with LiveTcpTestServer(config=cfg, provider_factory=_factory) as srv:
        if getattr(srv.app.state.service, "_chat_provider_factory", None) is None:
            srv.app.state.service._chat_provider_factory = lambda **_: (lambda req: {"action": "execution_offer", "goal": req.question, "summary": "执行任务"})
        url = f"{srv.base_url}/#token={cfg.access_token}"
        console_errors: list[str] = []
        page_errors: list[str] = []
        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            try:
                page = browser.new_page()
                page.on(
                    "console",
                    lambda m: console_errors.append(m.text) if m.type == "error" else None,
                )
                page.on("pageerror", lambda err: page_errors.append(str(err)))
                page.goto(url, wait_until="domcontentloaded")
                expect(page.get_by_test_id("host-status-banner")).to_be_visible()

                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id("project-name").fill("ui-trial")
                page.get_by_test_id("register-root").fill(str(proj_dir))
                page.get_by_test_id("confirm-read").check()
                page.get_by_test_id("register-project").click()
                expect(page.get_by_test_id("project-list")).to_contain_text("ui-trial")
                page.get_by_test_id("nav-chat").click()


                run0 = _start_run(page, "试用字面非法操作")
                assert _wait_terminal(page) == "failed"
                _assert_rejection(page, run0)
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("coverage-summary")).to_contain_text("operations[0]")

                run1 = _start_run(page, "试用非法写入范围", run0)
                assert _wait_terminal(page) == "failed"
                assert run1 != run0
                _assert_rejection(page, run1)
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("coverage-summary")).to_contain_text("operations[0]")

                run2 = _start_run(page, "试用意外字段名", run1)
                assert _wait_terminal(page) == "failed"
                assert run2 not in {run0, run1}
                _assert_rejection(page, run2)
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("coverage-summary")).to_contain_text("payload")

                run3 = _start_run(page, "修复 calc 缺陷并验证", run2)
                assert _wait_terminal(page) == "succeeded"
                assert run3 not in {run0, run1, run2}
                succeed_summary = page.get_by_test_id("coverage-summary").text_content() or ""
                assert "operations[0]" not in succeed_summary
                assert "payload" not in succeed_summary

                page.reload(wait_until="domcontentloaded")
                for rid, expected_field in (
                    (run0, "operations[0]"),
                    (run1, "operations[0]"),
                    (run2, "payload"),
                ):
                    page.get_by_test_id("nav-history").click()
                    page.get_by_test_id("run-history-select").select_option(value=rid)
                    page.get_by_test_id("nav-chat").click()
                    _assert_rejection(page, rid)
                    if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                    page.get_by_test_id("tab-checks").click()
                    expect(page.get_by_test_id("coverage-summary")).to_contain_text(
                        expected_field
                    )
                page.get_by_test_id("nav-history").click()
                page.get_by_test_id("run-history-select").select_option(value=run3)
                page.get_by_test_id("nav-chat").click()
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-id")).to_have_text(run3, timeout=15000)
                page.get_by_test_id("tab-checks").click()
                assert "operations[0]" not in (
                    page.get_by_test_id("coverage-summary").text_content() or ""
                )

                assert console_errors == []
                assert page_errors == []
            finally:
                browser.close()

def test_trial_unsupported_outcome_diagnostic_path(tmp_path: Path) -> None:
    """APP-T06 round-029: locate UNSUPPORTED_OUTCOME across request schema, codec decode, Core, host, and Chromium UI."""
    import uuid as _uuid

    import httpx
    from app_helpers import auth_headers
    from forecontract.runtime.engine import RuntimeEngine
    from forecontract_llm import LLMPlanner, PlannerError
    from forecontract_llm import codec as llm_codec
    from forecontract_llm import planner as llm_planner_mod

    proj_dir = create_buggy_calc_project(tmp_path / "unsupported-outcome-proj")
    cfg = make_project_host_config(tmp_path, allowed_roots=(proj_dir,))
    marker_outcome = "synthetic_unsupported_outcome_4e9a"

    expected_outcome_set = {
        "project_tool_result",
        "project_file_written",
        "project_command_result",
        "project_task_verified",
    }

    captured_requests: list[dict[str, Any]] = []
    decode_observations: list[dict[str, Any]] = []
    planner_observations: list[dict[str, Any]] = []
    core_plan_observations: list[dict[str, Any]] = []
    step_two_last_tool_result: list[dict[str, Any]] = []

    class _TwoStepUnsupportedOutcomeProvider:
        def __init__(self) -> None:
            self._step = 0

        async def complete(self, request: ModelRequest) -> ModelResponse:
            self._step += 1
            out_prop = (
                request.response_schema.get("properties", {})
                .get("expected_outcomes", {})
                .get("items", {})
                .get("properties", {})
                .get("outcome_type", {})
            )
            captured_requests.append(
                {
                    "step": self._step,
                    "request_id": request.request_id,
                    "outcome_type_schema": dict(out_prop),
                    "supported_outcome_types": list(
                        request.model_context.capabilities.supported_outcome_types
                    ),
                    "outcome_guidance_keys": sorted(
                        request.model_context.outcome_guidance.keys()
                    ),
                }
            )
            obs_state = request.observation.state if request.observation is not None else {}
            if self._step == 1:
                body = {
                    "schema_version": PROJECT_SCHEMA_VERSION,
                    "intent": "inspect calc.py",
                    "decision_summary": "read calc.py first",
                    "proposed_action": {
                        "schema_version": PROJECT_SCHEMA_VERSION,
                        "action_type": "read_file",
                        "payload": {"path": "calc.py"},
                        "metadata": {},
                    },
                    "expected_outcomes": [
                        {
                            "schema_version": PROJECT_SCHEMA_VERSION,
                            "outcome_type": "project_tool_result",
                            "payload": {"action_type": "read_file", "require_success": True},
                            "required": True,
                        }
                    ],
                }
            else:
                if isinstance(obs_state, dict) and isinstance(obs_state.get("last_tool_result"), dict):
                    step_two_last_tool_result.append(dict(obs_state["last_tool_result"]))
                body = {
                    "schema_version": PROJECT_SCHEMA_VERSION,
                    "intent": "propose scope with unsupported outcome_type",
                    "decision_summary": "send synthetic unsupported outcome_type",
                    "proposed_action": {
                        "schema_version": PROJECT_SCHEMA_VERSION,
                        "action_type": "propose_scope",
                        "payload": {
                            "exact_paths": ["calc.py"],
                            "operations": ["modify"],
                            "verification_plan_summary": "run calc tests",
                        },
                        "metadata": {},
                    },
                    "expected_outcomes": [
                        {
                            "schema_version": PROJECT_SCHEMA_VERSION,
                            "outcome_type": marker_outcome,
                            "payload": {"path": "calc.py"},
                            "required": True,
                        }
                    ],
                }
            return ModelResponse(
                schema_version="0.1",
                request_id=request.request_id,
                status="ok",
                content=json.dumps(body),
            )

    orig_decode_response = llm_codec.decode_response
    orig_llm_plan = LLMPlanner.plan
    orig_engine_plan = RuntimeEngine._plan

    def _wrapped_decode_response(*args: Any, **kwargs: Any) -> Any:
        try:
            res = orig_decode_response(*args, **kwargs)
            decode_observations.append({"status": "ok"})
            return res
        except PlannerError as exc:
            decode_observations.append(
                {
                    "status": "error",
                    "exc_type": type(exc).__name__,
                    "code": exc.code,
                    "location": exc.location,
                }
            )
            raise

    async def _wrapped_llm_plan(self_planner: Any, observation: Any) -> Any:
        try:
            res = await orig_llm_plan(self_planner, observation)
            planner_observations.append({"status": "ok"})
            return res
        except PlannerError as exc:
            planner_observations.append(
                {
                    "status": "error",
                    "exc_type": type(exc).__name__,
                    "code": exc.code,
                    "location": exc.location,
                }
            )
            raise

    async def _wrapped_engine_plan(
        self_engine: Any, req: Any, progress: Any, observation: Any
    ) -> Any:
        try:
            res = await orig_engine_plan(self_engine, req, progress, observation)
            core_plan_observations.append({"status": "ok"})
            return res
        except Exception as exc:
            core_plan_observations.append(
                {
                    "status": "error",
                    "exc_type": type(exc).__name__,
                    "code": getattr(exc, "code", None),
                    "location": getattr(exc, "location", None),
                }
            )
            raise

    llm_codec.decode_response = _wrapped_decode_response  # type: ignore[assignment]
    llm_planner_mod.decode_response = _wrapped_decode_response  # type: ignore[assignment]
    LLMPlanner.plan = _wrapped_llm_plan  # type: ignore[method-assign]
    RuntimeEngine._plan = _wrapped_engine_plan  # type: ignore[method-assign]
    try:
        with LiveTcpTestServer(
            config=cfg,
            provider_factory=lambda *, project, run_id: _TwoStepUnsupportedOutcomeProvider(),
        ) as srv:
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
                            if not req.url.startswith(srv.base_url)
                            else None
                        ),
                    )
                    page.goto(url, wait_until="domcontentloaded")
                    expect(page.get_by_test_id("host-status-banner")).to_be_visible()

                    page.get_by_test_id("nav-projects").click()
                    page.get_by_test_id("register-root").fill(str(proj_dir))
                    page.get_by_test_id("project-name").fill("u-outcome-proj")
                    page.get_by_test_id("confirm-read").check()
                    page.get_by_test_id("register-project").click()
                    expect(page.get_by_test_id("current-project")).not_to_have_text("-", timeout=10000)
                    page.get_by_test_id("nav-chat").click()

                    page.get_by_test_id("chat-input").fill("Locate outcome diagnostic path")
                    page.get_by_test_id("chat-send").click()
                    page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                    page.locator('[data-testid^="offer-confirm-btn-"]').last.click()

                    if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                    expect(page.get_by_test_id("run-state")).to_have_text("failed", timeout=20000)
                    if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                    expect(page.get_by_test_id("reason-code")).to_have_text("EXECUTION_ERROR", timeout=10000)

                    run_id = page.get_by_test_id("run-id").inner_text().strip()
                    session_id = page.get_by_test_id("current-session").inner_text().strip()
                    _uuid.UUID(run_id)
                    _uuid.UUID(session_id)

                    body_text = page.locator("body").inner_text()
                    reason_text = page.get_by_test_id("reason-code").inner_text()
                    ver_text = page.get_by_test_id("verification").inner_text()
                    error_text = page.get_by_test_id("error").inner_text()
                    events_text = page.get_by_test_id("tool-events").inner_text()

                    assert marker_outcome not in body_text
                    assert "Traceback" not in body_text
                    assert "EXECUTION_ERROR" in reason_text
                    assert "expected_outcomes[0].outcome_type" not in body_text
                    assert "UNSUPPORTED_OUTCOME" not in body_text
                    assert ver_text.strip() == "unknown"
                    assert error_text.strip() == ""
                    assert "read_file" in events_text
                    assert console_errors == []
                    assert page_errors == []
                    assert non_loopback == []
                finally:
                    browser.close()

            # Read back host state, persisted result, run_finished event, and trace for the same session/run via HTTP
            hdrs = auth_headers(cfg)
            run_resp = httpx.get(
                f"{srv.base_url}/api/v1/sessions/{session_id}/runs/{run_id}",
                headers=hdrs,
                timeout=10.0,
            )
            assert run_resp.status_code == 200
            run_data = run_resp.json()
            snap_resp = httpx.get(
                f"{srv.base_url}/api/v1/sessions/{session_id}/runs/{run_id}/snapshot",
                headers=hdrs,
                timeout=10.0,
            )
            assert snap_resp.status_code == 200
            snap_data = snap_resp.json()
            props_resp = httpx.get(
                f"{srv.base_url}/api/v1/sessions/{session_id}/runs/{run_id}/proposals",
                headers=hdrs,
                timeout=10.0,
            )
            assert props_resp.status_code == 200
            assert props_resp.json() == []

            finished_events = [
                ev for ev in snap_data.get("events", []) if ev.get("kind") == "run_finished"
            ]
            assert len(finished_events) == 1
            finished_ev = finished_events[0]

            assert run_data["state"] == "failed"
            assert run_data["verification"] == "unknown"
            assert run_data["reason_code"] == "EXECUTION_ERROR"
            assert finished_ev["payload"].get("reason_code") == "EXECUTION_ERROR"
            assert run_data["result"] is None

            # Verify request schema & capabilities on real complete(request)
            assert len(captured_requests) == 2
            for cap_req in captured_requests:
                ot_schema = cap_req["outcome_type_schema"]
                assert ot_schema.get("type") == "string"
                assert ot_schema.get("minLength") == 1
                assert ot_schema.get("enum") == cap_req["supported_outcome_types"]
                assert set(ot_schema.get("enum") or []) == expected_outcome_set
                assert set(cap_req["supported_outcome_types"]) == expected_outcome_set
                assert set(cap_req["outcome_guidance_keys"]) == expected_outcome_set

            # Positive step 1 produced real read_file tool result; step 2 never committed a contract
            assert len(step_two_last_tool_result) == 1
            assert step_two_last_tool_result[0].get("action_type") == "read_file"
            assert step_two_last_tool_result[0].get("tool_succeeded") is True

            assert decode_observations == [
                {"status": "ok"},
                {
                    "status": "error",
                    "exc_type": "PlannerError",
                    "code": "UNSUPPORTED_OUTCOME",
                    "location": ("outcomes", 0),
                },
            ]
            assert planner_observations == [
                {"status": "ok"},
                {
                    "status": "error",
                    "exc_type": "PlannerError",
                    "code": "UNSUPPORTED_OUTCOME",
                    "location": ("outcomes", 0),
                },
            ]
            assert core_plan_observations == [
                {"status": "ok"},
                {
                    "status": "error",
                    "exc_type": "RuntimeError",
                    "code": "DEPENDENCY_FAILURE",
                    "location": ("<field>",),
                },
            ]

            trace_path = cfg.data_root / "runs" / run_id / "trace.jsonl"
            trace_records = [
                json.loads(line)
                for line in trace_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            committed = [r for r in trace_records if r.get("kind") == "contract_committed"]
            assert len(committed) == 1

            persisted_dump = json.dumps(run_data) + json.dumps(finished_ev)
            assert marker_outcome not in persisted_dump
            assert "Traceback" not in persisted_dump
            assert "a proposed outcome is outside the declared capabilities" not in persisted_dump
    finally:
        llm_codec.decode_response = orig_decode_response  # type: ignore[assignment]
        llm_planner_mod.decode_response = orig_decode_response  # type: ignore[assignment]
        LLMPlanner.plan = orig_llm_plan  # type: ignore[method-assign]
        RuntimeEngine._plan = orig_engine_plan  # type: ignore[method-assign]


def test_trial_scope_forecast_example_reaches_approval(tmp_path: Path) -> None:
    """APP-T06 round-030: extract propose_scope example_forecast from real request and verify pending approval."""
    import uuid as _uuid

    import httpx
    from app_helpers import auth_headers

    proj_dir = create_buggy_calc_project(tmp_path / "scope-example-proj")
    before_calc_bytes = (proj_dir / "calc.py").read_bytes()
    cfg = make_project_host_config(tmp_path, allowed_roots=(proj_dir,))

    seen_requests: list[dict[str, Any]] = []
    extracted_examples: list[dict[str, Any]] = []

    class _ScopeExampleProvider:
        def __init__(self) -> None:
            self._step = 0

        async def complete(self, request: ModelRequest) -> ModelResponse:
            self._step += 1
            dumped = request.model_dump(mode="json")
            ot_schema = (
                dumped["response_schema"]["properties"]["expected_outcomes"]["items"]["properties"][
                    "outcome_type"
                ]
            )
            scope_guidance = (dumped["model_context"]["action_guidance"] or {}).get(
                "propose_scope", {}
            )
            example = scope_guidance.get("example_forecast")
            seen_requests.append(
                {
                    "step": self._step,
                    "outcome_type_enum": ot_schema.get("enum"),
                    "has_example": isinstance(example, dict),
                }
            )
            if self._step == 1:
                body: dict[str, Any] = {
                    "schema_version": PROJECT_SCHEMA_VERSION,
                    "intent": "inspect calc.py",
                    "decision_summary": "read calc.py first",
                    "proposed_action": {
                        "schema_version": PROJECT_SCHEMA_VERSION,
                        "action_type": "read_file",
                        "payload": {"path": "calc.py"},
                        "metadata": {},
                    },
                    "expected_outcomes": [
                        {
                            "schema_version": PROJECT_SCHEMA_VERSION,
                            "outcome_type": "project_tool_result",
                            "payload": {"action_type": "read_file", "require_success": True},
                            "required": True,
                        }
                    ],
                }
            else:
                assert isinstance(example, dict)
                extracted_examples.append(example)
                body = example
            return ModelResponse(
                schema_version="0.1",
                request_id=request.request_id,
                status="ok",
                content=json.dumps(body),
            )

    with LiveTcpTestServer(
        config=cfg,
        provider_factory=lambda *, project, run_id: _ScopeExampleProvider(),
    ) as srv:
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
                        if not req.url.startswith(srv.base_url)
                        else None
                    ),
                )
                page.goto(url, wait_until="domcontentloaded")
                expect(page.get_by_test_id("host-status-banner")).to_be_visible()

                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id("register-root").fill(str(proj_dir))
                page.get_by_test_id("project-name").fill("scope-ex-proj")
                page.get_by_test_id("confirm-read").check()
                page.get_by_test_id("register-project").click()
                expect(page.get_by_test_id("current-project")).not_to_have_text("-", timeout=10000)
                page.get_by_test_id("nav-chat").click()

                page.get_by_test_id("chat-input").fill("Propose scope using guidance example")
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                page.locator('[data-testid^="offer-confirm-btn-"]').last.click()

                # Scope proposal from the guidance example must appear pending in UI
                pending_scope = page.locator(
                    '[data-testid^="proposal-item-"]', has_text="[pending] scope "
                ).first
                expect(pending_scope).to_be_visible(timeout=20000)
                expect(pending_scope).to_contain_text("calc.py")
                expect(pending_scope).to_contain_text("modify")

                run_id = page.get_by_test_id("run-id").inner_text().strip()
                session_id = page.get_by_test_id("current-session").inner_text().strip()
                _uuid.UUID(run_id)
                _uuid.UUID(session_id)

                # While pending approval, calc.py bytes on disk remain untouched
                assert (proj_dir / "calc.py").read_bytes() == before_calc_bytes

                props_resp = httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{session_id}/runs/{run_id}/proposals",
                    headers=auth_headers(cfg),
                    timeout=10.0,
                )
                assert props_resp.status_code == 200
                props_list = props_resp.json()
                assert len(props_list) == 1
                prop = props_list[0]
                assert prop["kind"] == "scope"
                assert prop["state"] == "pending"
                scope_payload = prop.get("payload") or {}
                assert scope_payload.get("exact_paths") == ["calc.py"]
                assert scope_payload.get("operations") == ["modify"]

                # Cleanly cancel run without approving or writing files
                page.get_by_test_id("cancel-run").click()
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-state")).to_have_text("cancelled", timeout=15000)
                assert (proj_dir / "calc.py").read_bytes() == before_calc_bytes
                assert console_errors == []
                assert page_errors == []
                assert non_loopback == []
            finally:
                browser.close()

        assert len(seen_requests) >= 2
        assert all(
            set(r["outcome_type_enum"] or [])
            == {
                "project_tool_result",
                "project_file_written",
                "project_command_result",
                "project_task_verified",
            }
            for r in seen_requests
        )
        assert all(r["has_example"] for r in seen_requests)
        assert len(extracted_examples) == 1
        ex = extracted_examples[0]
        assert ex["schema_version"] == PROJECT_SCHEMA_VERSION
        assert ex["proposed_action"]["action_type"] == "propose_scope"
        assert ex["proposed_action"]["payload"]["exact_paths"] == ["calc.py"]
        assert ex["proposed_action"]["payload"]["operations"] == ["modify"]
        assert len(ex["expected_outcomes"]) == 1
        assert ex["expected_outcomes"][0]["outcome_type"] == "project_tool_result"
        assert ex["expected_outcomes"][0]["payload"]["action_type"] == "propose_scope"

