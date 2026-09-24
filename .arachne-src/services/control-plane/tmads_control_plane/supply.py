from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx

CELLULAR_TELEMETRY_FIELDS = (
    "iccid",
    "imei",
    "operator_name",
    "mcc",
    "mnc",
    "registration_state",
    "lte_interface",
    "rssi_dbm",
    "rx_bytes",
    "tx_bytes",
    "rx_delta_bytes",
    "tx_delta_bytes",
    "counter_boot_id",
    "interval_started_at",
    "collected_at",
)
CELLULAR_USAGE_PERIODS = ("last_interval", "last_24_hours", "lifetime")
CELLULAR_USAGE_FIELDS = ("rx_bytes", "tx_bytes", "total_bytes")


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _object(value: Any, message: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(message)
    return value


def _items(value: dict[str, Any], key: str, message: str) -> list[dict[str, Any]]:
    items = value.get(key)
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise RuntimeError(message)
    return items


LINK_MODES = {
    "cellular": "Cellular",
    "wifi": "Wifi",
    "ethernet": "Ethernet",
    "nfc": "NFC",
}


def format_clock(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return value
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def format_uptime(seconds: Any, *, extra_seconds: float = 0) -> str | None:
    if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds < 0:
        return None
    total = seconds + max(0, int(extra_seconds))
    days, remainder = divmod(total, 86_400)
    hours, remainder = divmod(remainder, 3_600)
    minutes, _ = divmod(remainder, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


def format_bytes(value: Any) -> str | None:
    """Format a real byte count with deterministic IEC units, preserving absence."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    amount = float(value)
    unit = units[0]
    for candidate in units[1:]:
        if amount < 1024:
            break
        amount /= 1024
        unit = candidate
    if unit == "B":
        return f"{value} B"
    rendered = f"{amount:.1f}".rstrip("0").rstrip(".")
    return f"{rendered} {unit}"


def _cellular_telemetry(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {key: value.get(key) for key in CELLULAR_TELEMETRY_FIELDS}


def _cellular_usage(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    for period in CELLULAR_USAGE_PERIODS:
        usage = value.get(period)
        result[period] = (
            {key: usage.get(key) for key in CELLULAR_USAGE_FIELDS}
            if isinstance(usage, dict)
            else None
        )
    return result


def _usage_display(value: dict[str, Any] | None) -> dict[str, Any]:
    return {
        period: (
            {
                key: format_bytes(usage.get(key))
                for key in CELLULAR_USAGE_FIELDS
            }
            if isinstance(usage := (value or {}).get(period), dict)
            else None
        )
        for period in CELLULAR_USAGE_PERIODS
    }


def _usage_history_item(value: dict[str, Any]) -> dict[str, Any]:
    rx_bytes = value.get("rx_bytes", value.get("rx_delta_bytes"))
    tx_bytes = value.get("tx_bytes", value.get("tx_delta_bytes"))
    total_bytes = value.get("total_bytes")
    if total_bytes is None and isinstance(rx_bytes, int) and isinstance(tx_bytes, int):
        total_bytes = rx_bytes + tx_bytes
    result = {
        "interval_started_at": value.get("interval_started_at"),
        "collected_at": value.get("collected_at"),
        "rx_bytes": rx_bytes,
        "tx_bytes": tx_bytes,
        "total_bytes": total_bytes,
    }
    result["display"] = {
        key: format_bytes(result[key]) for key in CELLULAR_USAGE_FIELDS
    }
    return result


def public_device(value: dict[str, Any]) -> dict[str, Any]:
    """Allow-list fleet fields so upstream key material can never reach the browser."""
    position = value.get("current_position")
    result = {
        key: value.get(key)
        for key in (
            "device_id",
            "organization_id",
            "status",
            "attested",
            "certificate_id",
            "runtime_key_id",
            "created_at",
            "enrolled_at",
            "last_seen_at",
            "last_heartbeat_at",
            "last_ack_cursor",
            "uptime_seconds",
            "boot_id",
            "link_type",
        )
    }
    result["current_position"] = (
        {
            key: position.get(key)
            for key in ("latitude", "longitude", "observed_at")
        }
        if isinstance(position, dict)
        else None
    )
    result["cellular_telemetry"] = _cellular_telemetry(
        value.get("cellular_telemetry")
    )
    result["cellular_usage"] = _cellular_usage(value.get("cellular_usage"))
    profile = value.get("profile")
    if isinstance(profile, dict):
        forbidden = ("public_key", "private_key", "enrollment", "token", "secret")

        def scrub(item: Any) -> Any:
            if isinstance(item, dict):
                return {
                    key: scrub(nested)
                    for key, nested in item.items()
                    if not any(term in key.lower() for term in forbidden)
                }
            if isinstance(item, list):
                return [scrub(nested) for nested in item]
            return item

        result["profile"] = scrub(profile)
    attestation = value.get("attestation")
    if isinstance(attestation, dict):
        result["attestation"] = {
            key: attestation.get(key)
            for key in (
                "certificate_id",
                "manufacturer",
                "issued_at",
                "expires_at",
                "signer_key_id",
            )
        }
    return result


@dataclass
class FleetClient:
    base_url: str
    bearer: str
    timeout: float
    client: Any = None

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        sender = self.client or httpx
        try:
            response = sender.get(
                f"{self.base_url.rstrip('/')}{path}",
                params=params,
                headers={"Authorization": f"Bearer {self.bearer}"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            return _object(response.json(), "Fleet service returned an invalid response")
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError("Fleet service is unavailable") from exc

    def devices(self) -> list[dict[str, Any]]:
        return [
            public_device(item)
            for item in _items(
                self._get("/internal/v1/fleet/devices", {"limit": 1000, "offset": 0}),
                "devices",
                "Fleet service returned an invalid device list",
            )
        ]

    def device(self, device_id: str) -> dict[str, Any]:
        encoded = quote(device_id, safe="")
        return public_device(self._get(f"/internal/v1/fleet/devices/{encoded}"))

    def positions(self, device_id: str, limit: int = 250) -> list[dict[str, Any]]:
        encoded = quote(device_id, safe="")
        return _items(
            self._get(
                f"/internal/v1/fleet/devices/{encoded}/positions",
                {"limit": limit},
            ),
            "positions",
            "Fleet service returned invalid positions",
        )

    def sync_history(self, device_id: str, limit: int = 100) -> list[dict[str, Any]]:
        encoded = quote(device_id, safe="")
        return _items(
            self._get(
                f"/internal/v1/fleet/devices/{encoded}/sync-history",
                {"limit": limit},
            ),
            "sync_history",
            "Fleet service returned invalid sync history",
        )

    def cellular_usage(self, device_id: str, limit: int = 48) -> list[dict[str, Any]]:
        encoded = quote(device_id, safe="")
        body = self._get(
            f"/internal/v1/fleet/devices/{encoded}/cellular-usage",
            {"limit": max(1, min(limit, 250))},
        )
        for key in ("cellular_usage", "intervals", "history"):
            if key in body:
                return _items(body, key, "Fleet service returned invalid cellular usage")
        raise RuntimeError("Fleet service returned invalid cellular usage")

    def movement_alerts(
        self, device_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        path = (
            f"/internal/v1/fleet/devices/{quote(device_id, safe='')}/movement-alerts"
            if device_id
            else "/internal/v1/fleet/movement-alerts"
        )
        return _items(
            self._get(path, {"limit": limit}),
            "movement_alerts",
            "Fleet service returned invalid movement alerts",
        )


@dataclass
class RevenueClient:
    base_url: str
    bearer: str
    timeout: float
    client: Any = None

    def device_revenue(
        self, period_start: datetime, period_end: datetime
    ) -> list[dict[str, Any]]:
        sender = self.client or httpx
        rows: list[dict[str, Any]] = []
        offset = 0
        try:
            while True:
                response = sender.get(
                    f"{self.base_url.rstrip('/')}/internal/v1/reports/device-revenue",
                    params={
                        "period_start": _iso(period_start),
                        "period_end": _iso(period_end),
                        "limit": 1000,
                        "offset": offset,
                    },
                    headers={"Authorization": f"Bearer {self.bearer}"},
                    timeout=self.timeout,
                )
                response.raise_for_status()
                body = _object(response.json(), "Settlement returned an invalid response")
                page = _items(body, "devices", "Settlement returned invalid revenue rows")
                rows.extend(page)
                if len(page) < 1000:
                    return rows
                offset += len(page)
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError("Settlement revenue is unavailable") from exc


@dataclass
class SupplyAggregator:
    fleet: FleetClient | None
    revenue: RevenueClient | None
    _cache: dict[str, tuple[datetime, Any]] = field(default_factory=dict)

    def _load(self, key: str, loader: Callable[[], Any]) -> tuple[Any, dict[str, Any]]:
        try:
            value = loader()
            fetched_at = datetime.now(UTC)
            self._cache[key] = (fetched_at, value)
            return value, {"state": "ok", "fetched_at": _iso(fetched_at), "error": None}
        except RuntimeError as exc:
            cached = self._cache.get(key)
            if cached:
                return cached[1], {
                    "state": "stale",
                    "fetched_at": _iso(cached[0]),
                    "error": str(exc),
                }
            return None, {"state": "error", "fetched_at": None, "error": str(exc)}

    @staticmethod
    def period(
        period_start: datetime | None, period_end: datetime | None
    ) -> tuple[datetime, datetime]:
        end = period_end or datetime.now(UTC)
        start = period_start or end - timedelta(days=30)
        if start.utcoffset() is None or end.utcoffset() is None:
            raise ValueError("period_start and period_end must include a timezone")
        if start >= end:
            raise ValueError("period_start must precede period_end")
        return start.astimezone(UTC), end.astimezone(UTC)

    @staticmethod
    def _revenue_by_device(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        totals: dict[str, dict[str, Any]] = {}
        for row in rows:
            device_id = row.get("device_id")
            currency = row.get("currency")
            if not isinstance(device_id, str) or not isinstance(currency, str):
                continue
            device = totals.setdefault(
                device_id,
                {
                    "currencies": {},
                    "lifetime_receipt_count": 0,
                    "period_receipt_count": 0,
                    "last_execution_at": None,
                },
            )
            amounts = device["currencies"].setdefault(
                currency,
                {"lifetime_revenue_minor": 0, "period_revenue_minor": 0},
            )
            amounts["lifetime_revenue_minor"] += int(row.get("lifetime_revenue_minor", 0))
            amounts["period_revenue_minor"] += int(row.get("period_revenue_minor", 0))
            device["lifetime_receipt_count"] += int(row.get("lifetime_receipt_count", 0))
            device["period_receipt_count"] += int(row.get("period_receipt_count", 0))
            latest = row.get("last_execution_at")
            if isinstance(latest, str) and (
                device["last_execution_at"] is None or latest > device["last_execution_at"]
            ):
                device["last_execution_at"] = latest
        return totals

    @staticmethod
    def _decorate(
        device: dict[str, Any],
        revenue: dict[str, Any] | None,
        alerts: list[dict[str, Any]],
        latest_position: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        result = public_device(device)
        result["revenue"] = revenue or {
            "currencies": {},
            "lifetime_receipt_count": 0,
            "period_receipt_count": 0,
            "last_execution_at": None,
        }
        result["movement_warning"] = bool(alerts)
        result["movement_alert_count"] = len(alerts)
        result["latest_movement_alert"] = alerts[0] if alerts else None
        telemetry = result.get("cellular_telemetry")
        result["cellular_usage_display"] = _usage_display(
            result.get("cellular_usage")
        )
        result["cellular_telemetry_state"] = "unavailable"
        if isinstance(telemetry, dict):
            result["cellular_telemetry_state"] = "unknown"
            collected_at = telemetry.get("collected_at")
            if isinstance(collected_at, str):
                try:
                    age = datetime.now(UTC) - datetime.fromisoformat(
                        collected_at.replace("Z", "+00:00")
                    )
                    result["cellular_telemetry_state"] = (
                        "current" if age <= timedelta(hours=1) else "stale"
                    )
                except ValueError:
                    pass
        if latest_position:
            result["current_fix"] = latest_position
        else:
            position = result.get("current_position")
            result["current_fix"] = position
        battery = result.get("battery_telemetry")
        if isinstance(battery, dict) and battery.get("level_percent") is not None:
            charging = "charging" if battery.get("charging") else "on battery"
            result["battery_summary"] = f"{battery['level_percent']}% · {charging}"
        else:
            result["battery_summary"] = None
        seen = result.get("last_heartbeat_at") or result.get("last_seen_at")
        result["connectivity"] = "unknown"
        age = None
        if isinstance(seen, str):
            try:
                age = datetime.now(UTC) - datetime.fromisoformat(seen.replace("Z", "+00:00"))
                result["connectivity"] = (
                    "online" if age <= timedelta(minutes=5) else
                    "stale" if age <= timedelta(hours=1) else "offline"
                )
            except ValueError:
                age = None
        reported = str(result.get("link_type") or "").lower()
        if reported in LINK_MODES:
            result["link_mode"] = LINK_MODES[reported]
        elif isinstance(result.get("cellular_telemetry"), dict):
            result["link_mode"] = "Cellular"
        else:
            result["link_mode"] = "Unknown"
        extra = age.total_seconds() if age is not None and result["connectivity"] == "online" else 0
        result["uptime_display"] = format_uptime(result.get("uptime_seconds"), extra_seconds=extra)
        result["last_sync_display"] = format_clock(seen)
        result["last_heartbeat_display"] = format_clock(result.get("last_heartbeat_at"))
        result["last_seen_display"] = format_clock(result.get("last_seen_at"))
        result["display_name"] = None
        result["label"] = result.get("device_id")
        if isinstance(result.get("current_fix"), dict):
            result["current_fix"] = {
                **result["current_fix"],
                "observed_at_display": format_clock(result["current_fix"].get("observed_at")),
            }
        return result

    def overview(
        self, period_start: datetime | None = None, period_end: datetime | None = None
    ) -> dict[str, Any]:
        start, end = self.period(period_start, period_end)
        if self.fleet:
            devices, fleet_state = self._load("devices", self.fleet.devices)
            alerts, alert_state = self._load("alerts", self.fleet.movement_alerts)
        else:
            devices, fleet_state = None, {
                "state": "error", "fetched_at": None, "error": "Fleet service is not configured"
            }
            alerts, alert_state = None, dict(fleet_state)
        revenue_key = f"revenue:{_iso(start)}:{_iso(end)}"
        if self.revenue:
            rows, revenue_state = self._load(
                revenue_key, lambda: self.revenue.device_revenue(start, end)
            )
        else:
            rows, revenue_state = None, {
                "state": "error",
                "fetched_at": None,
                "error": "Settlement revenue is not configured",
            }
        alert_rows = alerts or []
        by_device_alerts: dict[str, list[dict[str, Any]]] = {}
        for alert in alert_rows:
            if isinstance(alert.get("device_id"), str):
                by_device_alerts.setdefault(alert["device_id"], []).append(alert)
        by_device_revenue = self._revenue_by_device(rows or [])
        latest_positions: dict[str, dict[str, Any]] = {}
        position_states: dict[str, dict[str, Any]] = {}
        if self.fleet:
            for device in devices or []:
                device_id = str(device.get("device_id"))
                device_positions, state = self._load(
                    f"latest-position:{device_id}",
                    lambda device_id=device_id: self.fleet.positions(device_id, limit=1),
                )
                position_states[device_id] = state
                if device_positions:
                    latest_positions[device_id] = device_positions[0]
        position_values = list(position_states.values())
        position_state = (
            "error"
            if any(item["state"] == "error" for item in position_values)
            else "stale"
            if any(item["state"] == "stale" for item in position_values)
            else "ok"
        )
        joined = [
            self._decorate(
                device,
                by_device_revenue.get(str(device.get("device_id"))),
                by_device_alerts.get(str(device.get("device_id")), []),
                latest_positions.get(str(device.get("device_id"))),
            )
            for device in (devices or [])
        ]
        return {
            "period_start": _iso(start),
            "period_end": _iso(end),
            "devices": joined,
            "sources": {
                "fleet": fleet_state,
                "positions": {
                    "state": position_state,
                    "fetched_at": max(
                        (
                            item["fetched_at"]
                            for item in position_values
                            if item["fetched_at"] is not None
                        ),
                        default=None,
                    ),
                    "error": "; ".join(
                        sorted(
                            {
                                item["error"]
                                for item in position_values
                                if item["error"] is not None
                            }
                        )
                    )
                    or None,
                },
                "movement_alerts": alert_state,
                "revenue": revenue_state,
            },
        }

    def detail(
        self,
        device_id: str,
        period_start: datetime | None = None,
        period_end: datetime | None = None,
    ) -> dict[str, Any]:
        start, end = self.period(period_start, period_end)
        if not self.fleet:
            raise RuntimeError("Fleet service is not configured")
        device, device_state = self._load(
            f"device:{device_id}", lambda: self.fleet.device(device_id)
        )
        if device is None:
            raise RuntimeError(device_state["error"])
        positions, positions_state = self._load(
            f"positions:{device_id}", lambda: self.fleet.positions(device_id)
        )
        sync, sync_state = self._load(
            f"sync:{device_id}", lambda: self.fleet.sync_history(device_id)
        )
        cellular_usage, cellular_usage_state = self._load(
            f"cellular-usage:{device_id}",
            lambda: self.fleet.cellular_usage(device_id),
        )
        alerts, alerts_state = self._load(
            f"device-alerts:{device_id}",
            lambda: self.fleet.movement_alerts(device_id),
        )
        revenue_key = f"revenue:{_iso(start)}:{_iso(end)}"
        if self.revenue:
            revenue_rows, revenue_state = self._load(
                revenue_key, lambda: self.revenue.device_revenue(start, end)
            )
        else:
            revenue_rows, revenue_state = None, {
                "state": "error",
                "fetched_at": None,
                "error": "Settlement revenue is not configured",
            }
        revenue = self._revenue_by_device(revenue_rows or []).get(device_id)
        latest_position = positions[0] if positions else None
        return {
            "period_start": _iso(start),
            "period_end": _iso(end),
            "device": self._decorate(device, revenue, alerts or [], latest_position),
            "positions": positions or [],
            "sync_history": sync or [],
            "cellular_usage_history": [
                _usage_history_item(item) for item in (cellular_usage or [])
            ],
            "movement_alerts": alerts or [],
            "sources": {
                "fleet": device_state,
                "positions": positions_state,
                "sync_history": sync_state,
                "cellular_usage": cellular_usage_state,
                "movement_alerts": alerts_state,
                "revenue": revenue_state,
            },
        }


def apply_operator_labels(
    payload: dict[str, Any], aliases: dict[str, str]
) -> dict[str, Any]:
    def label(device: dict[str, Any]) -> dict[str, Any]:
        device_id = device.get("device_id")
        name = aliases.get(device_id) if isinstance(device_id, str) else None
        device["display_name"] = name.strip() if isinstance(name, str) and name.strip() else None
        device["label"] = device["display_name"] or device_id
        return device

    devices = payload.get("devices")
    if isinstance(devices, list):
        payload["devices"] = [
            label(device) if isinstance(device, dict) else device for device in devices
        ]
    if isinstance(payload.get("device"), dict):
        payload["device"] = label(payload["device"])
    payload["period_start_display"] = format_clock(payload.get("period_start"))
    payload["period_end_display"] = format_clock(payload.get("period_end"))
    return payload
