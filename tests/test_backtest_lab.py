"""Point-in-time backtest lab: causality, cost model, PBO."""
import math

import numpy as np
import pandas as pd
import pytest

from app.services import backtest_lab as lab


def _bars(n=500, seed=3, drift=0.05):
    rng = np.random.default_rng(seed)
    trend = np.cumsum(np.full(n, drift)) + 100 + np.sin(np.arange(n) / 9) * 2
    close = trend + rng.normal(0, 0.6, n)
    open_ = np.r_[close[0], close[:-1]]
    high = np.maximum(open_, close) + np.abs(rng.normal(0, 0.3, n))
    low = np.minimum(open_, close) - np.abs(rng.normal(0, 0.3, n))
    return lab.frame_from_candles([
        [str((n - i) * 3_600_000), str(open_[i]), str(high[i]),
         str(low[i]), str(close[i]), "100"] for i in range(n)
    ])


def test_indicators_are_causal():
    df = lab.prepare(_bars(400))
    cut = lab.prepare(_bars(400).iloc[:300])
    for col in ("EMA_20", "EMA_50", "ATR_14", "ADX_14", "ROC_3"):
        assert df[col].iloc[299] == pytest.approx(cut[col].iloc[299], rel=1e-9, abs=1e-9)


def test_backtest_runs_and_respects_costs():
    df = lab.prepare(_bars())
    res = lab.run_backtest(df, lab.BacktestParams())
    assert res.trades > 0
    assert res.max_drawdown_pct >= 0
    # Net is always gross minus the cost model; never positive when every
    # trade is a tiny gross winner below the round-trip cost.
    tight = lab.BacktestParams(atr_stop_mult=0.01, tp_r=0.01, max_hold_bars=1)
    r2 = lab.run_backtest(df, tight)
    if r2.trades:
        assert r2.net_return_pct < 0


def test_pbo_detects_stable_winner():
    # Config 0 is best in every block -> never overfit.
    blocks = np.array([[1.0, 0.1], [1.1, 0.2], [0.9, 0.1], [1.0, 0.2]])
    assert lab.pbo(blocks) == 0.0


def test_pbo_detects_overfit_winner():
    # Config 0 wins in-sample halves that exclude its bad block and loses OOS
    # often enough to rank in the bottom half.
    blocks = np.array([
        [5.0, 0.2], [5.0, 0.2], [5.0, 0.2], [5.0, 0.2],
        [5.0, 0.2], [5.0, 0.2], [5.0, 0.2], [-50.0, 0.2],
    ])
    val = lab.pbo(blocks)
    assert 0.0 < val <= 1.0
    assert not math.isnan(val)


def test_grid_and_summary_shape():
    df = lab.prepare(_bars())
    results = lab.run_grid({"BTC": df})
    assert len(results) == len(lab.expand_grid())
    summary = lab.summarise_grid(results)
    assert summary["best"] is not None
    assert 0.0 <= summary["pbo"] <= 1.0
    assert len(summary["configs"]) == len(results)
