"""U01–U12: Real Chromium frontend tests against controlled HTTP/SSE barriers for app.js."""

from __future__ import annotations

import json
import threading
import uuid
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Self
from urllib.parse import urlparse

from app_helpers import FAKE_TEST_TOKEN
from forecontract_app.models import ProjectView, SessionView
from forecontract_app.security import SECURITY_RESPONSE_HEADERS
from forecontract_app.session_models import SessionContextView
from playwright.sync_api import expect, sync_playwright

_STATIC_DIR = (
    Path(__file__).resolve().parents[2] / "apps" / "local" / "forecontract_app" / "static"
)


class _StaticFrontendServer:
    """Serve the real static index.html/style.css/app.js over loopback HTTP."""

    def __init__(self) -> None:
        handler = partial(_StaticHandler)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = int(self._httpd.server_address[1])
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5.0)


class _StaticHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        mapping = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "application/javascript; charset=utf-8"),
            "/style.css": ("style.css", "text/css; charset=utf-8"),
        }
        if path in mapping:
            fname, ctype = mapping[path]
            body = (_STATIC_DIR / fname).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in SECURITY_RESPONSE_HEADERS.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)
            return
        body = b'{"error":{"code":"NOT_FOUND","message":"not found"}}'
        self.send_response(404)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        _ = (format, args)


def _uid(n: int) -> str:
    return f"00000000-0000-4000-8000-{n:012d}"


def _make_project(pid: str, name: str, kind: str = "controlled") -> dict[str, Any]:
    return {
        "project_id": pid,
        "name": name,
        "workspace_root": f"D:/mock/{name}",
        "read_paths": ["src/"] if kind == "existing" else ["metrics.py"],
        "write_paths": [] if kind == "existing" else [{"path": "metrics.py", "operation": "modify"}],
        "commands_summary": [],
        "expected_tests": [],
        "kind": kind,
        "read_grant_id": _uid(900) if kind == "existing" else None,
    }


def _make_session(
    sid: str,
    pid: str,
    *,
    active_run_id: str | None = None,
    model_name: str = "offline-metrics-v1",
    rev: int = 0,
    constraints: str = "",
) -> dict[str, Any]:
    return {
        "session_id": sid,
        "project_id": pid,
        "model": {"provider": "offline", "model": model_name, "thinking": "disabled"},
        "active_run_id": active_run_id,
        "constraints_revision": rev,
        "constraints": constraints,
        "history_cursor": 0,
    }


def _make_context(
    sid: str,
    pid: str,
    *,
    rev: int = 0,
    constraints: str = "",
    cursor: int = 0,
) -> dict[str, Any]:
    return {
        "session_id": sid,
        "project_id": pid,
        "revision": rev,
        "constraints": constraints,
        "history_cursor": cursor,
        "storage_used_bytes": 40960,
        "storage_limit_bytes": 134217728,
    }


def _make_run(
    rid: str,
    sid: str,
    pid: str,
    *,
    state: str = "running",
    verification: str = "not_run",
    reason_code: str | None = None,
    last_seq: int = 0,
    truncated_before: int = 0,
    result: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "run_id": rid,
        "session_id": sid,
        "project_id": pid,
        "client_request_id": _uid(999),
        "goal": "goal text",
        "state": state,
        "verification": verification,
        "reason_code": reason_code,
        "last_seq": last_seq,
        "result": result,
        "truncated_before": truncated_before,
        "usage": usage,
    }


def _sse_body(events: list[dict[str, Any]], *, resync: bool = False) -> str:
    if resync:
        return 'event: resync_required\ndata: {"reason":"slow_subscriber"}\n\n'
    chunks = []
    for ev in events:
        chunks.append(f"id: {ev['seq']}\nevent: {ev['kind']}\ndata: {json.dumps(ev)}\n\n")
    return "".join(chunks)


def _attach_watchers(page: Any, *, base_url: str, allow_error_substrings: tuple[str, ...] = ()) -> tuple[list[str], list[str], list[str], list[dict[str, Any]]]:
    page_errors: list[str] = []
    console_errors: list[str] = []
    non_loopback: list[str] = []
    requests: list[dict[str, Any]] = []

    page.on("pageerror", lambda err: page_errors.append(str(err)))

    def _on_console(msg: Any) -> None:
        if msg.type == "error":
            text = msg.text
            if not any(tok in text for tok in allow_error_substrings):
                console_errors.append(text)

    page.on("console", _on_console)

    def _on_req(req: Any) -> None:
        if not req.url.startswith(f"{base_url}/"):
            non_loopback.append(req.url)
        post_json = None
        if req.post_data:
            try:
                post_json = req.post_data_json
            except Exception:  # noqa: BLE001
                post_json = req.post_data
        requests.append({"method": req.method, "url": req.url, "json": post_json})

    page.on("request", _on_req)
    return page_errors, console_errors, non_loopback, requests


def test_u01_project_session_navigation() -> None:
    p_id, q_id = _uid(1), _uid(2)
    sp1, sp2, sq1, sq2 = _uid(11), _uid(12), _uid(21), _uid(22)
    run_p1 = _uid(101)
    projects = [_make_project(p_id, "proj-p"), _make_project(q_id, "proj-q")]
    sessions = [
        _make_session(sp1, p_id, active_run_id=run_p1),
        _make_session(sp2, p_id),
        _make_session(sq1, q_id),
        _make_session(sq2, q_id),
    ]

    with _StaticFrontendServer() as srv, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page_errors, console_errors, non_loopback, reqs = _attach_watchers(
                page, base_url=srv.base_url
            )

            def _route(route: Any) -> None:
                url = route.request.url
                path = url.split("://" + url.split("/")[2], 1)[1].split("?", 1)[0]
                if path == "/api/v1/projects":
                    route.fulfill(status=200, json=projects)
                elif path == "/api/v1/sessions":
                    route.fulfill(status=200, json=sessions)
                elif path == f"/api/v1/sessions/{sp1}/context":
                    route.fulfill(status=200, json=_make_context(sp1, p_id))
                elif path == f"/api/v1/sessions/{sp1}/history":
                    route.fulfill(status=200, json={"session_id": sp1, "items": [], "cursor": 0, "has_more": False})
                elif path == f"/api/v1/sessions/{sp1}/runs/{run_p1}/snapshot":
                    route.fulfill(
                        status=200,
                        json={
                            "run": _make_run(run_p1, sp1, p_id, state="running"),
                            "events": [],
                            "cursor": 0,
                            "truncated_before": 0,
                            "pending_proposals": [
                                {
                                    "id": _uid(301),
                                    "kind": "scope",
                                    "project_id": p_id,
                                    "session_id": sp1,
                                    "run_id": run_p1,
                                    "digest": "a" * 64,
                                    "payload": {"exact_paths": ["metrics.py"], "operations": ["modify"], "verification_plan_summary": "p"},
                                    "state": "pending",
                                    "expires_at": 999999.0,
                                }
                            ],
                            "pending_questions": [],
                        },
                    )
                elif path == f"/api/v1/sessions/{sp1}/runs/{run_p1}/proposals":
                    route.fulfill(
                        status=200,
                        json=[
                            {
                                "id": _uid(301),
                                "kind": "scope",
                                "project_id": p_id,
                                "session_id": sp1,
                                "run_id": run_p1,
                                "digest": "a" * 64,
                                "payload": {"exact_paths": ["metrics.py"], "operations": ["modify"], "verification_plan_summary": "p"},
                                "state": "pending",
                                "expires_at": 999999.0,
                            }
                        ],
                    )
                elif path == f"/api/v1/sessions/{sp1}/runs/{run_p1}/questions":
                    route.fulfill(status=200, json=[])
                elif path == f"/api/v1/sessions/{sp1}/runs/{run_p1}/usage":
                    route.fulfill(status=200, json={"calls_started": 1, "calls_completed": 0, "usage_known_calls": 0, "usage_unknown_calls": 0, "known_input_tokens": 0, "known_output_tokens": 0, "input_tokens": 0, "output_tokens": 0, "max_calls": 8, "steps_used": 1, "max_steps": 16, "elapsed_ms": 10, "run_timeout_ms": 60000, "estimated_cost_cny": None, "billed_cost_cny": None, "cost_status": "offline_not_billed"})
                elif path == f"/api/v1/sessions/{sp1}/runs/{run_p1}/events":
                    route.fulfill(status=200, content_type="text/event-stream", body=": heartbeat\n\n")
                else:
                    route.fulfill(status=200, json={})

            page.route("**/api/**", _route)
            page.goto(f"{srv.base_url}/#token={FAKE_TEST_TOKEN}", wait_until="domcontentloaded")

            # Selecting P displays only P's 2 sessions and active run_p1 + pending card
            expect(page.get_by_test_id("current-project")).to_have_text(p_id)
            expect(page.get_by_test_id("current-session")).to_have_text(sp1)
            expect(page.get_by_test_id("run-id")).to_have_text(run_p1)
            expect(page.locator('[data-testid^="session-item-"]')).to_have_count(2)
            expect(page.get_by_test_id(f"session-item-{sq1}")).to_have_count(0)
            expect(page.get_by_test_id(f"proposal-item-{_uid(301)}")).to_be_visible()

            # Switching to Q clears P's run & cards and lists only Q's 2 sessions
            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id(f"project-item-{q_id}").click()
            expect(page.get_by_test_id("current-project")).to_have_text(q_id)
            expect(page.get_by_test_id("current-session")).to_have_text("-")
            expect(page.get_by_test_id("run-id")).to_have_text("-")
            expect(page.get_by_test_id("run-state")).to_have_text("idle")
            expect(page.locator('[data-testid^="proposal-item-"]')).to_have_count(0)
            expect(page.locator('[data-testid^="session-item-"]')).to_have_count(2)
            expect(page.get_by_test_id(f"session-item-{sq1}")).to_be_visible()
            expect(page.get_by_test_id(f"session-item-{sq2}")).to_be_visible()
            expect(page.get_by_test_id(f"session-item-{sp1}")).to_have_count(0)

            assert not [r for r in reqs if r["method"] in {"POST", "PUT"}]
            assert page_errors == []
            assert console_errors == []
            assert non_loopback == []
        finally:
            browser.close()


