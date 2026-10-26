"""tests/integration/test_server_lifecycle.py"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path

import pytest
from octop_harness.plugins.context import PluginContext
from octop_harness.plugins.manifest import PluginManifest

from octop.infra.agents.plugins.host import register_host_setup
from octop.infra.server import OctopServer
from tests.support.app import ensure_control_plane_bound


async def _bind_default_sqlite(srv: OctopServer) -> None:
    await ensure_control_plane_bound(srv)


@pytest.fixture
async def server(tmp_octop_home: Path):
    srv = OctopServer(home=tmp_octop_home)
    await srv.start()
    await _bind_default_sqlite(srv)
    yield srv
    await srv.stop()


async def test_start_defers_db_until_bind(tmp_octop_home: Path):
    srv = OctopServer(home=tmp_octop_home)
    await srv.start()
    try:
        assert tmp_octop_home.is_dir()
        assert (tmp_octop_home / "config.json").exists()
        assert not (tmp_octop_home / "octop.db").exists()
        assert srv.database_bound is False
        assert srv.services is None
        await _bind_default_sqlite(srv)
        assert (tmp_octop_home / "octop.db").exists()
        assert srv.database_bound is True
    finally:
        await srv.stop()


async def test_deferred_boot_starts_prebound_worker_only_after_app_runtime_exists(
    tmp_octop_home: Path,
) -> None:
    srv = OctopServer(home=tmp_octop_home)
    await srv.start()
    try:
        assert srv.plugin_host is not None
        started = asyncio.Event()
        saw_runtime: list[bool] = []

        async def worker(wake: asyncio.Event) -> None:
            assert wake.is_set()
            saw_runtime.append(srv.database_bound and srv.app_runtime is not None)
            started.set()
            await asyncio.Event().wait()

        context = PluginContext(
            manifest=PluginManifest("deferred-worker", "1", "Deferred", "skill", "main.py"),
            source_path=Path("deferred-worker"),
        )
        register_host_setup(
            context,
            lambda facade: facade.register_background_worker("reconcile", worker),
        )
        srv.plugin_host.bind_context(context, "1")
        assert not started.is_set()
        assert srv.app_runtime is None

        await _bind_default_sqlite(srv)
        await asyncio.wait_for(started.wait(), timeout=2)
        assert saw_runtime == [True]
    finally:
        await srv.stop()


async def test_start_creates_root_and_db(server: OctopServer, tmp_octop_home: Path):
    assert tmp_octop_home.is_dir()
    assert (tmp_octop_home / "octop.db").exists()
    assert (tmp_octop_home / "config.json").exists()


async def test_jwt_secret_seeded(server: OctopServer):
    assert server.services is not None
    secret = server.services.secret_repo.get("jwt")
    assert secret is not None
    assert len(secret) >= 32


async def test_user_manager_loaded(server: OctopServer):
    assert server.user_manager is not None
    assert server.user_manager.count() == 0


async def test_boot_then_stop_idempotent(tmp_octop_home: Path):
    srv = OctopServer(home=tmp_octop_home)
    await srv.start()
    await _bind_default_sqlite(srv)
    await srv.stop()
    await srv.stop()  # idempotent


async def test_runtime_workers_start_after_binding_wake_without_waiting_and_drain_on_stop(
    server: OctopServer,
) -> None:
    assert server.plugin_host is not None
    assert server.user_manager is not None
    owner_thread = threading.get_ident()
    started: asyncio.Future[int] = asyncio.get_running_loop().create_future()
    finalizing = asyncio.Event()
    release_finally = asyncio.Event()

    async def worker(wake: asyncio.Event) -> None:
        assert wake.is_set()
        if not started.done():
            started.set_result(threading.get_ident())
        try:
            await asyncio.Event().wait()
        finally:
            finalizing.set()
            await release_finally.wait()

    context = PluginContext(
        manifest=PluginManifest("server-worker", "1", "Server worker", "skill", "main.py"),
        source_path=Path("server-worker"),
    )
    register_host_setup(
        context,
        lambda facade: facade.register_background_worker("reconcile", worker),
    )
    await asyncio.get_running_loop().run_in_executor(
        None, server.plugin_host.bind_context, context, "1"
    )
    assert await asyncio.wait_for(started, timeout=2) == owner_thread

    created = await asyncio.wait_for(
        server.user_manager.create(
            username="worker-notification",
            password="TestPass12",
            role="user",
        ),
        timeout=2,
    )
    assert created.username == "worker-notification"
    assert not finalizing.is_set()

    stopping = asyncio.create_task(server.stop())
    await asyncio.wait_for(finalizing.wait(), timeout=2)
    assert not stopping.done()
    release_finally.set()
    await asyncio.wait_for(stopping, timeout=2)
    await server.stop()
