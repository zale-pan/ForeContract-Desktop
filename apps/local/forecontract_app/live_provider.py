"""Bounded live DeepSeek provider and factory with shared, persisted reservation and client ownership.

One authorization (one factory over one ``data_root``) owns exactly one shared quota: the
request count and the reserved cost are shared across every run that factory creates and are
persisted to a state file so a host restart does not mint a new authorization.

Reservation basis (computed before any send, never refunded on refusal or error):

- ``input_units`` = the exact UTF-8 byte length of the transport's canonical wire body, which
  already contains the system message, the user message (the request dump), the protocol fields
  and ``max_tokens``.  This is a conservative upper bound on the input token count: one token
  always encodes at least one byte, so the token count never exceeds the byte count.
- ``output_units`` = ``max_tokens``, the exact hard output cap the transport sends.
- ``reserved_cost`` = ``(input_units * price_input_cny_per_1m + output_units *
  price_output_cny_per_1m) / 1_000_000``.

The factory owns the transport clients it creates; each provider's ``aclose`` closes its client
exactly once and the factory ``aclose`` closes any remaining client.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from forecontract_llm import ModelRequest, ModelResponse
from forecontract_llm.provider import ModelProvider

from forecontract_app.deepseek_transport import DeepSeekProvider, make_client
from forecontract_app.models import LiveConfig

_SCHEMA = "0.1"


class LiveQuota:
    """Shared request-count and reserved-cost quota for one authorization."""

    def __init__(
        self, *, config: LiveConfig, persist: Callable[[], None] | None = None
    ) -> None:
        self._max_requests = config.max_requests
        self._budget_cny = config.budget_cny
        self._price_input = config.price_input_cny_per_1m
        self._price_output = config.price_output_cny_per_1m
        self._persist = persist
        self.requests_sent = 0
        self.reserved_cny = 0.0

    def restore(self, *, requests_sent: int, reserved_cny: float) -> None:
        self.requests_sent = requests_sent
        self.reserved_cny = reserved_cny

    def reserve(self, *, input_units: int, output_units: int) -> float | None:
        """Reserve the worst-case cost before send; return the cost, or ``None`` when refused."""
        if self.requests_sent >= self._max_requests:
            return None
        cost = (
            input_units * self._price_input + output_units * self._price_output
        ) / 1_000_000
        if self.reserved_cny + cost > self._budget_cny:
            return None
        self.requests_sent += 1
        self.reserved_cny += cost
        if self._persist is not None:
            self._persist()
        return cost


def _refused(request: ModelRequest) -> ModelResponse:
    return ModelResponse(
        schema_version=_SCHEMA, request_id=request.request_id, status="refused", content=None, usage=None
    )


class LiveProvider:
    """Reserve the shared quota before delegating one bounded POST to the transport."""

    def __init__(
        self,
        *,
        inner: DeepSeekProvider,
        quota: LiveQuota,
        client: httpx.AsyncClient,
        usage_accumulator: object | None = None,
        on_close: Callable[[], None] | None = None,
    ) -> None:
        self._inner = inner
        self._quota = quota
        self._client = client
        self._usage = usage_accumulator
        self._on_close = on_close
        self._closed = False
        self._close_error: BaseException | None = None

    @property
    def requests_sent(self) -> int:
        return self._quota.requests_sent

    @property
    def reserved_cny(self) -> float:
        return self._quota.reserved_cny

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    def _wire_units(self, request: ModelRequest) -> tuple[int, int]:
        """The transport's canonical wire dict provides the verifiable input/output bounds."""
        wire = self._inner._payload(request)
        input_units = len(json.dumps(wire, ensure_ascii=False).encode("utf-8"))
        output_units = int(wire["max_tokens"])
        return input_units, output_units

    async def complete(self, request: ModelRequest) -> ModelResponse:
        input_units, output_units = self._wire_units(request)
        cost = self._quota.reserve(input_units=input_units, output_units=output_units)
        if cost is None:
            return _refused(request)
        if self._usage is not None:
            self._usage.record_live_reservation(amount_cny=cost)
        return await self._inner.complete(request)

    async def aclose(self) -> None:
        if self._closed:
            return
        if self._close_error is not None:
            raise self._close_error
        try:
            await self._client.aclose()
        except asyncio.CancelledError:
            # The current task was cancelled (external cancel semantics preserved by re-raising).
            # Save a plain timeout for any later teardown re-entry, which must not re-raise a
            # cancellation that would interrupt the terminal commit/notification.
            self._close_error = TimeoutError("client close was cancelled before completion")
            raise
        except BaseException as exc:  # record the first close failure
            self._close_error = exc
            raise
        self._closed = True
        if self._on_close is not None:
            self._on_close()


class LiveProviderFactory:
    """Build one budget-bounded provider (and one client) per run over one shared quota."""

    def __init__(
        self,
        *,
        config: LiveConfig,
        transport: object | None = None,
        state_path: Path | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._state_path = state_path
        self._quota = LiveQuota(config=config, persist=self._persist_state)
        self._clients: list[httpx.AsyncClient] = []
        self._close_errors: list[BaseException] = []
        self._closed = False
        self._load_state()

    @property
    def quota(self) -> LiveQuota:
        return self._quota

    @property
    def created_clients(self) -> tuple[httpx.AsyncClient, ...]:
        return tuple(self._clients)

    def register_client(self, client: httpx.AsyncClient) -> None:
        self._clients.append(client)

    def deregister_client(self, client: httpx.AsyncClient) -> None:
        self._deregister_client(client)

    def _load_state(self) -> None:
        if self._state_path is not None and self._state_path.exists():
            data = json.loads(self._state_path.read_text("utf-8"))
            self._quota.restore(
                requests_sent=int(data["requests_sent"]),
                reserved_cny=float(data["reserved_cny"]),
            )

    def _persist_state(self) -> None:
        if self._state_path is None:
            return
        self._state_path.write_text(
            json.dumps(
                {
                    "requests_sent": self._quota.requests_sent,
                    "reserved_cny": self._quota.reserved_cny,
                }
            ),
            "utf-8",
        )

    def _deregister_client(self, client: httpx.AsyncClient) -> None:
        if client in self._clients:
            self._clients.remove(client)

    def __call__(
        self, *, project: Any, run_id: str, usage_accumulator: object | None = None
    ) -> ModelProvider:
        _ = project, run_id
        if self._closed:
            raise RuntimeError("live provider factory is closed")
        client = make_client(api_key=self._config.api_key, transport=self._transport)
        self._clients.append(client)
        inner = DeepSeekProvider(
            client=client,
            model=self._config.model,
            max_request_bytes=self._config.max_request_bytes,
            max_response_bytes=self._config.max_response_bytes,
            timeout_seconds=self._config.timeout_seconds,
        )
        return LiveProvider(
            inner=inner,
            quota=self._quota,
            client=client,
            usage_accumulator=usage_accumulator,
            on_close=lambda: self._deregister_client(client),
        )

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        for client in self._clients:
            if not client.is_closed:
                try:
                    await client.aclose()
                except Exception as exc:  # noqa: BLE001 - teardown must continue
                    self._close_errors.append(exc)
        self._clients.clear()
