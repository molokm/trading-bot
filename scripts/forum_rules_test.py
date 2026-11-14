"""Forum-sourced rules tested on our own 1H bars (read-only research).

Sources (fetched 14 Nov 2026):
  * ForexFactory "MA Crossover System that works for me" (AlistairC) —
    enter on a *pullback to the EMAs* after a fresh crossover, never when the
    rubber band is stretched, avoid "old" trends (corner-to-corner), SL at the
    swing, B/E at +50 pips, TP at +100 pips (1:2).
  * StockCharts ChartSchool "Chandelier Exit" (Le Beau / Elder) —
    long exit = highest high(22) − 3×ATR(22); short = lowest low(22) + 3×ATR.
  * Freqtrade docs (trailing stop) — trail only after an offset is reached,
    with a wider initial stop.

Every rule is tested on both halves of the sample and on the last 10 days; a
knob is only interesting when the sign holds in both halves (and in most
coins).
"""
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.services.backtest_lab import (  # noqa: E402
    FEE_ROUND_PCT, SLIPPAGE_PCT, WARMUP_BARS, BacktestParams, _signal,
    frame_from_candles, prepare,
)

DIR = sys.argv[1] if len(sys.argv) > 1 else "/tmp/candles_1114"
SYMS = sorted(f[:-5] for f in os.listdir(DIR) if f.endswith(".json"))


def load(sym, lo=0, hi=None):
    with open(os.path.join(DIR, f"{sym}.json")) as f:
        rows = sorted(json.load(f), key=lambda r: int(r[0]))
    rows = rows[lo:hi] if hi else rows[lo:]
    df = prepare(frame_from_candles(rows))
    df["ROC_12"] = df["Close"].pct_change(12) * 100.0
    # bars since EMA20/EMA50 cross (trend age, AlistairC rule 6)
    side = np.sign(df["EMA_20"] - df["EMA_50"]).fillna(0.0)
    age = []
    run = 0
    prev = 0.0
    for v in side:
        if v != prev:
            run = 0
            prev = v
        run += 1
        age.append(run)
    df["TREND_AGE"] = age
    df["STRETCH"] = (df["Close"] - df["EMA_20"]).abs() / df["ATR_14"]
    return df


