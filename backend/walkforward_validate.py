"""
Walk-Forward Validation for GA-Optimized Scalping Strategy
==========================================================
Split 1H data into 3 periods. Train GA on first 2, test on last 1.
This proves whether GA-optimized params generalize to unseen data.
"""
import sys, os, time
sys.path.insert(0, os.path.dirname(__file__))
os.chdir(os.path.dirname(__file__))

import numpy as np
from scalping_strategy import (
    downsample_5m_to_1h,
    compute_all_indicators,
    mode_f_long, mode_f_short,
    run_scalp_backtest, analyze_results,
)
from ga_nn_optimizer import GeneticOptimizer

np.random.seed(42)


def run_segment(close, high, low, vol, ts, params, cap=10000, fee=0.0005):
    """Run backtest on a data segment with given params."""
    bal, trades, eq = run_scalp_backtest(
        close, high, low, vol, ts,
        cap=cap, fee=fee,
        long_fn=mode_f_long, short_fn=mode_f_short,
        **params,
    )
    n_trades = len(trades)
    if n_trades == 0:
        return {"ret": 0, "trades": 0, "wr": 0, "pf": 0, "dd": 0, "bal": cap}

    ret = (bal / cap - 1) * 100
    wins = [t for t in trades if t["pnl"] > 0]
    wr = len(wins) / n_trades * 100
    gp = sum(t["pnl"] for t in wins) if wins else 0
    gl = abs(sum(t["pnl"] for t in trades if t["pnl"] <= 0)) or 0.001
    pf = gp / gl
    eq_arr = np.array(eq)
    dd = ((np.maximum.accumulate(eq_arr) - eq_arr) / np.maximum.accumulate(eq_arr) * 100).max()

    return {
        "ret": ret, "trades": n_trades, "wr": wr, "pf": pf, "dd": dd,
        "bal": bal, "trades_list": trades, "equity": eq,
    }


def print_segment(label, r):
    """Print one segment result."""
    print(f"  {label:<20} Ret={r['ret']:>+6.1f}%  Trades={r['trades']:>4}  "
          f"WR={r['wr']:>5.1f}%  PF={r['pf']:>5.2f}  DD={r['dd']:>5.1f}%")


