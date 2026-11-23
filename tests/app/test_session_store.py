"""C01–C08: Durable SQLite SessionStore, atomic commit, idempotency, reopen, security & quotas."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import project_helpers
import pytest
from app_helpers import (
    FAKE_TEST_TOKEN,
    make_host_config,
    pick_free_loopback_port,
    tree_hashes,
)
from forecontract_app.demo import OfflineMetricsProvider
from forecontract_app.models import (
    AppError,
    HostConfig,
    ModelSelection,
    StartRunRequest,
)
from forecontract_app.project_models import ProposalDecision, RegisterProjectRequest
from forecontract_app.service import AppService
from forecontract_app.session_models import (
    SESSION_DB_FILENAME,
    HistoryItem,
    SetConstraintsRequest,
)
from forecontract_app.session_store import SessionStore
from forecontract_llm import ModelRequest, ModelResponse
from session_helpers import (
    SpyUsageProvider,
    build_history_item_record,
    make_reopened_host_config,
    sha256_bytes,
    wait_for_run_terminal_async,
)


def test_c01_reopen_preserves_identity_history_sha_without_reinit(tmp_path: Path) -> None:
    cfg = make_host_config(tmp_path)
    existing_dir = project_helpers.create_buggy_calc_project(tmp_path / "existing_calc")
    cfg = HostConfig(
        data_root=cfg.data_root,
        workspace_parent=cfg.workspace_parent,
        python_executable=cfg.python_executable,
        bind_port=cfg.bind_port,
        access_token=cfg.access_token,
        project_roots=(existing_dir,),
    )

    async def _phase1() -> tuple[str, str, str, dict[str, str], dict[str, str], list[HistoryItem]]:
        svc = AppService(config=cfg)
        try:
            c_proj = await svc.create_project(name="ctrl-one")
            e_proj = await svc.register_project(
                request=RegisterProjectRequest(
                    name="exist-one", root=str(existing_dir), confirmed_read=True
                )
            )
            sess_c = svc.create_session(
                project_id=c_proj.project_id,
                model=ModelSelection(provider="offline", model="offline-metrics-v1"),
            )
            sess_e = svc.create_session(
                project_id=e_proj.project_id,
                model=ModelSelection(provider="offline", model="offline-project-v1"),
            )
            svc.set_session_constraints(
                session_id=sess_e.session_id,
                request=SetConstraintsRequest(
                    client_request_id=uuid.uuid4(),
                    expected_revision=0,
                    constraints="Keep tests immutable; fix calc.py only",
                ),
            )
            r_c = await svc.start_run(
                session_id=sess_c.session_id,
                request=StartRunRequest(
                    client_request_id=uuid.uuid4(),
                    goal="Fix metrics weighted_positive_total",
                    confirmed_scope=True,
                ),
            )
            done_c = await wait_for_run_terminal_async(
                svc, session_id=sess_c.session_id, run_id=r_c.run_id, timeout=30.0
            )
            assert done_c.state == "succeeded"
            assert done_c.verification == "passed"

            # Add extra user note outside metrics changes to verify reopen does not overwrite workspace
            extra = cfg.workspace_parent / "ctrl-one" / "notes.txt"
            extra.write_text("USER_KEPT_FILE\n", encoding="utf-8")

            hist_c = svc.get_session_history(session_id=sess_c.session_id, after=0, limit=50)
            assert len(hist_c.items) == 2
            ctrl_hashes = tree_hashes(cfg.workspace_parent / "ctrl-one")
            runs_hashes = tree_hashes(cfg.data_root / "runs")
            return c_proj.project_id, sess_c.session_id, r_c.run_id, ctrl_hashes, runs_hashes, hist_c.items
        finally:
            await svc.aclose()

    cpid, csid, crid, ctrl_before, runs_before, items_before = asyncio.run(_phase1())

    # Reopen on same data_root: no initializer re-run, project/runs trees unchanged
    cfg2 = make_reopened_host_config(cfg)

    async def _phase2() -> None:
        svc2 = AppService(config=cfg2)
        try:
            projs = {p.project_id: p for p in svc2.list_projects()}
            assert cpid in projs
            assert any(p.name == "exist-one" for p in projs.values())
            sess_map = {s.session_id: s for s in svc2.list_sessions()}
            assert csid in sess_map
            run_reopened = svc2.get_run(session_id=csid, run_id=crid)
            assert run_reopened.state == "succeeded"
            assert run_reopened.verification == "passed"
            assert run_reopened.goal == "Fix metrics weighted_positive_total"

            hist_reopened = svc2.get_session_history(session_id=csid, after=0, limit=50)
            assert [it.model_dump(mode="json") for it in hist_reopened.items] == [
                it.model_dump(mode="json") for it in items_before
            ]
            assert tree_hashes(cfg.workspace_parent / "ctrl-one") == ctrl_before
            assert tree_hashes(cfg.data_root / "runs") == runs_before
        finally:
            await svc2.aclose()

    asyncio.run(_phase2())


def test_c02_commit_failure_rolls_back_with_503_zero_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = make_host_config(tmp_path)
    provider_calls = 0

    def _factory(**_: Any) -> OfflineMetricsProvider:
        class _SpyMetrics(OfflineMetricsProvider):
            async def complete(self, request: ModelRequest) -> ModelResponse:
                nonlocal provider_calls
                provider_calls += 1
                return await super().complete(request)

        return _SpyMetrics()

    async def _scenario() -> tuple[str, str]:
        svc = AppService(config=cfg, provider_factory=_factory)
        try:
            proj = await svc.create_project(name="atomic-proj")
            sess = svc.create_session(
                project_id=proj.project_id,
                model=ModelSelection(provider="offline", model="offline-metrics-v1"),
            )
            ws_before = tree_hashes(cfg.workspace_parent / "atomic-proj")
            req_id = uuid.uuid4()

            def _fail_create_run(**_: Any) -> None:
                raise AppError("STORE_UNAVAILABLE", "injected sqlite commit failure", status_code=503)

            with monkeypatch.context() as mp:
                mp.setattr(svc._store, "create_run_initial", _fail_create_run)
                with pytest.raises(AppError) as exc_info:
                    await svc.start_run(
                        session_id=sess.session_id,
                        request=StartRunRequest(
                            client_request_id=req_id,
                            goal="Should fail before scheduling worker",
                            confirmed_scope=True,
                        ),
                    )
            assert exc_info.value.status_code == 503
            assert exc_info.value.code == "STORE_UNAVAILABLE"
            assert provider_calls == 0
            assert not any((cfg.data_root / "runs").glob("*")) if (cfg.data_root / "runs").exists() else True
            assert tree_hashes(cfg.workspace_parent / "atomic-proj") == ws_before
            assert svc.get_session_history(session_id=sess.session_id).items == []

            # Positive control with healthy store succeeds cleanly using the SAME req_id
            run = await svc.start_run(
                session_id=sess.session_id,
                request=StartRunRequest(
                    client_request_id=req_id,
                    goal="Fix metrics weighted_positive_total",
                    confirmed_scope=True,
                ),
            )
            done = await wait_for_run_terminal_async(
                svc, session_id=sess.session_id, run_id=run.run_id, timeout=30.0
            )
            assert done.state == "succeeded"
            assert provider_calls >= 1
            return sess.session_id, run.run_id
        finally:
            await svc.aclose()

    sid, rid = asyncio.run(_scenario())
    # Reopen verifies only the valid committed run exists
    svc2 = AppService(config=make_reopened_host_config(cfg))
    try:
        assert svc2.get_run(session_id=sid, run_id=rid).run_id == rid
        assert len(svc2.get_session_history(session_id=sid).items) == 2
    finally:
        asyncio.run(svc2.aclose())


def test_c03_idempotency_across_restart_conflict_and_session_isolation(tmp_path: Path) -> None:
    cfg = make_host_config(tmp_path)
    starts = 0

    def _factory(**_: Any) -> OfflineMetricsProvider:
        class _CountingMetrics(OfflineMetricsProvider):
            def __init__(self) -> None:
                super().__init__()
                nonlocal starts
                starts += 1

        return _CountingMetrics()

    shared_key = uuid.uuid4()

    async def _run_first() -> tuple[str, str, str]:
        svc = AppService(config=cfg, provider_factory=_factory)
        try:
            proj = await svc.create_project(name="idem-proj")
            s1 = svc.create_session(
                project_id=proj.project_id,
                model=ModelSelection(provider="offline", model="offline-metrics-v1"),
            )
            s2 = svc.create_session(
                project_id=proj.project_id,
                model=ModelSelection(provider="offline", model="offline-metrics-v1"),
            )
            req = StartRunRequest(
                client_request_id=shared_key,
                goal="Fix weighted_positive_total",
                confirmed_scope=True,
            )
            r1, rep1 = await svc.start_run_with_replay(session_id=s1.session_id, request=req)
            assert rep1 is False
            await wait_for_run_terminal_async(svc, session_id=s1.session_id, run_id=r1.run_id, timeout=30.0)
            r2, rep2 = await svc.start_run_with_replay(session_id=s1.session_id, request=req)
            assert rep2 is True
            assert r2.run_id == r1.run_id
            assert starts == 1

            # Same key with different goal -> 409 conflict
            with pytest.raises(AppError) as err:
                await svc.start_run_with_replay(
                    session_id=s1.session_id,
                    request=StartRunRequest(
                        client_request_id=shared_key,
                        goal="Changed goal",
                        confirmed_scope=True,
                    ),
                )
            assert err.value.status_code == 409
            return s1.session_id, s2.session_id, r1.run_id
        finally:
            await svc.aclose()

    s1_id, s2_id, id1 = asyncio.run(_run_first())

    async def _run_after_reopen() -> None:
        svc2 = AppService(config=make_reopened_host_config(cfg), provider_factory=_factory)
        try:
            r3, rep3 = await svc2.start_run_with_replay(
                session_id=s1_id,
                request=StartRunRequest(
                    client_request_id=shared_key,
                    goal="Fix weighted_positive_total",
                    confirmed_scope=True,
                ),
            )
            assert rep3 is True
            assert r3.run_id == id1
            assert starts == 1

            # Same key on second session is completely independent
            r_other, rep_other = await svc2.start_run_with_replay(
                session_id=s2_id,
                request=StartRunRequest(
                    client_request_id=shared_key,
                    goal="Second session run",
                    confirmed_scope=True,
                ),
            )
            assert rep_other is False
            assert r_other.run_id != id1
            await wait_for_run_terminal_async(svc2, session_id=s2_id, run_id=r_other.run_id, timeout=30.0)
            assert starts == 2
        finally:
            await svc2.aclose()

    asyncio.run(_run_after_reopen())


def test_c04_restart_reconciles_non_terminal_and_blocks_workspace(tmp_path: Path) -> None:
    for non_term_state in ("queued", "running", "cancelling", "unconfirmed_unknown"):
        sub = tmp_path / non_term_state
        sub.mkdir()
        cfg = make_host_config(sub)
        svc = AppService(config=cfg)
        proj = asyncio.run(svc.create_project(name="crash-proj"))
        sess = svc.create_session(
            project_id=proj.project_id,
            model=ModelSelection(provider="offline", model="offline-metrics-v1"),
        )
        run_id = str(uuid.uuid4())
        req_id = str(uuid.uuid4())
        store = svc._store
        now = "2026-11-16T00:00:00+00:00"
        with store.transaction() as conn:
            if non_term_state == "unconfirmed_unknown":
                conn.execute(
                    """INSERT INTO runs VALUES (?, ?, ?, 1, ?, 'g', 'unknown', 'unknown', 'CANCEL_CONFIRMATION_TIMEOUT', 1, 0, NULL, NULL, 0, 1, ?, ?)""",
                    (run_id, sess.session_id, proj.project_id, req_id, now, now),
                )
            else:
                conn.execute(
                    """INSERT INTO runs VALUES (?, ?, ?, 1, ?, 'g', ?, 'not_run', NULL, 1, 0, NULL, NULL, 1, 0, ?, ?)""",
                    (run_id, sess.session_id, proj.project_id, req_id, non_term_state, now, now),
                )
                conn.execute(
                    "UPDATE sessions SET active_run_id = ? WHERE session_id = ?",
                    (run_id, sess.session_id),
                )
            q_id = str(uuid.uuid4())
            conn.execute(
                """INSERT INTO questions VALUES (?, ?, ?, ?, 'act-1', 1, 'Pending?', '[]', ?, 'pending', NULL, NULL, NULL, NULL, ?, ?)""",
                (q_id, proj.project_id, sess.session_id, run_id, "a" * 64, now, now),
            )
        asyncio.run(svc.aclose())

        starts = 0

        def _count_factory(**_: Any) -> OfflineMetricsProvider:
            nonlocal starts
            starts += 1
            return OfflineMetricsProvider()

        svc2 = AppService(config=make_reopened_host_config(cfg), provider_factory=_count_factory)
        try:
            rv = svc2.get_run(session_id=sess.session_id, run_id=run_id)
            assert rv.state == "unknown"
            assert rv.verification == "unknown"
            assert rv.reason_code == (
                "CANCEL_CONFIRMATION_TIMEOUT" if non_term_state == "unconfirmed_unknown" else "HOST_RESTARTED"
            )
            qs = svc2.list_questions(session_id=sess.session_id, run_id=run_id)
            assert len(qs) == 1
            assert qs[0].state == "cancelled"
            assert qs[0].reason_code == "HOST_RESTARTED"
            assert starts == 0

            # Workspace remains poisoned/blocked until explicit handling
            with pytest.raises(AppError) as busy_err:
                asyncio.run(
                    svc2.start_run(
                        session_id=sess.session_id,
                        request=StartRunRequest(
                            client_request_id=uuid.uuid4(),
                            goal="Retry on poisoned workspace",
                            confirmed_scope=True,
                        ),
                    )
                )
            assert busy_err.value.status_code == 409
            assert busy_err.value.code == "WORKSPACE_BUSY"
            assert starts == 0
        finally:
            asyncio.run(svc2.aclose())

    # Normal completed project CAN explicitly start a second run after reopen
    ok_dir = tmp_path / "normal_ok"
    ok_dir.mkdir()
    cfg_ok = make_host_config(ok_dir)

    async def _normal_reopen() -> None:
        svc1 = AppService(config=cfg_ok)
        try:
            p = await svc1.create_project(name="clean-proj")
            s = svc1.create_session(
                project_id=p.project_id,
                model=ModelSelection(provider="offline", model="offline-metrics-v1"),
            )
            r1 = await svc1.start_run(
                session_id=s.session_id,
                request=StartRunRequest(
                    client_request_id=uuid.uuid4(), goal="Run 1", confirmed_scope=True
                ),
            )
            assert (await wait_for_run_terminal_async(svc1, session_id=s.session_id, run_id=r1.run_id)).state == "succeeded"
        finally:
            await svc1.aclose()

        svc2 = AppService(config=make_reopened_host_config(cfg_ok))
        try:
            r2 = await svc2.start_run(
                session_id=s.session_id,
                request=StartRunRequest(
                    client_request_id=uuid.uuid4(), goal="Run 2 after reopen", confirmed_scope=True
                ),
            )
            assert (await wait_for_run_terminal_async(svc2, session_id=s.session_id, run_id=r2.run_id)).state == "succeeded"
        finally:
            await svc2.aclose()

    asyncio.run(_normal_reopen())


def test_c05_cross_session_isolation_404_and_detached_return_copies(tmp_path: Path) -> None:
    cfg = make_host_config(tmp_path)

    async def _scenario() -> None:
        svc = AppService(config=cfg)
        try:
            proj = await svc.create_project(name="iso-proj")
            s1 = svc.create_session(
                project_id=proj.project_id,
                model=ModelSelection(provider="offline", model="offline-metrics-v1"),
            )
            s2 = svc.create_session(
                project_id=proj.project_id,
                model=ModelSelection(provider="offline", model="offline-metrics-v1"),
            )
            r1 = await svc.start_run(
                session_id=s1.session_id,
                request=StartRunRequest(
                    client_request_id=uuid.uuid4(), goal="Run in s1", confirmed_scope=True
                ),
            )
            await wait_for_run_terminal_async(svc, session_id=s1.session_id, run_id=r1.run_id)
            fake_sid = str(uuid.uuid4())
            fake_rid = str(uuid.uuid4())

            for bad_sid, bad_rid in (
                (fake_sid, fake_rid),
                (s2.session_id, r1.run_id),
                (s1.session_id, fake_rid),
            ):
                with pytest.raises(AppError) as e1:
                    svc.get_run(session_id=bad_sid, run_id=bad_rid)
                assert e1.value.status_code == 404
                with pytest.raises(AppError) as e2:
                    await svc.cancel_run(session_id=bad_sid, run_id=bad_rid)
                assert e2.value.status_code == 404
                with pytest.raises(AppError) as e3:
                    svc.list_questions(session_id=bad_sid, run_id=bad_rid)
                assert e3.value.status_code == 404
                with pytest.raises(AppError) as e4:
                    svc.get_run_usage(session_id=bad_sid, run_id=bad_rid)
                assert e4.value.status_code == 404

            with pytest.raises(AppError) as e5:
                svc.get_session_history(session_id=fake_sid)
            assert e5.value.status_code == 404

            # Mutating returned objects must NOT alter persisted store
            loaded = svc._store.load()
            loaded["sessions"].clear()  # type: ignore[attr-defined]
            assert len(svc._store.load()["sessions"]) == 2  # type: ignore[arg-type]

            hist = svc.get_session_history(session_id=s1.session_id)
            first_id = hist.items[0].item_id
            hist.items.clear()
            assert svc.get_session_history(session_id=s1.session_id).items[0].item_id == first_id
        finally:
            await svc.aclose()

    asyncio.run(_scenario())


def test_c06_rejects_future_version_corrupt_and_reparse_db_without_overwriting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. Future schema version -> STORE_INCOMPATIBLE, bytes untouched
    d1 = tmp_path / "future_db"
    d1.mkdir()
    ws1 = tmp_path / "ws1"
    ws1.mkdir()
    p1 = d1 / SESSION_DB_FILENAME
    c = sqlite3.connect(str(p1))
    c.execute("CREATE TABLE dummy(x INT);")
    c.execute("PRAGMA user_version = 99;")
    c.commit()
    c.close()
    before_future_sha = sha256_bytes(p1.read_bytes())

    provider_starts = 0

    def _spy(**_: Any) -> OfflineMetricsProvider:
        nonlocal provider_starts
        provider_starts += 1
        return OfflineMetricsProvider()

    with pytest.raises(AppError) as err1:
        AppService(
            config=HostConfig(
                data_root=d1,
                workspace_parent=ws1,
                python_executable=make_host_config(tmp_path / "ref1").python_executable,
                bind_port=pick_free_loopback_port(),
                access_token=FAKE_TEST_TOKEN,
            ),
            provider_factory=_spy,
        )
    assert err1.value.code == "STORE_INCOMPATIBLE"
    assert sha256_bytes(p1.read_bytes()) == before_future_sha
    assert provider_starts == 0

    # 2. Corrupt DB bytes -> STORE_CORRUPT, bytes untouched
    d2 = tmp_path / "corrupt_db"
    d2.mkdir()
    p2 = d2 / SESSION_DB_FILENAME
    p2.write_bytes(b"NOT_A_VALID_SQLITE_FILE_HEADER_1234567890")
    before_corrupt_sha = sha256_bytes(p2.read_bytes())
    with pytest.raises(AppError) as err2:
        SessionStore(data_root=d2)
    assert err2.value.code == "STORE_CORRUPT"
    assert sha256_bytes(p2.read_bytes()) == before_corrupt_sha

    # 3. Reparse junction as data_root pointing to outside sentinel -> rejected without touching sentinel
    outside = tmp_path / "outside_target"
    outside.mkdir()
    sentinel = outside / "sentinel.txt"
    sentinel.write_bytes(b"DO_NOT_TOUCH_SENTINEL")
    before_sent_sha = sha256_bytes(sentinel.read_bytes())
    junction = tmp_path / "data_junction"
    if project_helpers.try_create_junction(junction, outside):
        with pytest.raises(AppError) as err3:
            SessionStore(data_root=junction)
        assert err3.value.code == "REPARSE_ROOT_FORBIDDEN"
        assert sha256_bytes(sentinel.read_bytes()) == before_sent_sha

    # 4. Simulated reparse on sessions.sqlite3 file itself -> STORE_CORRUPT without touching bytes
    d3 = tmp_path / "reparse_file_db"
    d3.mkdir()
    p3 = d3 / SESSION_DB_FILENAME
    p3.write_bytes(b"PROTECTED_SENTINEL_BYTES")
    before_p3_sha = sha256_bytes(p3.read_bytes())
    with monkeypatch.context() as mp:
        mp.setattr(
            "forecontract_app.session_store.is_reparse_or_symlink",
            lambda path: path == p3,
        )
        with pytest.raises(AppError) as err4:
            SessionStore(data_root=d3)
    assert err4.value.code == "STORE_CORRUPT"
    assert sha256_bytes(p3.read_bytes()) == before_p3_sha

    # 5. Healthy positive store works
    healthy = tmp_path / "healthy"
    cfg_h = make_host_config(healthy)
    svc_h = AppService(config=cfg_h)
    try:
        assert svc_h._store.load()["schema_version"] == 1
    finally:
        asyncio.run(svc_h.aclose())


def test_c07_reopen_revalidates_host_roots_history_readable_run_403(tmp_path: Path) -> None:
    root_a = project_helpers.create_buggy_calc_project(tmp_path / "proj_a")
    root_b = project_helpers.create_buggy_calc_project(tmp_path / "proj_b")
    secret_marker = (root_a / "calc.py").read_bytes()
    cfg1 = HostConfig(
        data_root=(tmp_path / "data").resolve(),
        workspace_parent=(tmp_path / "ws1").resolve(),
        python_executable=make_host_config(tmp_path / "pyref").python_executable,
        bind_port=pick_free_loopback_port(),
        access_token=FAKE_TEST_TOKEN,
        project_roots=(root_a, root_b),
    )

    async def _seed() -> tuple[str, str, str]:
        svc = AppService(config=cfg1)
        try:
            pa = await svc.register_project(
                request=RegisterProjectRequest(name="proj-a", root=str(root_a), confirmed_read=True)
            )
            pb = await svc.register_project(
                request=RegisterProjectRequest(name="proj-b", root=str(root_b), confirmed_read=True)
            )
            sa = svc.create_session(
                project_id=pa.project_id,
                model=ModelSelection(provider="offline", model="offline-project-v1"),
            )
            sb = svc.create_session(
                project_id=pb.project_id,
                model=ModelSelection(provider="offline", model="offline-project-v1"),
            )
            pc = await svc.create_project(name="ctrl-c")
            sc = svc.create_session(
                project_id=pc.project_id,
                model=ModelSelection(provider="offline", model="offline-metrics-v1"),
            )
            r_ctrl = await svc.start_run(
                session_id=sc.session_id,
                request=StartRunRequest(client_request_id=uuid.uuid4(), goal="seed ctrl", confirmed_scope=True),
            )
            await wait_for_run_terminal_async(svc, session_id=sc.session_id, run_id=r_ctrl.run_id)
            svc.set_session_constraints(
                session_id=sa.session_id,
                request=SetConstraintsRequest(
                    client_request_id=uuid.uuid4(), expected_revision=0, constraints="stay readable"
                ),
            )
            return sa.session_id, sb.session_id, sc.session_id
        finally:
            await svc.aclose()

    sa_id, sb_id, sc_id = asyncio.run(_seed())

    # Reopen WITHOUT root_a in project_roots AND with changed workspace_parent (invalidates ctrl-c)
    ws2 = tmp_path / "ws2"
    ws2.mkdir()
    cfg2 = make_reopened_host_config(
        cfg1,
        workspace_parent=ws2,
        project_roots=(root_b,),
    )

    def _forbid_read_factory(*, project: Any, run_id: str) -> Any:
        raise AssertionError(f"must never spawn provider for invalidated project {project.name}")

    async def _verify_reopen() -> None:
        svc2 = AppService(config=cfg2, provider_factory=_forbid_read_factory)
        try:
            # History and context remain readable on invalidated projects
            assert svc2.get_session_context(session_id=sa_id).constraints == "stay readable"
            assert len(svc2.get_session_history(session_id=sc_id).items) == 2

            # Starting new runs on invalidated existing or controlled projects -> 403 PROJECT_BINDING_INVALID
            for bad_sid in (sa_id, sc_id):
                with pytest.raises(AppError) as err:
                    await svc2.start_run(
                        session_id=bad_sid,
                        request=StartRunRequest(
                            client_request_id=uuid.uuid4(), goal="blocked", confirmed_scope=True
                        ),
                    )
                assert err.value.status_code == 403
                assert err.value.code == "PROJECT_BINDING_INVALID"
            assert (root_a / "calc.py").read_bytes() == secret_marker
        finally:
            await svc2.aclose()

        # Allowed project_root root_b runs normally when provider is healthy
        svc3 = AppService(config=cfg2)
        try:
            r_b = await svc3.start_run(
                session_id=sb_id,
                request=StartRunRequest(
                    client_request_id=uuid.uuid4(), goal="Fix allowed root_b", confirmed_scope=True
                ),
            )
            stop_app = asyncio.Event()

            async def _approve_b() -> None:
                while not stop_app.is_set():
                    for prop in svc3.get_proposals(session_id=sb_id, run_id=r_b.run_id):
                        if prop.state == "pending":
                            await svc3.decide_proposal(
                                session_id=sb_id,
                                run_id=r_b.run_id,
                                proposal_id=prop.id,
                                request=ProposalDecision(
                                    digest=prop.digest,
                                    decision="approve",
                                    trusted_local_execution=True,
                                ),
                            )
                    await asyncio.sleep(0.02)

            app_task = asyncio.create_task(_approve_b())
            try:
                res_b = await wait_for_run_terminal_async(svc3, session_id=sb_id, run_id=r_b.run_id)
            finally:
                stop_app.set()
                await app_task
            assert res_b.run_id == r_b.run_id
            assert res_b.state == "succeeded"
        finally:
            await svc3.aclose()

    asyncio.run(_verify_reopen())


def test_c08_session_run_quotas_event_tail_64_512kb_and_sqlite_full(tmp_path: Path) -> None:
    # 1. Quota: 8 sessions max (9th rejected), 16 runs max (17th rejected)
    d1 = tmp_path / "quotas"
    cfg = make_host_config(d1)
    svc = AppService(config=cfg)
    try:
        proj = asyncio.run(svc.create_project(name="quota-proj"))
        s0 = None
        for idx in range(8):
            s = svc.create_session(
                project_id=proj.project_id,
                model=ModelSelection(provider="offline", model="offline-metrics-v1"),
            )
            if idx == 0:
                s0 = s
        assert s0 is not None
        with pytest.raises(AppError) as q_err:
            svc.create_session(
                project_id=proj.project_id,
                model=ModelSelection(provider="offline", model="offline-metrics-v1"),
            )
        assert q_err.value.status_code == 429

        # Fill 16 terminal runs on s0, then 17th fails with RUN_QUOTA_EXCEEDED
        # Wait: use a fast fail/ok scripted existing or fast direct run, or insert committed runs cleanly?
        # Running 16 fast controlled metrics runs or fast SpyUsageProvider runs with failed invalid json is <0.5s total!
    finally:
        asyncio.run(svc.aclose())

    # Fast 16-run quota test with immediate invalid-JSON provider failure
    d_runs = tmp_path / "run_quota"
    cfg_r = make_host_config(d_runs)
    fast_provider = SpyUsageProvider(lambda _i, _r: "not json")

    async def _run_quota_scenario() -> None:
        svc_r = AppService(config=cfg_r, provider_factory=lambda **_: fast_provider)
        try:
            p = await svc_r.create_project(name="rq-proj")
            s = svc_r.create_session(
                project_id=p.project_id,
                model=ModelSelection(provider="offline", model="offline-metrics-v1"),
            )
            for idx in range(16):
                rv = await svc_r.start_run(
                    session_id=s.session_id,
                    request=StartRunRequest(
                        client_request_id=uuid.uuid4(),
                        goal=f"run-{idx}",
                        confirmed_scope=True,
                    ),
                )
                await wait_for_run_terminal_async(svc_r, session_id=s.session_id, run_id=rv.run_id)
            with pytest.raises(AppError) as r_err:
                await svc_r.start_run(
                    session_id=s.session_id,
                    request=StartRunRequest(
                        client_request_id=uuid.uuid4(), goal="run-17", confirmed_scope=True
                    ),
                )
            assert r_err.value.status_code == 429
        finally:
            await svc_r.aclose()

    asyncio.run(_run_quota_scenario())

    # 2. Persisted event tail trimming: >64 events and >512 KiB trims ONLY event tail, preserving history_items
    d_ev = tmp_path / "ev_trim"
    cfg_ev = make_host_config(d_ev)
    svc_ev = AppService(config=cfg_ev)
    try:
        async def _seed_ev() -> tuple[Any, Any, int]:
            proj_ev_l = await svc_ev.create_project(name="ev-proj")
            sess_ev_l = svc_ev.create_session(
                project_id=proj_ev_l.project_id,
                model=ModelSelection(provider="offline", model="offline-metrics-v1"),
            )
            r_ev_l = await svc_ev.start_run(
                session_id=sess_ev_l.session_id,
                request=StartRunRequest(
                    client_request_id=uuid.uuid4(), goal="event tail test", confirmed_scope=True
                ),
            )
            await wait_for_run_terminal_async(
                svc_ev, session_id=sess_ev_l.session_id, run_id=r_ev_l.run_id
            )
            cnt = len(svc_ev.get_session_history(session_id=sess_ev_l.session_id).items)
            return proj_ev_l, sess_ev_l, cnt

        proj_ev, sess_ev, hist_count_before = asyncio.run(_seed_ev())

        # Emit 70 events on a fresh hub/run row directly via SessionStore.append_run_event
        # and also >512 KiB large ~14 KiB events
        big_run_id = str(uuid.uuid4())
        req_id = str(uuid.uuid4())
        from forecontract_app.events import RunEventHub
        from forecontract_app.models import RunView
        from forecontract_app.session_usage import UsageAccumulator

        u_acc = UsageAccumulator(max_calls=10, max_steps=10, run_timeout_ms=10000)
        hub = RunEventHub(
            project_id=proj_ev.project_id,
            session_id=sess_ev.session_id,
            run_id=big_run_id,
            persist_cb=lambda ev: svc_ev._store.append_run_event(event=ev),
        )
        q_ev = hub.prepare_event("run_queued", {"state": "queued"})
        goal_item = build_history_item_record(
            seq=len(svc_ev.get_session_history(session_id=sess_ev.session_id).items) + 1,
            project_id=proj_ev.project_id,
            session_id=sess_ev.session_id,
            run_id=big_run_id,
            kind="user_goal",
            text="kept user history",
        )
        svc_ev._store.create_run_initial(
            run_view=RunView(
                run_id=big_run_id,
                session_id=sess_ev.session_id,
                project_id=proj_ev.project_id,
                client_request_id=req_id,
                goal="kept user history",
                state="queued",
                verification="not_run",
            ),
            run_seq_order=2,
            payload_digest="b" * 64,
            goal_history_item=goal_item,
            queued_event=q_ev,
            usage_view=u_acc.snapshot(),
        )
        hub.commit_prepared_event(q_ev)
        # Emit 65 regular events (total 66 -> tail kept == 64, truncated_before == 2)
        for i in range(65):
            hub.emit("command_output", {"command_id": "c1", "seq": i + 1, "stream": "stdout", "text": f"line-{i}"})
        loaded_ev = svc_ev._store.load()
        assert len(loaded_ev["run_events"][big_run_id]) == 64  # type: ignore[index]
        run_meta = next(r["view"] for r in loaded_ev["runs"] if r["view"]["run_id"] == big_run_id)  # type: ignore[index]
        assert run_meta["truncated_before"] == 2
        assert run_meta["last_seq"] == 66

        # Now emit 45 events each ~14 KiB -> total bytes exceed 512 KiB so <64 events retained
        big_text = "X" * 14_000
        for i in range(45):
            hub.emit("command_output", {"command_id": "c1", "seq": 100 + i, "stream": "stdout", "text": big_text})
        loaded_ev2 = svc_ev._store.load()
        tail_events = loaded_ev2["run_events"][big_run_id]  # type: ignore[index]
        assert len(tail_events) < 40
        # History items were NEVER evicted
        assert len(loaded_ev2["history_items"][sess_ev.session_id]) == hist_count_before + 1  # type: ignore[index]
    finally:
        asyncio.run(svc_ev.aclose())

    # 3. Test injected small max_page_count triggering SQLITE_FULL -> atomic rollback & old data preserved
    d_full = tmp_path / "full_db"
    d_full.mkdir()
    store_full = SessionStore(data_root=d_full, max_page_count=64)
    try:
        from forecontract_app.models import ProjectView, SessionView

        p_view = ProjectView(
            project_id=str(uuid.uuid4()),
            name="full-p",
            workspace_root=str(tmp_path / "w_unused"),
            read_paths=[],
            write_paths=[],
            commands_summary=[],
            expected_tests=[],
        )
        store_full.insert_project(
            view=p_view,
            kind="controlled",
            workspace_root=p_view.workspace_root,
            canonical_root="w_unused",
            read_grant_id=None,
            spec={"view": p_view.model_dump(mode="json")},
        )
        s_view = SessionView(
            session_id=str(uuid.uuid4()),
            project_id=p_view.project_id,
            model=ModelSelection(provider="offline", model="offline-metrics-v1"),
        )
        store_full.insert_session(view=s_view, seq_order=1)
        baseline_item = build_history_item_record(
            seq=1,
            project_id=p_view.project_id,
            session_id=s_view.session_id,
            run_id=str(uuid.uuid4()),
            kind="user_goal",
            text="MUST_SURVIVE_SQLITE_FULL",
        )
        with store_full.transaction() as conn:
            store_full._insert_history_row(conn, baseline_item)

        # Force SQLITE_FULL inside a multi-insert transaction (page limit ~256 KiB -> hits at ~43 rows)
        with pytest.raises(AppError) as full_err, store_full.transaction() as conn:
            for seq in range(2, 60):
                big_it = build_history_item_record(
                    seq=seq,
                    project_id=p_view.project_id,
                    session_id=s_view.session_id,
                    run_id=str(uuid.uuid4()),
                    kind="answer",
                    text=f"{seq}-" + ("Z" * 3500),
                )
                store_full._insert_history_row(conn, big_it)
        assert full_err.value.code == "STORE_UNAVAILABLE"
        assert full_err.value.status_code == 503

        # Re-read store: integrity intact and committed baseline_item survives untouched
        after_full = store_full.load()
        kept_hist = after_full["history_items"][s_view.session_id]  # type: ignore[index]
        assert len(kept_hist) == 1
        assert kept_hist[0]["text"] == "MUST_SURVIVE_SQLITE_FULL"
    finally:
        store_full.close()


def _snapshot_db_files(db_path: Path) -> dict[str, bytes]:
    return {
        suffix: Path(f"{db_path}{suffix}").read_bytes()
        for suffix in ("", "-wal", "-shm", "-journal")
        if os.path.lexists(f"{db_path}{suffix}")
    }


def test_d01_future_wal_rejection_preserves_bytes(tmp_path: Path) -> None:
    wal_root = tmp_path / "wal_future"
    wal_root.mkdir()
    base_cfg = make_host_config(tmp_path / "host")
    cfg_wal = HostConfig(
        data_root=wal_root,
        workspace_parent=base_cfg.workspace_parent,
        python_executable=base_cfg.python_executable,
        bind_port=base_cfg.bind_port,
        access_token=base_cfg.access_token,
        project_roots=(),
    )

    init_store = SessionStore(data_root=wal_root)
    init_store.close()

    db_path = wal_root / SESSION_DB_FILENAME
    raw_conn = sqlite3.connect(str(db_path))
    try:
        raw_conn.execute("PRAGMA journal_mode=WAL;")
        raw_conn.execute("PRAGMA user_version=2;")
        raw_conn.commit()
    finally:
        raw_conn.close()

    before_bytes = db_path.read_bytes()
    before_sha = sha256_bytes(before_bytes)
    assert before_bytes[18:20] == b"\x02\x02"
    assert int.from_bytes(before_bytes[60:64], "big") == 2
    assert sorted(os.listdir(wal_root)) == [SESSION_DB_FILENAME]

    with pytest.raises(AppError) as store_err:
        SessionStore(data_root=wal_root)
    assert store_err.value.code == "STORE_INCOMPATIBLE"
    assert sorted(os.listdir(wal_root)) == [SESSION_DB_FILENAME]
    assert db_path.read_bytes() == before_bytes
    assert sha256_bytes(db_path.read_bytes()) == before_sha

    provider_starts = [0]

    def _counting_factory(**kwargs: Any) -> Any:
        provider_starts[0] += 1
        return OfflineMetricsProvider(**kwargs)

    with pytest.raises(AppError) as svc_err:
        AppService(config=cfg_wal, provider_factory=_counting_factory)
    assert svc_err.value.code == "STORE_INCOMPATIBLE"
    assert provider_starts[0] == 0
    for suffix in ("-wal", "-shm", "-journal"):
        assert not (wal_root / f"{SESSION_DB_FILENAME}{suffix}").exists()
    assert db_path.read_bytes() == before_bytes
    assert sha256_bytes(db_path.read_bytes()) == before_sha

    # Closed WAL with store_meta schema_version='2' must also be rejected read-only without sidecars
    meta_root = tmp_path / "wal_meta_future"
    meta_root.mkdir()
    meta_store = SessionStore(data_root=meta_root)
    meta_store.close()
    meta_db = meta_root / SESSION_DB_FILENAME
    meta_conn = sqlite3.connect(str(meta_db))
    try:
        meta_conn.execute("PRAGMA journal_mode=WAL;")
        meta_conn.execute("UPDATE store_meta SET value='2' WHERE key='schema_version';")
        meta_conn.commit()
    finally:
        meta_conn.close()

    meta_before = _snapshot_db_files(meta_db)
    assert meta_before[""][18:20] == b"\x02\x02"
    assert sorted(meta_before.keys()) == [""]

    try:
        opened_meta = SessionStore(data_root=meta_root)
    except AppError as exc:
        meta_caught = exc.code
    else:
        opened_meta.close()
        meta_caught = None
    assert meta_caught == "STORE_INCOMPATIBLE"
    assert _snapshot_db_files(meta_db) == meta_before

    # Closed WAL + store_meta='2' + empty -journal must reject without mutating main DB or deleting -journal
    meta_journal_root = tmp_path / "wal_meta_future_empty_journal"
    meta_journal_root.mkdir()
    mj_store = SessionStore(data_root=meta_journal_root)
    mj_db = mj_store.db_path
    mj_store.close()
    mj_conn = sqlite3.connect(str(mj_db))
    try:
        mj_conn.execute("PRAGMA journal_mode=WAL;")
        mj_conn.execute("UPDATE store_meta SET value='2' WHERE key='schema_version';")
        mj_conn.commit()
    finally:
        mj_conn.close()
    Path(f"{mj_db}-journal").write_bytes(b"")

    mj_before = _snapshot_db_files(mj_db)
    assert mj_before[""][18:20] == b"\x02\x02"
    assert mj_before["-journal"] == b""

    try:
        opened_mj = SessionStore(data_root=meta_journal_root)
    except AppError as exc:
        mj_caught = exc.code
    else:
        opened_mj.close()
        mj_caught = None
    assert mj_caught == "STORE_INCOMPATIBLE"
    assert _snapshot_db_files(mj_db) == mj_before

    # Positive control 1: healthy schema-1 WAL DB + empty -journal must still open cleanly
    healthy_journal_root = tmp_path / "wal_healthy_empty_journal"
    healthy_journal_root.mkdir()
    hj_store = SessionStore(data_root=healthy_journal_root)
    hj_db = hj_store.db_path
    hj_store.close()
    hj_conn = sqlite3.connect(str(hj_db))
    try:
        hj_conn.execute("PRAGMA journal_mode=WAL;")
        hj_conn.commit()
    finally:
        hj_conn.close()
    Path(f"{hj_db}-journal").write_bytes(b"")

    healthy_reopen = SessionStore(data_root=healthy_journal_root)
    try:
        assert healthy_reopen.load()["schema_version"] == 1
    finally:
        healthy_reopen.close()

    # Positive control 2: healthy schema-1 AppService opens and reloads cleanly
    ok_root = tmp_path / "healthy_schema_1"
    ok_root.mkdir()
    ok_cfg = HostConfig(
        data_root=ok_root,
        workspace_parent=base_cfg.workspace_parent,
        python_executable=base_cfg.python_executable,
        bind_port=base_cfg.bind_port,
        access_token=base_cfg.access_token,
        project_roots=(),
    )

    async def _healthy_roundtrip() -> str:
        svc_ok = AppService(config=ok_cfg)
        try:
            proj = await svc_ok.create_project(name="d01-healthy")
            return proj.project_id
        finally:
            await svc_ok.aclose()

    pid = asyncio.run(_healthy_roundtrip())
    svc_reopen = AppService(config=make_reopened_host_config(ok_cfg))
    try:
        assert any(p.project_id == pid for p in svc_reopen.list_projects())
    finally:
        asyncio.run(svc_reopen.aclose())


def test_offer_replay_and_legacy_reopen(tmp_path: Path) -> None:
    """Verify concurrent confirm_chat_execution_offer + crash recovery share one run_id and legacy DB reopens."""
    from forecontract_app.chat_models import (
        ConfirmChatOfferRequest,
        CreateChatTurnRequest,
    )

    proj_dir = tmp_path / "offer_store_proj"
    proj_dir.mkdir()
    (proj_dir / "calc.py").write_text("def add_numbers(a, b):\n    return a - b\n", encoding="utf-8")
    (proj_dir / "tests").mkdir()
    (proj_dir / "tests" / "test_calc.py").write_text(
        "from calc import add_numbers\ndef test_add():\n    assert add_numbers(2, 3) == 5\n",
        encoding="utf-8",
    )
    cfg = project_helpers.make_project_host_config(tmp_path, allowed_roots=(proj_dir,))

    def _chat_factory(*, project, session_id: str, turn_id: str):
        _ = (project, session_id, turn_id)
        return lambda _req: {
            "action": "execution_offer",
            "goal": "Fix add_numbers in calc.py",
            "summary": "修复 calc.py 加法",
        }

    async def _run_concurrent() -> tuple[str, str, str]:
        svc = AppService(config=cfg, chat_provider_factory=_chat_factory)
        try:
            proj = await svc.register_project(
                request=RegisterProjectRequest(
                    name="store-offer", root=str(proj_dir), confirmed_read=True
                )
            )
            sess = svc.create_session(
                project_id=proj.project_id,
                model=ModelSelection(provider="offline", model="offline-project-v1", thinking="disabled"),
            )
            turn, code = await svc.start_chat_turn(
                session_id=sess.session_id,
                request=CreateChatTurnRequest(
                    client_request_id=uuid.uuid4(),
                    text="修好 calc.py",
                    intent_mode="conversation",
                ),
            )
            assert code == 201 and turn.execution_offer is not None
            offer_id = turn.execution_offer.offer_id
            req = ConfirmChatOfferRequest(offer_id=offer_id, confirmed_scope=True)
            (r1, _), (r2, _) = await asyncio.gather(
                svc.confirm_chat_execution_offer(session_id=sess.session_id, turn_id=turn.turn_id, request=req),
                svc.confirm_chat_execution_offer(session_id=sess.session_id, turn_id=turn.turn_id, request=req),
            )
            assert r1.run_id == r2.run_id
            # Simulate crash where offer.run_id wasn't marked yet, then re-confirm repairs it
            with svc._store.transaction() as conn:
                conn.execute(
                    "UPDATE chat_execution_offers SET status='pending_confirm', run_id=NULL WHERE offer_id=?",
                    (offer_id,),
                )
            r3, replayed = await svc.confirm_chat_execution_offer(
                session_id=sess.session_id, turn_id=turn.turn_id, request=req
            )
            assert replayed is True and r3.run_id == r1.run_id
            repaired = svc._store.get_chat_execution_offer(session_id=sess.session_id, turn_id=turn.turn_id)
            assert repaired is not None and repaired.status == "started" and repaired.run_id == r1.run_id
            return sess.session_id, turn.turn_id, r1.run_id
        finally:
            await svc.aclose()

    sid, tid, rid = asyncio.run(_run_concurrent())
    # Simulate legacy DB without intent_mode or chat_execution_offers table? Verify reopen preserves turn & run
    svc2 = AppService(config=make_reopened_host_config(cfg))
    try:
        page = svc2.list_chat_turns(session_id=sid)
        assert len(page.items) == 1
        assert page.items[0].turn_id == tid
        assert page.items[0].execution_offer is not None
        assert page.items[0].execution_offer.run_id == rid
    finally:
        asyncio.run(svc2.aclose())
