#!/usr/bin/env python3
"""
Parameter sweep for Momentum Rotation v3 — honest methodology.
Reuses data cache from honest_backtest_3y.py.
"""
import asyncio
import json
import math
import os
import sys
from dataclasses import dataclass, asdict
from collections import defaultdict
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ═══ Reuse indicator functions from honest_backtest_3y ═══
exec_globals = {}
with open("scripts/honest_backtest_3y.py") as f:
    src = f.read()
# Extract only the indicator functions and data loading
indicator_section = src.split("async def load_data")[0]
exec(indicator_section, exec_globals)

ema_series = exec_globals["ema_series"]
atr_series = exec_globals["atr_series"]
adx_series = exec_globals["adx_series"]
rsi_series = exec_globals["rsi_series"]
roc_series = exec_globals["roc_series"]
sma_series = exec_globals["sma_series"]
correlation = exec_globals["correlation"]

COINS = ["BTC", "ETH", "BNB", "SOL"]
CT_VAL = {"BTC": 0.01, "ETH": 0.1, "BNB": 0.1, "SOL": 0.1}
LOT_SZ = {"BTC": 0.01, "ETH": 0.01, "BNB": 0.01, "SOL": 0.01}
COMMISSION = 0.001
SLIPPAGE = 0.0005

@dataclass
class StratConfig:
    name: str = "sweep"
    capital: float = 10000.0
    top_k: int = 2
    roc_period: int = 14
    ema_fast: int = 20
    ema_slow: int = 50
    atr_period: int = 14
    adx_min: float = 25.0
    min_hold_days: int = 20
    max_leverage: float = 2.0
    risk_per_trade: float = 0.10
    trail_atr_mult: float = 0.2
    breakeven_pct: float = 0.02
    partial_tp_pct: float = 0.05
    partial_tp_ratio: float = 0.5
    rsi_long_max: float = 75.0
    rsi_short_min: float = 25.0
    vol_mult: float = 1.5
    corr_threshold: float = 0.7
    atr_stop_mult: float = 3.0
    max_margin_pct: float = 1.0


def dynamic_leverage(atr, price, max_lev):
    if atr <= 0 or price <= 0:
        return 1.0
    atr_pct = atr / price
    lev = 1.0 / (atr_pct * 2)
    return max(1.0, min(lev, max_lev))


def calc_size(equity, coin, price, stop_dist, leverage, risk_pct, max_margin_pct):
    ct_val = CT_VAL.get(coin, 0.01)
    lot = LOT_SZ.get(coin, 0.01)
    if stop_dist <= 0 or price <= 0:
        stop_pct = 0.03
    else:
        stop_pct = stop_dist / price
    risk_usd = equity * risk_pct
    notional = risk_usd / stop_pct
    margin = notional / leverage if leverage > 0 else notional
    max_margin = equity * max_margin_pct
    if margin > max_margin:
        margin = max_margin
        notional = margin * leverage
    raw_sz = notional / (ct_val * price)
    sz = math.floor(raw_sz / lot + 1e-12) * lot
    return max(sz, lot)


