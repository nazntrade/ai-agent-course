# PLAN — Day 20: межсерверная оркестрация MCP (мониторинг и уведомления)

План реализации для `docs/specs/day-20-multi-mcp-orchestration/SPEC.md`. Пункты
трассируются к критериям `ACCEPTANCE.md` (`D20-01 … D20-32`).

**Статус:** реализация дня 20 выполнена; UNIT, INT и RESTART подтверждены, LIVE/UI
проверяются `test.bat acceptance`, реальный VPS-деплой и реальный Telegram не
выполнялись. Доделка добавила периодическую сводку (D20-30), привязку к заданию A
`source_task_id` (D20-31) и деплой-гейт A=9/B=6 (D20-32).

Имена, помеченные **предлагаемое**, были отсутствующими на момент планирования и
созданы этой задачей. Существующие имена взяты из кода без изменений.

## 1. Что не трогается

* Инструменты и контракты сервера A: `mcp_server/tools.py`,
  `mcp_server/server.py`, `mcp_server/tasks.py`, `mcp_server/scheduler.py`,
  `mcp_server/reports.py`, `mcp_server/web_search.py` (кроме реиспользования
  `normalize_url`), `storage/db.py`, `storage/tasks.py`, `storage/reports.py`.
* Существующие `.bat` (`test.bat`, `smoke_test.bat`, `run_app.bat`,
  `setup.bat`). **Осознанное исключение:** `deploy_vps.bat`/`deploy_vps.sh`
  расширены под день 20 (сервис `day16-notifier`, БД B вне Git-каталога,
  Telegram только у B, порядок рестарта A→B→backend, деплой-гейт A=9/B=6,
  `DEPLOY_STATUS: PASS`). На VPS ничего не запускалось (SPEC §15, D20-32).
* Governance-файлы (`AGENTS.md`, `PROJECT_RULES.md`, `opencode.json`,
  `.opencode/**`), AI-SERVER-Control-Center, AI-Project-Templates.
* Формат SSE-событий дня 16–19 (кроме **добавления** поля `server`), trace-цепочка
  arithmetic/search/tasks/composition, `chat_id`-инжекция, лимит 5 чатов,
  discovery CLI.
* `agent/provider.py`, `agent/tool_schema.py::HIDDEN_INJECTED_ARGUMENTS`
  (переиспользуется, не расширяется).

Осознанные изменения существующего кода: `agent/mcp_adapter.py` (необязательное
поле `McpTool.server`), `agent/orchestrator.py` (`server` в trace/SSE,
`trigger`, аддитивная инструкция про `source_task_id`), `agent/server.py` (hub +
новые endpoints + аддитивное `tool_names`), `agent/settings.py` (настройки B),
`static/index.html`, `static/app.js`, `.env.example`, `notifier_server/watches.py`
(сводка/`matched_items`/`summary_due`), `notifier_server/tools.py` (docstrings),
`agent/monitor.py` (per-watch `task_id`, `unknown_task`, второй summary-ход),
`harness/notifier_restart.py`, `harness/live_e2e.py`,
`harness/check_deploy_tools.py`, `tests/support/fake_telegram.py`, тесты дня 20.
Осознанно расширены `deploy_vps.bat`/`deploy_vps.sh` (SPEC §15).

Осознанная изоляция необязательного слота B (`MCP_NOTIFIER_URL=""`), чтобы
регрессии дней 16–19 не зависели от второго сервера:
`tests/integration/test_backend_live.py`, `harness/scheduler_restart.py`,
`harness/reports_restart.py`.

## 2. Новые и изменённые файлы

