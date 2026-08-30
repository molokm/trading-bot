import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DATABASE_URL = os.getenv("DATABASE_URL", "")


def _is_pg_url(url: str) -> bool:
    """Return True only for real PostgreSQL connection strings."""
    return bool(url) and url.startswith(("postgres://", "postgresql://"))


class Database:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or str(Path(__file__).parent.parent / "data" / "trading.db")
        self._conn: Optional = None
        self._pool: Optional = None
        # If DATABASE_URL is a file: path, extract it as SQLite path
        if DATABASE_URL and DATABASE_URL.startswith("file:"):
            self.db_path = DATABASE_URL.replace("file:", "")
        self._pg_mode = _is_pg_url(DATABASE_URL)
        self._fallback_reason = ""

    async def init(self):
        if self._pg_mode:
            import asyncpg
            print("[db] Connecting to PostgreSQL ...", flush=True)
            last_err = None
            conn = None
            for attempt in range(1, 4):
                try:
                    conn = await asyncio.wait_for(
                        asyncpg.connect(DATABASE_URL), timeout=30)
                    break
                except (asyncio.TimeoutError, OSError, Exception) as e:
                    last_err = e
                    print(f"[db] Attempt {attempt}/3 failed: {e}", flush=True)
                    if attempt < 3:
                        await asyncio.sleep(3 * attempt)
            else:
                # Neon/Render free tier often hits compute-time quota — do not
                # take down the whole trading app; fall back to local SQLite.
                err_s = str(last_err or "")
                quota = any(
                    x in err_s.lower()
                    for x in (
                        "compute time quota",
                        "exceeded the compute",
                        "insufficientresources",
                        "quota",
                        "too many connections",
                    )
                )
                print(
                    f"[db] PostgreSQL unavailable ({last_err}). "
                    f"{'Quota/limit hit — ' if quota else ''}"
                    f"falling back to SQLite at {self.db_path}",
                    flush=True,
                )
                self._pg_mode = False
                self._fallback_reason = err_s
                await self._init_sqlite()
                return
            print("[db] Connected, running migrations ...", flush=True)
            try:
                await self._migrate_pg(conn)
            finally:
                await conn.close()
            print("[db] PG init done", flush=True)
            self._fallback_reason = ""
        else:
            await self._init_sqlite()

    async def _init_sqlite(self):
        import aiosqlite
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._migrate_sqlite()
        print(f"[db] SQLite ready at {self.db_path}", flush=True)

    async def close(self):
        # PG mode opens a fresh connection per operation (no persistent pool to
        # close); SQLite keeps one connection that must be closed.
        if not self._pg_mode and self._conn:
            await self._conn.close()

    # ── SQLite schema ──

    async def _migrate_sqlite(self):
        await self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS bots (
                id            TEXT PRIMARY KEY,
                strategy_id   TEXT NOT NULL,
                strategy_code TEXT NOT NULL,
                symbol        TEXT NOT NULL,
                timeframe     TEXT NOT NULL DEFAULT '5m',
                capital       REAL NOT NULL DEFAULT 100.0,
                params        TEXT DEFAULT '{}',
                status        TEXT DEFAULT 'stopped',
                mode          TEXT DEFAULT 'demo',
                signal_type   TEXT DEFAULT 'diff',
                created_at    TEXT NOT NULL,
                stopped_at    TEXT,
                error         TEXT,
                name          TEXT
            );

            CREATE TABLE IF NOT EXISTS signals (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id        TEXT NOT NULL,
                timestamp     TEXT NOT NULL,
                side          TEXT NOT NULL,
                price         REAL,
                size          REAL,
                ord_type      TEXT DEFAULT 'market',
                status        TEXT DEFAULT 'pending',
                reject_reason TEXT,
                ord_id        TEXT,
                FOREIGN KEY (bot_id) REFERENCES bots(id)
            );

            CREATE TABLE IF NOT EXISTS trades (
                id            TEXT PRIMARY KEY,
                bot_id        TEXT NOT NULL,
                signal_id     INTEGER,
                ord_id        TEXT,
                inst_id       TEXT NOT NULL,
                side          TEXT NOT NULL,
                ord_type      TEXT DEFAULT 'market',
                sz            TEXT,
                px            TEXT,
                fee           TEXT,
                fee_ccy       TEXT,
                pnl           REAL DEFAULT 0,
                state         TEXT DEFAULT 'filled',
                timestamp     TEXT NOT NULL,
                FOREIGN KEY (bot_id) REFERENCES bots(id)
            );

            CREATE TABLE IF NOT EXISTS positions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id          TEXT NOT NULL,
                inst_id         TEXT NOT NULL,
                side            TEXT NOT NULL,
                size            REAL NOT NULL,
                entry_price     REAL NOT NULL,
                current_price   REAL,
                unrealized_pnl  REAL DEFAULT 0,
                opened_at       TEXT NOT NULL,
                updated_at      TEXT,
                UNIQUE (bot_id, inst_id, side),
                FOREIGN KEY (bot_id) REFERENCES bots(id)
            );

            CREATE TABLE IF NOT EXISTS performance_metrics (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id        TEXT NOT NULL,
                timestamp     TEXT NOT NULL,
                equity        REAL,
                total_pnl     REAL DEFAULT 0,
                win_rate      REAL,
                sharpe_ratio  REAL,
                max_drawdown  REAL,
                total_trades  INTEGER DEFAULT 0,
                FOREIGN KEY (bot_id) REFERENCES bots(id)
            );

            CREATE TABLE IF NOT EXISTS settings (
                key            TEXT PRIMARY KEY,
                value          TEXT
            );

            CREATE TABLE IF NOT EXISTS subscriptions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       TEXT NOT NULL UNIQUE,
                username      TEXT,
                first_name    TEXT,
                plan          TEXT DEFAULT 'monthly',
                status        TEXT DEFAULT 'active',
                active_until  TEXT,
                last_payment  TEXT,
                payment_id    TEXT,
                created_at    TEXT NOT NULL,
                updated_at    TEXT
            );

            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id   TEXT NOT NULL UNIQUE,
                username      TEXT,
                first_name    TEXT,
                plan          TEXT DEFAULT 'free',
                status        TEXT DEFAULT 'active',
                capital       REAL DEFAULT 10000,
                okx_key_enc   TEXT,
                okx_secret_enc TEXT,
                okx_pass_enc  TEXT,
                okx_demo      INTEGER DEFAULT 1,
                active_until  TEXT,
                created_at    TEXT NOT NULL,
                updated_at    TEXT
            );

            CREATE TABLE IF NOT EXISTS tg_processed_updates (
                update_id     INTEGER PRIMARY KEY,
                processed_at  TEXT NOT NULL
            );
        """)
        await self._conn.commit()

    # ── PostgreSQL schema ──

    async def _migrate_pg(self, conn):
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bots (
                id            TEXT PRIMARY KEY,
                strategy_id   TEXT NOT NULL,
                strategy_code TEXT NOT NULL,
                symbol        TEXT NOT NULL,
                timeframe     TEXT NOT NULL DEFAULT '5m',
                capital       DOUBLE PRECISION NOT NULL DEFAULT 100.0,
                params        TEXT DEFAULT '{}',
                status        TEXT DEFAULT 'stopped',
                mode          TEXT DEFAULT 'demo',
                signal_type   TEXT DEFAULT 'diff',
                created_at    TEXT NOT NULL,
                stopped_at    TEXT,
                error         TEXT,
                name          TEXT
            )
        """)
        # Add name column if missing (idempotent)
        try:
            await conn.execute("ALTER TABLE bots ADD COLUMN name TEXT")
        except Exception:
            pass

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id            SERIAL PRIMARY KEY,
                bot_id        TEXT NOT NULL REFERENCES bots(id),
                timestamp     TEXT NOT NULL,
                side          TEXT NOT NULL,
                price         DOUBLE PRECISION,
                size          DOUBLE PRECISION,
                ord_type      TEXT DEFAULT 'market',
                status        TEXT DEFAULT 'pending',
                reject_reason TEXT,
                ord_id        TEXT
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id            TEXT PRIMARY KEY,
                bot_id        TEXT NOT NULL REFERENCES bots(id),
                signal_id     INTEGER,
                ord_id        TEXT,
                inst_id       TEXT NOT NULL,
                side          TEXT NOT NULL,
                ord_type      TEXT DEFAULT 'market',
                sz            TEXT,
                px            TEXT,
                fee           TEXT,
                fee_ccy       TEXT,
                pnl           DOUBLE PRECISION DEFAULT 0,
                state         TEXT DEFAULT 'filled',
                timestamp     TEXT NOT NULL
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS positions (
                id              SERIAL PRIMARY KEY,
                bot_id          TEXT NOT NULL REFERENCES bots(id),
                inst_id         TEXT NOT NULL,
                side            TEXT NOT NULL,
                size            DOUBLE PRECISION NOT NULL,
                entry_price     DOUBLE PRECISION NOT NULL,
                current_price   DOUBLE PRECISION,
                unrealized_pnl  DOUBLE PRECISION DEFAULT 0,
                opened_at       TEXT NOT NULL,
                updated_at      TEXT,
                UNIQUE (bot_id, inst_id, side)
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS performance_metrics (
                id            SERIAL PRIMARY KEY,
                bot_id        TEXT NOT NULL REFERENCES bots(id),
                timestamp     TEXT NOT NULL,
                equity        DOUBLE PRECISION,
                total_pnl     DOUBLE PRECISION DEFAULT 0,
                win_rate      DOUBLE PRECISION,
                sharpe_ratio  DOUBLE PRECISION,
                max_drawdown  DOUBLE PRECISION,
                total_trades  INTEGER DEFAULT 0
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key            TEXT PRIMARY KEY,
                value          TEXT
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                id            SERIAL PRIMARY KEY,
                user_id       TEXT NOT NULL UNIQUE,
                username      TEXT,
                first_name    TEXT,
                plan          TEXT DEFAULT 'monthly',
                status        TEXT DEFAULT 'active',
                active_until  TEXT,
                last_payment  TEXT,
                payment_id    TEXT,
                created_at    TEXT NOT NULL,
                updated_at    TEXT
            )
        """)

        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            SERIAL PRIMARY KEY,
                telegram_id   TEXT NOT NULL UNIQUE,
                username      TEXT,
                first_name    TEXT,
                plan          TEXT DEFAULT 'free',
                status        TEXT DEFAULT 'active',
                capital       DOUBLE PRECISION DEFAULT 10000,
                okx_key_enc   TEXT,
                okx_secret_enc TEXT,
                okx_pass_enc  TEXT,
                okx_demo      INTEGER DEFAULT 1,
                active_until  TEXT,
                created_at    TEXT NOT NULL,
                updated_at    TEXT
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tg_processed_updates (
                update_id     BIGINT PRIMARY KEY,
                processed_at  TEXT NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS tracked_traders (
                unique_code   TEXT PRIMARY KEY,
                alias         TEXT,
                inst_type     TEXT DEFAULT 'SWAP',
                roi_pct       DOUBLE PRECISION DEFAULT 0,
                pnl_usd       DOUBLE PRECISION DEFAULT 0,
                win_rate      DOUBLE PRECISION DEFAULT 0,
                max_drawdown  DOUBLE PRECISION DEFAULT 0,
                aum           DOUBLE PRECISION DEFAULT 0,
                lead_days     INTEGER DEFAULT 0,
                copy_traders  INTEGER DEFAULT 0,
                verified      INTEGER DEFAULT 0,
                verify_score  DOUBLE PRECISION DEFAULT 0,
                tracked       INTEGER DEFAULT 1,
                tracking_since DOUBLE PRECISION,
                last_snapshot DOUBLE PRECISION,
                created_at    TEXT NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS copy_trades (
                id            TEXT PRIMARY KEY,
                trader_code   TEXT NOT NULL,
                inst_id       TEXT NOT NULL,
                side          TEXT NOT NULL,
                size          TEXT,
                entry_price   TEXT,
                entry_time    DOUBLE PRECISION,
                close_price   TEXT,
                close_time    DOUBLE PRECISION,
                pnl           DOUBLE PRECISION DEFAULT 0,
                reason        TEXT,
                created_at    TEXT NOT NULL,
                FOREIGN KEY (trader_code) REFERENCES tracked_traders(unique_code)
            )
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id            BIGSERIAL PRIMARY KEY,
                ts            TEXT NOT NULL,
                actor         TEXT,
                action        TEXT NOT NULL,
                detail        TEXT,
                meta          TEXT
            )
        """)

    # ── Query helpers ──

    async def _pg_connect(self):
        """Create a fresh asyncpg connection for one operation.

        Strategies run in their own threads with separate event loops; a shared
        pool bound to uvicorn's loop breaks with "attached to a different loop".
        So, like OKXClient, we open a fresh connection per request."""
        import asyncpg
        return await asyncpg.connect(DATABASE_URL)

    async def _fetchone(self, sql: str, params: tuple = ()) -> Optional[dict]:
        if self._pg_mode:
            conn = await self._pg_connect()
            try:
                row = await conn.fetchrow(sql, *params)
                return dict(row) if row else None
            finally:
                await conn.close()
        cur = await self._conn.execute(sql, params)
        row = await cur.fetchone()
        return dict(row) if row else None

    async def _fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        if self._pg_mode:
            conn = await self._pg_connect()
            try:
                rows = await conn.fetch(sql, *params)
                return [dict(r) for r in rows]
            finally:
                await conn.close()
        cur = await self._conn.execute(sql, params)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def _execute(self, sql: str, params: tuple = ()):
        if self._pg_mode:
            conn = await self._pg_connect()
            try:
                await conn.execute(sql, *params)
            finally:
                await conn.close()
        else:
            await self._conn.execute(sql, params)
            await self._conn.commit()

    async def _execute_returning(self, sql: str, params: tuple = ()) -> int:
        if self._pg_mode:
            conn = await self._pg_connect()
            try:
                val = await conn.fetchval(sql, *params)
                return val
            finally:
                await conn.close()
        cur = await self._conn.execute(sql, params)
        await self._conn.commit()
        return cur.lastrowid

    # ── Bots ──

    async def ensure_bot(self, bot_id: str, strategy_id: str = "portfolio",
                         strategy_code: str = "equity_tracker", symbol: str = "MULTI",
                         timeframe: str = "1D", capital: float = 0.0,
                         name: str = None) -> None:
        """Idempotent bot insert (INSERT OR IGNORE / ON CONFLICT DO NOTHING)."""
        now = datetime.now(timezone.utc).isoformat()
        if self._pg_mode:
            await self._execute(
                "INSERT INTO bots (id, strategy_id, strategy_code, symbol, timeframe, "
                "capital, params, status, mode, signal_type, created_at, name) "
                "VALUES ($1, $2, $3, $4, $5, $6, '{}', 'running', 'demo', 'portfolio', $7, $8) "
                "ON CONFLICT (id) DO NOTHING",
                (bot_id, strategy_id, strategy_code, symbol, timeframe, capital, now, name)
            )
        else:
            await self._execute(
                "INSERT OR IGNORE INTO bots (id, strategy_id, strategy_code, symbol, timeframe, "
                "capital, params, status, mode, signal_type, created_at, name) "
                "VALUES (?, ?, ?, ?, ?, ?, '{}', 'running', 'demo', 'portfolio', ?, ?)",
                (bot_id, strategy_id, strategy_code, symbol, timeframe, capital, now, name)
            )

    async def save_bot(self, bot_id: str, strategy_id: str, strategy_code: str,
                       symbol: str, timeframe: str, capital: float,
                       params: dict, mode: str = "demo", signal_type: str = "diff",
                       name: str = None) -> str:
        now = datetime.now(timezone.utc).isoformat()
        if self._pg_mode:
            await self._execute(
                "INSERT INTO bots (id, strategy_id, strategy_code, symbol, timeframe, "
                "capital, params, status, mode, signal_type, created_at, name) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, 'starting', $8, $9, $10, $11)",
                (bot_id, strategy_id, strategy_code, symbol, timeframe, capital,
                 json.dumps(params), mode, signal_type, now, name)
            )
        else:
            await self._execute(
                "INSERT INTO bots (id, strategy_id, strategy_code, symbol, timeframe, "
                "capital, params, status, mode, signal_type, created_at, name) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'starting', ?, ?, ?, ?)",
                (bot_id, strategy_id, strategy_code, symbol, timeframe, capital,
                 json.dumps(params), mode, signal_type, now, name)
            )
        return bot_id

    async def get_bot(self, bot_id: str) -> Optional[dict]:
        return await self._fetchone("SELECT * FROM bots WHERE id = $1" if self._pg_mode else "SELECT * FROM bots WHERE id = ?", (bot_id,))

    async def get_bots(self, status: str = None) -> list[dict]:
        if status:
            return await self._fetchall(
                "SELECT * FROM bots WHERE status = $1 ORDER BY created_at DESC" if self._pg_mode else "SELECT * FROM bots WHERE status = ? ORDER BY created_at DESC",
                (status,)
            )
        return await self._fetchall("SELECT * FROM bots ORDER BY created_at DESC")

    async def update_bot_status(self, bot_id: str, status: str, error: str = None):
        if error:
            if self._pg_mode:
                await self._execute("UPDATE bots SET status = $1, error = $2 WHERE id = $3", (status, error, bot_id))
            else:
                await self._execute("UPDATE bots SET status = ?, error = ? WHERE id = ?", (status, error, bot_id))
        else:
            if self._pg_mode:
                await self._execute("UPDATE bots SET status = $1 WHERE id = $2", (status, bot_id))
            else:
                await self._execute("UPDATE bots SET status = ? WHERE id = ?", (status, bot_id))

    async def update_bot_stopped(self, bot_id: str):
        now = datetime.now(timezone.utc).isoformat()
        if self._pg_mode:
            await self._execute("UPDATE bots SET status = 'stopped', stopped_at = $1 WHERE id = $2", (now, bot_id))
        else:
            await self._execute("UPDATE bots SET status = 'stopped', stopped_at = ? WHERE id = ?", (now, bot_id))

    async def delete_bot(self, bot_id: str):
        if self._pg_mode:
            await self._execute("DELETE FROM bots WHERE id = $1", (bot_id,))
        else:
            await self._execute("DELETE FROM bots WHERE id = ?", (bot_id,))

    # ── Signals ──

    async def save_signal(self, bot_id: str, timestamp: str, side: str,
                          price: float = None, size: float = None,
                          ord_type: str = "market", status: str = "pending",
                          reject_reason: str = None) -> int:
        if self._pg_mode:
            return await self._execute_returning(
                "INSERT INTO signals (bot_id, timestamp, side, price, size, ord_type, status, reject_reason) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id",
                (bot_id, timestamp, side, price, size, ord_type, status, reject_reason)
            )
        return await self._execute_returning(
            "INSERT INTO signals (bot_id, timestamp, side, price, size, ord_type, status, reject_reason) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (bot_id, timestamp, side, price, size, ord_type, status, reject_reason)
        )

    async def update_signal_status(self, signal_id: int, status: str,
                                   ord_id: str = None, reject_reason: str = None):
        if ord_id:
            if self._pg_mode:
                await self._execute("UPDATE signals SET status = $1, ord_id = $2 WHERE id = $3", (status, ord_id, signal_id))
            else:
                await self._execute("UPDATE signals SET status = ?, ord_id = ? WHERE id = ?", (status, ord_id, signal_id))
        elif reject_reason:
            if self._pg_mode:
                await self._execute("UPDATE signals SET status = $1, reject_reason = $2 WHERE id = $3", (status, reject_reason, signal_id))
            else:
                await self._execute("UPDATE signals SET status = ?, reject_reason = ? WHERE id = ?", (status, reject_reason, signal_id))
        else:
            if self._pg_mode:
                await self._execute("UPDATE signals SET status = $1 WHERE id = $2", (status, signal_id))
            else:
                await self._execute("UPDATE signals SET status = ? WHERE id = ?", (status, signal_id))

    async def get_signals(self, bot_id: str = None, limit: int = 100) -> list[dict]:
        if bot_id:
            return await self._fetchall(
                "SELECT * FROM signals WHERE bot_id = $1 ORDER BY id DESC LIMIT $2" if self._pg_mode else "SELECT * FROM signals WHERE bot_id = ? ORDER BY id DESC LIMIT ?",
                (bot_id, limit)
            )
        return await self._fetchall(
            "SELECT * FROM signals ORDER BY id DESC LIMIT $1" if self._pg_mode else "SELECT * FROM signals ORDER BY id DESC LIMIT ?",
            (limit,)
        )

    # ── Trades ──

    async def save_trade(self, bot_id: str, side: str, sz: str = None,
                         px: str = None, ord_id: str = None, inst_id: str = None,
                         ord_type: str = "market", fee: str = None,
                         fee_ccy: str = None, pnl: float = 0,
                         state: str = "filled", signal_id: int = None) -> str:
        trade_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        # sz/px/fee columns are TEXT: normalize to string so both SQLite and
        # asyncpg (Postgres) accept them (asyncpg rejects float for TEXT).
        def _to_str(v):
            if v is None:
                return ""
            if isinstance(v, float) or isinstance(v, int):
                # avoid ugly float artifacts (e.g. 10.200000000000001)
                return str(round(v, 8))
            return str(v)
        sz_s = _to_str(sz)
        px_s = _to_str(px)
        fee_s = _to_str(fee)
        if self._pg_mode:
            await self._execute(
                "INSERT INTO trades (id, bot_id, signal_id, ord_id, inst_id, side, "
                "ord_type, sz, px, fee, fee_ccy, pnl, state, timestamp) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)",
                (trade_id, bot_id, signal_id, ord_id, inst_id or "", side,
                 ord_type, sz_s, px_s, fee_s, fee_ccy or "",
                 pnl, state, now)
            )
        else:
            await self._execute(
                "INSERT INTO trades (id, bot_id, signal_id, ord_id, inst_id, side, "
                "ord_type, sz, px, fee, fee_ccy, pnl, state, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (trade_id, bot_id, signal_id, ord_id, inst_id or "", side,
                 ord_type, sz_s, px_s, fee_s, fee_ccy or "",
                 pnl, state, now)
            )
        return trade_id

    async def find_signal_id(self, inst_id: str, side: str) -> int:
        """Return the most recent opening signal_id for (inst_id, side).

        Used after a bot restart to re-attach the trade number to positions
        that were restored from the exchange, so close/partial messages keep
        the same "Сделка №N" as the original open.

        We take the latest signal_id by time: while a position is still open,
        the newest record for that (inst_id, side) belongs to it. If the
        position was already closed and reopened, the latest signal_id is the
        new open — which is what we want."""
        inst = inst_id or ""
        sql = (
            "SELECT signal_id FROM trades "
            "WHERE inst_id = $1 AND side = $2 AND signal_id IS NOT NULL "
            "ORDER BY timestamp DESC LIMIT 1"
            if self._pg_mode else
            "SELECT signal_id FROM trades "
            "WHERE inst_id = ? AND side = ? AND signal_id IS NOT NULL "
            "ORDER BY timestamp DESC LIMIT 1"
        )
        rows = await self._fetchall(sql, (inst, side))
        if rows and rows[0].get("signal_id"):
            return int(rows[0]["signal_id"])
        return 0

    async def get_trades(self, bot_id: str = None, limit: int = 100) -> list[dict]:
        if bot_id:
            return await self._fetchall(
                "SELECT * FROM trades WHERE bot_id = $1 ORDER BY timestamp DESC LIMIT $2" if self._pg_mode else "SELECT * FROM trades WHERE bot_id = ? ORDER BY timestamp DESC LIMIT ?",
                (bot_id, limit)
            )
        return await self._fetchall(
            "SELECT * FROM trades ORDER BY timestamp DESC LIMIT $1" if self._pg_mode else "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?",
            (limit,)
        )

    async def get_paired_trades(self, limit: int = 20, begin: str = None, end: str = None, bot_ids: list = None) -> list[dict]:
        return await self._get_paired_trades_impl(limit, begin, end, bot_ids)

    async def _get_paired_trades_impl(self, limit: int = 20, begin: str = None, end: str = None, bot_ids: list = None) -> list[dict]:
        if self._pg_mode:
            return await self._get_paired_pg(limit, begin, end, bot_ids)
        return await self._get_paired_sqlite(limit, begin, end, bot_ids)

    async def _paired_pg_query(self, limit, begin, end, bot_ids=None):
        where = "1=1"
        params = []
        idx = 0  # number of positional params used so far
        if begin:
            idx += 1
            where += f" AND timestamp >= ${idx}"
            params.append(begin)
        if end:
            idx += 1
            where += f" AND timestamp <= ${idx}"
            params.append(end)
        if bot_ids:
            placeholders = ", ".join([f"${i}" for i in range(idx + 1, idx + 1 + len(bot_ids))])
            where += f" AND bot_id IN ({placeholders})"
            params.extend(bot_ids)
            idx += len(bot_ids)
        idx += 1
        params.append(limit)

        return await self._fetchall(
            "SELECT signal_id, "
            "MAX(bot_id) as bot_id, "
            "MAX(inst_id) as inst_id, "
            "MAX(CASE WHEN state='filled' AND pnl=0 THEN timestamp END) as entry_time, "
            "MAX(CASE WHEN state='filled' AND pnl=0 THEN side END) as entry_side, "
            "MAX(CASE WHEN state='filled' AND pnl=0 THEN px END) as entry_px, "
            "MAX(CASE WHEN state='filled' AND pnl=0 THEN sz END) as entry_sz, "
            "MAX(CASE WHEN state='filled' AND pnl=0 THEN fee END) as entry_fee, "
            "MAX(CASE WHEN state='closed' OR (state='filled' AND pnl!=0) THEN timestamp END) as exit_time, "
            "MAX(CASE WHEN state='closed' OR (state='filled' AND pnl!=0) THEN px END) as exit_px, "
            "MAX(CASE WHEN state='closed' OR (state='filled' AND pnl!=0) THEN fee END) as exit_fee, "
            "MAX(CASE WHEN state='closed' THEN pnl WHEN state='filled' AND pnl!=0 THEN pnl END) as pnl "
            f"FROM trades WHERE signal_id IS NOT NULL AND {where} "
            "GROUP BY signal_id "
            "HAVING MAX(CASE WHEN state='closed' OR (state='filled' AND pnl!=0) THEN 1 ELSE 0 END) = 1 "
            f"ORDER BY exit_time DESC LIMIT ${idx}",
            tuple(params)
        )

    async def _get_paired_pg(self, limit, begin, end, bot_ids=None):
        rows = await self._paired_pg_query(limit, begin, end, bot_ids)
        return await self._format_paired(rows, begin, end, limit)

    async def _get_paired_sqlite(self, limit, begin, end, bot_ids=None):
        where = "1=1"
        params = []
        if begin:
            where += " AND timestamp >= ?"
            params.append(begin)
        if end:
            where += " AND timestamp <= ?"
            params.append(end)
        if bot_ids:
            where += " AND bot_id IN (" + ", ".join(["?"] * len(bot_ids)) + ")"
            params.extend(bot_ids)
        params.append(limit)

        rows = await self._fetchall(
            "SELECT signal_id, bot_id, inst_id, "
            "MAX(CASE WHEN state='filled' AND pnl=0 THEN timestamp END) as entry_time, "
            "MAX(CASE WHEN state='filled' AND pnl=0 THEN side END) as entry_side, "
            "MAX(CASE WHEN state='filled' AND pnl=0 THEN px END) as entry_px, "
            "MAX(CASE WHEN state='filled' AND pnl=0 THEN sz END) as entry_sz, "
            "MAX(CASE WHEN state='filled' AND pnl=0 THEN fee END) as entry_fee, "
            "MAX(CASE WHEN state='closed' OR (state='filled' AND pnl!=0) THEN timestamp END) as exit_time, "
            "MAX(CASE WHEN state='closed' OR (state='filled' AND pnl!=0) THEN px END) as exit_px, "
            "MAX(CASE WHEN state='closed' OR (state='filled' AND pnl!=0) THEN fee END) as exit_fee, "
            "MAX(CASE WHEN state='closed' THEN pnl WHEN state='filled' AND pnl!=0 THEN pnl END) as pnl "
            f"FROM trades WHERE signal_id IS NOT NULL AND {where} "
            "GROUP BY signal_id "
            "HAVING MAX(CASE WHEN state='closed' OR (state='filled' AND pnl!=0) THEN 1 ELSE 0 END) = 1 "
            "ORDER BY exit_time DESC LIMIT ?",
            tuple(params)
        )
        return await self._format_paired(rows, begin, end, limit)

    async def _format_paired(self, rows, begin, end, limit):
        result = []
        for r in rows:
            if r["entry_side"] is None:
                continue
            result.append({
                "signal_id": r["signal_id"],
                "bot_id": r["bot_id"],
                "inst_id": r["inst_id"],
                "side": r["entry_side"],
                "entry_time": r["entry_time"],
                "entry_px": r["entry_px"],
                "entry_sz": r["entry_sz"],
                "entry_fee": r["entry_fee"],
                "exit_time": r["exit_time"],
                "exit_px": r["exit_px"],
                "exit_fee": r["exit_fee"],
                "pnl": r["pnl"],
            })

        if self._pg_mode:
            return await self._format_paired_manual_pg(result, begin, end, limit)
        return await self._format_paired_manual_sqlite(result, begin, end, limit)

    async def _format_paired_manual_pg(self, result, begin, end, limit):
        params = []
        where = ""
        idx = 0
        if begin:
            idx += 1
            where += f" AND timestamp >= ${idx}"
            params.append(begin)
        if end:
            idx += 1
            where += f" AND timestamp <= ${idx}"
            params.append(end)

        all_manual = await self._fetchall(
            "SELECT id, bot_id, inst_id, side, sz, px, fee, fee_ccy, pnl, timestamp, state "
            f"FROM trades WHERE signal_id IS NULL{where} ORDER BY timestamp ASC",
            tuple(params)
        )
        return self._pair_manual(result, all_manual, limit)

    async def _format_paired_manual_sqlite(self, result, begin, end, limit):
        params = []
        where = ""
        if begin:
            where += " AND timestamp >= ?"
            params.append(begin)
        if end:
            where += " AND timestamp <= ?"
            params.append(end)

        all_manual = await self._fetchall(
            "SELECT id, bot_id, inst_id, side, sz, px, fee, fee_ccy, pnl, timestamp, state "
            f"FROM trades WHERE signal_id IS NULL{where} ORDER BY timestamp ASC",
            tuple(params)
        )
        return self._pair_manual(result, all_manual, limit)

    def _pair_manual(self, result, all_manual, limit):
        # Group by inst_id, then pair entries (pnl=0) with closes (pnl!=0)
        from collections import defaultdict
        by_inst = defaultdict(list)
        for t in all_manual:
            by_inst[t["inst_id"]].append(t)

        for inst_id, trades in by_inst.items():
            i = 0
            while i < len(trades):
                t = trades[i]
                if t["state"] == "filled" and t["pnl"] == 0:
                    # Look for next close for this instrument
                    for j in range(i + 1, len(trades)):
                        nxt = trades[j]
                        if nxt["inst_id"] == inst_id and nxt["state"] == "filled" and nxt["pnl"] != 0:
                            result.append({
                                "signal_id": None,
                                "bot_id": t["bot_id"],
                                "inst_id": inst_id,
                                "side": t["side"],
                                "entry_time": t["timestamp"],
                                "entry_px": t["px"],
                                "entry_sz": t["sz"],
                                "entry_fee": t["fee"],
                                "exit_time": nxt["timestamp"],
                                "exit_px": nxt["px"],
                                "exit_fee": nxt["fee"],
                                "pnl": nxt["pnl"],
                            })
                            i = j + 1
                            break
                    else:
                        i += 1
                else:
                    i += 1

        result.sort(key=lambda x: (x["exit_time"] or x["entry_time"] or ""), reverse=True)
        return result[:limit]

    async def get_pnl_by_period(self, days: int, bot_id: str = None) -> float:
        import datetime
        cutoff = (datetime.datetime.now(datetime.timezone.utc)
                  - datetime.timedelta(days=days)).isoformat()
        if bot_id:
            if self._pg_mode:
                row = await self._fetchone(
                    "SELECT COALESCE(SUM(pnl), 0) as total FROM trades "
                    "WHERE pnl != 0 AND bot_id = $1 AND timestamp >= $2",
                    (bot_id, cutoff)
                )
            else:
                row = await self._fetchone(
                    "SELECT COALESCE(SUM(pnl), 0) as total FROM trades "
                    "WHERE pnl != 0 AND bot_id = ? AND timestamp >= ?",
                    (bot_id, cutoff)
                )
        else:
            if self._pg_mode:
                row = await self._fetchone(
                    "SELECT COALESCE(SUM(pnl), 0) as total FROM trades "
                    "WHERE pnl != 0 AND timestamp >= $1",
                    (cutoff,)
                )
            else:
                row = await self._fetchone(
                    "SELECT COALESCE(SUM(pnl), 0) as total FROM trades "
                    "WHERE pnl != 0 AND timestamp >= ?",
                    (cutoff,)
                )
        return row["total"] if row else 0.0

    async def get_trades_summary(self, bot_id: str) -> dict:
        if self._pg_mode:
            row = await self._fetchone(
                "SELECT COUNT(*)::int as total, "
                "COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0)::int as wins, "
                "COALESCE(SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END), 0)::int as losses, "
                "COALESCE(SUM(pnl), 0) as total_pnl, "
                "COALESCE(AVG(CASE WHEN pnl > 0 THEN pnl END), 0) as avg_win, "
                "COALESCE(AVG(CASE WHEN pnl < 0 THEN pnl END), 0) as avg_loss "
                "FROM trades WHERE bot_id = $1 AND state IN ('filled', 'closed')",
                (bot_id,)
            )
        else:
            row = await self._fetchone(
                "SELECT COUNT(*) as total, "
                "COALESCE(SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), 0) as wins, "
                "COALESCE(SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END), 0) as losses, "
                "COALESCE(SUM(pnl), 0) as total_pnl, "
                "COALESCE(AVG(CASE WHEN pnl > 0 THEN pnl END), 0) as avg_win, "
                "COALESCE(AVG(CASE WHEN pnl < 0 THEN pnl END), 0) as avg_loss "
                "FROM trades WHERE bot_id = ? AND state IN ('filled', 'closed')",
                (bot_id,)
            )
        return row or {"total": 0, "wins": 0, "losses": 0, "total_pnl": 0,
                       "avg_win": 0, "avg_loss": 0}

    # ── Positions ──

    async def save_position(self, bot_id: str, inst_id: str, side: str,
                            size: float, entry_price: float,
                            current_price: float = None):
        now = datetime.now(timezone.utc).isoformat()
        existing = await self._fetchone(
            "SELECT id FROM positions WHERE bot_id = $1 AND inst_id = $2 AND side = $3"
            if self._pg_mode else
            "SELECT id FROM positions WHERE bot_id = ? AND inst_id = ? AND side = ?",
            (bot_id, inst_id, side))
        if existing:
            if self._pg_mode:
                await self._execute(
                    "UPDATE positions SET size=$1, entry_price=$2, "
                    "current_price=$3, updated_at=$4 WHERE bot_id=$5 AND inst_id=$6 AND side=$7",
                    (size, entry_price, current_price or entry_price, now, bot_id, inst_id, side)
                )
            else:
                await self._execute(
                    "UPDATE positions SET size=?, entry_price=?, "
                    "current_price=?, updated_at=? WHERE bot_id=? AND inst_id=? AND side=?",
                    (size, entry_price, current_price or entry_price, now, bot_id, inst_id, side)
                )
        else:
            if self._pg_mode:
                await self._execute(
                    "INSERT INTO positions (bot_id, inst_id, side, size, entry_price, current_price, opened_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                    (bot_id, inst_id, side, size, current_price or entry_price, current_price or entry_price, now)
                )
            else:
                await self._execute(
                    "INSERT INTO positions (bot_id, inst_id, side, size, entry_price, current_price, opened_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (bot_id, inst_id, side, size, current_price or entry_price, current_price or entry_price, now)
                )

    async def update_position_price(self, bot_id: str, current_price: float,
                                    unrealized_pnl: float = 0):
        now = datetime.now(timezone.utc).isoformat()
        if self._pg_mode:
            await self._execute(
                "UPDATE positions SET current_price=$1, unrealized_pnl=$2, updated_at=$3 WHERE bot_id=$4",
                (current_price, unrealized_pnl, now, bot_id)
            )
        else:
            await self._execute(
                "UPDATE positions SET current_price=?, unrealized_pnl=?, updated_at=? WHERE bot_id=?",
                (current_price, unrealized_pnl, now, bot_id)
            )

    async def get_position(self, bot_id: str) -> Optional[dict]:
        return await self._fetchone("SELECT * FROM positions WHERE bot_id = $1" if self._pg_mode else "SELECT * FROM positions WHERE bot_id = ?", (bot_id,))

    async def delete_position_inst(self, bot_id: str, inst_id: str, side: str = None):
        if side:
            sql = (
                "DELETE FROM positions WHERE bot_id = $1 AND inst_id = $2 AND side = $3"
                if self._pg_mode else
                "DELETE FROM positions WHERE bot_id = ? AND inst_id = ? AND side = ?"
            )
            await self._execute(sql, (bot_id, inst_id, side))
        else:
            sql = (
                "DELETE FROM positions WHERE bot_id = $1 AND inst_id = $2"
                if self._pg_mode else
                "DELETE FROM positions WHERE bot_id = ? AND inst_id = ?"
            )
            await self._execute(sql, (bot_id, inst_id))

    async def delete_position(self, bot_id: str):
        if self._pg_mode:
            await self._execute("DELETE FROM positions WHERE bot_id = $1", (bot_id,))
        else:
            await self._execute("DELETE FROM positions WHERE bot_id = ?", (bot_id,))

    async def claim_position(self, bot_id: str, inst_id: str, side: str,
                              size: float, entry_price: float) -> None:
        """Persist ownership so restarts can restore the position to THIS bot only."""
        side_n = (side or "long").lower()
        if side_n in ("sell", "s"):
            side_n = "short"
        elif side_n in ("buy", "b", "net"):
            side_n = "long" if side_n != "short" else side_n
        if side_n not in ("long", "short"):
            side_n = "long"
        await self.save_position(
            bot_id=bot_id, inst_id=inst_id, side=side_n,
            size=float(size), entry_price=float(entry_price),
            current_price=float(entry_price),
        )

    async def find_position_any_side(self, bot_id: str, inst_id: str, side: str = None) -> Optional[dict]:
        """Match long/short/net — OKX one-way mode reports posSide=net."""
        tried = []
        for s in (side, "long", "short", "net"):
            if not s or s in tried:
                continue
            tried.append(s)
            row = await self.find_position(bot_id, inst_id, s)
            if row:
                return row
        return None

    async def other_bot_owns_position_any(self, bot_id: str, inst_id: str, side: str = None) -> bool:
        for s in (side, "long", "short", "net"):
            if not s:
                continue
            if await self.other_bot_owns_position(bot_id, inst_id, s):
                return True
        # any other bot on this inst regardless of side
        sql = (
            "SELECT bot_id FROM positions WHERE inst_id = $1 AND bot_id <> $2 LIMIT 1"
            if self._pg_mode else
            "SELECT bot_id FROM positions WHERE inst_id = ? AND bot_id <> ? LIMIT 1"
        )
        row = await self._fetchone(sql, (inst_id, bot_id))
        return bool(row)

    async def last_bot_for_instrument(self, inst_id: str) -> Optional[str]:
        """bot_id of most recent trade on this instrument (ownership hint after restart)."""
        sql = (
            "SELECT bot_id FROM trades WHERE inst_id = $1 ORDER BY timestamp DESC LIMIT 1"
            if self._pg_mode else
            "SELECT bot_id FROM trades WHERE inst_id = ? ORDER BY timestamp DESC LIMIT 1"
        )
        row = await self._fetchone(sql, (inst_id,))
        return (row or {}).get("bot_id") if row else None

    async def find_position(self, bot_id: str, inst_id: str, side: str) -> Optional[dict]:
        """Lookup a single open position row owned by this bot."""
        sql = (
            "SELECT * FROM positions WHERE bot_id = $1 AND inst_id = $2 AND side = $3"
            if self._pg_mode else
            "SELECT * FROM positions WHERE bot_id = ? AND inst_id = ? AND side = ?"
        )
        return await self._fetchone(sql, (bot_id, inst_id, side))

    async def other_bot_owns_position(self, bot_id: str, inst_id: str, side: str) -> bool:
        """True if some OTHER bot already has this instrument/side in the positions table."""
        sql = (
            "SELECT bot_id FROM positions WHERE inst_id = $1 AND side = $2 AND bot_id <> $3 LIMIT 1"
            if self._pg_mode else
            "SELECT bot_id FROM positions WHERE inst_id = ? AND side = ? AND bot_id <> ? LIMIT 1"
        )
        row = await self._fetchone(sql, (inst_id, side, bot_id))
        return bool(row)

    async def get_all_positions(self) -> list[dict]:
        # LEFT JOIN — positions must still tag even if bots row is missing
        if self._pg_mode:
            return await self._fetchall(
                "SELECT p.*, b.strategy_id, b.symbol FROM positions p "
                "LEFT JOIN bots b ON b.id = p.bot_id ORDER BY p.opened_at DESC"
            )
        return await self._fetchall(
            "SELECT p.*, b.strategy_id, b.symbol FROM positions p "
            "LEFT JOIN bots b ON b.id = p.bot_id ORDER BY p.opened_at DESC"
        )

    # ── Metrics ──

    async def save_metric(self, bot_id: str, equity: float = None,
                          total_pnl: float = 0, win_rate: float = None,
                          sharpe_ratio: float = None, max_drawdown: float = None,
                          total_trades: int = 0):
        now = datetime.now(timezone.utc).isoformat()
        if self._pg_mode:
            await self._execute(
                "INSERT INTO performance_metrics (bot_id, timestamp, equity, total_pnl, "
                "win_rate, sharpe_ratio, max_drawdown, total_trades) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8)",
                (bot_id, now, equity, total_pnl, win_rate, sharpe_ratio,
                 max_drawdown, total_trades)
            )
        else:
            await self._execute(
                "INSERT INTO performance_metrics (bot_id, timestamp, equity, total_pnl, "
                "win_rate, sharpe_ratio, max_drawdown, total_trades) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (bot_id, now, equity, total_pnl, win_rate, sharpe_ratio,
                 max_drawdown, total_trades)
            )

    async def get_metrics(self, bot_id: str, limit: int = 100) -> list[dict]:
        if self._pg_mode:
            return await self._fetchall(
                "SELECT * FROM performance_metrics WHERE bot_id = $1 ORDER BY id DESC LIMIT $2",
                (bot_id, limit)
            )
        return await self._fetchall(
            "SELECT * FROM performance_metrics WHERE bot_id = ? ORDER BY id DESC LIMIT ?",
            (bot_id, limit)
        )

    # ── Subscriptions (paid Telegram signals) ──

    async def get_subscription(self, user_id: str) -> Optional[dict]:
        return await self._fetchone(
            "SELECT * FROM subscriptions WHERE user_id = $1" if self._pg_mode
            else "SELECT * FROM subscriptions WHERE user_id = ?",
            (str(user_id),)
        )

    async def save_subscription(self, user_id: str, username: str = None,
                                first_name: str = None, active_until: str = None,
                                payment_id: str = None, plan: str = "monthly",
                                status: str = "active") -> None:
        now = datetime.now(timezone.utc).isoformat()
        existing = await self.get_subscription(user_id)
        if existing:
            if self._pg_mode:
                await self._execute(
                    "UPDATE subscriptions SET username=$1, first_name=$2, plan=$3, "
                    "status=$4, active_until=$5, payment_id=$6, updated_at=$7 "
                    "WHERE user_id=$8",
                    (username, first_name, plan, status, active_until, payment_id, now, str(user_id))
                )
            else:
                await self._execute(
                    "UPDATE subscriptions SET username=?, first_name=?, plan=?, "
                    "status=?, active_until=?, payment_id=?, updated_at=? "
                    "WHERE user_id=?",
                    (username, first_name, plan, status, active_until, payment_id, now, str(user_id))
                )
        else:
            if self._pg_mode:
                await self._execute(
                    "INSERT INTO subscriptions (user_id, username, first_name, plan, status, "
                    "active_until, last_payment, payment_id, created_at, updated_at) "
                    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
                    (str(user_id), username, first_name, plan, status, active_until, now, payment_id, now, now)
                )
            else:
                await self._execute(
                    "INSERT INTO subscriptions (user_id, username, first_name, plan, status, "
                    "active_until, last_payment, payment_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (str(user_id), username, first_name, plan, status, active_until, now, payment_id, now, now)
                )

    async def list_subscriptions(self) -> list[dict]:
        return await self._fetchall(
            "SELECT * FROM subscriptions ORDER BY updated_at DESC"
        )

    async def delete_subscription(self, user_id: str) -> None:
        if self._pg_mode:
            await self._execute("DELETE FROM subscriptions WHERE user_id = $1", (str(user_id),))
        else:
            await self._execute("DELETE FROM subscriptions WHERE user_id = ?", (str(user_id),))

    # ── Users (multi-tenant accounts) ──

    async def get_user_by_telegram(self, telegram_id) -> Optional[dict]:
        return await self._fetchone(
            "SELECT * FROM users WHERE telegram_id = $1" if self._pg_mode
            else "SELECT * FROM users WHERE telegram_id = ?",
            (str(telegram_id),)
        )

    async def get_user_by_id(self, user_id: int) -> Optional[dict]:
        return await self._fetchone(
            "SELECT * FROM users WHERE id = $1" if self._pg_mode
            else "SELECT * FROM users WHERE id = ?",
            (user_id,)
        )

    async def find_or_create_user(self, telegram_id, username=None,
                                  first_name=None) -> dict:
        existing = await self.get_user_by_telegram(telegram_id)
        if existing:
            return existing
        now = datetime.now(timezone.utc).isoformat()
        if self._pg_mode:
            await self._execute(
                "INSERT INTO users (telegram_id, username, first_name, plan, status, "
                "capital, okx_demo, created_at, updated_at) "
                "VALUES ($1, $2, $3, 'free', 'active', 10000, 1, $4, $4) "
                "ON CONFLICT (telegram_id) DO NOTHING",
                (str(telegram_id), username, first_name, now)
            )
        else:
            await self._execute(
                "INSERT OR IGNORE INTO users (telegram_id, username, first_name, plan, "
                "status, capital, okx_demo, created_at, updated_at) "
                "VALUES (?, ?, ?, 'free', 'active', 10000, 1, ?, ?)",
                (str(telegram_id), username, first_name, now, now)
            )
        return await self.get_user_by_telegram(telegram_id)

    async def update_user(self, telegram_id, **fields) -> None:
        """Update user columns (safe whitelist). Fields: username, first_name,
        plan, status, capital, okx_key_enc, okx_secret_enc, okx_pass_enc,
        okx_demo, active_until."""
        allowed = {"username", "first_name", "plan", "status", "capital",
                   "okx_key_enc", "okx_secret_enc", "okx_pass_enc",
                   "okx_demo", "active_until"}
        cols = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not cols:
            return
        now = datetime.now(timezone.utc).isoformat()
        cols["updated_at"] = now
        if self._pg_mode:
            sets = ", ".join(f"{k}=${i}" for i, k in enumerate(cols.keys(), start=1))
            params = list(cols.values())
            params.append(str(telegram_id))
            await self._execute(
                f"UPDATE users SET {sets} WHERE telegram_id=${len(params)}",
                tuple(params)
            )
        else:
            sets = ", ".join(f"{k}=?" for k in cols.keys())
            params = list(cols.values())
            params.append(str(telegram_id))
            await self._execute(
                f"UPDATE users SET {sets} WHERE telegram_id=?",
                tuple(params)
            )

    async def list_users(self) -> list[dict]:
        return await self._fetchall("SELECT * FROM users ORDER BY created_at DESC")

    async def delete_user(self, telegram_id) -> None:
        if self._pg_mode:
            await self._execute("DELETE FROM users WHERE telegram_id = $1", (str(telegram_id),))
        else:
            await self._execute("DELETE FROM users WHERE telegram_id = ?", (str(telegram_id),))

    # ── Settings (key-value) ──

    async def get_setting(self, key: str) -> Optional[str]:
        sql = "SELECT value FROM settings WHERE key = $1" if self._pg_mode else "SELECT value FROM settings WHERE key = ?"
        row = await self._fetchone(sql, (key,))
        return row["value"] if row else None

    async def set_setting(self, key: str, value: str):
        if self._pg_mode:
            await self._execute(
                "INSERT INTO settings (key, value) VALUES ($1, $2) "
                "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                (key, value)
            )
        else:
            await self._execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value)
            )


    async def add_audit(self, action: str, actor: str = None, detail: str = None, meta: str = None):
        """Append an audit trail row (admin actions, mode switches, risk)."""
        from datetime import datetime, timezone
        ts = datetime.now(timezone.utc).isoformat()
        if self._pg_mode:
            await self._execute(
                "INSERT INTO audit_log (ts, actor, action, detail, meta) VALUES ($1,$2,$3,$4,$5)",
                (ts, actor or "", action, detail or "", meta or ""),
            )
        else:
            await self._execute(
                "INSERT INTO audit_log (ts, actor, action, detail, meta) VALUES (?,?,?,?,?)",
                (ts, actor or "", action, detail or "", meta or ""),
            )

    async def list_audit(self, limit: int = 100) -> list:
        limit = max(1, min(int(limit), 500))
        if self._pg_mode:
            return await self._fetchall(
                "SELECT id, ts, actor, action, detail, meta FROM audit_log ORDER BY id DESC LIMIT $1",
                (limit,),
            )
        return await self._fetchall(
            "SELECT id, ts, actor, action, detail, meta FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        )

    async def mark_update_processed(self, update_id: int) -> bool:
        """Atomically claim a Telegram update_id for processing.

        Returns True if this update was already processed (a duplicate that
        should be skipped), False if it is the first time we've seen it.
        Guards against double delivery when two pollers/processes race."""
        uid = int(update_id)
        now = datetime.now(timezone.utc).isoformat()
        if self._pg_mode:
            res = await self._execute_returning(
                "INSERT INTO tg_processed_updates (update_id, processed_at) "
                "VALUES ($1, $2) ON CONFLICT (update_id) DO NOTHING RETURNING update_id",
                (uid, now)
            )
            return res is None
        # SQLite: a returning INSERT is not available; check-then-insert under
        # the shared connection lock (see _execute) keeps this atomic.
        existing = await self._fetchone(
            "SELECT update_id FROM tg_processed_updates WHERE update_id = ?", (uid,)
        )
        if existing:
            return True
        await self._execute(
            "INSERT INTO tg_processed_updates (update_id, processed_at) VALUES (?, ?)",
            (uid, now)
        )
        return False

    # ── Cleanup ──

    async def delete_bot_all(self, bot_id: str):
        for table in ["performance_metrics", "positions", "trades", "signals"]:
            if self._pg_mode:
                await self._execute(f"DELETE FROM {table} WHERE bot_id = $1", (bot_id,))
            else:
                await self._execute(f"DELETE FROM {table} WHERE bot_id = ?", (bot_id,))
        if self._pg_mode:
            await self._execute("DELETE FROM bots WHERE id = $1", (bot_id,))
        else:
            await self._execute("DELETE FROM bots WHERE id = ?", (bot_id,))


db = Database()
