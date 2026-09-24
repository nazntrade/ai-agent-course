# ACCEPTANCE — Day 18: фоновые задания и сохраняемые чаты

Уровни проверки: **UNIT** (без сети и реальных процессов), **INT** (реальные процессы MCP и backend + реальный HTTP + loopback `FakeSearchServer` + тестовая SQLite), **LIVE** (реальная модель Qwen/DeepSeek), **UI** (Playwright + системный браузер), **RESTART** (фактический перезапуск реального процесса через доверенную точку входа), **REAL** (настоящий Tavily Search, только opt-in), **VPS** (ручная проверка оператором).

Уровень не занижается: «работает после рестарта» подтверждается реальным перезапуском процессов, «выполнение без открытого браузера» — прогоном при остановленном backend и без UI, внешняя граница Tavily — отдельным REAL-уровнем с явным opt-in.

| ID | Критерий | Уровень | Проверка |
| --- | --- | --- | --- |
| D18-01 | SQLite-схема v1, WAL + FK + busy_timeout, миграционный гейт, общий файл двух процессов | UNIT + INT | `tests/test_storage.py`; `smoke_test.bat` (`harness/live_mcp.py`, `AGENT_DB_PATH`) |
| D18-02 | CRUD чатов (создание/список/переименование/удаление) и персистентность | UNIT + INT | `tests/test_chats_api.py`; `tests/integration/test_backend_live.py` |
| D18-03 | Лимит 5 чатов: 6-й → 409 с понятным сообщением, без автоудаления | UNIT + UI | `tests/test_chats_api.py`; `test.bat acceptance` (`CHATS_UI_STATUS`) |
| D18-04 | Собственная история на чат; сообщения чата A не появляются в чате B | UNIT + INT | `tests/test_chats_api.py`, `tests/test_api.py`; `tests/integration/test_backend_live.py` |
| D18-05 | Модели — только окно последних 20 сообщений выбранного чата; полная история видна; без сжатия | UNIT | `tests/test_orchestrator.py`, `tests/test_api.py` (провайдер видит ≤20 + текущее) |
| D18-06 | История и задания переживают перезагрузку страницы и рестарт backend/MCP | RESTART + UI | `smoke_test.bat` (`PERSISTENCE_RESTART_STATUS`); `CHATS_UI_STATUS` (reload) |
| D18-07 | `Clear chat` очищает только выбранный чат, не создаёт чат и не трогает задания | UNIT + UI | `tests/test_chats_api.py`; `CHATS_UI_STATUS` |
| D18-08 | Удаление чата с активным заданием: предупреждение (409) до удаления; `force` → каскад messages/tasks/runs | UNIT + INT + UI | `tests/test_chats_api.py`; `tests/integration/test_tasks_live.py`; `TASKS_UI_STATUS` |
| D18-09 | После удаления чата нет новых прогонов и новых обращений к поисковому API; in-flight результат отбрасывается | INT | `tests/integration/test_tasks_live.py` (счётчик `GET /__stats__` фейка + отсутствие строк); `tests/test_storage.py`, `tests/test_scheduler.py` (запись прогона по уже удалённому заданию строку не создаёт) |
| D18-10 | В `tools/list` 7 инструментов; 4 новых возвращают structured-результаты | UNIT + INT | `tests/test_mcp_inprocess.py`, `tests/test_tools.py`; `tests/integration/test_tasks_live.py` |
| D18-11 | `chat_id` инжектится backend'ом и скрыт от модели; произвольное значение модели перезаписывается; сервер отвергает пустой/чужой контекст | UNIT + INT | `tests/test_orchestrator.py`, `tests/test_tool_schema.py`; `tests/integration/test_tasks_live.py` |
| D18-12 | Изоляция заданий: задания чата A не видны в чате B (list/latest/stop) | UNIT + INT | `tests/test_tasks.py`; `tests/integration/test_tasks_live.py` |
| D18-13 | Лимиты: min 60 с, max 30 дней (clamp), 3 задания на чат, 10 всего, дубликат не создаёт второе | UNIT + INT | `tests/test_tasks.py`; `tests/integration/test_tasks_live.py` |
| D18-14 | Планировщик выполняет due-задания в процессе MCP без браузера и backend; первый прогон — в пределах тика | INT | `tests/integration/test_tasks_live.py`; `SCHEDULER_RESTART_STATUS` (backend остановлен) |
| D18-15 | Прогоны `ok`/`empty`/`error` сохраняются; ошибка без выдуманной сводки | UNIT + INT | `tests/test_tasks.py`, `tests/test_scheduler.py`; `tests/integration/test_tasks_live.py` |
| D18-16 | `get_latest_search_run`: `ok` со ссылками, `empty`, `error`, `pending`; только свой чат | UNIT + INT + LIVE | `tests/test_tasks.py`; `tests/integration/test_tasks_live.py`; `TASKS_LIVE_STATUS` |
| D18-17 | `stop_search_task` прекращает будущие прогоны; unknown → ошибка; повтор — идемпотентен | UNIT + INT | `tests/test_tasks.py`; `tests/integration/test_tasks_live.py` |
| D18-18 | Рестарт MCP: расписание из БД; ровно один догоняющий прогон; без backfill; CAS исключает двойной запуск | RESTART + UNIT | `smoke_test.bat` (`SCHEDULER_RESTART_STATUS`); `tests/test_scheduler.py` |
| D18-19 | UI: создание/переключение/переименование/удаление чатов, лимит, `Clear chat`, панель заданий (состояние/сводка/ошибка), предупреждение при удалении | UI | `test.bat acceptance` (`CHATS_UI_STATUS`, `TASKS_UI_STATUS`); `tests/test_chats_tasks_ui.py` (source-guard) |
| D18-20 | LIVE: реальная модель создаёт задание и затем даёт сводку с ≥ 2 ссылками из tool-результата | LIVE | `test.bat acceptance` (`TASKS_LIVE_STATUS`, `harness/live_e2e.py --scenario tasks`) |
| D18-21 | Дни 16–17 сохранены: SSE, `Technical details`, `calculate`/`get_server_info`/`search_web`, discovery, trace-цепочка | UNIT + INT + LIVE | `test.bat`; `smoke_test.bat`; `test.bat acceptance` (arithmetic и search без регрессий) |
| D18-22 | Реальный Tavily для задания — только opt-in | REAL | `.venv\Scripts\python.exe harness\tasks_real_live.py` (только с `TASKS_REAL_ALLOW=1`; иначе `TASKS_REAL_STATUS: BLOCKED`, exit 2) |
| D18-23 | VPS: `AGENT_DB_PATH` вне деплой-каталога, оба сервиса под process manager, задания идут без браузера, БД переживает деплой | VPS | ручной чек-лист SPEC §13 (оператор; автоматизация в этой задаче не выполняется) |
| D18-24 | OpenAPI 3.1 обновлён и drift-тест зелёный | UNIT | `test.bat openapi`; `tests/test_openapi_snapshot.py` |
| D18-25 | Панель SCHEDULED TASKS автообновляется, пока страница открыта (опрос `GET /api/chats/{chat_id}/tasks` раз в 10 с), без нажатия Refresh; при переключении чата — немедленно; параллельных запросов нет; таймер очищается при уходе со страницы; опрос не запускает поиск/модель/инструмент | source-guard + UI | `tests/test_chats_tasks_ui.py` (source-guard: интервал, guard `tasksRequestInFlight`, `clearInterval`, `pagehide`/`beforeunload`, token-проверка, тело `loadTasks` без `chat/stream`/`mcp`); `test.bat acceptance` (`TASKS_UI_STATUS`: карточки появляются сами, счётчик фейка за цикл не растёт, switch обновляет немедленно) |
| D18-26 | Лимит результатов периодического задания: `max_results` 0..10 (0 = серверный дефолт), валидация/clamp, сохраняется в БД (миграция v1→v2), применяется на каждом прогоне; лимит переживает рестарт; дубликат по query не меняет лимит | UNIT + INT + UI | `tests/test_tasks.py` (валидация/clamp/персистентность/применение), `tests/test_storage.py` (миграция v1→v2), `tests/test_mcp_inprocess.py`, `tests/integration/test_tasks_live.py` (limit 3 и дефолт 5 на реальном MCP), `SCHEDULER_RESTART_STATUS` (лимит 1), `TASKS_UI_STATUS` (3 vs 5 ссылок) |

