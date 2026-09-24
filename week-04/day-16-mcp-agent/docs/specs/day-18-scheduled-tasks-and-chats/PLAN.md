# PLAN — Day 18: фоновые задания и сохраняемые чаты

План реализации утверждается Architect и выполняется только после `SPEC_GATE_STATUS: PASS` и команды пользователя «ДЕЛАЕМ». Каждый пункт трассируется к критериям `ACCEPTANCE.md`.

## 1. Новые и изменённые файлы

| Файл | Статус | Назначение |
| --- | --- | --- |
| `storage/__init__.py` | NEW | общий пакет персистентности (только stdlib) |
| `storage/db.py` | NEW | `Database`, pragmas (WAL, FK, busy_timeout), schema v2, forward-only миграции (v1→v2), миграционный гейт |
| `storage/chats.py` | NEW | SQL-репозиторий чатов и сообщений (backend-владение) |
| `storage/tasks.py` | NEW | SQL-репозиторий заданий и прогонов, `max_results`, CAS-захват, prune runs |
| `agent/chats.py` | NEW | `ChatService`, `ChatError`, лимит 5, заголовки, окно контекста, задачи для UI |
| `agent/sessions.py` | EDIT | вместо in-memory `SessionStore` — `ChatSession` + `SessionRegistry` (per-chat lock) |
| `agent/server.py` | EDIT | новые endpoints чатов, `chat_id` в `/api/chat/stream`, единый формат ошибок |
| `agent/orchestrator.py` | EDIT | окно контекста, инжект `chat_id`, `SYSTEM_PROMPT` (+`max_results`), trace `context_messages` |
| `agent/tool_schema.py` | EDIT | `to_openai_tools(..., hidden_properties)` — скрытие `chat_id` от модели |
| `agent/settings.py` | EDIT | `db_path`, `chat_context_messages` |
| `mcp_server/config.py` | EDIT | `resolve_db_path`, `resolve_task_tick_seconds` |
| `mcp_server/tasks.py` | NEW | `TaskService` (create/list/latest/stop, claim, execute), валидация `max_results`, ленивый `default_task_service` |
| `mcp_server/scheduler.py` | NEW | `TaskScheduler` (daemon-поток, тик, устойчивость к ошибкам) |
| `mcp_server/tools.py` | EDIT | 4 тонких инструмента с docstrings (`schedule_search_task` +`max_results`); прежние не изменяются |
| `mcp_server/server.py` | EDIT | регистрация инструментов; запуск планировщика в `run()` |
| `static/index.html` | EDIT | панель чатов, панель заданий, `Clear chat` |
| `static/app.js` | EDIT | CRUD чатов, лимит, переключение, задания + авто-обновление панели (опрос 10 с), стриминг по `chat_id` |
| `static/styles.css` | EDIT | стили списка чатов и карточек заданий |
| `tests/support/fakes.py` | EDIT | `sample_tools()` на 7 инструментов |
| `tests/support/fake_search.py` | EDIT | `GET /__stats__` (счётчик запросов) для INT; маркер `__test_many__` (6 результатов) |
| `tests/test_storage.py` | NEW | UNIT схемы v2/прагм/миграции v1→v2/CAS/каскада |
| `tests/test_chats_api.py` | NEW | UNIT HTTP API чатов |
| `tests/test_tasks.py` | NEW | UNIT инструментов заданий (+валидация/clamp `max_results`, лимит на прогоне) |
| `tests/test_scheduler.py` | NEW | UNIT планировщика (due, catch-up, stop, cascade, no-backfill, CAS) |
| `tests/test_chats_tasks_ui.py` | NEW | source-guard UI (ids, подписи, авто-опрос панели, отсутствие `chat_id` в DOM-текстах) |
| `tests/test_api.py` | EDIT | `chat_id`, API чатов, окно контекста |
| `tests/test_orchestrator.py` | EDIT | инжект/скрытие `chat_id`, промпт, окно |
| `tests/test_tool_schema.py` | EDIT | `hidden_properties` |
| `tests/test_tools.py` | EDIT | инструменты заданий |
| `tests/test_mcp_inprocess.py` | EDIT | 4 новых инструмента, structured/`ToolError` |
| `tests/test_settings.py` | EDIT | `AGENT_DB_PATH`, `AGENT_CHAT_CONTEXT_MESSAGES` |
| `tests/test_openapi_snapshot.py` | EDIT | новые пути в ожидаемом контракте |
| `tests/test_harness.py` | EDIT | новые статусы/цепочки/гейты |
| `tests/integration/test_backend_live.py` | EDIT | чаты/задания по реальному HTTP |
| `tests/integration/test_mcp_live.py` | EDIT | 7 инструментов (если проверяется счёт) |
| `tests/integration/test_tasks_live.py` | NEW | INT: планировщик, изоляция, удаление, нет новых вызовов поиска |
| `harness/live_mcp.py` | EDIT | `AGENT_DB_PATH`, запуск INT-модуля, шаг рестартов |
| `harness/scheduler_restart.py` | NEW | `PERSISTENCE_RESTART_STATUS`, `SCHEDULER_RESTART_STATUS` |
| `harness/live_e2e.py` | EDIT | `--scenario tasks`, `run_ui_tasks_e2e`, `run_ui_chats_e2e` |
| `harness/acceptance.py` | EDIT | шаг tasks: `TASKS_LIVE_STATUS`, `CHATS_UI_STATUS`, `TASKS_UI_STATUS` |
| `harness/mcp_unavailable_e2e.py` | EDIT | создание чата через API, `chat_id` вместо `session_id` |
| `harness/tasks_real_live.py` | NEW | REAL opt-in (`TASKS_REAL_ALLOW=1`), `TASKS_REAL_STATUS` |
| `docs/openapi.json` | EDIT (regen) | `test.bat openapi` после реализации |
| `.gitignore` | EDIT | добавить `data/` |
| `.env.example` | EDIT | `AGENT_DB_PATH`, `AGENT_CHAT_CONTEXT_MESSAGES`, `MCP_TASK_TICK_SECONDS` |
| `README.md` (week-04) | EDIT | раздел «День 18» (структура — как в днях 16–17) |
| `docs/specs/day-18-scheduled-tasks-and-chats/*` | NEW | этот комплект |

