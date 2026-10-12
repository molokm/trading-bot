# Архитектура и интеграции

> Актуально на 2026-10-12 (после ревизии структуры). Продукт — [PRODUCT.md](PRODUCT.md),
> деплой — [DEPLOY.md](DEPLOY.md), бэклог — [AUDIT.md](AUDIT.md).

## 1. Карта репозитория

```
.
├── backend/                  # FastAPI-приложение (Python 3.12)
│   ├── app/
│   │   ├── main.py           # монолит: приложение, 94 роута, middleware, старт ботов
│   │   ├── database.py       # Database (asyncpg/aiosqlite), 13 таблиц, пул per-loop
│   │   ├── orchestrator.py   # свечи/сканирование (наследие)
│   │   ├── scanner.py        # сканер рынка (tickers SWAP)
│   │   ├── r_tracker.py      # R-multiple трекер (наследие)
│   │   ├── routers/meta.py   # GET /api/meta/product
│   │   └── services/         # см. таблицу ниже
│   ├── static/               # ⚠ закоммиченная устаревшая сборка SPA (Docker перекрывает)
│   ├── data/                 # SQLite для локального запуска (в .gitignore)
│   ├── requirements.txt      # рантайм-зависимости
│   └── Dockerfile            # standalone-сборка без фронтенда
├── frontend/                 # React 18 + Vite + Tailwind (ru/en, PWA)
│   ├── src/pages/            # Dashboard, Bots, History, Stats, Admin, Settings, Docs, MiniApp, Login
│   ├── src/services/api.js   # тонкий fetch-клиент (BASE=/api)
│   └── dist/                 # сборка (в .gitignore)
├── tests/                    # единственный тестовый рут: pytest.ini → testpaths=tests
├── docs/                     # документация (см. ниже) + research/
├── external/                 # исследовательский стенд: скрипты, бэктесты, freqtrade, tradingview
├── deploy: Dockerfile, entrypoint.sh, render.yaml, start.sh
└── pytest.ini                # pythonpath=backend, testpaths=tests
```

Документация: [PRODUCT.md](PRODUCT.md) (что за продукт) · [ARCHITECTURE.md](ARCHITECTURE.md)
(этот файл) · [DEPLOY.md](DEPLOY.md) (прод/keep-alive/чек-лист) · [HISTORY.md](HISTORY.md)
(хроника и handoff-и) · [AUDIT.md](AUDIT.md) (находки и план) · [research/](research/)
(бэктесты и OOS-решения).

## 2. Backend: модули

| Модуль | Строк | Назначение |
|---|---|---|
| `main.py` | 7 257 | FastAPI-приложение, 94 роута, 5 middleware, автостарт ботов, PnL/статистика |
| `services/ai_strategy.py` | 5 821 | **Ядро торговли**: `AIStrategy`, `AIConfig`, `AIPosition`, индикаторы, оркестрация DEMO и LIVE-зеркала, журнал |
| `database.py` | 2 189 | `Database` (81 метод), схема 13 таблиц, инструкции для PG и SQLite |
| `services/pnl_engine.py` | 983 | Единый источник PnL: `aggregate_rows`, эпоха, фильтры атрибуции |
| `services/ai_agent.py` | 832 | LLM-вызовы (Groq/OpenRouter/OpenAI), промпты, `validate_decision`, risk-manager veto |
| `services/telegram_bot.py` | 707 | Long-polling, меню, команды, оплата Stars (Pro) |
| `services/okx_client.py` | 580 | REST-клиент OKX v5, подпись, retry/backoff, Circuit Breaker |
| `services/telegram_notifier.py` | 559 | Отправка уведомлений, `verify_init_data` для Mini App |
| `services/auth.py` | 387 | JWT (HS256), Fernet-шифрование ключей, blacklist, rate-limit |
| `services/position_claim.py` | 362 | Клеймы позиций между «ботами», защита от дублей |
| `services/trade_attribution.py` | 339 | Привязка сделок к ботам, определение scale-ботов |
| `services/risk_guard.py` | 280 | Kill-switch, дневной лимит, лимиты позиций/корреляции |
| `services/legacy_stubs.py` | 155 | Заглушки выведенных стратегий (`_RetiredBot`); поверхность, нужная `main.py`, закреплена тестом [tests/test_legacy_stubs_contract.py](../tests/test_legacy_stubs_contract.py) |
| `services/strategy_manager.py` · `equity_tracker.py` · `account_context.py` · `strategy_cards.py` · `analysis_logger.py` | 144 · 89 · 73 · 38 · 97 | Пер-юзерные боты, снапшоты equity, контекст счёта, тексты для TG, JSONL-журнал |
| `services/backtest_service.py` | 7 | Заглушка `run_backtest_async` |

