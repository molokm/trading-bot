#!/usr/bin/env python3
"""Backtest all loaded strategies: $1000 start, 1x, 0.05% fee, 1 year 5m."""
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
END   = "2026-06-14"
INITIAL = 1000.0
FEE = 0.0005

STRATEGIES = [
    "trend_bounce_levx",
    "trend_bounce_levx_pro",
    "trend_momentum_pro",
    "momentum_atr_trail",
    "trend_bounce_rapid",
    "micro_scalper",
]

COLORS = ["#1f77b4", "#ff7f0e", "#2ca02c", "#d62727", "#9467bd", "#8c564b"]


def load_params(sid):
    p = Path(__file__).parent / "strategies" / f"{sid}.py"
    if not p.exists():
        return {}
    for line in p.read_text().split("\n")[:20]:
        if line.startswith("# @params:"):
            try:
                return json.loads(line.split("@params:", 1)[1].strip())
            except:
                pass
    return {}


def backtest(sid, code, candles, params):
    df = pd.DataFrame(candles)
    df.columns = ["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"]
    for c in ("open", "high", "low", "close", "vol"):
        df[c] = df[c].astype(float)
    df["ts"] = pd.to_datetime(df["ts"].astype(int), unit="ms")
    df.sort_values("ts", inplace=True)
    df.reset_index(drop=True, inplace=True)
    n = len(df)

    ns = {"pd": pd, "np": np, "math": math}
    exec(code, ns)
    raw = ns["generate_signals"](df, params)
    if hasattr(raw, "values"):
        raw = raw.values
    signals = np.array(raw, dtype=int)

    close = df["close"].values
    ts    = df["ts"].values

    balance      = float(INITIAL)
    position     = 0.0
    entry_price  = 0.0
    fees_paid    = 0.0
    trades       = []
    equity_curve = []
    entry_bar    = -999

    for i in range(n):
        sig = int(signals[i])
        if sig in (2, -2):
            sig = 0

        if position == 0:
            equity_curve.append(balance)
        else:
            equity_curve.append(balance + position * (close[i] - entry_price))

        # EXIT
        if position != 0:
            should_exit = False
            if sig == 0:
                should_exit = True
            elif (position > 0 and sig == -1) or (position < 0 and sig == 1):
                should_exit = True

            if should_exit:
                entry_notional = abs(position) * entry_price
                exit_notional  = abs(position) * close[i]
                total_fee = (entry_notional + exit_notional) * FEE
                pnl = position * (close[i] - entry_price) - total_fee
                fees_paid += total_fee
                balance += pnl
                trades.append({"pnl": pnl, "fee": total_fee})
                position = 0.0
                entry_price = 0.0

        # ENTRY
        if position == 0 and sig != 0 and i - entry_bar >= 3:
            entry_price = close[i]
            position = (balance * 0.95 / entry_price) if sig == 1 else -(balance * 0.95 / entry_price)
            entry_bar = i

    # Force-close
    if position != 0:
        entry_notional = abs(position) * entry_price
        exit_notional  = abs(position) * close[-1]
        total_fee = (entry_notional + exit_notional) * FEE
        pnl = position * (close[-1] - entry_price) - total_fee
        fees_paid += total_fee
        balance += pnl
        trades.append({"pnl": pnl, "fee": total_fee})
        equity_curve[-1] = balance

    # Stats
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    n_trades = len(trades)
    n_wins   = len(wins)
    wr = n_wins / max(n_trades, 1) * 100

    gross_pnl = sum(t["pnl"] + t["fee"] for t in trades)
    net_pnl   = balance - INITIAL
    net_ret   = net_pnl / INITIAL * 100
    gross_ret = gross_pnl / INITIAL * 100
    fees_pct  = fees_paid / max(abs(gross_pnl), 1) * 100

    avg_win  = float(np.mean([t["pnl"] for t in wins]))   if wins   else 0
    avg_loss = float(np.mean([t["pnl"] for t in losses])) if losses else 0
    pf = (sum(t["pnl"] for t in wins) /
          max(abs(sum(t["pnl"] for t in losses)), 0.01)) if losses else 0

    eq = np.array(equity_curve, dtype=float)
    peak = eq[0]
    mdd = 0.0
    for v in eq:
        if v > peak: peak = v
        dd = (peak - v) / peak if peak > 0 else 0
        if dd > mdd: mdd = dd

    returns = pd.Series(eq).pct_change().dropna().values
    sharpe = float(np.mean(returns) / (np.std(returns) + 1e-9) * np.sqrt(365 * 24))

    dates = pd.to_datetime(ts)
    eq_s = pd.Series(eq, index=dates)
    monthly = eq_s.resample("ME").last().ffill().pct_change().dropna() * 100

    return {
        "dates": dates, "equity": eq, "monthly": monthly,
        "stats": {
            "net_ret": round(net_ret, 2),
            "gross_ret": round(gross_ret, 2),
            "fees_paid": round(fees_paid, 2),
            "fees_pct": round(fees_pct, 1),
            "final": round(balance, 2),
            "n_trades": n_trades,
            "n_wins": n_wins,
            "wr": round(wr, 1),
            "sharpe": round(sharpe, 2),
            "mdd": round(mdd * 100, 2),
            "pf": round(pf, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
        },
    }


async def main():
    print(f"Loading {SYMBOL} {TF}  {START} → {END}")
    candles = await ensure_candles(SYMBOL, TF, start_date=START, end_date=END,
                                    force_refresh=False, max_candles=300000)
    print(f"Candles: {len(candles)}\n")

    results = {}
    for sid in STRATEGIES:
        code = get_strategy_code(sid)
        if not code:
            print(f"SKIP {sid}")
            continue
        params = load_params(sid)
        params.update({"leverage": 1, "size_pct": 0.95, "fee": FEE})
        r = backtest(sid, code, candles, params)
        results[sid] = r

    # ── Summary table ──
    print(f"{'='*90}")
    print(f"  ALL STRATEGIES — 1 Year {TF}  {SYMBOL}  ${INITIAL:.0f} start  fee 0.05%  1x leverage")
    print(f"{'='*90}")
    print(f"{'Strategy':<25} {'Net%':>8} {'Final$':>8} {'Trades':>7} {'WR%':>6} {'Sharpe':>7} {'DD%':>6} {'PF':>6} {'Fees$':>8} {'Fees%':>6}")
    print(f"{'-'*90}")

    sorted_results = sorted(results.items(), key=lambda x: x[1]["stats"]["net_ret"], reverse=True)
    for sid, r in sorted_results:
        s = r["stats"]
        print(f"{sid:<25} {s['net_ret']:>+7.2f}% {s['final']:>8,.0f} {s['n_trades']:>7} {s['wr']:>5.1f}% {s['sharpe']:>7.2f} {s['mdd']:>5.2f}% {s['pf']:>5.2f} {s['fees_paid']:>8,.0f} {s['fees_pct']:>5.0f}%")

    print(f"{'-'*90}")
    print()

    # ── Monthly breakdown ──
    print(f"  MONTHLY BREAKDOWN (% return)")
    print(f"{'-'*90}")
    all_months = sorted(set().union(*(r["monthly"].index for r in results.values())))
    header = f"{'Month':>10}" + "".join(f" {s:>16}" for s in results)
    print(header)
    print("-" * len(header))
    for m in all_months:
        row = f"{m.strftime('%Y-%m'):>10}"
        for sid in results:
            val = results[sid]["monthly"].get(m, float("nan"))
            row += f" {val:>+15.2f}%" if not np.isnan(val) else f" {'—':>16}"
        print(row)
    print()

    # ── Chart ──
    n_strats = len(results)
    fig, axes = plt.subplots(2, n_strats, figsize=(7 * n_strats, 10),
                             gridspec_kw={"height_ratios": [1, 2.5], "wspace": 0.35, "hspace": 0.35})
    if n_strats == 1:
        axes = axes.reshape(2, 1)

    names = {
        "trend_bounce_levx": "LevX Original",
        "trend_bounce_levx_pro": "LevX Pro",
        "trend_momentum_pro": "Momentum Pro",
        "momentum_atr_trail": "ATR Trail",
        "trend_bounce_rapid": "Bounce Rapid",
        "micro_scalper": "Micro Scalper",
    }

    for idx, (sid, r) in enumerate(sorted_results):
        s = r["stats"]
        c = COLORS[idx % len(COLORS)]
        dates = r["dates"]
        eq = r["equity"]

        # Monthly
        ax_m = axes[0][idx]
        monthly = r["monthly"]
        colors_m = ["#2ca02c" if v >= 0 else "#d62728" for v in monthly.values]
        ax_m.bar(range(len(monthly)), monthly.values, color=colors_m, width=0.7)
        ax_m.axhline(0, color="gray", linewidth=0.5)
        ax_m.set_xticks(range(len(monthly)))
        ax_m.set_xticklabels([d.strftime("%b") for d in monthly.index], fontsize=7, rotation=45)
        ax_m.set_ylabel("Return %")
        name = names.get(sid, sid)
        ax_m.set_title(f"{name} — Monthly", fontsize=10)
        ax_m.grid(True, alpha=0.3, axis="y")

        # Equity
        ax_e = axes[1][idx]
        ax_e.plot(dates, eq, color=c, linewidth=0.8)
        ax_e.axhline(INITIAL, color="gray", linestyle="--", alpha=0.4, linewidth=0.5)
        ax_e.fill_between(dates, eq, INITIAL, where=eq >= INITIAL, color=c, alpha=0.08)
        ax_e.fill_between(dates, eq, INITIAL, where=eq < INITIAL, color="#d62728", alpha=0.06)
        label = (f"{name}\nNet {s['net_ret']:+.1f}%  WR {s['wr']}%  "
                 f"Sharpe {s['sharpe']}  DD {s['mdd']}%\n"
                 f"Trades {s['n_trades']}  PF {s['pf']}")
        ax_e.set_title(label, fontsize=8)
        ax_e.set_ylabel("Equity ($)")
        ax_e.grid(True, alpha=0.3)

    fig.suptitle(f"All Strategies — 1 Year {TF}  {SYMBOL}  "
                 f"(${INITIAL:.0f} start, fee 0.05%, 1x)",
                 fontsize=13, fontweight="bold")
    plt.tight_layout(rect=[0, 0, 1, 0.96])

    out = Path(__file__).parent.parent / "backtests_data" / "all_strategies_1yr_5m_1k.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Chart: {out}")


if __name__ == "__main__":
    asyncio.run(main())
