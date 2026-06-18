import asyncio
import gc
import json
import math
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, Response

from app.models.schemas import (
    OKXCredentials, BacktestRequest, LiveDeployRequest,
    StrategyMeta
)
from app.services.okx_client import OKXClientManager
from app.services.backtest_engine import BacktestEngine, load_strategy_file
from app.services.data_cache import ensure_candles
from app.services.strategy_loader import (
    list_strategies, get_strategy_code, save_strategy,
    delete_strategy, list_backtest_results,
    save_backtest_result, parse_strategy_file, STRATEGIES_DIR,
)
from app.database import db
from app.services.ws_manager import WSManager
from app.engine.bot_engine import BotEngine
from app.services.auth import login, guest, validate, logout, is_admin, PASSWORD, check_rate_limit, record_attempt

load_dotenv()

app = FastAPI(title="OKX Trading Terminal", version="2.0.0")

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

ws_manager: Optional[WSManager] = None
trade_log: list = []
live_bots: dict = {}

@dataclass
class BacktestJob:
    id: str
    status: str = "pending"  # pending | running | done | error
    progress: str = ""
    result: Optional[dict] = None
    error: Optional[str] = None
    ts: float = 0.0

_backtest_jobs: dict[str, BacktestJob] = {}
_bt_lock = asyncio.Lock()


async def _cleanup_old_jobs():
    while True:
        await asyncio.sleep(300)
        now = time.time()
        old = [jid for jid, job in list(_backtest_jobs.items())
               if job.status in ("done", "error") and job.ts <= now - 300]
        for jid in old:
            _backtest_jobs.pop(jid, None)
        if old:
            print(f"[main] Cleaned {len(old)} old backtest jobs", flush=True)

async def _startup_ws():
    """Connect WS in background so startup isn't blocked."""
    global ws_manager
    try:
        ws_manager = WSManager(_env_key, _env_secret, _env_pass, _env_demo)
        ws_manager.on("account", _ws_on_account)
        ws_manager.on("positions", _ws_on_positions)
        ws_manager.on("orders", _ws_on_orders)
        await ws_manager.start()
        await ws_manager.subscribe("account")
        await ws_manager.subscribe("positions")
        await ws_manager.subscribe("orders")
        print("[startup] WS connected", flush=True)
    except Exception as e:
        print(f"[startup] WS failed (non-fatal): {e}", flush=True)


@app.on_event("startup")
async def startup():
    try:
        print("[startup] 1/5 DB init ...", flush=True)
        await db.init()
        print("[startup] 2/5 OKX client init ...", flush=True)
        if _env_key and _env_secret and _env_pass:
            await client_manager.init_client(_env_key, _env_secret, _env_pass, _env_demo)

        print("[startup] 3/5 WebSocket (background) ...", flush=True)
        asyncio.create_task(_startup_ws())

        print("[startup] 4/5 Restore bots ...", flush=True)
        await _restore_bots()
        print("[startup] 5/5 Cleanup jobs ...", flush=True)
        asyncio.create_task(_cleanup_old_jobs())
        print("[startup] Done — server ready", flush=True)
    except Exception as e:
        print(f"[startup] ERROR: {e}", flush=True)
        raise

@app.on_event("shutdown")
async def shutdown():
    # Gracefully stop all running bots
    for bid, bot in list(live_bots.items()):
        try:
            if bot.status == "running":
                print(f"[shutdown] Stopping bot {bid}...", flush=True)
                await bot.stop()
        except Exception as e:
            print(f"[shutdown] Error stopping bot {bid}: {e}", flush=True)
    if ws_manager:
        await ws_manager.stop()
    await db.close()

async def _ws_on_account(data: list):
    for entry in data:
        for detail in entry.get("details", []):
            trade_log.append({
                "id": str(uuid.uuid4()),
                "timestamp": datetime.now().isoformat(),
                "type": "balance_update",
                "ccy": detail.get("ccy"),
                "eq": detail.get("eq"),
                "eqUsd": detail.get("eqUsd"),
            })

async def _ws_on_positions(data: list):
    for pos in data:
        bot_id = pos.get("instId", "")
        pnl = float(pos.get("upl", 0))
        await db.update_position_price(bot_id, float(pos.get("markPx", 0)), pnl)

