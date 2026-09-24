import glob
import sqlite3

path = [value for value in glob.glob("/var/lib/tmads/*.sqlite3") if "control" in value][0]
conn = sqlite3.connect(path)
conn.row_factory = sqlite3.Row
for row in conn.execute(
    """SELECT id, source_filename, source_hash, derivative_hash, processing_status
       FROM media_assets
       WHERE campaign_id='cmp_04fa2b1c-f650-4b59-9fc8-208a5df1e602'
       ORDER BY created_at"""
):
    print(dict(row))
