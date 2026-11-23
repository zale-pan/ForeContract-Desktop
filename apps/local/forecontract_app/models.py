"""Strict Pydantic models, dataclasses, and closed vocabularies for the local app."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Final, Literal, Protocol

from forecontract_llm.provider import ModelProvider
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION: Final[str] = "1"
MAX_GOAL_STRIPPED_LEN: Final[int] = 4096
MAX_EVENT_JSON_BYTES: Final[int] = 16 * 1024
MAX_REQUEST_BODY_BYTES: Final[int] = 64 * 1024
PROJECT_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9-]{0,31}$")

_STRICT_MODEL_CONFIG = ConfigDict(extra="forbid", strict=True, frozen=True)

StrictStr = Annotated[str, Field(strict=True)]
StrictBool = Annotated[bool, Field(strict=True)]
StrictInt = Annotated[int, Field(strict=True)]
PositiveStrictInt = Annotated[int, Field(strict=True, ge=1)]
NonNegativeStrictInt = Annotated[int, Field(strict=True, ge=0)]

RunState = Literal[
    "queued",
    "running",
    "cancelling",
    "succeeded",
    "failed",
    "cancelled",
    "unknown",
]
VerificationState = Literal["not_run", "passed", "failed", "unknown"]
AppEventKind = Literal[
    "run_queued",
    "run_started",
    "planning_started",
    "tool_result",
    "core_trace",
    "proposal_requested",
    "proposal_decided",
    "command_started",
    "command_output",
    "command_finished",
    "question_requested",
    "question_answered",
    "question_closed",
    "run_finished",
]
RunTrigger = Callable[[], Awaitable[None]]
ProjectKind = Literal["controlled", "existing"]


class _StrictAppModel(BaseModel):
    model_config = _STRICT_MODEL_CONFIG


@dataclass(frozen=True)
class LiveConfig:
    """Host-owned live provider configuration; credentials never leave the host."""

    api_key: str = field(repr=False)
    model: str
    max_requests: int = 8
    budget_cny: float = 1.0
    price_input_cny_per_1m: float = 0.5
    price_output_cny_per_1m: float = 2.0
    timeout_seconds: float = 30.0
    max_request_bytes: int = 262144
    max_response_bytes: int = 1048576


@dataclass(frozen=True)
class HostConfig:
    """Host configuration bound to local roots and a local access token."""

    data_root: Path
    workspace_parent: Path
    python_executable: Path
    bind_port: int
    access_token: str = field(repr=False)
    max_sessions: int = 8
    max_runs_per_session: int = 16
    max_events_per_run: int = 512
    project_roots: tuple[Path, ...] = ()
    extension_config: Path | None = None
    live: LiveConfig | None = None

    def __repr__(self) -> str:
        return (
            "HostConfig("
            f"data_root={self.data_root!r}, "
            f"workspace_parent={self.workspace_parent!r}, "
            f"python_executable={self.python_executable!r}, "
            f"bind_port={self.bind_port!r}, "
            "access_token='<redacted>', "
            f"max_sessions={self.max_sessions!r}, "
            f"max_runs_per_session={self.max_runs_per_session!r}, "
            f"max_events_per_run={self.max_events_per_run!r}, "
            f"project_roots={self.project_roots!r}, "
            f"extension_config={self.extension_config!r}, "
            f"live={self.live is not None!r}"
            ")"
        )


class AppError(Exception):
    """Structured application error carrying a closed code and HTTP status code."""

    code: str
    status_code: int

    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        self.code = code
        self.status_code = status_code
        self.message = message
        super().__init__(message)


class ProviderFactory(Protocol):
    """Host-injected offline ModelProvider factory for one project run."""

    def __call__(self, *, project: ProjectView, run_id: str) -> ModelProvider: ...


class ModelSelection(_StrictAppModel):
    """Selected planner profile; ``deepseek`` saves candidate metadata without execution."""

    provider: Literal["offline", "deepseek"]
    model: Annotated[str, Field(strict=True, min_length=1, max_length=128)]
    thinking: Literal["disabled"] = "disabled"

    @field_validator("model")
    @classmethod
    def _validate_model_name(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("model must be non-empty without surrounding whitespace")
        if any(ord(ch) < 32 for ch in value):
            raise ValueError("model must not contain control characters")
        return value

    @model_validator(mode="after")
    def _validate_provider_model_pair(self) -> ModelSelection:
        if self.provider == "offline" and self.model not in (
            "offline-metrics-v1",
            "offline-project-v1",
        ):
            raise ValueError(
                "offline provider accepts only 'offline-metrics-v1' or 'offline-project-v1'"
            )
        return self


class CreateProjectRequest(_StrictAppModel):
    """HTTP payload for creating a controlled example project."""

    name: StrictStr

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not PROJECT_NAME_PATTERN.fullmatch(value):
            raise ValueError("project name must match ^[a-z][a-z0-9-]{0,31}$")
        return value


class CreateSessionRequest(_StrictAppModel):
    """HTTP payload for creating an in-memory session bound to one project."""

    project_id: StrictStr
    model: ModelSelection

    @field_validator("project_id")
    @classmethod
    def _validate_project_id(cls, value: str) -> str:
        try:
            parsed = uuid.UUID(value)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("project_id must be a valid UUID string") from exc
        if str(parsed) != value:
            raise ValueError("project_id must use canonical lowercase UUID format")
        return value


_SKILL_ID_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class StartRunRequest(_StrictAppModel):
    """HTTP and service payload to start a bounded Coding run."""

    client_request_id: uuid.UUID
    goal: StrictStr
    confirmed_scope: StrictBool
    skill_ids: list[StrictStr] = Field(default_factory=list)

    @field_validator("skill_ids")
    @classmethod
    def _validate_skill_ids(cls, value: list[str]) -> list[str]:
        if len(value) > 4:
            raise ValueError("skill_ids cannot exceed 4 items")
        seen: set[str] = set()
        for sid in value:
            if not _SKILL_ID_PATTERN.fullmatch(sid):
                raise ValueError(f"invalid skill_id: {sid!r}")
            if sid in seen:
                raise ValueError(f"duplicate skill_id: {sid!r}")
            seen.add(sid)
        return value

    @field_validator("client_request_id", mode="before")
    @classmethod
    def _parse_strict_uuid(cls, value: object) -> uuid.UUID:
        if isinstance(value, uuid.UUID):
            return value
        if isinstance(value, str):
            try:
                parsed = uuid.UUID(value)
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError("client_request_id must be a valid UUID") from exc
            if str(parsed) != value.lower():
                raise ValueError("client_request_id must be a standard hyphenated UUID")
            return parsed
        raise ValueError("client_request_id must be a UUID or UUID string")

    @field_validator("goal")
    @classmethod
    def _validate_goal(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("goal must not contain NUL bytes")
        stripped = value.strip()
        if not stripped or len(stripped) > MAX_GOAL_STRIPPED_LEN:
            raise ValueError("goal after strip must be 1..4096 characters")
        return value


class CancelRunRequest(_StrictAppModel):
    """Strict empty object body for run cancellation."""


class WriteTargetView(_StrictAppModel):
    """Public summary of one authorized write target."""

    path: StrictStr
    operation: Literal["create", "modify"]


class ExpectedTestView(_StrictAppModel):
    """Public identity of one expected JUnit acceptance testcase."""

    classname: StrictStr
    name: StrictStr


class CommandSummaryView(_StrictAppModel):
    """Public read-only summary of one host-fixed command."""

    command_id: StrictStr
    purpose: Literal["diagnostic", "acceptance"]
    timeout_ms: PositiveStrictInt
    expected_tests: list[ExpectedTestView]


class ProjectView(_StrictAppModel):
    """Public representation of a host-registered controlled or existing project."""

    project_id: StrictStr
    name: StrictStr
    workspace_root: StrictStr
    read_paths: list[StrictStr]
    write_paths: list[WriteTargetView]
    commands_summary: list[CommandSummaryView]
    expected_tests: list[ExpectedTestView]
    kind: ProjectKind = "controlled"
    read_grant_id: StrictStr | None = None


class SessionView(_StrictAppModel):
    """Public representation of a session bound to a project."""

    session_id: StrictStr
    project_id: StrictStr
    model: ModelSelection
    active_run_id: StrictStr | None = None
    constraints_revision: NonNegativeStrictInt = 0
    constraints: StrictStr = ""
    history_cursor: NonNegativeStrictInt = 0


class RunView(_StrictAppModel):
    """Public view of one Coding run and its real verification summary."""

    run_id: StrictStr
    session_id: StrictStr
    project_id: StrictStr
    client_request_id: StrictStr
    goal: StrictStr
    state: RunState
    verification: VerificationState
    reason_code: StrictStr | None = None
    last_seq: NonNegativeStrictInt = 0
    result: dict[str, Any] | None = None
    truncated_before: NonNegativeStrictInt = 0
    usage: dict[str, Any] | None = None


class AppEvent(_StrictAppModel):
    """Strict bounded event envelope streamed over SSE and returned in snapshots."""

    schema_version: Literal["1"] = SCHEMA_VERSION
    project_id: StrictStr
    session_id: StrictStr
    run_id: StrictStr
    seq: PositiveStrictInt
    kind: AppEventKind
    payload: dict[str, Any]

    @model_validator(mode="after")
    def _validate_size_and_json(self) -> AppEvent:
        raw = json.dumps(self.model_dump(mode="json"), ensure_ascii=False, allow_nan=False).encode(
            "utf-8"
        )
        if len(raw) > MAX_EVENT_JSON_BYTES:
            raise ValueError(f"serialized AppEvent exceeds {MAX_EVENT_JSON_BYTES} UTF-8 bytes")
        return self


class RunSnapshot(_StrictAppModel):
    """Detached snapshot of run view, retained bounded events, proposals, questions, and cursor."""

    run: RunView
    events: list[AppEvent]
    cursor: NonNegativeStrictInt
    truncated_before: NonNegativeStrictInt
    pending_proposals: list[dict[str, Any]] = Field(default_factory=list)
    pending_questions: list[dict[str, Any]] = Field(default_factory=list)
