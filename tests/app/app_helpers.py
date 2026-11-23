"""Shared offline fixtures, real server harness, Windows PID checks, and Playwright helpers."""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import os
import queue
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Collection
from ctypes import wintypes
from pathlib import Path
from typing import Any, Self

import httpx
import uvicorn
from forecontract_app.demo import (
    OfflineMetricsProvider,
    build_controlled_coding_task,
    initialize_controlled_project,
)
from forecontract_app.models import HostConfig, ProjectView, ProviderFactory
from forecontract_app.server import create_app
from forecontract_coding.process import build_minimal_windows_env
from forecontract_llm import ModelRequest, ModelResponse

FAKE_TEST_TOKEN = "test-only-local-access-token-not-a-credential-0123456789abcdef0123456789"
CREDENTIAL_ENV_CANARY = "sk-test-only-forbidden-canary-never-inherit"

_SAFE_SUBPROCESS_KEYS = (
    "COMSPEC",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PATH",
    "PATHEXT",
    "PLAYWRIGHT_BROWSERS_PATH",
    "PROCESSOR_ARCHITECTURE",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONIOENCODING",
    "PYTHONPATH",
    "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "WINDIR",
)


def make_clean_subprocess_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build an explicit allowlisted environment without any API key or credential vars."""
    env: dict[str, str] = {}
    for key in _SAFE_SUBPROCESS_KEYS:
        val = os.environ.get(key)
        if val is not None:
            env[key] = val
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if extra:
        env.update(extra)
    return env


def pick_free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def make_host_config(
    base_dir: Path,
    *,
    port: int | None = None,
    token: str = FAKE_TEST_TOKEN,
    max_sessions: int = 8,
    max_runs_per_session: int = 16,
    max_events_per_run: int = 512,
) -> HostConfig:
    data_root = base_dir / "data"
    workspace_parent = base_dir / "workspaces"
    data_root.mkdir(parents=True, exist_ok=True)
    workspace_parent.mkdir(parents=True, exist_ok=True)
    return HostConfig(
        data_root=data_root,
        workspace_parent=workspace_parent,
        python_executable=Path(sys.executable),
        bind_port=port if port is not None else pick_free_loopback_port(),
        access_token=token,
        max_sessions=max_sessions,
        max_runs_per_session=max_runs_per_session,
        max_events_per_run=max_events_per_run,
    )


def auth_headers(config: HostConfig, *, with_origin: bool = True) -> dict[str, str]:
    headers = {
        "Host": f"127.0.0.1:{config.bind_port}",
        "Authorization": f"Bearer {config.access_token}",
    }
    if with_origin:
        headers["Origin"] = f"http://127.0.0.1:{config.bind_port}"
    return headers


def tree_hashes(root: Path, *, exclude_relative: Collection[str] = ()) -> dict[str, str]:
    if not root.exists():
        return {}
    excluded = set(exclude_relative)
    result: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            rel = p.relative_to(root).as_posix()
            if rel in excluded:
                continue
            result[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return result


def wait_until(predicate: Callable[[], Any], *, timeout: float = 10.0, interval: float = 0.02) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        val = predicate()
        if val:
            return val
        time.sleep(interval)
    raise AssertionError("timed out waiting for predicate")


async def async_wait_until(
    predicate: Callable[[], Any], *, timeout: float = 10.0, interval: float = 0.02
) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        val = predicate()
        if val:
            return val
        await asyncio.sleep(interval)
    raise AssertionError("timed out waiting for async predicate")


def is_pid_alive(pid: int) -> bool:
    """Inspect real OS process liveness via Win32 OpenProcess + GetExitCodeProcess."""
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD(0)
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
        return bool(ok and code.value == STILL_ACTIVE)
    finally:
        kernel32.CloseHandle(handle)


class _StartedUvicornServer(uvicorn.Server):
    def __init__(self, uv_config: uvicorn.Config, started_evt: threading.Event) -> None:
        super().__init__(uv_config)
        self._started_evt = started_evt

    async def startup(self, sockets: list[Any] | None = None) -> None:
        await super().startup(sockets=sockets)
        if self.started:
            self._started_evt.set()


class LiveTcpTestServer:
    """Manage a real single-loop Uvicorn server on ``127.0.0.1`` in a background thread."""

    def __init__(
        self,
        *,
        config: HostConfig,
        provider_factory: ProviderFactory | None = None,
    ) -> None:
        self.config = config
        self.app = create_app(config=config, provider_factory=provider_factory)
        self.base_url = f"http://127.0.0.1:{config.bind_port}"
        self._started_evt = threading.Event()
        self._uv_config = uvicorn.Config(
            app=self.app,
            host="127.0.0.1",
            port=config.bind_port,
            workers=1,
            reload=False,
            access_log=False,
            log_level="critical",
            timeout_graceful_shutdown=8,
        )
        self._server = _StartedUvicornServer(self._uv_config, self._started_evt)
        self._thread: threading.Thread | None = None
        self.error: BaseException | None = None

    def __enter__(self) -> Self:
        def _runner() -> None:
            try:
                if sys.platform == "win32":
                    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
                        runner.run(self._server.serve())
                else:
                    asyncio.run(self._server.serve())
            except BaseException as exc:  # noqa: BLE001
                self.error = exc
            finally:
                self._started_evt.set()

        self._thread = threading.Thread(target=_runner, daemon=True)
        self._thread.start()
        if not self._started_evt.wait(timeout=10.0):
            raise AssertionError("uvicorn server failed to start in time")
        if self.error is not None:
            raise AssertionError(f"uvicorn startup failed: {self.error!r}")
        if not self._server.started:
            raise AssertionError("uvicorn server exited before listening")
        return self

    def __exit__(self, *_: object) -> None:
        self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10.0)


def parse_sse_frames(chunks_iter: Any) -> list[dict[str, Any]]:
    """Incrementally parse SSE frames from a streaming byte/text iterator."""
    buf = ""
    frames: list[dict[str, Any]] = []
    ev_name = ""
    ev_id = ""
    data_lines: list[str] = []
    for raw_line in chunks_iter:
        if isinstance(raw_line, bytes):
            buf += raw_line.decode("utf-8")
        else:
            buf += raw_line
        lines = buf.splitlines(keepends=True)
        if lines and not lines[-1].endswith("\n"):
            buf = lines.pop()
        else:
            buf = ""
        for line_with_nl in lines:
            line = line_with_nl.rstrip("\r\n")
            if line == "":
                if data_lines:
                    frames.append(
                        {"id": ev_id, "event": ev_name, "data": "\n".join(data_lines)}
                    )
                ev_name = ""
                ev_id = ""
                data_lines = []
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                ev_name = line[6:].strip()
            elif line.startswith("id:"):
                ev_id = line[3:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].strip())
    return frames


class CountingMetricsProvider:
    """Wrap ``OfflineMetricsProvider`` recording exact inner/outer calls & payloads."""

    def __init__(self, inner: Any | None = None) -> None:
        self._inner = inner or OfflineMetricsProvider()
        self.calls = 0
        self.request_dumps: list[dict[str, Any]] = []
        self.response_dumps: list[dict[str, Any]] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        self.request_dumps.append(request.model_dump(mode="json"))
        resp = await self._inner.complete(request)
        self.response_dumps.append(resp.model_dump(mode="json"))
        return resp


def terminate_process_tree(proc: subprocess.Popen[Any]) -> None:
    """Cleanly close a test-owned subprocess (only its own spawned process tree)."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=3.0)
    except Exception:  # noqa: BLE001
        proc.kill()
        proc.wait(timeout=3.0)


