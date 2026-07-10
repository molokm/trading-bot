"""
Backtest rules.json strategy on native 4H Binance candles.
Uses exact same logic as orchestrator.py to validate the strategy.
"""
import json
import numpy as np
import pandas as pd
from pathlib import Path

RULES_PATH = Path(__file__).parent / "rules.json"
CANDLES_DIR = Path(__file__).parent / "data" / "candles"


def load_rules():
    with open(RULES_PATH) as f:
        return json.load(f)


def load_candles(symbol="BTCUSDT", timeframe="4H"):
    path = CANDLES_DIR / f"{symbol}_{timeframe}.csv"
    if not path.exists():
        print(f"Downloading {symbol} {timeframe} candles from Binance...")
        download_candles(symbol, timeframe)
    df = pd.read_csv(path)
    df["ts"] = pd.to_datetime(df["ts"])
    return df


def download_candles(symbol="BTCUSDT", timeframe="4H", limit=1000):
    """Download candles from Binance."""
    import httpx

    CANDLES_DIR.mkdir(parents=True, exist_ok=True)

    all_rows = []
    end_time = None

    while len(all_rows) < limit:
        params = {"symbol": symbol, "interval": timeframe.lower(), "limit": 1000}
        if end_time:
            params["endTime"] = end_time

        resp = httpx.get("https://api.binance.com/api/v3/klines", params=params, timeout=30)
        data = resp.json()

        if not data:
            break

        for c in data:
            all_rows.append({
                "ts": pd.to_datetime(c[0], unit="ms"),
                "Open": float(c[1]),
                "High": float(c[2]),
                "Low": float(c[3]),
                "Close": float(c[4]),
                "Volume": float(c[5]),
            })

        end_time = int(data[-1][0]) - 1
        if len(data) < 1000:
            break

    df = pd.DataFrame(all_rows)
    df = df.drop_duplicates(subset="ts").sort_values("ts").reset_index(drop=True)
    df.to_csv(CANDLES_DIR / f"{symbol}_{timeframe}.csv", index=False)
    print(f"Saved {len(df)} candles to {CANDLES_DIR / f'{symbol}_{timeframe}.csv'}")
    return df


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
    up_move = h - h.shift(1)
    down_move = l.shift(1) - l
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)
    tr = pd.concat([(h-l), (h-c.shift()).abs(), (l-c.shift()).abs()], axis=1).max(axis=1)
    atr_val = tr.ewm(alpha=1/n, adjust=False).mean()
    plus_di = 100 * pd.Series(plus_dm, index=df.index).ewm(alpha=1/n, adjust=False).mean() / atr_val
    minus_di = 100 * pd.Series(minus_dm, index=df.index).ewm(alpha=1/n, adjust=False).mean() / atr_val
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    adx_val = dx.ewm(alpha=1/n, adjust=False).mean()
    return adx_val, plus_di, minus_di


def compute_indicators(df, rules):
    df = df.copy()
    df["EMA_20"] = ema(df["Close"], 20)
    df["EMA_50"] = ema(df["Close"], 50)
    df["RSI_14"] = rsi(df["Close"], 14)
    df["ATR_14"] = calc_atr(df, 14)
    adx_val, _, _ = adx(df, 14)
    df["ADX_14"] = adx_val
    df["Vol_SMA_20"] = df["Volume"].rolling(20).mean()
    df["Vol_Ratio"] = df["Volume"] / df["Vol_SMA_20"].replace(0, np.nan)
    return df


def detect_regime(row):
    ema20 = row.get("EMA_20", 0)
    ema50 = row.get("EMA_50", 0)
    adx_val = row.get("ADX_14", 0)
    rsi_val = row.get("RSI_14", 50)
    if pd.isna(ema20) or pd.isna(ema50) or pd.isna(adx_val):
        return "unknown"
    if ema20 > ema50 and adx_val > 20 and rsi_val > 50:
        return "bull"
    elif ema20 < ema50 and adx_val > 20 and rsi_val < 50:
        return "bear"
    return "sideways"


