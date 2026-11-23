"""Bounded lifecycle and single-pending QuestionBroker for opt-in ask_user."""

from __future__ import annotations

import asyncio
import math
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from forecontract_app.models import AppError
from forecontract_app.project_models import ProjectError
from forecontract_app.session_models import (
    MAX_OPTION_LEN,
    MAX_QUESTION_OPTIONS,
    MAX_QUESTION_TEXT_LEN,
    MAX_QUESTIONS_PER_RUN,
    QUESTION_TTL_SECONDS,
    AnswerQuestionRequest,
    QuestionView,
    sha256_canonical_json,
    utc_now,
)


def _question_digest(
    *,
    project_id: str,
    session_id: str,
    run_id: str,
    question_id: str,
    action_id: str,
    question: str,
    options: list[str],
    expires_at: datetime,
) -> str:
    return sha256_canonical_json(
        {
            "project_id": project_id,
            "session_id": session_id,
            "run_id": run_id,
            "id": question_id,
            "action_id": action_id,
            "question": question,
            "options": list(options),
            "expires_at": expires_at.isoformat(),
        }
    )


def _validate_question_args(*, action_id: str, question: object, options: object) -> tuple[str, list[str]]:
    if not isinstance(action_id, str) or not action_id.strip() or "\x00" in action_id:
        raise ProjectError("INVALID_ACTION", "action_id must be non-blank", status_code=400)
    if not isinstance(question, str) or not question.strip() or "\x00" in question:
        raise ProjectError("INVALID_ACTION", "question must be a non-blank string", status_code=400)
    if len(question) > MAX_QUESTION_TEXT_LEN:
        raise ProjectError(
            "INVALID_ACTION",
            f"question exceeds {MAX_QUESTION_TEXT_LEN} characters",
            status_code=400,
        )
    if not isinstance(options, list):
        raise ProjectError("INVALID_ACTION", "options must be a list of strings", status_code=400)
    if len(options) > MAX_QUESTION_OPTIONS:
        raise ProjectError(
            "INVALID_ACTION",
            f"options cannot exceed {MAX_QUESTION_OPTIONS} items",
            status_code=400,
        )
    cleaned: list[str] = []
    seen: set[str] = set()
    for opt in options:
        if not isinstance(opt, str) or not opt.strip() or "\x00" in opt:
            raise ProjectError("INVALID_ACTION", "each option must be a non-blank string", status_code=400)
        if len(opt) > MAX_OPTION_LEN:
            raise ProjectError(
                "INVALID_ACTION",
                f"option exceeds {MAX_OPTION_LEN} characters",
                status_code=400,
            )
        if opt in seen:
            raise ProjectError("INVALID_ACTION", "options must be distinct", status_code=400)
        seen.add(opt)
        cleaned.append(opt)
    return question, cleaned


