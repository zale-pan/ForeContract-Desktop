"""Windows Job-Object-backed bidirectional stdio process manager for local MCP servers."""

from __future__ import annotations

import asyncio
import ctypes
import msvcrt
import os
import subprocess
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Final

from forecontract_app.extension_models import (
    MAX_JSONRPC_FRAME_BYTES,
    MAX_MCP_STDERR_BYTES,
    MCP_STOP_TIMEOUT_SECONDS,
    ExtensionCleanup,
    ExtensionError,
    StdioMcpServerConfig,
    compute_file_sha256,
)

_CREATE_SUSPENDED: Final[int] = 0x00000004
_CREATE_UNICODE_ENVIRONMENT: Final[int] = 0x00000400
_CREATE_NEW_PROCESS_GROUP: Final[int] = 0x00000200
_CREATE_NO_WINDOW: Final[int] = 0x08000000

_HANDLE_FLAG_INHERIT: Final[int] = 0x00000001
_STARTF_USESTDHANDLES: Final[int] = 0x00000100

_JOB_OBJECT_EXTENDED_LIMIT_INFO: Final[int] = 9
_JOB_OBJECT_BASIC_ACCOUNTING_INFO: Final[int] = 1
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: Final[int] = 0x00002000

_WAIT_OBJECT_0: Final[int] = 0x00000000
_INVALID_HANDLE_VALUE: Final[int] = ctypes.c_void_p(-1).value or -1

_SAFE_WIN_BASE_KEYS: Final[tuple[str, ...]] = (
    "COMSPEC",
    "NUMBER_OF_PROCESSORS",
    "OS",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "WINDIR",
)


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    ]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_int64),
        ("TotalKernelTime", ctypes.c_int64),
        ("ThisPeriodTotalUserTime", ctypes.c_int64),
        ("ThisPeriodTotalKernelTime", ctypes.c_int64),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


def _kernel32() -> ctypes.WinDLL:
    if os.name != "nt":
        raise ExtensionError("MCP_SPAWN_FAILED", "Windows Job stdio host requires Windows", status_code=500)
    dll = ctypes.WinDLL("kernel32", use_last_error=True)
    dll.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    dll.CreateJobObjectW.restype = wintypes.HANDLE
    dll.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    dll.SetInformationJobObject.restype = wintypes.BOOL
    dll.QueryInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    dll.QueryInformationJobObject.restype = wintypes.BOOL
    dll.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    dll.AssignProcessToJobObject.restype = wintypes.BOOL
    dll.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    dll.TerminateJobObject.restype = wintypes.BOOL
    dll.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    dll.TerminateProcess.restype = wintypes.BOOL
    dll.ResumeThread.argtypes = [wintypes.HANDLE]
    dll.ResumeThread.restype = wintypes.DWORD
    dll.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    dll.WaitForSingleObject.restype = wintypes.DWORD
    dll.CreatePipe.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(_SECURITY_ATTRIBUTES),
        wintypes.DWORD,
    ]
    dll.CreatePipe.restype = wintypes.BOOL
    dll.SetHandleInformation.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD]
    dll.SetHandleInformation.restype = wintypes.BOOL
    dll.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.POINTER(_SECURITY_ATTRIBUTES),
        ctypes.POINTER(_SECURITY_ATTRIBUTES),
        wintypes.BOOL,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPCWSTR,
        ctypes.POINTER(_STARTUPINFOW),
        ctypes.POINTER(_PROCESS_INFORMATION),
    ]
    dll.CreateProcessW.restype = wintypes.BOOL
    dll.CloseHandle.argtypes = [wintypes.HANDLE]
    dll.CloseHandle.restype = wintypes.BOOL
    return dll


