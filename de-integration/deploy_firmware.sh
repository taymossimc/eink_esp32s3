#!/usr/bin/env bash
# Upload the built firmware to the edge distributor and assign it to a device.
# Usage: deploy_firmware.sh <version> [device_id]
set -euo pipefail
VERSION="$1"
DEVICE="${2:-}"
BIN="$HOME/eink-fw/.pio/build/esp32-s3-first-light/firmware.bin"
HASH="sha256:$(sha256sum "$BIN" | cut -c1-64)"
echo "artifact: $HASH ($(stat -c%s "$BIN") bytes)"
cd /mnt/dockerprojects/digital_engine/deploy/arachne
TOKEN=$(grep -E '^TMADS_EDGE_INTERNAL_BEARER_TOKEN=' .env | cut -d= -f2-)
BASE="http://192.168.2.102:8781"
curl -s -X PUT -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/octet-stream" \
  --data-binary "@$BIN" \
  "$BASE/internal/v1/firmware/$HASH/content?version=$VERSION"
echo
if [ -n "$DEVICE" ]; then
  curl -s -X PUT -H "Authorization: Bearer $TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"version\":\"$VERSION\",\"artifact_hash\":\"$HASH\"}" \
    "$BASE/internal/v1/devices/$DEVICE/firmware-assignment"
  echo
fi
