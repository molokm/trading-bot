import asyncio
import json
import logging
import os
import time as _time
import uuid
import faulthandler
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from dataclasses import asdict

# Dump native thread tracebacks on fatal signals (segfault/abort) to a file
# that survives the crash, so we can see what actually killed the process.
_CRASH_LOG = os.path.join(os.environ.get("DATA_DIR", "/tmp"), "crash_traceback.log")
try:
    with open(_CRASH_LOG, "w") as _cf:
        _cf.write("")
    faulthandler.enable(file=open(_CRASH_LOG, "a"))
    faulthandler.register(11, file=open(_CRASH_LOG, "a"))   # SIGSEGV
    faulthandler.register(6, file=open(_CRASH_LOG, "a"))    # SIGABRT
except Exception:
    pass

# Also capture ANY uncaught Python exception (main thread + worker threads)
# into the same crash log — process may be restarted by Render after an
# unhandled error in a background thread (e.g. Smart Money mirror/tracker).
def _write_crash(text: str) -> None:
    try:
        with open(_CRASH_LOG, "a") as _cf:
            _cf.write("\n=== %s ===\n%s\n" % (_time.strftime("%Y-%m-%d %H:%M:%S"), text))
    except Exception:
        pass


def _excepthook(etype, value, tb):
    import traceback as _tb
    _write_crash("".join(_tb.format_exception(etype, value, tb)))


def _thread_excepthook(args):
    _write_crash("Thread %r: %s" % (args.thread and args.thread.name, "".join(
        _tb.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
        if (_tb := __import__("traceback")) else "?")
    ))


import sys
sys.excepthook = _excepthook
try:
    threading.excepthook = _thread_excepthook
except Exception:
    pass
import threading

logger = logging.getLogger("app")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response

from app.services.okx_client import OKXClientManager, OKXClient
from app.services.backtest_service import run_backtest_async
from app.database import db
from app.services.auth import (
    login, guest, validate, logout, is_admin, PASSWORD, grant_admin, grant_user,
    ensure_auth_secrets,
    get_user_id, encrypt_str, decrypt_str,
    check_rate_limit, record_attempt, guest_rate_limited, record_guest,
    get_blacklist, set_blacklist,
)
from app.services.strategy_manager import StrategyManager, PerUserClientManager
from app.services.rotation_strategy import RotationStrategy, RotationConfig, ROT_BOT_ID, STRATEGY_DESC
from app.services.impulse_strategy import ImpulseStrategy, ImpulseConfig, IMP_BOT_ID, STRATEGY_DESC as IMPULSE_DESC, STRATEGY_NAME as IMPULSE_NAME, STRATEGY_VERSION as IMPULSE_VERSION
from app.services.validation_strategy import ValidationStrategy, make_validation_config, VAL_BOT_ID
from app.services.ai_strategy import AIStrategy, AIConfig, AI_BOT_ID, STRATEGY_DESC as AI_DESC, STRATEGY_NAME as AI_NAME, STRATEGY_VERSION as AI_VERSION
from app.services.ai_agent import llm_status
from app.services.orderbook_scalp_strategy import (
    OrderBookScalpStrategy, ScalpConfig, SCALP_BOT_ID,
    STRATEGY_NAME as SCALP_NAME, STRATEGY_VERSION as SCALP_VERSION,
    STRATEGY_DESC as SCALP_DESC, compute_book_metrics,
)
try:
    from app.services.scalping_vwap_rev import (
        VWAPMeanReversion, ScalpConfig as VWAPScalpConfig, VWAP_BOT_ID,
        STRATEGY_NAME as VWAP_NAME, STRATEGY_VERSION as VWAP_VERSION,
        STRATEGY_DESC as VWAP_DESC,
    )
except Exception as _vwap_imp_err:
    print(f"[startup] VWAP module unavailable: {_vwap_imp_err}", flush=True)
    VWAP_BOT_ID = "vwap_mean_rev"
    VWAP_NAME = "VWAP Mean Reversion"
    VWAP_VERSION = "off"
    VWAP_DESC = "unavailable"
    class VWAPScalpConfig:
        def __init__(self, **kwargs): pass
    class VWAPMeanReversion:
        def __init__(self, *a, **k): self._running=False; self._positions={}; self._trade_log=[]
        def start(self): pass
        def stop(self): pass
        def get_status(self): return {"running": False, "strategy": VWAP_NAME, "version": "off"}
from app.services.smart_money_tracker import (
    SmartMoneyTracker, TrackerConfig, OKXCopyAPI,
    BOT_ID as SM_BOT_ID, STRATEGY_NAME as SM_NAME, STRATEGY_VERSION as SM_VERSION,
)
from app.services.telegram_notifier import TelegramNotifier
from app.services.strategy_cards import BACKTEST_SUMMARY as _BACKTEST_SUMMARY
from app.services.telegram_bot import TelegramBotPoller, _is_active, PRO_PRICE_STARS, PRO_PLAN_DAYS
from app.services.equity_tracker import EquityTracker, SNAPSHOT_INTERVAL
from app.services.risk_guard import get_status as risk_get_status, set_kill_switch, assert_can_open, update_daily_pnl
from app.services.analysis_logger import DEFAULT_PATH
from app.services.position_claim import sweep_exchange_orphans, orphan_close_enabled, claim_open, orphan_close_enabled, claim_open

# Legacy bot_id from the retired MomentumStrategy — kept for one-time DB cleanup
MOM_BOT_ID = "momentum_strategy"

load_dotenv()

_docs_enabled = os.getenv("ENABLE_DOCS", "false").lower() in ("1", "true")
app = FastAPI(
    title="OKX Trading Bot",
    version="3.0.0",
    docs_url="/docs" if _docs_enabled else None,
    redoc_url="/redoc" if _docs_enabled else None,
    openapi_url="/openapi.json" if _docs_enabled else None,
)

_cors_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
if not _cors_origins:
    _cors_origins = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "https://trading-bot-mu99.onrender.com",
    ]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).parent.parent / "static"
if STATIC_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(STATIC_DIR / "assets")), name="assets")

client_manager = OKXClientManager.get_instance()

_env_key = os.getenv("OKX_API_KEY", "")
_env_secret = os.getenv("OKX_SECRET_KEY", "")
_env_pass = os.getenv("OKX_PASSPHRASE", "")
_env_demo = os.getenv("OKX_DEMO", "true").lower() in ("1", "true")
# When "0"/"false": keep OKX read access (dashboard/trades) but do NOT auto-start
# the trading strategies. Use on a local viewer instance to avoid duplicate
# management of the same account that the deployed (Render) version handles.
_bots_auto_start = os.getenv("BOTS_AUTO_START", "1").strip().lower() not in ("0", "false", "no", "off")
# Per-bot auto-start flags: default OFF for Momentum/Impulse/Validation,
# default ON for AI. Override with MOM_AUTO_START / IMP_AUTO_START /
# VAL_AUTO_START / AI_AUTO_START env vars.
_mom_auto = os.getenv("MOM_AUTO_START", "0").strip().lower() not in ("0", "false", "no", "off")
_imp_auto = os.getenv("IMP_AUTO_START", "0").strip().lower() not in ("0", "false", "no", "off")
_val_auto = os.getenv("VAL_AUTO_START", "0").strip().lower() not in ("0", "false", "no", "off")
_ai_auto = os.getenv("AI_AUTO_START", "1").strip().lower() not in ("0", "false", "no", "off")

trade_log: list = []
_STARTED_AT = None  # set in startup(); used by /api/health uptime

def _json_safe_dict(d) -> dict:
    """Convert dict keys that are tuples/lists to strings (JSON-safe)."""
    if not isinstance(d, dict):
        return {}
    out = {}
    for k, v in d.items():
        if isinstance(k, (list, tuple)):
            key = "|".join(str(x) for x in k)
        else:
            key = str(k) if not isinstance(k, (str, int, float, bool)) and k is not None else k
            if key is None:
                key = "null"
        if isinstance(v, dict):
            v = _json_safe_dict(v)
        out[key] = v
    return out


rotation: Optional[RotationStrategy] = None
impulse: Optional[ImpulseStrategy] = None
validation: Optional[ValidationStrategy] = None
telegram = TelegramNotifier()
bot_poller: Optional[TelegramBotPoller] = None
equity_tracker: Optional[EquityTracker] = None

# Multi-tenant: per-user bots + their own OKX clients.
strategy_mgr = StrategyManager(db=db, notifier=telegram)
ai_bot = None
scalp_bot = None  # Order Book Scalp instance (retired)
vwap_rev_bot = None  # VWAP Mean Reversion instance
sm_tracker = None  # Smart Money Tracker instance
sm_mirror = None  # HL→OKX position mirror
_user_clients: dict[str, OKXClient] = {}
PLANS_PRICE = {"signals": PRO_PRICE_STARS, "pro": PRO_PRICE_STARS}

# ── Auth helpers ──

def get_token(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    try:
        c = request.cookies.get("auth_token") or ""
        if c:
            return c.strip()
    except Exception:
        pass
    return ""


def _set_auth_cookie(response, token: str, max_age: int = 86400):
    secure = (os.getenv("AUTH_COOKIE_SECURE") or "1").strip().lower() not in ("0", "false", "no")
    response.set_cookie(
        key="auth_token",
        value=token,
        httponly=True,
        secure=secure,
        samesite="lax",
        max_age=max_age,
        path="/",
    )
    return response


def _clear_auth_cookie(response):
    response.delete_cookie("auth_token", path="/")
    return response


async def write_audit(request: Request, action: str, detail: str = "", meta: str = ""):
    """Best-effort audit log; never breaks the request path."""
    try:
        role = validate(get_token(request)) or "anonymous"
        uid = get_user_id(get_token(request))
        actor = f"{role}:{uid}" if uid else role
        await db.add_audit(action=action, actor=actor, detail=detail, meta=meta)
    except Exception as e:
        print(f"[audit] write failed: {e}", flush=True)



def require_admin(request: Request):
    """FastAPI dependency: reject the request unless a valid admin token is present.

    Attach via ``Depends(require_admin)`` on sensitive/mutating routes as
    defense-in-depth alongside the global auth middleware.
    """
    if not is_admin(get_token(request)):
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── Server-side request hit logger (diagnostics for Telegram Mini App) ──
_SERVER_HITS = []


@app.middleware("http")
async def _server_hit_logger(request: Request, call_next):
    entry = {
        "t": _time.strftime("%H:%M:%S"),
        "p": request.url.path,
        "c": None,
        "m": request.method,
    }
    if request.url.path.startswith("/api/") and not request.url.path.startswith("/api/debug/"):
        _SERVER_HITS.append(entry)
        del _SERVER_HITS[:-400]
    try:
        response = await call_next(request)
        entry["c"] = response.status_code
        return response
    except Exception:
        entry["c"] = "ERR"
        raise


@app.get("/api/debug/server-hits", dependencies=[Depends(require_admin)])
async def debug_server_hits():
    """Return the most recent server-side API hits (for Mini App diagnostics)."""
    return {"hits": _SERVER_HITS[-80:]}


@app.on_event("startup")
async def startup():
    global _STARTED_AT
    _STARTED_AT = _time.time()
    try:
        print("[startup] 0/7 auth secrets ...", flush=True)
        ensure_auth_secrets()
        print("[startup] 1/7 DB init ...", flush=True)
        await db.init()
        await telegram.load_from_db(db)

        # Restore persistent logout blacklist (survives restart on Render).
        try:
            _bl = await db.get_setting("auth_blacklist")
            if _bl:
                import json as _json
                set_blacklist(_json.loads(_bl))
        except Exception as e:
            print(f"[startup] auth blacklist load: {e}", flush=True)

        # Strategy PnL/trades reset — ONLY when explicitly requested via env.
        # Previously this auto-wiped on every deploy when the hardcoded marker
        # changed: it DELETED closed trades from the DB and set pnl_epoch to the
        # deploy day (UTC). That silently dropped real closed trades (e.g. the
        # 30.08 ETH close +657) from both the trades list and PnL.
        force = (os.getenv("RESET_TRADING_STATS") or "").strip().lower() in ("1", "true", "yes")
        if force:
            print("[startup] RESET_TRADING_STATS=1 → wiping strategy stats ...", flush=True)
            await admin_reset_trading_stats({})
            await db.set_setting("trading_stats_reset_marker", "manual")
        else:
            # No explicit reset. If a previous automated deploy left a stale
            # pnl_epoch (marker != "manual"), clear it so OKX-confirmed closed
            # trades before that moment count again. Never touch "manual".
            try:
                prev_marker = await db.get_setting("trading_stats_reset_marker")
                if prev_marker and str(prev_marker) != "manual":
                    stale_epoch = await db.get_setting("pnl_epoch")
                    await db.set_setting("pnl_epoch", "")
                    await db.set_setting("trading_stats_reset_marker", "")
                    print(f"[startup] cleared stale auto-reset (marker={prev_marker!r} "
                          f"epoch={stale_epoch!r}) — history restored", flush=True)
            except Exception as e:
                print(f"[startup] clear stale reset: {e}", flush=True)

        print("[startup] 2/7 OKX client init ...", flush=True)
        if _env_key and _env_secret and _env_pass:
            await client_manager.init_client(_env_key, _env_secret, _env_pass, _env_demo)
        
        # Restore Smart Money tracker + mirrors from DB (survive Render /tmp wipe)
        try:
            from app.services.smart_money_mirror import get_mirror
            m = get_mirror(client_manager=client_manager, notifier=None, db=db)
            await m.hydrate_from_db()
            print(f"[startup] SM mirror targets={len(getattr(m, '_targets', {}) or {})}", flush=True)
        except Exception as e:
            print(f"[startup] SM mirror hydrate: {e}", flush=True)
        try:
            tr = _ensure_sm_tracker(execute=False, start=False)
            if tr and hasattr(tr, "hydrate_from_db"):
                await tr.hydrate_from_db()
            print("[startup] SM tracker hydrated", flush=True)
            _sm_auto = os.getenv("SM_AUTO_START", "0").strip().lower() not in ("0", "false", "no", "off")
            if _sm_auto and _bots_auto_start and tr and not getattr(tr, "_running", False):
                async def _sm_delayed_start(tracker=tr):
                    await asyncio.sleep(15)
                    try:
                        if not getattr(tracker, "_running", False):
                            tracker.start()
                            print("[startup] SM tracker auto-started (delayed)", flush=True)
                    except Exception as e:
                        print(f"[startup] SM auto-start: {e}", flush=True)
                asyncio.create_task(_sm_delayed_start())

        except Exception as e:
            print(f"[startup] SM tracker hydrate: {e}", flush=True)

        print("[startup] 3/7 Migration check ...", flush=True)
        # One-time cleanup: check if any old momentum data exists, wipe it all.
        # Checks trades table for old bot_id - most reliable signal.
        needs_cleanup = False
        try:
            if db._pg_mode:
                row = await db._fetchone(
                    "SELECT 1 FROM trades WHERE bot_id = $1 LIMIT 1", (MOM_BOT_ID,))
            else:
                row = await db._fetchone(
                    "SELECT 1 FROM trades WHERE bot_id = ? LIMIT 1", (MOM_BOT_ID,))
            if row:
                needs_cleanup = True
        except Exception:
            pass  # table might not exist yet on very first run
        if needs_cleanup:
            print("[startup]   Old momentum data found - one-time cleanup ...", flush=True)
            for table in ["trades", "signals", "positions", "performance_metrics", "bots"]:
                try:
                    await db._execute(f"DELETE FROM {table}")
                except Exception as e:
                    print(f"[startup]   clear {table}: {e}", flush=True)
            print("[startup]   Clean slate ready.", flush=True)
        print("[startup] 4/7 Rotation auto-start ...", flush=True)
        if _env_key and _env_secret and _env_pass and _bots_auto_start and _mom_auto:
            # v6.9-AI: match RotationConfig defaults (gate-aware). Do not re-inflate risk.
            rot_config = RotationConfig(
                symbols=["BTC", "ETH", "BNB", "XRP", "SOL", "DOGE", "ADA", "TRX", "AVAX", "LTC"],
                capital=10000.0,
                top_k=2,
                roc_period=14,
                ema_fast=20,
                ema_slow=50,
                atr_period=14,
                adx_min=26.0,
                min_roc=4.5,
                sma_long=200,
                min_hold_days=11,
                max_leverage=2.0,
                risk_per_trade=0.08,
                allocation_pct=0.30,
                atr_stop_mult=3.5,
                trail_atr_mult=3.0,
                breakeven_pct=0.025,
                partial_tp_pct=0.06,
                partial_tp_ratio=0.30,
                partial_tp2_pct=0.12,
                partial_tp2_ratio=0.30,
                allow_short=True,
                gate_enabled=True,
                gate_llm_veto=True,
                desk_telegram=False,
                poll_interval_sec=300,
                auto_execute=True,
            )
            r = RotationStrategy(config=rot_config, client_manager=client_manager, db=db,
                                 notifier=telegram)
            global rotation
            rotation = r
            try:
                await rotation.start()
                print("[startup]   Rotation RUNNING (auto-start after boot/wake)", flush=True)
            except Exception as e:
                print(f"[startup]   Rotation FAILED to start: {e}", flush=True)
        else:
            print("[startup]   Rotation skipped (no OKX env keys)", flush=True)
        print("[startup] 5/7 Impulse 1D auto-start ...", flush=True)
        if _env_key and _env_secret and _env_pass and _bots_auto_start and _imp_auto:
            imp_config = ImpulseConfig(
                symbols=["BTC", "ETH", "BNB", "XRP", "SOL", "DOGE", "ADA", "TRX", "AVAX", "LTC"],
                capital=10000.0,
                top_k=3,
                entry_roc=6.0,
                max_adds=0,
                risk_per_trade=0.045,
                sl_atr_mult=5.0,
                sl_atr_mult_short=5.0,
                trail_atr_mult=12.0,
                trail_atr_mult_short=12.0,
                cooldown_bars=3,
                tp1_atr=2.0,
                tp1_frac=0.25,
                tp2_atr=10.0,
                tp2_frac=0.3,
                max_hold_bars=28,
                max_leverage=3.0,
                poll_interval_sec=300,
                auto_execute=True,
                allow_short=False,
                btc_sma200_filter=True,
                peak_lock_after_tp1=True,
            )
            imp = ImpulseStrategy(config=imp_config, client_manager=client_manager, db=db,
                                  notifier=telegram)
            global impulse
            impulse = imp
            try:
                await impulse.start()
                print("[startup]   Impulse RUNNING (auto-start after boot/wake)", flush=True)
            except Exception as e:
                print(f"[startup]   Impulse FAILED to start: {e}", flush=True)
        else:
            print("[startup]   Impulse skipped (no OKX env keys)", flush=True)
        print("[startup] 6/7 MACD+Donchian Validation auto-start ...", flush=True)
        if _env_key and _env_secret and _env_pass and _bots_auto_start and _val_auto:
            val_config = make_validation_config(
                capital=300.0,
                top_k=2,
                donchian_n=30,
                tp_pct=0.08,
                tp_ratio=0.4,
                tp2_pct=0.08,
                be_pct=0.015,
                chandelier_atr=4.0,
                max_hold_days=3,
                risk_per_trade=0.14,
                allocation_pct=0.5,
                max_leverage=2.0,
                poll_interval_sec=300,
                auto_execute=True,
            )
            v = ValidationStrategy(config=val_config, client_manager=client_manager, db=db,
                                   notifier=telegram)
            global validation
            validation = v
            try:
                await validation.start()
                print("[startup]   Validation RUNNING (auto-start after boot/wake)", flush=True)
            except Exception as e:
                print(f"[startup]   Validation FAILED to start: {e}", flush=True)
        else:
            print("[startup]   Validation skipped (no OKX env keys)", flush=True)
        print("[startup] 7/7 Done ...", flush=True)
    except Exception as e:
        print(f"[startup] ERROR: {e}", flush=True)
        raise
    # Telegram paid-signals poller: start last, only when a bot token exists.
    global bot_poller, equity_tracker
    try:
        if telegram.token:
            bot_poller = TelegramBotPoller(notifier=telegram, db=db)
            bot_poller.start()
            print("[startup] Telegram poller started", flush=True)
            try:
                asyncio.get_event_loop().create_task(bot_poller.notify_signals_migration())
            except Exception as e:
                print(f"[startup] signals migration task error: {e}", flush=True)
        else:
            print("[startup] Telegram poller skipped (no bot token)", flush=True)
    except Exception as e:
        print(f"[startup] Telegram poller error: {e}", flush=True)
    # Public equity tracker: snapshot owner portfolio for the /tracker page.
    try:
        if client_manager.get_client():
            equity_tracker = EquityTracker(client_manager=client_manager, db=db)
            equity_tracker.start()
            print("[startup] Equity tracker started", flush=True)
    except Exception as e:
        print(f"[startup] Equity tracker error: {e}", flush=True)
    # Dashboard cache warmer: pre-compute the paired-trades/pnl pipeline in the
    # background so user requests are always served from the hot cache.
    global _warm_task
    try:
        # Delay heavy background work so /api/health answers immediately after bind
        async def _delayed_bg():
            await asyncio.sleep(8)
            try:
                await _warm_dashboard_caches()
            except Exception as e:
                print(f"[startup] warmer ended: {e}", flush=True)
        _warm_task = asyncio.create_task(_delayed_bg())
        print("[startup] Dashboard cache warmer scheduled (+8s)", flush=True)
        async def _delayed_orphan():
            await asyncio.sleep(120)
            await _orphan_sweep_loop()
        asyncio.create_task(_delayed_orphan())
        print("[startup] Orphan sweeper scheduled (+120s)", flush=True)
    except Exception as e:
        print(f"[startup] Dashboard cache warmer error: {e}", flush=True)

    try:
        print("[startup] AI Discretionary auto-start ...", flush=True)
        # AI runs independently: only needs OKX keys + AI_AUTO_START (default ON).
        # Not gated by BOTS_AUTO_START so we can disable the other bots while
        # keeping AI active for observation.
        _ai_auto = os.getenv("AI_AUTO_START", "1").strip().lower() not in ("0", "false", "no", "off")
        if _env_key and _env_secret and _env_pass and _ai_auto:
            global ai_bot
            _demo = os.getenv("OKX_DEMO", "true").lower() in ("1", "true", "yes", "on")
            # On demo, always execute (AI_EXECUTE=0 on demo is meaningless).
            # On live, respect AI_EXECUTE env with default off.
            if _demo:
                _exec = True
            else:
                env_ex = os.getenv("AI_EXECUTE", "").strip().lower()
                _exec = env_ex in ("1", "true", "yes", "on")
            ai_cfg = AIConfig(
                capital=float(os.getenv("AI_CAPITAL", "10000")),
                max_leverage=float(os.getenv("AI_MAX_LEVERAGE", "3")),
                max_positions=int(os.getenv("AI_MAX_POSITIONS", "1")),
                risk_per_trade=float(os.getenv("AI_RISK_PER_TRADE", "0.02")),
                poll_interval_sec=int(os.getenv("AI_POLL_SEC", "120")),
                execute=_exec,
            )
            ai_bot = AIStrategy(config=ai_cfg, client_manager=client_manager, db=db,
                               notifier=telegram)
            ai_bot.start()
            global _positions_cache
            _positions_cache = None
            print(
                f"[startup]   AI Discretionary RUNNING execute={_exec} capital={ai_cfg.capital}",
                flush=True,
            )
        else:
            print(
                "[startup]   AI skipped (need OKX keys + BOTS_AUTO_START; set AI_AUTO_START=0 to disable)",
                flush=True,
            )
    except Exception as e:
        print(f"[startup]   AI FAILED: {e}", flush=True)


@app.on_event("shutdown")
async def shutdown():
    global _warm_task
    if _warm_task:
        try:
            _warm_task.cancel()
        except Exception:
            pass
    if equity_tracker:
        try:
            equity_tracker.stop()
        except Exception:
            pass
    if bot_poller:
        try:
            bot_poller.stop()
        except Exception:
            pass
    if rotation and rotation._running:
        await rotation.stop()
    if impulse and impulse._running:
        await impulse.stop()
    if validation and validation._running:
        await validation.stop()
    if ai_bot and getattr(ai_bot, "_running", False):
        try:
            ai_bot.stop()
        except Exception:
            pass
    await db.close()
    try:
        from app.services.analysis_logger import get_logger
        get_logger().close()
    except Exception:
        pass


async def _okx_call(coro_factory):
    client = client_manager.get_client()
    if not client:
        if _env_key and _env_secret and _env_pass:
            await client_manager.init_client(_env_key, _env_secret, _env_pass, _env_demo)
            client = client_manager.get_client()
        if not client:
            return {"error": True, "message": "API not configured"}
    result = await coro_factory(client)
    if result.get("error"):
        if _env_key and _env_secret and _env_pass:
            await client_manager.init_client(_env_key, _env_secret, _env_pass, _env_demo)
            client = client_manager.get_client()
            if client:
                result = await coro_factory(client)
    return result


# ── Access control ──

PUBLIC_API_PATHS = {
    "/api/health",
    "/api/tracker",
    "/api/debug/client-error",
    "/api/debug/client-errors",
    "/api/auth/login",
    "/api/auth/guest",
    "/api/auth/status",
    "/api/auth/logout",
    "/api/auth/telegram",
    "/api/risk/status",
    "/api/ai/status",  # LLM config flags only (no secrets)
}

ADMIN_ONLY_PATHS = {
    "/api/credentials/status",
    "/api/credentials/test",
    "/api/credentials/init",
    "/api/trade/order",
    "/api/positions/close",
    "/api/positions/sweep-orphans",
    "/api/momentum/start",
    "/api/momentum/stop",
    "/api/momentum/config",
    "/api/rotation/start",
    "/api/rotation/stop",
    "/api/rotation/reset",
    "/api/rotation/config",
    "/api/impulse/start",
    "/api/impulse/stop",
    "/api/impulse/config",
    "/api/impulse/reset",
    "/api/validation/start",
    "/api/validation/stop",
    "/api/validation/reset",
    "/api/validation/config",
    "/api/validation/status",
    "/api/validation/trades",
    "/api/validation/indicators",
    "/api/db/reset-all",
    "/api/db/positions",
    "/api/telegram/status",
    "/api/telegram/config",
    "/api/telegram/test",
    "/api/telegram/simulate",
    "/api/telegram/menu",
    "/api/analysis/log",
    "/api/subs",
    "/api/subs/activate",
    "/api/subs/deactivate",
    "/api/subs/config",
    "/api/mode",
    "/api/audit",
    "/api/risk/kill",
    "/api/pnl/rebuild-strategy",
    "/api/admin/reset-trading-stats",
    "/api/ai/start",
    "/api/ai/stop",
    "/api/ai/decide",
    "/api/ai/correct-attribution",
    "/api/ai/logs",
    "/api/ai/logs/download",
}

ADMIN_ONLY_PREFIXES = (
    "/api/debug/",
    "/api/admin/",
    "/api/vwap_rev/",
)
# Smart Money reads (discover/status) allowed for any authenticated non-guest;
# mutations still use Depends(require_admin).

# Guest cannot read live trading / PnL (admin or telegram-user only)
GUEST_FORBIDDEN_PREFIXES = (
    "/api/pnl",
    "/api/trades",
    "/api/positions",
    "/api/portfolio",
    "/api/momentum",
    "/api/rotation",
    "/api/impulse",
    "/api/validation",
    "/api/ai/",
    "/api/smart-money",
    "/api/reports",
    "/api/backtest",
    "/api/credentials",
    "/api/mode",
    "/api/audit",
    "/api/db/",
    "/api/me",
)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    if not path.startswith("/api/") or path in PUBLIC_API_PATHS:
        return await call_next(request)
    role = validate(get_token(request))
    if role is None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    if path in ADMIN_ONLY_PATHS or any(path.startswith(p) for p in ADMIN_ONLY_PREFIXES):
        if role != "admin":
            return JSONResponse(status_code=403, content={"detail": "Forbidden"})
    if role == "guest":
        for p in GUEST_FORBIDDEN_PREFIXES:
            if path == p or path.startswith(p + "/") or path.startswith(p):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Guest cannot access trading data. Sign in as admin."},
                )
    return await call_next(request)


# ── Auth ──

@app.post("/api/auth/login")
async def auth_login(request: Request, data: dict):
    ip = request.client.host if request.client else "unknown"
    if check_rate_limit(ip):
        raise HTTPException(status_code=429, detail="Too many login attempts. Try again later.")
    token = login(data.get("password", ""))
    if token:
        record_attempt(ip, True)
        resp = JSONResponse({"token": token, "role": "admin", "cookie_auth": True})
        return _set_auth_cookie(resp, token)
    record_attempt(ip, False)
    raise HTTPException(status_code=401, detail="Invalid password")


@app.post("/api/auth/guest")
async def auth_guest(request: Request):
    ip = request.client.host if request.client else "unknown"
    if guest_rate_limited(ip):
        raise HTTPException(status_code=429, detail="Too many requests. Try again later.")
    record_guest(ip)
    token = guest()
    resp = JSONResponse({"token": token, "role": "guest", "cookie_auth": True})
    return _set_auth_cookie(resp, token)


@app.get("/api/auth/status")
async def auth_status(request: Request):
    token = get_token(request)
    valid = validate(token)
    admin = is_admin(token) if valid else False
    user_id = get_user_id(token) if valid else None
    plan = None
    if user_id:
        try:
            u = await db.get_user_by_telegram(user_id)
            plan = u.get("plan") if u else None
        except Exception:
            plan = None
    return {
        "authenticated": valid,
        "role": "admin" if admin else ("user" if (valid and user_id) else ("guest" if valid else "none")),
        "user_id": user_id,
        "plan": plan,
        "has_password": bool(PASSWORD),
    }


@app.post("/api/auth/telegram")
async def auth_telegram(data: dict):
    """Authenticate a Telegram Mini App user via WebApp initData.

    The initData signature is verified with the bot token. The chat matching
    TELEGRAM_CHAT_ID is granted an admin session; every other verified user is
    auto-provisioned with their own account (role=user, their own OKX creds).
    """
    init_data = (data or {}).get("initData", "")
    logger.info("mini auth: initData present=%s len=%s", bool(init_data), len(init_data))
    if not init_data:
        raise HTTPException(status_code=400, detail="Missing initData")
    if not telegram.token:
        logger.warning("mini auth: bot token not configured")
        raise HTTPException(status_code=400, detail="Telegram bot not configured")
    payload = telegram.verify_init_data(init_data)
    if payload is None:
        logger.warning("mini auth: initData signature INVALID")
        raise HTTPException(status_code=401, detail="Invalid Telegram initData")
    user = payload.get("user") or {}
    uid = str(user.get("id", ""))
    logger.info("mini auth: signature OK, user.id=%s (%s)", uid, user.get("username"))

    if telegram.chat_id and uid == telegram.chat_id:
        # Owner -> full admin session on env creds (existing behavior).
        token = grant_admin()
        logger.info("mini auth: admin token granted for owner %s", uid)
        return {
            "token": token,
            "role": "admin",
            "user": {
                "id": user.get("id"),
                "username": user.get("username"),
                "first_name": user.get("first_name"),
            },
        }

    # Any other verified Telegram user -> own account (multi-tenant), but only
    # if they hold an ACTIVE "pro" subscription (mini-app access is a Pro feature).
    # Free / signals-only users get 403.
    try:
        u = await db.find_or_create_user(
            uid, user.get("username"), user.get("first_name"))
    except Exception as e:
        logger.warning("mini auth: user provision error: %s", e)
        raise HTTPException(status_code=500, detail="Failed to create account")

    plan = (u or {}).get("plan", "free")
    if plan == "pro" and _is_active(u):
        token = grant_user(uid)
        logger.info("mini auth: PRO user account %s granted mini-app access", uid)
        return {
            "token": token,
            "role": "user",
            "user": {
                "id": user.get("id"),
                "username": user.get("username"),
                "first_name": user.get("first_name"),
            },
            "plan": plan,
        }

    # No active Pro plan -> no mini-app access. Free / signals-only users are
    # blocked with a clear, subscriber-facing message.
    logger.info("mini auth: user %s denied mini-app (plan=%s, active=%s)",
                uid, plan, _is_active(u) if u else False)
    raise HTTPException(
        status_code=403,
        detail="Доступ к мини-апу только по Pro-подписке. Сигналы — бесплатно в боте.",
    )


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    token = get_token(request)
    try:
        logout(token)
        # Persist blacklist so the revoked token stays invalid after restart.
        try:
            import json as _json
            await db.set_setting("auth_blacklist", _json.dumps(get_blacklist()))
        except Exception:
            pass
    except Exception:
        pass
    resp = JSONResponse({"ok": True})
    return _clear_auth_cookie(resp)


# ── Multi-tenant /api/me/* helpers ─────────────────────────────────────────
# (restored from 96ad252 — were accidentally removed in e0c251e, leaving the
#  /api/me/* routes calling undefined functions → NameError)

async def _me_ctx(request: Request):
    """Resolve the authenticated user context.

    Returns (role, user_id, user_row_or_None). Owner (admin) has user_id=None
    and keeps using env creds + global bots. 'user' role maps to their own
    account. Guests are rejected.
    """
    token = get_token(request)
    role = validate(token)
    if role not in ("admin", "user"):
        raise HTTPException(status_code=403, detail="Forbidden")
    user_id = get_user_id(token)
    user_row = None
    if user_id:
        try:
            user_row = await db.get_user_by_telegram(user_id)
        except Exception:
            user_row = None
    return role, user_id, user_row


async def _user_okx_client(user_id: str) -> Optional[OKXClient]:
    """Return (and cache) the user's OKXClient from their encrypted creds."""
    global _user_clients
    existing = _user_clients.get(user_id)
    if existing:
        return existing
    try:
        u = await db.get_user_by_telegram(user_id)
    except Exception:
        return None
    if not u:
        return None
    key = decrypt_str(u.get("okx_key_enc") or "")
    secret = decrypt_str(u.get("okx_secret_enc") or "")
    passphrase = decrypt_str(u.get("okx_pass_enc") or "")
    if not (key and secret and passphrase):
        return None
    client = OKXClient(key, secret, passphrase, bool(u.get("okx_demo", 1)))
    _user_clients[user_id] = client
    # Cap the cache to avoid unbounded memory growth on many users.
    # Dict keeps insertion order → evict oldest first (FIFO).
    MAX_USER_CLIENTS = 200
    if len(_user_clients) > MAX_USER_CLIENTS:
        try:
            _oldest = next(iter(_user_clients))
            _evict = _user_clients.pop(_oldest)
            _closer = _evict.close()
            if asyncio.iscoroutine(_closer):
                try:
                    asyncio.get_event_loop().create_task(_closer)
                except Exception:
                    pass
        except Exception:
            pass
    strategy_mgr.set_user_client(user_id, client)
    return client