def check_local_health(config: HostConfig) -> httpx.Response:
    return httpx.get(
        f"http://127.0.0.1:{config.bind_port}/api/v1/health",
        headers=auth_headers(config, with_origin=False),
        timeout=5.0,
    )


def unique_uuid() -> str:
    return str(uuid.uuid4())


def read_line_with_deadline(stream: Any, *, timeout: float = 15.0) -> str:
    """Read one non-empty text line from ``stream`` enforcing a real wall-clock deadline."""
    deadline = time.monotonic() + timeout
    out_q: queue.Queue[tuple[str, str]] = queue.Queue()

    def _reader() -> None:
        try:
            while True:
                line = stream.readline()
                if line == "":
                    out_q.put(("eof", ""))
                    return
                if line.strip():
                    out_q.put(("line", line))
                    return
        except BaseException as exc:  # noqa: BLE001
            out_q.put(("error", repr(exc)))

    threading.Thread(target=_reader, daemon=True).start()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AssertionError("timed out before waiting for process stdout line")
    try:
        kind, payload = out_q.get(timeout=remaining)
    except queue.Empty as exc:
        raise AssertionError(f"timed out after {timeout}s waiting for readiness stdout line") from exc
    if kind == "error":
        raise AssertionError(f"failed reading readiness stdout line: {payload}")
    if kind == "eof":
        raise AssertionError("subprocess closed stdout before emitting readiness line")
    return payload


_SHARED_CHILD_ENV_OBSERVATION: dict[str, Any] | None = None


def record_shared_child_env_observation(observation: dict[str, Any]) -> None:
    """Store A19 ready-child env canary observation for shared reuse without extra processes."""
    if "OPENAI_API_KEY_present" not in observation or not isinstance(
        observation["OPENAI_API_KEY_present"], bool
    ):
        raise AssertionError(f"invalid child env observation payload: {observation!r}")
    global _SHARED_CHILD_ENV_OBSERVATION
    _SHARED_CHILD_ENV_OBSERVATION = dict(observation)


def verify_production_child_env_equivalence(work_dir: Path) -> dict[str, str]:
    """Verify ``build_controlled_coding_task`` uses minimal canary-free Windows child env."""
    prod_data = work_dir / "prod_data"
    prod_ws = work_dir / "prod_ws"
    prod_out = work_dir / "prod_out"
    prod_data.mkdir(parents=True, exist_ok=True)
    prod_ws.mkdir(parents=True, exist_ok=True)
    prod_out.mkdir(parents=True, exist_ok=True)
    spec = initialize_controlled_project(
        project_id=str(uuid.uuid4()),
        name="env-equiv-check",
        data_root=prod_data,
        workspace_parent=prod_ws,
    )
    prod_task, _, _ = build_controlled_coding_task(
        project=spec,
        run_id="env-equiv",
        goal="check minimal env",
        output_root=prod_out,
        python_executable=Path(sys.executable),
    )
    prod_spec = prod_task.commands["pytest_acc"]
    return build_minimal_windows_env(spec=prod_spec, temp_dir=work_dir)


def ensure_shared_child_env_observation(work_dir: Path | None = None) -> dict[str, Any]:
    """Return A19's recorded ready-child env observation or fail explicitly without spawning a probe."""
    _ = work_dir
    if _SHARED_CHILD_ENV_OBSERVATION is None:
        raise AssertionError(
            "A19 shared child environment observation is missing; "
            "test_a19_real_command_cancel must produce real child evidence before test_a21_routes_and_errors"
        )
    return dict(_SHARED_CHILD_ENV_OBSERVATION)


def make_factory_spy(
    created: list[CountingMetricsProvider],
) -> ProviderFactory:
    def _factory(*, project: ProjectView, run_id: str) -> CountingMetricsProvider:
        _ = (project, run_id)
        prov = CountingMetricsProvider()
        created.append(prov)
        return prov

    return _factory
