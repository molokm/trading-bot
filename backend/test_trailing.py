"""
Test trailing stop variants on TrendJoin 4H.
Compare: fixed TP/SL vs trailing stop (various callback levels).
"""
import numpy as np
import pandas as pd
import json
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
    adx_val = dx.ewm(alpha=1/n, adjust=False).mean()
    return adx_val, plus_di, minus_di


def load_data():
    path = CANDLES_DIR / "BTCUSDT_4H.csv"
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["ts"])
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        df[c] = df[c].astype(float)
    df["EMA_20"] = ema(df["Close"], 20)
    df["EMA_50"] = ema(df["Close"], 50)
    df["RSI_14"] = rsi(df["Close"], 14)
    df["ATR_14"] = calc_atr(df, 14)
    adx_val, _, _ = adx(df, 14)
    df["ADX_14"] = adx_val
    return df


def backtest_trailing(df, sl_mult=2.0, tp_mult=4.5, trail_pct=None, trail_atr=None,
                      initial_capital=10000, leverage=3, fee=0.0005, regime_filter=True,
                      lock_at_r=None):
    """
    trail_pct: trailing stop as % of highest price since entry (e.g. 0.05 = 5%)
    trail_atr: trailing stop as ATR multiplier from highest price
    lock_at_r: lock profit at this R level (e.g. 1.0 = once +1R, trail from there)
    """
    cap = initial_capital
    pos = None  # entry, sl, size, peak_price, atr_at_entry
    trades = []
    peak = cap
    max_dd = 0
    equity = []

    ef = df["EMA_20"].values
    es = df["EMA_50"].values
    rv = df["RSI_14"].values
    av = df["ADX_14"].values
    at = df["ATR_14"].values
    cl = df["Close"].values
    hi = df["High"].values
    lo = df["Low"].values

    for i in range(1, len(df)):
        if pos:
            entry, sl, size, peak_px, atr_ref = pos
            r_val = entry - sl

            # Update trailing stop
            if hi[i] > peak_px:
                peak_px = hi[i]

            if trail_pct is not None:
                new_sl = peak_px * (1 - trail_pct)
            elif trail_atr is not None:
                new_sl = peak_px - trail_atr * atr_ref
            else:
                new_sl = sl

            # Lock profit at R level
            if lock_at_r is not None and r_val > 0:
                current_r = (peak_px - entry) / r_val
                if current_r >= lock_at_r:
                    lock_sl = entry + lock_at_r * r_val * 0.5  # Lock half the profit
                    new_sl = max(new_sl, lock_sl)

            # Only move SL up, never down (for long)
            sl = max(sl, new_sl)

            hit_sl = lo[i] <= sl
            hit_tp = not (trail_pct or trail_atr) and hi[i] >= entry + tp_mult * atr_ref

            if hit_sl or hit_tp:
                px = sl if hit_sl else (entry + tp_mult * atr_ref)
                pnl = (px - entry) * size * leverage
                fee_cost = px * size * leverage * fee
                cap += pnl - fee_cost
                r_mult = (px - entry) / r_val if r_val > 0 else 0
                reason = "SL" if hit_sl else "TP"
                trades.append({"pnl": pnl - fee_cost, "r": r_mult, "reason": reason})
                pos = None
            else:
                pos = (entry, sl, size, peak_px, atr_ref)

        if pos is None and cap > 0:
            e2, e5, r, a, atr_v = ef[i-1], es[i-1], rv[i-1], av[i-1], at[i-1]
            if np.isnan(e2) or np.isnan(e5) or np.isnan(a) or np.isnan(atr_v) or atr_v <= 0:
                equity.append(cap)
                continue

            if regime_filter:
                regime = "bull" if e2 > e5 and a > 20 and not np.isnan(r) and r > 50 else "other"
            else:
                regime = "bull"

            dist = (cl[i-1] - e2) / e2 * 100 if e2 > 0 else 0
            passed = (e2 > e5 and -3.0 < dist < 2.5 and (not np.isnan(r) and r > 30) and a > 18)

            if passed and regime == "bull":
                entry = cl[i]
                sl_init = entry - sl_mult * atr_v
                r_val = entry - sl_init
                sz = (cap * 0.02) / r_val if r_val > 0 else 0
                if sz > 0:
                    fee_cost = entry * sz * leverage * fee
                    cap -= fee_cost
                    pos = (entry, sl_init, sz, entry, atr_v)

        peak = max(peak, cap)
        dd = (peak - cap) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)
        equity.append(cap)

    # Close open position
    if pos:
        entry, sl, size, peak_px, atr_ref = pos
        px = cl[-1]
        pnl = (px - entry) * size * leverage
        fee_cost = px * size * leverage * fee
        cap += pnl - fee_cost
        r_val = entry - sl
        r_mult = (px - entry) / r_val if r_val > 0 else 0
        trades.append({"pnl": pnl - fee_cost, "r": r_mult, "reason": "EOD"})

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total_win = sum(t["pnl"] for t in wins)
    total_loss = sum(t["pnl"] for t in losses)
    pf = abs(total_win / total_loss) if total_loss != 0 else float("inf")

    years = len(df) * 4 / (365.25 * 24)
    cagr = ((cap / initial_capital) ** (1 / max(years, 0.1)) - 1) * 100

    return {
        "trades": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate": len(wins) / len(trades) * 100 if trades else 0,
        "final": cap, "return_pct": (cap - initial_capital) / initial_capital * 100,
        "cagr": cagr, "max_dd": max_dd, "pf": pf,
        "avg_r": np.mean([t["r"] for t in trades]) if trades else 0,
    }


