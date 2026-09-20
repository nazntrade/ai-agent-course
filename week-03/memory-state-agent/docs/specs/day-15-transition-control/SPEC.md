# SPEC — Day 15 Controlled State Transitions

Документ описывает усиление уже существующей FSM Day 13/14 в проекте
`week-03/memory-state-agent`. Вторая машина состояний не создаётся:
`tasks.can_apply` и `tasks.apply_transition` остаются единственным источником
истины; Day 15 добавляет понятную причину отказа, отдельный append-only аудит
отказов, read-only UI-диагностику переходов и косметику сайдбара/кнопок.

Документ самодостаточен; идентификаторы, имена файлов, полей и статусов записаны
латиницей, пояснения — по-русски.

Статусы документа: `SPEC_REVIEW: PASS`, `SPEC_GATE_STATUS: PASS`.

**SPEC_GATE пройден по прямому указанию пользователя.** Пользователь задал
объём Day 15 и явно разрешил не останавливаться после плана («После плана не
останавливайтесь»), поэтому отдельный шаг утверждения спецификации не
запрашивался.

## 1. Проблема и цель

FSM Day 13 умеет отклонять недопустимое действие, но отказ малопонятен: UI и
вызывающий код получают `noop` с технической строкой вида
`run_step is not allowed in stage=planning, ...`. Нет ответа на вопросы «почему
именно» и «что разрешено сейчас»; отказы не сохраняются отдельно от журнала
задач, поэтому их нельзя посмотреть в диагностике; карточка не подсказывает
следующее действие, а активный чат в сайдбаре помечен грубым символом `▶`.

**Цель.**

- Дать стабильные reason-коды и человеческое объяснение отказа вместе со списком
  действий, разрешённых сейчас.
- Записывать каждый отказ в отдельную append-only таблицу, не смешивая её с
  `task_events` и не меняя состояние задачи.
- Показать переходы в `Diagnostics / Task` read-only: текущее состояние,
  allowed, решения по каждому действию, последние отказы.
- Подсветить рекомендованное действие в карточке и активный чат в сайдбаре.

**Границы изменения поведения.** FSM `tasks.can_apply`/`apply_transition`,
транзишн-хендлеры, статусы и версии не меняются; hard-инварианты Day 14
продолжают блокировать переходы так же. Существующие тексты отказа могут
смениться на более понятные, но статус остаётся прежним (`noop`/`error`).

## 2. Границы задачи

### 2.1 IN scope

- `tasks.py`: `ACTION_LABELS`, reason-коды, `TransitionDecision`,
  `explain_transition`, `format_allowed_actions`, `format_refusal`.
- `task_storage.py`: append-only таблица `task_transition_attempts`,
  `TransitionAttempt`, `record_transition_attempt`, `list_transition_attempts`.
- `task_orchestrator.py`: объяснённый `_noop`, аудит отказов, аудит отказанного
  commit, read-only `transition_guard_report`.
- `task_ui.py`: перенос `ACTION_LABELS`, чистые форматтеры, expander
  `Transition guard`, CSS-подсветка рекомендованного действия.
- `app.py`: подпись активного чата без `▶`, CSS-подсветка активного чата.
- Тесты unit / integration temp SQLite / FakeClient / AppTest.
- Комплект `docs/specs/day-15-transition-control/`.

### 2.2 OUT scope

- Вторая машина состояний, изменение `can_apply`, транзишн-хендлеров и таблицы
  `task_events`.
- Изменение бизнес-логики hard-инвариантов Day 14.
- Запись отказов в `task_events`.
- Governance-файлы (`AGENTS.md`, `PROJECT_RULES.md`, `opencode.json`,
  `.opencode/agents/*.md`).
- README (обновляет Coordinator), реальные платные вызовы API.

## 3. Пользовательские сценарии

**SC-01 (понятный отказ).** Пользователь на стадии planning нажимает действие,
доступное только на execution. Карточка показывает отказ с причиной
`expected_action_mismatch` и списком «Allowed now»; провайдер не вызывается,
stage/status/version не меняются.

**SC-02 (аудит).** Каждый отказ появляется в `Diagnostics / Task →
Transition guard` в блоке recent refusals; строки лежат в отдельной таблице и не
входят в `task_events`.

