import asyncio
import json
import os
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional
from dataclasses import asdict

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response

from app.services.okx_client import OKXClientManager
from app.database import db
from app.services.auth import login, guest, validate, logout, is_admin, PASSWORD, check_rate_limit, record_attempt
from app.services.momentum_strategy import MomentumStrategy, MomentumConfig, MOM_BOT_ID
from app.services.rotation_strategy import RotationStrategy, RotationConfig, ROT_BOT_ID, STRATEGY_DESC

load_dotenv()

app = FastAPI(title="OKX Trading Bot", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
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
momentum: Optional[MomentumStrategy] = None
rotation: Optional[RotationStrategy] = None


@app.on_event("startup")
async def startup():
    try:
        print("[startup] 1/4 DB init ...", flush=True)
        await db.init()
        print("[startup] 2/4 OKX client init ...", flush=True)
        if _env_key and _env_secret and _env_pass:
            await client_manager.init_client(_env_key, _env_secret, _env_pass, _env_demo)
        print("[startup] 3/4 Migration check ...", flush=True)
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
        print("[startup] 4/4 Rotation auto-start ...", flush=True)
        if _env_key and _env_secret and _env_pass:
            rot_config = RotationConfig(
                symbols=["BTC", "ETH", "BNB", "SOL"],
                capital=10000.0,
                top_k=2,
                roc_period=14,
                ema_fast=20,
                ema_slow=50,
                atr_period=14,
                breakeven_pct=0.03,
                adx_min=18.0,
                min_hold_days=3,
                max_leverage=3.0,
                risk_per_trade=0.02,
                poll_interval_sec=300,
                auto_execute=True,
            )
            r = RotationStrategy(config=rot_config, client_manager=client_manager, db=db)
            global rotation
            rotation = r
            await rotation.start()
        print("[startup] Done - server ready", flush=True)
    except Exception as e:
        print(f"[startup] ERROR: {e}", flush=True)
        raise


@app.on_event("shutdown")
async def shutdown():
    if rotation and rotation._running:
        await rotation.stop()
    if momentum and momentum._running:
        await momentum.stop()
    await db.close()


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


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.method in ("POST", "PUT", "DELETE") and request.url.path.startswith("/api/"):
        skip = ("/api/auth/login", "/api/auth/guest", "/api/auth/logout")
        if request.url.path not in skip:
            token = get_token(request)
            if not validate(token):
                return JSONResponse(status_code=401, content={"detail": "Unauthorized"})
    return await call_next(request)


# ── Auth ──

@app.post("/api/auth/login")
async def auth_login(data: dict):
    token = login(data.get("password", ""))
    if token:
        return {"token": token, "role": "admin"}
    raise HTTPException(status_code=401, detail="Invalid password")


@app.post("/api/auth/guest")
async def auth_guest():
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

@app.get("/api/positions")
async def get_positions(inst_type: str = "SWAP"):
    result = await _okx_call(lambda c: c.get_positions(inst_type))
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result.get("message", ""))
    return {"positions": result.get("data", [])}


@app.post("/api/positions/close")
async def close_position(data: dict):
    client = client_manager.get_client()
    if not client:
        raise HTTPException(status_code=400, detail="API not configured")

    inst_id = data.get("instId")
    pos_side = data.get("posSide", "net")
    mgn_mode = data.get("mgnMode", "cross")

    if not inst_id:
        raise HTTPException(status_code=400, detail="instId required")

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
        return {"message": "Rotation already running"}
    d = data or {}
    config = RotationConfig(
        symbols=d.get("symbols", ["BTC", "ETH", "BNB", "SOL"]),
        capital=d.get("capital", 10000.0),
        top_k=d.get("top_k", 2),
        max_leverage=d.get("leverage", 3.0),
        auto_execute=d.get("auto_execute", True),
        poll_interval_sec=d.get("poll_interval_sec", 300),
    )
    rotation = RotationStrategy(config=config, client_manager=client_manager, db=db)
    await rotation.start()
    return {"message": "Momentum Rotation started", "config": asdict(config)}


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
    global momentum
    if not momentum:
        return {"message": "Momentum not running"}
    if not data:
        return {"message": "No config provided"}
    cfg = momentum.config
    if "symbols" in data:
        cfg.symbols = data["symbols"]
    for key in ("risk_per_trade", "max_positions", "auto_execute", "poll_interval_sec",
                "roc_fast", "roc_slow", "ema_fast", "ema_slow", "atr_stop_mult",
                "trail_pct", "adx_threshold", "mom_threshold",
                "breakeven_pct", "tp1_pct", "tp1_frac",
                "sl1_pct", "sl1_frac",
                "trend_adx_min", "range_adx_max", "range_sl_mult",
                "max_budget", "max_notional_per_position_pct", "max_total_notional_pct",
                "signal_risk_min", "signal_risk_max", "signal_adx_weak", "signal_adx_strong"):
        if key in data:
            setattr(cfg, key, data[key])
    return {"message": "Config updated", "config": cfg}


@app.get("/api/momentum/trades")
async def momentum_trades(limit: int = 20):
    """Trade history from Rotation strategy, formatted for Dashboard."""
    if not rotation:
        return {"trades": []}
    # Transform rotation trade_log entries to match Dashboard's expected format
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
            # For open trades — fields the allTrades useMemo checks
            "entry_time": t.get("time", ""),
            "exit_time": t.get("time", "") if not is_open else None,
        })
    return {"trades": trades}


@app.get("/api/momentum/indicators")
async def momentum_indicators():
    """Return latest computed indicators per coin (debug)."""
    if rotation:
        return {"indicators": rotation._latest_indicators}
    if not momentum:
        return {"indicators": {}}
    return {"indicators": getattr(momentum, "_latest_indicators", {})}

