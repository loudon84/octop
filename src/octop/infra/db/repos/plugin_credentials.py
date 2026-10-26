"""Encrypted credential blobs owned by individual plugins and users."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from octop.infra.db.pool import DatabasePool
from octop.infra.db.repos._base import DbRow, now_ts
from octop.infra.utils.ulid import new_ulid


@dataclass(frozen=True)
class PluginCredentialRow:
    pk: int
    id: str
    plugin_id: str
    owner_user_id: int
    ciphertext: bytes
    state: str
    revision: int
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row: DbRow) -> PluginCredentialRow:
        return cls(
            pk=int(row["id"]),
            id=str(row["credential_id"]),
            plugin_id=str(row["plugin_id"]),
            owner_user_id=int(row["owner_user_id"]),
            ciphertext=bytes(row["ciphertext"]),
            state=str(row["state"]),
            revision=int(row["revision"]),
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )


class PluginCredentialRepo:
    def __init__(self, db: DatabasePool) -> None:
        self._db = db

    def credential_id_exists(self, credential_id: str, *, conn: Any | None = None) -> bool:
        if conn is None:
            with self._db.connect() as read_conn:
                return self.credential_id_exists(credential_id, conn=read_conn)
        return (
            conn.execute(
                "SELECT 1 FROM plugin_credentials WHERE credential_id = ?",
                (credential_id,),
            ).fetchone()
            is not None
        )

    def stage(
        self,
        plugin_id: str,
        owner_user_id: int,
        ciphertext: bytes,
        *,
        credential_id: str | None = None,
        conn: Any | None = None,
    ) -> PluginCredentialRow:
        if conn is None:
            with self._db.transaction() as tx_conn:
                return self.stage(
                    plugin_id,
                    owner_user_id,
                    ciphertext,
                    credential_id=credential_id,
                    conn=tx_conn,
                )
        ts = now_ts()
        if credential_id is None:
            credential_id = new_ulid()
        conn.execute(
            "INSERT INTO plugin_credentials("
            "credential_id, plugin_id, owner_user_id, ciphertext, state, revision, "
            "created_at, updated_at"
            ") VALUES (?, ?, ?, ?, 'staged', 1, ?, ?)",
            (credential_id, plugin_id, owner_user_id, bytes(ciphertext), ts, ts),
        )
        row = self.get(plugin_id, credential_id, owner_user_id, conn=conn)
        if row is None:
            raise RuntimeError("plugin credential insert failed")
        return row

    def get(
        self,
        plugin_id: str,
        credential_id: str,
        owner_user_id: int,
        *,
        conn: Any | None = None,
    ) -> PluginCredentialRow | None:
        if conn is None:
            with self._db.connect() as read_conn:
                return self.get(plugin_id, credential_id, owner_user_id, conn=read_conn)
        row = conn.execute(
            "SELECT * FROM plugin_credentials WHERE plugin_id = ? AND credential_id = ? "
            "AND owner_user_id = ?",
            (plugin_id, credential_id, owner_user_id),
        ).fetchone()
        return PluginCredentialRow.from_row(row) if row is not None else None

    def activate(
        self,
        plugin_id: str,
        credential_id: str,
        owner_user_id: int,
        *,
        expected_revision: int,
        conn: Any | None = None,
    ) -> PluginCredentialRow | None:
        if conn is None:
            with self._db.transaction() as tx_conn:
                return self.activate(
                    plugin_id,
                    credential_id,
                    owner_user_id,
                    expected_revision=expected_revision,
                    conn=tx_conn,
                )
        result = conn.execute(
            "UPDATE plugin_credentials SET state = 'active', revision = revision + 1, "
            "updated_at = ? WHERE plugin_id = ? AND credential_id = ? AND owner_user_id = ? "
            "AND state = 'staged' AND revision = ?",
            (now_ts(), plugin_id, credential_id, owner_user_id, expected_revision),
        )
        if result.rowcount == 0:
            return None
        return self.get(plugin_id, credential_id, owner_user_id, conn=conn)

    def delete(
        self,
        plugin_id: str,
        credential_id: str,
        owner_user_id: int,
        *,
        conn: Any | None = None,
    ) -> bool:
        if conn is None:
            with self._db.transaction() as tx_conn:
                return self.delete(plugin_id, credential_id, owner_user_id, conn=tx_conn)
        result = conn.execute(
            "DELETE FROM plugin_credentials WHERE plugin_id = ? AND credential_id = ? "
            "AND owner_user_id = ?",
            (plugin_id, credential_id, owner_user_id),
        )
        return int(result.rowcount) > 0

    def delete_active(
        self,
        plugin_id: str,
        credential_id: str,
        owner_user_id: int,
        *,
        expected_revision: int,
        conn: Any | None = None,
    ) -> bool:
        if conn is None:
            with self._db.transaction() as tx_conn:
                return self.delete_active(
                    plugin_id,
                    credential_id,
                    owner_user_id,
                    expected_revision=expected_revision,
                    conn=tx_conn,
                )
        result = conn.execute(
            "DELETE FROM plugin_credentials WHERE plugin_id = ? AND credential_id = ? "
            "AND owner_user_id = ? AND state = 'active' AND revision = ?",
            (plugin_id, credential_id, owner_user_id, expected_revision),
        )
        return int(result.rowcount) > 0
