from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import math
import os
import re
import sqlite3
import tempfile
import threading
import time
import zipfile
from collections import defaultdict, deque
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware
from tmads_shared import (
    ContractV1,
    DeviceAttestationCertificateV1,
    DeviceHeartbeatV1,
    DeviceRegistrationClaimV1,
    MediaManifestV1,
    ObservationV1,
    ProofOfPlayV1,
    SpendGrantV1,
    canonical_sha256,
    decode_ed25519_public_key,
    ed25519_key_id,
    sign_ed25519,
    verify_device_heartbeat,
    verify_registration_claim,
)

from .config import Settings, get_settings
from .db import Database
from .firmware_routes import register_firmware_routes
from .firmware_store import build_firmware_offer, next_wake_at, profile_wake_interval_seconds
from .rendered_media_routes import register_rendered_media_routes
from .security import (
    hash_token,
    key_id,
    load_or_create_signing_key,
    new_enrollment_token,
    parse_public_key,
    verify_bearer,
    verify_device_request,
)

log = logging.getLogger("tmads.edge")
HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name in ("request_id", "method", "path", "status_code", "duration_ms"):
            value = getattr(record, name, None)
            if value is not None:
                payload[name] = value
        return json.dumps(payload, separators=(",", ":"))


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


def now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None = None) -> str:
    return (value or now()).isoformat().replace("+00:00", "Z")


def dump_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def haversine_meters(
    latitude_a: float,
    longitude_a: float,
    latitude_b: float,
    longitude_b: float,
) -> float:
    radius = 6_371_008.8
    lat_a, lat_b = math.radians(latitude_a), math.radians(latitude_b)
    delta_lat = lat_b - lat_a
    delta_lon = math.radians(longitude_b - longitude_a)
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat_a) * math.cos(lat_b) * math.sin(delta_lon / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(1 - value))


def gnss_coordinates(
    observation: ObservationV1,
    *,
    minimum_confidence: float,
    maximum_accuracy_meters: float,
) -> tuple[float, float] | None:
    value = observation.value
    if not isinstance(value, dict) or observation.confidence < minimum_confidence:
        return None
    latitude = value.get("latitude")
    longitude = value.get("longitude")
    if (
        isinstance(latitude, bool)
        or isinstance(longitude, bool)
        or not isinstance(latitude, (int, float))
        or not isinstance(longitude, (int, float))
    ):
        return None
    latitude, longitude = float(latitude), float(longitude)
    if not math.isfinite(latitude) or not math.isfinite(longitude):
        return None
    if not -90 <= latitude <= 90 or not -180 <= longitude <= 180:
        return None
    accuracy = value.get("horizontal_accuracy_meters")
    if accuracy is not None and (
        isinstance(accuracy, bool)
        or not isinstance(accuracy, (int, float))
        or not math.isfinite(float(accuracy))
        or float(accuracy) < 0
        or float(accuracy) > maximum_accuracy_meters
    ):
        return None
    fix_quality = str(value.get("fix_quality", "")).lower()
    if fix_quality in {"invalid", "none", "no_fix"}:
        return None
    return latitude, longitude


def parse_cursor(value: str | None) -> int:
    if value is None:
        return 0
    if not value.startswith("v1."):
        raise ValueError("cursor must use v1 format")
    try:
        sequence = int(value[3:])
    except ValueError as exc:
        raise ValueError("invalid cursor") from exc
    if sequence < 0:
        raise ValueError("invalid cursor")
    return sequence


def format_cursor(sequence: int) -> str:
    return f"v1.{sequence}"


def signed_object(app: FastAPI, payload: dict[str, Any]) -> dict[str, Any]:
    value = {**payload, "signer_key_id": app.state.signer_key_id}
    return {**value, "signature": sign_ed25519(value, app.state.signing_key)}


class DeviceRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    organization_id: str = Field(min_length=1, max_length=200)
    profile: dict[str, Any]
    enrollment_ttl_seconds: int = Field(default=3600, ge=60, le=86_400)
    reset_enrollment: bool = False


class EnrollmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: str = Field(min_length=1, max_length=200)
    enrollment_token: str = Field(min_length=32, max_length=256)
    public_key: str


class SharedMediaRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relative_path: str = Field(min_length=1, max_length=1000)


class AckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cursor: str
    applied_contract_hashes: list[str] = Field(default_factory=list, max_length=10_000)
    cached_media_hashes: list[str] = Field(default_factory=list, max_length=10_000)
    errors: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)


class HelloRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    software_version: str = Field(min_length=1, max_length=200)
    current_cursor: str | None = None
    health: dict[str, Any] = Field(default_factory=dict)


class ReceiptBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    batch_id: str = Field(min_length=1, max_length=512)
    receipts: tuple[ProofOfPlayV1, ...] = Field(min_length=1)


class ReceiptDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    receipt_id: str
    receipt_hash: str
    decision: Literal["ACCEPTED", "REJECTED"]
    reason_code: str
    charge_minor: int | None = None


class ReceiptBatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    batch_id: str
    decisions: tuple[ReceiptDecision, ...]


