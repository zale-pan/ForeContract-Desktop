"""Bounded workspace discovery, UTF-8/CRLF-safe chunked reads, ancestor instructions, and diffs."""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
from collections.abc import Iterable
from pathlib import Path, PureWindowsPath
from typing import Any

from forecontract_coding.errors import (
    CODING_FILE_CONFLICT,
    CODING_SCOPE_VIOLATION,
    CodingError,
)
from forecontract_coding.models import (
    canonicalize_relative_path,
    is_valid_sha256_hex,
    utf8_byte_length,
)
from forecontract_coding.workspace import commit_file_atomic

from forecontract_app.project_models import (
    DEFAULT_EXCLUDED_DIR_NAMES,
    PROJECT_MAX_INSTRUCTIONS_BYTES,
    PROJECT_MAX_LIST_LIMIT,
    PROJECT_MAX_READ_CHUNK_BYTES,
    PROJECT_MAX_READ_FILE_BYTES,
    PROJECT_MAX_SCAN_FILES,
    PROJECT_MAX_SCAN_TOTAL_BYTES,
    PROJECT_MAX_SEARCH_BATCH_BYTES,
    PROJECT_MAX_SEARCH_BATCH_FILES,
    PROJECT_MAX_SEARCH_MATCHES,
    PROJECT_MAX_SEARCH_TEXT_BYTES,
    PROJECT_MAX_TOTAL_INSTRUCTIONS_BYTES,
    PROJECT_MAX_WRITE_FILE_BYTES,
    DiffEntry,
    DirectoryEntry,
    FindPathsResult,
    InstructionFileInfo,
    ListDirectoryResult,
    ProjectError,
    ProjectInstructions,
    ProjectWriteOutcome,
    ReadFileResult,
    SearchMatchEntry,
    SearchTextResult,
    WorkspaceDiff,
    WorkspaceRevision,
)
from forecontract_app.security import check_no_reparse_ancestors, is_reparse_or_symlink

