import asyncio
import json
import logging
import os
import time as _time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional
from dataclasses import asdict

logger = logging.getLogger("app")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response

from app.services.okx_client import OKXClientManager
from app.services.backtest_service import run_backtest_async
from app.database import db
from app.services.auth import (
    login, guest, validate, logout, is_admin, PASSWORD, grant_admin,
    check_rate_limit, record_attempt, guest_rate_limited, record_guest,
)
from app.services.rotation_strategy import RotationStrategy, RotationConfig, ROT_BOT_ID, STRATEGY_DESC
from app.services.impulse_strategy import ImpulseStrategy, ImpulseConfig, IMP_BOT_ID, STRATEGY_DESC as IMPULSE_DESC, STRATEGY_NAME as IMPULSE_NAME, STRATEGY_VERSION as IMPULSE_VERSION
from app.services.validation_strategy import ValidationStrategy, make_validation_config, VAL_BOT_ID
from app.services.telegram_notifier import TelegramNotifier
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

trade_log: list = []
rotation: Optional[RotationStrategy] = None
impulse: Optional[ImpulseStrategy] = None
validation: Optional[ValidationStrategy] = None
telegram = TelegramNotifier()

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
    except Exception:
        entry["c"] = "ERR"
    return response


@app.get("/api/debug/server-hits")
async def debug_server_hits():
    """Return the most recent server-side API hits (for Mini App diagnostics)."""
    return {"hits": _SERVER_HITS[-80:]}


