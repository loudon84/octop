from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from octop.config import OctopConfig
from octop.infra.agents.plugins.host import PluginHost, PluginHostError
from octop.infra.agents.plugins.host_contracts import (
    CredentialActivation,
    RecordCheck,
    RecordWrite,
)
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.plugin_records import PluginRecordRepo
from octop.infra.db.repos.users import UserRepo
from octop.infra.db.services import build_shared_services
from octop.infra.utils.paths import PathLayout


@pytest.fixture
def db(tmp_path: Path) -> SqlitePool:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


def test_record_cas_create_update_and_owner_conflict(db: SqlitePool) -> None:
    owner_id = UserRepo(db).create(username="owner", role="user")
    repo = PluginRecordRepo(db)

    created = repo.cas(
        "plugin-a",
        "prefs",
        "theme",
        owner_user_id=owner_id,
        payload_json='{"theme":"dark"}',
        expected_revision=0,
    )
    assert created is not None
    assert created.revision == 1
    assert created.id
    assert created.pk > 0
    assert created.pk != created.id
    assert (
        repo.cas(
            "plugin-a",
            "prefs",
            "theme",
            owner_user_id=owner_id,
            payload_json='{"theme":"light"}',
            expected_revision=0,
        )
        is None
    )

    updated = repo.cas(
        "plugin-a",
        "prefs",
        "theme",
        owner_user_id=owner_id,
        payload_json='{"theme":"light"}',
        expected_revision=1,
    )
    assert updated is not None
    assert updated.id == created.id
    assert updated.revision == 2
    assert updated.payload_json == '{"theme":"light"}'
    assert (
        repo.cas(
            "plugin-a",
            "prefs",
            "theme",
            owner_user_id=owner_id + 1,
            payload_json="{}",
            expected_revision=2,
        )
        is None
    )
    with pytest.raises(ValueError):
        repo.cas(
            "plugin-a",
            "prefs",
            "theme",
            owner_user_id=owner_id,
            payload_json="{}",
            expected_revision=-1,
        )


def test_record_prefix_treats_like_characters_as_literals(db: SqlitePool) -> None:
    owner_id = UserRepo(db).create(username="owner", role="user")
    repo = PluginRecordRepo(db)
    for key in ("part%_one", "part%_two", "partXXone", "other"):
        assert (
            repo.cas(
                "plugin-a",
                "prefs",
                key,
                owner_user_id=owner_id,
                payload_json="{}",
                expected_revision=0,
            )
            is not None
        )

    rows = repo.list_prefix("plugin-a", "prefs", "part%_")
    assert [row.logical_key for row in rows] == ["part%_one", "part%_two"]


def test_shared_services_exposes_plugin_repositories(db: SqlitePool, tmp_path: Path) -> None:
    paths = PathLayout(tmp_path)
    services = build_shared_services(db=db, paths=paths, config=OctopConfig())

    assert isinstance(services.plugin_record_repo, PluginRecordRepo)
    from octop.infra.db.repos.plugin_credentials import PluginCredentialRepo

    assert isinstance(services.plugin_credential_repo, PluginCredentialRepo)


