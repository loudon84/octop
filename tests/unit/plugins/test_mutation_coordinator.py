from __future__ import annotations

import asyncio
import importlib
import shutil
import sqlite3
import sys
import textwrap
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest
from octop_harness.plugins import unload_plugin

from octop.config import OctopConfig
from octop.infra.agents.plugins.host import PluginHost, drain_plugin_tasks
from octop.infra.agents.plugins.host_contracts import PluginHostError
from octop.infra.agents.plugins.manager import (
    PluginManager,
    current_state,
    mutate,
    register_host,
    unregister_host,
    wait_for_mutations,
)
from octop.infra.server import OctopServer


def test_registered_runtime_host_rejects_unpermitted_sync_mutation_before_writes(tmp_path):
    host = PluginHost(lambda: None)
    register_host(host)
    try:
        manager = PluginManager(
            plugins_dir=tmp_path / "plugins", config_path=tmp_path / "config.json"
        )
        host_bound = PluginManager(
            plugins_dir=tmp_path / "host-bound",
            config_path=tmp_path / "host-config.json",
            host=host,
        )
        before = current_state()
        with pytest.raises(RuntimeError, match="async mutate"):
            manager.seed_bundled()
        with pytest.raises(RuntimeError, match="async mutate"):
            host_bound.load_missing()
        assert current_state() == before
        assert not (tmp_path / "config.json").exists()
        assert list((tmp_path / "plugins").iterdir()) == []
    finally:
        host.close()
        unregister_host(host)


@pytest.mark.asyncio
async def test_real_sdk_url_archive_path_mutation_rebinds_only_initiator(tmp_path, monkeypatch):
    import octop.infra.agents.plugins.manager as manager_module

    fixture = Path(__file__).resolve().parents[2] / "fixtures" / "plugins" / "echo-tool"
    archive = tmp_path / "echo-tool.zip"
    with ZipFile(archive, "w") as bundle:
        for path in fixture.rglob("*"):
            if path.is_file():
                bundle.write(path, path.relative_to(fixture.parent))
    host = PluginHost(lambda: None)
    other_host = PluginHost(lambda: None)
    register_host(host)
    register_host(other_host)
    manager = PluginManager(
        plugins_dir=tmp_path / "plugins", config_path=tmp_path / "config.json", host=host
    )

    def copy_archive(
        _url: str, filename: str | Path, *_args: object, **_kwargs: object
    ) -> tuple[str, None]:
        shutil.copyfile(archive, filename)
        return str(filename), None

    monkeypatch.setattr(manager_module.urllib.request, "urlretrieve", copy_archive)
    try:
        initial = await manager.mutate(lambda: manager.install_archive(archive))
        stale = host.facade("echo-tool")
        other_stale = other_host.bind_context(initial.context, str(initial.manifest.version))
        epoch = current_state().epoch
        replaced = await manager.mutate(
            lambda: manager.install_url("https://example.com/echo-tool.zip", force=True)
        )

        assert replaced is not initial
        assert current_state().epoch == epoch + 1
        assert host.active_plugins == {"echo-tool": str(replaced.manifest.version)}
        assert other_host.active_plugins == {}
        with pytest.raises(PluginHostError):
            stale.descriptors()
        with pytest.raises(PluginHostError):
            other_stale.descriptors()
    finally:
        host.close()
        other_host.close()
        unregister_host(host)
        unregister_host(other_host)
        unload_plugin("echo-tool")


