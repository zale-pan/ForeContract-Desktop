"""Strict DTOs, bounded limits, and canonical hashing helpers for APP-T03 durable sessions."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Annotated, Any, ClassVar, Final, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from forecontract_app.models import (
    NonNegativeStrictInt,
    PositiveStrictInt,
    StrictBool,
    StrictStr,
)

SESSION_DB_SCHEMA_VERSION: Final[int] = 1
SESSION_DB_FILENAME: Final[str] = "sessions.sqlite3"
SESSION_DB_PAGE_SIZE: Final[int] = 4096
SESSION_DB_MAX_PAGES: Final[int] = 32768
SESSION_DB_LIMIT_BYTES: Final[int] = SESSION_DB_PAGE_SIZE * SESSION_DB_MAX_PAGES

MAX_CONSTRAINTS_UTF8_BYTES: Final[int] = 8192
MAX_QUESTION_TEXT_LEN: Final[int] = 4096
MAX_ANSWER_TEXT_LEN: Final[int] = 4096
MAX_QUESTIONS_PER_RUN: Final[int] = 8
MAX_PENDING_QUESTIONS_PER_RUN: Final[int] = 1
MAX_QUESTION_OPTIONS: Final[int] = 4
MAX_OPTION_LEN: Final[int] = 128
QUESTION_TTL_SECONDS: Final[float] = 120.0
DEFAULT_CONTEXT_MAX_BYTES: Final[int] = 16384
MAX_HISTORY_ITEM_EXCERPT_BYTES: Final[int] = 1024
MAX_RUN_RESULT_SUMMARY_BYTES: Final[int] = 32 * 1024
MAX_PERSISTED_EVENTS_PER_RUN: Final[int] = 64
MAX_PERSISTED_EVENTS_BYTES_PER_RUN: Final[int] = 512 * 1024

HistoryItemKind = Literal["user_goal", "question", "answer", "run_result"]
QuestionState = Literal["pending", "answered", "expired", "cancelled"]

_DECLARED_STORAGE = BaseModel.__dict__.get("__dict__")
_STRICT_SESSION_CONFIG = ConfigDict(extra="forbid", strict=True, frozen=True)


def canonical_json_dumps(obj: object) -> str:
    """Serialize ``obj`` deterministically with sorted keys and finite JSON only."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_canonical_json(obj: object) -> str:
    """Compute SHA-256 hex digest over canonical UTF-8 JSON representation."""
    return hashlib.sha256(canonical_json_dumps(obj).encode("utf-8")).hexdigest()


def truncate_utf8_safe(value: str, max_bytes: int) -> tuple[str, bool]:
    """Safely truncate ``value`` so its UTF-8 encoding does not exceed ``max_bytes``."""
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    raw = value.encode("utf-8")
    if len(raw) <= max_bytes:
        return value, False
    cut = raw[:max_bytes]
    while cut:
        try:
            return cut.decode("utf-8"), True
        except UnicodeDecodeError:
            cut = cut[:-1]
    return "", True


def utc_now() -> datetime:
    """Return current timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def _ensure_utc_aware(dt: datetime) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware UTC")
    return dt.astimezone(timezone.utc)


def _check_non_blank_no_nul(value: str) -> str:
    if "\x00" in value or not value.strip():
        raise ValueError("value must be non-blank and NUL-free")
    return value


def _check_sha256_hex(value: str) -> str:
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
    return value


def _check_canonical_uuid_str(value: str) -> str:
    try:
        parsed = uuid.UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("must be a valid UUID string") from exc
    if str(parsed) != value.lower() or str(parsed) != value:
        raise ValueError("must be a canonical lowercase UUID string")
    return value


UtcDatetime = Annotated[datetime, AfterValidator(_ensure_utc_aware)]
NonBlankStr = Annotated[StrictStr, AfterValidator(_check_non_blank_no_nul)]
Sha256HexStr = Annotated[StrictStr, AfterValidator(_check_sha256_hex)]
UuidStr = Annotated[StrictStr, AfterValidator(_check_canonical_uuid_str)]


def compute_history_item_sha256(
    *,
    seq: int,
    item_id: str,
    project_id: str,
    session_id: str,
    run_id: str,
    kind: HistoryItemKind,
    text: str,
    source_ids: list[str],
    truncated: bool,
) -> str:
    """Return canonical SHA-256 over a HistoryItem's full payload excluding ``sha256``."""
    return sha256_canonical_json(
        {
            "seq": seq,
            "item_id": item_id,
            "project_id": project_id,
            "session_id": session_id,
            "run_id": run_id,
            "kind": kind,
            "text": text,
            "source_ids": list(source_ids),
            "truncated": truncated,
        }
    )