def test_plugin_storage_schema_enforces_keys_revisions_and_owner_fk(db: SqlitePool) -> None:
    owner_id = UserRepo(db).create(username="owner", role="user")
    with db.connect() as conn:
        conn.execute(
            "INSERT INTO plugin_records("
            "record_id, plugin_id, namespace, logical_key, owner_user_id, revision, "
            "payload_json, created_at, updated_at"
            ") VALUES ('record-1', 'plugin-a', 'prefs', 'theme', ?, 1, '{}', 1, 1)",
            (owner_id,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO plugin_records("
                "record_id, plugin_id, namespace, logical_key, owner_user_id, revision, "
                "payload_json, created_at, updated_at"
                ") VALUES ('record-2', 'plugin-a', 'prefs', 'theme', ?, 1, '{}', 1, 1)",
                (owner_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO plugin_records("
                "record_id, plugin_id, namespace, logical_key, owner_user_id, revision, "
                "payload_json, created_at, updated_at"
                ") VALUES ('record-3', 'plugin-a', 'prefs', 'other', ?, 0, '{}', 1, 1)",
                (owner_id,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO plugin_records("
                "record_id, plugin_id, namespace, logical_key, owner_user_id, revision, "
                "payload_json, created_at, updated_at"
                ") VALUES ('record-4', 'plugin-a', 'prefs', 'other', ?, 1, '{}', 1, 1)",
                (owner_id + 100,),
            )


def test_record_cas_two_real_connections_only_one_revision_wins(tmp_path: Path) -> None:
    path = tmp_path / "octop.db"
    first_db = SqlitePool(path)
    run_migrations(first_db)
    owner_id = UserRepo(first_db).create(username="owner", role="user")
    repo = PluginRecordRepo(first_db)
    created = repo.cas(
        "plugin-a",
        "prefs",
        "theme",
        owner_user_id=owner_id,
        payload_json="{}",
        expected_revision=0,
    )
    assert created is not None
    second_db = SqlitePool(path)
    barrier = threading.Barrier(2)
    results: list[object] = []

    def update(pool: SqlitePool, value: str) -> None:
        barrier.wait()
        results.append(
            PluginRecordRepo(pool).cas(
                "plugin-a",
                "prefs",
                "theme",
                owner_user_id=owner_id,
                payload_json=value,
                expected_revision=1,
            )
        )

    threads = [
        threading.Thread(target=update, args=(first_db, '{"v":1}')),
        threading.Thread(target=update, args=(second_db, '{"v":2}')),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert all(not thread.is_alive() for thread in threads)
    assert sum(result is not None for result in results) == 1
    assert repo.get("plugin-a", "prefs", "theme").revision == 2
    second_db.close()
    first_db.close()


def test_record_and_credential_operations_share_caller_transaction(db: SqlitePool) -> None:
    from octop.infra.db.repos.plugin_credentials import PluginCredentialRepo

    owner_id = UserRepo(db).create(username="owner", role="user")
    records = PluginRecordRepo(db)
    credentials = PluginCredentialRepo(db)
    with pytest.raises(RuntimeError, match="rollback"), db.transaction() as conn:
        assert (
            records.cas(
                "plugin-a",
                "prefs",
                "first",
                owner_user_id=owner_id,
                payload_json="{}",
                expected_revision=0,
                conn=conn,
            )
            is not None
        )
        assert (
            records.cas(
                "plugin-a",
                "prefs",
                "second",
                owner_user_id=owner_id,
                payload_json="{}",
                expected_revision=0,
                conn=conn,
            )
            is not None
        )
        credential = credentials.stage("plugin-a", owner_id, b"cipher", conn=conn)
        assert (
            credentials.activate(
                "plugin-a",
                credential.id,
                owner_id,
                expected_revision=1,
                conn=conn,
            )
            is not None
        )
        raise RuntimeError("rollback")
    assert records.get("plugin-a", "prefs", "first") is None
    assert records.get("plugin-a", "prefs", "second") is None
    assert credentials.get("plugin-a", credential.id, owner_id) is None


@pytest.mark.asyncio
async def test_checks_validate_revision_namespace_and_duplicate_expectations(
    db: SqlitePool, tmp_path: Path
) -> None:
    services = build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())
    owner_id = UserRepo(db).create(username="check-validation-owner", role="user")
    host = PluginHost(lambda: services)
    facade = host.bind("demo-plugin", "1")
    assert await host.bind("other-plugin", "1").commit(
        [RecordWrite("prefs", "same", owner_id, {}, 0)]
    )

    for revision in (True, 1.0, -1):
        with pytest.raises(PluginHostError) as exc:
            await facade.commit([], checks=[RecordCheck("prefs", "same", revision)])  # type: ignore[arg-type]
        assert exc.value.code == "invalid"
    with pytest.raises(PluginHostError) as exc:
        await facade.commit([], checks=[RecordCheck("@host", "same", 0)])
    assert exc.value.code == "denied"
    with pytest.raises(PluginHostError) as exc:
        await facade.commit(
            [],
            checks=[RecordCheck("prefs", "same", 0), RecordCheck("prefs", "same", 1)],
        )
    assert exc.value.code == "invalid"
    with pytest.raises(PluginHostError) as exc:
        await facade.commit([], checks=[RecordCheck("prefs", "same", 1)])
    assert exc.value.code == "conflict"

    assert (
        await facade.commit(
            [], checks=[RecordCheck("prefs", "same", 0), RecordCheck("prefs", "same", 0)]
        )
        == []
    )


@pytest.mark.asyncio
async def test_postgres_check_commit_locks_before_check_read(tmp_path: Path) -> None:
    from contextlib import contextmanager
    from types import SimpleNamespace

    statements: list[str] = []

    class Connection:
        def execute(self, sql: str, params=()):
            statements.append(sql)
            return self

        def fetchone(self):
            return None

    class PostgresPoolStub:
        dialect = "postgresql"

        @contextmanager
        def transaction(self):
            yield Connection()

    from octop.infra.db.repos.plugin_records import PluginRecordRepo

    db_stub = PostgresPoolStub()
    services = SimpleNamespace(
        db=db_stub,
        plugin_record_repo=PluginRecordRepo(db_stub),
        plugin_credential_repo=object(),
        secret_repo=object(),
    )
    facade = PluginHost(lambda: services).bind("demo-plugin", "1")
    assert await facade.commit([]) == []
    assert statements == []
    assert await facade.commit([], checks=[RecordCheck("prefs", "theme", 0)]) == []
    assert statements == [
        "LOCK TABLE plugin_records IN SHARE ROW EXCLUSIVE MODE",
        "SELECT * FROM plugin_records WHERE plugin_id = ? AND namespace = ? AND logical_key = ?",
    ]


@pytest.mark.asyncio
async def test_record_checks_allow_source_revision_without_rewriting_it(
    db: SqlitePool, tmp_path: Path
) -> None:
    services = build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())
    owner_id = UserRepo(db).create(username="check-owner", role="user")
    host = PluginHost(lambda: services)
    facade = host.bind("demo-plugin", "1")
    source = services.plugin_record_repo.cas(
        "demo-plugin",
        "connection",
        "one",
        owner_user_id=owner_id,
        payload_json='{"credential":"c1"}',
        expected_revision=0,
    )
    assert source is not None
    credential = services.plugin_credential_repo.stage("demo-plugin", owner_id, b"cipher")
    before = services.plugin_record_repo.get("demo-plugin", "connection", "one")

    rows = await facade.commit(
        [RecordWrite("sessions", "s1", owner_id, {"connection": "one"}, 0)],
        [CredentialActivation(credential.id, owner_id, 1)],
        checks=[RecordCheck("connection", "one", 1)],
    )

    after = services.plugin_record_repo.get("demo-plugin", "connection", "one")
    assert rows[0].revision == 1
    assert after == before
    assert (
        services.plugin_credential_repo.get("demo-plugin", credential.id, owner_id).state
        == "active"
    )


@pytest.mark.asyncio
async def test_failed_record_check_rolls_back_writes_and_credential_activation(
    db: SqlitePool, tmp_path: Path
) -> None:
    services = build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())
    owner_id = UserRepo(db).create(username="stale-owner", role="user")
    host = PluginHost(lambda: services)
    facade = host.bind("demo-plugin", "1")
    credential = await facade.stage_credential(owner_id, {"token": "staged"})
    assert await facade.commit([RecordWrite("connection", "one", owner_id, {"v": 1}, 0)])

    with pytest.raises(PluginHostError) as exc:
        await facade.commit(
            [RecordWrite("sessions", "s1", owner_id, {}, 0)],
            [CredentialActivation(credential, owner_id, 1)],
            checks=[RecordCheck("connection", "one", 0)],
        )
    assert exc.value.code == "conflict"
    assert await facade.get_record("sessions", "s1") is None
    assert (
        services.plugin_credential_repo.get("demo-plugin", credential, owner_id).state == "staged"
    )


@pytest.mark.asyncio
async def test_absent_record_check_serializes_against_create_on_second_pool(
    tmp_path: Path,
) -> None:
    import asyncio
    from types import SimpleNamespace

    path = tmp_path / "shared.db"
    first = SqlitePool(path)
    run_migrations(first)
    second = SqlitePool(path)
    owner_id = UserRepo(first).create(username="race-owner", role="user")
    services = build_shared_services(db=first, paths=PathLayout(tmp_path), config=OctopConfig())
    read_started = threading.Event()
    allow_check = threading.Event()
    original_repo = services.plugin_record_repo

    class PausingRepo:
        def __getattr__(self, name: str):
            return getattr(original_repo, name)

        def get(self, plugin_id: str, namespace: str, logical_key: str, *, conn=None):
            row = original_repo.get(plugin_id, namespace, logical_key, conn=conn)
            if namespace == "connection" and logical_key == "one":
                read_started.set()
                if not allow_check.wait(3):
                    raise TimeoutError("test did not release check")
            return row

    services = SimpleNamespace(
        db=first,
        plugin_record_repo=PausingRepo(),
        plugin_credential_repo=services.plugin_credential_repo,
        secret_repo=services.secret_repo,
    )
    facade = PluginHost(lambda: services).bind("demo-plugin", "1")
    commit_task = asyncio.create_task(
        facade.commit(
            [RecordWrite("sessions", "s1", owner_id, {}, 0)],
            checks=[RecordCheck("connection", "one", 0)],
        )
    )
    assert await asyncio.to_thread(read_started.wait, 3)
    writer_started = threading.Event()
    writer_done = threading.Event()

    def concurrent_create() -> None:
        writer_started.set()
        PluginRecordRepo(second).cas(
            "demo-plugin",
            "connection",
            "one",
            owner_user_id=owner_id,
            payload_json='{"raced":true}',
            expected_revision=0,
        )
        writer_done.set()

    writer_task = asyncio.create_task(asyncio.to_thread(concurrent_create))
    assert await asyncio.to_thread(writer_started.wait, 3)
    await asyncio.sleep(0.05)
    assert not writer_done.is_set()
    allow_check.set()
    rows = await commit_task
    await writer_task
    assert rows[0].revision == 1
    assert writer_done.is_set()
    assert original_repo.get("demo-plugin", "connection", "one") is not None
    second.close()
    first.close()


@pytest.mark.asyncio
async def test_commit_uses_validated_check_snapshot_when_input_list_changes(
    db: SqlitePool, tmp_path: Path
) -> None:
    import asyncio
    from contextlib import contextmanager
    from types import SimpleNamespace

    services = build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())
    owner_id = UserRepo(db).create(username="mutable-check-owner", role="user")
    source = services.plugin_record_repo.cas(
        "demo-plugin",
        "connection",
        "one",
        owner_user_id=owner_id,
        payload_json='{"revision":1}',
        expected_revision=0,
    )
    assert source is not None
    transaction_entered = threading.Event()
    allow_transaction = threading.Event()

    class PausingPool:
        dialect = db.dialect

        @contextmanager
        def transaction(self):
            transaction_entered.set()
            if not allow_transaction.wait(3):
                raise TimeoutError("test did not release transaction")
            with db.transaction() as conn:
                yield conn

    transaction_services = SimpleNamespace(
        db=PausingPool(),
        plugin_record_repo=services.plugin_record_repo,
        plugin_credential_repo=services.plugin_credential_repo,
        secret_repo=services.secret_repo,
    )
    facade = PluginHost(lambda: transaction_services).bind("demo-plugin", "1")
    checks = [RecordCheck("connection", "one", 1)]
    commit_task = asyncio.create_task(
        facade.commit(
            [RecordWrite("sessions", "s1", owner_id, {"connection": "one"}, 0)],
            checks=checks,
        )
    )
    assert await asyncio.to_thread(transaction_entered.wait, 3)
    changed = services.plugin_record_repo.cas(
        "demo-plugin",
        "connection",
        "one",
        owner_user_id=owner_id,
        payload_json='{"revision":2}',
        expected_revision=1,
    )
    assert changed is not None and changed.revision == 2
    checks.clear()
    allow_transaction.set()

    with pytest.raises(PluginHostError) as exc:
        await commit_task
    assert exc.value.code == "conflict"
    assert services.plugin_record_repo.get("demo-plugin", "connection", "one").revision == 2
    assert services.plugin_record_repo.get("demo-plugin", "sessions", "s1") is None