def run_strategy(daily_data, cfg: StratConfig):
    coin_data = {}
    for coin in COINS:
        bars = daily_data.get(coin, [])
        if len(bars) < 250:
            return None
        closes = [c["C"] for c in bars]
        highs = [c["H"] for c in bars]
        lows = [c["L"] for c in bars]
        coin_data[coin] = {
            "candles": bars,
            "roc": roc_series(closes, cfg.roc_period),
            "ema_f": ema_series(closes, cfg.ema_fast),
            "ema_s": ema_series(closes, cfg.ema_slow),
            "atr": atr_series(highs, lows, closes, cfg.atr_period),
            "adx": adx_series(highs, lows, closes, 14),
            "rsi": rsi_series(closes, cfg.rsi_period if hasattr(cfg, 'rsi_period') else 14),
            "sma200": sma_series(closes, 200),
        }

    common = None
    for coin, cd in coin_data.items():
        dates = {c["date"] for c in cd["candles"]}
        common = dates if common is None else (common & dates)
    all_dates = sorted(common)

    date_idx = {}
    for coin, cd in coin_data.items():
        date_idx[coin] = {c["date"]: i for i, c in enumerate(cd["candles"])}

    equity = cfg.capital
    positions = {}
    trades = []
    equity_curve = []
    filters_hit = defaultdict(int)
    last_rotate = -10**9
    start_i = max(cfg.ema_slow + 20, 210)
    peak_equity = equity
    max_dd = 0.0

    for i in range(start_i, len(all_dates)):
        date = all_dates[i]
        sig_date = all_dates[i - 1]

        # 1. Manage existing positions (H/L first, pessimistic)
        for coin in list(positions.keys()):
            pos = positions[coin]
            ci = date_idx[coin].get(date)
            if ci is None:
                continue
            bar = coin_data[coin]["candles"][ci]
            ct = CT_VAL[coin]
            trail = pos["atr"] * cfg.trail_atr_mult
            if trail <= 0:
                trail = pos["entry"] * 0.02

            hit_stop = False
            exit_raw = None
            reason = "trail_stop"

            if pos["side"] == "long":
                if bar["L"] <= pos["stop"]:
                    hit_stop = True
                    exit_raw = pos["stop"]
                if not hit_stop and bar["H"] > pos["peak"]:
                    pos["peak"] = bar["H"]
                    new_stop = pos["peak"] - trail
                    if new_stop > pos["stop"]:
                        pos["stop"] = new_stop
                if (not hit_stop and not pos["breakeven"]
                        and bar["C"] >= pos["entry"] * (1 + cfg.breakeven_pct)):
                    pos["stop"] = max(pos["stop"], pos["entry"] * 0.999)
                    pos["breakeven"] = True
            else:
                if bar["H"] >= pos["stop"]:
                    hit_stop = True
                    exit_raw = pos["stop"]
                if not hit_stop and bar["L"] < pos["trough"]:
                    pos["trough"] = bar["L"]
                    new_stop = pos["trough"] * (1 + trail / pos["entry"])
                    if new_stop < pos["stop"]:
                        pos["stop"] = new_stop
                if (not hit_stop and not pos["breakeven"]
                        and bar["C"] <= pos["entry"] * (1 - cfg.breakeven_pct)):
                    pos["stop"] = min(pos["stop"], pos["entry"] * 1.001)
                    pos["breakeven"] = True

            if hit_stop:
                if pos["side"] == "long":
                    exit_fill = exit_raw * (1 - SLIPPAGE)
                    pnl = pos["size"] * ct * (exit_fill - pos["entry"]) - pos["size"] * ct * exit_fill * COMMISSION
                else:
                    exit_fill = exit_raw * (1 + SLIPPAGE)
                    pnl = pos["size"] * ct * (pos["entry"] - exit_fill) - pos["size"] * ct * exit_fill * COMMISSION
                equity += pnl
                trades.append({"date": date, "coin": coin, "side": pos["side"],
                    "entry": pos["entry"], "exit": exit_fill, "size": pos["size"],
                    "pnl": round(pnl, 2), "reason": reason,
                    "hold_days": i - pos["entry_bar"], "notional": round(pos["notional"], 0)})
                del positions[coin]

        # 2. Rotation check
        if i - last_rotate < cfg.min_hold_days and positions:
            equity_curve.append((date, equity))
            continue

        # 3. Compute indicators on signal bar (yesterday)
        si = date_idx.get("BTC", {}).get(sig_date)
        if si is None:
            equity_curve.append((date, equity))
            continue

        rankings = []
        for coin in COINS:
            cd = coin_data.get(coin)
            if cd is None:
                continue
            idx = date_idx[coin].get(sig_date)
            if idx is None:
                continue
            roc_val = cd["roc"][idx]
            ema_trend = cd["ema_f"][idx] > cd["ema_s"][idx]
            adx_val = cd["adx"][idx]
            atr_val = cd["atr"][idx]
            rsi_val = cd["rsi"][idx]
            sma200 = cd["sma200"][idx] if idx < len(cd["sma200"]) else 0
            if atr_val <= 0 or roc_val == 0:
                continue
            rankings.append((coin, roc_val, ema_trend, adx_val, atr_val, rsi_val, sma200))

        if not rankings:
            equity_curve.append((date, equity))
            continue

        rankings.sort(key=lambda x: x[1], reverse=True)

        # 4. Pick targets with filters
        target_coins = set()
        btc_above_200 = False
        btc_ind = next((r for r in rankings if r[0] == "BTC"), None)
        if btc_ind and btc_ind[6] > 0:
            btc_above_200 = True

        for coin, roc_val, ema_trend, adx_val, atr_val, rsi_val, sma200 in rankings:
            if len(target_coins) >= cfg.top_k:
                break

            # Filters
            if adx_val < cfg.adx_min:
                filters_hit["adx"] += 1
                continue
            if abs(roc_val) < 0.03:
                filters_hit["min_roc"] += 1
                continue
            if not ema_trend and roc_val > 0:
                filters_hit["ema_trend"] += 1
                continue
            if ema_trend and roc_val < 0:
                filters_hit["ema_trend"] += 1
                continue
            if rsi_val > cfg.rsi_long_max and ema_trend:
                filters_hit["rsi_overbought"] += 1
                continue
            if rsi_val < cfg.rsi_short_min and not ema_trend:
                filters_hit["rsi_oversold"] += 1
                continue

            # Vol filter
            avg_atr_30 = sum(cd["atr"][max(0, idx-30):idx+1]) / min(30, idx+1) if idx >= 29 else atr_val
            if atr_val > avg_atr_30 * cfg.vol_mult:
                filters_hit["volatility"] += 1
                continue

            # Correlation
            if not correlation(coin_data[coin]["candles"][:idx+1], coin_data["BTC"]["candles"][:idx+1], 30):
                filters_hit["correlation"] += 1
                continue

            # Bear market long filter
            if not btc_above_200 and roc_val > 0 and ema_trend:
                filters_hit["bear_long"] += 1
                continue

            side = "long" if (roc_val > 0 and ema_trend) else "short"
            target_coins.add((coin, side))

        # 5. Close positions not in target
        for coin in list(positions.keys()):
            pos = positions[coin]
            if (coin, pos["side"]) not in target_coins:
                ci = date_idx[coin].get(date)
                if ci is None:
                    continue
                bar = coin_data[coin]["candles"][ci]
                ct = CT_VAL[coin]
                exit_raw = bar["O"]
                if pos["side"] == "long":
                    exit_fill = exit_raw * (1 - SLIPPAGE)
                    pnl = pos["size"] * ct * (exit_fill - pos["entry"]) - pos["size"] * ct * exit_fill * COMMISSION
                else:
                    exit_fill = exit_raw * (1 + SLIPPAGE)
                    pnl = pos["size"] * ct * (pos["entry"] - exit_fill) - pos["size"] * ct * exit_fill * COMMISSION
                equity += pnl
                trades.append({"date": date, "coin": coin, "side": pos["side"],
                    "entry": pos["entry"], "exit": exit_fill, "size": pos["size"],
                    "pnl": round(pnl, 2), "reason": "rotation_exit",
                    "hold_days": i - pos["entry_bar"], "notional": round(pos["notional"], 0)})
                del positions[coin]

        # 6. Open new positions
        for coin, side in target_coins:
            if coin in positions:
                continue
            ci = date_idx[coin].get(date)
            if ci is None:
                continue
            bar = coin_data[coin]["candles"][ci]
            sig_idx = date_idx[coin].get(sig_date)
            if sig_idx is None:
                continue
            entry_raw = bar["O"]
            atr_val = coin_data[coin]["atr"][sig_idx]
            ct = CT_VAL[coin]
            lev = dynamic_leverage(atr_val, entry_raw, cfg.max_leverage)
            stop_dist = atr_val * cfg.atr_stop_mult
            sz = calc_size(equity, coin, entry_raw, stop_dist, lev, cfg.risk_per_trade, cfg.max_margin_pct)
            if sz <= 0:
                continue

            if side == "long":
                stop = entry_raw - stop_dist
                notional = sz * ct * entry_raw
            else:
                stop = entry_raw + stop_dist
                notional = sz * ct * entry_raw

            margin = notional / lev if lev > 0 else notional
            if margin > equity * cfg.max_margin_pct:
                continue

            positions[coin] = {
                "coin": coin, "side": side, "size": sz, "entry": entry_raw,
                "stop": stop, "peak": entry_raw if side == "long" else entry_raw,
                "trough": entry_raw if side == "short" else entry_raw,
                "atr": atr_val, "leverage": lev, "notional": notional,
                "entry_bar": i, "breakeven": False, "partial_done": False,
                "signal_id": 0,
            }

        # Update daily check
        if positions or target_coins:
            last_rotate = i

        equity_curve.append((date, equity))
        if equity > peak_equity:
            peak_equity = equity
        dd = (peak_equity - equity) / peak_equity * 100 if peak_equity > 0 else 0
        max_dd = max(max_dd, dd)

    # Final close
    for coin in list(positions.keys()):
        pos = positions[coin]
        ci = date_idx[coin].get(all_dates[-1])
        if ci is None:
            continue
        bar = coin_data[coin]["candles"][ci]
        ct = CT_VAL[coin]
        exit_raw = bar["C"]
        if pos["side"] == "long":
            exit_fill = exit_raw * (1 - SLIPPAGE)
            pnl = pos["size"] * ct * (exit_fill - pos["entry"]) - pos["size"] * ct * exit_fill * COMMISSION
        else:
            exit_fill = exit_raw * (1 + SLIPPAGE)
            pnl = pos["size"] * ct * (pos["entry"] - exit_fill) - pos["size"] * ct * exit_fill * COMMISSION
        equity += pnl
        trades.append({"date": all_dates[-1], "coin": coin, "side": pos["side"],
            "entry": pos["entry"], "exit": exit_fill, "size": pos["size"],
            "pnl": round(pnl, 2), "reason": "final_close",
            "hold_days": len(all_dates) - 1 - pos["entry_bar"], "notional": round(pos["notional"], 0)})

    # Summarize
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total_pnl = sum(t["pnl"] for t in trades)
    years = (datetime.strptime(all_dates[-1], "%Y-%m-%d") - datetime.strptime(all_dates[0], "%Y-%m-%d")).days / 365.25
    cagr = ((equity / cfg.capital) ** (1 / years) - 1) * 100 if years > 0 and cfg.capital > 0 else 0
    avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
    profit_factor = abs(sum(t["pnl"] for t in wins) / sum(t["pnl"] for t in losses)) if losses and sum(t["pnl"] for t in losses) != 0 else 0
    wr = len(wins) / len(trades) * 100 if trades else 0

    return {
        "name": cfg.name,
        "config": asdict(cfg),
        "period": f"{all_dates[0]} → {all_dates[-1]}",
        "years": round(years, 2),
        "capital": cfg.capital,
        "final_equity": round(equity, 2),
        "total_return_pct": round((equity / cfg.capital - 1) * 100, 1),
        "cagr_pct": round(cagr, 1),
        "max_drawdown_pct": round(max_dd, 1),
        "sharpe": round((equity / cfg.capital - 1) / years / 0.15 * math.sqrt(252) if years > 0 else 0, 2),  # rough
        "closed_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(wr, 1),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "profit_factor": round(profit_factor, 2),
        "total_pnl": round(total_pnl, 2),
        "filters_hit": dict(filters_hit),
    }


