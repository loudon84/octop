from __future__ import annotations

import asyncio
import logging
import threading
import traceback
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from octop.config import OctopConfig
from octop.infra.agents.plugins.host import PluginHost, PluginHostError
from octop.infra.agents.plugins.host_contracts import PluginActor
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.users import UserRepo
from octop.infra.db.services import build_shared_services
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def services(tmp_path: Path) -> Any:
    db = SqlitePool(tmp_path / "octop.db")
    run_migrations(db)
    return build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())


def _with_user_repo(services: Any, user_repo: Any) -> Any:
    return replace(services, repos=replace(services.repos, user_repo=user_repo))


@pytest.mark.asyncio
async def test_reauthorizes_cached_roles_against_database_without_identity_setup(
    services: Any,
) -> None:
    users = UserRepo(services.db)
    user_id = users.create(
        username="reauthorize-admin",
        role="user",
        permissions=["agent_plugins"],
    )
    facade = PluginHost(lambda: services).bind("reauthorization", "1")

    stale_admin = PluginActor(
        user_id,
        True,
        frozenset({"stale"}),
        operation_id="turn-1",
        locale="zh",
    )
    with pytest.raises(PluginHostError) as denied:
        await facade.reauthorize_actor(stale_admin, "admin")
    assert denied.value.code == "denied"

    users.set_role(user_id, "admin")
    users.set_permissions(user_id, ["fresh-permission"])
    fresh = await facade.reauthorize_actor(PluginActor(user_id, False), "admin")
    assert fresh == PluginActor(
        user_id,
        True,
        frozenset({"fresh-permission"}),
        locale=users.get(user_id).locale,
    )

    no_agent = await facade.reauthorize_actor(PluginActor(user_id, False))
    assert no_agent.is_admin
    with pytest.raises(PluginHostError) as owner_denied:
        await facade.reauthorize_actor(PluginActor(user_id, True), "agent_owner")
    assert owner_denied.value.code == "denied"


@pytest.mark.asyncio
async def test_reauthorization_checks_real_agent_and_thread_ownership(services: Any) -> None:
    users = UserRepo(services.db)
    owner_id = users.create(username="reauthorize-owner", role="user")
    other_id = users.create(username="reauthorize-other", role="user")
    shared_id = services.agent_repo.create(
        agent_id="shared-agent",
        user_id=other_id,
        name="Shared",
    )
    own_id = services.agent_repo.create(
        agent_id="owned-agent",
        user_id=owner_id,
        name="Owned",
    )
    services.agent_repo.set_shared("shared-agent", True)
    services.thread_repo.insert(
        thread_id="owner-thread",
        agent_id="owned-agent",
        user_id=owner_id,
        channel_type="web",
        session_key="owner-session",
    )
    services.thread_repo.insert(
        thread_id="other-thread",
        agent_id="owned-agent",
        user_id=other_id,
        channel_type="web",
        session_key="other-session",
    )
    assert shared_id == "shared-agent"
    assert own_id == "owned-agent"
    facade = PluginHost(lambda: services).bind("reauthorization-scope", "1")

    shared = await facade.reauthorize_actor(PluginActor(owner_id, False, agent_id="shared-agent"))
    assert shared.agent_id == "shared-agent"
    owner = await facade.reauthorize_actor(
        PluginActor(owner_id, False, agent_id="owned-agent", thread_id="owner-thread"),
        "agent_owner",
    )
    assert owner.thread_id == "owner-thread"

    for actor, permission, code in (
        (PluginActor(other_id, False, agent_id="owned-agent"), "authenticated", "denied"),
        (
            PluginActor(owner_id, True, agent_id="owned-agent", thread_id="other-thread"),
            "authenticated",
            "denied",
        ),
        (PluginActor(owner_id, False, agent_id="missing-agent"), "authenticated", "not_found"),
    ):
        with pytest.raises(PluginHostError) as error:
            await facade.reauthorize_actor(actor, permission)  # type: ignore[arg-type]
        assert error.value.code == code


@pytest.mark.asyncio
async def test_reauthorization_rejects_invalid_arguments_before_services_lookup() -> None:
    calls = 0

    def services_provider() -> None:
        nonlocal calls
        calls += 1
        return None

    facade = PluginHost(services_provider).bind("reauthorization-inputs", "1")
    for actor, permission in (
        (object(), "authenticated"),
        (PluginActor(1, False), "unknown"),
        (PluginActor(1, False), None),
        (PluginActor(1, False), 1),
    ):
        with pytest.raises(PluginHostError) as error:
            await facade.reauthorize_actor(actor, permission)  # type: ignore[arg-type]
        assert error.value.code == "invalid"
    assert calls == 0


