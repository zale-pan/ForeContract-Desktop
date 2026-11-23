"""Strict DTOs, closed limits, and config loader for local Skills and MCP tools subset."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import Annotated, Any, Final, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from forecontract_app.models import (
    AppError,
    NonNegativeStrictInt,
    StrictBool,
    StrictStr,
)
from forecontract_app.security import check_no_reparse_ancestors, is_reparse_or_symlink
from forecontract_app.session_models import sha256_canonical_json

MCP_PROTOCOL_VERSION: Final[str] = "2025-11-25"
MAX_EXTENSION_CONFIG_BYTES: Final[int] = 64 * 1024
MAX_MCP_SERVERS: Final[int] = 8
MAX_MCP_ARGV_ITEMS: Final[int] = 64
MAX_MCP_ARGV_BYTES: Final[int] = 16 * 1024
MAX_JSONRPC_FRAME_BYTES: Final[int] = 256 * 1024
MAX_MCP_STDERR_BYTES: Final[int] = 8 * 1024
MAX_MCP_TOOL_PAGES: Final[int] = 16
MAX_MCP_TOOLS_PER_SERVER: Final[int] = 128
MAX_SCHEMA_DEPTH: Final[int] = 8

MAX_SKILL_COUNT: Final[int] = 64
MAX_SKILL_FILE_BYTES: Final[int] = 64 * 1024
DEFAULT_SKILL_PAGE_BYTES: Final[int] = 8192
MAX_SELECTED_SKILLS: Final[int] = 4
MAX_SELECTED_SKILL_CONTEXT_BYTES: Final[int] = 16 * 1024

EXTENSION_PROPOSAL_TTL_SECONDS: Final[float] = 120.0
MAX_MCP_CALLS_PER_RUN: Final[int] = 32
MAX_MCP_CALL_DATA_BYTES_PER_RUN: Final[int] = 1024 * 1024
MAX_MCP_RESULT_PAGE_BYTES: Final[int] = 8192

MCP_CONNECT_TIMEOUT_SECONDS: Final[float] = 5.0
MCP_LIST_TIMEOUT_SECONDS: Final[float] = 5.0
MCP_STOP_TIMEOUT_SECONDS: Final[float] = 2.0
MIN_MCP_CALL_TIMEOUT_MS: Final[int] = 100
MAX_MCP_CALL_TIMEOUT_MS: Final[int] = 30_000
DEFAULT_MCP_CALL_TIMEOUT_MS: Final[int] = 10_000

ASCII_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
SKILL_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SHA256_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_SHELL_NAMES: Final[frozenset[str]] = frozenset(
    {"cmd.exe", "powershell.exe", "pwsh.exe", "bash.exe", "sh.exe", "bash", "sh"}
)

ServerIdStr = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9._-]{1,64}$")]
SkillIdStr = Annotated[
    str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
]

ExtensionProposalKind = Literal["extension_connect", "extension_call"]
ExtensionProposalState = Literal["pending", "approved", "rejected", "expired", "revoked"]
McpCallStatus = Literal["success", "error", "cancelled", "timeout", "unknown"]
McpEffectStatus = Literal["unknown", "not_applied", "possibly_applied"]
McpServerState = Literal["not_connected", "connected", "error", "closed", "unknown"]


class ExtensionError(AppError):
    """Structured error raised by Skill discovery, MCP client, or extension runtime."""

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(code, message, status_code=status_code)


class _StrictExtModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


def _reject_nonfinite_json(token: str) -> Any:
    raise ValueError(f"non-finite JSON constant is forbidden: {token}")


def parse_strict_json_bytes(raw: bytes, *, limit_bytes: int, label: str) -> Any:
    """Parse bounded UTF-8 JSON rejecting non-finite numeric literals."""
    if len(raw) > limit_bytes:
        raise ExtensionError(
            "PAYLOAD_TOO_LARGE",
            f"{label} exceeds {limit_bytes} bytes limit",
            status_code=400,
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ExtensionError("INVALID_UTF8", f"{label} must be valid UTF-8", status_code=400) from exc
    if "\x00" in text:
        raise ExtensionError("INVALID_INPUT", f"{label} must not contain NUL bytes", status_code=400)
    try:
        return json.loads(text, parse_constant=_reject_nonfinite_json)
    except json.JSONDecodeError as exc:
        raise ExtensionError("INVALID_JSON", f"{label} is not valid JSON: {exc}", status_code=400) from exc


def validate_no_nonfinite_numbers(value: Any, *, depth: int = 0) -> None:
    """Recursively reject NaN/Infinity floats or excessive nesting."""
    if depth > 32:
        raise ExtensionError("INVALID_INPUT", "JSON value nesting exceeds limit", status_code=400)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ExtensionError("INVALID_INPUT", "non-finite float is forbidden", status_code=400)
    elif isinstance(value, dict):
        for k, v in value.items():
            if not isinstance(k, str) or "\x00" in k:
                raise ExtensionError("INVALID_INPUT", "object keys must be NUL-free strings", status_code=400)
            validate_no_nonfinite_numbers(v, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            validate_no_nonfinite_numbers(item, depth=depth + 1)


def _validate_absolute_local_path(raw: str, *, label: str, expect_dir: bool) -> Path:
    if not isinstance(raw, str) or not raw or not raw.strip() or "\x00" in raw:
        raise ExtensionError("INVALID_EXTENSION_CONFIG", f"{label} must be non-blank", status_code=400)
    if any(ord(ch) < 32 for ch in raw):
        raise ExtensionError(
            "INVALID_EXTENSION_CONFIG", f"{label} contains control characters", status_code=400
        )
    if raw.startswith(("\\\\", "//")):
        raise ExtensionError(
            "INVALID_EXTENSION_CONFIG", f"{label} UNC paths are forbidden", status_code=400
        )
    norm = raw.replace("\\", "/")
    segments = [seg for seg in norm.split("/") if seg]
    if "." in segments or ".." in segments:
        raise ExtensionError(
            "INVALID_EXTENSION_CONFIG", f"{label} must not contain '.' or '..' segments", status_code=400
        )
    win = PureWindowsPath(raw)
    if not win.is_absolute() or any(part in (".", "..") for part in win.parts):
        raise ExtensionError(
            "INVALID_EXTENSION_CONFIG", f"{label} must be a clean absolute path", status_code=400
        )
    p = Path(raw)
    try:
        check_no_reparse_ancestors(p, label=label)
    except AppError as exc:
        raise ExtensionError("INVALID_EXTENSION_CONFIG", exc.message, status_code=400) from exc
    if not p.exists():
        raise ExtensionError(
            "INVALID_EXTENSION_CONFIG", f"{label} does not exist on disk", status_code=400
        )
    if is_reparse_or_symlink(p):
        raise ExtensionError(
            "INVALID_EXTENSION_CONFIG", f"{label} must not be a symlink or reparse point", status_code=400
        )
    real_p = Path(os.path.realpath(p))
    if expect_dir:
        if not real_p.is_dir():
            raise ExtensionError(
                "INVALID_EXTENSION_CONFIG", f"{label} must be an existing directory", status_code=400
            )
    else:
        if not real_p.is_file():
            raise ExtensionError(
                "INVALID_EXTENSION_CONFIG", f"{label} must be an existing file", status_code=400
            )
        if real_p.name.lower() in _FORBIDDEN_SHELL_NAMES:
            raise ExtensionError(
                "SHELL_EXECUTABLE_FORBIDDEN",
                f"{label} shell executable '{real_p.name}' is forbidden",
                status_code=403,
            )
    return real_p


def compute_file_sha256(path: Path) -> str:
    """Compute lowercase SHA-256 hex digest of a regular file."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


