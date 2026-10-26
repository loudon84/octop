"""Typed HTTP management actions; these are never exposed as model tools."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, cast

from octop.infra.agents.plugins.host_contracts import PluginActor

from .connections import ConnectionService
from .i18n import tr
from .models import (
    BindingOutput,
    BindingSaveInput,
    ConnectionCatalogInput,
    ConnectionCatalogOutput,
    ConnectionProbeInput,
    ConnectionSaveInput,
    ConnectionSaveOutput,
    EmptyInput,
    EvidenceListInput,
    EvidenceListOutput,
    GrantSaveInput,
    GrantSaveOutput,
    OperationOutput,
    OperationPrepareInput,
    OriginalInput,
    OriginalOutput,
    ProbeOutput,
    QueryApprovalOutput,
    QueryApproveInput,
    QueryRevokeInput,
    ReadInput,
    ReadPage,
    SessionOutput,
    SessionSaveInput,
    SettingsOutput,
)
from .operations import OperationService
from .probes import ProbeService
from .service import KnowledgeService
from .sessions import SessionService


@dataclass(frozen=True)
class ActionSpec:
    name: str
    input_model: type[Any]
    output_model: type[Any]
    handler: Callable[..., Any]
    summary: str
    permission: Literal["authenticated", "admin", "agent_owner"]


def register_actions(facade: Any) -> tuple[ActionSpec, ...]:
    async def settings_get(actor: PluginActor, _payload: EmptyInput) -> SettingsOutput:
        return await ConnectionService(facade).get_settings(actor)

    async def connection_save(
        actor: PluginActor, payload: ConnectionSaveInput
    ) -> ConnectionSaveOutput:
        connection = await ConnectionService(facade).save_connection(actor, payload)
        return ConnectionSaveOutput(connection=connection)

    async def connection_catalog(
        actor: PluginActor, payload: ConnectionCatalogInput
    ) -> ConnectionCatalogOutput:
        return ConnectionCatalogOutput.model_validate(
            await ProbeService(facade).catalog(actor, payload)
        )

    async def connection_probe(actor: PluginActor, payload: ConnectionProbeInput) -> ProbeOutput:
        return await ProbeService(facade).probe(actor, payload)

    async def grant_save(actor: PluginActor, payload: GrantSaveInput) -> GrantSaveOutput:
        grant = await ConnectionService(facade).save_grant(actor, payload)
        return GrantSaveOutput(grant=grant)

    async def binding_get(actor: PluginActor, _payload: EmptyInput) -> BindingOutput:
        binding = await ConnectionService(facade).get_binding(actor)
        return BindingOutput(binding=binding)

    async def binding_save(actor: PluginActor, payload: BindingSaveInput) -> BindingOutput:
        binding = await ConnectionService(facade).save_binding(actor, payload)
        return BindingOutput(binding=binding)

    async def session_get(actor: PluginActor, _payload: EmptyInput) -> SessionOutput:
        return SessionOutput(session=await SessionService(facade).get(actor))

    async def session_save(actor: PluginActor, payload: SessionSaveInput) -> SessionOutput:
        return SessionOutput(session=await SessionService(facade).save(actor, payload))

    async def evidence_read(actor: PluginActor, payload: ReadInput) -> ReadPage:
        service = cast(KnowledgeService, facade.resolve_service("enterprise-knowledge", 1))
        return await service.read(actor, payload)

    async def evidence_original(actor: PluginActor, payload: OriginalInput) -> OriginalOutput:
        service = cast(KnowledgeService, facade.resolve_service("enterprise-knowledge", 1))
        return await service.original(actor, payload)

    async def operation_prepare(
        actor: PluginActor, payload: OperationPrepareInput
    ) -> OperationOutput:
        service = cast(KnowledgeService, facade.resolve_service("enterprise-knowledge", 1))
        return await service.prepare(actor, payload)

    async def evidence_list(actor: PluginActor, payload: EvidenceListInput) -> EvidenceListOutput:
        service = cast(KnowledgeService, facade.resolve_service("enterprise-knowledge", 1))
        return await service.evidence_list(actor, payload)

    async def query_approve(actor: PluginActor, payload: QueryApproveInput) -> QueryApprovalOutput:
        approval = await OperationService(facade).approve(
            actor, payload.operation_id, payload.public_query
        )
        return QueryApprovalOutput(approval=approval)

    async def query_revoke(actor: PluginActor, payload: QueryRevokeInput) -> QueryApprovalOutput:
        approval = await OperationService(facade).revoke_for_operation(actor, payload.operation_id)
        return QueryApprovalOutput(approval=approval)

    return (
        ActionSpec(
            "query.approve",
            QueryApproveInput,
            QueryApprovalOutput,
            query_approve,
            tr("actions.query.approve"),
            "authenticated",
        ),
        ActionSpec(
            "query.revoke",
            QueryRevokeInput,
            QueryApprovalOutput,
            query_revoke,
            tr("actions.query.revoke"),
            "authenticated",
        ),
        ActionSpec(
            "operation.prepare",
            OperationPrepareInput,
            OperationOutput,
            operation_prepare,
            tr("actions.operation.prepare"),
            "authenticated",
        ),
        ActionSpec(
            "evidence.list",
            EvidenceListInput,
            EvidenceListOutput,
            evidence_list,
            tr("actions.evidence.list"),
            "authenticated",
        ),
        ActionSpec(
            "evidence.read",
            ReadInput,
            ReadPage,
            evidence_read,
            tr("actions.evidence.read"),
            "authenticated",
        ),
        ActionSpec(
            "evidence.original",
            OriginalInput,
            OriginalOutput,
            evidence_original,
            tr("actions.evidence.original"),
            "authenticated",
        ),
        ActionSpec(
            "connection.catalog",
            ConnectionCatalogInput,
            ConnectionCatalogOutput,
            connection_catalog,
            tr("actions.connection.catalog"),
            "authenticated",
        ),
        ActionSpec(
            "connection.probe",
            ConnectionProbeInput,
            ProbeOutput,
            connection_probe,
            tr("actions.connection.probe"),
            "admin",
        ),
        ActionSpec(
            "settings.get",
            EmptyInput,
            SettingsOutput,
            settings_get,
            tr("actions.settings.get"),
            "authenticated",
        ),
        ActionSpec(
            "connection.save",
            ConnectionSaveInput,
            ConnectionSaveOutput,
            connection_save,
            tr("actions.connection.save"),
            "admin",
        ),
        ActionSpec(
            "grant.save",
            GrantSaveInput,
            GrantSaveOutput,
            grant_save,
            tr("actions.grant.save"),
            "admin",
        ),
        ActionSpec(
            "binding.get",
            EmptyInput,
            BindingOutput,
            binding_get,
            tr("actions.binding.get"),
            "agent_owner",
        ),
        ActionSpec(
            "binding.save",
            BindingSaveInput,
            BindingOutput,
            binding_save,
            tr("actions.binding.save"),
            "agent_owner",
        ),
        ActionSpec(
            "session.get",
            EmptyInput,
            SessionOutput,
            session_get,
            tr("actions.session.get"),
            "authenticated",
        ),
        ActionSpec(
            "session.save",
            SessionSaveInput,
            SessionOutput,
            session_save,
            tr("actions.session.save"),
            "authenticated",
        ),
    )
