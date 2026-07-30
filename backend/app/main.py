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


@app.on_event("startup")
async def startup():
    try:
        print("[startup] 1/3 DB init ...", flush=True)
        await db.init()
        print("[startup] 2/3 OKX client init ...", flush=True)
        if _env_key and _env_secret and _env_pass:
            await client_manager.init_client(_env_key, _env_secret, _env_pass, _env_demo)
        print("[startup] 3/3 Momentum auto-start ...", flush=True)
        if _env_key and _env_secret and _env_pass:
            config = MomentumConfig(
                symbols=["BTC", "ETH", "BNB", "SOL"],
                risk_per_trade=0.03,
                max_positions=4,
                leverage=3,
                auto_execute=True,
                poll_interval_sec=60,
                trail_pct=0.015,
                adx_threshold=20.0,
                breakeven_pct=0.003,
                tp1_pct=0.015,
                tp1_frac=0.5,
                trend_adx_min=25.0,
                range_adx_max=18.0,
                range_bb_period=20,
                range_bb_mult=2.0,
                range_rsi_period=14,
                range_rsi_oversold=35.0,
                range_rsi_overbought=65.0,
                range_risk_divisor=2.0,
                range_sl_mult=1.0,
            )
            m = MomentumStrategy(config=config, client_manager=client_manager, db=db)
            global momentum
            momentum = m
            await momentum.start()
        print("[startup] Done — server ready", flush=True)
    except Exception as e:
        print(f"[startup] ERROR: {e}", flush=True)
        raise


@app.on_event("shutdown")
async def shutdown():
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
    if not momentum:
        return {"running": False, "config": None, "equity": 0, "open_positions": [], "total_signals": 0, "total_trades": 0, "recent_signals": [], "recent_trades": []}
    return momentum.get_status()


@app.post("/api/momentum/start")
async def momentum_start(data: dict = None):
    global momentum
    if momentum and momentum._running:
        return {"message": "Momentum already running"}

    d = data or {}
    config = MomentumConfig(
        symbols=d.get("symbols", ["BTC", "ETH", "BNB", "SOL"]),
        risk_per_trade=d.get("risk_per_trade", 0.03),
        max_positions=d.get("max_positions", 4),
        leverage=d.get("leverage", 3),
        auto_execute=d.get("auto_execute", True),
        poll_interval_sec=d.get("poll_interval_sec", 60),
        roc_fast=d.get("roc_fast", 5),
        roc_slow=d.get("roc_slow", 50),
        ema_fast=d.get("ema_fast", 15),
        ema_slow=d.get("ema_slow", 30),
        atr_stop_mult=d.get("atr_stop_mult", 1.5),
        trail_pct=d.get("trail_pct", 0.015),
        adx_threshold=d.get("adx_threshold", 20.0),
        mom_threshold=d.get("mom_threshold", 0.0),
        breakeven_pct=d.get("breakeven_pct", 0.003),
        tp1_pct=d.get("tp1_pct", 0.015),
        tp1_frac=d.get("tp1_frac", 0.5),
        sl1_pct=d.get("sl1_pct", 0.0),
        sl1_frac=d.get("sl1_frac", 0.5),
        trend_adx_min=d.get("trend_adx_min", 25.0),
        range_adx_max=d.get("range_adx_max", 18.0),
        range_sl_mult=d.get("range_sl_mult", 1.0),
    )
    momentum = MomentumStrategy(config=config, client_manager=client_manager, db=db)
    await momentum.start()
    return {"message": "Momentum started", "config": asdict(config)}


@app.post("/api/momentum/stop")
async def momentum_stop():
    global momentum
    if not momentum:
        return {"message": "Momentum not running"}
    await momentum.stop()
    return {"message": "Momentum stopped"}


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
                "trend_adx_min", "range_adx_max", "range_sl_mult"):
        if key in data:
            setattr(cfg, key, data[key])
    return {"message": "Config updated", "config": cfg}


