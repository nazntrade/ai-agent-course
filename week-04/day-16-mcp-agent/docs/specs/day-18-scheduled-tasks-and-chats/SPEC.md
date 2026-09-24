# SPEC — Day 18: фоновые задания и сохраняемые чаты

Идентификатор задачи: `day-18-scheduled-tasks-and-chats`.
Статус: черновик архитектурного решения. Реализация не начата и начинается только после `SPEC_GATE_STATUS: PASS` и явной команды пользователя «ДЕЛАЕМ».
Источник задания: Task Contract пользователя (формулировка задания дня 18 приведена в §4).

## 1. Назначение

Связать две части задания дня 18 в существующем проекте `week-04/day-16-mcp-agent` (день 16 — MCP-агент, день 17 — веб-поиск Tavily):

1. **Сохраняемые чаты**: создание, переключение, переименование, удаление; не более 5 чатов; собственная история на чат; изоляция контекста; модели передаётся только ограниченное окно последних сообщений выбранного чата; `Clear` действует только на выбранный чат.
2. **Фоновые (периодические) задания**: агент создаёт задание через новый MCP-инструмент; задание и его прогоны сохраняются в SQLite; планировщик выполняет задания, пока запущены серверные процессы, независимо от открытого браузера; агент получает последнюю сводку (результаты поиска или сохранённую ошибку) и показывает её со ссылками.

Архитектура дней 16–17 сохраняется: отдельный процесс MCP-сервера (Streamable HTTP, `/mcp`), FastAPI-backend с SSE-чатом, единственная сетевая граница `mcp_server/web_search.py` (Tavily), провайдер модели как отдельная граница, trace, harness и существующие точки входа.

## 2. Границы

Входит:

* SQLite-хранилище чатов, сообщений, заданий и прогонов; общий файл БД для backend и MCP-сервера;
* HTTP API чатов (создание/список/переименование/удаление/очистка/история) и панель заданий;
* замена `POST /api/chat/stream` контракта `session_id` → `chat_id`;
* 4 новых MCP-инструмента: `schedule_search_task`, `list_search_tasks`, `get_latest_search_run`, `stop_search_task`;
* планировщик в процессе MCP-сервера: тик, CAS-захват, политика пропущенных запусков, переживание рестарта процесса;
* инжект `chat_id` backend'ом в аргументы chat-scoped инструментов и скрытие `chat_id` из схемы, которую видит модель;
* UI: список чатов, лимит 5 с понятным сообщением, переименование, удаление с предупреждением об активных заданиях, `Clear chat` для выбранного чата, панель заданий со состоянием/сводкой/ошибкой;
* документация: `docs/specs/day-18-scheduled-tasks-and-chats/*`, раздел README дня 18, обновление `docs/openapi.json`;
* изменение `.env.example` (новые переменные `AGENT_DB_PATH`, `AGENT_CHAT_CONTEXT_MESSAGES`, `MCP_TASK_TICK_SECONDS`);
* opt-in REAL-проверка планировщика на реальном Tavily.

Не входит:

* многопользовательская изоляция, аутентификация, роли, квоты на пользователя;
* генерация сводки моделью в фоне (модель не вызывается планировщиком; сводку формирует модель по запросу пользователя из сохранённого результата);
* разовые отложенные задачи («напомни через 10 минут»), push-уведомления, e-mail;
* автоматическое сжатие/пересказ истории, автоудаление чатов, экспорт/импорт;
* миграция памяти предыдущих дней (в днях 16–17 БД не было);
* изменение `agent/provider.py`, `agent/mcp_adapter.py`, `mcp_server/web_search.py`, `mcp_server/tools.py::calculate|get_server_info|search_web`, формата SSE-событий;
* изменение `.bat`-файлов и governance-файлов.

## 3. Термины

| Термин | Значение |
| --- | --- |
| **Chat** | Сохраняемая беседа: `id` (32 hex), `title`, собственные сообщения. Максимум 5. |
| **Message** | Сообщение чата с ролью `user` или `assistant` (роль `tool` не хранится, как и в днях 16–17). |
| **Task** | Периодическое задание чата: `query`, `interval_seconds`, статус `active|stopped`, расписание. Принадлежит ровно одному чату. |
| **Run** | Один прогон задания: `status = ok|empty|error`, сохранённый structured-результат поиска или санитизированная ошибка. |
| **Scheduler** | Поток в процессе MCP-сервера, который находит `due`-задания и выполняет их. |
| **Context window** | Последние N сообщений выбранного чата, которые уходят в модель (N по умолчанию 20). |

## 4. Соответствие заданию дня 18

