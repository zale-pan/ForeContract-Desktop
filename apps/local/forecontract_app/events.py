"""Bounded ring-buffered event log and cursor-safe SSE fan-out per Run."""

from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator, Callable
from typing import Any, Final

from forecontract_app.models import (
    MAX_EVENT_JSON_BYTES,
    SCHEMA_VERSION,
    AppError,
    AppEvent,
    AppEventKind,
    RunSnapshot,
    RunView,
)

MAX_SUBSCRIBERS_PER_RUN: Final[int] = 4
SUBSCRIBER_QUEUE_MAXSIZE: Final[int] = 64

_TRUNCATABLE_TEXT_FIELDS: Final[tuple[str, ...]] = (
    "stdout",
    "stderr",
    "content",
    "message",
    "goal",
    "excerpt",
)


def _utf8_json_bytes(obj: object) -> bytes:
    return json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8")


def _bound_payload_for_envelope(
    *,
    project_id: str,
    session_id: str,
    run_id: str,
    seq: int,
    kind: AppEventKind,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Ensure the serialized ``AppEvent`` fits strictly within 16 KiB UTF-8."""
    candidate_payload = dict(payload)
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "session_id": session_id,
        "run_id": run_id,
        "seq": seq,
        "kind": kind,
        "payload": candidate_payload,
    }
    encoded = _utf8_json_bytes(envelope)
    if len(encoded) <= MAX_EVENT_JSON_BYTES:
        return candidate_payload

    original_bytes = len(encoded)
    truncated = False
    for field_name in _TRUNCATABLE_TEXT_FIELDS:
        val = candidate_payload.get(field_name)
        if isinstance(val, str) and len(val.encode("utf-8")) > 512:
            raw_val = val.encode("utf-8")
            candidate_payload[field_name] = raw_val[:512].decode("utf-8", errors="ignore")
            truncated = True
    if truncated:
        candidate_payload["truncated"] = True
        candidate_payload["original_bytes"] = original_bytes
    envelope["payload"] = candidate_payload
    if len(_utf8_json_bytes(envelope)) <= MAX_EVENT_JSON_BYTES:
        return candidate_payload

    # Further shrink all string fields if payload still exceeds 16 KiB
    for key in list(candidate_payload.keys()):
        val = candidate_payload[key]
        if isinstance(val, str) and len(val.encode("utf-8")) > 128:
            candidate_payload[key] = val.encode("utf-8")[:128].decode("utf-8", errors="ignore")
            truncated = True
    candidate_payload["truncated"] = True
    candidate_payload["original_bytes"] = original_bytes
    envelope["payload"] = candidate_payload
    if len(_utf8_json_bytes(envelope)) <= MAX_EVENT_JSON_BYTES:
        return candidate_payload

    return {
        "summary": "event payload exceeded 16 KiB bound",
        "truncated": True,
        "original_bytes": original_bytes,
    }


class _SubscriberQueue:
    """Per-subscriber bounded buffer that drops to resync on overflow."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[AppEvent | None] = asyncio.Queue(
            maxsize=SUBSCRIBER_QUEUE_MAXSIZE
        )
        self.overflowed: bool = False

    def push_nowait(self, item: AppEvent | None) -> None:
        if self.overflowed:
            return
        if item is None:
            if self.queue.full():
                self.overflowed = True
                return
            self.queue.put_nowait(None)
            return
        if self.queue.full():
            # Do not keep unbounded per-connection buffers; force client snapshot resync.
            while not self.queue.empty():
                try:
                    self.queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
            self.overflowed = True
            self.queue.put_nowait(None)
            return
        self.queue.put_nowait(item)