| Файл | Статус | Назначение | Критерии |
| --- | --- | --- | --- |
| `notifier_server/__init__.py` | NEW | `SERVER_NAME`, `SERVER_VERSION` B (по образцу `mcp_server/__init__.py`) | D20-01 |
| `notifier_server/server.py` | NEW | сборка MCP-сервера B, endpoint `/mcp`, порт 8766, регистрация 6 инструментов; `python -m notifier_server` | D20-01, D20-05 |
| `notifier_server/tools.py` | NEW | 6 тонких инструментов с model-facing docstrings и `chat_id=""` | D20-05…D20-09, D20-13 |
| `notifier_server/watches.py` | NEW | сервис: критерий, baseline (`ok`/`empty`), `evaluate_run` без исключений на данных; расписание: `pending`/`error` двигают только `next_check_at` (без `last_check_at`), baseline и обычная проверка без новых items — `last_check_at`/`next_check_at`, при `should_notify=true` расписание не двигается; дедуп/retry, period_key, статусы доставок; **сводка**: `summary_due`/`next_summary_at` в списке и `evaluate_run`, `matched_items`, честная пустая сводка, `period_key="summary:<floor(now/interval)>"` | D20-06…D20-10, D20-17, D20-19, D20-30 |
| `notifier_server/config.py` | NEW | env `NOTIFIER_DB_PATH`, `TELEGRAM_*`, `NOTIFIER_TELEGRAM_API_BASE_URL`, `NOTIFIER_LOAD_DOTENV`, tick clamp; token вне `repr` | D20-11, D20-13, D20-18 |
| `notifier_server/db.py` | NEW | схема B, `SCHEMA_VERSION`, forward-only `MIGRATIONS`, версия-gate | D20-11 |
| `notifier_server/repository.py` | NEW | репозиторий `watches`/`seen_items`/`deliveries`; UNIQUE `(watch_id,kind,period_key)`; retry `UPDATE` (`attempts+1`, `updated_at`) | D20-09, D20-11, D20-12 |
| `notifier_server/telegram.py` | NEW | **единственная** сетевая граница B; base URL инжектируется, loopback-фейк в тестах | D20-13 |
| `agent/mcp_hub.py` | NEW (предлаг.) | `McpHub`: `probe_servers()` (параллельно, `asyncio.gather`) + `probe_server(label)` → `ServerProbe(label,status,tools)`; A-first протокольные методы; маршрутизация `call_tool` | D20-02, D20-04, D20-24 |
| `agent/mcp_adapter.py` | EDIT | необязательное `McpTool.server` (default `None`); `McpStatus` и `FakeMcpClient` не меняются | D20-02 |
| `agent/orchestrator.py` | EDIT | `run(..., *, trigger="chat", watch_id=None, system_prompt=None, allowed_tools=None, on_tool_result=None, require_result=None, injected_arguments=None)`; `server` в `tool_selected`/`tool_completed`; `monitor_incomplete` вместо `request_done`; `request_start.trigger`; аддитивная инструкция `SYSTEM_PROMPT` про `source_task_id` | D20-03, D20-07, D20-14, D20-24, D20-31 |
| `agent/monitor.py` | NEW (предлаг.) | host-side монитор: `ChatService.list_chats()` + host→B `list_notification_watches`, фильтр `active`/`next_check_at<=now`, запуск того же `Orchestrator`; **per-watch** инжект `task_id` из `source_task_id`, `unknown_task` без вызова A; второй ход сводки со своим `require_result` | D20-14, D20-20, D20-30, D20-31 |
| `agent/settings.py` | EDIT | `mcp_notifier_url`, `notifier_monitor_enabled`, `notifier_monitor_tick_seconds` (default 30, clamp 1..3600); **не** читает `TELEGRAM_*` и `NOTIFIER_LOAD_DOTENV` (их читает только процесс B) | D20-14, D20-18 |
| `agent/server.py` | EDIT | `McpHub` вместо одного клиента; `GET /api/mcp/servers` через один `probe_servers()` (+ аддитивное `tool_names`); `GET /api/chats/{chat_id}/watches` (404 до B; `available:false` при B down); `create_app(..., enable_monitor=False)` и запуск/останов монитора в `lifespan` | D20-04, D20-15, D20-32 |
| `agent/notifier_client.py` | NEW (предлаг.) | host→B чтение подписок/доставок для API/UI с обработкой недоступности B | D20-15 |
| `agent/__main__.py` | EDIT | передавать `enable_monitor=True` в `create_app` для реального процесса backend | D20-14 |
| `agent/chats.py` | EDIT | `list_chats()` уже есть; добавляется только при необходимости для UI — иначе не менять | D20-15 |
| `static/index.html` | EDIT | `MCP status` на два сервера; панель `Notification watches` | D20-16, D20-22 |
| `static/app.js` | EDIT | рендер обоих серверов, `[A]`/`[B]` в `Technical details`, загрузка панели подписок, статус доставки из B | D20-16, D20-22 |
| `.env.example` | EDIT | новые переменные (без настоящих значений): `MCP_NOTIFIER_*`, `NOTIFIER_DB_PATH`, `TELEGRAM_*`, `NOTIFIER_TELEGRAM_API_BASE_URL`, `NOTIFIER_MONITOR_ENABLED`, `NOTIFIER_MONITOR_TICK_SECONDS`, `NOTIFIER_LOAD_DOTENV` | D20-18 |
| `tests/test_notifier_watches.py` | NEW | UNIT: критерий, baseline (`ok`/`empty`), malformed/unknown_watch без исключения, отпечатки, retry failed/`attempts`, контентный `period_key` (`new_items`) vs временной (`summary`), разные материалы в одном периоде, накопленные прогоны (несколько `run`-payload); **сводка**: границы `summary_due`/`next_summary_at`, честная пустая сводка, дедуп периода, `failed`→сводка остаётся due→`sent`, `matched_items` включая seen | D20-06…D20-10, D20-17, D20-19, D20-28, D20-29, D20-30 |
| `tests/test_notifier_tools.py` | NEW | UNIT: 6 инструментов B, structured-ответы, валидация аргументов | D20-05, D20-06 |
| `tests/test_notifier_server.py` | NEW | UNIT: сборка B, регистрация 6 инструментов, endpoint `/mcp`, отсутствие импорта `agent.*` | D20-01, D20-05 |
| `tests/test_notifier_db.py` | NEW | UNIT: схема B, UNIQUE, `attempts`/`updated_at`, версия-gate | D20-11, D20-12 |
| `tests/test_notifier_config.py` | NEW | UNIT: env B, `NOTIFIER_LOAD_DOTENV`, tick clamp, токен вне `repr` | D20-13, D20-18 |
| `tests/test_notifier_telegram.py` | NEW | UNIT: санитизация, `not_configured`, токен вне `repr`/логов | D20-13 |
| `tests/test_monitor.py` | NEW | UNIT: перечисление подписок, прерывание тика при B-down, полнота/`monitor_incomplete`, `require_result`; per-watch инжект `task_id` из `source_task_id`, `unknown_task` без вызова A, второй summary-ход и `summary_result_incomplete` | D20-10, D20-14, D20-30, D20-31 |
| `tests/test_deploy_tools.py` | EDIT | UNIT: `tools_match`/`registered_tool_names` (обратная совместимость) + `servers_match` (двухсерверный ответ, disconnected, stale/extra, дубли меток) | D20-32 |
| `tests/test_mcp_hub.py` | NEW | UNIT: `probe_servers` параллельно, `ServerProbe`, коллизия имён → `protocol`, пустой URL → `not_configured`, A-first | D20-02, D20-04, D20-24 |
| `tests/test_orchestrator.py` | EDIT | `server` в trace/SSE, `trigger`, `require_result`, monitor-промпт, legacy без `server` | D20-03, D20-14 |
| `tests/test_notifier_api.py` | NEW | UNIT: `/api/mcp/servers` (один probe), `/watches` 404 до B, `available:false` при B down | D20-04, D20-15 |
| `tests/test_notifier_ui.py` | NEW | source-guard: оба сервера, `[A]`/`[B]`, панель без периодического опроса | D20-16 |
| `tests/test_settings.py` | EDIT | настройки B, отсутствие чтения `TELEGRAM_*`/`NOTIFIER_LOAD_DOTENV` в `agent/settings.py` | D20-13, D20-18 |
| `tests/support/fakes.py` | EDIT | `McpTool.server` в тестовых данных; fake B (при необходимости); `FakeMcpClient` не меняется | D20-02, D20-05 |
| `tests/support/fake_telegram.py` | NEW | loopback-фейк Telegram для INT/LIVE (пишет payload для проверки URL) | D20-13, D20-21 |
| `tests/integration/test_notifier_live.py` | NEW | INT: реальные A и B, реальный HTTP, `tools/list` каждого, fake Telegram; несколько накопленных прогонов A → обрабатывается последний (no backfill); два разных материала в одном периоде → два `period_key`/две `sent`, повтор → `duplicate` | D20-01, D20-05, D20-21, D20-28, D20-29 |
| `harness/notifier_restart.py` | NEW | RESTART: состояние B переживает рестарт; повтор не создаёт дубль; накопленные за простой прогоны → только последний; подписка с `source_task_id`, две подписки/два задания без смешения, сводка (`sent`→`duplicate` после рестарта), `failed`→`sent` на той же строке | D20-12, D20-28, D20-30, D20-31 |
| `harness/check_deploy_tools.py` | EDIT | деплой-гейт: `registered_tool_names`/`tools_match` (обратная совместимость) + двухсерверный `servers_match(response, {A: source, B: source})` по `connected`/`tool_names` | D20-32 |
| `harness/live_mcp.py` | EDIT | поднять B-процесс; `MCP_NOTIFIER_TEST_PORT` (default 8768), handshake B по server name; `require_free_ports` + порт B; `EXPECTED_MCP_POSTS_PER_PROBE = 2 × серверов`; окно с `NOTIFIER_MONITOR_ENABLED=0`; `NOTIFIER_LOAD_DOTENV=0` | D20-01, D20-21, D20-25 |
| `harness/live_e2e.py` | EDIT | сценарий `notifications` (LIVE A→B, watch создаётся моделью с непустым `source_task_id`) и monitor-LIVE **без гонки baseline**: watch создаётся host→B с `source_task_id` задания-результатов, harness поглощает baseline пустым `evaluate_run` (без сообщения), затем реальный monitor-тик читает именно это задание и доставляет `new_items`; `monitor_run_scope` требует `task_id == source_task_id`; UI обоих серверов | D20-14, D20-20, D20-22, D20-31 |
| `harness/notifier_real_live.py` | NEW | REAL Telegram opt-in (`NOTIFIER_REAL_ALLOW=1`) | D20-23 |
| `harness/acceptance.py` | EDIT | новый шаг/агрегация статусов дня 20 | D20-20…D20-23 |
| `deploy_vps.bat` | EDIT | **осознанно** под день 20: проверка, что коммит содержит `notifier_server/server.py`; деплой B-сервиса | D20-32 |
| `deploy_vps.sh` | EDIT | **осознанно** под день 20: `day16-notifier`, БД B вне Git-каталога, `/etc/day16/notifier.env` (root/600), запрет `TELEGRAM_*` в проектном `.env`, drop-in B, порядок рестарта A→B→backend, проверка `/api/mcp/servers` (A=9/B=6), `DEPLOY_STATUS: PASS`; SHA/fast-forward сохранены | D20-32 |
| `docs/vps-setup-day20.md` | NEW | инструкция первичной настройки VPS (пользователь/каталоги/env/unit B); без секретов и без выполнения на VPS | D20-32 |
| `docs/openapi.json` | EDIT (regen) | `/api/mcp/servers` (+`tool_names`), endpoint подписок | D20-15, D20-32 |
| `docs/specs/day-20-multi-mcp-orchestration/*` | NEW | этот комплект | D20-26, D20-27 |
| `README.md` (week-04) | EDIT (Developer) | раздел «День 20»: сводка vs `new_items`, `source_task_id`/`unknown_task`, ошибка Telegram/рестарт, VPS-чек-лист | D20-13, D20-18, D20-30, D20-31, D20-32 |

