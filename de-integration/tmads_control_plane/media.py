from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from PIL import Image, ImageOps

IMAGE_EXTENSIONS = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jpe": "image/jpeg",
    ".jfif": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".dib": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".avif": "image/avif",
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".ico": "image/x-icon",
}
VIDEO_EXTENSIONS = {
    ".mp4": {"video/mp4"},
    ".m4v": {"video/mp4"},
    ".mov": {"video/quicktime"},
    ".webm": {"video/webm"},
}
# Vector/document formats rasterized server-side before normalization.
DOCUMENT_EXTENSIONS = {
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
}
ALLOWED = {
    **{suffix: ("image", {mime}) for suffix, mime in IMAGE_EXTENSIONS.items()},
    **{suffix: ("video", mimes) for suffix, mimes in VIDEO_EXTENSIONS.items()},
    **{suffix: ("image", {mime}) for suffix, mime in DOCUMENT_EXTENSIONS.items()},
}

# Rasterization target for vector/document sources; large enough for the
# 1600 px device derivatives while bounding decode work.
RASTER_TARGET_PX = 2400


@dataclass(frozen=True)
class ProcessedMedia:
    derivative_hash: str
    width: int
    height: int
    duration_ms: int | None
    derivative_name: str


class MediaProcessor(Protocol):
    def process(self, source: Path, kind: str, media_id: str) -> ProcessedMedia: ...


class LocalMediaProcessor:
    def __init__(self, media_dir: Path, timeout_seconds: int = 180):
        self.media_dir = media_dir
        self.timeout_seconds = timeout_seconds
        Image.MAX_IMAGE_PIXELS = 50_000_000

    def process(self, source: Path, kind: str, media_id: str) -> ProcessedMedia:
        return self._image(source, media_id) if kind == "image" else self._video(source, media_id)

    def _image(self, source: Path, media_id: str) -> ProcessedMedia:
        destination = self.media_dir / f"{media_id}-portrait.webp"
        raster: Path | None = None
        if source.suffix.lower() in {".svg", ".pdf"}:
            raster = self._rasterize(source, media_id)
            source = raster
        try:
            with Image.open(source) as image:
                image.verify()
            with Image.open(source) as image:
                image = ImageOps.exif_transpose(image)
                if image.mode not in {"RGB", "RGBA"}:
                    image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
                width, height = image.size
                if width < 1 or height < 1:
                    raise ValueError("Image has no pixel dimensions")
                image.save(destination, "WEBP", quality=88, method=6)
        finally:
            if raster is not None:
                raster.unlink(missing_ok=True)
        return ProcessedMedia(
            f"sha256:{hashlib.sha256(destination.read_bytes()).hexdigest()}",
            width,
            height,
            None,
            destination.name,
        )

    def _rasterize(self, source: Path, media_id: str) -> Path:
        """Rasterize SVG or first-page PDF to a bounded PNG for normalization."""
        target = source.with_name(f"{media_id}-raster.png")
        suffix = source.suffix.lower()
        if source.stat().st_size > 20 * 1024 * 1024:
            raise ValueError("Vector/document uploads are limited to 20 MB")
        if suffix == ".svg":
            import cairosvg

            cairosvg.svg2png(
                url=str(source),
                write_to=str(target),
                output_width=RASTER_TARGET_PX,
            )
        elif suffix == ".pdf":
            import pypdfium2 as pdfium

            document = pdfium.PdfDocument(str(source))
            try:
                if len(document) < 1:
                    raise ValueError("PDF has no pages")
                page = document[0]
                width = page.get_width() or 1
                scale = max(1.0, RASTER_TARGET_PX / float(width))
                bitmap = page.render(scale=min(scale, 8.0))
                image = bitmap.to_pil().convert("RGB")
                image.save(target, "PNG")
            finally:
                document.close()
        else:
            raise ValueError("Unsupported document format")
        if not target.is_file() or target.stat().st_size == 0:
            raise ValueError("Rasterization produced no output")
        return target

    def _video(self, source: Path, media_id: str) -> ProcessedMedia:
        ffprobe, ffmpeg = shutil.which("ffprobe"), shutil.which("ffmpeg")
        if not ffprobe or not ffmpeg:
            raise ValueError("Video processing unavailable: ffprobe and ffmpeg are required")
        probe = subprocess.run(  # noqa: S603 - executable resolved by shutil.which; argv is fixed
            [
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height:format=duration",
                "-of",
                "json",
                str(source),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            **_resource_limits(),
        )
        metadata = json.loads(probe.stdout)
        stream = metadata["streams"][0]
        duration = float(metadata["format"]["duration"])
        if duration <= 0 or duration > 30:
            raise ValueError("Video duration must be 30 seconds or less")
        if int(stream["height"]) <= int(stream["width"]):
            raise ValueError("Video must be portrait orientation")
        destination = self.media_dir / f"{media_id}-portrait.mp4"
        subprocess.run(  # noqa: S603 - executable resolved by shutil.which; no shell is used
            [
                ffmpeg,
                "-nostdin",
                "-v",
                "error",
                "-i",
                str(source),
                "-vf",
                "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                "-an",
                "-y",
                str(destination),
            ],
            check=True,
            capture_output=True,
            timeout=self.timeout_seconds,
            **_resource_limits(),
        )
        return ProcessedMedia(
            f"sha256:{hashlib.sha256(destination.read_bytes()).hexdigest()}",
            1080,
            1920,
            int(duration * 1000),
            destination.name,
        )
def _resource_limits() -> dict[str, object]:
    if sys.platform == "win32":
        return {}

    def limit_child() -> None:
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (240, 240))
        resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
        resource.setrlimit(resource.RLIMIT_FSIZE, (512 * 1024**2, 512 * 1024**2))

    return {"preexec_fn": limit_child}  # noqa: S604 - fixed resource limits, no shell