def check_entry(row, prev):
    """Check all entry rules from rules.json. Returns (pass, details)."""
    # Rule 1: EMA alignment
    ema20 = prev.get("EMA_20", 0)
    ema50 = prev.get("EMA_50", 0)
    if pd.isna(ema20) or pd.isna(ema50):
        return False, {}
    trend_ok = ema20 > ema50

    # Rule 2: Pullback zone
    price = prev["Close"]
    dist = (price - ema20) / ema20 * 100 if ema20 > 0 else 0
    pullback_ok = -3.0 < dist < 2.5

    # Rule 3: RSI not dead
    rsi_val = prev.get("RSI_14", 50)
    rsi_ok = rsi_val > 30 and not pd.isna(rsi_val)

    # Rule 4: ADX trending
    adx_val = prev.get("ADX_14", 0)
    adx_ok = adx_val > 18 and not pd.isna(adx_val)

    # Rule 5: Volume (optional)
    vol_ratio = prev.get("Vol_Ratio", 0)
    vol_ok = vol_ratio > 1.2 or trend_ok

    details = {
        "trend_filter": trend_ok,
        "pullback_zone": pullback_ok,
        "rsi_not_dead": rsi_ok,
        "adx_trending": adx_ok,
        "volume_confirm": vol_ok,
    }

    # ALL required rules pass (volume is optional)
    passed = trend_ok and pullback_ok and rsi_ok and adx_ok
    return passed, details


def backtest(symbol="BTCUSDT", timeframe="4H", use_regime=True, initial_capital=10000,
             fee_rate=0.0005, sl_mult=1.8, tp_mult=4.5):
    """Run backtest of rules.json strategy."""
    rules = load_rules()
    df = load_candles(symbol, timeframe)
    df = compute_indicators(df, rules)

    capital = initial_capital
    peak_capital = initial_capital
    max_drawdown = 0
    trades = []
    equity_curve = []
    position = None  # {entry, stop, target, size, entry_time, r_value}

    for i in range(1, len(df)):
        row = df.iloc[i]
        prev = df.iloc[i-1]
        ts = row["ts"]

        # If in position, check exit
        if position is not None:
            hit_stop = row["Low"] <= position["stop"]
            hit_target = row["High"] >= position["target"]

            if hit_stop and hit_target:
                # Assume stop hit first (conservative)
                exit_price = position["stop"]
                exit_reason = "stop"
            elif hit_stop:
                exit_price = position["stop"]
                exit_reason = "stop"
            elif hit_target:
                exit_price = position["target"]
                exit_reason = "target"
            else:
                equity_curve.append({"ts": ts, "equity": capital})
                continue

            # Calculate PnL
            r_value = position["r_value"]
            final_r = (exit_price - position["entry"]) / r_value if r_value > 0 else 0
            pnl = (exit_price - position["entry"]) * position["size"]
            fee = exit_price * position["size"] * fee_rate
            net_pnl = pnl - fee
            capital += net_pnl

            trades.append({
                "entry_time": position["entry_time"],
                "exit_time": ts,
                "entry": position["entry"],
                "exit": exit_price,
                "stop": position["stop"],
                "target": position["target"],
                "r_value": r_value,
                "final_r": round(final_r, 3),
                "pnl": round(net_pnl, 2),
                "exit_reason": exit_reason,
                "capital_after": round(capital, 2),
            })

            position = None

        # If no position, check entry
        if position is None and capital > 0:
            regime = detect_regime(row) if use_regime else "bull"
            passed, details = check_entry(row, prev)

            if passed and regime == "bull":
                entry_price = row["Close"]
                atr = prev.get("ATR_14", 0)
                if pd.isna(atr) or atr <= 0:
                    equity_curve.append({"ts": ts, "equity": capital})
                    continue

                stop = entry_price - sl_mult * atr
                target = entry_price + tp_mult * atr
                r_value = entry_price - stop

                # Position sizing: risk 2% of capital
                risk_amount = capital * 0.02
                size = risk_amount / r_value if r_value > 0 else 0

                if size > 0:
                    fee = entry_price * size * fee_rate
                    capital -= fee

                    position = {
                        "entry": entry_price,
                        "stop": stop,
                        "target": target,
                        "size": size,
                        "entry_time": ts,
                        "r_value": r_value,
                        "regime": regime,
                    }

        peak_capital = max(peak_capital, capital)
        dd = (peak_capital - capital) / peak_capital * 100 if peak_capital > 0 else 0
        max_drawdown = max(max_drawdown, dd)
        equity_curve.append({"ts": ts, "equity": capital})

    # Close any remaining position at last price
    if position is not None and len(df) > 0:
        last_row = df.iloc[-1]
        exit_price = last_row["Close"]
        r_value = position["r_value"]
        final_r = (exit_price - position["entry"]) / r_value if r_value > 0 else 0
        pnl = (exit_price - position["entry"]) * position["size"]
        fee = exit_price * position["size"] * fee_rate
        capital += pnl - fee
        trades.append({
            "entry_time": position["entry_time"],
            "exit_time": last_row["ts"],
            "entry": position["entry"],
            "exit": exit_price,
            "stop": position["stop"],
            "target": position["target"],
            "r_value": r_value,
            "final_r": round(final_r, 3),
            "pnl": round(pnl - fee, 2),
            "exit_reason": "close_eod",
            "capital_after": round(capital, 2),
        })

    return analyze_results(trades, equity_curve, initial_capital, max_drawdown, use_regime, symbol, timeframe,
                           sl_mult, tp_mult, fee_rate)


