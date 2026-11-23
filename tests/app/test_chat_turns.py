"""Targeted offline tests for CHAT-T01 SQLite compatibility, idempotency, read-only bounds, and HTTP scope."""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import uuid
from pathlib import Path
from typing import Any

import project_helpers
import pytest
from fastapi.testclient import TestClient
from forecontract_app.chat_models import (
    ChatProviderRequest,
    ChatProviderResponse,
    CreateChatTurnRequest,
)
from forecontract_app.models import AppError, ModelSelection, StartRunRequest
from forecontract_app.project_models import RegisterProjectRequest
from forecontract_app.server import create_app
from forecontract_app.service import AppService
from forecontract_app.session_models import SESSION_DB_FILENAME
from forecontract_app.session_store import SessionStore
from test_session_store import (
    make_host_config,
    make_reopened_host_config,
    wait_for_run_terminal_async,
)


def _sha256_bytes(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_turn_storage_compatibility(tmp_path: Path) -> None:
    proj_dir = project_helpers.create_buggy_calc_project(tmp_path / "proj_compat")
    cfg = make_host_config(tmp_path)
    cfg = type(cfg)(
        data_root=cfg.data_root,
        workspace_parent=cfg.workspace_parent,
        python_executable=cfg.python_executable,
        bind_port=cfg.bind_port,
        access_token=cfg.access_token,
        project_roots=(proj_dir,),
    )

    async def _seed_old_v1() -> tuple[str, list[tuple[int, str, str, str]]]:
        svc = AppService(config=cfg)
        try:
            proj = await svc.create_project(name="compat-demo")
            sess = svc.create_session(
                project_id=proj.project_id,
                model=ModelSelection(provider="offline", model="offline-metrics-v1"),
            )
            run = await svc.start_run(
                session_id=sess.session_id,
                request=StartRunRequest(
                    client_request_id=uuid.uuid4(),
                    goal="Fix metrics weighted_positive_total",
                    confirmed_scope=True,
                ),
            )
            done = await wait_for_run_terminal_async(
                svc, session_id=sess.session_id, run_id=run.run_id, timeout=30.0
            )
            assert done.state == "succeeded"
            hist = svc.get_session_history(session_id=sess.session_id, after=0, limit=50)
            snap = [(it.seq, it.item_id, it.text, it.sha256) for it in hist.items]
            return sess.session_id, run.run_id, snap
        finally:
            await svc.aclose()

    sid, rid, hist_before = asyncio.run(_seed_old_v1())
    assert len(hist_before) == 2

    # Strip chat_turns & its index to produce a genuine legacy v1 SQLite database without chat_turns
    db_path = cfg.data_root / SESSION_DB_FILENAME
    legacy_tables = {
        "store_meta",
        "projects",
        "sessions",
        "runs",
        "run_idempotency",
        "constraint_idempotency",
        "history_items",
        "questions",
        "run_events",
    }
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("DROP INDEX IF EXISTS idx_chat_turns_session_seq;")
        conn.execute("DROP TABLE IF EXISTS chat_turns;")
        conn.commit()
        uv_pre = conn.execute("PRAGMA user_version;").fetchone()[0]
        meta_pre = conn.execute(
            "SELECT value FROM store_meta WHERE key='schema_version'"
        ).fetchone()[0]
        tables_pre = {
            str(r[0])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        assert uv_pre == 1
        assert meta_pre == "1"
        assert "chat_turns" not in tables_pre
        assert legacy_tables.issubset(tables_pre)
    finally:
        conn.close()

    # First reopen with new host: adds chat_turns, preserves session/run/history item-by-item
    cfg2 = make_reopened_host_config(cfg)
    svc2 = AppService(config=cfg2)
    try:
        assert any(s.session_id == sid for s in svc2.list_sessions())
        run_after_1 = svc2.get_run(session_id=sid, run_id=rid)
        assert run_after_1.run_id == rid
        assert run_after_1.state == "succeeded"
        hist_after = svc2.get_session_history(session_id=sid, after=0, limit=50)
        snap_after = [(it.seq, it.item_id, it.text, it.sha256) for it in hist_after.items]
        assert snap_after == hist_before

        conn = sqlite3.connect(str(db_path))
        try:
            uv = conn.execute("PRAGMA user_version;").fetchone()[0]
            meta = conn.execute(
                "SELECT value FROM store_meta WHERE key='schema_version'"
            ).fetchone()[0]
            tbl = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='chat_turns'"
            ).fetchone()
            assert uv == 1
            assert meta == "1"
            assert tbl is not None
        finally:
            conn.close()
    finally:
        asyncio.run(svc2.aclose())

    # Second reopen: still identical item-by-item
    svc3 = AppService(config=make_reopened_host_config(cfg))
    try:
        run_after_2 = svc3.get_run(session_id=sid, run_id=rid)
        assert run_after_2.run_id == rid
        assert run_after_2.state == "succeeded"
        hist_after_2 = svc3.get_session_history(session_id=sid, after=0, limit=50)
        snap_after_2 = [(it.seq, it.item_id, it.text, it.sha256) for it in hist_after_2.items]
        assert snap_after_2 == hist_before
    finally:
        asyncio.run(svc3.aclose())

    # Future version (user_version=2) DB must be rejected without creating chat_turns or mutating bytes.
    future_root = tmp_path / "future_data"
    future_root.mkdir(parents=True, exist_ok=True)
    future_db = future_root / SESSION_DB_FILENAME
    seed_store = SessionStore(data_root=future_root)
    seed_store.close()
    conn = sqlite3.connect(str(future_db))
    try:
        conn.execute("DROP TABLE IF EXISTS chat_turns;")
        conn.execute("PRAGMA user_version = 2;")
        conn.commit()
    finally:
        conn.close()
    before_future_sha = _sha256_bytes(future_db)

    with pytest.raises(AppError) as exc_info:
        SessionStore(data_root=future_root)
    assert exc_info.value.code == "STORE_INCOMPATIBLE"
    assert _sha256_bytes(future_db) == before_future_sha

    conn = sqlite3.connect(str(future_db))
    try:
        tbl = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='chat_turns'"
        ).fetchone()
        assert tbl is None
    finally:
        conn.close()