@pytest.mark.asyncio
async def test_real_sdk_sequential_plugins_rebind_unchanged_initiator_survivor(tmp_path):
    from octop_harness.plugins import PluginRegistry

    from octop.infra.agents.plugins.host_contracts import PluginActor

    def make_plugin(plugin_id: str) -> Path:
        directory = tmp_path / "source" / plugin_id
        directory.mkdir(parents=True)
        (directory / "plugin.yaml").write_text(
            f"id: {plugin_id}\nversion: 1.0.0\nname: {plugin_id}\n"
            "description: Sequential SDK mutation test\nkind: tool\nentry: main.py\n",
            encoding="utf-8",
        )
        (directory / "main.py").write_text(
            textwrap.dedent(
                f"""
                from pydantic import BaseModel
                from octop.infra.agents.plugins.host import register_host_setup

                class Input(BaseModel):
                    value: str

                class Output(BaseModel):
                    value: str

                async def action(_actor, _payload):
                    return Output(value="{plugin_id}")

                def setup_host(facade):
                    facade.register_action(
                        "ping", Input, Output, action, "Ping", "authenticated"
                    )

                def setup(ctx):
                    register_host_setup(ctx, setup_host)
                """
            ),
            encoding="utf-8",
        )
        return directory

    host = PluginHost(lambda: None)
    other_host = PluginHost(lambda: None)
    register_host(host)
    register_host(other_host)
    manager = PluginManager(
        plugins_dir=tmp_path / "plugins", config_path=tmp_path / "config.json", host=host
    )
    try:
        survivor = await manager.mutate(lambda: manager.install_path(make_plugin("survivor")))
        old_facade = host.facade("survivor")
        other_old_facade = other_host.bind_context(survivor.context, str(survivor.manifest.version))

        await manager.mutate(lambda: manager.install_path(make_plugin("newcomer")))

        assert host.active_plugins == {"newcomer": "1.0.0", "survivor": "1.0.0"}
        assert other_host.active_plugins == {}
        with pytest.raises(PluginHostError):
            old_facade.descriptors()
        with pytest.raises(PluginHostError):
            other_old_facade.descriptors()
        assert await host.facade("survivor").invoke_action(
            "ping", PluginActor(1, False), {"value": "x"}
        ) == {"value": "survivor"}
        assert await host.facade("newcomer").invoke_action(
            "ping", PluginActor(1, False), {"value": "x"}
        ) == {"value": "newcomer"}

        disabled = await manager.mutate(lambda: manager.set_enabled("survivor", False))
        assert disabled["enabled"] is False
        with pytest.raises(PluginHostError):
            host.facade("survivor")
        assert await host.facade("newcomer").invoke_action(
            "ping", PluginActor(1, False), {"value": "x"}
        ) == {"value": "newcomer"}

        await manager.mutate(lambda: manager.uninstall("newcomer"))
        assert host.active_plugins == {}
        assert other_host.active_plugins == {}
        with pytest.raises(PluginHostError):
            host.facade("newcomer")
        assert PluginRegistry().get("survivor") is None
        assert PluginRegistry().get("newcomer") is None
    finally:
        for current_host in (host, other_host):
            host_tasks = current_host.close()
            await drain_plugin_tasks(host_tasks)
            unregister_host(current_host)
        for plugin_id in ("survivor", "newcomer"):
            unload_plugin(plugin_id)


@pytest.mark.asyncio
async def test_required_service_dependent_action_drains_before_disable_mutates_sdk(
    tmp_path, monkeypatch
):
    from octop_harness.plugins import PluginRegistry

    import octop.infra.agents.plugins.manager as manager_module
    from octop.infra.agents.plugins.host_contracts import PluginActor

    source = tmp_path / "source"
    provider_dir = source / "provider-a"
    dependency_dir = source / "required-b"
    provider_dir.mkdir(parents=True)
    dependency_dir.mkdir(parents=True)
    provider_dir.joinpath("plugin.yaml").write_text(
        "id: provider-a\nversion: 1.0.0\nname: Provider A\n"
        "description: Dependency drain test\nkind: tool\nentry: main.py\n",
        encoding="utf-8",
    )
    provider_dir.joinpath("main.py").write_text(
        textwrap.dedent(
            """
            import asyncio
            import threading
            from pydantic import BaseModel
            from octop.infra.agents.plugins.host import register_host_setup

            service_resolved = threading.Event()
            cleanup_started = threading.Event()
            cleanup_finished = threading.Event()
            allow_cleanup = threading.Event()

            class Input(BaseModel):
                value: str

            class Output(BaseModel):
                value: str

            def setup_host(facade):
                facade.register_service(1, object, {"required-b": "1.0.0"})

                async def action(_actor, payload):
                    facade.resolve_service("provider-a", 1)
                    service_resolved.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        await asyncio.to_thread(cleanup_started.set)
                        await asyncio.to_thread(allow_cleanup.wait)
                        cleanup_finished.set()
                    return Output(value=payload.value)

                facade.register_action("wait", Input, Output, action, "Wait", "authenticated")

            def setup(ctx):
                register_host_setup(ctx, setup_host)
            """
        ),
        encoding="utf-8",
    )
    dependency_dir.joinpath("plugin.yaml").write_text(
        "id: required-b\nversion: 1.0.0\nname: Required B\n"
        "description: Service dependency\nkind: tool\nentry: main.py\n",
        encoding="utf-8",
    )
    dependency_dir.joinpath("main.py").write_text("def setup(_ctx):\n    pass\n", encoding="utf-8")

    host = PluginHost(lambda: None)
    register_host(host)
    manager = PluginManager(
        plugins_dir=tmp_path / "plugins", config_path=tmp_path / "config.json", host=host
    )
    original_write_enabled = manager_module._write_global_plugin_enabled
    writes_after_cleanup: list[bool] = []

    def guard_sdk_mutation(config_path: Path, plugin_id: str, enabled: bool) -> None:
        if plugin_id == "required-b":
            module = importlib.import_module("harness_plugin_provider_a")
            writes_after_cleanup.append(module.cleanup_finished.is_set())
            assert module.cleanup_finished.is_set()
        original_write_enabled(config_path, plugin_id, enabled)

    monkeypatch.setattr(manager_module, "_write_global_plugin_enabled", guard_sdk_mutation)

    try:
        await manager.mutate(lambda: manager.install_path(provider_dir))
        await manager.mutate(lambda: manager.install_path(dependency_dir))
        facade = host.facade("provider-a")
        action_task = asyncio.create_task(
            facade.invoke_action("wait", PluginActor(1, False), {"value": "x"})
        )
        module = importlib.import_module("harness_plugin_provider_a")
        assert await asyncio.to_thread(module.service_resolved.wait, 2)

        disable_task = asyncio.create_task(
            manager.mutate(lambda: manager.set_enabled("required-b", False))
        )
        assert await asyncio.to_thread(module.cleanup_started.wait, 2)
        assert not disable_task.done()
        assert writes_after_cleanup == []
        module.allow_cleanup.set()

        disabled = await asyncio.wait_for(disable_task, 3)
        assert disabled["enabled"] is False
        assert writes_after_cleanup == [True]
        with pytest.raises(PluginHostError):
            await action_task
        assert host.active_plugins == {"provider-a": "1.0.0"}
        assert PluginRegistry().get("required-b") is None
    finally:
        module = sys.modules.get("harness_plugin_provider_a")
        if module is not None:
            module.allow_cleanup.set()
        host_tasks = host.close()
        await drain_plugin_tasks(host_tasks)
        unregister_host(host)
        unload_plugin("provider-a")
        unload_plugin("required-b")


