"""Firmware artifact storage and per-device update offers."""

from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

FIRMWARE_SCHEMA = """
CREATE TABLE IF NOT EXISTS firmware_artifacts (
    artifact_hash TEXT PRIMARY KEY,
    version TEXT NOT NULL,
    byte_length INTEGER NOT NULL,
    object_path TEXT,
    uploaded_at TEXT NOT NULL,
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_firmware_version ON firmware_artifacts(version);
CREATE TABLE IF NOT EXISTS device_status_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL,
    received_at TEXT NOT NULL,
    report_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_device_status_reports_device_time
    ON device_status_reports(device_id, received_at DESC);
"""


def ensure_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(FIRMWARE_SCHEMA)


def firmware_path(object_root: Path, artifact_hash: str) -> Path:
    digest = artifact_hash.removeprefix("sha256:")
    return object_root / "firmware" / digest[:2] / digest


def profile_assigned_firmware(profile: dict[str, Any]) -> dict[str, Any] | None:
    raw = profile.get("assigned_firmware")
    if not isinstance(raw, dict):
        return None
    version = raw.get("version")
    artifact_hash = raw.get("artifact_hash")
    if not version or not artifact_hash:
        return None
    artifact_hash = str(artifact_hash)
    if not HASH_RE.fullmatch(artifact_hash):
        return None
    return {"version": str(version), "artifact_hash": artifact_hash}


def profile_wake_interval_seconds(profile: dict[str, Any], default: int) -> int:
    value = profile.get("wake_interval_seconds")
    if isinstance(value, int) and 300 <= value <= 86400 * 7:
        return value
    return default


def build_firmware_offer(
    db: Any,
    profile: dict[str, Any],
    reported_version: str,
    device_id: str,
) -> dict[str, Any] | None:
    assignment = profile_assigned_firmware(profile)
    if assignment is None:
        return None
    if assignment["version"].strip() == reported_version.strip():
        return None
    row = db.one(
        "SELECT * FROM firmware_artifacts WHERE artifact_hash=?",
        (assignment["artifact_hash"],),
    )
    if row is None or not row["object_path"]:
        return None
    return {
        "version": assignment["version"],
        "artifact_hash": assignment["artifact_hash"],
        "url": f"/api/v1/edge/{device_id}/firmware/{assignment['artifact_hash']}",
        "byte_length": int(row["byte_length"]),
    }


def hash_file(path: Path, max_bytes: int) -> tuple[str, int]:
    hasher = hashlib.sha256()
    length = 0
    with path.open("rb") as handle:
        while True:
            block = handle.read(65536)
            if not block:
                break
            length += len(block)
            if length > max_bytes:
                raise ValueError("firmware exceeds size limit")
            hasher.update(block)
    return f"sha256:{hasher.hexdigest()}", length


def install_firmware(
    db: Any,
    object_root: Path,
    artifact_hash: str,
    version: str,
    source: Path,
    max_bytes: int,
) -> dict[str, Any]:
    if not HASH_RE.fullmatch(artifact_hash):
        raise ValueError("invalid artifact hash")
    calculated, length = hash_file(source, max_bytes)
    if calculated != artifact_hash:
        raise ValueError("hash mismatch")
    target = firmware_path(object_root, artifact_hash)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    shutil.move(str(source), str(target))
    uploaded = datetime.now(UTC).isoformat()
    db.execute(
        """INSERT INTO firmware_artifacts(
               artifact_hash,version,byte_length,object_path,uploaded_at
           ) VALUES(?,?,?,?,?)
           ON CONFLICT(artifact_hash) DO UPDATE SET
           version=excluded.version,
           byte_length=excluded.byte_length,
           object_path=excluded.object_path,
           uploaded_at=excluded.uploaded_at""",
        (artifact_hash, version, length, str(target), uploaded),
    )
    return {
        "artifact_hash": artifact_hash,
        "version": version,
        "bytes": length,
        "stored": True,
    }


def list_firmware(db: Any) -> list[dict[str, Any]]:
    rows = db.all(
        "SELECT artifact_hash, version, byte_length, uploaded_at, notes "
        "FROM firmware_artifacts ORDER BY uploaded_at DESC"
    )
    return [dict(row) for row in rows]


def next_wake_at(profile: dict[str, Any], default_interval: int, now_fn: Any) -> str:
    interval = profile_wake_interval_seconds(profile, default_interval)
    return (now_fn() + timedelta(seconds=interval)).isoformat()
