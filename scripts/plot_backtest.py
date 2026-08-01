"""
Momentum Rotation - Equity curve + trade markers visualization.
Multi-panel: BTC price with signals, all-coin heatmap, equity curve, drawdown.
"""

import asyncio
import math
import os
import sys
import pickle
from datetime import datetime, timezone
from collections import defaultdict

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.font_manager as fm
from matplotlib.patches import FancyBboxPatch
import numpy as np

# Fonts
fm.fontManager.addfont('/usr/share/fonts/truetype/chinese/SarasaMonoSC-Regular.ttf')
fm.fontManager.addfont('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')
plt.rcParams['font.sans-serif'] = ['DejaVu Sans', 'Sarasa Mono SC']
plt.rcParams['axes.unicode_minus'] = False

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

CT_VAL = {"BTC": 0.01, "ETH": 0.1, "BNB": 0.1, "SOL": 0.1}
LOT_SZ = {"BTC": 0.01, "ETH": 0.01, "BNB": 0.01, "SOL": 0.01}
SWAP_MAP = {"BTC": "BTC-USDT-SWAP", "ETH": "ETH-USDT-SWAP", "BNB": "BNB-USDT-SWAP", "SOL": "SOL-USDT-SWAP"}
COINS = ["BTC", "ETH", "SOL", "BNB"]
BUDGET = 10000.0
CACHE_FILE = "/tmp/strategy_compare_cache_v2.pkl"

COIN_COLORS = {"BTC": "#F7931A", "ETH": "#627EEA", "SOL": "#9945FF", "BNB": "#F3BA2F"}
COIN_COLORS_LIGHT = {"BTC": "#F7931A44", "ETH": "#627EEA44", "SOL": "#9945FF44", "BNB": "#F3BA2F44"}

# ═══════════════════════════════════════════════════════════════════
#  COST MODEL (same as verify_rotation.py)
# ═══════════════════════════════════════════════════════════════════

class CostModel:
    COMMISSION = 0.001
    SLIPPAGE_PCT = 0.0005
    PER_SIDE = COMMISSION + SLIPPAGE_PCT

    @staticmethod
    def entry_cost(price, size, ct_val):
        slip_price = price * (1 + CostModel.SLIPPAGE_PCT)
        commission = size * ct_val * price * CostModel.COMMISSION
        slippage = size * ct_val * price * CostModel.SLIPPAGE_PCT
        return slip_price, commission + slippage

    @staticmethod
    def exit_cost(price, size, ct_val, side):
        if side == "long":
            slip_price = price * (1 - CostModel.SLIPPAGE_PCT)
        else:
            slip_price = price * (1 + CostModel.SLIPPAGE_PCT)
        commission = size * ct_val * price * CostModel.COMMISSION
        slippage = size * ct_val * price * CostModel.SLIPPAGE_PCT
        return slip_price, commission + slippage


# ═══════════════════════════════════════════════════════════════════
#  DATA
# ═══════════════════════════════════════════════════════════════════

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
        print("  Loaded cache", flush=True)
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
#  INDICATORS
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
#  BACKTEST (returns trades with full info for plotting)
# ═══════════════════════════════════════════════════════════════════

