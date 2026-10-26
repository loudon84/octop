from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from octop_harness.plugins.context import PluginContext
from octop_harness.plugins.manifest import PluginManifest

from octop.config import OctopConfig
from octop.infra.agents.plugins.host import PluginHost, PluginHostError, register_host_setup
from octop.infra.agents.plugins.host_contracts import (
    CredentialActivation,
    CredentialDeletion,
    RecordWrite,
    UserIdentityCheck,
)
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import PostgresPool
from octop.infra.db.repos.users import UserRepo
from octop.infra.db.services import build_shared_services
from octop.infra.utils.paths import PathLayout
from tests.support.postgresql import requires_postgresql


@pytest.fixture
def pg_pool() -> Any:
    import os

    url = os.environ["OCTOP_TEST_DATABASE_URL"]
    parsed = urlsplit(url)
    if (parsed.hostname, parsed.port, parsed.path.lstrip("/")) != (
        "127.0.0.1",
        51576,
        "octop_enterprise_p1_test",
    ):
        pytest.fail("I02b PostgreSQL tests are restricted to the task-owned loopback database")
    pool = PostgresPool(url)
    with pool.connect() as conn:
        row = conn.execute("SELECT current_database() AS name, current_user AS user").fetchone()
        if row["name"] != "octop_enterprise_p1_test" or row["user"] != "postgres":
            pool.close()
            pytest.fail("I02b PostgreSQL test runtime identity did not match the task brief")
    run_migrations(pool)
    yield pool
    pool.close()


@pytest.fixture
def pg_services(pg_pool: Any, tmp_path: Path) -> Any:
    return build_shared_services(db=pg_pool, paths=PathLayout(tmp_path), config=OctopConfig())


def _registered_facade(host: PluginHost, plugin_id: str) -> Any:
    context = PluginContext(
        manifest=PluginManifest(plugin_id, "1", plugin_id, "skill", "main.py"),
        source_path=Path(".").resolve(),
    )
    register_host_setup(context, lambda facade: facade.register_user_identity_consumer())
    return host.bind_context(context, "1")


@requires_postgresql
@pytest.mark.postgresql
@pytest.mark.asyncio
async def test_postgresql_identity_commit_and_active_credential_cleanup_are_atomic(
    pg_services: Any,
) -> None:
    user_id = UserRepo(pg_services.db).create(
        username=f"i02b-{uuid4().hex}", role="user", email=f"{uuid4().hex}@example.test"
    )
    host = PluginHost(lambda: pg_services)
    facade = _registered_facade(host, f"i02b-{uuid4().hex}")
    old_id = await facade.stage_credential(user_id, {"key": "old"})
    new_id = await facade.stage_credential(user_id, {"key": "new"})
    await facade.commit([], [CredentialActivation(old_id, user_id, 1)])
    identity = await facade.read_user_identity(user_id)

    await facade.commit(
        [RecordWrite("identity", "ready", user_id, {"active": new_id}, 0)],
        [CredentialActivation(new_id, user_id, 1)],
        identity_checks=[UserIdentityCheck(identity)],
        credential_deletions=[CredentialDeletion(old_id, user_id, 2)],
    )
    assert pg_services.plugin_credential_repo.get(facade.plugin_id, old_id, user_id) is None
    assert (
        pg_services.plugin_credential_repo.get(facade.plugin_id, new_id, user_id).state == "active"
    )

    stale = await facade.read_user_identity(user_id)
    UserRepo(pg_services.db).set_email(user_id, f"changed-{uuid4().hex}@example.test")
    with pytest.raises(PluginHostError) as exc:
        await facade.commit(
            [RecordWrite("identity", "stale", user_id, {}, 0)],
            identity_checks=[UserIdentityCheck(stale)],
        )
    assert exc.value.code == "conflict"
    assert await facade.get_record("identity", "stale") is None