def test_u02_stale_async_responses() -> None:
    p_id = _uid(1)
    sess_a, sess_b = _uid(11), _uid(12)
    run_a, run_b = _uid(101), _uid(102)
    prop_a, prop_b = _uid(301), _uid(302)
    q_a = _uid(401)
    cmd_a = "cmd-a"
    projects = [_make_project(p_id, "proj-stale")]
    sessions = [
        _make_session(sess_a, p_id, active_run_id=run_a, rev=1, constraints="OLD_A"),
        _make_session(sess_b, p_id, active_run_id=run_b, rev=5, constraints="KEEP_B"),
    ]
    ProjectView.model_validate(projects[0])
    for s_item in sessions:
        SessionView.model_validate(s_item)

    with _StaticFrontendServer() as srv, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page_errors, _console_errors, non_loopback, reqs = _attach_watchers(
                page, base_url=srv.base_url, allow_error_substrings=("500", "403")
            )
            held_a: list[tuple[str, Any]] = []
            held_snap_a: list[tuple[str, Any]] = []
            hold_cmd_a = [True]
            first_snap_a_done = [False]
            hold_snap_a_mode = [False]
            switch_count = [0]

            def _route(route: Any) -> None:
                path = urlparse(route.request.url).path
                if path == "/api/v1/projects":
                    route.fulfill(status=200, json=projects)
                elif path == "/api/v1/sessions":
                    route.fulfill(status=200, json=sessions)
                # Hold ALL session A reads after first SSE triggers command or hold snapshot explicitly
                elif f"/sessions/{sess_a}/" in path:
                    if path.endswith("/snapshot"):
                        if hold_snap_a_mode[0]:
                            held_snap_a.append((path, route))
                            return
                        if not first_snap_a_done[0]:
                            first_snap_a_done[0] = True
                            route.fulfill(
                                status=200,
                                json={
                                    "run": _make_run(run_a, sess_a, p_id, state="running"),
                                    "events": [
                                        {
                                            "schema_version": "1",
                                            "project_id": p_id,
                                            "session_id": sess_a,
                                            "run_id": run_a,
                                            "seq": 1,
                                            "kind": "command_started",
                                            "payload": {"command_id": cmd_a, "state": "running"},
                                        }
                                    ],
                                    "cursor": 1,
                                    "truncated_before": 0,
                                    "pending_proposals": [],
                                    "pending_questions": [],
                                },
                            )
                            return
                    if hold_snap_a_mode[0]:
                        if path.endswith("/context"):
                            route.fulfill(status=200, json=_make_context(sess_a, p_id, rev=1, constraints="OLD_A"))
                        elif path.endswith("/history"):
                            route.fulfill(status=200, json={"session_id": sess_a, "items": [], "cursor": 0, "has_more": False})
                        elif path.endswith(("/proposals", "/questions")):
                            route.fulfill(status=200, json=[])
                        elif path.endswith("/usage"):
                            route.fulfill(status=200, json={"calls_started": 0, "calls_completed": 0, "usage_known_calls": 0, "usage_unknown_calls": 0, "known_input_tokens": 0, "known_output_tokens": 0, "input_tokens": 0, "output_tokens": 0, "max_calls": 8, "steps_used": 0, "max_steps": 16, "elapsed_ms": 0, "run_timeout_ms": 60000, "estimated_cost_cny": None, "billed_cost_cny": None, "cost_status": "offline_not_billed"})
                        else:
                            route.fulfill(status=200, json={})
                        return
                    held_a.append((path, route))
                    return
                elif path == f"/api/v1/sessions/{sess_b}/context":
                    route.fulfill(status=200, json=_make_context(sess_b, p_id, rev=5, constraints="KEEP_B", cursor=2))
                elif path == f"/api/v1/sessions/{sess_b}/history":
                    route.fulfill(status=200, json={"session_id": sess_b, "items": [{"seq": 1, "item_id": _uid(51), "project_id": p_id, "session_id": sess_b, "run_id": run_b, "kind": "user_goal", "text": "B_GOAL", "source_ids": [run_b], "truncated": False, "sha256": "b" * 64}], "cursor": 1, "has_more": False})
                elif path == f"/api/v1/sessions/{sess_b}/runs/{run_b}/snapshot":
                    route.fulfill(
                        status=200,
                        json={
                            "run": _make_run(run_b, sess_b, p_id, state="running"),
                            "events": [],
                            "cursor": 0,
                            "truncated_before": 0,
                            "pending_proposals": [],
                            "pending_questions": [],
                        },
                    )
                elif path == f"/api/v1/sessions/{sess_b}/runs/{run_b}/proposals":
                    route.fulfill(
                        status=200,
                        json=[{"id": prop_b, "kind": "scope", "project_id": p_id, "session_id": sess_b, "run_id": run_b, "digest": "b" * 64, "payload": {"exact_paths": ["b.py"], "operations": ["modify"], "verification_plan_summary": "pb"}, "state": "pending", "expires_at": 999999.0}],
                    )
                elif path == f"/api/v1/sessions/{sess_b}/runs/{run_b}/questions":
                    route.fulfill(status=200, json=[])
                elif path == f"/api/v1/sessions/{sess_b}/runs/{run_b}/usage":
                    route.fulfill(status=200, json={"calls_started": 2, "calls_completed": 2, "usage_known_calls": 2, "usage_unknown_calls": 0, "known_input_tokens": 50, "known_output_tokens": 25, "input_tokens": 50, "output_tokens": 25, "max_calls": 8, "steps_used": 2, "max_steps": 16, "elapsed_ms": 50, "run_timeout_ms": 60000, "estimated_cost_cny": None, "billed_cost_cny": None, "cost_status": "offline_not_billed"})
                elif path == f"/api/v1/sessions/{sess_b}/runs/{run_b}":
                    route.fulfill(status=200, json=_make_run(run_b, sess_b, p_id, state="running"))
                elif path == f"/api/v1/sessions/{sess_b}/runs/{run_b}/events":
                    # Inject a foreign session_a SSE frame on stream B before heartbeat!
                    body = _sse_body([{"schema_version": "1", "project_id": p_id, "session_id": sess_a, "run_id": run_a, "seq": 99, "kind": "run_finished", "payload": {"state": "failed", "verification": "failed", "reason_code": "STALE_A"}}]) + ": keep\n\n"
                    route.fulfill(status=200, content_type="text/event-stream", body=body)
                else:
                    route.fulfill(status=200, json={})

            page.route("**/api/**", _route)
            page.goto(f"{srv.base_url}/#token={FAKE_TEST_TOKEN}", wait_until="domcontentloaded")

            # Wait until A's context/history/proposals/questions/usage/command are all held
            deadline = 50
            while deadline > 0 and len(held_a) < 6:
                page.wait_for_timeout(40)
                deadline -= 1
            assert any(p.endswith(f"/commands/{cmd_a}") for p, _ in held_a)
            assert any(p.endswith("/context") for p, _ in held_a)
            assert any(p.endswith("/proposals") for p, _ in held_a)
            assert any(p.endswith("/questions") for p, _ in held_a)
            assert any(p.endswith("/usage") for p, _ in held_a)
            assert any(p.endswith("/history") for p, _ in held_a)
            _ = hold_cmd_a

            # Switch to B while A's responses are still pending
            reqs_before_b = len(reqs)
            switch_count[0] += 1
            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id(f"session-item-{sess_b}").click()
            expect(page.get_by_test_id("current-session")).to_have_text(sess_b)
            expect(page.get_by_test_id("run-id")).to_have_text(run_b)
            expect(page.get_by_test_id("run-state")).to_have_text("running")
            expect(page.get_by_test_id("constraints-input")).to_have_value("KEEP_B")
            expect(page.get_by_test_id(f"proposal-item-{prop_b}")).to_be_visible()

            reqs_after_switch_mark = len(reqs)
            # Release ALL stale A responses (including an error response on questions/usage)
            for path, route in held_a:
                if path.endswith("/context"):
                    route.fulfill(status=200, json=_make_context(sess_a, p_id, rev=1, constraints="STALE_OLD_A"))
                elif path.endswith("/history"):
                    route.fulfill(status=200, json={"session_id": sess_a, "items": [{"seq": 1, "item_id": _uid(52), "project_id": p_id, "session_id": sess_a, "run_id": run_a, "kind": "user_goal", "text": "STALE_A_HIST", "source_ids": [run_a], "truncated": False, "sha256": "a" * 64}], "cursor": 1, "has_more": False})
                elif path.endswith("/proposals"):
                    route.fulfill(status=200, json=[{"id": prop_a, "kind": "scope", "project_id": p_id, "session_id": sess_a, "run_id": run_a, "digest": "a" * 64, "payload": {"exact_paths": ["a.py"], "operations": ["modify"], "verification_plan_summary": "pa"}, "state": "pending", "expires_at": 999999.0}])
                elif path.endswith("/questions"):
                    route.fulfill(status=500, json={"error": {"code": "STALE_A_ERR", "message": "stale failure"}})
                elif path.endswith("/usage"):
                    route.fulfill(status=200, json={"calls_started": 7, "calls_completed": 7, "usage_known_calls": 0, "usage_unknown_calls": 7, "known_input_tokens": 0, "known_output_tokens": 0, "input_tokens": None, "output_tokens": None, "max_calls": 8, "steps_used": 7, "max_steps": 16, "elapsed_ms": 999, "run_timeout_ms": 60000, "estimated_cost_cny": None, "billed_cost_cny": None, "cost_status": "offline_not_billed"})
                elif path.endswith(f"/commands/{cmd_a}"):
                    route.fulfill(status=200, json={"command_id": cmd_a, "run_id": run_a, "proposal_digest": "a" * 64, "state": "exited", "pid": 123, "exit_code": 0, "cleanup_ok": True, "remaining_active": 0, "first_seq": 1, "last_seq": 1, "chunks": [{"seq": 1, "stream": "stdout", "text": "STALE_CMD_A"}], "bytes_seen": 11, "truncated": False, "output_delivery_failed": False, "truncated_stdout_bytes": 0, "truncated_stderr_bytes": 0, "timed_out": False})
                else:
                    route.fulfill(status=403, json={"error": {"code": "STALE_ERR", "message": "stale"}})

            page.wait_for_timeout(150)

            # Assert B's identity, UI, draft, sessionStorage, errors, and request log are 100% preserved
            expect(page.get_by_test_id("current-session")).to_have_text(sess_b)
            expect(page.get_by_test_id("run-id")).to_have_text(run_b)
            expect(page.get_by_test_id("run-state")).to_have_text("running")
            expect(page.get_by_test_id("constraints-input")).to_have_value("KEEP_B")
            expect(page.get_by_test_id("context-revision")).to_contain_text("rev=5")
            expect(page.get_by_test_id("session-history")).not_to_contain_text("STALE_A_HIST")
            expect(page.get_by_test_id("proposal-list")).not_to_contain_text(prop_a)
            expect(page.get_by_test_id("command-list")).not_to_contain_text("STALE_CMD_A")
            expect(page.get_by_test_id("question-list")).not_to_contain_text(q_a)
            expect(page.get_by_test_id("error")).to_have_text("")
            expect(page.get_by_test_id("usage-summary")).to_contain_text("calls started=2")
            assert page.evaluate("sessionStorage.getItem('forecontract_active_session_id')") == sess_b
            assert not [r for r in reqs[reqs_after_switch_mark:] if r["method"] in {"POST", "PUT"}]
            _ = reqs_before_b

            # F02: A's /snapshot request must actually enter held_snap_a, then release 200 and 500 after switching to B
            hold_snap_a_mode[0] = True
            for release_kind in ("ok", "err"):
                held_snap_a.clear()
                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id(f"session-item-{sess_a}").click()
                snap_deadline = 50
                while snap_deadline > 0 and not any(p.endswith("/snapshot") for p, _ in held_snap_a):
                    page.wait_for_timeout(40)
                    snap_deadline -= 1
                assert any(p.endswith("/snapshot") for p, _ in held_snap_a), "A snapshot request must enter held set"

                page.get_by_test_id("nav-projects").click()
                page.get_by_test_id(f"session-item-{sess_b}").click()
                expect(page.get_by_test_id("current-session")).to_have_text(sess_b)
                expect(page.get_by_test_id("run-id")).to_have_text(run_b)
                expect(page.get_by_test_id("constraints-input")).to_have_value("KEEP_B")
                expect(page.get_by_test_id("error")).to_have_text("")

                for path, r_held in held_snap_a:
                    if path.endswith("/snapshot") and release_kind == "ok":
                        r_held.fulfill(
                            status=200,
                            json={
                                "run": _make_run(run_a, sess_a, p_id, state="failed", verification="failed", reason_code="STALE_SNAP_A"),
                                "events": [],
                                "cursor": 0,
                                "truncated_before": 0,
                                "pending_proposals": [],
                                "pending_questions": [],
                            },
                        )
                    else:
                        r_held.fulfill(status=500, json={"error": {"code": "STALE_SNAP_ERR", "message": "stale snap error"}})
                page.wait_for_timeout(120)
                expect(page.get_by_test_id("current-session")).to_have_text(sess_b)
                expect(page.get_by_test_id("run-id")).to_have_text(run_b)
                expect(page.get_by_test_id("run-state")).to_have_text("running")
                expect(page.get_by_test_id("constraints-input")).to_have_value("KEEP_B")
                expect(page.get_by_test_id("context-revision")).to_contain_text("rev=5")
                expect(page.get_by_test_id("error")).to_have_text("")
                assert page.evaluate("sessionStorage.getItem('forecontract_active_session_id')") == sess_b

            assert page_errors == []
            assert non_loopback == []
        finally:
            browser.close()


def test_u03_history_pagination_and_selection() -> None:
    p_id, s_id = _uid(1), _uid(11)
    r_old, r_dup, r_active = _uid(101), _uid(102), _uid(103)
    s_null_id = _uid(12)
    r_null_hist = _uid(104)

    items_p1 = []
    for seq in range(1, 51):
        rid = r_old if seq <= 25 else r_dup
        items_p1.append({
            "seq": seq,
            "item_id": _uid(1000 + seq),
            "project_id": p_id,
            "session_id": s_id,
            "run_id": rid,
            "kind": "user_goal" if seq % 2 else "run_result",
            "text": f"entry-{seq}",
            "source_ids": [rid],
            "truncated": False,
            "sha256": "a" * 64,
        })
    items_p2 = [{
        "seq": 51,
        "item_id": _uid(1051),
        "project_id": p_id,
        "session_id": s_id,
        "run_id": r_dup,
        "kind": "run_result",
        "text": "entry-51",
        "source_ids": [r_dup],
        "truncated": False,
        "sha256": "a" * 64,
    }]

    projects = [_make_project(p_id, "proj-hist")]
    sessions = [
        _make_session(s_id, p_id, active_run_id=r_active),
        _make_session(s_null_id, p_id, active_run_id=None),
    ]

    with _StaticFrontendServer() as srv, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page_errors, console_errors, non_loopback, reqs = _attach_watchers(
                page, base_url=srv.base_url, allow_error_substrings=("500",)
            )
            page2_mode = ["ok"]
            null_hist_calls = [0]

            def _route(route: Any) -> None:
                url = route.request.url
                base_path, _, qs = url.split("://" + url.split("/")[2], 1)[1].partition("?")
                if base_path == "/api/v1/projects":
                    route.fulfill(status=200, json=projects)
                elif base_path == "/api/v1/sessions":
                    route.fulfill(status=200, json=sessions)
                elif base_path == f"/api/v1/sessions/{s_id}/context":
                    route.fulfill(status=200, json=_make_context(s_id, p_id, cursor=51))
                elif base_path == f"/api/v1/sessions/{s_id}/history":
                    if "after=50" in qs:
                        route.fulfill(status=200, json={"session_id": s_id, "items": items_p2, "cursor": 51, "has_more": False})
                    else:
                        route.fulfill(status=200, json={"session_id": s_id, "items": items_p1, "cursor": 50, "has_more": True})
                elif base_path == f"/api/v1/sessions/{s_id}/runs/{r_active}/snapshot":
                    route.fulfill(status=200, json={"run": _make_run(r_active, s_id, p_id, state="running"), "events": [], "cursor": 0, "truncated_before": 0, "pending_proposals": [{"id": _uid(301), "kind": "scope", "project_id": p_id, "session_id": s_id, "run_id": r_active, "digest": "a" * 64, "payload": {"exact_paths": ["x.py"], "operations": ["modify"], "verification_plan_summary": "p"}, "state": "pending", "expires_at": 999999.0}], "pending_questions": []})
                elif base_path == f"/api/v1/sessions/{s_id}/runs/{r_old}/snapshot":
                    route.fulfill(status=200, json={"run": _make_run(r_old, s_id, p_id, state="succeeded", verification="passed", reason_code="OK"), "events": [], "cursor": 0, "truncated_before": 0, "pending_proposals": [{"id": _uid(309), "kind": "scope", "project_id": p_id, "session_id": s_id, "run_id": r_old, "digest": "c" * 64, "payload": {"exact_paths": ["x.py"], "operations": ["modify"], "verification_plan_summary": "p"}, "state": "pending", "expires_at": 999999.0}], "pending_questions": []})
                elif base_path.endswith(("/proposals", "/questions")):
                    route.fulfill(status=200, json=[])
                elif base_path.endswith("/usage"):
                    route.fulfill(status=200, json={"calls_started": 0, "calls_completed": 0, "usage_known_calls": 0, "usage_unknown_calls": 0, "known_input_tokens": 0, "known_output_tokens": 0, "input_tokens": 0, "output_tokens": 0, "max_calls": 8, "steps_used": 0, "max_steps": 16, "elapsed_ms": 0, "run_timeout_ms": 60000, "estimated_cost_cny": None, "billed_cost_cny": None, "cost_status": "offline_not_billed"})
                elif base_path.endswith("/events"):
                    route.fulfill(status=200, content_type="text/event-stream", body=": hb\n\n")
                elif base_path == f"/api/v1/sessions/{s_null_id}/context":
                    route.fulfill(status=200, json=_make_context(s_null_id, p_id, cursor=2))
                elif base_path == f"/api/v1/sessions/{s_null_id}/history":
                    null_hist_calls[0] += 1
                    if "after=0" in qs:
                        route.fulfill(status=200, json={"session_id": s_null_id, "items": [{"seq": 1, "item_id": _uid(2001), "project_id": p_id, "session_id": s_null_id, "run_id": r_null_hist, "kind": "user_goal", "text": "null-active", "source_ids": [r_null_hist], "truncated": False, "sha256": "d" * 64}], "cursor": 1, "has_more": True})
                    elif page2_mode[0] == "fail":
                        route.fulfill(status=500, json={"error": {"code": "PAGE2_FAIL", "message": "page fail"}})
                    else:
                        # Non-advancing cursor (cursor=1 on after=1!)
                        route.fulfill(status=200, json={"session_id": s_null_id, "items": [{"seq": 2, "item_id": _uid(2002), "project_id": p_id, "session_id": s_null_id, "run_id": r_null_hist, "kind": "run_result", "text": "p2", "source_ids": [r_null_hist], "truncated": False, "sha256": "d" * 64}], "cursor": 1, "has_more": True})
                elif base_path == f"/api/v1/sessions/{s_null_id}/runs/{r_null_hist}/snapshot":
                    route.fulfill(status=200, json={"run": _make_run(r_null_hist, s_null_id, p_id, state="succeeded", verification="passed", reason_code="OK"), "events": [], "cursor": 0, "truncated_before": 0, "pending_proposals": [], "pending_questions": []})
                else:
                    route.fulfill(status=200, json={})

            page.route("**/api/**", _route)
            page.goto(f"{srv.base_url}/#token={FAKE_TEST_TOKEN}", wait_until="domcontentloaded")

            expect(page.locator('[data-testid^="history-item-"]')).to_have_count(51)
            opt_vals = page.get_by_test_id("run-history-select").evaluate(
                "sel => Array.from(sel.options).map(o => o.value)"
            )
            assert opt_vals == [r_old, r_dup, r_active]

            # Select old terminal run: GET snapshot, no POST/PUT, no active approve buttons
            mark = len(reqs)
            page.get_by_test_id("nav-history").click()
            page.get_by_test_id("run-history-select").select_option(r_old)
            page.get_by_test_id("nav-chat").click()
            expect(page.get_by_test_id("run-id")).to_have_text(r_old)
            expect(page.get_by_test_id("run-state")).to_have_text("succeeded")
            expect(page.locator('[data-testid^="proposal-approve-"]')).to_have_count(0)
            assert not [r for r in reqs[mark:] if r["method"] in {"POST", "PUT"}]

            # Test active_run_id=None + failing page 2 stops reading & marks incomplete
            page2_mode[0] = "fail"
            null_hist_calls[0] = 0
            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id(f"session-item-{s_null_id}").click()
            expect(page.get_by_test_id("context-summary")).to_contain_text("不完整")
            expect(page.get_by_test_id("run-id")).to_have_text(r_null_hist)
            assert null_hist_calls[0] == 2

            # Test non-advancing cursor stops without infinite loop and marks incomplete
            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id(f"session-item-{s_id}").click()
            page2_mode[0] = "stalled_cursor"
            null_hist_calls[0] = 0
            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id(f"session-item-{s_null_id}").click()
            expect(page.get_by_test_id("context-summary")).to_contain_text("不完整")
            page.wait_for_timeout(120)
            assert null_hist_calls[0] == 2
            assert page_errors == []
            assert non_loopback == []
            _ = console_errors
        finally:
            browser.close()


