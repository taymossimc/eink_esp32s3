#!/usr/bin/env python3
"""Promote draft campaigns into valid, published test campaigns on Arachne.

Runs on the Arachne host. Mints an operator session directly in the
control-plane database (SHA-256 token hash), then drives the public API to:
  - normalize campaign windows and budgets,
  - upload a generated creative where a campaign has no READY media,
  - add product UPC mappings where missing,
  - create and publish a signed contract per campaign,
  - issue a spend grant per contract,
  - target the enrolled device by merging shelf device groups.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta

BASE = "https://digitalengine.leporia.net"
CONTAINER = "tmads-control-plane"
EDGE_URL = "http://192.168.2.102:8781"
ENV_PATH = "/mnt/dockerprojects/digital_engine/deploy/arachne/.env"


def sh(command: list[str], input_text: str | None = None) -> str:
    result = subprocess.run(
        command, capture_output=True, text=True, input=input_text, timeout=120
    )
    if result.returncode != 0:
        raise RuntimeError(f"{command}: {result.stderr.strip()[:500]}")
    return result.stdout


def container_python(script: str) -> str:
    return sh(["docker", "exec", "-i", CONTAINER, "python3", "-"], script)


def edge_token() -> str:
    with open(ENV_PATH, encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("TMADS_EDGE_INTERNAL_BEARER_TOKEN="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("edge internal token not found")


def mint_sessions() -> list[tuple[str, str, str]]:
    """One session per campaign-owning operator; returns (token, csrf, user)."""
    users_script = """
import glob, json, sqlite3
path = [p for p in glob.glob('/var/lib/tmads/*.sqlite3') if 'control' in p][0]
conn = sqlite3.connect(path)
conn.row_factory = sqlite3.Row
rows = conn.execute(
    \"\"\"SELECT DISTINCT u.id FROM users u
       JOIN organization_memberships m ON m.user_id = u.id
       JOIN campaigns c ON c.organization_id = m.organization_id
       WHERE u.status='ACTIVE' AND m.role IN ('owner','admin','campaign_manager')\"\"\"
).fetchall()
print(json.dumps([r['id'] for r in rows]))
"""
    user_ids = json.loads(container_python(users_script))
    sessions = []
    for user_id in user_ids:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        insert_script = f"""
import glob, hashlib, sqlite3
from datetime import datetime, timedelta, timezone
path = [p for p in glob.glob('/var/lib/tmads/*.sqlite3') if 'control' in p][0]
conn = sqlite3.connect(path)
now = datetime.now(timezone.utc)
conn.execute(
    \"\"\"INSERT INTO auth_sessions(
       id,user_id,token_hash,csrf_token,network_key_hash,created_at,expires_at,revoked_at)
       VALUES(?,?,?,?,NULL,?,?,NULL)\"\"\",
    ('ses_promote_' + '{uuid.uuid4().hex[:10]}', '{user_id}',
     hashlib.sha256('{token}'.encode()).hexdigest(), '{csrf}',
     now.isoformat(), (now + timedelta(hours=6)).isoformat()),
)
conn.commit()
print('ok')
"""
        container_python(insert_script)
        sessions.append((token, csrf, user_id))
    return sessions


class Api:
    def __init__(self, token: str, csrf: str):
        self.cookie = f"tmads_session={token}"
        self.csrf = csrf

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        content_type: str | None = None,
        extra_headers: dict[str, str] | None = None,
        base: str = BASE,
        auth: bool = True,
    ):
        request = urllib.request.Request(base + path, data=body, method=method)
        if auth:
            request.add_header("Cookie", self.cookie)
            request.add_header("X-CSRF-Token", self.csrf)
        if content_type:
            request.add_header("Content-Type", content_type)
        for key, value in (extra_headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read() or b"{}")
            except ValueError:
                detail = {}
            return error.code, detail

    def get(self, path: str):
        return self.request("GET", path)

    def post_json(self, path: str, payload: dict, headers: dict | None = None):
        return self.request(
            "POST", path, json.dumps(payload).encode(), "application/json", headers
        )

    def patch_json(self, path: str, payload: dict):
        return self.request(
            "PATCH", path, json.dumps(payload).encode(), "application/json"
        )

    def upload(self, path: str, filename: str, content: bytes, mime: str):
        boundary = "----promote" + secrets.token_hex(8)
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        return self.request(
            "POST", path, body, f"multipart/form-data; boundary={boundary}"
        )


def generate_creative(name: str, index: int) -> bytes:
    """Render a distinct 1200x1600 test PNG inside the container (has PIL)."""
    palette = [
        ("#1c3f94", "#f2c744"), ("#166534", "#ecfdf5"), ("#7c2d12", "#fed7aa"),
        ("#4c1d95", "#ede9fe"), ("#0e7490", "#cffafe"), ("#9d174d", "#fce7f3"),
    ]
    background, accent = palette[index % len(palette)]
    safe_name = name.replace("'", "")[:40]
    script = f"""
