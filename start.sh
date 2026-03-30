#!/usr/bin/env bash
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

source .venv/bin/activate

HOST="${DBS_HOST:-127.0.0.1}"
PORT="${DBS_PORT:-8010}"

exec uvicorn app.main:app --host "$HOST" --port "$PORT"