def analyze_results(trades, equity_curve, initial_capital, max_drawdown, use_regime,
                    symbol, timeframe, sl_mult, tp_mult, fee_rate):
    """Analyze and print backtest results."""
    if not trades:
        print(f"\n{'='*60}")
        print(f"  NO TRADES — {symbol} {timeframe} (regime={'ON' if use_regime else 'OFF'})")
        print(f"{'='*60}")
        return {"trades": 0, "total_return": 0}

    total_return = (equity_curve[-1]["equity"] - initial_capital) / initial_capital * 100
    years = len(equity_curve) * 4 / (365 * 6)  # 4H candles
    cagr = ((equity_curve[-1]["equity"] / initial_capital) ** (1 / years) - 1) * 100 if years > 0 else 0

    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    win_rate = len(wins) / len(trades) * 100

    total_win = sum(t["pnl"] for t in wins)
    total_loss = sum(t["pnl"] for t in losses)
    profit_factor = abs(total_win / total_loss) if total_loss != 0 else float("inf")

    avg_r = np.mean([t["final_r"] for t in trades])
    avg_win_r = np.mean([t["final_r"] for t in wins]) if wins else 0
    avg_loss_r = np.mean([t["final_r"] for t in losses]) if losses else 0

    targets = [t for t in trades if t["exit_reason"] == "target"]
    stops = [t for t in trades if t["exit_reason"] == "stop"]

    print(f"\n{'='*60}")
    print(f"  BACKTEST: {symbol} {timeframe}")
    print(f"  Strategy: Trend Join Long (rules.json)")
    print(f"  Regime Filter: {'ON' if use_regime else 'OFF'}")
    print(f"  SL: {sl_mult}x ATR | TP: {tp_mult}x ATR | Fee: {fee_rate*100:.2f}%")
    print(f"{'='*60}")
    print(f"  Period: {trades[0]['entry_time'].strftime('%Y-%m-%d')} → {trades[-1]['exit_time'].strftime('%Y-%m-%d')}")
    print(f"  Duration: {years:.1f} years")
    print()
    print(f"  Capital:  ${initial_capital:,.0f} → ${equity_curve[-1]['equity']:,.0f}")
    print(f"  Return:   {total_return:+.1f}%")
    print(f"  CAGR:     {cagr:+.1f}%")
    print(f"  MaxDD:    {max_drawdown:.1f}%")
    print()
    print(f"  Trades:   {len(trades)} (W:{len(wins)} L:{len(losses)})")
    print(f"  Win Rate: {win_rate:.0f}%")
    print(f"  Avg R:    {avg_r:+.3f}")
    print(f"  Avg Win:  {avg_win_r:+.3f}R | Avg Loss: {avg_loss_r:+.3f}R")
    print(f"  PF:       {profit_factor:.2f}")
    print()
    print(f"  Targets:  {len(targets)} | Stops: {len(stops)} | Other: {len(trades) - len(targets) - len(stops)}")
    print()
    print(f"  Total Fees: ${sum(t['pnl'] for t in trades) - (equity_curve[-1]['equity'] - initial_capital):,.2f}")
    print(f"{'='*60}")

    return {
        "trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "total_return": total_return,
        "cagr": cagr,
        "max_drawdown": max_drawdown,
        "profit_factor": profit_factor,
        "avg_r": avg_r,
        "avg_win_r": avg_win_r,
        "avg_loss_r": avg_loss_r,
        "final_capital": equity_curve[-1]["equity"],
        "targets": len(targets),
        "stops": len(stops),
    }