def _clear_user_client(user_id: str):
    global _user_clients
    old = _user_clients.pop(user_id, None)
    if old:
        try:
            asyncio.get_event_loop().create_task(old.close())
        except Exception:
            pass


def _user_notifier(user_id: str):
    """Notifier that delivers a user's bot signals to THEIR chat (not the channel)."""
    if not telegram.token:
        return telegram
    return TelegramNotifier(token=telegram.token, chat_id=str(user_id), channel_id="")


def _has_active_plan(user_row: dict) -> bool:
    """Pro users need a currently-active subscription to run bots."""
    if not user_row:
        return False
    plan = user_row.get("plan")
    if plan not in ("signals", "pro"):
        return False
    return _is_active(user_row)


@app.get("/api/me")
async def me_profile(request: Request):
    """Current user profile: plan, subscription status, creds state."""
    role, user_id, user_row = await _me_ctx(request)
    if user_id is None:
        # Owner: show env creds state.
        return {
            "role": "admin", "plan": "owner",
            "creds_configured": bool(_env_key and _env_secret and _env_pass),
            "demo": _env_demo, "owner": True,
        }
    creds = bool(user_row and user_row.get("okx_key_enc"))
    return {
        "role": "user",
        "telegram_id": user_id,
        "username": (user_row or {}).get("username"),
        "first_name": (user_row or {}).get("first_name"),
        "plan": (user_row or {}).get("plan", "free"),
        "active": _has_active_plan(user_row) if user_row else False,
        "active_until": (user_row or {}).get("active_until"),
        "creds_configured": creds,
        "demo": bool((user_row or {}).get("okx_demo", 1)),
        "capital": (user_row or {}).get("capital", 10000),
    }


@app.post("/api/me/credentials")
async def me_credentials(request: Request, data: dict = None):
    """Connect the user's own OKX API keys (encrypted at rest)."""
    role, user_id, _ = await _me_ctx(request)
    if user_id is None:
        raise HTTPException(status_code=400, detail="Owner uses env credentials")
    d = data or {}
    key = str(d.get("apiKey", "")).strip()
    secret = str(d.get("secretKey", "")).strip()
    passphrase = str(d.get("passphrase", "")).strip()
    demo = bool(d.get("demo", True))
    if not (key and secret and passphrase):
        raise HTTPException(status_code=400, detail="All credentials required")
    # Test before saving.
    test = OKXClient(key, secret, passphrase, demo)
    try:
        result = await test.get_balance()
    finally:
        try:
            await test.close()
        except Exception:
            pass
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result.get("message", "Connection failed"))
    await db.update_user(
        user_id, okx_key_enc=encrypt_str(key), okx_secret_enc=encrypt_str(secret),
        okx_pass_enc=encrypt_str(passphrase), okx_demo=1 if demo else 0,
    )
    # Stop running bots so they re-init with the new client on next start.
    strategy_mgr.stop_all(user_id)
    _clear_user_client(user_id)
    return {"message": "OKX keys connected", "demo": demo}


@app.post("/api/me/credentials/test")
async def me_credentials_test(request: Request, data: dict = None):
    """Test provided (or saved) OKX credentials."""
    role, user_id, _ = await _me_ctx(request)
    d = data or {}
    key = str(d.get("apiKey", "")).strip()
    secret = str(d.get("secretKey", "")).strip()
    passphrase = str(d.get("passphrase", "")).strip()
    demo = bool(d.get("demo", True))
    if user_id and not (key or secret or passphrase):
        u = await db.get_user_by_telegram(user_id)
        key = decrypt_str((u or {}).get("okx_key_enc") or "")
        secret = decrypt_str((u or {}).get("okx_secret_enc") or "")
        passphrase = decrypt_str((u or {}).get("okx_pass_enc") or "")
        demo = bool((u or {}).get("okx_demo", 1))
    if not (key and secret and passphrase):
        return {"ok": False, "message": "Укажите API Key, Secret Key и Passphrase"}
    test = OKXClient(key, secret, passphrase, demo)
    try:
        result = await test.get_balance()
    finally:
        try:
            await test.close()
        except Exception:
            pass
    if result.get("error"):
        return {"ok": False, "message": result.get("message", "Connection failed")}
    return {"ok": True, "message": "Connected successfully", "demo": demo}


@app.get("/api/me/portfolio")
async def me_portfolio(request: Request):
    role, user_id, _ = await _me_ctx(request)
    if user_id is None:
        return await get_portfolio()
    client = await _user_okx_client(user_id)
    if not client:
        raise HTTPException(status_code=400, detail="Подключите ключи OKX в настройках")
    result = await client.get_balance()
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result.get("message", ""))
    data = result.get("data", [])
    if not data:
        return {"totalEqUsd": 0, "details": []}
    acct = data[0] if isinstance(data, list) else data
    details = []
    for d in acct.get("details", []):
        details.append({
            "ccy": d.get("ccy"),
            "eq": float(d.get("eq", 0)),
            "eqUsd": float(d.get("eqUsd", 0)),
            "availBal": float(d.get("availBal", 0)),
            "frozenBal": float(d.get("frozenBal", 0)),
        })
    return {"totalEqUsd": float(acct.get("totalEq", 0)), "details": details}


@app.get("/api/me/positions")
async def me_positions(request: Request, inst_type: str = "SWAP"):
    role, user_id, _ = await _me_ctx(request)
    if user_id is None:
        return await get_positions(inst_type)
    client = await _user_okx_client(user_id)
    if not client:
        raise HTTPException(status_code=400, detail="Подключите ключи OKX в настройках")
    result = await client.get_positions(inst_type)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result.get("message", ""))
    return {"positions": result.get("data", [])}


@app.post("/api/me/positions/close")
async def me_positions_close(request: Request, data: dict = None):
    role, user_id, _ = await _me_ctx(request)
    if user_id is None:
        return await close_position(data or {})
    client = await _user_okx_client(user_id)
    if not client:
        raise HTTPException(status_code=400, detail="Подключите ключи OKX в настройках")
    d = data or {}
    inst_id = d.get("instId")
    if not inst_id:
        raise HTTPException(status_code=400, detail="instId required")
    pos_side = d.get("posSide") or "net"
    mgn_mode = d.get("mgnMode", "cross")
    result = await client.close_position(inst_id=inst_id, mgn_mode=mgn_mode, pos_side=pos_side)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result.get("message", ""))
    return {"message": "Position closed", "data": result.get("data")}


def _user_strategy_statuses(ub):
    rot_status = ub.rotation.get_status() if ub.rotation else {
        "running": False, "strategy": "momentum_rotation", "equity": 0,
        "open_positions": {}, "total_trades": 0, "total_pnl": 0,
    }
    imp_status = ub.impulse.get_status() if ub.impulse else {
        "running": False, "strategy": IMPULSE_NAME, "version": IMPULSE_VERSION,
        "equity": 0, "open_positions": [], "closed_trades": 0,
    }
    return rot_status, imp_status


@app.get("/api/me/status")
async def me_status(request: Request):
    role, user_id, user_row = await _me_ctx(request)
    if user_id is None:
        # Owner: same History-aligned PnL as /api/momentum/status cards
        rot = await momentum_status() if rotation else {"running": False}
        imp = await impulse_status() if impulse else {"running": False}
        return {
            "role": "admin",
            "plan": "owner",
            "rotation": rot,
            "impulse": imp,
        }
    ub = strategy_mgr.get_or_create(user_id)
    rot_status, imp_status = _user_strategy_statuses(ub)
    return {
        "role": "user",
        "plan": (user_row or {}).get("plan", "free"),
        "active": _has_active_plan(user_row) if user_row else False,
        "rotation": rot_status,
        "impulse": imp_status,
    }


def _require_pro(request, user_row) -> None:
    """Users must have an active Pro plan to run bots on their own account."""
    if not _has_active_plan(user_row):
        raise HTTPException(status_code=403, detail="Тариф Pro неактивен — оплатите подписку")


@app.post("/api/me/rotation/start")
async def me_rotation_start(request: Request, data: dict = None):
    role, user_id, user_row = await _me_ctx(request)
    if user_id is None:
        return await rotation_start(data)
    _require_pro(request, user_row)
    client = await _user_okx_client(user_id)
    if not client:
        raise HTTPException(status_code=400, detail="Подключите ключи OKX в настройках")
    ub = strategy_mgr.get_or_create(user_id)
    if ub.rotation and ub.rotation._running:
        return {"message": "Rotation already running", **ub.rotation.get_status()}
    d = data or {}
    cfg = RotationConfig(
        symbols=d.get("symbols", ["BTC", "ETH", "BNB", "XRP", "SOL", "DOGE", "ADA", "TRX", "AVAX", "LTC"]),
        capital=float(d.get("capital", 10000.0)),
        top_k=int(d.get("top_k", 2)),
        roc_period=int(d.get("roc_period", 14)),
        ema_fast=int(d.get("ema_fast", 20)),
        ema_slow=int(d.get("ema_slow", 50)),
        atr_period=int(d.get("atr_period", 14)),
        breakeven_pct=float(d.get("breakeven_pct", 0.05)),
        adx_min=float(d.get("adx_min", 29.0)),
        min_hold_days=int(d.get("min_hold_days", 11)),
        max_leverage=float(d.get("max_leverage", 2.0)),
        risk_per_trade=float(d.get("risk_per_trade", 0.14)),
        atr_stop_mult=float(d.get("atr_stop_mult", 2.7)),
        trail_atr_mult=float(d.get("trail_atr_mult", 0.2)),
        partial_tp_pct=float(d.get("partial_tp_pct", 0.08)),
        poll_interval_sec=int(d.get("poll_interval_sec", 300)),
        auto_execute=d.get("auto_execute", True),
    )
    bot = RotationStrategy(config=cfg, client_manager=ub.client_holder, db=db,
                           notifier=_user_notifier(user_id))
    bot.BOT_ID = ub.rot_bot_id
    ub.rotation = bot
    await bot.start()
    return {"message": "Rotation started", **bot.get_status()}


@app.post("/api/me/rotation/stop")
async def me_rotation_stop(request: Request):
    role, user_id, _ = await _me_ctx(request)
    if user_id is None:
        return await rotation_stop()
    ub = strategy_mgr.get_or_create(user_id)
    if not ub.rotation:
        return {"message": "Rotation not running"}
    await ub.rotation.stop()
    ub.rotation = None
    return {"message": "Rotation stopped"}


@app.post("/api/me/impulse/start")
async def me_impulse_start(request: Request, data: dict = None):
    role, user_id, user_row = await _me_ctx(request)
    if user_id is None:
        return await impulse_start(data)
    _require_pro(request, user_row)
    client = await _user_okx_client(user_id)
    if not client:
        raise HTTPException(status_code=400, detail="Подключите ключи OKX в настройках")
    ub = strategy_mgr.get_or_create(user_id)
    if ub.impulse and ub.impulse._running:
        return {"message": "Impulse already running", **ub.impulse.get_status()}
    d = data or {}
    cfg = ImpulseConfig(
        symbols=d.get("symbols", ["BTC", "ETH", "BNB", "XRP", "SOL", "DOGE", "ADA", "TRX", "AVAX", "LTC"]),
        capital=float(d.get("capital", 10000.0)),
        top_k=int(d.get("top_k", 4)),
        entry_roc=float(d.get("entry_roc", 4.0)),
        max_adds=int(d.get("max_adds", 2)),
        risk_per_trade=float(d.get("risk_per_trade", 0.10)),
        sl_atr_mult=float(d.get("sl_atr_mult", 5.0)),
        sl_atr_mult_short=float(d.get("sl_atr_mult_short", 5.0)),
        trail_atr_mult=float(d.get("trail_atr_mult", 8.0)),
        trail_atr_mult_short=float(d.get("trail_atr_mult_short", 8.0)),
        cooldown_bars=int(d.get("cooldown_bars", 5)),
        tp1_atr=float(d.get("tp1_atr", 2.0)),
        tp1_frac=float(d.get("tp1_frac", 0.3)),
        tp2_atr=float(d.get("tp2_atr", 6.0)),
        tp2_frac=float(d.get("tp2_frac", 0.3)),
        max_hold_bars=int(d.get("max_hold_bars", 30)),
        max_leverage=float(d.get("max_leverage", 3.0)),
        poll_interval_sec=int(d.get("poll_interval_sec", 300)),
        auto_execute=d.get("auto_execute", True),
    )
    bot = ImpulseStrategy(config=cfg, client_manager=ub.client_holder, db=db,
                          notifier=_user_notifier(user_id))
    bot.BOT_ID = ub.imp_bot_id
    ub.impulse = bot
    await bot.start()
    return {"message": "Impulse started", **bot.get_status()}


@app.post("/api/me/impulse/stop")
async def me_impulse_stop(request: Request):
    role, user_id, _ = await _me_ctx(request)
    if user_id is None:
        return await impulse_stop()
    ub = strategy_mgr.get_or_create(user_id)
    if not ub.impulse:
        return {"message": "Impulse not running"}
    await ub.impulse.stop()
    ub.impulse = None
    return {"message": "Impulse stopped"}


@app.get("/api/me/trades")
async def me_trades(request: Request, limit: int = 50):
    """Trade history for the authenticated user's bots."""
    role, user_id, _ = await _me_ctx(request)
    if user_id is None:
        # Owner: rotation + impulse trade logs.
        trades = []
        if rotation:
            trades += list(rotation._trade_log)
        if impulse:
            trades += list(impulse._trade_log)
        trades.sort(key=lambda t: t.get("time", ""), reverse=True)
        return {"trades": trades[:limit]}
    ub = strategy_mgr.get_or_create(user_id)
    trades = []
    if ub.rotation and ub.rotation._trade_log:
        trades += list(ub.rotation._trade_log)
    if ub.impulse and ub.impulse._trade_log:
        trades += list(ub.impulse._trade_log)
    # Include persisted trades from DB for this user's bots.
    try:
        for bid in (ub.rot_bot_id, ub.imp_bot_id):
            rows = await db.get_trades(bot_id=bid, limit=200)
            for t in reversed(rows):
                trades.append({
                    "time": t.get("timestamp", ""),
                    "side": t.get("side", ""),
                    "symbol": t.get("inst_id", ""),
                    "pnl": float(t.get("pnl", 0) or 0),
                    "entry_price": float(t.get("px", 0) or 0),
                    "reason": "closed",
                })
    except Exception:
        pass
    trades.sort(key=lambda t: t.get("time", ""), reverse=True)
    return {"trades": trades[:limit]}


@app.get("/api/me/pnl")
async def me_pnl(request: Request):
    """Realized PnL from the user's own trades (their bots only)."""
    role, user_id, _ = await _me_ctx(request)
    if user_id is None:
        return await get_pnl()
    ub = strategy_mgr.get_or_create(user_id)
    total = 0.0
    count = 0
    try:
        for bid in (ub.rot_bot_id, ub.imp_bot_id):
            rows = await db.get_trades(bot_id=bid, limit=5000)
            for t in rows:
                pnl = float(t.get("pnl", 0) or 0)
                if pnl != 0:
                    total += pnl
                    count += 1
    except Exception:
        pass
    for bot in (ub.rotation, ub.impulse):
        if bot and bot._trade_log:
            for t in bot._trade_log:
                pnl = float(t.get("pnl", 0) or 0)
                if pnl != 0:
                    total += pnl
                    count += 1
    return {
        "total": round(total, 2),
        "trades": count,
        "unrealized": 0.0,
        "source": "user_bots",
    }


# ══════════════════════════════════════════════════════════════
# PUBLIC EQUITY TRACKER (no auth — trust page for selling subscriptions)
# ══════════════════════════════════════════════════════════════

# _BACKTEST_SUMMARY imported from app.services.strategy_cards


@app.get("/api/tracker")
async def public_tracker():
    """Public live performance of the owner's bots (read-only, no auth)."""
    # 1. Current equity
    current_eq = 0.0
    try:
        eq_result = await _okx_call(lambda c: c.get_balance())
        if not eq_result.get("error") and eq_result.get("data"):
            acct = eq_result["data"][0]
            current_eq = float(acct.get("totalEq", 0) or 0)
    except Exception:
        pass

    # 2. Equity curve from snapshots
    curve = []
    try:
        rows = await db.get_metrics(bot_id="portfolio", limit=500)
        curve = [{"t": r["timestamp"], "equity": r["equity"]} for r in reversed(rows)]
    except Exception:
        pass

    # 3. Realized PnL + per-bot breakdown
    pnl = {"total": 0, "1d": 0, "7d": 0, "30d": 0, "per_bot": {}, "fees": 0, "unrealized": 0}
    try:
        pnl = await get_pnl()
    except Exception:
        pass

    # 4. Trade stats (owner)
    stats = {"trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
             "profit_factor": 0.0, "avg_win": 0.0, "avg_loss": 0.0, "best": 0.0, "worst": 0.0}
    recent = []
    try:
        resp = await get_paired_trades(limit=2000)
        trades = resp.get("trades", [])
        closed = [t for t in trades
                  if (t.get("reason") or "").lower() not in ("open", "add")
                  and t.get("pnl") is not None]
        wins = [float(t["pnl"]) for t in closed if float(t["pnl"]) > 0]
        losses = [float(t["pnl"]) for t in closed if float(t["pnl"]) <= 0]
        stats["trades"] = len(closed)
        stats["wins"] = len(wins)
        stats["losses"] = len(losses)
        stats["win_rate"] = round(len(wins) / len(closed) * 100, 1) if closed else 0.0
        gross_p = sum(wins)
        gross_l = abs(sum(losses))
        stats["profit_factor"] = round(gross_p / gross_l, 2) if gross_l else (gross_p > 0)
        stats["avg_win"] = round(gross_p / len(wins), 2) if wins else 0.0
        stats["avg_loss"] = round(gross_l / len(losses), 2) if losses else 0.0
        stats["best"] = round(max(wins), 2) if wins else 0.0
        stats["worst"] = round(min(losses), 2) if losses else 0.0
        recent = closed[:10]
    except Exception:
        pass

    return {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "demo": _env_demo,
        "connected": client_manager.get_client() is not None,
        "equity": round(current_eq, 2),
        "equity_curve": curve[-200:],
        "pnl": pnl,
        "stats": stats,
        "recent_trades": recent,
        "backtest": _BACKTEST_SUMMARY,
        "snapshot_interval_sec": SNAPSHOT_INTERVAL,
    }



# ── AI Discretionary 1H ──

@app.get("/api/ai/status")
async def ai_status():
    """AI status. Prefer History-sourced PnL (same as /api/pnl Total) when available."""
    global ai_bot
    if not ai_bot:
        return {
            "running": False,
            "strategy": "AI Discretionary 1H",
            "total_pnl": 0,
            "lifetime_pnl": 0,
            "open_positions": [],
        }
    try:
        status = ai_bot.get_status()
    except Exception as e:
        import traceback
        print(f"[ai/status] get_status error: {e}", flush=True)
        traceback.print_exc()
        return {"error": f"get_status: {type(e).__name__}: {e}", "running": ai_bot._running}
    internal = status.get("lifetime_pnl", status.get("total_pnl"))
    status["total_pnl_internal"] = internal
    status["lifetime_pnl_internal"] = internal
    try:
        # Prefer same totals as /api/pnl (includes entry-owner + forced overrides)
        pnl_resp = await get_pnl()
        per = pnl_resp.get("per_bot_all") or pnl_resp.get("per_bot") or {}
        ai_pnl = float(per.get("AI Discretionary 1H") or 0)
        # Trade counts from paired list (label AI after overrides)
        resp = await get_paired_trades(limit=5000)
        ai_n = 0
        ai_wins = 0
        epoch = await get_pnl_epoch()
        for tr in resp.get("trades", []) or []:
            if (tr.get("reason") or "").lower() in ("open", "add"):
                continue
            if tr.get("pnl") is None:
                continue
            if not _trade_after_epoch(tr, epoch):
                continue
            if (tr.get("bot") or "").strip() != "AI Discretionary 1H":
                continue
            try:
                pnl = float(tr.get("pnl") or 0)
            except (TypeError, ValueError):
                continue
            ai_n += 1
            if pnl > 0:
                ai_wins += 1
        status["total_pnl"] = round(ai_pnl, 2)
        status["lifetime_pnl"] = round(ai_pnl, 2)
        status["total_trades"] = ai_n
        status["wins"] = ai_wins
        status["win_rate"] = round(100.0 * ai_wins / ai_n, 1) if ai_n else 0.0
        status["total_pnl_source"] = "okx_history"
    except Exception as e:
        print(f"[ai/status] history pnl: {e}", flush=True)
        status["total_pnl"] = status.get("lifetime_pnl") or status.get("total_pnl") or 0
    return await _apply_history_kpi(status, "AI Discretionary 1H")




@app.post("/api/admin/reassign-trade", dependencies=[Depends(require_admin)])
async def admin_reassign_trade(data: dict = None):
    """Reassign a closed trade between strategy bots (DB + override list)."""
    data = data or {}
    from_bot = str(data.get("from_bot") or "rotation_strategy").strip()
    to_bot = str(data.get("to_bot") or "ai_strategy").strip()
    # accept human labels
    label_to_id = {
        "Momentum": "rotation_strategy",
        "Impulse 1D": "impulse_strategy",
        "AI Discretionary 1H": "ai_strategy",
        "MACD+Donchian Validation": "validation_strategy",
    }
    id_to_label = {v: k for k, v in label_to_id.items()}
    from_bot = label_to_id.get(from_bot, from_bot)
    to_bot = label_to_id.get(to_bot, to_bot)
    symbol = str(data.get("symbol") or data.get("coin") or "ETH").upper().replace("-USDT-SWAP", "")
    inst = f"{symbol}-USDT-SWAP"
    side = str(data.get("side") or data.get("pos_side") or "short").lower()
    pnl_near = data.get("pnl_near", data.get("pnl"))
    try:
        pnl_near = float(pnl_near) if pnl_near is not None else 134.17
    except (TypeError, ValueError):
        pnl_near = 134.17
    time_contains = str(data.get("time") or data.get("exit_date") or "2026-09-01")
    stats = await db.reassign_closed_trade(
        from_bot, to_bot, inst,
        side=side, pnl_near=pnl_near, time_contains=time_contains,
    )
    # persist override for paired pipeline
    import json as _json
    rule = {
        "inst_id": inst,
        "pos_side": "short" if side in ("short", "sell") else "long",
        "pnl_near": pnl_near,
        "exit_date": time_contains[:10],
        "to_bot": id_to_label.get(to_bot, "AI Discretionary 1H"),
    }
    try:
        raw = await db.get_setting("pnl_bot_overrides")
        arr = _json.loads(raw) if raw else []
        if not isinstance(arr, list):
            arr = []
        arr = [r for r in arr if not (
            r.get("inst_id") == rule["inst_id"]
            and abs(float(r.get("pnl_near") or 0) - pnl_near) < 1
        )]
        arr.append(rule)
        await db.set_setting("pnl_bot_overrides", _json.dumps(arr))
    except Exception as e:
        stats["override_err"] = str(e)
    # clear caches
    global _bot_stats_cache, _paired_cache
    _bot_stats_cache = {"ts": 0.0, "data": {}}
    _paired_cache = {}
    # adjust in-memory AI / rotation if present
    try:
        if ai_bot and abs(float(stats.get("pnl") or 0)) > 0:
            ai_bot._lifetime_pnl = float(getattr(ai_bot, "_lifetime_pnl", 0) or 0) + float(stats["pnl"])
            ai_bot._lifetime_trades = int(getattr(ai_bot, "_lifetime_trades", 0) or 0) + int(stats.get("moved") or 0)
    except Exception:
        pass
    return {"ok": True, "from_bot": from_bot, "to_bot": to_bot, "rule": rule, **stats}


@app.post("/api/ai/correct-attribution", dependencies=[Depends(require_admin)])
async def ai_correct_attribution(data: dict = None):
    """Move mis-attributed trades (default SOL) off AI PnL onto Impulse."""
    global ai_bot
    data = data or {}
    symbol = str(data.get("symbol") or "SOL").upper()
    to_bot = str(data.get("to_bot") or "impulse_strategy")
    if ai_bot and hasattr(ai_bot, "correct_misattributed"):
        return await ai_bot.correct_misattributed(symbol, to_bot)
    # offline fix via DB only
    from app.services.ai_strategy import AI_BOT_ID
    inst = f"{symbol}-USDT-SWAP"
    stats = await db.reassign_trades_instrument(AI_BOT_ID, to_bot, inst)
    try:
        from app.services.position_claim import release_open
        await release_open(db, AI_BOT_ID, inst, "long")
        await release_open(db, AI_BOT_ID, inst, "short")
    except Exception:
        pass
    return {"ok": True, "offline": True, **stats, "symbol": symbol, "to_bot": to_bot}


@app.post("/api/ai/start", dependencies=[Depends(require_admin)])
async def ai_start(data: dict = None):
    # decorator must be @app.post (not bare post)

    global ai_bot
    data = data or {}
    if ai_bot and getattr(ai_bot, "_running", False):
        return {"message": "AI already running", **ai_bot.get_status()}
    # Default execute=True on OKX demo so we accumulate real fills+logs for prompt tuning
    _demo = os.getenv("OKX_DEMO", "true").lower() in ("1", "true", "yes", "on")
    if "execute" in data:
        _exec = bool(data["execute"])
    elif _demo:
        _exec = True  # demo always executes (AI_EXECUTE=0 on demo is meaningless)
    else:
        env_ex = os.getenv("AI_EXECUTE", "").strip().lower()
        _exec = env_ex in ("1", "true", "yes", "on")
    cfg = AIConfig(
        capital=float(data.get("capital") or os.getenv("AI_CAPITAL", "10000")),
        max_leverage=float(data.get("max_leverage") or 3),
        max_positions=int(data.get("max_positions") or 1),
        risk_per_trade=float(data.get("risk_per_trade") or 0.02),
        poll_interval_sec=int(data.get("poll_interval_sec") or 120),
        provider=data.get("provider") or (
            "bai" if os.getenv("BAI_API_KEY", "").strip()
            else ("groq" if os.getenv("GROQ_API_KEY", "").strip() else None)
        ),
        execute=_exec,
    )
    if data.get("symbols"):
        cfg.symbols = list(data["symbols"])
    ai_bot = AIStrategy(config=cfg, client_manager=client_manager, db=db, notifier=telegram)
    ai_bot.start()
    global _positions_cache
    _positions_cache = None
    return {"message": "AI Discretionary started", **ai_bot.get_status()}


@app.post("/api/ai/stop", dependencies=[Depends(require_admin)])
async def ai_stop():
    global ai_bot
    if ai_bot:
        ai_bot.stop()
    return {"message": "AI stopped", "running": False}


@app.post("/api/ai/execute", dependencies=[Depends(require_admin)])
async def ai_execute(data: dict = None):
    """Toggle AI auto-trading (execute=on/off) at runtime. Optionally resets
    stale lifetime PnL (reset=1) — AI ran in signal mode so old data is bogus."""
    global ai_bot
    d = data or {}
    if not ai_bot:
        return {"ok": False, "message": "AI bot not running"}
    enabled = bool(d.get("execute"))
    if d.get("reset"):
        ai_bot.reset_lifetime_pnl()
    ai_bot.set_execute(enabled)
    st = ai_bot.get_status()
    return {
        "ok": True,
        "execute": st.get("execute"),
        "total_pnl": st.get("total_pnl"),
        "lifetime_pnl": st.get("lifetime_pnl"),
        "message": f"AI auto-trade {'ON' if enabled else 'OFF'}"
                   + (" (lifetime PnL reset to 0)" if d.get("reset") else ""),
    }




@app.get("/api/ai/logs", dependencies=[Depends(require_admin)])
async def ai_logs(limit: int = 200, event: str = None):
    """Export AI decision/trade logs for prompt tuning.

    Sources: in-memory decision log (running bot) + analysis.jsonl tail (bot=ai).
    """
    global ai_bot
    limit = max(1, min(int(limit or 200), 2000))
    mem = []
    if ai_bot:
        mem = list(getattr(ai_bot, "_decision_log", []) or [])[-limit:]
        if event:
            mem = [d for d in mem if (d.get("event") or d.get("action")) == event
                   or d.get("action") == event]

    file_rows = []
    try:
        from app.services.analysis_logger import DEFAULT_PATH
        path = Path(DEFAULT_PATH)
        if path.exists():
            # read last ~N*2 lines then filter
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
            for line in lines[-(limit * 3):]:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("bot") != "ai":
                    continue
                if event and row.get("event") != event:
                    continue
                file_rows.append(row)
            file_rows = file_rows[-limit:]
    except Exception as e:
        print(f"[ai/logs] file read: {e}", flush=True)

    return {
        "memory": mem,
        "file": file_rows,
        "memory_n": len(mem),
        "file_n": len(file_rows),
        "execute": bool(ai_bot and ai_bot._execute_enabled()) if ai_bot else False,
        "running": bool(ai_bot and getattr(ai_bot, "_running", False)),
    }


@app.get("/api/ai/logs/download", dependencies=[Depends(require_admin)])
async def ai_logs_download(limit: int = 500):
    """Download AI analysis lines as JSONL attachment."""
    from app.services.analysis_logger import DEFAULT_PATH
    path = Path(DEFAULT_PATH)
    out_lines = []
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines()[-(int(limit) * 5):]:
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("bot") == "ai":
                out_lines.append(json.dumps(row, ensure_ascii=False))
    body = ('\n'.join(out_lines[-int(limit):]) + ('\n' if out_lines else ''))
    return Response(
        content=body,
        media_type="application/x-ndjson",
        headers={"Content-Disposition": "attachment; filename=ai_decisions.jsonl"},
    )

@app.post("/api/ai/decide", dependencies=[Depends(require_admin)])
async def ai_decide_once():
    global ai_bot
    if not ai_bot or not getattr(ai_bot, "_running", False):
        raise HTTPException(status_code=400, detail="AI bot not running — start first")
    client = client_manager.get_client() if client_manager else None
    if not client:
        raise HTTPException(status_code=400, detail="OKX client not ready")
    await ai_bot._fetch_indicators(client)
    try:
        ai_bot._refresh_adaptive()
    except Exception:
        pass
    snap = ai_bot._snapshot()
    from app.services.ai_agent import call_llm
    decision = await call_llm(snap, provider=ai_bot._provider())
    enriched = ai_bot._enrich_decision(decision, snap)
    ai_bot._last_decision = enriched
    return {
        "snapshot": {
            "indicators": snap.get("indicators"),
            "open_positions": snap.get("open_positions"),
            "equity": snap.get("equity"),
        },
        "decision": enriched,
    }


# ── Health ──



# ── Smart Money Tracker ──────────────────────────────────────

def _ensure_sm_tracker(*, execute: bool | None = None, start: bool = False):
    """Create global Smart Money tracker on first use (browse/copy without manual Start)."""
    global sm_tracker
    from app.services.smart_money_tracker import (
        SmartMoneyTracker, TrackerConfig, OKXCopyAPI,
    )
    if sm_tracker is None:
        okx = OKXCopyAPI(
            api_key=_env_key or os.getenv("OKX_API_KEY", ""),
            secret_key=_env_secret or os.getenv("OKX_SECRET_KEY", "") or os.getenv("OKX_SECRET", ""),
            passphrase=_env_pass or os.getenv("OKX_PASSPHRASE", ""),
            demo=_env_demo,
        )
        cfg = TrackerConfig(
            sort_type="pnl_ratio",
            execute=bool(execute) if execute is not None else False,
        )
        sm_tracker = SmartMoneyTracker(
            config=cfg,
            client_manager=client_manager,
            db=db,
            notifier=None,  # no TG for Smart Money
            okx_api=okx,
        )
    else:
        if execute is True:
            try:
                sm_tracker.config.execute = True
            except Exception:
                pass
        # refresh keys if tracker was created empty
        try:
            if sm_tracker.okx_api and not getattr(sm_tracker.okx_api, "api_key", None):
                sm_tracker.okx_api.api_key = _env_key
                sm_tracker.okx_api.secret_key = _env_secret
                sm_tracker.okx_api.passphrase = _env_pass
        except Exception:
            pass
    if start and not getattr(sm_tracker, "_running", False):
        sm_tracker.start()
    return sm_tracker


@app.get("/api/smart-money/status")
async def smart_money_status():
    global sm_tracker
    if not sm_tracker:
        st = {
            "running": False,
            "strategy": SM_NAME,
            "version": SM_VERSION,
            "execute": False,
            "tracked_count": 0,
            "verified_count": 0,
            "copying_count": 0,
            "tracked": [],
            "open_positions": [],
        }
    else:
        st = sm_tracker.get_status()
    # Merge mirror opens so dashboard never treats SM BTC as orphan
    try:
        from app.services.smart_money_mirror import get_mirror
        m = get_mirror(client_manager=client_manager, notifier=None, db=db)
        mop = m.open_positions_list() if hasattr(m, "open_positions_list") else []
        cur = list(st.get("open_positions") or [])
        seen = {(p.get("inst_id"), p.get("side")) for p in cur}
        for p in mop:
            key = (p.get("inst_id"), p.get("side"))
            if key not in seen:
                cur.append(p)
                seen.add(key)
        st["open_positions"] = cur
        st["mirror_running"] = bool(getattr(m, "_running", False))
        st["mirror_targets"] = len(getattr(m, "_targets", {}) or {})
    except Exception as e:
        print(f"[sm/status] mirror merge: {e}", flush=True)
    return st


