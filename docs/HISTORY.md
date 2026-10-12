# Итоги разработки — торговый бот (handoff)

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