Ни один `.bat` не изменяется.

## 2. Порядок реализации

1. **Хранилище и конфиг** (D18-01): `storage/*`, `agent/settings.py`, `mcp_server/config.py`, `.gitignore`, `.env.example` → `tests/test_storage.py`, `tests/test_settings.py`.
2. **Backend чатов** (D18-02…D18-07): `agent/chats.py`, `agent/sessions.py`, `agent/server.py` → `tests/test_chats_api.py`, обновление `tests/test_api.py`.
3. **Граница контекста и инжект** (D18-05, D18-11): `agent/orchestrator.py`, `agent/tool_schema.py` → `tests/test_orchestrator.py`, `tests/test_tool_schema.py`.
4. **MCP-задания и планировщик** (D18-10…D18-18): `mcp_server/tasks.py`, `mcp_server/scheduler.py`, `mcp_server/tools.py`, `mcp_server/server.py` → `tests/test_tasks.py`, `tests/test_scheduler.py`, `tests/test_tools.py`, `tests/test_mcp_inprocess.py`, `tests/support/fakes.py`.
5. **UI** (D18-19): `static/*` → `tests/test_chats_tasks_ui.py`.
6. **INT** (D18-01…D18-18): `tests/support/fake_search.py`, `tests/integration/*`, `harness/live_mcp.py`, `harness/scheduler_restart.py`.
7. **LIVE + UI + REAL** (D18-19…D18-22): `harness/live_e2e.py`, `harness/acceptance.py`, `harness/mcp_unavailable_e2e.py`, `harness/tasks_real_live.py`.
8. **Документация и контракт** (D18-24): `docs/openapi.json` (`test.bat openapi`), README недели.
9. **Полный прогон**: `test.bat` → `smoke_test.bat` → `test.bat acceptance`; ручные проверки REAL (opt-in) и VPS (оператором).

## 3. Ключевые технические решения (для Developer)

