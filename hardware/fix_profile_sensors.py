import glob
import json
import sqlite3
from datetime import UTC, datetime

path = glob.glob("/var/lib/tmads-edge/*.sqlite3")
conn = sqlite3.connect(path[0])
conn.row_factory = sqlite3.Row
device = conn.execute(
    "SELECT profile_json FROM devices WHERE device_id='tmads-eink-a4cb8fdd9120'"
).fetchone()
profile = json.loads(device["profile_json"])
profile["sensors"] = {"usb_camera": True}
conn.execute(
    "UPDATE devices SET profile_json=?, updated_at=? WHERE device_id=?",
    (
        json.dumps(profile, separators=(",", ":")),
        datetime.now(UTC).isoformat(),
        "tmads-eink-a4cb8fdd9120",
    ),
)
conn.commit()
print("sensors restored:", profile["sensors"])
