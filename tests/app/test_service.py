"""A07–A12: Idempotency, workspace serialization, session isolation, cancel, and quotas."""

from __future__ import annotations

import asyncio
import time
import uuid
from pathlib import Path

import httpx
import pytest
from app_helpers import (
    CountingMetricsProvider,
    LiveTcpTestServer,
    async_wait_until,
    auth_headers,
    make_host_config,
    tree_hashes,
    wait_until,
)
from forecontract_app.models import (
    AppError,
    ModelSelection,
    ProjectView,
    StartRunRequest,
)
from forecontract_app.service import AppService
from forecontract_llm import ModelRequest, ModelResponse


def test_a07_idempotency(tmp_path: Path) -> None:
    cfg = make_host_config(tmp_path)
    created: list[CountingMetricsProvider] = []

    def _factory(*, project: ProjectView, run_id: str) -> CountingMetricsProvider:
        _ = (project, run_id)
        p = CountingMetricsProvider()
        created.append(p)
        return p

    with LiveTcpTestServer(config=cfg, provider_factory=_factory) as srv:
        h = auth_headers(cfg)
        proj = httpx.post(
            f"{srv.base_url}/api/v1/projects", headers=h, json={"name": "idem-proj"}, timeout=5.0
        ).json()
        sess = httpx.post(
            f"{srv.base_url}/api/v1/sessions",
            headers=h,
            json={
                "project_id": proj["project_id"],
                "model": {"provider": "offline", "model": "offline-metrics-v1"},
            },
            timeout=5.0,
        ).json()
        sid = sess["session_id"]
        req_id = str(uuid.uuid4())
        body = {"client_request_id": req_id, "goal": "实现 weighted_positive_total", "confirmed_scope": True}

        r1 = httpx.post(f"{srv.base_url}/api/v1/sessions/{sid}/runs", headers=h, json=body, timeout=5.0)
        r2 = httpx.post(f"{srv.base_url}/api/v1/sessions/{sid}/runs", headers=h, json=body, timeout=5.0)
        assert r1.status_code == 202
        assert r2.status_code == 200
        assert r1.json()["run_id"] == r2.json()["run_id"]
        rid = r1.json()["run_id"]

        conflict = httpx.post(
            f"{srv.base_url}/api/v1/sessions/{sid}/runs",
            headers=h,
            json={"client_request_id": req_id, "goal": "不同需求", "confirmed_scope": True},
            timeout=5.0,
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

        terminal = wait_until(
            lambda: (
                res := httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{sid}/runs/{rid}",
                    headers=auth_headers(cfg, with_origin=False),
                    timeout=5.0,
                ).json()
            )["state"]
            in ("succeeded", "failed", "cancelled", "unknown")
            and res
        )
        assert terminal["state"] == "succeeded"

        after_terminal = httpx.post(
            f"{srv.base_url}/api/v1/sessions/{sid}/runs", headers=h, json=body, timeout=5.0
        )
        assert after_terminal.status_code == 200
        assert after_terminal.json()["run_id"] == rid
        assert len(created) == 1
        run_dirs = [p for p in (cfg.data_root / "runs").iterdir() if p.is_dir()]
        assert len(run_dirs) == 1


