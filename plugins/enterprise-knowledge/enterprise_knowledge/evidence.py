"""Trust-boundary persistence for evidence returned by a successful source adapter."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from octop.infra.agents.plugins.host_contracts import (
    PluginActionError,
    PluginActor,
    PluginHostError,
    RecordCheck,
    RecordWrite,
)

from .connections import BINDINGS, CONNECTION_INTENTS, CONNECTIONS, GRANTS, ConnectionService
from .i18n import error_message
from .models import EvidenceRecord
from .operations import OPERATIONS, OperationService
from .search_http import WebSearchHit, validate_result_url
from .search_operations import SEARCH_EXECUTIONS
from .search_settings import CONFIG_KEY, SEARCH_INTENTS, SEARCH_SOURCES
from .sessions import SESSIONS, _session_key

EVIDENCE = "evidence"


def _error(actor: PluginActor, code: str, status: int) -> PluginActionError:
    return PluginActionError(code, error_message(code, actor.locale), status=status)


class EvidenceService:
    def __init__(self, facade: Any) -> None:
        self._facade = facade
        self._operations = OperationService(facade)

    async def record_successful_source(
        self,
        actor: PluginActor,
        operation_id: str,
        *,
        outcome: Literal["SUCCESS", "EMPTY", "FAILED", "DENIED"],
        items: list[dict[str, Any]],
    ) -> list[EvidenceRecord]:
        operation = await self._operations._owned_operation(actor, operation_id)
        if outcome != "SUCCESS":
            raise _error(actor, "EVIDENCE_SOURCE_INVALID", 400)
        payloads = await self._validated_source_records(actor, operation_id, operation, items)
        checks = [
            RecordCheck(OPERATIONS, operation_id, operation.revision),
            RecordCheck(
                SESSIONS,
                _session_key(actor.user_id, actor.agent_id or "", actor.thread_id or ""),
                operation.payload["session_revision"],
            ),
            RecordCheck(BINDINGS, actor.agent_id or "", operation.payload["binding_revision"]),
        ]
        connection_id = operation.payload.get("connection_id")
        if connection_id is not None:
            intent_revision = await ConnectionService(self._facade).assert_connection_available(
                connection_id, actor
            )
            checks.extend(
                (
                    RecordCheck(CONNECTIONS, connection_id, operation.payload["config_revision"]),
                    RecordCheck(
                        GRANTS,
                        f"{connection_id}:{actor.user_id}",
                        operation.payload["grant_revision"],
                    ),
                    RecordCheck(CONNECTION_INTENTS, connection_id, intent_revision),
                )
            )
        writes = tuple(
            RecordWrite(
                EVIDENCE,
                json.dumps([operation_id, payload.evidence_id], separators=(",", ":")),
                actor.user_id,
                payload.model_dump(mode="json"),
                0,
            )
            for payload in payloads
        )
        try:
            records = await self._facade.commit(writes, checks=tuple(checks))
        except PluginHostError as exc:
            if exc.code == "conflict":
                raise _error(actor, "CONFIG_CONFLICT", 409) from None
            raise
        return [EvidenceRecord.model_validate(record.payload) for record in records]

    async def record_web_result(
        self,
        actor: PluginActor,
        operation_id: str,
        hits: tuple[WebSearchHit, ...],
        *,
        degraded: bool = False,
    ) -> list[EvidenceRecord]:
        operation = await self._operations._owned_operation(actor, operation_id)
        execution = await self._facade.get_record(SEARCH_EXECUTIONS, operation_id)
        if (
            execution is None
            or execution.owner_user_id != actor.user_id
            or execution.payload.get("operation_id") != operation_id
            or execution.payload.get("user_id") != actor.user_id
            or execution.payload.get("agent_id") != actor.agent_id
            or execution.payload.get("thread_id") != actor.thread_id
            or execution.payload.get("state") != "COMPLETED"
            or execution.payload.get("result_provider") not in {"SEARXNG", "FIRECRAWL"}
            or execution.payload.get("search_revision") != operation.payload.get("search_revision")
            or not hits
        ):
            raise _error(actor, "EVIDENCE_SOURCE_INVALID", 400)
        provider = execution.payload["result_provider"]
        if any(hit.provider != provider for hit in hits):
            raise _error(actor, "EVIDENCE_SOURCE_INVALID", 400)
        payloads = await self._validated_source_records(
            actor,
            operation_id,
            operation,
            [
                {
                    "source_kind": "WEB",
                    "provider": hit.provider,
                    "degraded": degraded,
                    "search_revision": execution.payload["search_revision"],
                    "title": hit.title,
                    "content": hit.content,
                    "truncated": hit.truncated,
                    "source_version": hit.source_version,
                    "source_page": hit.source_page,
                    "locator": hit.locator,
                }
                for hit in hits
            ],
        )
        binding = await self._facade.get_record(BINDINGS, actor.agent_id or "")
        sources = await self._facade.get_record(SEARCH_SOURCES, CONFIG_KEY)
        intent = await self._facade.get_record(SEARCH_INTENTS, CONFIG_KEY)
        if (
            binding is None
            or binding.revision != operation.payload.get("binding_revision")
            or binding.payload.get("web_allowed") is not True
            or sources is None
            or sources.revision != operation.payload.get("search_revision")
            or intent is not None
            and intent.payload.get("state") in {"PREPARING", "BLOCKED"}
        ):
            raise _error(actor, "CONFIG_CONFLICT", 409)
        checks = (
            RecordCheck(OPERATIONS, operation_id, operation.revision),
            RecordCheck(SEARCH_EXECUTIONS, operation_id, execution.revision),
            RecordCheck(BINDINGS, actor.agent_id or "", binding.revision),
            RecordCheck(SEARCH_SOURCES, CONFIG_KEY, sources.revision),
            RecordCheck(SEARCH_INTENTS, CONFIG_KEY, intent.revision if intent is not None else 0),
        )
        writes = tuple(
            RecordWrite(
                EVIDENCE,
                json.dumps([operation_id, payload.evidence_id], separators=(",", ":")),
                actor.user_id,
                payload.model_dump(mode="json"),
                0,
            )
            for payload in payloads
        )
        try:
            records = await self._facade.commit(writes, checks=checks)
        except PluginHostError as exc:
            if exc.code == "conflict":
                raise _error(actor, "CONFIG_CONFLICT", 409) from None
            raise
        return [EvidenceRecord.model_validate(record.payload) for record in records]

    async def _validated_source_records(
        self,
        actor: PluginActor,
        operation_id: str,
        operation: Any,
        items: list[dict[str, Any]],
    ) -> list[EvidenceRecord]:
        if operation.payload.get("status") not in {"RETRIEVING", "GENERATING"} or not items:
            raise _error(actor, "EVIDENCE_SOURCE_INVALID", 400)
        payloads: list[EvidenceRecord] = []
        for item in items:
            if any(
                key in item
                for key in ("evidence_id", "operation_id", "user_id", "agent_id", "thread_id")
            ):
                raise _error(actor, "EVIDENCE_SOURCE_INVALID", 400)
            evidence_data = dict(item)
            if evidence_data.get("source_kind") == "KNOWLEDGE":
                evidence_data.setdefault("provider", "WEKNORA")
            evidence_data.setdefault("retrieved_at", datetime.now(UTC).isoformat())
            payload = EvidenceRecord(
                evidence_id=str(uuid4()),
                operation_id=operation_id,
                user_id=actor.user_id,
                agent_id=actor.agent_id,
                thread_id=actor.thread_id,
                **evidence_data,
            )
            if (
                payload.source_kind == "KNOWLEDGE"
                and payload.connection_id != operation.payload.get("connection_id")
            ):
                raise _error(actor, "EVIDENCE_SOURCE_INVALID", 400)
            payloads.append(payload)
        return payloads

    async def list_for_operation(
        self, actor: PluginActor, operation_id: str
    ) -> list[EvidenceRecord]:
        await self._operations._owned_operation(actor, operation_id)
        prefix = f'["{operation_id}",'
        records = await self._facade.list_records(EVIDENCE, prefix)
        return [self._validated_owned_record(actor, operation_id, record) for record in records]

    async def get(self, actor: PluginActor, operation_id: str, evidence_id: str) -> EvidenceRecord:
        await self._operations._owned_operation(actor, operation_id)
        key = json.dumps([operation_id, evidence_id], separators=(",", ":"))
        record = await self._facade.get_record(EVIDENCE, key)
        if record is None or record.owner_user_id != actor.user_id:
            raise _error(actor, "NOT_FOUND", 404)
        evidence = self._validated_owned_record(actor, operation_id, record)
        if evidence.evidence_id != evidence_id:
            raise _error(actor, "ACCESS_DENIED", 403)
        return evidence

    @staticmethod
    def _validated_owned_record(
        actor: PluginActor, operation_id: str, record: Any
    ) -> EvidenceRecord:
        try:
            evidence = EvidenceRecord.model_validate(record.payload)
            expected_key = json.dumps([operation_id, evidence.evidence_id], separators=(",", ":"))
            if evidence.source_kind == "WEB":
                validate_result_url(evidence.locator)
        except (TypeError, ValueError):
            raise _error(actor, "EVIDENCE_SOURCE_INVALID", 403) from None
        if (
            record.owner_user_id != actor.user_id
            or record.logical_key != expected_key
            or evidence.operation_id != operation_id
            or evidence.user_id != actor.user_id
            or evidence.agent_id != actor.agent_id
            or evidence.thread_id != actor.thread_id
        ):
            raise _error(actor, "ACCESS_DENIED", 403)
        return evidence