class RunEventHub:
    """Bounded per-run event sequence, snapshot store, commit-before-publish, and SSE fan-out."""

    def __init__(
        self,
        *,
        project_id: str,
        session_id: str,
        run_id: str,
        max_events: int = 512,
        max_subscribers: int = MAX_SUBSCRIBERS_PER_RUN,
        persist_cb: Callable[[AppEvent], int] | None = None,
    ) -> None:
        self._project_id = project_id
        self._session_id = session_id
        self._run_id = run_id
        self._max_events = max_events
        self._max_subscribers = max_subscribers
        self._persist_cb = persist_cb
        self._ring: deque[AppEvent] = deque(maxlen=max_events)
        self._last_seq = 0
        self._truncated_before = 0
        self._finished = False
        self._subscribers: set[_SubscriberQueue] = set()

    @classmethod
    def from_persisted(
        cls,
        *,
        project_id: str,
        session_id: str,
        run_id: str,
        last_seq: int,
        truncated_before: int,
        events: list[AppEvent],
        finished: bool,
        max_events: int = 512,
        max_subscribers: int = MAX_SUBSCRIBERS_PER_RUN,
        persist_cb: Callable[[AppEvent], int] | None = None,
    ) -> RunEventHub:
        """Restore a RunEventHub cleanly from persisted tail without resetting event identities."""
        hub = cls(
            project_id=project_id,
            session_id=session_id,
            run_id=run_id,
            max_events=max_events,
            max_subscribers=max_subscribers,
            persist_cb=persist_cb,
        )
        ordered = sorted(events, key=lambda e: e.seq)
        if len(ordered) > max_events:
            discarded = ordered[: len(ordered) - max_events]
            truncated_before = max(truncated_before, discarded[-1].seq)
            ordered = ordered[-max_events:]
        for ev in ordered:
            hub._ring.append(ev.model_copy(deep=True))
        hub._last_seq = max(last_seq, ordered[-1].seq if ordered else 0)
        hub._truncated_before = truncated_before
        hub._finished = finished or any(ev.kind == "run_finished" for ev in ordered)
        return hub

    @property
    def last_seq(self) -> int:
        return self._last_seq

    @property
    def truncated_before(self) -> int:
        return self._truncated_before

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def active_subscriber_count(self) -> int:
        return len(self._subscribers)

    def prepare_event(self, kind: AppEventKind, payload: dict[str, Any]) -> AppEvent:
        """Build and validate next-seq ``AppEvent`` without mutating hub state or publishing."""
        if self._finished:
            raise AppError("RUN_ALREADY_FINISHED", "run_finished already emitted", status_code=409)
        seq = self._last_seq + 1
        bounded_payload = _bound_payload_for_envelope(
            project_id=self._project_id,
            session_id=self._session_id,
            run_id=self._run_id,
            seq=seq,
            kind=kind,
            payload=payload,
        )
        return AppEvent(
            schema_version=SCHEMA_VERSION,
            project_id=self._project_id,
            session_id=self._session_id,
            run_id=self._run_id,
            seq=seq,
            kind=kind,
            payload=bounded_payload,
        )

    def commit_prepared_event(self, event: AppEvent, *, truncated_before: int = 0) -> AppEvent:
        """Publish an already-committed ``AppEvent`` to the ring buffer and live subscribers."""
        if self._finished:
            raise AppError("RUN_ALREADY_FINISHED", "run_finished already emitted", status_code=409)
        if event.seq <= self._last_seq:
            raise AppError("EVENT_SEQ_CONFLICT", "prepared event sequence is stale", status_code=409)
        self._last_seq = event.seq
        self._truncated_before = max(self._truncated_before, truncated_before)
        if len(self._ring) == self._max_events:
            evicted = self._ring.popleft()
            self._truncated_before = max(self._truncated_before, evicted.seq)
        stored = event.model_copy(deep=True)
        self._ring.append(stored)
        if stored.kind == "run_finished":
            self._finished = True
        for sub in tuple(self._subscribers):
            sub.push_nowait(stored)
            if self._finished:
                sub.push_nowait(None)
        return stored

    def emit(
        self,
        kind: AppEventKind,
        payload: dict[str, Any],
        *,
        persist: bool = True,
    ) -> AppEvent:
        """Prepare event, persist synchronously if configured, then publish to ring and SSE."""
        event = self.prepare_event(kind, payload)
        persisted_trunc = 0
        if persist and self._persist_cb is not None:
            persisted_trunc = int(self._persist_cb(event))
        return self.commit_prepared_event(event, truncated_before=persisted_trunc)

    def snapshot(self, run_view: RunView) -> RunSnapshot:
        """Return a detached snapshot consistent with ``cursor=last_seq``."""
        return RunSnapshot(
            run=run_view.model_copy(deep=True),
            events=[ev.model_copy(deep=True) for ev in self._ring],
            cursor=self._last_seq,
            truncated_before=self._truncated_before,
        )

    def open_subscription(
        self, *, after: int = 0
    ) -> tuple[list[AppEvent], _SubscriberQueue | None]:
        """Validate ``after`` cursor and subscriber quota, returning replay + live handle."""
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise AppError("INVALID_CURSOR", "after cursor must be a non-negative integer", status_code=422)
        if after > self._last_seq:
            raise AppError(
                "INVALID_CURSOR",
                "cursor cannot exceed latest produced event sequence",
                status_code=422,
            )
        if after < self._truncated_before:
            raise AppError(
                "CURSOR_EXPIRED",
                "cursor falls before earliest retained event; fetch /snapshot first",
                status_code=409,
            )
        if len(self._subscribers) >= self._max_subscribers:
            raise AppError(
                "TOO_MANY_SUBSCRIBERS",
                "maximum concurrent subscribers exceeded for run",
                status_code=429,
            )
        replay = [ev.model_copy(deep=True) for ev in self._ring if ev.seq > after]
        if self._finished:
            return replay, None
        sub = _SubscriberQueue()
        self._subscribers.add(sub)
        return replay, sub

    def close_subscription(self, sub: _SubscriberQueue) -> None:
        self._subscribers.discard(sub)

    async def stream_events(self, *, after: int = 0) -> AsyncIterator[AppEvent]:
        """Async iterator validating cursor/quota synchronously before yielding."""
        replay, sub = self.open_subscription(after=after)
        try:
            for item in replay:
                yield item
            if sub is None:
                return
            while True:
                nxt = await sub.queue.get()
                if nxt is None:
                    break
                yield nxt.model_copy(deep=True)
                if nxt.kind == "run_finished":
                    break
        finally:
            if sub is not None:
                self.close_subscription(sub)
