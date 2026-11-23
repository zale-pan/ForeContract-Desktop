"""S31–S36: Real Playwright browser workflows for Skills, stdio/HTTP MCP approvals, stale-epoch guards, reload/cancel, and keyboard/XSS safety."""

from __future__ import annotations

import re
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from app_helpers import LiveTcpTestServer, tree_hashes
from extension_helpers import (
    CallbackProjectProvider,
    LocalHttpMcpServer,
    ext_forecast,
    make_extension_host_config,
    make_skill,
    read_stdio_log,
    write_extension_config_file,
    write_stdio_mcp_script,
)
from forecontract_llm import ModelRequest
from playwright.sync_api import expect, sync_playwright
from project_helpers import create_buggy_calc_project


def test_s31_skill_browser(tmp_path: Path) -> None:
    import json

    proj = create_buggy_calc_project(tmp_path / "proj")
    s31_uniq = uuid.uuid4().hex[:12]
    make_skill(
        proj,
        "fix-calc",
        f"# Calc Fix Skill\nInspect calc.py and apply FIX_EXPR::return a + b  # UQ{s31_uniq}::END_FIX.\nThen run junit verify.\n",
    )
    make_skill(proj, "s31-b", "# Skill B\n")
    make_skill(proj, "s31-c", "# Skill C\n")
    make_skill(proj, "s31-d", "# Skill D\n")
    make_skill(proj, "s31-e", "# Skill E\n")
    make_skill(proj, "s31-huge", "H" * 16500)
    cfg = make_extension_host_config(tmp_path / "host", allowed_roots=(proj,))

    observed_fix31: list[str] = []

    def _decide(step: int, req: ModelRequest, state: dict[str, Any]) -> str:
        _ = req
        # F05: Dynamically extract the fix expression from the selected Skill in observation
        ext_state = state.get("extensions") or {}
        skills_dump = json.dumps(ext_state.get("selected_skills") or [], ensure_ascii=False)
        fix_match = re.search(r"FIX_EXPR::(return .*?)::END_FIX", skills_dump)
        if fix_match is None:
            return "MISSING_SKILL_OBSERVATION_ABORT"
        dynamic_new_text = fix_match.group(1)
        observed_fix31.append(dynamic_new_text)

        last = state.get("last_tool_result") or {}
        if step == 1:
            return ext_forecast(action_type="read_file", payload={"path": "calc.py"})
        if step == 2:
            return ext_forecast(
                action_type="propose_scope",
                payload={
                    "exact_paths": ["calc.py"],
                    "write_prefixes": [],
                    "operations": ["modify"],
                    "protected_paths": ["tests/test_calc.py"],
                    "verification_plan_summary": "pytest junit",
                },
            )
        if step == 3:
            return ext_forecast(
                action_type="propose_verification",
                payload={
                    "plan_id": "p31",
                    "summary": "run calc pytest",
                    "checks": [
                        {
                            "check_id": "unit",
                            "kind": "junit_exact",
                            "executable": sys.executable,
                            "argv": ["-B", "-m", "pytest", "-p", "no:cacheprovider", "tests/test_calc.py", "--junitxml={result_dir}/junit.xml"],
                            "cwd": ".",
                            "timeout_ms": 20000,
                            "protected_paths": ["tests/test_calc.py"],
                            "expected_tests": [{"classname": "tests.test_calc", "name": "test_add_basic"}],
                        }
                    ],
                },
            )
        if step == 4:
            return ext_forecast(action_type="read_file", payload={"path": "calc.py"})
        if last.get("action_type") == "read_file" and (last.get("sha256") or last.get("file_sha256")):
            file_sha = last.get("sha256") or last.get("file_sha256")
            return ext_forecast(
                action_type="edit_file",
                payload={
                    "path": "calc.py",
                    "expected_sha256": file_sha,
                    "old_text": "return a - b",
                    "new_text": dynamic_new_text,
                },
                outcome_type="project_file_written",
                outcome_payload={"path": "calc.py"},
            )
        return ext_forecast(
            action_type="verify_project",
            payload={"plan_id": state["plan_id"], "plan_digest": state["plan_digest"]},
            outcome_type="project_task_verified",
            outcome_payload={},
            task_hint=True,
        )

    with LiveTcpTestServer(config=cfg, provider_factory=lambda *, project, run_id: CallbackProjectProvider(_decide)) as srv, sync_playwright() as pw:
        if getattr(srv.app.state.service, "_chat_provider_factory", None) is None:
            srv.app.state.service._chat_provider_factory = lambda **_: (lambda req: {"action": "execution_offer", "goal": req.question, "summary": "执行任务"})
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(f"{srv.base_url}/#token={cfg.access_token}", wait_until="domcontentloaded")
            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id("register-root").fill(str(proj))
            page.get_by_test_id("project-name").fill("s31-proj")
            page.get_by_test_id("confirm-read").check()
            page.get_by_test_id("register-project").click()

            page.get_by_test_id("nav-extensions").click()
            cb = page.get_by_test_id("skill-select-fix-calc")
            expect(cb).to_be_visible(timeout=5000)

            # 1. Selecting a 5th skill in UI is blocked with SKILL_SELECTION_LIMIT
            for sid in ("fix-calc", "s31-b", "s31-c", "s31-d"):
                page.get_by_test_id(f"skill-select-{sid}").check()
            page.get_by_test_id("skill-select-s31-e").click()
            expect(page.get_by_test_id("error")).to_contain_text("SKILL_SELECTION_LIMIT")
            assert not page.get_by_test_id("skill-select-s31-e").is_checked()
            for sid in ("s31-b", "s31-c", "s31-d"):
                page.get_by_test_id(f"skill-select-{sid}").uncheck()

            # 2. Selecting >16 KiB skill and clicking start-run shows 4xx SKILL_CONTEXT_LIMIT without starting run
            cb.uncheck()
            page.get_by_test_id("skill-select-s31-huge").check()
            page.get_by_test_id("nav-chat").click()
            page.get_by_test_id("chat-input").fill("fix calc following skill")
            page.get_by_test_id("chat-send").click()
            page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
            page.locator('[data-testid^="offer-confirm-btn-"]').last.click()
            expect(page.get_by_test_id("error")).to_contain_text("SKILL_CONTEXT_LIMIT", timeout=5000)
            if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
            expect(page.get_by_test_id("run-state")).to_have_text("idle")
            page.get_by_test_id("nav-extensions").click()
            page.get_by_test_id("skill-select-s31-huge").uncheck()

            # 3. Valid skill selection runs end-to-end and surfaces Skill source path + sha256
            cb.check()
            page.get_by_test_id("nav-chat").click()
            page.locator('[data-testid^="offer-confirm-btn-"]').last.click()

            deadline = time.monotonic() + 25.0
            decided: set[str] = set()
            while time.monotonic() < deadline:
                if page.get_by_test_id("run-state").text_content() == "succeeded":
                    break
                buttons = page.locator('[data-testid^="proposal-approve-"]')
                for i in range(buttons.count()):
                    btn = buttons.nth(i)
                    tid = btn.get_attribute("data-testid") or ""
                    if tid in decided or not btn.is_visible() or btn.is_disabled():
                        continue
                    pid = tid.removeprefix("proposal-approve-")
                    trust = page.get_by_test_id(f"proposal-trust-{pid}")
                    if trust.count() > 0 and trust.is_visible() and not trust.is_checked():
                        trust.check()
                    btn.click()
                    decided.add(tid)
                page.wait_for_timeout(100)

            if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
            expect(page.get_by_test_id("run-state")).to_have_text("succeeded", timeout=10000)
            if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
            expect(page.get_by_test_id("verification")).to_have_text("passed")
            page.get_by_test_id("nav-extensions").click()
            page.get_by_test_id("ext-tab-mcp").click()
            expect(page.get_by_test_id("extension-result")).to_contain_text("fix-calc")
            expect(page.get_by_test_id("extension-result")).to_contain_text(".forecontract/skills/fix-calc/SKILL.md")
            # The actual file must equal the value parsed from the real skill request (causal, not a fixed constant).
            assert observed_fix31 and observed_fix31[0] in (proj / "calc.py").read_text(encoding="utf-8")
            assert observed_fix31[0] == f"return a + b  # UQ{s31_uniq}"
        finally:
            browser.close()

    # F04-S31 second scenario: the browser explicitly rejects the Skill script (marker 0 / no start), then a new
    # proposal is explicitly approved (marker appears) and the following real model request carries the script output.
    proj_sc = create_buggy_calc_project(tmp_path / "proj_sc")
    marker_sc = proj_sc / "script_marker.txt"
    started_sc = proj_sc / "script_started.txt"
    make_skill(
        proj_sc,
        "script-skill",
        "# Script skill\nExecute helper.py for the fix.\n",
        references={
            "helper.py": (
                "from pathlib import Path\n"
                "Path('script_started.txt').write_text('STARTED', encoding='utf-8')\n"
                "Path('script_marker.txt').write_text('MARKER_OK', encoding='utf-8')\n"
                "print('SCRIPT_STDOUT_OK')\n"
            )
        },
    )
    cfg_sc = make_extension_host_config(tmp_path / "host_sc", allowed_roots=(proj_sc,))
    script_obs = {"proposed": 0, "saw_output": False}

    def _decide_sc(step: int, req: ModelRequest, state: dict[str, Any]) -> str:
        _ = req
        last = state.get("last_tool_result") or {}
        cmd_payload = {
            "executable": sys.executable,
            "argv": ["-B", ".forecontract/skills/script-skill/helper.py"],
            "cwd": ".",
            "timeout_ms": 5000,
            "purpose": "diagnostic",
            "explanation": "run skill helper script",
        }
        if last.get("action_type") == "propose_command":
            if last.get("tool_succeeded") is True:
                return ext_forecast(
                    action_type="start_command",
                    payload={"proposal_id": last["proposal_id"], "proposal_digest": last["digest"]},
                )
            script_obs["proposed"] += 1
            return ext_forecast(action_type="propose_command", payload=cmd_payload)
        if last.get("action_type") == "start_command" and last.get("command_id"):
            return ext_forecast(action_type="poll_command", payload={"command_id": last["command_id"]})
        if last.get("action_type") == "poll_command" and "SCRIPT_STDOUT_OK" in (last.get("stdout_excerpt") or ""):
            script_obs["saw_output"] = True
            return "DONE_SC"
        return ext_forecast(action_type="propose_command", payload=cmd_payload)

    with (
        LiveTcpTestServer(
            config=cfg_sc,
            provider_factory=lambda *, project, run_id: CallbackProjectProvider(_decide_sc),
        ) as srv_sc,
        sync_playwright() as pw2,
    ):
        srv_sc.app.state.service._chat_provider_factory = lambda **_: (
            lambda req: {"action": "execution_offer", "goal": req.question, "summary": "执行任务"}
        )
        browser2 = pw2.chromium.launch(headless=True)
        try:
            page2 = browser2.new_page()
            page2.goto(f"{srv_sc.base_url}/#token={cfg_sc.access_token}", wait_until="domcontentloaded")
            page2.get_by_test_id("nav-projects").click()
            page2.get_by_test_id("register-root").fill(str(proj_sc))
            page2.get_by_test_id("project-name").fill("s31-sc-proj")
            page2.get_by_test_id("confirm-read").check()
            page2.get_by_test_id("register-project").click()
            expect(page2.get_by_test_id("current-project")).not_to_have_text("-")
            page2.get_by_test_id("nav-extensions").click()
            page2.get_by_test_id("skill-select-script-skill").check()
            page2.get_by_test_id("nav-chat").click()
            page2.get_by_test_id("chat-input").fill("run skill script explicitly")
            page2.get_by_test_id("chat-send").click()
            page2.locator('[data-testid^="offer-confirm-scope-"]').last.check()
            page2.locator('[data-testid^="offer-confirm-btn-"]').last.click()

            # 1. Explicitly reject the first script proposal -> no process, no marker, and the real runner start
            # interface is never invoked (0 start), observed by forwarding the actual start_approved_command.
            from unittest.mock import patch

            from forecontract_app.project_commands import ProjectCommandRunner

            start_calls_sc: list[str] = []
            orig_start = ProjectCommandRunner.start_approved_command

            def _counting_start(self: Any, **kw: Any) -> Any:
                start_calls_sc.append(str(kw.get("proposal_id") or ""))
                return orig_start(self, **kw)

            reject_btn = page2.locator('[data-testid^="proposal-reject-"]').first
            expect(reject_btn).to_be_visible(timeout=10000)
            with patch.object(ProjectCommandRunner, "start_approved_command", _counting_start):
                reject_btn.click()
                page2.wait_for_timeout(300)
            assert not marker_sc.exists()
            assert not started_sc.exists()
            assert start_calls_sc == []
        finally:
            browser2.close()

    # F04-S31 approve leg: propose a verification plan then the script command; the browser approves both (with
    # trust on the command), the marker appears, and the following real request carries the script output.
    proj_ap = create_buggy_calc_project(tmp_path / "proj_ap")
    marker_ap = proj_ap / "script_marker.txt"
    started_ap = proj_ap / "script_started.txt"
    make_skill(
        proj_ap,
        "script-skill",
        "# Script skill\nExecute helper.py for the fix.\n",
        references={
            "helper.py": (
                "from pathlib import Path\n"
                "Path('script_started.txt').write_text('STARTED', encoding='utf-8')\n"
                "Path('script_marker.txt').write_text('MARKER_OK', encoding='utf-8')\n"
                "print('SCRIPT_STDOUT_OK')\n"
            )
        },
    )
    cfg_ap = make_extension_host_config(tmp_path / "host_ap", allowed_roots=(proj_ap,))
    ap_obs = {"saw_output": False}
    ap_captured: list[tuple[str, ModelRequest]] = []
    started_cid_ap: list[str] = []

    def _decide_ap(step: int, req: ModelRequest, state: dict[str, Any]) -> str:
        last = state.get("last_tool_result") or {}
        cmd_payload = {
            "executable": sys.executable,
            "argv": ["-B", ".forecontract/skills/script-skill/helper.py"],
            "cwd": ".",
            "timeout_ms": 5000,
            "purpose": "diagnostic",
            "explanation": "run skill helper script",
        }
        if step == 1:
            return ext_forecast(
                action_type="propose_verification",
                payload={
                    "plan_id": "p1",
                    "summary": "exit check",
                    "checks": [
                        {
                            "check_id": "chk",
                            "kind": "exit_code",
                            "executable": sys.executable,
                            "argv": ["-B", "-c", "print('ok')"],
                            "cwd": ".",
                            "timeout_ms": 5000,
                        }
                    ],
                },
            )
        if step == 2:
            return ext_forecast(action_type="propose_command", payload=cmd_payload)
        if last.get("action_type") == "propose_command" and last.get("tool_succeeded") is True:
            return ext_forecast(
                action_type="start_command",
                payload={"proposal_id": last["proposal_id"], "proposal_digest": last["digest"]},
            )
        if last.get("action_type") == "start_command" and last.get("command_id"):
            started_cid_ap.append(str(last["command_id"]))
            return ext_forecast(action_type="poll_command", payload={"command_id": last["command_id"]})
        if last.get("action_type") == "poll_command" and "SCRIPT_STDOUT_OK" in (last.get("stdout_excerpt") or ""):
            ap_obs["saw_output"] = True
            ap_captured.append((str(last.get("command_id") or ""), req))
            return "DONE_AP"
        return ext_forecast(action_type="poll_command", payload={"command_id": last.get("command_id") or "cmd-0001"})

    with (
        LiveTcpTestServer(
            config=cfg_ap,
            provider_factory=lambda *, project, run_id: CallbackProjectProvider(_decide_ap),
        ) as srv_ap,
        sync_playwright() as pw_ap,
    ):
        srv_ap.app.state.service._chat_provider_factory = lambda **_: (
            lambda req: {"action": "execution_offer", "goal": req.question, "summary": "执行任务"}
        )
        browser_ap = pw_ap.chromium.launch(headless=True)
        try:
            page_ap = browser_ap.new_page()
            page_ap.goto(f"{srv_ap.base_url}/#token={cfg_ap.access_token}", wait_until="domcontentloaded")
            page_ap.get_by_test_id("nav-projects").click()
            page_ap.get_by_test_id("register-root").fill(str(proj_ap))
            page_ap.get_by_test_id("project-name").fill("s31-ap-proj")
            page_ap.get_by_test_id("confirm-read").check()
            page_ap.get_by_test_id("register-project").click()
            expect(page_ap.get_by_test_id("current-project")).not_to_have_text("-")
            page_ap.get_by_test_id("nav-extensions").click()
            page_ap.get_by_test_id("skill-select-script-skill").check()
            page_ap.get_by_test_id("nav-chat").click()
            page_ap.get_by_test_id("chat-input").fill("approve skill script explicitly")
            page_ap.get_by_test_id("chat-send").click()
            page_ap.locator('[data-testid^="offer-confirm-scope-"]').last.check()
            page_ap.locator('[data-testid^="offer-confirm-btn-"]').last.click()

            # Approve every pending proposal; the command proposal requires the trust checkbox.
            approved_ap: set[str] = set()
            deadline_ap = time.monotonic() + 20.0
            while time.monotonic() < deadline_ap:
                if marker_ap.exists() and marker_ap.read_text(encoding="utf-8") == "MARKER_OK":
                    break
                btns = page_ap.locator('[data-testid^="proposal-approve-"]')
                for i in range(btns.count()):
                    btn = btns.nth(i)
                    tid = btn.get_attribute("data-testid") or ""
                    if tid in approved_ap or not btn.is_visible() or btn.is_disabled():
                        continue
                    pid_ap = tid.removeprefix("proposal-approve-")
                    trust_ap = page_ap.get_by_test_id(f"proposal-trust-{pid_ap}")
                    if trust_ap.count() > 0 and trust_ap.is_visible() and not trust_ap.is_checked():
                        trust_ap.check()
                    btn.click()
                    approved_ap.add(tid)
                page_ap.wait_for_timeout(100)

            assert marker_ap.exists()
            assert marker_ap.read_text(encoding="utf-8") == "MARKER_OK"
            assert started_ap.exists() and started_ap.read_text(encoding="utf-8") == "STARTED"
            # The subsequent real ModelRequest must carry the command_id's stdout (do not exit at the marker alone).
            deadline_out = time.monotonic() + 10.0
            while time.monotonic() < deadline_out and not ap_captured:
                page_ap.wait_for_timeout(50)
            assert ap_captured, "no subsequent ModelRequest carried the script stdout"
            cmd_id_ap, req_ap = ap_captured[0]
            assert cmd_id_ap
            # The poll result's command_id must match the independently-saved start_command identity (not self-compare).
            assert started_cid_ap and started_cid_ap[0] == cmd_id_ap
            ltr_ap = req_ap.observation.state["last_tool_result"]
            assert ltr_ap["command_id"] == cmd_id_ap
            assert "SCRIPT_STDOUT_OK" in (ltr_ap.get("stdout_excerpt") or "")
        finally:
            browser_ap.close()

