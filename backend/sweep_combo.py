"""
Deep combo sweep: Mode F (OrderBook Proxy) + Mode H (OBV Divergence) on 1H.
Combines all winning enhancements: Dynamic Sizing, Session Filter, Partial TP, Breakeven.
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.dirname(__file__))

import numpy as np
import asyncio
from scalping_strategy import (
    downsample_5m_to_15m, downsample_5m_to_1h, compute_all_indicators,
    mode_f_long, mode_f_short, mode_h_long, mode_h_short,
    run_scalp_backtest, analyze_results,
)


def sweep_mode(close, high, low, vol, ts, mode_name, long_fn, short_fn,
               risk_vals, sl_vals, tp_vals, tr_a_vals, tr_d_vals,
               cd_vals, pt_vals, be_vals, sess_vals, dyn_vals, max_hold=30,
               precomputed_ind=None):
    """Grid sweep for a single mode with all enhancements."""
    best_score = -999
    best_params = None
    best_result = None
    results = []

    # Pre-compute indicators once for all combos
    if precomputed_ind is None:
        precomputed_ind = compute_all_indicators(close, high, low, vol)

    total = (len(risk_vals) * len(sl_vals) * len(tp_vals) * len(tr_a_vals) *
             len(tr_d_vals) * len(cd_vals) * len(pt_vals) * len(be_vals) *
             len(sess_vals) * len(dyn_vals))
    print(f"  {mode_name}: {total} combos...")

    count = 0
    for risk in risk_vals:
        for sl in sl_vals:
            for tp in tp_vals:
                for tr_a in tr_a_vals:
                    for tr_d in tr_d_vals:
                        for cd in cd_vals:
                            for pt in pt_vals:
                                for be in be_vals:
                                    for sess in sess_vals:
                                        for dyn in dyn_vals:
                                            count += 1
                                            if count % 500 == 0:
                                                print(f"    {count}/{total}...")

                                            bal, tr, eq = run_scalp_backtest(
                                                close, high, low, vol, ts,
                                                cap=10000, risk_pct=risk,
                                                sl_atr=sl, tp_atr=tp,
                                                trail_activate=tr_a, trail_atr=tr_d,
                                                cooldown=cd, max_hold=max_hold, fee=0.0005,
                                                long_fn=long_fn, short_fn=short_fn,
                                                partial_tp_pct=pt, partial_tp_atr=1.0,
                                                be_atr=be, session_filter=sess,
                                                session_start=8, session_end=20,
                                                dynamic_sizing=dyn,
                                                precomputed_ind=precomputed_ind)
                                            if len(tr) < 8:
                                                continue
                                            ret = (bal / 10000 - 1) * 100
                                            wins = [t for t in tr if t["pnl"] > 0]
                                            wr = len(wins) / len(tr) * 100
                                            gp = sum(t["pnl"] for t in wins) if wins else 0
                                            gl = abs(sum(t["pnl"] for t in tr if t["pnl"] <= 0)) or 0.001
                                            pf = gp / gl
                                            eq_a = np.array(eq)
                                            dd = ((np.maximum.accumulate(eq_a) - eq_a) / np.maximum.accumulate(eq_a) * 100).max()
                                            rdd = ret / dd if dd > 0 else 0

                                            results.append((ret, wr, pf, dd, rdd, risk, sl, tp, tr_a, tr_d, cd, pt, be, sess, dyn, len(tr)))

                                            if ret > 0 and pf > 1.0 and rdd > best_score:
                                                best_score = rdd
                                                best_params = (risk, sl, tp, tr_a, tr_d, cd, pt, be, sess, dyn)
                                                best_result = (bal, tr, eq)

    return best_params, best_result, results


async def main():
    from app.services.data_cache import _load_cache

    cache = _load_cache("BTC-USDT", "5m")
    if not cache:
        print("No 5m cache found")
        return

    arr = np.array(cache, dtype=object)
    close_5m = arr[:, 4].astype(float)

    # 1H data
    data_1h = downsample_5m_to_1h(cache)
    arr_1h = np.array(data_1h, dtype=object)
    close_1h = arr_1h[:, 4].astype(float)
    high_1h = arr_1h[:, 2].astype(float)
    low_1h = arr_1h[:, 3].astype(float)
    vol_1h = arr_1h[:, 5].astype(float)
    ts_1h = arr_1h[:, 0]

    # 15m data
    data_15m = downsample_5m_to_15m(cache)
    arr_15m = np.array(data_15m, dtype=object)
    close_15m = arr_15m[:, 4].astype(float)
    high_15m = arr_15m[:, 2].astype(float)
    low_15m = arr_15m[:, 3].astype(float)
    vol_15m = arr_15m[:, 5].astype(float)
    ts_15m = arr_15m[:, 0]

    # Pre-compute indicators for all timeframes
    print("Pre-computing indicators...")
    ind_1h = compute_all_indicators(close_1h, high_1h, low_1h, vol_1h)
    ind_15m = compute_all_indicators(close_15m, high_15m, low_15m, vol_15m)
    print("Done.\n")

    # ═══════════════════════════════════════════════════════════════
    # SWEEP 1: Mode F on 1H — wide grid
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'#'*70}")
    print(f" SWEEP 1: Mode F (OrderBook) on 1H")
    print(f"{'#'*70}")

    bp1, br1, res1 = sweep_mode(
        close_1h, high_1h, low_1h, vol_1h, ts_1h,
        "F: OrderBook 1H", mode_f_long, mode_f_short,
        risk_vals=[0.005, 0.01, 0.015, 0.02],
        sl_vals=[0.8, 1.0, 1.5],
        tp_vals=[1.5, 2.0, 3.0],
        tr_a_vals=[0.3, 0.5],
        tr_d_vals=[0.3, 0.5],
        cd_vals=[8, 12, 20],
        pt_vals=[0.0, 0.3],
        be_vals=[0.0, 0.5],
        sess_vals=[False, True],
        dyn_vals=[False, True],
        precomputed_ind=ind_1h,
    )

    # Top 20 by return
    res1.sort(key=lambda x: x[0], reverse=True)
    print(f"\n Top 20 by Return:")
    print(f" {'risk%':>6} {'SL':>4} {'TP':>4} {'TrA':>4} {'TrD':>4} {'CD':>3} {'PT%':>4} {'BE':>4} {'Sess':>6} {'Dyn':>4} {'#':>4} {'Ret%':>7} {'WR%':>5} {'PF':>5} {'DD%':>5} {'R/D':>5}")
    print(f" {'-'*92}")
    for ret, wr, pf, dd, rdd, risk, sl, tp, tr_a, tr_d, cd, pt, be, sess, dyn, n_tr in res1[:20]:
        sess_s = "8-20" if sess else "all"
        pt_s = f"{pt*100:.0f}%" if pt > 0 else "off"
        be_s = f"{be:.1f}" if be > 0 else "off"
        dyn_s = "on" if dyn else "off"
        print(f"  {risk*100:>5.1f}% {sl:>4.1f} {tp:>4.1f} {tr_a:>4.1f} {tr_d:>4.2f} {cd:>3} {pt_s:>4} {be_s:>4} {sess_s:>6} {dyn_s:>4} {n_tr:>4} {ret:>+6.1f}% {wr:>5.1f}% {pf:>5.2f} {dd:>5.1f}% {rdd:>5.2f}")

    if bp1:
        risk, sl, tp, tr_a, tr_d, cd, pt, be, sess, dyn = bp1
        print(f"\n >>> BEST F: risk={risk*100:.1f}% SL={sl} TP={tp} Trail={tr_a}/{tr_d} CD={cd} Partial={pt*100:.0f}% BE={be} Session={'8-20' if sess else 'all'} Dynamic={dyn}")
        analyze_results(10000, *br1)

    # Top 20 by Profit Factor
    res1_pf = sorted(res1, key=lambda x: x[2], reverse=True)
    print(f"\n Top 20 by Profit Factor:")
    print(f" {'risk%':>6} {'SL':>4} {'TP':>4} {'TrA':>4} {'TrD':>4} {'CD':>3} {'PT%':>4} {'BE':>4} {'Sess':>6} {'Dyn':>4} {'#':>4} {'Ret%':>7} {'WR%':>5} {'PF':>5} {'DD%':>5} {'R/D':>5}")
    print(f" {'-'*92}")
    for ret, wr, pf, dd, rdd, risk, sl, tp, tr_a, tr_d, cd, pt, be, sess, dyn, n_tr in res1_pf[:20]:
        sess_s = "8-20" if sess else "all"
        pt_s = f"{pt*100:.0f}%" if pt > 0 else "off"
        be_s = f"{be:.1f}" if be > 0 else "off"
        dyn_s = "on" if dyn else "off"
        print(f"  {risk*100:>5.1f}% {sl:>4.1f} {tp:>4.1f} {tr_a:>4.1f} {tr_d:>4.2f} {cd:>3} {pt_s:>4} {be_s:>4} {sess_s:>6} {dyn_s:>4} {n_tr:>4} {ret:>+6.1f}% {wr:>5.1f}% {pf:>5.2f} {dd:>5.1f}% {rdd:>5.2f}")

    # ═══════════════════════════════════════════════════════════════
    # SWEEP 2: Mode H on 1H — wide grid
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'#'*70}")
    print(f" SWEEP 2: Mode H (OBV Divergence) on 1H")
    print(f"{'#'*70}")

    bp2, br2, res2 = sweep_mode(
        close_1h, high_1h, low_1h, vol_1h, ts_1h,
        "H: OBV Div 1H", mode_h_long, mode_h_short,
        risk_vals=[0.005, 0.01, 0.015, 0.02],
        sl_vals=[0.8, 1.0, 1.5],
        tp_vals=[1.5, 2.0, 3.0],
        tr_a_vals=[0.3, 0.5],
        tr_d_vals=[0.3, 0.5],
        cd_vals=[5, 8, 12],
        pt_vals=[0.0, 0.3],
        be_vals=[0.0, 0.5],
        sess_vals=[False, True],
        dyn_vals=[False, True],
        precomputed_ind=ind_1h,
    )

    res2.sort(key=lambda x: x[0], reverse=True)
    print(f"\n Top 20 by Return:")
    print(f" {'risk%':>6} {'SL':>4} {'TP':>4} {'TrA':>4} {'TrD':>4} {'CD':>3} {'PT%':>4} {'BE':>4} {'Sess':>6} {'Dyn':>4} {'#':>4} {'Ret%':>7} {'WR%':>5} {'PF':>5} {'DD%':>5} {'R/D':>5}")
    print(f" {'-'*92}")
    for ret, wr, pf, dd, rdd, risk, sl, tp, tr_a, tr_d, cd, pt, be, sess, dyn, n_tr in res2[:20]:
        sess_s = "8-20" if sess else "all"
        pt_s = f"{pt*100:.0f}%" if pt > 0 else "off"
        be_s = f"{be:.1f}" if be > 0 else "off"
        dyn_s = "on" if dyn else "off"
        print(f"  {risk*100:>5.1f}% {sl:>4.1f} {tp:>4.1f} {tr_a:>4.1f} {tr_d:>4.2f} {cd:>3} {pt_s:>4} {be_s:>4} {sess_s:>6} {dyn_s:>4} {n_tr:>4} {ret:>+6.1f}% {wr:>5.1f}% {pf:>5.2f} {dd:>5.1f}% {rdd:>5.2f}")

    if bp2:
        risk, sl, tp, tr_a, tr_d, cd, pt, be, sess, dyn = bp2
        print(f"\n >>> BEST H: risk={risk*100:.1f}% SL={sl} TP={tp} Trail={tr_a}/{tr_d} CD={cd} Partial={pt*100:.0f}% BE={be} Session={'8-20' if sess else 'all'} Dynamic={dyn}")
        analyze_results(10000, *br2)

    # ═══════════════════════════════════════════════════════════════
    # SWEEP 3: Mode F on 15m — wide grid (previously +10.2%)
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'#'*70}")
    print(f" SWEEP 3: Mode F (OrderBook) on 15m")
    print(f"{'#'*70}")

    bp3, br3, res3 = sweep_mode(
        close_15m, high_15m, low_15m, vol_15m, ts_15m,
        "F: OrderBook 15m", mode_f_long, mode_f_short,
        risk_vals=[0.005, 0.01, 0.015, 0.02],
        sl_vals=[0.8, 1.0, 1.5],
        tp_vals=[1.5, 2.0, 3.0],
        tr_a_vals=[0.3, 0.5],
        tr_d_vals=[0.3, 0.5],
        cd_vals=[12, 20, 30],
        pt_vals=[0.0, 0.3],
        be_vals=[0.0, 0.5],
        sess_vals=[False, True],
        dyn_vals=[False, True],
        precomputed_ind=ind_15m,
    )

    res3.sort(key=lambda x: x[0], reverse=True)
    print(f"\n Top 20 by Return:")
    print(f" {'risk%':>6} {'SL':>4} {'TP':>4} {'TrA':>4} {'TrD':>4} {'CD':>3} {'PT%':>4} {'BE':>4} {'Sess':>6} {'Dyn':>4} {'#':>4} {'Ret%':>7} {'WR%':>5} {'PF':>5} {'DD%':>5} {'R/D':>5}")
    print(f" {'-'*92}")
    for ret, wr, pf, dd, rdd, risk, sl, tp, tr_a, tr_d, cd, pt, be, sess, dyn, n_tr in res3[:20]:
        sess_s = "8-20" if sess else "all"
        pt_s = f"{pt*100:.0f}%" if pt > 0 else "off"
        be_s = f"{be:.1f}" if be > 0 else "off"
        dyn_s = "on" if dyn else "off"
        print(f"  {risk*100:>5.1f}% {sl:>4.1f} {tp:>4.1f} {tr_a:>4.1f} {tr_d:>4.2f} {cd:>3} {pt_s:>4} {be_s:>4} {sess_s:>6} {dyn_s:>4} {n_tr:>4} {ret:>+6.1f}% {wr:>5.1f}% {pf:>5.2f} {dd:>5.1f}% {rdd:>5.2f}")

    if bp3:
        risk, sl, tp, tr_a, tr_d, cd, pt, be, sess, dyn = bp3
        print(f"\n >>> BEST F 15m: risk={risk*100:.1f}% SL={sl} TP={tp} Trail={tr_a}/{tr_d} CD={cd} Partial={pt*100:.0f}% BE={be} Session={'8-20' if sess else 'all'} Dynamic={dyn}")
        analyze_results(10000, *br3)

    # ═══════════════════════════════════════════════════════════════
    # FINAL SUMMARY
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'#'*70}")
    print(f" FINAL BEST RESULTS ACROSS ALL SWEEPS")
    print(f"{'#'*70}")

    all_bests = []
    if bp1 and br1:
        bal, tr, eq = br1
        ret = (bal / 10000 - 1) * 100
        wins = [t for t in tr if t["pnl"] > 0]
        wr = len(wins) / len(tr) * 100
        gp = sum(t["pnl"] for t in wins) if wins else 0
        gl = abs(sum(t["pnl"] for t in tr if t["pnl"] <= 0)) or 0.001
        pf = gp / gl
        eq_a = np.array(eq)
        dd = ((np.maximum.accumulate(eq_a) - eq_a) / np.maximum.accumulate(eq_a) * 100).max()
        all_bests.append(("F: OrderBook 1H", ret, wr, pf, dd, len(tr), bp1))

    if bp2 and br2:
        bal, tr, eq = br2
        ret = (bal / 10000 - 1) * 100
        wins = [t for t in tr if t["pnl"] > 0]
        wr = len(wins) / len(tr) * 100
        gp = sum(t["pnl"] for t in wins) if wins else 0
        gl = abs(sum(t["pnl"] for t in tr if t["pnl"] <= 0)) or 0.001
        pf = gp / gl
        eq_a = np.array(eq)
        dd = ((np.maximum.accumulate(eq_a) - eq_a) / np.maximum.accumulate(eq_a) * 100).max()
        all_bests.append(("H: OBV Div 1H", ret, wr, pf, dd, len(tr), bp2))

    if bp3 and br3:
        bal, tr, eq = br3
        ret = (bal / 10000 - 1) * 100
        wins = [t for t in tr if t["pnl"] > 0]
        wr = len(wins) / len(tr) * 100
        gp = sum(t["pnl"] for t in wins) if wins else 0
        gl = abs(sum(t["pnl"] for t in tr if t["pnl"] <= 0)) or 0.001
        pf = gp / gl
        eq_a = np.array(eq)
        dd = ((np.maximum.accumulate(eq_a) - eq_a) / np.maximum.accumulate(eq_a) * 100).max()
        all_bests.append(("F: OrderBook 15m", ret, wr, pf, dd, len(tr), bp3))

    all_bests.sort(key=lambda x: x[1], reverse=True)
    print(f"\n {'Strategy':<25} {'Ret%':>7} {'#':>4} {'WR%':>5} {'PF':>5} {'DD%':>5}")
    print(f" {'-'*58}")
    for name, ret, wr, pf, dd, n_tr, params in all_bests:
        print(f" {name:<25} {ret:>+6.1f}% {n_tr:>4} {wr:>5.1f}% {pf:>5.2f} {dd:>5.1f}%")

    if all_bests:
        name, ret, wr, pf, dd, n_tr, params = all_bests[0]
        print(f"\n >>> OVERALL BEST: {name}")
        risk, sl, tp, tr_a, tr_d, cd, pt, be, sess, dyn = params
        print(f"    risk={risk*100:.1f}% SL={sl} TP={tp} Trail={tr_a}/{tr_d} CD={cd} Partial={pt*100:.0f}% BE={be} Session={'8-20' if sess else 'all'} Dynamic={dyn}")
        # Use stored best result
        if name.startswith("F: OrderBook 1H") and br1:
            analyze_results(10000, *br1)
        elif name.startswith("H: OBV") and br2:
            analyze_results(10000, *br2)
        elif name.startswith("F: OrderBook 15m") and br3:
            analyze_results(10000, *br3)


if __name__ == "__main__":
    asyncio.run(main())
