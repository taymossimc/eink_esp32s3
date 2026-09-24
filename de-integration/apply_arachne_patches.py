#!/usr/bin/env python3
"""Apply Digital Engine integration patches on Arachne."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path("/mnt/dockerprojects/digital_engine")
EDGE = ROOT / "services/edge-distributor/tmads_edge_distributor"
CP = ROOT / "services/control-plane/tmads_control_plane"
CONTROL = ROOT / "services/control-plane"
DEPLOY = ROOT / "deploy/arachne"
SRC = Path(__file__).resolve().parent


def copy_modules() -> None:
    for name in (
        "firmware_store.py",
        "firmware_routes.py",
        "rendered_media_routes.py",
        "campaign_catalog.py",
    ):
        shutil.copy2(SRC / "tmads_edge_distributor" / name, EDGE / name)
    shutil.copy2(SRC / "tmads_control_plane" / "supply_firmware.py", CP / "supply_firmware.py")
    shutil.copy2(SRC / "tmads_control_plane" / "campaign_sync.py", CP / "campaign_sync.py")
    shutil.copy2(SRC / "tmads_control_plane" / "media.py", CP / "media.py")
    shutil.copy2(
        SRC / "tmads_control_plane/templates/supply_device.html",
        CONTROL / "templates/supply_device.html",
    )
    shutil.copy2(
        SRC / "tmads_control_plane/static/zxing-browser.min.js",
        CONTROL / "static/zxing-browser.min.js",
    )
    shutil.copy2(
        SRC / "tmads_control_plane/static/shelf.js",
        CONTROL / "static/shelf.js",
    )


def patch_db() -> None:
    path = EDGE / "db.py"
    text = path.read_text(encoding="utf-8")
    if "ensure_firmware_schema" not in text:
        text = text.replace(
            "import json\nimport sqlite3",
            "import json\nimport sqlite3\n\nfrom .firmware_store import ensure_schema as ensure_firmware_schema",
        )
        text = text.replace(
            "connection.executescript(POST_MIGRATION_SCHEMA)",
            "connection.executescript(POST_MIGRATION_SCHEMA)\n"
            "            ensure_firmware_schema(connection)",
        )
        path.write_text(text, encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    if "ensure_catalog_schema" not in text:
        text = text.replace(
            "from .firmware_store import ensure_schema as ensure_firmware_schema",
            "from .campaign_catalog import ensure_catalog_schema\n"
            "from .firmware_store import ensure_schema as ensure_firmware_schema",
        )
        text = text.replace(
            "            ensure_firmware_schema(connection)",
            "            ensure_firmware_schema(connection)\n"
            "            ensure_catalog_schema(connection)",
        )
        path.write_text(text, encoding="utf-8")


def patch_edge_main() -> None:
    path = EDGE / "main.py"
    text = path.read_text(encoding="utf-8")
    if "register_firmware_routes" not in text:
        text = text.replace(
            "from .security import (",
            "from .firmware_routes import register_firmware_routes\nfrom .firmware_store import build_firmware_offer, next_wake_at, profile_wake_interval_seconds\nfrom .security import (",
        )
        hello_old = '''        return signed_object(
            app,
            {
                "schema": "tmads.edge-hello.v1",
                "device_id": device_id,
                "organization_id": row["organization_id"],
                "server_time": iso(),
                "latest_cursor": format_cursor(db.max_sequence()),
                "manifest_url": f"/api/v1/edge/{device_id}/manifest",
            },
        )'''
        hello_new = '''        profile = json.loads(row["profile_json"])
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
        )'''
        if hello_old not in text:
            raise RuntimeError("hello block not found in main.py")
        text = text.replace(hello_old, hello_new)
        insert = '''
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

    return app'''
        if "\n    return app" not in text:
            raise RuntimeError("return app not found")
        text = text.replace("\n    return app", insert, 1)
        path.write_text(text, encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    if "register_rendered_media_routes" not in text:
        text = text.replace(
            "from .firmware_store import build_firmware_offer, next_wake_at, profile_wake_interval_seconds",
            "from .firmware_store import build_firmware_offer, next_wake_at, profile_wake_interval_seconds\n"
            "from .rendered_media_routes import register_rendered_media_routes",
        )
        call = '''    register_rendered_media_routes(
        app,
        db=db,
        settings=settings,
        authenticate_device=authenticate_device,
    )

    return app'''
        if "\n    return app" not in text:
            raise RuntimeError("return app not found for rendered media")
        text = text.replace("\n    return app", call, 1)
        path.write_text(text, encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    if "register_campaign_catalog_routes" not in text:
        text = text.replace(
            "from .rendered_media_routes import register_rendered_media_routes",
            "from .campaign_catalog import register_campaign_catalog_routes\n"
            "from .rendered_media_routes import register_rendered_media_routes",
        )
        call = '''    register_campaign_catalog_routes(
        app,
        db=db,
        settings=settings,
        authenticate_device=authenticate_device,
        signed_object=signed_object,
        internal_auth=internal_auth,
        iso=iso,
        dump_json=dump_json,
    )

    return app'''
        if "\n    return app" not in text:
            raise RuntimeError("return app not found for campaign catalog")
        text = text.replace("\n    return app", call, 1)
        path.write_text(text, encoding="utf-8")
    text = path.read_text(encoding="utf-8")
    if "status_report_fields" not in text:
        anchor = (
            "from .firmware_store import build_firmware_offer, "
            "next_wake_at, profile_wake_interval_seconds"
        )
        if anchor not in text:
            raise RuntimeError("firmware_store import anchor not found")
        text = text.replace(
            anchor,
            "from .firmware_store import ("
            "build_firmware_offer, next_wake_at, profile_schedule, "
            "profile_wake_interval_seconds, status_report_fields)",
        )
        payload_anchor = '''            **cellular_fleet_fields(row["device_id"]),
            **battery_fleet_fields(row["device_id"]),
        }'''
        if payload_anchor not in text:
            raise RuntimeError("fleet payload anchor not found")
        text = text.replace(
            payload_anchor,
            '''            **cellular_fleet_fields(row["device_id"]),
            **battery_fleet_fields(row["device_id"]),
            **status_report_fields(
                db, row["device_id"], settings.heartbeat_interval_seconds
            ),
        }''',
            1,
        )
        detail_anchor = '''        if detail:
            payload["profile"] = json.loads(row["profile_json"])'''
        if detail_anchor not in text:
            raise RuntimeError("fleet detail anchor not found")
        text = text.replace(
            detail_anchor,
            '''        if detail:
            payload["profile"] = json.loads(row["profile_json"])
            payload["schedule"] = profile_schedule(
                payload["profile"], settings.heartbeat_interval_seconds
            )
            payload["next_wake_at"] = next_wake_at(
                payload["profile"], settings.heartbeat_interval_seconds, now
            )''',
            1,
        )
        path.write_text(text, encoding="utf-8")


def patch_edge_signed_payload() -> None:
    """Expose exact RFC 8785 bytes so constrained devices can verify replies."""
    path = EDGE / "main.py"
    text = path.read_text(encoding="utf-8")
    if "from tmads_shared.crypto import signing_payload" not in text:
        text = text.replace(
            "from .config import Settings, get_settings",
            "from tmads_shared.crypto import signing_payload\n\n"
            "from .config import Settings, get_settings",
            1,
        )
    if '"_signed_payload"' not in text:
        old = """def signed_object(app: FastAPI, payload: dict[str, Any]) -> dict[str, Any]:
    value = {**payload, "signer_key_id": app.state.signer_key_id}
    return {**value, "signature": sign_ed25519(value, app.state.signing_key)}"""
        new = """def signed_object(app: FastAPI, payload: dict[str, Any]) -> dict[str, Any]:
    value = {**payload, "signer_key_id": app.state.signer_key_id}
    canonical = signing_payload(value)
    encoded = base64.urlsafe_b64encode(canonical).rstrip(b"=").decode("ascii")
    return {
        **value,
        "signature": sign_ed25519(value, app.state.signing_key),
        "_signed_payload": encoded,
    }"""
        if old not in text:
            raise RuntimeError("signed_object anchor not found")
        text = text.replace(old, new, 1)
    path.write_text(text, encoding="utf-8")


def patch_gateway() -> None:
    path = DEPLOY / "public-gateway.conf.template"
    text = path.read_text(encoding="utf-8")
    additions = [
        '        "~^POST:/device/api/v1/edge/[A-Za-z0-9][A-Za-z0-9._:-]{0,199}/check-in$" 1;',
        '        "~^GET:/device/api/v1/edge/[A-Za-z0-9][A-Za-z0-9._:-]{0,199}/firmware/sha256:[0-9a-f]{64}$" 1;',
        '        "~^GET:/device/api/v1/edge/[A-Za-z0-9][A-Za-z0-9._:-]{0,199}/media-rendered/sha256:[0-9a-f]{64}$" 1;',
        '        "~^GET:/device/api/v1/edge/[A-Za-z0-9][A-Za-z0-9._:-]{0,199}/catalog$" 1;',
    ]
    for line in additions:
        if line.strip() not in text:
            anchor = '        "~^POST:/device/api/v1/edge/[A-Za-z0-9][A-Za-z0-9._:-]{0,199}/hello$" 1;'
            text = text.replace(anchor, anchor + "\n" + line)
    path.write_text(text, encoding="utf-8")


def patch_edge_dependencies() -> None:
    path = ROOT / "services/edge-distributor/pyproject.toml"
    text = path.read_text(encoding="utf-8")
    if '"pillow' not in text.lower():
        text = text.replace(
            '  "httpx>=0.27,<1",',
            '  "httpx>=0.27,<1",\n  "pillow>=11,<13",',
        )
        path.write_text(text, encoding="utf-8")


def patch_control_plane() -> None:
    integrations = CP / "integrations.py"
    text = integrations.read_text(encoding="utf-8")
    if "upload_firmware" not in text:
        insert = '''
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
class SettlementClient:'''
        text = text.replace("\n\n@dataclass\nclass SettlementClient:", insert)
        integrations.write_text(text, encoding="utf-8")

    main_path = CP / "main.py"
    text = main_path.read_text(encoding="utf-8")
    if "supply_firmware" not in text:
        anchor = "from .supply import FleetClient, RevenueClient, SupplyAggregator, apply_operator_labels"
        if anchor not in text:
            raise RuntimeError("supply import anchor not found in main.py")
        text = text.replace(
            anchor,
            anchor + "\nfrom . import supply_firmware",
        )
        anchor = '''    @app.get("/api/v1/supply/devices/{device_id}/movement-alerts")
    def supply_device_movement_alerts('''
        block = '''    @app.get("/api/v1/supply/firmware")
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
    def supply_device_movement_alerts('''
        if anchor not in text:
            raise RuntimeError("control-plane anchor not found")
        text = text.replace(anchor, block)
        main_path.write_text(text, encoding="utf-8")

    shelf_path = CP / "shelf_activation.py"
    text = shelf_path.read_text(encoding="utf-8")
    old_order = """           ORDER BY r.revision DESC
           LIMIT 1"""
    new_order = """           ORDER BY r.published_at DESC, r.revision DESC
           LIMIT 1"""
    if old_order in text:
        shelf_path.write_text(
            text.replace(old_order, new_order, 1), encoding="utf-8"
        )
        text = shelf_path.read_text(encoding="utf-8")
    valid_anchor = """             AND r.lifecycle_state = 'PUBLISHED'
           ORDER BY r.published_at DESC, r.revision DESC
           LIMIT 1"""
    if "json_extract(r.contract_json, '$.valid_to')" not in text:
        valid_replacement = """             AND r.lifecycle_state = 'PUBLISHED'
             AND json_extract(r.contract_json, '$.valid_from') <= ?
             AND json_extract(r.contract_json, '$.valid_to') > ?
           ORDER BY r.published_at DESC, r.revision DESC
           LIMIT 1"""
        if valid_anchor not in text:
            raise RuntimeError("shelf valid-contract anchor not found")
        text = text.replace(valid_anchor, valid_replacement, 1)
        old_params = """        (campaign_id,),
    ).fetchone()"""
        new_params = """        (
            campaign_id,
            datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        ),
    ).fetchone()"""
        if old_params not in text:
            raise RuntimeError("shelf valid-contract params anchor not found")
        text = text.replace(old_params, new_params, 1)
        shelf_path.write_text(text, encoding="utf-8")

    main_text = main_path.read_text(encoding="utf-8")
    old_policy = (
        'response.headers["Permissions-Policy"] = '
        '"camera=(), microphone=(), geolocation=()"'
    )
    new_policy = (
        'camera_policy = "camera=(self)" if request.url.path.startswith("/shelf/") '
        'else "camera=()"\n'
        '        response.headers["Permissions-Policy"] = '
        'f"{camera_policy}, microphone=(), geolocation=()"'
    )
    if old_policy in main_text:
        main_path.write_text(
            main_text.replace(old_policy, new_policy, 1), encoding="utf-8"
        )
        main_text = main_path.read_text(encoding="utf-8")
    if "shelf_scanner_diagnostics" not in main_text:
        anchor = '''    @app.get("/api/v1/shelf/{token}")
    def shelf_install_status'''
        diagnostic_route = '''    @app.post("/api/v1/shelf/{token}/diagnostics")
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

    @app.get("/api/v1/shelf/{token}")
    def shelf_install_status'''
        if anchor not in main_text:
            raise RuntimeError("shelf status route anchor not found")
        main_path.write_text(
            main_text.replace(anchor, diagnostic_route, 1), encoding="utf-8"
        )
        main_text = main_path.read_text(encoding="utf-8")
    if "shelf_decode_barcode" not in main_text:
        anchor = '''    @app.get("/api/v1/shelf/{token}")
    def shelf_install_status'''
        decode_route = '''    @app.post("/api/v1/shelf/{token}/decode-barcode")
    async def shelf_decode_barcode(
        token: str,
        image: UploadFile = File(...),
    ) -> dict[str, str]:
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
                candidates = [source, ImageOps.autocontrast(source.convert("L"))]
                width, height = source.size
                candidates.extend(
                    [
                        source.crop((0, height // 4, width, 3 * height // 4)),
                        source.crop((0, height // 2, width, height)),
                    ]
                )
                for candidate in candidates:
                    results = zxingcpp.read_barcodes(
                        candidate,
                        formats=zxingcpp.BarcodeFormat.AllLinear,
                        try_rotate=True,
                        try_downscale=True,
                        try_invert=True,
                    )
                    for result in results:
                        digits = "".join(ch for ch in result.text if ch.isdigit())
                        if len(digits) == 13 and digits.startswith("0"):
                            digits = digits[1:]
                        if len(digits) in {8, 11, 12, 13, 14}:
                            log.info(
                                "shelf_barcode_decoded device=%s dimensions=%sx%s format=%s",
                                device_id,
                                width,
                                height,
                                result.format,
                            )
                            return {"upc": normalize_upc(digits)}
        except (OSError, ValueError) as exc:
            raise HTTPException(422, "Barcode photo could not be decoded") from exc
        raise HTTPException(422, "No UPC barcode was found in the photo")

    @app.get("/api/v1/shelf/{token}")
    def shelf_install_status'''
        if anchor not in main_text:
            raise RuntimeError("shelf decode route anchor not found")
        main_path.write_text(
            main_text.replace(anchor, decode_route, 1), encoding="utf-8"
        )

    pyproject_path = CONTROL / "pyproject.toml"
    pyproject = pyproject_path.read_text(encoding="utf-8")
    if '"zxing-cpp' not in pyproject.lower():
        pyproject = pyproject.replace(
            '  "Pillow>=10,<13",',
            '  "Pillow>=10,<13",\n  "zxing-cpp>=3,<4",',
        )
        pyproject_path.write_text(pyproject, encoding="utf-8")

    template_path = CONTROL / "templates/shelf_install.html"
    template = template_path.read_text(encoding="utf-8")
    if "/static/zxing-browser.min.js" not in template:
        template = template.replace(
            '<script src="/static/shelf.js" defer></script>',
            '<script src="/static/zxing-browser.min.js?v=1" defer></script>\n'
            '  <script src="/static/shelf.js?v=3" defer></script>',
        )
    if "data-shelf-barcode-photo" not in template:
        template = template.replace(
            '''        </div>

        <label class="shelf-field">
          <span>Installation photo (optional)</span>''',
            '''        </div>

        <label class="shelf-field">
          <span>Barcode photo fallback</span>
          <input data-shelf-barcode-photo type="file" accept="image/*" capture="environment">
          <small>Use this if live scanning cannot focus on the barcode.</small>
        </label>

        <label class="shelf-field">
          <span>Installation photo (optional)</span>''',
            1,
        )
    template = template.replace(
        '/static/zxing-browser.min.js" ', '/static/zxing-browser.min.js?v=1" '
    ).replace('/static/shelf.js" ', '/static/shelf.js?v=3" ')
    template = template.replace(
        "<span>Barcode photo fallback</span>",
        "<span>Scan barcode with camera (recommended on iPhone)</span>",
    ).replace(
        "Use this if live scanning cannot focus on the barcode.",
        "Take a close, sharp photo with the barcode filling most of the frame.",
    )
    template = template.replace("/static/shelf.js?v=2", "/static/shelf.js?v=6")
    template = template.replace("/static/shelf.js?v=3", "/static/shelf.js?v=6")
    template = template.replace("/static/shelf.js?v=4", "/static/shelf.js?v=6")
    template = template.replace("/static/shelf.js?v=5", "/static/shelf.js?v=6")
    template = template.replace("/static/shelf.js?v=6", "/static/shelf.js?v=7")
    template = template.replace("/static/shelf.js?v=7", "/static/shelf.js?v=8")
    template = template.replace("/static/shelf.js?v=8", "/static/shelf.js?v=9")
    template = template.replace("/static/shelf.js?v=9", "/static/shelf.js?v=10")
    template_path.write_text(template, encoding="utf-8")

    shelf_js_path = CONTROL / "static/shelf.js"
    shelf_js = shelf_js_path.read_text(encoding="utf-8")
    old_decoder = '''  async function decodeBarcodeFromCanvas() {
    if (!("BarcodeDetector" in window)) {
      showError("Automatic barcode detection is not supported here. Type the UPC manually.");
      return;
    }
    const detector = new BarcodeDetector({ formats: ["ean_13", "ean_8", "upc_a", "upc_e"] });
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext("2d").drawImage(video, 0, 0);
    const codes = await detector.detect(canvas);
    if (!codes.length) {
      showError("No barcode detected. Hold steady and try again.");
      return;
    }
    upcInput.value = codes[0].rawValue.replace(/\\D/g, "");
    clearMessages();
  }'''
    new_decoder = '''  async function decodeBarcodeFromCanvas() {
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext("2d").drawImage(video, 0, 0);
    let rawValue = "";
    if ("BarcodeDetector" in window) {
      const detector = new BarcodeDetector({ formats: ["ean_13", "ean_8", "upc_a", "upc_e"] });
      const codes = await detector.detect(canvas);
      if (codes.length) rawValue = codes[0].rawValue;
    } else if (window.ZXingBrowser) {
      try {
        const reader = new ZXingBrowser.BrowserMultiFormatOneDReader();
        rawValue = reader.decodeFromCanvas(canvas).getText();
      } catch (_) {
        rawValue = "";
      }
    } else {
      showError("Barcode decoding is unavailable. Type the UPC manually.");
      return;
    }
    if (!rawValue) {
      showError("No barcode detected. Hold steady and try again.");
      return;
    }
    upcInput.value = rawValue.replace(/\\D/g, "");
    clearMessages();
  }'''
    if old_decoder in shelf_js:
        shelf_js_path.write_text(
            shelf_js.replace(old_decoder, new_decoder, 1), encoding="utf-8"
        )


def patch_control_plane_sync() -> None:
    """Wire campaign_sync (catalog push, schedules, shelf ack) into main.py."""
    main_path = CP / "main.py"
    text = main_path.read_text(encoding="utf-8")

    if "from . import campaign_sync" not in text:
        anchor = "from . import supply_firmware"
        if anchor not in text:
            raise RuntimeError("supply_firmware import anchor not found")
        text = text.replace(anchor, anchor + "\nfrom . import campaign_sync", 1)

    if "push_campaign_catalog_quietly(connection, edge)" not in text:
        anchor = "                    process_sms_outbox()"
        if anchor not in text:
            raise RuntimeError("worker anchor not found")
        text = text.replace(
            anchor,
            anchor
            + "\n                    if edge is not None:"
            + "\n                        with db.connect() as worker_conn:"
            + "\n                            campaign_sync.push_campaign_catalog_quietly("
            + "\n                                worker_conn, edge"
            + "\n                            )",
            1,
        )

    if "push_device_derivatives" not in text:
        anchor = """            source_path.unlink(missing_ok=True)
            if edge:
                process_outbox(limit=2)"""
        if anchor not in text:
            raise RuntimeError("upload media anchor not found")
        text = text.replace(
            anchor,
            """            source_path.unlink(missing_ok=True)
            if edge:
                process_outbox(limit=2)
                campaign_sync.push_device_derivatives(
                    edge, result.derivative_hash, derivative_path
                )
                campaign_sync.push_campaign_catalog_quietly(conn, edge)""",
            1,
        )
    if "original_path = settings.media_dir" not in text:
        original_anchor = "            source_path.unlink(missing_ok=True)"
        if original_anchor not in text:
            raise RuntimeError("media original retention anchor not found")
        text = text.replace(
            original_anchor,
            """            # Retain the validated original for audit/reprocessing.
            original_path = settings.media_dir / (
                f"{media_id}-original{source_path.suffix.lower()}"
            )
            source_path.replace(original_path)""",
            1,
        )

    if "supply_campaign_catalog_sync" not in text:
        anchor = '''    @app.get("/api/v1/supply/devices/{device_id}/movement-alerts")'''
        if anchor not in text:
            raise RuntimeError("supply movement alerts anchor not found")
        block = '''    @app.post(
        "/api/v1/supply/campaign-catalog/sync",
        dependencies=[Depends(require_csrf)],
    )
    def supply_campaign_catalog_sync(
        request: Request, session: SupplySession, conn: Db
    ) -> dict[str, Any]:
        del session
        return campaign_sync.push_campaign_catalog(
            conn, request.app.state.edge_distributor
        )

    @app.get("/api/v1/supply/devices/{device_id}/schedule")
    def supply_device_schedule(
        device_id: str, request: Request, session: SupplySession
    ) -> dict[str, Any]:
        del session
        return campaign_sync.get_device_schedule(
            request.app.state.edge_distributor, device_id
        )

    @app.put(
        "/api/v1/supply/devices/{device_id}/schedule",
        dependencies=[Depends(require_csrf)],
    )
    def supply_device_schedule_update(
        device_id: str,
        body: campaign_sync.ScheduleIn,
        request: Request,
        session: SupplySession,
    ) -> dict[str, Any]:
        del session
        return campaign_sync.set_device_schedule(
            request.app.state.edge_distributor, device_id, body
        )

    @app.get("/api/v1/supply/devices/{device_id}/movement-alerts")'''
        text = text.replace(anchor, block, 1)

    if "shelf_deployment_status" not in text:
        anchor = '''    @app.get("/api/v1/shelf/{token}")
    def shelf_install_status'''
        if anchor not in text:
            raise RuntimeError("shelf status anchor not found")
        block = '''    @app.get("/api/v1/shelf/{token}/deployments/{deployment_id}")
    def shelf_deployment_ack(
        token: str, deployment_id: str, request: Request, conn: Db
    ) -> dict[str, Any]:
        verify_shelf_token(token, settings)
        return campaign_sync.shelf_deployment_status(
            conn,
            request.app.state.edge_distributor,
            deployment_id,
            hashlib.sha256(token.encode()).hexdigest(),
        )

    @app.get("/api/v1/shelf/{token}")
    def shelf_install_status'''
        text = text.replace(anchor, block, 1)

    legacy_shelf_battery = (
        '            "battery_telemetry": device.get("battery_telemetry"),'
    )
    if legacy_shelf_battery in text:
        text = text.replace(
            legacy_shelf_battery,
            '            "battery_telemetry": '
            'device.get("device_battery") or device.get("battery_telemetry"),',
            1,
        )

    upc_add_anchor = (
        '        return {"campaign_id": campaign_id, "upc": upc, "label": payload.label}'
    )
    if upc_add_anchor in text and "catalog after UPC change" not in text:
        text = text.replace(
            upc_add_anchor,
            "        # Refresh the edge preload catalog after UPC change.\n"
            "        campaign_sync.push_campaign_catalog_quietly(conn, edge)\n"
            + upc_add_anchor,
            1,
        )
        upc_remove_anchor = '        return {"status": "removed", "upc": normalized}'
        if upc_remove_anchor in text:
            text = text.replace(
                upc_remove_anchor,
                "        campaign_sync.push_campaign_catalog_quietly(conn, edge)\n"
                + upc_remove_anchor,
                1,
            )

    transition_anchor = (
        '        return {"id": campaign_id, "status": target, '
        '"transitioned_bids": transitioned}'
    )
    if (
        transition_anchor in text
        and "push_campaign_catalog_quietly(conn, edge)\n" + transition_anchor
        not in text
    ):
        text = text.replace(
            transition_anchor,
            "        campaign_sync.push_campaign_catalog_quietly(conn, edge)\n"
            + transition_anchor,
            1,
        )

    main_path.write_text(text, encoding="utf-8")

    pyproject_path = CONTROL / "pyproject.toml"
    pyproject = pyproject_path.read_text(encoding="utf-8")
    if '"cairosvg' not in pyproject.lower():
        pyproject = pyproject.replace(
            '  "Pillow>=10,<13",',
            '  "Pillow>=10,<13",\n  "cairosvg>=2.7,<3",\n  "pypdfium2>=4,<5",',
        )
        pyproject_path.write_text(pyproject, encoding="utf-8")

    dockerfile_path = CONTROL / "Dockerfile"
    dockerfile = dockerfile_path.read_text(encoding="utf-8")
    if "libcairo2" not in dockerfile:
        dockerfile = dockerfile.replace(
            "apt-get install -y --no-install-recommends ffmpeg curl",
            "apt-get install -y --no-install-recommends ffmpeg curl libcairo2",
        )
        dockerfile_path.write_text(dockerfile, encoding="utf-8")

    supply_path = CP / "supply.py"
    supply_text = supply_path.read_text(encoding="utf-8")
    if '"device_battery"' not in supply_text:
        allow_anchor = '''            "uptime_seconds",
            "boot_id",
            "link_type",
        )'''
        if allow_anchor not in supply_text:
            raise RuntimeError("public_device allow-list anchor not found")
        supply_text = supply_text.replace(
            allow_anchor,
            '''            "uptime_seconds",
            "boot_id",
            "link_type",
            "battery_telemetry",
            "device_battery",
            "status_report",
            "schedule",
            "next_wake_at",
        )''',
            1,
        )
        battery_anchor = '''        battery = result.get("battery_telemetry")
        if isinstance(battery, dict) and battery.get("level_percent") is not None:
            charging = "charging" if battery.get("charging") else "on battery"
            result["battery_summary"] = f"{battery['level_percent']}% · {charging}"
        else:
            result["battery_summary"] = None'''
        if battery_anchor not in supply_text:
            raise RuntimeError("battery summary anchor not found")
        supply_text = supply_text.replace(
            battery_anchor,
            '''        battery = result.get("device_battery") or result.get("battery_telemetry")
        result["battery_summary"] = None
        if isinstance(battery, dict):
            if battery.get("available") is False:
                result["battery_summary"] = "Sensor error"
            elif battery.get("level_percent") is not None:
                charging = (
                    "charging" if battery.get("charging") else "on battery"
                )
                result["battery_summary"] = (
                    f"{battery['level_percent']}% · {charging}"
                )
            elif battery.get("voltage_mv") is not None:
                result["battery_summary"] = (
                    f"{battery['voltage_mv'] / 1000:.2f} V"
                )''',
            1,
        )
        supply_path.write_text(supply_text, encoding="utf-8")


def patch_config() -> None:
    path = EDGE / "config.py"
    text = path.read_text(encoding="utf-8")
    if "max_firmware_bytes" not in text:
        text = text.replace(
            "max_media_bytes: int = Field(default=100 * 1024 * 1024, ge=1024)",
            "max_media_bytes: int = Field(default=100 * 1024 * 1024, ge=1024)\n"
            "    max_firmware_bytes: int = Field(default=6 * 1024 * 1024, ge=1024)",
        )
        path.write_text(text, encoding="utf-8")


def main() -> int:
    copy_modules()
    patch_db()
    patch_edge_main()
    patch_edge_signed_payload()
    patch_gateway()
    patch_edge_dependencies()
    patch_control_plane()
    patch_control_plane_sync()
    patch_config()
    print("Digital Engine firmware integration patches applied.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