## Требования к приёмке

* Уровни не заменяются более слабыми: INT не считается за RESTART, LIVE не подменяется UNIT-тестом с фейком, REAL не заявляется PASS без реального ключа и разрешения.
* Автотесты не тратят платные вызовы: `search_web` в UNIT/INT работает против `FakeSearchServer`; реальный Tavily — только D18-22.
* Тестовая БД — только в temp/`.runs`; реальная `data/day18.sqlite3` тестами не трогается.
* `TEST_STATUS: PASS` возможен только когда обязательные критерии проверены на своём уровне. Для D18-22 без разрешения/ключа — `BLOCKED` по этому критерию (не полный PASS). Для D18-23 — `BLOCKED`, пока оператор не выполнил чек-лист на VPS.

## Итог приёмки

| Проверка | Команда | Результат |
| --- | --- | --- |
| UNIT | `test.bat` | `UNIT_STATUS: PASS` — 424 теста, 41 skipped, exit 0 |
| INT/RESTART | `smoke_test.bat` | `MCP_INTEGRATION_STATUS: PASS` (11), `BACKEND_INTEGRATION_STATUS: PASS` (16), `SEARCH_INTEGRATION_STATUS: PASS` (7), `TASKS_INTEGRATION_STATUS: PASS` (7), `PERSISTENCE_RESTART_STATUS: PASS`, `SCHEDULER_RESTART_STATUS: PASS` |
| LIVE + UI | `test.bat acceptance` | `MCP_UNAVAILABLE_UI_STATUS: PASS`, `TRACE_STATUS: PASS`, live MCP PASS, `LIVE_LLM_STATUS: PASS`, `SEARCH_LIVE_STATUS: PASS`, `SEARCH_UI_STATUS: PASS`, `TASKS_LIVE_STATUS: PASS`, `CHATS_UI_STATUS: PASS`, `TASKS_UI_STATUS: PASS`, `key_isolation: true` |
| REAL Tavily | `.venv\Scripts\python.exe harness\tasks_real_live.py` | `TASKS_REAL_STATUS: BLOCKED` (нет `TASKS_REAL_ALLOW=1`/ключа; не запускался) |
| VPS | ручной чек-лист SPEC §13 | `BLOCKED` (оператор) |

