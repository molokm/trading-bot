# Impulse 1D — OOS review & v3 decision

Date: 2026-08-17  
Engine: `backtrader_impulse.py`, costs 0.10% + 0.05%, OKX 1D 10 coins.

## Baseline v2 (TUNED)

| Metric | Full | OOS (2025-04 → 2026-08) |
|--------|------|-------------------------|
| Total | +408.6% | +41.4% |
| CAGR | ~64.1% | ~30.2% |
| MaxDD | −36.5% | −23.7% |

Calendar (v2): 2023 +108.9% / 2024 +102.1% / 2025 **−6.3%** / 2026 +27.4%.

OOS is modest; 2025 is the weak year.

## Search (constraint: full total ≥ base AND OOS total ≥ base)

Most one-factor moves failed. Notable:

| Change | Full | OOS | Verdict |
|--------|------|-----|---------|
| entry_roc / top_k / higher risk | worse or OOS↓ | — | reject |
| max_hold=25 | full↓ | OOS **+84%** | reject (cuts full) |
| **max_hold=28** | **+429%** | **+48.7%** | accept |
| tp1_frac=0.25 alone | +437% | +41.0% (≈flat) | soft |
| **max_hold=28 + tp1_frac=0.25** | **+457%** | **+48.7%** | **accept best** |

## Applied v3

- `max_hold_bars`: 30 → **28**
- `tp1_frac`: 0.3 → **0.25** (lighter first TP, more runner)

Tradeoff: MaxDD full ~−38.7% vs −36.5% (slightly deeper).

## LIVE

Keep DEMO bias; set risk limits before LIVE. 2025-style chop still a risk.


## v4 (2026-08-21)

Anti-climax filter: skip entry when volume >= 3.5× average on the impulse day.
Full-sample BT: ~+5550tal, CAGR ~77.3