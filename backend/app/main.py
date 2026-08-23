import asyncio
import json
import logging
import os
import time as _time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from dataclasses import asdict

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
    get_user_id, encrypt_str, decrypt_str,
    check_rate_limit, record_attempt, guest_rate_limited, record_guest,
)
from app.services.strategy_manager import StrategyManager, PerUserClientManager
from app.services.rotation_strategy import RotationStrategy, RotationConfig, ROT_BOT_ID, STRATEGY_DESC
from app.services.impulse_strategy import ImpulseStrategy, ImpulseConfig, IMP_BOT_ID, STRATEGY_DESC as IMPULSE_DESC, STRATEGY_NAME as IMPULSE_NAME, STRATEGY_VERSION as IMPULSE_VERSION
from app.services.validation_strategy import ValidationStrategy, make_validation_config, VAL_BOT_ID
from app.services.ai_strategy import AIStrategy, AIConfig, AI_BOT_ID, STRATEGY_DESC as AI_DESC, STRATEGY_NAME as AI_NAME, STRATEGY_VERSION as AI_VERSION
from app.services.ai_agent import llm_status
from app.services.telegram_notifier import TelegramNotifier
from app.services.strategy_cards import BACKTEST_SUMMARY as _BACKTEST_SUMMARY
from app.services.telegram_bot import TelegramBotPoller, _is_active, PRO_PRICE_STARS, PRO_PLAN_DAYS
from app.services.equity_tracker import EquityTracker, SNAPSHOT_INTERVAL
from app.services.risk_guard import get_status as risk_get_status, set_kill_switch, assert_can_open, update_daily_pnl
from app.services.analysis_logger import DEFAULT_PATH

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

trade_log: list = []
_STARTED_AT = None  # set in startup(); used by /api/health uptime
rotation: Optional[RotationStrategy] = None
impulse: Optional[ImpulseStrategy] = None
validation: Optional[ValidationStrategy] = None
telegram = TelegramNotifier()
bot_poller: Optional[TelegramBotPoller] = None
equity_tracker: Optional[EquityTracker] = None

# Multi-tenant: per-user bots + their own OKX clients.
strategy_mgr = StrategyManager(db=db, notifier=telegram)
ai_bot = None  # AI Discretionary instance
_user_clients: dict[str, OKXClient] = {}
PLANS_PRICE = {"signals": PRO_PRICE_STARS, "pro": PRO_PRICE_STARS}

# ── Auth helpers ──