@pytest.mark.asyncio
async def test_startup_seed_waits_for_old_file_reader_before_replacing_package(
    tmp_path, monkeypatch
):
    import octop.infra.agents.plugins.bundled as bundled_module

    bundled_root = tmp_path / "catalog"
    source = bundled_root / "echo-tool"
    installed = tmp_path / "plugins" / "echo-tool"
    source.mkdir(parents=True)
    installed.mkdir(parents=True)
    (source / "plugin.yaml").write_text("id: echo-tool\nversion: 2.0.0\n", encoding="utf-8")
    (source / "code.py").write_text("new", encoding="utf-8")
    (installed / "plugin.yaml").write_text("id: echo-tool\nversion: 1.0.0\n", encoding="utf-8")
    (installed / "code.py").write_text("old", encoding="utf-8")
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(bundled_module, "default_bundled_plugins_root", lambda: bundled_root)
    host = PluginHost(lambda: None)
    register_host(host)
    manager = PluginManager(plugins_dir=tmp_path / "plugins", config_path=config, host=host)
    context = SimpleNamespace(plugin_id="echo-tool")
    host.bind_context(context, "1.0.0")
    read_old_file: list[str] = []
    started = asyncio.Event()

    async def reader() -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            read_old_file.append((installed / "code.py").read_text(encoding="utf-8"))

    task = asyncio.create_task(reader())
    with host._lock:
        host._plugins["echo-tool"].tasks.add(task)
    try:
        await started.wait()
        assert await manager.mutate(manager.seed_bundled) == ["echo-tool"]
        assert read_old_file == ["old"]
        assert (installed / "code.py").read_text(encoding="utf-8") == "new"
    finally:
        host.close()
        unregister_host(host)


@pytest.mark.asyncio
async def test_start_failure_after_plugin_load_closes_and_unregisters_host(tmp_path, monkeypatch):
    import octop.infra.server as server_module
    from octop.infra.agents.plugins.manager import PluginManager as Manager

    monkeypatch.setattr(server_module, "ensure_nofile_limit", lambda: None)
    monkeypatch.setattr(
        server_module, "ensure_mobile_capabilities_probed", lambda _path: OctopConfig()
    )
    monkeypatch.setattr(OctopServer, "_setup_logging", lambda _self: None)

    def fail_after_plugin_load(_server: OctopServer) -> None:
        raise RuntimeError("injected after load")

    monkeypatch.setattr(OctopServer, "_apply_plugin_channels", fail_after_plugin_load)
    monkeypatch.setattr("octop.infra.connectors.gateway.cli_install.ensure_cli_path", lambda: None)
    monkeypatch.setattr("octop.infra.utils.env_file.apply_env_file", lambda _path: None)

    server = OctopServer(tmp_path / "octop-home")
    with pytest.raises(RuntimeError, match="injected after load"):
        await server.start()

    assert server.plugin_host is None
    assert server.plugin_manager is None
    assert not current_state().in_progress
    standalone = Manager(
        plugins_dir=tmp_path / "standalone-plugins",
        config_path=tmp_path / "standalone-config.json",
    )
    assert standalone.load_missing() == []


