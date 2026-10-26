from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

_PLUGIN_ROOT = Path(__file__).resolve().parents[3] / "plugins" / "enterprise-knowledge"
sys.path.insert(0, str(_PLUGIN_ROOT.resolve()))
_models = import_module("enterprise_knowledge.provisioning_models")
ProvisioningConfig = _models.ProvisioningConfig
ProvisioningConfigSaveInput = _models.ProvisioningConfigSaveInput
ProvisioningRetainedListInput = _models.ProvisioningRetainedListInput
ProvisioningRetainedListOutput = _models.ProvisioningRetainedListOutput
ProvisioningRetainedSummary = _models.ProvisioningRetainedSummary
ProvisioningRetryInput = _models.ProvisioningRetryInput
ProvisioningStatus = _models.ProvisioningStatus
ProvisioningTaskRetryInput = _models.ProvisioningTaskRetryInput
ProvisioningTasksInput = _models.ProvisioningTasksInput
ProvisioningTasksOutput = _models.ProvisioningTasksOutput
ProvisioningTaskSummary = _models.ProvisioningTaskSummary

SCHEMA_ID = "octop.enterprise.provisioning.v1"
UUID4 = "123e4567-e89b-42d3-a456-426614174000"
UTC = "2026-10-25T12:34:56Z"


def test_status_and_config_outputs_have_fixed_wire_envelope() -> None:
    status = ProvisioningStatus(
        revision=2,
        state="READY",
        error_code=None,
        updated_at=UTC,
        last_attempt_at=None,
        next_retry_at=None,
        can_retry=False,
    )
    config = ProvisioningConfig(
        revision=2,
        anchor_revision=1,
        name="Private KB",
        base_url="https://weknora.example/api/v1",
        enabled=True,
        credential_configured=True,
        updated_at=UTC,
    )

    assert status.model_dump() == {
        "schema_id": SCHEMA_ID,
        "version": 1,
        "revision": 2,
        "state": "READY",
        "error_code": None,
        "updated_at": UTC,
        "last_attempt_at": None,
        "next_retry_at": None,
        "can_retry": False,
    }
    assert config.base_url == "https://weknora.example/api/v1"
    assert config.schema_id == SCHEMA_ID


def test_inputs_default_nulls_and_secret_serialization_are_safe() -> None:
    retry = ProvisioningRetryInput(expected_revision=0)
    tasks = ProvisioningTasksInput()
    retained = ProvisioningRetainedListInput()
    config = ProvisioningConfigSaveInput(
        expected_revision=0,
        expected_anchor_revision=0,
        name=" Private KB ",
        base_url="https://weknora.example:443/api/v1",
        enabled=True,
        system_admin_jwt=SecretStr("secret-jwt"),
    )

    assert retry.expected_revision == 0
    assert tasks.model_dump() == {"after_user_id": 0, "limit": 50}
    assert retained.model_dump() == {"after_key": None, "limit": 50}
    assert config.name == "Private KB"
    assert config.base_url == "https://weknora.example/api/v1"
    assert config.system_admin_jwt.get_secret_value() == "secret-jwt"
    serialized_config = config.model_dump_json()
    assert "secret-jwt" not in serialized_config
    assert "**********" in serialized_config
    assert "system_admin_jwt" not in ProvisioningConfig.model_json_schema()["properties"]


def test_task_and_retained_lists_accept_only_public_projections() -> None:
    status = ProvisioningStatus(
        revision=1,
        state="PENDING",
        error_code=None,
        updated_at=UTC,
        last_attempt_at=None,
        next_retry_at=None,
        can_retry=False,
    )
    task = ProvisioningTaskSummary(
        task_id=UUID4,
        user_id=7,
        email=None,
        status=status,
    )
    tasks = ProvisioningTasksOutput(items=[task], next_user_id=None)
    summary = ProvisioningRetainedSummary(
        intent_id=UUID4,
        local_user_id=7,
        resource_kind="KB",
        phase="VERIFIED",
        proof_kind="CREATED_201",
        has_receipt=True,
        review_required=False,
        revision=3,
    )
    retained = ProvisioningRetainedListOutput(items=[summary], next_key=None)

    assert tasks.items[0].user_id == 7
    assert retained.items[0].intent_id == UUID4
    assert tasks.schema_id == retained.schema_id == SCHEMA_ID
    with pytest.raises(ValidationError):
        ProvisioningTasksOutput.model_validate(
            {"items": [{**task.model_dump(), "remote_id": "remote"}], "next_user_id": None}
        )
    with pytest.raises(ValidationError):
        ProvisioningRetainedListOutput.model_validate(
            {"items": [{**summary.model_dump(), "credential_ref": "secret-ref"}], "next_key": None}
        )


@pytest.mark.parametrize("value", [True, 1.0, "1"])
def test_versions_revisions_limits_and_ids_reject_coercion(value: object) -> None:
    with pytest.raises(ValidationError):
        ProvisioningStatus.model_validate(
            {
                "version": value,
                "revision": 0,
                "state": "PENDING",
                "error_code": None,
                "updated_at": UTC,
                "last_attempt_at": None,
                "next_retry_at": None,
                "can_retry": False,
            }
        )
    with pytest.raises(ValidationError):
        ProvisioningRetryInput(expected_revision=value)
    with pytest.raises(ValidationError):
        ProvisioningTasksInput(limit=value)
    with pytest.raises(ValidationError):
        ProvisioningTasksInput.model_validate({"limit": 101})
    with pytest.raises(ValidationError):
        ProvisioningConfigSaveInput(
            expected_revision=0,
            expected_anchor_revision=0,
            name="name",
            base_url="https://weknora.example",
            enabled="true",
        )


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-10-25T12:34:56",
        "2026-10-25T12:34:56+01:00",
        "2026-10-25X12:34:56Z",
        "2026-02-30T12:34:56Z",
        "not-a-timestamp",
    ],
)
def test_public_timestamps_require_real_explicit_utc(timestamp: str) -> None:
    with pytest.raises(ValidationError):
        ProvisioningStatus(
            revision=0,
            state="PENDING",
            error_code=None,
            updated_at=timestamp,
            last_attempt_at=None,
            next_retry_at=None,
            can_retry=False,
        )