@app.get("/api/smart-money/discover")
async def smart_money_discover(
    page: str = "1",
    limit: str = "20",
    sort: str = "pnl_ratio",
    min_roi: float = 0,
    verified_only: bool = False,
    sources: str = "okx",
):
    """Discover traders from OKX + open sources (Hyperliquid, social).

    OKX uses the light single-call path; Hyperliquid/social are fetched in
    parallel and merged. Sources: comma-separated okx,hyperliquid,social.
    """
    import asyncio
    from app.services.smart_money_light import discover_okx_light
    from app.services.smart_money_tracker import OKXCopyAPI

    src_list = [s.strip().lower() for s in (sources or "okx").split(",") if s.strip()]
    want_okx = "okx" in src_list
    want_hl = any(s in src_list for s in ("hyperliquid", "hl"))
    want_social = any(s in src_list for s in ("social", "twitter", "x"))
    if not (want_okx or want_hl or want_social):
        want_okx = True  # default

    okx = OKXCopyAPI(
        api_key=_env_key or os.getenv("OKX_API_KEY", ""),
        secret_key=_env_secret or os.getenv("OKX_SECRET_KEY", "") or os.getenv("OKX_SECRET", ""),
        passphrase=_env_pass or os.getenv("OKX_PASSPHRASE", ""),
        demo=_env_demo,
    )
    sort_type = sort if sort not in ("roi", "") else "pnl_ratio"

    traders = []
    errors = []
    try:
        lim = max(1, min(30, int(limit) if str(limit).isdigit() else 20))
    except Exception:
        lim = 20

    async def _okx():
        try:
            out = await discover_okx_light(
                okx, page=page, limit=str(lim),
                sort_type=sort_type, min_roi_pct=float(min_roi or 0),
            )
            return out.get("traders") or []
        except Exception as e:
            errors.append(f"okx: {e}")
            return []

    async def _hl():
        try:
            from app.services.smart_money_sources import fetch_hyperliquid_cached
            hl = await asyncio.wait_for(
                fetch_hyperliquid_cached(limit=lim, min_account=50_000, window="month"),
                timeout=15.0,
            )
            return hl or []
        except Exception as e:
            errors.append(f"hyperliquid: {e}")
            return []

    async def _social():
        try:
            from app.services.smart_money_sources import fetch_social
            soc = await asyncio.wait_for(fetch_social(), timeout=5.0)
            return soc or []
        except Exception as e:
            errors.append(f"social: {e}")
            return []

    tasks = []
    if want_okx:
        tasks.append(_okx())
    if want_hl:
        tasks.append(_hl())
    if want_social:
        tasks.append(_social())
    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, list):
                traders.extend(r)

    # Apply filters
    min_roi_f = float(min_roi or 0)
    if min_roi_f > 0:
        traders = [t for t in traders if float(t.get("roi_pct") or 0) >= min_roi_f]
    if verified_only:
        traders = [t for t in traders if t.get("verified")]

    # Dedupe by unique_code, OKX wins on ties
    seen = set()
    dedup = []
    for t in traders:
        c = t.get("unique_code") or ""
        if not c or c in seen:
            continue
        seen.add(c)
        dedup.append(t)

    # Sort by ROI (default) unless a different sort requested
    if str(sort).lower() in ("pnl_ratio", "roi", ""):
        dedup.sort(key=lambda t: float(t.get("roi_pct") or 0), reverse=True)
    elif str(sort).lower() in ("pnl", "profit"):
        dedup.sort(key=lambda t: float(t.get("pnl_usd") or 0), reverse=True)
    elif str(sort).lower() in ("copyratio", "followers", "overview"):
        dedup.sort(key=lambda t: int(t.get("copy_traders") or 0), reverse=True)

    for i, t in enumerate(dedup, 1):
        t["rank"] = i
    dedup = dedup[:lim]

    return {
        "traders": dedup,
        "total": len(dedup),
        "sort": sort_type,
        "min_roi": min_roi_f,
        "sources": ",".join(src_list) if src_list else "okx",
        "mode": "multi",
        "errors": errors or None,
        "cached": bool(any(t.get("cached") for t in dedup)),
    }


@app.get("/api/smart-money/trader/{unique_code}")
async def smart_money_trader_detail(unique_code: str):
    """Get full details for a single trader."""
    tracker = _ensure_sm_tracker()
    detail = await tracker.get_trader_detail(unique_code)
    return detail


@app.get("/api/smart-money/tracked")
async def smart_money_tracked():
    """List all tracked traders."""
    global sm_tracker
    if not sm_tracker:
        return {"tracked": []}
    return {"tracked": sm_tracker.get_tracked()}


@app.post("/api/smart-money/track", dependencies=[Depends(require_admin)])
async def smart_money_track(data: dict = None):
    """Start tracking a trader."""
    global sm_tracker
    data = data or {}
    code = data.get("unique_code", "")
    if not code:
        return {"ok": False, "msg": "unique_code required"}
    tracker = _ensure_sm_tracker(start=False)
    res = await tracker.track_trader(code)
    try:
        await tracker.persist_to_db()
    except Exception as e:
        print(f"[sm/track] db persist: {e}", flush=True)
    return res


@app.post("/api/smart-money/untrack", dependencies=[Depends(require_admin)])
async def smart_money_untrack(data: dict = None):
    """Stop tracking a trader."""
    global sm_tracker
    data = data or {}
    code = data.get("unique_code", "")
    if not code:
        return {"ok": False, "msg": "unique_code required"}
    tracker = _ensure_sm_tracker()
    res = tracker.untrack_trader(code)
    try:
        await tracker.persist_to_db()
    except Exception as e:
        print(f"[sm/untrack] db persist: {e}", flush=True)
    return res


def _sm_okx_api() -> "OKXCopyAPI":
    """Build a fresh OKX Copy Trading API client (no tracker thread, no
    background work). Copy trading on OKX is a one-shot REST call, so it
    does NOT need the Smart Money tracker thread that crashed the process."""
    from app.services.smart_money_tracker import OKXCopyAPI
    return OKXCopyAPI(
        api_key=_env_key or os.getenv("OKX_API_KEY", ""),
        secret_key=_env_secret or os.getenv("OKX_SECRET_KEY", "") or os.getenv("OKX_SECRET", ""),
        passphrase=_env_pass or os.getenv("OKX_PASSPHRASE", ""),
        demo=_env_demo,
    )


@app.post("/api/smart-money/copy", dependencies=[Depends(require_admin)])
async def smart_money_copy(data: dict = None):
    """Start copying a trader on OKX (direct Copy Trading API — no tracker thread)."""
    data = data or {}
    code = data.get("unique_code", "")
    if not code:
        return {"ok": False, "msg": "unique_code required"}
    code_s = str(code or "").strip()
    if code_s.startswith("hl:") or code_s.startswith("social:"):
        return {
            "ok": False,
            "msg": (
                "Этот трейдер не с OKX (Hyperliquid/соцсети). "
                "Автокопирование запускается только для лидеров OKX Copy Trading — "
                "в списке включите источник OKX и нажмите «Копировать» на карточке с бейджем OKX."
            ),
        }
    okx = _sm_okx_api()
    if not (okx.api_key or "").strip():
        return {"ok": False, "msg": "OKX API keys not configured"}
    amt = str(data.get("copy_amt") or 500)
    try:
        resp = await okx.start_copy(
            inst_type="SWAP",
            unique_code=code_s,
            copy_mode="fixed_amount",
            copy_total_amt=amt,
            tp_ratio=str(data.get("tp_ratio") or 0.10),
            sl_ratio=str(data.get("sl_ratio") or 0.05),
            copy_mgn_mode="cross",
        )
        if resp.get("code") == "0":
            try:
                from .smart_money_ledger import get_sm_ledger
                get_sm_ledger().record_open(
                    kind="copy", symbol="PORTFOLIO", side="copy",
                    size=float(amt or 0), price=0, leader=code_s,
                    source="okx", note=f"OKX copy start {amt} USDT",
                )
            except Exception:
                pass
            return {"ok": True, "msg": f"copying started with {amt} USDT"}
        # OKX returns data:[] on many errors — guard against IndexError
        _data = resp.get("data") or []
        _msg = resp.get("msg", "unknown")
        if _data and isinstance(_data, list):
            _smsg = (_data[0] or {}).get("sMsg", "")
            if _smsg:
                _msg = _smsg
        return {"ok": False, "msg": f"{_msg} (code {resp.get('code')})"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


@app.post("/api/smart-money/stop-copy", dependencies=[Depends(require_admin)])
async def smart_money_stop_copy(data: dict = None):
    """Stop copying a trader on OKX."""
    data = data or {}
    code = data.get("unique_code", "")
    if not code:
        return {"ok": False, "msg": "unique_code required"}
    okx = _sm_okx_api()
    try:
        resp = await okx.stop_copy(inst_type="SWAP", unique_code=str(code).strip())
        if resp.get("code") == "0":
            try:
                from .smart_money_ledger import get_sm_ledger
                get_sm_ledger().record_close(
                    kind="copy", symbol="PORTFOLIO", side="copy",
                    size=0, price=0, pnl=0, leader=str(code).strip(),
                    source="okx", note="OKX copy stopped",
                )
            except Exception:
                pass
            return {"ok": True, "msg": "copying stopped"}
        _data = resp.get("data") or []
        _msg = resp.get("msg", "unknown")
        if _data and isinstance(_data, list):
            _smsg = (_data[0] or {}).get("sMsg", "")
            if _smsg:
                _msg = _smsg
        return {"ok": False, "msg": f"{_msg} (code {resp.get('code')})"}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


@app.get("/api/smart-money/my-copies")
async def smart_money_my_copies():
    """Get list of traders we're currently copying."""
    okx = _sm_okx_api()
    try:
        resp = await okx.get_my_lead_traders()
        if resp.get("code") == "0":
            return {"copies": resp.get("data", [])}
        return {"copies": [], "error": resp.get("msg", "")}
    except Exception as e:
        return {"copies": [], "error": str(e)}




@app.get("/api/smart-money/pnl")
async def smart_money_pnl():
    """PnL and open/closed trades only for Smart Money (copy + mirror)."""
    from app.services.smart_money_ledger import get_sm_ledger
    return get_sm_ledger().snapshot()


@app.get("/api/smart-money/trades")
async def smart_money_trades(limit: int = 100):
    from app.services.smart_money_ledger import get_sm_ledger
    return {"trades": get_sm_ledger().trades(limit=limit), "bot_id": "smart_money"}


@app.get("/api/smart-money/mirror/status")
async def smart_money_mirror_status():
    from app.services.smart_money_mirror import get_mirror
    m = get_mirror(client_manager=client_manager, notifier=None, db=db)
    return m.get_status()


@app.post("/api/smart-money/mirror/start", dependencies=[Depends(require_admin)])
async def smart_money_mirror_start(data: dict = None):
    """Start mirroring a public Hyperliquid trader onto OKX."""
    # Mirroring runs a background thread with its own event loop. On the
    # free-tier Render instance this destabilizes the process (site goes
    # down with empty 503, no crash log). Disabled until it can be reworked
    # to avoid per-thread async/network loops. OKX Copy Trading (one-shot
    # REST call) remains fully supported via /api/smart-money/copy.
    return {"ok": False, "msg": "Зеркала HL→OKX временно отключены — перерабатываются. Доступно OKX Copy Trading."}


@app.post("/api/smart-money/mirror/stop", dependencies=[Depends(require_admin)])
async def smart_money_mirror_stop(data: dict = None):
    from app.services.smart_money_mirror import get_mirror
    data = data or {}
    address = data.get("address") or data.get("unique_code") or ""
    m = get_mirror(client_manager=client_manager, notifier=None, db=db)
    return await m.stop_mirror(address, close_positions=bool(data.get("close_positions", False)))


@app.post("/api/smart-money/start", dependencies=[Depends(require_admin)])
async def smart_money_start(data: dict = None):
    """Start the Smart Money Tracker (+ restore mirror claims)."""
    global sm_tracker
    if os.getenv("SM_EXECUTION_DISABLED", "0").strip().lower() in ("1", "true", "yes", "on"):
        return {"ok": False, "msg": "Фоновый мониторинг Smart Money временно отключён — раздел перерабатывается"}
    data = data or {}
    try:
        if sm_tracker and getattr(sm_tracker, "_running", False):
            st = sm_tracker.get_status()
            return {"message": "Already running", **st}
        cfg = TrackerConfig(
            capital=float(data.get("capital") or 500),
            max_leverage=int(data.get("max_leverage") or 3),
            execute=bool(data.get("execute", False)),
            sort_type=data.get("sort_type") or "pnl_ratio",
            min_roi_pct=float(data.get("min_roi_pct") or 5.0),
            min_win_rate=float(data.get("min_win_rate") or 0.45),
            max_max_drawdown=float(data.get("max_max_drawdown") or 0.30),
            tp_ratio=float(data.get("tp_ratio") or 0.10),
            sl_ratio=float(data.get("sl_ratio") or 0.05),
            poll_interval_sec=float(data.get("poll_interval_sec") or 60),
        )
        okx_api = OKXCopyAPI(
            api_key=_env_key or os.getenv("OKX_API_KEY", ""),
            secret_key=_env_secret or os.getenv("OKX_SECRET_KEY", ""),
            passphrase=_env_pass or os.getenv("OKX_PASSPHRASE", ""),
            demo=_env_demo,
        )
        sm_tracker = SmartMoneyTracker(
            config=cfg, client_manager=client_manager, db=db,
            notifier=None, okx_api=okx_api,
        )
        if hasattr(sm_tracker, "hydrate_from_db"):
            await sm_tracker.hydrate_from_db()
        sm_tracker.start()
        try:
            await sm_tracker.persist_to_db()
        except Exception as e:
            print(f"[sm/start] db persist: {e}", flush=True)
        # Restore mirror + claims for open SM positions (e.g. BTC)
        try:
            from app.services.smart_money_mirror import get_mirror
            m = get_mirror(client_manager=client_manager, notifier=None, db=db)
            await m.hydrate_from_db()
        except Exception as e:
            print(f"[sm/start] mirror hydrate: {e}", flush=True)
        st = sm_tracker.get_status()
        return {"message": "Smart Money Tracker started", "ok": True, **st}
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=f"Smart Money start failed: {e}")


@app.post("/api/smart-money/stop", dependencies=[Depends(require_admin)])
async def smart_money_stop():
    """Stop the Smart Money Tracker."""
    global sm_tracker
    if sm_tracker:
        sm_tracker.stop()
    return {"message": "Smart Money Tracker stopped", "running": False}


@app.post("/api/smart-money/config", dependencies=[Depends(require_admin)])
async def smart_money_update_config(data: dict = None):
    """Update tracker config at runtime (capital, TP/SL, leverage, etc)."""
    global sm_tracker
    if not sm_tracker:
        return {"ok": False, "msg": "Tracker not initialized"}
    data = data or {}
    allowed = {
        "capital", "max_leverage", "tp_ratio", "sl_ratio",
        "max_daily_loss_pct", "max_open_copies", "copy_mode",
        "min_roi_pct", "min_win_rate", "max_max_drawdown",
        "min_lead_days", "poll_interval_sec", "execute",
    }
    filtered = {k: v for k, v in data.items() if k in allowed and v is not None}
    res = sm_tracker.update_config(**filtered)
    try:
        await sm_tracker.persist_to_db()
    except Exception as e:
        print(f"[sm/config] db persist: {e}", flush=True)
    return res


@app.get("/api/smart-money/trader/{unique_code}/history")
async def smart_money_trader_history(unique_code: str, limit: int = 50):
    """Get closed trade history for a trader."""
    global sm_tracker
    if not sm_tracker or not sm_tracker.okx_api:
        return {"trades": []}
    try:
        resp = await sm_tracker.okx_api.get_trader_position_history(
            unique_code, limit=str(limit)
        )
        if resp.get("code") == "0":
            trades = []
            for h in resp.get("data", []):
                trades.append({
                    "instId": h.get("instId", ""),
                    "side": h.get("side", ""),
                    "sz": h.get("sz", ""),
                    "avgPx": h.get("avgPx", ""),
                    "pnl": float(h.get("pnl", 0)),
                    "pnlRatio": float(h.get("pnlRatio", 0)),
                    "openTime": h.get("cTime", ""),
                    "closeTime": h.get("uTime", ""),
                    "lever": h.get("lever", ""),
                })
            return {"trades": trades}
        return {"trades": []}
    except Exception as e:
        return {"trades": [], "error": str(e)}


@app.get("/api/vwap_rev/status")
async def vwap_rev_status():
    global vwap_rev_bot
    if not vwap_rev_bot:
        return {
            "running": False,
            "strategy": VWAP_NAME,
            "version": VWAP_VERSION,
            "description": VWAP_DESC,
            "execute": False,
            "open_positions": [],
            "total_pnl": 0,
            "recent_signals": [],
        }
    return vwap_rev_bot.get_status()


@app.post("/api/vwap_rev/start", dependencies=[Depends(require_admin)])
async def vwap_rev_start(data: dict = None):
    global vwap_rev_bot
    data = data or {}
    if vwap_rev_bot and getattr(vwap_rev_bot, "_running", False):
        return {"message": "VWAP Mean Reversion already running", **vwap_rev_bot.get_status()}
    cfg = VWAPScalpConfig(
        capital=float(data.get("capital") or 5000),
        max_leverage=float(data.get("max_leverage") or 2),
        risk_per_trade=float(data.get("risk_per_trade") or 0.008),
    )
    if data.get("symbols"):
        cfg.symbols = list(data["symbols"])
    vwap_rev_bot = VWAPMeanReversion(
        config=cfg, client_manager=client_manager, db=db, notifier=None,
    )
    vwap_rev_bot.start()
    return {"message": "VWAP Mean Reversion started", **vwap_rev_bot.get_status()}


@app.post("/api/vwap_rev/stop", dependencies=[Depends(require_admin)])
async def vwap_rev_stop():
    global vwap_rev_bot
    if vwap_rev_bot:
        vwap_rev_bot.stop()
    return {"message": "VWAP Mean Reversion stopped", "running": False}



@app.get("/api/health/positions-claims", dependencies=[Depends(require_admin)])
async def health_positions_claims():
    """Compare OKX open SWAP positions vs DB strategy claims."""
    from app.services.position_claim import norm_side
    client = client_manager.get_client()
    exchange = []
    if client:
        try:
            res = await client.get_positions("SWAP")
            for p in (res.get("data") or []):
                try:
                    sz = abs(float(p.get("pos") or 0))
                except (TypeError, ValueError):
                    sz = 0
                if sz <= 0:
                    continue
                exchange.append({
                    "inst_id": p.get("instId"),
                    "side": norm_side(p.get("posSide") or "net"),
                    "size": sz,
                    "upl": float(p.get("upl") or 0),
                })
        except Exception as e:
            return {"ok": False, "error": str(e)}
    claims = []
    try:
        rows = await db.get_all_positions() if hasattr(db, "get_all_positions") else []
        for r in rows or []:
            claims.append({
                "bot_id": r.get("bot_id"),
                "inst_id": r.get("inst_id"),
                "side": r.get("side"),
                "size": r.get("size"),
            })
    except Exception as e:
        claims = [{"error": str(e)}]
    cl_keys = set()
    for c in claims:
        if c.get("inst_id"):
            cl_keys.add((c.get("inst_id"), norm_side(c.get("side") or "long")))
            cl_keys.add((c.get("inst_id"), "net"))
    only_exchange = [x for x in exchange if (x["inst_id"], x["side"]) not in cl_keys]
    only_claims = [
        c for c in claims
        if c.get("inst_id") and (c.get("inst_id"), norm_side(c.get("side") or "long")) not in
        {(e["inst_id"], e["side"]) for e in exchange}
    ]
    return {
        "ok": len(only_exchange) == 0,
        "exchange_count": len(exchange),
        "claims_count": len([c for c in claims if c.get("inst_id")]),
        "only_on_exchange": only_exchange,
        "only_in_db_claims": only_claims,
        "exchange": exchange,
        "claims": claims,
    }

@app.get("/api/health")
async def health():
    """Liveness + connection + bot run flags (for keep-alive monitors and UI)."""
    client = client_manager.get_client()
    connected = client is not None
    uptime = None
    if _STARTED_AT is not None:
        uptime = round(_time.time() - _STARTED_AT, 1)

    def _bot_flag(bot) -> bool:
        return bool(bot is not None and getattr(bot, "_running", False))

    # Lightweight process memory + SM diagnostics (no heavy calls, no secrets).
    diag = {}
    try:
        import resource
        diag["rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024, 1)
    except Exception:
        pass
    try:
        diag["sm_running"] = bool(getattr(sm_tracker, "_running", False))
        diag["sm_tracked"] = len(getattr(sm_tracker, "_traders", {}) or {})
        diag["sm_error"] = (getattr(sm_tracker, "_last_error", "") or "")[:200]
        diag["sm_execute"] = bool(getattr(getattr(sm_tracker, "config", None), "execute", False))
    except Exception:
        pass
    try:
        from app.services.smart_money_mirror import get_mirror
        m = get_mirror(client_manager=client_manager, notifier=None, db=db)
        diag["sm_mirror_running"] = bool(getattr(m, "_running", False))
        diag["sm_mirror_targets"] = len(getattr(m, "_targets", {}) or {})
    except Exception:
        pass
    # Read crash log (faulthandler dump) if it exists — try multiple paths
    try:
        import os as _os
        _crash_paths = [
            _os.environ.get("DATA_DIR", "/tmp") + "/crash_traceback.log",
            "/tmp/crash_traceback.log",
            _os.environ.get("HOME", "/tmp") + "/crash_traceback.log",
        ]
        for _cp in _crash_paths:
            if _os.path.exists(_cp) and _os.path.getsize(_cp) > 0:
                with open(_cp) as _f:
                    diag["crash_log"] = _f.read()[-2000:]
                break
        # log current rss to a file too (OOM hint)
        try:
            import resource as _r
            _cur = _r.getrusage(_r.RUSAGE_SELF).ru_maxrss
            diag["rss_current"] = round(_cur / 1024, 1)  # KB->MB approx (linux) / bytes->MB (mac)
        except Exception:
            pass
    except Exception:
        pass
    # Telegram config status
    diag["telegram"] = {
        "configured": telegram.configured,
        "status": telegram.status,
        "chat_id": (telegram.chat_id[:2] + "…" + telegram.chat_id[-3:]) if telegram.chat_id else "",
        "token": (telegram.token[:6] + "…" + telegram.token[-4:]) if telegram.token else "",
    }
    # AI env config
    diag["ai_env"] = {
        "AI_EXECUTE": os.getenv("AI_EXECUTE", "(unset)"),
        "AI_AUTO_START": os.getenv("AI_AUTO_START", "(unset)"),
        "AI_EXEC_CFG": None if ai_bot is None or ai_bot.config.execute is None else ai_bot.config.execute,
        "MOM_AUTO_START": os.getenv("MOM_AUTO_START", "(unset, default off)"),
        "IMP_AUTO_START": os.getenv("IMP_AUTO_START", "(unset, default off)"),
        "VAL_AUTO_START": os.getenv("VAL_AUTO_START", "(unset, default off)"),
    }
    # PnL diagnostics: epoch + per_bot + recent trades, so we can see why
    # cards may look wrong (e.g. -288 today).
    try:
        diag["pnl_epoch"] = await get_pnl_epoch()
        _pr = await get_pnl()
        diag["pnl_total"] = _pr.get("total")
        diag["pnl_1d"] = _pr.get("1d")
        diag["pnl_week"] = _pr.get("week")
        diag["pnl_per_bot"] = _pr.get("per_bot")
        diag["pnl_source"] = _pr.get("source")
        diag["pnl_skipped_untagged"] = _pr.get("skipped_untagged")
        # Last trades with PnL to explain the daily number
        try:
            _pt = await get_paired_trades(limit=500)
            from datetime import datetime as _dt, timezone as _tz
            _now = _dt.now(_tz.utc)
            _today = []
            _eth_debug = []
            for _t in _pt.get("trades", []):
                _ts = _t.get("exit_time") or _t.get("time") or ""
                if not _ts:
                    continue
                try:
                    _parsed = _dt.fromisoformat(_ts)
                    if _parsed.tzinfo is None:
                        _parsed = _parsed.replace(tzinfo=_tz.utc)
                    _today_flag = (_now - _parsed).total_seconds() <= 86400
                except Exception:
                    _today_flag = False
                _pnl = _t.get("pnl")
                if _pnl is None:
                    continue
                _inst = _t.get("inst_id") or _t.get("symbol", "")
                if "ETH" in _inst:
                    _eth_debug.append({
                        "time": _ts[:19],
                        "ord_id": str(_t.get("ord_id", ""))[:24],
                        "entry_ord_id": str(_t.get("entry_ord_id", ""))[:24],
                        "bot": _t.get("bot", ""),
                        "pnl": round(float(_pnl), 2),
                        "reason": _t.get("reason", ""),
                    })
                if not _today_flag:
                    continue
                _today.append({
                    "time": _ts[:19],
                    "inst": _inst,
                    "pnl": round(float(_pnl), 2),
                    "bot": _t.get("bot") or "",
                    "side": _t.get("side", ""),
                    "reason": _t.get("reason", ""),
                })
            _today.sort(key=lambda x: x["time"], reverse=True)
            diag["pnl_today_trades"] = _today[:15]
            _ieb = _pt.get("debug", {}).get("inst_entry_bot", {}) or {}
            diag["inst_entry_bot"] = _json_safe_dict(_ieb)
            if _eth_debug:
                diag["pnl_eth_debug"] = _eth_debug
        except Exception as e:
            diag["pnl_today_trades"] = None
    except Exception as e:
        diag["pnl_err"] = str(e)

    return {
        "status": "ok",
        "connected": connected,
        "demo": _env_demo,
        "version": os.environ.get("RENDER_GIT_COMMIT", "")[:12],
        "uptime_sec": uptime,
        "bots": {
            "rotation": _bot_flag(rotation),
            "impulse": _bot_flag(impulse),
            "validation": _bot_flag(validation),
            "ai": _bot_flag(ai_bot),
            "scalp": False,
            "vwap_rev": _bot_flag(vwap_rev_bot),
            "smart_money": bool(getattr(sm_tracker, "_running", False)),
        },
        "auth": "jwt",
        "risk": risk_get_status().to_dict(),
        "sm_diag": diag,
    }


# ── Risk guards (stage-3a) ──

@app.get("/api/risk/status")
async def risk_status():
    """Public-ish status for UI badges (no secrets)."""
    daily = None
    try:
        # best-effort daily pnl from existing endpoint helper if present
        from app.services import risk_guard as _rg  # noqa: F401
    except Exception:
        pass
    st = risk_get_status(daily_pnl=None)
    return st.to_dict()


@app.post("/api/risk/kill", dependencies=[Depends(require_admin)])
async def risk_kill(request: Request, data: dict = None):
    """Enable/disable runtime kill switch (blocks new entries, not closes)."""
    data = data or {}
    enabled = bool(data.get("enabled", True))
    set_kill_switch(enabled)
    await write_audit(request, "risk.kill_switch", detail=f"enabled={enabled}")
    return {"ok": True, **risk_get_status().to_dict()}


# ── Credentials ──

@app.get("/api/credentials/status", dependencies=[Depends(require_admin)])
async def credentials_status():
    configured = bool(_env_key and _env_secret and _env_pass)
    return {"configured": configured, "demo": _env_demo}


@app.post("/api/credentials/test", dependencies=[Depends(require_admin)])
async def credentials_test(data: dict):
    key = data.get("apiKey", _env_key)
    secret = data.get("secretKey", _env_secret)
    passphrase = data.get("passphrase", _env_pass)
    demo = data.get("demo", _env_demo)
    try:
        test_manager = OKXClientManager()
        result = await test_manager.init_client(key, secret, passphrase, demo)
        if result.get("error"):
            return {"success": False, "message": result.get("message", "Connection failed")}
        return {"success": True, "message": "Connected successfully"}
    except Exception as e:
        return {"success": False, "message": str(e)}


@app.post("/api/credentials/init", dependencies=[Depends(require_admin)])
async def credentials_init(request: Request, data: dict):
    global _env_key, _env_secret, _env_pass, _env_demo
    key = data.get("apiKey", "")
    secret = data.get("secretKey", "")
    passphrase = data.get("passphrase", "")
    demo = data.get("demo", True)

    if not key or not secret or not passphrase:
        raise HTTPException(status_code=400, detail="All credentials required")

    _env_key = key
    _env_secret = secret
    _env_pass = passphrase
    _env_demo = demo

    result = await client_manager.init_client(key, secret, passphrase, demo)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result.get("message", "Connection failed"))
    await write_audit(request, "credentials.init", detail=f"demo={bool(demo)}")
    return {"message": "Credentials configured", "demo": demo}


@app.get("/api/mode", dependencies=[Depends(require_admin)])
async def get_trading_mode():
    return {"demo": _env_demo, "okx_demo": _env_demo, "live": not _env_demo}


@app.post("/api/mode", dependencies=[Depends(require_admin)])
async def set_trading_mode(request: Request, data: dict = None):
    """Switch DEMO/LIVE for the owner OKX client.

    Switching to LIVE requires confirm == "LIVE" to avoid accidental flips.
    """
    global _env_demo
    data = data or {}
    demo = bool(data.get("demo", True))
    if not demo:
        if str(data.get("confirm", "")).strip() != "LIVE":
            raise HTTPException(
                status_code=400,
                detail='Switching to LIVE requires confirm: "LIVE"',
            )
    if not (_env_key and _env_secret and _env_pass):
        raise HTTPException(status_code=400, detail="OKX credentials not configured")
    prev = _env_demo
    _env_demo = demo
    result = await client_manager.init_client(_env_key, _env_secret, _env_pass, demo)
    if result.get("error"):
        _env_demo = prev
        raise HTTPException(status_code=400, detail=result.get("message", "Reconnect failed"))
    await write_audit(
        request,
        "mode.switch",
        detail=f"{'DEMO' if prev else 'LIVE'} -> {'DEMO' if demo else 'LIVE'}",
    )
    return {"ok": True, "demo": _env_demo, "live": not _env_demo}


@app.get("/api/audit", dependencies=[Depends(require_admin)])
async def get_audit(limit: int = 100):
    rows = await db.list_audit(limit=limit)
    return {"items": rows}


# ── Portfolio ──

@app.get("/api/portfolio")
async def get_portfolio():
    global _portfolio_cache, _portfolio_cache_ts
    now_s = _time.time()
    if _portfolio_cache is not None and (now_s - _portfolio_cache_ts) < _POS_CACHE_TTL:
        return _portfolio_cache
    result = await _okx_call(lambda c: c.get_balance())
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result.get("message", ""))
    data = result.get("data", [])
    if not data:
        return {"totalEqUsd": 0, "details": []}
    acct = data[0] if isinstance(data, list) else data
    total_eq = float(acct.get("totalEq", 0))
    details = []
    for d in acct.get("details", []):
        details.append({
            "ccy": d.get("ccy"),
            "eq": float(d.get("eq", 0)),
            "eqUsd": float(d.get("eqUsd", 0)),
            "availBal": float(d.get("availBal", 0)),
            "frozenBal": float(d.get("frozenBal", 0)),
        })
    out = {"totalEqUsd": total_eq, "details": details}
    _portfolio_cache = out
    _portfolio_cache_ts = _time.time()
    return out


# ── Positions ──

def _tag_position_bot(inst_id: str, pos_side: str, *, db_pos_map: dict | None = None) -> str:
    """Determine which bot owns an OKX position.

    Priority: in-memory _positions → trade logs → DB positions table → empty.
    """
    norm_side = pos_side.lower() if pos_side else ""

    def _match(bot) -> bool:
        # Do not require _running: after redeploy memory may still be refilled
        # while status is stopped, or restore completed on first tick.
        if not (bot and getattr(bot, "_positions", None)):
            return False
        for coin, pos in bot._positions.items():
            if pos.inst_id == inst_id and (
                pos.side == norm_side or norm_side in ("", "net")
                or (norm_side in ("long", "short") and pos.side in ("long", "short"))
            ):
                # side must agree when both are directional
                if norm_side in ("long", "short") and pos.side in ("long", "short"):
                    if pos.side != norm_side:
                        continue
                return True
        return False

    if _match(rotation):
        return "Momentum"
    if _match(impulse):
        return "Impulse 1D"
    if _match(validation):
        return "MACD+Donchian Validation"
    if _match(ai_bot):
        return "AI Discretionary 1H"

    # Fallback: trade logs (same priority)
    if rotation and rotation._trade_log:
        for t in reversed(rotation._trade_log):
            sym = t.get("symbol", "") or t.get("inst_id", "")
            if sym == inst_id and t.get("reason") == "open":
                return "Momentum"
    if impulse and impulse._trade_log:
        for t in reversed(impulse._trade_log):
            sym = t.get("symbol", "") or t.get("inst_id", "")
            if sym == inst_id and t.get("reason") == "open":
                return "Impulse 1D"
    if validation and validation._trade_log:
        for t in reversed(validation._trade_log):
            sym = t.get("symbol", "") or t.get("inst_id", "")
            if sym == inst_id and t.get("reason") == "open":
                return "MACD+Donchian Validation"
    if ai_bot and ai_bot._trade_log:
        for t in reversed(ai_bot._trade_log):
            sym = t.get("symbol", "") or t.get("inst_id", "")
            if sym == inst_id and t.get("reason") == "open":
                return "AI Discretionary 1H"
    if vwap_rev_bot and vwap_rev_bot._trade_log:
        for t in reversed(vwap_rev_bot._trade_log):
            sym = t.get("symbol", "") or t.get("inst_id", "")
            if sym == inst_id and t.get("reason") == "open":
                return "VWAP Mean Reversion"

    # Fallback: DB positions table (survives restarts)
    if db_pos_map is not None:
        # Try exact side match first
        bot_id = db_pos_map.get((inst_id, norm_side))
        if not bot_id and norm_side == "net":
            # One-way mode: OKX returns "net" but DB stores "long" or "short"
            bot_id = db_pos_map.get((inst_id, "long")) or db_pos_map.get((inst_id, "short"))
        if not bot_id:
            # Last resort: any position for this instrument
            for (iid, _), bid in db_pos_map.items():
                if iid == inst_id:
                    bot_id = bid
                    break
        if bot_id:
            name = _db_bot_name(bot_id)
            if name:
                return name

    return ""