@app.get("/api/momentum/trades")
async def momentum_trades(limit: int = 20):
    """Trade history from OKX fills — source of truth is the exchange."""
    # 1. Fetch raw fills from OKX
    raw_fills = await _fetch_okx_fills(limit=100)
    
    # 2. Pair fills into entry+close trades
    paired = await _pair_fills(raw_fills)
    
    # 3. Merge with live open positions from OKX (not yet in fills as close)
    open_insts = set()
    for t in paired:
        if t.get("reason") == "open":
            open_insts.add(t["inst_id"])
    
    # Also add open positions from momentum bot if running
    if momentum:
        for coin, pos in momentum._positions.items():
            if pos.inst_id not in open_insts:
                paired.append({
                    "time": pos.opened_at,
                    "side": "buy" if pos.side == "long" else "sell",
                    "symbol": pos.inst_id,
                    "size": pos.size,
                    "entry": pos.entry_price,
                    "entry_price": pos.entry_price,
                    "stop": pos.stop_price,
                    "reason": "open",
                    "pos_side": pos.side,
                    "inst_id": pos.inst_id,
                    "source": "okx",
                })
                open_insts.add(pos.inst_id)
    
    # 4. Sort and limit
    paired.sort(key=lambda t: t.get("time", ""), reverse=True)
    paired = paired[-limit:]
    
    return {"trades": paired}


@app.get("/api/momentum/indicators")
async def momentum_indicators():
    """Return latest computed indicators per coin (debug)."""
    global momentum
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


# ── PnL ──

# ── OKX helpers ──

import time as _time

SWAP_INSTRUMENTS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "BNB-USDT-SWAP", "SOL-USDT-SWAP"]

# Simple in-memory cache for OKX fills (updated on each /api/momentum/trades call)
_fills_cache: list[dict] = []
_fills_cache_ts: float = 0
_FILLS_TTL = 30  # seconds


async def _fetch_okx_fills(limit: int = 100) -> list[dict]:
    """Fetch fills-history from OKX for all SWAP instruments. Returns raw OKX fill dicts."""
    global _fills_cache, _fills_cache_ts
    now = _time.time()
    if _fills_cache and (now - _fills_cache_ts) < _FILLS_TTL:
        return _fills_cache

    all_fills = []
    for inst_id in SWAP_INSTRUMENTS:
        result = await _okx_call(lambda c, iid=inst_id: c.get_fills_history(inst_type="SWAP", instId=iid, limit=limit))
        if result.get("error") or not result.get("data"):
            # Fallback to regular fills
            result = await _okx_call(lambda c, iid=inst_id: c.get_fills(inst_id=iid, limit=limit))
        if not result.get("error") and result.get("data"):
            all_fills.extend(result["data"])

    # Sort by timestamp descending (newest first)
    all_fills.sort(key=lambda f: f.get("ts", "0"), reverse=True)
    _fills_cache = all_fills
    _fills_cache_ts = now
    return all_fills


def _ms_to_iso(ts_ms: str) -> str:
    """Convert OKX millisecond timestamp to ISO string."""
    if not ts_ms:
        return ""
    try:
        return datetime.fromtimestamp(int(ts_ms) / 1000).isoformat()
    except (ValueError, OSError, TypeError):
        return ts_ms


