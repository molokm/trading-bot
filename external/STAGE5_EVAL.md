# Stage 5 — Independent strategy evaluation protocol

This document is the **source of truth** for how strategy performance claims
must be read. UI card numbers are *informative*, not guarantees.

## 1. What the live bots run

| Bot | Code | Bar | Universe |
|-----|------|-----|----------|
| Momentum Rotation v5 | `backend/app/services/rotation_strategy.py` | 1D | 10 OKX SWAP alts |
| Impulse 1D v2 | `backend/app/services/impulse_strategy.py` | 1D | same |
| MACD+Donchian Validation | `validation_strategy.py` / macd_donchian | 1D | same |

Configs auto-started on Render must match the Backtrader engines under
`external/backtests/` (see `BACKTEST_CARDS.md`).

## 2. Cost model (mandatory for any “honest” number)

| Item | Value |
|------|-------|
| Commission | **0.10%** per side (taker) |
| Slippage | **0.05%** per side |
| Signal | previous bar **close** only (no look-ahead) |
| Fill | next bar **open** |
| Stop | pessimistic high/low check where engine supports it |
| Funding | include when available (`honest_backtest_3y` / bills type 8) |

Any result without commission+slippage is **not** comparable to live.

## 3. In-sample vs out-of-sample

Parameter sweeps (`sweep_momentum.py`, `sweep_impulse.py`, `sweep_validation.py`)
and walk-forward scripts **select** parameters on historical windows.

Therefore:

- **Full-window CAGR/Sharpe on 2023–2026 after tuning is partly in-sample.**
- Prefer metrics from **held-out OOS windows** (`walkforward.py`, `walkforward_v3.py`, `wfo_oos_compare.py`).
- A high full-sample CAGR with MaxDD ~35–50% is **not** proof of edge after costs in live regime.

### How to read UI claims today

| Claim in UI / STRATEGY_DESC | Interpretation |
|-----------------------------|----------------|
| Momentum CAGR ~60%, Sharpe 1.23, MaxDD −52% | Full-sample BT after v5 tuning — **optimistic upper bound** |
| Impulse CAGR ~63%, Sharpe 1.58, MaxDD −36% | Same caveat |
| Validation CAGR ~50% (tuned) | Improved vs older 18% config; still check OOS years (2025–2026 soft) |

Live demo equity path can diverge: different capital, concurrent bots, partial fills, sleep/restarts, and single-coin concentration (e.g. XRP).

## 4. Repro commands (independent engines)

From repo root (needs network for candle download, or use local cache):

```bash
# Momentum (Backtrader, OKX 1D)
python external/backtests/backtrader_momentum_rotation.py \
  --pairs BTC,ETH,BNB,XRP,SOL,DOGE,ADA,TRX,AVAX,LTC --days 1100

# Impulse
python external/backtests/backtrader_impulse.py \
  --pairs BTC,ETH,BNB,XRP,SOL,DOGE,ADA,TRX,AVAX,LTC --days 1100

# Validation MACD+Donchian
python external/backtests/backtrader_macd_donchian.py

# Walk-forward / OOS compare (honest_backtest family)
python external/scripts/wfo_oos_compare.py
python external/scripts/walkforward_v3.py

# Aggregate existing JSON + protocol checklist
python external/scripts/stage5_eval_report.py
```

## 5. Pass / fail checklist before trusting a config live

1. Costs ≥ 0.10% + 0.05% per side applied.
2. No look-ahead (signal on close, fill on next open).
3. At least one **OOS** window after last parameter change.
4. MaxDD and worst calendar year reported (not only CAGR).
5. Live paper (OKX demo) tracks same rules for ≥ 30 days without silent param edits.
6. `RISK_MAX_DAILY_LOSS_USD` / kill switch set before real capital.

## 6. Next engineering steps (optional)

- Wire `stage5_eval_report.py` output into `/api/reports/backtests` (read-only).
- Freeze strategy params in git tags when promoting DEMO → LIVE.
- Ban silent hyperopt on the same window used for marketing cards.
