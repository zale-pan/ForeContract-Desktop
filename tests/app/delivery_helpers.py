"""APP-T06 delivery fixtures: portable source copy, inventory project, and joint Skill/MCP provider.

These are test-only fixtures.  Nothing in the product startup path may import
``tests.app``; these helpers live exclusively under the test root and reuse the
existing ``app_helpers`` / ``extension_helpers`` building blocks.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path
from typing import Any

from extension_helpers import ext_forecast, make_skill
from forecontract_llm import ModelProvider, ModelRequest, ModelResponse
from forecontract_llm.models import ModelUsage

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Relative-to-repo roots that together form the portable source deliverable.
# ``copy_delivery_sources`` preserves this repo-relative layout under the target.
_APP_PACKAGE_DIR = "apps/local/forecontract_app"
_PACKAGE_SRC_DIRS = (
    "packages/core/src/forecontract",
    "packages/planners/llm/src/forecontract_llm",
    "packages/adapters/coding/src/forecontract_coding",
)
_DELIVERY_DOCS = (
    "apps/local/README.zh-CN.md",
    "docs/LOCAL_DELIVERY.zh-CN.md",
)
_REQUIREMENTS = "apps/local/requirements.txt"
_STATIC_FILES = (
    "apps/local/forecontract_app/static/index.html",
    "apps/local/forecontract_app/static/app.js",
    "apps/local/forecontract_app/static/style.css",
)

_MARKER_RE = re.compile(r"MARKER::(\S+?)::END")

NORMALIZE_INITIAL = (
    '"""Inventory name normalization."""\n\n\n'
    "def normalize_name(name: str) -> str:\n"
    '    """Normalize a display name to a lowercase snake identifier."""\n'
    '    raise NotImplementedError("TODO: implement normalize_name")\n'
)

RENDER_INITIAL = (
    '"""Inventory status rendering."""\n\n\n'
    "def render_status(name: str) -> str:\n"
    '    """Render a status label for a named item."""\n'
    '    return f"{name}: ERROR"\n'
)

INVENTORY_TEST = (
    "from src.normalize import normalize_name\n"
    "from src.render import render_status\n\n\n"
    "def test_normalize() -> None:\n"
    '    assert normalize_name("  Hello World ") == "hello_world"\n'
    '    assert normalize_name("Foo-Bar") == "foo_bar"\n\n\n'
    "def test_render() -> None:\n"
    '    assert render_status("build") == "build: OK"\n'
)

INVENTORY_AGENTS = (
    "# Inventory Project Rules\n\n"
    "- Only modify `src/` modules when fixing behavior.\n"
    "- Do not edit `tests/`, `AGENTS.md`, or `USER_NOTES.md`.\n"
    "- Include the delivered marker in any modified source file.\n"
)

INVENTORY_USER_NOTES = "# User Notes\n\nSmall inventory project used for offline joint acceptance.\n"

INVENTORY_SKILL = (
    "# Inventory Fix Skill\n\n"
    "Fix `src/normalize.py` and `src/render.py`:\n"
    "- `normalize_name`: strip, lowercase, replace runs of non-alphanumerics with `_`.\n"
    "- `render_status`: return `f\"{{name}}: OK\"`.\n"
    "- Include this marker in a comment in both files: MARKER::{marker}::END\n"
)

NORMALIZE_OLD_TEXT = 'raise NotImplementedError("TODO: implement normalize_name")'
RENDER_OLD_TEXT = 'return f"{name}: ERROR"'

NORMALIZE_NEW_TEMPLATE = (
    "# MARKER::{marker}::END\n"
    '    return name.strip().lower().replace(" ", "_").replace("-", "_")'
)
RENDER_NEW_TEMPLATE = '# MARKER::{marker}::END\n    return f"{{name}}: OK"'


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _is_reparse(path: Path) -> bool:
    if path.is_symlink():
        return True
    try:
        if os.path.lexists(path) and os.path.isjunction(path):  # Python 3.12+
            return True
    except (AttributeError, OSError):
        pass
    return False


def _copy_python_package(source_dir: Path, destination_dir: Path, out: dict[str, str]) -> None:
    """Copy every ``.py`` file under ``source_dir`` (repo-relative) preserving relative layout.

    Rejects reparse points and skips ``__pycache__`` / ``*.pyc`` artifacts.
    """
    for py in sorted(source_dir.rglob("*.py")):
        rel = py.relative_to(_REPO_ROOT)
        parts = rel.parts
        if "__pycache__" in parts or any(part.endswith(".pyc") for part in parts):
            continue
        if _is_reparse(py):
            raise AssertionError(f"reparse point inside source deliverable: {rel}")
        for ancestor in list(py.parents)[:-1]:
            if ancestor == _REPO_ROOT:
                break
            if _is_reparse(ancestor):
                raise AssertionError(f"reparse ancestor inside source deliverable: {rel}")
        target = destination_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        data = py.read_bytes()
        target.write_bytes(data)
        out[rel.as_posix()] = _sha256_bytes(data)


def copy_delivery_sources(source_root: Path, destination: Path) -> dict[str, str]:
    """Build a single portable source candidate and return ``{repo_relative_path: sha256}``.

    Only the app package ``.py``, the three dependency package ``src`` ``.py`` files,
    the ``static`` frontend assets, the two delivery docs and ``requirements.txt`` are
    copied.  History, tests, credentials, environment data, user data and ``.git`` are
    never copied, and no zip/wheel is produced.
    """
    destination.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str] = {}

    _copy_python_package(source_root / _APP_PACKAGE_DIR, destination, hashes)
    for pkg_dir in _PACKAGE_SRC_DIRS:
        _copy_python_package(source_root / pkg_dir, destination, hashes)

    for rel in (*_STATIC_FILES, *_DELIVERY_DOCS, _REQUIREMENTS):
        src = source_root / rel
        if not src.is_file():
            raise AssertionError(f"missing delivery source: {rel}")
        if _is_reparse(src):
            raise AssertionError(f"reparse point in delivery source: {rel}")
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        data = src.read_bytes()
        target.write_bytes(data)
        hashes[rel] = _sha256_bytes(data)
    return hashes


def make_delivery_project(root: Path, *, marker: str) -> Path:
    """Create the small inventory project with one Skill carrying ``marker``.

    Only the two ``src`` files carry explicit placeholders to be repaired; ``tests``,
    ``AGENTS.md``, ``USER_NOTES.md`` and the Skill are frozen after initialization.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", marker):
        raise AssertionError(f"invalid delivery marker: {marker!r}")
    root.mkdir(parents=True, exist_ok=True)
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "normalize.py").write_text(NORMALIZE_INITIAL, encoding="utf-8")
    (root / "src" / "render.py").write_text(RENDER_INITIAL, encoding="utf-8")
    (root / "tests" / "test_inventory.py").write_text(INVENTORY_TEST, encoding="utf-8")
    (root / "AGENTS.md").write_text(INVENTORY_AGENTS, encoding="utf-8")
    (root / "USER_NOTES.md").write_text(INVENTORY_USER_NOTES, encoding="utf-8")
    make_skill(
        root,
        "inventory",
        INVENTORY_SKILL.format(marker=marker),
    )
    return root