_EXCLUDED_DIR_LOWER = frozenset(name.lower() for name in DEFAULT_EXCLUDED_DIR_NAMES)
_WRITE_PROTECTED_NAMES_LOWER = frozenset(
    {
        "agents.md",
        "claude.md",
        ".cursorrules",
        ".gitignore",
        "plan.md",
    }
)
_SECRET_DIRS_LOWER = frozenset({"credentials", "secrets"})
_EXCLUDED_NOTE = (
    "Excluded dirs (.git/.hg/.svn/.venv/venv/node_modules/__pycache__/.pytest_cache/"
    ".ruff_cache/dist/build/credentials/secrets), secret files (.env/.env.*/.pem/.key/"
    "credentials.*/secrets.*), and outside-project side effects are unaudited."
)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _b64_encode_cursor(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode(
        "utf-8"
    )
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _b64_decode_cursor(token: str) -> dict[str, Any]:
    if not isinstance(token, str) or not token.strip() or len(token) > 4096:
        raise ProjectError("INVALID_CURSOR", "malformed continuation cursor", status_code=422)
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        loaded = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ProjectError("INVALID_CURSOR", "malformed continuation cursor", status_code=422) from exc
    if not isinstance(loaded, dict):
        raise ProjectError("INVALID_CURSOR", "malformed continuation cursor", status_code=422)
    return loaded


def _is_secret_or_excluded_filename(name: str) -> bool:
    low = name.lower()
    if low == ".env" or low.startswith(".env."):
        return True
    if low.endswith((".pem", ".key")):
        return True
    return low in _SECRET_DIRS_LOWER or low.startswith(("secrets.", "credentials."))


def _is_excluded_rel_parts(parts: tuple[str, ...], *, is_dir: bool) -> bool:
    for idx, part in enumerate(parts):
        low = part.lower()
        if (idx < len(parts) - 1 or is_dir) and low in _EXCLUDED_DIR_LOWER:
            return True
        if idx == len(parts) - 1 and not is_dir and _is_secret_or_excluded_filename(part):
            return True
    return False


def _is_write_protected_rel(rel_path: str, extra_protected: frozenset[str] = frozenset()) -> bool:
    parts = tuple(rel_path.split("/"))
    if _is_excluded_rel_parts(parts, is_dir=False):
        return True
    leaf_low = parts[-1].lower()
    if leaf_low in _WRITE_PROTECTED_NAMES_LOWER:
        return True
    if leaf_low.startswith("plan_") and leaf_low.endswith(".json"):
        return True
    if any(p.lower() == ".git" for p in parts[:-1]):
        return True
    return os.path.normcase(rel_path) in extra_protected


def _match_glob_pattern(rel_path: str, pattern: str) -> bool:
    """Match workspace-relative POSIX glob without regex or arbitrary code."""
    import fnmatch

    norm_pat = pattern.replace("\\", "/").lstrip("/")
    if not norm_pat or norm_pat == "*":
        return True
    if "/" not in norm_pat:
        return fnmatch.fnmatchcase(rel_path.rsplit("/", 1)[-1], norm_pat) or fnmatch.fnmatchcase(
            rel_path, norm_pat
        )
    return fnmatch.fnmatchcase(rel_path, norm_pat)


def normalize_relative_dir(raw: object) -> str:
    """Validate and canonicalize a workspace-relative directory path (``'.'`` for root)."""
    if not isinstance(raw, str):
        raise ProjectError("INVALID_PATH", "directory path must be a string", status_code=400)
    stripped = raw.strip()
    if not stripped or stripped == ".":
        return "."
    if "\x00" in raw or any(ord(ch) < 32 for ch in raw):
        raise ProjectError("INVALID_PATH", "directory path contains control bytes", status_code=400)
    if raw.startswith(("/", "\\")) or ":" in raw:
        raise ProjectError("INVALID_PATH", "absolute or rooted paths are forbidden", status_code=400)
    win = PureWindowsPath(raw)
    if win.drive or win.root or win.is_absolute():
        raise ProjectError("INVALID_PATH", "absolute path is forbidden", status_code=400)
    norm = raw.replace("\\", "/").rstrip("/")
    if not norm:
        return "."
    if norm == ".":
        return "."
    return canonicalize_relative_path(norm)


def normalize_relative_file(raw: object) -> str:
    """Validate and canonicalize a workspace-relative file path."""
    try:
        return canonicalize_relative_path(raw)
    except (TypeError, ValueError) as exc:
        raise ProjectError("INVALID_PATH", str(exc), status_code=400) from exc


class ProjectWorkspace:
    """Guarded synchronous workspace explorer, instruction reader, writer, and lazy differ."""

    def __init__(self, *, root: Path, data_root: Path) -> None:
        if not isinstance(root, Path) or not isinstance(data_root, Path):
            raise ProjectError("INVALID_ROOT", "root and data_root must be Path objects", status_code=400)
        if not root.is_absolute() or not root.exists() or not root.is_dir():
            raise ProjectError("INVALID_ROOT", "project root must be an existing directory", status_code=400)
        check_no_reparse_ancestors(root, label="project_root")
        self.root = Path(os.path.realpath(root))
        self.data_root = Path(os.path.realpath(data_root))
        self.root_identity = os.path.normcase(str(self.root))
        self._baseline: WorkspaceRevision | None = None
        self._app_written: dict[str, str] = {}
        self._before_texts: dict[str, str] = {}
        self._extra_protected: set[str] = set()

    def set_extra_protected_paths(self, paths: Iterable[str]) -> None:
        normalized: set[str] = set()
        for raw in paths:
            norm = normalize_relative_file(raw)
            normalized.add(os.path.normcase(norm))
        self._extra_protected = normalized

    def _protected_set(self) -> frozenset[str]:
        return frozenset(self._extra_protected)

    def _resolve_clean_dir(self, rel_dir: str) -> Path:
        norm = normalize_relative_dir(rel_dir)
        if norm == ".":
            check_no_reparse_ancestors(self.root, label="project_root")
            return self.root
        parts = tuple(norm.split("/"))
        if _is_excluded_rel_parts(parts, is_dir=True):
            raise ProjectError("PROTECTED_PATH_FORBIDDEN", "access to excluded directory is forbidden", status_code=403)
        cur = self.root
        for part in parts:
            cur = cur / part
            if os.path.lexists(cur) and is_reparse_or_symlink(cur):
                raise ProjectError("REPARSE_PATH_FORBIDDEN", "reparse/symlink path is forbidden", status_code=403)
            if not cur.exists() or not cur.is_dir():
                raise ProjectError("FILE_NOT_FOUND", f"directory '{norm}' not found", status_code=404)
        return cur

    def _resolve_existing_file(self, raw_path: object, *, allow_write_protected_read: bool = True) -> tuple[str, Path]:
        norm = normalize_relative_file(raw_path)
        parts = tuple(norm.split("/"))
        if _is_excluded_rel_parts(parts, is_dir=False):
            raise ProjectError("PROTECTED_PATH_FORBIDDEN", f"access to protected path '{norm}' is forbidden", status_code=403)
        if not allow_write_protected_read and _is_write_protected_rel(norm, self._protected_set()):
            raise ProjectError("PROTECTED_PATH_FORBIDDEN", f"modifying protected path '{norm}' is forbidden", status_code=403)
        cur = self.root
        for idx, part in enumerate(parts):
            cur = cur / part
            if os.path.lexists(cur) and is_reparse_or_symlink(cur):
                raise ProjectError("REPARSE_PATH_FORBIDDEN", "reparse/symlink path is forbidden", status_code=403)
            if idx < len(parts) - 1 and not cur.is_dir():
                raise ProjectError("FILE_NOT_FOUND", f"path '{norm}' does not exist", status_code=404)
        if not cur.exists() or not cur.is_file():
            raise ProjectError("FILE_NOT_FOUND", f"file '{norm}' does not exist", status_code=404)
        if Path(os.path.realpath(cur)) != self.root / Path(*parts):
            raise ProjectError("PATH_ESCAPE_FORBIDDEN", "resolved file escapes workspace root", status_code=403)
        return norm, cur

    def list_directory(
        self,
        path: str = ".",
        cursor: str | None = None,
        limit: int = PROJECT_MAX_LIST_LIMIT,
    ) -> ListDirectoryResult:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > PROJECT_MAX_LIST_LIMIT:
            raise ProjectError(
                "INVALID_INPUT",
                f"limit must be an integer in 1..{PROJECT_MAX_LIST_LIMIT}",
                status_code=422,
            )
        rel_dir = normalize_relative_dir(path)
        target_dir = self._resolve_clean_dir(rel_dir)

        visible: list[DirectoryEntry] = []
        raw_items: list[tuple[str, bool, int]] = []
        scanned = 0
        excluded = 0

        with os.scandir(target_dir) as it:
            for entry in it:
                scanned += 1
                if scanned > PROJECT_MAX_SCAN_FILES:
                    raise ProjectError(
                        "SCAN_LIMIT",
                        f"directory enumeration exceeded {PROJECT_MAX_SCAN_FILES} entries",
                        status_code=413,
                    )
                entry_path = Path(entry.path)
                if is_reparse_or_symlink(entry_path):
                    excluded += 1
                    continue
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    is_file = entry.is_file(follow_symlinks=False)
                except OSError:
                    excluded += 1
                    continue
                if not is_dir and not is_file:
                    excluded += 1
                    continue
                rel_child = entry.name if rel_dir == "." else f"{rel_dir}/{entry.name}"
                parts = tuple(rel_child.split("/"))
                if _is_excluded_rel_parts(parts, is_dir=is_dir):
                    excluded += 1
                    continue
                size_b = 0
                if is_file:
                    try:
                        size_b = entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        excluded += 1
                        continue
                raw_items.append((entry.name, is_dir, size_b))

        raw_items.sort(key=lambda item: (item[0].casefold(), item[0]))
        for name, is_dir, size_b in raw_items:
            rel_child = name if rel_dir == "." else f"{rel_dir}/{name}"
            visible.append(
                DirectoryEntry(
                    name=name,
                    path=rel_child,
                    kind="dir" if is_dir else "file",
                    size_bytes=size_b,
                )
            )

        snapshot_sha = sha256_bytes(
            json.dumps(
                [[e.name, e.kind, e.size_bytes] for e in visible],
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        )

        offset = 0
        if cursor is not None:
            cur = _b64_decode_cursor(cursor)
            if cur.get("op") != "list_directory" or cur.get("dir") != rel_dir:
                raise ProjectError("INVALID_CURSOR", "cursor does not match directory request", status_code=409)
            if cur.get("snapshot") != snapshot_sha:
                raise ProjectError(
                    "INVALID_CURSOR",
                    "directory changed since cursor was issued",
                    status_code=409,
                )
            raw_off = cur.get("offset")
            if isinstance(raw_off, bool) or not isinstance(raw_off, int) or raw_off < 0 or raw_off > len(visible):
                raise ProjectError("INVALID_CURSOR", "cursor offset out of range", status_code=409)
            offset = raw_off

        page = visible[offset : offset + limit]
        next_off = offset + len(page)
        truncated = next_off < len(visible)
        next_cursor = (
            _b64_encode_cursor(
                {
                    "op": "list_directory",
                    "dir": rel_dir,
                    "offset": next_off,
                    "snapshot": snapshot_sha,
                }
            )
            if truncated
            else None
        )
        return ListDirectoryResult(
            directory=rel_dir,
            entries=page,
            truncated=truncated,
            next_cursor=next_cursor,
            scanned=scanned,
            excluded=excluded,
        )

    def _iter_eligible_files_ordered(self) -> Iterable[tuple[str, Path, int]]:
        """Yield ``(rel_path, abs_path, size_bytes)`` deterministically skipping symlinks/exclusions."""
        dir_stack: list[tuple[str, Path]] = [(".", self.root)]
        while dir_stack:
            rel_dir, abs_dir = dir_stack.pop()
            if is_reparse_or_symlink(abs_dir):
                continue
            children: list[tuple[str, bool, Path, int]] = []
            try:
                with os.scandir(abs_dir) as it:
                    for entry in it:
                        p = Path(entry.path)
                        if is_reparse_or_symlink(p):
                            continue
                        try:
                            is_d = entry.is_dir(follow_symlinks=False)
                            is_f = entry.is_file(follow_symlinks=False)
                        except OSError:
                            continue
                        if not is_d and not is_f:
                            continue
                        size_b = 0
                        if is_f:
                            try:
                                size_b = entry.stat(follow_symlinks=False).st_size
                            except OSError:
                                continue
                        children.append((entry.name, is_d, p, size_b))
            except OSError:
                continue

            children.sort(key=lambda c: (c[0].casefold(), c[0]))
            subdirs: list[tuple[str, Path]] = []
            for name, is_d, child_path, size_b in children:
                rel_child = name if rel_dir == "." else f"{rel_dir}/{name}"
                parts = tuple(rel_child.split("/"))
                if _is_excluded_rel_parts(parts, is_dir=is_d):
                    yield (f"__EXCLUDED__/{rel_child}", child_path, -1)
                    continue
                if is_d:
                    subdirs.append((rel_child, child_path))
                else:
                    yield (rel_child, child_path, size_b)
            dir_stack.extend(reversed(subdirs))

    def find_paths(
        self,
        pattern: str = "*",
        cursor: str | None = None,
        limit: int = PROJECT_MAX_LIST_LIMIT,
    ) -> FindPathsResult:
        if not isinstance(pattern, str) or not pattern.strip() or "\x00" in pattern:
            raise ProjectError("INVALID_INPUT", "pattern must be non-blank", status_code=422)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > PROJECT_MAX_LIST_LIMIT:
            raise ProjectError("INVALID_INPUT", f"limit must be in 1..{PROJECT_MAX_LIST_LIMIT}", status_code=422)

        resume_after: str | None = None
        if cursor is not None:
            cur = _b64_decode_cursor(cursor)
            if cur.get("op") != "find_paths" or cur.get("pattern") != pattern:
                raise ProjectError("INVALID_CURSOR", "cursor does not match find_paths query", status_code=409)
            ra = cur.get("last_seen")
            if not isinstance(ra, str) or not ra:
                raise ProjectError("INVALID_CURSOR", "malformed find_paths cursor", status_code=409)
            resume_after = ra

        paths_out: list[str] = []
        scanned = 0
        excluded = 0
        last_seen: str | None = None
        truncated = False
        passing_resume = resume_after is None

        gen = iter(self._iter_eligible_files_ordered())
        for rel_path, _, size_b in gen:
            if size_b < 0:
                if passing_resume:
                    excluded += 1
                continue
            if not passing_resume:
                if rel_path == resume_after:
                    passing_resume = True
                continue
            scanned += 1
            if scanned > PROJECT_MAX_SCAN_FILES:
                raise ProjectError("SCAN_LIMIT", "find_paths scan exceeded 10,000 entries", status_code=413)
            last_seen = rel_path
            if _match_glob_pattern(rel_path, pattern):
                paths_out.append(rel_path)
            if len(paths_out) >= limit or scanned >= PROJECT_MAX_SEARCH_BATCH_FILES:
                # Check if at least one more eligible entry exists.
                for next_rel, _, next_size in gen:
                    if next_size >= 0:
                        truncated = True
                        break
                    excluded += 1
                break

        next_cursor = (
            _b64_encode_cursor(
                {"op": "find_paths", "pattern": pattern, "last_seen": last_seen}
            )
            if truncated and last_seen is not None
            else None
        )
        return FindPathsResult(
            pattern=pattern,
            paths=paths_out,
            scanned=scanned,
            excluded=excluded,
            truncated=truncated,
            next_cursor=next_cursor,
        )

    def read_file(
        self,
        path: str,
        start_line: int = 1,
        max_lines: int = 200,
        byte_offset: int = 0,
        expected_sha256: str | None = None,
    ) -> ReadFileResult:
        if isinstance(start_line, bool) or not isinstance(start_line, int) or start_line < 1:
            raise ProjectError("INVALID_INPUT", "start_line must be >= 1", status_code=422)
        if isinstance(max_lines, bool) or not isinstance(max_lines, int) or not (1 <= max_lines <= 200):
            raise ProjectError("INVALID_INPUT", "max_lines must be in 1..200", status_code=422)
        if isinstance(byte_offset, bool) or not isinstance(byte_offset, int) or byte_offset < 0:
            raise ProjectError("INVALID_INPUT", "byte_offset must be >= 0", status_code=422)
        if expected_sha256 is not None and not is_valid_sha256_hex(expected_sha256):
            raise ProjectError("INVALID_INPUT", "expected_sha256 must be 64-hex lowercase", status_code=422)

        norm, target = self._resolve_existing_file(path, allow_write_protected_read=True)
        try:
            st = target.stat(follow_symlinks=False)
        except OSError as exc:
            raise ProjectError("FILE_NOT_FOUND", f"cannot stat '{norm}'", status_code=404) from exc
        if st.st_size > PROJECT_MAX_READ_FILE_BYTES:
            raise ProjectError(
                "READ_LIMIT",
                f"file size {st.st_size} exceeds 8 MiB limit",
                status_code=413,
            )

        raw = target.read_bytes()
        if len(raw) > PROJECT_MAX_READ_FILE_BYTES:
            raise ProjectError("READ_LIMIT", "file exceeds 8 MiB limit", status_code=413)
        file_sha = sha256_bytes(raw)
        if expected_sha256 is not None and file_sha != expected_sha256:
            raise ProjectError(
                "FILE_CONFLICT",
                f"file '{norm}' changed concurrently (expected {expected_sha256[:12]}, actual {file_sha[:12]})",
                status_code=409,
            )

        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProjectError("BINARY_OR_NON_UTF8", f"file '{norm}' is not valid UTF-8", status_code=415) from exc

        total_bytes = len(raw)
        if total_bytes == 0:
            if byte_offset > 0 or start_line > 1:
                raise ProjectError("INVALID_OFFSET", "offset beyond empty file", status_code=400)
            return ReadFileResult(
                path=norm,
                sha256=file_sha,
                content="",
                start_line=1,
                next_start_line=None,
                byte_offset=0,
                next_byte_offset=None,
                line_count=0,
                total_bytes=0,
                truncated=False,
                eof=True,
            )

        # Build line start byte offsets (each line ends at b"\n" if present).
        line_starts: list[int] = [0]
        pos = 0
        while True:
            nl = raw.find(b"\n", pos)
            if nl < 0:
                break
            nxt = nl + 1
            if nxt < total_bytes:
                line_starts.append(nxt)
            pos = nxt

        if byte_offset > 0:
            if byte_offset > total_bytes:
                raise ProjectError("INVALID_OFFSET", "byte_offset past EOF", status_code=400)
            if byte_offset < total_bytes and (raw[byte_offset] & 0xC0) == 0x80:
                raise ProjectError("INVALID_OFFSET", "byte_offset lands mid-UTF-8 sequence", status_code=400)
            start_pos = byte_offset
            cur_line = raw[:start_pos].count(b"\n") + 1
        else:
            if start_line > len(line_starts):
                return ReadFileResult(
                    path=norm,
                    sha256=file_sha,
                    content="",
                    start_line=start_line,
                    next_start_line=None,
                    byte_offset=total_bytes,
                    next_byte_offset=None,
                    line_count=0,
                    total_bytes=total_bytes,
                    truncated=False,
                    eof=True,
                )
            cur_line = start_line
            start_pos = line_starts[start_line - 1]

        if start_pos >= total_bytes:
            return ReadFileResult(
                path=norm,
                sha256=file_sha,
                content="",
                start_line=cur_line,
                next_start_line=None,
                byte_offset=start_pos,
                next_byte_offset=None,
                line_count=0,
                total_bytes=total_bytes,
                truncated=False,
                eof=True,
            )

        end_pos = start_pos
        lines_emitted = 0
        while lines_emitted < max_lines and end_pos < total_bytes:
            nl = raw.find(b"\n", end_pos)
            line_end = total_bytes if nl < 0 else nl + 1
            if line_end - start_pos <= PROJECT_MAX_READ_CHUNK_BYTES:
                end_pos = line_end
                lines_emitted += 1
            elif lines_emitted > 0:
                break
            else:
                # Single line longer than 16 KiB: slice cleanly at UTF-8 codepoint & never split CRLF.
                hard_end = min(total_bytes, start_pos + PROJECT_MAX_READ_CHUNK_BYTES)
                while hard_end > start_pos and hard_end < total_bytes and (raw[hard_end] & 0xC0) == 0x80:
                    hard_end -= 1
                if (
                    hard_end > start_pos
                    and hard_end < total_bytes
                    and raw[hard_end - 1 : hard_end + 1] == b"\r\n"
                    and hard_end - 1 > start_pos
                ):
                    hard_end -= 1
                if hard_end == start_pos:
                    # Ensure at least one character is emitted.
                    hard_end = min(total_bytes, start_pos + 4)
                    while hard_end < total_bytes and (raw[hard_end] & 0xC0) == 0x80:
                        hard_end += 1
                end_pos = hard_end
                break

        chunk_bytes = raw[start_pos:end_pos]
        text = chunk_bytes.decode("utf-8")
        eof = end_pos >= total_bytes
        truncated = not eof
        next_byte = None if eof else end_pos
        next_line = None if eof else (cur_line + lines_emitted)
        return ReadFileResult(
            path=norm,
            sha256=file_sha,
            content=text,
            start_line=cur_line,
            next_start_line=next_line,
            byte_offset=start_pos,
            next_byte_offset=next_byte,
            line_count=lines_emitted if lines_emitted > 0 else (1 if text else 0),
            total_bytes=total_bytes,
            truncated=truncated,
            eof=eof,
        )

    def search_text(
        self,
        query: str,
        paths: list[str] | None = None,
        cursor: str | None = None,
        max_matches: int = PROJECT_MAX_SEARCH_MATCHES,
    ) -> SearchTextResult:
        if not isinstance(query, str) or not query or "\x00" in query:
            raise ProjectError("INVALID_INPUT", "search query must be non-empty", status_code=422)
        if isinstance(max_matches, bool) or not isinstance(max_matches, int) or not (
            1 <= max_matches <= PROJECT_MAX_SEARCH_MATCHES
        ):
            raise ProjectError(
                "INVALID_INPUT",
                f"max_matches must be in 1..{PROJECT_MAX_SEARCH_MATCHES}",
                status_code=422,
            )

        norm_filter: list[str] | None = None
        if paths is not None:
            if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
                raise ProjectError("INVALID_INPUT", "paths must be a list of strings", status_code=422)
            validated_filters: list[str] = []
            for raw_p in paths:
                if not raw_p.strip():
                    continue
                if "\x00" in raw_p or any(ord(ch) < 32 for ch in raw_p):
                    raise ProjectError("INVALID_PATH", "search path contains control bytes", status_code=400)
                if raw_p.startswith(("/", "\\")) or ":" in raw_p:
                    raise ProjectError("INVALID_PATH", "absolute search path is forbidden", status_code=400)
                win = PureWindowsPath(raw_p)
                if win.drive or win.root or win.is_absolute():
                    raise ProjectError("INVALID_PATH", "absolute search path is forbidden", status_code=400)
                cleaned = raw_p.replace("\\", "/").strip("/")
                segs = [s for s in cleaned.split("/") if s]
                if not segs or any(s in (".", "..") for s in segs):
                    raise ProjectError("INVALID_PATH", "traversal or dot segments are forbidden in search paths", status_code=400)
                validated_filters.append("/".join(segs))
            norm_filter = validated_filters

        resume_file: str | None = None
        resume_line = 1
        resume_offset = 0
        if cursor is not None:
            cur = _b64_decode_cursor(cursor)
            if cur.get("op") != "search_text" or cur.get("query") != query or cur.get("paths") != norm_filter:
                raise ProjectError("INVALID_CURSOR", "cursor does not match search_text query", status_code=409)
            rf = cur.get("file")
            rl = cur.get("line", 1)
            ro = cur.get("offset", 0)
            if (
                not isinstance(rf, str)
                or not rf
                or isinstance(rl, bool)
                or not isinstance(rl, int)
                or rl < 1
                or isinstance(ro, bool)
                or not isinstance(ro, int)
                or ro < 0
            ):
                raise ProjectError("INVALID_CURSOR", "malformed search_text cursor", status_code=409)
            resume_file = rf
            resume_line = rl
            resume_offset = ro

        def _utf8_safe_cut(chunk: bytes) -> int:
            n = len(chunk)
            for idx in range(n - 1, max(-1, n - 5), -1):
                b = chunk[idx]
                if (b & 0xC0) == 0x80:
                    continue
                if b < 0x80:
                    return n
                if b < 0xE0:
                    seq_len = 2
                elif b < 0xF0:
                    seq_len = 3
                elif b < 0xF8:
                    seq_len = 4
                else:
                    return n
                return idx if (n - idx) < seq_len else n
            return n

        def _utf8_char_start(chunk: bytes, pos: int) -> int:
            while 0 < pos < len(chunk) and (chunk[pos] & 0xC0) == 0x80:
                pos -= 1
            return pos

        matches: list[SearchMatchEntry] = []
        matches_text_bytes = 0
        scanned = 0
        bytes_scanned = 0
        excluded = 0
        truncated = False
        next_cursor_payload: dict[str, Any] | None = None
        passing_resume = resume_file is None
        query_byte_len = utf8_byte_length(query)

        gen = iter(self._iter_eligible_files_ordered())
        for rel_path, abs_path, size_b in gen:
            if size_b < 0:
                if passing_resume:
                    excluded += 1
                continue
            if norm_filter and not any(
                rel_path == f or rel_path.startswith(f"{f}/") or _match_glob_pattern(rel_path, f)
                for f in norm_filter
            ):
                continue

            start_line_no = 1
            file_offset = 0
            if not passing_resume:
                if rel_path == resume_file:
                    passing_resume = True
                    start_line_no = resume_line
                    file_offset = resume_offset
                else:
                    continue

            if size_b > PROJECT_MAX_READ_FILE_BYTES:
                excluded += 1
                continue

            remaining_budget = PROJECT_MAX_SEARCH_BATCH_BYTES - bytes_scanned
            if remaining_budget <= 0:
                truncated = True
                next_cursor_payload = {
                    "op": "search_text",
                    "query": query,
                    "paths": norm_filter,
                    "file": rel_path,
                    "line": start_line_no,
                    "offset": file_offset,
                }
                break

            try:
                buf = bytearray()
                with abs_path.open("rb") as fh:
                    fsize = os.fstat(fh.fileno()).st_size
                    if fsize > PROJECT_MAX_READ_FILE_BYTES:
                        excluded += 1
                        continue
                    if file_offset > 0:
                        fh.seek(file_offset)
                    while len(buf) < remaining_budget:
                        part = fh.read(min(262144, remaining_budget - len(buf)))
                        if not part:
                            break
                        buf.extend(part)
                raw_chunk = bytes(buf)
                hit_eof = (len(raw_chunk) < remaining_budget) or (file_offset + len(raw_chunk) >= fsize)
                bytes_scanned += len(raw_chunk)

                has_more_in_file = False
                next_file_offset = file_offset + len(raw_chunk)
                next_file_line = start_line_no
                split_on_newline = True

                if not hit_eof and raw_chunk:
                    last_nl = raw_chunk.rfind(b"\n")
                    if last_nl >= 0:
                        scan_slice = raw_chunk[: last_nl + 1]
                        next_file_offset = file_offset + last_nl + 1
                        has_more_in_file = True
                    else:
                        split_on_newline = False
                        cut = _utf8_safe_cut(raw_chunk)
                        if cut <= 0:
                            raise UnicodeDecodeError("utf-8", raw_chunk, 0, min(1, len(raw_chunk)), "invalid boundary")
                        scan_slice = raw_chunk[:cut]
                        overlap = max(0, query_byte_len - 1)
                        if overlap > 0 and cut > overlap + 4:
                            advance_end = _utf8_char_start(raw_chunk, cut - overlap)
                            if advance_end <= 0:
                                advance_end = cut
                        else:
                            advance_end = cut
                        next_file_offset = file_offset + advance_end
                        has_more_in_file = True
                else:
                    scan_slice = raw_chunk

                text = scan_slice.decode("utf-8")
            except (OSError, UnicodeDecodeError):
                excluded += 1
                continue

            scanned += 1
            cur_line = 1 if file_offset == 0 else start_line_no
            line_byte_cursor = 0
            hit_output_cap = False

            for raw_line in text.splitlines(keepends=True):
                line_bytes_len = len(raw_line.encode("utf-8"))
                line_str = raw_line.rstrip("\r\n")
                if cur_line >= start_line_no and query in line_str:
                    clipped = line_str[:512]
                    entry_bytes = utf8_byte_length(rel_path) + utf8_byte_length(clipped) + 32
                    if len(matches) >= max_matches or (
                        matches and matches_text_bytes + entry_bytes > PROJECT_MAX_SEARCH_TEXT_BYTES
                    ):
                        truncated = True
                        next_cursor_payload = {
                            "op": "search_text",
                            "query": query,
                            "paths": norm_filter,
                            "file": rel_path,
                            "line": cur_line,
                            "offset": file_offset + line_byte_cursor,
                        }
                        hit_output_cap = True
                        break
                    matches.append(
                        SearchMatchEntry(path=rel_path, line_number=cur_line, line_text=clipped)
                    )
                    matches_text_bytes += entry_bytes
                line_byte_cursor += line_bytes_len
                if raw_line.endswith(("\n", "\r")):
                    cur_line += 1

            if hit_output_cap:
                break

            if has_more_in_file:
                truncated = True
                next_file_line = cur_line if split_on_newline else start_line_no
                next_cursor_payload = {
                    "op": "search_text",
                    "query": query,
                    "paths": norm_filter,
                    "file": rel_path,
                    "line": next_file_line,
                    "offset": next_file_offset,
                }
                break

            if (
                scanned >= PROJECT_MAX_SEARCH_BATCH_FILES
                or bytes_scanned >= PROJECT_MAX_SEARCH_BATCH_BYTES
            ):
                # Check if any more matching-filter eligible files remain without reading contents.
                for next_rel, _, next_size in gen:
                    if next_size < 0:
                        excluded += 1
                        continue
                    if norm_filter and not any(
                        next_rel == f
                        or next_rel.startswith(f"{f}/")
                        or _match_glob_pattern(next_rel, f)
                        for f in norm_filter
                    ):
                        continue
                    truncated = True
                    next_cursor_payload = {
                        "op": "search_text",
                        "query": query,
                        "paths": norm_filter,
                        "file": next_rel,
                        "line": 1,
                        "offset": 0,
                    }
                    break
                break

        next_cursor = _b64_encode_cursor(next_cursor_payload) if truncated and next_cursor_payload else None
        return SearchTextResult(
            query=query,
            matches=matches,
            scanned=scanned,
            excluded=excluded,
            truncated=truncated,
            next_cursor=next_cursor,
        )

    def ancestor_dirs_for_targets(
        self,
        *,
        target_paths: Iterable[str] = (),
        target_dirs: Iterable[str] = (),
    ) -> tuple[str, ...]:
        """Return deterministically ordered ancestor dirs (root-first) for target files & dirs."""
        dirs_seen: set[str] = {"."}
        for raw_p in target_paths:
            norm = normalize_relative_file(raw_p)
            parts = norm.split("/")[:-1]
            cur: list[str] = []
            for seg in parts:
                cur.append(seg)
                dirs_seen.add("/".join(cur))
        for raw_d in target_dirs:
            norm_d = normalize_relative_dir(raw_d)
            if norm_d == ".":
                continue
            cur_d: list[str] = []
            for seg in norm_d.split("/"):
                cur_d.append(seg)
                dirs_seen.add("/".join(cur_d))
        return tuple(sorted(dirs_seen, key=lambda d: (0 if d == "." else len(d.split("/")), d)))

    def load_instructions_for_targets(
        self,
        *,
        target_paths: Iterable[str] = (),
        target_dirs: Iterable[str] = (),
    ) -> ProjectInstructions:
        """Load AGENTS.md along the exact union of ancestor chains for ``target_paths`` & ``target_dirs``."""
        rel_dirs = self.ancestor_dirs_for_targets(
            target_paths=target_paths, target_dirs=target_dirs
        )
        files_info: list[InstructionFileInfo] = []
        chunks: list[str] = []
        total_bytes = 0

        for rel_d in rel_dirs:
            candidate = self.root / "AGENTS.md" if rel_d == "." else self.root / rel_d / "AGENTS.md"
            rel_file = "AGENTS.md" if rel_d == "." else f"{rel_d}/AGENTS.md"
            if not os.path.lexists(candidate):
                continue
            if is_reparse_or_symlink(candidate) or not candidate.is_file():
                raise ProjectError(
                    "INSTRUCTIONS_INCOMPLETE",
                    f"instruction file '{rel_file}' is unreadable or a reparse point",
                    status_code=400,
                )
            try:
                raw = candidate.read_bytes()
                decoded = raw.decode("utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise ProjectError(
                    "INSTRUCTIONS_INCOMPLETE",
                    f"instruction file '{rel_file}' could not be read as UTF-8",
                    status_code=400,
                ) from exc
            if len(raw) > PROJECT_MAX_INSTRUCTIONS_BYTES:
                raise ProjectError(
                    "INSTRUCTIONS_INCOMPLETE",
                    f"instruction file '{rel_file}' exceeds {PROJECT_MAX_INSTRUCTIONS_BYTES} bytes",
                    status_code=413,
                )
            total_bytes += len(raw)
            if total_bytes > PROJECT_MAX_TOTAL_INSTRUCTIONS_BYTES:
                raise ProjectError(
                    "INSTRUCTIONS_INCOMPLETE",
                    f"total instruction chain exceeds {PROJECT_MAX_TOTAL_INSTRUCTIONS_BYTES} bytes",
                    status_code=413,
                )
            fsha = sha256_bytes(raw)
            files_info.append(
                InstructionFileInfo(
                    path=rel_file,
                    sha256=fsha,
                    scope=rel_d,
                    size_bytes=len(raw),
                )
            )
            chunks.append(f"# [{rel_file} sha256={fsha}]\n{decoded}")

        if not files_info:
            return ProjectInstructions(present=False, files=[], content="", sha256=None, total_bytes=0)

        chain_digest = sha256_bytes(
            json.dumps(
                [[f.path, f.sha256] for f in files_info], separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8")
        )
        return ProjectInstructions(
            present=True,
            files=files_info,
            content="\n\n".join(chunks),
            sha256=chain_digest,
            total_bytes=total_bytes,
        )

    def load_instructions(self, target_path: str | None = None) -> ProjectInstructions:
        """Load root AGENTS.md and optional ancestor-chain AGENTS.md (root -> target dir)."""
        return self.load_instructions_for_targets(
            target_paths=(target_path,) if target_path is not None else (),
        )

    def create_file(self, path: str, content: str) -> ProjectWriteOutcome:
        norm = normalize_relative_file(path)
        if _is_write_protected_rel(norm, self._protected_set()):
            raise ProjectError(
                "PROTECTED_PATH_FORBIDDEN",
                f"creating protected path '{norm}' is forbidden",
                status_code=403,
            )
        if not isinstance(content, str):
            raise ProjectError("INVALID_INPUT", "content must be a UTF-8 string", status_code=422)
        raw = content.encode("utf-8")
        if len(raw) > PROJECT_MAX_WRITE_FILE_BYTES:
            raise ProjectError(
                "WRITE_LIMIT",
                f"create_file content ({len(raw)} bytes) exceeds {PROJECT_MAX_WRITE_FILE_BYTES} bytes",
                status_code=413,
            )
        target = self.root / Path(*norm.split("/"))
        if os.path.lexists(target):
            raise ProjectError("FILE_CONFLICT", f"target '{norm}' already exists", status_code=409)
        if self._baseline is None:
            self.capture_baseline()
        try:
            outcome = commit_file_atomic(
                workspace_root=self.root,
                path=norm,
                operation="create",
                expected_sha256=None,
                content=raw,
                max_file_bytes=PROJECT_MAX_WRITE_FILE_BYTES,
            )
        except CodingError as exc:
            status = 409 if exc.code == CODING_FILE_CONFLICT else (
                413 if exc.code == CODING_SCOPE_VIOLATION else 400
            )
            code = "FILE_CONFLICT" if exc.code == CODING_FILE_CONFLICT else (
                "WRITE_LIMIT" if exc.code == CODING_SCOPE_VIOLATION else exc.code
            )
            raise ProjectError(code, exc.message, status_code=status) from exc
        self._app_written[norm] = outcome.read_back_sha256
        return ProjectWriteOutcome(
            path=norm,
            operation="create",
            before_sha256=None,
            written_sha256=outcome.written_sha256,
            read_back_sha256=outcome.read_back_sha256,
            size_bytes=len(raw),
        )

    def edit_file(
        self,
        *,
        path: str,
        expected_sha256: str,
        old_text: str,
        new_text: str,
    ) -> ProjectWriteOutcome:
        norm = normalize_relative_file(path)
        if _is_write_protected_rel(norm, self._protected_set()):
            raise ProjectError(
                "PROTECTED_PATH_FORBIDDEN",
                f"modifying protected path '{norm}' is forbidden",
                status_code=403,
            )
        if not is_valid_sha256_hex(expected_sha256):
            raise ProjectError("INVALID_INPUT", "expected_sha256 must be 64-hex", status_code=422)
        if not isinstance(old_text, str) or not old_text or not isinstance(new_text, str):
            raise ProjectError("INVALID_INPUT", "old_text must be non-empty string", status_code=422)

        _, target = self._resolve_existing_file(norm, allow_write_protected_read=False)
        st = target.stat(follow_symlinks=False)
        if st.st_size > PROJECT_MAX_WRITE_FILE_BYTES:
            raise ProjectError(
                "WRITE_LIMIT",
                f"existing file size {st.st_size} exceeds {PROJECT_MAX_WRITE_FILE_BYTES} bytes",
                status_code=413,
            )
        current_bytes = target.read_bytes()
        current_sha = sha256_bytes(current_bytes)
        if current_sha != expected_sha256:
            raise ProjectError(
                "FILE_CONFLICT",
                f"file '{norm}' SHA mismatch (expected {expected_sha256[:12]}, actual {current_sha[:12]})",
                status_code=409,
            )
        try:
            current_text = current_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ProjectError("BINARY_OR_NON_UTF8", f"file '{norm}' is not UTF-8", status_code=415) from exc

        count = current_text.count(old_text)
        if count == 0:
            raise ProjectError("EDIT_MATCH_NOT_FOUND", "old_text not found in file", status_code=409)
        if count > 1:
            raise ProjectError("EDIT_MATCH_AMBIGUOUS", "old_text matched multiple times", status_code=409)

        updated_text = current_text.replace(old_text, new_text, 1)
        updated_bytes = updated_text.encode("utf-8")
        if len(updated_bytes) > PROJECT_MAX_WRITE_FILE_BYTES:
            raise ProjectError(
                "WRITE_LIMIT",
                f"updated content ({len(updated_bytes)} bytes) exceeds {PROJECT_MAX_WRITE_FILE_BYTES} bytes",
                status_code=413,
            )

        if self._baseline is None:
            self.capture_baseline()
        self._before_texts.setdefault(norm, current_text)
        try:
            outcome = commit_file_atomic(
                workspace_root=self.root,
                path=norm,
                operation="modify",
                expected_sha256=expected_sha256,
                content=updated_bytes,
                max_file_bytes=PROJECT_MAX_WRITE_FILE_BYTES,
            )
        except CodingError as exc:
            status = 409 if exc.code == CODING_FILE_CONFLICT else (
                413 if exc.code == CODING_SCOPE_VIOLATION else 400
            )
            code = "FILE_CONFLICT" if exc.code == CODING_FILE_CONFLICT else (
                "WRITE_LIMIT" if exc.code == CODING_SCOPE_VIOLATION else exc.code
            )
            raise ProjectError(code, exc.message, status_code=status) from exc

        self._app_written[norm] = outcome.read_back_sha256
        return ProjectWriteOutcome(
            path=norm,
            operation="modify",
            before_sha256=current_sha,
            written_sha256=outcome.written_sha256,
            read_back_sha256=outcome.read_back_sha256,
            size_bytes=len(updated_bytes),
        )

    def hash_protected_paths(self, rel_paths: Iterable[str]) -> dict[str, str]:
        """Compute clean current SHA-256 digests for protected workspace-relative paths."""
        out: dict[str, str] = {}
        for raw in sorted(set(rel_paths)):
            norm, abs_p = self._resolve_existing_file(raw, allow_write_protected_read=True)
            raw_bytes = abs_p.read_bytes()
            out[norm] = sha256_bytes(raw_bytes)
        return out

    def capture_revision(self) -> WorkspaceRevision:
        """Compute bounded workspace inventory & SHA-256 digest (10,000 files / 64 MiB)."""
        inventory: dict[str, str] = {}
        file_count = 0
        total_bytes = 0
        scope_complete = True

        for rel_path, abs_path, size_b in self._iter_eligible_files_ordered():
            if size_b < 0:
                continue
            file_count += 1
            total_bytes += size_b
            if file_count > PROJECT_MAX_SCAN_FILES or total_bytes > PROJECT_MAX_SCAN_TOTAL_BYTES:
                scope_complete = False
                break
            try:
                raw = abs_path.read_bytes()
            except OSError:
                scope_complete = False
                break
            inventory[rel_path] = sha256_bytes(raw)

        sorted_pairs = sorted(inventory.items())
        digest = sha256_bytes(
            json.dumps(sorted_pairs, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        )
        prefix = "" if scope_complete else "incomplete:"
        return WorkspaceRevision(
            revision=f"{prefix}{digest}",
            file_count=file_count,
            total_bytes=total_bytes,
            scope_complete=scope_complete,
            excluded_note=_EXCLUDED_NOTE,
            inventory=inventory if scope_complete else {},
        )

    def capture_baseline(self) -> WorkspaceRevision:
        if self._baseline is None:
            self._baseline = self.capture_revision()
        return self._baseline

    def _bounded_excerpt(self, rel_path: str, before_sha: str | None, after_sha: str | None) -> str:
        if after_sha is None:
            return f"--- {rel_path} (deleted)\n+++ /dev/null"
        try:
            abs_p = self.root / Path(*rel_path.split("/"))
            if not abs_p.is_file() or abs_p.stat(follow_symlinks=False).st_size > PROJECT_MAX_WRITE_FILE_BYTES:
                return f"@@ {rel_path} ({'created' if before_sha is None else 'modified'}) @@"
            text = abs_p.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return f"@@ {rel_path} (binary/unreadable) @@"
        lines = text.splitlines()
        if before_sha is None:
            diff_lines = [f"+++ {rel_path}"] + [f"+{line}" for line in lines[:40]]
            return "\n".join(diff_lines)[:2048]
        before_text = self._before_texts.get(rel_path)
        if before_text is not None:
            rendered = self.render_text_diff(before_text, text, path=rel_path)
            if rendered:
                return rendered
        diff_lines = [f"--- {rel_path}", f"+++ {rel_path}"] + [f"+{line}" for line in lines[:40]]
        return "\n".join(diff_lines)[:2048]

    def diff_from_baseline(self) -> WorkspaceDiff:
        baseline = self.capture_baseline()
        final = self.capture_revision()
        scope_complete = baseline.scope_complete and final.scope_complete
        if not scope_complete:
            return WorkspaceDiff(
                baseline_revision=baseline.revision,
                final_revision=final.revision,
                scope_complete=False,
                excluded_note=_EXCLUDED_NOTE,
                entries=[],
                diff_truncated=True,
            )

        base_inv = baseline.inventory
        final_inv = final.inventory
        all_paths = sorted(set(base_inv) | set(final_inv))
        entries: list[DiffEntry] = []
        diff_truncated = False
        total_excerpt_bytes = 0

        for rel in all_paths:
            b_sha = base_inv.get(rel)
            a_sha = final_inv.get(rel)
            if b_sha == a_sha:
                continue
            op: Any = "create" if b_sha is None else ("delete" if a_sha is None else "modify")
            source: Any = "ai" if (a_sha is not None and self._app_written.get(rel) == a_sha) else "external_or_command"
            excerpt = ""
            if len(entries) < 128 and total_excerpt_bytes < 24 * 1024:
                excerpt = self._bounded_excerpt(rel, b_sha, a_sha)
                total_excerpt_bytes += len(excerpt.encode("utf-8"))
            else:
                diff_truncated = True
            entries.append(
                DiffEntry(
                    path=rel,
                    operation=op,
                    before_sha256=b_sha,
                    after_sha256=a_sha,
                    source=source,
                    diff_excerpt=excerpt,
                )
            )
        return WorkspaceDiff(
            baseline_revision=baseline.revision,
            final_revision=final.revision,
            scope_complete=True,
            excluded_note=_EXCLUDED_NOTE,
            entries=entries,
            diff_truncated=diff_truncated,
        )

    @staticmethod
    def render_text_diff(before_text: str, after_text: str, *, path: str) -> str:
        lines = list(
            difflib.unified_diff(
                before_text.splitlines(),
                after_text.splitlines(),
                fromfile=f"a/{path}",
                tofile=f"b/{path}",
                lineterm="",
            )
        )
        return "\n".join(lines)[:2048]