* **Ленивая БД.** `create_app()`, `MCPServer._build()`, `run()` и `tools/list` не открывают БД. Открытие — первая операция; схема создаётся один раз на процесс (`CREATE TABLE IF NOT EXISTS` в `BEGIN IMMEDIATE`). Это сохраняет `test.bat openapi` и in-process тесты без побочных файлов.
* **Короткие соединения.** `sqlite3.connect(path, timeout=5.0)` на операцию; `Row`, WAL, `foreign_keys=ON`, `busy_timeout=5000`. Соединение не живёт между потоками.
* **Один писатель на таблицу** (матрица §5.2 SPEC): backend пишет `chats/messages`; MCP — `tasks/runs`; backend удаляет чат каскадом; MCP читает `chats` только для валидации.
* **Планировщик** стартует в `MCPServer.run()` (не в `_build()`), чтобы in-process тесты не поднимали поток; тик по умолчанию 2 с; ошибки тика логируются и не фатальны.
* **CAS-захват слота** (см. SPEC §6.3) — идемпотентность при пересечении тиков и двух процессах; `next_run_at` продвигается **до** прогона; пропущенные слоты не бэкфиллятся.
* **Прогон не пересоздаёт задание**: `runs` пишется только при существующем `task_id` (проверка + FK; `IntegrityError` → отбросить).
* **`chat_id` инжектится после валидации** аргументов модели и исключается из model-facing схемы и UI/trace копии аргументов (`HIDDEN_INJECTED_ARGUMENTS` в `agent/tool_schema.py`/`agent/orchestrator.py`). Серверные инструменты требуют непустой `chat_id` и проверяют принадлежность задания чату.
* **Признак успеха прогона**: `ok` (count>0), `empty` (count=0), `error` (санитизированный текст). Сводку формирует модель из `get_latest_search_run`; фоновых вызовов модели нет.
* **Удаление**: `DELETE` без `force` при активных заданиях → 409; с `force` — одна транзакция `DELETE FROM chats WHERE id=?`; счётчик остановленных заданий — до удаления.
* **Trace**: `request_start` c `chat_id` и `context_messages`; событийный контракт не меняется.
* **Окно контекста**: `AGENT_CHAT_CONTEXT_MESSAGES=20` (clamp 2…100); полная история в БД/UI; без сжатия.

## 4. Тесты по уровням

