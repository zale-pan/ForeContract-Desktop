"""APP-T06 J01–J20: offline joint acceptance and portable local source delivery.

Three nodes:
  * ``test_j01_portable_cli``          (J01–J05) — copy sources, launch the real CLI from the
    copy on a constrained ``PYTHONPATH``, drive a controlled example through real Chromium,
    stop it via stdin, restart it and read history back.
  * ``test_j02_joint_skill_mcp_browser`` (J06–J15) — registered inventory project with a real
    Skill and a real stdio MCP echo, driven through real Chromium for two runs, reload, restart.
  * ``test_j03_delivery_docs``         (J16–J20) — structural checks of the two delivery docs.

The delivered ``ModelProvider`` is injected only through the app's own ``provider_factory``
seam.  J02 additionally installs context-scoped, transparent *forwarding* observers over the
real ``RuntimeEngine._commit_contract`` / ``ProjectCodingAdapter.execute`` /
``_AppTraceWriter.append`` / ``ProjectCommandRunner.start_approved_command`` entry points
(per S01), forwarding every call unchanged, recording only after the original returns, and
restoring exact callable identity (asserted with ``is``) once the ``monkeypatch.context``
exits after the second service closes.

Per S02, the completed run only persists MCP-call *metadata*; the response body
(``text``/``structured_content``/``content_items``) is not persisted and restores empty.
The test asserts persisted-metadata equality across reopen and the explicit absent body.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from app_helpers import (
    LiveTcpTestServer,
    auth_headers,
    is_pid_alive,
    make_clean_subprocess_env,
    pick_free_loopback_port,
    read_line_with_deadline,
    tree_hashes,
)
from delivery_helpers import (
    copy_delivery_sources,
    make_delivery_project,
    make_joint_provider,
)
from extension_helpers import (
    make_extension_host_config,
    read_stdio_log,
    write_extension_config_file,
    write_stdio_mcp_script,
)
from forecontract.runtime import RuntimeEngine
from forecontract_app.project_adapter import ProjectCodingAdapter
from forecontract_app.project_commands import ProjectCommandRunner
from forecontract_app.runtime import _AppTraceWriter
from playwright.sync_api import expect, sync_playwright

_REPO_ROOT = Path(__file__).resolve().parents[2]
_README_PATH = _REPO_ROOT / "apps" / "local" / "README.zh-CN.md"
_LOCAL_DELIVERY_PATH = _REPO_ROOT / "docs" / "LOCAL_DELIVERY.zh-CN.md"

_SKILL_MARKER_RE = "MARKER::"
_FROZEN_FIXTURE_FILES = (
    "tests/test_inventory.py",
    "AGENTS.md",
    "USER_NOTES.md",
    ".forecontract/skills/inventory/SKILL.md",
)


def _scratch_root(tmp_path: Path) -> Path:
    return Path(os.environ.get("FORECONTRACT_TEST_SCRATCH", str(tmp_path)))


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _attach_watchers(
    page: Any, *, base_url: str
) -> tuple[list[str], list[str], list[str], list[dict[str, Any]]]:
    page_errors: list[str] = []
    console_errors: list[str] = []
    non_loopback: list[str] = []
    requests: list[dict[str, Any]] = []

    page.on("pageerror", lambda err: page_errors.append(str(err)))
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
    page.on(
        "request",
        lambda req: (
            non_loopback.append(req.url)
            if not req.url.startswith(f"{base_url}/")
            else None
        ),
    )

    def _on_req(req: Any) -> None:
        requests.append({"method": req.method, "url": req.url})

    page.on("request", _on_req)
    return page_errors, console_errors, non_loopback, requests


def _delivery_pythonpath(delivery_root: Path, deps_root: Path) -> str:
    return os.pathsep.join(
        [
            str(delivery_root / "apps" / "local"),
            str(delivery_root / "packages" / "core" / "src"),
            str(delivery_root / "packages" / "planners" / "llm" / "src"),
            str(delivery_root / "packages" / "adapters" / "coding" / "src"),
            str(deps_root),
        ]
    )


def _r003_reap(proc: subprocess.Popen[str]) -> None:
    """Boundedly reap a test-owned CLI subprocess without masking the original failure."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5.0)
    except Exception:  # noqa: BLE001
        if proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=5.0)
            except Exception:  # noqa: BLE001
                proc.poll()


def _launch_delivery_cli(
    *,
    py_path: str,
    cwd: Path,
    data_root: Path,
    workspace_parent: Path,
    project_root: Path,
    port: int,
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
        "--project-root",
        str(project_root),
        "--port",
        str(port),
        "--offline",
        "--control-stdin",
    ]
    env = make_clean_subprocess_env(extra={"PYTHONPATH": py_path})
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        cwd=str(cwd),
        env=env,
    )
    try:
        assert proc.stdout is not None
        line = read_line_with_deadline(proc.stdout, timeout=30.0)
        prefix = "FORECONTRACT_APP_URL="
        assert line.startswith(prefix), f"unexpected CLI stdout: {line!r}"
        full_url = line[len(prefix) :].strip()
        token = full_url.split("#token=", 1)[1]
        base_url = full_url.split("/#token=", 1)[0]
        return proc, base_url, token
    except BaseException:
        _r003_reap(proc)
        raise


def _stop_cli(proc: subprocess.Popen[str]) -> int:
    assert proc.stdin is not None
    proc.stdin.write("stop\n")
    proc.stdin.flush()
    return proc.wait(timeout=12.0)


# --------------------------------------------------------------------------
# S01 / F02 context-scoped transparent observers (``_r002_`` prefix helpers)
# --------------------------------------------------------------------------

def _r002_observe_commit(orig_commit: Any, commits: list[dict[str, Any]]) -> Any:
    def observe(self: Any, request: Any, progress: Any, contract: Any, previous: Any, replaces: Any) -> Any:
        result = orig_commit(self, request, progress, contract, previous, replaces)
        commits.append(
            {
                "run_id": request.run_id,
                "step_id": progress.current_step,
                "contract_id": contract.contract_id,
                "action_id": contract.action.metadata.get("action_id"),
                "action_type": contract.action.action_type,
                "action": contract.action.model_dump(mode="json"),
            }
        )
        return result

    return observe


def _r002_observe_execute(orig_execute: Any, executions: list[dict[str, Any]]) -> Any:
    async def observe(self: Any, action: Any) -> Any:
        outcome = await orig_execute(self, action)
        executions.append(
            {
                "action_id": action.metadata.get("action_id"),
                "action_type": action.action_type,
                "action": action.model_dump(mode="json"),
                "outcome_id": outcome.outcome_id,
                "outcome_action_id": outcome.changes.get("action_id"),
                "outcome_action_type": outcome.changes.get("action_type"),
            }
        )
        return outcome

    return observe