def validate_loopback_http_url(url: str) -> str:
    """Enforce literal loopback HTTP-only URL without userinfo, query, fragment, or DNS host."""
    if not isinstance(url, str) or not url or url != url.strip() or "\x00" in url:
        raise ValueError("url must be a non-blank string without surrounding whitespace")
    if any(ord(ch) < 32 for ch in url):
        raise ValueError("url contains control characters")
    if "?" in url or "#" in url or "@" in url:
        raise ValueError("url must not contain userinfo, query, or fragment")
    parsed = urlsplit(url)
    if parsed.scheme != "http":
        raise ValueError("url scheme must be exact lowercase 'http'")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("url must not include userinfo")
    if parsed.query or parsed.fragment:
        raise ValueError("url must not include query or fragment")
    netloc = parsed.netloc
    if not netloc or "@" in netloc:
        raise ValueError("url missing host:port")
    if netloc.startswith("["):
        if "]:" not in netloc:
            raise ValueError("IPv6 URL must use literal '[::1]:<port>'")
        host_part, port_str = netloc.split("]:", 1)
        host_literal = host_part + "]"
        if host_literal != "[::1]":
            raise ValueError("only literal '[::1]' or '127.0.0.1' loopback host is allowed")
    else:
        if ":" not in netloc:
            raise ValueError("URL must include explicit port")
        host_literal, port_str = netloc.split(":", 1)
        if host_literal != "127.0.0.1":
            raise ValueError("only literal '127.0.0.1' or '[::1]' loopback host is allowed")
    if not port_str.isdigit():
        raise ValueError("port must be an explicit integer 1..65535")
    port = int(port_str)
    if not (1 <= port <= 65535) or str(port) != port_str:
        raise ValueError("port must be canonical integer in 1..65535")
    return url


