"""End-to-end test of shelf token, decode-barcode, status, and ack routes.

Runs inside the control-plane container where settings and zxing-cpp exist.
Calls the API over the loopback uvicorn port.
"""

import io
import json
import urllib.request

import zxingcpp
from PIL import Image

from tmads_control_plane.config import get_settings
from tmads_control_plane.shelf_activation import create_shelf_token

settings = get_settings()
token = create_shelf_token("tmads-eink-a4cb8fdd9120", settings)
print("token created, length", len(token))

BASE = "http://127.0.0.1:8000"

# 1. Status endpoint.
with urllib.request.urlopen(f"{BASE}/api/v1/shelf/{token}", timeout=15) as response:
    print("status:", json.loads(response.read()))

# 2. Barcode decode: UPC-A 990000000004 rendered as EAN-13 with leading zero
# (check digit 4 for payload 099000000000).
barcode = zxingcpp.write_barcode(
    zxingcpp.BarcodeFormat.EAN13, "0990000000004", width=600, height=300
)
pil = Image.fromarray(barcode).convert("RGB")
padded = Image.new("RGB", (800, 500), "white")
padded.paste(pil, (100, 100))
buffer = io.BytesIO()
padded.save(buffer, "JPEG", quality=92)
content = buffer.getvalue()

boundary = "----test1234"
body = (
    f"--{boundary}\r\n"
    'Content-Disposition: form-data; name="image"; filename="barcode.jpg"\r\n'
    "Content-Type: image/jpeg\r\n\r\n"
).encode() + content + f"\r\n--{boundary}--\r\n".encode()
request = urllib.request.Request(
    f"{BASE}/api/v1/shelf/{token}/decode-barcode",
    data=body,
    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=30) as response:
        print("decode:", json.loads(response.read()))
except urllib.error.HTTPError as error:
    print("decode failed:", error.code, error.read().decode()[:200])

# 3. UPC lookup for the decoded product.
request = urllib.request.Request(
    f"{BASE}/api/v1/shelf/{token}/lookup",
    data=json.dumps({"upc": "990000000004"}).encode(),
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=15) as response:
        print("lookup:", json.loads(response.read()))
except urllib.error.HTTPError as error:
    print("lookup failed:", error.code, error.read().decode()[:300])

# 4. Deploy the mapped campaign and read its acknowledgement stage.
boundary = "----deploy1234"
body = (
    f"--{boundary}\r\n"
    'Content-Disposition: form-data; name="upc"\r\n\r\n'
    "990000000004\r\n"
    f"--{boundary}--\r\n"
).encode()
request = urllib.request.Request(
    f"{BASE}/api/v1/shelf/{token}/deploy",
    data=body,
    headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    method="POST",
)
try:
    with urllib.request.urlopen(request, timeout=30) as response:
        deployment = json.loads(response.read())
        print("deploy:", deployment)
    with urllib.request.urlopen(
        f"{BASE}/api/v1/shelf/{token}/deployments/"
        f"{deployment['deployment_id']}",
        timeout=15,
    ) as response:
        print("ack:", json.loads(response.read()))
except urllib.error.HTTPError as error:
    print("deploy/ack failed:", error.code, error.read().decode()[:300])

# 5. Unknown deployment IDs still return a clean 404.
try:
    with urllib.request.urlopen(
        f"{BASE}/api/v1/shelf/{token}/deployments/shelf_unknown", timeout=15
    ) as response:
        print("ack:", json.loads(response.read()))
except urllib.error.HTTPError as error:
    print("ack (expected 404):", error.code)