def test_turn_idempotency_and_recovery(tmp_path: Path) -> None:
    proj_dir = project_helpers.create_buggy_calc_project(tmp_path / "proj_idem")
    cfg = project_helpers.make_project_host_config(tmp_path, allowed_roots=(proj_dir,))

    provider_calls = 0
    mode = "ok"

    async def _run_test() -> None:
        nonlocal provider_calls, mode
        gate_started = asyncio.Event()
        gate_release = asyncio.Event()

        def _factory(**_kw: Any):
            async def _provider(req: ChatProviderRequest) -> ChatProviderResponse:
                nonlocal provider_calls
                provider_calls += 1
                if mode == "slow":
                    gate_started.set()
                    await gate_release.wait()
                    return ChatProviderResponse(
                        action="final",
                        assistant_message=f"Slow reply to {req.question}",
                    )
                if mode == "fail":
                    raise RuntimeError("boom")
                return ChatProviderResponse(
                    action="final",
                    assistant_message=f"Reply #{provider_calls}: {req.question}",
                )

            return _provider

        svc = AppService(config=cfg, chat_provider_factory=_factory)
        try:
            proj = await svc.register_project(
                request=RegisterProjectRequest(
                    name="idem-proj", root=str(proj_dir), confirmed_read=True
                )
            )
            sess = svc.create_session(
                project_id=proj.project_id,
                model=ModelSelection(provider="offline", model="offline-project-v1"),
            )

            # 1. Concurrent same-key request returns 202 while first is running; provider called once.
            mode = "slow"
            k1 = uuid.uuid4()
            t1 = asyncio.create_task(
                svc.start_chat_turn(
                    session_id=sess.session_id,
                    request=CreateChatTurnRequest(client_request_id=k1, text="First question"),
                )
            )
            await asyncio.wait_for(gate_started.wait(), timeout=5.0)
            turn_inflight, code_inflight = await svc.start_chat_turn(
                session_id=sess.session_id,
                request=CreateChatTurnRequest(client_request_id=k1, text="First question"),
            )
            assert code_inflight == 202
            assert turn_inflight.status == "running"
            gate_release.set()
            turn1, code1 = await t1
            assert code1 == 201
            assert turn1.status == "completed"
            assert provider_calls == 1

            # Replay same key + same payload -> 200, no extra provider call
            replay1, code_r1 = await svc.start_chat_turn(
                session_id=sess.session_id,
                request={"client_request_id": k1, "text": "First question"},  # type: ignore[arg-type]
            )
            assert code_r1 == 200
            assert replay1.turn_id == turn1.turn_id
            assert provider_calls == 1

            # Same key + different payload -> 409 IDEMPOTENCY_CONFLICT
            with pytest.raises(AppError) as conflict_exc:
                await svc.start_chat_turn(
                    session_id=sess.session_id,
                    request={"client_request_id": k1, "text": "Different question"},  # type: ignore[arg-type]
                )
            assert conflict_exc.value.code == "IDEMPOTENCY_CONFLICT"

            # 2. Provider failure -> failed status, no half-baked assistant_message
            mode = "fail"
            k_fail = uuid.uuid4()
            turn_fail, code_fail = await svc.start_chat_turn(
                session_id=sess.session_id,
                request={"client_request_id": k_fail, "text": "Fail please"},  # type: ignore[arg-type]
            )
            assert code_fail == 201
            assert turn_fail.status == "failed"
            assert turn_fail.assistant_message is None
            assert turn_fail.error_code == "CHAT_PROVIDER_FAILED"

            # 3. Add 2 more completed turns for cursor pagination
            mode = "ok"
            for idx in range(2):
                _, c = await svc.start_chat_turn(
                    session_id=sess.session_id,
                    request={"client_request_id": uuid.uuid4(), "text": f"Extra {idx}"},  # type: ignore[arg-type]
                )
                assert c == 201

            p1 = svc.list_chat_turns(session_id=sess.session_id, after_seq=0, limit=2)
            assert len(p1.items) == 2
            assert p1.has_more is True
            p2 = svc.list_chat_turns(
                session_id=sess.session_id, after_seq=p1.next_after_seq, limit=2
            )
            assert len(p2.items) == 2
            assert p2.has_more is False
            all_seqs = [it.seq for it in p1.items + p2.items]
            assert all_seqs == [1, 2, 3, 4]

            # 4. Simulate interrupted turn left running across restart
            svc._store.insert_chat_turn_running(
                turn_id=str(uuid.uuid4()),
                project_id=proj.project_id,
                session_id=sess.session_id,
                client_request_id=str(uuid.uuid4()),
                payload_digest="a" * 64,
                user_message="Interrupted prompt",
                provider_mode="offline",
                created_at="2026-11-20T00:00:00+00:00",
            )
        finally:
            await svc.aclose()

        calls_before_restart = provider_calls
        svc2 = AppService(config=make_reopened_host_config(cfg), chat_provider_factory=_factory)
        try:
            page = svc2.list_chat_turns(session_id=sess.session_id, after_seq=0, limit=10)
            last = page.items[-1]
            assert last.status == "interrupted"
            assert last.user_message == "Interrupted prompt"
            assert last.assistant_message is None
            assert provider_calls == calls_before_restart
        finally:
            await svc2.aclose()

    asyncio.run(_run_test())