def _fill_to_trade(f: dict) -> dict:
    """Convert a single OKX fill dict to our trade format for frontend."""
    side = f.get("side", "")
    pnl = float(f.get("pnl", 0) or 0)
    px = float(f.get("fillPx", 0) or 0)
    sz = float(f.get("sz", 0) or 0)
    inst_id = f.get("instId", "")
    is_close = pnl != 0

    # Determine pos_side from side + close direction
    # OKX: for long close → side=sell, pnl>0; for short close → side=buy, pnl>0
    if is_close:
        pos_side = "long" if side == "sell" else "short"
        reason = "closed"
    else:
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
    """Pair OKX fills into entry+close trades. Returns list of trade dicts for frontend.
    Strategy: group by instId, pair buy→sell (long) or sell→buy (short) sequentially.
    Each fill with pnl!=0 is a close. Fills with pnl=0 are entries.
    Side-consistency: long close (side=sell) must match buy entry; short close (side=buy) must match sell entry."""
    # Separate entries and closes per instrument
    by_inst: dict[str, dict] = {}  # inst_id -> {entries: [], closes: []}
    for f in fills:
        inst = f.get("instId", "")
        if inst not in by_inst:
            by_inst[inst] = {"entries": [], "closes": []}
        pnl = float(f.get("pnl", 0) or 0)
        if pnl != 0:
            by_inst[inst]["closes"].append(f)
        else:
            by_inst[inst]["entries"].append(f)

    paired = []
    for inst_id, groups in by_inst.items():
        # Sort entries by time ascending
        entries = sorted(groups["entries"], key=lambda x: x.get("ts", "0"))
        closes = sorted(groups["closes"], key=lambda x: x.get("ts", "0"))

        # For each close, find the best matching entry with side-consistency
        used_entries = set()
        for close_f in closes:
            close_ts = close_f.get("ts", "0")
            close_side = close_f.get("side", "")
            # Long close: side=sell → expected entry side=buy
            # Short close: side=buy → expected entry side=sell
            expected_entry_side = "buy" if close_side == "sell" else "sell"

            best_entry = None
            best_idx = -1
            for i, entry_f in enumerate(entries):
                if i in used_entries:
                    continue
                if entry_f.get("ts", "0") <= close_ts:
                    best_entry = entry_f
                    best_idx = i
            # Prefer side-consistent match: pick latest entry with matching side
            side_match = None
            side_match_idx = -1
            for i, entry_f in enumerate(entries):
                if i in used_entries:
                    continue
                if entry_f.get("side", "") == expected_entry_side and entry_f.get("ts", "0") <= close_ts:
                    side_match = entry_f
                    side_match_idx = i
            if side_match:
                best_entry = side_match
                best_idx = side_match_idx

            if best_entry:
                used_entries.add(best_idx)
                entry_px = float(best_entry.get("fillPx", 0) or 0)
                exit_px = float(close_f.get("fillPx", 0) or 0)
                pos_side = "long" if close_side == "sell" else "short"
                paired.append({
                    "time": _ms_to_iso(close_f.get("ts", "")),
                    "side": close_side,
                    "symbol": inst_id,
                    "size": float(close_f.get("sz", 0) or 0),
                    "pnl": float(close_f.get("pnl", 0) or 0),
                    "ord_id": close_f.get("ordId", ""),
                    "fee": close_f.get("fee", "0"),
                    "entry": entry_px,
                    "entry_price": entry_px,
                    "exit_price": exit_px,
                    "reason": "closed",
                    "pos_side": pos_side,
                    "inst_id": inst_id,
                    "source": "okx",
                })
            else:
                # Close without matching entry — show as closed with just exit price
                paired.append(_fill_to_trade(close_f))

        # Remaining unpaired entries (open positions)
        for i, entry_f in enumerate(entries):
            if i not in used_entries:
                paired.append(_fill_to_trade(entry_f))

    return paired


# ── PnL from OKX bills ──

async def _get_okx_realized_pnl() -> dict:
    """Calculate realized PnL from OKX close fills (pnl != 0). More reliable than bills."""
    now_ms = int(_time.time() * 1000)
    periods = {"1d": 86400_000, "7d": 604800_000, "30d": 2592000_000}
    pnl = {"1d": 0.0, "7d": 0.0, "30d": 0.0}

    all_fills = await _fetch_okx_fills(limit=100)

    for f in all_fills:
        try:
            fill_pnl = float(f.get("pnl", 0) or 0)
        except (ValueError, TypeError):
            continue
        if fill_pnl == 0:
            continue  # Skip entries (pnl=0)
        try:
            fill_ts = int(f.get("ts", 0))
        except (ValueError, TypeError):
            continue
        if fill_ts == 0:
            continue
        for key, window in periods.items():
            if fill_ts >= now_ms - window:
                pnl[key] += fill_pnl

    return pnl


@app.get("/api/pnl")
async def get_pnl():
    """Realized PnL from OKX bills + unrealized from OKX positions. No local calculations."""
    realized = await _get_okx_realized_pnl()
    unrealized = 0.0
    result = await _okx_call(lambda c: c.get_positions("SWAP"))
    if not result.get("error"):
        for p in result.get("data", []):
            unrealized += float(p.get("upl", 0))
    return {
        "1d": round(realized["1d"], 2),
        "7d": round(realized["7d"], 2),
        "30d": round(realized["30d"], 2),
        "unrealized": round(unrealized, 2),
    }


# ── Trades ──

@app.get("/api/trades")
async def get_all_trades(limit: int = 100):
    """All trades from OKX fills (unpaired, raw)."""
    raw_fills = await _fetch_okx_fills(limit=limit)
    trades = [_fill_to_trade(f) for f in raw_fills]
    return {"trades": trades[:limit]}


@app.get("/api/trades/paired")
async def get_paired_trades(limit: int = 15, begin: str = None, end: str = None):
    """Paired entry+close trades from OKX fills."""
    raw_fills = await _fetch_okx_fills(limit=200)
    paired = await _pair_fills(raw_fills)
    if begin or end:
        filtered = []
        for t in paired:
            t_time = t.get("time", "")
            if begin and t_time and t_time < begin:
                continue
            if end and t_time and t_time > end:
                continue
            filtered.append(t)
        paired = filtered
    paired = paired[:limit]
    return {"trades": paired}


# ── DB Positions ──

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