Ни один `.bat` не изменяется.

## 3. Порядок реализации

Последовательность обязательна: каждый шаг опирается на предыдущий.

1. **B-пакет и его тесты.** `notifier_server/{__init__,config,db,repository,
   watches,telegram,tools,server}.py` → `tests/test_notifier_watches.py`,
   `tests/test_notifier_tools.py`, `tests/test_notifier_server.py`,
   `tests/test_notifier_config.py`, `tests/test_notifier_telegram.py`,
   `tests/test_notifier_db.py`. Контракты: baseline `ok`/`empty`, `evaluate_run`
   без `ToolError` на данных, retry `UPDATE` (`attempts+1`), контентный
   `period_key` для `new_items` (разные материалы в одном периоде — разные
   ключи), временной `period_key` только для `summary`, накопленные прогоны
   (обрабатывается последний), `NOTIFIER_LOAD_DOTENV`.
   Уровень: UNIT. Критерии: D20-05…D20-13, D20-17, D20-18, D20-19, D20-28,
   D20-29.
2. **McpHub и `server` в trace/SSE.** `agent/mcp_adapter.py` (поле `server`),
   `agent/mcp_hub.py` (S1: `probe_servers`/`probe_server`/`ServerProbe`, A-first,
   коллизия → `protocol`, пустой URL → `not_configured`), `agent/orchestrator.py`
   (S2: per-server `mcp_connect`, агрегированный `mcp_list_tools.per_server`,
   `server` только когда известен), `agent/settings.py`, `agent/server.py` →
   `tests/test_mcp_hub.py`, `tests/test_orchestrator.py`,
   `tests/test_settings.py`, `tests/support/fakes.py`.
   Уровень: UNIT. Критерии: D20-02, D20-03, D20-04, D20-24.
