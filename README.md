# COPIX — торговый терминал и бот OKX

Алгоритмический торговый бот на фьючерсах USDT-M биржи **OKX** + веб-терминал и
Telegram Mini App для управления им.

- **Продукт:** единственная стратегия — **AI Discretionary 1H** (BTC / ETH / SOL / XRP),
  решения принимает LLM, исполнение — реальными ордерами OKX с биржевыми стоп/тейками.
- **Деплой:** Render (Docker, free tier) → `https://trading-bot-mu99.onrender.com`
- **Репозиторий:** `git@github.com:molokm/trading-bot.git`
- **Стек:** FastAPI + `asyncio`/`asyncpg` (PostgreSQL) · React 18 + Vite + Tailwind · Docker

## ⚠ Ключевой контракт: LIVE — зеркало DEMO

`DEMO` — единственный «мозг» и единственный источник решений. Счёт `LIVE` **строго
механически копирует** позиции DEMO. Самостоятельная торговля на LIVE запрещена пятью
инвариантами, которые проверяются в рантайме при инициализации стратегии
(`_verify_mirror_guards()` в [backend/app/services/ai_strategy.py](backend/app/services/ai_strategy.py))
и покрыты тестами [tests/test_mirror_strict.py](tests/test_mirror_strict.py).

**Любое изменение этих гвардов обязано** обновить тесты, пройти CI и быть осознанным:
нарушение инвариантов означает независимые сделки на LIVE и потерю средств.

## Быстрый старт

```bash
# 1. Зависимости бэкенда (канонический venv — backend/.venv)
./backend/.venv/bin/python -m pip install -r backend/requirements.txt

# 2. Переменные окружения
cp .env.example backend/.env      # заполнить OKX_* и Telegram/Groq ключи

# 3. Полный стек: backend :8000 + vite :3000
./start.sh

# Только бэкенд
cd backend && ./.venv/bin/python -m uvicorn app.main:app --reload --port 8000
```

Фронтенд отдельно: `cd frontend && npm ci && npm run dev` (проксирует `/api` на :8000).

## Тесты

```bash
./backend/.venv/bin/python -m pytest          # 38 тестов: зеркало (14) + PnL (24)
```

Конфигурация — [pytest.ini](pytest.ini) (`pythonpath=backend`, `testpaths=tests`).
Тесты запускаются в CI на каждый push: [.github/workflows/ci.yml](.github/workflows/ci.yml).

## Деплой

```bash
git push origin main     # Render подхватит автоматически (autoDeploy: true)
```

Проверка после деплоя: `curl https://trading-bot-mu99.onrender.com/api/health` —
в ответе `version` равен первым 12 символам SHA коммита, `bots.ai: true`, состояние
`live_mirror`. Полный чек-лист и настройка keep-alive (free tier засыпает) —
[docs/DEPLOY.md](docs/DEPLOY.md).

## Структура

```
backend/     FastAPI-приложение (app/main.py, app/services/*, database.py)
frontend/    React SPA + Telegram Mini App
tests/       pytest: инварианты LIVE-зеркала и контракт PnL
docs/        документация: продукт, архитектура, деплой, история, аудит, research/
external/    исследовательский стенд: бэктесты, freqtrade, скрипты верификации
Dockerfile · entrypoint.sh · render.yaml   сборка и деплой
```

Для запуска одного контейнера есть также [backend/Dockerfile](backend/Dockerfile)
(раздаёт уже собранный SPA из `backend/static`).

## Документация

| Файл | О чём |
|---|---|
| [docs/PRODUCT.md](docs/PRODUCT.md) | Что за продукт, как торгует, монетизация, архив результатов |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Карта кода, внешние интеграции, API, переменные окружения |
| [docs/DEPLOY.md](docs/DEPLOY.md) | Прод, keep-alive, чек-лист проверки после деплоя |
| [docs/HISTORY.md](docs/HISTORY.md) | Хроника разработки и handoff-и |
| [docs/AUDIT.md](docs/AUDIT.md) | Находки аудита и бэклог по этапам |
| [docs/research/](docs/research/) | Бэктесты, OOS-решения, портфельные отчёты |

## Требования

- Python **3.12** (Docker) — локально работает и новее
- Node **20+** для сборки фронтенда
- Ключи: OKX (demo/live), Telegram Bot, LLM (Groq / OpenRouter / OpenAI) — см.
  [.env.example](.env.example)

## Оговорка

Торговля фьючерсами с плечом высокорисковая. Результаты прошлых периодов не гарантируют
будущей доходности; возможны убыточные периоды и просадки.