def test_a08_workspace_serialization(tmp_path: Path) -> None:
    cfg = make_host_config(tmp_path)
    active_count = 0
    max_simultaneous_writers = 0
    invocations = 0
    gate = asyncio.Event()
    entered = asyncio.Event()

    class _SlowSerializedProvider:
        def __init__(self) -> None:
            nonlocal invocations
            invocations += 1
            self._inner = CountingMetricsProvider()

        async def complete(self, request: ModelRequest) -> ModelResponse:
            nonlocal active_count, max_simultaneous_writers
            active_count += 1
            max_simultaneous_writers = max(max_simultaneous_writers, active_count)
            entered.set()
            try:
                await gate.wait()
                return await self._inner.complete(request)
            finally:
                active_count -= 1

    async def _scenario() -> None:
        svc = AppService(config=cfg, provider_factory=lambda **_: _SlowSerializedProvider())
        try:
            proj = await svc.create_project(name="serial-proj")
            s1 = svc.create_session(
                project_id=proj.project_id,
                model=ModelSelection(provider="offline", model="offline-metrics-v1"),
            )
            s2 = svc.create_session(
                project_id=proj.project_id,
                model=ModelSelection(provider="offline", model="offline-metrics-v1"),
            )
            req_a = StartRunRequest(client_request_id=uuid.uuid4(), goal="a", confirmed_scope=True)
            req_b = StartRunRequest(client_request_id=uuid.uuid4(), goal="b", confirmed_scope=True)

            barrier = asyncio.Barrier(2)
            codes: list[int] = []
            busy_codes: list[str] = []

            async def _launch(sid: str, req: StartRunRequest) -> None:
                await barrier.wait()
                try:
                    await svc.start_run(session_id=sid, request=req)
                    codes.append(202)
                except AppError as exc:
                    codes.append(exc.status_code)
                    busy_codes.append(exc.code)

            await asyncio.gather(_launch(s1.session_id, req_a), _launch(s1.session_id, req_b))
            assert sorted(codes) == [202, 409]
            assert busy_codes == ["WORKSPACE_BUSY"]

            await entered.wait()
            with pytest.raises(AppError) as e_other:
                await svc.start_run(
                    session_id=s2.session_id,
                    request=StartRunRequest(client_request_id=uuid.uuid4(), goal="c", confirmed_scope=True),
                )
            assert e_other.value.code == "WORKSPACE_BUSY"
            assert invocations == 1
            assert max_simultaneous_writers == 1
            gate.set()
            await async_wait_until(lambda: not svc._active_workspaces)
        finally:
            gate.set()
            await svc.aclose()

    asyncio.run(_scenario())


def test_a09_session_identity(tmp_path: Path) -> None:
    cfg = make_host_config(tmp_path)
    with LiveTcpTestServer(config=cfg) as srv:
        h = auth_headers(cfg)
        proj = httpx.post(
            f"{srv.base_url}/api/v1/projects", headers=h, json={"name": "sess-iso"}, timeout=5.0
        ).json()
        s1 = httpx.post(
            f"{srv.base_url}/api/v1/sessions",
            headers=h,
            json={
                "project_id": proj["project_id"],
                "model": {"provider": "offline", "model": "offline-metrics-v1"},
            },
            timeout=5.0,
        ).json()
        s2 = httpx.post(
            f"{srv.base_url}/api/v1/sessions",
            headers=h,
            json={
                "project_id": proj["project_id"],
                "model": {"provider": "offline", "model": "offline-metrics-v1"},
            },
            timeout=5.0,
        ).json()
        run = httpx.post(
            f"{srv.base_url}/api/v1/sessions/{s1['session_id']}/runs",
            headers=h,
            json={"client_request_id": str(uuid.uuid4()), "goal": "g", "confirmed_scope": True},
            timeout=5.0,
        ).json()
        rid = run["run_id"]

        cross_get = httpx.get(
            f"{srv.base_url}/api/v1/sessions/{s2['session_id']}/runs/{rid}",
            headers=auth_headers(cfg, with_origin=False),
            timeout=5.0,
        )
        cross_cancel = httpx.post(
            f"{srv.base_url}/api/v1/sessions/{s2['session_id']}/runs/{rid}/cancel",
            headers=h,
            json={},
            timeout=5.0,
        )
        cross_events = httpx.get(
            f"{srv.base_url}/api/v1/sessions/{s2['session_id']}/runs/{rid}/events?after=0",
            headers=auth_headers(cfg, with_origin=False),
            timeout=5.0,
        )
        assert cross_get.status_code == 404
        assert cross_cancel.status_code == 404
        assert cross_events.status_code == 404

        finished = wait_until(
            lambda: (
                res := httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{s1['session_id']}/runs/{rid}",
                    headers=auth_headers(cfg, with_origin=False),
                    timeout=5.0,
                ).json()
            )["state"]
            in ("succeeded", "failed", "cancelled", "unknown")
            and res
        )
        assert finished["state"] == "succeeded"


