#!/bin/bash
# Run the backend locally. Canonical venv is .venv; fall back to legacy venv/.
cd "$(dirname "$0")"
PY=./.venv/bin/python
[ -x "$PY" ] || PY=./venv/bin/python
exec "$PY" -m uvicorn app.main:app --host 0.0.0.0 --port 8000
