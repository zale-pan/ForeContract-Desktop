"""S19–S24: Core RuntimeEngine + Planner integration, failure feedback, oracle independence, capacity, concurrency, and compatibility."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from extension_helpers import (
    CallbackProjectProvider,
    LocalHttpMcpServer,
    ext_forecast,
    make_extension_host_config,
    make_skill,
    write_extension_config_file,
    write_stdio_mcp_script,
)
from forecontract.models.actions import ActionSpec
from forecontract.protocols.adapter import UnsupportedActionError
from forecontract.runtime.engine import RuntimeEngine
from forecontract.tracing import JsonlTraceStore
from forecontract_app.extension_models import (
    ExtensionCleanup,
    ExtensionConfig,
    HttpMcpServerConfig,
)
from forecontract_app.extension_runtime import EXTENSION_ACTION_NAMES, ExtensionRuntime
from forecontract_app.models import ModelSelection, StartRunRequest
from forecontract_app.project_adapter import ProjectCodingAdapter
from forecontract_app.project_commands import ProjectCommandRunner
from forecontract_app.project_demo import build_registered_project_task
from forecontract_app.project_models import (
    PROJECT_SCHEMA_VERSION,
    RegisterProjectRequest,
)
from forecontract_app.project_permissions import PermissionBroker
from forecontract_app.project_runtime import run_project_task
from forecontract_app.project_workspace import ProjectWorkspace
from forecontract_app.service import AppService
from forecontract_llm import ModelRequest
from project_helpers import create_buggy_calc_project


def test_s19_core_contract(tmp_path: Path, monkeypatch) -> None:
    proj = create_buggy_calc_project(tmp_path / "proj")
    make_skill(proj, "s19-skill", "# Skill 19\nUse calc_hint.\n")

    # 1. Default extensions=None strictly keeps 13 actions & rejects extension actions
    ws0 = ProjectWorkspace(root=proj, data_root=tmp_path / "d0")
    canon = os.path.normcase(os.path.realpath(proj))
    task0, _ = build_registered_project_task(
        project_id="p0",
        project_name="p0",
        project_root=proj,
        root_identity=canon,
        read_grant_id="rg0",
        data_root=tmp_path / "d0",
        output_root=tmp_path / "o0",
        run_id="r0",
        goal="default check",
        python_executable=Path(sys.executable),
    )
    (tmp_path / "d0").mkdir(parents=True, exist_ok=True)
    (tmp_path / "o0").mkdir(parents=True, exist_ok=True)
    b0 = PermissionBroker(run_id="r0", project_root=proj, project_id="p0", session_id="s0", root_identity=canon)
    r0 = ProjectCommandRunner(run_id="r0", project_root=proj, data_root=tmp_path / "d0", broker=b0, emit=lambda k, p: None)
    ad0 = ProjectCodingAdapter(task=task0, workspace=ws0, broker=b0, command_runner=r0)
    assert len(ad0.capabilities().supported_actions) == 13
    with pytest.raises(UnsupportedActionError):
        ad0.validate_action(
            ActionSpec(
                schema_version=PROJECT_SCHEMA_VERSION,
                action_type="list_skills",
                payload={},
                metadata={"action_id": "x"},
            )
        )

    # 2. With ExtensionRuntime: Core + LLMPlanner exercise all 6 extension actions; observe real Core commits/executions
    with LocalHttpMcpServer(label="s19-srv") as http_srv:
        cfg = ExtensionConfig(version=1, servers=[HttpMcpServerConfig(id="srv-19", transport="http", url=http_srv.url)])
        trace_path = tmp_path / "trace.jsonl"
        trace_store = JsonlTraceStore(trace_path)
        commits: list[dict[str, Any]] = []
        executions: list[dict[str, Any]] = []

        orig_commit = RuntimeEngine._commit_contract
        orig_execute = ProjectCodingAdapter.execute

        def observe_commit(self, request, progress, contract, previous, replaces):
            result = orig_commit(self, request, progress, contract, previous, replaces)
            if request.run_id == "r19" and contract.action.action_type in EXTENSION_ACTION_NAMES:
                commits.append(
                    {
                        "cid": contract.contract_id,
                        "sid": progress.current_step,
                        "aid": contract.action.metadata["action_id"],
                        "action": contract.action.model_dump(mode="json"),
                    }
                )
            return result

        async def observe_execute(self, action):
            watched = action.action_type in EXTENSION_ACTION_NAMES
            binding = None
            if watched:
                matches = [c for c in commits if c["aid"] == action.metadata["action_id"]]
                assert matches, "extension execute without prior real Core commit"
                binding = matches[-1]
                assert binding["action"] == action.model_dump(mode="json")
                started = [
                    e
                    for e in trace_store.read_run("r19")
                    if e.kind == "attempt_started"
                    and e.contract_id == binding["cid"]
                    and e.step_id == binding["sid"]
                ]
                assert started
                attempt = started[-1]
            outcome = await orig_execute(self, action)
            if watched:
                assert outcome.changes["action_id"] == binding["aid"]
                assert outcome.changes["action_type"] == action.action_type
                assert outcome.outcome_id
                executions.append(
                    {
                        **binding,
                        "attempt_id": attempt.attempt_id,
                        "outcome_id": outcome.outcome_id,
                        "status": outcome.status,
                    }
                )
            return outcome

        async def _run() -> None:
            data_root = tmp_path / "data"
            out_root = tmp_path / "out"
            data_root.mkdir(parents=True, exist_ok=True)
            out_root.mkdir(parents=True, exist_ok=True)
            task, pcfg = build_registered_project_task(
                project_id="p19",
                project_name="p19",
                project_root=proj,
                root_identity=canon,
                read_grant_id="rg19",
                data_root=data_root,
                output_root=out_root,
                run_id="r19",
                goal="exercise 6 extension actions",
                python_executable=Path(sys.executable),
                session_id="s19",
                max_steps=8,
                max_planning_calls=8,
            )
            broker = PermissionBroker(run_id="r19", project_root=proj, project_id="p19", session_id="s19", root_identity=canon)
            ext = ExtensionRuntime(
                project_id="p19",
                session_id="s19",
                run_id="r19",
                project_root=proj,
                config=cfg,
                selected_skill_ids=("s19-skill",),
                scratch_root=tmp_path / "ext_scratch",
                emit=lambda k, p: None,
            )

            seen_actions: list[str] = []

            def _decide(step: int, req: ModelRequest, state: dict[str, Any]) -> str:
                _ = req, state
                seq = [
                    ("list_skills", {}),
                    ("read_skill", {"skill_id": "s19-skill"}),
                    ("list_mcp_servers", {}),
                    ("connect_mcp", {"server_id": "srv-19"}),
                    ("list_mcp_tools", {"server_id": "srv-19"}),
                    ("call_mcp", {"server_id": "srv-19", "tool_name": "echo_tool", "arguments": {"msg": "core"}}),
                ]
                idx = min(step - 1, len(seq) - 1)
                at, pl = seq[idx]
                seen_actions.append(at)
                return ext_forecast(action_type=at, payload=pl)

            prov = CallbackProjectProvider(_decide)

            async def _approve_ext() -> None:
                for _ in range(400):
                    for p in ext.list_proposals():
                        if p.state == "pending":
                            await ext.decide(proposal_id=p.id, digest=p.digest, decision="approve")
                    await asyncio.sleep(0.01)

            t_app = asyncio.create_task(_approve_ext())
            try:
                await run_project_task(
                    task=task,
                    provider=prov,
                    planner_config=pcfg,
                    permissions=broker,
                    trace_writer=trace_store,
                    emit=lambda k, p: None,
                    extensions=ext,
                )
            finally:
                t_app.cancel()

            for act in EXTENSION_ACTION_NAMES:
                assert act in seen_actions
            assert ext.cleanup_result is not None and ext.cleanup_result.cleanup_ok

        with monkeypatch.context() as mp:
            mp.setattr(RuntimeEngine, "_commit_contract", observe_commit)
            mp.setattr(ProjectCodingAdapter, "execute", observe_execute)
            asyncio.run(_run())

        assert {x["action"]["action_type"] for x in executions} == set(EXTENSION_ACTION_NAMES)
        assert len({x["outcome_id"] for x in executions}) == len(executions)
        records = trace_store.read_run("r19")
        for x in executions:
            committed = [
                e
                for e in records
                if e.kind == "contract_committed" and e.contract_id == x["cid"] and e.step_id == x["sid"]
            ]
            starts = [
                e
                for e in records
                if e.kind == "attempt_started"
                and e.contract_id == x["cid"]
                and e.step_id == x["sid"]
                and e.attempt_id == x["attempt_id"]
            ]
            finishes = [
                e
                for e in records
                if e.kind == "attempt_finished"
                and e.contract_id == x["cid"]
                and e.step_id == x["sid"]
                and e.attempt_id == x["attempt_id"]
            ]
            checks = [
                e
                for e in records
                if e.kind == "verification_recorded"
                and e.scope == "attempt"
                and e.contract_id == x["cid"]
                and e.step_id == x["sid"]
                and e.attempt_id == x["attempt_id"]
            ]
            assert len(committed) == len(starts) == len(finishes) == len(checks) == 1
            assert committed[0].sequence < starts[0].sequence < finishes[0].sequence < checks[0].sequence
            assert finishes[0].execution_status == x["status"]


def test_s20_feedback(tmp_path: Path) -> None:
    proj = create_buggy_calc_project(tmp_path / "proj")
    canon = os.path.normcase(os.path.realpath(proj))
    log_s = tmp_path / "logs" / "s20.jsonl"
    sc_s = write_stdio_mcp_script(tmp_path / "scripts", name="s20.py", log_path=log_s, mode="normal")

    async def _run() -> None:
        from forecontract_app.extension_models import StdioMcpServerConfig

        srv = StdioMcpServerConfig(
            id="srv-20", transport="stdio", executable=sys.executable, argv=["-B", str(sc_s)], cwd=str(tmp_path)
        )
        cfg = ExtensionConfig(version=1, servers=[srv])
        data_root = tmp_path / "d20"
        out_root = tmp_path / "o20"
        data_root.mkdir(parents=True, exist_ok=True)
        out_root.mkdir(parents=True, exist_ok=True)
        task, pcfg = build_registered_project_task(
            project_id="p20",
            project_name="p20",
            project_root=proj,
            root_identity=canon,
            read_grant_id="rg20",
            data_root=data_root,
            output_root=out_root,
            run_id="r20",
            goal="read MCP error and retry dynamically",
            python_executable=Path(sys.executable),
            session_id="s20",
            max_steps=6,
            max_planning_calls=6,
        )
        broker = PermissionBroker(run_id="r20", project_root=proj, project_id="p20", session_id="s20", root_identity=canon)
        ext = ExtensionRuntime(
            project_id="p20",
            session_id="s20",
            run_id="r20",
            project_root=proj,
            config=cfg,
            selected_skill_ids=(),
            scratch_root=tmp_path / "sc20",
            emit=lambda k, p: None,
        )

        failed_call_ids: list[str] = []
        second_attempt_req_text: list[str] = []

        def _decide(step: int, req: ModelRequest, state: dict[str, Any]) -> str:
            last = state.get("last_tool_result") or {}
            if step == 1:
                return ext_forecast(action_type="connect_mcp", payload={"server_id": "srv-20"})
            if step == 2:
                return ext_forecast(action_type="list_mcp_tools", payload={"server_id": "srv-20"})
            if step == 3:
                return ext_forecast(
                    action_type="call_mcp",
                    payload={"server_id": "srv-20", "tool_name": "fail_tool", "arguments": {"attempt": 1}},
                )
            if last.get("action_type") == "call_mcp" and last.get("is_error") is True:
                failed_call_ids.append(str(last["call_id"]))
                second_attempt_req_text.append(json.dumps(req.model_dump(mode="json"), ensure_ascii=False))
                assert "retry with attempt=2" in str(last.get("text", ""))
                return ext_forecast(
                    action_type="call_mcp",
                    payload={"server_id": "srv-20", "tool_name": "fail_tool", "arguments": {"attempt": 2}},
                )
            return ext_forecast(action_type="list_directory", payload={})

        prov = CallbackProjectProvider(_decide)

        async def _approver() -> None:
            for _ in range(400):
                for p in ext.list_proposals():
                    if p.state == "pending":
                        await ext.decide(proposal_id=p.id, digest=p.digest, decision="approve")
                await asyncio.sleep(0.01)

        t_app = asyncio.create_task(_approver())
        try:
            await run_project_task(
                task=task,
                provider=prov,
                planner_config=pcfg,
                permissions=broker,
                trace_writer=JsonlTraceStore(tmp_path / "trace20.jsonl"),
                emit=lambda k, p: None,
                extensions=ext,
            )
        finally:
            t_app.cancel()

        assert len(failed_call_ids) == 1
        assert failed_call_ids[0] in second_attempt_req_text[0]
        calls = ext.build_run_extensions_view().calls
        assert len(calls) == 2
        assert calls[0].status == "error" and calls[0].is_error
        assert calls[1].status == "success" and not calls[1].is_error and "attempt_2_ok" in calls[1].text

    asyncio.run(_run())


def test_s21_completion_oracle(tmp_path: Path) -> None:
    proj = create_buggy_calc_project(tmp_path / "proj")
    canon = os.path.normcase(os.path.realpath(proj))

    with LocalHttpMcpServer(label="s21-srv") as http_srv:
        cfg = ExtensionConfig(version=1, servers=[HttpMcpServerConfig(id="srv-21", transport="http", url=http_srv.url)])

        # 1. MCP claims "tests pass" WITHOUT verify_project -> terminal_status MUST NOT be succeeded
        async def _run_no_verify() -> None:
            d1 = tmp_path / "d21a"
            o1 = tmp_path / "o21a"
            d1.mkdir(parents=True, exist_ok=True)
            o1.mkdir(parents=True, exist_ok=True)
            task, pcfg = build_registered_project_task(
                project_id="p21a",
                project_name="p21a",
                project_root=proj,
                root_identity=canon,
                read_grant_id="rg21a",
                data_root=d1,
                output_root=o1,
                run_id="r21a",
                goal="no verify check",
                python_executable=Path(sys.executable),
                max_steps=4,
                max_planning_calls=4,
            )
            broker = PermissionBroker(run_id="r21a", project_root=proj, project_id="p21a", session_id="s21a", root_identity=canon)
            ext = ExtensionRuntime(
                project_id="p21a",
                session_id="s21a",
                run_id="r21a",
                project_root=proj,
                config=cfg,
                selected_skill_ids=(),
                scratch_root=tmp_path / "sc21a",
                emit=lambda k, p: None,
            )

            def _decide(step: int, req: ModelRequest, state: dict[str, Any]) -> str:
                _ = req, state
                if step == 1:
                    return ext_forecast(action_type="connect_mcp", payload={"server_id": "srv-21"})
                if step == 2:
                    return ext_forecast(action_type="list_mcp_tools", payload={"server_id": "srv-21"})
                return ext_forecast(
                    action_type="call_mcp",
                    payload={"server_id": "srv-21", "tool_name": "calc_hint", "arguments": {"target": "calc.py"}},
                    task_hint=True,
                )

            async def _approver() -> None:
                for _ in range(400):
                    for p in ext.list_proposals():
                        if p.state == "pending":
                            await ext.decide(proposal_id=p.id, digest=p.digest, decision="approve")
                    await asyncio.sleep(0.01)

            ta = asyncio.create_task(_approver())
            try:
                no_project_verification_run = await run_project_task(
                    task=task,
                    provider=CallbackProjectProvider(_decide),
                    planner_config=pcfg,
                    permissions=broker,
                    trace_writer=JsonlTraceStore(tmp_path / "t21a.jsonl"),
                    emit=lambda k, p: None,
                    extensions=ext,
                )
            finally:
                ta.cancel()
            assert no_project_verification_run.terminal_status != "succeeded"

        asyncio.run(_run_no_verify())

        # 2. Real edit + real junit_exact verify_project passes oracle
        async def _run_verified() -> None:
            d2 = tmp_path / "d21b"
            o2 = tmp_path / "o21b"
            d2.mkdir(parents=True, exist_ok=True)
            o2.mkdir(parents=True, exist_ok=True)
            task, pcfg = build_registered_project_task(
                project_id="p21b",
                project_name="p21b",
                project_root=proj,
                root_identity=canon,
                read_grant_id="rg21b",
                data_root=d2,
                output_root=o2,
                run_id="r21b",
                goal="fix calc using mcp hint and real verify",
                python_executable=Path(sys.executable),
                max_steps=12,
                max_planning_calls=12,
            )
            broker = PermissionBroker(run_id="r21b", project_root=proj, project_id="p21b", session_id="s21b", root_identity=canon)
            ext = ExtensionRuntime(
                project_id="p21b",
                session_id="s21b",
                run_id="r21b",
                project_root=proj,
                config=cfg,
                selected_skill_ids=(),
                scratch_root=tmp_path / "sc21b",
                emit=lambda k, p: None,
            )

            def _decide(step: int, req: ModelRequest, state: dict[str, Any]) -> str:
                _ = req
                last = state.get("last_tool_result") or {}
                if step == 1:
                    return ext_forecast(action_type="connect_mcp", payload={"server_id": "srv-21"})
                if step == 2:
                    return ext_forecast(action_type="list_mcp_tools", payload={"server_id": "srv-21"})
                if step == 3:
                    return ext_forecast(
                        action_type="call_mcp",
                        payload={"server_id": "srv-21", "tool_name": "calc_hint", "arguments": {"target": "calc.py"}},
                    )
                if step == 4:
                    return ext_forecast(action_type="read_file", payload={"path": "calc.py"})
                if step == 5:
                    return ext_forecast(
                        action_type="propose_scope",
                        payload={
                            "exact_paths": ["calc.py"],
                            "write_prefixes": [],
                            "operations": ["modify"],
                            "protected_paths": ["tests/test_calc.py"],
                            "verification_plan_summary": "pytest junit exact",
                        },
                    )
                if step == 6:
                    return ext_forecast(
                        action_type="propose_verification",
                        payload={
                            "plan_id": "plan-s21",
                            "summary": "run calc pytest",
                            "checks": [
                                {
                                    "check_id": "unit",
                                    "kind": "junit_exact",
                                    "executable": sys.executable,
                                    "argv": ["-B", "-m", "pytest", "-p", "no:cacheprovider", "tests/test_calc.py", "--junitxml={result_dir}/junit.xml"],
                                    "cwd": ".",
                                    "timeout_ms": 20000,
                                    "protected_paths": ["tests/test_calc.py"],
                                    "expected_tests": [{"classname": "tests.test_calc", "name": "test_add_basic"}],
                                }
                            ],
                        },
                    )
                if step == 7:
                    return ext_forecast(action_type="read_file", payload={"path": "calc.py"})
                if last.get("action_type") == "read_file" and (last.get("sha256") or last.get("file_sha256")):
                    file_sha = last.get("sha256") or last.get("file_sha256")
                    return ext_forecast(
                        action_type="edit_file",
                        payload={
                            "path": "calc.py",
                            "expected_sha256": file_sha,
                            "old_text": "return a - b",
                            "new_text": "return a + b",
                        },
                        outcome_type="project_file_written",
                        outcome_payload={"path": "calc.py"},
                    )
                return ext_forecast(
                    action_type="verify_project",
                    payload={
                        "plan_id": state["plan_id"],
                        "plan_digest": state["plan_digest"],
                    },
                    outcome_type="project_task_verified",
                    outcome_payload={},
                    task_hint=True,
                )

            async def _approver() -> None:
                for _ in range(800):
                    for p in ext.list_proposals():
                        if p.state == "pending":
                            await ext.decide(proposal_id=p.id, digest=p.digest, decision="approve")
                    for bp in broker.pending_proposals():
                        broker.decide(
                            proposal_id=bp.id,
                            request={"digest": bp.digest, "decision": "approve", "trusted_local_execution": True},
                        )
                    await asyncio.sleep(0.01)

            ta = asyncio.create_task(_approver())
            try:
                ok_run = await run_project_task(
                    task=task,
                    provider=CallbackProjectProvider(_decide),
                    planner_config=pcfg,
                    permissions=broker,
                    trace_writer=JsonlTraceStore(tmp_path / "t21b.jsonl"),
                    emit=lambda k, p: None,
                    extensions=ext,
                )
            finally:
                ta.cancel()
            assert ok_run.terminal_status == "succeeded" and ok_run.verification == "passed"

        asyncio.run(_run_verified())


def test_s22_capacity(tmp_path: Path) -> None:
    proj = create_buggy_calc_project(tmp_path / "proj")
    canon = os.path.normcase(os.path.realpath(proj))
    make_skill(proj, "long-skill", "SKILL_KEEP_MARKER_" + ("K" * 12000))
    skill_file = proj / ".forecontract/skills/long-skill/SKILL.md"
    real_sha = hashlib.sha256(skill_file.read_bytes()).hexdigest()

    with LocalHttpMcpServer(label="s22-long") as http_srv:
        cfg = ExtensionConfig(version=1, servers=[HttpMcpServerConfig(id="srv-22", transport="http", url=http_srv.url)])

        async def _run() -> None:
            d = tmp_path / "d22"
            o = tmp_path / "o22"
            d.mkdir(parents=True, exist_ok=True)
            o.mkdir(parents=True, exist_ok=True)
            task, _pcfg = build_registered_project_task(
                project_id="p22",
                project_name="p22",
                project_root=proj,
                root_identity=canon,
                read_grant_id="rg22",
                data_root=d,
                output_root=o,
                run_id="r22",
                goal="capacity compaction preserves skill and constraints",
                python_executable=Path(sys.executable),
                max_steps=5,
                max_planning_calls=5,
            )
            broker = PermissionBroker(run_id="r22", project_root=proj, project_id="p22", session_id="s22", root_identity=canon)
            ext = ExtensionRuntime(
                project_id="p22",
                session_id="s22",
                run_id="r22",
                project_root=proj,
                config=cfg,
                selected_skill_ids=("long-skill",),
                scratch_root=tmp_path / "sc22",
                emit=lambda k, p: None,
            )
            # Invoke big output tool (30 KB text) -> bounded to 8 KiB with truncated=True
            ws = ProjectWorkspace(root=proj, data_root=d)
            runner = ProjectCommandRunner(run_id="r22", project_root=proj, data_root=d, broker=broker, emit=lambda k, p: None)
            ad = ProjectCodingAdapter(task=task, workspace=ws, broker=broker, command_runner=runner, extensions=ext)
            await ad.start()

            async def _auto() -> None:
                for _ in range(300):
                    for p in ext.list_proposals():
                        if p.state == "pending":
                            await ext.decide(proposal_id=p.id, digest=p.digest, decision="approve")
                    await asyncio.sleep(0.01)

            ta = asyncio.create_task(_auto())
            try:
                await ad.execute(ActionSpec(schema_version=PROJECT_SCHEMA_VERSION, action_type="connect_mcp", payload={"server_id": "srv-22"}, metadata={"action_id": "a1"}))
                await ad.execute(ActionSpec(schema_version=PROJECT_SCHEMA_VERSION, action_type="list_mcp_tools", payload={"server_id": "srv-22"}, metadata={"action_id": "a2"}))
                out = await ad.execute(
                    ActionSpec(
                        schema_version=PROJECT_SCHEMA_VERSION,
                        action_type="call_mcp",
                        payload={"server_id": "srv-22", "tool_name": "echo_tool", "arguments": {"msg": "Y" * 10000}},
                        metadata={"action_id": "a3"},
                    )
                )
                assert out.changes["truncated"] is True
                obs = await ad.observe()
                raw_bytes = len(obs.model_dump_json().encode("utf-8"))
                assert raw_bytes <= 65536
                sel_skills = obs.state["extensions"]["selected_skills"]
                assert len(sel_skills) == 1
                assert sel_skills[0]["content"] == "SKILL_KEEP_MARKER_" + ("K" * 12000)
                assert sel_skills[0]["path"] == ".forecontract/skills/long-skill/SKILL.md"
                assert sel_skills[0]["sha256"] == real_sha
                assert sel_skills[0]["source_trust"] == "untrusted_skill"
                assert task.goal_text == "capacity compaction preserves skill and constraints"
                assert task.root_identity == canon
                assert task.read_grant_id


                # Trigger >64 KiB observation compaction: sets observation_overflow/state_complete/extensions_truncated
                # while keeping selected_skills content and security/permission state intact
                ad._last_tool_result = {
                    "action_id": "a-over-64k",
                    "action_type": "read_file",
                    "tool_succeeded": True,
                    "content": "W" * 70000,
                }
                obs_over = await ad.observe()
                assert len(obs_over.model_dump_json().encode("utf-8")) <= 65536
                assert obs_over.state["observation_overflow"] is True
                assert obs_over.state["state_complete"] is False
                assert obs_over.state["extensions"]["extensions_truncated"] is True
                sel_skills_over = obs_over.state["extensions"]["selected_skills"]
                assert len(sel_skills_over) == 1
                assert sel_skills_over[0]["content"] == "SKILL_KEEP_MARKER_" + ("K" * 12000)
                assert sel_skills_over[0]["path"] == ".forecontract/skills/long-skill/SKILL.md"
                assert sel_skills_over[0]["sha256"] == real_sha
                assert obs_over.state["scope_ok"] is True
                assert obs_over.state["root_identity"] == canon
                assert "verification" in obs_over.state

                # 32 tool calls per run succeed; 33rd call rejected with MCP_CALL_QUOTA_EXCEEDED
                for idx in range(2, 33):
                    res_i = await ad.execute(
                        ActionSpec(
                            schema_version=PROJECT_SCHEMA_VERSION,
                            action_type="call_mcp",
                            payload={
                                "server_id": "srv-22",
                                "tool_name": "echo_tool",
                                "arguments": {"msg": f"call-{idx}"},
                            },
                            metadata={"action_id": f"a-quota-{idx}"},
                        )
                    )
                    assert res_i.changes["tool_succeeded"] is True
                assert len(ext._calls) == 32
                assert http_srv.call_count == 32

                res_33 = await ad.execute(
                    ActionSpec(
                        schema_version=PROJECT_SCHEMA_VERSION,
                        action_type="call_mcp",
                        payload={
                            "server_id": "srv-22",
                            "tool_name": "echo_tool",
                            "arguments": {"msg": "call-33-blocked"},
                        },
                        metadata={"action_id": "a-quota-33"},
                    )
                )
                assert res_33.changes["tool_succeeded"] is False
                assert res_33.changes["error_code"] == "MCP_CALL_QUOTA_EXCEEDED"
                assert res_33.changes["effect_applied"] is False
                assert http_srv.call_count == 32
            finally:
                ta.cancel()
                await ad.stop()

            # 1 MiB cumulative call data limit: calls within 1 MiB succeed, next call rejected
            ext_bytes = ExtensionRuntime(
                project_id="p22",
                session_id="s22",
                run_id="r22-bytes",
                project_root=proj,
                config=cfg,
                selected_skill_ids=(),
                scratch_root=tmp_path / "sc22_bytes",
                emit=lambda k, p: None,
            )

            async def _auto_bytes() -> None:
                for _ in range(300):
                    for p in ext_bytes.list_proposals():
                        if p.state == "pending":
                            await ext_bytes.decide(
                                proposal_id=p.id, digest=p.digest, decision="approve"
                            )
                    await asyncio.sleep(0.01)

            tb = asyncio.create_task(_auto_bytes())
            try:
                await ext_bytes.execute(
                    ActionSpec(
                        schema_version=PROJECT_SCHEMA_VERSION,
                        action_type="connect_mcp",
                        payload={"server_id": "srv-22"},
                        metadata={"action_id": "b-conn"},
                    )
                )
                await ext_bytes.execute(
                    ActionSpec(
                        schema_version=PROJECT_SCHEMA_VERSION,
                        action_type="list_mcp_tools",
                        payload={"server_id": "srv-22"},
                        metadata={"action_id": "b-list"},
                    )
                )
                for b_idx in range(1, 5):
                    rb = await ext_bytes.execute(
                        ActionSpec(
                            schema_version=PROJECT_SCHEMA_VERSION,
                            action_type="call_mcp",
                            payload={
                                "server_id": "srv-22",
                                "tool_name": "bulk_tool",
                                "arguments": {"data": "B" * 100000},
                            },
                            metadata={"action_id": f"b-call-{b_idx}"},
                        )
                    )
                    assert rb["tool_succeeded"] is True
                assert ext_bytes._total_call_bytes > 1048576
                calls_before = http_srv.call_count
                rb_over = await ext_bytes.execute(
                    ActionSpec(
                        schema_version=PROJECT_SCHEMA_VERSION,
                        action_type="call_mcp",
                        payload={
                            "server_id": "srv-22",
                            "tool_name": "bulk_tool",
                            "arguments": {"data": "overflow"},
                        },
                        metadata={"action_id": "b-call-over"},
                    )
                )
                assert rb_over["tool_succeeded"] is False
                assert rb_over["error_code"] == "MCP_DATA_QUOTA_EXCEEDED"
                assert rb_over["effect_applied"] is False
                assert http_srv.call_count == calls_before
            finally:
                tb.cancel()
                await ext_bytes.aclose()

                # Capture the actual ModelRequest (not ad.observe or the local task object): goal, task_criteria,
                # capabilities/auth identity, and the real skill-bytes SHA on a fresh run + fresh server.
                with LocalHttpMcpServer(label="s22-cap") as http_cap:
                    cfg_cap = ExtensionConfig(
                        version=1, servers=[HttpMcpServerConfig(id="srv-22-cap", transport="http", url=http_cap.url)]
                    )
                    task_cap, pcfg_cap = build_registered_project_task(
                        project_id="p22", project_name="p22", project_root=proj, root_identity=canon,
                        read_grant_id="rg22", data_root=d, output_root=o, run_id="r22-cap",
                        goal="capacity compaction preserves skill and constraints",
                        python_executable=Path(sys.executable), max_steps=4, max_planning_calls=4,
                    )
                    broker_cap = PermissionBroker(run_id="r22-cap", project_root=proj, project_id="p22",
                                                  session_id="s22", root_identity=canon)
                    ext_cap = ExtensionRuntime(
                        project_id="p22", session_id="s22", run_id="r22-cap", project_root=proj, config=cfg_cap,
                        selected_skill_ids=("long-skill",), scratch_root=tmp_path / "sc22_cap",
                        emit=lambda k, p: None,
                    )
                    captured_s22: list[ModelRequest] = []

                    def _decide_cap(step: int, req: ModelRequest, state: dict[str, Any]) -> str:
                        _ = state
                        captured_s22.append(req)
                        if step == 1:
                            return ext_forecast(action_type="connect_mcp", payload={"server_id": "srv-22-cap"})
                        if step == 2:
                            return ext_forecast(action_type="list_mcp_tools", payload={"server_id": "srv-22-cap"})
                        if step == 3:
                            return ext_forecast(
                                action_type="call_mcp",
                                payload={"server_id": "srv-22-cap", "tool_name": "echo_tool", "arguments": {"msg": "Y" * 10000}},
                            )
                        return ext_forecast(action_type="list_mcp_tools", payload={"server_id": "srv-22-cap"})

                    prov_cap = CallbackProjectProvider(_decide_cap)

                    async def _auto_cap() -> None:
                        for _ in range(300):
                            for prop in ext_cap.list_proposals():
                                if prop.state == "pending":
                                    await ext_cap.decide(proposal_id=prop.id, digest=prop.digest, decision="approve")
                            await asyncio.sleep(0.01)

                    # Real recent session history (conversation) that, combined with the long Skill and the tool
                    # output, exceeds the 64KiB observation cap and triggers real session compaction.
                    from forecontract_app.session_context import (
                        ConversationRuntime,
                        build_context_frame,
                    )
                    from forecontract_app.session_models import HistoryItem

                    cap_pid = str(uuid.uuid4())
                    cap_sid = str(uuid.uuid4())
                    cap_rid = str(uuid.uuid4())
                    cap_constraints = "read-only local project: no network writes"
                    hist_items = []
                    for hi in range(14):
                        hist_items.append(
                            HistoryItem(
                                seq=hi + 1,
                                item_id=str(uuid.uuid4()),
                                project_id=cap_pid,
                                session_id=cap_sid,
                                run_id=cap_rid,
                                kind="run_result",
                                text="H" * 6000,
                                source_ids=[],
                                truncated=False,
                                sha256=hashlib.sha256(f"h{hi}".encode()).hexdigest(),
                            )
                        )
                    cap_frame = build_context_frame(
                        project_id=cap_pid,
                        session_id=cap_sid,
                        run_id=cap_rid,
                        goal="capacity compaction preserves skill and constraints",
                        constraints=cap_constraints,
                        history=hist_items,
                    )
                    conv = ConversationRuntime(context_frame=cap_frame)

                    t_cap = asyncio.create_task(_auto_cap())
                    try:
                        await run_project_task(
                            task=task_cap, provider=prov_cap, planner_config=pcfg_cap, permissions=broker_cap,
                            trace_writer=JsonlTraceStore(tmp_path / "s22-cap.jsonl"),
                            emit=lambda k, p: None, extensions=ext_cap, conversation=conv,
                        )
                    finally:
                        t_cap.cancel()

                # The request that follows the actual tool output (call_mcp), carrying the compacted observation.
                req_skill = None
                for rq in captured_s22:
                    sk = rq.observation.state.get("extensions", {}).get("selected_skills")
                    ltr = rq.observation.state.get("last_tool_result") or {}
                    if sk and ltr.get("action_type") == "call_mcp":
                        req_skill = rq
                        break
                assert req_skill is not None
                ltr_skill = req_skill.observation.state.get("last_tool_result") or {}
                assert ltr_skill.get("truncated") is True
                assert req_skill.model_context.goal_text == "capacity compaction preserves skill and constraints"
                assert req_skill.model_context.goal_id == task_cap.goal_id
                assert any(c.required for c in req_skill.model_context.task_criteria.conditions)
                assert "connect_mcp" in req_skill.model_context.capabilities.supported_actions
                assert req_skill.model_context.capabilities.adapter_name
                assert req_skill.observation.state["root_identity"] == canon
                # Real session compaction: the conversation's recent_items were trimmed to fit the observation cap.
                conv_payload = req_skill.observation.state.get("conversation") or {}
                assert conv_payload.get("truncated") is True
                assert conv_payload.get("omitted_count", 0) > 0
                # Precise user constraint (the verbatim constraints string) and auth identity.
                assert conv_payload.get("constraints") == cap_constraints
                assert req_skill.observation.state["root_identity"] == task_cap.root_identity
                sk = req_skill.observation.state["extensions"]["selected_skills"][0]
                assert sk["content"] == "SKILL_KEEP_MARKER_" + ("K" * 12000)
                assert sk["path"] == ".forecontract/skills/long-skill/SKILL.md"
                assert sk["sha256"] == real_sha
                assert sk["source_trust"] == "untrusted_skill"
                assert len(req_skill.observation.model_dump_json().encode("utf-8")) <= 65536

        asyncio.run(_run())


def test_s23_cancellation_identity(tmp_path: Path) -> None:
    import threading
    import time

    from extension_helpers import is_pid_alive
    from forecontract_app.models import AppError

    proj_a = create_buggy_calc_project(tmp_path / "proj_a")
    proj_b = create_buggy_calc_project(tmp_path / "proj_b")
    ready_file = tmp_path / "hang_s23.json"
    log_a = tmp_path / "logs" / "a23.jsonl"
    sc_a = write_stdio_mcp_script(
        tmp_path / "scripts", name="a23.py", log_path=log_a, mode="hang_with_child", ready_file=ready_file
    )
    ext_cfg_path = write_extension_config_file(
        tmp_path / "ext_s23.json",
        servers=[
            {
                "id": "srv-hang",
                "transport": "stdio",
                "executable": sys.executable,
                "argv": ["-B", str(sc_a)],
                "cwd": str(tmp_path),
            }
        ],
    )

    b_calls: list[int] = []
    b_released = threading.Event()

    def _decide(step: int, req: ModelRequest, state: dict[str, Any]) -> str:
        _ = req
        if os.path.basename(str(state.get("root_identity", ""))).lower() == "proj_a":
            if step == 1:
                return ext_forecast(action_type="connect_mcp", payload={"server_id": "srv-hang"})
            if step == 2:
                return ext_forecast(action_type="list_mcp_tools", payload={"server_id": "srv-hang"})
            return ext_forecast(
                action_type="call_mcp",
                payload={"server_id": "srv-hang", "tool_name": "echo_tool", "arguments": {"msg": "hang"}},
            )
        b_calls.append(step)
        if b_released.is_set():
            return "STOP_RUN_B_FAST"
        return ext_forecast(action_type="list_directory", payload={"path": "."})

    async def _run() -> None:
        cfg = make_extension_host_config(
            tmp_path / "host",
            allowed_roots=(proj_a, proj_b),
            extension_config_path=ext_cfg_path,
        )
        svc = AppService(config=cfg, provider_factory=lambda *, project, run_id: CallbackProjectProvider(_decide))
        try:
            pa = await svc.register_project(request=RegisterProjectRequest(name="proj-a", root=str(proj_a), confirmed_read=True))
            pb = await svc.register_project(request=RegisterProjectRequest(name="proj-b", root=str(proj_b), confirmed_read=True))
            sa = svc.create_session(project_id=pa.project_id, model=ModelSelection(provider="offline", model="offline-project-v1"))
            sb = svc.create_session(project_id=pb.project_id, model=ModelSelection(provider="offline", model="offline-project-v1"))

            ra = await svc.start_run(session_id=sa.session_id, request=StartRunRequest(client_request_id=uuid.uuid4(), goal="run A hangs", confirmed_scope=True))
            rec_a = svc._runs_by_id[ra.run_id]

            # Approve connect & call in run A while running
            approved: set[str] = set()
            for _ in range(300):
                if rec_a.extension_runtime is not None:
                    for ep in rec_a.extension_runtime.list_proposals():
                        if ep.state == "pending" and ep.id not in approved:
                            approved.add(ep.id)
                            await svc.decide_extension_proposal(
                                session_id=sa.session_id,
                                run_id=ra.run_id,
                                proposal_id=ep.id,
                                request={"digest": ep.digest, "decision": "approve"},  # type: ignore[arg-type]
                            )
                if len(approved) >= 2:
                    break
                await asyncio.sleep(0.02)

            # Wait deterministically for ready_file to confirm tools/call is actively executing in child
            deadline = time.monotonic() + 5.0
            pids: dict[str, Any] = {}
            while time.monotonic() < deadline:
                if ready_file.exists():
                    try:
                        pids = json.loads(ready_file.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        pids = {}
                    if pids.get("call_started") is True:
                        break
                await asyncio.sleep(0.01)
            assert pids.get("call_started") is True

            # Start B while A is still hanging/ready; B reaches its own provider request barrier and stays active.
            rb = await svc.start_run(
                session_id=sb.session_id,
                request=StartRunRequest(
                    client_request_id=uuid.uuid4(), goal="run B normal", confirmed_scope=True
                ),
            )
            rec_b = svc._runs_by_id[rb.run_id]
            deadline_b = time.monotonic() + 8.0
            while time.monotonic() < deadline_b and not b_calls:
                await asyncio.sleep(0.01)
            assert b_calls, "run B never reached its provider request barrier"
            assert rec_b.view.state == "running"

            # Cancel A; B must continue unaffected and its result attributed correctly.
            await svc.cancel_run(session_id=sa.session_id, run_id=ra.run_id)
            b_released.set()
            await asyncio.wait_for(rec_a.settled_event.wait(), timeout=8.0)
            assert rec_a.view.state == "cancelled"
            assert not is_pid_alive(int(pids["parent_pid"]))
            assert not is_pid_alive(int(pids["child_pid"]))

            await asyncio.wait_for(rec_b.settled_event.wait(), timeout=8.0)
            assert rec_b.view.state in ("failed", "succeeded") and rec_b.view.state != "cancelled"
            assert rec_b.view.run_id == rb.run_id
        finally:
            await svc.aclose()

    asyncio.run(_run())

    # Controlled extension-cleanup failure injected in the test context and driven through the REAL AppService:
    # the run terminal state becomes unknown, verification is not passed, and a same-project new run is refused.
    async def _run_cleanup_failure() -> None:
        from unittest.mock import patch

        cfg_u = make_extension_host_config(
            tmp_path / "host_unk", allowed_roots=(proj_b,), extension_config_path=ext_cfg_path
        )
        svc_u = AppService(config=cfg_u, provider_factory=lambda *, project, run_id: CallbackProjectProvider(_decide))

        async def _failed_aclose(self: ExtensionRuntime) -> ExtensionCleanup:
            return ExtensionCleanup(cleanup_ok=False, remaining_active=1)

        try:
            with patch.object(ExtensionRuntime, "aclose", _failed_aclose):
                pu = await svc_u.register_project(
                    request=RegisterProjectRequest(name="proj-unk", root=str(proj_b), confirmed_read=True)
                )
                su = svc_u.create_session(
                    project_id=pu.project_id,
                    model=ModelSelection(provider="offline", model="offline-project-v1"),
                )
                ru_view = await svc_u.start_run(
                    session_id=su.session_id,
                    request=StartRunRequest(
                        client_request_id=uuid.uuid4(),
                        goal="cleanup failure run",
                        confirmed_scope=True,
                    ),
                )
                rec_u = svc_u._runs_by_id[ru_view.run_id]
                await asyncio.wait_for(rec_u.settled_event.wait(), timeout=8.0)
                assert rec_u.view.state == "unknown"
                assert rec_u.view.verification != "passed"
            # A same-project new run is refused while the workspace remains poisoned/locked.
            with pytest.raises(AppError):
                await svc_u.start_run(
                    session_id=su.session_id,
                    request=StartRunRequest(
                        client_request_id=uuid.uuid4(),
                        goal="blocked follow-up",
                        confirmed_scope=True,
                    ),
                )
        finally:
            await svc_u.aclose()

    asyncio.run(_run_cleanup_failure())


def test_s24_normal_compatibility(tmp_path: Path) -> None:
    proj = create_buggy_calc_project(tmp_path / "proj")
    canon = os.path.normcase(os.path.realpath(proj))

    with LocalHttpMcpServer(label="s24-srv") as http_srv:
        cfg = ExtensionConfig(version=1, servers=[HttpMcpServerConfig(id="srv-24", transport="http", url=http_srv.url)])

        async def _run() -> None:
            (proj / "calc.py").write_text("def add_numbers(a: int, b: int) -> int:\n    return a + b\n", encoding="utf-8")

            # Part A: Adapter-level stale verification after call_mcp then re-verification
            da = tmp_path / "d24a"
            oa = tmp_path / "o24a"
            da.mkdir(parents=True, exist_ok=True)
            oa.mkdir(parents=True, exist_ok=True)
            task_a, _ = build_registered_project_task(
                project_id="p24a", project_name="p24a", project_root=proj, root_identity=canon, read_grant_id="rg24a", data_root=da, output_root=oa, run_id="r24a", goal="adapter stale verify", python_executable=Path(sys.executable), max_steps=12, max_planning_calls=12
            )
            broker_a = PermissionBroker(run_id="r24a", project_root=proj, project_id="p24a", session_id="s24a", root_identity=canon)
            runner_a = ProjectCommandRunner(run_id="r24a", project_root=proj, data_root=da, broker=broker_a, emit=lambda k, p: None)
            ws_a = ProjectWorkspace(root=proj, data_root=da)
            ext_a = ExtensionRuntime(project_id="p24a", session_id="s24a", run_id="r24a", project_root=proj, config=cfg, selected_skill_ids=(), scratch_root=tmp_path / "sc24a", emit=lambda k, p: None)
            ad_a = ProjectCodingAdapter(task=task_a, workspace=ws_a, broker=broker_a, command_runner=runner_a, extensions=ext_a)

            async def _auto_a() -> None:
                for _ in range(500):
                    for bp in broker_a.pending_proposals():
                        broker_a.decide(proposal_id=bp.id, request={"digest": bp.digest, "decision": "approve", "trusted_local_execution": True})
                    for ep in ext_a.list_proposals():
                        if ep.state == "pending":
                            await ext_a.decide(proposal_id=ep.id, digest=ep.digest, decision="approve")
                    await asyncio.sleep(0.01)

            ta_a = asyncio.create_task(_auto_a())
            try:
                await ad_a.execute(
                    ActionSpec(
                        schema_version=PROJECT_SCHEMA_VERSION,
                        action_type="propose_verification",
                        payload={
                            "plan_id": "plan-s24",
                            "summary": "exit ok",
                            "checks": [
                                {"check_id": "c1", "kind": "exit_code", "executable": sys.executable, "argv": ["-B", "-c", "print('ok')"], "cwd": ".", "timeout_ms": 5000}
                            ],
                        },
                        metadata={"action_id": "a24-1"},
                    )
                )
                obs0 = await ad_a.observe()
                plan_digest = obs0.state["plan_digest"]
                await ad_a.execute(
                    ActionSpec(
                        schema_version=PROJECT_SCHEMA_VERSION,
                        action_type="verify_project",
                        payload={"plan_id": "plan-s24", "plan_digest": plan_digest},
                        metadata={"action_id": "a24-2"},
                    )
                )
                obs1 = await ad_a.observe()
                assert obs1.state["verification"]["status"] == "passed"

                await ad_a.execute(ActionSpec(schema_version=PROJECT_SCHEMA_VERSION, action_type="connect_mcp", payload={"server_id": "srv-24"}, metadata={"action_id": "a24-3"}))
                await ad_a.execute(ActionSpec(schema_version=PROJECT_SCHEMA_VERSION, action_type="list_mcp_tools", payload={"server_id": "srv-24"}, metadata={"action_id": "a24-4"}))
                await ad_a.execute(ActionSpec(schema_version=PROJECT_SCHEMA_VERSION, action_type="call_mcp", payload={"server_id": "srv-24", "tool_name": "echo_tool", "arguments": {"msg": "after-verify"}}, metadata={"action_id": "a24-5"}))
                obs2 = await ad_a.observe()
                assert obs2.state["verification"]["status"] == "unknown"
                assert obs2.state["verification"]["stale_reason"] == "extension_call_after_verification"

                await ad_a.execute(
                    ActionSpec(
                        schema_version=PROJECT_SCHEMA_VERSION,
                        action_type="verify_project",
                        payload={"plan_id": "plan-s24", "plan_digest": plan_digest},
                        metadata={"action_id": "a24-6"},
                    )
                )
                obs3 = await ad_a.observe()
                assert obs3.state["verification"]["status"] == "passed"
                assert obs3.state["verification"]["stale_reason"] is None
            finally:
                ta_a.cancel()
                await ext_a.aclose()

            # Part B: Full Core runtime compatibility with call_mcp + final verify_project
            d = tmp_path / "d24"
            o = tmp_path / "o24"
            d.mkdir(parents=True, exist_ok=True)
            o.mkdir(parents=True, exist_ok=True)
            task, pcfg = build_registered_project_task(
                project_id="p24", project_name="p24", project_root=proj, root_identity=canon, read_grant_id="rg24", data_root=d, output_root=o, run_id="r24", goal="verify after call_mcp", python_executable=Path(sys.executable), max_steps=12, max_planning_calls=12
            )
            broker = PermissionBroker(run_id="r24", project_root=proj, project_id="p24", session_id="s24", root_identity=canon)
            ext = ExtensionRuntime(project_id="p24", session_id="s24", run_id="r24", project_root=proj, config=cfg, selected_skill_ids=(), scratch_root=tmp_path / "sc24", emit=lambda k, p: None)

            def _decide(step: int, req: ModelRequest, state: dict[str, Any]) -> str:
                _ = req
                if step == 1:
                    return ext_forecast(action_type="connect_mcp", payload={"server_id": "srv-24"})
                if step == 2:
                    return ext_forecast(action_type="list_mcp_tools", payload={"server_id": "srv-24"})
                if step == 3:
                    return ext_forecast(action_type="call_mcp", payload={"server_id": "srv-24", "tool_name": "echo_tool", "arguments": {"msg": "compat"}})
                if step == 4:
                    return ext_forecast(
                        action_type="propose_verification",
                        payload={
                            "plan_id": "plan-s24b",
                            "summary": "exit ok",
                            "checks": [
                                {"check_id": "c1", "kind": "exit_code", "executable": sys.executable, "argv": ["-B", "-c", "print('ok')"], "cwd": ".", "timeout_ms": 5000}
                            ],
                        },
                    )
                return ext_forecast(action_type="verify_project", payload={"plan_id": state["plan_id"], "plan_digest": state["plan_digest"]}, outcome_type="project_task_verified", outcome_payload={}, task_hint=True)

            async def _auto() -> None:
                for _ in range(500):
                    for bp in broker.pending_proposals():
                        broker.decide(proposal_id=bp.id, request={"digest": bp.digest, "decision": "approve", "trusted_local_execution": True})
                    for ep in ext.list_proposals():
                        if ep.state == "pending":
                            await ext.decide(proposal_id=ep.id, digest=ep.digest, decision="approve")
                    await asyncio.sleep(0.01)

            ta = asyncio.create_task(_auto())
            try:
                res = await run_project_task(
                    task=task,
                    provider=CallbackProjectProvider(_decide),
                    planner_config=pcfg,
                    permissions=broker,
                    trace_writer=JsonlTraceStore(tmp_path / "t24.jsonl"),
                    emit=lambda k, p: None,
                    extensions=ext,
                )
            finally:
                ta.cancel()

            assert res.terminal_status == "succeeded" and res.verification == "passed"

        asyncio.run(_run())
