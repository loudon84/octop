"""Cache-Control for dashboard SPA shell vs hashed Vite assets."""

from __future__ import annotations

from pathlib import Path

import pytest
from starlette import responses

from octop.api.app import _dashboard_response, dashboard_cache_control, is_dashboard_asset_path


def test_shell_files_are_revalidated() -> None:
    assert dashboard_cache_control("") == "no-cache"
    assert dashboard_cache_control("index.html") == "no-cache"
    assert dashboard_cache_control("sw.js") == "no-cache"
    assert dashboard_cache_control("manifest.json") == "no-cache"


def test_hashed_assets_are_immutable() -> None:
    assert (
        dashboard_cache_control("assets/index.abc123.js") == "public, max-age=31536000, immutable"
    )
    assert (
        dashboard_cache_control("assets/vendor-utils.Xyz.css")
        == "public, max-age=31536000, immutable"
    )


def test_other_static_files_leave_cache_unset() -> None:
    assert dashboard_cache_control("logo_vertical_dark.svg") is None
    assert dashboard_cache_control("offline.html") is None


def test_only_hashed_assets_skip_the_spa_fallback() -> None:
    assert is_dashboard_asset_path("assets/index.abc123.js") is True
    assert is_dashboard_asset_path("chat") is False
    assert is_dashboard_asset_path("") is False


@pytest.mark.parametrize(
    ("extension", "content_type"),
    (
        ("js", "text/javascript; charset=utf-8"),
        ("mjs", "text/javascript; charset=utf-8"),
        ("css", "text/css; charset=utf-8"),
    ),
)
def test_dashboard_assets_use_browser_safe_mime_types_when_system_mapping_is_wrong(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    extension: str,
    content_type: str,
) -> None:
    asset = tmp_path / f"bundle.{extension}"
    asset.write_text("asset", encoding="utf-8")
    monkeypatch.setattr(responses, "guess_type", lambda _path: ("text/plain", None))

    response = _dashboard_response(asset, f"assets/{asset.name}")

    assert response.headers["content-type"] == content_type
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_other_dashboard_extensions_keep_system_mime_inference(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset = tmp_path / "bundle.svg"
    asset.write_text("asset", encoding="utf-8")
    monkeypatch.setattr(responses, "guess_type", lambda _path: ("text/plain", None))

    response = _dashboard_response(asset, f"assets/{asset.name}")

    assert response.headers["content-type"] == "text/plain; charset=utf-8"