def safe_import_path(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise ValueError("relative_path must remain inside import root")
    root_resolved = root.resolve()
    resolved = (root_resolved / candidate).resolve()
    if not resolved.is_relative_to(root_resolved):
        raise ValueError("relative_path must remain inside import root")
    return resolved


def media_path(root: Path, asset_hash: str) -> Path:
    if not HASH_RE.fullmatch(asset_hash):
        raise ValueError("invalid SHA-256 asset hash")
    digest = asset_hash.removeprefix("sha256:")
    return root / digest[:2] / digest


def hash_file(path: Path, max_bytes: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    length = 0
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            length += len(block)
            if length > max_bytes:
                raise ValueError("media exceeds configured size limit")
            digest.update(block)
    return f"sha256:{digest.hexdigest()}", length


def required_sensor(kind: str) -> str | None:
    normalized = kind.lower().replace("-", "_")
    if any(word in normalized for word in ("face", "camera", "qfv", "head_pose")):
        return "usb_camera"
    if "mmwave" in normalized or "presence_radar" in normalized:
        return "mmwave"
    if any(word in normalized for word in ("audio", "sound", "microphone")):
        return "microphone"
    return None


def contract_relevant(
    contract: ContractV1, profile: dict[str, Any], current: datetime, lookahead: int
) -> bool:
    if contract.status not in {"PUBLISHED", "ACTIVE"}:
        return False
    if contract.valid_to <= current or contract.valid_from > current + timedelta(seconds=lookahead):
        return False
    geography = contract.geography
    location = profile.get("location", {})
    if geography.type == "device_group":
        if geography.value not in profile.get("device_groups", []):
            return False
    elif geography.type == "geohash_prefix":
        if not str(location.get("geohash", "")).startswith(geography.value):
            return False
    elif geography.type == "polygon_ref":
        if geography.value not in location.get("polygon_refs", []):
            return False
    sensors = profile.get("sensors", {})
    for criterion in contract.criteria:
        sensor = required_sensor(criterion.kind) if criterion.required else None
        if sensor and not bool(sensors.get(sensor)):
            return False
    supported_profiles = set(profile.get("media_profiles", ["tmads-portrait-v1"]))
    if not any(item.profile in supported_profiles for item in contract.media):
        return False
    return True


def relevant_contract_rows(
    db: Database, profile: dict[str, Any], lookahead: int
) -> list[tuple[sqlite3.Row, ContractV1]]:
    result: list[tuple[sqlite3.Row, ContractV1]] = []
    for row in db.all("SELECT * FROM contracts ORDER BY contract_id, revision"):
        contract = ContractV1.model_validate_json(row["payload_json"])
        if contract_relevant(contract, profile, now(), lookahead):
            result.append((row, contract))
    return result


def relevant_media_hashes(contracts: list[tuple[sqlite3.Row, ContractV1]]) -> set[str]:
    return {media.asset_hash for _, contract in contracts for media in contract.media}


def device_row(db: Database, device_id: str) -> sqlite3.Row:
    row = db.one("SELECT * FROM devices WHERE device_id = ?", (device_id,))
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "device not found")
    return row


def create_app(
    settings: Settings | None = None,
    settlement_client: httpx.Client | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.object_root.mkdir(parents=True, exist_ok=True)
    settings.import_root.mkdir(parents=True, exist_ok=True)
    db = Database(settings.db_path)
    signing_key, signer_key_id, public_key = load_or_create_signing_key(settings.key_path)
    try:
        manufacturer_roots = {
            manufacturer: decode_ed25519_public_key(value)
            for manufacturer, value in settings.manufacturer_root_public_keys.items()
        }
    except ValueError as exc:
        raise RuntimeError(f"invalid manufacturer root public key: {exc}") from exc
    configure_logging()
    if bool(settings.settlement_base_url) != bool(settings.settlement_internal_token):
        raise RuntimeError("settlement URL and internal token must be configured together")
    owns_settlement_client = settlement_client is None and bool(settings.settlement_base_url)
    settlement = settlement_client or (
        httpx.Client() if settings.settlement_base_url else None
    )

    def enqueue_settlement(
        connection: sqlite3.Connection,
        object_type: str,
        object_key: str,
        payload: dict[str, Any],
    ) -> None:
        connection.execute(
            """INSERT OR IGNORE INTO settlement_outbox(
               id,object_type,object_key,payload_json,status,attempts,next_attempt_at,
               last_error,created_at,synced_at) VALUES(?,?,?,?, 'PENDING',0,?,NULL,?,NULL)
               ON CONFLICT(object_type,object_key) DO UPDATE SET
               payload_json=excluded.payload_json,status='PENDING',attempts=0,
               next_attempt_at=excluded.next_attempt_at,last_error=NULL,synced_at=NULL""",
            (
                f"{object_type}:{object_key}",
                object_type,
                object_key,
                dump_json(payload),
                iso(),
                iso(),
            ),
        )

    def deliver_settlement_outbox() -> dict[str, int]:
        if settlement is None or not settings.settlement_base_url:
            return {"synced": 0, "failed": 0, "quarantined": 0}
        synced = failed = quarantined = 0
        for row in db.all(
            """SELECT * FROM settlement_outbox
               WHERE status IN ('PENDING','FAILED') AND next_attempt_at<=?
               ORDER BY created_at LIMIT 100""",
            (iso(),),
        ):
            try:
                endpoint = (
                    "/internal/v1/device-keys"
                    if row["object_type"] == "device_key"
                    else "/internal/v1/observations"
                )
                response = settlement.post(
                    settings.settlement_base_url.rstrip("/") + endpoint,
                    content=row["payload_json"],
                    headers={
                        "Authorization": f"Bearer {settings.settlement_internal_token}",
                        "Content-Type": "application/json",
                        "Idempotency-Key": row["object_key"],
                    },
                    timeout=settings.settlement_timeout_seconds,
                )
                response.raise_for_status()
            except (httpx.HTTPError, RuntimeError) as exc:
                attempts = row["attempts"] + 1
                terminal = attempts >= settings.settlement_sync_max_attempts
                with db.transaction(immediate=True) as connection:
                    connection.execute(
                        """UPDATE settlement_outbox SET status=?,attempts=?,
                           next_attempt_at=?,last_error=? WHERE id=?""",
                        (
                            "QUARANTINED" if terminal else "FAILED",
                            attempts,
                            iso(now() + timedelta(seconds=min(3600, 2 ** min(attempts, 12)))),
                            str(exc)[:1000],
                            row["id"],
                        ),
                    )
                quarantined += int(terminal)
                failed += int(not terminal)
                continue
            with db.transaction(immediate=True) as connection:
                connection.execute(
                    """UPDATE settlement_outbox SET status='SYNCED',attempts=attempts+1,
                       last_error=NULL,synced_at=? WHERE id=?""",
                    (iso(), row["id"]),
                )
            synced += 1
        return {"synced": synced, "failed": failed, "quarantined": quarantined}

    async def settlement_sync_forever() -> None:
        while True:
            try:
                await asyncio.to_thread(deliver_settlement_outbox)
            except Exception:
                log.exception("settlement_sync_worker_failed")
            await asyncio.sleep(settings.settlement_sync_interval_seconds)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        log.info("edge_distributor_started")
        sync_task = (
            asyncio.create_task(settlement_sync_forever())
            if settlement is not None
            else None
        )
        try:
            yield
        finally:
            if sync_task:
                sync_task.cancel()
                with suppress(asyncio.CancelledError):
                    await sync_task
            if owns_settlement_client and settlement is not None:
                settlement.close()
            log.info("edge_distributor_stopped")

    app = FastAPI(title="TMAds Edge Distributor", version="1.0.0", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_host_list)
    app.state.settings = settings
    app.state.db = db
    app.state.signing_key = signing_key
    app.state.signer_key_id = signer_key_id
    app.state.public_key = public_key
    rate_buckets: dict[str, deque[float]] = defaultdict(deque)
    registration_rate_buckets: dict[str, deque[float]] = defaultdict(deque)
    rate_lock = threading.Lock()

    @app.middleware("http")
    async def limits_and_logging(request: Request, call_next: Any) -> Response:
        started = time.monotonic()
        request_id = os.urandom(8).hex()
        path = request.url.path
        media_upload = path.startswith("/internal/v1/media/") and path.endswith("/content")
        maximum = settings.max_media_bytes if media_upload else settings.max_request_bytes
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > maximum:
                    return JSONResponse(413, {"detail": "request body too large"})
            except ValueError:
                return JSONResponse(400, {"detail": "invalid content-length"})
        identity = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        identity = identity or (request.client.host if request.client else "unknown")
        current = time.monotonic()
        if path in {
            "/api/v1/devices/register",
            "/api/v1/devices/registration-claims",
        }:
            with rate_lock:
                registration_bucket = registration_rate_buckets[identity]
                while registration_bucket and registration_bucket[0] <= current - 60:
                    registration_bucket.popleft()
                if len(registration_bucket) >= settings.registration_rate_limit_per_minute:
                    return JSONResponse(429, {"detail": "registration rate limit exceeded"})
                registration_bucket.append(current)
        identity = f"{identity}:{path.split('/')[1:5]}"
        with rate_lock:
            bucket = rate_buckets[identity]
            while bucket and bucket[0] <= current - 60:
                bucket.popleft()
            if len(bucket) >= settings.rate_limit_per_minute:
                return JSONResponse(429, {"detail": "rate limit exceeded"})
            bucket.append(current)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        log.info(
            "http_request",
            extra={
                "request_id": request_id,
                "method": request.method,
                "path": path,
                "status_code": response.status_code,
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
            },
        )
        return response

    def internal_auth(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        if not verify_bearer(authorization, settings.internal_bearer_token):
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "invalid internal bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    def operator_audit(
        connection: sqlite3.Connection,
        action: str,
        object_type: str,
        object_id: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """INSERT INTO operator_audit(
               action,object_type,object_id,detail_json,created_at)
               VALUES(?,?,?,?,?)""",
            (action, object_type, object_id, dump_json(detail or {}), iso()),
        )

    def persist_position(
        connection: sqlite3.Connection,
        device_id: str,
        observation: ObservationV1,
        *,
        source: str,
    ) -> tuple[bool, int | None]:
        coordinates = gnss_coordinates(
            observation,
            minimum_confidence=settings.location_min_confidence,
            maximum_accuracy_meters=settings.location_max_accuracy_meters,
        )
        if coordinates is None:
            return False, None
        latitude, longitude = coordinates
        received_at = iso()
        connection.execute(
            """INSERT INTO device_positions(
               device_id,observation_id,latitude,longitude,confidence,
               observed_at,received_at,source) VALUES(?,?,?,?,?,?,?,?)""",
            (
                device_id,
                observation.observation_id,
                latitude,
                longitude,
                observation.confidence,
                iso(observation.observed_at),
                received_at,
                source,
            ),
        )
        row = connection.execute(
            """SELECT movement_anchor_latitude,movement_anchor_longitude,
                      movement_candidate_latitude,movement_candidate_longitude,
                      movement_candidate_at
               FROM devices WHERE device_id=?""",
            (device_id,),
        ).fetchone()
        alert_id: int | None = None
        if row["movement_anchor_latitude"] is None:
            connection.execute(
                """UPDATE devices SET current_latitude=?,current_longitude=?,
                   current_position_at=?,movement_anchor_latitude=?,
                   movement_anchor_longitude=? WHERE device_id=?""",
                (latitude, longitude, iso(observation.observed_at), latitude, longitude, device_id),
            )
        else:
            baseline_latitude = float(row["movement_anchor_latitude"])
            baseline_longitude = float(row["movement_anchor_longitude"])
            baseline_distance = haversine_meters(
                baseline_latitude, baseline_longitude, latitude, longitude
            )
            candidate_latitude = row["movement_candidate_latitude"]
            candidate_longitude = row["movement_candidate_longitude"]
            updates: tuple[Any, ...]
            if baseline_distance < settings.movement_jitter_meters:
                updates = (None, None, None)
            elif baseline_distance < settings.movement_threshold_meters:
                updates = (None, None, None)
            elif candidate_latitude is None or candidate_longitude is None:
                updates = (latitude, longitude, iso(observation.observed_at))
            else:
                confirmation_distance = haversine_meters(
                    float(candidate_latitude),
                    float(candidate_longitude),
                    latitude,
                    longitude,
                )
                if confirmation_distance <= settings.movement_confirmation_radius_meters:
                    cursor = connection.execute(
                        """INSERT INTO movement_alerts(
                           device_id,baseline_latitude,baseline_longitude,
                           detected_latitude,detected_longitude,distance_meters,
                           first_observed_at,confirmed_at) VALUES(?,?,?,?,?,?,?,?)""",
                        (
                            device_id,
                            baseline_latitude,
                            baseline_longitude,
                            latitude,
                            longitude,
                            baseline_distance,
                            row["movement_candidate_at"],
                            iso(observation.observed_at),
                        ),
                    )
                    alert_id = int(cursor.lastrowid)
                    connection.execute(
                        """UPDATE devices SET movement_anchor_latitude=?,
                           movement_anchor_longitude=? WHERE device_id=?""",
                        (latitude, longitude, device_id),
                    )
                    updates = (None, None, None)
                else:
                    updates = (latitude, longitude, iso(observation.observed_at))
            connection.execute(
                """UPDATE devices SET current_latitude=?,current_longitude=?,
                   current_position_at=?,movement_candidate_latitude=?,
                   movement_candidate_longitude=?,movement_candidate_at=?
                   WHERE device_id=?""",
                (
                    latitude,
                    longitude,
                    iso(observation.observed_at),
                    *updates,
                    device_id,
                ),
            )
        cutoff = iso(now() - timedelta(days=settings.location_history_retention_days))
        connection.execute(
            "DELETE FROM device_positions WHERE device_id=? AND observed_at<?",
            (device_id, cutoff),
        )
        connection.execute(
            """DELETE FROM device_positions WHERE id IN (
               SELECT id FROM device_positions WHERE device_id=?
               ORDER BY observed_at DESC,id DESC LIMIT -1 OFFSET ?)""",
            (device_id, settings.location_history_max_points),
        )
        return True, alert_id

    def persist_cellular_telemetry(
        connection: sqlite3.Connection,
        device_id: str,
        heartbeat: DeviceHeartbeatV1,
        *,
        received_at: str,
    ) -> None:
        telemetry = heartbeat.cellular_telemetry
        if telemetry is None:
            return
        values = (
            device_id,
            heartbeat.nonce,
            iso(heartbeat.sent_at),
            heartbeat.signer_key_id,
            heartbeat.signature,
            telemetry.iccid,
            telemetry.imei,
            telemetry.operator_name,
            telemetry.mcc,
            telemetry.mnc,
            telemetry.registration_state,
            telemetry.lte_interface,
            telemetry.rssi_dbm,
            telemetry.rx_bytes,
            telemetry.tx_bytes,
            telemetry.rx_delta_bytes,
            telemetry.tx_delta_bytes,
            telemetry.counter_boot_id,
            iso(telemetry.interval_started_at),
            iso(telemetry.collected_at),
            received_at,
        )
        columns = """device_id,heartbeat_nonce,heartbeat_sent_at,
                     heartbeat_signer_key_id,heartbeat_signature,iccid,imei,
                     operator_name,mcc,mnc,registration_state,lte_interface,rssi_dbm,
                     rx_bytes,tx_bytes,rx_delta_bytes,tx_delta_bytes,counter_boot_id,
                     interval_started_at,collected_at,received_at"""
        connection.execute(
            f"""INSERT INTO cellular_usage_history({columns})
                VALUES({",".join("?" for _ in values)})""",
            values,
        )
        connection.execute(
            f"""INSERT INTO device_cellular_latest({columns})
                VALUES({",".join("?" for _ in values)})
                ON CONFLICT(device_id) DO UPDATE SET
                  heartbeat_nonce=excluded.heartbeat_nonce,
                  heartbeat_sent_at=excluded.heartbeat_sent_at,
                  heartbeat_signer_key_id=excluded.heartbeat_signer_key_id,
                  heartbeat_signature=excluded.heartbeat_signature,
                  iccid=excluded.iccid,imei=excluded.imei,
                  operator_name=excluded.operator_name,mcc=excluded.mcc,mnc=excluded.mnc,
                  registration_state=excluded.registration_state,
                  lte_interface=excluded.lte_interface,rssi_dbm=excluded.rssi_dbm,
                  rx_bytes=excluded.rx_bytes,tx_bytes=excluded.tx_bytes,
                  rx_delta_bytes=excluded.rx_delta_bytes,
                  tx_delta_bytes=excluded.tx_delta_bytes,
                  counter_boot_id=excluded.counter_boot_id,
                  interval_started_at=excluded.interval_started_at,
                  collected_at=excluded.collected_at,received_at=excluded.received_at
                WHERE excluded.collected_at >= device_cellular_latest.collected_at""",
            values,
        )
        connection.execute(
            """INSERT INTO cellular_usage_totals(device_id,rx_bytes,tx_bytes,updated_at)
               VALUES(?,?,?,?)
               ON CONFLICT(device_id) DO UPDATE SET
                 rx_bytes=cellular_usage_totals.rx_bytes+excluded.rx_bytes,
                 tx_bytes=cellular_usage_totals.tx_bytes+excluded.tx_bytes,
                 updated_at=excluded.updated_at""",
            (
                device_id,
                telemetry.rx_delta_bytes,
                telemetry.tx_delta_bytes,
                received_at,
            ),
        )
        connection.execute(
            """INSERT INTO cellular_usage_recent(
                 device_id,heartbeat_nonce,rx_bytes,tx_bytes,received_at)
               VALUES(?,?,?,?,?)""",
            (
                device_id,
                heartbeat.nonce,
                telemetry.rx_delta_bytes,
                telemetry.tx_delta_bytes,
                received_at,
            ),
        )
        connection.execute(
            "DELETE FROM cellular_usage_recent WHERE received_at<?",
            (iso(now() - timedelta(hours=24)),),
        )
        connection.execute(
            "DELETE FROM cellular_usage_history WHERE device_id=? AND received_at<?",
            (
                device_id,
                iso(now() - timedelta(days=settings.cellular_usage_retention_days)),
            ),
        )
        connection.execute(
            """DELETE FROM cellular_usage_history WHERE id IN (
                 SELECT id FROM cellular_usage_history WHERE device_id=?
                 ORDER BY received_at DESC,id DESC LIMIT -1 OFFSET ?)""",
            (device_id, settings.cellular_usage_max_rows_per_device),
        )

    def persist_battery_telemetry(
        connection: sqlite3.Connection,
        device_id: str,
        heartbeat: DeviceHeartbeatV1,
        *,
        received_at: str,
    ) -> None:
        telemetry = heartbeat.battery_telemetry
        if telemetry is None:
            return
        connection.execute(
            """INSERT INTO device_battery_latest(
                   device_id, heartbeat_nonce, heartbeat_sent_at, level_percent,
                   charging, voltage_mv, temperature_c, collected_at, received_at)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(device_id) DO UPDATE SET
                 heartbeat_nonce=excluded.heartbeat_nonce,
                 heartbeat_sent_at=excluded.heartbeat_sent_at,
                 level_percent=excluded.level_percent,
                 charging=excluded.charging,
                 voltage_mv=excluded.voltage_mv,
                 temperature_c=excluded.temperature_c,
                 collected_at=excluded.collected_at,
                 received_at=excluded.received_at
               WHERE excluded.collected_at >= device_battery_latest.collected_at""",
            (
                device_id,
                heartbeat.nonce,
                iso(heartbeat.sent_at),
                telemetry.level_percent,
                None if telemetry.charging is None else int(telemetry.charging),
                telemetry.voltage_mv,
                telemetry.temperature_c,
                iso(telemetry.collected_at),
                received_at,
            ),
        )

    def cellular_telemetry_payload(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "iccid": row["iccid"],
            "imei": row["imei"],
            "operator_name": row["operator_name"],
            "mcc": row["mcc"],
            "mnc": row["mnc"],
            "registration_state": row["registration_state"],
            "lte_interface": row["lte_interface"],
            "rssi_dbm": row["rssi_dbm"],
            "rx_bytes": row["rx_bytes"],
            "tx_bytes": row["tx_bytes"],
            "rx_delta_bytes": row["rx_delta_bytes"],
            "tx_delta_bytes": row["tx_delta_bytes"],
            "counter_boot_id": row["counter_boot_id"],
            "interval_started_at": row["interval_started_at"],
            "collected_at": row["collected_at"],
        }

    def usage_bytes(rx_bytes: int, tx_bytes: int) -> dict[str, int]:
        return {
            "rx_bytes": rx_bytes,
            "tx_bytes": tx_bytes,
            "total_bytes": rx_bytes + tx_bytes,
        }

    def battery_telemetry_payload(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "level_percent": row["level_percent"],
            "charging": bool(row["charging"]) if row["charging"] is not None else None,
            "voltage_mv": row["voltage_mv"],
            "temperature_c": row["temperature_c"],
            "collected_at": row["collected_at"],
        }

    def battery_fleet_fields(device_id: str) -> dict[str, Any]:
        latest = db.one(
            "SELECT * FROM device_battery_latest WHERE device_id=?",
            (device_id,),
        )
        return {"battery_telemetry": battery_telemetry_payload(latest)}

    def cellular_fleet_fields(device_id: str) -> dict[str, Any]:
        latest = db.one(
            "SELECT * FROM device_cellular_latest WHERE device_id=?",
            (device_id,),
        )
        if latest is None:
            return {"cellular_telemetry": None, "cellular_usage": None}
        last_interval = db.one(
            """SELECT rx_delta_bytes,tx_delta_bytes FROM cellular_usage_history
               WHERE device_id=? ORDER BY received_at DESC,id DESC LIMIT 1""",
            (device_id,),
        )
        recent = db.one(
            """SELECT COALESCE(SUM(rx_bytes),0) AS rx_bytes,
                      COALESCE(SUM(tx_bytes),0) AS tx_bytes
               FROM cellular_usage_recent WHERE device_id=? AND received_at>=?""",
            (device_id, iso(now() - timedelta(hours=24))),
        )
        lifetime = db.one(
            "SELECT rx_bytes,tx_bytes FROM cellular_usage_totals WHERE device_id=?",
            (device_id,),
        )
        return {
            "cellular_telemetry": cellular_telemetry_payload(latest),
            "cellular_usage": {
                "last_interval": usage_bytes(
                    int(last_interval["rx_delta_bytes"]) if last_interval else 0,
                    int(last_interval["tx_delta_bytes"]) if last_interval else 0,
                ),
                "last_24_hours": usage_bytes(
                    int(recent["rx_bytes"]) if recent else 0,
                    int(recent["tx_bytes"]) if recent else 0,
                ),
                "lifetime": usage_bytes(
                    int(lifetime["rx_bytes"]) if lifetime else 0,
                    int(lifetime["tx_bytes"]) if lifetime else 0,
                ),
            },
        }

    def attested_registration_response(
        device_id: str, claim_hash: str, *, created: bool
    ) -> dict[str, Any]:
        outbox = db.one(
            """SELECT status FROM settlement_outbox
               WHERE object_type='device_key' AND object_key=(
                   SELECT key_id FROM devices WHERE device_id=?)""",
            (device_id,),
        )
        return {
            "schema": "tmads.device-registration-result.v1",
            "device_id": device_id,
            "status": "active",
            "created": created,
            "claim_hash": claim_hash,
            "heartbeat_interval_seconds": settings.heartbeat_interval_seconds,
            "server_time": iso(),
            "server_key_id": signer_key_id,
            "server_public_key": public_key,
            "settlement_sync_status": (
                outbox["status"] if settlement is not None and outbox else "NOT_CONFIGURED"
            ),
        }

    async def authenticate_device(request: Request, device_id: str) -> sqlite3.Row:
        body = await request.body()
        try:
            verify_device_request(
                db,
                device_id=device_id,
                timestamp=request.headers.get("x-tmads-timestamp"),
                nonce=request.headers.get("x-tmads-nonce"),
                signature=request.headers.get("x-tmads-signature"),
                method=request.method,
                path=request.url.path,
                body=body,
                window_seconds=settings.request_window_seconds,
            )
        except PermissionError as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
        return device_row(db, device_id)

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def readiness() -> dict[str, str]:
        try:
            db.one("SELECT 1")
            probe = settings.object_root / ".write-probe"
            probe.write_bytes(b"")
            probe.unlink()
        except OSError as exc:
            raise HTTPException(503, "storage unavailable") from exc
        return {"status": "ready", "signer_key_id": signer_key_id}

    @app.get("/.well-known/tmads-edge-key")
    def edge_key() -> dict[str, str]:
        return {"algorithm": "Ed25519", "key_id": signer_key_id, "public_key": public_key}

    @app.get("/api/v1/relay/capabilities")
    def relay_capabilities() -> dict[str, Any]:
        return {
            "https_store_and_forward": True,
            "signed_bundle_export": True,
            "receipt_bundle_import": bool(settings.settlement_base_url),
            "removable_media_compatible": True,
            "bluetooth_transport": False,
            "wifi_direct_transport": False,
        }

    @app.get(
        "/internal/v1/relay/{device_id}/bundle",
        dependencies=[Depends(internal_auth)],
    )
    def export_relay_bundle(device_id: str) -> Response:
        row = device_row(db, device_id)
        profile = json.loads(row["profile_json"])
        contracts = relevant_contract_rows(
            db, profile, settings.manifest_lookahead_seconds
        )
        media_hashes = relevant_media_hashes(contracts)
        files: list[tuple[str, str, bytes]] = []
        for contract_row, contract in contracts:
            wire = contract.model_dump(mode="json", by_alias=True, exclude_none=True)
            wire["contract_hash"] = contract_row["object_hash"]
            files.append(
                (
                    f"contracts/{contract_row['object_hash'].removeprefix('sha256:')}.json",
                    "contract",
                    dump_json(wire).encode(),
                )
            )
        for grant in db.all(
            "SELECT * FROM spend_grants WHERE device_id=? AND valid_to>?",
            (device_id, iso()),
        ):
            files.append(
                (
                    f"spend-grants/{grant['grant_id']}.json",
                    "spend_grant",
                    grant["payload_json"].encode(),
                )
            )
        for observation in db.all(
            """SELECT * FROM observations
               WHERE (target_device_id IS NULL OR target_device_id=?)
               AND valid_until>?""",
            (device_id, iso()),
        ):
            files.append(
                (
                    f"observations/{observation['observation_id']}.json",
                    "observation",
                    observation["payload_json"].encode(),
                )
            )
        for asset_hash in sorted(media_hashes):
            manifest_row = db.one(
                "SELECT * FROM media_manifests WHERE asset_hash=?", (asset_hash,)
            )
            if manifest_row and manifest_row["object_path"]:
                path = media_path(settings.object_root, asset_hash)
                calculated, length = hash_file(path, settings.max_media_bytes)
                if calculated != asset_hash or length != manifest_row["byte_length"]:
                    raise HTTPException(409, f"media verification failed: {asset_hash}")
                files.append(
                    (
                        f"media/{asset_hash.removeprefix('sha256:')}",
                        "media",
                        path.read_bytes(),
                    )
                )
        entries = [
            {
                "path": name,
                "kind": kind,
                "size": len(content),
                "sha256": "sha256:" + hashlib.sha256(content).hexdigest(),
            }
            for name, kind, content in files
        ]
        manifest: dict[str, Any] = {
            "schema": "tmads.sync-bundle.v1",
            "bundle_id": f"relay-{device_id}-{db.max_sequence()}",
            "sequence": db.max_sequence(),
            "issued_at": iso(),
            "source": signer_key_id,
            "target": device_id,
            "files": entries,
        }
        canonical = dump_json(manifest).encode()
        manifest["signature"] = base64.b64encode(
            app.state.signing_key.sign(canonical)
        ).decode()
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", dump_json(manifest))
            for name, _kind, content in files:
                archive.writestr(name, content)
        return Response(
            output.getvalue(),
            media_type="application/zip",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="tmads-relay-{device_id}.zip"'
                ),
                "X-TMAds-Bundle-SHA256": hashlib.sha256(
                    output.getvalue()
                ).hexdigest(),
            },
        )

    @app.post(
        "/internal/v1/relay/{device_id}/receipts",
        dependencies=[Depends(internal_auth)],
        response_model=ReceiptBatchResult,
    )
    def import_relay_receipts(
        device_id: str, value: ReceiptBatch
    ) -> ReceiptBatchResult:
        return forward_receipts(device_id, value)

    @app.get(
        "/internal/v1/settlement-sync",
        dependencies=[Depends(internal_auth)],
    )
    def settlement_sync_status() -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in db.all(
                """SELECT object_type,object_key,status,attempts,next_attempt_at,
                   last_error,created_at,synced_at FROM settlement_outbox
                   ORDER BY created_at DESC LIMIT 500"""
            )
        ]

    @app.post(
        "/internal/v1/settlement-sync/retry",
        dependencies=[Depends(internal_auth)],
    )
    def retry_settlement_sync() -> dict[str, int]:
        db.execute(
            """UPDATE settlement_outbox SET status='PENDING',attempts=0,
               next_attempt_at=?,last_error=NULL WHERE status='QUARANTINED'""",
            (iso(),),
        )
        return deliver_settlement_outbox()

    @app.put(
        "/internal/v1/devices/{device_id}",
        dependencies=[Depends(internal_auth)],
    )
    def register_device(device_id: str, value: DeviceRegistration) -> dict[str, Any]:
        if not device_id or len(device_id) > 200:
            raise HTTPException(422, "invalid device_id")
        created = iso()
        existing = db.one("SELECT * FROM devices WHERE device_id = ?", (device_id,))
        token: str | None = None
        if existing is None or value.reset_enrollment:
            token = new_enrollment_token()
        expires = iso(now() + timedelta(seconds=value.enrollment_ttl_seconds)) if token else None
        with db.transaction(immediate=True) as connection:
            if existing is None:
                connection.execute(
                    """INSERT INTO devices(
                       device_id, organization_id, status, profile_json,
                       enrollment_token_hash, enrollment_expires_at, created_at, updated_at
                    ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        device_id,
                        value.organization_id,
                        "pending",
                        dump_json(value.profile),
                        hash_token(token or ""),
                        expires,
                        created,
                        created,
                    ),
                )
            else:
                fields: list[Any] = [
                    value.organization_id,
                    dump_json(value.profile),
                    created,
                    device_id,
                ]
                connection.execute(
                    "UPDATE devices SET organization_id=?, profile_json=?, updated_at=? "
                    "WHERE device_id=?",
                    fields,
                )
                if token:
                    if existing["key_id"] and existing["public_key"]:
                        enqueue_settlement(
                            connection,
                            "device_key",
                            existing["key_id"],
                            {
                                "device_id": device_id,
                                "publisher_organization_id": existing["organization_id"],
                                "signer_key_id": existing["key_id"],
                                "public_key": "ed25519:"
                                + base64.urlsafe_b64encode(existing["public_key"])
                                .rstrip(b"=")
                                .decode(),
                                "valid_from": existing["enrolled_at"] or existing["created_at"],
                                "valid_to": created,
                                "revoked": True,
                            },
                        )
                    connection.execute(
                        """UPDATE devices SET status='pending', public_key=NULL, key_id=NULL,
                           enrollment_token_hash=?, enrollment_expires_at=?, enrolled_at=NULL
                           WHERE device_id=?""",
                        (hash_token(token), expires, device_id),
                    )
            operator_audit(
                connection,
                "device.enrollment_rotated" if existing and token else "device.registered",
                "device",
                device_id,
                {"organization_id": value.organization_id, "token_issued": bool(token)},
            )
        response: dict[str, Any] = {
            "device_id": device_id,
            "status": "pending" if token else existing["status"],
        }
        if token:
            response["enrollment_token"] = token
            response["enrollment_expires_at"] = expires
        return response

    @app.post("/api/v1/devices/register")
    @app.post("/api/v1/devices/registration-claims", include_in_schema=False)
    def register_attested_device(claim: DeviceRegistrationClaimV1) -> dict[str, Any]:
        claim_hash = canonical_sha256(claim)
        exact = db.one(
            "SELECT device_id FROM registration_claims WHERE claim_hash=?",
            (claim_hash,),
        )
        if exact is not None:
            return attested_registration_response(exact["device_id"], claim_hash, created=False)
        manufacturer_root = manufacturer_roots.get(claim.certificate.manufacturer)
        if manufacturer_root is None:
            raise HTTPException(401, "untrusted device manufacturer")
        try:
            valid = verify_registration_claim(
                claim,
                manufacturer_root,
                now=now(),
                expected_nonce=claim.nonce,
                max_age=timedelta(seconds=settings.registration_freshness_seconds),
                expected_device_id=claim.device_id,
            )
        except ValueError as exc:
            raise HTTPException(422, f"invalid attestation key binding: {exc}") from exc
        if not valid:
            raise HTTPException(401, "invalid or stale device registration claim")
        if gnss_coordinates(
            claim.gnss_observation,
            minimum_confidence=settings.location_min_confidence,
            maximum_accuracy_meters=settings.location_max_accuracy_meters,
        ) is None:
            raise HTTPException(422, "GNSS fix does not meet registration quality requirements")
        runtime_key = decode_ed25519_public_key(
            claim.certificate.runtime_device_public_key
        )
        runtime_key_id = ed25519_key_id(runtime_key)
        raw_runtime_key = parse_public_key(claim.certificate.runtime_device_public_key)
        timestamp = iso()
        claim_json = dump_json(
            claim.model_dump(mode="json", by_alias=True, exclude_none=True)
        )
        profile = {
            "attested": True,
            "manufacturer": claim.certificate.manufacturer,
            "sensors": {
                "location": True,
                "sensor_producer": claim.certificate.sensor_producer_public_key is not None,
            },
        }
        try:
            with db.transaction(immediate=True) as connection:
                exact = connection.execute(
                    "SELECT device_id FROM registration_claims WHERE claim_hash=?",
                    (claim_hash,),
                ).fetchone()
                if exact is not None:
                    return attested_registration_response(
                        exact["device_id"], claim_hash, created=False
                    )
                existing = connection.execute(
                    "SELECT * FROM devices WHERE device_id=?",
                    (claim.device_id,),
                ).fetchone()
                if existing is not None:
                    raise HTTPException(
                        409, "device identity already exists with a different registration claim"
                    )
                connection.execute(
                    """INSERT INTO devices(
                       device_id,organization_id,status,profile_json,public_key,key_id,
                       enrollment_token_hash,enrollment_expires_at,enrolled_at,last_seen_at,
                       created_at,updated_at,attestation_certificate_json,certificate_id,
                       location_public_key,sensor_public_key,last_heartbeat_at)
                       VALUES(?,?,?,?,?,?,NULL,NULL,?,?,?,?,?,?,?,?,?)""",
                    (
                        claim.device_id,
                        settings.attested_organization_id,
                        "active",
                        dump_json(profile),
                        raw_runtime_key,
                        runtime_key_id,
                        timestamp,
                        timestamp,
                        timestamp,
                        timestamp,
                        dump_json(
                            claim.certificate.model_dump(
                                mode="json", by_alias=True, exclude_none=True
                            )
                        ),
                        claim.certificate.certificate_id,
                        claim.certificate.location_producer_public_key,
                        claim.certificate.sensor_producer_public_key,
                        timestamp,
                    ),
                )
                connection.execute(
                    """INSERT INTO registration_claims(
                       claim_hash,device_id,certificate_id,nonce,claim_json,
                       claimed_at,received_at) VALUES(?,?,?,?,?,?,?)""",
                    (
                        claim_hash,
                        claim.device_id,
                        claim.certificate.certificate_id,
                        claim.nonce,
                        claim_json,
                        iso(claim.claimed_at),
                        timestamp,
                    ),
                )
                persist_position(
                    connection,
                    claim.device_id,
                    claim.gnss_observation,
                    source="registration",
                )
                enqueue_settlement(
                    connection,
                    "device_key",
                    runtime_key_id,
                    {
                        "device_id": claim.device_id,
                        "publisher_organization_id": settings.attested_organization_id,
                        "signer_key_id": runtime_key_id,
                        "public_key": claim.certificate.runtime_device_public_key,
                        "valid_from": timestamp,
                        "revoked": False,
                    },
                )
                operator_audit(
                    connection,
                    "device.attested_registered",
                    "device",
                    claim.device_id,
                    {
                        "certificate_id": claim.certificate.certificate_id,
                        "manufacturer": claim.certificate.manufacturer,
                        "runtime_key_id": runtime_key_id,
                        "location_key_id": claim.gnss_observation.signer_key_id,
                        "sensor_key_bound": (
                            claim.certificate.sensor_producer_public_key is not None
                        ),
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "registration nonce, certificate, or identity replay") from exc
        deliver_settlement_outbox()
        return attested_registration_response(claim.device_id, claim_hash, created=True)

    @app.post("/api/v1/devices/{device_id}/heartbeat")
    @app.post("/api/v1/edge/{device_id}/heartbeat", include_in_schema=False)
    def attested_heartbeat(
        device_id: str,
        heartbeat: DeviceHeartbeatV1,
    ) -> dict[str, Any]:
        if heartbeat.device_id != device_id:
            raise HTTPException(422, "heartbeat device_id does not match URL")
        row = device_row(db, device_id)
        if row["status"] != "active" or not row["attestation_certificate_json"]:
            raise HTTPException(401, "device is not actively attested")
        try:
            certificate = json.loads(row["attestation_certificate_json"])
            parsed_certificate = DeviceAttestationCertificateV1.model_validate(certificate)
            valid = verify_device_heartbeat(
                heartbeat,
                parsed_certificate,
                now=now(),
                expected_nonce=heartbeat.nonce,
                max_age=timedelta(seconds=settings.registration_freshness_seconds),
            )
        except (ValueError, TypeError) as exc:
            raise HTTPException(401, "invalid heartbeat key binding") from exc
        if not valid:
            raise HTTPException(401, "invalid or stale device heartbeat")
        timestamp = iso()
        try:
            with db.transaction(immediate=True) as connection:
                connection.execute(
                    "DELETE FROM nonces WHERE timestamp < ?",
                    (
                        int(time.time())
                        - max(
                            settings.request_window_seconds,
                            settings.registration_freshness_seconds,
                        ),
                    ),
                )
                connection.execute(
                    "INSERT INTO nonces(device_id,nonce,timestamp) VALUES(?,?,?)",
                    (device_id, heartbeat.nonce, int(heartbeat.sent_at.timestamp())),
                )
                persist_cellular_telemetry(
                    connection,
                    device_id,
                    heartbeat,
                    received_at=timestamp,
                )
                persist_battery_telemetry(
                    connection,
                    device_id,
                    heartbeat,
                    received_at=timestamp,
                )
                position_accepted, alert_id = persist_position(
                    connection,
                    device_id,
                    heartbeat.gnss_observation,
                    source="heartbeat",
                )
                link_type = (
                    "cellular" if heartbeat.cellular_telemetry is not None else "unknown"
                )
                connection.execute(
                    """UPDATE devices SET last_seen_at=?,last_heartbeat_at=?,
                       last_uptime_seconds=?,last_boot_id=?,link_type=?,
                       updated_at=? WHERE device_id=?""",
                    (
                        timestamp,
                        timestamp,
                        heartbeat.software_health.uptime_seconds,
                        heartbeat.software_health.boot_id,
                        link_type,
                        timestamp,
                        device_id,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "heartbeat nonce or observation replay") from exc
        return {
            "schema": "tmads.device-heartbeat-result.v1",
            "device_id": device_id,
            "status": "accepted",
            "received_at": timestamp,
            "next_heartbeat_at": iso(
                now() + timedelta(seconds=settings.heartbeat_interval_seconds)
            ),
            "position_accepted": position_accepted,
            "movement_alert_id": alert_id,
        }

    @app.post("/api/v1/devices/enroll")
    def enroll_device(value: EnrollmentRequest) -> dict[str, str]:
        try:
            raw_public = parse_public_key(value.public_key)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        current = iso()
        with db.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM devices WHERE device_id = ?", (value.device_id,)
            ).fetchone()
            if (
                row is None
                or row["status"] != "pending"
                or not row["enrollment_token_hash"]
                or hash_token(value.enrollment_token) != row["enrollment_token_hash"]
                or row["enrollment_expires_at"] < current
            ):
                raise HTTPException(401, "invalid or expired enrollment token")
            device_key_id = key_id(raw_public)
            connection.execute(
                """UPDATE devices SET status='active', public_key=?, key_id=?,
                   enrollment_token_hash=NULL, enrollment_expires_at=NULL,
                   enrolled_at=?, updated_at=? WHERE device_id=?""",
                (raw_public, device_key_id, current, current, value.device_id),
            )
            enqueue_settlement(
                connection,
                "device_key",
                device_key_id,
                {
                    "device_id": value.device_id,
                    "publisher_organization_id": row["organization_id"],
                    "signer_key_id": device_key_id,
                    "public_key": value.public_key,
                    "valid_from": current,
                    "revoked": False,
                },
            )
            operator_audit(
                connection,
                "device.enrolled",
                "device",
                value.device_id,
                {"key_id": device_key_id},
            )
        deliver_settlement_outbox()
        return {
            "device_id": value.device_id,
            "status": "active",
            "key_id": device_key_id,
            "server_key_id": signer_key_id,
            "server_public_key": public_key,
        }

    @app.post(
        "/internal/v1/devices/{device_id}/rotate-key",
        dependencies=[Depends(internal_auth)],
    )
    def rotate_device_key(device_id: str) -> dict[str, Any]:
        row = device_row(db, device_id)
        return register_device(
            device_id,
            DeviceRegistration(
                organization_id=row["organization_id"],
                profile=json.loads(row["profile_json"]),
                reset_enrollment=True,
            ),
        )

    @app.post(
        "/internal/v1/devices/{device_id}/disable",
        dependencies=[Depends(internal_auth)],
    )
    def disable_device(device_id: str) -> dict[str, str]:
        row = device_row(db, device_id)
        timestamp = iso()
        with db.transaction(immediate=True) as connection:
            if row["key_id"] and row["public_key"]:
                enqueue_settlement(
                    connection,
                    "device_key",
                    row["key_id"],
                    {
                        "device_id": device_id,
                        "publisher_organization_id": row["organization_id"],
                        "signer_key_id": row["key_id"],
                        "public_key": "ed25519:"
                        + base64.urlsafe_b64encode(row["public_key"])
                        .rstrip(b"=")
                        .decode(),
                        "valid_from": row["enrolled_at"] or row["created_at"],
                        "valid_to": timestamp,
                        "revoked": True,
                    },
                )
            connection.execute(
                """UPDATE devices SET status='disabled',enrollment_token_hash=NULL,
                   enrollment_expires_at=NULL,updated_at=? WHERE device_id=?""",
                (timestamp, device_id),
            )
            operator_audit(
                connection,
                "device.disabled",
                "device",
                device_id,
                {"revoked_key_id": row["key_id"]},
            )
        deliver_settlement_outbox()
        return {"device_id": device_id, "status": "disabled"}

    @app.get(
        "/internal/v1/operator-audit",
        dependencies=[Depends(internal_auth)],
    )
    def operator_audit_log() -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in db.all(
                """SELECT * FROM operator_audit ORDER BY created_at DESC,id DESC
                   LIMIT 1000"""
            )
        ]

    def fleet_device_payload(row: sqlite3.Row, *, detail: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "device_id": row["device_id"],
            "organization_id": row["organization_id"],
            "status": row["status"],
            "attested": row["attestation_certificate_json"] is not None,
            "certificate_id": row["certificate_id"],
            "runtime_key_id": row["key_id"],
            "created_at": row["created_at"],
            "enrolled_at": row["enrolled_at"],
            "last_seen_at": row["last_seen_at"],
            "last_heartbeat_at": row["last_heartbeat_at"],
            "last_ack_cursor": format_cursor(row["last_ack_cursor"]),
            "uptime_seconds": row["last_uptime_seconds"],
            "boot_id": row["last_boot_id"],
            "link_type": row["link_type"],
            "current_position": (
                {
                    "latitude": row["current_latitude"],
                    "longitude": row["current_longitude"],
                    "observed_at": row["current_position_at"],
                }
                if row["current_latitude"] is not None
                else None
            ),
            **cellular_fleet_fields(row["device_id"]),
            **battery_fleet_fields(row["device_id"]),
        }
        if detail:
            payload["profile"] = json.loads(row["profile_json"])
            if row["attestation_certificate_json"]:
                certificate = json.loads(row["attestation_certificate_json"])
                payload["attestation"] = {
                    "certificate_id": certificate["certificate_id"],
                    "manufacturer": certificate["manufacturer"],
                    "runtime_device_public_key": certificate["runtime_device_public_key"],
                    "location_producer_public_key": certificate[
                        "location_producer_public_key"
                    ],
                    "sensor_producer_public_key": certificate.get(
                        "sensor_producer_public_key"
                    ),
                    "issued_at": certificate["issued_at"],
                    "expires_at": certificate["expires_at"],
                    "signer_key_id": certificate["signer_key_id"],
                }
        return payload

    @app.get(
        "/internal/v1/fleet/devices",
        dependencies=[Depends(internal_auth)],
    )
    def fleet_devices(
        organization_id: Annotated[str | None, Query(max_length=200)] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        if organization_id:
            rows = db.all(
                """SELECT * FROM devices WHERE organization_id=?
                   ORDER BY device_id LIMIT ? OFFSET ?""",
                (organization_id, limit, offset),
            )
        else:
            rows = db.all(
                "SELECT * FROM devices ORDER BY device_id LIMIT ? OFFSET ?",
                (limit, offset),
            )
        return {
            "devices": [fleet_device_payload(row) for row in rows],
            "limit": limit,
            "offset": offset,
        }

    @app.get(
        "/internal/v1/fleet/devices/{device_id}",
        dependencies=[Depends(internal_auth)],
    )
    def fleet_device_detail(device_id: str) -> dict[str, Any]:
        return fleet_device_payload(device_row(db, device_id), detail=True)

    @app.get(
        "/internal/v1/fleet/devices/{device_id}/positions",
        dependencies=[Depends(internal_auth)],
    )
    def fleet_device_positions(
        device_id: str,
        limit: Annotated[int, Query(ge=1, le=10_000)] = 1000,
        before: Annotated[datetime | None, Query()] = None,
    ) -> dict[str, Any]:
        device_row(db, device_id)
        if before is not None:
            rows = db.all(
                """SELECT observation_id,latitude,longitude,confidence,
                          observed_at,received_at,source
                   FROM device_positions WHERE device_id=? AND observed_at<?
                   ORDER BY observed_at DESC,id DESC LIMIT ?""",
                (device_id, iso(before), limit),
            )
        else:
            rows = db.all(
                """SELECT observation_id,latitude,longitude,confidence,
                          observed_at,received_at,source
                   FROM device_positions WHERE device_id=?
                   ORDER BY observed_at DESC,id DESC LIMIT ?""",
                (device_id, limit),
            )
        return {"device_id": device_id, "positions": [dict(row) for row in rows]}

    @app.get(
        "/internal/v1/fleet/devices/{device_id}/cellular-usage",
        dependencies=[Depends(internal_auth)],
    )
    def fleet_device_cellular_usage(
        device_id: str,
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    ) -> dict[str, Any]:
        device_row(db, device_id)
        rows = db.all(
            """SELECT heartbeat_nonce,heartbeat_sent_at,heartbeat_signer_key_id,
                      heartbeat_signature,operator_name,mcc,mnc,registration_state,
                      lte_interface,rssi_dbm,rx_bytes,tx_bytes,rx_delta_bytes,
                      tx_delta_bytes,counter_boot_id,interval_started_at,collected_at,
                      received_at
               FROM cellular_usage_history WHERE device_id=?
               ORDER BY received_at DESC,id DESC LIMIT ?""",
            (device_id, limit),
        )
        return {
            "device_id": device_id,
            "usage_history": [
                {
                    **dict(row),
                    "total_delta_bytes": (
                        row["rx_delta_bytes"] + row["tx_delta_bytes"]
                    ),
                }
                for row in rows
            ],
            "limit": limit,
        }

    @app.get(
        "/internal/v1/fleet/devices/{device_id}/sync-history",
        dependencies=[Depends(internal_auth)],
    )
    def fleet_device_sync_history(
        device_id: str,
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    ) -> dict[str, Any]:
        device_row(db, device_id)
        rows = db.all(
            """SELECT id,cursor,received_at,detail_json FROM sync_acks
               WHERE device_id=? ORDER BY received_at DESC,id DESC LIMIT ?""",
            (device_id, limit),
        )
        return {
            "device_id": device_id,
            "sync_history": [
                {
                    "id": row["id"],
                    "cursor": format_cursor(row["cursor"]),
                    "received_at": row["received_at"],
                    "detail": json.loads(row["detail_json"]),
                }
                for row in rows
            ],
        }

    @app.get(
        "/internal/v1/fleet/movement-alerts",
        dependencies=[Depends(internal_auth)],
    )
    def fleet_movement_alerts(
        device_id: Annotated[str | None, Query(max_length=200)] = None,
        organization_id: Annotated[str | None, Query(max_length=200)] = None,
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    ) -> dict[str, Any]:
        if device_id and organization_id:
            rows = db.all(
                """SELECT a.* FROM movement_alerts a
                   JOIN devices d ON d.device_id=a.device_id
                   WHERE a.device_id=? AND d.organization_id=?
                   ORDER BY a.confirmed_at DESC,a.id DESC LIMIT ?""",
                (device_id, organization_id, limit),
            )
        elif device_id:
            rows = db.all(
                """SELECT a.* FROM movement_alerts a
                   WHERE a.device_id=?
                   ORDER BY a.confirmed_at DESC,a.id DESC LIMIT ?""",
                (device_id, limit),
            )
        elif organization_id:
            rows = db.all(
                """SELECT a.* FROM movement_alerts a
                   JOIN devices d ON d.device_id=a.device_id
                   WHERE d.organization_id=?
                   ORDER BY a.confirmed_at DESC,a.id DESC LIMIT ?""",
                (organization_id, limit),
            )
        else:
            rows = db.all(
                """SELECT * FROM movement_alerts
                   ORDER BY confirmed_at DESC,id DESC LIMIT ?""",
                (limit,),
            )
        return {"movement_alerts": [dict(row) for row in rows]}

    @app.get(
        "/internal/v1/fleet/devices/{device_id}/movement-alerts",
        dependencies=[Depends(internal_auth)],
        include_in_schema=False,
    )
    def fleet_device_movement_alerts(
        device_id: str,
        limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    ) -> dict[str, Any]:
        device_row(db, device_id)
        rows = db.all(
            """SELECT * FROM movement_alerts WHERE device_id=?
               ORDER BY confirmed_at DESC,id DESC LIMIT ?""",
            (device_id, limit),
        )
        return {"device_id": device_id, "movement_alerts": [dict(row) for row in rows]}

    @app.post(
        "/api/v1/edge/{device_id}/receipts/batch",
        response_model=ReceiptBatchResult,
    )
    def forward_receipts(device_id: str, value: ReceiptBatch) -> ReceiptBatchResult:
        if any(item.device_id != device_id for item in value.receipts):
            raise HTTPException(400, "receipt device does not match path")
        if settlement is None or not settings.settlement_base_url:
            raise HTTPException(503, "settlement is not configured")
        try:
            response = settlement.post(
                settings.settlement_base_url.rstrip("/")
                + f"/internal/v1/edge/{device_id}/receipts/batch",
                content=value.model_dump_json(by_alias=True, exclude_none=True),
                headers={
                    "Authorization": f"Bearer {settings.settlement_internal_token}",
                    "Content-Type": "application/json",
                },
                timeout=settings.settlement_timeout_seconds,
            )
            response.raise_for_status()
            result = ReceiptBatchResult.model_validate(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(502, "settlement rejected or returned an invalid response") from exc
        expected = {
            item.receipt_id: canonical_sha256(item)
            for item in value.receipts
        }
        received = {item.receipt_id: item.receipt_hash for item in result.decisions}
        if result.batch_id != value.batch_id or received != expected:
            raise HTTPException(502, "settlement response does not match submitted receipts")
        return result

    @app.put(
        "/internal/v1/contracts/{object_hash}",
        dependencies=[Depends(internal_auth)],
    )
    async def upsert_contract(object_hash: str, request: Request) -> dict[str, Any]:
        try:
            contract = ContractV1.model_validate(await request.json())
        except Exception as exc:
            raise HTTPException(422, f"invalid contract: {exc}") from exc
        if contract.signature is None or contract.signer_key_id is None:
            raise HTTPException(422, "published contract must be signed")
        calculated = canonical_sha256(contract)
        if object_hash != calculated:
            raise HTTPException(422, "contract hash does not match canonical object")
        payload = contract.model_dump(mode="json", by_alias=True, exclude_none=True)
        encoded = dump_json(payload)
        inserted = False
        try:
            with db.transaction(immediate=True) as connection:
                existing = connection.execute(
                    "SELECT payload_json FROM contracts WHERE object_hash=?", (object_hash,)
                ).fetchone()
                if existing and existing["payload_json"] != encoded:
                    raise HTTPException(409, "immutable object hash collision")
                if not existing:
                    connection.execute(
                        """INSERT INTO contracts(
                           object_hash, contract_id, revision, payload_json, valid_from,
                           valid_to, status, created_at
                        ) VALUES(?,?,?,?,?,?,?,?)""",
                        (
                            object_hash,
                            contract.contract_id,
                            contract.revision,
                            encoded,
                            iso(contract.valid_from),
                            iso(contract.valid_to),
                            contract.status,
                            iso(),
                        ),
                    )
                    connection.execute(
                        "INSERT INTO events(kind, object_hash, created_at) VALUES('contract',?,?)",
                        (object_hash, iso()),
                    )
                    inserted = True
        except sqlite3.IntegrityError as exc:
            raise HTTPException(409, "contract ID/revision already has another object") from exc
        return {"object_hash": object_hash, "created": inserted}

    @app.put(
        "/internal/v1/media-manifests/{asset_hash}",
        dependencies=[Depends(internal_auth)],
    )
    async def upsert_media_manifest(asset_hash: str, request: Request) -> dict[str, Any]:
        try:
            manifest = MediaManifestV1.model_validate(await request.json())
        except Exception as exc:
            raise HTTPException(422, f"invalid media manifest: {exc}") from exc
        if manifest.asset_hash != asset_hash:
            raise HTTPException(422, "manifest asset_hash does not match URL")
        encoded = dump_json(
            manifest.model_dump(mode="json", by_alias=True, exclude_none=True)
        )
        inserted = False
        with db.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT payload_json FROM media_manifests WHERE asset_hash=?", (asset_hash,)
            ).fetchone()
            if existing and existing["payload_json"] != encoded:
                raise HTTPException(409, "media manifest is immutable")
            if not existing:
                connection.execute(
                    """INSERT INTO media_manifests(
                       asset_hash, media_id, payload_json, byte_length, mime, created_at
                    ) VALUES(?,?,?,?,?,?)""",
                    (
                        asset_hash,
                        manifest.media_id,
                        encoded,
                        manifest.bytes,
                        manifest.mime,
                        iso(),
                    ),
                )
                connection.execute(
                    """INSERT INTO events(kind, object_hash, created_at)
                       VALUES('media_manifest',?,?)""",
                    (asset_hash, iso()),
                )
                inserted = True
        return {"asset_hash": asset_hash, "created": inserted}

    @app.put(
        "/internal/v1/spend-grants/{grant_id}",
        dependencies=[Depends(internal_auth)],
    )
    async def upsert_spend_grant(grant_id: str, request: Request) -> dict[str, Any]:
        try:
            grant = SpendGrantV1.model_validate(await request.json())
        except Exception as exc:
            raise HTTPException(422, f"invalid spend grant: {exc}") from exc
        if grant.grant_id != grant_id:
            raise HTTPException(422, "grant_id does not match URL")
        if grant.signature is None or grant.signer_key_id is None:
            raise HTTPException(422, "spend grant must be signed")
        if db.one("SELECT 1 FROM devices WHERE device_id=?", (grant.device_id,)) is None:
            raise HTTPException(409, "target device is not registered")
        encoded = dump_json(
            grant.model_dump(mode="json", by_alias=True, exclude_none=True)
        )
        inserted = False
        with db.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT payload_json FROM spend_grants WHERE grant_id=?", (grant_id,)
            ).fetchone()
            if existing and existing["payload_json"] != encoded:
                raise HTTPException(409, "spend grant is immutable")
            if not existing:
                connection.execute(
                    """INSERT INTO spend_grants(
                       grant_id,contract_hash,device_id,payload_json,valid_from,valid_to,created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (
                        grant_id,
                        grant.contract_hash,
                        grant.device_id,
                        encoded,
                        iso(grant.valid_from),
                        iso(grant.valid_to),
                        iso(),
                    ),
                )
                connection.execute(
                    "INSERT INTO events(kind,object_hash,created_at) VALUES('spend_grant',?,?)",
                    (grant_id, iso()),
                )
                inserted = True
        return {"grant_id": grant_id, "created": inserted}

    @app.put(
        "/internal/v1/observations/{observation_id}",
        dependencies=[Depends(internal_auth)],
    )
    async def upsert_observation(
        observation_id: str,
        request: Request,
        device_id: Annotated[str | None, Query()] = None,
    ) -> dict[str, Any]:
        try:
            observation = ObservationV1.model_validate(await request.json())
        except Exception as exc:
            raise HTTPException(422, f"invalid observation: {exc}") from exc
        if observation.observation_id != observation_id:
            raise HTTPException(422, "observation_id does not match URL")
        if observation.signature is None or observation.signer_key_id is None:
            raise HTTPException(422, "observation must be producer-signed")
        if device_id and db.one("SELECT 1 FROM devices WHERE device_id=?", (device_id,)) is None:
            raise HTTPException(409, "target device is not registered")
        object_hash = canonical_sha256(observation)
        wire = observation.model_dump(mode="json", by_alias=True, exclude_none=True)
        encoded = dump_json(wire)
        inserted = False
        with db.transaction(immediate=True) as connection:
            existing = connection.execute(
                "SELECT payload_json,target_device_id FROM observations WHERE observation_id=?",
                (observation_id,),
            ).fetchone()
            if existing and (
                existing["payload_json"] != encoded
                or existing["target_device_id"] != device_id
            ):
                raise HTTPException(409, "observation ID is immutable")
            if not existing:
                connection.execute(
                    "INSERT INTO observations VALUES(?,?,?,?,?,?,?,?)",
                    (
                        observation_id,
                        object_hash,
                        observation.kind,
                        device_id,
                        encoded,
                        iso(observation.observed_at),
                        iso(observation.valid_until),
                        iso(),
                    ),
                )
                connection.execute(
                    "INSERT INTO events(kind,object_hash,created_at) VALUES('observation',?,?)",
                    (observation_id, iso()),
                )
                inserted = True
            enqueue_settlement(
                connection,
                "observation",
                observation_id,
                {"observation": wire},
            )
        delivery = deliver_settlement_outbox()
        settlement_registered = delivery["synced"] > 0 or db.one(
            """SELECT 1 FROM settlement_outbox
               WHERE object_type='observation' AND object_key=? AND status='SYNCED'""",
            (observation_id,),
        ) is not None
        return {
            "observation_id": observation_id,
            "object_hash": object_hash,
            "created": inserted,
            "settlement_registered": settlement_registered,
            "settlement_sync_status": (
                db.one(
                    """SELECT status FROM settlement_outbox
                       WHERE object_type='observation' AND object_key=?""",
                    (observation_id,),
                )["status"]
                if settlement is not None
                else "NOT_CONFIGURED"
            ),
        }

    @app.delete(
        "/internal/v1/observations/{observation_id}",
        dependencies=[Depends(internal_auth)],
    )
    def revoke_observation(observation_id: str) -> dict[str, Any]:
        with db.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT target_device_id FROM observations WHERE observation_id=?",
                (observation_id,),
            ).fetchone()
            prior = connection.execute(
                "SELECT target_device_id FROM observation_revocations WHERE observation_id=?",
                (observation_id,),
            ).fetchone()
            if row is None and prior is None:
                raise HTTPException(404, "observation not found")
            if row:
                connection.execute("DELETE FROM observations WHERE observation_id=?", (observation_id,))
                connection.execute(
                    "INSERT INTO observation_revocations VALUES(?,?,?)",
                    (observation_id, row["target_device_id"], iso()),
                )
                connection.execute(
                    """INSERT INTO events(kind,object_hash,operation,created_at)
                       VALUES('observation',?,'delete',?)""",
                    (observation_id, iso()),
                )
        return {"observation_id": observation_id, "revoked": True}

    @app.delete(
        "/internal/v1/spend-grants/{grant_id}",
        dependencies=[Depends(internal_auth)],
    )
    def revoke_spend_grant(grant_id: str) -> dict[str, Any]:
        with db.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT device_id FROM spend_grants WHERE grant_id=?", (grant_id,)
            ).fetchone()
            prior = connection.execute(
                "SELECT device_id FROM spend_grant_revocations WHERE grant_id=?",
                (grant_id,),
            ).fetchone()
            if row is None and prior is None:
                raise HTTPException(404, "spend grant not found")
            device_id = row["device_id"] if row else prior["device_id"]
            if row:
                connection.execute("DELETE FROM spend_grants WHERE grant_id=?", (grant_id,))
                connection.execute(
                    """INSERT INTO spend_grant_revocations(grant_id,device_id,revoked_at)
                       VALUES(?,?,?)""",
                    (grant_id, device_id, iso()),
                )
                connection.execute(
                    """INSERT INTO events(kind,object_hash,operation,created_at)
                       VALUES('spend_grant',?,'delete',?)""",
                    (grant_id, iso()),
                )
        return {"grant_id": grant_id, "revoked": True}

    def install_media(asset_hash: str, source: Path, remove_source: bool) -> dict[str, Any]:
        row = db.one(
            "SELECT byte_length FROM media_manifests WHERE asset_hash=?", (asset_hash,)
        )
        if row is None:
            raise HTTPException(409, "register media manifest before content")
        try:
            calculated, length = hash_file(source, settings.max_media_bytes)
        except (OSError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
        if calculated != asset_hash or length != row["byte_length"]:
            raise HTTPException(422, "media hash or length does not match manifest")
        target = media_path(settings.object_root, asset_hash)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            existing_hash, existing_length = hash_file(target, settings.max_media_bytes)
            if existing_hash != asset_hash or existing_length != length:
                raise HTTPException(409, "existing object is corrupt")
            if remove_source:
                source.unlink(missing_ok=True)
        elif remove_source:
            os.replace(source, target)
        else:
            temporary = target.with_suffix(f".{os.urandom(6).hex()}.tmp")
            try:
                with source.open("rb") as input_handle, temporary.open("xb") as output_handle:
                    while block := input_handle.read(1024 * 1024):
                        output_handle.write(block)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        relative = target.relative_to(settings.object_root).as_posix()
        db.execute(
            "UPDATE media_manifests SET object_path=?, verified_at=? WHERE asset_hash=?",
            (relative, iso(), asset_hash),
        )
        return {"asset_hash": asset_hash, "bytes": length, "stored": True}

    @app.put(
        "/internal/v1/media/{asset_hash}/content",
        dependencies=[Depends(internal_auth)],
    )
    async def upload_media(asset_hash: str, request: Request) -> dict[str, Any]:
        if not HASH_RE.fullmatch(asset_hash):
            raise HTTPException(422, "invalid asset hash")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="upload-", dir=settings.object_root
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        length = 0
        try:
            with temporary.open("wb") as handle:
                async for block in request.stream():
                    length += len(block)
                    if length > settings.max_media_bytes:
                        raise HTTPException(413, "media exceeds configured size limit")
                    handle.write(block)
            return install_media(asset_hash, temporary, True)
        finally:
            temporary.unlink(missing_ok=True)

    @app.post(
        "/internal/v1/media/{asset_hash}/register",
        dependencies=[Depends(internal_auth)],
    )
    def register_shared_media(
        asset_hash: str, value: SharedMediaRegistration
    ) -> dict[str, Any]:
        try:
            source = safe_import_path(settings.import_root, value.relative_path)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if not source.is_file():
            raise HTTPException(404, "shared media file not found")
        return install_media(asset_hash, source, False)

    @app.post("/api/v1/edge/{device_id}/hello")
    async def hello(
        device_id: str,
        value: HelloRequest,
        row: sqlite3.Row = Depends(authenticate_device),
    ) -> dict[str, Any]:
        if value.current_cursor is not None:
            try:
                parse_cursor(value.current_cursor)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
        db.execute("UPDATE devices SET last_seen_at=? WHERE device_id=?", (iso(), device_id))
        profile = json.loads(row["profile_json"])
        firmware_offer = build_firmware_offer(
            db, profile, value.software_version, device_id
        )
        return signed_object(
            app,
            {
                "schema": "tmads.edge-hello.v1",
                "device_id": device_id,
                "organization_id": row["organization_id"],
                "server_time": iso(),
                "latest_cursor": format_cursor(db.max_sequence()),
                "manifest_url": f"/api/v1/edge/{device_id}/manifest",
                "next_wake_at": next_wake_at(
                    profile, settings.heartbeat_interval_seconds, now
                ),
                "wake_interval_seconds": profile_wake_interval_seconds(
                    profile, settings.heartbeat_interval_seconds
                ),
                "firmware_offer": firmware_offer,
            },
        )

    @app.get("/api/v1/edge/{device_id}/manifest")
    async def manifest(
        device_id: str,
        row: sqlite3.Row = Depends(authenticate_device),
    ) -> dict[str, Any]:
        profile = json.loads(row["profile_json"])
        contracts = relevant_contract_rows(db, profile, settings.manifest_lookahead_seconds)
        hashes = relevant_media_hashes(contracts)
        media = [
            db.json(item)
            for item in db.all(
                "SELECT * FROM media_manifests ORDER BY asset_hash"
            )
            if item["asset_hash"] in hashes
        ]
        sequence = db.max_sequence()
        return signed_object(
            app,
            {
                "schema": "tmads.edge-manifest.v1",
                "device_id": device_id,
                "sequence": sequence,
                "cursor": format_cursor(sequence),
                "generated_at": iso(),
                "contracts": [
                    {"object_hash": item["object_hash"], "payload": contract.model_dump(
                        mode="json", by_alias=True, exclude_none=True
                    )}
                    for item, contract in contracts
                ],
                "media_manifests": media,
                "spend_grants": [
                    db.json(item)
                    for item in db.all(
                        """SELECT * FROM spend_grants
                           WHERE device_id=? AND valid_to>? ORDER BY created_at""",
                        (device_id, iso()),
                    )
                ],
                "observations": [
                    db.json(item)
                    for item in db.all(
                        """SELECT * FROM observations
                           WHERE (target_device_id IS NULL OR target_device_id=?)
                             AND valid_until>? ORDER BY observed_at""",
                        (device_id, iso()),
                    )
                ],
            },
        )

    @app.get("/api/v1/edge/{device_id}/delta")
    async def delta(
        device_id: str,
        row: sqlite3.Row = Depends(authenticate_device),
        cursor: Annotated[str | None, Query()] = None,
    ) -> dict[str, Any]:
        try:
            start = parse_cursor(cursor)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        end = db.max_sequence()
        if start > end:
            raise HTTPException(409, "cursor is ahead of server")
        profile = json.loads(row["profile_json"])
        contracts = relevant_contract_rows(db, profile, settings.manifest_lookahead_seconds)
        contract_by_hash = {item["object_hash"]: contract for item, contract in contracts}
        media_hashes = relevant_media_hashes(contracts)
        changes: list[dict[str, Any]] = []
        for event in db.all(
            "SELECT * FROM events WHERE sequence>? AND sequence<=? ORDER BY sequence",
            (start, end),
        ):
            payload: dict[str, Any] | None = None
            if event["kind"] == "contract" and event["object_hash"] in contract_by_hash:
                payload = contract_by_hash[event["object_hash"]].model_dump(
                    mode="json", by_alias=True, exclude_none=True
                )
            elif event["kind"] == "media_manifest" and event["object_hash"] in media_hashes:
                media_row = db.one(
                    "SELECT * FROM media_manifests WHERE asset_hash=?",
                    (event["object_hash"],),
                )
                payload = db.json(media_row) if media_row else None
            elif event["kind"] == "spend_grant":
                if event["operation"] == "delete":
                    revoked = db.one(
                        """SELECT * FROM spend_grant_revocations
                           WHERE grant_id=? AND device_id=?""",
                        (event["object_hash"], device_id),
                    )
                    payload = (
                        {"grant_id": revoked["grant_id"], "device_id": revoked["device_id"]}
                        if revoked
                        else None
                    )
                else:
                    grant_row = db.one(
                        "SELECT * FROM spend_grants WHERE grant_id=? AND device_id=?",
                        (event["object_hash"], device_id),
                    )
                    payload = db.json(grant_row) if grant_row else None
            elif event["kind"] == "observation":
                if event["operation"] == "delete":
                    revoked = db.one(
                        """SELECT * FROM observation_revocations
                           WHERE observation_id=? AND
                           (target_device_id IS NULL OR target_device_id=?)""",
                        (event["object_hash"], device_id),
                    )
                    payload = (
                        {"observation_id": revoked["observation_id"]}
                        if revoked
                        else None
                    )
                else:
                    observation_row = db.one(
                        """SELECT * FROM observations WHERE observation_id=? AND
                           (target_device_id IS NULL OR target_device_id=?)""",
                        (event["object_hash"], device_id),
                    )
                    payload = db.json(observation_row) if observation_row else None
            if payload is not None:
                changes.append(
                    {
                        "sequence": event["sequence"],
                        "kind": event["kind"],
                        "operation": event["operation"],
                        "object_hash": event["object_hash"],
                        "payload": payload,
                    }
                )
        return signed_object(
            app,
            {
                "schema": "tmads.edge-delta.v1",
                "device_id": device_id,
                "from_cursor": format_cursor(start),
                "cursor": format_cursor(end),
                "generated_at": iso(),
                "changes": changes,
            },
        )

    @app.get("/api/v1/edge/{device_id}/media/{asset_hash}")
    async def serve_media(
        device_id: str,
        asset_hash: str,
        _: sqlite3.Row = Depends(authenticate_device),
    ) -> Response:
        if not HASH_RE.fullmatch(asset_hash):
            raise HTTPException(404, "media not found")
        row = db.one(
            "SELECT * FROM media_manifests WHERE asset_hash=?", (asset_hash,)
        )
        if row is None or not row["object_path"]:
            raise HTTPException(404, "media not found")
        target = media_path(settings.object_root, asset_hash)
        if not target.is_file():
            raise HTTPException(404, "media not found")
        try:
            calculated, length = hash_file(target, settings.max_media_bytes)
        except (OSError, ValueError) as exc:
            raise HTTPException(409, "media object failed verification") from exc
        if calculated != asset_hash or length != row["byte_length"]:
            raise HTTPException(409, "media object failed verification")
        return FileResponse(
            target,
            media_type=row["mime"],
            filename=asset_hash.removeprefix("sha256:"),
            headers={"Digest": f"sha-256={asset_hash.removeprefix('sha256:')}"},
        )

    @app.post("/api/v1/edge/{device_id}/sync/ack")
    async def sync_ack(
        device_id: str,
        value: AckRequest,
        row: sqlite3.Row = Depends(authenticate_device),
    ) -> dict[str, Any]:
        try:
            cursor_value = parse_cursor(value.cursor)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if cursor_value > db.max_sequence():
            raise HTTPException(409, "ack cursor is ahead of server")
        if cursor_value < row["last_ack_cursor"]:
            raise HTTPException(409, "ack cursor cannot move backwards")
        received = iso()
        with db.transaction(immediate=True) as connection:
            connection.execute(
                "UPDATE devices SET last_ack_cursor=?, last_seen_at=? WHERE device_id=?",
                (cursor_value, received, device_id),
            )
            connection.execute(
                """INSERT INTO sync_acks(device_id, cursor, received_at, detail_json)
                   VALUES(?,?,?,?)""",
                (device_id, cursor_value, received, dump_json(value.model_dump())),
            )
        return signed_object(
            app,
            {
                "schema": "tmads.edge-sync-ack.v1",
                "device_id": device_id,
                "cursor": format_cursor(cursor_value),
                "received_at": received,
            },
        )

    register_firmware_routes(
        app,
        db=db,
        settings=settings,
        authenticate_device=authenticate_device,
        signed_object=signed_object,
        internal_auth=internal_auth,
        iso=iso,
        now=now,
        dump_json=dump_json,
        default_wake_interval=settings.heartbeat_interval_seconds,
    )
    register_rendered_media_routes(
        app,
        db=db,
        settings=settings,
        authenticate_device=authenticate_device,
    )

    return app
