"""Composio's personal OAuth and permission contract at the HTTP boundary."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from octop.infra.connectors.builder import mcp_server_name
from octop.infra.connectors.composio import COMPOSIO_KIND
from octop.infra.utils.ulid import new_ulid
from tests.support.auth import create_user, resolve_user_id


async def _start_composio_oauth(client, auth):
    with (
        patch("octop.api.routers.connectors._is_public_http_uri", return_value=False),
        patch(
            "octop.api.routers.connectors.start_oauth_for_target",
            new_callable=AsyncMock,
            return_value=(
                "https://connect.composio.dev/oauth/authorize?state=test",
                "verifier",
                {"flow": "catalog", "kind": COMPOSIO_KIND},
            ),
        ),
    ):
        response = await client.post(
            "/api/connectors/oauth/start",
            headers=auth,
            json={"target": {"type": "catalog", "kind": COMPOSIO_KIND}},
        )
    assert response.status_code == 200
    return response.json()["state_id"]


async def _seed_composio(server, user_id: int) -> str:
    repo = server.services.repos.connector_repo
    instance_id = new_ulid()
    repo.create(
        instance_id=instance_id,
        user_id=user_id,
        kind=COMPOSIO_KIND,
        display_name="My Composio",
        mcp_server_name=mcp_server_name(COMPOSIO_KIND, instance_id),
        shared=False,
        config_json="{}",
    )
    from octop.api.routers.connectors import _connector_service

    _connector_service(server).encrypt_and_store(
        instance_id=instance_id,
        payload={"access_token": "secret-access-token", "refresh_token": "secret-refresh-token"},
    )
    return instance_id


@pytest.fixture
async def env(env_with_agent):
    yield env_with_agent


async def test_composio_cannot_be_created_or_credentials_patched_through_generic_api(env):
    client, server, auth, _ = env
    created = await client.post(
        "/api/connector-instances",
        headers=auth,
        json={
            "kind": COMPOSIO_KIND,
            "display_name": "Imported Composio",
            "credentials": {"access_token": "client-supplied-secret"},
        },
    )
    assert created.status_code == 400
    assert not server.services.repos.connector_repo.list_by_kind(COMPOSIO_KIND)

    user_id = await resolve_user_id(client, auth, "admin")
    instance_id = await _seed_composio(server, user_id)
    patched = await client.patch(
        f"/api/connector-instances/{instance_id}",
        headers=auth,
        json={"credentials": {"access_token": "replacement-secret"}},
    )
    assert patched.status_code == 400
    from octop.api.routers.connectors import _connector_service

    assert _connector_service(server).decrypt(instance_id)["access_token"] == "secret-access-token"


async def test_composio_generic_test_refresh_are_not_owner_call_bypasses(env):
    client, server, auth, _ = env
    user_id = await resolve_user_id(client, auth, "admin")
    instance_id = await _seed_composio(server, user_id)

    tested = await client.post(f"/api/connector-instances/{instance_id}/test", headers=auth)
    refreshed = await client.post(f"/api/connector-instances/{instance_id}/refresh", headers=auth)
    patched = await client.patch(
        f"/api/connector-instances/{instance_id}",
        headers=auth,
        json={"display_name": "renamed"},
    )
    assert tested.status_code == 400
    assert refreshed.status_code == 400
    assert patched.status_code == 400


async def test_admin_can_read_redacted_composio_and_force_disconnect_only(env):
    client, server, admin_auth, _ = env
    await create_user(client, admin_auth, username="composio_owner")
    owner_id = await resolve_user_id(client, admin_auth, "composio_owner")
    instance_id = await _seed_composio(server, owner_id)

    read = await client.get(f"/api/connector-instances/{instance_id}", headers=admin_auth)
    assert read.status_code == 200
    assert "secret-access-token" not in read.text
    assert "secret-refresh-token" not in read.text
    assert read.json()["credentials_preview"]["oauth_configured"] is True

    test_result = await client.post(
        f"/api/connector-instances/{instance_id}/test", headers=admin_auth
    )
    refresh = await client.post(
        f"/api/connector-instances/{instance_id}/refresh", headers=admin_auth
    )
    patch_result = await client.patch(
        f"/api/connector-instances/{instance_id}",
        headers=admin_auth,
        json={"display_name": "admin rename"},
    )
    search_as_admin = await client.post(
        "/api/connectors/composio/apps/search",
        headers=admin_auth,
        json={"query": "gmail", "user_id": owner_id},
    )
    admin_delete = await client.delete(
        f"/api/connector-instances/{instance_id}", headers=admin_auth
    )
    assert test_result.status_code == 403
    assert refresh.status_code == 403
    assert patch_result.status_code == 403
    assert search_as_admin.status_code == 404
    assert admin_delete.status_code == 204


async def test_composio_authorize_can_be_cancelled_and_late_callback_is_rejected(env):
    client, server, auth, _ = env
    state_id = await _start_composio_oauth(client, auth)
    other_auth = await create_user(client, auth, username="composio_cancel_other")
    wrong_user_cancel = await client.post(
        "/api/connectors/oauth/cancel",
        headers=other_auth,
        json={"state_id": state_id},
    )
    assert wrong_user_cancel.status_code == 404
    cancelled = await client.post(
        "/api/connectors/oauth/cancel", headers=auth, json={"state_id": state_id}
    )
    assert cancelled.status_code == 200

    with (
        patch(
            "octop.api.routers.connectors.exchange_oauth_code",
            new_callable=AsyncMock,
            return_value={"access_token": "late-access-token"},
        ) as exchange,
        patch(
            "octop.infra.connectors.composio.ComposioService.validate_oauth_tokens",
            new_callable=AsyncMock,
        ) as validate,
    ):
        response = await client.get(
            "/api/connectors/oauth/callback",
            params={"code": "late-code", "state": state_id},
        )
    assert response.status_code == 400
    exchange.assert_not_awaited()
    validate.assert_not_awaited()
    assert not server.services.repos.connector_repo.list_by_kind(COMPOSIO_KIND)


async def test_disconnect_before_first_callback_invalidates_even_without_a_connection(env):
    client, server, auth, _ = env
    disabled_state = await _start_composio_oauth(client, auth)
    disabled = await client.post("/api/connectors/composio/disable", headers=auth)
    assert disabled.status_code == 404
    with patch(
        "octop.api.routers.connectors.exchange_oauth_code", new_callable=AsyncMock
    ) as exchange:
        stale = await client.get(
            "/api/connectors/oauth/callback",
            params={
                "code": "late-code",
                "state": server.services.repos.connector_repo.get_oauth_state_by_id(
                    disabled_state
                ).state,
            },
        )
    assert stale.status_code == 400
    exchange.assert_not_awaited()

    state_id = await _start_composio_oauth(client, auth)
    disconnected = await client.post("/api/connectors/composio/disconnect", headers=auth)
    assert disconnected.status_code == 200

    with patch(
        "octop.api.routers.connectors.exchange_oauth_code", new_callable=AsyncMock
    ) as exchange:
        response = await client.get(
            "/api/connectors/oauth/callback",
            params={"code": "late-code", "state": state_id},
        )
    assert response.status_code == 400
    exchange.assert_not_awaited()
    assert not server.services.repos.connector_repo.list_by_kind(COMPOSIO_KIND)


async def test_cancel_during_validation_prevents_callback_from_replacing_credentials(env):
    client, server, auth, _ = env
    user_id = await resolve_user_id(client, auth, "admin")
    instance_id = await _seed_composio(server, user_id)
    state_id = await _start_composio_oauth(client, auth)

    async def cancel_while_validating(_tokens):
        cancelled = await client.post(
            "/api/connectors/oauth/cancel", headers=auth, json={"state_id": state_id}
        )
        assert cancelled.status_code == 200
        return {"health": "reachable", "capabilities": {}}

    with (
        patch(
            "octop.api.routers.connectors.exchange_oauth_code",
            new_callable=AsyncMock,
            return_value={"access_token": "replacement-access-token"},
        ),
        patch(
            "octop.infra.connectors.composio.ComposioService.validate_oauth_tokens",
            new_callable=AsyncMock,
            side_effect=cancel_while_validating,
        ),
    ):
        response = await client.get(
            "/api/connectors/oauth/callback",
            params={
                "code": "good-code",
                "state": server.services.repos.connector_repo.get_oauth_state_by_id(state_id).state,
            },
        )
    assert response.status_code == 400
    from octop.api.routers.connectors import _connector_service

    assert _connector_service(server).decrypt(instance_id)["access_token"] == "secret-access-token"


async def test_failed_composio_validation_preserves_existing_connection(env):
    client, server, auth, _ = env
    user_id = await resolve_user_id(client, auth, "admin")
    instance_id = await _seed_composio(server, user_id)
    state_id = await _start_composio_oauth(client, auth)
    state_row = server.services.repos.connector_repo.get_oauth_state_by_id(state_id)

    with (
        patch(
            "octop.api.routers.connectors.exchange_oauth_code",
            new_callable=AsyncMock,
            return_value={"access_token": "invalid-replacement-token"},
        ),
        patch(
            "octop.infra.connectors.composio.ComposioService.validate_oauth_tokens",
            new_callable=AsyncMock,
            side_effect=RuntimeError("provider echoed invalid-replacement-token"),
        ),
    ):
        response = await client.get(
            "/api/connectors/oauth/callback",
            params={"code": "bad-code", "state": state_row.state},
        )
    assert response.status_code == 400
    assert "invalid-replacement-token" not in response.text
    from octop.api.routers.connectors import _connector_service

    assert _connector_service(server).decrypt(instance_id)["access_token"] == "secret-access-token"


async def test_composio_callback_validates_before_save_and_pending_never_returns_tokens(env):
    client, server, auth, _ = env
    state_id = await _start_composio_oauth(client, auth)
    user_id = await resolve_user_id(client, auth, "admin")
    state_rows = server.services.repos.connector_repo
    state_row = state_rows.get_oauth_state_by_id(state_id)

    with (
        patch(
            "octop.api.routers.connectors.exchange_oauth_code",
            new_callable=AsyncMock,
            return_value={
                "access_token": "valid-access-token",
                "refresh_token": "valid-refresh-token",
            },
        ),
        patch(
            "octop.infra.connectors.composio.ComposioService.validate_oauth_tokens",
            new_callable=AsyncMock,
            return_value={"health": "reachable", "capabilities": {}},
        ) as validate,
    ):
        response = await client.get(
            "/api/connectors/oauth/callback",
            params={"code": "good-code", "state": state_row.state},
        )
    assert response.status_code == 200
    validate.assert_awaited_once()
    pending = await client.get(f"/api/connectors/oauth/pending/{state_id}", headers=auth)
    assert pending.status_code == 200
    data = pending.json()
    assert data["kind"] == COMPOSIO_KIND
    assert data["applied"] is True
    assert "tokens" not in data
    assert data["connection"]["instance_id"]
    assert "valid-access-token" not in pending.text
    assert "valid-refresh-token" not in pending.text
    assert len(state_rows.list_by_kind(COMPOSIO_KIND)) == 1
    assert user_id == state_rows.list_by_kind(COMPOSIO_KIND)[0].user_id


async def test_composio_authorization_does_not_allow_shared_or_default_open(env):
    client, server, auth, _ = env
    for fields, expected_status in (({"shared": True}, 403), ({"default_open": True}, 400)):
        response = await client.post(
            "/api/connector-instances",
            headers=auth,
            json={"kind": COMPOSIO_KIND, "display_name": "Composio", **fields},
        )
        assert response.status_code == expected_status
    assert not server.services.repos.connector_repo.list_by_kind(COMPOSIO_KIND)
