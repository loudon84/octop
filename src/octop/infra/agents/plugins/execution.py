"""Trusted, in-process identity carried across one agent execution scope."""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Literal

from octop.infra.agents.plugins.host_contracts import PluginActor, PluginHostError

ExecutionKind = Literal["turn", "resume", "continuation"]
_TICKET_META_KEY = "_octop_plugin_execution_ticket"
_active_ticket: ContextVar[ExecutionTicket | None] = ContextVar(
    "octop_plugin_execution_ticket", default=None
)
_active_peer_dispatch: ContextVar[_PeerDispatchScope | None] = ContextVar(
    "octop_peer_dispatch_permit", default=None
)


@dataclass(frozen=True, eq=False)
class ExecutionTicket:
    _host_key: object
    actor: PluginActor
    agent_id: str
    thread_id: str
    plugin_context: dict[str, dict[str, Any]]
    execution_kind: ExecutionKind


@dataclass(frozen=True, eq=False)
class PeerDispatchPermit:
    _host_key: object
    from_agent_id: str
    to_agent_id: str
    source_thread_id: str | None


@dataclass
class _PeerDispatchScope:
    permit: PeerDispatchPermit
    consumed: bool = False


def issue_peer_dispatch_permit(
    host: Any,
    *,
    from_agent_id: str,
    to_agent_id: str,
    source_thread_id: str | None,
) -> PeerDispatchPermit:
    return PeerDispatchPermit(
        host._execution_ticket_key,
        from_agent_id,
        to_agent_id,
        source_thread_id,
    )


@contextmanager
def peer_dispatch_scope(permit: PeerDispatchPermit) -> Iterator[None]:
    token = _active_peer_dispatch.set(_PeerDispatchScope(permit))
    try:
        yield
    finally:
        _active_peer_dispatch.reset(token)


def consume_peer_dispatch_permit(
    host: Any,
    *,
    from_agent_id: str,
    to_agent_id: str,
    source_thread_id: str | None,
) -> bool:
    scope = _active_peer_dispatch.get()
    if scope is None:
        return False
    permit = scope.permit
    if (
        permit._host_key is not host._execution_ticket_key
        or permit.from_agent_id != from_agent_id
        or permit.to_agent_id != to_agent_id
        or permit.source_thread_id != source_thread_id
        or scope.consumed
    ):
        return False
    scope.consumed = True
    return True


def issue_execution_ticket(
    host: Any,
    actor: PluginActor,
    *,
    plugin_context: dict[str, dict[str, Any]] | None = None,
    execution_kind: ExecutionKind = "turn",
) -> ExecutionTicket:
    if actor.agent_id is None or actor.thread_id is None:
        raise PluginHostError("invalid")
    copied = copy.deepcopy(plugin_context or {})
    if not isinstance(copied, dict) or any(
        not isinstance(plugin_id, str) or not isinstance(value, dict)
        for plugin_id, value in copied.items()
    ):
        raise PluginHostError("invalid")
    try:
        copied = json.loads(json.dumps(copied, allow_nan=False))
    except (TypeError, ValueError):
        raise PluginHostError("invalid") from None
    return ExecutionTicket(
        host._execution_ticket_key,
        actor,
        actor.agent_id,
        actor.thread_id,
        copied,
        execution_kind,
    )


def ticket_metadata(ticket: ExecutionTicket) -> dict[str, Any]:
    return {_TICKET_META_KEY: ticket}


def ticket_from_metadata(metadata: object) -> ExecutionTicket | None:
    if not isinstance(metadata, dict):
        return None
    ticket = metadata.get(_TICKET_META_KEY)
    return ticket if isinstance(ticket, ExecutionTicket) else None


def activate_ticket(host: Any, ticket: ExecutionTicket | None) -> Token[ExecutionTicket | None]:
    if ticket is not None and ticket._host_key is not host._execution_ticket_key:
        raise PluginHostError("denied")
    return _active_ticket.set(ticket)


def reset_ticket(token: Token[ExecutionTicket | None]) -> None:
    _active_ticket.reset(token)


def current_ticket() -> ExecutionTicket | None:
    return _active_ticket.get()


@contextmanager
def without_execution_ticket() -> Iterator[None]:
    """Prevent a child/team operation from inheriting its caller's identity."""
    token = _active_ticket.set(None)
    try:
        yield
    finally:
        _active_ticket.reset(token)


async def execution_scope(
    host: Any,
    ticket: ExecutionTicket | None,
    *,
    agent_id: str,
    thread_id: str,
    execution_kind: ExecutionKind | None = None,
) -> Any:
    await host.authorize_execution(
        ticket,
        agent_id=agent_id,
        thread_id=thread_id,
        execution_kind=execution_kind,
    )
    token = activate_ticket(host, ticket)
    return token


def copy_plugin_context(ticket: ExecutionTicket, plugin_id: str) -> dict[str, Any]:
    return copy.deepcopy(ticket.plugin_context.get(plugin_id, {}))


def ticket_with_kind(ticket: ExecutionTicket, execution_kind: ExecutionKind) -> ExecutionTicket:
    return ExecutionTicket(
        ticket._host_key,
        ticket.actor,
        ticket.agent_id,
        ticket.thread_id,
        copy.deepcopy(ticket.plugin_context),
        execution_kind,
    )