@pytest.mark.asyncio
async def test_mutation_reservation_survives_request_cancellation_until_worker_finishes(tmp_path):
    executor = ThreadPoolExecutor(max_workers=2)
    asyncio.get_running_loop().set_default_executor(executor)
    host = PluginHost(lambda: None)
    register_host(host)
    started = threading.Event()
    release = threading.Event()
    cancelled_waiter_started = threading.Event()
    third_started = threading.Event()

    def first() -> None:
        started.set()
        assert release.wait(5)

    def second() -> None:
        cancelled_waiter_started.set()

    def third() -> None:
        third_started.set()

    try:
        first_request = asyncio.create_task(mutate(first))
        assert await asyncio.to_thread(started.wait, 2)
        assert current_state().in_progress
        first_request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first_request

        second_request = asyncio.create_task(mutate(second))
        third_request = asyncio.create_task(mutate(third))
        await asyncio.sleep(0.05)
        assert not cancelled_waiter_started.is_set()
        assert not third_started.is_set()
        second_request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second_request
        assert current_state().in_progress
        release.set()
        await asyncio.wait_for(third_request, 2)
        assert not cancelled_waiter_started.is_set()
        assert third_started.is_set()
        assert not current_state().in_progress
    finally:
        release.set()
        await asyncio.wait_for(wait_for_mutations(), 2)
        host.close()
        unregister_host(host)
        executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_cross_loop_cancel_at_reservation_grant_transfers_owner_and_wakes_idle_waiter(
    monkeypatch,
):
    import octop.infra.agents.plugins.manager as manager_module

    owner_started = threading.Event()
    owner_release = threading.Event()
    second_started = threading.Event()
    grant_claimed = threading.Event()
    continue_grant = threading.Event()
    cancel_delivered = threading.Event()
    loop_ready = threading.Event()
    foreign_loop = asyncio.new_event_loop()

    def run_foreign_loop() -> None:
        asyncio.set_event_loop(foreign_loop)
        loop_ready.set()
        foreign_loop.run_forever()
        foreign_loop.close()

    loop_thread = threading.Thread(target=run_foreign_loop)
    loop_thread.start()
    assert await asyncio.to_thread(loop_ready.wait, 2)
    host = PluginHost(lambda: None)
    register_host(host)

    def owner_operation() -> None:
        owner_started.set()
        assert owner_release.wait(5)

    def second_operation() -> None:
        second_started.set()

    def canceled_operation() -> None:
        pytest.fail("a canceled reservation waiter must not run its operation")

    async def waiting_operation() -> None:
        await mutate(canceled_operation)

    async def launch_waiter() -> asyncio.Task[None]:
        return asyncio.create_task(waiting_operation())

    owner_task = asyncio.create_task(mutate(owner_operation))
    assert await asyncio.to_thread(owner_started.wait, 2)
    launch_future = asyncio.run_coroutine_threadsafe(launch_waiter(), foreign_loop)
    foreign_waiter = await asyncio.to_thread(launch_future.result, 2)
    next_task = asyncio.create_task(mutate(second_operation))
    idle_task = asyncio.create_task(wait_for_mutations())

    async def wait_for_queued_waiters() -> None:
        for _ in range(200):
            with manager_module._coordinator_lock:
                if (
                    len(manager_module._coordinator_waiters) == 2
                    and len(manager_module._coordinator_idle_waiters) == 1
                ):
                    return
            await asyncio.sleep(0.005)
        pytest.fail("reservation and idle waiters were not queued")

    await wait_for_queued_waiters()
    with manager_module._coordinator_lock:
        canceled_future = manager_module._coordinator_waiters[0]
    real_notify = manager_module._notify_reservation_waiter

    def pause_after_atomic_claim(waiter) -> bool:
        if waiter is canceled_future:
            assert waiter.set_running_or_notify_cancel()
            grant_claimed.set()
            assert continue_grant.wait(5)
            waiter.set_result(None)
            return True
        return real_notify(waiter)

    monkeypatch.setattr(manager_module, "_notify_reservation_waiter", pause_after_atomic_claim)

    def cancel_during_grant() -> None:
        assert grant_claimed.wait(5)

        def cancel_waiter() -> None:
            foreign_waiter.cancel()
            cancel_delivered.set()

        foreign_loop.call_soon_threadsafe(cancel_waiter)
        assert cancel_delivered.wait(2)
        continue_grant.set()

    cancel_thread = threading.Thread(target=cancel_during_grant)
    cancel_thread.start()
    owner_release.set()

    try:
        await asyncio.wait_for(owner_task, 3)
        await asyncio.wait_for(next_task, 3)
        await asyncio.wait_for(idle_task, 3)
        assert second_started.is_set()
        assert foreign_waiter.cancelled()
        assert not current_state().in_progress
    finally:
        owner_release.set()
        continue_grant.set()
        cancel_thread.join(timeout=2)
        if foreign_loop.is_running():
            foreign_loop.call_soon_threadsafe(foreign_loop.stop)
        loop_thread.join(timeout=2)
        if not host.is_closed:
            host.close()
        if not current_state().in_progress:
            unregister_host(host)


