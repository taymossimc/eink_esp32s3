#!/usr/bin/env python3
"""Exercise four orientations and four promoted campaigns on the live device."""

from __future__ import annotations

import json
import os
import time
import urllib.request
from datetime import UTC, datetime

BASE = "http://192.168.2.102:8781"
DEVICE = "tmads-eink-a4cb8fdd9120"
CASES = (
    (90, "cmp_51c9da91-963d-4f4c-b1cf-f37180eae70e"),
    (180, "cmp_701e9259-d05b-49bc-b74b-2b863794381e"),
    (270, "cmp_5118ad15-be7f-460a-a73d-fcb791e7a183"),
    (0, "cmp_04fa2b1c-f650-4b59-9fc8-208a5df1e602"),
)


def request(method: str, path: str, payload: dict | None = None) -> dict:
    token = os.environ["EDGE_TOKEN"]
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(BASE + path, data=body, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.loads(response.read() or b"{}")


def main() -> None:
    catalog = request("GET", "/internal/v1/campaign-catalog")
    hashes = {
        campaign["campaign_id"]: campaign["media"][0]["asset_hash"]
        for campaign in catalog["campaigns"]
        if campaign.get("media")
    }
    detail = request("GET", f"/internal/v1/fleet/devices/{DEVICE}")
    original_profile = detail["profile"]
    results = []
    try:
        case_limit = max(1, min(len(CASES), int(os.environ.get("CASE_LIMIT", "4"))))
        for orientation, campaign_id in CASES[:case_limit]:
            profile = dict(original_profile)
            profile["device_groups"] = [f"shelf-campaign-{campaign_id}"]
            request(
                "PUT",
                f"/internal/v1/devices/{DEVICE}",
                {
                    "organization_id": detail["organization_id"],
                    "profile": profile,
                    "reset_enrollment": False,
                },
            )
            started = datetime.now(UTC).isoformat()
            request(
                "PUT",
                f"/internal/v1/devices/{DEVICE}/schedule",
                {
                    "wake_interval_seconds": 300,
                    "awake_timeout_seconds": 1800,
                    "awake_poll_seconds": 30,
                    "display_orientation": orientation,
                },
            )
            expected = hashes[campaign_id]
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                time.sleep(5)
                state = request("GET", f"/internal/v1/fleet/devices/{DEVICE}")
                report = state.get("status_report") or {}
                display = report.get("display") or {}
                if (
                    report.get("received_at", "") >= started
                    and display.get("orientation") == orientation
                    and display.get("active_asset_hash") == expected
                ):
                    results.append(
                        {
                            "orientation": orientation,
                            "campaign_id": campaign_id,
                            "asset_hash": expected,
                            "observed_at": report["received_at"],
                            "elapsed_seconds": round(
                                (
                                    datetime.now(UTC)
                                    - datetime.fromisoformat(started)
                                ).total_seconds(),
                                2,
                            ),
                            "timings": report.get("timings") or {},
                            "storage": report.get("storage") or {},
                        }
                    )
                    print(json.dumps(results[-1]), flush=True)
                    break
            else:
                raise TimeoutError(
                    f"device did not confirm {orientation}° / {campaign_id}"
                )
    finally:
        request(
            "PUT",
            f"/internal/v1/devices/{DEVICE}",
            {
                "organization_id": detail["organization_id"],
                "profile": original_profile,
                "reset_enrollment": False,
            },
        )
    print(json.dumps({"cases": results}, indent=2))


if __name__ == "__main__":
    main()
