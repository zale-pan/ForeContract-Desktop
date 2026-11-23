"""Read-only workspace file browsing and bounded local Git review for UI-T01."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from forecontract_app.project_models import (
    ListDirectoryResult,
    ReadFileResult,
)
from forecontract_app.project_workspace import (
    ProjectError,
    ProjectWorkspace,
    _is_excluded_rel_parts,
    is_reparse_or_symlink,
    normalize_relative_file,
)

GIT_TIMEOUT_SECONDS = 5.0
GIT_MAX_STATUS_ENTRIES = 200
GIT_MAX_PATCH_BYTES = 64 * 1024
GIT_MAX_STATUS_BYTES = 256 * 1024
GIT_MAX_STDERR_BYTES = 8 * 1024
GIT_MAX_CONFIG_BYTES = 1024 * 1024
_EMPTY_TREE_OID = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"

_STRIP_GIT_ENV_KEYS = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_SYSTEM",
        "GIT_EXTERNAL_DIFF",
        "GIT_EXEC_PATH",
        "GIT_TEMPLATE_DIR",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "GIT_SSH",
        "GIT_SSH_COMMAND",
    }
)


class ProjectGitStatusEntry(BaseModel):
    """One visible Git status entry for a project workspace."""

    model_config = ConfigDict(extra="forbid")

    path: str
    index_status: str
    worktree_status: str


class ProjectGitStatusView(BaseModel):
    """Bounded read-only Git status response."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    state: Literal["ready", "not_repository", "unavailable", "unsupported_layout"]
    branch: str | None = None
    head_oid: str | None = None
    entries: list[ProjectGitStatusEntry] = []
    truncated: bool = False
    reason: str | None = None


class ProjectGitDiffView(BaseModel):
    """Bounded read-only Git diff response for one path and review area."""

    model_config = ConfigDict(extra="forbid")

    project_id: str
    path: str
    area: Literal["index", "worktree"]
    kind: Literal["text", "binary", "untracked", "no_change", "unavailable"]
    patch: str = ""
    truncated: bool = False
    reason: str | None = None


def _resolve_trusted_git_executable() -> str | None:
    """Resolve the host git executable without invoking a shell."""
    git_path = shutil.which("git")
    if not git_path:
        return None
    candidate = Path(git_path)
    if not candidate.is_file():
        return None
    return str(candidate)


def _build_safe_git_env() -> dict[str, str]:
    """Return a sanitized environment preventing external filters, prompts, and config overrides."""
    env = {k: v for k, v in os.environ.items() if k.upper() not in _STRIP_GIT_ENV_KEYS}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_CONFIG_GLOBAL"] = ""
    env["GIT_CONFIG_SYSTEM"] = ""
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    env["LC_ALL"] = "C"
    return env


@dataclass(frozen=True)
class _GitCommandResult:
    """Bounded result from a read-only git subprocess."""

    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool = False
    stderr_truncated: bool = False


def _read_stream_bounded(
    stream: object,
    limit: int,
    on_limit: threading.Event,
    out_buf: bytearray,
    trunc_flag: list[bool],
) -> None:
    """Read from ``stream`` incrementally up to ``limit`` bytes, signaling early termination on overflow."""
    try:
        while True:
            chunk = stream.read(4096)  # type: ignore[attr-defined]
            if not chunk:
                break
            remaining = limit - len(out_buf)
            if remaining > 0:
                out_buf.extend(chunk[:remaining])
            if len(chunk) > remaining:
                trunc_flag[0] = True
                on_limit.set()
                break
    except (OSError, ValueError):
        pass


