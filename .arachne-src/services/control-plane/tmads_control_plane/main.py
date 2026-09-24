import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import sqlite3
import sys
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Self
from urllib.parse import quote

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .accounting import (
    allocate_grant_sequence,
    apply_settlement,
    balance,
    release_grant,
    reserve_grant,
)
from .config import Settings, get_settings
from .db import Database
from .funding import (
    DemoFundingProvider,
    DisabledFundingProvider,
    FundingProvider,
    StripeFundingProvider,
)
from .integrations import (
    EdgeDistributorClient,
    LedgerGatewayClient,
    OracleClient,
    SettlementClient,
    normalized_json,
    outbox_payload,
    price_preview,
    score_curve,
)
from .media import LocalMediaProcessor, MediaProcessor, sniff_media
from .plays import plays_over_time
from .security import (
    DemoSmsProvider,
    DisabledSmsProvider,
    SmsProvider,
    TwilioMessagingSmsProvider,
    TwilioVerifySmsProvider,
    digest,
    hash_otp,
    is_supply_admin_phone,
    load_or_create_signing_key,
    new_token,
    normalize_phone,
    rotate_signing_key,
)
from .sports_catalog import SPORTS_LEAGUES, SPORTS_TEAM_CODES, SPORTS_TEAMS, sports_catalog_json
from .shelf_activation import (
    create_shelf_token,
    deploy_campaign_to_device,
    device_is_awake,
    list_campaign_upcs,
    lookup_campaign_for_upc,
    normalize_upc,
    published_contract_for_campaign,
    save_compliance_photo,
    shelf_device_group,
    shelf_install_url,
    verify_shelf_token,
)
from .supply import FleetClient, RevenueClient, SupplyAggregator, apply_operator_labels
from . import supply_firmware

log = logging.getLogger("tmads")
logging.basicConfig(
    level=logging.INFO,
    format='{"time":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","message":"%(message)s"}',
)


def now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None = None) -> str:
    return (value or now()).isoformat().replace("+00:00", "Z")


def uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4()}"


class OrganizationIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=120)


class DepositIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    amount_minor: int = Field(default=10_000, ge=100, le=1_000_000)


class CampaignIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    organization_id: str
    name: str = Field(min_length=2, max_length=120)
    description: str = Field(default="", max_length=2000)
    budget_cap_minor: int = Field(default=10_000, ge=100)
    start_at: datetime
    end_at: datetime


class BidIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    total_cap_minor: int = Field(default=10_000, ge=1)
    valid_from: datetime
    valid_to: datetime
    geography: str = Field(default="geo:toronto-v1", min_length=1)
    qfv_price_min_minor: int = Field(default=5, ge=0)
    qfv_price_max_minor: int = Field(default=25, ge=0)
    max_qfvs_per_play: int = Field(default=8, ge=1, le=100)
    require_face_count: int = Field(default=1, ge=1)
    weather_weight: int = Field(default=25, ge=0, le=100)
    sports_weight: int = Field(default=80, ge=0, le=100)
    market_weight: int = Field(default=50, ge=0, le=100)
    market_symbol: str = Field(
        default="DIA",
        min_length=1,
        max_length=32,
        pattern=r"^\^?[A-Z0-9]+(?:[.-][A-Z0-9]+)*$",
    )
    sports_league: str = Field(default="NHL")
    sports_team: str = Field(default="TOR")
    market_score_curve: dict[str, Any] = Field(
        default_factory=lambda: {"type": "linear", "min": -2.0, "max": 2.0}
    )
    criteria: list[dict[str, Any]] | None = Field(default=None, max_length=100)
    media_ids: list[str] = Field(min_length=1)

    @field_validator("market_symbol", "sports_league", "sports_team", mode="before")
    @classmethod
    def normalize_scope(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def valid_sports_scope(self) -> Self:
        if self.sports_league not in SPORTS_TEAM_CODES:
            raise ValueError(
                f"sports_league must be one of {', '.join(SPORTS_LEAGUES)}"
            )
        if self.sports_team not in SPORTS_TEAM_CODES[self.sports_league]:
            raise ValueError(
                f"sports_team {self.sports_team!r} does not belong to "
                f"{self.sports_league}"
            )
        return self


class MemberInviteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    phone: str
    display_name: str = Field(min_length=1, max_length=120)
    role: str


class MemberRoleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str
    display_name: str | None = Field(default=None, min_length=1, max_length=120)


class UserProfileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(min_length=1, max_length=120)


class DeviceAliasIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str = Field(min_length=1, max_length=80)


class PreviewIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sample_scores: dict[str, float] | None = None
    sample_values: dict[str, float] | None = None


class SpendGrantIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: str = Field(min_length=1, max_length=200)
    amount_minor: int = Field(gt=0)


def create_app(
    settings: Settings | None = None,
    *,
    sms_provider: SmsProvider | None = None,
    funding_provider: FundingProvider | None = None,
    media_processor: MediaProcessor | None = None,
    edge_distributor_client: EdgeDistributorClient | None = None,
    settlement_client: SettlementClient | None = None,
    ledger_gateway_client: LedgerGatewayClient | None = None,
    supply_fleet_client: FleetClient | None = None,
    supply_revenue_client: RevenueClient | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    demo_environments = {"development", "test", "local", "demo"}
    if settings.demo_otp_enabled and settings.environment not in demo_environments:
        raise RuntimeError("Demo OTP is forbidden outside an explicit demo environment")
    if settings.prototype_otp_bypass_enabled:
        log.warning(
            "PROTOTYPE OTP BYPASS IS ENABLED; disable TMADS_PROTOTYPE_OTP_BYPASS_ENABLED "
            "before production use"
        )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    settings.quarantine_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.db_path)
    if sms_provider is not None:
        sms = sms_provider
    elif settings.demo_otp_enabled:
        sms = DemoSmsProvider(True)
    elif settings.sms_provider == "twilio":
        if not (settings.twilio_account_sid and settings.twilio_auth_token):
            raise RuntimeError("Twilio is selected but its credentials are incomplete")
        if settings.twilio_phone_number:
            sms = TwilioMessagingSmsProvider(
                settings.twilio_account_sid,
                settings.twilio_auth_token,
                settings.twilio_phone_number,
            )
        elif settings.twilio_verify_service_sid:
            sms = TwilioVerifySmsProvider(
                settings.twilio_account_sid,
                settings.twilio_auth_token,
                settings.twilio_verify_service_sid,
            )
        else:
            raise RuntimeError(
                "Twilio requires either a Verify Service SID or a sender phone number"
            )
    else:
        sms = DisabledSmsProvider()
    if funding_provider is not None:
        funding = funding_provider
    elif settings.funding_provider == "demo":
        if settings.environment not in {"development", "test", "local"}:
            raise RuntimeError("Demo funding is restricted to local development and tests")
        funding = DemoFundingProvider()
    elif settings.funding_provider == "stripe":
        if not (
            settings.stripe_secret_key
            and settings.stripe_success_url
            and settings.stripe_cancel_url
        ):
            raise RuntimeError("Stripe funding is selected but its configuration is incomplete")
        if not settings.stripe_secret_key.startswith("sk_test_"):
            raise RuntimeError("Stripe funding requires a test-mode secret key")
        funding = StripeFundingProvider(
            settings.stripe_secret_key,
            settings.stripe_webhook_secret or "",
            settings.stripe_success_url,
            settings.stripe_cancel_url,
        )
    elif settings.funding_provider == "disabled":
        funding = DisabledFundingProvider()
    else:
        raise RuntimeError(f"Unknown funding provider: {settings.funding_provider}")
    processor = media_processor or LocalMediaProcessor(
        settings.media_dir, settings.ffmpeg_timeout_seconds
    )
    if bool(settings.edge_distributor_base_url) != bool(
        settings.edge_distributor_internal_bearer
    ):
        raise RuntimeError("Edge distributor URL and bearer token must be configured together")
    edge = edge_distributor_client or (
        EdgeDistributorClient(
            settings.edge_distributor_base_url,
            settings.edge_distributor_internal_bearer,
            settings.edge_distributor_timeout_seconds,
        )
        if settings.edge_distributor_base_url and settings.edge_distributor_internal_bearer
        else None
    )
    if bool(settings.oracle_base_url) != bool(settings.oracle_internal_token):
        raise RuntimeError("Oracle URL and internal token must be configured together")
    oracle = (
        OracleClient(
            settings.oracle_base_url,
            settings.oracle_internal_token,
            settings.oracle_timeout_seconds,
        )
        if settings.oracle_base_url and settings.oracle_internal_token
        else None
    )
    if (
        settlement_client is None
        and bool(settings.settlement_base_url) != bool(settings.settlement_internal_api_key)
    ):
        raise RuntimeError("Settlement URL and internal API key must be configured together")
    settlement = settlement_client or (
        SettlementClient(
            settings.settlement_base_url,
            settings.settlement_internal_api_key,
            settings.settlement_timeout_seconds,
        )
        if settings.settlement_base_url and settings.settlement_internal_api_key
        else None
    )
    supply = SupplyAggregator(
        supply_fleet_client
        or (
            FleetClient(
                settings.edge_distributor_base_url,
                settings.edge_distributor_internal_bearer,
                settings.edge_distributor_timeout_seconds,
            )
            if settings.edge_distributor_base_url
            and settings.edge_distributor_internal_bearer
            else None
        ),
        supply_revenue_client
        or (
            RevenueClient(
                settings.settlement_base_url,
                settings.settlement_internal_api_key,
                settings.settlement_timeout_seconds,
            )
            if settings.settlement_base_url and settings.settlement_internal_api_key
            else None
        ),
    )
    if (
        ledger_gateway_client is None
        and bool(settings.ledger_gateway_base_url) != bool(settings.ledger_gateway_token)
    ):
        raise RuntimeError("Ledger gateway URL and bearer token must be configured together")
    ledger = ledger_gateway_client or (
        LedgerGatewayClient(
            settings.ledger_gateway_base_url,
            settings.ledger_gateway_token,
            settings.ledger_gateway_timeout_seconds,
        )
        if settings.ledger_gateway_base_url and settings.ledger_gateway_token
        else None
    )
    root = Path(__file__).resolve().parent.parent
    templates = Jinja2Templates(directory=root / "templates")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if settings.environment == "production" and ledger is None:
            raise RuntimeError("Production publication requires the ledger gateway")
        if settings.environment == "production" and settlement is None:
            raise RuntimeError("Production publication requires settlement")
        db.initialize()
        stop = threading.Event()

        def worker() -> None:
            while not stop.wait(settings.outbox_worker_interval_seconds):
                try:
                    process_publication_outbox()
                    process_lifecycle_outbox()
                    reconcile_contract_lifecycle()
                    process_grant_registrations()
                    process_outbox()
                    process_sms_outbox()
                except Exception:
                    log.exception("publication_outbox_worker_failed")

        thread = threading.Thread(target=worker, name="tmads-publication-outbox", daemon=True)
        background_work = edge or ledger or settlement or not isinstance(sms, DisabledSmsProvider)
        if background_work:
            thread.start()
        try:
            yield
        finally:
            stop.set()
            if background_work:
                thread.join(timeout=2)

    app = FastAPI(title="TMAds Control Plane", version="0.1.0", lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    app.state.sms_provider = sms
    app.state.funding_provider = funding
    app.state.media_processor = processor
    app.state.edge_distributor = edge
    app.state.ledger_gateway = ledger
    app.state.settlement = settlement
    app.state.supply = supply
    app.state.oracle = oracle
    app.mount("/static", StaticFiles(directory=root / "static"), name="static")
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[item.strip() for item in settings.allowed_hosts.split(",")],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        camera_policy = "camera=(self)" if request.url.path.startswith("/shelf/") else "camera=()"
        response.headers["Permissions-Policy"] = f"{camera_policy}, microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data: https://tile.openstreetmap.org https://*.basemaps.cartocdn.com; "
            "connect-src 'self' https://tile.openstreetmap.org https://*.basemaps.cartocdn.com; style-src 'self'; "
            "script-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
        )
        if settings.cookie_secure:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    def connection() -> sqlite3.Connection:
        conn = db.connect()
        try:
            yield conn
        finally:
            conn.close()

    Db = Annotated[sqlite3.Connection, Depends(connection)]

    def current_session(request: Request, conn: Db) -> sqlite3.Row:
        token = request.cookies.get(settings.cookie_name)
        if not token:
            raise HTTPException(401, "Authentication required")
        row = conn.execute(
            """SELECT s.*,u.phone_e164,u.display_name,u.status,u.last_login_at
               FROM auth_sessions s
               JOIN users u ON u.id=s.user_id
               WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.expires_at>?""",
            (digest(token), iso()),
        ).fetchone()
        if not row:
            raise HTTPException(401, "Session expired")
        if row["status"] != "ACTIVE":
            raise HTTPException(403, "Account is not active")
        return row

    Session = Annotated[sqlite3.Row, Depends(current_session)]

    def supply_session(request: Request, conn: Db) -> sqlite3.Row:
        token = request.cookies.get(settings.cookie_name)
        configured = settings.supply_admin_phone
        if not token or not configured:
            raise HTTPException(404, "Not found")
        row = conn.execute(
            """SELECT s.*,u.phone_e164,u.display_name,u.status,u.last_login_at
               FROM auth_sessions s
               JOIN users u ON u.id=s.user_id
               WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.expires_at>?""",
            (digest(token), iso()),
        ).fetchone()
        if (
            not row
            or row["status"] != "ACTIVE"
            or not is_supply_admin_phone(row["phone_e164"], configured)
        ):
            raise HTTPException(404, "Not found")
        return row

    SupplySession = Annotated[sqlite3.Row, Depends(supply_session)]

    async def require_csrf(
        request: Request,
        session: Session,
        x_csrf_token: Annotated[str | None, Header()] = None,
    ) -> None:
        supplied = x_csrf_token
        if not supplied and request.headers.get("content-type", "").startswith(
            "application/x-www-form-urlencoded"
        ):
            supplied = str((await request.form()).get("csrf_token", ""))
        if not supplied or not secrets.compare_digest(supplied, session["csrf_token"]):
            raise HTTPException(403, "CSRF validation failed")

    permissions = {
        "manage_organization": {"owner", "admin"},
        "manage_members": {"owner", "admin"},
        "deposit_funds": {"owner", "admin", "billing_manager"},
        "view_wallet": {
            "owner",
            "admin",
            "campaign_manager",
            "billing_manager",
            "analyst",
            "viewer",
        },
        "manage_campaigns": {"owner", "admin", "campaign_manager"},
        "publish_bids": {"owner", "admin", "campaign_manager"},
        "view_reports": {
            "owner",
            "admin",
            "campaign_manager",
            "billing_manager",
            "analyst",
            "viewer",
        },
    }
    roles = set().union(*permissions.values())

    def membership(
        conn: sqlite3.Connection,
        user_id: str,
        organization_id: str,
        action: str | None = None,
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM organization_memberships WHERE organization_id=? AND user_id=?",
            (organization_id, user_id),
        ).fetchone()
        if not row or (action and row["role"] not in permissions[action]):
            raise HTTPException(404, "Organization not found")
        return row

    def campaign_for_user(
        conn: sqlite3.Connection,
        user_id: str,
        campaign_id: str,
        action: str | None = None,
    ) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Campaign not found")
        membership(conn, user_id, row["organization_id"], action)
        return row

    def bid_for_user(
        conn: sqlite3.Connection, user_id: str, bid_id: str, action: str | None = None
    ) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Bid not found")
        membership(conn, user_id, row["organization_id"], action)
        return row

    def client_ip(request: Request) -> str:
        peer = request.client.host if request.client else None
        if not peer:
            raise HTTPException(503, "Unable to establish client network identity")
        if peer == "testclient" and settings.environment == "test":
            peer = "127.0.0.1"
        try:
            peer_ip = ipaddress.ip_address(peer)
            trusted = any(
                peer_ip in ipaddress.ip_network(item.strip(), strict=False)
                for item in settings.trusted_proxy_cidrs.split(",")
                if item.strip()
            )
        except ValueError as exc:
            raise HTTPException(503, "Unable to establish client network identity") from exc
        forwarded = request.headers.get("x-forwarded-for")
        if not forwarded or not trusted:
            return peer_ip.compressed
        try:
            chain = [ipaddress.ip_address(item.strip()) for item in forwarded.split(",")]
        except ValueError as exc:
            raise HTTPException(400, "Invalid forwarded client address") from exc
        return chain[0].compressed

    def ip_key(value: str) -> str:
        key_material = settings.ip_hash_secret
        if not key_material:
            if settings.environment not in {"development", "test", "local"}:
                raise HTTPException(503, "IP throttling secret is not configured")
            key_material = digest(f"local-only:{settings.db_path.resolve()}")
        return hmac.new(key_material.encode(), value.encode(), hashlib.sha256).hexdigest()

    def throttle_ip(conn: sqlite3.Connection, request: Request, action: str, cap: int) -> None:
        key = ip_key(client_ip(request))
        window = now().replace(minute=0, second=0, microsecond=0)
        with conn:
            conn.execute(
                """INSERT INTO otp_ip_windows VALUES(?,?,?,1)
                   ON CONFLICT(ip_key_hash,action,window_started_at)
                   DO UPDATE SET request_count=request_count+1""",
                (key, action, iso(window)),
            )
            count = conn.execute(
                """SELECT request_count FROM otp_ip_windows
                   WHERE ip_key_hash=? AND action=? AND window_started_at=?""",
                (key, action, iso(window)),
            ).fetchone()[0]
        if count > cap:
            raise HTTPException(429, "Too many authentication requests from this network")

    def enqueue(
        conn: sqlite3.Connection,
        organization_id: str,
        object_type: str,
        object_key: str,
        payload_json: str | None = None,
        content_path: str | None = None,
    ) -> None:
        conn.execute(
            """INSERT OR IGNORE INTO distributor_outbox
               VALUES(?,?,?,?,?,?,'PENDING',0,NULL,?,?,NULL)""",
            (
                uid("out"),
                organization_id,
                object_type,
                object_key,
                payload_json,
                content_path,
                iso(),
                iso(),
            ),
        )

    def persist_anchor(
        conn: sqlite3.Connection,
        revision_id: str,
        object_hash: str,
        result: dict[str, Any],
        *,
        anchor_kind: str,
        lifecycle_event_id: str | None = None,
    ) -> dict[str, Any]:
        existing = conn.execute(
            """SELECT * FROM ledger_anchors
               WHERE object_hash=? AND anchor_kind=?""",
            (object_hash, anchor_kind),
        ).fetchone()
        if existing:
            return dict(existing)
        anchor_id = uid("anchor")
        anchored_at = iso()
        conn.execute(
            """INSERT INTO ledger_anchors(
               id,revision_id,adapter,network,transaction_ref,object_hash,prototype,
               anchored_at,chain_id,block_number,contract_address,finality,
               anchor_kind,lifecycle_event_id)
               VALUES(?,?,?,?,?,?,0,?,?,?,?,?,?,?)""",
            (
                anchor_id,
                revision_id,
                "besu",
                settings.ledger_network,
                result["transaction_hash"],
                object_hash,
                anchored_at,
                result["chain_id"],
                result["block_number"],
                result["contract_address"],
                result["finality"],
                anchor_kind,
                lifecycle_event_id,
            ),
        )
        return dict(
            conn.execute("SELECT * FROM ledger_anchors WHERE id=?", (anchor_id,)).fetchone()
        )

    def process_outbox(only_id: str | None = None, limit: int = 25) -> dict[str, int]:
        if edge is None:
            return {"synced": 0, "failed": 0}
        synced = failed = 0
        for _ in range(limit):
            with db.transaction() as tx:
                row = tx.execute(
                    """SELECT * FROM distributor_outbox
                       WHERE status IN ('PENDING','FAILED') AND next_attempt_at<=?
                       AND (? IS NULL OR id=?)
                       ORDER BY CASE object_type
                         WHEN 'media_manifest' THEN 1 WHEN 'media_content' THEN 2 ELSE 0 END,
                         created_at LIMIT 1""",
                    (iso(), only_id, only_id),
                ).fetchone()
                if not row:
                    break
                tx.execute(
                    "UPDATE distributor_outbox SET status='PROCESSING',attempts=attempts+1 WHERE id=?",
                    (row["id"],),
                )
            try:
                result = edge.deliver(*outbox_payload(row))
            except RuntimeError as exc:
                failed += 1
                delay = min(3600, 2 ** min(row["attempts"] + 1, 10))
                with db.transaction() as tx:
                    tx.execute(
                        """UPDATE distributor_outbox SET status='FAILED',last_error=?,
                           next_attempt_at=? WHERE id=?""",
                        (
                            str(exc)[:500],
                            iso(now() + timedelta(seconds=delay)),
                            row["id"],
                        ),
                    )
                if only_id:
                    break
                continue
            with db.transaction() as tx:
                tx.execute(
                    """UPDATE distributor_outbox SET status='SYNCED',last_error=NULL,synced_at=?
                       WHERE id=?""",
                    (iso(), row["id"]),
                )
                if row["object_type"] == "device":
                    tx.execute(
                        """UPDATE devices SET distributor_status='SYNCED',
                           enrollment_token=COALESCE(?,enrollment_token),
                           updated_at=? WHERE id=?""",
                        (result.get("enrollment_token"), iso(), row["object_key"]),
                    )
            synced += 1
            if only_id:
                break
        return {"synced": synced, "failed": failed}

    def enqueue_sms(
        conn: sqlite3.Connection,
        *,
        kind: str,
        phone_e164: str,
        body: str,
        related_id: str | None = None,
    ) -> str:
        outbox_id = uid("sms")
        conn.execute(
            """INSERT INTO sms_outbox(
               id,kind,phone_e164,body,related_id,status,attempts,next_attempt_at,
               provider_id,last_error,created_at,sent_at)
               VALUES(?,?,?,?,?,'PENDING',0,?,NULL,NULL,?,NULL)""",
            (outbox_id, kind, phone_e164, body, related_id, iso(), iso()),
        )
        return outbox_id

    def process_sms_outbox(only_id: str | None = None, limit: int = 25) -> dict[str, int]:
        sent = failed = quarantined = 0
        for _ in range(limit):
            with db.connect() as connection:
                row = connection.execute(
                    """SELECT * FROM sms_outbox
                       WHERE status IN ('PENDING','FAILED') AND next_attempt_at<=?
                       AND (? IS NULL OR id=?) ORDER BY created_at LIMIT 1""",
                    (iso(), only_id, only_id),
                ).fetchone()
            if row is None:
                break
            attempts = row["attempts"] + 1
            try:
                provider_id = sms.send_message(row["phone_e164"], row["body"])
            except RuntimeError as exc:
                terminal = attempts >= settings.sms_delivery_max_attempts
                with db.transaction() as tx:
                    tx.execute(
                        """UPDATE sms_outbox SET status=?,attempts=?,next_attempt_at=?,
                           last_error=? WHERE id=?""",
                        (
                            "QUARANTINED" if terminal else "FAILED",
                            attempts,
                            iso(now() + timedelta(seconds=min(3600, 2 ** min(attempts, 12)))),
                            str(exc)[:500],
                            row["id"],
                        ),
                    )
                    if row["kind"] == "INVITATION" and row["related_id"]:
                        tx.execute(
                            """UPDATE organization_invitations SET delivery_status='FAILED',
                               delivery_attempts=?,delivery_error=? WHERE id=?""",
                            (attempts, str(exc)[:500], row["related_id"]),
                        )
                failed += int(not terminal)
                quarantined += int(terminal)
            else:
                with db.transaction() as tx:
                    tx.execute(
                        """UPDATE sms_outbox SET status='SENT',attempts=?,provider_id=?,
                           last_error=NULL,sent_at=? WHERE id=?""",
                        (attempts, provider_id, iso(), row["id"]),
                    )
                    if row["kind"] == "INVITATION" and row["related_id"]:
                        tx.execute(
                            """UPDATE organization_invitations SET delivery_status='SENT',
                               delivery_attempts=?,delivery_provider_id=?,
                               delivery_error=NULL,delivered_at=? WHERE id=?""",
                            (attempts, provider_id, iso(), row["related_id"]),
                        )
                sent += 1
            if only_id:
                break
        return {"sent": sent, "failed": failed, "quarantined": quarantined}

    def process_grant_registrations(limit: int = 25) -> dict[str, int]:
        if settlement is None:
            return {"registered": 0, "failed": 0}
        registered = failed = 0
        with db.connect() as connection:
            rows = connection.execute(
                """SELECT * FROM grant_commitments
                   WHERE status IN ('RESERVED','FAILED')
                   AND settlement_next_attempt_at<=?
                   ORDER BY created_at LIMIT ?""",
                (iso(), limit),
            ).fetchall()
        for row in rows:
            try:
                settlement.register_grant(row["payload_json"])
            except RuntimeError as exc:
                attempts = row["settlement_attempts"] + 1
                with db.transaction() as tx:
                    tx.execute(
                        """UPDATE grant_commitments SET status='FAILED',
                           settlement_attempts=?,settlement_next_attempt_at=?,
                           settlement_last_error=?,updated_at=? WHERE grant_id=?""",
                        (
                            attempts,
                            iso(now() + timedelta(seconds=min(3600, 2 ** min(attempts, 12)))),
                            str(exc)[:1000],
                            iso(),
                            row["grant_id"],
                        ),
                    )
                failed += 1
                continue
            with db.transaction() as tx:
                tx.execute(
                    """UPDATE grant_commitments SET status='REGISTERED',
                       settlement_attempts=settlement_attempts+1,
                       settlement_last_error=NULL,updated_at=? WHERE grant_id=?""",
                    (iso(), row["grant_id"]),
                )
                enqueue(
                    tx,
                    row["organization_id"],
                    "spend_grant",
                    row["grant_id"],
                    row["payload_json"],
                )
            registered += 1
        return {"registered": registered, "failed": failed}

    def audit(
        conn: sqlite3.Connection,
        user_id: str | None,
        org_id: str | None,
        action: str,
        object_type: str,
        object_id: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO audit_events VALUES(?,?,?,?,?,?,?,?)",
            (uid("audit"), org_id, user_id, action, object_type, object_id, json.dumps(detail or {}), iso()),
        )

    def json_row(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    @app.get("/healthz")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    def ready(conn: Db) -> dict[str, Any]:
        conn.execute("SELECT 1").fetchone()
        return {
            "status": "ready",
            "prototype_otp_bypass_enabled": settings.prototype_otp_bypass_enabled,
            "operations": {
                "sms_retryable": conn.execute(
                    "SELECT COUNT(*) FROM sms_outbox WHERE status IN ('PENDING','FAILED')"
                ).fetchone()[0],
                "sms_quarantined": conn.execute(
                    "SELECT COUNT(*) FROM sms_outbox WHERE status='QUARANTINED'"
                ).fetchone()[0],
                "media_quarantined": conn.execute(
                    "SELECT COUNT(*) FROM media_assets WHERE processing_status='QUARANTINED'"
                ).fetchone()[0],
                "media_rejected": conn.execute(
                    "SELECT COUNT(*) FROM media_assets WHERE processing_status='REJECTED'"
                ).fetchone()[0],
                "distribution_failed": conn.execute(
                    "SELECT COUNT(*) FROM distributor_outbox WHERE status='FAILED'"
                ).fetchone()[0],
            },
        }

    @app.get("/api/v1/capabilities")
    def capabilities() -> dict[str, Any]:
        return {
            "sms_provider": settings.sms_provider,
            "prototype_otp_bypass_enabled": settings.prototype_otp_bypass_enabled,
            "invitation_delivery": bool(
                settings.invitation_base_url
                and (
                    settings.environment in demo_environments
                    or (
                        settings.sms_provider == "twilio"
                        and settings.twilio_phone_number
                    )
                )
            ),
            "media_validation": {
                "signature_sniffing": True,
                "ffmpeg_required_for_video": True,
                "malware_scanner": "unavailable",
                "unscanned_upload_disposition": "quarantine_then_decode_and_transcode",
            },
            "future_integrations": {
                "multi_ledger_bridge": "unavailable",
            },
            "sports_leagues": list(SPORTS_LEAGUES),
        }

    def require_operator(
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        expected = (
            f"Bearer {settings.operator_api_token}"
            if settings.operator_api_token
            else None
        )
        if expected is None:
            raise HTTPException(503, "Operator API is not configured")
        if not hmac.compare_digest(authorization or "", expected):
            raise HTTPException(401, "Invalid operator credentials")

    @app.post(
        "/internal/v1/signing-keys/rotate",
        dependencies=[Depends(require_operator)],
    )
    def rotate_contract_signing_key(conn: Db) -> dict[str, str]:
        old_id, old_public, _private, new_id, new_public = rotate_signing_key(
            settings.secrets_dir
        )
        timestamp = iso()
        with conn:
            conn.execute(
                """INSERT INTO signing_keys(
                   key_id,algorithm,public_key,status,created_at,retired_at)
                   VALUES(?, 'Ed25519', ?, 'RETIRED', ?, ?)
                   ON CONFLICT(key_id) DO UPDATE SET status='RETIRED',retired_at=?""",
                (old_id, old_public, timestamp, timestamp, timestamp),
            )
            conn.execute(
                """INSERT INTO signing_keys(
                   key_id,algorithm,public_key,status,created_at)
                   VALUES(?, 'Ed25519', ?, 'ACTIVE', ?)""",
                (new_id, new_public, timestamp),
            )
            audit(
                conn,
                None,
                None,
                "signing_key.rotated",
                "signing_key",
                new_id,
                {"predecessor_key_id": old_id, "storage": "software_file"},
            )
        return {
            "algorithm": "Ed25519",
            "key_id": new_id,
            "public_key": new_public,
            "predecessor_key_id": old_id,
            "storage": "software_file",
        }

    @app.post(
        "/internal/v1/signing-keys/{key_id}/revoke",
        dependencies=[Depends(require_operator)],
    )
    def revoke_contract_signing_key(
        key_id: str, payload: dict[str, str], conn: Db
    ) -> dict[str, str]:
        row = conn.execute(
            "SELECT * FROM signing_keys WHERE key_id=?", (key_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Signing key not found")
        if row["status"] == "ACTIVE":
            raise HTTPException(409, "Rotate the active key before revoking it")
        reason = payload.get("reason", "").strip()
        if not reason:
            raise HTTPException(422, "A revocation reason is required")
        conn.execute(
            """UPDATE signing_keys SET status='REVOKED',revoked_at=?,reason=?
               WHERE key_id=?""",
            (iso(), reason[:500], key_id),
        )
        audit(
            conn,
            None,
            None,
            "signing_key.revoked",
            "signing_key",
            key_id,
            {"reason": reason[:500]},
        )
        return {"key_id": key_id, "status": "REVOKED"}

    @app.get(
        "/internal/v1/signing-keys",
        dependencies=[Depends(require_operator)],
    )
    def list_signing_keys(conn: Db) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM signing_keys ORDER BY created_at DESC"
            ).fetchall()
        ]

    @app.post("/api/v1/auth/recovery/start")
    @app.post("/api/v1/auth/sms/start")
    def sms_start(payload: dict[str, str], request: Request, conn: Db) -> dict[str, Any]:
        recovery = request.url.path.endswith("/recovery/start")
        throttle_ip(
            conn,
            request,
            "recovery_start" if recovery else "start",
            settings.otp_ip_start_hourly_cap,
        )
        try:
            phone = normalize_phone(payload.get("phone", ""))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        cutoff = iso(now() - timedelta(hours=1))
        count = conn.execute(
            "SELECT COUNT(*) FROM otp_challenges WHERE phone_e164=? AND sent_at>?",
            (phone, cutoff),
        ).fetchone()[0]
        if count >= settings.otp_hourly_cap:
            raise HTTPException(429, "Too many codes requested")
        latest = conn.execute(
            "SELECT sent_at FROM otp_challenges WHERE phone_e164=? ORDER BY sent_at DESC LIMIT 1",
            (phone,),
        ).fetchone()
        if latest and datetime.fromisoformat(latest[0].replace("Z", "+00:00")) > now() - timedelta(
            seconds=settings.otp_resend_seconds
        ):
            raise HTTPException(429, "Please wait before requesting another code")
        challenge_id = uid("otp")
        code = settings.demo_otp_code if settings.demo_otp_enabled else f"{secrets.randbelow(1_000_000):06d}"
        conn.execute(
            "INSERT INTO otp_challenges VALUES(?,?,?,?,?,?,NULL)",
            (
                challenge_id,
                phone,
                hash_otp(challenge_id, code),
                0,
                iso(),
                iso(now() + timedelta(seconds=settings.otp_ttl_seconds)),
            ),
        )
        try:
            sms.start(phone, code)
        except RuntimeError as exc:
            audit(
                conn,
                None,
                None,
                "auth.recovery_delivery_failed" if recovery else "auth.otp_delivery_failed",
                "phone",
                digest(phone),
                {"network_key_hash": ip_key(client_ip(request)), "error": str(exc)[:200]},
            )
            raise HTTPException(503, str(exc)) from exc
        audit(
            conn,
            None,
            None,
            "auth.recovery_started" if recovery else "auth.otp_started",
            "phone",
            digest(phone),
            {"network_key_hash": ip_key(client_ip(request))},
        )
        response: dict[str, Any] = {
            "challenge_id": challenge_id,
            "expires_in": settings.otp_ttl_seconds,
            "recovery": recovery,
        }
        if settings.demo_otp_enabled:
            response["demo_code"] = code
        return response

    @app.post("/api/v1/auth/recovery/verify")
    @app.post("/api/v1/auth/sms/verify")
    def sms_verify(
        payload: dict[str, str], request: Request, response: Response, conn: Db
    ) -> dict[str, str]:
        recovery = request.url.path.endswith("/recovery/verify")
        network_hash = ip_key(client_ip(request))
        throttle_ip(
            conn,
            request,
            "recovery_verify" if recovery else "verify",
            settings.otp_ip_verify_hourly_cap,
        )
        challenge = conn.execute(
            "SELECT * FROM otp_challenges WHERE id=?", (payload.get("challenge_id", ""),)
        ).fetchone()
        if (
            not challenge
            or challenge["consumed_at"]
            or challenge["expires_at"] <= iso()
            or challenge["attempts"] >= settings.otp_attempt_cap
        ):
            audit(
                conn,
                None,
                None,
                "auth.recovery_failed" if recovery else "auth.login_failed",
                "otp_challenge",
                payload.get("challenge_id", "unknown"),
                {"reason": "invalid_or_expired", "network_key_hash": network_hash},
            )
            raise HTTPException(400, "Invalid or expired code")
        conn.execute("UPDATE otp_challenges SET attempts=attempts+1 WHERE id=?", (challenge["id"],))
        supplied_code = payload.get("code", "")
        prototype_bypass = settings.prototype_otp_bypass_enabled and hmac.compare_digest(
            supplied_code,
            settings.prototype_otp_bypass_code,
        )
        if prototype_bypass:
            approved = True
        else:
            try:
                approved = sms.verify(
                    challenge["phone_e164"],
                    supplied_code,
                    challenge["code_hash"],
                    challenge["id"],
                )
            except RuntimeError as exc:
                raise HTTPException(503, str(exc)) from exc
        if not approved:
            audit(
                conn,
                None,
                None,
                "auth.recovery_failed" if recovery else "auth.login_failed",
                "otp_challenge",
                challenge["id"],
                {"reason": "incorrect_code", "network_key_hash": network_hash},
            )
            raise HTTPException(400, "Invalid or expired code")
        user = conn.execute(
            "SELECT * FROM users WHERE phone_e164=?", (challenge["phone_e164"],)
        ).fetchone()
        user_id = user["id"] if user else uid("usr")
        if not user:
            conn.execute(
                """INSERT INTO users(id,phone_e164,display_name,created_at)
                   VALUES(?,?,NULL,?)""",
                (user_id, challenge["phone_e164"], iso()),
            )
        elif user["status"] != "ACTIVE":
            audit(
                conn,
                user_id,
                None,
                "auth.login_blocked",
                "user",
                user_id,
                {"status": user["status"], "network_key_hash": network_hash},
            )
            raise HTTPException(403, "Account is not active")
        known_network = conn.execute(
            "SELECT 1 FROM auth_sessions WHERE user_id=? AND network_key_hash=?",
            (user_id, network_hash),
        ).fetchone()
        conn.execute("UPDATE otp_challenges SET consumed_at=? WHERE id=?", (iso(), challenge["id"]))
        if recovery:
            conn.execute(
                "UPDATE auth_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                (iso(), user_id),
            )
        token, csrf = new_token(), new_token()
        conn.execute(
            """INSERT INTO auth_sessions(
               id,user_id,token_hash,csrf_token,network_key_hash,created_at,expires_at,revoked_at)
               VALUES(?,?,?,?,?,?,?,NULL)""",
            (
                uid("ses"),
                user_id,
                digest(token),
                csrf,
                network_hash,
                iso(),
                iso(now() + timedelta(hours=settings.session_hours)),
            ),
        )
        conn.execute("UPDATE users SET last_login_at=? WHERE id=?", (iso(), user_id))
        if user and not known_network:
            notice_id = uid("notice")
            conn.execute(
                """INSERT INTO security_notifications
                   VALUES(?,?,?,?,?,?,NULL)""",
                (
                    notice_id,
                    user_id,
                    "NEW_NETWORK_LOGIN",
                    "warning",
                    json.dumps(
                        {
                            "message": "A login succeeded from a network not previously used by this account.",
                            "network_key_suffix": network_hash[-8:],
                        },
                        separators=(",", ":"),
                    ),
                    iso(),
                ),
            )
            enqueue_sms(
                conn,
                kind="SECURITY",
                phone_e164=challenge["phone_e164"],
                body=(
                    "TMAds security alert: your account signed in from a new network. "
                    "If this was not you, use account recovery immediately."
                ),
                related_id=notice_id,
            )
        audit(
            conn,
            user_id,
            None,
            "auth.recovery_succeeded" if recovery else "auth.login_succeeded",
            "user",
            user_id,
            {
                "new_network": bool(user and not known_network),
                "network_key_hash": network_hash,
                "prototype_otp_bypass": prototype_bypass,
            },
        )
        if user and not known_network:
            process_sms_outbox()
        response.set_cookie(
            settings.cookie_name,
            token,
            max_age=settings.session_hours * 3600,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="lax",
            path="/",
        )
        return {
            "status": "recovered" if recovery else "authenticated",
            "csrf_token": csrf,
        }

    @app.post("/api/v1/auth/logout", dependencies=[Depends(require_csrf)])
    def logout(request: Request, response: Response, conn: Db) -> dict[str, str]:
        token = request.cookies.get(settings.cookie_name)
        if token:
            conn.execute("UPDATE auth_sessions SET revoked_at=? WHERE token_hash=?", (iso(), digest(token)))
        response.delete_cookie(settings.cookie_name, path="/")
        return {"status": "logged_out"}

    @app.get("/api/v1/me")
    def me(session: Session, conn: Db) -> dict[str, Any]:
        orgs = conn.execute(
            """SELECT o.id,o.name,m.role FROM organizations o JOIN organization_memberships m
               ON m.organization_id=o.id WHERE m.user_id=? ORDER BY o.name""",
            (session["user_id"],),
        ).fetchall()
        return {
            "id": session["user_id"],
            "phone_e164": session["phone_e164"],
            "display_name": session["display_name"],
            "status": session["status"],
            "last_login_at": session["last_login_at"],
            "csrf_token": session["csrf_token"],
            "organizations": [dict(row) for row in orgs],
            "security_notifications": [
                {**dict(row), "detail": json.loads(row["detail_json"])}
                for row in conn.execute(
                    """SELECT * FROM security_notifications
                       WHERE user_id=? ORDER BY created_at DESC LIMIT 50""",
                    (session["user_id"],),
                ).fetchall()
            ],
        }

    @app.post(
        "/api/v1/security-notifications/{notification_id}/read",
        dependencies=[Depends(require_csrf)],
    )
    def read_security_notification(
        notification_id: str, session: Session, conn: Db
    ) -> dict[str, str]:
        changed = conn.execute(
            """UPDATE security_notifications SET read_at=?
               WHERE id=? AND user_id=?""",
            (iso(), notification_id, session["user_id"]),
        )
        if changed.rowcount != 1:
            raise HTTPException(404, "Security notification not found")
        return {"status": "read"}

    @app.patch("/api/v1/me", dependencies=[Depends(require_csrf)])
    def update_profile(payload: UserProfileIn, session: Session, conn: Db) -> dict[str, str]:
        display_name = payload.display_name.strip()
        if not display_name:
            raise HTTPException(422, "Name must not be blank")
        conn.execute(
            "UPDATE users SET display_name=? WHERE id=?",
            (display_name, session["user_id"]),
        )
        return {"id": session["user_id"], "display_name": display_name}

    @app.post("/api/v1/organizations", dependencies=[Depends(require_csrf)])
    def create_organization(payload: OrganizationIn, session: Session, conn: Db) -> dict[str, Any]:
        org_id, timestamp = uid("org"), iso()
        with db.transaction() as tx:
            tx.execute(
                "INSERT INTO organizations VALUES(?,?,?,?,?,?)",
                (org_id, payload.name.strip(), "advertiser", session["user_id"], timestamp, timestamp),
            )
            tx.execute(
                "INSERT INTO organization_memberships VALUES(?,?,?,?)",
                (org_id, session["user_id"], "owner", timestamp),
            )
        return {"id": org_id, "name": payload.name.strip(), "role": "owner"}

    @app.get("/api/v1/organizations")
    def list_organizations(session: Session, conn: Db) -> list[dict[str, Any]]:
        rows = conn.execute(
            """SELECT o.*,m.role FROM organizations o JOIN organization_memberships m
               ON m.organization_id=o.id WHERE m.user_id=? ORDER BY o.created_at DESC""",
            (session["user_id"],),
        ).fetchall()
        return [json_row(row) for row in rows]

    @app.get("/api/v1/organizations/{organization_id}")
    def get_organization(organization_id: str, session: Session, conn: Db) -> dict[str, Any]:
        membership(conn, session["user_id"], organization_id)
        row = conn.execute("SELECT * FROM organizations WHERE id=?", (organization_id,)).fetchone()
        return json_row(row)

    @app.patch("/api/v1/organizations/{organization_id}", dependencies=[Depends(require_csrf)])
    def update_organization(
        organization_id: str, payload: OrganizationIn, session: Session, conn: Db
    ) -> dict[str, Any]:
        membership(conn, session["user_id"], organization_id, "manage_organization")
        conn.execute(
            "UPDATE organizations SET name=?,updated_at=? WHERE id=?",
            (payload.name.strip(), iso(), organization_id),
        )
        return {"id": organization_id, "name": payload.name.strip()}

    @app.get("/api/v1/organizations/{organization_id}/members")
    def list_members(organization_id: str, session: Session, conn: Db) -> list[dict[str, Any]]:
        membership(conn, session["user_id"], organization_id)
        rows = conn.execute(
            """SELECT m.user_id,m.role,m.created_at,u.phone_e164,u.display_name
               FROM organization_memberships m JOIN users u ON u.id=m.user_id
               WHERE m.organization_id=? ORDER BY m.created_at""",
            (organization_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @app.post(
        "/api/v1/organizations/{organization_id}/members/invite",
        dependencies=[Depends(require_csrf)],
    )
    def invite_member(
        organization_id: str, payload: MemberInviteIn, session: Session, conn: Db
    ) -> dict[str, Any]:
        membership(conn, session["user_id"], organization_id, "manage_members")
        if payload.role not in roles:
            raise HTTPException(422, "Unknown organization role")
        try:
            phone = normalize_phone(payload.phone)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        display_name = payload.display_name.strip()
        if not display_name:
            raise HTTPException(422, "Name must not be blank")
        existing = conn.execute(
            """SELECT 1 FROM organization_memberships m JOIN users u ON u.id=m.user_id
               WHERE m.organization_id=? AND u.phone_e164=?""",
            (organization_id, phone),
        ).fetchone()
        if existing:
            raise HTTPException(409, "This phone number is already a member")
        token = new_token()
        invitation_id = uid("inv")
        expires_at = iso(now() + timedelta(hours=settings.invitation_ttl_hours))
        with db.transaction() as tx:
            tx.execute(
                """UPDATE organization_invitations SET revoked_at=?
                   WHERE organization_id=? AND phone_e164=? AND accepted_at IS NULL
                   AND revoked_at IS NULL""",
                (iso(), organization_id, phone),
            )
            tx.execute(
                """INSERT INTO organization_invitations(
                   id,organization_id,phone_e164,invitee_name,role,token_hash,
                   invited_by,invited_at,expires_at,accepted_at,accepted_by,revoked_at)
                   VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL,NULL)""",
                (
                    invitation_id,
                    organization_id,
                    phone,
                    display_name,
                    payload.role,
                    digest(token),
                    session["user_id"],
                    iso(),
                    expires_at,
                ),
            )
            audit(
                tx,
                session["user_id"],
                organization_id,
                "organization.member_invited",
                "invitation",
                invitation_id,
                {
                    "phone_suffix": phone[-4:],
                    "display_name": display_name,
                    "role": payload.role,
                },
            )
        delivery_status = "FAILED"
        delivery_error: str | None = None
        if settings.invitation_base_url:
            invitation_url = (
                f"{settings.invitation_base_url.rstrip('/')}/accept-invitation"
                f"?token={token}"
            )
            with db.transaction() as tx:
                outbox_id = enqueue_sms(
                    tx,
                    kind="INVITATION",
                    phone_e164=phone,
                    body=(
                        f"{display_name}, you were invited to TMAds. "
                        f"Sign in and accept: {invitation_url}"
                    ),
                    related_id=invitation_id,
                )
            process_sms_outbox(only_id=outbox_id)
            delivery = conn.execute(
                """SELECT status,last_error FROM sms_outbox WHERE id=?""",
                (outbox_id,),
            ).fetchone()
            delivery_status = delivery["status"]
            delivery_error = delivery["last_error"]
        else:
            delivery_error = "TMADS_INVITATION_BASE_URL is not configured"
            conn.execute(
                """UPDATE organization_invitations
                   SET delivery_status='FAILED',delivery_error=? WHERE id=?""",
                (delivery_error, invitation_id),
            )
        result = {
            "id": invitation_id,
            "phone_e164": phone,
            "display_name": display_name,
            "role": payload.role,
            "expires_at": expires_at,
            "delivery_status": delivery_status,
            "delivery_error": delivery_error,
        }
        if settings.environment in demo_environments:
            result["acceptance_token"] = token
        return result

    @app.post(
        "/api/v1/organizations/{organization_id}/invitations/{invitation_id}/retry",
        dependencies=[Depends(require_csrf)],
    )
    def retry_invitation(
        organization_id: str, invitation_id: str, session: Session, conn: Db
    ) -> dict[str, Any]:
        membership(conn, session["user_id"], organization_id, "manage_members")
        invitation = conn.execute(
            """SELECT * FROM organization_invitations WHERE id=? AND organization_id=?
               AND accepted_at IS NULL AND revoked_at IS NULL AND expires_at>?""",
            (invitation_id, organization_id, iso()),
        ).fetchone()
        if invitation is None:
            raise HTTPException(404, "Invitation not found or expired")
        if not settings.invitation_base_url:
            raise HTTPException(503, "TMADS_INVITATION_BASE_URL is not configured")
        token = new_token()
        url = (
            f"{settings.invitation_base_url.rstrip('/')}/accept-invitation"
            f"?token={token}"
        )
        with db.transaction() as tx:
            tx.execute(
                """UPDATE organization_invitations SET token_hash=?,
                   delivery_status='PENDING',delivery_error=NULL WHERE id=?""",
                (digest(token), invitation_id),
            )
            outbox_id = enqueue_sms(
                tx,
                kind="INVITATION",
                phone_e164=invitation["phone_e164"],
                body=(
                    f"{invitation['invitee_name']}, you were invited to TMAds. "
                    f"Sign in and accept: {url}"
                ),
                related_id=invitation_id,
            )
        process_sms_outbox(only_id=outbox_id)
        delivery = conn.execute(
            "SELECT status,provider_id,last_error FROM sms_outbox WHERE id=?",
            (outbox_id,),
        ).fetchone()
        return {
            "id": invitation_id,
            "delivery_status": delivery["status"],
            "delivery_provider_id": delivery["provider_id"],
            "delivery_error": delivery["last_error"],
        }

    @app.get("/api/v1/invitations")
    def pending_invitations(session: Session, conn: Db) -> list[dict[str, Any]]:
        rows = conn.execute(
            """SELECT i.id,i.organization_id,o.name AS organization_name,
               i.invitee_name,i.role,i.expires_at,i.delivery_status,
               i.delivery_attempts,i.delivery_error
               FROM organization_invitations i JOIN organizations o ON o.id=i.organization_id
               WHERE i.phone_e164=? AND i.accepted_at IS NULL AND i.revoked_at IS NULL
               AND i.expires_at>? ORDER BY i.invited_at""",
            (session["phone_e164"], iso()),
        ).fetchall()
        return [dict(row) for row in rows]

    @app.post("/api/v1/invitations/{token}/accept", dependencies=[Depends(require_csrf)])
    def accept_invitation(token: str, session: Session, conn: Db) -> dict[str, Any]:
        with db.transaction() as tx:
            invitation = tx.execute(
                """SELECT * FROM organization_invitations WHERE token_hash=?
                   AND accepted_at IS NULL AND revoked_at IS NULL AND expires_at>?""",
                (digest(token), iso()),
            ).fetchone()
            if not invitation or invitation["phone_e164"] != session["phone_e164"]:
                raise HTTPException(404, "Invitation not found or expired")
            tx.execute(
                "INSERT OR IGNORE INTO organization_memberships VALUES(?,?,?,?)",
                (
                    invitation["organization_id"],
                    session["user_id"],
                    invitation["role"],
                    iso(),
                ),
            )
            if invitation["invitee_name"]:
                tx.execute(
                    """UPDATE users SET display_name=COALESCE(NULLIF(display_name,''),?)
                       WHERE id=?""",
                    (invitation["invitee_name"], session["user_id"]),
                )
            updated = tx.execute(
                """UPDATE organization_invitations SET accepted_at=?,accepted_by=?
                   WHERE id=? AND accepted_at IS NULL""",
                (iso(), session["user_id"], invitation["id"]),
            )
            if updated.rowcount != 1:
                raise HTTPException(409, "Invitation was already accepted")
            audit(
                tx,
                session["user_id"],
                invitation["organization_id"],
                "organization.invitation_accepted",
                "invitation",
                invitation["id"],
            )
        return {
            "organization_id": invitation["organization_id"],
            "role": invitation["role"],
            "status": "accepted",
        }

    def protect_last_owner(
        conn: sqlite3.Connection, organization_id: str, user_id: str, new_role: str | None
    ) -> None:
        target = conn.execute(
            """SELECT role FROM organization_memberships
               WHERE organization_id=? AND user_id=?""",
            (organization_id, user_id),
        ).fetchone()
        if not target:
            raise HTTPException(404, "Member not found")
        if target["role"] == "owner" and new_role != "owner":
            owners = conn.execute(
                """SELECT COUNT(*) FROM organization_memberships
                   WHERE organization_id=? AND role='owner'""",
                (organization_id,),
            ).fetchone()[0]
            if owners <= 1:
                raise HTTPException(409, "An organization must retain at least one owner")

    @app.patch(
        "/api/v1/organizations/{organization_id}/members/{member_id}",
        dependencies=[Depends(require_csrf)],
    )
    def update_member(
        organization_id: str,
        member_id: str,
        payload: MemberRoleIn,
        session: Session,
        conn: Db,
    ) -> dict[str, Any]:
        membership(conn, session["user_id"], organization_id, "manage_members")
        if payload.role not in roles:
            raise HTTPException(422, "Unknown organization role")
        with db.transaction() as tx:
            protect_last_owner(tx, organization_id, member_id, payload.role)
            tx.execute(
                """UPDATE organization_memberships SET role=?
                   WHERE organization_id=? AND user_id=?""",
                (payload.role, organization_id, member_id),
            )
            display_name = payload.display_name.strip() if payload.display_name else None
            if payload.display_name is not None and not display_name:
                raise HTTPException(422, "Name must not be blank")
            if display_name is not None:
                tx.execute(
                    "UPDATE users SET display_name=? WHERE id=?",
                    (display_name, member_id),
                )
            audit(
                tx,
                session["user_id"],
                organization_id,
                "organization.member_role_updated",
                "user",
                member_id,
                {"role": payload.role, "display_name": display_name},
            )
        return {
            "user_id": member_id,
            "role": payload.role,
            "display_name": display_name,
        }

    @app.delete(
        "/api/v1/organizations/{organization_id}/members/{member_id}",
        dependencies=[Depends(require_csrf)],
    )
    def remove_member(
        organization_id: str, member_id: str, session: Session, conn: Db
    ) -> dict[str, str]:
        membership(conn, session["user_id"], organization_id, "manage_members")
        with db.transaction() as tx:
            protect_last_owner(tx, organization_id, member_id, None)
            tx.execute(
                "DELETE FROM organization_memberships WHERE organization_id=? AND user_id=?",
                (organization_id, member_id),
            )
            audit(
                tx,
                session["user_id"],
                organization_id,
                "organization.member_removed",
                "user",
                member_id,
            )
        return {"status": "removed"}

    def wallet_payload(conn: sqlite3.Connection, organization_id: str) -> dict[str, Any]:
        available = balance(conn, organization_id, "org_available")
        reserved = balance(conn, organization_id, "org_reserved")
        spent = balance(conn, organization_id, "org_spent")
        deposited = conn.execute(
            """SELECT COALESCE(SUM(e.credit_minor),0)
               FROM financial_entries e JOIN financial_accounts a ON a.id=e.account_id
               JOIN financial_transactions t ON t.id=e.transaction_id
               WHERE a.organization_id=? AND a.account_type IN ('org_available','campaign_funds')
               AND t.kind IN ('demo_sandbox_deposit','stripe_test_deposit')""",
            (organization_id,),
        ).fetchone()[0]
        pending = conn.execute(
            """SELECT COALESCE(SUM(amount_minor),0) FROM pending_deposits
               WHERE organization_id=? AND status='pending'""",
            (organization_id,),
        ).fetchone()[0]
        return {
            "organization_id": organization_id,
            "currency": "USD",
            "balance_minor": available,
            "available_minor": available,
            "reserved_minor": reserved,
            "spent_minor": spent,
            "deposited_minor": int(deposited),
            "pending_minor": int(pending),
            "label": "Prepaid Campaign Funds",
        }

    @app.get("/api/v1/organizations/{organization_id}/wallet")
    def wallet(organization_id: str, session: Session, conn: Db) -> dict[str, Any]:
        membership(conn, session["user_id"], organization_id, "view_wallet")
        return wallet_payload(conn, organization_id)

    @app.get("/api/v1/organizations/{organization_id}/wallet/entries")
    def wallet_entries(organization_id: str, session: Session, conn: Db) -> list[dict[str, Any]]:
        membership(conn, session["user_id"], organization_id, "view_wallet")
        rows = conn.execute(
            """SELECT e.*,t.kind,t.description FROM financial_entries e
               JOIN financial_transactions t ON t.id=e.transaction_id
               JOIN financial_accounts a ON a.id=e.account_id
               WHERE a.organization_id=? AND a.account_type IN ('org_available','campaign_funds')
               ORDER BY e.created_at DESC""",
            (organization_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @app.get("/api/v1/organizations/{organization_id}/wallet/transactions")
    def wallet_transactions(
        organization_id: str,
        session: Session,
        conn: Db,
        limit: int = Query(default=100, ge=1, le=500),
        offset: int = Query(default=0, ge=0),
    ) -> list[dict[str, Any]]:
        membership(conn, session["user_id"], organization_id, "view_wallet")
        rows = conn.execute(
            """SELECT t.id,t.kind,t.description,t.created_at,
               SUM(e.debit_minor) AS debit_minor,SUM(e.credit_minor) AS credit_minor
               FROM financial_transactions t JOIN financial_entries e ON e.transaction_id=t.id
               JOIN financial_accounts a ON a.id=e.account_id
               WHERE t.organization_id=? AND a.organization_id=?
               GROUP BY t.id,t.kind,t.description,t.created_at ORDER BY t.created_at DESC
               LIMIT ? OFFSET ?""",
            (organization_id, organization_id, limit, offset),
        ).fetchall()
        return [
            {
                **dict(row),
                "amount_minor": max(int(row["debit_minor"]), int(row["credit_minor"])),
            }
            for row in rows
        ]

    @app.get("/api/v1/organizations/{organization_id}/wallet/grants")
    def wallet_grants(
        organization_id: str, session: Session, conn: Db
    ) -> list[dict[str, Any]]:
        membership(conn, session["user_id"], organization_id, "view_wallet")
        rows = conn.execute(
            """SELECT grant_id,campaign_id,contract_hash,device_id,currency,amount_minor,
               spent_minor,released_minor,
               amount_minor-spent_minor-released_minor AS pending_minor,
               status,settlement_attempts,settlement_next_attempt_at,
               settlement_last_error,created_at,updated_at FROM grant_commitments
               WHERE organization_id=? ORDER BY created_at DESC""",
            (organization_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @app.get("/api/v1/organizations/{organization_id}/wallet/deposits")
    def wallet_deposits(
        organization_id: str,
        session: Session,
        conn: Db,
        limit: int = Query(default=25, ge=1, le=100),
    ) -> list[dict[str, Any]]:
        membership(conn, session["user_id"], organization_id, "view_wallet")
        rows = conn.execute(
            """SELECT id,provider,amount_minor,currency,status,stripe_session_id,
               transaction_id,created_at,updated_at
               FROM pending_deposits WHERE organization_id=?
               ORDER BY created_at DESC LIMIT ?""",
            (organization_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    @app.post(
        "/api/v1/organizations/{organization_id}/wallet/deposits",
        dependencies=[Depends(require_csrf)],
    )
    def deposit(
        organization_id: str,
        payload: DepositIn,
        session: Session,
        conn: Db,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        membership(conn, session["user_id"], organization_id, "deposit_funds")
        if not idempotency_key or len(idempotency_key) > 255:
            raise HTTPException(400, "A valid Idempotency-Key header is required")
        key = idempotency_key
        if funding.name == "stripe":
            pending_id = "dep_" + hashlib.sha256(
                f"{organization_id}:{key}".encode()
            ).hexdigest()[:32]
            with db.transaction() as tx:
                existing = tx.execute(
                    """SELECT * FROM pending_deposits
                       WHERE organization_id=? AND idempotency_key=?""",
                    (organization_id, key),
                ).fetchone()
                if existing:
                    if (
                        existing["amount_minor"] != payload.amount_minor
                        or existing["user_id"] != session["user_id"]
                    ):
                        raise HTTPException(409, "Idempotency key was already used")
                    return {
                        "pending_deposit_id": existing["id"],
                        "session_id": existing["stripe_session_id"],
                        "checkout_url": existing["stripe_checkout_url"],
                        "status": existing["status"],
                    }
                timestamp = iso()
                tx.execute(
                    """INSERT INTO pending_deposits
                       (id,organization_id,user_id,provider,amount_minor,currency,
                        idempotency_key,stripe_session_id,stripe_checkout_url,status,
                        transaction_id,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,? ,NULL,?,?)""",
                    (
                        pending_id,
                        organization_id,
                        session["user_id"],
                        "stripe",
                        payload.amount_minor,
                        "USD",
                        key,
                        None,
                        None,
                        "pending",
                        timestamp,
                        timestamp,
                    ),
                )
                try:
                    checkout = funding.create_checkout(
                        organization_id=organization_id,
                        user_id=session["user_id"],
                        pending_deposit_id=pending_id,
                        amount_minor=payload.amount_minor,
                        idempotency_key="tmads:"
                        + hashlib.sha256(f"{organization_id}:{key}".encode()).hexdigest(),
                    )
                except RuntimeError as exc:
                    raise HTTPException(503, str(exc)) from exc
                tx.execute(
                    """UPDATE pending_deposits
                       SET stripe_session_id=?,stripe_checkout_url=?,updated_at=? WHERE id=?""",
                    (checkout.id, checkout.url, iso(), pending_id),
                )
                audit(
                    tx,
                    session["user_id"],
                    organization_id,
                    "wallet.stripe_checkout_created",
                    "pending_deposit",
                    pending_id,
                    {"amount_minor": payload.amount_minor, "currency": "USD"},
                )
            return {
                "pending_deposit_id": pending_id,
                "session_id": checkout.id,
                "checkout_url": checkout.url,
                "status": "pending",
            }

        if funding.name != "demo":
            raise HTTPException(503, "No funding provider configured")
        with db.transaction() as tx:
            existing = tx.execute(
                "SELECT id FROM financial_transactions WHERE organization_id=? AND idempotency_key=?",
                (organization_id, key),
            ).fetchone()
            if existing:
                return wallet_payload(tx, organization_id)
            try:
                receipt = funding.authorize(organization_id, payload.amount_minor, key)
            except RuntimeError as exc:
                raise HTTPException(503, str(exc)) from exc
            funds_id, clearing_id = f"acct_{organization_id}_funds", "acct_platform_demo_clearing"
            tx.execute(
                "INSERT OR IGNORE INTO financial_accounts VALUES(?,?,?,?)",
                (funds_id, organization_id, "org_available", "USD"),
            )
            tx.execute(
                "INSERT OR IGNORE INTO financial_accounts VALUES(?,?,?,?)",
                (clearing_id, None, "demo_clearing", "USD"),
            )
            transaction_id = uid("txn")
            tx.execute(
                "INSERT INTO financial_transactions VALUES(?,?,?,?,?,?)",
                (
                    transaction_id,
                    organization_id,
                    key,
                    "demo_sandbox_deposit",
                    f"Demo sandbox funding ({receipt.reference}) — not real money or regulated escrow",
                    iso(),
                ),
            )
            for account, debit, credit in (
                (clearing_id, payload.amount_minor, 0),
                (funds_id, 0, payload.amount_minor),
            ):
                tx.execute(
                    "INSERT INTO financial_entries VALUES(?,?,?,?,?,?,?,?)",
                    (
                        uid("entry"),
                        transaction_id,
                        account,
                        debit,
                        credit,
                        "demo_deposit",
                        transaction_id,
                        iso(),
                    ),
                )
            audit(
                tx,
                session["user_id"],
                organization_id,
                "wallet.demo_deposited",
                "transaction",
                transaction_id,
                {
                    "amount_minor": payload.amount_minor,
                    "provider": receipt.provider,
                    "provider_reference": receipt.reference,
                    "sandbox": receipt.sandbox,
                },
            )
        return wallet_payload(conn, organization_id)

    def event_dict(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if hasattr(value, "to_dict_recursive"):
            return value.to_dict_recursive()
        return dict(value)

    def checkout_matches_pending(
        pending: sqlite3.Row, session_object: dict[str, Any]
    ) -> bool:
        metadata = event_dict(session_object.get("metadata") or {})
        return not (
            pending["provider"] != "stripe"
            or str(session_object.get("currency", "")).upper() != pending["currency"]
            or session_object.get("amount_total") != pending["amount_minor"]
            or metadata.get("organization_id") != pending["organization_id"]
            or metadata.get("user_id") != pending["user_id"]
            or metadata.get("pending_deposit_id") != pending["id"]
        )

    def credit_paid_stripe_deposit(
        tx: sqlite3.Connection, pending: sqlite3.Row, session_object: dict[str, Any]
    ) -> bool:
        if (
            not checkout_matches_pending(pending, session_object)
            or session_object.get("payment_status") != "paid"
        ):
            return False
        if pending["transaction_id"] is not None:
            return True
        session_id = str(session_object.get("id", ""))
        funds_id = f"acct_{pending['organization_id']}_funds"
        clearing_id = "acct_platform_stripe_clearing"
        tx.execute(
            "INSERT OR IGNORE INTO financial_accounts VALUES(?,?,?,?)",
            (funds_id, pending["organization_id"], "org_available", "USD"),
        )
        tx.execute(
            "INSERT OR IGNORE INTO financial_accounts VALUES(?,?,?,?)",
            (clearing_id, None, "external_stripe_clearing", "USD"),
        )
        transaction_id = uid("txn")
        tx.execute(
            "INSERT INTO financial_transactions VALUES(?,?,?,?,?,?)",
            (
                transaction_id,
                pending["organization_id"],
                f"stripe:{pending['id']}",
                "stripe_test_deposit",
                f"Stripe test-mode funding ({session_id}) — not regulated escrow",
                iso(),
            ),
        )
        for account, debit, credit in (
            (clearing_id, pending["amount_minor"], 0),
            (funds_id, 0, pending["amount_minor"]),
        ):
            tx.execute(
                "INSERT INTO financial_entries VALUES(?,?,?,?,?,?,?,?)",
                (
                    uid("entry"),
                    transaction_id,
                    account,
                    debit,
                    credit,
                    "stripe_checkout_session",
                    session_id,
                    iso(),
                ),
            )
        tx.execute(
            """UPDATE pending_deposits
               SET status='paid',transaction_id=?,updated_at=? WHERE id=?""",
            (transaction_id, iso(), pending["id"]),
        )
        audit(
            tx,
            pending["user_id"],
            pending["organization_id"],
            "wallet.stripe_deposited",
            "transaction",
            transaction_id,
            {
                "amount_minor": pending["amount_minor"],
                "currency": pending["currency"],
                "stripe_session_id": session_id,
            },
        )
        return True

    def confirm_stripe_checkout_return(
        organization_id: str, session_id: str
    ) -> bool:
        if funding.name != "stripe" or not hasattr(funding, "retrieve_checkout"):
            return False
        try:
            checkout = event_dict(funding.retrieve_checkout(session_id))
        except Exception:
            log.exception("Stripe checkout return could not be retrieved")
            return False
        with db.transaction() as tx:
            pending = tx.execute(
                """SELECT * FROM pending_deposits
                   WHERE organization_id=? AND stripe_session_id=?""",
                (organization_id, session_id),
            ).fetchone()
            if not pending:
                return False
            return credit_paid_stripe_deposit(tx, pending, checkout)

    @app.post("/api/v1/webhooks/stripe")
    async def stripe_webhook(
        request: Request,
        stripe_signature: Annotated[str | None, Header(alias="Stripe-Signature")] = None,
    ) -> dict[str, Any]:
        if funding.name != "stripe":
            raise HTTPException(404, "Stripe webhook is not configured")
        if not stripe_signature:
            raise HTTPException(400, "Missing Stripe-Signature")
        body = await request.body()
        try:
            event = event_dict(funding.construct_event(body, stripe_signature))
        except (ValueError, KeyError) as exc:
            raise HTTPException(400, "Invalid Stripe webhook") from exc
        except Exception as exc:
            if exc.__class__.__name__ == "SignatureVerificationError":
                raise HTTPException(400, "Invalid Stripe signature") from exc
            raise

        event_id, event_type = str(event.get("id", "")), str(event.get("type", ""))
        if not event_id or not event_type:
            raise HTTPException(400, "Invalid Stripe event")
        relevant = {
            "checkout.session.completed",
            "checkout.session.async_payment_succeeded",
            "checkout.session.async_payment_failed",
            "checkout.session.expired",
        }
        session_object = event_dict(event.get("data", {}).get("object", {}))
        session_id = str(session_object.get("id", ""))
        response_status = "ignored"
        response_error: str | None = None

        with db.transaction() as tx:
            prior = tx.execute(
                "SELECT status FROM stripe_events WHERE event_id=?", (event_id,)
            ).fetchone()
            if prior:
                return {"received": True, "duplicate": True, "status": prior["status"]}
            tx.execute(
                """INSERT INTO stripe_events
                   (event_id,event_type,pending_deposit_id,status,error,created_at,processed_at)
                   VALUES(?,?,NULL,'received',NULL,?,NULL)""",
                (event_id, event_type, iso()),
            )
            if event_type in relevant:
                pending = tx.execute(
                    "SELECT * FROM pending_deposits WHERE stripe_session_id=?",
                    (session_id,),
                ).fetchone()
                if not pending:
                    response_status, response_error = "rejected", "Unknown Checkout Session"
                elif not checkout_matches_pending(pending, session_object):
                    response_status, response_error = (
                        "rejected",
                        "Checkout Session does not match pending deposit",
                    )
                elif event_type in {
                    "checkout.session.async_payment_failed",
                    "checkout.session.expired",
                }:
                    if pending["status"] != "paid":
                        tx.execute(
                            "UPDATE pending_deposits SET status='failed',updated_at=? WHERE id=?",
                            (iso(), pending["id"]),
                        )
                    response_status = "processed"
                elif session_object.get("payment_status") == "paid":
                    credit_paid_stripe_deposit(tx, pending, session_object)
                    response_status = "processed"
                else:
                    response_status = "processed"
                if pending:
                    tx.execute(
                        "UPDATE stripe_events SET pending_deposit_id=? WHERE event_id=?",
                        (pending["id"], event_id),
                    )
            tx.execute(
                """UPDATE stripe_events SET status=?,error=?,processed_at=?
                   WHERE event_id=?""",
                (response_status, response_error, iso(), event_id),
            )
        if response_status == "rejected":
            raise HTTPException(400, response_error)
        return {"received": True, "duplicate": False, "status": response_status}

    @app.post("/api/v1/campaigns", dependencies=[Depends(require_csrf)])
    def create_campaign(payload: CampaignIn, session: Session, conn: Db) -> dict[str, Any]:
        membership(conn, session["user_id"], payload.organization_id, "manage_campaigns")
        if payload.start_at >= payload.end_at:
            raise HTTPException(422, "Campaign start must precede end")
        campaign_id, timestamp = uid("cmp"), iso()
        conn.execute(
            "INSERT INTO campaigns VALUES(?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
            (
                campaign_id,
                payload.organization_id,
                payload.name.strip(),
                payload.description,
                "DRAFT",
                payload.budget_cap_minor,
                "USD",
                iso(payload.start_at),
                iso(payload.end_at),
                session["user_id"],
                timestamp,
                timestamp,
            ),
        )
        audit(conn, session["user_id"], payload.organization_id, "campaign.created", "campaign", campaign_id)
        return json_row(conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone())

    @app.get("/api/v1/campaigns")
    def list_campaigns(
        session: Session, conn: Db, organization_id: str | None = None
    ) -> list[dict[str, Any]]:
        if organization_id:
            membership(conn, session["user_id"], organization_id)
        rows = conn.execute(
            """SELECT c.* FROM campaigns c JOIN organization_memberships m
               ON m.organization_id=c.organization_id WHERE m.user_id=?
               AND (? IS NULL OR c.organization_id=?) ORDER BY c.created_at DESC""",
            (session["user_id"], organization_id, organization_id),
        ).fetchall()
        return [dict(row) for row in rows]

    @app.get("/api/v1/campaigns/{campaign_id}")
    def get_campaign(campaign_id: str, session: Session, conn: Db) -> dict[str, Any]:
        return json_row(campaign_for_user(conn, session["user_id"], campaign_id))

    @app.patch("/api/v1/campaigns/{campaign_id}", dependencies=[Depends(require_csrf)])
    def update_campaign(
        campaign_id: str, payload: CampaignIn, session: Session, conn: Db
    ) -> dict[str, Any]:
        campaign = campaign_for_user(conn, session["user_id"], campaign_id, "manage_campaigns")
        if campaign["status"] not in {"DRAFT", "PAUSED"}:
            raise HTTPException(409, "Published campaign history cannot be mutated")
        if payload.organization_id != campaign["organization_id"]:
            raise HTTPException(422, "Campaign organization cannot change")
        conn.execute(
            """UPDATE campaigns SET name=?,description=?,budget_cap_minor=?,start_at=?,end_at=?,
               updated_at=? WHERE id=?""",
            (
                payload.name,
                payload.description,
                payload.budget_cap_minor,
                iso(payload.start_at),
                iso(payload.end_at),
                iso(),
                campaign_id,
            ),
        )
        return json_row(conn.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone())

    @app.delete("/api/v1/campaigns/{campaign_id}", dependencies=[Depends(require_csrf)])
    def archive_campaign(campaign_id: str, session: Session, conn: Db) -> dict[str, str]:
        campaign = campaign_for_user(
            conn, session["user_id"], campaign_id, "manage_campaigns"
        )
        active = conn.execute(
            """SELECT 1 FROM bids WHERE campaign_id=? AND current_revision>0
               AND status NOT IN ('CANCELLED','COMPLETED','EXPIRED','EXHAUSTED')""",
            (campaign_id,),
        ).fetchone()
        if active:
            raise HTTPException(409, "Complete or cancel active bids before archiving")
        conn.execute(
            "UPDATE campaigns SET status='ARCHIVED',archived_at=?,updated_at=? WHERE id=?",
            (iso(), iso(), campaign_id),
        )
        audit(
            conn,
            session["user_id"],
            campaign["organization_id"],
            "campaign.archived",
            "campaign",
            campaign_id,
        )
        return {"status": "ARCHIVED"}

    @app.post(
        "/api/v1/campaigns/{campaign_id}/media", dependencies=[Depends(require_csrf)]
    )
    async def upload_media(
        campaign_id: str,
        session: Session,
        conn: Db,
        file: Annotated[UploadFile, File()],
    ) -> dict[str, Any]:
        campaign = campaign_for_user(conn, session["user_id"], campaign_id, "manage_campaigns")
        media_id = uid("med")
        source_name = f"{media_id}{Path(file.filename or '').suffix.lower()}"
        source_path = settings.quarantine_dir / source_name
        size, hasher = 0, hashlib.sha256()
        header = bytearray()
        try:
            with source_path.open("xb") as destination:
                while chunk := await file.read(1024 * 1024):
                    size += len(chunk)
                    if size > settings.max_upload_bytes:
                        raise HTTPException(413, "Upload exceeds configured limit")
                    if len(header) < 64:
                        header.extend(chunk[: 64 - len(header)])
                    hasher.update(chunk)
                    destination.write(chunk)
            kind, detected_mime = sniff_media(file.filename or "", bytes(header))
        except ValueError as exc:
            source_path.unlink(missing_ok=True)
            raise HTTPException(422, str(exc)) from exc
        except Exception:
            source_path.unlink(missing_ok=True)
            raise
        timestamp = iso()
        conn.execute(
            """INSERT INTO media_assets VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                media_id,
                campaign_id,
                campaign["organization_id"],
                Path(file.filename or "upload").name,
                f"sha256:{hasher.hexdigest()}",
                None,
                kind,
                detected_mime,
                None,
                None,
                None,
                size,
                "QUARANTINED",
                None,
                source_name,
                timestamp,
            ),
        )
        try:
            result = processor.process(source_path, kind, media_id)
            derivative_path = settings.media_dir / result.derivative_name
            quarantined_derivative = settings.quarantine_dir / result.derivative_name
            if not derivative_path.exists() and quarantined_derivative.exists():
                quarantined_derivative.replace(derivative_path)
            if not derivative_path.is_file():
                raise ValueError("Media processor did not produce the declared derivative")
            derivative_mime = "image/webp" if kind == "image" else "video/mp4"
            MediaManifestV1, sign_media = shared_media_components()
            media_private_key, media_key_id = load_or_create_signing_key(
                settings.secrets_dir
            )
            unsigned_manifest = MediaManifestV1(
                schema="tmads.media-manifest.v1",
                media_id=media_id,
                asset_hash=result.derivative_hash,
                kind=kind,
                width=result.width,
                height=result.height,
                duration_ms=result.duration_ms,
                mime=derivative_mime,
                bytes=derivative_path.stat().st_size,
                audio=False,
                created_at=timestamp,
                derivative_profile="tmads-portrait-v1",
                signer_key_id=media_key_id,
            )
            manifest = unsigned_manifest.model_copy(
                update={"signature": sign_media(unsigned_manifest, media_private_key)}
            ).model_dump(mode="json", by_alias=True, exclude_none=True)
            with db.transaction() as tx:
                tx.execute(
                    """UPDATE media_assets SET derivative_hash=?,width=?,height=?,duration_ms=?,
                       processing_status='READY',mime=?,bytes=?,storage_name=? WHERE id=?""",
                    (
                        result.derivative_hash,
                        result.width,
                        result.height,
                        result.duration_ms,
                        derivative_mime,
                        derivative_path.stat().st_size,
                        result.derivative_name,
                        media_id,
                    ),
                )
                enqueue(
                    tx,
                    campaign["organization_id"],
                    "media_manifest",
                    result.derivative_hash,
                    normalized_json(manifest),
                )
                enqueue(
                    tx,
                    campaign["organization_id"],
                    "media_content",
                    result.derivative_hash,
                    content_path=str(derivative_path.resolve()),
                )
            source_path.unlink(missing_ok=True)
            if edge:
                process_outbox(limit=2)
        except Exception as exc:
            log.warning("media_processing_failed", extra={"media_id": media_id, "error": str(exc)})
            conn.execute(
                "UPDATE media_assets SET processing_status='REJECTED',error=? WHERE id=?",
                (str(exc)[:500], media_id),
            )
        return json_row(conn.execute("SELECT * FROM media_assets WHERE id=?", (media_id,)).fetchone())

    @app.get("/api/v1/media/{media_id}")
    def get_media(media_id: str, session: Session, conn: Db) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM media_assets WHERE id=?", (media_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Media not found")
        membership(conn, session["user_id"], row["organization_id"])
        return json_row(row)

    @app.get("/api/v1/media/{media_id}/content")
    def media_content(media_id: str, session: Session, conn: Db) -> FileResponse:
        row = get_media(media_id, session, conn)
        if row["kind"] != "image" or row["processing_status"] != "READY":
            raise HTTPException(404, "Media preview is not available")
        path = settings.media_dir / (row["storage_name"] or "")
        if not path.is_file():
            matches = sorted(settings.media_dir.glob(f"{media_id}-original.*"))
            path = matches[0] if matches else path
        if not path.is_file():
            raise HTTPException(404, "Media file is missing")
        return FileResponse(path, media_type=row["mime"] or "image/webp")

    @app.get("/api/v1/media/{media_id}/status")
    def media_status(media_id: str, session: Session, conn: Db) -> dict[str, Any]:
        row = get_media(media_id, session, conn)
        return {
            "id": media_id,
            "processing_status": row["processing_status"],
            "error": row["error"],
            "malware_scan": "UNAVAILABLE",
            "validation": "SERVER_SIGNATURE_AND_SAFE_DECODE_TRANSCODE",
        }

    @app.delete("/api/v1/media/{media_id}", dependencies=[Depends(require_csrf)])
    def delete_media(media_id: str, session: Session, conn: Db) -> dict[str, str]:
        row = conn.execute("SELECT * FROM media_assets WHERE id=?", (media_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Media not found")
        membership(conn, session["user_id"], row["organization_id"], "manage_campaigns")
        used = conn.execute(
            "SELECT 1 FROM bid_revisions WHERE contract_json LIKE ?", (f'%"{media_id}"%',)
        ).fetchone()
        if used:
            raise HTTPException(409, "Published media history cannot be deleted")
        conn.execute("DELETE FROM media_assets WHERE id=?", (media_id,))
        (settings.media_dir / row["storage_name"]).unlink(missing_ok=True)
        (settings.quarantine_dir / row["storage_name"]).unlink(missing_ok=True)
        return {"status": "deleted"}

    def validate_bid_data(
        conn: sqlite3.Connection, bid: sqlite3.Row
    ) -> tuple[dict[str, Any], list[sqlite3.Row]]:
        draft = json.loads(bid["draft_json"])
        errors = []
        campaign = conn.execute(
            "SELECT * FROM campaigns WHERE id=?", (bid["campaign_id"],)
        ).fetchone()
        if not campaign or campaign["status"] in {"CANCELLED", "ARCHIVED"}:
            errors.append("Cancelled or archived campaigns cannot be published")
        if bid["status"] == "CANCELLED":
            errors.append("Cancelled bids cannot be published")
        start = datetime.fromisoformat(draft["valid_from"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(draft["valid_to"].replace("Z", "+00:00"))
        if start >= end:
            errors.append("Validity start must precede end")
        if end - start > timedelta(hours=2, seconds=1):
            errors.append("MVP bid validity may not exceed two hours")
        if draft["qfv_price_min_minor"] > draft["qfv_price_max_minor"]:
            errors.append("Minimum QFV price must not exceed maximum")
        if campaign and draft["total_cap_minor"] > campaign["budget_cap_minor"]:
            errors.append("Bid cap exceeds the campaign budget cap")
        try:
            score_curve(0.0, draft["market_score_curve"])
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"Invalid market score curve: {exc}")
        if draft.get("criteria") is not None:
            CriterionV1 = shared_contract_components()[6]
            try:
                [CriterionV1.model_validate(item) for item in draft["criteria"]]
            except (TypeError, ValueError) as exc:
                errors.append(f"Invalid criteria list: {exc}")
        media_rows = [
            row
            for media_id in draft["media_ids"]
            if (row := conn.execute("SELECT * FROM media_assets WHERE id=?", (media_id,)).fetchone())
        ]
        if len(media_rows) != len(set(draft["media_ids"])) or any(
            row["organization_id"] != bid["organization_id"]
            or row["campaign_id"] != bid["campaign_id"]
            or row["processing_status"] != "READY"
            for row in media_rows
        ):
            errors.append("All selected media must be READY and belong to this campaign")
        if errors:
            raise HTTPException(422, detail=errors)
        return draft, media_rows

    @app.get("/api/v1/criteria/catalog")
    def criteria_catalog(session: Session) -> dict[str, Any]:
        del session
        return {
            "kinds": [
                "time.hour",
                "time.weekday",
                "date.iso",
                "date.month",
                "geography.geohash",
                "weather.code",
                "weather.class",
                "weather.temperature",
                "weather.humidity",
                "weather.precipitation",
                "weather.wind",
                "sports.proximity",
                "sports.state",
                "sports.result",
                "sports.scoring",
                "market.daily_return",
                "market.value",
                "market.direction",
                "market.session",
                "attention.face_count",
                "attention.qfv_count",
                "attention.qfv_confidence",
                "gnss.latitude",
                "gnss.longitude",
                "gnss.speed",
                "sensor.presence",
                "sensor.mmwave_count",
                "sensor.sound_level",
            ],
            "operators": ["eq", "neq", "lt", "lte", "gt", "gte", "range", "in"],
            "curves": [
                "binary",
                "linear",
                "inverse_linear",
                "triangular",
                "trapezoidal",
                "step",
                "distance_decay",
                "time_proximity_decay",
            ],
            "unknown_policies": [
                "fail_closed",
                "neutral",
                "ineligible",
                "last_known",
                "defer",
            ],
            "acceptance_preset_available": True,
            "sports_teams": sports_catalog_json(),
        }

    @app.post("/api/v1/campaigns/{campaign_id}/bids", dependencies=[Depends(require_csrf)])
    def create_bid(
        campaign_id: str, payload: BidIn, session: Session, conn: Db
    ) -> dict[str, Any]:
        campaign = campaign_for_user(conn, session["user_id"], campaign_id, "manage_campaigns")
        if campaign["status"] in {"CANCELLED", "ARCHIVED"}:
            raise HTTPException(409, "Cancelled or archived campaigns cannot accept bids")
        draft = payload.model_dump(mode="json")
        draft["valid_from"], draft["valid_to"] = iso(payload.valid_from), iso(payload.valid_to)
        bid_id, timestamp = uid("bid"), iso()
        conn.execute(
            "INSERT INTO bids VALUES(?,?,?,?,?,?,?,?)",
            (
                bid_id,
                campaign_id,
                campaign["organization_id"],
                0,
                "DRAFT",
                json.dumps(draft, separators=(",", ":"), sort_keys=True),
                timestamp,
                timestamp,
            ),
        )
        return json_row(conn.execute("SELECT * FROM bids WHERE id=?", (bid_id,)).fetchone())

    @app.get("/api/v1/bids/{bid_id}")
    def get_bid(bid_id: str, session: Session, conn: Db) -> dict[str, Any]:
        row = bid_for_user(conn, session["user_id"], bid_id)
        result = dict(row)
        result["draft"] = json.loads(result.pop("draft_json"))
        revisions = conn.execute(
            "SELECT * FROM bid_revisions WHERE bid_id=? ORDER BY revision DESC", (bid_id,)
        ).fetchall()
        result["revisions"] = [dict(item) for item in revisions]
        result["lifecycle_events"] = [
            dict(item)
            for item in conn.execute(
                """SELECT * FROM lifecycle_events WHERE bid_id=?
                   ORDER BY created_at DESC""",
                (bid_id,),
            ).fetchall()
        ]
        result["distribution"] = [
            dict(item)
            for item in conn.execute(
                """SELECT object_key,status,attempts,last_error,synced_at
                   FROM distributor_outbox WHERE object_type='contract'
                   AND object_key IN (
                     SELECT canonical_hash FROM bid_revisions WHERE bid_id=?
                   ) ORDER BY created_at DESC""",
                (bid_id,),
            ).fetchall()
        ]
        return result

    @app.patch("/api/v1/bids/{bid_id}", dependencies=[Depends(require_csrf)])
    def update_bid(bid_id: str, payload: BidIn, session: Session, conn: Db) -> dict[str, Any]:
        bid = bid_for_user(conn, session["user_id"], bid_id, "manage_campaigns")
        if bid["status"] == "CANCELLED":
            raise HTTPException(409, "Cancelled bids cannot be edited")
        if bid["status"] == "PENDING_LEDGER":
            raise HTTPException(409, "A contract revision is awaiting ledger publication")
        draft = payload.model_dump(mode="json")
        draft["valid_from"], draft["valid_to"] = iso(payload.valid_from), iso(payload.valid_to)
        conn.execute(
            "UPDATE bids SET draft_json=?,status='DRAFT',updated_at=? WHERE id=?",
            (json.dumps(draft, separators=(",", ":"), sort_keys=True), iso(), bid_id),
        )
        return get_bid(bid_id, session, conn)

    @app.post("/api/v1/bids/{bid_id}/price-preview")
    def preview_bid_price(
        bid_id: str, payload: PreviewIn, session: Session, conn: Db
    ) -> dict[str, Any]:
        bid = bid_for_user(conn, session["user_id"], bid_id)
        draft = json.loads(bid["draft_json"])
        names = ["weather", "sports", "market"]
        if payload.sample_scores is not None:
            try:
                scores = {name: float(payload.sample_scores[name]) for name in names}
            except (KeyError, TypeError, ValueError) as exc:
                raise HTTPException(422, "Sample scores must include weather, sports, and market") from exc
        elif payload.sample_values is not None:
            try:
                scores = {
                    "weather": float(payload.sample_values["weather"]),
                    "sports": float(payload.sample_values["sports"]),
                    "market": score_curve(
                        float(payload.sample_values["market"]), draft["market_score_curve"]
                    ),
                }
            except (KeyError, TypeError, ValueError) as exc:
                raise HTTPException(422, f"Invalid sample values: {exc}") from exc
        else:
            if oracle is None:
                raise HTTPException(503, "No oracle is configured; provide explicit sample scores")
            try:
                scores = oracle.scores(
                    names,
                    market_symbol=draft["market_symbol"],
                    sports_league=draft["sports_league"],
                    sports_team=draft["sports_team"],
                )
            except RuntimeError as exc:
                raise HTTPException(503, str(exc)) from exc
        if any(not 0 <= value <= 1 for value in scores.values()):
            raise HTTPException(422, "Criterion scores must be between 0 and 1")
        weighted = [
            (draft["weather_weight"], scores["weather"]),
            (draft["sports_weight"], scores["sports"]),
            (draft["market_weight"], scores["market"]),
        ]
        return {
            **price_preview(
                draft["qfv_price_min_minor"], draft["qfv_price_max_minor"], weighted
            ),
            "scores": scores,
            "weights": {
                "weather": draft["weather_weight"],
                "sports": draft["sports_weight"],
                "market": draft["market_weight"],
            },
            "market_symbol": draft["market_symbol"],
            "sports_league": draft["sports_league"],
            "sports_team": draft["sports_team"],
        }

    @app.post("/api/v1/bids/{bid_id}/validate", dependencies=[Depends(require_csrf)])
    def validate_bid(bid_id: str, session: Session, conn: Db) -> dict[str, Any]:
        bid = bid_for_user(conn, session["user_id"], bid_id, "manage_campaigns")
        validate_bid_data(conn, bid)
        conn.execute("UPDATE bids SET status='VALIDATED',updated_at=? WHERE id=?", (iso(), bid_id))
        return {"valid": True, "status": "VALIDATED"}

    def shared_contract_components() -> tuple[Any, ...]:
        path = str(settings.shared_package_path)
        if path not in sys.path:
            sys.path.insert(0, path)
        try:
            from tmads_shared.crypto import canonical_sha256, sign_ed25519
            from tmads_shared.models import (
                BillingV1,
                BudgetV1,
                ContractMediaRefV1,
                ContractV1,
                CriterionV1,
                GeographyV1,
                OfflinePolicyV1,
            )
        except ImportError as exc:
            raise HTTPException(503, "TMAds shared protocol package is unavailable") from exc
        return (
            canonical_sha256,
            sign_ed25519,
            BillingV1,
            BudgetV1,
            ContractMediaRefV1,
            ContractV1,
            CriterionV1,
            GeographyV1,
            OfflinePolicyV1,
        )

    def shared_funds_components() -> tuple[Any, ...]:
        path = str(settings.shared_package_path)
        if path not in sys.path:
            sys.path.insert(0, path)
        try:
            from tmads_shared.crypto import sign_ed25519
            from tmads_shared.models import SettlementPostedV1, SpendGrantV1
        except ImportError as exc:
            raise HTTPException(503, "TMAds shared protocol package is unavailable") from exc
        return sign_ed25519, SettlementPostedV1, SpendGrantV1

    def shared_media_components() -> tuple[Any, Any]:
        path = str(settings.shared_package_path)
        if path not in sys.path:
            sys.path.insert(0, path)
        try:
            from tmads_shared.crypto import sign_ed25519
            from tmads_shared.models import MediaManifestV1
        except ImportError as exc:
            raise HTTPException(503, "TMAds shared protocol package is unavailable") from exc
        return MediaManifestV1, sign_ed25519

    def signed_lifecycle(
        bid: sqlite3.Row,
        revision_row: sqlite3.Row,
        event_type: str,
        user_id: str,
        predecessor_event_hash: str | None,
    ) -> tuple[dict[str, Any], str, str, dict[str, Any], str]:
        (
            canonical_sha256,
            sign_ed25519,
            _BillingV1,
            _BudgetV1,
            _ContractMediaRefV1,
            ContractV1,
            _CriterionV1,
            _GeographyV1,
            _OfflinePolicyV1,
        ) = shared_contract_components()
        private_key, key_id = load_or_create_signing_key(settings.secrets_dir)
        event = {
            "schema": "tmads.lifecycle.v1",
            "event_id": uid("life"),
            "bid_id": bid["id"],
            "organization_id": bid["organization_id"],
            "event_type": event_type,
            "active_contract_hash": revision_row["canonical_hash"],
            "predecessor_event_hash": predecessor_event_hash,
            "issued_at": iso(),
            "issued_by": user_id,
            "signer_key_id": key_id,
        }
        event["signature"] = sign_ed25519(event, private_key)
        event_hash = canonical_sha256(event)
        source = ContractV1.model_validate_json(revision_row["contract_json"])
        lifecycle_contract = source.model_copy(
            update={
                "contract_id": uid("ctr"),
                "revision": source.revision + 1,
                "previous_contract_hash": revision_row["canonical_hash"],
                "status": event_type,
                "issued_at": now(),
                "signature": None,
            }
        )
        lifecycle_signature = sign_ed25519(lifecycle_contract, private_key)
        signed_contract = lifecycle_contract.model_copy(
            update={"signature": lifecycle_signature}
        )
        contract_hash = canonical_sha256(signed_contract)
        return (
            event,
            event_hash,
            key_id,
            signed_contract.model_dump(mode="json", by_alias=True, exclude_none=True),
            contract_hash,
        )

    def persist_lifecycle(
        tx: sqlite3.Connection,
        bid: sqlite3.Row,
        revision_row: sqlite3.Row,
        event_type: str,
        user_id: str,
    ) -> dict[str, Any]:
        prior = tx.execute(
            """SELECT canonical_hash FROM lifecycle_events
               WHERE bid_id=? ORDER BY created_at DESC LIMIT 1""",
            (bid["id"],),
        ).fetchone()
        event, event_hash, key_id, distributed, distributed_hash = signed_lifecycle(
            bid,
            revision_row,
            event_type,
            user_id,
            prior["canonical_hash"] if prior else None,
        )
        tx.execute(
            """INSERT INTO lifecycle_events(
               id,bid_id,revision_id,organization_id,event_type,active_contract_hash,
               predecessor_event_hash,event_json,canonical_hash,signature,signer_key_id,
               created_by,created_at,anchor_status,distributed_contract_json,
               distributed_contract_hash)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'PENDING_LEDGER',?,?)""",
            (
                event["event_id"],
                bid["id"],
                revision_row["id"],
                bid["organization_id"],
                event_type,
                revision_row["canonical_hash"],
                event["predecessor_event_hash"],
                normalized_json(event),
                event_hash,
                event["signature"],
                key_id,
                user_id,
                event["issued_at"],
                normalized_json(distributed),
                distributed_hash,
            ),
        )
        return {**event, "canonical_hash": event_hash, "distributor_contract_hash": distributed_hash}

    def process_lifecycle_event(event_id: str, *, update_bid: bool = True) -> dict[str, Any]:
        with db.connect() as connection:
            row = connection.execute(
                """SELECT e.*,r.revision FROM lifecycle_events e
                   JOIN bid_revisions r ON r.id=e.revision_id WHERE e.id=?""",
                (event_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("Lifecycle event was not found")
        if row["anchor_status"] == "FINAL":
            with db.connect() as connection:
                anchor = connection.execute(
                    """SELECT * FROM ledger_anchors
                       WHERE lifecycle_event_id=? ORDER BY anchored_at DESC LIMIT 1""",
                    (event_id,),
                ).fetchone()
            return dict(anchor) if anchor else {}
        if ledger is None:
            raise RuntimeError("Ledger gateway is not configured")
        result = ledger.change_contract_state(
            row["active_contract_hash"], row["event_type"], row["canonical_hash"]
        )
        with db.transaction() as tx:
            current = tx.execute(
                "SELECT anchor_status FROM lifecycle_events WHERE id=?", (event_id,)
            ).fetchone()
            if current["anchor_status"] != "FINAL":
                anchor = persist_anchor(
                    tx,
                    row["revision_id"],
                    row["canonical_hash"],
                    result,
                    anchor_kind="LIFECYCLE",
                    lifecycle_event_id=event_id,
                )
                tx.execute(
                    "UPDATE lifecycle_events SET anchor_status='FINAL' WHERE id=?",
                    (event_id,),
                )
                tx.execute(
                    "UPDATE bid_revisions SET lifecycle_state=? WHERE id=?",
                    (row["event_type"], row["revision_id"]),
                )
                if update_bid:
                    tx.execute(
                        "UPDATE bids SET status=?,updated_at=? WHERE id=?",
                        (row["event_type"], iso(), row["bid_id"]),
                    )
                enqueue(
                    tx,
                    row["organization_id"],
                    "contract",
                    row["distributed_contract_hash"],
                    row["distributed_contract_json"],
                )
                return anchor
        return result

    def process_lifecycle_outbox(limit: int = 25) -> dict[str, int]:
        completed = failed = 0
        with db.connect() as connection:
            rows = connection.execute(
                """SELECT id FROM lifecycle_events WHERE anchor_status='PENDING_LEDGER'
                   ORDER BY created_at LIMIT ?""",
                (limit,),
            ).fetchall()
        for row in rows:
            try:
                process_lifecycle_event(row["id"])
                completed += 1
            except RuntimeError:
                failed += 1
        return {"completed": completed, "failed": failed}

    def process_publication_outbox(
        only_id: str | None = None, limit: int = 25
    ) -> dict[str, int]:
        completed = failed = 0
        for _ in range(limit):
            with db.connect() as connection:
                row = connection.execute(
                    """SELECT p.*,r.bid_id,r.revision,r.contract_json,r.canonical_hash,
                       r.previous_hash,b.campaign_id,b.organization_id AS bid_organization_id
                       FROM publication_outbox p
                       JOIN bid_revisions r ON r.id=p.revision_id
                       JOIN bids b ON b.id=r.bid_id
                       WHERE p.status!='COMPLETE' AND p.next_attempt_at<=?
                       AND (? IS NULL OR p.id=?)
                       ORDER BY p.created_at LIMIT 1""",
                    (iso(), only_id, only_id),
                ).fetchone()
            if row is None:
                break
            try:
                status_value = row["status"]
                if status_value == "PENDING_LEDGER":
                    if ledger is None:
                        raise RuntimeError("Ledger gateway is not configured")
                    result = ledger.publish_contract(
                        row["contract_json"], row["canonical_hash"]
                    )
                    with db.transaction() as tx:
                        persist_anchor(
                            tx,
                            row["revision_id"],
                            row["canonical_hash"],
                            result,
                            anchor_kind="CONTRACT",
                        )
                        tx.execute(
                            """UPDATE publication_outbox
                               SET status='PENDING_SETTLEMENT',attempts=attempts+1,
                               last_error=NULL,next_attempt_at=? WHERE id=?""",
                            (iso(), row["id"]),
                        )
                    status_value = "PENDING_SETTLEMENT"
                if status_value == "PENDING_SETTLEMENT":
                    if settlement is None:
                        raise RuntimeError("Settlement is not configured")
                    settlement.register_contract(
                        row["contract_json"], row["canonical_hash"]
                    )
                    next_status = (
                        "PENDING_SUPERSEDE" if row["revision"] > 1 else "READY_FINALIZE"
                    )
                    with db.transaction() as tx:
                        tx.execute(
                            """UPDATE publication_outbox SET status=?,attempts=attempts+1,
                               last_error=NULL,next_attempt_at=? WHERE id=?""",
                            (next_status, iso(), row["id"]),
                        )
                    status_value = next_status
                if status_value == "PENDING_SUPERSEDE":
                    with db.transaction() as tx:
                        bid = tx.execute(
                            "SELECT * FROM bids WHERE id=?", (row["bid_id"],)
                        ).fetchone()
                        predecessor = tx.execute(
                            """SELECT * FROM bid_revisions
                               WHERE bid_id=? AND canonical_hash=?""",
                            (row["bid_id"], row["previous_hash"]),
                        ).fetchone()
                        event = tx.execute(
                            """SELECT id FROM lifecycle_events
                               WHERE revision_id=? AND event_type='SUPERSEDED'
                               ORDER BY created_at DESC LIMIT 1""",
                            (predecessor["id"],),
                        ).fetchone()
                        if event is None:
                            created = persist_lifecycle(
                                tx,
                                bid,
                                predecessor,
                                "SUPERSEDED",
                                row["requested_by"],
                            )
                            event_id = created["event_id"]
                        else:
                            event_id = event["id"]
                    process_lifecycle_event(event_id, update_bid=False)
                    with db.transaction() as tx:
                        tx.execute(
                            """UPDATE publication_outbox SET status='READY_FINALIZE',
                               attempts=attempts+1,last_error=NULL,next_attempt_at=? WHERE id=?""",
                            (iso(), row["id"]),
                        )
                    status_value = "READY_FINALIZE"
                if status_value == "READY_FINALIZE":
                    published_at = iso()
                    with db.transaction() as tx:
                        tx.execute(
                            """UPDATE bid_revisions SET lifecycle_state='PUBLISHED',
                               published_at=? WHERE id=?""",
                            (published_at, row["revision_id"]),
                        )
                        tx.execute(
                            """UPDATE bids SET current_revision=?,status='PUBLISHED',
                               updated_at=? WHERE id=?""",
                            (row["revision"], published_at, row["bid_id"]),
                        )
                        tx.execute(
                            "UPDATE campaigns SET status='ACTIVE',updated_at=? WHERE id=?",
                            (published_at, row["campaign_id"]),
                        )
                        enqueue(
                            tx,
                            row["organization_id"],
                            "contract",
                            row["canonical_hash"],
                            row["contract_json"],
                        )
                        tx.execute(
                            """UPDATE publication_outbox SET status='COMPLETE',
                               completed_at=?,last_error=NULL WHERE id=?""",
                            (published_at, row["id"]),
                        )
                        audit(
                            tx,
                            row["requested_by"],
                            row["organization_id"],
                            "bid.published",
                            "bid_revision",
                            row["revision_id"],
                            {"canonical_hash": row["canonical_hash"]},
                        )
                    completed += 1
            except RuntimeError as exc:
                failed += 1
                with db.transaction() as tx:
                    current = tx.execute(
                        "SELECT attempts FROM publication_outbox WHERE id=?", (row["id"],)
                    ).fetchone()
                    delay = min(3600, 2 ** min(current["attempts"] + 1, 10))
                    tx.execute(
                        """UPDATE publication_outbox SET attempts=attempts+1,last_error=?,
                           next_attempt_at=? WHERE id=?""",
                        (
                            str(exc)[:500],
                            iso(now() + timedelta(seconds=delay)),
                            row["id"],
                        ),
                    )
            if only_id:
                break
        return {"completed": completed, "failed": failed}

    @app.post("/api/v1/bids/{bid_id}/publish", dependencies=[Depends(require_csrf)])
    def publish_bid(bid_id: str, session: Session, conn: Db) -> dict[str, Any]:
        bid = bid_for_user(conn, session["user_id"], bid_id, "publish_bids")
        pending = conn.execute(
            """SELECT p.id,p.status,r.revision,r.canonical_hash,r.signature
               FROM publication_outbox p JOIN bid_revisions r ON r.id=p.revision_id
               WHERE r.bid_id=? AND p.status!='COMPLETE'
               ORDER BY p.created_at DESC LIMIT 1""",
            (bid_id,),
        ).fetchone()
        if pending is not None:
            conn.execute(
                "UPDATE publication_outbox SET next_attempt_at=? WHERE id=?",
                (iso(), pending["id"]),
            )
            process_publication_outbox(only_id=pending["id"])
            updated = conn.execute(
                "SELECT status,last_error FROM publication_outbox WHERE id=?",
                (pending["id"],),
            ).fetchone()
            anchor = conn.execute(
                """SELECT * FROM ledger_anchors WHERE object_hash=?
                   AND anchor_kind='CONTRACT'""",
                (pending["canonical_hash"],),
            ).fetchone()
            return {
                "bid_id": bid_id,
                "revision": pending["revision"],
                "canonical_hash": pending["canonical_hash"],
                "signature": pending["signature"],
                "publication_status": updated["status"],
                "last_error": updated["last_error"],
                "ledger_anchor": dict(anchor) if anchor else None,
            }
        draft, media_rows = validate_bid_data(conn, bid)
        available = wallet_payload(conn, bid["organization_id"])["balance_minor"]
        if available < draft["total_cap_minor"]:
            raise HTTPException(
                409,
                f"Insufficient Campaign Funds: {available} available, "
                f"{draft['total_cap_minor']} required",
            )
        (
            canonical_sha256,
            sign_ed25519,
            BillingV1,
            BudgetV1,
            ContractMediaRefV1,
            ContractV1,
            CriterionV1,
            GeographyV1,
            OfflinePolicyV1,
        ) = shared_contract_components()
        revision = bid["current_revision"] + 1
        previous = conn.execute(
            """SELECT canonical_hash,contract_json FROM bid_revisions
               WHERE bid_id=? ORDER BY revision DESC LIMIT 1""",
            (bid_id,),
        ).fetchone()
        private_key, key_id = load_or_create_signing_key(settings.secrets_dir)
        contract = ContractV1(
            schema="tmads.contract.v1",
            contract_id=(
                json.loads(previous["contract_json"])["contract_id"]
                if previous
                else uid("ctr")
            ),
            bid_id=bid_id,
            campaign_id=bid["campaign_id"],
            organization_id=bid["organization_id"],
            revision=revision,
            previous_contract_hash=previous["canonical_hash"] if previous else None,
            status="PUBLISHED",
            currency="USD",
            valid_from=draft["valid_from"],
            valid_to=draft["valid_to"],
            budget=BudgetV1(total_cap_minor=draft["total_cap_minor"]),
            billing=BillingV1(
                qfv_price_min_minor=draft["qfv_price_min_minor"],
                qfv_price_max_minor=draft["qfv_price_max_minor"],
                max_qfvs_per_play=draft["max_qfvs_per_play"],
                max_cost_per_play_minor=draft["qfv_price_max_minor"]
                * draft["max_qfvs_per_play"],
            ),
            geography=(
                GeographyV1(required=True, type="device_group", value=draft["geography"])
                if str(draft["geography"]).startswith("shelf-campaign-")
                else GeographyV1(required=True, type="polygon_ref", value=draft["geography"])
            ),
            criteria=tuple(
                CriterionV1.model_validate(item) for item in draft["criteria"]
            ) if draft.get("criteria") is not None else (
                CriterionV1(
                    criterion_id="screen-facing-face",
                    kind="attention.face_count",
                    required=True,
                    weight=0,
                    operator="gte",
                    target=draft["require_face_count"],
                    freshness_seconds=5,
                    unknown="ineligible",
                ),
                CriterionV1(
                    criterion_id="weather",
                    kind="weather.class",
                    required=False,
                    weight=draft["weather_weight"],
                    operator="in",
                    target=["clear", "cloudy", "rain", "snow"],
                    freshness_seconds=1800,
                    unknown="neutral",
                    score_curve={"type": "binary"},
                ),
                CriterionV1(
                    criterion_id=(
                        f"{draft['sports_league'].lower()}-"
                        f"{draft['sports_team'].lower()}-game"
                    ),
                    kind="sports.proximity",
                    required=False,
                    weight=draft["sports_weight"],
                    operator="range",
                    target=[-120, 30],
                    freshness_seconds=3600,
                    unknown="neutral",
                    league=draft["sports_league"],
                    teams_any=(draft["sports_team"],),
                    score_curve={
                        "type": "time_proximity_decay",
                        "half_life": 120,
                    },
                ),
                CriterionV1(
                    criterion_id=(
                        "market-"
                        + re.sub(r"[^a-z0-9]+", "-", draft["market_symbol"].lower()).strip("-")
                    ),
                    kind="market.daily_return",
                    required=False,
                    weight=draft["market_weight"],
                    operator="range",
                    target=[-20, 20],
                    score_curve=draft["market_score_curve"],
                    freshness_seconds=900,
                    unknown="neutral",
                    instrument_symbol=draft["market_symbol"],
                ),
            ),
            media=tuple(
                ContractMediaRefV1(
                    media_id=row["id"],
                    asset_hash=row["derivative_hash"],
                    profile="tmads-portrait-v1",
                )
                for row in media_rows
            ),
            offline_policy=OfflinePolicyV1(
                allowed=True, max_oracle_age_seconds=3600, requires_spend_grant=True
            ),
            ruleset="tmads-evaluator-v1",
            issued_at=now(),
            signer_key_id=key_id,
        )
        signature = sign_ed25519(contract, private_key)
        signed = contract.model_copy(update={"signature": signature})
        contract_hash = canonical_sha256(signed)
        revision_id, published_at = uid("rev"), iso()
        publication_id = uid("publication")
        with db.transaction() as tx:
            tx.execute(
                "INSERT INTO bid_revisions VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    revision_id,
                    bid_id,
                    revision,
                    signed.model_dump_json(by_alias=True, exclude_none=True),
                    contract_hash,
                    previous["canonical_hash"] if previous else None,
                    signature,
                    key_id,
                    published_at,
                    "PENDING_LEDGER",
                ),
            )
            tx.execute(
                """INSERT INTO publication_outbox
                   VALUES(?,?,?,?,'PENDING_LEDGER',0,NULL,?,?,NULL)""",
                (
                    publication_id,
                    revision_id,
                    bid["organization_id"],
                    session["user_id"],
                    published_at,
                    published_at,
                ),
            )
            tx.execute(
                "UPDATE bids SET status='PENDING_LEDGER',updated_at=? WHERE id=?",
                (published_at, bid_id),
            )
        process_publication_outbox(only_id=publication_id)
        publication = conn.execute(
            "SELECT status,last_error FROM publication_outbox WHERE id=?",
            (publication_id,),
        ).fetchone()
        anchor = conn.execute(
            """SELECT * FROM ledger_anchors WHERE object_hash=?
               AND anchor_kind='CONTRACT'""",
            (contract_hash,),
        ).fetchone()
        distribution = conn.execute(
            """SELECT status FROM distributor_outbox
               WHERE object_type='contract' AND object_key=?""",
            (contract_hash,),
        ).fetchone()
        return {
            "bid_id": bid_id,
            "revision": revision,
            "canonical_hash": contract_hash,
            "signature": signature,
            "publication_status": publication["status"],
            "last_error": publication["last_error"],
            "distributor_status": (
                distribution["status"]
                if distribution
                else ("NOT_CONFIGURED" if edge is None else "BLOCKED_ON_PUBLICATION")
            ),
            "ledger_anchor": dict(anchor) if anchor else None,
        }

    @app.post(
        "/api/v1/contracts/{contract_hash}/spend-grants",
        dependencies=[Depends(require_csrf)],
    )
    def issue_spend_grant(
        contract_hash: str,
        payload: SpendGrantIn,
        session: Session,
        conn: Db,
        idempotency_key: Annotated[str | None, Header()] = None,
    ) -> dict[str, Any]:
        if settlement is None:
            raise HTTPException(503, "Settlement is not configured")
        if not idempotency_key or len(idempotency_key) > 255:
            raise HTTPException(400, "A valid Idempotency-Key header is required")
        revision = conn.execute(
            """SELECT r.*,b.organization_id,b.campaign_id,c.budget_cap_minor
               FROM bid_revisions r JOIN bids b ON b.id=r.bid_id
               JOIN campaigns c ON c.id=b.campaign_id WHERE r.canonical_hash=?""",
            (contract_hash,),
        ).fetchone()
        if revision is None:
            raise HTTPException(404, "Published contract not found")
        membership(conn, session["user_id"], revision["organization_id"], "publish_bids")
        contract_data = json.loads(revision["contract_json"])
        contract_cap = int(contract_data["budget"]["total_cap_minor"])
        grant_id = "grant_" + str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"tmads:{revision['organization_id']}:{idempotency_key}",
            )
        )
        sign_ed25519, _SettlementPostedV1, SpendGrantV1 = shared_funds_components()
        private_key, signer_key_id = load_or_create_signing_key(settings.secrets_dir)
        valid_from = max(now(), datetime.fromisoformat(contract_data["valid_from"]))
        valid_to = datetime.fromisoformat(contract_data["valid_to"])
        if valid_from >= valid_to:
            raise HTTPException(409, "Contract is no longer valid")
        try:
            with db.transaction() as tx:
                existing = tx.execute(
                    "SELECT * FROM grant_commitments WHERE grant_id=?",
                    (grant_id,),
                ).fetchone()
                if existing:
                    if (
                        existing["device_id"] != payload.device_id
                        or existing["amount_minor"] != payload.amount_minor
                        or existing["contract_hash"] != contract_hash
                    ):
                        raise ValueError("Idempotency key was already used")
                    grant_json = existing["payload_json"]
                else:
                    sequence = allocate_grant_sequence(tx, payload.device_id)
                    unsigned = SpendGrantV1(
                        schema="tmads.spend-grant.v1",
                        grant_id=grant_id,
                        contract_hash=contract_hash,
                        device_id=payload.device_id,
                        currency=contract_data["currency"],
                        max_amount_minor=payload.amount_minor,
                        valid_from=valid_from,
                        valid_to=valid_to,
                        sequence=sequence,
                        issuer=revision["organization_id"],
                        nonce=secrets.token_urlsafe(24),
                        signer_key_id=signer_key_id,
                    )
                    grant = unsigned.model_copy(
                        update={"signature": sign_ed25519(unsigned, private_key)}
                    )
                    grant_json = grant.model_dump_json(by_alias=True, exclude_none=True)
                    reserve_grant(
                        tx,
                        grant_id=grant_id,
                        organization_id=revision["organization_id"],
                        campaign_id=revision["campaign_id"],
                        contract_hash=contract_hash,
                        device_id=payload.device_id,
                        amount_minor=payload.amount_minor,
                        currency=contract_data["currency"],
                        campaign_cap_minor=revision["budget_cap_minor"],
                        contract_cap_minor=contract_cap,
                        payload_json=grant_json,
                    )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        try:
            registration = settlement.register_grant(grant_json)
        except RuntimeError as exc:
            with db.transaction() as tx:
                tx.execute(
                    """UPDATE grant_commitments SET status='FAILED',
                       settlement_attempts=settlement_attempts+1,
                       settlement_next_attempt_at=?,settlement_last_error=?,updated_at=?
                       WHERE grant_id=?""",
                    (
                        iso(now() + timedelta(seconds=2)),
                        str(exc)[:1000],
                        iso(),
                        grant_id,
                    ),
                )
            raise HTTPException(502, str(exc)) from exc
        with db.transaction() as tx:
            tx.execute(
                "UPDATE grant_commitments SET status='REGISTERED',updated_at=? WHERE grant_id=?",
                (iso(), grant_id),
            )
            enqueue(
                tx,
                revision["organization_id"],
                "spend_grant",
                grant_id,
                grant_json,
            )
        distribution = conn.execute(
            """SELECT id,status FROM distributor_outbox
               WHERE object_type='spend_grant' AND object_key=?""",
            (grant_id,),
        ).fetchone()
        if edge and distribution:
            process_outbox(only_id=distribution["id"])
            distribution = conn.execute(
                "SELECT id,status FROM distributor_outbox WHERE id=?",
                (distribution["id"],),
            ).fetchone()
        return {
            "grant_id": grant_id,
            "sequence": json.loads(grant_json)["sequence"],
            "status": registration["status"],
            "reserved_minor": payload.amount_minor,
            "distributor_status": distribution["status"] if edge else "NOT_CONFIGURED",
        }

    @app.post(
        "/api/v1/spend-grants/{grant_id}/release",
        dependencies=[Depends(require_csrf)],
    )
    def release_spend_grant(
        grant_id: str,
        session: Session,
        conn: Db,
    ) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM grant_commitments WHERE grant_id=?", (grant_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "Spend grant not found")
        membership(conn, session["user_id"], row["organization_id"], "deposit_funds")
        if settlement is None:
            raise HTTPException(503, "Settlement is not configured")
        try:
            confirmation = settlement.confirm_grant_unused(grant_id)
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from exc
        local_remaining = row["amount_minor"] - row["spent_minor"] - row["released_minor"]
        if (
            confirmation["currency"] != row["currency"]
            or confirmation["remaining_minor"] != local_remaining
        ):
            raise HTTPException(409, "Settlement and control-plane grant balances differ")
        try:
            with db.transaction() as tx:
                transaction_id, amount = release_grant(tx, grant_id)
                enqueue(
                    tx,
                    row["organization_id"],
                    "spend_grant_revoke",
                    grant_id,
                )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {
            "grant_id": grant_id,
            "released_minor": amount,
            "transaction_id": transaction_id,
        }

    @app.post("/internal/v1/settlement/events")
    async def settlement_callback(request: Request) -> dict[str, Any]:
        secret = settings.settlement_callback_signing_secret
        if not secret:
            raise HTTPException(503, "Settlement callbacks are not configured")
        raw = await request.body()
        timestamp = request.headers.get("x-tmads-timestamp")
        signature = request.headers.get("x-tmads-signature")
        event_header = request.headers.get("x-tmads-event-id")
        if not timestamp or not signature:
            raise HTTPException(401, "Missing settlement callback authentication")
        try:
            created = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if abs((now() - created).total_seconds()) > settings.settlement_callback_window_seconds:
                raise ValueError("stale callback")
            wire = json.loads(raw)
            _sign, SettlementPostedV1, _SpendGrantV1 = shared_funds_components()
            if wire.get("schema") == "tmads.settlement.anchored.v1":
                from tmads_shared.models import SettlementAnchoredV1

                event = SettlementAnchoredV1.model_validate(wire)
            elif wire.get("schema") == "tmads.settlement.rejected.v1":
                from tmads_shared.models import SettlementRejectedV1

                event = SettlementRejectedV1.model_validate(wire)
            else:
                event = SettlementPostedV1.model_validate(wire)
        except (ValueError, TypeError) as exc:
            raise HTTPException(422, f"Invalid settlement event: {exc}") from exc
        if event_header and event_header != event.event_id:
            raise HTTPException(401, "Settlement event ID header mismatch")
        message = f"{timestamp}.{event.event_id}.".encode() + raw
        expected = "sha256=" + hmac.new(
            secret.encode(), message, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise HTTPException(401, "Invalid settlement callback signature")
        try:
            with db.transaction() as tx:
                if wire.get("schema") == "tmads.settlement.anchored.v1":
                    receipt_row = tx.execute(
                        """SELECT * FROM advertiser_settlement_receipts
                           WHERE receipt_id=?""",
                        (event.receipt_id,),
                    ).fetchone()
                    if receipt_row is None:
                        raise ValueError("anchor update arrived before settlement posting")
                    if (
                        receipt_row["advertiser_organization_id"]
                        != event.advertiser_organization_id
                    ):
                        raise ValueError("anchor update organization mismatch")
                    duplicate = receipt_row["transaction_hash"] is not None
                    tx.execute(
                        """UPDATE advertiser_settlement_receipts SET status='ANCHORED',
                           batch_id=?,merkle_root=?,leaf_hash=?,leaf_index=?,proof_json=?,
                           chain_id=?,transaction_hash=?,block_number=?,contract_address=?,
                           finality=?,anchored_at=? WHERE receipt_id=?""",
                        (
                            event.batch_id,
                            event.merkle_root,
                            event.leaf_hash,
                            event.leaf_index,
                            json.dumps(
                                [
                                    item.model_dump(mode="json")
                                    for item in event.proof
                                ],
                                separators=(",", ":"),
                            ),
                            event.chain_id,
                            event.transaction_hash,
                            event.block_number,
                            event.contract_address,
                            event.finality,
                            iso(event.anchored_at),
                            event.receipt_id,
                        ),
                    )
                    tx.execute(
                        """UPDATE publisher_payables SET status='ANCHORED'
                           WHERE receipt_id=?""",
                        (event.receipt_id,),
                    )
                    transaction_id = event.transaction_hash
                elif wire.get("schema") == "tmads.settlement.rejected.v1":
                    prior = tx.execute(
                        """SELECT receipt_id FROM advertiser_settlement_receipts
                           WHERE receipt_id=? OR event_id=?""",
                        (event.receipt_id, event.event_id),
                    ).fetchone()
                    duplicate = prior is not None
                    if not duplicate:
                        tx.execute(
                            """INSERT INTO advertiser_settlement_receipts(
                               receipt_id,event_id,advertiser_organization_id,campaign_id,
                               contract_hash,contract_revision,grant_id,device_pseudonym,
                               publisher_organization_id,media_hash,execution_at,qfv_count,
                               soft_score,amount_minor,currency,status,reason_code,
                               observation_ids_json,processed_at)
                               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?)""",
                            (
                                event.receipt_id,
                                event.event_id,
                                event.advertiser_organization_id,
                                event.campaign_id,
                                event.contract_hash,
                                event.contract_revision,
                                event.grant_id,
                                event.device_pseudonym,
                                event.publisher_organization_id,
                                event.media_hash,
                                iso(event.execution_at),
                                event.qfv_count,
                                event.soft_score,
                                event.currency,
                                event.status,
                                event.reason_code,
                                json.dumps(event.observation_ids, separators=(",", ":")),
                                iso(),
                            ),
                        )
                    transaction_id = f"rejected:{event.receipt_id}"
                else:
                    transaction_id, duplicate = apply_settlement(
                        tx,
                        event,
                        raw_json=raw.decode(),
                        signature=signature,
                    )
        except (UnicodeDecodeError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc
        return {
            "event_id": event.event_id,
            "transaction_id": transaction_id,
            "duplicate": duplicate,
        }

    @app.get("/api/v1/reports/plays-over-time")
    def fleet_plays_over_time(
        session: Session,
        conn: Db,
        range_key: Annotated[str, Query(alias="range")] = "last_30d",
        period_start: Annotated[datetime | None, Query()] = None,
        period_end: Annotated[datetime | None, Query()] = None,
        campaign_id: Annotated[list[str] | None, Query()] = None,
    ) -> dict[str, Any]:
        try:
            return plays_over_time(
                conn,
                session["user_id"],
                range_key=range_key,
                period_start=period_start,
                period_end=period_end,
                campaign_ids=campaign_id,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/v1/organizations/{organization_id}/reports/campaign-performance")
    def campaign_performance(
        organization_id: str, session: Session, conn: Db
    ) -> list[dict[str, Any]]:
        membership(conn, session["user_id"], organization_id, "view_reports")
        rows = conn.execute(
            """SELECT r.campaign_id,c.name,
               SUM(CASE WHEN r.status IN ('POSTED','ANCHORED') THEN 1 ELSE 0 END) accepted_count,
               SUM(CASE WHEN r.status IN ('REJECTED','QUARANTINED') THEN 1 ELSE 0 END) rejected_count,
               COUNT(*) plays,COALESCE(SUM(r.qfv_count),0) qfv,
               COALESCE(SUM(CASE WHEN r.status IN ('POSTED','ANCHORED')
                            THEN r.amount_minor ELSE 0 END),0) spend_minor,r.currency
               FROM advertiser_settlement_receipts r
               JOIN campaigns c ON c.id=r.campaign_id
               WHERE r.advertiser_organization_id=?
               GROUP BY r.campaign_id,c.name,r.currency ORDER BY c.name""",
            (organization_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @app.get("/api/v1/organizations/{organization_id}/reports/settlements")
    def settlement_history(
        organization_id: str,
        session: Session,
        conn: Db,
        campaign_id: str | None = None,
    ) -> list[dict[str, Any]]:
        membership(conn, session["user_id"], organization_id, "view_reports")
        rows = conn.execute(
            """SELECT receipt_id,campaign_id,contract_hash,contract_revision,grant_id,
               device_pseudonym,media_hash,execution_at,qfv_count,soft_score,
               amount_minor,currency,status,reason_code,observation_ids_json,batch_id,
               merkle_root,leaf_hash,leaf_index,proof_json,chain_id,transaction_hash,
               block_number,contract_address,finality,anchored_at
               FROM advertiser_settlement_receipts
               WHERE advertiser_organization_id=?
               AND (? IS NULL OR campaign_id=?)
               ORDER BY execution_at DESC LIMIT 1000""",
            (organization_id, campaign_id, campaign_id),
        ).fetchall()
        return [
            {
                **dict(row),
                "observation_ids": json.loads(row["observation_ids_json"]),
                "merkle_proof": (
                    json.loads(row["proof_json"]) if row["proof_json"] else None
                ),
            }
            for row in rows
        ]

    @app.get("/internal/v1/reports/publisher-payables")
    def publisher_payables(
        conn: Db,
        publisher_organization_id: str,
        authorization: Annotated[str | None, Header()] = None,
    ) -> list[dict[str, Any]]:
        expected = (
            f"Bearer {settings.settlement_internal_api_key}"
            if settings.settlement_internal_api_key
            else None
        )
        if expected is None or not hmac.compare_digest(authorization or "", expected):
            raise HTTPException(401, "invalid internal credentials")
        rows = conn.execute(
            """SELECT * FROM publisher_payables WHERE publisher_organization_id=?
               ORDER BY created_at DESC LIMIT 1000""",
            (publisher_organization_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @app.get("/api/v1/bids/{bid_id}/ledger-anchors")
    def anchors(bid_id: str, session: Session, conn: Db) -> list[dict[str, Any]]:
        bid_for_user(conn, session["user_id"], bid_id)
        rows = conn.execute(
            """SELECT a.* FROM ledger_anchors a JOIN bid_revisions r ON r.id=a.revision_id
               WHERE r.bid_id=? ORDER BY a.anchored_at DESC""",
            (bid_id,),
        ).fetchall()
        return [{**dict(row), "prototype": bool(row["prototype"])} for row in rows]

    @app.post("/api/v1/bids/{bid_id}/pause", dependencies=[Depends(require_csrf)])
    def pause_bid(bid_id: str, session: Session, conn: Db) -> dict[str, Any]:
        return lifecycle_action(bid_id, "PAUSED", session, conn)

    @app.post("/api/v1/bids/{bid_id}/resume", dependencies=[Depends(require_csrf)])
    def resume_bid(bid_id: str, session: Session, conn: Db) -> dict[str, Any]:
        bid = bid_for_user(conn, session["user_id"], bid_id, "publish_bids")
        if bid["status"] != "PAUSED":
            raise HTTPException(409, "Only a paused bid can resume")
        return lifecycle_action(bid_id, "ACTIVE", session, conn)

    @app.post("/api/v1/bids/{bid_id}/complete", dependencies=[Depends(require_csrf)])
    def complete_bid(bid_id: str, session: Session, conn: Db) -> dict[str, Any]:
        return lifecycle_action(bid_id, "COMPLETED", session, conn)

    @app.post("/api/v1/bids/{bid_id}/cancel", dependencies=[Depends(require_csrf)])
    def cancel_bid(bid_id: str, session: Session, conn: Db) -> dict[str, Any]:
        return lifecycle_action(bid_id, "CANCELLED", session, conn)

    def lifecycle_action(
        bid_id: str, event_type: str, session: sqlite3.Row, conn: sqlite3.Connection
    ) -> dict[str, Any]:
        bid = bid_for_user(conn, session["user_id"], bid_id, "publish_bids")
        if not bid["current_revision"]:
            raise HTTPException(409, "Only a published bid has an active contract")
        if bid["status"] == "PENDING_LEDGER":
            raise HTTPException(409, "Publication must finish before lifecycle changes")
        if bid["status"] in {"CANCELLED", "COMPLETED", "EXPIRED", "EXHAUSTED"}:
            raise HTTPException(409, "Terminal bids cannot transition")
        if bid["status"] == event_type:
            raise HTTPException(409, f"Bid is already {event_type}")
        active = conn.execute(
            "SELECT * FROM bid_revisions WHERE bid_id=? AND revision=?",
            (bid_id, bid["current_revision"]),
        ).fetchone()
        pending = conn.execute(
            """SELECT * FROM lifecycle_events WHERE revision_id=? AND event_type=?
               AND anchor_status='PENDING_LEDGER' ORDER BY created_at DESC LIMIT 1""",
            (active["id"], event_type),
        ).fetchone()
        if pending is None:
            with db.transaction() as tx:
                event = persist_lifecycle(tx, bid, active, event_type, session["user_id"])
                event_id = event["event_id"]
        else:
            event = json.loads(pending["event_json"])
            event["canonical_hash"] = pending["canonical_hash"]
            event_id = pending["id"]
        try:
            anchor = process_lifecycle_event(event_id)
        except RuntimeError as exc:
            raise HTTPException(
                503,
                {
                    "status": "PENDING_LEDGER",
                    "event_id": event_id,
                    "detail": str(exc),
                },
            ) from exc
        with db.transaction() as tx:
            audit(
                tx,
                session["user_id"],
                bid["organization_id"],
                f"bid.{event_type.lower()}",
                "lifecycle_event",
                event["event_id"],
                {"active_contract_hash": active["canonical_hash"]},
            )
        return {
            "status": event_type,
            "event_id": event_id,
            "canonical_hash": event["canonical_hash"],
            "signature": event["signature"],
            "active_contract_hash": active["canonical_hash"],
            "distributor_status": "PENDING" if edge else "NOT_CONFIGURED",
            "ledger_anchor": anchor,
        }

    def campaign_transition(
        campaign_id: str,
        target: str,
        session: sqlite3.Row,
        conn: sqlite3.Connection,
    ) -> dict[str, Any]:
        campaign = campaign_for_user(
            conn, session["user_id"], campaign_id, "manage_campaigns"
        )
        allowed = {
            "PAUSED": {"ACTIVE"},
            "ACTIVE": {"PAUSED"},
            "COMPLETED": {"ACTIVE", "PAUSED"},
            "CANCELLED": {"DRAFT", "ACTIVE", "PAUSED"},
        }
        if campaign["status"] not in allowed[target]:
            raise HTTPException(
                409, f"Campaign cannot transition from {campaign['status']} to {target}"
            )
        event_type = {
            "PAUSED": "PAUSED",
            "ACTIVE": "ACTIVE",
            "COMPLETED": "COMPLETED",
            "CANCELLED": "CANCELLED",
        }[target]
        bids = conn.execute(
            """SELECT id,status,current_revision FROM bids WHERE campaign_id=?
               ORDER BY created_at""",
            (campaign_id,),
        ).fetchall()
        transitioned: list[str] = []
        for bid in bids:
            if not bid["current_revision"]:
                if target == "CANCELLED":
                    conn.execute(
                        "UPDATE bids SET status='CANCELLED',updated_at=? WHERE id=?",
                        (iso(), bid["id"]),
                    )
                continue
            eligible = (
                (target == "PAUSED" and bid["status"] in {"PUBLISHED", "ACTIVE"})
                or (target == "ACTIVE" and bid["status"] == "PAUSED")
                or (
                    target in {"COMPLETED", "CANCELLED"}
                    and bid["status"] not in {
                        "COMPLETED",
                        "CANCELLED",
                        "EXPIRED",
                        "EXHAUSTED",
                    }
                )
            )
            if eligible:
                lifecycle_action(bid["id"], event_type, session, conn)
                transitioned.append(bid["id"])
        conn.execute(
            "UPDATE campaigns SET status=?,updated_at=? WHERE id=?",
            (target, iso(), campaign_id),
        )
        audit(
            conn,
            session["user_id"],
            campaign["organization_id"],
            f"campaign.{target.lower()}",
            "campaign",
            campaign_id,
            {"transitioned_bids": transitioned},
        )
        return {"id": campaign_id, "status": target, "transitioned_bids": transitioned}

    @app.post(
        "/api/v1/campaigns/{campaign_id}/pause",
        dependencies=[Depends(require_csrf)],
    )
    def pause_campaign(
        campaign_id: str, session: Session, conn: Db
    ) -> dict[str, Any]:
        return campaign_transition(campaign_id, "PAUSED", session, conn)

    @app.post(
        "/api/v1/campaigns/{campaign_id}/resume",
        dependencies=[Depends(require_csrf)],
    )
    def resume_campaign(
        campaign_id: str, session: Session, conn: Db
    ) -> dict[str, Any]:
        return campaign_transition(campaign_id, "ACTIVE", session, conn)

    @app.post(
        "/api/v1/campaigns/{campaign_id}/complete",
        dependencies=[Depends(require_csrf)],
    )
    def complete_campaign(
        campaign_id: str, session: Session, conn: Db
    ) -> dict[str, Any]:
        return campaign_transition(campaign_id, "COMPLETED", session, conn)

    @app.post(
        "/api/v1/campaigns/{campaign_id}/cancel",
        dependencies=[Depends(require_csrf)],
    )
    def cancel_campaign(
        campaign_id: str, session: Session, conn: Db
    ) -> dict[str, Any]:
        return campaign_transition(campaign_id, "CANCELLED", session, conn)

    def reconcile_contract_lifecycle() -> dict[str, int]:
        transitioned = failed = 0
        with db.connect() as connection:
            rows = connection.execute(
                """SELECT b.*,c.created_by,
                   COALESCE((SELECT SUM(g.spent_minor) FROM grant_commitments g
                             JOIN bid_revisions br ON br.canonical_hash=g.contract_hash
                             WHERE br.bid_id=b.id),0) AS spent_minor
                   FROM bids b JOIN campaigns c ON c.id=b.campaign_id
                   WHERE b.current_revision>0
                   AND b.status IN ('PUBLISHED','ACTIVE','PAUSED')"""
            ).fetchall()
        for bid in rows:
            draft = json.loads(bid["draft_json"])
            event_type: str | None = None
            if draft["valid_to"] <= iso():
                event_type = "EXPIRED"
            elif bid["spent_minor"] >= draft["total_cap_minor"]:
                event_type = "EXHAUSTED"
            if event_type is None:
                continue
            try:
                with db.transaction() as tx:
                    active = tx.execute(
                        "SELECT * FROM bid_revisions WHERE bid_id=? AND revision=?",
                        (bid["id"], bid["current_revision"]),
                    ).fetchone()
                    existing = tx.execute(
                        """SELECT id FROM lifecycle_events WHERE revision_id=?
                           AND event_type=? ORDER BY created_at DESC LIMIT 1""",
                        (active["id"], event_type),
                    ).fetchone()
                    event_id = (
                        existing["id"]
                        if existing
                        else persist_lifecycle(
                            tx, bid, active, event_type, bid["created_by"]
                        )["event_id"]
                    )
                process_lifecycle_event(event_id)
                transitioned += 1
            except RuntimeError:
                failed += 1
        with db.transaction() as tx:
            expired_campaigns = tx.execute(
                """SELECT c.id FROM campaigns c WHERE c.status IN ('ACTIVE','PAUSED')
                   AND c.end_at<=? AND NOT EXISTS(
                     SELECT 1 FROM bids b WHERE b.campaign_id=c.id
                     AND b.current_revision>0
                     AND b.status NOT IN ('COMPLETED','CANCELLED','EXPIRED','EXHAUSTED'))""",
                (iso(),),
            ).fetchall()
            for campaign in expired_campaigns:
                tx.execute(
                    "UPDATE campaigns SET status='COMPLETED',updated_at=? WHERE id=?",
                    (iso(), campaign["id"]),
                )
        return {"transitioned": transitioned, "failed": failed}

    @app.get("/api/v1/bids/{bid_id}/lifecycle-events")
    def lifecycle_events(bid_id: str, session: Session, conn: Db) -> list[dict[str, Any]]:
        bid_for_user(conn, session["user_id"], bid_id)
        rows = conn.execute(
            """SELECT * FROM lifecycle_events WHERE bid_id=?
               ORDER BY created_at DESC""",
            (bid_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @app.delete("/api/v1/bids/{bid_id}", dependencies=[Depends(require_csrf)])
    def delete_bid(bid_id: str, session: Session, conn: Db) -> dict[str, str]:
        bid = bid_for_user(conn, session["user_id"], bid_id, "manage_campaigns")
        if bid["current_revision"]:
            raise HTTPException(409, "Published bid history cannot be deleted")
        conn.execute("DELETE FROM bids WHERE id=?", (bid_id,))
        return {"status": "deleted"}

    @app.get("/api/v1/distributor/outbox")
    def outbox_status(
        session: Session, conn: Db, organization_id: str
    ) -> list[dict[str, Any]]:
        membership(conn, session["user_id"], organization_id, "view_reports")
        rows = conn.execute(
            """SELECT id,object_type,object_key,status,attempts,last_error,created_at,synced_at
               FROM distributor_outbox WHERE organization_id=?
               ORDER BY created_at DESC""",
            (organization_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    @app.get("/api/v1/operations/sms-outbox")
    def sms_outbox_status(session: Session, conn: Db) -> list[dict[str, Any]]:
        rows = conn.execute(
            """SELECT s.id,s.kind,s.status,s.attempts,s.next_attempt_at,s.last_error,
                      s.created_at,s.sent_at
               FROM sms_outbox s
               WHERE (s.kind='SECURITY' AND s.phone_e164=?)
                  OR (s.kind='INVITATION' AND EXISTS(
                      SELECT 1 FROM organization_invitations i
                      JOIN organization_memberships m ON m.organization_id=i.organization_id
                      WHERE i.id=s.related_id AND m.user_id=?
                      AND m.role IN ('owner','admin')))
               ORDER BY s.created_at DESC LIMIT 500""",
            (session["phone_e164"], session["user_id"]),
        ).fetchall()
        return [dict(row) for row in rows]

    @app.post(
        "/api/v1/operations/sms-outbox/{outbox_id}/retry",
        dependencies=[Depends(require_csrf)],
    )
    def retry_sms_outbox(
        outbox_id: str, session: Session, conn: Db
    ) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM sms_outbox WHERE id=?", (outbox_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "SMS outbox row not found")
        authorized = row["kind"] == "SECURITY" and row["phone_e164"] == session["phone_e164"]
        if row["kind"] == "INVITATION":
            invitation = conn.execute(
                "SELECT organization_id FROM organization_invitations WHERE id=?",
                (row["related_id"],),
            ).fetchone()
            if invitation:
                try:
                    membership(
                        conn,
                        session["user_id"],
                        invitation["organization_id"],
                        "manage_members",
                    )
                    authorized = True
                except HTTPException:
                    authorized = False
        if not authorized:
            raise HTTPException(404, "SMS outbox row not found")
        conn.execute(
            """UPDATE sms_outbox SET status='PENDING',attempts=0,next_attempt_at=?,
               last_error=NULL WHERE id=?""",
            (iso(), outbox_id),
        )
        process_sms_outbox(only_id=outbox_id)
        return dict(
            conn.execute(
                """SELECT id,kind,status,attempts,next_attempt_at,last_error,sent_at
                   FROM sms_outbox WHERE id=?""",
                (outbox_id,),
            ).fetchone()
        )

    @app.post(
        "/api/v1/distributor/outbox/{outbox_id}/retry",
        dependencies=[Depends(require_csrf)],
    )
    def retry_outbox(outbox_id: str, session: Session, conn: Db) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM distributor_outbox WHERE id=?", (outbox_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Outbox row not found")
        membership(conn, session["user_id"], row["organization_id"], "manage_organization")
        if edge is None:
            raise HTTPException(503, "Edge distributor is not configured")
        conn.execute(
            """UPDATE distributor_outbox SET status='PENDING',next_attempt_at=?,last_error=NULL
               WHERE id=?""",
            (iso(), outbox_id),
        )
        process_outbox(only_id=outbox_id)
        updated = conn.execute(
            "SELECT status,attempts,last_error,synced_at FROM distributor_outbox WHERE id=?",
            (outbox_id,),
        ).fetchone()
        return dict(updated)

    @app.get("/", response_class=HTMLResponse)
    def landing(request: Request) -> Response:
        return templates.TemplateResponse(
            request,
            "landing.html",
            {"demo_otp": settings.demo_otp_enabled, "demo_code": settings.demo_otp_code},
        )

    @app.get("/accept-invitation", response_class=HTMLResponse)
    def accept_invitation_page(request: Request, conn: Db) -> Response:
        token = (request.query_params.get("token") or "").strip()
        if not token or len(token) > 200:
            raise HTTPException(404, "Invitation not found")
        cookie = request.cookies.get(settings.cookie_name)
        session = None
        if cookie:
            session = conn.execute(
                """SELECT s.*,u.phone_e164,u.display_name,u.status,u.last_login_at
                   FROM auth_sessions s
                   JOIN users u ON u.id=s.user_id
                   WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.expires_at>?""",
                (digest(cookie), iso()),
            ).fetchone()
        if session is None or session["status"] != "ACTIVE":
            next_path = f"/accept-invitation?token={token}"
            return RedirectResponse(f"/?next={quote(next_path, safe='')}", status_code=303)
        invitation = conn.execute(
            """SELECT i.id,i.invitee_name,i.role,i.expires_at,o.name AS organization_name
               FROM organization_invitations i
               JOIN organizations o ON o.id=i.organization_id
               WHERE i.token_hash=? AND i.phone_e164=? AND i.accepted_at IS NULL
                 AND i.revoked_at IS NULL AND i.expires_at>?""",
            (digest(token), session["phone_e164"], iso()),
        ).fetchone()
        return templates.TemplateResponse(
            request,
            "accept_invitation.html",
            {
                "request": request,
                "session": session,
                "csrf_token": session["csrf_token"],
                "token": token,
                "invitation": dict(invitation) if invitation else None,
            },
        )

    def ui_context(request: Request, session: sqlite3.Row, conn: sqlite3.Connection) -> dict[str, Any]:
        orgs = list_organizations(session, conn)
        return {
            "request": request,
            "session": session,
            "csrf_token": session["csrf_token"],
            "organizations": orgs,
            "funding_provider": funding.name,
            "is_supply_admin": is_supply_admin_phone(
                session["phone_e164"],
                settings.supply_admin_phone,
            ),
            "carto_basemap_key": settings.carto_basemap_key,
        }

    def supply_aliases(conn: sqlite3.Connection) -> dict[str, str]:
        return {
            row["device_id"]: row["display_name"]
            for row in conn.execute(
                "SELECT device_id, display_name FROM device_aliases"
            )
        }

    def supply_payload(
        period_start: datetime | None = None,
        period_end: datetime | None = None,
        device_id: str | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        try:
            payload = (
                supply.detail(device_id, period_start, period_end)
                if device_id is not None
                else supply.overview(period_start, period_end)
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        aliases = supply_aliases(conn) if conn is not None else {}
        return apply_operator_labels(payload, aliases)

    @app.get("/api/v1/supply/devices")
    def supply_devices(
        session: SupplySession,
        conn: Db,
        period_start: Annotated[datetime | None, Query()] = None,
        period_end: Annotated[datetime | None, Query()] = None,
    ) -> dict[str, Any]:
        del session
        return supply_payload(period_start, period_end, conn=conn)

    @app.get("/api/v1/supply/devices/{device_id}")
    def supply_device(
        device_id: str,
        session: SupplySession,
        conn: Db,
        period_start: Annotated[datetime | None, Query()] = None,
        period_end: Annotated[datetime | None, Query()] = None,
    ) -> dict[str, Any]:
        del session
        return supply_payload(period_start, period_end, device_id, conn)

    @app.patch(
        "/api/v1/supply/devices/{device_id}",
        dependencies=[Depends(require_csrf)],
    )
    def rename_supply_device(
        device_id: str,
        body: DeviceAliasIn,
        session: SupplySession,
        conn: Db,
    ) -> dict[str, Any]:
        payload = supply_payload(device_id=device_id, conn=conn)
        if not payload.get("device"):
            raise HTTPException(404, "Device not found")
        name = body.display_name.strip()
        if not name:
            raise HTTPException(422, "display_name is required")
        conn.execute(
            """INSERT INTO device_aliases(device_id, display_name, updated_at)
               VALUES(?,?,?)
               ON CONFLICT(device_id) DO UPDATE SET
                 display_name=excluded.display_name,
                 updated_at=excluded.updated_at""",
            (device_id, name, iso()),
        )
        audit(
            conn,
            session["user_id"],
            None,
            "supply.device.renamed",
            "supply_device",
            device_id,
            {"display_name": name},
        )
        return {"device_id": device_id, "display_name": name, "label": name}

    @app.get("/api/v1/supply/devices/{device_id}/positions")
    def supply_device_positions(
        device_id: str, session: SupplySession, conn: Db
    ) -> dict[str, Any]:
        del session
        payload = supply_payload(device_id=device_id, conn=conn)
        return {
            "device_id": device_id,
            "positions": payload["positions"],
            "source": payload["sources"]["positions"],
        }

    @app.get("/api/v1/supply/devices/{device_id}/sync-history")
    def supply_device_sync_history(
        device_id: str, session: SupplySession, conn: Db
    ) -> dict[str, Any]:
        del session
        payload = supply_payload(device_id=device_id, conn=conn)
        return {
            "device_id": device_id,
            "sync_history": payload["sync_history"],
            "source": payload["sources"]["sync_history"],
        }

    @app.get("/api/v1/supply/firmware")
    def supply_firmware_list(request: Request, session: SupplySession) -> dict[str, Any]:
        del session
        return supply_firmware.list_artifacts(request.app.state.edge_distributor)

    @app.post(
        "/api/v1/supply/firmware",
        dependencies=[Depends(require_csrf)],
    )
    async def supply_firmware_upload(
        request: Request,
        session: SupplySession,
        conn: Db,
    ) -> dict[str, Any]:
        del session
        return await supply_firmware.upload_artifact(
            request, conn, request.app.state.edge_distributor
        )

    @app.post(
        "/api/v1/supply/devices/{device_id}/firmware-assignment",
        dependencies=[Depends(require_csrf)],
    )
    def supply_firmware_assign(
        device_id: str,
        body: supply_firmware.FirmwareAssignIn,
        request: Request,
        session: SupplySession,
        conn: Db,
    ) -> dict[str, Any]:
        del session
        return supply_firmware.assign_to_device(
            device_id, body, conn, request.app.state.edge_distributor
        )

    @app.get("/api/v1/supply/devices/{device_id}/movement-alerts")
    def supply_device_movement_alerts(
        device_id: str, session: SupplySession, conn: Db
    ) -> dict[str, Any]:
        del session
        payload = supply_payload(device_id=device_id, conn=conn)
        return {
            "device_id": device_id,
            "movement_alerts": payload["movement_alerts"],
            "source": payload["sources"]["movement_alerts"],
        }

    @app.get("/supply", response_class=HTMLResponse)
    def supply_page(
        request: Request,
        session: SupplySession,
        conn: Db,
        period_start: Annotated[datetime | None, Query()] = None,
        period_end: Annotated[datetime | None, Query()] = None,
    ) -> Response:
        context = ui_context(request, session, conn)
        context["supply"] = supply_payload(period_start, period_end, conn=conn)
        audit(
            conn,
            session["user_id"],
            None,
            "supply.dashboard.viewed",
            "supply_fleet",
            "fleet",
        )
        return templates.TemplateResponse(request, "supply.html", context)

    @app.get("/supply/devices/{device_id}", response_class=HTMLResponse)
    def supply_device_page(
        device_id: str,
        request: Request,
        session: SupplySession,
        conn: Db,
        period_start: Annotated[datetime | None, Query()] = None,
        period_end: Annotated[datetime | None, Query()] = None,
    ) -> Response:
        context = ui_context(request, session, conn)
        context["supply"] = supply_payload(
            period_start, period_end, device_id, conn
        )
        try:
            context["shelf_install_url"] = shelf_install_url(
                settings,
                create_shelf_token(device_id, settings),
            )
        except RuntimeError:
            context["shelf_install_url"] = None
        audit(
            conn,
            session["user_id"],
            None,
            "supply.device.viewed",
            "supply_device",
            device_id,
        )
        return templates.TemplateResponse(request, "supply_device.html", context)

    @app.get("/dashboard", response_class=HTMLResponse)
    def dashboard(request: Request, session: Session, conn: Db) -> Response:
        context = ui_context(request, session, conn)
        context["campaigns"] = list_campaigns(session, conn)
        wallets = [
            wallet_payload(conn, organization["id"])
            for organization in context["organizations"]
        ]
        context["wallet_totals"] = {
            "available_minor": sum(item["available_minor"] for item in wallets),
            "reserved_minor": sum(item["reserved_minor"] for item in wallets),
            "spent_minor": sum(item["spent_minor"] for item in wallets),
        }
        context["events"] = [
            dict(row)
            for row in conn.execute(
                """SELECT a.* FROM audit_events a JOIN organization_memberships m
                   ON m.organization_id=a.organization_id WHERE m.user_id=?
                   ORDER BY a.created_at DESC LIMIT 8""",
                (session["user_id"],),
            ).fetchall()
        ]
        return templates.TemplateResponse(request, "dashboard.html", context)

    @app.get("/funds", response_class=HTMLResponse)
    def funds_page(request: Request, session: Session, conn: Db) -> Response:
        context = ui_context(request, session, conn)
        context["wallets"] = [
            {
                "organization": organization,
                "wallet": wallet_payload(conn, organization["id"]),
            }
            for organization in context["organizations"]
        ]
        return templates.TemplateResponse(request, "funds.html", context)

    @app.get("/profile", response_class=HTMLResponse)
    def profile_page(request: Request, session: Session, conn: Db) -> Response:
        context = ui_context(request, session, conn)
        context["user"] = dict(
            conn.execute(
                "SELECT id,phone_e164,display_name,created_at FROM users WHERE id=?",
                (session["user_id"],),
            ).fetchone()
        )
        return templates.TemplateResponse(request, "profile.html", context)

    @app.get("/organizations", response_class=HTMLResponse)
    def organizations_page(request: Request, session: Session, conn: Db) -> Response:
        context = ui_context(request, session, conn)
        context["invitations"] = pending_invitations(session, conn)
        return templates.TemplateResponse(request, "organizations.html", context)

    @app.get("/organizations/{organization_id}", response_class=HTMLResponse)
    def organization_page(
        organization_id: str, request: Request, session: Session, conn: Db
    ) -> Response:
        member = membership(conn, session["user_id"], organization_id)
        context = ui_context(request, session, conn)
        context["organization"] = get_organization(organization_id, session, conn)
        context["membership"] = dict(member)
        context["members"] = list_members(organization_id, session, conn)
        context["pending_invites"] = [
            dict(row)
            for row in conn.execute(
                """SELECT id,phone_e164,invitee_name,role,invited_at,expires_at,
                          delivery_status,delivery_error
                   FROM organization_invitations
                   WHERE organization_id=? AND accepted_at IS NULL
                     AND revoked_at IS NULL AND expires_at>?
                   ORDER BY invited_at DESC""",
                (organization_id, iso()),
            )
        ]
        return templates.TemplateResponse(request, "organization_detail.html", context)

    @app.get("/organizations/{organization_id}/wallet", response_class=HTMLResponse)
    def wallet_page(
        organization_id: str, request: Request, session: Session, conn: Db
    ) -> Response:
        membership(conn, session["user_id"], organization_id, "view_wallet")
        funding_credited = False
        checkout_session_id = request.query_params.get("session_id")
        if request.query_params.get("funding") == "success" and checkout_session_id:
            funding_credited = confirm_stripe_checkout_return(
                organization_id, checkout_session_id
            )
        context = ui_context(request, session, conn)
        context["wallet"] = wallet(organization_id, session, conn)
        context["transactions"] = wallet_transactions(
            organization_id, session, conn, limit=100, offset=0
        )
        context["grants"] = wallet_grants(organization_id, session, conn)
        context["pending_deposits"] = wallet_deposits(
            organization_id, session, conn, limit=10
        )
        context["funding_return"] = request.query_params.get("funding")
        context["funding_credited"] = funding_credited
        return templates.TemplateResponse(request, "wallet.html", context)

    @app.get("/organizations/{organization_id}/reports", response_class=HTMLResponse)
    def reports_page(
        organization_id: str, request: Request, session: Session, conn: Db
    ) -> Response:
        context = ui_context(request, session, conn)
        context["organization"] = get_organization(organization_id, session, conn)
        context["performance"] = campaign_performance(organization_id, session, conn)
        context["settlements"] = settlement_history(
            organization_id, session, conn
        )
        return templates.TemplateResponse(request, "reports.html", context)

    @app.get("/campaigns", response_class=HTMLResponse)
    def campaigns_page(request: Request, session: Session, conn: Db) -> Response:
        context = ui_context(request, session, conn)
        context["campaigns"] = list_campaigns(session, conn)
        return templates.TemplateResponse(request, "campaigns.html", context)

    @app.get("/campaigns/{campaign_id}", response_class=HTMLResponse)
    def campaign_page(campaign_id: str, request: Request, session: Session, conn: Db) -> Response:
        campaign = campaign_for_user(conn, session["user_id"], campaign_id)
        context = ui_context(request, session, conn)
        context["campaign"] = dict(campaign)
        context["media"] = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM media_assets WHERE campaign_id=? ORDER BY created_at DESC",
                (campaign_id,),
            ).fetchall()
        ]
        context["bids"] = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM bids WHERE campaign_id=? ORDER BY created_at DESC", (campaign_id,)
            ).fetchall()
        ]
        context["sports_teams"] = SPORTS_TEAMS
        context["product_upcs"] = list_campaign_upcs(conn, campaign_id)
        context["shelf_device_group"] = shelf_device_group(campaign_id)
        return templates.TemplateResponse(request, "campaign_detail.html", context)

    @app.get("/bids/{bid_id}", response_class=HTMLResponse)
    def bid_page(bid_id: str, request: Request, session: Session, conn: Db) -> Response:
        context = ui_context(request, session, conn)
        context["bid"] = get_bid(bid_id, session, conn)
        context["anchors"] = anchors(bid_id, session, conn)
        context["lifecycle"] = lifecycle_events(bid_id, session, conn)
        context["sports_teams"] = SPORTS_TEAMS
        return templates.TemplateResponse(request, "bid_detail.html", context)

    @app.get("/audit", response_class=HTMLResponse)
    def audit_page(request: Request, session: Session, conn: Db) -> Response:
        context = ui_context(request, session, conn)
        context["events"] = [
            dict(row)
            for row in conn.execute(
                """SELECT a.* FROM audit_events a JOIN organization_memberships m
                   ON m.organization_id=a.organization_id WHERE m.user_id=?
                   ORDER BY a.created_at DESC LIMIT 200""",
                (session["user_id"],),
            ).fetchall()
        ]
        return templates.TemplateResponse(request, "audit.html", context)

    class CampaignUpcIn(BaseModel):
        model_config = ConfigDict(extra="forbid")
        upc: str = Field(min_length=8, max_length=14)
        label: str | None = Field(default=None, max_length=120)

    class ShelfDeployIn(BaseModel):
        model_config = ConfigDict(extra="forbid")
        upc: str = Field(min_length=8, max_length=14)

    @app.get("/shelf/{token}", response_class=HTMLResponse)
    def shelf_install_page(token: str, request: Request) -> Response:
        device_id = verify_shelf_token(token, settings)
        return templates.TemplateResponse(
            request,
            "shelf_install.html",
            {"request": request, "token": token, "device_id": device_id},
        )

    @app.post("/api/v1/shelf/{token}/diagnostics")
    async def shelf_scanner_diagnostics(token: str, request: Request) -> dict[str, bool]:
        device_id = verify_shelf_token(token, settings)
        try:
            payload = await request.json()
        except ValueError:
            payload = {"event": "invalid-json"}
        log.info(
            "shelf_scanner_diagnostic device=%s payload=%s",
            device_id,
            json.dumps(payload, separators=(",", ":"), default=str)[:2000],
        )
        return {"ok": True}

    @app.post("/api/v1/shelf/{token}/decode-barcode")
    async def shelf_decode_barcode(token: str, image: UploadFile = File(...)) -> dict[str, str]:
        device_id = verify_shelf_token(token, settings)
        content = await image.read()
        if not content or len(content) > settings.max_upload_bytes:
            raise HTTPException(413, "Barcode photo exceeds upload limit")
        try:
            import io
            import zxingcpp
            from PIL import Image, ImageOps
            with Image.open(io.BytesIO(content)) as source:
                source = ImageOps.exif_transpose(source).convert("RGB")
                width, height = source.size
                candidates = [source, ImageOps.autocontrast(source.convert("L")), source.crop((0, height // 4, width, 3 * height // 4)), source.crop((0, height // 2, width, height))]
                for candidate in candidates:
                    results = zxingcpp.read_barcodes(candidate, formats=zxingcpp.BarcodeFormat.AllLinear, try_rotate=True, try_downscale=True, try_invert=True)
                    for result in results:
                        digits = "".join(ch for ch in result.text if ch.isdigit())
                        if len(digits) == 13 and digits.startswith("0"):
                            digits = digits[1:]
                        if len(digits) in {8, 11, 12, 13, 14}:
                            log.info("shelf_barcode_decoded device=%s dimensions=%sx%s format=%s", device_id, width, height, result.format)
                            return {"upc": normalize_upc(digits)}
        except (OSError, ValueError) as exc:
            raise HTTPException(422, "Barcode photo could not be decoded") from exc
        raise HTTPException(422, "No UPC barcode was found in the photo")

    @app.get("/api/v1/shelf/{token}")
    def shelf_install_status(token: str, conn: Db) -> dict[str, Any]:
        device_id = verify_shelf_token(token, settings)
        if supply.fleet is None:
            raise HTTPException(503, "Fleet status is unavailable")
        device = supply.fleet.device(device_id)
        return {
            "device_id": device_id,
            "status": device.get("status"),
            "online": device_is_awake(device),
            "last_heartbeat_at": device.get("last_heartbeat_at"),
            "battery_telemetry": device.get("battery_telemetry"),
        }

    @app.post("/api/v1/shelf/{token}/lookup")
    def shelf_lookup_upc(token: str, payload: ShelfDeployIn, conn: Db) -> dict[str, Any]:
        verify_shelf_token(token, settings)
        upc = normalize_upc(payload.upc)
        campaign = lookup_campaign_for_upc(conn, upc)
        if campaign is None:
            raise HTTPException(404, "No active campaign is mapped to that product code.")
        contract = published_contract_for_campaign(conn, campaign["id"])
        if contract is None:
            raise HTTPException(
                409,
                "This product is mapped to a campaign that has not been published yet.",
            )
        return {
            "upc": upc,
            "campaign_id": campaign["id"],
            "campaign_name": campaign["name"],
            "upc_label": campaign["upc_label"],
            "contract_hash": contract["canonical_hash"],
        }

    @app.post("/api/v1/shelf/{token}/deploy")
    async def shelf_deploy(
        token: str,
        conn: Db,
        upc: Annotated[str, Form()],
        compliance_photo: UploadFile | None = File(default=None),
    ) -> dict[str, Any]:
        device_id = verify_shelf_token(token, settings)
        normalized = normalize_upc(upc)
        campaign = lookup_campaign_for_upc(conn, normalized)
        if campaign is None:
            raise HTTPException(404, "No active campaign is mapped to that product code.")
        contract = published_contract_for_campaign(conn, campaign["id"])
        if contract is None:
            raise HTTPException(
                409,
                "This product is mapped to a campaign that has not been published yet.",
            )
        compliance_path = None
        if compliance_photo is not None and compliance_photo.filename:
            content = await compliance_photo.read()
            if len(content) > settings.max_upload_bytes:
                raise HTTPException(413, "Compliance photo exceeds upload limit")
            compliance_path = save_compliance_photo(
                settings,
                f"pending_{secrets.token_hex(8)}",
                content,
            )
        result = deploy_campaign_to_device(
            conn,
            settings=settings,
            fleet=supply.fleet,
            edge=edge,
            device_id=device_id,
            campaign_id=campaign["id"],
            contract_hash=contract["canonical_hash"],
            upc=normalized,
            token=token,
            compliance_photo_path=compliance_path,
        )
        if compliance_path:
            final_path = save_compliance_photo(
                settings,
                result["deployment_id"],
                Path(compliance_path).read_bytes(),
            )
            Path(compliance_path).unlink(missing_ok=True)
            conn.execute(
                "UPDATE shelf_deployments SET compliance_photo_path=? WHERE id=?",
                (final_path, result["deployment_id"]),
            )
            result["compliance_photo_saved"] = True
        return result

    @app.get("/api/v1/campaigns/{campaign_id}/product-upcs")
    def list_product_upcs(campaign_id: str, session: Session, conn: Db) -> list[dict[str, Any]]:
        campaign = campaign_for_user(conn, session["user_id"], campaign_id)
        membership(conn, session["user_id"], campaign["organization_id"], "view_reports")
        return list_campaign_upcs(conn, campaign_id)

    @app.post(
        "/api/v1/campaigns/{campaign_id}/product-upcs",
        dependencies=[Depends(require_csrf)],
    )
    def add_product_upc(
        campaign_id: str,
        payload: CampaignUpcIn,
        session: Session,
        conn: Db,
    ) -> dict[str, Any]:
        campaign = campaign_for_user(conn, session["user_id"], campaign_id)
        membership(conn, session["user_id"], campaign["organization_id"], "manage_campaigns")
        upc = normalize_upc(payload.upc)
        timestamp = iso()
        conn.execute(
            """INSERT INTO campaign_product_upcs(campaign_id, upc, label, created_at)
               VALUES (?,?,?,?)
               ON CONFLICT(campaign_id, upc) DO UPDATE SET
                 label=excluded.label""",
            (campaign_id, upc, payload.label, timestamp),
        )
        return {"campaign_id": campaign_id, "upc": upc, "label": payload.label}

    @app.delete(
        "/api/v1/campaigns/{campaign_id}/product-upcs/{upc}",
        dependencies=[Depends(require_csrf)],
    )
    def remove_product_upc(
        campaign_id: str,
        upc: str,
        session: Session,
        conn: Db,
    ) -> dict[str, str]:
        campaign = campaign_for_user(conn, session["user_id"], campaign_id)
        membership(conn, session["user_id"], campaign["organization_id"], "manage_campaigns")
        normalized = normalize_upc(upc)
        conn.execute(
            "DELETE FROM campaign_product_upcs WHERE campaign_id=? AND upc=?",
            (campaign_id, normalized),
        )
        return {"status": "removed", "upc": normalized}

    @app.post(
        "/api/v1/campaigns/{campaign_id}/shelf-bids",
        dependencies=[Depends(require_csrf)],
    )
    def create_shelf_bid(
        campaign_id: str,
        session: Session,
        conn: Db,
    ) -> dict[str, Any]:
        campaign = campaign_for_user(conn, session["user_id"], campaign_id)
        membership(conn, session["user_id"], campaign["organization_id"], "manage_campaigns")
        ready = conn.execute(
            """SELECT id FROM media_assets
               WHERE campaign_id=? AND processing_status='READY' LIMIT 1""",
            (campaign_id,),
        ).fetchone()
        if ready is None:
            raise HTTPException(409, "Upload and validate at least one creative first.")
        upcs = list_campaign_upcs(conn, campaign_id)
        if not upcs:
            raise HTTPException(409, "Add at least one product UPC before creating a shelf bid.")
        media_ids = [
            row["id"]
            for row in conn.execute(
                """SELECT id FROM media_assets
                   WHERE campaign_id=? AND processing_status='READY'""",
                (campaign_id,),
            ).fetchall()
        ]
        payload = BidIn(
            valid_from=datetime.fromisoformat(campaign["start_at"].replace("Z", "+00:00")),
            valid_to=datetime.fromisoformat(campaign["end_at"].replace("Z", "+00:00")),
            total_cap_minor=int(campaign["budget_cap_minor"]),
            geography=shelf_device_group(campaign_id),
            qfv_price_min_minor=5,
            qfv_price_max_minor=25,
            max_qfvs_per_play=8,
            require_face_count=1,
            weather_weight=0,
            sports_weight=0,
            market_weight=0,
            criteria=[
                {
                    "criterion_id": "screen-facing-face",
                    "kind": "attention.face_count",
                    "required": True,
                    "weight": 0,
                    "operator": "gte",
                    "target": 1,
                    "freshness_seconds": 5,
                    "unknown": "ineligible",
                }
            ],
            media_ids=media_ids,
        )
        return create_bid(campaign_id, payload, session, conn)

    @app.post(
        "/api/v1/supply/devices/{device_id}/shelf-token",
        dependencies=[Depends(require_csrf)],
    )
    def issue_device_shelf_token(
        device_id: str,
        session: SupplySession,
        conn: Db,
    ) -> dict[str, str]:
        if supply.fleet is None:
            raise HTTPException(503, "Fleet service is unavailable")
        supply.fleet.device(device_id)
        token = create_shelf_token(device_id, settings)
        url = shelf_install_url(settings, token)
        audit(
            conn,
            session["user_id"],
            None,
            "supply.shelf_token.issued",
            "supply_device",
            device_id,
        )
        return {"token": token, "url": url}

    @app.exception_handler(HTTPException)
    async def html_auth_redirect(request: Request, exc: HTTPException) -> Response:
        public_prefixes = ("/api/", "/internal/", "/shelf/")
        if exc.status_code == 401 and not request.url.path.startswith(public_prefixes):
            return RedirectResponse("/", 303)
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)

    return app


app = create_app()
