# Деплой и эксплуатация

> Актуально на 2026-10-12. Архитектура — [ARCHITECTURE.md](ARCHITECTURE.md).

## 1. Как устроен деплой

| Шаг | Что происходит |
|---|---|
| 1 | `git push origin main` → GitHub (`molokm/trading-bot`) |
| 2 | **Render** видит push (`autoDeploy: true` в [render.yaml](../render.yaml)) и собирает [Dockerfile](../Dockerfile): стадия `node:20-slim` собирает SPA → стадия `python:3.12-slim` ставит `backend/requirements.txt` и копирует `dist` в `./static` |
| 3 | Контейнер стартует через [entrypoint.sh](../entrypoint.sh): `PORT` (Render) → `BACKEND_PORT` → 8000, `uvicorn app.main:app --limit-concurrency 50` |
| 4 | Render проверяет `GET /api/health`; при 200 сервис переключается на новую версию |

Параллельно [.github/workflows/ci.yml](../.github/workflows/ci.yml) прогоняет на каждый
push: компиляцию backend, **pytest** (`tests/`), сборку фронтенда и публикацию образа в
GHCR. Деплой на Render идёт независимо от CI — если тесты красные, прод всё равно
обновится.

## 2. Проверка после деплоя

```bash
curl -s https://trading-bot-mu99.onrender.com/api/health | python3 -m json.tool
```

Смотреть в ответе:

| Поле | Ожидание |
|---|---|
| `status` | `ok` |
| `version` | первые 12 символов SHA задеплоенного коммита (`RENDER_GIT_COMMIT`) |
| `connected` | `true` (ключи OKX приняты) |
| `demo` | `true` пока торговля идёт на демо-счёте |
| `bots.ai` | `true` — цикл стратегии запущен |
| `live_mirror` | `{connected, enabled}` — состояние LIVE-зеркала |
| `auth` | `jwt` |
| `risk` | `kill_switch`, лимиты, число открытых позиций |
| `sm_diag.rss_mb` | потребление памяти (free tier — 512 МБ) |

`uptime_sec` сбрасывается при перезапуске — по нему видно, был ли сервис усыплён/перезапущен.

## 3. Keep-alive (free tier)

Free-инстанс Render засыпает без трафика — торговые циклы останавливаются до следующего
запроса. Нужен **внешний** монитор:

1. UptimeRobot / Better Stack / cron-job.org.
2. Пинг **каждые 5 минут**: `GET` (или `HEAD`) `https://trading-bot-mu99.onrender.com/api/health`.
3. Ожидание — HTTP 200 и `{"status":"ok", ...}`.

Keep-alive намеренно не встроен в код и не связан с торговой логикой.

## 4. Секреты, сессии и ключи

| Переменная | Назначение |
|---|---|
| `TOKEN_ENCRYPTION_KEY` | Fernet-ключ для шифрования OKX-ключей пользователей в БД |
| `JWT_SECRET` | Секрет подписи JWT-сессий (рекомендуется задать отдельно) |
| `DASHBOARD_PASSWORD` | Пароль администратора |
| `DATABASE_URL` | PostgreSQL (Neon): `postgresql://…?sslmode=require` |

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Правила:

- Сессии — самодостаточные **JWT** (24 ч), поэтому переживают перезапуск и «сон» Render,
  пока секрет стабилен между деплоями.
- Если `TOKEN_ENCRYPTION_KEY` не задан, ключ детерминированно выводится из `JWT_SECRET`
  или `DASHBOARD_PASSWORD`; при полном отсутствии секретов сервис **не стартует**
  (fail-closed).
- ⚠ **Смена `DASHBOARD_PASSWORD` или `JWT_SECRET` при незаданном `TOKEN_ENCRYPTION_KEY`
  делает ранее сохранённые OKX-ключи нечитаемыми.** Задавайте `TOKEN_ENCRYPTION_KEY`
  явно и не меняйте его.
- Все секреты в `render.yaml` помечены `sync: false` и задаются в дашборде Render вручную.

## 5. Смоук-чеклист после деплоя

1. Открыть сайт → Guest → дашборд грузится, виден бейдж режима.
2. Перезапустить/передеплоить сервис → обновить страницу **без** повторного логина —
   сессия жива (JWT).
3. Монитор зелёный на `/api/health`.
4. Админ-логин → видны «Настройки»; guest их не видит.
5. Проверить `bots.ai: true` и состояние `live_mirror` — после редеплоя бот и зеркало
   должны подняться автоматически, если были включены до него.

---

## Архив заметок

### 2026-08-16 — Stage 1: sessions + keep-alive

- JWT-сессии вместо in-memory карт (`backend/app/services/auth.py`).
- Guest-режим: `POST /api/auth/guest` выдаёт `role=guest`; мутирующие роуты требуют админа;
  на 401 SPA редиректит на `/login?reason=session`.
- Health-полезная нагрузка пополнена `uptime_sec`, флагами ботов (тогда —
  `rotation|impulse|validation`), `auth: "jwt"`. Сейчас боты в `bots` — `ai`, `ai_scale`,
  `rotation`, `impulse`, `validation`, `scalp`, `vwap_rev`, `smart_money`, из них активен
  только `ai` (остальные retired).
- Автостарт стратегий на каждом старте процесса (включая пробуждение Render), ошибки
  изолированы: падение одного бота не блокирует остальные.

### 2026-08-12

Проверить, что закрытые сделки сохраняются между передеплоями (историческая задача;
решение — PostgreSQL вместо локального SQLite + таблица `exchange_close_trades`).
