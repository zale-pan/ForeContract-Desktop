"""FastAPI application factory, strict security boundary middleware, and SSE routes."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from starlette.datastructures import Headers, MutableHeaders, QueryParams
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from forecontract_app.chat_models import (
    ChatTurnsPage,
    ChatTurnView,
    ConfirmChatOfferRequest,
    CreateChatTurnRequest,
    ProjectSessionTitlesView,
)
from forecontract_app.chat_runtime import ChatProviderFactory
from forecontract_app.events import _SubscriberQueue
from forecontract_app.extension_models import (
    ExtensionProposalDecision,
    ExtensionProposalView,
    McpServerSummary,
    RunExtensionsView,
    SkillInfo,
)
from forecontract_app.models import (
    MAX_REQUEST_BODY_BYTES,
    AppError,
    AppEvent,
    CancelRunRequest,
    CreateProjectRequest,
    CreateSessionRequest,
    HostConfig,
    ProjectView,
    ProviderFactory,
    RunSnapshot,
    RunView,
    SessionView,
    StartRunRequest,
)
from forecontract_app.project_models import (
    CommandView,
    ListDirectoryResult,
    ProjectError,
    ProposalDecision,
    ProposalView,
    ReadFileResult,
    RegisterProjectRequest,
    StopCommandRequest,
)
from forecontract_app.project_review import ProjectGitDiffView, ProjectGitStatusView
from forecontract_app.security import (
    SECURITY_RESPONSE_HEADERS,
    validate_bearer_token,
    validate_content_length_header,
    validate_host_header,
    validate_origin_and_fetch_metadata,
)
from forecontract_app.service import AppService
from forecontract_app.session_models import (
    AnswerQuestionRequest,
    HistoryPage,
    QuestionView,
    SessionContextView,
    SetConstraintsRequest,
    UsageView,
)

_STATIC_DIR = Path(__file__).resolve().parent / "static"


def _error_json(status_code: int, code: str, message: str) -> JSONResponse:
    resp = JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )
    for k, v in SECURITY_RESPONSE_HEADERS.items():
        resp.headers[k] = v
    return resp


def _parse_non_negative_strict_int(raw: str) -> int:
    if raw.startswith(("+", "-")) or not raw.isdigit():
        raise AppError("INVALID_CURSOR", "after must be a non-negative integer", status_code=422)
    return int(raw, 10)


def _parse_bounded_strict_int(
    raw: str, *, label: str, minimum: int, maximum: int, code: str = "INVALID_INPUT"
) -> int:
    if raw.startswith(("+", "-")) or not raw.isdigit():
        raise AppError(code, f"{label} must be an integer in {minimum}..{maximum}", status_code=422)
    val = int(raw, 10)
    if val < minimum or val > maximum:
        raise AppError(code, f"{label} must be an integer in {minimum}..{maximum}", status_code=422)
    return val


def _reject_nonfinite_json_constant(const_str: str) -> Any:
    raise AppError("INVALID_INPUT", f"non-finite JSON constant forbidden: {const_str}", status_code=422)


class _SecurityBoundaryMiddleware:
    """Pure ASGI security boundary enforcing Host, Origin, bearer token, headers, and 64 KiB cap."""

    def __init__(self, app: ASGIApp, *, config: HostConfig) -> None:
        self._app = app
        self._config = config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        response_started = False

        async def _send_with_headers(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                hdrs = MutableHeaders(scope=message)
                for key, value in SECURITY_RESPONSE_HEADERS.items():
                    hdrs[key] = value
            await send(message)

        try:
            headers = Headers(scope=scope)
            method = str(scope.get("method", "GET")).upper()
            path = str(scope.get("path", ""))
            query_string = scope.get("query_string", b"")
            query_keys = set(
                QueryParams(
                    query_string.decode("latin-1")
                    if isinstance(query_string, (bytes, bytearray))
                    else str(query_string)
                ).keys()
            )

            validate_host_header(headers.get("host"), self._config.bind_port)
            validate_origin_and_fetch_metadata(
                method=method,
                origin=headers.get("origin"),
                sec_fetch_site=headers.get("sec-fetch-site"),
                bind_port=self._config.bind_port,
            )
            validate_content_length_header(headers.get("content-length"))

            next_receive: Receive = receive
            # Bound and replay request body without BaseHTTPMiddleware receive mismatch.
            if method in {"POST", "PUT", "PATCH"}:
                chunks: list[bytes] = []
                total = 0
                while True:
                    msg = await receive()
                    msg_type = msg.get("type")
                    if msg_type == "http.disconnect":
                        break
                    if msg_type == "http.request":
                        chunk = msg.get("body", b"")
                        total += len(chunk)
                        if total > MAX_REQUEST_BODY_BYTES:
                            raise AppError(
                                "PAYLOAD_TOO_LARGE",
                                "request body exceeds 64 KiB limit",
                                status_code=413,
                            )
                        chunks.append(chunk)
                        if not msg.get("more_body", False):
                            break
                cached_body = b"".join(chunks)
                if cached_body.strip():
                    ct = (headers.get("content-type") or "").split(";", 1)[0].strip().lower()
                    if ct in ("", "application/json") or ct.endswith("+json"):
                        try:
                            json.loads(
                                cached_body.decode("utf-8"),
                                parse_constant=_reject_nonfinite_json_constant,
                            )
                        except UnicodeDecodeError as exc:
                            raise AppError(
                                "INVALID_INPUT", "request body must be UTF-8 JSON", status_code=422
                            ) from exc
                        except json.JSONDecodeError:
                            pass
                replayed = False

                async def _replay_receive() -> Message:
                    nonlocal replayed
                    if not replayed:
                        replayed = True
                        return {"type": "http.request", "body": cached_body, "more_body": False}
                    return await receive()

                next_receive = _replay_receive

            if path.startswith("/api/"):
                validate_bearer_token(
                    authorization=headers.get("authorization"),
                    expected_token=self._config.access_token,
                    query_keys=query_keys,
                )

            await self._app(scope, next_receive, _send_with_headers)
        except AppError as exc:
            if not response_started:
                await _error_json(exc.status_code, exc.code, exc.message)(scope, receive, send)
        except Exception:  # noqa: BLE001 - never expose internal tracebacks or tokens
            if not response_started:
                await _error_json(500, "INTERNAL_ERROR", "internal server error")(
                    scope, receive, send
                )


def create_app(
    *,
    config: HostConfig,
    provider_factory: ProviderFactory | None = None,
    chat_provider_factory: ChatProviderFactory | None = None,
) -> FastAPI:
    """Build the FastAPI local host with lifespan-owned ``AppService`` and strict guards."""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        service = AppService(
            config=config,
            provider_factory=provider_factory,
            chat_provider_factory=chat_provider_factory,
        )
        application.state.service = service
        try:
            yield
        finally:
            await service.aclose()

    app = FastAPI(
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(_SecurityBoundaryMiddleware, config=config)

    @app.exception_handler(AppError)
    async def _handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        return _error_json(exc.status_code, exc.code, exc.message)

    @app.exception_handler(ProjectError)
    async def _handle_project_error(_: Request, exc: ProjectError) -> JSONResponse:
        return _error_json(exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    @app.exception_handler(ValidationError)
    async def _handle_validation_error(_: Request, __: Exception) -> JSONResponse:
        return _error_json(422, "INVALID_INPUT", "request validation failed")

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exc(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = "NOT_FOUND" if exc.status_code == 404 else "HTTP_ERROR"
        msg = "resource not found" if exc.status_code == 404 else "request rejected"
        return _error_json(exc.status_code, code, msg)

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, __: Exception) -> JSONResponse:
        return _error_json(500, "INTERNAL_ERROR", "internal server error")

    def _service(request: Request) -> AppService:
        return request.app.state.service  # type: ignore[no-any-return]

    @app.get("/", include_in_schema=False)
    async def _serve_index() -> Response:
        return Response(
            content=(_STATIC_DIR / "index.html").read_bytes(),
            media_type="text/html; charset=utf-8",
        )

    @app.get("/app.js", include_in_schema=False)
    async def _serve_app_js() -> Response:
        return Response(
            content=(_STATIC_DIR / "app.js").read_bytes(),
            media_type="application/javascript; charset=utf-8",
        )

    @app.get("/i18n.js", include_in_schema=False)
    async def _serve_i18n_js() -> Response:
        return Response(
            content=(_STATIC_DIR / "i18n.js").read_bytes(),
            media_type="application/javascript; charset=utf-8",
        )

    @app.get("/style.css", include_in_schema=False)
    async def _serve_style_css() -> Response:
        return Response(
            content=(_STATIC_DIR / "style.css").read_bytes(),
            media_type="text/css; charset=utf-8",
        )

    @app.get("/api/v1/health")
    async def _health(request: Request) -> dict[str, Any]:
        if config.live is not None:
            return {
                "status": "ok",
                "mode": "live",
                "live_enabled": True,
                "provider": "deepseek",
                "model": config.live.model,
                "readiness": "ready",
            }
        svc = _service(request)
        test_injected = (
            getattr(svc, "_provider_factory", None) is not None
            or getattr(svc, "_chat_provider_factory", None) is not None
        )
        if test_injected:
            return {
                "status": "ok",
                "mode": "offline",
                "live_enabled": False,
                "test_provider_injected": True,
            }
        return {
            "status": "ok",
            "mode": "unconfigured",
            "live_enabled": False,
            "test_provider_injected": False,
            "readiness": "missing_live_config",
        }

    @app.post("/api/v1/projects", status_code=201, response_model=ProjectView)
    async def _create_project(payload: CreateProjectRequest, request: Request) -> ProjectView:
        return await _service(request).create_project(name=payload.name)

    @app.post("/api/v1/projects/register", status_code=201, response_model=ProjectView)
    async def _register_project(payload: RegisterProjectRequest, request: Request) -> ProjectView:
        return await _service(request).register_project(request=payload)

    @app.get("/api/v1/projects", response_model=list[ProjectView])
    async def _list_projects(request: Request) -> list[ProjectView]:
        return _service(request).list_projects()

    @app.post("/api/v1/sessions", status_code=201, response_model=SessionView)
    async def _create_session(payload: CreateSessionRequest, request: Request) -> SessionView:
        return _service(request).create_session(
            project_id=payload.project_id,
            model=payload.model,
        )

    @app.get("/api/v1/sessions", response_model=list[SessionView])
    async def _list_sessions(request: Request) -> list[SessionView]:
        return _service(request).list_sessions()

    @app.get("/api/v1/sessions/{sid}/history", response_model=HistoryPage)
    async def _get_session_history(
        sid: str,
        request: Request,
        after: str = Query(default="0"),
        limit: str = Query(default="50"),
    ) -> HistoryPage:
        parsed_after = _parse_non_negative_strict_int(after)
        parsed_limit = _parse_bounded_strict_int(limit, label="limit", minimum=1, maximum=100)
        return _service(request).get_session_history(
            session_id=sid, after=parsed_after, limit=parsed_limit
        )

    @app.get("/api/v1/sessions/{sid}/context", response_model=SessionContextView)
    async def _get_session_context(sid: str, request: Request) -> SessionContextView:
        return _service(request).get_session_context(session_id=sid)

    @app.post("/api/v1/sessions/{sid}/chat/turns")
    async def _create_chat_turn(
        sid: str, payload: CreateChatTurnRequest, request: Request
    ) -> JSONResponse:
        view, status_code = await _service(request).start_chat_turn(
            session_id=sid,
            request=payload,
        )
        return JSONResponse(
            status_code=status_code,
            content=json.loads(view.model_dump_json()),
        )

    @app.get("/api/v1/sessions/{sid}/chat/turns", response_model=ChatTurnsPage)
    async def _list_chat_turns(
        sid: str,
        request: Request,
        after_seq: str = Query(default="0"),
        limit: str = Query(default="50"),
    ) -> ChatTurnsPage:
        parsed_after = _parse_non_negative_strict_int(after_seq)
        parsed_limit = _parse_bounded_strict_int(limit, label="limit", minimum=1, maximum=100)
        return _service(request).list_chat_turns(
            session_id=sid,
            after_seq=parsed_after,
            limit=parsed_limit,
        )

    @app.get(
        "/api/v1/sessions/{sid}/chat/turns/by-request/{client_request_id}",
        response_model=ChatTurnView,
    )
    async def _get_chat_turn_by_request(
        sid: str, client_request_id: str, request: Request
    ) -> ChatTurnView:
        try:
            parsed_uuid = str(uuid.UUID(client_request_id))
        except ValueError as exc:
            raise AppError("INVALID_INPUT", "client_request_id must be a valid UUID", status_code=422) from exc
        return _service(request).get_chat_turn_by_client_request_id(
            session_id=sid,
            client_request_id=parsed_uuid,
        )

    @app.post("/api/v1/sessions/{sid}/chat/turns/{turn_id}/confirm")
    async def _confirm_chat_offer(
        sid: str, turn_id: str, payload: ConfirmChatOfferRequest, request: Request
    ) -> JSONResponse:
        run_view, replayed = await _service(request).confirm_chat_execution_offer(
            session_id=sid,
            turn_id=turn_id,
            request=payload,
        )
        return JSONResponse(
            status_code=200 if replayed else 201,
            content=json.loads(run_view.model_dump_json()),
        )

    @app.get("/api/v1/projects/{pid}/chat/session-titles", response_model=ProjectSessionTitlesView)
    async def _list_project_session_titles(
        pid: str, request: Request
    ) -> ProjectSessionTitlesView:
        return _service(request).list_project_session_titles(project_id=pid)

    @app.put("/api/v1/sessions/{sid}/context", response_model=SessionContextView)
    async def _set_session_constraints(
        sid: str, payload: SetConstraintsRequest, request: Request
    ) -> SessionContextView:
        return _service(request).set_session_constraints(session_id=sid, request=payload)

    @app.get("/api/v1/sessions/{sid}/runs/{rid}/questions", response_model=list[QuestionView])
    async def _list_questions(sid: str, rid: str, request: Request) -> list[QuestionView]:
        return _service(request).list_questions(session_id=sid, run_id=rid)

    @app.post(
        "/api/v1/sessions/{sid}/runs/{rid}/questions/{qid}/answer",
        response_model=QuestionView,
    )
    async def _answer_question(
        sid: str,
        rid: str,
        qid: str,
        payload: AnswerQuestionRequest,
        request: Request,
    ) -> QuestionView:
        return _service(request).answer_question(
            session_id=sid, run_id=rid, question_id=qid, request=payload
        )

    @app.get("/api/v1/sessions/{sid}/runs/{rid}/usage", response_model=UsageView)
    async def _get_run_usage(sid: str, rid: str, request: Request) -> UsageView:
        return _service(request).get_run_usage(session_id=sid, run_id=rid)

    @app.post("/api/v1/sessions/{sid}/runs")
    async def _start_run(
        sid: str, payload: StartRunRequest, request: Request
    ) -> JSONResponse:
        view, is_replay = await _service(request).start_run_with_replay(
            session_id=sid,
            request=payload,
        )
        return JSONResponse(
            status_code=200 if is_replay else 202,
            content=json.loads(view.model_dump_json()),
        )

    @app.get("/api/v1/sessions/{sid}/runs/{rid}", response_model=RunView)
    async def _get_run(sid: str, rid: str, request: Request) -> RunView:
        return _service(request).get_run(session_id=sid, run_id=rid)

    @app.post("/api/v1/sessions/{sid}/runs/{rid}/cancel")
    async def _cancel_run(
        sid: str, rid: str, payload: CancelRunRequest, request: Request
    ) -> JSONResponse:
        _ = payload
        view, already_terminal = await _service(request).cancel_run_with_status(
            session_id=sid,
            run_id=rid,
        )
        return JSONResponse(
            status_code=200 if already_terminal else 202,
            content=json.loads(view.model_dump_json()),
        )

    @app.get("/api/v1/projects/{pid}/skills", response_model=list[SkillInfo])
    async def _list_project_skills(pid: str, request: Request) -> list[SkillInfo]:
        return _service(request).list_project_skills(project_id=pid)

    @app.get("/api/v1/projects/{pid}/files", response_model=ListDirectoryResult)
    async def _list_project_files(
        pid: str,
        request: Request,
        path: str = Query(default="."),
        cursor: str | None = Query(default=None),
        limit: str = Query(default="100"),
    ) -> ListDirectoryResult:
        parsed_limit = _parse_bounded_strict_int(limit, label="limit", minimum=1, maximum=100)
        return _service(request).list_project_files(
            project_id=pid,
            path=path,
            cursor=cursor,
            limit=parsed_limit,
        )

    @app.get("/api/v1/projects/{pid}/file", response_model=ReadFileResult)
    async def _read_project_file(
        pid: str,
        request: Request,
        path: str = Query(...),
        start_line: str = Query(default="1"),
        byte_offset: str = Query(default="0"),
        expected_sha256: str | None = Query(default=None),
    ) -> ReadFileResult:
        parsed_start = _parse_bounded_strict_int(
            start_line, label="start_line", minimum=1, maximum=10_000_000
        )
        parsed_offset = _parse_non_negative_strict_int(byte_offset)
        return _service(request).read_project_file(
            project_id=pid,
            path=path,
            start_line=parsed_start,
            byte_offset=parsed_offset,
            expected_sha256=expected_sha256,
        )

    @app.get("/api/v1/projects/{pid}/git/status", response_model=ProjectGitStatusView)
    async def _get_project_git_status(pid: str, request: Request) -> ProjectGitStatusView:
        return _service(request).get_project_git_status(project_id=pid)

    @app.get("/api/v1/projects/{pid}/git/diff", response_model=ProjectGitDiffView)
    async def _get_project_git_diff(
        pid: str,
        request: Request,
        path: str = Query(...),
        area: Literal["index", "worktree"] = Query(...),
    ) -> ProjectGitDiffView:
        return _service(request).get_project_git_diff(
            project_id=pid,
            path=path,
            area=area,
        )

    @app.get("/api/v1/extensions/servers", response_model=list[McpServerSummary])
    async def _list_extension_servers(request: Request) -> list[McpServerSummary]:
        return _service(request).list_extension_servers()

    @app.get(
        "/api/v1/sessions/{sid}/runs/{rid}/extensions",
        response_model=RunExtensionsView,
    )
    async def _get_run_extensions(
        sid: str, rid: str, request: Request
    ) -> RunExtensionsView:
        return _service(request).get_run_extensions(session_id=sid, run_id=rid)

    @app.post(
        "/api/v1/sessions/{sid}/runs/{rid}/extensions/proposals/{eid}/decision",
        response_model=ExtensionProposalView,
    )
    async def _decide_extension_proposal(
        sid: str,
        rid: str,
        eid: str,
        payload: ExtensionProposalDecision,
        request: Request,
    ) -> ExtensionProposalView:
        return await _service(request).decide_extension_proposal(
            session_id=sid,
            run_id=rid,
            proposal_id=eid,
            request=payload,
        )

    @app.get("/api/v1/sessions/{sid}/runs/{rid}/snapshot", response_model=RunSnapshot)
    async def _snapshot(sid: str, rid: str, request: Request) -> RunSnapshot:
        return _service(request).snapshot(session_id=sid, run_id=rid)

    @app.get(
        "/api/v1/sessions/{sid}/runs/{rid}/proposals",
        response_model=list[ProposalView],
    )
    async def _list_proposals(sid: str, rid: str, request: Request) -> list[ProposalView]:
        return _service(request).get_proposals(session_id=sid, run_id=rid)

    @app.post(
        "/api/v1/sessions/{sid}/runs/{rid}/proposals/{pid}/decision",
        response_model=ProposalView,
    )
    async def _decide_proposal(
        sid: str,
        rid: str,
        pid: str,
        payload: ProposalDecision,
        request: Request,
    ) -> ProposalView:
        return await _service(request).decide_proposal(
            session_id=sid,
            run_id=rid,
            proposal_id=pid,
            request=payload,
        )

    @app.get(
        "/api/v1/sessions/{sid}/runs/{rid}/commands/{cid}",
        response_model=CommandView,
    )
    async def _get_command(
        sid: str,
        rid: str,
        cid: str,
        request: Request,
        after: str = Query(default="0"),
    ) -> CommandView:
        parsed_after = _parse_non_negative_strict_int(after)
        return _service(request).get_command(
            session_id=sid,
            run_id=rid,
            command_id=cid,
            after=parsed_after,
        )

    @app.post("/api/v1/sessions/{sid}/runs/{rid}/commands/{cid}/stop")
    async def _stop_command(
        sid: str,
        rid: str,
        cid: str,
        payload: StopCommandRequest,
        request: Request,
    ) -> JSONResponse:
        _ = payload
        view, already_terminal = await _service(request).stop_command_with_status(
            session_id=sid,
            run_id=rid,
            command_id=cid,
        )
        return JSONResponse(
            status_code=200 if already_terminal else 202,
            content=json.loads(view.model_dump_json()),
        )

    @app.get("/api/v1/sessions/{sid}/runs/{rid}/events")
    async def _events(
        sid: str,
        rid: str,
        request: Request,
        after: str = Query(default="0"),
    ) -> StreamingResponse:
        parsed_after = _parse_non_negative_strict_int(after)
        hub, replay, sub = _service(request).open_event_subscription(
            session_id=sid,
            run_id=rid,
            after=parsed_after,
        )

        async def _sse_generator() -> AsyncIterator[bytes]:
            try:
                for ev in replay:
                    yield _format_sse(ev)
                    if ev.kind == "run_finished":
                        return
                if sub is None:
                    return
                assert isinstance(sub, _SubscriberQueue)
                while True:
                    if await request.is_disconnected():
                        return
                    try:
                        item = await asyncio.wait_for(sub.queue.get(), timeout=10.0)
                    except asyncio.TimeoutError:
                        yield b": heartbeat\n\n"
                        continue
                    if item is None:
                        if sub.overflowed:
                            yield b"event: resync_required\ndata: {\"reason\":\"slow_subscriber\"}\n\n"
                        return
                    yield _format_sse(item)
                    if item.kind == "run_finished":
                        return
            finally:
                if sub is not None:
                    hub.close_subscription(sub)

        return StreamingResponse(
            _sse_generator(),
            media_type="text/event-stream; charset=utf-8",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    return app


def _format_sse(event: AppEvent) -> bytes:
    return f"id: {event.seq}\nevent: {event.kind}\ndata: {event.model_dump_json()}\n\n".encode()