def run(df, p, *, chase=2.0, max_age=None, max_stretch=None,
        entry="momentum", exit_kind="fixed", k=3.0, activate_r=1.0,
        tp=True, pullback_atr=0.5):
    n = len(df)
    trades = []
    holds = []
    i = WARMUP_BARS
    while i < n - 1:
        row = df.iloc[i]
        close = float(row["Close"])
        atr = float(row["ATR_14"] or 0)
        if atr <= 0 or close <= 0:
            i += 1
            continue
        atr_pct = atr / close * 100.0
        if entry == "momentum":
            side = _signal(row, p)
        else:  # pullback entry: trend intact + price at the mean
            e20 = float(row["EMA_20"] or 0)
            e50 = float(row["EMA_50"] or 0)
            adx = float(row["ADX_14"] or 0)
            side = None
            if adx >= p.min_adx:
                if close > e50 and e20 > e50 and abs(close - e20) / atr <= pullback_atr:
                    side = "long"
                elif close < e50 and e20 < e50 and abs(close - e20) / atr <= pullback_atr:
                    side = "short"
        if side is None:
            i += 1
            continue
        if chase and atr_pct > 0 and abs(float(row["ROC_12"] or 0)) / atr_pct > chase:
            i += 1
            continue
        if max_age is not None and float(row["TREND_AGE"] or 0) > max_age:
            i += 1
            continue
        if max_stretch is not None and float(row["STRETCH"] or 0) > max_stretch:
            i += 1
            continue
        entry_px = float(df.iloc[i + 1]["Open"])
        if entry_px <= 0:
            i += 1
            continue
        sd = atr * p.atr_stop_mult
        if side == "long":
            ep = entry_px * (1 + SLIPPAGE_PCT)
            stop, take = ep - sd, ep + sd * p.tp_r
        else:
            ep = entry_px * (1 - SLIPPAGE_PCT)
            stop, take = ep + sd, ep - sd * p.tp_r
        base_stop = stop
        hh = ll = ep
        moved = False
        exit_px = None
        end = min(n - 1, i + p.max_hold_bars)
        j = i + 1
        while j <= end:
            hi, lo = float(df.iloc[j]["High"]), float(df.iloc[j]["Low"])
            hh = max(hh, hi)
            ll = min(ll, lo)
            if exit_kind == "chandelier":
                if side == "long":
                    stop = max(stop, hh - k * atr)
                else:
                    stop = min(stop, ll + k * atr)
            elif exit_kind in ("be", "trail_offset"):
                mfe_r = ((hh - ep) / sd) if side == "long" else ((ep - ll) / sd)
                if mfe_r >= activate_r:
                    if not moved:
                        moved = True
                        if side == "long":
                            stop = max(stop, ep)
                        else:
                            stop = min(stop, ep)
                    if exit_kind == "trail_offset":
                        if side == "long":
                            stop = max(stop, hh - k * atr)
                        else:
                            stop = min(stop, ll + k * atr)
            if side == "long":
                if lo <= stop:
                    exit_px = stop
                    break
                if tp and hi >= take:
                    exit_px = take
                    break
            else:
                if hi >= stop:
                    exit_px = stop
                    break
                if tp and lo <= take:
                    exit_px = take
                    break
            j += 1
        if exit_px is None:
            exit_px = float(df.iloc[end]["Close"])
            j = end
        if exit_px <= 0:
            i += 1
            continue
        if side == "long":
            ret = (exit_px * (1 - SLIPPAGE_PCT) - ep) / ep
        else:
            ret = (ep - exit_px * (1 + SLIPPAGE_PCT)) / ep
        trades.append(ret - FEE_ROUND_PCT)
        holds.append(j - i)
        i = max(j + 1, i + 1)
    return stats(trades, holds)


def stats(trades, holds=None):
    if not trades:
        return None
    w = [t for t in trades if t > 0]
    l = [-t for t in trades if t < 0]
    pf = (sum(w) / sum(l)) if sum(l) > 0 else float("inf")
    return dict(n=len(trades), wr=len(w) / len(trades), pf=pf,
                net=sum(trades) * 100.0, exp=float(np.mean(trades)) * 100.0,
                hold=float(np.mean(holds)) if holds else 0.0)


def row(label, results):
    ok = [r for r in results if r]
    if not ok:
        print(f"  {label:44} no trades")
        return None
    n = sum(r["n"] for r in ok)
    return dict(
        label=label, n=n,
        wr=sum(r["wr"] * r["n"] for r in ok) / n,
        pf=float(np.mean([r["pf"] for r in ok])),
        net=sum(r["net"] for r in ok),
        exp=sum(r["exp"] for r in ok),
        hold=float(np.mean([r["hold"] for r in ok])),
    )


def show(label, results):
    s = row(label, results)
    if s:
        print(f"  {label:44} n={s['n']:5d} WR={s['wr']*100:5.1f}% "
              f"PF={s['pf']:5.2f} net={s['net']:8.2f}% exp={s['exp']:6.2f}% "
              f"hold={s['hold']:4.1f}b")
    return s


