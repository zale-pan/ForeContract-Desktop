"""B25–B27: HTTP route validation/auth/error mapping, idempotency/alias workspace locks, real CLI offline boundary."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from app_helpers import (
    CREDENTIAL_ENV_CANARY,
    FAKE_TEST_TOKEN,
    auth_headers,
    make_clean_subprocess_env,
    pick_free_loopback_port,
    read_line_with_deadline,
    terminate_process_tree,
)
from forecontract_app.server import create_app
from project_helpers import (
    create_buggy_calc_project,
    create_multifile_project,
    make_project_host_config,
    register_and_start_project_run,
    sha256_file,
)


def test_b25_new_routes_auth_and_error_mapping(tmp_path: Path) -> None:
    allowed_parent = tmp_path / "allowed"
    proj_dir = create_buggy_calc_project(allowed_parent / "http-repo")
    cfg = make_project_host_config(tmp_path, allowed_roots=(proj_dir,))

    async def _scenario() -> None:
        app = create_app(config=cfg)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url=f"http://127.0.0.1:{cfg.bind_port}"
            ) as client:
                # 401/403 boundary checks
                no_auth = await client.post(
                    "/api/v1/projects/register",
                    headers={
                        "Host": f"127.0.0.1:{cfg.bind_port}",
                        "Origin": f"http://127.0.0.1:{cfg.bind_port}",
                    },
                    json={"name": "p1", "root": str(proj_dir), "confirmed_read": True},
                )
                assert no_auth.status_code == 401

                bad_origin = await client.post(
                    "/api/v1/projects/register",
                    headers={
                        "Host": f"127.0.0.1:{cfg.bind_port}",
                        "Authorization": f"Bearer {cfg.access_token}",
                        "Origin": "http://evil.example:9999",
                    },
                    json={"name": "p1", "root": str(proj_dir), "confirmed_read": True},
                )
                assert bad_origin.status_code == 403

                bad_host = await client.get(
                    "/api/v1/projects",
                    headers={"Host": "evil.local:80", "Authorization": f"Bearer {cfg.access_token}"},
                )
                assert bad_host.status_code == 403

                query_tok = await client.get(
                    f"/api/v1/projects?token={cfg.access_token}",
                    headers={
                        "Host": f"127.0.0.1:{cfg.bind_port}",
                        "Authorization": f"Bearer {cfg.access_token}",
                    },
                )
                assert query_tok.status_code in (400, 401, 403)

                headers = auth_headers(cfg)
                bad_bool = await client.post(
                    "/api/v1/projects/register",
                    headers=headers,
                    json={"name": "p1", "root": str(proj_dir), "confirmed_read": "true"},
                )
                assert bad_bool.status_code == 422

                # Parent directory fails exact-root allowlist with 403
                parent_rej = await client.post(
                    "/api/v1/projects/register",
                    headers=headers,
                    json={"name": "parent-no", "root": str(allowed_parent), "confirmed_read": True},
                )
                assert parent_rej.status_code == 403
                assert parent_rej.json()["error"]["code"] == "PROJECT_ROOT_NOT_ALLOWED"

                _, sess, run = await register_and_start_project_run(
                    client,
                    cfg,
                    name="http-repo",
                    root=proj_dir,
                    goal="Fix add_numbers bug",
                )
                sid = sess["session_id"]
                rid = run["run_id"]

                # ProjectError mapping check (NOT 500!) on bad cursor & missing IDs
                bad_after = await client.get(
                    f"/api/v1/sessions/{sid}/runs/{rid}/commands/nope?after=-1",
                    headers=headers,
                )
                assert bad_after.status_code == 422

                missing_cmd = await client.get(
                    f"/api/v1/sessions/{sid}/runs/{rid}/commands/nope?after=0",
                    headers=headers,
                )
                assert missing_cmd.status_code == 404
                assert missing_cmd.json()["error"]["code"] == "NOT_FOUND"

                missing_prop = await client.post(
                    f"/api/v1/sessions/{sid}/runs/{rid}/proposals/nope/decision",
                    headers=headers,
                    json={"digest": "a" * 64, "decision": "approve", "trusted_local_execution": False},
                )
                assert missing_prop.status_code == 404
                assert missing_prop.json()["error"]["code"] == "NOT_FOUND"

                # Drive two-stage approvals: scope -> verification -> command (missing trust -> 403 -> approve)
                saw_scope = False
                saw_verif = False
                saw_cmd = False
                tried_missing_trust = False
                decided: set[str] = set()
                final_run: dict[str, Any] = run
                for _ in range(150):
                    p_resp = await client.get(
                        f"/api/v1/sessions/{sid}/runs/{rid}/proposals", headers=headers
                    )
                    assert p_resp.status_code == 200
                    for prop in p_resp.json():
                        pid = prop["id"]
                        if prop["state"] == "pending" and pid not in decided:
                            if prop["kind"] == "scope":
                                saw_scope = True
                            elif prop["kind"] == "verification":
                                saw_verif = True
                            elif prop["kind"] == "command":
                                saw_cmd = True
                                # Wrong digest MUST map to 409 PROPOSAL_CONFLICT (never 500)
                                bad_dig = await client.post(
                                    f"/api/v1/sessions/{sid}/runs/{rid}/proposals/{pid}/decision",
                                    headers=headers,
                                    json={
                                        "digest": "0" * 64,
                                        "decision": "approve",
                                        "trusted_local_execution": True,
                                    },
                                )
                                assert bad_dig.status_code == 409
                                assert bad_dig.json()["error"]["code"] == "PROPOSAL_DIGEST_MISMATCH"

                                if not tried_missing_trust:
                                    tried_missing_trust = True
                                    no_trust = await client.post(
                                        f"/api/v1/sessions/{sid}/runs/{rid}/proposals/{pid}/decision",
                                        headers=headers,
                                        json={
                                            "digest": prop["digest"],
                                            "decision": "approve",
                                            "trusted_local_execution": False,
                                        },
                                    )
                                    assert no_trust.status_code == 403
                                    assert no_trust.json()["error"]["code"] == "TRUSTED_LOCAL_EXECUTION_REQUIRED"

                            d_resp = await client.post(
                                f"/api/v1/sessions/{sid}/runs/{rid}/proposals/{pid}/decision",
                                headers=headers,
                                json={
                                    "digest": prop["digest"],
                                    "decision": "approve",
                                    "trusted_local_execution": prop["kind"] == "command",
                                },
                            )
                            assert d_resp.status_code == 200, d_resp.text
                            assert d_resp.json()["state"] == "approved"
                            decided.add(pid)

                    r_resp = await client.get(f"/api/v1/sessions/{sid}/runs/{rid}", headers=headers)
                    assert r_resp.status_code == 200
                    final_run = r_resp.json()
                    if final_run["state"] in ("succeeded", "failed", "cancelled", "unknown"):
                        break
                    await asyncio.sleep(0.05)

                assert saw_scope and saw_verif and saw_cmd
                assert final_run["state"] == "succeeded"
                assert final_run["verification"] == "passed"
                assert final_run["result"]["changed_paths"]
                checks_obj = final_run["result"]["checks"]
                assert isinstance(checks_obj, dict)
                assert "junit_1" in checks_obj
                assert checks_obj["junit_1"]["passed"] is True

    asyncio.run(_scenario())


def test_b26_idempotency_workspace_lock_and_cancel_revokes_proposals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = create_buggy_calc_project(tmp_path / "repo")
    cfg = make_project_host_config(tmp_path, allowed_roots=(repo,))

    async def _scenario() -> None:
        app = create_app(config=cfg)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url=f"http://127.0.0.1:{cfg.bind_port}"
            ) as client:
                headers = auth_headers(cfg)
                _proj, sess, run1 = await register_and_start_project_run(
                    client, cfg, name="lock-repo", root=repo, goal="First run waits on proposal"
                )
                sid = sess["session_id"]
                rid1 = run1["run_id"]

                # Second registration pointing to same physical repo -> 409 PROJECT_ROOT_OVERLAP
                alias_reg = await client.post(
                    "/api/v1/projects/register",
                    headers=headers,
                    json={"name": "alias-repo", "root": str(repo), "confirmed_read": True},
                )
                assert alias_reg.status_code == 409

                # Starting another run on the active project -> 409 WORKSPACE_BUSY
                req_id_2 = str(uuid.uuid4())
                busy = await client.post(
                    f"/api/v1/sessions/{sid}/runs",
                    headers=headers,
                    json={"client_request_id": req_id_2, "goal": "Concurrent run", "confirmed_scope": True},
                )
                assert busy.status_code == 409
                assert busy.json()["error"]["code"] == "WORKSPACE_BUSY"

                # Wait until scope proposal is pending, then cancel the run -> proposal revoked
                for _ in range(100):
                    p_resp = await client.get(
                        f"/api/v1/sessions/{sid}/runs/{rid1}/proposals", headers=headers
                    )
                    if p_resp.json():
                        break
                    await asyncio.sleep(0.02)

                c_resp = await client.post(
                    f"/api/v1/sessions/{sid}/runs/{rid1}/cancel",
                    headers=headers,
                    json={},
                )
                assert c_resp.status_code in (200, 202)
                for _ in range(100):
                    r_check = await client.get(f"/api/v1/sessions/{sid}/runs/{rid1}", headers=headers)
                    if r_check.json()["state"] in ("cancelled", "failed", "succeeded", "unknown"):
                        break
                    await asyncio.sleep(0.03)

                p_after = await client.get(
                    f"/api/v1/sessions/{sid}/runs/{rid1}/proposals", headers=headers
                )
                assert any(p["state"] == "revoked" for p in p_after.json())

                # After release, start a new run and check idempotency replay (200) & conflict (409)
                cid = str(uuid.uuid4())
                r2 = await client.post(
                    f"/api/v1/sessions/{sid}/runs",
                    headers=headers,
                    json={"client_request_id": cid, "goal": "Second run", "confirmed_scope": True},
                )
                assert r2.status_code == 202
                r2_replay = await client.post(
                    f"/api/v1/sessions/{sid}/runs",
                    headers=headers,
                    json={"client_request_id": cid, "goal": "Second run", "confirmed_scope": True},
                )
                assert r2_replay.status_code == 200
                assert r2_replay.json()["run_id"] == r2.json()["run_id"]

                r2_conflict = await client.post(
                    f"/api/v1/sessions/{sid}/runs",
                    headers=headers,
                    json={"client_request_id": cid, "goal": "Different goal", "confirmed_scope": True},
                )
                assert r2_conflict.status_code == 409

                # Cancel run2 cleanly before shutdown
                await client.post(
                    f"/api/v1/sessions/{sid}/runs/{r2.json()['run_id']}/cancel",
                    headers=headers,
                    json={},
                )

    asyncio.run(_scenario())

    # Cleanup failure / unknown terminal run MUST keep workspace lock held (409 WORKSPACE_BUSY)
    import forecontract_app.project_commands as pc
    from forecontract_app.project_models import PROJECT_SCHEMA_VERSION
    from forecontract_coding.errors import CodingError
    from forecontract_llm import ModelRequest, ModelResponse

    def _fail_cleanup(**kwargs: Any) -> Any:
        raise CodingError("CODING_PROCESS_CLEANUP_FAILED", "simulated residual process")

    class _CleanupLockProvider:
        def __init__(self) -> None:
            self._step = 0

        async def complete(self, request: ModelRequest) -> ModelResponse:
            self._step += 1
            if self._step == 1:
                body = {
                    "schema_version": PROJECT_SCHEMA_VERSION,
                    "intent": "propose verification",
                    "decision_summary": "propose plan",
                    "proposed_action": {
                        "schema_version": PROJECT_SCHEMA_VERSION,
                        "action_type": "propose_verification",
                        "payload": {
                            "plan_id": "plan-lock",
                            "summary": "diag",
                            "checks": [
                                {
                                    "check_id": "u1",
                                    "kind": "exit_code",
                                    "executable": sys.executable,
                                    "argv": ["-B", "-c", "print('ok')"],
                                    "cwd": ".",
                                    "timeout_ms": 5000,
                                }
                            ],
                        },
                        "metadata": {},
                    },
                    "expected_outcomes": [
                        {
                            "schema_version": PROJECT_SCHEMA_VERSION,
                            "outcome_type": "project_tool_result",
                            "payload": {"action_type": "propose_verification", "require_success": True},
                            "required": True,
                        }
                    ],
                }
            elif self._step == 2:
                body = {
                    "schema_version": PROJECT_SCHEMA_VERSION,
                    "intent": "propose cmd",
                    "decision_summary": "propose cmd",
                    "proposed_action": {
                        "schema_version": PROJECT_SCHEMA_VERSION,
                        "action_type": "propose_command",
                        "payload": {
                            "command_id": "cleanup-fail-http",
                            "executable": sys.executable,
                            "argv": ["-V"],
                            "cwd": ".",
                            "timeout_ms": 5000,
                            "purpose": "diagnostic",
                            "explanation": "trigger cleanup fail",
                        },
                        "metadata": {},
                    },
                    "expected_outcomes": [
                        {
                            "schema_version": PROJECT_SCHEMA_VERSION,
                            "outcome_type": "project_tool_result",
                            "payload": {"action_type": "propose_command", "require_success": True},
                            "required": True,
                        }
                    ],
                }
            elif self._step == 3:
                last = request.observation.state.get("last_tool_result") or {}
                body = {
                    "schema_version": PROJECT_SCHEMA_VERSION,
                    "intent": "start cmd",
                    "decision_summary": "start cmd",
                    "proposed_action": {
                        "schema_version": PROJECT_SCHEMA_VERSION,
                        "action_type": "start_command",
                        "payload": {
                            "proposal_id": str(last.get("proposal_id")),
                            "proposal_digest": str(last.get("digest")),
                        },
                        "metadata": {},
                    },
                    "expected_outcomes": [
                        {
                            "schema_version": PROJECT_SCHEMA_VERSION,
                            "outcome_type": "project_tool_result",
                            "payload": {"action_type": "start_command", "require_success": False},
                            "required": True,
                        }
                    ],
                }
            else:
                return ModelResponse(
                    schema_version="0.1",
                    request_id=request.request_id,
                    status="error",
                    error_code="PROVIDER_ERROR",
                    error_message="stop loop after cleanup failure",
                )
            return ModelResponse(
                schema_version="0.1",
                request_id=request.request_id,
                status="ok",
                content=json.dumps(body),
            )

    monkeypatch.setattr(pc, "run_command_in_job", _fail_cleanup)
    unk_root = create_buggy_calc_project(tmp_path / "repo_unk")
    cfg_unk = make_project_host_config(tmp_path / "unk_host", allowed_roots=(unk_root,))

    async def _unk_lock_scenario() -> None:
        app_unk = create_app(config=cfg_unk, provider_factory=lambda **_: _CleanupLockProvider())
        async with app_unk.router.lifespan_context(app_unk):
            transport = httpx.ASGITransport(app=app_unk)
            async with httpx.AsyncClient(
                transport=transport, base_url=f"http://127.0.0.1:{cfg_unk.bind_port}"
            ) as client:
                hdrs = auth_headers(cfg_unk)
                _p, sess, r_unk = await register_and_start_project_run(
                    client, cfg_unk, name="unk-lock", root=unk_root, goal="Cleanup failure lock test"
                )
                sid_u = sess["session_id"]
                rid_u = r_unk["run_id"]
                decided_u: set[str] = set()
                final_u: dict[str, Any] = {}
                for _ in range(120):
                    pr = await client.get(f"/api/v1/sessions/{sid_u}/runs/{rid_u}/proposals", headers=hdrs)
                    for prop in pr.json():
                        pid = prop["id"]
                        if prop["state"] == "pending" and pid not in decided_u:
                            await client.post(
                                f"/api/v1/sessions/{sid_u}/runs/{rid_u}/proposals/{pid}/decision",
                                headers=hdrs,
                                json={
                                    "digest": prop["digest"],
                                    "decision": "approve",
                                    "trusted_local_execution": prop["kind"] == "command",
                                },
                            )
                            decided_u.add(pid)
                    rr = await client.get(f"/api/v1/sessions/{sid_u}/runs/{rid_u}", headers=hdrs)
                    final_u = rr.json()
                    if final_u["state"] in ("succeeded", "failed", "cancelled", "unknown"):
                        break
                    await asyncio.sleep(0.03)

                assert final_u["state"] == "unknown"
                blocked = await client.post(
                    f"/api/v1/sessions/{sid_u}/runs",
                    headers=hdrs,
                    json={"client_request_id": str(uuid.uuid4()), "goal": "Should stay locked", "confirmed_scope": True},
                )
                assert blocked.status_code == 409
                assert blocked.json()["error"]["code"] == "WORKSPACE_BUSY"

    asyncio.run(_unk_lock_scenario())


def test_b27_cli_project_root_and_offline_boundary(tmp_path: Path) -> None:
    data_root = tmp_path / "cli_data"
    ws_parent = tmp_path / "cli_workspaces"
    allowed_parent = tmp_path / "cli_allowed"
    proj_dir = create_multifile_project(allowed_parent / "cli-proj", include_large_notes=False, doc_count=0)
    unallowed = create_buggy_calc_project(tmp_path / "cli_unallowed")
    port = pick_free_loopback_port()

    env = make_clean_subprocess_env(
        {
            "DEEPSEEK_API_KEY": CREDENTIAL_ENV_CANARY,
            "OPENAI_API_KEY": CREDENTIAL_ENV_CANARY,
        }
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-B",
            "-m",
            "forecontract_app",
            "--data-root",
            str(data_root),
            "--workspace-parent",
            str(ws_parent),
            "--project-root",
            str(proj_dir),
            "--port",
            str(port),
            "--offline",
            "--control-stdin",
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env=env,
    )
    try:
        assert proc.stdout is not None and proc.stdin is not None
        line = read_line_with_deadline(proc.stdout, timeout=15.0)
        assert line.startswith(f"FORECONTRACT_APP_URL=http://127.0.0.1:{port}/#token=")
        token = line.split("#token=", 1)[1].strip()
        assert len(token) >= 32 and token != FAKE_TEST_TOKEN

        hdrs = {
            "Host": f"127.0.0.1:{port}",
            "Authorization": f"Bearer {token}",
            "Origin": f"http://127.0.0.1:{port}",
        }
        base = f"http://127.0.0.1:{port}"

        # Parent dir & unrelated dir both denied 403
        for bad_dir, nm in ((allowed_parent, "parent-bad"), (unallowed, "unallowed-bad")):
            r_denied = httpx.post(
                f"{base}/api/v1/projects/register",
                headers=hdrs,
                json={"name": nm, "root": str(bad_dir), "confirmed_read": True},
                timeout=5.0,
            )
            assert r_denied.status_code == 403

        # /docs blocked & CSP header present on /
        assert httpx.get(f"{base}/docs", headers={"Host": f"127.0.0.1:{port}"}, timeout=5.0).status_code == 404
        idx = httpx.get(f"{base}/", headers={"Host": f"127.0.0.1:{port}"}, timeout=5.0)
        assert idx.status_code == 200
        assert "frame-ancestors 'none'" in idx.headers.get("content-security-policy", "")

        r_ok = httpx.post(
            f"{base}/api/v1/projects/register",
            headers=hdrs,
            json={"name": "cli-proj", "root": str(proj_dir), "confirmed_read": True},
            timeout=5.0,
        )
        assert r_ok.status_code == 201
        assert r_ok.json()["read_grant_id"]
        pid = r_ok.json()["project_id"]

        r_sess = httpx.post(
            f"{base}/api/v1/sessions",
            headers=hdrs,
            json={
                "project_id": pid,
                "model": {"provider": "deepseek", "model": "deepseek-flash", "thinking": "disabled"},
            },
            timeout=5.0,
        )
        assert r_sess.status_code == 201
        r_live = httpx.post(
            f"{base}/api/v1/sessions/{r_sess.json()['session_id']}/runs",
            headers=hdrs,
            json={"client_request_id": str(uuid.uuid4()), "goal": "try live", "confirmed_scope": True},
            timeout=5.0,
        )
        assert r_live.status_code == 409 and r_live.json()["error"]["code"] == "LIVE_DISABLED"

        proc.stdin.write("stop\n")
        proc.stdin.flush()
        rc = proc.wait(timeout=10.0)
        assert rc == 0
        assert "return a - b" in (proj_dir / "src" / "mathops.py").read_text(encoding="utf-8")
        assert sha256_file(proj_dir / "src" / "mathops.py")

        # Old --allow-project-root and unknown --live flags must exit non-zero
        for bad_flag in ("--allow-project-root", "--live"):
            extra = [str(proj_dir)] if bad_flag == "--allow-project-root" else []
            bad_cli = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "forecontract_app",
                    "--data-root",
                    str(data_root),
                    "--workspace-parent",
                    str(ws_parent),
                    "--port",
                    str(pick_free_loopback_port()),
                    bad_flag,
                    *extra,
                ],
                capture_output=True,
                text=True,
                env=make_clean_subprocess_env(),
                check=False,
            )
            assert bad_cli.returncode != 0
    finally:
        terminate_process_tree(proc)
