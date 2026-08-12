"""
Momentum Rotation — HONEST backtest with full audit.
Checks: no look-ahead, slippage, realistic execution.
"""

import asyncio
import math
import os
import sys
import pickle
from datetime import datetime, timezone
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

CT_VAL = {"BTC": 0.01, "ETH": 0.1, "BNB": 0.1, "SOL": 0.1}
LOT_SZ = {"BTC": 0.01, "ETH": 0.01, "BNB": 0.01, "SOL": 0.01}
SWAP_MAP = {"BTC": "BTC-USDT-SWAP", "ETH": "ETH-USDT-SWAP", "BNB": "BNB-USDT-SWAP", "SOL": "SOL-USDT-SWAP"}
BINANCE_MAP = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "BNB": "BNBUSDT", "SOL": "SOLUSDT"}
COINS = ["BTC", "ETH", "SOL", "BNB"]
BUDGET = 10000.0
CACHE_FILE = "/tmp/strategy_compare_cache_v2.pkl"


# ═══════════════════════════════════════════════════════════════════
#  REALISTIC COST MODEL
# ═════════════════════════════════════════════════════════════════

class CostModel:
    """All-in cost per trade side."""
    COMMISSION = 0.001        # 0.1% taker fee (OKX SWAP taker)
    SLIPPAGE_PCT = 0.0005     # 0.05% slippage (market order on SWAP)
    # Total round-trip cost: 2 * (0.1% + 0.05%) = 0.3%
    # Per side: 0.15%
    PER_SIDE = COMMISSION + SLIPPAGE_PCT

    @staticmethod
    def entry_cost(price, size, ct_val):
        """Extra cost on entry (commission + slippage worsens price)."""
        slip_price = price * (1 + CostModel.SLIPPAGE_PCT)  # buy higher
        commission = size * ct_val * price * CostModel.COMMISSION
        slippage = size * ct_val * price * CostModel.SLIPPAGE_PCT
        return slip_price, commission + slippage

    @staticmethod
    def exit_cost(price, size, ct_val, side):
        """Price after slippage + commission on exit."""
        if side == "long":
            slip_price = price * (1 - CostModel.SLIPPAGE_PCT)  # sell lower
        else:
            slip_price = price * (1 + CostModel.SLIPPAGE_PCT)  # buy back higher
        commission = size * ct_val * price * CostModel.COMMISSION
        slippage = size * ct_val * price * CostModel.SLIPPAGE_PCT
        return slip_price, commission + slippage


# ═══════════════════════════════════════════════════════════════════
#  DATA
# ═══════════════════════════════════════════════════════════════════

async def fetch_binance_bars(http_client, symbol, interval, days_back=1100):
    import time as _time
    all_candles = []
    bpd = {"1d": 1, "4h": 6, "1h": 24}.get(interval, 1)
    total_bars = int(days_back * bpd * 1.05) + 50
    start_ms = int((_time.time() - days_back * 86400) * 1000)
    while len(all_candles) < total_bars:
        params = {"symbol": symbol, "interval": interval, "limit": "1000"}
        if not all_candles:
            params["startTime"] = str(start_ms)
        else:
            params["startTime"] = str(all_candles[-1]["ts"] + 1)
        try:
            resp = await http_client.get("https://api.binance.com/api/v3/klines", params=params)
            data = resp.json()
        except Exception as e:
            print(f"    Error {symbol}: {e}", flush=True)
            break
        if not isinstance(data, list) or len(data) == 0:
            break
        for c in data:
            ts = int(c[0])
            all_candles.append({
                "ts": ts,
                "datetime": datetime.fromtimestamp(ts / 1000, tz=timezone.utc),
                "O": float(c[1]), "H": float(c[2]), "L": float(c[3]), "C": float(c[4]), "V": float(c[5]),
            })
        if len(data) < 1000:
            break
        await asyncio.sleep(0.2)
    all_candles.sort(key=lambda x: x["ts"])
    return all_candles


