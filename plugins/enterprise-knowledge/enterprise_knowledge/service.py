"""Flat application service exposed through the versioned Host service proxy."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from types import MappingProxyType
from typing import Any

from octop.infra.agents.plugins.host_contracts import (
    HostRecord,
    PluginActionError,
    PluginActor,
    PluginExecution,
    PluginHostError,
    RecordCheck,
    RecordWrite,
)

from .connections import BINDINGS, CONNECTION_INTENTS, CONNECTIONS, GRANTS, ConnectionService
from .evidence import EVIDENCE, EvidenceService
from .i18n import error_message
from .models import (
    EvidenceListInput,
    EvidenceListOutput,
    KnowledgeBaseSummary,
    KnowledgeListOutput,
    OperationOutput,
    OperationPrepareInput,
    OperationRecord,
    OriginalInput,
    OriginalOutput,
    ReadInput,
    ReadPage,
    RetrievalOutput,
    SessionSnapshot,
)
from .operations import OPERATIONS, TERMINAL, OperationService
from .policy import ScopePolicy, apply_requested_ids
from .reading import ReadingService
from .sessions import SESSIONS, SessionService, _session_key
from .weknora import OperationBudget, SearchHit, WeKnoraError, WeKnoraProvider

_REQUIRED = {"enterprise-knowledge": "0.1.0", "enterprise-policy": "0.1.0"}
_CONTEXT_KEY = "operation_id"


def _fail(actor: PluginActor, code: str, status: int = 409) -> PluginActionError:
    return PluginActionError(code, error_message(code, actor.locale), status=status)


def _record_key(operation_id: str, evidence_id: str) -> str:
    return json.dumps([operation_id, evidence_id], separators=(",", ":"))


class KnowledgeService:
    """Flat, generation-bound producer API; internal adapters never escape."""

    def __init__(
        self,
        facade: Any,
        *,
        provider_factory: Callable[..., Any] = WeKnoraProvider,
        tool_functions: Mapping[str, Callable[..., Any]] | None = None,
    ) -> None:
        self._facade = facade
        self._provider_factory = provider_factory
        self._tool_functions = MappingProxyType(dict(tool_functions or {}))
        self._operations = OperationService(facade)
        self._evidence = EvidenceService(facade)
        self._sessions = SessionService(facade)

    async def prepare(self, actor: PluginActor, request: OperationPrepareInput) -> OperationOutput:
        session = await self._sessions.get(actor)
        if session.revision != request.expected_session_revision:
            raise _fail(actor, "CONFIG_CONFLICT")
        binding, connection, grant, checked_session = await ScopePolicy(self._facade)._current(
            actor
        )
        self._assert_local_session(actor, binding, connection, grant, checked_session)
        if (
            checked_session.revision != request.expected_session_revision
            or checked_session.mode != "KB_ONLY"
            or checked_session.invalid
            or not checked_session.selected_kb_ids
        ):
            raise _fail(actor, "SCOPE_INVALID")
        local = ScopePolicy._local_scope(binding.payload, connection, grant, actor)
        if not set(checked_session.selected_kb_ids).issubset(local):
            raise _fail(actor, "SCOPE_INVALID")
        operation = await self._operations.create(actor, checked_session)
        return OperationOutput(operation=operation)

    async def evidence_list(
        self, actor: PluginActor, request: EvidenceListInput
    ) -> EvidenceListOutput:
        operation = await self._owned_operation(actor, request.operation_id)
        binding, connection, grant, session = await ScopePolicy(self._facade)._current(actor)
        self._assert_pinned(actor, operation, binding, connection, grant, session)
        intent_revision = await self._assert_intent(actor, connection.logical_key)
        evidence = await self._evidence.list_for_operation(actor, request.operation_id)
        allowed = ScopePolicy._local_scope(binding.payload, connection, grant, actor)
        if any(
            item.kb_id not in allowed or item.kb_id not in session.selected_kb_ids
            for item in evidence
        ):
            raise _fail(actor, "SCOPE_INVALID", 403)
        await self._check_commit(
            actor, operation, session, binding, connection, grant, intent_revision
        )
        return EvidenceListOutput(operation=self._operation_dto(operation), evidence=evidence)

    async def read(self, actor: PluginActor, request: ReadInput) -> ReadPage:
        return await ReadingService(self._facade).read(actor, request)

    async def original(self, actor: PluginActor, request: OriginalInput) -> OriginalOutput:
        return await ReadingService(self._facade).original(actor, request)

    async def retrieve_current(self, question: str) -> RetrievalOutput:
        budget = OperationBudget()
        provider: Any | None = None
        actor: PluginActor | None = None
        operation: HostRecord | None = None
        claimed_operation: HostRecord | None = None
        try:
            async with asyncio.timeout(budget.remaining_lifetime_seconds()):
                _execution, actor, operation_id = await self._current_operation()
                if not isinstance(question, str) or not question.strip():
                    raise _fail(actor, "CONFIG_INVALID", 400)
                operation = await self._owned_operation(actor, operation_id)
                if operation.payload.get("status") != "VALIDATING":
                    raise _fail(actor, "CONFIG_CONFLICT")
                binding, connection, grant, session = await ScopePolicy(self._facade)._current(
                    actor
                )
                self._assert_pinned(actor, operation, binding, connection, grant, session)
                claim_intent_revision = await self._assert_intent(actor, connection.logical_key)
                local = ScopePolicy._local_scope(binding.payload, connection, grant, actor)
                if not session.selected_kb_ids or not set(session.selected_kb_ids).issubset(local):
                    raise _fail(actor, "SCOPE_INVALID")

                claimed_payload = {**operation.payload, "status": "RETRIEVING", "error_code": None}
                checks = self._checks(
                    actor, operation, session, binding, connection, grant, claim_intent_revision
                )
                try:
                    claimed = await self._facade.commit(
                        (
                            RecordWrite(
                                OPERATIONS,
                                operation_id,
                                actor.user_id,
                                claimed_payload,
                                operation.revision,
                            ),
                        ),
                        checks=checks,
                    )
                except PluginHostError as exc:
                    if exc.code == "conflict":
                        raise _fail(actor, "CONFIG_CONFLICT") from None
                    raise
                operation = claimed[0]
                claimed_operation = operation
                provider = self._provider_factory(
                    self._facade,
                    actor,
                    connection.logical_key,
                    connection.revision,
                    budget=budget,
                )
                scope = await ScopePolicy(self._facade, provider).query_scope(actor)
                if not set(scope.effective_kb_ids).issubset(set(session.selected_kb_ids)):
                    raise _fail(actor, "SCOPE_INVALID")
                metadata = await provider.metadata_for(
                    connection.logical_key, scope.effective_kb_ids, actor
                )
                metadata_by_id = {
                    row.get("kb_id"): row
                    for row in metadata
                    if isinstance(row, Mapping) and row.get("kb_id") in scope.effective_kb_ids
                }
                observed = [
                    KnowledgeBaseSummary(
                        kb_id=kb_id,
                        name=(
                            metadata_by_id[kb_id].get("name")
                            if isinstance(metadata_by_id.get(kb_id, {}).get("name"), str)
                            else None
                        ),
                    )
                    for kb_id in scope.effective_kb_ids
                ]
                hits = await provider.search(question, scope.effective_kb_ids)
                _current_execution, current_actor, current_id = await self._current_operation()
                if current_id != operation_id or current_actor != actor:
                    raise _fail(actor, "ACCESS_DENIED", 403)
                current = await self._owned_operation(actor, operation_id)
                binding, connection, grant, session = await ScopePolicy(self._facade)._current(
                    actor
                )
                self._assert_pinned(actor, current, binding, connection, grant, session)
                await self._assert_intent(actor, connection.logical_key)
                if (
                    current.revision != operation.revision
                    or current.payload.get("status") != "RETRIEVING"
                ):
                    raise _fail(actor, "CONFIG_CONFLICT")
                if (
                    scope.session_revision != session.revision
                    or scope.source_revisions.connection_id != connection.logical_key
                    or scope.source_revisions.config_revision != connection.revision
                    or scope.source_revisions.binding_revision != binding.revision
                    or scope.source_revisions.grant_revision != grant.revision
                ):
                    raise _fail(actor, "READ_SOURCE_CHANGED")

                evidence = (
                    await self._evidence._validated_source_records(
                        actor,
                        operation_id,
                        current,
                        [self._hit_payload(hit, connection.logical_key) for hit in hits],
                    )
                    if hits
                    else []
                )
                final_status = "GENERATING" if evidence else "EMPTY"
                new_operation = {
                    **current.payload,
                    "status": final_status,
                    "error_code": None,
                    "observed_knowledge_bases": [item.model_dump(mode="json") for item in observed],
                }
                writes = [
                    RecordWrite(
                        EVIDENCE,
                        _record_key(operation_id, item.evidence_id),
                        actor.user_id,
                        item.model_dump(mode="json"),
                        0,
                    )
                    for item in evidence
                ]
                writes.append(
                    RecordWrite(
                        OPERATIONS, operation_id, actor.user_id, new_operation, current.revision
                    )
                )
                try:
                    saved = await self._facade.commit(
                        tuple(writes),
                        checks=self._checks(
                            actor,
                            current,
                            session,
                            binding,
                            connection,
                            grant,
                            claim_intent_revision,
                        ),
                    )
                except PluginHostError as exc:
                    if exc.code == "conflict":
                        raise _fail(actor, "READ_SOURCE_CHANGED") from None
                    raise
                result_operation = OperationRecord.model_validate(
                    {**saved[-1].payload, "revision": saved[-1].revision}
                )
                return RetrievalOutput(operation=result_operation, evidence=evidence)
        except TimeoutError:
            if actor is not None and claimed_operation is not None:
                await self._fail_claimed_operation(
                    actor, claimed_operation, "UPSTREAM_BUDGET_EXCEEDED"
                )
            if actor is None:
                raise PluginHostError("unavailable") from None
            raise _fail(actor, "UPSTREAM_BUDGET_EXCEEDED", 503) from None
        except WeKnoraError as exc:
            if actor is not None and claimed_operation is not None:
                await self._fail_claimed_operation(actor, claimed_operation, exc.code)
            raise _fail(actor, exc.code, exc.status) from None
        except PluginActionError as exc:
            if actor is not None and claimed_operation is not None:
                await self._fail_claimed_operation(actor, claimed_operation, exc.code)
            raise
        finally:
            if provider is not None:
                await provider.aclose()

    async def search_current(self, knowledge_base_ids: list[str] | None = None) -> RetrievalOutput:
        actor, operation = await self._current_generating()
        bases = self._observation(operation)
        local_scope = await self._current_local_scope(actor, operation)
        observed_ids = [item.kb_id for item in bases]
        if not set(observed_ids).issubset(local_scope):
            raise _fail(actor, "SCOPE_INVALID", 403)
        try:
            effective = apply_requested_ids(actor, observed_ids, knowledge_base_ids)
        except (PluginActionError, TypeError, ValueError):
            raise _fail(actor, "SCOPE_INVALID", 403) from None
        evidence = await self._evidence.list_for_operation(actor, operation.payload["operation_id"])
        selected = [item for item in evidence if item.kb_id in effective]
        await self._final_current_check(actor, operation)
        return RetrievalOutput(operation=self._operation_dto(operation), evidence=selected)

    async def list_current(self) -> KnowledgeListOutput:
        actor, operation = await self._current_generating()
        bases = self._observation(operation)
        local_scope = await self._current_local_scope(actor, operation)
        if not {item.kb_id for item in bases}.issubset(local_scope):
            raise _fail(actor, "SCOPE_INVALID", 403)
        await self._final_current_check(actor, operation)
        return KnowledgeListOutput(operation=self._operation_dto(operation), knowledge_bases=bases)

    async def read_current(self, evidence_id: str, continuation: str | None = None) -> ReadPage:
        actor, operation = await self._current_generating()
        evidence = await self._evidence.get(actor, operation.payload["operation_id"], evidence_id)
        observed = {item.kb_id for item in self._observation(operation)}
        local = await self._current_local_scope(actor, operation)
        if evidence.kb_id not in observed or evidence.kb_id not in local:
            raise _fail(actor, "SCOPE_INVALID", 403)
        return await ReadingService(self._facade).read(
            actor,
            ReadInput(
                operation_id=operation.payload["operation_id"],
                evidence_id=evidence_id,
                continuation=continuation,
            ),
        )

    async def owned_tools_current(self, tools: Sequence[Any]) -> dict[str, str]:
        actor, operation = await self._current_generating()
        owned: dict[str, str] = {}
        for tool in tools:
            function = tool.coroutine if hasattr(tool, "coroutine") else getattr(tool, "func", None)
            role = next(
                (
                    candidate
                    for candidate in ("list", "search", "read")
                    if (callback := self._tool_functions.get(candidate)) is not None
                    and callable(callback)
                    and callback is function
                ),
                None,
            )
            name = getattr(tool, "name", None)
            if role is not None and isinstance(name, str) and name:
                owned[role] = name
        await self._final_current_check(actor, operation)
        return owned

    async def complete_current(self) -> OperationOutput:
        _execution, actor, operation_id = await self._current_operation()
        record = await self._owned_operation(actor, operation_id)
        binding, connection, grant, session = await ScopePolicy(self._facade)._current(actor)
        self._assert_pinned(actor, record, binding, connection, grant, session)
        intent_revision = await self._assert_intent(actor, connection.logical_key)
        if record.payload.get("status") in TERMINAL:
            return OperationOutput(operation=self._operation_dto(record))
        if record.payload.get("status") != "GENERATING":
            raise _fail(actor, "CONFIG_CONFLICT")
        payload = {**record.payload, "status": "COMPLETED", "error_code": None}
        try:
            saved = await self._facade.commit(
                (RecordWrite(OPERATIONS, operation_id, actor.user_id, payload, record.revision),),
                checks=self._checks(
                    actor, record, session, binding, connection, grant, intent_revision
                ),
            )
        except PluginHostError as exc:
            if exc.code == "conflict":
                raise _fail(actor, "READ_SOURCE_CHANGED") from None
            raise
        return OperationOutput(operation=self._operation_dto(saved[0]))

    async def deny_current(self, code: str) -> OperationOutput:
        if code not in {"CONFIG_INVALID", "ACCESS_DENIED", "PLUGIN_NOT_READY"}:
            raise PluginHostError("invalid")
        _execution, actor, operation_id = await self._current_operation()
        record = await self._owned_operation(actor, operation_id)
        if record.payload.get("status") != "VALIDATING":
            return OperationOutput(operation=self._operation_dto(record))
        try:
            saved = await self._facade.commit(
                (
                    RecordWrite(
                        OPERATIONS,
                        operation_id,
                        actor.user_id,
                        {**record.payload, "status": "DENIED", "error_code": code},
                        record.revision,
                    ),
                )
            )
        except PluginHostError:
            raise
        return OperationOutput(operation=self._operation_dto(saved[0]))

    async def fail_current(self, operation_id: str, code: str) -> OperationOutput:
        if code not in {
            "ACCESS_DENIED",
            "READ_SOURCE_CHANGED",
            "UPSTREAM_SCHEMA_INVALID",
            "UPSTREAM_UNAVAILABLE",
            "CONFIG_CONFLICT",
        }:
            raise PluginHostError("invalid")
        _execution, actor, current_id = await self._current_operation()
        if current_id != operation_id:
            raise _fail(actor, "ACCESS_DENIED", 403)
        record = await self._owned_operation(actor, operation_id)
        if record.payload.get("status") != "GENERATING":
            return OperationOutput(operation=self._operation_dto(record))
        try:
            saved = await self._facade.commit(
                (
                    RecordWrite(
                        OPERATIONS,
                        operation_id,
                        actor.user_id,
                        {**record.payload, "status": "FAILED", "error_code": code},
                        record.revision,
                    ),
                )
            )
        except PluginHostError:
            raise
        return OperationOutput(operation=self._operation_dto(saved[0]))

    async def _current_operation(self) -> tuple[PluginExecution, PluginActor, str]:
        try:
            execution = await self._facade.current_execution()
        except PluginHostError:
            raise
        required = dict(execution.required_plugins)
        if execution.execution_kind != "turn" or required != _REQUIRED:
            raise _fail(execution.actor, "PLUGIN_NOT_READY", 409)
        actor = execution.actor
        raw = execution.context.get(_CONTEXT_KEY)
        if (
            not actor.agent_id
            or not actor.thread_id
            or not isinstance(raw, str)
            or not self._is_uuid4(raw)
        ):
            raise _fail(actor, "ACCESS_DENIED", 403)
        return execution, actor, raw

    async def _current_generating(self) -> tuple[PluginActor, HostRecord]:
        _execution, actor, operation_id = await self._current_operation()
        operation = await self._owned_operation(actor, operation_id)
        if operation.payload.get("status") != "GENERATING":
            raise _fail(actor, "CONFIG_CONFLICT")
        binding, connection, grant, session = await ScopePolicy(self._facade)._current(actor)
        self._assert_pinned(actor, operation, binding, connection, grant, session)
        await self._assert_intent(actor, connection.logical_key)
        return actor, operation

    async def _current_local_scope(self, actor: PluginActor, operation: HostRecord) -> set[str]:
        binding, connection, grant, session = await ScopePolicy(self._facade)._current(actor)
        self._assert_pinned(actor, operation, binding, connection, grant, session)
        return ScopePolicy._local_scope(binding.payload, connection, grant, actor) & set(
            session.selected_kb_ids
        )

    async def _final_current_check(self, actor: PluginActor, operation: HostRecord) -> None:
        binding, connection, grant, session = await ScopePolicy(self._facade)._current(actor)
        self._assert_pinned(actor, operation, binding, connection, grant, session)
        intent_revision = await self._assert_intent(actor, connection.logical_key)
        await self._check_commit(
            actor, operation, session, binding, connection, grant, intent_revision
        )

    async def _owned_operation(self, actor: PluginActor, operation_id: str) -> HostRecord:
        try:
            return await self._operations._owned_operation(actor, operation_id)
        except PluginActionError:
            raise

    async def _assert_intent(self, actor: PluginActor, connection_id: str) -> int:
        return await ConnectionService(self._facade).assert_connection_available(
            connection_id, actor
        )

    async def _check_commit(
        self,
        actor: PluginActor,
        operation: HostRecord,
        session: SessionSnapshot,
        binding: HostRecord,
        connection: HostRecord,
        grant: HostRecord,
        intent_revision: int | None = None,
    ) -> None:
        try:
            await self._facade.commit(
                (),
                checks=self._checks(
                    actor, operation, session, binding, connection, grant, intent_revision
                ),
            )
        except PluginHostError as exc:
            if exc.code == "conflict":
                raise _fail(actor, "READ_SOURCE_CHANGED") from None
            raise

    @staticmethod
    def _checks(
        actor: PluginActor,
        operation: HostRecord,
        session: SessionSnapshot,
        binding: HostRecord,
        connection: HostRecord,
        grant: HostRecord,
        intent_revision: int | None,
    ) -> tuple[RecordCheck, ...]:
        checks = [
            RecordCheck(OPERATIONS, operation.logical_key, operation.revision),
            RecordCheck(
                SESSIONS,
                _session_key(actor.user_id, actor.agent_id or "", actor.thread_id or ""),
                session.revision,
            ),
            RecordCheck(BINDINGS, binding.logical_key, binding.revision),
            RecordCheck(CONNECTIONS, connection.logical_key, connection.revision),
            RecordCheck(GRANTS, f"{connection.logical_key}:{actor.user_id}", grant.revision),
        ]
        if intent_revision is not None:
            checks.append(RecordCheck(CONNECTION_INTENTS, connection.logical_key, intent_revision))
        return tuple(checks)

    @staticmethod
    def _assert_local_session(
        actor: PluginActor,
        binding: HostRecord,
        connection: HostRecord,
        grant: HostRecord,
        session: SessionSnapshot,
    ) -> None:
        if (
            not binding.payload.get("enabled")
            or not connection.payload.get("enabled")
            or session.connection_id != connection.logical_key
            or session.config_revision != connection.revision
            or session.binding_revision != binding.revision
            or session.grant_revision != grant.revision
            or session.agent_id != actor.agent_id
            or session.thread_id != actor.thread_id
        ):
            raise _fail(actor, "READ_SOURCE_CHANGED")

    @classmethod
    def _assert_pinned(
        cls,
        actor: PluginActor,
        operation: HostRecord,
        binding: HostRecord,
        connection: HostRecord,
        grant: HostRecord,
        session: SessionSnapshot,
    ) -> None:
        cls._assert_local_session(actor, binding, connection, grant, session)
        payload = operation.payload
        if (
            payload.get("session_revision") != session.revision
            or payload.get("connection_id") != connection.logical_key
            or payload.get("config_revision") != connection.revision
            or payload.get("binding_revision") != binding.revision
            or payload.get("grant_revision") != grant.revision
        ):
            raise _fail(actor, "READ_SOURCE_CHANGED")

    @staticmethod
    def _operation_dto(record: HostRecord) -> OperationRecord:
        return OperationRecord.model_validate({**record.payload, "revision": record.revision})

    @staticmethod
    def _observation(operation: HostRecord) -> list[KnowledgeBaseSummary]:
        raw = operation.payload.get("observed_knowledge_bases")
        if not isinstance(raw, list):
            raise _fail_from_record("UPSTREAM_SCHEMA_INVALID")
        return [KnowledgeBaseSummary.model_validate(item) for item in raw]

    @staticmethod
    def _is_uuid4(value: str) -> bool:
        from uuid import UUID

        try:
            return str(UUID(value)) == value and UUID(value).version == 4
        except (ValueError, AttributeError):
            return False

    @staticmethod
    def _hit_payload(hit: SearchHit, connection_id: str) -> dict[str, Any]:
        return {
            "source_kind": "KNOWLEDGE",
            "connection_id": connection_id,
            "kb_id": hit.knowledge_base_id,
            "knowledge_id": hit.knowledge_id,
            "title": hit.title or "",
            "content": hit.content,
            "truncated": hit.truncated,
            "source_version": None,
            "chunk_index": hit.chunk_index,
            "source_page": hit.source_page,
        }

    async def _fail_claimed_operation(
        self, actor: PluginActor, operation: HostRecord, code: str
    ) -> None:
        current = await self._facade.get_record(OPERATIONS, operation.logical_key)
        if (
            current is None
            or current.revision != operation.revision
            or current.payload.get("status") != "RETRIEVING"
        ):
            return
        try:
            await self._facade.commit(
                (
                    RecordWrite(
                        OPERATIONS,
                        operation.logical_key,
                        actor.user_id,
                        {**current.payload, "status": "FAILED", "error_code": code},
                        current.revision,
                    ),
                )
            )
        except PluginHostError:
            return


def _fail_from_record(code: str) -> PluginActionError:
    return PluginActionError(code, error_message(code), status=503)
