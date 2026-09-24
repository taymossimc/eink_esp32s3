import glob
import json
import sqlite3

path = glob.glob("/var/lib/tmads-edge/*.sqlite3") + glob.glob(
    "/var/lib/tmads/*.sqlite3"
)
conn = sqlite3.connect(path[0])
conn.row_factory = sqlite3.Row
rows = conn.execute(
    """SELECT received_at, report_json FROM device_status_reports
       WHERE device_id='tmads-eink-a4cb8fdd9120'
         AND received_at > '2026-09-22T03:30'
       ORDER BY received_at DESC LIMIT 20"""
).fetchall()
for row in rows:
    report = json.loads(row["report_json"])
    print(
        row["received_at"],
        report.get("software_version"),
        report.get("wake_reason"),
        "uptime", report.get("uptime_seconds"),
        "active", (report.get("display") or {}).get("active_asset_hash", "")[:20],
        "err", (report.get("display") or {}).get("last_campaign_error", "")[:60],
        "timings", json.dumps(report.get("timings") or {}),
    )
acks = conn.execute(
    """SELECT received_at, detail_json FROM sync_acks
       WHERE device_id='tmads-eink-a4cb8fdd9120'
         AND received_at > '2026-09-22T03:30'
       ORDER BY received_at DESC LIMIT 20"""
).fetchall()
for row in acks:
    detail = json.loads(row["detail_json"])
    print(
        "ACK", row["received_at"],
        "contracts", len(detail.get("applied_contract_hashes", [])),
        "cached", len(detail.get("cached_media_hashes", [])),
        "errors", detail.get("errors"),
    )
