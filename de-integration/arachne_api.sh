#!/usr/bin/env bash
# Helper for internal Digital Engine API calls on Arachne.
# Usage: arachne_api.sh <edge|cp> <METHOD> <path> [json-body-file]
set -euo pipefail
cd /mnt/dockerprojects/digital_engine/deploy/arachne
EDGE_TOKEN=$(grep -E '^TMADS_EDGE_INTERNAL_BEARER_TOKEN=' .env | cut -d= -f2-)
EDGE_URL="http://192.168.2.102:8781"
SERVICE="$1"; METHOD="$2"; API_PATH="$3"; BODY_FILE="${4:-}"
case "$SERVICE" in
  edge) BASE="$EDGE_URL"; TOKEN="$EDGE_TOKEN" ;;
  *) echo "unknown service" >&2; exit 1 ;;
esac
ARGS=( -s -X "$METHOD" -H "Authorization: Bearer $TOKEN" )
if [ -n "$BODY_FILE" ]; then
  ARGS+=( -H "Content-Type: application/json" --data-binary "@$BODY_FILE" )
fi
curl "${ARGS[@]}" "$BASE$API_PATH"
