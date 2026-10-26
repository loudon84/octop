from __future__ import annotations

import asyncio
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from octop_harness.plugins import PluginRegistry
from octop_harness.plugins.context import PluginContext
from octop_harness.plugins.manifest import PluginManifest
from pydantic import BaseModel

import octop.infra.agents.plugins.manager as manager_module
from octop.infra.agents.plugins.host import PluginHost, register_host_setup
from octop.infra.agents.plugins.host_contracts import (
    PluginActor,
    PluginHostError,
    PluginResource,
)
from octop.infra.agents.plugins.manager import PluginManager

_PLUGIN_FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "plugins" / "echo-tool"


class _Input(BaseModel):
    value: str


class _Output(BaseModel):
    value: str


@pytest.fixture(autouse=True)
def _reset_registry() -> Any:
    PluginRegistry.reset()
    yield
    PluginRegistry.reset()


async def test_deactivate_cancels_action_and_awaits_completion() -> None:
    host = PluginHost(lambda: None)
    facade = host.bind("lifecycle-plugin", "1")
    started = asyncio.Event()

    async def wait_handler(_actor: PluginActor, payload: _Input) -> _Output:
        started.set()
        await asyncio.Event().wait()
        return _Output(value=payload.value)

    facade.register_action("wait", _Input, _Output, wait_handler, "Wait", "authenticated")
    task = asyncio.create_task(facade.invoke_action("wait", PluginActor(1, False), {"value": "x"}))
    await started.wait()
    await host.deactivate_and_wait("lifecycle-plugin")
    with pytest.raises(PluginHostError):
        await task
    with pytest.raises(PluginHostError):
        host.facade("lifecycle-plugin")


async def test_stream_task_is_cancelled_and_iterator_is_closed() -> None:
    host = PluginHost(lambda: None)
    facade = host.bind("stream-plugin", "1")
    started = asyncio.Event()
    closed = asyncio.Event()

    async def stream(_actor: PluginActor, _token: str) -> PluginResource:
        async def body():
            try:
                started.set()
                await asyncio.Event().wait()
                yield b"unreachable"
            finally:
                closed.set()

        return PluginResource(body=body())

    facade.register_resource(stream)
    resource = await facade.invoke_resource(PluginActor(1, False), "opaque")

    async def consume() -> None:
        async for _chunk in facade.stream_resource(resource):
            pass

    task = asyncio.create_task(consume())
    await started.wait()
    await host.deactivate_and_wait("stream-plugin")
    assert task.cancelled()
    assert closed.is_set()


async def test_close_waits_for_an_operation_lock_without_blocking_event_loop() -> None:
    host = PluginHost(lambda: None)
    host.bind("lease-plugin", "1")
    entered = threading.Event()
    release = threading.Event()

    def hold_operation_lock() -> None:
        with host._operation_lock:
            entered.set()
            assert release.wait(timeout=5)

    worker = asyncio.get_running_loop().run_in_executor(None, hold_operation_lock)
    assert await asyncio.to_thread(entered.wait, 2)
    closer = asyncio.create_task(host.close_and_wait())
    heartbeat = asyncio.Event()
    asyncio.get_running_loop().call_soon(heartbeat.set)
    await asyncio.wait_for(heartbeat.wait(), timeout=0.2)
    assert not closer.done()
    release.set()
    await asyncio.wait_for(worker, timeout=2)
    await asyncio.wait_for(closer, timeout=2)
    with pytest.raises(PluginHostError):
        host.bind("later-plugin", "1")


def test_facade_service_proxy_is_bound_to_caller_and_provider_generations() -> None:
    host = PluginHost(lambda: None)
    host.bind("dependency", "1")
    provider = host.bind("provider", "1")

    class Service:
        def ping(self) -> str:
            return "pong"

    provider.register_service(1, Service, {"dependency": "1"})
    consumer = host.bind("consumer", "1")
    caller_bound = consumer.resolve_service("provider", 1)
    assert caller_bound.ping() == "pong"

    host.bind("consumer", "1")
    with pytest.raises(PluginHostError):
        caller_bound.ping()

    provider_bound = host.facade("consumer").resolve_service("provider", 1)
    assert provider_bound.ping() == "pong"
    replacement_provider = host.bind("provider", "1")
    with pytest.raises(PluginHostError):
        provider_bound.ping()

    replacement_provider.register_service(1, Service, {"dependency": "1"})
    dependency_bound = host.facade("consumer").resolve_service("provider", 1)
    assert dependency_bound.ping() == "pong"
    host.bind("dependency", "1")
    with pytest.raises(PluginHostError):
        dependency_bound.ping()