**SC-03 (терминальное состояние).** После done или cancel любое доменное
действие даёт `noop` с причиной `task_is_terminal`; отказ записан, провайдер не
вызван.

**SC-04 (перезапуск).** Pause, полный перезапуск (новые `ChatStore`,
`TaskRepository`, `TaskOrchestrator` на том же файле), resume: та же задача и
шаг, `version` +1, без дублей событий; аудит отказов сохраняется.

**SC-05 (сайдбар).** Активный чат не помечается `▶`, а выделяется мягким зелёным
фоном и рамкой; селектор точный и не задевает похожие id (`chat_4` vs `chat_42`).

**SC-06 (рекомендация).** В карточке подсвечено ровно то действие, которое
предлагает текущий `expected_action_type`; множество кнопок по-прежнему равно
`allowed_actions`.

## 4. Функциональные требования

**FR-01. Единый источник подписей.** `tasks.ACTION_LABELS` покрывает все
`DOMAIN_ACTIONS` и `UI_ACTIONS`; `task_ui` импортирует его и не хранит копию.

**FR-02. Reason-коды.** В `tasks.py` определены `REASON_ALLOWED=""`,
`REASON_TASK_NOT_FOUND`, `REASON_TERMINAL`, `REASON_PAUSED`, `REASON_BLOCKED`,
`REASON_EXPECTED_ACTION_MISMATCH`, `REASON_PROGRESS_INCOMPLETE`,
`REASON_RETRY_REQUIRES_API_ERROR`, `REASON_CONFIRMATION_REQUIRED`,
`REASON_NOT_ALLOWED`, `REASON_INVALID_TRANSITION`, `REASON_INVALID_PAYLOAD`,
кортеж `REFUSAL_REASONS` и `REASON_TEXTS` (английские пояснения).

**FR-03. Decision.** `@dataclass(frozen=True) class TransitionDecision`: `action`,
`allowed`, `reason=REASON_ALLOWED`, `allowed_actions=()`, `message=""`.

**FR-04. Объяснение.** `explain_transition(task, action, *, last_event_type=None,
progress=None)`: `allowed_actions = can_apply(...)`;
`allowed = action in allowed_actions`; reason-лестница только для недопустимого
действия (task None → not_found; terminal; paused; blocked; finish_execution с
неполными шагами → progress_incomplete; retry без API_ERROR →
retry_requires_api_error; иначе expected_action_mismatch/not_allowed);
`message = format_refusal(decision)`. `can_apply` и хендлеры не меняются.

**FR-05. Формат.** `format_allowed_actions(actions)` рендерит подписи через
`ACTION_LABELS`, пустой набор → `"none"`. `format_refusal(decision)` возвращает
`"<Label> is not allowed: <reason text>. Allowed now: <...>."`, а при пустом
наборе — `"... No action is allowed in the current state."`.

**FR-06. DDL.** В `_SCHEMA` добавляется append-only `task_transition_attempts`
(id, task_id FK ON DELETE CASCADE, action, from_stage, from_status,
expected_action_type, reason, allowed_actions_json, created_at), индекс
`(task_id, id)` и триггер `BEFORE UPDATE RAISE(ABORT)`. Миграция только через
`CREATE ... IF NOT EXISTS`, повторное открытие идемпотентно.

**FR-07. Модель и API хранилища.** `@dataclass class TransitionAttempt`; методы
`record_transition_attempt(task_id, action, *, reason, allowed_actions=(),
from_stage=None, from_status=None, expected_action_type=None) -> int` (только
INSERT; нет задачи → `TaskNotFoundError`) и `list_transition_attempts(task_id,
limit=None) -> list` (хронологически; при `limit` — последние N, всё равно по
возрастанию id).

**FR-08. Изоляция аудита.** Отказы не появляются в `task_events`, не меняют
`version`, stage, status и артефакты; удаление чата каскадно удаляет строки
аудита.

**FR-09. Orchestrator `_noop`.** `_noop(task, action, reason=None)`: для
известной задачи считает `explain_transition` (с `latest_event_type`), при
переданном `reason` подменяет код, best-effort пишет аудит, возвращает
`TaskActionResult(status=STATUS_NOOP, error_kind=ERROR_INVALID_TRANSITION,
error_message=<format_refusal>)`. Для `task is None` — без аудита.

