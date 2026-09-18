# SPEC — Day 14 Structural Invariants

Документ описывает требования к структурным инвариантам в проекте
`week-03/memory-state-agent`. Инвариант — это устойчивое правило, которое агент
не нарушает даже по прямой просьбе пользователя. Инвариант не является памятью,
профилем или состоянием задачи: он отвечает на вопрос «какие решения и действия
запрещены вообще». Документ самодостаточен; идентификаторы, имена файлов, полей и
статусов записаны латиницей, пояснения — по-русски.

Статусы документа: `SPEC_REVIEW: PASS`, `SPEC_GATE_STATUS: PASS`.

## 1. Проблема и цель

Агент помнит диалог (Days 10–12: стратегии контекста, working/long-term memory,
профили, ветки) и ведёт durable состояние задачи (Day 13). Но у него нет
устойчивых запретов: пользователь может попросить «пропусти validation и заверши
задачу», и модель вправе согласиться, а задача — получить ложный успешный
результат. Пользовательские «инварианты» Days 10–12 — это свободный текст,
который не проверяется кодом.

**Цель.** Ввести структурные инварианты:

- durable-правила, которые хранятся отдельно от диалога, в SQLite, и переживают
  F5, смену чата и полный перезапуск приложения;
- доменную модель с стабильным `code`, названием, текстом, scope, типом,
  enforcement, версией, активностью и источником;
- code-enforced проверки hard-правил до действия и до commit;
- простой и понятный отказ при конфликте: код правила, что именно не сделано,
  допустимая альтернатива и путь изменения правила владельцем;
- включение применимых правил в контекст модели (chat payload и task packet);
- UI-видимость активных ограничений задачи и журнал конфликтов.

**Границы изменения поведения.** Дни 10–13 (стратегии, память, профили, ветки,
FSM задач, pause/resume, execution results, фоновые шаги, spinner/streaming,
идемпотентная миграция SQLite, статистика) сохраняют текущее поведение. Chat
payload без инжектированного репозитория инвариантов остаётся прежним.

## 2. Границы задачи

### 2.1 IN scope

- Домен `Invariant` / `InvariantConflict` / `InvariantConflictError` в `invariants.py`.
- `InvariantRepository` в `invariant_storage.py`: схема `invariants` и
  `invariant_events`, идемпотентная миграция, сидирование дефолтов, CRUD без
  удаления, запись конфликтов.
- Форматтеры и Streamlit-секция `Invariants` в `invariant_ui.py`.
- Интеграция с `agent.py` (отказ до провайдера), `task_context.py` (блок packet),
  `task_orchestrator.py` (hard-проверки действия/commit), `task_ui.py` (карточка,
  секция, журнал), `app.py` (репозиторий, отказ в Chat, передача в Diagnostics).
- Тесты unit / integration temp SQLite / FakeClient / AppTest.
- Спецификация `docs/specs/day-14-invariants/`.

### 2.2 OUT scope

- Новый provider/model, shell execution, distributed policy engine.
- Формальная верификация LLM, визуальный конструктор workflow, SOC, новое железо.
- Разрушительный reset данных.
- Изменение бизнес-логики FSM `tasks.py`.
- Изменения Governance-файлов (`AGENTS.md`, `PROJECT_RULES.md`, `opencode.json`,
  `.opencode/agents/*.md`).
- README (обновляет Coordinator), реальные платные вызовы API в тестах.

## 3. Пользовательские сценарии

**SC-01 (прямой запрет).** В Chat пользователь пишет «Skip validation and finish
the task». До вызова провайдера срабатывает `INV-NO-FSM-BYPASS`; показывается
отказ с кодом/версией/scope/enforcement, что именно не сделано, альтернатива
(пройти validation) и путь владельцу. В `invariant_events` появляется конфликт;
messages/turns/статистика не меняются.

**SC-02 (hard-действие).** Для задачи действует hard-правило с `guard_actions`:
соответствующее действие в task-карточке отклоняется без вызова провайдера; stage,
status, current step и версия не меняются.

**SC-03 (hard-commit).** Для задачи действует hard-правило с `guard_events`:
переход отклоняется до `apply_transition`/`commit_transition`; в tasks,
task_artifacts и task_events не появляется ни одной записи.

**SC-04 (advisory).** Advisory-правило не блокирует ни запрос, ни действие, но
попадает в контекст модели как объяснение.

