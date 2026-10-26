"""Single-attempt HTTP adapters for the selected web search providers."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import re
import ssl
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from typing import Any, Literal, NoReturn, cast
from urllib.parse import unquote_to_bytes, urlencode, urlsplit

import httpx
from pydantic import (
    Field,
    SecretStr,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from .models import StrictDto

type SearchProvider = Literal["SEARXNG", "FIRECRAWL"]
type SearchErrorCode = Literal[
    "INVALID_CONFIG",
    "UNSUPPORTED_CONFIG",
    "INVALID_QUERY",
    "INVALID_USAGE",
    "TIMEOUT",
    "RATE_LIMITED",
    "UPSTREAM_5XX",
    "AUTH_REJECTED",
    "HTTP_REJECTED",
    "SCHEMA_INVALID",
    "UNAVAILABLE",
]

_ALLOWED_CODES = frozenset(
    {
        "INVALID_CONFIG",
        "UNSUPPORTED_CONFIG",
        "INVALID_QUERY",
        "INVALID_USAGE",
        "TIMEOUT",
        "RATE_LIMITED",
        "UPSTREAM_5XX",
        "AUTH_REJECTED",
        "HTTP_REJECTED",
        "SCHEMA_INVALID",
        "UNAVAILABLE",
    }
)
_ENDPOINT_SUFFIXES = ("/search", "/v2", "/v2/search")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_PERCENT_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")


class SearchHttpError(Exception):
    """An error containing only provider, fixed code, and optional HTTP status."""

    def __init__(
        self,
        provider: SearchProvider | None,
        code: SearchErrorCode,
        status: int | None = None,
    ) -> None:
        safe_provider = (
            provider if isinstance(provider, str) and provider in {"SEARXNG", "FIRECRAWL"} else None
        )
        safe_code = code if code in _ALLOWED_CODES else "UNAVAILABLE"
        self.provider = safe_provider
        self.code: SearchErrorCode = safe_code
        self.status = status if type(status) is int and 100 <= status <= 599 else None
        super().__init__(safe_code)


class SearchSourceConfig(StrictDto):
    """Trusted server configuration for a selected provider."""

    provider: SearchProvider
    base_url: StrictStr
    credential: SecretStr | None = Field(default=None, repr=False)
    ca_bundle: StrictStr | None = None

    def __init__(self, **data: object) -> None:
        failure: SearchHttpError | None = None
        try:
            super().__init__(**data)
        except ValidationError:
            failure = SearchHttpError(_safe_provider(data.get("provider")), "INVALID_CONFIG")
        if failure is not None:
            raise failure

    @classmethod
    def model_validate(cls, obj: object, **kwargs: Any) -> SearchSourceConfig:
        failure: SearchHttpError | None = None
        result: SearchSourceConfig | None = None
        provider = obj.get("provider") if isinstance(obj, dict) else None
        try:
            result = super().model_validate(obj, **kwargs)
        except ValidationError:
            failure = SearchHttpError(_safe_provider(provider), "INVALID_CONFIG")
        if failure is not None:
            raise failure
        assert result is not None
        return result

    @model_validator(mode="after")
    def _provider_credential(self) -> SearchSourceConfig:
        if self.provider == "FIRECRAWL" and self.credential is not None:
            raw = self.credential.get_secret_value()
            if not raw or raw != raw.strip() or "\r" in raw or "\n" in raw:
                raise ValueError("invalid credential")
            raw.encode("utf-8")
        return self

    @field_validator("ca_bundle")
    @classmethod
    def _ca_shape(cls, value: str | None) -> str | None:
        if value is not None:
            if not value or value != value.strip() or _CONTROL_RE.search(value):
                raise ValueError("invalid CA bundle path")
            value.encode("utf-8")
        return value


class WebSearchHit(StrictDto):
    provider: SearchProvider
    source_kind: Literal["WEB"]
    title: StrictStr
    locator: StrictStr
    content: StrictStr
    original_length: StrictInt
    truncated: StrictBool
    source_version: None = None
    source_page: None = None


class SearchHttpResult(StrictDto):
    provider: SearchProvider
    status: Literal["SUCCESS", "EMPTY"]
    hits: tuple[WebSearchHit, ...]
    results_truncated: StrictBool


def _safe_provider(value: object) -> SearchProvider | None:
    if not isinstance(value, str) or value not in {"SEARXNG", "FIRECRAWL"}:
        return None
    return cast(SearchProvider, value)


def _invalid_config(provider: SearchProvider | None) -> NoReturn:
    raise SearchHttpError(provider, "INVALID_CONFIG")


def _url_text_is_safe(value: str) -> bool:
    try:
        value.encode("utf-8")
    except UnicodeError:
        return False
    return not _CONTROL_RE.search(value) and "\\" not in value and not _PERCENT_RE.search(value)


def _unsafe_path(path: str) -> bool:
    for segment in path.split("/"):
        decoded = unquote_to_bytes(segment)
        while True:
            if decoded in {b".", b".."} or b"/" in decoded or b"\\" in decoded:
                return True
            if not re.search(rb"%(?:2e|2f|5c|25)", decoded, re.IGNORECASE):
                break
            next_decoded = unquote_to_bytes(decoded)
            if next_decoded == decoded:
                break
            decoded = next_decoded
    return False


def normalize_search_base_url(provider: SearchProvider, value: str) -> str:
    safe_provider = _safe_provider(provider)
    if safe_provider is None or not isinstance(value, str) or not value or value != value.strip():
        _invalid_config(safe_provider)
    if (
        not _url_text_is_safe(value)
        or any(character.isspace() for character in value)
        or "?" in value
        or "#" in value
    ):
        _invalid_config(safe_provider)
    try:
        split = urlsplit(value)
        parsed = httpx.URL(value)
        if split.username is not None or split.password is not None or not split.hostname:
            _invalid_config(safe_provider)
        if split.netloc.endswith(":"):
            _invalid_config(safe_provider)
        if split.query or split.fragment or "?" in value or "#" in value:
            _invalid_config(safe_provider)
        port = split.port
        host = parsed.raw_host.decode("ascii").lower()
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"}:
            _invalid_config(safe_provider)
        is_loopback = host == "localhost"
        with suppress(ValueError):
            is_loopback = is_loopback or ipaddress.ip_address(host).is_loopback
        if scheme == "http" and not is_loopback:
            _invalid_config(safe_provider)
        path = split.path
        if _PERCENT_RE.search(path):
            _invalid_config(safe_provider)
        if _unsafe_path(path):
            _invalid_config(safe_provider)
        if any(path.rstrip("/").endswith(suffix) for suffix in _ENDPOINT_SUFFIXES):
            _invalid_config(safe_provider)
        normalized_port = port
        if (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
            normalized_port = None
        netloc = parsed.netloc.decode("ascii")
        if normalized_port is None:
            netloc = parsed.raw_host.decode("ascii").lower()
            if ":" in netloc and not netloc.startswith("["):
                netloc = f"[{netloc}]"
        normalized = f"{scheme}://{netloc}{path.rstrip('/')}"
        return normalized
    except (ValueError, UnicodeError, httpx.InvalidURL):
        pass
    _invalid_config(safe_provider)


def validate_result_url(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or not _url_text_is_safe(value)
        or value != value.strip()
    ):
        raise ValueError("invalid result URL")
    if any(character.isspace() for character in value):
        raise ValueError("invalid result URL")
    split = urlsplit(value)
    parsed = httpx.URL(value)
    if split.scheme.lower() not in {"http", "https"} or not split.hostname:
        raise ValueError("invalid result URL")
    if split.username is not None or split.password is not None or not parsed.raw_host:
        raise ValueError("invalid result URL")
    _ = split.port
    if re.search(r"%(?:0[ad])", value, re.IGNORECASE) or _unsafe_path(split.path):
        raise ValueError("invalid result URL")
    return value


def _strict_json(payload: bytes) -> object:
    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite JSON number")

    def unique_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        output: dict[str, object] = {}
        for key, value in pairs:
            if key in output:
                raise ValueError("duplicate JSON key")
            output[key] = value
        return output

    text = payload.decode("utf-8", errors="strict")
    return json.loads(text, parse_constant=reject_constant, object_pairs_hook=unique_pairs)


def _parse_response(provider: SearchProvider, payload: bytes) -> SearchHttpResult:
    failure = False
    result: SearchHttpResult | None = None
    try:
        root = _strict_json(payload)
        if not isinstance(root, dict):
            raise ValueError("invalid response root")
        if provider == "SEARXNG":
            rows = root.get("results")
            snippet_key = "content"
        else:
            if root.get("success") is not True:
                raise ValueError("invalid success marker")
            data = root.get("data")
            if not isinstance(data, dict):
                raise ValueError("invalid response data")
            rows = data.get("web")
            snippet_key = "description"
        if not isinstance(rows, list):
            raise ValueError("invalid result list")
        hits: list[WebSearchHit] = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("invalid result row")
            title = row.get("title")
            if not isinstance(title, str) or not title.strip() or _CONTROL_RE.search(title):
                raise ValueError("invalid result title")
            title.encode("utf-8")
            locator = validate_result_url(row.get("url"))
            content = row.get(snippet_key, "")
            if not isinstance(content, str):
                raise ValueError("invalid result snippet")
            content.encode("utf-8")
            length = len(content)
            hits.append(
                WebSearchHit(
                    provider=provider,
                    source_kind="WEB",
                    title=title,
                    locator=locator,
                    content=content[:1200],
                    original_length=length,
                    truncated=length > 1200,
                )
            )
        if not hits:
            result = SearchHttpResult(
                provider=provider, status="EMPTY", hits=(), results_truncated=False
            )
        else:
            result = SearchHttpResult(
                provider=provider,
                status="SUCCESS",
                hits=tuple(hits[:8]),
                results_truncated=len(hits) > 8,
            )
    except Exception:
        failure = True
    if failure:
        raise SearchHttpError(provider, "SCHEMA_INVALID")
    assert result is not None
    return result


async def _async_body(payload: bytes) -> AsyncIterator[bytes]:
    yield payload


class SearchHttpAttempt:
    """Owns one HTTP request, parser/TLS futures, and their eventual cleanup."""

    def __init__(
        self,
        config: SearchSourceConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        failure: SearchHttpError | None = None
        provider = _safe_provider(getattr(config, "provider", None))
        endpoint: str | None = None
        try:
            if provider is None or not isinstance(config, SearchSourceConfig):
                raise ValueError("invalid config")
            if provider == "SEARXNG" and config.credential is not None:
                failure = SearchHttpError(provider, "UNSUPPORTED_CONFIG")
            else:
                base = normalize_search_base_url(provider, config.base_url)
                suffix = "/search" if provider == "SEARXNG" else "/v2/search"
                endpoint = base + suffix
        except SearchHttpError as exc:
            failure = exc
        except Exception:
            failure = SearchHttpError(provider, "INVALID_CONFIG")
        if failure is not None:
            raise failure
        assert provider is not None and endpoint is not None
        self._provider = provider
        self._endpoint = endpoint
        self._credential = (
            config.credential.get_secret_value() if config.credential is not None else None
        )
        self._ca_bundle = config.ca_bundle
        self._transport = transport
        self._clock = clock
        self._state = "NEW"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: httpx.AsyncClient | None = None
        self._tls_future: asyncio.Future[httpx.AsyncClient] | None = None
        self._parser_future: asyncio.Future[SearchHttpResult] | None = None
        self._drain_task: asyncio.Task[None] | None = None
        self._client_closed = False

    async def search(
        self,
        public_query: str,
        *,
        deadline_at: float | None = None,
        pre_send: Callable[[], Awaitable[None]] | None = None,
    ) -> SearchHttpResult:
        if self._state != "NEW":
            raise SearchHttpError(self._provider, "INVALID_USAGE")
        self._state = "RUNNING"
        self._loop = asyncio.get_running_loop()
        started = self._clock()
        deadline = started + 10.0
        if deadline_at is not None:
            if (
                isinstance(deadline_at, bool)
                or not isinstance(deadline_at, (int, float))
                or not math.isfinite(deadline_at)
            ):
                self._state = "DONE"
                self._credential = None
                raise SearchHttpError(self._provider, "INVALID_QUERY")
            deadline = min(deadline, float(deadline_at))
        try:
            if not isinstance(public_query, str) or not public_query:
                invalid_query = True
            else:
                invalid_query = False
                try:
                    public_query.encode("utf-8")
                except UnicodeError:
                    invalid_query = True
            if invalid_query:
                raise SearchHttpError(self._provider, "INVALID_QUERY")
            self._ensure_time(deadline)
            client = await self._get_client(deadline)
            self._ensure_time(deadline)
            if self._provider == "SEARXNG":
                payload = urlencode([("q", public_query), ("format", "json")]).encode("ascii")
                request = httpx.Request(
                    "POST",
                    self._endpoint,
                    content=_async_body(payload),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            else:
                headers = (
                    {"Authorization": f"Bearer {self._credential}"}
                    if self._credential is not None
                    else {}
                )
                headers["Content-Type"] = "application/json"
                payload = json.dumps(
                    {"query": public_query, "limit": 8, "highlights": False},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                request = httpx.Request(
                    "POST",
                    self._endpoint,
                    content=_async_body(payload),
                    headers=headers,
                )
            remaining = self._remaining(deadline)
            if remaining <= 0:
                raise SearchHttpError(self._provider, "TIMEOUT")
            if pre_send is not None:
                try:
                    await asyncio.wait_for(pre_send(), timeout=remaining)
                except TimeoutError:
                    raise SearchHttpError(self._provider, "TIMEOUT") from None
                self._ensure_time(deadline)
                remaining = self._remaining(deadline)
                if remaining <= 0:
                    raise SearchHttpError(self._provider, "TIMEOUT")
            request.extensions["timeout"] = {
                "connect": remaining,
                "read": remaining,
                "write": remaining,
                "pool": remaining,
            }
            response: httpx.Response | None = None
            request_error: SearchHttpError | None = None
            try:
                response = await asyncio.wait_for(
                    client.send(request, auth=None, follow_redirects=False),
                    timeout=remaining,
                )
            except httpx.TimeoutException:
                request_error = SearchHttpError(self._provider, "TIMEOUT")
            except TimeoutError:
                request_error = SearchHttpError(self._provider, "TIMEOUT")
            except httpx.HTTPError:
                request_error = SearchHttpError(self._provider, "UNAVAILABLE")
            if request_error is not None:
                raise request_error
            assert response is not None
            status = response.status_code
            if status == 429:
                raise SearchHttpError(self._provider, "RATE_LIMITED", status)
            if 500 <= status <= 599:
                raise SearchHttpError(self._provider, "UPSTREAM_5XX", status)
            if status in {401, 403}:
                raise SearchHttpError(self._provider, "AUTH_REJECTED", status)
            if status != 200:
                raise SearchHttpError(self._provider, "HTTP_REJECTED", status)
            body_error: SearchHttpError | None = None
            try:
                body = await response.aread()
            except httpx.TimeoutException:
                body_error = SearchHttpError(self._provider, "TIMEOUT")
            except httpx.HTTPError:
                body_error = SearchHttpError(self._provider, "UNAVAILABLE")
            if body_error is not None:
                raise body_error
            self._ensure_time(deadline)
            loop = asyncio.get_running_loop()
            self._parser_future = loop.run_in_executor(None, _parse_response, self._provider, body)
            try:
                result = await self._await_deadline(self._parser_future, deadline)
            except TimeoutError:
                parser_timed_out = True
            else:
                parser_timed_out = False
            if parser_timed_out:
                raise SearchHttpError(self._provider, "TIMEOUT")
            self._ensure_time(deadline)
            return result
        except asyncio.CancelledError:
            self._state = "CANCELLED"
            self._credential = None
            raise
        except SearchHttpError:
            self._state = "DONE"
            self._credential = None
            raise
        except Exception:
            self._state = "DONE"
            self._credential = None
            raise
        finally:
            if self._state == "RUNNING":
                self._state = "DONE"

    async def aclose(self) -> None:
        if self._state == "CLOSED":
            return
        if self._state == "RUNNING":
            raise SearchHttpError(self._provider, "INVALID_USAGE")
        if self._drain_task is None:
            if self._loop is None:
                self._loop = asyncio.get_running_loop()
            self._state = "CLOSING"
            self._drain_task = self._loop.create_task(self._drain())
        drain = self._drain_task
        await asyncio.shield(drain)
        self._state = "CLOSED"

    def _remaining(self, deadline: float) -> float:
        return deadline - self._clock()

    def _ensure_time(self, deadline: float) -> None:
        if self._remaining(deadline) <= 0:
            raise SearchHttpError(self._provider, "TIMEOUT")

    async def _await_deadline(
        self, future: asyncio.Future[SearchHttpResult], deadline: float
    ) -> SearchHttpResult:
        remaining = self._remaining(deadline)
        if remaining <= 0:
            raise TimeoutError
        return await asyncio.wait_for(asyncio.shield(future), timeout=remaining)

    async def _get_client(self, deadline: float) -> httpx.AsyncClient:
        if self._client is not None:
            return self._client
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(None, _make_client, self._ca_bundle, self._transport)
        self._tls_future = future
        client: httpx.AsyncClient | None = None
        failure: SearchHttpError | None = None
        try:
            remaining = self._remaining(deadline)
            if remaining <= 0:
                raise TimeoutError
            client = await asyncio.wait_for(asyncio.shield(future), timeout=remaining)
        except TimeoutError:
            failure = SearchHttpError(self._provider, "TIMEOUT")
        except Exception:
            failure = SearchHttpError(self._provider, "UNAVAILABLE")
        if failure is not None:
            raise failure
        assert client is not None
        self._client = client
        return client

    async def _drain(self) -> None:
        if self._tls_future is not None:
            with suppress(BaseException):
                client = await asyncio.shield(self._tls_future)
                if self._client is None:
                    self._client = client
        if self._parser_future is not None:
            with suppress(BaseException):
                await asyncio.shield(self._parser_future)
        if self._client is not None and not self._client_closed:
            self._client_closed = True
            with suppress(BaseException):
                await self._client.aclose()


def _make_client(
    ca_bundle: str | None, transport: httpx.AsyncBaseTransport | None
) -> httpx.AsyncClient:
    context = ssl.create_default_context(cafile=ca_bundle)
    return httpx.AsyncClient(
        transport=transport,
        verify=context,
        follow_redirects=False,
        trust_env=False,
    )
