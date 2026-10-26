"""Generic versioned storage for plugin-owned records."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, map_rows, now_ts
from octop.infra.utils.ulid import new_ulid


@dataclass(frozen=True)
class PluginRecordRow:
    pk: int
    id: str
    plugin_id: str
    namespace: str
    logical_key: str
    owner_user_id: int
    revision: int
    payload_json: str
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row: DbRow) -> PluginRecordRow:
        return cls(
            pk=int(row["id"]),
            id=str(row["record_id"]),
            plugin_id=str(row["plugin_id"]),
            namespace=str(row["namespace"]),
            logical_key=str(row["logical_key"]),
            owner_user_id=int(row["owner_user_id"]),
            revision=int(row["revision"]),
            payload_json=str(row["payload_json"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )


class PluginRecordRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def lock_for_checks(self, conn: Any) -> None:
        """Serialize PostgreSQL commits that validate read-only record revisions."""
        if self._db.dialect == "postgresql":
            conn.execute("LOCK TABLE plugin_records IN SHARE ROW EXCLUSIVE MODE")

    def get(
        self,
        plugin_id: str,
        namespace: str,
        logical_key: str,
        *,
        conn: Any | None = None,
    ) -> PluginRecordRow | None:
        if conn is None:
            with self._db.connect() as read_conn:
                return self.get(plugin_id, namespace, logical_key, conn=read_conn)
        row = conn.execute(
            "SELECT * FROM plugin_records "
            "WHERE plugin_id = ? AND namespace = ? AND logical_key = ?",
            (plugin_id, namespace, logical_key),
        ).fetchone()
        return PluginRecordRow.from_row(row) if row is not None else None

    def list_prefix(
        self,
        plugin_id: str,
        namespace: str,
        prefix: str,
        *,
        conn: Any | None = None,
    ) -> list[PluginRecordRow]:
        if conn is None:
            with self._db.connect() as read_conn:
                return self.list_prefix(plugin_id, namespace, prefix, conn=read_conn)
        rows = conn.execute(
            "SELECT * FROM plugin_records "
            "WHERE plugin_id = ? AND namespace = ? "
            "AND substr(logical_key, 1, length(?)) = ? ORDER BY logical_key",
            (plugin_id, namespace, prefix, prefix),
        ).fetchall()
        return map_rows(rows, PluginRecordRow)

    def cas(
        self,
        plugin_id: str,
        namespace: str,
        logical_key: str,
        *,
        owner_user_id: int,
        payload_json: str,
        expected_revision: int,
        conn: Any | None = None,
    ) -> PluginRecordRow | None:
        if expected_revision < 0:
            raise ValueError("expected_revision must be non-negative")
        if conn is None:
            with self._db.transaction() as tx_conn:
                return self.cas(
                    plugin_id,
                    namespace,
                    logical_key,
                    owner_user_id=owner_user_id,
                    payload_json=payload_json,
                    expected_revision=expected_revision,
                    conn=tx_conn,
                )

        ts = now_ts()
        if expected_revision == 0:
            record_id = new_ulid()
            result = conn.execute(
                "INSERT INTO plugin_records("
                "record_id, plugin_id, namespace, logical_key, owner_user_id, revision, "
                "payload_json, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?) "
                "ON CONFLICT (plugin_id, namespace, logical_key) DO NOTHING",
                (
                    record_id,
                    plugin_id,
                    namespace,
                    logical_key,
                    owner_user_id,
                    payload_json,
                    ts,
                    ts,
                ),
            )
            if result.rowcount == 0:
                return None
        else:
            result = conn.execute(
                "UPDATE plugin_records SET payload_json = ?, revision = revision + 1, "
                "updated_at = ? WHERE plugin_id = ? AND namespace = ? AND logical_key = ? "
                "AND owner_user_id = ? AND revision = ?",
                (
                    payload_json,
                    ts,
                    plugin_id,
                    namespace,
                    logical_key,
                    owner_user_id,
                    expected_revision,
                ),
            )
            if result.rowcount == 0:
                return None
        return self.get(plugin_id, namespace, logical_key, conn=conn)