def test_a10_cancel_waiting_provider(tmp_path: Path) -> None:
    cfg = make_host_config(tmp_path)
    entered = asyncio.Event()
    calls_after_cancel = 0
    cancelled_seen = False

    class _CooperativeWaitingProvider:
        async def complete(self, request: ModelRequest) -> ModelResponse:
            nonlocal calls_after_cancel
            _ = request
            if cancelled_seen:
                calls_after_cancel += 1
            entered.set()
            await asyncio.sleep(60.0)
            raise AssertionError("unreachable")

    async def _scenario() -> None:
        nonlocal cancelled_seen
        svc = AppService(config=cfg, provider_factory=lambda **_: _CooperativeWaitingProvider())
        try:
            proj = await svc.create_project(name="cancel-ok")
            source_before = tree_hashes(cfg.workspace_parent / "cancel-ok")
            sess = svc.create_session(
                project_id=proj.project_id,
                model=ModelSelection(provider="offline", model="offline-metrics-v1"),
            )
            run = await svc.start_run(
                session_id=sess.session_id,
                request=StartRunRequest(client_request_id=uuid.uuid4(), goal="cancel me", confirmed_scope=True),
            )
            await entered.wait()
            cancelled_seen = True
            t0 = time.monotonic()
            mid = await svc.cancel_run(session_id=sess.session_id, run_id=run.run_id)
            assert mid.state == "cancelling"
            cancelled = await async_wait_until(
                lambda: (
                    cur := svc.get_run(session_id=sess.session_id, run_id=run.run_id)
                ).state
                == "cancelled"
                and cur,
                timeout=5.0,
            )
            elapsed = time.monotonic() - t0
            assert elapsed <= 5.0
            assert cancelled.state == "cancelled"
            assert cancelled.verification != "passed"
            assert tree_hashes(cfg.workspace_parent / "cancel-ok") == source_before
            assert calls_after_cancel == 0
            snap = svc.snapshot(session_id=sess.session_id, run_id=run.run_id)
            assert sum(1 for e in snap.events if e.kind == "run_finished") == 1
            again = await svc.cancel_run(session_id=sess.session_id, run_id=run.run_id)
            assert again.state == "cancelled"
        finally:
            await svc.aclose()

    asyncio.run(_scenario())


def test_a11_cancel_unknown_keeps_lock(tmp_path: Path) -> None:
    cfg = make_host_config(tmp_path)
    dependency_entered = asyncio.Event()
    release_dependency = asyncio.Event()

    class _UncooperativeProvider:
        async def complete(self, request: ModelRequest) -> ModelResponse:
            _ = request
            dependency_entered.set()
            while not release_dependency.is_set():
                try:
                    await release_dependency.wait()
                except asyncio.CancelledError:
                    # Deliberately swallow cancellation until finally releases dependency.
                    continue
            return ModelResponse(
                schema_version="0.1",
                request_id=request.request_id,
                status="ok",
                content="{}",
            )

    async def _scenario() -> None:
        svc = AppService(config=cfg, provider_factory=lambda **_: _UncooperativeProvider())
        try:
            proj = await svc.create_project(name="cancel-unknown")
            sess = svc.create_session(
                project_id=proj.project_id,
                model=ModelSelection(provider="offline", model="offline-metrics-v1"),
            )
            run = await svc.start_run(
                session_id=sess.session_id,
                request=StartRunRequest(client_request_id=uuid.uuid4(), goal="stuck", confirmed_scope=True),
            )
            await dependency_entered.wait()
            t0 = time.monotonic()
            await svc.cancel_run(session_id=sess.session_id, run_id=run.run_id)
            final_view = await async_wait_until(
                lambda: (
                    cur := svc.get_run(session_id=sess.session_id, run_id=run.run_id)
                ).state
                in ("cancelled", "unknown", "failed", "succeeded")
                and cur,
                timeout=6.0,
            )
            cleanup_elapsed = time.monotonic() - t0
            assert cleanup_elapsed <= 5.5
            assert final_view.state == "unknown"

            with pytest.raises(AppError) as e_busy:
                await svc.start_run(
                    session_id=sess.session_id,
                    request=StartRunRequest(client_request_id=uuid.uuid4(), goal="next", confirmed_scope=True),
                )
            assert e_busy.value.code == "WORKSPACE_BUSY"
        finally:
            release_dependency.set()
            await asyncio.sleep(0.05)

        after_late = svc.get_run(session_id=sess.session_id, run_id=run.run_id)
        assert after_late.state == "unknown"
        snap = svc.snapshot(session_id=sess.session_id, run_id=run.run_id)
        assert sum(1 for e in snap.events if e.kind == "run_finished") == 1
        await svc.aclose()

    asyncio.run(_scenario())