Формулировка задания из Task Contract:

> «Фоновые (отложенные/периодические) задания, создаваемые агентом через MCP-инструмент, выполняемые на сервере независимо от открытого браузера, сохраняющие результаты и позволяющие агенту получить сводку. Минимальный сценарий — периодический поиск по теме через существующий `search_web`.»

| Требование формулировки | Решение |
| --- | --- |
| «создаваемые агентом через MCP-инструмент» | модель выбирает `schedule_search_task`; вызов идёт по реальному MCP |
| «выполняемые на сервере» | планировщик живёт в процессе MCP-сервера рядом с `search_web` |
| «независимо от открытого браузера» | выполнение не требует UI и не требует backend: INT-проверка подтверждает прогон при остановленном backend |
| «сохраняющие результаты» | таблица `runs`: structured-результат или санитизированная ошибка |
| «позволяющие агенту получить сводку» | `get_latest_search_run`; модель формирует сводку со ссылками из сохранённого результата |
| минимальный сценарий | `interval_seconds = 86400`, первый запуск в пределах тика, далее раз в сутки; ошибка/пустая выдача — честно, без выдуманной сводки |

Оригинальный текст курсового задания дня 18 в репозитории отсутствует (в `week-04/` только `day-16-mcp-agent`); подтверждение дано против формулировки Task Contract. Если курсовой текст содержит дополнительные требования, это отдельное расхождение для `TASK_STATUS: WAITING_FOR_DECISION`.

## 5. Хранение: SQLite

### 5.1 Путь и владение

* Один общий файл: `AGENT_DB_PATH` (по умолчанию `<project>/data/day18.sqlite3`). Читают оба процесса: backend (`agent.settings`) и MCP-сервер (`mcp_server.config`); значения по умолчанию вычисляются от корня проекта и совпадают.
* На VPS задаётся абсолютный путь вне деплой-каталога: `AGENT_DB_PATH=/var/lib/day16/day18.sqlite3`.
* Каталог данных добавляется в `.gitignore` (`data/`); файл БД в Git не попадает.
* Файл открывается **лениво**: `create_app()`, `tools/list`, discovery и построение MCP-сервера не создают и не открывают БД. Реальное открытие — первая операция (HTTP-запрос чатов или первый tool call/тик планировщика). Это сохраняет side-effect-free `test.bat openapi` и in-process MCP-тесты.

### 5.2 Режим и параллельный доступ

На каждое соединение: `sqlite3.connect(path, timeout=5.0)`; `PRAGMA journal_mode=WAL`, `PRAGMA synchronous=NORMAL`, `PRAGMA foreign_keys=ON`, `PRAGMA busy_timeout=5000`; `row_factory = sqlite3.Row`. Соединения короткоживущие (на операцию), чтобы не переносить соединение между потоками. WAL допускает параллельных читателей и одного писателя, поэтому два локальных процесса на одной машине безопасны; сетевая ФС в качестве места для БД не поддерживается и не подразумевается.

**Матрица владения (обязательная):**

| Таблица | Backend | MCP-сервер |
| --- | --- | --- |
| `chats`, `messages` | чтение/запись | только чтение (валидация `chat_id`, чтение для сводки) |
| `tasks`, `runs` | только чтение (UI) + каскадное удаление через `chats` | чтение/запись |
| `meta` | чтение | чтение/запись при миграции |

Запись в чужие таблицы запрещена и проверяется code review + тестами.

### 5.3 Схема (schema_version = 2)

```sql
CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chats (
  id         TEXT PRIMARY KEY,
  title      TEXT NOT NULL,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  chat_id    TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  role       TEXT NOT NULL CHECK (role IN ('user','assistant')),
  content    TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);

CREATE TABLE IF NOT EXISTS tasks (
  id               TEXT PRIMARY KEY,
  chat_id          TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  query            TEXT NOT NULL,
  interval_seconds INTEGER NOT NULL,
  max_results      INTEGER NOT NULL DEFAULT 0,
  status           TEXT NOT NULL CHECK (status IN ('active','stopped')),
  created_at       REAL NOT NULL,
  updated_at       REAL NOT NULL,
  next_run_at      REAL NOT NULL,
  stopped_at       REAL,
  last_run_at      REAL,
  last_status      TEXT,
  last_error       TEXT,
  run_count        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_due  ON tasks(status, next_run_at);
CREATE INDEX IF NOT EXISTS idx_tasks_chat ON tasks(chat_id, created_at);

CREATE TABLE IF NOT EXISTS runs (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id      TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  started_at   REAL NOT NULL,
  finished_at  REAL NOT NULL,
  status       TEXT NOT NULL CHECK (status IN ('ok','empty','error')),
  result_json  TEXT,
  result_count INTEGER NOT NULL DEFAULT 0,
  error        TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id, id);
```