@pytest.mark.asyncio
async def test_plugin_action_cannot_mutate_its_own_generation():
    host = PluginHost(lambda: None)
    register_host(host)
    host.bind("initiating-plugin", "1")
    called = False

    def operation() -> None:
        nonlocal called
        called = True

    async def initiate() -> None:
        with pytest.raises(RuntimeError, match="cannot mutate its own"):
            await mutate(operation)

    task = asyncio.create_task(initiate())
    with host._lock:
        host._plugins["initiating-plugin"].tasks.add(task)
    try:
        await asyncio.wait_for(task, 2)
        assert not called
        assert host.active_plugins == {"initiating-plugin": "1"}
    finally:
        host.close()
        unregister_host(host)


@pytest.mark.asyncio
async def test_two_hosts_preserve_real_sqlite_commit_before_registry_mutation(tmp_path):
    database = tmp_path / "commit.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE writes (value TEXT NOT NULL)")
    host_a = PluginHost(lambda: None)
    host_b = PluginHost(lambda: None)
    register_host(host_a)
    register_host(host_b)
    host_a.bind("commit-plugin", "1")
    host_b.bind("other-plugin", "1")
    with host_a._lock:
        generation = host_a._plugins["commit-plugin"].generation
    lease_entered = threading.Event()
    allow_commit = threading.Event()
    committed = threading.Event()
    mutation_entered = threading.Event()
    loop = asyncio.get_running_loop()

    def commit_transaction() -> None:
        with (
            sqlite3.connect(database, timeout=5) as connection,
            host_a._operation_lease("commit-plugin", generation),
        ):
            connection.execute("INSERT INTO writes (value) VALUES (?)", ("committed",))
            lease_entered.set()
            assert allow_commit.wait(5)
            connection.commit()
            committed.set()

    commit_future = loop.run_in_executor(None, commit_transaction)
    assert await asyncio.to_thread(lease_entered.wait, 2)

    def mutate_registry() -> None:
        assert committed.is_set()
        assert host_a.active_plugins == {}
        assert host_b.active_plugins == {}
        mutation_entered.set()

    mutation = asyncio.create_task(mutate(mutate_registry))
    await asyncio.sleep(0.05)
    assert current_state().in_progress
    assert not mutation_entered.is_set()
    assert not committed.is_set()
    allow_commit.set()
    await asyncio.wait_for(commit_future, 2)
    await asyncio.wait_for(mutation, 2)
    assert mutation_entered.is_set()
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT value FROM writes").fetchone() == ("committed",)
    with pytest.raises(PluginHostError), host_a._operation_lease("commit-plugin", generation):
        pytest.fail("stale generation entered an operation after invalidation")
    host_a.close()
    host_b.close()
    unregister_host(host_a)
    unregister_host(host_b)