@requires_postgresql
@pytest.mark.postgresql
@pytest.mark.asyncio
async def test_postgresql_late_deletion_conflict_rolls_back_prior_changes(
    pg_services: Any,
) -> None:
    user_id = UserRepo(pg_services.db).create(
        username=f"i02b-rollback-{uuid4().hex}",
        role="user",
        email=f"{uuid4().hex}@example.test",
    )
    host = PluginHost(lambda: pg_services)
    facade = _registered_facade(host, f"i02b-rollback-{uuid4().hex}")
    old_first = await facade.stage_credential(user_id, {"key": "first"})
    old_second = await facade.stage_credential(user_id, {"key": "second"})
    new_id = await facade.stage_credential(user_id, {"key": "new"})
    await facade.commit(
        [],
        [
            CredentialActivation(old_first, user_id, 1),
            CredentialActivation(old_second, user_id, 1),
        ],
    )
    snapshot = await facade.read_user_identity(user_id)

    with pytest.raises(PluginHostError) as exc:
        await facade.commit(
            [RecordWrite("identity", "rollback", user_id, {"active": new_id}, 0)],
            [CredentialActivation(new_id, user_id, 1)],
            identity_checks=[UserIdentityCheck(snapshot)],
            credential_deletions=[
                CredentialDeletion(old_first, user_id, 2),
                CredentialDeletion(old_second, user_id, 999),
            ],
        )

    assert exc.value.code == "conflict"
    assert await facade.get_record("identity", "rollback") is None
    assert (
        pg_services.plugin_credential_repo.get(facade.plugin_id, new_id, user_id).state == "staged"
    )
    assert (
        pg_services.plugin_credential_repo.get(facade.plugin_id, old_first, user_id).state
        == "active"
    )
    assert (
        pg_services.plugin_credential_repo.get(facade.plugin_id, old_second, user_id).state
        == "active"
    )


@requires_postgresql
@pytest.mark.postgresql
@pytest.mark.parametrize("field", ["email", "disabled"])
@pytest.mark.parametrize("boundary", ["commit", "rollback"])
@pytest.mark.asyncio
async def test_postgresql_identity_share_lock_blocks_concurrent_user_update(
    pg_services: Any, field: str, boundary: str
) -> None:
    user_id = UserRepo(pg_services.db).create(
        username=f"i02b-lock-{uuid4().hex}", role="user", email=f"{uuid4().hex}@example.test"
    )

    class PausingPool:
        @contextmanager
        def transaction(self):
            with self.pool.transaction() as conn:
                yield conn
                self.transaction_open.set()
                if self.pause:
                    if not self.allow_commit.wait(5):
                        raise TimeoutError("test did not release PostgreSQL transaction")
                    if self.boundary == "rollback":
                        raise RuntimeError("forced rollback after commit body")

        def __init__(self, pool: Any, transaction_boundary: str) -> None:
            self.pool = pool
            self.transaction_open = threading.Event()
            self.allow_commit = threading.Event()
            self.pause = True
            self.boundary = transaction_boundary

    pause_db = PausingPool(pg_services.db, boundary)
    services = SimpleNamespace(
        db=pause_db,
        user_repo=pg_services.user_repo,
        plugin_record_repo=pg_services.plugin_record_repo,
        plugin_credential_repo=pg_services.plugin_credential_repo,
        secret_repo=pg_services.secret_repo,
    )
    host = PluginHost(lambda: services)
    facade = _registered_facade(host, f"i02b-lock-{uuid4().hex}")
    snapshot = await facade.read_user_identity(user_id)
    commit_task = asyncio.create_task(
        facade.commit(
            [RecordWrite("identity", field, user_id, {}, 0)],
            identity_checks=[UserIdentityCheck(snapshot)],
        )
    )

    writer_started = threading.Event()
    writer_done = threading.Event()

    def update_user() -> None:
        writer_started.set()
        if field == "email":
            UserRepo(pg_services.db).set_email(user_id, f"updated-{uuid4().hex}@example.test")
        else:
            UserRepo(pg_services.db).set_disabled(user_id, True)
        writer_done.set()

    writer_task: asyncio.Task[None] | None = None
    outcomes: list[Any]
    try:
        assert await asyncio.to_thread(pause_db.transaction_open.wait, 3)
        writer_task = asyncio.create_task(asyncio.to_thread(update_user))
        assert await asyncio.to_thread(writer_started.wait, 2)
        assert not await asyncio.to_thread(writer_done.wait, 0.2)
    finally:
        pause_db.allow_commit.set()
        if writer_task is None:
            outcomes = await asyncio.gather(commit_task, return_exceptions=True)
        else:
            outcomes = await asyncio.gather(commit_task, writer_task, return_exceptions=True)
    if writer_task is None:
        raise AssertionError("writer was not started")
    update_result = outcomes[1]
    assert not isinstance(update_result, BaseException)
    assert writer_done.is_set()
    if boundary == "commit":
        assert not isinstance(outcomes[0], BaseException)
        assert await facade.get_record("identity", field) is not None
    else:
        assert isinstance(outcomes[0], PluginHostError)
        assert outcomes[0].code == "unavailable"
        assert await facade.get_record("identity", field) is None
