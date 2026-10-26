"""Durable plugin proof records stored in the existing settings table."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool

_KEY_PREFIX = "octop.plugin-retained.v1:"


@dataclass(frozen=True)
class RetainedRecordData:
    namespace: str
    logical_key: str
    revision: int
    payload: dict[str, Any]


@dataclass(frozen=True)
class RetainedRecordPageData:
    items: tuple[RetainedRecordData, ...]
    next_logical_key: str | None


def _key(plugin_id: str, namespace: str, logical_key: str) -> str:
    parts = (plugin_id, namespace, logical_key)
    return _KEY_PREFIX + ":".join(part.encode("utf-8").hex() for part in parts)


def _decode(value: str, namespace: str, logical_key: str) -> RetainedRecordData:
    def object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, item in pairs:
            if name in result:
                raise ValueError
            result[name] = item
        return result

    try:
        envelope = json.loads(value, object_pairs_hook=object_without_duplicates)
        if (
            not isinstance(envelope, dict)
            or set(envelope) != {"format_version", "revision", "payload"}
            or type(envelope["format_version"]) is not int
            or envelope["format_version"] != 1
            or type(envelope["revision"]) is not int
            or envelope["revision"] < 1
            or not isinstance(envelope["payload"], dict)
        ):
            raise ValueError
        payload = json.loads(json.dumps(envelope["payload"], allow_nan=False))
    except (RecursionError, TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("invalid retained record") from None
    return RetainedRecordData(namespace, logical_key, envelope["revision"], payload)


class PluginRetainedRecordRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def lock_for_checks(self, conn: Any) -> None:
        """Serialize retained absent-key checks and writes on PostgreSQL."""
        if self._db.dialect == "postgresql":
            conn.execute("LOCK TABLE settings IN SHARE ROW EXCLUSIVE MODE")

    def get(
        self,
        plugin_id: str,
        namespace: str,
        logical_key: str,
        *,
        conn: Any | None = None,
    ) -> RetainedRecordData | None:
        if conn is None:
            with self._db.connect() as read_conn:
                return self.get(plugin_id, namespace, logical_key, conn=read_conn)
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?",
            (_key(plugin_id, namespace, logical_key),),
        ).fetchone()
        return _decode(str(row["value"]), namespace, logical_key) if row is not None else None

    def list_prefix_page(
        self,
        plugin_id: str,
        namespace: str,
        prefix: str,
        *,
        after_key: str | None,
        limit: int,
        conn: Any | None = None,
    ) -> RetainedRecordPageData:
        if conn is None:
            with self._db.connect() as read_conn:
                return self.list_prefix_page(
                    plugin_id, namespace, prefix, after_key=after_key, limit=limit, conn=read_conn
                )
        encoded_prefix = _key(plugin_id, namespace, prefix)
        collation = '"C"' if self._db.dialect == "postgresql" else "BINARY"
        params: list[Any] = [encoded_prefix, encoded_prefix]
        after_clause = ""
        if after_key is not None:
            after_clause = f" AND key COLLATE {collation} > ?"
            params.append(_key(plugin_id, namespace, after_key))
        params.append(limit + 1)
        rows = conn.execute(
            f"SELECT key, value FROM settings WHERE substr(key COLLATE {collation}, 1, length(?)) = ?"
            + after_clause
            + f" ORDER BY key COLLATE {collation} LIMIT ?",
            tuple(params),
        ).fetchall()
        decoded: list[RetainedRecordData] = []
        for row in rows[:limit]:
            key = str(row["key"])
            try:
                raw = key[len(_KEY_PREFIX) :].split(":")
                if len(raw) != 3 or any(
                    not part or any(ch not in "0123456789abcdef" for ch in part) for part in raw
                ):
                    raise ValueError
                decoded_plugin, decoded_namespace, logical_key = (
                    bytes.fromhex(part).decode("utf-8") for part in raw
                )
                if (
                    decoded_plugin != plugin_id
                    or decoded_namespace != namespace
                    or not logical_key
                    or key != _key(decoded_plugin, decoded_namespace, logical_key)
                ):
                    raise ValueError
            except (ValueError, UnicodeDecodeError):
                raise ValueError("invalid retained record key") from None
            decoded.append(_decode(str(row["value"]), namespace, logical_key))
        next_key = decoded[-1].logical_key if len(rows) > limit and decoded else None
        return RetainedRecordPageData(tuple(decoded), next_key)

    def cas(
        self,
        plugin_id: str,
        namespace: str,
        logical_key: str,
        *,
        payload_json: str,
        expected_revision: int,
        conn: Any | None = None,
    ) -> RetainedRecordData | None:
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 0
        ):
            raise ValueError("invalid expected revision")
        if conn is None:
            with self._db.transaction() as tx_conn:
                self.lock_for_checks(tx_conn)
                return self.cas(
                    plugin_id,
                    namespace,
                    logical_key,
                    payload_json=payload_json,
                    expected_revision=expected_revision,
                    conn=tx_conn,
                )
        key = _key(plugin_id, namespace, logical_key)
        if _decode(payload_json, namespace, logical_key).revision != expected_revision + 1:
            raise ValueError("invalid retained record revision")
        if expected_revision == 0:
            result = conn.execute(
                "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO NOTHING",
                (key, payload_json),
            )
            if result.rowcount == 0:
                existing = conn.execute(
                    "SELECT value FROM settings WHERE key = ?", (key,)
                ).fetchone()
                if existing is not None:
                    _decode(str(existing["value"]), namespace, logical_key)
                return None
        else:
            original = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
            if original is None:
                return None
            original_value = str(original["value"])
            current = _decode(original_value, namespace, logical_key)
            if current.revision != expected_revision:
                return None
            result = conn.execute(
                "UPDATE settings SET value = ? WHERE key = ? AND value = ?",
                (payload_json, key, original_value),
            )
        if result.rowcount == 0:
            return None
        return self.get(plugin_id, namespace, logical_key, conn=conn)
