"""
Fixed TP Scalper: minimal fixed take-profit, grid search over TP/SL/signal combos.
Tests 1H candles. Fee is critical at this scale.
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


def load_1h():
    path = CANDLES_DIR / "BTCUSDT_1H.csv"
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["ts"])
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        df[c] = df[c].astype(float)
    return df


def load_4h():
    path = CANDLES_DIR / "BTCUSDT_4H.csv"
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["ts"])
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        df[c] = df[c].astype(float)
    return df


def add_indicators(df, ema_fast=8, ema_slow=21, rsi_period=14, atr_period=14):
    df = df.copy()
    df["EMA_F"] = ema(df["Close"], ema_fast)
    df["EMA_S"] = ema(df["Close"], ema_slow)
    df["RSI"] = rsi(df["Close"], rsi_period)
    df["ATR"] = calc_atr(df, atr_period)
    df["ATR_PCT"] = df["ATR"] / df["Close"] * 100
    df["Vol_SMA"] = df["Volume"].rolling(20).mean()
    df["Vol_Ratio"] = df["Volume"] / df["Vol_SMA"].replace(0, np.nan)
    df["Close_Pct"] = df["Close"].pct_change() * 100
    # EMA cross signals
    df["EMA_Cross"] = (df["EMA_F"] > df["EMA_S"]).astype(int)
    df["EMA_Cross_Chg"] = df["EMA_Cross"].diff()
    # RSI zones
    df["RSI_Low"] = df["RSI"] < 40
    df["RSI_High"] = df["RSI"] > 60
    # Price momentum (last 3 candles)
    df["Mom3"] = df["Close"].pct_change(3) * 100
    return df


def scalp_backtest(df, tp_pct, sl_pct, signal_type="ema_cross_rsi",
                   ema_fast=8, ema_slow=21, rsi_period=14,
                   initial_capital=10000, leverage=3, fee=0.0005,
                   cooldown=1, max_bars=0):
    """
    Fixed TP/SL scalper.
    tp_pct, sl_pct: in % (e.g. 0.3 = 0.3%)
    signal_type:
      - "ema_cross": enter on EMA cross in trend direction
      - "ema_cross_rsi": EMA cross + RSI filter
      - "rsi_bounce": RSI bounce from oversold
      - "momentum": price momentum + EMA trend
      - "pullback": trend + pullback to EMA
    max_bars: close after N bars if no TP/SL (0 = no timeout)
    """
    df = add_indicators(df, ema_fast, ema_slow, rsi_period)

    ef = df["EMA_F"].values
    es = df["EMA_S"].values
    rv = df["RSI"].values
    at = df["ATR"].values
    at_pct = df["ATR_PCT"].values
    cl = df["Close"].values
    hi = df["High"].values
    lo = df["Low"].values
    vol = df["Vol_Ratio"].values
    mom3 = df["Mom3"].values
    ec = df["EMA_Cross_Chg"].values

    cap = initial_capital
    pos = None  # (entry, sl, tp, size, bar_count, direction)
    trades = []
    last_exit = -cooldown - 1
    peak = cap
    max_dd = 0

    for i in range(1, len(df)):
        # --- Exit check ---
        if pos:
            entry, sl, tp, size, bars, direction = pos
            bars += 1

            hit = False
            if direction == 1:  # Long
                hit_sl = lo[i] <= sl
                hit_tp = hi[i] >= tp
            else:  # Short
                hit_sl = hi[i] >= sl
                hit_tp = lo[i] <= tp

            timeout = max_bars > 0 and bars >= max_bars

            if hit_sl or hit_tp or timeout:
                if hit_sl:
                    px = sl; reason = "SL"
                elif hit_tp:
                    px = tp; reason = "TP"
                else:
                    px = cl[i]; reason = "TIME"

                if direction == 1:
                    pnl = (px - entry) * size * leverage
                else:
                    pnl = (entry - px) * size * leverage
                fee_cost = px * abs(size) * leverage * fee
                cap += pnl - fee_cost

                r_mult = abs(px - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0
                trades.append({"pnl": pnl - fee_cost, "r": r_mult * direction,
                               "reason": reason, "dir": direction})
                last_exit = i
                pos = None

        # --- Entry check ---
        if pos is None and cap > 0 and (i - last_exit) >= cooldown:
            r, e_f, e_s = rv[i-1], ef[i-1], es[i-1]
            atr_v = at_pct[i-1]
            v = vol[i-1]
            m = mom3[i-1] if not np.isnan(mom3[i-1]) else 0

            if np.isnan(e_f) or np.isnan(e_s) or np.isnan(r) or np.isnan(atr_v):
                continue

            direction = 0
            sl_dist_pct = sl_pct / leverage  # SL in price terms
            tp_dist_pct = tp_pct / leverage

            # --- Signal logic ---
            if signal_type == "ema_cross":
                # Long: EMA cross up
                if ec[i-1] == 1 and e_f > e_s:
                    direction = 1
                # Short: EMA cross down
                elif ec[i-1] == -1 and e_f < e_s:
                    direction = -1

            elif signal_type == "ema_cross_rsi":
                if ec[i-1] == 1 and e_f > e_s and r > 45 and r < 65:
                    direction = 1
                elif ec[i-1] == -1 and e_f < e_s and r < 55 and r > 35:
                    direction = -1

            elif signal_type == "rsi_bounce":
                # Long: RSI was <35, now bouncing (prev RSI < 35, current rising)
                r_prev = rv[i-2] if i >= 2 and not np.isnan(rv[i-2]) else 50
                if r_prev < 35 and r > r_prev and e_f > e_s * 0.998:
                    direction = 1
                # Short: RSI was >65, now falling
                elif r_prev > 65 and r < r_prev and e_f < e_s * 1.002:
                    direction = -1

            elif signal_type == "momentum":
                # Long: positive momentum + uptrend
                if m > 0.3 and e_f > e_s and r > 40 and r < 70:
                    direction = 1
                # Short: negative momentum + downtrend
                elif m < -0.3 and e_f < e_s and r < 60 and r > 30:
                    direction = -1

            elif signal_type == "pullback":
                # Long: uptrend + pullback to EMA + bounce
                dist = (cl[i-1] - e_f) / e_f * 100 if e_f > 0 else 0
                if e_f > e_s and -1.0 < dist < 0.3 and r > 35 and r < 55:
                    direction = 1
                # Short: downtrend + pull up to EMA
                elif e_f < e_s and -0.3 < dist < 1.0 and r < 65 and r > 45:
                    direction = -1

            elif signal_type == "mean_revert":
                # Long: oversold in uptrend
                if r < 32 and e_f > e_s * 0.995:
                    direction = 1
                # Short: overbought in downtrend
                elif r > 68 and e_f < e_s * 1.005:
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

                # Position sizing: risk 2%
                risk_amt = cap * 0.02
                size = risk_amt / sl_dist if sl_dist > 0 else 0

                if size > 0:
                    fee_cost = entry * size * leverage * fee
                    cap -= fee_cost
                    pos = (entry, sl_price, tp_price, size, 0, direction)

        # Track peak
        peak = max(peak, cap)
        dd = (peak - cap) / peak * 100 if peak > 0 else 0
        max_dd = max(max_dd, dd)

    # Close open position
    if pos:
        entry, sl, tp, size, bars, direction = pos
        px = cl[-1]
        if direction == 1:
            pnl = (px - entry) * size * leverage
        else:
            pnl = (entry - px) * size * leverage
        fee_cost = px * abs(size) * leverage * fee
        cap += pnl - fee_cost
        trades.append({"pnl": pnl - fee_cost, "r": 0, "reason": "EOD", "dir": direction})

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
        "sl_pct": sl_pct, "tp_pct": tp_pct,
    }


def walk_forward_df(df, bt_params, n_splits=3):
    total = len(df)
    window = total // (n_splits + 1)
    results = []
    for i in range(n_splits):
        test_start = (i + 1) * window
        test_end = min(test_start + window, total)
        if test_end <= test_start:
            break
        test_df = df.iloc[:test_end].copy().reset_index(drop=True)
        r = scalp_backtest(test_df, **bt_params)
        start_date = df.iloc[test_start]["ts"].strftime("%Y-%m-%d")
        end_date = df.iloc[min(test_end-1, len(df)-1)]["ts"].strftime("%Y-%m-%d")
        results.append({
            "window": f"{start_date} -> {end_date}",
            "trades": r["trades"], "return": r["return_pct"],
            "win_rate": r["win_rate"], "max_dd": r["max_dd"], "pf": r["pf"],
        })
        tag = "+" if r["return_pct"] > 0 else ""
        print(f"    WF {i+1}: {start_date} -> {end_date} | {r['trades']:>4d} trades | "
              f"{tag}{r['return_pct']:>7.1f}% | WR {r['win_rate']:>4.0f}% | "
              f"PF {r['pf']:.2f}")
    profitable = sum(1 for r in results if r["return"] > 0)
    return results, profitable


if __name__ == "__main__":
    print("=" * 80)
    print("  FIXED TP SCALPER: 1H BTC")
    print("=" * 80)

    df = load_1h()
    print(f"  Data: {len(df)} candles ({df['ts'].iloc[0]} to {df['ts'].iloc[-1]})")

    # ── Grid Search ──
    signals = ["ema_cross", "ema_cross_rsi", "rsi_bounce", "momentum", "pullback", "mean_revert"]
    tp_levels = [0.2, 0.3, 0.4, 0.5, 0.7, 1.0]
    sl_multipliers = [1.0, 1.5, 2.0]  # SL = TP * multiplier

    print(f"\n  Grid: {len(signals)} signals x {len(tp_levels)} TPs x {len(sl_multipliers)} SLs = {len(signals)*len(tp_levels)*len(sl_multipliers)} combos")
    print(f"  Fee: 0.05% per trade (x2 for entry+exit) = 0.10% round-trip")

    all_results = []
    count = 0
    for sig in signals:
        for tp in tp_levels:
            for sl_mult in sl_multipliers:
                sl = tp * sl_mult
                r = scalp_backtest(df, tp_pct=tp, sl_pct=sl, signal_type=sig)
                r["signal"] = sig
                r["sl_pct"] = sl
                all_results.append(r)
                count += 1

    # Show top 20 by CAGR (min 20 trades)
    profitable = [r for r in all_results if r["trades"] >= 20 and r["return_pct"] > 0]
    profitable.sort(key=lambda x: x["cagr"], reverse=True)

    print(f"\n  Total combos: {count}")
    print(f"  Profitable (min 20 trades): {len(profitable)}")

    print(f"\n  {'Signal':<16} {'TP%':>5} {'SL%':>5} {'CAGR':>8} {'MaxDD':>7} {'PF':>6} {'Trades':>7} {'WR':>5}")
    print(f"  {'─'*65}")
    for r in profitable[:25]:
        print(f"  {r['signal']:<16} {r['tp_pct']:>5.1f} {r['sl_pct']:>5.1f} "
              f"{r['cagr']:>+7.1f}% {r['max_dd']:>5.1f}% "
              f"{r['pf']:>6.2f} {r['trades']:>7} {r['win_rate']:>4.0f}%")

    # ── Top 3 Walk-Forward ──
    if profitable:
        print(f"\n{'='*80}")
        print("  WALK-FORWARD on top 3")
        print(f"{'='*80}")

        for r in profitable[:3]:
            print(f"\n  {r['signal']} | TP {r['tp_pct']}% SL {r['sl_pct']}%:")
            params = {
                "tp_pct": r["tp_pct"], "sl_pct": r["sl_pct"],
                "signal_type": r["signal"],
            }
            wf_results, wf_p = walk_forward_df(df, params)
            print(f"    WF Score: {wf_p}/{len(wf_results)} windows profitable")

    # ── Summary ──
    print(f"\n{'='*80}")
    print("  COMPARISON")
    print(f"{'='*80}")
    print(f"  {'Strategy':<40} {'CAGR':>8} {'MaxDD':>7} {'PF':>6} {'Trades':>7}")
    print(f"  {'─'*68}")
    print(f"  {'TrendJoin 4H 3x (baseline)':<40} {'+15.6%':>8} {'68.8%':>7} {'1.10':>6} {'325':>7}")
    if profitable:
        best = profitable[0]
        label = f"Scalper {best['signal']} TP{best['tp_pct']}%"
        print(f"  {label:<40} {best['cagr']:>+7.1f}% {best['max_dd']:>5.1f}% {best['pf']:>6.2f} {best['trades']:>7}")
    print(f"{'='*80}")
