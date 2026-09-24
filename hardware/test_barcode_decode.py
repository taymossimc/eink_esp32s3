"""Generate a UPC-A barcode with zxing-cpp and verify the shelf decoder."""

import base64
import io
import sys

import zxingcpp
from PIL import Image

image = zxingcpp.write_barcode(
    zxingcpp.BarcodeFormat.UPCA, "099000000003", width=600, height=300
)
pil = Image.fromarray(image).convert("RGB")
padded = Image.new("RGB", (800, 500), "white")
padded.paste(pil, (100, 100))
buffer = io.BytesIO()
padded.save(buffer, "JPEG", quality=92)
sys.stdout.write(base64.b64encode(buffer.getvalue()).decode())
