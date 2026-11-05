# Хроника проекта COPIX

Сжатая хроника по логам разработки (17.08 → 12.10.2026). Подробный архивный текст
handoff-а от 12.08 — в конце файла.

| Период | Главное |
|---|---|
| 12.08 | Handoff: мультитенантность, тариф Pro, Telegram Stars, публичный `/tracker` |
| 17.08 → 31.08 | Мультиботы (Momentum, Impulse, OBI-скальпинг, VWAP), Smart Money, серия падений Render |
| 01.09 → 15.09 | Переход на AI-центризм, таблица `exchange_close_trades`, рождение LIVE-зеркала |
| 16.09 → 30.09 | Сага PnL, реальные стоп/тейки на OKX, шифрование ключей, единый `pnl_engine` |
| 01.10 → 07.10 | Кризис: PnL = 0 и самостоятельные LIVE-сделки → строгие гварды зеркала (v1.22) |
| 12.10 | Полный аудит приложения + ревизия структуры репозитория (эта сессия) |

---

## 2026-10-12 — ревизия структуры и тесты в CI

- Полный аудит приложения: 22 047 строк backend, 6 235 frontend, 94 роута, 13 таблиц.
  Вывод: критических дефектов нет, есть 4 вида техдолга (наблюдаемость, ручные списки
  безопасности, фронтенд без отмены запросов, монолиты) → [AUDIT.md](AUDIT.md).
- **Тесты возвращены в строй:** pytest подключён в CI (раньше только `py_compile`),
  4 сгнивших теста PnL переписаны под актуальный контракт, тестовый рут сведён в `tests/`
  (38 тестов зелёные).
- **Структура:** документация собрана в `docs/`, отчёты — в `docs/research/`, добавлены
  [README.md](../README.md) и [ARCHITECTURE.md](ARCHITECTURE.md).
- **Чистка:** удалено ~2,4 ГБ устаревших снапшотов бэкапов, закрыты дыры `.gitignore`.

## 2026-09-16 → 2026-10-12 — от мультибота к защищённому зеркалу

- **16.09** Аудит нашёл критические дефекты: `AIPosition` без `unrealized_pnl` (LIVE-контур
  мёртв после редеплоя), клонирование LIVE-позиций, kill-switch блокировал **закрытия**,
  plaintext LIVE-ключей. Закрыто коммитами `fa0ecb47`, `8383e4f6`, `2df40d09`.
- **17–18.09** Разбор 12+ фильтров входа: `min_roc_abs` (0.25 → 0.15), вынос `btc_roc_veto`
  в конфиг, баг falsy-zero (`0 or 0.20`), self-blocking (`mock:` в reason блокировал
  собственный сигнал). Добавлены `/api/ai/diagnostics` и подсчёт времени до бара.
- **20.09** После убытка BCH −$357 перешли с программного стопа на **реальные conditional
  SL/TP на OKX** с amend при трейлинге; `poll_interval_sec` 300 → 60.
- **21.09** Единый источник PnL — `pnl_engine` (до этого Mini App и Dashboard показывали
  разные суммы).
- **22–27.09** Расхождение PnL −90.99 vs −68.85: 5 закрытий без `clOrdId` считались в
  `/api/stats`, но отбрасывались в `_compute_pnl` — исправлено DB-fallback'ом.
- **02.10** Кризисный день: PnL зеркала = 0, откаты кода, расхождение с OKX ~90% сделок
  и **50+ самостоятельных LIVE-сделок** (ETH LONG) из-за авто-включения флага зеркала и
  gap-fill клонирования. Введены К1–К4: строгий opt-in, watermark
  `live_mirror_connected_at`, fail-closed `_mirror_enabled()`, единый чокепойнт `_place`.
- **03.10** Шесть этапов оптимизации: Circuit Breaker для OKX, прогрев кэшей при смене
  режима, position-level лимиты и корреляционные группы, `/api/health/circuit-breaker`.
