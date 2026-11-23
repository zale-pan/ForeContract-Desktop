"""Controlled metrics example project builder and bounded offline ModelProvider."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from forecontract_coding import (
    CodingConfig,
    CodingTask,
    CommandSpec,
    ExpectedTestCase,
    WriteTarget,
)
from forecontract_llm import ModelRequest, ModelResponse, PlannerConfig

from forecontract_app.models import (
    AppError,
    CommandSummaryView,
    ExpectedTestView,
    ProjectView,
    WriteTargetView,
)
from forecontract_app.security import (
    check_no_reparse_ancestors,
    ensure_clean_directory,
    is_reparse_or_symlink,
)

INITIAL_METRICS_SOURCE: Final[str] = (
    '"""Controlled local metrics example module (APP-T01 offline example)."""\n\n\n'
    "def weighted_positive_total(pairs, min_weight=2):\n"
    '    """Return sum(w * v for w, v in pairs if w >= min_weight and v > 0)."""\n'
    '    raise NotImplementedError("TODO: implement weighted_positive_total")\n'
)

FIXED_METRICS_SOURCE: Final[str] = (
    '"""Controlled local metrics example module (APP-T01 offline example)."""\n\n\n'
    "def weighted_positive_total(pairs, min_weight=2):\n"
    '    """Return sum(w * v for w, v in pairs if w >= min_weight and v > 0)."""\n'
    "    return sum(w * v for w, v in pairs if w >= min_weight and v > 0)\n"
)

METRICS_ACCEPTANCE_TEST: Final[str] = (
    "from metrics import weighted_positive_total\n\n\n"
    "def test_weighted_basic():\n"
    "    assert weighted_positive_total([(1, 10), (2, 5), (3, -4)]) == 10\n\n\n"
    "def test_weighted_threshold_and_empty():\n"
    "    assert weighted_positive_total([], min_weight=3) == 0\n"
    "    assert weighted_positive_total([(4, 7), (2, 7)], min_weight=3) == 28\n"
)

INITIAL_METRICS_SHA256: Final[str] = hashlib.sha256(
    INITIAL_METRICS_SOURCE.encode("utf-8")
).hexdigest()
METRICS_ACCEPTANCE_SHA256: Final[str] = hashlib.sha256(
    METRICS_ACCEPTANCE_TEST.encode("utf-8")
).hexdigest()

OFFLINE_CAPABILITY_NOTICE: Final[str] = (
    "离线示例替身（offline-metrics-v1）：仅演示受控 metrics.py 示例修复与固定验收闭环，"
    "不代表通用仓库开发或真实模型推理能力。"
)


@dataclass(frozen=True, slots=True)
class ControlledProjectSpec:
    """Host-owned immutable specification for a controlled metrics example project."""

    view: ProjectView
    workspace_dir: Path
    acceptance_file: Path
    acceptance_sha256: str
    initial_source_sha256: str


def initialize_controlled_project(
    *,
    project_id: str,
    name: str,
    data_root: Path,
    workspace_parent: Path,
) -> ControlledProjectSpec:
    """Create a fresh create-only controlled metrics project workspace and acceptance file."""
    ws_dir = workspace_parent / name
    if os.path.lexists(ws_dir):
        raise AppError(
            "PROJECT_EXISTS",
            "project directory already exists and cannot be overwritten or adopted",
            status_code=409,
        )
    check_no_reparse_ancestors(ws_dir, label="workspace_root")

    acc_dir = data_root / "projects" / project_id / "acceptance"
    ensure_clean_directory(acc_dir, label="acceptance_dir")
    acc_file = acc_dir / "test_metrics_acceptance.py"
    if os.path.lexists(acc_file):
        raise AppError(
            "PROJECT_EXISTS",
            "acceptance file already exists for project",
            status_code=409,
        )

    try:
        ws_dir.mkdir(exist_ok=False)
        if is_reparse_or_symlink(ws_dir):
            raise AppError(
                "REPARSE_ROOT_FORBIDDEN",
                "created project directory cannot be a reparse point",
                status_code=400,
            )
        metrics_path = ws_dir / "metrics.py"
        metrics_path.write_bytes(INITIAL_METRICS_SOURCE.encode("utf-8"))
        acc_file.write_bytes(METRICS_ACCEPTANCE_TEST.encode("utf-8"))
    except AppError:
        raise
    except OSError as exc:
        # Leave already-written files untouched on partial initialization failure; do not register.
        raise AppError(
            "PROJECT_INIT_FAILED",
            "failed to initialize controlled example project files",
            status_code=500,
        ) from exc

    expected = [
        ExpectedTestView(classname="test_metrics_acceptance", name="test_weighted_basic"),
        ExpectedTestView(
            classname="test_metrics_acceptance", name="test_weighted_threshold_and_empty"
        ),
    ]
    view = ProjectView(
        project_id=project_id,
        name=name,
        workspace_root=str(ws_dir),
        read_paths=["metrics.py"],
        write_paths=[WriteTargetView(path="metrics.py", operation="modify")],
        commands_summary=[
            CommandSummaryView(
                command_id="pytest_acc",
                purpose="acceptance",
                timeout_ms=15000,
                expected_tests=expected,
            )
        ],
        expected_tests=expected,
    )
    return ControlledProjectSpec(
        view=view,
        workspace_dir=ws_dir,
        acceptance_file=acc_file,
        acceptance_sha256=METRICS_ACCEPTANCE_SHA256,
        initial_source_sha256=INITIAL_METRICS_SHA256,
    )


def build_controlled_coding_task(
    *,
    project: ControlledProjectSpec,
    run_id: str,
    goal: str,
    output_root: Path,
    python_executable: Path,
) -> tuple[CodingTask, CodingConfig, PlannerConfig]:
    """Construct strict ``CodingTask``, ``CodingConfig``, and ``PlannerConfig`` for a run."""
    if not project.acceptance_file.is_file() or is_reparse_or_symlink(project.acceptance_file):
        raise AppError(
            "ACCEPTANCE_MISSING",
            "controlled acceptance test file is missing or unsafe",
            status_code=500,
        )
    actual_acc_sha = hashlib.sha256(project.acceptance_file.read_bytes()).hexdigest()
    if actual_acc_sha != project.acceptance_sha256:
        raise AppError(
            "ACCEPTANCE_TAMPERED",
            "controlled acceptance test source digest mismatch",
            status_code=409,
        )

    ws_dir = project.workspace_dir
    acc_file = project.acceptance_file
    task = CodingTask(
        schema_version="0.1",
        run_id=run_id,
        goal_id=f"metrics-{project.view.name}",
        goal_text=goal,
        workspace_root=str(ws_dir),
        read_paths=["metrics.py"],
        write_paths=[WriteTarget(path="metrics.py", operation="modify")],
        commands={
            "pytest_acc": CommandSpec(
                command_id="pytest_acc",
                executable=str(python_executable),
                args=[
                    "-B",
                    "-m",
                    "pytest",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                    "--import-mode=importlib",
                    f"--rootdir={acc_file.parent}",
                    f"--confcutdir={acc_file.parent}",
                    "--basetemp={result_dir}/pytest-temp",
                    "--junitxml={result_dir}/junit.xml",
                    str(acc_file),
                ],
                cwd=str(ws_dir),
                env={
                    "PYTHONPATH": str(ws_dir),
                    "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
                },
                timeout_ms=15000,
                purpose="acceptance",
                expected_tests=[
                    ExpectedTestCase(
                        classname="test_metrics_acceptance",
                        name="test_weighted_basic",
                    ),
                    ExpectedTestCase(
                        classname="test_metrics_acceptance",
                        name="test_weighted_threshold_and_empty",
                    ),
                ],
            )
        },
        verification_command_ids=["pytest_acc"],
        max_steps=12,
        max_planning_calls=12,
        max_replans=4,
        run_timeout_ms=60000,
    )
    config = CodingConfig(
        schema_version="0.1",
        output_root=str(output_root),
        process_cleanup_ms=2000,
    )
    planner_config = PlannerConfig(
        schema_version="0.1",
        call_timeout_ms=15000,
        cleanup_timeout_ms=500,
        max_calls=12,
    )
    return task, config, planner_config


class OfflineMetricsProvider:
    """Deterministic observation-driven offline ModelProvider for controlled metrics demo."""

    def __init__(self) -> None:
        self._wrote = False
        self._verified = False

    def _build_proposal(self, request: ModelRequest) -> dict[str, Any]:
        state = request.observation.state
        last = state.get("last_tool_result")
        files = {entry["path"]: entry for entry in state.get("files", [])}
        metrics_info = files.get("metrics.py") or {}
        metrics_sha = metrics_info.get("sha256")

        if last is None:
            return {
                "schema_version": "0.1",
                "intent": "inspect controlled metrics.py source",
                "decision_summary": f"{OFFLINE_CAPABILITY_NOTICE} 读取 metrics.py",
                "proposed_action": {
                    "schema_version": "0.1",
                    "action_type": "read_file",
                    "payload": {"path": "metrics.py", "start_line": 1, "max_lines": 50},
                    "metadata": {},
                },
                "expected_outcomes": [
                    {
                        "schema_version": "0.1",
                        "outcome_type": "coding_tool_result",
                        "payload": {"action_type": "read_file", "path": "metrics.py"},
                        "required": True,
                    }
                ],
            }

        if not self._wrote and last.get("action_type") == "read_file":
            self._wrote = True
            return {
                "schema_version": "0.1",
                "intent": "implement weighted_positive_total in controlled metrics.py",
                "decision_summary": f"{OFFLINE_CAPABILITY_NOTICE} 写入示例修复并绑定观测 SHA",
                "proposed_action": {
                    "schema_version": "0.1",
                    "action_type": "write_file",
                    "payload": {
                        "path": "metrics.py",
                        "expected_sha256": metrics_sha,
                        "content": FIXED_METRICS_SOURCE,
                    },
                    "metadata": {},
                },
                "expected_outcomes": [
                    {
                        "schema_version": "0.1",
                        "outcome_type": "coding_file_written",
                        "payload": {"path": "metrics.py"},
                        "required": True,
                    }
                ],
            }

        self._verified = True
        return {
            "schema_version": "0.1",
            "intent": "run host acceptance tests via verify_task",
            "decision_summary": f"{OFFLINE_CAPABILITY_NOTICE} 触发固定验收",
            "proposed_action": {
                "schema_version": "0.1",
                "action_type": "verify_task",
                "payload": {},
                "metadata": {},
            },
            "expected_outcomes": [
                {
                    "schema_version": "0.1",
                    "outcome_type": "coding_task_verified",
                    "payload": {},
                    "required": True,
                }
            ],
        }

    async def complete(self, request: ModelRequest) -> ModelResponse:
        body = self._build_proposal(request)
        return ModelResponse(
            schema_version="0.1",
            request_id=request.request_id,
            status="ok",
            content=json.dumps(body, ensure_ascii=False),
        )
