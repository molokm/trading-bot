#!/bin/bash
set -e

ts() { date -Iseconds; }

echo "[entrypoint] $(ts) === STARTING ==="
echo "[entrypoint] $(ts) PORT=$PORT"
echo "[entrypoint] $(ts) BACKEND_PORT=${BACKEND_PORT:-}"

# Render injects PORT; local/dev may use BACKEND_PORT
LISTEN_PORT="${PORT:-${BACKEND_PORT:-8000}}"
echo "[entrypoint] $(ts) LISTEN_PORT=$LISTEN_PORT"
echo "[entrypoint] $(ts) DATABASE_URL set: $([ -n "$DATABASE_URL" ] && echo YES || echo NO)"
echo "[entrypoint] $(ts) OKX_API_KEY set: $([ -n "$OKX_API_KEY" ] && echo YES || echo NO)"

echo "[entrypoint] $(ts) Starting uvicorn on $LISTEN_PORT ..."
exec uvicorn app.main:app --host 0.0.0.0 --port "$LISTEN_PORT" \
  --log-level info \
  --timeout-keep-alive 30 \
  --limit-concurrency 50 \
  2>&1
