"""Strict Pydantic models, limits, actions, proposals, commands, and errors for APP-T02."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    StringConstraints,
    field_validator,
    model_validator,
)

from forecontract_app.models import (
    NonNegativeStrictInt,
    PositiveStrictInt,
)


def _check_non_blank_str(value: str) -> str:
    if not value.strip() or "\x00" in value:
        raise ValueError("must be non-blank and NUL-free")
    return value


NonBlankStr = Annotated[StrictStr, AfterValidator(_check_non_blank_str)]
ProjectNameStr = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-z][a-z0-9-]{0,31}$")]
Sha256HexStr = Annotated[str, StringConstraints(strict=True, pattern=r"^[0-9a-f]{64}$")]

PROJECT_SCHEMA_VERSION: Final[str] = "0.1"

PROJECT_MAX_READ_CHUNK_BYTES: Final[int] = 16 * 1024
PROJECT_MAX_READ_FILE_BYTES: Final[int] = 8 * 1024 * 1024
PROJECT_MAX_WRITE_FILE_BYTES: Final[int] = 64 * 1024
PROJECT_MAX_OBSERVATION_BYTES: Final[int] = 64 * 1024
PROJECT_MAX_INSTRUCTIONS_BYTES: Final[int] = 16 * 1024
PROJECT_MAX_TOTAL_INSTRUCTIONS_BYTES: Final[int] = 64 * 1024
PROJECT_MAX_COMMAND_OUTPUT_BYTES: Final[int] = 1024 * 1024
PROJECT_MAX_COMMAND_PAGE_BYTES: Final[int] = 16 * 1024
PROJECT_MAX_COMMAND_CHUNKS: Final[int] = 2048
PROJECT_MAX_SCAN_FILES: Final[int] = 10_000
PROJECT_MAX_SCAN_TOTAL_BYTES: Final[int] = 64 * 1024 * 1024
PROJECT_MAX_SEARCH_BATCH_FILES: Final[int] = 200
PROJECT_MAX_SEARCH_BATCH_BYTES: Final[int] = 4 * 1024 * 1024
PROJECT_MAX_SEARCH_MATCHES: Final[int] = 50
PROJECT_MAX_SEARCH_TEXT_BYTES: Final[int] = 16 * 1024
PROJECT_MAX_LIST_LIMIT: Final[int] = 100
PROJECT_MAX_COMMAND_ARGS: Final[int] = 128
PROJECT_MAX_COMMAND_ARG_BYTES: Final[int] = 8192
PROJECT_MAX_COMMAND_ARGV_TOTAL_BYTES: Final[int] = 32 * 1024
PROJECT_MAX_COMMAND_EXPLANATION_BYTES: Final[int] = 2048
PROJECT_MAX_COMMAND_TIMEOUT_MS: Final[int] = 300_000
PROJECT_PROPOSAL_TTL_SECONDS: Final[float] = 120.0

DEFAULT_EXCLUDED_DIR_NAMES: Final[frozenset[str]] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        "credentials",
        "secrets",
    }
)

SUPPORTED_PROJECT_ACTIONS: Final[tuple[str, ...]] = (
    "list_directory",
    "find_paths",
    "read_file",
    "search_text",
    "propose_scope",
    "propose_verification",
    "create_file",
    "edit_file",
    "propose_command",
    "start_command",
    "poll_command",
    "stop_command",
    "verify_project",
)

ProposalKind = Literal["scope", "command", "verification"]
ProposalState = Literal["pending", "approved", "rejected", "expired", "revoked"]
CommandState = Literal["starting", "running", "exited", "cancelled", "timed_out", "unknown"]
VerificationKind = Literal["junit_exact", "exit_code"]
CheckIdStr = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Za-z0-9._-]{1,64}$")]
_BRACE_RE = re.compile(r"\{[^{}]*\}")


def validate_command_argv_list(argv: list[str]) -> list[str]:
    """Validate bounded non-shell command argument list allowing only ``{result_dir}``."""
    if len(argv) > PROJECT_MAX_COMMAND_ARGS:
        raise ValueError(f"argv exceeds max {PROJECT_MAX_COMMAND_ARGS} arguments")
    total_bytes = 0
    for item in argv:
        if not isinstance(item, str):
            raise TypeError("argv entries must be strict strings")
        if "\x00" in item:
            raise ValueError("argv entries must not contain NUL bytes")
        raw = item.encode("utf-8")
        if len(raw) > PROJECT_MAX_COMMAND_ARG_BYTES:
            raise ValueError(f"single argv entry exceeds {PROJECT_MAX_COMMAND_ARG_BYTES} bytes")
        total_bytes += len(raw)
        if total_bytes > PROJECT_MAX_COMMAND_ARGV_TOTAL_BYTES:
            raise ValueError(f"total argv bytes exceed {PROJECT_MAX_COMMAND_ARGV_TOTAL_BYTES} bytes")
        for match in _BRACE_RE.finditer(item):
            if match.group(0) != "{result_dir}":
                raise ValueError("only {result_dir} brace placeholder is allowed")
        if "{result_dir}" in item:
            after = item.split("{result_dir}", 1)[1]
            if ".." in after.replace("\\", "/").split("/"):
                raise ValueError("{result_dir} suffix must not contain '..'")
    return argv


class ProjectError(Exception):
    """Structured error raised by existing-project workspace, permissions, and runner."""

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class _StrictProjectModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class RegisterProjectRequest(_StrictProjectModel):
    name: ProjectNameStr
    root: Annotated[str, StringConstraints(strict=True, min_length=1, max_length=2048)]
    confirmed_read: StrictBool

    @field_validator("root")
    @classmethod
    def _reject_blank_root(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("root must be non-blank and NUL-free")
        return value


class ExpectedTestCaseView(_StrictProjectModel):
    classname: NonBlankStr
    name: NonBlankStr


class ScopeProposalPayload(_StrictProjectModel):
    exact_paths: Annotated[list[NonBlankStr], Field(strict=True)] = Field(default_factory=list)
    write_prefixes: Annotated[list[NonBlankStr], Field(strict=True)] = Field(default_factory=list)
    operations: Annotated[list[Literal["create", "modify"]], Field(strict=True, min_length=1)]
    protected_paths: Annotated[list[NonBlankStr], Field(strict=True)] = Field(default_factory=list)
    verification_plan_summary: Annotated[
        str, StringConstraints(strict=True, min_length=1, max_length=1024)
    ]

    @model_validator(mode="after")
    def _check_non_empty_targets(self) -> ScopeProposalPayload:
        if not self.exact_paths and not self.write_prefixes:
            raise ValueError("scope proposal must declare at least one exact_path or write_prefix")
        return self


class VerificationCheck(_StrictProjectModel):
    check_id: CheckIdStr
    kind: VerificationKind
    executable: NonBlankStr
    argv: Annotated[list[StrictStr], Field(strict=True)] = Field(default_factory=list)
    cwd: NonBlankStr = "."
    timeout_ms: Annotated[int, StrictInt, Field(ge=1, le=PROJECT_MAX_COMMAND_TIMEOUT_MS)]
    protected_paths: Annotated[list[NonBlankStr], Field(strict=True)] = Field(default_factory=list)
    expected_tests: Annotated[list[ExpectedTestCaseView], Field(strict=True)] = Field(
        default_factory=list
    )

    @field_validator("argv")
    @classmethod
    def _check_argv(cls, value: list[str]) -> list[str]:
        return validate_command_argv_list(value)

    @model_validator(mode="after")
    def _check_kind_requirements(self) -> VerificationCheck:
        if self.kind == "junit_exact":
            if not self.expected_tests:
                raise ValueError("junit_exact check must declare non-empty expected_tests")
            seen: set[tuple[str, str]] = set()
            for tc in self.expected_tests:
                key = (tc.classname, tc.name)
                if key in seen:
                    raise ValueError("junit_exact expected_tests must be unique")
                seen.add(key)
            if not any("{result_dir}" in item for item in self.argv):
                raise ValueError("junit_exact check argv must include {result_dir}")
        else:
            if self.expected_tests:
                raise ValueError("exit_code check must have empty expected_tests")
        return self


class VerificationPlan(_StrictProjectModel):
    plan_id: NonBlankStr | None = None
    summary: Annotated[str, StringConstraints(strict=True, max_length=1024)] = ""
    checks: Annotated[list[VerificationCheck], Field(strict=True, min_length=1)]

    @model_validator(mode="after")
    def _check_unique_ids(self) -> VerificationPlan:
        seen: set[str] = set()
        for chk in self.checks:
            if chk.check_id in seen:
                raise ValueError(f"duplicate check_id '{chk.check_id}' in VerificationPlan")
            seen.add(chk.check_id)
        return self


class CommandProposal(_StrictProjectModel):
    command_id: CheckIdStr | None = None
    executable: NonBlankStr
    argv: Annotated[list[StrictStr], Field(strict=True)] = Field(default_factory=list)
    cwd: NonBlankStr = "."
    timeout_ms: Annotated[int, StrictInt, Field(ge=1, le=PROJECT_MAX_COMMAND_TIMEOUT_MS)]
    purpose: Literal["diagnostic", "verification"]
    explanation: Annotated[str, StringConstraints(strict=True, min_length=1)]
    executable_sha256: Sha256HexStr | None = None
    check_id: CheckIdStr | None = None

    @field_validator("argv")
    @classmethod
    def _check_argv(cls, value: list[str]) -> list[str]:
        return validate_command_argv_list(value)

    @field_validator("explanation")
    @classmethod
    def _check_explanation(cls, value: str) -> str:
        if not value.strip() or "\x00" in value:
            raise ValueError("explanation must be non-blank and NUL-free")
        if len(value.encode("utf-8")) > PROJECT_MAX_COMMAND_EXPLANATION_BYTES:
            raise ValueError(
                f"explanation exceeds {PROJECT_MAX_COMMAND_EXPLANATION_BYTES} UTF-8 bytes"
            )
        return value


class ProposalDecision(_StrictProjectModel):
    digest: Sha256HexStr
    decision: Literal["approve", "reject"]
    trusted_local_execution: StrictBool = False


class ProposalView(_StrictProjectModel):
    id: NonBlankStr
    kind: ProposalKind
    project_id: NonBlankStr
    session_id: NonBlankStr
    run_id: NonBlankStr
    digest: Sha256HexStr
    payload: dict[str, Any] = Field(default_factory=dict)
    state: ProposalState
    expires_at: Annotated[float, StrictFloat | StrictInt]
    trusted_local_execution: StrictBool = False


class CommandChunk(_StrictProjectModel):
    seq: NonNegativeStrictInt
    stream: Literal["stdout", "stderr"]
    text: StrictStr


class CommandView(_StrictProjectModel):
    command_id: NonBlankStr
    run_id: NonBlankStr
    proposal_digest: Sha256HexStr
    state: CommandState
    pid: StrictInt | None = None
    exit_code: StrictInt | None = None
    cleanup_ok: StrictBool = True
    remaining_active: NonNegativeStrictInt = 0
    first_seq: NonNegativeStrictInt = 0
    last_seq: NonNegativeStrictInt = 0
    chunks: list[CommandChunk] = Field(default_factory=list)
    bytes_seen: NonNegativeStrictInt = 0
    truncated: StrictBool = False
    output_delivery_failed: StrictBool = False
    truncated_stdout_bytes: NonNegativeStrictInt = 0
    truncated_stderr_bytes: NonNegativeStrictInt = 0
    timed_out: StrictBool = False


class StopCommandRequest(_StrictProjectModel):
    pass


class DirectoryEntry(_StrictProjectModel):
    name: NonBlankStr
    path: NonBlankStr
    kind: Literal["dir", "file"]
    size_bytes: NonNegativeStrictInt = 0


class ListDirectoryResult(_StrictProjectModel):
    directory: NonBlankStr
    entries: list[DirectoryEntry] = Field(default_factory=list)
    truncated: StrictBool = False
    next_cursor: StrictStr | None = None
    scanned: NonNegativeStrictInt = 0
    excluded: NonNegativeStrictInt = 0


class FindPathsResult(_StrictProjectModel):
    pattern: NonBlankStr
    paths: list[NonBlankStr] = Field(default_factory=list)
    scanned: NonNegativeStrictInt = 0
    excluded: NonNegativeStrictInt = 0
    truncated: StrictBool = False
    next_cursor: StrictStr | None = None


class SearchMatchEntry(_StrictProjectModel):
    path: NonBlankStr
    line_number: PositiveStrictInt
    line_text: StrictStr


class SearchTextResult(_StrictProjectModel):
    query: NonBlankStr
    matches: list[SearchMatchEntry] = Field(default_factory=list)
    scanned: NonNegativeStrictInt = 0
    excluded: NonNegativeStrictInt = 0
    truncated: StrictBool = False
    next_cursor: StrictStr | None = None


class ReadFileResult(_StrictProjectModel):
    path: NonBlankStr
    sha256: Sha256HexStr
    content: StrictStr
    start_line: PositiveStrictInt
    next_start_line: PositiveStrictInt | None = None
    byte_offset: NonNegativeStrictInt = 0
    next_byte_offset: NonNegativeStrictInt | None = None
    line_count: NonNegativeStrictInt = 0
    total_bytes: NonNegativeStrictInt = 0
    truncated: StrictBool = False
    eof: StrictBool = False


class InstructionFileInfo(_StrictProjectModel):
    path: NonBlankStr
    sha256: Sha256HexStr
    scope: NonBlankStr
    size_bytes: NonNegativeStrictInt


class ProjectInstructions(_StrictProjectModel):
    present: StrictBool
    files: list[InstructionFileInfo] = Field(default_factory=list)
    content: StrictStr = ""
    sha256: StrictStr | None = None
    total_bytes: NonNegativeStrictInt = 0


class ProjectWriteOutcome(_StrictProjectModel):
    path: NonBlankStr
    operation: Literal["create", "modify"]
    before_sha256: Sha256HexStr | None = None
    written_sha256: Sha256HexStr
    read_back_sha256: Sha256HexStr
    size_bytes: NonNegativeStrictInt


class WorkspaceRevision(_StrictProjectModel):
    revision: NonBlankStr
    file_count: NonNegativeStrictInt
    total_bytes: NonNegativeStrictInt
    scope_complete: StrictBool = True
    excluded_note: NonBlankStr
    inventory: dict[str, str] = Field(default_factory=dict)


class DiffEntry(_StrictProjectModel):
    path: NonBlankStr
    operation: Literal["create", "modify", "delete"]
    before_sha256: Sha256HexStr | None = None
    after_sha256: Sha256HexStr | None = None
    source: Literal["ai", "external_or_command"] = "ai"
    diff_excerpt: StrictStr = ""


class WorkspaceDiff(_StrictProjectModel):
    baseline_revision: NonBlankStr
    final_revision: NonBlankStr
    scope_complete: StrictBool = True
    excluded_note: NonBlankStr
    entries: list[DiffEntry] = Field(default_factory=list)
    diff_truncated: StrictBool = False


class CheckExecutionRecord(_StrictProjectModel):
    check_id: NonBlankStr
    kind: VerificationKind
    command_id: NonBlankStr
    proposal_id: NonBlankStr
    proposal_digest: Sha256HexStr
    executable: NonBlankStr
    executable_sha256: Sha256HexStr
    argv: list[StrictStr] = Field(default_factory=list)
    cwd: NonBlankStr
    result_dir: str | None = None
    junit_xml_path: str | None = None
    exit_code: StrictInt | None = None
    timed_out: StrictBool = False
    cancelled: StrictBool = False
    cleanup_ok: StrictBool = True
    remaining_active: NonNegativeStrictInt = 0
    output_bytes_seen: NonNegativeStrictInt = 0
    output_delivery_failed: StrictBool = False
    start_revision: NonBlankStr
    end_revision: NonBlankStr
    protected_shas_before: dict[str, str] = Field(default_factory=dict)
    protected_shas_after: dict[str, str] = Field(default_factory=dict)
    expected_tests: list[ExpectedTestCaseView] = Field(default_factory=list)
    observed_tests: list[ExpectedTestCaseView] = Field(default_factory=list)
    junit_valid: StrictBool = False
    passed: StrictBool = False
    failure_reason: str | None = None


class ProjectVerificationEvidence(_StrictProjectModel):
    baseline_revision: NonBlankStr
    final_revision: NonBlankStr
    scope_complete: StrictBool = True
    core_completed: StrictBool = True
    plan_id: NonBlankStr | None = None
    plan_digest: Sha256HexStr | None = None
    approved_plan: VerificationPlan | None = None
    protected_shas_expected: dict[str, str] = Field(default_factory=dict)
    protected_shas_final: dict[str, str] = Field(default_factory=dict)
    checks: list[CheckExecutionRecord] = Field(default_factory=list)
    changed_paths: list[dict[str, Any]] = Field(default_factory=list)
    diff: WorkspaceDiff | None = None
    diff_truncated: StrictBool = False
    cleanup_ok: StrictBool = True
    remaining_active: NonNegativeStrictInt = 0
    unverified_reasons: list[str] = Field(default_factory=list)
    stale_after_verification: StrictBool = False


class ProjectTask(_StrictProjectModel):
    run_id: NonBlankStr
    goal_id: NonBlankStr
    goal_text: NonBlankStr
    project_id: NonBlankStr
    root_identity: NonBlankStr
    read_grant_id: NonBlankStr
    data_root: Path
    output_root: Path
    python_executable: Path
    session_id: NonBlankStr = "default-session"
    max_steps: PositiveStrictInt = 120
    max_planning_calls: PositiveStrictInt = 64
    max_replans: NonNegativeStrictInt = 32
    run_timeout_ms: PositiveStrictInt = 600_000
    proposal_ttl_seconds: Annotated[float, StrictFloat | StrictInt, Field(gt=0.0, le=300.0)] = (
        PROJECT_PROPOSAL_TTL_SECONDS
    )


class ProjectRunResult(_StrictProjectModel):
    terminal_status: Literal["succeeded", "failed", "cancelled", "unknown"]
    verification: Literal["not_run", "passed", "failed", "unknown"]
    reason_code: NonBlankStr
    scope_complete: StrictBool = True
    plan_id: str | None = None
    plan_digest: str | None = None
    baseline_revision: NonBlankStr
    final_revision: NonBlankStr
    checks: dict[str, CheckExecutionRecord] = Field(default_factory=dict)
    changed_paths: list[dict[str, Any]] = Field(default_factory=list)
    diff: WorkspaceDiff | None = None
    diff_truncated: StrictBool = False
    unverified_reasons: list[str] = Field(default_factory=list)
    cleanup: dict[str, Any] = Field(default_factory=dict)
    core_terminal_status: NonBlankStr
    core_terminal_reason: NonBlankStr
    steps_executed: NonNegativeStrictInt = 0
    planning_calls_used: NonNegativeStrictInt = 0
    acceptance: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _check_mapping_keys(self) -> ProjectRunResult:
        for key, rec in self.checks.items():
            if key != rec.check_id:
                raise ValueError(f"checks key '{key}' does not match check_id '{rec.check_id}'")
        return self