@pytest.mark.asyncio
async def test_reauthorization_denies_disabled_or_deleted_users(services: Any) -> None:
    users = UserRepo(services.db)
    disabled_id = users.create(username="reauthorize-disabled", role="admin")
    deleted_id = users.create(username="reauthorize-deleted", role="admin")
    users.set_disabled(disabled_id, True)
    users.delete(deleted_id)
    facade = PluginHost(lambda: services).bind("reauthorization-users", "1")

    for user_id in (disabled_id, deleted_id):
        with pytest.raises(PluginHostError) as error:
            await facade.reauthorize_actor(PluginActor(user_id, True), "admin")
        assert error.value.code == "denied"


@pytest.mark.asyncio
async def test_reauthorization_rejects_facades_after_rebind_deactivate_or_close(
    services: Any,
) -> None:
    user_id = UserRepo(services.db).create(username="reauthorize-stale", role="admin")
    for lifecycle in ("rebind", "deactivate", "close"):
        host = PluginHost(lambda: services)
        facade = host.bind(f"reauthorize-{lifecycle}", "1")
        if lifecycle == "rebind":
            host.bind(facade.plugin_id, "2")
        elif lifecycle == "deactivate":
            host.deactivate(facade.plugin_id)
        else:
            host.close()

        with pytest.raises(PluginHostError) as error:
            await facade.reauthorize_actor(PluginActor(user_id, False))
        assert error.value.code == "unavailable"


@pytest.mark.asyncio
async def test_reauthorization_hides_database_exception_context_and_logs(
    services: Any, caplog: pytest.LogCaptureFixture
) -> None:
    user_id = UserRepo(services.db).create(username="reauthorize-db-error", role="user")
    real_users = services.user_repo

    class BrokenUserRepo:
        def get(self, _user_id: int) -> Any:
            raise RuntimeError("synthetic-secret-marker")

        def __getattr__(self, name: str) -> Any:
            return getattr(real_users, name)

    broken_services = _with_user_repo(services, BrokenUserRepo())
    facade = PluginHost(lambda: broken_services).bind("reauthorization-error", "1")
    caplog.set_level(logging.DEBUG)

    with pytest.raises(PluginHostError) as error:
        await facade.reauthorize_actor(PluginActor(user_id, False))

    rendered = "".join(
        traceback.format_exception(type(error.value), error.value, error.value.__traceback__)
    )
    assert error.value.code == "unavailable"
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__
    assert "synthetic-secret-marker" not in rendered
    assert "synthetic-secret-marker" not in caplog.text


@pytest.mark.asyncio
async def test_invalidation_during_blocked_read_drains_before_old_facade_delivery(
    services: Any,
) -> None:
    user_id = UserRepo(services.db).create(username="reauthorize-blocked", role="admin")
    real_users = services.user_repo
    entered_read = threading.Event()
    release_read = threading.Event()

    class BlockingUserRepo:
        def get(self, requested_id: int) -> Any:
            entered_read.set()
            if not release_read.wait(timeout=5):
                raise RuntimeError("read barrier timeout")
            return real_users.get(requested_id)

        def __getattr__(self, name: str) -> Any:
            return getattr(real_users, name)

    blocking_services = _with_user_repo(services, BlockingUserRepo())
    host = PluginHost(lambda: blocking_services)
    facade = host.bind("reauthorization-drain", "1")
    invalidation_finished = asyncio.Event()
    actual_run = facade._run

    async def hold_delivery_until_drain(operation: Any, **kwargs: Any) -> Any:
        result = await actual_run(operation, **kwargs)
        await invalidation_finished.wait()
        return result

    facade._run = hold_delivery_until_drain  # type: ignore[method-assign]
    request = asyncio.create_task(facade.reauthorize_actor(PluginActor(user_id, False), "admin"))
    invalidation: asyncio.Task[None] | None = None
    try:
        assert await asyncio.to_thread(entered_read.wait, 2)

        invalidation = asyncio.create_task(host.deactivate_and_wait("reauthorization-drain"))
        heartbeat = asyncio.Event()
        asyncio.get_running_loop().call_soon(heartbeat.set)
        await asyncio.wait_for(heartbeat.wait(), timeout=0.5)
        assert not invalidation.done()
        assert not request.done()

        release_read.set()
        await asyncio.wait_for(invalidation, timeout=3)
        invalidation_finished.set()
        with pytest.raises(PluginHostError) as error:
            await asyncio.wait_for(request, timeout=3)
        assert error.value.code == "unavailable"
    finally:
        release_read.set()
        invalidation_finished.set()
        if invalidation is not None:
            await asyncio.gather(invalidation, return_exceptions=True)
        await asyncio.gather(request, return_exceptions=True)