async def _ws_on_orders(data: list):
    for ord_data in data:
        trade_log.append({
            "id": str(uuid.uuid4()),
            "timestamp": datetime.now().isoformat(),
            "type": "order_update",
            "instId": ord_data.get("instId"),
            "ordId": ord_data.get("ordId"),
            "state": ord_data.get("state"),
            "fillSz": ord_data.get("fillSz"),
            "fillPx": ord_data.get("fillPx"),
        })


def _active_bot_count() -> int:
    return sum(1 for b in live_bots.values() if b.status == "running")


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


async def _restore_bots():
    bots = await db.get_bots()
    restored = 0
    auto_started = 0
    for b in bots:
        try:
            bid = b["id"]
            if bid in live_bots:
                continue
            params = json.loads(b["params"]) if isinstance(b["params"], str) else b.get("params") or {}
            bot = BotEngine(
                bot_id=bid,
                strategy_id=b["strategy_id"],
                strategy_code=b["strategy_code"],
                symbol=b["symbol"],
                timeframe=b["timeframe"],
                capital=float(b["capital"]),
                params=params,
                client_manager=client_manager,
                trade_log=trade_log,
                get_active_bot_count=_active_bot_count,
                name=b.get("name"),
            )
            bot.status = "stopped"
            live_bots[bid] = bot
            restored += 1
            # Auto-start bots that were running or starting before restart
            db_status = b.get("status", "stopped")
            if db_status in ("running", "starting"):
                def _make_start_task(bid_ref, bot_ref):
                    async def _safe_start():
                        try:
                            await bot_ref.start()
                            print(f"[startup] Auto-started bot {bid_ref}", flush=True)
                        except Exception as e:
                            print(f"[startup] ERROR auto-starting bot {bid_ref}: {e}", flush=True)
                            bot_ref.status = "stopped"
                    return _safe_start()
                asyncio.create_task(_make_start_task(bid, bot))
                auto_started += 1
        except Exception as e:
            print(f"[startup] Error restoring bot {b.get('id', '?')}: {e}", flush=True)
    if bots:
        print(f"[startup] Restored {len(bots)} bots from DB ({restored} total, {auto_started} auto-started)", flush=True)


# ── Auth helpers ──

def get_token(request: Request):
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:]
    return ""

# Middleware: protect all POST/PUT/DELETE routes (except auth endpoints)
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if request.method in ("POST", "PUT", "DELETE") and request.url.path.startswith("/api/"):
        skip = ("/api/auth/login", "/api/auth/guest", "/api/auth/logout")
        if request.url.path not in skip and PASSWORD:
            token = get_token(request)
            if not is_admin(token):
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await call_next(request)

@app.post("/api/auth/login")
async def auth_login(request: Request):
    delay = check_rate_limit(request.client.host)
    if delay:
        await asyncio.sleep(delay)
    body = await request.json()
    token = login(body.get("password", ""))
    if not token:
        record_attempt(request.client.host, False)
        return JSONResponse({"detail": "Неверный пароль"}, status_code=401)
    record_attempt(request.client.host, True)
    return {"token": token, "role": "admin"}

@app.post("/api/auth/guest")
async def auth_guest():
    token = guest()
    return {"token": token, "role": "guest"}

@app.get("/api/auth/status")
async def auth_status(request: Request):
    token = get_token(request)
    role = validate(token)
    return {"authenticated": bool(role), "role": role, "has_password": bool(PASSWORD)}

@app.post("/api/auth/logout")
async def auth_logout(request: Request):
    token = get_token(request)
    logout(token)
    return {"status": "ok"}

@app.get("/api/health")
async def health():
    client = client_manager.get_client()
    return {
        "status": "ok",
        "connected": client.is_connected if client else False,
        "has_credentials": client_manager.is_ready(),
        "demo": client.demo if client else False,
        "env_configured": bool(_env_key and _env_secret and _env_pass),
        "env_demo": _env_demo,
        "db_path": str(db.db_path),
        "ws_running": ws_manager._running if ws_manager else False,
    }

@app.get("/api/credentials/status")
async def credentials_status():
    client = client_manager.get_client()
    return {
        "has_credentials": client_manager.is_ready(),
        "demo": client.demo if client else _env_demo,
        "env_configured": bool(_env_key and _env_secret and _env_pass),
        "connected": client.is_connected if client else False,
    }