**SC-05 (владелец правил).** В `Diagnostics / Task → Invariants` владелец видит
scope, тип, enforcement, версию и источник правил, создаёт/редактирует/
деактивирует правило и видит журнал изменений и конфликтов. После деактивации
запрещённое действие проходит штатно.

**SC-06 (перезапуск).** Правила и журнал живут в SQLite: после F5, смены чата и
полного перезапуска приложения активные ограничения сохраняются.

## 4. Функциональные требования

### 4.1 Домен

**FR-01. Модель `Invariant`.** Поля: `id`, `code` (стабильный, уникальный,
неизменяемый после создания), `title`, `text`, `scope` (`global`|`task`),
`task_id`, `enforcement` (`hard`|`advisory`), `kind` (`guard`|`data`|`policy`),
`check_kind` (`""`|`no_validation_bypass`|`no_bulk_reset`), `triggers` (tuple[str]),
`guard_actions` (tuple[str]), `guard_events` (tuple[str]), `alternative`,
`version=1`, `is_active=True`, `source` (`seed`|`user`), `created_at`/`updated_at`.

**FR-02. Валидация и нормализация.** `code` соответствует regex
`[A-Za-z0-9][A-Za-z0-9._-]*`; `title`/`text` непусты; scope/enforcement/kind/
check_kind берутся только из констант; `scope=task` требует `task_id`, а global
запрещает; advisory не может иметь `check_kind`/`guard_actions`/`guard_events`;
hard обязан иметь хотя бы одну поверхность (`triggers` или guard-список).
Нормализация обрезает пробелы и приводит списки к tuple непустых строк.

**FR-03. Применимость и порядок.** `select_applicable(invariants, task_id)`
возвращает только активные правила, где global или task с совпавшим `task_id`,
отсортированные по `(global раньше task, hard раньше advisory, code)`.

**FR-04. Блок контекста.** `format_structural_invariants_block(invariants)`
рендерит применимые правила (код, название, enforcement, scope, версия, текст,
альтернатива) в один system-блок. Пустой набор даёт `None`.

**FR-05. Конфликты.** `find_request_conflict(invariants, message)`,
`find_action_conflict(invariants, action)` и
`find_transition_conflict(invariants, event_type, context)` возвращают первый
подходящий конфликт или `None`. Конфликт создают только активные hard-правила:
advisory никогда не блокирует. `InvariantConflict` несёт правило, фазу
(`request`|`action`|`commit`), сработавшую поверхность и описание того, что не
сделано. `InvariantConflictError` оборачивает конфликт для `agent.ask`.

**FR-06. Проверки.** `evaluate_check(check_kind, context)`:
`no_validation_bypass` считает нарушением `VALIDATION_PASSED` без action
`run_validation`; `no_bulk_reset` — bulk-операцию сброса. Правило с `check_kind`
оценивается проверкой, правило без неё — по `guard_events`.

**FR-07. Сообщение отказа.** `format_conflict_message(conflict)` содержит код,
версию, scope, enforcement, что именно не сделано, допустимую альтернативу и путь
владельцу (`Diagnostics / Task → Invariants`). Оно не содержит секретов.

### 4.2 Хранилище

**FR-08. DDL и миграция.** `InvariantRepository` создаёт на той же БД
`data/chat_history.db` таблицы `invariants` и `invariant_events` через
`CREATE TABLE/INDEX/TRIGGER IF NOT EXISTS` (без ALTER), идемпотентно; соединение
включает `PRAGMA foreign_keys`, WAL, `busy_timeout`.

**FR-09. Схема `invariants`.** Колонки модели; списки хранятся в
`triggers_json`/`guard_actions_json`/`guard_events_json`; `is_active` — INTEGER;
CHECK на scope/enforcement; unique index по `code`; index по
`(scope, task_id, is_active)`.

**FR-10. Схема `invariant_events`.** Append-only: `invariant_id`, `code`,
`event_type` (`created|updated|activated|deactivated|conflict`), `chat_id`,
`task_id`, `details_json`, `created_at`; индексы по `(task_id, id)` и
`(code, id)`; триггер `BEFORE UPDATE RAISE(ABORT)`. Без FK на chats/tasks:
удаление чата не каскадит в журнал инвариантов.