@app.get("/api/momentum/chart-data")
async def momentum_chart_data():
    """Return trade markers + entry/stop/be/tp1 lines for chart overlay."""
    global momentum
    markers = []
    trade_lines = []

    def ts_or_none(t):
        if not t:
            return None
        try:
            return int(datetime.fromisoformat(t).timestamp())
        except Exception:
            return None

    def be_price(entry, pct=0.005):
        return round(entry * (1 + pct), 2)

    def tp1_price(entry, pct=0.02):
        return round(entry * (1 + pct), 2)

    if momentum:
        cfg = momentum.config
        be_pct = cfg.breakeven_pct if hasattr(cfg, 'breakeven_pct') else 0.005
        tp1_pct = cfg.tp1_pct if hasattr(cfg, 'tp1_pct') else 0.02

        buys: dict[str, dict] = {}
        for t in momentum._trade_log:
            side = t.get("side", "")
            symbol = t.get("symbol", "")
            time_str = t.get("time", "")
            if not time_str or not symbol:
                continue
            t_ts = ts_or_none(time_str)
            if not t_ts:
                continue

            if side == "buy":
                markers.append({
                    "time": t_ts, "side": "buy", "symbol": symbol,
                    "entry": t.get("entry", 0), "stop": t.get("stop", 0),
                })
                buys.setdefault(symbol, []).append({
                    "ts": t_ts, "entry": t.get("entry", 0),
                    "stop": t.get("stop", 0),
                })
            elif side == "sell":
                markers.append({
                    "time": t_ts, "side": "sell", "symbol": symbol,
                    "exit_price": t.get("exit_price", 0),
                    "entry_price": t.get("entry_price", 0),
                    "pnl": t.get("pnl", 0), "reason": t.get("reason", ""),
                })

        for coin, pos in momentum._positions.items():
            entry = pos.entry_price
            trade_lines.append({
                "symbol": pos.symbol, "inst_id": pos.inst_id,
                "entry": entry, "stop": pos.stop_price,
                "breakeven": be_price(entry, be_pct),
                "tp1": tp1_price(entry, tp1_pct),
                "peak": pos.peak_price,
                "stage": pos.stage, "size": pos.size_remaining, "original_size": pos.size,
            })

        # Past closed trades: skip buys that match current open positions
        open_keys = set()
        for coin, pos in momentum._positions.items():
            open_keys.add((pos.symbol, round(pos.entry_price, 2)))
        for symbol, ent in buys.items():
            for b in ent:
                entry = b["entry"]
                sym_short = symbol.replace("-USDT-SWAP", "").replace("-USD-SWAP", "")
                key = (sym_short, round(entry, 2))
                if key in open_keys:
                    continue
                trade_lines.append({
                    "symbol": symbol,
                    "entry": entry,
                    "stop": b["stop"],
                    "breakeven": be_price(entry, be_pct),
                    "tp1": tp1_price(entry, tp1_pct),
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
        symbols=d.get("symbols", ["BTC", "ETH", "BNB", "SOL"]),
        capital=d.get("capital", 10000.0),
        top_k=d.get("top_k", 2),
        roc_period=d.get("roc_period", 14),
        ema_fast=d.get("ema_fast", 20),
        ema_slow=d.get("ema_slow", 50),
        atr_period=d.get("atr_period", 14),
        breakeven_pct=d.get("breakeven_pct", 0.03),
        adx_min=d.get("adx_min", 18.0),
        min_hold_days=d.get("min_hold_days", 3),
        max_leverage=d.get("leverage", 3.0),
        risk_per_trade=d.get("risk_per_trade", 0.02),
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
    global rotation, momentum
    if rotation and rotation._running:
        await rotation.stop()
    if momentum and momentum._running:
        await momentum.stop()
    rotation = None
    momentum = None
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
    return {"trades": rotation._trade_log[-limit:]}


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


import time as _time

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


@app.get("/api/pnl")
async def get_pnl():
    """PNL for Dashboard metric cards. Realized from _trade_log, unrealized from OKX."""
    total_realized = 0.0
    realized_1d = 0.0
    realized_7d = 0.0
    realized_30d = 0.0
    if rotation and rotation._trade_log:
        from datetime import datetime as dt, timezone as tz
        now = dt.now(tz.utc)
        for t in rotation._trade_log:
            pnl = t.get("pnl", 0)
            if not pnl:
                continue
            total_realized += pnl
            try:
                t_time = dt.fromisoformat(t["time"])
                age = (now - t_time).total_seconds()
                if age <= 86400:
                    realized_1d += pnl
                if age <= 604800:
                    realized_7d += pnl
                if age <= 2592000:
                    realized_30d += pnl
            except Exception:
                realized_30d += pnl
    # Unrealized PNL — from OKX positions (same source as positions table)
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
        "unrealized": round(unrealized, 2),
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
    """Paired entry+exit trades from Rotation strategy, formatted for Dashboard."""
    if not rotation:
        return {"trades": []}
    trades = rotation._trade_log
    # Pair entry (pnl=0 or reason=open) with next exit for same coin
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
            })
    # Also include unmatched entries as open trades
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
        })
    paired = paired[-limit:]
    return {"trades": paired}


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
        return FileResponse(str(STATIC_DIR / "index.html"))

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        if full_path.startswith("api/") or full_path.startswith("docs") or full_path.startswith("openapi"):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        return FileResponse(str(STATIC_DIR / "index.html"))


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("BACKEND_PORT", "8000"))
    host = os.getenv("BACKEND_HOST", "0.0.0.0")
    uvicorn.run("app.main:app", host=host, port=port, reload=True)
