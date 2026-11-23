"""Deterministic token/step/budget usage aggregation without fabricated cost billing."""

from __future__ import annotations

import time
from collections.abc import Callable

from forecontract_llm.errors import PlannerError
from forecontract_llm.models import ModelUsage, read_declared_copy

from forecontract_app.models import AppError
from forecontract_app.session_models import UsageView


def _require_pos_int(val: object, *, label: str) -> int:
    if isinstance(val, bool) or not isinstance(val, int) or val < 1:
        raise AppError("INVALID_INPUT", f"{label} must be a positive integer", status_code=422)
    return val


class UsageAccumulator:
    """Thread-loop-local usage & monotonic runtime tracker bound to one Run."""

    def __init__(
        self,
        *,
        max_calls: int,
        max_steps: int,
        run_timeout_ms: int,
        monotonic: Callable[[], float] | None = None,
        initial_elapsed_ms: int = 0,
        live: bool = False,
    ) -> None:
        self._max_calls = _require_pos_int(max_calls, label="max_calls")
        self._max_steps = _require_pos_int(max_steps, label="max_steps")
        self._run_timeout_ms = _require_pos_int(run_timeout_ms, label="run_timeout_ms")
        self._live = bool(live)
        if isinstance(initial_elapsed_ms, bool) or not isinstance(initial_elapsed_ms, int) or initial_elapsed_ms < 0:
            raise AppError("INVALID_INPUT", "initial_elapsed_ms must be >= 0", status_code=422)
        self._monotonic: Callable[[], float] = monotonic if monotonic is not None else time.monotonic
        self._start_monotonic: float = float(self._monotonic())
        self._initial_elapsed_ms: int = initial_elapsed_ms
        self._frozen_elapsed_ms: int | None = None

        self._started_order: list[str] = []
        self._started_ids: set[str] = set()
        self._finished: dict[str, tuple[int, int] | None] = {}
        self._usage_known_calls: int = 0
        self._completed_unknown_calls: int = 0
        self._known_input_tokens: int = 0
        self._known_output_tokens: int = 0
        self._steps_used: int = 0
        self._reserved_cny: float = 0.0

    def begin_call(self, *, request_id: str) -> None:
        """Register the start of one real model provider call, deduplicated by ``request_id``."""
        if not isinstance(request_id, str) or not request_id.strip() or "\x00" in request_id:
            raise AppError("INVALID_INPUT", "request_id must be non-blank", status_code=422)
        if request_id in self._started_ids:
            return
        self._started_ids.add(request_id)
        self._started_order.append(request_id)

    def finish_call(self, *, request_id: str, usage: ModelUsage | None) -> None:
        """Record completion and optional ``ModelUsage`` for an already-begun ``request_id``."""
        if not isinstance(request_id, str) or not request_id.strip() or "\x00" in request_id:
            raise AppError("INVALID_INPUT", "request_id must be non-blank", status_code=422)
        if request_id not in self._started_ids:
            raise AppError(
                "USAGE_UNKNOWN_REQUEST",
                "finish_call requires an active begin_call for request_id",
                status_code=400,
            )

        normalized: tuple[int, int] | None
        if usage is None:
            normalized = None
        else:
            try:
                checked = read_declared_copy(usage, ModelUsage, "INPUT_INVALID")
            except PlannerError as exc:
                raise AppError("INVALID_INPUT", "invalid ModelUsage record", status_code=422) from exc
            normalized = (int(checked.input_tokens), int(checked.output_tokens))

        if request_id in self._finished:
            prev = self._finished[request_id]
            if prev == normalized:
                return
            raise AppError(
                "USAGE_CONFLICT",
                "request_id already finished with different usage",
                status_code=409,
            )

        self._finished[request_id] = normalized
        if normalized is None:
            self._completed_unknown_calls += 1
        else:
            self._usage_known_calls += 1
            self._known_input_tokens += normalized[0]
            self._known_output_tokens += normalized[1]

    def set_steps_used(self, steps_used: int) -> None:
        """Record actual executed Core steps cleanly without exceeding monotonicity."""
        if isinstance(steps_used, bool) or not isinstance(steps_used, int) or steps_used < 0:
            raise AppError("INVALID_INPUT", "steps_used must be non-negative int", status_code=422)
        self._steps_used = max(self._steps_used, steps_used)

    def record_live_reservation(self, *, amount_cny: float) -> None:
        """Accumulate the live provider's successfully reserved monetary amount for this run."""
        if isinstance(amount_cny, bool) or not isinstance(amount_cny, (int, float)) or amount_cny < 0:
            raise AppError("INVALID_INPUT", "amount_cny must be non-negative", status_code=422)
        self._reserved_cny += float(amount_cny)

    def _compute_elapsed_ms(self) -> int:
        if self._frozen_elapsed_ms is not None:
            return self._frozen_elapsed_ms
        delta = max(0.0, float(self._monotonic()) - self._start_monotonic)
        return self._initial_elapsed_ms + round(delta * 1000.0)

    def finalize(self) -> UsageView:
        """Freeze elapsed runtime once run terminates so reopen does not inflate elapsed_ms."""
        if self._frozen_elapsed_ms is None:
            self._frozen_elapsed_ms = self._compute_elapsed_ms()
        return self.snapshot()

    def snapshot(self) -> UsageView:
        """Return an honest, detached ``UsageView`` preserving known subtotals and unknown flags."""
        calls_started = len(self._started_order)
        calls_completed = len(self._finished)
        unfinished = calls_started - calls_completed
        unknown_total = self._completed_unknown_calls + unfinished
        if unknown_total > 0:
            total_in: int | None = None
            total_out: int | None = None
        else:
            total_in = self._known_input_tokens
            total_out = self._known_output_tokens
        reserved: float | None
        if self._live:
            reserved = self._reserved_cny
            cost_status: str = "live_reserved_billing_unknown"
        else:
            reserved = None
            cost_status = "offline_not_billed"
        return UsageView(
            calls_started=calls_started,
            calls_completed=calls_completed,
            usage_known_calls=self._usage_known_calls,
            usage_unknown_calls=unknown_total,
            known_input_tokens=self._known_input_tokens,
            known_output_tokens=self._known_output_tokens,
            input_tokens=total_in,
            output_tokens=total_out,
            max_calls=self._max_calls,
            steps_used=self._steps_used,
            max_steps=self._max_steps,
            elapsed_ms=self._compute_elapsed_ms(),
            run_timeout_ms=self._run_timeout_ms,
            estimated_cost_cny=None,
            billed_cost_cny=None,
            cost_status=cost_status,
            reserved_cny=reserved,
        )