def _tag_trade_bot(trade: dict, *, db_pos_map: dict | None = None) -> str:
    """Tag a paired trade with bot name. Works for both open and closed trades."""
    inst_id = trade.get("inst_id", "") or trade.get("symbol", "")
    pos_side = trade.get("pos_side", "")
    # DB bot_id is authoritative when present
    by_id = _db_bot_name(trade.get("bot_id", "") or "")
    if by_id:
        return by_id
    if trade.get("reason") == "open":
        return _tag_position_bot(inst_id, pos_side, db_pos_map=db_pos_map)
    # Prefer exact ordId match against in-memory close logs (most reliable)
    ord_id = str(trade.get("ord_id") or trade.get("close_ord_id") or "").strip()
    if ord_id:
        for bot_label, log in (
            ("Momentum", getattr(rotation, "_trade_log", None) if rotation else None),
            ("Impulse 1D", getattr(impulse, "_trade_log", None) if impulse else None),
            ("MACD+Donchian Validation", getattr(validation, "_trade_log", None) if validation else None),
            ("AI Discretionary 1H", getattr(ai_bot, "_trade_log", None) if ai_bot else None),
            ("VWAP Mean Reversion", getattr(vwap_rev_bot, "_trade_log", None) if vwap_rev_bot else None),
        ):
            if not log:
                continue
            for t in log:
                if str(t.get("ord_id", "") or "").strip() == ord_id:
                    return bot_label
    # For closed trades, check trade logs for matching entry+exit
    entry_time = trade.get("entry_time", "")
    if rotation and rotation._trade_log:
        for t in rotation._trade_log:
            if t.get("time", "") == entry_time and t.get("symbol", "") == inst_id:
                return "Momentum"
    if impulse and impulse._trade_log:
        for t in impulse._trade_log:
            if t.get("time", "") == entry_time and t.get("symbol", "") == inst_id:
                return "Impulse 1D"
    if validation and validation._trade_log:
        for t in validation._trade_log:
            if t.get("time", "") == entry_time and t.get("symbol", "") == inst_id:
                return "MACD+Donchian Validation"
    if ai_bot and ai_bot._trade_log:
        for t in ai_bot._trade_log:
            if t.get("time", "") == entry_time and t.get("symbol", "") == inst_id:
                return "AI Discretionary 1H"
    if vwap_rev_bot and vwap_rev_bot._trade_log:
        for t in vwap_rev_bot._trade_log:
            if t.get("time", "") == entry_time and t.get("symbol", "") == inst_id:
                return "VWAP Mean Reversion"
    # Do NOT match by symbol+side alone — that wrongly attached Impulse SOL etc. to AI
    # after redeploy adoption and corrupted strategy PnL.
    return _db_bot_name(trade.get("bot_id", ""))


def _db_bot_name(bot_id: str) -> str:
    """Map DB bot_id -> UI bot name. Handles per-user suffixed ids."""
    if not bot_id:
        return ""
    base = str(bot_id).split(":")[0]
    if base in ("momentum_strategy", "rotation_strategy", MOM_BOT_ID, ROT_BOT_ID):
        return "Momentum"
    if base in ("impulse_strategy", IMP_BOT_ID):
        return "Impulse 1D"
    if base == VAL_BOT_ID:
        return "MACD+Donchian Validation"
    if base == AI_BOT_ID:
        return "AI Discretionary 1H"
    if base == SCALP_BOT_ID:
        return "Order Book Scalp"
    if base == VWAP_BOT_ID:
        return "VWAP Mean Reversion"
    if base in ("smart_money", "smart_money_mirror", "sm_mirror"):
        return "Умные деньги"
    if base in (
        "Momentum", "Impulse 1D", "MACD+Donchian Validation",
        "AI Discretionary 1H", "Order Book Scalp", "Умные деньги",
    ):
        return base
    return ""


async def get_pnl_epoch() -> str:
    """ISO timestamp; trades before this are ignored for strategy cards & total stats."""
    try:
        v = await db.get_setting("pnl_epoch")
        if v and str(v).strip():
            return str(v).strip()
    except Exception:
        pass
    return ""


def _trade_after_epoch(tr: dict, epoch: str) -> bool:
    """Filter closed history by epoch; always keep live open rows."""
    if not epoch:
        return True
    reason = (tr.get("reason") or "").lower()
    # Open / live positions must survive epoch (PnL reset is for realized only)
    if reason in ("open", "add") or tr.get("pnl") is None:
        return True
    ts = (
        tr.get("exit_time")
        or tr.get("time")
        or tr.get("entry_time")
        or tr.get("timestamp")
        or ""
    )
    if not ts:
        return True
    try:
        return str(ts)[:19] >= str(epoch)[:19]
    except Exception:
        return True


async def _orphan_sweep_loop():

    """Periodically close exchange positions not owned by any strategy."""
    global _positions_cache
    import asyncio as _asyncio
    await _asyncio.sleep(45)  # let bots restore first
    while True:
        try:
            client = client_manager.get_client()
            if client and orphan_close_enabled():
                mem = set()
                for bot in (rotation, impulse, validation, ai_bot, vwap_rev_bot, sm_tracker):
                    if not bot or not getattr(bot, "_positions", None):
                        continue
                    for pos in bot._positions.values():
                        mem.add((getattr(pos, "inst_id", None) or "", getattr(pos, "side", "long")))
                closed = await sweep_exchange_orphans(client, db, mem)
                if closed:
                    print(f"[orphan-sweep] closed {len(closed)}: {closed}", flush=True)
                    _positions_cache = None
        except Exception as e:
            print(f"[orphan-sweep] error: {e}", flush=True)
        await _asyncio.sleep(120)  # every 2 min


@app.post("/api/positions/sweep-orphans", dependencies=[Depends(require_admin)])
async def sweep_orphans():
    """Close exchange positions not claimed by any strategy (anti-orphan)."""
    client = client_manager.get_client()
    if not client:
        raise HTTPException(status_code=400, detail="API not configured")
    mem = set()
    for bot in (rotation, impulse, validation, ai_bot, vwap_rev_bot, sm_tracker):
        if not bot or not getattr(bot, "_positions", None):
            continue
        for pos in bot._positions.values():
            mem.add((pos.inst_id, getattr(pos, "side", "long")))
    closed = await sweep_exchange_orphans(client, db, mem)
    global _positions_cache
    _positions_cache = None
    return {"closed": closed, "n": len(closed)}


@app.get("/api/positions")
async def get_positions(inst_type: str = "SWAP"):
    global _positions_cache, _positions_cache_ts, _POS_RECLAIM_TS
    now_s = _time.time()
    if _positions_cache is not None and (now_s - _positions_cache_ts) < _POS_CACHE_TTL:
        return _positions_cache
    # Heavy OKX fills/algo reclaim — at most once per _POS_RECLAIM_TTL
    do_heavy_reclaim = (now_s - float(_POS_RECLAIM_TS or 0)) >= float(_POS_RECLAIM_TTL or 90)
    result = await _okx_call(lambda c: c.get_positions(inst_type))
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result.get("message", ""))
    # Build DB positions map for fallback tagging (survives restarts)
    db_pos_map = {}
    try:
        db_rows = await db.get_all_positions()
        for row in db_rows:
            db_pos_map[(row.get("inst_id", ""), row.get("side", ""))] = row.get("bot_id", "")
    except Exception:
        pass
    # Merge durable open_positions:{bot_id} snapshots (survive trade wipes)
    try:
        import json
        for bid in (ROT_BOT_ID, IMP_BOT_ID, VAL_BOT_ID, AI_BOT_ID, "smart_money"):
            try:
                raw = await db.get_setting(f"open_positions:{bid}")
                if not raw:
                    continue
                data = json.loads(raw) if isinstance(raw, str) else raw
                for row in data or []:
                    iid = row.get("inst_id") or ""
                    side = (row.get("side") or "long").lower()
                    if iid and (iid, side) not in db_pos_map:
                        db_pos_map[(iid, side)] = bid
            except Exception:
                continue
    except Exception:
        pass
    # Tag each position with bot name; auto-reclaim if last trade was ours
    tagged = []

    async def _inject_bot_memory(bot_label: str, inst_id: str, side: str, sz: float, entry: float):
        """Rehydrate strategy in-memory book so UI botMap + management work after deploy."""
        global rotation, impulse, validation, ai_bot
        coin = inst_id.replace("-USDT-SWAP", "").replace("-USD-SWAP", "")
        entry = float(entry or 0)
        sz = float(sz or 0)
        if entry <= 0 or sz <= 0 or not coin:
            return
        stop = entry * 0.985 if side == "long" else entry * 1.015
        now_iso = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
        try:
            if bot_label == "Momentum" and rotation:
                from app.services.rotation_strategy import RotPosition
                if coin not in (getattr(rotation, "_positions", None) or {}):
                    rotation._positions[coin] = RotPosition(
                        symbol=inst_id, coin=coin, inst_id=inst_id, side=side,
                        size=sz, size_original=sz, entry_price=entry, stop_price=stop,
                        peak_price=entry, opened_at=now_iso,
                        atr=entry * 0.015, atr_hourly=entry * 0.015,
                        leverage=float(getattr(getattr(rotation, "config", None), "max_leverage", 3) or 3),
                    )
                    print(f"[positions] injected {coin} → Momentum", flush=True)
            elif bot_label == "Impulse 1D" and impulse:
                # Impulse stores positions similarly (coin key)
                pos_map = getattr(impulse, "_positions", None)
                if pos_map is not None and coin not in pos_map:
                    P = getattr(impulse, "Position", None) or getattr(impulse, "ImpPosition", None)
                    if P is None:
                        # minimal duck object
                        class _P:
                            pass
                        p = _P()
                        p.symbol = inst_id; p.coin = coin; p.inst_id = inst_id
                        p.side = side; p.size = sz; p.size_original = sz
                        p.entry_price = entry; p.stop_price = stop; p.peak_price = entry
                        p.opened_at = now_iso; p.atr = entry * 0.015
                        pos_map[coin] = p
                    else:
                        try:
                            pos_map[coin] = P(
                                symbol=inst_id, coin=coin, inst_id=inst_id, side=side,
                                size=sz, entry_price=entry, stop_price=stop,
                            )
                        except TypeError:
                            p = P.__new__(P)
                            for k, v in dict(symbol=inst_id, coin=coin, inst_id=inst_id, side=side,
                                             size=sz, entry_price=entry, stop_price=stop).items():
                                try:
                                    setattr(p, k, v)
                                except Exception:
                                    pass
                            pos_map[coin] = p
                    print(f"[positions] injected {coin} → Impulse", flush=True)
            elif bot_label.startswith("MACD") and validation:
                pos_map = getattr(validation, "_positions", None)
                if pos_map is not None and coin not in pos_map:
                    from app.services.rotation_strategy import RotPosition
                    pos_map[coin] = RotPosition(
                        symbol=inst_id, coin=coin, inst_id=inst_id, side=side,
                        size=sz, size_original=sz, entry_price=entry, stop_price=stop,
                        peak_price=entry, opened_at=now_iso,
                        atr=entry * 0.015, atr_hourly=entry * 0.015, leverage=3.0,
                    )
                    print(f"[positions] injected {coin} → Validation", flush=True)
            elif bot_label.startswith("AI") and ai_bot:
                pos_map = getattr(ai_bot, "_positions", None)
                if pos_map is not None and coin not in pos_map:
                    # AI may use dict positions
                    try:
                        pos_map[coin] = {
                            "inst_id": inst_id, "coin": coin, "side": side,
                            "size": sz, "entry_price": entry, "stop_price": stop,
                        }
                    except Exception:
                        pass
                    print(f"[positions] injected {coin} → AI", flush=True)
        except Exception as e:
            print(f"[positions] inject {bot_label}: {e}", flush=True)

    for p in result.get("data", []):
        inst = p.get("instId", "") or ""
        pos_side = (p.get("posSide", "net") or "net").lower()
        try:
            pos_raw = float(p.get("pos") or 0)
        except (TypeError, ValueError):
            pos_raw = 0.0
        # One-way mode: posSide=net, sign of pos indicates direction
        if pos_side == "short" or pos_raw < 0:
            side_n = "short"
        else:
            side_n = "long"
        sz = abs(pos_raw)
        try:
            entry = float(p.get("avgPx") or 0)
        except (TypeError, ValueError):
            entry = 0.0

        bot_name = _tag_position_bot(inst, pos_side, db_pos_map=db_pos_map)
        if not bot_name:
            bot_name = _tag_position_bot(inst, side_n, db_pos_map=db_pos_map)

        if not bot_name and inst:
            try:
                last_bot = await db.last_bot_for_instrument(inst)
                if last_bot and sz > 0 and entry > 0:
                    await claim_open(db, last_bot, inst, side_n, sz, entry)
                    db_pos_map[(inst, side_n)] = last_bot
                    db_pos_map[(inst, "net")] = last_bot
                    bot_name = _db_bot_name(last_bot) or _tag_position_bot(inst, side_n, db_pos_map=db_pos_map)
                    if bot_name:
                        print(f"[positions] reclaimed {inst} {side_n} → {last_bot}", flush=True)
            except Exception as e:
                print(f"[positions] reclaim {inst}: {e}", flush=True)

        if not bot_name and inst and do_heavy_reclaim:
            try:
                fills = await _fetch_okx_fills(limit=100)
                prefix_map = {
                    "rot": (ROT_BOT_ID, "Momentum"),
                    "imp": (IMP_BOT_ID, "Impulse 1D"),
                    "ai": (AI_BOT_ID, "AI Discretionary 1H"),
                    "val": (VAL_BOT_ID, "MACD+Donchian Validation"),
                }
                for f in fills or []:
                    if (f.get("instId") or "") != inst:
                        continue
                    cid = str(f.get("clOrdId") or "").lower()
                    for pref, (bid, label) in prefix_map.items():
                        if cid.startswith(pref):
                            if sz > 0 and entry > 0:
                                await claim_open(db, bid, inst, side_n, sz, entry)
                                db_pos_map[(inst, side_n)] = bid
                                db_pos_map[(inst, "net")] = bid
                                bot_name = label
                                print(f"[positions] reclaimed via clOrdId {cid[:20]} → {label}", flush=True)
                            break
                    if bot_name:
                        break
            except Exception as e:
                print(f"[positions] fill-tag {inst}: {e}", flush=True)

        # Pending algo / stop orders often keep clOrdId longer than fills window
        if not bot_name and inst and do_heavy_reclaim:
            try:
                client = client_manager.get_client()
                if client and hasattr(client, "get_order_list"):
                    pass
                # OKX pending algos via generic call if available
                if client:
                    for meth in ("get_orders_pending", "get_order_list", "orders_pending"):
                        fn = getattr(client, meth, None)
                        if not callable(fn):
                            continue
                        try:
                            ores = await fn(inst_type="SWAP")
                        except TypeError:
                            try:
                                ores = await fn("SWAP")
                            except Exception:
                                continue
                        except Exception:
                            continue
                        for o in (ores.get("data") or []) if isinstance(ores, dict) else []:
                            if (o.get("instId") or "") != inst:
                                continue
                            cid = str(o.get("clOrdId") or "").lower()
                            if cid.startswith("rot"):
                                await claim_open(db, ROT_BOT_ID, inst, side_n, sz, entry)
                                bot_name = "Momentum"
                                print(f"[positions] reclaimed via pending order {cid[:20]} → Momentum", flush=True)
                                break
                            if cid.startswith("imp"):
                                await claim_open(db, IMP_BOT_ID, inst, side_n, sz, entry)
                                bot_name = "Impulse 1D"
                                break
                        if bot_name:
                            break
            except Exception as e:
                print(f"[positions] pending-tag {inst}: {e}", flush=True)

        # Last resort: unique running strategy whose universe contains coin and no other claim
        if not bot_name and inst and sz > 0 and entry > 0:
            try:
                coin = inst.replace("-USDT-SWAP", "").replace("-USD-SWAP", "")
                candidates = []
                # (bot_id, label, bot_obj, universe)
                try:
                    from app.services.rotation_strategy import COINS as _RC
                except Exception:
                    _RC = ["BTC", "ETH", "BNB", "XRP", "SOL", "DOGE", "ADA", "TRX", "AVAX", "LTC"]
                if rotation and getattr(rotation, "_running", False):
                    univ = list(getattr(getattr(rotation, "config", None), "symbols", None) or _RC)
                    if coin in univ:
                        candidates.append((ROT_BOT_ID, "Momentum", rotation))
                if impulse and getattr(impulse, "_running", False):
                    univ = list(getattr(getattr(impulse, "config", None), "symbols", None) or _RC)
                    if coin in univ:
                        candidates.append((IMP_BOT_ID, "Impulse 1D", impulse))
                if validation and getattr(validation, "_running", False):
                    univ = list(getattr(getattr(validation, "config", None), "symbols", None) or _RC)
                    if coin in univ:
                        candidates.append((VAL_BOT_ID, "MACD+Donchian Validation", validation))
                if ai_bot and getattr(ai_bot, "_running", False):
                    univ = list(getattr(getattr(ai_bot, "config", None), "symbols", None) or ["BTC", "ETH", "SOL", "XRP"])
                    if coin in univ:
                        candidates.append((AI_BOT_ID, "AI Discretionary 1H", ai_bot))
                # Only auto-claim when exactly one candidate is running for this coin
                if len(candidates) == 1:
                    bid, label, _bot = candidates[0]
                    other = False
                    try:
                        other = await db.other_bot_owns_position_any(bid, inst, side_n)
                    except Exception:
                        other = False
                    if not other:
                        await claim_open(db, bid, inst, side_n, sz, entry)
                        db_pos_map[(inst, side_n)] = bid
                        bot_name = label
                        print(f"[positions] last-resort claim {inst} → {label} (unique running bot)", flush=True)
            except Exception as e:
                print(f"[positions] last-resort {inst}: {e}", flush=True)

        if bot_name and inst and sz > 0:
            await _inject_bot_memory(bot_name, inst, side_n, sz, entry)

        p["bot"] = bot_name
        p["_side_norm"] = side_n
        tagged.append(p)
    out = {"positions": tagged}
    _positions_cache = out
    _positions_cache_ts = _time.time()
    return out


@app.post("/api/positions/bind", dependencies=[Depends(require_admin)])
async def positions_bind(data: dict = None):
    """Force-bind an OKX position to a strategy (claim + Momentum memory if needed)."""
    data = data or {}
    inst = (data.get("instId") or data.get("inst_id") or "").strip()
    side = (data.get("side") or data.get("posSide") or "long").lower()
    if side in ("sell", "s"):
        side = "short"
    elif side not in ("long", "short"):
        side = "long"
    bot = (data.get("bot") or data.get("bot_id") or "Momentum").strip()
    inv = {
        "Momentum": ROT_BOT_ID,
        "rotation_strategy": ROT_BOT_ID,
        ROT_BOT_ID: ROT_BOT_ID,
        "Impulse 1D": IMP_BOT_ID,
        "impulse_strategy": IMP_BOT_ID,
        IMP_BOT_ID: IMP_BOT_ID,
        "AI Discretionary 1H": AI_BOT_ID,
        AI_BOT_ID: AI_BOT_ID,
        "MACD+Donchian Validation": VAL_BOT_ID,
        VAL_BOT_ID: VAL_BOT_ID,
        "smart_money": "smart_money",
        "Умные деньги": "smart_money",
        "Smart Money": "smart_money",
        "smart_money_mirror": "smart_money",
    }
    bid = inv.get(bot) or inv.get(bot.replace(" ", "_"))
    if not inst or not bid:
        raise HTTPException(status_code=400, detail="instId and bot required")
    # size/entry from exchange
    sz, entry = 0.0, 0.0
    client = client_manager.get_client()
    if client:
        res = await client.get_positions("SWAP")
        for p in res.get("data") or []:
            if p.get("instId") == inst:
                try:
                    sz = abs(float(p.get("pos") or 0))
                    entry = float(p.get("avgPx") or 0)
                except (TypeError, ValueError):
                    pass
                break
    if sz <= 0 or entry <= 0:
        sz = float(data.get("size") or 0)
        entry = float(data.get("entry") or 0)
    if sz <= 0 or entry <= 0:
        raise HTTPException(status_code=400, detail="Cannot resolve size/entry from OKX")
    await claim_open(db, bid, inst, side, sz, entry)
    if bid == ROT_BOT_ID:
        # inject memory via internal helper path: call get_positions logic lightly
        try:
            from app.services.rotation_strategy import RotPosition
            global rotation
            if rotation:
                coin = inst.replace("-USDT-SWAP", "").replace("-USD-SWAP", "")
                stop = entry * 0.985 if side == "long" else entry * 1.015
                rotation._positions[coin] = RotPosition(
                    symbol=inst, coin=coin, inst_id=inst, side=side,
                    size=sz, size_original=sz, entry_price=entry, stop_price=stop,
                    peak_price=entry,
                    opened_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
                    atr=entry * 0.015, atr_hourly=entry * 0.015,
                    leverage=3.0,
                )
                try:
                    await rotation._persist_open_snapshot()
                except Exception:
                    pass
        except Exception as e:
            print(f"[bind] inject: {e}", flush=True)
    global _positions_cache
    _positions_cache = None
    return {"ok": True, "inst_id": inst, "side": side, "bot_id": bid, "size": sz, "entry": entry}


@app.post("/api/positions/close", dependencies=[Depends(require_admin)])

async def close_position(data: dict):
    client = client_manager.get_client()
    if not client:
        raise HTTPException(status_code=400, detail="API not configured")

    inst_id = data.get("instId")
    pos_side = data.get("posSide") or "net"
    mgn_mode = data.get("mgnMode", "cross")

    if not inst_id:
        raise HTTPException(status_code=400, detail="instId required")

    # Auto-detect posSide from the open position if not provided explicitly.
    if pos_side == "net" and "posSide" not in data:
        try:
            positions_resp = await client.get_positions("SWAP")
            if not positions_resp.get("error") and positions_resp.get("data"):
                for p in positions_resp["data"]:
                    if p.get("instId") == inst_id:
                        pos_side = p.get("posSide", "net")
                        break
        except Exception as e:
            print(f"[positions/close] posSide auto-detect error: {e}", flush=True)

    result = await client.close_position(inst_id=inst_id, mgn_mode=mgn_mode, pos_side=pos_side)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result.get("message", ""))
    return {"message": "Position closed", "data": result.get("data")}


# ── Market ──

_ticker_cache: dict = {}
_ticker_cache_ts: dict = {}
_TICKER_TTL = 5  # seconds — dashboard polls 11 tickers every 10s

_positions_cache: dict = None
_positions_cache_ts: float = 0
_portfolio_cache: dict = None
_portfolio_cache_ts: float = 0
_POS_CACHE_TTL = 3  # seconds — avoid 2+ OKX calls per dashboard poll for positions/balance
_POS_RECLAIM_TS = 0.0
_POS_RECLAIM_TTL = 90.0  # heavy fill/algo reclaim at most once per 90s
_FUNDING_CACHE = 0.0
_FUNDING_CACHE_TS = 0.0
_FUNDING_TTL = 120.0
_SM_DISCOVER_CACHE = {"ts": 0.0, "key": "", "data": None}
_SM_DISCOVER_LOCK = None



@app.get("/api/market/ticker")
async def get_ticker(inst_id: str = "BTC-USDT"):
    now_s = _time.time()
    if _ticker_cache.get(inst_id) and (now_s - _ticker_cache_ts.get(inst_id, 0)) < _TICKER_TTL:
        return _ticker_cache[inst_id]
    result = await _okx_call(lambda c: c.get_ticker(inst_id))
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result.get("message", ""))
    data = result.get("data", [{}])[0] if result.get("data") else {}
    if data:
        _ticker_cache[inst_id] = data
        _ticker_cache_ts[inst_id] = _time.time()
    return data


@app.get("/api/market/tickers")
async def get_tickers(inst_id: str = ""):
    """Batch ticker fetch — one request replaces N individual /market/ticker
    calls (the dashboard's 10 coin-price strip)."""
    ids = [i.strip() for i in (inst_id or "").split(",") if i.strip()]
    if not ids:
        return {"tickers": []}
    out = []
    for iid in ids:
        try:
            t = await get_ticker(inst_id=iid)
        except HTTPException:
            t = {}
        if t:
            out.append({"instId": iid, **t})
    return {"tickers": out}


@app.get("/api/market/candles")
async def get_candles(inst_id: str = "BTC-USDT-SWAP", bar: str = "1H", limit: int = 100):
    result = await _okx_call(lambda c: c.get_candles(inst_id, bar=bar, limit=limit))
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result.get("message", ""))
    return {"candles": result.get("data", [])}


# ── Backtest ──

_backtest_sem: Optional[asyncio.Semaphore] = None
_freqtrade_sem: Optional[asyncio.Semaphore] = None
_bt_attempts: dict[str, list[float]] = {}
_BT_MAX_ATTEMPTS = 3
_BT_WINDOW_SEC = 600.0


def _get_backtest_sem() -> asyncio.Semaphore:
    global _backtest_sem
    if _backtest_sem is None:
        _backtest_sem = asyncio.Semaphore(2)
    return _backtest_sem


def _get_freqtrade_sem() -> asyncio.Semaphore:
    global _freqtrade_sem
    if _freqtrade_sem is None:
        _freqtrade_sem = asyncio.Semaphore(1)
    return _freqtrade_sem


def _freqtrade_rate_limited(ip: str) -> bool:
    now = _time.time()
    _bt_attempts[ip] = [t for t in _bt_attempts[ip] if now - t < _BT_WINDOW_SEC]
    if len(_bt_attempts) > 10000:
        _bt_attempts.clear()
    if len(_bt_attempts[ip]) >= _BT_MAX_ATTEMPTS:
        return True
    _bt_attempts[ip].append(now)
    return False


@app.post("/api/backtest/run")
async def backtest_run(data: dict):
    """Run a real-data momentum backtest on OKX candles. Public market data."""
    async with _get_backtest_sem():
        try:
            result = await run_backtest_async(data or {})
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            print(f"[backtest] ERROR: {e}", flush=True)
            raise HTTPException(status_code=500, detail=f"Ошибка бэктеста: {e}")

    # Persist as the "last backtest" so it survives reloads / other devices.
    try:
        await db.set_setting("last_backtest", json.dumps(result, ensure_ascii=False))
    except Exception as e:
        print(f"[backtest] save last result failed: {e}", flush=True)
    return result


@app.post("/api/backtest/freqtrade")
async def backtest_freqtrade(request: Request, data: dict):
    """Run a backtest on the independent freqtrade engine (momentum / impulse)."""
    import re
    import subprocess

    ip = request.client.host if request.client else "unknown"
    if _freqtrade_rate_limited(ip):
        raise HTTPException(status_code=429, detail="Слишком много запросов к freqtrade. Подождите.")

    strategy = (data or {}).get("strategy", "momentum")
    start = str((data or {}).get("start", "20220101"))
    end = str((data or {}).get("end", "20260809"))
    if not re.fullmatch(r"\d{8}", start) or not re.fullmatch(r"\d{8}", end):
        raise HTTPException(status_code=400, detail="Неверный формат дат. Используйте YYYYMMDD.")
    repo = Path(__file__).resolve().parents[2]
    ft = repo / "external" / "freqtrade_test" / "venv" / "bin" / "freqtrade"
    if strategy == "impulse":
        cfg = repo / "external" / "freqtrade_test" / "config_impulse.json"
        strat_name = "Impulse1D"
    else:
        cfg = repo / "external" / "freqtrade_test" / "config.json"
        strat_name = "MomentumRotation"
    userdir = repo / "external" / "freqtrade_test" / "user_data"

    def _run_ft():
        return subprocess.run(
            [str(ft), "backtesting", "--config", str(cfg), "--strategy", strat_name,
             "--userdir", str(userdir), "--timerange", f"{start}-{end}",
             "--cache", "none"],
            capture_output=True, text=True, timeout=900,
        )

    try:
        async with _get_freqtrade_sem():
            p = await asyncio.to_thread(_run_ft)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Freqtrade не запустился: {e}")

    out = (p.stdout or "") + (p.stderr or "")

    def grab(pattern):
        m = re.search(pattern, out)
        return m.group(1).strip() if m else None

    row = None
    row_line = re.search(r"│\s*" + re.escape(strat_name) + r"\s*│.*", out)
    if row_line:
        cells = [c.strip() for c in row_line.group(0).split("│")]
        # cells: ['', NAME, trades, avg, usdt, pct, duration, 'wins 0 losses win%', 'dd_usdt USDT dd_pct', '']
        try:
            trades = cells[2]
            avg_profit = cells[3]
            total_usdt = cells[4]
            total_pct = cells[5]
            win_lose = cells[7].split()
            wins, losses, win_pct = win_lose[0], win_lose[2], win_lose[3]
            dd_usdt = cells[8].split()[0]
            row = {
                "trades": trades, "avg_profit_pct": avg_profit,
                "total_profit_usdt": total_usdt, "total_return_pct": total_pct,
                "wins": wins, "losses": losses, "win_rate_pct": win_pct,
                "dd_usdt": dd_usdt,
            }
        except (IndexError, ValueError):
            row = None

    longs, shorts = grab(r"Long / Short trades\s*│\s*(\d+)\s*/\s*(\d+)"), None
    ls = re.search(r"Long / Short trades\s*│\s*(\d+)\s*/\s*(\d+)", out)

    r = {
        "engine": "freqtrade",
        "strategy": strategy,
        "strategy_name": strat_name,
        "period": f"{start}-{end}",
        "cagr_pct": grab(r"CAGR %\s*│\s*([-\d.]+)%"),
        "total_return_pct": grab(r"Total profit %\s*│\s*([-\d.]+)%"),
        "max_drawdown_pct": grab(r"Max % of account underwater\s*│\s*([-\d.]+)%"),
        "longs": ls.group(1) if ls else None,
        "shorts": ls.group(2) if ls else None,
        "fee_note": "комиссия 0.05%/сторона (тейкер OKX), плечо 2x (momentum) / 3x (impulse)",
    }
    if row:
        r.update(row)
    return r


@app.get("/api/backtest/last")
async def backtest_last():
    """Return the most recently run backtest result (or null)."""
    raw = await db.get_setting("last_backtest")
    if not raw:
        return {"result": None}
    try:
        return {"result": json.loads(raw)}
    except Exception:
        return {"result": None}


# ── Trade ──

@app.post("/api/trade/order", dependencies=[Depends(require_admin)])
async def place_order(data: dict):
    """Manual orders disabled by default — only strategy signal path may open risk."""
    allow = os.getenv("ALLOW_MANUAL_ORDERS", "0").strip().lower() in ("1", "true", "yes", "on")
    if not allow:
        raise HTTPException(
            status_code=403,
            detail="Manual orders disabled. Opens only via strategy signals "
                   "(set ALLOW_MANUAL_ORDERS=1 to override).",
        )
    client = client_manager.get_client()
    if not client:
        raise HTTPException(status_code=400, detail="API not configured")

    result = await client.place_order(
        inst_id=data["instId"],
        side=data["side"],
        ord_type=data.get("ordType", "market"),
        sz=str(data["sz"]),
        td_mode=data.get("tdMode", "cash"),
        px=data.get("px"),
        pos_side=data.get("posSide"),
        reduce_only=data.get("reduceOnly", False),
        tgt_ccy=data.get("tgtCcy"),
    )
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result.get("message", ""))
    return {"message": "Order placed", "data": result.get("data")}


@app.get("/api/trade/orders")
async def get_orders(inst_type: str = "SPOT"):
    result = await _okx_call(lambda c: c.get_orders(inst_type))
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["message"])
    return {"orders": result.get("data", [])}


@app.get("/api/trade/log")
async def get_trade_log():
    return {"orders": trade_log[-100:]}


# ── Momentum Strategy ──

@app.get("/api/momentum/status")
async def momentum_status():
    if not rotation:
        status = {
            "running": False, "managed": False,
            "config": {"max_positions": 2, "risk_per_trade": 0, "tp1_pct": 0},
            "equity": 0, "open_positions": [], "total_signals": 0, "total_trades": 0,
            "recent_signals": [], "recent_trades": [], "description": STRATEGY_DESC,
        }
    else:
        status = rotation.get_status()
        status["total_pnl_internal"] = status.get("total_pnl")
    return await _apply_history_kpi(status, "Momentum")


@app.post("/api/momentum/start", dependencies=[Depends(require_admin)])
async def momentum_start(data: dict = None):
    """Start Rotation strategy (Dashboard calls this endpoint)."""
    global rotation
    if rotation and rotation._running:
        return {"message": "Rotation already running", **rotation.get_status()}
    d = data or {}
    config = RotationConfig(
        symbols=d.get("symbols", ["BTC", "ETH", "BNB", "XRP", "SOL", "DOGE", "ADA", "TRX", "AVAX", "LTC"]),
        capital=float(d.get("capital", 10000.0)),
        top_k=int(d.get("top_k", d.get("max_positions", 2))),
        roc_period=int(d.get("roc_period", 14)),
        ema_fast=int(d.get("ema_fast", 20)),
        ema_slow=int(d.get("ema_slow", 50)),
        atr_period=int(d.get("atr_period", 14)),
        breakeven_pct=float(d.get("breakeven_pct", 0.05)),
        adx_min=float(d.get("adx_min", d.get("adx_threshold", 29.0))),
        min_hold_days=int(d.get("min_hold_days", 11)),
        max_leverage=float(d.get("max_leverage", d.get("leverage", 2.0))),
        risk_per_trade=float(d.get("risk_per_trade", 0.14)),
        trail_atr_mult=float(d.get("trail_atr_mult", 0.2)),
        partial_tp_pct=float(d.get("partial_tp_pct", d.get("tp1_pct", 0.08))),
        partial_tp_ratio=float(d.get("partial_tp_ratio", d.get("tp1_frac", 0.5))),
        auto_execute=d.get("auto_execute", True),
        poll_interval_sec=int(d.get("poll_interval_sec", 300)),
    )
    rotation = RotationStrategy(config=config, client_manager=client_manager, db=db,
                                notifier=telegram)
    await rotation.start()
    return {"message": "Momentum Rotation started", **rotation.get_status()}


@app.post("/api/momentum/stop", dependencies=[Depends(require_admin)])
async def momentum_stop():
    """Stop Rotation strategy (Dashboard calls this endpoint)."""
    global rotation
    if not rotation:
        return {"message": "Bot not running"}
    await rotation.stop()
    return {"message": "Bot stopped"}


