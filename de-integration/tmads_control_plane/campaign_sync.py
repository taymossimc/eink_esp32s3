"""Campaign catalog push, device derivatives, schedules, and shelf status.

This module keeps the edge distributor's preload catalog in sync with the
control plane's campaign inventory (all statuses, including DRAFT), produces
orientation-aware device derivatives, and proxies per-device schedule
management for the fleet UI.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from PIL import Image, ImageOps
from pydantic import BaseModel, ConfigDict, Field

log = logging.getLogger("tmads.campaign_sync")
_last_catalog_digest: str | None = None

VARIANT_DIMENSIONS: dict[str, tuple[int, int]] = {
    "portrait": (1200, 1600),
    "landscape": (1600, 1200),
}


class ScheduleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    wake_interval_seconds: int | None = Field(default=None, ge=300, le=604800)
    awake_timeout_seconds: int | None = Field(default=None, ge=60, le=3600)
    awake_poll_seconds: int | None = Field(default=None, ge=10, le=3600)
    display_orientation: int | None = Field(default=None)


def build_catalog_payload(conn: sqlite3.Connection) -> dict[str, Any]:
    campaigns_out: list[dict[str, Any]] = []
    campaigns = conn.execute(
        "SELECT * FROM campaigns WHERE status != 'ARCHIVED' ORDER BY id"
    ).fetchall()
    for campaign in campaigns:
        media_rows = conn.execute(
            """SELECT id, derivative_hash, processing_status FROM media_assets
               WHERE campaign_id=? AND kind='image' ORDER BY created_at""",
            (campaign["id"],),
        ).fetchall()
        upc_rows = conn.execute(
            """SELECT upc, label FROM campaign_product_upcs
               WHERE campaign_id=? ORDER BY upc""",
            (campaign["id"],),
        ).fetchall()
        campaigns_out.append(
            {
                "campaign_id": campaign["id"],
                "name": campaign["name"],
                "status": campaign["status"],
                "start_at": campaign["start_at"],
                "end_at": campaign["end_at"],
                "updated_at": campaign["updated_at"],
                "upcs": [dict(row) for row in upc_rows],
                "media": [
                    {
                        "media_id": row["id"],
                        "asset_hash": row["derivative_hash"],
                        "status": row["processing_status"],
                    }
                    for row in media_rows
                    if row["derivative_hash"]
                ],
            }
        )
    return {"campaigns": campaigns_out, "replace": True}


def push_campaign_catalog(
    conn: sqlite3.Connection, edge: Any, *, force: bool = True
) -> dict[str, Any]:
    global _last_catalog_digest
    if edge is None:
        return {"pushed": False, "error": "edge distributor is not configured"}
    payload = build_catalog_payload(conn)
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    if not force and digest == _last_catalog_digest:
        return {"pushed": False, "unchanged": True}
    import httpx

    sender = edge.client or httpx
    try:
        response = sender.put(
            f"{edge.base_url.rstrip('/')}/internal/v1/campaign-catalog",
            content=encoded,
            headers={**edge.headers, "Content-Type": "application/json"},
            timeout=edge.timeout,
        )
        response.raise_for_status()
        result = response.json()
    except (OSError, httpx.HTTPError, ValueError) as exc:
        log.warning("campaign_catalog_push_failed error=%s", exc)
        return {"pushed": False, "error": str(exc)}
    _last_catalog_digest = digest
    return {"pushed": True, **result}


def push_campaign_catalog_quietly(conn: sqlite3.Connection, edge: Any) -> None:
    """Best-effort push used from mutation paths; never raises."""
    try:
        push_campaign_catalog(conn, edge, force=False)
    except Exception:  # noqa: BLE001 - catalog sync must never break the caller
        log.warning("campaign_catalog_push_crashed", exc_info=True)


def generate_device_derivatives(
    source: Path,
) -> dict[str, Path]:
    """Render the two device variants into temporary files."""
    outputs: dict[str, Path] = {}
    for variant, dimensions in VARIANT_DIMENSIONS.items():
        handle = tempfile.NamedTemporaryFile(
            prefix=f"derivative-{variant}-", suffix=".jpg", delete=False
        )
        handle.close()
        target = Path(handle.name)
        with Image.open(source) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            image = ImageOps.fit(
                image,
                dimensions,
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )
            image.save(target, "JPEG", quality=90, optimize=True, progressive=False)
        outputs[variant] = target
    return outputs


def push_device_derivatives(
    edge: Any, asset_hash: str, derivative_path: Path
) -> dict[str, Any]:
    """Precompute portrait/landscape device JPEGs and push them to the edge."""
    if edge is None:
        return {"pushed": False, "error": "edge distributor is not configured"}
    import httpx

    sender = edge.client or httpx
    results: dict[str, Any] = {"pushed": True, "variants": {}}
    try:
        outputs = generate_device_derivatives(derivative_path)
    except (OSError, ValueError) as exc:
        log.warning("derivative_generation_failed error=%s", exc)
        return {"pushed": False, "error": str(exc)}
    try:
        for variant, path in outputs.items():
            try:
                with path.open("rb") as content:
                    response = sender.put(
                        f"{edge.base_url.rstrip('/')}/internal/v1/media/"
                        f"{asset_hash}/derivative/{variant}",
                        content=content,
                        headers={
                            **edge.headers,
                            "Content-Type": "application/octet-stream",
                        },
                        timeout=edge.timeout,
                    )
                response.raise_for_status()
                results["variants"][variant] = response.json()
            except (OSError, httpx.HTTPError, ValueError) as exc:
                log.warning(
                    "derivative_push_failed variant=%s error=%s", variant, exc
                )
                results["pushed"] = False
                results.setdefault("errors", {})[variant] = str(exc)
    finally:
        for path in outputs.values():
            path.unlink(missing_ok=True)
    return results


def set_device_schedule(edge: Any, device_id: str, body: ScheduleIn) -> dict[str, Any]:
    if edge is None:
        raise HTTPException(503, "Edge distributor is not configured")
    import httpx

    sender = edge.client or httpx
    try:
        response = sender.put(
            f"{edge.base_url.rstrip('/')}/internal/v1/devices/{device_id}/schedule",
            json=body.model_dump(exclude_none=True),
            headers={**edge.headers, "Content-Type": "application/json"},
            timeout=edge.timeout,
        )
    except (OSError, httpx.HTTPError) as exc:
        raise HTTPException(502, "Edge distributor is unavailable") from exc
    if response.status_code == 404:
        raise HTTPException(404, "Device not found")
    if response.status_code == 422:
        raise HTTPException(422, response.json().get("detail", "invalid schedule"))
    response.raise_for_status()
    return response.json()


def get_device_schedule(edge: Any, device_id: str) -> dict[str, Any]:
    if edge is None:
        raise HTTPException(503, "Edge distributor is not configured")
    import httpx

    sender = edge.client or httpx
    try:
        response = sender.get(
            f"{edge.base_url.rstrip('/')}/internal/v1/devices/{device_id}/schedule",
            headers=edge.headers,
            timeout=edge.timeout,
        )
    except (OSError, httpx.HTTPError) as exc:
        raise HTTPException(502, "Edge distributor is unavailable") from exc
    if response.status_code == 404:
        raise HTTPException(404, "Device not found")
    response.raise_for_status()
    return response.json()


def contract_media_hashes(
    conn: sqlite3.Connection, campaign_id: str, contract_hash: str | None = None
) -> set[str]:
    hashes: set[str] = set()
    rows = conn.execute(
        """SELECT r.contract_json FROM bid_revisions r
           JOIN bids b ON b.id = r.bid_id
           WHERE b.campaign_id=? AND r.lifecycle_state='PUBLISHED'
             AND (? IS NULL OR r.canonical_hash=?)""",
        (campaign_id, contract_hash, contract_hash),
    ).fetchall()
    for row in rows:
        try:
            contract = json.loads(row["contract_json"])
        except ValueError:
            continue
        for media in contract.get("media", []):
            asset_hash = media.get("asset_hash")
            if asset_hash:
                hashes.add(str(asset_hash))
    return hashes


def shelf_deployment_status(
    conn: sqlite3.Connection,
    edge: Any,
    deployment_id: str,
    token_hash: str,
) -> dict[str, Any]:
    """Delivery/sync/display acknowledgement for a shelf deployment."""
    row = conn.execute(
        "SELECT * FROM shelf_deployments WHERE id=? AND token_hash=?",
        (deployment_id, token_hash),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Deployment not found")
    result: dict[str, Any] = {
        "deployment_id": deployment_id,
        "device_id": row["device_id"],
        "campaign_id": row["campaign_id"],
        "upc": row["upc"],
        "deployed_at": row["deployed_at"],
        "stage": "delivered",
        "message": "Campaign assigned. Waiting for the sign to sync.",
    }
    if edge is None:
        return result
    try:
        device = edge.fleet_device(row["device_id"])
    except RuntimeError:
        return result
    report = device.get("status_report") or {}
    display = report.get("display") if isinstance(report, dict) else {}
    display = display if isinstance(display, dict) else {}
    active_hash = str(display.get("active_asset_hash") or "")
    campaign_hashes = contract_media_hashes(
        conn, row["campaign_id"], row["contract_hash"]
    )
    last_seen = device.get("last_seen_at")
    synced = False
    if isinstance(last_seen, str) and row["deployed_at"]:
        try:
            seen_at = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
            deployed_at = datetime.fromisoformat(
                str(row["deployed_at"]).replace("Z", "+00:00")
            )
            synced = seen_at > deployed_at
        except ValueError:
            synced = False
    if active_hash and active_hash in campaign_hashes:
        result["stage"] = "displayed"
        result["message"] = "The sign is now displaying this campaign."
    elif synced:
        result["stage"] = "synced"
        result["message"] = (
            "The sign has checked in and is downloading the campaign."
        )
    result["device_last_seen_at"] = last_seen
    result["active_asset_hash"] = active_hash or None
    return result