def _r002_observe_trace(orig_append: Any, traces: list[dict[str, Any]]) -> Any:
    def observe(self: Any, record: Any) -> Any:
        result = orig_append(self, record)  # forward first (preserve return/exception)
        traces.append(
            {
                "kind": record.kind,
                "run_id": record.run_id,
                "sequence": record.sequence,
                "step_id": getattr(record, "step_id", None),
                "contract_id": getattr(record, "contract_id", None),
                "attempt_id": getattr(record, "attempt_id", None),
            }
        )
        return result

    return observe


def _r002_observe_start_command(
    orig_start: Any, cmd_entries: list[object], cmd_starts: list[dict[str, Any]]
) -> Any:
    def observe(self: Any, *args: Any, **kwargs: Any) -> Any:
        cmd_entries.append(1)
        result = orig_start(self, *args, **kwargs)
        cmd_starts.append(
            {
                "proposal_id": kwargs.get("proposal_id"),
                "command_id": result.command_id,
                "run_id": result.run_id,
            }
        )
        return result

    return observe


def _r002_stdio_counts(log_path: Path) -> dict[str, int]:
    started = 0
    initialize = 0
    tools_call = 0
    for ev in read_stdio_log(log_path):
        if ev.get("event") == "started":
            started += 1
        elif ev.get("event") == "rpc" and ev.get("method") == "initialize":
            initialize += 1
        elif ev.get("event") == "rpc" and ev.get("method") == "tools/call":
            tools_call += 1
    return {"started": started, "initialize": initialize, "tools_call": tools_call}


def _r002_fetch_proposals(
    base_url: str, headers: dict[str, str], session_id: str, run_id: str
) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    proj = httpx.get(
        f"{base_url}/api/v1/sessions/{session_id}/runs/{run_id}/proposals",
        headers=headers,
        timeout=5.0,
    )
    for p in proj.json():
        out[str(p["id"])] = p
    ext = httpx.get(
        f"{base_url}/api/v1/sessions/{session_id}/runs/{run_id}/extensions",
        headers=headers,
        timeout=5.0,
    )
    for p in ext.json().get("proposals") or []:
        out[str(p["id"])] = p
    return out


def _approve_joint_pending(
    page: Any,
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    run_id: str,
    project_id: str,
    log_path: Path,
    cmd_entries: list[object],
    cmd_starts: list[dict[str, Any]],
    approved: list[dict[str, Any]],
    obs: dict[str, Any],
) -> None:
    deadline = time.monotonic() + 120.0
    decided: set[str] = set()
    while time.monotonic() < deadline:
        st = page.get_by_test_id("run-state").text_content()
        if st in ("succeeded", "failed", "cancelled", "unknown"):
            return
        props = _r002_fetch_proposals(base_url, headers, session_id, run_id)
        for prefix in ("extension-approve-", "proposal-approve-"):
            btns = page.locator(f'[data-testid^="{prefix}"]')
            for i in range(btns.count()):
                btn = btns.nth(i)
                tid = btn.get_attribute("data-testid") or ""
                if tid in decided or not btn.is_visible() or btn.is_disabled():
                    continue
                pid = tid.removeprefix(prefix)
                prop = props.get(pid)
                if prop is None:
                    props = _r002_fetch_proposals(base_url, headers, session_id, run_id)
                    prop = props.get(pid)
                if prop is None:
                    # Never approve a button without its real proposal; wait for the state to
                    # arrive on the next poll within the same deadline.
                    continue
                assert prop.get("id"), "proposal missing id"
                assert prop.get("digest"), "proposal missing digest"
                assert prop.get("state") == "pending", f"proposal not pending: {prop.get('state')}"
                assert prop.get("run_id") == run_id, "proposal run_id mismatch"
                assert prop.get("session_id") == session_id, "proposal session_id mismatch"
                assert prop.get("project_id") == project_id, "proposal project_id mismatch"
                if prefix == "extension-approve-" and prop.get("kind") == "extension_connect" and obs.get("stdio_before_connect") is None:
                    obs["stdio_before_connect"] = _r002_stdio_counts(log_path)
                if prefix == "extension-approve-" and prop.get("kind") == "extension_call" and obs.get("stdio_before_call") is None:
                    obs["stdio_before_call"] = _r002_stdio_counts(log_path)
                if prefix == "proposal-approve-" and prop.get("kind") == "command" and obs.get("cmd_entries_before_first_command") is None:
                    obs["cmd_entries_before_first_command"] = len(cmd_entries)
                if prop.get("kind") == "command":
                    key = (pid, run_id)
                    first = obs.setdefault("cmd_first_obs", {}).get(key)
                    if first is None:
                        first = {
                            "key": key,
                            "entries": len(cmd_entries),
                            "starts": len(cmd_starts),
                            "project_id": prop.get("project_id"),
                            "session_id": prop.get("session_id"),
                            "run_id": prop.get("run_id"),
                            "id": prop.get("id"),
                            "digest": prop.get("digest"),
                        }
                        obs["cmd_first_obs"][key] = first
                if prefix == "proposal-approve-":
                    trust = page.get_by_test_id(f"proposal-trust-{pid}")
                    if trust.count() > 0 and trust.is_visible() and not trust.is_checked():
                        trust.check()
                if prop.get("kind") == "command":
                    first = obs["cmd_first_obs"][(pid, run_id)]
                    # F02.a: re-read the live proposal after trust, verify still pending + identity.
                    latest = _r002_fetch_proposals(base_url, headers, session_id, run_id).get(pid)
                    assert latest is not None, f"command proposal disappeared before approval: {pid}"
                    assert latest.get("state") == "pending", f"command proposal not pending: {latest.get('state')}"
                    assert latest.get("project_id") == first["project_id"] == project_id
                    assert latest.get("session_id") == first["session_id"] == session_id
                    assert latest.get("run_id") == first["run_id"] == run_id
                    assert latest.get("id") == first["id"] == pid
                    assert latest.get("digest") == first["digest"]
                    second = (len(cmd_entries), len(cmd_starts))
                    assert second == (first["entries"], first["starts"]), f"command count changed: {(pid, run_id)}"
                btn.click()
                decided.add(tid)
                approved.append(
                    {
                        "id": pid,
                        "kind": prop.get("kind"),
                        "tool_name": prop.get("tool_name"),
                        "run_id": prop.get("run_id"),
                        "digest": prop.get("digest"),
                        "state": prop.get("state"),
                    }
                )
                if prop.get("kind") == "command":
                    first = obs["cmd_first_obs"][(pid, run_id)]
                    _r004_wait_cmd_start(
                        cmd_starts,
                        cmd_entries,
                        proposal_id=pid,
                        run_id=run_id,
                        entries_before=first["entries"],
                        starts_before=first["starts"],
                    )
                    obs.setdefault("cmd_checked", []).append(
                        {
                            "key": (pid, run_id),
                            "first": (first["entries"], first["starts"]),
                            "second": tuple(second),
                        }
                    )
                if prefix == "extension-approve-" and prop.get("kind") == "extension_connect":
                    obs["stdio_after_connect"] = _r002_wait_stdio(
                        log_path, lambda c: c["started"] >= 1 and c["initialize"] >= 1
                    )
                if prefix == "extension-approve-" and prop.get("kind") == "extension_call":
                    obs["stdio_after_call"] = _r002_wait_stdio(
                        log_path, lambda c: c["tools_call"] >= 1
                    )
        page.wait_for_timeout(80)
    raise AssertionError("timed out approving joint proposals")