class _JointDeliveryProvider:
    """Observation-driven offline provider driving the Skill -> MCP -> scope -> edit -> verify loop.

    The first request's goal text selects the mode:
      * fix mode     (run 1): repair the two inventory placeholders using the marker
                    echoed back through a real stdio MCP ``echo_tool`` result.
      * readonly mode(run 2): re-propose verification and re-run it without edits.
    Every fact the provider relies on is read from the real ``ModelRequest``
    observation (selected skills, tool results), never from a test closure.
    """

    def __init__(self, observed: list[dict[str, Any]]) -> None:
        self._observed = observed
        self._mode: str | None = None
        self._calls = 0
        self._skill_marker: str | None = None
        self._mcp_marker: str | None = None
        self._skill_recorded = False
        self._read_skill_recorded = False
        self._mcp_recorded = False
        self._normalize_sha: str | None = None
        self._render_sha: str | None = None
        self._readonly_plan_requested = False

    def _extract_observations(self, state: dict[str, Any]) -> None:
        last = state.get("last_tool_result") or {}
        ext = state.get("extensions") or {}

        if self._skill_marker is None:
            for s in (ext.get("selected_skills") or []):
                if isinstance(s, dict) and s.get("id") == "inventory":
                    m = _MARKER_RE.search(str(s.get("content") or ""))
                    if m:
                        self._skill_marker = m.group(1)
                    if not self._skill_recorded:
                        self._skill_recorded = True
                        self._observed.append(
                            {
                                "phase": "selected_skill",
                                "skill_id": s.get("id"),
                                "sha256": s.get("sha256"),
                                "path": s.get("path"),
                                "content": s.get("content"),
                            }
                        )

        if (
            not self._read_skill_recorded
            and last.get("action_type") == "read_skill"
            and last.get("tool_succeeded")
        ):
            self._read_skill_recorded = True
            self._observed.append(
                {
                    "phase": "read_skill",
                    "skill_id": last.get("id"),
                    "sha256": last.get("sha256"),
                    "content": last.get("content"),
                }
            )

        if (
            self._mcp_marker is None
            and last.get("action_type") == "call_mcp"
            and last.get("tool_succeeded")
        ):
            m = _MARKER_RE.search(str(last.get("text") or ""))
            if m:
                self._mcp_marker = m.group(1)
            if not self._mcp_recorded:
                self._mcp_recorded = True
                self._observed.append(
                    {
                        "phase": "mcp_call",
                        "server_id": last.get("server_id"),
                        "tool_name": last.get("tool_name"),
                        "sha256": last.get("sha256"),
                        "text": last.get("text"),
                    }
                )

    def _scope_payload(self) -> dict[str, Any]:
        return {
            "exact_paths": ["src/normalize.py", "src/render.py"],
            "write_prefixes": [],
            "operations": ["modify"],
            "protected_paths": ["tests/test_inventory.py"],
            "verification_plan_summary": "run inventory pytest junit",
        }

    def _plan_payload(self) -> dict[str, Any]:
        return {
            "plan_id": "inventory-junit",
            "summary": "run inventory pytest junit",
            "checks": [
                {
                    "check_id": "inventory_unit",
                    "kind": "junit_exact",
                    "executable": sys.executable,
                    "argv": [
                        "-B",
                        "-m",
                        "pytest",
                        "-p",
                        "no:cacheprovider",
                        "tests/test_inventory.py",
                        "--junitxml={result_dir}/junit.xml",
                    ],
                    "cwd": ".",
                    "timeout_ms": 20000,
                    "protected_paths": ["tests/test_inventory.py"],
                    "expected_tests": [
                        {"classname": "tests.test_inventory", "name": "test_normalize"},
                        {"classname": "tests.test_inventory", "name": "test_render"},
                    ],
                }
            ],
        }

    def _next_fix(self, state: dict[str, Any]) -> str:
        last = state.get("last_tool_result") or {}

        if last.get("action_type") == "read_file" and last.get("tool_succeeded"):
            p = str(last.get("path", ""))
            sha = str(last.get("sha256") or last.get("file_sha256") or "")
            if p == "src/normalize.py":
                self._normalize_sha = sha
            elif p == "src/render.py":
                self._render_sha = sha

        if self._calls == 1:
            return ext_forecast(action_type="read_skill", payload={"skill_id": "inventory"})
        if self._calls == 2:
            return ext_forecast(action_type="connect_mcp", payload={"server_id": "srv-inventory"})
        if self._calls == 3:
            return ext_forecast(action_type="list_mcp_tools", payload={"server_id": "srv-inventory"})
        if self._calls == 4:
            if not self._skill_marker:
                return "MISSING_SKILL_MARKER_ABORT"
            return ext_forecast(
                action_type="call_mcp",
                payload={
                    "server_id": "srv-inventory",
                    "tool_name": "echo_tool",
                    "arguments": {"msg": f"MARKER::{self._skill_marker}::END"},
                },
            )
        if self._calls == 5:
            if not self._mcp_marker:
                return "MISSING_MCP_ECHO_MARKER_ABORT"
            return ext_forecast(action_type="propose_scope", payload=self._scope_payload())
        if self._calls == 6:
            return ext_forecast(action_type="propose_verification", payload=self._plan_payload())
        if self._calls == 7:
            return ext_forecast(action_type="read_file", payload={"path": "src/normalize.py"})
        if self._calls == 8:
            if not self._normalize_sha or not self._mcp_marker:
                return "MISSING_NORMALIZE_SHA_OR_MARKER_ABORT"
            return ext_forecast(
                action_type="edit_file",
                payload={
                    "path": "src/normalize.py",
                    "expected_sha256": self._normalize_sha,
                    "old_text": NORMALIZE_OLD_TEXT,
                    "new_text": NORMALIZE_NEW_TEMPLATE.format(marker=self._mcp_marker),
                },
                outcome_type="project_file_written",
                outcome_payload={"path": "src/normalize.py"},
            )
        if self._calls == 9:
            return ext_forecast(action_type="read_file", payload={"path": "src/render.py"})
        if self._calls == 10:
            if not self._render_sha or not self._mcp_marker:
                return "MISSING_RENDER_SHA_OR_MARKER_ABORT"
            return ext_forecast(
                action_type="edit_file",
                payload={
                    "path": "src/render.py",
                    "expected_sha256": self._render_sha,
                    "old_text": RENDER_OLD_TEXT,
                    "new_text": RENDER_NEW_TEMPLATE.format(marker=self._mcp_marker),
                },
                outcome_type="project_file_written",
                outcome_payload={"path": "src/render.py"},
            )
        return ext_forecast(
            action_type="verify_project",
            payload={"plan_id": state.get("plan_id") or "inventory-junit", "plan_digest": state.get("plan_digest") or ""},
            outcome_type="project_task_verified",
            outcome_payload={},
            task_hint=True,
        )

    def _next_readonly(self, state: dict[str, Any]) -> str:
        conv = state.get("conversation") or {}
        recent = conv.get("recent_items") or []
        if recent:
            self._observed.append(
                {
                    "phase": "readonly_context",
                    "recent_items": [
                        {
                            "kind": it.get("kind"),
                            "run_id": it.get("run_id"),
                            "text": it.get("text"),
                            "source_ids": it.get("source_ids"),
                            "sha256": it.get("sha256"),
                        }
                        for it in recent
                    ],
                }
            )
        if not self._readonly_plan_requested:
            self._readonly_plan_requested = True
            return ext_forecast(action_type="propose_verification", payload=self._plan_payload())
        return ext_forecast(
            action_type="verify_project",
            payload={"plan_id": state.get("plan_id") or "inventory-junit", "plan_digest": state.get("plan_digest") or ""},
            outcome_type="project_task_verified",
            outcome_payload={},
            task_hint=True,
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        state = (
            request.observation.state
            if request.observation is not None and isinstance(request.observation.state, dict)
            else {}
        )
        if self._mode is None:
            goal = request.model_context.goal_text or ""
            self._mode = "readonly" if ("不修改" in goal or "核对" in goal) else "fix"
        self._extract_observations(state)
        self._calls += 1
        content = self._next_fix(state) if self._mode == "fix" else self._next_readonly(state)
        return ModelResponse(
            schema_version="0.1",
            request_id=request.request_id,
            status="ok",
            content=content,
            usage=ModelUsage(schema_version="0.1", input_tokens=16, output_tokens=8),
        )


def make_joint_provider(*, observed: list[dict[str, Any]]) -> ModelProvider:
    """Return a fresh joint Skill/MCP provider recording observations into ``observed``."""
    return _JointDeliveryProvider(observed=observed)