def test_s32_stdio_browser(tmp_path: Path) -> None:
    proj = create_buggy_calc_project(tmp_path / "proj")
    s32_uniq = uuid.uuid4().hex[:12]
    log_s = tmp_path / "logs" / "s32.jsonl"
    sc_s = write_stdio_mcp_script(
        tmp_path / "scripts", name="s32.py", log_path=log_s, label="stdio-32", fix_expr=f"a + b  # UQ{s32_uniq}"
    )
    ext_cfg = write_extension_config_file(
        tmp_path / "ext32.json",
        servers=[{"id": "srv-stdio32", "transport": "stdio", "executable": sys.executable, "argv": ["-B", str(sc_s)], "cwd": str(tmp_path)}],
    )
    cfg = make_extension_host_config(tmp_path / "host", allowed_roots=(proj,), extension_config_path=ext_cfg)
    observed_mcp_fix: list[str] = []

    def _decide(step: int, req: ModelRequest, state: dict[str, Any]) -> str:
        _ = req
        last = state.get("last_tool_result") or {}
        if last.get("action_type") == "call_mcp" and last.get("tool_name") == "calc_hint":
            m = re.search(r"FIX:\s*(return [^(]+)", str(last.get("text") or ""))
            if m:
                observed_mcp_fix.append(m.group(1).strip())
        if step == 1:
            return ext_forecast(action_type="connect_mcp", payload={"server_id": "srv-stdio32"})
        if step == 2:
            return ext_forecast(action_type="list_mcp_tools", payload={"server_id": "srv-stdio32"})
        if step == 3:
            return ext_forecast(action_type="call_mcp", payload={"server_id": "srv-stdio32", "tool_name": "calc_hint", "arguments": {"target": "calc.py"}})
        if step == 4:
            if not observed_mcp_fix:
                return "MISSING_STDIO_MCP_HINT_ABORT"
            return ext_forecast(
                action_type="propose_scope",
                payload={"exact_paths": ["calc.py"], "write_prefixes": [], "operations": ["modify"], "protected_paths": ["tests/test_calc.py"], "verification_plan_summary": "pytest junit"},
            )
        if step == 5:
            return ext_forecast(
                action_type="propose_verification",
                payload={
                    "plan_id": "p32",
                    "summary": "run calc pytest",
                    "checks": [
                        {
                            "check_id": "unit",
                            "kind": "junit_exact",
                            "executable": sys.executable,
                            "argv": ["-B", "-m", "pytest", "-p", "no:cacheprovider", "tests/test_calc.py", "--junitxml={result_dir}/junit.xml"],
                            "cwd": ".",
                            "timeout_ms": 20000,
                            "protected_paths": ["tests/test_calc.py"],
                            "expected_tests": [{"classname": "tests.test_calc", "name": "test_add_basic"}],
                        }
                    ],
                },
            )
        if step == 6:
            return ext_forecast(action_type="read_file", payload={"path": "calc.py"})
        if last.get("action_type") == "read_file" and (last.get("sha256") or last.get("file_sha256")):
            if not observed_mcp_fix:
                return "MISSING_STDIO_MCP_HINT_ABORT"
            file_sha = last.get("sha256") or last.get("file_sha256")
            return ext_forecast(
                action_type="edit_file",
                payload={"path": "calc.py", "expected_sha256": file_sha, "old_text": "return a - b", "new_text": observed_mcp_fix[-1]},
                outcome_type="project_file_written",
                outcome_payload={"path": "calc.py"},
            )
        return ext_forecast(
            action_type="verify_project",
            payload={"plan_id": state["plan_id"], "plan_digest": state["plan_digest"]},
            outcome_type="project_task_verified",
            outcome_payload={},
            task_hint=True,
        )

    with LiveTcpTestServer(config=cfg, provider_factory=lambda *, project, run_id: CallbackProjectProvider(_decide)) as srv, sync_playwright() as pw:
        if getattr(srv.app.state.service, "_chat_provider_factory", None) is None:
            srv.app.state.service._chat_provider_factory = lambda **_: (lambda req: {"action": "execution_offer", "goal": req.question, "summary": "执行任务"})
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(f"{srv.base_url}/#token={cfg.access_token}", wait_until="domcontentloaded")
            page.get_by_test_id("nav-extensions").click()
            page.get_by_test_id("ext-tab-mcp").click()
            expect(page.get_by_test_id("mcp-server-list")).to_contain_text("srv-stdio32")
            expect(page.get_by_test_id("mcp-server-list")).to_contain_text("state=not_connected")
            # Opening UI never auto-spawns stdio MCP server
            assert len(read_stdio_log(log_s)) == 0

            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id("register-root").fill(str(proj))
            page.get_by_test_id("project-name").fill("s32-proj")
            page.get_by_test_id("confirm-read").check()
            page.get_by_test_id("register-project").click()
            expect(page.get_by_test_id("current-project")).not_to_have_text("-")
            page.get_by_test_id("nav-chat").click()
            page.get_by_test_id("chat-input").fill("fix via stdio mcp")
            page.get_by_test_id("chat-send").click()
            page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
            page.locator('[data-testid^="offer-confirm-btn-"]').last.click()

            deadline = time.monotonic() + 25.0
            decided: set[str] = set()
            saw_ext_card_details = False
            while time.monotonic() < deadline:
                if page.get_by_test_id("run-state").text_content() == "succeeded":
                    break
                ext_cards = page.locator('[data-testid^="extension-proposal-"]')
                for i in range(ext_cards.count()):
                    ctext = ext_cards.nth(i).text_content() or ""
                    if "srv-stdio32" in ctext and "exe_sha=" in ctext and "受信任本机执行" in ctext:
                        saw_ext_card_details = True
                for prefix in ("extension-approve-", "proposal-approve-"):
                    btns = page.locator(f'[data-testid^="{prefix}"]')
                    for i in range(btns.count()):
                        btn = btns.nth(i)
                        tid = btn.get_attribute("data-testid") or ""
                        if tid in decided or not btn.is_visible() or btn.is_disabled():
                            continue
                        if prefix == "proposal-approve-":
                            pid = tid.removeprefix(prefix)
                            trust = page.get_by_test_id(f"proposal-trust-{pid}")
                            if trust.count() > 0 and trust.is_visible() and not trust.is_checked():
                                trust.check()
                        btn.click()
                        decided.add(tid)
                page.wait_for_timeout(100)

            assert saw_ext_card_details is True
            assert len(observed_mcp_fix) == 1 and observed_mcp_fix[0] == f"return a + b  # UQ{s32_uniq}"
            if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
            expect(page.get_by_test_id("run-state")).to_have_text("succeeded", timeout=10000)
            page.get_by_test_id("nav-extensions").click()
            page.get_by_test_id("ext-tab-mcp").click()
            expect(page.get_by_test_id("extension-result")).to_contain_text(f"FIX: return a + b  # UQ{s32_uniq}")
            call_items = page.locator('[data-testid^="extension-call-mcpc-"]')
            assert call_items.count() >= 1
            call_text = call_items.first.text_content() or ""
            assert "status=success" in call_text and "effect=possibly_applied" in call_text
            assert observed_mcp_fix and observed_mcp_fix[0] in (proj / "calc.py").read_text(encoding="utf-8")
            page.get_by_test_id("nav-chat").click()
            if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
            expect(page.get_by_test_id("verification")).to_have_text("passed")
        finally:
            browser.close()


