"""Offline API and boundary tests for UI-T01 read-only file and Git review endpoints."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import forecontract_app.project_review as project_review_mod
import httpx
import pytest
from app_helpers import LiveTcpTestServer, auth_headers
from project_helpers import make_project_host_config


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return proc.stdout.strip()


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "offline@example.invalid")
    _git(repo, "config", "user.name", "Offline Test")


def _register_project(base_url: str, hdrs: dict[str, str], root: Path, name: str = "review-proj") -> str:
    resp = httpx.post(
        f"{base_url}/api/v1/projects/register",
        headers=hdrs,
        json={"name": name, "root": str(root), "confirmed_read": True},
        timeout=10.0,
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["project_id"])


def test_binding_and_file_paging(tmp_path: Path) -> None:
    """Verify pid/binding checks, exclusion enforcement, cursor paging, and expected_sha256 conflicts."""
    proj = tmp_path / "proj-a"
    proj.mkdir()
    (proj / "a.txt").write_text("alpha\n", encoding="utf-8")
    (proj / "b.txt").write_text("beta\n", encoding="utf-8")
    (proj / "c.txt").write_text("gamma\n", encoding="utf-8")
    (proj / ".env").write_text("TOP_SECRET=do_not_expose\n", encoding="utf-8")

    cfg = make_project_host_config(tmp_path, allowed_roots=(proj,))
    with LiveTcpTestServer(config=cfg) as srv:
        hdrs = auth_headers(cfg)
        pid = _register_project(srv.base_url, hdrs, proj)

        # Controlled project is rejected for existing-project file/git review
        ctrl = httpx.post(
            f"{srv.base_url}/api/v1/projects",
            headers=hdrs,
            json={"name": "ctrl-proj"},
            timeout=10.0,
        )
        assert ctrl.status_code == 201
        ctrl_pid = ctrl.json()["project_id"]
        assert (
            httpx.get(f"{srv.base_url}/api/v1/projects/{ctrl_pid}/files", headers=hdrs).status_code
            == 403
        )

        # Unknown pid -> 404
        assert (
            httpx.get(f"{srv.base_url}/api/v1/projects/nonexistent-pid/files", headers=hdrs).status_code
            == 404
        )

        # Paging returns visible files only (.env excluded)
        page1 = httpx.get(
            f"{srv.base_url}/api/v1/projects/{pid}/files",
            headers=hdrs,
            params={"path": ".", "limit": 2},
            timeout=10.0,
        )
        assert page1.status_code == 200
        d1 = page1.json()
        names1 = [e["name"] for e in d1["entries"]]
        assert ".env" not in names1
        assert d1["truncated"] is True
        assert d1["next_cursor"] is not None

        page2 = httpx.get(
            f"{srv.base_url}/api/v1/projects/{pid}/files",
            headers=hdrs,
            params={"path": ".", "cursor": d1["next_cursor"], "limit": 2},
            timeout=10.0,
        )
        assert page2.status_code == 200
        names2 = [e["name"] for e in page2.json()["entries"]]
        assert names1 + names2 == ["a.txt", "b.txt", "c.txt"]

        # Excluded and escaping paths are refused
        assert (
            httpx.get(
                f"{srv.base_url}/api/v1/projects/{pid}/file",
                headers=hdrs,
                params={"path": ".env"},
            ).status_code
            in (403, 404)
        )
        assert (
            httpx.get(
                f"{srv.base_url}/api/v1/projects/{pid}/file",
                headers=hdrs,
                params={"path": "../escape.txt"},
            ).status_code
            in (400, 403)
        )

        # Read file + expected_sha256 mismatch -> 409
        read1 = httpx.get(
            f"{srv.base_url}/api/v1/projects/{pid}/file",
            headers=hdrs,
            params={"path": "a.txt"},
        )
        assert read1.status_code == 200
        sha1 = read1.json()["sha256"]
        (proj / "a.txt").write_text("alpha modified\n", encoding="utf-8")
        conflict = httpx.get(
            f"{srv.base_url}/api/v1/projects/{pid}/file",
            headers=hdrs,
            params={"path": "a.txt", "expected_sha256": sha1},
        )
        assert conflict.status_code == 409

    # Persisted binding becomes invalid when allowed_roots no longer permits proj
    other_root = tmp_path / "other"
    other_root.mkdir()
    cfg2 = make_project_host_config(tmp_path, allowed_roots=(other_root,))
    with LiveTcpTestServer(config=cfg2) as srv2:
        hdrs2 = auth_headers(cfg2)
        assert (
            httpx.get(f"{srv2.base_url}/api/v1/projects/{pid}/files", headers=hdrs2).status_code
            == 403
        )
        assert (
            httpx.get(f"{srv2.base_url}/api/v1/projects/{pid}/git/status", headers=hdrs2).status_code
            == 403
        )


def test_git_states_and_patches(tmp_path: Path) -> None:
    """Verify initial unborn HEAD, dual-area diffs, deleted, untracked, binary, conflict, and detached HEAD."""
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "init.txt").write_text("first\n", encoding="utf-8")
    _git(repo, "add", "init.txt")

    cfg = make_project_host_config(tmp_path, allowed_roots=(repo,))
    with LiveTcpTestServer(config=cfg) as srv:
        hdrs = auth_headers(cfg)
        pid = _register_project(srv.base_url, hdrs, repo)

        # Unborn HEAD with staged new file
        st0 = httpx.get(f"{srv.base_url}/api/v1/projects/{pid}/git/status", headers=hdrs).json()
        assert st0["state"] == "ready"
        assert st0["branch"] == "main"
        assert st0["head_oid"] is None
        assert any(e["path"] == "init.txt" and e["index_status"] == "A" for e in st0["entries"])

        d_init = httpx.get(
            f"{srv.base_url}/api/v1/projects/{pid}/git/diff",
            headers=hdrs,
            params={"path": "init.txt", "area": "index"},
        ).json()
        assert d_init["kind"] == "text"
        assert "+first" in d_init["patch"]

        # Commit baseline, then create staged, unstaged, deleted, untracked, punctuation (with brackets), and binary
        (repo / "keep.txt").write_text("line1\n", encoding="utf-8")
        (repo / "note [v1]+ok.txt").write_text("punct1\n", encoding="utf-8")
        (repo / "del.txt").write_text("remove me\n", encoding="utf-8")
        (repo / "bin.dat").write_bytes(b"\x00\x01\x02\x03")
        _git(repo, "add", "keep.txt", "note [v1]+ok.txt", "del.txt", "bin.dat")
        _git(repo, "commit", "-m", "base")

        # Staged edit + distinct worktree edit on keep.txt and bracket punctuation file
        (repo / "keep.txt").write_text("line1\nstaged_line\n", encoding="utf-8")
        (repo / "note [v1]+ok.txt").write_text("punct1\npunct2\n", encoding="utf-8")
        _git(repo, "add", "keep.txt", "note [v1]+ok.txt")
        (repo / "note [v1]+ok.txt").write_text("punct1\npunct2\npunct3\n", encoding="utf-8")
        (repo / "keep.txt").write_text("line1\nstaged_line\nworktree_line\n", encoding="utf-8")
        (repo / "del.txt").unlink()
        (repo / "untracked.txt").write_text("hello untracked\n", encoding="utf-8")
        (repo / "bin.dat").write_bytes(b"\x00\xff\xfe\x80")

        st1 = httpx.get(f"{srv.base_url}/api/v1/projects/{pid}/git/status", headers=hdrs).json()
        assert st1["head_oid"] is not None
        by_path = {e["path"]: e for e in st1["entries"]}
        assert by_path["keep.txt"]["index_status"] == "M"
        assert by_path["keep.txt"]["worktree_status"] == "M"
        assert by_path["del.txt"]["worktree_status"] == "D"
        assert by_path["untracked.txt"]["index_status"] == "?"
        d_punct_idx = httpx.get(
            f"{srv.base_url}/api/v1/projects/{pid}/git/diff",
            headers=hdrs,
            params={"path": "note [v1]+ok.txt", "area": "index"},
        ).json()
        assert d_punct_idx["kind"] == "text" and "+punct2" in d_punct_idx["patch"]
        assert d_punct_idx["path"] == "note [v1]+ok.txt"
        d_punct_wt = httpx.get(
            f"{srv.base_url}/api/v1/projects/{pid}/git/diff",
            headers=hdrs,
            params={"path": "note [v1]+ok.txt", "area": "worktree"},
        ).json()
        assert d_punct_wt["kind"] == "text" and "+punct3" in d_punct_wt["patch"]
        assert d_punct_wt["path"] == "note [v1]+ok.txt"

        diff_idx = httpx.get(
            f"{srv.base_url}/api/v1/projects/{pid}/git/diff",
            headers=hdrs,
            params={"path": "keep.txt", "area": "index"},
        ).json()
        assert diff_idx["kind"] == "text"
        assert "+staged_line" in diff_idx["patch"]
        assert "worktree_line" not in diff_idx["patch"]

        diff_wt = httpx.get(
            f"{srv.base_url}/api/v1/projects/{pid}/git/diff",
            headers=hdrs,
            params={"path": "keep.txt", "area": "worktree"},
        ).json()
        assert diff_wt["kind"] == "text"
        assert "+worktree_line" in diff_wt["patch"]

        diff_del = httpx.get(
            f"{srv.base_url}/api/v1/projects/{pid}/git/diff",
            headers=hdrs,
            params={"path": "del.txt", "area": "worktree"},
        ).json()
        assert diff_del["kind"] == "text"
        assert "-remove me" in diff_del["patch"]

        diff_untracked = httpx.get(
            f"{srv.base_url}/api/v1/projects/{pid}/git/diff",
            headers=hdrs,
            params={"path": "untracked.txt", "area": "worktree"},
        ).json()
        assert diff_untracked["kind"] == "untracked"

        diff_bin = httpx.get(
            f"{srv.base_url}/api/v1/projects/{pid}/git/diff",
            headers=hdrs,
            params={"path": "bin.dat", "area": "worktree"},
        ).json()
        assert diff_bin["kind"] == "binary"

        # Detached HEAD check
        _git(repo, "checkout", "--detach", "HEAD")
        st_det = httpx.get(f"{srv.base_url}/api/v1/projects/{pid}/git/status", headers=hdrs).json()
        assert st_det["branch"] is None
        assert st_det["head_oid"] == _git(repo, "rev-parse", "--short", "HEAD")

        # Conflict state check
        _git(repo, "checkout", "main")
        _git(repo, "reset", "--hard", "HEAD")
        (repo / "keep.txt").write_text("main_side\n", encoding="utf-8")
        _git(repo, "add", "keep.txt")
        _git(repo, "commit", "-m", "main_edit")
        _git(repo, "checkout", "-b", "side", "HEAD~1")
        (repo / "keep.txt").write_text("branch_side\n", encoding="utf-8")
        _git(repo, "add", "keep.txt")
        _git(repo, "commit", "-m", "side_edit")
        merge_proc = subprocess.run(
            ["git", "-C", str(repo), "merge", "main"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert merge_proc.returncode != 0
        st_conf = httpx.get(f"{srv.base_url}/api/v1/projects/{pid}/git/status", headers=hdrs).json()
        conf_entry = next(e for e in st_conf["entries"] if e["path"] == "keep.txt")
        assert conf_entry["index_status"] == "U" and conf_entry["worktree_status"] == "U"


def test_review_readonly_and_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify Git review does not mutate HEAD/refs/index/worktree and enforces timeout and size caps."""
    repo = tmp_path / "repo-ro"
    _init_repo(repo)
    (repo / "big.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "big.txt")
    _git(repo, "commit", "-m", "init")

    # Add >200 untracked files and >64 KiB diff
    for idx in range(205):
        (repo / f"f_{idx:03d}.txt").write_text("x\n", encoding="utf-8")
    (repo / "big.txt").write_text(("A" * 100 + "\n") * 900, encoding="utf-8")

    before_head = (repo / ".git" / "HEAD").read_bytes()
    before_index_sha = hashlib.sha256((repo / ".git" / "index").read_bytes()).hexdigest()
    before_big = (repo / "big.txt").read_bytes()
    before_refs = _git(repo, "show-ref")

    cfg = make_project_host_config(tmp_path, allowed_roots=(repo,))
    with LiveTcpTestServer(config=cfg) as srv:
        hdrs = auth_headers(cfg)
        pid = _register_project(srv.base_url, hdrs, repo)

        st = httpx.get(f"{srv.base_url}/api/v1/projects/{pid}/git/status", headers=hdrs).json()
        assert st["state"] == "ready"
        assert st["truncated"] is True
        assert len(st["entries"]) == project_review_mod.GIT_MAX_STATUS_ENTRIES

        real_run_git = project_review_mod._run_git
        captured_git_results: list[tuple[list[str], object]] = []

        def _spy_run_git(git_exe: str, root: Path, args: list[str], **kwargs: object) -> object:
            res = real_run_git(git_exe, root, args, **kwargs)
            captured_git_results.append((list(args), res))
            return res

        monkeypatch.setattr(project_review_mod, "_run_git", _spy_run_git)
        df = httpx.get(
            f"{srv.base_url}/api/v1/projects/{pid}/git/diff",
            headers=hdrs,
            params={"path": "big.txt", "area": "worktree"},
        ).json()
        assert df["kind"] == "text"
        assert df["truncated"] is True
        assert len(df["patch"].encode("utf-8")) <= project_review_mod.GIT_MAX_PATCH_BYTES
        diff_calls = [res for args, res in captured_git_results if args and args[0] == "diff"]
        assert len(diff_calls) == 1
        assert getattr(diff_calls[0], "stdout_truncated", False) is True
        assert len(getattr(diff_calls[0], "stdout", b"")) <= project_review_mod.GIT_MAX_PATCH_BYTES + 1

        assert (repo / ".git" / "HEAD").read_bytes() == before_head
        assert hashlib.sha256((repo / ".git" / "index").read_bytes()).hexdigest() == before_index_sha
        assert (repo / "big.txt").read_bytes() == before_big
        assert _git(repo, "show-ref") == before_refs

        # Timeout simulation -> unavailable
        def _raise_timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
            raise subprocess.TimeoutExpired(cmd="git", timeout=5.0)

        monkeypatch.setattr(project_review_mod, "_run_git", _raise_timeout)
        st_to = httpx.get(f"{srv.base_url}/api/v1/projects/{pid}/git/status", headers=hdrs).json()
        assert st_to["state"] == "unavailable"


def test_unavailable_and_exclusions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify non-repo, missing git, unsupported layouts, and secret/excluded path non-leakage."""
    non_repo = tmp_path / "plain"
    non_repo.mkdir()
    (non_repo / "note.txt").write_text("plain\n", encoding="utf-8")

    # Repo with nested child registered (parent git -> unsupported_layout)
    parent_repo = tmp_path / "parent"
    _init_repo(parent_repo)
    child_dir = parent_repo / "sub"
    child_dir.mkdir()
    (child_dir / "ok.txt").write_text("ok\n", encoding="utf-8")

    # Repo with .git file (unsupported_layout)
    git_file_dir = tmp_path / "gitfile"
    git_file_dir.mkdir()
    (git_file_dir / ".git").write_text("gitdir: ../elsewhere\n", encoding="utf-8")

    # Repo with excluded secret files
    sec_repo = tmp_path / "secrets"
    _init_repo(sec_repo)
    (sec_repo / "visible.txt").write_text("public\n", encoding="utf-8")
    (sec_repo / ".env").write_text("API_KEY=DO_NOT_LEAK\n", encoding="utf-8")
    (sec_repo / "credentials.json").write_text("SECRET_CRED=DO_NOT_LEAK\n", encoding="utf-8")
    _git(sec_repo, "add", "visible.txt", ".env", "credentials.json")
    _git(sec_repo, "commit", "-m", "sec")
    (sec_repo / ".env").write_text("API_KEY=LEAK_EDITED\n", encoding="utf-8")

    # Unsafe repo placeholder registered in allowed_roots
    unsafe_repo = tmp_path / "unsafe_repo"
    _init_repo(unsafe_repo)

    cfg = make_project_host_config(
        tmp_path, allowed_roots=(non_repo, child_dir, git_file_dir, sec_repo, unsafe_repo)
    )
    with LiveTcpTestServer(config=cfg) as srv:
        hdrs = auth_headers(cfg)
        pid_plain = _register_project(srv.base_url, hdrs, non_repo, "plain")
        pid_child = _register_project(srv.base_url, hdrs, child_dir, "child")
        pid_gitfile = _register_project(srv.base_url, hdrs, git_file_dir, "gitfile")
        pid_sec = _register_project(srv.base_url, hdrs, sec_repo, "sec")

        st_plain = httpx.get(
            f"{srv.base_url}/api/v1/projects/{pid_plain}/git/status", headers=hdrs
        ).json()
        assert st_plain["state"] == "not_repository"

        st_child = httpx.get(
            f"{srv.base_url}/api/v1/projects/{pid_child}/git/status", headers=hdrs
        ).json()
        assert st_child["state"] == "unsupported_layout"

        st_gitfile = httpx.get(
            f"{srv.base_url}/api/v1/projects/{pid_gitfile}/git/status", headers=hdrs
        ).json()
        assert st_gitfile["state"] == "unsupported_layout"

        # Secret files excluded from status and refused in diff
        st_sec = httpx.get(
            f"{srv.base_url}/api/v1/projects/{pid_sec}/git/status", headers=hdrs
        ).json()
        assert st_sec["state"] == "ready"
        sec_paths = [e["path"] for e in st_sec["entries"]]
        assert ".env" not in sec_paths
        assert "credentials.json" not in sec_paths

        d_sec = httpx.get(
            f"{srv.base_url}/api/v1/projects/{pid_sec}/git/diff",
            headers=hdrs,
            params={"path": ".env", "area": "worktree"},
        )
        assert d_sec.status_code == 403
        assert "DO_NOT_LEAK" not in d_sec.text
        assert "LEAK_EDITED" not in d_sec.text

        # Directory (existing and deleted) and wildcard targets are rejected in git/diff for both areas
        (sec_repo / "sub").mkdir()
        (sec_repo / "sub" / "item.txt").write_text("sub\n", encoding="utf-8")
        (sec_repo / "deldir").mkdir()
        (sec_repo / "deldir" / "gone.txt").write_text("gone\n", encoding="utf-8")
        _git(sec_repo, "add", "deldir/gone.txt")
        _git(sec_repo, "commit", "-m", "add deldir")
        (sec_repo / "deldir" / "gone.txt").unlink()
        (sec_repo / "deldir").rmdir()
        for bad_path in ("sub", "deldir", "*.txt"):
            for area in ("index", "worktree"):
                assert (
                    httpx.get(
                        f"{srv.base_url}/api/v1/projects/{pid_sec}/git/diff",
                        headers=hdrs,
                        params={"path": bad_path, "area": area},
                    ).status_code
                    == 400
                )

        # Unsafe local .git/config ([core] fsmonitor, [filter.evil], malformed) refuses execution before spawning git
        (unsafe_repo / "ok.txt").write_text("v1\n", encoding="utf-8")
        _git(unsafe_repo, "add", "ok.txt")
        _git(unsafe_repo, "commit", "-m", "base")
        (unsafe_repo / "ok.txt").write_text("v2\n", encoding="utf-8")
        cfg_file = unsafe_repo / ".git" / "config"
        orig_cfg_text = cfg_file.read_text(encoding="utf-8")

        git_invocations: list[list[str]] = []
        real_run_git = project_review_mod._run_git

        def _record_git(git_exe: str, root: Path, args: list[str], **kwargs: object) -> object:
            git_invocations.append(list(args))
            return real_run_git(git_exe, root, args, **kwargs)

        monkeypatch.setattr(project_review_mod, "_run_git", _record_git)
        pid_unsafe = _register_project(srv.base_url, hdrs, unsafe_repo, "unsafe")
        for bad_snippet in (
            '\n[core]\n\tfsmonitor = "python -c pass"\n',
            '\n[filter.evil]\n\tclean = "python -c pass"\n',
            '\n[include]\n\tpath = "../other"\n',
            '\n[unclosed\n',
        ):
            cfg_file.write_text(orig_cfg_text + bad_snippet, encoding="utf-8")
            before_cfg = cfg_file.read_bytes()
            before_head = (unsafe_repo / ".git" / "HEAD").read_bytes()
            git_invocations.clear()
            st_unsafe = httpx.get(
                f"{srv.base_url}/api/v1/projects/{pid_unsafe}/git/status", headers=hdrs
            ).json()
            assert st_unsafe["state"] == "unavailable"
            df_unsafe = httpx.get(
                f"{srv.base_url}/api/v1/projects/{pid_unsafe}/git/diff",
                headers=hdrs,
                params={"path": "ok.txt", "area": "worktree"},
            ).json()
            assert df_unsafe["kind"] == "unavailable"
            assert git_invocations == []
            assert cfg_file.read_bytes() == before_cfg
            assert (unsafe_repo / ".git" / "HEAD").read_bytes() == before_head

        # Reparse/symlink .git/objects is rejected before running git
        cfg_file.write_text(orig_cfg_text, encoding="utf-8")
        real_reparse = project_review_mod.is_reparse_or_symlink
        monkeypatch.setattr(
            project_review_mod,
            "is_reparse_or_symlink",
            lambda p: True if Path(p).name == "objects" and Path(p).parent.name == ".git" else real_reparse(p),
        )
        git_invocations.clear()
        st_obj = httpx.get(
            f"{srv.base_url}/api/v1/projects/{pid_unsafe}/git/status", headers=hdrs
        ).json()
        assert st_obj["state"] == "unsupported_layout"
        assert git_invocations == []
        monkeypatch.setattr(project_review_mod, "is_reparse_or_symlink", real_reparse)

        # Metadata failure / truncation stops immediately with zero subsequent diff calls
        def _fail_ls_tree(git_exe: str, root: Path, args: list[str], **kwargs: object) -> object:
            git_invocations.append(list(args))
            if args and args[0] == "ls-tree":
                return project_review_mod._GitCommandResult(
                    returncode=1, stdout=b"", stderr=b"bad tree", stdout_truncated=False, stderr_truncated=False
                )
            return real_run_git(git_exe, root, args, **kwargs)

        monkeypatch.setattr(project_review_mod, "_run_git", _fail_ls_tree)
        git_invocations.clear()
        df_meta_fail = httpx.get(
            f"{srv.base_url}/api/v1/projects/{pid_unsafe}/git/diff",
            headers=hdrs,
            params={"path": "ok.txt", "area": "worktree"},
        ).json()
        assert df_meta_fail["kind"] == "unavailable"
        assert not any(call and call[0] == "diff" for call in git_invocations)

        def _trunc_ls_files(git_exe: str, root: Path, args: list[str], **kwargs: object) -> object:
            git_invocations.append(list(args))
            if args and args[0] == "ls-files":
                return project_review_mod._GitCommandResult(
                    returncode=0, stdout=b"ok.txt", stderr=b"", stdout_truncated=True, stderr_truncated=False
                )
            return real_run_git(git_exe, root, args, **kwargs)

        monkeypatch.setattr(project_review_mod, "_run_git", _trunc_ls_files)
        git_invocations.clear()
        df_meta_trunc = httpx.get(
            f"{srv.base_url}/api/v1/projects/{pid_unsafe}/git/diff",
            headers=hdrs,
            params={"path": "ok.txt", "area": "index"},
        ).json()
        assert df_meta_trunc["kind"] == "unavailable"
        assert not any(call and call[0] == "diff" for call in git_invocations)

        # Controlled HEAD / branch ref checks under the same branch name (refs/heads/main)
        def _run_head_case(*, show_ref_rc: int, show_ref_stderr: bytes = b"") -> tuple[dict[str, object], list[list[str]]]:
            calls: list[list[str]] = []

            def _mock_head(git_exe: str, root: Path, args: list[str], **kwargs: object) -> object:
                calls.append(list(args))
                if args == ["rev-parse", "--verify", "HEAD"]:
                    return project_review_mod._GitCommandResult(128, b"", b"fatal: Needed a single revision", False, False)
                if args == ["symbolic-ref", "--quiet", "HEAD"]:
                    return project_review_mod._GitCommandResult(0, b"refs/heads/main\n", b"", False, False)
                if args == ["show-ref", "--verify", "--quiet", "refs/heads/main"]:
                    return project_review_mod._GitCommandResult(show_ref_rc, b"", show_ref_stderr, False, False)
                return real_run_git(git_exe, root, args, **kwargs)

            monkeypatch.setattr(project_review_mod, "_run_git", _mock_head)
            resp_json = httpx.get(
                f"{srv.base_url}/api/v1/projects/{pid_unsafe}/git/diff",
                headers=hdrs,
                params={"path": "ok.txt", "area": "index"},
            ).json()
            return resp_json, calls

        # 1) Missing branch ref (rc=1, empty stderr) -> valid unborn initial repo, proceeds to diff
        unborn_df, unborn_calls = _run_head_case(show_ref_rc=1)
        assert unborn_df["kind"] == "text"
        assert any(call and call[0] == "diff" for call in unborn_calls)

        # 2) Branch ref exists (rc=0) but HEAD unparseable -> unavailable, zero diff calls
        exists_df, exists_calls = _run_head_case(show_ref_rc=0)
        assert exists_df["kind"] == "unavailable"
        assert not any(call and call[0] == "diff" for call in exists_calls)

        # 3) Branch ref existence uncertain (rc=128 / stderr) -> unavailable, zero diff calls
        uncertain_df, uncertain_calls = _run_head_case(show_ref_rc=128, show_ref_stderr=b"fatal: bad ref")
        assert uncertain_df["kind"] == "unavailable"
        assert not any(call and call[0] == "diff" for call in uncertain_calls)

        monkeypatch.setattr(project_review_mod, "_run_git", real_run_git)

        # Missing git executable -> unavailable
        monkeypatch.setattr(project_review_mod, "_resolve_trusted_git_executable", lambda: None)
        st_no_git = httpx.get(
            f"{srv.base_url}/api/v1/projects/{pid_sec}/git/status", headers=hdrs
        ).json()
        assert st_no_git["state"] == "unavailable"
