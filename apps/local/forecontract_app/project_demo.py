"""Deterministic offline example provider and task builder for registered local projects."""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from forecontract_llm import ModelRequest, ModelResponse, PlannerConfig

from forecontract_app.project_models import (
    PROJECT_PROPOSAL_TTL_SECONDS,
    PROJECT_SCHEMA_VERSION,
    ProjectTask,
)

_TEST_DEF_RE = re.compile(r"^def\s+(test_[A-Za-z0-9_]+)\s*\(", re.MULTILINE)


def build_registered_project_task(
    *,
    project_id: str,
    project_name: str,
    project_root: Path | None = None,
    root_identity: str | None = None,
    read_grant_id: str = "",
    data_root: Path,
    output_root: Path,
    run_id: str,
    goal: str,
    python_executable: Path,
    session_id: str = "default-session",
    max_steps: int = 120,
    max_planning_calls: int = 64,
    max_replans: int = 32,
    run_timeout_ms: int = 600_000,
    proposal_ttl_seconds: float = PROJECT_PROPOSAL_TTL_SECONDS,
) -> tuple[ProjectTask, PlannerConfig]:
    """Build strict ``ProjectTask`` and ``PlannerConfig`` for an existing registered project."""
    if root_identity:
        resolved_identity = root_identity
    elif project_root is not None:
        resolved_identity = os.path.normcase(os.path.realpath(project_root))
    else:
        raise ValueError("either root_identity or project_root is required")
    del project_name
    task = ProjectTask(
        run_id=run_id,
        goal_id=f"project-{project_id}-{uuid.uuid4().hex[:8]}",
        goal_text=goal,
        project_id=project_id,
        root_identity=resolved_identity,
        read_grant_id=read_grant_id or str(uuid.uuid4()),
        data_root=data_root,
        output_root=output_root,
        python_executable=python_executable,
        session_id=session_id,
        max_steps=max_steps,
        max_planning_calls=max_planning_calls,
        max_replans=max_replans,
        run_timeout_ms=run_timeout_ms,
        proposal_ttl_seconds=proposal_ttl_seconds,
    )
    planner_cfg = PlannerConfig(
        schema_version="0.1",
        call_timeout_ms=15000,
        cleanup_timeout_ms=500,
        max_calls=max_planning_calls,
    )
    return task, planner_cfg


def _infer_offline_edit(content: str) -> tuple[str, str] | None:
    replacements = (
        ("return a - b", "return a + b"),
        ("return x - y", "return x + y"),
        ("return a + b  # BUG_MUL", "return a * b"),
        ('# TODO: fix', '# Fixed'),
        ('return f"{name}: ERROR"', 'return f"{name}: OK"'),
        ('return f"{label}: ERROR"', 'return f"{label}: OK"'),
        ('STATUS = "BROKEN"', 'STATUS = "OK"'),
    )
    for old_text, new_text in replacements:
        if content.count(old_text) == 1:
            return old_text, new_text
    return None


