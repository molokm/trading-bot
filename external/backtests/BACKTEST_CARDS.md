# Настройки Backtrader-бэктестера (карточки стратегий на фронте)

Этот документ фиксирует **точные воспроизводимые настройки** и команды, которыми
считаются цифры на карточках стратегий в `frontend/src/pages/BotsPage.jsx`
(`MOM_BACKTEST`, `IMP_BACKTEST`, `VAL_BACKTEST`), чтобы к вопросу
«откуда эти цифры / почему не сходятся» больше не возвращаться.

Все движки — независимые Backtrader-реализации live-стратегий, данные — нативные
OKX 1D свечи. Свежий прогон даёт цифры, **близкие к карточкам** (отличие ~2–3 pp —
из-за нового незакрытого бара). Карточки были зафиксированы по состоянию данных
на 2026-08-14 (кэш `.momentum_sweep_cache.pkl` / `.impulse_sweep_cache.pkl`,
созданы в коммите `6c3da9d`).

---

## Общие условия (все стратегии)

| Условие | Значение |
|---|---|
| Данные | OKX-SWAP нативные 1D свечи, 10 монет: BTC, ETH, BNB, XRP, SOL, DOGE, ADA, TRX, AVAX, LTC |
| Период | 2023-05-04 → 2026-08-15 (~1100 баров, 3.28 года) |
| Стартовый капитал | $10 000 |
| Комиссия | 0.10% taker за сторону |
| Проскальзывание | 0.05% за сторону |
| Сигнал | по закрытию предыдущего бара (без подглядывания) |
| Исполнение | вход/выход по открытию следующего бара; стоп срабатывает на текущем баре, закрытие — на следующем open |

---

## Momentum Rotation v5 — `backtrader_momentum_rotation.py`

```
python external/backtests/backtrader_momentum_rotation.py \
    --pairs BTC,ETH,BNB,XRP,SOL,DOGE,ADA,TRX,AVAX,LTC --days 1100
```

**Настройки (в коде движка = live-конфиг v5):**

| Параметр | Значение |
|---|---|
| top_k | 2 |
| risk_per_trade | 0.20 |
| allocation_pct | 0.5 |
| max_leverage | 2.0 |
| adx_min | 25.0 |
| min_roc | 4.5 |
| vol_mult | 2.2 |
| corr_threshold | 0.85 |
| atr_stop_mult | 4.5 |
| trail_atr_mult | 3.0 |
| breakeven_pct | 0.05 |
| min_hold_days | 11 |

**Результат:**

| Метрика | Карточка | Свежий прогон (15.08) |
|---|---|---|
| CAGR | 59.8% | 62.6% |
| MaxDD | −51.8% | −51.8% |
| Годовые | 2023 +34.0% / 2024 +41.4% / 2025 +14.3% / 2026 +113.6% | 2023 +35.6% / 2024 +41.4% / 2025 +14.3% / 2026 +123.7% |

Источник оптимизации: sweep + walk-forward в `external/backtests/sweep_momentum.py`
и `walkforward.py` (коммит `6c3da9d`, TUNED-конфиг).

---

## Impulse 1D v2 — `backtrader_impulse.py`

```
python external/backtests/backtrader_impulse.py \
    --pairs BTC,ETH,BNB,XRP,SOL,DOGE,ADA,TRX,AVAX,LTC --days 1100
```

**Настройки (в коде движка = live-конфиг v2):**

| Параметр | Значение |
|---|---|
| top_k | 3 |
| risk_per_trade | 0.10 |
| max_leverage | 3.0 |
| entry_roc | 3.0 |
| max_adds | 0 |
| cooldown_bars | 3 |
| max_hold_bars | 30 |
| sl_atr_mult | 5.0 |
| trail_atr_mult | 12.0 |
| tp1_atr | 2.0 (доля 0.3) |
| tp2_atr | 10.0 (доля 0.3) |

**Результат:**

| Метрика | Карточка | Свежий прогон (15.08) |
|---|---|---|
| CAGR | 63.5% | 64.4% |
| MaxDD | −36.5% | −36.5% |

Источник оптимизации: sweep + walk-forward в `external/backtests/sweep_impulse.py`
и `walkforward.py` (коммит `6c3da9d`, TUNED-конфиг).

---

## MACD+Donchian Validation — `backtrader_macd_donchian.py`

```
python external/backtests/backtrader_macd_donchian.py --days 1100
```

**Настройки (в коде движка = live-конфиг валидатора, оптимизирован 2026-08-16
sweep + walk-forward):**

| Параметр | Значение |
|---|---|
| top_k | 4 |
| risk_per_trade | 0.14 |
| allocation_pct | 0.5 |
| max_leverage | 2.0 |
| donchian_n | 15 |
| macd_fast / slow / signal | 12 / 26 / 9 |
| atr_period | 14 |
| chandelier_atr | 4.0 |
| tp_pct | 0.10 (частичный TP: закрыть 20% при +10%) |
| tp_ratio | 0.2 |
| tp2_pct | 0.08 |
| be_pct | 0.015 |
| max_hold_days | 3 |

**Результат:**

| Метрика | Карточка (2026-08-16) | Свежий прогон (15.08) |
|---|---|---|
| CAGR | 49.7% | 49.7% |
| Total | +276.3% | +276.3% |
| Sharpe | 1.19 | 1.19 |
| MaxDD | −48.0% | −48.0% |
| Годовые | 2023 +57.3% / 2024 +243.4% / 2025 −9.3% / 2026 −26.0% | то же |

Результат сохранён в `macd_donchian_validation_bt_result.json`.

**Walk-forward (2026-08-16):** IS 2023-05→2025-04, OOS 2025-04→2026-08.
Оптимизированный конфиг существенно лучше прежнего на OOS
(CAGR −24.5% vs −39.1%, MaxDD −48% vs −62.1%); OOS остаётся убыточной —
Donchian+MACD структурно слаб в 2025–2026. Прежний конфиг (до оптимизации):
`tp_pct 0.08 / tp_ratio 0.3 / tp2_pct 0.10 / alloc 0.15 / lev 1×` → CAGR 17.7%,
MaxDD −62.1% (2023 +62.0% / 2024 +120.3% / 2025 −28.6% / 2026 −38.3%).

---

## Примечание про кэш данных (разница «карточка vs свежий прогон»)

- Карточки зафиксированы на кэше данных от **2026-08-14**
  (`.momentum_sweep_cache.pkl`, `.impulse_sweep_cache.pkl`).
- Свежие данные OKX добавляют +1 бар (2026-08-15), из-за чего CAGR меняется
  на ~2–3 pp, а годовая 2026 — на ~10 pp.
- Пересоздать кэш: `python external/backtests/sweep_momentum.py --pairs ... --days 1100`
  (первый запуск без кэша загрузит данные заново).
