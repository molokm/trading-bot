#!/usr/bin/env python3
"""Сеточный перебор Rsi2Pullback: оценка каждой комбинации на 2022-2024 И 2024-2026."""
import json, subprocess, sys, os, re

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FT = os.path.join(REPO, "external", "freqtrade_test", "venv", "bin", "freqtrade")
CFG = os.path.join(REPO, "external", "freqtrade_test", "config_breakout.json")
USERDIR = os.path.join(REPO, "external", "freqtrade_test", "user_data")
STRAT = "Rsi2Pullback"
PARAMS_PATH = os.path.join(USERDIR, "strategies", "rsi2_pullback.json")

TRAIN = "20220101-20240224"
FWD = "20240225-20260802"


def run(timerange):
    p = subprocess.run(
        [FT, "backtesting", "--config", CFG, "--strategy", STRAT,
         "--userdir", USERDIR, "--timerange", timerange, "--cache", "none"],
        capture_output=True, text=True)
    out = p.stdout + p.stderr
    m = re.search(r"CAGR %\s+│\s+([-\d.]+)%", out)
    if not m:
        m = re.search(r"CAGR %\s*│\s*([-\d.]+)%", out)
    trades = re.search(r"│\s+Rsi2Pullback │\s+(\d+)", out)
    return (float(m.group(1)) if m else None), (int(trades.group(1)) if trades else 0)


def main():
    grid = {
        "rsi2_enter": [10, 15, 20],
        "rsi2_exit": [55, 60, 65],
        "max_hold": [2, 3, 5],
        "atr_stop_mult": [2.0, 3.0],
    }
    results = []
    for ent in grid["rsi2_enter"]:
        for ex in grid["rsi2_exit"]:
            for hold in grid["max_hold"]:
                for atr in grid["atr_stop_mult"]:
                    params = {
                        "strategy_name": "Rsi2Pullback",
                        "params": {
                            "buy": {"rsi2_enter": ent, "risk_per_trade": 0.10, "max_leverage": 1.0},
                            "sell": {"rsi2_exit": ex, "max_hold": hold, "atr_stop_mult": atr},
                        },
                        "ft_stratparam_v": 1,
                    }
                    with open(PARAMS_PATH, "w") as f:
                        json.dump(params, f)
                    c_train, t_train = run(TRAIN)
                    c_fwd, t_fwd = run(FWD)
                    results.append({
                        "ent": ent, "ex": ex, "hold": hold, "atr": atr,
                        "c_train": c_train, "t_train": t_train,
                        "c_fwd": c_fwd, "t_fwd": t_fwd,
                        "both_pos": (c_train is not None and c_fwd is not None
                                     and c_train > 0 and c_fwd > 0),
                    })
                    print(f"ent={ent} ex={ex} hold={hold} atr={atr} | "
                          f"train={c_train}% fwd={c_fwd}% "
                          f"({'BOTH+' if results[-1]['both_pos'] else ''})", flush=True)

    print("\n=== РОБАСТНЫЕ (положительные на обоих окнах) ===")
    robust = [r for r in results if r["both_pos"]]
    robust.sort(key=lambda r: r["c_fwd"], reverse=True)
    for r in robust:
        print(f"  ent={r['ent']} ex={r['ex']} hold={r['hold']} atr={r['atr']} "
              f"| train {r['c_train']:+.1f}% | fwd {r['c_fwd']:+.1f}%")
    if not robust:
        print("  (нет)")
    print("\n=== ТОП-5 по форварду ===")
    by_fwd = sorted(results, key=lambda r: r["c_fwd"] or -999, reverse=True)[:5]
    for r in by_fwd:
        print(f"  ent={r['ent']} ex={r['ex']} hold={r['hold']} atr={r['atr']} "
              f"| train {r['c_train']:+.1f}% | fwd {r['c_fwd']:+.1f}%")


if __name__ == "__main__":
    main()
