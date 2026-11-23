"""Bounded request, response, tool-read, and title projection models for read-only chat turns."""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_CHAT_USER_TEXT_BYTES = 8 * 1024
MAX_CHAT_TOOL_CALLS_PER_TURN = 8
MAX_CHAT_CONTEXT_BYTES = 16 * 1024
MAX_CHAT_ASSISTANT_TEXT_BYTES = 8 * 1024
MAX_CHAT_TOOL_SUMMARY_BYTES = 1024
MAX_CHAT_TOOL_EXCERPT_BYTES = 3072
MAX_CHAT_SESSION_TITLE_BYTES = 96
MAX_CHAT_RECENT_TURNS_IN_CONTEXT = 6
MAX_CHAT_REFERENCES_PER_TURN = 4
MAX_CHAT_EVIDENCE_EXCERPT_BYTES = 1536
MAX_CHAT_OFFER_GOAL_BYTES = 2000
MAX_CHAT_OFFER_SUMMARY_BYTES = 2000

READONLY_CHAT_TOOLS: frozenset[str] = frozenset(
    {"list_directory", "find_paths", "read_file", "search_text"}
)

ChatTurnStatus = Literal["running", "completed", "failed", "interrupted"]
ChatToolName = Literal["list_directory", "find_paths", "read_file", "search_text"]
ChatIntentMode = Literal["readonly", "conversation"]
ChatExecutionOfferStatus = Literal["pending_confirm", "started"]
SessionTitleSource = Literal["chat_turn", "history_goal", "session_id"]
EvidenceAvailability = Literal["available", "raw_output_unavailable"]


class _StrictChatModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


def _validate_uuid_value(value: object, *, label: str) -> uuid.UUID:
    if isinstance(value, bool):
        raise TypeError(f"{label} must be a UUID")
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            return uuid.UUID(value)
        except ValueError as exc:
            raise ValueError(f"{label} must be a valid UUID") from exc
    raise ValueError(f"{label} must be a UUID")


