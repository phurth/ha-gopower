#!/usr/bin/env bash
# Deploy GoPower Solar integration to Home Assistant OS
set -euo pipefail

HAOS_HOST="${HAOS_HOST:-root@10.115.19.131}"
HAOS_PORT="${HAOS_PORT:-22}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"
SRC="$REPO_DIR/custom_components/gopower"
DST="$HAOS_HOST:/config/custom_components/gopower"

echo "==> Deploying gopower to ${HAOS_HOST}:${HAOS_PORT}"
echo "    Source: $SRC"
echo "    Target: $DST"

# Create target directory
ssh -p "$HAOS_PORT" "$HAOS_HOST" "mkdir -p /config/custom_components/gopower/translations"

# Sync files
scp -p -P "$HAOS_PORT" "$SRC"/*.py "$SRC"/*.json "$DST/"
scp -p -P "$HAOS_PORT" "$SRC"/translations/*.json "$DST/translations/"

echo "==> Files deployed. Restarting HA core..."
ssh -p "$HAOS_PORT" "$HAOS_HOST" "ha core restart"
