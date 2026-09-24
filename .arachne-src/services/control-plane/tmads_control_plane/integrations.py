from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


def _anchor_result(value: Any, *, object_hash: str | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError("Ledger gateway returned an invalid response")
    required = {
        "chain_id": int,
        "transaction_hash": str,
        "block_number": int,
        "contract_address": str,
        "finality": str,
    }
    if any(not isinstance(value.get(key), expected) for key, expected in required.items()):
        raise RuntimeError("Ledger gateway returned an incompatible anchor response")
    transaction_hash = value["transaction_hash"]
    if len(transaction_hash) == 64:
        try:
            int(transaction_hash, 16)
        except ValueError:
            pass
        else:
            value = {**value, "transaction_hash": f"0x{transaction_hash}"}
    if (
        value["chain_id"] <= 0
        or value["block_number"] < 0
        or not value["transaction_hash"].startswith("0x")
        or value["finality"] != "FINAL"
    ):
        raise RuntimeError("Ledger gateway returned an incompatible anchor response")
    if object_hash is not None and value.get("object_hash") != object_hash:
        raise RuntimeError("Ledger gateway anchored a different contract hash")
    return value


@dataclass
class LedgerGatewayClient:
    base_url: str
    bearer: str
    timeout: float
    client: Any = None

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.bearer}"}

    def publish_contract(self, payload_json: str, expected_hash: str) -> dict[str, Any]:
        sender = self.client or httpx
        try:
            response = sender.post(
                f"{self.base_url.rstrip('/')}/api/v1/ledger/contracts",
                content=json.dumps(
                    {"object": json.loads(payload_json), "expected_hash": expected_hash},
                    separators=(",", ":"),
                ),
                headers={**self.headers, "Content-Type": "application/json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            return _anchor_result(response.json(), object_hash=expected_hash)
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError("Ledger gateway is unavailable or rejected the contract") from exc

    def change_contract_state(
        self, contract_hash: str, state: str, lifecycle_event_hash: str
    ) -> dict[str, Any]:
        sender = self.client or httpx
        try:
            response = sender.post(
                f"{self.base_url.rstrip('/')}/api/v1/ledger/contracts/state",
                json={
                    "contract_hash": contract_hash,
                    "state": state,
                    "lifecycle_event_hash": lifecycle_event_hash,
                },
                headers={**self.headers, "Idempotency-Key": lifecycle_event_hash},
                timeout=self.timeout,
            )
            response.raise_for_status()
            return _anchor_result(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError("Ledger gateway is unavailable or rejected the lifecycle event") from exc


def score_curve(value: float, curve: dict[str, Any]) -> float:
    """Evaluate the deterministic v1 score curves and clamp to [0, 1]."""
    kind = str(curve.get("type", "linear"))
    low = float(curve.get("min", 0))
    high = float(curve.get("max", 1))
    if not math.isfinite(value) or not math.isfinite(low) or not math.isfinite(high):
        raise ValueError("Curve values must be finite")
    if kind == "binary":
        score = 1.0 if value >= float(curve.get("threshold", high)) else 0.0
    elif kind in {"linear", "inverse_linear"}:
        if high <= low:
            raise ValueError("Curve max must exceed min")
        score = (value - low) / (high - low)
        if kind == "inverse_linear":
            score = 1 - score
    elif kind == "triangular":
        peak = float(curve.get("peak", (low + high) / 2))
        if not low < peak < high:
            raise ValueError("Triangular curve requires min < peak < max")
        score = (value - low) / (peak - low) if value <= peak else (high - value) / (high - peak)
    elif kind == "trapezoidal":
        rise_end = float(curve.get("rise_end", low))
        fall_start = float(curve.get("fall_start", high))
        if not low <= rise_end <= fall_start <= high:
            raise ValueError("Invalid trapezoidal curve")
        if rise_end <= value <= fall_start:
            score = 1.0
        elif value < rise_end:
            score = 1.0 if rise_end == low else (value - low) / (rise_end - low)
        else:
            score = 1.0 if fall_start == high else (high - value) / (high - fall_start)
    elif kind == "step":
        score = 0.0
        for step in curve.get("steps", []):
            if value >= float(step["at"]):
                score = float(step["score"])
    elif kind in {"distance_decay", "time_proximity_decay"}:
        full = float(curve.get("full_score_until", 0))
        zero = float(curve.get("zero_score_at", high))
        if zero <= full:
            raise ValueError("Decay endpoint must exceed full-score endpoint")
        score = 1.0 if value <= full else (zero - value) / (zero - full)
    else:
        raise ValueError(f"Unsupported score curve: {kind}")
    return round(max(0.0, min(1.0, score)), 12)


def price_preview(
    minimum_minor: int,
    maximum_minor: int,
    weighted_scores: list[tuple[int, float]],
) -> dict[str, Any]:
    if minimum_minor > maximum_minor:
        raise ValueError("Minimum price must not exceed maximum price")
    total_weight = sum(weight for weight, _ in weighted_scores if weight > 0)
    soft_score = (
        sum(weight * max(0.0, min(1.0, score)) for weight, score in weighted_scores if weight > 0)
        / total_weight
        if total_weight
        else 0.0
    )
    exact = minimum_minor + (maximum_minor - minimum_minor) * soft_score
    return {
        "soft_score": round(soft_score, 12),
        "current_qfv_price_minor": round(exact, 6),
        "billing_qfv_price_minor": int(math.floor(exact + 0.5)),
        "formula": "min + (max - min) * soft_score",
    }


@dataclass
class OracleClient:
    base_url: str
    token: str
    timeout: float
    client: Any = None

    def scores(
        self,
        criteria: list[str],
        *,
        market_symbol: str,
        sports_league: str,
        sports_team: str,
    ) -> dict[str, float]:
        try:
            sender = self.client or httpx
            response = sender.get(
                f"{self.base_url.rstrip('/')}/internal/v1/scores",
                params={
                    "criteria": ",".join(criteria),
                    "market_symbol": market_symbol,
                    "sports_league": sports_league,
                    "sports_team": sports_team,
                },
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError("Oracle is unavailable") from exc
        values = body.get("scores") if isinstance(body, dict) else None
        if not isinstance(values, dict):
            raise RuntimeError("Oracle returned an invalid response")
        if set(values) != set(criteria):
            raise RuntimeError("Oracle response did not exactly match requested scores")
        try:
            result = {key: float(values[key]) for key in criteria}
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Oracle response omitted required scores") from exc
        if any(not math.isfinite(value) or not 0 <= value <= 1 for value in result.values()):
            raise RuntimeError("Oracle returned an out-of-range score")
        return result


@dataclass
class EdgeDistributorClient:
    base_url: str
    bearer: str
    timeout: float
    client: Any = None

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.bearer}"}

    def deliver(
        self,
        object_type: str,
        object_key: str,
        payload_json: str | None,
        content_path: str | None,
    ) -> dict[str, Any]:
        url = self.base_url.rstrip("/")
        sender = self.client or httpx
        try:
            if object_type == "contract":
                response = sender.put(
                    f"{url}/internal/v1/contracts/{object_key}",
                    content=payload_json,
                    headers={**self.headers, "Content-Type": "application/json"},
                    timeout=self.timeout,
                )
            elif object_type == "media_manifest":
                response = sender.put(
                    f"{url}/internal/v1/media-manifests/{object_key}",
                    content=payload_json,
                    headers={**self.headers, "Content-Type": "application/json"},
                    timeout=self.timeout,
                )
            elif object_type == "media_content":
                if not content_path:
                    raise RuntimeError("Media outbox row has no content path")
                with Path(content_path).open("rb") as content:
                    response = sender.put(
                        f"{url}/internal/v1/media/{object_key}/content",
                        content=content,
                        headers={**self.headers, "Content-Type": "application/octet-stream"},
                        timeout=self.timeout,
                    )
            elif object_type == "device":
                response = sender.put(
                    f"{url}/internal/v1/devices/{object_key}",
                    content=payload_json,
                    headers={**self.headers, "Content-Type": "application/json"},
                    timeout=self.timeout,
                )
            elif object_type == "spend_grant":
                response = sender.put(
                    f"{url}/internal/v1/spend-grants/{object_key}",
                    content=payload_json,
                    headers={**self.headers, "Content-Type": "application/json"},
                    timeout=self.timeout,
                )
            elif object_type == "spend_grant_revoke":
                response = sender.delete(
                    f"{url}/internal/v1/spend-grants/{object_key}",
                    headers=self.headers,
                    timeout=self.timeout,
                )
            else:
                raise RuntimeError("Unsupported distributor outbox object")
            response.raise_for_status()
            result = response.json()
        except (OSError, httpx.HTTPError, ValueError) as exc:
            raise RuntimeError("Edge distributor is unavailable or rejected the object") from exc
        if not isinstance(result, dict):
            raise RuntimeError("Edge distributor returned an invalid response")
        if object_type == "contract":
            valid = result.get("object_hash") == object_key and isinstance(
                result.get("created"), bool
            )
        elif object_type == "media_manifest":
            valid = result.get("asset_hash") == object_key and isinstance(
                result.get("created"), bool
            )
        elif object_type == "media_content":
            valid = (
                result.get("asset_hash") == object_key
                and result.get("stored") is True
                and isinstance(result.get("bytes"), int)
            )
        elif object_type == "spend_grant":
            valid = result.get("grant_id") == object_key and isinstance(
                result.get("created"), bool
            )
        elif object_type == "spend_grant_revoke":
            valid = result.get("grant_id") == object_key and result.get("revoked") is True
        else:
            status = result.get("status")
            valid = result.get("device_id") == object_key and status in {"pending", "active"}
            if "enrollment_token" in result:
                valid = valid and isinstance(result.get("enrollment_token"), str)
        if not valid:
            raise RuntimeError("Edge distributor returned an incompatible response")
        return result

    def fleet_device(self, device_id: str) -> dict[str, Any]:
        sender = self.client or httpx
        try:
            response = sender.get(
                f"{self.base_url.rstrip('/')}/internal/v1/fleet/devices/{device_id}",
                headers=self.headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError("Edge distributor is unavailable or rejected the request") from exc
        if not isinstance(result, dict) or result.get("device_id") != device_id:
            raise RuntimeError("Edge distributor returned an invalid device response")
        return result

    def device_profile(
        self,
        device_id: str,
        organization_id: str,
        profile: dict[str, Any],
    ) -> dict[str, Any]:
        payload = json.dumps(
            {
                "organization_id": organization_id,
                "profile": profile,
                "reset_enrollment": False,
            },
            separators=(",", ":"),
        )
        return self.deliver("device", device_id, payload, None)

    def list_firmware(self) -> dict[str, Any]:
        sender = self.client or httpx
        response = sender.get(
            f"{self.base_url.rstrip('/')}/internal/v1/firmware",
            headers=self.headers,
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()

    def upload_firmware(
        self,
        artifact_hash: str,
        version: str,
        content_path: str,
        notes: str | None = None,
    ) -> dict[str, Any]:
        sender = self.client or httpx
        params = {"version": version}
        if notes:
            params["notes"] = notes
        with Path(content_path).open("rb") as content:
            response = sender.put(
                f"{self.base_url.rstrip('/')}/internal/v1/firmware/{artifact_hash}/content",
                params=params,
                content=content,
                headers={**self.headers, "Content-Type": "application/octet-stream"},
                timeout=self.timeout,
            )
        response.raise_for_status()
        return response.json()

    def assign_firmware(
        self,
        device_id: str,
        version: str,
        artifact_hash: str,
        wake_interval_seconds: int | None = None,
    ) -> dict[str, Any]:
        sender = self.client or httpx
        payload: dict[str, Any] = {
            "version": version,
            "artifact_hash": artifact_hash,
        }
        if wake_interval_seconds is not None:
            payload["wake_interval_seconds"] = wake_interval_seconds
        response = sender.put(
            f"{self.base_url.rstrip('/')}/internal/v1/devices/{device_id}/firmware-assignment",
            json=payload,
            headers={**self.headers, "Content-Type": "application/json"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response.json()


@dataclass
class SettlementClient:
    base_url: str
    bearer: str
    timeout: float
    client: Any = None

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.bearer}",
            "Content-Type": "application/json",
        }

    def register_contract(self, payload_json: str, expected_hash: str) -> dict[str, Any]:
        sender = self.client or httpx
        try:
            response = sender.post(
                f"{self.base_url.rstrip('/')}/internal/v1/contracts",
                content=json.dumps(
                    {"contract": json.loads(payload_json), "expected_hash": expected_hash},
                    separators=(",", ":"),
                ),
                headers={**self.headers, "Idempotency-Key": expected_hash},
                timeout=self.timeout,
            )
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError("Settlement rejected the contract") from exc
        if (
            not isinstance(result, dict)
            or result.get("contract_hash") != expected_hash
            or result.get("status") not in {"registered", "existing"}
        ):
            raise RuntimeError("Settlement returned an incompatible contract response")
        return result

    def register_grant(self, payload_json: str) -> dict[str, Any]:
        sender = self.client or httpx
        try:
            response = sender.post(
                f"{self.base_url.rstrip('/')}/internal/v1/spend-grants",
                content=json.dumps({"grant": json.loads(payload_json)}, separators=(",", ":")),
                headers=self.headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError("Settlement rejected the spend grant") from exc
        if (
            not isinstance(result, dict)
            or not isinstance(result.get("grant_id"), str)
            or result.get("status") not in {"registered", "existing"}
        ):
            raise RuntimeError("Settlement returned an incompatible grant response")
        return result

    def confirm_grant_unused(self, grant_id: str) -> dict[str, Any]:
        sender = self.client or httpx
        try:
            response = sender.post(
                f"{self.base_url.rstrip('/')}/internal/v1/spend-grants/{grant_id}/release",
                headers={"Authorization": f"Bearer {self.bearer}"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError("Settlement could not confirm unused grant funds") from exc
        if (
            not isinstance(result, dict)
            or result.get("grant_id") != grant_id
            or result.get("status") != "released"
            or not isinstance(result.get("remaining_minor"), int)
        ):
            raise RuntimeError("Settlement returned an incompatible release response")
        return result


def outbox_payload(row: sqlite3.Row) -> tuple[str, str, str | None, str | None]:
    return row["object_type"], row["object_key"], row["payload_json"], row["content_path"]


def normalized_json(value: dict[str, Any]) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)
