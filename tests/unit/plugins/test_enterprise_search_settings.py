"""Host + SQLite + MockTransport proofs for search.get/save/test/recover."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
from pydantic import SecretStr, ValidationError

_PLUGIN_ROOT = Path(__file__).resolve().parents[3] / "plugins" / "enterprise-knowledge"
sys.path.insert(0, str(_PLUGIN_ROOT.resolve()))


def _host(tmp_path: Path, username: str = "search-admin") -> tuple[Any, int, Any]:
    from octop_harness.plugins.context import PluginContext
    from octop_harness.plugins.manifest import PluginManifest

    from octop.config import OctopConfig
    from octop.infra.agents.plugins.host import PluginHost, register_host_setup
    from octop.infra.db.migrate import run_migrations
    from octop.infra.db.pool import SqlitePool
    from octop.infra.db.repos.users import UserRepo
    from octop.infra.db.services import build_shared_services
    from octop.infra.utils.paths import PathLayout

    db = SqlitePool(tmp_path / "search-settings.db")
    run_migrations(db)
    services = build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())
    actor_id = UserRepo(db).create(username=username, role="admin")
    host = PluginHost(lambda: services)
    context = PluginContext(
        manifest=PluginManifest("enterprise-knowledge", "1", "EK", "tool", "main.py"),
        source_path=_PLUGIN_ROOT,
    )
    register_host_setup(context, lambda facade: facade.register_user_identity_consumer())
    facade = host.bind_context(context, "1")
    return db, actor_id, facade


def _employee(db: Any, username: str = "search-employee") -> int:
    from octop.infra.db.repos.users import UserRepo

    return UserRepo(db).create(username=username, role="user")


def _save_input(
    *,
    revision: int = 0,
    primary_url: str = "https://searx.example/",
    secondary_url: str = "https://firecrawl.example/",
    secondary_op: str = "KEEP",
    secondary_credential: str | None = None,
    primary_op: str = "KEEP",
    primary_credential: str | None = None,
) -> Any:
    from enterprise_knowledge.search_settings import SearchSaveInput, SearchSourceSavePart

    primary_kwargs: dict[str, Any] = {
        "base_url": primary_url,
        "credential_op": primary_op,
    }
    if primary_credential is not None:
        primary_kwargs["credential"] = SecretStr(primary_credential)
    secondary_kwargs: dict[str, Any] = {
        "base_url": secondary_url,
        "credential_op": secondary_op,
    }
    if secondary_credential is not None:
        secondary_kwargs["credential"] = SecretStr(secondary_credential)
    return SearchSaveInput(
        expected_revision=revision,
        primary=SearchSourceSavePart(**primary_kwargs),
        secondary=SearchSourceSavePart(**secondary_kwargs),
    )


@pytest.mark.asyncio
async def test_get_empty_defaults(tmp_path: Path) -> None:
    from enterprise_knowledge.search_settings import SearchSettingsService

    from octop.infra.agents.plugins.host_contracts import PluginActor

    _, actor_id, facade = _host(tmp_path)
    settings = await SearchSettingsService(facade).get_settings(PluginActor(actor_id, True))
    assert settings.revision == 0
    assert settings.recovery_blocked is False
    assert settings.primary.provider == "SEARXNG"
    assert settings.secondary.provider == "FIRECRAWL"
    assert settings.web_search_ready is False


@pytest.mark.asyncio
async def test_save_keep_and_cas_conflict(tmp_path: Path) -> None:
    from enterprise_knowledge.search_settings import SearchSettingsService

    from octop.infra.agents.plugins.host_contracts import PluginActionError, PluginActor

    _, actor_id, facade = _host(tmp_path)
    service = SearchSettingsService(facade)
    actor = PluginActor(actor_id, True)
    saved = await service.save(actor, _save_input())
    assert saved.revision == 1
    assert saved.primary.base_url == "https://searx.example"
    assert saved.secondary.base_url == "https://firecrawl.example"
    assert saved.web_search_ready is True
    assert saved.secondary.credential_configured is False

    with pytest.raises(PluginActionError) as caught:
        await service.save(actor, _save_input(revision=0))
    assert caught.value.code == "CONFIG_CONFLICT"


@pytest.mark.asyncio
async def test_searxng_replace_credential_rejected(tmp_path: Path) -> None:
    from enterprise_knowledge.search_settings import (
        SearchSaveInput,
        SearchSettingsService,
        SearchSourceSavePart,
    )

    from octop.infra.agents.plugins.host_contracts import PluginActionError, PluginActor

    _, actor_id, facade = _host(tmp_path)

    with pytest.raises(ValidationError):
        SearchSourceSavePart(
            base_url="https://searx.example/",
            credential_op="KEEP",
            credential=SecretStr("x"),
        )

    service = SearchSettingsService(facade)
    actor = PluginActor(actor_id, True)
    crafted = SearchSaveInput.model_construct(
        expected_revision=0,
        primary=SearchSourceSavePart.model_construct(
            base_url="https://searx.example/",
            ca_bundle=None,
            credential_op="REPLACE",
            credential=SecretStr("nope"),
            reported_version=None,
        ),
        secondary=SearchSourceSavePart(
            base_url="https://firecrawl.example/",
            credential_op="KEEP",
        ),
    )
    with pytest.raises(PluginActionError) as caught:
        await service.save(actor, crafted)
    assert caught.value.code == "UNSUPPORTED_CONFIG"


@pytest.mark.asyncio
async def test_firecrawl_replace_clear_and_non_admin(tmp_path: Path) -> None:
    from enterprise_knowledge.search_settings import SearchSettingsService

    from octop.infra.agents.plugins.host_contracts import PluginActionError, PluginActor

    db, actor_id, facade = _host(tmp_path)
    service = SearchSettingsService(facade)
    admin = PluginActor(actor_id, True)
    saved = await service.save(
        admin,
        _save_input(secondary_op="REPLACE", secondary_credential="fc-secret"),
    )
    assert saved.secondary.credential_configured is True

    cleared = await service.save(
        admin,
        _save_input(revision=saved.revision, secondary_op="CLEAR"),
    )
    assert cleared.secondary.credential_configured is False

    employee_id = _employee(db)
    with pytest.raises(PluginActionError) as caught:
        await service.save(
            PluginActor(employee_id, False),
            _save_input(revision=cleared.revision),
        )
    assert (
        caught.value.code in {"ACCESS_DENIED", "CONFIG_INVALID", "FORBIDDEN"}
        or caught.value.status == 403
    )


@pytest.mark.asyncio
async def test_test_primary_mock_and_no_fallback(tmp_path: Path) -> None:
    from enterprise_knowledge.search_settings import SearchSettingsService, SearchTestInput

    from octop.infra.agents.plugins.host_contracts import PluginActor

    _, actor_id, facade = _host(tmp_path)
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = await request.aread()
        assert request.url.path.endswith("/search")
        form = parse_qs(body.decode("ascii"))
        assert form["q"] == [" exact query "]
        return httpx.Response(
            200,
            json={"results": [{"title": "T", "url": "https://r.example/a", "content": "c"}]},
            request=request,
        )

    service = SearchSettingsService(facade, transport=httpx.MockTransport(handler))
    admin = PluginActor(actor_id, True)
    saved = await service.save(admin, _save_input())
    result = await service.test(
        admin,
        SearchTestInput(
            source_role="PRIMARY",
            expected_revision=saved.revision,
            public_query=" exact query ",
        ),
    )
    assert result.diagnostic.status == "SUCCESS"
    assert result.diagnostic.hit_count == 1
    assert result.diagnostic.code is None
    assert len(requests) == 1
    assert " exact query " not in json.dumps(result.diagnostic.model_dump())


@pytest.mark.asyncio
async def test_test_pre_send_conflict_zero_http(tmp_path: Path) -> None:
    from contextlib import suppress

    from enterprise_knowledge.search_http import (
        SearchHttpAttempt,
        SearchHttpError,
        SearchSourceConfig,
    )
    from enterprise_knowledge.search_settings import (
        SEARCH_SOURCES,
        SearchSettingsService,
        _error,
    )

    from octop.infra.agents.plugins.host_contracts import (
        PluginActionError,
        PluginActor,
        RecordWrite,
    )

    _, actor_id, facade = _host(tmp_path)
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"results": []}, request=request)

    service = SearchSettingsService(facade, transport=httpx.MockTransport(handler))
    admin = PluginActor(actor_id, True)
    saved = await service.save(admin, _save_input())
    observed = saved.revision
    assert saved.primary.base_url is not None

    async def pre_send() -> None:
        current = await facade.get_record(SEARCH_SOURCES, "default")
        assert current is not None
        await facade.commit(
            (
                RecordWrite(
                    SEARCH_SOURCES,
                    "default",
                    current.owner_user_id,
                    {**current.payload, "bumped": True},
                    current.revision,
                ),
            )
        )
        fresh = await service._fresh_admin(admin)
        latest = await facade.get_record(SEARCH_SOURCES, "default")
        if latest is None or latest.revision != observed:
            raise _error(fresh, "CONFIG_CONFLICT", 409)

    attempt = SearchHttpAttempt(
        SearchSourceConfig(provider="SEARXNG", base_url=saved.primary.base_url),
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(PluginActionError) as caught:
            await attempt.search("q", pre_send=pre_send)
        assert caught.value.code == "CONFIG_CONFLICT"
    finally:
        with suppress(SearchHttpError):
            await attempt.aclose()
    assert requests == []


@pytest.mark.asyncio
async def test_recovery_blocked_rejects_save_and_test(tmp_path: Path) -> None:
    from enterprise_knowledge.search_settings import (
        SEARCH_INTENTS,
        SEARCH_RECOVERY,
        SEARCH_SOURCES,
        SearchRecoverInput,
        SearchSettingsService,
        SearchTestInput,
    )

    from octop.infra.agents.plugins.host_contracts import (
        PluginActionError,
        PluginActor,
        RecordWrite,
    )

    _, actor_id, facade = _host(tmp_path)
    service = SearchSettingsService(facade)
    admin = PluginActor(actor_id, True)
    saved = await service.save(admin, _save_input())

    intent = await facade.get_record(SEARCH_INTENTS, "default")
    assert intent is not None
    recovery_id = "11111111-1111-4111-8111-111111111111"
    await facade.commit(
        (
            RecordWrite(
                SEARCH_INTENTS,
                "default",
                admin.user_id,
                {
                    "recovery_id": recovery_id,
                    "state": "BLOCKED",
                    "staged_credential_ref": None,
                    "staged_credential_owner_user_id": None,
                },
                intent.revision,
            ),
            RecordWrite(
                SEARCH_RECOVERY,
                recovery_id,
                admin.user_id,
                {
                    "recovery_id": recovery_id,
                    "state": "BLOCKED",
                    "staged_credential_ref": None,
                    "staged_credential_owner_user_id": None,
                },
                0,
            ),
        )
    )

    settings = await service.get_settings(admin)
    assert settings.recovery_blocked is True

    with pytest.raises(PluginActionError) as save_err:
        await service.save(admin, _save_input(revision=saved.revision))
    assert save_err.value.code == "RECOVERY_BLOCKED"

    with pytest.raises(PluginActionError) as test_err:
        await service.test(
            admin,
            SearchTestInput(
                source_role="PRIMARY",
                expected_revision=saved.revision,
                public_query="q",
            ),
        )
    assert test_err.value.code == "RECOVERY_BLOCKED"

    recovered = await service.recover(admin, SearchRecoverInput(expected_revision=saved.revision))
    assert recovered.recovery_blocked is False
    # sources untouched
    current = await facade.get_record(SEARCH_SOURCES, "default")
    assert current is not None and current.revision == saved.revision


@pytest.mark.asyncio
async def test_secondary_test_uses_bearer_and_records_error_without_query_leak(
    tmp_path: Path,
) -> None:
    from enterprise_knowledge.search_settings import SearchSettingsService, SearchTestInput

    from octop.infra.agents.plugins.host_contracts import PluginActor

    _, actor_id, facade = _host(tmp_path)
    auth_headers: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        auth_headers.append(request.headers.get("Authorization"))
        await request.aread()
        return httpx.Response(401, request=request)

    service = SearchSettingsService(facade, transport=httpx.MockTransport(handler))
    admin = PluginActor(actor_id, True)
    saved = await service.save(
        admin,
        _save_input(secondary_op="REPLACE", secondary_credential="fc-token"),
    )
    result = await service.test(
        admin,
        SearchTestInput(
            source_role="SECONDARY",
            expected_revision=saved.revision,
            public_query="secret-query-sentinel",
        ),
    )
    assert auth_headers == ["Bearer fc-token"]
    assert result.diagnostic.status == "ERROR"
    assert result.diagnostic.code == "AUTH_REJECTED"
    dumped = json.dumps(result.model_dump())
    assert "secret-query-sentinel" not in dumped
    assert "fc-token" not in dumped
