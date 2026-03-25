#!/usr/bin/env bash
set -euo pipefail

cd /home/sedjwin/DeviceBridgeService
. /home/sedjwin/DeviceBridgeService/.venv/bin/activate
exec python -m uvicorn app.main:app --host 0.0.0.0 --port 8011