def get_token(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return ""


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
        print("[startup] 1/7 DB init ...", flush=True)
        await db.init()
        await telegram.load_from_db(db)
        print("[startup] 2/7 OKX client init ...", flush=True)
        if _env_key and _env_secret and _env_pass:
            await client_manager.init_client(_env_key, _env_secret, _env_pass, _env_demo)
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
        if _env_key and _env_secret and _env_pass and _bots_auto_start:
            rot_config = RotationConfig(
                symbols=["BTC", "ETH", "BNB", "XRP", "SOL", "DOGE", "ADA", "TRX", "AVAX", "LTC"],
                capital=10000.0,
                top_k=2,
                roc_period=14,
                ema_fast=20,
                ema_slow=50,
                atr_period=14,
                adx_min=25.0,
                min_roc=3.5,
                sma_long=200,
                min_hold_days=11,
                max_leverage=2.0,
                risk_per_trade=0.20,
                allocation_pct=0.45,
                atr_stop_mult=4.5,
                trail_atr_mult=3.0,
                breakeven_pct=0.05,
                partial_tp_pct=0.08,
                partial_tp_ratio=0.5,
                allow_short=True,
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
        if _env_key and _env_secret and _env_pass and _bots_auto_start:
            imp_config = ImpulseConfig(
                symbols=["BTC", "ETH", "BNB", "XRP", "SOL", "DOGE", "ADA", "TRX", "AVAX", "LTC"],
                capital=10000.0,
                top_k=3,
                entry_roc=3.0,
                max_adds=0,
                risk_per_trade=0.10,
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
        if _env_key and _env_secret and _env_pass and _bots_auto_start:
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
        _warm_task = asyncio.create_task(_warm_dashboard_caches())
        print("[startup] Dashboard cache warmer started", flush=True)
    except Exception as e:
        print(f"[startup] Dashboard cache warmer error: {e}", flush=True)

    try:
        print("[startup] AI Discretionary (optional) ...", flush=True)
        _ai_auto = os.getenv("AI_AUTO_START", "0").strip().lower() in ("1", "true", "yes", "on")
        if _env_key and _env_secret and _env_pass and _ai_auto:
            global ai_bot
            ai_cfg = AIConfig(capital=float(os.getenv("AI_CAPITAL", "10000")))
            ai_bot = AIStrategy(config=ai_cfg, client_manager=client_manager, db=db,
                               notifier=telegram)
            ai_bot.start()
            print("[startup]   AI Discretionary RUNNING", flush=True)
        else:
            print("[startup]   AI skipped (set AI_AUTO_START=1 to enable)", flush=True)
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
}

ADMIN_ONLY_PATHS = {
    "/api/credentials/status",
    "/api/credentials/test",
    "/api/credentials/init",
    "/api/trade/order",
    "/api/positions/close",
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
}

ADMIN_ONLY_PREFIXES = ("/api/debug/",)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if not request.url.path.startswith("/api/") or request.url.path in PUBLIC_API_PATHS:
        return await call_next(request)
    role = validate(get_token(request))
    if role is None:
        return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    if request.url.path in ADMIN_ONLY_PATHS or request.url.path.startswith(ADMIN_ONLY_PREFIXES):
        if role != "admin":
            return JSONResponse(status_code=403, content={"detail": "Forbidden"})
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
        return {"token": token, "role": "admin"}
    record_attempt(ip, False)
    raise HTTPException(status_code=401, detail="Invalid password")


@app.post("/api/auth/guest")
async def auth_guest(request: Request):
    ip = request.client.host if request.client else "unknown"
    if guest_rate_limited(ip):
        raise HTTPException(status_code=429, detail="Too many requests. Try again later.")
    record_guest(ip)
    token = guest()
    return {"token": token, "role": "guest"}


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
    return logout(token)


# ══════════════════════════════════════════════════════════════
# MULTI-TENANT /api/me/* — per-user mini-app account
# ══════════════════════════════════════════════════════════════

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
    if plan != "pro":
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
    global ai_bot
    if not ai_bot:
        return {
            "running": False,
            "strategy": AI_NAME,
            "version": AI_VERSION,
            "description": AI_DESC,
            **llm_status(),
            "open_positions": [],
            "total_pnl": 0,
        }
    return ai_bot.get_status()


@app.post("/api/ai/start", dependencies=[Depends(require_admin)])
async def ai_start(data: dict = None):
    global ai_bot
    data = data or {}
    if ai_bot and getattr(ai_bot, "_running", False):
        return {"message": "AI already running", **ai_bot.get_status()}
    cfg = AIConfig(
        capital=float(data.get("capital") or os.getenv("AI_CAPITAL", "10000")),
        max_leverage=float(data.get("max_leverage") or 3),
        max_positions=int(data.get("max_positions") or 1),
        risk_per_trade=float(data.get("risk_per_trade") or 0.02),
        poll_interval_sec=int(data.get("poll_interval_sec") or 120),
        provider=data.get("provider") or ("groq" if os.getenv("GROQ_API_KEY", "").strip() else None),
        execute=bool(data["execute"]) if "execute" in data else None,
    )
    if data.get("symbols"):
        cfg.symbols = list(data["symbols"])
    ai_bot = AIStrategy(config=cfg, client_manager=client_manager, db=db, notifier=telegram)
    ai_bot.start()
    return {"message": "AI Discretionary started", **ai_bot.get_status()}


@app.post("/api/ai/stop", dependencies=[Depends(require_admin)])
async def ai_stop():
    global ai_bot
    if ai_bot:
        ai_bot.stop()
    return {"message": "AI stopped", "running": False}


@app.post("/api/ai/decide", dependencies=[Depends(require_admin)])
async def ai_decide_once():
    global ai_bot
    if not ai_bot or not getattr(ai_bot, "_running", False):
        raise HTTPException(status_code=400, detail="AI bot not running — start first")
    client = client_manager.get_client() if client_manager else None
    if not client:
        raise HTTPException(status_code=400, detail="OKX client not ready")
    await ai_bot._fetch_indicators(client)
    snap = ai_bot._snapshot()
    from app.services.ai_agent import call_llm
    from datetime import datetime, timezone
    decision = await call_llm(snap, provider=ai_bot._provider())
    ai_bot._last_decision = {**decision, "time": datetime.now(timezone.utc).isoformat()}
    return {
        "snapshot": {
            "indicators": snap.get("indicators"),
            "open_positions": snap.get("open_positions"),
            "equity": snap.get("equity"),
        },
        "decision": decision,
    }


# ── Health ──

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
        },
        "auth": "jwt",
        "risk": risk_get_status().to_dict(),
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

def _tag_position_bot(inst_id: str, pos_side: str) -> str:
    """Determine which bot owns an OKX position by checking running bots' in-memory positions.

    Order: Rotation (Momentum) → Impulse → Validation. Validation shares the same
    coin universe and must not steal tags from older strategies if both claim a pos.
    """
    norm_side = pos_side.lower() if pos_side else ""

    def _match(bot) -> bool:
        if not (bot and bot._running and bot._positions):
            return False
        for coin, pos in bot._positions.items():
            if pos.inst_id == inst_id and pos.side == norm_side:
                return True
        return False

    if _match(rotation):
        return "Momentum"
    if _match(impulse):
        return "Impulse 1D"
    if _match(validation):
        return "MACD+Donchian Validation"

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
    return ""


def _tag_trade_bot(trade: dict) -> str:
    """Tag a paired trade with bot name. Works for both open and closed trades."""
    inst_id = trade.get("inst_id", "") or trade.get("symbol", "")
    pos_side = trade.get("pos_side", "")
    # DB bot_id is authoritative when present
    by_id = _db_bot_name(trade.get("bot_id", "") or "")
    if by_id:
        return by_id
    if trade.get("reason") == "open":
        return _tag_position_bot(inst_id, pos_side)
    # Prefer exact ordId match against in-memory close logs (most reliable)
    ord_id = str(trade.get("ord_id") or trade.get("close_ord_id") or "").strip()
    if ord_id:
        for bot_label, log in (
            ("Momentum", getattr(rotation, "_trade_log", None) if rotation else None),
            ("Impulse 1D", getattr(impulse, "_trade_log", None) if impulse else None),
            ("MACD+Donchian Validation", getattr(validation, "_trade_log", None) if validation else None),
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
    # Fallback: match by symbol+side (works when entry_time is unknown)
    side = trade.get("side", "")
    if rotation and rotation._trade_log:
        for t in rotation._trade_log:
            if t.get("symbol", "") == inst_id and t.get("side", "") == side and t.get("pnl", 0) != 0:
                return "Momentum"
    if impulse and impulse._trade_log:
        for t in impulse._trade_log:
            if t.get("symbol", "") == inst_id and t.get("side", "") == side and t.get("pnl", 0) != 0:
                return "Impulse 1D"
    if validation and validation._trade_log:
        for t in validation._trade_log:
            if t.get("symbol", "") == inst_id and t.get("side", "") == side and t.get("pnl", 0) != 0:
                return "MACD+Donchian Validation"
    # Fallback: DB bot_id stored for this trade
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
    return ""


@app.get("/api/positions")
async def get_positions(inst_type: str = "SWAP"):
    global _positions_cache, _positions_cache_ts
    now_s = _time.time()
    if _positions_cache is not None and (now_s - _positions_cache_ts) < _POS_CACHE_TTL:
        return _positions_cache
    result = await _okx_call(lambda c: c.get_positions(inst_type))
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result.get("message", ""))
    # Tag each position with bot name
    tagged = []
    for p in result.get("data", []):
        p["bot"] = _tag_position_bot(p.get("instId", ""), p.get("posSide", "net"))
        tagged.append(p)
    out = {"positions": tagged}
    _positions_cache = out
    _positions_cache_ts = _time.time()
    return out


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
_POS_CACHE_TTL = 5  # seconds — avoid 2+ OKX calls per dashboard poll for positions/balance


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
    # Redirect to rotation strategy
    if not rotation:
        return {
            "running": False, "config": {"max_positions": 2, "risk_per_trade": 0, "tp1_pct": 0},
            "equity": 0, "open_positions": [], "total_signals": 0, "total_trades": 0,
            "recent_signals": [], "recent_trades": [], "description": STRATEGY_DESC,
        }
    status = rotation.get_status()
    internal = status.get("total_pnl")
    status["total_pnl_internal"] = internal
    # Prefer same History/per_bot source as /api/pnl so card matches dashboard totals
    stats = (await _bot_history_stats()).get("Momentum")
    if stats and stats.get("total_trades", 0) > 0:
        status.update(stats)
        status["total_pnl_source"] = "okx_history"
        if internal is not None and abs(float(internal or 0) - float(stats.get("total_pnl") or 0)) > 1.0:
            print(f"[momentum/status] PnL mismatch internal={internal} history={stats.get('total_pnl')}", flush=True)
    else:
        # Fallback: sum in-memory close log (same as internal equity path)
        status["total_pnl_source"] = "internal"
    return status


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
                trade["bot"] = _tag_trade_bot(trade)
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
        return {"running": False, "strategy": IMPULSE_NAME, "version": IMPULSE_VERSION,
                "equity": 0, "capital": 0, "open_positions": [], "closed_trades": 0,
                "config": None, "description": IMPULSE_DESC}
    status = impulse.get_status()
    internal = status.get("total_pnl")
    status["total_pnl_internal"] = internal
    stats = (await _bot_history_stats()).get("Impulse 1D")
    if stats and stats.get("total_trades", 0) > 0:
        status.update(stats)
        status["total_pnl_source"] = "okx_history"
        if internal is not None and abs(float(internal or 0) - float(stats.get("total_pnl") or 0)) > 1.0:
            print(f"[impulse/status] PnL mismatch internal={internal} history={stats.get('total_pnl')}", flush=True)
    else:
        status["total_pnl_source"] = "internal"
    return status


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
    return validation.get_status()


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
        if cur is not None and cur["size"] > 0:
            avg_entry = cur["cost"] / cur["size"]
            pos_side = cur["pos_side"]
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
            ("bills", lambda c, **kw: c.get_bills(inst_type="SWAP", **kw)),
            ("archive", lambda c, **kw: c.get_bills_archive(inst_type="SWAP", **kw)),
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


@app.get("/api/pnl")
async def get_pnl():
    """PNL for Dashboard metric cards — all realized figures are computed
    directly from the History rows (get_paired_trades: DB + OKX bills/fills),
    so the cards always match the History section exactly. Only the
    "unrealized" card is taken from OKX positions (matches the exchange).
    Falls back to OKX bills / in-memory logs when History is empty."""
    from datetime import datetime as dt, timezone as tz, timedelta as td

    realized_1d = 0.0
    realized_7d = 0.0
    realized_30d = 0.0
    realized_week = 0.0
    total_realized = 0.0
    total_fees = 0.0
    source = "none"
    per_bot = {}
    account_total = 0.0

    # ── 1. Primary: History rows — the single source for the cards ──
    # `total`/`account_total` cover EVERY closed trade shown in the History
    # section (Momentum, Impulse 1D, Validation, manual…), so the "Всего"
    # card equals History's "Итого". `per_bot` is the per-bot breakdown of the
    # same rows; open rows (pnl=None) contribute nothing.
    try:
        resp = await get_paired_trades(limit=5000)
        trades = resp.get("trades", [])
        closed = [t for t in trades if (t.get("reason") or "").lower() != "open"]
        if closed:
            source = "history"
            now = dt.now(tz.utc)
            week_start = (now - td(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
            for t in closed:
                try:
                    pnl = float(t.get("pnl", 0) or 0)
                except (TypeError, ValueError):
                    continue
                bot = t.get("bot") or ""
                if bot:
                    per_bot[bot] = per_bot.get(bot, 0.0) + pnl
                total_realized += pnl
                account_total += pnl
                try:
                    fee = abs(float(t.get("fee", 0) or 0))
                except (TypeError, ValueError):
                    fee = 0.0
                if fee is not None:
                    total_fees += fee
                time_str = t.get("time", "") or t.get("exit_time", "")
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
                        realized_30d += pnl
                else:
                    realized_30d += pnl
            print(f"[pnl] History (primary): total={total_realized:.2f} account_total={account_total:.2f} "
                  f"1d={realized_1d:.2f} 7d={realized_7d:.2f} 30d={realized_30d:.2f} week={realized_week:.2f} "
                  f"fees={total_fees:.2f} closed={len(closed)} per_bot={per_bot}", flush=True)
    except Exception as e:
        import traceback
        print(f"[pnl] History source error: {e}", flush=True)
        traceback.print_exc()

    # ── 2. Fallback: OKX bills (only if History is empty) ──
    if source == "none":
        try:
            bills = await _fetch_all_trade_bills()
            if bills:
                now = dt.now(tz.utc)
                week_start = (now - td(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
                source = "okx_bills"
                for b in bills:
                    try:
                        b_pnl = float(b.get("pnl") or 0)
                    except (TypeError, ValueError):
                        continue
                    if b_pnl == 0:
                        continue
                    try:
                        b_fee = abs(float(b.get("fee") or 0))
                    except (TypeError, ValueError):
                        b_fee = 0.0
                    total_fees += b_fee
                    total_realized += b_pnl
                    account_total += b_pnl
                    ts_str = b.get("ts", "")
                    if ts_str:
                        try:
                            b_time = dt.fromtimestamp(int(ts_str) / 1000, tz=tz.utc)
                            age_sec = (now - b_time).total_seconds()
                            if age_sec <= 86400:
                                realized_1d += b_pnl
                            if age_sec <= 604800:
                                realized_7d += b_pnl
                            if age_sec <= 2592000:
                                realized_30d += b_pnl
                            if b_time >= week_start:
                                realized_week += b_pnl
                        except (ValueError, OSError, TypeError):
                            realized_30d += b_pnl
                    else:
                        realized_30d += b_pnl
                print(f"[pnl] OKX bills fallback: total={total_realized:.2f} "
                      f"1d={realized_1d:.2f} 7d={realized_7d:.2f} 30d={realized_30d:.2f} "
                      f"week={realized_week:.2f} fees={total_fees:.2f}", flush=True)
        except Exception as e:
            import traceback
            print(f"[pnl] OKX bills fallback error: {e}", flush=True)
            traceback.print_exc()

    # ── 3. Fallback: OKX fills pairing (if neither history nor bills) ──
    if source == "none":
        try:
            _fills_cache_ts = 0  # bypass cache
            raw_fills = await _fetch_okx_fills(limit=300)
            paired = await _pair_fills(raw_fills)
            if paired:
                source = "okx_fills"
                now = dt.now(tz.utc)
                week_start = (now - td(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
                for t in paired:
                    if t.get("reason") != "closed":
                        continue
                    try:
                        trade_pnl = float(t.get("pnl", 0) or 0)
                    except (ValueError, TypeError):
                        continue
                    total_realized += trade_pnl
                    account_total += trade_pnl
                    try:
                        total_fees += abs(float(t.get("fee", 0) or 0))
                    except (ValueError, TypeError):
                        pass
                    time_str = t.get("time", "")
                    if time_str:
                        try:
                            t_time = dt.fromisoformat(time_str)
                            if t_time.tzinfo is None:
                                t_time = t_time.replace(tzinfo=tz.utc)
                            age_sec = (now - t_time).total_seconds()
                            if age_sec <= 86400:
                                realized_1d += trade_pnl
                            if age_sec <= 604800:
                                realized_7d += trade_pnl
                            if age_sec <= 2592000:
                                realized_30d += trade_pnl
                            if t_time >= week_start:
                                realized_week += trade_pnl
                        except (ValueError, OSError, TypeError):
                            realized_30d += trade_pnl
                    else:
                        realized_30d += trade_pnl
        except Exception as e:
            import traceback
            print(f"[pnl] OKX fills error: {e}", flush=True)
            traceback.print_exc()

    # ── 4. Last fallback: in-memory from running bots ──
    if source == "none":
        now = dt.now(tz.utc)
        week_start = (now - td(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        all_logs = []
        if rotation and rotation._trade_log:
            all_logs.extend(rotation._trade_log)

        if all_logs:
            source = "memory"
            for t in all_logs:
                pnl = t.get("pnl", 0)
                if not pnl:
                    continue
                total_realized += pnl
                account_total += pnl
                try:
                    t_time = dt.fromisoformat(t["time"])
                    if t_time.tzinfo is None:
                        t_time = t_time.replace(tzinfo=tz.utc)
                    age = (now - t_time).total_seconds()
                    if age <= 86400:
                        realized_1d += pnl
                    if age <= 604800:
                        realized_7d += pnl
                    if age <= 2592000:
                        realized_30d += pnl
                    if t_time >= week_start:
                        realized_week += pnl
                except Exception:
                    realized_30d += pnl

    # ── Unrealized PNL — always from OKX positions (matches exchange) ──
    unrealized = 0.0
    try:
        pos_result = await _okx_call(lambda c: c.get_positions("SWAP"))
        if not pos_result.get("error"):
            for p in pos_result.get("data", []):
                # Ignore flat rows OKX sometimes returns with residual upl
                try:
                    if abs(float(p.get("pos", 0) or 0)) <= 0:
                        continue
                except (TypeError, ValueError):
                    continue
                unrealized += float(p.get("upl", 0) or 0)
    except Exception:
        pass

    # Feed risk_guard so place_order can enforce RISK_MAX_DAILY_LOSS_USD
    try:
        update_daily_pnl(realized_1d + unrealized)
    except Exception:
        pass

    return {
        "total": round(total_realized, 2),
        "account_total": round(account_total, 2),
        "1d": round(realized_1d, 2),
        "7d": round(realized_7d, 2),
        "30d": round(realized_30d, 2),
        "week": round(realized_week, 2),
        "unrealized": round(unrealized, 2),
        "source": source,
        "fees": round(total_fees, 2),
        "per_bot": {k: round(v, 2) for k, v in per_bot.items()},
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


async def _bot_history_stats() -> dict:
    """Per-bot cumulative stats from the SAME pipeline as /api/pnl (get_paired_trades).

    Cached briefly so status cards and Total PnL breakdown never diverge.
    """
    now_s = _time.time()
    if now_s - _bot_stats_cache["ts"] < _BOT_STATS_TTL:
        return _bot_stats_cache["data"]
    stats = {}
    try:
        # Reuse get_pnl so per_bot keys/sums are identical to dashboard cards
        pnl_resp = await get_pnl()
        per = pnl_resp.get("per_bot") or {}
        # Also count trades for win_rate from paired list
        resp = await get_paired_trades(limit=5000)
        counts = {}
        for tr in resp.get("trades", []):
            bot = tr.get("bot") or ""
            if bot not in ("Momentum", "Impulse 1D", "MACD+Donchian Validation"):
                continue
            if (tr.get("reason") or "").lower() == "open":
                continue
            try:
                pnl = float(tr.get("pnl", 0) or 0)
            except (TypeError, ValueError):
                continue
            c = counts.setdefault(bot, {"total_trades": 0, "wins": 0, "losses": 0})
            c["total_trades"] += 1
            if pnl > 0:
                c["wins"] += 1
            else:
                c["losses"] += 1
        for bot, total_pnl in per.items():
            if bot not in ("Momentum", "Impulse 1D", "MACD+Donchian Validation"):
                # map bot_id style keys if any
                mapped = _db_bot_name(bot) or bot
            else:
                mapped = bot
            if mapped not in ("Momentum", "Impulse 1D", "MACD+Donchian Validation"):
                continue
            c = counts.get(mapped) or counts.get(bot) or {"total_trades": 0, "wins": 0, "losses": 0}
            total = c["total_trades"]
            stats[mapped] = {
                "total_pnl": round(float(total_pnl or 0), 2),
                "total_trades": total,
                "wins": c.get("wins", 0),
                "losses": c.get("losses", 0),
                "win_rate": round(c["wins"] / total * 100, 1) if total else 0.0,
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
_PAIRED_TTL = 30  # seconds — dashboard polls /api/pnl + /api/trades/paired every 10s


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
_WARM_INTERVAL = 30


async def _warm_dashboard_caches() -> None:
    """Keep the expensive dashboard caches hot in the background so the first
    (and every 30s) user request never pays the 3-8s bills/fills/DB pipeline."""
    while True:
        try:
            await asyncio.sleep(_WARM_INTERVAL)
            await get_paired_trades(limit=500)
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
    bot_ids = [ROT_BOT_ID, MOM_BOT_ID, IMP_BOT_ID, VAL_BOT_ID]
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
    live_bots = [("rotation", rotation), ("impulse", impulse), ("validation", validation)]
    live_names = {ROT_BOT_ID: "Momentum", IMP_BOT_ID: "Impulse 1D", VAL_BOT_ID: "MACD+Donchian Validation"}
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
            if prev is None or (bp != 0 and prev.get("pnl") == 0):
                bill_by_ord[bid] = {"pnl": bp, "fee": bf,
                                    "ts": b.get("ts", ""),
                                    "clOrdId": str(b.get("clOrdId", "") or "").strip()}
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

    def _okx_bot(ord_id: str) -> str:
        cid = fill_clord.get(ord_id, "") or bill_by_ord.get(ord_id, {}).get("clOrdId", "")
        if cid.startswith("rot"):
            return "Momentum"
        if cid.startswith("imp"):
            return "Impulse 1D"
        b = _db_bot_name(ord_to_bot.get(ord_id, ""))
        return b if b in ("Momentum", "Impulse 1D") else ""

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
                "bot": _okx_bot(ord_id),
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
                      "pair_err": pair_bills_err}}

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
                    "bot": _tag_trade_bot(t),
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