def test_u04_start_single_flight() -> None:
    p_id, q_id = _uid(1), _uid(2)
    sa, sa2, sb = _uid(21), _uid(23), _uid(22)
    new_rid = _uid(105)
    t1, o1 = _uid(201), _uid(202)
    projects = [_make_project(p_id, "proj-one"), _make_project(q_id, "proj-two")]
    sessions = [
        _make_session(sa, p_id, constraints="A_ORIGINAL"),
        _make_session(sa2, p_id, constraints="SA2_KEEP"),
        _make_session(sb, q_id, constraints="B_KEEP"),
    ]

    def _offer_turn(sid: str, tid: str, oid: str, started: bool) -> dict[str, Any]:
        return {
            "turn_id": tid, "project_id": p_id, "session_id": sid,
            "client_request_id": _uid(203), "seq": 1, "status": "completed",
            "intent_mode": "conversation", "user_message": "Fix calc.py",
            "references": [], "resolved_evidence": [], "tool_reads": [],
            "assistant_message": None,
            "execution_offer": {
                "offer_id": oid, "session_id": sid, "turn_id": tid,
                "client_request_id": _uid(203), "goal": "Captured unique goal for U04",
                "summary": "修复 calc.py",
                "status": "started" if started else "pending_confirm",
                "run_id": new_rid if started else None,
                "run_client_request_id": _uid(204),
                "created_at": "2026-11-21T00:00:00Z", "updated_at": "2026-11-21T00:00:00Z",
            },
            "error_code": None, "error_message": None, "provider_mode": "offline",
            "context_truncated": False, "created_at": "2026-11-21T00:00:00Z", "updated_at": "2026-11-21T00:00:00Z",
        }

    with _StaticFrontendServer() as srv, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page_errors, _, non_loopback, reqs = _attach_watchers(
                page, base_url=srv.base_url, allow_error_substrings=("Failed", "net::")
            )
            held_confirm_posts: list[Any] = []
            confirm_mode = ["ok"]
            offer_started = [False]

            def _route(route: Any) -> None:
                url = route.request.url
                path = url.split("://" + url.split("/")[2], 1)[1].split("?", 1)[0]
                m = route.request.method
                if path == "/api/v1/projects":
                    route.fulfill(status=200, json=projects)
                elif path == "/api/v1/sessions" and m == "GET":
                    route.fulfill(status=200, json=sessions)
                elif path.endswith("/chat/turns") and m == "GET":
                    if f"/sessions/{sa}/" in path:
                        route.fulfill(status=200, json={"session_id": sa, "items": [_offer_turn(sa, t1, o1, offer_started[0])], "next_after_seq": 1, "has_more": False})
                    else:
                        route.fulfill(status=200, json={"session_id": sb, "items": [], "next_after_seq": 0, "has_more": False})
                elif path.endswith("/confirm") and m == "POST":
                    if confirm_mode[0] == "abort":
                        route.abort("failed")
                    else:
                        held_confirm_posts.append(route)
                elif path.endswith("/context"):
                    sid = sa if f"/sessions/{sa}/" in path else (sa2 if f"/sessions/{sa2}/" in path else sb)
                    pid = q_id if sid == sb else p_id
                    cons = "SA2_KEEP" if sid == sa2 else ("B_KEEP" if sid == sb else "A_ORIGINAL")
                    route.fulfill(status=200, json=_make_context(sid, pid, constraints=cons))
                elif path.endswith("/history"):
                    sid = sa if f"/sessions/{sa}/" in path else (sa2 if f"/sessions/{sa2}/" in path else sb)
                    route.fulfill(status=200, json={"session_id": sid, "items": [], "cursor": 0, "has_more": False})
                elif path.endswith("/snapshot"):
                    route.fulfill(status=200, json={"run": _make_run(new_rid, sa, p_id, state="succeeded", verification="passed", reason_code="OK"), "events": [], "cursor": 0, "truncated_before": 0, "pending_proposals": [], "pending_questions": []})
                elif path.endswith(("/proposals", "/questions")):
                    route.fulfill(status=200, json=[])
                elif path.endswith("/usage"):
                    route.fulfill(status=200, json={"calls_started": 0, "calls_completed": 0, "usage_known_calls": 0, "usage_unknown_calls": 0, "known_input_tokens": 0, "known_output_tokens": 0, "input_tokens": 0, "output_tokens": 0, "max_calls": 8, "steps_used": 0, "max_steps": 16, "elapsed_ms": 0, "run_timeout_ms": 60000, "estimated_cost_cny": None, "billed_cost_cny": None, "cost_status": "offline_not_billed"})
                else:
                    route.fulfill(status=200, json={})

            page.route("**/api/**", _route)
            page.goto(f"{srv.base_url}/#token={FAKE_TEST_TOKEN}", wait_until="domcontentloaded")
            page.evaluate(
                """() => {
                    window.__confirmSettled = [];
                    window.__armConfirmBtnSettle = (tid, sid, attempt) => {
                        const btn = document.querySelector(`[data-testid='offer-confirm-btn-${tid}']`);
                        if (!btn || !btn.disabled) {
                            throw new Error('expected disabled confirm button instance');
                        }
                        const protoDesc = Object.getOwnPropertyDescriptor(HTMLButtonElement.prototype, 'disabled');
                        Object.defineProperty(btn, 'disabled', {
                            configurable: true,
                            get() {
                                return protoDesc.get.call(this);
                            },
                            set(val) {
                                protoDesc.set.call(this, val);
                                if (val === false) {
                                    window.__confirmSettled.push({ sessionId: sid, turnId: tid, attempt });
                                    window.dispatchEvent(new CustomEvent('test:confirm-settled', {
                                        detail: { sessionId: sid, turnId: tid, attempt },
                                    }));
                                }
                            },
                        });
                    };
                }"""
            )
            expect(page.get_by_test_id("current-session")).to_have_text(sa)
            page.get_by_test_id(f"offer-confirm-scope-{t1}").check()
            page.evaluate("(tid) => { const b = document.querySelector(`[data-testid='offer-confirm-btn-${tid}']`); b.click(); b.click(); }", t1)
            expect(page.get_by_test_id(f"offer-confirm-btn-{t1}")).to_be_disabled()
            assert len(held_confirm_posts) == 1
            page.evaluate("([tid, sid, att]) => window.__armConfirmBtnSettle(tid, sid, att)", [t1, sa, 1])

            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id(f"project-item-{q_id}").click()
            expect(page.get_by_test_id("current-project")).to_have_text(q_id)
            offer_started[0] = True
            held_confirm_posts.pop(0).fulfill(status=201, json=_make_run(new_rid, sa, p_id, state="queued"))
            page.wait_for_function(
                "([sid, tid, att]) => window.__confirmSettled.some((x) => x.sessionId === sid && x.turnId === tid && x.attempt === att)",
                arg=[sa, t1, 1],
            )
            expect(page.get_by_test_id("current-project")).to_have_text(q_id)

            # Same-project A->B cross-session late-response and late-error isolation (exact finally settle)
            offer_started[0] = False
            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id(f"project-item-{p_id}").click()
            page.get_by_test_id(f"session-item-{sa}").click()
            expect(page.get_by_test_id("current-session")).to_have_text(sa)
            page.get_by_test_id("nav-chat").click()
            page.get_by_test_id(f"offer-confirm-scope-{t1}").check()
            confirm_posts_before_ab = len([r for r in reqs if r["method"] == "POST" and r["url"].endswith("/confirm")])
            page.get_by_test_id(f"offer-confirm-btn-{t1}").click()
            expect(page.get_by_test_id(f"offer-confirm-btn-{t1}")).to_be_disabled()
            assert len(held_confirm_posts) == 1
            page.evaluate("([tid, sid, att]) => window.__armConfirmBtnSettle(tid, sid, att)", [t1, sa, 2])

            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id(f"session-item-{sa2}").click()
            expect(page.get_by_test_id("current-session")).to_have_text(sa2)
            expect(page.get_by_test_id("context-revision")).to_contain_text(f"session={sa2}")
            expect(page.get_by_test_id("constraints-input")).to_have_value("SA2_KEEP")
            held_confirm_posts.pop(0).fulfill(status=201, json=_make_run(new_rid, sa, p_id, state="queued"))
            page.wait_for_function(
                "([sid, tid, att]) => window.__confirmSettled.some((x) => x.sessionId === sid && x.turnId === tid && x.attempt === att)",
                arg=[sa, t1, 2],
            )
            expect(page.get_by_test_id("current-session")).to_have_text(sa2)
            expect(page.get_by_test_id("run-id")).to_have_text("-")
            expect(page.get_by_test_id("error")).to_be_hidden()
            expect(page.get_by_test_id("context-revision")).to_contain_text(f"session={sa2}")
            expect(page.get_by_test_id("constraints-input")).to_have_value("SA2_KEEP")
            assert len([r for r in reqs if r["method"] == "POST" and r["url"].endswith("/confirm")]) == confirm_posts_before_ab + 1

            # Late error isolation from sa -> sa2 within same project
            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id(f"session-item-{sa}").click()
            expect(page.get_by_test_id("current-session")).to_have_text(sa)
            page.get_by_test_id("nav-chat").click()
            page.get_by_test_id(f"offer-confirm-scope-{t1}").check()
            page.get_by_test_id(f"offer-confirm-btn-{t1}").click()
            expect(page.get_by_test_id(f"offer-confirm-btn-{t1}")).to_be_disabled()
            assert len(held_confirm_posts) == 1
            page.evaluate("([tid, sid, att]) => window.__armConfirmBtnSettle(tid, sid, att)", [t1, sa, 3])
            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id(f"session-item-{sa2}").click()
            expect(page.get_by_test_id("current-session")).to_have_text(sa2)
            held_confirm_posts.pop(0).fulfill(
                status=500, json={"error": {"code": "LATE_ERR", "message": "late confirm error"}}
            )
            page.wait_for_function(
                "([sid, tid, att]) => window.__confirmSettled.some((x) => x.sessionId === sid && x.turnId === tid && x.attempt === att)",
                arg=[sa, t1, 3],
            )
            expect(page.get_by_test_id("current-session")).to_have_text(sa2)
            expect(page.get_by_test_id("run-id")).to_have_text("-")
            expect(page.get_by_test_id("error")).to_be_hidden()
            expect(page.get_by_test_id("context-revision")).to_contain_text(f"session={sa2}")
            expect(page.get_by_test_id("constraints-input")).to_have_value("SA2_KEEP")
            assert len([r for r in reqs if r["method"] == "POST" and r["url"].endswith("/confirm")]) == confirm_posts_before_ab + 2

            offer_started[0] = False
            confirm_mode[0] = "abort"
            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id(f"project-item-{p_id}").click()
            page.get_by_test_id(f"session-item-{sa}").click()
            expect(page.get_by_test_id("current-session")).to_have_text(sa)
            page.get_by_test_id("nav-chat").click()
            page.get_by_test_id(f"offer-confirm-scope-{t1}").check()
            confirm_before = len([r for r in reqs if r["method"] == "POST" and r["url"].endswith("/confirm")])
            page.get_by_test_id(f"offer-confirm-btn-{t1}").click()
            expect(page.get_by_test_id("error")).to_contain_text("NETWORK_ERROR")
            expect(page.get_by_test_id(f"offer-confirm-btn-{t1}")).to_be_enabled()
            assert len([r for r in reqs if r["method"] == "POST" and r["url"].endswith("/confirm")]) == confirm_before + 1

            confirm_mode[0] = "ok"
            offer_started[0] = True
            page.get_by_test_id(f"offer-confirm-btn-{t1}").click()
            held_confirm_posts.pop(0).fulfill(status=201, json=_make_run(new_rid, sa, p_id, state="queued"))
            page.get_by_test_id("toggle-inspector").click()
            expect(page.get_by_test_id("run-id")).to_have_text(new_rid)
            assert page_errors == []
            assert non_loopback == []
        finally:
            browser.close()