import base64, io, sys
from PIL import Image, ImageDraw
image = Image.new('RGB', (1200, 1600), '{background}')
draw = ImageDraw.Draw(image)
draw.rectangle([60, 60, 1140, 1540], outline='{accent}', width=18)
draw.rectangle([120, 660, 1080, 940], fill='{accent}')
try:
    from PIL import ImageFont
    font = ImageFont.load_default(96)
    small = ImageFont.load_default(48)
except Exception:
    font = small = None
draw.text((160, 720), '{safe_name}'[:18], fill='{background}', font=font)
draw.text((160, 860), 'TMAds test creative #{index}', fill='{background}', font=small)
draw.text((160, 1400), 'Digital Engine fleet validation', fill='{accent}', font=small)
buffer = io.BytesIO()
image.save(buffer, 'PNG')
sys.stdout.write(base64.b64encode(buffer.getvalue()).decode())
"""
    import base64

    return base64.b64decode(container_python(script))


def campaign_media(campaign_id: str) -> list[dict]:
    """Media rows straight from the control-plane DB (no list API exists)."""
    script = f"""
import glob, json, sqlite3
path = [p for p in glob.glob('/var/lib/tmads/*.sqlite3') if 'control' in p or 'tmads' in p][0]
conn = sqlite3.connect(path)
conn.row_factory = sqlite3.Row
rows = conn.execute(
    "SELECT id, processing_status, kind FROM media_assets WHERE campaign_id=?",
    ('{campaign_id}',),
).fetchall()
print(json.dumps([dict(r) for r in rows]))
"""
    return json.loads(container_python(script))


def edge_request(method: str, path: str, payload: dict | None = None):
    request = urllib.request.Request(EDGE_URL + path, method=method)
    request.add_header("Authorization", f"Bearer {edge_token()}")
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, data=body, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read() or b"{}")
        except ValueError:
            return error.code, {}


def campaign_state(campaign_id: str) -> dict:
    """Published-contract and grant state straight from the DB."""
    script = f"""
import glob, json, sqlite3
from datetime import datetime, timedelta, timezone
path = [p for p in glob.glob('/var/lib/tmads/*.sqlite3') if 'control' in p][0]
conn = sqlite3.connect(path)
conn.row_factory = sqlite3.Row
horizon = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
published = conn.execute(
    \"\"\"SELECT r.canonical_hash, r.contract_json FROM bid_revisions r
       JOIN bids b ON b.id = r.bid_id
       WHERE b.campaign_id=? AND r.lifecycle_state='PUBLISHED'
       ORDER BY r.published_at DESC LIMIT 1\"\"\",
    ('{campaign_id}',),
).fetchone()
active_hash = None
if published:
    contract = json.loads(published['contract_json'])
    if contract.get('valid_to', '') > horizon:
        active_hash = published['canonical_hash']