* **UNIT** (`test.bat`): `tests/test_storage.py` (DDL v2, pragmas через повторное открытие, версия схемы, миграция v1→v2 на БД с данными, CAS, каскад, prune runs, конкурентные соединения); `tests/test_chats_api.py` (CRUD, лимит 409, clear, delete 409/force, 404, изоляция сообщений); `tests/test_tasks.py` (валидации, лимиты, `max_results` validate/clamp/персистентность/применение на прогоне, дубликат не меняет лимит, list/latest/stop, изоляция чатов, `not_configured`); `tests/test_scheduler.py` (due-выборка, CAS, catch-up один раз, stop, cascade, no-backfill, ошибка прогона сохраняется); `tests/test_orchestrator.py` (инжект/перезапись `chat_id`, окно 20, промпт включая `max_results`); `tests/test_tool_schema.py` (скрытие свойства); `tests/test_mcp_inprocess.py` (схемы 7 инструментов включая `max_results`, structured, `ToolError`); `tests/test_openapi_snapshot.py`; `tests/test_harness.py`.
* **INT** (`smoke_test.bat` → `harness/live_mcp.py`, `RUN_LIVE_MCP=1`): `tests/integration/test_tasks_live.py` — реальный MCP-процесс + loopback `FakeSearchServer` + общая тестовая БД: создание задания через реальный MCP (с инжектом `chat_id`), первый прогон в пределах тика, `get_latest_search_run` со ссылками `docs.example.test`, лимит `max_results=3` применяется на прогоне (3 из 6 результатов фейка), дефолт без лимита даёт 5, пустая выдача и ошибка сохраняются, изоляция чатов, `stop` прекращает прогоны, удаление чата через реальный backend (`DELETE?force=true`) → счётчик запросов фейка не растёт, строки заданий и прогонов удалены; `tests/integration/test_backend_live.py` — CRUD чатов по реальному HTTP и сохранение истории; `harness/scheduler_restart.py` — рестарт backend (чаты/сообщения живы, `PERSISTENCE_RESTART_STATUS`), затем при остановленном backend рестарт MCP с заранее просроченным заданием `max_results=1` (ровно один догоняющий прогон, результат ≤ 1, `SCHEDULER_RESTART_STATUS`).
* **LIVE** (`test.bat acceptance` → `harness/live_e2e.py --scenario tasks --ui`, реальный Qwen/DeepSeek): turn 1 — модель вызывает `schedule_search_task`; ожидание первого прогона; turn 2 — модель вызывает `get_latest_search_run` и отвечает с ≥ 2 ссылками из tool-результата (`TASKS_LIVE_STATUS`); trace-цепочки обоих turn-ов проверяются.
* **UI** (там же): `CHATS_UI_STATUS` — переключение, переименование, лимит 5 (сообщение), сохранение истории после `page.reload()`, `Clear chat` только выбранного чата; `TASKS_UI_STATUS` — панель заданий со статусом и сводкой/ошибкой, авто-обновление без нажатия Refresh (карточки и ссылки появляются сами), лимит `max_results=3` даёт 3 ссылки, дефолт даёт 5, за цикл опроса счётчик запросов фейка не растёт, переключение чата обновляет панель немедленно; удаление чата с активным заданием показывает предупреждение, после подтверждения задание и чат исчезают.
* **RESTART**: `smoke_test.bat` (шаги `harness/scheduler_restart.py`, реальные процессы).
* **REAL**: `harness/tasks_real_live.py` (opt-in `TASKS_REAL_ALLOW=1` и реальный ключ): чат сидируется через `storage`/backend, через реальный MCP создаётся только задание (сервер требует существующий `chat_id`) и ожидается один реальный прогон Tavily; иначе `TASKS_REAL_STATUS: BLOCKED` (exit 2). В автоматике не запускается.
* **VPS**: ручной чек-лист из SPEC §13 (оператор; в CI не автоматизируется).

## 5. Точки входа и маршрут проверки

Новые режимы `.bat` не добавляются (файлы защищены permission-правилом). Проверки встроены в существующие точки входа:

```
test.bat              unit discover включает новые модули
smoke_test.bat        harness/live_mcp.py: INT + рестарты (новые статусы)
test.bat acceptance   harness/acceptance.py: + шаг tasks (LIVE/UI статусы)
test.bat live [ui]    регрессия дней 16–17 без изменений
test.bat openapi      обновление docs/openapi.json
```

Тестовые БД — в `.runs/<run>/day18.sqlite3`; реальная `data/` не используется.

## 6. Риски и меры

| Риск | Мера |
| --- | --- |
| Ложные срабатывания CAS/блокировки SQLite | WAL + busy_timeout 5 с; короткие транзакции; тест конкурентных соединений |
| Вторая копия MCP-сервера (деплой) удвоит прогоны | CAS-захват слота; тест «два захвата — один прогон» |
| Модель не вызовет нужный инструмент в LIVE | однозначные формулировки; повторные требования к tool-цепочке как в дне 17 |
| Удаление чата в момент прогона | результат отбрасывается; гарантия «нет новых вызовов»; тест со счётчиком фейка |
| `chat_id` «утёк» в модель/UI | скрытие свойства + тесты schema/orchestrator + source-guard UI |
| Рестарт-тест зависит от таймингов | тестовая БД с просроченным `next_run_at` и polling в пределах 30 с |
| Тесты создают реальную БД | `AGENT_DB_PATH` всегда задаётся на temp-путь/.runs |

## 7. Что нельзя проверить чтением (для тестировщика)

* Реальное поведение Tavily — только REAL opt-in с разрешением пользователя.
* Поведение VPS (systemd, права `/var/lib/day16`) — ручная проверка оператором.
* Фактический выбор инструментов реальной моделью — проверяется LIVE-сценарием, не гарантируется дизайном.
