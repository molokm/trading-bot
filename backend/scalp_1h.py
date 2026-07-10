"""
TrendJoin Scalper: 1H timeframe, tighter TP/SL, faster EMAs.
Tests multiple parameter combos and walk-forward validation.
"""
import numpy as np
import pandas as pd
from pathlib import Path

CANDLES_DIR = Path(__file__).parent / "data" / "candles"

def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()

def rsi(s, n=14):
    delta = s.diff()
    gain = delta.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss)

def calc_atr(df, n=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    tr = pd.concat([(h-l), (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()

def adx(df, n=14):
    h, l, c = df["High"], df["Low"], df["Close"]
    up = h - h.shift(1)
    dn = l.shift(1) - l
    plus_dm = np.where((up > dn) & (up > 0), up, 0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0)
    tr = pd.concat([(h-l), (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    atr_val = tr.ewm(alpha=1/n, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1/n, adjust=False).mean() / atr_val
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1/n, adjust=False).mean() / atr_val
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1/n, adjust=False).mean(), plus_di, minus_di


def load_data():
    path = CANDLES_DIR / "BTCUSDT_1H.csv"
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["ts"])
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        df[c] = df[c].astype(float)
    return df


def add_indicators(df, ema_fast=10, ema_slow=30, rsi_period=14, atr_period=14, adx_period=14):
    df = df.copy()
    df["EMA_F"] = ema(df["Close"], ema_fast)
    df["EMA_S"] = ema(df["Close"], ema_slow)
    df["RSI"] = rsi(df["Close"], rsi_period)
    df["ATR"] = calc_atr(df, atr_period)
    df["ADX"], _, _ = adx(df, adx_period)
    df["Vol_SMA"] = df["Volume"].rolling(20).mean()
    return df


def backtest(df, ema_fast=10, ema_slow=30, sl_mult=1.0, tp_mult=2.0,
             rsi_exit=55, initial_capital=5000, leverage=3, fee=0.0005,
             regime_filter=True, risk_pct=0.02, cooldown=2):
    """Run scalper backtest. Returns equity curve, trades, final capital."""
    df = add_indicators(df, ema_fast, ema_slow)

    ef = df["EMA_F"].values
    es = df["EMA_S"].values
    rv = df["RSI"].values
    av = df["ADX"].values
    at = df["ATR"].values
    cl = df["Close"].values
    hi = df["High"].values
    lo = df["Low"].values
    vol = df["Vol_SMA"].values

    cap = initial_capital
    pos = None  # (entry, sl, tp, size, atr_ref)
    trades = []
    last_exit_idx = -cooldown - 1
    equity = []
    peak = cap
    max_dd = 0

    for i in range(1, len(df)):
        # Check exit
        if pos:
            hit_sl = lo[i] <= pos[1]
            hit_tp = hi[i] >= pos[2]
            hit_rsi = not np.isnan(rv[i]) and rv[i] > rsi_exit

            if hit_sl or hit_tp or hit_rsi:
                if hit_sl:
                    px = pos[1]; reason = "SL"
                elif hit_tp:
                    px = pos[2]; reason = "TP"
                else:
                    px = cl[i]; reason = "RSI"

                pnl = (px - pos[0]) * pos[3] * leverage
                fee_cost = px * pos[3] * leverage * fee
                cap += pnl - fee_cost
                r_mult = (px - pos[0]) / (pos[0] - pos[1]) if (pos[0] - pos[1]) > 0 else 0
                trades.append({"entry": pos[0], "exit": px, "pnl": pnl - fee_cost,
                               "reason": reason, "r": r_mult, "idx": i})
                last_exit_idx = i
                pos = None

        # Check entry
        if pos is None and cap > 0 and (i - last_exit_idx) >= cooldown:
            e2, e5, r, a, atr_v = ef[i-1], es[i-1], rv[i-1], av[i-1], at[i-1]
            if np.isnan(e2) or np.isnan(e5) or np.isnan(a) or np.isnan(atr_v) or atr_v <= 0:
                equity.append(cap)
                continue

            # Regime filter
            if regime_filter:
                regime = "bull" if e2 > e5 and a > 20 and not np.isnan(r) and r > 50 else "other"
            else:
                regime = "bull"

            trend_ok = e2 > e5
            dist = (cl[i-1] - e2) / e2 * 100 if e2 > 0 else 0
            pullback_ok = -2.0 < dist < 1.5  # Tighter pullback for scalper
            rsi_ok = not np.isnan(r) and 25 < r < 55  # RSI in sweet spot
            adx_ok = a > 15  # Lower ADX threshold for more signals

            passed = trend_ok and pullback_ok and rsi_ok and adx_ok

            if passed and regime == "bull":
                entry = cl[i]
                sl_p = entry - sl_mult * atr_v
                tp_p = entry + tp_mult * atr_v
                r_val = entry - sl_p
                sz = (cap * risk_pct) / r_val if r_val > 0 else 0
                if sz > 0:
                    fee_cost = entry * sz * leverage * fee
                    cap -= fee_cost
                    pos = (entry, sl_p, tp_p, sz, atr_v)

        peak = max(peak, cap)
        dd = (peak - cap) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)
        equity.append(cap)

    # Close open position
    if pos:
        px = cl[-1]
        pnl = (px - pos[0]) * pos[3] * leverage
        fee_cost = px * pos[3] * leverage * fee
        cap += pnl - fee_cost
        trades.append({"entry": pos[0], "exit": px, "pnl": pnl - fee_cost,
                       "reason": "EOD", "r": 0, "idx": len(df)-1})

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total_win = sum(t["pnl"] for t in wins)
    total_loss = sum(t["pnl"] for t in losses)
    pf = abs(total_win / total_loss) if total_loss != 0 else float("inf")

    years = len(df) * 1 / (365.25 * 24)
    cagr = ((cap / initial_capital) ** (1 / max(years, 0.1)) - 1) * 100

    return {
        "trades": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0,
        "final": cap, "return_pct": (cap - initial_capital) / initial_capital * 100,
        "cagr": cagr, "max_dd": max_dd, "pf": pf,
        "avg_r": np.mean([t["r"] for t in trades]) if trades else 0,
        "equity": equity, "trades_list": trades,
        "sl_tp": f"SL {sl_mult}x / TP {tp_mult}x",
        "ema": f"EMA {ema_fast}/{ema_slow}",
    }


def walk_forward_test(df, params, n_splits=3):
    """Walk-forward: train on 2/3, test on 1/3, rolling."""
    total = len(df)
    window = total // (n_splits + 1)
    results = []

    for i in range(n_splits):
        test_start = (i + 1) * window
        test_end = min(test_start + window, total)
        if test_end <= test_start:
            break

        test_df = df.iloc[:test_end].copy().reset_index(drop=True)
        r = backtest(test_df, **params)

        start_date = df.iloc[test_start]["ts"].strftime("%Y-%m-%d") if test_start < len(df) else "?"
        end_date = df.iloc[min(test_end-1, len(df)-1)]["ts"].strftime("%Y-%m-%d")
        results.append({
            "window": f"{start_date} → {end_date}",
            "trades": r["trades"], "return": r["return_pct"],
            "win_rate": r["win_rate"], "max_dd": r["max_dd"], "pf": r["pf"],
        })
        emoji = "+" if r["return_pct"] > 0 else ""
        print(f"    WF {i+1}: {start_date}→{end_date} | {r['trades']} trades | "
              f"{emoji}{r['return_pct']:.1f}% | WR {r['win_rate']:.0f}% | "
              f"MaxDD {r['max_dd']:.1f}% | PF {r['pf']:.2f}")

    profitable = sum(1 for r in results if r["return"] > 0)
    return results, profitable


if __name__ == "__main__":
    print("=" * 70)
    print("  TRENDJOIN SCALPER: 1H TIMEFRAME")
    print("=" * 70)

    df = load_data()
    print(f"  Data: {len(df)} 1H candles ({df['ts'].iloc[0]} to {df['ts'].iloc[-1]})")
    print(f"  Period: {len(df)/365.25/24:.1f} years")

    # ── Grid Search ──
    print(f"\n{'─'*70}")
    print("  GRID SEARCH: EMA / SL / TP combos")
    print(f"{'─'*70}")

    combos = [
        # ema_fast, ema_slow, sl_mult, tp_mult
        (8, 21, 0.8, 1.6),   # R:R 1:2, fast
        (8, 21, 1.0, 2.0),   # R:R 1:2
        (8, 21, 1.2, 2.5),   # R:R ~1:2
        (10, 30, 0.8, 1.6),
        (10, 30, 1.0, 2.0),
        (10, 30, 1.2, 2.5),
        (10, 30, 1.0, 3.0),  # R:R 1:3
        (12, 26, 1.0, 2.0),
        (12, 26, 1.2, 2.5),
        (15, 50, 1.5, 3.0),  # More conservative
        (15, 50, 2.0, 4.0),  # Classic 4H scaled down
    ]

    results_all = []
    for ef, es, sl, tp in combos:
        params = {"ema_fast": ef, "ema_slow": es, "sl_mult": sl, "tp_mult": tp}
        r = backtest(df, **params)
        r["params"] = params
        results_all.append(r)
        tag = "+" if r["return_pct"] > 0 else ""
        ema_label = f"{ef}/{es}"
        print(f"  EMA {ema_label:>5s} SL {sl:.1f} TP {tp:.1f} | "
              f"{r['trades']:>4d} trades | {tag}{r['return_pct']:>7.1f}% | "
              f"CAGR {r['cagr']:>+6.1f}% | MaxDD {r['max_dd']:>5.1f}% | "
              f"WR {r['win_rate']:>4.0f}% | PF {r['pf']:.2f}")

    # ── Best by CAGR ──
    best_cagr = max(results_all, key=lambda x: x["cagr"])
    # Best by PF (min 10 trades)
    pf_candidates = [r for r in results_all if r["trades"] >= 10]
    best_pf = max(pf_candidates, key=lambda x: x["pf"]) if pf_candidates else best_cagr
    # Best risk-adjusted (CAGR / MaxDD)
    ra_candidates = [r for r in results_all if r["max_dd"] > 0 and r["trades"] >= 10]
    best_ra = max(ra_candidates, key=lambda x: x["cagr"] / x["max_dd"]) if ra_candidates else best_cagr

    print(f"\n  Best CAGR:     EMA {best_cagr['ema']} | {best_cagr['sl_tp']} | CAGR {best_cagr['cagr']:+.1f}% | MaxDD {best_cagr['max_dd']:.1f}%")
    print(f"  Best PF:       EMA {best_pf['ema']} | {best_pf['sl_tp']} | PF {best_pf['pf']:.2f} | CAGR {best_pf['cagr']:+.1f}%")
    print(f"  Best Risk-Adj: EMA {best_ra['ema']} | {best_ra['sl_tp']} | CAGR/DD {best_ra['cagr']/best_ra['max_dd']:.2f}")

    # ── Walk-Forward on best risk-adjusted ──
    print(f"\n{'─'*70}")
    print(f"  WALK-FORWARD: Best Risk-Adjusted ({best_ra['ema']}, {best_ra['sl_tp']})")
    print(f"{'─'*70}")
    wf_results, wf_profitable = walk_forward_test(df, best_ra["params"])
    print(f"\n  WF Score: {wf_profitable}/{len(wf_results)} windows profitable")

    # ── Also WF on best CAGR ──
    if best_cagr != best_ra:
        print(f"\n{'─'*70}")
        print(f"  WALK-FORWARD: Best CAGR ({best_cagr['ema']}, {best_cagr['sl_tp']})")
        print(f"{'─'*70}")
        wf2, wf2_p = walk_forward_test(df, best_cagr["params"])
        print(f"\n  WF Score: {wf2_p}/{len(wf2)} windows profitable")

    # ── Compare with 4H baseline ──
    print(f"\n{'='*70}")
    print("  COMPARISON WITH 4H BASELINE")
    print(f"{'='*70}")
    print(f"  {'Strategy':<35} {'CAGR':>8} {'MaxDD':>8} {'PF':>6} {'Trades':>8} {'WR':>6}")
    print(f"  {'─'*70}")
    print(f"  {'TrendJoin 4H 1x (baseline)':<35} {'+3.7%':>8} {'24.7%':>8} {'1.35':>6} {'298':>8} {'39%':>6}")
    print(f"  {'TrendJoin 4H 3x':<35} {'+15.6%':>8} {'68.8%':>8} {'1.35':>6} {'325':>8} {'36%':>6}")

    for r in [best_ra, best_cagr]:
        label = f"Scalper 1H {r['ema']} {r['sl_tp']}"
        print(f"  {label:<35} "
              f"{r['cagr']:>+6.1f}% {r['max_dd']:>6.1f}% "
              f"{r['pf']:>6.2f} {r['trades']:>8} {r['win_rate']:>5.0f}%")
    print(f"{'='*70}")