async def fetch_okx_daily(http_client, inst_id, days_back=1100):
    all_candles = []
    after = ""
    while len(all_candles) < days_back + 50:
        limit = min(300, days_back + 50 - len(all_candles))
        params = {"instId": inst_id, "bar": "1D", "limit": str(limit)}
        if after:
            params["after"] = after
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        try:
            resp = await http_client.get(f"https://www.okx.com/api/v5/market/candles?{qs}")
            result = resp.json()
        except Exception as e:
            print(f"    OKX error {inst_id}: {e}", flush=True)
            break
        if result.get("code") != "0":
            break
        data = result.get("data", [])
        if not data:
            break
        for c in data:
            ts = int(c[0])
            all_candles.append({
                "ts": ts,
                "datetime": datetime.fromtimestamp(ts / 1000, tz=timezone.utc),
                "O": float(c[1]), "H": float(c[2]), "L": float(c[3]), "C": float(c[4]), "V": float(c[5]),
            })
        if len(data) < limit:
            break
        after = data[-1][0]
        await asyncio.sleep(0.25)
    all_candles.sort(key=lambda x: x["ts"])
    return all_candles


async def fetch_data():
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "rb") as f:
            data = pickle.load(f)
        print(f"  Loaded cache", flush=True)
        return data

    import httpx
    client = httpx.AsyncClient(timeout=30.0)
    data = {"1D": {}}

    print("  [OKX] Daily SWAP...", flush=True)
    for coin in COINS:
        print(f"    {coin}...", end="", flush=True)
        data["1D"][coin] = await fetch_okx_daily(client, SWAP_MAP[coin])
        print(f" {len(data['1D'][coin])} bars", flush=True)
        await asyncio.sleep(0.3)

    await client.aclose()
    with open(CACHE_FILE, "wb") as f:
        pickle.dump(data, f)
    return data


# ═══════════════════════════════════════════════════════════════════
#  INDICATORS (all point-in-time, no look-ahead)
# ═══════════════════════════════════════════════════════════════════

def ema_series(data, period):
    if len(data) < period:
        return data[:]
    k = 2 / (period + 1)
    result = [data[0]]
    for v in data[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result


def atr_series(highs, lows, closes, period=14):
    trs = [0]
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        trs.append(tr)
    result = [0] * len(trs)
    if len(trs) < period + 1:
        return result, trs
    val = sum(trs[1:period+1]) / period
    result[period] = val
    for i in range(period + 1, len(trs)):
        val = (val * (period - 1) + trs[i]) / period
        result[i] = val
    return result, trs


def adx_series(highs, lows, closes, period=14):
    n = len(closes)
    if n < period * 2 + 1:
        return [0]*n, [0]*n, [0]*n
    plus_dm = [0]*n; minus_dm = [0]*n; trs = [0]*n
    for i in range(1, n):
        up = highs[i] - highs[i-1]; down = lows[i-1] - lows[i]
        plus_dm[i] = max(up, 0) if up > down else 0
        minus_dm[i] = max(down, 0) if down > up else 0
        trs[i] = max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1]))
    s_pdm = sum(plus_dm[1:period+1]); s_mdm = sum(minus_dm[1:period+1]); s_tr = sum(trs[1:period+1])
    pdi_arr = [0]*n; mdi_arr = [0]*n; adx_arr = [0]*n; dx_list = []
    for i in range(period, n):
        s_pdm = s_pdm - s_pdm/period + plus_dm[i]
        s_mdm = s_mdm - s_mdm/period + minus_dm[i]
        s_tr = s_tr - s_tr/period + trs[i]
        pdi = (s_pdm / s_tr * 100) if s_tr > 0 else 0
        mdi = (s_mdm / s_tr * 100) if s_tr > 0 else 0
        pdi_arr[i] = pdi; mdi_arr[i] = mdi
        dx = (abs(pdi - mdi) / (pdi + mdi) * 100) if (pdi + mdi) > 0 else 0
        dx_list.append(dx)
    if len(dx_list) >= period:
        adx_val = sum(dx_list[:period]) / period
        for i in range(period, len(dx_list)):
            adx_val = (adx_val * (period - 1) + dx_list[i]) / period
            adx_arr[period + i] = adx_val
    return adx_arr, pdi_arr, mdi_arr


