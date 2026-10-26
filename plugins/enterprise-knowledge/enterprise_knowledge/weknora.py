"""Request-scoped asynchronous adapter for the fixed WeKnora v0.8.3 contract."""

from __future__ import annotations

import asyncio
import math
import ssl
import time
from collections.abc import Callable, Mapping
from typing import Any, cast
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, ValidationError

from octop.infra.agents.plugins.host_contracts import PluginActor

from . import i18n
from .connections import CONNECTION_INTENTS, CONNECTIONS
from .models import normalize_base_url

_MAX_REQUESTS = 10
_MAX_REQUEST_SECONDS = 30.0
_MAX_OPERATION_SECONDS = 90.0
_SEARCH_MATCH_COUNT = 8
_SEARCH_RESULT_LIMIT = 8
_CONTENT_LIMIT = 1200


def _build_async_client(ca_bundle: str | None) -> httpx.AsyncClient:
    """Build TLS roots and the default HTTP client; caller runs this in an executor."""
    ssl_context = ssl.create_default_context(cafile=ca_bundle)
    return httpx.AsyncClient(verify=ssl_context, follow_redirects=False)


class WeKnoraError(Exception):
    """Sanitized adapter failure; never includes upstream bodies or request data."""

    def __init__(self, code: str, locale: str = "en") -> None:
        self.code = code
        self.status = 409 if code in {"CONFIG_CONFLICT", "RECOVERY_BLOCKED"} else 503
        super().__init__(i18n.error_message(code, locale))


