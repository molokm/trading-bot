import asyncio
import math
import uuid
from datetime import datetime
from typing import Callable, Optional

import numpy as np
import pandas as pd

from app.database import db
from app.engine.risk_manager import risk_manager
from app.engine.signal_executor import execute_open, execute_close
from app.services.data_cache import ensure_candles


XGB_FEATURE_NAMES = [
    "rsi", "atr_pct", "macd_hist", "bb_width", "vol_ratio",
    "dist_ema200_pct", "dist_ema50_pct", "swing_range", "dist_to_swing",
]


def _load_xgb_gate(model_dir: str = None):
    """Загружает XGBoost-фильтр, если есть обученная модель"""
    import json, os
    from pathlib import Path

    if model_dir is None:
        model_dir = str(Path(__file__).parent.parent / "models")
    meta_path = os.path.join(model_dir, "xgb_meta.json")
    model_path = os.path.join(model_dir, "xgb_gate.json")
    if not os.path.exists(model_path):
        return None, None, 0.5
    try:
        import xgboost as xgb
        model = xgb.XGBClassifier()
        model.load_model(model_path)
        meta = json.load(open(meta_path)) if os.path.exists(meta_path) else {}
        threshold = meta.get("threshold", 0.5)
        print(f"[XGB] Gate loaded: threshold={threshold}", flush=True)
        return model, XGB_FEATURE_NAMES, threshold
    except Exception as e:
        print(f"[XGB] Load failed: {e}", flush=True)
        return None, None, 0.5


def _compute_xgb_features(df: pd.DataFrame) -> np.ndarray:
    """Векторизованный расчёт всех 9 фич для XGBoost на всю историю"""
    close = df["close"].values.astype(np.float64)
    high = df["high"].values.astype(np.float64)
    low = df["low"].values.astype(np.float64)
    vol = df["vol"].values.astype(np.float64)
    n = len(df)

    tr = np.maximum.reduce([
        high - low,
        np.abs(high - np.roll(close, 1)),
        np.abs(low - np.roll(close, 1)),
    ])
    tr[0] = high[0] - low[0]
    atr14 = pd.Series(tr).rolling(14).mean().values

    delta = np.diff(close, prepend=close[0])
    gain = np.where(delta > 0, delta, 0)
    loss = np.where(delta < 0, -delta, 0)
    avg_g = pd.Series(gain).ewm(span=14).mean().values
    avg_l = pd.Series(loss).ewm(span=14).mean().values + 1e-9
    rsi = 100 - 100 / (1 + avg_g / avg_l)

    ema200 = pd.Series(close).ewm(span=200).mean().values
    ema50 = pd.Series(close).ewm(span=50).mean().values
    ema12 = pd.Series(close).ewm(span=12).mean().values
    ema26 = pd.Series(close).ewm(span=26).mean().values
    macd = ema12 - ema26
    macd_sig = pd.Series(macd).ewm(span=9).mean().values

    sma20 = pd.Series(close).rolling(20).mean().values
    std20 = pd.Series(close).rolling(20).std().values
    bb_width = (std20 * 4) / (sma20 + 1e-9)

    vol_sma20 = pd.Series(vol).rolling(20).mean().values + 1e-9
    vol_ratio = vol / vol_sma20

    F = np.column_stack([
        rsi,
        atr14 / (close + 1e-9),
        macd - macd_sig,
        bb_width,
        vol_ratio,
        close / ema200 - 1,
        close / ema50 - 1,
        np.full(n, 0.0),   # swing_range placeholder
        np.full(n, 0.0),   # dist_to_swing placeholder
    ])
    return F.astype(np.float32)


