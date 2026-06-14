#!/usr/bin/env python3
"""Backtest all 5 strategies on 6 months 1H BTC-USDT with charts."""
import asyncio, json, sys, os, math
from pathlib import Path
from datetime import datetime

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import FancyBboxPatch

sys.path.insert(0, os.path.dirname(__file__))
from app.services.data_cache import ensure_candles
from app.services.strategy_loader import get_strategy_code

# ── Config ──
SYMBOL = "BTC-USDT"
TF = "1H"
START = "2025-12-14"
END = "2026-06-14"
INITIAL_CAPITAL = 10000.0
STRATEGIES = {
    "momentum_atr_trail":  {"fee": 0.0005, "size_pct": 0.95},
    "trend_momentum_pro":  {"fee": 0.0005, "size_pct": 0.95},
    "trend_bounce_levx":   {"fee": 0.0005, "size_pct": 0.25, "leverage": 10},
    "trend_bounce_rapid":  {"fee": 0.0005, "size_pct": 0.20},
    "micro_scalper":       {"fee": 0.0005, "size_pct": 0.15},
}

def _load_params(strategy_id: str) -> dict:
    """Load @params from the strategy file header."""
    p = Path(__file__).parent / "strategies" / f"{strategy_id}.py"
    if not p.exists():
        return {}
    for line in p.read_text().split("\n")[:20]:
        if line.startswith("# @params:"):
            try:
                return json.loads(line[len("# @params:"):].strip())
            except Exception:
                return {}
    return {}


def backtest_one(sid: str, code: str, candles_raw, params_overrides: dict, initial_capital: float):
    df = pd.DataFrame(candles_raw)
    df.columns = ["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"]
    df = df.astype({"open": float, "high": float, "low": float, "close": float, "vol": float})
    df["ts"] = pd.to_datetime(df["ts"].astype(int), unit="ms")
    df = df.sort_values("ts").reset_index(drop=True)
    n = len(df)

    # Compile strategy
    ns = {"pd": pd, "np": np, "math": math}
    exec(code, ns)
    generate_signals = ns.get("generate_signals")

    # Load @params and merge overrides
    params = _load_params(sid)
    params.update(params_overrides)

    # Generate signals
    raw = generate_signals(df, params)
    if hasattr(raw, "values"):
        raw = raw.values
    signals = np.array(raw, dtype=int)
    has_self_stops = bool(np.any((signals == 2) | (signals == -2)))

    fee_rate = params.get("fee", 0.001)
    size_pct = params.get("size_pct", 0.95)
    close = df["close"].values
    ts = df["ts"].values

    leverage = params.get("leverage", 1)
    balance = float(initial_capital)
    equity = np.full(n, np.nan)
    trades = []
    position = 0.0
    entry_price = 0.0
    entry_bar = 0

    for i in range(n):
        sig = int(signals[i])
        if sig in (2, -2):
            sig = 0

        if position == 0:
            equity[i] = balance
        else:
            equity[i] = balance + position * (close[i] - entry_price)

        # Exit on signal
        if position != 0 and sig == 0:
            exit_price = close[i]
            en = abs(position) * entry_price
            xn = abs(position) * exit_price
            total_fee = (en + xn) * fee_rate
            pnl = position * (exit_price - entry_price) - total_fee
            balance += pnl
            trades.append({"time": str(ts[i]), "type": "exit", "side": "close_long" if position > 0 else "close_short",
                          "price": exit_price, "pnl": pnl, "balance": balance})
            position = 0.0
            entry_price = 0.0
            continue

        # Flip
        if position > 0 and sig == -1:
            exit_price = close[i]
            en = abs(position) * entry_price
            xn = abs(position) * exit_price
            total_fee = (en + xn) * fee_rate
            pnl = position * (exit_price - entry_price) - total_fee
            balance += pnl
            trades.append({"time": str(ts[i]), "type": "exit", "side": "close_long",
                          "price": exit_price, "pnl": pnl, "balance": balance})
            position = 0.0
            entry_price = 0.0

        if position < 0 and sig == 1:
            exit_price = close[i]
            en = abs(position) * entry_price
            xn = abs(position) * exit_price
            total_fee = (en + xn) * fee_rate
            pnl = position * (exit_price - entry_price) - total_fee
            balance += pnl
            trades.append({"time": str(ts[i]), "type": "exit", "side": "close_short",
                          "price": exit_price, "pnl": pnl, "balance": balance})
            position = 0.0
            entry_price = 0.0

        if i - entry_bar < 3 and position == 0:
            continue

        # Entry
        if position == 0 and sig != 0:
            entry_price = close[i]
            pos_size = balance * size_pct * leverage / entry_price
            position = pos_size if sig == 1 else -pos_size
            entry_bar = i
            trades.append({"time": str(ts[i]), "type": "entry", "side": "buy" if sig == 1 else "sell",
                          "price": entry_price, "size": abs(position), "balance": balance})

    # Close final
    if position != 0:
        exit_price = close[-1]
        en = abs(position) * entry_price
        xn = abs(position) * exit_price
        total_fee = (en + xn) * fee_rate
        pnl = position * (exit_price - entry_price) - total_fee
        balance += pnl
        trades.append({"time": str(ts[-1]), "type": "exit", "side": "close_final",
                      "price": exit_price, "pnl": pnl, "balance": balance})
        equity[-1] = balance
        position = 0.0

    # Stats
    exit_trades = [t for t in trades if t["type"] == "exit"]
    winners = [t for t in exit_trades if t["pnl"] > 0]
    losers = [t for t in exit_trades if t["pnl"] < 0]
    total_return = balance - initial_capital
    total_return_pct = (total_return / initial_capital) * 100

    eq_series = pd.Series([v for v in equity if not np.isnan(v)])
    returns_h = eq_series.pct_change().dropna().values
    sharpe = np.mean(returns_h) / (np.std(returns_h) + 1e-9) * np.sqrt(365 * 24)

    peak = equity[0]
    max_dd = 0.0
    for v in equity:
        if np.isnan(v):
            continue
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    sum_win = sum(t["pnl"] for t in winners)
    sum_loss = abs(sum(t["pnl"] for t in losers))
    pf = round(sum_win / sum_loss, 2) if sum_loss > 0 else 0

    return {
        "sid": sid,
        "equity": equity,
        "trades": trades,
        "dates": df["ts"].values,
        "stats": {
            "return_pct": round(total_return_pct, 2),
            "return_abs": round(total_return, 2),
            "final": round(balance, 2),
            "trades": len(exit_trades),
            "wins": len(winners),
            "losses": len(losers),
            "win_rate": round(len(winners) / max(len(exit_trades), 1) * 100, 1),
            "sharpe": round(sharpe, 4),
            "max_dd": round(max_dd * 100, 2),
            "pf": pf,
            "avg_win": round(np.mean([t["pnl"] for t in winners]), 2) if winners else 0,
            "avg_loss": round(np.mean([t["pnl"] for t in losers]), 2) if losers else 0,
            "self_stops": has_self_stops,
        }
    }


