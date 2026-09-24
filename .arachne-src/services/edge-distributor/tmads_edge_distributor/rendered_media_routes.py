"""Device-ready JPEG derivatives for constrained e-ink clients."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, HTTPException
from fastapi.responses import FileResponse
from PIL import Image, ImageOps

HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _media_path(root: Path, asset_hash: str) -> Path:
    digest = asset_hash.removeprefix("sha256:")
    return root / digest[:2] / digest


def _rendered_path(root: Path, asset_hash: str) -> Path:
    digest = asset_hash.removeprefix("sha256:")
    return root / "rendered" / f"{digest}.jpg"


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
        _: sqlite3.Row = Depends(authenticate_device),
    ) -> FileResponse:
        del device_id
        if not HASH_RE.fullmatch(asset_hash):
            raise HTTPException(404, "media not found")
        row = db.one(
            "SELECT * FROM media_manifests WHERE asset_hash=?", (asset_hash,)
        )
        if row is None or not row["object_path"]:
            raise HTTPException(404, "media not found")
        source = _media_path(settings.object_root, asset_hash)
        if not source.is_file():
            raise HTTPException(404, "media not found")

        target = _rendered_path(settings.object_root, asset_hash)
        if not target.is_file() or target.stat().st_mtime < source.stat().st_mtime:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".tmp")
            try:
                with Image.open(source) as image:
                    image = ImageOps.exif_transpose(image).convert("RGB")
                    image = ImageOps.fit(
                        image,
                        (1200, 1600),
                        method=Image.Resampling.LANCZOS,
                        centering=(0.5, 0.5),
                    )
                    image.save(
                        temporary,
                        "JPEG",
                        quality=90,
                        optimize=True,
                        progressive=False,
                    )
                temporary.replace(target)
            except (OSError, ValueError) as exc:
                temporary.unlink(missing_ok=True)
                raise HTTPException(409, "media rendering failed") from exc

        return FileResponse(
            target,
            media_type="image/jpeg",
            filename=f"{asset_hash.removeprefix('sha256:')}.jpg",
        )