def _run_git(
    git_exe: str,
    root: Path,
    args: list[str],
    *,
    timeout: float = GIT_TIMEOUT_SECONDS,
    max_stdout_bytes: int = GIT_MAX_STATUS_BYTES,
    max_stderr_bytes: int = GIT_MAX_STDERR_BYTES,
) -> _GitCommandResult:
    """Run one read-only git command with safe flags, bounded pipe collection, and timeout."""
    argv = [
        git_exe,
        "--no-optional-locks",
        "--literal-pathspecs",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "diff.external=",
        "-c",
        "diff.mnemonicPrefix=false",
        "-c",
        "diff.noprefix=false",
        "-c",
        "core.quotepath=false",
        "-c",
        "core.autocrlf=false",
        "-c",
        "filter.lfs.clean=",
        "-c",
        "filter.lfs.smudge=",
        "-c",
        "filter.lfs.process=",
        "-c",
        "gc.auto=0",
        *args,
    ]
    proc = subprocess.Popen(
        argv,
        cwd=str(root),
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_build_safe_git_env(),
    )
    assert proc.stdout is not None and proc.stderr is not None
    out_buf = bytearray()
    err_buf = bytearray()
    out_trunc = [False]
    err_trunc = [False]
    limit_hit = threading.Event()
    t_out = threading.Thread(
        target=_read_stream_bounded,
        args=(proc.stdout, max_stdout_bytes, limit_hit, out_buf, out_trunc),
        daemon=True,
    )
    t_err = threading.Thread(
        target=_read_stream_bounded,
        args=(proc.stderr, max_stderr_bytes, limit_hit, err_buf, err_trunc),
        daemon=True,
    )
    t_out.start()
    t_err.start()
    try:
        # Poll briefly if output cap was hit so we terminate the child immediately
        deadline = timeout
        step = 0.02
        elapsed = 0.0
        while True:
            rc = proc.poll()
            if rc is not None:
                break
            if limit_hit.is_set():
                proc.kill()
                proc.wait(timeout=1.0)
                break
            wait_for = min(step, max(0.0, deadline - elapsed))
            if wait_for <= 0.0:
                proc.kill()
                proc.wait(timeout=1.0)
                raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)
            limit_hit.wait(timeout=wait_for)
            elapsed += wait_for
    finally:
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass
        t_out.join(timeout=1.0)
        t_err.join(timeout=1.0)
    return _GitCommandResult(
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=bytes(out_buf),
        stderr=bytes(err_buf),
        stdout_truncated=out_trunc[0],
        stderr_truncated=err_trunc[0],
    )


def _utf8_truncate_bytes(raw: bytes, limit: int) -> tuple[str, bool]:
    """Decode UTF-8 bytes and truncate cleanly at a character boundary within ``limit`` bytes."""
    if len(raw) <= limit:
        return raw.decode("utf-8", errors="replace"), False
    cut = raw[:limit]
    while cut and (cut[-1] & 0xC0) == 0x80:
        cut = cut[:-1]
    if cut and (cut[-1] & 0x80):
        # Drop any incomplete multi-byte leading byte at the boundary
        try:
            cut.decode("utf-8")
        except UnicodeDecodeError:
            while cut:
                cut = cut[:-1]
                try:
                    cut.decode("utf-8")
                    break
                except UnicodeDecodeError:
                    continue
    return cut.decode("utf-8", errors="replace"), True


def _is_path_visible_under_root(root: Path, rel_path: str) -> bool:
    """Return True only if ``rel_path`` is non-excluded and contains no symlink/reparse segments."""
    if not rel_path or rel_path.startswith(("/", "\\")):
        return False
    parts = tuple(rel_path.replace("\\", "/").split("/"))
    if any(p in ("", ".", "..") for p in parts):
        return False
    if _is_excluded_rel_parts(parts, is_dir=False):
        return False
    cur = root
    for part in parts[:-1]:
        cur = cur / part
        if cur.exists() and is_reparse_or_symlink(cur):
            return False
    leaf = cur / parts[-1]
    return not (leaf.exists() and is_reparse_or_symlink(leaf))


def _has_parent_git(root: Path) -> bool:
    """Check whether any ancestor of ``root`` has a .git entry."""
    for parent in root.resolve().parents:
        if (parent / ".git").exists():
            return True
    return False


