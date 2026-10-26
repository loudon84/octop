"""Controlled primary/fallback web search orchestration."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, cast

import httpx
from pydantic import SecretStr

from octop.infra.agents.plugins.host_contracts import PluginActionError, PluginActor

from .models import SearchExecutionRecord
from .search_http import (
    SearchHttpAttempt,
    SearchHttpError,
    SearchHttpResult,
    SearchSourceConfig,
)
from .search_operations import SearchOperationService
from .search_settings import CONFIG_KEY, SEARCH_SOURCES

_FALLBACK_CODES = frozenset({"TIMEOUT", "RATE_LIMITED", "UPSTREAM_5XX"})


@dataclass(frozen=True)
class SearchRunResult:
    execution: SearchExecutionRecord
    result: SearchHttpResult | None


class SearchService:
    """Runs one durable search round per operation and owns attempt cleanup."""

    def __init__(
        self,
        facade: Any,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._facade = facade
        self._operations = SearchOperationService(facade)
        self._transport = transport
        self._clock = clock
        self._active: dict[str, asyncio.Task[SearchRunResult]] = {}
        self._drains: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()
        self._closed = False

    async def execute(
        self,
        actor: PluginActor,
        operation_id: str,
        approval_id: str,
        public_query: str,
    ) -> SearchRunResult:
        if self._closed:
            raise RuntimeError("search service is closed")
        async with self._lock:
            task = self._active.get(operation_id)
            if task is None:
                task = asyncio.create_task(
                    self._execute(actor, operation_id, approval_id, public_query)
                )
                self._active[operation_id] = task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            task.cancel()
            with suppress(BaseException):
                await task
            with suppress(PluginActionError):
                await self._operations.terminate(actor, operation_id, "CANCELLED")
            raise
        finally:
            if task.done():
                async with self._lock:
                    if self._active.get(operation_id) is task:
                        self._active.pop(operation_id, None)

    async def aclose(self) -> None:
        self._closed = True
        active = tuple(self._active.values())
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)
        drains = tuple(self._drains)
        if drains:
            await asyncio.gather(*drains, return_exceptions=True)

    async def _execute(
        self,
        actor: PluginActor,
        operation_id: str,
        approval_id: str,
        public_query: str,
    ) -> SearchRunResult:
        reservation = await self._operations.reserve_primary(
            actor, operation_id, approval_id, public_query
        )
        execution = reservation.execution
        if not reservation.acquired:
            if execution.state in {
                "COMPLETED",
                "EMPTY",
                "FAILED",
                "CANCELLED",
                "INTERRUPTED",
                "UNKNOWN",
            }:
                return SearchRunResult(execution, None)
            if execution.owner_boot_token == self._operations.owner_token:
                return SearchRunResult(execution, None)
            unknown = await self._operations.terminate(
                actor, operation_id, "UNKNOWN", error_code="RESULT_UNKNOWN"
            )
            return SearchRunResult(unknown, None)

        overall_deadline = self._clock() + 22.0
        claim_token = execution.claim_token
        try:
            primary, secondary = await self._load_configs(execution.search_revision)
            try:
                result = await self._attempt(
                    actor,
                    operation_id,
                    claim_token,
                    "PRIMARY",
                    primary,
                    public_query,
                    overall_deadline,
                )
            except SearchHttpError as primary_error:
                if primary_error.code not in _FALLBACK_CODES:
                    failed = await self._operations.finish(
                        actor,
                        operation_id,
                        claim_token,
                        "FAILED",
                        provider=primary.provider,
                        error_code=primary_error.code,
                    )
                    return SearchRunResult(failed, None)
                execution = await self._operations.reserve_secondary(
                    actor, operation_id, claim_token
                )
                try:
                    result = await self._attempt(
                        actor,
                        operation_id,
                        claim_token,
                        "SECONDARY",
                        secondary,
                        public_query,
                        overall_deadline,
                    )
                except SearchHttpError as secondary_error:
                    failed = await self._operations.finish(
                        actor,
                        operation_id,
                        claim_token,
                        "FAILED",
                        provider=secondary.provider,
                        error_code=secondary_error.code,
                    )
                    return SearchRunResult(failed, None)

            if result.status == "SUCCESS":
                finished = await self._operations.finish(
                    actor,
                    operation_id,
                    claim_token,
                    "COMPLETED",
                    provider=result.provider,
                )
            else:
                finished = await self._operations.finish(
                    actor,
                    operation_id,
                    claim_token,
                    "EMPTY",
                    provider=result.provider,
                )
            return SearchRunResult(finished, result)
        except asyncio.CancelledError:
            with suppress(PluginActionError):
                await self._operations.terminate(actor, operation_id, "CANCELLED")
            raise
        except PluginActionError:
            with suppress(PluginActionError):
                await self._operations.terminate(
                    actor, operation_id, "UNKNOWN", error_code="COMMIT_UNKNOWN"
                )
            raise
        except Exception:
            with suppress(PluginActionError):
                await self._operations.terminate(
                    actor, operation_id, "UNKNOWN", error_code="RESULT_UNKNOWN"
                )
            raise

    async def _attempt(
        self,
        actor: PluginActor,
        operation_id: str,
        claim_token: str,
        role: str,
        config: SearchSourceConfig,
        public_query: str,
        overall_deadline: float,
    ) -> SearchHttpResult:
        attempt = SearchHttpAttempt(
            config,
            transport=self._transport,
            clock=self._clock,
        )

        async def pre_send() -> None:
            await self._operations.pre_send(
                actor,
                operation_id,
                claim_token,
                cast(Any, role),
            )

        try:
            return await attempt.search(
                public_query,
                deadline_at=overall_deadline,
                pre_send=pre_send,
            )
        finally:
            self._track_drain(attempt)

    def _track_drain(self, attempt: SearchHttpAttempt) -> None:
        task = asyncio.create_task(attempt.aclose())
        self._drains.add(task)
        task.add_done_callback(self._drains.discard)

    async def _load_configs(
        self, expected_revision: int
    ) -> tuple[SearchSourceConfig, SearchSourceConfig]:
        record = await self._facade.get_record(SEARCH_SOURCES, CONFIG_KEY)
        if record is None or record.revision != expected_revision:
            raise RuntimeError("search configuration changed")
        primary_part = cast(Mapping[str, Any], record.payload.get("primary") or {})
        secondary_part = cast(Mapping[str, Any], record.payload.get("secondary") or {})
        credential: SecretStr | None = None
        ref = secondary_part.get("credential_ref")
        owner = secondary_part.get("credential_owner_user_id")
        if isinstance(ref, str) and isinstance(owner, int):
            material = await self._facade.resolve_credential(ref, owner)
            secret = material.get("api_key") if isinstance(material, Mapping) else None
            if isinstance(secret, str):
                credential = SecretStr(secret)
        primary = SearchSourceConfig(
            provider="SEARXNG",
            base_url=primary_part.get("base_url"),
            ca_bundle=primary_part.get("ca_bundle"),
        )
        secondary = SearchSourceConfig(
            provider="FIRECRAWL",
            base_url=secondary_part.get("base_url"),
            ca_bundle=secondary_part.get("ca_bundle"),
            credential=credential,
        )
        return primary, secondary