@pytest.mark.asyncio
async def test_drain_plugin_tasks_waits_for_task_on_another_running_loop():
    task_ready = threading.Event()
    allow_finish = threading.Event()
    finally_progress = threading.Event()
    loop_ready = threading.Event()
    loop_holder: list[asyncio.AbstractEventLoop] = []
    task_holder: list[asyncio.Task[None]] = []

    def run_loop() -> None:
        loop = asyncio.new_event_loop()
        loop_holder.append(loop)
        asyncio.set_event_loop(loop)

        async def work() -> None:
            task_ready.set()
            try:
                await asyncio.to_thread(allow_finish.wait)
            finally:
                await asyncio.to_thread(finally_progress.set)

        task_holder.append(loop.create_task(work()))
        loop_ready.set()
        loop.run_forever()
        loop.close()

    thread = threading.Thread(target=run_loop)
    thread.start()
    assert await asyncio.to_thread(task_ready.wait, 2)
    assert loop_ready.is_set()
    drain = asyncio.create_task(drain_plugin_tasks(tuple(task_holder)))
    await asyncio.sleep(0.05)
    assert not drain.done()
    heartbeat = asyncio.Event()
    asyncio.get_running_loop().call_soon(heartbeat.set)
    await asyncio.wait_for(heartbeat.wait(), 1)
    allow_finish.set()
    await asyncio.wait_for(drain, 2)
    assert finally_progress.is_set()
    loop_holder[0].call_soon_threadsafe(loop_holder[0].stop)
    thread.join(timeout=2)
    assert not thread.is_alive()
    assert loop_holder[0].is_closed()


@pytest.mark.asyncio
async def test_two_hosts_drain_action_and_resource_cleanup_before_sdk_force_replace(
    tmp_path, monkeypatch
):
    from octop_harness.plugins import PluginRegistry

    import octop.infra.agents.plugins.manager as manager_module
    from octop.infra.agents.plugins.host_contracts import PluginActor

    source = tmp_path / "source" / "drain-probe"
    source.mkdir(parents=True)
    (source / "plugin.yaml").write_text(
        "id: drain-probe\nversion: 1.0.0\nname: Drain Probe\n"
        "description: Cross-loop cleanup test\nkind: tool\nentry: main.py\n",
        encoding="utf-8",
    )
    (source / "helper.py").write_text(
        "from pathlib import Path\n"
        "def read_marker():\n"
        "    return Path(__file__).with_name('marker.txt').read_text(encoding='utf-8')\n",
        encoding="utf-8",
    )
    (source / "marker.txt").write_text("old-package", encoding="utf-8")
    (source / "main.py").write_text(
        textwrap.dedent(
            """
            import asyncio
            import importlib
            from pydantic import BaseModel
            from octop.infra.agents.plugins.host import register_host_setup
            from octop.infra.agents.plugins.host_contracts import PluginResource

            started = {"action": __import__("threading").Event(),
                       "resource": __import__("threading").Event()}
            observed = []

            class Input(BaseModel):
                value: str

            class Output(BaseModel):
                value: str

            async def _cleanup(kind):
                def read_old_package():
                    helper = importlib.import_module(".helper", package=__package__)
                    return helper.read_marker()
                marker = await asyncio.to_thread(read_old_package)
                observed.append((kind, marker))

            async def action(_actor, payload):
                started["action"].set()
                try:
                    await asyncio.Event().wait()
                finally:
                    await _cleanup("action")
                return Output(value=payload.value)

            async def resource(_actor, _token):
                async def body():
                    started["resource"].set()
                    try:
                        await asyncio.Event().wait()
                        yield b"unreachable"
                    finally:
                        await _cleanup("resource-aclose")
                return PluginResource(body=body())

            def _host_setup(facade):
                facade.register_action(
                    "wait", Input, Output, action, "Wait for cancellation", "authenticated"
                )
                facade.register_resource(resource)

            def setup(ctx):
                register_host_setup(ctx, _host_setup)
            """
        ),
        encoding="utf-8",
    )

    plugin_id = "drain-probe"
    module_name = "harness_plugin_drain_probe"
    installed = tmp_path / "plugins" / plugin_id
    host_action = PluginHost(lambda: None)
    host_resource = PluginHost(lambda: None)
    register_host(host_action)
    register_host(host_resource)
    manager = PluginManager(
        plugins_dir=tmp_path / "plugins",
        config_path=tmp_path / "config.json",
        host=host_action,
    )
    loops: list[asyncio.AbstractEventLoop] = []
    loop_threads: list[threading.Thread] = []
    loop_ready: list[threading.Event] = []
    observed_at_replace: list[tuple[str, str]] = []
    real_rmtree = shutil.rmtree

    def guarded_rmtree(path, *args, **kwargs):
        if Path(path) == installed:
            old_module = importlib.import_module(module_name)
            observed_at_replace.extend(old_module.observed)
            assert sorted(old_module.observed) == [
                ("action", "old-package"),
                ("resource-aclose", "old-package"),
            ]
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(manager_module.shutil, "rmtree", guarded_rmtree)

    def start_loop() -> tuple[asyncio.AbstractEventLoop, threading.Event]:
        ready = threading.Event()
        result: list[asyncio.AbstractEventLoop] = []

        def run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            result.append(loop)
            ready.set()
            loop.run_forever()
            loop.close()

        thread = threading.Thread(target=run)
        thread.start()
        assert ready.wait(2)
        loops.append(result[0])
        loop_threads.append(thread)
        loop_ready.append(ready)
        return result[0], ready

    try:
        loaded = await manager.mutate(lambda: manager.install_path(source))
        old_module = importlib.import_module(module_name)
        facade_action = host_action.facade(plugin_id)
        facade_resource = host_resource.bind_context(loaded.context, "1.0.0")
        loop_action, _ = start_loop()
        loop_resource, _ = start_loop()

        async def run_action() -> None:
            with suppress(PluginHostError):
                await facade_action.invoke_action(
                    "wait", PluginActor(1, False), {"value": "unused"}
                )

        async def run_resource() -> None:
            resource = await facade_resource.invoke_resource(PluginActor(1, False), "opaque")
            async for _chunk in facade_resource.stream_resource(resource):
                pass

        action_future = asyncio.run_coroutine_threadsafe(run_action(), loop_action)
        resource_future = asyncio.run_coroutine_threadsafe(run_resource(), loop_resource)
        assert await asyncio.to_thread(old_module.started["action"].wait, 2)
        assert await asyncio.to_thread(old_module.started["resource"].wait, 2)
        (source / "marker.txt").write_text("new-package", encoding="utf-8")

        replaced = await asyncio.wait_for(
            manager.mutate(lambda: manager.install_path(source, force=True)), 3
        )
        assert replaced.manifest.version == "1.0.0"
        assert sorted(observed_at_replace) == [
            ("action", "old-package"),
            ("resource-aclose", "old-package"),
        ]
        assert (installed / "marker.txt").read_text(encoding="utf-8") == "new-package"
        assert action_future.done()
        assert resource_future.done()
        assert PluginRegistry().get(plugin_id) is replaced
    finally:
        for loop in loops:
            if loop.is_running():
                loop.call_soon_threadsafe(loop.stop)
        for thread in loop_threads:
            thread.join(timeout=2)
        for host in (host_action, host_resource):
            host.close()
            unregister_host(host)
        if not current_state().in_progress:
            from octop_harness.plugins import unload_plugin

            unload_plugin(plugin_id)


