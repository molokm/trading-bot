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

    async def init(self):
        if self._pg_mode:
            import asyncpg
            print("[db] Connecting to PostgreSQL ...", flush=True)
            last_err = None
            for attempt in range(1, 4):
                try:
                    self._pool = await asyncio.wait_for(
                        asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=3),
                        timeout=30
                    )
                    break
                except (asyncio.TimeoutError, OSError, Exception) as e:
                    last_err = e
                    print(f"[db] Attempt {attempt}/3 failed: {e}", flush=True)
                    if attempt < 3:
                        await asyncio.sleep(3 * attempt)
            else:
                raise last_err  # type: ignore
            print("[db] Connected, running migrations ...", flush=True)
            async with self._pool.acquire() as conn:
                await self._migrate_pg(conn)
            print("[db] PG init done", flush=True)
        else:
            import aiosqlite
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            self._conn = await aiosqlite.connect(self.db_path)
            self._conn.row_factory = aiosqlite.Row
            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA foreign_keys=ON")
            await self._migrate_sqlite()

    async def close(self):
        if self._pg_mode and self._pool:
            await self._pool.close()
        elif self._conn:
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
                bot_id          TEXT NOT NULL UNIQUE,
                inst_id         TEXT NOT NULL,
                side            TEXT NOT NULL,
                size            REAL NOT NULL,
                entry_price     REAL NOT NULL,
                current_price   REAL,
                unrealized_pnl  REAL DEFAULT 0,
                opened_at       TEXT NOT NULL,
                updated_at      TEXT,
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
                bot_id          TEXT NOT NULL UNIQUE REFERENCES bots(id),
                inst_id         TEXT NOT NULL,
                side            TEXT NOT NULL,
                size            DOUBLE PRECISION NOT NULL,
                entry_price     DOUBLE PRECISION NOT NULL,
                current_price   DOUBLE PRECISION,
                unrealized_pnl  DOUBLE PRECISION DEFAULT 0,
                opened_at       TEXT NOT NULL,
                updated_at      TEXT
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

    # ── Query helpers ──

    async def _fetchone(self, sql: str, params: tuple = ()) -> Optional[dict]:
        if self._pg_mode:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(sql, *params)
                return dict(row) if row else None
        cur = await self._conn.execute(sql, params)
        row = await cur.fetchone()
        return dict(row) if row else None

    async def _fetchall(self, sql: str, params: tuple = ()) -> list[dict]:
        if self._pg_mode:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(sql, *params)
                return [dict(r) for r in rows]
        cur = await self._conn.execute(sql, params)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def _execute(self, sql: str, params: tuple = ()):
        if self._pg_mode:
            async with self._pool.acquire() as conn:
                await conn.execute(sql, *params)
        else:
            await self._conn.execute(sql, params)
            await self._conn.commit()

    async def _execute_returning(self, sql: str, params: tuple = ()) -> int:
        if self._pg_mode:
            async with self._pool.acquire() as conn:
                val = await conn.fetchval(sql, *params)
                return val
        cur = await self._conn.execute(sql, params)
        await self._conn.commit()
        return cur.lastrowid

    # ── Bots ──

    async def save_bot(self, bot_id: str, strategy_id: str, strategy_code: str,
                       symbol: str, timeframe: str, capital: float,
                       params: dict, mode: str = "demo", signal_type: str = "diff",
                       name: str = None) -> str:
        now = datetime.utcnow().isoformat()
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
        now = datetime.utcnow().isoformat()
        if self._pg_mode:
            await self._execute(
                "INSERT INTO trades (id, bot_id, signal_id, ord_id, inst_id, side, "
                "ord_type, sz, px, fee, fee_ccy, pnl, state, timestamp) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)",
                (trade_id, bot_id, signal_id, ord_id, inst_id or "", side,
                 ord_type, sz or "", px or "", fee or "", fee_ccy or "",
                 pnl, state, now)
            )
        else:
            await self._execute(
                "INSERT INTO trades (id, bot_id, signal_id, ord_id, inst_id, side, "
                "ord_type, sz, px, fee, fee_ccy, pnl, state, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (trade_id, bot_id, signal_id, ord_id, inst_id or "", side,
                 ord_type, sz or "", px or "", fee or "", fee_ccy or "",
                 pnl, state, now)
            )
        return trade_id

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

    async def get_paired_trades(self, limit: int = 20, begin: str = None, end: str = None) -> list[dict]:
        return await self._get_paired_trades_impl(limit, begin, end)

    async def _get_paired_trades_impl(self, limit: int = 20, begin: str = None, end: str = None) -> list[dict]:
        if self._pg_mode:
            return await self._get_paired_pg(limit, begin, end)
        return await self._get_paired_sqlite(limit, begin, end)

    async def _paired_pg_query(self, limit, begin, end):
        where = "1=1"
        params = []
        idx = 0
        if begin:
            idx += 1
            where += f" AND timestamp >= ${idx}"
            params.append(begin)
        if end:
            idx += 1
            where += f" AND timestamp <= ${idx}"
            params.append(end)
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

    async def _get_paired_pg(self, limit, begin, end):
        rows = await self._paired_pg_query(limit, begin, end)
        return await self._format_paired(rows, begin, end, limit)

    async def _get_paired_sqlite(self, limit, begin, end):
        where = "1=1"
        params = []
        if begin:
            where += " AND timestamp >= ?"
            params.append(begin)
        if end:
            where += " AND timestamp <= ?"
            params.append(end)
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
        cutoff = (datetime.datetime.utcnow() - datetime.timedelta(days=days)).isoformat()
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
        now = datetime.utcnow().isoformat()
        existing = await self._fetchone("SELECT id FROM positions WHERE bot_id = $1" if self._pg_mode else "SELECT id FROM positions WHERE bot_id = ?", (bot_id,))
        if existing:
            if self._pg_mode:
                await self._execute(
                    "UPDATE positions SET inst_id=$1, side=$2, size=$3, entry_price=$4, "
                    "current_price=$5, updated_at=$6 WHERE bot_id=$7",
                    (inst_id, side, size, entry_price, current_price or entry_price, now, bot_id)
                )
            else:
                await self._execute(
                    "UPDATE positions SET inst_id=?, side=?, size=?, entry_price=?, "
                    "current_price=?, updated_at=? WHERE bot_id=?",
                    (inst_id, side, size, entry_price, current_price or entry_price, now, bot_id)
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
        now = datetime.utcnow().isoformat()
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

    async def delete_position(self, bot_id: str):
        if self._pg_mode:
            await self._execute("DELETE FROM positions WHERE bot_id = $1", (bot_id,))
        else:
            await self._execute("DELETE FROM positions WHERE bot_id = ?", (bot_id,))

    async def get_all_positions(self) -> list[dict]:
        return await self._fetchall(
            "SELECT p.*, b.strategy_id, b.symbol FROM positions p "
            "JOIN bots b ON b.id = p.bot_id ORDER BY p.opened_at DESC"
        )

    # ── Metrics ──

    async def save_metric(self, bot_id: str, equity: float = None,
                          total_pnl: float = 0, win_rate: float = None,
                          sharpe_ratio: float = None, max_drawdown: float = None,
                          total_trades: int = 0):
        now = datetime.utcnow().isoformat()
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