@app.post("/api/credentials/test")
async def test_connection(creds: OKXCredentials):
    result = await client_manager.test_connection(
        creds.api_key, creds.secret_key, creds.passphrase, creds.demo
    )
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["message"])
    return {"connected": True, "demo": creds.demo, "data": result}


@app.post("/api/credentials/init")
async def init_credentials(creds: OKXCredentials):
    client = await client_manager.init_client(
        creds.api_key, creds.secret_key, creds.passphrase, creds.demo
    )
    test = await client.get_balance()
    if test.get("error"):
        raise HTTPException(status_code=400, detail=test["message"])
    return {"connected": True, "demo": creds.demo, "message": "Credentials saved"}


@app.get("/api/portfolio")
async def get_portfolio():
    result = await _okx_call(lambda c: c.get_balance())
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result.get("message", ""))
    data = result.get("data", [{}])[0]
    details = data.get("details", [])
    total = float(data.get("totalEq", 0))
    return {
        "totalEqUsd": round(total, 2),
        "details": [
            {
                "ccy": d["ccy"],
                "eq": d.get("eq", "0"),
                "eqUsd": round(float(d.get("eqUsd", 0)), 2),
                "availBal": d.get("availBal", "0"),
                "frozenBal": d.get("frozenBal", "0"),
            }
            for d in details
        ]
    }


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
    pos_side = data.get("posSide")
    sz = data.get("sz", "0")
    mgn_mode = data.get("mgnMode", "cross")

    if not inst_id or not pos_side or not sz:
        raise HTTPException(status_code=400, detail="instId, posSide, sz required")

    side = "buy" if pos_side == "short" else "sell"

    result = await client.place_order(
        inst_id=inst_id, side=side, ord_type="market",
        sz=str(sz), td_mode=mgn_mode, pos_side=pos_side,
    )
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result.get("message", ""))
    return {"ok": True, "data": result.get("data", [])}


@app.get("/api/market/ticker")
async def get_ticker(inst_id: str = "BTC-USDT"):
    result = await _okx_call(lambda c: c.get_ticker(inst_id))
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result.get("message", ""))
    data = result.get("data", [{}])[0]
    return {
        "instId": data.get("instId"),
        "last": data.get("last"),
        "bid": data.get("bidPx"),
        "ask": data.get("askPx"),
        "vol24h": data.get("volCcy24h"),
        "high24h": data.get("high24h"),
        "low24h": data.get("low24h"),
        "change24h": data.get("change24h"),
    }


@app.get("/api/market/candles")
async def get_candles(inst_id: str = "BTC-USDT", bar: str = "1H",
                       after: str = None, before: str = None, limit: int = 200):
    result = await _okx_call(lambda c: c.get_candles(inst_id, bar, after, before, limit))
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result.get("message", ""))
    return {
        "candles": [
            {
                "ts": c[0], "open": c[1], "high": c[2], "low": c[3],
                "close": c[4], "vol": c[5], "volCcy": c[6], "volCcyQuote": c[7]
            }
            for c in result.get("data", [])
        ]
    }


@app.post("/api/trade/order")
async def place_order(req: dict):
    client = client_manager.get_client()
    if not client:
        raise HTTPException(status_code=400, detail="API not configured")
    result = await client.place_order(
        inst_id=req["instId"],
        side=req["side"],
        ord_type=req.get("ordType", "market"),
        sz=req["sz"],
        px=req.get("px"),
        td_mode=req.get("tdMode", "cash"),
    )
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["message"])
    trade_log.append({
        "id": str(uuid.uuid4()),
        "timestamp": datetime.now().isoformat(),
        "instId": req["instId"],
        "side": req["side"],
        "sz": req["sz"],
        "state": "filled",
        **result.get("data", [{}])[0]
    })
    return result


@app.get("/api/trade/orders")
async def get_orders(inst_type: str = "SWAP"):
    client = client_manager.get_client()
    if not client:
        raise HTTPException(status_code=400, detail="API not configured")
    result = await client.get_orders(inst_type)
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["message"])
    return {"orders": result.get("data", [])}


