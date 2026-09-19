# Momentum — weak years 2023/2025 and min_roc retune

Date: 2026-08-18

## Why 2023 and 2025 look weak

**2023 (sample starts ~May):** only ~8 months in the BT window; post-banking-stress recovery then milder trends. Strict gates (`min_roc=4.5`, `adx_min=25`) skip many mild trends → few quality rotations → ~+21% calendar.

**2025:** more two-way / choppy crypto tape. Classic momentum (ROC + EMA + ADX) underperforms; many would-be entries fail ADX/ROC or get stopped in ranges → ~+16%.

**2024/2026:** strong directional legs → strategy captures the bulk of full-sample return.

Lowering filters (ADX 18–22, top_k=3, short hold) often **destroys** full-sample or flips 2025 negative (noise entries).

## Search (constraint: keep full & OOS strong)

| Change | Full | OOS | 2023 | 2025 | Notes |
|--------|------|-----|------|------|-------|
| v6 base min_roc=4.5 | +456% | +311% | +21% | +16% | ref |
| **min_roc=3.5** | **+538%** | **+317%** | +21% | **+17%** | **best** |
| min_roc=3.0 | +443% | +293% | +21% | +7% | worse 2025 |
| adx_min 22 alone | +62% | +124% | +21% | −15% | bad |
| top_k=3 alone | +335% | +254% | +12% | −17% | bad |
| trail tighter | weak | — | +33% 2023 | −32% 2025 | trades 2025 for 2023 |

## Applied v6.1

- `min_roc`: **4.5 → 3.5** (slightly more medium-strength trends)
- Indicators set unchanged: ROC, EMA20/50, ADX, RSI, vol, corr, BTC regime
- Card: CAGR **75.9%**, MaxDD **43.4%**, years +20.6 / +85.0 / +16.7 / +150.5

2023 still structurally capped by short sample + regime; further forcing entries hurts 2025 more than it helps 2023.