3. **Обвязка A/промпт.** Дополнить `SYSTEM_PROMPT` в `agent/orchestrator.py`
   описанием цепочки A→B и monitor-промпта; подтвердить, что инструменты A не
   изменены. Уровень: UNIT. Критерии: D20-03, D20-14, D20-24.
4. **Монитор.** `agent/monitor.py` (S7/S8): `Orchestrator.run(..., trigger=
   "monitor", watch_id, system_prompt, allowed_tools, require_result)`;
   перечисление `ChatService.list_chats()` + host→B `list_notification_watches`;
   прерывание тика при первом B-down. Запуск/останов в `lifespan`
   `agent/server.py` через `create_app(..., enable_monitor=...)`;
   `agent/__main__.py` передаёт `enable_monitor=True`; флаг
   `NOTIFIER_MONITOR_ENABLED`. → `tests/test_monitor.py`.
   Уровень: UNIT. Критерии: D20-10, D20-14.
5. **API/UI.** `agent/server.py` (`GET /api/mcp/servers` — один `probe_servers()`;
   `GET /api/chats/{chat_id}/watches` — 404 до B при неизвестном чате,
   `available:false` при B-dow), `agent/notifier_client.py`, `static/*` (оба
   сервера, `[A]`/`[B]`, панель без периодического опроса) →
   `tests/test_notifier_api.py`, `tests/test_notifier_ui.py`.
   Уровень: UNIT + source-guard. Критерии: D20-04, D20-15, D20-16.
