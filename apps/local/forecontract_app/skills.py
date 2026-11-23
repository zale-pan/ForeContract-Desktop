"""Local project Skill discovery and bounded UTF-8 page reader."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Final

from forecontract_app.extension_models import (
    DEFAULT_SKILL_PAGE_BYTES,
    MAX_SELECTED_SKILL_CONTEXT_BYTES,
    MAX_SELECTED_SKILLS,
    MAX_SKILL_COUNT,
    MAX_SKILL_FILE_BYTES,
    SKILL_ID_PATTERN,
    ExtensionError,
    SelectedSkillContext,
    SkillInfo,
    SkillPage,
)
from forecontract_app.security import check_no_reparse_ancestors, is_reparse_or_symlink

_RESERVED_DIR_NAMES: Final[frozenset[str]] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".env",
        "credentials",
        "secrets",
        ".aws",
        ".ssh",
        ".gnupg",
    }
)


def _is_forbidden_segment(seg: str) -> bool:
    low = seg.lower()
    if low in _RESERVED_DIR_NAMES:
        return True
    if low == ".env" or low.startswith(".env."):
        return True
    return low.endswith((".pem", ".key", ".p12", ".pfx", ".kdbx"))


def _validate_skill_id(skill_id: object) -> str:
    if not isinstance(skill_id, str) or not SKILL_ID_PATTERN.fullmatch(skill_id):
        raise ExtensionError("INVALID_SKILL_ID", "skill_id must match ASCII pattern", status_code=400)
    if _is_forbidden_segment(skill_id):
        raise ExtensionError("FORBIDDEN_SKILL_ID", "reserved directory name forbidden", status_code=403)
    return skill_id


def _normalize_skill_relpath(raw: object) -> list[str]:
    if not isinstance(raw, str) or not raw or not raw.strip() or "\x00" in raw:
        raise ExtensionError("INVALID_SKILL_PATH", "relative_path must be non-blank", status_code=400)
    if any(ord(ch) < 32 for ch in raw):
        raise ExtensionError("INVALID_SKILL_PATH", "relative_path contains control bytes", status_code=400)
    if "\\" in raw or raw.startswith(("/", "~")):
        raise ExtensionError(
            "INVALID_SKILL_PATH", "absolute or backslash paths are forbidden", status_code=403
        )
    if len(raw) >= 2 and raw[1] == ":" and raw[0].isalpha():
        raise ExtensionError("INVALID_SKILL_PATH", "drive-letter paths are forbidden", status_code=403)
    posix = PurePosixPath(raw)
    if posix.is_absolute():
        raise ExtensionError("INVALID_SKILL_PATH", "absolute path forbidden", status_code=403)
    parts = [p for p in raw.split("/") if p != ""]
    if raw.endswith("/") or not parts:
        raise ExtensionError("INVALID_SKILL_PATH", "relative_path must point to a file", status_code=400)
    for part in parts:
        if part in (".", ".."):
            raise ExtensionError(
                "INVALID_SKILL_PATH", "traversal segments are forbidden", status_code=403
            )
        if _is_forbidden_segment(part):
            raise ExtensionError(
                "FORBIDDEN_SKILL_PATH",
                f"reserved credential/secret segment '{part}' is forbidden",
                status_code=403,
            )
    return parts


def _read_verified_utf8_file(path: Path) -> tuple[bytes, str, str]:
    if os.path.lexists(path) and is_reparse_or_symlink(path):
        raise ExtensionError("FORBIDDEN_SKILL_PATH", "skill target cannot be symlink or reparse", status_code=403)
    if not path.exists() or not path.is_file():
        raise ExtensionError("SKILL_FILE_NOT_FOUND", "skill file does not exist", status_code=404)
    try:
        st = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ExtensionError("SKILL_READ_FAILED", f"cannot stat skill file: {exc}", status_code=400) from exc
    if st.st_size > MAX_SKILL_FILE_BYTES:
        raise ExtensionError(
            "SKILL_FILE_TOO_LARGE",
            f"skill file exceeds {MAX_SKILL_FILE_BYTES} bytes",
            status_code=400,
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ExtensionError("SKILL_READ_FAILED", f"cannot read skill file: {exc}", status_code=400) from exc
    if len(raw) > MAX_SKILL_FILE_BYTES:
        raise ExtensionError(
            "SKILL_FILE_TOO_LARGE",
            f"skill file exceeds {MAX_SKILL_FILE_BYTES} bytes",
            status_code=400,
        )
    if b"\x00" in raw:
        raise ExtensionError("INVALID_SKILL_CONTENT", "NUL bytes are forbidden in skill files", status_code=400)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExtensionError("INVALID_UTF8", "skill file must be valid UTF-8", status_code=400) from exc
    sha = hashlib.sha256(raw).hexdigest()
    return raw, text, sha


class SkillCatalog:
    """Safe single-level discovery and bounded UTF-8 reader under ``.forecontract/skills``."""

    def __init__(self, project_root: Path) -> None:
        if not isinstance(project_root, Path):
            raise ExtensionError("INVALID_PROJECT_ROOT", "project_root must be Path", status_code=400)
        self._project_root = project_root

    def _resolve_skills_root(self, *, create_not_allowed: bool = True) -> Path | None:
        _ = create_not_allowed
        try:
            check_no_reparse_ancestors(self._project_root, label="project_root")
        except Exception as exc:
            raise ExtensionError("FORBIDDEN_SKILL_PATH", f"unsafe project_root: {exc}", status_code=403) from exc
        if not self._project_root.exists() or not self._project_root.is_dir():
            raise ExtensionError("INVALID_PROJECT_ROOT", "project_root does not exist", status_code=400)
        real_proj = Path(os.path.realpath(self._project_root))

        fc_dir = real_proj / ".forecontract"
        if os.path.lexists(fc_dir):
            if is_reparse_or_symlink(fc_dir):
                raise ExtensionError(
                    "FORBIDDEN_SKILL_PATH", ".forecontract cannot be symlink or reparse point", status_code=403
                )
            if not fc_dir.is_dir():
                return None
        else:
            return None

        skills_dir = fc_dir / "skills"
        if os.path.lexists(skills_dir):
            if is_reparse_or_symlink(skills_dir):
                raise ExtensionError(
                    "FORBIDDEN_SKILL_PATH",
                    ".forecontract/skills cannot be symlink or reparse point",
                    status_code=403,
                )
            if not skills_dir.is_dir():
                return None
        else:
            return None

        real_skills = Path(os.path.realpath(skills_dir))
        if real_proj not in real_skills.parents:
            raise ExtensionError("FORBIDDEN_SKILL_PATH", "skills dir escapes project root", status_code=403)
        return real_skills

    def list_skills(self) -> list[SkillInfo]:
        """Discover single-level ASCII skill dirs containing ``SKILL.md`` without reading refs."""
        skills_dir = self._resolve_skills_root()
        if skills_dir is None:
            return []

        found: list[SkillInfo] = []
        with os.scandir(skills_dir) as it:
            entries = sorted(it, key=lambda e: e.name)

        for entry in entries:
            name = entry.name
            entry_path = Path(entry.path)
            if is_reparse_or_symlink(entry_path):
                continue
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if not SKILL_ID_PATTERN.fullmatch(name) or _is_forbidden_segment(name):
                continue
            skill_md = entry_path / "SKILL.md"
            if not os.path.lexists(skill_md) or is_reparse_or_symlink(skill_md):
                continue
            try:
                if not skill_md.is_file():
                    continue
                raw, _, sha = _read_verified_utf8_file(skill_md)
            except ExtensionError:
                continue
            found.append(
                SkillInfo(
                    id=name,
                    path=f".forecontract/skills/{name}/SKILL.md",
                    sha256=sha,
                    total_bytes=len(raw),
                )
            )
            if len(found) > MAX_SKILL_COUNT:
                raise ExtensionError(
                    "SKILL_LIMIT_EXCEEDED",
                    f"project exceeds maximum {MAX_SKILL_COUNT} skills",
                    status_code=400,
                )
        return found

    def read(
        self,
        *,
        skill_id: str,
        relative_path: str = "SKILL.md",
        byte_offset: int = 0,
        max_bytes: int = DEFAULT_SKILL_PAGE_BYTES,
        expected_sha256: str | None = None,
    ) -> SkillPage:
        """Read one UTF-8-aligned byte page from ``SKILL.md`` or a relative file under that skill."""
        clean_id = _validate_skill_id(skill_id)
        parts = _normalize_skill_relpath(relative_path)
        if isinstance(byte_offset, bool) or not isinstance(byte_offset, int) or byte_offset < 0:
            raise ExtensionError("INVALID_OFFSET", "byte_offset must be non-negative int", status_code=400)
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or not (1 <= max_bytes <= DEFAULT_SKILL_PAGE_BYTES)
        ):
            raise ExtensionError(
                "INVALID_INPUT",
                f"max_bytes must be integer in 1..{DEFAULT_SKILL_PAGE_BYTES}",
                status_code=400,
            )
        if byte_offset > 0 and not expected_sha256:
            raise ExtensionError(
                "EXPECTED_SHA256_REQUIRED",
                "expected_sha256 is required when byte_offset > 0",
                status_code=400,
            )
        if expected_sha256 is not None and (
            not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
            or any(c not in "0123456789abcdef" for c in expected_sha256)
        ):
            raise ExtensionError("INVALID_INPUT", "expected_sha256 must be 64 lowercase hex", status_code=400)

        skills_dir = self._resolve_skills_root()
        if skills_dir is None:
            raise ExtensionError("SKILL_NOT_FOUND", "skills directory does not exist", status_code=404)

        skill_root = skills_dir / clean_id
        if os.path.lexists(skill_root) and is_reparse_or_symlink(skill_root):
            raise ExtensionError("FORBIDDEN_SKILL_PATH", "skill directory cannot be symlink or reparse", status_code=403)
        if not skill_root.exists() or not skill_root.is_dir():
            raise ExtensionError("SKILL_NOT_FOUND", f"skill '{clean_id}' not found", status_code=404)
        real_skill_root = Path(os.path.realpath(skill_root))
        if skills_dir not in real_skill_root.parents:
            raise ExtensionError("FORBIDDEN_SKILL_PATH", "skill directory escapes skills root", status_code=403)

        cur = real_skill_root
        for part in parts:
            cur = cur / part
            if os.path.lexists(cur) and is_reparse_or_symlink(cur):
                raise ExtensionError(
                    "FORBIDDEN_SKILL_PATH",
                    "symlink or junction forbidden inside skill path",
                    status_code=403,
                )
        real_target = Path(os.path.realpath(cur))
        if real_skill_root not in real_target.parents:
            raise ExtensionError("FORBIDDEN_SKILL_PATH", "target escapes owning skill root", status_code=403)

        raw, _, sha = _read_verified_utf8_file(real_target)
        total_bytes = len(raw)
        if expected_sha256 is not None and sha != expected_sha256:
            raise ExtensionError(
                "STALE_SOURCE",
                "skill file sha256 changed since previous read",
                status_code=409,
            )
        if byte_offset > total_bytes:
            raise ExtensionError(
                "INVALID_OFFSET",
                f"byte_offset {byte_offset} exceeds total_bytes {total_bytes}",
                status_code=400,
            )

        if byte_offset == total_bytes:
            return SkillPage(
                id=clean_id,
                path=f".forecontract/skills/{clean_id}/{'/'.join(parts)}",
                sha256=sha,
                total_bytes=total_bytes,
                content="",
                byte_offset=byte_offset,
                next_offset=total_bytes,
                eof=True,
            )

        # Verify byte_offset starts at a valid UTF-8 codepoint boundary.
        if (raw[byte_offset] & 0xC0) == 0x80:
            raise ExtensionError(
                "INVALID_OFFSET",
                "byte_offset splits a UTF-8 multi-byte character",
                status_code=400,
            )

        end = min(total_bytes, byte_offset + max_bytes)
        content: str | None = None
        while end > byte_offset:
            try:
                content = raw[byte_offset:end].decode("utf-8")
                break
            except UnicodeDecodeError:
                end -= 1
        if content is None:
            raise ExtensionError(
                "INVALID_INPUT",
                "page size too small to decode next UTF-8 character cleanly",
                status_code=400,
            )

        return SkillPage(
            id=clean_id,
            path=f".forecontract/skills/{clean_id}/{'/'.join(parts)}",
            sha256=sha,
            total_bytes=total_bytes,
            content=content,
            byte_offset=byte_offset,
            next_offset=end,
            eof=(end >= total_bytes),
        )


def load_selected_skills(
    *, project_root: Path, skill_ids: Sequence[str]
) -> list[SelectedSkillContext]:
    """Validate and load selected SKILL.md texts up to max 4 items and 16 KiB total context."""
    cleaned_ids: list[str] = []
    seen: set[str] = set()
    for sid in skill_ids:
        valid = _validate_skill_id(sid)
        if valid in seen:
            raise ExtensionError("INVALID_INPUT", f"duplicate selected skill_id: {valid}", status_code=400)
        seen.add(valid)
        cleaned_ids.append(valid)

    if len(cleaned_ids) > MAX_SELECTED_SKILLS:
        raise ExtensionError(
            "SKILL_SELECTION_LIMIT",
            f"cannot select more than {MAX_SELECTED_SKILLS} skills per run",
            status_code=400,
        )
    if not cleaned_ids:
        return []

    catalog = SkillCatalog(project_root)
    available = {item.id: item for item in catalog.list_skills()}
    selected: list[SelectedSkillContext] = []
    total_raw_bytes = 0
    for sid in cleaned_ids:
        if sid not in available:
            raise ExtensionError("SKILL_NOT_FOUND", f"selected skill '{sid}' not found", status_code=400)
        chunks: list[str] = []
        offset = 0
        sha: str | None = None
        info: SkillPage | None = None
        while True:
            page = catalog.read(
                skill_id=sid,
                relative_path="SKILL.md",
                byte_offset=offset,
                max_bytes=DEFAULT_SKILL_PAGE_BYTES,
                expected_sha256=sha,
            )
            info = page
            sha = page.sha256
            chunks.append(page.content)
            offset = page.next_offset
            if page.eof:
                break
        assert info is not None
        total_raw_bytes += info.total_bytes
        if total_raw_bytes > MAX_SELECTED_SKILL_CONTEXT_BYTES:
            raise ExtensionError(
                "SKILL_CONTEXT_LIMIT",
                f"selected skills exceed {MAX_SELECTED_SKILL_CONTEXT_BYTES} UTF-8 bytes",
                status_code=400,
            )
        selected.append(
            SelectedSkillContext(
                id=sid,
                path=info.path,
                sha256=info.sha256,
                total_bytes=info.total_bytes,
                content="".join(chunks),
            )
        )

    serialized = json.dumps([s.model_dump(mode="json") for s in selected], ensure_ascii=False).encode(
        "utf-8"
    )
    if len(serialized) > MAX_SELECTED_SKILL_CONTEXT_BYTES:
        raise ExtensionError(
            "SKILL_CONTEXT_LIMIT",
            f"selected skill context package exceeds {MAX_SELECTED_SKILL_CONTEXT_BYTES} bytes",
            status_code=400,
        )
    return selected
