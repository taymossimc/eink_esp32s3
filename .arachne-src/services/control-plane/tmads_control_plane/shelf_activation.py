"""In-store shelf activation: NFC tokens, UPC routing, and campaign delivery."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from fastapi import HTTPException

_UPC_DIGITS = re.compile(r"\D")


def shelf_device_group(campaign_id: str) -> str:
    return f"shelf-campaign-{campaign_id}"


def normalize_upc(raw: str) -> str:
    digits = _UPC_DIGITS.sub("", raw.strip())
    if len(digits) not in {8, 11, 12, 13, 14}:
        raise ValueError("Enter a valid 8–14 digit product code.")
    if len(digits) == 11:
        digits = f"0{digits}"
    return digits


def _token_secret(settings: Any) -> str:
    secret = str(getattr(settings, "ip_hash_secret", "") or "").strip()
    if len(secret) < 32:
        raise RuntimeError("Shelf activation requires TMADS_IP_HASH_SECRET")
    return secret


def create_shelf_token(
    device_id: str,
    settings: Any,
    *,
    ttl_hours: int = 168,
) -> str:
    expires = int((datetime.now(UTC) + timedelta(hours=ttl_hours)).timestamp())
    payload = f"{device_id}|{expires}"
    signature = hmac.new(
        _token_secret(settings).encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()
    raw = f"{payload}|{signature}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def verify_shelf_token(token: str, settings: Any) -> str:
    try:
        padded = token + "=" * (-len(token) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode()).decode()
        device_id, expires_raw, signature = decoded.rsplit("|", 2)
        payload = f"{device_id}|{expires_raw}"
        expected = hmac.new(
            _token_secret(settings).encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("invalid signature")
        if int(expires_raw) < int(datetime.now(UTC).timestamp()):
            raise ValueError("expired")
        if not device_id.startswith("tmads-"):
            raise ValueError("invalid device")
        return device_id
    except ValueError as exc:
        raise HTTPException(400, "This activation link is invalid or has expired.") from exc
    except (UnicodeDecodeError, TypeError, ValueError) as exc:
        raise HTTPException(400, "This activation link is invalid or has expired.") from exc


def lookup_campaign_for_upc(conn: sqlite3.Connection, upc: str) -> sqlite3.Row | None:
    normalized = normalize_upc(upc)
    return conn.execute(
        """SELECT c.*, m.upc, m.label AS upc_label
           FROM campaign_product_upcs m
           JOIN campaigns c ON c.id = m.campaign_id
           WHERE m.upc = ? AND c.status IN ('ACTIVE', 'DRAFT')
           ORDER BY c.updated_at DESC
           LIMIT 1""",
        (normalized,),
    ).fetchone()


def published_contract_for_campaign(
    conn: sqlite3.Connection,
    campaign_id: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT r.canonical_hash, r.contract_json, b.organization_id
           FROM bid_revisions r
           JOIN bids b ON b.id = r.bid_id
           WHERE b.campaign_id = ?
             AND r.lifecycle_state = 'PUBLISHED'
           ORDER BY r.published_at DESC, r.revision DESC
           LIMIT 1""",
        (campaign_id,),
    ).fetchone()


class FleetReader(Protocol):
    def device(self, device_id: str) -> dict[str, Any]: ...


class EdgeWriter(Protocol):
    def device_profile(
        self,
        device_id: str,
        organization_id: str,
        profile: dict[str, Any],
    ) -> dict[str, Any]: ...


def device_is_awake(device: dict[str, Any], *, max_age_seconds: int = 900) -> bool:
    seen = device.get("last_heartbeat_at") or device.get("last_seen_at")
    if not isinstance(seen, str):
        return False
    try:
        observed = datetime.fromisoformat(seen.replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(UTC) - observed).total_seconds() <= max_age_seconds


def merge_shelf_profile(
    profile: dict[str, Any],
    *,
    campaign_id: str,
    upc: str,
) -> dict[str, Any]:
    merged = dict(profile)
    groups = {str(item) for item in merged.get("device_groups", []) if item}
    groups.add(shelf_device_group(campaign_id))
    merged["device_groups"] = sorted(groups)
    merged["active_shelf_upc"] = upc
    merged["shelf_deployed_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    return merged


def deploy_campaign_to_device(
    conn: sqlite3.Connection,
    *,
    settings: Any,
    fleet: FleetReader | None,
    edge: EdgeWriter | None,
    device_id: str,
    campaign_id: str,
    contract_hash: str,
    upc: str,
    token: str,
    compliance_photo_path: str | None = None,
) -> dict[str, Any]:
    if fleet is None or edge is None:
        raise HTTPException(503, "Fleet delivery is not configured")
    device = fleet.device(device_id)
    if str(device.get("status", "")).lower() != "active":
        raise HTTPException(409, "This sign is not active yet.")
    if not device_is_awake(device):
        raise HTTPException(
            409,
            "This sign has not checked in recently. Tap the NFC tag again and wait "
            "a few seconds before scanning the product code.",
        )
    organization_id = str(device.get("organization_id") or "")
    if not organization_id:
        raise HTTPException(409, "This sign is missing an organization assignment.")
    profile = device.get("profile")
    if not isinstance(profile, dict):
        profile = {}
    updated_profile = merge_shelf_profile(
        profile,
        campaign_id=campaign_id,
        upc=upc,
    )
    edge.device_profile(device_id, organization_id, updated_profile)
    deployment_id = f"shelf_{uuid.uuid4()}"
    deployed_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    conn.execute(
        """INSERT INTO shelf_deployments(
               id, device_id, campaign_id, contract_hash, upc, token_hash,
               compliance_photo_path, status, message, deployed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            deployment_id,
            device_id,
            campaign_id,
            contract_hash,
            upc,
            hashlib.sha256(token.encode()).hexdigest(),
            compliance_photo_path,
            "delivered",
            "Campaign profile updated; the sign should sync on its next pull.",
            deployed_at,
        ),
    )
    return {
        "deployment_id": deployment_id,
        "device_id": device_id,
        "campaign_id": campaign_id,
        "contract_hash": contract_hash,
        "upc": upc,
        "status": "delivered",
        "deployed_at": deployed_at,
        "device_online": True,
    }


def save_compliance_photo(
    settings: Any,
    deployment_id: str,
    content: bytes,
) -> str:
    root = Path(settings.data_dir) / "compliance"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{deployment_id}.jpg"
    path.write_bytes(content)
    return str(path)


def shelf_install_url(settings: Any, token: str) -> str:
    base = str(settings.invitation_base_url or "https://digitalengine.leporia.net").rstrip("/")
    return f"{base}/shelf/{token}"


def list_campaign_upcs(conn: sqlite3.Connection, campaign_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT upc, label, created_at
           FROM campaign_product_upcs
           WHERE campaign_id=?
           ORDER BY upc""",
        (campaign_id,),
    ).fetchall()
    return [dict(row) for row in rows]
