from __future__ import annotations

import json
import sqlite3

from .firmware_store import ensure_schema as ensure_firmware_schema
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','active','disabled')),
    profile_json TEXT NOT NULL,
    public_key BLOB,
    key_id TEXT,
    enrollment_token_hash TEXT UNIQUE,
    enrollment_expires_at TEXT,
    enrolled_at TEXT,
    last_seen_at TEXT,
    last_ack_cursor INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS nonces (
    device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
    nonce TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    PRIMARY KEY (device_id, nonce)
);
CREATE INDEX IF NOT EXISTS idx_nonces_timestamp ON nonces(timestamp);

CREATE TABLE IF NOT EXISTS contracts (
    object_hash TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(contract_id, revision)
);

CREATE TABLE IF NOT EXISTS media_manifests (
    asset_hash TEXT PRIMARY KEY,
    media_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    byte_length INTEGER NOT NULL,
    mime TEXT NOT NULL,
    object_path TEXT,
    verified_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS spend_grants (
    grant_id TEXT PRIMARY KEY,
    contract_hash TEXT NOT NULL,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    payload_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spend_grants_device
 ON spend_grants(device_id, valid_to);
CREATE TABLE IF NOT EXISTS spend_grant_revocations (
    grant_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    revoked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS observations (
    observation_id TEXT PRIMARY KEY,
    object_hash TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    target_device_id TEXT,
    payload_json TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_observations_target
 ON observations(target_device_id, valid_until);
CREATE TABLE IF NOT EXISTS observation_revocations (
    observation_id TEXT PRIMARY KEY,
    target_device_id TEXT,
    revoked_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    object_hash TEXT NOT NULL,
    operation TEXT NOT NULL DEFAULT 'upsert',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_sequence ON events(sequence);

CREATE TABLE IF NOT EXISTS sync_acks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    cursor INTEGER NOT NULL,
    received_at TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settlement_outbox (
    id TEXT PRIMARY KEY,
    object_type TEXT NOT NULL,
    object_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('PENDING','FAILED','SYNCED','QUARANTINED')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error TEXT,
    created_at TEXT NOT NULL,
    synced_at TEXT,
    UNIQUE(object_type, object_key)
);
CREATE INDEX IF NOT EXISTS idx_settlement_outbox_due
 ON settlement_outbox(status,next_attempt_at);
CREATE TABLE IF NOT EXISTS operator_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS registration_claims (
    claim_hash TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
    certificate_id TEXT NOT NULL,
    nonce TEXT NOT NULL UNIQUE,
    claim_json TEXT NOT NULL,
    claimed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    UNIQUE(device_id, certificate_id)
);

CREATE TABLE IF NOT EXISTS device_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
    observation_id TEXT NOT NULL UNIQUE,
    latitude REAL NOT NULL,
    longitude REAL NOT NULL,
    confidence REAL NOT NULL,
    observed_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    source TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS movement_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
    baseline_latitude REAL NOT NULL,
    baseline_longitude REAL NOT NULL,
    detected_latitude REAL NOT NULL,
    detected_longitude REAL NOT NULL,
    distance_meters REAL NOT NULL,
    first_observed_at TEXT NOT NULL,
    confirmed_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','acknowledged'))
);

CREATE TABLE IF NOT EXISTS device_cellular_latest (
    device_id TEXT PRIMARY KEY REFERENCES devices(device_id) ON DELETE CASCADE,
    heartbeat_nonce TEXT NOT NULL,
    heartbeat_sent_at TEXT NOT NULL,
    heartbeat_signer_key_id TEXT NOT NULL,
    heartbeat_signature TEXT NOT NULL,
    iccid TEXT,
    imei TEXT,
    operator_name TEXT,
    mcc TEXT,
    mnc TEXT,
    registration_state TEXT NOT NULL,
    lte_interface TEXT NOT NULL,
    rssi_dbm INTEGER,
    rx_bytes INTEGER NOT NULL CHECK(rx_bytes >= 0),
    tx_bytes INTEGER NOT NULL CHECK(tx_bytes >= 0),
    rx_delta_bytes INTEGER NOT NULL CHECK(rx_delta_bytes >= 0),
    tx_delta_bytes INTEGER NOT NULL CHECK(tx_delta_bytes >= 0),
    counter_boot_id TEXT NOT NULL,
    interval_started_at TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    received_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cellular_usage_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
    heartbeat_nonce TEXT NOT NULL,
    heartbeat_sent_at TEXT NOT NULL,
    heartbeat_signer_key_id TEXT NOT NULL,
    heartbeat_signature TEXT NOT NULL,
    iccid TEXT,
    imei TEXT,
    operator_name TEXT,
    mcc TEXT,
    mnc TEXT,
    registration_state TEXT NOT NULL,
    lte_interface TEXT NOT NULL,
    rssi_dbm INTEGER,
    rx_bytes INTEGER NOT NULL CHECK(rx_bytes >= 0),
    tx_bytes INTEGER NOT NULL CHECK(tx_bytes >= 0),
    rx_delta_bytes INTEGER NOT NULL CHECK(rx_delta_bytes >= 0),
    tx_delta_bytes INTEGER NOT NULL CHECK(tx_delta_bytes >= 0),
    counter_boot_id TEXT NOT NULL,
    interval_started_at TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    received_at TEXT NOT NULL,
    UNIQUE(device_id, heartbeat_nonce)
);

CREATE TABLE IF NOT EXISTS cellular_usage_totals (
    device_id TEXT PRIMARY KEY REFERENCES devices(device_id) ON DELETE CASCADE,
    rx_bytes INTEGER NOT NULL DEFAULT 0 CHECK(rx_bytes >= 0),
    tx_bytes INTEGER NOT NULL DEFAULT 0 CHECK(tx_bytes >= 0),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cellular_usage_recent (
    device_id TEXT NOT NULL REFERENCES devices(device_id) ON DELETE CASCADE,
    heartbeat_nonce TEXT NOT NULL,
    rx_bytes INTEGER NOT NULL CHECK(rx_bytes >= 0),
    tx_bytes INTEGER NOT NULL CHECK(tx_bytes >= 0),
    received_at TEXT NOT NULL,
    PRIMARY KEY(device_id, heartbeat_nonce)
);

CREATE TABLE IF NOT EXISTS device_battery_latest (
    device_id TEXT PRIMARY KEY REFERENCES devices(device_id) ON DELETE CASCADE,
    heartbeat_nonce TEXT NOT NULL,
    heartbeat_sent_at TEXT NOT NULL,
    level_percent INTEGER CHECK(level_percent >= 0 AND level_percent <= 100),
    charging INTEGER,
    voltage_mv INTEGER CHECK(voltage_mv >= 0),
    temperature_c REAL,
    collected_at TEXT NOT NULL,
    received_at TEXT NOT NULL
);
"""

DEVICE_COLUMNS: dict[str, str] = {
    "attestation_certificate_json": "TEXT",
    "certificate_id": "TEXT",
    "location_public_key": "TEXT",
    "sensor_public_key": "TEXT",
    "last_heartbeat_at": "TEXT",
    "current_latitude": "REAL",
    "current_longitude": "REAL",
    "current_position_at": "TEXT",
    "movement_anchor_latitude": "REAL",
    "movement_anchor_longitude": "REAL",
    "movement_candidate_latitude": "REAL",
    "movement_candidate_longitude": "REAL",
    "movement_candidate_at": "TEXT",
    "last_uptime_seconds": "INTEGER",
    "last_boot_id": "TEXT",
    "link_type": "TEXT",
}

POST_MIGRATION_SCHEMA = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_devices_certificate_id
 ON devices(certificate_id) WHERE certificate_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_devices_organization
 ON devices(organization_id, status, device_id);
CREATE INDEX IF NOT EXISTS idx_registration_claims_device
 ON registration_claims(device_id, received_at);
CREATE INDEX IF NOT EXISTS idx_device_positions_device_time
 ON device_positions(device_id, observed_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_movement_alerts_device_time
 ON movement_alerts(device_id, confirmed_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_cellular_usage_device_time
 ON cellular_usage_history(device_id, received_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_cellular_usage_recent_time
 ON cellular_usage_recent(received_at);
"""


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            existing = {
                row["name"] for row in connection.execute("PRAGMA table_info(devices)")
            }
            for name, definition in DEVICE_COLUMNS.items():
                if name not in existing:
                    connection.execute(f"ALTER TABLE devices ADD COLUMN {name} {definition}")
            connection.executescript(POST_MIGRATION_SCHEMA)
            ensure_firmware_schema(connection)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        with self._lock, self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    def one(self, sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self.connect() as connection:
            return connection.execute(sql, parameters).fetchone()

    def all(self, sql: str, parameters: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self.connect() as connection:
            return connection.execute(sql, parameters).fetchall()

    def execute(self, sql: str, parameters: tuple[Any, ...] = ()) -> None:
        with self.transaction(immediate=True) as connection:
            connection.execute(sql, parameters)

    def max_sequence(self) -> int:
        row = self.one("SELECT COALESCE(MAX(sequence), 0) AS value FROM events")
        return int(row["value"]) if row else 0

    @staticmethod
    def json(row: sqlite3.Row, field: str = "payload_json") -> dict[str, Any]:
        return json.loads(row[field])