def test_s33_http_browser(tmp_path: Path) -> None:
    proj = create_buggy_calc_project(tmp_path / "proj")
    s33_uniq = uuid.uuid4().hex[:12]
    with LocalHttpMcpServer(sse_mode=True, label="http33", fix_expr=f"a + b  # UQ{s33_uniq}") as http_mcp:
        ext_cfg = write_extension_config_file(
            tmp_path / "ext33.json",
            servers=[{"id": "srv-http33", "transport": "http", "url": http_mcp.url}],
        )
        cfg = make_extension_host_config(tmp_path / "host", allowed_roots=(proj,), extension_config_path=ext_cfg)
        saw_rejection: list[bool] = []
        observed_http_fix: list[str] = []

        def _decide(step: int, req: ModelRequest, state: dict[str, Any]) -> str:
            _ = req
            last = state.get("last_tool_result") or {}
            if last.get("action_type") == "call_mcp" and last.get("error_code") == "PROPOSAL_DENIED":
                saw_rejection.append(True)
            if last.get("action_type") == "call_mcp" and last.get("tool_name") == "calc_hint":
                m = re.search(r"http33:\s*(return .+)", str(last.get("text") or ""))
                if m:
                    observed_http_fix.append(m.group(1))
            if step == 1:
                return ext_forecast(action_type="connect_mcp", payload={"server_id": "srv-http33"})
            if step == 2:
                return ext_forecast(action_type="list_mcp_tools", payload={"server_id": "srv-http33"})
            if step == 3:
                return ext_forecast(action_type="call_mcp", payload={"server_id": "srv-http33", "tool_name": "echo_tool", "arguments": {"msg": "REJECT_THIS"}})
            if step == 4:
                if not saw_rejection:
                    return "MISSING_REJECTION_FEEDBACK_ABORT"
                return ext_forecast(action_type="call_mcp", payload={"server_id": "srv-http33", "tool_name": "calc_hint", "arguments": {"target": "calc.py"}})
            if step == 5:
                if not observed_http_fix:
                    return "MISSING_HTTP_MCP_HINT_ABORT"
                return ext_forecast(
                    action_type="propose_scope",
                    payload={"exact_paths": ["calc.py"], "write_prefixes": [], "operations": ["modify"], "protected_paths": ["tests/test_calc.py"], "verification_plan_summary": "pytest junit"},
                )
            if step == 6:
                return ext_forecast(
                    action_type="propose_verification",
                    payload={
                        "plan_id": "p33",
                        "summary": "run calc pytest",
                        "checks": [
                            {
                                "check_id": "unit",
                                "kind": "junit_exact",
                                "executable": sys.executable,
                                "argv": ["-B", "-m", "pytest", "-p", "no:cacheprovider", "tests/test_calc.py", "--junitxml={result_dir}/junit.xml"],
                                "cwd": ".",
                                "timeout_ms": 20000,
                                "protected_paths": ["tests/test_calc.py"],
                                "expected_tests": [{"classname": "tests.test_calc", "name": "test_add_basic"}],
                            }
                        ],
                    },
                )
            if step == 7:
                return ext_forecast(action_type="read_file", payload={"path": "calc.py"})
            if last.get("action_type") == "read_file" and (last.get("sha256") or last.get("file_sha256")):
                if not observed_http_fix:
                    return "MISSING_HTTP_MCP_HINT_ABORT"
                file_sha = last.get("sha256") or last.get("file_sha256")
                return ext_forecast(
                    action_type="edit_file",
                    payload={"path": "calc.py", "expected_sha256": file_sha, "old_text": "return a - b", "new_text": observed_http_fix[-1]},
                    outcome_type="project_file_written",
                    outcome_payload={"path": "calc.py"},
                )
            return ext_forecast(
                action_type="verify_project",
                payload={"plan_id": state["plan_id"], "plan_digest": state["plan_digest"]},
                outcome_type="project_task_verified",
                outcome_payload={},
                task_hint=True,
            )

        with LiveTcpTestServer(config=cfg, provider_factory=lambda *, project, run_id: CallbackProjectProvider(_decide)) as srv, sync_playwright() as pw:
            if getattr(srv.app.state.service, "_chat_provider_factory", None) is None:
                srv.app.state.service._chat_provider_factory = lambda **_: (lambda req: {"action": "execution_offer", "goal": req.question, "summary": "执行任务"})
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(f"{srv.base_url}/#token={cfg.access_token}", wait_until="domcontentloaded")
                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id("register-root").fill(str(proj))
                page.get_by_test_id("project-name").fill("s33-proj")
                page.get_by_test_id("confirm-read").check()
                page.get_by_test_id("register-project").click()
                expect(page.get_by_test_id("current-project")).not_to_have_text("-")
                page.get_by_test_id("nav-chat").click()
                page.get_by_test_id("chat-input").fill("http sse browser test")
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                page.locator('[data-testid^="offer-confirm-btn-"]').last.click()

                deadline = time.monotonic() + 25.0
                decided: set[str] = set()
                while time.monotonic() < deadline:
                    if page.get_by_test_id("run-state").text_content() == "succeeded":
                        break
                    ext_cards = page.locator('[data-testid^="extension-proposal-"]')
                    for i in range(ext_cards.count()):
                        card = ext_cards.nth(i)
                        card_tid = card.get_attribute("data-testid") or ""
                        pid = card_tid.removeprefix("extension-proposal-")
                        if card_tid in decided:
                            continue
                        txt = card.text_content() or ""
                        if "REJECT_THIS" in txt:
                            rej = page.get_by_test_id(f"extension-reject-{pid}")
                            if rej.count() > 0 and rej.is_visible():
                                rej.click()
                                decided.add(card_tid)
                        else:
                            app = page.get_by_test_id(f"extension-approve-{pid}")
                            if app.count() > 0 and app.is_visible():
                                app.click()
                                decided.add(card_tid)
                    pbtns = page.locator('[data-testid^="proposal-approve-"]')
                    for i in range(pbtns.count()):
                        btn = pbtns.nth(i)
                        tid = btn.get_attribute("data-testid") or ""
                        if tid in decided or not btn.is_visible() or btn.is_disabled():
                            continue
                        pid = tid.removeprefix("proposal-approve-")
                        trust = page.get_by_test_id(f"proposal-trust-{pid}")
                        if trust.count() > 0 and trust.is_visible() and not trust.is_checked():
                            trust.check()
                        btn.click()
                        decided.add(tid)
                    page.wait_for_timeout(100)

                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-state")).to_have_text("succeeded", timeout=10000)
                page.get_by_test_id("nav-extensions").click()
                page.get_by_test_id("ext-tab-mcp").click()
                expect(page.get_by_test_id("extension-result")).to_contain_text("http33")
                assert http_mcp.call_count == 1
                assert observed_http_fix and observed_http_fix[0] in (proj / "calc.py").read_text(encoding="utf-8")
                page.get_by_test_id("nav-chat").click()
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("verification")).to_have_text("passed")
            finally:
                browser.close()