LIVE-прогоны выполнил `harness/live_e2e.py`, который сам поднял и остановил локальную модель. По артефакту `.runs/<timestamp>-live-e2e-tasks/report.json`: запрошенная модель `qwen3.8-27b-local`, сконфигурированная на endpoint `qwen3.8-27iq4xs`, `started_by_harness: true`, `endpoint_running_before_run: false`, `model_ready: true`; получены потоковые `delta` и `done` (`finish_reason: stop`), вызваны `schedule_search_task` и `get_latest_search_run`, в ответе 3 ссылки из tool-результата.

Расхождение счётчиков: `test.bat` — 424 теста, unit-шаг `test.bat acceptance` — 400; разница 24 = три DOM-класса (`MarkdownRendererDomTest` 16, `SearchSpinnerBrowserTest` 5, `LinkStyleBrowserTest` 3), которые при `sanitized_env()` без браузерных путей пропускаются целиком (skipped 41 → 44). Тесты дня 18 выполняются в обоих прогонах; пропуска нет.

Независимая приёмка Tester: `TEST_STATUS: BLOCKED` только из-за D18-22 и D18-23 (сознательно отложены пользователем); по остальным критериям D18-01…D18-21, D18-24 — PASS на уровне UNIT/INT/RESTART/LIVE/UI, дефектов нет.