def test_u05_mutation_single_flight() -> None:
    p_id = _uid(1)
    s1, s2 = _uid(11), _uid(12)
    r1, r2 = _uid(101), _uid(102)
    prop1, q1, cmd1 = _uid(301), _uid(401), "cmd-lock-1"
    projects = [_make_project(p_id, "proj-lock", kind="existing")]
    sessions = [_make_session(s1, p_id, active_run_id=r1), _make_session(s2, p_id, active_run_id=r2)]

    with _StaticFrontendServer() as srv, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page_errors, _, non_loopback, reqs = _attach_watchers(page, base_url=srv.base_url)
            held_mutations: dict[str, list[Any]] = {}

            def _hold(key: str, route: Any) -> None:
                held_mutations.setdefault(key, []).append(route)

            def _route(route: Any) -> None:
                url = route.request.url
                path = url.split("://" + url.split("/")[2], 1)[1].split("?", 1)[0]
                m = route.request.method
                if path == "/api/v1/projects" and m == "GET":
                    route.fulfill(status=200, json=projects)
                elif path == "/api/v1/sessions" and m == "GET":
                    route.fulfill(status=200, json=sessions)
                elif path == "/api/v1/projects" and m == "POST":
                    _hold("create_proj", route)
                elif path == "/api/v1/projects/register" and m == "POST":
                    _hold("reg_proj", route)
                elif path == "/api/v1/sessions" and m == "POST":
                    _hold("new_sess", route)
                elif path.endswith("/context") and m == "PUT":
                    _hold("put_ctx", route)
                elif path.endswith("/cancel") and m == "POST":
                    _hold("cancel", route)
                elif "/answer" in path and m == "POST":
                    _hold("answer", route)
                elif "/decision" in path and m == "POST":
                    _hold("decision", route)
                elif "/commands/" in path and path.endswith("/stop") and m == "POST":
                    _hold("stop_cmd", route)
                elif path.endswith("/context"):
                    sid = path.split("/")[4]
                    route.fulfill(status=200, json=_make_context(sid, p_id))
                elif path.endswith("/history"):
                    sid = path.split("/")[4]
                    route.fulfill(status=200, json={"session_id": sid, "items": [], "cursor": 0, "has_more": False})
                elif path.endswith("/snapshot"):
                    rid = path.split("/")[6]
                    sid = path.split("/")[4]
                    route.fulfill(status=200, json={"run": _make_run(rid, sid, p_id, state="running"), "events": [{"schema_version": "1", "project_id": p_id, "session_id": sid, "run_id": rid, "seq": 1, "kind": "command_started", "payload": {"command_id": cmd1, "state": "running"}}] if rid == r1 else [], "cursor": 1 if rid == r1 else 0, "truncated_before": 0, "pending_proposals": [], "pending_questions": []})
                elif path.endswith("/proposals"):
                    rid = path.split("/")[6]
                    if rid == r1:
                        route.fulfill(status=200, json=[{"id": prop1, "kind": "scope", "project_id": p_id, "session_id": s1, "run_id": r1, "digest": "a" * 64, "payload": {"exact_paths": ["a.py"], "operations": ["modify"], "verification_plan_summary": "p"}, "state": "pending", "expires_at": 999999.0}])
                    else:
                        route.fulfill(status=200, json=[])
                elif path.endswith("/questions"):
                    rid = path.split("/")[6]
                    if rid == r1:
                        route.fulfill(status=200, json=[{"id": q1, "project_id": p_id, "session_id": s1, "run_id": r1, "action_id": "a1", "question": "Q?", "options": ["y"], "digest": "d" * 64, "state": "pending", "answer": None, "created_at": "2026-11-16T00:00:00Z", "expires_at": "2026-11-16T01:00:00Z", "reason_code": None}])
                    else:
                        route.fulfill(status=200, json=[])
                elif path.endswith("/usage"):
                    route.fulfill(status=200, json={"calls_started": 0, "calls_completed": 0, "usage_known_calls": 0, "usage_unknown_calls": 0, "known_input_tokens": 0, "known_output_tokens": 0, "input_tokens": 0, "output_tokens": 0, "max_calls": 8, "steps_used": 0, "max_steps": 16, "elapsed_ms": 0, "run_timeout_ms": 60000, "estimated_cost_cny": None, "billed_cost_cny": None, "cost_status": "offline_not_billed"})
                elif f"/commands/{cmd1}" in path:
                    route.fulfill(status=200, json={"command_id": cmd1, "run_id": r1, "proposal_digest": "a" * 64, "state": "running", "pid": 123, "exit_code": None, "cleanup_ok": False, "remaining_active": 1, "first_seq": 0, "last_seq": 0, "chunks": [], "bytes_seen": 0, "truncated": False, "output_delivery_failed": False, "truncated_stdout_bytes": 0, "truncated_stderr_bytes": 0, "timed_out": False})
                elif path.endswith("/events"):
                    route.fulfill(status=200, content_type="text/event-stream", body=": hb\n\n")
                else:
                    route.fulfill(status=200, json={})

            page.route("**/api/**", _route)
            page.goto(f"{srv.base_url}/#token={FAKE_TEST_TOKEN}", wait_until="domcontentloaded")
            expect(page.get_by_test_id(f"proposal-item-{prop1}")).to_be_visible()
            expect(page.get_by_test_id(f"question-item-{q1}")).to_be_visible()
            if page.get_by_test_id("chat-inspector").is_hidden():
                page.get_by_test_id("toggle-inspector").click()
            page.get_by_test_id("tab-commands").click()
            expect(page.get_by_test_id(f"stop-command-{cmd1}")).to_be_visible()

            # 1. Create / Register / New session / Cancel / Answer / Proposal shared lock / Stop command
            for btn_id, key in [
                ("create-project-btn", "create_proj"),
                ("register-project-btn", "reg_proj"),
                ("new-session-btn", "new_sess"),
                ("cancel-run-btn", "cancel"),
                (f"stop-command-{cmd1}", "stop_cmd"),
            ]:
                if btn_id in ("create-project-btn", "register-project-btn", "new-session-btn"):
                    page.get_by_test_id("nav-projects").click()
                else:
                    page.get_by_test_id("nav-chat").click()
                if btn_id in ("create-project-btn",):
                    page.get_by_test_id("project-name").fill("p-dup")
                if btn_id == "register-project-btn":
                    page.get_by_test_id("register-root").fill("D:/mock/reg")
                    page.get_by_test_id("confirm-read").check()
                loc = page.get_by_test_id(btn_id.replace("-btn", "")) if "-btn" in btn_id else page.get_by_test_id(btn_id)
                page.evaluate("(el) => { el.click(); el.click(); }", loc.element_handle())
                expect(loc).to_be_disabled()
                assert len(held_mutations.get(key, [])) == 1

            # Fulfill held non-switching mutations safely
            held_mutations["create_proj"].pop(0).fulfill(status=201, json=projects[0])
            held_mutations["reg_proj"].pop(0).fulfill(status=201, json=projects[0])
            held_mutations["new_sess"].pop(0).fulfill(status=201, json=sessions[0])
            held_mutations["cancel"].pop(0).fulfill(status=202, json=_make_run(r1, s1, p_id, state="cancelling"))
            held_mutations["stop_cmd"].pop(0).fulfill(status=202, json={"command_id": cmd1, "run_id": r1, "proposal_digest": "a" * 64, "state": "running", "pid": 123, "exit_code": None, "cleanup_ok": False, "remaining_active": 1, "first_seq": 0, "last_seq": 0, "chunks": [], "bytes_seen": 0, "truncated": False, "output_delivery_failed": False, "truncated_stdout_bytes": 0, "truncated_stderr_bytes": 0, "timed_out": False})

            # Re-switch to s1 so r1 interactive cards are visible cleanly
            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id(f"session-item-{s1}").click()
            expect(page.get_by_test_id(f"proposal-approve-{prop1}")).to_be_visible()

            # Shared lock on proposal approve + reject
            page.evaluate(
                "(id) => { document.querySelector(`[data-testid=\"proposal-approve-${id}\"]`).click(); document.querySelector(`[data-testid=\"proposal-reject-${id}\"]`).click(); }",
                prop1,
            )
            expect(page.get_by_test_id(f"proposal-approve-{prop1}")).to_be_disabled()
            expect(page.get_by_test_id(f"proposal-reject-{prop1}")).to_be_disabled()
            assert len(held_mutations["decision"]) == 1
            held_mutations["decision"].pop(0).fulfill(
                status=200,
                json={"id": prop1, "kind": "scope", "project_id": p_id, "session_id": s1, "run_id": r1, "digest": "a" * 64, "payload": {"exact_paths": ["a.py"], "operations": ["modify"], "verification_plan_summary": "p"}, "state": "approved", "expires_at": 999999.0},
            )
            expect(page.get_by_test_id(f"proposal-item-{prop1}")).to_contain_text("[approved]")

            # Answer lock + constraints lock + switch away so stale s1 receipt does not touch s2
            page.get_by_test_id(f"question-answer-input-{q1}").fill("my ans")
            page.evaluate(
                "(qid) => { const b = document.querySelector(`[data-testid=\"question-submit-${qid}\"]`); b.click(); b.click(); }",
                q1,
            )
            expect(page.get_by_test_id(f"question-submit-{q1}")).to_be_disabled()
            assert len(held_mutations["answer"]) == 1
            page.get_by_test_id("nav-settings").click()
            page.get_by_test_id("settings-sec-constraints").click()
            page.get_by_test_id("constraints-input").fill("C_S1")
            page.evaluate("() => { const b = document.getElementById('save-constraints-btn'); b.click(); b.click(); }")
            expect(page.get_by_test_id("save-constraints")).to_be_disabled()
            assert len(held_mutations["put_ctx"]) == 1

            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id(f"session-item-{s2}").click()
            expect(page.get_by_test_id("current-session")).to_have_text(s2)
            expect(page.get_by_test_id("save-constraints")).to_be_enabled()
            held_mutations["answer"].pop(0).fulfill(status=200, json={"id": q1, "project_id": p_id, "session_id": s1, "run_id": r1, "action_id": "a1", "question": "Q?", "options": ["y"], "digest": "d" * 64, "state": "answered", "answer": "my ans", "created_at": "2026-11-16T00:00:00Z", "expires_at": "2026-11-16T01:00:00Z", "reason_code": None})
            held_mutations["put_ctx"].pop(0).fulfill(status=200, json=_make_context(s1, p_id, rev=1, constraints="C_S1"))
            page.wait_for_timeout(100)
            expect(page.get_by_test_id("current-session")).to_have_text(s2)
            expect(page.get_by_test_id("context-revision")).to_contain_text("rev=0")

            # F01 same-project counterexamples: new_session_after_switch & constraint_body_after_switch (+ positive controls)
            sa, sb, sc, sd = _uid(31), _uid(32), _uid(33), _uid(34)
            sp_proj = _make_project(p_id, "same-proj-mut")
            sp_sessions = [_make_session(sa, p_id, constraints="A_ORIGINAL"), _make_session(sb, p_id, constraints="B_KEEP")]
            ProjectView.model_validate(sp_proj)
            for item in sp_sessions:
                SessionView.model_validate(item)

            page2 = browser.new_page()
            try:
                pe2, _, nlb2, _ = _attach_watchers(page2, base_url=srv.base_url)
                page2.add_init_script(
                    """(() => {
                      const realFetch = window.fetch;
                      window.__delayNextPutContextBody = false;
                      window.fetch = async (...args) => {
                        const response = await realFetch(...args);
                        if (window.__delayNextPutContextBody && args[1]?.method === 'PUT' && String(args[0]).endsWith('/context')) {
                          window.__delayNextPutContextBody = false;
                          const realJson = response.json.bind(response);
                          response.json = async () => {
                            const body = await realJson();
                            window.__bodyWaiting = true;
                            await new Promise(resolve => { window.__releaseBody = resolve; });
                            return body;
                          };
                        }
                        return response;
                      };
                    })();"""
                )
                held_sp_new_sess: list[Any] = []
                sp_new_sess_mode = ["hold"]

                def _route_sp_mut(route: Any) -> None:
                    req = route.request
                    path = urlparse(req.url).path
                    m = req.method
                    if path == "/api/v1/projects" and m == "GET":
                        route.fulfill(status=200, json=[sp_proj])
                    elif path == "/api/v1/sessions" and m == "GET":
                        route.fulfill(status=200, json=sp_sessions)
                    elif path == "/api/v1/sessions" and m == "POST":
                        if sp_new_sess_mode[0] == "hold":
                            held_sp_new_sess.append(route)
                        else:
                            created_pos = _make_session(sd, p_id, constraints="")
                            SessionView.model_validate(created_pos)
                            sp_sessions.append(created_pos)
                            route.fulfill(status=201, json=created_pos)
                    elif path.endswith("/context") and m == "PUT":
                        sid = path.split("/")[4]
                        req_json = req.post_data_json
                        val = req_json["constraints"] if isinstance(req_json, dict) else "A_SAVED"
                        rev = int(req_json.get("expected_revision", 0)) + 1 if isinstance(req_json, dict) else 1
                        body = _make_context(sid, p_id, rev=rev, constraints=val)
                        SessionContextView.model_validate(body)
                        route.fulfill(status=200, json=body)
                    elif path.endswith("/context"):
                        sid = path.split("/")[4]
                        val = "B_KEEP" if sid == sb else ("A_ORIGINAL" if sid == sa else "")
                        body = _make_context(sid, p_id, constraints=val)
                        SessionContextView.model_validate(body)
                        route.fulfill(status=200, json=body)
                    elif path.endswith("/history"):
                        route.fulfill(status=200, json={"session_id": path.split("/")[4], "items": [], "cursor": 0, "has_more": False})
                    else:
                        route.fulfill(status=404, json={"error": {"code": "NOT_FOUND", "message": "unexpected"}})

                page2.route("**/api/**", _route_sp_mut)
                page2.goto(f"{srv.base_url}/#token={FAKE_TEST_TOKEN}", wait_until="domcontentloaded")
                expect(page2.get_by_test_id("current-session")).to_have_text(sa)
                expect(page2.get_by_test_id("constraints-input")).to_have_value("A_ORIGINAL")

                # 1) new_session_after_switch: A creates -> barrier -> click B -> fulfill C -> B stays selected
                page2.get_by_test_id("nav-projects").click()
                page2.get_by_test_id("new-session").click()
                for _ in range(100):
                    if held_sp_new_sess:
                        break
                    page2.wait_for_timeout(15)
                assert len(held_sp_new_sess) == 1, "POST /sessions must reach explicit hold barrier"
                page2.get_by_test_id("nav-projects").click()
                page2.get_by_test_id(f"session-item-{sb}").click()
                expect(page2.get_by_test_id("current-session")).to_have_text(sb)
                expect(page2.get_by_test_id("constraints-input")).to_have_value("B_KEEP")
                expect(page2.get_by_test_id("error")).to_have_text("")
                created_c = _make_session(sc, p_id)
                SessionView.model_validate(created_c)
                sp_sessions.append(created_c)
                held_sp_new_sess.pop(0).fulfill(status=201, json=created_c)
                expect(page2.get_by_test_id("new-session")).to_be_enabled()
                page2.wait_for_timeout(120)
                expected_sp = {"session": sb, "stored_session": sb, "constraints": "B_KEEP", "error": ""}
                observed_new = {
                    "session": page2.get_by_test_id("current-session").inner_text(),
                    "stored_session": page2.evaluate("sessionStorage.getItem('forecontract_active_session_id')"),
                    "constraints": page2.get_by_test_id("constraints-input").input_value(),
                    "error": page2.get_by_test_id("error").inner_text(),
                }
                assert observed_new == expected_sp

                # 2) constraint_body_after_switch: A saves -> body read barrier -> click B -> release body -> B keeps B_KEEP & rev=0
                page2.get_by_test_id("nav-projects").click()
                page2.get_by_test_id(f"session-item-{sa}").click()
                expect(page2.get_by_test_id("current-session")).to_have_text(sa)
                page2.get_by_test_id("nav-settings").click()
                page2.get_by_test_id("settings-sec-constraints").click()
                page2.get_by_test_id("constraints-input").fill("A_SAVED")
                page2.evaluate("() => { window.__delayNextPutContextBody = true; }")
                page2.get_by_test_id("save-constraints").click()
                page2.wait_for_function("() => window.__bodyWaiting === true")
                page2.get_by_test_id("nav-projects").click()
                page2.get_by_test_id(f"session-item-{sb}").click()
                expect(page2.get_by_test_id("current-session")).to_have_text(sb)
                expect(page2.get_by_test_id("constraints-input")).to_have_value("B_KEEP")
                expect(page2.get_by_test_id("error")).to_have_text("")
                page2.evaluate("() => { window.__releaseBody(); }")
                expect(page2.get_by_test_id("save-constraints")).to_be_enabled()
                page2.wait_for_timeout(120)
                observed_body = {
                    "session": page2.get_by_test_id("current-session").inner_text(),
                    "stored_session": page2.evaluate("sessionStorage.getItem('forecontract_active_session_id')"),
                    "constraints": page2.get_by_test_id("constraints-input").input_value(),
                    "error": page2.get_by_test_id("error").inner_text(),
                }
                assert observed_body == expected_sp
                expect(page2.get_by_test_id("context-revision")).to_contain_text("rev=0")

                # Positive controls: saving constraints cleanly on B & creating session normally without switching
                page2.get_by_test_id("nav-settings").click()
                page2.get_by_test_id("settings-sec-constraints").click()
                page2.get_by_test_id("constraints-input").fill("B_POSITIVE")
                page2.get_by_test_id("save-constraints").click()
                expect(page2.get_by_test_id("context-revision")).to_contain_text("rev=1")
                expect(page2.get_by_test_id("constraints-input")).to_have_value("B_POSITIVE")
                sp_new_sess_mode[0] = "ok"
                page2.get_by_test_id("nav-projects").click()
                page2.get_by_test_id("new-session").click()
                expect(page2.get_by_test_id("current-session")).to_have_text(sd)
                assert pe2 == []
                assert nlb2 == []
            finally:
                page2.close()

            # F01 C -> B -> C across ensureSessionForSelection final refreshSessionContextAndHistory await (U05 save-constraints branch)
            sb_u5, sc_u5 = _uid(52), _uid(53)
            proj_u5 = _make_project(p_id, "aba-save-proj")
            ProjectView.model_validate(proj_u5)
            sess_u5: list[dict[str, Any]] = []
            u5_req_log: list[dict[str, Any]] = []
            u5_session_posts = [0]
            page3 = browser.new_page()
            try:
                pe3, _, nlb3, _ = _attach_watchers(page3, base_url=srv.base_url)
                page3.add_init_script(
                    """(() => {
                      const realFetch = window.fetch;
                      window.__interceptFirstCreationContextBody = false;
                      window.__creationBodyWaiting = false;
                      window.fetch = async (input, init) => {
                        const url = typeof input === 'string' ? input : (input && input.url) || '';
                        const method = (init && init.method) ? init.method.toUpperCase() : 'GET';
                        const response = await realFetch(input, init);
                        if (window.__interceptFirstCreationContextBody && method === 'GET' && url.includes('/context')) {
                          window.__interceptFirstCreationContextBody = false;
                          const realJson = response.json.bind(response);
                          response.json = async () => {
                            const body = await realJson();
                            window.__creationBodyWaiting = true;
                            await new Promise(resolve => { window.__releaseCreationBody = resolve; });
                            return body;
                          };
                        }
                        return response;
                      };
                    })();"""
                )

                def _route_aba_u05(route: Any) -> None:
                    req = route.request
                    path = urlparse(req.url).path
                    m = req.method
                    post_body = None
                    if m in {"POST", "PUT"} and req.post_data:
                        post_body = req.post_data_json
                    u5_req_log.append({"method": m, "path": path, "body": post_body})
                    if path == "/api/v1/projects" and m == "GET":
                        route.fulfill(status=200, json=[proj_u5])
                    elif path == "/api/v1/sessions" and m == "GET":
                        route.fulfill(status=200, json=sess_u5)
                    elif path == "/api/v1/sessions" and m == "POST":
                        u5_session_posts[0] += 1
                        new_b = _make_session(sb_u5, p_id, constraints="B_KEEP")
                        new_c = _make_session(sc_u5, p_id, constraints="C_FRESH_SERVER")
                        SessionView.model_validate(new_b)
                        SessionView.model_validate(new_c)
                        sess_u5.extend([new_b, new_c])
                        route.fulfill(status=201, json=new_c)
                    elif path.endswith("/context") and m == "PUT":
                        sid = path.split("/")[4]
                        val = post_body["constraints"] if isinstance(post_body, dict) else ""
                        rev = int(post_body.get("expected_revision", 0)) + 1 if isinstance(post_body, dict) else 1
                        body = _make_context(sid, p_id, rev=rev, constraints=val)
                        SessionContextView.model_validate(body)
                        route.fulfill(status=200, json=body)
                    elif path.endswith("/context"):
                        sid = path.split("/")[4]
                        val = "B_KEEP" if sid == sb_u5 else "C_FRESH_SERVER"
                        ctx = _make_context(sid, p_id, constraints=val)
                        SessionContextView.model_validate(ctx)
                        route.fulfill(status=200, json=ctx)
                    elif path.endswith("/history"):
                        route.fulfill(status=200, json={"session_id": path.split("/")[4], "items": [], "cursor": 0, "has_more": False})
                    else:
                        route.fulfill(status=404, json={"error": {"code": "NOT_FOUND", "message": "unexpected"}})

                page3.route("**/api/**", _route_aba_u05)
                page3.goto(f"{srv.base_url}/#token={FAKE_TEST_TOKEN}", wait_until="domcontentloaded")
                expect(page3.get_by_test_id("current-session")).to_have_text("-")

                # Save from no-session state to trigger ensureSessionForSelection POST /sessions -> C
                page3.get_by_test_id("nav-settings").click()
                page3.get_by_test_id("settings-sec-constraints").click()
                page3.get_by_test_id("constraints-input").fill("OLD_STALE_CONSTRAINTS")
                page3.evaluate("() => { window.__interceptFirstCreationContextBody = true; }")
                page3.get_by_test_id("save-constraints").click()

                page3.wait_for_function("() => window.__creationBodyWaiting === true")
                creation_body_waiting = page3.evaluate("() => window.__creationBodyWaiting === true")
                assert creation_body_waiting is True
                assert u5_session_posts[0] == 1

                # Perform real clicks B -> C while original C creation context body is still held
                page3.get_by_test_id("nav-projects").click()
                page3.get_by_test_id(f"session-item-{sb_u5}").click()
                expect(page3.get_by_test_id("current-session")).to_have_text(sb_u5)
                expect(page3.get_by_test_id("constraints-input")).to_have_value("B_KEEP")

                page3.get_by_test_id("nav-projects").click()
                page3.get_by_test_id(f"session-item-{sc_u5}").click()
                expect(page3.get_by_test_id("current-session")).to_have_text(sc_u5)
                expect(page3.get_by_test_id("constraints-input")).to_have_value("C_FRESH_SERVER")
                fresh_text = "NEW_CONSTRAINTS_AFTER_ABA"
                page3.get_by_test_id("nav-settings").click()
                page3.get_by_test_id("settings-sec-constraints").click()
                page3.get_by_test_id("constraints-input").fill(fresh_text)
                context_revision_before_release = page3.get_by_test_id("context-revision").inner_text()

                page3.evaluate("() => { window.__releaseCreationBody(); }")
                expect(page3.get_by_test_id("save-constraints")).to_be_enabled()
                page3.wait_for_timeout(120)

                expected_aba = {"session": sc_u5, "stored_session": sc_u5, "text": fresh_text, "error": ""}
                observed_after_release = {
                    "session": page3.get_by_test_id("current-session").inner_text(),
                    "stored_session": page3.evaluate("sessionStorage.getItem('forecontract_active_session_id')"),
                    "text": page3.get_by_test_id("constraints-input").input_value(),
                    "error": page3.get_by_test_id("error").inner_text(),
                }
                assert observed_after_release == expected_aba
                assert page3.get_by_test_id("context-revision").inner_text() == context_revision_before_release
                old_followup_requests = [
                    r for r in u5_req_log if r["method"] == "PUT" and r["path"] == f"/api/v1/sessions/{sc_u5}/context"
                ]
                assert old_followup_requests == []

                # Fresh explicit Save click must release cleanly and send exactly one PUT /sessions/C/context with fresh_text
                fresh_mark = len(u5_req_log)
                page3.get_by_test_id("save-constraints").click()
                expect(page3.get_by_test_id("context-revision")).to_contain_text("rev=1")
                fresh_followup_requests = [
                    r for r in u5_req_log[fresh_mark:] if r["method"] == "PUT" and r["path"] == f"/api/v1/sessions/{sc_u5}/context"
                ]
                assert len(fresh_followup_requests) == 1
                assert fresh_followup_requests[0]["body"]["constraints"] == fresh_text
                assert pe3 == []
                assert nlb3 == []
            finally:
                page3.close()

            assert page_errors == []
            assert non_loopback == []
            _ = reqs
        finally:
            browser.close()


