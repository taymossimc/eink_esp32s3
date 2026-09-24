#!/usr/bin/env python3
"""End-to-end UPC deployment and display acknowledgement validation."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import urllib.parse
import urllib.request

BASE = "https://digitalengine.leporia.net"
EDGE = "http://192.168.2.102:8781"
DEVICE = "tmads-eink-a4cb8fdd9120"
UPC = "087577954431"


def edge(method: str, path: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(EDGE + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {os.environ['EDGE_TOKEN']}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read() or b"{}")


def create_token() -> str:
    script = f"""
from tmads_control_plane.main import app
from tmads_control_plane.shelf_activation import create_shelf_token
print(create_shelf_token('{DEVICE}', app.state.settings, ttl_hours=1))
"""
    completed = subprocess.run(
        ["docker", "exec", "-i", "tmads-control-plane", "python3", "-"],
        input=script,
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    return completed.stdout.strip().splitlines()[-1]


def api(method: str, path: str, form: dict[str, str] | None = None) -> dict:
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if form is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read() or b"{}")


def main() -> None:
    before = edge("GET", f"/internal/v1/fleet/devices/{DEVICE}")
    original = before["profile"]
    targeted = dict(original)
    targeted["device_groups"] = []
    edge(
        "PUT",
        f"/internal/v1/devices/{DEVICE}",
        {
            "organization_id": before["organization_id"],
            "profile": targeted,
            "reset_enrollment": False,
        },
    )
    edge(
        "PUT",
        f"/internal/v1/devices/{DEVICE}/schedule",
        {
            "wake_interval_seconds": 300,
            "awake_timeout_seconds": 1800,
            "awake_poll_seconds": 30,
            "display_orientation": 0,
        },
    )
    token = create_token()
    deployment = api(
        "POST",
        f"/api/v1/shelf/{urllib.parse.quote(token)}/deploy",
        {"upc": UPC},
    )
    print(json.dumps({"deployment": deployment}), flush=True)
    deployment_id = deployment["deployment_id"]
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        time.sleep(10)
        acknowledgement = api(
            "GET",
            f"/api/v1/shelf/{urllib.parse.quote(token)}/deployments/"
            f"{urllib.parse.quote(deployment_id)}",
        )
        print(json.dumps({"ack": acknowledgement}), flush=True)
        if acknowledgement["stage"] == "displayed":
            break
    else:
        raise TimeoutError("shelf deployment did not reach displayed acknowledgement")
    print(
        json.dumps(
            {
                "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                "deployment": deployment,
                "acknowledgement": acknowledgement,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