- **04.10** Локальная копия потеряла 10 коммитов (поверх распакован старый бэкап) —
  сделан полный бэкап `_backups/20261004_002647/` (git bundle + БД + env).
- **06.10** Аудит (~40 находок), перф-фиксы, деплой **v1.21-tuned-exits** (`ffc27a74`):
  тюнинг выходов (`allocation_pct` 0.35→0.30, `exit_min_profit` 0.10→0.30,
  `trail_activate` 0.65→0.85, `time_stop_stale` 5→7 ч, `AI_RISK_PER_TRADE` 0.02→0.012).
- **07.10** «Зеркало опять совершало самостоятельные сделки» — ребейз с `--theirs` снёс
  строгую логику. Восстановлено в `26fe93ce`, затем **v1.22** (`d4587522`): докстринг с
  5 инвариантами, `_verify_mirror_guards()` в конструкторе (RuntimeError
  `MIRROR GUARDS MISSING — LIVE TRADING UNSAFE`), 14 тестов. **Это текущий прод.**

## 2026-08-17 → 2026-09-15 — эра мультиботов и разворот к AI

- **17–18.08** Синхронизация с GitHub (origin был впереди на 26 коммитов: JWT-сессии на
  Render, guest-режим, риск-контроль, kill switch). Разбор «фантомной сделки»: модель
  «открытые = боты + OKX, закрытые = только `/api/trades/paired`».
- **21–23.08** Impulse v4, Rotation v6.2, коммит `feat: add AI Discretionary 1H strategy and
  Scalping v2 + AI Orchestrator`. Борьба за корректные бейджи стратегий.
- **24–25.08** Честный бэктест: Momentum v3 +444.5% (CAGR 100.5%, MaxDD 17.3%, Sharpe 2.68);
  задеплоена **VWAP Mean Reversion 15m** (7 лет, +125%).
- **29.08** Раздел «Скальпинг по стакану (OBI)» переделан в **Smart Money Tracker**
  (поиск и копирование трейдеров, источник — Hyperliquid).
- **31.08** Инцидент с ETH +657.72 и ~10 падений Render за день: OOM на 36 МБ JSON
  лидерборда Hyperliquid, `asyncio.run()` из фоновых потоков, asyncpg на чужом event loop.
  Итог: `disable Smart Money background execution`, раздел пересобран как read-only.
- **01.09** Разворот в AI-центризм: все прочие боты выключены, атрибуция сделок
  исправлена, PnL переведён в детерминированный поток.
- **03.09** Новая таблица `exchange_close_trades` — база для честного PnL.
- **12–15.09** Создано **LIVE-зеркало** («single brain, dual account»): DEMO — мозг,
  LIVE — копия. Удалены вкладка Live и переключатель режимов (−612 строк UI). Появились
  бейджи DEMO/LIVE, метрики открытой сделки в карточке зеркала, `next_tick_at`, top-3 сигнала.

## Август 2026 — Telegram-подписки (архивная модель)

Тариф **Сигналы** (100 ⭐ → приватный канал `RotationTradeBot-signals`) и **Pro**
(500 ⭐ → Mini App + свои ключи OKX + автоторговля), бот `@RotationTradeBot` с оплатой
Telegram Stars, публичный equity-трекер `/tracker`. Позже платный канал и тариф «Сигналы»
выведены (осталась бесплатная рассылка сигналов и тариф Pro), `/tracker` отдан в retired.

---

# Приложение: handoff от 2026-08-12 (архив)

> Текст сохранён как исторический документ. Часть решений с тех пор изменена: продукт
> переведён в режим AI_ONLY, платный канал сигналов и `/tracker` выведены, LIVE-зеркало
> получило жёсткие гварды. Актуальное состояние — [PRODUCT.md](PRODUCT.md) и
> [ARCHITECTURE.md](ARCHITECTURE.md).