def test_u06_stream_reconnect() -> None:
    p_id = _uid(1)
    s1, s2 = _uid(11), _uid(12)
    r1, r2 = _uid(101), _uid(102)
    projects = [_make_project(p_id, "proj-sse")]
    sessions = [_make_session(s1, p_id, active_run_id=r1), _make_session(s2, p_id, active_run_id=r2)]

    with _StaticFrontendServer() as srv, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.clock.install()
            page_errors, _, non_loopback, reqs = _attach_watchers(
                page, base_url=srv.base_url, allow_error_substrings=("Failed", "500", "409")
            )
            sse_mode = ["fail"]
            s1_event_reqs = [0]
            s1_snapshots = [0]

            def _route(route: Any) -> None:
                url = route.request.url
                path = url.split("://" + url.split("/")[2], 1)[1].split("?", 1)[0]
                if path == "/api/v1/projects":
                    route.fulfill(status=200, json=projects)
                elif path == "/api/v1/sessions":
                    route.fulfill(status=200, json=sessions)
                elif path.endswith("/context"):
                    sid = path.split("/")[4]
                    route.fulfill(status=200, json=_make_context(sid, p_id))
                elif path.endswith("/history"):
                    sid = path.split("/")[4]
                    route.fulfill(status=200, json={"session_id": sid, "items": [], "cursor": 0, "has_more": False})
                elif path == f"/api/v1/sessions/{s1}/runs/{r1}/snapshot":
                    s1_snapshots[0] += 1
                    route.fulfill(status=200, json={"run": _make_run(r1, s1, p_id, state="running"), "events": [], "cursor": 0, "truncated_before": 0, "pending_proposals": [], "pending_questions": []})
                elif path == f"/api/v1/sessions/{s2}/runs/{r2}/snapshot":
                    route.fulfill(status=200, json={"run": _make_run(r2, s2, p_id, state="running"), "events": [], "cursor": 0, "truncated_before": 0, "pending_proposals": [], "pending_questions": []})
                elif path.endswith(("/proposals", "/questions")):
                    route.fulfill(status=200, json=[])
                elif path.endswith("/usage"):
                    route.fulfill(status=200, json={"calls_started": 0, "calls_completed": 0, "usage_known_calls": 0, "usage_unknown_calls": 0, "known_input_tokens": 0, "known_output_tokens": 0, "input_tokens": 0, "output_tokens": 0, "max_calls": 8, "steps_used": 0, "max_steps": 16, "elapsed_ms": 0, "run_timeout_ms": 60000, "estimated_cost_cny": None, "billed_cost_cny": None, "cost_status": "offline_not_billed"})
                elif path == f"/api/v1/sessions/{s1}/runs/{r1}/events":
                    s1_event_reqs[0] += 1
                    if sse_mode[0] == "fail":
                        route.abort("failed")
                    elif sse_mode[0] == "409":
                        sse_mode[0] = "ok"
                        route.fulfill(status=409, json={"error": {"code": "SEQ_EXPIRED", "message": "expired"}})
                    elif sse_mode[0] == "resync":
                        sse_mode[0] = "ok"
                        route.fulfill(status=200, content_type="text/event-stream", body=_sse_body([], resync=True))
                    else:
                        route.fulfill(status=200, content_type="text/event-stream", body=": ok\n\n")
                elif path == f"/api/v1/sessions/{s2}/runs/{r2}/events":
                    route.fulfill(status=200, content_type="text/event-stream", body=": ok\n\n")
                else:
                    route.fulfill(status=200, json={})

            page.route("**/api/**", _route)
            page.goto(f"{srv.base_url}/#token={FAKE_TEST_TOKEN}", wait_until="domcontentloaded")

            # Initial failure schedules retry #1 in 1s -> "reconnecting"
            expect(page.get_by_test_id("connection-state")).to_have_text("reconnecting")
            assert s1_event_reqs[0] == 1

            # 1s, 2s, 4s exponential backoff retries (3 attempts then stop)
            page.clock.run_for(1000)
            expect(page.get_by_test_id("connection-state")).to_have_text("reconnecting")
            assert s1_event_reqs[0] == 2

            page.clock.run_for(2000)
            expect(page.get_by_test_id("connection-state")).to_have_text("reconnecting")
            assert s1_event_reqs[0] == 3

            page.clock.run_for(4000)
            expect(page.get_by_test_id("connection-state")).to_have_text("disconnected")
            expect(page.get_by_test_id("run-state")).to_have_text("running")
            assert s1_event_reqs[0] == 4

            # No further retries after 3 failures
            page.clock.run_for(10000)
            assert s1_event_reqs[0] == 4

            # Explicit reconnect recovers via GET (testing 409 -> snapshot)
            snaps_before_409 = s1_snapshots[0]
            sse_mode[0] = "409"
            page.get_by_test_id("reconnect-stream").click()
            expect(page.get_by_test_id("connection-state")).to_have_text("connected")
            assert s1_snapshots[0] == snaps_before_409 + 1

            # Explicit resync_required event triggers snapshot cleanly
            sse_mode[0] = "resync"
            page.clock.run_for(100)
            # Re-open from disconnected or force resync via manual switch back:
            sse_mode[0] = "fail"
            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id(f"session-item-{s2}").click()
            expect(page.get_by_test_id("current-session")).to_have_text(s2)
            # Back to s1 with resync stream -> 1 extra snapshot call from resync_required
            sse_mode[0] = "resync"
            snaps_before_resync = s1_snapshots[0]
            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id(f"session-item-{s1}").click()
            deadline = 50
            while deadline > 0 and s1_snapshots[0] < snaps_before_resync + 2:
                page.wait_for_timeout(40)
                deadline -= 1
            assert s1_snapshots[0] >= snaps_before_resync + 2

            # Pending timer on s1 cancelled cleanly when switching to s2
            sse_mode[0] = "fail"
            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id(f"session-item-{s2}").click()
            expect(page.get_by_test_id("current-session")).to_have_text(s2)
            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id(f"session-item-{s1}").click()
            expect(page.get_by_test_id("connection-state")).to_have_text("reconnecting")
            count_at_switch = s1_event_reqs[0]
            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id(f"session-item-{s2}").click()
            page.clock.run_for(10000)
            assert s1_event_reqs[0] == count_at_switch
            assert not [r for r in reqs if r["method"] in {"POST", "PUT"}]
            assert page_errors == []
            assert non_loopback == []
        finally:
            browser.close()


