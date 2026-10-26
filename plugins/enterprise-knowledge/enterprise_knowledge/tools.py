"""Model-facing, generation-bound enterprise knowledge tools."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, cast

from octop.infra.agents.plugins.host_contracts import PluginHostError

from .i18n import tr
from .service import KnowledgeService

_SERVICE_PLUGIN_ID = "enterprise-knowledge"
_SERVICE_API_VERSION = 1
_SCHEMA_ID = "octop.enterprise.knowledge.v1"


def register_tools(ctx: Any) -> tuple[dict[str, Callable[..., Any]], Callable[[Any], None]]:
    """Register stable SDK callbacks and return their generation binder."""

    facade: Any | None = None

    def bind_facade(callback_facade: Any) -> None:
        nonlocal facade
        facade = callback_facade

    def service() -> KnowledgeService:
        if facade is None:
            raise PluginHostError("unavailable")
        return cast(
            KnowledgeService,
            facade.resolve_service(_SERVICE_PLUGIN_ID, _SERVICE_API_VERSION),
        )

    async def enterprise_knowledge_list() -> str:
        result = await service().list_current()
        return json.dumps(
            {
                "schema_id": _SCHEMA_ID,
                "version": 1,
                "operation_id": result.operation.operation_id,
                "knowledge_bases": [
                    {"kb_id": item.kb_id, "name": item.name} for item in result.knowledge_bases
                ],
            },
            ensure_ascii=False,
        )

    async def enterprise_knowledge_search(
        knowledge_base_ids: list[str] | None = None,
    ) -> str:
        if facade is None:
            raise PluginHostError("unavailable")
        result = await service().search_current(knowledge_base_ids)
        actor = await facade.current_execution()
        locale = actor.actor.locale
        evidence = [
            {
                "evidence_id": item.evidence_id,
                "source_kind": item.source_kind,
                "title": item.title,
                "content": item.content,
                "truncated": item.truncated,
                "retrieved_at": item.retrieved_at,
                "source_version": item.source_version,
                "source_page": item.source_page,
            }
            for item in result.evidence
        ]
        return json.dumps(
            {
                "octop_ui": {"renderer": "enterprise_knowledge_evidence", "version": 1},
                "data": {
                    "schema_id": _SCHEMA_ID,
                    "version": 1,
                    "operation_id": result.operation.operation_id,
                    "reused": True,
                    "evidence": evidence,
                },
                "text": tr("tools.search.reused", locale).format(count=len(evidence)),
            },
            ensure_ascii=False,
        )

    async def enterprise_knowledge_read(evidence_id: str, continuation: str | None = None) -> str:
        result = await service().read_current(evidence_id, continuation)
        return result.model_dump_json()

    registered: dict[str, Callable[..., Any]] = {
        "list": enterprise_knowledge_list,
        "search": enterprise_knowledge_search,
        "read": enterprise_knowledge_read,
    }
    descriptions = {
        "enterprise_knowledge_list": tr("tools.list.description"),
        "enterprise_knowledge_search": tr("tools.search.description"),
        "enterprise_knowledge_read": tr("tools.read.description"),
    }
    for function in registered.values():
        ctx.tool(function.__name__, function, description=descriptions[function.__name__])
    return registered, bind_facade
