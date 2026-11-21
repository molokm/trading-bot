# Исследования и бэктесты

Отчёты по стратегиям, которые разрабатывались и проверялись в проекте. Часть из них
относится к стратегиям, уже выведенным из эксплуатации (см. [../PRODUCT.md](../PRODUCT.md)),
но сохранена как источник проверенных цифр и методик.

Код стенда живёт в [../../external/](../../external/): `scripts/` (бэктестеры, верификация,
сбор данных), `backtests/` (результаты прогонов), `freqtrade_test/` (проверка на
стороннем движке freqtrade).

| Документ | О чём |
|---|---|
| [PORTFOLIO_2026-08-10.md](PORTFOLIO_2026-08-10.md) | Портфельный анализ momentum v4 + impulse 1D: расхождение движков, метрики, корреляции, годовые доходности |
| [MOMENTUM_OOS_DECISION.md](MOMENTUM_OOS_DECISION.md) | Решение по Momentum Rotation на вне-выборочном периоде |
| [MOMENTUM_WEAK_YEARS.md](MOMENTUM_WEAK_YEARS.md) | Разбор слабых лет стратегии |
| [IMPULSE_OOS_DECISION.md](IMPULSE_OOS_DECISION.md) | Решение по Impulse 1D |
| [VALIDATION_OOS_DECISION.md](VALIDATION_OOS_DECISION.md) | Решение по демо-валидатору исполнительного механизма |
| [STAGE5_EVAL.md](STAGE5_EVAL.md) | Оценка этапа 5 (риск-лимиты, устойчивость) |

## Как воспроизвести

```bash
pip install freqtrade[hyperopt]
python external/scripts/verify_results.py            # весь период + форвард-тест
python external/scripts/verify_results.py --full     # только 2022-2026
python external/scripts/verify_results.py --forward  # только форвард-тест
```

Данные (публичные свечи OKX) скачиваются автоматически. Точные параметры стратегий в
репозитории не раскрываются.