def main():
    with open(os.path.join(DIR, "BTC.json")) as f:
        total = len(json.load(f))
    half = total // 2
    slices = {"full": (0, None), "older": (0, half), "newer": (half, total),
              "last10d": (max(0, total - 240), None)}
    dfs = {k: {s: load(s, lo, hi) for s in SYMS} for k, (lo, hi) in slices.items()}

    def P(**kw):
        d = dict(min_adx=24.0, min_roc=0.15, atr_stop_mult=1.15, tp_r=2.0,
                 max_hold_bars=24)
        d.update(kw)
        return BacktestParams(**d)

    print("=== 0. baseline (current split: adx24 / 1.15ATR / 2R / 24h / chase2) ===")
    for name in slices:
        show(name, [run(df, P()) for df in dfs[name].values()])

    print("\n=== 1. AlistairC rule 3: enter only near EMA20 (rubber band) ===")
    for name in ("full", "older", "newer", "last10d"):
        print(f"-- {name} --")
        for x in (None, 2.0, 1.5, 1.0):
            show("no stretch cap" if x is None else f"|close-EMA20| <= {x:.1f} ATR",
                 [run(df, P(), max_stretch=x) for df in dfs[name].values()])

    print("\n=== 2. AlistairC rule 6: avoid OLD trends (bars since 20/50 cross) ===")
    for name in ("full", "older", "newer", "last10d"):
        print(f"-- {name} --")
        for a in (None, 12, 24, 48, 96):
            show("any age" if a is None else f"trend age <= {a} bars",
                 [run(df, P(), max_age=a) for df in dfs[name].values()])

    print("\n=== 3. Chandelier exit (StockCharts: extreme -/+ k*ATR) ===")
    for name in ("full", "older", "newer", "last10d"):
        print(f"-- {name} --")
        show("fixed stop/take (baseline)", [run(df, P()) for df in dfs[name].values()])
        for k in (2.5, 3.0, 3.5):
            show(f"chandelier {k:.1f}xATR + take",
                 [run(df, P(), exit_kind="chandelier", k=k) for df in dfs[name].values()])
            show(f"chandelier {k:.1f}xATR, no take",
                 [run(df, P(), exit_kind="chandelier", k=k, tp=False)
                  for df in dfs[name].values()])

    print("\n=== 4. B/E at +1R / trail after +1R (Freqtrade trailing-offset) ===")
    for name in ("full", "older", "newer", "last10d"):
        print(f"-- {name} --")
        show("fixed (baseline)", [run(df, P()) for df in dfs[name].values()])
        show("B/E after +1R", [run(df, P(), exit_kind="be", activate_r=1.0)
                               for df in dfs[name].values()])
        show("B/E after +1.5R", [run(df, P(), exit_kind="be", activate_r=1.5)
                                 for df in dfs[name].values()])
        show("trail 2xATR after +1R", [run(df, P(), exit_kind="trail_offset", k=2.0,
                                           activate_r=1.0) for df in dfs[name].values()])

    print("\n=== 5. pullback ENTRY model (AlistairC) vs momentum entry ===")
    for name in ("full", "older", "newer", "last10d"):
        print(f"-- {name} --")
        show("momentum entry (current)", [run(df, P()) for df in dfs[name].values()])
        for pb in (0.75, 0.5, 0.35):
            show(f"pullback <= {pb:.2f} ATR, chase off",
                 [run(df, P(min_roc=0.0), entry="pullback", pullback_atr=pb, chase=0.0)
                  for df in dfs[name].values()])

    print("\n=== 6. per-symbol: best exit vs baseline (full sample) ===")
    p = P()
    better = worse = flat = 0
    for s in SYMS:
        a = run(dfs["full"][s], p)
        b = run(dfs["full"][s], p, exit_kind="chandelier", k=3.0)
        if not a or not b:
            continue
        if b["exp"] > a["exp"] + 0.05:
            better += 1
            tag = "BETTER"
        elif b["exp"] < a["exp"] - 0.05:
            worse += 1
            tag = "WORSE"
        else:
            flat += 1
            tag = "flat"
        print(f"  {s[:4]:5} exp_base={a['exp']:6.2f} exp_chand={b['exp']:6.2f} "
              f"pf_base={a['pf']:5.2f} pf_chand={b['pf']:5.2f} {tag}")
    print(f"  better {better} / flat {flat} / worse {worse}")


if __name__ == "__main__":
    main()