@app.get("/api/trade/log")
async def get_trade_log():
    return {"orders": trade_log[-100:]}


@app.get("/api/strategies")
async def get_strategies():
    return {"strategies": list_strategies()}


@app.post("/api/strategies/upload")
async def upload_strategy(data: dict):
    filename = data.get("filename", "strategy.py")
    content = data.get("content", "")
    if not content:
        raise HTTPException(status_code=400, detail="No content")
    if save_strategy(filename, content):
        return {"message": "Uploaded", "filename": filename}
    raise HTTPException(status_code=500, detail="Save failed")


@app.delete("/api/strategies/{strategy_id}")
async def remove_strategy(strategy_id: str):
    if delete_strategy(strategy_id):
        return {"message": "Deleted"}
    raise HTTPException(status_code=404, detail="Not found")


async def _run_backtest_job(job: 'BacktestJob', req: BacktestRequest, strategy_code: str, strategy_name: str):
    async with _bt_lock:
        job.status = "running"
    try:
        job.progress = "Загрузка свечей..."
        all_candles = await asyncio.wait_for(
            ensure_candles(
                req.symbol, req.timeframe,
                start_date=req.start_date,
                end_date=req.end_date,
                force_refresh=True,
                max_candles=200000,
            ),
            timeout=300
        )
        if not all_candles:
            raise ValueError("Нет данных за указанный период")

        strategy_meta = load_strategy_file(
            str(STRATEGIES_DIR / f"{req.strategy_id}.py")
        ) or load_strategy_file(
            str(STRATEGIES_DIR / f"{req.strategy_id}.json")
        )
        if not strategy_meta:
            strategy_name = req.strategy_id

        engine = BacktestEngine(strategy_code, strategy_name)
        default_params = {}
        if strategy_meta:
            params_str = strategy_meta.get("@params", "")
            if params_str:
                try:
                    default_params = json.loads(params_str)
                except (json.JSONDecodeError, TypeError):
                    pass
        req_merged = {**default_params, **dict(req.params or {})}
        params = {"name": strategy_name, "timeframe": req.timeframe, "symbol": req.symbol, **req_merged}

        job.progress = "Расчёт стратегии..."
        bt_result = engine.run(all_candles, req.initial_capital, params)

        if "error" in bt_result:
            raise ValueError(bt_result["error"])

        bt_result["symbol"] = req.symbol
        bt_result["candles_loaded"] = len(all_candles)
        save_backtest_result(req.strategy_id, bt_result)

        del all_candles
        gc.collect()

        async with _bt_lock:
            job.status = "done"
            job.result = bt_result
            job.progress = "Готово"
            job.ts = time.time()
    except asyncio.TimeoutError:
        async with _bt_lock:
            job.status = "error"
            job.error = "Таймаут загрузки свечей (>120 сек)"
            job.ts = time.time()
    except Exception as e:
        async with _bt_lock:
            job.status = "error"
            job.error = str(e)
            job.ts = time.time()


@app.post("/api/backtest/run")
async def run_backtest(req: BacktestRequest):
    strategy_code = get_strategy_code(req.strategy_id)
    if not strategy_code:
        raise HTTPException(status_code=404, detail="Strategy not found")

    client = client_manager.get_client()
    if not client:
        raise HTTPException(status_code=400, detail="API not configured")

    strategy_meta = load_strategy_file(
        str(STRATEGIES_DIR / f"{req.strategy_id}.py")
    ) or load_strategy_file(
        str(STRATEGIES_DIR / f"{req.strategy_id}.json")
    )
    strategy_name = strategy_meta.get("@name", strategy_meta.get("name", req.strategy_id)) if strategy_meta else req.strategy_id

    job_id = uuid.uuid4().hex[:12]
    job = BacktestJob(id=job_id)
    _backtest_jobs[job_id] = job

    asyncio.create_task(_run_backtest_job(job, req, strategy_code, strategy_name))
    return {"job_id": job_id, "status": "accepted"}


@app.get("/api/backtest/status/{job_id}")
async def backtest_status(job_id: str):
    job = _backtest_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {
        "status": job.status,
        "progress": job.progress,
        "result": job.result,
        "error": job.error,
    }


