#!/usr/bin/env bash
set -euo pipefail

# ════════════════════════════════════════════════════════════
# restore.sh — восстановление из бэкапа
# ════════════════════════════════════════════════════════════
# Использование:
#   ./scripts/restore.sh ~/trading-bot-backups/2026-06-13_190000
# ════════════════════════════════════════════════════════════

if [[ $# -lt 1 ]]; then
  echo "Ошибка: укажи директорию с бэкапом"
  echo "  ./scripts/restore.sh ~/trading-bot-backups/2026-06-13_190000"
  echo ""
  echo "Доступные бэкапы:"
  ls -1d "$HOME/trading-bot-backups/"*/ 2>/dev/null || echo "(нет бэкапов)"
  exit 1
fi

BACKUP_DIR="$1"
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [[ ! -d "$BACKUP_DIR" ]]; then
  echo "Ошибка: директория не найдена: $BACKUP_DIR"
  exit 1
fi

echo "🔹 Восстановление из: $BACKUP_DIR"
echo "🔹 Проект: $PROJECT_DIR"
echo ""

# ── 1. SQLite ──────────────────────────────────────────────
echo "[1/4] SQLite..."
SQLITE_DEST="$PROJECT_DIR/backend/data/trading.db"
if [[ -f "$BACKUP_DIR/trading.db" ]]; then
  cp "$BACKUP_DIR/trading.db" "$SQLITE_DEST"
  echo "  ✓ trading.db восстановлен"
fi

# ── 2. Стратегии ──────────────────────────────────────────
echo "[2/4] Стратегии..."
if [[ -d "$BACKUP_DIR/strategies" ]]; then
  cp "$BACKUP_DIR/strategies/"*.py "$PROJECT_DIR/backend/strategies/" 2>/dev/null || true
  echo "  ✓ стратегии восстановлены ($(ls "$BACKUP_DIR/strategies/"*.py 2>/dev/null | wc -l) файлов)"
fi

# ── 3. Конфиги ────────────────────────────────────────────
echo "[3/4] Конфигурация..."
if [[ -d "$BACKUP_DIR/config" ]]; then
  if [[ -f "$BACKUP_DIR/config/.env.raw" ]]; then
    cp "$BACKUP_DIR/config/.env.raw" "$PROJECT_DIR/backend/.env"
    chmod 600 "$PROJECT_DIR/backend/.env"
    echo "  ✓ .env.raw → backend/.env (600)"
  elif [[ -f "$BACKUP_DIR/config/.env" ]]; then
    cp "$BACKUP_DIR/config/.env" "$PROJECT_DIR/backend/.env"
    echo "  ⚠ .env восстановлен из redacted-версии (без секретных ключей)"
  fi
fi

# ── 4. Backtest-результаты ────────────────────────────────
echo "[4/4] Backtest-результаты..."
if [[ -d "$BACKUP_DIR/backtests" ]]; then
  if [[ -f "$BACKUP_DIR/backtests/results.tar.gz" ]]; then
    mkdir -p "$PROJECT_DIR/backend/backtests_data"
    tar xzf "$BACKUP_DIR/backtests/results.tar.gz" -C "$PROJECT_DIR/backend/backtests_data/"
    echo "  ✓ результаты восстановлены из tar.gz"
  fi
  if [[ -d "$BACKUP_DIR/backtests/candles" ]]; then
    mkdir -p "$PROJECT_DIR/backend/backtests_data/candles"
    cp "$BACKUP_DIR/backtests/candles/"*.json "$PROJECT_DIR/backend/backtests_data/candles/" 2>/dev/null || true
    echo "  ✓ свечи восстановлены"
  fi
fi

echo ""
echo "═ Восстановление завершено ═"