`meta` содержит `schema_version = 2`. `max_results` задания хранит лимит результатов на прогон: `0` — серверный дефолт, `1..10` — явный лимит; он применяется к каждому прогону (см. §8.2).

Инициализация выполняется в `BEGIN IMMEDIATE`: `CREATE TABLE IF NOT EXISTS` + проверка версии; если в файле версия **новее** известной, процесс поднимает контролируемую ошибку и не пишет в БД (fail fast, без порчи данных). Версия **старее** известной обновляется миграциями вперёд в той же транзакции: `MIGRATIONS: dict[int, tuple[str, ...]] = {2: ("ALTER TABLE tasks ADD COLUMN max_results INTEGER NOT NULL DEFAULT 0",)}`, затем `UPDATE meta SET value = 2`. База v1 с чатами/заданиями/прогонами обновляется на месте без потери данных; существующие задания получают `max_results = 0` (серверный дефолт).

### 5.4 Гарантии сохранности

| Событие | Гарантия |
| --- | --- |
| Обновление страницы браузера | История и список чатов читаются из БД через API; потеря невозможна |
| Рестарт backend | Чаты/сообщения/задания остаются на диске; UI восстанавливает выбранный (последний обновлённый) чат |
| Рестарт MCP-сервера | Задания остаются; планировщик перечитывает расписание из БД; пропущенный запуск выполняется один раз (см. §6) |
| Обновление проекта (локально) | `data/` не затрагивается; файл вне Git и вне процесса сборки |
| Обновление проекта (VPS) | БД лежит вне `/opt/day16/repo` (`/var/lib/day16/...`) и не затрагивается `git merge --ff-only` в `deploy_vps.sh` |
| Совместимость | Существующих БД нет (дни 16–17 БД не имели); первый запуск создаёт схему. Память прежних in-memory сессий не мигрирует и не восстанавливается |

### 5.5 Ограничения хранилища

* Сообщения хранятся полностью (без автоудаления и без сжатия); размер БД ограничен бытовым использованием прототипа (5 чатов). Ограничение роста — отдельная будущая задача, в день 18 не решается.
* Прогоны ограничены: на задание хранится не более `MAX_RUNS_PER_TASK = 50` последних прогонов.
* Лимиты фиксированы константами: `MAX_CHATS = 5` (agent/chats.py), `MAX_TASKS_PER_CHAT = 3`, `MAX_TASKS_TOTAL = 10`, `TASK_MIN_INTERVAL_SECONDS = 60`, `TASK_MAX_INTERVAL_SECONDS = 2592000` (30 дней), `MAX_RUNS_PER_TASK = 50` (mcp_server/tasks.py), `MAX_TITLE_LENGTH = 80`, автозаголовок 60 символов.

## 6. Планировщик

### 6.1 Где живёт и почему

Планировщик — **в процессе MCP-сервера** (`mcp_server/scheduler.py`), запускается в `mcp_server/server.py::MCPServer.run()` перед `app.run(...)` (не в `_build()` — иначе in-process тесты поднимали бы поток). Обоснование:

1. Единственная платная внешняя граница (`search_web` + ключ Tavily) уже принадлежит MCP-серверу; плановый прогон использует тот же `WebSearchService` без нового сетевого пути и без второго держателя ключа.
2. Выполнение не зависит от backend и браузера: перезапуск/деплой backend не останавливает расписание; INT-проверка выполняет прогон при остановленном backend.
3. Жизненный цикл «создание → прогон → сводка» не выходит за один процесс, что упрощает идемпотентность и гарантию «нет новых обращений к поисковому API после удаления чата».

### 6.2 Тик и останов

* Поток `daemon=True`, `threading.Event` для остановки; тик `MCP_TASK_TICK_SECONDS` (default 2.0, clamp 0.5…60) читается из `mcp_server.config`.
* Ошибка тика (БД/поиск/запись) логируется и не убивает поток: следующий тик повторяет.
* Останов процесса завершает поток; `taskkill /F /T` из harness/`run_app.bat` корректен.
* Планировщик стартует только в реальном процессе (`run()`), не в тестах и не в backend.

### 6.3 Захват и идемпотентность

Захват due-задания — compare-and-swap в одной транзакции:

```
UPDATE tasks
   SET next_run_at = :now + interval_seconds, updated_at = :now
 WHERE id = :id AND status = 'active' AND next_run_at = :claimed_next_run_at
```

