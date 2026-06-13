#!/bin/bash
echo "=== OKX Trading Terminal ==="

BACKEND_DIR="$(cd "$(dirname "$0")" && pwd)/backend"
FRONTEND_DIR="$(cd "$(dirname "$0")" && pwd)/frontend"

# Start backend
echo "[Backend] Starting on http://localhost:8000 ..."
cd "$BACKEND_DIR"
source venv/bin/activate 2>/dev/null
PYTHONPATH="$BACKEND_DIR" uvicorn app.main:app --host 0.0.0.0 --port 8000 &
BACKEND_PID=$!

# Start frontend
echo "[Frontend] Starting on http://localhost:3000 ..."
cd "$FRONTEND_DIR"
npm run dev &
FRONTEND_PID=$!

echo ""
echo "  Backend:  http://localhost:8000"
echo "  Frontend: http://localhost:3000"
echo "  API Docs: http://localhost:8000/docs"
echo ""
echo "Press Ctrl+C to stop both servers."

trap "kill $BACKEND_PID $FRONTEND_PID 2>/dev/null; exit" INT TERM
wait
