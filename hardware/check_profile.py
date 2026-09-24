import glob
import json
import sqlite3

path = glob.glob("/var/lib/tmads-edge/*.sqlite3")
conn = sqlite3.connect(path[0])
conn.row_factory = sqlite3.Row
device = conn.execute(
    "SELECT profile_json FROM devices WHERE device_id='tmads-eink-a4cb8fdd9120'"
).fetchone()
print(json.dumps(json.loads(device["profile_json"]), indent=1, sort_keys=True))
contract = conn.execute(
    """SELECT payload_json FROM contracts
       WHERE object_hash LIKE 'sha256:6751bf05%'"""
).fetchone()
payload = json.loads(contract["payload_json"])
print("criteria:")
for criterion in payload.get("criteria", []):
    print(" ", criterion.get("kind"), "required=", criterion.get("required"))
