"""Firmware distribution routes for the edge distributor."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Annotated, Any, Callable

from fastapi import Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from .firmware_store import (
    HASH_RE,
    build_firmware_offer,
    firmware_path,
    hash_file,
    install_firmware,
    list_firmware,
    next_wake_at,
    profile_wake_interval_seconds,
)


class CheckInRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    software_version: str = Field(min_length=1, max_length=200)
    boot_id: str = Field(min_length=1, max_length=128)
    uptime_seconds: int = Field(default=0, ge=0)
    wake_reason: str = Field(default="unknown", max_length=64)
    battery_percent: int | None = Field(default=None, ge=0, le=100)
    connectivity: dict[str, Any] = Field(default_factory=dict)
    gnss: dict[str, Any] = Field(default_factory=dict)
    battery: dict[str, Any] = Field(default_factory=dict)
    display: dict[str, Any] = Field(default_factory=dict)
    storage: dict[str, Any] = Field(default_factory=dict)


class FirmwareUploadMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(min_length=1, max_length=64)
    artifact_hash: str = Field(min_length=71, max_length=71)
    notes: str | None = Field(default=None, max_length=500)


class FirmwareAssignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: str = Field(min_length=1, max_length=64)
    artifact_hash: str = Field(min_length=71, max_length=71)
    wake_interval_seconds: int | None = Field(default=None, ge=300, le=604800)


def register_firmware_routes(
    app: Any,
    *,
    db: Any,
    settings: Any,
    authenticate_device: Callable[..., sqlite3.Row],
    signed_object: Callable[..., dict[str, Any]],
    internal_auth: Callable[..., None],
    iso: Callable[..., str],
    now: Callable[..., Any],
    dump_json: Callable[[dict[str, Any]], str],
    default_wake_interval: int = 46800,
) -> None:
    max_bytes = getattr(settings, "max_firmware_bytes", 6 * 1024 * 1024)

    @app.get(
        "/internal/v1/firmware",
        dependencies=[Depends(internal_auth)],
    )
    def internal_list_firmware() -> dict[str, Any]:
        return {"artifacts": list_firmware(db)}

    @app.put(
        "/internal/v1/firmware/{artifact_hash}/content",
        dependencies=[Depends(internal_auth)],
    )
    async def internal_upload_firmware(
        artifact_hash: str,
        request: Request,
        version: Annotated[str, Query(min_length=1, max_length=64)],
        notes: Annotated[str | None, Query(max_length=500)] = None,
    ) -> dict[str, Any]:
        if not HASH_RE.fullmatch(artifact_hash):
            raise HTTPException(422, "invalid artifact hash")
        if not version.strip():
            raise HTTPException(422, "version is required")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="fw-upload-", dir=settings.object_root
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        length = 0
        try:
            with temporary.open("wb") as handle:
                async for block in request.stream():
                    length += len(block)
                    if length > max_bytes:
                        raise HTTPException(413, "firmware exceeds configured size limit")
                    handle.write(block)
            result = install_firmware(
                db,
                settings.object_root,
                artifact_hash,
                version.strip(),
                temporary,
                max_bytes,
            )
            if notes:
                db.execute(
                    "UPDATE firmware_artifacts SET notes=? WHERE artifact_hash=?",
                    (notes.strip(), artifact_hash),
                )
            return result
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            temporary.unlink(missing_ok=True)

    @app.put(
        "/internal/v1/devices/{device_id}/firmware-assignment",
        dependencies=[Depends(internal_auth)],
    )
    def internal_assign_firmware(
        device_id: str, value: FirmwareAssignRequest
    ) -> dict[str, Any]:
        if not HASH_RE.fullmatch(value.artifact_hash):
            raise HTTPException(422, "invalid artifact hash")
        row = db.one(
            "SELECT * FROM firmware_artifacts WHERE artifact_hash=?",
            (value.artifact_hash,),
        )
        if row is None:
            raise HTTPException(404, "firmware artifact not found")
        device = db.one("SELECT * FROM devices WHERE device_id=?", (device_id,))
        if device is None:
            raise HTTPException(404, "device not found")
        profile = db.json(device, "profile_json")
        profile["assigned_firmware"] = {
            "version": value.version,
            "artifact_hash": value.artifact_hash,
        }
        if value.wake_interval_seconds is not None:
            profile["wake_interval_seconds"] = value.wake_interval_seconds
        timestamp = iso()
        db.execute(
            "UPDATE devices SET profile_json=?, updated_at=? WHERE device_id=?",
            (dump_json(profile), timestamp, device_id),
        )
        return {
            "device_id": device_id,
            "assigned_firmware": profile["assigned_firmware"],
            "wake_interval_seconds": profile.get("wake_interval_seconds"),
            "updated_at": timestamp,
        }

    @app.post("/api/v1/edge/{device_id}/check-in")
    async def device_check_in(
        device_id: str,
        value: CheckInRequest,
        row: sqlite3.Row = Depends(authenticate_device),
    ) -> dict[str, Any]:
        profile = db.json(row, "profile_json")
        db.execute(
            """UPDATE devices SET last_seen_at=?, last_uptime_seconds=?, last_boot_id=?,
               updated_at=? WHERE device_id=?""",
            (
                iso(),
                value.uptime_seconds,
                value.boot_id,
                iso(),
                device_id,
            ),
        )
        received_at = iso()
        db.execute(
            """INSERT INTO device_status_reports(device_id,received_at,report_json)
               VALUES(?,?,?)""",
            (device_id, received_at, dump_json(value.model_dump())),
        )
        offer = build_firmware_offer(
            db, profile, value.software_version, device_id
        )
        wake = next_wake_at(profile, default_wake_interval, now)
        return signed_object(
            app,
            {
                "schema": "tmads.eink-checkin-result.v1",
                "device_id": device_id,
                "server_time": iso(),
                "next_wake_at": wake,
                "wake_interval_seconds": profile_wake_interval_seconds(
                    profile, default_wake_interval
                ),
                "firmware_offer": offer,
            },
        )

    @app.get("/api/v1/edge/{device_id}/firmware/{artifact_hash}")
    async def serve_firmware(
        device_id: str,
        artifact_hash: str,
        _: sqlite3.Row = Depends(authenticate_device),
    ) -> Response:
        del device_id
        if not HASH_RE.fullmatch(artifact_hash):
            raise HTTPException(404, "firmware not found")
        row = db.one(
            "SELECT * FROM firmware_artifacts WHERE artifact_hash=?",
            (artifact_hash,),
        )
        if row is None or not row["object_path"]:
            raise HTTPException(404, "firmware not found")
        target = firmware_path(settings.object_root, artifact_hash)
        if not target.is_file():
            raise HTTPException(404, "firmware not found")
        try:
            calculated, length = hash_file(target, max_bytes)
        except (OSError, ValueError) as exc:
            raise HTTPException(409, "firmware object failed verification") from exc
        if calculated != artifact_hash or length != row["byte_length"]:
            raise HTTPException(409, "firmware object failed verification")
        return FileResponse(
            target,
            media_type="application/octet-stream",
            filename=f"firmware-{row['version']}.bin",
            headers={
                "Digest": f"sha-256={artifact_hash.removeprefix('sha256:')}",
                "X-Firmware-Version": row["version"],
            },
        )