**FR-10. Причины вызовов.** `_guarded` → `_noop(task, action)`; retry noop →
`REASON_RETRY_REQUIRES_API_ERROR` (нет последнего `API_ERROR`) или
`REASON_NOT_ALLOWED` (нечего повторять); cancel без подтверждения →
`REASON_CONFIRMATION_REQUIRED`.

**FR-11. Отказ commit.** В `_commit` при `InvalidTransitionError` /
`TransitionPayloadError` пишется best-effort аудит
(`REASON_INVALID_TRANSITION` / `REASON_INVALID_PAYLOAD`), но возвращается
прежний `_error` (статус не подменяется).

**FR-12. Hard-инварианты.** `_refused` (конфликт Day 14) по-прежнему пишет
только `invariant_events` и ничего не пишет в новую таблицу.

**FR-13. Read-only отчёт.** `transition_guard_report(task_id, *, limit=10) ->
TransitionGuardReport(task, allowed, decisions, refusals)`, где
`allowed = can_apply(...)`, `decisions = [explain_transition(task, a, ...) for a
in DOMAIN_ACTIONS]`, `refusals = repository.list_transition_attempts(task_id,
limit=limit)`. Провайдер не вызывается, ничего не записывается; неизвестная
задача даёт пустой отчёт.

**FR-14. UI-форматтеры.** Чистые `recommended_action(task)`,
`is_review_plan_recommended(task)`, `recommended_action_css(task)`,
`guard_decision_rows(decisions)`, `transition_attempt_rows(attempts)`. CSS
использует зелёный `#2e7d32` и точный селектор
`div[class~="st-key-task_action_<a>"] button` /
`div[class~="st-key-task_review_plan"] button`, иначе пустая строка. Колонки
аудита не пересекаются с `event`/`invariant_event`.

**FR-15. Панель `Transition guard`.** В `Diagnostics / Task` сразу после
`_render_task_fields(task)` и до `_render_plan(...)` рисуется expander
`Transition guard` (текущее состояние, allowed, dataframe решений, dataframe
последних отказов, caption об отдельном append-only аудите). Единственный
виджет — безопасная кнопка `Test Finish execution` (FR-21); других
transition-кнопок и форм, `st.json` и вложенных expander нет; провайдер не
вызывается.

**FR-16. Подсветка карточки.** В `_render_active_card` перед `Review
plan`/действиями инжектируется CSS рекомендованного действия. Логика выбора
кнопок не меняется и остаётся `allowed_actions`.

**FR-17. Сайдбар.** Подпись активного чата — `chat.title` без `▶`; при
`chat_id is not None` перед циклом чатов инжектируется CSS с точным селектором
`div[class~="st-key-chat_{chat_id}"] button`, мягким зелёным фоном
`rgba(46,125,50,0.18)` и рамкой `rgba(46,125,50,0.75)` (`!important`).

**FR-18. Документация.** Комплект `docs/specs/day-15-transition-control/`.

**FR-19. Снимок фактов в execution-пакете.** `task_context` добавляет
`BLOCK_TASK_FACTS = "task_facts"` **после** `task_snapshot` и до артефактов
только для `STAGE_EXECUTION`. `build_step_facts(task, *, events=(), refusals=())`
копирует `stage`, `status`, `current_step`, `current_step_index`,
`expected_action_type`, `expected_action_text`, `version`, события
(`id`/`event_type`/`created_at`) и отказы (`id`/`action`/`reason`/`created_at`) в
frozen `StepFacts` из `task_prompts`; `format_step_facts_block` рендерит блок.
`ContextPacket.step_facts` заполнен только для execution. Оркестратор передаёт не
более последних 20 событий и 10 отказов (read-only). Снимок живёт в
`task_prompts`, чтобы не было цикла `task_context` ↔ `task_prompts`.

