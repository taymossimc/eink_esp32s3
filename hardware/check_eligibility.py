import glob
import json
import sqlite3
from datetime import UTC, datetime

path = glob.glob("/var/lib/tmads-edge/*.sqlite3") + glob.glob(
    "/var/lib/tmads/*.sqlite3"
)
conn = sqlite3.connect(path[0])
conn.row_factory = sqlite3.Row

device = conn.execute(
    "SELECT * FROM devices WHERE device_id='tmads-eink-a4cb8fdd9120'"
).fetchone()
profile = json.loads(device["profile_json"])
print("groups:", profile.get("device_groups"))
print("media_profiles:", profile.get("media_profiles"))

now = datetime.now(UTC).isoformat()
for row in conn.execute(
    "SELECT object_hash, payload_json FROM contracts ORDER BY created_at DESC LIMIT 15"
):
    contract = json.loads(row["payload_json"])
    geography = contract.get("geography", {})
    reasons = []
    if contract.get("status") not in {"PUBLISHED", "ACTIVE"}:
        reasons.append(f"status={contract.get('status')}")
    if contract.get("valid_to", "") <= now:
        reasons.append(f"expired {contract.get('valid_to')}")
    if geography.get("type") == "device_group" and geography.get(
        "value"
    ) not in profile.get("device_groups", []):
        reasons.append(f"group-miss {geography.get('value')}")
    profiles = {m.get("profile") for m in contract.get("media", [])}
    print(
        contract.get("campaign_id", "?")[:40],
        "valid_to", contract.get("valid_to"),
        "geo", geography.get("type"), (geography.get("value") or "")[:40],
        "media", profiles,
        "BLOCKED:" if reasons else "RELEVANT",
        ";".join(reasons),
    )