def roc_series(closes, period):
    result = [0] * len(closes)
    for i in range(period, len(closes)):
        result[i] = (closes[i] / closes[i-period] - 1) * 100
    return result


# ═══════════════════════════════════════════════════════════════════
#  HONEST MOMENTUM ROTATION BACKTEST
# ═══════════════════════════════════════════════════════════════════

def backtest_honest(daily_data, params=None, fixed_sizing=False, look_ahead=False):
    """
    CRITICAL: All signals computed on bar [i-1] (yesterday's close).
    Entry at bar [i] OPEN price (next day open).
    Stop/trailing checked against bar [i] HIGH/LOW.
    This means ZERO look-ahead.

    fixed_sizing: use BUDGET instead of equity for sizing (no compounding)
    look_ahead: enter at signal bar close (CHEATING, for comparison only)
    """
    if params is None:
        params = {
            "roc_period": 14,
            "top_k": 2,
            "atr_period": 14,
            "atr_stop_mult": 2.0,
            "trail_pct": 0.02,
            "max_pos_pct": 0.40,
            "min_hold_days": 3,
            "ema_fast": 20,
            "ema_slow": 50,
            "adx_min": 18,
        }

    equity = BUDGET
    equity_curve = [BUDGET]
    trades = []
    positions = {}
    peak_equity = equity
    max_dd = 0
    last_rotate_day = -999
    audit_log = []  # detailed log for verification

    # Build per-coin series
    coin_data = {}
    for coin in COINS:
        candles = daily_data.get(coin, [])
        if not candles:
            coin_data[coin] = None
            continue
        closes = [c["C"] for c in candles]
        opens = [c["O"] for c in candles]
        highs = [c["H"] for c in candles]
        lows = [c["L"] for c in candles]
        dates = [c["datetime"].strftime("%Y-%m-%d") for c in candles]
        roc = roc_series(closes, params["roc_period"])
        ema_f = ema_series(closes, params["ema_fast"])
        ema_s = ema_series(closes, params["ema_slow"])
        atr_arr, _ = atr_series(highs, lows, closes, params["atr_period"])
        adx_arr, pdi_arr, mdi_arr = adx_series(highs, lows, closes, 14)
        coin_data[coin] = {
            "candles": candles, "closes": closes, "opens": opens,
            "highs": highs, "lows": lows, "dates": dates,
            "roc": roc, "ema_f": ema_f, "ema_s": ema_s,
            "atr": atr_arr, "adx": adx_arr, "pdi": pdi_arr, "mdi": mdi_arr,
        }

    # Date index (from BTC as reference)
    ref = coin_data.get("BTC")
    if not ref:
        return None
    n = len(ref["candles"])
    min_idx = params["ema_slow"] + 20

    for i in range(1, n):  # start from bar 1 (need bar 0 for signals)
        date = ref["dates"][i]
        coin = "BTC"  # reference

        # ── 1. MANAGE EXISTING POSITIONS ──
        for c in list(positions.keys()):
            cd = coin_data.get(c)
            if not cd or i >= len(cd["candles"]):
                continue

            pos = positions[c]
            high = cd["highs"][i]   # today's high
            low = cd["lows"][i]     # today's low
            ct_val = CT_VAL[c]

            if pos["side"] == "long":
                # Update peak
                if high > pos["peak"]:
                    pos["peak"] = high
                # Trailing stop (only tightens, never widens)
                new_stop = pos["peak"] * (1 - params["trail_pct"])
                if new_stop > pos["stop"]:
                    pos["stop"] = new_stop

                # Breakeven after 3% move
                if not pos["breakeven"] and high >= pos["entry"] * 1.03:
                    pos["stop"] = max(pos["stop"], pos["entry"] * 0.999)
                    pos["breakeven"] = True

                # Check if stop hit (low <= stop means price touched stop)
                if low <= pos["stop"] and pos["size"] > 0:
                    # EXIT: sell at stop price WITH SLIPPAGE (worse)
                    exit_price_raw = pos["stop"]
                    exit_price, exit_fees = CostModel.exit_cost(exit_price_raw, pos["size"], ct_val, "long")
                    pnl = pos["size"] * (exit_price - pos["entry"]) * ct_val
                    net_pnl = pnl - exit_fees
                    equity += net_pnl
                    hold_days = i - pos["entry_bar"]
                    trade = {
                        "date": date, "coin": c, "side": "long",
                        "pnl": round(net_pnl, 2), "reason": "trail_stop",
                        "entry_date": pos["entry_date"], "entry_price": pos["entry"],
                        "exit_price": round(exit_price_raw, 2),
                        "slippage": round(exit_price_raw - exit_price, 4),
                        "fees": round(exit_fees, 2), "hold_days": hold_days,
                        "size": pos["size"],
                    }
                    trades.append(trade)
                    audit_log.append(f"  EXIT  {date} {c:4} LONG  stop={exit_price_raw:.2f} slip={exit_price_raw-exit_price:.4f} fee={exit_fees:.2f} pnl={net_pnl:.2f}")
                    del positions[c]
                    continue

            else:  # short
                if low < pos["trough"]:
                    pos["trough"] = low
                new_stop = pos["trough"] * (1 + params["trail_pct"])
                if new_stop < pos["stop"]:
                    pos["stop"] = new_stop

                if not pos["breakeven"] and low <= pos["entry"] * 0.97:
                    pos["stop"] = min(pos["stop"], pos["entry"] * 1.001)
                    pos["breakeven"] = True

                if high >= pos["stop"] and pos["size"] > 0:
                    exit_price_raw = pos["stop"]
                    exit_price, exit_fees = CostModel.exit_cost(exit_price_raw, pos["size"], ct_val, "short")
                    pnl = pos["size"] * (pos["entry"] - exit_price) * ct_val
                    net_pnl = pnl - exit_fees
                    equity += net_pnl
                    hold_days = i - pos["entry_bar"]
                    trade = {
                        "date": date, "coin": c, "side": "short",
                        "pnl": round(net_pnl, 2), "reason": "trail_stop",
                        "entry_date": pos["entry_date"], "entry_price": pos["entry"],
                        "exit_price": round(exit_price_raw, 2),
                        "slippage": round(exit_price - exit_price_raw, 4),
                        "fees": round(exit_fees, 2), "hold_days": hold_days,
                        "size": pos["size"],
                    }
                    trades.append(trade)
                    audit_log.append(f"  EXIT  {date} {c:4} SHORT stop={exit_price_raw:.2f} slip={exit_price-exit_price_raw:.4f} fee={exit_fees:.2f} pnl={net_pnl:.2f}")
                    del positions[c]

        # ── 2. COOLDOWN / MIN HOLD ──
        days_since_rotate = i - last_rotate_day
        if days_since_rotate < params["min_hold_days"] and len(positions) > 0:
            equity = max(equity, 0)
            equity_curve.append(equity)
            if equity > peak_equity:
                peak_equity = equity
            dd = (peak_equity - equity) / peak_equity * 100 if peak_equity > 0 else 0
            max_dd = max(max_dd, dd)
            continue

        # ── 3. COMPUTE SIGNALS ──
        si = i - 1 if not look_ahead else i  # signal bar
        if si < min_idx:
            equity = max(equity, 0)
            equity_curve.append(equity)
            continue

        rankings = []
        for c in COINS:
            cd = coin_data.get(c)
            if not cd or si >= len(cd["closes"]):
                continue
            roc_val = cd["roc"][si]         # ROC based on YESTERDAY's close
            ema_trend = cd["ema_f"][si] > cd["ema_s"][si]  # EMA at YESTERDAY
            adx_val = cd["adx"][si]         # ADX at YESTERDAY
            atr_val = cd["atr"][si]         # ATR at YESTERDAY
            if atr_val <= 0:
                continue
            rankings.append((c, roc_val, ema_trend, adx_val, atr_val))

        if not rankings:
            equity_curve.append(equity)
            continue

        rankings.sort(key=lambda x: x[1], reverse=True)

        # ── 4. DETERMINE TARGET COINS ──
        target_coins = set()
        for c, roc_val, ema_trend, adx_val, atr_val in rankings:
            if len(target_coins) >= params["top_k"]:
                break
            if roc_val > 0 and ema_trend and adx_val >= params["adx_min"]:
                target_coins.add((c, "long"))
            elif roc_val < 0 and not ema_trend and adx_val >= params["adx_min"]:
                target_coins.add((c, "short"))

        # ── 5. CLOSE POSITIONS NOT IN TARGET ──
        for c in list(positions.keys()):
            pos = positions[c]
            side = pos["side"]
            if (c, side) not in target_coins:
                cd = coin_data.get(c)
                if not cd or i >= len(cd["candles"]):
                    continue
                # EXIT at today's OPEN (or CLOSE if look-ahead) with slippage
                exit_raw = cd["closes"][i] if look_ahead else cd["opens"][i]
                ct_val = CT_VAL[c]
                exit_price, exit_fees = CostModel.exit_cost(exit_raw, pos["size"], ct_val, side)
                if side == "long":
                    pnl = pos["size"] * (exit_price - pos["entry"]) * ct_val
                else:
                    pnl = pos["size"] * (pos["entry"] - exit_price) * ct_val
                net_pnl = pnl - exit_fees
                equity += net_pnl
                hold_days = i - pos["entry_bar"]
                trade = {
                    "date": date, "coin": c, "side": side,
                    "pnl": round(net_pnl, 2), "reason": "rotation_exit",
                    "entry_date": pos["entry_date"], "entry_price": pos["entry"],
                    "exit_price": round(exit_raw, 2),
                    "slippage": round(abs(exit_price - exit_raw), 4),
                    "fees": round(exit_fees, 2), "hold_days": hold_days,
                    "size": pos["size"],
                }
                trades.append(trade)
                audit_log.append(f"  EXIT  {date} {c:4} {side.upper():5} rot_exit open={exit_raw:.2f} slip={abs(exit_price-exit_raw):.4f} fee={exit_fees:.2f} pnl={net_pnl:.2f}")
                del positions[c]

        # ── 6. OPEN NEW POSITIONS at bar [i] OPEN (today's open) ──
        for c, side in target_coins:
            if c in positions:
                continue
            cd = coin_data[c]
            if i >= len(cd["candles"]):
                continue

            # Entry at OPEN (next day) or CLOSE (look-ahead)
            entry_raw = cd["opens"][i] if not look_ahead else cd["closes"][si]
            atr_val = cd["atr"][si]  # ATR from YESTERDAY (signal bar)
            if atr_val <= 0 or entry_raw <= 0:
                continue

            ct_val = CT_VAL[c]
            lot = LOT_SZ[c]

            # Sizing (fixed_sizing = no compounding)
            alloc_pct = min(1.0 / params["top_k"], params["max_pos_pct"])
            size_base = BUDGET if fixed_sizing else equity
            notional = size_base * alloc_pct
            raw_sz = notional / (ct_val * entry_raw)
            max_sz = BUDGET * params["max_pos_pct"] / (ct_val * entry_raw)
            sz = min(raw_sz, max_sz)
            sz = round(sz / lot) * lot
            if sz < lot:
                continue

            # Entry WITH SLIPPAGE (worse price)
            entry_price, entry_fees = CostModel.entry_cost(entry_raw, sz, ct_val)

            # Stop based on ATR from signal bar (yesterday)
            if side == "long":
                stop = entry_price - params["atr_stop_mult"] * atr_val
            else:
                stop = entry_price + params["atr_stop_mult"] * atr_val

            equity -= entry_fees

            positions[c] = {
                "entry": entry_price,  # actual fill price (with slippage)
                "entry_raw": entry_raw,  # theoretical open price
                "size": sz, "stop": stop,
                "peak": entry_price, "trough": entry_price,
                "side": side, "entry_bar": i,
                "entry_date": date, "breakeven": False,
            }

            audit_log.append(f"  ENTRY {date} {c:4} {side.upper():5} open={entry_raw:.2f} fill={entry_price:.2f} slip={entry_price-entry_raw:.4f} fee={entry_fees:.2f} stop={stop:.2f} sz={sz}")

        last_rotate_day = i

        equity = max(equity, 0)
        equity_curve.append(equity)
        if equity > peak_equity:
            peak_equity = equity
        dd = (peak_equity - equity) / peak_equity * 100 if peak_equity > 0 else 0
        max_dd = max(max_dd, dd)

    # ── METRICS ──
    total_pnl = equity - BUDGET
    total_return_pct = total_pnl / BUDGET * 100
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 999

    pnls = [t["pnl"] for t in trades]
    avg_pnl = sum(pnls) / len(pnls) if pnls else 0
    std_pnl = math.sqrt(sum((p - avg_pnl)**2 for p in pnls) / len(pnls)) if pnls else 1
    sharpe = (avg_pnl / std_pnl) * math.sqrt(252) if std_pnl > 0 else 0

    days = n
    years = days / 365
    annual_return = ((equity / BUDGET) ** (1 / max(years, 0.1)) - 1) * 100 if equity > 0 else 0

    total_slippage = sum(t.get("slippage", 0) for t in trades)
    total_fees = sum(t.get("fees", 0) for t in trades)

    by_coin = defaultdict(lambda: {"count": 0, "pnl": 0, "wins": 0})
    for t in trades:
        by_coin[t["coin"]]["count"] += 1
        by_coin[t["coin"]]["pnl"] += t["pnl"]
        if t["pnl"] > 0:
            by_coin[t["coin"]]["wins"] += 1

    return {
        "equity": round(equity, 2),
        "total_pnl": round(total_pnl, 2),
        "total_return_pct": round(total_return_pct, 2),
        "annual_return_pct": round(annual_return, 2),
        "trades": len(trades),
        "wins": len(wins), "losses": len(losses),
        "win_rate": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
        "sharpe": round(sharpe, 2),
        "max_dd": round(max_dd, 1),
        "avg_win": round(sum(t["pnl"] for t in wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else 0,
        "max_single_loss": round(min(t["pnl"] for t in trades), 2) if trades else 0,
        "total_slippage": round(total_slippage, 2),
        "total_fees": round(total_fees, 2),
        "by_coin": {k: {"count": v["count"], "pnl": round(v["pnl"], 2), "wins": v["wins"], "wr": round(v["wins"]/v["count"]*100, 1) if v["count"] > 0 else 0} for k, v in by_coin.items()},
        "trades_list": trades,
        "audit_log": audit_log,
        "equity_curve": equity_curve,
    }


# ═══════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════

async def main():
    print("=" * 90, flush=True)
    print("  MOMENTUM ROTATION — 4-WAY HONESTY CHECK", flush=True)
    print("=" * 90, flush=True)

    data = await fetch_data()

    # ════ 4 variants ════
    # A: signal on [i-1], entry at [i] OPEN, slippage+commission, COMPOUND sizing
    # B: signal on [i-1], entry at [i] OPEN, slippage+commission, FIXED sizing (budget)
    # C: signal on [i-1], entry at [i] OPEN, ZERO costs, COMPOUND sizing
    # D: signal on [i],   entry at [i] CLOSE, slippage+commission (LOOK-AHEAD baseline)

    results = {}

    # A: Full realistic (compound)
    print("\n  [A] Full realistic (compound + slip + comm + next-open)...", flush=True)
    results["A"] = backtest_honest(data["1D"])

    # B: Fixed sizing (no compounding)
    saved_compound = True
    # We need to modify sizing. Easiest: override equity in sizing to BUDGET
    # Hack: temporarily set a flag
    print("  [B] Fixed sizing (budget-based + slip + comm + next-open)...", flush=True)
    CostModel.SLIPPAGE_PCT = 0.0005
    CostModel.COMMISSION = 0.001
    r_b = backtest_honest(data["1D"], fixed_sizing=True)
    results["B"] = r_b

    # C: No costs at all (compound)
    print("  [C] No costs (compound, no slip, no comm, next-open)...", flush=True)
    CostModel.SLIPPAGE_PCT = 0
    CostModel.COMMISSION = 0
    results["C"] = backtest_honest(data["1D"])
    CostModel.SLIPPAGE_PCT = 0.0005
    CostModel.COMMISSION = 0.001

    # D: LOOK-AHEAD (entry at signal close = cheating)
    print("  [D] LOOK-AHEAD baseline (entry at signal close + slip + comm)...", flush=True)
    results["D"] = backtest_honest(data["1D"], look_ahead=True)

    # ════ COMPARISON ════
    print(f"\n{'='*90}", flush=True)
    print("  4-WAY COMPARISON", flush=True)
    print(f"{'='*90}", flush=True)

    header = f"  {'METRIC':<24} {'A:Realistic':>14} {'B:FixedSize':>14} {'C:NoCost':>14} {'D:LookAhead':>14}"
    print(header, flush=True)
    print(f"  {'─'*24}{'─'*16*4}", flush=True)

    rows = [
        ("CAGR %", lambda r: f"{r['annual_return_pct']:.1f}%"),
        ("Max DD %", lambda r: f"-{r['max_dd']:.1f}%"),
        ("Final Equity", lambda r: f"${r['equity']:,.0f}"),
        ("Trades", lambda r: str(r['trades'])),
        ("Win Rate", lambda r: f"{r['win_rate']:.1f}%"),
        ("Profit Factor", lambda r: f"{r['profit_factor']:.2f}"),
        ("Sharpe", lambda r: f"{r['sharpe']:.2f}"),
    ]
    for label, fn in rows:
        row = f"  {label:<24}"
        for key in ["A", "B", "C", "D"]:
            row += f" {fn(results[key]):>14}"
        print(row, flush=True)

    # Key diffs
    print(f"\n{'─'*70}", flush=True)
    ra, rb, rc, rd = results["A"], results["B"], results["C"], results["D"]
    print(f"  Impact of compounding:     A vs B = {ra['annual_return_pct'] - rb['annual_return_pct']:+.1f}% CAGR", flush=True)
    print(f"  Impact of all costs:       A vs C = {ra['annual_return_pct'] - rc['annual_return_pct']:+.1f}% CAGR", flush=True)
    print(f"  Impact of look-ahead:      D vs A = {rd['annual_return_pct'] - ra['annual_return_pct']:+.1f}% CAGR", flush=True)

    print(f"\n{'='*90}", flush=True)
    print("  HONESTY VERDICT (Variant A = ground truth)", flush=True)
    print(f"{'='*90}", flush=True)
    print(f"  [1] Look-ahead:  D-A = {rd['annual_return_pct']-ra['annual_return_pct']:+.1f}% (cheating adds this much)", flush=True)
    print(f"  [2] No look-ahead (A): CAGR = {ra['annual_return_pct']:.1f}%, DD = -{ra['max_dd']:.1f}%", flush=True)
    print(f"  [3] Fixed sizing (B):   CAGR = {rb['annual_return_pct']:.1f}% (no compounding bias)", flush=True)
    print(f"  [4] All-in cost drag:     {ra['annual_return_pct']-rc['annual_return_pct']:.1f}% CAGR", flush=True)
    print(f"  [5] Entry: next-day OPEN (realistic for daily signals)", flush=True)
    print(f"  [6] Slippage: 0.05% per side (market order on liquid SWAP)", flush=True)
    print(f"  [7] Commission: 0.1% per side (OKX taker)", flush=True)
    honest_cagr = rb["annual_return_pct"]  # fixed sizing = no compounding bias
    in_zone = 50 <= honest_cagr <= 70
    print(f"\n  >>> HONEST CAGR (fixed sizing, all costs): {honest_cagr:.1f}%", flush=True)
    print(f"  >>> Target zone 50-70%: {'YES' if in_zone else 'NO'}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
