#!/usr/bin/env python3
"""1-year 5m backtest for the 3 running strategies with OKX fees."""
import asyncio, json, sys, os, math
from pathlib import Path

import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

sys.path.insert(0, os.path.dirname(__file__))
from app.services.data_cache import ensure_candles
from app.services.strategy_loader import get_strategy_code

SYMBOL = "BTC-USDT"
TF = "5m"
START = "2025-06-14"
END = "2026-06-14"
INITIAL = 10000.0

# The 3 running strategies with realistic OKX taker fees (0.05% = 0.0005)
STRATEGIES = {
    "trend_bounce_levx": {
        "params": {"fee": 0.0005, "size_pct": 0.25, "leverage": 10},
    },
    "trend_momentum_pro": {
        "params": {"fee": 0.0005, "size_pct": 0.95},
    },
    "momentum_atr_trail": {
        "params": {"fee": 0.0005, "size_pct": 0.95},
    },
}


def _load_file_params(sid):
    p = Path(__file__).parent / "strategies" / f"{sid}.py"
    if not p.exists():
        return {}
    for line in p.read_text().split("\n")[:20]:
        if line.startswith("# @params:"):
            try:
                return json.loads(line[len("# @params:"):].strip())
            except Exception:
                return {}
    return {}


def run_backtest(sid, code, candles, params):
    df = pd.DataFrame(candles)
    df.columns = ["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"]
    df = df.astype({"open": float, "high": float, "low": float, "close": float, "vol": float})
    df["ts"] = pd.to_datetime(df["ts"].astype(int), unit="ms")
    df = df.sort_values("ts").reset_index(drop=True)
    n = len(df)

    ns = {"pd": pd, "np": np, "math": math}
    exec(code, ns)
    signals_fn = ns.get("generate_signals")

    file_params = _load_file_params(sid)
    file_params.update(params)
    params = file_params

    raw = signals_fn(df, params)
    if hasattr(raw, "values"):
        raw = raw.values
    sigs = np.array(raw, dtype=int)
    has_self_stops = bool(np.any((sigs == 2) | (sigs == -2)))

    fee = params.get("fee", 0.001)
    size_pct = params.get("size_pct", 0.95)
    lev = params.get("leverage", 1)
    close = df["close"].values
    ts = df["ts"].values

    balance = float(INITIAL)
    equity = np.full(n, np.nan)
    trades = []
    position = 0.0
    entry_price = 0.0
    fees_paid = 0.0

    for i in range(n):
        sig = int(sigs[i])
        if sig in (2, -2):
            sig = 0

        equity[i] = balance if position == 0 else balance + position * (close[i] - entry_price)

        # Exit on signal
        if position != 0 and sig == 0:
            exit_p = close[i]
            en = abs(position) * entry_price
            xn = abs(position) * exit_p
            tf = (en + xn) * fee
            fees_paid += tf
            pnl = position * (exit_p - entry_price) - tf
            balance += pnl
            trades.append({"time": str(ts[i]), "type": "exit",
                           "pnl": pnl, "fee": tf, "balance": balance})
            position = 0.0
            entry_price = 0.0
            continue

        # Flip: exit + enter opposite
        if position > 0 and sig == -1:
            exit_p = close[i]
            en = abs(position) * entry_price
            xn = abs(position) * exit_p
            tf = (en + xn) * fee
            fees_paid += tf
            pnl = position * (exit_p - entry_price) - tf
            balance += pnl
            trades.append({"time": str(ts[i]), "type": "exit", "pnl": pnl, "fee": tf, "balance": balance})
            position = 0.0
            entry_price = 0.0

        if position < 0 and sig == 1:
            exit_p = close[i]
            en = abs(position) * entry_price
            xn = abs(position) * exit_p
            tf = (en + xn) * fee
            fees_paid += tf
            pnl = position * (exit_p - entry_price) - tf
            balance += pnl
            trades.append({"time": str(ts[i]), "type": "exit", "pnl": pnl, "fee": tf, "balance": balance})
            position = 0.0
            entry_price = 0.0

        # Cooldown
        if position == 0 and i > 0 and sig == 0:
            continue

        # Entry
        if position == 0 and sig != 0:
            entry_price = close[i]
            pos_size = balance * size_pct * lev / entry_price
            position = pos_size if sig == 1 else -pos_size
            trades.append({"time": str(ts[i]), "type": "entry", "price": entry_price,
                          "size": abs(position), "balance": balance})

    if position != 0:
        exit_p = close[-1]
        en = abs(position) * entry_price
        xn = abs(position) * exit_p
        tf = (en + xn) * fee
        fees_paid += tf
        pnl = position * (exit_p - entry_price) - tf
        balance += pnl
        trades.append({"time": str(ts[-1]), "type": "exit", "pnl": pnl, "fee": tf, "balance": balance})
        equity[-1] = balance
        position = 0.0

    exits = [t for t in trades if t["type"] == "exit"]
    wins = [t for t in exits if t["pnl"] > 0]
    losses = [t for t in exits if t["pnl"] < 0]
    ret = balance - INITIAL
    ret_pct = (ret / INITIAL) * 100
    # Also compute return without fees
    gross_pnl = sum(t["pnl"] + t["fee"] for t in exits)
    ret_gross_pct = (gross_pnl / INITIAL) * 100

    eq_clean = pd.Series([v for v in equity if not np.isnan(v)])
    returns = eq_clean.pct_change().dropna().values
    sharpe = float(np.mean(returns) / (np.std(returns) + 1e-9) * np.sqrt(365 * 24))

    peak = equity[0]
    mdd = 0.0
    for v in equity:
        if np.isnan(v):
            continue
        if v > peak:
            peak = v
        dd = (peak - v) / peak if peak > 0 else 0
        if dd > mdd:
            mdd = dd

    sum_w = sum(t["pnl"] for t in wins)
    sum_l = abs(sum(t["pnl"] for t in losses))
    pf = round(sum_w / sum_l, 2) if sum_l > 0 else 0

    return {
        "equity": equity, "trades": trades, "dates": df["ts"].values,
        "fees_paid": round(fees_paid, 2),
        "ret_gross_pct": round(ret_gross_pct, 2),
        "stats": {
            "ret_pct": round(ret_pct, 2),
            "ret_abs": round(ret, 2),
            "final": round(balance, 2),
            "trades": len(exits),
            "wins": len(wins), "losses": len(losses),
            "wr": round(len(wins) / max(len(exits), 1) * 100, 1),
            "sharpe": round(sharpe, 4),
            "mdd": round(mdd * 100, 2),
            "pf": pf,
            "avg_w": round(np.mean([t["pnl"] for t in wins]), 2) if wins else 0,
            "avg_l": round(np.mean([t["pnl"] for t in losses]), 2) if losses else 0,
            "lev": lev,
            "self_stops": has_self_stops,
            "fee_pct": fee,
        },
    }