def test_s34_stale_ui(tmp_path: Path) -> None:
    proj_a = create_buggy_calc_project(tmp_path / "proj_a")
    proj_b = create_buggy_calc_project(tmp_path / "proj_b")
    make_skill(proj_a, "skill-a", "SKILL_ONLY_A")
    make_skill(proj_b, "skill-b", "SKILL_ONLY_B")

    with LocalHttpMcpServer(label="s34-http") as http_mcp:
        ext_cfg = write_extension_config_file(
            tmp_path / "ext34.json",
            servers=[{"id": "srv-34", "transport": "http", "url": http_mcp.url}],
        )
        cfg = make_extension_host_config(
            tmp_path / "host", allowed_roots=(proj_a, proj_b), extension_config_path=ext_cfg
        )

        def _decide(step: int, req: ModelRequest, state: dict[str, Any]) -> str:
            _ = req, state
            if step == 1:
                return ext_forecast(action_type="connect_mcp", payload={"server_id": "srv-34"})
            return "DONE_S34"

        with (
            LiveTcpTestServer(
                config=cfg,
                provider_factory=lambda *, project, run_id: CallbackProjectProvider(_decide),
            ) as srv,
            sync_playwright() as pw,
        ):
            srv.app.state.service._chat_provider_factory = lambda **_: (
                lambda req: {"action": "execution_offer", "goal": req.question, "summary": "执行任务"}
            )
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.goto(f"{srv.base_url}/#token={cfg.access_token}", wait_until="domcontentloaded")
                page.get_by_test_id("nav-projects").click()
                for idx, (rp, name) in enumerate([(proj_a, "proj-a"), (proj_b, "proj-b")]):
                    page.get_by_test_id("register-root").fill(str(rp))
                    page.get_by_test_id("project-name").fill(name)
                    if idx == 0:
                        page.get_by_test_id("confirm-read").check()
                    page.get_by_test_id("register-project").click()
                    expect(page.get_by_test_id("project-list")).to_contain_text(name)

                post_requests: list[str] = []
                page.on(
                    "request",
                    lambda req: post_requests.append(f"{req.method} {req.url}")
                    if req.method not in ("GET", "HEAD")
                    else None,
                )

                def _record_b_fields() -> dict[str, str]:
                    return {
                        "project": page.get_by_test_id("current-project").text_content(),
                        "session": page.get_by_test_id("current-session").text_content(),
                        "run_id": page.get_by_test_id("run-id").text_content(),
                        "run_state": page.get_by_test_id("run-state").text_content(),
                        "goal": page.get_by_test_id("chat-input").input_value(),
                        "error": page.get_by_test_id("error").text_content(),
                    }

                def _assert_fields_same(before: dict[str, str]) -> None:
                    assert page.get_by_test_id("current-project").text_content() == before["project"]
                    assert page.get_by_test_id("current-session").text_content() == before["session"]
                    assert page.get_by_test_id("run-id").text_content() == before["run_id"]
                    assert page.get_by_test_id("run-state").text_content() == before["run_state"]
                    assert page.get_by_test_id("chat-input").input_value() == before["goal"]
                    assert page.get_by_test_id("error").text_content() == before["error"]

                # 1. Intercept /skills via page.route and hold proj-a's response until after switching to proj-b
                routed_skill_urls: list[str] = []
                page.route(
                    re.compile(r".*/api/v1/projects/.*/skills$"),
                    lambda route: (routed_skill_urls.append(route.request.url), route.continue_()),
                )
                # Body gate: the fetch has already returned its Response; the gate holds the JSON/text body
                # consumption (Response.json/text), which is what the contract requires (not a delayed fetch).
                page.evaluate(
                    """() => {
                        const origFetch = window.fetch.bind(window);
                        window.__staleGateArmed = true;
                        window.__releaseStaleSkills = null;
                        window.fetch = async function(input, init) {
                            const url = typeof input === "string" ? input : input.url;
                            const resp = await origFetch(input, init);
                            if (window.__staleGateArmed && url.endsWith("/skills")) {
                                window.__staleGateArmed = false;
                                const origJson = resp.json.bind(resp);
                                const origText = resp.text.bind(resp);
                                let gated = false;
                                resp.json = async () => {
                                    if (!gated) {
                                        gated = true;
                                        await new Promise((resolve) => { window.__releaseStaleSkills = resolve; });
                                    }
                                    return origJson();
                                };
                                resp.text = async () => {
                                    if (!gated) {
                                        gated = true;
                                        await new Promise((resolve) => { window.__releaseStaleSkills = resolve; });
                                    }
                                    return origText();
                                };
                            }
                            return resp;
                        };
                    }"""
                )

                p_items = page.locator('[data-testid^="project-item-"] button')
                p_items.nth(0).click()
                # Wait until proj-a's /skills fetch is held at the gate
                page.wait_for_function("() => typeof window.__releaseStaleSkills === 'function'", timeout=5000)
                # Switch to proj-b before proj-a's /skills response resolves
                p_items.nth(1).click()
                page.get_by_test_id("nav-extensions").click()
                expect(page.get_by_test_id("skill-select-skill-b")).to_be_visible(timeout=5000)
                b_fields_before = _record_b_fields()
                body_waiting = page.evaluate("() => typeof window.__releaseStaleSkills === 'function'")
                assert body_waiting is True
                # Body-waiting confirmed: release A's gated body and require B's fields to stay identical.
                skill_post_before = len(post_requests)
                page.evaluate("() => window.__releaseStaleSkills && window.__releaseStaleSkills()")
                page.wait_for_timeout(150)
                assert len(post_requests) == skill_post_before
                expect(page.get_by_test_id("skill-select-skill-b")).to_be_visible()
                assert page.get_by_test_id("skill-select-skill-a").count() == 0
                assert len(routed_skill_urls) >= 2
                _assert_fields_same(b_fields_before)

                # 2. Rapid double-click on extension approve button sends exactly 1 POST .../decision
                decision_posts: list[str] = []
                page.route(
                    re.compile(r".*/api/v1/sessions/.*/runs/.*/extensions/proposals/.*/decision$"),
                    lambda route: (decision_posts.append(route.request.url), route.continue_()),
                )
                page.get_by_test_id("nav-chat").click()
                page.get_by_test_id("chat-input").fill("s34 single flight decision")
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                page.locator('[data-testid^="offer-confirm-btn-"]').last.click()

                approve_btn = page.locator('[data-testid^="extension-approve-"]').first
                expect(approve_btn).to_be_visible(timeout=10000)
                # Fire two synchronous clicks in the same turn to test single-flight lock
                approve_btn.evaluate("el => { el.click(); el.click(); }")
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-state")).to_have_text("failed", timeout=10000)
                assert len(decision_posts) == 1
                page.unroute_all()

                # 3. Extension-result body gate: hold /extensions response body consumption, switch to B,
                # record B fields, release, and require B unchanged (no auto-POST).
                page.evaluate(
                    """() => {
                        const origFetch = window.fetch.bind(window);
                        window.__gateExtArmed = true;
                        window.__releaseExt = null;
                        window.fetch = async function(input, init) {
                            const url = typeof input === "string" ? input : input.url;
                            const resp = await origFetch(input, init);
                            if (window.__gateExtArmed && url.includes("/extensions") && (!init || init.method !== "POST")) {
                                window.__gateExtArmed = false;
                                const oj = resp.json.bind(resp);
                                const ot = resp.text.bind(resp);
                                let gated = false;
                                resp.json = async () => { if (!gated) { gated = true; await new Promise((r) => { window.__releaseExt = r; }); } return oj(); };
                                resp.text = async () => { if (!gated) { gated = true; await new Promise((r) => { window.__releaseExt = r; }); } return ot(); };
                            }
                            return resp;
                        };
                    }"""
                )
                # Trigger an extension-result fetch on the current project (A = proj-b), then switch to proj-a (B)
                # while A's /extensions body is still gated.
                page.get_by_test_id("chat-input").fill("s34 extension result gate")
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                page.locator('[data-testid^="offer-confirm-btn-"]').last.click()
                page.wait_for_function("() => typeof window.__releaseExt === 'function'", timeout=8000)
                page.get_by_test_id("nav-projects").click()
                p_items.nth(0).click()
                page.get_by_test_id("nav-extensions").click()
                expect(page.get_by_test_id("skill-select-skill-a")).to_be_visible(timeout=5000)
                ext_b_before = _record_b_fields()
                ext_post_before = len(post_requests)
                page.evaluate("() => window.__releaseExt && window.__releaseExt()")
                page.wait_for_timeout(150)
                assert len(post_requests) == ext_post_before
                _assert_fields_same(ext_b_before)

                # 4. Approval-response body gate: hold the /decision POST response body consumption, then release.
                page.evaluate(
                    """() => {
                        const origFetch = window.fetch.bind(window);
                        window.__gateDecArmed = true;
                        window.__releaseDec = null;
                        window.fetch = async function(input, init) {
                            const url = typeof input === "string" ? input : input.url;
                            const resp = await origFetch(input, init);
                            if (window.__gateDecArmed && url.includes("/decision") && init && init.method === "POST") {
                                window.__gateDecArmed = false;
                                const oj = resp.json.bind(resp);
                                let gated = false;
                                resp.json = async () => { if (!gated) { gated = true; await new Promise((r) => { window.__releaseDec = r; }); } return oj(); };
                            }
                            return resp;
                        };
                    }"""
                )
                # Start a fresh run so a pending proposal exists for the approval gate (A = proj-a).
                page.get_by_test_id("nav-chat").click()
                page.get_by_test_id("chat-input").fill("s34 approval gate")
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                page.locator('[data-testid^="offer-confirm-btn-"]').last.click()
                dec_btn = page.locator('[data-testid^="extension-approve-"]').first
                expect(dec_btn).to_be_visible(timeout=10000)
                dec_btn.click()
                page.wait_for_function("() => typeof window.__releaseDec === 'function'", timeout=8000)
                # Switch to B (proj-b) while A's /decision body is still gated; record B, release A, verify.
                page.get_by_test_id("nav-projects").click()
                p_items.nth(1).click()
                page.get_by_test_id("nav-extensions").click()
                expect(page.get_by_test_id("skill-select-skill-b")).to_be_visible(timeout=5000)
                dec_b_before = _record_b_fields()
                dec_post_before = len(post_requests)
                page.evaluate("() => window.__releaseDec && window.__releaseDec()")
                page.wait_for_timeout(150)
                assert len(post_requests) == dec_post_before
                _assert_fields_same(dec_b_before)
                page.unroute_all()
            finally:
                browser.close()


