"""
RSI Bounce Scalper on 15m candles — more signals, more trades.
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

def load_15m():
    path = CANDLES_DIR / "BTCUSDT_15m.csv"
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["ts"])
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        df[c] = df[c].astype(float)
    return df

def load_1h():
    path = CANDLES_DIR / "BTCUSDT_1H.csv"
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["ts"])
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        df[c] = df[c].astype(float)
    return df


def scalp_15m(df, tp_pct, sl_pct, ema_fast=8, ema_slow=21, rsi_period=14,
              leverage=3, fee=0.0005, cooldown=4, max_bars=0):
    """RSI bounce scalper on 15m."""
    df = df.copy()
    df["EMA_F"] = ema(df["Close"], ema_fast)
    df["EMA_S"] = ema(df["Close"], ema_slow)
    df["RSI"] = rsi(df["Close"], rsi_period)

    ef = df["EMA_F"].values
    es = df["EMA_S"].values
    rv = df["RSI"].values
    cl = df["Close"].values
    hi = df["High"].values
    lo = df["Low"].values

    cap = 10000
    pos = None
    trades = []
    last_exit = -cooldown - 1
    peak = cap
    max_dd = 0

    for i in range(2, len(df)):
        # Exit
        if pos:
            entry, sl, tp, size, bars, direction = pos
            bars += 1
            if direction == 1:
                hit_sl = lo[i] <= sl
                hit_tp = hi[i] >= tp
            else:
                hit_sl = hi[i] >= sl
                hit_tp = lo[i] <= tp
            timeout = max_bars > 0 and bars >= max_bars

            if hit_sl or hit_tp or timeout:
                px = sl if hit_sl else (tp if hit_tp else cl[i])
                if direction == 1:
                    pnl = (px - entry) * size * leverage
                else:
                    pnl = (entry - px) * size * leverage
                fee_cost = px * abs(size) * leverage * fee
                cap += pnl - fee_cost
                trades.append({"pnl": pnl - fee_cost, "reason": "SL" if hit_sl else ("TP" if hit_tp else "TIME")})
                last_exit = i
                pos = None

        # Entry
        if pos is None and cap > 0 and (i - last_exit) >= cooldown:
            r = rv[i-1]
            e_f, e_s = ef[i-1], es[i-1]
            if np.isnan(r) or np.isnan(e_f) or np.isnan(e_s):
                continue

            direction = 0

            # RSI bounce long: RSI was oversold, now bouncing, EMA aligned
            r_prev = rv[i-2] if not np.isnan(rv[i-2]) else 50
            if r_prev < 32 and r > r_prev and e_f > e_s * 0.998:
                direction = 1
            # RSI bounce short
            elif r_prev > 68 and r < r_prev and e_f < e_s * 1.002:
                direction = -1

            if direction != 0:
                entry = cl[i]
                tp_dist = entry * tp_pct / 100
                sl_dist = entry * sl_pct / 100
                if direction == 1:
                    tp_price = entry + tp_dist
                    sl_price = entry - sl_dist
                else:
                    tp_price = entry - tp_dist
                    sl_price = entry + sl_dist

                risk_amt = cap * 0.02
                size = risk_amt / sl_dist if sl_dist > 0 else 0
                if size > 0:
                    fee_cost = entry * size * leverage * fee
                    cap -= fee_cost
                    pos = (entry, sl_price, tp_price, size, 0, direction)

        peak = max(peak, cap)
        dd = (peak - cap) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)

    if pos:
        entry, sl, tp, size, bars, direction = pos
        px = cl[-1]
        if direction == 1:
            pnl = (px - entry) * size * leverage
        else:
            pnl = (entry - px) * size * leverage
        cap += pnl - px * abs(size) * leverage * fee
        trades.append({"pnl": pnl, "reason": "EOD"})

    wins = sum(1 for t in trades if t["pnl"] > 0)
    losses = len(trades) - wins
    tw = sum(t["pnl"] for t in trades if t["pnl"] > 0)
    tl = sum(t["pnl"] for t in trades if t["pnl"] <= 0)
    pf = abs(tw / tl) if tl != 0 else float("inf")
    years = len(df) * 15 / (365.25 * 24 * 60)
    cagr = ((cap / 10000) ** (1 / max(years, 0.1)) - 1) * 100

    return {
        "trades": len(trades), "wins": wins, "losses": losses,
        "win_rate": wins / len(trades) * 100 if trades else 0,
        "final": cap, "return_pct": (cap - 10000) / 10000 * 100,
        "cagr": cagr, "max_dd": max_dd, "pf": pf,
    }


def walk_forward_15m(df, tp_pct, sl_pct, leverage=3, n_splits=3):
    total = len(df)
    window = total // (n_splits + 1)
    results = []
    for i in range(n_splits):
        test_start = (i + 1) * window
        test_end = min(test_start + window, total)
        if test_end <= test_start:
            break
        test_df = df.iloc[:test_end].copy().reset_index(drop=True)
        r = scalp_15m(test_df, tp_pct=tp_pct, sl_pct=sl_pct, leverage=leverage)
        s = df.iloc[test_start]["ts"].strftime("%Y-%m-%d")
        e = df.iloc[min(test_end-1, len(df)-1)]["ts"].strftime("%Y-%m-%d")
        tag = "+" if r["return_pct"] > 0 else ""
        print(f"    WF {i+1}: {s} -> {e} | {r['trades']:>4d} trades | "
              f"{tag}{r['return_pct']:>7.1f}% | WR {r['win_rate']:>4.0f}% | PF {r['pf']:.2f}")
        results.append(r["return_pct"])
    return sum(1 for x in results if x > 0), len(results)


if __name__ == "__main__":
    df15 = load_15m()
    print("=" * 80)
    print("  RSI BOUNCE SCALPER: 15m BTC")
    print(f"  Data: {len(df15)} candles ({df15['ts'].iloc[0]} to {df15['ts'].iloc[-1]})")
    print("=" * 80)

    # Grid search on 15m
    print(f"\n  {'TP%':>5} {'SL%':>5} {'Lev':>4} {'CAGR':>8} {'MaxDD':>7} {'PF':>6} {'Trades':>7} {'WR':>5}")
    print(f"  {'─'*50}")

    best_r = None
    for tp in [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0]:
        for sl_mult in [0.8, 1.0, 1.2, 1.5]:
            sl = tp * sl_mult
            for lev in [1, 3]:
                r = scalp_15m(df15, tp_pct=tp, sl_pct=sl, leverage=lev)
                if r["trades"] >= 20:
                    print(f"  {tp:>5.1f} {sl:>5.1f} {lev:>3d}x {r['cagr']:>+7.1f}% {r['max_dd']:>5.1f}% "
                          f"{r['pf']:>6.2f} {r['trades']:>7} {r['win_rate']:>4.0f}%")
                    if best_r is None or r["cagr"] > best_r.get("cagr", -999):
                        if r["max_dd"] < 60:
                            best_r = r.copy()
                            best_r["tp"] = tp
                            best_r["sl"] = sl
                            best_r["lev"] = lev

    # Walk-forward on best
    if best_r:
        print(f"\n  Best: TP {best_r['tp']}% SL {best_r['sl']}% {best_r['lev']}x")
        print(f"  Walk-forward:")
        wf_p, wf_n = walk_forward_15m(df15, best_r["tp"], best_r["sl"], best_r["lev"])
        print(f"  WF Score: {wf_p}/{wf_n}")

        # Also test more conservative
        print(f"\n  WF: TP 0.5% SL 0.5% 3x:")
        wp2, wn2 = walk_forward_15m(df15, 0.5, 0.5, 3)
        print(f"  WF Score: {wp2}/{wn2}")

        print(f"\n  WF: TP 0.7% SL 0.7% 3x:")
        wp3, wn3 = walk_forward_15m(df15, 0.7, 0.7, 3)
        print(f"  WF Score: {wp3}/{wn3}")

    print(f"\n  Comparison:")
    print(f"    TrendJoin 4H 3x:        CAGR +15.6% MaxDD 68.8%")
    print(f"    RSI Bounce 1H 3x:       CAGR +8.4%  MaxDD 15.4%")
    if best_r:
        print(f"    RSI Bounce 15m {best_r['lev']}x:      CAGR {best_r['cagr']:+.1f}% MaxDD {best_r['max_dd']:.1f}%")