def _parse_strict_uuid_field(value: object, *, label: str) -> uuid.UUID:
    if isinstance(value, uuid.UUID):
        return value
    if isinstance(value, str):
        try:
            parsed = uuid.UUID(value)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError(f"{label} must be a valid UUID") from exc
        if str(parsed) != value.lower():
            raise ValueError(f"{label} must use standard hyphenated UUID format")
        return parsed
    raise ValueError(f"{label} must be a UUID or UUID string")


def _extract_declared_dict(obj: BaseModel) -> dict[str, Any]:
    if _DECLARED_STORAGE is None:
        raise ValueError("cannot inspect BaseModel storage")
    stored = _DECLARED_STORAGE.__get__(obj, type(obj))
    if not isinstance(stored, dict):
        raise TypeError("invalid stored model fields")
    copied: dict[str, Any] = {}
    for k, v in stored.items():
        if isinstance(v, list):
            copied[k] = list(v)
        elif isinstance(v, dict):
            copied[k] = dict(v)
        else:
            copied[k] = v
    return copied


class _StrictSessionModel(BaseModel):
    """Strict, frozen base model that re-validates declared storage even on model_construct/copy."""

    model_config = _STRICT_SESSION_CONFIG
    _copy_list_fields: ClassVar[tuple[str, ...]] = ()

    @classmethod
    def model_validate(cls, obj: object, **kwargs: object) -> Self:
        if isinstance(obj, cls):
            raw = _extract_declared_dict(obj)
            return super().model_validate(raw, **kwargs)  # type: ignore[arg-type]
        return super().model_validate(obj, **kwargs)  # type: ignore[arg-type]

    @model_validator(mode="after")
    def _detach_lists(self) -> Self:
        for fname in type(self)._copy_list_fields:
            val = getattr(self, fname)
            if isinstance(val, list):
                object.__setattr__(self, fname, list(val))
        return self


