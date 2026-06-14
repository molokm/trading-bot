#!/usr/bin/env python3
"""Definitive 1-year 5m backtest for the 3 running strategies.

Same logic for ALL strategies:
  1 → long, -1 → short, 0/2/-2 → flat (exit)
  Position sizing: balance * size_pct * leverage / entry_price
  Fees: (entry_notional + exit_notional) * fee_rate on EVERY trade
"""
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
INITIAL = 10000.0

# Running strategies — only these 3
STRATEGIES = {
    "trend_bounce_levx":  {"leverage": 10, "size_pct": 0.25, "fee": 0.0005},
    "trend_momentum_pro": {"leverage": 1,  "size_pct": 0.95, "fee": 0.0005},
    "momentum_atr_trail": {"leverage": 1,  "size_pct": 0.95, "fee": 0.0005},
}

def load_file_params(sid):
    p = Path(__file__).parent / "strategies" / f"{sid}.py"
    if not p.exists():
        return {}
    for line in p.read_text().split("\n")[:20]:
        if line.startswith("# @params:"):
            try:
                return json.loads(line.split("@params:", 1)[1].strip())
            except Exception:
                pass
    return {}


def backtest(sid, code, candles, overrides):
    """Universal backtest — same logic for every strategy."""
    # ── Build DataFrame ──
    df = pd.DataFrame(candles)
    df.columns = ["ts", "open", "high", "low", "close", "vol", "volCcy", "volCcyQuote", "confirm"]
    for c in ("open", "high", "low", "close", "vol"):
        df[c] = df[c].astype(float)
    df["ts"] = pd.to_datetime(df["ts"].astype(int), unit="ms")
    df.sort_values("ts", inplace=True)
    df.reset_index(drop=True, inplace=True)
    n = len(df)

    # ── Merge params ──
    params = load_file_params(sid)
    params.update(overrides)

    fee       = params.get("fee", 0.001)
    size_pct  = params.get("size_pct", 0.95)
    leverage  = params.get("leverage", 1)

    # ── Generate signals (ONE call) ──
    ns = {"pd": pd, "np": np, "math": math}
    exec(code, ns)
    raw = ns["generate_signals"](df, params)
    if hasattr(raw, "values"):
        raw = raw.values
    signals = np.array(raw, dtype=int)          # raw signal array

    close = df["close"].values
    ts    = df["ts"].values

    # ── Core loop ──
    balance       = float(INITIAL)
    position      = 0.0          # signed qty
    entry_price   = 0.0
    fees_paid     = 0.0
    trades        = []           # list of dicts for each completed trade
    equity_curve  = []
    last_entry_bar = -999

    for i in range(n):
        sig = int(signals[i])
        # Treat 2/-2 as flat (strategy's own trail exit)
        if sig in (2, -2):
            sig = 0

        # Snapshot equity
        if position == 0:
            equity_curve.append(balance)
        else:
            equity_curve.append(balance + position * (close[i] - entry_price))

        # ── EXIT ──
        if position != 0:
            should_exit = False
            # Signal goes flat
            if sig == 0:
                should_exit = True
            # Signal flips direction
            elif (position > 0 and sig == -1) or (position < 0 and sig == 1):
                should_exit = True

            if should_exit:
                exit_price = close[i]
                entry_notional = abs(position) * entry_price
                exit_notional  = abs(position) * exit_price
                total_fee      = (entry_notional + exit_notional) * fee
                pnl = position * (exit_price - entry_price) - total_fee
                fees_paid += total_fee
                balance += pnl
                trades.append({
                    "bar":       i,
                    "ts":        str(ts[i]),
                    "side":      "long" if position > 0 else "short",
                    "entry":     round(entry_price, 2),
                    "exit":      round(exit_price, 2),
                    "pnl":       round(pnl, 2),
                    "fee":       round(total_fee, 2),
                    "balance":   round(balance, 2),
                })
                position    = 0.0
                entry_price = 0.0

        # ── ENTRY (only when flat) ──
        if position == 0 and sig != 0 and i - last_entry_bar >= 3:
            entry_price = close[i]
            pos_size = balance * size_pct * leverage / entry_price
            position = pos_size if sig == 1 else -pos_size
            last_entry_bar = i

    # ── Force-close at end ──
    if position != 0:
        exit_price = close[-1]
        entry_notional = abs(position) * entry_price
        exit_notional  = abs(position) * exit_price
        total_fee      = (entry_notional + exit_notional) * fee
        pnl = position * (exit_price - entry_price) - total_fee
        fees_paid += total_fee
        balance += pnl
        trades.append({
            "bar":       n - 1,
            "ts":        str(ts[-1]),
            "side":      "long" if position > 0 else "short",
            "entry":     round(entry_price, 2),
            "exit":      round(exit_price, 2),
            "pnl":       round(pnl, 2),
            "fee":       round(total_fee, 2),
            "balance":   round(balance, 2),
        })
        equity_curve[-1] = balance
        position = 0.0

    # ── Statistics ──
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    n_trades = len(trades)
    n_wins   = len(wins)
    n_losses = len(losses)
    wr       = n_wins / max(n_trades, 1) * 100

    gross_pnl  = sum(t["pnl"] + t["fee"] for t in trades)
    net_pnl    = balance - INITIAL
    net_ret    = net_pnl / INITIAL * 100
    gross_ret  = gross_pnl / INITIAL * 100
    fees_ratio = fees_paid / max(abs(gross_pnl), 1) * 100

    avg_win  = np.mean([t["pnl"] for t in wins])   if wins   else 0
    avg_loss = np.mean([t["pnl"] for t in losses]) if losses else 0
    pf       = (sum(t["pnl"] for t in wins) /
                max(abs(sum(t["pnl"] for t in losses)), 0.01)) if losses else 0

    eq = np.array(equity_curve, dtype=float)
    peak = eq[0]
    mdd  = 0.0
    for v in eq:
        if v > peak: peak = v
        dd = (peak - v) / peak if peak > 0 else 0
        if dd > mdd: mdd = dd

    returns = pd.Series(eq).pct_change().dropna().values
    sharpe = float(np.mean(returns) / (np.std(returns) + 1e-9) * np.sqrt(365 * 24))

    # Monthly returns
    dates = pd.to_datetime(ts)
    eq_s  = pd.Series(eq, index=dates)
    monthly = eq_s.resample("ME").last().ffill().pct_change().dropna() * 100

    return {
        "dates":      dates,
        "equity":     eq,
        "trades":     trades,
        "monthly":    monthly,
        "stats": {
            "net_ret":     round(net_ret, 2),
            "gross_ret":   round(gross_ret, 2),
            "fees_paid":   round(fees_paid, 2),
            "fees_pct":    round(fees_ratio, 1),
            "final":       round(balance, 2),
            "n_trades":    n_trades,
            "n_wins":      n_wins,
            "n_losses":    n_losses,
            "wr":          round(wr, 1),
            "sharpe":      round(sharpe, 2),
            "mdd":         round(mdd * 100, 2),
            "pf":          round(pf, 2),
            "avg_win":     round(float(avg_win), 2),
            "avg_loss":    round(float(avg_loss), 2),
            "leverage":    leverage,
            "fee_bps":     round(fee * 10000, 1),
        },
    }