@app.get("/api/backtest/xgb-dataset/{job_id}")
async def xgb_dataset(job_id: str):
    """Возвращает XGBoost dataset из результатов бэктеста"""
    job = _backtest_jobs.get(job_id)
    if not job or not job.result:
        raise HTTPException(status_code=404, detail="Job or result not found")
    ds = job.result.get("xgb_dataset", [])
    return {
        "strategy": job.result.get("strategy_name"),
        "n_samples": len(ds),
        "feature_names": [
            "rsi", "atr_pct", "macd_hist", "bb_width", "vol_ratio",
            "dist_ema200_pct", "dist_ema50_pct", "swing_range", "dist_to_swing",
        ],
        "data": ds,
    }


@app.post("/api/live/deploy")
async def deploy_live(req: LiveDeployRequest):
    strategy_code = get_strategy_code(req.strategy_id)
    if not strategy_code:
        raise HTTPException(status_code=404, detail="Strategy not found")

    client = client_manager.get_client()
    if not client:
        raise HTTPException(status_code=400, detail="API not configured")

    ns = {"pd": pd, "np": np, "math": math}
    try:
        exec(strategy_code, ns)
        fn = ns.get("generate_signals")
        if not fn:
            raise HTTPException(status_code=400, detail="Strategy missing generate_signals function")
    except SyntaxError as e:
        raise HTTPException(status_code=400, detail=f"Strategy syntax error: {e}")

    # Merge strategy @params with request params (request takes priority)
    strategy_path = STRATEGIES_DIR / f"{req.strategy_id}.py"
    strategy_meta = parse_strategy_file(strategy_path) if strategy_path.exists() else None
    strategy_params = strategy_meta.get("params", {}) if strategy_meta else {}
    merged_params = {**strategy_params, **req.params}

    bot_id = str(uuid.uuid4())[:8]
    signal_type = "position"
    bot = BotEngine(
        bot_id=bot_id,
        strategy_id=req.strategy_id,
        strategy_code=strategy_code,
        symbol=req.symbol,
        timeframe=req.timeframe,
        capital=req.capital,
        params=merged_params,
        client_manager=client_manager,
        trade_log=trade_log,
        get_active_bot_count=_active_bot_count,
        name=req.name,
    )
    await db.save_bot(
        bot_id=bot_id, strategy_id=req.strategy_id,
        strategy_code=strategy_code, symbol=req.symbol,
        timeframe=req.timeframe, capital=req.capital,
        params=merged_params, mode="demo",
        signal_type=signal_type, name=req.name,
    )
    live_bots[bot_id] = bot
    await bot.start()
    return {
        "bot_id": bot_id,
        "status": "running",
        "name": req.name,
        "message": f"Strategy {req.strategy_id} deployed on {req.symbol}",
        "cycle_interval_sec": {"1m": 60, "3m": 180, "5m": 300, "15m": 900,
                               "30m": 1800, "1H": 3600, "4H": 14400, "1D": 86400}.get(req.timeframe, 600),
    }


@app.get("/api/live/bots")
async def list_live_bots(status: str = None):
    if status:
        return {"bots": [b.to_dict() for b in live_bots.values() if b.status == status]}
    return {"bots": [b.to_dict() for b in live_bots.values()]}


@app.get("/api/live/bots/{bot_id}")
async def get_bot_detail(bot_id: str):
    if bot_id not in live_bots:
        raise HTTPException(status_code=404, detail="Bot not found")
    return live_bots[bot_id].to_dict()


@app.post("/api/live/stop/{bot_id}")
async def stop_bot(bot_id: str):
    if bot_id not in live_bots:
        raise HTTPException(status_code=404, detail="Bot not found")
    await live_bots[bot_id].stop()
    return {"message": f"Bot {bot_id} stopped"}


@app.post("/api/live/start/{bot_id}")
async def start_bot(bot_id: str):
    if bot_id not in live_bots:
        raise HTTPException(status_code=404, detail="Bot not found")
    bot = live_bots[bot_id]
    if bot.status == "running":
        return {"message": f"Bot {bot_id} is already running", "bot_id": bot_id}
    bot.error = None
    bot.status = "starting"
    await bot.start()
    return {"message": f"Bot {bot_id} started", "bot_id": bot_id}


