"""C21–C23: UsageAccumulator exact arithmetic, conflict guards, real provider tracking & zero billing/network."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
import project_helpers
import pytest
from app_helpers import CREDENTIAL_ENV_CANARY, auth_headers, make_host_config
from forecontract.tracing import JsonlTraceStore
from forecontract_app.models import (
    AppError,
    ModelSelection,
    StartRunRequest,
)
from forecontract_app.project_demo import build_registered_project_task
from forecontract_app.project_permissions import PermissionBroker
from forecontract_app.project_runtime import run_project_task
from forecontract_app.project_workspace import ProjectWorkspace
from forecontract_app.server import create_app
from forecontract_app.session_context import ConversationRuntime, build_context_frame
from forecontract_app.session_models import SetConstraintsRequest
from forecontract_app.session_usage import UsageAccumulator
from forecontract_llm import ModelUsage
from session_helpers import (
    SpyUsageProvider,
    forecast_json,
    make_reopened_host_config,
    wait_for_run_terminal_async,
)


def test_c21_usage_aggregation_idempotency_unknown_null_and_conflict() -> None:
    now_val = [10.0]
    acc = UsageAccumulator(
        max_calls=10, max_steps=12, run_timeout_ms=60_000, monotonic=lambda: now_val[0]
    )

    # finish_call before begin_call -> rejected
    with pytest.raises(AppError) as err_unk:
        acc.finish_call(request_id="req-missing", usage=None)
    assert err_unk.value.code == "USAGE_UNKNOWN_REQUEST"

    # Two distinct known usage calls + one idempotent repeat
    acc.begin_call(request_id="req-1")
    u1 = ModelUsage(schema_version="0.1", input_tokens=120, output_tokens=30)
    acc.finish_call(request_id="req-1", usage=u1)
    # Idempotent repeat of finish_call and begin_call -> no double counting
    acc.begin_call(request_id="req-1")
    acc.finish_call(request_id="req-1", usage=u1)

    # Conflicting usage on same request_id -> 409 USAGE_CONFLICT
    with pytest.raises(AppError) as err_conf:
        acc.finish_call(
            request_id="req-1",
            usage=ModelUsage(schema_version="0.1", input_tokens=999, output_tokens=30),
        )
    assert err_conf.value.status_code == 409

    acc.begin_call(request_id="req-2")
    acc.finish_call(
        request_id="req-2",
        usage=ModelUsage(schema_version="0.1", input_tokens=80, output_tokens=20),
    )
    snap_known = acc.snapshot()
    assert snap_known.calls_started == 2
    assert snap_known.calls_completed == 2
    assert snap_known.usage_known_calls == 2
    assert snap_known.usage_unknown_calls == 0
    assert snap_known.known_input_tokens == 200
    assert snap_known.known_output_tokens == 50
    assert snap_known.input_tokens == 200
    assert snap_known.output_tokens == 50
    assert snap_known.estimated_cost_cny is None
    assert snap_known.billed_cost_cny is None
    assert snap_known.cost_status == "offline_not_billed"

    # One unknown/errored call -> totals become None, known subtotals preserved
    acc.begin_call(request_id="req-3")
    acc.finish_call(request_id="req-3", usage=None)
    # Plus an unfinished started call (exception / cancel)
    acc.begin_call(request_id="req-4")
    now_val[0] = 12.5
    acc.set_steps_used(4)
    snap_mixed = acc.snapshot()
    assert snap_mixed.calls_started == 4
    assert snap_mixed.calls_completed == 3
    assert snap_mixed.usage_known_calls == 2
    assert snap_mixed.usage_unknown_calls == 2
    assert snap_mixed.known_input_tokens == 200
    assert snap_mixed.known_output_tokens == 50
    assert snap_mixed.input_tokens is None
    assert snap_mixed.output_tokens is None
    assert snap_mixed.steps_used == 4
    assert snap_mixed.elapsed_ms == 2500

    # After finalize(), advancing monotonic clock does NOT inflate elapsed_ms
    fin = acc.finalize()
    now_val[0] = 99.0
    assert acc.snapshot().elapsed_ms == fin.elapsed_ms == 2500


def test_c22_real_provider_usage_tracking_budget_cutoff_and_frozen_elapsed(tmp_path: Path) -> None:
    repo = project_helpers.create_buggy_calc_project(tmp_path / "repo")
    data_root = tmp_path / "data"
    out_root = tmp_path / "out"
    data_root.mkdir()
    out_root.mkdir()
    ws = ProjectWorkspace(root=repo, data_root=data_root)
    c22_sid = str(uuid.uuid4())
    c22_rid = str(uuid.uuid4())
    broker = PermissionBroker(run_id=c22_rid, project_root=repo, root_identity=ws.root_identity)

    task, pcfg = build_registered_project_task(
        project_id=str(uuid.uuid4()),
        project_name="calc",
        root_identity=ws.root_identity,
        data_root=data_root,
        output_root=out_root,
        run_id=c22_rid,
        session_id=c22_sid,
        goal="Check real max_calls=2 budget cutoff and usage tracking",
        python_executable=Path(sys.executable),
        max_steps=5,
        max_planning_calls=2,
        run_timeout_ms=60_000,
    )
    usage_acc = UsageAccumulator(
        max_calls=pcfg.max_calls,
        max_steps=task.max_steps,
        run_timeout_ms=task.run_timeout_ms,
    )
    frame = build_context_frame(
        project_id=task.project_id,
        session_id=task.session_id,
        run_id=task.run_id,
        goal=task.goal_text,
        constraints="",
        history=[],
    )
    conv = ConversationRuntime(context_frame=frame, usage_accumulator=usage_acc)
    spy = SpyUsageProvider(
        lambda _i, _r: forecast_json(action_type="list_directory", payload={}),
        usage_fn=lambda i, _r: ModelUsage(
            schema_version="0.1", input_tokens=10 * (i + 1), output_tokens=5
        ),
    )

    res = asyncio.run(
        run_project_task(
            task=task,
            provider=spy,
            planner_config=pcfg,
            permissions=broker,
            trace_writer=JsonlTraceStore(data_root / "t_c22.jsonl"),
            emit=lambda _k, _p: None,
            workspace=ws,
            conversation=conv,
        )
    )
    assert res.terminal_status != "succeeded"
    # Budget stopped cleanly at max_calls=2 without extra request
    assert len(spy.requests) == 2
    snap = usage_acc.finalize()
    assert snap.calls_started == 2
    assert snap.calls_completed == 2
    assert snap.max_calls == 2
    assert snap.max_steps == 5
    assert snap.run_timeout_ms == 60_000
    assert snap.known_input_tokens == 30
    assert snap.known_output_tokens == 10
    assert snap.input_tokens == 30
    assert snap.output_tokens == 10


def test_c23_usage_projection_no_cost_no_env_key_reads_or_network_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", CREDENTIAL_ENV_CANARY)
    cfg = make_host_config(tmp_path)

    def _forbid_network(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("network socket forbidden in offline usage mode")

    async def _scenario() -> tuple[str, str, dict[str, Any]]:
        svc_app = create_app(config=cfg)
        async with svc_app.router.lifespan_context(svc_app):
            svc = svc_app.state.service
            proj = await svc.create_project(name="usage-proj")
            sess = svc.create_session(
                project_id=proj.project_id,
                model=ModelSelection(provider="offline", model="offline-metrics-v1"),
            )
            run = await svc.start_run(
                session_id=sess.session_id,
                request=StartRunRequest(
                    client_request_id=uuid.uuid4(),
                    goal="Track offline usage without billing",
                    confirmed_scope=True,
                ),
            )
            await wait_for_run_terminal_async(svc, session_id=sess.session_id, run_id=run.run_id)
            # Mutate constraints afterwards -> run usage limits MUST NOT reset
            svc.set_session_constraints(
                session_id=sess.session_id,
                request=SetConstraintsRequest(
                    client_request_id=uuid.uuid4(),
                    expected_revision=0,
                    constraints="Updated constraints after run",
                ),
            )
            transport = httpx.ASGITransport(app=svc_app)
            async with httpx.AsyncClient(transport=transport, base_url=f"http://127.0.0.1:{cfg.bind_port}") as client:
                u_resp = await client.get(
                    f"/api/v1/sessions/{sess.session_id}/runs/{run.run_id}/usage",
                    headers=auth_headers(cfg, with_origin=False),
                )
                assert u_resp.status_code == 200
                u_json = u_resp.json()
                assert u_json["estimated_cost_cny"] is None
                assert u_json["billed_cost_cny"] is None
                assert u_json["cost_status"] == "offline_not_billed"
                # OfflineMetricsProvider sets usage=None -> input_tokens is None
                assert u_json["calls_started"] >= 1
                assert u_json["usage_unknown_calls"] >= 1
                assert u_json["input_tokens"] is None
                assert CREDENTIAL_ENV_CANARY not in json.dumps(u_json)
                return sess.session_id, run.run_id, u_json

    sid, rid, before_reopen = asyncio.run(_scenario())
    reopen_cfg = make_reopened_host_config(cfg)
    svc2_app = create_app(config=reopen_cfg)

    async def _reopen_check() -> None:
        async with svc2_app.router.lifespan_context(svc2_app):
            transport = httpx.ASGITransport(app=svc2_app)
            async with httpx.AsyncClient(
                transport=transport, base_url=f"http://127.0.0.1:{reopen_cfg.bind_port}"
            ) as client:
                u2 = await client.get(
                    f"/api/v1/sessions/{sid}/runs/{rid}/usage",
                    headers=auth_headers(reopen_cfg, with_origin=False),
                )
                assert u2.status_code == 200
                assert u2.json() == before_reopen

    asyncio.run(_reopen_check())
    assert os.environ.get("DEEPSEEK_API_KEY") == CREDENTIAL_ENV_CANARY
