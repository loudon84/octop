from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from octop.infra.errors import OctopError
from octop.infra.users.identity import Role
from tests.support.app import octop_client, write_octop_config


async def test_public_policy_api_is_typed_and_admin_update_controls_creation(env) -> None:
    client, server, admin_auth = env

    public = await client.get("/api/auth/registration-policy")
    assert public.status_code == 200
    assert public.json() == {"email_required": False}
    assert (await client.get("/api/auth/me")).status_code == 401
    from octop.api.deps import is_jwt_exempt_path

    assert is_jwt_exempt_path("/api/auth/registration-policy") is True
    assert is_jwt_exempt_path("/api/auth/me") is False

    wrong_type = await client.put(
        "/api/settings/registration-policy", headers=admin_auth, json={"email_required": "true"}
    )
    extra = await client.put(
        "/api/settings/registration-policy",
        headers=admin_auth,
        json={"email_required": True, "user_id": 3},
    )
    assert wrong_type.status_code == 422
    assert extra.status_code == 422
    assert (
        await client.put("/api/settings/registration-policy", json={"email_required": True})
    ).status_code == 401

    created = await client.post(
        "/api/users",
        headers=admin_auth,
        json={"username": "delegate", "password": "TestPass12", "role": "user"},
    )
    assert created.status_code == 201
    delegated_login = await client.post(
        "/api/auth/login", json={"username": "delegate", "password": "TestPass12"}
    )
    delegated_auth = {"Authorization": f"Bearer {delegated_login.json()['access_token']}"}
    denied = await client.put(
        "/api/settings/registration-policy",
        headers=delegated_auth,
        json={"email_required": True},
    )
    assert denied.status_code == 403

    delegated_users = await client.post(
        "/api/users",
        headers=admin_auth,
        json={
            "username": "users-delegate",
            "password": "TestPass12",
            "role": "user",
            "permissions": ["users"],
        },
    )
    assert delegated_users.status_code == 201
    delegated_users_login = await client.post(
        "/api/auth/login", json={"username": "users-delegate", "password": "TestPass12"}
    )
    delegated_users_auth = {
        "Authorization": f"Bearer {delegated_users_login.json()['access_token']}"
    }
    assert (await client.get("/api/users", headers=delegated_users_auth)).status_code == 200
    delegated_users_denied = await client.put(
        "/api/settings/registration-policy",
        headers=delegated_users_auth,
        json={"email_required": True},
    )
    assert delegated_users_denied.status_code == 403

    updated = await client.put(
        "/api/settings/registration-policy",
        headers=admin_auth,
        json={"email_required": True},
    )
    assert updated.status_code == 200
    assert updated.json() == {"email_required": True}
    assert (await client.get("/api/auth/registration-policy")).json() == {"email_required": True}

    invite = await client.post("/api/users/invites", headers=admin_auth, json={})
    assert invite.status_code == 201
    denied_redeem = await client.post(
        "/api/auth/invite/redeem",
        json={"code": invite.json()["code"], "username": "missing", "password": "TestPass12"},
    )
    assert denied_redeem.status_code == 400
    assert denied_redeem.json()["error"]["code"] == "EMAIL_REQUIRED"
    assert server.services.invite_repo.get_by_code(invite.json()["code"]).used_at is None

    await server.user_manager.create(
        username="existing", password="TestPass12", role=Role.USER, email="owned@example.com"
    )
    duplicate_invite = await client.post("/api/users/invites", headers=admin_auth, json={})
    duplicate_redeem = await client.post(
        "/api/auth/invite/redeem",
        json={
            "code": duplicate_invite.json()["code"],
            "username": "duplicate",
            "password": "TestPass12",
            "email": "OWNED@example.com",
        },
    )
    assert duplicate_redeem.status_code == 409
    assert duplicate_redeem.json()["error"]["code"] == "EMAIL_TAKEN"
    assert server.services.invite_repo.get_by_code(duplicate_invite.json()["code"]).used_at is None
    assert "owned@example.com" not in duplicate_redeem.text

    unique_invite = await client.post("/api/users/invites", headers=admin_auth, json={})
    redeemed = await client.post(
        "/api/auth/invite/redeem",
        json={
            "code": unique_invite.json()["code"],
            "username": "normalized",
            "password": "TestPass12",
            "email": "  Unique@Example.com ",
        },
    )
    assert redeemed.status_code == 200
    assert server.services.user_repo.get_by_email("unique@example.com") is not None

    spec = client._octop_app.openapi()
    get_op = spec["paths"]["/api/auth/registration-policy"]["get"]
    put_op = spec["paths"]["/api/settings/registration-policy"]["put"]
    assert get_op["summary"] == "Get public user registration policy"
    assert put_op["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "RegistrationPolicyRequest"
    )
    server.services.settings_repo.set("user_registration.email_required", "secret-value")
    corrupt = await client.get("/api/auth/registration-policy")
    assert corrupt.status_code == 503
    assert corrupt.json()["error"]["code"] == "REGISTRATION_POLICY_INVALID"
    assert "secret-value" not in corrupt.text