@app.post("/api/live/restart/{bot_id}")
async def restart_bot(bot_id: str):
    if bot_id not in live_bots:
        raise HTTPException(status_code=404, detail="Bot not found")
    bot = live_bots[bot_id]
    await bot.stop()
    await asyncio.sleep(1)
    bot.position = 0.0
    bot.entry_price = 0.0
    bot.last_position = 0
    bot.error = None
    bot.status = "starting"
    await bot.start()
    return {"message": f"Bot {bot_id} restarted", "bot_id": bot_id}


@app.post("/api/auto-trade/start")
async def auto_trade_start(req: LiveDeployRequest):
    if not req.strategy_id:
        raise HTTPException(status_code=400, detail="strategy_id required")
    strategy_code = get_strategy_code(req.strategy_id)
    if not strategy_code:
        raise HTTPException(status_code=404, detail="Strategy not found")
    client = client_manager.get_client()
    if not client:
        raise HTTPException(status_code=400, detail="API not configured")
    bot_id = str(uuid.uuid4())[:8]
    signal_type = "position"
    bot = BotEngine(
        bot_id=bot_id,
        strategy_id=req.strategy_id,
        strategy_code=strategy_code,
        symbol=req.symbol,
        timeframe=req.timeframe,
        capital=req.capital,
        params=req.params,
        client_manager=client_manager,
        trade_log=trade_log,
        get_active_bot_count=_active_bot_count,
        name=req.name,
    )
    await db.save_bot(
        bot_id=bot_id, strategy_id=req.strategy_id,
        strategy_code=strategy_code, symbol=req.symbol,
        timeframe=req.timeframe, capital=req.capital,
        params=req.params, mode="demo",
        signal_type=signal_type, name=req.name,
    )
    live_bots[bot_id] = bot
    await bot.start()
    return {
        "bot_id": bot_id,
        "status": "running",
        "name": req.name,
        "message": f"Auto-trade started: {req.strategy_id} on {req.symbol}",
    }


@app.get("/api/auto-trade/status")
async def auto_trade_status():
    active = [(bid, b.to_dict()) for bid, b in live_bots.items() if b.status == "running"]
    return {
        "active_count": len(active),
        "bots": [a[1] for a in active],
    }


@app.get("/api/backtest/history")
async def get_backtest_history():
    return {"results": list_backtest_results()}


# ── Database-backed endpoints ──

@app.get("/api/bots")
async def list_bots():
    rows = await db.get_bots()
    active = {b.id: b.to_dict() for b in live_bots.values()}
    result = []
    for row in rows:
        bid = row["id"]
        if bid in active:
            result.append(active[bid])
        else:
            row["capital"] = row.get("capital") or 100.0
            try:
                row["params"] = json.loads(row.get("params", "{}"))
            except (json.JSONDecodeError, TypeError):
                row["params"] = {}
            result.append(row)
    return {"bots": result}


@app.get("/api/bots/{bot_id}/signals")
async def get_bot_signals(bot_id: str, limit: int = 100):
    signals = await db.get_signals(bot_id=bot_id, limit=limit)
    return {"signals": signals}


@app.get("/api/bots/{bot_id}/planned")
async def get_bot_planned(bot_id: str):
    bot = live_bots.get(bot_id)
    if bot:
        return {"planned": bot.planned_trade, "status": bot.status}
    return {"planned": None, "status": "offline"}


@app.get("/api/bots/{bot_id}/trades")
async def get_bot_trades(bot_id: str, limit: int = 100):
    trades = await db.get_trades(bot_id=bot_id, limit=limit)
    summary = await db.get_trades_summary(bot_id)
    return {"trades": trades, "summary": summary}