**FR-20. Валидация текста шага.** `task_prompts.validate_step_text(text, facts)`
бросает `StepFactsViolationError` с `violations` при вымышленных ссылках (токены
`EVT[-_]\w*`); ссылках на события по номеру
(`(?:event|событие|event[_ ]?id)\D{0,8}(\d+)`), если номера нет в
`facts.events`; заявленной стадии/статусе
(`(?:stage|стадия|status|статус)\s*[:=]\s*(\w+)`), не совпадающих со снимком;
«задача выполнена»/`status completed` при `status != completed`; отрицании
записанного события или перехода в текущую стадию в предложении с отрицанием
(«не выполнялся», «не зафиксирован», «не наступил», «не состоялся»,
«отсутствует в журнале»). Кадры `transition guard`/«аудит отказов»/«запрещ» не
считаются противоречием; `facts is None` отключает проверку.
`StageExecutor.run_step(..., facts=None)` после непустого непрерванного текста
делает один retry с `messages + step_retry_feedback(exc)` и большим бюджетом; при
повторе нарушения — `API_ERROR(invalid_response)` с компактным `error` (≤200
символов). Биллинг и `attempts` как при plan-retry; частичный стрим отбрасывается;
`StageExecutionResult` не расширяется; `facts=None` сохраняет прежнее поведение.

**FR-21. Безопасный probe-перехода.** `TaskOrchestrator.probe_refused_transition(
task_id, action=ACTION_FINISH_EXECUTION) -> TransitionProbeResult(task, action,
allowed, reason="", message="", audit_id=None, error=None)`. Probe вызывает
**только** production `_guarded` и никогда `_simple`/`_commit`/`apply_transition`;
провайдер не вызывается, `stage/status/version/current_step/current_step_index` и
`task_events` не меняются.
- `blocked is None` (действие разрешено) → `allowed=True`, `audit_id=None`,
  ничего не пишется;
- `blocked.status == STATUS_NOOP` → аудит уже записан production-цепочкой
  `_noop → _refusal_decision → _record_refusal`; probe перечитывает
  `list_transition_attempts(task_id, limit=5)` и берёт строку с этим `action` и
  максимальным id: `reason` и `audit_id` — из storage, не из LLM;
- `blocked.status == STATUS_REFUSED` (hard-инвариант) — это invariant-journal,
  строки в `task_transition_attempts` нет → `audit_id=None`,
  `message=blocked.error_message`;
- задача не найдена → `error`.

**FR-22. Недоказанная попытка.** В `task_prompts` определён
`REASON_UNCONFIRMED_ATTEMPT = "unconfirmed_attempt"` (не входит в
`tasks.REFUSAL_REASONS`). `_unconfirmed_attempt_violations(text, facts)`
вызывается в `validate_step_text` **после** существующих проверок и отклоняет
утверждение о совершённой/отклонённой/записанной в аудит попытке перехода, если
снимок storage её не подтверждает: `refusals` пусты → нарушение; назван
отсутствующий `action`/`reason` → нарушение; performed-ветка называет хотя бы
одну стадию и **ни одна** из названных не совпадает с `facts.stage` → нарушение.
Названный `refusal.action` (как есть или `_`→пробел) либо `refusal.reason` из
непустого снимка подтверждает факт; непустые `refusals` без конкретики тоже
допустимы. Условные предложения (`\bбы\b`, будет/будут, может/могут,
показывает/покажет, «если») утверждением не считаются; эти слова матчатся по
границам слов. Маркер под отрицанием («не выполнен», «не инициирован», «не
отклонён», «не заблокирован», «не зафиксирован», «не совершён», «не
осуществлён», «не произведён», «не состоялся») — отрицание, а не утверждение.
`_AUDIT_REFERENCE_RE = r"(?:audit|аудит)\s*(?:id\s*|#\s*|№\s*|:\s*)?(\d+)"`
(дата после «аудит от …» не читается как id; формы `audit #12`, `audit id 12`,
`audit 12`, `аудит №12`, `аудит: 12` читаются), а неизвестный `audit id` даёт
`REASON_FABRICATED_REFERENCE` (через `_known_refusal_ids`).

**FR-23. Правило read-only Guard в промпте.** `TASK_EXECUTION_SYSTEM_PROMPT`,
напоминание в `build_step_messages` и `step_retry_feedback` требуют брать факт
попытки/отказа только из `task_facts` (refusals/audit id), а read-only
`Transition guard` описывать исключительно условно («Finish execution сейчас был
бы отклонён с причиной …»).

### 4.1 Модель guard планирования (итерация 3)

