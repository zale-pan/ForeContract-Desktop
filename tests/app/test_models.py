"""A01 & A04: Strict input models and hard refusal of live DeepSeek execution."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import httpx
import pytest
from app_helpers import make_host_config, tree_hashes
from forecontract_app.models import AppError, ModelSelection, StartRunRequest
from forecontract_app.service import AppService
from pydantic import ValidationError


@pytest.mark.parametrize(
    "patch",
    [
        {"goal": ""},
        {"goal": "   "},
        {"goal": "x" * 4097},
        {"goal": "ab\x00cd"},
        {"goal": 123},
        {"confirmed_scope": 1},
        {"confirmed_scope": "true"},
        {"client_request_id": "not-a-uuid"},
        {"client_request_id": 12345},
        {"argv": ["cmd"]},
        {"output_root": "D:/escape"},
        {"workspace_root": "D:/escape"},
        {"provider": "deepseek"},
    ],
)
def test_a01_strict_inputs(patch: dict[str, object]) -> None:
    req_uuid = uuid.uuid4()
    valid_ok = StartRunRequest(
        client_request_id=req_uuid,
        goal="修复加权汇总",
        confirmed_scope=True,
    )
    assert valid_ok.goal == "修复加权汇总"

    valid_request: dict[str, object] = {
        "client_request_id": req_uuid,
        "goal": "修复加权汇总",
        "confirmed_scope": True,
    }
    with pytest.raises(ValidationError):
        StartRunRequest.model_validate(valid_request | patch)


@pytest.mark.parametrize(
    "bad_model",
    [
        {"provider": "offline", "model": "deepseek-flash"},
        {"provider": "offline", "model": ""},
        {"provider": "openai", "model": "gpt-4"},
        {"provider": "deepseek", "model": "deepseek-flash", "thinking": "enabled"},
        {"provider": "deepseek", "model": "deepseek-flash", "api_key": "sk-secret"},
        {"provider": "deepseek", "model": "x" * 129},
    ],
)
def test_a01_model_selection_bounds(bad_model: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ModelSelection.model_validate(bad_model)


def test_d02_goal_domain() -> None:
    req_uuid = uuid.uuid4()
    valid_request: dict[str, object] = {
        "client_request_id": req_uuid,
        "goal": "修复加权汇总",
        "confirmed_scope": True,
    }
    with pytest.raises(ValidationError):
        StartRunRequest.model_validate(valid_request | {"client_request_id": 12345})

    goal = " " * 9000 + "x"
    assert len(goal.strip()) == 1
    assert StartRunRequest.model_validate(valid_request | {"goal": goal}).goal == goal
    assert StartRunRequest.model_validate(valid_request | {"goal": "x" * 4096}).goal == "x" * 4096
    with pytest.raises(ValidationError):
        StartRunRequest.model_validate(valid_request | {"goal": "x" * 4097})


def test_a04_live_is_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = make_host_config(tmp_path)
    provider_factory_calls = 0
    external_http_calls: list[object] = []

    def _forbid_factory(*, project: object, run_id: str) -> object:
        nonlocal provider_factory_calls
        provider_factory_calls += 1
        raise AssertionError("provider_factory must not be called when live is disabled")

    orig_async_post = httpx.AsyncClient.post
    orig_sync_post = httpx.Client.post

    async def _spy_async_post(self: object, *args: object, **kwargs: object) -> object:
        external_http_calls.append((args, kwargs))
        return await orig_async_post(self, *args, **kwargs)  # type: ignore[arg-type]

    def _spy_sync_post(self: object, *args: object, **kwargs: object) -> object:
        external_http_calls.append((args, kwargs))
        return orig_sync_post(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx.AsyncClient, "post", _spy_async_post)
    monkeypatch.setattr(httpx.Client, "post", _spy_sync_post)

    async def _scenario() -> None:
        svc = AppService(config=cfg, provider_factory=_forbid_factory)  # type: ignore[arg-type]
        try:
            proj = await svc.create_project(name="metrics-live-disabled")
            sess = svc.create_session(
                project_id=proj.project_id,
                model=ModelSelection(
                    provider="deepseek", model="deepseek-flash", thinking="disabled"
                ),
            )
            assert sess.model.provider == "deepseek"
            assert sess.model.model == "deepseek-flash"

            lock_file = cfg.data_root / ".host.lock"
            assert lock_file.is_file()
            before = tree_hashes(cfg.data_root, exclude_relative={".host.lock"})
            req = StartRunRequest(
                client_request_id=uuid.uuid4(),
                goal="修复加权汇总",
                confirmed_scope=True,
            )
            with pytest.raises(AppError) as exc_info:
                await svc.start_run(session_id=sess.session_id, request=req)
            assert (exc_info.value.status_code, exc_info.value.code) == (409, "LIVE_DISABLED")
            assert provider_factory_calls == 0
            assert external_http_calls == []
            assert tree_hashes(cfg.data_root, exclude_relative={".host.lock"}) == before
            assert lock_file.is_file()
            assert not (cfg.data_root / "runs").exists()
        finally:
            await svc.aclose()

    asyncio.run(_scenario())
