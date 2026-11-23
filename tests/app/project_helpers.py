"""Shared existing-project fixtures and API/proposal helpers for APP-T02 tests."""

from __future__ import annotations

import ctypes
import hashlib
import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from app_helpers import FAKE_TEST_TOKEN, auth_headers, pick_free_loopback_port
from forecontract_app.models import HostConfig


def make_project_host_config(
    base_dir: Path,
    *,
    allowed_roots: tuple[Path, ...] = (),
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
        project_roots=allowed_roots,
    )


def create_buggy_calc_project(root: Path) -> Path:
    """Create a realistic minimal existing Python project with a deliberate subtraction bug."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "calc.py").write_text(
        '"""Simple calculator module."""\n\n\n'
        "def add_numbers(a: int, b: int) -> int:\n"
        "    return a - b\n",
        encoding="utf-8",
    )
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_calc.py").write_text(
        "from calc import add_numbers\n\n\n"
        "def test_add_basic() -> None:\n"
        "    assert add_numbers(2, 3) == 5\n"
        "    assert add_numbers(-1, 4) == 3\n",
        encoding="utf-8",
    )
    return root


def create_multifile_project(
    root: Path,
    *,
    include_large_notes: bool = True,
    doc_count: int = 300,
) -> Path:
    """Create the multi-file project fixture with AGENTS.md, src/, tests/, check_build.py, large notes, and excluded dirs."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "AGENTS.md").write_text(
        "# Project Rules\n\n"
        "- Only modify `src/` modules when fixing bugs.\n"
        "- Do not edit `tests/` or `check_build.py`.\n",
        encoding="utf-8",
    )
    src = root / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "__init__.py").write_text("", encoding="utf-8")
    (src / "mathops.py").write_text(
        '"""Arithmetic helpers."""\n\n\n'
        "def add(a: int, b: int) -> int:\n"
        "    return a - b\n",
        encoding="utf-8",
    )
    (src / "formatting.py").write_text(
        '"""Formatting helpers."""\n\n\n'
        "def format_status(name: str) -> str:\n"
        '    return f"{name}: ERROR"\n\n\n'
        "def get_ok_label() -> str:\n"
        "    from src.constants import LABEL_OK\n\n"
        "    return LABEL_OK\n",
        encoding="utf-8",
    )
    tests = root / "tests"
    tests.mkdir(parents=True, exist_ok=True)
    (tests / "test_mathops.py").write_text(
        "from src.formatting import format_status, get_ok_label\n"
        "from src.mathops import add\n\n\n"
        "def test_add_positive() -> None:\n"
        "    assert add(2, 3) == 5\n"
        "    assert add(10, 5) == 15\n\n\n"
        "def test_format_and_constants() -> None:\n"
        '    assert get_ok_label() == "OK"\n'
        '    assert format_status("build") == "build: OK"\n',
        encoding="utf-8",
    )
    (root / "check_build.py").write_text(
        "import sys\n"
        "from src.formatting import format_status, get_ok_label\n"
        "from src.mathops import add\n\n"
        "assert add(4, 6) == 10\n"
        'assert get_ok_label() == "OK"\n'
        'assert format_status("pkg") == "pkg: OK"\n'
        'print("BUILD_CHECK_OK")\n'
        "sys.exit(0)\n",
        encoding="utf-8",
    )

    if include_large_notes:
        notes = root / "notes"
        notes.mkdir(exist_ok=True)
        # >64 KiB file containing multi-byte UTF-8, CRLF, and a >20 KiB single line
        long_line = ("中文说明-LONG-" * 1500) + "\r\n"
        extra_lines = "".join(f"第{i:04d}行：UTF-8与CRLF混合内容\r\n" for i in range(600))
        (notes / "large.txt").write_bytes((long_line + extra_lines).encode("utf-8"))

    if doc_count > 0:
        docs = root / "docs"
        docs.mkdir(exist_ok=True)
        payload_chunk = "searchable documentation payload line.\n" * 400  # ~15.6 KiB each
        for idx in range(doc_count):
            suffix = "TARGET_IN_BATCH_TWO\n" if idx == 250 else ""
            (docs / f"item{idx:03d}.txt").write_text(
                f"doc-{idx:03d}\n{payload_chunk}{suffix}",
                encoding="utf-8",
            )

    # Fixed excluded dirs & files
    (root / ".git").mkdir(exist_ok=True)
    (root / ".git" / "config").write_text("[core]\n\trepositoryformatversion = 0\n", encoding="utf-8")
    (root / "__pycache__").mkdir(exist_ok=True)
    (root / "__pycache__" / "mathops.cpython-312.pyc").write_bytes(b"\x00\x01\x02\x03")
    (root / ".env").write_text("SECRET_TOKEN=topsecret\n", encoding="utf-8")
    (root / ".env.example").write_text("SECRET_TOKEN=example\n", encoding="utf-8")
    (root / "secrets.json").write_text('{"api_key": "fake"}\n', encoding="utf-8")
    return root


