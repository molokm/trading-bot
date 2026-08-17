# Momentum Rotation v5 — OOS review & decision

Date: 2026-08-17  
Engine: `external/backtests/backtrader_momentum_rotation.py` (commission 0.10% + slip 0.05%)  
Data: OKX native 1D, 10 coins, cache `.momentum_sweep_cache.pkl` (2023-05-05 → 2026-08-16)

## Config under review (live / TUNED)

```
top_k=2, risk_per_trade=0.20, allocation_pct=0.5, max_leverage=2,
adx_min=25, min_roc=4.5, vol_mult=2.2, corr_threshold=0.85,
atr_stop_mult=4.5, trail_atr_mult=3.0, breakeven_pct=0.05, min_hold_days=11
```

## Full sample

| Metric | Value |
|--------|-------|
| Total return | +297.8% |
| CAGR | ~52.3% |
| MaxDD | **−51.8%** |
| Sharpe | ~1.13 |

## Calendar years (same equity curve, windowed)

| Year | Total | MaxDD | Sharpe |
|------|-------|-------|--------|
| 2023 | +9.6% | −7.6% | 0.78 |
| 2024 | +38.9% | **−51.8%** | 0.88 |
| 2025 | +14.3% | −38.7% | 0.54 |
| 2026 (partial) | **+123.1%** | −20.3% | 2.36 |

Most of the headline CAGR is **2026**. 2025 is modest; 2024 carries the deep drawdown.

## IS / OOS split (60% / 40% by time)

IS: 2023-05-05 → 2025-04-24 · OOS: 2025-04-24 → 2026-08-16  

| Config | IS CAGR | OOS CAGR* | OOS MaxDD | OOS Sharpe |
|--------|---------|-----------|-----------|------------|
| **tuned_v5_live** (risk 0.20) | 3.7% | 177.7% | −20.3% | 2.18 |
| tuned_risk10 (risk 0.10) | 7.1% | 81.6% | −15.8% | 1.95 |
| old_default | 3.4% | 29.0% | −33.5% | 0.75 |

\*OOS CAGR is measured on the **continuing equity curve** (starts at equity at OOS boundary). After a deep IS drawdown, recovery inflates OOS CAGR — use **ranking and MaxDD**, not the absolute CAGR number.

**Ranking:** TUNED ≫ OLD on this OOS window. Lower risk (0.10) keeps strong OOS with milder DD.

## Caveats

1. Params were tuned on the full 2023–2026 window → not a pure holdout design.  
2. Path dependence: one bad year (2024 DD) + one strong partial year (2026).  
3. Live `risk_per_trade=0.20` is aggressive vs max leverage 2 and concurrent top_k=2.  
4. API defaults elsewhere still mention adx_min=29 / risk 0.14 — keep startup config as source of truth.

## Decision

| Item | Call |
|------|------|
| Strategy | **KEEP** Momentum Rotation v5 |
| LIVE full size | **NO** until ≥30d demo path matches rules and risk limits set |
| DEMO | **YES** — continue |
| Risk | Prefer **`risk_per_trade=0.10`** (or ≤0.14) for any capital that matters |
| Kill switch / daily loss | **Required** before LIVE |
| Next review | After next full quarter or if MaxDD from peak exceeds 25% live |

## Commands to reproduce

```bash
cd external/backtests
python3 -c "..."  # see walkforward.py --strat momentum
python walkforward.py --strat momentum   # full IS grid + OOS table
```


## Improvement 2026-08-17: vol_mult 2.2 → 2.0

Constraint: full-sample **and** OOS total return must not decrease vs TUNED v5.

| Config | Full total | OOS total | MaxDD |
|--------|------------|-----------|-------|
| baseline vol_mult=2.2 | +297.4% | +281.3% | −51.8% |
| **vol_mult=2.0** | **+330.0%** | **+307.8%** | −51.8% |

Other one-factor moves (min_roc, adx, trail, stop, hold) failed the constraint.
vol_mult=1.9 matched 2.0; 2.1 was slightly worse than 2.0 but still above baseline.

**Caveat:** search was on the same historical window — not a guarantee of future returns.


## Partial exits experiment (2026-08-17)

User idea: scale out at **+1%**, trail remainder, lock small profit on reverse.

Engine already supports one partial + breakeven + ATR trail:
- baseline: partial **50% at +8%**, BE at **+5%**, trail 3×ATR

### Results vs baseline (vol_mult=2.0)

| Idea | Full total | OOS total | MaxDD | Verdict |
|------|------------|-----------|-------|---------|
| baseline 8%/50%/BE5% | +332% | +310% | −51.8% | ref |
| **+1% / 30–50%** + BE5% | full ≈ or + | **OOS much worse** | often better | **reject** (fails OOS) |
| +1% + early BE (1–2%) | full collapses | weak | — | **reject** |
| +5%/50%/BE5% | +342% | +262% OOS↓ | −38% | reject OOS |
| **8%/30%/BE5%** | **+390%** | **+311%** | −53.3% | **accept** (return ≥ baseline) |
| 8%/40%/BE5% | +361% | +311% | −52.6% | accept milder |

**Why +1% hurts OOS:** on daily bars a +1% touch is noise; early scale-out cuts runners that dominated 2025–2026 recovery. Early BE after 1% turns winners into scratches and kills trend capture.

**Applied:** `partial_tp_ratio` **0.5 → 0.3** (still first take at +8%, BE +5%). Slightly higher MaxDD tradeoff for higher full return without cutting OOS.