@app.post("/api/momentum/config", dependencies=[Depends(require_admin)])
async def momentum_update_config(data: dict = None):
    """Update running Rotation strategy config (hot-reload safe fields)."""
    global rotation
    if not rotation:
        return {"message": "Bot not running"}
    if not data:
        return {"message": "No config provided"}
    cfg = rotation.config
    if "symbols" in data:
        cfg.symbols = data["symbols"]
    # Map legacy UI names → RotationConfig fields
    aliases = {
        "max_positions": "top_k",
        "adx_threshold": "adx_min",
        "tp1_pct": "partial_tp_pct",
        "tp1_frac": "partial_tp_ratio",
        "leverage": "max_leverage",
    }
    for src, dst in aliases.items():
        if src in data and dst not in data:
            data[dst] = data[src]
    for key in (
        "capital", "top_k", "risk_per_trade", "auto_execute", "poll_interval_sec",
        "roc_period", "ema_fast", "ema_slow", "atr_period",
        "trail_atr_mult", "adx_min", "min_hold_days", "max_leverage",
        "breakeven_pct", "partial_tp_pct", "partial_tp_ratio",
        "rsi_period", "rsi_long_max", "rsi_short_min", "vol_mult", "corr_threshold",
    ):
        if key in data:
            setattr(cfg, key, data[key])
    return {"message": "Config updated", "config": asdict(cfg)}


@app.get("/api/momentum/trades")
async def momentum_trades(limit: int = 20):
    """Trade history — in-memory from running bot, fallback to OKX fills, then DB."""
    # 1. In-memory from running rotation bot
    if rotation and len(rotation._trade_log) > 0:
        trades = []
        for t in reversed(rotation._trade_log):
            if len(trades) >= limit:
                break
            is_open = t.get("reason") == "open" or t.get("pnl", 0) == 0
            trades.append({
                "time": t.get("time", ""),
                "symbol": t.get("symbol", ""),
                "side": t.get("side", ""),
                "pos_side": t.get("pos_side", ""),
                "size": t.get("size", 0),
                "pnl": t.get("pnl", 0),
                "entry": t.get("entry_price") or t.get("entry", 0),
                "entry_price": t.get("entry_price") or t.get("entry", 0),
                "exit_price": t.get("exit_price", 0),
                "stop": t.get("stop", 0),
                "reason": t.get("reason", ""),
                "ord_id": t.get("signal_id", ""),
                "inst_id": t.get("symbol", ""),
                "entry_time": t.get("time", ""),
                "exit_time": t.get("time", "") if not is_open else None,
                "bot": "Momentum",
            })
        return {"trades": trades}

    # 2. Fallback: fetch real fills from OKX exchange
    try:
        global _fills_cache_ts
        _fills_cache_ts = 0  # bypass cache
        raw_fills = await _fetch_okx_fills(limit=300)
        # Exclude fills that belong to the validation bot (its ordIds are
        # persisted with bot_id=validation_strategy). Otherwise its trades
        # leak into the Momentum window on the dashboard.
        try:
            val_ord_ids = {str(r["ord_id"]).strip() for r in await db._fetchall(
                "SELECT ord_id FROM trades WHERE bot_id = ? AND ord_id IS NOT NULL"
                " AND ord_id != ''"
                if not db._pg_mode else
                "SELECT ord_id FROM trades WHERE bot_id = $1 AND ord_id IS NOT NULL"
                " AND ord_id != ''",
                (VAL_BOT_ID,)) if r.get("ord_id")}
        except Exception:
            val_ord_ids = set()
        if val_ord_ids:
            raw_fills = [f for f in raw_fills if str(f.get("ordId", "")).strip() not in val_ord_ids]
        # Also drop fills whose client order id marks them as the validation bot
        # (CL_ORD_PREFIX="val"). New orders from the validator carry clOrdId=val<ts>.
        raw_fills = [f for f in raw_fills
                     if not str(f.get("clOrdId", "")).startswith("val")]
        paired = await _pair_fills(raw_fills)
        if paired:
            # Enrich with algo orders (TP/SL) for open positions
            try:
                algo_r = await _okx_call(lambda c: c.get_algo_orders(ord_type="conditional"))
                algo_map = {}
                if not algo_r.get("error") and algo_r.get("data"):
                    for o in algo_r["data"]:
                        iid = o.get("instId", "")
                        algo_map.setdefault(iid, []).append(o)
            except Exception:
                algo_map = {}

            trades = []
            # Build DB positions map for fallback tagging
            db_pos_map = {}
            try:
                db_rows = await db.get_all_positions()
                for row in db_rows:
                    db_pos_map[(row.get("inst_id", ""), row.get("side", ""))] = row.get("bot_id", "")
            except Exception:
                pass
            for t in reversed(paired):
                if len(trades) >= limit:
                    break
                entry = t.get("entry", 0) or t.get("entry_price", 0)
                exit_px = t.get("exit_price", 0)
                is_open = t.get("reason") == "open"
                inst_id = t.get("inst_id", "") or t.get("symbol", "")
                coin = inst_id.replace("-USDT-SWAP", "").replace("-USD-SWAP", "")
                trade = {
                    "time": t.get("time", ""),
                    "entry_time": t.get("entry_time", ""),
                    "exit_time": t.get("time", "") if not is_open else None,
                    "symbol": inst_id,
                    "inst_id": inst_id,
                    "coin": coin,
                    "side": t.get("side", ""),
                    "pos_side": t.get("pos_side", "long"),
                    "size": t.get("size", 0),
                    "pnl": t.get("pnl", 0),
                    "entry": entry,
                    "entry_price": entry,
                    "exit_price": exit_px,
                    "stop": 0,
                    "reason": t.get("reason", ""),
                    "ord_id": t.get("ord_id", ""),
                    "source": "okx",
                }
                trade["bot"] = _tag_trade_bot(trade, db_pos_map=db_pos_map)
                if is_open and inst_id in algo_map:
                    for ao in algo_map[inst_id]:
                        sl = ao.get("slTriggerPxPx") or ao.get("slTriggerPx")
                        tp = ao.get("tpTriggerPxPx") or ao.get("tpTriggerPx")
                        if sl:
                            trade["stop"] = float(sl)
                        if tp:
                            trade["tp"] = float(tp)
                trades.append(trade)
            print(f"[momentum/trades] OKX fallback: {len(trades)} trades from exchange", flush=True)
            return {"trades": trades}
    except Exception as e:
        import traceback
        print(f"[momentum/trades] OKX fallback error: {e}", flush=True)
        traceback.print_exc()

    # 3. Last resort: DB paired trades
    try:
        db_trades = await db.get_paired_trades(limit=limit, bot_ids=[ROT_BOT_ID, MOM_BOT_ID])
        result = []
        for t in db_trades:
            entry_side = t.get("entry_side", "buy")
            entry_px = t.get("entry_px", 0)
            exit_px = t.get("exit_px", 0)
            try:
                entry_px = float(entry_px) if entry_px else 0
            except (TypeError, ValueError):
                entry_px = 0
            try:
                exit_px = float(exit_px) if exit_px else 0
            except (TypeError, ValueError):
                exit_px = 0
            result.append({
                "time": t.get("exit_time") or t.get("entry_time", ""),
                "symbol": t.get("inst_id", ""),
                "side": "buy" if entry_side == "buy" else "sell",
                "pos_side": "long" if entry_side == "buy" else "short",
                "size": 0,
                "pnl": float(t.get("pnl", 0) or 0),
                "entry": entry_px,
                "entry_price": entry_px,
                "exit_price": exit_px,
                "stop": 0,
                "reason": "closed",
                "ord_id": str(t.get("signal_id", "")),
                "inst_id": t.get("inst_id", ""),
                "entry_time": t.get("entry_time", ""),
                "exit_time": t.get("exit_time", ""),
                "bot": _db_bot_name(t.get("bot_id", "")),
            })
        print(f"[momentum/trades] DB fallback returned {len(result)} trades", flush=True)
        return {"trades": result}
    except Exception as e:
        import traceback
        print(f"[momentum/trades] DB fallback error: {e}", flush=True)
        traceback.print_exc()
        return {"trades": []}


@app.get("/api/momentum/indicators")
async def momentum_indicators():
    """Return latest computed indicators per coin (debug)."""
    if not rotation:
        return {"indicators": {}}
    return {"indicators": rotation._latest_indicators}


@app.get("/api/momentum/chart-data")
async def momentum_chart_data():
    """Return trade markers + entry/stop/be/tp1 lines for chart overlay."""
    markers = []
    trade_lines = []

    def ts_or_none(t):
        if not t:
            return None
        try:
            return int(datetime.fromisoformat(t).timestamp())
        except Exception:
            return None

    if not rotation:
        return {"markers": markers, "trade_lines": trade_lines}

    cfg = rotation.config
    tp1_pct = getattr(cfg, "partial_tp_pct", 0.05)

    buys: dict[str, list] = {}
    for t in rotation._trade_log:
        side = t.get("side", "")
        symbol = t.get("symbol", "")
        time_str = t.get("time", "")
        if not time_str or not symbol:
            continue
        t_ts = ts_or_none(time_str)
        if not t_ts:
            continue

        if side in ("buy", "long", "open") or t.get("reason") == "open":
            markers.append({
                "time": t_ts, "side": "buy", "symbol": symbol,
                "entry": t.get("entry_price") or t.get("entry", 0),
                "stop": t.get("stop", 0),
            })
            buys.setdefault(symbol, []).append({
                "ts": t_ts,
                "entry": t.get("entry_price") or t.get("entry", 0),
                "stop": t.get("stop", 0),
            })
        elif side in ("sell", "short") or t.get("pnl", 0) != 0:
            markers.append({
                "time": t_ts, "side": "sell", "symbol": symbol,
                "exit_price": t.get("exit_price", 0),
                "entry_price": t.get("entry_price") or t.get("entry", 0),
                "pnl": t.get("pnl", 0), "reason": t.get("reason", ""),
            })

    for coin, pos in rotation._positions.items():
        entry = pos.entry_price
        if pos.side == "long":
            be = round(entry * (1 - 0.001), 2)
            tp1 = round(entry * (1 + tp1_pct), 2)
        else:
            be = round(entry * (1 + 0.001), 2)
            tp1 = round(entry * (1 - tp1_pct), 2)
        trade_lines.append({
            "symbol": pos.symbol, "inst_id": pos.inst_id,
            "entry": entry, "stop": pos.stop_price,
            "breakeven": be, "tp1": tp1,
            "peak": pos.peak_price,
            "stage": "trailing" if pos.breakeven else ("partial" if pos.partial_done else "initial"),
            "size": pos.size, "original_size": pos.size_original,
        })

    open_keys = {(pos.symbol, round(pos.entry_price, 2)) for pos in rotation._positions.values()}
    for symbol, ent in buys.items():
        for b in ent:
            entry = b["entry"]
            if (symbol, round(entry, 2)) in open_keys:
                continue
            trade_lines.append({
                "symbol": symbol,
                "entry": entry,
                "stop": b["stop"],
                "breakeven": round(entry * (1 - 0.001), 2),
                "tp1": round(entry * (1 + tp1_pct), 2),
                "stage": "closed",
            })

    return {"markers": markers, "trade_lines": trade_lines}


# ══════════════════════════════════════════════════════════════
# ROTATION STRATEGY ENDPOINTS
# ══════════════════════════════════════════════════════════════

@app.get("/api/rotation/status")
async def rotation_status():
    if not rotation:
        return {"running": False, "strategy": "momentum_rotation", "equity": 0,
                "open_positions": {}, "total_trades": 0, "total_pnl": 0,
                "config": None, "description": ""}
    return rotation.get_status()


@app.post("/api/rotation/start", dependencies=[Depends(require_admin)])
async def rotation_start(data: dict = None):
    global rotation
    if rotation and rotation._running:
        return {"message": "Rotation already running"}
    d = data or {}
    cfg = RotationConfig(
        symbols=d.get("symbols", ["BTC", "ETH", "BNB", "XRP", "SOL", "DOGE", "ADA", "TRX", "AVAX", "LTC"]),
        capital=d.get("capital", 10000.0),
        top_k=d.get("top_k", 2),
        roc_period=d.get("roc_period", 14),
        ema_fast=d.get("ema_fast", 20),
        ema_slow=d.get("ema_slow", 50),
        atr_period=d.get("atr_period", 14),
        breakeven_pct=d.get("breakeven_pct", 0.05),
        adx_min=d.get("adx_min", 29.0),
        min_hold_days=d.get("min_hold_days", 11),
        max_leverage=d.get("leverage", 2.0),
        risk_per_trade=d.get("risk_per_trade", 0.14),
        atr_stop_mult=d.get("atr_stop_mult", 2.7),
        trail_atr_mult=d.get("trail_atr_mult", 0.2),
        partial_tp_pct=d.get("partial_tp_pct", 0.08),
        poll_interval_sec=d.get("poll_interval_sec", 300),
        auto_execute=d.get("auto_execute", True),
    )
    rotation = RotationStrategy(config=cfg, client_manager=client_manager, db=db)
    await rotation.start()
    return {"message": "Rotation started", "config": asdict(cfg)}


@app.post("/api/rotation/stop", dependencies=[Depends(require_admin)])
async def rotation_stop():
    global rotation
    if not rotation:
        return {"message": "Rotation not running"}
    await rotation.stop()
    return {"message": "Rotation stopped"}


@app.post("/api/rotation/reset", dependencies=[Depends(require_admin)])
async def rotation_reset():
    """Reset all trades, signals, positions, PNL for rotation strategy."""
    global rotation
    # Stop if running
    if rotation and rotation._running:
        await rotation.stop()
    # Delete all data for rotation bot
    if db._pg_mode:
        for table in ["trades", "signals", "positions", "performance_metrics"]:
            try:
                await db._execute(f"DELETE FROM {table} WHERE bot_id = $1", (ROT_BOT_ID,))
            except Exception as e:
                print(f"[reset] Error clearing {table}: {e}", flush=True)
        try:
            await db._execute("DELETE FROM bots WHERE id = $1", (ROT_BOT_ID,))
        except Exception as e:
            print(f"[reset] Error clearing bots: {e}", flush=True)
    elif db._conn:
        for table in ["trades", "signals", "positions", "performance_metrics"]:
            try:
                await db._execute(f"DELETE FROM {table} WHERE bot_id = ?", (ROT_BOT_ID,))
            except Exception as e:
                print(f"[reset] Error clearing {table}: {e}", flush=True)
        try:
            await db._execute("DELETE FROM bots WHERE id = ?", (ROT_BOT_ID,))
        except Exception as e:
            print(f"[reset] Error clearing bots: {e}", flush=True)
    # Reset in-memory
    rotation = None
    return {"message": "Rotation reset complete - PNL = 0"}


@app.post("/api/db/reset-all", dependencies=[Depends(require_admin)])
async def db_reset_all():
    """Nuclear reset: clear ALL bot data (trades, signals, positions, metrics, bots)."""
    global rotation
    if rotation and rotation._running:
        await rotation.stop()
    rotation = None
    for table in ["trades", "signals", "positions", "performance_metrics", "bots"]:
        try:
            await db._execute(f"DELETE FROM {table}")
        except Exception as e:
            print(f"[reset-all] Error clearing {table}: {e}", flush=True)
    return {"message": "All data reset - clean slate"}


@app.get("/api/rotation/trades")
async def rotation_trades(limit: int = 50):
    trades = []
    if rotation and rotation._trade_log:
        trades = [dict(t) for t in rotation._trade_log[-limit:]]
    elif db:
        try:
            rows = await db.get_trades(bot_id=ROT_BOT_ID, limit=limit)
            for t in reversed(rows):
                trades.append({
                    "time": t.get("timestamp", ""),
                    "side": t.get("side", ""),
                    "symbol": t.get("inst_id", ""),
                    "inst_id": t.get("inst_id", ""),
                    "coin": (t.get("inst_id", "") or "").replace("-USDT-SWAP", "").replace("-USD-SWAP", ""),
                    "size": float(t.get("sz", 0) or 0),
                    "pnl": float(t.get("pnl", 0) or 0),
                    "entry_price": float(t.get("px", 0) or 0),
                    "reason": "closed",
                    "signal_id": t.get("signal_id", 0),
                })
        except Exception as e:
            print(f"[rotation/trades] DB fallback error: {e}", flush=True)
    for t in trades:
        t.setdefault("bot", "Momentum")
    return {"trades": trades}


@app.get("/api/rotation/indicators")
async def rotation_indicators():
    if not rotation:
        return {"indicators": {}}
    return {"indicators": rotation._latest_indicators}


@app.post("/api/rotation/config", dependencies=[Depends(require_admin)])
async def rotation_update_config(data: dict = None):
    global rotation
    if not rotation:
        return {"message": "Rotation not running"}
    if not data:
        return {"message": "No config provided"}
    cfg = rotation.config
    for key in ("symbols", "top_k", "roc_period", "ema_fast", "ema_slow",
                "atr_period", "atr_stop_mult", "trail_pct", "breakeven_pct",
                "adx_min", "min_hold_days", "max_pos_pct", "poll_interval_sec",
                "auto_execute", "capital"):
        if key in data:
            setattr(cfg, key, data[key])
    return {"message": "Config updated", "config": asdict(cfg)}


# ══════════════════════════════════════════════════════════════
# IMPULSE 1D STRATEGY ENDPOINTS
# ══════════════════════════════════════════════════════════════

@app.get("/api/impulse/status")
async def impulse_status():
    if not impulse:
        status = {
            "running": False, "managed": False,
            "strategy": IMPULSE_NAME, "version": IMPULSE_VERSION,
            "equity": 0, "capital": 0, "open_positions": [], "closed_trades": 0,
            "total_trades": 0, "total_pnl": 0, "win_rate": 0,
            "config": None, "description": IMPULSE_DESC,
        }
    else:
        status = impulse.get_status()
        status["total_pnl_internal"] = status.get("total_pnl")
    return await _apply_history_kpi(status, "Impulse 1D")


@app.post("/api/impulse/start", dependencies=[Depends(require_admin)])
async def impulse_start(data: dict = None):
    """Start Impulse 1D strategy."""
    global impulse
    if impulse and impulse._running:
        return {"message": "Impulse already running", **impulse.get_status()}
    d = data or {}
    cfg = ImpulseConfig(
        symbols=d.get("symbols", ["BTC", "ETH", "BNB", "XRP", "SOL", "DOGE", "ADA", "TRX", "AVAX", "LTC"]),
        capital=float(d.get("capital", 10000.0)),
        top_k=int(d.get("top_k", 4)),
        entry_roc=float(d.get("entry_roc", 4.0)),
        max_adds=int(d.get("max_adds", 2)),
        risk_per_trade=float(d.get("risk_per_trade", 0.10)),
        sl_atr_mult=float(d.get("sl_atr_mult", 5.0)),
        sl_atr_mult_short=float(d.get("sl_atr_mult_short", 5.0)),
        trail_atr_mult=float(d.get("trail_atr_mult", 8.0)),
        trail_atr_mult_short=float(d.get("trail_atr_mult_short", 8.0)),
        cooldown_bars=int(d.get("cooldown_bars", 5)),
        tp1_atr=float(d.get("tp1_atr", 2.0)),
        tp1_frac=float(d.get("tp1_frac", 0.3)),
        tp2_atr=float(d.get("tp2_atr", 6.0)),
        tp2_frac=float(d.get("tp2_frac", 0.3)),
        max_hold_bars=int(d.get("max_hold_bars", 30)),
        max_leverage=float(d.get("max_leverage", d.get("leverage", 3.0))),
        poll_interval_sec=int(d.get("poll_interval_sec", 300)),
        auto_execute=d.get("auto_execute", True),
    )
    impulse = ImpulseStrategy(config=cfg, client_manager=client_manager, db=db,
                              notifier=telegram)
    await impulse.start()
    return {"message": "Impulse 1D started", **impulse.get_status()}


@app.post("/api/impulse/stop", dependencies=[Depends(require_admin)])
async def impulse_stop():
    """Stop Impulse 1D strategy."""
    global impulse
    if not impulse:
        return {"message": "Impulse not running"}
    await impulse.stop()
    return {"message": "Impulse stopped"}


@app.post("/api/impulse/config", dependencies=[Depends(require_admin)])
async def impulse_update_config(data: dict = None):
    """Update running Impulse config (hot-reload safe fields)."""
    global impulse
    if not impulse:
        return {"message": "Impulse not running"}
    if not data:
        return {"message": "No config provided"}
    cfg = impulse.config
    for key in ("symbols", "capital", "top_k", "entry_roc", "max_adds",
                "risk_per_trade", "sl_atr_mult", "sl_atr_mult_short",
                "trail_atr_mult", "trail_atr_mult_short", "cooldown_bars",
                "tp1_atr", "tp1_frac", "tp2_atr", "tp2_frac", "max_hold_bars",
                "max_leverage", "poll_interval_sec", "auto_execute"):
        if key in data:
            setattr(cfg, key, data[key])
    return {"message": "Config updated", "config": asdict(cfg)}


@app.get("/api/impulse/trades")
async def impulse_trades(limit: int = 50):
    if not impulse:
        return {"trades": []}
    trades = [dict(t) for t in impulse._trade_log[-limit:]]
    for t in trades:
        t.setdefault("bot", "Impulse 1D")
    return {"trades": trades}


@app.get("/api/impulse/indicators")
async def impulse_indicators():
    if not impulse:
        return {"indicators": {}}
    return {"indicators": impulse._latest_indicators}


@app.post("/api/impulse/reset", dependencies=[Depends(require_admin)])
async def impulse_reset():
    """Reset all trades, signals, positions, PNL for the impulse strategy."""
    global impulse
    if impulse and impulse._running:
        await impulse.stop()
    if db._pg_mode:
        for table in ["trades", "signals", "positions", "performance_metrics"]:
            try:
                await db._execute(f"DELETE FROM {table} WHERE bot_id = $1", (IMP_BOT_ID,))
            except Exception as e:
                print(f"[impulse/reset] Error clearing {table}: {e}", flush=True)
        try:
            await db._execute("DELETE FROM bots WHERE id = $1", (IMP_BOT_ID,))
        except Exception as e:
            print(f"[impulse/reset] Error clearing bots: {e}", flush=True)
    elif db._conn:
        for table in ["trades", "signals", "positions", "performance_metrics"]:
            try:
                await db._execute(f"DELETE FROM {table} WHERE bot_id = ?", (IMP_BOT_ID,))
            except Exception as e:
                print(f"[impulse/reset] Error clearing {table}: {e}", flush=True)
        try:
            await db._execute("DELETE FROM bots WHERE id = ?", (IMP_BOT_ID,))
        except Exception as e:
            print(f"[impulse/reset] Error clearing bots: {e}", flush=True)
    impulse = None
    return {"message": "Impulse reset complete - PNL = 0"}


# ══════════════════════════════════════════════════════════════
# VALIDATION STRATEGY ENDPOINTS (проверка исполнения)
# ══════════════════════════════════════════════════════════════

@app.get("/api/validation/status", dependencies=[Depends(require_admin)])
async def validation_status():
    if not validation:
        return {"running": False, "strategy": "macd_donchian_validation",
                "equity": 0, "open_positions": [], "total_trades": 0,
                "total_pnl": 0, "config": {}}
    status = validation.get_status()
    internal = status.get("total_pnl")
    status["total_pnl_internal"] = internal
    # Prefer same History/per_bot source as /api/pnl so the card matches the
    # dashboard Total PnL breakdown (same pattern as momentum/impulse status).
    stats = (await _bot_history_stats()).get("MACD+Donchian Validation")
    if stats and stats.get("total_trades", 0) > 0:
        status.update(stats)
        status["total_pnl_source"] = "okx_history"
        if internal is not None and abs(float(internal or 0) - float(stats.get("total_pnl") or 0)) > 1.0:
            print(f"[validation/status] PnL mismatch internal={internal} history={stats.get('total_pnl')}", flush=True)
    else:
        status["total_pnl_source"] = "internal"
    return status


@app.post("/api/validation/start", dependencies=[Depends(require_admin)])
async def validation_start(data: dict = None):
    """Start the validation bot (MACD+Donchian)."""
    global validation
    if validation and validation._running:
        return {"message": "Validation already running", **validation.get_status()}
    d = data or {}
    cfg = make_validation_config(
        capital=float(d.get("capital", 300.0)),
        top_k=int(d.get("top_k", 4)),
        donchian_n=int(d.get("donchian_n", 15)),
        tp_pct=float(d.get("tp_pct", 0.08)),
        tp_ratio=float(d.get("tp_ratio", 0.3)),
        tp2_pct=float(d.get("tp2_pct", 0.10)),
        be_pct=float(d.get("be_pct", 0.015)),
        chandelier_atr=float(d.get("chandelier_atr", 4.0)),
        max_hold_days=int(d.get("max_hold_days", 3)),
        risk_per_trade=float(d.get("risk_per_trade", 0.14)),
        allocation_pct=float(d.get("allocation_pct", 0.15)),
        max_leverage=float(d.get("max_leverage", 1.0)),
        poll_interval_sec=int(d.get("poll_interval_sec", 300)),
        auto_execute=d.get("auto_execute", True),
    )
    validation = ValidationStrategy(config=cfg, client_manager=client_manager, db=db,
                                    notifier=telegram)
    await validation.start()
    return {"message": "Validation started", **validation.get_status()}


@app.post("/api/validation/stop", dependencies=[Depends(require_admin)])
async def validation_stop():
    global validation
    if not validation:
        return {"message": "Validation not running"}
    await validation.stop()
    return {"message": "Validation stopped"}


@app.post("/api/validation/reset", dependencies=[Depends(require_admin)])
async def validation_reset():
    """Reset all trades, signals, positions, PNL for the validation bot."""
    global validation
    if validation and validation._running:
        await validation.stop()
    if db._pg_mode:
        for table in ["trades", "signals", "positions", "performance_metrics"]:
            try:
                await db._execute(f"DELETE FROM {table} WHERE bot_id = $1", (VAL_BOT_ID,))
            except Exception as e:
                print(f"[validation/reset] Error clearing {table}: {e}", flush=True)
        try:
            await db._execute("DELETE FROM bots WHERE id = $1", (VAL_BOT_ID,))
        except Exception as e:
            print(f"[validation/reset] Error clearing bots: {e}", flush=True)
    elif db._conn:
        for table in ["trades", "signals", "positions", "performance_metrics"]:
            try:
                await db._execute(f"DELETE FROM {table} WHERE bot_id = ?", (VAL_BOT_ID,))
            except Exception as e:
                print(f"[validation/reset] Error clearing {table}: {e}", flush=True)
        try:
            await db._execute("DELETE FROM bots WHERE id = ?", (VAL_BOT_ID,))
        except Exception as e:
            print(f"[validation/reset] Error clearing bots: {e}", flush=True)
    validation = None
    return {"message": "Validation reset complete - PNL = 0"}


@app.get("/api/validation/trades", dependencies=[Depends(require_admin)])
async def validation_trades(limit: int = 50):
    trades = []
    if validation and validation._trade_log:
        trades = [dict(t) for t in validation._trade_log[-limit:]]
    elif db:
        # Fallback: restore from Postgres/SQLite after a restart (in-memory empty).
        try:
            rows = await db.get_trades(bot_id=VAL_BOT_ID, limit=limit)
            for t in reversed(rows):
                trades.append({
                    "time": t.get("timestamp", ""),
                    "side": t.get("side", ""),
                    "symbol": t.get("inst_id", ""),
                    "inst_id": t.get("inst_id", ""),
                    "coin": (t.get("inst_id", "") or "").replace("-USDT-SWAP", "").replace("-USD-SWAP", ""),
                    "size": float(t.get("sz", 0) or 0),
                    "pnl": float(t.get("pnl", 0) or 0),
                    "entry_price": float(t.get("px", 0) or 0),
                    "reason": "closed",
                    "signal_id": t.get("signal_id", 0),
                })
        except Exception as e:
            print(f"[validation/trades] DB fallback error: {e}", flush=True)
    for t in trades:
        t.setdefault("bot", "MACD+Donchian Validation")
    return {"trades": trades}


@app.get("/api/validation/indicators", dependencies=[Depends(require_admin)])
async def validation_indicators():
    if not validation:
        return {"indicators": {}}
    return {"indicators": validation._latest_indicators}


@app.post("/api/validation/config", dependencies=[Depends(require_admin)])
async def validation_update_config(data: dict = None):
    global validation
    if not validation:
        return {"message": "Validation not running"}
    if not data:
        return {"message": "No config provided"}
    cfg = validation.config
    for key in ("symbols", "top_k", "donchian_n", "macd_fast", "macd_slow",
                "macd_signal", "chandelier_atr", "hard_stop_atr",
                "tp_pct", "tp_ratio", "tp2_pct", "be_pct", "max_hold_days",
                "atr_period", "atr_stop_mult", "trail_atr_mult", "breakeven_pct",
                "partial_tp_pct", "partial_tp_ratio", "adx_min", "min_roc",
                "min_hold_days", "max_leverage", "risk_per_trade",
                "allocation_pct", "poll_interval_sec", "auto_execute",
                "capital", "roi_table"):
        if key in data:
            setattr(cfg, key, data[key])
    return {"message": "Config updated", "config": asdict(cfg)}


# ══════════════════════════════════════════════════════════════
# TELEGRAM NOTIFICATIONS
# ══════════════════════════════════════════════════════════════

@app.get("/api/telegram/status", dependencies=[Depends(require_admin)])
async def telegram_status():
    """Return Telegram notification config status (token masked)."""
    masked_token = (telegram.token[:10] + "…" + telegram.token[-4:]) if telegram.token else ""
    masked_chat = (telegram.chat_id[:2] + "…" + telegram.chat_id[-3:]) if telegram.chat_id else ""
    return {
        "configured": telegram.configured,
        "status": telegram.status,
        "chat_id": masked_chat,
        "chat_id_masked": masked_chat,
        "token_masked": masked_token,
    }


@app.post("/api/telegram/config", dependencies=[Depends(require_admin)])
async def telegram_config(data: dict = None):
    """Set/update Telegram bot token, chat id and signals channel at runtime."""
    d = data or {}
    telegram.configure(token=d.get("token", ""), chat_id=d.get("chat_id", ""),
                       channel_id=d.get("channel_id", ""))
    # Persist to DB so the config survives restarts/redeploys.
    try:
        if telegram.token:
            await db.set_setting("TELEGRAM_BOT_TOKEN", telegram.token)
        if telegram.chat_id:
            await db.set_setting("TELEGRAM_CHAT_ID", telegram.chat_id)
        if telegram.channel_id:
            await db.set_setting("TELEGRAM_CHANNEL_ID", telegram.channel_id)
    except Exception as e:
        print(f"[telegram/config] DB persist error: {e}", flush=True)
    # Make sure the paid-signals poller is running if a token appeared.
    global bot_poller
    if telegram.token and (bot_poller is None or not bot_poller._running):
        try:
            bot_poller = TelegramBotPoller(notifier=telegram, db=db)
            bot_poller.start()
            print("[telegram/config] poller started", flush=True)
        except Exception as e:
            print(f"[telegram/config] poller start error: {e}", flush=True)
    return await telegram_status()


@app.post("/api/telegram/test", dependencies=[Depends(require_admin)])
async def telegram_test(data: dict = None):
    """Send a test message to verify the Telegram connection."""
    d = data or {}
    token = d.get("token", "") or telegram.token
    chat_id = d.get("chat_id", "") or telegram.chat_id
    if not (token and chat_id):
        return {"ok": False, "message": "Telegram не настроен: задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID"}
    notifier = TelegramNotifier(token=token, chat_id=chat_id)
    ok = await notifier.send("✅ Уведомления о сделках настроены и работают!")
    return {
        "ok": ok,
        "message": "Сообщение отправлено" if ok
        else "Не удалось отправить. Проверьте token и chat_id (например, через @userinfobot).",
    }


@app.post("/api/telegram/simulate", dependencies=[Depends(require_admin)])
async def telegram_simulate(data: dict = None):
    """Send sample trade-signal messages to Telegram to preview the real format.

    No real order is placed — just the exact open / partial-TP / close messages
    (rotation bot) plus a pyramid add-on message (impulse bot), with sample data.
    """
    d = data or {}
    token = d.get("token", "") or telegram.token
    chat_id = d.get("chat_id", "") or telegram.chat_id
    if not (token and chat_id):
        return {"ok": False, "message": "Telegram не настроен: задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID"}
    notifier = TelegramNotifier(token=token, chat_id=chat_id)

    open_px = 67250.00
    msg_open = notifier.open_msg(
        coin="BTC", side="long", price=open_px, stop=round(open_px * 0.97, 2),
        size=0.03, leverage=3.0, bot_name="Momentum Rotation v6.2",
        signal_id=123,
    )
    msg_partial = notifier.partial_msg(
        coin="BTC", side="long", entry=open_px, exit_px=round(open_px * 1.05, 2),
        pnl=76.50, closed_sz=0.015, remaining_sz=0.015,
        bot_name="Momentum Rotation v6.2", signal_id=123,
    )
    msg_close = notifier.close_msg(
        coin="BTC", side="long", entry=open_px, exit_px=round(open_px * 1.09, 2),
        pnl=201.75, reason="trail_stop", bot_name="Momentum Rotation v6.2",
        signal_id=123,
    )
    msg_add = notifier.add_msg(
        coin="ETH", side="long", price=3450.00, size=0.4, total=1.2,
        bot_name="Impulse 1D v4", signal_id=124,
    )

    results = {}
    for name, text in (("open", msg_open), ("partial", msg_partial),
                       ("close", msg_close), ("add", msg_add)):
        ok = await notifier.send(text)
        results[name] = ok
        print(f"[telegram/simulate] {name}: sent={ok}", flush=True)

    ok_all = all(results.values())
    return {
        "ok": ok_all,
        "message": "Все 4 сигнала отправлены" if ok_all else f"Частичная отправка: {results}",
        "results": results,
    }


