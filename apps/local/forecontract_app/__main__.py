"""CLI entrypoint for launching the local offline ForeContract web host."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import math
import os
import secrets
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from forecontract_app.models import AppError, HostConfig, LiveConfig
from forecontract_app.security import validate_host_config
from forecontract_app.server import create_app
from forecontract_app.service import validate_project_roots


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="forecontract_app",
        description="ForeContract local offline visualization host",
    )
    parser.add_argument("--data-root", required=True, help="Absolute path for data root")
    parser.add_argument(
        "--workspace-parent", required=True, help="Absolute path for example workspaces parent"
    )
    parser.add_argument("--port", type=int, default=8765, help="Loopback bind port on 127.0.0.1")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--offline",
        action="store_true",
        default=False,
        help="Run in bounded offline mode (default when no mode flag is given)",
    )
    mode.add_argument(
        "--live-deepseek",
        action="store_true",
        default=False,
        help="Run with the live DeepSeek provider (host-owned credentials from DEEPSEEK_API_KEY)",
    )
    parser.add_argument(
        "--live-max-requests",
        type=int,
        default=8,
        help="Maximum total live DeepSeek requests for this host authorization (default: 8)",
    )
    parser.add_argument(
        "--live-budget-cny",
        type=float,
        default=1.0,
        help="Maximum local CNY reservation budget for this host authorization (default: 1.0)",
    )
    parser.add_argument(
        "--control-stdin",
        action="store_true",
        default=False,
        help="Wait for a 'stop' line on stdin to trigger graceful server exit",
    )
    parser.add_argument(
        "--project-root",
        action="append",
        default=[],
        help="Exact allowlisted local directory root allowed for existing-project registration",
    )
    parser.add_argument(
        "--extension-config",
        default=None,
        help="Optional absolute path to UTF-8 JSON MCP extension configuration",
    )
    return parser


class _BoundUvicornServer(uvicorn.Server):
    """Uvicorn Server subclass emitting the startup URL fragment only once actually listening."""

    def __init__(self, uvicorn_config: uvicorn.Config, *, startup_line: str) -> None:
        super().__init__(uvicorn_config)
        self._startup_line = startup_line

    async def startup(self, sockets: list[object] | None = None) -> None:
        await super().startup(sockets=sockets)  # type: ignore[arg-type]
        if self.started:
            sys.stdout.write(self._startup_line + "\n")
            sys.stdout.flush()


async def _serve_async(
    *,
    config: HostConfig,
    control_stdin: bool,
) -> int:
    app = create_app(config=config)
    uv_config = uvicorn.Config(
        app=app,
        host="127.0.0.1",
        port=config.bind_port,
        workers=1,
        reload=False,
        access_log=False,
        log_level="critical",
        timeout_graceful_shutdown=8,
    )
    startup_line = f"FORECONTRACT_APP_URL=http://127.0.0.1:{config.bind_port}/#token={config.access_token}"
    server = _BoundUvicornServer(uv_config, startup_line=startup_line)

    stop_thread: threading.Thread | None = None
    if control_stdin:
        loop = asyncio.get_running_loop()

        def _watch_stdin() -> None:
            while not server.should_exit:
                line = sys.stdin.readline()
                if line == "":
                    break
                if line.strip() == "stop":
                    loop.call_soon_threadsafe(setattr, server, "should_exit", True)
                    break

        stop_thread = threading.Thread(target=_watch_stdin, daemon=True)
        stop_thread.start()

    try:
        await server.serve()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
        return code if code != 0 else 1
    if not server.started:
        return 1
    _ = stop_thread
    return 0


def _run_server_coro(config: HostConfig, *, control_stdin: bool) -> int:
    if sys.platform == "win32":
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            return runner.run(_serve_async(config=config, control_stdin=control_stdin))
    return asyncio.run(_serve_async(config=config, control_stdin=control_stdin))


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2

    token = secrets.token_urlsafe(32)
    try:
        ext_path = Path(args.extension_config) if args.extension_config else None
        if ext_path is not None:
            from forecontract_app.extension_models import load_extension_config

            load_extension_config(path=ext_path)
        live: LiveConfig | None = None
        if bool(args.live_deepseek):
            api_key = os.environ.get("DEEPSEEK_API_KEY")
            if not api_key or not api_key.strip():
                raise AppError(
                    "LIVE_CONFIG_MISSING",
                    "DEEPSEEK_API_KEY is required for --live-deepseek",
                    status_code=503,
                )
            max_reqs = int(args.live_max_requests)
            budget_cny = float(args.live_budget_cny)
            if max_reqs < 1:
                raise AppError(
                    "INVALID_INPUT",
                    "--live-max-requests must be >= 1",
                    status_code=422,
                )
            if not math.isfinite(budget_cny) or budget_cny <= 0:
                raise AppError(
                    "INVALID_INPUT",
                    "--live-budget-cny must be a positive finite number",
                    status_code=422,
                )
            live = LiveConfig(
                api_key=api_key,
                model="deepseek-flash",
                max_requests=max_reqs,
                budget_cny=budget_cny,
            )
        config = HostConfig(
            data_root=Path(args.data_root),
            workspace_parent=Path(args.workspace_parent),
            python_executable=Path(sys.executable),
            bind_port=int(args.port),
            access_token=token,
            project_roots=(),
            extension_config=ext_path,
            live=live,
        )
        validate_host_config(config)
        validated_roots = validate_project_roots(config, raw_strings=args.project_root)
        config = dataclasses.replace(config, project_roots=validated_roots)
    except AppError as exc:
        sys.stderr.write(f"ERROR[{exc.code}]: {exc.message}\n")
        sys.stderr.flush()
        return 2

    try:
        return _run_server_coro(config=config, control_stdin=bool(args.control_stdin))
    except Exception:  # noqa: BLE001
        sys.stderr.write("ERROR[SERVER_START_FAILED]: host terminated with error\n")
        sys.stderr.flush()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
