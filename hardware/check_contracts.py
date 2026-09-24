import glob
import json
import sqlite3

path = glob.glob("/var/lib/tmads-edge/*.sqlite3") + glob.glob(
    "/var/lib/tmads/*.sqlite3"
)
conn = sqlite3.connect(path[0])
conn.row_factory = sqlite3.Row
print("edge db:", path[0])
for row in conn.execute(
    """SELECT object_hash, contract_id, status, valid_from, valid_to, created_at
       FROM contracts ORDER BY created_at DESC LIMIT 12"""
):
    print(
        row["created_at"], row["status"],
        row["valid_from"], "->", row["valid_to"],
        row["object_hash"][:24], row["contract_id"][:20],
    )