async def test_online_admin_and_invite_creation_wake_once_and_remain_identity_visible(env) -> None:
    client, server, admin_auth = env
    notifications: list[None] = []
    assert server.user_manager is not None
    assert server.plugin_host is not None
    server.user_manager._user_created_notifier = lambda: notifications.append(None)

    direct = await client.post(
        "/api/users",
        headers=admin_auth,
        json={"username": "notified-direct", "password": "TestPass12", "role": "user"},
    )
    assert direct.status_code == 201
    assert notifications == [None]

    await server.user_manager.create(
        username="email-owner", password="TestPass12", role=Role.USER, email="taken@example.com"
    )
    notifications.clear()
    await client.put(
        "/api/settings/registration-policy",
        headers=admin_auth,
        json={"email_required": True},
    )
    denied_invite = await client.post("/api/users/invites", headers=admin_auth, json={})
    denied = await client.post(
        "/api/auth/invite/redeem",
        json={
            "code": denied_invite.json()["code"],
            "username": "missing-email",
            "password": "TestPass12",
        },
    )
    assert denied.status_code == 400
    assert denied.json()["error"]["code"] == "EMAIL_REQUIRED"
    assert server.services.invite_repo.get_by_code(denied_invite.json()["code"]).used_at is None
    assert notifications == []

    duplicate_invite = await client.post("/api/users/invites", headers=admin_auth, json={})
    duplicate = await client.post(
        "/api/auth/invite/redeem",
        json={
            "code": duplicate_invite.json()["code"],
            "username": "duplicate-email",
            "password": "TestPass12",
            "email": "TAKEN@example.com",
        },
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "EMAIL_TAKEN"
    assert server.services.invite_repo.get_by_code(duplicate_invite.json()["code"]).used_at is None
    assert notifications == []

    successful_invite = await client.post("/api/users/invites", headers=admin_auth, json={})
    redeemed = await client.post(
        "/api/auth/invite/redeem",
        json={
            "code": successful_invite.json()["code"],
            "username": "notified-invite",
            "password": "TestPass12",
            "email": "invitee@example.test",
        },
    )
    assert redeemed.status_code == 200
    assert notifications == [None]

    from octop_harness.plugins.context import PluginContext
    from octop_harness.plugins.manifest import PluginManifest

    from octop.infra.agents.plugins.host import register_host_setup

    context = PluginContext(
        manifest=PluginManifest("identity-scan", "1", "Identity scan", "skill", "main.py"),
        source_path=Path("identity-scan"),
    )
    register_host_setup(context, lambda facade: facade.register_user_identity_consumer())
    facade = server.plugin_host.bind_context(context, "1")
    visible = await facade.list_user_identities()
    visible_ids = {identity.user_id for identity in visible.items}
    assert direct.json()["id"] in visible_ids
    invited = server.services.user_repo.get_by_username("notified-invite")
    assert invited is not None and invited.id in visible_ids


async def test_registration_policy_openapi_schema_and_scalar_docs_route(tmp_octop_home) -> None:
    write_octop_config(tmp_octop_home, enable_api_docs=True)
    async with octop_client(tmp_octop_home, patch_llm=False) as (client, _server):
        docs = await client.get("/api/docs")
        openapi = await client.get("/api/openapi.json")
        assert docs.status_code == 200
        assert openapi.status_code == 200
        spec = openapi.json()
        assert spec["paths"]["/api/auth/registration-policy"]["get"]["summary"] == (
            "Get public user registration policy"
        )
        put_schema = spec["paths"]["/api/settings/registration-policy"]["put"]["requestBody"][
            "content"
        ]["application/json"]["schema"]["$ref"]
        assert put_schema.endswith("RegistrationPolicyRequest")
        assert spec["paths"]["/api/auth/registration-policy"]["get"]["responses"]["200"]["content"][
            "application/json"
        ]["schema"]["$ref"].endswith("RegistrationPolicyResponse")


async def test_user_manager_requires_normalized_unique_email_and_keeps_optional_default(
    env,
) -> None:
    _client, server, _auth = env
    optional = await server.user_manager.create(
        username="optional", password="TestPass12", role=Role.USER
    )
    assert server.services.user_repo.get(optional.id).email is None

    server.services.settings_repo.set("user_registration.email_required", "true")
    with pytest.raises(OctopError) as missing:
        await server.user_manager.create(username="missing", password="TestPass12", role=Role.USER)
    assert missing.value.code.value == "EMAIL_REQUIRED"

    first, second = await asyncio.gather(
        server.user_manager.create(
            username="first", password="TestPass12", role=Role.USER, email=" Person@Example.com "
        ),
        server.user_manager.create(
            username="second", password="TestPass12", role=Role.USER, email="person@example.COM"
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(value, BaseException) for value in (first, second)) == 1
    assert (
        sum(
            isinstance(value, OctopError) and value.code.value == "EMAIL_TAKEN"
            for value in (first, second)
        )
        == 1
    )
    row = server.services.user_repo.get_by_email("person@example.com")
    assert row is not None

    from octop.cli.support.offline_ops import create_user_offline

    with pytest.raises(OctopError) as offline_missing:
        create_user_offline(
            username="offline-missing",
            password="TestPass12",
            role="user",
            home=server.services.paths.root,
        )
    assert offline_missing.value.code.value == "EMAIL_REQUIRED"
    offline = create_user_offline(
        username="offline-user",
        password="TestPass12",
        role="user",
        email="  Offline@Example.com ",
        home=server.services.paths.root,
    )
    assert offline["email"] == "offline@example.com"


async def test_stale_admin_claim_is_rechecked_in_policy_domain(env) -> None:
    _client, server, _auth = env
    admin = server.services.user_repo.get_by_username("admin")
    assert admin is not None
    server.services.user_repo.set_role(admin.id, "user")
    from octop.infra.users.registration_policy import (
        UserRegistrationPolicy,
        update_registration_policy,
    )

    with pytest.raises(OctopError) as denied:
        update_registration_policy(server.services, admin.id, UserRegistrationPolicy(True))
    assert denied.value.code.value == "FORBIDDEN"