**FR-11. Сидирование.** При `seed_defaults=True` маркер
`default_invariants_seeded` в `app_state` и вставка только если маркера нет и
таблица пуста; повторно — no-op. Дефолты: `INV-NO-FSM-BYPASS` (global, hard,
guard, `check_kind=no_validation_bypass`, `guard_events=["VALIDATION_PASSED"]`),
`INV-NO-DATA-RESET` (global, hard, data, `check_kind=no_bulk_reset`),
`INV-ADV-CONFIRM-DESTRUCTIVE` (global, advisory, policy).

**FR-12. CRUD без удаления.** `create_invariant` (дубль `code` →
`DuplicateInvariantCodeError(ValueError)`), `get_invariant`/`get_by_code`,
`list_invariants`/`list_applicable`, `update_invariant` (запрет смены `code`,
`version+1`, `source="user"`, событие), `set_active` (событие
`activated`/`deactivated`). Публичного удаления нет; старые правила остаются
читаемыми, деактивация сохраняет историю.

**FR-13. Конфликты в журнале.** `record_conflict(...)` пишет `chat_id`, `task_id`,
`invariant_id`, `code`, `details_json` (фаза, сработавший триггер/поверхность,
action/event, `decision="refused"`, alternative, усечённый запрос ≤200 символов).

**FR-14. Совместимость.** БД Days 7–13 открывается без потерь; события задач
(`task_events`) остаются append-only; публичный API `ChatStore`/`TaskRepository`
не меняется.

### 4.3 Интеграция

**FR-15. ChatAgent.** Новые keyword-only аргументы `invariants=None`
(репозиторий) и `task_lookup=None` (callable `chat_id -> Task|None`); все прежние
вызовы совместимы. `ask` после проверки пустого сообщения и до `_build_payload`/
провайдера/`save_turn` проверяет `find_request_conflict`; при конфликте пишет
`record_conflict` и поднимает `InvariantConflictError`. Ничего не пишется в
messages/turns/статистику. `_with_memory_blocks` добавляет структурный блок после
свободного текста инвариантов; пустой блок пропускается.

**FR-16. Task packet.** Новый блок `BLOCK_STRUCTURAL_INVARIANTS` в `BLOCK_ORDER`
между `BLOCK_INVARIANTS` и `BLOCK_PROFILE`; kwarg
`structural_invariants=()` в `build_context_packet`; пустой блок пропускается;
без правил packet прежний.

**FR-17. Orchestrator.** Keyword-only `invariants=None`, property `invariants`,
метод `applicable_invariants(task_id)`; `_build_packet` передаёт
`structural_invariants`; `_guarded` после `can_apply` проверяет hard-действие (лог
фазы `action`, провайдер не вызывается); `retry` — та же проверка до
`append_event(RETRY)`; `_commit` повторно проверяет `find_transition_conflict` до
`apply_transition`/`commit_transition` (лог фазы `commit`, ни одной записи в
tasks/task_artifacts/task_events). Новые `STATUS_REFUSED="refused"`,
`ERROR_INVARIANT_CONFLICT="invariant_conflict"`.

**FR-18. app.py.** `InvariantRepository` в `st.session_state`; единая
`_build_agent(chat_id)`, передающая `invariants=` и `task_lookup=`; обработчик Chat
ловит `InvariantConflictError` до общего `except Exception` и показывает
`format_conflict_message` без rerun и без ложного ответа; репозиторий передаётся в
`render_diagnostics_task`; в Diagnostics/Memory добавляется caption о структурных
правилах.

### 4.4 UI

**FR-19. Карточка Chat.** Компактная caption активных ограничений задачи
(`format_active_restrictions`), пустая при отсутствии применимых правил. Карточка
по-прежнему не вызывает провайдера.

**FR-20. Секция `Invariants`.** В `Diagnostics / Task` показываются scope, тип,
enforcement, версия и источник каждого правила, применимые к задаче ограничения,
управление (создание/редактирование/деактивация) и журнал событий. Ключи виджетов
имеют префикс `invariant_`; `st.json` не используется (сырой JSON — только
checkbox-опция в markdown-блоке); вложенных expander нет.

**FR-21. Журнал.** В журнале Tasks рядом с task timeline показываются события
инвариантов (колонки не пересекаются с `event`: `invariant_event`, `code`,
`phase`, `decision`, `created`).

**FR-22. README.** Раздел Дня 14 добавляет Coordinator.

