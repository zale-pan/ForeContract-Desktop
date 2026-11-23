"""Asynchronous DeepSeek transport used by the host smoke entry.

This module owns exactly one boundary: turning an existing planner request into one bounded
HTTPS POST and turning the provider envelope back into the existing ``ModelResponse``. It does
not decode business forecasts, repair JSON, retry, follow redirects or read any credential from
the environment; the caller injects a configured ``httpx.AsyncClient``.

The transport never invents a model version: the caller passes an explicit non-empty model.
A bounded failure returns ``status="refused"`` without publishing content; only cancellation is
re-raised unchanged, because a cancelled call must stay cancelled.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
from forecontract_llm.models import ModelResponse, ModelUsage

ENDPOINT = "https://api.deepseek.com/chat/completions"

_SCHEMA = "0.1"

_SYSTEM_PROMPT = (
    "You answer one planner request. Reply with exactly one JSON object that satisfies the "
    "response schema carried in the user message; no prose, no markdown fences, no JSON repair."
)

# Four explicit finite phases; the caller additionally bounds the whole call by one deadline.
_CONNECT_TIMEOUT = 5.0
_READ_TIMEOUT = 15.0
_WRITE_TIMEOUT = 15.0
_POOL_TIMEOUT = 5.0


class _TooLarge(Exception):
    """The received body exceeded the configured response byte ceiling."""


def make_client(*, api_key: str, transport: object | None = None) -> httpx.AsyncClient:
    """Build the one client the provider uses.

    ``trust_env`` is false so no proxy or netrc is read, redirects are never followed, and all
    four timeout phases are finite. The key is only placed in the per-request header.
    """

    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("an explicit non-empty api_key is required")
    timeout = httpx.Timeout(connect=_CONNECT_TIMEOUT, read=_READ_TIMEOUT,
                            write=_WRITE_TIMEOUT, pool=_POOL_TIMEOUT)
    return httpx.AsyncClient(
        transport=transport if transport is not None else httpx.AsyncHTTPTransport(),
        headers={"authorization": f"Bearer {api_key}"},
        timeout=timeout,
        trust_env=False,
        follow_redirects=False,
    )


def _refused(request: object) -> ModelResponse:
    return ModelResponse(schema_version=_SCHEMA, request_id=request.request_id,
                         status="refused", content=None, usage=None)


def _usage(envelope: dict) -> ModelUsage | None:
    """A missing usage is unknown; an invalid one is a bounded failure."""

    usage = envelope.get("usage", None)
    if usage is None:
        return None
    if not isinstance(usage, dict):
        raise TypeError("usage must be an object")
    values = []
    for name in ("prompt_tokens", "completion_tokens"):
        value = usage.get(name, None)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TypeError("usage counts must be non-negative integers")
        values.append(value)
    return ModelUsage(schema_version=_SCHEMA, input_tokens=values[0], output_tokens=values[1])


def _content(envelope: dict) -> str:
    """One legal choice, a normal stop and a non-empty string content."""

    if not isinstance(envelope, dict):
        raise TypeError("the provider envelope must be an object")
    choices = envelope.get("choices", None)
    if not isinstance(choices, list) or len(choices) != 1:
        raise ValueError("exactly one choice is required")
    choice = choices[0]
    if not isinstance(choice, dict):
        raise TypeError("the choice must be an object")
    if choice.get("finish_reason") != "stop":
        raise ValueError("only a normal stop is accepted")
    message = choice.get("message", None)
    if not isinstance(message, dict):
        raise TypeError("the choice must carry a message")
    content = message.get("content", None)
    if not isinstance(content, str) or not content.strip():
        raise ValueError("the content must be a non-empty string")
    return content


class DeepSeekProvider:
    """One provider bound to one injected client and one explicit model."""

    def __init__(self, *, client: object, model: str, max_request_bytes: int = 262144,
                 max_response_bytes: int = 1048576, timeout_seconds: float = 30.0,
                 max_output_tokens_cap: int | None = None,
                 remaining_seconds: object | None = None) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("an explicit non-empty model is required")
        for name, value in (("max_request_bytes", max_request_bytes),
                            ("max_response_bytes", max_response_bytes),
                            ("timeout_seconds", timeout_seconds)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise TypeError(f"{name} must be a positive number")
        self._client = client
        self._model = model
        self._max_request_bytes = int(max_request_bytes)
        self._max_response_bytes = int(max_response_bytes)
        self._timeout_seconds = float(timeout_seconds)
        if max_output_tokens_cap is not None and (isinstance(max_output_tokens_cap, bool)
                                                  or not isinstance(max_output_tokens_cap, int)
                                                  or max_output_tokens_cap <= 0):
            raise TypeError("max_output_tokens_cap must be a positive integer")
        self._max_output_tokens_cap = max_output_tokens_cap
        self._remaining_seconds = remaining_seconds
        # The raw provider envelope of the latest accepted call, for the host's redacted record.
        self.last_payload: dict | None = None
        self.last_model: str | None = None
        self.last_completion_id: str | None = None
        # The exact JSON body that was sent, and its real UTF-8 byte count.
        self.last_wire: dict | None = None
        self.last_wire_bytes: int | None = None
        self.last_http_status: int | None = None
        self.last_sent: bool = False
        self.last_max_tokens: int | None = None

    def _max_output_tokens(self, request: object) -> int:
        """The output budget of the current request; a retrieval envelope carries it on its base."""

        value = getattr(request, "max_output_tokens", None)
        if value is None:
            base = getattr(request, "base_request", None)
            value = getattr(base, "max_output_tokens", None)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("the request carries no usable output budget")
        if self._max_output_tokens_cap is not None:
            return min(value, self._max_output_tokens_cap)
        return value

    def _payload(self, request: object) -> dict:
        """The complete canonical request, carried as the user message verbatim."""

        user = json.dumps(request.model_dump(mode="json"), ensure_ascii=False)
        return {
            "model": self._model,
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "max_tokens": self._max_output_tokens(request),
        }

    async def _receive(self, body: bytes) -> bytes:
        """One bounded read of the response body; the caller bounds the whole call."""

        self.last_sent = True
        async with self._client.stream("POST", ENDPOINT, content=body,
                                       headers={"content-type": "application/json"}) as response:
            self.last_http_status = response.status_code
            if response.status_code != 200:
                return b""
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > self._max_response_bytes:
                    raise _TooLarge()
                chunks.append(chunk)
            return b"".join(chunks)

    async def complete(self, request: object) -> ModelResponse:
        """Perform at most one bounded POST and map the envelope back to a ModelResponse."""

        wire = self._payload(request)
        body = json.dumps(wire, ensure_ascii=False).encode("utf-8")
        self.last_wire = wire
        self.last_wire_bytes = len(body)
        self.last_max_tokens = wire.get("max_tokens")
        self.last_payload = None
        self.last_model = None
        self.last_completion_id = None
        self.last_http_status = None
        self.last_sent = False
        if len(body) > self._max_request_bytes:
            # An over-sized request is refused before anything is sent.
            return _refused(request)
        limit = self._timeout_seconds
        if callable(self._remaining_seconds):
            try:
                limit = min(limit, float(self._remaining_seconds()))
            except Exception:  # noqa: BLE001 - an unusable bound is a bounded refusal
                return _refused(request)
            if limit <= 0:
                return _refused(request)
        deadline = time.monotonic() + limit
        try:
            raw = await asyncio.wait_for(self._receive(body), limit)
        except asyncio.CancelledError:
            # An external cancellation stays cancelled; no late content is published.
            raise
        except _TooLarge:
            return _refused(request)
        except (asyncio.TimeoutError, TimeoutError):
            return _refused(request)
        except Exception:  # noqa: BLE001 - transport text is never surfaced
            return _refused(request)
        if time.monotonic() >= deadline:
            # A transport that swallows cancellation and answers after the bound is never a success.
            return _refused(request)
        try:
            envelope = json.loads(raw.decode("utf-8"))
            content = _content(envelope)
            usage = _usage(envelope)
        except Exception:  # noqa: BLE001 - decode and schema failures are bounded
            return _refused(request)
        self.last_payload = envelope
        self.last_model = envelope.get("model") if isinstance(envelope.get("model"), str) else None
        identifier = envelope.get("id")
        self.last_completion_id = identifier if isinstance(identifier, str) else None
        return ModelResponse(schema_version=_SCHEMA, request_id=request.request_id,
                             status="ok", content=content, usage=usage)