Frontend — 9 060 строк; крупнейшие: `Dashboard.jsx` 1 694, `SettingsPage.jsx` 807,
`BotsPage.jsx` 788, `MiniAppPage.jsx` 517, `i18n/ru.js` и `i18n/en.js` по 710.

## 3. Внешние интеграции

| Система | Что делает код | Где | Переменные |
|---|---|---|---|
| **Render** | Хостинг прод-сервиса: Docker, `plan: free`, `autoDeploy: true`, health-check `/api/health`; порт из `PORT` | [render.yaml](../render.yaml), [entrypoint.sh](../entrypoint.sh), [Dockerfile](../Dockerfile) | `PORT`, `BACKEND_PORT`, `RENDER_GIT_COMMIT` (версия в health) |
| **GitHub** | Источник деплоя (`push` в `main` → Render); CI: компиляция + **pytest** + сборка фронта + публикация образа в GHCR | [.github/workflows/ci.yml](../.github/workflows/ci.yml) | `GITHUB_TOKEN` (в CI) |
| **OKX API v5** | Единственная биржа: REST `https://www.okx.com` (баланс, позиции, ордера, algo-ордера, fills, bills, свечи), HMAC-SHA256-подпись, `x-simulated-trading: 1` в demo, retry на 429/500xx, Circuit Breaker; WebSocket-клиент объявлен, но не используется | [okx_client.py](../backend/app/services/okx_client.py) | `OKX_API_KEY`, `OKX_SECRET_KEY`, `OKX_PASSPHRASE`, `OKX_DEMO` |
| **Telegram Bot API** | Long-polling `getUpdates` (offset + дедуп в таблице `tg_processed_updates`), уведомления о сделках, команды `/start /subscribe_pro /status /stats /tracker`, оплата **Stars** (`currency=XTR`), `setChatMenuButton` → `/mini`; проверка `initData` для Mini App | [telegram_bot.py](../backend/app/services/telegram_bot.py), [telegram_notifier.py](../backend/app/services/telegram_notifier.py) | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_CHANNEL_ID`, `PRO_PRICE_STARS`, `PRO_PLAN_DAYS` |
| **LLM-провайдеры** | OpenAI-совместимый `POST /chat/completions` (`response_format=json_object`): Groq (primary, `openai/gpt-oss-20b`) → OpenRouter → OpenAI; cooldown на 429/ошибки; `mock_decide`, если ключей нет | [ai_agent.py](../backend/app/services/ai_agent.py) | `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `AI_LLM_*` |
| **PostgreSQL (Neon)** | Прод-хранилище: asyncpg-пул `min=1/max=5`, кэш пула per event loop; локально — SQLite | [database.py](../backend/app/database.py) | `DATABASE_URL` |
| **Telegram WebApp JS** | Скрипт Mini App на странице | [frontend/index.html](../frontend/index.html) | — |
| **Hyperliquid / Binance / Copy Trading** | Были в Smart Money и бэктестах; в рантайме **сетевых вызовов нет** (заглушки). Живут только в исследовательском стенде `external/` | — | — |

Keep-alive на free tier: в коде **отсутствует**, нужен внешний монитор (см. [DEPLOY.md](DEPLOY.md)).

## 4. База данных

13 таблиц (`CREATE TABLE IF NOT EXISTS`): `bots`, `signals`, `trades`, `positions`,
`performance_metrics`, `settings`, `subscriptions`, `users`, `tg_processed_updates`,
`exchange_close_trades`, `tracked_traders`, `copy_trades`, `audit_log`.

Индексы: `idx_trades_mode_ts`, `idx_trades_bot`, `idx_trades_account_mode_ts`,
`idx_ect_mode_ts`, `idx_ect_bot_label`. Часть рантайм-конфигурации живёт в таблице
`settings` (в т.ч. `TELEGRAM_CHANNEL_ID`, offset Telegram, чёрный список JWT).

## 5. HTTP API (94 роута)

- **Публичные**: `GET/HEAD /api/health`, `GET /api/health/circuit-breaker`,
  `POST /api/auth/{login,guest,telegram,logout}`, `GET /api/auth/status`,
  `GET /api/ai/status`, `GET /api/me/dashboard`, `GET /api/startup-log`,
  `GET /api/meta/product`, SPA-fallback (`GET /{full_path}`), `/assets/*`.
