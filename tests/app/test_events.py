"""A13, A14 & A15: Ordered event provenance, bounded ring/subscribers, and SSE reconnect."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest
from app_helpers import (
    CountingMetricsProvider,
    async_wait_until,
    make_factory_spy,
    make_host_config,
)
from forecontract.tracing import JsonlTraceStore
from forecontract_app.events import SUBSCRIBER_QUEUE_MAXSIZE, RunEventHub
from forecontract_app.models import AppError, ModelSelection, RunView, StartRunRequest
from forecontract_app.service import AppService


def test_a13_order_and_actual_sources(tmp_path: Path) -> None:
    cfg = make_host_config(tmp_path)
    providers: list[CountingMetricsProvider] = []

    async def _scenario() -> None:
        svc = AppService(config=cfg, provider_factory=make_factory_spy(providers))
        try:
            proj = await svc.create_project(name="events-order")
            sess = svc.create_session(
                project_id=proj.project_id,
                model=ModelSelection(provider="offline", model="offline-metrics-v1"),
            )
            run = await svc.start_run(
                session_id=sess.session_id,
                request=StartRunRequest(
                    client_request_id=uuid.uuid4(),
                    goal="实现 weighted_positive_total",
                    confirmed_scope=True,
                ),
            )
            await async_wait_until(lambda: svc.get_run(session_id=sess.session_id, run_id=run.run_id).state in ("succeeded", "failed", "cancelled", "unknown"))
            snap = svc.snapshot(session_id=sess.session_id, run_id=run.run_id)
            events = snap.events
            seqs = [e.seq for e in events]
            assert seqs == list(range(1, len(seqs) + 1))
            assert len({(e.run_id, e.seq) for e in events}) == len(events)
            assert events[-1].kind == "run_finished"
            assert sum(1 for e in events if e.kind == "run_finished") == 1

            # tool_result events project actual observation.state.last_tool_result values
            tool_events = [e for e in events if e.kind == "tool_result"]
            observed_results = [
                req["observation"]["state"]["last_tool_result"]
                for req in providers[0].request_dumps
                if req["observation"]["state"].get("last_tool_result") is not None
            ]
            assert len(tool_events) == len(observed_results)
            for ev, obs in zip(tool_events, observed_results, strict=True):
                assert ev.payload["action_type"] == obs["action_type"]
                assert ev.payload["tool_succeeded"] == obs["tool_succeeded"]

            # core_trace events match validated JsonlTraceStore records
            trace_records = JsonlTraceStore(cfg.data_root / "runs" / run.run_id / "trace.jsonl").read_run(run.run_id)
            core_events = [e for e in events if e.kind == "core_trace"]
            assert len(core_events) == len(trace_records)
            for ev, rec in zip(core_events, trace_records, strict=True):
                assert ev.payload["record_id"] == rec.record_id
                assert ev.payload["kind"] == rec.kind
                assert ev.payload["sequence"] == rec.sequence
        finally:
            await svc.aclose()

    asyncio.run(_scenario())


def test_a14_cursor_and_bounds() -> None:
    hub = RunEventHub(
        project_id="p1",
        session_id="s1",
        run_id="r1",
        max_events=512,
        max_subscribers=4,
    )
    run_view = RunView(
        run_id="r1",
        session_id="s1",
        project_id="p1",
        client_request_id=str(uuid.uuid4()),
        goal="stress",
        state="running",
        verification="not_run",
    )

    # Open 4 concurrent active subscribers (5th must fail 429) and overflow one slow sub
    subs = [hub.open_subscription(after=0) for _ in range(4)]
    with pytest.raises(AppError) as e_sub:
        hub.open_subscription(after=0)
    assert e_sub.value.status_code == 429

    # Emit 560 events including oversized stdout payload > 16 KiB
    big_text = "Z" * 24000
    for idx in range(559):
        payload = {"stdout": big_text} if idx == 10 else {"step": idx}
        hub.emit("planning_started", payload)
    hub.emit("run_finished", {"state": "succeeded", "verification": "passed", "reason_code": "TASK_ACHIEVED"})

    snap = hub.snapshot(run_view.model_copy(update={"last_seq": hub.last_seq}))
    retained = snap.events
    assert len(retained) == 512
    assert snap.truncated_before == 48
    assert retained[0].seq == 49 and retained[-1].seq == 560
    for ev in retained:
        raw = json.dumps(ev.model_dump(mode="json"), ensure_ascii=False).encode("utf-8")
        assert len(raw) <= 16384

    # Oversized event had to be evicted cleanly; also test directly on a fresh event
    ev_big = RunEventHub(project_id="p", session_id="s", run_id="r").emit(
        "tool_result", {"action_type": "run_command", "stdout": big_text}
    )
    assert ev_big.payload["truncated"] is True
    assert ev_big.payload["original_bytes"] > 16384
    assert len(json.dumps(ev_big.model_dump(mode="json"), ensure_ascii=False).encode("utf-8")) <= 16384

    # Slow subscribers remained strictly bounded at SUBSCRIBER_QUEUE_MAXSIZE and marked overflowed
    for _, q in subs:
        assert q is not None
        assert q.queue.qsize() <= SUBSCRIBER_QUEUE_MAXSIZE
        assert q.overflowed is True
        hub.close_subscription(q)

    # Cursor validation: expired (409) & future (422) & exact replay
    with pytest.raises(AppError) as e_exp:
        hub.open_subscription(after=10)
    assert e_exp.value.status_code == 409
    assert e_exp.value.code == "CURSOR_EXPIRED"

    with pytest.raises(AppError) as e_fut:
        hub.open_subscription(after=561)
    assert e_fut.value.status_code == 422

    replay, sub = hub.open_subscription(after=500)
    assert sub is None
    assert [e.seq for e in replay] == [n for n in [e.seq for e in retained] if n > 500]


def test_a15_snapshot_reconnect_no_reexecution(tmp_path: Path) -> None:
    cfg = make_host_config(tmp_path)
    providers: list[CountingMetricsProvider] = []
    entered = asyncio.Event()
    proceed = asyncio.Event()

    class _GatedProvider:
        def __init__(self) -> None:
            self._base = CountingMetricsProvider()
            providers.append(self._base)

        async def complete(self, request: object) -> object:
            entered.set()
            await proceed.wait()
            return await self._base.complete(request)  # type: ignore[arg-type]

    async def _scenario() -> None:
        svc = AppService(config=cfg, provider_factory=lambda **_: _GatedProvider())  # type: ignore[arg-type]
        try:
            proj = await svc.create_project(name="reconnect-proj")
            sess = svc.create_session(
                project_id=proj.project_id,
                model=ModelSelection(provider="offline", model="offline-metrics-v1"),
            )
            run = await svc.start_run(
                session_id=sess.session_id,
                request=StartRunRequest(
                    client_request_id=uuid.uuid4(),
                    goal="实现 weighted_positive_total",
                    confirmed_scope=True,
                ),
            )
            await entered.wait()

            # Simulate initial client connecting, reading prefix, and disconnecting mid-run
            first_chunk: list[int] = []
            async for ev in svc.events(session_id=sess.session_id, run_id=run.run_id, after=0):
                first_chunk.append(ev.seq)
                if len(first_chunk) >= 2:
                    break
            assert first_chunk == [1, 2]

            # Disconnecting did NOT cancel run; fetch snapshot then resume from cursor
            snap = svc.snapshot(session_id=sess.session_id, run_id=run.run_id)
            assert snap.cursor == snap.run.last_seq
            assert all(e.seq <= snap.cursor for e in snap.events)

            resumed: list[int] = []

            async def _consume_rest() -> None:
                async for ev in svc.events(
                    session_id=sess.session_id, run_id=run.run_id, after=snap.cursor
                ):
                    resumed.append(ev.seq)

            consumer = asyncio.create_task(_consume_rest())
            await asyncio.sleep(0.05)
            proceed.set()
            await consumer

            assert all(seq > snap.cursor for seq in resumed)
            assert resumed == list(range(snap.cursor + 1, resumed[-1] + 1))
            assert len(providers) == 1
            assert providers[0].calls == 3
        finally:
            proceed.set()
            await svc.aclose()

    asyncio.run(_scenario())