def _r002_wait_stdio(log_path: Path, predicate: Any, timeout: float = 20.0) -> dict[str, int]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        counts = _r002_stdio_counts(log_path)
        if predicate(counts):
            return counts
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for stdio log condition: {_r002_stdio_counts(log_path)}")


def _r002_wait_run_id(page: Any, *, expected_prev: str | None = None, timeout: float = 15.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rid = page.get_by_test_id("run-id").inner_text().strip()
        if rid and rid != "-" and (expected_prev is None or rid != expected_prev):
            return rid
        time.sleep(0.05)
    raise AssertionError("timed out waiting for run-id")


def _r003_stable_call(c: dict[str, Any]) -> dict[str, Any]:
    """Return the persisted metadata fields of one MCP call (body fields excluded)."""
    persisted = (
        "call_id",
        "action_id",
        "server_id",
        "tool_name",
        "args_sha256",
        "schema_sha256",
        "status",
        "is_error",
        "effect",
        "truncated",
        "original_bytes",
        "sha256",
        "error_code",
        "error_message",
    )
    out = {k: c.get(k) for k in persisted}
    em = out.get("error_message")
    if isinstance(em, str) and len(em) > 256:
        out["error_message"] = em[:256]
    return out


def _r004_normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _r004_wait_cmd_start(
    cmd_starts: list[dict[str, Any]],
    cmd_entries: list[object],
    *,
    proposal_id: str,
    run_id: str,
    entries_before: int,
    starts_before: int,
    timeout: float = 30.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(
            s.get("proposal_id") == proposal_id and s.get("run_id") == run_id for s in cmd_starts
        ):
            assert len(cmd_entries) == entries_before + 1
            assert len(cmd_starts) == starts_before + 1
            return
        time.sleep(0.05)
    raise AssertionError(f"command start not observed for proposal {proposal_id} run {run_id}")


def test_j01_portable_cli(tmp_path: Path) -> None:
    scratch = _scratch_root(tmp_path)
    scratch.mkdir(parents=True, exist_ok=True)
    delivery_root = scratch / "delivery-source"
    data_root = scratch / "cli-data"
    workspace_parent = scratch / "cli-workspaces"
    project_root = scratch / "cli-projects"
    external_workdir = scratch / "外部 工作 目录"
    # F01: pre-create distinct, non-overlapping data / workspace / project roots.
    for root in (data_root, workspace_parent, project_root, external_workdir):
        root.mkdir(parents=True, exist_ok=True)

    # J01: copy only the portable source deliverable and verify per-file SHA equality.
    hashes = copy_delivery_sources(_REPO_ROOT, delivery_root)
    assert hashes
    for rel, sha in hashes.items():
        assert _sha256_file(delivery_root / rel) == sha
        assert _sha256_file(_REPO_ROOT / rel) == sha
    for extra in delivery_root.rglob("*.pyc"):
        raise AssertionError(f"unexpected cache artifact in copy: {extra}")
    for extra in delivery_root.rglob("__pycache__"):
        raise AssertionError(f"unexpected __pycache__ in copy: {extra}")

    # J02: constrained PYTHONPATH (only copy roots + explicit third-party deps root).
    deps_root = Path(uvicorn.__file__).resolve().parent.parent
    py_path = _delivery_pythonpath(delivery_root, deps_root)
    assert str(_REPO_ROOT / "tests") not in py_path
    assert str(_REPO_ROOT / "apps" / "local") not in py_path

    probe_code = (
        "import json\n"
        "import forecontract_app, forecontract, forecontract_llm, forecontract_coding\n"
        "print(json.dumps({'app': forecontract_app.__file__, 'core': forecontract.__file__, "
        "'llm': forecontract_llm.__file__, 'coding': forecontract_coding.__file__}, ensure_ascii=False))\n"
    )
    probe = subprocess.run(
        [sys.executable, "-B", "-c", probe_code],
        cwd=str(external_workdir),
        env=make_clean_subprocess_env(extra={"PYTHONPATH": py_path}),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    imported = json.loads(probe.stdout.strip().splitlines()[-1])
    expected_roots = {
        "app": delivery_root / "apps" / "local" / "forecontract_app",
        "core": delivery_root / "packages" / "core" / "src" / "forecontract",
        "llm": delivery_root / "packages" / "planners" / "llm" / "src" / "forecontract_llm",
        "coding": delivery_root / "packages" / "adapters" / "coding" / "src" / "forecontract_coding",
    }
    for key, root in expected_roots.items():
        resolved = Path(imported[key]).resolve()
        assert resolved.is_relative_to(root.resolve()), f"{key} resolved outside copy: {resolved}"

    # Launch the real CLI from the copy.
    port = pick_free_loopback_port()
    proc, base_url, token = _launch_delivery_cli(
        py_path=py_path,
        cwd=external_workdir,
        data_root=data_root,
        workspace_parent=workspace_parent,
        project_root=project_root,
        port=port,
    )

    try:
        # J03: health + real Chromium page load, no JS errors, no non-loopback requests.
        health = httpx.get(
            f"{base_url}/api/v1/health",
            headers={"Host": f"127.0.0.1:{port}", "Authorization": f"Bearer {token}"},
            timeout=5.0,
        )
        assert health.status_code == 200
        assert health.json()["status"] == "ok" and health.json()["live_enabled"] is False
        assert health.json()["mode"] in ("offline", "unconfigured")

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page_errors, console_errors, non_loopback, _ = _attach_watchers(
                    page, base_url=base_url
                )
                page.goto(f"{base_url}/#token={token}", wait_until="domcontentloaded")
                assert "#token=" not in page.url

                # J04: controlled example through the real CLI host.
                cli_headers = {"Authorization": f"Bearer {token}", "Origin": base_url}
                proj_j04 = httpx.post(
                    f"{base_url}/api/v1/projects",
                    headers=cli_headers,
                    json={"name": "delivery-demo"},
                    timeout=5.0,
                ).json()
                sess_j04 = httpx.post(
                    f"{base_url}/api/v1/sessions",
                    headers=cli_headers,
                    json={
                        "project_id": proj_j04["project_id"],
                        "model": {"provider": "offline", "model": "offline-metrics-v1"},
                    },
                    timeout=5.0,
                ).json()
                httpx.post(
                    f"{base_url}/api/v1/sessions/{sess_j04['session_id']}/runs",
                    headers=cli_headers,
                    json={
                        "client_request_id": "22222222-2222-4222-8222-222222222222",
                        "goal": "实现加权正数求和的 metrics.py 示例修复并运行验收",
                        "confirmed_scope": True,
                    },
                    timeout=5.0,
                )
                page.reload(wait_until="domcontentloaded")
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-state")).to_have_text("succeeded", timeout=20000)
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("verification")).to_have_text("passed")
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("changed-paths")).to_contain_text("metrics.py")

                assert page_errors == []
                assert console_errors == []
                assert non_loopback == []
            finally:
                browser.close()

        metrics_path = workspace_parent / "delivery-demo" / "metrics.py"
        assert metrics_path.is_file()
        # F01.a: the real repaired function body must be present in the delivered file.
        assert (
            "return sum(w * v for w, v in pairs if w >= min_weight and v > 0)"
            in metrics_path.read_text(encoding="utf-8")
        )

        # F01: capture original project/session/run identity, run result, and history stable fields.
        hdr = {"Host": f"127.0.0.1:{port}", "Authorization": f"Bearer {token}"}
        projects = httpx.get(f"{base_url}/api/v1/projects", headers=hdr, timeout=5.0).json()
        sessions = httpx.get(f"{base_url}/api/v1/sessions", headers=hdr, timeout=5.0).json()
        assert len(projects) == 1 and projects[0]["name"] == "delivery-demo"
        assert len(sessions) == 1
        project_id = projects[0]["project_id"]
        session_id = sessions[0]["session_id"]
        history_before = httpx.get(
            f"{base_url}/api/v1/sessions/{session_id}/history", headers=hdr, timeout=5.0
        ).json()
        run_result_items = [it for it in history_before.get("items", []) if it.get("kind") == "run_result"]
        assert run_result_items
        run_id = run_result_items[0]["run_id"]
        run1_json = httpx.get(
            f"{base_url}/api/v1/sessions/{session_id}/runs/{run_id}", headers=hdr, timeout=5.0
        ).json()
        assert run1_json["state"] == "succeeded" and run1_json["verification"] == "passed"
        run_result_stable = run1_json.get("result") or {}
        history_stable = [
            {
                "item_id": it.get("item_id"),
                "seq": it.get("seq"),
                "project_id": it.get("project_id"),
                "session_id": it.get("session_id"),
                "run_id": it.get("run_id"),
                "kind": it.get("kind"),
                "text": it.get("text"),
                "source_ids": it.get("source_ids"),
                "truncated": it.get("truncated"),
                "sha256": it.get("sha256"),
            }
            for it in history_before.get("items", [])
        ]
        metrics_sha_after = _sha256_file(metrics_path)

        # J05: bounded stop via stdin (no process-name kill), then restart on same data_root.
        exit_code = _stop_cli(proc)
        assert exit_code == 0
        assert not is_pid_alive(proc.pid)
        deadline = time.monotonic() + 5.0
        while _port_open(port) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not _port_open(port)

        port2 = pick_free_loopback_port()
        proc2, base_url2, token2 = _launch_delivery_cli(
            py_path=py_path,
            cwd=external_workdir,
            data_root=data_root,
            workspace_parent=workspace_parent,
            project_root=project_root,
            port=port2,
        )
        try:
            hdr2 = {"Host": f"127.0.0.1:{port2}", "Authorization": f"Bearer {token2}"}
            projects2 = httpx.get(f"{base_url2}/api/v1/projects", headers=hdr2, timeout=5.0).json()
            sessions2 = httpx.get(f"{base_url2}/api/v1/sessions", headers=hdr2, timeout=5.0).json()
            assert [p["project_id"] for p in projects2] == [project_id]
            assert [s["session_id"] for s in sessions2] == [session_id]
            run1_reopen = httpx.get(
                f"{base_url2}/api/v1/sessions/{session_id}/runs/{run_id}", headers=hdr2, timeout=5.0
            ).json()
            assert run1_reopen["state"] == run1_json["state"]
            assert run1_reopen["verification"] == run1_json["verification"]
            assert (run1_reopen.get("result") or {}) == run_result_stable
            history_after = httpx.get(
                f"{base_url2}/api/v1/sessions/{session_id}/history", headers=hdr2, timeout=5.0
            ).json()
            history_after_stable = [
                {
                    "item_id": it.get("item_id"),
                    "seq": it.get("seq"),
                    "project_id": it.get("project_id"),
                    "session_id": it.get("session_id"),
                    "run_id": it.get("run_id"),
                    "kind": it.get("kind"),
                    "text": it.get("text"),
                    "source_ids": it.get("source_ids"),
                    "truncated": it.get("truncated"),
                    "sha256": it.get("sha256"),
                }
                for it in history_after.get("items", [])
            ]
            assert history_after_stable == history_stable
            assert _sha256_file(metrics_path) == metrics_sha_after
        finally:
            try:
                exit_code2 = _stop_cli(proc2)
                assert exit_code2 == 0
                assert not is_pid_alive(proc2.pid)
                deadline2 = time.monotonic() + 5.0
                while _port_open(port2) and time.monotonic() < deadline2:
                    time.sleep(0.05)
                assert not _port_open(port2)
            finally:
                # F01.b: the second process is reapd even if the normal stop/assert raised.
                _r003_reap(proc2)
    finally:
        _r003_reap(proc)


def test_j02_joint_skill_mcp_browser(tmp_path: Path, monkeypatch: Any) -> None:
    marker = f"DELIVERY_{uuid.uuid4().hex[:16]}"
    proj = make_delivery_project(tmp_path / "inventory-proj", marker=marker)
    log_path = tmp_path / "logs" / "joint.jsonl"
    script = write_stdio_mcp_script(
        tmp_path / "scripts",
        name="joint_mcp.py",
        log_path=log_path,
        label="inventory-srv",
    )
    ext_cfg = write_extension_config_file(
        tmp_path / "ext.json",
        servers=[
            {
                "id": "srv-inventory",
                "transport": "stdio",
                "executable": sys.executable,
                "argv": ["-B", str(script)],
                "cwd": str(tmp_path),
            }
        ],
    )
    cfg = make_extension_host_config(
        tmp_path / "host", allowed_roots=(proj,), extension_config_path=ext_cfg
    )

    frozen_before = {rel: _sha256_file(proj / rel) for rel in _FROZEN_FIXTURE_FILES}
    observed: list[dict[str, Any]] = []

    # S01 / F02 observation state (context-scoped transparent forwarders).
    commits: list[dict[str, Any]] = []
    executions: list[dict[str, Any]] = []
    traces: list[dict[str, Any]] = []
    cmd_entries: list[object] = []
    cmd_starts: list[dict[str, Any]] = []
    approved: list[dict[str, Any]] = []
    obs: dict[str, Any] = {}

    orig_commit = RuntimeEngine._commit_contract
    orig_execute = ProjectCodingAdapter.execute
    orig_append = _AppTraceWriter.append
    orig_start = ProjectCommandRunner.start_approved_command

    with monkeypatch.context() as mp:
        mp.setattr(RuntimeEngine, "_commit_contract", _r002_observe_commit(orig_commit, commits))
        mp.setattr(ProjectCodingAdapter, "execute", _r002_observe_execute(orig_execute, executions))
        mp.setattr(_AppTraceWriter, "append", _r002_observe_trace(orig_append, traces))
        mp.setattr(
            ProjectCommandRunner, "start_approved_command",
            _r002_observe_start_command(orig_start, cmd_entries, cmd_starts),
        )

        def factory(*, project: Any, run_id: str) -> Any:
            _ = project, run_id
            return make_joint_provider(observed=observed)

        with LiveTcpTestServer(config=cfg, provider_factory=factory) as srv, sync_playwright() as pw:
            if getattr(srv.app.state.service, "_chat_provider_factory", None) is None:
                srv.app.state.service._chat_provider_factory = lambda **_: (lambda req: {"action": "execution_offer", "goal": req.question, "summary": "执行任务"})
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page_errors, console_errors, non_loopback, requests = _attach_watchers(
                    page, base_url=srv.base_url
                )
                page.goto(f"{srv.base_url}/#token={cfg.access_token}", wait_until="domcontentloaded")

                # J06: register + select Skill through the real page; registration is read-only.
                before_tree = tree_hashes(proj)
                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id("register-root").fill(str(proj))
                page.get_by_test_id("project-name").fill("inventory-joint")
                page.get_by_test_id("confirm-read").check()
                page.get_by_test_id("register-project").click()
                expect(page.get_by_test_id("project-list")).to_contain_text("inventory-joint")
                page.get_by_test_id("nav-extensions").click()
                expect(page.get_by_test_id("skill-select-inventory")).to_be_visible(timeout=5000)
                assert tree_hashes(proj) == before_tree
                page.get_by_test_id("skill-select-inventory").check()
                page.get_by_test_id("nav-chat").click()
                assert page.get_by_test_id("model").count() == 0

                # J07-J10: first run (fix).
                page.get_by_test_id("chat-input").fill("修复 inventory 项目的 normalize 与 render 占位并通过验收")
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                page.locator('[data-testid^="offer-confirm-btn-"]').last.click()
                run1_id = _r002_wait_run_id(page, timeout=15.0)
                session_id = page.get_by_test_id("current-session").inner_text().strip()
                project_id = page.get_by_test_id("current-project").inner_text().strip()
                headers = auth_headers(cfg)
                _approve_joint_pending(
                    page,
                    base_url=srv.base_url,
                    headers=headers,
                    session_id=session_id,
                    run_id=run1_id,
                    project_id=project_id,
                    log_path=log_path,
                    cmd_entries=cmd_entries,
                    cmd_starts=cmd_starts,
                    approved=approved,
                    obs=obs,
                )
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-state")).to_have_text("succeeded", timeout=15000)
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("verification")).to_have_text("passed")

                # F02 4.1: stdio counts before/after connect and call.
                assert obs.get("stdio_before_connect") == {"started": 0, "initialize": 0, "tools_call": 0}
                assert obs.get("stdio_after_connect") is not None
                assert obs["stdio_after_connect"]["started"] >= 1
                assert obs["stdio_after_connect"]["initialize"] >= 1
                assert obs.get("stdio_before_call") is not None
                assert obs["stdio_before_call"]["tools_call"] == 0
                assert obs.get("stdio_after_call") is not None
                assert obs["stdio_after_call"]["tools_call"] >= 1

                # F02 4.1: run1 command starts bound to run1 approved command proposals.
                command_proposal_ids = [
                    a["id"] for a in approved if a.get("kind") == "command" and a.get("run_id") == run1_id
                ]
                assert command_proposal_ids, "no run1 command proposal approved"
                assert obs.get("cmd_entries_before_first_command") == 0
                assert cmd_starts, "no command start observed"
                run1_cmd_starts = [s for s in cmd_starts if s.get("run_id") == run1_id]
                assert {s["proposal_id"] for s in run1_cmd_starts} == set(command_proposal_ids)
                assert all(s["command_id"] for s in run1_cmd_starts)
                assert len(cmd_entries) == len(cmd_starts)

                # J07: skill id/sha/content and MCP echo marker were actually observed.
                skill_obs = [o for o in observed if o.get("phase") == "selected_skill"]
                mcp_obs = [o for o in observed if o.get("phase") == "mcp_call"]
                assert skill_obs and skill_obs[0]["skill_id"] == "inventory"
                assert skill_obs[0]["sha256"] == _sha256_file(proj / ".forecontract" / "skills" / "inventory" / "SKILL.md")
                assert marker in skill_obs[0]["content"]
                assert mcp_obs and marker in mcp_obs[0]["text"]

                # J08/J10: the marker flowed from the MCP result into both repaired src files.
                normalize_text = (proj / "src" / "normalize.py").read_text(encoding="utf-8")
                render_text = (proj / "src" / "render.py").read_text(encoding="utf-8")
                assert marker in normalize_text and marker in render_text
                assert "raise NotImplementedError" not in normalize_text
                assert 'f"{name}: ERROR"' not in render_text

                # J10: changed-paths and frozen fixture integrity.
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("changed-paths")).to_contain_text("src/normalize.py")
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("changed-paths")).to_contain_text("src/render.py")
                for rel, sha in frozen_before.items():
                    assert _sha256_file(proj / rel) == sha, f"frozen fixture changed: {rel}"

                # J10: run result carries exactly two changed paths and two observed tests.
                run1_json = httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{session_id}/runs/{run1_id}",
                    headers=headers,
                    timeout=5.0,
                ).json()
                result = run1_json.get("result") or {}
                changed_paths = result.get("changed_paths") or []
                assert {c.get("path") for c in changed_paths} == {"src/normalize.py", "src/render.py"}
                checks = result.get("checks") or {}
                observed_tests = (checks.get("inventory_unit") or {}).get("observed_tests") or []
                assert {(t.get("classname"), t.get("name")) for t in observed_tests} == {
                    ("tests.test_inventory", "test_normalize"),
                    ("tests.test_inventory", "test_render"),
                }

                # F02 4.2: disk SHA equals the actual diff/revision for the two repaired files.
                normalize_sha_after_run1 = _sha256_file(proj / "src" / "normalize.py")
                render_sha_after_run1 = _sha256_file(proj / "src" / "render.py")
                assert result["final_revision"] == result["diff"]["final_revision"]
                disk_sha = {
                    "src/normalize.py": normalize_sha_after_run1,
                    "src/render.py": render_sha_after_run1,
                }
                seen_diff_paths: set[str] = set()
                for change in result["diff"]["entries"]:
                    p = change.get("path")
                    if p in disk_sha:
                        assert disk_sha[p] == change.get("after_sha256")
                        seen_diff_paths.add(p)
                assert seen_diff_paths == {"src/normalize.py", "src/render.py"}
                run1_result_full = run1_json.get("result")

                # F02.b: save run1 real diff entries (both paths, non-empty excerpt) and
                # rendered check display for the later historical readback.
                run1_diff_excerpts = {
                    change["path"]: change.get("diff_excerpt") or ""
                    for change in result["diff"]["entries"]
                }
                assert set(run1_diff_excerpts) == {"src/normalize.py", "src/render.py"}
                assert all(run1_diff_excerpts.values()), "diff_excerpt empty"
                # Pre-refresh: the real page diff-detail matches the saved entry excerpts.
                for path in ("src/normalize.py", "src/render.py"):
                    page.get_by_test_id("diff-file-select").select_option(value=path)
                    pre = page.get_by_test_id("diff-detail").locator("pre").text_content()
                    assert _r004_normalize_newlines(pre or "") == _r004_normalize_newlines(
                        run1_diff_excerpts[path]
                    )
                run1_coverage_summary = page.get_by_test_id("coverage-summary").text_content()
                run1_verification = page.get_by_test_id("verification").text_content()
                assert "inventory_unit" in run1_coverage_summary
                assert run1_verification == "passed"

                run1_history = httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{session_id}/history", headers=headers, timeout=5.0
                ).json()
                run1_result_history = [
                    it for it in run1_history.get("items", []) if it.get("kind") == "run_result" and it.get("run_id") == run1_id
                ]
                assert run1_result_history

                # S02: pre-close body (transient) and persisted metadata.
                run1_ext = httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{session_id}/runs/{run1_id}/extensions",
                    headers=headers,
                    timeout=5.0,
                ).json()
                run1_calls_full = [dict(c) for c in run1_ext.get("calls") or []]
                assert run1_calls_full, "run1 extensions calls empty"
                assert any(marker in (c.get("text") or "") for c in run1_calls_full)
                run1_persisted_calls = [
                    _r003_stable_call(dict(c))
                    for c in run1_json.get("result", {}).get("extensions", {}).get("calls") or []
                ]
                assert run1_persisted_calls
                # S02: live -> persisted metadata chain (14 fields, error_message truncated <=256).
                live_by_id = {c["call_id"]: _r003_stable_call(c) for c in run1_calls_full}
                persisted_by_id = {c["call_id"]: c for c in run1_persisted_calls}
                assert len(run1_calls_full) == len(run1_persisted_calls)
                assert len(live_by_id) == len(run1_calls_full)
                assert len(persisted_by_id) == len(run1_persisted_calls)
                assert set(live_by_id) == set(persisted_by_id)
                for call_id in sorted(live_by_id):
                    assert live_by_id[call_id] == persisted_by_id[call_id]

                # J11: explicit second run, read-only check.
                page.get_by_test_id("chat-input").fill("核对刚才结果，不修改文件")
                page.get_by_test_id("chat-send").click()
                page.locator('[data-testid^="offer-confirm-scope-"]').last.check()
                page.locator('[data-testid^="offer-confirm-btn-"]').last.click()
                run2_id = _r002_wait_run_id(page, expected_prev=run1_id, timeout=15.0)
                _approve_joint_pending(
                    page,
                    base_url=srv.base_url,
                    headers=headers,
                    session_id=session_id,
                    run_id=run2_id,
                    project_id=project_id,
                    log_path=log_path,
                    cmd_entries=cmd_entries,
                    cmd_starts=cmd_starts,
                    approved=approved,
                    obs=obs,
                )
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-state")).to_have_text("succeeded", timeout=15000)
                assert run2_id != run1_id
                assert page.get_by_test_id("current-session").inner_text().strip() == session_id
                assert page.get_by_test_id("current-project").inner_text().strip() == project_id
                assert _sha256_file(proj / "src" / "normalize.py") == normalize_sha_after_run1
                assert _sha256_file(proj / "src" / "render.py") == render_sha_after_run1
                run2_json = httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{session_id}/runs/{run2_id}",
                    headers=headers,
                    timeout=5.0,
                ).json()
                assert (run2_json.get("result") or {}).get("changed_paths") == []
                run2_result_full = run2_json.get("result")

                # F02 4.3: readonly_context carries the prior run result/source with stable fields.
                readonly_obs = [o for o in observed if o.get("phase") == "readonly_context"]
                assert readonly_obs
                recent = readonly_obs[-1].get("recent_items") or []
                assert any(
                    it.get("run_id") == run1_id and it.get("kind") == "run_result" and it.get("sha256") and it.get("source_ids")
                    for it in recent
                )
                prior_result = run1_result_history[0]
                matched = [
                    it for it in recent
                    if it.get("run_id") == run1_id and it.get("kind") == "run_result"
                    and it.get("sha256") == prior_result.get("sha256")
                    and it.get("source_ids") == prior_result.get("source_ids")
                ]
                assert matched, "prior run result sha256/source_ids not preserved in second run request"

                # F02 4.3: run2 verification + command proposals observed, all bound to run2, ids fresh.
                run1_approved_ids = {a["id"] for a in approved if a.get("run_id") == run1_id}
                run2_approved = [a for a in approved if a.get("run_id") == run2_id]
                assert any(a.get("kind") == "verification" for a in run2_approved)
                assert any(a.get("kind") == "command" for a in run2_approved)
                assert all(a.get("run_id") == run2_id for a in run2_approved)
                assert not (run1_approved_ids & {a["id"] for a in run2_approved})
                run2_cmd_starts = [s for s in cmd_starts if s.get("run_id") == run2_id]
                run2_command_ids = {a["id"] for a in run2_approved if a.get("kind") == "command"}
                assert run2_cmd_starts
                assert {s["proposal_id"] for s in run2_cmd_starts} == run2_command_ids
                assert run2_json.get("verification") == "passed"

                # F02.a: checked command instances (duplicates preserved) correspond exactly to
                # the full command approval list, covering both runs, non-empty.
                checked_keys = [c["key"] for c in obs.get("cmd_checked", [])]
                approved_keys = [
                    (a["id"], a["run_id"]) for a in approved if a.get("kind") == "command"
                ]
                assert checked_keys and len(checked_keys) == len(approved_keys)
                assert sorted(checked_keys) == sorted(approved_keys)
                assert {rid for _, rid in checked_keys} == {run1_id, run2_id}

                # S01.a: every captured execution (both runs) has a matching commit; no set dedup.
                checked_execs: list[tuple[dict[str, Any], dict[str, Any]]] = []
                for x in executions:
                    matches = [
                        c
                        for c in commits
                        if c.get("action_id") == x.get("action_id")
                        and c.get("action_type") == x.get("action_type")
                    ]
                    assert matches, f"captured execution has no matching commit: {x}"
                    commit = matches[-1]
                    assert commit.get("run_id") in (run1_id, run2_id), f"commit run out of scope: {commit.get('run_id')}"
                    x = dict(x)
                    x["run_id"] = commit["run_id"]
                    x["contract_id"] = commit["contract_id"]
                    x["step_id"] = commit["step_id"]
                    checked_execs.append((x, commit))
                assert checked_execs, "no executed actions checked"
                assert len(checked_execs) == len(executions), "checked executions != captured executions"
                assert [x["action_id"] for x, _ in checked_execs] == [
                    e["action_id"] for e in executions
                ]
                assert {x["run_id"] for x, _ in checked_execs} == {run1_id, run2_id}
                assert {"call_mcp", "edit_file"} <= {x["action_type"] for x, _ in checked_execs}
                for x, commit in checked_execs:
                    assert x["action_id"] == commit["action_id"]
                    assert x["action"] == commit["action"]
                    assert x["outcome_id"]
                    assert x["outcome_action_id"] == x["action_id"]
                    assert x["outcome_action_type"] == x["action_type"]
                    committed_evs = [
                        t for t in traces
                        if t["kind"] == "contract_committed"
                        and t["contract_id"] == commit["contract_id"]
                        and t["step_id"] == commit["step_id"]
                        and t["run_id"] == x["run_id"]
                    ]
                    started_evs = [
                        t for t in traces
                        if t["kind"] == "attempt_started"
                        and t["contract_id"] == commit["contract_id"]
                        and t["step_id"] == commit["step_id"]
                        and t["run_id"] == x["run_id"]
                    ]
                    finished_evs = [
                        t for t in traces
                        if t["kind"] == "attempt_finished"
                        and t["contract_id"] == commit["contract_id"]
                        and t["step_id"] == commit["step_id"]
                        and t["run_id"] == x["run_id"]
                    ]
                    assert committed_evs and started_evs and finished_evs
                    committed_event = committed_evs[-1]
                    started_event = started_evs[-1]
                    finished_event = finished_evs[-1]
                    assert committed_event["kind"] == "contract_committed"
                    assert committed_event["run_id"] == started_event["run_id"] == finished_event["run_id"] == x["run_id"]
                    assert committed_event["contract_id"] == started_event["contract_id"] == finished_event["contract_id"] == x["contract_id"]
                    assert committed_event["step_id"] == started_event["step_id"] == finished_event["step_id"] == x["step_id"]
                    assert started_event["attempt_id"] == finished_event["attempt_id"]
                    assert committed_event["sequence"] < started_event["sequence"] < finished_event["sequence"]

                # F02.b: reload is read-only; run1 history diff/check data read back consistently.
                stdio_counts_before_reload = _r002_stdio_counts(log_path)
                cmd_entries_before_reload = len(cmd_entries)
                cmd_starts_before_reload = len(cmd_starts)
                mark = len(requests)
                page.reload(wait_until="domcontentloaded")
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-state")).to_have_text("succeeded", timeout=15000)
                page.get_by_test_id("nav-history").click()
                page.get_by_test_id("run-history-select").select_option(value=run1_id)
                page.get_by_test_id("nav-chat").click()
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("run-id")).to_have_text(run1_id, timeout=15000)
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("changed-paths")).to_contain_text(
                    normalize_sha_after_run1, timeout=15000
                )
                if page.get_by_test_id("chat-inspector").is_hidden(): page.get_by_test_id("toggle-inspector").click()
                expect(page.get_by_test_id("changed-paths")).to_contain_text(render_sha_after_run1)
                # F02.b: real historical diff content and check display readback.
                for path in ("src/normalize.py", "src/render.py"):
                    page.get_by_test_id("diff-file-select").select_option(value=path)
                    pre = page.get_by_test_id("diff-detail").locator("pre").text_content()
                    assert _r004_normalize_newlines(pre or "") == _r004_normalize_newlines(
                        run1_diff_excerpts[path]
                    )
                assert page.get_by_test_id("coverage-summary").text_content() == run1_coverage_summary
                assert page.get_by_test_id("verification").text_content() == run1_verification
                mutation = [r for r in requests[mark:] if r["method"] in {"POST", "PUT", "DELETE"}]
                assert not mutation
                assert _r002_stdio_counts(log_path) == stdio_counts_before_reload
                assert len(cmd_entries) == cmd_entries_before_reload
                assert len(cmd_starts) == cmd_starts_before_reload

                assert page_errors == []
                assert console_errors == []
                assert non_loopback == []
            finally:
                browser.close()

        # F02.d: capture close baselines AFTER the first service closed (observation still active).
        stdio_after_close = _r002_stdio_counts(log_path)
        cmd_entries_after_close = len(cmd_entries)
        cmd_starts_after_close = len(cmd_starts)

        # F02.c / J13: reopen on the same data_root with a new token.
        port2 = pick_free_loopback_port()
        reopened_cfg = make_extension_host_config(
            tmp_path / "host",
            allowed_roots=(proj,),
            extension_config_path=ext_cfg,
            port=port2,
            token=f"reopen-token-{uuid.uuid4().hex}",
        )
        with LiveTcpTestServer(config=reopened_cfg) as srv2:
            if getattr(srv2.app.state.service, "_chat_provider_factory", None) is None:
                srv2.app.state.service._chat_provider_factory = lambda **_: (lambda req: {"action": "execution_offer", "goal": req.question, "summary": "执行任务"})
            hdr2 = auth_headers(reopened_cfg)
            projects = httpx.get(f"{srv2.base_url}/api/v1/projects", headers=hdr2, timeout=5.0).json()
            sessions = httpx.get(f"{srv2.base_url}/api/v1/sessions", headers=hdr2, timeout=5.0).json()
            assert len(projects) == 1 and projects[0]["name"] == "inventory-joint"
            assert projects[0]["project_id"] == project_id
            assert len(sessions) == 1
            assert sessions[0]["session_id"] == session_id
            history = httpx.get(
                f"{srv2.base_url}/api/v1/sessions/{session_id}/history", headers=hdr2, timeout=5.0
            ).json()
            run_result_run_ids = {it["run_id"] for it in history.get("items", []) if it.get("kind") == "run_result"}
            assert run_result_run_ids == {run1_id, run2_id}
            run1_reopen = httpx.get(
                f"{srv2.base_url}/api/v1/sessions/{session_id}/runs/{run1_id}", headers=hdr2, timeout=5.0
            ).json()
            assert run1_reopen["state"] == "succeeded" and run1_reopen["verification"] == "passed"
            run2_reopen = httpx.get(
                f"{srv2.base_url}/api/v1/sessions/{session_id}/runs/{run2_id}", headers=hdr2, timeout=5.0
            ).json()
            assert run2_reopen["state"] == "succeeded" and run2_reopen["verification"] == "passed"
            # F02.c: both stable run results equal the pre-close saved values (full dict).
            assert run1_reopen.get("result") == run1_result_full
            assert run2_reopen.get("result") == run2_result_full
            run1_ext_resp = httpx.get(
                f"{srv2.base_url}/api/v1/sessions/{session_id}/runs/{run1_id}/extensions",
                headers=hdr2,
                timeout=5.0,
            )
            assert run1_ext_resp.status_code == 200
            run1_ext_reopen = run1_ext_resp.json()
            assert run1_ext_reopen.get("project_id") == project_id
            assert run1_ext_reopen.get("session_id") == session_id
            assert run1_ext_reopen.get("run_id") == run1_id
            assert run1_ext_reopen.get("total_calls") == len(run1_persisted_calls)
            reopened_calls = [dict(c) for c in run1_ext_reopen.get("calls") or []]
            reopened_by_id = {c["call_id"]: _r003_stable_call(c) for c in reopened_calls}
            # S02: full live -> persisted -> reopened metadata chain (14 fields each).
            assert reopened_calls
            assert len(reopened_calls) == len(run1_calls_full) == len(run1_persisted_calls)
            assert len(reopened_by_id) == len(reopened_calls)
            assert set(reopened_by_id) == set(live_by_id) == set(persisted_by_id)
            for call_id in sorted(live_by_id):
                assert live_by_id[call_id] == persisted_by_id[call_id] == reopened_by_id[call_id]
            # S02: the MCP response body is not persisted; it restores empty (no body).
            for c in reopened_calls:
                assert c.get("text") == ""
                assert c.get("structured_content") is None
                assert c.get("content_items") == []
            # The marker remains readable as real source-repair evidence, not as MCP body.
            assert marker in (proj / "src" / "normalize.py").read_text(encoding="utf-8")
            assert marker in (proj / "src" / "render.py").read_text(encoding="utf-8")
            assert _sha256_file(proj / "src" / "normalize.py") == normalize_sha_after_run1
            assert _sha256_file(proj / "src" / "render.py") == render_sha_after_run1
            for rel, sha in frozen_before.items():
                assert _sha256_file(proj / rel) == sha, f"frozen fixture changed after reopen: {rel}"

    # S01.b: after the monkeypatch context exits, every entry is restored to its exact original.
    assert RuntimeEngine._commit_contract is orig_commit
    assert ProjectCodingAdapter.execute is orig_execute
    assert _AppTraceWriter.append is orig_append
    assert ProjectCommandRunner.start_approved_command is orig_start

    # F02.d: after the second service closes, counters equal the pre-reopen baselines.
    assert _r002_stdio_counts(log_path) == stdio_after_close
    assert len(cmd_entries) == cmd_entries_after_close
    assert len(cmd_starts) == cmd_starts_after_close