@app.post("/api/telegram/menu", dependencies=[Depends(require_admin)])
async def telegram_menu(request: Request, data: dict = None):
    """Set the bot's chat menu button to open the Mini App (/mini)."""
    if not (telegram.token and telegram.chat_id):
        return {"ok": False, "message": "Telegram не настроен: задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID"}
    d = data or {}
    url = (d.get("url") or "").strip()
    if not url:
        origin = str(request.base_url).rstrip("/")
        url = f"{origin}/mini"
    ok = await telegram.set_chat_menu_button(url)
    print(f"[telegram/menu] url={url} ok={ok}", flush=True)
    return {
        "ok": ok,
        "url": url,
        "message": "Кнопка меню установлена" if ok else "Ошибка установки кнопки меню",
    }


# ══════════════════════════════════════════════════════════════
# PAID SIGNAL SUBSCRIPTIONS (Telegram Stars)
# ══════════════════════════════════════════════════════════════

@app.get("/api/subs", dependencies=[Depends(require_admin)])
async def subs_list():
    """List all subscribers with status + revenue stats (admin)."""
    rows = await db.list_subscriptions()
    subscribers = []
    revenue = {"signals": 0, "pro": 0}
    active = 0
    for r in rows:
        active_until = r.get("active_until", "")
        is_active = _is_active(r)
        if is_active:
            active += 1
        plan = r.get("plan", "pro")
        # Every saved subscription row with a payment_id is a paid month.
        if r.get("payment_id"):
            revenue[plan if plan in revenue else "pro"] += PLANS_PRICE[plan if plan in PLANS_PRICE else "pro"]
        subscribers.append({
            "user_id": r.get("user_id"),
            "username": r.get("username") or "",
            "first_name": r.get("first_name") or "",
            "plan": plan,
            "status": "active" if is_active else "expired",
            "active_until": active_until,
            "last_payment": r.get("last_payment"),
            "payment_id": r.get("payment_id"),
        })
    # Registered mini-app users (may not have paid yet).
    users = []
    try:
        for u in await db.list_users():
            users.append({
                "telegram_id": u.get("telegram_id"),
                "username": u.get("username") or "",
                "first_name": u.get("first_name") or "",
                "plan": u.get("plan"),
                "active": _is_active(u),
                "active_until": u.get("active_until"),
                "creds_configured": bool(u.get("okx_key_enc")),
            })
    except Exception:
        pass
    return {
        "subscribers": subscribers,
        "users": users,
        "stats": {
            "total": len(subscribers),
            "active": active,
            "expired": len(subscribers) - active,
            "revenue_stars": sum(revenue.values()),
            "revenue_signals": revenue["signals"],
            "revenue_pro": revenue["pro"],
            "price_stars": PRO_PRICE_STARS,
            "pro_price_stars": PRO_PRICE_STARS,
        },
    }


@app.post("/api/subs/activate", dependencies=[Depends(require_admin)])
async def subs_activate(data: dict = None):
    """Manually grant/extend a subscription (e.g. cash payment or admin test)."""
    d = data or {}
    user_id = str(d.get("user_id", "")).strip()
    days = int(d.get("days", PRO_PLAN_DAYS))
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    from datetime import timedelta as _td
    from datetime import datetime as _dt, timezone as _tz
    now = _dt.now(_tz.utc)
    try:
        cur = await db.get_subscription(user_id)
        base = now
        if cur and cur.get("active_until"):
            try:
                base = _dt.strptime(cur["active_until"], "%Y-%m-%d %H:%M")
            except ValueError:
                base = now
    except Exception:
        base = now
    until = (base + _td(days=days)).strftime("%Y-%m-%d %H:%M")
    await db.save_subscription(
        user_id=user_id,
        username=d.get("username", ""),
        first_name=d.get("first_name", ""),
        active_until=until,
        payment_id=d.get("payment_id", "") or f"manual_{int(_time.time())}",
        plan="pro", status="active",
    )
    # Keep users.plan in sync so the mini-app gate grants access.
    try:
        await db.find_or_create_user(user_id, d.get("username", ""), d.get("first_name", ""))
        await db.update_user(
            user_id, plan="pro", username=d.get("username", ""),
            first_name=d.get("first_name", ""), active_until=until,
        )
    except Exception as e:
        logger.warning("subs/activate user sync error: %s", e)
    # Optionally notify the user directly in Telegram.
    if d.get("notify") and telegram.token and user_id.isdigit():
        try:
            await telegram._send_to(
                user_id,
                f"✅ Подписка активирована администратором до <b>{until}</b> (UTC).",
                "HTML",
            )
        except Exception:
            pass
    return {"message": "Subscription activated", "active_until": until}


@app.post("/api/subs/deactivate", dependencies=[Depends(require_admin)])
async def subs_deactivate(data: dict = None):
    """Immediately deactivate a user's subscription."""
    d = data or {}
    user_id = str(d.get("user_id", "")).strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")
    await db.delete_subscription(user_id)
    return {"message": "Subscription deactivated"}


@app.get("/api/subs/config", dependencies=[Depends(require_admin)])
async def subs_config():
    """Subscription product config + poller readiness (admin)."""
    return {
        "pro_price_stars": PRO_PRICE_STARS,
        "pro_plan_days": PRO_PLAN_DAYS,
        "signals_free": True,
        "bot_configured": bool(telegram.token),
        "poller_running": bool(bot_poller and bot_poller._running),
    }


@app.get("/api/chart/trades")
async def chart_trades(inst_id: str = "BTC-USDT-SWAP"):
    """Return real trade markers + TP/SL lines for a specific instrument.
    Closed trades: entry (green) + exit (red/green) markers.
    Open positions: entry marker (blue) + TP/SL price lines from algo orders."""
    try:
        raw_fills = await _fetch_okx_fills(limit=300, inst_id=inst_id)
    except Exception as e:
        print(f"[chart_trades] _fetch_okx_fills error: {e}", flush=True)
        return {"markers": [], "tp_sl_lines": [], "debug": {"error": str(e), "raw_fills": 0, "paired": 0}}

    paired = await _pair_fills(raw_fills)
    inst_paired = [t for t in paired if t.get("inst_id") == inst_id]
    closed_count = sum(1 for t in inst_paired if t.get("reason") == "closed")
    open_count = sum(1 for t in inst_paired if t.get("reason") == "open")
    print(f"[chart_trades] raw={len(raw_fills)} paired={len(paired)} inst={inst_id} closed={closed_count} open={open_count}", flush=True)

    def _to_ts(time_str):
        if not time_str:
            return None
        try:
            return int(datetime.fromisoformat(time_str).timestamp())
        except (ValueError, OSError, TypeError):
            return None

    markers = []
    for t in inst_paired:
        if t.get("reason") == "closed":
            entry_ts = _to_ts(t.get("entry_time"))
            entry_px = t.get("entry", 0)
            if entry_ts and entry_px and entry_px > 0:
                pos_side = t.get("pos_side", "long")
                markers.append({
                    "time": entry_ts,
                    "position": "belowBar" if pos_side == "long" else "aboveBar",
                    "color": "#00ff88",
                    "shape": "arrowUp" if pos_side == "long" else "arrowDown",
                    "text": f"IN {entry_px:.2f}",
                })
            close_ts = _to_ts(t.get("time"))
            exit_px = t.get("exit_price", 0)
            if close_ts and exit_px and exit_px > 0:
                pnl = t.get("pnl", 0) or 0
                pos_side = t.get("pos_side", "long")
                markers.append({
                    "time": close_ts,
                    "position": "aboveBar" if pos_side == "long" else "belowBar",
                    "color": "#00ff88" if pnl >= 0 else "#ff4757",
                    "shape": "arrowDown" if pos_side == "long" else "arrowUp",
                    "text": f"{pnl:+.2f}",
                })
        else:
            open_ts = _to_ts(t.get("entry_time") or t.get("time"))
            entry_px = t.get("entry", 0)
            if open_ts and entry_px and entry_px > 0:
                pos_side = t.get("pos_side", "long")
                markers.append({
                    "time": open_ts,
                    "position": "belowBar" if pos_side == "long" else "aboveBar",
                    "color": "#4a9eff",
                    "shape": "arrowUp" if pos_side == "long" else "arrowDown",
                    "text": f"OPEN {entry_px:.2f}",
                })

    # Fetch TP/SL from algo orders for open positions
    tp_sl_lines = []
    try:
        algo_r = await _okx_call(lambda c: c.get_algo_orders(ord_type="conditional"))
        if not algo_r.get("error") and algo_r.get("data"):
            for order in algo_r["data"]:
                if order.get("instId") != inst_id:
                    continue
                # OKX algo order fields: tpTriggerPxPx, slTriggerPxPx (or tpTriggerPx, slTriggerPx)
                tp_price = order.get("tpTriggerPxPx") or order.get("tpTriggerPx")
                sl_price = order.get("slTriggerPxPx") or order.get("slTriggerPx")
                pos_side = order.get("posSide", "net")
                sz = float(order.get("sz", 0) or 0)
                if tp_price and float(tp_price) > 0:
                    tp_sl_lines.append({
                        "price": float(tp_price),
                        "type": "tp",
                        "pos_side": pos_side,
                        "size": sz,
                        "label": f"TP {float(tp_price):.2f}",
                    })
                if sl_price and float(sl_price) > 0:
                    tp_sl_lines.append({
                        "price": float(sl_price),
                        "type": "sl",
                        "pos_side": pos_side,
                        "size": sz,
                        "label": f"SL {float(sl_price):.2f}",
                    })
        print(f"[chart_trades] algo orders for {inst_id}: {len(tp_sl_lines)} TP/SL lines, algo_error={algo_r.get('error')}, algo_msg={algo_r.get('message', '')}", flush=True)
    except Exception as e:
        print(f"[chart_trades] algo orders error: {e}", flush=True)

    # Log marker creation details
    for t in inst_paired:
        entry_ts = _to_ts(t.get("entry_time"))
        close_ts = _to_ts(t.get("time"))
        entry_px = t.get("entry", 0)
        exit_px = t.get("exit_price", 0)
        print(f"[chart_trades] trade: reason={t.get('reason')} entry_ts={entry_ts} close_ts={close_ts} entry={entry_px} exit={exit_px}", flush=True)

    markers.sort(key=lambda m: m["time"])
    return {
        "markers": markers,
        "tp_sl_lines": tp_sl_lines,
        "debug": {
            "raw_fills": len(raw_fills),
            "paired": len(paired),
            "matched": len(inst_paired),
            "closed": closed_count,
            "open": open_count,
            "inst_ids": list(set(t.get("inst_id", "") for t in paired)),
            "client_ok": client_manager.get_client() is not None,
            "demo": _env_demo,
            "okx_errors": _fills_errors,
            "sample": [{"entry_time": t.get("entry_time", ""), "time": t.get("time", ""), "entry": t.get("entry", 0), "exit_price": t.get("exit_price", 0), "pnl": t.get("pnl", 0), "reason": t.get("reason", ""), "pos_side": t.get("pos_side", "")} for t in inst_paired[:3]],
        }
    }


SWAP_INSTRUMENTS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "BNB-USDT-SWAP", "SOL-USDT-SWAP"]

_fills_cache: list[dict] = []
_fills_cache_ts: float = 0
_fills_cache_limit: int = 0
_FILLS_TTL = 30  # seconds


_fills_errors: list[str] = []