def write_barrier_script(
    script_path: Path,
    *,
    spawn_child: bool = False,
) -> Path:
    """Write a deterministic Python script using ready-file & release-flag synchronization."""
    child_code = (
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "p = Path(sys.argv[1])\n"
        "r = Path(sys.argv[2])\n"
        "t = Path(str(p) + '.tmp')\n"
        "t.write_text(str(os.getpid()), encoding='utf-8')\n"
        "os.replace(t, p)\n"
        "end = time.monotonic() + 60.0\n"
        "while not r.exists() and time.monotonic() < end:\n"
        "    time.sleep(0.02)\n"
    )
    child_snippet = (
        "    child = subprocess.Popen(\n"
        f"        [sys.executable, '-B', '-c', {child_code!r}, str(child_pid_file), str(release_file)]\n"
        "    )\n"
        "    deadline = time.monotonic() + 8.0\n"
        "    while True:\n"
        "        if child.poll() is not None:\n"
        "            raise RuntimeError(f'child exited early with {child.returncode}')\n"
        "        try:\n"
        "            if child_pid_file.exists():\n"
        "                parsed_pid = int(child_pid_file.read_text(encoding='utf-8').strip())\n"
        "                if parsed_pid > 0 and parsed_pid != os.getpid():\n"
        "                    break\n"
        "        except (OSError, ValueError):\n"
        "            pass\n"
        "        if time.monotonic() > deadline:\n"
        "            child.kill()\n"
        "            raise TimeoutError('child did not write child_pid_file before timeout')\n"
        "        time.sleep(0.02)\n"
        if spawn_child
        else ""
    )
    script_path.write_text(
        "import os, subprocess, sys, time\n"
        "from pathlib import Path\n\n"
        "ready_file = Path(sys.argv[1])\n"
        "release_file = Path(sys.argv[2])\n"
        "child_pid_file = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(str(ready_file) + '.cpid')\n"
        "if __name__ == '__main__':\n"
        f"{child_snippet}"
        "    ready_tmp = Path(str(ready_file) + '.tmp')\n"
        "    ready_tmp.write_text(str(os.getpid()), encoding='utf-8')\n"
        "    os.replace(ready_tmp, ready_file)\n"
        "    print('READY_SIGNAL', flush=True)\n"
        "    while not release_file.exists():\n"
        "        time.sleep(0.02)\n"
        "    print('RELEASE_DONE', flush=True)\n",
        encoding="utf-8",
    )
    return script_path


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def is_pid_alive(pid: int) -> bool:
    """Return True if an OS process with ``pid`` is currently running."""
    if pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == still_active
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    return True


def try_create_junction(link_path: Path, target_path: Path) -> bool:
    """Attempt to create an NTFS directory junction cleanly; returns False if OS disallows."""
    if os.name != "nt":
        try:
            os.symlink(target_path, link_path, target_is_directory=True)
            return True
        except OSError:
            return False
    proc = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link_path), str(target_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0 and link_path.exists()


async def register_and_start_project_run(
    client: Any,
    config: HostConfig,
    *,
    name: str,
    root: Path,
    goal: str,
    model: str = "offline-project-v1",
    provider: str = "offline",
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Helper calling HTTP register -> session -> start_run and returning JSON payloads."""
    headers = auth_headers(config)
    proj_resp = await client.post(
        "/api/v1/projects/register",
        headers=headers,
        json={"name": name, "root": str(root), "confirmed_read": True},
    )
    assert proj_resp.status_code == 201, proj_resp.text
    proj = proj_resp.json()

    sess_resp = await client.post(
        "/api/v1/sessions",
        headers=headers,
        json={
            "project_id": proj["project_id"],
            "model": {"provider": provider, "model": model, "thinking": "disabled"},
        },
    )
    assert sess_resp.status_code == 201, sess_resp.text
    sess = sess_resp.json()

    run_resp = await client.post(
        f"/api/v1/sessions/{sess['session_id']}/runs",
        headers=headers,
        json={
            "client_request_id": str(uuid.uuid4()),
            "goal": goal,
            "confirmed_scope": True,
        },
    )
    assert run_resp.status_code == 202, run_resp.text
    return proj, sess, run_resp.json()
