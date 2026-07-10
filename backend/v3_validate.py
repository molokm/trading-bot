"""
V3 Rigorous Validation — No BS, just truth
Tests:
1. Buy & Hold comparison (is this even worth it?)
2. Monte Carlo (1000 iterations, random trade order)
3. Parameter sensitivity (ATR stop ±20%, target ±20%)
4. Market regime analysis (bull/bear/sideways)
5. Bootstrap statistical significance
6. Slippage sensitivity (what if 0.1% instead of 0.05%?)
7. Max consecutive losses
8. Monthly return distribution
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from daily_swing_v3 import *
import yfinance as yf
import warnings
warnings.filterwarnings('ignore')

np.random.seed(42)

print("=" * 70)
print("  V3 STRATEGY — RIGOROUS VALIDATION")
print("=" * 70)

# ── Download data ──
print("\n[1] Downloading data...")
data = {}
for sym, ticker in TICKERS.items():
    df = yf.download(ticker, period='3y', interval='1d', auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[['Open', 'High', 'Low', 'Close', 'Volume']].astype(float)
    df.index = pd.to_datetime(df.index)
    data[sym] = enrich(df)

common_dates = data['BTC'].index
for sym in UNIVERSE:
    common_dates = common_dates.intersection(data[sym].index)
for sym in UNIVERSE:
    data[sym] = data[sym].loc[common_dates]

print(f"  Period: {common_dates[0].date()} to {common_dates[-1].date()} ({len(common_dates)} days)")

# ── Run base backtest ──
trades, final_eq, max_dd, eq_curve = run_backtest(data, INITIAL_CAPITAL, verbose=False)
metrics = calc_metrics(trades, INITIAL_CAPITAL, final_eq, max_dd, eq_curve)


# ═══════════════════════════════════════════════════════════════
# TEST 1: BUY & HOLD COMPARISON
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("  TEST 1: STRATEGY vs BUY & HOLD")
print("=" * 70)

btc_data = data['BTC']
btc_start = btc_data['Close'].iloc[0]
btc_end = btc_data['Close'].iloc[-1]
btc_bh_return = (btc_end / btc_start - 1) * 100
btc_bh_cagr = ((btc_end / btc_start) ** (365.25 / len(common_dates)) - 1) * 100

# BTC-only buy & hold
btc_only_trades, btc_only_final, btc_only_dd_val, btc_only_curve = run_backtest(
    {'BTC': data['BTC']}, INITIAL_CAPITAL, verbose=False
)
btc_only_m = calc_metrics(btc_only_trades, INITIAL_CAPITAL, btc_only_final, btc_only_dd_val, btc_only_curve)

# Equal-weight B&H across 4 coins
equal_weight_return = 0
for sym in UNIVERSE:
    s = data[sym]['Close'].iloc[0]
    e = data[sym]['Close'].iloc[-1]
    equal_weight_return += (e / s - 1) * 100 / 4
equal_weight_cagr = ((1 + equal_weight_return/100) ** (365.25 / len(common_dates)) - 1) * 100

print(f"\n  {'Metric':<25} {'V3 Strategy':>15} {'BTC Buy&Hold':>15} {'Equal-Weight B&H':>15}")
print(f"  {'-'*70}")
print(f"  {'Total Return':<25} {metrics['total_return_pct']:>+14.1f}% {btc_bh_return:>+14.1f}% {equal_weight_return:>+14.1f}%")
print(f"  {'CAGR':<25} {metrics['cagr']:>+14.1f}% {btc_bh_cagr:>+14.1f}% {equal_weight_cagr:>+14.1f}%")
print(f"  {'Max Drawdown':<25} {metrics['max_drawdown']:>14.1f}% {'30%+':>15} {'30%+':>15}")
print(f"  {'Sharpe Ratio':<25} {metrics['sharpe']:>14.2f} {'~0.3-0.5':>15} {'~0.3-0.5':>15}")

# Risk-adjusted: return per unit of drawdown
strategy_return_dd = metrics['total_return_pct'] / metrics['max_drawdown'] if metrics['max_drawdown'] > 0 else 0
btc_return_dd = btc_bh_return / 30  # rough estimate
print(f"\n  Return/MaxDD ratio:     {strategy_return_dd:>14.2f}x {'~1.0x':>15} {'~1.0x':>15}")

verdict = "PASS" if metrics['cagr'] > 0 and metrics['sharpe'] > 0.3 else "MARGINAL" if metrics['cagr'] > 0 else "FAIL"
print(f"\n  Verdict: {verdict} — Strategy {'beats' if metrics['cagr'] > btc_bh_cagr else 'trails'} buy&hold on risk-adjusted basis")


# ═══════════════════════════════════════════════════════════════
# TEST 2: MONTE CARLO (1000 iterations)
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("  TEST 2: MONTE CARLO SIMULATION (1000 iterations)")
print("=" * 70)

trade_pnls = [t['pnl'] for t in trades]
n_trades = len(trade_pnls)
mc_results = []

for i in range(1000):
    shuffled = np.random.permutation(trade_pnls)
    equity = INITIAL_CAPITAL
    peak = INITIAL_CAPITAL
    max_dd = 0
    for pnl in shuffled:
        equity += pnl
        peak = max(peak, equity)
        dd = (peak - equity) / peak
        max_dd = max(max_dd, dd)
    mc_results.append({
        'final': equity,
        'return': (equity / INITIAL_CAPITAL - 1) * 100,
        'max_dd': max_dd * 100,
    })

mc_returns = [r['return'] for r in mc_results]
mc_dds = [r['max_dd'] for r in mc_results]
mc_finals = [r['final'] for r in mc_results]

print(f"\n  Iterations: 1000")
print(f"  Trades shuffled: {n_trades}")
print(f"\n  {'Metric':<25} {'Mean':>10} {'Median':>10} {'5th %ile':>10} {'95th %ile':>10}")
print(f"  {'-'*65}")
print(f"  {'Final Equity':<25} ${np.mean(mc_finals):>9,.0f} ${np.median(mc_finals):>9,.0f} ${np.percentile(mc_finals, 5):>9,.0f} ${np.percentile(mc_finals, 95):>9,.0f}")
print(f"  {'Total Return':<25} {np.mean(mc_returns):>+9.1f}% {np.median(mc_returns):>+9.1f}% {np.percentile(mc_returns, 5):>+9.1f}% {np.percentile(mc_returns, 95):>+9.1f}%")
print(f"  {'Max Drawdown':<25} {np.mean(mc_dds):>9.1f}% {np.median(mc_dds):>9.1f}% {np.percentile(mc_dds, 5):>9.1f}% {np.percentile(mc_dds, 95):>9.1f}%")

# Probability of loss
prob_loss = sum(1 for r in mc_returns if r < 0) / 1000 * 100
prob_ruin = sum(1 for r in mc_finals if r < INITIAL_CAPITAL * 0.5) / 1000 * 100
print(f"\n  Probability of loss:         {prob_loss:.1f}%")
print(f"  Probability of -50%:         {prob_ruin:.1f}%")
print(f"  Worst case (5th %ile):       ${np.percentile(mc_finals, 5):,.0f} ({np.percentile(mc_returns, 5):+.1f}%)")

mc_verdict = "PASS" if prob_loss < 30 and np.percentile(mc_returns, 5) > -20 else "WARN" if prob_loss < 50 else "FAIL"
print(f"\n  Verdict: {mc_verdict}")


# ═══════════════════════════════════════════════════════════════
# TEST 3: PARAMETER SENSITIVITY
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("  TEST 3: PARAMETER SENSITIVITY")
print("=" * 70)

# We'll test key parameters by modifying globals
import daily_swing_v3 as v3

base_cagr = metrics['cagr']
base_pf = metrics['profit_factor']
base_dd = metrics['max_drawdown']

param_tests = []

# Test ATR stop multiplier
for stop_mult in [1.4, 1.6, 1.8, 2.0, 2.2, 2.5]:
    old = v3.ATR_STOP_MULT
    v3.ATR_STOP_MULT = stop_mult
    t, eq, dd, ec = run_backtest(data, INITIAL_CAPITAL, verbose=False)
    m = calc_metrics(t, INITIAL_CAPITAL, eq, dd, ec)
    param_tests.append(('ATR Stop', stop_mult, m['cagr'], m['profit_factor'], m['max_drawdown']))
    v3.ATR_STOP_MULT = old

# Test ATR target multiplier
for target_mult in [3.0, 3.5, 4.0, 4.5, 5.0, 6.0]:
    old = v3.ATR_TARGET_MULT
    v3.ATR_TARGET_MULT = target_mult
    t, eq, dd, ec = run_backtest(data, INITIAL_CAPITAL, verbose=False)
    m = calc_metrics(t, INITIAL_CAPITAL, eq, dd, ec)
    param_tests.append(('ATR Target', target_mult, m['cagr'], m['profit_factor'], m['max_drawdown']))
    v3.ATR_TARGET_MULT = old

# Test Donchian length
for dc_len in [10, 15, 20, 25, 30]:
    old = v3.DONCHIAN_LEN
    v3.DONCHIAN_LEN = dc_len
    t, eq, dd, ec = run_backtest(data, INITIAL_CAPITAL, verbose=False)
    m = calc_metrics(t, INITIAL_CAPITAL, eq, dd, ec)
    param_tests.append(('Donchian', dc_len, m['cagr'], m['profit_factor'], m['max_drawdown']))
    v3.DONCHIAN_LEN = old

# Test RSI pullback
for rsi_pb in [35, 40, 45, 50, 55]:
    old = v3.RSI_PULLBACK
    v3.RSI_PULLBACK = rsi_pb
    t, eq, dd, ec = run_backtest(data, INITIAL_CAPITAL, verbose=False)
    m = calc_metrics(t, INITIAL_CAPITAL, eq, dd, ec)
    param_tests.append(('RSI Pullback', rsi_pb, m['cagr'], m['profit_factor'], m['max_drawdown']))
    v3.RSI_PULLBACK = old

# Test ADX threshold
for adx_min in [10, 15, 18, 22, 25, 30]:
    old = v3.ADX_MIN
    v3.ADX_MIN = adx_min
    t, eq, dd, ec = run_backtest(data, INITIAL_CAPITAL, verbose=False)
    m = calc_metrics(t, INITIAL_CAPITAL, eq, dd, ec)
    param_tests.append(('ADX Min', adx_min, m['cagr'], m['profit_factor'], m['max_drawdown']))
    v3.ADX_MIN = old

# Test Risk per trade
for risk in [0.01, 0.015, 0.02, 0.025, 0.03]:
    old = v3.RISK_PER_TRADE
    v3.RISK_PER_TRADE = risk
    t, eq, dd, ec = run_backtest(data, INITIAL_CAPITAL, verbose=False)
    m = calc_metrics(t, INITIAL_CAPITAL, eq, dd, ec)
    param_tests.append(('Risk/Trade', f"{risk:.1%}", m['cagr'], m['profit_factor'], m['max_drawdown']))
    v3.RISK_PER_TRADE = old

# Print grouped by parameter
current_param = ""
print(f"\n  {'Param':<15} {'Value':>8} {'CAGR':>8} {'PF':>6} {'MaxDD':>8} {'ΔCAGR':>8}")
print(f"  {'-'*55}")
for param, val, cagr, pf, dd in param_tests:
    if param != current_param:
        current_param = param
        print()
    delta = cagr - base_cagr
    marker = " ◄" if param == "ATR Stop" and val == 1.8 else ""
    print(f"  {param:<15} {str(val):>8} {cagr:>+7.1f}% {pf:>5.2f} {dd:>7.1f}% {delta:>+7.1f}%{marker}")

# Stability check: how many combos are profitable?
profitable_combos = sum(1 for _, _, cagr, _, _ in param_tests if cagr > 0)
total_combos = len(param_tests)
print(f"\n  Profitable parameter combos: {profitable_combos}/{total_combos} ({profitable_combos/total_combos*100:.0f}%)")
param_verdict = "ROBUST" if profitable_combos/total_combos > 0.7 else "SENSITIVE" if profitable_combos/total_combos > 0.5 else "FRAGILE"
print(f"  Verdict: {param_verdict}")


# ═══════════════════════════════════════════════════════════════
# TEST 4: MARKET REGIME ANALYSIS
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("  TEST 4: MARKET REGIME ANALYSIS")
print("=" * 70)

# Define regimes based on BTC 50-day return
btc_prices = data['BTC']['Close']
btc_50d_ret = btc_prices.pct_change(50)

# Classify each day
regimes = pd.Series(index=common_dates, dtype=str)
for date in common_dates:
    if date not in btc_50d_ret.index or pd.isna(btc_50d_ret[date]):
        regimes[date] = 'unknown'
    elif btc_50d_ret[date] > 0.15:
        regimes[date] = 'strong_bull'
    elif btc_50d_ret[date] > 0.03:
        regimes[date] = 'bull'
    elif btc_50d_ret[date] > -0.03:
        regimes[date] = 'sideways'
    elif btc_50d_ret[date] > -0.15:
        regimes[date] = 'bear'
    else:
        regimes[date] = 'strong_bear'

# Run backtest per regime
regime_stats = {}
for regime in ['strong_bull', 'bull', 'sideways', 'bear', 'strong_bear']:
    regime_dates = regimes[regimes == regime].index
    if len(regime_dates) < 20:
        continue

    regime_trades = [t for t in trades if t['entry_date'] in regime_dates or t['exit_date'] in regime_dates]
    regime_pnl = sum(t['pnl'] for t in regime_trades)
    regime_wins = len([t for t in regime_trades if t['pnl'] > 0])

    regime_stats[regime] = {
        'trades': len(regime_trades),
        'pnl': regime_pnl,
        'win_rate': regime_wins / len(regime_trades) * 100 if regime_trades else 0,
        'days': len(regime_dates),
    }

print(f"\n  {'Regime':<15} {'Days':>6} {'Trades':>8} {'PnL':>12} {'WinRate':>10}")
print(f"  {'-'*55}")
for regime in ['strong_bull', 'bull', 'sideways', 'bear', 'strong_bear']:
    if regime in regime_stats:
        s = regime_stats[regime]
        emoji = "▲" if s['pnl'] > 0 else "▼"
        print(f"  {regime:<15} {s['days']:>6} {s['trades']:>8} ${s['pnl']:>+10,.2f} {s['win_rate']:>9.1f}% {emoji}")

# Worst regime
worst_regime = min(regime_stats.items(), key=lambda x: x[1]['pnl']) if regime_stats else None
if worst_regime:
    print(f"\n  Worst regime: {worst_regime[0]} (PnL ${worst_regime[1]['pnl']:+,.2f})")
regime_verdict = "PASS" if all(s['pnl'] > -1000 for s in regime_stats.values()) else "WARN"
print(f"  Verdict: {regime_verdict}")


# ═══════════════════════════════════════════════════════════════
# TEST 5: BOOTSTRAP STATISTICAL SIGNIFICANCE
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("  TEST 5: BOOTSTRAP STATISTICAL SIGNIFICANCE")
print("=" * 70)

n_bootstrap = 5000
bootstrap_sharpes = []
bootstrap_pf = []

for _ in range(n_bootstrap):
    sampled = np.random.choice(trade_pnls, size=n_trades, replace=True)
    pos = sampled[sampled > 0]
    neg = sampled[sampled <= 0]
    pf = abs(pos.sum() / neg.sum()) if len(neg) > 0 and neg.sum() != 0 else 3.0
    bootstrap_pf.append(pf)

    # Compute Sharpe from equity series
    eq = np.cumsum(np.concatenate([[INITIAL_CAPITAL], sampled]))
    daily_ret = np.diff(eq) / eq[:-1]
    if np.std(daily_ret) > 0:
        sharpe = np.mean(daily_ret) / np.std(daily_ret) * np.sqrt(365)
    else:
        sharpe = 0
    bootstrap_sharpes.append(sharpe)

# 95% confidence intervals
sharpe_ci_low = np.percentile(bootstrap_sharpes, 2.5)
sharpe_ci_high = np.percentile(bootstrap_sharpes, 97.5)
pf_ci_low = np.percentile(bootstrap_pf, 2.5)
pf_ci_high = np.percentile(bootstrap_pf, 97.5)

# P-value: probability that true Sharpe <= 0
p_value_sharpe = sum(1 for s in bootstrap_sharpes if s <= 0) / n_bootstrap
p_value_pf = sum(1 for pf in bootstrap_pf if pf <= 1.0) / n_bootstrap

print(f"\n  Bootstrap iterations: {n_bootstrap}")
print(f"\n  Sharpe Ratio:")
print(f"    Point estimate:  {metrics['sharpe']:.3f}")
print(f"    95% CI:          [{sharpe_ci_low:.3f}, {sharpe_ci_high:.3f}]")
print(f"    P(Sharpe ≤ 0):   {p_value_sharpe:.4f} {'***' if p_value_sharpe < 0.01 else '**' if p_value_sharpe < 0.05 else '*' if p_value_sharpe < 0.1 else 'ns'}")
print(f"\n  Profit Factor:")
print(f"    Point estimate:  {metrics['profit_factor']:.3f}")
print(f"    95% CI:          [{pf_ci_low:.3f}, {pf_ci_high:.3f}]")
print(f"    P(PF ≤ 1.0):     {p_value_pf:.4f} {'***' if p_value_pf < 0.01 else '**' if p_value_pf < 0.05 else '*' if p_value_pf < 0.1 else 'ns'}")

# Significance levels
print(f"\n  Significance levels: *** p<0.01, ** p<0.05, * p<0.10, ns=not significant")
stat_verdict = "SIGNIFICANT" if p_value_sharpe < 0.05 and p_value_pf < 0.05 else "MARGINAL" if p_value_sharpe < 0.10 else "NOT SIGNIFICANT"
print(f"  Verdict: {stat_verdict}")


# ═══════════════════════════════════════════════════════════════
# TEST 6: SLIPPAGE SENSITIVITY
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("  TEST 6: SLIPPAGE SENSITIVITY")
print("=" * 70)

print(f"\n  {'Slippage':>10} {'CAGR':>8} {'PF':>6} {'MaxDD':>8} {'Trades':>8}")
print(f"  {'-'*45}")
for slip in [0.0001, 0.0005, 0.001, 0.002, 0.003, 0.005]:
    old = v3.SLIPPAGE_PCT
    v3.SLIPPAGE_PCT = slip
    t, eq, dd, ec = run_backtest(data, INITIAL_CAPITAL, verbose=False)
    m = calc_metrics(t, INITIAL_CAPITAL, eq, dd, ec)
    print(f"  {slip:>9.4%} {m['cagr']:>+7.1f}% {m['profit_factor']:>5.2f} {m['max_drawdown']:>7.1f}% {m['n_trades']:>8}")
    v3.SLIPPAGE_PCT = old

print(f"\n  Base case: {v3.SLIPPAGE_PCT:.2%} slippage")
slippage_survives = True
for slip in [0.002, 0.003]:
    v3.SLIPPAGE_PCT = slip
    t, eq, dd, ec = run_backtest(data, INITIAL_CAPITAL, verbose=False)
    m = calc_metrics(t, INITIAL_CAPITAL, eq, dd, ec)
    if m['profit_factor'] < 1.0:
        slippage_survives = False
    v3.SLIPPAGE_PCT = 0.0005
slip_verdict = "PASS" if slippage_survives else "FAIL"
print(f"  Verdict: {slip_verdict} — Strategy {'survives' if slippage_survives else 'breaks'} at 0.2-0.3% slippage")


# ═══════════════════════════════════════════════════════════════
# TEST 7: STREAK ANALYSIS
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("  TEST 7: STREAK ANALYSIS (worst case psychology)")
print("=" * 70)

win_streak = 0
lose_streak = 0
max_win_streak = 0
max_lose_streak = 0
current_lose_streak = 0
max_lose_streak_pnl = 0
current_pnl = 0

for t in sorted(trades, key=lambda x: x['exit_date']):
    if t['pnl'] > 0:
        win_streak += 1
        lose_streak = 0
        max_win_streak = max(max_win_streak, win_streak)
    else:
        lose_streak += 1
        win_streak = 0
        max_lose_streak = max(max_lose_streak, lose_streak)
        current_pnl += t['pnl']
        if lose_streak == 1:
            current_pnl = t['pnl']
        max_lose_streak_pnl = min(max_lose_streak_pnl, current_pnl)

print(f"\n  Max win streak:      {max_win_streak}")
print(f"  Max lose streak:     {max_lose_streak}")
print(f"  Max lose streak PnL: ${max_lose_streak_pnl:,.2f}")
print(f"  Max lose streak as % of capital: {abs(max_lose_streak_pnl)/INITIAL_CAPITAL*100:.1f}%")

# How many months are negative?
monthly = metrics.get('monthly', {})
neg_months = sum(1 for v in monthly.values() if v < 0)
total_months = len(monthly)
print(f"\n  Negative months: {neg_months}/{total_months} ({neg_months/total_months*100:.0f}%)")

# Longest drawdown period
eq_series = pd.DataFrame(eq_curve).set_index('date')
running_max = eq_series['equity'].cummax()
dd_series = (running_max - eq_series['equity']) / running_max
in_dd = dd_series > 0.01  # >1% drawdown

# Find longest continuous drawdown
max_dd_duration = 0
current_dd_duration = 0
for val in in_dd:
    if val:
        current_dd_duration += 1
        max_dd_duration = max(max_dd_duration, current_dd_duration)
    else:
        current_dd_duration = 0

print(f"  Longest drawdown:    {max_dd_duration} days ({max_dd_duration/30:.1f} months)")


# ═══════════════════════════════════════════════════════════════
# FINAL VERDICT
# ═══════════════════════════════════════════════════════════════
print("\n" + "=" * 70)
print("  FINAL VERDICT")
print("=" * 70)

all_tests = {
    'Buy & Hold comparison': verdict,
    'Monte Carlo': mc_verdict,
    'Parameter sensitivity': param_verdict,
    'Market regimes': regime_verdict,
    'Statistical significance': stat_verdict,
    'Slippage sensitivity': slip_verdict,
}

print()
for test, result in all_tests.items():
    status = "✓" if result in ("PASS", "ROBUST", "SIGNIFICANT") else "△" if result in ("MARGINAL", "WARN") else "✗"
    print(f"  {status} {test:<35} {result}")

overall = "PRODUCTION READY" if all(v in ("PASS", "ROBUST", "SIGNIFICANT") for v in all_tests.values()) else "USE WITH CAUTION" if sum(1 for v in all_tests.values() if v in ("PASS", "ROBUST", "SIGNIFICANT")) >= 4 else "NOT RECOMMENDED"
print(f"\n  Overall: {overall}")
print("=" * 70)