class HistoryItem(_StrictSessionModel):
    """Durable, ordered conversation history item bound to a session and run."""

    _copy_list_fields: ClassVar[tuple[str, ...]] = ("source_ids",)

    seq: PositiveStrictInt
    item_id: UuidStr
    project_id: UuidStr
    session_id: UuidStr
    run_id: UuidStr
    kind: HistoryItemKind
    text: StrictStr
    source_ids: list[NonBlankStr] = Field(default_factory=list)
    truncated: StrictBool = False
    sha256: Sha256HexStr

    @field_validator("text")
    @classmethod
    def _validate_text(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("history item text must not contain NUL bytes")
        return value


class HistoryPage(_StrictSessionModel):
    """Cursor-paginated history page for one session."""

    _copy_list_fields: ClassVar[tuple[str, ...]] = ("items",)

    session_id: UuidStr
    items: list[HistoryItem]
    cursor: NonNegativeStrictInt
    has_more: StrictBool

    @model_validator(mode="after")
    def _validate_items_monotonic(self) -> HistoryPage:
        revalidated = [HistoryItem.model_validate(item) for item in self.items]
        for idx in range(1, len(revalidated)):
            if revalidated[idx].seq <= revalidated[idx - 1].seq:
                raise ValueError("HistoryPage items must have strictly increasing seq")
        for item in revalidated:
            if item.session_id != self.session_id:
                raise ValueError("HistoryPage item session_id mismatch")
        object.__setattr__(self, "items", revalidated)
        return self


class SetConstraintsRequest(_StrictSessionModel):
    """Strict payload for updating session fixed constraints with optimistic revision."""

    client_request_id: uuid.UUID
    expected_revision: NonNegativeStrictInt
    constraints: StrictStr

    @field_validator("client_request_id", mode="before")
    @classmethod
    def _parse_req_id(cls, value: object) -> uuid.UUID:
        return _parse_strict_uuid_field(value, label="client_request_id")

    @field_validator("constraints")
    @classmethod
    def _validate_constraints(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("constraints must not contain NUL bytes")
        if len(value.encode("utf-8")) > MAX_CONSTRAINTS_UTF8_BYTES:
            raise ValueError(f"constraints exceed {MAX_CONSTRAINTS_UTF8_BYTES} UTF-8 bytes")
        return value


class SessionContextView(_StrictSessionModel):
    """Public view of session constraints, revision, cursor, and SQLite quota usage."""

    session_id: UuidStr
    project_id: UuidStr
    revision: NonNegativeStrictInt
    constraints: StrictStr
    history_cursor: NonNegativeStrictInt
    storage_used_bytes: NonNegativeStrictInt
    storage_limit_bytes: PositiveStrictInt


class ContextFrame(_StrictSessionModel):
    """Deterministic UTF-8 byte-bounded context frame passed into existing-project runs."""

    _copy_list_fields: ClassVar[tuple[str, ...]] = ("recent_items", "source_item_ids")

    schema_version: Literal[1] = 1
    project_id: UuidStr
    session_id: UuidStr
    run_id: UuidStr
    goal_sha256: Sha256HexStr
    constraints: StrictStr
    history_cursor: NonNegativeStrictInt
    recent_items: list[HistoryItem]
    omitted_count: NonNegativeStrictInt
    source_item_ids: list[NonBlankStr]
    truncated: StrictBool

    @model_validator(mode="after")
    def _revalidate_items(self) -> ContextFrame:
        revalidated = [HistoryItem.model_validate(item) for item in self.recent_items]
        object.__setattr__(self, "recent_items", revalidated)
        return self


class QuestionView(_StrictSessionModel):
    """Durable, digest-bound view of one ask_user question in a run."""

    _copy_list_fields: ClassVar[tuple[str, ...]] = ("options",)

    id: UuidStr
    project_id: UuidStr
    session_id: UuidStr
    run_id: UuidStr
    action_id: NonBlankStr
    question: NonBlankStr
    options: list[NonBlankStr]
    digest: Sha256HexStr
    state: QuestionState
    answer: StrictStr | None = None
    created_at: UtcDatetime
    expires_at: UtcDatetime
    reason_code: StrictStr | None = None

    @field_validator("question")
    @classmethod
    def _validate_question(cls, value: str) -> str:
        if len(value) > MAX_QUESTION_TEXT_LEN:
            raise ValueError(f"question exceeds {MAX_QUESTION_TEXT_LEN} characters")
        return value

    @field_validator("options")
    @classmethod
    def _validate_options(cls, value: list[str]) -> list[str]:
        if len(value) > MAX_QUESTION_OPTIONS:
            raise ValueError(f"options exceed maximum {MAX_QUESTION_OPTIONS}")
        if len(set(value)) != len(value):
            raise ValueError("options must be distinct")
        for opt in value:
            if len(opt) > MAX_OPTION_LEN:
                raise ValueError(f"option exceeds {MAX_OPTION_LEN} characters")
        return list(value)


class AnswerQuestionRequest(_StrictSessionModel):
    """Strict payload for answering one pending question."""

    client_request_id: uuid.UUID
    question_digest: Sha256HexStr
    text: NonBlankStr

    @field_validator("client_request_id", mode="before")
    @classmethod
    def _parse_req_id(cls, value: object) -> uuid.UUID:
        return _parse_strict_uuid_field(value, label="client_request_id")

    @field_validator("text")
    @classmethod
    def _validate_answer_text(cls, value: str) -> str:
        if len(value) > MAX_ANSWER_TEXT_LEN:
            raise ValueError(f"answer text exceeds {MAX_ANSWER_TEXT_LEN} characters")
        return value


class UsageView(_StrictSessionModel):
    """Honest offline usage & budget projection without fabricated cost billing."""

    calls_started: NonNegativeStrictInt
    calls_completed: NonNegativeStrictInt
    usage_known_calls: NonNegativeStrictInt
    usage_unknown_calls: NonNegativeStrictInt
    known_input_tokens: NonNegativeStrictInt
    known_output_tokens: NonNegativeStrictInt
    input_tokens: NonNegativeStrictInt | None = None
    output_tokens: NonNegativeStrictInt | None = None
    max_calls: PositiveStrictInt
    steps_used: NonNegativeStrictInt
    max_steps: PositiveStrictInt
    elapsed_ms: NonNegativeStrictInt
    run_timeout_ms: PositiveStrictInt
    estimated_cost_cny: None = None
    billed_cost_cny: None = None
    cost_status: Literal["offline_not_billed", "live_reserved_billing_unknown"] = "offline_not_billed"
    reserved_cny: float | None = None
