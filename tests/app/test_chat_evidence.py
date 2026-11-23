"""CHAT-T02 offline tests for run evidence references, scope/digest checks, and mock live chat."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import project_helpers
import pytest
from forecontract_app.chat_models import ChatProviderRequest, ChatProviderResponse
from forecontract_app.models import (
    AppError,
    HostConfig,
    LiveConfig,
    ModelSelection,
    StartRunRequest,
)
from forecontract_app.project_models import ProposalDecision, RegisterProjectRequest
from forecontract_app.service import AppService
from test_session_store import make_reopened_host_config, wait_for_run_terminal_async


def seed_evidence_demo(
    project_root: Path,
    data_root: Path,
    *,
    model: ModelSelection | None = None,
) -> dict[str, Any]:
    """Seed one registered calc project with two distinguishable runs (fail then pass)."""
    selected_model = model or ModelSelection(provider="offline", model="offline-project-v1")
    if not (project_root / "calc.py").exists():
        project_helpers.create_buggy_calc_project(project_root)
    ws_parent = data_root.parent / "workspaces"
    ws_parent.mkdir(parents=True, exist_ok=True)
    data_root.mkdir(parents=True, exist_ok=True)
    live_cfg_opt = (
        LiveConfig(
            api_key="sk-synthetic-seed-only",
            model=selected_model.model,
            max_requests=100,
            budget_cny=10.0,
        )
        if selected_model.provider == "deepseek"
        else None
    )
    cfg = HostConfig(
        data_root=data_root,
        workspace_parent=ws_parent,
        python_executable=project_helpers.make_project_host_config(
            data_root.parent, allowed_roots=(project_root,)
        ).python_executable,
        bind_port=8765,
        access_token="seed-token-unused",
        project_roots=(project_root,),
        live=live_cfg_opt,
    )

    async def _approve_all(svc: AppService, sid: str, rid: str) -> None:
        for _ in range(400):
            r = svc.get_run(session_id=sid, run_id=rid)
            if r.state in ("succeeded", "failed", "cancelled", "unknown"):
                return
            for p in svc.get_proposals(session_id=sid, run_id=rid):
                if p.state == "pending":
                    await svc.decide_proposal(
                        session_id=sid,
                        run_id=rid,
                        proposal_id=p.id,
                        request=ProposalDecision(
                            digest=p.digest,
                            decision="approve",
                            trusted_local_execution=True,
                        ),
                    )
            await asyncio.sleep(0.05)
        raise TimeoutError(f"run {rid} did not finish")

    async def _seed() -> dict[str, Any]:
        # Run 1: modify calc.py (still buggy) + fail verification; Run 2: fix calc.py + pass verification
        before_sha = project_helpers.sha256_file(project_root / "calc.py")
        step_mode = {"run": 0}

        def _seed_provider_factory(*, project: Any, run_id: str) -> Any:
            _ = project, run_id
            from forecontract_app.project_demo import OfflineProjectProvider

            step_mode["run"] += 1
            cur_run = step_mode["run"]

            class _SeedProvider(OfflineProjectProvider):
                def _analyze_repo(self, python_exe: str) -> None:
                    self._analyzed = True
                    calc_sha, _content = self._files["calc.py"]
                    self._planned_creates = []
                    self._protected_paths = []
                    if cur_run == 1:
                        self._planned_edits = [
                            {
                                "path": "calc.py",
                                "expected_sha256": calc_sha,
                                "old_text": "    return a - b",
                                "new_text": "    return a * b  # RUN1_BUG_MARKER",
                            }
                        ]
                        self._plan_checks = [
                            {
                                "check_id": "chk_1",
                                "kind": "exit_code",
                                "executable": python_exe,
                                "argv": ["-B", "-c", "import sys; print('RUN1_FAIL_OUT'); sys.exit(4)"],
                                "cwd": ".",
                                "timeout_ms": 15000,
                                "protected_paths": [],
                                "expected_tests": [],
                            }
                        ]
                    else:
                        self._planned_edits = [
                            {
                                "path": "calc.py",
                                "expected_sha256": calc_sha,
                                "old_text": "    return a * b  # RUN1_BUG_MARKER",
                                "new_text": "    return a + b  # RUN2_FIX_MARKER",
                            }
                        ]
                        self._plan_checks = [
                            {
                                "check_id": "chk_2",
                                "kind": "exit_code",
                                "executable": python_exe,
                                "argv": [
                                    "-B",
                                    "-c",
                                    "import calc; assert calc.add_numbers(2, 3) == 5; print('RUN2_PASS_OUT')",
                                ],
                                "cwd": ".",
                                "timeout_ms": 15000,
                                "protected_paths": [],
                                "expected_tests": [],
                            }
                        ]

                def _next_step(self, request: Any) -> dict[str, Any] | None:
                    last = request.observation.state.get("last_tool_result")
                    if cur_run == 1 and isinstance(last, dict) and last.get("action_type") == "verify_project":
                        self._aborted = True
                    return super()._next_step(request)

            return _SeedProvider()

        svc = AppService(config=cfg, provider_factory=_seed_provider_factory)
        if selected_model.provider == "deepseek":
            class _SeedLiveFactory:
                def __call__(self, *, project: Any, run_id: str, usage_accumulator: Any) -> Any:
                    _ = usage_accumulator
                    inner = _seed_provider_factory(project=project, run_id=run_id)

                    class _SeedLiveAdapter:
                        async def complete(self, request: Any) -> Any:
                            return await inner.complete(request)

                        async def aclose(self) -> None:
                            return None

                    return _SeedLiveAdapter()

                async def aclose(self) -> None:
                    return None

            svc._live_factory = _SeedLiveFactory()
        try:
            proj = await svc.register_project(
                request=RegisterProjectRequest(
                    name="evidence-demo", root=str(project_root), confirmed_read=True
                )
            )
            sess = svc.create_session(
                project_id=proj.project_id,
                model=selected_model,
            )
            r1 = await svc.start_run(
                session_id=sess.session_id,
                request=StartRunRequest(
                    client_request_id=uuid.uuid4(),
                    goal="Run1 attempt calc",
                    confirmed_scope=True,
                ),
            )
            await _approve_all(svc, sess.session_id, r1.run_id)
            r1_done = await wait_for_run_terminal_async(svc, session_id=sess.session_id, run_id=r1.run_id)
            r1_after_sha = project_helpers.sha256_file(project_root / "calc.py")

            r2 = await svc.start_run(
                session_id=sess.session_id,
                request=StartRunRequest(
                    client_request_id=uuid.uuid4(),
                    goal="Run2 fix calc",
                    confirmed_scope=True,
                ),
            )
            await _approve_all(svc, sess.session_id, r2.run_id)
            r2_done = await wait_for_run_terminal_async(svc, session_id=sess.session_id, run_id=r2.run_id)
            r1_cmd_id = next(iter((r1_done.result or {})["checks"].values()))["command_id"]
            r2_cmd_id = next(iter((r2_done.result or {})["checks"].values()))["command_id"]
            return {
                "project_id": proj.project_id,
                "session_id": sess.session_id,
                "run1_id": r1_done.run_id,
                "run2_id": r2_done.run_id,
                "run1_command_id": r1_cmd_id,
                "run2_command_id": r2_cmd_id,
                "calc_before_sha": before_sha,
                "run1_after_sha": r1_after_sha,
                "diff_marker": "RUN1_BUG_MARKER",
                "fail_marker": "RUN1_FAIL_OUT",
                "pass_marker": "RUN2_PASS_OUT",
            }
        finally:
            await svc.aclose()

    return asyncio.run(_seed())


def test_historical_diff_and_expired_command(tmp_path: Path) -> None:
    proj_dir = tmp_path / "ev_proj"
    data_dir = tmp_path / "ev_data"
    seed = seed_evidence_demo(proj_dir, data_dir)

    # Mutate calc.py on disk after both runs; historical diff query MUST NOT read current disk
    (proj_dir / "calc.py").write_text(
        "def add_numbers(a: int, b: int) -> int:\n    return a + b  # CURRENT_DISK_MARKER\n",
        encoding="utf-8",
    )

    cfg = project_helpers.make_project_host_config(tmp_path, allowed_roots=(proj_dir,))
    cfg = HostConfig(
        data_root=data_dir,
        workspace_parent=data_dir.parent / "workspaces",
        python_executable=cfg.python_executable,
        bind_port=cfg.bind_port,
        access_token=cfg.access_token,
        project_roots=(proj_dir,),
    )

    seen_reqs: list[ChatProviderRequest] = []

    def _spy_factory(**_kw: Any):
        def _provider(req: ChatProviderRequest) -> ChatProviderResponse:
            seen_reqs.append(req)
            return ChatProviderResponse(
                action="final",
                assistant_message=f"Evidence count={len(req.evidence)}",
            )

        return _provider

    async def _verify() -> None:
        # Reopened host has command_runner=None (expired raw output) and persisted diff/checks
        svc = AppService(config=make_reopened_host_config(cfg), chat_provider_factory=_spy_factory)
        try:
            t1, c1 = await svc.start_chat_turn(
                session_id=seed["session_id"],
                request={  # type: ignore[arg-type]
                    "client_request_id": uuid.uuid4(),
                    "text": "Explain why Run1 calc was wrong",
                    "references": [
                        {
                            "kind": "diff_hunk",
                            "run_id": seed["run1_id"],
                            "path": "calc.py",
                            "before_sha256": seed["calc_before_sha"],
                            "after_sha256": seed["run1_after_sha"],
                            "old_start": 2,
                            "old_count": 4,
                            "new_start": 2,
                            "new_count": 4,
                        }
                    ],
                },
            )
            assert c1 == 201
            assert t1.status == "completed"
            ev_diff = seen_reqs[-1].evidence[0]
            assert ev_diff["run_id"] == seed["run1_id"]
            assert ev_diff["before_sha256"] == seed["calc_before_sha"]
            assert ev_diff["after_sha256"] == seed["run1_after_sha"]
            assert "RUN1_BUG_MARKER" in ev_diff["excerpt"]
            assert "CURRENT_DISK_MARKER" not in ev_diff["excerpt"]

            _t2, c2 = await svc.start_chat_turn(
                session_id=seed["session_id"],
                request={  # type: ignore[arg-type]
                    "client_request_id": uuid.uuid4(),
                    "text": "Compare Run1 and Run2 commands",
                    "references": [
                        {"kind": "command", "run_id": seed["run1_id"], "command_id": seed["run1_command_id"]},
                        {"kind": "command", "run_id": seed["run2_id"], "command_id": seed["run2_command_id"]},
                    ],
                },
            )
            assert c2 == 201
            assert len(seen_reqs[-1].evidence) == 2
            c_r1, c_r2 = seen_reqs[-1].evidence
            assert c_r1["availability"] == "raw_output_unavailable"
            assert c_r1["note"] == "原始输出不可用"
            assert c_r1["exit_code"] == 4
            assert c_r2["exit_code"] == 0
            assert c_r2["passed"] is True
            assert len(seen_reqs[-1].model_dump_json().encode("utf-8")) <= 16 * 1024
        finally:
            await svc.aclose()

        # Also verify default OfflineReadonlyChatProvider renders historical diff & unavailable note
        svc2 = AppService(config=make_reopened_host_config(cfg))
        try:
            t3, _ = await svc2.start_chat_turn(
                session_id=seed["session_id"],
                request={  # type: ignore[arg-type]
                    "client_request_id": uuid.uuid4(),
                    "text": "Summarize Run1 diff and command",
                    "references": [
                        {
                            "kind": "diff_hunk",
                            "run_id": seed["run1_id"],
                            "path": "calc.py",
                            "before_sha256": seed["calc_before_sha"],
                            "after_sha256": seed["run1_after_sha"],
                            "old_start": 2,
                            "old_count": 4,
                            "new_start": 2,
                            "new_count": 4,
                        },
                        {"kind": "command", "run_id": seed["run1_id"], "command_id": seed["run1_command_id"]},
                    ],
                },
            )
            assert t3.assistant_message is not None
            assert "RUN1_BUG_MARKER" in t3.assistant_message
            assert "原始输出不可用" in t3.assistant_message
        finally:
            await svc2.aclose()

    asyncio.run(_verify())


def test_reference_scope_digest_and_restart(tmp_path: Path) -> None:
    proj_dir = tmp_path / "scope_proj"
    data_dir = tmp_path / "scope_data"
    seed = seed_evidence_demo(proj_dir, data_dir)
    cfg = project_helpers.make_project_host_config(tmp_path, allowed_roots=(proj_dir,))
    cfg = HostConfig(
        data_root=data_dir,
        workspace_parent=data_dir.parent / "workspaces",
        python_executable=cfg.python_executable,
        bind_port=cfg.bind_port,
        access_token=cfg.access_token,
        project_roots=(proj_dir,),
    )

    provider_calls = 0

    def _spy_factory(**_kw: Any):
        def _provider(req: ChatProviderRequest) -> ChatProviderResponse:
            nonlocal provider_calls
            provider_calls += 1
            return ChatProviderResponse(action="final", assistant_message=f"ok:{req.question}")

        return _provider

    async def _verify() -> None:
        svc = AppService(config=make_reopened_host_config(cfg), chat_provider_factory=_spy_factory)
        try:
            other_sess = svc.create_session(
                project_id=seed["project_id"],
                model=ModelSelection(provider="offline", model="offline-project-v1"),
            )

            # 1. Cross-session run_id -> 404 before provider
            with pytest.raises(AppError) as e_cross:
                await svc.start_chat_turn(
                    session_id=other_sess.session_id,
                    request={  # type: ignore[arg-type]
                        "client_request_id": uuid.uuid4(),
                        "text": "Cross session",
                        "references": [{"kind": "command", "run_id": seed["run1_id"], "command_id": seed["run1_command_id"]}],
                    },
                )
            assert e_cross.value.code == "NOT_FOUND"

            # 2. Forged command_id -> 404 before provider
            with pytest.raises(AppError) as e_cmd:
                await svc.start_chat_turn(
                    session_id=seed["session_id"],
                    request={  # type: ignore[arg-type]
                        "client_request_id": uuid.uuid4(),
                        "text": "Fake cmd",
                        "references": [{"kind": "command", "run_id": seed["run1_id"], "command_id": "forged_cmd"}],
                    },
                )
            assert e_cmd.value.code == "NOT_FOUND"

            # 3. Wrong SHA -> 409 before provider
            with pytest.raises(AppError) as e_sha:
                await svc.start_chat_turn(
                    session_id=seed["session_id"],
                    request={  # type: ignore[arg-type]
                        "client_request_id": uuid.uuid4(),
                        "text": "Wrong SHA",
                        "references": [
                            {
                                "kind": "diff_hunk",
                                "run_id": seed["run1_id"],
                                "path": "calc.py",
                                "before_sha256": "0" * 64,
                                "after_sha256": seed["run1_after_sha"],
                                "old_start": 1,
                                "old_count": 2,
                                "new_start": 1,
                                "new_count": 2,
                            }
                        ],
                    },
                )
            assert e_sha.value.code == "EVIDENCE_MISMATCH"

            # 4. Out-of-range hunk -> 422 before provider
            with pytest.raises(AppError) as e_rng:
                await svc.start_chat_turn(
                    session_id=seed["session_id"],
                    request={  # type: ignore[arg-type]
                        "client_request_id": uuid.uuid4(),
                        "text": "Out of range",
                        "references": [
                            {
                                "kind": "diff_hunk",
                                "run_id": seed["run1_id"],
                                "path": "calc.py",
                                "before_sha256": seed["calc_before_sha"],
                                "after_sha256": seed["run1_after_sha"],
                                "old_start": 999,
                                "old_count": 1,
                                "new_start": 999,
                                "new_count": 1,
                            }
                        ],
                    },
                )
            assert e_rng.value.code == "EVIDENCE_OUT_OF_RANGE"
            assert provider_calls == 0

            # 5. Idempotency: same key + same refs -> 200; same key + changed refs -> 409; legacy no-ref replay -> 200
            k_ev = uuid.uuid4()
            ref_payload = [{"kind": "command", "run_id": seed["run1_id"], "command_id": seed["run1_command_id"]}]
            t1, c1 = await svc.start_chat_turn(
                session_id=seed["session_id"],
                request={"client_request_id": k_ev, "text": "With ref", "references": ref_payload},  # type: ignore[arg-type]
            )
            assert c1 == 201
            t1_rep, c1_rep = await svc.start_chat_turn(
                session_id=seed["session_id"],
                request={"client_request_id": k_ev, "text": "With ref", "references": ref_payload},  # type: ignore[arg-type]
            )
            assert c1_rep == 200 and t1_rep.turn_id == t1.turn_id
            assert provider_calls == 1

            with pytest.raises(AppError) as e_conf:
                await svc.start_chat_turn(
                    session_id=seed["session_id"],
                    request={  # type: ignore[arg-type]
                        "client_request_id": k_ev,
                        "text": "With ref",
                        "references": [{"kind": "command", "run_id": seed["run2_id"], "command_id": seed["run2_command_id"]}],
                    },
                )
            assert e_conf.value.code == "IDEMPOTENCY_CONFLICT"

            k_plain = uuid.uuid4()
            _, cp1 = await svc.start_chat_turn(
                session_id=seed["session_id"],
                request={"client_request_id": k_plain, "text": "Plain turn"},  # type: ignore[arg-type]
            )
            _, cp2 = await svc.start_chat_turn(
                session_id=seed["session_id"],
                request={"client_request_id": k_plain, "text": "Plain turn"},  # type: ignore[arg-type]
            )
            assert cp1 == 201 and cp2 == 200
            assert provider_calls == 2
        finally:
            await svc.aclose()

        # 6. Restart readback preserves references and resolved_evidence
        svc2 = AppService(config=make_reopened_host_config(cfg), chat_provider_factory=_spy_factory)
        try:
            page = svc2.list_chat_turns(session_id=seed["session_id"], after_seq=0, limit=10)
            assert len(page.items) == 2
            assert len(page.items[0].references) == 1
            assert len(page.items[0].resolved_evidence) == 1
            assert page.items[0].resolved_evidence[0].command_id == seed["run1_command_id"]
        finally:
            await svc2.aclose()

    asyncio.run(_verify())


def test_live_chat_wire_quota_and_replay(tmp_path: Path) -> None:
    proj_dir = tmp_path / "live_proj"
    data_dir = tmp_path / "live_data"
    seed = seed_evidence_demo(
        proj_dir,
        data_dir,
        model=ModelSelection(provider="deepseek", model="deepseek-flash"),
    )
    san_proj = tmp_path / "san_proj"
    san_data = tmp_path / "san_data"
    san_seed = seed_evidence_demo(
        san_proj,
        san_data,
        model=ModelSelection(provider="deepseek", model="deepseek-flash"),
    )

    captured_wires: list[dict[str, Any]] = []

    def _mock_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        captured_wires.append(body)
        return httpx.Response(
            200,
            json={
                "id": f"chatcmpl-{len(captured_wires)}",
                "model": "deepseek-flash",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "action": "final",
                                    "assistant_message": f"Live answer #{len(captured_wires)}",
                                }
                            )
                        },
                    }
                ],
                "usage": {"prompt_tokens": 50, "completion_tokens": 12},
            },
        )

    mock_transport = httpx.MockTransport(_mock_handler)
    base_cfg = project_helpers.make_project_host_config(tmp_path, allowed_roots=(proj_dir,))
    live_cfg = HostConfig(
        data_root=data_dir,
        workspace_parent=data_dir.parent / "workspaces",
        python_executable=base_cfg.python_executable,
        bind_port=base_cfg.bind_port,
        access_token=base_cfg.access_token,
        project_roots=(proj_dir,),
        live=LiveConfig(
            api_key="sk-synthetic-offline-key",
            model="deepseek-flash",
            max_requests=2,
            budget_cny=1.0,
        ),
    )

    async def _verify() -> None:
        svc = AppService(config=live_cfg, live_transport=mock_transport)
        try:
            # 0. Verify both original runs remain bound to original deepseek-flash session after restart
            r1_reloaded = svc.get_run(session_id=seed["session_id"], run_id=seed["run1_id"])
            r2_reloaded = svc.get_run(session_id=seed["session_id"], run_id=seed["run2_id"])
            assert next(iter((r1_reloaded.result or {})["checks"].values()))["exit_code"] == 4
            assert next(iter((r2_reloaded.result or {})["checks"].values()))["exit_code"] == 0

            # 1. Independent offline session stays offline (0 wire requests)
            offline_sess = svc.create_session(
                project_id=seed["project_id"],
                model=ModelSelection(provider="offline", model="offline-project-v1"),
            )
            t_off, _ = await svc.start_chat_turn(
                session_id=offline_sess.session_id,
                request={"client_request_id": uuid.uuid4(), "text": "Offline question"},  # type: ignore[arg-type]
            )
            assert t_off.provider_mode == "offline"
            assert len(captured_wires) == 0

            # 2. Original deepseek-flash session sends 2 turns (Run1 diff + both Run1/Run2 commands),
            #    replay adds 0, 3rd hits quota
            with pytest.raises(AppError):
                await svc.start_chat_turn(
                    session_id=seed["session_id"],
                    request={  # type: ignore[arg-type]
                        "client_request_id": uuid.uuid4(),
                        "text": "Forged ref",
                        "references": [{"kind": "command", "run_id": seed["run1_id"], "command_id": "bad"}],
                    },
                )
            assert len(captured_wires) == 0

            k1 = uuid.uuid4()
            tl1, c1 = await svc.start_chat_turn(
                session_id=seed["session_id"],
                request={  # type: ignore[arg-type]
                    "client_request_id": k1,
                    "text": "Explain Run1 diff bug",
                    "references": [
                        {
                            "kind": "diff_hunk",
                            "run_id": seed["run1_id"],
                            "path": "calc.py",
                            "before_sha256": seed["calc_before_sha"],
                            "after_sha256": seed["run1_after_sha"],
                            "old_start": 2,
                            "old_count": 4,
                            "new_start": 2,
                            "new_count": 4,
                        }
                    ],
                },
            )
            assert c1 == 201 and tl1.status == "completed" and tl1.provider_mode == "live"
            # Replay does not send a second wire request
            tl1_rep, c1_rep = await svc.start_chat_turn(
                session_id=seed["session_id"],
                request={  # type: ignore[arg-type]
                    "client_request_id": k1,
                    "text": "Explain Run1 diff bug",
                    "references": [
                        {
                            "kind": "diff_hunk",
                            "run_id": seed["run1_id"],
                            "path": "calc.py",
                            "before_sha256": seed["calc_before_sha"],
                            "after_sha256": seed["run1_after_sha"],
                            "old_start": 2,
                            "old_count": 4,
                            "new_start": 2,
                            "new_count": 4,
                        }
                    ],
                },
            )
            assert c1_rep == 200 and tl1_rep.turn_id == tl1.turn_id
            assert len(captured_wires) == 1

            tl2, _ = await svc.start_chat_turn(
                session_id=seed["session_id"],
                request={  # type: ignore[arg-type]
                    "client_request_id": uuid.uuid4(),
                    "text": "Compare Run1 and Run2 commands",
                    "references": [
                        {"kind": "command", "run_id": seed["run1_id"], "command_id": seed["run1_command_id"]},
                        {"kind": "command", "run_id": seed["run2_id"], "command_id": seed["run2_command_id"]},
                    ],
                },
            )
            assert tl2.status == "completed" and len(captured_wires) == 2

            # Inspect serialized wire payload: model, non-thinking, evidence identity, 2-turn context
            w1 = captured_wires[0]
            w2 = captured_wires[1]
            assert w1["model"] == "deepseek-flash"
            assert w1["thinking"] == {"type": "disabled"}
            assert w1["messages"][1]["role"] == "user"
            user_payload_1 = json.loads(w1["messages"][1]["content"])
            ev1 = user_payload_1["evidence"][0]
            assert ev1["run_id"] == seed["run1_id"]
            assert ev1["path"] == "calc.py"
            assert ev1["before_sha256"] == seed["calc_before_sha"]
            assert ev1["after_sha256"] == seed["run1_after_sha"]
            assert seed["diff_marker"] in ev1["excerpt"]

            assert w2["messages"][1]["role"] == "user"
            user_payload_2 = json.loads(w2["messages"][1]["content"])
            assert [
                (e["run_id"], e["command_id"], e["exit_code"]) for e in user_payload_2["evidence"]
            ] == [
                (seed["run1_id"], seed["run1_command_id"], 4),
                (seed["run2_id"], seed["run2_command_id"], 0),
            ]
            assert "Explain Run1 diff bug" in w2["messages"][1]["content"]

            # 3rd live request hits max_requests=2 -> failed with LIVE_QUOTA_EXCEEDED, no fallback to offline
            tl3, _ = await svc.start_chat_turn(
                session_id=seed["session_id"],
                request={"client_request_id": uuid.uuid4(), "text": "Over quota"},  # type: ignore[arg-type]
            )
            assert tl3.status == "failed"
            assert tl3.error_code == "LIVE_QUOTA_EXCEEDED"
            assert tl3.assistant_message is None
            assert len(captured_wires) == 2
        finally:
            await svc.aclose()

        # 3. F02: Separate synthetic command record verifying argv/failure_reason/excerpt sanitization,
        #    long-output truncation, <= 16384B wire user-content budget, and MockTransport wire messages.
        san_wires: list[dict[str, Any]] = []

        def _san_handler(request: httpx.Request) -> httpx.Response:
            san_wires.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(
                200,
                json={
                    "id": f"chatcmpl-san-{len(san_wires)}",
                    "model": "deepseek-flash",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": json.dumps(
                                    {"action": "final", "assistant_message": "Sanitized OK"}
                                )
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 40, "completion_tokens": 8},
                },
            )

        san_cfg = HostConfig(
            data_root=san_data,
            workspace_parent=san_data.parent / "workspaces",
            python_executable=base_cfg.python_executable,
            bind_port=base_cfg.bind_port,
            access_token=base_cfg.access_token,
            project_roots=(san_proj,),
            live=LiveConfig(
                api_key="sk-synthetic-offline-key",
                model="deepseek-flash",
                max_requests=4,
                budget_cny=1.0,
            ),
        )
        svc_san = AppService(config=san_cfg, live_transport=httpx.MockTransport(_san_handler))
        try:
            from types import SimpleNamespace

            san_sess = svc_san._sessions[san_seed["session_id"]]
            san_run1 = san_sess.runs[san_seed["run1_id"]]
            san_run2 = san_sess.runs[san_seed["run2_id"]]

            # Inject synthetic sensitive argv/failure_reason + >2KiB chunks on run1 (live output)
            san_run1.view.result["checks"]["chk_1"]["argv"] = [
                "python",
                "--keep-flag",
                "Bearer SYNTH_ARG_SECRET_123",
            ]
            san_run1.view.result["checks"]["chk_1"]["failure_reason"] = (
                "auth_error Bearer SYNTH_FAIL_SECRET_456"
            )
            san_run1.view.result["checks"]["chk_1"]["exit_code"] = 7
            long_text = "SYNTH_LONG_HEAD " + ("Z" * 2400) + " SYNTH_LONG_TAIL_CUT"
            san_run1.command_runner = SimpleNamespace(
                get_command=lambda cid, after=0: SimpleNamespace(
                    command_id=cid,
                    exit_code=7,
                    argv=san_run1.view.result["checks"]["chk_1"]["argv"],
                    chunks=[SimpleNamespace(text=long_text)],
                )
            )

            # Inject synthetic sensitive argv/failure_reason on run2 (expired: command_runner=None)
            san_run2.view.result["checks"]["chk_2"]["argv"] = [
                "python",
                "--safe-expired",
                "Bearer SYNTH_EXP_ARG_789",
            ]
            san_run2.view.result["checks"]["chk_2"]["failure_reason"] = (
                "expired_fail Bearer SYNTH_EXP_FAIL_012"
            )
            san_run2.command_runner = None

            t_san, code_san = await svc_san.start_chat_turn(
                session_id=san_seed["session_id"],
                request={  # type: ignore[arg-type]
                    "client_request_id": uuid.uuid4(),
                    "text": "Check sanitized live and expired commands",
                    "references": [
                        {"kind": "command", "run_id": san_seed["run1_id"], "command_id": san_seed["run1_command_id"]},
                        {"kind": "command", "run_id": san_seed["run2_id"], "command_id": san_seed["run2_command_id"]},
                    ],
                },
            )
            assert code_san == 201 and t_san.status == "completed"
            assert len(t_san.resolved_evidence) == 2
            ev_live, ev_exp = t_san.resolved_evidence
            assert ev_live.truncated is True
            assert ev_live.exit_code == 7
            assert "--keep-flag" in ev_live.argv
            assert "Bearer [REDACTED]" in " ".join(ev_live.argv)
            assert "Bearer [REDACTED]" in (ev_live.failure_reason or "")
            assert "SYNTH_LONG_HEAD" in ev_live.excerpt
            assert "SYNTH_LONG_TAIL_CUT" not in ev_live.excerpt
            assert ev_exp.availability == "raw_output_unavailable"
            assert ev_exp.note == "原始输出不可用"
            assert "--safe-expired" in ev_exp.argv
            assert "Bearer [REDACTED]" in " ".join(ev_exp.argv)
            assert "Bearer [REDACTED]" in (ev_exp.failure_reason or "")

            user_msg = next(m for m in san_wires[0]["messages"] if m.get("role") == "user")
            user_content = user_msg["content"]
            assert len(user_content.encode("utf-8")) <= 16384
            wire_payload = json.loads(user_content)
            by_key = {(e["run_id"], e["command_id"]): e for e in wire_payload["evidence"]}
            w_live = by_key[(san_seed["run1_id"], san_seed["run1_command_id"])]
            w_exp = by_key[(san_seed["run2_id"], san_seed["run2_command_id"])]
            assert w_live["exit_code"] == 7
            assert "--keep-flag" in w_live["argv"]
            assert "Bearer [REDACTED]" in " ".join(w_live["argv"])
            assert "Bearer [REDACTED]" in (w_live["failure_reason"] or "")
            assert w_live["truncated"] is True
            assert "SYNTH_LONG_HEAD" in w_live["excerpt"]
            assert "SYNTH_LONG_TAIL_CUT" not in w_live["excerpt"]
            assert w_exp["exit_code"] == 0
            assert w_exp["availability"] == "raw_output_unavailable"
            assert w_exp["note"] == "原始输出不可用"
            assert "--safe-expired" in w_exp["argv"]
            assert "Bearer [REDACTED]" in " ".join(w_exp["argv"])
            assert "Bearer [REDACTED]" in (w_exp["failure_reason"] or "")

            wire_msg = json.dumps(san_wires[0]["messages"], ensure_ascii=False)
            for secret in (
                "SYNTH_ARG_SECRET_123",
                "SYNTH_FAIL_SECRET_456",
                "SYNTH_EXP_ARG_789",
                "SYNTH_EXP_FAIL_012",
                "SYNTH_LONG_TAIL_CUT",
            ):
                assert secret not in wire_msg
            assert "Bearer [REDACTED]" in wire_msg
        finally:
            await svc_san.aclose()

    asyncio.run(_verify())


def test_live_chat_parse_diagnostic_binding(tmp_path: Path) -> None:
    """Verify outer_json / content_json JSONDecodeError diagnostics, sanitization, and original rejections."""
    import sqlite3

    proj_dir = tmp_path / "diag_proj"
    data_dir = tmp_path / "diag_data"
    seed = seed_evidence_demo(
        proj_dir,
        data_dir,
        model=ModelSelection(provider="deepseek", model="deepseek-flash"),
    )
    base_cfg = project_helpers.make_project_host_config(tmp_path, allowed_roots=(proj_dir,))
    live_cfg = HostConfig(
        data_root=data_dir,
        workspace_parent=data_dir.parent / "workspaces",
        python_executable=base_cfg.python_executable,
        bind_port=base_cfg.bind_port,
        access_token=base_cfg.access_token,
        project_roots=(proj_dir,),
        live=LiveConfig(
            api_key="sk-synthetic-offline-key",
            model="deepseek-flash",
            max_requests=20,
            budget_cny=1.0,
        ),
    )

    sentinel_outer = "SENTINEL_OUTER_αβγ_719"
    sentinel_content = "SENTINEL_CONTENT_δ_824"

    # Fixtures with known exact lengths and error offsets
    tool_call_envelope = json.dumps(
        {
            "id": "cmpl-a1",
            "model": "deepseek-flash",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(
                            {
                                "action": "tool_call",
                                "tool_name": "read_file",
                                "tool_args": {"path": "calc.py"},
                            }
                        )
                    },
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10},
        }
    ).encode("utf-8")

    outer_prefix = json.dumps(
        {
            "id": "cmpl-a2",
            "model": "deepseek-flash",
            "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
        }
    )
    outer_trailing_text = f'{outer_prefix} "{sentinel_outer}"'
    outer_trailing_pos = len(outer_prefix) + 1

    content_bad_text = f'{{"action": "final", "assistant_message": "中文_{sentinel_content}", broken}}'
    content_bad_pos = content_bad_text.index("broken")
    content_bad_envelope = json.dumps(
        {
            "id": "cmpl-b1",
            "model": "deepseek-flash",
            "choices": [{"finish_reason": "stop", "message": {"content": content_bad_text}}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10},
        }
    ).encode("utf-8")

    empty_content_envelope = json.dumps(
        {
            "id": "cmpl-empty-content",
            "model": "deepseek-flash",
            "choices": [{"finish_reason": "stop", "message": {"content": ""}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 1},
        }
    ).encode("utf-8")

    outer_syntax_text = '{"id": broken}'
    outer_syntax_pos = outer_syntax_text.index("broken")

    final_ok_envelope = json.dumps(
        {
            "id": "cmpl-final-ok",
            "model": "deepseek-flash",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "content": json.dumps(
                            {"action": "final", "assistant_message": "Normal verified final reply."}
                        )
                    },
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 12},
        }
    ).encode("utf-8")

    non_stop_envelope = json.dumps(
        {
            "id": "cmpl-non-stop",
            "model": "deepseek-flash",
            "choices": [{"finish_reason": "length", "message": {"content": "{}"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
    ).encode("utf-8")

    schema_invalid_envelope = json.dumps(
        {
            "id": "cmpl-schema-bad",
            "model": "deepseek-flash",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"content": json.dumps({"action": "unknown_action"})},
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
    ).encode("utf-8")

    scripted_responses: list[bytes] = [
        tool_call_envelope,
        outer_trailing_text.encode("utf-8"),
        content_bad_envelope,
        b"",
        empty_content_envelope,
        outer_syntax_text.encode("utf-8"),
        final_ok_envelope,
        non_stop_envelope,
        schema_invalid_envelope,
        b"\xff\xfe",
    ]
    call_seq = [0]

    def _mock_handler(_request: httpx.Request) -> httpx.Response:
        idx = call_seq[0]
        call_seq[0] += 1
        return httpx.Response(200, content=scripted_responses[idx])

    mock_transport = httpx.MockTransport(_mock_handler)

    async def _run() -> None:
        svc = AppService(config=live_cfg, live_transport=mock_transport)
        try:
            req_a = uuid.uuid4()
            turn_a, code_a = await svc.start_chat_turn(
                session_id=seed["session_id"],
                request={"client_request_id": req_a, "text": "Turn A outer trailing"},  # type: ignore[arg-type]
            )
            assert code_a == 201 and turn_a.status == "failed"

            req_b = uuid.uuid4()
            turn_b, code_b = await svc.start_chat_turn(
                session_id=seed["session_id"],
                request={"client_request_id": req_b, "text": "Turn B content syntax"},  # type: ignore[arg-type]
            )
            assert code_b == 201 and turn_b.status == "failed"

            t_empty_out, _ = await svc.start_chat_turn(
                session_id=seed["session_id"],
                request={"client_request_id": uuid.uuid4(), "text": "Empty outer"},  # type: ignore[arg-type]
            )
            t_empty_cnt, _ = await svc.start_chat_turn(
                session_id=seed["session_id"],
                request={"client_request_id": uuid.uuid4(), "text": "Empty content"},  # type: ignore[arg-type]
            )
            t_syn_out, _ = await svc.start_chat_turn(
                session_id=seed["session_id"],
                request={"client_request_id": uuid.uuid4(), "text": "Outer invalid syntax"},  # type: ignore[arg-type]
            )
            t_final_ok, _ = await svc.start_chat_turn(
                session_id=seed["session_id"],
                request={"client_request_id": uuid.uuid4(), "text": "Valid final"},  # type: ignore[arg-type]
            )
            t_non_stop, _ = await svc.start_chat_turn(
                session_id=seed["session_id"],
                request={"client_request_id": uuid.uuid4(), "text": "Non stop"},  # type: ignore[arg-type]
            )
            t_schema_bad, _ = await svc.start_chat_turn(
                session_id=seed["session_id"],
                request={"client_request_id": uuid.uuid4(), "text": "Schema invalid"},  # type: ignore[arg-type]
            )
            t_bad_utf8, _ = await svc.start_chat_turn(
                session_id=seed["session_id"],
                request={"client_request_id": uuid.uuid4(), "text": "Bad UTF-8"},  # type: ignore[arg-type]
            )
        finally:
            await svc.aclose()

        # Reopen from persistent storage and verify turn A & turn B diagnostics
        svc2 = AppService(config=live_cfg, live_transport=mock_transport)
        try:
            re_a = svc2.get_chat_turn_by_client_request_id(
                session_id=seed["session_id"], client_request_id=str(req_a)
            )
            re_b = svc2.get_chat_turn_by_client_request_id(
                session_id=seed["session_id"], client_request_id=str(req_b)
            )
            assert re_a is not None and re_b is not None
            assert re_a.turn_id == turn_a.turn_id and re_b.turn_id == turn_b.turn_id
            assert re_a.turn_id != re_b.turn_id

            expected_a = (
                "live chat provider returned invalid envelope: JSONDecodeError "
                f"(stage=outer_json, turn_id={re_a.turn_id}, call_index=2, "
                f"input_chars={len(outer_trailing_text)}, error_pos={outer_trailing_pos}, "
                "category=trailing_data)"
            )
            expected_b = (
                "live chat provider returned invalid envelope: JSONDecodeError "
                f"(stage=content_json, turn_id={re_b.turn_id}, call_index=1, "
                f"input_chars={len(content_bad_text)}, error_pos={content_bad_pos}, "
                "category=invalid_syntax)"
            )
            assert re_a.status == "failed"
            assert re_a.error_code == "LIVE_PROVIDER_INVALID"
            assert re_a.error_message == expected_a
            assert re_a.assistant_message is None

            assert re_b.status == "failed"
            assert re_b.error_code == "LIVE_PROVIDER_INVALID"
            assert re_b.error_message == expected_b
            assert re_b.assistant_message is None

            # Verify empty_input and non-trailing invalid_syntax controls
            assert t_empty_out.error_code == "LIVE_PROVIDER_INVALID"
            assert t_empty_out.error_message == (
                "live chat provider returned invalid envelope: JSONDecodeError "
                f"(stage=outer_json, turn_id={t_empty_out.turn_id}, call_index=1, "
                "input_chars=0, error_pos=0, category=empty_input)"
            )
            assert t_empty_cnt.error_code == "LIVE_PROVIDER_INVALID"
            assert t_empty_cnt.error_message == (
                "live chat provider returned invalid envelope: JSONDecodeError "
                f"(stage=content_json, turn_id={t_empty_cnt.turn_id}, call_index=1, "
                "input_chars=0, error_pos=0, category=empty_input)"
            )
            assert t_syn_out.error_code == "LIVE_PROVIDER_INVALID"
            assert t_syn_out.error_message == (
                "live chat provider returned invalid envelope: JSONDecodeError "
                f"(stage=outer_json, turn_id={t_syn_out.turn_id}, call_index=1, "
                f"input_chars={len(outer_syntax_text)}, error_pos={outer_syntax_pos}, "
                "category=invalid_syntax)"
            )

            # Normal final & original rejection controls (no JSON stage mislabeling)
            assert t_final_ok.status == "completed"
            assert t_final_ok.assistant_message == "Normal verified final reply."

            assert t_non_stop.status == "failed"
            assert t_non_stop.error_code == "LIVE_PROVIDER_INVALID"
            assert t_non_stop.error_message == "live chat provider returned invalid envelope: ValueError"

            assert t_schema_bad.status == "failed"
            assert t_schema_bad.error_code == "LIVE_PROVIDER_INVALID"
            assert t_schema_bad.error_message == "live chat provider returned invalid envelope: ValidationError"

            assert t_bad_utf8.status == "failed"
            assert t_bad_utf8.error_code == "LIVE_PROVIDER_INVALID"
            assert t_bad_utf8.error_message == "live chat provider returned invalid envelope: UnicodeDecodeError"

            hist = svc2.get_session_history(session_id=seed["session_id"])
            hist_dump = hist.model_dump_json()
            for secret in (sentinel_outer, sentinel_content, "broken", "cmpl-a2", "cmpl-b1"):
                assert secret not in hist_dump
        finally:
            await svc2.aclose()

        # Verify sentinels never persisted anywhere in sessions.sqlite3
        db_uri = (data_dir / "sessions.sqlite3").as_uri() + "?mode=ro"
        with sqlite3.connect(db_uri, uri=True) as conn:
            tables = [
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            ]
            for tbl in tables:
                for row in conn.execute(f'SELECT * FROM "{tbl}"').fetchall():
                    row_text = " ".join(str(cell) for cell in row if cell is not None)
                    assert sentinel_outer not in row_text
                    assert sentinel_content not in row_text

    asyncio.run(_run())
    assert call_seq[0] == len(scripted_responses)
