# Аудит и бэклог качества

Источники: полный аудит от **2026-10-12** (см. [HISTORY.md](HISTORY.md)) + ревизия
структуры и обвязки, выполненная 2026-10-12. Объём на момент аудита: backend 22 047 строк
Python, frontend 6 235, 94 роута, 13 таблиц.

**Главный вывод аудита:** критических дефектов и дыр в авторизации нет; логика LIVE-зеркала
зафиксирована runtime-проверками и тестами. Технический долг — четырёх видов: наблюдаемость,
ручное управление безопасностью, фронтенд без отмены запросов, монолиты.

---

## Этап 1 — тесты и структура ✅ (2026-10-12)

- [x] **pytest в CI** — [.github/workflows/ci.yml](../.github/workflows/ci.yml) теперь
      ставит зависимости и прогоняет `pytest`; раньше CI делал только `py_compile`,
      из-за чего тесты «сгнили».
- [x] **4 сгнивших теста PnL** переписаны под актуальный контракт (эпоха `2026-09-01`,
      `ais*` не относится к AI, безымянные сделки не считаются) и расширены до 24 кейсов.
- [x] **Единый тестовый рут** `tests/` + [pytest.ini](../pytest.ini)
      (`pythonpath=backend`, `testpaths=tests`); `backend/tests/` удалён.
- [x] **Структура документации**: всё в [docs/](.), отчёты исследований — в
      [docs/research/](research/), добавлены [README.md](../README.md),
      [ARCHITECTURE.md](ARCHITECTURE.md), этот файл.
- [x] **Чистка папки**: удалены устаревшие снапшоты бэкапов (~2,4 ГБ) и закрыты
      `.gitignore`-дыры (`_analysis/`, `_backups/`, `backup_*.tar.gz`, `__pycache__`).

## Этап 2 — мёртвая обвязка и конфигурация

- [ ] **`render.yaml`**: убрать мёртвые `SIGNAL_PRICE_STARS`, `SIGNAL_PLAN_DAYS`,
      `SIGNAL_INVITE_DAYS`, `SM_EXECUTION_DISABLED`; `TRACKER_URL` ведёт на retired
      `/api/tracker` → либо удалить переменную, либо убрать кнопку «Live results» из
      меню бота.
- [ ] **`.env.example`**: убрать `SESSION_SECRET`, `AI_ONLY_MODE` (в коде жёстко `True`),
      `ALLOW_INSECURE_SECRETS`.
- [ ] **Битый импорт** `app.services.smart_money_ledger` в
      [main.py:5758](../backend/app/main.py) — модуля нет, ошибка глотается `try/except`.
- [ ] **`backend/static/`** — закоммиченная устаревшая сборка SPA (в ней страницы
      `BacktestPage`, `ChartPage`, `SmartMoneyPage`, `TrackerPage`, которых уже нет).
      Обновить из `frontend/dist` или перестать хранить в git.
- [ ] **Зависимости**: `ccxt`, `aiohttp`, `beautifulsoup4`, `yt-dlp` в
      [requirements.txt](../backend/requirements.txt) не используются; `websockets` —
      только мёртвым `subscribe_ticker()`.
- [ ] **Мёртвый код**: 13 неиспользуемых импортов, 7 невызываемых функций в
      `legacy_stubs.py`, пустой пакет `backend/app/routes/`, мёртвый модуль `alerts.py`,
      мёртвые ключи i18n (`scalp.*`, `nav.smartMoney`, `nav.backtest`, `nav.chart`).
- [ ] **`.deploy_trigger`** — файл-артефакт форс-передеплоя, никем не читается.
- [ ] **Персистентность**: в `render.yaml` нет `disk:` → состояние в `/tmp`/`cwd/data`
      (журнал AI, blacklist, клеймы) теряется при передеплое. Решить: Render Disk или
      перенос в Postgres.

## Этап 3 — хрупкие места безопасности

- [ ] **Декларативный auth вместо трёх ручных списков.** Сейчас `ADMIN_ONLY_PATHS`
      (~45 путей), `ADMIN_ONLY_PREFIXES`, `GUEST_FORBIDDEN_PREFIXES` надо синхронизировать
      вручную, плюс 56 из 94 роутов дублируют проверку через `Depends(require_admin)`.
      План: `@require_role("admin")` как единственный источник правды + при старте
      сверять список зарегистрированных роутов с декораторами и падать при расхождении
      (тот же приём, что уже сработал для зеркала).
- [ ] **`GET /api/startup-log` публичен** — раскрывает версии и пути. Закрыть админом.
- [ ] **`GET /api/ai/status` публичен** — осознанное решение (нужен Mini App), но отдаёт
      конфигурацию LLM; пересмотреть состав полей.

## Этап 4 — наблюдаемость

- [ ] **339 `except Exception`, из них ~152 молчаливых.** Начать с 20 мест, влияющих на
      деньги и зеркало: логировать с контекстом вместо `pass`.
- [ ] **`Dashboard.jsx`: `.catch(() => null)` на всех 8 загрузках** → пустой экран без
      сообщений. Показывать баннер «данные устарели».
- [ ] Опросы `1s/30s` без учёта `document.visibilityState` (кроме Dashboard) — жгут квоту
      Render и мешают сну free tier.

## Этап 5 — фронтенд

- [ ] **0 `AbortController` в проекте**: гонки при размонтировании и перезапись свежих
      данных устаревшими в Dashboard, BotsPage, SettingsPage, HistoryPage, AdminPage.
      Ввести хук `useApi` с отменой и `cancelled`-гардом.
- [ ] `pnlClass()` продублирована в `MiniAppPage.jsx` и `StatsPage.jsx` — вынести в
      `components/ui.jsx`.
- [ ] Разбить `Dashboard.jsx` (1 694 строки) на `Dashboard` + `PositionsPanel` +
      `TradesPanel` + `PnlCards`.

## Этап 6 — структура кода

- [ ] `main.py` 7 257 строк → переносить роуты в `backend/app/routers/` по 10–15 за
      коммит (пакет создан и ждёт).
- [ ] Вынести периодическую синхронизацию OKX bills из обработчика запроса в фоновую
      задачу — уберёт пики ~7 с на `/api/ai/status`.
- [ ] Два event loop (`ai_strategy` работает в своём потоке) + 17 module-level кэшей в
      `main.py` без блокировок — при рефакторинге держать в голове.

---

## Что проверено и признано чистым (не переделывать)

- Дублирующихся эндпоинтов нет (94 уникальных).
- Дыр в авторизации нет: `/api/live/status|trades` публичны намеренно, guest-ограничения
  работают, `/api/debug/` и `/api/admin/` закрыты префиксами.
- Водопад на дашборде устранён (`Promise.all`).
- OKX-клиент полностью асинхронный, синхронных вызовов нет.
- Mutable default arguments и `except:` без типа отсутствуют.
- Индексы PG и batch-upsert в `database.py` корректны.