async def test_host_enforces_agent_owner_thread_and_resource_scope() -> None:
    agents = {
        "owned": SimpleNamespace(id="owned", user_id=7, is_shared=False),
        "shared": SimpleNamespace(id="shared", user_id=8, is_shared=True),
    }
    threads = {
        "mine": SimpleNamespace(id="mine", agent_id="owned", user_id=7),
        "other": SimpleNamespace(id="other", agent_id="owned", user_id=8),
    }

    class AgentRepo:
        def get(self, agent_id: str) -> Any:
            return agents.get(agent_id)

    class ThreadRepo:
        def get(self, thread_id: str) -> Any:
            return threads.get(thread_id)

    services = SimpleNamespace(agent_repo=AgentRepo(), thread_repo=ThreadRepo())
    host = PluginHost(lambda: services)
    facade = host.bind("scope-plugin", "1")

    async def action(_actor: PluginActor, payload: _Input) -> _Output:
        return _Output(value=payload.value)

    async def resource(_actor: PluginActor, _token: str) -> PluginResource:
        return PluginResource(body=b"ok")

    facade.register_action("owner", _Input, _Output, action, "Owner", "agent_owner")
    facade.register_resource(resource)
    owner = PluginActor(7, False, agent_id="owned", thread_id="mine")
    assert await facade.invoke_action("owner", owner, {"value": "ok"}) == {"value": "ok"}

    with pytest.raises(PluginHostError, match="denied"):
        await facade.invoke_action(
            "owner",
            PluginActor(7, False, agent_id="shared"),
            {"value": "shared agent is not owned"},
        )
    with pytest.raises(PluginHostError, match="denied"):
        await facade.invoke_action(
            "owner",
            PluginActor(7, False, agent_id="owned", thread_id="other"),
            {"value": "cross-user thread"},
        )
    with pytest.raises(PluginHostError, match="denied"):
        await facade.invoke_resource(
            PluginActor(7, False, agent_id="owned", thread_id="other"), "opaque"
        )


def test_failed_concurrent_host_callback_cannot_deactivate_new_generation() -> None:
    host = PluginHost(lambda: None)
    callback_entered = threading.Event()
    release_callback = threading.Event()
    callback_errors: list[BaseException] = []

    def failing_setup(_facade: Any) -> None:
        callback_entered.set()
        assert release_callback.wait(timeout=5)
        raise RuntimeError("failed old registration")

    old_context = PluginContext(
        manifest=PluginManifest(
            id="concurrent-plugin",
            version="1",
            name="Concurrent plugin",
            kind="skill",
            entry="main.py",
        ),
        source_path=Path("old-plugin"),
    )
    new_context = PluginContext(
        manifest=PluginManifest(
            id="concurrent-plugin",
            version="1",
            name="Concurrent plugin",
            kind="skill",
            entry="main.py",
        ),
        source_path=Path("new-plugin"),
    )
    register_host_setup(old_context, failing_setup)

    def bind_old() -> None:
        try:
            host.bind_context(old_context, "1")
        except BaseException as exc:
            callback_errors.append(exc)

    old_loader = threading.Thread(target=bind_old)
    old_loader.start()
    assert callback_entered.wait(timeout=2)
    new_facade = host.bind_context(new_context, "1")
    release_callback.set()
    old_loader.join(timeout=2)

    assert not old_loader.is_alive()
    assert callback_errors
    assert host.facade("concurrent-plugin").plugin_id == new_facade.plugin_id


def test_failed_manager_callback_preserves_new_host_and_registry_instance(
    tmp_path, monkeypatch
) -> None:
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "echo-tool"
    shutil.copytree(_PLUGIN_FIXTURE, plugin_dir)
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    old_plugin = manager_module._load_plugin_dir(_PLUGIN_FIXTURE, install_deps=False)
    new_plugin = manager_module._load_plugin_dir(_PLUGIN_FIXTURE, install_deps=False)
    PluginRegistry().clear()
    host = PluginHost(lambda: None)
    manager_module.register_host(host)
    mgr = PluginManager(plugins_dir=plugins_dir, config_path=config_path, host=host)
    callback_entered = threading.Event()
    release_callback = threading.Event()
    callback_errors: list[BaseException] = []
    load_results: list[Any] = []

    def failing_setup(_facade: Any) -> None:
        callback_entered.set()
        assert release_callback.wait(timeout=5)
        raise RuntimeError("old callback failed")

    register_host_setup(old_plugin.context, failing_setup)
    register_host_setup(new_plugin.context, lambda _facade: None)

    def fake_load(_path: Path, *, install_deps: bool) -> Any:
        del install_deps
        PluginRegistry().register(old_plugin)
        return old_plugin

    monkeypatch.setattr(manager_module, "_load_plugin_dir", fake_load)

    def load_old() -> None:
        try:
            load_results.append(
                asyncio.run(mgr.mutate(lambda: mgr.load_missing(install_deps=False)))
            )
        except BaseException as exc:
            callback_errors.append(exc)

    old_loader = threading.Thread(target=load_old)
    old_loader.start()
    assert callback_entered.wait(timeout=2)
    PluginRegistry().register(new_plugin)
    new_facade = host.bind_context(new_plugin.context, "2")
    release_callback.set()
    old_loader.join(timeout=2)

    assert not old_loader.is_alive()
    assert not callback_errors
    assert load_results == [[]]
    assert PluginRegistry().get("echo-tool") is new_plugin
    assert host.facade("echo-tool").plugin_id == new_facade.plugin_id
    host.close()
    manager_module.unregister_host(host)