def test_readonly_context_and_actions(tmp_path: Path) -> None:
    proj_dir = project_helpers.create_buggy_calc_project(tmp_path / "proj_ro")
    before_calc_sha = _sha256_bytes(proj_dir / "calc.py")
    cfg = project_helpers.make_project_host_config(tmp_path, allowed_roots=(proj_dir,))

    seen_requests: list[ChatProviderRequest] = []

    async def _run_test() -> None:
        # Part A: Default OfflineReadonlyChatProvider reacts to actual files & test names
        svc = AppService(config=cfg)
        try:
            proj = await svc.register_project(
                request=RegisterProjectRequest(
                    name="ro-proj", root=str(proj_dir), confirmed_read=True
                )
            )
            sess = svc.create_session(
                project_id=proj.project_id,
                model=ModelSelection(provider="offline", model="offline-project-v1"),
            )
            t1, c1 = await svc.start_chat_turn(
                session_id=sess.session_id,
                request={"client_request_id": uuid.uuid4(), "text": "calc.py 有什么问题？"},  # type: ignore[arg-type]
            )
            assert c1 == 201
            assert t1.status == "completed"
            assert t1.assistant_message is not None
            assert "calc.py" in t1.assistant_message
            assert "return a - b" in t1.assistant_message
            assert "尚未" in t1.assistant_message

            # Modify fixture test file externally -> answer changes accordingly
            (proj_dir / "tests" / "test_calc.py").write_text(
                "from calc import add_numbers\n\ndef test_renamed_custom_probe() -> None:\n    assert add_numbers(2, 3) == 5\n",
                encoding="utf-8",
            )
            t2, c2 = await svc.start_chat_turn(
                session_id=sess.session_id,
                request={"client_request_id": uuid.uuid4(), "text": "追问测试覆盖范围"},  # type: ignore[arg-type]
            )
            assert c2 == 201
            assert t2.status == "completed"
            assert t2.assistant_message is not None
            assert "test_renamed_custom_probe" in t2.assistant_message
            assert "承接上一轮" in t2.assistant_message
            assert _sha256_bytes(proj_dir / "calc.py") == before_calc_sha
            assert svc.list_sessions()[0].active_run_id is None
        finally:
            await svc.aclose()

        # Part B: Spy provider verifies 2nd-turn context, write rejection, escape rejection, and 8-tool budget
        mode = "normal"

        def _spy_factory(**_kw: Any):
            def _provider(req: ChatProviderRequest) -> ChatProviderResponse:
                seen_requests.append(req)
                if mode == "forbidden_write":
                    return ChatProviderResponse(
                        action="tool_call",
                        tool_name="edit_file",
                        tool_args={"path": "calc.py"},
                    )
                if mode == "escape_read":
                    if not req.tool_observations:
                        return ChatProviderResponse(
                            action="tool_call",
                            tool_name="read_file",
                            tool_args={"path": "../secret.txt"},
                        )
                    obs = req.tool_observations[0]
                    assert obs["ok"] is False
                    return ChatProviderResponse(
                        action="final",
                        assistant_message=f"Rejected with {obs['error_code']}",
                    )
                if mode == "budget":
                    return ChatProviderResponse(
                        action="tool_call",
                        tool_name="list_directory",
                        tool_args={"path": "."},
                    )
                return ChatProviderResponse(
                    action="final",
                    assistant_message=f"ok:{len(req.recent_turns)}",
                )

            return _provider

        svc2 = AppService(config=make_reopened_host_config(cfg), chat_provider_factory=_spy_factory)
        try:
            sess_id = svc2.list_sessions()[0].session_id
            t3, _ = await svc2.start_chat_turn(
                session_id=sess_id,
                request={"client_request_id": uuid.uuid4(), "text": "Second context check"},  # type: ignore[arg-type]
            )
            assert t3.status == "completed"
            assert len(seen_requests[-1].recent_turns) >= 2

            mode = "forbidden_write"
            t_forb, _ = await svc2.start_chat_turn(
                session_id=sess_id,
                request={"client_request_id": uuid.uuid4(), "text": "Try write"},  # type: ignore[arg-type]
            )
            assert t_forb.status == "failed"
            assert t_forb.error_code == "CHAT_TOOL_FORBIDDEN"
            assert t_forb.assistant_message is None

            mode = "escape_read"
            t_esc, _ = await svc2.start_chat_turn(
                session_id=sess_id,
                request={"client_request_id": uuid.uuid4(), "text": "Try escape"},  # type: ignore[arg-type]
            )
            assert t_esc.status == "completed"
            assert t_esc.tool_reads[0].ok is False

            mode = "budget"
            t_bud, _ = await svc2.start_chat_turn(
                session_id=sess_id,
                request={"client_request_id": uuid.uuid4(), "text": "Exceed budget"},  # type: ignore[arg-type]
            )
            assert t_bud.status == "failed"
            assert t_bud.error_code == "TOOL_BUDGET_EXCEEDED"
            assert len(t_bud.tool_reads) == 8
        finally:
            await svc2.aclose()

    asyncio.run(_run_test())


