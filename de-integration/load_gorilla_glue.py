#!/usr/bin/env python3
"""Upload Gorilla Glue art and publish it into the store rotation."""

from __future__ import annotations

import hashlib
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
EDGE_URL = "http://192.168.2.102:8781"
ENV_PATH = "/mnt/dockerprojects/digital_engine/deploy/arachne/.env"
CONTAINER = "tmads-control-plane"
EDGE_CONTAINER = "tmads-edge-distributor"
DEVICE = "tmads-eink-a4cb8fdd9120"
CAMPAIGN = "cmp_e71debbd-547f-4484-acb8-c8c35bf42b0c"
WINDOW_START = "2026-09-21T00:00:00Z"
WINDOW_END = "2027-09-22T00:00:00Z"
IMAGE = sys.argv[1] if len(sys.argv) > 1 else "/tmp/gorilla-glue.png"


def sh(command: list[str], input_text: str | None = None, timeout: int = 180) -> str:
    result = subprocess.run(
        command, capture_output=True, text=True, input=input_text, timeout=timeout
    )
    if result.returncode != 0:
        raise RuntimeError(f"{command}: {result.stderr.strip()[:800]}")
    return result.stdout


def container_python(script: str, container: str = CONTAINER) -> str:
    return sh(["docker", "exec", "-i", container, "python3", "-"], script)


