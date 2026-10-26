from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

import pytest
from octop_harness.plugins.context import PluginContext
from octop_harness.plugins.manifest import PluginManifest

from octop.config import OctopConfig
from octop.infra.agents.plugins.host import PluginHost, PluginHostError, register_host_setup
from octop.infra.agents.plugins.host_contracts import RecordWrite, UserIdentityCheck
from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.users import UserRepo
from octop.infra.db.services import build_shared_services
from octop.infra.utils.paths import PathLayout


def _context(plugin_id: str, setup: Any) -> PluginContext:
    context = PluginContext(
        manifest=PluginManifest(
            id=plugin_id,
            version="1",
            name=plugin_id,
            kind="skill",
            entry="main.py",
        ),
        source_path=Path("plugin"),
    )
    register_host_setup(context, setup)
    return context


@pytest.fixture
def services(tmp_path: Path) -> Any:
    db = SqlitePool(tmp_path / "octop.db")
    run_migrations(db)
    return build_shared_services(db=db, paths=PathLayout(tmp_path), config=OctopConfig())


@pytest.mark.asyncio
async def test_worker_registration_requires_setup_window_and_starts_once_with_set_event() -> None:
    host = PluginHost(lambda: None)
    started: list[asyncio.Event] = []
    released = asyncio.Event()
    worker_started = asyncio.Event()

    async def worker(wake: asyncio.Event) -> None:
        started.append(wake)
        worker_started.set()
        await released.wait()

    context = _context(
        "worker-plugin", lambda facade: facade.register_background_worker("sync", worker)
    )
    facade = host.bind_context(context, "1")
    with pytest.raises(PluginHostError) as exc:
        facade.register_background_worker("late", worker)
    assert exc.value.code == "denied"
    assert started == []

    await host.start_background_workers()
    await host.start_background_workers()
    registration = host._plugins["worker-plugin"].background_workers["sync"]
    assert registration.task is not None
    await asyncio.wait_for(worker_started.wait(), timeout=1)
    assert len(started) == 1
    assert started[0].is_set()
    released.set()
    await asyncio.wait_for(registration.task, timeout=1)
    await host.start_background_workers()
    assert len(started) == 1
    await host.close_and_wait()


@pytest.mark.asyncio
async def test_worker_wake_is_host_local_and_does_not_restart_failed_worker(caplog: Any) -> None:
    first = PluginHost(lambda: None)
    second = PluginHost(lambda: None)
    entered = asyncio.Event()
    fail = asyncio.Event()
    calls = 0
    second_waiting = asyncio.Event()

    async def worker(wake: asyncio.Event) -> None:
        nonlocal calls
        calls += 1
        entered.set()
        assert wake.is_set()
        wake.clear()
        await fail.wait()
        raise RuntimeError("remote-secret-marker")

    async def second_worker(wake: asyncio.Event) -> None:
        wake.clear()
        second_waiting.set()
        await wake.wait()

    ctx = _context("wake-plugin", lambda facade: facade.register_background_worker("sync", worker))
    first.bind_context(ctx, "1")
    second.bind_context(
        _context(
            "other-wake-plugin",
            lambda facade: facade.register_background_worker("sync", second_worker),
        ),
        "1",
    )
    await first.start_background_workers()
    await second.start_background_workers()
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.wait_for(second_waiting.wait(), 1)
    first.wake_background_workers()
    first.wake_background_workers()
    registration = first._plugins["wake-plugin"].background_workers["sync"]
    second_registration = second._plugins["other-wake-plugin"].background_workers["sync"]
    assert registration.event is not None
    assert second_registration.event is not None
    await asyncio.wait_for(registration.event.wait(), timeout=1)
    assert not second_registration.event.is_set()
    assert calls == 1
    fail.set()
    assert registration.task is not None
    await asyncio.wait_for(registration.task, timeout=1)
    await first.start_background_workers()
    assert calls == 1
    assert "remote-secret-marker" not in caplog.text
    await first.close_and_wait()
    await second.close_and_wait()


@pytest.mark.asyncio
async def test_dynamic_setup_from_executor_starts_on_owner_loop() -> None:
    host = PluginHost(lambda: None)
    owner_thread = threading.get_ident()
    started: asyncio.Future[int] = asyncio.get_running_loop().create_future()

    async def worker(wake: asyncio.Event) -> None:
        assert wake.is_set()
        if not started.done():
            started.set_result(threading.get_ident())
        await asyncio.Event().wait()

    await host.start_background_workers()
    context = _context(
        "dynamic-plugin", lambda facade: facade.register_background_worker("sync", worker)
    )
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, host.bind_context, context, "1")
    assert await asyncio.wait_for(started, 1) == owner_thread
    await host.deactivate_and_wait("dynamic-plugin")
    await host.close_and_wait()