Дата: 2026-08-12 (ночь)
Ветка: `main`, репозиторий `https://github.com/molokm/trading-bot`, прод: `https://trading-bot-mu99.onrender.com`
Рабочая директория: `~/Documents/Торговый БОТ`

---

## 1. Что уже было до этой сессии

- Бот Momentum Rotation v4 + Impulse 1D на OKX USDT-M фьючерсах (дневные свечи)
- Независимая валидация: freqtrade + walk-forward + honest backtest (вне выборки)
- Telegram-уведомления о сделках, Mini App, бэктест-движок, админ-дашборд
- Демо-валидатор исполнительного механизма (Validation bot)

## 2. Что сделано в этой сессии (2 коммита)

### Коммит `96ad252` — Мультитенантность + Pro тариф
**Модель продаж:**
- 📡 **Сигналы** — 100 ⭐/мес → приватный канал с сигналами
- 💎 **Pro** — 500 ⭐/мес → мини-ап + свои ключи OKX + автоторговля ботами

**Backend:**
- `backend/app/services/telegram_bot.py` (новый) — long-polling поллер:
  - команды `/start`, `/subscribe`, `/subscribe_pro`, `/status`, `/help`
  - приём оплаты **Telegram Stars** (`sendInvoice` XTR → `pre_checkout_query` → `successful_payment`)
  - выдача одноразового инвайта в приватный канал (`createChatInviteLink`)
  - тариф определяется из invoice payload (`sub_pro_<ts>` / `sub_signals_<ts>`), продление **стакается**
- `database.py`:
  - таблица `subscriptions` (user_id, plan=signals|pro, active_until, payment_id)
  - таблица `users` (telegram_id, plan, статус, зашифрованные ключи OKX, active_until)
  - методы CRUD: subscriptions, users (find_or_create_user, update_user, list_users)
  - `ensure_bot()` — идемпотентная вставка bots (для FK performance_metrics)
- `strategy_manager.py` (новый) — пер-юзер инстансы ботов:
  - `UserBots` (rotation/impulse/validation на пользователя)
  - `PerUserClientManager` — холдер OKXClient пользователя
  - изолированные `bot_id`: `rotation_strategy:<uid>` / `impulse_strategy:<uid>`
- `impulse_strategy.py` — жёсткий `IMP_BOT_ID` заменён на `self.BOT_ID` (для пер-юзер изоляции)
- `auth.py` — сессии с `user_id`, `grant_user()`, `get_user_id()`, `encrypt_str()/decrypt_str()` (Fernet)
- `main.py`:
  - `/api/auth/telegram` → владелец=admin, любой другой верифицированный Telegram-юзер → своя учётка (role=user)
  - новый контур **`/api/me/*`**: профиль, подключение ключей, портфель, позиции, статусы, старт/стоп ботов, сделки, PnL
  - Pro-гейт: боты запускаются только при активной подписке Pro
  - уведомления юзерских ботов идут **в их личный чат**, не в канал (`_user_notifier`)
  - `/api/subs` — подписчики + планы + выручка по тарифам + пользователи мини-ап
- `.env.example`, `backend/.env.example`, `render.yaml` — переменные SIGNAL_*/PRO_*

**Frontend:**
- `MiniAppPage.jsx` — план-бейдж (FREE/Сигналы/PRO), форма «Подключить счёт OKX», кнопки Старт/Стоп Momentum+Impulse, данные через `/api/me/*` для role=user
- `SettingsPage.jsx` — панель «Платные сигналы · Подписки» (выручка, PRO-цена, список подписчиков + пользователей, продление/отключение)
- `api.js` — методы `/api/me/*` и `/api/subs/*`

**Бонус-фикс:** `_server_hit_logger` падал с UnboundLocalError на любом исключении в эндпоинте — исправлено (re-raise).

