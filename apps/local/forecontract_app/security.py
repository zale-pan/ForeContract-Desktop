"""Local access, path/reparse, request boundary, and header security controls."""

from __future__ import annotations

import hmac
import os
import stat
from pathlib import Path, PureWindowsPath
from typing import Final

from forecontract_app.models import MAX_REQUEST_BODY_BYTES, AppError, HostConfig

_FILE_ATTRIBUTE_REPARSE_POINT: Final[int] = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)
_ALLOWED_FETCH_SITES: Final[frozenset[str]] = frozenset({"same-origin", "none"})
_WRITE_METHODS: Final[frozenset[str]] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

CSP_HEADER_VALUE: Final[str] = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "connect-src 'self'; "
    "img-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'none'"
)

SECURITY_RESPONSE_HEADERS: Final[dict[str, str]] = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": CSP_HEADER_VALUE,
}


def is_reparse_or_symlink(path: Path) -> bool:
    """Return True if ``path`` exists and is a symlink, junction, or reparse point."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True
    if stat.S_ISLNK(st.st_mode):
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    if attrs & _FILE_ATTRIBUTE_REPARSE_POINT:
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction):
        try:
            if is_junction():
                return True
        except OSError:
            return True
    return False


def _validate_raw_local_dir_path(raw: object, *, label: str) -> Path:
    if not isinstance(raw, Path):
        if isinstance(raw, str):
            raw_str = raw
        else:
            raise AppError("INVALID_HOST_ROOT", f"{label} must be a local path", status_code=400)
    else:
        raw_str = str(raw)
    if not raw_str or not raw_str.strip() or "\x00" in raw_str:
        raise AppError("INVALID_HOST_ROOT", f"{label} must be non-blank", status_code=400)
    if any(ord(ch) < 32 for ch in raw_str):
        raise AppError("INVALID_HOST_ROOT", f"{label} contains control characters", status_code=400)
    if raw_str.startswith(("\\\\", "//")):
        raise AppError("INVALID_HOST_ROOT", f"{label} must not be a UNC path", status_code=400)
    norm_slashes = raw_str.replace("\\", "/")
    raw_segments = [seg for seg in norm_slashes.split("/") if seg]
    if "." in raw_segments or ".." in raw_segments:
        raise AppError(
            "INVALID_HOST_ROOT", f"{label} must not contain '.' or '..' segments", status_code=400
        )
    win = PureWindowsPath(raw_str)
    if not win.is_absolute():
        raise AppError("INVALID_HOST_ROOT", f"{label} must be an absolute path", status_code=400)
    if any(part in (".", "..") for part in win.parts):
        raise AppError(
            "INVALID_HOST_ROOT", f"{label} must not contain traversal parts", status_code=400
        )
    path = Path(raw_str)
    check_no_reparse_ancestors(path, label=label)
    if path.exists() and not path.is_dir():
        raise AppError("INVALID_HOST_ROOT", f"{label} must be a directory", status_code=400)
    return path


def check_no_reparse_ancestors(path: Path, *, label: str) -> None:
    """Reject if ``path`` or any existing ancestor is a symlink, junction, or reparse point."""
    cur = path
    seen: set[Path] = set()
    while cur not in seen:
        seen.add(cur)
        if os.path.lexists(cur) and is_reparse_or_symlink(cur):
            raise AppError(
                "REPARSE_ROOT_FORBIDDEN",
                f"{label} and all ancestors must not be symlinks or reparse points",
                status_code=400,
            )
        parent = cur.parent
        if parent == cur:
            break
        cur = parent


def ensure_clean_directory(path: Path, *, label: str) -> Path:
    """Validate ancestor chain and create directory segments safely without resolving escapes."""
    _validate_raw_local_dir_path(path, label=label)
    parts = path.parts
    cur = Path(parts[0])
    for part in parts[1:]:
        cur = cur / part
        if os.path.lexists(cur):
            if is_reparse_or_symlink(cur) or not cur.is_dir():
                raise AppError(
                    "REPARSE_ROOT_FORBIDDEN",
                    f"{label} contains an unsafe path component",
                    status_code=400,
                )
        else:
            cur.mkdir(exist_ok=False)
    return path


def paths_overlap(a: Path, b: Path) -> bool:
    """Return True if resolved ``a`` and ``b`` are equal or one contains the other."""
    ra = Path(os.path.realpath(a))
    rb = Path(os.path.realpath(b))
    return ra == rb or ra in rb.parents or rb in ra.parents


def validate_host_config(config: HostConfig) -> None:
    """Strictly validate HostConfig roots, non-overlap, python executable, port, and token."""
    if not isinstance(config, HostConfig):
        raise AppError("INVALID_HOST_CONFIG", "HostConfig instance required", status_code=400)
    if (
        isinstance(config.bind_port, bool)
        or not isinstance(config.bind_port, int)
        or not (1 <= config.bind_port <= 65535)
    ):
        raise AppError("INVALID_HOST_CONFIG", "bind_port must be in 1..65535", status_code=400)
    if not isinstance(config.access_token, str) or len(config.access_token) < 16:
        raise AppError("INVALID_HOST_CONFIG", "access_token must be non-empty", status_code=400)
    for name, val in (
        ("max_sessions", config.max_sessions),
        ("max_runs_per_session", config.max_runs_per_session),
        ("max_events_per_run", config.max_events_per_run),
    ):
        if isinstance(val, bool) or not isinstance(val, int) or val < 1:
            raise AppError("INVALID_HOST_CONFIG", f"{name} must be positive", status_code=400)

    data_root = _validate_raw_local_dir_path(config.data_root, label="data_root")
    workspace_parent = _validate_raw_local_dir_path(
        config.workspace_parent, label="workspace_parent"
    )
    ensure_clean_directory(data_root, label="data_root")
    ensure_clean_directory(workspace_parent, label="workspace_parent")
    if paths_overlap(data_root, workspace_parent):
        raise AppError(
            "OVERLAPPING_ROOTS",
            "data_root and workspace_parent must be distinct and non-overlapping",
            status_code=400,
        )

    py_raw = config.python_executable
    if not isinstance(py_raw, Path):
        raise AppError("INVALID_PYTHON", "python_executable must be a Path", status_code=400)
    py_win = PureWindowsPath(str(py_raw))
    if not py_win.is_absolute() or any(p in (".", "..") for p in py_win.parts):
        raise AppError(
            "INVALID_PYTHON", "python_executable must be a clean absolute path", status_code=400
        )
    check_no_reparse_ancestors(py_raw, label="python_executable")
    if not py_raw.is_file():
        raise AppError("INVALID_PYTHON", "python_executable does not exist", status_code=400)


class DataRootFileLock:
    """Exclusive Windows byte-range lock on ``<data_root>/.host.lock`` held until closed."""

    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path
        self._fd: int | None = None

    def acquire(self) -> None:
        if os.path.lexists(self._lock_path) and is_reparse_or_symlink(self._lock_path):
            raise AppError("LOCK_CONFLICT", "lock path must not be a reparse point", status_code=409)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(str(self._lock_path), flags, 0o600)
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            size = os.fstat(fd).st_size
            if size == 0:
                os.write(fd, b"1")
                os.lseek(fd, 0, os.SEEK_SET)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            self._fd = fd
        except OSError as exc:
            os.close(fd)
            raise AppError(
                "DATA_ROOT_LOCKED",
                "another local host instance already holds this data_root lock",
                status_code=409,
            ) from exc

    def release(self) -> None:
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            try:
                os.close(fd)
            except OSError:
                pass


def validate_host_header(host_header: str | None, bind_port: int) -> None:
    """Require exact ``127.0.0.1:<port>`` Host header."""
    expected = f"127.0.0.1:{bind_port}"
    if not host_header or host_header != expected:
        raise AppError("INVALID_HOST", "host header is not permitted", status_code=403)


def validate_origin_and_fetch_metadata(
    *,
    method: str,
    origin: str | None,
    sec_fetch_site: str | None,
    bind_port: int,
) -> None:
    """Enforce strict same-origin Origin and Fetch Metadata policies."""
    expected_origin = f"http://127.0.0.1:{bind_port}"
    if sec_fetch_site is not None and sec_fetch_site not in _ALLOWED_FETCH_SITES:
        raise AppError("CROSS_SITE_FORBIDDEN", "cross-site request forbidden", status_code=403)
    if origin is not None and origin != expected_origin:
        raise AppError("INVALID_ORIGIN", "origin header is not permitted", status_code=403)
    if method.upper() in _WRITE_METHODS and origin != expected_origin:
        raise AppError("INVALID_ORIGIN", "write request requires local origin", status_code=403)


def validate_bearer_token(
    *,
    authorization: str | None,
    expected_token: str,
    query_keys: set[str],
) -> None:
    """Validate Authorization: Bearer <token> and forbid token query parameters."""
    if "token" in query_keys or "access_token" in query_keys:
        raise AppError("TOKEN_IN_QUERY_FORBIDDEN", "token in query is forbidden", status_code=401)
    if not authorization or not authorization.startswith("Bearer "):
        raise AppError("UNAUTHORIZED", "missing or invalid bearer token", status_code=401)
    supplied = authorization[len("Bearer ") :]
    if not supplied or not hmac.compare_digest(supplied, expected_token):
        raise AppError("UNAUTHORIZED", "missing or invalid bearer token", status_code=401)


def validate_content_length_header(content_length: str | None) -> None:
    """Reject missing-int or oversized Content-Length before reading body."""
    if content_length is None:
        return
    try:
        parsed = int(content_length)
    except ValueError as exc:
        raise AppError("INVALID_INPUT", "invalid content-length header", status_code=400) from exc
    if parsed < 0:
        raise AppError("INVALID_INPUT", "negative content-length header", status_code=400)
    if parsed > MAX_REQUEST_BODY_BYTES:
        raise AppError("PAYLOAD_TOO_LARGE", "request body exceeds 64 KiB limit", status_code=413)
