"""A02, A03 & A06: Local access boundaries, static/body limits, and root/reparse safety."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest
from app_helpers import (
    LiveTcpTestServer,
    auth_headers,
    make_host_config,
    pick_free_loopback_port,
    tree_hashes,
)
from forecontract_app.models import AppError, HostConfig
from forecontract_app.security import CSP_HEADER_VALUE
from forecontract_app.service import AppService


def test_a02_local_access(tmp_path: Path) -> None:
    cfg = make_host_config(tmp_path)
    factory_calls = 0

    def _factory(*, project: object, run_id: str) -> object:
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("provider must never be invoked")

    with LiveTcpTestServer(config=cfg, provider_factory=_factory) as srv:  # type: ignore[arg-type]
        base = srv.base_url
        unauth = httpx.get(
            f"{base}/api/v1/health",
            headers={"Host": f"127.0.0.1:{cfg.bind_port}"},
            timeout=5.0,
        )
        wrong_token = httpx.get(
            f"{base}/api/v1/health",
            headers={
                "Host": f"127.0.0.1:{cfg.bind_port}",
                "Authorization": "Bearer wrong-token-value",
            },
            timeout=5.0,
        )
        query_token = httpx.get(
            f"{base}/api/v1/health?token={cfg.access_token}",
            headers=auth_headers(cfg, with_origin=False),
            timeout=5.0,
        )
        bad_host = httpx.get(
            f"{base}/api/v1/health",
            headers={
                "Host": "evil.example",
                "Authorization": f"Bearer {cfg.access_token}",
            },
            timeout=5.0,
        )
        foreign_origin = httpx.post(
            f"{base}/api/v1/projects",
            headers={
                "Host": f"127.0.0.1:{cfg.bind_port}",
                "Authorization": f"Bearer {cfg.access_token}",
                "Origin": "http://evil.example",
            },
            json={"name": "p1"},
            timeout=5.0,
        )
        missing_write_origin = httpx.post(
            f"{base}/api/v1/projects",
            headers=auth_headers(cfg, with_origin=False),
            json={"name": "p1"},
            timeout=5.0,
        )
        cross_site = httpx.get(
            f"{base}/api/v1/health",
            headers={
                "Host": f"127.0.0.1:{cfg.bind_port}",
                "Authorization": f"Bearer {cfg.access_token}",
                "Sec-Fetch-Site": "cross-site",
            },
            timeout=5.0,
        )
        valid_health = httpx.get(
            f"{base}/api/v1/health",
            headers=auth_headers(cfg, with_origin=False),
            timeout=5.0,
        )

        assert unauth.status_code == 401
        assert wrong_token.status_code == 401
        assert query_token.status_code == 401
        assert bad_host.status_code == 403
        assert foreign_origin.status_code == 403
        assert missing_write_origin.status_code == 403
        assert cross_site.status_code == 403
        assert "access-control-allow-origin" not in {
            k.lower() for k in valid_health.headers
        }
        assert valid_health.status_code == 200
        assert valid_health.json() == {"status": "ok", "mode": "offline", "live_enabled": False}
        assert factory_calls == 0


def test_a03_static_and_body_boundary(tmp_path: Path) -> None:
    cfg = make_host_config(tmp_path)
    sentinel_text = "TOP_SECRET_SENTINEL_FILE_CONTENT_DO_NOT_SERVE"
    sentinel_file = cfg.data_root / "sentinel.txt"
    sentinel_file.write_text(sentinel_text, encoding="utf-8")

    with LiveTcpTestServer(config=cfg) as srv:
        base = srv.base_url
        h = auth_headers(cfg)
        oversized = {"name": "a" * 70000}
        over_64k = httpx.post(
            f"{base}/api/v1/projects",
            headers=h | {"Content-Type": "application/json"},
            content=json.dumps(oversized).encode("utf-8"),
            timeout=5.0,
        )
        traversal = httpx.get(
            f"{base}/..%2Fdata%2Fsentinel.txt",
            headers={"Host": f"127.0.0.1:{cfg.bind_port}"},
            timeout=5.0,
        )
        raw_data_url = httpx.get(
            f"{base}/data/sentinel.txt",
            headers={"Host": f"127.0.0.1:{cfg.bind_port}"},
            timeout=5.0,
        )
        docs_url = httpx.get(
            f"{base}/docs",
            headers={"Host": f"127.0.0.1:{cfg.bind_port}"},
            timeout=5.0,
        )
        openapi_url = httpx.get(
            f"{base}/openapi.json",
            headers={"Host": f"127.0.0.1:{cfg.bind_port}"},
            timeout=5.0,
        )
        index_resp = httpx.get(
            f"{base}/",
            headers={"Host": f"127.0.0.1:{cfg.bind_port}"},
            timeout=5.0,
        )
        js_resp = httpx.get(
            f"{base}/app.js",
            headers={"Host": f"127.0.0.1:{cfg.bind_port}"},
            timeout=5.0,
        )
        css_resp = httpx.get(
            f"{base}/style.css",
            headers={"Host": f"127.0.0.1:{cfg.bind_port}"},
            timeout=5.0,
        )

        assert over_64k.status_code == 413
        assert traversal.status_code in (400, 404)
        assert raw_data_url.status_code == 404
        assert docs_url.status_code == 404
        assert openapi_url.status_code == 404
        assert index_resp.status_code == 200
        assert js_resp.status_code == 200
        assert css_resp.status_code == 200

        all_responses = b"".join(
            r.content
            for r in (
                over_64k,
                traversal,
                raw_data_url,
                docs_url,
                openapi_url,
                index_resp,
                js_resp,
                css_resp,
            )
        )
        assert sentinel_text.encode("utf-8") not in all_responses
        assert cfg.access_token.encode("utf-8") not in all_responses
        csp = index_resp.headers.get("content-security-policy", "")
        assert csp == CSP_HEADER_VALUE
        assert "script-src 'self'" in csp
        assert "style-src 'self'" in csp
        assert "connect-src 'self'" in csp
        assert "frame-ancestors 'none'" in csp
        assert index_resp.headers.get("cache-control") == "no-store"


def test_a06_roots_and_existing_project(tmp_path: Path) -> None:
    real_target = tmp_path / "real_target"
    real_target.mkdir()
    sentinel = real_target / "sentinel.bin"
    sentinel.write_bytes(b"immutable-sentinel-bytes\n")

    # 1. Overlapping / raw traversal / NTFS junction reparse roots rejected cleanly.
    data_overlap = tmp_path / "overlap"
    ws_overlap = data_overlap / "workspaces"
    data_overlap.mkdir()
    ws_overlap.mkdir()

    invalid_pairs: list[tuple[Path, Path]] = [
        (data_overlap, data_overlap),
        (data_overlap, ws_overlap),
        (Path(str(tmp_path) + "/d1/../d2"), tmp_path / "ws_other"),
        (Path("relative/data"), tmp_path / "ws_rel"),
    ]

    if os.name == "nt":
        junction_path = tmp_path / "junction_root"
        proc = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(junction_path), str(real_target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0 and junction_path.exists():
            invalid_pairs.append((junction_path / "data_inside", tmp_path / "ws_clean"))

    for bad_data, bad_ws in invalid_pairs:
        bad_cfg = HostConfig(
            data_root=bad_data,
            workspace_parent=bad_ws,
            python_executable=Path(os.sys.executable),
            bind_port=pick_free_loopback_port(),
            access_token="t" * 32,
        )
        with pytest.raises(AppError):
            AppService(config=bad_cfg)

    assert sentinel.read_bytes() == b"immutable-sentinel-bytes\n"

    # 2. Existing directory in workspace_parent cannot be overwritten/adopted across hosts.
    shared_ws_parent = tmp_path / "shared_ws"
    shared_ws_parent.mkdir()
    data1 = tmp_path / "data1"
    data2 = tmp_path / "data2"
    data1.mkdir()
    data2.mkdir()

    cfg1 = HostConfig(
        data_root=data1,
        workspace_parent=shared_ws_parent,
        python_executable=Path(os.sys.executable),
        bind_port=pick_free_loopback_port(),
        access_token="t" * 32,
    )
    cfg2 = HostConfig(
        data_root=data2,
        workspace_parent=shared_ws_parent,
        python_executable=Path(os.sys.executable),
        bind_port=pick_free_loopback_port(),
        access_token="t" * 32,
    )

    async def _scenario() -> None:
        svc1 = AppService(config=cfg1)
        try:
            await svc1.create_project(name="fixed-proj")
            existing_before = tree_hashes(shared_ws_parent / "fixed-proj")
            with pytest.raises(AppError) as e_dup:
                await svc1.create_project(name="fixed-proj")
            assert e_dup.value.code == "PROJECT_EXISTS"
            assert e_dup.value.status_code == 409
            assert tree_hashes(shared_ws_parent / "fixed-proj") == existing_before
        finally:
            await svc1.aclose()

        svc2 = AppService(config=cfg2)
        try:
            existing_before = tree_hashes(shared_ws_parent / "fixed-proj")
            with pytest.raises(AppError) as e_cross:
                await svc2.create_project(name="fixed-proj")
            assert e_cross.value.code == "PROJECT_EXISTS"
            assert tree_hashes(shared_ws_parent / "fixed-proj") == existing_before
        finally:
            await svc2.aclose()

    asyncio.run(_scenario())
