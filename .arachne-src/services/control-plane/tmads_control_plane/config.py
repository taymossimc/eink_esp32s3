from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .security import normalize_supply_admin_phones


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TMADS_", env_file=".env", extra="ignore")

    environment: str = "production"
    data_dir: Path = Path("/var/lib/tmads")
    media_dir: Path = Path("/var/lib/tmads/media")
    secrets_dir: Path = Path("/run/secrets/tmads")
    database_path: Path | None = None
    cookie_secure: bool = True
    cookie_name: str = "tmads_session"
    session_hours: int = Field(default=12, ge=1, le=168)
    otp_ttl_seconds: int = Field(default=300, ge=60, le=900)
    otp_resend_seconds: int = Field(default=60, ge=10)
    otp_attempt_cap: int = Field(default=5, ge=2, le=10)
    otp_hourly_cap: int = Field(default=6, ge=1, le=30)
    otp_ip_start_hourly_cap: int = Field(default=30, ge=1, le=1000)
    otp_ip_verify_hourly_cap: int = Field(default=100, ge=1, le=5000)
    trusted_proxy_cidrs: str = ""
    ip_hash_secret: str | None = Field(default=None, min_length=32)
    invitation_ttl_hours: int = Field(default=72, ge=1, le=720)
    invitation_base_url: str | None = None
    demo_otp_enabled: bool = False
    demo_otp_code: str = Field(default="246810", pattern=r"^\d{6}$")
    prototype_otp_bypass_enabled: bool = False
    prototype_otp_bypass_code: str = Field(default="123123", pattern=r"^\d{6}$")
    sms_provider: str = "disabled"
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_verify_service_sid: str | None = None
    twilio_phone_number: str | None = None
    sms_delivery_max_attempts: int = Field(default=8, ge=1, le=100)
    funding_provider: str = "disabled"
    stripe_secret_key: str | None = None
    stripe_webhook_secret: str | None = None
    stripe_success_url: str | None = None
    stripe_cancel_url: str | None = None
    max_upload_bytes: int = Field(default=50 * 1024 * 1024, ge=1024)
    media_quarantine_dir: Path | None = None
    ffmpeg_timeout_seconds: int = Field(default=180, ge=10, le=900)
    allowed_hosts: str = "localhost,127.0.0.1,testserver"
    shared_package_path: Path = Path("/opt/tmads/shared")
    oracle_base_url: str | None = None
    oracle_internal_token: str | None = Field(default=None, min_length=24)
    oracle_timeout_seconds: float = Field(default=3.0, ge=0.1, le=30)
    edge_distributor_base_url: str | None = None
    edge_distributor_internal_bearer: str | None = Field(default=None, min_length=24)
    edge_distributor_timeout_seconds: float = Field(default=5.0, ge=0.1, le=30)
    outbox_worker_interval_seconds: float = Field(default=5.0, ge=0.2, le=300)
    ledger_gateway_base_url: str | None = None
    ledger_gateway_token: str | None = Field(default=None, min_length=32)
    ledger_gateway_timeout_seconds: float = Field(default=15.0, ge=0.1, le=120)
    ledger_network: str = Field(default="besu-qbft", min_length=1, max_length=100)
    settlement_base_url: str | None = None
    settlement_internal_api_key: str | None = Field(default=None, min_length=24)
    settlement_callback_signing_secret: str | None = Field(default=None, min_length=32)
    settlement_timeout_seconds: float = Field(default=5.0, ge=0.1, le=30)
    settlement_callback_window_seconds: int = Field(default=300, ge=30, le=3600)
    operator_api_token: str | None = Field(default=None, min_length=32)
    carto_basemap_key: str | None = None
    supply_admin_phone: str | None = None

    @field_validator("supply_admin_phone")
    @classmethod
    def normalize_supply_admin_phone(cls, value: str | None) -> str | None:
        return normalize_supply_admin_phones(value)

    @property
    def supply_admin_phones(self) -> frozenset[str]:
        if not self.supply_admin_phone:
            return frozenset()
        return frozenset(self.supply_admin_phone.split(","))

    @property
    def db_path(self) -> Path:
        return self.database_path or self.data_dir / "control-plane.sqlite3"

    @property
    def quarantine_dir(self) -> Path:
        return self.media_quarantine_dir or self.data_dir / "quarantine"


@lru_cache
def get_settings() -> Settings:
    return Settings()