def test_u07_event_identity_and_dedup() -> None:
    p_id, p_other = _uid(1), _uid(2)
    s_id, s_other = _uid(11), _uid(12)
    r_id, r_other = _uid(101), _uid(102)
    cmd_id = "cmd-dedup"

    events = [
        {"schema_version": "1", "project_id": p_id, "session_id": s_id, "run_id": r_id, "seq": 8, "kind": "tool_result", "payload": {"action_type": "edit_file", "path": "eight.py", "tool_succeeded": True}},
        {"schema_version": "1", "project_id": p_id, "session_id": s_id, "run_id": r_id, "seq": 6, "kind": "command_started", "payload": {"command_id": cmd_id, "state": "running"}},
        {"schema_version": "1", "project_id": p_id, "session_id": s_id, "run_id": r_id, "seq": 6, "kind": "command_started", "payload": {"command_id": cmd_id, "state": "running"}},
        {"schema_version": "1", "project_id": p_id, "session_id": s_id, "run_id": r_id, "seq": 7, "kind": "command_output", "payload": {"command_id": cmd_id, "seq": 1, "stream": "stdout", "text": "ONCE_LINE\n"}},
        {"schema_version": "1", "project_id": p_id, "session_id": s_id, "run_id": r_id, "seq": 9, "kind": "command_output", "payload": {"command_id": cmd_id, "seq": 1, "stream": "stdout", "text": "ONCE_LINE\n"}},
        {"schema_version": "1", "project_id": p_other, "session_id": s_id, "run_id": r_id, "seq": 10, "kind": "tool_result", "payload": {"action_type": "wrong_proj", "path": "WRONG_PROJ.py"}},
        {"schema_version": "1", "project_id": p_id, "session_id": s_other, "run_id": r_id, "seq": 11, "kind": "tool_result", "payload": {"action_type": "wrong_sess", "path": "WRONG_SESS.py"}},
        {"schema_version": "1", "project_id": p_id, "session_id": s_id, "run_id": r_other, "seq": 12, "kind": "tool_result", "payload": {"action_type": "wrong_run", "path": "WRONG_RUN.py"}},
    ]

    with _StaticFrontendServer() as srv, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page_errors, console_errors, non_loopback, _ = _attach_watchers(page, base_url=srv.base_url)

            def _route(route: Any) -> None:
                path = route.request.url.split("://" + route.request.url.split("/")[2], 1)[1].split("?", 1)[0]
                if path == "/api/v1/projects":
                    route.fulfill(status=200, json=[_make_project(p_id, "proj-dedup")])
                elif path == "/api/v1/sessions":
                    route.fulfill(status=200, json=[_make_session(s_id, p_id, active_run_id=r_id)])
                elif path.endswith("/context"):
                    route.fulfill(status=200, json=_make_context(s_id, p_id))
                elif path.endswith("/history"):
                    route.fulfill(status=200, json={"session_id": s_id, "items": [], "cursor": 0, "has_more": False})
                elif path.endswith("/snapshot"):
                    route.fulfill(status=200, json={"run": _make_run(r_id, s_id, p_id, state="succeeded", verification="passed", reason_code="OK", last_seq=9, truncated_before=5), "events": [events[0]], "cursor": 9, "truncated_before": 5, "pending_proposals": [], "pending_questions": []})
                elif path.endswith(("/proposals", "/questions")):
                    route.fulfill(status=200, json=[])
                elif path.endswith("/usage"):
                    route.fulfill(status=200, json={"calls_started": 1, "calls_completed": 1, "usage_known_calls": 1, "usage_unknown_calls": 0, "known_input_tokens": 10, "known_output_tokens": 5, "input_tokens": 10, "output_tokens": 5, "max_calls": 8, "steps_used": 1, "max_steps": 16, "elapsed_ms": 5, "run_timeout_ms": 60000, "estimated_cost_cny": None, "billed_cost_cny": None, "cost_status": "offline_not_billed"})
                elif f"/commands/{cmd_id}" in path:
                    route.fulfill(status=200, json={"command_id": cmd_id, "run_id": r_id, "proposal_digest": "a" * 64, "state": "exited", "pid": 99, "exit_code": 0, "cleanup_ok": True, "remaining_active": 0, "first_seq": 1, "last_seq": 1, "chunks": [{"seq": 1, "stream": "stdout", "text": "ONCE_LINE\n"}, {"seq": 1, "stream": "stdout", "text": "ONCE_LINE\n"}], "bytes_seen": 10, "truncated": False, "output_delivery_failed": False, "truncated_stdout_bytes": 0, "truncated_stderr_bytes": 0, "timed_out": False})
                elif path.endswith("/events"):
                    route.fulfill(status=200, content_type="text/event-stream", body=_sse_body(events))
                else:
                    route.fulfill(status=200, json={})

            # Mark run temporarily running first to consume SSE, then inspect
            page.route("**/api/**", _route)
            # Re-route snapshot as running so SSE opens, then return succeeded on GET run
            snap_count = [0]

            def _route2(route: Any) -> None:
                path = route.request.url.split("://" + route.request.url.split("/")[2], 1)[1].split("?", 1)[0]
                if path.endswith("/snapshot"):
                    snap_count[0] += 1
                    route.fulfill(status=200, json={"run": _make_run(r_id, s_id, p_id, state="running", verification="not_run", last_seq=9, truncated_before=5), "events": [events[0]], "cursor": 5, "truncated_before": 5, "pending_proposals": [], "pending_questions": []})
                    return
                if path == f"/api/v1/sessions/{s_id}/runs/{r_id}":
                    route.fulfill(status=200, json=_make_run(r_id, s_id, p_id, state="succeeded", verification="passed", reason_code="OK", last_seq=9, truncated_before=5))
                    return
                _route(route)

            page.route("**/api/**", _route2)
            page.goto(f"{srv.base_url}/#token={FAKE_TEST_TOKEN}", wait_until="domcontentloaded")
            expect(page.get_by_test_id("run-state")).to_have_text("succeeded")
            expect(page.get_by_test_id("events-truncation-notice")).to_contain_text("seq <= 5")

            ev_texts = page.get_by_test_id("tool-events").locator("li").all_inner_texts()
            assert [t.split(" ", 1)[0] for t in ev_texts] == ["#6", "#7", "#8", "#9"]
            joined = "\n".join(ev_texts)
            assert "WRONG_PROJ.py" not in joined
            assert "WRONG_SESS.py" not in joined
            assert "WRONG_RUN.py" not in joined

            cmd_out = page.get_by_test_id(f"command-output-{cmd_id}").inner_text()
            assert cmd_out.count("ONCE_LINE") == 1
            assert page_errors == []
            assert console_errors == []
            assert non_loopback == []
        finally:
            browser.close()