6. **Harness/tests.** `tests/support/fake_telegram.py`,
   `tests/integration/test_notifier_live.py` (INT D20-28: несколько прогонов A →
   обрабатывается последний; INT D20-29: два разных материала в одном периоде →
   два `period_key`/две `sent`, повтор того же множества → `duplicate`),
   `harness/notifier_restart.py`
   (S6: retry `failed` повтором, не дублем; D20-28), `harness/live_mcp.py` (S10:
   `MCP_NOTIFIER_TEST_PORT` default 8768, handshake B по server name,
   `require_free_ports` + порт B, `EXPECTED_MCP_POSTS_PER_PROBE = 2 × серверов`,
   окно с `NOTIFIER_MONITOR_ENABLED=0`), `harness/live_e2e.py` (монитор-LIVE **без
   гонки baseline**: сначала A-таск с `EMPTY_MARKER` и `MCP_TASK_TICK_SECONDS=0.5`,
   дождаться `get_latest_search_run.status=="empty"`, и только затем watch;
   payload fake Telegram), `harness/acceptance.py`, `harness/notifier_real_live.py`,
   `tests/test_harness.py`.
   Уровни: INT + RESTART + LIVE + UI + REAL. Критерии: D20-01, D20-12,
   D20-20…D20-23, D20-25, D20-28, D20-29.
7. **README (Coordinator, не Developer).** Раздел «День 20»: запуск B, env,
   панель, начало диалога с ботом. Критерии: D20-13, D20-18.