class StdioMcpServerConfig(_StrictExtModel):
    """Configured local stdio MCP server launched directly without shell interpolation."""

    id: ServerIdStr
    transport: Literal["stdio"]
    executable: StrictStr
    argv: list[StrictStr] = Field(default_factory=list)
    cwd: StrictStr
    executable_sha256: StrictStr = ""

    @field_validator("argv")
    @classmethod
    def _validate_argv(cls, value: list[str]) -> list[str]:
        if len(value) > MAX_MCP_ARGV_ITEMS:
            raise ValueError(f"argv exceeds {MAX_MCP_ARGV_ITEMS} items")
        total = 0
        for item in value:
            if "\x00" in item:
                raise ValueError("argv must not contain NUL bytes")
            total += len(item.encode("utf-8"))
        if total > MAX_MCP_ARGV_BYTES:
            raise ValueError(f"argv total size exceeds {MAX_MCP_ARGV_BYTES} bytes")
        return value

    @model_validator(mode="after")
    def _validate_paths_and_digest(self) -> StdioMcpServerConfig:
        real_exe = _validate_absolute_local_path(self.executable, label="executable", expect_dir=False)
        real_cwd = _validate_absolute_local_path(self.cwd, label="cwd", expect_dir=True)
        exe_sha = compute_file_sha256(real_exe)
        if self.executable_sha256 and self.executable_sha256 != exe_sha:
            raise ValueError("executable_sha256 does not match binary on disk")
        object.__setattr__(self, "executable", str(real_exe))
        object.__setattr__(self, "cwd", str(real_cwd))
        object.__setattr__(self, "executable_sha256", exe_sha)
        return self


class HttpMcpServerConfig(_StrictExtModel):
    """Configured literal-loopback Streamable HTTP MCP server."""

    id: ServerIdStr
    transport: Literal["http"]
    url: StrictStr

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        return validate_loopback_http_url(value)


McpServerConfig = Annotated[
    StdioMcpServerConfig | HttpMcpServerConfig,
    Field(discriminator="transport"),
]


class ExtensionConfig(_StrictExtModel):
    """Validated bounded extension configuration loaded from CLI path or empty default."""

    version: Literal[1] = 1
    servers: list[McpServerConfig] = Field(default_factory=list)
    path: StrictStr | None = None
    config_sha256: StrictStr = ""

    @field_validator("servers")
    @classmethod
    def _validate_servers(cls, value: list[McpServerConfig]) -> list[McpServerConfig]:
        if len(value) > MAX_MCP_SERVERS:
            raise ValueError(f"servers count exceeds max {MAX_MCP_SERVERS}")
        seen: set[str] = set()
        for srv in value:
            if srv.id in seen:
                raise ValueError(f"duplicate server id: {srv.id}")
            seen.add(srv.id)
        return value

    @model_validator(mode="after")
    def _ensure_digest(self) -> ExtensionConfig:
        if not self.config_sha256:
            canonical = sha256_canonical_json(
                {
                    "version": self.version,
                    "servers": [s.model_dump(mode="json") for s in self.servers],
                }
            )
            object.__setattr__(self, "config_sha256", canonical)
        elif not _SHA256_RE.fullmatch(self.config_sha256):
            raise ValueError("config_sha256 must be 64 lowercase hex chars")
        return self

    def get_server(self, server_id: str) -> McpServerConfig | None:
        for srv in self.servers:
            if srv.id == server_id:
                return srv
        return None