@app.get("/api/bots/{bot_id}/chart")
async def get_bot_chart(bot_id: str, limit: int = 200, bar: str = None):
    bot = live_bots.get(bot_id)
    if not bot:
        rows = await db.get_bots(bot_id=bot_id)
        if not rows:
            raise HTTPException(status_code=404, detail="Bot not found")
        from app.services.strategy_loader import parse_strategy_file, STRATEGIES_DIR
        strategy_path = STRATEGIES_DIR / f"{rows[0]['strategy_id']}.py"
        meta = parse_strategy_file(strategy_path) if strategy_path.exists() else {}
        params = json.loads(rows[0].get("params", "{}"))
        symbol = rows[0]["symbol"]
        timeframe = rows[0]["timeframe"]
        strategy_code = rows[0].get("strategy_code", "")
    else:
        meta = {}
        params = bot.params
        symbol = bot.symbol
        timeframe = bot.timeframe
        strategy_code = bot.strategy_code

    tf = bar or timeframe
    raw = await ensure_candles(symbol, tf, live_limit=limit)
    if not raw:
        return {"candles": [], "trades": [], "signals": [], "indicators": {}}

    df = pd.DataFrame(raw)
    df.columns = ["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"]
    for col in ["open", "high", "low", "close", "vol"]:
        df[col] = df[col].astype(float)
    df["ts"] = pd.to_datetime(df["ts"].astype(int), unit="ms")
    df = df.sort_values("ts").reset_index(drop=True)

    candles_out = []
    for _, r in df.iterrows():
        candles_out.append({
            "time": int(r["ts"].timestamp()),
            "open": round(r["open"], 2),
            "high": round(r["high"], 2),
            "low": round(r["low"], 2),
            "close": round(r["close"], 2),
            "volume": round(r["vol"], 4),
        })

    signals_out = []
    indicator_series = {}
    if strategy_code:
        ns = {"pd": pd, "np": np, "math": math}
        try:
            exec(strategy_code, ns)
            fn = ns.get("generate_signals")
            if fn:
                sig = fn(df, params)
                sig_arr = sig.values if hasattr(sig, "values") else list(sig)
                for i, v in enumerate(sig_arr):
                    if v != 0:
                        signals_out.append({
                            "time": int(df.iloc[i]["ts"].timestamp()),
                            "value": int(v),
                        })
        except Exception:
            pass

    close_vals = df["close"].values.astype(float)
    indicators = {"lines": [], "signals": signals_out}

    ema_periods = set()
    for k, v in params.items():
        if "ema" in k.lower() or "ma" in k.lower():
            try:
                ema_periods.add(int(v))
            except (ValueError, TypeError):
                pass
    if not ema_periods:
        meta_params = meta.get("params", {})
        for k, v in meta_params.items():
            if "ema" in k.lower() or "ma" in k.lower():
                try:
                    ema_periods.add(int(v))
                except (ValueError, TypeError):
                    pass
    if not ema_periods:
        ema_periods = {50, 200}

    for period in sorted(ema_periods):
        vals = pd.Series(close_vals).ewm(span=period).mean().values
        line_data = []
        for i in range(len(df)):
            if not np.isnan(vals[i]):
                line_data.append({
                    "time": int(df.iloc[i]["ts"].timestamp()),
                    "value": round(float(vals[i]), 2),
                })
        indicators["lines"].append({
            "name": f"EMA {period}",
            "data": line_data,
            "color": "#f0b429" if period == min(ema_periods) else "#a78bfa",
        })

    has_rsi = any("rsi" in k.lower() for k in params) or any("rsi" in k.lower() for k in meta_params)
    if has_rsi or True:
        delta_vals = pd.Series(close_vals).diff().values
        gain = np.where(delta_vals > 0, delta_vals, 0)
        loss = np.where(delta_vals < 0, -delta_vals, 0)
        avg_gain = pd.Series(gain).rolling(14).mean().values
        avg_loss = pd.Series(loss).rolling(14).mean().values
        rsi_vals = np.full(len(close_vals), 50.0)
        for i in range(14, len(close_vals)):
            if avg_loss[i] == 0:
                rsi_vals[i] = 100.0
            else:
                rsi_vals[i] = 100.0 - 100.0 / (1.0 + avg_gain[i] / avg_loss[i])
        rsi_data = []
        for i in range(len(df)):
            if not np.isnan(rsi_vals[i]):
                rsi_data.append({
                    "time": int(df.iloc[i]["ts"].timestamp()),
                    "value": round(float(rsi_vals[i]), 1),
                })
        indicators["rsi"] = rsi_data

    if hasattr(bot, "position") and bot.position != 0:
        indicators["current_position"] = {
            "side": "long" if bot.position > 0 else "short",
            "size": round(abs(bot.position), 6),
            "entry": round(bot.entry_price, 2),
        }

    trades = await db.get_trades(bot_id=bot_id, limit=50)
    markers = []
    for t in trades:
        px = float(t.get("px", 0))
        ts = t.get("timestamp", "")
        try:
            t_int = int(pd.Timestamp(ts).timestamp()) if ts else 0
        except Exception:
            t_int = 0
        is_buy = t.get("side") == "buy" or t.get("side") == "buy"
        markers.append({
            "time": t_int,
            "position": "belowBar" if is_buy else "aboveBar",
            "color": "#00ff88" if is_buy else "#ff4444",
            "shape": "arrowUp" if is_buy else "arrowDown",
            "text": f"{'BUY' if is_buy else 'SELL'} ${px}",
        })

    return {
        "candles": candles_out,
        "trades": trades,
        "markers": markers,
        "indicators": indicators,
        "symbol": symbol,
        "timeframe": tf,
    }


