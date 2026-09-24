from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TMADS_EDGE_", env_file=".env", extra="ignore"
    )

    environment: str = "production"
    data_dir: Path = Path("/var/lib/tmads-edge")
    database_path: Path | None = None
    object_root: Path = Path("/var/lib/tmads-edge/objects")
    import_root: Path = Path("/var/lib/tmads-edge/import")
    signing_key_path: Path | None = None
    internal_bearer_token: str = Field(min_length=24)
    settlement_base_url: str | None = None
    settlement_internal_token: str | None = Field(default=None, min_length=24)
    settlement_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    settlement_sync_interval_seconds: float = Field(default=5.0, ge=0.2, le=300)
    settlement_sync_max_attempts: int = Field(default=20, ge=1, le=100)
    request_window_seconds: int = Field(default=300, ge=30, le=900)
    manifest_lookahead_seconds: int = Field(default=86_400, ge=0, le=604_800)
    max_request_bytes: int = Field(default=2 * 1024 * 1024, ge=1024)
    max_media_bytes: int = Field(default=100 * 1024 * 1024, ge=1024)
    max_firmware_bytes: int = Field(default=6 * 1024 * 1024, ge=1024)
    rate_limit_per_minute: int = Field(default=120, ge=1, le=100_000)
    registration_rate_limit_per_minute: int = Field(default=10, ge=1, le=10_000)
    registration_freshness_seconds: int = Field(default=300, ge=30, le=900)
    heartbeat_interval_seconds: int = Field(default=60, ge=10, le=86_400)
    cellular_usage_retention_days: int = Field(default=90, ge=2, le=3650)
    cellular_usage_max_rows_per_device: int = Field(
        default=10_000, ge=1, le=1_000_000
    )
    location_history_retention_days: int = Field(default=90, ge=1, le=3650)
    location_history_max_points: int = Field(default=10_000, ge=2, le=1_000_000)
    movement_threshold_meters: float = Field(default=100.0, gt=0, le=100_000)
    movement_confirmation_radius_meters: float = Field(default=100.0, gt=0, le=100_000)
    movement_jitter_meters: float = Field(default=10.0, ge=0, le=1000)
    location_min_confidence: float = Field(default=0.8, ge=0, le=1)
    location_max_accuracy_meters: float = Field(default=50.0, gt=0, le=10_000)
    attested_organization_id: str = Field(default="attested-fleet", min_length=1, max_length=200)
    manufacturer_root_public_keys: dict[str, str] = Field(default_factory=dict)
    allowed_hosts: str = "localhost,127.0.0.1,testserver"

    @field_validator("manufacturer_root_public_keys", mode="before")
    @classmethod
    def parse_manufacturer_roots(cls, value: object) -> object:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError("manufacturer roots must be a JSON object") from exc
        if not isinstance(value, dict):
            raise ValueError("manufacturer roots must be a JSON object")
        return value

    @property
    def db_path(self) -> Path:
        return self.database_path or self.data_dir / "edge.sqlite3"

    @property
    def key_path(self) -> Path:
        return self.signing_key_path or self.data_dir / "edge-signing-ed25519.pem"

    @property
    def allowed_host_list(self) -> list[str]:
        return [item.strip() for item in self.allowed_hosts.split(",") if item.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