def test_a12_quotas_and_detached_views(tmp_path: Path) -> None:
    cfg = make_host_config(tmp_path, max_sessions=8, max_runs_per_session=16)
    with LiveTcpTestServer(config=cfg) as srv:
        h = auth_headers(cfg)
        proj = httpx.post(
            f"{srv.base_url}/api/v1/projects", headers=h, json={"name": "quota-proj"}, timeout=5.0
        ).json()
        sids: list[str] = []
        for _ in range(8):
            r = httpx.post(
                f"{srv.base_url}/api/v1/sessions",
                headers=h,
                json={
                    "project_id": proj["project_id"],
                    "model": {"provider": "offline", "model": "offline-metrics-v1"},
                },
                timeout=5.0,
            )
            assert r.status_code == 201
            sids.append(r.json()["session_id"])

        ninth = httpx.post(
            f"{srv.base_url}/api/v1/sessions",
            headers=h,
            json={
                "project_id": proj["project_id"],
                "model": {"provider": "offline", "model": "offline-metrics-v1"},
            },
            timeout=5.0,
        )
        assert ninth.status_code == 429

        sid = sids[0]
        last_rid = ""
        for idx in range(16):
            rr = httpx.post(
                f"{srv.base_url}/api/v1/sessions/{sid}/runs",
                headers=h,
                json={"client_request_id": str(uuid.uuid4()), "goal": f"g-{idx}", "confirmed_scope": True},
                timeout=5.0,
            )
            assert rr.status_code == 202
            last_rid = rr.json()["run_id"]
            wait_until(
                lambda rid=last_rid: httpx.get(
                    f"{srv.base_url}/api/v1/sessions/{sid}/runs/{rid}",
                    headers=auth_headers(cfg, with_origin=False),
                    timeout=5.0,
                ).json()["state"]
                in ("succeeded", "failed", "cancelled", "unknown")
            )

        seventeenth = httpx.post(
            f"{srv.base_url}/api/v1/sessions/{sid}/runs",
            headers=h,
            json={"client_request_id": str(uuid.uuid4()), "goal": "overflow", "confirmed_scope": True},
            timeout=5.0,
        )
        assert seventeenth.status_code == 429
        run_dirs = [p for p in (cfg.data_root / "runs").iterdir() if p.is_dir()]
        assert len(run_dirs) == 16

    # Detached snapshot copy check directly via AppService
    async def _detached_check() -> None:
        svc = AppService(config=make_host_config(tmp_path / "det"))
        try:
            proj = await svc.create_project(name="detached-proj")
            sess = svc.create_session(
                project_id=proj.project_id,
                model=ModelSelection(provider="offline", model="offline-metrics-v1"),
            )
            run = await svc.start_run(
                session_id=sess.session_id,
                request=StartRunRequest(client_request_id=uuid.uuid4(), goal="check", confirmed_scope=True),
            )
            await async_wait_until(
                lambda: svc.get_run(session_id=sess.session_id, run_id=run.run_id).state == "succeeded"
            )
            snap = svc.snapshot(session_id=sess.session_id, run_id=run.run_id)
            assert snap.events
            snap.events.clear()
            snap_again = svc.snapshot(session_id=sess.session_id, run_id=run.run_id)
            assert len(snap_again.events) > 0
        finally:
            await svc.aclose()

    asyncio.run(_detached_check())
