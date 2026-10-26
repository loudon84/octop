"""Host + SQLite proofs for durable search claims and request accounting."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import pytest

_PLUGIN_ROOT = Path(__file__).resolve().parents[3] / "plugins" / "enterprise-knowledge"
sys.path.insert(0, str(_PLUGIN_ROOT.resolve()))


def _host(tmp_path: Path) -> tuple[Any, Any]:
    from octop_harness.plugins.context import PluginContext
    from octop_harness.plugins.manifest import PluginManifest

    from octop.config import OctopConfig
    from octop.infra.agents.plugins.host import PluginHost, register_host_setup
    from octop.infra.agents.plugins.host_contracts import PluginActor
    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.pool import SqlitePool
    from octop.infra.db.repos.agents import AgentRepo
    from octop.infra.db.repos.threads import ThreadRepo
    from octop.infra.db.repos.users import UserRepo
    from octop.infra.db.services import build_shared_services
    from octop.infra.utils.paths import PathLayout

    db = SqlitePool(tmp_path / "search-operations.db")
    run_migrations(db)
    services = build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())
    user_id = UserRepo(db).create(username="search-worker", role="user")
    AgentRepo(db).create(agent_id="agent-1", user_id=user_id, name="Search agent")
    ThreadRepo(db).insert(
        thread_id="thread-1",
        agent_id="agent-1",
        user_id=user_id,
        channel_type="dashboard",
        session_key="search-operation-thread",
    )
    host = PluginHost(lambda: services)
    context = PluginContext(
        manifest=PluginManifest("enterprise-knowledge", "1", "EK", "tool", "main.py"),
        source_path=_PLUGIN_ROOT,
    )
    register_host_setup(context, lambda facade: facade.register_user_identity_consumer())
    facade = host.bind_context(context, "1")
    actor = PluginActor(
        user_id=user_id,
        is_admin=False,
        agent_id="agent-1",
        thread_id="thread-1",
    )
    return facade, actor


async def _prepared(tmp_path: Path) -> tuple[Any, Any, Any, Any]:
    from enterprise_knowledge.models import SessionCapabilities, SessionSnapshot
    from enterprise_knowledge.operations import OperationService
    from enterprise_knowledge.search_settings import SEARCH_SOURCES

    from octop.infra.agents.plugins.host_contracts import RecordWrite

    facade, actor = _host(tmp_path)
    await facade.commit(
        (
            RecordWrite(
                "bindings",
                "agent-1",
                actor.user_id,
                {"enabled": True, "web_allowed": True},
                0,
            ),
            RecordWrite(
                SEARCH_SOURCES,
                "default",
                actor.user_id,
                {
                    "primary": {
                        "provider": "SEARXNG",
                        "base_url": "https://searx.example",
                    },
                    "secondary": {
                        "provider": "FIRECRAWL",
                        "base_url": "https://firecrawl.example",
                    },
                },
                0,
            ),
        )
    )
    operations = OperationService(facade)
    operation = await operations.create(
        actor,
        SessionSnapshot(
            agent_id="agent-1",
            thread_id="thread-1",
            revision=0,
            selection_present=False,
            mode="WEB_ONLY",
            binding_revision=1,
            search_revision=1,
            search_ready=True,
            capabilities=SessionCapabilities(
                can_save_selection=True,
                can_select_knowledge=False,
                knowledge_ready=False,
                web_ready=True,
            ),
        ),
    )
    approval = await operations.approve(actor, operation.operation_id, " exact public query ")
    return facade, actor, operation, approval


@pytest.mark.asyncio
async def test_primary_claim_atomically_consumes_digest_only_approval(
    tmp_path: Path,
) -> None:
    from enterprise_knowledge.search_operations import SearchOperationService

    facade, actor, operation, approval = await _prepared(tmp_path)
    service = SearchOperationService(facade)
    reservation = await service.reserve_primary(
        actor,
        operation.operation_id,
        approval.approval_id,
        " exact public query ",
    )

    assert reservation.acquired is True
    assert reservation.execution.attempt_count == 1
    assert reservation.execution.state == "PRIMARY_RESERVED"
    stored = await facade.get_record("approvals", approval.approval_id)
    assert stored is not None and stored.payload["state"] == "CONSUMED"
    assert stored.payload["query_digest"] == reservation.execution.query_digest
    assert "public_query" not in stored.payload


@pytest.mark.asyncio
async def test_concurrent_claims_share_one_execution_and_one_consumption(
    tmp_path: Path,
) -> None:
    from enterprise_knowledge.search_operations import SearchOperationService

    facade, actor, operation, approval = await _prepared(tmp_path)
    first_service = SearchOperationService(facade)
    second_service = SearchOperationService(facade)
    reservations = await asyncio.gather(
        first_service.reserve_primary(
            actor, operation.operation_id, approval.approval_id, " exact public query "
        ),
        second_service.reserve_primary(
            actor, operation.operation_id, approval.approval_id, " exact public query "
        ),
    )

    assert sorted(item.acquired for item in reservations) == [False, True]
    assert {item.execution.claim_token for item in reservations} == {
        reservations[0].execution.claim_token
    }
    assert {item.execution.attempt_count for item in reservations} == {1}
    approval_record = await facade.get_record("approvals", approval.approval_id)
    assert approval_record is not None
    assert approval_record.revision == 2


@pytest.mark.asyncio
async def test_secondary_is_reserved_once_and_rebuilt_service_cannot_replay(
    tmp_path: Path,
) -> None:
    from enterprise_knowledge.search_operations import SearchOperationService

    facade, actor, operation, approval = await _prepared(tmp_path)
    service = SearchOperationService(facade)
    primary = await service.reserve_primary(
        actor, operation.operation_id, approval.approval_id, " exact public query "
    )
    secondary = await service.reserve_secondary(
        actor, operation.operation_id, primary.execution.claim_token
    )
    assert secondary.attempt_count == 2
    assert secondary.state == "SECONDARY_RESERVED"

    with pytest.raises(Exception) as caught:
        await service.reserve_secondary(
            actor, operation.operation_id, primary.execution.claim_token
        )
    assert getattr(caught.value, "code", None) == "CONFIG_CONFLICT"

    rebuilt = SearchOperationService(facade)
    observed = await rebuilt.reserve_primary(
        actor, operation.operation_id, approval.approval_id, " exact public query "
    )
    assert observed.acquired is False
    assert observed.execution.revision == secondary.revision
    assert observed.execution.attempt_count == 2


@pytest.mark.asyncio
async def test_config_change_and_cancelled_operation_reject_new_reservation(
    tmp_path: Path,
) -> None:
    from enterprise_knowledge.search_operations import SearchOperationService
    from enterprise_knowledge.search_settings import SEARCH_SOURCES

    from octop.infra.agents.plugins.host_contracts import RecordWrite

    facade, actor, operation, approval = await _prepared(tmp_path)
    service = SearchOperationService(facade)
    primary = await service.reserve_primary(
        actor, operation.operation_id, approval.approval_id, " exact public query "
    )
    sources = await facade.get_record(SEARCH_SOURCES, "default")
    assert sources is not None
    await facade.commit(
        (
            RecordWrite(
                SEARCH_SOURCES,
                "default",
                sources.owner_user_id,
                {**sources.payload, "changed": True},
                sources.revision,
            ),
        )
    )
    with pytest.raises(Exception) as conflict:
        await service.reserve_secondary(
            actor, operation.operation_id, primary.execution.claim_token
        )
    assert getattr(conflict.value, "code", None) == "CONFIG_CONFLICT"

    terminated = await service.terminate(actor, operation.operation_id, "UNKNOWN")
    assert terminated.state == "UNKNOWN"
    observed = await SearchOperationService(facade).reserve_primary(
        actor, operation.operation_id, approval.approval_id, " exact public query "
    )
    assert observed.acquired is False
    assert observed.execution.state == "UNKNOWN"