def _validate_non_bool_int(value: object, *, label: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _validate_sha256_or_none(value: object, *, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError(f"{label} must be 64 lowercase hex chars or null")
    return value


class DiffHunkReference(_StrictChatModel):
    """Structured reference to one historical diff hunk in a prior run."""

    kind: Literal["diff_hunk"]
    run_id: str
    path: str
    before_sha256: str | None = None
    after_sha256: str | None = None
    old_start: int | None = None
    old_count: int | None = None
    new_start: int | None = None
    new_count: int | None = None

    @field_validator("run_id")
    @classmethod
    def _check_run_id(cls, value: str) -> str:
        return str(_validate_uuid_value(value, label="run_id"))

    @field_validator("path")
    @classmethod
    def _check_path(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ValueError("path must be non-blank")
        return value.strip()

    @field_validator("before_sha256", "after_sha256")
    @classmethod
    def _check_shas(cls, value: str | None) -> str | None:
        return _validate_sha256_or_none(value, label="sha256")

    @field_validator("old_start", "new_start")
    @classmethod
    def _check_starts(cls, value: int | None) -> int | None:
        if value is None:
            return None
        return _validate_non_bool_int(value, label="line_start", minimum=0)

    @field_validator("old_count", "new_count")
    @classmethod
    def _check_counts(cls, value: int | None) -> int | None:
        if value is None:
            return None
        return _validate_non_bool_int(value, label="line_count", minimum=0)

    @model_validator(mode="after")
    def _check_sides(self) -> DiffHunkReference:
        if self.before_sha256 is None and self.after_sha256 is None:
            raise ValueError("diff_hunk must include before_sha256 or after_sha256")
        if self.before_sha256 is None and (self.old_start not in (None, 0) or self.old_count not in (None, 0)):
            raise ValueError("created file diff_hunk must not specify old-side range")
        if self.after_sha256 is None and (self.new_start not in (None, 0) or self.new_count not in (None, 0)):
            raise ValueError("deleted file diff_hunk must not specify new-side range")
        has_old = self.before_sha256 is not None and self.old_start is not None and self.old_start >= 1
        has_new = self.after_sha256 is not None and self.new_start is not None and self.new_start >= 1
        if not has_old and not has_new:
            raise ValueError("diff_hunk must specify at least one valid hunk range")
        return self


class CommandReference(_StrictChatModel):
    """Structured reference to one command execution in a prior run."""

    kind: Literal["command"]
    run_id: str
    command_id: str

    @field_validator("run_id")
    @classmethod
    def _check_run_id(cls, value: str) -> str:
        return str(_validate_uuid_value(value, label="run_id"))

    @field_validator("command_id")
    @classmethod
    def _check_command_id(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip() or "\x00" in value:
            raise ValueError("command_id must be non-blank")
        return value.strip()


ChatReferenceItem = Annotated[
    DiffHunkReference | CommandReference, Field(discriminator="kind")
]


class ResolvedChatEvidence(_StrictChatModel):
    """Host-verified, bounded, sanitized evidence attached to a chat turn."""

    kind: Literal["diff_hunk", "command"]
    run_id: str
    availability: EvidenceAvailability
    label: str
    path: str | None = None
    before_sha256: str | None = None
    after_sha256: str | None = None
    old_start: int | None = None
    old_count: int | None = None
    new_start: int | None = None
    new_count: int | None = None
    command_id: str | None = None
    exit_code: int | None = None
    passed: bool | None = None
    failure_reason: str | None = None
    argv: list[str] = Field(default_factory=list)
    excerpt: str = ""
    truncated: bool = False
    note: str | None = None


class CreateChatTurnRequest(_StrictChatModel):
    """Client request to execute one conversation or read-only chat turn."""

    client_request_id: uuid.UUID
    text: str
    references: list[ChatReferenceItem] = Field(default_factory=list)
    intent_mode: ChatIntentMode = "readonly"

    @field_validator("client_request_id", mode="before")
    @classmethod
    def _check_client_request_id(cls, value: object) -> uuid.UUID:
        return _validate_uuid_value(value, label="client_request_id")

    @field_validator("text")
    @classmethod
    def _check_text(cls, value: str) -> str:
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError("text must be a NUL-free string")
        if not value.strip():
            raise ValueError("text must not be blank")
        if len(value.encode("utf-8")) > MAX_CHAT_USER_TEXT_BYTES:
            raise ValueError(f"text exceeds {MAX_CHAT_USER_TEXT_BYTES} UTF-8 bytes")
        return value

    @field_validator("references")
    @classmethod
    def _check_references(cls, value: list[ChatReferenceItem]) -> list[ChatReferenceItem]:
        if len(value) > MAX_CHAT_REFERENCES_PER_TURN:
            raise ValueError(f"at most {MAX_CHAT_REFERENCES_PER_TURN} references allowed per turn")
        return value


class ChatToolReadRecord(_StrictChatModel):
    """Persisted bounded summary of one whitelisted read-only workspace tool call."""

    step: int = Field(ge=1)
    tool_name: ChatToolName
    args: dict[str, Any] = Field(default_factory=dict)
    ok: bool
    summary: str
    path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    sha256: str | None = None
    truncated: bool = False
    error_code: str | None = None


class ChatExecutionOfferView(_StrictChatModel):
    """Persisted confirmable execution offer bound to one chat turn."""

    offer_id: str
    session_id: str
    turn_id: str
    client_request_id: str
    goal: str
    summary: str
    status: ChatExecutionOfferStatus
    run_id: str | None = None
    run_client_request_id: str
    created_at: str
    updated_at: str


class ConfirmChatOfferRequest(_StrictChatModel):
    """Explicit user confirmation of one persisted execution offer."""

    offer_id: str
    confirmed_scope: bool = False
    selected_skill_ids: list[str] = Field(default_factory=list)

    @field_validator("offer_id")
    @classmethod
    def _check_offer_id(cls, value: str) -> str:
        return str(_validate_uuid_value(value, label="offer_id"))

    @field_validator("selected_skill_ids")
    @classmethod
    def _check_skills(cls, value: list[str]) -> list[str]:
        if len(value) > 16:
            raise ValueError("too many selected_skill_ids")
        out: list[str] = []
        for s in value:
            if not isinstance(s, str) or not s.strip() or "\x00" in s:
                raise ValueError("selected_skill_ids must contain non-blank strings")
            out.append(s.strip())
        return out


class ChatTurnView(_StrictChatModel):
    """Persisted conversation turn returned to clients."""

    turn_id: str
    project_id: str
    session_id: str
    client_request_id: str
    seq: int = Field(ge=1)
    status: ChatTurnStatus
    intent_mode: ChatIntentMode = "readonly"
    user_message: str
    references: list[ChatReferenceItem] = Field(default_factory=list)
    resolved_evidence: list[ResolvedChatEvidence] = Field(default_factory=list)
    tool_reads: list[ChatToolReadRecord] = Field(default_factory=list)
    assistant_message: str | None = None
    execution_offer: ChatExecutionOfferView | None = None
    error_code: str | None = None
    error_message: str | None = None
    provider_mode: str = "offline"
    context_truncated: bool = False
    created_at: str
    updated_at: str


class ChatTurnsPage(_StrictChatModel):
    """Cursor-paginated slice of read-only chat turns for one session."""

    session_id: str
    items: list[ChatTurnView]
    next_after_seq: int = Field(ge=0)
    has_more: bool


class SessionTitleView(_StrictChatModel):
    """Lightweight session title projection scoped to one project."""

    session_id: str
    project_id: str
    title: str
    source: SessionTitleSource
    updated_at: str


class ProjectSessionTitlesView(_StrictChatModel):
    """Project-scoped list of session title projections."""

    project_id: str
    titles: list[SessionTitleView]


class ChatProviderRequest(_StrictChatModel):
    """Bounded context passed to a chat turn provider."""

    project_id: str
    session_id: str
    turn_id: str
    intent_mode: ChatIntentMode = "readonly"
    constraints: str
    question: str
    recent_turns: list[dict[str, str]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    tool_observations: list[dict[str, Any]] = Field(default_factory=list)
    allowed_tools: list[str] = Field(
        default_factory=lambda: sorted(READONLY_CHAT_TOOLS)
    )
    context_truncated: bool = False
    remaining_tool_budget: int = Field(ge=0, le=MAX_CHAT_TOOL_CALLS_PER_TURN)


class ChatProviderResponse(_StrictChatModel):
    """Single step decision returned by a chat turn provider."""

    action: Literal["tool_call", "final", "execution_offer"]
    tool_name: str | None = None
    tool_args: dict[str, Any] = Field(default_factory=dict)
    assistant_message: str | None = None
    goal: str | None = None
    summary: str | None = None

    @model_validator(mode="after")
    def _validate_action_branch(self) -> ChatProviderResponse:
        if self.action == "tool_call":
            if not isinstance(self.tool_name, str) or not self.tool_name.strip():
                raise ValueError("tool_call requires a non-blank tool_name")
            if self.assistant_message is not None or self.goal is not None or self.summary is not None:
                raise ValueError("tool_call must not include assistant_message, goal, or summary")
            return self
        if self.action == "final":
            if (
                not isinstance(self.assistant_message, str)
                or not self.assistant_message.strip()
                or "\x00" in self.assistant_message
            ):
                raise ValueError("final requires a non-blank NUL-free assistant_message")
            if self.tool_name is not None or self.tool_args:
                raise ValueError("final must not include tool_name or tool_args")
            if self.goal is not None or self.summary is not None:
                raise ValueError("final must not include goal or summary")
            return self
        # action == "execution_offer"
        if self.tool_name is not None or self.tool_args or self.assistant_message is not None:
            raise ValueError("execution_offer must not include tool_name, tool_args, or assistant_message")
        for field_name, val, max_bytes in (
            ("goal", self.goal, MAX_CHAT_OFFER_GOAL_BYTES),
            ("summary", self.summary, MAX_CHAT_OFFER_SUMMARY_BYTES),
        ):
            if not isinstance(val, str) or not val.strip() or "\x00" in val:
                raise ValueError(f"execution_offer requires non-blank NUL-free {field_name}")
            if len(val.encode("utf-8")) > max_bytes:
                raise ValueError(f"execution_offer {field_name} exceeds {max_bytes} UTF-8 bytes")
        return self