@app.get("/api/pnl")
async def get_pnl():
    pnl_1d = await db.get_pnl_by_period(1)
    pnl_7d = await db.get_pnl_by_period(7)
    pnl_30d = await db.get_pnl_by_period(30)
    return {"1d": round(pnl_1d, 2), "7d": round(pnl_7d, 2), "30d": round(pnl_30d, 2)}

@app.get("/api/trades")
async def get_all_trades(limit: int = 100):
    trades = await db.get_trades(limit=limit)
    return {"trades": trades}


@app.get("/api/trades/paired")
async def get_paired_trades(limit: int = 15, begin: str = None, end: str = None):
    paired = await db.get_paired_trades(limit=limit, begin=begin, end=end)
    return {"trades": paired}


@app.get("/api/bots/{bot_id}/metrics")
async def get_bot_metrics(bot_id: str, limit: int = 100):
    metrics = await db.get_metrics(bot_id=bot_id, limit=limit)
    return {"metrics": metrics}


@app.delete("/api/bots/{bot_id}")
async def delete_bot(bot_id: str):
    if bot_id in live_bots:
        bot = live_bots[bot_id]
        if bot.status == "running":
            await bot.stop()
        del live_bots[bot_id]
    await db.delete_bot_all(bot_id)
    return {"message": f"Bot {bot_id} and all data deleted"}


@app.get("/api/signals")
async def get_all_signals(limit: int = 100):
    signals = await db.get_signals(limit=limit)
    return {"signals": signals}


@app.get("/api/db/positions")
async def get_db_positions():
    positions = await db.get_all_positions()
    return {"positions": positions}


@app.get("/api/ws/status")
async def ws_status():
    if ws_manager:
        return {"running": ws_manager._running, "subscribed": list(ws_manager._subscribed)}
    return {"running": False, "subscribed": []}


@app.get("/api/debug/binance-test")
async def debug_binance():
    """Test API connectivity from Render"""
    import httpx as _httpx
    results = {}
    async with _httpx.AsyncClient(timeout=10.0) as c:
        now = int(datetime.now().timestamp())
        ago = now - 86400

        for name, url, params in [
            ("binance", "https://api.binance.com/api/v3/klines",
             {"symbol": "BTCUSDT", "interval": "5m", "limit": "1"}),
            ("bybit", "https://api.bybit.com/v5/market/kline",
             {"symbol": "BTCUSDT", "interval": "5", "limit": "1"}),
            ("kucoin", "https://api.kucoin.com/api/v1/market/candles",
             {"symbol": "BTC-USDT", "type": "5min", "startAt": str(ago), "endAt": str(now)}),
            ("gateio", "https://api.gateio.ws/api/v4/spot/candlesticks",
             {"currency_pair": "BTC_USDT", "interval": "5m", "limit": "1"}),
            ("bitfinex", "https://api-pub.bitfinex.com/v2/candles/trade:5m:tBTCUSD/hist",
             {"limit": "1"}),
            ("kraken", "https://api.kraken.com/0/public/OHLC",
             {"pair": "XBTUSDT", "interval": "5"}),
        ]:
            try:
                r = await c.get(url, params=params, timeout=5)
                data = r.json() if r.status_code == 200 else None
                results[name] = {"status": r.status_code, "ok": r.status_code == 200}
                if r.status_code != 200:
                    results[name]["body"] = r.text[:100]
            except Exception as e:
                results[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"}

    return results


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