8. **Деплой и сводка (доделка дня 20).** Осознанно расширить
   `deploy_vps.bat`/`deploy_vps.sh` (сервис `day16-notifier`, БД B вне
   Git-каталога, Telegram только у B, порядок рестарта A→B→backend, деплой-гейт
   A=9/B=6 через `harness/check_deploy_tools.py`), добавить
   `docs/vps-setup-day20.md`; на VPS **ничего не запускать**. Периодическая
   сводка и привязка `source_task_id` — в `notifier_server/watches.py`,
   `agent/monitor.py`, `agent/orchestrator.py` (аддитивная инструкция),
   `agent/server.py` (`tool_names`). Критерии: D20-30, D20-31, D20-32.
9. **OpenAPI и полный прогон.** Сначала перегенерировать `docs/openapi.json`
   (`test.bat openapi`) — **до** acceptance, иначе `test_openapi_snapshot` даст
   drift. Затем `test.bat` → `smoke_test.bat` → `test.bat acceptance`.

Шаг REAL в acceptance (`harness/acceptance.py`) без `NOTIFIER_REAL_ALLOW=1`
печатает `NOTIFIER_REAL_STATUS: BLOCKED` и **не** валит прогон (не FAIL).

## 4. Ключевые технические решения

* **Изоляция процессов.** B — отдельный пакет без импорта `agent.*`; свои
  зависимости и своя БД. Это повторяет границу, уже принятую для `mcp_server`
  (`mcp_server/config.py` явно документирует запрет импорта `agent.*`).
* **Хост-граница `McpHub`.** Оркестрация и маршрутизация живут в хосте, а не в
  серверах: A и B друг о друге не знают, прямой вызов A→B невозможен по
  построению. `McpTool.server` — необязательное поле, поэтому существующие
  `FakeMcpClient` (`tests/support/fakes.py`) и весь день 16–19 не ломаются.
* **Baseline как контракт.** Первый `evaluate_run` помечает seen и не уведомляет;
  пользователь получает только будущие новости.
* **Единственная точка отправки.** `send_notification` — и отправка, и запись
  seen-set/delivery; идемпотентность по `(watch_id, kind, period_key)` делает
  повторный тик и рестарт безопасными.
* **Общий pipeline для чата и монитора.** Монитор использует тот же
  `Orchestrator` и провайдер, отличаясь только `trigger` и подмножеством
  инструментов, поэтому межсерверная цепочка видна в trace в обоих каналах.
* **Переиспользование `normalize_url`.** Отпечатки строятся существующей
  функцией `mcp_server/reports.py::normalize_url`; копия не создаётся (D20-17).
* **Сохрание семантики A в API.** `/api/mcp/status` и `/api/mcp/tools` остаются
  про сервер A; оба сервера видны через новый `/api/mcp/servers` (D20-15).

## 5. Тесты по уровням

* **UNIT** (без сети, `test.bat`): инструменты и сервис B, критерий, baseline,
  отпечатки и `normalize_url`, идемпотентность deliveries, контентный
  `period_key` (`new_items`) vs временной (`summary`), разные материалы в одном
  периоде, накопленные прогоны, санитизация и `not_configured` Telegram, токен вне
  `repr`, hub-агрегация и маршрутизация, поле `server` в trace/SSE, `trigger`,
  monitor-промпт, schema B, `/api/mcp/servers` (+`tool_names`), недоступность B,
  source-guard UI, OpenAPI drift; **сводка** (`summary_due`/`next_summary_at`/
  `matched_items`/пустая сводка/дедуп), per-watch `task_id`/`unknown_task`,
  двухсерверный `servers_match`. Критерии: D20-02…D20-11, D20-13…D20-19, D20-24,
  D20-28, D20-29, D20-30, D20-31, D20-32.
* **INT** (`smoke_test.bat`): реальные процессы A и B, реальный HTTP, реальный
  `tools/list` каждого сервера, loopback-фейк Telegram; несколько накопленных
  прогонов A → обрабатывается последний; два разных материала в одном периоде →
  два `period_key`/две `sent`, повтор того же множества → `duplicate`;
  `NOTIFIER_INTEGRATION_STATUS`.
  Критерии: D20-01, D20-05, D20-21, D20-28, D20-29.