@pytest.mark.asyncio
async def test_pending_task_on_stopped_loop_fails_mutation_before_registry_operation(tmp_path):
    from pydantic import BaseModel

    from octop.infra.agents.plugins.host_contracts import PluginActor

    loop = asyncio.new_event_loop()
    loop_ready = threading.Event()
    stop_loop = threading.Event()
    resume_loop = threading.Event()
    exit_loop = threading.Event()
    action_started = threading.Event()
    cleanup_started = threading.Event()
    allow_cleanup = threading.Event()
    cleanup_finished = threading.Event()
    self_mutation_rejected = threading.Event()

    def run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop_ready.set()
        while True:
            loop.run_forever()
            if exit_loop.is_set():
                break
            stop_loop.set()
            assert resume_loop.wait(10)
            resume_loop.clear()
        loop.close()

    thread = threading.Thread(target=run_loop)
    thread.start()
    assert await asyncio.to_thread(loop_ready.wait, 2)

    class Input(BaseModel):
        value: str

    class Output(BaseModel):
        value: str

    host = PluginHost(lambda: None)
    register_host(host)
    registered = True
    facade = host.bind("stopped-loop-plugin", "1")

    async def action(_actor, _payload: Input) -> Output:
        action_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            with pytest.raises(RuntimeError, match="cannot mutate its own Registry"):
                await mutate(lambda: pytest.fail("retired action must not mutate"))
            self_mutation_rejected.set()
            await asyncio.to_thread(cleanup_started.set)
            await asyncio.to_thread(allow_cleanup.wait)
            cleanup_finished.set()
        return Output(value="done")

    facade.register_action("wait", Input, Output, action, "Wait", "authenticated")
    action_future = asyncio.run_coroutine_threadsafe(
        facade.invoke_action("wait", PluginActor(1, False), {"value": "x"}), loop
    )
    assert await asyncio.to_thread(action_started.wait, 2)
    entered = False

    def operation() -> None:
        nonlocal entered
        entered = True

    try:
        loop.call_soon_threadsafe(loop.stop)
        assert await asyncio.to_thread(stop_loop.wait, 2)
        with pytest.raises(PluginHostError):
            await mutate(operation)
        assert not entered
        assert not current_state().in_progress
        assert host.active_plugins == {}
        assert not action_future.done()

        with pytest.raises(PluginHostError):
            await mutate(operation)
        assert not entered
        assert not action_future.done()

        host.close()
        with pytest.raises(RuntimeError, match="pending plugin cleanup"):
            unregister_host(host)

        resume_loop.set()
        assert await asyncio.to_thread(self_mutation_rejected.wait, 2)
        assert await asyncio.to_thread(cleanup_started.wait, 2)
        assert not cleanup_finished.is_set()
        allow_cleanup.set()
        assert await asyncio.to_thread(cleanup_finished.wait, 2)
        with pytest.raises(PluginHostError):
            await asyncio.wrap_future(action_future)
        assert await mutate(operation) is None
        assert entered
        unregister_host(host)
        registered = False
    finally:
        allow_cleanup.set()
        exit_loop.set()
        resume_loop.set()
        if not loop.is_closed():
            loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        if not host.is_closed:
            host.close()
        if registered and not current_state().in_progress:
            unregister_host(host)