def walk_forward_test(df, bt_func, n_splits=3):
    total = len(df)
    window = total // (n_splits + 1)
    results = []
    for i in range(n_splits):
        test_start = (i + 1) * window
        test_end = min(test_start + window, total)
        if test_end <= test_start:
            break
        test_df = df.iloc[:test_end].copy().reset_index(drop=True)
        r = bt_func(test_df)
        start_date = df.iloc[test_start]["ts"].strftime("%Y-%m-%d")
        end_date = df.iloc[min(test_end-1, len(df)-1)]["ts"].strftime("%Y-%m-%d")
        results.append({
            "window": f"{start_date} → {end_date}",
            "trades": r["trades"], "return": r["return_pct"],
            "win_rate": r["win_rate"], "max_dd": r["max_dd"], "pf": r["pf"],
        })
        tag = "+" if r["return_pct"] > 0 else ""
        print(f"    WF {i+1}: {start_date}→{end_date} | {r['trades']:>3d} trades | "
              f"{tag}{r['return_pct']:>7.1f}% | WR {r['win_rate']:>4.0f}% | "
              f"MaxDD {r['max_dd']:>5.1f}% | PF {r['pf']:.2f}")
    profitable = sum(1 for r in results if r["return"] > 0)
    return results, profitable


if __name__ == "__main__":
    df = load_data()
    print(f"Data: {len(df)} 4H candles ({df['ts'].iloc[0]} to {df['ts'].iloc[-1]})")

    # ── Define variants ──
    variants = [
        ("Fixed TP/SL (baseline)", {"sl_mult": 2.0, "tp_mult": 4.5}),
        ("Fixed TP/SL 1.8/4.5", {"sl_mult": 1.8, "tp_mult": 4.5}),
        ("Trail 3% from peak", {"sl_mult": 2.0, "tp_mult": 999, "trail_pct": 0.03}),
        ("Trail 5% from peak", {"sl_mult": 2.0, "tp_mult": 999, "trail_pct": 0.05}),
        ("Trail 2.0 ATR", {"sl_mult": 2.0, "tp_mult": 999, "trail_atr": 2.0}),
        ("Trail 2.5 ATR", {"sl_mult": 2.0, "tp_mult": 999, "trail_atr": 2.5}),
        ("Trail 3.0 ATR", {"sl_mult": 2.0, "tp_mult": 999, "trail_atr": 3.0}),
        ("Lock at 1R, trail 2.0 ATR", {"sl_mult": 2.0, "tp_mult": 999, "trail_atr": 2.0, "lock_at_r": 1.0}),
        ("Lock at 1.5R, trail 2.0 ATR", {"sl_mult": 2.0, "tp_mult": 999, "trail_atr": 2.0, "lock_at_r": 1.5}),
        ("Lock at 2R, trail 2.5 ATR", {"sl_mult": 2.0, "tp_mult": 999, "trail_atr": 2.5, "lock_at_r": 2.0}),
        ("Trail 5% + lock 1R", {"sl_mult": 2.0, "tp_mult": 999, "trail_pct": 0.05, "lock_at_r": 1.0}),
        ("Trail 3% + lock 1.5R", {"sl_mult": 2.0, "tp_mult": 999, "trail_pct": 0.03, "lock_at_r": 1.5}),
    ]

    print(f"\n{'='*75}")
    print("  TRAILING STOP TEST: TrendJoin 4H (regime filter ON, 3x leverage)")
    print(f"{'='*75}")
    print(f"\n  {'Variant':<30} {'CAGR':>8} {'MaxDD':>8} {'PF':>6} {'Trades':>7} {'WR':>6} {'AvgR':>6}")
    print(f"  {'─'*70}")

    all_results = []
    for name, params in variants:
        r = backtest_trailing(df, **params, leverage=3)
        r["name"] = name
        all_results.append(r)
        tag = "+" if r["return_pct"] > 0 else ""
        print(f"  {name:<30} {r['cagr']:>+7.1f}% {r['max_dd']:>6.1f}% "
              f"{r['pf']:>6.2f} {r['trades']:>7} {r['win_rate']:>5.0f}% {r['avg_r']:>+.3f}")

    # ── Walk-forward on top 3 ──
    sorted_by_cagr = sorted(all_results, key=lambda x: x["cagr"], reverse=True)
    top3 = sorted_by_cagr[:3]

    print(f"\n{'='*75}")
    print("  WALK-FORWARD VALIDATION (top 3 by CAGR)")
    print(f"{'='*75}")

    for r in top3:
        name = r["name"]
        params = [v for n, v in variants if n == name][0]
        print(f"\n  {name}:")
        wf_results, wf_p = walk_forward_test(df, lambda d: backtest_trailing(d, **params))
        print(f"    WF Score: {wf_p}/{len(wf_results)} windows profitable")

    # ── Best comparison ──
    best_trail = max([r for r in all_results if r["trades"] >= 50], key=lambda x: x["cagr"])
    baseline = [r for r in all_results if r["name"] == "Fixed TP/SL (baseline)"][0]

    print(f"\n{'='*75}")
    print(f"  BEST TRAILING vs BASELINE")
    print(f"{'='*75}")
    print(f"  {'':30} {'CAGR':>8} {'MaxDD':>8} {'PF':>6} {'Trades':>7}")
    print(f"  {'─'*60}")
    print(f"  {'Fixed TP/SL (baseline)':<30} {baseline['cagr']:>+7.1f}% {baseline['max_dd']:>6.1f}% {baseline['pf']:>6.2f} {baseline['trades']:>7}")
    print(f"  {best_trail['name']:<30} {best_trail['cagr']:>+7.1f}% {best_trail['max_dd']:>6.1f}% {best_trail['pf']:>6.2f} {best_trail['trades']:>7}")
    print(f"{'='*75}")