* Успешный CAS (ровно одна строка) — право на единственный прогон; проигравший CAS (второй процесс, пересечение тиков) пропускает слот.
* Окно `next_run_at <= now` — задание к выполнению; выборка `ORDER BY next_run_at LIMIT 5` за тик.
* Запись прогона выполняется только если задание ещё существует (проверка существования + FK; `IntegrityError` → результат отбрасывается, задание не пересоздаётся).
* Задание со статусом `stopped` планировщиком не выбирается.

### 6.4 Рестарт и пропущенные запуски

* Расписание не хранится в памяти: после рестарта процесс читает `tasks` из БД.
* Политика пропущенных запусков: **не более одного догоняющего прогона на задание**. Если `next_run_at <= now` (в том числе на несколько интервалов), выполняется ровно один прогон; следующий `next_run_at = now + interval_seconds`. Backfill по каждому пропущенному слоту не делается.
* Падение процесса в середине прогона: слот уже израсходован (CAS выполнен), прогон может быть потерян; следующий запуск — через полный интервал. Документировано как осознанный компромисс «at most once на слот».

### 6.5 Первый запуск и ошибки

* `next_run_at = now` при создании: первый прогон стартует в пределах тика (секунды), затем — каждые `interval_seconds`. Это делает демонстрацию и LIVE-сценарий воспроизводимыми.
* Создание задания при незаконфигурированном `search_web` отклоняется с `ToolError` (честное сообщение), вместо отложенной ошибки через сутки.
* Ошибка/пустая выдача сохраняются как прогон (`error`/`empty`); выдуманных результатов нет.
* Смена настроек: `SearchConfig` резолвится один раз при первом использовании и кэшируется (поведение дня 17). После правки `.env` MCP-сервер нужно перезапустить; задания при этом сохраняются.

## 7. Граница контекста модели

* Модели передаётся **только окно последних N сообщений выбранного чата**, N = 20 (`AGENT_CHAT_CONTEXT_MESSAGES`, clamp 2…100), плюс системный промпт и текущее сообщение.
* Полная история чата остаётся в БД и **полностью видна в UI**.
* Автоматического сжатия, пересказа и «сворачивания» истории нет.
* Роль `tool` в историю не пишется (как и ранее); хранятся только `user`/`assistant`.
* Ход с ошибкой не сохраняется: `user`+`assistant` пишутся одной транзакцией только при успешном финальном ответе (сохранение семантики дня 16).
* Trace `request_start` получает `chat_id` вместо `session_id` и поле `context_messages` (число сообщений окна) — диагностическое подтверждение лимита.

## 8. MCP-инструменты

### 8.1 Контракт передачи `chat_id` (модель его не знает)

Решение: **backend инжектит `chat_id` в аргументы инструмента, а модель этого аргумента не видит.**

* Каждый chat-scoped инструмент объявлен как `..., chat_id: str = ""` (опциональный параметр серверной схемы).
* Backend в `agent/orchestrator.py::_run_tools` после разбора и валидации аргументов модели принудительно ставит `arguments["chat_id"] = session.chat_id`, **перезаписывая любое значение модели**.
* В модель инструменты уходят без свойства `chat_id`: `agent/tool_schema.py::to_openai_tools` получает список скрытых свойств (`hidden_properties`) и удаляет их из `properties` и `required`. `chat_id` не появляется ни в payload модели, ни в UI-copy аргументов, ни в trace.
* Серверная сторона дополнительно защищается: пустой/неизвестный `chat_id` → `ToolError` («This tool needs an active chat context»); задания всегда фильтруются по `chat_id`, поэтому чужой чат недостижим.
* Рассмотренная альтернатива — протокольный механизм `x-mcp-header` / `Mcp-Param-*` (присутствует в установленном SDK v2: `mcp/shared/inbound.py`, `x_mcp_header_map`): клиент зеркалит аннотированный аргумент в заголовок и **требует, чтобы инструмент был листингован в той же MCP-сессии**. Наш адаптер осознанно открывает короткую сессию на вызов (`agent/mcp_adapter.py`), а probe-сессия отдельная, поэтому включение механизма потребовало бы дополнительный `tools/list` перед каждым вызовом и изменило бы инвариант «один probe на запрос» (проверяется `harness/live_mcp.py`). Механизм не используется, решение revision-независимо; факт наличия механизма прочитан в исходниках SDK, но не исполнялся.

### 8.2 Инструменты (итого их 7: 3 прежних + 4 новых)

