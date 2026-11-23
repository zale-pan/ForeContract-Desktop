"""Single-active Job-Object command runner with incremental UTF-8 streaming and bounded 1 MiB/16 KiB pagination."""

from __future__ import annotations

import asyncio
import codecs
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from forecontract_coding.errors import CodingError
from forecontract_coding.models import CommandSpec, ExpectedTestCase
from forecontract_coding.process import ProcessExecutionResult, run_command_in_job

from forecontract_app.project_models import (
    PROJECT_MAX_COMMAND_CHUNKS,
    PROJECT_MAX_COMMAND_OUTPUT_BYTES,
    PROJECT_MAX_COMMAND_PAGE_BYTES,
    CommandChunk,
    CommandProposal,
    CommandState,
    CommandView,
    ExpectedTestCaseView,
    ProjectError,
    VerificationKind,
)
from forecontract_app.project_permissions import PermissionBroker
from forecontract_app.security import ensure_clean_directory


@dataclass
class _CommandRecord:
    view: CommandView
    task: asyncio.Task[None] | None = None
    done_event: asyncio.Event = field(default_factory=asyncio.Event)
    chunks: deque[CommandChunk] = field(default_factory=deque)
    stdout_retained_bytes: int = 0
    stderr_retained_bytes: int = 0
    evicted_before_seq: int = 0
    seq_counter: int = 0
    stdout_decoder: codecs.IncrementalDecoder = field(
        default_factory=lambda: codecs.getincrementaldecoder("utf-8")(errors="replace")
    )
    stderr_decoder: codecs.IncrementalDecoder = field(
        default_factory=lambda: codecs.getincrementaldecoder("utf-8")(errors="replace")
    )
    cancel_requested: bool = False
    raw_output_failed: bool = False
    result_dir: Path | None = None
    stdout_full_for_checks: str = ""
    stderr_full_for_checks: str = ""


def build_command_spec_for_check(
    *,
    command_id: str,
    proposal: CommandProposal,
    resolved_cwd: Path | str = ".",
    check_kind: VerificationKind | None = None,
    expected_tests: list[ExpectedTestCaseView] | None = None,
    result_dir: Path | None = None,
) -> tuple[CommandSpec, list[str]]:
    """Build Coding ``CommandSpec`` and expanded argv for one approved command/check."""
    is_junit_exact = (
        proposal.purpose == "verification"
        and check_kind == "junit_exact"
        and bool(expected_tests)
    )
    if is_junit_exact:
        assert expected_tests is not None
        spec_purpose: Any = "acceptance"
        spec_expected = [
            ExpectedTestCase(classname=tc.classname, name=tc.name) for tc in expected_tests
        ]
    else:
        spec_purpose = "diagnostic"
        spec_expected = []

    expanded_args = [
        arg.replace("{result_dir}", str(result_dir)) if result_dir is not None else arg
        for arg in proposal.argv
    ]
    spec = CommandSpec(
        command_id=command_id,
        executable=proposal.executable,
        args=list(proposal.argv),
        cwd=str(resolved_cwd),
        env={},
        timeout_ms=proposal.timeout_ms,
        purpose=spec_purpose,
        expected_tests=spec_expected,
    )
    return spec, expanded_args