class BotEngine:
    def __init__(self, bot_id: str, strategy_id: str, strategy_code: str,
                 symbol: str, timeframe: str, capital: float, params: dict,
                 client_manager, trade_log: list,
                 get_active_bot_count: Callable[[], int],
                 name: str = None):
        self.id = bot_id
        self.name = name
        self.strategy_id = strategy_id
        self.strategy_code = strategy_code
        self.symbol = symbol
        self.timeframe = timeframe
        self.capital = float(capital)
        self.params = params
        self.client_manager = client_manager
        self.trade_log = trade_log
        self._get_active_bot_count = get_active_bot_count
        self.status = "starting"
        self.started_at = datetime.now().isoformat()
        self.orders: list = []
        self.last_position = 0
        self.position = 0.0
        self.entry_price = 0.0
        self.pnl = 0.0
        self.unrealized_pnl = 0.0
        self.current_price = 0.0
        self._entry_fee = 0.0
        self.cycle_count = 0
        self.last_cycle_at = None
        self.error = None
        self._task: Optional[asyncio.Task] = None
        self._is_diff = ".diff()" in strategy_code
        self._signal_type = "diff" if self._is_diff else "position"
        self.trade_count = 0
        self.win_count = 0
        self.loss_count = 0
        self.mode = "demo"
        self._xgb_model, _, self._xgb_threshold = _load_xgb_gate()
        self._last_xgb_features = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "capital": round(self.capital, 2),
            "params": self.params,
            "status": self.status,
            "started_at": self.started_at,
            "pnl": round(self.pnl, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 2),
            "total_pnl": round(self.pnl + self.unrealized_pnl, 2),
            "position": round(self.position, 6),
            "entry_price": round(self.entry_price, 2),
            "current_price": round(self.current_price, 2),
            "cycle_count": self.cycle_count,
            "last_cycle": self.last_cycle_at,
            "error": self.error,
            "orders": self.orders[-10:],
            "signal_type": self._signal_type,
            "trade_count": self.trade_count,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "mode": "demo",
        }

    async def start(self):
        self._task = asyncio.create_task(self._loop())
        self.status = "running"
        await db.update_bot_status(self.id, "running")

    async def stop(self):
        self.status = "stopped"
        if self._task:
            self._task.cancel()
        if self.position != 0:
            await self._close_position("bot_stopped")
        await db.update_bot_stopped(self.id)

    def _compile_fn(self):
        ns = {"pd": pd, "np": np, "math": math}
        exec(self.strategy_code, ns)
        return ns.get("generate_signals")

    def _get_intended_position(self, sig_arr):
        arr = np.array(sig_arr, dtype=float)
        if len(arr) == 0:
            return 0
        last = int(arr[-1])
        if last in (2, -2):
            return 0
        if self._is_diff:
            pos = 0
            for s in arr:
                if s == 1 and pos == 0:
                    pos = 1
                elif s == -1 and pos == 1:
                    pos = 0
                elif s == -1 and pos == 0:
                    pos = -1
                elif s == 1 and pos == -1:
                    pos = 0
                elif s == -2 and pos == 1:
                    pos = -1
                elif s == 2 and pos == -1:
                    pos = 1
            return int(pos)
        else:
            return int(arr[-1])

    async def _close_position(self, reason="signal", signal_id: int = None):
        if self.position == 0:
            return
        try:
            result = await execute_close(self, reason, db, signal_id=signal_id)
            if result.get("pnl") is not None:
                risk_manager.record_trade(self.id, result["pnl"])
                self.trade_log.append({
                    "id": str(uuid.uuid4()),
                    "timestamp": datetime.now().isoformat(),
                    "instId": self.symbol,
                    "side": "sell" if self.position > 0 else "buy",
                    "sz": str(abs(self.position)),
                    "pnl": result["pnl"],
                    "state": "closed",
                    "bot_id": self.id,
                })
                print(f"[BOT {self.id}] CLOSED pnl={result['pnl']} capital={result['capital']}", flush=True)
        except Exception as e:
            print(f"[BOT {self.id}] CLOSE ERROR: {e}", flush=True)

    async def _open_position(self, side, price, signal_id: int = None):
        result = await execute_open(self, side, price, db, signal_id=signal_id)
        if result.get("error"):
            print(f"[BOT {self.id}] OPEN FAILED: {result.get('message')}", flush=True)
            return result
        self.trade_log.append({
            "id": str(uuid.uuid4()),
            "timestamp": datetime.now().isoformat(),
            "instId": self.symbol, "side": side,
            "sz": result.get("size", ""),
            "state": "filled",
            "bot_id": self.id,
        })
        print(f"[BOT {self.id}] OPENED side={side} sz={result.get('size')} pos={self.position:.6f}", flush=True)
        return result

    async def _loop(self):
        interval_map = {"1m": 60, "3m": 120, "5m": 240, "15m": 600,
                        "30m": 900, "1H": 1200, "4H": 3600, "1D": 14400}
        interval = interval_map.get(self.timeframe, 600)

        while self.status == "running":
            try:
                self.cycle_count += 1
                self.last_cycle_at = datetime.now().isoformat()

                client = self.client_manager.get_client()
                if not client:
                    self.error = "no_client"
                    await asyncio.sleep(interval)
                    continue

                from datetime import timedelta as _td
                bar_sec = {"1m":60,"3m":180,"5m":300,"15m":900,"30m":1800,
                           "1H":3600,"4H":14400,"1D":86400}.get(self.timeframe, 300)
                lookback = max(3600, bar_sec * 2000)
                start_dt = (datetime.now() - _td(seconds=lookback)).strftime("%Y-%m-%dT%H:%M:%S")
                candles = await ensure_candles(
                    self.symbol, self.timeframe, start_date=start_dt, live_limit=0
                )
                if not candles or len(candles) < 50:
                    self.error = "no_candles"
                    await asyncio.sleep(interval)
                    continue
                self.error = None

                df = pd.DataFrame(candles)
                df.columns = ["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"]
                for col in ["open", "high", "low", "close", "vol"]:
                    df[col] = df[col].astype(float)
                df["ts"] = pd.to_datetime(df["ts"].astype(int), unit="ms")
                df = df.sort_values("ts").reset_index(drop=True)

                fn = self._compile_fn()
                if not fn:
                    self.error = "no_generate_signals"
                    await asyncio.sleep(interval)
                    continue

                try:
                    signals = fn(df, self.params)
                except Exception as e:
                    self.error = f"signal_error: {str(e)}"
                    await asyncio.sleep(interval)
                    continue

                sig_arr = signals.values if hasattr(signals, "values") else signals
                # Auto-correct _is_diff: позиционные сигналы содержат только {-1,0,1}
                if self._is_diff and len(sig_arr) > 0:
                    uv = set(int(v) for v in np.unique(sig_arr))
                    if uv.issubset({-1, 0, 1}):
                        self._is_diff = False
                current_position = self._get_intended_position(sig_arr)

                current_price = float(df["close"].iloc[-1])
                try:
                    tk = await client.get_ticker(self.symbol)
                    if not tk.get("error") and tk.get("data"):
                        live_last = float(tk["data"][0].get("last", 0))
                        if live_last > 0:
                            current_price = live_last
                except Exception:
                    pass
                self.current_price = current_price
                self.unrealized_pnl = self.position * (current_price - self.entry_price) if self.position != 0 else 0.0
                ts_now = str(df["ts"].iloc[-1])

                had_position = self.position != 0
                wants_position = current_position != 0
                signal_changed = current_position != self.last_position

                if signal_changed:
                    signal_side = ("buy" if current_position == 1
                                   else "sell" if current_position == -1
                                   else "close")
                    signal_id = await db.save_signal(
                        self.id, ts_now, signal_side,
                        price=current_price,
                        status="pending",
                    )

                    if had_position:
                        await self._close_position("signal", signal_id=signal_id)
                    if wants_position:
                        side = "buy" if current_position == 1 else "sell"
                        size = (self.capital * 0.95) / current_price
                        risk = risk_manager.check_open(
                            self, side, size, current_price,
                            self._get_active_bot_count()
                        )
                        if not risk.ok:
                            self.error = f"risk_blocked: {risk.reason}"
                            await db.update_signal_status(
                                signal_id, "rejected",
                                reject_reason=risk.reason,
                            )
                            print(f"[BOT {self.id}] RISK BLOCKED: {risk.reason}", flush=True)
                        else:
                            # XGBoost gate: отсеиваем заведомо убыточные сигналы (только для momentum_pro)
                            xgb_allowed = True
                            if self._xgb_model is not None and self.strategy_id == "trend_momentum_pro":
                                try:
                                    feat = _compute_xgb_features(df)
                                    self._last_xgb_features = feat
                                    row = feat[-1:]

                                    swing_win = int(self.params.get("swing_window", 40))
                                    high = df["high"].values.astype(np.float64)
                                    low = df["low"].values.astype(np.float64)
                                    n = len(df)
                                    for j in range(n - swing_win, n):
                                        if high[j] == max(high[j - swing_win: j + swing_win + 1]):
                                            row[0, -2] = 1.0
                                        if low[j] == min(low[j - swing_win: j + swing_win + 1]):
                                            row[0, -1] = 1.0

                                    prob_win = self._xgb_model.predict_proba(row)[0][1]
                                    if prob_win < self._xgb_threshold:
                                        xgb_allowed = False
                                        self.error = f"xgb_rejected: prob_win={prob_win:.3f}"
                                        await db.update_signal_status(
                                            signal_id, "rejected",
                                            reject_reason=self.error,
                                        )
                                        print(f"[BOT {self.id}] XGB REJECTED: {self.error}", flush=True)
                                except Exception as e:
                                    print(f"[BOT {self.id}] XGB error: {e}", flush=True)

                            if xgb_allowed:
                                result = await self._open_position(side, current_price,
                                                                   signal_id=signal_id)
                                if result and result.get("ord_id"):
                                    await db.update_signal_status(
                                        signal_id, "executed",
                                        ord_id=result["ord_id"],
                                    )
                                else:
                                    await db.update_signal_status(
                                        signal_id, "failed",
                                    )
                    else:
                        await db.update_signal_status(signal_id, "executed")

                    self.last_position = current_position

                print(f"[BOT {self.id}] cycle={self.cycle_count} sig_pos={current_position} last_pos={self.last_position} pos={self.position:.6f} err={self.error or '-'} @ {ts_now}", flush=True)
                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                self.status = "stopped"
                break
            except Exception as e:
                self.error = str(e)
                import traceback
                traceback.print_exc()
                await asyncio.sleep(30)