Общие правила: возвращаемые типы — конкретные `dict[str, Any]` (иначе SDK отдаёт текст), ошибки — `ToolError` с санитизированным текстом (без путей, ключей, заголовков), лимиты проверяются до записи.

**`schedule_search_task(query: str, interval_seconds: int, chat_id: str = "", max_results: int = 0)`**

* `query` — непустая строка после `strip`, ≤ 600 символов (усечение как в `search_web`).
* `interval_seconds` — integer (не bool); `< 60` → `ToolError("The minimum interval is 60 seconds; use search_web for a one-off search")`; `> 2592000` → clamp до 2592000 (в ответе — эффективное значение).
* `max_results` — integer (не bool); `< 0` → `ToolError("Argument 'max_results' must not be negative; use 0 for the server default")`; не-integer → `ToolError("Argument 'max_results' must be an integer")`; `> 10` → clamp до 10 (общий `SEARCH_MAX_RESULTS_CAP`); `0` — серверный дефолт. Лимит валидируется после `interval_seconds` и до проверки `configured`, сохраняется в задаче и используется на каждом прогоне (`execute_claimed`). Лимит в ответе — эффективное сохранённое значение.
* Лимиты: активных заданий в чате ≥ 3 → `ToolError("This chat already has 3 scheduled tasks. Stop one before creating another")`; всего активных ≥ 10 → `ToolError("The server already has 10 scheduled tasks. Stop one before creating another")`.
* Дубликат: активное задание с тем же нормализованным `query` в этом чате → не создаётся; возвращается существующее с `created: false`; **лимит существующего задания не меняется** (для смены лимита сначала `stop_search_task`, затем повторное планирование).
* `search_web` не сконфигурирован → `ToolError("Web search is not configured on this server (the search API key is missing). Do not invent results.")` — до создания задания.
* Успех:

```json
{
  "task_id": "a1b2c3d4e5f6",
  "status": "active",
  "query": "python news",
  "interval_seconds": 86400,
  "max_results": 3,
  "created_at": "2026-09-24T08:00:00Z",
  "next_run_at": "2026-09-24T08:00:02Z",
  "created": true,
  "note": "The first run starts within a few seconds; later runs repeat every 86400 seconds."
}
```

**`list_search_tasks(chat_id: str = "")`**

* Успех: `{"count": N, "tasks": [...]}`; элемент: `{task_id, query, interval_seconds, max_results, status, created_at, next_run_at, last_run_at|null, last_status|null, last_error|null, run_count}`; сортировка по `created_at`.
* Пусто: `{"count": 0, "tasks": [], "note": "No scheduled tasks in this chat."}`.

**`get_latest_search_run(task_id: str = "", chat_id: str = "")`**

* Без `task_id` — самый свежий прогон среди заданий чата; с `task_id` — прогон этого задания; чужой/неизвестный `task_id` → `ToolError("No scheduled task with this id exists in the current chat")`.
* `status`: `ok` (непустая выдача), `empty` (поиск успешен, результатов нет), `error` (сохранённая санитизированная ошибка), `pending` (прогона ещё не было).
* `ok`/`empty`: `result_count`, `results` (`title`,`url`,`description`), `note` из контракта дня 17. `error`: `results: []`, `error: "<санитизированное сообщение>"`. `pending`: `note: "The first run has not finished yet."`.
* Никогда не выдумывает сводку: при ошибке модель получает текст ошибки.

**`stop_search_task(task_id: str, chat_id: str = "")`**

* `active` → `stopped` (`stopped_at`); ответ `{task_id, status: "stopped", stopped_at, note: "No further runs will start."}`.
* Уже `stopped` → успех с `note: "This task was already stopped."` (идемпотентно).
* Неизвестный/чужой `task_id` → `ToolError("No scheduled task with this id exists in the current chat")`.
* Прогон, уже начатый в момент остановки, может завершиться (расходует не более одного вызова) и будет записан; новые запуски не начинаются.

### 8.3 Промпт модели

`SYSTEM_PROMPT` дополняется (текст — английский, формулировки проверяются UNIT-тестом): использовать `schedule_search_task` для повторяющихся/ежедневных поисков; объяснять, что первый прогон начнётся в ближайшие секунды, а сводки станут доступны после него; для сводки вызывать `get_latest_search_run` и приводить ссылки из tool-результата; не выдумывать результаты и не утверждать, что задание требует открытого браузера; для остановки — сначала `list_search_tasks`, затем `stop_search_task`.

## 9. HTTP API (OpenAPI 3.1)

Единый формат ошибки для контролируемых случаев:

```json
{"detail": {"category": "chat_limit", "message": "The limit of 5 chats is reached. Delete a chat to create a new one."}}
```

