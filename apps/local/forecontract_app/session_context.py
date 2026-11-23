"""Deterministic bounded ContextFrame assembly and per-run ConversationRuntime."""

from __future__ import annotations

import hashlib
from typing import Any

from forecontract_app.models import MAX_GOAL_STRIPPED_LEN, AppError
from forecontract_app.session_models import (
    DEFAULT_CONTEXT_MAX_BYTES,
    MAX_CONSTRAINTS_UTF8_BYTES,
    MAX_HISTORY_ITEM_EXCERPT_BYTES,
    ContextFrame,
    HistoryItem,
    truncate_utf8_safe,
)
from forecontract_app.session_questions import QuestionBroker
from forecontract_app.session_usage import UsageAccumulator


def _frame_utf8_bytes(frame: ContextFrame) -> int:
    return len(frame.model_dump_json().encode("utf-8"))


def build_context_frame(
    *,
    project_id: str,
    session_id: str,
    run_id: str,
    goal: str,
    constraints: str,
    history: list[HistoryItem],
    max_bytes: int = DEFAULT_CONTEXT_MAX_BYTES,
) -> ContextFrame:
    """Deterministically pack bounded recent history and verbatim constraints within ``max_bytes``."""
    if not isinstance(project_id, str) or not project_id.strip():
        raise AppError("INVALID_INPUT", "project_id is required", status_code=422)
    if not isinstance(session_id, str) or not session_id.strip():
        raise AppError("INVALID_INPUT", "session_id is required", status_code=422)
    if not isinstance(run_id, str) or not run_id.strip():
        raise AppError("INVALID_INPUT", "run_id is required", status_code=422)
    if not isinstance(goal, str) or "\x00" in goal:
        raise AppError("INVALID_INPUT", "goal must be a NUL-free string", status_code=422)
    stripped_goal = goal.strip()
    if not stripped_goal or len(stripped_goal) > MAX_GOAL_STRIPPED_LEN:
        raise AppError(
            "INVALID_INPUT",
            f"goal after strip must be 1..{MAX_GOAL_STRIPPED_LEN} characters",
            status_code=422,
        )
    if not isinstance(constraints, str) or "\x00" in constraints:
        raise AppError("INVALID_INPUT", "constraints must be a NUL-free string", status_code=422)
    if len(constraints.encode("utf-8")) > MAX_CONSTRAINTS_UTF8_BYTES:
        raise AppError(
            "INVALID_INPUT",
            f"constraints exceed {MAX_CONSTRAINTS_UTF8_BYTES} UTF-8 bytes",
            status_code=422,
        )
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise AppError("INVALID_INPUT", "max_bytes must be a positive integer", status_code=422)
    if not isinstance(history, list):
        raise AppError("INVALID_INPUT", "history must be a list of HistoryItem", status_code=422)

    validated_history: list[HistoryItem] = []
    for raw_item in history:
        item = HistoryItem.model_validate(raw_item)
        if item.session_id != session_id or item.project_id != project_id:
            raise AppError(
                "INVALID_INPUT",
                "history contains item from another project or session",
                status_code=400,
            )
        validated_history.append(item)

    goal_sha256 = hashlib.sha256(goal.encode("utf-8")).hexdigest()
    history_cursor = max((it.seq for it in validated_history), default=0)
    total_items = len(validated_history)

    base_frame = ContextFrame(
        schema_version=1,
        project_id=project_id,
        session_id=session_id,
        run_id=run_id,
        goal_sha256=goal_sha256,
        constraints=constraints,
        history_cursor=history_cursor,
        recent_items=[],
        omitted_count=total_items,
        source_item_ids=[],
        truncated=total_items > 0,
    )
    if _frame_utf8_bytes(base_frame) > max_bytes:
        raise AppError(
            "CONTEXT_BUDGET_EXCEEDED",
            "fixed ContextFrame fields exceed max_bytes budget",
            status_code=422,
        )

    newest_first = sorted(validated_history, key=lambda it: it.seq, reverse=True)
    selected_desc: list[HistoryItem] = []
    current_best = base_frame

    for orig in newest_first:
        excerpt, text_cut = truncate_utf8_safe(orig.text, MAX_HISTORY_ITEM_EXCERPT_BYTES)
        # Preserve original item sha256 and source_ids even when text is compacted for prompt budget.
        compacted_item = HistoryItem(
            seq=orig.seq,
            item_id=orig.item_id,
            project_id=orig.project_id,
            session_id=orig.session_id,
            run_id=orig.run_id,
            kind=orig.kind,
            text=excerpt,
            source_ids=list(orig.source_ids),
            truncated=bool(orig.truncated or text_cut),
            sha256=orig.sha256,
        )
        candidate_desc = [*selected_desc, compacted_item]
        candidate_asc = list(reversed(candidate_desc))
        omitted = total_items - len(candidate_asc)
        any_truncated = omitted > 0 or any(it.truncated for it in candidate_asc)
        candidate_frame = ContextFrame(
            schema_version=1,
            project_id=project_id,
            session_id=session_id,
            run_id=run_id,
            goal_sha256=goal_sha256,
            constraints=constraints,
            history_cursor=history_cursor,
            recent_items=candidate_asc,
            omitted_count=omitted,
            source_item_ids=[it.item_id for it in candidate_asc],
            truncated=any_truncated,
        )
        if _frame_utf8_bytes(candidate_frame) > max_bytes:
            break
        selected_desc = candidate_desc
        current_best = candidate_frame

    return current_best


class ConversationRuntime:
    """In-run conversation container holding bounded ContextFrame, optional QuestionBroker, and UsageAccumulator."""

    def __init__(
        self,
        *,
        context_frame: ContextFrame,
        question_broker: QuestionBroker | None = None,
        usage_accumulator: UsageAccumulator | None = None,
    ) -> None:
        self._frame = ContextFrame.model_validate(context_frame)
        self._question_broker = question_broker
        self._usage_accumulator = usage_accumulator

    @property
    def project_id(self) -> str:
        return self._frame.project_id

    @property
    def session_id(self) -> str:
        return self._frame.session_id

    @property
    def run_id(self) -> str:
        return self._frame.run_id

    @property
    def context_frame(self) -> ContextFrame:
        return ContextFrame.model_validate(self._frame)

    @property
    def question_broker(self) -> QuestionBroker | None:
        return self._question_broker

    @property
    def usage_accumulator(self) -> UsageAccumulator | None:
        return self._usage_accumulator

    def observation_payload(self, *, max_recent_items: int | None = None) -> dict[str, Any]:
        """Return serialized ContextFrame dict, optionally dropping oldest recent_items to fit observation cap."""
        frame = self._frame
        if max_recent_items is not None and max_recent_items < len(frame.recent_items):
            keep_count = max(0, max_recent_items)
            kept = frame.recent_items[-keep_count:] if keep_count > 0 else []
            extra_omitted = len(frame.recent_items) - len(kept)
            trimmed_frame = ContextFrame(
                schema_version=frame.schema_version,
                project_id=frame.project_id,
                session_id=frame.session_id,
                run_id=frame.run_id,
                goal_sha256=frame.goal_sha256,
                constraints=frame.constraints,
                history_cursor=frame.history_cursor,
                recent_items=kept,
                omitted_count=frame.omitted_count + extra_omitted,
                source_item_ids=[it.item_id for it in kept],
                truncated=True,
            )
            return trimmed_frame.model_dump(mode="json")
        return frame.model_dump(mode="json")
