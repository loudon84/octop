from __future__ import annotations

import json

from octop.infra.agents.plugins.host_contracts import PluginActor, RequiredExtensionWrite
from tests.support.app import octop_client, write_octop_config
from tests.support.auth import bootstrap_admin, create_user


async def test_plugin_requirements_http_shared_access_and_typed_contract(env) -> None:
    client, server, admin_auth = env
    assert server.services is not None and server.plugin_host is not None
    owner_id = server.user_manager.get_by_id(1).id
    server.services.agent_repo.create(agent_id="readiness-shared", user_id=owner_id, name="Shared")
    server.services.agent_repo.set_shared("readiness-shared", True)
    employee_auth = await create_user(client, admin_auth, username="readiness-employee")
    users = (await client.get("/api/users", headers=admin_auth)).json()
    employee_id = next(row["id"] for row in users if row["username"] == "readiness-employee")
    server.services.thread_repo.insert(
        thread_id="readiness-thread",
        agent_id="readiness-shared",
        user_id=employee_id,
        channel_type="dashboard",
        session_key="readiness-session",
    )
    server.services.agent_repo.create(
        agent_id="readiness-private", user_id=owner_id, name="Private"
    )
    server.services.thread_repo.insert(
        thread_id="readiness-private-thread",
        agent_id="readiness-private",
        user_id=employee_id,
        channel_type="dashboard",
        session_key="readiness-private-session",
    )

    host = server.plugin_host
    declarer = host.bind("readiness-declarer", "1.0")
    host.bind("missing-extension", "2.0")
    await declarer.commit(
        [],
        requirements=(
            RequiredExtensionWrite(
                PluginActor(owner_id, True, agent_id="readiness-shared"),
                "readiness-shared",
                {"missing-extension": "2.0", "readiness-declarer": "1.0"},
                0,
            ),
        ),
    )
    thread_key = json.dumps(
        ["readiness-shared", "readiness-thread", "readiness-declarer"],
        separators=(",", ":"),
    )
    server.services.plugin_record_repo.cas(
        "@host",
        "required_extensions",
        thread_key,
        owner_user_id=owner_id,
        payload_json=json.dumps(
            {
                "actor_user_id": owner_id,
                "agent_id": "readiness-shared",
                "thread_id": "readiness-thread",
                "declaring_plugin_id": "readiness-declarer",
                "required_plugins": {"thread-only-plugin": "3.0"},
                "enabled": True,
            },
            separators=(",", ":"),
        ),
        expected_revision=0,
    )
    host.deactivate("missing-extension")

    base_url = "/api/plugins/requirements?agent_id=readiness-shared"
    response = await client.get(base_url, headers=employee_auth)
    assert response.status_code == 200
    assert response.json() == {
        "api_version": 1,
        "required_plugins": {"missing-extension": "2.0", "readiness-declarer": "1.0"},
        "unavailable_plugins": ["missing-extension"],
    }
    thread_response = await client.get(
        f"{base_url}&thread_id=readiness-thread", headers=employee_auth
    )
    assert thread_response.status_code == 200
    assert thread_response.json()["required_plugins"] == {
        "missing-extension": "2.0",
        "readiness-declarer": "1.0",
        "thread-only-plugin": "3.0",
    }
    assert thread_response.json()["unavailable_plugins"] == [
        "missing-extension",
        "thread-only-plugin",
    ]
    assert "owner_user_id" not in response.text
    assert "declaring_plugin_id" not in response.text
    assert (await client.get(base_url)).status_code == 401

    foreign = await client.get(f"{base_url}&thread_id=unknown-thread", headers=admin_auth)
    assert foreign.status_code == 404
    private_with_owner_claim = await client.get(
        "/api/plugins/requirements?agent_id=readiness-private&user_id=" + str(owner_id),
        headers=employee_auth,
    )
    assert private_with_owner_claim.status_code == 403
    mismatched_agent_with_owner_claim = await client.get(
        f"{base_url}&thread_id=readiness-private-thread&user_id={owner_id}",
        headers=employee_auth,
    )
    assert mismatched_agent_with_owner_claim.status_code == 404
    admin_cannot_read_employee_thread = await client.get(
        f"{base_url}&thread_id=readiness-thread", headers=admin_auth
    )
    assert admin_cannot_read_employee_thread.status_code == 403
    spoofed = await client.get(f"{base_url}&user_id={owner_id}", headers=employee_auth)
    assert spoofed.status_code == 200
    assert spoofed.json()["required_plugins"] == {
        "missing-extension": "2.0",
        "readiness-declarer": "1.0",
    }


async def test_plugin_requirements_openapi_and_docs(tmp_octop_home) -> None:
    write_octop_config(tmp_octop_home, enable_api_docs=True)
    async with octop_client(tmp_octop_home, patch_llm=False) as (client, _server):
        await bootstrap_admin(client, tmp_octop_home)
        schema = (await client.get("/api/openapi.json")).json()
        operation = schema["paths"]["/api/plugins/requirements"]["get"]
        assert operation["summary"]
        assert "PluginRequirementReadinessResponse" in str(operation["responses"])
        assert "Agent declarations" in operation["description"]
        response_schema = schema["components"]["schemas"]["PluginRequirementReadinessResponse"]
        assert response_schema["properties"]["required_plugins"]["description"]
        assert response_schema["properties"]["unavailable_plugins"]["description"]
        query_names = {item["name"] for item in operation["parameters"]}
        assert {"agent_id", "thread_id"} <= query_names
        assert (await client.get("/api/docs")).status_code == 200
