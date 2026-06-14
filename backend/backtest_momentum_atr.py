#!/usr/bin/env python3
"""Correct backtest for momentum_atr_trail.

Key insight: generate_signals already implements the full logic
(EMA200 filter + swing breakout entry + ATR trail stop exit).
The backtest engine should just follow the signals:
  1 = long, -1 = short, 0/2/-2 = flat
"""
import asyncio, json, sys, os
from pathlib import Path
from datetime import datetime

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

sys.path.insert(0, os.path.dirname(__file__))
from app.services.data_cache import ensure_candles
from app.services.strategy_loader import get_strategy_code

# ── params ──
STRATEGY_ID = "momentum_atr_trail"
SYMBOL = "BTC-USDT"
TF = "5m"
START = "2024-06-14"
END = "2026-06-14"
INITIAL_CAPITAL = 10000.0
FEE = 0.0005
SIZE_PCT = 0.95


def run_backtest(candles_raw, params):
    df = pd.DataFrame(candles_raw)
    df.columns = ["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"]
    df = df.astype({"open": float, "high": float, "low": float, "close": float, "vol": float})
    df["ts"] = pd.to_datetime(df["ts"].astype(int), unit="ms")
    df = df.sort_values("ts").reset_index(drop=True)
    n = len(df)
    print(f"Loaded {n} candles: {df['ts'].iloc[0]} -> {df['ts'].iloc[-1]}")

    # ── Load & compile strategy ──
    code = get_strategy_code(STRATEGY_ID)
    ns = {"pd": pd, "np": np, "math": __import__("math")}
    exec(code, ns)
    generate_signals = ns.get("generate_signals")
    if not generate_signals:
        raise ValueError("No generate_signals found")

    # ── Get signals ──
    raw = generate_signals(df, params)
    if hasattr(raw, "values"):
        raw = raw.values
    signals = np.array(raw, dtype=int)

    # ── Backtest loop ──
    fee_rate = params.get("fee", FEE)
    size_pct = params.get("size_pct", SIZE_PCT)
    close = df["close"].values
    ts = df["ts"].values

    balance = float(INITIAL_CAPITAL)
    equity = np.full(n, np.nan)
    trades = []
    position = 0.0  # signed contracts
    entry_price = 0.0
    entry_bar = 0
    last_sig = 0

    for i in range(n):
        sig = int(signals[i])
        if sig in (2, -2):
            sig = 0  # treat exit signals as flat
        if last_sig in (2, -2):
            last_sig = 0

        # Compute equity
        if position == 0:
            equity[i] = balance
        else:
            equity[i] = balance + position * (close[i] - entry_price)

        # Closed at 2/-2 on previous iteration
        if sig == 0 and last_sig not in (1, -1):
            last_sig = sig
            continue

        if position != 0:
            # Check if signal wants to exit
            sig_wants_flat = (sig == 0) or (position > 0 and sig == -1) or (position < 0 and sig == 1)
            if sig_wants_flat:
                exit_price = close[i]
                entry_notional = abs(position) * entry_price
                exit_notional = abs(position) * exit_price
                total_fee = (entry_notional + exit_notional) * fee_rate
                pnl = position * (exit_price - entry_price) - total_fee
                balance += pnl
                trades.append({
                    "time": str(ts[i]),
                    "type": "exit",
                    "side": "close_long" if position > 0 else "close_short",
                    "price": exit_price,
                    "pnl": pnl,
                    "balance": balance,
                })
                position = 0.0
                entry_price = 0.0
                # Fall through to possibly re-enter on same bar
                if sig == 0:
                    last_sig = sig
                    continue

        # Enter if signal and not in position
        if position == 0 and sig != 0 and i - entry_bar >= 3:
            entry_price = close[i]
            pos_size = balance * size_pct / entry_price
            position = pos_size if sig == 1 else -pos_size
            entry_bar = i
            trades.append({
                "time": str(ts[i]),
                "type": "entry",
                "side": "buy" if sig == 1 else "sell",
                "price": entry_price,
                "size": abs(position),
                "balance": balance,
            })

        last_sig = sig

    # Close final
    if position != 0:
        exit_price = close[-1]
        entry_notional = abs(position) * entry_price
        exit_notional = abs(position) * exit_price
        total_fee = (entry_notional + exit_notional) * fee_rate
        pnl = position * (exit_price - entry_price) - total_fee
        balance += pnl
        trades.append({
            "time": str(ts[-1]),
            "type": "exit",
            "side": "close_final",
            "price": exit_price,
            "pnl": pnl,
            "balance": balance,
        })
        equity[-1] = balance
        position = 0.0

    # ── Stats ──
    entry_trades = [t for t in trades if t["type"] == "entry"]
    exit_trades = [t for t in trades if t["type"] == "exit"]
    winners = [t for t in exit_trades if t["pnl"] > 0]
    losers = [t for t in exit_trades if t["pnl"] < 0]
    total_return = balance - INITIAL_CAPITAL
    total_return_pct = (total_return / INITIAL_CAPITAL) * 100

    # Sharpe (hourly sampling)
    eq_series = pd.Series([v for v in equity if not np.isnan(v)])
    returns_h = eq_series.pct_change().dropna().values
    sharpe = np.mean(returns_h) / (np.std(returns_h) + 1e-9) * np.sqrt(365 * 24)

    # Max DD
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

    # Profit factor
    sum_win = sum(t["pnl"] for t in winners)
    sum_loss = abs(sum(t["pnl"] for t in losers))
    pf = round(sum_win / sum_loss, 2) if sum_loss > 0 else 0

    avg_win = np.mean([t["pnl"] for t in winners]) if winners else 0
    avg_loss = np.mean([t["pnl"] for t in losers]) if losers else 0

    print(f"\n=== Momentum ATR Trail (correct backtest) ===")
    print(f"Period: {df['ts'].iloc[0].date()} -> {df['ts'].iloc[-1].date()}")
    print(f"Return: {total_return_pct:.2f}%  (${total_return:.0f})")
    print(f"Final: ${balance:.0f}")
    print(f"Trades: {len(exit_trades)} ({len(winners)}W/{len(losers)}L)")
    print(f"WinRate: {len(winners)/max(len(exit_trades),1)*100:.1f}%")
    print(f"Sharpe: {sharpe:.4f}")
    print(f"Max DD: {max_dd*100:.2f}%")
    print(f"Profit Factor: {pf}")
    print(f"Avg Win: ${avg_win:.2f}  Avg Loss: ${avg_loss:.2f}")

    # ── Plot ──
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(18, 10), gridspec_kw={"height_ratios": [3, 1]})
    fig.suptitle(f"Momentum ATR Trail — 2yr {TF} ({SYMBOL})", fontsize=14, fontweight="bold")

    # Price + entries/exits
    dates = df["ts"].values
    ax1.plot(dates, close, color="gray", alpha=0.5, linewidth=0.8, label="Price")
    entry_dates = [pd.Timestamp(t["time"]) for t in entry_trades]
    entry_prices = [t["price"] for t in entry_trades]
    entry_colors = ["green" if t["side"] == "buy" else "red" for t in entry_trades]
    ax1.scatter(entry_dates, entry_prices, c=entry_colors, marker="^", s=60, label="Buy", zorder=5)

    exit_dates = [pd.Timestamp(t["time"]) for t in exit_trades]
    exit_prices = [t["price"] for t in exit_trades]
    exit_markers = ["v" if t["pnl"] > 0 else "x" for t in exit_trades]
    exit_colors = ["lime" if t["pnl"] > 0 else "red" for t in exit_trades]
    for xd, xp, xm, xc in zip(exit_dates, exit_prices, exit_markers, exit_colors):
        ax1.scatter(xd, xp, c=xc, marker=xm, s=80, zorder=5)

    ax1.set_ylabel("Price (USD)")
    ax1.legend(loc="upper left")
    ax1.grid(True, alpha=0.3)

    # Equity curve
    eq_clean = [v if not np.isnan(v) else INITIAL_CAPITAL for v in equity]
    ax2.plot(dates, eq_clean, color="blue", linewidth=1.2, label="Equity")
    ax2.axhline(INITIAL_CAPITAL, color="gray", linestyle="--", alpha=0.5, label=f"Start (${INITIAL_CAPITAL:.0f})")
    ax2.fill_between(dates, eq_clean, INITIAL_CAPITAL, where=[v >= INITIAL_CAPITAL for v in eq_clean],
                     color="green", alpha=0.15)
    ax2.fill_between(dates, eq_clean, INITIAL_CAPITAL, where=[v < INITIAL_CAPITAL for v in eq_clean],
                     color="red", alpha=0.15)

    # Trade dots on equity
    for t in entry_trades:
        td = pd.Timestamp(t["time"])
        idx = (df["ts"] - td).abs().argmin()
        ax2.scatter(td, eq_clean[idx], c="blue", marker="o", s=20, zorder=5)

    for t in exit_trades:
        td = pd.Timestamp(t["time"])
        idx = (df["ts"] - td).abs().argmin()
        ec = "lime" if t["pnl"] > 0 else "red"
        ax2.scatter(td, eq_clean[idx], c=ec, marker="D", s=25, zorder=5)

    ax2.set_ylabel("Equity (USD)")
    ax2.legend(loc="upper left")
    ax2.grid(True, alpha=0.3)

    # Stats box
    stats_text = (
        f"Return: {total_return_pct:+.1f}%  |  Trades: {len(exit_trades)}  |  "
        f"WR: {len(winners)/max(len(exit_trades),1)*100:.0f}%  |  "
        f"Sharpe: {sharpe:.2f}  |  DD: {max_dd*100:.1f}%  |  PF: {pf}"
    )
    fig.text(0.5, 0.01, stats_text, ha="center", fontsize=11,
             bbox=dict(boxstyle="round", facecolor="lightyellow", alpha=0.9))

    plt.tight_layout(rect=[0, 0.04, 1, 0.97])

    out_dir = Path(__file__).parent.parent / "backtests_data"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "momentum_atr_trail_2yr.png"
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"\nChart saved: {out_path}")


async def main():
    print(f"Loading {SYMBOL} {TF} data: {START} -> {END} ...")
    candles = await ensure_candles(
        SYMBOL, TF,
        start_date=START,
        end_date=END,
        force_refresh=False,
        max_candles=300000,
    )
    print(f"Got {len(candles)} candles")

    params = {
        "ema_trend": 200,
        "swing_window": 30,
        "atr_period": 14,
        "atr_mult": 2.0,
        "size_pct": SIZE_PCT,
        "fee": FEE,
    }

    run_backtest(candles, params)


if __name__ == "__main__":
    asyncio.run(main())