def test_manager_binds_sdk_loaded_context_and_invalidates_each_lifecycle(
    tmp_path, monkeypatch
) -> None:
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "echo-tool"
    shutil.copytree(_PLUGIN_FIXTURE, plugin_dir)
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    loaded = manager_module._load_plugin_dir(_PLUGIN_FIXTURE, install_deps=False)
    PluginRegistry().clear()
    host = PluginHost(lambda: None)
    manager_module.register_host(host)
    contexts: list[str] = []

    async def ping(_actor: PluginActor, payload: _Input) -> _Output:
        return _Output(value=payload.value)

    def setup(facade: Any) -> None:
        contexts.append(facade.plugin_id)
        facade.register_action("ping", _Input, _Output, ping, "Ping", "authenticated")

    register_host_setup(loaded.context, setup)

    def fake_load(_path: Path, *, install_deps: bool) -> Any:
        del install_deps
        PluginRegistry().register(loaded)
        return loaded

    monkeypatch.setattr(manager_module, "_load_plugin_dir", fake_load)
    mgr = PluginManager(plugins_dir=plugins_dir, config_path=config_path, host=host)

    def run_mutation(operation: Any) -> Any:
        return asyncio.run(mgr.mutate(operation))

    assert [
        plugin.manifest.id
        for plugin in run_mutation(lambda: mgr.load_installed(install_deps=False))
    ] == ["echo-tool"]
    old = host.facade("echo-tool")
    assert old.descriptors()["actions"][0]["name"] == "ping"
    run_mutation(lambda: mgr.load_installed(install_deps=False))
    with pytest.raises(PluginHostError):
        old.descriptors()
    old = host.facade("echo-tool")

    disabled = run_mutation(lambda: mgr.set_enabled("echo-tool", False))
    assert disabled["enabled"] is False
    with pytest.raises(PluginHostError):
        old.descriptors()
    enabled = run_mutation(lambda: mgr.set_enabled("echo-tool", True))
    assert enabled["enabled"] is True
    assert host.facade("echo-tool").descriptors()["actions"][0]["name"] == "ping"

    replaced = run_mutation(lambda: mgr.install_path(_PLUGIN_FIXTURE, force=True))
    assert replaced.manifest.id == "echo-tool"
    assert host.facade("echo-tool").descriptors()["actions"][0]["name"] == "ping"
    run_mutation(lambda: mgr.uninstall("echo-tool"))
    with pytest.raises(PluginHostError):
        host.facade("echo-tool")
    shutil.copytree(_PLUGIN_FIXTURE, plugin_dir)
    assert [plugin.manifest.id for plugin in run_mutation(lambda: mgr.load_missing())] == [
        "echo-tool"
    ]
    assert host.facade("echo-tool").descriptors()["actions"][0]["name"] == "ping"
    assert contexts == ["echo-tool"] * 5
    host.close()
    manager_module.unregister_host(host)


def test_manager_callback_failure_unloads_registration(tmp_path, monkeypatch) -> None:
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "echo-tool"
    shutil.copytree(_PLUGIN_FIXTURE, plugin_dir)
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    loaded = manager_module._load_plugin_dir(_PLUGIN_FIXTURE, install_deps=False)
    PluginRegistry().clear()
    host = PluginHost(lambda: None)
    manager_module.register_host(host)

    def fail_setup(_facade: Any) -> None:
        raise RuntimeError("setup callback failed")

    register_host_setup(loaded.context, fail_setup)

    def fake_load(_path: Path, *, install_deps: bool) -> Any:
        del install_deps
        PluginRegistry().register(loaded)
        return loaded

    monkeypatch.setattr(manager_module, "_load_plugin_dir", fake_load)
    mgr = PluginManager(plugins_dir=plugins_dir, config_path=config_path, host=host)
    assert asyncio.run(mgr.mutate(lambda: mgr.load_missing(install_deps=False))) == []
    assert PluginRegistry().get("echo-tool") is None
    with pytest.raises(PluginHostError):
        host.facade("echo-tool")
    host.close()
    manager_module.unregister_host(host)
