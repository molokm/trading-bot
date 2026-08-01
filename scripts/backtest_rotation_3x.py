#!/usr/bin/env python3
"""
Rotation Strategy Backtest — 3x Leverage, $10,000 Capital.
Exact match of live rotation_strategy.py logic.

Rules:
  1. Signal on bar T (yesterday) -> entry at bar T+1 OPEN (next day)
  2. ROC(14) ranking, top-2, EMA20>EMA50 + ADX>=18 = long, else short
  3. ATR(14) initial stop x2, trailing stop 2%, breakeven after 3%
  4. Min hold 3 days
  5. Leverage 3x, max 40% margin per position
  6. Commission 0.1% taker + 0.05% slippage per side
  7. NO compounding (fixed $10k budget for sizing)
  8. Pessimistic: stop checked BEFORE peak update
"""

import asyncio
import math
import sys
import time as _time
from datetime import datetime, timezone

import httpx

# ═══ Constants (same as live) ═══
CT_VAL = {"BTC": 0.01, "ETH": 0.1, "BNB": 0.1, "SOL": 0.1}
LOT_SZ = {"BTC": 0.01, "ETH": 0.01, "BNB": 0.01, "SOL": 0.01}
BINANCE_MAP = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "BNB": "BNBUSDT", "SOL": "SOLUSDT"}
COINS = ["BTC", "ETH", "SOL", "BNB"]

# ═══ Strategy params (same as live RotationConfig) ═══
CAPITAL     = 10_000.0
LEVERAGE    = 3.0
TOP_K       = 2
ROC_PERIOD  = 14
EMA_FAST    = 20
EMA_SLOW    = 50
ATR_PERIOD  = 14
ATR_STOP_M  = 2.0
TRAIL_PCT   = 0.02
BE_PCT      = 0.03
ADX_MIN     = 18.0
MIN_HOLD    = 3
MAX_POS_PCT = 0.40
COMMISSION  = 0.001   # 0.1% OKX SWAP taker
SLIPPAGE    = 0.0005  # 0.05%


# ═══ Indicators ═══

def ema_series(data, period):
    if len(data) < period:
        return [0.0] * len(data)
    k = 2 / (period + 1)
    result = [data[0]]
    for v in data[1:]:
        result.append(v * k + result[-1] * (1 - k))
    return result