## 5. Нефункциональные требования

**NFR-01. Без новых зависимостей.** stdlib + `streamlit` + `openai` + `dotenv`.

**NFR-02. Слои.** Домен без Streamlit/SQL/провайдера; UI не меняет stage напрямую;
SQLite-репозиторий не решает, что запрещено.

**NFR-03. Идемпотентная миграция без потери данных.**

**NFR-04. Тестируемость.** Unit, integration temp SQLite, FakeClient, AppTest; без
сети и настоящего `.env`.

**NFR-05. Одно соединение на операцию.** WAL, `busy_timeout`,
`PRAGMA foreign_keys` на каждом соединении.

**NFR-06. Без секретов.** Отказ и журнал не содержат секретов и полных служебных
промптов; запрос усечён.

**NFR-07. Регрессия.** Проходит весь `test.bat` и `smoke_test.bat`; число тестов
фиксируется прогоном.

**NFR-08. Производительность.** Ограничение числа запросов на turn: применяемые
правила читаются локально, провайдер не вызывается при отказе.

## 6. Модель состояния

### 6.1 Invariant

| Поле | Тип / значения | Смысл |
|---|---|---|
| `id` | INTEGER PK | Идентификатор |
| `code` | TEXT UNIQUE, immutable | Стабильный код |
| `title` | TEXT NOT NULL | Название |
| `text` | TEXT NOT NULL | Текст правила |
| `scope` | global \| task | Область |
| `task_id` | INTEGER NULL | Задача для task-scope |
| `enforcement` | hard \| advisory | Жёсткость |
| `kind` | guard \| data \| policy | Тип правила |
| `check_kind` | check или "" | Структурная проверка |
| `triggers` | JSON list | Фразы запроса |
| `guard_actions` | JSON list | Запрещённые действия |
| `guard_events` | JSON list | Запрещённые переходы |
| `alternative` | TEXT | Допустимая альтернатива |
| `version` | INTEGER | Версия правила |
| `is_active` | INTEGER | Активность |
| `source` | seed \| user | Источник |
| `created_at`/`updated_at` | TEXT | Время |

### 6.2 InvariantEvent

`id`, `invariant_id`, `code`, `event_type`, `chat_id`, `task_id`, `details_json`,
`created_at`. Append-only; без FK на chats/tasks.

## 7. Инварианты дизайна

**INV-01.** Правила хранятся отдельно от диалога и не извлекаются из последних
сообщений.

**INV-02.** `code` неизменяем; редактирование бампает `version` и пишет событие.

**INV-03.** Удаления правил нет; деактивация сохраняет правило и историю.

**INV-04.** Конфликт не меняет stage/status/current step/результаты/историю задачи.

**INV-05.** Hard-действие проверяется до вызова провайдера; hard-переход — до
любой записи.

**INV-06.** Advisory не блокирует и не заявляет абсолютной гарантии.

**INV-07.** При конфликте не создаётся ложный успешный артефакт и не сохраняется
ложный ответ.

**INV-08.** Служебный структурный блок не становится элементом памяти/facts.

**INV-09.** Журнал `invariant_events` append-only (триггер BEFORE UPDATE).

**INV-10.** Сидирование выполняется один раз по маркеру и не воскрешает удалённые
правила.

## 8. Точки enforcement

| Точка | Фаза | Что проверяется | Нарушение |
|---|---|---|---|
| `ChatAgent.ask` | `request` | triggers против сообщения | `InvariantConflictError`, ничего не пишется |
| `TaskOrchestrator._guarded` | `action` | guard_actions против действия | `STATUS_REFUSED`, провайдер не вызывается |
| `TaskOrchestrator.retry` | `action` | guard_actions против повторяемого действия | `STATUS_REFUSED`, RETRY не пишется |
| `TaskOrchestrator._commit` | `commit` | check_kind/guard_events против перехода | `STATUS_REFUSED`, ни одной записи |

## 9. UI

См. FR-19…FR-21. Ключевые правила:

- Chat: компактная caption ограничений, отказ в месте ввода без rerun.
- `Diagnostics / Task → Invariants`: таблица правил, применимые ограничения,
  управление, журнал; сырой JSON — опция; без `st.json` и вложенных expander.
- `Diagnostics / Memory`: caption о структурных правилах, существующие панели не
  меняются.

