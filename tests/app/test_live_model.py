"""Live model integration: shared quota, verifiable reservation, and client lifecycle.

Every case uses ``httpx.MockTransport`` and a synthetic credential: no socket, no real key,
no environment read.  The transport is asserted to stay a byte-identical controlled copy of
the frozen example source.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "deepseek_requests.v1.json"
_TRANSPORT_PATH = REPO_ROOT / "apps" / "local" / "forecontract_app" / "deepseek_transport.py"
_EXAMPLE_PATH = REPO_ROOT / "examples" / "deepseek_provider.py"
_FROZEN_TRANSPORT_SHA = "2904d82e1379be808c3b9f1c9e92d232f39bae73c29b7c941da0d06af33b0b4e"

from forecontract_app.deepseek_transport import ENDPOINT, DeepSeekProvider, make_client
from forecontract_app.live_provider import LiveProvider, LiveProviderFactory, LiveQuota
from forecontract_app.models import (
    HostConfig,
    LiveConfig,
    ModelSelection,
    StartRunRequest,
)
from forecontract_app.project_demo import OfflineProjectProvider
from forecontract_app.project_models import ProposalDecision, RegisterProjectRequest
from forecontract_app.service import AppService
from forecontract_llm.models import ModelRequest
from project_helpers import create_buggy_calc_project, make_project_host_config
from session_helpers import wait_for_run_terminal_async


def _direct_request(*, goal_text: str | None = None) -> ModelRequest:
    data = json.loads(_FIXTURE.read_bytes())
    if goal_text is not None:
        data["direct"]["model_context"]["goal_text"] = goal_text
    return ModelRequest.model_validate(data["direct"])


def _ok_envelope() -> dict:
    return {
        "id": "synthetic",
        "model": "deepseek-flash",
        "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "{}"}}
        ],
        "usage": {"prompt_tokens": 21, "completion_tokens": 5},
    }


def _mk_transport(*, status: int = 200, delay: float = 0.0):
    calls: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if delay:
            await asyncio.sleep(delay)
        body = b"" if status != 200 else json.dumps(_ok_envelope()).encode()
        return httpx.Response(status, content=body)

    return httpx.MockTransport(handle), calls


def _config(**overrides: object) -> LiveConfig:
    return LiveConfig(api_key="sk-synthetic-not-a-key", model="deepseek-flash", **overrides)


def test_transport_is_controlled_copy_of_frozen_example() -> None:
    transport = _TRANSPORT_PATH.read_bytes()
    example = _EXAMPLE_PATH.read_bytes()
    assert hashlib.sha256(transport).hexdigest() == _FROZEN_TRANSPORT_SHA
    assert transport == example
    assert ENDPOINT == "https://api.deepseek.com/chat/completions"


def test_missing_or_blank_credential_is_rejected_without_transmission() -> None:
    for empty in ("", "   "):
        with pytest.raises(ValueError):
            make_client(api_key=empty)


def test_live_config_repr_redacts_credential() -> None:
    config = _config()
    assert "sk-synthetic-not-a-key" not in repr(config)


def test_credential_only_in_authorization_header_and_no_env_read() -> None:
    client = make_client(api_key="sk-synthetic-not-a-key")
    try:
        assert client.trust_env is False
        assert client.follow_redirects is False
        assert client.headers.get("authorization") == "Bearer sk-synthetic-not-a-key"
    finally:
        asyncio.run(client.aclose())


def test_legal_selection_serializes_one_request() -> None:
    transport, calls = _mk_transport()
    factory = LiveProviderFactory(config=_config(), transport=transport)

    async def scenario() -> tuple[str, int]:
        provider = factory(project=None, run_id="run-1")
        response = await provider.complete(_direct_request())
        return response.status, len(calls)

    status, sent = asyncio.run(scenario())
    assert status == "ok"
    assert sent == 1


def test_quota_shared_across_runs_and_rebuild() -> None:
    transport, calls = _mk_transport()
    factory = LiveProviderFactory(config=_config(max_requests=8), transport=transport)
    request = _direct_request()

    async def scenario() -> tuple[list[str], str, int]:
        first = factory(project=None, run_id="run-1")
        results = [await first.complete(request) for _ in range(8)]
        second = factory(project=None, run_id="run-2")
        extra = await second.complete(request)
        return [r.status for r in results], extra.status, len(calls)

    statuses, extra, sent = asyncio.run(scenario())
    assert statuses == ["ok"] * 8
    assert extra == "refused"
    assert sent == 8


def test_concurrent_quota_shared_atomic() -> None:
    transport, calls = _mk_transport()
    factory = LiveProviderFactory(config=_config(max_requests=1), transport=transport)
    request = _direct_request()

    async def scenario() -> tuple[list[str], int]:
        p1 = factory(project=None, run_id="r1")
        p2 = factory(project=None, run_id="r2")
        results = await asyncio.gather(p1.complete(request), p2.complete(request))
        return [r.status for r in results], len(calls)

    statuses, sent = asyncio.run(scenario())
    assert sorted(statuses) == ["ok", "refused"]
    assert sent == 1


def test_cross_run_budget_exhausts_and_zero_second_send() -> None:
    transport, calls = _mk_transport()
    request = _direct_request()
    inner = DeepSeekProvider(
        client=make_client(api_key="sk-synthetic-not-a-key", transport=transport),
        model="deepseek-flash",
    )
    wire_bytes = len(json.dumps(inner._payload(request), ensure_ascii=False).encode("utf-8"))
    factory = LiveProviderFactory(
        config=_config(
            max_requests=8,
            price_input_cny_per_1m=1_000_000.0,
            price_output_cny_per_1m=0.0,
            budget_cny=float(wire_bytes),
        ),
        transport=transport,
    )

    async def scenario() -> tuple[str, str, int]:
        first = factory(project=None, run_id="r1")
        first_status = (await first.complete(request)).status
        second = factory(project=None, run_id="r2")
        second_status = (await second.complete(request)).status
        return first_status, second_status, len(calls)

    first_status, second_status, sent = asyncio.run(scenario())
    assert first_status == "ok"
    assert second_status == "refused"
    assert sent == 1
    asyncio.run(inner._client.aclose())


def test_error_does_not_reset_quota() -> None:
    transport, calls = _mk_transport(status=500)
    factory = LiveProviderFactory(config=_config(max_requests=1), transport=transport)
    request = _direct_request()

    async def scenario() -> tuple[str, str, int, int]:
        p1 = factory(project=None, run_id="r1")
        first = await p1.complete(request)
        p2 = factory(project=None, run_id="r2")
        second = await p2.complete(request)
        return first.status, second.status, len(calls), p1.requests_sent

    first, second, sent, reserved = asyncio.run(scenario())
    assert first == "refused"  # HTTP 500 is a bounded refusal
    assert second == "refused"  # quota now exhausted, refused before send
    assert sent == 1
    assert reserved == 1


def test_reservation_covers_actual_wire_input_and_refuses_below_bound() -> None:
    request = _direct_request(goal_text='修复加权汇总 "quoted" \\ 转义 \u4e2d\u6587\n多行')
    transport, calls = _mk_transport()
    client = make_client(api_key="sk-synthetic-not-a-key", transport=transport)
    inner = DeepSeekProvider(client=client, model="deepseek-flash")
    wire = inner._payload(request)
    wire_bytes = len(json.dumps(wire, ensure_ascii=False).encode("utf-8"))
    assert any(m["role"] == "system" for m in wire["messages"])
    assert wire["max_tokens"] == request.max_output_tokens

    cfg_ok = _config(
        price_input_cny_per_1m=1_000_000.0, price_output_cny_per_1m=0.0, budget_cny=float(wire_bytes)
    )
    ok = LiveProvider(inner=inner, quota=LiveQuota(config=cfg_ok), client=client)
    assert asyncio.run(ok.complete(request)).status == "ok"
    assert len(calls) == 1

    cfg_low = _config(
        price_input_cny_per_1m=1_000_000.0,
        price_output_cny_per_1m=0.0,
        budget_cny=float(wire_bytes - 1),
    )
    low = LiveProvider(inner=inner, quota=LiveQuota(config=cfg_low), client=client)
    before = len(calls)
    assert asyncio.run(low.complete(request)).status == "refused"
    assert len(calls) == before
    asyncio.run(client.aclose())


def test_output_reservation_uses_max_tokens_and_refuses_below_bound() -> None:
    request = _direct_request()
    transport, _calls = _mk_transport()
    client = make_client(api_key="sk-synthetic-not-a-key", transport=transport)
    inner = DeepSeekProvider(client=client, model="deepseek-flash")
    max_tokens = int(inner._payload(request)["max_tokens"])

    cfg_ok = _config(
        price_input_cny_per_1m=0.0, price_output_cny_per_1m=1_000_000.0, budget_cny=float(max_tokens)
    )
    ok = LiveProvider(inner=inner, quota=LiveQuota(config=cfg_ok), client=client)
    assert asyncio.run(ok.complete(request)).status == "ok"

    cfg_low = _config(
        price_input_cny_per_1m=0.0,
        price_output_cny_per_1m=1_000_000.0,
        budget_cny=float(max_tokens - 1),
    )
    low = LiveProvider(inner=inner, quota=LiveQuota(config=cfg_low), client=client)
    assert asyncio.run(low.complete(request)).status == "refused"
    asyncio.run(client.aclose())


def test_factory_closes_completed_client_once() -> None:
    transport, _calls = _mk_transport()
    factory = LiveProviderFactory(config=_config(), transport=transport)

    async def scenario() -> tuple[httpx.AsyncClient, ...]:
        provider = factory(project=None, run_id="ok")
        assert (await provider.complete(_direct_request())).status == "ok"
        clients = factory.created_clients
        await factory.aclose()
        return clients

    clients = asyncio.run(scenario())
    assert len(clients) == 1
    assert clients[0].is_closed


def test_factory_closes_error_client_once() -> None:
    transport, _calls = _mk_transport(status=500)
    factory = LiveProviderFactory(config=_config(), transport=transport)

    async def scenario() -> tuple[httpx.AsyncClient, ...]:
        provider = factory(project=None, run_id="err")
        assert (await provider.complete(_direct_request())).status == "refused"
        clients = factory.created_clients
        await factory.aclose()
        return clients

    clients = asyncio.run(scenario())
    assert all(c.is_closed for c in clients)


def test_factory_over_limit_rejection_closes_clients_once() -> None:
    transport, _calls = _mk_transport()
    factory = LiveProviderFactory(config=_config(max_requests=1), transport=transport)
    request = _direct_request()

    async def scenario() -> tuple[httpx.AsyncClient, ...]:
        p1 = factory(project=None, run_id="r1")
        assert (await p1.complete(request)).status == "ok"
        p2 = factory(project=None, run_id="r2")
        assert (await p2.complete(request)).status == "refused"
        clients = factory.created_clients
        await factory.aclose()
        return clients

    clients = asyncio.run(scenario())
    assert len(clients) == 2
    assert len({id(c) for c in clients}) == 2
    assert all(c.is_closed for c in clients)


def test_factory_cancellation_then_close_does_not_hang(monkeypatch: pytest.MonkeyPatch) -> None:
    transport, _calls = _mk_transport(delay=10.0)
    factory = LiveProviderFactory(config=_config(), transport=transport)
    request = _direct_request()

    async def scenario() -> tuple[httpx.AsyncClient, ...]:
        provider = factory(project=None, run_id="cancel")
        task = asyncio.create_task(provider.complete(request))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        clients = factory.created_clients
        await asyncio.wait_for(factory.aclose(), timeout=2.0)
        return clients

    clients = asyncio.run(scenario())
    assert all(c.is_closed for c in clients)

    # External cancellation of an in-flight close must surface CancelledError to the caller,
    # while a re-entry reads the saved plain timeout without re-entering the client close.
    transport2, _ = _mk_transport()
    close_entered = asyncio.Event()
    allow_close = asyncio.Event()
    captured = _capture_clients(monkeypatch, close_entered=close_entered, allow_close=allow_close)
    factory2 = LiveProviderFactory(config=_config(), transport=transport2)

    async def scenario2() -> None:
        provider2 = factory2(project=None, run_id="r2")
        try:
            close_task = asyncio.create_task(provider2.aclose())
            await asyncio.wait_for(close_entered.wait(), timeout=5.0)
            close_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await close_task
            close_entries = captured[0][1]["n"]
            with pytest.raises(TimeoutError):
                await provider2.aclose()
            assert captured[0][1]["n"] == close_entries  # no new client close entry
        finally:
            allow_close.set()
            await asyncio.wait_for(factory2.aclose(), timeout=2.0)

    asyncio.run(scenario2())


def _live_host_config(tmp_path: Path, proj_dir: Path, **live: object) -> HostConfig:
    cfg = make_project_host_config(tmp_path, allowed_roots=(proj_dir,))
    return dataclasses.replace(
        cfg, live=LiveConfig(api_key="sk-synthetic-not-a-key", model="deepseek-flash", **live)
    )


async def _register_and_session(svc: AppService, proj_dir: Path) -> str:
    proj = await svc.register_project(
        request=RegisterProjectRequest(name="live-demo", root=str(proj_dir), confirmed_read=True)
    )
    sess = svc.create_session(
        project_id=proj.project_id,
        model=ModelSelection(provider="deepseek", model="deepseek-flash"),
    )
    return sess.session_id


async def _start_and_wait(svc: AppService, session_id: str) -> str:
    run = await svc.start_run(
        session_id=session_id,
        request=StartRunRequest(
            client_request_id=uuid.uuid4(), goal="fix bug", confirmed_scope=True
        ),
    )
    await wait_for_run_terminal_async(svc, session_id=session_id, run_id=run.run_id)
    return run.run_id


def _capture_clients(
    monkeypatch: pytest.MonkeyPatch,
    *,
    close_entered: asyncio.Event | None = None,
    allow_close: asyncio.Event | None = None,
    raise_on_close: bool = False,
    block_close: asyncio.Event | None = None,
) -> list[tuple[httpx.AsyncClient, dict[str, int]]]:
    import forecontract_app.live_provider as lp

    captured: list[tuple[httpx.AsyncClient, dict[str, int]]] = []
    orig = lp.make_client

    def _tracked(*, api_key: str, transport: object | None = None) -> httpx.AsyncClient:
        client = orig(api_key=api_key, transport=transport)
        count = {"n": 0}
        orig_aclose = client.aclose

        async def _aclose() -> None:
            count["n"] += 1
            if close_entered is not None:
                close_entered.set()
            if allow_close is not None:
                await allow_close.wait()
            if block_close is not None:
                await block_close.wait()
            if raise_on_close:
                raise RuntimeError("synthetic close failure")
            await orig_aclose()

        client.aclose = _aclose  # type: ignore[method-assign]
        captured.append((client, count))
        return client

    monkeypatch.setattr(lp, "make_client", _tracked)
    return captured


def _brain_transport():
    """Mock HTTP that replays the OfflineProjectProvider through the production transport."""
    brain = OfflineProjectProvider()
    calls: list[httpx.Request] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = json.loads(request.content)
        user_msg = json.loads(body["messages"][1]["content"])
        model_request = ModelRequest.model_validate(user_msg)
        response = await brain.complete(model_request)
        content = response.content if response.status == "ok" else "{}"
        return httpx.Response(
            200,
            json={
                "id": "synthetic",
                "model": "deepseek-flash",
                "choices": [
                    {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            },
        )

    return httpx.MockTransport(handle), calls


async def _drive_run_to_terminal(svc: AppService, session_id: str, run_id: str, timeout: float = 40.0) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        view = svc.get_run(session_id=session_id, run_id=run_id)
        if view.state in {"succeeded", "failed", "cancelled", "unknown"}:
            return view
        for prop in svc.get_proposals(session_id=session_id, run_id=run_id):
            if prop.state == "pending":
                await svc.decide_proposal(
                    session_id=session_id,
                    run_id=run_id,
                    proposal_id=prop.id,
                    request=ProposalDecision(
                        digest=prop.digest, decision="approve", trusted_local_execution=True
                    ),
                )
        await asyncio.sleep(0.05)
    raise AssertionError("timed out driving run to terminal")


def test_restart_same_authorization_no_second_send(tmp_path: Path) -> None:
    proj_dir = create_buggy_calc_project(tmp_path / "repo")
    transport, calls = _mk_transport(status=500)
    cfg = _live_host_config(tmp_path, proj_dir, max_requests=1)

    async def first() -> tuple[str, int]:
        svc = AppService(config=cfg, live_transport=transport)
        try:
            sid = await _register_and_session(svc, proj_dir)
            await _start_and_wait(svc, sid)
            return sid, len(calls)
        finally:
            await svc.aclose()

    sid, sent_after_first = asyncio.run(first())
    assert sent_after_first == 1

    async def second(session_id: str) -> int:
        svc = AppService(config=cfg, live_transport=transport)
        try:
            await _start_and_wait(svc, session_id)
            return len(calls)
        finally:
            await svc.aclose()

    sent_after_second = asyncio.run(second(sid))
    assert sent_after_second == 1  # no second transmission after restart

    _budget_exhausted_restart(tmp_path)


def _budget_exhausted_restart(tmp_path: Path) -> None:
    # Probe the fixed output cap with an effectively unlimited budget.
    probe_dir = create_buggy_calc_project(tmp_path / "probe-repo")
    probe_transport, probe_calls = _mk_transport(status=500)
    probe_cfg = _live_host_config(
        tmp_path / "probe",
        probe_dir,
        max_requests=8,
        price_input_cny_per_1m=0.0,
        price_output_cny_per_1m=1_000_000.0,
        budget_cny=1e12,
    )

    async def probe() -> int:
        svc = AppService(config=probe_cfg, live_transport=probe_transport)
        try:
            sid = await _register_and_session(svc, probe_dir)
            await _start_and_wait(svc, sid)
            return int(json.loads(probe_calls[0].content)["max_tokens"])
        finally:
            await svc.aclose()

    max_tokens = asyncio.run(probe())

    proj_dir = create_buggy_calc_project(tmp_path / "repo")
    transport, calls = _mk_transport(status=500)
    cfg = _live_host_config(
        tmp_path / "budget",
        proj_dir,
        max_requests=8,
        price_input_cny_per_1m=0.0,
        price_output_cny_per_1m=1_000_000.0,
        budget_cny=float(max_tokens),
    )

    async def scenario() -> tuple[int, int]:
        svc = AppService(config=cfg, live_transport=transport)
        try:
            sid = await _register_and_session(svc, proj_dir)
            await _start_and_wait(svc, sid)
            remaining = svc._live_factory._quota.requests_sent < svc._live_factory._quota._max_requests
            return len(calls), 1 if remaining else 0
        finally:
            await svc.aclose()

    sent_after_first, remaining_ok = asyncio.run(scenario())
    assert sent_after_first == 1
    assert remaining_ok == 1  # count not exhausted, only budget exhausted

    async def second_run() -> int:
        svc = AppService(config=cfg, live_transport=transport)
        try:
            sessions = svc.list_sessions()
            sid = sessions[0].session_id
            await _start_and_wait(svc, sid)
            return len(calls)
        finally:
            await svc.aclose()

    assert asyncio.run(second_run()) == 1


def test_live_usage_reservation_roundtrip(tmp_path: Path) -> None:
    proj_dir = create_buggy_calc_project(tmp_path / "repo")
    transport, calls = _mk_transport(status=500)
    price_in = 1_000_000.0
    price_out = 1_000_000.0
    cfg = _live_host_config(
        tmp_path,
        proj_dir,
        max_requests=1,
        price_input_cny_per_1m=price_in,
        price_output_cny_per_1m=price_out,
        budget_cny=1e12,
    )

    async def scenario() -> tuple[str, str, str, dict, dict, int]:
        svc = AppService(config=cfg, live_transport=transport)
        try:
            sid = await _register_and_session(svc, proj_dir)
            first_id = await _start_and_wait(svc, sid)
            first_usage = svc.get_run(session_id=sid, run_id=first_id).usage
            second_id = await _start_and_wait(svc, sid)
            second_usage = svc.get_run(session_id=sid, run_id=second_id).usage
            return sid, first_id, second_id, first_usage, second_usage, len(calls)
        finally:
            await svc.aclose()

    sid, rid1, rid2, first_usage, second_usage, sent = asyncio.run(scenario())
    assert sent == 1
    content = calls[0].content
    parsed = json.loads(content)
    expected = (len(content) * price_in + int(parsed["max_tokens"]) * price_out) / 1_000_000

    def _fields(u: dict) -> dict:
        return {
            "reserved_cny": u.get("reserved_cny"),
            "cost_status": u.get("cost_status"),
            "estimated_cost_cny": u.get("estimated_cost_cny"),
            "billed_cost_cny": u.get("billed_cost_cny"),
        }

    f_first = _fields(first_usage)
    f_second = _fields(second_usage)
    assert f_first["reserved_cny"] == pytest.approx(expected)
    assert f_first["reserved_cny"] > 0
    assert f_second["reserved_cny"] == 0
    for f in (f_first, f_second):
        assert f["cost_status"] == "live_reserved_billing_unknown"
        assert f["estimated_cost_cny"] is None
        assert f["billed_cost_cny"] is None

    # SQLite readback: one row per (session_id, run_id).
    db = sqlite3.connect(cfg.data_root / "sessions.sqlite3")
    try:
        rows = dict(
            db.execute(
                "SELECT run_id, usage_json FROM runs WHERE session_id=? AND run_id IN (?,?)",
                (sid, rid1, rid2),
            ).fetchall()
        )
    finally:
        db.close()
    assert set(rows) == {rid1, rid2}
    sql_first = _fields(json.loads(rows[rid1]))
    sql_second = _fields(json.loads(rows[rid2]))
    assert sql_first == f_first
    assert sql_second == f_second

    # Reopen the same data root and read back by original (sid, rid) without recreating.
    async def reopen() -> None:
        svc = AppService(config=cfg, live_transport=transport)
        try:
            r1 = svc.get_run_usage(session_id=sid, run_id=rid1)
            r2 = svc.get_run_usage(session_id=sid, run_id=rid2)
            assert _fields(r1.model_dump(mode="json")) == f_first
            assert _fields(r2.model_dump(mode="json")) == f_second
        finally:
            await svc.aclose()

    asyncio.run(reopen())
    assert len(calls) == 1  # readback did not transmit


@pytest.mark.parametrize(
    "scenario", ["error", "over_limit", "succeeded", "close_exception", "close_timeout"]
)
def test_host_run_terminal_closes_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    proj_dir = create_buggy_calc_project(tmp_path / "repo")
    if scenario == "succeeded":
        transport, _calls = _brain_transport()
        cfg = _live_host_config(tmp_path, proj_dir, max_requests=64)
    elif scenario == "over_limit":
        transport, _calls = _mk_transport(status=500)
        cfg = _live_host_config(tmp_path, proj_dir, max_requests=1)
    elif scenario == "close_exception":
        transport, _calls = _mk_transport(status=500)
        cfg = _live_host_config(tmp_path, proj_dir, max_requests=8)
    else:
        transport, _calls = _mk_transport(status=500)
        cfg = _live_host_config(tmp_path, proj_dir, max_requests=8)
    _TERMINAL = {"succeeded", "failed", "cancelled", "unknown"}

    close_order: list[str] = []

    def _capture_close_timeout(
        monkeypatch: pytest.MonkeyPatch, order: list[str]
    ) -> list[tuple[httpx.AsyncClient, dict[str, int]]]:
        """Close-timeout client: record entry and the CancelledError actually caught on close."""
        import forecontract_app.live_provider as lp

        captured: list[tuple[httpx.AsyncClient, dict[str, int]]] = []
        orig = lp.make_client
        block_forever = asyncio.Event()

        def _tracked(*, api_key: str, transport: object | None = None) -> httpx.AsyncClient:
            client = orig(api_key=api_key, transport=transport)
            count = {"n": 0}
            orig_aclose = client.aclose

            async def _aclose() -> None:
                count["n"] += 1
                if count["n"] == 1:
                    order.append("entry")
                    try:
                        await block_forever.wait()  # bounded close times out -> CancelledError
                    except asyncio.CancelledError:
                        order.append("caught_cancel")
                        raise
                await orig_aclose()

            client.aclose = _aclose  # type: ignore[method-assign]
            captured.append((client, count))
            return client

        monkeypatch.setattr(lp, "make_client", _tracked)
        return captured

    if scenario == "close_timeout":
        captured = _capture_close_timeout(monkeypatch, close_order)
    else:
        close_entered = asyncio.Event() if scenario == "succeeded" else None
        allow_close = asyncio.Event() if scenario == "succeeded" else None
        captured = _capture_clients(
            monkeypatch,
            raise_on_close=(scenario == "close_exception"),
            close_entered=close_entered,
            allow_close=allow_close,
        )

    async def scenario_fn() -> tuple[str, str | None]:
        svc = AppService(config=cfg, live_transport=transport)
        reopen_rid: str | None = None
        try:
            sid = await _register_and_session(svc, proj_dir)

            # Transparent finalize_run observer: run the original first, count only success.
            finalize_counts: dict[str, int] = {}
            store = svc._store  # type: ignore[attr-defined]
            orig_finalize = store.finalize_run

            def _tracked_finalize(**kw: Any) -> int:
                rv = orig_finalize(**kw)
                rid = kw["run_view"].run_id
                finalize_counts[rid] = finalize_counts.get(rid, 0) + 1
                return rv

            store.finalize_run = _tracked_finalize  # type: ignore[method-assign]

            def _sql_row(rid: str) -> tuple[Any, ...] | None:
                db = sqlite3.connect(cfg.data_root / "sessions.sqlite3")
                try:
                    return db.execute(
                        "SELECT state, cleanup_confirmed, poisoned_workspace FROM runs WHERE run_id=?",
                        (rid,),
                    ).fetchone()
                finally:
                    db.close()

            def _sql_run_finished(rid: str) -> tuple[int, Any] | None:
                db = sqlite3.connect(cfg.data_root / "sessions.sqlite3")
                try:
                    row = db.execute(
                        "SELECT seq, payload_json FROM run_events WHERE run_id=? AND kind='run_finished'",
                        (rid,),
                    ).fetchone()
                    if row is None:
                        return None
                    return int(row[0]), json.loads(row[1]).get("state")
                finally:
                    db.close()

            def _sql_run_finished_count(rid: str) -> int:
                db = sqlite3.connect(cfg.data_root / "sessions.sqlite3")
                try:
                    return int(
                        db.execute(
                            "SELECT COUNT(*) FROM run_events WHERE run_id=? AND kind='run_finished'",
                            (rid,),
                        ).fetchone()[0]
                    )
                finally:
                    db.close()

            def _make_consumer(replay: list[Any], sub: Any) -> tuple[list[Any], asyncio.Task[None]]:
                finished: list[Any] = []

                async def consume() -> None:
                    for ev in replay:
                        if ev.kind == "run_finished":
                            finished.append(ev)
                    if sub is None:
                        return
                    while True:
                        item = await asyncio.wait_for(sub.queue.get(), timeout=10.0)
                        if item is None:
                            return
                        if item.kind == "run_finished":
                            finished.append(item)

                return finished, asyncio.create_task(consume())

            async def _subscribe(rid: str) -> tuple[list[Any], Any]:
                _hub, replay, sub = svc.open_event_subscription(session_id=sid, run_id=rid, after=0)
                assert sub is not None
                assert not any(ev.kind == "run_finished" for ev in replay)
                return replay, sub

            def _assert_delivery(rid: str, finished: list[Any], *, normal_close: bool) -> None:
                assert len(finished) == 1
                assert finalize_counts.get(rid, 0) == 1
                assert _sql_run_finished_count(rid) == 1
                seq_state = _sql_run_finished(rid)
                assert seq_state is not None
                assert finished[0].seq == seq_state[0]
                assert finished[0].payload.get("state") == seq_state[1]
                rec = svc._runs_by_id.get(rid)  # type: ignore[attr-defined]
                assert rec is not None
                assert rec.live_provider is None
                assert rec.wrapped_provider is None
                if normal_close:
                    factory = svc._live_factory  # type: ignore[attr-defined]
                    assert factory is not None
                    assert len(factory.created_clients) == 0

            async def _start(sid: str) -> str:
                run = await svc.start_run(
                    session_id=sid,
                    request=StartRunRequest(
                        client_request_id=uuid.uuid4(), goal="fix bug", confirmed_scope=True
                    ),
                )
                return run.run_id

            if scenario == "succeeded":
                rid = await _start(sid)
                replay, sub = await _subscribe(rid)
                finished, consumer = _make_consumer(replay, sub)
                drive = asyncio.create_task(_drive_run_to_terminal(svc, sid, rid))
                await asyncio.wait_for(close_entered.wait(), timeout=40.0)
                # Close barrier: terminal not yet visible anywhere before the close completes.
                assert svc.get_run(session_id=sid, run_id=rid).state not in _TERMINAL
                assert not svc._runs_by_id[rid].settled_event.is_set()  # type: ignore[attr-defined]
                assert finalize_counts.get(rid, 0) == 0
                assert not finished  # no live run_finished delivered yet
                row = _sql_row(rid)
                assert row is not None and row[0] not in _TERMINAL
                allow_close.set()
                view = await asyncio.wait_for(drive, timeout=40.0)
                assert view.state == "succeeded"
                await asyncio.wait_for(consumer, timeout=10.0)
                _assert_delivery(rid, finished, normal_close=True)
                # Reference release and factory tracking while the host is still running.
                assert captured and captured[0][0].is_closed
                assert captured[0][1]["n"] == 1
            elif scenario == "over_limit":
                rid1 = await _start(sid)
                replay1, sub1 = await _subscribe(rid1)
                finished1, consumer1 = _make_consumer(replay1, sub1)
                await wait_for_run_terminal_async(svc, session_id=sid, run_id=rid1)
                await asyncio.wait_for(consumer1, timeout=10.0)
                _assert_delivery(rid1, finished1, normal_close=True)

                rid2 = await _start(sid)
                replay2, sub2 = await _subscribe(rid2)
                finished2, consumer2 = _make_consumer(replay2, sub2)
                await wait_for_run_terminal_async(svc, session_id=sid, run_id=rid2)
                await asyncio.wait_for(consumer2, timeout=10.0)
                _assert_delivery(rid2, finished2, normal_close=True)
                assert len(_calls) == 1  # second run refused before any transmission
            else:
                rid = await _start(sid)
                if scenario == "close_timeout":
                    # Record the production publish point: wrap commit_prepared_event so the
                    # run_finished commit (service.py:1139) appends "terminal" synchronously.
                    hub = svc._runs_by_id[rid].hub  # type: ignore[attr-defined]
                    orig_commit = hub.commit_prepared_event

                    def _tracked_commit(event: Any, *, truncated_before: int = 0) -> Any:
                        result = orig_commit(event, truncated_before=truncated_before)
                        if event.kind == "run_finished":
                            close_order.append("terminal")
                        return result

                    hub.commit_prepared_event = _tracked_commit  # type: ignore[method-assign]
                replay, sub = await _subscribe(rid)
                finished, consumer = _make_consumer(replay, sub)
                view = await wait_for_run_terminal_async(svc, session_id=sid, run_id=rid)
                await asyncio.wait_for(consumer, timeout=10.0)
                if scenario in {"close_exception", "close_timeout"}:
                    assert view.state == "unknown"
                    assert view.reason_code == "CLEANUP_UNCONFIRMED"
                    row = _sql_row(rid)
                    assert row is not None
                    assert row[0] == "unknown"
                    assert row[1] == 0  # cleanup_confirmed
                    assert row[2] == 1  # poisoned_workspace
                _assert_delivery(rid, finished, normal_close=(scenario == "error"))
                if scenario == "close_timeout":
                    assert close_order == ["entry", "caught_cancel", "terminal"]
                if scenario == "close_exception":
                    reopen_rid = rid
            return sid, reopen_rid
        finally:
            await svc.aclose()

    sid, reopen_rid = asyncio.run(scenario_fn())
    if scenario == "close_exception":
        assert captured and captured[0][0].is_closed is False
    elif scenario == "close_timeout":
        # The timed-out close was recorded as unconfirmed; the factory finally closed it.
        assert captured and captured[0][0].is_closed
    else:
        for client, count in captured:
            assert client.is_closed
            assert count["n"] == 1

    if scenario == "close_exception":
        # Same-root reopen: a new AppService reads the original terminal by (sid, rid).
        async def reopen() -> None:
            svc2 = AppService(config=cfg, live_transport=transport)
            try:
                view = svc2.get_run(session_id=sid, run_id=reopen_rid)  # type: ignore[arg-type]
                assert view.state == "unknown"
                assert view.reason_code == "CLEANUP_UNCONFIRMED"
            finally:
                await svc2.aclose()

        asyncio.run(reopen())


def test_host_cancel_shutdown_closes_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proj_dir = create_buggy_calc_project(tmp_path / "repo")
    entered = asyncio.Event()
    cancel_entered = asyncio.Event()
    shutdown_entered = asyncio.Event()
    release = asyncio.Event()

    async def handle(request: httpx.Request) -> httpx.Response:
        entered.set()
        await release.wait()
        return httpx.Response(200, content=b"{}")

    transport = httpx.MockTransport(handle)
    cfg = _live_host_config(tmp_path, proj_dir, max_requests=8)
    captured = _capture_clients(monkeypatch)

    async def scenario() -> tuple[str, str]:
        svc = AppService(config=cfg, live_transport=transport)
        try:
            sid = await _register_and_session(svc, proj_dir)

            finalize_counts: dict[str, int] = {}
            store = svc._store  # type: ignore[attr-defined]
            orig_finalize = store.finalize_run

            def _tracked_finalize(**kw: Any) -> int:
                rv = orig_finalize(**kw)
                rid = kw["run_view"].run_id
                finalize_counts[rid] = finalize_counts.get(rid, 0) + 1
                return rv

            store.finalize_run = _tracked_finalize  # type: ignore[method-assign]

            run = await svc.start_run(
                session_id=sid,
                request=StartRunRequest(
                    client_request_id=uuid.uuid4(), goal="fix bug", confirmed_scope=True
                ),
            )
            rid = run.run_id
            await entered.wait()

            _hub, replay, sub = svc.open_event_subscription(session_id=sid, run_id=rid, after=0)
            assert sub is not None
            assert not any(ev.kind == "run_finished" for ev in replay)  # count only live, exclude replay
            finished: list[Any] = []

            async def consume() -> None:
                while True:
                    item = await asyncio.wait_for(sub.queue.get(), timeout=10.0)
                    if item is None:
                        return
                    if item.kind == "run_finished":
                        finished.append(item)

            consumer = asyncio.create_task(consume())

            # Cancel barrier inside the real _cancel_watchdog cleanup, held until release.
            watchdog_task: list[asyncio.Task[None]] = []
            orig_watchdog = svc._cancel_watchdog  # type: ignore[attr-defined]

            async def _tracked_watchdog(run_rec: Any) -> None:
                watchdog_task.append(asyncio.current_task())  # type: ignore[arg-type]
                cancel_entered.set()
                await release.wait()
                await orig_watchdog(run_rec)

            svc._cancel_watchdog = _tracked_watchdog  # type: ignore[method-assign,attr-defined]

            # Shutdown barrier inside the actual aclose wait (settled_event.wait).
            rec = svc._runs_by_id[rid]  # type: ignore[attr-defined]
            orig_wait = rec.settled_event.wait

            def _tracked_wait() -> Any:
                shutdown_entered.set()
                return orig_wait()

            rec.settled_event.wait = _tracked_wait  # type: ignore[method-assign]

            async def do_cancel() -> None:
                await svc.cancel_run(session_id=sid, run_id=rid)

            async def do_shutdown() -> None:
                await svc.aclose()

            t_cancel = asyncio.create_task(do_cancel())
            t_shutdown = asyncio.create_task(do_shutdown())
            await asyncio.wait_for(cancel_entered.wait(), timeout=5.0)
            await asyncio.wait_for(shutdown_entered.wait(), timeout=5.0)
            # Real cleanup and shutdown are both entered but neither has completed.
            assert watchdog_task and not watchdog_task[0].done()
            assert not t_shutdown.done()
            release.set()
            await asyncio.wait_for(
                asyncio.gather(t_cancel, t_shutdown, watchdog_task[0]), timeout=10.0
            )
            await asyncio.wait_for(consumer, timeout=10.0)

            assert len(finished) == 1
            assert finalize_counts.get(rid, 0) == 1
            db = sqlite3.connect(cfg.data_root / "sessions.sqlite3")
            try:
                row = db.execute(
                    "SELECT seq, payload_json FROM run_events WHERE run_id=? AND kind='run_finished'",
                    (rid,),
                ).fetchone()
                run_finished_rows = int(
                    db.execute(
                        "SELECT COUNT(*) FROM run_events WHERE run_id=? AND kind='run_finished'",
                        (rid,),
                    ).fetchone()[0]
                )
            finally:
                db.close()
            assert run_finished_rows == 1
            assert row is not None
            assert finished[0].seq == int(row[0])
            assert finished[0].payload.get("state") == json.loads(row[1]).get("state")
            return sid, rid
        finally:
            release.set()
            await svc.aclose()

    sid, rid = asyncio.run(scenario())
    assert len(captured) == 1
    client, count = captured[0]
    assert client.is_closed
    assert count["n"] == 1

    # Same-root reopen: a new AppService reads the original terminal by (sid, rid).
    async def reopen() -> None:
        svc2 = AppService(config=cfg, live_transport=transport)
        try:
            view = svc2.get_run(session_id=sid, run_id=rid)
            assert view.state == "cancelled"
        finally:
            await svc2.aclose()

    asyncio.run(reopen())