def _build_minimal_mcp_env(temp_dir: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for key in _SAFE_WIN_BASE_KEYS:
        val = os.environ.get(key)
        if val:
            env[key] = val
    temp_dir.mkdir(parents=True, exist_ok=True)
    temp_str = str(temp_dir)
    env["TEMP"] = temp_str
    env["TMP"] = temp_str
    env["TMPDIR"] = temp_str
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONUTF8"] = "1"
    return env


def _encode_env_block(env: dict[str, str]) -> ctypes.Array[ctypes.c_wchar]:
    items = sorted(env.items(), key=lambda kv: kv[0].upper())
    block = "".join(f"{k}={v}\x00" for k, v in items) + "\x00"
    return ctypes.create_unicode_buffer(block)


def _query_active_processes(k32: ctypes.WinDLL, h_job: wintypes.HANDLE) -> int:
    acct = _JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
    retlen = wintypes.DWORD(0)
    ok = k32.QueryInformationJobObject(
        h_job,
        _JOB_OBJECT_BASIC_ACCOUNTING_INFO,
        ctypes.byref(acct),
        ctypes.sizeof(acct),
        ctypes.byref(retlen),
    )
    if not ok:
        return -1
    return int(acct.ActiveProcesses)


class McpFrameTooLargeSentinel:
    """Sentinel delivered to asyncio stdout queue when a raw line exceeds 256 KiB."""

    def __init__(self, byte_count: int) -> None:
        self.byte_count = byte_count


class McpJobProcess:
    """Bidirectional Windows Job-owned stdio child process with bounded teardown."""

    def __init__(self, *, server: StdioMcpServerConfig, scratch_root: Path) -> None:
        self._server = server
        self._scratch_root = scratch_root
        self._k32 = _kernel32()
        self._h_job: wintypes.HANDLE | None = None
        self._h_proc: wintypes.HANDLE | None = None
        self._pid: int | None = None
        self._in_fd: int | None = None
        self._out_fd: int | None = None
        self._err_fd: int | None = None
        self._in_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stdout_queue: asyncio.Queue[bytes | McpFrameTooLargeSentinel | None] | None = None
        self._out_thread: threading.Thread | None = None
        self._err_thread: threading.Thread | None = None
        self._stderr_lock = threading.Lock()
        self._stderr_buf = bytearray()
        self._stderr_total = 0
        self._stderr_truncated = False
        self._closed = False
        self._cleanup_result: ExtensionCleanup | None = None

    @property
    def pid(self) -> int | None:
        return self._pid

    @property
    def stdout_queue(self) -> asyncio.Queue[bytes | McpFrameTooLargeSentinel | None]:
        if self._stdout_queue is None:
            raise ExtensionError("MCP_NOT_STARTED", "MCP process not started", status_code=409)
        return self._stdout_queue

    def stderr_text(self) -> str:
        with self._stderr_lock:
            return self._stderr_buf.decode("utf-8", errors="replace")

    def start(self) -> None:
        """Spawn the configured stdio executable suspended inside a dedicated Windows Job Object."""
        if self._h_proc is not None:
            raise ExtensionError("MCP_ALREADY_STARTED", "MCP process already started", status_code=409)
        live_sha = compute_file_sha256(Path(self._server.executable))
        if live_sha != self._server.executable_sha256:
            raise ExtensionError(
                "EXECUTABLE_MODIFIED",
                "stdio MCP executable changed on disk before launch",
                status_code=409,
            )

        self._loop = asyncio.get_running_loop()
        self._stdout_queue = asyncio.Queue(maxsize=256)

        temp_dir = self._scratch_root / "mcp_tmp" / self._server.id
        env_block = _encode_env_block(_build_minimal_mcp_env(temp_dir))
        cmdline = subprocess.list2cmdline([self._server.executable, *list(self._server.argv)])

        sa = _SECURITY_ATTRIBUTES()
        sa.nLength = ctypes.sizeof(_SECURITY_ATTRIBUTES)
        sa.lpSecurityDescriptor = None
        sa.bInheritHandle = True

        in_read = wintypes.HANDLE()
        in_write = wintypes.HANDLE()
        out_read = wintypes.HANDLE()
        out_write = wintypes.HANDLE()
        err_read = wintypes.HANDLE()
        err_write = wintypes.HANDLE()

        def _safe_close(handle: wintypes.HANDLE | None) -> None:
            if handle is None:
                return
            raw = getattr(handle, "value", handle)
            if raw and raw != _INVALID_HANDLE_VALUE:
                self._k32.CloseHandle(handle)

        if not self._k32.CreatePipe(ctypes.byref(in_read), ctypes.byref(in_write), ctypes.byref(sa), 0):
            raise ExtensionError("MCP_SPAWN_FAILED", "failed to create stdin pipe", status_code=500)
        if not self._k32.CreatePipe(ctypes.byref(out_read), ctypes.byref(out_write), ctypes.byref(sa), 0):
            _safe_close(in_read)
            _safe_close(in_write)
            raise ExtensionError("MCP_SPAWN_FAILED", "failed to create stdout pipe", status_code=500)
        if not self._k32.CreatePipe(ctypes.byref(err_read), ctypes.byref(err_write), ctypes.byref(sa), 0):
            for h in (in_read, in_write, out_read, out_write):
                _safe_close(h)
            raise ExtensionError("MCP_SPAWN_FAILED", "failed to create stderr pipe", status_code=500)

        self._k32.SetHandleInformation(in_write, _HANDLE_FLAG_INHERIT, 0)
        self._k32.SetHandleInformation(out_read, _HANDLE_FLAG_INHERIT, 0)
        self._k32.SetHandleInformation(err_read, _HANDLE_FLAG_INHERIT, 0)

        h_job = self._k32.CreateJobObjectW(None, None)
        if not h_job:
            for h in (in_read, in_write, out_read, out_write, err_read, err_write):
                _safe_close(h)
            raise ExtensionError("MCP_SPAWN_FAILED", "failed to create Windows Job Object", status_code=500)

        ext = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        ext.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._k32.SetInformationJobObject(
            h_job, _JOB_OBJECT_EXTENDED_LIMIT_INFO, ctypes.byref(ext), ctypes.sizeof(ext)
        ):
            for h in (h_job, in_read, in_write, out_read, out_write, err_read, err_write):
                _safe_close(h)
            raise ExtensionError("MCP_SPAWN_FAILED", "failed to configure KILL_ON_JOB_CLOSE", status_code=500)

        si = _STARTUPINFOW()
        si.cb = ctypes.sizeof(_STARTUPINFOW)
        si.dwFlags = _STARTF_USESTDHANDLES
        si.hStdInput = in_read
        si.hStdOutput = out_write
        si.hStdError = err_write
        pi = _PROCESS_INFORMATION()

        created = self._k32.CreateProcessW(
            self._server.executable,
            ctypes.create_unicode_buffer(cmdline),
            None,
            None,
            True,
            _CREATE_SUSPENDED
            | _CREATE_UNICODE_ENVIRONMENT
            | _CREATE_NEW_PROCESS_GROUP
            | _CREATE_NO_WINDOW,
            ctypes.byref(env_block),
            self._server.cwd,
            ctypes.byref(si),
            ctypes.byref(pi),
        )

        # Parent immediately closes child-end pipe handles.
        _safe_close(in_read)
        _safe_close(out_write)
        _safe_close(err_write)

        if not created:
            _safe_close(in_write)
            _safe_close(out_read)
            _safe_close(err_read)
            _safe_close(h_job)
            raise ExtensionError(
                "MCP_SPAWN_FAILED",
                f"CreateProcessW failed with WinError {ctypes.get_last_error()}",
                status_code=500,
            )

        h_proc = pi.hProcess
        h_thread = pi.hThread
        self._pid = int(pi.dwProcessId)

        if not self._k32.AssignProcessToJobObject(h_job, h_proc):
            self._k32.TerminateProcess(h_proc, 1)
            _safe_close(h_thread)
            _safe_close(h_proc)
            _safe_close(h_job)
            _safe_close(in_write)
            _safe_close(out_read)
            _safe_close(err_read)
            raise ExtensionError("MCP_SPAWN_FAILED", "AssignProcessToJobObject failed", status_code=500)

        assert in_write.value is not None and out_read.value is not None and err_read.value is not None
        self._in_fd = msvcrt.open_osfhandle(in_write.value, 0)
        self._out_fd = msvcrt.open_osfhandle(out_read.value, os.O_RDONLY)
        self._err_fd = msvcrt.open_osfhandle(err_read.value, os.O_RDONLY)
        self._h_job = h_job
        self._h_proc = h_proc

        self._out_thread = threading.Thread(target=self._read_stdout_loop, daemon=True)
        self._err_thread = threading.Thread(target=self._read_stderr_loop, daemon=True)
        self._out_thread.start()
        self._err_thread.start()

        if self._k32.ResumeThread(h_thread) == 0xFFFFFFFF:
            _safe_close(h_thread)
            raise ExtensionError("MCP_SPAWN_FAILED", "ResumeThread failed after job assignment", status_code=500)
        _safe_close(h_thread)

    def _enqueue_stdout(self, item: bytes | McpFrameTooLargeSentinel | None) -> None:
        loop = self._loop
        q = self._stdout_queue
        if loop is None or q is None or loop.is_closed():
            return

        def _put() -> None:
            try:
                q.put_nowait(item)
            except asyncio.QueueFull:
                # Never silently drop EOF sentinel.
                if item is None:
                    try:
                        q.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                    try:
                        q.put_nowait(None)
                    except asyncio.QueueFull:
                        pass

        try:
            loop.call_soon_threadsafe(_put)
        except RuntimeError:
            pass

    def _read_stdout_loop(self) -> None:
        fd = self._out_fd
        if fd is None:
            self._enqueue_stdout(None)
            return
        buf = bytearray()
        oversized = False
        try:
            while True:
                try:
                    chunk = os.read(fd, 8192)
                except OSError:
                    break
                if not chunk:
                    break
                if oversized:
                    # Wait until newline resets oversized mode or pipe closes.
                    nl = chunk.find(b"\n")
                    if nl == -1:
                        continue
                    oversized = False
                    chunk = chunk[nl + 1 :]
                    if not chunk:
                        continue
                buf.extend(chunk)
                while True:
                    nl_idx = buf.find(b"\n")
                    if nl_idx == -1:
                        if len(buf) > MAX_JSONRPC_FRAME_BYTES + 2:
                            oversized = True
                            self._enqueue_stdout(McpFrameTooLargeSentinel(len(buf)))
                            buf.clear()
                        break
                    line = bytes(buf[:nl_idx])
                    del buf[: nl_idx + 1]
                    if line.endswith(b"\r"):
                        line = line[:-1]
                    if len(line) > MAX_JSONRPC_FRAME_BYTES:
                        self._enqueue_stdout(McpFrameTooLargeSentinel(len(line)))
                    elif line:
                        self._enqueue_stdout(line)
        finally:
            if buf:
                tail = bytes(buf[:-1] if buf.endswith(b"\r") else buf)
                if len(tail) > MAX_JSONRPC_FRAME_BYTES:
                    self._enqueue_stdout(McpFrameTooLargeSentinel(len(tail)))
                elif tail:
                    self._enqueue_stdout(tail)
            self._enqueue_stdout(None)

    def _read_stderr_loop(self) -> None:
        fd = self._err_fd
        if fd is None:
            return
        while True:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            with self._stderr_lock:
                self._stderr_total += len(chunk)
                rem = MAX_MCP_STDERR_BYTES - len(self._stderr_buf)
                if rem > 0:
                    self._stderr_buf.extend(chunk[:rem])
                if len(chunk) > rem:
                    self._stderr_truncated = True

    async def write_line(self, data: bytes) -> None:
        """Write one JSON-RPC line plus trailing newline to child stdin."""
        if self._closed or self._in_fd is None:
            raise ExtensionError("MCP_TRANSPORT_CLOSED", "stdio stdin is closed", status_code=409)
        payload = data if data.endswith(b"\n") else (data + b"\n")

        def _write_sync() -> None:
            with self._in_lock:
                fd = self._in_fd
                if fd is None:
                    raise OSError("stdin fd closed")
                view = memoryview(payload)
                written = 0
                while written < len(payload):
                    n = os.write(fd, view[written:])
                    if n <= 0:
                        raise OSError("short write to stdin pipe")
                    written += n

        try:
            await asyncio.to_thread(_write_sync)
        except OSError as exc:
            raise ExtensionError("MCP_TRANSPORT_ERROR", f"stdio write failed: {exc}", status_code=502) from exc

    async def close(self, *, timeout_seconds: float = MCP_STOP_TIMEOUT_SECONDS) -> ExtensionCleanup:
        """Close stdin then terminate the owned Windows Job within a single bounded deadline."""
        if self._cleanup_result is not None:
            return self._cleanup_result
        self._closed = True
        total_budget = max(0.2, float(timeout_seconds))

        def _close_sync() -> ExtensionCleanup:
            deadline = time.monotonic() + total_budget
            with self._in_lock:
                if self._in_fd is not None:
                    try:
                        os.close(self._in_fd)
                    except OSError:
                        pass
                    self._in_fd = None

            proc_exited = True
            if self._h_proc is not None:
                grace_ms = min(200, max(10, int((deadline - time.monotonic()) * 1000)))
                wait_rc = self._k32.WaitForSingleObject(self._h_proc, grace_ms)
                proc_exited = wait_rc == _WAIT_OBJECT_0

            active = 0
            if self._h_job is not None:
                active = _query_active_processes(self._k32, self._h_job)
                if not proc_exited or active != 0:
                    self._k32.TerminateJobObject(self._h_job, 1)
                    while True:
                        active = _query_active_processes(self._k32, self._h_job)
                        if active == 0 or time.monotonic() >= deadline:
                            break
                        time.sleep(0.01)
                self._k32.CloseHandle(self._h_job)
                self._h_job = None

            if self._h_proc is not None:
                rem_ms = min(50, max(0, int((deadline - time.monotonic()) * 1000)))
                if self._k32.WaitForSingleObject(self._h_proc, rem_ms) != _WAIT_OBJECT_0:
                    proc_exited = False
                else:
                    proc_exited = True
                self._k32.CloseHandle(self._h_proc)
                self._h_proc = None

            if self._out_fd is not None:
                try:
                    os.close(self._out_fd)
                except OSError:
                    pass
                self._out_fd = None
            if self._err_fd is not None:
                try:
                    os.close(self._err_fd)
                except OSError:
                    pass
                self._err_fd = None

            join_rem = max(0.02, min(0.2, deadline - time.monotonic()))
            if self._out_thread is not None:
                self._out_thread.join(timeout=join_rem)
            if self._err_thread is not None:
                self._err_thread.join(timeout=max(0.01, deadline - time.monotonic()))

            cleanup_ok = (active == 0) and proc_exited
            rem_count = 0 if active == 0 else (active if active > 0 else 1)
            return ExtensionCleanup(
                cleanup_ok=cleanup_ok,
                remaining_active=rem_count,
                details={
                    "transport": "stdio",
                    "server_id": self._server.id,
                    "pid": self._pid,
                    "stderr_bytes": self._stderr_total,
                    "stderr_truncated": self._stderr_truncated,
                },
            )

        res = await asyncio.to_thread(_close_sync)
        self._cleanup_result = res
        return res
