"""MockTransport proofs for controlled primary/fallback search."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx
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

    db = SqlitePool(tmp_path / "search.db")
    run_migrations(db)
    services = build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())
    user_id = UserRepo(db).create(username="search-runner", role="user")
    AgentRepo(db).create(agent_id="agent-1", user_id=user_id, name="Search agent")
    ThreadRepo(db).insert(
        thread_id="thread-1",
        agent_id="agent-1",
        user_id=user_id,
        channel_type="dashboard",
        session_key="search-thread",
    )
    host = PluginHost(lambda: services)
    context = PluginContext(
        manifest=PluginManifest("enterprise-knowledge", "1", "EK", "tool", "main.py"),
        source_path=_PLUGIN_ROOT,
    )
    register_host_setup(context, lambda facade: facade.register_user_identity_consumer())
    facade = host.bind_context(context, "1")
    return facade, PluginActor(
        user_id=user_id,
        is_admin=False,
        agent_id="agent-1",
        thread_id="thread-1",
    )


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
    approval = await operations.approve(actor, operation.operation_id, "public query")
    return facade, actor, operation, approval


@pytest.mark.asyncio
async def test_primary_success_is_reused_by_concurrent_call(tmp_path: Path) -> None:
    from enterprise_knowledge.search import SearchService

    facade, actor, operation, approval = await _prepared(tmp_path)
    requests: list[httpx.Request] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await request.aread()
        entered.set()
        await release.wait()
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "result",
                        "url": "https://result.example/a",
                        "content": "snippet",
                    }
                ]
            },
            request=request,
        )

    service = SearchService(facade, transport=httpx.MockTransport(handler))
    first = asyncio.create_task(
        service.execute(actor, operation.operation_id, approval.approval_id, "public query")
    )
    await entered.wait()
    second = asyncio.create_task(
        service.execute(actor, operation.operation_id, approval.approval_id, "public query")
    )
    release.set()
    one, two = await asyncio.gather(first, second)
    await service.aclose()

    assert len(requests) == 1
    assert one == two
    assert one.execution.state == "COMPLETED"
    assert one.execution.attempt_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("primary", "fallback"),
    [
        ("timeout", True),
        (429, True),
        (500, True),
        (401, False),
        (403, False),
        (400, False),
        (200, False),
    ],
)
async def test_fallback_error_table(tmp_path: Path, primary: int | str, fallback: bool) -> None:
    from enterprise_knowledge.search import SearchService

    facade, actor, operation, approval = await _prepared(tmp_path)
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await request.aread()
        if len(requests) == 1:
            if primary == "timeout":
                raise httpx.ReadTimeout("timed out", request=request)
            assert isinstance(primary, int)
            body = {"results": []} if primary == 200 else None
            return httpx.Response(primary, json=body, request=request)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "web": [
                        {
                            "title": "fallback",
                            "url": "https://result.example/b",
                            "description": "snippet",
                        }
                    ]
                },
            },
            request=request,
        )

    service = SearchService(facade, transport=httpx.MockTransport(handler))
    result = await service.execute(
        actor, operation.operation_id, approval.approval_id, "public query"
    )
    await service.aclose()

    assert len(requests) == (2 if fallback else 1)
    assert result.execution.attempt_count == (2 if fallback else 1)
    if primary == 200:
        assert result.execution.state == "EMPTY"
    elif fallback:
        assert result.execution.state == "COMPLETED"
        assert result.result is not None and result.result.provider == "FIRECRAWL"
    else:
        assert result.execution.state == "FAILED"


@pytest.mark.asyncio
async def test_each_send_rechecks_config_and_change_prevents_fallback(
    tmp_path: Path,
) -> None:
    from enterprise_knowledge.search import SearchService
    from enterprise_knowledge.search_settings import SEARCH_SOURCES

    from octop.infra.agents.plugins.host_contracts import RecordWrite

    facade, actor, operation, approval = await _prepared(tmp_path)
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await request.aread()
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
        return httpx.Response(500, request=request)

    service = SearchService(facade, transport=httpx.MockTransport(handler))
    with pytest.raises(Exception) as caught:
        await service.execute(actor, operation.operation_id, approval.approval_id, "public query")
    await service.aclose()

    assert getattr(caught.value, "code", None) == "CONFIG_CONFLICT"
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_source_ten_and_operation_twenty_two_deadlines_use_one_clock(
    tmp_path: Path,
) -> None:
    from enterprise_knowledge.search import SearchService

    facade, actor, operation, approval = await _prepared(tmp_path)
    now = [0.0]
    timeouts: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        await request.aread()
        timeouts.append(request.extensions["timeout"]["read"])
        if len(timeouts) == 1:
            return httpx.Response(500, request=request)
        return httpx.Response(
            200,
            json={"success": True, "data": {"web": []}},
            request=request,
        )

    service = SearchService(
        facade,
        transport=httpx.MockTransport(handler),
        clock=lambda: now[0],
    )
    reserve_secondary = service._operations.reserve_secondary

    async def delayed_secondary(*args: Any, **kwargs: Any) -> Any:
        reserved = await reserve_secondary(*args, **kwargs)
        now[0] = 12.5
        return reserved

    service._operations.reserve_secondary = delayed_secondary
    result = await service.execute(
        actor, operation.operation_id, approval.approval_id, "public query"
    )
    await service.aclose()

    assert result.execution.state == "EMPTY"
    assert len(timeouts) == 2
    assert 0 < timeouts[0] <= 10
    assert 0 < timeouts[1] <= 9.5


@pytest.mark.asyncio
async def test_fallback_starts_without_waiting_for_primary_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import enterprise_knowledge.search as search_module

    facade, actor, operation, approval = await _prepared(tmp_path)
    requests: list[httpx.Request] = []
    release_close = asyncio.Event()
    fallback_sent = asyncio.Event()
    real_attempt = search_module.SearchHttpAttempt

    class SlowCloseAttempt(real_attempt):
        async def aclose(self) -> None:
            await release_close.wait()
            await super().aclose()

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await request.aread()
        if len(requests) == 1:
            return httpx.Response(500, request=request)
        fallback_sent.set()
        return httpx.Response(
            200,
            json={"success": True, "data": {"web": []}},
            request=request,
        )

    monkeypatch.setattr(search_module, "SearchHttpAttempt", SlowCloseAttempt)
    service = search_module.SearchService(facade, transport=httpx.MockTransport(handler))
    task = asyncio.create_task(
        service.execute(actor, operation.operation_id, approval.approval_id, "public query")
    )
    try:
        await asyncio.wait_for(fallback_sent.wait(), timeout=2)
        assert len(requests) == 2
        assert (await asyncio.wait_for(task, timeout=2)).execution.state == "EMPTY"
    finally:
        release_close.set()
        await service.aclose()


@pytest.mark.asyncio
async def test_cancellation_is_terminal_and_never_falls_back(tmp_path: Path) -> None:
    from enterprise_knowledge.search import SearchService

    facade, actor, operation, approval = await _prepared(tmp_path)
    requests: list[httpx.Request] = []
    entered = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await request.aread()
        entered.set()
        await asyncio.Event().wait()
        return httpx.Response(500, request=request)

    service = SearchService(facade, transport=httpx.MockTransport(handler))
    task = asyncio.create_task(
        service.execute(actor, operation.operation_id, approval.approval_id, "public query")
    )
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    stored = await facade.get_record("search_executions", operation.operation_id)
    assert stored is not None
    assert stored.payload["state"] == "CANCELLED"
    assert stored.payload["attempt_count"] == 1
    assert len(requests) == 1
    await service.aclose()


@pytest.mark.asyncio
async def test_operation_cancel_discards_late_success_and_never_starts_fallback(
    tmp_path: Path,
) -> None:
    from enterprise_knowledge.operations import OperationService
    from enterprise_knowledge.search import SearchService

    facade, actor, operation, approval = await _prepared(tmp_path)
    requests: list[httpx.Request] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await request.aread()
        entered.set()
        await release.wait()
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "title": "late result",
                        "url": "https://result.example/late",
                        "content": "late snippet",
                    }
                ]
            },
            request=request,
        )

    service = SearchService(facade, transport=httpx.MockTransport(handler))
    task = asyncio.create_task(
        service.execute(actor, operation.operation_id, approval.approval_id, "public query")
    )
    await entered.wait()
    current = await facade.get_record("operations", operation.operation_id)
    assert current is not None
    await OperationService(facade).transition(
        actor, operation.operation_id, current.revision, "CANCELLED"
    )
    release.set()

    with pytest.raises(Exception) as caught:
        await task
    assert getattr(caught.value, "code", None) == "CONFIG_CONFLICT"
    execution = await facade.get_record("search_executions", operation.operation_id)
    assert execution is not None
    assert execution.payload["state"] == "CANCELLED"
    assert len(requests) == 1
    await service.aclose()


@pytest.mark.asyncio
async def test_restarted_service_marks_old_reservation_unknown_without_sending(
    tmp_path: Path,
) -> None:
    from enterprise_knowledge.search import SearchService
    from enterprise_knowledge.search_operations import SearchOperationService

    facade, actor, operation, approval = await _prepared(tmp_path)
    old = SearchOperationService(facade)
    reservation = await old.reserve_primary(
        actor, operation.operation_id, approval.approval_id, "public query"
    )
    assert reservation.execution.state == "PRIMARY_RESERVED"
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        raise AssertionError("a restarted service must not replay an old reservation")

    restarted = SearchService(facade, transport=httpx.MockTransport(handler))
    result = await restarted.execute(
        actor, operation.operation_id, approval.approval_id, "public query"
    )
    await restarted.aclose()

    assert result.execution.state == "UNKNOWN"
    assert result.execution.error_code == "RESULT_UNKNOWN"
    assert requests == []


@pytest.mark.asyncio
async def test_disabled_generation_cannot_start_fallback_or_commit_late_result(
    tmp_path: Path,
) -> None:
    from enterprise_knowledge.search import SearchService

    facade, actor, operation, approval = await _prepared(tmp_path)
    requests: list[httpx.Request] = []
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        await request.aread()
        entered.set()
        await release.wait()
        return httpx.Response(500, request=request)

    service = SearchService(facade, transport=httpx.MockTransport(handler))
    task = asyncio.create_task(
        service.execute(actor, operation.operation_id, approval.approval_id, "public query")
    )
    await entered.wait()
    facade._host.deactivate("enterprise-knowledge")
    release.set()

    with pytest.raises(Exception) as caught:
        await task
    assert getattr(caught.value, "code", None) == "unavailable"
    assert len(requests) == 1
    await service.aclose()
