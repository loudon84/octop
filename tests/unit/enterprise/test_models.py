from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path

import pytest
from pydantic import ValidationError

_PLUGIN_ROOT = Path(__file__).resolve().parents[3] / "plugins" / "enterprise-knowledge"
sys.path.insert(0, str(_PLUGIN_ROOT.resolve()))
_models = import_module("enterprise_knowledge.models")
BindingConfig = _models.BindingConfig
ConnectionConfig = _models.ConnectionConfig
GrantConfig = _models.GrantConfig
BindingOutput = _models.BindingOutput
ConnectionSaveOutput = _models.ConnectionSaveOutput
GrantSaveOutput = _models.GrantSaveOutput
SettingsOutput = _models.SettingsOutput
EvidenceRecord = _models.EvidenceRecord
SessionSaveInput = _models.SessionSaveInput
SessionCapabilities = _models.SessionCapabilities


def test_p07c1_service_dtos_are_strict_and_observation_is_backward_compatible() -> None:
    operation = _models.OperationRecord(
        operation_id="00000000-0000-4000-8000-000000000001",
        revision=1,
        user_id=9,
        agent_id="agent-1",
        thread_id="thread-1",
        session_revision=0,
        config_revision=1,
        binding_revision=1,
        grant_revision=1,
        connection_id="00000000-0000-4000-8000-000000000002",
        status="VALIDATING",
    )
    assert operation.observed_knowledge_bases is None
    assert _models.KnowledgeBaseSummary(kb_id="kb-1", name=None).model_dump() == {
        "kb_id": "kb-1",
        "name": None,
    }
    request = _models.OperationPrepareInput(expected_session_revision=0)
    assert request.version == 1
    for invalid in (True, 1.0, "1"):
        with pytest.raises(ValidationError):
            _models.OperationPrepareInput.model_validate({"expected_session_revision": invalid})
    with pytest.raises(ValidationError):
        _models.OperationPrepareInput.model_validate(
            {"expected_session_revision": 0, "operation_id": operation.operation_id}
        )


def test_enterprise_records_are_strict_and_default_scopes_are_empty() -> None:
    connection = ConnectionConfig.model_validate(
        {
            "schema_id": "octop.enterprise.knowledge.v1",
            "version": 1,
            "connection_id": "00000000-0000-4000-8000-000000000001",
            "revision": 1,
            "name": "Research",
            "base_url": "https://knowledge.example/api/v1",
            "enabled": False,
            "credential_configured": True,
            "probe_status": "CONFIGURED",
        }
    )
    assert connection.allowed_kb_ids == []
    assert connection.version == 1
    assert connection.model_config["frozen"] is True
    with pytest.raises(ValidationError):
        ConnectionConfig.model_validate({**connection.model_dump(), "unexpected": True})
    with pytest.raises(ValidationError):
        connection.enabled = True

    grant = GrantConfig.model_validate(
        {
            "schema_id": "octop.enterprise.knowledge.v1",
            "version": 1,
            "connection_id": connection.connection_id,
            "revision": 0,
            "user_id": 9,
        }
    )
    binding = BindingConfig.model_validate(
        {
            "schema_id": "octop.enterprise.knowledge.v1",
            "version": 1,
            "agent_id": "agent-1",
            "connection_ref": connection.connection_id,
        }
    )
    assert grant.user_allowed_kb_ids == []
    assert binding.expert_allowed_kb_ids == []
    assert binding.default_kb_ids == []
    with pytest.raises(ValidationError):
        BindingConfig.model_validate(
            {
                **binding.model_dump(),
                "expert_allowed_kb_ids": ["kb-1"],
                "default_kb_ids": ["kb-2"],
            }
        )


@pytest.mark.parametrize("revision", [True, -1, 1.0, "1"])
def test_revision_rejects_bool_negative_float_and_coercion(revision: object) -> None:
    valid = {
        "schema_id": "octop.enterprise.knowledge.v1",
        "version": 1,
        "connection_id": "00000000-0000-4000-8000-000000000001",
        "revision": 1,
        "name": "Research",
        "base_url": "https://knowledge.example/api/v1",
        "enabled": False,
        "credential_configured": True,
        "probe_status": "CONFIGURED",
    }
    assert ConnectionConfig.model_validate(valid).revision == 1
    with pytest.raises(ValidationError):
        ConnectionConfig.model_validate({**valid, "revision": revision})


def test_schema_version_uuid_and_ids_are_not_coerced_or_path_like() -> None:
    base = {
        "schema_id": "octop.enterprise.knowledge.v1",
        "version": 1,
        "connection_id": "00000000-0000-4000-8000-000000000001",
        "revision": 0,
        "name": "Research",
        "base_url": "https://knowledge.example/api/v1",
        "enabled": False,
        "credential_configured": True,
        "probe_status": "CONFIGURED",
    }
    for changes in (
        {"version": True},
        {"version": 2},
        {"connection_id": "00000000-0000-1000-8000-000000000001"},
        {"connection_id": "../escape"},
    ):
        with pytest.raises(ValidationError):
            ConnectionConfig.model_validate({**base, **changes})