def test_chat_http_scope_and_contention(tmp_path: Path) -> None:
    proj_a = project_helpers.create_buggy_calc_project(tmp_path / "proj_a")
    proj_b = project_helpers.create_buggy_calc_project(tmp_path / "proj_b")
    (proj_b / "only_in_b.py").write_text("VALUE_B = 42\n", encoding="utf-8")
    cfg = project_helpers.make_project_host_config(tmp_path, allowed_roots=(proj_a, proj_b))
    headers = {
        "Authorization": f"Bearer {cfg.access_token}",
        "Origin": f"http://127.0.0.1:{cfg.bind_port}",
    }

    app = create_app(config=cfg)
    with TestClient(app, base_url=f"http://127.0.0.1:{cfg.bind_port}") as client:
        pa = client.post(
            "/api/v1/projects/register",
            headers=headers,
            json={"name": "proj-a", "root": str(proj_a), "confirmed_read": True},
        ).json()
        pb = client.post(
            "/api/v1/projects/register",
            headers=headers,
            json={"name": "proj-b", "root": str(proj_b), "confirmed_read": True},
        ).json()

        sa = client.post(
            "/api/v1/sessions",
            headers=headers,
            json={
                "project_id": pa["project_id"],
                "model": {"provider": "offline", "model": "offline-project-v1"},
            },
        ).json()
        sb = client.post(
            "/api/v1/sessions",
            headers=headers,
            json={
                "project_id": pb["project_id"],
                "model": {"provider": "offline", "model": "offline-project-v1"},
            },
        ).json()

        # Unauthenticated rejected
        unauth = client.post(
            f"/api/v1/sessions/{sa['session_id']}/chat/turns",
            headers={"Origin": f"http://127.0.0.1:{cfg.bind_port}"},
            json={"client_request_id": str(uuid.uuid4()), "text": "hi"},
        )
        assert unauth.status_code == 401

        # Cross-session / missing lookup -> 404
        k_a = str(uuid.uuid4())
        r_a = client.post(
            f"/api/v1/sessions/{sa['session_id']}/chat/turns",
            headers=headers,
            json={"client_request_id": k_a, "text": "Question in A\nsecond line"},
        )
        assert r_a.status_code == 201
        r_a_dup = client.post(
            f"/api/v1/sessions/{sa['session_id']}/chat/turns",
            headers=headers,
            json={"client_request_id": k_a, "text": "Question in A\nsecond line"},
        )
        assert r_a_dup.status_code == 200
        r_a_conf = client.post(
            f"/api/v1/sessions/{sa['session_id']}/chat/turns",
            headers=headers,
            json={"client_request_id": k_a, "text": "Changed"},
        )
        assert r_a_conf.status_code == 409

        # Oversized (> 8 KiB) input -> 422
        r_big = client.post(
            f"/api/v1/sessions/{sa['session_id']}/chat/turns",
            headers=headers,
            json={"client_request_id": str(uuid.uuid4()), "text": "x" * 9000},
        )
        assert r_big.status_code == 422

        # Cross-session by-request -> 404
        cross = client.get(
            f"/api/v1/sessions/{sb['session_id']}/chat/turns/by-request/{k_a}",
            headers=headers,
        )
        assert cross.status_code == 404

        # Set constraints on A and ask in B -> B titles/constraints/files stay isolated
        client.put(
            f"/api/v1/sessions/{sa['session_id']}/context",
            headers=headers,
            json={
                "client_request_id": str(uuid.uuid4()),
                "expected_revision": 0,
                "constraints": "SECRET_CONSTRAINT_A",
            },
        )
        r_b = client.post(
            f"/api/v1/sessions/{sb['session_id']}/chat/turns",
            headers=headers,
            json={"client_request_id": str(uuid.uuid4()), "text": "only_in_b.py 有什么？"},
        )
        assert r_b.status_code == 201
        b_msg = r_b.json()["assistant_message"]
        assert "only_in_b.py" in b_msg
        assert "SECRET_CONSTRAINT_A" not in b_msg

        titles_a = client.get(
            f"/api/v1/projects/{pa['project_id']}/chat/session-titles",
            headers=headers,
        ).json()
        assert len(titles_a["titles"]) == 1
        assert titles_a["titles"][0]["title"] == "Question in A"

        # Real entry-point mutual contention:
        # 1) Active run holds workspace -> POST /chat/turns returns 409 and records zero new turns
        import threading
        import time

        turns_before_run = client.get(
            f"/api/v1/sessions/{sa['session_id']}/chat/turns",
            headers=headers,
        ).json()["items"]
        run_resp = client.post(
            f"/api/v1/sessions/{sa['session_id']}/runs",
            headers=headers,
            json={
                "client_request_id": str(uuid.uuid4()),
                "goal": "Hold workspace for contention test",
                "confirmed_scope": True,
            },
        )
        assert run_resp.status_code == 202
        held_run_id = run_resp.json()["run_id"]
        try:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                cur_run = client.get(
                    f"/api/v1/sessions/{sa['session_id']}/runs/{held_run_id}",
                    headers=headers,
                ).json()
                if cur_run["state"] == "running":
                    break
                time.sleep(0.02)

            busy_chat = client.post(
                f"/api/v1/sessions/{sa['session_id']}/chat/turns",
                headers=headers,
                json={"client_request_id": str(uuid.uuid4()), "text": "Blocked by real run"},
            )
            assert busy_chat.status_code == 409
            turns_during_run = client.get(
                f"/api/v1/sessions/{sa['session_id']}/chat/turns",
                headers=headers,
            ).json()["items"]
            assert len(turns_during_run) == len(turns_before_run)
        finally:
            cancel_resp = client.post(
                f"/api/v1/sessions/{sa['session_id']}/runs/{held_run_id}/cancel",
                headers=headers,
                json={},
            )
            assert cancel_resp.status_code == 202
            cancel_deadline = time.monotonic() + 5.0
            last_state: str | None = None
            last_reason: str | None = None
            confirmed_cancelled = False
            while time.monotonic() < cancel_deadline:
                cur_run = client.get(
                    f"/api/v1/sessions/{sa['session_id']}/runs/{held_run_id}",
                    headers=headers,
                ).json()
                last_state = cur_run.get("state")
                last_reason = cur_run.get("reason_code")
                if last_state == "cancelled" and last_reason == "USER_CANCELLED":
                    confirmed_cancelled = True
                    break
                if last_state in {"succeeded", "failed", "unknown"}:
                    break
                time.sleep(0.02)
            safe_state = (
                last_state
                if last_state
                in {"queued", "running", "cancelling", "succeeded", "failed", "cancelled", "unknown"}
                else "other"
            )
            safe_reason = last_reason if last_reason in {"USER_CANCELLED"} else "other"
            assert confirmed_cancelled, (
                f"run did not reach cancelled/USER_CANCELLED: state={safe_state} reason={safe_reason}"
            )
            db_conn = sqlite3.connect(str(cfg.data_root / SESSION_DB_FILENAME))
            try:
                row = db_conn.execute(
                    "SELECT cleanup_confirmed, poisoned_workspace FROM runs WHERE run_id = ?",
                    (held_run_id,),
                ).fetchone()
                assert row is not None and row[0] == 1 and row[1] == 0
            finally:
                db_conn.close()

        # 2) Active chat turn inside async offline provider -> POST /runs returns 409 and creates zero new runs
        svc: AppService = app.state.service
        chat_entered = threading.Event()
        chat_release = threading.Event()
        orig_chat_factory = svc._chat_provider_factory

        def _blocking_chat_factory(**_kw: Any):
            async def _provider(req: ChatProviderRequest) -> ChatProviderResponse:
                chat_entered.set()
                deadline = time.monotonic() + 5.0
                while not chat_release.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                return ChatProviderResponse(
                    action="final",
                    assistant_message=f"Released: {req.question}",
                )

            return _provider

        svc._chat_provider_factory = _blocking_chat_factory
        chat_results: list[int] = []

        def _run_chat_worker() -> None:
            r = client.post(
                f"/api/v1/sessions/{sa['session_id']}/chat/turns",
                headers=headers,
                json={"client_request_id": str(uuid.uuid4()), "text": "Hold chat turn"},
            )
            chat_results.append(r.status_code)

        worker = threading.Thread(target=_run_chat_worker, daemon=True)
        worker.start()
        try:
            if not chat_entered.wait(timeout=5.0):
                chat_release.set()
                worker.join(timeout=2.0)
                raise AssertionError(
                    f"chat provider was not entered; worker_status={chat_results} worker_alive={worker.is_alive()}"
                )
            runs_before = len(svc._runs_by_id)
            busy_run = client.post(
                f"/api/v1/sessions/{sa['session_id']}/runs",
                headers=headers,
                json={
                    "client_request_id": str(uuid.uuid4()),
                    "goal": "Blocked by real chat",
                    "confirmed_scope": True,
                },
            )
            assert busy_run.status_code == 409
            assert len(svc._runs_by_id) == runs_before
        finally:
            chat_release.set()
            worker.join(timeout=5.0)
            svc._chat_provider_factory = orig_chat_factory
        assert not worker.is_alive() and chat_results == [201]

        # Invalidated project root -> 403 PROJECT_BINDING_INVALID
        svc._projects[pa["project_id"]].binding_valid = False
        invalid_root_resp = client.post(
            f"/api/v1/sessions/{sa['session_id']}/chat/turns",
            headers=headers,
            json={"client_request_id": str(uuid.uuid4()), "text": "After invalid root"},
        )
        assert invalid_root_resp.status_code == 403