async def main():
    print(f"Loading {SYMBOL} {TF}  1 year: {START} → {END}")
    candles = await ensure_candles(SYMBOL, TF, start_date=START, end_date=END,
                                    force_refresh=False, max_candles=300000)
    print(f"Candles loaded: {len(candles)}")
    print(f"Period: {pd.to_datetime(int(candles[0][0]), unit='ms')} → "
          f"{pd.to_datetime(int(candles[-1][0]), unit='ms')}\n")

    results = {}
    for sid, overrides in STRATEGIES.items():
        code = get_strategy_code(sid)
        if not code:
            print(f"SKIP {sid}")
            continue
        r = backtest(sid, code, candles, overrides)
        results[sid] = r

        s = r["stats"]
        lv = s["leverage"]
        print(f"{'='*70}")
        print(f"  {sid}  (leverage {lv}x, fee {s['fee_bps']} bps)")
        print(f"{'='*70}")
        print(f"  Net return:   {s['net_ret']:+.2f}%")
        print(f"  Gross return: {s['gross_ret']:+.2f}%")
        print(f"  Fees paid:    ${s['fees_paid']:,.2f}  ({s['fees_pct']:.0f}% of gross)")
        print(f"  Final capital: ${s['final']:,.0f}")
        print(f"  Trades:       {s['n_trades']}  ({s['n_wins']}W / {s['n_losses']}L)")
        print(f"  Win rate:     {s['wr']}%")
        print(f"  Sharpe:       {s['sharpe']}")
        print(f"  Max drawdown: {s['mdd']}%")
        print(f"  Profit factor: {s['pf']}")
        print(f"  Avg win:  ${s['avg_win']:.2f}   Avg loss: ${s['avg_loss']:.2f}")
        print()

    # ── Monthly breakdown table ──
    print(f"\n{'='*70}")
    print(f"  MONTHLY BREAKDOWN (% return)")
    print(f"{'='*70}")
    all_months = sorted(set().union(*(r["monthly"].index for r in results.values())))
    header = f"{'Month':>10}" + "".join(f"  {sid:>18}" for sid in results)
    print(header)
    print("-" * len(header))
    for m in all_months:
        row = f"{m.strftime('%Y-%m'):>10}"
        for sid, r in results.items():
            val = r["monthly"].get(m, float("nan"))
            row += f"  {val:>+17.2f}%" if not np.isnan(val) else f"  {'—':>18}"
        print(row)
    print()

    # ── Chart ──
    fig, axes = plt.subplots(2, 3, figsize=(22, 10),
                             gridspec_kw={"height_ratios": [1, 2.5], "wspace": 0.3, "hspace": 0.35})

    colors = {"trend_bounce_levx": "#d62728",
              "trend_momentum_pro": "#ff7f0e",
              "momentum_atr_trail": "#2ca02c"}
    names  = {"trend_bounce_levx": "Trend Bounce LevX",
              "trend_momentum_pro": "Trend Momentum Pro",
              "momentum_atr_trail": "Momentum ATR Trail"}

    for idx, (sid, r) in enumerate(results.items()):
        s = r["stats"]
        c = colors[sid]
        dates = r["dates"]
        eq = r["equity"]

        # Monthly bars
        ax_m = axes[0][idx]
        monthly = r["monthly"]
        colors_m = ["#2ca02c" if v >= 0 else "#d62728" for v in monthly.values]
        ax_m.bar(range(len(monthly)), monthly.values, color=colors_m, width=0.7)
        ax_m.axhline(0, color="gray", linewidth=0.5)
        ax_m.set_xticks(range(len(monthly)))
        ax_m.set_xticklabels([d.strftime("%b") for d in monthly.index], fontsize=7, rotation=45)
        ax_m.set_ylabel("Return %")
        ax_m.set_title(f"{names[sid]}  —  Monthly", fontsize=10)
        ax_m.grid(True, alpha=0.3, axis="y")

        # Equity curve
        ax_e = axes[1][idx]
        ax_e.plot(dates, eq, color=c, linewidth=0.8)
        ax_e.axhline(INITIAL, color="gray", linestyle="--", alpha=0.4, linewidth=0.5)
        ax_e.fill_between(dates, eq, INITIAL, where=eq >= INITIAL, color=c, alpha=0.08)
        ax_e.fill_between(dates, eq, INITIAL, where=eq <  INITIAL, color="#d62728", alpha=0.06)

        if r["trades"]:
            trade_dates_np = np.array([np.datetime64(t["ts"]) for t in r["trades"]], dtype="datetime64[ns]")
            dates_np = np.array(dates.values, dtype="datetime64[ns]")
            trade_eqs   = np.array([eq[np.abs(dates_np - td).argmin()] for td in trade_dates_np])
            trade_pnls  = np.array([t["pnl"] for t in r["trades"]])
            win_mask  = trade_pnls > 0
            loss_mask = ~win_mask
            ax_e.scatter(trade_dates_np[win_mask],  trade_eqs[win_mask],  c="#2ca02c", marker=".", s=6, alpha=0.3, zorder=5)
            ax_e.scatter(trade_dates_np[loss_mask], trade_eqs[loss_mask], c="#d62728", marker=".", s=6, alpha=0.3, zorder=5)

        label = (f"{names[sid]}  |  Net {s['net_ret']:+.1f}%  "
                 f"WR {s['wr']}%  Sharpe {s['sharpe']}  "
                 f"DD {s['mdd']}%  PF {s['pf']}  "
                 f"{s['n_trades']}t  {s['leverage']}x")
        ax_e.set_title(label, fontsize=9)
        ax_e.set_ylabel("Equity ($)")
        ax_e.grid(True, alpha=0.3)

    fig.suptitle(f"Running Strategies — 1 Year {TF}  {SYMBOL}  "
                 f"(${INITIAL:.0f} start, fee 0.05%)",
                 fontsize=13, fontweight="bold")
    plt.tight_layout(rect=[0, 0, 1, 0.97])

    out_dir = Path(__file__).parent.parent / "backtests_data"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "running_strategies_1yr_5m_final.png"
    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"Chart: {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
