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


SCHEDULE_LIMITS: dict[str, tuple[int, int, int]] = {
    # field: (minimum, maximum, default)
    "wake_interval_seconds": (300, 604800, 46800),
    "awake_timeout_seconds": (60, 3600, 300),
    "awake_poll_seconds": (10, 3600, 30),
}

VALID_ORIENTATIONS = {0, 90, 180, 270}


def profile_schedule(profile: dict[str, Any], default_wake: int) -> dict[str, Any]:
    """Validated per-device schedule with defaults for missing fields."""
    schedule: dict[str, Any] = {}
    for field, (low, high, default) in SCHEDULE_LIMITS.items():
        value = profile.get(field)
        if field == "wake_interval_seconds":
            default = default_wake
        if isinstance(value, int) and low <= value <= high:
            schedule[field] = value
        else:
            schedule[field] = default
    orientation = profile.get("display_orientation")
    schedule["display_orientation"] = (
        orientation if isinstance(orientation, int) and orientation in VALID_ORIENTATIONS else 0
    )
    return schedule


def apply_schedule_update(
    profile: dict[str, Any], update: dict[str, Any]
) -> dict[str, Any]:
    """Validate and merge schedule fields into a device profile."""
    for field, (low, high, _) in SCHEDULE_LIMITS.items():
        if field not in update or update[field] is None:
            continue
        value = update[field]
        if not isinstance(value, int) or not low <= value <= high:
            raise ValueError(f"{field} must be between {low} and {high}")
        profile[field] = value
    if "display_orientation" in update and update["display_orientation"] is not None:
        orientation = update["display_orientation"]
        if not isinstance(orientation, int) or orientation not in VALID_ORIENTATIONS:
            raise ValueError("display_orientation must be 0, 90, 180, or 270")
        profile["display_orientation"] = orientation
    return profile


def latest_status_report(db: Any, device_id: str) -> dict[str, Any] | None:
    row = db.one(
        """SELECT received_at, report_json FROM device_status_reports
           WHERE device_id=? ORDER BY received_at DESC, id DESC LIMIT 1""",
        (device_id,),
    )
    if row is None:
        return None
    import json as _json

    try:
        report = _json.loads(row["report_json"])
    except ValueError:
        return None
    report["received_at"] = row["received_at"]
    return report


def battery_from_status_report(
    report: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Adapt the firmware's nested battery report into the fleet battery model."""
    if not isinstance(report, dict):
        return None
    battery = report.get("battery")
    if not isinstance(battery, dict):
        return None
    if not battery.get("available"):
        return {
            "available": False,
            "error": battery.get("error"),
            "collected_at": report.get("received_at"),
        }
    voltage = battery.get("voltage_v")
    current = battery.get("current_ma")
    power = battery.get("power_mw")
    level = battery.get("soc_percent")
    return {
        "available": True,
        "level_percent": (
            max(0, min(100, int(level))) if isinstance(level, (int, float)) else None
        ),
        "voltage_mv": (
            int(float(voltage) * 1000) if isinstance(voltage, (int, float)) else None
        ),
        "current_ma": (
            round(float(current), 2) if isinstance(current, (int, float)) else None
        ),
        "power_mw": (
            round(float(power), 2) if isinstance(power, (int, float)) else None
        ),
        # A positive current means the battery is being charged.
        "charging": (
            bool(float(current) > 5) if isinstance(current, (int, float)) else None
        ),
        "collected_at": report.get("received_at"),
    }


def status_report_fields(db: Any, device_id: str, default_wake: int) -> dict[str, Any]:
    """Fleet payload extras joined from the latest device status report."""
    report = latest_status_report(db, device_id)
    fields: dict[str, Any] = {
        "status_report": None,
        "device_battery": battery_from_status_report(report),
    }
    if report is None:
        return fields
    fields["status_report"] = {
        "received_at": report.get("received_at"),
        "software_version": report.get("software_version"),
        "wake_reason": report.get("wake_reason"),
        "uptime_seconds": report.get("uptime_seconds"),
        "connectivity": report.get("connectivity"),
        "gnss": report.get("gnss"),
        "display": report.get("display"),
        "storage": report.get("storage"),
        "timings": report.get("timings"),
    }
    return fields


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