class ProjectCommandRunner:
    """Manages approved native subprocess commands within one run (max 1 active command)."""

    def __init__(
        self,
        *,
        run_id: str,
        project_root: Path,
        data_root: Path,
        broker: PermissionBroker,
        emit: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._run_id = run_id
        self._project_root = project_root
        self._data_root = data_root
        self._broker = broker
        self._emit = emit
        self._commands: dict[str, _CommandRecord] = {}
        self._order: list[str] = []

    def has_running_command(self) -> bool:
        return any(rec.view.state in ("starting", "running") for rec in self._commands.values())

    def active_command_ids(self) -> list[str]:
        return [
            cid for cid in self._order if self._commands[cid].view.state in ("starting", "running")
        ]

    def cleanup_summary(self) -> tuple[bool, int]:
        cleanup_ok = all(rec.view.cleanup_ok for rec in self._commands.values())
        rem = sum(rec.view.remaining_active for rec in self._commands.values())
        return cleanup_ok, rem

    def list_commands(self) -> list[CommandView]:
        return [self.get_command(cid, after=self._commands[cid].evicted_before_seq) for cid in self._order]

    def _emit_event(self, kind: str, payload: dict[str, Any]) -> None:
        if self._emit is not None:
            self._emit(kind, payload)

    def _split_chunks(self, text: str) -> list[str]:
        if not text:
            return []
        out: list[str] = []
        buf: list[str] = []
        cur_bytes = 0
        for ch in text:
            cb = len(ch.encode("utf-8"))
            if cur_bytes + cb > 4096 and buf:
                out.append("".join(buf))
                buf = [ch]
                cur_bytes = cb
            else:
                buf.append(ch)
                cur_bytes += cb
        if buf:
            out.append("".join(buf))
        return out

    def _append_chunk_text(self, rec: _CommandRecord, stream: str, text: str, raw_byte_count: int) -> None:
        s_literal: Any = "stdout" if stream == "stdout" else "stderr"
        rec.view = rec.view.model_copy(
            update={"bytes_seen": rec.view.bytes_seen + raw_byte_count}
        )
        if not text:
            return

        for piece in self._split_chunks(text):
            piece_bytes = len(piece.encode("utf-8"))
            rec.seq_counter += 1
            chunk = CommandChunk(seq=rec.seq_counter, stream=s_literal, text=piece)
            rec.chunks.append(chunk)
            if s_literal == "stdout":
                rec.stdout_retained_bytes += piece_bytes
                if len(rec.stdout_full_for_checks) < 128 * 1024:
                    rec.stdout_full_for_checks += piece
            else:
                rec.stderr_retained_bytes += piece_bytes
                if len(rec.stderr_full_for_checks) < 128 * 1024:
                    rec.stderr_full_for_checks += piece

            # Enforce 1 MiB per stream and max chunk ring cap cleanly.
            while len(rec.chunks) > PROJECT_MAX_COMMAND_CHUNKS or (
                rec.stdout_retained_bytes > PROJECT_MAX_COMMAND_OUTPUT_BYTES
                or rec.stderr_retained_bytes > PROJECT_MAX_COMMAND_OUTPUT_BYTES
            ):
                if not rec.chunks:
                    break
                evicted = rec.chunks.popleft()
                eb = len(evicted.text.encode("utf-8"))
                rec.evicted_before_seq = max(rec.evicted_before_seq, evicted.seq)
                if evicted.stream == "stdout":
                    rec.stdout_retained_bytes = max(0, rec.stdout_retained_bytes - eb)
                    rec.view = rec.view.model_copy(
                        update={
                            "truncated": True,
                            "truncated_stdout_bytes": rec.view.truncated_stdout_bytes + eb,
                        }
                    )
                else:
                    rec.stderr_retained_bytes = max(0, rec.stderr_retained_bytes - eb)
                    rec.view = rec.view.model_copy(
                        update={
                            "truncated": True,
                            "truncated_stderr_bytes": rec.view.truncated_stderr_bytes + eb,
                        }
                    )

            first_seq = rec.chunks[0].seq if rec.chunks else rec.seq_counter
            rec.view = rec.view.model_copy(
                update={"first_seq": first_seq, "last_seq": rec.seq_counter}
            )
            self._emit_event(
                "command_output",
                {
                    "command_id": rec.view.command_id,
                    "run_id": self._run_id,
                    "seq": chunk.seq,
                    "stream": chunk.stream,
                    "text": chunk.text,
                },
            )

    def _on_raw_chunk_threadsafe(
        self,
        loop: asyncio.AbstractEventLoop,
        rec: _CommandRecord,
        stream: str,
        raw: bytes,
    ) -> None:
        def _deliver() -> None:
            try:
                dec = rec.stdout_decoder if stream == "stdout" else rec.stderr_decoder
                text = dec.decode(raw, final=False)
                self._append_chunk_text(rec, stream, text, len(raw))
            except Exception:  # noqa: BLE001
                rec.raw_output_failed = True
                rec.view = rec.view.model_copy(update={"output_delivery_failed": True})

        if loop.is_closed():
            rec.raw_output_failed = True
            return
        loop.call_soon_threadsafe(_deliver)

    def start_approved_command(
        self,
        *,
        proposal_id: str,
        proposal_digest: str | None = None,
        check_kind: VerificationKind | None = None,
        expected_tests: list[ExpectedTestCaseView] | None = None,
        current_root_identity: str | None = None,
        current_instruction_sha256: str | None = None,
        current_instruction_files: dict[str, str] | None = None,
        executable: str | None = None,
        argv: list[str] | None = None,
        cwd: str | None = None,
        timeout_ms: int | None = None,
    ) -> CommandView:
        if self.has_running_command():
            raise ProjectError(
                "COMMAND_IN_PROGRESS",
                "another command is currently running in this session run",
                status_code=409,
            )

        receipt = self._broker.consume_command_grant(
            proposal_id=proposal_id,
            proposal_digest=proposal_digest,
            current_run_id=self._run_id,
            current_root_identity=current_root_identity,
            current_instruction_sha256=current_instruction_sha256,
            current_instruction_files=current_instruction_files,
            executable=executable,
            argv=argv,
            cwd=cwd,
            timeout_ms=timeout_ms,
        )
        cmd = receipt.command
        cmd_id = cmd.command_id or f"cmd-{len(self._order) + 1:04d}"
        if cmd_id in self._commands:
            cmd_id = f"{cmd_id}-{len(self._order) + 1}"

        cmd_dir = self._data_root / "runs" / self._run_id / "commands" / cmd_id
        ensure_clean_directory(cmd_dir, label="command_data_dir")
        temp_dir = cmd_dir / "tmp"
        result_dir = cmd_dir / "result"
        ensure_clean_directory(temp_dir, label="command_temp_dir")
        ensure_clean_directory(result_dir, label="command_result_dir")

        spec, expanded_args = build_command_spec_for_check(
            command_id=cmd_id,
            proposal=cmd,
            resolved_cwd=receipt.resolved_cwd,
            check_kind=check_kind,
            expected_tests=expected_tests,
            result_dir=result_dir,
        )
        full_argv = (cmd.executable, *expanded_args)

        view = CommandView(
            command_id=cmd_id,
            run_id=self._run_id,
            proposal_digest=receipt.proposal_digest,
            state="running",
            pid=None,
            exit_code=None,
            cleanup_ok=True,
            remaining_active=0,
            first_seq=0,
            last_seq=0,
            chunks=[],
            bytes_seen=0,
            truncated=False,
            output_delivery_failed=False,
        )
        rec = _CommandRecord(view=view, result_dir=result_dir)
        self._commands[cmd_id] = rec
        self._order.append(cmd_id)

        loop = asyncio.get_running_loop()
        self._emit_event(
            "command_started",
            {
                "command_id": cmd_id,
                "run_id": self._run_id,
                "proposal_digest": receipt.proposal_digest,
                "state": "running",
            },
        )

        async def _runner() -> None:
            final_state: CommandState = "unknown"
            exit_code: int | None = None
            cleanup_ok = True
            remaining_active = 0
            timed_out = False
            delivery_failed = False
            stdout_trunc_bytes = 0
            stderr_trunc_bytes = 0
            try:
                res: ProcessExecutionResult = await run_command_in_job(
                    spec=spec,
                    argv=full_argv,
                    temp_dir=temp_dir,
                    max_output_bytes=PROJECT_MAX_COMMAND_OUTPUT_BYTES,
                    process_cleanup_ms=2000,
                    on_output=lambda s, b: self._on_raw_chunk_threadsafe(loop, rec, s, b),
                )
                # Flush remaining incremental decoder bytes and any pending call_soon callbacks.
                await asyncio.sleep(0)
                tail_out = rec.stdout_decoder.decode(b"", final=True)
                tail_err = rec.stderr_decoder.decode(b"", final=True)
                if tail_out:
                    self._append_chunk_text(rec, "stdout", tail_out, 0)
                if tail_err:
                    self._append_chunk_text(rec, "stderr", tail_err, 0)

                exit_code = res.exit_code
                cleanup_ok = bool(res.cleanup_ok)
                remaining_active = int(res.active_processes_remaining)
                timed_out = bool(res.timed_out)
                delivery_failed = bool(res.output_delivery_failed or rec.raw_output_failed)
                if not cleanup_ok or remaining_active != 0:
                    final_state = "unknown"
                elif timed_out:
                    final_state = "timed_out"
                elif res.cancelled or rec.cancel_requested:
                    final_state = "cancelled"
                elif exit_code is not None:
                    final_state = "exited"
                else:
                    final_state = "unknown"

                if res.stdout_truncated and rec.view.truncated_stdout_bytes == 0:
                    stdout_trunc_bytes = max(1, len(res.stdout.encode("utf-8")) // 10)
                if res.stderr_truncated and rec.view.truncated_stderr_bytes == 0:
                    stderr_trunc_bytes = max(1, len(res.stderr.encode("utf-8")) // 10)
                if res.stdout and not rec.stdout_full_for_checks:
                    rec.stdout_full_for_checks = res.stdout
                if res.stderr and not rec.stderr_full_for_checks:
                    rec.stderr_full_for_checks = res.stderr
            except asyncio.CancelledError:
                await asyncio.sleep(0)
                cleanup_ok = True
                remaining_active = 0
                delivery_failed = rec.raw_output_failed
                final_state = "cancelled"
            except CodingError as exc:
                await asyncio.sleep(0)
                cleanup_ok = exc.code != "CODING_PROCESS_CLEANUP_FAILED"
                remaining_active = 1 if not cleanup_ok else 0
                delivery_failed = rec.raw_output_failed
                final_state = "unknown" if not cleanup_ok else (
                    "cancelled" if rec.cancel_requested else "unknown"
                )
            except Exception:  # noqa: BLE001
                cleanup_ok = True
                remaining_active = 0
                delivery_failed = True
                final_state = "unknown"
            finally:
                rec.view = rec.view.model_copy(
                    update={
                        "state": final_state,
                        "exit_code": exit_code,
                        "cleanup_ok": cleanup_ok,
                        "remaining_active": remaining_active,
                        "timed_out": timed_out,
                        "output_delivery_failed": delivery_failed or rec.view.output_delivery_failed,
                        "truncated": (
                            rec.view.truncated
                            or stdout_trunc_bytes > 0
                            or stderr_trunc_bytes > 0
                        ),
                        "truncated_stdout_bytes": (
                            rec.view.truncated_stdout_bytes + stdout_trunc_bytes
                        ),
                        "truncated_stderr_bytes": (
                            rec.view.truncated_stderr_bytes + stderr_trunc_bytes
                        ),
                    }
                )
                rec.done_event.set()
                self._emit_event(
                    "command_finished",
                    {
                        "command_id": cmd_id,
                        "run_id": self._run_id,
                        "proposal_digest": receipt.proposal_digest,
                        "state": rec.view.state,
                        "exit_code": rec.view.exit_code,
                        "cleanup_ok": rec.view.cleanup_ok,
                        "remaining_active": rec.view.remaining_active,
                    },
                )

        rec.task = asyncio.create_task(_runner())
        return rec.view.model_copy(deep=True)

    async def wait_command(self, command_id: str) -> CommandView:
        rec = self._commands.get(command_id)
        if rec is None:
            raise ProjectError("NOT_FOUND", "command not found", status_code=404)
        if not rec.done_event.is_set() and rec.task is not None and not rec.task.done():
            await asyncio.gather(rec.task, return_exceptions=True)
        await rec.done_event.wait()
        return self.get_command(command_id, after=rec.evicted_before_seq)

    async def run_approved_command(
        self,
        *,
        proposal_id: str,
        proposal_digest: str | None = None,
        check_kind: VerificationKind | None = None,
        expected_tests: list[ExpectedTestCaseView] | None = None,
        current_root_identity: str | None = None,
        current_instruction_sha256: str | None = None,
        current_instruction_files: dict[str, str] | None = None,
        executable: str | None = None,
        argv: list[str] | None = None,
        cwd: str | None = None,
        timeout_ms: int | None = None,
    ) -> CommandView:
        started = self.start_approved_command(
            proposal_id=proposal_id,
            proposal_digest=proposal_digest,
            check_kind=check_kind,
            expected_tests=expected_tests,
            current_root_identity=current_root_identity,
            current_instruction_sha256=current_instruction_sha256,
            current_instruction_files=current_instruction_files,
            executable=executable,
            argv=argv,
            cwd=cwd,
            timeout_ms=timeout_ms,
        )
        return await self.wait_command(started.command_id)

    def get_result_dir(self, command_id: str) -> Path | None:
        rec = self._commands.get(command_id)
        return rec.result_dir if rec is not None else None

    def get_captured_output(self, command_id: str) -> tuple[str, str]:
        rec = self._commands.get(command_id)
        if rec is None:
            return "", ""
        return rec.stdout_full_for_checks, rec.stderr_full_for_checks

    def get_command(self, command_id: str, *, after: int = 0) -> CommandView:
        rec = self._commands.get(command_id)
        if rec is None:
            raise ProjectError("NOT_FOUND", "command not found", status_code=404)
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ProjectError("INVALID_CURSOR", "after must be a non-negative integer", status_code=422)
        if after > rec.seq_counter:
            raise ProjectError(
                "INVALID_CURSOR",
                f"after={after} exceeds last available sequence {rec.seq_counter}",
                status_code=422,
            )
        if rec.evicted_before_seq > 0 and after < rec.evicted_before_seq:
            raise ProjectError(
                "CURSOR_EXPIRED",
                f"sequence {after} evicted from bounded command ring",
                status_code=409,
            )

        page_chunks: list[CommandChunk] = []
        page_bytes = 0
        for chunk in rec.chunks:
            if chunk.seq <= after:
                continue
            cb = len(chunk.text.encode("utf-8"))
            if page_chunks and page_bytes + cb > PROJECT_MAX_COMMAND_PAGE_BYTES:
                break
            page_chunks.append(chunk)
            page_bytes += cb

        first_seq = rec.chunks[0].seq if rec.chunks else 0
        return rec.view.model_copy(
            update={
                "chunks": page_chunks,
                "first_seq": first_seq,
                "last_seq": rec.seq_counter,
            }
        )

    async def stop_command(self, command_id: str) -> tuple[CommandView, bool]:
        rec = self._commands.get(command_id)
        if rec is None:
            raise ProjectError("NOT_FOUND", "command not found", status_code=404)
        if rec.done_event.is_set() and rec.view.state not in ("starting", "running"):
            return self.get_command(command_id, after=rec.evicted_before_seq), True

        rec.cancel_requested = True
        if rec.task is not None and not rec.task.done():
            rec.task.cancel()
            await asyncio.gather(rec.task, return_exceptions=True)
        if not rec.done_event.is_set():
            rec.view = rec.view.model_copy(
                update={"state": "cancelled", "cleanup_ok": True, "remaining_active": 0}
            )
            rec.done_event.set()
        await rec.done_event.wait()
        return self.get_command(command_id, after=rec.evicted_before_seq), False

    async def stop_all(self) -> None:
        for cid in list(self._order):
            rec = self._commands[cid]
            if not rec.done_event.is_set():
                await self.stop_command(cid)