@pytest.mark.asyncio
async def test_failed_setup_never_starts_registered_worker() -> None:
    host = PluginHost(lambda: None)
    calls: list[int] = []

    async def worker(_wake: asyncio.Event) -> None:
        calls.append(1)

    def setup(facade: Any) -> None:
        facade.register_background_worker("sync", worker)
        raise RuntimeError("setup failed")

    with pytest.raises(RuntimeError):
        host.bind_context(_context("failed-worker", setup), "1")
    await host.start_background_workers()
    assert calls == []
    assert "failed-worker" not in host.active_plugins
    await host.close_and_wait()


@pytest.mark.asyncio
async def test_queued_old_generation_start_is_discarded_after_replacement() -> None:
    host = PluginHost(lambda: None)
    await host.start_background_workers()
    old_started = asyncio.Event()
    new_started = asyncio.Event()
    old_schedule_entered = threading.Event()
    release_old_schedule = threading.Event()
    original_schedule = host._schedule_generation_workers
    delayed = False

    async def old_worker(_wake: asyncio.Event) -> None:
        old_started.set()

    async def new_worker(_wake: asyncio.Event) -> None:
        new_started.set()

    def delay_first_schedule(plugin_id: str, generation: int) -> None:
        nonlocal delayed
        if not delayed:
            delayed = True
            old_schedule_entered.set()
            if not release_old_schedule.wait(timeout=3):
                raise TimeoutError("old generation scheduling was not released")
        original_schedule(plugin_id, generation)

    host._schedule_generation_workers = delay_first_schedule  # type: ignore[method-assign]
    old_context = _context(
        "replace-worker", lambda facade: facade.register_background_worker("sync", old_worker)
    )
    binding = asyncio.get_running_loop().run_in_executor(None, host.bind_context, old_context, "1")
    assert await asyncio.wait_for(asyncio.to_thread(old_schedule_entered.wait, 2), 3)

    host._schedule_generation_workers = original_schedule  # type: ignore[method-assign]
    new_context = _context(
        "replace-worker", lambda facade: facade.register_background_worker("sync", new_worker)
    )
    host.bind_context(new_context, "2")
    release_old_schedule.set()
    await asyncio.wait_for(binding, 2)
    await asyncio.wait_for(new_started.wait(), 2)
    assert not old_started.is_set()
    await host.close_and_wait()


@pytest.mark.asyncio
async def test_deactivate_and_close_wait_for_worker_finally() -> None:
    host = PluginHost(lambda: None)
    entered = asyncio.Event()
    release_finally = asyncio.Event()
    finalized = asyncio.Event()

    async def worker(_wake: asyncio.Event) -> None:
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await release_finally.wait()
            finalized.set()

    context = _context(
        "drain-plugin", lambda facade: facade.register_background_worker("sync", worker)
    )
    host.bind_context(context, "1")
    await host.start_background_workers()
    await asyncio.wait_for(entered.wait(), 1)
    deactivation = asyncio.create_task(host.deactivate_and_wait("drain-plugin"))
    await asyncio.sleep(0)
    assert not deactivation.done()
    release_finally.set()
    await asyncio.wait_for(deactivation, 1)
    assert finalized.is_set()

    await host.close_and_wait()
    await host.close_and_wait()


@pytest.mark.asyncio
async def test_foreign_loop_start_is_rejected_and_unstarted_wake_is_safe() -> None:
    host = PluginHost(lambda: None)
    host.wake_background_workers()
    await host.start_background_workers()
    result: list[BaseException] = []

    def run_other_loop() -> None:
        try:
            asyncio.run(host.start_background_workers())
        except BaseException as exc:
            result.append(exc)

    await asyncio.get_running_loop().run_in_executor(None, run_other_loop)
    assert len(result) == 1
    assert isinstance(result[0], PluginHostError)
    assert result[0].code == "unavailable"
    await host.close_and_wait()


def test_registration_rejects_bad_name_callable_duplicate_and_bare_bind() -> None:
    host = PluginHost(lambda: None)
    bare = host.bind("bare-worker", "1")

    async def worker(_wake: asyncio.Event) -> None:
        return None

    with pytest.raises(PluginHostError) as exc:
        bare.register_background_worker("sync", worker)
    assert exc.value.code == "denied"

    attempts: list[str] = []

    def setup(facade: Any) -> None:
        for name, callback in (
            ("../bad", worker),
            ("sync", lambda _wake: None),
        ):
            with pytest.raises(PluginHostError) as error:
                facade.register_background_worker(name, callback)
            attempts.append(error.value.code)
        facade.register_background_worker("sync", worker)
        with pytest.raises(PluginHostError) as error:
            facade.register_background_worker("sync", worker)
        attempts.append(error.value.code)

    host.bind_context(_context("valid-worker", setup), "1")
    assert attempts == ["invalid", "invalid", "conflict"]