def test_conversation_offer_readonly_and_scope(tmp_path: Path) -> None:
    """Verify readonly rejects execution_offer, conversation persists offer, and confirm enforces scope & binding."""
    proj_dir = project_helpers.create_buggy_calc_project(tmp_path / "offer_proj")
    before_calc = (proj_dir / "calc.py").read_bytes()
    cfg = project_helpers.make_project_host_config(tmp_path, allowed_roots=(proj_dir,))

    provider_reqs: list[Any] = []

    def _chat_factory(*, project, session_id: str, turn_id: str):
        _ = (project, session_id, turn_id)

        def _provider(req):
            provider_reqs.append(req)
            if "修好" in req.question:
                return {
                    "action": "execution_offer",
                    "goal": "Fix add_numbers in calc.py and run pytest",
                    "summary": "将把 calc.py 的减法修复为加法并运行测试",
                }
            return {"action": "final", "assistant_message": "calc.py 第 4 行使用了减法。"}

        return _provider

    app = create_app(config=cfg, chat_provider_factory=_chat_factory)
    headers = {"Authorization": f"Bearer {cfg.access_token}", "Origin": f"http://127.0.0.1:{cfg.bind_port}"}
    with TestClient(app, base_url=f"http://127.0.0.1:{cfg.bind_port}") as client:
        pa_resp = client.post(
            "/api/v1/projects/register",
            headers=headers,
            json={"name": "offer-a", "root": str(proj_dir), "confirmed_read": True},
        )
        assert pa_resp.status_code == 201, pa_resp.text
        pa = pa_resp.json()
        sa = client.post(
            "/api/v1/sessions",
            headers=headers,
            json={
                "project_id": pa["project_id"],
                "model": {"provider": "offline", "model": "offline-project-v1", "thinking": "disabled"},
            },
        ).json()
        sb = client.post(
            "/api/v1/sessions",
            headers=headers,
            json={
                "project_id": pa["project_id"],
                "model": {"provider": "offline", "model": "offline-project-v1", "thinking": "disabled"},
            },
        ).json()
        sid = sa["session_id"]

        # 1. readonly mode rejects execution_offer from provider
        ro_resp = client.post(
            f"/api/v1/sessions/{sid}/chat/turns",
            headers=headers,
            json={
                "client_request_id": str(uuid.uuid4()),
                "text": "修好 calc.py",
                "intent_mode": "readonly",
            },
        )
        assert ro_resp.status_code == 201
        ro_body = ro_resp.json()
        assert ro_body["status"] == "failed"
        assert ro_body["error_code"] == "CHAT_PROVIDER_INVALID"
        assert ro_body["execution_offer"] is None
        assert (proj_dir / "calc.py").read_bytes() == before_calc

        # 2. conversation mode persists execution_offer (pending_confirm) with zero runs
        conv_resp = client.post(
            f"/api/v1/sessions/{sid}/chat/turns",
            headers=headers,
            json={
                "client_request_id": str(uuid.uuid4()),
                "text": "修好 calc.py",
                "intent_mode": "conversation",
            },
        )
        assert conv_resp.status_code == 201
        conv_body = conv_resp.json()
        assert conv_body["status"] == "completed"
        offer = conv_body["execution_offer"]
        assert offer is not None
        assert offer["status"] == "pending_confirm"
        assert offer["run_id"] is None
        assert (proj_dir / "calc.py").read_bytes() == before_calc

        # 3. confirm with confirmed_scope=False -> 403, zero runs
        unconf = client.post(
            f"/api/v1/sessions/{sid}/chat/turns/{conv_body['turn_id']}/confirm",
            headers=headers,
            json={"offer_id": offer["offer_id"], "confirmed_scope": False},
        )
        assert unconf.status_code == 403

        # 4. wrong session -> 404; wrong turn -> 404
        wrong_sess = client.post(
            f"/api/v1/sessions/{sb['session_id']}/chat/turns/{conv_body['turn_id']}/confirm",
            headers=headers,
            json={"offer_id": offer["offer_id"], "confirmed_scope": True},
        )
        assert wrong_sess.status_code == 404
        wrong_turn = client.post(
            f"/api/v1/sessions/{sid}/chat/turns/{uuid.uuid4()}/confirm",
            headers=headers,
            json={"offer_id": offer["offer_id"], "confirmed_scope": True},
        )
        assert wrong_turn.status_code == 404

        # 5. confirmed -> starts 1 run, re-confirm replays same run_id
        c1 = client.post(
            f"/api/v1/sessions/{sid}/chat/turns/{conv_body['turn_id']}/confirm",
            headers=headers,
            json={"offer_id": offer["offer_id"], "confirmed_scope": True},
        )
        assert c1.status_code == 201
        rid = c1.json()["run_id"]
        c2 = client.post(
            f"/api/v1/sessions/{sid}/chat/turns/{conv_body['turn_id']}/confirm",
            headers=headers,
            json={"offer_id": offer["offer_id"], "confirmed_scope": True},
        )
        assert c2.status_code == 200
        assert c2.json()["run_id"] == rid
        turns_after = client.get(f"/api/v1/sessions/{sid}/chat/turns", headers=headers).json()["items"]
        matched = next(t for t in turns_after if t["turn_id"] == conv_body["turn_id"])
        assert matched["execution_offer"]["status"] == "started"
        assert matched["execution_offer"]["run_id"] == rid
        assert list(provider_reqs[-1].evidence or []) == []

        # 6. Cross-page (>50 turns) auto-selection prefers newest offer-bound terminal run with valid diff
        import time

        from forecontract_app.chat_models import ChatExecutionOfferView
        from forecontract_app.models import RunView
        from forecontract_app.service import RunEventHub, _RunRecord

        svc = app.state.service
        sess_rec = svc._sessions[sid]
        proj_rec = svc._projects[pa["project_id"]]
        old_rid = str(uuid.uuid4())
        new_rid = str(uuid.uuid4())
        old_b_sha = "a" * 64
        old_a_sha = "b" * 64
        new_b_sha = "c" * 64
        new_a_sha = "d" * 64
        def _seed_turn(idx: int, offer_run_id: str | None = None) -> None:
            t_id = str(uuid.uuid4())
            req_id = str(uuid.uuid4())
            run_t = svc._store.insert_chat_turn_running(
                turn_id=t_id,
                project_id=pa["project_id"],
                session_id=sid,
                client_request_id=req_id,
                payload_digest=f"{idx:064x}",
                user_message=f"seed-{idx}",
                provider_mode="offline",
                created_at="2026-11-22T00:00:00Z",
                intent_mode="conversation",
            )
            off = None
            if offer_run_id is not None:
                off = ChatExecutionOfferView(
                    offer_id=str(uuid.uuid4()),
                    session_id=sid,
                    turn_id=t_id,
                    client_request_id=req_id,
                    goal="seeded",
                    summary="seeded",
                    status="started",
                    run_id=offer_run_id,
                    run_client_request_id=str(uuid.uuid4()),
                    created_at="2026-11-22T00:00:00Z",
                    updated_at="2026-11-22T00:00:00Z",
                )
            svc._store.finalize_chat_turn(
                turn=run_t.model_copy(
                    update={
                        "status": "completed",
                        "assistant_message": "ok",
                        "execution_offer": off,
                        "updated_at": "2026-11-22T00:00:00Z",
                    }
                )
            )

        def _seed_on_host_loop() -> None:
            for r_id, b_sha, a_sha, old_ln, new_ln in (
                (old_rid, old_b_sha, old_a_sha, "return old_v", "return old_fixed"),
                (new_rid, new_b_sha, new_a_sha, "return new_v", "return new_fixed"),
            ):
                r_view = RunView(
                    run_id=r_id,
                    session_id=sid,
                    project_id=pa["project_id"],
                    client_request_id=str(uuid.uuid4()),
                    goal="seeded",
                    state="succeeded",
                    verification="passed",
                    reason_code="OK",
                    result={
                        "diff": {
                            "entries": [
                                {
                                    "path": "calc.py",
                                    "before_sha256": b_sha,
                                    "after_sha256": a_sha,
                                    "diff_excerpt": f"@@ -1,1 +1,1 @@\n-{old_ln}\n+{new_ln}\n",
                                }
                            ]
                        }
                    },
                )
                sess_rec.runs[r_id] = _RunRecord(
                    view=r_view,
                    hub=RunEventHub(
                        project_id=r_view.project_id,
                        session_id=r_view.session_id,
                        run_id=r_view.run_id,
                    ),
                    session=sess_rec,
                    project=proj_rec,
                )
            _seed_turn(1, old_rid)
            for i in range(2, 56):
                _seed_turn(i, None)
            _seed_turn(56, new_rid)

        assert client.portal is not None
        client.portal.call(_seed_on_host_loop)

        def _sample_locks() -> dict[str, Any]:
            pid = pa["project_id"]
            root_key = proj_rec.canonical_root
            run_rec = sess_rec.runs.get(rid)
            return {
                "project_active": pid in svc._active_workspaces,
                "project_poisoned": pid in svc._poisoned_workspaces,
                "root_active": root_key in svc._active_root_keys,
                "root_poisoned": root_key in svc._poisoned_root_keys,
                "chat_project_active": pid in svc._active_chat_projects,
                "chat_session_active": sid in svc._active_chat_sessions,
                "in_flight_run_id": rid,
                "in_flight_run_state": run_rec.view.state if run_rec else None,
            }

        locks_before_busy = client.portal.call(_sample_locks)
        calls_before_busy = len(provider_reqs)
        busy_resp = client.post(
            f"/api/v1/sessions/{sid}/chat/turns",
            headers=headers,
            json={
                "client_request_id": str(uuid.uuid4()),
                "text": "刚才改了什么",
                "intent_mode": "conversation",
            },
        )
        locks_after_busy = client.portal.call(_sample_locks)
        assert busy_resp.status_code == 409
        busy_err = busy_resp.json().get("error") or {}
        assert busy_err.get("code") == "WORKSPACE_BUSY"
        assert len(provider_reqs) == calls_before_busy
        print(
            "[diag:409]",
            f"status={busy_resp.status_code}",
            f"code={busy_err.get('code')}",
            f"msg={str(busy_err.get('message') or '')[:80]}",
            f"locks_before={locks_before_busy}",
            f"locks_after={locks_after_busy}",
        )

        # Cleanly cancel and wait for the in-flight run from step 5 to reach terminal state and release workspace lock
        cancel_resp = client.post(f"/api/v1/sessions/{sid}/runs/{rid}/cancel", headers=headers, json={})
        assert cancel_resp.status_code in (200, 202)
        for _ in range(100):
            r_state = client.get(f"/api/v1/sessions/{sid}/runs/{rid}", headers=headers).json()["state"]
            locks_now = client.portal.call(_sample_locks)
            if r_state in ("succeeded", "failed", "cancelled", "unknown") and not locks_now["project_active"] and not locks_now["root_active"]:
                break
            time.sleep(0.05)
        assert not client.portal.call(_sample_locks)["project_active"]

        calls_before = len(provider_reqs)
        followup_resp = client.post(
            f"/api/v1/sessions/{sid}/chat/turns",
            headers=headers,
            json={
                "client_request_id": str(uuid.uuid4()),
                "text": "刚才改了什么",
                "intent_mode": "conversation",
            },
        )
        assert followup_resp.status_code == 201, followup_resp.text
        assert len(provider_reqs) == calls_before + 1
        auto_evs = list(provider_reqs[-1].evidence or [])
        assert len(auto_evs) == 1
        ev_auto = auto_evs[0]
        assert isinstance(ev_auto, dict)
        assert ev_auto["run_id"] == new_rid
        assert ev_auto["path"] == "calc.py"
        assert ev_auto["before_sha256"] == new_b_sha
        assert ev_auto["after_sha256"] == new_a_sha
        assert "return new_fixed" in ev_auto["excerpt"]
        assert "return old_fixed" not in ev_auto["excerpt"]

        # Explicit reference still takes priority over auto-selection
        explicit_resp = client.post(
            f"/api/v1/sessions/{sid}/chat/turns",
            headers=headers,
            json={
                "client_request_id": str(uuid.uuid4()),
                "text": "看旧记录",
                "intent_mode": "conversation",
                "references": [
                    {
                        "kind": "diff_hunk",
                        "run_id": old_rid,
                        "path": "calc.py",
                        "before_sha256": old_b_sha,
                        "after_sha256": old_a_sha,
                        "old_start": 1,
                        "old_count": 1,
                        "new_start": 1,
                        "new_count": 1,
                    }
                ],
            },
        )
        assert explicit_resp.status_code == 201
        exp_evs = list(provider_reqs[-1].evidence or [])
        assert len(exp_evs) == 1
        ev_exp = exp_evs[0]
        assert isinstance(ev_exp, dict)
        assert ev_exp["run_id"] == old_rid
        assert ev_exp["path"] == "calc.py"
        assert ev_exp["before_sha256"] == old_b_sha
        assert ev_exp["after_sha256"] == old_a_sha
        assert "return old_fixed" in ev_exp["excerpt"]