def walk_forward(symbol="BTCUSDT", timeframe="4H", n_splits=3):
    """Walk-forward validation: train on window, test on next window."""
    rules = load_rules()
    df = load_candles(symbol, timeframe)
    df = compute_indicators(df, rules)

    total_len = len(df)
    window = total_len // (n_splits + 1)

    results = []
    for i in range(n_splits):
        train_start = i * window
        train_end = (i + 1) * window
        test_start = train_end
        test_end = min(test_start + window, total_len)

        if test_end <= test_start:
            break

        # Test on out-of-sample
        test_df = df.iloc[test_start:test_end].copy().reset_index(drop=True)

        capital = 10000
        peak = 10000
        max_dd = 0
        trades = []

        position = None
        for j in range(1, len(test_df)):
            row = test_df.iloc[j]
            prev = test_df.iloc[j-1]

            if position:
                hit_stop = row["Low"] <= position["stop"]
                hit_target = row["High"] >= position["target"]
                if hit_stop or hit_target:
                    exit_price = position["stop"] if hit_stop else position["target"]
                    pnl = (exit_price - position["entry"]) * position["size"]
                    fee = exit_price * position["size"] * 0.0005
                    capital += pnl - fee
                    trades.append({"pnl": pnl - fee})
                    position = None

            if position is None and capital > 0:
                regime = detect_regime(row)
                passed, _ = check_entry(row, prev)
                if passed and regime == "bull":
                    entry = row["Close"]
                    atr = prev.get("ATR_14", 0)
                    if pd.isna(atr) or atr <= 0:
                        continue
                    stop = entry - 1.8 * atr
                    target = entry + 4.5 * atr
                    r_value = entry - stop
                    size = (capital * 0.02) / r_value if r_value > 0 else 0
                    if size > 0:
                        fee = entry * size * 0.0005
                        capital -= fee
                        position = {"entry": entry, "stop": stop, "target": target, "size": size, "r_value": r_value}

            peak = max(peak, capital)
            dd = (peak - capital) / peak * 100 if peak > 0 else 0
            max_dd = max(max_dd, dd)

        total_return = (capital - 10000) / 10000 * 100
        wins = sum(1 for t in trades if t["pnl"] > 0)
        win_rate = wins / len(trades) * 100 if trades else 0

        train_start_date = df.iloc[train_start]["ts"].strftime("%Y-%m-%d")
        test_start_date = df.iloc[test_start]["ts"].strftime("%Y-%m-%d")
        test_end_date = df.iloc[min(test_end-1, len(df)-1)]["ts"].strftime("%Y-%m-%d")

        results.append({
            "window": f"{test_start_date} → {test_end_date}",
            "trades": len(trades),
            "return": total_return,
            "win_rate": win_rate,
            "max_dd": max_dd,
        })

        emoji = "🟢" if total_return > 0 else "🔴"
        print(f"  {emoji} Window {i+1}: {test_start_date} → {test_end_date} | "
              f"Trades: {len(trades)} | Return: {total_return:+.1f}% | "
              f"WR: {win_rate:.0f}% | MaxDD: {max_dd:.1f}%")

    profitable = sum(1 for r in results if r["return"] > 0)
    print(f"\n  Walk-Forward: {profitable}/{len(results)} windows profitable", flush=True)

    return results


if __name__ == "__main__":
    import sys

    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    timeframe = sys.argv[2] if len(sys.argv) > 2 else "4H"

    print(f"\n{'#'*60}")
    print(f"  RULES.JSON STRATEGY BACKTEST")
    print(f"  {symbol} {timeframe}")
    print(f"{'#'*60}")

    # Download candles if needed
    load_candles(symbol, timeframe)

    # Backtest with regime filter
    print(f"\n{'─'*60}")
    print(f"  WITH REGIME FILTER")
    print(f"{'─'*60}")
    r1 = backtest(symbol, timeframe, use_regime=True)

    # Backtest without regime filter
    print(f"\n{'─'*60}")
    print(f"  WITHOUT REGIME FILTER")
    print(f"{'─'*60}")
    r2 = backtest(symbol, timeframe, use_regime=False)

    # Walk-forward
    print(f"\n{'─'*60}")
    print(f"  WALK-FORWARD VALIDATION (WITH REGIME)")
    print(f"{'─'*60}")
    wf = walk_forward(symbol, timeframe)

    # Comparison
    print(f"\n{'='*60}")
    print(f"  COMPARISON")
    print(f"{'='*60}")
    print(f"  {'Metric':<20} {'With Regime':>12} {'No Regime':>12}")
    print(f"  {'─'*44}")
    print(f"  {'Trades':<20} {r1['trades']:>12} {r2['trades']:>12}")
    print(f"  {'Win Rate':<20} {r1['win_rate']:>11.0f}% {r2['win_rate']:>11.0f}%")
    print(f"  {'Return':<20} {r1['total_return']:>+11.1f}% {r2['total_return']:>+11.1f}%")
    print(f"  {'CAGR':<20} {r1['cagr']:>+11.1f}% {r2['cagr']:>+11.1f}%")
    print(f"  {'MaxDD':<20} {r1['max_drawdown']:>11.1f}% {r2['max_drawdown']:>11.1f}%")
    print(f"  {'PF':<20} {r1['profit_factor']:>12.2f} {r2['profit_factor']:>12.2f}")
    print(f"  {'Avg R':<20} {r1['avg_r']:>+12.3f} {r2['avg_r']:>+12.3f}")
    print(f"{'='*60}")