async def main():
    print(f"Loading {SYMBOL} {TF}: {START} -> {END} ...")
    candles = await ensure_candles(SYMBOL, TF, start_date=START, end_date=END,
                                    force_refresh=False, max_candles=100000)
    print(f"Got {len(candles)} candles")

    results = {}
    for sid, overrides in STRATEGIES.items():
        print(f"\n--- {sid} ---")
        code = get_strategy_code(sid)
        if not code:
            print(f"  SKIP: no code")
            continue
        r = backtest_one(sid, code, candles, overrides, INITIAL_CAPITAL)
        s = r["stats"]
        results[sid] = r
        lv = overrides.get("leverage", 1)
        print(f"  Return: {s['return_pct']:+.1f}%  Trades: {s['trades']}  "
              f"WR: {s['win_rate']}%  Sharpe: {s['sharpe']:.2f}  "
              f"DD: {s['max_dd']}%  PF: {s['pf']}  "
              f"SelfStops: {'✓' if s['self_stops'] else '×'}  Lev: {lv}x")

    # ── Multi-panel chart ──
    n_strats = len(results)
    fig, axes = plt.subplots(n_strats + 1, 1, figsize=(18, 4 * (n_strats + 1)),
                             sharex=True, gridspec_kw={"height_ratios": [1.5] + [1] * n_strats})
    fig.suptitle(f"All Strategies — 6mo {TF} {SYMBOL}  (${INITIAL_CAPITAL:.0f} start)", fontsize=14, fontweight="bold")

    dates = list(results.values())[0]["dates"]
    close = pd.DataFrame(candles).iloc[:, 4].astype(float).values
    axes[0].plot(dates, close, color="gray", alpha=0.5, linewidth=0.8)
    axes[0].set_ylabel("Price", fontsize=10)
    axes[0].set_title("BTC-USDT Price", fontsize=10)
    axes[0].grid(True, alpha=0.3)

    colors = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd"]
    for idx, (sid, res) in enumerate(results.items()):
        ax = axes[idx + 1]
        s = res["stats"]
        eq = [v if not np.isnan(v) else INITIAL_CAPITAL for v in res["equity"]]
        c = colors[idx % len(colors)]

        ax.plot(dates, eq, color=c, linewidth=1.0, label=sid)
        ax.axhline(INITIAL_CAPITAL, color="gray", linestyle="--", alpha=0.4)
        ax.fill_between(dates, eq, INITIAL_CAPITAL,
                        where=[v >= INITIAL_CAPITAL for v in eq],
                        color=c, alpha=0.1)
        ax.fill_between(dates, eq, INITIAL_CAPITAL,
                        where=[v < INITIAL_CAPITAL for v in eq],
                        color="red", alpha=0.08)

        # Trade markers
        for t in res["trades"]:
            td = pd.Timestamp(t["time"])
            if t["type"] == "entry" and sid in ("momentum_atr_trail", "trend_momentum_pro"):
                idx_ts = (pd.Series(dates) - td).abs().argmin()
                me = "o"
                ax.scatter(td, eq[idx_ts], c=c, marker=me, s=12, zorder=5, alpha=0.4)

        ax.set_ylabel("Equity ($)", fontsize=9)
        lv = STRATEGIES.get(sid, {}).get("leverage", 1)
        label = f"{sid}  |  {s['return_pct']:+.1f}%  WR {s['win_rate']}%  𝚂{s['sharpe']:.2f}  DD {s['max_dd']}%  PF{s['pf']}  {s['trades']}t  {lv}x"
        ax.legend([label], loc="upper left", fontsize=8)
        ax.grid(True, alpha=0.3)

    plt.tight_layout(rect=[0, 0, 1, 0.98])
    out_dir = Path(__file__).parent.parent / "backtests_data"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"all_strategies_{TF}_6mo.png"
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"\nChart saved: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