`task_prompts` проверяет план на двух **поверхностях**: `steps` (время —
`execution`) и `acceptance_criteria` (время — `validation` после
`EXECUTION_FINISHED`). На момент планирования уже прошли `TASK_CREATED`,
`PLAN_CREATED`, `PLAN_ACCEPTED`; будущими остаются `VALIDATION_PASSED/FAILED` и
`done/completed`. `plan_violations(plan)` возвращает
`PlanViolation(index, title, reason, location)` с `location` = `step`/`criterion`
(1-based внутри поверхности); `plan_step_violations(plan)` — steps-only
совместимый вид. Классы по precedence: **HARD** → **future_stage** →
**missing_entity** → **nonexistent_transition** → **prior_stage**; reason-коды
`requires_other_stage`, `missing_entity`, `nonexistent_transition`. Прошлые стадии
(`plan_accepted`/`plan_created`, «проверка planning») допустимы только в
ретроспективной рамке (`Event timeline`, «журнал», «истори», «артефакт»);
возврат в `planning` запрещён, кроме framework guard-frame (`Transition guard`,
«аудит отказов», «диагностик», `refusal audit`). Code-verb стемы
(`реализ|добав|настро|…`) снимают future/prior-срабатывания, HARD — нет.
`parse_plan_response` использует `plan_violations`, а `plan_retry_feedback`
формирует отдельные секции шагов и критериев.

**Ограничения эвристики.** Классификатор основан на подстроках и стемах без
морфологического разбора: возможны ложные срабатывания (например,
«выполнить переход в execution» в предметной задаче) и пропуски
перефразированных future-действий. HARD-фразы остаются безусловными, поэтому
они приоритетнее code-verb исключения; список маркеров точечно сужается, а не
тест.

### 4.2 Граница «факты storage ↔ текст LLM» (итерация 4)

Свободный текст результата шага — единственный ответ стадии без строгого
JSON-контракта, поэтому раньше модель могла сочинить Event timeline: выдумать
`EVT-EXEC-001`, заявить `plan_accepted (переход в execution ещё не выполнялся)` и
описать фиктивную траекторию, противоречащую SQLite-журналу и UI. Разделение
ответственности:

- **Журнал принадлежит коду.** Фактические `stage`/`status`/`current_step`,
  идентификаторы событий, timeline и аудит отказов читает storage; код
  прикрепляет их к пакету блоком `task_facts` (`task_context`) и никогда не
  передаёт модели право их переписывать. ID/ссылки в тексте модели валидируются
  по этому же снимку (`task_prompts.validate_step_text`), а не по вере в ответ.
- **Модель формирует только содержимое шага**, опираясь на переданные кодом
  факты. Системный промпт `TASK_EXECUTION_SYSTEM_PROMPT` и напоминание в
  `build_step_messages` запрещают выдумывать и называть отсутствующие
  идентификаторы/события, заявлять чужую стадию/статус и отрицать записанные
  события.
- **Нарушение = один retry с `step_retry_feedback`**, затем
  `API_ERROR(invalid_response)` без артефакта и без изменения stage/version;
  частичный стрим не сохраняется. Текст с реальными фактами не наказывается.

Детектор консервативен и намеренно эвристичен: он ловит явные формы
(`EVT-...`, числовые ссылки на неизвестные события, `stage:/status:`-заявления,
отрицание записанного события) и не выполняет морфологический разбор. Возможны
ложные пропуски перефразированных утверждений и ложные срабатывания на
предметном тексте; кадры `transition guard`/«аудит отказов»/«запрещ» исключены,
а реальный ID из снимка разрешён.

### 4.3 Достоверность execution-шага (итерация 5)

Дефект: текст шага 3 начинался с «Инициирован недопустимый переход из стадии
execution…», хотя в `task_facts.refusals` было пусто, Event timeline содержал
только `STEP_COMPLETED`, а реально был лишь read-only просмотр `Transition
guard` (`Finish execution` = no). Модель заявила совершённую/отклонённую попытку
без подтверждения storage. Исправление:

- **Неутверждаемая попытка.** Новый `unconfirmed_attempt` валидатор (FR-22)
  отклоняет такое утверждение, повторяя тот же retry-контракт: один retry с
  `step_retry_feedback`, затем `API_ERROR(invalid_response)` без артефакта и без
  изменения состояния.