def backtest_for_plot(daily_data, params=None):
    if params is None:
        params = {
            "roc_period": 14, "top_k": 2, "atr_period": 14,
            "atr_stop_mult": 2.0, "trail_pct": 0.02,
            "max_pos_pct": 0.40, "min_hold_days": 3,
            "ema_fast": 20, "ema_slow": 50, "adx_min": 18,
        }

    equity = BUDGET
    equity_curve = [BUDGET]
    trades = []
    positions = {}
    peak_equity = equity
    max_dd = 0
    last_rotate_day = -999

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
        dates = [c["datetime"] for c in candles]
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

    ref = coin_data.get("BTC")
    if not ref:
        return None
    n = len(ref["candles"])
    min_idx = params["ema_slow"] + 20

    # For plotting: track which coin is held each day
    held_coins = [None] * n  # list of sets
    equity_dates = [ref["dates"][0]]

    for i in range(1, n):
        date = ref["dates"][i]
        equity_dates.append(date)

        # Track what's held
        held_coins[i] = set(positions.keys())

        # ── 1. MANAGE EXISTING POSITIONS ──
        for c in list(positions.keys()):
            cd = coin_data.get(c)
            if not cd or i >= len(cd["candles"]):
                continue
            pos = positions[c]
            high = cd["highs"][i]
            low = cd["lows"][i]
            ct_val = CT_VAL[c]

            if pos["side"] == "long":
                if high > pos["peak"]:
                    pos["peak"] = high
                new_stop = pos["peak"] * (1 - params["trail_pct"])
                if new_stop > pos["stop"]:
                    pos["stop"] = new_stop
                if not pos["breakeven"] and high >= pos["entry"] * 1.03:
                    pos["stop"] = max(pos["stop"], pos["entry"] * 0.999)
                    pos["breakeven"] = True
                if low <= pos["stop"] and pos["size"] > 0:
                    exit_price_raw = pos["stop"]
                    exit_price, exit_fees = CostModel.exit_cost(exit_price_raw, pos["size"], ct_val, "long")
                    pnl = pos["size"] * (exit_price - pos["entry"]) * ct_val
                    net_pnl = pnl - exit_fees
                    equity += net_pnl
                    trades.append({
                        "date": date, "coin": c, "side": "long",
                        "pnl": round(net_pnl, 2), "reason": "trail_stop",
                        "entry_date": pos["entry_date"], "entry_price": pos["entry"],
                        "exit_price": round(exit_price_raw, 2),
                        "fees": round(exit_fees, 2), "hold_days": i - pos["entry_bar"],
                        "type": "exit",
                    })
                    del positions[c]
                    continue
            else:
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
                    trades.append({
                        "date": date, "coin": c, "side": "short",
                        "pnl": round(net_pnl, 2), "reason": "trail_stop",
                        "entry_date": pos["entry_date"], "entry_price": pos["entry"],
                        "exit_price": round(exit_price_raw, 2),
                        "fees": round(exit_fees, 2), "hold_days": i - pos["entry_bar"],
                        "type": "exit",
                    })
                    del positions[c]

        # ── 2. COOLDOWN ──
        days_since_rotate = i - last_rotate_day
        if days_since_rotate < params["min_hold_days"] and len(positions) > 0:
            equity = max(equity, 0)
            equity_curve.append(equity)
            if equity > peak_equity:
                peak_equity = equity
            dd = (peak_equity - equity) / peak_equity * 100 if peak_equity > 0 else 0
            max_dd = max(max_dd, dd)
            continue

        # ── 3. SIGNALS on [i-1] ──
        si = i - 1
        if si < min_idx:
            equity = max(equity, 0)
            equity_curve.append(equity)
            continue

        rankings = []
        for c in COINS:
            cd = coin_data.get(c)
            if not cd or si >= len(cd["closes"]):
                continue
            roc_val = cd["roc"][si]
            ema_trend = cd["ema_f"][si] > cd["ema_s"][si]
            adx_val = cd["adx"][si]
            atr_val = cd["atr"][si]
            if atr_val <= 0:
                continue
            rankings.append((c, roc_val, ema_trend, adx_val, atr_val))

        if not rankings:
            equity_curve.append(equity)
            continue

        rankings.sort(key=lambda x: x[1], reverse=True)

        target_coins = set()
        for c, roc_val, ema_trend, adx_val, atr_val in rankings:
            if len(target_coins) >= params["top_k"]:
                break
            if roc_val > 0 and ema_trend and adx_val >= params["adx_min"]:
                target_coins.add((c, "long"))
            elif roc_val < 0 and not ema_trend and adx_val >= params["adx_min"]:
                target_coins.add((c, "short"))

        # ── 5. CLOSE positions not in target ──
        for c in list(positions.keys()):
            pos = positions[c]
            side = pos["side"]
            if (c, side) not in target_coins:
                cd = coin_data.get(c)
                if not cd or i >= len(cd["candles"]):
                    continue
                exit_raw = cd["opens"][i]
                ct_val = CT_VAL[c]
                exit_price, exit_fees = CostModel.exit_cost(exit_raw, pos["size"], ct_val, side)
                if side == "long":
                    pnl = pos["size"] * (exit_price - pos["entry"]) * ct_val
                else:
                    pnl = pos["size"] * (pos["entry"] - exit_price) * ct_val
                net_pnl = pnl - exit_fees
                equity += net_pnl
                trades.append({
                    "date": date, "coin": c, "side": side,
                    "pnl": round(net_pnl, 2), "reason": "rotation_exit",
                    "entry_date": pos["entry_date"], "entry_price": pos["entry"],
                    "exit_price": round(exit_raw, 2),
                    "fees": round(exit_fees, 2), "hold_days": i - pos["entry_bar"],
                    "type": "exit",
                })
                del positions[c]

        # ── 6. OPEN NEW at bar [i] OPEN ──
        for c, side in target_coins:
            if c in positions:
                continue
            cd = coin_data[c]
            if i >= len(cd["candles"]):
                continue
            entry_raw = cd["opens"][i]
            atr_val = cd["atr"][si]
            if atr_val <= 0 or entry_raw <= 0:
                continue
            ct_val = CT_VAL[c]
            lot = LOT_SZ[c]
            alloc_pct = min(1.0 / params["top_k"], params["max_pos_pct"])
            notional = equity * alloc_pct
            raw_sz = notional / (ct_val * entry_raw)
            max_sz = BUDGET * params["max_pos_pct"] / (ct_val * entry_raw)
            sz = min(raw_sz, max_sz)
            sz = round(sz / lot) * lot
            if sz < lot:
                continue
            entry_price, entry_fees = CostModel.entry_cost(entry_raw, sz, ct_val)
            if side == "long":
                stop = entry_price - params["atr_stop_mult"] * atr_val
            else:
                stop = entry_price + params["atr_stop_mult"] * atr_val
            equity -= entry_fees
            positions[c] = {
                "entry": entry_price, "entry_raw": entry_raw,
                "size": sz, "stop": stop,
                "peak": entry_price, "trough": entry_price,
                "side": side, "entry_bar": i,
                "entry_date": date, "breakeven": False,
            }
            trades.append({
                "date": date, "coin": c, "side": side,
                "entry_price": round(entry_raw, 2),
                "reason": "signal_entry",
                "type": "entry",
            })

        last_rotate_day = i
        held_coins[i] = set(positions.keys())
        equity = max(equity, 0)
        equity_curve.append(equity)
        if equity > peak_equity:
            peak_equity = equity
        dd = (peak_equity - equity) / peak_equity * 100 if peak_equity > 0 else 0
        max_dd = max(max_dd, dd)

    # Metrics
    total_return_pct = (equity - BUDGET) / BUDGET * 100
    years = n / 365
    annual_return = ((equity / BUDGET) ** (1 / max(years, 0.1)) - 1) * 100 if equity > 0 else 0

    # Drawdown series
    dd_series = []
    peak = BUDGET
    for eq in equity_curve:
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100 if peak > 0 else 0
        dd_series.append(-dd)

    return {
        "equity_curve": equity_curve,
        "equity_dates": equity_dates,
        "trades": trades,
        "held_coins": held_coins,
        "dd_series": dd_series,
        "coin_data": coin_data,
        "annual_return": annual_return,
        "max_dd": max_dd,
        "final_equity": equity,
        "total_return_pct": total_return_pct,
    }


