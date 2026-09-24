from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Literal

RangeKey = Literal[
    "last_7d",
    "last_30d",
    "last_90d",
    "this_month",
    "last_12m",
    "custom",
]

RANGE_LABELS: dict[str, str] = {
    "last_7d": "Last 7 days",
    "last_30d": "Last 30 days",
    "last_90d": "Last 90 days",
    "this_month": "This month",
    "last_12m": "Last 12 months",
    "custom": "Custom",
}


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_clock(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.astimezone(UTC)
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def resolve_range(
    range_key: str,
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    *,
    now: datetime | None = None,
) -> tuple[str, datetime, datetime, str]:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    key = range_key if range_key in RANGE_LABELS else "last_30d"
    if key == "custom":
        start = parse_clock(period_start)
        end = parse_clock(period_end)
        if start is None or end is None:
            raise ValueError("Custom range requires period_start and period_end")
        if end <= start:
            raise ValueError("period_end must be after period_start")
        if end - start > timedelta(days=366):
            raise ValueError("Custom range cannot exceed 366 days")
    elif key == "last_7d":
        start, end = current - timedelta(days=7), current
    elif key == "last_90d":
        start, end = current - timedelta(days=90), current
    elif key == "this_month":
        start, end = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0), current
    elif key == "last_12m":
        start, end = current - timedelta(days=365), current
    else:
        key = "last_30d"
        start, end = current - timedelta(days=30), current
    span = end - start
    if span <= timedelta(hours=48):
        granularity = "hour"
    elif span <= timedelta(days=90):
        granularity = "day"
    else:
        granularity = "week"
    return key, start, end, granularity


def bucket_start(value: datetime, granularity: str) -> datetime:
    instant = value.astimezone(UTC)
    if granularity == "hour":
        return instant.replace(minute=0, second=0, microsecond=0)
    if granularity == "week":
        day = instant.replace(hour=0, minute=0, second=0, microsecond=0)
        return day - timedelta(days=day.weekday())
    return instant.replace(hour=0, minute=0, second=0, microsecond=0)


def iter_buckets(start: datetime, end: datetime, granularity: str) -> list[datetime]:
    step = {
        "hour": timedelta(hours=1),
        "day": timedelta(days=1),
        "week": timedelta(days=7),
    }[granularity]
    cursor = bucket_start(start, granularity)
    last = bucket_start(end - timedelta(microseconds=1), granularity)
    buckets: list[datetime] = []
    while cursor <= last:
        buckets.append(cursor)
        cursor += step
    return buckets


def bucket_key(value: str, granularity: str) -> str | None:
    parsed = parse_clock(value)
    if parsed is None:
        return None
    return _iso(bucket_start(parsed, granularity))


def plays_over_time(
    conn: Any,
    user_id: str,
    *,
    range_key: str = "last_30d",
    period_start: datetime | None = None,
    period_end: datetime | None = None,
    campaign_ids: list[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    key, start, end, granularity = resolve_range(
        range_key, period_start, period_end, now=now
    )
    campaigns = [
        {"id": row["id"], "name": row["name"], "status": row["status"]}
        for row in conn.execute(
            """SELECT c.id,c.name,c.status FROM campaigns c
               JOIN organization_memberships m ON m.organization_id=c.organization_id
               WHERE m.user_id=? AND c.archived_at IS NULL
               ORDER BY c.created_at DESC""",
            (user_id,),
        )
    ]
    allowed = {item["id"] for item in campaigns}
    selected = [item for item in (campaign_ids or []) if item]
    if len(selected) > 50:
        raise ValueError("Select at most 50 campaigns")
    unknown = [item for item in selected if item not in allowed]
    if unknown:
        raise ValueError("One or more campaigns were not found")
    clauses = [
        "m.user_id=?",
        "r.execution_at>=?",
        "r.execution_at<?",
    ]
    params: list[Any] = [user_id, _iso(start), _iso(end)]
    if selected:
        clauses.append(f"r.campaign_id IN ({','.join('?' for _ in selected)})")
        params.extend(selected)
    grouped: dict[str, int] = {}
    for row in conn.execute(
        f"""SELECT r.execution_at
            FROM advertiser_settlement_receipts r
            JOIN organization_memberships m
              ON m.organization_id=r.advertiser_organization_id
            WHERE {' AND '.join(clauses)}""",
        params,
    ):
        mapped = bucket_key(row["execution_at"], granularity)
        if mapped:
            grouped[mapped] = grouped.get(mapped, 0) + 1
    series = []
    for bucket in iter_buckets(start, end, granularity):
        stamp = _iso(bucket)
        series.append({"bucket": stamp, "plays": grouped.get(stamp, 0)})
    return {
        "range": key,
        "range_label": RANGE_LABELS[key],
        "granularity": granularity,
        "period_start": _iso(start),
        "period_end": _iso(end),
        "total_plays": sum(item["plays"] for item in series),
        "all_campaigns": not selected,
        "campaign_ids": selected,
        "campaigns": campaigns,
        "series": series,
    }