- **Read-only Guard — только условно.** `Transition guard` описывается фразой
  «…сейчас был бы отклонён с причиной …»; безусловное «переход инициирован /
  отклонён / записан» запрещено и промптом (FR-23), и валидатором.
- **Факт из кода.** Единый источник фактов — `task_transition_attempts`:
  execution-пакет получает снимок через `_step_fact_sources`, валидатор сверяет
  текст с ним, guard-панель рендерит те же строки, а `format_result_journal_line`
  даёт code-rendered `Journal: #…`.
- **Явный probe.** Кнопка `Test Finish execution` (FR-21) вызывает production
  transition-guard; при отказе production-цепочка пишет append-only audit, и
  панель показывает реальный `reason` и `audit id` (`Refusal audit #<id>: …`).
  Без клика панель ничего не пишет; при `audit_id=None` (hard-инвариант или сбой)
  id не выдумывается.

## 5. Нефункциональные требования

**NFR-01. Без новых зависимостей.** stdlib + `streamlit` + `openai` + `dotenv`.

**NFR-02. Слои.** Домен без Streamlit/SQL/провайдера; UI не меняет stage
напрямую; SQLite-репозиторий не решает, что запрещено.

**NFR-03. Идемпотентная миграция без потери данных.**

**NFR-04. Тестируемость.** Unit, integration temp SQLite, FakeClient, AppTest;
без сети и настоящего `.env`.

**NFR-05. Одно соединение на операцию.** WAL, `busy_timeout`,
`PRAGMA foreign_keys` на каждом соединении.

**NFR-06. Без секретов.** Аудит и сообщения не содержат секретов и полных
служебных промптов.

**NFR-07. Регрессия.** Проходит весь `test.bat` и `smoke_test.bat`; число тестов
фиксируется прогоном.

**NFR-08. Read-only диагностика.** Построение отчёта и рендер не вызывают
провайдера и не пишут в БД.

## 6. Модель данных

### 6.1 TransitionDecision (домен)

| Поле | Тип | Смысл |
|---|---|---|
| `action` | str | Доменное действие |
| `allowed` | bool | Разрешено ли сейчас |
| `reason` | str | Reason-код или `""` |
| `allowed_actions` | tuple | Текущий набор `can_apply` |
| `message` | str | Готовое объяснение |

### 6.2 TransitionAttempt (SQLite)

| Колонка | Тип | Смысл |
|---|---|---|
| `id` | INTEGER PK | Идентификатор строки аудита |
| `task_id` | INTEGER FK CASCADE | Задача |
| `action` | TEXT | Отклонённое действие |
| `from_stage`/`from_status` | TEXT | Состояние на момент отказа |
| `expected_action_type` | TEXT | Ожидаемое действие на момент отказа |
| `reason` | TEXT | Reason-код |
| `allowed_actions_json` | TEXT | Разрешённый тогда набор |
| `created_at` | TEXT | Время |

### 6.3 TransitionGuardReport (use case)

`task`, `allowed`, `decisions`, `refusals`.

## 7. Инварианты дизайна

**INV-01.** `can_apply`/`apply_transition` — единственный источник истины;
`explain_transition` не дублирует правила, а читает их.

**INV-02.** Отказ не меняет stage, status, current step, version, артефакты и
`task_events`.

**INV-03.** Аудит отказов append-only (триггер BEFORE UPDATE) и отделён от
`task_events` и `invariant_events`.

**INV-04.** Hard-инвариант по-прежнему не пишет в аудит отказов, только в
`invariant_events`.

**INV-05.** Диагностика переходов read-only: без форм, `st.json`, вложенных
expander и вызовов провайдера. Единственное исключение — безопасная кнопка
`Test Finish execution`, чей явный клик вызывает production-guard и может
добавить только одну append-only строку аудита; task, `task_events`, артефакты и
версия не меняются.

**INV-06.** Точные CSS-селекторы `class~=` не задевают похожие ключи.

**INV-07.** Миграция аддитивная и идемпотентная.

**INV-08.** Модель не владеет журналом: фактические ID/события/stage/status
прикрепляет код из storage, а свободный текст шага валидируется тем же снимком —
включая факт попытки/отказа (`unconfirmed_attempt`, invented `audit` id);
нарушение даёт retry и затем `invalid_response`, ничего не записывая.