Категории: `chat_not_found`, `chat_limit`, `chat_has_active_tasks`, `invalid_title`, `chat_storage_unavailable`. Валидационные 422 FastAPI остаются как есть.

| Метод и путь | Назначение | Успех | Ошибки |
| --- | --- | --- | --- |
| `GET /api/chats` | список чатов | `{"chats": [ChatInfo], "count", "limit": 5, "limit_reached": bool}` | — |
| `POST /api/chats` | создать чат (`{"title": ""}`) | 201 `ChatInfo` | 409 `chat_limit` |
| `PATCH /api/chats/{chat_id}` | переименовать (`{"title": "..."}`) | 200 `ChatInfo` | 404, 422 `invalid_title` |
| `DELETE /api/chats/{chat_id}?force=false` | удалить чат | 200 `{"deleted": true, "stopped_tasks": N}` | 404; 409 `chat_has_active_tasks` (+ `active_tasks`) |
| `GET /api/chats/{chat_id}/messages` | полная история | `{"chat_id", "messages": [{"role","content","created_at"}], "count"}` | 404 |
| `POST /api/chats/{chat_id}/clear` | очистить выбранный чат | `{"cleared": true, "messages_deleted": N}` | 404 |
| `GET /api/chats/{chat_id}/tasks` | задания чата для UI | `{"chat_id", "tasks": [TaskInfo], "count"}` | 404 |
| `POST /api/chat/stream` | диалог (SSE) | поток событий без изменений | 404 `chat_not_found`, 422 |

`ChatInfo = {id, title, created_at, updated_at, message_count, active_tasks_count}`. `TaskInfo` повторяет `list_search_tasks` и добавляет `last_run`: `{status, ran_at, result_count, results, error}` или `null`. Timestamps в HTTP — epoch (float), в MCP-ответах — ISO-8601 UTC.

**Изменение контракта**: `POST /api/chat/stream` принимает `{"chat_id": "...", "message": "..."}`; поле `session_id` удаляется. Это внутренний контракт (потребители — только наш UI и harness), он отражается в `docs/openapi.json` и тестах.

Правила:

* `POST /api/chats` при 5 чатах → 409, существующие чаты не удаляются и не вытесняются;
* авто-заголовок: при первом успешном сообщении, если заголовок всё ещё дефолтный («New chat»), он становится первыми 60 символами сообщения (whitespace сжат); переименование пользователем всегда приоритетно;
* `clear` удаляет только сообщения выбранного чата, не создаёт чат и не трогает задания;
* `DELETE` без `force` при активных заданиях → 409 с числом и понятным текстом; с `force=true` — каскадное удаление `chats → messages/tasks/runs` одной транзакцией.

## 10. UI (English)

Структура `static/index.html`:

* левая панель `#chats-panel`: заголовок `Chats`, кнопка `#new-chat-button` («New chat»), список `#chats-list`, сообщение лимита `#chat-limit-message`;
* элемент списка: `.chat-item[data-chat-id]` с `.chat-select`, `.chat-rename`, `.chat-delete`, активный помечен `.active`;
* в шапке чата кнопка `#clear-button` (текст меняется с `Clear session` на `Clear chat`);
* панель заданий `#tasks-panel` под чатом: `#tasks-list`, `#tasks-refresh-button`; карточка задания показывает query, статус (`active`/`stopped`), интервал, `last_run` (статус, время, до 5 ссылок) или сохранённую ошибку;
* `#messages`, композер и блок `MCP status` сохраняются.

Поведение:

* загрузка страницы: `GET /api/chats`; если чатов нет — создаётся один (`POST /api/chats`); выбирается последний обновлённый, загружаются `messages` и `tasks`;
* панель заданий обновляется сама, пока страница открыта: `setInterval(loadTasks, TASKS_POLL_INTERVAL_MS)`, `TASKS_POLL_INTERVAL_MS = 10000`, без нажатия Refresh; опрос вызывает только `GET /api/chats/{chat_id}/tasks` и **никогда** не запускает поиск, модель или инструмент. Кнопка `#tasks-refresh-button` сохраняется. Параллельные опросы запрещены (`tasksRequestInFlight` + `finally`); ответ для уже не выбранного чата отбрасывается (`selectionToken`/`activeChatId`); таймер очищается на `pagehide`/`beforeunload` (`stopTasksPolling`) и идемпотентно возобновляется на `pageshow` (`startTasksPolling`);
* переключение чата — загрузка его сообщений и заданий немедленно, не дожидаясь следующего тика опроса; сообщения других чатов не показываются;
* создание при лимите — сообщение `The limit of 5 chats is reached. Delete a chat to create a new one.` (из `detail.message`), без автоудаления;
* переименование — диалог ввода, `PATCH`;
* удаление — сначала `DELETE` без `force`; при 409 показывается предупреждение сервера («This chat has N active scheduled task(s)...») и по подтверждению повторяется `DELETE?force=true`;
* `Clear chat` — `POST .../clear` для выбранного чата; скрытого создания чата нет;
* на время стриминга ответа действия списка чатов блокируются (UI-упрощение от гонок);
* ссылки заданий рендерятся как `<a href>` через DOM API (без innerHTML);
* `chat_id`/`task_id` никогда не выводятся в тексте интерфейса.