def _detect_media(header: bytes) -> str | None:
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if header.startswith(b"BM"):
        return "image/bmp"
    if header.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        brand = header[8:12]
        if brand in {b"avif", b"avis", b"mif1", b"heic", b"heix", b"heif"}:
            return "image/avif" if brand in {b"avif", b"avis"} else "image/heif"
        return "video/quicktime" if brand == b"qt  " else "video/mp4"
    if header.startswith(b"\x1a\x45\xdf\xa3"):
        return "video/webm"
    return None


def _looks_like_svg(header: bytes) -> bool:
    text = header.lstrip(b"\xef\xbb\xbf \t\r\n")
    return text.startswith((b"<?xml", b"<svg", b"<!DOCTYPE svg", b"<!--"))


def sniff_media(filename: str, header: bytes) -> tuple[str, str]:
    """Classify uploads from extension and signature. Images are accepted loosely."""
    suffix = Path(filename).suffix.lower()
    if suffix in DOCUMENT_EXTENSIONS:
        if suffix == ".pdf" and not header.startswith(b"%PDF"):
            raise ValueError("File signature does not match its extension")
        if suffix == ".svg" and not _looks_like_svg(header):
            raise ValueError("File signature does not match its extension")
        return "image", DOCUMENT_EXTENSIONS[suffix]
    detected = _detect_media(header)
    if detected and detected.startswith("image/"):
        return "image", detected
    if suffix in IMAGE_EXTENSIONS:
        return "image", detected or IMAGE_EXTENSIONS[suffix]
    if suffix not in VIDEO_EXTENSIONS:
        raise ValueError("Unsupported file extension")
    mime_types = VIDEO_EXTENSIONS[suffix]
    if detected not in mime_types:
        raise ValueError("File signature does not match its extension")
    return "video", detected


def classify(filename: str, content_type: str | None, header: bytes | None = None) -> str:
    """Compatibility wrapper; callers handling uploads must supply inspected bytes."""
    if header is None:
        raise ValueError("Server-side file signature is required")
    kind, detected = sniff_media(filename, header)
    if kind != "image" and content_type and content_type.lower() != detected:
        raise ValueError("Declared MIME type does not match file signature")
    return kind
