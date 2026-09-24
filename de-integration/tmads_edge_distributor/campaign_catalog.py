"""Campaign preload catalog with orientation-aware rendered derivatives.

The control plane pushes the full campaign inventory (including DRAFT
campaigns) here. Devices fetch a signed catalog that lists, for every
campaign asset, the SHA-256 hash and byte length of the device-ready JPEG
derivative so they can preload and verify content on local USB storage.
Display eligibility is still governed exclusively by the targeted contract
manifest; DRAFT assets are cacheable but never displayable.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, HTTPException, Request
from PIL import Image, ImageOps

HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

VARIANT_DIMENSIONS: dict[str, tuple[int, int]] = {
    "portrait": (1200, 1600),
    "landscape": (1600, 1200),
}

CATALOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS campaign_catalog (
    campaign_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rendered_media (
    asset_hash TEXT NOT NULL,
    variant TEXT NOT NULL CHECK(variant IN ('portrait','landscape')),
    rendered_hash TEXT NOT NULL,
    byte_length INTEGER NOT NULL,
    rendered_at TEXT NOT NULL,
    source_mtime REAL NOT NULL,
    PRIMARY KEY (asset_hash, variant)
);
"""


def ensure_catalog_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(CATALOG_SCHEMA)


def media_source_path(object_root: Path, asset_hash: str) -> Path:
    digest = asset_hash.removeprefix("sha256:")
    return object_root / digest[:2] / digest


def rendered_media_path(object_root: Path, asset_hash: str, variant: str) -> Path:
    digest = asset_hash.removeprefix("sha256:")
    return object_root / "rendered" / f"{digest}-{variant}.jpg"


def _hash_bytes_of(path: Path) -> tuple[str, int]:
    hasher = hashlib.sha256()
    length = 0
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            length += len(block)
            hasher.update(block)
    return f"sha256:{hasher.hexdigest()}", length


def render_variant(source: Path, target: Path, variant: str) -> None:
    dimensions = VARIANT_DIMENSIONS[variant]
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".tmp")
    try:
        with Image.open(source) as image:
            image = ImageOps.exif_transpose(image).convert("RGB")
            image = ImageOps.fit(
                image,
                dimensions,
                method=Image.Resampling.LANCZOS,
                centering=(0.5, 0.5),
            )
            image.save(temporary, "JPEG", quality=90, optimize=True, progressive=False)
        temporary.replace(target)
    except (OSError, ValueError):
        temporary.unlink(missing_ok=True)
        raise


def store_prerendered(
    db: Any,
    object_root: Path,
    asset_hash: str,
    variant: str,
    source: Path,
) -> dict[str, Any]:
    """Install an externally produced derivative (pushed by the control plane)."""
    if variant not in VARIANT_DIMENSIONS:
        raise ValueError("invalid variant")
    target = rendered_media_path(object_root, asset_hash, variant)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    source.replace(target)
    rendered_hash, byte_length = _hash_bytes_of(target)
    original = media_source_path(object_root, asset_hash)
    source_mtime = original.stat().st_mtime if original.is_file() else 0.0
    db.execute(
        """INSERT INTO rendered_media(
               asset_hash,variant,rendered_hash,byte_length,rendered_at,source_mtime
           ) VALUES(?,?,?,?,?,?)
           ON CONFLICT(asset_hash,variant) DO UPDATE SET
           rendered_hash=excluded.rendered_hash,
           byte_length=excluded.byte_length,
           rendered_at=excluded.rendered_at,
           source_mtime=excluded.source_mtime""",
        (
            asset_hash,
            variant,
            rendered_hash,
            byte_length,
            datetime.now(UTC).isoformat(),
            source_mtime,
        ),
    )
    return {
        "asset_hash": asset_hash,
        "variant": variant,
        "rendered_hash": rendered_hash,
        "byte_length": byte_length,
    }


def ensure_rendered(
    db: Any,
    object_root: Path,
    asset_hash: str,
    variant: str,
) -> dict[str, Any] | None:
    """Return derivative metadata, generating the file locally when needed."""
    if variant not in VARIANT_DIMENSIONS or not HASH_RE.fullmatch(asset_hash):
        return None
    source = media_source_path(object_root, asset_hash)
    if not source.is_file():
        return None
    target = rendered_media_path(object_root, asset_hash, variant)
    row = db.one(
        "SELECT * FROM rendered_media WHERE asset_hash=? AND variant=?",
        (asset_hash, variant),
    )
    source_mtime = source.stat().st_mtime
    if (
        row is not None
        and target.is_file()
        and float(row["source_mtime"]) >= source_mtime
    ):
        return {
            "rendered_hash": row["rendered_hash"],
            "byte_length": int(row["byte_length"]),
        }
    try:
        render_variant(source, target, variant)
    except (OSError, ValueError):
        return None
    rendered_hash, byte_length = _hash_bytes_of(target)
    db.execute(
        """INSERT INTO rendered_media(
               asset_hash,variant,rendered_hash,byte_length,rendered_at,source_mtime
           ) VALUES(?,?,?,?,?,?)
           ON CONFLICT(asset_hash,variant) DO UPDATE SET
           rendered_hash=excluded.rendered_hash,
           byte_length=excluded.byte_length,
           rendered_at=excluded.rendered_at,
           source_mtime=excluded.source_mtime""",
        (
            asset_hash,
            variant,
            rendered_hash,
            byte_length,
            datetime.now(UTC).isoformat(),
            source_mtime,
        ),
    )
    return {"rendered_hash": rendered_hash, "byte_length": byte_length}


