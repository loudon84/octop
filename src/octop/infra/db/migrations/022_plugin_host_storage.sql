-- Schema v22: generic plugin records and encrypted credentials.

CREATE TABLE IF NOT EXISTS plugin_records (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  record_id TEXT NOT NULL UNIQUE,
  plugin_id TEXT NOT NULL,
  namespace TEXT NOT NULL,
  logical_key TEXT NOT NULL,
  owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  revision INTEGER NOT NULL CHECK (revision > 0),
  payload_json TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  UNIQUE (plugin_id, namespace, logical_key)
);

CREATE TABLE IF NOT EXISTS plugin_credentials (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  credential_id TEXT NOT NULL UNIQUE,
  plugin_id TEXT NOT NULL,
  owner_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  ciphertext BLOB NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('staged', 'active')),
  revision INTEGER NOT NULL CHECK (revision > 0),
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

UPDATE _schema_version SET version = 22;