* **RESTART** (`harness/notifier_restart.py`): состояние B (`watches`,
  `seen_items`, `deliveries`) переживает рестарт; повтор прогона не создаёт дубль;
  накопленные за простой прогоны → только последний (no backfill); две подписки с
  разными `source_task_id` не смешиваются; сводка после рестарта → `duplicate`;
  `failed`→`sent` на той же строке; `NOTIFIER_RESTART_STATUS`.
  Критерии: D20-12, D20-28, D20-30, D20-31.
* **Деплой-гейт** (UNIT + статический разбор): `harness/check_deploy_tools.py` и
  `deploy_vps.sh` проверяют `/api/mcp/servers` против `mcp_server/server.py` (9)
  и `notifier_server/server.py` (6). На VPS ничего не запускается.
  Критерий: D20-32.
* **LIVE** (`test.bat acceptance`): реальная локальная Qwen ведёт цепочку
  `get_latest_search_run(A)` → `evaluate_run(B)` → `send_notification(B)`; внешний
  Tavily и Telegram — фейки; в отчёте указывается фактическая модель
  (`AGENT_MODEL_NAME`) и что это **не** модель ролей Control Center.
  `NOTIFICATIONS_LIVE_STATUS`, `NOTIFICATIONS_MONITOR_LIVE_STATUS`.
  Критерии: D20-20, D20-24.
* **UI** (Playwright, `test.bat acceptance`): `MCP status` показывает оба сервера;
  строки `Technical details` помечены `[A]`/`[B]`; панель `Notification watches`;
  `NOTIFIER_SERVERS_UI_STATUS`, `NOTIFICATION_UI_STATUS`. Критерии: D20-16,
  D20-22.
* **REAL** (opt-in, `NOTIFIER_REAL_ALLOW=1`): `harness/notifier_real_live.py`;
  иначе `BLOCKED`. Реальный Tavily остаётся `BLOCKED`. Критерий: D20-23.
* **Регрессии дней 16–19**: `test.bat`, `smoke_test.bat`, `test.bat acceptance`.
  Критерий: D20-25.
* **VPS**: `deploy_vps.bat`/`deploy_vps.sh` осознанно расширены и проверяются
  UNIT-парсером контрактов; сам деплой на VPS — ручной чек-лист (SPEC §15),
  ничего не запускалось. Критерии: D20-27, D20-32.

## 6. Точки входа

Новые `.bat` не добавляются (файлы защищены); проверки встраиваются в
существующие режимы через `harness/`:

```
test.bat                  UNIT
smoke_test.bat            INT + RESTART (NOTIFIER_INTEGRATION_STATUS, NOTIFIER_RESTART_STATUS)
test.bat acceptance       LIVE + UI (NOTIFICATIONS_LIVE_STATUS,
                          NOTIFICATIONS_MONITOR_LIVE_STATUS,
                          NOTIFIER_SERVERS_UI_STATUS, NOTIFICATION_UI_STATUS)
test.bat openapi          обновление docs/openapi.json
Ручной opt-in REAL:       .venv\Scripts\python.exe harness\notifier_real_live.py
                          (только с NOTIFIER_REAL_ALLOW=1)
```

## 7. Риски и меры

