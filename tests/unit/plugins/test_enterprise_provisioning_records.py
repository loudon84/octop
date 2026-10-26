from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

PLUGIN_ROOT = Path(__file__).resolve().parents[3] / "plugins" / "enterprise-knowledge"
sys.path.insert(0, str(PLUGIN_ROOT.resolve()))


def _identity() -> dict[str, Any]:
    return {
        "schema_id": "octop.enterprise.provisioning.v1",
        "version": 1,
        "user_id": 9,
        "email": "employee@example.test",
        "disabled": False,
        "snapshot_token": "identity-snapshot",
    }


def _task_payload() -> dict[str, Any]:
    return {
        "schema_id": "octop.enterprise.provisioning.v1",
        "version": 1,
        "task_id": str(uuid4()),
        "identity": _identity(),
        "state": "PENDING",
        "error_code": None,
        "checkpoint": "NONE",
        "domain_id": None,
        "base_url": None,
        "user_intent_id": None,
        "kb_intent_id": None,
        "key_intent_id": None,
        "bootstrap_ref": None,
        "bootstrap_revision": None,
        "bootstrap_created_at": None,
        "bootstrap_expires_at": None,
        "final_key_ref": None,
        "final_key_revision": None,
        "remote_user_id": None,
        "tenant_id": None,
        "kb_id": None,
        "key_id": None,
        "attempt": None,
        "consecutive_failures": 0,
        "updated_at": "2026-10-25T12:34:56Z",
        "last_attempt_at": None,
        "next_retry_at": None,
        "last_retry": None,
    }


@pytest.mark.parametrize("header", ["schema_id", "version"])
def test_task_record_requires_explicit_persistent_headers(header: str) -> None:
    from enterprise_knowledge.provisioning_records import ProvisioningTaskRecord

    payload = _task_payload()
    payload.pop(header)
    with pytest.raises(ValidationError):
        ProvisioningTaskRecord.model_validate(payload)


@pytest.mark.parametrize("bad", [0, -1, True, 1.0, "1", 18_446_744_073_709_551_616])
def test_task_key_id_is_strict_uint64(bad: object) -> None:
    from enterprise_knowledge.provisioning_records import ProvisioningTaskRecord

    with pytest.raises(ValidationError):
        ProvisioningTaskRecord.model_validate({**_task_payload(), "key_id": bad})


@pytest.mark.parametrize("key_id", [1, 18_446_744_073_709_551_615])
def test_task_key_id_preserves_uint64_integer_boundaries(key_id: int) -> None:
    from enterprise_knowledge.provisioning_records import ProvisioningTaskRecord

    parsed = ProvisioningTaskRecord.model_validate({**_task_payload(), "key_id": key_id})
    assert type(parsed.key_id) is int
    assert parsed.key_id == key_id


def test_task_remote_ids_keep_exact_text_and_enforce_codepoint_limit() -> None:
    from enterprise_knowledge.provisioning_records import ProvisioningTaskRecord

    exact = "x" * 36
    parsed = ProvisioningTaskRecord.model_validate({**_task_payload(), "remote_user_id": exact})
    assert parsed.remote_user_id == exact
    with pytest.raises(ValidationError):
        ProvisioningTaskRecord.model_validate({**_task_payload(), "remote_user_id": exact + "x"})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("domain_id", "not-a-uuid"),
        ("base_url", ""),
        ("base_url", "https://example.test/api/v1/"),
    ],
)
def test_task_domain_and_base_url_are_canonical(field: str, value: str) -> None:
    from enterprise_knowledge.provisioning_records import ProvisioningTaskRecord

    with pytest.raises(ValidationError):
        ProvisioningTaskRecord.model_validate(
            {
                **_task_payload(),
                "domain_id": str(uuid4()),
                "base_url": "https://example.test/api/v1",
                field: value,
            }
        )


def test_task_record_rejects_unknown_version_extra_and_missing_nullable_field() -> None:
    from enterprise_knowledge.provisioning_records import (
        ProvisioningIdentity,
        ProvisioningTaskRecord,
    )

    with pytest.raises(ValidationError):
        ProvisioningTaskRecord.model_validate({**_task_payload(), "version": 2})
    with pytest.raises(ValidationError):
        ProvisioningTaskRecord.model_validate({**_task_payload(), "unexpected": "value"})
    identity = _identity()
    del identity["email"]
    with pytest.raises(ValidationError):
        ProvisioningIdentity.model_validate(identity)


@pytest.mark.parametrize(
    ("remote_id", "valid"),
    [("😀" * 36, True), ("😀" * 37, False), ("\ud800", False)],
)
def test_task_remote_ids_count_unicode_codepoints_and_reject_invalid_utf8(
    remote_id: str, valid: bool
) -> None:
    from enterprise_knowledge.provisioning_records import ProvisioningTaskRecord

    if valid:
        assert (
            ProvisioningTaskRecord.model_validate(
                {**_task_payload(), "remote_user_id": remote_id}
            ).remote_user_id
            == remote_id
        )
    else:
        with pytest.raises(ValidationError):
            ProvisioningTaskRecord.model_validate({**_task_payload(), "remote_user_id": remote_id})