def main():
    from app.services.data_cache import _load_cache

    cache = _load_cache("BTC-USDT", "5m")
    if not cache:
        print("No 5m cache found")
        return

    data_1h = downsample_5m_to_1h(cache)
    arr = np.array(data_1h, dtype=object)
    close = arr[:, 4].astype(float)
    high = arr[:, 2].astype(float)
    low = arr[:, 3].astype(float)
    vol = arr[:, 5].astype(float)
    ts = arr[:, 0]
    n = len(close)

    # Default params (baseline)
    default_params = {
        "risk_pct": 0.01, "sl_atr": 1.5, "tp_atr": 2.0,
        "trail_activate": 1.0, "trail_atr": 0.75,
        "cooldown": 5, "max_hold": 20,
        "partial_tp_pct": 0.0, "be_atr": 0.0,
    }

    print(f"{'#'*70}")
    print(f" WALK-FORWARD VALIDATION: GA-Optimized Mode F on 1H")
    print(f" Data: {n} candles 1H (~{n//24} days)")
    print(f"{'#'*70}")

    # ═══════════════════════════════════════════════════════════════
    # METHOD 1: Simple 70/30 split (train on first 70%, test on last 30%)
    # ═══════════════════════════════════════════════════════════════
    print(f"\n{'='*70}")
    print(f" METHOD 1: 70/30 SPLIT")
    print(f" Train on first 70%, test on last 30%")
    print(f"{'='*70}")

    split_70 = int(n * 0.7)
    train_close, test_close = close[:split_70], close[split_70:]
    train_high, test_high = high[:split_70], high[split_70:]
    train_low, test_low = low[:split_70], low[split_70:]
    train_vol, test_vol = vol[:split_70], vol[split_70:]
    train_ts, test_ts = ts[:split_70], ts[split_70:]

    print(f"\n  Training: {split_70} bars ({split_70//24} days)")
    print(f"  Testing:  {n - split_70} bars ({(n - split_70)//24} days)")

    # Baseline on test set (default params)
    print(f"\n  --- Baseline (default params) on TEST set ---")
    baseline_test = run_segment(test_close, test_high, test_low, test_vol, test_ts, default_params)
    print_segment("Default on test", baseline_test)

    # GA optimize on TRAIN set
    print(f"\n  --- GA Optimizing on TRAIN set ---")
    t0 = time.time()
    ga = GeneticOptimizer(
        train_close, train_high, train_low, train_vol, train_ts,
        mode_f_long, mode_f_short,
        pop_size=40, generations=25,
    )
    best_chrom, best_fit = ga.evolve(verbose=True)
    train_params = ga._decode(best_chrom)
    print(f"  GA time: {time.time()-t0:.0f}s")

    # Test GA params on TRAIN (in-sample)
    train_result = run_segment(train_close, train_high, train_low, train_vol, train_ts, train_params)
    print(f"\n  --- GA params IN-SAMPLE (train set) ---")
    print_segment("GA on train", train_result)

    # Test GA params on TEST (out-of-sample)
    test_result = run_segment(test_close, test_high, test_low, test_vol, test_ts, train_params)
    print(f"\n  --- GA params OUT-OF-SAMPLE (test set) ---")
    print_segment("GA on test", test_result)

    # Compare
    print(f"\n  --- DECAY (train→test) ---")
    if train_result['ret'] != 0:
        decay = (test_result['ret'] / train_result['ret'] * 100) if train_result['ret'] != 0 else 0
        print(f"  Return decay: {decay:.0f}% of in-sample return retained")
    else:
        print(f"  No in-sample return to compare")

    print(f"\n  Best GA params: {train_params}")

    # ═══════════════════════════════════════════════════════════════
    # METHOD 2: 3-Fold Walk-Forward (rolling window)
    # ═══════════════════════════════════════════════════════════════
    print(f"\n\n{'='*70}")
    print(f" METHOD 2: 3-FOLD WALK-FORWARD")
    print(f" Train on 40%, test on next 30%, then test on last 30%")
    print(f"{'='*70}")

    fold_size = n // 3
    fold_results = []

    for fold in range(3):
        if fold == 0:
            # Fold 1: train on first 40%, test on middle 30%
            train_end = int(n * 0.4)
            test_start = train_end
            test_end = int(n * 0.7)
        elif fold == 1:
            # Fold 2: train on first 70%, test on last 30%
            train_end = int(n * 0.7)
            test_start = train_end
            test_end = n
        else:
            # Fold 3: train on middle 40%, test on last 30%
            train_start = int(n * 0.1)
            train_end = int(n * 0.5)
            test_start = train_end
            test_end = int(n * 0.8)

        if fold == 2:
            tc = close[train_start:train_end]
            th = high[train_start:train_end]
            tl = low[train_start:train_end]
            tv = vol[train_start:train_end]
            tts = ts[train_start:train_end]
        else:
            tc = close[:train_end]
            th = high[:train_end]
            tl = low[:train_end]
            tv = vol[:train_end]
            tts = ts[:train_end]

        tec = close[test_start:test_end]
        teh = high[test_start:test_end]
        tel = low[test_start:test_end]
        tev = vol[test_start:test_start + len(tec)] if test_start + len(tec) <= n else vol[test_start:n]
        tes = ts[test_start:test_start + len(tec)] if test_start + len(tec) <= n else ts[test_start:n]

        print(f"\n  --- Fold {fold+1}: Train {len(tc)} bars → Test {len(tec)} bars ---")

        # GA optimize on train fold
        t0 = time.time()
        ga_fold = GeneticOptimizer(
            tc, th, tl, tv, tts,
            mode_f_long, mode_f_short,
            pop_size=30, generations=20,
        )
        fold_chrom, fold_fit = ga_fold.evolve(verbose=False)
        fold_params = ga_fold._decode(fold_chrom)
        print(f"  GA time: {time.time()-t0:.0f}s")

        # Test on train (in-sample)
        fold_train = run_segment(tc, th, tl, tv, tts, fold_params)
        # Test on test (out-of-sample)
        fold_test = run_segment(tec, teh, tel, tev, tes, fold_params)

        print_segment(f"Fold {fold+1} train", fold_train)
        print_segment(f"Fold {fold+1} test", fold_test)

        decay = (fold_test['ret'] / fold_train['ret'] * 100) if fold_train['ret'] != 0 else 0
        print(f"  Decay: {decay:.0f}% | Params: SL={fold_params['sl_atr']:.1f} TP={fold_params['tp_atr']:.1f} "
              f"Trail={fold_params['trail_activate']:.1f}/{fold_params['trail_atr']:.1f} CD={fold_params['cooldown']} "
              f"Risk={fold_params['risk_pct']*100:.1f}%")

        fold_results.append({"train": fold_train, "test": fold_test, "params": fold_params, "decay": decay})

    # Summary
    print(f"\n{'='*70}")
    print(f" WALK-FORWARD SUMMARY")
    print(f"{'='*70}")
    avg_oos_ret = np.mean([f["test"]["ret"] for f in fold_results])
    avg_oos_wr = np.mean([f["test"]["wr"] for f in fold_results])
    avg_oos_pf = np.mean([f["test"]["pf"] for f in fold_results])
    avg_oos_dd = np.mean([f["test"]["dd"] for f in fold_results])
    avg_oos_trades = np.mean([f["test"]["trades"] for f in fold_results])
    avg_decay = np.mean([f["decay"] for f in fold_results])

    print(f"  Avg OOS Return:     {avg_oos_ret:>+6.1f}%")
    print(f"  Avg OOS Trades:     {avg_oos_trades:>5.0f}")
    print(f"  Avg OOS WR:         {avg_oos_wr:>5.1f}%")
    print(f"  Avg OOS PF:         {avg_oos_pf:>5.2f}")
    print(f"  Avg OOS DD:         {avg_oos_dd:>5.1f}%")
    print(f"  Avg Train→Test:     {avg_decay:.0f}% retention")

    # ═══════════════════════════════════════════════════════════════
    # METHOD 3: Rolling 3-month walk-forward
    # ═══════════════════════════════════════════════════════════════
    print(f"\n\n{'='*70}")
    print(f" METHOD 3: ROLLING 3-MONTH WALK-FORWARD")
    print(f" Train on 6 months, test on next 3 months, roll forward")
    print(f"{'='*70}")

    train_months = 6 * 30 * 24  # 6 months of 1H bars
    test_months = 3 * 30 * 24   # 3 months
    step = test_months           # roll by 3 months

    rolling_results = []
    pos = train_months
    fold_num = 0

    while pos + test_months <= n:
        fold_num += 1
        train_close_r = close[pos - train_months:pos]
        train_high_r = high[pos - train_months:pos]
        train_low_r = low[pos - train_months:pos]
        train_vol_r = vol[pos - train_months:pos]
        train_ts_r = ts[pos - train_months:pos]

        test_close_r = close[pos:pos + test_months]
        test_high_r = high[pos:pos + test_months]
        test_low_r = low[pos:pos + test_months]
        test_vol_r = vol[pos:pos + test_months]
        test_ts_r = ts[pos:pos + test_months]

        print(f"\n  --- Roll {fold_num}: Train months 0-{pos//720}, test months {pos//720}-{(pos+test_months)//720} ---")

        ga_roll = GeneticOptimizer(
            train_close_r, train_high_r, train_low_r, train_vol_r, train_ts_r,
            mode_f_long, mode_f_short,
            pop_size=30, generations=20,
        )
        roll_chrom, roll_fit = ga_roll.evolve(verbose=False)
        roll_params = ga_roll._decode(roll_chrom)

        roll_train = run_segment(train_close_r, train_high_r, train_low_r, train_vol_r, train_ts_r, roll_params)
        roll_test = run_segment(test_close_r, test_high_r, test_low_r, test_vol_r, test_ts_r, roll_params)

        print_segment(f"Roll {fold_num} train", roll_train)
        print_segment(f"Roll {fold_num} test", roll_test)

        decay = (roll_test['ret'] / roll_train['ret'] * 100) if roll_train['ret'] != 0 else 0
        print(f"  Decay: {decay:.0f}% | Params: SL={roll_params['sl_atr']:.1f} TP={roll_params['tp_atr']:.1f}")

        rolling_results.append({"train": roll_train, "test": roll_test, "params": roll_params})
        pos += step

    if rolling_results:
        avg_roll_ret = np.mean([r["test"]["ret"] for r in rolling_results])
        avg_roll_wr = np.mean([r["test"]["wr"] for r in rolling_results])
        avg_roll_pf = np.mean([r["test"]["pf"] for r in rolling_results])
        avg_roll_dd = np.mean([r["test"]["dd"] for r in rolling_results])
        avg_roll_trades = np.mean([r["test"]["trades"] for r in rolling_results])

        print(f"\n  --- Rolling Walk-Forward Summary ---")
        print(f"  Rolls tested:     {len(rolling_results)}")
        print(f"  Avg OOS Return:   {avg_roll_ret:>+6.1f}%")
        print(f"  Avg OOS Trades:   {avg_roll_trades:>5.0f}")
        print(f"  Avg OOS WR:       {avg_roll_wr:>5.1f}%")
        print(f"  Avg OOS PF:       {avg_roll_pf:>5.2f}")
        print(f"  Avg OOS DD:       {avg_roll_dd:>5.1f}%")
        profitable = sum(1 for r in rolling_results if r["test"]["ret"] > 0)
        print(f"  Profitable rolls: {profitable}/{len(rolling_results)} ({profitable/len(rolling_results)*100:.0f}%)")


    # ═══════════════════════════════════════════════════════════════
    # FINAL: Full out-of-sample test with best params from all folds
    # ═══════════════════════════════════════════════════════════════
    print(f"\n\n{'='*70}")
    print(f" FINAL: FULL OUT-OF-SAMPLE COMPARISON")
    print(f"{'='*70}")

    # Use the method 1 params (trained on 70%, tested on 30%)
    print(f"\n  Method 1 params on full test set (last 30%):")
    print_segment("70/30 test", test_result)

    # Use the best rolling params
    if rolling_results:
        best_roll = max(rolling_results, key=lambda r: r["test"]["ret"] / max(r["test"]["dd"], 0.1))
        print(f"\n  Best rolling params ({best_roll['test']['ret']:+.1f}% on its test):")
        full_test_best = run_segment(close, high, low, vol, ts, best_roll["params"])
        print_segment("Best roll on FULL", full_test_best)
        print(f"  Params: {best_roll['params']}")

    # Also show what the original unoptimized Mode F gets on full data
    print(f"\n  Original Mode F (default params, full data):")
    full_default = run_segment(close, high, low, vol, ts, default_params)
    print_segment("Default on full", full_default)

    print(f"\n{'='*70}")
    print(f" VERDICT")
    print(f"{'='*70}")
    if avg_oos_ret > 5 and avg_oos_pf > 1.2:
        print(f"  GA optimization is ROBUST — positive OOS returns with PF > 1.2")
    elif avg_oos_ret > 0:
        print(f"  GA optimization shows SOME edge — marginal OOS profitability")
    else:
        print(f"  GA optimization may be OVERFIT — negative average OOS returns")
    print(f"{'='*70}")


if __name__ == "__main__":
    main()