class QuestionBroker:
    """Manages per-run opt-in user questions with strict TTL, quota, and commit-before-wake semantics."""

    def __init__(
        self,
        *,
        project_id: str,
        session_id: str,
        run_id: str,
        persist: Callable[[QuestionView], None],
        emit: Callable[[str, dict[str, Any]], None],
        ttl_seconds: float = QUESTION_TTL_SECONDS,
        monotonic: Callable[[], float] | None = None,
        now_utc: Callable[[], datetime] | None = None,
        max_questions: int = MAX_QUESTIONS_PER_RUN,
    ) -> None:
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or not math.isfinite(float(ttl_seconds))
            or float(ttl_seconds) <= 0.0
        ):
            raise ProjectError("INVALID_INPUT", "ttl_seconds must be positive finite", status_code=400)
        if isinstance(max_questions, bool) or not isinstance(max_questions, int) or max_questions < 1:
            raise ProjectError("INVALID_INPUT", "max_questions must be positive int", status_code=400)

        self._project_id = project_id
        self._session_id = session_id
        self._run_id = run_id
        self._persist = persist
        self._emit = emit
        self._ttl_seconds = float(ttl_seconds)
        self._custom_monotonic = monotonic is not None
        self._monotonic: Callable[[], float] = monotonic if monotonic is not None else time.monotonic
        self._now_utc: Callable[[], datetime] = now_utc if now_utc is not None else utc_now
        self._max_questions = max_questions

        self._by_id: dict[str, QuestionView] = {}
        self._by_action: dict[str, str] = {}
        self._order: list[str] = []
        self._waiters: dict[str, asyncio.Event] = {}
        self._deadlines: dict[str, float] = {}
        self._answer_idempotency: dict[str, tuple[str, str]] = {}

    def _pending_id(self) -> str | None:
        for qid in self._order:
            if self._by_id[qid].state == "pending":
                return qid
        return None

    def list_questions(self) -> list[QuestionView]:
        """Return detached copies of all questions in creation order."""
        return [self._by_id[qid].model_copy(deep=True) for qid in self._order]

    def get_question(self, question_id: str) -> QuestionView | None:
        item = self._by_id.get(question_id)
        return item.model_copy(deep=True) if item is not None else None

    def _transition_pending(
        self,
        *,
        question_id: str,
        new_state: str,
        reason_code: str | None = None,
        answer_text: str | None = None,
        emit_kind: str = "question_closed",
        raise_on_persist_error: bool = True,
    ) -> QuestionView:
        current = self._by_id[question_id]
        updates: dict[str, Any] = {"state": new_state}
        if reason_code is not None:
            updates["reason_code"] = reason_code
        if answer_text is not None:
            updates["answer"] = answer_text
        updated = current.model_copy(update=updates)
        try:
            self._persist(updated.model_copy(deep=True))
        except Exception:
            if raise_on_persist_error:
                raise
            # Best-effort memory/waiter release on cancellation paths while keeping failure visible.
        self._by_id[question_id] = updated
        self._emit(emit_kind, updated.model_dump(mode="json"))
        waiter = self._waiters.get(question_id)
        if waiter is not None:
            waiter.set()
        return updated

    async def ask(self, *, action_id: str, question: str, options: list[str]) -> QuestionView:
        """Create/reuse one pending question for ``action_id`` and wait boundedly until resolved."""
        clean_q, clean_opts = _validate_question_args(
            action_id=action_id, question=question, options=options
        )

        existing_qid = self._by_action.get(action_id)
        if existing_qid is not None:
            existing = self._by_id[existing_qid]
            if existing.question != clean_q or list(existing.options) != clean_opts:
                raise ProjectError(
                    "QUESTION_CONFLICT",
                    "action_id reused with different question payload",
                    status_code=409,
                )
            if existing.state != "pending":
                return existing.model_copy(deep=True)
            return await self._await_resolution(existing_qid)

        if self._pending_id() is not None:
            raise ProjectError(
                "QUESTION_ALREADY_PENDING",
                "another pending question already exists in this run",
                status_code=409,
            )
        if len(self._order) >= self._max_questions:
            raise ProjectError(
                "QUESTION_QUOTA_EXCEEDED",
                "maximum questions per run reached",
                status_code=409,
            )

        qid = str(uuid.uuid4())
        now_dt = self._now_utc()
        if now_dt.tzinfo is None:
            raise ProjectError("INVALID_INPUT", "now_utc must return timezone-aware datetime", status_code=500)
        expires_dt = now_dt + timedelta(seconds=self._ttl_seconds)
        digest = _question_digest(
            project_id=self._project_id,
            session_id=self._session_id,
            run_id=self._run_id,
            question_id=qid,
            action_id=action_id,
            question=clean_q,
            options=clean_opts,
            expires_at=expires_dt,
        )
        view = QuestionView(
            id=qid,
            project_id=self._project_id,
            session_id=self._session_id,
            run_id=self._run_id,
            action_id=action_id,
            question=clean_q,
            options=clean_opts,
            digest=digest,
            state="pending",
            answer=None,
            created_at=now_dt,
            expires_at=expires_dt,
            reason_code=None,
        )

        # Commit persistence BEFORE registering in-memory pending state or emitting event.
        self._persist(view.model_copy(deep=True))
        self._by_id[qid] = view
        self._by_action[action_id] = qid
        self._order.append(qid)
        self._waiters[qid] = asyncio.Event()
        self._deadlines[qid] = float(self._monotonic()) + self._ttl_seconds
        self._emit("question_requested", view.model_dump(mode="json"))

        return await self._await_resolution(qid)

    async def _await_resolution(self, qid: str) -> QuestionView:
        waiter = self._waiters[qid]
        deadline = self._deadlines[qid]
        try:
            while self._by_id[qid].state == "pending":
                remaining = deadline - float(self._monotonic())
                if remaining <= 0:
                    updated = self._transition_pending(
                        question_id=qid,
                        new_state="expired",
                        reason_code="QUESTION_EXPIRED",
                        emit_kind="question_closed",
                        raise_on_persist_error=False,
                    )
                    return updated.model_copy(deep=True)
                wait_slice = min(remaining, 0.01) if self._custom_monotonic else remaining
                try:
                    await asyncio.wait_for(waiter.wait(), timeout=wait_slice)
                except asyncio.TimeoutError:
                    continue
            return self._by_id[qid].model_copy(deep=True)
        except asyncio.CancelledError:
            if self._by_id[qid].state == "pending":
                self._transition_pending(
                    question_id=qid,
                    new_state="cancelled",
                    reason_code="RUN_CANCELLED",
                    emit_kind="question_closed",
                    raise_on_persist_error=False,
                )
            raise

    def answer(self, *, question_id: str, request: AnswerQuestionRequest) -> QuestionView:
        """Validate digest & state, persist answer atomically BEFORE waking waiter."""
        validated = AnswerQuestionRequest.model_validate(request)
        if not isinstance(question_id, str) or question_id not in self._by_id:
            raise AppError("NOT_FOUND", "question not found", status_code=404)

        req_key = str(validated.client_request_id)
        payload_digest = sha256_canonical_json(
            {
                "question_digest": validated.question_digest,
                "text": validated.text,
            }
        )
        prior = self._answer_idempotency.get(req_key)
        if prior is not None:
            prior_qid, prior_digest = prior
            if prior_qid != question_id or prior_digest != payload_digest:
                raise AppError(
                    "IDEMPOTENCY_CONFLICT",
                    "client_request_id reused with different answer payload",
                    status_code=409,
                )
            return self._by_id[question_id].model_copy(deep=True)

        current = self._by_id[question_id]
        if current.state != "pending":
            raise AppError(
                "QUESTION_CLOSED",
                f"question is already closed ({current.state})",
                status_code=409,
            )

        deadline = self._deadlines.get(question_id)
        if deadline is not None and float(self._monotonic()) >= deadline:
            self._transition_pending(
                question_id=question_id,
                new_state="expired",
                reason_code="QUESTION_EXPIRED",
                emit_kind="question_closed",
                raise_on_persist_error=False,
            )
            raise AppError("QUESTION_CLOSED", "question has expired", status_code=409)

        if validated.question_digest != current.digest:
            raise AppError(
                "QUESTION_DIGEST_MISMATCH",
                "question_digest does not match current question",
                status_code=409,
            )

        updated = self._transition_pending(
            question_id=question_id,
            new_state="answered",
            reason_code="USER_ANSWERED",
            answer_text=validated.text,
            emit_kind="question_answered",
            raise_on_persist_error=True,
        )
        self._answer_idempotency[req_key] = (question_id, payload_digest)
        return updated.model_copy(deep=True)

    def cancel_pending(self, *, reason_code: str) -> None:
        """Close any currently pending question and wake waiter without leaving orphan waits."""
        clean_reason = reason_code.strip() if isinstance(reason_code, str) and reason_code.strip() else "CANCELLED"
        for qid in list(self._order):
            if self._by_id[qid].state == "pending":
                self._transition_pending(
                    question_id=qid,
                    new_state="cancelled",
                    reason_code=clean_reason,
                    emit_kind="question_closed",
                    raise_on_persist_error=False,
                )