def _is_repo_config_safe(dot_git: Path) -> tuple[bool, str | None]:
    """Inspect ``.git/config`` without spawning git and reject unsafe local hooks/filters/includes."""
    cfg_path = dot_git / "config"
    if is_reparse_or_symlink(cfg_path):
        return False, "git config is a symlink or reparse point"
    if not cfg_path.exists():
        return True, None
    if not cfg_path.is_file():
        return False, "git config is not a regular file"
    try:
        if cfg_path.stat().st_size > GIT_MAX_CONFIG_BYTES:
            return False, "git config exceeds maximum allowed size"
        raw_text = cfg_path.read_bytes().decode("utf-8", errors="strict")
    except (OSError, UnicodeDecodeError):
        return False, "unable to safely read git config"

    lines = raw_text.splitlines()
    logical_lines: list[str] = []
    buf = ""
    for line in lines:
        stripped = line.rstrip("\r\n")
        if stripped.endswith("\\") and not stripped.endswith("\\\\"):
            buf += stripped[:-1]
            continue
        buf += stripped
        logical_lines.append(buf)
        buf = ""
    if buf:
        logical_lines.append(buf)

    section = ""
    for raw_line in logical_lines:
        line = raw_line.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if "\x00" in line:
            return False, "git config contains NUL byte"
        if line.startswith("["):
            end_idx = line.find("]")
            if end_idx == -1:
                return False, "malformed git config section header"
            trailing = line[end_idx + 1 :].strip()
            if trailing and not trailing.startswith(("#", ";")):
                return False, "unexpected trailing content after git config section header"
            header = line[1:end_idx].strip()
            if not header:
                return False, "empty git config section header"
            # Support [section], [section "sub"], and [section.sub]
            quote_idx = header.find('"')
            if quote_idx != -1:
                if not header.endswith('"') or header.count('"') % 2 != 0:
                    return False, "malformed quoted git config subsection"
                sec_name = header[:quote_idx].strip().lower()
            else:
                sec_name = header.split(".", 1)[0].split(None, 1)[0].strip().lower()
            if not sec_name or any(ch.isspace() for ch in sec_name):
                return False, "malformed git config section name"
            section = sec_name
            if section in ("include", "includeif"):
                return False, "git config includes are not supported"
            continue
        if not section:
            return False, "git config entry outside section"
        # Strip inline comment outside quotes
        cleaned: list[str] = []
        in_quote = False
        escaped = False
        for ch in line:
            if escaped:
                cleaned.append(ch)
                escaped = False
                continue
            if ch == "\\":
                cleaned.append(ch)
                escaped = True
                continue
            if ch == '"':
                in_quote = not in_quote
                cleaned.append(ch)
                continue
            if not in_quote and ch in ("#", ";"):
                break
            cleaned.append(ch)
        if in_quote or escaped:
            return False, "unterminated quote or escape in git config"
        item = "".join(cleaned).strip()
        if not item:
            continue
        if "=" in item:
            key_raw, val_raw = item.split("=", 1)
            key = key_raw.strip().lower()
            val = val_raw.strip().strip('"').strip().lower()
        else:
            key = item.lower()
            val = "true"
        if not key or any(ch.isspace() for ch in key):
            return False, "malformed git config key"
        if (section, key) == ("core", "fsmonitor") and val not in ("", "false", "0", "no", "off"):
            return False, "unsafe git config: core.fsmonitor"
        if (section, key) in {
            ("core", "sshcommand"),
            ("core", "askpass"),
            ("core", "editor"),
            ("core", "pager"),
            ("core", "hookspath"),
            ("diff", "external"),
            ("credential", "helper"),
            ("gpg", "program"),
        }:
            return False, f"unsafe git config: {section}.{key}"
        if section == "diff" and key in ("command", "textconv"):
            return False, f"unsafe git config: diff.{key}"
        if section == "filter" and key in ("clean", "smudge", "process"):
            return False, f"unsafe git config: filter.{key}"
        if section == "merge" and key == "driver":
            return False, "unsafe git config: merge.driver"
    return True, None


def _validate_repo_layout(
    *,
    project_id: str,
    root: Path,
    git_exe: str | None,
) -> tuple[Literal["ready", "not_repository", "unavailable", "unsupported_layout"], str | None]:
    """Verify git availability and enforce strict single-root local repository layout."""
    if is_reparse_or_symlink(root):
        return "unsupported_layout", "project root is a symlink or reparse point"

    dot_git = root / ".git"
    if not dot_git.exists():
        if _has_parent_git(root):
            return "unsupported_layout", "project root is inside a parent git repository"
        return "not_repository", "project root is not a git repository"

    if is_reparse_or_symlink(dot_git) or not dot_git.is_dir():
        return "unsupported_layout", "only standard local .git directories are supported"

    if (dot_git / "commondir").exists() or (dot_git / "objects" / "info" / "alternates").exists():
        return "unsupported_layout", "linked worktrees and external alternates are not supported"

    for git_sub in ("objects", "refs", "HEAD", "index"):
        sub_path = dot_git / git_sub
        if sub_path.exists() and is_reparse_or_symlink(sub_path):
            return "unsupported_layout", f"git internal entry '{git_sub}' is a symlink or reparse point"

    cfg_ok, cfg_reason = _is_repo_config_safe(dot_git)
    if not cfg_ok:
        return "unavailable", cfg_reason

    if git_exe is None:
        return "unavailable", "git executable is not available"

    try:
        top_proc = _run_git(git_exe, root, ["rev-parse", "--show-toplevel"])
    except (OSError, subprocess.TimeoutExpired):
        return "unavailable", "git inspection timed out or failed"

    if top_proc.returncode != 0:
        return "unavailable", "git rev-parse failed"

    top_str = top_proc.stdout.decode("utf-8", errors="replace").strip()
    try:
        if Path(top_str).resolve() != root.resolve():
            return "unsupported_layout", "git toplevel does not match project root"
    except OSError:
        return "unsupported_layout", "unable to resolve git toplevel"

    return "ready", None


