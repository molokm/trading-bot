"""Real-data backtest engine for the Backtest UI.

Fetches live candles from the public OKX market API (no credentials needed),
runs the momentum strategy and returns a result object shaped for the
frontend Backtest page. Mirrors the validated `backtest_momentum.py` logic.
"""

import asyncio
import math
import time
from datetime import datetime, timezone

import httpx
import numpy as np
import pandas as pd

OKX_MARKET_URL = "https://www.okx.com/api/v5/market/candles"
OKX_HISTORY_URL = "https://www.okx.com/api/v5/market/history-candles"

# ── Strategy config (V3 rotation, matches live bot: rotation_strategy.py) ──
DEFAULT_CAPITAL = 10_000.0
RISK_PER_TRADE = 0.05
COMMISSION_PCT = 0.001
SLIPPAGE_PCT = 0.0005
MAX_CONCURRENT = 2          # top_k

ROC_PERIOD = 14
EMA_FAST = 20
EMA_SLOW = 50
ADX_PERIOD = 14
ADX_THRESHOLD = 22.0
MIN_ROC = 2.0               # min |roc| to even rank a coin
ATR_PERIOD = 14
ATR_STOP_MULT = 3.5
TRAIL_ATR_MULT = 0.1
BREAKEVEN_PCT = 0.02
PARTIAL_TP_PCT = 0.10
PARTIAL_TP_RATIO = 0.5
RSI_PERIOD = 14
RSI_LONG_MAX = 75.0
RSI_SHORT_MIN = 25.0
VOL_MULT = 1.5
CORR_THRESHOLD = 0.7
SMA_LONG = 200
MIN_HOLD_BARS = 3
MAX_LEVERAGE = 2.0
MAX_MARGIN_PCT = 2.0
ALLOW_SHORT = True

# Warmup needed for EMA50/SMA200/ADX14 indicators before any signal can fire.
MIN_CANDLES_PER_PAIR = 130
# Upper bound on candles fetched per pair (covers a full year on 1H bars
# and a 7-day window on 1m bars).
MAX_CANDLES_PER_PAIR = 12000

PERIOD_DAYS = {"7d": 7, "30d": 30, "90d": 90, "1y": 365}
TF_MINUTES = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
              "1h": 60, "2h": 120, "4h": 240, "6h": 360, "12h": 720, "1d": 1440}
# UI timeframe -> OKX bar parameter (OKX is case-sensitive: 1H / 1D).
BAR_MAP = {"1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
           "1h": "1H", "2h": "2H", "4h": "4H", "6h": "6H", "12h": "12H", "1d": "1D"}


def _pair_label(inst_id: str) -> str:
    for suffix in ("-USDT-SWAP", "-USDT", "-USDC-SWAP", "-USDC"):
        if inst_id.endswith(suffix):
            return inst_id[: -len(suffix)]
    return inst_id.split("-")[0]


# ── Indicators ──

def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(s, n=14):
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss)


