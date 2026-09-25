#!/usr/bin/env bash
# Idempotent Cloud Agent setup for the eink_esp32s3 monorepo.
#
# Sets up three developer workflows:
#   1. ESP32-S3 firmware builds via PlatformIO (pio run)
#   2. Image conversion tooling (image_conversion/convert.py, needs Pillow)
#   3. Arachne backend services (FastAPI + SQLite control-plane web app)
#
# Safe to run repeatedly: every step converges instead of appending state.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# --- System packages: Python venv support (only when missing) ---
if ! python3 -c "import ensurepip" >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo apt-get install -y python3-venv python3-pip
fi

# --- Python virtual environment ---
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip wheel

# --- Firmware + image-conversion tooling ---
pip install "platformio>=6,<7" "Pillow>=10"

# --- Arachne backend services (editable installs) ---
# These live under .arachne-src, which is only present on branches that include
# the backend. Guard the install so setup still succeeds on branches without it.
if [ -f ".arachne-src/services/control-plane/pyproject.toml" ]; then
  pip install -e ".arachne-src/services/control-plane[dev]"
  # edge-distributor imports an out-of-repo `tmads_shared` package that is not
  # vendored here, so it cannot be started from this checkout. Install it anyway
  # so its dependencies are present; ignore failure to keep setup resilient.
  pip install -e ".arachne-src/services/edge-distributor[dev]" || true
else
  echo "Arachne backend (.arachne-src) not present in this checkout; skipping."
fi

# --- Prime PlatformIO platform + toolchains + libraries (cached into image) ---
if [ -f platformio.ini ]; then
  pio pkg install -e esp32-s3-first-light
fi

echo "Cloud Agent environment ready."
