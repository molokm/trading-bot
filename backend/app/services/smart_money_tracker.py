"""Smart Money Tracker — discovery, verification, tracking & copy of OKX lead traders.

Replaces the Order Book Scalp (OBI) strategy.
Uses OKX Copy Trading public API for leaderboard + stats,
and private API for copy-trading execution.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("smart_money")

BOT_ID = "smart_money"
STRATEGY_NAME = "Smart Money Tracker"
STRATEGY_VERSION = "v1.0"

DATA_DIR = os.environ.get("DATA_DIR", "/tmp")
if not os.path.isdir(DATA_DIR):
    DATA_DIR = "/tmp"


# ──────────────────────── Config ────────────────────────

@dataclass
class TrackerConfig:
    """Configuration for the Smart Money Tracker."""
    # Discovery
    sort_type: str = "pnl_ratio"          # roi | copyRatio | pnl
    inst_type: str = "SWAP"
    min_lead_days: int = 14         # minimum days as lead trader
    min_assets: float = 0.0         # min AUM filter
    max_assets: float = 0.0         # max AUM filter (0 = no limit)
    page_size: int = 20

    # Verification thresholds
    min_roi_pct: float = 5.0        # minimum ROI % to pass verification
    min_win_rate: float = 0.45      # minimum win rate (0-1)
    max_max_drawdown: float = 0.30  # maximum allowed drawdown (0-1)
    min_profitable_weeks: int = 3   # out of last 4 weeks
    min_copy_traders: int = 5       # social proof

    # Copy settings
    capital: float = 500.0          # USDT per copy trade
    max_leverage: int = 3
    copy_mode: str = "fixed_amt"    # fixed_amt | fixed_ratio | fixed_qty
    tp_ratio: float = 0.10          # 10% TP
    sl_ratio: float = 0.05          # 5% SL
    max_daily_loss_pct: float = 0.05
    max_open_copies: int = 5

    # Monitoring
    poll_interval_sec: int = 60     # how often to check tracked traders
    snapshot_interval_sec: int = 3600  # how often to snapshot performance

    # Execution
    execute: bool = False
    notify_telegram: bool = False


# ──────────────────────── Data Classes ────────────────────────

@dataclass
class TraderProfile:
    """A lead trader from OKX leaderboard."""
    unique_code: str
    alias: str = ""
    inst_type: str = "SWAP"
    roi_pct: float = 0.0
    pnl_usd: float = 0.0
    copy_ratio: float = 0.0
    copy_traders: int = 0
    lead_days: int = 0
    aum: float = 0.0
    win_rate: float = 0.0
    max_drawdown: float = 0.0
    # Extended stats
    total_trades: int = 0
    period_label: str = ""          # e.g. "30д", "месяц", "739д ведения"
    period_days: int = 0
    avg_hold_hours: float = 0.0
    preferred_coins: List[str] = field(default_factory=list)
    weekly_pnl: List[Dict] = field(default_factory=list)
    # Verification
    verified: bool = False
    verify_score: float = 0.0
    verify_failures: List[str] = field(default_factory=list)
    last_verified: float = 0.0
    # Tracking state
    tracked: bool = False
    tracking_since: float = 0.0
    last_snapshot: float = 0.0
    # Current positions
    current_positions: List[Dict] = field(default_factory=list)
    last_positions_fetch: float = 0.0


@dataclass
class CopyTrade:
    """Record of a copy trade executed."""
    id: str = ""
    trader_code: str = ""
    inst_id: str = ""
    side: str = ""
    size: str = ""
    entry_price: str = ""
    entry_time: float = 0.0
    close_price: str = ""
    close_time: float = 0.0
    pnl: float = 0.0
    reason: str = ""


# ──────────────────────── OKX API Client ────────────────────────

class OKXCopyAPI:
    """Thin wrapper around OKX Copy Trading API endpoints."""

    BASE = "https://www.okx.com"

    def __init__(self, api_key: str = "", secret_key: str = "",
                 passphrase: str = "", demo: bool = False):
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase
        self.demo = demo

    def _sign(self, ts: str, method: str, path: str, body: str = "") -> str:
        import hmac, hashlib, base64
        msg = f"{ts}{method}{path}{body}"
        mac = hmac.new(self.secret_key.encode(), msg.encode(), hashlib.sha256)
        return base64.b64encode(mac.digest()).decode()

    def _auth_headers(self, method: str, path: str, body: str = "") -> dict:
        from datetime import datetime as _dt
        now = _dt.utcnow()
        ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
        h = {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": self._sign(ts, method, path, body),
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
        }
        if self.demo:
            h["x-simulated-trading"] = "1"
        return h

    async def _get(self, path: str, params: dict = None) -> dict:
        url = f"{self.BASE}{path}"
        if params:
            qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
            if qs:
                url += f"?{qs}"
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(url)
            return r.json()

    async def _get_auth(self, path: str, params: dict = None) -> dict:
        url = f"{self.BASE}{path}"
        qs = ""
        if params:
            qs = "&".join(f"{k}={v}" for k, v in params.items() if v is not None)
            if qs:
                url += f"?{qs}"
        sign_path = f"{path}?{qs}" if qs else path
        headers = self._auth_headers("GET", sign_path)
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(url, headers=headers)
            return r.json()

    async def _post_auth(self, path: str, body: dict = None) -> dict:
        body_str = json.dumps(body) if body else ""
        headers = self._auth_headers("POST", path, body_str)
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(f"{self.BASE}{path}", headers=headers, content=body_str)
            return r.json()

    # ── Public endpoints (no auth) ──

    async def get_lead_traders(self, sort_type="pnl_ratio", inst_type="SWAP",
                                min_lead_days=0, min_assets=0.0, max_assets=0.0,
                                page="1", limit="20") -> dict:
        # OKX: overview | pnl | aum | win_ratio | pnl_ratio | current_copy_trader_pnl
        st_map = {
            "roi": "pnl_ratio",
            "pnl_ratio": "pnl_ratio",
            "pnl": "pnl",
            "aum": "aum",
            "win_ratio": "win_ratio",
            "winRatio": "win_ratio",
            "overview": "overview",
            "copyRatio": "overview",
            "followers": "overview",
        }
        st = st_map.get(str(sort_type or "pnl_ratio"), "pnl_ratio")
        # minLeadDays: 1=7d, 2=30d, 3=90d, 4=180d (not raw day count)
        mld = min_lead_days
        if isinstance(mld, (int, float)) and mld >= 7:
            mld = "2" if mld < 90 else ("3" if mld < 180 else "4")
        elif mld in (0, "0", None, ""):
            mld = None
        params = {
            "instType": inst_type or "SWAP",
            "sortType": st,
            "page": str(page or "1"),
            "limit": str(min(int(limit or 20), 20)),
        }
        if mld:
            params["minLeadDays"] = str(mld)
        if min_assets:
            params["minAssets"] = str(min_assets)
        if max_assets:
            params["maxAssets"] = str(max_assets)
        return await self._get("/api/v5/copytrading/public-lead-traders", params)

    async def get_trader_stats(self, unique_code: str, inst_type="SWAP",
                                last_days: str = "30") -> dict:
        return await self._get("/api/v5/copytrading/public-stats", {
            "instType": inst_type,
            "uniqueCode": unique_code,
            "lastDays": last_days,
        })

    async def get_trader_pnl(self, unique_code: str, inst_type="SWAP",
                              last_days: str = "30") -> dict:
        return await self._get("/api/v5/copytrading/public-pnl", {
            "instType": inst_type,
            "uniqueCode": unique_code,
            "lastDays": last_days,
        })

    async def get_trader_weekly_pnl(self, unique_code: str,
                                     inst_type="SWAP") -> dict:
        return await self._get("/api/v5/copytrading/public-weekly-pnl", {
            "instType": inst_type,
            "uniqueCode": unique_code,
        })

    async def get_trader_positions(self, unique_code: str,
                                    inst_type="SWAP") -> dict:
        return await self._get("/api/v5/copytrading/public-current-subpositions", {
            "instType": inst_type,
            "uniqueCode": unique_code,
        })

    async def get_trader_position_history(self, unique_code: str,
                                           inst_type="SWAP",
                                           limit: str = "20") -> dict:
        return await self._get("/api/v5/copytrading/public-subpositions-history", {
            "instType": inst_type,
            "uniqueCode": unique_code,
            "limit": limit,
        })

    async def get_trader_preferences(self, unique_code: str,
                                      inst_type="SWAP") -> dict:
        return await self._get("/api/v5/copytrading/public-preference-currency", {
            "instType": inst_type,
            "uniqueCode": unique_code,
        })

    async def get_trader_copy_count(self, unique_code: str,
                                     inst_type="SWAP") -> dict:
        return await self._get("/api/v5/copytrading/public-copy-traders", {
            "instType": inst_type,
            "uniqueCode": unique_code,
        })

    # ── Private endpoints (auth required) ──

    async def start_copy(self, inst_type, unique_code, copy_mode="fixed_amt",
                          copy_total_amt="", copy_amt="", copy_ratio="",
                          tp_ratio="", sl_ratio="", copy_mgn_mode="cross",
                          inst_id="") -> dict:
        body = {
            "instType": inst_type,
            "uniqueCode": unique_code,
            "copyMode": copy_mode,
            "copyMgnMode": copy_mgn_mode,
        }
        if copy_total_amt:
            body["copyTotalAmt"] = copy_total_amt
        if copy_amt:
            body["copyAmt"] = copy_amt
        if copy_ratio:
            body["copyRatio"] = copy_ratio
        if tp_ratio:
            body["tpRatio"] = tp_ratio
        if sl_ratio:
            body["slRatio"] = sl_ratio
        if inst_id:
            body["instId"] = inst_id
            body["copyInstIdType"] = "1"
        return await self._post_auth("/api/v5/copytrading/first-copy-settings", body)

    async def amend_copy(self, inst_type, unique_code, **kwargs) -> dict:
        body = {"instType": inst_type, "uniqueCode": unique_code}
        body.update(kwargs)
        return await self._post_auth("/api/v5/copytrading/amend-copy-settings", body)

    async def stop_copy(self, inst_type, unique_code,
                         sub_pos_close_type="0") -> dict:
        return await self._post_auth("/api/v5/copytrading/stop-copy-trading", {
            "instType": inst_type,
            "uniqueCode": unique_code,
            "subPosCloseType": sub_pos_close_type,
        })

    async def get_my_lead_traders(self, inst_type="SWAP") -> dict:
        return await self._get_auth("/api/v5/copytrading/current-lead-traders", {
            "instType": inst_type,
        })

    async def get_copy_settings(self, inst_type, unique_code) -> dict:
        return await self._get_auth("/api/v5/copytrading/copy-settings", {
            "instType": inst_type,
            "uniqueCode": unique_code,
        })

    async def get_copy_config(self) -> dict:
        return await self._get_auth("/api/v5/copytrading/config")


# ──────────────────────── Verification Engine ────────────────────────

class TraderVerifier:
    """Scores and verifies traders before allowing copy."""

    def __init__(self, config: TrackerConfig):
        self.cfg = config

    def verify(self, trader: TraderProfile, stats: dict = None,
               weekly_pnl: list = None, copy_count: int = 0) -> TraderProfile:
        """Run all verification checks on a trader. Sets verified=True/False."""
        failures = []
        score = 0.0

        # 1. ROI check
        if trader.roi_pct < self.cfg.min_roi_pct:
            failures.append(f"ROI {trader.roi_pct:.1f}% < {self.cfg.min_roi_pct}%")
        else:
            score += min(trader.roi_pct / self.cfg.min_roi_pct, 2.0) * 20

        # 2. Win rate check
        if trader.win_rate > 0 and trader.win_rate < self.cfg.min_win_rate:
            failures.append(f"WR {trader.win_rate:.1%} < {self.cfg.min_win_rate:.0%}")
        elif trader.win_rate > 0:
            score += min(trader.win_rate / 0.6, 1.5) * 20

        # 3. Max drawdown check
        if trader.max_drawdown > 0 and trader.max_drawdown > self.cfg.max_max_drawdown:
            failures.append(f"MaxDD {trader.max_drawdown:.1%} > {self.cfg.max_max_drawdown:.0%}")
        elif trader.max_drawdown > 0:
            score += max(0, (1 - trader.max_drawdown / self.cfg.max_max_drawdown)) * 15

        # 4. Consistency (profitable weeks)
        if weekly_pnl:
            profitable_weeks = sum(
                1 for w in weekly_pnl
                if float(w.get("pnl", 0)) > 0
            )
            if profitable_weeks < self.cfg.min_profitable_weeks:
                failures.append(
                    f"Profitable weeks {profitable_weeks}/"
                    f"{len(weekly_pnl)} < {self.cfg.min_profitable_weeks}"
                )
            else:
                score += (profitable_weeks / max(len(weekly_pnl), 1)) * 25
        else:
            # No weekly data — partial score
            score += 10

        # 5. Social proof (copy traders)
        if copy_count > 0 and copy_count < self.cfg.min_copy_traders:
            failures.append(f"Copy traders {copy_count} < {self.cfg.min_copy_traders}")
        elif copy_count > 0:
            score += min(copy_count / 20, 1.5) * 10

        # 6. Lead days bonus
        if trader.lead_days >= 30:
            score += 10
        elif trader.lead_days >= 14:
            score += 5

        trader.verify_failures = failures
        trader.verified = len(failures) == 0
        trader.verify_score = round(min(score, 100), 1)
        trader.last_verified = time.time()
        return trader


# ──────────────────────── Main Tracker ────────────────────────

class SmartMoneyTracker:
    """Main orchestrator: discovery → verification → tracking → copy."""

    def __init__(self, config: TrackerConfig = None, client_manager=None,
                 db=None, notifier=None, okx_api: OKXCopyAPI = None):
        self.config = config or TrackerConfig()
        self.client_manager = client_manager
        self.db = db
        self.notifier = notifier
        self.okx_api = okx_api

        self.verifier = TraderVerifier(self.config)

        # State
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._traders: Dict[str, TraderProfile] = {}
        self._copy_trades: List[CopyTrade] = []
        self._discover_cache: List[Dict] = []
        self._discover_ts: float = 0.0
        self._last_error: str = ""
        self._lifetime_pnl: float = 0.0
        self._lifetime_copies: int = 0
        self._session_copies: int = 0
        self._daily_loss: float = 0.0
        self._daily_reset_ts: float = 0.0

        # Load persisted state
        self._load_state()

    # ────────── Lifecycle ──────────

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._thread_main, daemon=True,
                                         name="sm-tracker")
        self._thread.start()
        logger.info("Smart Money Tracker started")

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
        self._persist()
        logger.info("Smart Money Tracker stopped")

    def _thread_main(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._run())

    async def _run(self):
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                self._last_error = str(e)
                logger.error(f"Tick error: {e}", exc_info=True)
            await asyncio.sleep(self.config.poll_interval_sec)
        self._persist()

    async def _tick(self):
        """Main monitoring loop tick."""
        # Reset daily loss counter
        now = time.time()
        if now - self._daily_reset_ts > 86400:
            self._daily_loss = 0.0
            self._daily_reset_ts = now

        # Update tracked traders
        for code, trader in list(self._traders.items()):
            if not trader.tracked:
                continue
            try:
                await self._update_trader(trader)
            except Exception as e:
                logger.error(f"Update trader {code} error: {e}")

        # Snapshot periodically
        if now - self._last_snapshot_time() > self.config.snapshot_interval_sec:
            self._db_snapshot()

    def _last_snapshot_time(self) -> float:
        return max((t.last_snapshot for t in self._traders.values()), default=0)

    # ────────── Discovery ──────────

    async def discover(self, page: str = "1", limit: str = "20",
                       sort_type: str = None) -> List[Dict]:
        """Fetch OKX leaderboard and return raw trader data."""
        if not self.okx_api:
            return []

        try:
            resp = await self.okx_api.get_lead_traders(
                sort_type=sort_type or self.config.sort_type or "roi",
                inst_type=self.config.inst_type,
                min_lead_days=self.config.min_lead_days,
                page=page,
                limit=limit,
            )
            if resp.get("code") != "0":
                self._last_error = resp.get("msg", "discover failed")
                return []

            traders = resp.get("data", [])
            # OKX returns [{ dataVer, ranks: [ {...}, ... ] }]
            if traders and isinstance(traders[0], dict) and "uniqueCode" not in traders[0]:
                ranks = traders[0].get("ranks")
                if isinstance(ranks, list) and ranks:
                    traders = ranks
                else:
                    inner = traders[0].get("data") or traders[0].get("list") or []
                    if isinstance(inner, list) and inner:
                        traders = inner
            self._discover_cache = traders
            self._discover_ts = time.time()
            return traders
        except Exception as e:
            self._last_error = str(e)
            return []

    async def discover_and_verify(self, page="1", limit="20",
                                  sort_type: str = None,
                                  min_roi_pct: float = None,
                                  only_verified: bool = False,
                                  sources: str = None) -> List[Dict]:
        """Discover traders from OKX + open sources, rank by ROI."""
        src_list = [s.strip().lower() for s in (sources or "okx,hyperliquid,social").split(",") if s.strip()]
        results = []

        # ── OKX ──
        if "okx" in src_list and self.okx_api:
            raw = await self.discover(page, limit, sort_type=sort_type)
        else:
            raw = []

        for t_data in raw:
            code = t_data.get("uniqueCode", "")
            if not code:
                continue

            trader = self._parse_trader(t_data)
            # Rank already has winRatio / copyTraderNum — use as baseline
            copy_count = int(trader.copy_traders or 0)
            stats_data = []
            weekly_data = []

            # Period: OKX ranking ROI is cumulative over lead tenure
            if trader.lead_days and trader.lead_days > 0:
                trader.period_days = int(trader.lead_days)
                trader.period_label = f"{trader.lead_days}д ведения"
            else:
                trader.period_label = "OKX рейтинг"

            # Optional enrichment (non-fatal; rate-limit friendly)
            # lastDays: 1=7д, 2=30д, 3=90д, 4=180д
            try:
                stats_resp = await self.okx_api.get_trader_stats(code, last_days="2")
                if stats_resp.get("code") == "0":
                    stats_data = stats_resp.get("data") or []
                    if stats_data and isinstance(stats_data[0], dict):
                        s = stats_data[0]
                        if s.get("winRate") not in (None, ""):
                            wr = float(s.get("winRate") or 0)
                            trader.win_rate = wr / 100.0 if wr > 1 else wr
                        if s.get("maxDrawdown") not in (None, ""):
                            dd = float(s.get("maxDrawdown") or 0)
                            trader.max_drawdown = dd / 100.0 if dd > 1 else dd
                        if s.get("totalTrades") not in (None, ""):
                            trader.total_trades = int(float(s.get("totalTrades") or 0))
                        # profitDays + lossDays ≈ activity days in window (proxy for trade activity)
                        pd = int(float(s.get("profitDays") or 0))
                        ld = int(float(s.get("lossDays") or 0))
                        if trader.total_trades <= 0 and (pd or ld):
                            trader.total_trades = pd + ld
                            # annotate that count is trading-days in 30d window
                            if not trader.period_label or "ведения" in trader.period_label:
                                pass  # keep tenure for ROI; trades refer to 30d activity
                        # Store 30d window hint for UI
                        row_period_stats = f"WR 30д; активность {pd + ld}д"
            except Exception:
                pass

            try:
                weekly_resp = await self.okx_api.get_trader_weekly_pnl(code)
                if weekly_resp.get("code") == "0":
                    weekly_data = weekly_resp.get("data") or []
                    if weekly_data:
                        trader.weekly_pnl = weekly_data
            except Exception:
                pass

            # Closed positions count (up to 50 recent) as trade-count proxy
            if trader.total_trades <= 0:
                try:
                    hist = await self.okx_api.get_trader_position_history(code, limit="50")
                    if hist.get("code") == "0":
                        hdata = hist.get("data") or []
                        if hdata:
                            trader.total_trades = len(hdata)
                            if len(hdata) >= 50:
                                # indicate 50+ 
                                trader.total_trades = 50
                                row_trades_cap = True
                except Exception:
                    pass

            self.verifier.verify(trader, stats_data, weekly_data, copy_count)
            if not trader.copy_traders:
                trader.copy_traders = copy_count

            row = asdict(trader)
            row["source"] = "okx"
            row["copyable"] = True
            row["profile_url"] = (
                t_data.get("portLink")
                or f"https://www.okx.com/copy-trading/account/{code}"
            )
            row["note"] = "OKX Copy Trading"
            row["period_label"] = trader.period_label or "OKX"
            row["period_days"] = int(trader.period_days or trader.lead_days or 0)
            row["total_trades"] = int(trader.total_trades or 0)
            row["trades_label"] = (
                f"{row['total_trades']}+" if row["total_trades"] >= 50 else str(row["total_trades"] or "н/д")
            )
            row["pnl_period_note"] = (
                f"PnL/ROI за {row['period_label']}"
                if row.get("period_label") else "PnL/ROI за период рейтинга"
            )
            results.append(row)

        # ── Hyperliquid + social (open sources) ──
        try:
            from .smart_money_sources import fetch_hyperliquid_cached, fetch_social
            ext = []
            if "hyperliquid" in src_list or "hl" in src_list:
                ext.extend(await fetch_hyperliquid_cached(limit=int(limit) if str(limit).isdigit() else 25))
            if "social" in src_list or "twitter" in src_list or "x" in src_list:
                ext.extend(await fetch_social())
            # dedupe by unique_code
            seen = {r.get("unique_code") for r in results}
            for e in ext:
                c = e.get("unique_code")
                if c and c not in seen:
                    results.append(e)
                    seen.add(c)
        except Exception as ex:
            print(f"[SmartMoney] external sources: {ex}", flush=True)

        min_roi = float(min_roi_pct if min_roi_pct is not None else getattr(self.config, "min_roi_pct", 0) or 0)
        if min_roi > 0:
            results = [r for r in results if float(r.get("roi_pct") or 0) >= min_roi]
        if only_verified:
            results = [r for r in results if r.get("verified")]
        if src_list:
            results = [r for r in results if (r.get("source") or "okx").lower() in src_list
                       or ((r.get("source") or "") == "hyperliquid" and "hl" in src_list)]
        results.sort(
            key=lambda r: (
                float(r.get("roi_pct") or 0),
                float(r.get("verify_score") or 0),
                int(r.get("copy_traders") or 0),
            ),
            reverse=True,
        )
        for i, r in enumerate(results, 1):
            r["rank"] = i
        return results

    async def get_trader_detail(self, unique_code: str) -> Dict:
        """Get full details for a single trader."""
        if not self.okx_api:
            return {}

        # Check if we already track this trader
        if unique_code in self._traders:
            trader = self._traders[unique_code]
        else:
            trader = TraderProfile(unique_code=unique_code)

        # Fetch all data in parallel
        stats_r, pnl_r, weekly_r, pos_r, hist_r, pref_r, copy_r = await asyncio.gather(
            self.okx_api.get_trader_stats(unique_code),
            self.okx_api.get_trader_pnl(unique_code),
            self.okx_api.get_trader_weekly_pnl(unique_code),
            self.okx_api.get_trader_positions(unique_code),
            self.okx_api.get_trader_position_history(unique_code, limit="50"),
            self.okx_api.get_trader_preferences(unique_code),
            self.okx_api.get_trader_copy_count(unique_code),
            return_exceptions=True,
        )

        # Parse stats
        if isinstance(stats_r, dict) and stats_r.get("code") == "0" and stats_r.get("data"):
            s = stats_r["data"][0]
            trader.win_rate = float(s.get("winRate", 0))
            trader.total_trades = int(s.get("totalTrades", 0))
            trader.avg_hold_hours = float(s.get("avgHoldTime", 0))
            trader.max_drawdown = float(s.get("maxDrawdown", 0))
            trader.aum = float(s.get("aum", 0))
            trader.lead_days = int(s.get("leadDays", 0))

        # Parse PnL
        if isinstance(pnl_r, dict) and pnl_r.get("code") == "0" and pnl_r.get("data"):
            p = pnl_r["data"][0]
            trader.roi_pct = float(p.get("roi", 0)) * 100
            trader.pnl_usd = float(p.get("pnl", 0))

        # Parse weekly PnL
        if isinstance(weekly_r, dict) and weekly_r.get("code") == "0":
            trader.weekly_pnl = weekly_r.get("data", [])

        # Parse positions
        if isinstance(pos_r, dict) and pos_r.get("code") == "0":
            trader.current_positions = pos_r.get("data", [])
            trader.last_positions_fetch = time.time()

        # Parse position history (closed trades)
        trade_history = []
        if isinstance(hist_r, dict) and hist_r.get("code") == "0":
            for h in hist_r.get("data", []):
                trade_history.append({
                    "instId": h.get("instId", ""),
                    "side": h.get("side", ""),
                    "sz": h.get("sz", ""),
                    "avgPx": h.get("avgPx", ""),
                    "pnl": float(h.get("pnl", 0)),
                    "pnlRatio": float(h.get("pnlRatio", 0)),
                    "openTime": h.get("cTime", ""),
                    "closeTime": h.get("uTime", ""),
                    "杠杆": h.get("lever", ""),
                })

        # Parse preferences
        if isinstance(pref_r, dict) and pref_r.get("code") == "0" and pref_r.get("data"):
            trader.preferred_coins = [
                p.get("ccy", "") for p in pref_r["data"]
            ]

        # Parse copy count
        if isinstance(copy_r, dict) and copy_r.get("code") == "0" and copy_r.get("data"):
            trader.copy_traders = int(copy_r["data"][0].get("copyTraders", 0))

        # Verify
        self.verifier.verify(trader, stats_r.get("data") if isinstance(stats_r, dict) else None,
                              trader.weekly_pnl, trader.copy_traders)

        result = asdict(trader)
        result["trade_history"] = trade_history
        return result

    # ────────── Tracking ──────────

    async def track_trader(self, unique_code: str) -> Dict:
        """Add a trader to tracked list."""
        if unique_code in self._traders and self._traders[unique_code].tracked:
            return {"ok": True, "msg": "already tracked"}

        detail = await self.get_trader_detail(unique_code)
        if not detail:
            return {"ok": False, "msg": "trader not found"}

        trader = TraderProfile(
            unique_code=unique_code,
            alias=detail.get("alias", ""),
            roi_pct=detail.get("roi_pct", 0),
            pnl_usd=detail.get("pnl_usd", 0),
            win_rate=detail.get("win_rate", 0),
            max_drawdown=detail.get("max_drawdown", 0),
            aum=detail.get("aum", 0),
            lead_days=detail.get("lead_days", 0),
            copy_traders=detail.get("copy_traders", 0),
            verified=detail.get("verified", False),
            verify_score=detail.get("verify_score", 0),
            tracked=True,
            tracking_since=time.time(),
            current_positions=detail.get("current_positions", []),
        )
        self._traders[unique_code] = trader
        self._persist()

        return {"ok": True, "msg": f"now tracking {trader.alias or unique_code}"}

    def untrack_trader(self, unique_code: str) -> Dict:
        """Remove a trader from tracked list."""
        if unique_code not in self._traders:
            return {"ok": False, "msg": "not tracked"}
        self._traders[unique_code].tracked = False
        self._persist()
        return {"ok": True, "msg": "untracked"}

    def get_tracked(self) -> List[Dict]:
        """Return all tracked traders."""
        return [asdict(t) for t in self._traders.values() if t.tracked]

    # ────────── Config Update ──────────

    def update_config(self, **kwargs) -> Dict:
        """Update tracker config at runtime."""
        for k, v in kwargs.items():
            if hasattr(self.config, k):
                old = getattr(self.config, k)
                if isinstance(old, int):
                    setattr(self.config, k, int(v))
                elif isinstance(old, float):
                    setattr(self.config, k, float(v))
                elif isinstance(old, bool):
                    setattr(self.config, k, bool(v))
                else:
                    setattr(self.config, k, v)
        self._persist()
        return {"ok": True, "config": asdict(self.config)}

    # ────────── Copy Trading ──────────

    async def start_copying(self, unique_code: str,
                             copy_amt: str = None) -> Dict:
        """Start copying a trader on OKX."""
        if not self.okx_api:
            return {"ok": False, "msg": "no OKX copy API client"}
        if not (getattr(self.okx_api, "api_key", None) or "").strip():
            return {"ok": False, "msg": "OKX API keys not configured"}

        # Prefer live trading client if present, but private copy API uses okx_api keys
        if self.client_manager:
            client = self.client_manager.get_client()
            if not client:
                return {"ok": False, "msg": "OKX client not connected — проверьте ключи / подключение"}

        if not self.config.execute:
            # Explicit copy action enables execution for this session
            self.config.execute = True

        # Check daily loss limit
        if self._daily_loss >= self.config.max_daily_loss_pct * self.config.capital:
            return {"ok": False, "msg": "daily loss limit reached"}

        # Check max open copies
        active = sum(1 for t in self._traders.values()
                     if t.tracked and t.current_positions)
        if active >= self.config.max_open_copies:
            return {"ok": False, "msg": f"max {self.config.max_open_copies} copies reached"}

        amt = copy_amt or str(self.config.capital)

        try:
            resp = await self.okx_api.start_copy(
                inst_type=self.config.inst_type,
                unique_code=unique_code,
                copy_mode=self.config.copy_mode,
                copy_total_amt=amt if self.config.copy_mode == "fixed_amt" else "",
                copy_ratio=str(self.config.copy_ratio) if self.config.copy_mode == "fixed_ratio" else "",
                tp_ratio=str(self.config.tp_ratio),
                sl_ratio=str(self.config.sl_ratio),
            )

            if resp.get("code") == "0":
                self._lifetime_copies += 1
                self._session_copies += 1
                self._persist()
                return {"ok": True, "msg": f"copying started with {amt} USDT"}
            else:
                msg = resp.get("data", [{}])[0].get("sMsg", resp.get("msg", "unknown"))
                return {"ok": False, "msg": msg}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    async def stop_copying(self, unique_code: str) -> Dict:
        """Stop copying a trader on OKX."""
        if not self.okx_api:
            return {"ok": False, "msg": "no API connection"}

        try:
            resp = await self.okx_api.stop_copy(
                inst_type=self.config.inst_type,
                unique_code=unique_code,
            )
            if resp.get("code") == "0":
                return {"ok": True, "msg": "copying stopped"}
            else:
                msg = resp.get("data", [{}])[0].get("sMsg", resp.get("msg", "unknown"))
                return {"ok": False, "msg": msg}
        except Exception as e:
            return {"ok": False, "msg": str(e)}

    async def get_my_copies(self) -> List[Dict]:
        """Get list of traders we're currently copying."""
        if not self.okx_api:
            return []
        try:
            resp = await self.okx_api.get_my_lead_traders()
            if resp.get("code") == "0":
                return resp.get("data", [])
            return []
        except Exception:
            return []

    # ────────── Internal ──────────

    async def _update_trader(self, trader: TraderProfile):
        """Refresh a tracked trader's positions and stats."""
        if not self.okx_api:
            return

        now = time.time()

        # Refresh positions every 60s
        if now - trader.last_positions_fetch > 60:
            try:
                resp = await self.okx_api.get_trader_positions(trader.unique_code)
                if resp.get("code") == "0":
                    trader.current_positions = resp.get("data", [])
                    trader.last_positions_fetch = now
            except Exception as e:
                logger.error(f"Fetch positions for {trader.unique_code}: {e}")

        # Refresh stats every 5 min
        if now - trader.last_snapshot > 300:
            try:
                stats_r = await self.okx_api.get_trader_stats(trader.unique_code)
                if isinstance(stats_r, dict) and stats_r.get("code") == "0" and stats_r.get("data"):
                    s = stats_r["data"][0]
                    trader.win_rate = float(s.get("winRate", 0))
                    trader.total_trades = int(s.get("totalTrades", 0))

                pnl_r = await self.okx_api.get_trader_pnl(trader.unique_code)
                if isinstance(pnl_r, dict) and pnl_r.get("code") == "0" and pnl_r.get("data"):
                    p = pnl_r["data"][0]
                    trader.roi_pct = float(p.get("roi", 0)) * 100
                    trader.pnl_usd = float(p.get("pnl", 0))

                trader.last_snapshot = now
            except Exception as e:
                logger.error(f"Refresh stats for {trader.unique_code}: {e}")

    def _parse_trader(self, data: dict) -> TraderProfile:
        """Parse raw OKX leaderboard rank into TraderProfile."""
        def _f(*keys, default=0.0):
            for k in keys:
                if data.get(k) is not None and data.get(k) != "":
                    try:
                        return float(data.get(k))
                    except (TypeError, ValueError):
                        continue
            return float(default)

        def _i(*keys, default=0):
            for k in keys:
                if data.get(k) is not None and data.get(k) != "":
                    try:
                        return int(float(data.get(k)))
                    except (TypeError, ValueError):
                        continue
            return int(default)

        # pnlRatio from OKX is a fraction (0.727 = 72.7%) or already large
        ratio = _f("pnlRatio", "roi", "pnl_ratio", default=0.0)
        if abs(ratio) <= 5:  # treat as fraction
            roi_pct = ratio * 100.0
        else:
            roi_pct = ratio

        wr = _f("winRatio", "win_rate", "winRate", default=0.0)
        # keep 0-1 scale for verifier; UI multiplies if <=1
        if wr > 1:
            wr = wr / 100.0

        dd = _f("maxDrawdown", "max_drawdown", default=0.0)
        if dd > 1:
            dd = dd / 100.0

        return TraderProfile(
            unique_code=str(data.get("uniqueCode") or data.get("unique_code") or ""),
            alias=str(data.get("nickName") or data.get("alias") or data.get("nick_name") or ""),
            inst_type=str(data.get("instType") or "SWAP"),
            roi_pct=roi_pct,
            pnl_usd=_f("pnl", "pnl_usd", default=0.0),
            copy_ratio=_f("copyRatio", default=0.0),
            copy_traders=_i("copyTraderNum", "copyTraders", "copy_traders", default=0),
            lead_days=_i("leadDays", "lead_days", default=0),
            aum=_f("aum", default=0.0),
            win_rate=wr,
            max_drawdown=dd,
        )

    # ────────── Status ──────────

    def get_status(self) -> Dict:
        tracked = [t for t in self._traders.values() if t.tracked]
        verified = [t for t in tracked if t.verified]
        copying = [t for t in tracked if t.current_positions]

        return {
            "running": self._running,
            "execute": self.config.execute,
            "strategy": STRATEGY_NAME,
            "version": STRATEGY_VERSION,
            "tracked_count": len(tracked),
            "verified_count": len(verified),
            "copying_count": len(copying),
            "lifetime_copies": self._lifetime_copies,
            "session_copies": self._session_copies,
            "lifetime_pnl": round(self._lifetime_pnl, 2),
            "daily_loss": round(self._daily_loss, 2),
            "last_error": self._last_error,
            "config": asdict(self.config),
            "tracked": [asdict(t) for t in tracked],
        }

    # ────────── Persistence ──────────

    def _state_path(self) -> str:
        return os.path.join(DATA_DIR, "smart_money_state.json")

    def _load_state(self):
        try:
            path = self._state_path()
            if os.path.exists(path):
                with open(path) as f:
                    data = json.load(f)
                self._lifetime_pnl = data.get("lifetime_pnl", 0)
                self._lifetime_copies = data.get("lifetime_copies", 0)
                self._daily_loss = data.get("daily_loss", 0)
                self._daily_reset_ts = data.get("daily_reset_ts", 0)
                # Restore tracked traders
                for td in data.get("traders", []):
                    tp = TraderProfile(**{
                        k: v for k, v in td.items()
                        if k in TraderProfile.__dataclass_fields__
                    })
                    self._traders[tp.unique_code] = tp
        except Exception as e:
            logger.error(f"Load state: {e}")

    def _persist(self):
        try:
            data = {
                "lifetime_pnl": self._lifetime_pnl,
                "lifetime_copies": self._lifetime_copies,
                "daily_loss": self._daily_loss,
                "daily_reset_ts": self._daily_reset_ts,
                "traders": [asdict(t) for t in self._traders.values()],
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            path = self._state_path()
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2, default=str)
            os.replace(tmp, path)
        except Exception as e:
            logger.error(f"Persist state: {e}")

    def _db_snapshot(self):
        """Save performance metrics to DB."""
        if not self.db:
            return
        try:
            self.db.save_metric(
                bot_id=BOT_ID,
                equity=self.config.capital + self._lifetime_pnl,
                total_pnl=self._lifetime_pnl,
                win_rate=0,
                total_trades=self._lifetime_copies,
            )
            for t in self._traders.values():
                if t.tracked:
                    t.last_snapshot = time.time()
        except Exception as e:
            logger.error(f"DB snapshot: {e}")