## 11. Изоляция контекста

1. Окно истории грузится по `chat_id` текущего запроса под per-chat `asyncio.Lock`; чужие сообщения в промпт не попадают.
2. Chat-scoped инструменты всегда получают `chat_id` из backend'а; модель не может подставить чужой чат (значение перезаписывается), сервер отвергает пустой/неизвестный контекст.
3. Все SQL-запросы заданий/прогонов скоупятся по `chat_id` (или по `task_id` с проверкой принадлежности чату).
4. Прогоны одного чата не попадают в сводку другого: `get_latest_search_run` и `GET /api/chats/{id}/tasks` фильтруют чат.
5. UI показывает только сообщения/задания выбранного чата.
6. Проверки: UNIT `tests/test_chats_api.py`, `tests/test_tasks.py`; INT `tests/integration/test_tasks_live.py`, `tests/integration/test_backend_live.py`.

## 12. Удаление чата с действующим заданием

1. UI запрашивает `DELETE /api/chats/{id}` без `force`.
2. Backend считает активные задания; если они есть — 409 `chat_has_active_tasks` с числом и сообщением; удаление не выполняется.
3. Пользователь подтверждает; UI повторяет `DELETE ...?force=true`.
4. Backend одной транзакцией удаляет чат; FK-каскад удаляет сообщения, задания и прогоны; в ответе — сколько заданий было остановлено.
5. Планировщик не видит удалённые строки на следующем тике; in-flight прогон (≤1) может завершиться, но его результат отбрасывается (проверка существования задания), задание не пересоздаётся. После коммита удаления новые обращения к поисковому API не начинаются.
6. Проверки: UNIT `tests/test_chats_api.py`; INT `tests/integration/test_tasks_live.py` (счётчик запросов loopback-фейка до/после удаления + отсутствие строк заданий/прогонов); UI-предупреждение — `TASKS_UI_STATUS`.

## 13. Локальная машина и VPS

**Локально:**

* `run_app.bat` (режим `all`) поднимает MCP-сервер (вместе с планировщиком) и backend; БД — `data/day18.sqlite3` (gitignored), создаётся автоматически.
* `.env` не обязателен для чатов и заданий: без ключа Tavily чат и чаты работают, создание задания отклоняется с понятным сообщением, `search_web` сообщает `not configured`.
* Историю и задания можно проверить перезапуском backend/MCP и обновлением страницы.
* `python -m mcp_server` (режим `run_app.bat mcp`) запускает планировщик автоматически; новые режимы `.bat` не добавляются.

**VPS (существующий контур: `day16-mcp`, `day16-backend`, `/opt/day16/repo`, `/etc/day16/search.env`):**

* Оба процесса — под процессным менеджером (systemd) с `Restart=always`; открытый браузер не является условием выполнения заданий.
* `AGENT_DB_PATH=/var/lib/day16/day18.sqlite3` задаётся в обоих юнитах (drop-in), каталог принадлежит пользователю `day16`; БД вне деплой-каталога и переживает fast-forward деплой.
* Ключ Tavily остаётся только в MCP-сервисе (`EnvironmentFile=/etc/day16/search.env`, `MCP_LOAD_DOTENV=0`) — планировщик использует его внутри процесса MCP.
* Резервное копирование: остановить сервисы и скопировать файл БД (или `sqlite3 ... ".backup"`), включая актуальное состояние WAL.
* `deploy_vps.sh` в этой задаче не изменяется: его проверка `tools_count >= 3` проходит и при 7 инструментах. Настройка `AGENT_DB_PATH` — разовая ручная операция оператора (описана в README).
* TLS/reverse proxy/аутентификация остаются вне дня 18 (как и в дне 16).

## 14. Совместимость дней 16–17 (обязательно сохранить)

