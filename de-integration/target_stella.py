#!/usr/bin/env python3
"""Target the production Stella creative to the e-ink device."""

import json
import os
import urllib.request

BASE = os.environ.get("EDGE_URL", "http://192.168.2.102:8781")
TOKEN = os.environ["EDGE_TOKEN"]
DEVICE = "tmads-eink-a4cb8fdd9120"
CAMPAIGN = "cmp_04fa2b1c-f650-4b59-9fc8-208a5df1e602"


def request(method: str, path: str, payload: dict | None = None) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=body, method=method)
    req.add_header("Authorization", f"Bearer {TOKEN}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read())


device = request("GET", f"/internal/v1/fleet/devices/{DEVICE}")
profile = dict(device.get("profile") or {})
profile["device_groups"] = [f"shelf-campaign-{CAMPAIGN}"]
profile["active_shelf_upc"] = "087577954431"
profile["display_orientation"] = 0
updated = request(
    "PUT",
    f"/internal/v1/devices/{DEVICE}",
    {
        "organization_id": device["organization_id"],
        "profile": profile,
        "reset_enrollment": False,
    },
)
print(
    json.dumps(
        {
            "device_id": DEVICE,
            "campaign_id": CAMPAIGN,
            "asset_hash": (
                "sha256:4f0af76dbba883af317046987b3f28b1"
                "f5a64fa52192d031474e2daf52c25b51"
            ),
            "profile_update": updated,
        },
        indent=2,
    )
)
