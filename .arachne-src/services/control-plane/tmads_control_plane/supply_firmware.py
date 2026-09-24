"""Supply-side firmware artifact upload and device assignment."""

from __future__ import annotations

import hashlib
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from .integrations import EdgeDistributorClient

MAX_FIRMWARE_BYTES = 6 * 1024 * 1024


class FirmwareAssignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(min_length=1, max_length=64)
    artifact_hash: str = Field(min_length=71, max_length=71)
    wake_interval_seconds: int | None = Field(default=None, ge=300, le=604800)


def list_artifacts(edge: EdgeDistributorClient | None) -> dict[str, Any]:
    if edge is None:
        raise HTTPException(503, "Edge distributor is not configured")
    return edge.list_firmware()


async def upload_artifact(
    request: Request,
    conn: sqlite3.Connection,
    edge: EdgeDistributorClient | None,
) -> dict[str, Any]:
    if edge is None:
        raise HTTPException(503, "Edge distributor is not configured")
    form = await request.form()
    upload = form.get("file")
    version = str(form.get("version") or "").strip()
    notes = str(form.get("notes") or "").strip() or None
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(422, "file is required")
    if not version:
        raise HTTPException(422, "version is required")

    with tempfile.NamedTemporaryFile(delete=False) as temporary:
        hasher = hashlib.sha256()
        total = 0
        while True:
            block = await upload.read(65536)
            if not block:
                break
            total += len(block)
            if total > MAX_FIRMWARE_BYTES:
                temporary.close()
                Path(temporary.name).unlink(missing_ok=True)
                raise HTTPException(413, "firmware exceeds size limit")
            hasher.update(block)
            temporary.write(block)
        temporary_path = temporary.name

    artifact_hash = f"sha256:{hasher.hexdigest()}"
    try:
        result = edge.upload_firmware(
            artifact_hash,
            version,
            temporary_path,
            notes,
        )
    finally:
        Path(temporary_path).unlink(missing_ok=True)

    try:
        conn.execute(
            """INSERT INTO operator_notes(object_type, object_id, note, created_at)
               VALUES('firmware', ?, ?, datetime('now'))""",
            (artifact_hash, notes or f"Uploaded firmware {version}"),
        )
    except sqlite3.Error:
        pass
    return result


def assign_to_device(
    device_id: str,
    body: FirmwareAssignIn,
    conn: sqlite3.Connection,
    edge: EdgeDistributorClient | None,
) -> dict[str, Any]:
    if edge is None:
        raise HTTPException(503, "Edge distributor is not configured")
    result = edge.assign_firmware(
        device_id,
        body.version,
        body.artifact_hash,
        body.wake_interval_seconds,
    )
    try:
        conn.execute(
            """INSERT INTO operator_notes(object_type, object_id, note, created_at)
               VALUES('device', ?, ?, datetime('now'))""",
            (
                device_id,
                f"Assigned firmware {body.version} ({body.artifact_hash})",
            ),
        )
    except sqlite3.Error:
        pass
    return result
