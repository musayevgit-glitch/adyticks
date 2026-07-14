#!/bin/bash
# Low-RAM VPS runner: headed Chromium under Xvfb so grecaptcha.execute works.
set -euo pipefail
cd "$(dirname "$0")/.."

PY="${HOME}/adyticks/.venv/bin/python"
if [ ! -x "$PY" ]; then
  PY="$(command -v python3)"
fi

export HEADLESS="${HEADLESS:-0}"
export ADY_BROWSER="${ADY_BROWSER:-chromium,firefox}"

if command -v xvfb-run >/dev/null 2>&1; then
  exec xvfb-run -a "$PY" main.py --once
fi

echo "WARN: xvfb-run not found; running headless may fail grecaptcha" >&2
export HEADLESS=1
exec "$PY" main.py --once
