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

    async def _validated_source_records(
        self,
        actor: PluginActor,
        operation_id: str,
        operation: Any,
        items: list[dict[str, Any]],
    ) -> list[EvidenceRecord]:
        if operation.payload.get("status") != "RETRIEVING" or not items:
            raise _error(actor, "EVIDENCE_SOURCE_INVALID", 400)
        payloads: list[EvidenceRecord] = []
        for item in items:
            if any(
                key in item
                for key in ("evidence_id", "operation_id", "user_id", "agent_id", "thread_id")
            ):
                raise _error(actor, "EVIDENCE_SOURCE_INVALID", 400)
            evidence_data = dict(item)
            evidence_data.setdefault("retrieved_at", datetime.now(UTC).isoformat())
            payload = EvidenceRecord(
                evidence_id=str(uuid4()),
                operation_id=operation_id,
                user_id=actor.user_id,
                agent_id=actor.agent_id,
                thread_id=actor.thread_id,
                **evidence_data,
            )
            if payload.connection_id != operation.payload.get("connection_id"):
                raise _error(actor, "EVIDENCE_SOURCE_INVALID", 400)
            payloads.append(payload)
        return payloads

    async def list_for_operation(
        self, actor: PluginActor, operation_id: str
    ) -> list[EvidenceRecord]:
        await self._operations._owned_operation(actor, operation_id)
        prefix = f'["{operation_id}",'
        records = await self._facade.list_records(EVIDENCE, prefix)
        return [EvidenceRecord.model_validate(record.payload) for record in records]

    async def get(self, actor: PluginActor, operation_id: str, evidence_id: str) -> EvidenceRecord:
        await self._operations._owned_operation(actor, operation_id)
        key = json.dumps([operation_id, evidence_id], separators=(",", ":"))
        record = await self._facade.get_record(EVIDENCE, key)
        if record is None or record.owner_user_id != actor.user_id:
            raise _error(actor, "NOT_FOUND", 404)
        return EvidenceRecord.model_validate(record.payload)
