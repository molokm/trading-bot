#!/bin/bash
set -e

ts() { date -Iseconds; }

echo "[entrypoint] $(ts) === STARTING ==="
echo "[entrypoint] $(ts) PORT=$PORT"
echo "[entrypoint] $(ts) BACKEND_PORT=${BACKEND_PORT:-8000}"
echo "[entrypoint] $(ts) DATABASE_URL set: $([ -n \"$DATABASE_URL\" ] && echo YES || echo NO)"
echo "[entrypoint] $(ts) OKX_API_KEY set: $([ -n \"$OKX_API_KEY\" ] && echo YES || echo NO)"

echo "[entrypoint] $(ts) Starting uvicorn..."
exec uvicorn app.main:app --host 0.0.0.0 --port "${BACKEND_PORT:-8000}" --log-level info 2>&1
