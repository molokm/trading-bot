"""Point-in-time backtest lab + PBO (probability of backtest overfitting).

Why this module exists
----------------------
The old ``backtest_service`` was a retired stub and there was no way to answer
"does this exit/entry threshold actually work?" other than waiting weeks of
live ticks. This module is a deliberately small, dependency-free (pandas+numpy)
research harness:

* **Point-in-time**: every signal at bar ``i`` uses only bars ``<= i`` and is
  filled at the *next* bar's open, so there is no look-ahead. Indicators are
  computed on the closed-bar series (same warmup as production).
* **Same cost model as live**: ``FEE_ROUND_PCT`` (0.14% per round trip) plus
  per-side slippage.
* **PBO**: combinatorially symmetric cross-validation (Bailey et al.) over a
  parameter grid. PBO close to 0 means "the config that looked best in-sample
  also ranked well out-of-sample"; high PBO means the grid only fits noise.

It is *not* a replay of the LLM decisions — it validates the deterministic
entry/exit skeleton (trend + ADX + ROC + ATR stop/take), which is what the
threshold knobs in ``AIConfig`` control.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

FEE_ROUND_PCT = 0.0014      # 0.14% per round trip (taker both sides + funding)
SLIPPAGE_PCT = 0.0002       # per side
WARMUP_BARS = 60


@dataclass
class BacktestParams:
    min_adx: float = 22.0
    min_roc: float = 0.25        # |ROC(3)| % move required in trade direction
    atr_stop_mult: float = 1.5
    tp_r: float = 2.0            # take-profit distance in R multiples
    max_hold_bars: int = 24


@dataclass
class BacktestResult:
    params: Dict
    trades: int
    win_rate: float
    net_return_pct: float
    profit_factor: float
    expectancy_pct: float
    max_drawdown_pct: float
    sharpe: float
    block_returns: List[float] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            'params': dict(self.params),
            'trades': self.trades,
            'win_rate': round(self.win_rate, 4),
            'net_return_pct': round(self.net_return_pct, 4),
            'profit_factor': round(self.profit_factor, 4),
            'expectancy_pct': round(self.expectancy_pct, 4),
            'max_drawdown_pct': round(self.max_drawdown_pct, 4),
            'sharpe': round(self.sharpe, 4),
        }


def frame_from_candles(candles: Sequence[Sequence]) -> pd.DataFrame:
    """OKX candles (newest first, strings) -> ascending OHLCV DataFrame."""
    rows = sorted(candles, key=lambda r: int(r[0]))
    df = pd.DataFrame(
        {
            'ts': [int(r[0]) for r in rows],
            'Open': [float(r[1]) for r in rows],
            'High': [float(r[2]) for r in rows],
            'Low': [float(r[3]) for r in rows],
            'Close': [float(r[4]) for r in rows],
            'Volume': [float(r[5]) for r in rows],
        }
    )
    return df


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Add the production indicator set (point-in-time by construction)."""
    from app.orchestrator import adx, calc_atr, ema

    out = df.copy()
    out['EMA_20'] = ema(out['Close'], 20)
    out['EMA_50'] = ema(out['Close'], 50)
    out['ATR_14'] = calc_atr(out, 14)
    adx_val, plus_di, minus_di = adx(out, 14)
    out['ADX_14'] = adx_val
    out['PLUS_DI'] = plus_di
    out['MINUS_DI'] = minus_di
    out['ROC_3'] = out['Close'].pct_change(3) * 100.0
    return out


def _signal(row: pd.Series, p: BacktestParams) -> Optional[str]:
    """Deterministic trend signal on a *closed* bar."""
    try:
        close = float(row['Close'])
        e20 = float(row['EMA_20'])
        e50 = float(row['EMA_50'])
        a = float(row['ADX_14'])
        roc = float(row['ROC_3'])
        pdi = float(row['PLUS_DI'])
        mdi = float(row['MINUS_DI'])
    except (TypeError, ValueError, KeyError):
        return None
    if not all(math.isfinite(v) for v in (close, e20, e50, a, roc, pdi, mdi)):
        return None
    if a < p.min_adx:
        return None
    if close > e50 and e20 > e50 and pdi > mdi and roc >= p.min_roc:
        return 'long'
    if close < e50 and e20 < e50 and mdi > pdi and roc <= -p.min_roc:
        return 'short'
    return None


