from __future__ import annotations

import sys
from importlib import import_module
from pathlib import Path

import pytest
from pydantic import ValidationError

_PLUGIN_ROOT = Path(__file__).resolve().parents[3] / "plugins" / "enterprise-knowledge"
sys.path.insert(0, str(_PLUGIN_ROOT.resolve()))
_models = import_module("enterprise_knowledge.models")
ConnectionSaveInput = _models.ConnectionSaveInput
normalize_base_url = _models.normalize_base_url


def test_base_url_canonicalizes_api_v1_and_idna_host() -> None:
    assert normalize_base_url("https://faß.example/api/v1/api/v1/") == (
        "https://xn--fa-hia.example/api/v1"
    )
    assert normalize_base_url("https://knowledge.example/api/v1") == (
        "https://knowledge.example/api/v1"
    )


@pytest.mark.parametrize(
    "value",
    [
        "https://knowledge.example/tenant",
        "https://knowledge.example/a%2Fb",
        "https://knowledge.example/a%23b",
        "https://knowledge.example/a%3Fb",
    ],
)
def test_base_url_rejects_non_root_path_before_mutation(value: str) -> None:
    with pytest.raises(ValidationError):
        ConnectionSaveInput(name="Knowledge", base_url=value, api_key="secret")


@pytest.mark.parametrize(
    "value",
    [
        "http://knowledge.example",
        "http://user@knowledge.example",
        "https://user:password@knowledge.example",
        "https://knowledge.example/api/v1?",
        "https://knowledge.example/api/v1#",
        "ftp://knowledge.example",
    ],
)
def test_base_url_rejects_unapproved_or_ambiguous_urls(value: str) -> None:
    with pytest.raises(ValidationError):
        ConnectionSaveInput(name="Knowledge", base_url=value, api_key="secret")


def test_remote_http_requires_canonical_exact_test_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCTOP_WEKNORA_TEST_HTTP_ORIGIN", "http://faß.example:80")
    assert normalize_base_url("http://faß.example/api/v1") == ("http://xn--fa-hia.example/api/v1")
    monkeypatch.setenv("OCTOP_WEKNORA_TEST_HTTP_ORIGIN", "http://fass.example")
    with pytest.raises(ValueError):
        normalize_base_url("http://faß.example")


@pytest.mark.parametrize(
    ("base_url", "allowed_origin"),
    [
        ("http://weknora.example:0", "http://weknora.example"),
        ("http://weknora.example", "http://weknora.example:0"),
        ("http://weknora.example:/api/v1", "http://weknora.example"),
        ("http://weknora.example/api/v1", "http://weknora.example:"),
    ],
)
def test_explicit_port_zero_is_not_the_default_http_port(
    base_url: str, allowed_origin: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OCTOP_WEKNORA_TEST_HTTP_ORIGIN", allowed_origin)
    with pytest.raises(ValueError):
        normalize_base_url(base_url)


def test_new_connection_requires_nonempty_write_only_key_and_edit_can_reuse_it() -> None:
    with pytest.raises(ValidationError):
        ConnectionSaveInput(name="Knowledge", base_url="https://knowledge.example")
    with pytest.raises(ValidationError):
        ConnectionSaveInput(name="Knowledge", base_url="https://knowledge.example", api_key="")
    request = ConnectionSaveInput(
        connection_id="00000000-0000-4000-8000-000000000001",
        expected_revision=1,
        name="Knowledge",
        base_url="https://knowledge.example",
    )
    assert request.api_key is None
    assert "secret" not in repr(request)


@pytest.mark.parametrize("tenant_id", ["", "../tenant", "tenant\nname"])
def test_tenant_ids_reject_empty_path_and_control_values(tenant_id: str) -> None:
    with pytest.raises(ValidationError):
        ConnectionSaveInput(
            name="Knowledge",
            base_url="https://knowledge.example",
            api_key="secret",
            tenant_id=tenant_id,
        )
