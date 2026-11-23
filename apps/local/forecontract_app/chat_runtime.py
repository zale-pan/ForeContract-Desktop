"""Offline read-only chat runtime, bounded context builder, and ProjectWorkspace tool loop."""

from __future__ import annotations

import inspect
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from forecontract_app.chat_models import (
    MAX_CHAT_ASSISTANT_TEXT_BYTES,
    MAX_CHAT_CONTEXT_BYTES,
    MAX_CHAT_EVIDENCE_EXCERPT_BYTES,
    MAX_CHAT_RECENT_TURNS_IN_CONTEXT,
    MAX_CHAT_TOOL_CALLS_PER_TURN,
    MAX_CHAT_TOOL_EXCERPT_BYTES,
    MAX_CHAT_TOOL_SUMMARY_BYTES,
    READONLY_CHAT_TOOLS,
    ChatIntentMode,
    ChatProviderRequest,
    ChatProviderResponse,
    ChatToolReadRecord,
    ChatTurnView,
    ResolvedChatEvidence,
)
from forecontract_app.models import AppError
from forecontract_app.project_models import ProjectError
from forecontract_app.project_workspace import ProjectWorkspace
from forecontract_app.session_models import truncate_utf8_safe

ChatTurnProvider = Callable[
    [ChatProviderRequest],
    ChatProviderResponse | dict[str, Any] | Awaitable[ChatProviderResponse | dict[str, Any]],
]
ChatProviderFactory = Callable[..., ChatTurnProvider]