def test_u08_truthful_terminal_states() -> None:
    p_id = _uid(1)
    s_id = _uid(11)
    cases = [
        (_uid(201), "running", "not_run", None, "-"),
        (_uid(202), "succeeded", "passed", "OK", "OK"),
        (_uid(203), "failed", "failed", "VERIFICATION_FAILED", "VERIFICATION_FAILED"),
        (_uid(204), "cancelled", "unknown", "USER_CANCELLED", "USER_CANCELLED"),
        (_uid(205), "unknown", "unknown", "HOST_RESTARTED", "HOST_RESTARTED"),
        (_uid(206), "failed", "unknown", "CHECK_EXIT0_UNVERIFIED", "CHECK_EXIT0_UNVERIFIED"),
    ]
    current_idx = [0]

    with _StaticFrontendServer() as srv, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page_errors, _, non_loopback, _ = _attach_watchers(
                page, base_url=srv.base_url, allow_error_substrings=("Failed",)
            )
            page.clock.install()

            def _route(route: Any) -> None:
                rid = cases[current_idx[0]][0]
                path = route.request.url.split("://" + route.request.url.split("/")[2], 1)[1].split("?", 1)[0]
                if path == "/api/v1/projects":
                    route.fulfill(status=200, json=[_make_project(p_id, "proj-term")])
                elif path == "/api/v1/sessions":
                    route.fulfill(status=200, json=[_make_session(s_id, p_id, active_run_id=rid)])
                elif path.endswith("/context"):
                    route.fulfill(status=200, json=_make_context(s_id, p_id))
                elif path.endswith("/history"):
                    route.fulfill(status=200, json={"session_id": s_id, "items": [{"seq": i + 1, "item_id": _uid(3000 + i), "project_id": p_id, "session_id": s_id, "run_id": c[0], "kind": "run_result", "text": c[1], "source_ids": [c[0]], "truncated": False, "sha256": "e" * 64} for i, c in enumerate(cases)], "cursor": len(cases), "has_more": False})
                elif "/snapshot" in path:
                    target_rid = path.split("/")[6]
                    match = next(c for c in cases if c[0] == target_rid)
                    res = {"checks": [{"check_id": "chk", "kind": "exit_code", "exit_code": 0, "passed": True}]} if match[0] == _uid(206) else None
                    route.fulfill(status=200, json={"run": _make_run(match[0], s_id, p_id, state=match[1], verification=match[2], reason_code=match[3], result=res), "events": [], "cursor": 0, "truncated_before": 0, "pending_proposals": [], "pending_questions": []})
                elif path.endswith(("/proposals", "/questions")):
                    route.fulfill(status=200, json=[])
                elif path.endswith("/usage"):
                    route.fulfill(status=200, json={"calls_started": 0, "calls_completed": 0, "usage_known_calls": 0, "usage_unknown_calls": 0, "known_input_tokens": 0, "known_output_tokens": 0, "input_tokens": 0, "output_tokens": 0, "max_calls": 8, "steps_used": 0, "max_steps": 16, "elapsed_ms": 0, "run_timeout_ms": 60000, "estimated_cost_cny": None, "billed_cost_cny": None, "cost_status": "offline_not_billed"})
                elif path.endswith("/events"):
                    route.abort("failed")
                else:
                    route.fulfill(status=200, json={})

            page.route("**/api/**", _route)
            page.goto(f"{srv.base_url}/#token={FAKE_TEST_TOKEN}", wait_until="domcontentloaded")

            # Disconnect during running does not mutate run-state/verification
            expect(page.get_by_test_id("run-state")).to_have_text("running")
            expect(page.get_by_test_id("verification")).to_have_text("not_run")
            expect(page.get_by_test_id("reason-code")).to_have_text("-")
            page.clock.run_for(1000)
            expect(page.get_by_test_id("connection-state")).to_have_text("reconnecting")
            page.clock.run_for(2000)
            expect(page.get_by_test_id("connection-state")).to_have_text("reconnecting")
            page.clock.run_for(4000)
            expect(page.get_by_test_id("connection-state")).to_have_text("disconnected")
            expect(page.get_by_test_id("run-state")).to_have_text("running")
            expect(page.get_by_test_id("verification")).to_have_text("not_run")

            for rid, rstate, rver, _, expected_reason in cases[1:]:
                page.get_by_test_id("nav-history").click()
                page.get_by_test_id("run-history-select").select_option(rid)
                page.get_by_test_id("nav-chat").click()
                expect(page.get_by_test_id("run-id")).to_have_text(rid)
                expect(page.get_by_test_id("run-state")).to_have_text(rstate)
                expect(page.get_by_test_id("verification")).to_have_text(rver)
                expect(page.get_by_test_id("reason-code")).to_have_text(expected_reason)

            assert page_errors == []
            assert non_loopback == []
        finally:
            browser.close()


def test_u09_permission_and_question_binding() -> None:
    p_id = _uid(1)
    s1, s2 = _uid(11), _uid(12)
    r1, r2 = _uid(101), _uid(102)
    prop1, prop2 = _uid(301), _uid(302)
    q1, q2 = _uid(401), _uid(402)
    dig_p1, dig_p2 = "1" * 64, "2" * 64
    dig_q1, dig_q2 = "3" * 64, "4" * 64

    with _StaticFrontendServer() as srv, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page_errors, _, non_loopback, reqs = _attach_watchers(
                page, base_url=srv.base_url, allow_error_substrings=("403", "409")
            )
            prop_mode = ["require_trust"]
            q1_answered: list[str | None] = [None]

            def _route(route: Any) -> None:
                path = route.request.url.split("://" + route.request.url.split("/")[2], 1)[1].split("?", 1)[0]
                m = route.request.method
                if path == "/api/v1/projects":
                    route.fulfill(status=200, json=[_make_project(p_id, "proj-bind", kind="existing")])
                elif path == "/api/v1/sessions":
                    route.fulfill(status=200, json=[_make_session(s1, p_id, active_run_id=r1), _make_session(s2, p_id, active_run_id=r2)])
                elif path.endswith("/context"):
                    sid = path.split("/")[4]
                    route.fulfill(status=200, json=_make_context(sid, p_id))
                elif path.endswith("/history"):
                    sid = path.split("/")[4]
                    route.fulfill(status=200, json={"session_id": sid, "items": [], "cursor": 0, "has_more": False})
                elif path.endswith("/snapshot"):
                    sid = path.split("/")[4]
                    rid = r1 if sid == s1 else r2
                    route.fulfill(status=200, json={"run": _make_run(rid, sid, p_id, state="running"), "events": [], "cursor": 0, "truncated_before": 0, "pending_proposals": [], "pending_questions": []})
                elif path in {f"/api/v1/sessions/{s1}/runs/{r1}", f"/api/v1/sessions/{s2}/runs/{r2}"}:
                    sid = path.split("/")[4]
                    rid = r1 if sid == s1 else r2
                    route.fulfill(status=200, json=_make_run(rid, sid, p_id, state="running"))
                elif path.endswith("/proposals"):
                    sid = path.split("/")[4]
                    if sid == s1:
                        route.fulfill(status=200, json=[{"id": prop1, "kind": "command", "project_id": p_id, "session_id": s1, "run_id": r1, "digest": dig_p1, "payload": {"command": {"executable": "python", "argv": ["-V"], "cwd": ".", "timeout_ms": 5000, "purpose": "diagnostic", "explanation": "check"}}, "state": "pending", "expires_at": 999999.0}])
                    else:
                        route.fulfill(status=200, json=[{"id": prop2, "kind": "scope", "project_id": p_id, "session_id": s2, "run_id": r2, "digest": dig_p2, "payload": {"exact_paths": ["x.py"], "operations": ["modify"], "verification_plan_summary": "p"}, "state": "pending", "expires_at": 999999.0}])
                elif path.endswith("/questions"):
                    sid = path.split("/")[4]
                    qid, dig, rid = (q1, dig_q1, r1) if sid == s1 else (q2, dig_q2, r2)
                    ans_val = q1_answered[0] if sid == s1 else None
                    q_state = "answered" if ans_val is not None else "pending"
                    route.fulfill(status=200, json=[{"id": qid, "project_id": p_id, "session_id": sid, "run_id": rid, "action_id": "a1", "question": "Approve?", "options": ["yes"], "digest": dig, "state": q_state, "answer": ans_val, "created_at": "2026-11-16T00:00:00Z", "expires_at": "2026-11-16T01:00:00Z", "reason_code": None}])
                elif path.endswith("/usage"):
                    route.fulfill(status=200, json={"calls_started": 0, "calls_completed": 0, "usage_known_calls": 0, "usage_unknown_calls": 0, "known_input_tokens": 0, "known_output_tokens": 0, "input_tokens": 0, "output_tokens": 0, "max_calls": 8, "steps_used": 0, "max_steps": 16, "elapsed_ms": 0, "run_timeout_ms": 60000, "estimated_cost_cny": None, "billed_cost_cny": None, "cost_status": "offline_not_billed"})
                elif path.endswith("/answer") and m == "POST":
                    body = route.request.post_data_json
                    qid = path.split("/")[8]
                    q1_answered[0] = body["text"]
                    route.fulfill(status=200, json={"id": qid, "project_id": p_id, "session_id": s1, "run_id": r1, "action_id": "a1", "question": "Approve?", "options": ["yes"], "digest": body["question_digest"], "state": "answered", "answer": body["text"], "created_at": "2026-11-16T00:00:00Z", "expires_at": "2026-11-16T01:00:00Z", "reason_code": None})
                elif path.endswith("/decision") and m == "POST":
                    if prop_mode[0] == "require_trust":
                        route.fulfill(status=403, json={"error": {"code": "TRUSTED_LOCAL_EXECUTION_REQUIRED", "message": "checkbox required"}})
                    else:
                        route.fulfill(status=409, json={"error": {"code": "PROPOSAL_EXPIRED", "message": "expired"}})
                elif path.endswith("/events"):
                    route.fulfill(status=200, content_type="text/event-stream", body=": hb\n\n")
                else:
                    route.fulfill(status=200, json={})

            page.route("**/api/**", _route)
            page.goto(f"{srv.base_url}/#token={FAKE_TEST_TOKEN}", wait_until="domcontentloaded")
            expect(page.get_by_test_id(f"proposal-item-{prop1}")).to_be_visible()

            # Free text "批准全部" emits only /answer, 0 /decision
            dec_before = len([r for r in reqs if r["url"].endswith("/decision")])
            page.get_by_test_id(f"question-answer-input-{q1}").fill("批准全部")
            page.get_by_test_id(f"question-submit-{q1}").click()
            expect(page.get_by_test_id(f"question-item-{q1}")).to_contain_text("answer=批准全部")
            ans_reqs = [r for r in reqs if r["url"].endswith("/answer")]
            assert len(ans_reqs) == 1
            assert f"/sessions/{s1}/runs/{r1}/questions/{q1}/answer" in ans_reqs[0]["url"]
            assert ans_reqs[0]["json"]["question_digest"] == dig_q1
            assert ans_reqs[0]["json"]["text"] == "批准全部"
            assert len([r for r in reqs if r["url"].endswith("/decision")]) == dec_before

            # Approving command without trust checkbox keeps trusted_local_execution=False & displays 403
            page.get_by_test_id(f"proposal-approve-{prop1}").click()
            expect(page.get_by_test_id("error")).to_contain_text("TRUSTED_LOCAL_EXECUTION_REQUIRED")
            dec1 = [r for r in reqs if r["url"].endswith("/decision")][-1]
            assert f"/sessions/{s1}/runs/{r1}/proposals/{prop1}/decision" in dec1["url"]
            assert dec1["json"]["digest"] == dig_p1
            assert dec1["json"]["trusted_local_execution"] is False

            # Checking trust + 409 expired does not auto-re-approve
            prop_mode[0] = "expired"
            page.get_by_test_id(f"proposal-trust-{prop1}").check()
            dec_before_409 = len([r for r in reqs if r["url"].endswith("/decision")])
            page.get_by_test_id(f"proposal-approve-{prop1}").click()
            expect(page.get_by_test_id("error")).to_contain_text("PROPOSAL_EXPIRED")
            page.wait_for_timeout(120)
            assert len([r for r in reqs if r["url"].endswith("/decision")]) == dec_before_409 + 1

            # Switch to s2/r2 -> prop1 card is completely removed & replaced by prop2
            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id(f"session-item-{s2}").click()
            expect(page.get_by_test_id(f"proposal-item-{prop2}")).to_be_visible()
            expect(page.get_by_test_id(f"proposal-item-{prop1}")).to_have_count(0)
            assert page_errors == []
            assert non_loopback == []
        finally:
            browser.close()