def load_extension_config(*, path: Path | None) -> ExtensionConfig:
    """Explicitly load and validate bounded UTF-8 JSON MCP server configuration."""
    if path is None:
        return ExtensionConfig(version=1, servers=[], path=None)
    if not isinstance(path, Path):
        raise ExtensionError("INVALID_EXTENSION_CONFIG", "path must be Path | None", status_code=400)
    real_path = _validate_absolute_local_path(str(path), label="extension_config", expect_dir=False)
    try:
        st = real_path.stat(follow_symlinks=False)
        if st.st_size > MAX_EXTENSION_CONFIG_BYTES:
            raise ExtensionError(
                "INVALID_EXTENSION_CONFIG",
                f"extension_config exceeds {MAX_EXTENSION_CONFIG_BYTES} bytes",
                status_code=400,
            )
        raw = real_path.read_bytes()
    except ExtensionError:
        raise
    except OSError as exc:
        raise ExtensionError(
            "INVALID_EXTENSION_CONFIG", f"cannot read extension_config: {exc}", status_code=400
        ) from exc
    file_sha = hashlib.sha256(raw).hexdigest()
    try:
        parsed = parse_strict_json_bytes(raw, limit_bytes=MAX_EXTENSION_CONFIG_BYTES, label="extension_config")
        if not isinstance(parsed, dict):
            raise TypeError("extension_config root must be a JSON object")
        validate_no_nonfinite_numbers(parsed)
        return ExtensionConfig.model_validate(
            {
                **parsed,
                "path": str(real_path),
                "config_sha256": file_sha,
            }
        )
    except ExtensionError:
        raise
    except Exception as exc:
        raise ExtensionError("INVALID_EXTENSION_CONFIG", str(exc), status_code=400) from exc


class SkillInfo(_StrictExtModel):
    """Discovered local Skill metadata without loading full file content."""

    id: SkillIdStr
    path: StrictStr
    sha256: StrictStr
    total_bytes: NonNegativeStrictInt


class SkillPage(_StrictExtModel):
    """Bounded UTF-8 byte page of a local Skill markdown or relative reference file."""

    id: SkillIdStr
    path: StrictStr
    sha256: StrictStr
    total_bytes: NonNegativeStrictInt
    content: StrictStr
    byte_offset: NonNegativeStrictInt
    next_offset: NonNegativeStrictInt
    eof: StrictBool


class SelectedSkillContext(_StrictExtModel):
    """Verbatim selected SKILL.md context carried into initial planning observation."""

    id: SkillIdStr
    path: StrictStr
    sha256: StrictStr
    total_bytes: NonNegativeStrictInt
    content: StrictStr


class McpToolInfo(_StrictExtModel):
    """Normalized MCP tool descriptor with bounded schema digest and executability flag."""

    server_id: ServerIdStr
    name: StrictStr
    title: StrictStr = ""
    description: StrictStr = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    schema_sha256: StrictStr
    supported: StrictBool = True
    unsupported_reason: StrictStr | None = None
    annotations: dict[str, Any] = Field(default_factory=dict)


class McpServerSummary(_StrictExtModel):
    """Sanitized MCP server summary without spawning processes or exposing secrets."""

    id: ServerIdStr
    transport: Literal["stdio", "http"]
    executable: StrictStr | None = None
    argv: list[StrictStr] | None = None
    cwd: StrictStr | None = None
    url: StrictStr | None = None
    config_sha256: StrictStr
    executable_sha256: StrictStr | None = None


class McpServerView(_StrictExtModel):
    """Live or sanitized status view of one MCP server inside a run."""

    id: ServerIdStr
    transport: Literal["stdio", "http"]
    executable: StrictStr | None = None
    argv: list[StrictStr] | None = None
    cwd: StrictStr | None = None
    url: StrictStr | None = None
    config_sha256: StrictStr
    executable_sha256: StrictStr | None = None
    state: McpServerState = "not_connected"
    protocol_version: StrictStr | None = None
    server_name: StrictStr | None = None
    server_version: StrictStr | None = None
    tools_count: NonNegativeStrictInt = 0
    tools_listed: StrictBool = False
    list_changed_stale: StrictBool = False
    session_id_present: StrictBool = False
    last_error: StrictStr | None = None
    stderr_excerpt: StrictStr = ""


class McpToolResult(_StrictExtModel):
    """Bounded structured result of one MCP ``tools/call`` invocation."""

    call_id: StrictStr
    action_id: StrictStr = ""
    server_id: ServerIdStr
    tool_name: StrictStr
    args_sha256: StrictStr
    schema_sha256: StrictStr
    status: McpCallStatus
    is_error: StrictBool
    effect: McpEffectStatus
    text: StrictStr = ""
    structured_content: Any = None
    content_items: list[dict[str, Any]] = Field(default_factory=list)
    truncated: StrictBool = False
    original_bytes: NonNegativeStrictInt = 0
    sha256: StrictStr
    error_code: StrictStr | None = None
    error_message: StrictStr | None = None


