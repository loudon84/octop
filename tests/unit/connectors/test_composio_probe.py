from scripts.composio_probe import _oauth_summary


def test_oauth_summary_reports_presence_without_exposing_tokens() -> None:
    tokens = {
        "access_token": "access-secret",
        "refresh_token": "refresh-secret",
        "id_token": "id-secret",
        "client_secret": "client-secret",
    }

    summary = _oauth_summary(tokens)

    assert summary == {
        "has_access_token": True,
        "has_refresh_token": True,
        "has_id_token": True,
    }
    assert not set(summary).intersection(tokens)
    assert not any(secret in str(summary) for secret in tokens.values())
