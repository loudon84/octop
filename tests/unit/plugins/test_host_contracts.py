from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from octop_harness.plugins.context import PluginContext
from octop_harness.plugins.manifest import PluginManifest
from pydantic import BaseModel

from octop.infra.agents.plugins.host import (
    PluginHost,
    PluginHostError,
    host_setup_callback,
    register_host_setup,
)
from octop.infra.agents.plugins.host_contracts import (
    PluginActor,
)
from octop.infra.agents.plugins.host_contracts import (
    PluginHostError as ContractError,
)


def test_plugin_actor_is_immutable_and_rejects_invalid_user() -> None:
    actor = PluginActor(user_id=4, is_admin=False, permissions=frozenset({"read"}))
    assert actor.user_id == 4
    with pytest.raises((AttributeError, TypeError)):
        actor.user_id = 9  # type: ignore[misc]
    with pytest.raises(ValueError):
        PluginActor(user_id=0, is_admin=False)
    with pytest.raises(ValueError):
        PluginActor(user_id=True, is_admin=False)  # type: ignore[arg-type]


def test_plugin_host_errors_expose_only_fixed_codes() -> None:
    error = ContractError("denied", "secret-bearing detail")
    assert error.code == "denied"
    assert "secret-bearing detail" not in str(error)
    errors = [
        ContractError(code)
        for code in ("unavailable", "conflict", "denied", "invalid", "not_found")
    ]
    assert {item.code for item in errors} == {
        "unavailable",
        "conflict",
        "denied",
        "invalid",
        "not_found",
    }


def test_register_host_setup_keeps_callback_on_context_and_plain_context_works() -> None:
    context = type("Context", (), {})()

    def callback(_facade: Any) -> None:
        return None

    register_host_setup(context, callback)
    assert host_setup_callback(context) is callback
    assert host_setup_callback(object()) is None


def test_setup_callback_receives_bound_facade_and_failure_cleans_generation() -> None:
    host = PluginHost(lambda: None)
    plain_context = type("Context", (), {"plugin_id": "plain-plugin"})()
    assert host.bind_context(plain_context, "1.0.0").plugin_id == "plain-plugin"

    context = PluginContext(
        manifest=PluginManifest(
            id="demo-plugin", version="1.0.0", name="Demo", kind="skill", entry="main.py"
        ),
        source_path=Path("demo-plugin"),
    )
    seen: list[str] = []
    register_host_setup(context, lambda facade: seen.append(facade.plugin_id))
    facade = host.bind_context(context, "1.0.0")
    assert seen == ["demo-plugin"]
    assert facade.plugin_id == "demo-plugin"

    broken = PluginContext(
        manifest=PluginManifest(
            id="broken-plugin", version="1.0.0", name="Broken", kind="skill", entry="main.py"
        ),
        source_path=Path("broken-plugin"),
    )
    register_host_setup(broken, lambda _facade: (_ for _ in ()).throw(RuntimeError()))
    with pytest.raises(RuntimeError):
        host.bind_context(broken, "1.0.0")
    assert "broken-plugin" not in host.active_plugins


def test_reserved_plugin_id_and_namespace_are_rejected() -> None:
    host = PluginHost(lambda: None)
    with pytest.raises(PluginHostError) as exc:
        host.bind("@octop.required", "1.0")
    assert exc.value.code == "invalid"
    facade = host.bind("normal-plugin", "1.0")
    with pytest.raises(PluginHostError) as exc:
        facade.register_action("../../secret", BaseModel, BaseModel, _action, "x", "authenticated")
    assert exc.value.code == "invalid"


async def _action(_actor: PluginActor, payload: BaseModel) -> BaseModel:
    return payload


def test_deactivate_rebind_close_and_separate_hosts_invalidate_facades() -> None:
    first = PluginHost(lambda: None)
    second = PluginHost(lambda: None)
    old = first.bind("demo-plugin", "1")
    isolated = second.bind("demo-plugin", "1")
    new = first.bind("demo-plugin", "2")
    with pytest.raises(PluginHostError):
        old.register_resource(_resource)
    assert isolated.plugin_id == "demo-plugin"
    first.deactivate("demo-plugin")
    with pytest.raises(PluginHostError):
        new.register_resource(_resource)
    second.close()
    with pytest.raises(PluginHostError):
        isolated.register_resource(_resource)


async def _resource(_actor: PluginActor, _token: str) -> Any:
    return None


def test_service_resolution_checks_versions_and_memoizes_factory_per_generation() -> None:
    host = PluginHost(lambda: None)
    host.bind("demo-plugin", "1")
    host.bind("dependency", "2")
    calls: list[int] = []

    class Service:
        def ping(self) -> str:
            return "pong"

    facade = host.facade("demo-plugin")
    facade.register_service(1, lambda: calls.append(1) or Service(), {"dependency": "2"})
    first = host.resolve_service("demo-plugin", 1)
    second = host.resolve_service("demo-plugin", 1)
    assert first.ping() == second.ping() == "pong"
    assert calls == [1]

    host.bind("dependency", "3")
    with pytest.raises(PluginHostError) as exc:
        host.resolve_service("demo-plugin", 1)
    assert exc.value.code == "unavailable"
    with pytest.raises(PluginHostError):
        first.ping()
    host.bind("dependency", "2")
    with pytest.raises(PluginHostError):
        first.ping()
    host.deactivate("dependency")

    replacement = host.bind("demo-plugin", "1")
    replacement.register_service(1, Service, {"cycle-peer": "1"})
    with pytest.raises(PluginHostError) as exc:
        host.resolve_service("demo-plugin", 1)
    assert exc.value.code == "unavailable"

    peer = host.bind("cycle-peer", "1")
    peer.register_service(1, Service, {"demo-plugin": "1"})
    assert host.resolve_service("demo-plugin", 1).ping() == "pong"
    assert host.resolve_service("cycle-peer", 1).ping() == "pong"
    replacement = host.facade("demo-plugin")
    host.bind("demo-plugin", "2")
    with pytest.raises(PluginHostError):
        first.ping()
    with pytest.raises(PluginHostError):
        replacement.register_resource(_resource)


def test_action_and_resource_descriptors_expose_models_without_handler_secrets() -> None:
    class Input(BaseModel):
        query: str

    class Output(BaseModel):
        answer: str

    host = PluginHost(lambda: None)
    facade = host.bind("demo-plugin", "1")

    async def handler(_actor: PluginActor, payload: Input) -> Output:
        return Output(answer=payload.query)

    facade.register_action("search.query", Input, Output, handler, "Search", "authenticated")
    facade.register_resource(_resource)
    descriptor = facade.descriptors()
    assert descriptor["actions"][0]["input_schema"]["properties"]["query"]["type"] == "string"
    assert descriptor["actions"][0]["output_schema"]["properties"]["answer"]["type"] == "string"
    assert "handler" not in str(descriptor)
    with pytest.raises(PluginHostError):
        facade.register_action("search.query", Input, Output, handler, "duplicate", "authenticated")
