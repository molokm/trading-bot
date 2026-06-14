#!/usr/bin/env bash
set -euo pipefail

# ════════════════════════════════════════════════════════════
# backup.sh — полный бэкап торгового бота (локальное хранение)
# ════════════════════════════════════════════════════════════
# Использование:
#   ./scripts/backup.sh                         # обычный бэкап
#   ./scripts/backup.sh --with-backtests        # включая 1.7G backtest-результатов
#   DATABASE_URL="postgresql://..." ./scripts/backup.sh  # + pg_dump Neon
#
# Бэкапы сохраняются в: ~/trading-bot-backups/YYYY-MM-DD_HHMMSS/
# ════════════════════════════════════════════════════════════

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_ROOT="$HOME/trading-bot-backups"
TIMESTAMP=$(date '+%Y-%m-%d_%H%M%S')
BACKUP_DIR="$BACKUP_ROOT/$TIMESTAMP"

WITH_BACKTESTS=false
if [[ "${1:-}" == "--with-backtests" ]]; then
  WITH_BACKTESTS=true
fi

mkdir -p "$BACKUP_DIR"
echo "🔹 Бэкап: $BACKUP_DIR"
echo ""

# ── 1. SQLite (локальная БД) ──────────────────────────────
echo "[1/6] SQLite..."
SQLITE_SRC="$PROJECT_DIR/backend/data/trading.db"
if [[ -f "$SQLITE_SRC" ]]; then
  sqlite3 "$SQLITE_SRC" ".backup '$BACKUP_DIR/trading.db'"
  sqlite3 "$SQLITE_SRC" ".dump" | gzip > "$BACKUP_DIR/trading_dump.sql.gz"
  echo "  ✓ trading.db ($(du -h "$SQLITE_SRC" | cut -f1))"
fi

# дополнительный trading_bot.db (если есть)
EXTRA_DB="$PROJECT_DIR/backend/trading_bot.db"
if [[ -f "$EXTRA_DB" ]]; then
  sqlite3 "$EXTRA_DB" ".backup '$BACKUP_DIR/trading_bot.db'"
  echo "  ✓ trading_bot.db ($(du -h "$EXTRA_DB" | cut -f1))"
fi

# ── 2. Стратегии ────────────────────────────────────────────
echo "[2/6] Стратегии..."
mkdir -p "$BACKUP_DIR/strategies"
if ls "$PROJECT_DIR/backend/strategies/"*.py 1>/dev/null 2>&1; then
  cp "$PROJECT_DIR/backend/strategies/"*.py "$BACKUP_DIR/strategies/"
  echo "  ✓ backend/strategies/ ($(ls "$PROJECT_DIR/backend/strategies/"*.py 2>/dev/null | wc -l) файлов)"
fi

# ── 3. Конфиги ─────────────────────────────────────────────
echo "[3/6] Конфигурация..."
mkdir -p "$BACKUP_DIR/config"

for f in ".env.example" "render.yaml" "Dockerfile" "start.sh" "backend/Dockerfile" "backend/run.sh" "backend/.dockerignore" "backend/fly.toml"; do
  src="$PROJECT_DIR/$f"
  if [[ -f "$src" ]]; then
    cp "$src" "$BACKUP_DIR/config/"
  fi
done

# .env (без ключей на случай если попадёт в бэкап — заменяем sensitive)
if [[ -f "$PROJECT_DIR/backend/.env" ]]; then
  sed 's/^OKX_SECRET_KEY=.*/OKX_SECRET_KEY=***REDACTED***/' \
    "$PROJECT_DIR/backend/.env" > "$BACKUP_DIR/config/.env"
  # также копируем как есть в protected (с правами 600)
  cp "$PROJECT_DIR/backend/.env" "$BACKUP_DIR/config/.env.raw"
  chmod 600 "$BACKUP_DIR/config/.env.raw"
  echo "  ✓ backend/.env → .env (redacted) + .env.raw (protected)"
fi