async def _fetch_okx_fills(limit: int = 100, inst_id: str = None) -> list[dict]:
    """Fetch fills from OKX. If inst_id given, fetch only for that instrument (up to 300).
    Otherwise fetch all SWAP fills (up to 300 total with pagination)."""
    global _fills_cache, _fills_cache_ts, _fills_cache_limit, _fills_errors
    # Cache key includes inst_id
    cache_key = inst_id or "__all__"
    now = _time.time()
    if (_fills_cache and (now - _fills_cache_ts) < _FILLS_TTL
            and _fills_cache_limit >= limit and getattr(_fetch_okx_fills, '_cache_key', '') == cache_key):
        print(f"[_fetch_okx_fills] cache hit, {len(_fills_cache)} fills (key={cache_key})", flush=True)
        return _fills_cache

    all_fills = []
    errors = []
    effective_limit = min(limit, 1000)
    pages = max(1, (effective_limit + 99) // 100)

    if inst_id:
        # Per-instrument fetch with pagination — up to 300 fills for this instrument
        after_ts = ""
        for page in range(pages):
            params = {"inst_type": "SWAP", "instId": inst_id, "limit": 100}
            if after_ts:
                params["after"] = after_ts
            r1 = await _okx_call(lambda c, p=params: c.get_fills_history(**p))
            data = r1.get("data", [])
            print(f"[_fetch_okx_fills] {inst_id} page {page+1}: error={r1.get('error')}, data_len={len(data)}", flush=True)
            if r1.get("error"):
                errors.append(f"{inst_id} p{page+1}: {r1.get('message', '')}")
                break
            if not data:
                break
            all_fills.extend(data)
            if len(data) < 100:
                break
            after_ts = data[-1].get("ts", "")
    else:
        # All SWAP instruments with pagination
        after_ts = ""
        for page in range(pages):
            params = {"inst_type": "SWAP", "limit": 100}
            if after_ts:
                params["after"] = after_ts
            r1 = await _okx_call(lambda c, p=params: c.get_fills_history(**p))
            data = r1.get("data", [])
            print(f"[_fetch_okx_fills] all-SWAP page {page+1}: error={r1.get('error')}, data_len={len(data)}", flush=True)
            if r1.get("error"):
                errors.append(f"all-SWAP p{page+1}: {r1.get('message', '')}")
                break
            if not data:
                break
            all_fills.extend(data)
            if len(data) < 100:
                break
            after_ts = data[-1].get("ts", "")

        # Fallback to regular fills if no results
        if not all_fills:
            r2 = await _okx_call(lambda c: c.get_fills(limit=100))
            print(f"[_fetch_okx_fills] fills (fallback): error={r2.get('error')}, data_len={len(r2.get('data', []))}", flush=True)
            if r2.get("error"):
                errors.append(f"fills: {r2.get('message', '')}")
            if not r2.get("error") and r2.get("data"):
                all_fills.extend(r2["data"])

    # Sort by timestamp ascending (oldest first — needed for _pair_fills)
    all_fills.sort(key=lambda f: f.get("ts", "0"))
    _fills_cache = all_fills
    _fills_cache_ts = now
    _fills_cache_limit = effective_limit
    _fills_errors = errors
    _fetch_okx_fills._cache_key = cache_key
    print(f"[_fetch_okx_fills] total: {len(all_fills)} fills for {cache_key}, errors={errors}", flush=True)
    return all_fills


def _ms_to_iso(ts_ms: str) -> str:
    """Convert OKX millisecond timestamp to UTC ISO string (timezone-aware, so
    the frontend renders it in the correct local/Moscow time)."""
    if not ts_ms:
        return ""
    try:
        return datetime.fromtimestamp(
            int(ts_ms) / 1000, tz=timezone.utc).isoformat()
    except (ValueError, OSError, TypeError):
        return ts_ms


def _parse_fill_pnl(f: dict):
    """Parse pnl from OKX fill. OKX uses 'fillPnl' field. Returns float or None if unknown."""
    raw = f.get("fillPnl") or f.get("pnl")  # fillPnl for v5, pnl as fallback
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (ValueError, TypeError):
        return None


def _parse_fill_sz(f: dict) -> float:
    """Parse fill size from OKX fill. OKX uses 'fillSz' field."""
    return float(f.get("fillSz", 0) or 0)


def _is_close_fill(f: dict, direction: str = None) -> bool:
    """Determine if a fill is closing a position.
    Priority: 1) subType (3/4=open, 5/6=close — present on every trade fill),
    2) pnl field, 3) posSide+side, 4) direction tracking.

    subType is the reliable signal for demo accounts where fillPnl/posSide may
    be missing and direction tracking breaks under rapid open/close churn."""
    sub = str(f.get("subType", "") or "")
    if sub in ("5", "6"):
        return True
    if sub in ("1", "2", "3", "4"):
        return False

    pnl = _parse_fill_pnl(f)
    if pnl is not None and pnl != 0:
        return True
    if pnl == 0:
        return False

    pos_side = f.get("posSide", "")
    side = f.get("side", "")

    if pos_side and pos_side != "net":
        if (pos_side == "long" and side == "sell") or \
           (pos_side == "short" and side == "buy"):
            return True
        return False

    if direction:
        if (direction == "long" and side == "sell") or \
           (direction == "short" and side == "buy"):
            return True

    return False


def _fill_to_trade(f: dict, is_close: bool = False) -> dict:
    """Convert a single OKX fill dict to our trade format for frontend."""
    side = f.get("side", "")
    pnl = _parse_fill_pnl(f)
    if pnl is None:
        pnl = 0.0
    px = float(f.get("fillPx", 0) or 0)
    sz = _parse_fill_sz(f)
    inst_id = f.get("instId", "")
    pos_side = f.get("posSide", "")

    if is_close:
        if not pos_side or pos_side == "net":
            pos_side = "long" if side == "sell" else "short"
        reason = "closed"
    else:
        if not pos_side or pos_side == "net":
            pos_side = "long" if side == "buy" else "short"
        reason = "open"

    trade = {
        "time": _ms_to_iso(f.get("ts", "")),
        "side": side,
        "symbol": inst_id,
        "size": sz,
        "pnl": pnl,
        "ord_id": f.get("ordId", ""),
        "fee": f.get("fee", "0"),
        "entry": px if not is_close else 0,
        "entry_price": px if not is_close else 0,
        "exit_price": px if is_close else 0,
        "reason": reason,
        "pos_side": pos_side,
        "inst_id": inst_id,
        "source": "okx",
    }
    return trade


async def _pair_fills(fills: list[dict]) -> list[dict]:
    """Pair OKX fills into entry+close trades using sequential direction tracking.
    Works with or without pnl field (demo accounts may return pnl=null).
    Uses posSide+side for entry/close detection, with direction tracking as fallback.
    Calculates PnL from entry/exit prices when OKX pnl is not available."""
    # Group by instrument
    by_inst: dict[str, list] = {}
    for f in fills:
        inst = f.get("instId", "")
        by_inst.setdefault(inst, []).append(f)

    paired = []
    for inst_id, inst_fills in by_inst.items():
        inst_fills.sort(key=lambda x: x.get("ts", "0"))

        # Sequential state tracking
        direction = None  # "long" or "short" or None (flat)
        entry_size = 0.0
        entry_cost = 0.0  # total cost for weighted average price
        entry_time = ""
        entry_ord_id = ""
        entry_fees = 0.0
        entry_side = ""

        for f in inst_fills:
            fill_sz = _parse_fill_sz(f)
            fill_px = float(f.get("fillPx", 0) or 0)
            fill_side = f.get("side", "")
            fill_ts = f.get("ts", "")
            fill_pnl = _parse_fill_pnl(f)
            fill_fee = float(f.get("fee", 0) or 0)

            is_close = _is_close_fill(f, direction)

            if not is_close:
                # Entry fill — accumulate into current position
                if direction is None:
                    direction = "long" if fill_side == "buy" else "short"
                    entry_side = fill_side
                entry_size += fill_sz
                entry_cost += fill_sz * fill_px
                entry_fees += fill_fee
                if not entry_time:
                    entry_time = fill_ts
                if not entry_ord_id:
                    entry_ord_id = f.get("ordId", "")
            else:
                # Close fill
                if entry_size > 0:
                    close_size = min(fill_sz, entry_size)
                    avg_entry = entry_cost / entry_size if entry_size > 0 else 0

                    # Calculate PnL from prices if OKX doesn't provide it
                    if fill_pnl is not None and fill_pnl != 0:
                        calc_pnl = fill_pnl
                    elif direction == "long":
                        calc_pnl = (fill_px - avg_entry) * close_size
                    else:
                        calc_pnl = (avg_entry - fill_px) * close_size

                    paired.append({
                        "time": _ms_to_iso(fill_ts),
                        "entry_time": _ms_to_iso(entry_time),
                        "side": fill_side,
                        "symbol": inst_id,
                        "size": close_size,
                        "pnl": round(calc_pnl, 4),
                        "ord_id": f.get("ordId", ""),
                        "fee": str(fill_fee),
                        "entry": round(avg_entry, 4),
                        "entry_price": round(avg_entry, 4),
                        "exit_price": round(fill_px, 4),
                        "entry_ord_id": entry_ord_id,
                        "reason": "closed",
                        "pos_side": direction,
                        "inst_id": inst_id,
                        "source": "okx",
                    })

                    # Reduce remaining entry by closed amount
                    entry_size -= close_size
                    entry_cost = avg_entry * entry_size  # remaining cost at same avg price
                    if entry_size <= 1e-10:
                        direction = None
                        entry_size = 0.0
                        entry_cost = 0.0
                        entry_time = ""
                        entry_ord_id = ""
                        entry_fees = 0.0
                        entry_side = ""
                else:
                    # Close without matching entry — standalone
                    pos_out = f.get("posSide", "")
                    if not pos_out or pos_out == "net":
                        pos_out = "long" if fill_side == "sell" else "short"
                    paired.append({
                        "time": _ms_to_iso(fill_ts),
                        "entry_time": "",
                        "side": fill_side,
                        "symbol": inst_id,
                        "size": fill_sz,
                        "pnl": fill_pnl or 0.0,
                        "ord_id": f.get("ordId", ""),
                        "fee": str(fill_fee),
                        "entry": 0,
                        "entry_price": 0,
                        "exit_price": round(fill_px, 4),
                        "entry_ord_id": entry_ord_id,
                        "reason": "closed",
                        "pos_side": pos_out,
                        "inst_id": inst_id,
                        "source": "okx",
                    })

        # Remaining unpaired entries (currently open positions)
        if entry_size > 1e-10:
            avg_entry = entry_cost / entry_size if entry_size > 0 else 0
            paired.append({
                "time": _ms_to_iso(entry_time),
                "entry_time": _ms_to_iso(entry_time),
                "side": entry_side or ("buy" if direction == "long" else "sell"),
                "symbol": inst_id,
                "size": round(entry_size, 4),
                "pnl": 0.0,
                "ord_id": entry_ord_id,
                "fee": str(round(entry_fees, 4)),
                "entry": round(avg_entry, 4),
                "entry_price": round(avg_entry, 4),
                "exit_price": 0,
                "reason": "open",
                "pos_side": direction or "long",
                "inst_id": inst_id,
                "source": "okx",
            })

    return paired


def _pair_bills(bills: list) -> list:
    """Pair OKX trade bills into entry+close rows using subType and exact pnl.

    Authoritative source: unlike fills, every trade bill carries subType
    (3/4 = open, 5/6 = close) plus exact pnl/fee/px/sz, so it stays correct on
    demo accounts where fills lack fillPnl/posSide and sequential tracking breaks
    under rapid open/close churn. Close bills sharing one ordId (partial fills)
    are aggregated into a single row."""
    by_inst: dict[str, list] = {}
    for b in bills:
        by_inst.setdefault(b.get("instId", ""), []).append(b)

    rows = []

    def _flush_close(pending: dict, cur: dict):
        """Emit the aggregated close row and reduce the open position."""
        avg_entry = 0.0
        pos_side = "short" if pending["side"] == "buy" else "long"
        entry_ord = ""
        if cur is not None and cur["size"] > 0:
            avg_entry = cur["cost"] / cur["size"]
            pos_side = cur["pos_side"]
            entry_ord = str(cur.get("ord_id", "") or "").strip()
            close_sz = min(pending["size"], cur["size"])
            cur["size"] -= close_sz
            cur["cost"] = avg_entry * cur["size"] if cur["size"] > 0 else 0.0
            cur["fee"] += pending["fee"]
        rows.append({
            "time": _ms_to_iso(pending["time"]),
            "entry_time": _ms_to_iso(pending["entry_time"]),
            "side": pending["side"],
            "symbol": pending["inst_id"], "inst_id": pending["inst_id"],
            "size": round(pending["size"], 4),
            "pnl": round(pending["pnl"], 4),
            "ord_id": pending["ord_id"], "fee": round(pending["fee"], 4),
            "entry": round(avg_entry, 4), "entry_price": round(avg_entry, 4),
            "exit_price": round(pending["px"], 4),
            "entry_ord_id": entry_ord,
            "reason": "closed", "pos_side": pos_side, "source": "okx_bills",
        })

    for inst_id, inst_bills in by_inst.items():
        try:
            inst_bills.sort(key=lambda x: str(x.get("ts", "0")))
        except Exception:
            inst_bills.sort(key=lambda x: str(x.get("ts", "0")))
        cur = None  # accumulated open: {size,cost,time,ord_id,side,pos_side,fee}
        pending = None  # aggregated close for the current ordId
        for b in inst_bills:
            try:
                sub = str(b.get("subType", "") or "")
                try:
                    sz = float(b.get("sz", 0) or 0)
                    px = float(b.get("px", 0) or 0)
                except (TypeError, ValueError):
                    continue
                try:
                    pnl = float(b.get("pnl", 0) or 0)
                except (TypeError, ValueError):
                    pnl = 0.0
                try:
                    fee = abs(float(b.get("fee", 0) or 0))
                except (TypeError, ValueError):
                    fee = 0.0
                ts = str(b.get("ts", "") or "")
                ord_id = str(b.get("ordId", "") or "").strip()
                side = b.get("side", "")

                if sub in ("3", "4"):
                    # Entry (open). Flush any pending close, then accumulate.
                    if pending is not None:
                        _flush_close(pending, cur)
                        if cur is not None and cur["size"] <= 1e-9:
                            cur = None
                        pending = None
                    if cur is None:
                        cur = {
                            "size": 0.0, "cost": 0.0, "time": ts, "ord_id": ord_id,
                            "side": side,
                            "pos_side": "short" if side == "sell" else "long",
                            "fee": 0.0,
                        }
                    cur["size"] += sz
                    cur["cost"] += sz * px
                    cur["fee"] += fee
                    cur["ord_id"] = ord_id  # always update to the most recent open
                    cur["time"] = ts
                    cur["side"] = side
                    continue

                if sub in ("5", "6"):
                    # Close (realized PnL).
                    if pending is None:
                        pending = {
                            "size": 0.0, "pnl": 0.0, "fee": 0.0, "ord_id": ord_id,
                            "time": ts, "px": px, "side": side,
                            "entry_time": cur["time"] if cur else ts,
                            "inst_id": inst_id,
                        }
                    elif pending["ord_id"] == ord_id:
                        pending["px"] = px
                        pending["time"] = ts
                    else:
                        _flush_close(pending, cur)
                        if cur is not None and cur["size"] <= 1e-9:
                            cur = None
                        pending = {
                            "size": 0.0, "pnl": 0.0, "fee": 0.0, "ord_id": ord_id,
                            "time": ts, "px": px, "side": side,
                            "entry_time": cur["time"] if cur else ts,
                            "inst_id": inst_id,
                        }
                    pending["size"] += sz
                    pending["pnl"] += pnl
                    pending["fee"] += fee
                    continue
            except Exception as e:
                print(f"[pair_bills] skip bad bill: {e} ({b.get('billId', '')})", flush=True)
                continue

        if pending is not None:
            _flush_close(pending, cur)
        if cur is not None and cur["size"] > 0:
            avg_entry = cur["cost"] / cur["size"] if cur["size"] > 0 else 0.0
            rows.append({
                "time": _ms_to_iso(cur["time"]),
                "entry_time": _ms_to_iso(cur["time"]),
                "side": cur["side"],
                "symbol": inst_id, "inst_id": inst_id,
                "size": round(cur["size"], 4), "pnl": None, "ord_id": cur["ord_id"],
                "fee": round(cur["fee"], 4),
                "entry": round(avg_entry, 4), "entry_price": round(avg_entry, 4),
                "exit_price": None,
                "reason": "open", "pos_side": cur["pos_side"], "source": "okx_bills",
            })

    rows.sort(key=lambda t: (t.get("time") or ""), reverse=True)
    return rows


# ── PnL from OKX bills ──

async def _get_okx_realized_pnl() -> dict:
    """Calculate realized PnL from paired trades. Works even when OKX pnl field is null (demo accounts)."""
    now_ms = int(_time.time() * 1000)
    periods = {"1d": 86400_000, "7d": 604800_000, "30d": 2592000_000}
    pnl = {"1d": 0.0, "7d": 0.0, "30d": 0.0}

    all_fills = await _fetch_okx_fills(limit=100)
    paired = await _pair_fills(all_fills)

    for t in paired:
        if t.get("reason") != "closed":
            continue
        try:
            trade_pnl = float(t.get("pnl", 0) or 0)
        except (ValueError, TypeError):
            continue
        time_str = t.get("time", "")
        if not time_str:
            continue
        try:
            trade_ts = int(datetime.fromisoformat(time_str).timestamp() * 1000)
        except (ValueError, OSError, TypeError):
            continue
        for key, window in periods.items():
            if trade_ts >= now_ms - window:
                pnl[key] += trade_pnl

    return pnl


_bills_cache: list = []
_bills_cache_ts: float = 0
_BILLS_TTL = 60  # seconds — avoid OKX 429 from dashboard polls hitting bills every 10s


async def _fetch_all_trade_bills(limit_per_page: int = 100) -> list:
    """Fetch OKX account bills of trade type (type=2) for the whole available
    history: recent 7 days via /account/bills, older up to 3 months via
    /account/bills-archive, paginated backwards by billId.

    Result is cached for _BILLS_TTL seconds: get_paired_trades is polled by the
    dashboard every ~10s and each full fetch pages up to 20 requests, which
    trips OKX rate limits (429) and silently kills the authoritative bills."""
    global _bills_cache, _bills_cache_ts
    now = _time.time()
    if _bills_cache and (now - _bills_cache_ts) < _BILLS_TTL:
        return _bills_cache
    bills: list = []
    seen: set = set()
    try:
        for endpoint, fn in (
            ("bills", lambda c, **kw: c.get_bills(inst_type="SWAP", type="2", **kw)),
            ("archive", lambda c, **kw: c.get_bills_archive(inst_type="SWAP", type="2", **kw)),
        ):
            after = ""
            for _ in range(10):
                kw = {"limit": limit_per_page}
                if after:
                    kw["after"] = after
                resp = None
                for attempt in range(3):
                    resp = await _okx_call(lambda c, e=fn, k=kw: e(c, **k))
                    if not resp.get("error"):
                        break
                    msg = str(resp.get("message", ""))
                    if "429" in msg or "Too Many Requests" in msg:
                        await asyncio.sleep(1.0 + attempt)
                        continue
                    break
                if resp.get("error"):
                    print(f"[bills] {endpoint} error: {resp.get('message', '')}", flush=True)
                    break
                data = resp.get("data", [])
                if not data:
                    break
                added = 0
                for b in data:
                    bid = b.get("billId", "")
                    if bid in seen:
                        continue
                    seen.add(bid)
                    # Trade fills only (type=2). Some demo fills carry pnl=0 for
                    # the opening fill and the real pnl on the closing fill.
                    if str(b.get("type", "")) == "2":
                        bills.append(b)
                        added += 1
                after = data[-1].get("billId", "")
                if added == 0 or len(data) < limit_per_page:
                    break
    except Exception as e:
        import traceback
        print(f"[bills] fetch error: {e}", flush=True)
        traceback.print_exc()
    if bills:
        _bills_cache = bills
        _bills_cache_ts = _time.time()
    return bills



@app.post("/api/pnl/rebuild-strategy", dependencies=[Depends(require_admin)])
async def pnl_rebuild_strategy(data: dict = None):
    """Reassign SOL off AI -> Impulse and rebuild each strategy PnL from DB trades only."""
    data = data or {}
    symbol = str(data.get("symbol") or "SOL").upper()
    out = {"steps": []}
    # 1) Move SOL trades from AI to Impulse if any remain
    try:
        inst = f"{symbol}-USDT-SWAP"
        st = await db.reassign_trades_instrument(AI_BOT_ID, IMP_BOT_ID, inst)
        out["steps"].append({"reassign": st})
        from app.services.position_claim import release_open
        await release_open(db, AI_BOT_ID, inst, "long")
        await release_open(db, AI_BOT_ID, inst, "short")
    except Exception as e:
        out["steps"].append({"reassign_err": str(e)})
    # 2) Summaries
    bots = {
        "Momentum": ROT_BOT_ID,
        "Impulse 1D": IMP_BOT_ID,
        "MACD+Donchian Validation": VAL_BOT_ID,
        "AI Discretionary 1H": AI_BOT_ID,
    }
    summaries = {}
    for label, bid in bots.items():
        try:
            summaries[label] = await db.get_trades_summary(bid)
        except Exception as e:
            summaries[label] = {"error": str(e)}
    out["summaries"] = summaries
    # 3) Refresh live AI bot memory
    global ai_bot
    if ai_bot and hasattr(ai_bot, "correct_misattributed"):
        try:
            # clear one-shot flag to allow rebuild path
            try:
                await db.set_setting(f"ai_misattr_fixed:{AI_BOT_ID}:{symbol}", "")
            except Exception:
                pass
            out["ai_correct"] = await ai_bot.correct_misattributed(symbol, IMP_BOT_ID)
        except Exception as e:
            out["ai_correct_err"] = str(e)
    return out


@app.post("/api/admin/reset-trading-stats", dependencies=[Depends(require_admin)])
async def admin_reset_trading_stats(data: dict = None):
    """Wipe strategy trading history and start PnL counting from now (UTC).

    Does not close exchange positions and does not delete position claims. Does not change strategy code/params.
    Sets pnl_epoch so OKX history before this moment is ignored in cards.
    """
    from datetime import datetime as dt, timezone as tz
    data = data or {}
    # start of today UTC unless explicit epoch provided
    if data.get("epoch"):
        epoch = str(data["epoch"])
    else:
        epoch = dt.now(tz.utc).strftime("%Y-%m-%dT00:00:00")

    bot_ids = [
        ROT_BOT_ID, MOM_BOT_ID, IMP_BOT_ID, VAL_BOT_ID, AI_BOT_ID,
        "smart_money",
    ]
    try:
        from app.services.orderbook_scalp_strategy import SCALP_BOT_ID
        bot_ids.append(SCALP_BOT_ID)
    except Exception:
        pass
    # unique preserve order
    seen = set()
    bot_ids = [b for b in bot_ids if b and not (b in seen or seen.add(b))]

    wipe = await db.wipe_strategy_trading_data(bot_ids)
    await db.set_setting("pnl_epoch", epoch)
    # Mark as an explicit (manual) reset so startup never clears this epoch.
    await db.set_setting("trading_stats_reset_marker", "manual")
    # clear lifetime blobs
    for key in (
        f"ai_lifetime:{AI_BOT_ID}",
        f"ai_misattr_fixed:{AI_BOT_ID}:SOL",
    ):
        try:
            await db.set_setting(key, "")
        except Exception:
            pass

    # in-memory reset for running bots — PnL/logs only; KEEP open positions
    global rotation, impulse, validation, ai_bot
    for bot in (rotation, impulse, validation, ai_bot):
        if not bot:
            continue
        try:
            if hasattr(bot, "_trade_log"):
                # keep only open markers if any
                try:
                    bot._trade_log = [
                        t for t in (bot._trade_log or [])
                        if (t.get("reason") or "").lower() in ("open", "add")
                        or t.get("pnl") is None
                    ]
                except Exception:
                    bot._trade_log = []
            if hasattr(bot, "_session_pnl"):
                bot._session_pnl = 0.0
            if hasattr(bot, "_lifetime_pnl"):
                bot._lifetime_pnl = 0.0
            if hasattr(bot, "_lifetime_trades"):
                bot._lifetime_trades = 0
            if hasattr(bot, "_lifetime_wins"):
                bot._lifetime_wins = 0
            if hasattr(bot, "_lifetime_fees"):
                bot._lifetime_fees = 0.0
            if hasattr(bot, "_equity") and hasattr(bot, "_capital"):
                bot._equity = float(getattr(bot, "_capital", 0) or 0)
            # Re-assert DB claims for any positions still held in memory
            try:
                from app.services.position_claim import claim_open
                positions = getattr(bot, "_positions", None) or {}
                bid = getattr(bot, "BOT_ID", None)
                if bid and positions:
                    for pos in positions.values():
                        await claim_open(
                            db, bid,
                            getattr(pos, "inst_id", None) or getattr(pos, "symbol", ""),
                            getattr(pos, "side", "long"),
                            float(getattr(pos, "size", 0) or 0),
                            float(getattr(pos, "entry_price", 0) or 0),
                        )
            except Exception as e:
                print(f"[reset] re-claim {getattr(bot,'BOT_ID',bot)}: {e}", flush=True)
        except Exception as e:
            print(f"[reset] mem {getattr(bot,'BOT_ID',bot)}: {e}", flush=True)

    # Smart Money ledger (realized PnL counter) only — NEVER wipe mirror/tracker state
    try:
        import os
        from app.services.smart_money_ledger import LEDGER_PATH, get_sm_ledger
        if os.path.exists(LEDGER_PATH):
            os.remove(LEDGER_PATH)
        import app.services.smart_money_ledger as sml
        sml._ledger = None
        get_sm_ledger()
    except Exception as e:
        print(f"[reset] sm ledger: {e}", flush=True)

    # clear caches
    global _bot_stats_cache, _paired_cache
    try:
        _bot_stats_cache["ts"] = 0
        _bot_stats_cache["data"] = {}
    except Exception:
        pass
    try:
        _paired_cache.clear()
    except Exception:
        pass

    print(f"[reset] trading stats wiped epoch={epoch} bots={bot_ids}", flush=True)
    return {
        "ok": True,
        "epoch": epoch,
        "bots": bot_ids,
        "wipe": wipe,
        "message": "PnL and trade cards reset. Counting from epoch. Open exchange positions unchanged.",
    }


@app.get("/api/pnl")

def _active_bot_labels() -> set:
    """Human labels of bots currently running (for dashboard PnL cards)."""
    labels = set()
    try:
        if rotation and getattr(rotation, "_running", False):
            labels.add("Momentum")
        if impulse and getattr(impulse, "_running", False):
            labels.add("Impulse 1D")
        if validation and getattr(validation, "_running", False):
            labels.add("MACD+Donchian Validation")
        if ai_bot and getattr(ai_bot, "_running", False):
            labels.add("AI Discretionary 1H")
        if vwap_rev_bot and getattr(vwap_rev_bot, "_running", False):
            labels.add("VWAP Mean Reversion")
        if sm_tracker and getattr(sm_tracker, "_running", False):
            labels.add("Умные деньги")
    except Exception:
        pass
    return labels


async def get_pnl():

    """Dashboard PnL: ONLY closed trades with hard strategy binding, after pnl_epoch.

    Strict tags: clOrdId prefix / DB bot_id / explicit bot label from pairing.
    Untagged OKX noise never enters Total / 1d / week / strategy cards.
    """
    from datetime import datetime as dt, timezone as tz, timedelta as td

    STRICT_BOTS = {
        "Momentum",
        "Impulse 1D",
        "MACD+Donchian Validation",
        "AI Discretionary 1H",
        "Order Book Scalp",
        "VWAP Mean Reversion",
        "Умные деньги",
    }

    def _normalize_bot(tr: dict) -> str:
        bot = (tr.get("bot") or "").strip()
        if not bot:
            try:
                bot = (_tag_trade_bot(tr) or "").strip()
            except Exception:
                bot = ""
        if not bot:
            try:
                bot = (_db_bot_name(tr.get("bot_id") or "") or "").strip()
            except Exception:
                bot = ""
        # bot_id aliases
        if bot in ("rotation_strategy", "momentum_strategy", MOM_BOT_ID, ROT_BOT_ID):
            bot = "Momentum"
        elif bot in ("impulse_strategy", IMP_BOT_ID):
            bot = "Impulse 1D"
        elif bot in (VAL_BOT_ID, "validation_strategy"):
            bot = "MACD+Donchian Validation"
        elif bot in (AI_BOT_ID, "ai_strategy"):
            bot = "AI Discretionary 1H"
        elif bot in ("smart_money", "smart_money_mirror"):
            bot = "Умные деньги"
        # SOL without ai* never AI
        inst_u = (tr.get("inst_id") or tr.get("symbol") or "").upper()
        cid = str(tr.get("clOrdId") or tr.get("cl_ord_id") or "").lower()
        if bot == "AI Discretionary 1H" and "SOL" in inst_u and not cid.startswith("ai"):
            bot = "Impulse 1D"
        # Require strict known strategy label
        if bot not in STRICT_BOTS:
            return ""
        # Prefer clOrdId / bot_id proof when present on row
        if cid:
            if cid.startswith("rot") and bot != "Momentum":
                bot = "Momentum"
            elif cid.startswith("imp") and bot != "Impulse 1D":
                bot = "Impulse 1D"
            elif cid.startswith("ai") and bot != "AI Discretionary 1H":
                bot = "AI Discretionary 1H"
            elif cid.startswith("val") and bot != "MACD+Donchian Validation":
                bot = "MACD+Donchian Validation"
        return bot

    realized_1d = 0.0
    realized_7d = 0.0
    realized_30d = 0.0
    realized_week = 0.0
    total_realized = 0.0
    total_fees = 0.0
    source = "none"
    per_bot = {}
    account_total = 0.0
    skipped_untagged = 0

    try:
        epoch = await get_pnl_epoch()
        # If reset marker exists but epoch missing, force start of today UTC
        if not epoch:
            try:
                marker = await db.get_setting("trading_stats_reset_marker")
                if marker:
                    epoch = dt.now(tz.utc).strftime("%Y-%m-%dT00:00:00")
                    await db.set_setting("pnl_epoch", epoch)
            except Exception:
                pass

        resp = await get_paired_trades(limit=5000)
        trades = resp.get("trades", []) or []
        closed_tagged = []
        for tr in trades:
            reason = (tr.get("reason") or "").lower()
            if reason in ("open", "add"):
                continue
            if tr.get("pnl") is None:
                continue
            try:
                float(tr.get("pnl"))
            except (TypeError, ValueError):
                continue
            if not _trade_after_epoch(tr, epoch):
                continue
            bot = _normalize_bot(tr)
            # Hard corrections (same as paired forced rules)
            try:
                _inst = str(tr.get("inst_id") or tr.get("symbol") or "")
                _et = str(tr.get("exit_time") or tr.get("time") or "")
                _pnl = float(tr.get("pnl") or 0)
                if (
                    _inst == "ETH-USDT-SWAP"
                    and "2026-09-01T17:33" in _et
                    and abs(_pnl - 167.08) < 45
                ):
                    bot = "AI Discretionary 1H"
                if (
                    _inst == "ETH-USDT-SWAP"
                    and "2026-09-01T17:33" in _et
                    and abs(_pnl - 134.17) < 45
                ):
                    bot = "AI Discretionary 1H"
            except Exception:
                pass
            if not bot:
                skipped_untagged += 1
                continue
            tr = dict(tr)
            tr["bot"] = bot
            closed_tagged.append(tr)

        if closed_tagged or epoch:
            # epoch set ⇒ zeros are valid (fresh start), do not fall back to raw bills
            source = "history_strict" if closed_tagged else "epoch_empty"
            now = dt.now(tz.utc)
            week_start = (now - td(days=now.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            for tr in closed_tagged:
                try:
                    pnl = float(tr.get("pnl", 0) or 0)
                except (TypeError, ValueError):
                    continue
                bot = tr.get("bot") or ""
                if bot:
                    per_bot[bot] = per_bot.get(bot, 0.0) + pnl
                total_realized += pnl
                account_total += pnl
                try:
                    total_fees += abs(float(tr.get("fee", 0) or 0))
                except (TypeError, ValueError):
                    pass
                time_str = tr.get("time", "") or tr.get("exit_time", "")
                if time_str:
                    try:
                        t_time = dt.fromisoformat(time_str)
                        if t_time.tzinfo is None:
                            t_time = t_time.replace(tzinfo=tz.utc)
                        age_sec = (now - t_time).total_seconds()
                        if age_sec <= 86400:
                            realized_1d += pnl
                        if age_sec <= 604800:
                            realized_7d += pnl
                        if age_sec <= 2592000:
                            realized_30d += pnl
                        if t_time >= week_start:
                            realized_week += pnl
                    except (ValueError, OSError, TypeError):
                        pass
            print(
                f"[pnl] strict: total={total_realized:.2f} 1d={realized_1d:.2f} "
                f"week={realized_week:.2f} per_bot={ {k: round(v,2) for k,v in per_bot.items()} } "
                f"tagged={len(closed_tagged)} skip_untagged={skipped_untagged} epoch={epoch!r}",
                flush=True,
            )
    except Exception as e:
        import traceback
        print(f"[pnl] History source error: {e}", flush=True)
        traceback.print_exc()

    # No raw OKX-bills fallback into Total — that reintroduced -$1006 without strategy tags.

    # Unrealized from open positions (exchange)
    unrealized = 0.0
    try:
        pos_result = await _okx_call(lambda c: c.get_positions("SWAP"))
        if not pos_result.get("error"):
            for pos in pos_result.get("data", []):
                try:
                    if abs(float(pos.get("pos", 0) or 0)) <= 0:
                        continue
                except (TypeError, ValueError):
                    continue
                unrealized += float(pos.get("upl", 0) or 0)
    except Exception:
        pass

    try:
        update_daily_pnl(realized_1d + unrealized)
    except Exception:
        pass

    # Funding (OKX bills type=8) — cached to avoid extra OKX load every poll
    funding = 0.0
    funding_source = "none"
    funding_n = 0
    try:
        global _FUNDING_CACHE, _FUNDING_CACHE_TS
        now_f = _time.time()
        if _FUNDING_CACHE_TS and (now_f - _FUNDING_CACHE_TS) < _FUNDING_TTL:
            funding = float(_FUNDING_CACHE or 0)
            funding_source = "cache"
        else:
            epoch = await get_pnl_epoch()
            # Paginate backwards by billId so we don't miss funding events when
            # the account trades many instruments (10+ coins × 3/day ⇒ 50-bill
            # single page covers < 2 days). Cap pages to bound OKX load.
            after = ""
            for _page in range(6):
                resp_f = await _okx_call(
                    lambda c, a=after: c.get_bills(
                        inst_type="SWAP", type="8", limit=100,
                        **({"after": a} if a else {})
                    )
                )
                if resp_f.get("error"):
                    if _page == 0:
                        print(f"[pnl] funding fetch: {resp_f.get('message', '')}", flush=True)
                    break
                page_data = resp_f.get("data") or []
                if not page_data:
                    break
                stop = False
                for b in page_data:
                    ts = b.get("ts") or b.get("cTime") or ""
                    try:
                        if epoch and ts:
                            from datetime import datetime as _dt, timezone as _tz
                            t_iso = _dt.fromtimestamp(int(ts) / 1000, tz=_tz.utc).strftime("%Y-%m-%dT%H:%M:%S")
                            if t_iso[:19] < str(epoch)[:19]:
                                stop = True
                                break
                    except Exception:
                        pass
                    try:
                        v = b.get("pnl")
                        if v is None or v == "":
                            v = b.get("balChg")
                        funding += float(v or 0)
                        funding_n += 1
                    except (TypeError, ValueError):
                        continue
                if stop or len(page_data) < 100:
                    break
                after = page_data[-1].get("billId", "")
                if not after:
                    break
            funding_source = "okx_bills_type8"
            _FUNDING_CACHE = funding
            _FUNDING_CACHE_TS = now_f
    except Exception as e:
        print(f"[pnl] funding: {e}", flush=True)

    # OKX fillPnl is typically net of trading fees; fees field is informational
    fees_note = (
        "OKX fillPnl/bill.pnl is usually net of trading fees; "
        "do not subtract 'fees' again from total. Funding is separate (type=8)."
    )

    economic = total_realized + unrealized + funding

    # Active-bots-only view for main dashboard cards
    per_bot_all = {k: float(v) for k, v in per_bot.items()}
    active = _active_bot_labels()
    if active:
        per_bot = {k: v for k, v in per_bot.items() if k in active}
        total_realized = sum(per_bot.values())
        # Note: 1d/7d/week still include all strict trades; total/per_bot match cards
    return {
        "total": round(total_realized, 2),
        "account_total": round(account_total, 2),
        "1d": round(realized_1d, 2),
        "7d": round(realized_7d, 2),
        "30d": round(realized_30d, 2),
        "week": round(realized_week, 2),
        "unrealized": round(unrealized, 2),
        "funding": round(funding, 4),
        "funding_source": funding_source,
        "funding_bills": funding_n,
        "economic_approx": round(economic, 2),
        "source": source,
        "fees": round(total_fees, 2),
        "fees_informational": True,
        "pnl_includes_fee": True,
        "fees_note": fees_note,
        "per_bot": {k: round(v, 2) for k, v in per_bot.items()},
        "per_bot_all": {k: round(v, 2) for k, v in per_bot_all.items()},
        "active_bots": sorted(active) if active else [],
        "pnl_epoch": await get_pnl_epoch(),
        "skipped_untagged": skipped_untagged,
        "timezone": "UTC",
    }




@app.get("/api/pnl/reconcile", dependencies=[Depends(require_admin)])
async def pnl_reconcile():
    """Compare dashboard strict PnL vs OKX bills (trade + funding) + positions upl.

    Helps detect attribution gaps (untagged), funding drift, and upl mismatch.
    """
    from datetime import datetime as dt, timezone as tz

    dash = await get_pnl()
    epoch = dash.get("pnl_epoch") or await get_pnl_epoch()

    # OKX trade bills — all SWAP closes/opens with pnl
    okx_trade_pnl = 0.0
    okx_trade_n = 0
    okx_tagged_pnl = 0.0
    okx_untagged_pnl = 0.0
    try:
        # Prefer paired pipeline bills if available via get_paired_trades debug
        # Direct bills type=2 (trade) when API supports type filter
        for type_arg in ("2", None):
            try:
                if type_arg:
                    resp = await _okx_call(
                        lambda c, t=type_arg: c.get_bills(inst_type="SWAP", type=t, limit=100)
                    )
                else:
                    resp = await _okx_call(
                        lambda c: c.get_bills(inst_type="SWAP", limit=100)
                    )
                if resp.get("error"):
                    continue
                for b in resp.get("data") or []:
                    sub = str(b.get("subType") or "")
                    # close subtypes
                    if sub and sub not in ("5", "6", "3", "4", "1", "2"):
                        continue
                    try:
                        ts = b.get("ts") or ""
                        if epoch and ts:
                            t_iso = dt.fromtimestamp(int(ts) / 1000, tz=tz.utc).strftime("%Y-%m-%dT%H:%M:%S")
                            if t_iso[:19] < str(epoch)[:19]:
                                continue
                    except Exception:
                        pass
                    try:
                        p = float(b.get("pnl") or 0)
                    except (TypeError, ValueError):
                        p = 0.0
                    if sub in ("5", "6") or p != 0:
                        okx_trade_pnl += p
                        okx_trade_n += 1
                        cid = str(b.get("clOrdId") or "").lower()
                        if cid.startswith(("rot", "imp", "ai", "val")):
                            okx_tagged_pnl += p
                        else:
                            okx_untagged_pnl += p
                break
            except Exception:
                continue
    except Exception as e:
        print(f"[reconcile] bills: {e}", flush=True)

    # Positions upl
    upl = 0.0
    n_pos = 0
    try:
        pos_result = await _okx_call(lambda c: c.get_positions("SWAP"))
        if not pos_result.get("error"):
            for pos in pos_result.get("data") or []:
                try:
                    if abs(float(pos.get("pos") or 0)) <= 0:
                        continue
                    upl += float(pos.get("upl") or 0)
                    n_pos += 1
                except (TypeError, ValueError):
                    continue
    except Exception:
        pass

    r_dash = float(dash.get("total") or 0)
    u_dash = float(dash.get("unrealized") or 0)
    f_dash = float(dash.get("funding") or 0)

    return {
        "ok": abs(u_dash - upl) < 0.5 and abs(r_dash - okx_tagged_pnl) < 5.0,
        "pnl_epoch": epoch,
        "dashboard": {
            "realized_tagged": r_dash,
            "unrealized": u_dash,
            "funding": f_dash,
            "fees_informational": dash.get("fees"),
            "economic_approx": dash.get("economic_approx"),
            "skipped_untagged": dash.get("skipped_untagged"),
            "per_bot": dash.get("per_bot"),
            "source": dash.get("source"),
        },
        "okx": {
            "trade_pnl_all": round(okx_trade_pnl, 4),
            "trade_pnl_tagged_clord": round(okx_tagged_pnl, 4),
            "trade_pnl_untagged": round(okx_untagged_pnl, 4),
            "trade_bills_n": okx_trade_n,
            "unrealized_upl": round(upl, 4),
            "open_positions": n_pos,
            "funding": f_dash,
        },
        "diffs": {
            "realized_dash_minus_okx_tagged": round(r_dash - okx_tagged_pnl, 4),
            "unrealized_dash_minus_okx": round(u_dash - upl, 4),
            "okx_all_minus_dash": round(okx_trade_pnl - r_dash, 4),
        },
        "notes": [
            "Dashboard realized = strategy-tagged closed trades after pnl_epoch only.",
            "OKX trade_pnl_all may include untagged/manual fills.",
            "fillPnl usually already net of trading fees; fees on dashboard are informational.",
            "Funding is separate (bills type=8), included in economic_approx.",
            "Timestamps and epoch filter use UTC.",
        ],
    }


@app.get("/api/reports/summary")
async def reports_summary():
    """Single reporting snapshot for UI/export — same trade source as History/Dashboard.

    Fields:
    - realized / unrealized / fees / funding (funding best-effort from OKX bills type=8)
    - periods 1d/7d/30d/week aligned with /api/pnl
    - trade_count, wins, losses, win_rate from closed paired trades
    - source labels for transparency
    """
    pnl = await get_pnl()
    paired = await get_paired_trades(limit=5000)
    trades = [x for x in (paired.get("trades") or []) if (x.get("reason") or "").lower() in ("closed", "tp", "sl", "trail", "breakeven", "manual", "")]
    # prefer explicit closed-like
    closed = []
    for x in (paired.get("trades") or []):
        reason = (x.get("reason") or "").lower()
        if reason in ("open", "tp1"):
            continue
        try:
            if float(x.get("pnl") or 0) == 0 and reason == "open":
                continue
        except (TypeError, ValueError):
            pass
        closed.append(x)

    fees = 0.0
    wins = losses = 0
    for x in closed:
        try:
            fees += abs(float(x.get("fee") or 0))
        except (TypeError, ValueError):
            pass
        try:
            pval = float(x.get("pnl") or 0)
        except (TypeError, ValueError):
            pval = 0.0
        if pval > 0:
            wins += 1
        elif pval < 0:
            losses += 1
    n = wins + losses
    win_rate = round(wins / n * 100, 1) if n else 0.0

    funding = 0.0
    funding_source = "none"
    try:
        resp = await _okx_call(lambda c: c.get_bills(inst_type="SWAP", type="8", limit=100))
        if not resp.get("error"):
            for b in resp.get("data") or []:
                try:
                    # OKX funding often in pnl or balChg
                    v = b.get("pnl")
                    if v is None or v == "":
                        v = b.get("balChg")
                    funding += float(v or 0)
                except (TypeError, ValueError):
                    continue
            funding_source = "okx_bills_type8"
    except Exception as e:
        print(f"[reports] funding fetch: {e}", flush=True)

    net = float(pnl.get("total") or 0) + float(pnl.get("unrealized") or 0) + funding - 0.0
    # fees already often embedded in trade pnl on OKX; still surface separately
    return {
        "as_of": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "realized_total": pnl.get("total"),
        "realized_1d": pnl.get("1d"),
        "realized_7d": pnl.get("7d"),
        "realized_30d": pnl.get("30d"),
        "realized_week": pnl.get("week"),
        "unrealized": pnl.get("unrealized"),
        "fees_reported": round(fees, 4) if fees else pnl.get("fees"),
        "funding": round(funding, 4),
        "funding_source": funding_source,
        "net_approx": round(float(pnl.get("total") or 0) + float(pnl.get("unrealized") or 0) + funding, 2),
        "per_bot": pnl.get("per_bot") or {},
        "trades_closed": len(closed),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": win_rate,
        "pnl_source": pnl.get("source"),
        "note": "PnL matches History paired trades; funding is OKX bills type=8 (best-effort, last page).",
    }


_bot_stats_cache = {"ts": 0, "data": {}}  # {"Momentum": {...}, "Impulse 1D": {...}}
_BOT_STATS_TTL = 15  # seconds


async def _apply_history_kpi(status: dict, bot_label: str) -> dict:
    """Overlay durable KPI onto strategy status (running or stopped)."""
    status = dict(status or {})
    try:
        all_stats = await _bot_history_stats()
        stats = all_stats.get(bot_label) or {}
        status["total_pnl"] = stats.get("total_pnl", status.get("total_pnl", 0))
        status["total_trades"] = stats.get("total_trades", status.get("total_trades", 0))
        status["wins"] = stats.get("wins", status.get("wins", 0))
        status["losses"] = stats.get("losses", status.get("losses", 0))
        status["win_rate"] = stats.get("win_rate", status.get("win_rate", 0))
        status["lifetime_pnl"] = status.get("total_pnl")
        status["total_pnl_source"] = stats.get("total_pnl_source", "okx_history")
        status["kpi_from_history"] = True
    except Exception as e:
        print(f"[kpi] {bot_label}: {e}", flush=True)
        status["kpi_from_history"] = False
    return status


async def _bot_history_stats() -> dict:
    """Per-bot KPI from the SAME pipeline as /api/pnl — works even if bots are stopped.

    Always returns entries for known strategy cards (zeros after pnl_epoch reset).
    """
    now_s = _time.time()
    if now_s - _bot_stats_cache["ts"] < _BOT_STATS_TTL:
        return _bot_stats_cache["data"]

    KNOWN = (
        "Momentum", "Impulse 1D", "MACD+Donchian Validation",
        "AI Discretionary 1H", "Order Book Scalp", "Умные деньги",
    )
    stats = {
        name: {
            "total_pnl": 0.0,
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "total_pnl_source": "okx_history",
        }
        for name in KNOWN
    }
    try:
        pnl_resp = await get_pnl()
        per = pnl_resp.get("per_bot") or {}
        resp = await get_paired_trades(limit=5000)
        epoch = await get_pnl_epoch()
        counts = {}
        pnl_sum = {}
        for tr in resp.get("trades", []) or []:
            bot = tr.get("bot") or _db_bot_name(tr.get("bot_id") or "") or ""
            if bot not in KNOWN:
                continue
            if (tr.get("reason") or "").lower() in ("open", "add"):
                continue
            if not _trade_after_epoch(tr, epoch):
                continue
            try:
                pnl = float(tr.get("pnl", 0) or 0)
            except (TypeError, ValueError):
                continue
            c = counts.setdefault(bot, {"total_trades": 0, "wins": 0, "losses": 0})
            c["total_trades"] += 1
            if pnl > 0:
                c["wins"] += 1
            elif pnl < 0:
                c["losses"] += 1
            pnl_sum[bot] = pnl_sum.get(bot, 0.0) + pnl

        for bot in KNOWN:
            mapped_pnl = per.get(bot)
            if mapped_pnl is None:
                for k, v in per.items():
                    if (_db_bot_name(k) or k) == bot:
                        mapped_pnl = v
                        break
            if mapped_pnl is None:
                mapped_pnl = pnl_sum.get(bot, 0.0)
            c = counts.get(bot) or {"total_trades": 0, "wins": 0, "losses": 0}
            total = int(c["total_trades"])
            stats[bot] = {
                "total_pnl": round(float(mapped_pnl or 0), 2),
                "total_trades": total,
                "wins": int(c.get("wins", 0)),
                "losses": int(c.get("losses", 0)),
                "win_rate": round(c["wins"] / total * 100, 1) if total else 0.0,
                "total_pnl_source": "okx_history",
            }
    except Exception as e:
        print(f"[bot_stats] error: {e}", flush=True)
    _bot_stats_cache["ts"] = now_s
    _bot_stats_cache["data"] = stats
    return stats


# ── Trades ──

@app.get("/api/trades")
async def get_all_trades(limit: int = 100):
    """Trades from Rotation strategy (not OKX fills)."""
    if rotation:
        return {"trades": rotation._trade_log[-limit:]}
    return {"trades": []}


_paired_cache: dict = {}
_paired_lock = asyncio.Lock()
_PAIRED_TTL = 10  # seconds — dashboard polls /api/pnl + /api/trades/paired every 10s


@app.get("/api/trades/paired")
async def get_paired_trades(limit: int = 500, begin: str = None, end: str = None):
    """Paired entry+exit trades — all bots, all time, sourced from the DB
    (persisted) plus live in-memory logs. Fallback to OKX fills only when
    nothing is stored yet.

    Cached for _PAIRED_TTL with single-flight: /api/pnl, /api/trades/paired
    and the bot-status stats all recompute the same expensive pipeline
    (OKX bills + fills + DB reads), which previously spiked to 5-14s on every
    cache expiry and stalled the dashboard."""
    global _paired_cache
    now_s = _time.time()
    if _paired_cache and (now_s - _paired_cache["ts"]) < _PAIRED_TTL:
        trades = _paired_cache["data"]
        return {"trades": trades[:limit], "debug": dict(_paired_cache["debug"])}
    async with _paired_lock:
        now_s = _time.time()
        if _paired_cache and (now_s - _paired_cache["ts"]) < _PAIRED_TTL:
            trades = _paired_cache["data"]
            return {"trades": trades[:limit], "debug": dict(_paired_cache["debug"])}
        resp = await _get_paired_trades_impl(limit=5000, begin=begin, end=end)
        trades = resp.get("trades", [])
        if trades:
            _paired_cache = {
                "ts": _time.time(),
                "data": trades,
                "debug": resp.get("debug", {}),
            }
        return {"trades": trades[:limit], "debug": resp.get("debug", {})}


_warm_task: Optional[asyncio.Task] = None
_WARM_INTERVAL = 45.0


async def _warm_dashboard_caches() -> None:
    """Keep dashboard caches warm without saturating the free-tier instance."""
    while True:
        try:
            await asyncio.sleep(_WARM_INTERVAL)
            try:
                await asyncio.wait_for(get_paired_trades(limit=300), timeout=25.0)
            except asyncio.TimeoutError:
                print("[warm] paired cache timed out (25s) — skip", flush=True)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[warm] paired cache: {e}", flush=True)


async def _get_paired_trades_impl(limit: int = 500, begin: str = None, end: str = None):
    """Paired entry+exit trades — all bots, all time, sourced from the DB
    (persisted) plus live in-memory logs. Fallback to OKX fills only when
    nothing is stored yet."""
    # 1. Gather all raw trade records: persisted (DB) + live (in-memory).
    raw = []
    bot_ids = [ROT_BOT_ID, MOM_BOT_ID, IMP_BOT_ID, VAL_BOT_ID, AI_BOT_ID]
    # Map inst_id -> most recent bot that traded it. Used to tag manual/external
    # closes (manual_close/exchange_stop) whose close order has no clOrdId and
    # whose ord_id is empty in the DB — without this, OKX rows lose their bot
    # attribution and their PnL drops out of strategy cards.
    # Sources: in-memory trade logs (fast, reliable) + DB trades (fallback).
    inst_last_bot: dict = {}
    _bot_name_map = {
        ROT_BOT_ID: "Momentum", MOM_BOT_ID: "Momentum",
        IMP_BOT_ID: "Impulse 1D", VAL_BOT_ID: "MACD+Donchian Validation",
        AI_BOT_ID: "AI Discretionary 1H",
        "rotation": "Momentum", "momentum": "Momentum",
        "impulse": "Impulse 1D", "validation": "MACD+Donchian Validation",
        "ai": "AI Discretionary 1H",
    }
    # Build from in-memory trade logs (most reliable — survives DB failures)
    for _bid_name, _bot_obj in [("rotation", rotation), ("impulse", impulse),
                                  ("validation", validation), ("ai", ai_bot)]:
        if _bot_obj and hasattr(_bot_obj, '_trade_log') and _bot_obj._trade_log:
            for _t in reversed(_bot_obj._trade_log):
                _i = _t.get("symbol") or _t.get("inst_id") or ""
                if _i and _i not in inst_last_bot:
                    inst_last_bot[_i] = _bid_name
    # Fallback: DB trades (if table is populated)
    if db:
        try:
            _ib_rows = await db._fetchall(
                "SELECT inst_id, bot_id, timestamp FROM trades "
                "WHERE bot_id IS NOT NULL AND bot_id != '' "
                "ORDER BY timestamp DESC LIMIT 2000"
            )
            for _r in _ib_rows:
                _i = _r.get("inst_id") or ""
                if _i and _i not in inst_last_bot:
                    inst_last_bot[_i] = str(_r.get("bot_id") or "").split(":")[0]
        except Exception as e:
            print(f"[trades/paired] inst_last_bot DB fallback: {e}", flush=True)
    if db:
        try:
            for bid in bot_ids:
                rows = await db.get_trades(bot_id=bid, limit=5000)
                for t in rows:
                    px = float(t.get("px", 0) or 0)
                    pnl = float(t.get("pnl", 0) or 0)
                    inst = t.get("inst_id", "")
                    raw.append({
                        "time": t.get("timestamp", ""),
                        "side": t.get("side", ""),
                        "symbol": inst,
                        "inst_id": inst,
                        "ord_id": str(t.get("ord_id", "") or "").strip(),
                        "entry_price": px,
                        "exit_price": None,
                        "pnl": pnl,
                        "reason": "open" if pnl == 0 else "closed",
                        "pos_side": "long" if t.get("side") == "buy" else "short",
                        "signal_id": t.get("signal_id", 0),
                        "bot_id": bid,
                    })
        except Exception as e:
            print(f"[trades/paired] DB read error: {e}", flush=True)

    # Live in-memory logs (they may include entries not yet flushed to DB).
    live_bots = [("rotation", rotation), ("impulse", impulse), ("validation", validation),
                 ("ai", ai_bot)]
    live_names = {ROT_BOT_ID: "Momentum", IMP_BOT_ID: "Impulse 1D",
                  VAL_BOT_ID: "MACD+Donchian Validation", AI_BOT_ID: "AI Discretionary 1H"}
    for key, bot in live_bots:
        if bot and bot._trade_log:
            for t in bot._trade_log:
                raw.append({
                    "time": t.get("time", ""),
                    "side": t.get("side", ""),
                    "symbol": t.get("symbol", ""),
                    "inst_id": t.get("symbol", "") or t.get("inst_id", ""),
                    "ord_id": str(t.get("ord_id", "") or "").strip(),
                    "entry_price": t.get("entry_price") or t.get("entry", 0),
                    "exit_price": t.get("exit_price", None),
                    "pnl": t.get("pnl", 0),
                    "reason": t.get("reason", "open"),
                    "pos_side": t.get("pos_side", "long"),
                    "signal_id": t.get("signal_id", 0),
                    "bot_id": t.get("bot_id", ""),
                })

    # 2. OKX authoritative rows FIRST (bills = exact realized PnL/fee per ord_id,
    #    fills = real prices + open/close pairing). A local ledger row is trusted
    #    only when OKX does not cover the same trade — this keeps the pushed
    #    rows faithful to the exchange (the ledger may carry pnl=0 / placeholder
    #    prices, e.g. XRP closes, which previously broke the History cards).
    okx_rows = []
    okx_ord_ids = set()
    okx_close_key = set()  # (inst_id, close-time floored to the minute)
    okx_open_keys = {}     # inst_id -> set(ord_id) for open rows
    bill_by_ord = {}
    flag_raw = bool(raw)
    bills = []
    try:
        bills = await _fetch_all_trade_bills()
        for b in bills:
            bid = str(b.get("ordId", "")).strip()
            if not bid:
                continue
            try:
                bp = float(b.get("pnl") or 0)
            except (TypeError, ValueError):
                bp = 0.0
            try:
                bf = abs(float(b.get("fee") or 0))
            except (TypeError, ValueError):
                bf = 0.0
            prev = bill_by_ord.get(bid)
            if prev is None:
                bill_by_ord[bid] = {"pnl": bp, "fee": bf,
                                    "ts": b.get("ts", ""),
                                    "clOrdId": str(b.get("clOrdId", "") or "").strip()}
            else:
                # Aggregate pnl/fee across ALL bills sharing the same ordId
                # (e.g. partial fills where one close order produces multiple
                # bills). The old code only stored the first bill, which
                # caused the last-mile enrich (step 4) to overwrite the
                # correctly aggregated pnl from _pair_bills with a single
                # bill's pnl — e.g. the ETH close at 20:44 30.08 showed
                # pnl=13.33 instead of 680.10 (sum of 18 close bills).
                prev["pnl"] += bp
                prev["fee"] += bf
                prev["ts"] = b.get("ts", prev["ts"])
                if not prev["clOrdId"]:
                    prev["clOrdId"] = str(b.get("clOrdId", "") or "").strip()
    except Exception as e:
        print(f"[trades/paired] bills fetch error: {e}", flush=True)

    try:
        raw_fills = await _fetch_okx_fills(limit=1000)
    except Exception as e:
        print(f"[trades/paired] fills fetch error: {e}", flush=True)
        raw_fills = []
    raw_fills = [f for f in raw_fills
                 if not str(f.get("clOrdId", "") or "").startswith("val")]
    try:
        val_ord_ids = {str(r["ord_id"]).strip() for r in await db._fetchall(
            "SELECT ord_id FROM trades WHERE bot_id = ? AND ord_id IS NOT NULL"
            " AND ord_id != ''"
            if not db._pg_mode else
            "SELECT ord_id FROM trades WHERE bot_id = $1 AND ord_id IS NOT NULL"
            " AND ord_id != ''",
            (VAL_BOT_ID,)) if r.get("ord_id")}
    except Exception:
        val_ord_ids = set()
    if val_ord_ids:
        raw_fills = [f for f in raw_fills
                     if str(f.get("ordId", "")).strip() not in val_ord_ids]

    # ord_id -> bot (attribution fallback for orders without clOrdId)
    try:
        _rows = await db._fetchall(
            "SELECT bot_id, ord_id FROM trades WHERE ord_id IS NOT NULL AND ord_id != ''"
        )
        ord_to_bot = {str(r["ord_id"]).strip(): str(r["bot_id"]).split(":")[0]
                      for r in _rows if r.get("ord_id")}
    except Exception:
        ord_to_bot = {}
    fill_clord = {str(f.get("ordId", "")).strip(): str(f.get("clOrdId", "") or "").strip()
                  for f in raw_fills}

    # inst_id -> bot from ENTRY fills (clOrdId prefix). Used as final fallback
    # for close orders whose own clOrdId is missing (manual/external/exchange_stop).
    # The ENTRY fill always has the bot's clOrdId (e.g. "rot...").
    # Only subType 3/4 (entry) fills count, and we iterate NEWEST-first so the
    # MOST RECENT opener wins — the bot that opened the current/last position is
    # the rightful owner. (The old code kept the OLDEST entry, which wrongly
    # attributed ETH to Momentum because an old Momentum entry predated the
    # AI opens that actually opened the position being closed.)
    inst_entry_bot: dict = {}
    _clord_to_bot = {"rot": "Momentum", "imp": "Impulse 1D", "ai": "AI Discretionary 1H",
                     "val": "MACD+Donchian Validation",
                     "scl": "Order Book Scalp", "scalp": "Order Book Scalp",
                     "vwap": "VWAP Mean Reversion"}
    for _f in reversed(raw_fills):
        _sub = str(_f.get("subType") or "")
        if _sub not in ("3", "4"):
            continue  # only entry fills define who opened the position
        _cid = str(_f.get("clOrdId", "") or "").strip().lower()
        _fi = _f.get("instId") or _f.get("inst_id") or ""
        if not _fi or not _cid:
            continue
        # Side of the entry: sell opens a short, buy opens a long. A position can
        # be opened by different bots over time on the SAME instrument (AI opened
        # an ETH short with ai..., later Momentum opened an ETH long with rot...),
        # so the opener is keyed by (inst_id, side), not just inst_id.
        _fside = str(_f.get("side") or "").lower()
        _entry_side = "short" if _fside == "sell" else ("long" if _fside == "buy" else "")
        if not _entry_side:
            continue
        _key = ( _fi, _entry_side)
        for _prefix, _bname in _clord_to_bot.items():
            if _cid.startswith(_prefix) and _key not in inst_entry_bot:
                inst_entry_bot[_key] = _bname
                break

    def _okx_bot(ord_id: str, *, entry_ord_id: str = "") -> str:
        """Map OKX ordId → strategy label via clOrdId prefix or DB trades.bot_id.

        Ownership follows the ENTRY order: the bot that OPENED the position is
        the rightful owner, even if the closing order carries a different bot's
        clOrdId (e.g. Momentum adopted an AI position and later closed it with a
        rot... clOrdId). The close-order clOrdId is only used when no entry
        clOrdId is known (e.g. manually-opened positions closed by a bot)."""
        def _match(prefix: str) -> str:
            m = {
                "rot": "Momentum", "imp": "Impulse 1D", "ai": "AI Discretionary 1H",
                "val": "MACD+Donchian Validation",
                "scl": "Order Book Scalp", "scalp": "Order Book Scalp",
                "vwap": "VWAP Mean Reversion",
            }
            for k, v in m.items():
                if prefix.startswith(k):
                    return v
            return ""
        # ENTRY clOrdId is authoritative for ownership (the opener owns the trade).
        if entry_ord_id:
            ecid = (fill_clord.get(entry_ord_id, "")
                    or bill_by_ord.get(entry_ord_id, {}).get("clOrdId", "")
                    or "").strip().lower()
            if ecid:
                b = _match(ecid)
                if b:
                    return b
        # Fallback: the order's own clOrdId (manual/external opens lack a bot
        # clOrdId on the entry, so attribute by who closed it).
        cid = (fill_clord.get(ord_id, "")
               or bill_by_ord.get(ord_id, {}).get("clOrdId", "")
               or "").strip().lower()
        if cid:
            b = _match(cid)
            if b:
                return b
        # Any bot_id stored for this ord_id (AI/Validation/Scalp/etc.)
        b = _db_bot_name(ord_to_bot.get(ord_id, ""))
        return b or ""

    pair_bills_err = ""
    try:
        fills_paired = _pair_bills(bills) if bills else await _pair_fills(raw_fills)
        for t in fills_paired:
            inst = t.get("inst_id", "") or t.get("symbol", "")
            is_open = t.get("reason") == "open"
            ord_id = str(t.get("ord_id", "") or "").strip()
            bill = bill_by_ord.get(ord_id)
            if bill and t.get("source") != "okx_bills":
                t = dict(t)
                t["pnl"] = bill["pnl"]
                t["fee"] = str(bill["fee"])
            if is_open:
                okx_open_keys.setdefault(inst, set()).add(ord_id)
            else:
                okx_ord_ids.add(ord_id)
                try:
                    okx_close_key.add((inst, (t.get("time") or "")[:16]))
                except Exception:
                    pass
            entry_px = t.get("entry", 0) or t.get("entry_price", 0)
            exit_px = t.get("exit_price", 0)
            entry_ord = str(t.get("entry_ord_id", "") or "").strip()
            okx_rows.append({
                "time": t.get("time", ""),
                "entry_time": t.get("entry_time", ""),
                "exit_time": t.get("time", "") if not is_open else None,
                "side": "buy" if t.get("pos_side") == "long" else "sell",
                "symbol": inst,
                "inst_id": inst,
                "ord_id": ord_id,
                "entry": entry_px,
                "entry_px": entry_px,
                "exit_price": exit_px,
                "exit_px": exit_px,
                "pnl": t.get("pnl", 0) if not is_open else None,
                "reason": t.get("reason", ""),
                "pos_side": t.get("pos_side", "long"),
                "signal_id": t.get("signal_id", 0) or ord_id,
                "bot": _okx_bot(ord_id, entry_ord_id=entry_ord),
                "fee": t.get("fee", "0"),
            })
    except Exception as e:
        import traceback
        print(f"[trades/paired] OKX pairing error: {e}", flush=True)
        traceback.print_exc()
        pair_bills_err = f"{type(e).__name__}: {e}"

    # Dedupe. Open pairs: only one live position per instrument exists at a time
    # (cooldown + single-position-per-bot), so collapsing by inst+minute is safe
    # and merges DB/live copies with the OKX row (OKX rows are added first, so
    # they win — real prices/PnL, corrupt ledger rows collapse away).
    def _dedup_key(t):
        oid = str(t.get("ord_id") or "").strip()
        ts = t.get("exit_time") or t.get("entry_time") or t.get("time") or ""
        if oid:
            return ("oid", oid)
        try:
            ts_floored = ts[:16]
        except Exception:
            ts_floored = ts
        reason = (t.get("reason") or "").lower()
        if reason in ("open", "add") or t.get("pnl") is None:
            return ("open", t.get("inst_id") or "", ts_floored)
        pnl = t.get("pnl")
        try:
            pnl_r = round(float(pnl or 0), 4)
        except (TypeError, ValueError):
            pnl_r = 0.0
        return ("fb", t.get("inst_id") or "", pnl_r, ts_floored)

    seen = set()
    dedup = []
    for t in okx_rows:
        key = _dedup_key(t)
        if key in seen:
            continue
        seen.add(key)
        dedup.append(t)

    # Backfill strategy tag for rows that still lack bot (esp. AI without clOrdId,
    # and manual/external closes whose close order has no clOrdId + empty ord_id).
    for t in dedup:
        if t.get("bot"):
            continue
        try:
            tagged = _tag_trade_bot(t)
            if tagged:
                t["bot"] = tagged
                continue
        except Exception:
            pass
        # DB ord_id map again with full bot name list
        oid = str(t.get("ord_id") or "").strip()
        if oid and oid in ord_to_bot:
            t["bot"] = _db_bot_name(ord_to_bot[oid]) or t.get("bot") or ""
            continue
        # Manual/external close fallback: no ord_id / no clOrdId — attribute by
        # the instrument's most recent entry fill (inst_entry_bot uses the ENTRY
        # order's clOrdId prefix, which is the most reliable signal for who opened
        # the position). Fallback to inst_last_bot (in-memory trade log) only if
        # entry fills are also unavailable.
        inst = str(t.get("inst_id") or t.get("symbol") or "").strip()
        # ENTRY fill clOrdId map — most reliable: the person who opened the trade
        # is the rightful owner, even if the close came from a different bot
        # (e.g. Momentum's orphan sweep or exchange-stop).
        if inst and inst in inst_entry_bot:
            t["bot"] = inst_entry_bot[inst] or t.get("bot") or ""
            continue
        # In-memory trade log fallback: last bot that touched this instrument.
        if inst and inst in inst_last_bot:
            _raw_name = inst_last_bot[inst]
            t["bot"] = _bot_name_map.get(_raw_name, _raw_name) or t.get("bot") or ""
            continue

    # 3. Legacy coverage from DB + live memory — ONLY for trades OKX does not
    #    cover (older than the fills window, or missing ord_id with no matching
    #    OKX close). Corrupt ledger closes (pnl=0 with an OKX close in the same
    #    minute, or an ord_id OKX already accounted for) are skipped.
    if flag_raw:
        raw.sort(key=lambda t: (t.get("time") or "", t.get("side") or ""))
        legacy = []
        for t in raw:
            inst = t.get("inst_id") or t.get("symbol", "")
            reason = (t.get("reason") or "").lower()
            oid = str(t.get("ord_id", "") or "").strip()
            if oid and oid in okx_ord_ids:
                continue  # OKX already has this close (real data wins)
            try:
                close_key = (inst, (t.get("time") or "")[:16])
            except Exception:
                close_key = None
            if close_key and close_key in okx_close_key:
                continue  # corrupt ledger close (pnl=0) with a real OKX close nearby
            is_entry = reason in ("open", "add") or (t.get("pnl") in (None, 0) and t.get("side") == "buy")
            if is_entry:
                # Live open rows that OKX already reports as open are redundant.
                if okx_open_keys.get(inst):
                    continue
                legacy.append(t)
                continue
            pnl0 = float(t.get("pnl", 0) or 0)
            if pnl0 == 0:
                continue  # placeholder/corrupt close (zero pnl) — the real OKX row wins
            legacy.append(t)
        open_map = {}
        paired = []
        for t in legacy:
            inst = t.get("inst_id") or t.get("symbol", "")
            pnl = float(t.get("pnl", 0) or 0)
            reason = (t.get("reason") or "").lower()
            is_entry = reason in ("open", "add") or (pnl == 0 and t.get("side") == "buy")
            if is_entry:
                open_map.setdefault(inst, []).append(t)
            elif pnl != 0 or reason in ("closed", "close", "manual_close", "exchange_stop", "rotation_exit", "stop", "tp", "trail", "breakeven", "roi"):
                entries = open_map.get(inst, [])
                entry = entries.pop(0) if entries else None
                bot_name = _db_bot_name(t.get("bot_id", "")) or "Momentum"
                paired.append({
                    "time": t.get("time", ""),
                    "entry_time": entry.get("time", "") if entry else t.get("time", ""),
                    "exit_time": t.get("time", ""),
                    "side": "buy" if (t.get("pos_side") == "long" or entry and entry.get("pos_side") == "long") else "sell",
                    "symbol": t.get("symbol", ""),
                    "inst_id": t.get("inst_id", "") or t.get("symbol", ""),
                    "ord_id": str(t.get("ord_id", "") or "").strip(),
                    "entry": entry.get("entry_price", 0) if entry else 0,
                    "entry_px": entry.get("entry_price", 0) if entry else 0,
                    "exit_price": t.get("exit_price", 0) or float(t.get("entry_price", 0) or 0),
                    "exit_px": t.get("exit_price", 0) or float(t.get("entry_price", 0) or 0),
                    "pnl": pnl,
                    "reason": reason,
                    "pos_side": t.get("pos_side", "long"),
                    "signal_id": t.get("signal_id", ""),
                    "bot": bot_name,
                })
        # 3. Still-open entries.
        for inst, entries in open_map.items():
            for entry in entries:
                bot_name = _db_bot_name(entry.get("bot_id", "")) or "Momentum"
                paired.append({
                    "time": entry.get("time", ""),
                    "entry_time": entry.get("time", ""),
                    "exit_time": None,
                    "side": "buy" if entry.get("pos_side") == "long" else "sell",
                    "symbol": entry.get("symbol", ""),
                    "inst_id": entry.get("inst_id", "") or entry.get("symbol", ""),
                    "ord_id": str(entry.get("ord_id", "") or "").strip(),
                    "entry": entry.get("entry_price", 0),
                    "entry_px": entry.get("entry_price", 0),
                    "exit_price": None,
                    "exit_px": None,
                    "pnl": None,
                    "reason": "open",
                    "pos_side": entry.get("pos_side", "long"),
                    "signal_id": entry.get("signal_id", ""),
                    "bot": bot_name,
                })
        paired.sort(key=lambda t: (t.get("exit_time") or t.get("entry_time") or ""), reverse=True)
        # Merge legacy rows into the OKX-first dedup (OKX rows already in `seen`
        # win by ord_id / inst+minute, so corrupt ledger closes collapse away).
        for t in paired:
            key = _dedup_key(t)
            if key in seen:
                continue
            seen.add(key)
            dedup.append(t)

    # Correct attribution: the bot that OPENED the position owns the trade, even
    # if a different bot closed it (e.g. Momentum adopted an AI position and
    # closed it with a rot... clOrdId). inst_entry_bot maps (inst_id, side) -> bot
    # from the ENTRY fill's clOrdId, which is the authoritative opener signal.
    # Keyed by (inst, side) because the same instrument can have different openers
    # for different sides (AI short vs Momentum long on the same coin).
    # Applied AFTER the legacy merge so DB-sourced rows are corrected too.
    try:
        for t in dedup:
            inst = str(t.get("inst_id") or t.get("symbol") or "").strip()
            if not inst:
                continue
            pside = str(t.get("pos_side") or "long").strip().lower()
            opener = inst_entry_bot.get((inst, pside), "")
            if not opener:
                opener = inst_entry_bot.get(inst, "")
            if not opener:
                continue
            cur_bot = str(t.get("bot") or "")
            if cur_bot and cur_bot != opener:
                t["bot"] = opener
    except Exception as e:
        print(f"[trades/paired] entry-owner override error: {e}", flush=True)

    # Admin / manual corrections (durable settings + built-in known fixes)
    try:
        import json as _json
        forced = []
        # Known fix: ETH short closed 2026-09-01 ~17:33 UTC (+134 USDT) was
        # labeled Momentum but opened by AI Discretionary.
        # ETH close 2026-09-01 17:33 UTC (20:33 MSK) +167.08 — Telegram said Momentum,
        # but position was opened by AI. Match by time+pnl (close of short is side=buy).
        forced.append({
            "inst_id": "ETH-USDT-SWAP",
            "exit_time_prefix": "2026-09-01T17:33",
            "pnl_near": 167.08,
            "to_bot": "AI Discretionary 1H",
        })
        forced.append({
            "inst_id": "ETH-USDT-SWAP",
            "exit_time_prefix": "2026-09-01T17:33",
            "pnl_near": 134.17,
            "to_bot": "AI Discretionary 1H",
        })
        try:
            raw = await db.get_setting("pnl_bot_overrides")
            if raw:
                extra = _json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(extra, list):
                    forced.extend(extra)
        except Exception:
            pass
        for rule in forced:
            inst = str(rule.get("inst_id") or rule.get("inst") or "").strip()
            to_bot = str(rule.get("to_bot") or "").strip()
            if not inst or not to_bot:
                continue
            pside = str(rule.get("pos_side") or rule.get("side") or "").strip().lower()
            pnl_near = rule.get("pnl_near")
            exit_date = str(rule.get("exit_date") or rule.get("date") or "")
            exit_pfx = str(rule.get("exit_time_prefix") or "")
            for t in dedup:
                if (t.get("reason") or "").lower() not in ("closed", "close", "partial"):
                    continue
                ti = str(t.get("inst_id") or t.get("symbol") or "").strip()
                if ti != inst and not ti.startswith(inst.split("-")[0]):
                    continue
                if ti != inst:
                    continue
                et = str(t.get("exit_time") or t.get("time") or t.get("timestamp") or "")
                if exit_pfx and exit_pfx not in et:
                    continue
                if exit_date and not exit_pfx and exit_date not in et:
                    continue
                if pside:
                    tps = str(t.get("pos_side") or "").lower()
                    ts = str(t.get("side") or "").lower()
                    # close of short is typically side=buy; open short side=sell
                    if tps and tps not in (pside, ""):
                        if not (pside == "short" and ts in ("buy", "sell", "short")):
                            if not (pside == "long" and ts in ("buy", "sell", "long")):
                                continue
                if pnl_near is not None:
                    try:
                        if abs(float(t.get("pnl") or 0) - float(pnl_near)) > 45.0:
                            continue
                    except (TypeError, ValueError):
                        continue
                prev = t.get("bot")
                t["bot"] = to_bot
                t["bot_id"] = "ai_strategy" if "AI" in to_bot else t.get("bot_id")
                if prev != to_bot:
                    print(f"[trades/paired] forced bot {prev!r}→{to_bot!r} {inst} pnl={t.get('pnl')} time={et[:19]}", flush=True)
    except Exception as e:
        print(f"[trades/paired] forced override error: {e}", flush=True)

    # 4. Last-mile: enrich any surviving closed row with the exact OKX bill PnL
    #    and fee (covers closes whose fills fell outside the fill window but
    #    whose ord_id is present in the account bills).
    try:
        for t in dedup:
            if (t.get("reason") or "").lower() != "closed":
                continue
            oid = str(t.get("ord_id") or "").strip()
            bill = bill_by_ord.get(oid) if oid else None
            if bill:
                t["pnl"] = bill["pnl"]
                t["fee"] = str(bill["fee"])
    except Exception:
        pass

    dedup.sort(key=lambda t: (t.get("exit_time") or t.get("entry_time") or ""), reverse=True)
    print(f"[trades/paired] OKX+bills+DB: {len(dedup)} trades "
          f"(okx_rows={len(okx_rows)}, legacy={len(paired) if flag_raw else 0})", flush=True)
    return {"trades": dedup[:limit],
            "debug": {"bills": len(bills), "raw_fills": len(raw_fills),
                      "okx_rows": len(okx_rows), "okx_ord_ids": len(okx_ord_ids),
                      "pair_err": pair_bills_err,
                      "inst_entry_bot": _json_safe_dict(inst_entry_bot)}}

    # 2. Fallback: fetch real fills from OKX exchange
    try:
        _fills_cache_ts = 0
        raw_fills = await _fetch_okx_fills(limit=300)
        # Exclude fills that belong to the validation bot (its ordIds are
        # persisted with bot_id=validation_strategy). Otherwise its trades
        # leak into the Momentum window on the dashboard.
        try:
            val_ord_ids = {str(r["ord_id"]).strip() for r in await db._fetchall(
                "SELECT ord_id FROM trades WHERE bot_id = ? AND ord_id IS NOT NULL"
                " AND ord_id != ''"
                if not db._pg_mode else
                "SELECT ord_id FROM trades WHERE bot_id = $1 AND ord_id IS NOT NULL"
                " AND ord_id != ''",
                (VAL_BOT_ID,)) if r.get("ord_id")}
        except Exception:
            val_ord_ids = set()
        if val_ord_ids:
            raw_fills = [f for f in raw_fills if str(f.get("ordId", "")).strip() not in val_ord_ids]
        # Also drop fills whose client order id marks them as the validation bot
        # (CL_ORD_PREFIX="val"). New orders from the validator carry clOrdId=val<ts>.
        raw_fills = [f for f in raw_fills
                     if not str(f.get("clOrdId", "")).startswith("val")]
        paired = await _pair_fills(raw_fills)
        if paired:
            # Build DB positions map for fallback tagging
            db_pos_map = {}
            try:
                db_rows = await db.get_all_positions()
                for row in db_rows:
                    db_pos_map[(row.get("inst_id", ""), row.get("side", ""))] = row.get("bot_id", "")
            except Exception:
                pass
            result = []
            for t in paired[-limit:]:
                entry_px = t.get("entry", 0) or t.get("entry_price", 0)
                exit_px = t.get("exit_price", 0)
                is_open = t.get("reason") == "open"
                result.append({
                    "time": t.get("time", ""),
                    "entry_time": t.get("entry_time", ""),
                    "exit_time": t.get("time", "") if not is_open else None,
                    "side": "buy" if t.get("pos_side") == "long" else "sell",
                    "symbol": t.get("inst_id", "") or t.get("symbol", ""),
                    "inst_id": t.get("inst_id", "") or t.get("symbol", ""),
                    "entry": entry_px,
                    "entry_px": entry_px,
                    "exit_price": exit_px,
                    "exit_px": exit_px,
                    "pnl": t.get("pnl", 0) if not is_open else None,
                    "reason": t.get("reason", ""),
                    "pos_side": t.get("pos_side", "long"),
                    "signal_id": t.get("ord_id", ""),
                    "bot": _tag_trade_bot(t, db_pos_map=db_pos_map),
                })
            print(f"[trades/paired] OKX fallback: {len(result)} trades from exchange", flush=True)
            return {"trades": result}
    except Exception as e:
        import traceback
        print(f"[trades/paired] OKX fallback error: {e}", flush=True)
        traceback.print_exc()

    # 3. Last resort: DB paired trades
    try:
        db_trades = await db.get_paired_trades(limit=limit, begin=begin, end=end, bot_ids=[ROT_BOT_ID, MOM_BOT_ID, IMP_BOT_ID, VAL_BOT_ID])
        result = []
        for t in db_trades:
            entry_side = t.get("entry_side", "buy")
            entry_px = t.get("entry_px", 0)
            exit_px = t.get("exit_px", 0)
            try:
                entry_px = float(entry_px) if entry_px else 0
            except (TypeError, ValueError):
                entry_px = 0
            try:
                exit_px = float(exit_px) if exit_px else 0
            except (TypeError, ValueError):
                exit_px = 0
            result.append({
                "time": t.get("exit_time") or t.get("entry_time", ""),
                "entry_time": t.get("entry_time", ""),
                "exit_time": t.get("exit_time", ""),
                "side": entry_side,
                "symbol": t.get("inst_id", ""),
                "inst_id": t.get("inst_id", ""),
                "entry": entry_px,
                "entry_px": entry_px,
                "exit_price": exit_px,
                "exit_px": exit_px,
                "pnl": float(t.get("pnl", 0) or 0),
                "reason": "closed",
                "pos_side": "long" if entry_side == "buy" else "short",
                "signal_id": t.get("signal_id", ""),
                "bot": _db_bot_name(t.get("bot_id", "")),
            })
        return {"trades": result}
    except Exception as e:
        import traceback
        print(f"[trades/paired] DB fallback error: {e}", flush=True)
        traceback.print_exc()
        return {"trades": []}


@app.get("/api/debug/trades-db", dependencies=[Depends(require_admin)])
async def debug_trades_db():
    """Diagnostic: check what's in the DB trades table."""
    try:
        count = await db._fetchone("SELECT count(*) as c FROM trades")
        total = count["c"] if count else 0

        with_signal = await db._fetchone("SELECT count(*) as c FROM trades WHERE signal_id IS NOT NULL")
        paired_count = with_signal["c"] if with_signal else 0

        # Get last 5 trades
        recent = await db._fetchall("SELECT id, bot_id, inst_id, side, px, pnl, state, timestamp, signal_id FROM trades ORDER BY timestamp DESC LIMIT 5")
        for r in recent:
            r["px"] = str(r.get("px", ""))

        # Try paired trades query
        paired = await db.get_paired_trades(limit=5, bot_ids=[ROT_BOT_ID, MOM_BOT_ID])

        return {
            "total_trades": total,
            "with_signal_id": paired_count,
            "recent_trades": recent,
            "paired_trades": paired,
        }
    except Exception as e:
        import traceback
        return {"error": str(e), "traceback": traceback.format_exc()}


# ── DB Positions ──

@app.get("/api/debug/fills", dependencies=[Depends(require_admin)])
async def debug_fills():
    """Diagnostic endpoint: shows raw OKX fills (ALL fields) and pairing results."""
    client = client_manager.get_client()
    client_ok = client is not None
    demo = _env_demo

    # Force cache bypass
    global _fills_cache_ts
    _fills_cache_ts = 0

    raw_fills = await _fetch_okx_fills(limit=100)
    paired = await _pair_fills(raw_fills)

    # Show first raw fill with ALL fields for inspection
    first_fill = raw_fills[0] if raw_fills else {}
    # Also show one that might be a close (different side or later)
    close_candidate = None
    for f in raw_fills:
        ps = f.get("posSide", "")
        s = f.get("side", "")
        if ps and ps != "net" and ((ps == "long" and s == "sell") or (ps == "short" and s == "buy")):
            close_candidate = f
            break

    closed_trades = [t for t in paired if t.get("reason") == "closed"]
    open_trades = [t for t in paired if t.get("reason") == "open"]

    return {
        "client_ok": client_ok,
        "demo": demo,
        "raw_fills_count": len(raw_fills),
        "paired_count": len(paired),
        "closed_count": len(closed_trades),
        "open_count": len(open_trades),
        "first_fill_all_fields": first_fill,
        "close_candidate_all_fields": close_candidate,
        "sample_raw": raw_fills[:3],
        "sample_closed": closed_trades[:3],
        "sample_open": open_trades[:3],
        "field_names": list(first_fill.keys()) if first_fill else [],
    }


@app.get("/api/analysis", dependencies=[Depends(require_admin)])
async def trade_analysis():
    """Detailed trade analysis: PnL breakdown, last ETH trades, stop-loss detection."""
    # Force cache bypass
    global _fills_cache_ts
    _fills_cache_ts = 0

    # Fetch ETH fills specifically
    eth_fills = await _fetch_okx_fills(limit=300, inst_id="ETH-USDT-SWAP")
    eth_paired = await _pair_fills(eth_fills)

    # Also get all fills for total PnL
    _fills_cache_ts = 0  # reset cache for all-instruments fetch
    all_fills = await _fetch_okx_fills(limit=300)
    all_paired = await _pair_fills(all_fills)

    # Current positions
    pos_result = await _okx_call(lambda c: c.get_positions("SWAP"))
    positions = []
    if not pos_result.get("error"):
        for p in pos_result.get("data", []):
            positions.append({
                "inst_id": p.get("instId"),
                "pos_side": p.get("posSide", "net"),
                "size": float(p.get("pos", 0)),
                "avg_entry": float(p.get("avgPx", 0)),
                "upl": float(p.get("upl", 0)),
                "upl_ratio": float(p.get("uplRatio", 0)),
                "liq_price": float(p.get("liqPx", 0)) if p.get("liqPx") else None,
            })

    # Analyze ETH trades
    eth_closed = [t for t in eth_paired if t.get("reason") == "closed"]
    eth_open = [t for t in eth_paired if t.get("reason") == "open"]

    # Cumulative PnL
    cumulative = 0.0
    eth_trade_details = []
    for t in eth_closed:
        pnl = float(t.get("pnl", 0) or 0)
        cumulative += pnl
        entry_px = t.get("entry", 0)
        exit_px = t.get("exit_price", 0)
        # Detect if SL was hit (loss > 1% of entry)
        sl_hit = False
        if pnl < 0 and entry_px > 0:
            loss_pct = abs(pnl / (entry_px * t.get("size", 1))) * 100
            if loss_pct > 1.0:
                sl_hit = True
        eth_trade_details.append({
            "entry_time": t.get("entry_time", ""),
            "close_time": t.get("time", ""),
            "entry": entry_px,
            "exit": exit_px,
            "size": t.get("size", 0),
            "pnl": round(pnl, 4),
            "cumulative_pnl": round(cumulative, 4),
            "pos_side": t.get("pos_side", ""),
            "sl_hit": sl_hit,
            "price_change_pct": round((exit_px - entry_px) / entry_px * 100, 2) if entry_px > 0 else 0,
        })

    # Total PnL across all instruments
    total_pnl = sum(float(t.get("pnl", 0) or 0) for t in all_paired if t.get("reason") == "closed")
    total_trades = sum(1 for t in all_paired if t.get("reason") == "closed")
    win_trades = sum(1 for t in all_paired if t.get("reason") == "closed" and float(t.get("pnl", 0) or 0) > 0)
    loss_trades = total_trades - win_trades

    return {
        "summary": {
            "total_pnl": round(total_pnl, 2),
            "total_trades": total_trades,
            "win": win_trades,
            "loss": loss_trades,
            "win_rate": round(win_trades / total_trades * 100, 1) if total_trades > 0 else 0,
        },
        "eth": {
            "total_fills": len(eth_fills),
            "closed_trades": len(eth_closed),
            "open_trades": len(eth_open),
            "total_eth_pnl": round(sum(float(t.get("pnl", 0) or 0) for t in eth_closed), 4),
            "last_10_trades": eth_trade_details[-10:],
            "open_positions": [{
                "entry": t.get("entry", 0),
                "size": t.get("size", 0),
                "entry_time": t.get("entry_time", ""),
                "pos_side": t.get("pos_side", ""),
            } for t in eth_open],
        },
        "current_positions": positions,
        "by_instrument": {
            inst: {
                "trades": sum(1 for t in all_paired if t.get("inst_id") == inst and t.get("reason") == "closed"),
                "pnl": round(sum(float(t.get("pnl", 0) or 0) for t in all_paired if t.get("inst_id") == inst and t.get("reason") == "closed"), 4),
            }
            for inst in set(t.get("inst_id", "") for t in all_paired)
        },
    }


@app.get("/api/db/positions", dependencies=[Depends(require_admin)])
async def get_db_positions():
    positions = await db.get_all_positions()
    return {"positions": positions}


# ── Analysis log download ──

@app.get("/api/analysis/log", dependencies=[Depends(require_admin)])
async def analysis_log_download(request: Request):
    token = get_token(request)
    if not validate(token):
        raise HTTPException(status_code=401, detail="Unauthorized")
    p = Path(DEFAULT_PATH)
    if not p.exists():
        raise HTTPException(status_code=404, detail="Analysis log not found")
    return FileResponse(
        str(p),
        media_type="application/x-ndjson",
        filename=p.name,
        headers={"Cache-Control": "no-store"},
    )


# ── Static files / SPA ──

if STATIC_DIR.exists():
    @app.head("/api/health")
    async def health_head():
        return JSONResponse({"status": "ok"})

    @app.head("/")
    async def root_head():
        return Response(status_code=200)

    @app.get("/")
    async def serve_root():
        return FileResponse(
            str(STATIC_DIR / "index.html"),
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        if full_path.startswith("api/") or full_path.startswith("docs") or full_path.startswith("openapi"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        candidate = STATIC_DIR / full_path
        if full_path and candidate.is_file():
            return FileResponse(
                str(candidate),
                headers={"Cache-Control": "public, max-age=31536000, immutable"},
            )
        return FileResponse(
            str(STATIC_DIR / "index.html"),
            headers={"Cache-Control": "no-store"},
        )


# ── Mini App client logs ──

_MINI_LOG_RING = []


@app.post("/api/debug/mini-log", dependencies=[Depends(require_admin)])
async def mini_log_collect(data: dict):
    """Collect client-side logs from the Telegram Mini App (admin-only)."""
    logs = (data or {}).get("logs") or []
    if isinstance(logs, list):
        lines = [str(l)[:2000] for l in logs]
        _MINI_LOG_RING.extend(lines)
        del _MINI_LOG_RING[:-500]
        for line in lines:
            logger.info("MINI %s", line)
    return {"saved": len(logs)}


@app.post("/api/debug/client-error")
async def client_error_collect(data: dict):
    """Collect frontend JS errors (public — no auth, for WebView diagnostics)."""
    err = (data or {}).get("error") or data or {}
    msg = str(err.get("message") or err)[:2000]
    stack = str(err.get("stack") or "")[:3000]
    line = f"CLIENT-ERR: {msg}\n{stack}"
    _MINI_LOG_RING.append(line)
    del _MINI_LOG_RING[:-500]
    logger.error("CLIENT-ERR %s %s", msg, stack[:500])
    return {"ok": True}


@app.get("/api/debug/client-errors")
async def client_errors_read():
    """Read recent captured client errors (public, for diagnostics)."""
    return {"errors": [l for l in _MINI_LOG_RING if l.startswith("CLIENT-ERR")][-20:]}


@app.get("/api/debug/mini-log", dependencies=[Depends(require_admin)])
async def mini_log_read():
    """Return the most recent Mini App client logs."""
    return {"count": len(_MINI_LOG_RING), "logs": _MINI_LOG_RING[-150:]}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("BACKEND_PORT", "8000"))
    host = os.getenv("BACKEND_HOST", "0.0.0.0")
    uvicorn.run("app.main:app", host=host, port=port, reload=True)