class OperationBudget:
    """One atomic request and wall-time budget shared by one provider instance."""

    def __init__(
        self,
        *,
        max_requests: int = _MAX_REQUESTS,
        operation_seconds: float = _MAX_OPERATION_SECONDS,
        request_seconds: float = _MAX_REQUEST_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_requests = min(max_requests, _MAX_REQUESTS)
        self.operation_seconds = min(operation_seconds, _MAX_OPERATION_SECONDS)
        self.request_seconds = min(request_seconds, _MAX_REQUEST_SECONDS)
        self._clock = clock
        self._started_at = self._clock()
        self._requests_started = 0
        self._lock = asyncio.Lock()
        self._cancelled = False

    @property
    def requests_started(self) -> int:
        return self._requests_started

    def cancel(self) -> None:
        self._cancelled = True

    def remaining_seconds(self) -> float:
        if self._cancelled:
            raise TimeoutError("cancelled")
        remaining = self.operation_seconds - (self._clock() - self._started_at)
        if remaining <= 0 or self._requests_started >= self.max_requests:
            raise TimeoutError("budget")
        return remaining

    def operation_expired(self) -> bool:
        return self._clock() - self._started_at >= self.operation_seconds

    def remaining_lifetime_seconds(self) -> float:
        """Wall-clock time left for bounded Host persistence after a read failure."""
        remaining = self.operation_seconds - (self._clock() - self._started_at)
        if remaining <= 0:
            raise TimeoutError("budget")
        return remaining

    async def reserve(self) -> float:
        async with self._lock:
            now = self._clock()
            if self._cancelled:
                raise TimeoutError("cancelled")
            remaining = self.operation_seconds - (now - self._started_at)
            if remaining <= 0 or self._requests_started >= self.max_requests:
                raise TimeoutError("budget")
            self._requests_started += 1
            return min(remaining, self.request_seconds)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class SearchHit(_StrictModel):
    knowledge_id: StrictStr
    knowledge_base_id: StrictStr
    content: StrictStr
    original_length: StrictInt
    truncated: bool
    title: StrictStr | None = None
    score: float | None = None
    chunk_index: StrictInt | None = None
    source_page: StrictInt | None = None
    source_version: StrictStr | None = None


class KnowledgeDetail(_StrictModel):
    knowledge_id: StrictStr
    knowledge_base_id: StrictStr
    title: StrictStr | None = None
    source_version: StrictStr | None = None


class KnowledgeChunk(_StrictModel):
    id: StrictStr
    knowledge_id: StrictStr
    knowledge_base_id: StrictStr
    chunk_index: StrictInt
    content: StrictStr
    chunk_type: StrictStr


class ChunkPage(_StrictModel):
    items: tuple[KnowledgeChunk, ...]
    total: StrictInt
    page: StrictInt
    page_size: StrictInt


class _WeKnoraReadClient:
    """A single-operation provider bound to one actor, connection and revision.

    Create one instance for one policy/action operation. Its KB list and budget are
    intentionally private to that instance. `ca_bundle` is a trusted server-side
    configuration value; action arguments cannot supply it.
    """

    def __init__(
        self,
        facade: Any,
        actor: PluginActor,
        connection_id: str,
        expected_config_revision: int,
        *,
        client: httpx.AsyncClient | None = None,
        ca_bundle: str | None = None,
        budget: OperationBudget | None = None,
    ) -> None:
        self._facade = facade
        self._actor = actor
        self._connection_id = connection_id
        self._expected_revision = expected_config_revision
        self._client = client
        self._ca_bundle = ca_bundle
        self._budget = budget or OperationBudget()
        self._base_url: str | None = None
        self._api_key: str | None = None
        self._tenant_id: str | None = None
        self._kb_rows: list[Mapping[str, Any]] | None = None
        self._details: dict[str, KnowledgeDetail] = {}
        self._setup_lock = asyncio.Lock()
        self._client_cleanup_tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    async def visible_kb_ids(self, connection_id: str, actor: PluginActor) -> list[str]:
        self._bind(connection_id, actor)
        rows = await self._list_rows()
        return [str(row["id"]) for row in rows]

    async def metadata_for(
        self, connection_id: str, kb_ids: list[str], actor: PluginActor
    ) -> list[Mapping[str, Any]]:
        self._bind(connection_id, actor)
        allowed = set(kb_ids)
        rows = await self._list_rows()
        return [self._metadata(row) for row in rows if row.get("id") in allowed]

    async def search(self, query: str, knowledge_base_ids: list[str]) -> list[SearchHit]:
        if not isinstance(query, str) or not query or not knowledge_base_ids:
            raise WeKnoraError("UPSTREAM_SCHEMA_INVALID", self._actor.locale)
        ids = self._validated_ids(knowledge_base_ids)
        payload = await self._json_request(
            "POST",
            "/knowledge-search?resource_urls=handle",
            json={"query": query, "knowledge_base_ids": ids, "match_count": _SEARCH_MATCH_COUNT},
        )
        data = self._success_data(payload, list)
        hits: list[SearchHit] = []
        for raw in data[:_SEARCH_RESULT_LIMIT]:
            if not isinstance(raw, Mapping):
                raise WeKnoraError("UPSTREAM_SCHEMA_INVALID", self._actor.locale)
            knowledge_id = raw.get("knowledge_id")
            content = raw.get("content")
            if not self._nonempty(knowledge_id) or not isinstance(content, str):
                raise WeKnoraError("UPSTREAM_SCHEMA_INVALID", self._actor.locale)
            kb_id = raw.get("knowledge_base_id")
            title = raw.get("knowledge_title")
            if kb_id is not None and (not self._nonempty(kb_id) or kb_id not in ids):
                raise WeKnoraError("UPSTREAM_SCHEMA_INVALID", self._actor.locale)
            if kb_id is None:
                detail = await self.knowledge_detail(str(knowledge_id), ids)
                kb_id = detail.knowledge_base_id
                if title is None:
                    title = detail.title
            bounded = content[:_CONTENT_LIMIT]
            score = raw.get("score")
            if score is not None and (
                isinstance(score, bool)
                or not isinstance(score, (int, float))
                or not math.isfinite(score)
            ):
                raise WeKnoraError("UPSTREAM_SCHEMA_INVALID", self._actor.locale)
            chunk_index = raw.get("chunk_index")
            if chunk_index is not None and (type(chunk_index) is not int or chunk_index < 0):
                raise WeKnoraError("UPSTREAM_SCHEMA_INVALID", self._actor.locale)
            source_page = self._source_page(raw.get("source_locators"))
            try:
                hits.append(
                    SearchHit(
                        knowledge_id=cast(str, knowledge_id),
                        knowledge_base_id=kb_id,
                        content=bounded,
                        original_length=len(content),
                        truncated=len(content) > len(bounded),
                        title=title if isinstance(title, str) else None,
                        score=float(score) if score is not None else None,
                        chunk_index=chunk_index,
                        source_page=source_page,
                        source_version=None,
                    )
                )
            except ValidationError:
                raise WeKnoraError("UPSTREAM_SCHEMA_INVALID", self._actor.locale) from None
        return hits

    async def knowledge_detail(
        self, knowledge_id: str, allowed_kb_ids: list[str]
    ) -> KnowledgeDetail:
        allowed = self._validated_ids(allowed_kb_ids)
        cached = self._details.get(knowledge_id)
        if cached is not None:
            if cached.knowledge_base_id not in allowed:
                raise WeKnoraError("UPSTREAM_SCHEMA_INVALID", self._actor.locale)
            return cached
        payload = await self._json_request(
            "GET", f"/knowledge/{quote(self._path_id(knowledge_id), safe='')}"
        )
        data = self._success_data(payload, Mapping)
        if data.get("id") != knowledge_id:
            raise WeKnoraError("UPSTREAM_SCHEMA_INVALID", self._actor.locale)
        kb_id = data.get("knowledge_base_id")
        if not self._nonempty(kb_id) or kb_id not in allowed:
            raise WeKnoraError("UPSTREAM_SCHEMA_INVALID", self._actor.locale)
        try:
            detail = KnowledgeDetail(
                knowledge_id=knowledge_id,
                knowledge_base_id=kb_id,
                title=data.get("title") if isinstance(data.get("title"), str) else None,
                source_version=None,
            )
        except ValidationError:
            raise WeKnoraError("UPSTREAM_SCHEMA_INVALID", self._actor.locale) from None
        self._details[knowledge_id] = detail
        return detail

    async def chunks(
        self,
        knowledge_id: str,
        allowed_kb_ids: list[str],
        *,
        page: int = 1,
        page_size: int = 10,
    ) -> ChunkPage:
        allowed = self._validated_ids(allowed_kb_ids)
        if (
            type(page) is not int
            or page < 1
            or type(page_size) is not int
            or not 1 <= page_size <= 100
        ):
            raise WeKnoraError("UPSTREAM_SCHEMA_INVALID", self._actor.locale)
        await self.knowledge_detail(knowledge_id, allowed)
        path = f"/chunks/{quote(self._path_id(knowledge_id), safe='')}?page={page}&page_size={page_size}"
        payload = await self._json_request("GET", path)
        data = self._success_data(payload, list)
        total, actual_page, actual_size = (
            payload.get("total"),
            payload.get("page"),
            payload.get("page_size"),
        )
        if any(type(value) is not int or value < 0 for value in (total, actual_page, actual_size)):
            raise WeKnoraError("UPSTREAM_SCHEMA_INVALID", self._actor.locale)
        if actual_page != page or actual_size != page_size:
            raise WeKnoraError("UPSTREAM_SCHEMA_INVALID", self._actor.locale)
        if len(data) > page_size:
            raise WeKnoraError("UPSTREAM_SCHEMA_INVALID", self._actor.locale)
        items: list[KnowledgeChunk] = []
        for row in data:
            if not isinstance(row, Mapping):
                raise WeKnoraError("UPSTREAM_SCHEMA_INVALID", self._actor.locale)
            if (
                row.get("knowledge_id") != knowledge_id
                or row.get("knowledge_base_id") not in allowed
                or row.get("chunk_type") != "text"
                or type(row.get("chunk_index")) is not int
                or row.get("chunk_index", -1) < 0
                or not self._nonempty(row.get("id"))
            ):
                raise WeKnoraError("UPSTREAM_SCHEMA_INVALID", self._actor.locale)
            try:
                items.append(
                    KnowledgeChunk(
                        id=row["id"],
                        knowledge_id=knowledge_id,
                        knowledge_base_id=row["knowledge_base_id"],
                        chunk_index=row["chunk_index"],
                        content=row["content"],
                        chunk_type="text",
                    )
                )
            except (KeyError, ValidationError):
                raise WeKnoraError("UPSTREAM_SCHEMA_INVALID", self._actor.locale) from None
        try:
            return ChunkPage(
                items=tuple(items),
                total=cast(int, total),
                page=cast(int, actual_page),
                page_size=cast(int, actual_size),
            )
        except ValidationError:
            raise WeKnoraError("UPSTREAM_SCHEMA_INVALID", self._actor.locale) from None

    async def aclose(self) -> None:
        self._closed = True
        self._budget.cancel()
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._api_key = None

    async def _list_rows(self) -> list[Mapping[str, Any]]:
        if self._kb_rows is not None:
            return self._kb_rows
        payload = await self._json_request("GET", "/knowledge-bases")
        data = self._success_data(payload, list)
        seen: set[str] = set()
        rows: list[Mapping[str, Any]] = []
        for row in data:
            if not isinstance(row, Mapping) or not self._nonempty(row.get("id")):
                raise WeKnoraError("UPSTREAM_SCHEMA_INVALID", self._actor.locale)
            kb_id = str(row["id"])
            if kb_id in seen:
                raise WeKnoraError("UPSTREAM_SCHEMA_INVALID", self._actor.locale)
            seen.add(kb_id)
            rows.append(row)
        self._kb_rows = rows
        return rows

    async def _json_request(self, method: str, path: str, *, json: Any = None) -> Mapping[str, Any]:
        try:
            operation_timeout = self._budget.remaining_seconds()
        except asyncio.CancelledError:
            self._abort_operation()
            raise
        except TimeoutError as exc:
            code = "UPSTREAM_BUDGET_EXCEEDED"
            if str(exc) == "cancelled":
                self._abort_operation()
                raise WeKnoraError("UPSTREAM_UNAVAILABLE", self._actor.locale) from None
            self._abort_operation()
            raise WeKnoraError(code, self._actor.locale) from None
        try:
            async with asyncio.timeout(operation_timeout):
                client = await self._ensure_ready()
                await self._ensure_current()
                try:
                    timeout = await self._budget.reserve()
                except TimeoutError as exc:
                    if str(exc) == "cancelled":
                        self._abort_operation()
                        raise WeKnoraError("UPSTREAM_UNAVAILABLE", self._actor.locale) from None
                    raise WeKnoraError("UPSTREAM_BUDGET_EXCEEDED", self._actor.locale) from None
                headers = {"X-API-Key": self._api_key or ""}
                if self._tenant_id is not None:
                    headers["X-Tenant-ID"] = self._tenant_id
                async with asyncio.timeout(timeout):
                    response = await client.request(
                        method,
                        f"{self._base_url}{path}",
                        headers=headers,
                        json=json,
                        timeout=httpx.Timeout(timeout),
                        follow_redirects=False,
                    )
                    if 300 <= response.status_code < 400:
                        raise WeKnoraError("UPSTREAM_HTTP_ERROR", self._actor.locale)
                    if response.status_code in {401, 403}:
                        raise WeKnoraError("UPSTREAM_AUTH_FAILED", self._actor.locale)
                    if not response.is_success:
                        raise WeKnoraError("UPSTREAM_HTTP_ERROR", self._actor.locale)
                    try:
                        data = response.json()
                    except (ValueError, UnicodeDecodeError):
                        raise WeKnoraError("UPSTREAM_SCHEMA_INVALID", self._actor.locale) from None
        except asyncio.CancelledError:
            self._abort_operation()
            raise
        except TimeoutError:
            operation_expired = self._budget.operation_expired()
            self._abort_operation()
            if operation_expired:
                raise WeKnoraError("UPSTREAM_BUDGET_EXCEEDED", self._actor.locale) from None
            raise WeKnoraError("UPSTREAM_TIMEOUT", self._actor.locale) from None
        except httpx.TimeoutException:
            raise WeKnoraError("UPSTREAM_TIMEOUT", self._actor.locale) from None
        except httpx.HTTPError:
            raise WeKnoraError("UPSTREAM_UNAVAILABLE", self._actor.locale) from None
        if not isinstance(data, Mapping):
            raise WeKnoraError("UPSTREAM_SCHEMA_INVALID", self._actor.locale)
        return data

    async def _ensure_ready(self) -> httpx.AsyncClient:
        async with self._setup_lock:
            return await self._ensure_ready_once()

    async def _ensure_ready_once(self) -> httpx.AsyncClient:
        if self._closed:
            raise WeKnoraError("UPSTREAM_UNAVAILABLE", self._actor.locale)
        if self._base_url is not None and self._api_key is not None:
            if self._client is None:
                raise WeKnoraError("UPSTREAM_UNAVAILABLE", self._actor.locale)
            return self._client
        await self._ensure_current()
        try:
            record = await self._facade.get_record(CONNECTIONS, self._connection_id)
            payload = record.payload
            credential_ref = payload.get("credential_ref")
            owner_id = payload.get("credential_owner_user_id")
            credentials = await self._facade.resolve_credential(credential_ref, owner_id)
            api_key = credentials.get("api_key")
            if not isinstance(api_key, str) or not api_key:
                raise ValueError("missing api key")
            self._base_url = normalize_base_url(str(payload["base_url"]))
            if self._base_url != payload["base_url"]:
                raise ValueError("noncanonical base url")
            tenant_id = payload.get("tenant_id")
            self._tenant_id = tenant_id if isinstance(tenant_id, str) and tenant_id else None
            self._api_key = api_key
        except WeKnoraError:
            raise
        except Exception:
            raise WeKnoraError("PLUGIN_NOT_READY", self._actor.locale) from None
        if self._client is None:
            loop = asyncio.get_running_loop()
            future = loop.run_in_executor(None, _build_async_client, self._ca_bundle)
            try:
                self._client = await asyncio.shield(future)
            except asyncio.CancelledError:
                task = asyncio.create_task(self._close_client_when_ready(future))
                self._client_cleanup_tasks.add(task)
                task.add_done_callback(self._client_cleanup_tasks.discard)
                raise
            except Exception:
                self._api_key = None
                raise WeKnoraError("PLUGIN_NOT_READY", self._actor.locale) from None
        return self._client

    async def _close_client_when_ready(self, future: asyncio.Future[httpx.AsyncClient]) -> None:
        try:
            client = await future
        except BaseException:
            return
        await client.aclose()

    def _abort_operation(self) -> None:
        self._budget.cancel()
        self._api_key = None

    async def _ensure_current(self) -> None:
        try:
            record = await self._facade.get_record(CONNECTIONS, self._connection_id)
            intent = await self._facade.get_record(CONNECTION_INTENTS, self._connection_id)
        except Exception:
            raise WeKnoraError("PLUGIN_NOT_READY", self._actor.locale) from None
        if record is None or record.revision != self._expected_revision:
            raise WeKnoraError("CONFIG_CONFLICT", self._actor.locale)
        if intent is not None and intent.payload.get("state") in {"PREPARING", "BLOCKED"}:
            raise WeKnoraError("RECOVERY_BLOCKED", self._actor.locale)

    def _bind(self, connection_id: str, actor: PluginActor) -> None:
        if self._closed or connection_id != self._connection_id or actor != self._actor:
            raise WeKnoraError("CONFIG_CONFLICT", self._actor.locale)

    def _success_data(self, payload: Mapping[str, Any], expected_type: type[Any]) -> Any:
        if payload.get("success") is not True or not isinstance(payload.get("data"), expected_type):
            raise WeKnoraError("UPSTREAM_SCHEMA_INVALID", self._actor.locale)
        return payload["data"]

    @staticmethod
    def _metadata(row: Mapping[str, Any]) -> Mapping[str, Any]:
        result: dict[str, Any] = {"kb_id": row["id"]}
        for field in ("name", "description"):
            if isinstance(row.get(field), str):
                result[field] = row[field]
        for field in ("knowledge_count", "chunk_count"):
            value = row.get(field)
            if type(value) is int and value >= 0:
                result[field] = value
        return result

    @staticmethod
    def _source_page(value: Any) -> int | None:
        if not isinstance(value, Mapping):
            return None
        page = value.get("page")
        return page if type(page) is int and page >= 0 else None

    @staticmethod
    def _nonempty(value: Any) -> bool:
        return isinstance(value, str) and bool(value) and value == value.strip()

    @classmethod
    def _validated_ids(cls, values: list[str]) -> list[str]:
        if not values or any(
            not cls._nonempty(value)
            or "/" in value
            or "\\" in value
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            for value in values
        ):
            raise WeKnoraError("UPSTREAM_SCHEMA_INVALID")
        return list(dict.fromkeys(values))

    @classmethod
    def _path_id(cls, value: str) -> str:
        if (
            not cls._nonempty(value)
            or value in {".", ".."}
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
        ):
            raise WeKnoraError("UPSTREAM_SCHEMA_INVALID")
        return value


class WeKnoraProvider(_WeKnoraReadClient):
    """Employee read provider; unlike the lower client it requires enabled config."""

    async def _ensure_current(self) -> None:
        await super()._ensure_current()
        record = await self._facade.get_record(CONNECTIONS, self._connection_id)
        if record is None or record.payload.get("enabled") is not True:
            raise WeKnoraError("CONFIG_CONFLICT", self._actor.locale)
