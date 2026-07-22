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
                auto_execute=True,
                poll_interval_sec=900,
                trail_pct=0.03,
                adx_threshold=20.0,
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
        auto_execute=d.get("auto_execute", True),
        poll_interval_sec=d.get("poll_interval_sec", 3600),
        roc_fast=d.get("roc_fast", 5),
        roc_slow=d.get("roc_slow", 50),
        ema_fast=d.get("ema_fast", 15),
        ema_slow=d.get("ema_slow", 30),
        atr_stop_mult=d.get("atr_stop_mult", 1.5),
        trail_pct=d.get("trail_pct", 0.03),
        adx_threshold=d.get("adx_threshold", 20.0),
        mom_threshold=d.get("mom_threshold", 0.0),
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


@app.get("/api/momentum/trades")
async def momentum_trades(limit: int = 20):
    global momentum
    trades = []
    if momentum and momentum._trade_log:
        trades = momentum._trade_log[-limit:]
    elif momentum and momentum.db:
        try:
            db_trades = await momentum.db.get_trades(bot_id=MOM_BOT_ID, limit=limit)
            trades = [
                {
                    "time": t.get("timestamp", ""),
                    "side": t.get("side", ""),
                    "symbol": t.get("inst_id", ""),
                    "size": float(t.get("sz", 0) or 0),
                    "pnl": float(t.get("pnl", 0) or 0),
                    "ord_id": t.get("ord_id", ""),
                }
                for t in db_trades
            ]
        except Exception as e:
            print(f"[Momentum] trades DB error: {e}", flush=True)
    return {"trades": trades}


@app.get("/api/momentum/chart-data")
async def momentum_chart_data():
    """Return trade markers + open position stops for chart overlay."""
    global momentum
    markers = []
    open_positions = []

    if momentum:
        for t in momentum._trade_log:
            side = t.get("side", "")
            symbol = t.get("symbol", "")
            time_str = t.get("time", "")
            if not time_str or not symbol:
                continue
            try:
                ts = int(datetime.fromisoformat(time_str).timestamp())
            except Exception:
                continue
            if side == "buy":
                markers.append({
                    "time": ts,
                    "side": "buy",
                    "symbol": symbol,
                    "entry": t.get("entry", 0),
                    "stop": t.get("stop", 0),
                    "adx": t.get("adx", 0),
                })
            elif side == "sell":
                markers.append({
                    "time": ts,
                    "side": "sell",
                    "symbol": symbol,
                    "exit_price": t.get("exit_price", 0),
                    "entry_price": t.get("entry_price", 0),
                    "pnl": t.get("pnl", 0),
                    "reason": t.get("reason", ""),
                })

        for coin, pos in momentum._positions.items():
            open_positions.append({
                "symbol": pos.symbol,
                "inst_id": pos.inst_id,
                "entry": pos.entry_price,
                "stop": pos.stop_price,
                "peak": pos.peak_price,
                "size": pos.size,
            })

    return {"markers": markers, "open_positions": open_positions}


# ── PnL ──

import time as _time

async def _get_okx_realized_pnl() -> dict:
    """Fetch realized PnL from OKX bills (trades only, type=2).
    Returns dict with keys '1d', '7d', '30d' containing PnL sums."""
    result = await _okx_call(lambda c: c.get_bills("SWAP", 100))
    now_ms = int(_time.time() * 1000)
    periods = {"1d": 86400_000, "7d": 604800_000, "30d": 2592000_000}
    pnl = {"1d": 0.0, "7d": 0.0, "30d": 0.0}

    if result.get("error"):
        return pnl

    for bill in result.get("data", []):
        if bill.get("type") != "2":
            continue
        try:
            bill_pnl = float(bill.get("pnl", 0))
        except (ValueError, TypeError):
            continue
        if bill_pnl == 0:
            continue
        bill_ts = int(bill.get("ts", 0))
        for key, window in periods.items():
            if bill_ts >= now_ms - window:
                pnl[key] += bill_pnl

    return pnl


@app.get("/api/pnl")
async def get_pnl():
    pnl_db_1d = await db.get_pnl_by_period(1)
    pnl_db_7d = await db.get_pnl_by_period(7)
    pnl_db_30d = await db.get_pnl_by_period(30)

    okx_pnl = await _get_okx_realized_pnl()

    unrealized = 0.0
    result = await _okx_call(lambda c: c.get_positions("SWAP"))
    if not result.get("error"):
        for p in result.get("data", []):
            unrealized += float(p.get("upl", 0))

    return {
        "1d": round(pnl_db_1d + okx_pnl["1d"], 2),
        "7d": round(pnl_db_7d + okx_pnl["7d"], 2),
        "30d": round(pnl_db_30d + okx_pnl["30d"], 2),
        "unrealized": round(unrealized, 2),
    }


# ── Trades ──

@app.get("/api/trades")
async def get_all_trades(limit: int = 100):
    trades = await db.get_trades(limit=limit)
    return {"trades": (trades or [])[:limit]}


@app.get("/api/trades/paired")
async def get_paired_trades(limit: int = 15, begin: str = None, end: str = None):
    paired = await db.get_paired_trades(limit=limit, begin=begin, end=end)
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