async def main():
    cache_path = "scripts/honest_3y_cache.json"
    if not os.path.exists(cache_path):
        print("ERROR: honest_3y_cache.json not found. Run honest_backtest_3y.py first.")
        sys.exit(1)

    with open(cache_path) as f:
        daily_data = json.load(f)
    print(f"Data loaded: {list(daily_data.keys())}", flush=True)

    BASE = {
        "name": "v3_live_match",
        "capital": 10000.0,
        "top_k": 2,
        "roc_period": 14,
        "ema_fast": 20,
        "ema_slow": 50,
        "atr_period": 14,
        "adx_min": 25.0,
        "min_hold_days": 20,
        "max_leverage": 2.0,
        "risk_per_trade": 0.10,
        "trail_atr_mult": 0.2,
        "breakeven_pct": 0.02,
        "partial_tp_pct": 0.05,
        "partial_tp_ratio": 0.5,
        "rsi_long_max": 75.0,
        "rsi_short_min": 25.0,
        "vol_mult": 1.5,
        "corr_threshold": 0.7,
        "atr_stop_mult": 3.0,
        "max_margin_pct": 1.0,
    }

    # One-at-a-time sweep
    param_ranges = {
        "adx_min": [18.0, 20.0, 22.0, 25.0, 28.0, 30.0],
        "min_hold_days": [3, 5, 10, 15, 20],
        "risk_per_trade": [0.05, 0.10, 0.15, 0.20],
        "trail_atr_mult": [0.1, 0.2, 0.3, 0.5],
        "breakeven_pct": [0.01, 0.02, 0.03],
        "partial_tp_pct": [0.03, 0.05, 0.07, 0.10],
        "max_leverage": [1.0, 1.5, 2.0],
        "atr_stop_mult": [2.0, 2.5, 3.0, 3.5, 4.0],
        "rsi_long_max": [70, 75, 80],
        "rsi_short_min": [20, 25, 30],
        "vol_mult": [1.0, 1.5, 2.0],
        "corr_threshold": [0.5, 0.6, 0.7, 0.8],
    }

    total_combos = 1
    for v in param_ranges.values():
        total_combos *= len(v)
    print(f"Total combinations: {total_combos} (one-at-a-time: {sum(len(v) for v in param_ranges.values())})", flush=True)

    # One-at-a-time sweep
    results = []
    base_result = run_strategy(daily_data, StratConfig(**BASE))
    if base_result:
        results.append(base_result)
        print(f"BASE: ret={base_result['total_return_pct']:+.1f}% CAGR={base_result['cagr_pct']:+.1f}% DD={base_result['max_drawdown_pct']:.1f}% Sharpe={base_result['sharpe']:.2f} PF={base_result['profit_factor']:.2f}", flush=True)

    for param, values in param_ranges.items():
        for val in values:
            cfg_dict = {**BASE, param: val, "name": f"{param}={val}"}
            r = run_strategy(daily_data, StratConfig(**cfg_dict))
            if r:
                results.append(r)
                print(f"  {param}={val}: ret={r['total_return_pct']:+.1f}% CAGR={r['cagr_pct']:+.1f}% DD={r['max_drawdown_pct']:.1f}% Sharpe={r['sharpe']:.2f} PF={r['profit_factor']:.2f}", flush=True)

    results.sort(key=lambda x: x.get("cagr_pct", -999), reverse=True)

    out_path = "scripts/sweep_results.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\nSaved {len(results)} results to {out_path}", flush=True)

    print(f"\n{'='*80}")
    print("TOP 15 RESULTS")
    print(f"{'='*80}")
    for i, r in enumerate(results[:15]):
        c = r["config"]
        diffs = {k: v for k, v in c.items() if k not in BASE and k != "name"}
        diff_str = ", ".join(f"{k}={v}" for k, v in diffs.items())
        print(f"\n#{i+1}: ret={r['total_return_pct']:+.1f}% CAGR={r['cagr_pct']:+.1f}% DD={r['max_drawdown_pct']:.1f}% Sharpe={r['sharpe']:.2f} PF={r['profit_factor']:.2f} WR={r['win_rate']:.0f}% trades={r['closed_trades']}")
        if diff_str:
            print(f"    {diff_str}")

    # Best config
    best = results[0]
    print(f"\n{'='*80}")
    print("BEST CONFIG")
    print(f"{'='*80}")
    print(json.dumps(best["config"], indent=2, default=str))
    print(f"Result: ret={best['total_return_pct']:+.1f}% CAGR={best['cagr_pct']:+.1f}%")

if __name__ == "__main__":
    asyncio.run(main())