def atr(df, n=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def roc(s, n):
    return s.pct_change(n) * 100


def sma(s, n):
    return s.rolling(n).mean()


def adx(df, n=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    up = h - h.shift(1)
    down = l.shift(1) - l
    plus_dm = np.where((up > down) & (up > 0), up, 0)
    minus_dm = np.where((down > up) & (down > 0), down, 0)
    tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr_val = tr.ewm(alpha=1 / n, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / atr_val
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / atr_val
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_val = dx.ewm(alpha=1 / n, adjust=False).mean()
    return adx_val, plus_di, minus_di


def enrich(df):
    df = df.copy()
    df["EMA_fast"] = ema(df["Close"], EMA_FAST)
    df["EMA_slow"] = ema(df["Close"], EMA_SLOW)
    df["ROC"] = roc(df["Close"], ROC_PERIOD)
    df["ADX"], df["Plus_DI"], df["Minus_DI"] = adx(df, ADX_PERIOD)
    df["ATR"] = atr(df, ATR_PERIOD)
    df["RSI"] = rsi(df["Close"], RSI_PERIOD)
    df["SMA_long"] = sma(df["Close"], SMA_LONG)
    # Rolling average of ATR over the trailing 30 bars (volatility filter).
    df["ATR_avg30"] = df["ATR"].rolling(30).mean()
    return df


# ── Data fetch (public OKX market candles, paginated) ──

async def _fetch_page(client, url, params):
    try:
        resp = await client.get(url, params=params)
        return resp.json()
    except Exception as e:
        raise RuntimeError(f"Ошибка запроса свечей: {e}")


async def fetch_candles(inst_id: str, bar: str, total: int) -> pd.DataFrame:
    """Fetch up to `total` candles of `inst_id`/`bar` from OKX (newest first).

    `market/candles` only exposes the most recent ~1440 bars, so older history
    is paginated from `market/history-candles` (100/page). This lets a full
    year load on hourly (and coarser) timeframes.
    """
    all_candles = []
    after = ""
    async with httpx.AsyncClient(timeout=30.0) as _client:
        # 1) Recent window via market/candles (300/page).
        while len(all_candles) < total:
            params = {"instId": inst_id, "bar": bar, "limit": "300"}
            if after:
                params["after"] = after
            data = await _fetch_page(_client, OKX_MARKET_URL, params)
            if data.get("code") != "0" or not data.get("data"):
                break
            candles = data["data"]
            all_candles.extend(candles)
            after = candles[-1][0]
            if len(candles) < 300:
                break
            await asyncio.sleep(0.1)

        # 2) Older history via market/history-candles (100/page) if needed.
        while len(all_candles) < total:
            params = {"instId": inst_id, "bar": bar, "limit": "100", "after": after}
            data = await _fetch_page(_client, OKX_HISTORY_URL, params)
            if data.get("code") != "0" or not data.get("data"):
                break
            candles = data["data"]
            all_candles.extend(candles)
            after = candles[-1][0]
            if len(candles) < 100:
                break
            await asyncio.sleep(0.08)

    if not all_candles:
        raise RuntimeError(f"Нет данных по инструменту {inst_id}")

    df = pd.DataFrame(all_candles, columns=[
        "ts", "Open", "High", "Low", "Close", "Volume", "VolCcy", "VolCcyQuote", "Confirm"
    ])
    df["ts"] = pd.to_datetime(df["ts"].astype("int64"), unit="ms")
    df = df[["ts", "Open", "High", "Low", "Close", "Volume"]].astype({"Open": float, "High": float,
                                                                       "Low": float, "Close": float,
                                                                       "Volume": float})
    df.set_index("ts", inplace=True)
    df.sort_index(inplace=True)
    # Drop any duplicate timestamps (history-candles may overlap the recent window).
    df = df[~df.index.duplicated(keep="first")]
    return df[["Open", "High", "Low", "Close", "Volume"]]


# ── Backtest loop ──

def _returns_series(close_series, n=30):
    """Rolling daily returns for correlation (last n bars)."""
    rets = close_series.pct_change().tail(n).dropna().tolist()
    return rets


def _corr(a, b):
    a = list(a)[-30:]
    b = list(b)[-30:]
    if len(a) < 10 or len(b) < 10:
        return 0.0
    n = min(len(a), len(b))
    a, b = a[-n:], b[-n:]
    ma, mb = sum(a) / n, sum(b) / n
    cov = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    sa = math.sqrt(sum((x - ma) ** 2 for x in a))
    sb = math.sqrt(sum((y - mb) ** 2 for y in b))
    if sa == 0 or sb == 0:
        return 0.0
    return cov / (sa * sb)


def run_backtest(data_dict: dict, initial_capital: float):
    """V3 momentum rotation over aligned daily bars.

    Rules match the live bot (rotation_strategy.py):
      - Signal on bar T close (causal indicators) -> entry at bar T+1 open.
      - Direction: roc > +min_roc + EMA trend + ADX>=22 -> long;
        roc < -min_roc + no EMA trend + ADX>=22 -> short (if allow_short).
      - Filters: volatility (ATR vs avg30), RSI extremes, BTC 200-MA regime,
        min |roc|, correlation between held pairs.
      - Exit: initial stop 3.5xATR, dynamic trailing 0.1xATR, breakeven after
        2%, partial TP: close 50% at +10%.
    """
    equity = initial_capital
    positions = {}                     # sym -> pos dict
    all_trades = []
    equity_curve = [{"trade": 0, "value": initial_capital}]
    last_rotate = -10**9

    # Common index = bars present in every pair.
    common = set(data_dict[list(data_dict)[0]].index)
    for df in data_dict.values():
        common &= set(df.index)
    all_dates = sorted(common)

    # Pre-build bar lookup per symbol for O(1) index access.
    idx_map = {sym: {d: i for i, d in enumerate(df.index)} for sym, df in data_dict.items()}

    def btc_above_200ma(date):
        btc_df = data_dict.get("BTC-USDT-SWAP")
        if btc_df is None:
            return True
        btc_dates = list(btc_df.index)
        if date not in idx_map["BTC-USDT-SWAP"]:
            return True
        i = idx_map["BTC-USDT-SWAP"][date]
        if i < 1:
            return True
        prev = btc_df.iloc[i - 1]
        if pd.isna(prev["SMA_long"]) or prev["SMA_long"] <= 0:
            return True
        return prev["Close"] > prev["SMA_long"]

    for date in all_dates:
        date_i = all_dates.index(date)
        mtm = {sym: df.loc[date, "Close"] for sym, df in data_dict.items() if date in df.index}

        # ── 1. Manage open positions (pessimistic: stop vs H/L first) ──
        for sym in list(positions.keys()):
            pos = positions[sym]
            row = data_dict[sym].loc[date]
            trail = pos["atr"] * TRAIL_ATR_MULT
            if trail <= 0:
                trail = pos["entry_price"] * 0.02

            hit = False
            exit_raw = None
            reason = "trail_stop"

            if pos["side"] == "long":
                if row["Low"] <= pos["stop"]:
                    hit, exit_raw, reason = True, pos["stop"], "stop_loss"
                else:
                    if row["High"] > pos["peak"]:
                        pos["peak"] = row["High"]
                        ns = pos["peak"] - trail
                        if ns > pos["stop"]:
                            pos["stop"] = ns
                    if not pos["breakeven"] and row["Close"] >= pos["entry_price"] * (1 + BREAKEVEN_PCT):
                        pos["stop"] = max(pos["stop"], pos["entry_price"] * 0.999)
                        pos["breakeven"] = True
                    if not pos["partial"] and row["High"] >= pos["entry_price"] * (1 + PARTIAL_TP_PCT):
                        exit_raw, reason = pos["entry_price"] * (1 + PARTIAL_TP_PCT), "partial_tp"
                        hit = True
            else:
                if row["High"] >= pos["stop"]:
                    hit, exit_raw, reason = True, pos["stop"], "stop_loss"
                else:
                    if row["Low"] < pos["peak"]:
                        pos["peak"] = row["Low"]
                        ns = pos["peak"] + trail
                        if ns < pos["stop"]:
                            pos["stop"] = ns
                    if not pos["breakeven"] and row["Close"] <= pos["entry_price"] * (1 - BREAKEVEN_PCT):
                        pos["stop"] = min(pos["stop"], pos["entry_price"] * 1.001)
                        pos["breakeven"] = True
                    if not pos["partial"] and row["Low"] <= pos["entry_price"] * (1 - PARTIAL_TP_PCT):
                        exit_raw, reason = pos["entry_price"] * (1 - PARTIAL_TP_PCT), "partial_tp"
                        hit = True

            if hit and reason == "partial_tp" and not pos["partial"]:
                # Close PARTIAL_TP_RATIO of the position, keep the rest trailing.
                close_shares = pos["shares"] * PARTIAL_TP_RATIO
                if close_shares <= 0:
                    hit = False
                else:
                    fill = exit_raw * (1 - SLIPPAGE_PCT) if pos["side"] == "long" else exit_raw * (1 + SLIPPAGE_PCT)
                    pnl = close_shares * (fill - pos["entry_price"]) - close_shares * fill * COMMISSION_PCT
                    if pos["side"] == "short":
                        pnl = close_shares * (pos["entry_price"] - fill) - close_shares * fill * COMMISSION_PCT
                    equity += pnl
                    entry_val = close_shares * pos["entry_price"]
                    pnl_pct = (pnl / entry_val * 100) if entry_val else 0.0
                    all_trades.append({
                        "symbol": sym, "entry_time": _iso(pos["entry_date"]), "exit_time": _iso(date),
                        "pair": _pair_label(sym), "side": "LONG" if pos["side"] == "long" else "SHORT",
                        "entry_px": round(pos["entry_price"], 6), "exit_px": round(fill, 6),
                        "pnl": round(pnl, 4), "pnl_pct": round(pnl_pct, 4),
                        "reason": "partial_tp", "r_multiple": round(PARTIAL_TP_PCT / 0.01 * 0.1, 4),
                    })
                    equity_curve.append({"trade": len(all_trades), "value": round(equity, 4)})
                    pos["shares"] -= close_shares
                    pos["partial"] = True
                    hit = False

            if hit:
                fill = exit_raw * (1 - SLIPPAGE_PCT) if pos["side"] == "long" else exit_raw * (1 + SLIPPAGE_PCT)
                pnl = pos["shares"] * (fill - pos["entry_price"]) - pos["shares"] * fill * COMMISSION_PCT
                if pos["side"] == "short":
                    pnl = pos["shares"] * (pos["entry_price"] - fill) - pos["shares"] * fill * COMMISSION_PCT
                equity += pnl
                entry_val = pos["shares"] * pos["entry_price"]
                pnl_pct = (pnl / entry_val * 100) if entry_val else 0.0
                r_dist = abs(pos["entry_price"] - pos["stop_at_entry"])
                r_mult = ((fill - pos["entry_price"]) / r_dist) if pos["side"] == "long" and r_dist > 0 else 0.0
                if pos["side"] == "short" and r_dist > 0:
                    r_mult = ((pos["entry_price"] - fill) / r_dist)
                all_trades.append({
                    "symbol": sym, "entry_time": _iso(pos["entry_date"]), "exit_time": _iso(date),
                    "pair": _pair_label(sym), "side": "LONG" if pos["side"] == "long" else "SHORT",
                    "entry_px": round(pos["entry_price"], 6), "exit_px": round(fill, 6),
                    "pnl": round(pnl, 4), "pnl_pct": round(pnl_pct, 4),
                    "reason": reason, "r_multiple": round(r_mult, 4),
                })
                equity_curve.append({"trade": len(all_trades), "value": round(equity, 4)})
                del positions[sym]

        # ── 2. Rotation (min-hold cooldown) ──
        if date_i - last_rotate < MIN_HOLD_BARS and positions:
            continue

        btc_above = btc_above_200ma(date)

        ranked = []
        for sym, df in data_dict.items():
            i = idx_map[sym].get(date)
            if i is None or i < 1:
                continue
            prev = df.iloc[i - 1]           # signal bar = yesterday's close
            atr_val = prev["ATR"]
            if pd.isna(atr_val) or atr_val <= 0:
                continue

            # Volatility filter: skip if ATR > avg30 * vol_mult
            avg30 = prev["ATR_avg30"]
            if not pd.isna(avg30) and avg30 > 0 and atr_val > avg30 * VOL_MULT:
                continue

            ema_trend = prev["EMA_fast"] > prev["EMA_slow"]
            rsi_val = prev["RSI"]
            roc_val = prev["ROC"]
            adx_val = prev["ADX"]

            if pd.isna(roc_val) or pd.isna(adx_val) or pd.isna(rsi_val):
                continue

            # RSI filters
            if rsi_val > RSI_LONG_MAX and ema_trend:
                continue
            if rsi_val < RSI_SHORT_MIN and not ema_trend:
                continue
            # BTC 200-MA regime: no longs below it
            if not btc_above and roc_val > 0 and ema_trend:
                continue
            # min |roc|
            if abs(roc_val) < MIN_ROC:
                continue

            trend_val = (prev["EMA_fast"] - prev["EMA_slow"]) / prev["EMA_slow"] * 100 if prev["EMA_slow"] > 0 else 0
            score = roc_val * 0.5 + trend_val * 0.3 + (adx_val / 50) * 0.2
            rets = _returns_series(df["Close"].iloc[:i])
            ranked.append({
                "sym": sym, "score": score, "roc": roc_val, "ema_trend": ema_trend,
                "adx": adx_val, "atr": atr_val, "rets": rets,
            })

        ranked.sort(key=lambda x: x["score"], reverse=True)

        targets = []
        for row in ranked:
            if len(targets) >= MAX_CONCURRENT:
                break
            if row["roc"] > 0 and row["ema_trend"] and row["adx"] >= ADX_THRESHOLD:
                side = "long"
            elif (ALLOW_SHORT and row["roc"] < 0 and not row["ema_trend"]
                  and row["adx"] >= ADX_THRESHOLD):
                side = "short"
            else:
                continue
            # Correlation vs held / selected
            corr_ok = True
            check_against = [positions[p]["rets"] for p in positions] + [t["rets"] for t in targets]
            for held in check_against:
                if abs(_corr(row["rets"], held)) > CORR_THRESHOLD:
                    corr_ok = False
                    break
            if not corr_ok:
                continue
            targets.append({"sym": row["sym"], "side": side, "atr": row["atr"], "rets": row["rets"]})

        # Close rotated-out positions at TODAY OPEN (no longer in top-k).
        target_set = {(t["sym"], t["side"]) for t in targets}
        for sym in list(positions.keys()):
            pos = positions[sym]
            if (sym, pos["side"]) in target_set:
                continue
            i = idx_map[sym][date]
            exit_raw = data_dict[sym].iloc[i]["Open"]
            fill = exit_raw * (1 - SLIPPAGE_PCT) if pos["side"] == "long" else exit_raw * (1 + SLIPPAGE_PCT)
            pnl = pos["shares"] * (fill - pos["entry_price"]) - pos["shares"] * fill * COMMISSION_PCT
            if pos["side"] == "short":
                pnl = pos["shares"] * (pos["entry_price"] - fill) - pos["shares"] * fill * COMMISSION_PCT
            equity += pnl
            entry_val = pos["shares"] * pos["entry_price"]
            pnl_pct = (pnl / entry_val * 100) if entry_val else 0.0
            r_dist = abs(pos["entry_price"] - pos["stop_at_entry"])
            r_mult = ((fill - pos["entry_price"]) / r_dist) if pos["side"] == "long" and r_dist > 0 else 0.0
            if pos["side"] == "short" and r_dist > 0:
                r_mult = ((pos["entry_price"] - fill) / r_dist)
            all_trades.append({
                "symbol": sym, "entry_time": _iso(pos["entry_date"]), "exit_time": _iso(date),
                "pair": _pair_label(sym), "side": "LONG" if pos["side"] == "long" else "SHORT",
                "entry_px": round(pos["entry_price"], 6), "exit_px": round(fill, 6),
                "pnl": round(pnl, 4), "pnl_pct": round(pnl_pct, 4),
                "reason": "rotation_exit", "r_multiple": round(r_mult, 4),
            })
            equity_curve.append({"trade": len(all_trades), "value": round(equity, 4)})
            del positions[sym]

        # Open new at today's OPEN (T+1)
        for t in targets:
            sym = t["sym"]
            if sym in positions:
                continue
            i = idx_map[sym][date]
            row = data_dict[sym].iloc[i]
            entry_price = row["Open"]
            atr_val = t["atr"]
            stop_dist = atr_val * ATR_STOP_MULT
            if t["side"] == "long":
                fill = entry_price * (1 + SLIPPAGE_PCT)
                stop = fill - stop_dist
            else:
                fill = entry_price * (1 - SLIPPAGE_PCT)
                stop = fill + stop_dist
            if stop_dist <= 0 or fill <= 0:
                continue

            # Risk-based sizing (leverage-capped)
            lev = max(1.0, min(MAX_LEVERAGE, 1.0 / ((atr_val / fill) * 2))) if fill > 0 else MAX_LEVERAGE
            risk_usd = equity * RISK_PER_TRADE
            notional = risk_usd / (stop_dist / fill) if fill > 0 else 0
            margin = notional / lev if lev > 0 else notional
            max_margin = equity * MAX_MARGIN_PCT
            if margin > max_margin:
                margin = max_margin
                notional = margin * lev
            shares = notional / fill if fill > 0 else 0
            if shares <= 0:
                continue

            equity -= shares * fill * COMMISSION_PCT
            positions[sym] = {
                "side": t["side"], "shares": shares, "entry_price": fill,
                "entry_date": date, "stop": stop, "stop_at_entry": stop,
                "peak": fill, "atr": atr_val, "breakeven": False, "partial": False,
                "rets": t["rets"],
            }
            all_trades.append({
                "symbol": sym, "entry_time": _iso(date), "exit_time": None,
                "pair": _pair_label(sym), "side": "LONG" if t["side"] == "long" else "SHORT",
                "entry_px": round(fill, 6), "exit_px": None,
                "pnl": round(-shares * fill * COMMISSION_PCT, 4), "pnl_pct": 0.0,
                "reason": "open", "r_multiple": 0.0,
            })
            last_rotate = date_i

    # Force-close at the end
    if positions:
        date = all_dates[-1]
        for sym in list(positions.keys()):
            pos = positions[sym]
            fill = data_dict[sym].loc[date, "Close"]
            fill = fill * (1 - SLIPPAGE_PCT) if pos["side"] == "long" else fill * (1 + SLIPPAGE_PCT)
            pnl = pos["shares"] * (fill - pos["entry_price"]) - pos["shares"] * fill * COMMISSION_PCT
            if pos["side"] == "short":
                pnl = pos["shares"] * (pos["entry_price"] - fill) - pos["shares"] * fill * COMMISSION_PCT
            equity += pnl
            entry_val = pos["shares"] * pos["entry_price"]
            pnl_pct = (pnl / entry_val * 100) if entry_val else 0.0
            all_trades.append({
                "symbol": sym, "entry_time": _iso(pos["entry_date"]), "exit_time": _iso(date),
                "pair": _pair_label(sym), "side": "LONG" if pos["side"] == "long" else "SHORT",
                "entry_px": round(pos["entry_price"], 6), "exit_px": round(fill, 6),
                "pnl": round(pnl, 4), "pnl_pct": round(pnl_pct, 4),
                "reason": "backtest_end", "r_multiple": 0.0,
            })
            equity_curve.append({"trade": len(all_trades), "value": round(equity, 4)})

    return all_trades, equity_curve


def _iso(ts) -> str:
    if isinstance(ts, (pd.Timestamp, datetime)):
        return ts.isoformat()
    return str(ts)


def sanitize(obj):
    """Recursively convert numpy types to native Python for JSON serialization."""
    if isinstance(obj, dict):
        return {k: sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [sanitize(v) for v in obj]
    if isinstance(obj, (np.floating, np.integer, np.bool_)):
        return obj.item()
    if isinstance(obj, (np.ndarray,)):
        return sanitize(obj.tolist())
    if isinstance(obj, (pd.Timestamp,)):
        return obj.isoformat()
    return obj


# ── Metrics ──

def calc_metrics(trades, equity_curve, initial_capital, years, tf_minutes=1440):
    # Only count closed trades (entries with "open"/exit_time None excluded).
    trades = [t for t in trades if t.get("exit_time")]
    if not trades:
        return {"error": "no trades"}

    final = equity_curve[-1]["value"]
    total_return = (final / initial_capital - 1) * 100
    cagr = ((final / initial_capital) ** (1 / years) - 1) * 100 if years > 0 else 0.0

    eq = pd.Series([p["value"] for p in equity_curve])
    bar_returns = eq.pct_change().dropna()
    ann_factor = 365.0 * 24.0 * 60.0 / (tf_minutes or 1)
    sharpe = (bar_returns.mean() / bar_returns.std() * np.sqrt(ann_factor)) if bar_returns.std() > 0 else 0.0

    peak = eq.cummax()
    dd = (eq - peak) / peak
    max_dd = dd.min() * 100

    pnls = [t["pnl"] for t in trades]
    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p <= 0]
    win_rate = len(winners) / len(pnls) * 100 if pnls else 0
    avg_win = float(np.mean(winners)) if winners else 0.0
    avg_loss = float(np.mean(losers)) if losers else 0.0
    profit_factor = (sum(winners) / abs(sum(losers))) if losers else (float("inf") if winners else 0.0)

    r_multiples = [t["r_multiple"] for t in trades]
    avg_r = float(np.mean(r_multiples)) if r_multiples else 0.0

    by_sym = {}
    for t in trades:
        sym = t["pair"]
        entry = by_sym.setdefault(sym, {"trades": 0, "win_rate": 0, "total_pnl": 0, "avg_pnl": 0, "pnls": []})
        entry["trades"] += 1
        entry["pnls"].append(t["pnl"])
    for sym, s in by_sym.items():
        s["win_rate"] = sum(1 for p in s["pnls"] if p > 0) / len(s["pnls"]) * 100
        s["total_pnl"] = round(sum(s["pnls"]), 2)
        s["avg_pnl"] = round(sum(s["pnls"]) / len(s["pnls"]), 2)
        del s["pnls"]

    by_reason = {}
    for t in trades:
        r = t["reason"]
        entry = by_reason.setdefault(r, {"count": 0, "total_pnl": 0, "avg_pnl": 0, "pnls": []})
        entry["count"] += 1
        entry["pnls"].append(t["pnl"])
    for r, s in by_reason.items():
        s["total_pnl"] = round(sum(s["pnls"]), 2)
        s["avg_pnl"] = round(sum(s["pnls"]) / len(s["pnls"]), 2)
        del s["pnls"]

    return {
        "initial": round(initial_capital, 2),
        "final": round(final, 2),
        "total_return": round(total_return, 2),
        "total_return_pct": round(total_return, 2),
        "cagr": round(cagr, 2),
        "sharpe": round(sharpe, 2),
        "max_dd": round(max_dd, 2),
        "total_trades": len(trades),
        "win_rate": round(win_rate, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2) if np.isfinite(profit_factor) else None,
        "avg_r": round(avg_r, 2),
        "by_sym": by_sym,
        "by_reason": by_reason,
    }


def build_heatmap(trades):
    """Sum PnL by weekday(0=Mon) and hour for the day/hour heatmap."""
    buckets = {}
    for t in trades:
        try:
            dt = datetime.fromisoformat(t["exit_time"])
        except (TypeError, ValueError):
            continue
        key = (dt.weekday(), dt.hour)
        buckets[key] = buckets.get(key, 0.0) + t["pnl"]
    return [{"day": d, "hour": h, "value": round(v, 2)}
            for (d, h), v in sorted(buckets.items())]


# ── Orchestration ──

async def run_backtest_async(config: dict) -> dict:
    pairs = [p for p in (config.get("pairs") or []) if p]
    if not pairs:
        raise ValueError("Выберите хотя бы один инструмент")
    period = config.get("period", "30d")
    timeframe = config.get("timeframe", "1d")
    strategy = config.get("strategy", "momentum")
    capital = float(config.get("capital", DEFAULT_CAPITAL))

    if period not in PERIOD_DAYS:
        raise ValueError(f"Неизвестный период: {period}")
    if timeframe not in TF_MINUTES:
        raise ValueError(f"Неизвестный таймфрейм: {timeframe}")

    bar = BAR_MAP[timeframe]
    days = PERIOD_DAYS[period]
    needed = int(days * 24 * 60 / TF_MINUTES[timeframe])
    # Fetch extra warmup bars (EMA50/SMA200/ADX14/ATR30 need ~210 bars history)
    # so indicator values are valid across the whole requested window.
    warmup = 250
    total = min(max(needed + warmup, MIN_CANDLES_PER_PAIR + 40), MAX_CANDLES_PER_PAIR)

    if needed > MAX_CANDLES_PER_PAIR:
        raise ValueError(
            f"Слишком большой объём данных: {needed} свечей. "
            f"Для периода {period} на таймфрейме {timeframe} нужно {needed} свечей "
            f"(максимум {MAX_CANDLES_PER_PAIR}). Выберите таймфрейм покрупнее."
        )

    data_dict = {}
    notes = []
    for inst in pairs:
        df = await fetch_candles(inst, bar, total)
        if len(df) < MIN_CANDLES_PER_PAIR:
            raise ValueError(
                f"Инструмент {inst}: для периода {period} на таймфрейме {timeframe} "
                f"доступно только {len(df)} свечей, а стратегии нужно минимум "
                f"{MIN_CANDLES_PER_PAIR}. Выберите меньший таймфрейм или больший период."
            )
        data_dict[inst] = enrich(df)
        notes.append(f"{inst}: {len(df)} свечей")

    # Align to common range so all pairs cover the same bars.
    start = max(df.index[0] for df in data_dict.values())
    end = min(df.index[-1] for df in data_dict.values())
    years = (end - start).total_seconds() / (365.25 * 24 * 3600)
    for inst in data_dict:
        data_dict[inst] = data_dict[inst].loc[start:end]

    # Honor the requested period exactly: only run on the most recent `needed`
    # bars (warmup bars above are used solely for indicator history).
    start = end - pd.Timedelta(days=days - 1)
    for inst in data_dict:
        data_dict[inst] = data_dict[inst].loc[start:end]

    trades, equity_curve = run_backtest(data_dict, capital)
    metrics = calc_metrics(trades, equity_curve, capital, years, TF_MINUTES[timeframe])

    if metrics.get("error") == "no trades":
        raise ValueError(
            "Стратегия не нашла ни одной сделки за выбранный период. "
            "Попробуйте больший период или меньший таймфрейм."
        )

    # Frontend metric field names.
    metrics_ui = {
        "totalReturn": metrics["total_return"],
        "totalReturnPct": metrics["total_return_pct"],
        "winRate": metrics["win_rate"],
        "profitFactor": metrics["profit_factor"] or 0,
        "sharpe": metrics["sharpe"],
        "maxDD": abs(metrics["max_dd"]),
        "trades": metrics["total_trades"],
        "cagr": metrics["cagr"],
        "avgWin": metrics["avg_win"],
        "avgLoss": metrics["avg_loss"],
        "avgR": metrics["avg_r"],
        "bySym": metrics["by_sym"],
        "byReason": metrics["by_reason"],
    }

    # Map internal exit reasons to UI display reasons (sl/tp/trail/be/rotation).
    reason_map = {"stop_loss": "sl", "partial_tp": "tp", "trail_stop": "trail",
                  "rotation_exit": "rotation", "backtest_end": "backtest_end", "open": "open"}
    # Only include closed trades (entries tracked as "open" have no exit data).
    trade_list = []
    for t in trades:
        if not t.get("exit_time"):
            continue
        trade_list.append({
            **{k: v for k, v in t.items() if k not in ("symbol",)},
            "reason": reason_map.get(t["reason"], t["reason"]),
        })

    result = {
        "metrics": metrics_ui,
        "equityCurve": equity_curve,
        "tradeList": trade_list,
        "heatmap": build_heatmap(trades),
        "config": {
            **config,
            "strategy": strategy,
            "capital": capital,
            "runAt": datetime.now(timezone.utc).isoformat(),
        },
        "dataRange": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "bars": int(len(common_index_for(data_dict))),
            "years": round(years, 2),
        },
        "dataNote": "OKX market candles · " + ", ".join(notes),
    }
    return sanitize(result)


def common_index_for(data_dict):
    common = set(data_dict[list(data_dict)[0]].index)
    for df in data_dict.values():
        common &= set(df.index)
    return sorted(common)