def test_u10_command_output_and_stop() -> None:
    p_id, s_id, r_id, cid = _uid(1), _uid(11), _uid(101), "cmd-out-1"
    with _StaticFrontendServer() as srv, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page_errors, console_errors, non_loopback, reqs = _attach_watchers(page, base_url=srv.base_url)
            cmd_state = ["running"]
            terminal_run = [False]

            def _route(route: Any) -> None:
                path = route.request.url.split("://" + route.request.url.split("/")[2], 1)[1].split("?", 1)[0]
                m = route.request.method
                if path == "/api/v1/projects":
                    route.fulfill(status=200, json=[_make_project(p_id, "proj-cmd", kind="existing")])
                elif path == "/api/v1/sessions":
                    route.fulfill(status=200, json=[_make_session(s_id, p_id, active_run_id=r_id)])
                elif path.endswith("/context"):
                    route.fulfill(status=200, json=_make_context(s_id, p_id))
                elif path.endswith("/history"):
                    route.fulfill(status=200, json={"session_id": s_id, "items": [], "cursor": 0, "has_more": False})
                elif path.endswith("/snapshot"):
                    st = "cancelled" if terminal_run[0] else "running"
                    route.fulfill(status=200, json={"run": _make_run(r_id, s_id, p_id, state=st), "events": [{"schema_version": "1", "project_id": p_id, "session_id": s_id, "run_id": r_id, "seq": 1, "kind": "command_started", "payload": {"command_id": cid, "state": cmd_state[0], "pid": 4321}}, {"schema_version": "1", "project_id": p_id, "session_id": s_id, "run_id": r_id, "seq": 2, "kind": "command_output", "payload": {"command_id": cid, "seq": 1, "stream": "stdout", "text": "OUT_1\n"}}, {"schema_version": "1", "project_id": p_id, "session_id": s_id, "run_id": r_id, "seq": 3, "kind": "command_output", "payload": {"command_id": cid, "seq": 1, "stream": "stdout", "text": "OUT_1\n"}}, {"schema_version": "1", "project_id": p_id, "session_id": s_id, "run_id": r_id, "seq": 4, "kind": "command_output", "payload": {"command_id": cid, "seq": 2, "stream": "stderr", "text": "ERR_2\n"}}], "cursor": 4, "truncated_before": 0, "pending_proposals": [], "pending_questions": []})
                elif path == f"/api/v1/sessions/{s_id}/runs/{r_id}/commands/{cid}":
                    route.fulfill(status=200, json={"command_id": cid, "run_id": r_id, "proposal_digest": "a" * 64, "state": cmd_state[0], "pid": 4321, "exit_code": 1 if cmd_state[0] == "cancelled" else None, "cleanup_ok": cmd_state[0] == "cancelled", "remaining_active": 0 if cmd_state[0] == "cancelled" else 1, "first_seq": 1, "last_seq": 2, "chunks": [{"seq": 1, "stream": "stdout", "text": "OUT_1\n"}, {"seq": 1, "stream": "stdout", "text": "OUT_1\n"}, {"seq": 2, "stream": "stderr", "text": "ERR_2\n"}], "bytes_seen": 12, "truncated": True, "output_delivery_failed": False, "truncated_stdout_bytes": 128, "truncated_stderr_bytes": 0, "timed_out": False})
                elif path.endswith("/stop") and m == "POST":
                    route.fulfill(status=202, json={"command_id": cid, "run_id": r_id, "proposal_digest": "a" * 64, "state": "running", "pid": 4321, "exit_code": None, "cleanup_ok": False, "remaining_active": 1, "first_seq": 1, "last_seq": 2, "chunks": [], "bytes_seen": 12, "truncated": True, "output_delivery_failed": False, "truncated_stdout_bytes": 128, "truncated_stderr_bytes": 0, "timed_out": False})
                elif path.endswith(("/proposals", "/questions")):
                    route.fulfill(status=200, json=[])
                elif path.endswith("/usage"):
                    route.fulfill(status=200, json={"calls_started": 0, "calls_completed": 0, "usage_known_calls": 0, "usage_unknown_calls": 0, "known_input_tokens": 0, "known_output_tokens": 0, "input_tokens": 0, "output_tokens": 0, "max_calls": 8, "steps_used": 0, "max_steps": 16, "elapsed_ms": 0, "run_timeout_ms": 60000, "estimated_cost_cny": None, "billed_cost_cny": None, "cost_status": "offline_not_billed"})
                elif path.endswith("/events"):
                    route.fulfill(status=200, content_type="text/event-stream", body=": hb\n\n")
                else:
                    route.fulfill(status=200, json={})

            page.route("**/api/**", _route)
            page.goto(f"{srv.base_url}/#token={FAKE_TEST_TOKEN}", wait_until="domcontentloaded")
            if page.get_by_test_id("chat-inspector").is_hidden():
                page.get_by_test_id("toggle-inspector").click()
            page.get_by_test_id("tab-commands").click()
            out_pre = page.get_by_test_id(f"command-output-{cid}")
            expect(out_pre).to_be_visible()
            txt = out_pre.inner_text()
            assert txt.count("OUT_1") == 1
            assert "[1:stdout] OUT_1" in txt
            assert "[2:stderr] ERR_2" in txt
            expect(page.get_by_test_id(f"command-item-{cid}")).to_contain_text("truncated=true")

            # Stop 202 must NOT fabricate cancelled!
            page.get_by_test_id(f"stop-command-{cid}").click()
            expect(page.get_by_test_id(f"command-item-{cid}")).to_contain_text("停止中")
            assert "state=cancelled" not in page.get_by_test_id(f"command-item-{cid}").inner_text()

            # Read back real terminal cancelled + cleanup_ok=true + remaining=0
            cmd_state[0] = "cancelled"
            terminal_run[0] = True
            page.reload(wait_until="domcontentloaded")
            if page.get_by_test_id("chat-inspector").is_hidden():
                page.get_by_test_id("toggle-inspector").click()
            page.get_by_test_id("tab-commands").click()
            expect(page.get_by_test_id(f"command-item-{cid}")).to_contain_text("state=cancelled")
            expect(page.get_by_test_id(f"command-item-{cid}")).to_contain_text("cleanup_ok=true remaining=0")
            expect(page.get_by_test_id(f"stop-command-{cid}")).to_have_count(0)
            _ = reqs
            assert page_errors == []
            assert console_errors == []
            assert non_loopback == []
        finally:
            browser.close()


def test_u11_diff_and_check_provenance() -> None:
    p_id, s_id, r_id = _uid(1), _uid(11), _uid(101)
    result_dto = {
        "scope_complete": False,
        "changed_paths": [
            {"path": "src/a.py", "operation": "modify", "before_sha256": "1" * 64, "after_sha256": "2" * 64},
            {"path": "scripts/gen.txt", "operation": "create", "before_sha256": None, "after_sha256": "3" * 64},
        ],
        "diff": {
            "baseline_revision": "rev0",
            "final_revision": "rev1",
            "scope_complete": False,
            "excluded_note": "excluded .git and .env* directories",
            "diff_truncated": True,
            "entries": [
                {"path": "src/a.py", "operation": "modify", "source": "ai", "before_sha256": "1" * 64, "after_sha256": "2" * 64, "diff_excerpt": "@@ -1 +1 @@\n-old\n+new"},
                {"path": "scripts/gen.txt", "operation": "create", "source": "external_or_command", "before_sha256": None, "after_sha256": "3" * 64, "diff_excerpt": "+generated line"},
            ],
        },
        "checks": [
            {"check_id": "unit_tests", "kind": "junit_exact", "exit_code": 0, "junit_valid": False, "passed": False, "expected_tests": [{"classname": "t", "name": "t1"}, {"classname": "t", "name": "t2"}], "observed_tests": [{"classname": "t", "name": "t1"}], "failure_reason": "missing_junit_case"},
            {"check_id": "smoke_exit", "kind": "exit_code", "exit_code": 0, "junit_valid": False, "passed": True, "expected_tests": [], "observed_tests": [], "failure_reason": None},
        ],
        "unverified_reasons": ["junit report incomplete"],
    }

    with _StaticFrontendServer() as srv, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page_errors, console_errors, non_loopback, _ = _attach_watchers(page, base_url=srv.base_url)

            def _route(route: Any) -> None:
                path = route.request.url.split("://" + route.request.url.split("/")[2], 1)[1].split("?", 1)[0]
                if path == "/api/v1/projects":
                    route.fulfill(status=200, json=[_make_project(p_id, "proj-diff", kind="existing")])
                elif path == "/api/v1/sessions":
                    route.fulfill(status=200, json=[_make_session(s_id, p_id, active_run_id=r_id)])
                elif path.endswith("/context"):
                    route.fulfill(status=200, json=_make_context(s_id, p_id))
                elif path.endswith("/history"):
                    route.fulfill(status=200, json={"session_id": s_id, "items": [], "cursor": 0, "has_more": False})
                elif path.endswith("/snapshot"):
                    route.fulfill(status=200, json={"run": _make_run(r_id, s_id, p_id, state="failed", verification="failed", reason_code="VERIFICATION_FAILED", result=result_dto), "events": [], "cursor": 0, "truncated_before": 0, "pending_proposals": [], "pending_questions": []})
                elif path.endswith(("/proposals", "/questions")):
                    route.fulfill(status=200, json=[])
                elif path.endswith("/usage"):
                    route.fulfill(status=200, json={"calls_started": 0, "calls_completed": 0, "usage_known_calls": 0, "usage_unknown_calls": 0, "known_input_tokens": 0, "known_output_tokens": 0, "input_tokens": 0, "output_tokens": 0, "max_calls": 8, "steps_used": 0, "max_steps": 16, "elapsed_ms": 0, "run_timeout_ms": 60000, "estimated_cost_cny": None, "billed_cost_cny": None, "cost_status": "offline_not_billed"})
                else:
                    route.fulfill(status=200, json={})

            page.route("**/api/**", _route)
            page.goto(f"{srv.base_url}/#token={FAKE_TEST_TOKEN}", wait_until="domcontentloaded")

            expect(page.get_by_test_id("coverage-summary")).to_contain_text("unit_tests(junit_exact: junit_valid=false passed=false exit=0 observed=1/2 failure=missing_junit_case)")
            expect(page.get_by_test_id("coverage-summary")).to_contain_text("smoke_exit(exit_code: 仅检查退出码=0，非单元测试全过 exit=0 passed=true)")
            expect(page.get_by_test_id("coverage-summary")).to_contain_text("excluded .git and .env* directories")

            expect(page.get_by_test_id("diff-detail")).to_contain_text("src/a.py")
            expect(page.get_by_test_id("diff-detail")).to_contain_text("source=ai")
            expect(page.get_by_test_id("diff-detail")).to_contain_text("2" * 64)
            expect(page.get_by_test_id("diff-detail")).to_contain_text("节选截断=true")
            expect(page.get_by_test_id("diff-detail")).to_contain_text("不代表文件全文")

            if page.get_by_test_id("chat-inspector").is_hidden():
                page.get_by_test_id("toggle-inspector").click()
            page.get_by_test_id("diff-file-select").select_option("scripts/gen.txt")
            expect(page.get_by_test_id("diff-detail")).to_contain_text("scripts/gen.txt")
            expect(page.get_by_test_id("diff-detail")).to_contain_text("source=external_or_command")
            expect(page.get_by_test_id("diff-detail")).to_contain_text("3" * 64)
            expect(page.get_by_test_id("diff-detail")).to_contain_text("+generated line")
            assert page_errors == []
            assert console_errors == []
            assert non_loopback == []
        finally:
            browser.close()


def test_u12_http_errors_and_empty_loading() -> None:
    p_id = _uid(1)
    s1, s2 = _uid(11), _uid(12)
    r1 = _uid(101)
    with _StaticFrontendServer() as srv, sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page_errors, _, non_loopback, reqs = _attach_watchers(
                page, base_url=srv.base_url, allow_error_substrings=("401", "403", "409", "422", "500", "Failed")
            )
            mode = ["empty"]
            held_snap: list[Any] = []

            def _route(route: Any) -> None:
                path = route.request.url.split("://" + route.request.url.split("/")[2], 1)[1].split("?", 1)[0]
                m = route.request.method
                if mode[0] == "empty":
                    route.fulfill(status=200, json=[])
                    return
                if path == "/api/v1/projects":
                    route.fulfill(status=200, json=[_make_project(p_id, "proj-err")])
                elif path == "/api/v1/sessions" and m == "GET":
                    route.fulfill(status=200, json=[_make_session(s1, p_id, active_run_id=r1), _make_session(s2, p_id, active_run_id=None)])
                elif path.endswith("/context") and m == "GET":
                    sid = path.split("/")[4]
                    route.fulfill(status=200, json=_make_context(sid, p_id))
                elif path.endswith("/history"):
                    sid = path.split("/")[4]
                    route.fulfill(status=200, json={"session_id": sid, "items": [], "cursor": 0, "has_more": False})
                elif path == f"/api/v1/sessions/{s1}/runs/{r1}/snapshot":
                    if mode[0] == "loading":
                        held_snap.append(route)
                    else:
                        route.fulfill(status=200, json={"run": _make_run(r1, s1, p_id, state="succeeded", verification="passed", reason_code="OK"), "events": [], "cursor": 0, "truncated_before": 0, "pending_proposals": [], "pending_questions": []})
                elif path.endswith(("/proposals", "/questions")):
                    route.fulfill(status=200, json=[])
                elif path.endswith("/usage"):
                    route.fulfill(status=200, json={"calls_started": 0, "calls_completed": 0, "usage_known_calls": 0, "usage_unknown_calls": 0, "known_input_tokens": 0, "known_output_tokens": 0, "input_tokens": 0, "output_tokens": 0, "max_calls": 8, "steps_used": 0, "max_steps": 16, "elapsed_ms": 0, "run_timeout_ms": 60000, "estimated_cost_cny": None, "billed_cost_cny": None, "cost_status": "offline_not_billed"})
                elif path.endswith("/context") and m == "PUT":
                    code_map = {
                        "401": (401, "UNAUTHORIZED", "bad token"),
                        "403": (403, "FORBIDDEN", "forbidden"),
                        "409": (409, "CONFLICT", "rev conflict"),
                        "422": (422, "INVALID_INPUT", "too large"),
                        "500": (500, "INTERNAL_ERROR", "server boom"),
                    }
                    if mode[0] in code_map:
                        st, ec, msg = code_map[mode[0]]
                        route.fulfill(status=st, json={"error": {"code": ec, "message": msg}})
                    else:
                        route.abort("failed")
                else:
                    route.fulfill(status=200, json={})

            page.route("**/api/**", _route)
            # 1. Empty initial state
            page.goto(f"{srv.base_url}/#token={FAKE_TEST_TOKEN}", wait_until="domcontentloaded")
            expect(page.get_by_test_id("current-project")).to_have_text("-")
            expect(page.get_by_test_id("current-session")).to_have_text("-")
            expect(page.get_by_test_id("run-state")).to_have_text("idle")

            # 2. Loading state while snapshot held, and switching to s2 rejects stale 500 from held s1 snapshot
            mode[0] = "loading"
            page.reload(wait_until="domcontentloaded")
            expect(page.get_by_test_id("connection-state")).to_have_text("loading")
            page.get_by_test_id("nav-projects").click()
            page.get_by_test_id(f"session-item-{s2}").click()
            expect(page.get_by_test_id("current-session")).to_have_text(s2)
            expect(page.get_by_test_id("run-state")).to_have_text("idle")
            if held_snap:
                held_snap.pop(0).fulfill(status=500, json={"error": {"code": "STALE_500", "message": "stale"}})
            page.wait_for_timeout(100)
            expect(page.get_by_test_id("current-session")).to_have_text(s2)
            expect(page.get_by_test_id("error")).to_have_text("")

            # 3. 401/403/409/422/500 & network drop with ZERO automatic write retries
            page.get_by_test_id("nav-settings").click()
            page.get_by_test_id("settings-sec-constraints").click()
            for err_code, expected_text in [
                ("401", "UNAUTHORIZED"),
                ("403", "FORBIDDEN"),
                ("409", "CONFLICT"),
                ("422", "INVALID_INPUT"),
                ("500", "INTERNAL_ERROR"),
                ("abort", "NETWORK_ERROR"),
            ]:
                mode[0] = err_code
                puts_before = len([r for r in reqs if r["method"] == "PUT"])
                page.get_by_test_id("constraints-input").fill(f"val-{err_code}-{uuid.uuid4()}")
                page.get_by_test_id("save-constraints").click()
                expect(page.get_by_test_id("error")).to_contain_text(expected_text)
                page.wait_for_timeout(80)
                assert len([r for r in reqs if r["method"] == "PUT"]) == puts_before + 1

            assert page_errors == []
            assert non_loopback == []
        finally:
            browser.close()
