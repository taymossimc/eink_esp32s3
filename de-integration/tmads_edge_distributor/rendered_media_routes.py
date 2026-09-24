"""Device-ready JPEG derivatives for constrained e-ink clients."""

from __future__ import annotations

import sqlite3
from typing import Any, Callable

from fastapi import Depends, HTTPException, Query
from fastapi.responses import FileResponse

from .campaign_catalog import (
    HASH_RE,
    VARIANT_DIMENSIONS,
    ensure_rendered,
    rendered_media_path,
)


def register_rendered_media_routes(
    app: Any,
    *,
    db: Any,
    settings: Any,
    authenticate_device: Callable[..., sqlite3.Row],
) -> None:
    @app.get("/api/v1/edge/{device_id}/media-rendered/{asset_hash}")
    async def serve_rendered_media(
        device_id: str,
        asset_hash: str,
        variant: str = Query(default="portrait"),
        _: sqlite3.Row = Depends(authenticate_device),
    ) -> FileResponse:
        del device_id
        if variant not in VARIANT_DIMENSIONS:
            raise HTTPException(422, "variant must be portrait or landscape")
        if not HASH_RE.fullmatch(asset_hash):
            raise HTTPException(404, "media not found")
        row = db.one(
            "SELECT * FROM media_manifests WHERE asset_hash=?", (asset_hash,)
        )
        if row is None or not row["object_path"]:
            raise HTTPException(404, "media not found")
        meta = ensure_rendered(db, settings.object_root, asset_hash, variant)
        if meta is None:
            raise HTTPException(409, "media rendering failed")
        target = rendered_media_path(settings.object_root, asset_hash, variant)
        if not target.is_file():
            raise HTTPException(409, "media rendering failed")
        digest = meta["rendered_hash"].removeprefix("sha256:")
        return FileResponse(
            target,
            media_type="image/jpeg",
            filename=f"{asset_hash.removeprefix('sha256:')}-{variant}.jpg",
            headers={
                "Digest": f"sha-256={digest}",
                "X-Rendered-Hash": meta["rendered_hash"],
                "X-Rendered-Bytes": str(meta["byte_length"]),
            },
        )
