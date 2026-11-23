"""B01–B09: Read-only registration, exact root matching, bounded exploration, atomic writes (F06), and lazy revisions."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Self

import httpx
import pytest
from app_helpers import auth_headers
from forecontract_app.project_commands import ProjectCommandRunner
from forecontract_app.project_models import (
    ProjectError,
    ProjectTask,
    ProposalDecision,
    ScopeProposalPayload,
    VerificationCheck,
    VerificationPlan,
)
from forecontract_app.project_permissions import PermissionBroker
from forecontract_app.project_verification import evaluate_project_result
from forecontract_app.project_workspace import ProjectWorkspace, sha256_bytes
from forecontract_app.server import create_app
from forecontract_coding.errors import CodingError
from forecontract_coding.workspace import commit_file_atomic
from project_helpers import (
    create_buggy_calc_project,
    create_multifile_project,
    make_project_host_config,
    try_create_junction,
)


def _hash_tree(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            result[p.relative_to(root).as_posix()] = sha256_bytes(p.read_bytes())
    return result


def test_b01_register_readonly_exact_root_and_boundary(tmp_path: Path) -> None:
    allowed_parent = tmp_path / "allowed_parent"
    allowed_exact = allowed_parent / "repo_exact"
    child_inside = allowed_exact / "subchild"
    outside_dir = tmp_path / "outside"
    create_buggy_calc_project(allowed_exact)
    child_inside.mkdir(parents=True)
    create_buggy_calc_project(outside_dir)

    junction_dir = tmp_path / "junction_to_exact"
    has_junction = try_create_junction(junction_dir, allowed_exact)

    cfg = make_project_host_config(tmp_path, allowed_roots=(allowed_exact,))
    before_inventory = _hash_tree(allowed_exact)

    async def _scenario() -> None:
        app = create_app(config=cfg)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url=f"http://127.0.0.1:{cfg.bind_port}"
            ) as client:
                headers = auth_headers(cfg)

                unconfirmed = await client.post(
                    "/api/v1/projects/register",
                    headers=headers,
                    json={"name": "unconfirmed", "root": str(allowed_exact), "confirmed_read": False},
                )
                assert unconfirmed.status_code == 403
                assert unconfirmed.json()["error"]["code"] == "READ_NOT_CONFIRMED"

                outside = await client.post(
                    "/api/v1/projects/register",
                    headers=headers,
                    json={"name": "outside", "root": str(outside_dir), "confirmed_read": True},
                )
                assert outside.status_code == 403
                assert outside.json()["error"]["code"] == "PROJECT_ROOT_NOT_ALLOWED"

                # F04: exact root equality only — ancestor and descendant must both be rejected
                parent_rej = await client.post(
                    "/api/v1/projects/register",
                    headers=headers,
                    json={"name": "parent-rej", "root": str(allowed_parent), "confirmed_read": True},
                )
                assert parent_rej.status_code == 403
                assert parent_rej.json()["error"]["code"] == "PROJECT_ROOT_NOT_ALLOWED"

                child_rej = await client.post(
                    "/api/v1/projects/register",
                    headers=headers,
                    json={"name": "child-rej", "root": str(child_inside), "confirmed_read": True},
                )
                assert child_rej.status_code == 403
                assert child_rej.json()["error"]["code"] == "PROJECT_ROOT_NOT_ALLOWED"

                traversal = await client.post(
                    "/api/v1/projects/register",
                    headers=headers,
                    json={
                        "name": "trav",
                        "root": f"{allowed_exact}/../{allowed_exact.name}",
                        "confirmed_read": True,
                    },
                )
                assert traversal.status_code == 400

                overlap_data = await client.post(
                    "/api/v1/projects/register",
                    headers=headers,
                    json={"name": "overlap", "root": str(cfg.data_root), "confirmed_read": True},
                )
                assert overlap_data.status_code == 409

                if has_junction:
                    junc = await client.post(
                        "/api/v1/projects/register",
                        headers=headers,
                        json={"name": "junc", "root": str(junction_dir), "confirmed_read": True},
                    )
                    assert junc.status_code in (400, 403)

                ok = await client.post(
                    "/api/v1/projects/register",
                    headers=headers,
                    json={"name": "existing-calc", "root": str(allowed_exact), "confirmed_read": True},
                )
                assert ok.status_code == 201, ok.text
                view = ok.json()
                assert view["kind"] == "existing"
                assert view["read_paths"] == []
                assert view["write_paths"] == []
                assert view["read_grant_id"]

                dup = await client.post(
                    "/api/v1/projects/register",
                    headers=headers,
                    json={"name": "existing-calc-2", "root": str(allowed_exact), "confirmed_read": True},
                )
                assert dup.status_code == 409

    asyncio.run(_scenario())
    assert _hash_tree(allowed_exact) == before_inventory


def test_b02_list_directory_sorted_limits_and_cursor_conflict(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    data_root = tmp_path / "data"
    data_root.mkdir()

    (repo / "b_file.txt").write_text("b", encoding="utf-8")
    (repo / "c_file.txt").write_text("c", encoding="utf-8")
    (repo / "D_file.txt").write_text("d", encoding="utf-8")
    (repo / "e_file.txt").write_text("e", encoding="utf-8")

    ws = ProjectWorkspace(root=repo, data_root=data_root)
    with pytest.raises(ProjectError) as limit_exc:
        ws.list_directory(".", limit=101)
    assert limit_exc.value.code == "INVALID_INPUT"

    page1 = ws.list_directory(".", limit=2)
    assert [e.name for e in page1.entries] == ["b_file.txt", "c_file.txt"]
    assert page1.truncated is True
    assert page1.next_cursor is not None

    # Normal second page without mutation works in sorted order
    page2 = ws.list_directory(".", cursor=page1.next_cursor, limit=2)
    assert [e.name for e in page2.entries] == ["D_file.txt", "e_file.txt"]
    assert page2.truncated is False

    # F05: insert "a_file.txt" before "b_file.txt" after page1 cursor was issued -> old cursor MUST 409 conflict
    (repo / "a_file.txt").write_text("a", encoding="utf-8")
    with pytest.raises(ProjectError) as stale_exc:
        ws.list_directory(".", cursor=page1.next_cursor, limit=2)
    assert stale_exc.value.status_code == 409
    assert stale_exc.value.code == "INVALID_CURSOR"


def test_b03_find_paths_and_search_text_bounded_batches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import forecontract_app.project_workspace as pw

    repo = tmp_path / "multi"
    data_root = tmp_path / "data"
    data_root.mkdir()
    create_multifile_project(repo, include_large_notes=False, doc_count=300)

    # Put binary noise file inside docs that must be skipped without crashing search_text
    (repo / "docs" / "aaa_binary.bin").write_bytes(b"\xff\xfe\x00\x00notutf8")

    ws = ProjectWorkspace(root=repo, data_root=data_root)

    # Batch 1 scans <= 200 files or <= 4 MiB; "TARGET_IN_BATCH_TWO" lives in item250.txt (batch 2+)
    s1 = ws.search_text("TARGET_IN_BATCH_TWO")
    assert s1.scanned <= 200
    assert s1.matches == []
    assert s1.truncated is True
    assert s1.next_cursor is not None

    all_matches: list[str] = []
    cur = s1.next_cursor
    steps = 1
    while cur is not None:
        nxt = ws.search_text("TARGET_IN_BATCH_TWO", cursor=cur)
        all_matches.extend(m.path for m in nxt.matches)
        cur = nxt.next_cursor
        steps += 1
        assert steps <= 10
    assert all_matches == ["docs/item250.txt"]

    # 5 MiB file bounded-batch search + real reader I/O instrumentation (F03)
    large_dir = repo / "large_dir"
    large_dir.mkdir()
    large_file = large_dir / "large.txt"
    large_file.write_bytes(b"a" * (4 * 1024 * 1024 + 128) + b"\nNEEDLE_AFTER_4MIB\n")
    giant_line = large_dir / "giant_line.txt"
    giant_line.write_bytes(b"b" * (5 * 1024 * 1024))

    read_bytes_calls: list[Path] = []
    streamed_bytes = 0
    orig_read_bytes = Path.read_bytes
    orig_path_open = Path.open

    def _spy_read_bytes(self_path: Path) -> bytes:
        if self_path in (large_file, giant_line):
            read_bytes_calls.append(self_path)
        return orig_read_bytes(self_path)

    class _CountingReader:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def fileno(self) -> int:
            return int(self._inner.fileno())

        def seek(self, offset: int, whence: int = 0) -> int:
            return int(self._inner.seek(offset, whence))

        def read(self, size: int = -1) -> bytes:
            nonlocal streamed_bytes
            chunk = self._inner.read(size)
            streamed_bytes += len(chunk)
            return chunk

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *args: object) -> Any:
            return self._inner.__exit__(*args)

    def _spy_open(self_path: Path, *args: Any, **kwargs: Any) -> Any:
        handle = orig_path_open(self_path, *args, **kwargs)
        mode = args[0] if args else kwargs.get("mode", "r")
        if "b" in str(mode) and self_path in (large_file, giant_line):
            return _CountingReader(handle)
        return handle

    monkeypatch.setattr(Path, "read_bytes", _spy_read_bytes)
    monkeypatch.setattr(Path, "open", _spy_open)

    streamed_bytes = 0
    b_large_1 = ws.search_text("NEEDLE_AFTER_4MIB", paths=["large_dir/large.txt"])
    assert read_bytes_calls == []
    assert streamed_bytes <= pw.PROJECT_MAX_SEARCH_BATCH_BYTES
    assert b_large_1.matches == []
    assert b_large_1.truncated is True
    assert b_large_1.next_cursor is not None

    streamed_bytes = 0
    b_large_2 = ws.search_text(
        "NEEDLE_AFTER_4MIB", paths=["large_dir/large.txt"], cursor=b_large_1.next_cursor
    )
    assert read_bytes_calls == []
    assert streamed_bytes <= pw.PROJECT_MAX_SEARCH_BATCH_BYTES
    assert b_large_2.truncated is False
    assert b_large_2.next_cursor is None
    assert [(m.path, m.line_number, "NEEDLE_AFTER_4MIB" in m.line_text) for m in b_large_2.matches] == [
        ("large_dir/large.txt", 2, True)
    ]

    streamed_bytes = 0
    b_giant_1 = ws.search_text("MISSING_TOKEN", paths=["large_dir/giant_line.txt"])
    assert read_bytes_calls == []
    assert streamed_bytes <= pw.PROJECT_MAX_SEARCH_BATCH_BYTES
    assert b_giant_1.truncated is True
    assert b_giant_1.next_cursor is not None
    b_giant_2 = ws.search_text(
        "MISSING_TOKEN", paths=["large_dir/giant_line.txt"], cursor=b_giant_1.next_cursor
    )
    assert b_giant_2.matches == []
    assert b_giant_2.truncated is False
    assert b_giant_2.next_cursor is None
    monkeypatch.undo()

    # 50 match / 16 KiB output bound check
    hit_dir = repo / "many_hits"
    hit_dir.mkdir()
    big_line = "REPEAT_MATCH " + ("Z" * 800)
    (hit_dir / "hits.txt").write_text("\n".join([big_line] * 80) + "\n", encoding="utf-8")
    bounded = ws.search_text("REPEAT_MATCH", paths=["many_hits/hits.txt"], max_matches=50)
    assert len(bounded.matches) <= 50
    encoded = json.dumps([m.model_dump() for m in bounded.matches], ensure_ascii=False).encode("utf-8")
    assert len(encoded) <= 20 * 1024

    # find_paths pagination & traversal rejection
    fp1 = ws.find_paths("docs/*.txt", limit=50)
    assert len(fp1.paths) == 50
    assert fp1.truncated is True
    assert fp1.next_cursor is not None
    with pytest.raises(ProjectError):
        ws.search_text("x", paths=["../outside.txt"])


def test_b04_read_file_chunking_utf8_crlf_5mib_and_8mib_limit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    data_root = tmp_path / "data"
    data_root.mkdir()

    # >64 KiB file with multi-byte UTF-8, CRLF, and >20 KiB single long line
    long_line = ("汉字LONG-" * 6000) + "\r\n"
    tail_lines = "".join(f"L{i:03d}-文本内容\r\n" for i in range(500))
    raw_bytes = (long_line + tail_lines).encode("utf-8")
    assert len(raw_bytes) > 64 * 1024
    target = repo / "notes.txt"
    target.write_bytes(raw_bytes)

    ws = ProjectWorkspace(root=repo, data_root=data_root)
    pieces: list[str] = []
    start_line = 1
    byte_offset = 0
    sha: str | None = None
    chunk_count = 0
    while True:
        res = ws.read_file(
            "notes.txt",
            start_line=start_line,
            max_lines=200,
            byte_offset=byte_offset,
            expected_sha256=sha,
        )
        assert len(res.content.encode("utf-8")) <= 16 * 1024
        sha = sha or res.sha256
        pieces.append(res.content)
        chunk_count += 1
        if res.eof:
            break
        if res.next_byte_offset is not None:
            byte_offset = res.next_byte_offset
        elif res.next_start_line is not None:
            byte_offset = 0
            start_line = res.next_start_line
        else:
            pytest.fail("truncated read without continuation")
    assert chunk_count >= 5
    assert "".join(pieces).encode("utf-8") == raw_bytes

    # Mid-read mutation raises 409 FILE_CONFLICT
    first = ws.read_file("notes.txt", max_lines=10)
    target.write_text("changed concurrently\n", encoding="utf-8")
    with pytest.raises(ProjectError) as conflict:
        ws.read_file(
            "notes.txt",
            byte_offset=first.next_byte_offset or 0,
            expected_sha256=first.sha256,
        )
    assert conflict.value.code == "FILE_CONFLICT"

    # Valid 5 MiB file reads cleanly without false READ_LIMIT
    five_mib = repo / "five_mib.txt"
    five_mib.write_bytes(b"A" * (5 * 1024 * 1024))
    chunk5 = ws.read_file("five_mib.txt", byte_offset=0)
    assert chunk5.total_bytes == 5 * 1024 * 1024
    assert len(chunk5.content.encode("utf-8")) <= 16 * 1024

    # Over 8 MiB -> 413 READ_LIMIT
    over_eight = repo / "over_8mib.txt"
    over_eight.write_bytes(b"B" * (8 * 1024 * 1024 + 1))
    with pytest.raises(ProjectError) as lim:
        ws.read_file("over_8mib.txt")
    assert lim.value.code == "READ_LIMIT"
    assert lim.value.status_code == 413


def test_b05_exclusions_gitignore_ignored_and_instruction_chain(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src" / "sub").mkdir(parents=True)
    data_root = tmp_path / "data"
    data_root.mkdir()

    # F05: root 10 KiB AGENTS.md (> 8192 bytes!) must be read in full, NOT truncated at 8192!
    root_marker = "ROOT_END_MARKER_10K"
    root_text = ("Root guidance line.\n" * 520) + root_marker + "\n"
    root_bytes = root_text.encode("utf-8")
    assert 10 * 1024 <= len(root_bytes) < 16 * 1024
    (repo / "AGENTS.md").write_bytes(root_bytes)
    (repo / "src" / "AGENTS.md").write_text("Sub guidance.\n", encoding="utf-8")
    (repo / "src" / "sub" / "mod.py").write_text("VALUE = 1\n", encoding="utf-8")

    # Excluded files & .gitignore check
    (repo / ".gitignore").write_text("ignored_by_gitignore.txt\n", encoding="utf-8")
    (repo / "ignored_by_gitignore.txt").write_text("must still be visible", encoding="utf-8")
    (repo / ".env").write_text("KEY=1\n", encoding="utf-8")
    (repo / ".env.example").write_text("KEY=example\n", encoding="utf-8")
    (repo / ".git").mkdir()
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (repo / "secrets.json").write_text("{}", encoding="utf-8")

    ws = ProjectWorkspace(root=repo, data_root=data_root)
    listing = {e.name for e in ws.list_directory(".", limit=100).entries}
    assert "ignored_by_gitignore.txt" in listing
    for secret in (".env", ".env.example", ".git", "secrets.json"):
        assert secret not in listing
    for secret_path in (".env", ".env.example", ".git/HEAD", "secrets.json"):
        with pytest.raises(ProjectError) as exc:
            ws.read_file(secret_path)
        assert exc.value.code == "PROTECTED_PATH_FORBIDDEN"

    # Full 10 KiB root instruction + src/AGENTS.md loaded completely in order
    chain = ws.load_instructions("src/sub/mod.py")
    assert chain.present is True
    assert [f.path for f in chain.files] == ["AGENTS.md", "src/AGENTS.md"]
    assert root_marker in chain.content

    # >16 KiB single AGENTS.md raises INSTRUCTIONS_INCOMPLETE
    (repo / "src" / "AGENTS.md").write_bytes(b"X" * (16 * 1024 + 1))
    with pytest.raises(ProjectError) as too_big:
        ws.load_instructions("src/sub/mod.py")
    assert too_big.value.code == "INSTRUCTIONS_INCOMPLETE"

    # 5 nested AGENTS.md files each <=16 KiB totaling >64 KiB raises INSTRUCTIONS_INCOMPLETE
    nested = repo / "a" / "b" / "c" / "d" / "e"
    nested.mkdir(parents=True)
    for anc in (repo / "a", repo / "a" / "b", repo / "a" / "b" / "c", repo / "a" / "b" / "c" / "d", nested):
        (anc / "AGENTS.md").write_bytes(b"Y" * (15 * 1024))
    with pytest.raises(ProjectError) as chain_over:
        ws.load_instructions("a/b/c/d/e/file.py")
    assert chain_over.value.code == "INSTRUCTIONS_INCOMPLETE"


def test_b06_scope_plan_create_multi_edit_and_unified_diff(tmp_path: Path) -> None:
    repo = create_buggy_calc_project(tmp_path / "repo")
    data_root = tmp_path / "data"
    data_root.mkdir()
    (repo / "extra.py").write_text("VAL = 1\n", encoding="utf-8")

    ws = ProjectWorkspace(root=repo, data_root=data_root)
    ws.capture_baseline()
    broker = PermissionBroker(
        run_id="r-b06",
        project_root=repo,
        root_identity=ws.root_identity,
        ttl_seconds=120.0,
    )

    # Without scope approval, writes are blocked
    with pytest.raises(ProjectError) as no_scope:
        broker.verify_write_allowed(
            path="calc.py",
            operation="modify",
            current_run_id="r-b06",
            current_root_identity=ws.root_identity,
            current_instruction_sha256="",
        )
    assert no_scope.value.code == "SCOPE_REQUIRED"

    scope_prop = broker.create_scope_proposal(
        scope=ScopeProposalPayload(
            exact_paths=["new_helper.py", "calc.py", "extra.py"],
            operations=["create", "modify"],
            protected_paths=["tests/test_calc.py"],
            verification_plan_summary="run calc pytest",
        ),
        current_instruction_sha256="",
    )
    broker.decide(
        proposal_id=scope_prop.id,
        request=ProposalDecision(digest=scope_prop.digest, decision="approve"),
    )

    ws.create_file("new_helper.py", "HELP = 42\n")
    r1 = ws.read_file("calc.py")
    ws.edit_file(
        path="calc.py",
        expected_sha256=r1.sha256,
        old_text="return a - b",
        new_text="return a + b",
    )
    r2 = ws.read_file("extra.py")
    ws.edit_file(
        path="extra.py",
        expected_sha256=r2.sha256,
        old_text="VAL = 1",
        new_text="VAL = 2",
    )

    diff = ws.diff_from_baseline()
    by_path = {e.path: e for e in diff.entries}
    assert set(by_path) == {"new_helper.py", "calc.py", "extra.py"}
    assert by_path["new_helper.py"].operation == "create"
    assert "+HELP = 42" in by_path["new_helper.py"].diff_excerpt
    assert "-    return a - b" in by_path["calc.py"].diff_excerpt
    assert "+    return a + b" in by_path["calc.py"].diff_excerpt
    assert "-VAL = 1" in by_path["extra.py"].diff_excerpt
    assert "+VAL = 2" in by_path["extra.py"].diff_excerpt

    # Q00 & Q01: nested AGENTS.md chain through ProjectCodingAdapter + plain AGENTS.md create_file
    from forecontract.models.actions import ActionSpec
    from forecontract.models.outcomes import ExecutionStatus
    from forecontract_app.project_adapter import ProjectCodingAdapter

    nested_repo = create_buggy_calc_project(tmp_path / "nested_repo")
    nested_data = tmp_path / "nested_data"
    nested_data.mkdir()
    (nested_repo / "AGENTS.md").write_text("root policy v1\n", encoding="utf-8")
    (nested_repo / "src").mkdir()
    (nested_repo / "src" / "AGENTS.md").write_text("src policy v1\n", encoding="utf-8")
    nested_ws = ProjectWorkspace(root=nested_repo, data_root=nested_data)
    nested_broker = PermissionBroker(
        run_id="r-b06-nested",
        project_root=nested_repo,
        root_identity=nested_ws.root_identity,
        ttl_seconds=120.0,
    )
    nested_runner = ProjectCommandRunner(
        run_id="r-b06-nested",
        project_root=nested_repo,
        data_root=nested_data,
        broker=nested_broker,
    )
    nested_task = ProjectTask(
        run_id="r-b06-nested",
        goal_id="g-b06-nested",
        goal_text="Nested instruction chain test",
        project_id="p-b06-nested",
        root_identity=nested_ws.root_identity,
        read_grant_id="rg-b06-nested",
        data_root=nested_data,
        output_root=tmp_path / "nested_out",
        python_executable=Path(sys.executable),
    )
    nested_adapter = ProjectCodingAdapter(
        task=nested_task, workspace=nested_ws, broker=nested_broker, command_runner=nested_runner
    )

    async def _run_nested_with_approvals() -> None:
        await nested_adapter.start()

        async def _approve_when_pending() -> None:
            for _ in range(500):
                if nested_broker.pending_proposals():
                    break
                await asyncio.sleep(0.01)
            else:
                raise TimeoutError("timed out waiting for pending proposal in test_b06")
            for p in nested_broker.pending_proposals():
                nested_broker.decide(
                    proposal_id=p.id,
                    request=ProposalDecision(digest=p.digest, decision="approve"),
                )

        t1 = asyncio.create_task(_approve_when_pending())
        r_scope = await nested_adapter.execute(
            ActionSpec(
                schema_version="0.1",
                action_type="propose_scope",
                payload={
                    "exact_paths": ["src/new.py"],
                    "operations": ["create", "modify"],
                    "protected_paths": ["tests/test_calc.py"],
                    "verification_plan_summary": "run calc",
                },
                metadata={"action_id": "n-scope"},
            )
        )
        await t1
        assert r_scope.status == ExecutionStatus.SUCCEEDED

        t2 = asyncio.create_task(_approve_when_pending())
        r_plan = await nested_adapter.execute(
            ActionSpec(
                schema_version="0.1",
                action_type="propose_verification",
                payload={
                    "plan_id": "plan-nested",
                    "summary": "unit check",
                    "checks": [
                        {
                            "check_id": "u1",
                            "kind": "exit_code",
                            "executable": sys.executable,
                            "argv": ["-V"],
                            "cwd": ".",
                            "timeout_ms": 5000,
                        }
                    ],
                },
                metadata={"action_id": "n-plan"},
            )
        )
        await t2
        assert r_plan.status == ExecutionStatus.SUCCEEDED

        r_create = await nested_adapter.execute(
            ActionSpec(
                schema_version="0.1",
                action_type="create_file",
                payload={"path": "src/new.py", "content": "x = 1\n"},
                metadata={"action_id": "n-create"},
            )
        )
        assert r_create.status == ExecutionStatus.SUCCEEDED
        assert (nested_repo / "src" / "new.py").read_bytes() == b"x = 1\n"

        # Tampering child AGENTS.md invalidates subsequent write under F01
        (nested_repo / "src" / "AGENTS.md").write_text("src policy TAMPERED\n", encoding="utf-8")
        r_tamper = await nested_adapter.execute(
            ActionSpec(
                schema_version="0.1",
                action_type="edit_file",
                payload={
                    "path": "src/new.py",
                    "expected_sha256": sha256_bytes(b"x = 1\n"),
                    "old_text": "x = 1",
                    "new_text": "x = 999",
                },
                metadata={"action_id": "n-edit"},
            )
        )
        assert r_tamper.status == ExecutionStatus.FAILED
        assert r_tamper.error is not None
        assert r_tamper.error.code == "INSTRUCTIONS_CHANGED"
        assert (nested_repo / "src" / "new.py").read_bytes() == b"x = 1\n"

    asyncio.run(_run_nested_with_approvals())


def test_b07_write_rejection_and_f06_commit_file_atomic_limit(tmp_path: Path) -> None:
    outside_sentinel = tmp_path / "sentinel.txt"
    outside_sentinel.write_bytes(b"UNTOUCHED_SENTINEL")
    root = create_buggy_calc_project(tmp_path / "repo")
    data_root = tmp_path / "data"
    data_root.mkdir()
    (root / "AGENTS.md").write_text("Stay safe.\n", encoding="utf-8")

    ws = ProjectWorkspace(root=root, data_root=data_root)
    ws.set_extra_protected_paths(["tests/test_calc.py"])
    for forbidden in ("../sentinel.txt", "AGENTS.md", ".env", "tests/test_calc.py"):
        with pytest.raises(ProjectError):
            ws.create_file(forbidden, "evil\n")
    assert outside_sentinel.read_bytes() == b"UNTOUCHED_SENTINEL"

    # Preserve legacy create + non-None expected_sha256 -> CODING_FILE_CONFLICT
    with pytest.raises(CodingError) as legacy_conflict:
        commit_file_atomic(
            workspace_root=root,
            path="conflict_create.txt",
            operation="create",
            expected_sha256="0" * 64,
            content=b"abc",
        )
    assert legacy_conflict.value.code == "CODING_FILE_CONFLICT"
    assert not (root / "conflict_create.txt").exists()

    # Exact F06 assertions (contract §3)
    before = outside_sentinel.read_bytes()
    with pytest.raises(CodingError, match="CODING_INPUT_INVALID"):
        commit_file_atomic(
            workspace_root=root,
            path="over.txt",
            operation="create",
            expected_sha256=None,
            content=b"x" * 65537,
            max_file_bytes=65537,
        )
    assert not (root / "over.txt").exists()
    assert outside_sentinel.read_bytes() == before
    commit_file_atomic(
        workspace_root=root,
        path="valid.txt",
        operation="create",
        expected_sha256=None,
        content=b"x" * 65536,
        max_file_bytes=65536,
    )
    assert (root / "valid.txt").read_bytes() == b"x" * 65536


def test_b08_observation_64kib_cap_and_overflow_flag(tmp_path: Path) -> None:
    from forecontract.models.actions import ActionSpec
    from forecontract_app.project_adapter import ProjectCodingAdapter

    root = create_buggy_calc_project(tmp_path / "repo")
    data_root = tmp_path / "data"
    data_root.mkdir()
    big_file = root / "giant.txt"
    # Giant content across read_file + search_text would exceed 16 KiB per tool if unbounded;
    # pushing huge instruction content + big tool result triggers 64 KiB Observation cap.
    (root / "AGENTS.md").write_bytes(b"I" * (16 * 1024))
    sub = root / "sub1" / "sub2" / "sub3"
    sub.mkdir(parents=True)
    for anc in (root / "sub1", root / "sub1" / "sub2", sub):
        (anc / "AGENTS.md").write_bytes(b"M" * (15 * 1024))
    big_file.write_text("X" * 15000 + "\n", encoding="utf-8")

    ws = ProjectWorkspace(root=root, data_root=data_root)
    broker = PermissionBroker(
        run_id="r-b08",
        project_root=root,
        root_identity=ws.root_identity,
    )
    runner = ProjectCommandRunner(run_id="r-b08", project_root=root, data_root=data_root, broker=broker)
    task = ProjectTask(
        run_id="r-b08",
        goal_id="g-b08",
        goal_text="Check observation cap",
        project_id="p-b08",
        root_identity=ws.root_identity,
        read_grant_id="rg-b08",
        data_root=data_root,
        output_root=tmp_path / "out",
        python_executable=Path(Path(__file__).resolve().as_posix()),
    )
    adapter = ProjectCodingAdapter(task=task, workspace=ws, broker=broker, command_runner=runner)

    async def _scenario() -> Any:
        await adapter.start()
        # Force a huge last_tool_result to test 64 KiB observation truncation guard
        adapter._last_tool_result = {
            "action_type": "read_file",
            "tool_succeeded": True,
            "content": "Y" * (70 * 1024),
        }
        return await adapter.observe()

    obs = asyncio.run(_scenario())
    encoded = json.dumps(obs.state, ensure_ascii=False, allow_nan=False).encode("utf-8")
    assert len(encoded) <= 64 * 1024
    assert obs.state["observation_overflow"] is True
    assert obs.state["state_complete"] is False
    # Verify validate_action succeeds for valid action without disk I/O
    spec = ActionSpec(schema_version="0.1", action_type="list_directory", payload={"path": "."})
    assert adapter.validate_action(spec) is None


def test_b09_lazy_observation_command_diff_and_scope_limit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from forecontract_app.project_adapter import ProjectCodingAdapter
    from forecontract_app.project_models import CheckExecutionRecord

    root = create_buggy_calc_project(tmp_path / "repo")
    (root / "to_modify.py").write_text("X = 1\n", encoding="utf-8")
    (root / "to_delete.py").write_text("DEL = 1\n", encoding="utf-8")
    data_root = tmp_path / "data"
    data_root.mkdir()

    ws = ProjectWorkspace(root=root, data_root=data_root)
    scan_calls: list[str] = []
    orig_capture = ws.capture_revision

    def _spy_capture() -> Any:
        scan_calls.append("capture")
        return orig_capture()

    monkeypatch.setattr(ws, "capture_revision", _spy_capture)

    broker = PermissionBroker(run_id="r-b09", project_root=root, root_identity=ws.root_identity)
    runner = ProjectCommandRunner(run_id="r-b09", project_root=root, data_root=data_root, broker=broker)
    task = ProjectTask(
        run_id="r-b09",
        goal_id="g-b09",
        goal_text="Lazy revision and diff test",
        project_id="p-b09",
        root_identity=ws.root_identity,
        read_grant_id="rg-b09",
        data_root=data_root,
        output_root=tmp_path / "out",
        python_executable=Path(Path(__file__).resolve().as_posix()),
    )
    adapter = ProjectCodingAdapter(task=task, workspace=ws, broker=broker, command_runner=runner)

    async def _scenario() -> None:
        await adapter.start()
        before_count = len(scan_calls)
        # Repeated observe() calls MUST be lazy and MUST NOT trigger full-repo scan
        for _ in range(5):
            await adapter.observe()
        assert len(scan_calls) == before_count

    asyncio.run(_scenario())

    # Simulate external/command-created add/modify/delete
    (root / "cmd_created.txt").write_text("added\n", encoding="utf-8")
    (root / "to_modify.py").write_text("X = 2\n", encoding="utf-8")
    (root / "to_delete.py").unlink()
    diff = ws.diff_from_baseline()
    ops = {e.path: e for e in diff.entries}
    assert ops["cmd_created.txt"].operation == "create"
    assert ops["cmd_created.txt"].source == "external_or_command"
    assert ops["to_modify.py"].operation == "modify"
    assert ops["to_modify.py"].source == "external_or_command"
    assert ops["to_delete.py"].operation == "delete"
    assert ops["to_delete.py"].source == "external_or_command"
    assert ".git" in diff.excluded_note

    # Lower 64 MiB budget boundary via monkeypatch on project_workspace constant to test scope_complete=False cleanly
    import forecontract_app.project_workspace as pw

    monkeypatch.setattr(pw, "PROJECT_MAX_SCAN_TOTAL_BYTES", 20)
    rev_over = orig_capture()
    assert rev_over.scope_complete is False

    ev = adapter.build_evidence(core_completed=True)
    ev = ev.model_copy(
        update={
            "scope_complete": False,
            "approved_plan": VerificationPlan(
                plan_id="p1",
                summary="s",
                checks=[
                    VerificationCheck(
                        check_id="c1",
                        kind="exit_code",
                        executable="python",
                        argv=["-V"],
                        cwd=".",
                        timeout_ms=5000,
                    )
                ],
            ),
            "checks": [
                CheckExecutionRecord(
                    check_id="c1",
                    kind="exit_code",
                    command_id="cmd1",
                    proposal_id="pr1",
                    proposal_digest="a" * 64,
                    executable="python",
                    executable_sha256="b" * 64,
                    argv=["-V"],
                    cwd=".",
                    exit_code=0,
                    start_revision=ev.final_revision,
                    end_revision=ev.final_revision,
                    passed=True,
                )
            ],
        }
    )
    res = evaluate_project_result(task=task, evidence=ev)
    assert res.terminal_status == "failed"
    assert res.verification == "unknown"
    assert res.reason_code == "SCOPE_INCOMPLETE"