| Риск | Мера |
| --- | --- |
| Коллизия имён инструментов A и B | UNIT-проверка непустого пересечения множеств имён; при коллизии — контролируемая ошибка конфигурации (D20-02) |
| `EXPECTED_MCP_POSTS_PER_PROBE` в `harness/live_mcp.py` рассчитан на один сервер | пересчитать на число сконфигурированных серверов; `.bat` не трогается (D20-01, D20-25) |
| Тесты/снимки жёстко ожидают `tools_count = 9` | эти тесты проверяют сервер A и **остаются на 9** (ACCEPTANCE §3, D20-25); их счётчики **не** обновляются, `tests/integration/*` и `tests/test_mcp_inprocess.py` остаются как есть; объединённые 15 проверяются только новыми UNIT-тестами hub (`tests/test_mcp_hub.py`) |
| Монитор по умолчанию on может сделать сетевой вызов в тестах | тесты изолируют сеть и подставляют fake Telegram; monitor в тестах выключен или замокан |
| Модель не вызовет цепочку A→B | monitor-промпт и docstrings; неполный ход → `monitor_incomplete`, следующий тик повторяет (D20-10, D20-14) |
| Ложная доставка по тексту модели | статус берётся из `delivery.status` B; UI не доверяет тексту LLL (D20-16) |
| Токен Telegram утечёт | единая сетевая граница, `repr=False`, санитизация ошибок, `not_configured` без сети (D20-13) |
| B недоступен на этапе INT/LIVE | hub не валит A; `/api/mcp/servers` даёт `connected:false`; `/watches` — `available:false` (D20-04, D20-15) |
| Регрессия дней 16–19 | существующие UNIT/INT/LIVE/UI сохранены и прогнаны; инструменты A не изменены (D20-25) |
| **Коллизия порта 8766**: `smoke_test.bat` (не меняем) задаёт `MCP_TEST_PORT=8766` для A, B не может взять default 8766 | отдельный `MCP_NOTIFIER_TEST_PORT` (default 8768), `require_free_ports([...])` с портом B, `MCP_NOTIFIER_URL` из него; default 8766 остаётся только для реального `run_app` (S10, D20-01, D20-21) |
| **Поглощение failed-доставок**: `duplicate` мог бы навсегда заблокировать повтор | `duplicate` только для строки `status='sent'`; `failed`/`not_configured` → `UPDATE` той же строки (`attempts+1`); `not_required` строку не создаёт (S6, D20-09, D20-12) |
| **Baseline**: первый прогон может быть `pending`/`error` и ошибочно «съесть» точку отсчёта | baseline поглощается только при `status in {"ok","empty"}`; `pending`/`error` не поглощают (S4, D20-07, D20-08) |
| **Монитор по умолчанию НЕ стартует в unit-тестах**: иначе фоновый поток и сетевой вызов | `create_app(..., enable_monitor=False)` по умолчанию; `agent/__main__.py` передаёт `enable_monitor=True`; `NOTIFIER_MONITOR_ENABLED`; тесты используют `False` и fake Telegram (S8/S9, D20-14) |
| **`verify_tools_listed` при двух серверах**: объединённый `tool_names` содержит и A, и B | верификатор ищет конкретный инструмент A в объединённом списке — совместим; `tools_count` проверяется как `per_server` + сумма 15 (S2, D20-03, D20-25) |
| **OpenAPI drift**: новые endpoints меняют схему | `test.bat openapi` перегенерирует `docs/openapi.json` **до** acceptance (PLAN §3 шаг 9, D20-15) |
| Ложная доставка по тексту модели | статус берётся из `delivery.status` B; LIVE PASS требует `status=="sent"` и URL из `fake_search.RESULT_URLS` в payload fake Telegram (S12, D20-09, D20-20) |

## 8. Решения Architect по прежним неясностям

Architect подтвердил дизайн со статусом `SPEC_GATE_STATUS: CHANGES_REQUIRED`; все
четыре прежние неясности закрыты и перенесены в SPEC:

1. **`mcp_connect`/`mcp_list_tools` при hub** — решено (S2): `mcp_connect` по
   записи на сервер, `mcp_list_tools` — одна агрегированная запись с
   `per_server: {"A":9,"B":6}`; совместимость с `verify_trace`/`verify_tools_listed`
   обоснована в SPEC §10.
2. **Форма `McpStatus`/`McpHub`** — решено (S1): `McpStatus` **не** расширяется;
   агрегация в новом `ServerProbe`; `probe_servers()`/`probe_server(label)`;
   протокольные методы A-first.
3. **Lifecycle монитора в unit-тестах** — решено: `create_app(..., enable_monitor=False)`
   по умолчанию, `agent/__main__.py` передаёт `enable_monitor=True`.
4. **Чтение подписок backend-ом** — решено (S8/S11): только host→B
   (`list_notification_watches`), БД B напрямую не открывается.

Открытых вопросов к Architect не осталось; до реализации спецификация полна.