class ProjectReviewService:
    """Read-only workspace file and Git review adapter."""

    def __init__(self, *, data_root: Path) -> None:
        self._data_root = data_root

    def list_files(
        self,
        *,
        root: Path,
        path: str = ".",
        cursor: str | None = None,
        limit: int = 100,
    ) -> ListDirectoryResult:
        ws = ProjectWorkspace(root=root, data_root=self._data_root)
        return ws.list_directory(path=path, cursor=cursor, limit=limit)

    def read_file(
        self,
        *,
        root: Path,
        path: str,
        start_line: int = 1,
        byte_offset: int = 0,
        expected_sha256: str | None = None,
    ) -> ReadFileResult:
        ws = ProjectWorkspace(root=root, data_root=self._data_root)
        return ws.read_file(
            path=path,
            start_line=start_line,
            byte_offset=byte_offset,
            expected_sha256=expected_sha256,
        )

    def git_status(self, *, project_id: str, root: Path) -> ProjectGitStatusView:
        git_exe = _resolve_trusted_git_executable()
        state, reason = _validate_repo_layout(project_id=project_id, root=root, git_exe=git_exe)
        if state != "ready" or git_exe is None:
            return ProjectGitStatusView(project_id=project_id, state=state, reason=reason)

        try:
            branch_proc = _run_git(git_exe, root, ["branch", "--show-current"])
            head_proc = _run_git(git_exe, root, ["rev-parse", "--short", "HEAD"])
            status_proc = _run_git(
                git_exe,
                root,
                ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
            )
        except (OSError, subprocess.TimeoutExpired):
            return ProjectGitStatusView(
                project_id=project_id,
                state="unavailable",
                reason="git status timed out or failed",
            )

        if status_proc.returncode != 0 and not status_proc.stdout_truncated:
            return ProjectGitStatusView(
                project_id=project_id,
                state="unavailable",
                reason="git status returned non-zero exit code",
            )

        branch_name = branch_proc.stdout.decode("utf-8", errors="replace").strip() or None
        head_oid = (
            head_proc.stdout.decode("utf-8", errors="replace").strip() or None
            if head_proc.returncode == 0
            else None
        )

        entries: list[ProjectGitStatusEntry] = []
        truncated = bool(status_proc.stdout_truncated)
        tokens = status_proc.stdout.split(b"\x00")
        if status_proc.stdout_truncated and tokens:
            # Drop potentially incomplete trailing token when stdout was capped mid-stream
            tokens = tokens[:-1]
        idx = 0
        while idx < len(tokens):
            tok = tokens[idx]
            idx += 1
            if not tok:
                continue
            text = tok.decode("utf-8", errors="replace")
            if len(text) < 4 or text[2] != " ":
                continue
            x_code, y_code = text[0], text[1]
            rel_path = text[3:].replace("\\", "/")
            if x_code in ("R", "C") or y_code in ("R", "C"):
                # Split renames/copies into delete(old) + add(new) while keeping exclusion checks.
                old_path = ""
                if idx < len(tokens):
                    old_path = tokens[idx].decode("utf-8", errors="replace").replace("\\", "/")
                    idx += 1
                if x_code == "R" and old_path and _is_path_visible_under_root(root, old_path):
                    if len(entries) < GIT_MAX_STATUS_ENTRIES:
                        entries.append(
                            ProjectGitStatusEntry(path=old_path, index_status="D", worktree_status=" ")
                        )
                    else:
                        truncated = True
                if _is_path_visible_under_root(root, rel_path):
                    if len(entries) < GIT_MAX_STATUS_ENTRIES:
                        entries.append(
                            ProjectGitStatusEntry(path=rel_path, index_status="A", worktree_status=y_code)
                        )
                    else:
                        truncated = True
                continue

            if not _is_path_visible_under_root(root, rel_path):
                continue
            if len(entries) >= GIT_MAX_STATUS_ENTRIES:
                truncated = True
                break
            entries.append(
                ProjectGitStatusEntry(
                    path=rel_path,
                    index_status=x_code,
                    worktree_status=y_code,
                )
            )

        return ProjectGitStatusView(
            project_id=project_id,
            state="ready",
            branch=branch_name,
            head_oid=head_oid,
            entries=entries,
            truncated=truncated,
        )

    def git_diff(
        self,
        *,
        project_id: str,
        root: Path,
        path: str,
        area: Literal["index", "worktree"],
    ) -> ProjectGitDiffView:
        norm = normalize_relative_file(path)
        parts = tuple(norm.split("/"))
        if _is_excluded_rel_parts(parts, is_dir=False) or any(p.lower() == ".git" for p in parts):
            raise ProjectError("EXCLUDED_PATH", f"path '{norm}' is excluded from review", status_code=403)
        cur = root
        for part in parts[:-1]:
            cur = cur / part
            if cur.exists() and is_reparse_or_symlink(cur):
                raise ProjectError("EXCLUDED_PATH", f"path '{norm}' crosses a symlink", status_code=403)
        leaf = cur / parts[-1]
        if leaf.exists() and is_reparse_or_symlink(leaf):
            raise ProjectError("EXCLUDED_PATH", f"path '{norm}' is a symlink", status_code=403)
        if leaf.exists() and leaf.is_dir():
            raise ProjectError("INVALID_PATH", f"path '{norm}' is a directory", status_code=400)
        if any(ch in norm for ch in ("*", "?")):
            raise ProjectError("INVALID_PATH", f"path '{norm}' contains unsupported wildcard characters", status_code=400)

        git_exe = _resolve_trusted_git_executable()
        state, reason = _validate_repo_layout(project_id=project_id, root=root, git_exe=git_exe)
        if state != "ready" or git_exe is None:
            return ProjectGitDiffView(
                project_id=project_id,
                path=norm,
                area=area,
                kind="unavailable",
                reason=reason or state,
            )

        try:
            # Verify target is not a tracked/HEAD directory (including deleted directories)
            ls_files_proc = _run_git(git_exe, root, ["ls-files", "-z", "--", norm])
            if ls_files_proc.returncode != 0 or ls_files_proc.stdout_truncated:
                return ProjectGitDiffView(
                    project_id=project_id,
                    path=norm,
                    area=area,
                    kind="unavailable",
                    reason="git ls-files metadata check failed or truncated",
                )
            if ls_files_proc.stdout:
                tracked = [t.decode("utf-8", errors="replace").replace("\\", "/") for t in ls_files_proc.stdout.split(b"\x00") if t]
                if any(tp != norm and tp.startswith(f"{norm}/") for tp in tracked):
                    raise ProjectError("INVALID_PATH", f"path '{norm}' is a directory", status_code=400)
                if any(tp != norm for tp in tracked):
                    return ProjectGitDiffView(
                        project_id=project_id,
                        path=norm,
                        area=area,
                        kind="unavailable",
                        reason="git ls-files returned ambiguous path",
                    )

            head_proc = _run_git(git_exe, root, ["rev-parse", "--verify", "HEAD"])
            if head_proc.stdout_truncated:
                return ProjectGitDiffView(
                    project_id=project_id,
                    path=norm,
                    area=area,
                    kind="unavailable",
                    reason="git HEAD verification truncated",
                )
            head_ok = head_proc.returncode == 0
            if not head_ok:
                sym_proc = _run_git(git_exe, root, ["symbolic-ref", "--quiet", "HEAD"])
                sym_ref = sym_proc.stdout.decode("utf-8", errors="replace").strip()
                if (
                    sym_proc.returncode != 0
                    or sym_proc.stdout_truncated
                    or not sym_ref.startswith("refs/heads/")
                ):
                    return ProjectGitDiffView(
                        project_id=project_id,
                        path=norm,
                        area=area,
                        kind="unavailable",
                        reason="git HEAD state cannot be verified",
                    )
                ref_proc = _run_git(git_exe, root, ["show-ref", "--verify", "--quiet", sym_ref])
                ref_absent = (
                    ref_proc.returncode == 1
                    and not ref_proc.stdout_truncated
                    and not ref_proc.stderr_truncated
                    and not ref_proc.stderr.strip()
                )
                if not ref_absent:
                    return ProjectGitDiffView(
                        project_id=project_id,
                        path=norm,
                        area=area,
                        kind="unavailable",
                        reason="git HEAD branch reference exists or cannot be verified",
                    )
            else:
                ls_tree_proc = _run_git(git_exe, root, ["ls-tree", "-z", "HEAD", "--", norm])
                if ls_tree_proc.returncode != 0 or ls_tree_proc.stdout_truncated:
                    return ProjectGitDiffView(
                        project_id=project_id,
                        path=norm,
                        area=area,
                        kind="unavailable",
                        reason="git ls-tree metadata check failed or truncated",
                    )
                if ls_tree_proc.stdout:
                    for entry in ls_tree_proc.stdout.split(b"\x00"):
                        if not entry:
                            continue
                        meta_part, _, tree_path = entry.decode("utf-8", errors="replace").partition("\t")
                        norm_tree = tree_path.replace("\\", "/")
                        if norm_tree == norm and meta_part.startswith("040000 "):
                            raise ProjectError("INVALID_PATH", f"path '{norm}' is a directory", status_code=400)
                        if norm_tree != norm:
                            return ProjectGitDiffView(
                                project_id=project_id,
                                path=norm,
                                area=area,
                                kind="unavailable",
                                reason="git ls-tree returned ambiguous path",
                            )
                        if not meta_part.startswith(("100644 ", "100755 ")):
                            raise ProjectError("EXCLUDED_PATH", f"path '{norm}' has unsupported git object mode", status_code=403)

            st_proc = _run_git(
                git_exe,
                root,
                ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--", norm],
            )
            if st_proc.returncode != 0 or st_proc.stdout_truncated:
                return ProjectGitDiffView(
                    project_id=project_id,
                    path=norm,
                    area=area,
                    kind="unavailable",
                    reason="git status metadata check failed or truncated",
                )
            if area == "worktree" and st_proc.stdout.startswith(b"?? "):
                return ProjectGitDiffView(
                    project_id=project_id,
                    path=norm,
                    area=area,
                    kind="untracked",
                    reason="untracked file; inspect via file reader",
                )

            if area == "index":
                base_ref = "HEAD" if head_ok else _EMPTY_TREE_OID
                diff_args = ["diff", "--cached", "--no-renames", "--no-ext-diff", "--no-textconv", base_ref, "--", norm]
            else:
                diff_args = ["diff", "--no-renames", "--no-ext-diff", "--no-textconv", "--", norm]

            diff_proc = _run_git(
                git_exe,
                root,
                diff_args,
                max_stdout_bytes=GIT_MAX_PATCH_BYTES + 1,
            )
        except (OSError, subprocess.TimeoutExpired):
            return ProjectGitDiffView(
                project_id=project_id,
                path=norm,
                area=area,
                kind="unavailable",
                reason="git diff timed out or failed",
            )

        if diff_proc.returncode != 0 and not diff_proc.stdout_truncated:
            return ProjectGitDiffView(
                project_id=project_id,
                path=norm,
                area=area,
                kind="unavailable",
                reason="git diff returned non-zero exit code",
            )

        raw = diff_proc.stdout
        if not raw:
            return ProjectGitDiffView(
                project_id=project_id,
                path=norm,
                area=area,
                kind="no_change",
            )

        expected_diff_hdr = f"diff --git a/{norm} b/{norm}"
        for hdr_line in raw.splitlines():
            if hdr_line.startswith(b"diff --git "):
                text_line = hdr_line.decode("utf-8", errors="replace")
                if text_line != expected_diff_hdr:
                    raise ProjectError("EXCLUDED_PATH", "diff header does not match requested single path", status_code=403)
            elif hdr_line.startswith((b"rename from ", b"rename to ", b"copy from ", b"copy to ")):
                text_line = hdr_line.decode("utf-8", errors="replace")
                _, _, hdr_path = text_line.partition(" from " if " from " in text_line else " to ")
                if hdr_path != norm or not _is_path_visible_under_root(root, hdr_path):
                    raise ProjectError("EXCLUDED_PATH", "diff references an excluded or mismatched path", status_code=403)

        if b"\nBinary files " in raw or raw.startswith(b"Binary files ") or b"\nGIT binary patch" in raw:
            return ProjectGitDiffView(
                project_id=project_id,
                path=norm,
                area=area,
                kind="binary",
                reason="binary file diff omitted",
            )

        patch_text, cut_truncated = _utf8_truncate_bytes(raw, GIT_MAX_PATCH_BYTES)
        return ProjectGitDiffView(
            project_id=project_id,
            path=norm,
            area=area,
            kind="text",
            patch=patch_text,
            truncated=bool(cut_truncated or diff_proc.stdout_truncated),
        )