- **Авторизация**: глобальный middleware (JWT из `Authorization: Bearer` или cookie
  `auth_token`) + точечный `Depends(require_admin)`; три ручных списка —
  `ADMIN_ONLY_PATHS`, `ADMIN_ONLY_PREFIXES`, `GUEST_FORBIDDEN_PREFIXES`
  (это хрупкое место, см. [AUDIT.md](AUDIT.md)).
- **Группы**: `/api/ai/*` (статус, конфиг, старт/стоп, диагностика, логи),
  `/api/live/*` (статус/connect/disconnect — владелец LIVE-зеркала),
  `/api/me/*` (Mini App: профиль, ключи, режим, портфель, позиции, сделки, PnL),
  `/api/pnl*`, `/api/stats`, `/api/trades*`, `/api/positions/*`, `/api/portfolio`,
  `/api/risk/*`, `/api/subs*`, `/api/admin/*`, `/api/telegram/*`, `/api/debug/*`.
- Отдельно: `/api/meta/product` (`{"product":"COPIX","mode":"AI_ONLY","bots":["AI Discretionary 1H"]}`).

## 6. Переменные окружения

| Группа | Переменные |
|---|---|
| Секреты | `OKX_API_KEY`, `OKX_SECRET_KEY`, `OKX_PASSPHRASE`, `DATABASE_URL`, `JWT_SECRET`, `TOKEN_ENCRYPTION_KEY`, `DASHBOARD_PASSWORD`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `TELEGRAM_CHANNEL_ID`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY` |
| Риск | `RISK_KILL_SWITCH`, `RISK_MAX_DAILY_LOSS_USD`, `RISK_MAX_POSITION_USD`, `RISK_MAX_LEVERAGE`, `RISK_MAX_OPEN_POSITIONS`, `RISK_MAX_POS_PER_SYMBOL`, `RISK_MAX_CORRELATED_EXPOSURE_USD` |
| Бот | `BOTS_AUTO_START`, `AI_AUTO_START`, `AI_FORCE_AUTOSTART`, `AI_EXECUTE`, `AI_CAPITAL`, `AI_MAX_LEVERAGE`, `AI_MAX_POSITIONS`, `AI_RISK_PER_TRADE`, `AI_POLL_SEC`, `ALLOW_MANUAL_ORDERS`, `OKX_DEMO` |
| LLM | `AI_LLM_PROVIDER`, `AI_LLM_MODEL`, `AI_LLM_FALLBACKS`, `AI_LLM_PAYLOAD_CHARS`, `AI_FORCE_MODEL`, `AI_ALLOW_LARGE_MODEL`, `GROQ_BASE_URL`, `OPENAI_BASE_URL`, `OPENROUTER_BASE_URL` |
| Прочее | `PORT`, `BACKEND_PORT`, `BACKEND_HOST`, `ENABLE_DOCS`, `CORS_ORIGINS`, `DATA_DIR`, `ANALYSIS_LOG_PATH`, `PNL_TZ`, `ORPHAN_CLOSE`, `ALLOW_MANUAL_ORDERS`, `PRO_PRICE_STARS`, `PRO_PLAN_DAYS`, `PRO_PLAN_LABEL`, `TRACKER_INTERVAL_SEC`, `AI_PERSIST_USER_STOP`, `RESET_TRADING_STATS` |

Полный шаблон с комментариями — [.env.example](../.env.example); минимальный для
локального запуска — [backend/.env.example](../backend/.env.example).

⚠ Осталось мёртвым: `RENDER_DISK_PATH` (в [render.yaml](../render.yaml) нет `disk:`).
Удалены как неиспользуемые в этапе 2A: `TRACKER_URL`, `SIGNAL_PRICE_STARS`,
`SIGNAL_PLAN_DAYS`, `SIGNAL_INVITE_DAYS`, `SM_EXECUTION_DISABLED`, `SESSION_SECRET`,
`AI_ONLY_MODE` (режим задан в коде), `ALLOW_INSECURE_SECRETS`.

## 7. Локальный запуск и проверки

```bash
# Полный стек (backend :8000 + vite :3000)
./start.sh

# Только бэкенд
cd backend && ./.venv/bin/python -m uvicorn app.main:app --reload --port 8000

# Тесты (pytest.ini: pythonpath=backend, testpaths=tests)
./backend/.venv/bin/python -m pip install -r backend/requirements-dev.txt
./backend/.venv/bin/python -m pytest

# Сборка фронтенда
cd frontend && npm run build
```

Канонический venv — `backend/.venv` (в нём есть `pytest`); `backend/venv` — устаревший
дубль, оставлен как есть.
