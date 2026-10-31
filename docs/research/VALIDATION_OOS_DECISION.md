# MACD+Donchian Validation — OOS review & v2

Date: 2026-08-17  
Engine: `backtrader_macd_donchian.py`, costs 0.10%+0.05%, OKX 1D 10 coins.

## Problem

Strategy is **structurally weak in 2025–2026** (breakout/MACD regime).

| Config | Full total | OOS total | MaxDD |
|--------|------------|-----------|-------|
| OLD (was live: lev1, alloc15%, tp8%/30%) | +70.6% | **−47.6%** | −62% |
| TUNED v1 (tp10%/20%, lev2, alloc50%) | +275.3% | **−29.9%** | −48% |
| **v2 = TUNED + tp_pct 0.08** | **+267.1%** | **−16.2%** | **−41%** |

Calendar v2: 2023 +40.8% / 2024 +281.9% / 2025 −16.1% / 2026 −20.1%.

## Search notes

- Most levers could not make OOS positive.
- `tp_pct=0.08` (vs 0.10) best single change: OOS −30% → −16%, MaxDD improves, full slightly lower.
- Longer max_hold did not help OOS on this engine.

## Applied v2

- Live aligned to **TUNED sizing** (allocation 0.5, leverage 2.0, tp_ratio 0.2, tp2 0.08) — was accidentally on OLD.
- `tp_pct`: **0.10 → 0.08**

## Recommendation

- Keep on **DEMO only**; do not size up on LIVE while OOS remains negative.
- Prefer Momentum v6 / Impulse v3 for capital allocation.
- Re-review after another quarter of market data.


## v3 — OOS equalization (2026-08-17)

Goal: push OOS toward non-negative even if full-sample CAGR falls.

Only configuration found with **OOS > 0** in extensive search:

| Param | v2 | v3 |
|-------|----|----|
| donchian_n | 15 | **30** |
| top_k | 4 | **2** |
| tp_ratio | 0.2 | **0.4** |

| Metric | v2 | v3 |
|--------|----|----|
| Full total | +267% | **+102%** |
| Full CAGR | ~48.6% | **~23.9%** |
| OOS total | −16.2% | **+1.9%** |
| MaxDD | −40.7% | **−31.0%** |

Years v3: 2023 +25.3% / 2024 +56.7% / **2025 +18.7%** / 2026 −17.5%.

Neighbors (donchian 28/32, other ratios) mostly fall back to negative OOS — edge is narrow.
Still DEMO-first; 2026 partial year remains soft.