## 10. Критерии приёмки (Given/When/Then)

**AC-01 (FR-01, FR-02).** Given корректные поля; When создаётся `Invariant`; Then
валидация проходит; при пустых `title`/`text`, неверном `code`, чужом enum,
отсутствии `task_id` у task-scope, `task_id` у global, guard-полях у advisory или
отсутствии поверхности у hard — `ValueError`.

**AC-02 (FR-03).** Given набор правил; When `select_applicable(task_id)`; Then
возвращаются только активные global и task с совпавшим `task_id`, в порядке
`(global, task)`, внутри — `(hard, advisory, code)`.

**AC-03 (FR-04).** Given пустой набор; When рендерится блок; Then `None`; иначе
блок содержит код, enforcement, scope и альтернативу.

**AC-04 (FR-05, FR-06).** Given inactive/task-чужой/advisory; When проверяются
предикаты; Then конфликта нет; для активного hard — возвращается конфликт нужной
фазы.

**AC-05 (FR-07).** Given конфликт; When форматируется сообщение; Then есть код,
версия, scope, enforcement, что не сделано, альтернатива, путь владельцу.

**AC-06 (FR-08, FR-09, FR-10).** Given новая/старая БД; When открывается
`InvariantRepository`; Then таблицы и индексы создаются идемпотентно, старые
данные целы, `task_events` UPDATE отклоняется, журнал инвариантов append-only.

**AC-07 (FR-11, INV-10).** Given пустая таблица; When открывается репозиторий; Then
сидируются три правила один раз; повторное открытие — no-op; удалённое правило не
воскрешается.

**AC-08 (FR-12, INV-02, INV-03).** Given правило; When создаётся дубль `code`,
редактируется `code`, правило редактируется/деактивируется; Then дубль и смена
`code` отклоняются, версия растёт, `source=user`, события пишутся, правило остаётся
читаемым.

**AC-09 (FR-13, NFR-06).** Given длинный запрос; When `record_conflict`; Then
`details_json` содержит фазу, decision, alternative, запрос ≤200 символов.

**AC-10 (FR-15, SC-01).** Given активный `INV-NO-FSM-BYPASS`; When пользователь
пишет «Skip validation and finish the task»; Then провайдер не вызывается, история
и turns не меняются, поднимается `InvariantConflictError`, конфликт записан.

**AC-11 (FR-15, INV-06).** Given advisory-правило; When сообщение совпадает; Then
запрос не блокируется, правило есть в chat payload.

**AC-12 (FR-15, FR-16).** Given применимые правила; When строится chat payload и
task packet; Then структурный блок добавлен в нужном порядке; без правил payload
прежний.

**AC-13 (FR-17, INV-05).** Given hard-правило с `guard_actions`; When действие;
Then `STATUS_REFUSED`, `payloads == []`, task state версия/статус не изменены,
конфликт `action`.

**AC-14 (FR-17, INV-04).** Given hard-правило с `guard_events`; When `pause`; Then
`STATUS_REFUSED`, ни одной записи в tasks/task_artifacts/task_events, конфликт
`commit`.

**AC-15 (FR-17).** Given hard-правило с `guard_actions`; When `retry`; Then RETRY не
пишется, последнее событие остаётся `API_ERROR`.

**AC-16 (FR-17).** Given задача завершена; When действие недопустимо по FSM; Then
`noop`, а не `refused`.

**AC-17 (FR-17).** Given активный hard-блок; When правило деактивируется; Then
действие проходит штатно.

**AC-18 (FR-17).** Given seed `INV-NO-FSM-BYPASS`; When легитимный `run_validation`
и `VALIDATION_PASSED`; Then переход разрешён, конфликт не пишется.

**AC-19 (FR-18, FR-19, FR-20, FR-21).** Given AppTest с temp store и bare client;
When открыт `Diagnostics / Task`; Then есть секция `Invariants` с правилами,
компактная строка в карточке, отказ при `chat_input`, создание/редактирование/
деактивация работают, события в журнале, `st.json` не используется, вложенных
expander нет.

**AC-20 (NFR-07).** Given полный набор; When `test.bat` и `smoke_test.bat`; Then
всё проходит, число тестов зафиксировано.

**AC-21 (FR-22).** Given README; When проверяется раздел Дня 14; Then он описывает
инварианты, UI и сценарий. (Coordinator.)