### Коммит `f3e5de2` — Публичный equity-трекер
- `backend/app/services/equity_tracker.py` (новый) — снапшоты equity владельца в `performance_metrics` (bot_id=`portfolio`) каждые 600с + на старте
- `GET /api/tracker` (публичный, без auth): текущий equity, кривая, PnL 24ч/7д/30д, статистика (win rate, profit factor, avg win/loss), разбивка по стратегиям, результаты бэктестов (статичная «credibility»-панель), последние сделки
- `TrackerPage.jsx` — публичная страница `/tracker` (без авторизации): метрики, кривая счёта, бэктесты, дисклеймер
- `App.jsx` — маршрут `/tracker` (публичный), `api.js` → `getTracker()`

## 3. Настройка канала (сделано)

- Канал: **RotationTradeBot-signals** (приватный)
- **TELEGRAM_CHANNEL_ID = `-1004318659681`**
- Бот `@RotationTradeBot` — админ канала, право «Приглашать участников» (проверено: createChatInviteLink работает)
- ID получен из ссылки на сообщение `t.me/c/4318659681/5` (число = ID без `-100` префикса)
- Локальный `backend/.env` обновлён
- **На сервере** ID задан через дашборд (Настройки → Telegram → Канал сигналов) — в памяти, без рестарта

## 4. Текущее состояние прода

- Сервер жив, OKX подключён (demo-режим), equity ~$76 800
- `/tracker` работает (6+ снапшотов, 50 сделок, win rate 70%)
- Бот-поллер на сервере активен (подтверждено через 409 Conflict при локальном getUpdates)

## 5. Ключевые переменные окружения

```
TELEGRAM_BOT_TOKEN=...            # бот @RotationTradeBot (задан локально + на сервере)
TELEGRAM_CHAT_ID=107946948        # личный чат владельца (@mvlad_m)
TELEGRAM_CHANNEL_ID=-1004318659681  # канал сигналов
SIGNAL_PRICE_STARS=100            # цена сигналов
SIGNAL_PLAN_DAYS=30
SIGNAL_INVITE_DAYS=3
PRO_PRICE_STARS=500               # цена Pro
PRO_PLAN_DAYS=30
TRACKER_INTERVAL_SEC=600          # снапшоты трекера
```

## 6. Как проверить/протестировать (быстро)

```bash
# бэкенд локально
cd backend && venv/bin/python -m app.main

# сборка фронтенда
cd frontend && npm run build

# тесты оплат (сигналы/pro) — прогнать:
#   venv/bin/python -c "<см. тесты в истории сессии>"
```

Живой тест в Telegram:
1. `@RotationTradeBot` → `/start` → меню тарифов
2. `/subscribe` → инвойс 100 ⭐
3. `/subscribe_pro` → инвойс 500 ⭐
4. Следующий сигнал → в канале

## 7. Деплой

- Push в `origin/main` → Render autoDeploy (render.yaml)
- После пушей: `96ad252`, `f3e5de2` задеплоены
- Прод: `https://trading-bot-mu99.onrender.com`

## 8. Идеи для следующего шага (монетизация)

1. **Копи-трейдинг / profit-share** — инвестор даёт read-only ключ, бот торгует, берём % с прибыли
2. **Другие биржи** (Binance/Bybit) через CCXT (уже в requirements)
3. **Спот-режим** для консервативных клиентов
4. **Кнопка «тест-рассылка в канал»** в дашборде (сейчас `/api/telegram/simulate` шлёт только владельцу, не в канал)
5. **Оплата картой/криптой** для Pro помимо Stars (Stripe/CryptoPay)

## 9. Известные ограничения / на что обратить внимание

- Сейчас всё на **demo** (OKX_DEMO=true) — для реальной торговли переключить на сервере
- Пер-юзер боты торгуют на реальных счетах пользователей — нужен юр./договорная сторона и лимиты рисков
- `_user_clients` кэш OKXClient в памяти: при смене ключей юзером клиент пересоздаётся (реализовано)
- Снапшоты трекера только по владельцу; кривая заполняется ~через 1ч (6 точек/час минимум)
- Секреты: `backend/.env` в gitignore, на сервере — secrets Render (sync:false, заполняются вручную)