def test_j03_delivery_docs(tmp_path: Path) -> None:
    _ = tmp_path
    readme = _README_PATH.read_text(encoding="utf-8")
    local = _LOCAL_DELIVERY_PATH.read_text(encoding="utf-8")

    # J16: copyable PowerShell startup assembling the four source roots, with quoted paths,
    # non-overlapping data/project dirs, and no D:/PythonProject / audit-store prerequisite.
    assert "$env:PYTHONPATH" in readme
    assert r"apps\local" in readme
    assert r"packages\core\src" in readme
    assert r"packages\planners\llm\src" in readme
    assert r"packages\adapters\coding\src" in readme
    assert "--data-root" in readme and "--project-root" in readme and "--offline" in readme
    assert "Python >= 3.12" in readme or "Python>=3.12" in readme or "3.12" in readme
    assert "D:/PythonProject" not in readme
    assert "audit-store" not in readme

    # F03: README link resolves to the actual file relative to the README directory.
    link_needle = "docs/LOCAL_DELIVERY.zh-CN.md](../../docs/LOCAL_DELIVERY.zh-CN.md)"
    assert link_needle in readme
    resolved = (_README_PATH.parent / "../../docs/LOCAL_DELIVERY.zh-CN.md").resolve()
    assert resolved == _LOCAL_DELIVERY_PATH.resolve()
    assert resolved.is_file()

    # F03: LOCAL_DELIVERY internal file links resolve relative to the docs directory.
    local_link_needle = "[apps/local/README.zh-CN.md](../apps/local/README.zh-CN.md)"
    assert local_link_needle in local
    local_resolved = (_LOCAL_DELIVERY_PATH.parent / "../apps/local/README.zh-CN.md").resolve()
    assert local_resolved == _README_PATH.resolve()
    assert local_resolved.is_file()

    # J17 / F03: M01-M12 matrix complete and correct; honest boundaries.
    for token in ("M01", "M02", "M03", "M04", "M05", "M06", "M07", "M08", "M09", "M10", "M11", "M12"):
        assert token in local, f"missing matrix row {token}"
    assert "LIVE_DISABLED" in local
    assert "offline" in local
    for version in ("2.13.5", "0.162.7", "0.54.0", "0.28.1", "9.1.1", "1.64.1", "0.16.9"):
        assert version in local, f"missing version {version}"

    # J18: distinguish prepared-dependency source launch from fresh-environment install.
    assert "未运行" in local or "not_run" in local or "未实测" in local

    # J19: schema/format compatibility statement.
    assert "schema" in local.lower() or "sessions.sqlite3" in local

    # J20: link targets exist; original U20 documented-capability strings remain; no API-key example,
    # no automatic cleanup/unauthorized publication step.
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
        assert expected in readme, f"missing U20 requirement in README: {expected}"
    assert "sk-" not in readme and "sk-" not in local
    assert "LOCAL_DELIVERY.zh-CN.md" in readme