# ═══════════════════════════════════════════════════════════════════
#  PLOTTING
# ═══════════════════════════════════════════════════════════════════

def create_chart(result):
    eq_dates = result["equity_dates"]
    eq_curve = result["equity_curve"]
    dd_series = result["dd_series"]
    trades = result["trades"]
    held = result["held_coins"]
    coin_data = result["coin_data"]

    n = len(eq_dates)
    dates_num = mdates.date2num(eq_dates)

    # Separate trades
    entries = [t for t in trades if t["type"] == "entry"]
    exits = [t for t in trades if t["type"] == "exit"]

    # ── Figure setup ──
    fig = plt.figure(figsize=(22, 16), facecolor="#0d1117")

    # Grid: 4 rows
    # Row 0: BTC price with signals (tall)
    # Row 1: Held coins ribbon
    # Row 2: Equity curve (tall)
    # Row 3: Drawdown
    gs = fig.add_gridspec(4, 1, height_ratios=[2.5, 0.6, 2.5, 1.2], hspace=0.08,
                          left=0.06, right=0.94, top=0.93, bottom=0.05)

    ax_style = {"facecolor": "#0d1117"}
    tick_color = "#8b949e"
    grid_color = "#21262d"

    def style_ax(ax, ylabel=""):
        ax.set_facecolor("#0d1117")
        ax.tick_params(colors=tick_color, labelsize=9)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.spines["left"].set_color(grid_color)
        ax.spines["bottom"].set_color(grid_color)
        ax.grid(True, color=grid_color, alpha=0.5, linewidth=0.5)
        if ylabel:
            ax.set_ylabel(ylabel, color=tick_color, fontsize=10, fontweight="bold")

    # ══════════════════════════════════════════════════════════════
    # PANEL 1: BTC Price + Trade Markers
    # ══════════════════════════════════════════════════════════════
    ax1 = fig.add_subplot(gs[0], **ax_style)
    style_ax(ax1, "BTC Price (USDT)")

    btc_cd = coin_data.get("BTC")
    if btc_cd:
        btc_dates = btc_cd["dates"]
        btc_closes = btc_cd["closes"]
        ax1.plot(btc_dates, btc_closes, color=COIN_COLORS["BTC"], linewidth=1.2, alpha=0.9, zorder=2)
        # Shade held periods
        for i in range(1, n):
            if i < len(held) and held[i]:
                for c in held[i]:
                    if c == "BTC" and i < len(btc_dates):
                        ax1.axvspan(btc_dates[i-1] if i > 0 else btc_dates[i],
                                    btc_dates[i],
                                    color=COIN_COLORS_LIGHT["BTC"], alpha=0.3, zorder=1)

    # Plot entry/exit markers on BTC price
    for t in entries:
        if t["coin"] == "BTC":
            marker = "^" if t["side"] == "long" else "v"
            color = "#00ff88" if t["side"] == "long" else "#ff4466"
            ax1.scatter(t["date"], t["entry_price"], marker=marker, color=color,
                       s=60, zorder=5, edgecolors="white", linewidths=0.5, alpha=0.85)

    for t in exits:
        if t["coin"] == "BTC":
            marker = "v" if t["side"] == "long" else "^"
            color = "#ff4466" if t["side"] == "long" else "#00ff88"
            ax1.scatter(t["date"], t["exit_price"], marker=marker, color=color,
                       s=50, zorder=5, edgecolors="white", linewidths=0.5, alpha=0.7)

    ax1.set_title("Momentum Rotation Backtest — BTC Price with Trade Signals",
                   color="white", fontsize=14, fontweight="bold", pad=12)

    # ══════════════════════════════════════════════════════════════
    # PANEL 2: Held Coins Ribbon
    # ══════════════════════════════════════════════════════════════
    ax2 = fig.add_subplot(gs[1], **ax_style)
    style_ax(ax2)
    ax2.set_yticks([0, 1, 2, 3])
    ax2.set_yticklabels(COINS, fontsize=8, color=tick_color)
    ax2.set_ylim(-0.5, 3.5)

    for coin_idx, coin in enumerate(COINS):
        for i in range(1, n):
            if i < len(held) and held[i] and coin in held[i]:
                side = None
                # determine side from positions at that point
                ax2.axvspan(eq_dates[i-1] if i > 0 else eq_dates[i],
                           eq_dates[i],
                           ymin=(coin_idx - 0.4) / 4, ymax=(coin_idx + 0.4) / 4,
                           color=COIN_COLORS[coin], alpha=0.7, zorder=2)

    ax2.set_title("Active Positions", color=tick_color, fontsize=9, loc="left", pad=4)

    # ══════════════════════════════════════════════════════════════
    # PANEL 3: Equity Curve
    # ══════════════════════════════════════════════════════════════
    ax3 = fig.add_subplot(gs[2], **ax_style)
    style_ax(ax3, "Equity (USDT)")

    # Fill under equity curve
    ax3.fill_between(eq_dates, BUDGET, eq_curve, color="#58a6ff", alpha=0.1, zorder=1)
    ax3.plot(eq_dates, eq_curve, color="#58a6ff", linewidth=1.8, zorder=3)

    # Benchmark: buy & hold BTC
    if btc_cd:
        btc_bh = [BUDGET * btc_cd["closes"][i] / btc_cd["closes"][0] for i in range(n)]
        ax3.plot(btc_cd["dates"][:n], btc_bh, color=COIN_COLORS["BTC"], linewidth=1.0,
                alpha=0.4, linestyle="--", zorder=2, label="BTC Buy & Hold")
        ax3.legend(loc="upper left", fontsize=9, facecolor="#0d1117", edgecolor=grid_color,
                  labelcolor=tick_color)

    # Mark entries/exits on equity curve
    for t in entries:
        pnl_color = "#00ff88" if t.get("pnl", 0) > 0 else ("#ff4466" if t.get("pnl", 0) < 0 else "#8b949e")
    # Show trade PnL on equity curve for exits
    for t in exits:
        color = "#00ff88" if t["pnl"] > 0 else "#ff4466"
        alpha = min(0.9, 0.3 + abs(t["pnl"]) / 2000)
        ax3.scatter(t["date"], None, marker="o", color=color, s=20, alpha=0.6, zorder=4)

    # Annotate final equity
    final_eq = eq_curve[-1]
    ax3.annotate(f"${final_eq:,.0f}", xy=(eq_dates[-1], final_eq),
                xytext=(-80, 15), textcoords="offset points",
                color="#58a6ff", fontsize=11, fontweight="bold",
                arrowprops=dict(arrowstyle="->", color="#58a6ff", lw=1.2))
    ax3.annotate(f"${BUDGET:,.0f}", xy=(eq_dates[0], BUDGET),
                xytext=(10, -15), textcoords="offset points",
                color=tick_color, fontsize=9)

    # Stats box
    wins = [t for t in exits if t["pnl"] > 0]
    losses_t = [t for t in exits if t["pnl"] <= 0]
    wr = len(wins) / len(exits) * 100 if exits else 0
    stats_text = (
        f"CAGR: {result['annual_return']:.1f}%\n"
        f"Max DD: -{result['max_dd']:.1f}%\n"
        f"Trades: {len(exits)}\n"
        f"Win Rate: {wr:.1f}%\n"
        f"Return: {result['total_return_pct']:.0f}%"
    )
    props = dict(boxstyle="round,pad=0.5", facecolor="#161b22", edgecolor="#30363d", alpha=0.9)
    ax3.text(0.98, 0.35, stats_text, transform=ax3.transAxes, fontsize=9,
            verticalalignment='top', horizontalalignment='right',
            bbox=props, color="#c9d1d9", family="monospace")

    # ══════════════════════════════════════════════════════════════
    # PANEL 4: Drawdown
    # ══════════════════════════════════════════════════════════════
    ax4 = fig.add_subplot(gs[3], **ax_style)
    style_ax(ax4, "Drawdown %")

    ax4.fill_between(eq_dates, dd_series, 0, color="#ff4466", alpha=0.3, zorder=1)
    ax4.plot(eq_dates, dd_series, color="#ff4466", linewidth=0.8, alpha=0.8, zorder=2)
    ax4.axhline(y=0, color=grid_color, linewidth=0.5)

    # ══════════════════════════════════════════════════════════════
    # X-axis formatting
    # ══════════════════════════════════════════════════════════════
    for ax in [ax1, ax2, ax3, ax4]:
        ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
        ax.xaxis.set_major_locator(mdates.MonthLocator(interval=3))
        plt.setp(ax.xaxis.get_majorticklabels(), rotation=45, ha="right")

    # Hide x labels for all but bottom
    for ax in [ax1, ax2, ax3]:
        plt.setp(ax.xaxis.get_majorticklabels(), visible=False)

    # ══════════════════════════════════════════════════════════════
    # Add legend for markers
    # ══════════════════════════════════════════════════════════════
    from matplotlib.lines import Line2D
    legend_elements = [
        Line2D([0], [0], marker="^", color="w", markerfacecolor="#00ff88",
               markersize=8, label="Long Entry", linestyle="None"),
        Line2D([0], [0], marker="v", color="w", markerfacecolor="#ff4466",
               markersize=8, label="Long Exit / Short Entry", linestyle="None"),
        Line2D([0], [0], color="#58a6ff", linewidth=2, label="Strategy Equity"),
        Line2D([0], [0], color=COIN_COLORS["BTC"], linewidth=1, linestyle="--",
               alpha=0.5, label="BTC Buy & Hold"),
    ]
    ax1.legend(handles=legend_elements, loc="upper left", fontsize=8,
              facecolor="#161b22", edgecolor="#30363d", labelcolor="#c9d1d9",
              ncol=4)

    plt.savefig("/home/z/my-project/download/momentum_rotation_backtest.png",
               dpi=150, facecolor="#0d1117", edgecolor="none",
               bbox_inches="tight")
    print(f"  Saved: /home/z/my-project/download/momentum_rotation_backtest.png", flush=True)


async def main():
    print("Loading data...", flush=True)
    data = await fetch_data()
    print("Running backtest...", flush=True)
    result = backtest_for_plot(data["1D"])
    if not result:
        print("ERROR: backtest returned None", flush=True)
        return
    print(f"  CAGR: {result['annual_return']:.1f}%, Trades: {len([t for t in result['trades'] if t['type']=='exit'])}", flush=True)
    print("Creating chart...", flush=True)
    create_chart(result)
    print("Done!", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