@pytest.mark.parametrize(
    ("resource_kind", "kb_id", "key_id", "valid"),
    [
        ("USER", None, None, True),
        ("USER", "kb", None, False),
        ("KB", "kb", None, True),
        ("KB", None, None, False),
        ("KEY", "kb", 23, True),
        ("KEY", "kb", None, False),
        ("KB", "kb", 23, False),
    ],
)
def test_receipt_resource_shape_is_strict(
    resource_kind: str, kb_id: str | None, key_id: int | None, valid: bool
) -> None:
    from enterprise_knowledge.provisioning_records import ProvisioningReceiptRecord

    receipt = {
        "schema_id": "octop.enterprise.provisioning.v1",
        "version": 1,
        "intent_id": str(uuid4()),
        "domain_id": str(uuid4()),
        "local_user_id": 9,
        "resource_kind": resource_kind,
        "proof_kind": "CREATED_201",
        "task_id": str(uuid4()),
        "source_attempt_id": str(uuid4()),
        "recorded_at": "2026-10-25T12:34:56Z",
        "remote_user_id": "remote-user",
        "tenant_id": 17,
        "kb_id": kb_id,
        "key_id": key_id,
    }
    if valid:
        assert ProvisioningReceiptRecord.model_validate(receipt).resource_kind == resource_kind
    else:
        with pytest.raises(ValidationError):
            ProvisioningReceiptRecord.model_validate(receipt)


def test_task_bootstrap_deadline_pair_and_retry_audit_shape_are_strict() -> None:
    from enterprise_knowledge.provisioning_records import ProvisioningTaskRecord

    with pytest.raises(ValidationError):
        ProvisioningTaskRecord.model_validate(
            {**_task_payload(), "bootstrap_ref": str(uuid4()), "bootstrap_revision": 2}
        )
    malformed_audit = {
        "schema_id": "octop.enterprise.provisioning.v1",
        "version": 1,
        "actor_user_id": 9,
        "kind": "SELF",
        "requested_at": "2026-10-25T12:34:56Z",
        "reason": "not-allowed-for-self",
    }
    from enterprise_knowledge.provisioning_records import ProvisioningRetryAudit

    with pytest.raises(ValidationError):
        ProvisioningRetryAudit.model_validate(malformed_audit)


def test_bootstrap_deadline_is_fixed_at_twenty_four_hours() -> None:
    from uuid import uuid4

    from enterprise_knowledge.provisioning_records import ProvisioningTaskRecord

    payload = {
        **_task_payload(),
        "bootstrap_ref": str(uuid4()),
        "bootstrap_revision": 2,
        "bootstrap_created_at": "2026-10-23T12:00:00Z",
        "bootstrap_expires_at": "2026-10-24T12:00:00Z",
    }
    assert (
        ProvisioningTaskRecord.model_validate(payload).bootstrap_expires_at
        == "2026-10-24T12:00:00Z"
    )
    with pytest.raises(ValidationError):
        ProvisioningTaskRecord.model_validate(
            {**payload, "bootstrap_expires_at": "2026-10-24T12:00:01Z"}
        )


@pytest.mark.parametrize(
    ("resource_kind", "tenant_id", "remote_id", "valid"),
    [
        ("USER", None, "opaque-user", True),
        ("KB", 5, "opaque-kb", True),
        ("TENANT", None, "18446744073709551615", True),
        ("KEY", 5, "23", True),
        ("TENANT", None, "01", False),
        ("TENANT", None, "18446744073709551616", False),
        ("KEY", None, "23", False),
    ],
)
def test_resource_claim_ids_preserve_typed_scope_and_canonical_uint64(
    resource_kind: str, tenant_id: int | None, remote_id: str, valid: bool
) -> None:
    from uuid import uuid4

    from enterprise_knowledge.provisioning_records import ProvisioningResourceClaimRecord

    payload = {
        "schema_id": "octop.enterprise.provisioning.v1",
        "version": 1,
        "domain_id": str(uuid4()),
        "resource_kind": resource_kind,
        "tenant_id": tenant_id,
        "remote_id": remote_id,
        "local_user_id": 9,
        "task_id": str(uuid4()),
        "intent_id": str(uuid4()),
        "receipt_id": str(uuid4()),
        "claimed_at": "2026-10-25T12:34:56Z",
    }
    if valid:
        assert ProvisioningResourceClaimRecord.model_validate(payload).remote_id == remote_id
    else:
        with pytest.raises(ValidationError):
            ProvisioningResourceClaimRecord.model_validate(payload)
