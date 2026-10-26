"""Authenticated HTTP surface for versioned plugin host actions and resources."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

import pytest
from octop_harness.plugins.context import PluginContext
from octop_harness.plugins.manifest import PluginManifest
from pydantic import BaseModel, ConfigDict

from octop.infra.agents.plugins.host import register_host_setup
from octop.infra.agents.plugins.host_contracts import PluginActor, PluginResource
from tests.support.app import octop_client, write_octop_config
from tests.support.auth import bootstrap_admin, create_user


class _Payload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


class _Result(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


async def _install_synthetic_host_plugin(
    server: Any, plugin_id: str = "synthetic-plugin"
) -> dict[str, Any]:
    assert server.plugin_host is not None
    started = asyncio.Event()
    finish = asyncio.Event()
    cancelled = asyncio.Event()
    received: list[PluginActor] = []

    async def ping(actor: PluginActor, payload: _Payload) -> _Result:
        received.append(actor)
        return _Result(value=payload.value)

    async def admin_action(_actor: PluginActor, payload: _Payload) -> _Result:
        return _Result(value=payload.value)

    async def failing_action(_actor: PluginActor, _payload: _Payload) -> _Result:
        raise RuntimeError("HANDLER_SECRET_CANARY")

    async def invalid_output(_actor: PluginActor, _payload: _Payload) -> _Result:
        return {"value": "OUTPUT_SECRET_CANARY", "extra": "OUTPUT_SECRET_CANARY"}  # type: ignore[return-value]

    async def wait_action(_actor: PluginActor, payload: _Payload) -> _Result:
        started.set()
        try:
            await finish.wait()
        finally:
            cancelled.set()
        return _Result(value=payload.value)

    async def resource(_actor: PluginActor, token: str) -> PluginResource:
        if token in {"broken", "RESOURCE_SECRET_CANARY"}:
            raise RuntimeError("RESOURCE_SECRET_CANARY")
        return PluginResource(body=b"plugin bytes", media_type="application/octet-stream")

    def setup(facade: Any) -> None:
        facade.register_action("ping", _Payload, _Result, ping, "Ping", "authenticated")
        facade.register_action("admin", _Payload, _Result, admin_action, "Admin", "admin")
        facade.register_action("owner", _Payload, _Result, ping, "Owner", "agent_owner")
        facade.register_action("fail", _Payload, _Result, failing_action, "Fail", "authenticated")
        facade.register_action(
            "bad_output", _Payload, _Result, invalid_output, "Bad output", "authenticated"
        )
        facade.register_action("wait", _Payload, _Result, wait_action, "Wait", "authenticated")
        facade.register_resource(resource)

    context = PluginContext(
        manifest=PluginManifest(
            id=plugin_id,
            version="1.0.0",
            name=plugin_id,
            kind="tool",
            entry="main.py",
        ),
        source_path=Path(__file__).parent,
    )
    register_host_setup(context, setup)
    server.plugin_host.bind_context(context, "1.0.0")
    return {"started": started, "finish": finish, "cancelled": cancelled, "received": received}


async def test_plugin_action_auth_schema_context_and_safe_errors(env, caplog) -> None:
    client, server, admin_auth = env
    state = await _install_synthetic_host_plugin(server)

    assert (await client.get("/api/plugins/synthetic-plugin/actions")).status_code == 401
    catalog = await client.get("/api/plugins/synthetic-plugin/actions", headers=admin_auth)
    assert catalog.status_code == 200
    assert catalog.json()["actions"][0]["input_schema"]["properties"]["value"]["type"] == "string"
    assert (
        next(item for item in catalog.json()["actions"] if item["name"] == "admin")["permission"]
        == "admin"
    )

    ok = await client.post(
        "/api/plugins/synthetic-plugin/actions/ping",
        headers=admin_auth,
        json={"api_version": 1, "payload": {"value": "pong"}},
    )
    assert ok.status_code == 200
    assert ok.json() == {"api_version": 1, "result": {"value": "pong"}}
    actor = state["received"][-1]
    assert actor.user_id == server.user_manager.get_by_id(actor.user_id).id
    assert actor.is_admin is True

    current_user = server.user_manager.get_by_id(actor.user_id)
    assert current_user is not None
    current_user.locale = "en"
    await client.post(
        "/api/plugins/synthetic-plugin/actions/ping",
        headers={**admin_auth, "Accept-Language": "zh-CN"},
        json={"api_version": 1, "payload": {"value": "locale"}},
    )
    assert state["received"][-1].locale == "en"
    current_user.locale = "zh"
    await client.post(
        "/api/plugins/synthetic-plugin/actions/ping",
        headers={**admin_auth, "Accept-Language": "en-US"},
        json={"api_version": 1, "payload": {"value": "locale"}},
    )
    assert state["received"][-1].locale == "zh"

    fake_actor = await client.post(
        "/api/plugins/synthetic-plugin/actions/ping",
        headers=admin_auth,
        json={
            "api_version": 1,
            "actor": {"user_id": "SPOOF_SECRET_CANARY"},
            "payload": {"value": "x"},
        },
    )
    assert fake_actor.status_code == 400
    assert "SPOOF_SECRET_CANARY" not in fake_actor.text
    assert "SPOOF_SECRET_CANARY" not in caplog.text
    extra_context = await client.post(
        "/api/plugins/synthetic-plugin/actions/ping",
        headers=admin_auth,
        json={"api_version": 1, "context": {"user_id": 999}, "payload": {"value": "x"}},
    )
    assert extra_context.status_code == 400
    assert "999" not in extra_context.text
    bad_version = await client.post(
        "/api/plugins/synthetic-plugin/actions/ping",
        headers=admin_auth,
        json={"api_version": "VERSION_SECRET_CANARY", "payload": {"value": "x"}},
    )
    assert bad_version.status_code == 400
    assert "VERSION_SECRET_CANARY" not in bad_version.text
    assert "VERSION_SECRET_CANARY" not in caplog.text
    bad_context_type = await client.post(
        "/api/plugins/synthetic-plugin/actions/ping",
        headers=admin_auth,
        json={
            "api_version": 1,
            "context": "CONTEXT_TYPE_SECRET_CANARY",
            "payload": {"value": "x"},
        },
    )
    assert bad_context_type.status_code == 400
    assert "CONTEXT_TYPE_SECRET_CANARY" not in bad_context_type.text
    assert "CONTEXT_TYPE_SECRET_CANARY" not in caplog.text
    user_auth = await create_user(client, admin_auth, username="plugin-user")
    denied = await client.post(
        "/api/plugins/synthetic-plugin/actions/admin",
        headers=user_auth,
        json={"api_version": 1, "payload": {"value": "x"}},
    )
    assert denied.status_code == 403

    bad_input = await client.post(
        "/api/plugins/synthetic-plugin/actions/ping",
        headers=admin_auth,
        json={"api_version": 1, "payload": {"value": "INPUT_SECRET_CANARY", "extra": "x"}},
    )
    assert bad_input.status_code == 400
    assert "INPUT_SECRET_CANARY" not in bad_input.text

    for action, canary in (
        ("fail", "HANDLER_SECRET_CANARY"),
        ("bad_output", "OUTPUT_SECRET_CANARY"),
    ):
        response = await client.post(
            f"/api/plugins/synthetic-plugin/actions/{action}",
            headers=admin_auth,
            json={"api_version": 1, "payload": {"value": "x"}},
        )
        assert response.status_code == 500
        assert canary not in response.text
        assert canary not in caplog.text

    unknown = await client.post(
        "/api/plugins/synthetic-plugin/actions/unknown",
        headers=admin_auth,
        json={"api_version": 1, "payload": {}},
    )
    assert unknown.status_code == 404


async def test_plugin_action_context_checks_and_resource_response(env, caplog) -> None:
    client, server, admin_auth = env
    await _install_synthetic_host_plugin(server)
    assert server.services is not None
    admin_id = server.user_manager.get_by_id(1).id
    server.services.agent_repo.create(agent_id="ctx-agent", user_id=admin_id, name="context")
    server.services.thread_repo.insert(
        thread_id="ctx-thread",
        agent_id="ctx-agent",
        user_id=admin_id,
        channel_type="dashboard",
        session_key="ctx-session",
    )
    server.services.agent_repo.create(agent_id="ctx-agent-alt", user_id=admin_id, name="other")
    server.services.thread_repo.insert(
        thread_id="ctx-thread-other-agent",
        agent_id="ctx-agent-alt",
        user_id=admin_id,
        channel_type="dashboard",
        session_key="ctx-session-alt",
    )
    await create_user(client, admin_auth, username="thread-owner")
    user_id = next(
        row["id"]
        for row in (await client.get("/api/users", headers=admin_auth)).json()
        if row["username"] == "thread-owner"
    )
    server.services.thread_repo.insert(
        thread_id="ctx-thread-other-user",
        agent_id="ctx-agent",
        user_id=user_id,
        channel_type="dashboard",
        session_key="ctx-session-other-user",
    )
    ok = await client.post(
        "/api/plugins/synthetic-plugin/actions/owner",
        headers=admin_auth,
        json={
            "api_version": 1,
            "context": {"agent_id": "ctx-agent", "thread_id": "ctx-thread"},
            "payload": {"value": "owned"},
        },
    )
    assert ok.status_code == 200
    mismatched_agent = await client.post(
        "/api/plugins/synthetic-plugin/actions/ping",
        headers=admin_auth,
        json={
            "api_version": 1,
            "context": {"agent_id": "ctx-agent", "thread_id": "ctx-thread-other-agent"},
            "payload": {"value": "x"},
        },
    )
    assert mismatched_agent.status_code == 404
    cross_user_thread = await client.post(
        "/api/plugins/synthetic-plugin/actions/ping",
        headers=admin_auth,
        json={
            "api_version": 1,
            "context": {"agent_id": "ctx-agent", "thread_id": "ctx-thread-other-user"},
            "payload": {"value": "x"},
        },
    )
    assert cross_user_thread.status_code == 403
    no_agent = await client.post(
        "/api/plugins/synthetic-plugin/actions/ping",
        headers=admin_auth,
        json={
            "api_version": 1,
            "context": {"thread_id": "ctx-thread"},
            "payload": {"value": "x"},
        },
    )
    assert no_agent.status_code == 400

    resource = await client.get(
        "/api/plugins/synthetic-plugin/resources/opaque-token", headers=admin_auth
    )
    assert resource.status_code == 200
    assert resource.content == b"plugin bytes"
    broken = await client.get(
        "/api/plugins/synthetic-plugin/resources/RESOURCE_SECRET_CANARY", headers=admin_auth
    )
    assert broken.status_code == 500
    assert "RESOURCE_SECRET_CANARY" not in broken.text
    assert "RESOURCE_SECRET_CANARY" not in caplog.text


async def test_plugin_action_openapi_docs_include_typed_contract(tmp_octop_home) -> None:
    write_octop_config(tmp_octop_home, enable_api_docs=True)
    async with octop_client(tmp_octop_home, patch_llm=False) as (client, server):
        await bootstrap_admin(client, tmp_octop_home)
        await _install_synthetic_host_plugin(server)
        schema = (await client.get("/api/openapi.json")).json()
        operation = schema["paths"]["/api/plugins/{plugin_id}/actions/{action}"]["post"]
        assert operation["summary"]
        assert "PluginActionEnvelope" in str(operation["requestBody"])
        assert (await client.get("/api/docs")).status_code == 200


async def test_plugin_http_disable_and_uninstall_cancel_inflight_actions(env) -> None:
    client, server, admin_auth = env
    assert server.plugin_manager is not None
    await server.plugin_manager.mutate(
        lambda: server.plugin_manager.install_path(
            Path(__file__).resolve().parents[1] / "fixtures" / "plugins" / "echo-tool",
            force=True,
        ),
    )
    state = await _install_synthetic_host_plugin(server, "echo-tool")
    request = asyncio.create_task(
        client.post(
            "/api/plugins/echo-tool/actions/wait",
            headers=admin_auth,
            json={"api_version": 1, "payload": {"value": "x"}},
        )
    )
    await asyncio.wait_for(state["started"].wait(), timeout=2)
    entered = threading.Event()
    release = threading.Event()

    def hold_host_operation_lock() -> None:
        assert server.plugin_host is not None
        with server.plugin_host._operation_lock:
            entered.set()
            assert release.wait(timeout=5)

    lease = asyncio.get_running_loop().run_in_executor(None, hold_host_operation_lock)
    assert await asyncio.to_thread(entered.wait, 2)
    disable_request = asyncio.create_task(
        client.patch("/api/plugins/echo-tool", headers=admin_auth, json={"enabled": False})
    )
    heartbeat = asyncio.Event()
    asyncio.get_running_loop().call_soon(heartbeat.set)
    await asyncio.wait_for(heartbeat.wait(), timeout=0.2)
    assert not disable_request.done()
    release.set()
    await asyncio.wait_for(lease, timeout=2)
    disabled = await disable_request
    assert disabled.status_code == 200
    canceled_response = await request
    assert canceled_response.status_code == 503
    assert state["cancelled"].is_set()

    enabled = await client.patch(
        "/api/plugins/echo-tool", headers=admin_auth, json={"enabled": True}
    )
    assert enabled.status_code == 200
    state = await _install_synthetic_host_plugin(server, "echo-tool")
    request = asyncio.create_task(
        client.post(
            "/api/plugins/echo-tool/actions/wait",
            headers=admin_auth,
            json={"api_version": 1, "payload": {"value": "x"}},
        )
    )
    await asyncio.wait_for(state["started"].wait(), timeout=2)
    removed = await client.delete("/api/plugins/echo-tool", headers=admin_auth)
    assert removed.status_code == 200
    canceled_response = await request
    assert canceled_response.status_code == 503
    assert state["cancelled"].is_set()


async def test_plugin_host_is_recreated_after_stop_restart(env) -> None:
    _client, server, _auth = env
    old_host = server.plugin_host
    assert old_host is not None
    await server.stop()
    await server.start()
    assert server.plugin_host is not None
    assert server.plugin_host is not old_host
    from octop.infra.agents.plugins.host import PluginHostError

    with pytest.raises(PluginHostError):
        old_host.facade("after-restart")
