"""Local authorization policy for employee knowledge scopes."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from pydantic import ValidationError

from octop.infra.agents.plugins.host_contracts import HostRecord, PluginActionError, PluginActor

from .connections import BINDINGS, CONNECTIONS, GRANTS, ConnectionService
from .i18n import error_message
from .models import ScopeDecision, SessionSourceRevisions
from .sessions import SessionService

_KB_ID_RE = re.compile(r"^[^/\\\x00-\x1f\x7f]+$")


class KnowledgeDirectoryProvider(Protocol):
    """Server-injected source of the current credential-visible knowledge directory."""

    async def visible_kb_ids(self, connection_id: str, actor: PluginActor) -> list[str]: ...

    async def metadata_for(
        self, connection_id: str, kb_ids: list[str], actor: PluginActor
    ) -> list[Mapping[str, Any]]: ...


def _fail(actor: PluginActor, code: str, status: int = 403) -> PluginActionError:
    return PluginActionError(code, error_message(code, actor.locale), status=status)


def _ids(values: Sequence[str]) -> list[str]:
    if isinstance(values, (str, bytes)):
        raise ValueError("IDs must be a sequence of strings")
    result: set[str] = set()
    for value in values:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or not _KB_ID_RE.fullmatch(value)
        ):
            raise ValueError("invalid knowledge base identifier")
        result.add(value)
    return sorted(result)


def decide_scope(
    *,
    credential_visible_ids: Sequence[str],
    enterprise_allowed_ids: Sequence[str],
    employee_allowed_ids: Sequence[str],
    expert_allowed_ids: Sequence[str],
    session_selected_ids: Sequence[str] | None = None,
) -> list[str]:
    """Intersect the five independently sourced authorization layers."""
    layers = [
        set(_ids(credential_visible_ids)),
        set(_ids(enterprise_allowed_ids)),
        set(_ids(employee_allowed_ids)),
        set(_ids(expert_allowed_ids)),
    ]
    if session_selected_ids is not None:
        layers.append(set(_ids(session_selected_ids)))
    if not layers or any(not layer for layer in layers):
        return []
    return sorted(set.intersection(*layers))


def apply_requested_ids(
    actor: PluginActor, scope: Sequence[str], requested_ids: Sequence[str] | None
) -> list[str]:
    """Apply optional tool narrowing; explicit empty or any out-of-scope ID denies all."""
    effective = _ids(scope)
    if requested_ids is None:
        return effective
    requested = _ids(requested_ids)
    if not requested or not set(requested).issubset(effective):
        raise _fail(actor, "SCOPE_INVALID")
    return requested


class ScopePolicy:
    """Read current Host authorization records and produce a deterministic scope."""

    def __init__(self, facade: Any, provider: KnowledgeDirectoryProvider | None = None) -> None:
        self._facade = facade
        self._provider = provider

    async def query_scope(
        self, actor: PluginActor, requested_ids: Sequence[str] | None = None
    ) -> ScopeDecision:
        binding, connection, grant, session = await self._current(actor)
        if session.mode not in {"KB_ONLY", "HYBRID"}:
            raise _fail(actor, "MODE_NOT_ALLOWED", 409)
        locally_allowed = self._local_scope(binding.payload, connection, grant, actor)
        local_query_scope = sorted(locally_allowed & set(session.selected_kb_ids))
        if not local_query_scope:
            raise _fail(actor, "SCOPE_INVALID")
        if requested_ids is not None:
            try:
                requested_ids = apply_requested_ids(actor, local_query_scope, requested_ids)
            except (PluginActionError, TypeError, ValueError):
                raise _fail(actor, "SCOPE_INVALID") from None
        provider = self._require_provider(actor)
        visible = await provider.visible_kb_ids(connection.logical_key, actor)
        try:
            effective = decide_scope(
                credential_visible_ids=visible,
                enterprise_allowed_ids=connection.payload.get("allowed_kb_ids", []),
                employee_allowed_ids=grant.payload.get("user_allowed_kb_ids", []),
                expert_allowed_ids=binding.payload.get("expert_allowed_kb_ids", []),
                session_selected_ids=session.selected_kb_ids,
            )
        except (TypeError, ValueError):
            raise _fail(actor, "SCOPE_INVALID") from None
        try:
            effective = apply_requested_ids(actor, effective, requested_ids)
        except PluginActionError:
            raise _fail(actor, "SCOPE_INVALID") from None
        if not effective:
            raise _fail(actor, "SCOPE_INVALID")
        return ScopeDecision(
            effective_kb_ids=effective,
            source_revisions=SessionSourceRevisions(
                connection_id=session.connection_id,
                config_revision=session.config_revision,
                binding_revision=session.binding_revision,
                grant_revision=session.grant_revision,
            ),
            session_revision=session.revision,
        )

    async def catalog(self, actor: PluginActor) -> list[dict[str, Any]]:
        """Return metadata within the first four layers; session selection is not applied."""
        binding, connection, grant, session = await self._current(actor)
        if session.mode not in {"KB_ONLY", "HYBRID"}:
            raise _fail(actor, "MODE_NOT_ALLOWED", 409)
        local_scope = self._local_scope(binding.payload, connection, grant, actor)
        if not local_scope:
            raise _fail(actor, "SCOPE_INVALID")
        provider = self._require_provider(actor)
        visible = await provider.visible_kb_ids(connection.logical_key, actor)
        try:
            allowed = decide_scope(
                credential_visible_ids=visible,
                enterprise_allowed_ids=connection.payload.get("allowed_kb_ids", []),
                employee_allowed_ids=grant.payload.get("user_allowed_kb_ids", []),
                expert_allowed_ids=binding.payload.get("expert_allowed_kb_ids", []),
            )
        except (TypeError, ValueError):
            raise _fail(actor, "SCOPE_INVALID") from None
        if not allowed:
            raise _fail(actor, "SCOPE_INVALID")
        rows = await provider.metadata_for(connection.logical_key, allowed, actor)
        allowed_ids = set(allowed)
        return [dict(row) for row in rows if row.get("kb_id") in allowed_ids]

    async def _current(self, actor: PluginActor) -> tuple[HostRecord, HostRecord, HostRecord, Any]:
        if not actor.agent_id or not actor.thread_id:
            raise _fail(actor, "AGENT_REQUIRED", 400)
        binding = await self._facade.get_record(BINDINGS, actor.agent_id)
        if binding is None or not binding.payload.get("enabled"):
            raise _fail(actor, "CONNECTION_DISABLED", 409)
        connection_id = binding.payload.get("connection_ref")
        if not isinstance(connection_id, str) or not connection_id:
            raise _fail(actor, "CONNECTION_DISABLED", 409)
        connection = await self._facade.get_record(CONNECTIONS, connection_id)
        if connection is None:
            raise _fail(actor, "NOT_FOUND", 404)
        await ConnectionService(self._facade).assert_connection_available(connection_id, actor)
        if not connection.payload.get("enabled"):
            raise _fail(actor, "CONNECTION_DISABLED", 409)
        grant = await self._facade.get_record(GRANTS, f"{connection_id}:{actor.user_id}")
        if grant is None:
            raise _fail(actor, "SCOPE_INVALID")
        try:
            session = await SessionService(self._facade).get(actor)
        except ValidationError:
            raise _fail(actor, "SCOPE_INVALID") from None
        if (
            session.connection_id != connection_id
            or session.config_revision != connection.revision
            or session.binding_revision != binding.revision
            or session.grant_revision != grant.revision
        ):
            raise _fail(actor, "CONFIG_CONFLICT", 409)
        return binding, connection, grant, session

    @staticmethod
    def _local_scope(
        binding: Mapping[str, Any], connection: HostRecord, grant: HostRecord, actor: PluginActor
    ) -> set[str]:
        try:
            layers = (
                _ids(connection.payload.get("allowed_kb_ids", [])),
                _ids(grant.payload.get("user_allowed_kb_ids", [])),
                _ids(binding.get("expert_allowed_kb_ids", [])),
            )
        except (TypeError, ValueError):
            raise _fail(actor, "SCOPE_INVALID") from None
        return set.intersection(*(set(layer) for layer in layers)) if all(layers) else set()

    def _require_provider(self, actor: PluginActor) -> KnowledgeDirectoryProvider:
        if self._provider is None:
            raise _fail(actor, "PLUGIN_NOT_READY", 503)
        return self._provider