## 8. Критерии приёмки (Given/When/Then)

**AC-01 (FR-01).** Given все доменные и UI-действия; When проверяются подписи;
Then `ACTION_LABELS` покрывает каждое, `task_ui` не хранит копию.

**AC-02 (FR-02…FR-05).** Given состояние и действие; When вызывается
`explain_transition`; Then `allowed` совпадает с `can_apply`, для отказов
выдаётся специфичный reason, который появляется в `format_refusal` вместе с
allowed.

**AC-03 (FR-04).** Given done/cancelled/paused/blocked, этап до approve,
`finish_execution` с неполными шагами, retry без `API_ERROR`; When объясняется
действие; Then причины `task_is_terminal`/`status_paused`/`status_blocked`/
`expected_action_mismatch`/`progress_incomplete`/`retry_requires_api_error`.

**AC-04 (FR-06…FR-08).** Given temp SQLite; When пишутся и читаются отказы; Then
round-trip сохраняет `allowed_actions`, UPDATE отклоняется триггером, отказы не
в `task_events` и не меняют `version`, удаление чата каскадит, повторное
открытие идемпотентно.

**AC-05 (FR-09…FR-12).** Given FakeClient; When действие недопустимо; Then
`STATUS_NOOP`, провайдер не вызван, состояние/версия/артефакты не меняются,
отказ записан; hard-конфликт по-прежнему `STATUS_REFUSED` без новой записи.

**AC-06 (FR-13).** Given задача; When строится `transition_guard_report`; Then
он возвращает allowed/decisions/refusals без вызова провайдера и без записи.

**AC-07 (FR-14).** Given decisions/attempts; When рендерятся строки; Then
колонки соответствуют спецификации, CSS содержит `#2e7d32` и точный `class~=`
селектор.

**AC-08 (FR-15).** Given AppTest в `Diagnostics / Task`; When задача имеет отказ;
Then expander `Transition guard` показывает allowed, решения и аудит, без
`st.json` и вложенных expander; единственный виджет панели — безопасная кнопка
`Test Finish execution`; её **явный** клик вызывает production-guard и может
добавить ровно одну append-only строку в `task_transition_attempts`, при этом
`stage`/`status`/`version`/`current step`/`task_events`/артефакты не меняются.

**AC-09 (FR-16).** Given AppTest в Chat; When карточка показывает набор
`allowed_actions`; Then присутствует CSS рекомендованного действия, а
запрещённые действия не отрисованы.

**AC-10 (FR-17).** Given AppTest сайдбара; When чат активен; Then его подпись без
`▶`, а разметка содержит его точный CSS-селектор.

**AC-11 (NFR-07).** Given полный набор; When `test.bat` и `smoke_test.bat`; Then
всё проходит, число тестов зафиксировано.

**AC-12 (FR-18).** Given комплект документации; When проверяется раздел Day 15;
Then он описывает требования, план и способы проверки. (Coordinator/Developer.)

**AC-13 (FR-19, FR-20, INV-08).** Given execution-пакет и свободный текст шага;
When блок `task_facts` содержит реальные id событий, а текст выдумывает
`EVT-...`/чужую стадию/отрицает записанное событие; Then детектор отклоняет
текст, `task_stage` делает один retry с `step_retry_feedback`, при повторе
пишет `API_ERROR(invalid_response)` без артефакта и без изменения
stage/current_step/version, а чистый текст с реальными фактами проходит без
retry; UI показывает code-rendered строку журнала под результатом шага.

## 9. UI

- Карточка: подсвечено ровно рекомендованное действие; набор кнопок не меняется.
- `Diagnostics / Task → Transition guard`: состояние, allowed, решения, аудит.
  Панель не вызывает провайдера и не меняет задачу; единственный её виджет —
  безопасная кнопка `Test Finish execution`. Её явный клик вызывает
  production-guard: при мягком отказе добавляется ровно одна append-only строка
  в `task_transition_attempts`, при разрешённом действии не пишется ничего, а
  `stage`/`status`/`version`/`current step`/`task_events`/артефакты остаются
  неизменными.
- Сайдбар: активный чат без `▶`, мягкая зелёная подсветка точным селектором.