@pytest.mark.asyncio
async def test_action_finally_cannot_queue_mutation_while_owner_drains_it():
    from pydantic import BaseModel

    from octop.infra.agents.plugins.host_contracts import PluginActor

    host = PluginHost(lambda: None)
    register_host(host)
    started = asyncio.Event()
    self_mutation_rejected = asyncio.Event()
    operation_ran = False
    facade = host.bind("draining-self-mutation", "1")

    class Input(BaseModel):
        value: str

    class Output(BaseModel):
        value: str

    async def action(_actor, _payload: Input) -> Output:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            with pytest.raises(RuntimeError, match="cannot mutate its own Registry"):
                await mutate(lambda: pytest.fail("drained action must not queue mutation"))
            self_mutation_rejected.set()
        return Output(value="done")

    facade.register_action("wait", Input, Output, action, "Wait", "authenticated")
    action_task = asyncio.create_task(
        facade.invoke_action("wait", PluginActor(1, False), {"value": "x"})
    )
    try:
        await started.wait()

        def operation() -> None:
            nonlocal operation_ran
            operation_ran = True

        await asyncio.wait_for(mutate(operation), 3)
        assert self_mutation_rejected.is_set()
        assert action_task.done()
        await asyncio.gather(action_task, return_exceptions=True)
        assert operation_ran
    finally:
        if not action_task.done():
            action_task.cancel()
            await asyncio.gather(action_task, return_exceptions=True)
        host.close()
        unregister_host(host)


@pytest.mark.asyncio
async def test_host_owns_retired_action_before_coordinator_retains_it(monkeypatch):
    from pydantic import BaseModel

    from octop.infra.agents.plugins.host_contracts import PluginActor

    host = PluginHost(lambda: None)
    register_host(host)
    facade = host.bind("retired-ownership-window", "1")
    action_started = asyncio.Event()
    self_mutation_rejected = asyncio.Event()
    coordinator_retain_window = threading.Event()
    allow_deactivate_return = threading.Event()

    class Input(BaseModel):
        value: str

    class Output(BaseModel):
        value: str

    async def action(_actor, _payload: Input) -> Output:
        action_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            with pytest.raises(RuntimeError, match="cannot mutate its own Registry"):
                await mutate(lambda: pytest.fail("retired action must not queue mutation"))
            self_mutation_rejected.set()
        return Output(value="done")

    facade.register_action("wait", Input, Output, action, "Wait", "authenticated")
    action_task = asyncio.create_task(
        facade.invoke_action("wait", PluginActor(1, False), {"value": "x"})
    )
    original_deactivate = host.deactivate

    def pause_after_state_pop(plugin_id: str):
        tasks = original_deactivate(plugin_id)
        coordinator_retain_window.set()
        assert allow_deactivate_return.wait(3)
        return tasks

    monkeypatch.setattr(host, "deactivate", pause_after_state_pop)
    try:
        await action_started.wait()
        owner = asyncio.create_task(mutate(lambda: None))
        assert await asyncio.to_thread(coordinator_retain_window.wait, 2)
        # Deactivate has popped the state and requested cancellation, but its worker
        # has not returned to _retain_retired_tasks yet.
        await asyncio.wait_for(self_mutation_rejected.wait(), 2)
        assert not owner.done()
        allow_deactivate_return.set()
        await asyncio.wait_for(owner, 3)
        assert action_task.done()
        await asyncio.gather(action_task, return_exceptions=True)
    finally:
        allow_deactivate_return.set()
        if not action_task.done():
            action_task.cancel()
            await asyncio.gather(action_task, return_exceptions=True)
        host.close()
        unregister_host(host)