grants = conn.execute(
    \"\"\"SELECT grant_id, status, amount_minor FROM grant_commitments
       WHERE campaign_id=? AND status IN ('RESERVED','REGISTERED')\"\"\",
    ('{campaign_id}',),
).fetchall()
print(json.dumps({{
    'active_contract_hash': active_hash,
    'open_grants': [dict(g) for g in grants],
}}))
"""
    return json.loads(container_python(script))


def main() -> int:
    sessions = mint_sessions()
    print(f"{len(sessions)} operator sessions minted")

    status, devices = edge_request("GET", "/internal/v1/fleet/devices")
    active_devices = [
        d for d in devices.get("devices", [])
        if d["status"] == "active" and d["device_id"].startswith("tmads-eink")
    ]
    if not active_devices:
        print("no active tmads-eink device found; grants/groups will be skipped")
        device = None
    else:
        device = max(active_devices, key=lambda d: d.get("last_seen_at") or "")
        print("target device:", device["device_id"])

    now = datetime.now(UTC)
    window_start = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    window_end = (now + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")

    all_campaigns = []
    for token, csrf, user_id in sessions:
        api = Api(token, csrf)
        status, me = api.get("/api/v1/me")
        if status != 200:
            print(f"auth failed for {user_id}: {status}")
            continue
        for organization in me["organizations"]:
            status, wallet = api.get(
                f"/api/v1/organizations/{organization['id']}/wallet"
            )
            if status == 200:
                print(
                    f"user {user_id} org {organization['id']}: "
                    f"balance_minor={wallet.get('balance_minor')}"
                )
            status, rows = api.get(
                f"/api/v1/campaigns?organization_id={organization['id']}"
            )
            if status == 200:
                for row in rows:
                    if row["id"] not in {c["id"] for c, _ in all_campaigns}:
                        all_campaigns.append((row, api))
    print(f"{len(all_campaigns)} campaigns visible")

    summary = []
    upc_index = 0
    shelf_groups = set()
    for campaign, api in sorted(all_campaigns, key=lambda c: c[0]["created_at"]):
        campaign_id = campaign["id"]
        name = campaign["name"]
        state = campaign["status"]
        if state == "ARCHIVED":
            continue
        entry = {"campaign": name, "id": campaign_id, "initial_status": state}
        summary.append(entry)

        # 1. Normalize window and budget (budget headroom for grants).
        needs_window = campaign["end_at"] <= now.strftime("%Y-%m-%dT%H:%M:%SZ")
        needs_budget = int(campaign["budget_cap_minor"]) < 2000
        payload = {
            "organization_id": campaign["organization_id"],
            "name": name,
            "description": campaign.get("description") or "",
            "budget_cap_minor": max(int(campaign["budget_cap_minor"]), 2000),
            "start_at": window_start,
            "end_at": window_end,
        }
        if state in {"DRAFT", "PAUSED"}:
            status, body = api.patch_json(f"/api/v1/campaigns/{campaign_id}", payload)
            entry["window_update"] = status
        elif needs_window or needs_budget:
            api.post_json(f"/api/v1/campaigns/{campaign_id}/pause", {})
            status, body = api.patch_json(f"/api/v1/campaigns/{campaign_id}", payload)
            entry["window_update"] = status
            api.post_json(f"/api/v1/campaigns/{campaign_id}/resume", {})
        else:
            entry["window_update"] = "kept"

        # 2. Ensure READY media.
        media = campaign_media(campaign_id)
        ready = [
            m for m in media
            if m["processing_status"] == "READY" and m["kind"] == "image"
        ]
        if not ready:
            content = generate_creative(name, len(summary))
            status, body = api.upload(
                f"/api/v1/campaigns/{campaign_id}/media",
                f"test-creative-{len(summary)}.png",
                content,
                "image/png",
            )
            entry["media_upload"] = (
                body.get("processing_status", status) if isinstance(body, dict) else status
            )
            time.sleep(1)
            media = campaign_media(campaign_id)
            ready = [
                m for m in media
                if m["processing_status"] == "READY" and m["kind"] == "image"
            ]
        entry["ready_media"] = len(ready)
        if not ready:
            entry["skipped"] = "no READY media"
            continue

        # 3. Ensure a UPC mapping for barcode activation.
        status, upcs = api.get(f"/api/v1/campaigns/{campaign_id}/product-upcs")
        if status == 200 and not upcs:
            upc_index += 1
            test_upc = f"9900000000{upc_index:02d}"
            status, body = api.post_json(
                f"/api/v1/campaigns/{campaign_id}/product-upcs",
                {"upc": test_upc, "label": f"Test product {upc_index}"},
            )
            entry["upc_added"] = body.get("upc", status)
        elif status == 200:
            entry["upc_added"] = f"existing:{upcs[0]['upc']}"

        # 4. Publish a signed contract (2-hour MVP validity window) unless a
        # currently valid published contract already exists.
        existing = campaign_state(campaign_id)
        if existing["active_contract_hash"]:
            entry["publish"] = {
                "status": "existing",
                "contract_hash": existing["active_contract_hash"],
            }
            shelf_groups.add(f"shelf-campaign-{campaign_id}")
            contract_hash = existing["active_contract_hash"]
            if device is not None and not existing["open_grants"]:
                status, grant = api.post_json(
                    f"/api/v1/contracts/{contract_hash}/spend-grants",
                    {"device_id": device["device_id"], "amount_minor": 50},
                    headers={
                        "Idempotency-Key": f"promote-{campaign_id}-{now:%Y%m%d%H}"
                    },
                )
                entry["grant"] = grant.get(
                    "grant_id", f"failed {status}: {grant}"
                )
            elif existing["open_grants"]:
                entry["grant"] = f"existing:{existing['open_grants'][0]['grant_id']}"
            continue
        media_ids = [m["id"] for m in ready]
        bid_payload = {
            "total_cap_minor": 100,
            "valid_from": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "valid_to": (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "geography": f"shelf-campaign-{campaign_id}",
            "qfv_price_min_minor": 5,
            "qfv_price_max_minor": 25,
            "max_qfvs_per_play": 4,
            "require_face_count": 1,
            "weather_weight": 0,
            "sports_weight": 0,
            "market_weight": 0,
            "criteria": [
                {
                    "criterion_id": "screen-facing-face",
                    "kind": "attention.face_count",
                    "required": True,
                    "weight": 0,
                    "operator": "gte",
                    "target": 1,
                    "freshness_seconds": 5,
                    "unknown": "ineligible",
                }
            ],
            "media_ids": media_ids,
        }
        status, bid = api.post_json(
            f"/api/v1/campaigns/{campaign_id}/bids", bid_payload
        )
        if status != 200 or "id" not in bid:
            entry["bid"] = f"failed {status}: {bid}"
            continue
        bid_id = bid["id"]
        status, published = api.post_json(f"/api/v1/bids/{bid_id}/publish", {})
        entry["publish"] = {
            "status": status,
            "publication_status": published.get("publication_status"),
            "contract_hash": published.get("canonical_hash"),
        }
        contract_hash = published.get("canonical_hash")
        if status != 200 or not contract_hash:
            continue
        shelf_groups.add(f"shelf-campaign-{campaign_id}")

        # 5. Issue a spend grant for the device; on a funding-cap conflict,
        # release stale grants from earlier sessions and retry once.
        if device is not None:
            grant_payload = {
                "device_id": device["device_id"],
                "amount_minor": 50,
            }
            key = f"promote-{campaign_id}-{now:%Y%m%d%H}"
            status, grant = api.post_json(
                f"/api/v1/contracts/{contract_hash}/spend-grants",
                grant_payload,
                headers={"Idempotency-Key": key},
            )
            if status == 409 and "cap" in str(grant).lower():
                for stale in campaign_state(campaign_id)["open_grants"]:
                    api.post_json(
                        f"/api/v1/spend-grants/{stale['grant_id']}/release", {}
                    )
                status, grant = api.post_json(
                    f"/api/v1/contracts/{contract_hash}/spend-grants",
                    grant_payload,
                    headers={"Idempotency-Key": key + "-retry"},
                )
            entry["grant"] = grant.get("grant_id", f"failed {status}: {grant}")

    # 6. Target the device: merge every shelf group into its profile.
    if device is not None and shelf_groups:
        status, detail = edge_request(
            "GET", f"/internal/v1/fleet/devices/{device['device_id']}"
        )
        profile = detail.get("profile") or {}
        groups = {str(g) for g in profile.get("device_groups", []) if g}
        groups |= shelf_groups
        profile["device_groups"] = sorted(groups)
        status, body = edge_request(
            "PUT",
            f"/internal/v1/devices/{device['device_id']}",
            {
                "organization_id": detail["organization_id"],
                "profile": profile,
                "reset_enrollment": False,
            },
        )
        print("device groups update:", status, profile["device_groups"])

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