class OfflineProjectProvider:
    """Observation-driven offline ModelProvider for the 13-action ProjectCodingAdapter protocol."""

    def __init__(self) -> None:
        self._listed = False
        self._root_names: set[str] = set()
        self._found = False
        self._find_cursor: str | None = None
        self._discovered_py: list[str] = []
        self._read_queue: list[str] = []
        self._read_queue_built = False
        self._files: dict[str, tuple[str, str]] = {}
        self._partial_chunks: dict[str, list[str]] = {}
        self._pending_read: dict[str, Any] | None = None
        self._analyzed = False
        self._planned_creates: list[dict[str, str]] = []
        self._planned_edits: list[dict[str, str]] = []
        self._protected_paths: list[str] = []
        self._plan_checks: list[dict[str, Any]] = []
        self._scope_requested = False
        self._scope_approved = False
        self._plan_requested = False
        self._plan_approved = False
        self._created_done: set[str] = set()
        self._edited_done: set[str] = set()
        self._verified = False
        self._aborted = False

    def _forecast_json(
        self,
        *,
        intent: str,
        summary: str,
        action_type: str,
        payload: dict[str, Any],
        outcome_type: str = "project_tool_result",
        outcome_payload: dict[str, Any] | None = None,
        task_hint: bool = False,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "schema_version": PROJECT_SCHEMA_VERSION,
            "intent": intent,
            "decision_summary": summary,
            "proposed_action": {
                "schema_version": PROJECT_SCHEMA_VERSION,
                "action_type": action_type,
                "payload": payload,
                "metadata": {},
            },
            "expected_outcomes": [
                {
                    "schema_version": PROJECT_SCHEMA_VERSION,
                    "outcome_type": outcome_type,
                    "payload": outcome_payload
                    if outcome_payload is not None
                    else {"action_type": action_type, "require_success": True},
                    "required": True,
                }
            ],
        }
        if task_hint:
            body["task_completion_hint"] = True
        return body

    def _ingest_last_result(self, state: dict[str, Any]) -> None:
        last = state.get("last_tool_result")
        if not isinstance(last, dict):
            return
        at = last.get("action_type")
        ok = bool(last.get("tool_succeeded"))

        if at == "list_directory" and ok:
            for ent in last.get("entries", []):
                if isinstance(ent, dict) and isinstance(ent.get("path"), str):
                    self._root_names.add(ent["path"])

        elif at == "find_paths" and ok:
            for p in last.get("paths", []):
                if isinstance(p, str) and p not in self._discovered_py:
                    self._discovered_py.append(p)
            nxt = last.get("next_cursor")
            if last.get("truncated") and isinstance(nxt, str) and nxt:
                self._find_cursor = nxt
                self._found = False
            else:
                self._find_cursor = None
                self._found = True

        elif at == "read_file":
            if ok:
                path = str(last.get("path", ""))
                sha = str(last.get("sha256") or last.get("file_sha256") or "")
                chunk = str(last.get("content", ""))
                self._partial_chunks.setdefault(path, []).append(chunk)
                if last.get("truncated") and not last.get("eof"):
                    if last.get("next_byte_offset") is not None:
                        self._pending_read = {
                            "path": path,
                            "byte_offset": int(last["next_byte_offset"]),
                            "expected_sha256": sha,
                        }
                    elif last.get("next_start_line") is not None:
                        self._pending_read = {
                            "path": path,
                            "start_line": int(last["next_start_line"]),
                            "max_lines": 200,
                            "expected_sha256": sha,
                        }
                else:
                    self._pending_read = None
                    full_text = "".join(self._partial_chunks.pop(path, [chunk]))
                    self._files[path] = (sha, full_text)

        elif at == "propose_scope":
            if ok and last.get("state") == "approved":
                self._scope_approved = True
            else:
                self._aborted = True

        elif at == "propose_verification":
            if ok and last.get("state") == "approved":
                self._plan_approved = True
            else:
                self._aborted = True

        elif at == "create_file":
            if ok:
                self._created_done.add(str(last.get("path", "")))
            else:
                self._aborted = True

        elif at == "edit_file":
            if ok:
                self._edited_done.add(str(last.get("path", "")))
            else:
                self._aborted = True

        elif at == "verify_project" and ok and last.get("verification_status") == "passed":
            self._verified = True

    def _analyze_repo(self, python_exe: str) -> None:
        self._analyzed = True
        creates: list[dict[str, str]] = []
        edits: list[dict[str, str]] = []
        test_files: list[str] = []
        has_build = "check_build.py" in self._files
        protected: set[str] = set()
        if "AGENTS.md" in self._files or "AGENTS.md" in self._root_names:
            protected.add("AGENTS.md")
        if has_build:
            protected.add("check_build.py")

        need_constants_py = False
        for path, (sha, content) in sorted(self._files.items()):
            norm = path.replace("\\", "/")
            if norm.startswith(("tests/", "test_")) or "/test_" in norm:
                if norm.endswith(".py"):
                    test_files.append(norm)
                    protected.add(norm)
                continue
            if norm == "check_build.py" or not norm.endswith(".py"):
                continue

            if "from src.constants import LABEL_OK" in content and "src/constants.py" not in self._files:
                need_constants_py = True

            inferred = _infer_offline_edit(content)
            if inferred is not None:
                old_text, new_text = inferred
                edits.append(
                    {
                        "path": norm,
                        "expected_sha256": sha,
                        "old_text": old_text,
                        "new_text": new_text,
                    }
                )

        if need_constants_py:
            creates.append(
                {
                    "path": "src/constants.py",
                    "content": '"""Shared formatting constants."""\n\nLABEL_OK = "OK"\n',
                }
            )

        checks: list[dict[str, Any]] = []
        for idx, tpath in enumerate(sorted(test_files), start=1):
            _, tcontent = self._files.get(tpath, ("", ""))
            test_funcs = _TEST_DEF_RE.findall(tcontent)
            classname = tpath.removesuffix(".py").replace("/", ".")
            if test_funcs:
                checks.append(
                    {
                        "check_id": f"junit_{idx}",
                        "kind": "junit_exact",
                        "executable": python_exe,
                        "argv": [
                            "-B",
                            "-m",
                            "pytest",
                            "-p",
                            "no:cacheprovider",
                            "--junitxml={result_dir}/junit.xml",
                            tpath,
                        ],
                        "cwd": ".",
                        "timeout_ms": 30000,
                        "protected_paths": [tpath],
                        "expected_tests": [
                            {"classname": classname, "name": fn} for fn in test_funcs
                        ],
                    }
                )
            else:
                checks.append(
                    {
                        "check_id": f"test_exit_{idx}",
                        "kind": "exit_code",
                        "executable": python_exe,
                        "argv": ["-B", "-m", "pytest", "-p", "no:cacheprovider", tpath],
                        "cwd": ".",
                        "timeout_ms": 30000,
                        "protected_paths": [tpath],
                        "expected_tests": [],
                    }
                )

        if has_build:
            checks.append(
                {
                    "check_id": "build_check",
                    "kind": "exit_code",
                    "executable": python_exe,
                    "argv": ["-B", "check_build.py"],
                    "cwd": ".",
                    "timeout_ms": 15000,
                    "protected_paths": ["check_build.py"],
                    "expected_tests": [],
                }
            )

        if not checks:
            checks.append(
                {
                    "check_id": "smoke_compile",
                    "kind": "exit_code",
                    "executable": python_exe,
                    "argv": ["-B", "-c", "import sys; sys.exit(0)"],
                    "cwd": ".",
                    "timeout_ms": 10000,
                    "protected_paths": [],
                    "expected_tests": [],
                }
            )

        self._planned_creates = creates
        self._planned_edits = edits
        self._protected_paths = sorted(protected)
        self._plan_checks = checks

    def _next_step(self, request: ModelRequest) -> dict[str, Any] | None:
        state = request.observation.state
        self._ingest_last_result(state)

        if self._aborted:
            return self._forecast_json(
                intent="abort after rejected or failed write/proposal",
                summary="拒绝或失败后中止，受前置条件约束不得写入未授权文件",
                action_type="create_file",
                payload={"path": ".forecontract_aborted_unapproved__", "content": ""},
                outcome_type="project_file_written",
                outcome_payload={"path": ".forecontract_aborted_unapproved__"},
            )

        if not self._listed:
            self._listed = True
            return self._forecast_json(
                intent="list project root",
                summary="只读探索项目根目录",
                action_type="list_directory",
                payload={"path": ".", "limit": 100},
            )

        if not self._found:
            return self._forecast_json(
                intent="find python files",
                summary="分页检索项目内的 Python 源码与测试文件",
                action_type="find_paths",
                payload={"pattern": "*.py", "cursor": self._find_cursor, "limit": 100},
            )

        if self._pending_read is not None:
            pending = dict(self._pending_read)
            return self._forecast_json(
                intent=f"continue reading {pending['path']}",
                summary=f"继续分页读取 {pending['path']}",
                action_type="read_file",
                payload=pending,
            )

        if not self._read_queue_built:
            self._read_queue_built = True
            src_files = [
                p
                for p in self._discovered_py
                if not p.startswith("tests/") and "/test_" not in p and not p.startswith("test_")
            ]
            test_files = [p for p in self._discovered_py if p not in src_files]
            ordered: list[str] = []
            if "AGENTS.md" in self._root_names:
                ordered.append("AGENTS.md")
            ordered.extend(sorted(src_files))
            ordered.extend(sorted(test_files))
            self._read_queue = ordered

        while self._read_queue:
            nxt_path = self._read_queue.pop(0)
            if nxt_path not in self._files:
                return self._forecast_json(
                    intent=f"read {nxt_path}",
                    summary=f"读取 {nxt_path} 并记录 SHA-256",
                    action_type="read_file",
                    payload={"path": nxt_path, "start_line": 1, "max_lines": 200},
                )

        if not self._analyzed:
            python_exe = str((state.get("runtime_capabilities") or {}).get("python_executable", ""))
            self._analyze_repo(python_exe)

        if (self._planned_creates or self._planned_edits) and not self._scope_approved and not self._scope_requested:
            self._scope_requested = True
            exact_paths = sorted(
                {c["path"] for c in self._planned_creates}
                | {e["path"] for e in self._planned_edits}
            )
            ops: list[str] = []
            if self._planned_creates:
                ops.append("create")
            if self._planned_edits:
                ops.append("modify")
            return self._forecast_json(
                intent="propose explicit write scope and protected paths",
                summary="申请精确写权限与受保护验证文件边界",
                action_type="propose_scope",
                payload={
                    "exact_paths": exact_paths,
                    "write_prefixes": [],
                    "operations": ops,
                    "protected_paths": list(self._protected_paths),
                    "verification_plan_summary": f"Run {len(self._plan_checks)} verification checks",
                },
                outcome_payload={"action_type": "propose_scope", "require_success": False},
            )

        if not self._plan_approved and not self._plan_requested:
            self._plan_requested = True
            return self._forecast_json(
                intent="propose concrete verification plan",
                summary="提交包含检查列表与预期测试的验证计划",
                action_type="propose_verification",
                payload={
                    "plan_id": "plan-1",
                    "summary": "Verify all planned code edits with tests and build script",
                    "checks": self._plan_checks,
                },
                outcome_payload={"action_type": "propose_verification", "require_success": False},
            )

        for item in self._planned_creates:
            if item["path"] not in self._created_done:
                return self._forecast_json(
                    intent=f"create {item['path']}",
                    summary=f"原子创建新文件 {item['path']}",
                    action_type="create_file",
                    payload={"path": item["path"], "content": item["content"]},
                    outcome_type="project_file_written",
                    outcome_payload={"path": item["path"]},
                )

        for item in self._planned_edits:
            if item["path"] not in self._edited_done:
                return self._forecast_json(
                    intent=f"edit {item['path']}",
                    summary=f"原子修改 {item['path']}",
                    action_type="edit_file",
                    payload={
                        "path": item["path"],
                        "expected_sha256": item["expected_sha256"],
                        "old_text": item["old_text"],
                        "new_text": item["new_text"],
                    },
                    outcome_type="project_file_written",
                    outcome_payload={"path": item["path"]},
                )

        plan_id = str(state.get("plan_id") or "plan-1")
        plan_digest = str(state.get("plan_digest") or "")
        return self._forecast_json(
            intent="verify project against approved plan",
            summary="执行经批准的验证计划并校验 JUnit XML 与修订一致性",
            action_type="verify_project",
            payload={"plan_id": plan_id, "plan_digest": plan_digest},
            outcome_type="project_task_verified",
            outcome_payload={},
            task_hint=True,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        body = self._next_step(request)
        if body is None:
            return ModelResponse(
                schema_version="0.1",
                request_id=request.request_id,
                status="error",
                content="Proposal denied or action aborted; stopping without unauthorized writes.",
            )
        return ModelResponse(
            schema_version="0.1",
            request_id=request.request_id,
            status="ok",
            content=json.dumps(body, ensure_ascii=False),
        )