def run_backtest(df: pd.DataFrame, p: BacktestParams,
                 *, block_count: int = 16) -> BacktestResult:
    """Simulate the deterministic skeleton; returns per-trade + block returns.

    Fills happen at the next bar open; within a bar the stop is assumed to be
    hit before the take-profit (conservative, no intrabar generosity).
    """
    n = len(df)
    trades: List[float] = []
    equity = [0.0]
    i = WARMUP_BARS
    while i < n - 1:
        side = _signal(df.iloc[i], p)
        if side is None:
            i += 1
            continue
        atr = float(df.iloc[i]['ATR_14'] or 0)
        entry = float(df.iloc[i + 1]['Open'])
        if atr <= 0 or entry <= 0:
            i += 1
            continue
        stop_dist = atr * p.atr_stop_mult
        if side == 'long':
            entry_px = entry * (1 + SLIPPAGE_PCT)
            stop = entry_px - stop_dist
            take = entry_px + stop_dist * p.tp_r
        else:
            entry_px = entry * (1 - SLIPPAGE_PCT)
            stop = entry_px + stop_dist
            take = entry_px - stop_dist * p.tp_r

        exit_px = None
        end = min(n - 1, i + p.max_hold_bars)
        for j in range(i + 1, end + 1):
            hi = float(df.iloc[j]['High'])
            lo = float(df.iloc[j]['Low'])
            if side == 'long':
                if lo <= stop:
                    exit_px = stop
                    break
                if hi >= take:
                    exit_px = take
                    break
            else:
                if hi >= stop:
                    exit_px = stop
                    break
                if lo <= take:
                    exit_px = take
                    break
        if exit_px is None:
            exit_px = float(df.iloc[end]['Close'])
            j = end
        if exit_px <= 0:
            i += 1
            continue
        if side == 'long':
            ret = (exit_px * (1 - SLIPPAGE_PCT) - entry_px) / entry_px
        else:
            ret = (entry_px - exit_px * (1 + SLIPPAGE_PCT)) / entry_px
        ret -= FEE_ROUND_PCT
        trades.append(ret)
        equity.append(equity[-1] + ret)
        i = max(j + 1, i + 1)

    return _summarise(p, trades, equity, n, block_count)


def _summarise(p: BacktestParams, trades: List[float], equity: List[float],
               n: int, block_count: int) -> BacktestResult:
    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t < 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    pf = (gross_win / gross_loss) if gross_loss > 0 else (float('inf') if gross_win > 0 else 0.0)
    peak = 0.0
    max_dd = 0.0
    for v in equity:
        peak = max(peak, v)
        max_dd = max(max_dd, peak - v)
    mean = float(np.mean(trades)) if trades else 0.0
    std = float(np.std(trades, ddof=1)) if len(trades) > 1 else 0.0
    sharpe = (mean / std * math.sqrt(len(trades))) if std > 0 else 0.0

    # Time-block aggregation so PBO compares the same regimes across configs.
    blocks = [0.0] * block_count
    # Trades are sequential, so distribute them evenly over the same number of
    # blocks (a trade belongs to the block of its index in the trade list).
    if trades:
        for k, t in enumerate(trades):
            blocks[min(block_count - 1, int(k * block_count / len(trades)))] += t

    return BacktestResult(
        params={
            'min_adx': p.min_adx, 'min_roc': p.min_roc,
            'atr_stop_mult': p.atr_stop_mult, 'tp_r': p.tp_r,
            'max_hold_bars': p.max_hold_bars,
        },
        trades=len(trades),
        win_rate=(len(wins) / len(trades)) if trades else 0.0,
        net_return_pct=sum(trades) * 100.0,
        profit_factor=pf,
        expectancy_pct=(mean * 100.0) if trades else 0.0,
        max_drawdown_pct=max_dd * 100.0,
        sharpe=sharpe,
        block_returns=blocks,
        equity_curve=equity,
    )


