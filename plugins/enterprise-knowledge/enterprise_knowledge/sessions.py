"""Actor-scoped knowledge session snapshots using Host records and CAS."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, TypedDict

from octop.infra.agents.plugins.host_contracts import (
    HostRecord,
    PluginActionError,
    PluginActor,
    PluginHostError,
    RecordCheck,
    RecordWrite,
)

from .connections import BINDINGS, CONNECTION_INTENTS, CONNECTIONS, GRANTS, ConnectionService
from .i18n import error_message
from .models import SCHEMA_ID, SessionCapabilities, SessionSaveInput, SessionSnapshot
from .search_settings import CONFIG_KEY, SEARCH_INTENTS, SEARCH_SOURCES

SESSIONS = "sessions"


class SourceRevisions(TypedDict):
    connection_id: str | None
    binding_revision: int
    config_revision: int
    grant_revision: int


class SearchState(TypedDict):
    revision: int
    ready: bool
    intent_revision: int


def _session_key(user_id: int, agent_id: str, thread_id: str) -> str:
    return json.dumps([user_id, agent_id, thread_id], ensure_ascii=False, separators=(",", ":"))


def _error(actor: PluginActor, code: str, status: int) -> PluginActionError:
    return PluginActionError(code, error_message(code, actor.locale), status=status)


def _source_revisions(
    binding: HostRecord | None,
    connection: HostRecord | None,
    grant: HostRecord | None,
    connection_id: str | None,
) -> SourceRevisions:
    return {
        "connection_id": connection_id,
        "binding_revision": binding.revision if binding is not None else 0,
        "config_revision": connection.revision if connection is not None else 0,
        "grant_revision": grant.revision if grant is not None else 0,
    }


class SessionService:
    def __init__(self, facade: Any) -> None:
        self._facade = facade

    async def get(self, actor: PluginActor) -> SessionSnapshot:
        agent_id, thread_id = self._scope(actor)
        binding = await self._facade.get_record(BINDINGS, agent_id)
        binding_payload = binding.payload if binding is not None else {}
        search = await self._search_state(binding_payload)
        connection_id = (
            binding_payload.get("connection_ref") if binding_payload.get("enabled") else None
        )
        connection = (
            await self._facade.get_record(CONNECTIONS, connection_id)
            if connection_id is not None
            else None
        )
        grant = (
            await self._facade.get_record(GRANTS, f"{connection_id}:{actor.user_id}")
            if connection is not None
            else None
        )
        allowed = self._authorized_selection(binding_payload, connection, grant)
        capabilities = await self._capabilities(actor, connection, allowed, search["ready"])
        revisions = _source_revisions(binding, connection, grant, connection_id)
        key = _session_key(actor.user_id, agent_id, thread_id)
        saved = await self._facade.get_record(SESSIONS, key)
        if saved is None:
            selected = [
                item for item in binding_payload.get("default_kb_ids", []) if item in allowed
            ]
            capabilities = capabilities.model_copy(
                update={
                    "knowledge_ready": bool(selected)
                    and capabilities.knowledge_ready
                    and capabilities.can_select_knowledge
                }
            )
            return SessionSnapshot(
                agent_id=agent_id,
                thread_id=thread_id,
                revision=0,
                selection_present=False,
                selected_kb_ids=selected,
                mode="KB_ONLY",
                capabilities=capabilities,
                search_revision=search["revision"],
                search_ready=search["ready"],
                **revisions,
            )
        payload = saved.payload
        saved_revisions = {
            name: payload.get(name, None if name == "connection_id" else 0) for name in revisions
        }
        knowledge_changed = saved_revisions != revisions
        selected = list(payload.get("selected_kb_ids", []))
        scope_invalid = any(item not in allowed for item in selected)
        mode = payload.get("mode", "KB_ONLY")
        search_changed = (
            mode in {"WEB_ONLY", "HYBRID"}
            and payload.get("search_revision", 0) != search["revision"]
        )
        mode_unready = (
            mode == "WEB_ONLY"
            and not search["ready"]
            or mode == "HYBRID"
            and (not search["ready"] or not capabilities.knowledge_ready or not selected)
        )
        invalid = (
            (knowledge_changed and mode != "WEB_ONLY")
            or (scope_invalid and mode != "WEB_ONLY")
            or search_changed
            or mode_unready
        )
        reason = (
            "SOURCE_CHANGED"
            if knowledge_changed or search_changed
            else "SCOPE_REVOKED"
            if scope_invalid
            else "SEARCH_NOT_READY"
            if mode_unready
            else None
        )
        capabilities = capabilities.model_copy(
            update={
                "knowledge_ready": bool(selected)
                and capabilities.knowledge_ready
                and capabilities.can_select_knowledge
                and not invalid
            }
        )
        return SessionSnapshot(
            agent_id=agent_id,
            thread_id=thread_id,
            revision=saved.revision,
            selection_present=True,
            selected_kb_ids=selected,
            mode=mode,
            **revisions,
            search_revision=search["revision"],
            search_ready=search["ready"],
            invalid=invalid,
            invalid_reason=reason,
            capabilities=capabilities,
        )

    async def save(self, actor: PluginActor, request: SessionSaveInput) -> SessionSnapshot:
        agent_id, thread_id = self._scope(actor)

        binding = await self._facade.get_record(BINDINGS, agent_id)
        binding_payload: Mapping[str, Any] = binding.payload if binding is not None else {}
        search = await self._search_state(binding_payload)
        connection_id = (
            binding_payload.get("connection_ref") if binding_payload.get("enabled") else None
        )
        connection = (
            await self._facade.get_record(CONNECTIONS, connection_id)
            if connection_id is not None
            else None
        )
        grant = (
            await self._facade.get_record(GRANTS, f"{connection_id}:{actor.user_id}")
            if connection is not None
            else None
        )
        revisions = _source_revisions(binding, connection, grant, connection_id)
        requested_sources = request.source_revisions.model_dump()
        requested_knowledge = {
            name: requested_sources[name]
            for name in ("connection_id", "binding_revision", "config_revision", "grant_revision")
        }
        if requested_knowledge != revisions:
            raise _error(actor, "CONFIG_CONFLICT", 409)
        if (
            request.mode in {"WEB_ONLY", "HYBRID"}
            and request.source_revisions.search_revision != search["revision"]
        ):
            raise _error(actor, "CONFIG_CONFLICT", 409)
        allowed = self._authorized_selection(binding_payload, connection, grant)
        if not set(request.selected_kb_ids).issubset(allowed):
            raise _error(actor, "SCOPE_INVALID", 400)
        capabilities = await self._capabilities(actor, connection, allowed, search["ready"])
        if request.mode == "WEB_ONLY" and not search["ready"]:
            raise _error(actor, "MODE_NOT_ALLOWED", 409)
        if request.mode == "HYBRID" and (
            not search["ready"] or not request.selected_kb_ids or not capabilities.knowledge_ready
        ):
            raise _error(actor, "MODE_NOT_ALLOWED", 409)
        key = _session_key(actor.user_id, agent_id, thread_id)
        old = await self._facade.get_record(SESSIONS, key)
        current_revision = old.revision if old is not None else 0
        if current_revision != request.expected_revision:
            raise _error(actor, "CONFIG_CONFLICT", 409)

        intent_revision: int | None = None
        if connection_id is not None and request.mode != "WEB_ONLY":
            intent_revision = await ConnectionService(self._facade).assert_connection_available(
                connection_id, actor
            )
        source = request.source_revisions
        checks = [RecordCheck(BINDINGS, agent_id, source.binding_revision)]
        if connection_id is not None and request.mode != "WEB_ONLY":
            checks.extend(
                (
                    RecordCheck(CONNECTIONS, connection_id, source.config_revision),
                    RecordCheck(GRANTS, f"{connection_id}:{actor.user_id}", source.grant_revision),
                    RecordCheck(CONNECTION_INTENTS, connection_id, intent_revision or 0),
                )
            )
        if request.mode in {"WEB_ONLY", "HYBRID"}:
            checks.extend(
                (
                    RecordCheck(SEARCH_SOURCES, CONFIG_KEY, search["revision"]),
                    RecordCheck(SEARCH_INTENTS, CONFIG_KEY, search["intent_revision"]),
                )
            )
        payload = {
            "schema_id": SCHEMA_ID,
            "version": 1,
            "user_id": actor.user_id,
            "agent_id": agent_id,
            "thread_id": thread_id,
            "selected_kb_ids": list(request.selected_kb_ids),
            "mode": request.mode,
            "connection_id": source.connection_id,
            "search_revision": search["revision"],
            **revisions,
        }
        try:
            saved = await self._facade.commit(
                (RecordWrite(SESSIONS, key, actor.user_id, payload, request.expected_revision),),
                checks=tuple(checks),
            )
        except PluginHostError as exc:
            if exc.code == "conflict":
                raise _error(actor, "CONFIG_CONFLICT", 409) from None
            raise
        capabilities = capabilities.model_copy(
            update={
                "knowledge_ready": bool(request.selected_kb_ids) and capabilities.knowledge_ready
            }
        )
        return SessionSnapshot(
            agent_id=agent_id,
            thread_id=thread_id,
            revision=saved[0].revision,
            selection_present=True,
            selected_kb_ids=list(request.selected_kb_ids),
            mode=request.mode,
            capabilities=capabilities,
            search_revision=search["revision"],
            search_ready=search["ready"],
            **revisions,
        )

    async def _capabilities(
        self, actor: PluginActor, connection: Any, allowed: set[str], web_ready: bool
    ) -> SessionCapabilities:
        isolated = False
        connection_id = connection.logical_key if connection is not None else None
        if connection_id is not None:
            try:
                await ConnectionService(self._facade).assert_connection_available(
                    connection_id, actor
                )
            except PluginActionError as exc:
                if exc.code != "RECOVERY_BLOCKED":
                    raise
                isolated = True
        ready = False
        if (
            connection is not None
            and connection.payload.get("enabled")
            and allowed
            and not isolated
        ):
            # probes imports policy -> sessions, so this must remain lazy to break that cycle.
            from .probes import PROBES, valid_probe_record

            probe_rows = [
                await self._facade.get_record(PROBES, f"{connection_id}:{component}")
                for component in ("LIST", "SEARCH", "READ")
            ]
            current = {
                component
                for component, row in zip(("LIST", "SEARCH", "READ"), probe_rows, strict=True)
                if row is not None
                and valid_probe_record(
                    row.payload, connection.logical_key, component, connection.revision
                )
                and row.payload.get("status") == "SUCCESS"
            }
            ready = current.issuperset({"LIST", "SEARCH", "READ"})
        return SessionCapabilities(
            can_save_selection=not isolated,
            can_select_knowledge=bool(allowed) and not isolated,
            knowledge_ready=ready,
            web_ready=web_ready,
        )

    async def _search_state(self, binding: Mapping[str, Any]) -> SearchState:
        sources = await self._facade.get_record(SEARCH_SOURCES, CONFIG_KEY)
        intent = await self._facade.get_record(SEARCH_INTENTS, CONFIG_KEY)
        blocked = intent is not None and intent.payload.get("state") in {"PREPARING", "BLOCKED"}
        payload = sources.payload if sources is not None else {}
        primary = payload.get("primary")
        secondary = payload.get("secondary")
        ready = (
            binding.get("enabled") is True
            and binding.get("web_allowed") is True
            and isinstance(primary, Mapping)
            and isinstance(primary.get("base_url"), str)
            and isinstance(secondary, Mapping)
            and isinstance(secondary.get("base_url"), str)
            and not blocked
        )
        return {
            "revision": sources.revision if sources is not None else 0,
            "ready": ready,
            "intent_revision": intent.revision if intent is not None else 0,
        }

    @staticmethod
    def _scope(actor: PluginActor) -> tuple[str, str]:
        if not actor.agent_id or not actor.thread_id:
            raise _error(actor, "AGENT_REQUIRED", 400)
        return actor.agent_id, actor.thread_id

    @staticmethod
    def _authorized_selection(
        binding: Mapping[str, Any],
        connection: HostRecord | None,
        grant: HostRecord | None,
    ) -> set[str]:
        if connection is None or grant is None or not connection.payload.get("enabled"):
            return set()
        return (
            set(binding.get("expert_allowed_kb_ids", []))
            & set(connection.payload.get("allowed_kb_ids", []))
            & set(grant.payload.get("user_allowed_kb_ids", []))
        )