def test_s35_cancel_reload(tmp_path: Path) -> None:
    import json
    import threading

    from extension_helpers import is_pid_alive

    proj = create_buggy_calc_project(tmp_path / "proj")
    ready_file = tmp_path / "ready_s35.json"
    log_s = tmp_path / "logs" / "s35.jsonl"
    sc_s = write_stdio_mcp_script(
        tmp_path / "scripts", name="s35.py", log_path=log_s, mode="hang_with_child", ready_file=ready_file
    )
    ext_cfg = write_extension_config_file(
        tmp_path / "ext35.json",
        servers=[{"id": "srv-hang35", "transport": "stdio", "executable": sys.executable, "argv": ["-B", str(sc_s)], "cwd": str(tmp_path)}],
    )
    cfg = make_extension_host_config(tmp_path / "host", allowed_roots=(proj,), extension_config_path=ext_cfg)

    def _decide(step: int, req: ModelRequest, state: dict[str, Any]) -> str:
        _ = req, state
        if step == 1:
            return ext_forecast(action_type="connect_mcp", payload={"server_id": "srv-hang35"})
        if step == 2:
            return ext_forecast(action_type="list_mcp_tools", payload={"server_id": "srv-hang35"})
        return ext_forecast(action_type="call_mcp", payload={"server_id": "srv-hang35", "tool_name": "echo_tool", "arguments": {"msg": "hang35"}})

    with LiveTcpTestServer(config=cfg, provider_factory=lambda *, project, run_id: CallbackProjectProvider(_decide)) as srv, sync_playwright() as pw:
        if getattr(srv.app.state.service, "_chat_provider_factory", None) is None:
            srv.app.state.service._chat_provider_factory = lambda **_: (lambda req: {"action": "execution_offer", "goal": req.question, "summary": "执行任务"})
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.goto(f"{srv.base_url}/#token={cfg.access_token}", wait_until="domcontentloaded")
            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id("register-root").fill(str(proj))
            page.get_by_test_id("project-name").fill("s35-proj")
            page.get_by_test_id("confirm-read").check()
            page.get_by_test_id("register-project").click()
            expect(page.get_by_test_id("current-project")).not_to_have_text("-")
            page.get_by_test_id("nav-chat").click()
            page.get_by_test_id("chat-input").fill("reload while hung call")
            page.get_by_test_id("chat-send").click()
            page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
            page.locator('[data-testid^="offer-confirm-btn-"]').last.click()

            decided: set[str] = set()
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline and len(decided) < 2:
                btns = page.locator('[data-testid^="extension-approve-"]')
                for i in range(btns.count()):
                    btn = btns.nth(i)
                    tid = btn.get_attribute("data-testid") or ""
                    if tid not in decided and btn.is_visible() and not btn.is_disabled():
                        btn.click()
                        decided.add(tid)
                page.wait_for_timeout(80)
            assert len(decided) == 2

            # Wait deterministically for ready_file to confirm child process and tools/call are active
            ready_deadline = time.monotonic() + 5.0
            pids: dict[str, Any] = {}
            while time.monotonic() < ready_deadline:
                if ready_file.exists():
                    try:
                        pids = json.loads(ready_file.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        pids = {}
                    if pids.get("call_started") is True:
                        break
                page.wait_for_timeout(30)
            assert pids.get("call_started") is True

            svc = srv.app.state.service
            run_rec = next(iter(svc._runs_by_id.values()))
            ext_rt = run_rec.extension_runtime
            assert ext_rt is not None
            mcp_client = ext_rt._clients.get("srv-hang35")
            assert mcp_client is not None
            assert mcp_client.active_readers_peak == 1

            tools_calls_before_reload = len(
                [e for e in read_stdio_log(log_s) if e.get("event") == "rpc" and e.get("method") == "tools/call"]
            )
            project_hashes_before_reload = tree_hashes(proj)
            mutation_requests_during_reload: list[str] = []
            page.on(
                "request",
                lambda req: mutation_requests_during_reload.append(f"{req.method} {req.url}")
                if req.method not in ("GET", "HEAD")
                else None,
            )
            # UI event stream (browser SSE subscription) peak sampled across the FULL reload window:
            # before, during the switch, and after; must never exceed 1 active connection.
            ui_samples: list[int] = []
            ui_stop = threading.Event()

            def _sample_ui() -> None:
                while not ui_stop.is_set():
                    ui_samples.append(run_rec.hub.active_subscriber_count)
                    ui_stop.wait(0.02)

            sampler = threading.Thread(target=_sample_ui, daemon=True)
            sampler.start()
            try:
                page.wait_for_timeout(200)
                page.reload(wait_until="domcontentloaded")
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-state")).to_have_text("running", timeout=10000)
                page.wait_for_timeout(200)
            finally:
                ui_stop.set()
                sampler.join(timeout=3)
            assert mutation_requests_during_reload == []
            assert tree_hashes(proj) == project_hashes_before_reload
            assert (
                len([e for e in read_stdio_log(log_s) if e.get("event") == "rpc" and e.get("method") == "tools/call"])
                == tools_calls_before_reload
            )
            assert ui_samples and max(ui_samples) == 1

            spawns_before_cancel = len([e for e in read_stdio_log(log_s) if e.get("event") == "started"])
            page.get_by_test_id("cancel-run").click()
            if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
            expect(page.get_by_test_id("run-state")).to_have_text("cancelled", timeout=10000)
            page.reload(wait_until="domcontentloaded")
            if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
            expect(page.get_by_test_id("run-state")).to_have_text("cancelled", timeout=10000)
            assert len([e for e in read_stdio_log(log_s) if e.get("event") == "started"]) == spawns_before_cancel
            assert not is_pid_alive(int(pids["parent_pid"]))
            assert not is_pid_alive(int(pids["child_pid"]))
            transport = getattr(mcp_client, "_transport", None)
            assert transport is None or transport._active_readers == 0
        finally:
            browser.close()


def test_s36_keyboard_xss(tmp_path: Path) -> None:
    def _tab_to(page: Any, testid: str, max_tabs: int = 60) -> None:
        """Reach ``testid`` using real Tab presses (no focus()); assert activeElement matches."""
        for _ in range(max_tabs):
            if page.evaluate("document.activeElement.getAttribute('data-testid')") == testid:
                return
            page.keyboard.press("Tab")
        raise AssertionError(f"Tab never reached {testid}")

    proj = create_buggy_calc_project(tmp_path / "proj")
    make_skill(proj, "xss-skill", "XSS SKILL <img src=x onerror=window.__xss=9>")
    before_proj_hashes = tree_hashes(proj)
    log_x = tmp_path / "logs" / "s36.jsonl"
    sc_x = write_stdio_mcp_script(tmp_path / "scripts", name="s36.py", log_path=log_x)
    ext_cfg = write_extension_config_file(
        tmp_path / "ext36.json",
        servers=[{"id": "srv-xss", "transport": "stdio", "executable": sys.executable, "argv": ["-B", str(sc_x)], "cwd": str(tmp_path)}],
    )
    cfg = make_extension_host_config(tmp_path / "host", allowed_roots=(proj,), extension_config_path=ext_cfg)

    def _decide(step: int, req: ModelRequest, state: dict[str, Any]) -> str:
        _ = req, state
        if step == 1:
            return ext_forecast(action_type="connect_mcp", payload={"server_id": "srv-xss"})
        if step == 2:
            return ext_forecast(action_type="list_mcp_tools", payload={"server_id": "srv-xss"})
        return ext_forecast(action_type="call_mcp", payload={"server_id": "srv-xss", "tool_name": "malicious_tool", "arguments": {"q": "<script>window.__xss=4</script>"}})

    with LiveTcpTestServer(config=cfg, provider_factory=lambda *, project, run_id: CallbackProjectProvider(_decide)) as srv, sync_playwright() as pw:
        if getattr(srv.app.state.service, "_chat_provider_factory", None) is None:
            srv.app.state.service._chat_provider_factory = lambda **_: (lambda req: {"action": "execution_offer", "goal": req.question, "summary": "执行任务"})
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            non_loopback: list[str] = []
            page.on(
                "request",
                lambda req: non_loopback.append(req.url)
                if not req.url.startswith(f"http://127.0.0.1:{cfg.bind_port}/")
                else None,
            )
            page.goto(f"{srv.base_url}/#token={cfg.access_token}", wait_until="domcontentloaded")
            # Desktop (1280x800) no horizontal overflow
            assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth") is True

            # Switch to mobile (375x812) and verify navigation toggle & no horizontal overflow
            page.set_viewport_size({"width": 375, "height": 812})
            expect(page.get_by_test_id("navigation-toggle")).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth") is True
            page.get_by_test_id("navigation-toggle").click()
            page.get_by_test_id("nav-projects").click()
            for tid in ("navigation-toggle", "register-project"):
                box = page.get_by_test_id(tid).bounding_box()
                assert box is not None
                assert box["x"] >= 0
                assert box["x"] + box["width"] <= 375
                expect(page.get_by_test_id(tid)).to_be_visible()

            page.get_by_test_id("register-root").fill(str(proj))
            page.get_by_test_id("project-name").fill("s36-proj")
            page.get_by_test_id("confirm-read").check()
            page.get_by_test_id("register-project").click()

            page.get_by_test_id("navigation-toggle").click()
            page.get_by_test_id("nav-extensions").click()
            cb = page.get_by_test_id("skill-select-xss-skill")
            expect(cb).to_be_visible(timeout=5000)
            _tab_to(page, "skill-select-xss-skill")
            assert page.evaluate("document.activeElement.getAttribute('data-testid')") == "skill-select-xss-skill"
            page.keyboard.press("Space")
            assert cb.is_checked()
            # 375px: skill checkbox is visible and its bbox is within the viewport.
            cb_box = cb.bounding_box()
            assert cb_box is not None and cb_box["x"] >= 0 and cb_box["x"] + cb_box["width"] <= 375
            expect(cb).to_be_visible()

            page.get_by_test_id("navigation-toggle").click()
            page.get_by_test_id("nav-chat").click()
            page.get_by_test_id("chat-input").fill("xss and keyboard test")
            page.get_by_test_id("chat-send").click()
            page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
            page.locator('[data-testid^="offer-confirm-btn-"]').last.click()

            decided: set[str] = set()
            rejected: set[str] = set()
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline and len(decided) < 2:
                # 375px: approve/reject controls are visible and within the viewport.
                for prefix in ("extension-approve-", "extension-reject-"):
                    ctrls = page.locator(f'[data-testid^="{prefix}"]')
                    for i in range(ctrls.count()):
                        ctrl = ctrls.nth(i)
                        if ctrl.is_visible():
                            box = ctrl.bounding_box()
                            assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= 375
                btns = page.locator('[data-testid^="extension-approve-"]')
                for i in range(btns.count()):
                    btn = btns.nth(i)
                    tid = btn.get_attribute("data-testid") or ""
                    if tid not in decided and btn.is_visible() and not btn.is_disabled():
                        _tab_to(page, tid)
                        assert page.evaluate("document.activeElement.getAttribute('data-testid')") == tid
                        page.keyboard.press("Enter")
                        decided.add(tid)
                page.wait_for_timeout(80)

            # Keyboard reject leg: Tab (no focus) to a reject control, assert activeElement, and press Enter.
            rej_ctrl = page.locator('[data-testid^="extension-reject-"]').first
            expect(rej_ctrl).to_be_visible(timeout=8000)
            rej_tid = rej_ctrl.get_attribute("data-testid") or ""
            _tab_to(page, rej_tid)
            assert page.evaluate("document.activeElement.getAttribute('data-testid')") == rej_tid
            page.keyboard.press("Enter")
            rejected.add(rej_tid)
            page.wait_for_timeout(150)
            assert len(rejected) == 1

            page.get_by_test_id("navigation-toggle").click()
            page.get_by_test_id("nav-extensions").click()
            page.get_by_test_id("ext-tab-mcp").click()
            expect(page.get_by_test_id("extension-result")).to_contain_text("<img src=x onerror=window.__xss=3>", timeout=10000)
            assert page.evaluate("window.__xss || null") is None
            assert non_loopback == []
            # Verify no horizontal overflow in both mobile (375x812) and desktop (1280x800) with long/untrusted content rendered
            assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth") is True
            page.set_viewport_size({"width": 1280, "height": 800})
            assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth") is True
            # Zero unauthorized workspace writes
            assert tree_hashes(proj) == before_proj_hashes
        finally:
            browser.close()