_FILE_REF_RE = re.compile(r"([A-Za-z0-9_./-]+\.(?:py|js|cjs|ts|md|json|txt|yaml|yml|html|css))")
_TEST_DEF_RE = re.compile(r"^\s*def\s+(test_[A-Za-z0-9_]+)\s*\(", re.MULTILINE)
_PY_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.MULTILINE)
_SECRET_PATTERNS = (
    (re.compile(r"Bearer\s+[A-Za-z0-9._-]+", re.IGNORECASE), "Bearer [REDACTED]"),
    (re.compile(r"#token=[^\s\"'&]+", re.IGNORECASE), "#token=[REDACTED]"),
    (re.compile(r"(DEEPSEEK_API_KEY\s*=\s*)[^\s\"']+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"\bsk-[A-Za-z0-9]{12,}\b"), "sk-[REDACTED]"),
)


def sanitize_evidence_text(value: str) -> str:
    """Strip common credential/token patterns from bounded evidence excerpts."""
    out = value.replace("\x00", "")
    for pat, repl in _SECRET_PATTERNS:
        out = pat.sub(repl, out)
    return out


def _bound_str(value: str, max_bytes: int) -> tuple[str, bool]:
    clipped, cut = truncate_utf8_safe(value, max_bytes)
    return clipped, cut


def _request_utf8_bytes(req: ChatProviderRequest) -> int:
    return len(req.model_dump_json().encode("utf-8"))


def build_chat_provider_request(
    *,
    project_id: str,
    session_id: str,
    turn_id: str,
    constraints: str,
    question: str,
    completed_turns: list[ChatTurnView],
    tool_observations: list[dict[str, Any]],
    remaining_tool_budget: int,
    resolved_evidence: list[ResolvedChatEvidence] | None = None,
    max_bytes: int = MAX_CHAT_CONTEXT_BYTES,
    intent_mode: ChatIntentMode = "readonly",
) -> ChatProviderRequest:
    """Pack fixed constraints, verified evidence, recent turns, and tool observations within ``max_bytes``."""
    clipped_constraints, c_cut = _bound_str(constraints, 4096)
    clipped_question, q_cut = _bound_str(question, 4096)
    truncated = bool(c_cut or q_cut)

    base = ChatProviderRequest(
        project_id=project_id,
        session_id=session_id,
        turn_id=turn_id,
        intent_mode=intent_mode,
        constraints=clipped_constraints,
        question=clipped_question,
        recent_turns=[],
        evidence=[],
        tool_observations=[],
        context_truncated=truncated,
        remaining_tool_budget=remaining_tool_budget,
    )
    if _request_utf8_bytes(base) > max_bytes:
        raise AppError(
            "CONTEXT_BUDGET_EXCEEDED",
            "fixed chat context exceeds budget",
            status_code=422,
        )

    packed_evidence: list[dict[str, Any]] = []
    for ev in resolved_evidence or []:
        ev_dict = ev.model_dump(mode="json")
        raw_excerpt = sanitize_evidence_text(str(ev_dict.get("excerpt") or ""))
        bounded_excerpt, ex_cut = _bound_str(raw_excerpt, MAX_CHAT_EVIDENCE_EXCERPT_BYTES)
        ev_dict["excerpt"] = bounded_excerpt
        if ex_cut:
            ev_dict["truncated"] = True
            truncated = True
        cand = base.model_copy(
            update={"evidence": [*packed_evidence, ev_dict], "context_truncated": truncated}
        )
        if _request_utf8_bytes(cand) <= max_bytes:
            packed_evidence.append(ev_dict)
            continue
        # Shrink excerpt progressively so verified identity fields still fit within max_bytes.
        for cap in (512, 192, 0):
            shrunk, _ = _bound_str(bounded_excerpt, cap)
            ev_dict["excerpt"] = shrunk
            ev_dict["truncated"] = True
            truncated = True
            cand_shrunk = base.model_copy(
                update={"evidence": [*packed_evidence, ev_dict], "context_truncated": True}
            )
            if _request_utf8_bytes(cand_shrunk) <= max_bytes:
                packed_evidence.append(ev_dict)
                break

    packed_obs: list[dict[str, Any]] = []
    for obs in tool_observations:
        cand_obs = [*packed_obs, obs]
        cand = base.model_copy(
            update={
                "evidence": packed_evidence,
                "tool_observations": cand_obs,
                "context_truncated": truncated,
            }
        )
        if _request_utf8_bytes(cand) <= max_bytes:
            packed_obs = cand_obs
        else:
            truncated = True

    recent_pool = list(completed_turns[-MAX_CHAT_RECENT_TURNS_IN_CONTEXT:])
    if len(completed_turns) > len(recent_pool):
        truncated = True

    selected_desc: list[dict[str, str]] = []
    for turn in reversed(recent_pool):
        u_msg, u_cut = _bound_str(turn.user_message, 1024)
        a_msg, a_cut = _bound_str(turn.assistant_message or "", 1536)
        if u_cut or a_cut:
            truncated = True
        entry = {
            "seq": str(turn.seq),
            "user_message": u_msg,
            "assistant_message": a_msg,
        }
        cand_turns = list(reversed([*selected_desc, entry]))
        cand = base.model_copy(
            update={
                "recent_turns": cand_turns,
                "evidence": packed_evidence,
                "tool_observations": packed_obs,
                "context_truncated": truncated,
            }
        )
        if _request_utf8_bytes(cand) <= max_bytes:
            selected_desc.append(entry)
        else:
            truncated = True
            break

    final_turns = list(reversed(selected_desc))
    return base.model_copy(
        update={
            "recent_turns": final_turns,
            "evidence": packed_evidence,
            "tool_observations": packed_obs,
            "context_truncated": truncated,
        }
    )


def _execute_readonly_tool(
    *,
    ws: ProjectWorkspace,
    step: int,
    tool_name: str,
    tool_args: dict[str, Any],
) -> tuple[ChatToolReadRecord, dict[str, Any]]:
    if tool_name not in READONLY_CHAT_TOOLS:
        raise AppError(
            "CHAT_TOOL_FORBIDDEN",
            f"tool '{tool_name}' is not allowed in read-only chat",
            status_code=403,
        )
    safe_args = dict(tool_args)
    try:
        if tool_name == "list_directory":
            rel_path = str(safe_args.get("path", "."))
            limit = int(safe_args.get("limit", 50))
            res = ws.list_directory(path=rel_path, limit=min(max(limit, 1), 100))
            entries = [
                {"name": e.name, "kind": e.kind, "size_bytes": e.size_bytes}
                for e in res.entries[:30]
            ]
            summary, cut = _bound_str(
                f"List {res.directory} ({len(res.entries)} entries)",
                MAX_CHAT_TOOL_SUMMARY_BYTES,
            )
            record = ChatToolReadRecord(
                step=step,
                tool_name="list_directory",
                args={"path": res.directory, "limit": limit},
                ok=True,
                summary=summary,
                path=res.directory,
                truncated=bool(res.truncated or cut),
            )
            obs = {
                "step": step,
                "tool_name": "list_directory",
                "ok": True,
                "path": res.directory,
                "entries": entries,
                "truncated": res.truncated,
            }
            return record, obs

        if tool_name == "find_paths":
            pattern = str(safe_args.get("pattern", "*"))
            limit = int(safe_args.get("limit", 50))
            res = ws.find_paths(pattern=pattern, limit=min(max(limit, 1), 100))
            summary, cut = _bound_str(
                f"Find {pattern} ({len(res.paths)} paths)",
                MAX_CHAT_TOOL_SUMMARY_BYTES,
            )
            record = ChatToolReadRecord(
                step=step,
                tool_name="find_paths",
                args={"pattern": pattern, "limit": limit},
                ok=True,
                summary=summary,
                truncated=bool(res.truncated or cut),
            )
            obs = {
                "step": step,
                "tool_name": "find_paths",
                "ok": True,
                "pattern": pattern,
                "paths": list(res.paths[:40]),
                "truncated": res.truncated,
            }
            return record, obs

        if tool_name == "read_file":
            rel_path = str(safe_args.get("path", ""))
            start_line = int(safe_args.get("start_line", 1))
            max_lines = int(safe_args.get("max_lines", 120))
            byte_offset = int(safe_args.get("byte_offset", 0))
            res = ws.read_file(
                path=rel_path,
                start_line=start_line,
                max_lines=min(max(max_lines, 1), 200),
                byte_offset=byte_offset,
            )
            end_line = res.start_line + max(res.line_count - 1, 0)
            excerpt, excerpt_cut = _bound_str(res.content, MAX_CHAT_TOOL_EXCERPT_BYTES)
            summary, sum_cut = _bound_str(
                f"Read {res.path} (L{res.start_line}-{end_line}, sha:{res.sha256[:12]})",
                MAX_CHAT_TOOL_SUMMARY_BYTES,
            )
            record = ChatToolReadRecord(
                step=step,
                tool_name="read_file",
                args={"path": res.path, "start_line": res.start_line, "max_lines": max_lines},
                ok=True,
                summary=summary,
                path=res.path,
                start_line=res.start_line,
                end_line=end_line,
                sha256=res.sha256,
                truncated=bool(res.truncated or excerpt_cut or sum_cut),
            )
            obs = {
                "step": step,
                "tool_name": "read_file",
                "ok": True,
                "path": res.path,
                "start_line": res.start_line,
                "end_line": end_line,
                "sha256": res.sha256,
                "content": excerpt,
                "truncated": bool(res.truncated or excerpt_cut),
            }
            return record, obs

        # search_text
        query = str(safe_args.get("query", ""))
        paths_arg = safe_args.get("paths")
        paths_filter = [str(p) for p in paths_arg] if isinstance(paths_arg, list) else None
        max_matches = int(safe_args.get("max_matches", 20))
        res = ws.search_text(
            query=query,
            paths=paths_filter,
            max_matches=min(max(max_matches, 1), 50),
        )
        matches = [
            {
                "path": m.path,
                "line_number": m.line_number,
                "line_text": _bound_str(m.line_text, 240)[0],
                "sha256": m.file_sha256,
            }
            for m in res.matches[:15]
        ]
        summary, cut = _bound_str(
            f"Search '{query}' ({len(res.matches)} matches)",
            MAX_CHAT_TOOL_SUMMARY_BYTES,
        )
        record = ChatToolReadRecord(
            step=step,
            tool_name="search_text",
            args={"query": query, "paths": paths_filter},
            ok=True,
            summary=summary,
            truncated=bool(res.truncated or cut),
        )
        obs = {
            "step": step,
            "tool_name": "search_text",
            "ok": True,
            "query": query,
            "matches": matches,
            "truncated": res.truncated,
        }
        return record, obs

    except ProjectError as exc:
        err_summary, _ = _bound_str(
            f"{tool_name} rejected ({exc.code}): {exc.message}",
            MAX_CHAT_TOOL_SUMMARY_BYTES,
        )
        record = ChatToolReadRecord(
            step=step,
            tool_name=tool_name,  # type: ignore[arg-type]
            args=safe_args,
            ok=False,
            summary=err_summary,
            path=str(safe_args.get("path")) if "path" in safe_args else None,
            error_code=exc.code,
        )
        obs = {
            "step": step,
            "tool_name": tool_name,
            "ok": False,
            "error_code": exc.code,
            "message": exc.message,
        }
        return record, obs


class OfflineReadonlyChatProvider:
    """Deterministic offline read-only provider grounded in actual ProjectWorkspace reads."""

    def __call__(self, req: ChatProviderRequest) -> ChatProviderResponse:
        if req.evidence:
            parts: list[str] = []
            if req.recent_turns:
                last_turn = req.recent_turns[-1]
                parts.append(
                    f"承接上一轮（#{last_turn.get('seq', '?')}）关于“{last_turn.get('user_message', '')[:48]}”的讨论："
                )
            parts.append(f"基于已核验的 {len(req.evidence)} 项运行证据回答：")
            for ev in req.evidence:
                kind = ev.get("kind")
                run_id = str(ev.get("run_id", ""))
                avail = str(ev.get("availability", "available"))
                excerpt = str(ev.get("excerpt") or "").strip()
                if kind == "diff_hunk":
                    b_sha = str(ev.get("before_sha256") or "none")[:12]
                    a_sha = str(ev.get("after_sha256") or "none")[:12]
                    line_info = f"old L{ev.get('old_start')} (+{ev.get('old_count')}) -> new L{ev.get('new_start')} (+{ev.get('new_count')})"
                    detail = f"- [Diff] `{ev.get('path')}` (run:{run_id[:8]}, sha:{b_sha}->{a_sha}, {line_info})"
                    if excerpt:
                        detail += f"\n  历史片段：\n{excerpt}"
                    parts.append(detail)
                elif kind == "command":
                    cid = str(ev.get("command_id", ""))
                    exit_code = ev.get("exit_code")
                    passed = ev.get("passed")
                    argv = " ".join(str(a) for a in (ev.get("argv") or []))
                    detail = f"- [Command] `{cid}` (run:{run_id[:8]}, exit={exit_code}, passed={passed}, argv=`{argv}`)"
                    if avail == "raw_output_unavailable":
                        detail += f"；状态：{ev.get('note') or '原始输出不可用'}"
                        if ev.get("failure_reason"):
                            detail += f"，失败原因：{ev.get('failure_reason')}"
                    elif excerpt:
                        detail += f"\n  输出节选：\n{excerpt}"
                    parts.append(detail)
            if req.constraints.strip():
                parts.append(f"已遵守会话约束：{req.constraints.strip()[:120]}")
            return ChatProviderResponse(action="final", assistant_message="\n".join(parts))

        obs_by_tool = [o for o in req.tool_observations if o.get("ok") is True]
        found_paths: list[str] = []
        for o in obs_by_tool:
            if o.get("tool_name") == "find_paths":
                found_paths = [str(p) for p in o.get("paths", [])]
                break

        if not any(o.get("tool_name") == "find_paths" for o in req.tool_observations):
            return ChatProviderResponse(
                action="tool_call",
                tool_name="find_paths",
                tool_args={"pattern": "*", "limit": 40},
            )

        already_read = {
            str(o.get("path"))
            for o in req.tool_observations
            if o.get("tool_name") == "read_file" and o.get("path")
        }

        if req.remaining_tool_budget > 0 and len(already_read) < 3:
            mentioned = _FILE_REF_RE.findall(req.question)
            for prev in reversed(req.recent_turns):
                mentioned.extend(_FILE_REF_RE.findall(prev.get("user_message", "")))

            q_lower = req.question.lower()
            asks_tests = any(
                kw in q_lower for kw in ("test", "pytest", "测试", "覆盖", "coverage")
            )

            candidates: list[str] = []
            for m in mentioned:
                for fp in found_paths:
                    if (fp == m or fp.endswith("/" + m) or m.endswith("/" + fp)) and fp not in candidates:
                        candidates.append(fp)

            if asks_tests:
                for fp in found_paths:
                    name = fp.rsplit("/", 1)[-1]
                    if (name.startswith("test_") or name.endswith("_test.py")) and fp not in candidates:
                        candidates.append(fp)

            for fp in found_paths:
                if fp.endswith(".py") and fp not in candidates:
                    candidates.append(fp)

            for fp in found_paths:
                if fp not in candidates:
                    candidates.append(fp)

            for target in candidates:
                if target not in already_read:
                    return ChatProviderResponse(
                        action="tool_call",
                        tool_name="read_file",
                        tool_args={"path": target, "start_line": 1, "max_lines": 120},
                    )

        read_obs = [
            o for o in obs_by_tool if o.get("tool_name") == "read_file"
        ]
        parts: list[str] = []
        if req.recent_turns:
            last_turn = req.recent_turns[-1]
            parts.append(
                f"承接上一轮（#{last_turn.get('seq', '?')}）关于“{last_turn.get('user_message', '')[:48]}”的讨论："
            )

        if found_paths:
            parts.append(
                f"当前工作区共发现 {len(found_paths)} 个可读文件：{', '.join(found_paths[:8])}。"
            )
        else:
            parts.append("当前工作区未发现可读取的普通文件。")

        for ro in read_obs:
            r_path = str(ro.get("path", ""))
            r_sha = str(ro.get("sha256", ""))[:12]
            s_line = ro.get("start_line", 1)
            e_line = ro.get("end_line", 1)
            content = str(ro.get("content", ""))
            test_names = _TEST_DEF_RE.findall(content)
            def_names = _PY_DEF_RE.findall(content)
            first_code_lines = [
                ln.strip()
                for ln in content.splitlines()
                if ln.strip() and not ln.strip().startswith("#")
            ][:3]
            detail = f"- `{r_path}` (L{s_line}-L{e_line}, sha:{r_sha})"
            if test_names:
                detail += f"：包含静态测试函数 {', '.join(test_names[:6])}"
            elif def_names:
                detail += f"：包含定义 {', '.join(def_names[:6])}"
            if first_code_lines:
                detail += f"；关键片段：`{' | '.join(first_code_lines)}`"
            parts.append(detail)

        if req.constraints.strip():
            parts.append(f"已遵守会话约束：{req.constraints.strip()[:120]}")

        parts.append(
            "注：以上结论仅基于离线只读模式下的静态源码读取，尚未在当前对话轮中运行测试命令。"
        )
        return ChatProviderResponse(
            action="final",
            assistant_message="\n".join(parts),
        )


async def execute_conversation_chat_turn(
    *,
    project_id: str,
    session_id: str,
    turn_id: str,
    project_root: Path,
    data_root: Path,
    constraints: str,
    question: str,
    completed_turns: list[ChatTurnView],
    provider: ChatTurnProvider,
    resolved_evidence: list[ResolvedChatEvidence] | None = None,
    intent_mode: ChatIntentMode = "readonly",
) -> tuple[
    list[ChatToolReadRecord],
    str | None,
    tuple[str, str] | None,
    bool,
    str | None,
    str | None,
]:
    """Run one bounded chat turn supporting optional execution_offer in conversation mode."""
    ws = ProjectWorkspace(root=project_root, data_root=data_root)
    tool_reads: list[ChatToolReadRecord] = []
    observations: list[dict[str, Any]] = []
    context_truncated = False

    for step in range(1, MAX_CHAT_TOOL_CALLS_PER_TURN + 2):
        remaining = max(0, MAX_CHAT_TOOL_CALLS_PER_TURN - len(tool_reads))
        try:
            req = build_chat_provider_request(
                project_id=project_id,
                session_id=session_id,
                turn_id=turn_id,
                constraints=constraints,
                question=question,
                completed_turns=completed_turns,
                tool_observations=observations,
                remaining_tool_budget=remaining,
                resolved_evidence=resolved_evidence,
                intent_mode=intent_mode,
            )
            context_truncated = context_truncated or req.context_truncated
            raw_resp = provider(req)
            if inspect.isawaitable(raw_resp):
                raw_resp = await raw_resp
            resp = ChatProviderResponse.model_validate(raw_resp)
        except AppError as exc:
            return tool_reads, None, None, context_truncated, exc.code, exc.message
        except Exception as exc:  # noqa: BLE001
            return (
                tool_reads,
                None,
                None,
                context_truncated,
                "CHAT_PROVIDER_FAILED",
                f"read-only provider failed: {type(exc).__name__}",
            )

        if resp.action == "final":
            if not isinstance(resp.assistant_message, str) or not resp.assistant_message.strip():
                return (
                    tool_reads,
                    None,
                    None,
                    context_truncated,
                    "CHAT_PROVIDER_INVALID",
                    "provider returned empty assistant_message",
                )
            bounded_msg, _ = _bound_str(resp.assistant_message.strip(), MAX_CHAT_ASSISTANT_TEXT_BYTES)
            return tool_reads, bounded_msg, None, context_truncated, None, None

        if resp.action == "execution_offer":
            if intent_mode != "conversation":
                return (
                    tool_reads,
                    None,
                    None,
                    context_truncated,
                    "CHAT_PROVIDER_INVALID",
                    "execution_offer is not permitted in readonly intent_mode",
                )
            assert resp.goal is not None and resp.summary is not None
            return (
                tool_reads,
                None,
                (resp.goal.strip(), resp.summary.strip()),
                context_truncated,
                None,
                None,
            )

        if len(tool_reads) >= MAX_CHAT_TOOL_CALLS_PER_TURN:
            return (
                tool_reads,
                None,
                None,
                context_truncated,
                "TOOL_BUDGET_EXCEEDED",
                f"read-only chat turn exceeded {MAX_CHAT_TOOL_CALLS_PER_TURN} tool calls",
            )

        if not resp.tool_name:
            return (
                tool_reads,
                None,
                None,
                context_truncated,
                "CHAT_PROVIDER_INVALID",
                "provider requested tool_call without tool_name",
            )

        try:
            record, obs = _execute_readonly_tool(
                ws=ws,
                step=step,
                tool_name=resp.tool_name,
                tool_args=resp.tool_args,
            )
        except AppError as exc:
            return tool_reads, None, None, context_truncated, exc.code, exc.message

        tool_reads.append(record)
        observations.append(obs)

    return (
        tool_reads,
        None,
        None,
        context_truncated,
        "TOOL_BUDGET_EXCEEDED",
        f"read-only chat turn exceeded {MAX_CHAT_TOOL_CALLS_PER_TURN} tool calls",
    )


async def execute_readonly_chat_turn(
    *,
    project_id: str,
    session_id: str,
    turn_id: str,
    project_root: Path,
    data_root: Path,
    constraints: str,
    question: str,
    completed_turns: list[ChatTurnView],
    provider: ChatTurnProvider,
    resolved_evidence: list[ResolvedChatEvidence] | None = None,
) -> tuple[list[ChatToolReadRecord], str | None, bool, str | None, str | None]:
    """Run one bounded read-only chat turn and return ``(tool_reads, assistant_message, context_truncated, error_code, error_message)``."""
    tool_reads, msg, _offer, ctx_cut, err_code, err_msg = await execute_conversation_chat_turn(
        project_id=project_id,
        session_id=session_id,
        turn_id=turn_id,
        project_root=project_root,
        data_root=data_root,
        constraints=constraints,
        question=question,
        completed_turns=completed_turns,
        provider=provider,
        resolved_evidence=resolved_evidence,
        intent_mode="readonly",
    )
    return tool_reads, msg, ctx_cut, err_code, err_msg
