from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from octop.infra.db.migrate import run_migrations
from octop.infra.db.pool import SqlitePool
from octop.infra.db.repos.plugin_retained_records import PluginRetainedRecordRepo


@pytest.fixture
def db(tmp_path: Path) -> SqlitePool:
    pool = SqlitePool(tmp_path / "octop.db")
    run_migrations(pool)
    return pool


def _envelope(revision: int, payload: dict[str, object]) -> str:
    return json.dumps(
        {"format_version": 1, "revision": revision, "payload": payload},
        separators=(",", ":"),
    )


def test_retained_keys_are_encoded_and_prefix_pages_are_literal_and_stable(
    db: SqlitePool,
) -> None:
    repo = PluginRetainedRecordRepo(db)
    for key in ("part%_one", "part%_two", "partXXone", "other"):
        assert (
            repo.cas(
                "plugin-a",
                "notes:東京",
                key,
                payload_json=_envelope(1, {"key": key}),
                expected_revision=0,
            )
            is not None
        )

    with db.connect() as conn:
        stored = conn.execute(
            "SELECT key FROM settings WHERE substr(key, 1, length(?)) = ? ORDER BY key",
            ("octop.plugin-retained.v1:", "octop.plugin-retained.v1:"),
        ).fetchall()
    assert stored[0]["key"].startswith("octop.plugin-retained.v1:")
    assert "part%_one" not in stored[0]["key"]
    assert all(
        part == part.lower() and all(ch in "0123456789abcdef" for ch in part)
        for part in stored[0]["key"].split(":")[1:]
    )

    first = repo.list_prefix_page("plugin-a", "notes:東京", "part%_", after_key=None, limit=1)
    second = repo.list_prefix_page(
        "plugin-a", "notes:東京", "part%_", after_key=first.next_logical_key, limit=1
    )
    assert [row.logical_key for row in first.items + second.items] == ["part%_one", "part%_two"]
    assert first.next_logical_key == "part%_one"
    assert second.next_logical_key is None
    assert (
        repo.list_prefix_page("plugin-b", "notes:東京", "part%_", after_key=None, limit=10).items
        == ()
    )


def test_retained_cas_uses_expected_revision_and_rejects_malformed_envelopes(
    db: SqlitePool,
) -> None:
    repo = PluginRetainedRecordRepo(db)
    created = repo.cas(
        "plugin-a",
        "proof",
        "key",
        payload_json=_envelope(1, {"step": "intent"}),
        expected_revision=0,
    )
    assert created is not None and created.revision == 1
    assert (
        repo.cas(
            "plugin-a",
            "proof",
            "key",
            payload_json=_envelope(1, {"lost": True}),
            expected_revision=0,
        )
        is None
    )
    updated = repo.cas(
        "plugin-a",
        "proof",
        "key",
        payload_json=_envelope(2, {"step": "receipt"}),
        expected_revision=1,
    )
    assert updated is not None and updated.revision == 2
    assert (
        repo.cas("plugin-a", "proof", "key", payload_json=_envelope(2, {}), expected_revision=1)
        is None
    )

    with db.transaction() as conn:
        conn.execute(
            "UPDATE settings SET value = ? WHERE key = ?",
            (
                '{"format_version":true,"revision":1,"payload":{}}',
                "octop.plugin-retained.v1:706c7567696e2d61:70726f6f66:6b6579",
            ),
        )
    with pytest.raises(ValueError):
        repo.get("plugin-a", "proof", "key")
    with db.transaction() as conn:
        conn.execute(
            "UPDATE settings SET value = ? WHERE key = ?",
            (
                '{"format_version":1,"format_version":1,"revision":1,"payload":{}}',
                "octop.plugin-retained.v1:706c7567696e2d61:70726f6f66:6b6579",
            ),
        )
    with pytest.raises(ValueError):
        repo.get("plugin-a", "proof", "key")


def test_retained_cas_conflict_is_deterministic_across_independent_pools(
    db: SqlitePool,
) -> None:
    second_db = SqlitePool(db.path)
    first_repo = PluginRetainedRecordRepo(db)
    second_repo = PluginRetainedRecordRepo(second_db)
    assert (
        first_repo.cas(
            "plugin-a",
            "proof",
            "race",
            payload_json=_envelope(1, {"n": 0}),
            expected_revision=0,
        )
        is not None
    )
    start = threading.Barrier(3)

    def update(repo: PluginRetainedRecordRepo, value: int):
        start.wait(timeout=3)
        return repo.cas(
            "plugin-a",
            "proof",
            "race",
            payload_json=_envelope(2, {"n": value}),
            expected_revision=1,
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(update, first_repo, 1)
            second = executor.submit(update, second_repo, 2)
            start.wait(timeout=3)
            winners = (first.result(timeout=5), second.result(timeout=5))
        assert sum(row is not None for row in winners) == 1
        result = first_repo.get("plugin-a", "proof", "race")
        assert result is not None and result.revision == 2
        assert result.payload in ({"n": 1}, {"n": 2})
    finally:
        second_db.close()