def test_registration_rejects_async_generator() -> None:
    host = PluginHost(lambda: None)

    async def generator_worker(_wake: asyncio.Event) -> Any:
        yield None

    def setup(facade: Any) -> None:
        facade.register_background_worker("generator", generator_worker)

    with pytest.raises(PluginHostError) as exc:
        host.bind_context(_context("async-generator", setup), "1")

    assert exc.value.code == "invalid"
    assert "async-generator" not in host.active_plugins


@pytest.mark.asyncio
async def test_closed_host_worker_wake_is_a_safe_noop() -> None:
    host = PluginHost(lambda: None)
    await host.start_background_workers()
    await host.close_and_wait()

    host.wake_background_workers()


@pytest.mark.asyncio
@pytest.mark.parametrize("rollback", [False, True], ids=["commit", "rollback"])
async def test_cancelled_worker_commit_holds_real_sqlite_lease_through_finalization(
    services: Any,
    monkeypatch: pytest.MonkeyPatch,
    rollback: bool,
) -> None:
    owner = UserRepo(services.db).create(
        username="worker-owner", role="user", email="worker@example.test"
    )
    host = PluginHost(lambda: services)
    transaction_body_finished = threading.Event()
    release_finalization = threading.Event()
    transaction_finalized = threading.Event()
    deactivate_entered = threading.Event()
    identity_loaded = asyncio.Event()
    worker_cancelled = asyncio.Event()

    async def worker(wake: asyncio.Event, facade: Any) -> None:
        assert wake.is_set()
        identity = await facade.read_user_identity(owner)
        identity_loaded.set()
        try:
            await facade.commit(
                [RecordWrite("worker", "ready", owner, {"ready": True}, 0)],
                identity_checks=[UserIdentityCheck(identity)],
            )
        except asyncio.CancelledError:
            worker_cancelled.set()
            raise

    def setup(facade: Any) -> None:
        facade.register_user_identity_consumer()

        async def registered_worker(wake: asyncio.Event) -> None:
            await worker(wake, facade)

        facade.register_background_worker("commit", registered_worker)

    facade = host.bind_context(_context("commit-worker", setup), "1")
    page = await facade.list_user_identities()
    assert [identity.user_id for identity in page.items] == [owner]
    original_transaction = services.db.transaction

    @contextmanager
    def controlled_transaction() -> Any:
        try:
            with original_transaction() as conn:
                yield conn
                transaction_body_finished.set()
                if not release_finalization.wait(timeout=5):
                    raise TimeoutError("transaction finalization release was not signaled")
                if rollback:
                    raise RuntimeError("force transaction rollback")
        finally:
            transaction_finalized.set()

    monkeypatch.setattr(services.db, "transaction", controlled_transaction)
    await host.start_background_workers()
    await asyncio.wait_for(identity_loaded.wait(), 2)
    worker_task = host._plugins["commit-worker"].background_workers["commit"].task
    assert worker_task is not None
    lifecycle_task: asyncio.Task[None] | None = None
    try:
        assert await asyncio.wait_for(asyncio.to_thread(transaction_body_finished.wait, 2), 3)
        worker_task.cancel()
        await asyncio.wait_for(worker_cancelled.wait(), 2)
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(worker_task, 2)
        assert not transaction_finalized.is_set()

        original_deactivate = host.deactivate

        def observed_deactivate(plugin_id: str) -> Any:
            deactivate_entered.set()
            return original_deactivate(plugin_id)

        monkeypatch.setattr(host, "deactivate", observed_deactivate)
        lifecycle_task = asyncio.create_task(host.deactivate_and_wait("commit-worker"))
        assert await asyncio.wait_for(asyncio.to_thread(deactivate_entered.wait, 2), 3)
        lease_acquired = host._operation_lock.acquire(blocking=False)
        if lease_acquired:
            host._operation_lock.release()
        assert not lease_acquired
        assert not transaction_finalized.is_set()
        assert not lifecycle_task.done()

        release_finalization.set()
        assert await asyncio.wait_for(asyncio.to_thread(transaction_finalized.wait, 2), 3)
        await asyncio.wait_for(lifecycle_task, 3)
        lifecycle_task = None

        record = services.plugin_record_repo.get("commit-worker", "worker", "ready")
        if rollback:
            assert record is None
        else:
            assert record is not None
            assert record.owner_user_id == owner

        with pytest.raises(PluginHostError) as exc:
            await facade.commit(
                [RecordWrite("worker", "stale", owner, {"stale": True}, 0)],
                identity_checks=[UserIdentityCheck(page.items[0])],
            )
        assert exc.value.code == "unavailable"
    finally:
        release_finalization.set()
        if transaction_body_finished.is_set() and not transaction_finalized.is_set():
            assert await asyncio.wait_for(asyncio.to_thread(transaction_finalized.wait, 2), 3)
        if lifecycle_task is not None:
            await asyncio.wait_for(asyncio.gather(lifecycle_task, return_exceptions=True), 3)
        if not worker_task.done():
            worker_task.cancel()
            await asyncio.wait_for(asyncio.gather(worker_task, return_exceptions=True), 3)
        await host.close_and_wait()