DEFAULT_GRID: Dict[str, Iterable] = {
    'min_adx': (18.0, 22.0, 26.0),
    'min_roc': (0.10, 0.25, 0.40),
    'atr_stop_mult': (1.2, 1.5, 1.8),
    'tp_r': (1.5, 2.0, 2.5),
}


def expand_grid(grid: Optional[Dict[str, Iterable]] = None) -> List[BacktestParams]:
    g = dict(DEFAULT_GRID)
    if grid:
        for k, v in grid.items():
            if k in g and v:
                g[k] = tuple(v)
    keys = list(g.keys())
    out = []
    for combo in itertools.product(*(g[k] for k in keys)):
        out.append(BacktestParams(**dict(zip(keys, combo))))
    return out


def run_grid(dfs: Dict[str, pd.DataFrame], grid=None, *,
             block_count: int = 16) -> List[BacktestResult]:
    """Run every config across symbols; per-symbol returns are merged per block.

    A config trades each symbol independently, so its block return is the sum
    across symbols for the same block index.
    """
    configs = expand_grid(grid)
    results: List[BacktestResult] = []
    for p in configs:
        merged = [0.0] * block_count
        all_trades: List[float] = []
        all_eq: List[float] = [0.0]
        for _sym, df in dfs.items():
            r = run_backtest(df, p, block_count=block_count)
            all_trades.extend(r.equity_curve[1:])
            for k in range(min(block_count, len(r.block_returns))):
                merged[k] += r.block_returns[k]
        eq = [0.0]
        for t in all_trades:
            eq.append(eq[-1] + t)
        res = _summarise(p, all_trades, eq, 1, block_count)
        res.block_returns = merged
        results.append(res)
    return results


def pbo(block_matrix: np.ndarray) -> float:
    """Probability of backtest overfitting (CSCV).

    ``block_matrix`` has shape (blocks, configs) and holds each config's return
    per block. Returns the share of symmetric IS/OOS splits where the in-sample
    winner lands in the bottom half out-of-sample.
    """
    m = np.asarray(block_matrix, dtype=float)
    if m.ndim != 2 or m.shape[0] < 4 or m.shape[1] < 2:
        return float('nan')
    t, n = m.shape
    if t % 2:
        m = m[:-1]
        t -= 1
    half = t // 2
    totals = np.zeros(n)
    overfit = 0
    total = 0
    for is_idx in itertools.combinations(range(t), half):
        is_mask = np.zeros(t, dtype=bool)
        is_mask[list(is_idx)] = True
        is_scores = m[is_mask].sum(axis=0)
        best = int(np.argmax(is_scores))
        oos_scores = m[~is_mask].sum(axis=0)
        # Rank 0 = worst; overfit if the IS winner is in the bottom half OOS.
        rank = int(np.argsort(np.argsort(oos_scores))[best])
        if rank < n / 2:
            overfit += 1
        total += 1
    return overfit / total if total else float('nan')


def summarise_grid(results: List[BacktestResult]) -> dict:
    """Best config + PBO + the full table (sorted by net return)."""
    if not results:
        return {'configs': [], 'pbo': float('nan'), 'best': None}
    mat = np.array([r.block_returns for r in results]).T  # blocks x configs
    table = sorted((r.to_dict() for r in results),
                   key=lambda d: d['net_return_pct'], reverse=True)
    return {
        'configs': table,
        'pbo': round(float(pbo(mat)), 4) if mat.size else float('nan'),
        'best': table[0],
        'metric_note': 'net of 0.14% round-trip fees + 0.02%/side slippage',
    }