def register_campaign_catalog_routes(
    app: Any,
    *,
    db: Any,
    settings: Any,
    authenticate_device: Callable[..., sqlite3.Row],
    signed_object: Callable[..., dict[str, Any]],
    internal_auth: Callable[..., None],
    iso: Callable[..., str],
    dump_json: Callable[[dict[str, Any]], str],
) -> None:
    @app.put(
        "/internal/v1/campaign-catalog",
        dependencies=[Depends(internal_auth)],
    )
    def internal_put_campaign_catalog(value: dict[str, Any]) -> dict[str, Any]:
        campaigns = value.get("campaigns")
        if not isinstance(campaigns, list):
            raise HTTPException(422, "campaigns list is required")
        timestamp = iso()
        seen: set[str] = set()
        with db.transaction(immediate=True) as connection:
            for item in campaigns:
                campaign_id = str(item.get("campaign_id") or "").strip()
                if not campaign_id:
                    continue
                seen.add(campaign_id)
                connection.execute(
                    """INSERT INTO campaign_catalog(campaign_id,payload_json,updated_at)
                       VALUES(?,?,?)
                       ON CONFLICT(campaign_id) DO UPDATE SET
                       payload_json=excluded.payload_json,
                       updated_at=excluded.updated_at""",
                    (campaign_id, dump_json(item), timestamp),
                )
            if value.get("replace", True):
                existing = connection.execute(
                    "SELECT campaign_id FROM campaign_catalog"
                ).fetchall()
                for row in existing:
                    if row["campaign_id"] not in seen:
                        connection.execute(
                            "DELETE FROM campaign_catalog WHERE campaign_id=?",
                            (row["campaign_id"],),
                        )
        return {"stored": len(seen), "updated_at": timestamp}

    @app.get(
        "/internal/v1/campaign-catalog",
        dependencies=[Depends(internal_auth)],
    )
    def internal_get_campaign_catalog() -> dict[str, Any]:
        rows = db.all("SELECT * FROM campaign_catalog ORDER BY campaign_id")
        return {
            "campaigns": [db.json(row) for row in rows],
            "updated_at": max((row["updated_at"] for row in rows), default=None),
        }

    @app.put(
        "/internal/v1/media/{asset_hash}/derivative/{variant}",
        dependencies=[Depends(internal_auth)],
    )
    async def internal_put_media_derivative(
        asset_hash: str, variant: str, request: Request
    ) -> dict[str, Any]:
        if variant not in VARIANT_DIMENSIONS:
            raise HTTPException(422, "variant must be portrait or landscape")
        if not HASH_RE.fullmatch(asset_hash):
            raise HTTPException(422, "invalid asset hash")
        descriptor, temporary_name = tempfile.mkstemp(
            prefix="derivative-", dir=settings.object_root
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        length = 0
        try:
            with temporary.open("wb") as handle:
                async for block in request.stream():
                    length += len(block)
                    if length > settings.max_media_bytes:
                        raise HTTPException(413, "derivative exceeds size limit")
                    handle.write(block)
            if length == 0:
                raise HTTPException(422, "derivative body is empty")
            return store_prerendered(
                db, settings.object_root, asset_hash, variant, temporary
            )
        finally:
            temporary.unlink(missing_ok=True)

    @app.get("/api/v1/edge/{device_id}/catalog")
    async def device_campaign_catalog(
        device_id: str,
        _: sqlite3.Row = Depends(authenticate_device),
    ) -> dict[str, Any]:
        campaigns_out: list[dict[str, Any]] = []
        for row in db.all("SELECT * FROM campaign_catalog ORDER BY campaign_id"):
            item = db.json(row)
            assets_out: list[dict[str, Any]] = []
            for media in item.get("media", []):
                asset_hash = str(media.get("asset_hash") or "")
                if not HASH_RE.fullmatch(asset_hash):
                    continue
                manifest = db.one(
                    "SELECT asset_hash FROM media_manifests WHERE asset_hash=?",
                    (asset_hash,),
                )
                if manifest is None:
                    continue
                variants: dict[str, Any] = {}
                for variant in VARIANT_DIMENSIONS:
                    meta = ensure_rendered(
                        db, settings.object_root, asset_hash, variant
                    )
                    if meta is None:
                        continue
                    variants[variant] = {
                        "rendered_hash": meta["rendered_hash"],
                        "bytes": meta["byte_length"],
                        "url": (
                            f"/api/v1/edge/{device_id}/media-rendered/"
                            f"{asset_hash}?variant={variant}"
                        ),
                    }
                if not variants:
                    continue
                assets_out.append(
                    {
                        "asset_hash": asset_hash,
                        "media_id": media.get("media_id"),
                        "media_status": media.get("status"),
                        "variants": variants,
                    }
                )
            campaigns_out.append(
                {
                    "campaign_id": item.get("campaign_id"),
                    "name": item.get("name"),
                    "status": item.get("status"),
                    "upcs": item.get("upcs", []),
                    "valid_from": item.get("start_at"),
                    "valid_to": item.get("end_at"),
                    "assets": assets_out,
                }
            )
        return signed_object(
            app,
            {
                "schema": "tmads.eink-campaign-catalog.v1",
                "device_id": device_id,
                "generated_at": iso(),
                "campaigns": campaigns_out,
            },
        )
