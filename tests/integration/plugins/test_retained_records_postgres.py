from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from octop_harness.plugins.context import PluginContext
from octop_harness.plugins.manifest import PluginManifest
from tests.support.postgresql import requires_postgresql

from octop.config import OctopConfig
from octop.infra.agents.plugins.host import PluginHost, PluginHostError, register_host_setup
from octop.infra.agents.plugins.host_contracts import (
    CredentialActivation,
    CredentialDeletion,
    RecordWrite,
    RetainedRecordCheck,
    RetainedRecordWrite,
    UserIdentityCheck,
)
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import PostgresPool
from octop.infra.db.repos.plugin_retained_records import PluginRetainedRecordRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.db.services import build_shared_services
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def pg_pool() -> PostgresPool:
    import os

    url = os.environ["OCTOP_TEST_DATABASE_URL"]
    parsed = urlsplit(url)
    if (parsed.hostname, parsed.port, parsed.path.lstrip("/")) != (
        "127.0.0.1",
        51576,
        "octop_enterprise_p1_test",
    ):
        pytest.fail("I02d PostgreSQL tests are restricted to the task-owned loopback database")
    pool = PostgresPool(url)
    with pool.connect() as conn:
        row = conn.execute("SELECT current_database() AS name, current_user AS user").fetchone()
        if row["name"] != "octop_enterprise_p1_test" or row["user"] != "postgres":
            pool.close()
            pytest.fail("I02d PostgreSQL test runtime identity did not match the task brief")
    run_migrations(pool)
    yield pool
    pool.close()


@pytest.fixture
def pg_services(pg_pool: PostgresPool, tmp_path: Path):
    return build_shared_services(db=pg_pool, paths=PathLayout(tmp_path), config=OctopConfig())


def _plugin() -> str:
    return f"i02d-{uuid4().hex}"


def _identity_facade(services, plugin_id: str):
    context = PluginContext(
        manifest=PluginManifest(plugin_id, "1", plugin_id, "skill", "main.py"),
        source_path=Path(".").resolve(),
    )
    register_host_setup(context, lambda facade: facade.register_user_identity_consumer())
    return PluginHost(lambda: services).bind_context(context, "1")


@pytest.mark.parametrize("rollback", [False, True], ids=["commit", "rollback"])
@requires_postgresql
@pytest.mark.postgresql
@pytest.mark.asyncio
async def test_absent_retained_check_holds_competing_cas_to_transaction_boundary(
    pg_services, pg_pool: PostgresPool, rollback: bool
) -> None:
    plugin_id = _plugin()
    owner = UserRepo(pg_pool).create(username=f"i02d-boundary-{uuid4().hex}", role="user")
    identity_facade = _identity_facade(pg_services, plugin_id)
    identity = await identity_facade.read_user_identity(owner)
    entered = threading.Event()
    release = threading.Event()

    class PausingPool:
        @contextmanager
        def transaction(self):
            with pg_pool.transaction() as conn:
                yield conn
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test did not release PostgreSQL transaction boundary")
                if rollback:
                    raise RuntimeError("force PostgreSQL transaction rollback")

    # SharedServices is frozen; the proxy preserves every real service and pauses only this commit.
    class ServicesProxy:
        def __getattr__(self, name: str):
            return getattr(pg_services, name)

    proxy = ServicesProxy()
    object.__setattr__(proxy, "db", PausingPool())
    facade = _identity_facade(proxy, plugin_id)
    commit_task = asyncio.create_task(
        facade.commit(
            [RecordWrite("state", "boundary", owner, {"committed": True}, 0)],
            identity_checks=[UserIdentityCheck(identity)],
            retained_checks=[RetainedRecordCheck("proof", "absent", 0)],
        )
    )
    writer_done = threading.Event()

    lock_attempted = threading.Event()

    class TrackingConnection:
        def __init__(self, conn) -> None:
            self._conn = conn

        def execute(self, sql, *args):
            if sql.startswith("LOCK TABLE settings"):
                lock_attempted.set()
            return self._conn.execute(sql, *args)

        def __getattr__(self, name: str):
            return getattr(self._conn, name)

    class TrackingPool:
        dialect = pg_pool.dialect

        @contextmanager
        def transaction(self):
            with pg_pool.transaction() as conn:
                yield TrackingConnection(conn)

    repo = PluginRetainedRecordRepo(TrackingPool())

    def competing_create() -> None:
        repo.cas(
            plugin_id,
            "proof",
            "absent",
            payload_json='{"format_version":1,"revision":1,"payload":{}}',
            expected_revision=0,
        )
        writer_done.set()

    writer_task: asyncio.Task[None] | None = None
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 4), 5)
        writer_task = asyncio.create_task(asyncio.to_thread(competing_create))
        assert await asyncio.wait_for(asyncio.to_thread(lock_attempted.wait, 2), 3)
        assert not await asyncio.to_thread(writer_done.wait, 0.2)
    finally:
        release.set()
        tasks: list[asyncio.Task[object]] = [commit_task]
        if writer_task is not None:
            tasks.append(writer_task)
        outcomes = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=6)
    commit_outcome = outcomes[0]
    if rollback:
        assert isinstance(commit_outcome, PluginHostError)
        assert commit_outcome.code == "unavailable"
        assert pg_services.plugin_record_repo.get(plugin_id, "state", "boundary") is None
    else:
        assert isinstance(commit_outcome, list) and len(commit_outcome) == 1
        boundary = await facade.get_record("state", "boundary")
        assert boundary is not None and boundary.payload == {"committed": True}
    assert writer_task is not None and len(outcomes) == 2
    assert writer_done.is_set()
    assert pg_services.plugin_retained_record_repo.get(plugin_id, "proof", "absent").revision == 1