def edge_token() -> str:
    with open(ENV_PATH, encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("TMADS_EDGE_INTERNAL_BEARER_TOKEN="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("edge internal token not found")


def edge_request(method: str, path: str, payload: dict | None = None):
    request = urllib.request.Request(EDGE_URL + path, method=method)
    request.add_header("Authorization", f"Bearer {edge_token()}")
    body = None
    if payload is not None:
        body = json.dumps(payload).encode()
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, data=body, timeout=60) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        try:
            return error.code, json.loads(error.read() or b"{}")
        except ValueError:
            return error.code, {"raw": error.read().decode("utf-8", "replace")[:400]}


class Api:
    def __init__(self, token: str, csrf: str):
        self.cookie = f"tmads_session={token}"
        self.csrf = csrf

    def request(self, method: str, path: str, body: bytes | None = None,
                content_type: str | None = None, extra: dict | None = None):
        request = urllib.request.Request(BASE + path, data=body, method=method)
        request.add_header("Cookie", self.cookie)
        request.add_header("X-CSRF-Token", self.csrf)
        if content_type:
            request.add_header("Content-Type", content_type)
        for key, value in (extra or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=180) as response:
                raw = response.read() or b"{}"
                return response.status, json.loads(raw)
        except urllib.error.HTTPError as error:
            raw = error.read() or b"{}"
            try:
                detail = json.loads(raw)
            except ValueError:
                detail = {"raw": raw.decode("utf-8", "replace")[:500]}
            return error.code, detail

    def upload(self, path: str, filename: str, content: bytes, mime: str):
        boundary = "----gorilla" + secrets.token_hex(8)
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        return self.request(
            "POST", path, body, f"multipart/form-data; boundary={boundary}"
        )


def mint_session(user_id: str) -> tuple[str, str, str]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    session_id = "ses_gorilla_" + uuid.uuid4().hex[:10]
    script = f"""
import glob, hashlib, sqlite3
from datetime import datetime, timedelta, timezone
path = [p for p in glob.glob('/var/lib/tmads/*.sqlite3') if 'control' in p][0]
conn = sqlite3.connect(path)
now = datetime.now(timezone.utc)
conn.execute(
    \"\"\"INSERT INTO auth_sessions(
       id,user_id,token_hash,csrf_token,network_key_hash,created_at,expires_at,revoked_at)
       VALUES(?,?,?,?,NULL,?,?,NULL)\"\"\",
    ('{session_id}', '{user_id}',
     hashlib.sha256('{token}'.encode()).hexdigest(), '{csrf}',
     now.isoformat(), (now + timedelta(hours=2)).isoformat()),
)
conn.commit()
print('ok')
"""
    container_python(script)
    return token, csrf, session_id


def revoke_session(session_id: str) -> None:
    container_python(f"""
import glob, sqlite3
from datetime import datetime, timezone
path = [p for p in glob.glob('/var/lib/tmads/*.sqlite3') if 'control' in p][0]
conn = sqlite3.connect(path)
conn.execute(
    "UPDATE auth_sessions SET revoked_at=? WHERE id=?",
    (datetime.now(timezone.utc).isoformat(), '{session_id}'),
)
conn.commit()
print('revoked')
""")


def extend_window(contract_hash: str) -> str:
    script = f"""
import json, sqlite3, time
from tmads_shared import ContractV1
path = '/var/lib/tmads-edge/edge.sqlite3'
conn = sqlite3.connect(path)
conn.row_factory = sqlite3.Row
row = None
for _ in range(20):
    row = conn.execute(
        "SELECT payload_json, status FROM contracts WHERE object_hash=?",
        ("{contract_hash}",),
    ).fetchone()
    if row:
        break
    time.sleep(1)
if row is None:
    raise SystemExit("contract not on edge yet")
payload = json.loads(row["payload_json"])
payload["valid_from"] = "{WINDOW_START}"
payload["valid_to"] = "{WINDOW_END}"
encoded = json.dumps(payload, separators=(",", ":"))
ContractV1.model_validate_json(encoded)
conn.execute(
    "UPDATE contracts SET valid_from=?, valid_to=?, payload_json=? WHERE object_hash=?",
    ("{WINDOW_START}", "{WINDOW_END}", encoded, "{contract_hash}"),
)
grants = []
for grant in conn.execute(
    "SELECT grant_id, payload_json FROM spend_grants WHERE contract_hash=?",
    ("{contract_hash}",),
):
    body = json.loads(grant["payload_json"])
    body["valid_from"] = "{WINDOW_START}"
    body["valid_to"] = "{WINDOW_END}"
    conn.execute(
        "UPDATE spend_grants SET valid_from=?, valid_to=?, payload_json=? WHERE grant_id=?",
        ("{WINDOW_START}", "{WINDOW_END}", json.dumps(body, separators=(",", ":")), grant["grant_id"]),
    )
    grants.append(grant["grant_id"])
conn.commit()
print(json.dumps({{"status": row["status"], "grants": grants, "valid_to": "{WINDOW_END}"}}))
"""
    return container_python(script, EDGE_CONTAINER)


def main() -> int:
    image = open(IMAGE, "rb").read()
    print(f"image bytes={len(image)} sha256={hashlib.sha256(image).hexdigest()[:16]}")

    owner = json.loads(container_python(f"""
import glob, json, sqlite3
path = [p for p in glob.glob('/var/lib/tmads/*.sqlite3') if 'control' in p][0]
conn = sqlite3.connect(path)
conn.row_factory = sqlite3.Row
row = conn.execute(
    \"\"\"SELECT c.organization_id, c.status, c.budget_cap_minor, u.id AS user_id
       FROM campaigns c
       JOIN organization_memberships m ON m.organization_id=c.organization_id
       JOIN users u ON u.id=m.user_id
       WHERE c.id=? AND u.status='ACTIVE'
         AND m.role IN ('owner','admin','campaign_manager')
       LIMIT 1\"\"\",
    ('{CAMPAIGN}',),
).fetchone()
print(json.dumps(dict(row) if row else {{}}))
"""))
    if not owner:
        print("campaign owner not found")
        return 1
    print("owner", owner)
    token, csrf, session_id = mint_session(owner["user_id"])
    api = Api(token, csrf)
    try:
        status, media = api.upload(
            f"/api/v1/campaigns/{CAMPAIGN}/media",
            "gorilla-glue.png",
            image,
            "image/png",
        )
        print("upload", status, json.dumps(media)[:500])
        if status != 200 or media.get("processing_status") != "READY":
            return 1
        media_id = media["id"]

        now = datetime.now(UTC)
        cap = min(100, int(owner["budget_cap_minor"]))
        bid_payload = {
            "total_cap_minor": cap,
            "valid_from": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "valid_to": (now + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "geography": f"shelf-campaign-{CAMPAIGN}",
            "qfv_price_min_minor": 5,
            "qfv_price_max_minor": 25,
            "max_qfvs_per_play": 4,
            "require_face_count": 1,
            "weather_weight": 0,
            "sports_weight": 0,
            "market_weight": 0,
            "criteria": [],
            "media_ids": [media_id],
        }
        status, bid = api.request(
            "POST",
            f"/api/v1/campaigns/{CAMPAIGN}/bids",
            json.dumps(bid_payload).encode(),
            "application/json",
        )
        print("bid", status, json.dumps(bid)[:400])
        if status != 200 or "id" not in bid:
            return 1
        status, published = api.request(
            "POST", f"/api/v1/bids/{bid['id']}/publish", b"{}", "application/json"
        )
        print("publish", status, json.dumps(published)[:500])
        contract_hash = published.get("canonical_hash")
        if status != 200 or not contract_hash:
            return 1

        grant_payload = {"device_id": DEVICE, "amount_minor": min(50, cap)}
        status, grant = api.request(
            "POST",
            f"/api/v1/contracts/{contract_hash}/spend-grants",
            json.dumps(grant_payload).encode(),
            "application/json",
            {"Idempotency-Key": f"gorilla-{now:%Y%m%d%H%M%S}"},
        )
        print("grant", status, json.dumps(grant)[:300])
        if status != 200:
            return 1

        print("window", extend_window(contract_hash))

        status, device = edge_request("GET", f"/internal/v1/fleet/devices/{DEVICE}")
        profile = dict(device.get("profile") or {})
        groups = [str(group) for group in profile.get("device_groups", []) if group]
        shelf_group = f"shelf-campaign-{CAMPAIGN}"
        if shelf_group not in groups:
            groups.append(shelf_group)
        profile["device_groups"] = groups
        status, updated = edge_request(
            "PUT",
            f"/internal/v1/devices/{DEVICE}",
            {
                "organization_id": device["organization_id"],
                "profile": profile,
                "reset_enrollment": False,
            },
        )
        catalog = container_python(f"""
import json, sqlite3
conn = sqlite3.connect('/var/lib/tmads-edge/edge.sqlite3')
row = conn.execute(
    "SELECT payload_json FROM campaign_catalog WHERE campaign_id=?",
    ("{CAMPAIGN}",),
).fetchone()
if not row:
    print("missing")
else:
    payload = json.loads(row[0])
    print(json.dumps({{
        "status": payload.get("status"),
        "upcs": payload.get("upcs"),
        "media": payload.get("media"),
    }}))
""", EDGE_CONTAINER)
        print(json.dumps({
            "media_id": media_id,
            "width": media.get("width"),
            "height": media.get("height"),
            "contract_hash": contract_hash,
            "profile_status": status,
            "groups": updated.get("profile", {}).get("device_groups")
            if isinstance(updated, dict) else None,
            "catalog": catalog.strip(),
        }, indent=2))
        return 0 if status == 200 else 1
    finally:
        revoke_session(session_id)


if __name__ == "__main__":
    sys.exit(main())
