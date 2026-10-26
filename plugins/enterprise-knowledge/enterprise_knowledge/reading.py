"""Evidence-backed, budgeted continuous reads from the authorized knowledge source."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from octop.infra.agents.plugins.host_contracts import (
    HostRecord,
    PluginActionError,
    PluginActor,
    PluginHostError,
    RecordCheck,
    RecordWrite,
)

from .connections import (
    BINDINGS,
    CONNECTION_INTENTS,
    CONNECTIONS,
    GRANTS,
    ConnectionService,
)
from .evidence import EVIDENCE
from .i18n import error_message
from .models import EvidenceRecord, OriginalInput, OriginalOutput, ReadInput, ReadPage
from .operations import OPERATIONS, OperationService
from .policy import ScopePolicy
from .sessions import SESSIONS, _session_key
from .weknora import OperationBudget, WeKnoraError, WeKnoraProvider

READING_CURSORS = "reading_cursors"
_PAGE_SIZE = 100
_WINDOW_SIZE = 8000
_CURSOR_LIFETIME = timedelta(minutes=30)


def _fail(actor: PluginActor, code: str, status: int = 409) -> PluginActionError:
    return PluginActionError(code, error_message(code, actor.locale), status=status)


def _framed(hasher: Any, value: str | int) -> None:
    data = str(value).encode("utf-8")
    hasher.update(len(data).to_bytes(8, "big"))
    hasher.update(data)


@dataclass(frozen=True)
class _ScanResult:
    digest: str
    total_chunks: int
    text_length: int
    start_offset: int
    content: str
    next_position: dict[str, Any] | None


def _record_key(operation_id: str, evidence_id: str) -> str:
    return json.dumps([operation_id, evidence_id], separators=(",", ":"))


class ReadingService:
    def __init__(
        self,
        facade: Any,
        *,
        provider_factory: Callable[..., Any] = WeKnoraProvider,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._facade = facade
        self._provider_factory = provider_factory
        self._clock = clock
        self._operations = OperationService(facade)

    async def read(self, actor: PluginActor, request: ReadInput) -> ReadPage:
        budget = OperationBudget()
        provider: Any | None = None
        try:
            async with asyncio.timeout(budget.remaining_lifetime_seconds()):
                operation, evidence_record, evidence = await self._owned_source(
                    actor, request.operation_id, request.evidence_id
                )
                cursor: HostRecord | None = None
                if request.continuation is not None:
                    cursor = await self._facade.get_record(READING_CURSORS, request.continuation)
                    if cursor is None or not self._cursor_matches(
                        cursor, actor, operation, evidence_record, evidence, request
                    ):
                        raise _fail(actor, "READ_CURSOR_INVALID", 400)
                    if self._expired(cursor.payload):
                        raise _fail(actor, "READ_CURSOR_INVALID", 400)

                policy = ScopePolicy(self._facade)
                binding, connection, grant, session = await policy._current(actor)
                self._assert_local_source(
                    actor, operation, evidence, binding, connection, grant, session, policy
                )
                intent_revision = await ConnectionService(self._facade).assert_connection_available(
                    connection.logical_key, actor
                )
                if cursor is not None and any(
                    cursor.payload.get(field) != value
                    for field, value in (
                        ("connection_id", connection.logical_key),
                        ("config_revision", connection.revision),
                        ("binding_revision", binding.revision),
                        ("grant_revision", grant.revision),
                        ("session_revision", session.revision),
                        ("intent_revision", intent_revision),
                    )
                ):
                    raise _fail(actor, "READ_SOURCE_CHANGED")

                connection_id = operation.payload.get("connection_id")
                config_revision = operation.payload.get("config_revision")
                if not isinstance(connection_id, str) or type(config_revision) is not int:
                    raise _fail(actor, "READ_SOURCE_CHANGED")
                provider = self._provider_factory(
                    self._facade,
                    actor,
                    connection_id,
                    config_revision,
                    ca_bundle=os.environ.get("OCTOP_WEKNORA_CA_BUNDLE"),
                    budget=budget,
                )
                scope = await ScopePolicy(self._facade, provider).query_scope(actor)
                self._assert_operation_source(actor, operation, evidence, scope)
                if evidence.kb_id not in scope.effective_kb_ids:
                    raise _fail(actor, "READ_SOURCE_CHANGED")

                knowledge_id = evidence.knowledge_id or ""
                detail = await provider.knowledge_detail(knowledge_id, scope.effective_kb_ids)
                if detail.knowledge_base_id != evidence.kb_id:
                    raise _fail(actor, "UPSTREAM_SCHEMA_INVALID", 503)
                scan = await self._scan(
                    actor,
                    operation,
                    evidence_record,
                    scope,
                    intent_revision,
                    provider,
                    evidence,
                    cursor,
                )
                if cursor is not None and cursor.payload.get("digest") != scan.digest:
                    raise _fail(actor, "READ_SOURCE_CHANGED")

                if scan.text_length == 0:
                    response = ReadPage(
                        operation_id=request.operation_id,
                        evidence_id=request.evidence_id,
                        knowledge_id=knowledge_id,
                        status="EMPTY",
                        content="",
                        truncated=False,
                    )
                    await self._final_check(
                        actor, operation, evidence_record, scope, intent_revision
                    )
                    return response

                window = scan.content
                has_more = scan.start_offset + len(window) < scan.text_length
                continuation = None
                if has_more:
                    continuation = str(uuid4())
                    position = scan.next_position
                    if position is None:
                        raise _fail(actor, "UPSTREAM_SCHEMA_INVALID", 503)
                    issued_at = cursor.payload["issued_at"] if cursor is not None else self._now()
                    expires_at = (
                        cursor.payload["expires_at"]
                        if cursor is not None
                        else (datetime.fromisoformat(issued_at) + _CURSOR_LIFETIME).isoformat()
                    )
                    cursor_payload = self._cursor_payload(
                        actor,
                        operation,
                        evidence_record,
                        evidence,
                        scope,
                        intent_revision,
                        scan.digest,
                        issued_at,
                        expires_at,
                        continuation,
                        position,
                    )
                response = ReadPage(
                    operation_id=request.operation_id,
                    evidence_id=request.evidence_id,
                    knowledge_id=knowledge_id,
                    status="SUCCESS",
                    content=window,
                    truncated=has_more,
                    continuation=continuation,
                )
                if has_more:
                    checks = await self._checks(
                        actor, operation, evidence_record, scope, intent_revision
                    )
                    try:
                        await self._facade.commit(
                            (
                                RecordWrite(
                                    READING_CURSORS,
                                    continuation,
                                    actor.user_id,
                                    cursor_payload,
                                    0,
                                ),
                            ),
                            checks=checks,
                        )
                    except PluginHostError as exc:
                        if exc.code == "conflict":
                            raise _fail(actor, "READ_SOURCE_CHANGED") from None
                        raise
                else:
                    await self._final_check(
                        actor, operation, evidence_record, scope, intent_revision
                    )
                return response
        except TimeoutError:
            raise _fail(actor, "UPSTREAM_BUDGET_EXCEEDED", 503) from None
        except WeKnoraError as exc:
            raise _fail(actor, exc.code, exc.status) from None
        finally:
            if provider is not None:
                await provider.aclose()

    async def original(self, actor: PluginActor, request: OriginalInput) -> OriginalOutput:
        operation, evidence_record, evidence = await self._owned_source(
            actor, request.operation_id, request.evidence_id
        )
        if evidence.source_kind != "KNOWLEDGE":
            raise _fail(actor, "EVIDENCE_SOURCE_INVALID", 400)
        policy = ScopePolicy(self._facade)
        binding, connection, grant, session = await policy._current(actor)
        self._assert_local_source(
            actor, operation, evidence, binding, connection, grant, session, policy
        )
        intent_revision = await ConnectionService(self._facade).assert_connection_available(
            connection.logical_key, actor
        )
        if (
            connection.logical_key != operation.payload.get("connection_id")
            or connection.revision != operation.payload.get("config_revision")
            or binding.revision != operation.payload.get("binding_revision")
            or grant.revision != operation.payload.get("grant_revision")
            or session.revision != operation.payload.get("session_revision")
            or evidence.kb_id not in set(session.selected_kb_ids)
        ):
            raise _fail(actor, "READ_SOURCE_CHANGED")
        decision = SimpleNamespace(
            source_revisions=SimpleNamespace(
                connection_id=connection.logical_key,
                config_revision=connection.revision,
                binding_revision=binding.revision,
                grant_revision=grant.revision,
            ),
            session_revision=session.revision,
        )
        response = OriginalOutput(
            operation_id=request.operation_id, evidence_id=request.evidence_id
        )
        await self._final_check(actor, operation, evidence_record, decision, intent_revision)
        return response

    async def _owned_source(
        self, actor: PluginActor, operation_id: str, evidence_id: str
    ) -> tuple[HostRecord, HostRecord, EvidenceRecord]:
        operation = await self._operations._owned_operation(actor, operation_id)
        evidence_record = await self._facade.get_record(
            EVIDENCE, _record_key(operation_id, evidence_id)
        )
        if evidence_record is None or evidence_record.owner_user_id != actor.user_id:
            raise _fail(actor, "NOT_FOUND", 404)
        evidence = EvidenceRecord.model_validate(evidence_record.payload)
        if evidence.source_kind != "KNOWLEDGE":
            raise _fail(actor, "EVIDENCE_SOURCE_INVALID", 400)
        if (
            evidence.operation_id != operation_id
            or evidence.connection_id != operation.payload.get("connection_id")
            or evidence.user_id != actor.user_id
            or evidence.agent_id != actor.agent_id
            or evidence.thread_id != actor.thread_id
        ):
            raise _fail(actor, "ACCESS_DENIED", 403)
        return operation, evidence_record, evidence

    @staticmethod
    def _assert_operation_source(
        actor: PluginActor, operation: HostRecord, evidence: EvidenceRecord, scope: Any
    ) -> None:
        payload = operation.payload
        revisions = scope.source_revisions
        if (
            scope.session_revision != payload.get("session_revision")
            or revisions.connection_id != payload.get("connection_id")
            or revisions.config_revision != payload.get("config_revision")
            or revisions.binding_revision != payload.get("binding_revision")
            or revisions.grant_revision != payload.get("grant_revision")
            or evidence.connection_id != revisions.connection_id
        ):
            raise _fail(actor, "READ_SOURCE_CHANGED")

    @staticmethod
    def _assert_local_source(
        actor: PluginActor,
        operation: HostRecord,
        evidence: EvidenceRecord,
        binding: HostRecord,
        connection: HostRecord,
        grant: HostRecord,
        session: Any,
        policy: ScopePolicy,
    ) -> None:
        payload = operation.payload
        authorized = policy._local_scope(binding.payload, connection, grant, actor)
        if (
            session.mode != "KB_ONLY"
            or session.invalid
            or session.connection_id != connection.logical_key
            or session.config_revision != connection.revision
            or session.binding_revision != binding.revision
            or session.grant_revision != grant.revision
            or session.revision != payload.get("session_revision")
            or connection.logical_key != payload.get("connection_id")
            or connection.revision != payload.get("config_revision")
            or binding.revision != payload.get("binding_revision")
            or grant.revision != payload.get("grant_revision")
            or evidence.connection_id != connection.logical_key
        ):
            raise _fail(actor, "READ_SOURCE_CHANGED")
        if evidence.kb_id not in authorized or evidence.kb_id not in session.selected_kb_ids:
            raise _fail(actor, "SCOPE_INVALID", 403)

    async def _scan(
        self,
        actor: PluginActor,
        operation: HostRecord,
        evidence_record: HostRecord,
        scope: Any,
        intent_revision: int,
        provider: Any,
        evidence: EvidenceRecord,
        cursor: HostRecord | None,
    ) -> _ScanResult:
        knowledge_id = evidence.knowledge_id or ""
        hasher = hashlib.sha256()
        total: int | None = None
        expected_pages: int | None = None
        previous_index = -1
        seen_ids: set[str] = set()
        total_text_length = 0
        start_offset: int | None = 0 if cursor is None else None
        target_found = cursor is None
        target = (
            (cursor.payload.get("next_chunk_id"), cursor.payload.get("next_chunk_index"))
            if cursor is not None
            else None
        )
        target_offset = cursor.payload.get("character_offset") if cursor is not None else None
        window_parts: list[str] = []
        next_position: dict[str, Any] | None = None
        page = 1
        while expected_pages is None or page <= expected_pages:
            # Recheck the original local authorization snapshot before each page request.
            await self._verify_checks(actor, operation, evidence_record, scope, intent_revision)
            result = await provider.chunks(
                knowledge_id, scope.effective_kb_ids, page=page, page_size=_PAGE_SIZE
            )
            if result.page != page or result.page_size != _PAGE_SIZE:
                raise WeKnoraError("UPSTREAM_SCHEMA_INVALID", provider._actor.locale)
            if total is None:
                total = result.total
                expected_pages = max(1, math.ceil(total / _PAGE_SIZE))
                for value in (knowledge_id, evidence.kb_id or "", _PAGE_SIZE, total, total):
                    _framed(hasher, value)
            elif result.total != total:
                raise WeKnoraError("UPSTREAM_SCHEMA_INVALID", provider._actor.locale)
            expected_count = min(_PAGE_SIZE, max(0, total - (page - 1) * _PAGE_SIZE))
            if len(result.items) != expected_count:
                raise WeKnoraError("UPSTREAM_SCHEMA_INVALID", provider._actor.locale)
            for chunk in result.items:
                if chunk.id in seen_ids or chunk.chunk_index <= previous_index:
                    raise WeKnoraError("UPSTREAM_SCHEMA_INVALID", provider._actor.locale)
                if (
                    chunk.knowledge_id != knowledge_id
                    or chunk.knowledge_base_id != evidence.kb_id
                    or chunk.chunk_type != "text"
                ):
                    raise WeKnoraError("UPSTREAM_SCHEMA_INVALID", provider._actor.locale)
                seen_ids.add(chunk.id)
                previous_index = chunk.chunk_index
                for value in (chunk.id, chunk.chunk_index, chunk.content):
                    _framed(hasher, value)

                chunk_start = total_text_length
                chunk_end = chunk_start + len(chunk.content)
                if target == (chunk.id, chunk.chunk_index):
                    target_found = True
                    if type(target_offset) is not int or not 0 <= target_offset <= len(
                        chunk.content
                    ):
                        raise _fail(actor, "READ_SOURCE_CHANGED")
                    start_offset = chunk_start + target_offset
                if start_offset is not None:
                    window_end = start_offset + _WINDOW_SIZE
                    overlap_start = max(chunk_start, start_offset)
                    overlap_end = min(chunk_end, window_end)
                    if overlap_end > overlap_start:
                        part = chunk.content[
                            overlap_start - chunk_start : overlap_end - chunk_start
                        ]
                        window_parts.append(part)
                    if (
                        chunk.content
                        and next_position is None
                        and (chunk_start <= window_end < chunk_end or window_end == chunk_start)
                    ):
                        next_position = {
                            "next_chunk_id": chunk.id,
                            "next_chunk_index": chunk.chunk_index,
                            "character_offset": window_end - chunk_start,
                        }
                total_text_length = chunk_end
            if result.items:
                del chunk
            del result
            page += 1
        if total is None or len(seen_ids) != total:
            raise WeKnoraError("UPSTREAM_SCHEMA_INVALID", provider._actor.locale)
        if not target_found or start_offset is None or start_offset > total_text_length:
            raise _fail(actor, "READ_SOURCE_CHANGED")
        if cursor is not None and total_text_length > 0 and start_offset >= total_text_length:
            raise _fail(actor, "READ_SOURCE_CHANGED")
        return _ScanResult(
            digest=hasher.hexdigest(),
            total_chunks=total,
            text_length=total_text_length,
            start_offset=start_offset,
            content="".join(window_parts),
            next_position=next_position,
        )

    def _cursor_matches(
        self,
        record: HostRecord,
        actor: PluginActor,
        operation: HostRecord,
        evidence_record: HostRecord,
        evidence: EvidenceRecord,
        request: ReadInput,
    ) -> bool:
        payload = record.payload
        expected = {
            "token_id": request.continuation,
            "user_id": actor.user_id,
            "agent_id": actor.agent_id,
            "thread_id": actor.thread_id,
            "operation_id": request.operation_id,
            "operation_revision": operation.revision,
            "evidence_id": request.evidence_id,
            "evidence_revision": evidence_record.revision,
            "connection_id": evidence.connection_id,
            "knowledge_id": evidence.knowledge_id,
            "kb_id": evidence.kb_id,
        }
        return record.owner_user_id == actor.user_id and all(
            payload.get(key) == value for key, value in expected.items()
        )

    def _expired(self, payload: dict[str, Any]) -> bool:
        try:
            expires_at = datetime.fromisoformat(payload["expires_at"])
            return expires_at.tzinfo is None or self._clock() >= expires_at
        except (KeyError, TypeError, ValueError):
            return True

    def _cursor_payload(
        self,
        actor: PluginActor,
        operation: HostRecord,
        evidence_record: HostRecord,
        evidence: EvidenceRecord,
        scope: Any,
        intent_revision: int,
        digest: str,
        issued_at: str,
        expires_at: str,
        token_id: str,
        position: dict[str, Any],
    ) -> dict[str, Any]:
        revisions = scope.source_revisions
        return {
            "schema_id": "octop.enterprise.knowledge.v1",
            "version": 1,
            "token_id": token_id,
            "user_id": actor.user_id,
            "agent_id": actor.agent_id,
            "thread_id": actor.thread_id,
            "operation_id": operation.payload["operation_id"],
            "operation_revision": operation.revision,
            "evidence_id": evidence.evidence_id,
            "evidence_revision": evidence_record.revision,
            "connection_id": revisions.connection_id,
            "knowledge_id": evidence.knowledge_id,
            "kb_id": evidence.kb_id,
            "config_revision": revisions.config_revision,
            "binding_revision": revisions.binding_revision,
            "grant_revision": revisions.grant_revision,
            "session_revision": scope.session_revision,
            "intent_revision": intent_revision,
            "digest": digest,
            **position,
            "issued_at": issued_at,
            "expires_at": expires_at,
        }

    async def _checks(
        self,
        actor: PluginActor,
        operation: HostRecord,
        evidence_record: HostRecord,
        scope: Any,
        intent_revision: int,
    ) -> tuple[RecordCheck, ...]:
        revisions = scope.source_revisions
        checks = [
            RecordCheck(OPERATIONS, operation.logical_key, operation.revision),
            RecordCheck(EVIDENCE, evidence_record.logical_key, evidence_record.revision),
            RecordCheck(
                SESSIONS,
                _session_key(actor.user_id, actor.agent_id or "", actor.thread_id or ""),
                scope.session_revision,
            ),
            RecordCheck(BINDINGS, actor.agent_id or "", revisions.binding_revision),
        ]
        if revisions.connection_id is not None:
            checks.extend(
                (
                    RecordCheck(CONNECTIONS, revisions.connection_id, revisions.config_revision),
                    RecordCheck(
                        GRANTS,
                        f"{revisions.connection_id}:{actor.user_id}",
                        revisions.grant_revision,
                    ),
                    RecordCheck(CONNECTION_INTENTS, revisions.connection_id, intent_revision),
                )
            )
        return tuple(checks)

    async def _final_check(
        self,
        actor: PluginActor,
        operation: HostRecord,
        evidence_record: HostRecord,
        scope: Any,
        intent_revision: int,
    ) -> None:
        await self._verify_checks(actor, operation, evidence_record, scope, intent_revision)

    async def _verify_checks(
        self,
        actor: PluginActor,
        operation: HostRecord,
        evidence_record: HostRecord,
        scope: Any,
        intent_revision: int,
    ) -> None:
        try:
            await self._facade.commit(
                (),
                checks=await self._checks(
                    actor, operation, evidence_record, scope, intent_revision
                ),
            )
        except PluginHostError as exc:
            if exc.code == "conflict":
                raise _fail(actor, "READ_SOURCE_CHANGED") from None
            raise

    def _now(self) -> str:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("cursor clock must return an aware datetime")
        return value.astimezone(UTC).isoformat()