class ExtensionProposalView(_StrictExtModel):
    """Immutable run-bound approval request for MCP connect or single tool call."""

    id: StrictStr
    kind: ExtensionProposalKind
    project_id: StrictStr
    session_id: StrictStr
    run_id: StrictStr
    action_id: StrictStr
    server_id: ServerIdStr
    tool_name: StrictStr | None = None
    transport: Literal["stdio", "http"]
    executable: StrictStr | None = None
    argv: list[StrictStr] | None = None
    cwd: StrictStr | None = None
    url: StrictStr | None = None
    config_sha256: StrictStr
    executable_sha256: StrictStr | None = None
    schema_sha256: StrictStr | None = None
    args_sha256: StrictStr | None = None
    arguments: dict[str, Any] | None = None
    digest: StrictStr
    state: ExtensionProposalState
    trusted_local_execution: StrictBool = True
    summary: StrictStr = ""
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None


class ExtensionProposalDecision(_StrictExtModel):
    """HTTP / service payload for approving or rejecting an extension proposal."""

    digest: StrictStr
    decision: Literal["approve", "reject"]

    @field_validator("digest")
    @classmethod
    def _validate_digest(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("digest must be 64 lowercase hexadecimal characters")
        return value


class ExtensionCleanup(_StrictExtModel):
    """Explicit teardown verification report for MCP processes, sockets, and tasks."""

    cleanup_ok: StrictBool
    remaining_active: NonNegativeStrictInt
    details: dict[str, Any] = Field(default_factory=dict)


class RunExtensionsView(_StrictExtModel):
    """Bounded read-only snapshot of extensions within one run."""

    project_id: StrictStr
    session_id: StrictStr
    run_id: StrictStr
    selected_skills: list[SkillInfo] = Field(default_factory=list)
    servers: list[McpServerView] = Field(default_factory=list)
    proposals: list[ExtensionProposalView] = Field(default_factory=list)
    calls: list[McpToolResult] = Field(default_factory=list)
    total_call_bytes: NonNegativeStrictInt = 0
    total_calls: NonNegativeStrictInt = 0


def sanitize_server_summary(
    server: McpServerConfig, *, config_sha256: str
) -> McpServerSummary:
    """Return a sanitized server summary suitable for UI/API listing without spawning."""
    if isinstance(server, StdioMcpServerConfig):
        return McpServerSummary(
            id=server.id,
            transport="stdio",
            executable=server.executable,
            argv=list(server.argv),
            cwd=server.cwd,
            url=None,
            config_sha256=config_sha256,
            executable_sha256=server.executable_sha256,
        )
    return McpServerSummary(
        id=server.id,
        transport="http",
        executable=None,
        argv=None,
        cwd=None,
        url=server.url,
        config_sha256=config_sha256,
        executable_sha256=None,
    )


def _assert_config_identity(*, config: ExtensionConfig) -> None:
    """Verify that a file-backed ExtensionConfig still matches its startup canonical path and SHA-256."""
    if config.path is None:
        return
    try:
        real_path = _validate_absolute_local_path(
            config.path, label="extension_config", expect_dir=False
        )
        if os.path.normcase(str(real_path)) != os.path.normcase(str(config.path)):
            raise ExtensionError(
                "CONFIG_MODIFIED",
                "extension_config canonical path changed since load",
                status_code=409,
            )
        if not real_path.exists() or not real_path.is_file():
            raise ExtensionError(
                "CONFIG_MODIFIED",
                f"extension_config is missing or not a regular file: {config.path}",
                status_code=409,
            )
        st = real_path.stat()
        if st.st_size > MAX_EXTENSION_CONFIG_BYTES:
            raise ExtensionError(
                "CONFIG_MODIFIED",
                f"extension_config exceeds {MAX_EXTENSION_CONFIG_BYTES} bytes",
                status_code=409,
            )
        current_sha = compute_file_sha256(real_path)
    except ExtensionError as exc:
        if exc.code == "CONFIG_MODIFIED":
            raise
        raise ExtensionError(
            "CONFIG_MODIFIED",
            f"extension_config identity check failed: {exc.message}",
            status_code=409,
        ) from exc
    except OSError as exc:
        raise ExtensionError(
            "CONFIG_MODIFIED",
            f"cannot read extension_config: {exc}",
            status_code=409,
        ) from exc
    if current_sha != config.config_sha256:
        raise ExtensionError(
            "CONFIG_MODIFIED",
            "extension_config file SHA-256 changed since startup/reload",
            status_code=409,
        )