@requires_postgresql
@pytest.mark.postgresql
@pytest.mark.asyncio
async def test_same_key_retained_cas_has_one_winner_across_pools(
    pg_services, pg_pool: PostgresPool, tmp_path: Path
) -> None:
    plugin_id = _plugin()
    import os

    second_pool = PostgresPool(os.environ["OCTOP_TEST_DATABASE_URL"])
    try:
        second_services = build_shared_services(
            db=second_pool, paths=PathLayout(tmp_path), config=OctopConfig()
        )
        first = PluginHost(lambda: pg_services).bind(plugin_id, "1")
        second = PluginHost(lambda: second_services).bind(plugin_id, "1")
        outcomes = await asyncio.gather(
            first.commit([], retained_writes=[RetainedRecordWrite("proof", "same", {"n": 1}, 0)]),
            second.commit([], retained_writes=[RetainedRecordWrite("proof", "same", {"n": 2}, 0)]),
            return_exceptions=True,
        )
        assert sum(not isinstance(value, BaseException) for value in outcomes) == 1
        failures = [value for value in outcomes if isinstance(value, PluginHostError)]
        assert len(failures) == 1 and failures[0].code == "conflict"
        row = pg_services.plugin_retained_record_repo.get(plugin_id, "proof", "same")
        assert row is not None and row.revision == 1
    finally:
        second_pool.close()


@requires_postgresql
@pytest.mark.postgresql
@pytest.mark.asyncio
async def test_covered_mixed_commit_locks_user_before_tables_and_user_delete_cascades(
    pg_services, pg_pool: PostgresPool
) -> None:
    plugin_id = _plugin()
    owner = UserRepo(pg_pool).create(username=f"i02d-{uuid4().hex}", role="user")
    facade = _identity_facade(pg_services, plugin_id)
    old_id = await facade.stage_credential(owner, {"proof": "old"})
    new_id = await facade.stage_credential(owner, {"proof": "new"})
    await facade.commit([], [CredentialActivation(old_id, owner, 1)])
    identity = await facade.read_user_identity(owner)

    identity_lock_acquired = threading.Event()
    release_after_identity_lock = threading.Event()

    class PausingUserRepo:
        def __getattr__(self, name: str):
            return getattr(pg_services.user_repo, name)

        def lock_identity_rows(self, conn, user_ids):
            rows = pg_services.user_repo.lock_identity_rows(conn, user_ids)
            identity_lock_acquired.set()
            if not release_after_identity_lock.wait(5):
                raise TimeoutError("test did not release PostgreSQL identity row lock")
            return rows

    class ServicesProxy:
        def __getattr__(self, name: str):
            if name == "user_repo":
                return PausingUserRepo()
            return getattr(pg_services, name)

    proxy = ServicesProxy()
    facade = _identity_facade(proxy, plugin_id)
    commit_task = asyncio.create_task(
        facade.commit(
            [RecordWrite("state", "ready", owner, {"credential": new_id}, 0)],
            [CredentialActivation(new_id, owner, 1)],
            identity_checks=[UserIdentityCheck(identity)],
            credential_deletions=[CredentialDeletion(old_id, owner, 2)],
            retained_writes=[RetainedRecordWrite("proof", "receipt", {"ok": True}, 0)],
            retained_checks=[RetainedRecordCheck("proof", "receipt", 0)],
        )
    )
    delete_started = threading.Event()
    delete_done = threading.Event()

    def delete_user() -> None:
        delete_started.set()
        UserRepo(pg_pool).delete(owner)
        delete_done.set()

    delete_task: asyncio.Task[None] | None = None
    outcomes: list[object] = []
    try:
        assert await asyncio.wait_for(asyncio.to_thread(identity_lock_acquired.wait, 4), 5)
        delete_task = asyncio.create_task(asyncio.to_thread(delete_user))
        assert await asyncio.wait_for(asyncio.to_thread(delete_started.wait, 2), 3)
        assert not await asyncio.to_thread(delete_done.wait, 0.2)
    finally:
        release_after_identity_lock.set()
        tasks: list[asyncio.Task[object]] = [commit_task]
        if delete_task is not None:
            tasks.append(delete_task)
        outcomes = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=6)
    assert len(outcomes) == 2 and isinstance(outcomes[0], list) and len(outcomes[0]) == 1
    assert outcomes[1] is None
    assert delete_done.is_set()
    assert await facade.get_record("state", "ready") is None
    assert pg_services.plugin_credential_repo.get(plugin_id, new_id, owner) is None
    retained = await facade.get_retained_record("proof", "receipt")
    assert retained is not None and retained.payload == {"ok": True}