| Поведение | Гарантия |
| --- | --- |
| SSE-контракт: `status`, `delta`, `tool_call`, `tool_result`, `done`, `error`, keep-alive `: ping`, `error` без `done` | не изменяется |
| `Technical details`: свёрнутый блок, `tool_call`/`tool_result`/`status` только внутри | не изменяется; `chat_id` скрыт |
| Инструменты `calculate`, `get_server_info`, `search_web` | контракты и поведение не изменяются |
| MCP discovery (`discovery_cli.py`, пагинация, exit codes) | не изменяется; список теперь из 7 инструментов |
| Trace-цепочка `mcp_connect → mcp_list_tools → model_request(tool_selection) → tool_selected → tool_completed → model_request(final_answer) → request_done` | сохраняется для сценариев arithmetic/search |
| Провайдер модели (`agent.provider.py`) и адаптер MCP (`agent/mcp_adapter.py`) | не изменяются |
| `run_app.bat`, `test.bat`, `smoke_test.bat` | не изменяются |
| `search_web`: Tavily, лимиты, ошибки, `more_results_available: false` | не изменяется |
| Осознанные изменения | `Clear session` → `Clear chat` (только выбранный чат); `session_id` → `chat_id` в `/api/chat/stream`; список MCP-инструментов 3 → 7 |

## 15. Безопасность

* Ключ Tavily остаётся только в процессе MCP-сервера; планировщик использует его там же; backend, модель, UI, trace и ответы API ключа не видят.
* `chat_id`/`task_id` — непрозрачные идентификаторы, не секреты; в UI не выводятся, в trace допустимы (trace не должен содержать текстов пользователя сверх существующего правила: query попадает в `tool_selected` так же, как это уже происходит у `search_web`).
* Ошибки инструментов и API санитизированы: без путей, URL-ов, заголовков и ключей.
* Тесты изолированы от сети и реального `.env`; БД тестов — во временном каталоге/`.runs`, реальный `data/day18.sqlite3` тестами не используется.
* Реальный Tavily в автотестах не вызывается; только opt-in REAL-режим с явным разрешением.
* Авторизация отсутствует (однопользовательский прототип), как и ранее; многопользовательская изоляция — вне границ.

## 16. Ограничения и непроверяемое чтением

* Курсовой текст дня 18 в репозитории отсутствует; соответствие подтверждено только по формулировке Task Contract (§4).
* Поведение `x-mcp-header` прочитано в исходниках SDK, но не исполнялось; в решении не используется (не влияет на результат).
* Реальный Tavily и VPS в этой задаче автоматически не проверяются (REAL — opt-in, VPS — ручная проверка оператором).
* Выбор инструмента реальной моделью не детерминирован; LIVE-сценарий использует однозначные формулировки и повторяет требования дня 17 к проверке tool-цепочки.
* Рост БД сообщений не ограничен; при необходимости — отдельная задача.
* Мгновенная отмена уже начатого сетевого вызова Tavily невозможна (stdlib HTTP без отмены); гарантия — отсутствие **новых** вызовов после удаления чата.
* Миграция схемы однонаправленная (forward-only): база v1 обновляется до v2 на месте; отката к v1 нет. База новее известной версии по-прежнему отклоняется (`SchemaVersionError`).
* Панель заданий рендерит не более `MAX_RENDERED_TASK_LINKS = 5` ссылок на прогон: при `max_results` от 6 до 10 сервер сохраняет и возвращает больше ссылок (до 10), но UI показывает первые 5.

## 17. Маршрут проверки

Новые режимы `.bat` не добавляются (файлы защищены). Используются существующие точки входа:

```
test.bat                  UNIT: новые модули storage/chats/tasks/scheduler, API, schema,
                          orchestrator, in-process MCP, OpenAPI drift
smoke_test.bat            INT: реальный MCP + реальный backend + loopback FakeSearchServer
                          (TASKS_INTEGRATION_STATUS), рестарт backend
                          (PERSISTENCE_RESTART_STATUS), рестарт MCP и догоняющий прогон
                          (SCHEDULER_RESTART_STATUS)
test.bat acceptance       LIVE + UI: arithmetic → search → tasks (TASKS_LIVE_STATUS,
                          CHATS_UI_STATUS, TASKS_UI_STATUS)
test.bat live [ui]        регрессия дня 16 (без изменений)
test.bat openapi          обновление docs/openapi.json
Ручной opt-in REAL:       .venv\Scripts\python.exe harness\tasks_real_live.py
                          (только с TASKS_REAL_ALLOW=1 и реальным ключом)
```

Тестовая БД harness всегда лежит в `.runs/<run>/day18.sqlite3`; реальная `data/` не трогается.
