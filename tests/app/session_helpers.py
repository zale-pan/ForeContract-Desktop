"""Shared offline fixtures, Core/HTTP/CLI/Job helpers for APP-T03 durable session tests."""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import subprocess
import sys
import uuid
from collections.abc import Callable
from ctypes import wintypes
from pathlib import Path
from typing import Any

from app_helpers import (
    make_clean_subprocess_env,
    pick_free_loopback_port,
    read_line_with_deadline,
    terminate_process_tree,
)
from forecontract_app.models import HostConfig
from forecontract_app.project_models import PROJECT_SCHEMA_VERSION
from forecontract_app.session_models import HistoryItem, compute_history_item_sha256
from forecontract_llm import ModelRequest, ModelResponse, ModelUsage


def build_history_item_record(
    *,
    seq: int,
    project_id: str,
    session_id: str,
    run_id: str,
    kind: str,
    text: str,
    source_ids: list[str] | None = None,
    truncated: bool = False,
) -> HistoryItem:
    item_id = str(uuid.uuid4())
    srcs = list(source_ids or [run_id])
    sha = compute_history_item_sha256(
        seq=seq,
        item_id=item_id,
        project_id=project_id,
        session_id=session_id,
        run_id=run_id,
        kind=kind,  # type: ignore[arg-type]
        text=text,
        source_ids=srcs,
        truncated=truncated,
    )
    return HistoryItem(
        seq=seq,
        item_id=item_id,
        project_id=project_id,
        session_id=session_id,
        run_id=run_id,
        kind=kind,  # type: ignore[arg-type]
        text=text,
        source_ids=srcs,
        truncated=truncated,
        sha256=sha,
    )


def forecast_json(
    *,
    action_type: str,
    payload: dict[str, Any],
    outcome_type: str = "project_tool_result",
    outcome_payload: dict[str, Any] | None = None,
    require_success: bool = True,
    task_hint: bool | None = None,
) -> str:
    if outcome_payload is None:
        outcome_payload = (
            {"action_type": action_type, "require_success": require_success}
            if outcome_type == "project_tool_result"
            else {}
        )
    obj: dict[str, Any] = {
        "schema_version": PROJECT_SCHEMA_VERSION,
        "intent": f"execute {action_type}",
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
                "outcome_type": outcome_type,
                "payload": outcome_payload,
                "required": True,
            }
        ],
    }
    if task_hint is not None:
        obj["task_completion_hint"] = task_hint
    return json.dumps(obj, ensure_ascii=False)


def start_cli_host_subprocess(
    *,
    data_root: Path,
    workspace_parent: Path,
    port: int | None = None,
    project_roots: tuple[Path, ...] = (),
    extra_args: tuple[str, ...] = (),
) -> tuple[subprocess.Popen[str], str, str, int]:
    """Spawn CLI server subprocess safely using clean allowlisted env and read startup URL."""
    bound_port = port if port is not None else pick_free_loopback_port()
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
        str(bound_port),
        "--offline",
        "--control-stdin",
        *[arg for pr in project_roots for arg in ("--project-root", str(pr))],
        *extra_args,
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
    assert line.startswith(prefix), f"unexpected CLI stdout: {line!r}"
    full_url = line[len(prefix) :].strip()
    token = full_url.split("#token=", 1)[1]
    base_url = full_url.split("/#token=", 1)[0]
    return proc, base_url, token, bound_port


class OwnJobHandle:
    """Windows Job Object owning ONLY the test's own spawned host subprocess tree."""

    def __init__(self, proc: subprocess.Popen[Any]) -> None:
        self.proc = proc
        self._job: Any = None
        if sys.platform == "win32":
            kernel32 = ctypes.windll.kernel32
            self._job = kernel32.CreateJobObjectW(None, None)
            if not self._job:
                raise RuntimeError("CreateJobObjectW failed")
            # Ensure handle belongs only to this spawned process tree
            h_proc = kernel32.OpenProcess(0x1F0FFF, False, int(proc.pid))
            if not h_proc:
                kernel32.CloseHandle(self._job)
                self._job = None
                raise RuntimeError("OpenProcess failed")
            try:
                ok = kernel32.AssignProcessToJobObject(
                    wintypes.HANDLE(self._job), wintypes.HANDLE(h_proc)
                )
                if not ok:
                    raise RuntimeError("AssignProcessToJobObject failed")
            finally:
                kernel32.CloseHandle(h_proc)

    def terminate_tree(self) -> None:
        if sys.platform == "win32" and self._job:
            ctypes.windll.kernel32.TerminateJobObject(wintypes.HANDLE(self._job), 1)
            ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(self._job))
            self._job = None
            try:
                self.proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                pass
        else:
            terminate_process_tree(self.proc)

    def close_handle_only(self) -> None:
        if sys.platform == "win32" and self._job:
            ctypes.windll.kernel32.CloseHandle(wintypes.HANDLE(self._job))
            self._job = None


class SpyUsageProvider:
    """Scripted or delegating offline ModelProvider recording requests & returning optional ModelUsage."""

    def __init__(
        self,
        builder: Callable[[int, ModelRequest], str],
        *,
        usage_fn: Callable[[int, ModelRequest], ModelUsage | None] | None = None,
    ) -> None:
        self._builder = builder
        self._usage_fn = usage_fn
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        idx = len(self.requests)
        self.requests.append(request)
        content = self._builder(idx, request)
        usage = self._usage_fn(idx, request) if self._usage_fn is not None else None
        return ModelResponse(
            schema_version="0.1",
            request_id=request.request_id,
            status="ok",
            content=content,
            usage=usage,
        )


def make_reopened_host_config(
    orig: HostConfig,
    *,
    port: int | None = None,
    token: str | None = None,
    project_roots: tuple[Path, ...] | None = None,
    workspace_parent: Path | None = None,
    max_sessions: int | None = None,
    max_runs_per_session: int | None = None,
) -> HostConfig:
    return HostConfig(
        data_root=orig.data_root,
        workspace_parent=workspace_parent if workspace_parent is not None else orig.workspace_parent,
        python_executable=orig.python_executable,
        bind_port=port if port is not None else pick_free_loopback_port(),
        access_token=token if token is not None else f"reopen-token-{uuid.uuid4().hex}",
        max_sessions=max_sessions if max_sessions is not None else orig.max_sessions,
        max_runs_per_session=(
            max_runs_per_session if max_runs_per_session is not None else orig.max_runs_per_session
        ),
        max_events_per_run=orig.max_events_per_run,
        project_roots=project_roots if project_roots is not None else orig.project_roots,
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def wait_for_run_terminal_async(
    service: Any, *, session_id: str, run_id: str, timeout: float = 20.0
) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout
    terminal = {"succeeded", "failed", "cancelled", "unknown"}
    while asyncio.get_running_loop().time() < deadline:
        rv = service.get_run(session_id=session_id, run_id=run_id)
        if rv.state in terminal:
            return rv
        await asyncio.sleep(0.03)
    raise AssertionError(f"timed out waiting for run {run_id}")