@app.on_event("startup")
async def startup():
    try:
        print("[startup] 1/6 DB init ...", flush=True)
        await db.init()
        await telegram.load_from_db(db)
        print("[startup] 2/6 OKX client init ...", flush=True)
        if _env_key and _env_secret and _env_pass:
            await client_manager.init_client(_env_key, _env_secret, _env_pass, _env_demo)
        print("[startup] 3/6 Migration check ...", flush=True)
        # One-time cleanup: check if any old momentum data exists, wipe it all.
        # Checks trades table for old bot_id - most reliable signal.
        needs_cleanup = False
        try:
            if db._conn:
                row = await db._fetchone(
                    "SELECT 1 FROM trades WHERE bot_id = ? LIMIT 1", (MOM_BOT_ID,))
                if row:
                    needs_cleanup = True
            elif db._pool:
                import asyncpg
                async with db._pool.acquire() as conn:
                    row = await conn.fetchrow(
                        "SELECT 1 FROM trades WHERE bot_id = $1 LIMIT 1", MOM_BOT_ID)
                    if row:
                        needs_cleanup = True
        except Exception:
            pass  # table might not exist yet on very first run
        if needs_cleanup:
            print("[startup]   Old momentum data found - one-time cleanup ...", flush=True)
            for table in ["trades", "signals", "positions", "performance_metrics", "bots"]:
                try:
                    if db._conn:
                        await db._execute(f"DELETE FROM {table}")
                    elif db._pool:
                        async with db._pool.acquire() as conn:
                            await conn.execute(f"DELETE FROM {table}")
                except Exception as e:
                    print(f"[startup]   clear {table}: {e}", flush=True)
            print("[startup]   Clean slate ready.", flush=True)
        print("[startup] 4/6 Rotation auto-start ...", flush=True)
        if _env_key and _env_secret and _env_pass:
            rot_config = RotationConfig(
                symbols=["BTC", "ETH", "BNB", "XRP", "SOL", "DOGE", "ADA", "TRX", "AVAX", "LTC"],
                capital=10000.0,
                top_k=2,
                roc_period=14,
                ema_fast=20,
                ema_slow=50,
                atr_period=14,
                adx_min=29.0,
                min_roc=4.5,
                sma_long=200,
                min_hold_days=11,
                max_leverage=2.0,
                risk_per_trade=0.14,
                allocation_pct=1.0,
                atr_stop_mult=2.7,
                trail_atr_mult=0.2,
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
            await rotation.start()
        print("[startup] 4/6 Impulse 1D auto-start ...", flush=True)
        if _env_key and _env_secret and _env_pass:
            imp_config = ImpulseConfig(
                symbols=["BTC", "ETH", "BNB", "XRP", "SOL", "DOGE", "ADA", "TRX", "AVAX", "LTC"],
                capital=10000.0,
                top_k=4,
                entry_roc=4.0,
                max_adds=2,
                risk_per_trade=0.10,
                sl_atr_mult=5.0,
                sl_atr_mult_short=5.0,
                trail_atr_mult=8.0,
                trail_atr_mult_short=8.0,
                cooldown_bars=5,
                tp1_atr=2.0,
                tp1_frac=0.3,
                tp2_atr=6.0,
                tp2_frac=0.3,
                max_hold_bars=30,
                max_leverage=3.0,
                poll_interval_sec=300,
                auto_execute=True,
            )
            imp = ImpulseStrategy(config=imp_config, client_manager=client_manager, db=db,
                                  notifier=telegram)
            global impulse
            impulse = imp
            await impulse.start()
        print("[startup] 4/6 Validation (demo) auto-start ...", flush=True)
        # Валидатор исполнительного механизма на демо-счёте. Ослабленные фильтры
        # принудительно открывают сделки. Отключается env VALIDATION_BOT=0.
        if os.getenv("VALIDATION_BOT", "1").lower() not in ("0", "false", "no"):
            if _env_key and _env_secret and _env_pass:
                val_config = make_validation_config(
                    capital=float(os.getenv("VALIDATION_CAPITAL", "300")),
                    top_k=int(os.getenv("VALIDATION_TOP_K", "1")),
                    min_roc=float(os.getenv("VALIDATION_MIN_ROC", "1.5")),
                    adx_min=float(os.getenv("VALIDATION_ADX_MIN", "18")),
                    auto_execute=os.getenv("VALIDATION_AUTO_EXECUTE", "1") != "0",
                )
                v = ValidationStrategy(config=val_config, client_manager=client_manager, db=db,
                                       notifier=telegram)
                global validation
                validation = v
                await validation.start()
        print("[startup] 5/6 Done ...", flush=True)
    except Exception as e:
        print(f"[startup] ERROR: {e}", flush=True)
        raise


@app.on_event("shutdown")
async def shutdown():
    if rotation and rotation._running:
        await rotation.stop()
    if impulse and impulse._running:
        await impulse.stop()
    if validation and validation._running:
        await validation.stop()
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


# ── Auth helpers ──

def get_token(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return ""


# ── Access control ──

PUBLIC_API_PATHS = {
    "/api/health",
    "/api/auth/login",
    "/api/auth/guest",
    "/api/auth/status",
    "/api/auth/logout",
    "/api/auth/telegram",
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
    "/api/db/reset-all",
    "/api/db/positions",
    "/api/telegram/status",
    "/api/telegram/config",
    "/api/telegram/test",
    "/api/telegram/simulate",
    "/api/telegram/menu",
    "/api/analysis/log",
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
    return {
        "authenticated": valid,
        "role": "admin" if admin else ("guest" if valid else "none"),
        "has_password": bool(PASSWORD),
    }


@app.post("/api/auth/telegram")
async def auth_telegram(data: dict):
    """Authenticate a Telegram Mini App user via WebApp initData.

    The initData signature is verified with the bot token; only the chat
    matching TELEGRAM_CHAT_ID is granted an admin session — no password needed.
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
    logger.info("mini auth: signature OK, user.id=%s (%s), chat_id=%s",
                uid, user.get("username"), telegram.chat_id)
    if not telegram.chat_id or uid != telegram.chat_id:
        logger.warning("mini auth: user %s != allowed chat %s -> 403", uid, telegram.chat_id)
        raise HTTPException(status_code=403, detail="Telegram user not authorized")
    token = grant_admin()
    logger.info("mini auth: admin token granted for user %s", uid)
    return {
        "token": token,
        "role": "admin",
        "user": {
            "id": user.get("id"),
            "username": user.get("username"),
            "first_name": user.get("first_name"),
        },
    }


@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    token = get_token(request)
    return logout(token)


# ── Health ──

@app.get("/api/health")
async def health():
    client = client_manager.get_client()
    connected = client is not None
    return {"status": "ok", "connected": connected, "demo": _env_demo}


# ── Credentials ──

@app.get("/api/credentials/status")
async def credentials_status():
    configured = bool(_env_key and _env_secret and _env_pass)
    return {"configured": configured, "demo": _env_demo}


@app.post("/api/credentials/test")
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


@app.post("/api/credentials/init")
async def credentials_init(data: dict):
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
    return {"message": "Credentials configured", "demo": demo}


# ── Portfolio ──

@app.get("/api/portfolio")
async def get_portfolio():
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
    return {"totalEqUsd": total_eq, "details": details}


# ── Positions ──

def _tag_position_bot(inst_id: str, pos_side: str) -> str:
    """Determine which bot owns an OKX position by checking running bots' in-memory positions."""
    # Normalize pos_side for matching
    norm_side = pos_side.lower() if pos_side else ""
    # Check Impulse bot positions
    if impulse and impulse._running and impulse._positions:
        for coin, pos in impulse._positions.items():
            if pos.inst_id == inst_id and pos.side == norm_side:
                return "Impulse 1D"
    # Check Rotation bot positions
    if rotation and rotation._running and rotation._positions:
        for coin, pos in rotation._positions.items():
            if pos.inst_id == inst_id and pos.side == norm_side:
                return "Momentum"
    # Fallback: check trade logs for recent open entry of this instrument
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
    return ""


def _tag_trade_bot(trade: dict) -> str:
    """Tag a paired trade with bot name. Works for both open and closed trades."""
    inst_id = trade.get("inst_id", "") or trade.get("symbol", "")
    pos_side = trade.get("pos_side", "")
    if trade.get("reason") == "open":
        return _tag_position_bot(inst_id, pos_side)
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
    # Fallback: DB bot_id stored for this trade
    return _db_bot_name(trade.get("bot_id", ""))


def _db_bot_name(bot_id: str) -> str:
    """Map DB bot_id -> UI bot name."""
    if bot_id in ("momentum_strategy", "rotation_strategy", MOM_BOT_ID, ROT_BOT_ID):
        return "Momentum"
    if bot_id in ("impulse_strategy", IMP_BOT_ID):
        return "Impulse 1D"
    if bot_id == VAL_BOT_ID:
        return "Validation"
    return ""


@app.get("/api/positions")
async def get_positions(inst_type: str = "SWAP"):
    result = await _okx_call(lambda c: c.get_positions(inst_type))
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result.get("message", ""))
    # Tag each position with bot name
    tagged = []
    for p in result.get("data", []):
        p["bot"] = _tag_position_bot(p.get("instId", ""), p.get("posSide", "net"))
        tagged.append(p)
    return {"positions": tagged}


@app.post("/api/positions/close")
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

@app.get("/api/market/ticker")
async def get_ticker(inst_id: str = "BTC-USDT"):
    result = await _okx_call(lambda c: c.get_ticker(inst_id))
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result.get("message", ""))
    return result.get("data", [{}])[0] if result.get("data") else {}


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

@app.post("/api/trade/order")
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
    return rotation.get_status()


@app.post("/api/momentum/start")
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


@app.post("/api/momentum/stop")
async def momentum_stop():
    """Stop Rotation strategy (Dashboard calls this endpoint)."""
    global rotation
    if not rotation:
        return {"message": "Bot not running"}
    await rotation.stop()
    return {"message": "Bot stopped"}


@app.post("/api/momentum/config")
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
        # Exclude fills that belong to the demo validation bot (its ordIds are
        # persisted with bot_id=validation_strategy). Otherwise its demo trades
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
        # Also drop fills whose client order id marks them as the demo validator
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


@app.post("/api/rotation/start")
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


@app.post("/api/rotation/stop")
async def rotation_stop():
    global rotation
    if not rotation:
        return {"message": "Rotation not running"}
    await rotation.stop()
    return {"message": "Rotation stopped"}


@app.post("/api/rotation/reset")
async def rotation_reset():
    """Reset all trades, signals, positions, PNL for rotation strategy."""
    global rotation
    # Stop if running
    if rotation and rotation._running:
        await rotation.stop()
    # Delete all data for rotation bot
    if db._conn:
        for table in ["trades", "signals", "positions", "performance_metrics"]:
            try:
                await db._execute(f"DELETE FROM {table} WHERE bot_id = ?", (ROT_BOT_ID,))
            except Exception as e:
                print(f"[reset] Error clearing {table}: {e}", flush=True)
        try:
            await db._execute("DELETE FROM bots WHERE id = ?", (ROT_BOT_ID,))
        except Exception as e:
            print(f"[reset] Error clearing bots: {e}", flush=True)
    elif db._pool:
        import asyncpg
        async with db._pool.acquire() as conn:
            for table in ["trades", "signals", "positions", "performance_metrics"]:
                await conn.execute(f"DELETE FROM {table} WHERE bot_id = $1", ROT_BOT_ID)
            await conn.execute("DELETE FROM bots WHERE id = $1", ROT_BOT_ID)
    # Reset in-memory
    rotation = None
    return {"message": "Rotation reset complete - PNL = 0"}


@app.post("/api/db/reset-all")
async def db_reset_all():
    """Nuclear reset: clear ALL bot data (trades, signals, positions, metrics, bots)."""
    global rotation
    if rotation and rotation._running:
        await rotation.stop()
    rotation = None
    for table in ["trades", "signals", "positions", "performance_metrics", "bots"]:
        try:
            if db._conn:
                await db._execute(f"DELETE FROM {table}")
            elif db._pool:
                async with db._pool.acquire() as conn:
                    await conn.execute(f"DELETE FROM {table}")
        except Exception as e:
            print(f"[reset-all] Error clearing {table}: {e}", flush=True)
    return {"message": "All data reset - clean slate"}


@app.get("/api/rotation/trades")
async def rotation_trades(limit: int = 50):
    if not rotation:
        return {"trades": []}
    trades = [dict(t) for t in rotation._trade_log[-limit:]]
    for t in trades:
        t.setdefault("bot", "Momentum")
    return {"trades": trades}


@app.get("/api/rotation/indicators")
async def rotation_indicators():
    if not rotation:
        return {"indicators": {}}
    return {"indicators": rotation._latest_indicators}


@app.post("/api/rotation/config")
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
    return impulse.get_status()


@app.post("/api/impulse/start")
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


@app.post("/api/impulse/stop")
async def impulse_stop():
    """Stop Impulse 1D strategy."""
    global impulse
    if not impulse:
        return {"message": "Impulse not running"}
    await impulse.stop()
    return {"message": "Impulse stopped"}


@app.post("/api/impulse/config")
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


@app.post("/api/impulse/reset")
async def impulse_reset():
    """Reset all trades, signals, positions, PNL for the impulse strategy."""
    global impulse
    if impulse and impulse._running:
        await impulse.stop()
    if db._conn:
        for table in ["trades", "signals", "positions", "performance_metrics"]:
            try:
                await db._execute(f"DELETE FROM {table} WHERE bot_id = ?", (IMP_BOT_ID,))
            except Exception as e:
                print(f"[impulse/reset] Error clearing {table}: {e}", flush=True)
        try:
            await db._execute("DELETE FROM bots WHERE id = ?", (IMP_BOT_ID,))
        except Exception as e:
            print(f"[impulse/reset] Error clearing bots: {e}", flush=True)
    elif db._pool:
        import asyncpg
        async with db._pool.acquire() as conn:
            for table in ["trades", "signals", "positions", "performance_metrics"]:
                await conn.execute(f"DELETE FROM {table} WHERE bot_id = $1", IMP_BOT_ID)
            await conn.execute("DELETE FROM bots WHERE id = $1", IMP_BOT_ID)
    impulse = None
    return {"message": "Impulse reset complete - PNL = 0"}


# ══════════════════════════════════════════════════════════════
# VALIDATION STRATEGY ENDPOINTS (демо-проверка исполнения)
# ══════════════════════════════════════════════════════════════

@app.get("/api/validation/status")
async def validation_status():
    if not validation:
        return {"running": False, "strategy": "momentum_validation",
                "equity": 0, "open_positions": [], "total_trades": 0,
                "total_pnl": 0, "config": {}}
    return validation.get_status()


@app.post("/api/validation/start")
async def validation_start(data: dict = None):
    """Start the demo validation bot (relaxed filters → forces trades)."""
    global validation
    if validation and validation._running:
        return {"message": "Validation already running", **validation.get_status()}
    d = data or {}
    cfg = make_validation_config(
        capital=float(d.get("capital", 300.0)),
        top_k=int(d.get("top_k", 1)),
        min_roc=float(d.get("min_roc", 1.5)),
        adx_min=float(d.get("adx_min", 18.0)),
        min_hold_days=int(d.get("min_hold_days", 1)),
        max_leverage=float(d.get("max_leverage", 2.0)),
        risk_per_trade=float(d.get("risk_per_trade", 0.14)),
        allocation_pct=float(d.get("allocation_pct", 0.15)),
        poll_interval_sec=int(d.get("poll_interval_sec", 300)),
        auto_execute=d.get("auto_execute", True),
    )
    validation = ValidationStrategy(config=cfg, client_manager=client_manager, db=db,
                                    notifier=telegram)
    await validation.start()
    return {"message": "Validation started", **validation.get_status()}


@app.post("/api/validation/stop")
async def validation_stop():
    global validation
    if not validation:
        return {"message": "Validation not running"}
    await validation.stop()
    return {"message": "Validation stopped"}


@app.post("/api/validation/reset")
async def validation_reset():
    """Reset all trades, signals, positions, PNL for the validation bot."""
    global validation
    if validation and validation._running:
        await validation.stop()
    if db._conn:
        for table in ["trades", "signals", "positions", "performance_metrics"]:
            try:
                await db._execute(f"DELETE FROM {table} WHERE bot_id = ?", (VAL_BOT_ID,))
            except Exception as e:
                print(f"[validation/reset] Error clearing {table}: {e}", flush=True)
        try:
            await db._execute("DELETE FROM bots WHERE id = ?", (VAL_BOT_ID,))
        except Exception as e:
            print(f"[validation/reset] Error clearing bots: {e}", flush=True)
    else:
        import asyncpg
        async with db._pool.acquire() as conn:
            for table in ["trades", "signals", "positions", "performance_metrics"]:
                await conn.execute(f"DELETE FROM {table} WHERE bot_id = $1", VAL_BOT_ID)
            await conn.execute("DELETE FROM bots WHERE id = $1", VAL_BOT_ID)
    validation = None
    return {"message": "Validation reset complete - PNL = 0"}


@app.get("/api/validation/trades")
async def validation_trades(limit: int = 50):
    if not validation:
        return {"trades": []}
    trades = [dict(t) for t in validation._trade_log[-limit:]]
    for t in trades:
        t.setdefault("bot", "Validation")
    return {"trades": trades}


@app.get("/api/validation/indicators")
async def validation_indicators():
    if not validation:
        return {"indicators": {}}
    return {"indicators": validation._latest_indicators}


@app.post("/api/validation/config")
async def validation_update_config(data: dict = None):
    global validation
    if not validation:
        return {"message": "Validation not running"}
    if not data:
        return {"message": "No config provided"}
    cfg = validation.config
    for key in ("symbols", "top_k", "roc_period", "ema_fast", "ema_slow",
                "atr_period", "atr_stop_mult", "trail_pct", "breakeven_pct",
                "adx_min", "min_roc", "min_hold_days", "max_leverage",
                "risk_per_trade", "allocation_pct", "poll_interval_sec",
                "auto_execute", "capital"):
        if key in data:
            setattr(cfg, key, data[key])
    return {"message": "Config updated", "config": asdict(cfg)}


# ══════════════════════════════════════════════════════════════
# TELEGRAM NOTIFICATIONS
# ══════════════════════════════════════════════════════════════

@app.get("/api/telegram/status")
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


@app.post("/api/telegram/config")
async def telegram_config(data: dict = None):
    """Set/update Telegram bot token and chat id at runtime."""
    d = data or {}
    telegram.configure(token=d.get("token", ""), chat_id=d.get("chat_id", ""))
    # Persist to DB so the config survives restarts/redeploys.
    try:
        if telegram.token:
            await db.set_setting("TELEGRAM_BOT_TOKEN", telegram.token)
        if telegram.chat_id:
            await db.set_setting("TELEGRAM_CHAT_ID", telegram.chat_id)
    except Exception as e:
        print(f"[telegram/config] DB persist error: {e}", flush=True)
    return await telegram_status()


@app.post("/api/telegram/test")
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


@app.post("/api/telegram/simulate")
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
        size=0.03, leverage=3.0,
    )
    msg_partial = notifier.partial_msg(
        coin="BTC", side="long", entry=open_px, exit_px=round(open_px * 1.05, 2),
        pnl=76.50, closed_sz=0.015, remaining_sz=0.015,
    )
    msg_close = notifier.close_msg(
        coin="BTC", side="long", entry=open_px, exit_px=round(open_px * 1.09, 2),
        pnl=201.75, reason="trail_stop",
    )
    msg_add = notifier.add_msg(
        coin="ETH", side="long", price=3450.00, size=0.4, total=1.2,
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


@app.post("/api/telegram/menu")
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
    effective_limit = min(limit, 300)
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
    """Convert OKX millisecond timestamp to ISO string."""
    if not ts_ms:
        return ""
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000).isoformat()
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
    Priority: 1) pnl field, 2) posSide+side, 3) direction tracking."""
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


async def _fetch_all_trade_bills(limit_per_page: int = 100) -> list:
    """Fetch OKX account bills of trade type (type=2) for the whole available
    history: recent 7 days via /account/bills, older up to 3 months via
    /account/bills-archive, paginated backwards by billId."""
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
                resp = await _okx_call(lambda c, e=fn, k=kw: e(c, **k))
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
    return bills


@app.get("/api/pnl")
async def get_pnl():
    """PNL for Dashboard metric cards. Realized from OKX account bills
    (sum of trade-type bill `pnl`, covering up to 3 months via archive),
    unrealized from OKX positions."""
    from datetime import datetime as dt, timezone as tz, timedelta as td

    realized_1d = 0.0
    realized_7d = 0.0
    realized_30d = 0.0
    realized_week = 0.0
    total_realized = 0.0
    total_fees = 0.0
    source = "none"

    # ── 1. Primary: OKX account bills (trade type) — matches exchange exactly ──
    try:
        bills = await _fetch_all_trade_bills()
        if bills:
            source = "okx_bills"
            now = dt.now(tz.utc)
            week_start = (now - td(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
            for b in bills:
                try:
                    b_pnl = float(b.get("pnl") or 0)
                except (TypeError, ValueError):
                    b_pnl = 0.0
                try:
                    b_fee = abs(float(b.get("fee") or 0))
                except (TypeError, ValueError):
                    b_fee = 0.0
                total_realized += b_pnl
                total_fees += b_fee
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
            print(f"[pnl] OKX bills: total={total_realized:.2f} 1d={realized_1d:.2f} "
                  f"7d={realized_7d:.2f} 30d={realized_30d:.2f} week={realized_week:.2f} "
                  f"fees={total_fees:.2f} bills={len(bills)}", flush=True)
    except Exception as e:
        import traceback
        print(f"[pnl] OKX bills error: {e}", flush=True)
        traceback.print_exc()

    # ── 2. Fallback: OKX fills pairing (if bills unavailable) ──
    if source == "none":
        try:
            global _fills_cache_ts
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
                    fee_str = t.get("fee", "0")
                    try:
                        total_fees += abs(float(fee_str))
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
                print(f"[pnl] OKX fills: total={total_realized:.2f} 1d={realized_1d:.2f} "
                      f"7d={realized_7d:.2f} 30d={realized_30d:.2f} week={realized_week:.2f} "
                      f"fees={total_fees:.2f}", flush=True)
        except Exception as e:
            import traceback
            print(f"[pnl] OKX fills error: {e}", flush=True)
            traceback.print_exc()

    # ── 2. Fallback: in-memory from running bots ──
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
                unrealized += float(p.get("upl", 0) or 0)
    except Exception:
        pass

    return {
        "total": round(total_realized, 2),
        "1d": round(realized_1d, 2),
        "7d": round(realized_7d, 2),
        "30d": round(realized_30d, 2),
        "week": round(realized_week, 2),
        "unrealized": round(unrealized, 2),
        "source": source,
        "fees": round(total_fees, 2),
    }


# ── Trades ──

@app.get("/api/trades")
async def get_all_trades(limit: int = 100):
    """Trades from Rotation strategy (not OKX fills)."""
    if rotation:
        return {"trades": rotation._trade_log[-limit:]}
    return {"trades": []}


@app.get("/api/trades/paired")
async def get_paired_trades(limit: int = 500, begin: str = None, end: str = None):
    """Paired entry+exit trades — in-memory from running bot, fallback to DB."""
    # 1. Try in-memory rotation trade log
    if rotation and len(rotation._trade_log) > 0:
        trades = rotation._trade_log
        paired = []
        entry_map = {}
        for t in trades:
            coin = t.get("coin", "")
            if t.get("reason") == "open" or t.get("pnl", 0) == 0:
                entry_map[coin] = t
            elif t.get("pnl", 0) != 0:
                entry = entry_map.pop(coin, None)
                paired.append({
                    "time": t.get("time", ""),
                    "entry_time": entry.get("time", "") if entry else t.get("time", ""),
                    "exit_time": t.get("time", ""),
                    "side": "buy" if t.get("pos_side") == "long" else "sell",
                    "symbol": t.get("symbol", ""),
                    "inst_id": t.get("symbol", ""),
                    "entry": entry.get("entry_price") or entry.get("entry", 0) if entry else 0,
                    "entry_px": entry.get("entry_price") or entry.get("entry", 0) if entry else 0,
                    "exit_price": t.get("exit_price", 0),
                    "exit_px": t.get("exit_price", 0),
                    "pnl": t.get("pnl", 0),
                    "reason": t.get("reason", ""),
                    "pos_side": t.get("pos_side", ""),
                    "signal_id": t.get("signal_id", ""),
                    "bot": "Momentum",
                })
        for coin, entry in entry_map.items():
            paired.append({
                "time": entry.get("time", ""),
                "entry_time": entry.get("time", ""),
                "exit_time": None,
                "side": "buy" if entry.get("pos_side") == "long" else "sell",
                "symbol": entry.get("symbol", ""),
                "inst_id": entry.get("symbol", ""),
                "entry": entry.get("entry_price") or entry.get("entry", 0),
                "entry_px": entry.get("entry_price") or entry.get("entry", 0),
                "exit_price": None,
                "exit_px": None,
                "pnl": None,
                "reason": "open",
                "pos_side": entry.get("pos_side", ""),
                "signal_id": entry.get("signal_id", ""),
                "bot": "Momentum",
            })
        paired = paired[-limit:]
        return {"trades": paired}

    # 2. Fallback: fetch real fills from OKX exchange
    try:
        global _fills_cache_ts
        _fills_cache_ts = 0
        raw_fills = await _fetch_okx_fills(limit=300)
        # Exclude fills that belong to the demo validation bot (its ordIds are
        # persisted with bot_id=validation_strategy). Otherwise its demo trades
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
        # Also drop fills whose client order id marks them as the demo validator
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
        db_trades = await db.get_paired_trades(limit=limit, begin=begin, end=end, bot_ids=[ROT_BOT_ID, MOM_BOT_ID])
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


@app.get("/api/debug/trades-db")
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

@app.get("/api/debug/fills")
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


@app.get("/api/analysis")
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


@app.get("/api/db/positions")
async def get_db_positions():
    positions = await db.get_all_positions()
    return {"positions": positions}


# ── Analysis log download ──

@app.get("/api/analysis/log")
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
        return FileResponse(
            str(STATIC_DIR / "index.html"),
            headers={"Cache-Control": "no-store"},
        )


# ── Mini App client logs ──

_MINI_LOG_RING = []


@app.post("/api/debug/mini-log")
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


@app.get("/api/debug/mini-log")
async def mini_log_read():
    """Return the most recent Mini App client logs."""
    return {"count": len(_MINI_LOG_RING), "logs": _MINI_LOG_RING[-150:]}


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("BACKEND_PORT", "8000"))
    host = os.getenv("BACKEND_HOST", "0.0.0.0")
    uvicorn.run("app.main:app", host=host, port=port, reload=True)