@pytest.mark.parametrize("version", [True, 1.0, "1"])
def test_output_wrappers_reject_non_integer_schema_version(version: object) -> None:
    connection = ConnectionConfig(
        connection_id="00000000-0000-4000-8000-000000000001",
        revision=0,
        name="Research",
        base_url="https://knowledge.example",
        enabled=False,
        credential_configured=False,
        probe_status="DISABLED",
    )
    grant = GrantConfig(
        connection_id=connection.connection_id,
        revision=0,
        user_id=1,
    )
    binding = BindingConfig(agent_id="agent-1")
    for model, payload in (
        (SettingsOutput, {"capabilities": {}}),
        (ConnectionSaveOutput, {"connection": connection}),
        (GrantSaveOutput, {"grant": grant}),
        (BindingOutput, {"binding": binding}),
    ):
        assert model.model_validate(payload).version == 1
        with pytest.raises(ValidationError):
            model.model_validate({**payload, "version": version})


def test_session_snapshot_distinguishes_missing_from_explicit_empty_selection() -> None:
    session = _models.SessionSnapshot(
        agent_id="agent-1",
        thread_id="thread-1",
        revision=0,
        selection_present=False,
        selected_kb_ids=["kb-default"],
        mode="KB_ONLY",
    )
    assert session.selection_present is False
    assert session.selected_kb_ids == ["kb-default"]
    empty = session.model_copy(
        update={"revision": 1, "selection_present": True, "selected_kb_ids": []}
    )
    assert empty.selection_present is True
    assert empty.selected_kb_ids == []


def test_session_capabilities_are_strict_and_do_not_mean_readiness() -> None:
    capabilities = SessionCapabilities(
        can_save_selection=True,
        can_select_knowledge=True,
        knowledge_ready=False,
        web_ready=False,
    )
    assert capabilities.can_save_selection is True
    assert capabilities.can_select_knowledge is True
    assert capabilities.knowledge_ready is False
    assert capabilities.web_ready is False
    with pytest.raises(ValidationError):
        SessionCapabilities.model_validate(
            {
                "can_save_selection": 1,
                "can_select_knowledge": True,
                "knowledge_ready": False,
                "web_ready": False,
            }
        )


def test_session_save_requires_explicit_strict_source_revisions() -> None:
    base = {
        "expected_revision": 0,
        "selected_kb_ids": [],
        "source_revisions": {
            "connection_id": None,
            "config_revision": 0,
            "binding_revision": 0,
            "grant_revision": 0,
        },
    }
    assert SessionSaveInput.model_validate(base).source_revisions.binding_revision == 0
    for invalid in (
        {key: value for key, value in base.items() if key != "source_revisions"},
        {**base, "source_revisions": {"connection_id": None, "config_revision": 0}},
        {**base, "source_revisions": {**base["source_revisions"], "other": 0}},
        {
            **base,
            "source_revisions": {**base["source_revisions"], "grant_revision": True},
        },
        {
            **base,
            "source_revisions": {**base["source_revisions"], "grant_revision": 1.0},
        },
        {
            **base,
            "source_revisions": {**base["source_revisions"], "grant_revision": "1"},
        },
        {
            **base,
            "source_revisions": {
                **base["source_revisions"],
                "connection_id": "00000000-0000-4000-8000-00000000000A",
            },
        },
    ):
        with pytest.raises(ValidationError):
            SessionSaveInput.model_validate(invalid)


def test_locale_lookup_is_preloaded_for_async_action_use(monkeypatch: pytest.MonkeyPatch) -> None:
    i18n = import_module("enterprise_knowledge.i18n")

    def no_disk_read(_self: Path, *_args: object, **_kwargs: object) -> str:
        raise AssertionError("locale lookup performed a filesystem read")

    monkeypatch.setattr(Path, "read_text", no_disk_read)
    assert i18n.error_message("ACCESS_DENIED", "zh") == "你无权管理此配置"


def test_knowledge_evidence_requires_real_source_ids_and_utc_time() -> None:
    evidence = {
        "evidence_id": "00000000-0000-4000-8000-000000000001",
        "operation_id": "00000000-0000-4000-8000-000000000002",
        "user_id": 1,
        "agent_id": "agent-1",
        "thread_id": "thread-1",
        "source_kind": "KNOWLEDGE",
        "provider": "WEKNORA",
        "connection_id": "00000000-0000-4000-8000-000000000003",
        "kb_id": "kb-1",
        "knowledge_id": "doc-1",
        "content": "verified source content",
        "truncated": False,
        "retrieved_at": "2026-10-25T00:00:00+00:00",
    }
    assert EvidenceRecord.model_validate(evidence).content == "verified source content"
    with pytest.raises(ValidationError):
        EvidenceRecord.model_validate(
            {key: value for key, value in evidence.items() if key != "kb_id"}
        )
    with pytest.raises(ValidationError):
        EvidenceRecord.model_validate({**evidence, "retrieved_at": "2026-10-25T08:00:00+08:00"})
    with pytest.raises(ValidationError):
        EvidenceRecord.model_validate({**evidence, "provider": "SEARXNG"})

    web = {
        **evidence,
        "source_kind": "WEB",
        "provider": "SEARXNG",
        "search_revision": 3,
        "connection_id": None,
        "kb_id": None,
        "knowledge_id": None,
        "locator": "https://result.example/source",
    }
    assert EvidenceRecord.model_validate(web).provider == "SEARXNG"
    with pytest.raises(ValidationError):
        EvidenceRecord.model_validate({**web, "kb_id": "forged-kb"})