@pytest.mark.parametrize(
    "timestamp",
    [
        "2026-10-25T12:34:56Z",
        "2026-10-25T12:34:56+00:00",
        "20261025T123456Z",
        "2026-W43-7T12:34:56+00:00",
    ],
)
def test_public_timestamps_accept_iso8601_utc_forms(timestamp: str) -> None:
    status = ProvisioningStatus(
        revision=0,
        state="PENDING",
        error_code=None,
        updated_at=timestamp,
        last_attempt_at=None,
        next_retry_at=None,
        can_retry=False,
    )
    assert status.updated_at == timestamp


def test_state_error_uuid_and_list_element_domains_are_closed() -> None:
    status = {
        "revision": 1,
        "state": "PENDING",
        "error_code": None,
        "updated_at": UTC,
        "last_attempt_at": None,
        "next_retry_at": None,
        "can_retry": False,
    }
    with pytest.raises(ValidationError):
        ProvisioningStatus.model_validate({**status, "state": "OTHER"})
    with pytest.raises(ValidationError):
        ProvisioningStatus.model_validate({**status, "error_code": "REMOTE_ID"})
    with pytest.raises(ValidationError):
        ProvisioningTaskRetryInput(
            task_id="123E4567-E89B-42D3-A456-426614174000",
            expected_revision=1,
            reason="retry",
        )
    with pytest.raises(ValidationError):
        ProvisioningTaskRetryInput(
            task_id="123e4567-e89b-12d3-a456-426614174000",
            expected_revision=1,
            reason="retry",
        )
    with pytest.raises(ValidationError):
        ProvisioningTasksOutput.model_validate({"items": ["not-a-summary"], "next_user_id": None})
    with pytest.raises(ValidationError):
        ProvisioningTaskRetryInput(task_id=UUID4, expected_revision=1, reason=123)


def test_retry_reason_and_secret_require_trimmed_nonempty_text() -> None:
    assert (
        ProvisioningTaskRetryInput(
            task_id=UUID4, expected_revision=1, reason="  Retry this  "
        ).reason
        == "Retry this"
    )
    for reason in ("  ", "x" * 501):
        with pytest.raises(ValidationError):
            ProvisioningTaskRetryInput(task_id=UUID4, expected_revision=1, reason=reason)
    for token in ("", "  token", "token "):
        with pytest.raises(ValidationError):
            ProvisioningConfigSaveInput(
                expected_revision=0,
                expected_anchor_revision=0,
                name="name",
                base_url="https://weknora.example",
                enabled=True,
                system_admin_jwt=token,
            )


def test_null_fields_are_required_but_nullable_and_actor_fields_are_absent() -> None:
    with pytest.raises(ValidationError):
        ProvisioningStatus(
            revision=0,
            state="PENDING",
            error_code=None,
            updated_at=UTC,
            last_attempt_at=None,
            can_retry=False,
        )
    assert "user_id" not in ProvisioningRetryInput.model_json_schema()["properties"]
    assert "system_admin_jwt" not in ProvisioningConfig.model_json_schema()["properties"]
    with pytest.raises(ValidationError):
        ProvisioningRetryInput.model_validate({"expected_revision": 0, "user_id": 9})
    with pytest.raises(ValidationError):
        ProvisioningRetainedListInput.model_validate({"namespace": "other", "limit": 50})


def test_status_updated_at_is_required_but_nullable() -> None:
    status = {
        "revision": 0,
        "state": "PENDING",
        "error_code": None,
        "updated_at": None,
        "last_attempt_at": None,
        "next_retry_at": None,
        "can_retry": False,
    }
    assert ProvisioningStatus.model_validate(status).updated_at is None
    with pytest.raises(ValidationError):
        ProvisioningStatus.model_validate(
            {key: value for key, value in status.items() if key != "updated_at"}
        )


def test_jwt_null_and_omitted_are_both_accepted() -> None:
    common = {
        "expected_revision": 0,
        "expected_anchor_revision": 0,
        "name": "name",
        "base_url": "https://weknora.example",
        "enabled": True,
    }
    omitted = ProvisioningConfigSaveInput.model_validate(common)
    explicit_null = ProvisioningConfigSaveInput.model_validate({**common, "system_admin_jwt": None})

    assert omitted.system_admin_jwt is None
    assert explicit_null.system_admin_jwt is None


@pytest.mark.parametrize("name", ["  ", "x" * 81])
def test_config_names_reject_empty_and_overlong_values(name: str) -> None:
    with pytest.raises(ValidationError):
        ProvisioningConfigSaveInput(
            expected_revision=0,
            expected_anchor_revision=0,
            name=name,
            base_url="https://weknora.example",
            enabled=False,
        )
    with pytest.raises(ValidationError):
        ProvisioningConfig(
            revision=0,
            anchor_revision=0,
            name=name,
            base_url=None,
            enabled=False,
            credential_configured=False,
            updated_at=None,
        )


def test_remote_plain_http_is_rejected_by_the_shared_url_policy() -> None:
    with pytest.raises(ValidationError):
        ProvisioningConfigSaveInput(
            expected_revision=0,
            expected_anchor_revision=0,
            name="name",
            base_url="http://weknora.example/api/v1",
            enabled=False,
        )