# frontend
for f in "frontend/package.json" "frontend/vite.config.js" "frontend/tailwind.config.js"; do
  src="$PROJECT_DIR/$f"
  if [[ -f "$src" ]]; then
    cp "$src" "$BACKUP_DIR/config/"
  fi
done

echo "  ✓ конфиги скопированы"

# ── 4. Backtest-результаты (опционально, большой объём) ────
if $WITH_BACKTESTS; then
  echo "[4/6] Backtest-результаты (1.7G)..."
  BACKTEST_DIR="$PROJECT_DIR/backend/backtests_data"

  # свечи (маленькие)
  if [[ -d "$BACKTEST_DIR/candles" ]]; then
    mkdir -p "$BACKUP_DIR/backtests/candles"
    cp "$BACKTEST_DIR/candles/"*.json "$BACKUP_DIR/backtests/candles/"
    echo "  ✓ свечи ($(ls "$BACKTEST_DIR/candles/"*.json 2>/dev/null | wc -l) файлов)"
  fi

  # результаты — tar + gzip (экономит ~2-3x)
  RESULT_COUNT=$(ls "$BACKTEST_DIR/"*_2*.json 2>/dev/null | wc -l || true)
  if [[ "$RESULT_COUNT" -gt 0 ]]; then
    mkdir -p "$BACKUP_DIR/backtests/results"
    echo "  архивирую $RESULT_COUNT файлов..."
    tar czf "$BACKUP_DIR/backtests/results.tar.gz" \
      -C "$BACKTEST_DIR" \
      $(ls "$BACKTEST_DIR/"*.json 2>/dev/null | xargs -n1 basename)
    echo "  ✓ backtest-результаты → results.tar.gz"
  fi
else
  echo "[4/6] Backtest-результаты — пропущены (--with-backtests)"
fi

# ── 5. PostgreSQL (Neon, если доступен) ────────────────────
echo "[5/6] PostgreSQL (Neon)..."
PG_URL="${DATABASE_URL:-}"
PG_DUMP="$(command -v pg_dump 2>/dev/null || echo "/opt/homebrew/opt/libpq/bin/pg_dump")"
if [[ -n "$PG_URL" ]]; then
  if [[ -x "$PG_DUMP" ]]; then
    echo "  pg_dump найден, создаю дамп..."
    "$PG_DUMP" "$PG_URL" --no-owner --no-acl --format=c \
      -f "$BACKUP_DIR/neon_dump.pgdump" 2>&1 || echo "  ⚠ pg_dump не удался"
  else
    echo "  pg_dump не установлен. Установка: brew install libpq"
    echo "  и добавь в PATH:  export PATH=\"\$(brew --prefix libpq)/bin:\$PATH\""
  fi
else
  echo "  DATABASE_URL не задана — пропускаю (Neon только на Render)"
fi

# ── 6. Мета-информация ─────────────────────────────────────
echo "[6/6] Мета-информация..."
{
  echo "Проект: Торговый бот OKX/BTC-USDT-SWAP"
  echo "Дата:   $(date '+%Y-%m-%d %H:%M:%S')"
  echo ""
  echo "--- git status ---"
  git -C "$PROJECT_DIR" status --short 2>/dev/null || echo "(не git-репозиторий)"
  echo ""
  echo "--- git log (последние 3) ---"
  git -C "$PROJECT_DIR" log --oneline -3 2>/dev/null || echo "(нет)"
  echo ""
  echo "--- стратегии ---"
  ls -1 "$PROJECT_DIR/backend/strategies/"*.py 2>/dev/null || echo "(нет)"
} > "$BACKUP_DIR/MANIFEST.txt"

# ── Итого ──────────────────────────────────────────────────
echo ""
echo "═══════════════════════════════════════════"
echo "✅ Бэкап завершён: $BACKUP_DIR"
du -sh "$BACKUP_DIR"
echo ""
echo "Содержимое:"
ls -lh "$BACKUP_DIR" | grep -v "^total"
echo "═══════════════════════════════════════════"