def atr_series(highs, lows, closes, period=14):
    n = len(closes)
    trs = [0.0] * n
    for i in range(1, n):
        trs[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
    result = [0.0] * n
    if n < period + 1:
        return result
    val = sum(trs[1:period+1]) / period
    result[period] = val
    for i in range(period + 1, n):
        val = (val * (period - 1) + trs[i]) / period
        result[i] = val
    return result

def adx_series(highs, lows, closes, period=14):
    n = len(closes)
    if n < period * 2 + 1:
        return [0.0] * n
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    trs = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i-1]
        down = lows[i-1] - lows[i]
        plus_dm[i] = max(up, 0) if up > down else 0.0
        minus_dm[i] = max(down, 0) if down > up else 0.0
        trs[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
    s_pdm = sum(plus_dm[1:period+1])
    s_mdm = sum(minus_dm[1:period+1])
    s_tr = sum(trs[1:period+1])
    adx_arr = [0.0] * n
    dx_list = []
    for i in range(period, n):
        s_pdm = s_pdm - s_pdm / period + plus_dm[i]
        s_mdm = s_mdm - s_mdm / period + minus_dm[i]
        s_tr = s_tr - s_tr / period + trs[i]
        pdi = (s_pdm / s_tr * 100) if s_tr > 0 else 0.0
        mdi = (s_mdm / s_tr * 100) if s_tr > 0 else 0.0
        dx = (abs(pdi - mdi) / (pdi + mdi) * 100) if (pdi + mdi) > 0 else 0.0
        dx_list.append(dx)
    if len(dx_list) >= period:
        adx_val = sum(dx_list[:period]) / period
        for i in range(period, len(dx_list)):
            adx_val = (adx_val * (period - 1) + dx_list[i]) / period
            adx_arr[period + i] = adx_val
    return adx_arr

def roc_series(closes, period):
    result = [0.0] * len(closes)
    for i in range(period, len(closes)):
        result[i] = (closes[i] / closes[i - period] - 1) * 100
    return result


# ═══ Data fetch ═══

async def fetch_daily(coin, days_back=1200):
    symbol = BINANCE_MAP[coin]
    all_candles = []
    start_ms = int((_time.time() - days_back * 86400) * 1000)
    async with httpx.AsyncClient(timeout=30) as client:
        while len(all_candles) < days_back + 50:
            params = {"symbol": symbol, "interval": "1d", "limit": "1000"}
            if not all_candles:
                params["startTime"] = str(start_ms)
            else:
                params["startTime"] = str(all_candles[-1]["ts"] + 1)
            try:
                resp = await client.get("https://api.binance.com/api/v3/klines", params=params)
                data = resp.json()
            except Exception as e:
                print(f"  Error {coin}: {e}", flush=True)
                break
            if not isinstance(data, list) or len(data) == 0:
                break
            for c in data:
                all_candles.append({
                    "ts": int(c[0]),
                    "date": datetime.fromtimestamp(int(c[0])/1000, tz=timezone.utc).strftime("%Y-%m-%d"),
                    "O": float(c[1]), "H": float(c[2]), "L": float(c[3]), "C": float(c[4]),
                })
            if len(data) < 1000:
                break
    all_candles.sort(key=lambda x: x["ts"])
    return all_candles


# ═══ Backtest engine ═══

def run_backtest(daily_data):
    # Build per-coin indicator series
    coin_data = {}
    for coin in COINS:
        candles = daily_data.get(coin, [])
        if len(candles) < 100:
            print(f"  WARNING: {coin} has only {len(candles)} bars, skipping", flush=True)
            continue
        closes = [c["C"] for c in candles]
        highs = [c["H"] for c in candles]
        lows = [c["L"] for c in candles]
        coin_data[coin] = {
            "candles": candles,
            "roc": roc_series(closes, ROC_PERIOD),
            "ema_f": ema_series(closes, EMA_FAST),
            "ema_s": ema_series(closes, EMA_SLOW),
            "atr": atr_series(highs, lows, closes, ATR_PERIOD),
            "adx": adx_series(highs, lows, closes, 14),
        }

    # Find common date range
    all_dates = None
    for coin, cd in coin_data.items():
        dates = [c["date"] for c in cd["candles"]]
        if all_dates is None:
            all_dates = set(dates)
        else:
            all_dates &= set(dates)
    all_dates = sorted(all_dates)
    print(f"  Common bars: {len(all_dates)} ({all_dates[0]} to {all_dates[-1]})", flush=True)

    # Date -> index map for each coin
    date_idx = {}
    for coin, cd in coin_data.items():
        m = {c["date"]: i for i, c in enumerate(cd["candles"])}
        date_idx[coin] = m

    # State
    equity = CAPITAL
    positions = {}  # coin -> pos dict
    trades = []
    equity_curve = []
    last_rotate_day = -999
    peak_equity = equity
    max_dd = 0.0
    year_equity = {}  # year -> equity at end

    start_i = EMA_SLOW + 20  # need enough history for indicators

    for i in range(start_i, len(all_dates)):
        date = all_dates[i]
        dt = datetime.strptime(date, "%Y-%m-%d")
        year = dt.year

        # ── 1. Manage existing positions (pessimistic: stop BEFORE peak) ──
        for coin in list(positions.keys()):
            if coin not in date_idx or date not in date_idx[coin]:
                continue
            ci = date_idx[coin][date]
            cd = coin_data[coin]
            pos = positions[coin]
            bar_low = cd["candles"][ci]["L"]
            bar_high = cd["candles"][ci]["H"]
            bar_close = cd["candles"][ci]["C"]
            ct = CT_VAL[coin]

            hit_stop = False
            reason = "trail_stop"

            if pos["side"] == "long":
                # PESSIMISTIC: check stop first, then update peak
                if bar_low <= pos["stop"]:
                    hit_stop = True
                    exit_raw = pos["stop"]
                if not hit_stop and bar_high > pos["peak"]:
                    pos["peak"] = bar_high
                    new_stop = pos["peak"] * (1 - TRAIL_PCT)
                    if new_stop > pos["stop"]:
                        pos["stop"] = new_stop
                if not hit_stop and not pos["breakeven"] and bar_close >= pos["entry"] * (1 + BE_PCT):
                    pos["stop"] = max(pos["stop"], pos["entry"] * 0.999)
                    pos["breakeven"] = True
            else:  # short
                if bar_high >= pos["stop"]:
                    hit_stop = True
                    exit_raw = pos["stop"]
                if not hit_stop and bar_low < pos["trough"]:
                    pos["trough"] = bar_low
                    new_stop = pos["trough"] * (1 + TRAIL_PCT)
                    if new_stop < pos["stop"]:
                        pos["stop"] = new_stop
                if not hit_stop and not pos["breakeven"] and bar_close <= pos["entry"] * (1 - BE_PCT):
                    pos["stop"] = min(pos["stop"], pos["entry"] * 1.001)
                    pos["breakeven"] = True

            if hit_stop:
                # Exit with slippage
                if pos["side"] == "long":
                    exit_fill = exit_raw * (1 - SLIPPAGE)
                    pnl = pos["size"] * ct * (exit_fill - pos["entry"]) - pos["size"] * ct * exit_fill * COMMISSION
                else:
                    exit_fill = exit_raw * (1 + SLIPPAGE)
                    pnl = pos["size"] * ct * (pos["entry"] - exit_fill) - pos["size"] * ct * exit_fill * COMMISSION
                equity += pnl
                trades.append({
                    "date": date, "coin": coin, "side": pos["side"],
                    "entry": pos["entry"], "exit": exit_fill,
                    "size": pos["size"], "pnl": round(pnl, 2),
                    "reason": reason, "hold_days": i - pos["entry_bar"],
                    "notional": round(pos["notional"], 0),
                })
                del positions[coin]

        # ── 2. Rotation check (once per day, min hold) ──
        if i - last_rotate_day < MIN_HOLD and positions:
            if equity > peak_equity:
                peak_equity = equity
            dd = (peak_equity - equity) / peak_equity * 100 if peak_equity > 0 else 0
            max_dd = max(max_dd, dd)
            equity_curve.append((date, equity))
            continue

        # ── 3. Compute indicators for signal bar (yesterday = i-1) ──
        sig_date = all_dates[i - 1] if i > 0 else None
        if not sig_date:
            equity_curve.append((date, equity))
            continue

        rankings = []
        for coin, cd in coin_data.items():
            if sig_date not in date_idx[coin]:
                continue
            si = date_idx[coin][sig_date]
            roc_val = cd["roc"][si]
            ema_trend = cd["ema_f"][si] > cd["ema_s"][si]
            adx_val = cd["adx"][si]
            atr_val = cd["atr"][si]
            if atr_val <= 0:
                continue
            rankings.append((coin, roc_val, ema_trend, adx_val, atr_val))

        if not rankings:
            equity_curve.append((date, equity))
            continue

        rankings.sort(key=lambda x: x[1], reverse=True)

        # ── 4. Pick targets ──
        target_coins = set()
        for coin, roc_val, ema_trend, adx_val, atr_val in rankings:
            if len(target_coins) >= TOP_K:
                break
            if roc_val > 0 and ema_trend and adx_val >= ADX_MIN:
                target_coins.add((coin, "long"))
            elif roc_val < 0 and not ema_trend and adx_val >= ADX_MIN:
                target_coins.add((coin, "short"))

        # ── 5. Close positions not in target ──
        for coin in list(positions.keys()):
            pos = positions[coin]
            if (coin, pos["side"]) not in target_coins:
                if coin not in date_idx or date not in date_idx[coin]:
                    continue
                ci = date_idx[coin][date]
                cd = coin_data[coin]
                ct = CT_VAL[coin]
                # Exit at today's open (rotation exit)
                exit_raw = cd["candles"][ci]["O"]
                if pos["side"] == "long":
                    exit_fill = exit_raw * (1 - SLIPPAGE)
                    pnl = pos["size"] * ct * (exit_fill - pos["entry"]) - pos["size"] * ct * exit_fill * COMMISSION
                else:
                    exit_fill = exit_raw * (1 + SLIPPAGE)
                    pnl = pos["size"] * ct * (pos["entry"] - exit_fill) - pos["size"] * ct * exit_fill * COMMISSION
                equity += pnl
                trades.append({
                    "date": date, "coin": coin, "side": pos["side"],
                    "entry": pos["entry"], "exit": exit_fill,
                    "size": pos["size"], "pnl": round(pnl, 2),
                    "reason": "rotation", "hold_days": i - pos["entry_bar"],
                    "notional": round(pos["notional"], 0),
                })
                del positions[coin]

        # ── 6. Open new positions ──
        for coin, side in target_coins:
            if coin in positions:
                continue
            if coin not in date_idx or date not in date_idx[coin]:
                continue
            ci = date_idx[coin][date]
            cd = coin_data[coin]
            entry_raw = cd["candles"][ci]["O"]  # enter at today's OPEN
            atr_val = cd["atr"][date_idx[coin][sig_date]]  # ATR from signal bar
            ct = CT_VAL[coin]
            lot = LOT_SZ[coin]

            # Sizing: margin = capital * 40%, notional = margin * 3x
            margin = CAPITAL * MAX_POS_PCT
            notional = margin * LEVERAGE
            raw_sz = notional / (ct * entry_raw)
            sz = round(raw_sz / lot) * lot
            if sz < lot:
                continue
            actual_notional = sz * ct * entry_raw

            # Entry with slippage
            if side == "long":
                entry_fill = entry_raw * (1 + SLIPPAGE)
            else:
                entry_fill = entry_raw * (1 - SLIPPAGE)
            entry_fee = actual_notional * COMMISSION
            equity -= entry_fee

            # Initial stop from ATR
            if side == "long":
                stop = entry_fill - ATR_STOP_M * atr_val
            else:
                stop = entry_fill + ATR_STOP_M * atr_val

            positions[coin] = {
                "entry": entry_fill, "size": sz, "stop": stop,
                "peak": entry_fill, "trough": entry_fill,
                "side": side, "entry_bar": i, "breakeven": False,
                "notional": actual_notional,
            }
            last_rotate_day = i

        # Track
        equity = max(equity, 0)
        equity_curve.append((date, equity))
        if equity > peak_equity:
            peak_equity = equity
        dd = (peak_equity - equity) / peak_equity * 100 if peak_equity > 0 else 0
        max_dd = max(max_dd, dd)
        year_equity[year] = equity

    return {
        "trades": trades,
        "equity_curve": equity_curve,
        "final_equity": equity,
        "max_dd": max_dd,
        "total_pnl": equity - CAPITAL,
        "year_equity": year_equity,
    }


def print_results(label, lev, r, cap):
    wins = [t for t in r["trades"] if t["pnl"] > 0]
    losses = [t for t in r["trades"] if t["pnl"] <= 0]
    total_trades = len(r["trades"])
    win_rate = len(wins) / total_trades * 100 if total_trades else 0
    avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
    pf = abs(sum(t["pnl"] for t in wins) / sum(t["pnl"] for t in losses)) if losses and sum(t["pnl"] for t in losses) != 0 else 0

    first_date = r["equity_curve"][0][0] if r["equity_curve"] else "N/A"
    last_date = r["equity_curve"][-1][0] if r["equity_curve"] else "N/A"
    years = (datetime.strptime(last_date, "%Y-%m-%d") - datetime.strptime(first_date, "%Y-%m-%d")).days / 365.25 if first_date != "N/A" else 1
    cagr = ((r["final_equity"] / cap) ** (1 / years) - 1) * 100 if years > 0 else 0

    daily_rets = []
    for i in range(1, len(r["equity_curve"])):
        prev = r["equity_curve"][i-1][1]
        curr = r["equity_curve"][i][1]
        if prev > 0:
            daily_rets.append((curr - prev) / prev)
    sharpe = 0
    if daily_rets:
        import statistics
        avg_r = statistics.mean(daily_rets)
        std_r = statistics.stdev(daily_rets)
        sharpe = (avg_r / std_r) * (365.25 ** 0.5) if std_r > 0 else 0

    print(f"\n{'='*70}", flush=True)
    print(f"  {label}  ({first_date} → {last_date}, {years:.1f}y)", flush=True)
    print(f"{'='*70}", flush=True)
    print(f"  Equity:       ${r['final_equity']:>12,.2f}  |  PnL: ${r['total_pnl']:>+10,.2f}", flush=True)
    print(f"  CAGR:         {cagr:>+12.1f}%  |  DD:  {-r['max_dd']:>8.1f}%  |  Sharpe: {sharpe:.2f}", flush=True)
    print(f"  Trades: {total_trades:>5}  |  WR: {win_rate:.1f}%  |  PF: {pf:.2f}", flush=True)
    print(f"  Avg Win: ${avg_win:>+10,.2f}  |  Avg Loss: ${avg_loss:>+10,.2f}", flush=True)
    notional_per = cap * MAX_POS_PCT * lev
    print(f"  Notional/pos: ${notional_per:,.0f} ({lev}x)  |  Total: ${notional_per*2:,.0f}", flush=True)

    # Year breakdown
    print(f"\n  {'Year':<8} {'Equity':>12} {'PnL':>12} {'Return':>10}", flush=True)
    print(f"  {'-'*44}", flush=True)
    prev_yr_eq = cap
    for yr in sorted(r["year_equity"].keys()):
        eq = r["year_equity"][yr]
        pnl = eq - prev_yr_eq
        ret = pnl / prev_yr_eq * 100 if prev_yr_eq > 0 else 0
        print(f"  {yr:<8} ${eq:>11,.2f} ${pnl:>+11,.2f} {ret:>+9.1f}%", flush=True)
        prev_yr_eq = eq

    # Exits
    reasons = {}
    for t in r["trades"]:
        rn = t["reason"]
        if rn not in reasons:
            reasons[rn] = {"count": 0, "pnl": 0, "wins": 0}
        reasons[rn]["count"] += 1
        reasons[rn]["pnl"] += t["pnl"]
        if t["pnl"] > 0:
            reasons[rn]["wins"] += 1
    print(f"\n  Exits:", flush=True)
    for reason, s in sorted(reasons.items()):
        wr = s["wins"] / s["count"] * 100 if s["count"] else 0
        print(f"    {reason:<15} {s['count']:>3} trades  PnL=${s['pnl']:>+10,.2f}  WR={wr:.0f}%", flush=True)

    worst = min(r["trades"], key=lambda t: t["pnl"]) if r["trades"] else None
    if worst:
        print(f"  Worst trade: ${worst['pnl']:+,.2f} ({abs(worst['pnl'])/cap*100:.1f}% of capital) — {worst['date']} {worst['coin']} {worst['side']}", flush=True)

    return {"cagr": cagr, "dd": r['max_dd'], "sharpe": sharpe, "final": r['final_equity']}


async def main():
    print("\n" + "="*70, flush=True)
    print("  ROTATION STRATEGY: 1x vs 3x COMPARISON, $10,000 Capital", flush=True)
    print("="*70, flush=True)

    # Fetch data
    print("\n[1/3] Fetching daily data from Binance...", flush=True)
    daily_data = {}
    async with httpx.AsyncClient(timeout=30) as client:
        tasks = [fetch_daily(coin, days_back=1200) for coin in COINS]
        results = await asyncio.gather(*tasks)
        for coin, data in zip(COINS, results):
            daily_data[coin] = data
            print(f"  {coin}: {len(data)} bars  ({data[0]['date'] if data else 'N/A'} → {data[-1]['date'] if data else 'N/A'})", flush=True)

    params_str = (f"  ROC({ROC_PERIOD})  EMA({EMA_FAST}/{EMA_SLOW})  ADX>={ADX_MIN}  Top-{TOP_K}\n"
                   f"  ATR stop x{ATR_STOP_M}  Trail {TRAIL_PCT*100}%  BE {BE_PCT*100}%  MinHold {MIN_HOLD}d\n"
                   f"  Commission: {COMMISSION*100}% + Slippage: {SLIPPAGE*100}%  per side")
    print(f"\n[2/3] Running backtests...\n  {params_str}", flush=True)

    # === Variant A: No leverage (previous settings) ===
    global LEVERAGE
    LEVERAGE = 1.0
    r1 = run_backtest(daily_data)
    m1 = print_results("VARIANT A — NO Leverage (1x, previous)", 1.0, r1, CAPITAL)

    # === Variant B: 3x leverage (current) ===
    LEVERAGE = 3.0
    r3 = run_backtest(daily_data)
    m3 = print_results("VARIANT B — 3x Leverage (current)", 3.0, r3, CAPITAL)

    # === Comparison ===
    print(f"\n{'='*70}", flush=True)
    print(f"  COMPARISON", flush=True)
    print(f"{'='*70}", flush=True)
    print(f"  {'Metric':<20} {'1x (prev)':>15} {'3x (curr)':>15} {'Delta':>12}", flush=True)
    print(f"  {'-'*64}", flush=True)
    print(f"  {'Final Equity':<20} ${m1['final']:>14,.2f} ${m3['final']:>14,.2f} ${m3['final']-m1['final']:>+11,.2f}", flush=True)
    print(f"  {'CAGR':<20} {m1['cagr']:>+14.1f}% {m3['cagr']:>+14.1f}% {m3['cagr']-m1['cagr']:>+11.1f}%", flush=True)
    print(f"  {'Max Drawdown':<20} {-m1['dd']:>14.1f}% {-m3['dd']:>14.1f}% {-m3['dd']+m1['dd']:>+11.1f}%", flush=True)
    print(f"  {'Sharpe Ratio':<20} {m1['sharpe']:>15.2f} {m3['sharpe']:>15.2f} {m3['sharpe']-m1['sharpe']:>+11.2f}", flush=True)

    # DD per year
    print(f"\n  --- Year-by-year Return Comparison ---", flush=True)
    print(f"  {'Year':<8} {'1x Equity':>12} {'1x Ret':>10} {'3x Equity':>12} {'3x Ret':>10}", flush=True)
    print(f"  {'-'*54}", flush=True)
    all_years = sorted(set(list(r1["year_equity"].keys()) + list(r3["year_equity"].keys())))
    prev1, prev3 = CAPITAL, CAPITAL
    for yr in all_years:
        eq1 = r1["year_equity"].get(yr, prev1)
        eq3 = r3["year_equity"].get(yr, prev3)
        ret1 = (eq1 - prev1) / prev1 * 100 if prev1 > 0 else 0
        ret3 = (eq3 - prev3) / prev3 * 100 if prev3 > 0 else 0
        print(f"  {yr:<8} ${eq1:>11,.2f} {ret1:>+9.1f}% ${eq3:>11,.2f} {ret3:>+9.1f}%", flush=True)
        prev1, prev3 = eq1, eq3

    print()


if __name__ == "__main__":
    asyncio.run(main())