async def main():
    print(f"Loading {SYMBOL} {TF} 1 year: {START} -> {END}")
    candles = await ensure_candles(SYMBOL, TF, start_date=START, end_date=END,
                                    force_refresh=False, max_candles=300000)
    print(f"Got {len(candles)} candles: {pd.to_datetime(int(candles[0][0]), unit='ms').date()} -> "
          f"{pd.to_datetime(int(candles[-1][0]), unit='ms').date()}")

    results = {}
    for sid, cfg in STRATEGIES.items():
        print(f"\n{'='*60}")
        print(f"  {sid}")
        print(f"{'='*60}")
        code = get_strategy_code(sid)
        if not code:
            print(f"  SKIP — no code")
            continue
        r = run_backtest(sid, code, candles, cfg["params"])
        s = r["stats"]
        results[sid] = r

        line = (f"  Return: {s['ret_pct']:+.2f}%  (gross: {r['ret_gross_pct']:+.2f}%)"
                f"  Final: ${s['final']:,.0f}")
        print(line)
        print(f"  Trades: {s['trades']}  ({s['wins']}W/{s['losses']}L)"
              f"  WR: {s['wr']}%  Sharpe: {s['sharpe']:.2f}")
        print(f"  Max DD: {s['mdd']}%  PF: {s['pf']}  Lev: {s['lev']}x")
        print(f"  Avg Win: ${s['avg_w']:.2f}  Avg Loss: ${s['avg_l']:.2f}")
        print(f"  Total fees paid: ${r['fees_paid']:.2f}")
        print(f"  Fee rate: {s['fee_pct']*100:.2f}%  Self-stops: {'✓' if s['self_stops'] else '×'}")

    # ── Combined chart ──
    cols = 3
    fig, axes = plt.subplots(2, cols, figsize=(20, 10),
                             gridspec_kw={"height_ratios": [1, 2], "hspace": 0.3})
    colors = {"trend_bounce_levx": "#d62728", "trend_momentum_pro": "#ff7f0e", "momentum_atr_trail": "#2ca02c"}
    names = {"trend_bounce_levx": "LevX", "trend_momentum_pro": "Momentum Pro", "momentum_atr_trail": "ATR Trail"}

    for idx, (sid, r) in enumerate(results.items()):
        s = r["stats"]
        c = colors[sid]
        dates = r["dates"]
        eq = [v if not np.isnan(v) else INITIAL for v in r["equity"]]

        # Equity curve
        ax = axes[1][idx]
        ax.plot(dates, eq, color=c, linewidth=0.9)
        ax.axhline(INITIAL, color="gray", linestyle="--", alpha=0.4)
        ax.fill_between(dates, eq, INITIAL, where=[v >= INITIAL for v in eq],
                        color=c, alpha=0.08)
        ax.fill_between(dates, eq, INITIAL, where=[v < INITIAL for v in eq],
                        color="red", alpha=0.06)

        # Trade markers
        for t in r["trades"]:
            if t["type"] == "entry":
                td = pd.Timestamp(t["time"])
                eq_at = eq[(pd.Series(dates) - td).abs().argmin()]
                ax.scatter(td, eq_at, c=c, marker="o", s=8, alpha=0.3, zorder=5)

        label = (f"{names[sid]}  |  {s['ret_pct']:+.1f}%  WR{s['wr']}%  "
                 f"𝚺{s['sharpe']:.2f}  DD{s['mdd']}%  PF{s['pf']}  {s['trades']}t  {s['lev']}x")
        ax.set_title(label, fontsize=10)
        ax.set_ylabel("Equity ($)", fontsize=9)
        ax.grid(True, alpha=0.3)

        # Monthly bars
        ax2 = axes[0][idx]
        df_m = pd.DataFrame({"ts": pd.to_datetime(dates), "eq": eq})
        df_m = df_m.resample("ME", on="ts")["eq"].last().ffill()
        ret_m = df_m.pct_change() * 100
        ret_m = ret_m.iloc[1:]
        colors_m = ["green" if v >= 0 else "red" for v in ret_m]
        ax2.bar(range(len(ret_m)), ret_m, color=colors_m, width=0.7)
        ax2.axhline(0, color="gray", linewidth=0.5)
        ax2.set_xticks(range(len(ret_m)))
        ax2.set_xticklabels([d.strftime("%b") for d in ret_m.index], fontsize=7, rotation=45)
        ax2.set_ylabel("Mo. %", fontsize=9)
        ax2.set_title(f"{names[sid]} — Monthly Return", fontsize=10)
        ax2.grid(True, alpha=0.3, axis="y")

    fig.suptitle(f"Running Strategies — 1 Year {TF} {SYMBOL} (${INITIAL:.0f} start, fee {STRATEGIES[list(STRATEGIES.keys())[0]]['params']['fee']*100:.2f}%)",
                 fontsize=13, fontweight="bold")
    plt.tight_layout(rect=[0, 0, 1, 0.97])

    out_dir = Path(__file__).parent.parent / "backtests_data"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "running_strategies_1yr_5m.png"
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"\n{'='*60}")
    print(f"Chart saved: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
