# SPEC — Day 13 Task State Machine

Документ описывает требования к состоянию задачи в проекте `week-03/memory-state-agent`.
Команда пользователя на реализацию сформулирована как `ДЕЛАЕМ`: durable, версионируемое
состояние задачи с конечным автоматом (FSM) по стадиям, артефактами, append-only журналом,
per-stage context packet и минимальным UI. Документ самодостаточен: все нужные определения,
идентификаторы и правила приведены в его тексте.

Идентификаторы, имена файлов, событий, полей и статусов записаны латиницей; пояснения — по-русски.
Статусы этого документа: `SPEC_REVIEW: PASS`, `SPEC_GATE_STATUS: PASS`.

## 1. Проблема и цель

Агент помнит диалог (short-term/working/long-term memory, профили, ветки, стратегии контекста),
но не имеет формализованного состояния задачи. После перезапуска нельзя продолжить прерванную
работу без повторного объяснения цели: в базе нет ни цели, ни плана, ни того, на каком шаге
пользователь остановился, ни того, какое действие ожидается следующим.

**Цель.** Ввести durable, версионируемое состояние задачи:

- FSM `planning → execution → validation → done` с проверяемыми preconditions;
- неизменяемые артефакты и append-only журнал событий;
- pause/resume, переживающие полный перезапуск приложения;
- block/unblock, cancel с подтверждением, retry после ошибки API;
- per-stage context packet с примерной стоимостью блоков;
- суммарная статистика стоимости задачи;
- минимальный UI: вертикальный индикатор стадий в `Chat`, режим `Diagnostics / Task`, карточка задачи.

**Границы изменения поведения.** Дни 10–12 (стратегии контекста, слои памяти, пользовательские
профили, история чатов и веток, streaming, статистика токенов и стоимости, безопасное поведение
при ошибках API, существующие тесты и smoke-test) должны сохранить текущее поведение. Основной
Payload обычного чата не меняется (см. FR-28).

## 2. Границы задачи

### 2.1 IN scope

- Домен `Task` / `TaskArtifact` / `TaskEvent` / `WorkflowProfile`.
- FSM с preconditions; закрытая таблица переходов.
- SQLite-хранилище и идемпотентная миграция.
- pause/resume с сохранением после полного перезапуска приложения.
- block/unblock.
- cancel с подтверждением.
- retry после ошибки API.
- reject/re-plan плана.
- per-stage context packet с примерной стоимостью блоков.
- Суммарная статистика стоимости задачи.
- Вертикальный индикатор стадий в `Chat`.
- Режим `Diagnostics / Task`.
- Полные тесты (unit, интеграционные с временной SQLite, FakeClient, AppTest).
- Обновление `README.md` и сценарий демонстрационного видео.

### 2.2 OUT scope

- Разные модели/provider на стадию (только точка расширения).
- Дополнительные workflow-профили в UI.
- Визуальный редактор workflow.
- Автоматический `MemoryExtractor`.
- Фоновые воркеры.
- Jira-подобные функции.
- Выполнение shell-команд задачей.
- Распределённая очередь.
- Release/security gate, SAST/DAST/SCA/SBOM.
- Полноценный coding-agent.
- Публичное удаление задач/событий.
- Редактирование артефактов.
- Изменения `PROJECT_RULES.md` (раздел SDD — задача Configurator, вне зоны Developer).
- Изменения `context.py`, `strategies.py`, `facts.py`, `profile.py`, `stats.py`, `tokens.py`,
  `models.py`, `app_logic.py`, `test.bat`, `smoke_test.bat`, `run_app.bat`, `requirements.txt`.

## 3. Пользовательские сценарии

**SC-01 (основной жизненный цикл).** create → planning → accept plan → execution (шаг за шагом)
→ pause → полное закрытие и повторный запуск приложения → resume → продолжить с текущего шага
→ finish execution → validation → done.

**SC-02 (переделка дефектных шагов).** validation failed с несколькими дефектными шагами →
переделка только дефектных шагов → validation повторно → done.

**SC-03 (ошибка API).** Ошибка API до первого токена или обрыв во время stream → Retry →
успешное завершение шага; частичный результат не сохранён.

**SC-04 (блокировка).** block с причиной и ожидаемым действием → unblock → продолжение с той же
стадии.

**SC-05 (отмена).** cancel с подтверждением; журнал и артефакты сохранены.

**SC-06 (чат без задач).** Чат без задач показывает компактное приглашение создать задачу;
поведение Дней 10–12 не меняется.

## 4. Функциональные требования

### 4.1 Домен и FSM

**FR-01. Домен Task.** Модуль `tasks.py` описывает `Task` со полями: `id`, `chat_id`,
`workflow_profile_id`, `title`, `goal`, `stage`, `status`, `current_step` (TEXT,
человекочитаемая подпись), `current_step_index` (INTEGER NULL, 1-based номер шага в плане),
`expected_action_type`, `expected_action_text`, `pause_reason`, `version`, `created_at`,
`updated_at`. `stage ∈ planning | execution | validation | done`.
`status ∈ active | paused | blocked | completed | cancelled`. Новая задача:
`stage=planning`, `status=active`, `version=1`, `expected_action_type=run_planning`.

**FR-02. Домен TaskArtifact.** Поля: `id`, `task_id`, `stage`, `kind`, `revision`, `content`
(JSON), `created_at`. `kind ∈ task_brief | specification | plan | execution_result |
validation_result | final_result`. Артефакты неизменяемы; `revision = MAX(kind)+1` в той же
транзакции. Схемы `content`:

- `task_brief`: `{text}`;
- `specification`: `{markdown}`;
- `plan`: `{summary, acceptance_criteria: [непустые строки], steps: [{index: 1..N уникальные,
  title, description}]}`;
- `execution_result`: `{step_index, round (1-based номер ревизии для шага), text}` — пишется
  только при успехе;
- `validation_result`: `{passed: bool, defects: [{step_index, description}], notes}`;
  при `passed=false` `defects` непуст и `step_index ∈ 1..N`; при `passed=true` `defects` пуст;
- `final_result`: `{markdown}` — одна ревизия.

**FR-03. Домен TaskEvent.** Поля: `id`, `task_id`, `event_type`, `from_stage`, `to_stage`,
`from_status`, `to_status`, `payload_json`, `idempotency_key`, `created_at`. Типы событий:
`TASK_CREATED`, `PLAN_CREATED`, `PLAN_REJECTED`, `PLAN_ACCEPTED`, `STEP_COMPLETED`,
`EXECUTION_FINISHED`, `VALIDATION_PASSED`, `VALIDATION_FAILED`, `PAUSE`, `RESUME`, `BLOCK`,
`UNBLOCK`, `CANCEL`, `API_ERROR`, `RETRY`. Журнал append-only: только INSERT.

**FR-04. Домен WorkflowProfile.** Поля: `id`, `name` (UNIQUE), `display_name`, `stages_json`,
`instructions_json`, `executors_json`, `models_json`, `validation_json`, `is_default`,
`created_at`, `updated_at`. Для Дня 13 — один default workflow; хранилище допускает добавление
других профилей без изменения кода.

**FR-05. FSM и preconditions.** `TaskStateMachine` в `tasks.py` содержит таблицу переходов и
проверяет preconditions до любой записи. Разрешены только переходы из раздела 9.

**FR-06. Отклонение недопустимых переходов.** Запрещённые переходы отклоняет доменный слой,
и ничего не пишется. Ошибка — `InvalidTransitionError`; некорректный payload перехода —
`TransitionPayloadError`; успешный результат — `TransitionResult`.

**FR-07. Инварианты.** Домен и хранилище обеспечивают INV-01…INV-11 раздела 7.

**FR-08. Expected actions и badges.** `expected_action_type ∈ run_planning | confirm_plan |
run_step | finish_execution | run_validation | review_result | user_action | none`. Ожидаемый
текст: `TASK_CREATED` → «Run planning»; `PLAN_CREATED` → «Accept or reject the plan»;
`PLAN_REJECTED` → «Run planning»; `PLAN_ACCEPTED` → «Run the current step»; `STEP_COMPLETED` →
«Run the current step» либо «Finish execution»; `EXECUTION_FINISHED` → «Run validation»;
`VALIDATION_PASSED` → «Review the result»; `VALIDATION_FAILED` → «Fix the defects and run the
step»; `BLOCK` → «Provide the required input and unblock»; `CANCEL` → пусто.
badges: active (planning/execution/validation) → `RUNNING`; paused → `PAUSED`; blocked →
`BLOCKED`; completed → `COMPLETED`; cancelled → `CANCELLED`.

**FR-09. can_apply — единый источник.** `can_apply(task, last_event_type)` возвращает набор
доступных действий и используется и доменом, и UI. Доменные действия: `run_planning`,
`reject_plan`, `accept_plan`, `run_step`, `finish_execution`, `run_validation`, `pause`, `resume`,
`block`, `unblock`, `cancel`, `retry`. UI-действия: `open_result`, `new_task`,
`open_diagnostics`. Наборы:

| Состояние | Набор действий |
|---|---|
| planning/active + run_planning | run_planning, pause, block, cancel |
| planning/active + confirm_plan | accept_plan, reject_plan, pause, block, cancel |
| execution/active + run_step | run_step, pause, block, cancel |
| execution/active + finish_execution | finish_execution, pause, block, cancel |
| validation/active + run_validation | run_validation, pause, block, cancel |
| paused | resume, block, cancel |
| blocked | unblock, cancel |
| done | open_result, new_task |
| cancelled | new_task |

Если последнее событие — `API_ERROR`, дополнительно доступен `retry`. В `Chat` показываются
только действия из `can_apply`; домен отклоняет всё остальное.

### 4.2 Хранилище

**FR-10. DDL и индексы.** `TaskRepository` создаёт таблицы `workflow_profiles`, `tasks`,
`task_artifacts`, `task_events` со всеми колонками, partial-индексами и триггером
`BEFORE UPDATE RAISE(ABORT)` для событий. Точная схема — в разделе 10.

**FR-11. Идемпотентная миграция.** `TaskRepository` принимает путь к БД от `ChatStore.db_path`
(аддитивное публичное свойство). Все объекты создаются через `CREATE TABLE/INDEX/TRIGGER IF NOT
EXISTS`; `TaskRepository` повторяет `PRAGMA foreign_keys = ON`, `WAL`, `busy_timeout` на каждом
соединении. Старые БД (Week 2, дни 7–13) открываются как раньше; старые чаты получают ноль
задач. Существующие таблицы не изменяются.

**FR-12. Идемпотентность и optimistic locking.** Переход выполняется одной транзакцией:
preconditions FSM до записи + `UPDATE tasks ... WHERE id=? AND version=?` (при `rowcount=0` —
`TaskVersionConflictError`) + `INSERT task_events` + `INSERT task_artifacts` + один коммит.
`UNIQUE(task_id, kind, revision)`, `UNIQUE(task_id, idempotency_key)` (partial, где ключ не
NULL). `idempotency_key` несут только state-changing события в формате
`f"{action}:{task_id}:{version}"` (version до изменения); `API_ERROR`, `RETRY` и
`TASK_CREATED` — NULL (последний пишется в одной транзакции с созданием задачи, двойная
отправка защищена nonce формы).

**FR-13. Append-only и удаление.** Обновление события невозможно (триггер + API); для
`task_events`/`task_artifacts` запрещён `INSERT OR REPLACE` (upsert разрешён только для
`app_state.active_task`). `DELETE` допустим только каскадом при удалении чата; публичного
удаления события или задачи нет.

**FR-14. Выбранная задача.** `app_state` ключ `active_task:{chat_id}`: set/get, висячий id
игнорируется, при удалении чата очищается в обработчике `app.py`. `resolve_display_task`
возвращает выбранную задачу либо безопасный fallback.

**FR-15. Публичный API TaskRepository.** `create_task`, `get_task`/`list_tasks`,
`append_event`, `commit_transition`, `add_artifact`, `list_artifacts`/`list_events`,
`set_active_task_id`/`get_active_task_id`, `resolve_display_task`, `clear_selection`,
`get_task_usage`, `TaskVersionConflictError`.

### 4.3 Use cases стадий

**FR-16. Создание задачи.** `TASK_CREATED`: задачи нет → planning/active, `version=1`;
необязательный `task_brief`; `expected_action=run_planning`, `current_step=«Planning»`,
`current_step_index=NULL`.

**FR-17. План.** `PLAN_CREATED`: stage=planning, `expected_action=run_planning` → planning/active;
`specification` rev+1, `plan` rev+1 (валидный JSON). `current_step=title` шага 1,
`current_step_index=1`, `expected_action=confirm_plan`. Повторно допустим только после
`PLAN_REJECTED` (создаёт `plan` rev+1). Промпты и бюджеты стадий — константы в `task_prompts.py`;
строгий парсер плана отклоняет невалидный/пустой/обрезанный JSON.

**FR-18. Reject/Accept плана.** `PLAN_REJECTED`: planning, `expected_action=confirm_plan` →
planning/active; `expected_action=run_planning`; старый `plan` сохраняется. `PLAN_ACCEPTED`:
planning, `expected_action=confirm_plan` → execution/active; `expected_action=run_step`,
`current_step`/`current_step_index` = первый незавершённый шаг.

**FR-19. Шаг.** `STEP_COMPLETED`: execution/active, `expected_action=run_step`,
`current_step_index=i`, шаг i не завершён (ожидание переделки разрешено) → execution/active;
`execution_result` rev+1 (`step_index=i`, `round`); продвижение указателя по правилу раздела 8.

**FR-20. Завершение execution.** `EXECUTION_FINISHED`: execution/active,
`expected_action=finish_execution` и все шаги 1..N завершены → validation/active;
`current_step=«Validation»`, `current_step_index=NULL`, `expected_action=run_validation`.

**FR-21. Валидация.** `VALIDATION_PASSED`: validation/active, `expected_action=run_validation`,
вердикт `passed` → `validation_result` rev+1; done/completed; `current_step=«Done»`,
`current_step_index=NULL`, `expected_action=review_result`; `final_result` rev1.
`VALIDATION_FAILED`: validation/active, `expected_action=run_validation`, вердикт failed с
дефектами → `validation_result` rev+1; execution/active; `current_step_index=min{defects.step_index}`,
`current_step` = title этого шага, `expected_action=run_step`.

**FR-22. Правило завершённости и переделки шагов.** Определения:

- `latest_exec(i)` — артефакт `execution_result` шага i с максимальным `artifact.id`;
- `blocking_validation(i)` — артефакт `validation_result` с `passed=false` и максимальным `id`,
  в `defects` которого есть i.

Шаг i **завершён** ⇔ `latest_exec(i)` существует и (`blocking_validation(i)` отсутствует или
`blocking_validation(i).id < latest_exec(i).id`). Шаг i **ожидает переделки** ⇔
`blocking_validation(i)` существует и он новее `latest_exec(i)` (или `latest_exec(i)`
отсутствует). Новая успешная ревизия шага имеет больший id и автоматически снимает ожидание
переделки. `execution_result` пишется только при успехе; неуспешная попытка даёт только
`API_ERROR` и не меняет наборы. Продвижение после `STEP_COMPLETED`: минимальный индекс среди
«ожидающих переделки»; если таких нет — минимальный индекс среди незавершённых; если нет ни
тех, ни других — `expected_action=finish_execution`, `current_step=«Execution»`.

**FR-23. Pause/Resume.** `PAUSE`: planning|execution|validation, active → paused; stage и
`expected_action` сохраняются; `pause_reason` опционально. `RESUME`: paused → active; stage,
`current_step`, `current_step_index` и `expected_action` сохраняются; `pause_reason` очищается.
Состояние сохраняется в SQLite и переживает полный перезапуск приложения.

**FR-24. Block/Unblock.** `BLOCK`: planning|execution|validation, active|paused → blocked;
требуются `pause_reason` (причина) и `expected_action_text` (действие пользователя),
`expected_action_type=user_action`; stage сохраняется. Блокировка из paused после `UNBLOCK`
возвращает в active (paused-контекст не восстанавливается). `UNBLOCK`: blocked → active;
`pause_reason` очищается; `expected_action` пересчитывается по состоянию: planning без плана →
`run_planning`; planning с планом → `confirm_plan`; execution → `run_step` или
`finish_execution` (если все шаги завершены); validation → `run_validation`; done →
`review_result`.

**FR-25. Cancel.** `CANCEL`: planning|execution|validation, active|paused|blocked, только
`confirmed=true` → cancelled; stage сохраняется; `expected_action=none`. Журнал и артефакты
сохраняются.

**FR-26. Ошибки API и Retry.** Ошибка API до первого токена, обрыв mid-stream, context
overflow, обрезанный/невалидный JSON: `Task` не меняется, артефакта нет, пишется событие
`API_ERROR` с `kind ∈ provider_error | stream_error | invalid_response | truncated |
context_overflow`; усечённое сообщение ≤ 200 символов; сохраняются `finish_reason`, `usage`,
`attempts`. UI предлагает Retry/Pause/Cancel (после overflow Retry доступен). Невалидный,
пустой или обрезанный JSON плана/валидации: один повтор с увеличенным бюджетом, затем
`API_ERROR(invalid_response)`. Обрезанный execution-шаг (`finish_reason length/max_tokens`):
один повтор с увеличенным бюджетом, затем `API_ERROR(truncated)` без артефакта. `RETRY`
допустим только если последнее событие — `API_ERROR`; это только событие, затем повтор текущего
действия.

### 4.4 Context packet и провайдер

**FR-27. StageContextBuilder.** Порядок блоков: 1) базовый system prompt; 2) invariants;
3) активный пользовательский профиль; 4) инструкции workflow и текущей стадии; 5) task snapshot
(goal, stage, status, current step, expected action, version); 6) необходимые артефакты прошлых
стадий; 7) релевантные working/long-term memory items; 8) часть истории, выбранная текущей
context strategy; 9) текущее сообщение/действие.

Блоки 4 и 9 по действиям: `run_planning` — инструкция стадии planning + синтетическое сообщение
с goal и task_brief; `run_step` — инструкция execution + синтетическое сообщение с
title/description текущего шага и (при переделке) дефектами validation; `run_validation` —
инструкция validation + синтетическое сообщение с acceptance criteria.
`accept_plan`/`reject_plan`/`finish_execution`/`pause`/`resume`/`block`/`unblock`/`cancel` —
LLM не вызывается. Артефакты: planning → task_brief (если есть); execution → specification +
plan + дефекты последней validation (при переделке); validation → specification + plan +
последняя `execution_result` по каждому шагу + известные ограничения. Отправляются только
необходимые последние ревизии. Preview packet в `Diagnostics / Task` строится без единого
API-вызова, показывает блоки, ≈ токены и ≈ стоимость. Полная история автоматически не
отправляется.

**FR-28. Профиль и сохранение chat payload.** В task-пакеты активный профиль включается
(блок 3) — это осознанное расширение Дня 12, фиксируется в README; служебные вызовы сводки и
facts профиль по-прежнему не получают. Chat-payload (порядок profile → invariants) не меняется
байт-в-байт. `memory.format_invariants_block()` — единственный источник строки инвариантов;
равенство прежней строке проверяется тестом.

**FR-29. StageExecutor.** Planning и validation — всегда нестриминговые. Execution-шаг стримит
только при `config.stream=True` и переданном `on_chunk`; spinner показывается до первого чанка;
текст рендерится в отдельный placeholder в основной области `Chat` (не `chat_message`, не
`st.bottom`); при ошибке placeholder очищается; потоковый текст не пишется в `messages` и не
становится артефактом; в БД попадает только успешно завершённый результат.

**FR-30. ChatAgent.complete.** Аддитивный метод
`ChatAgent.complete(messages, *, max_tokens=None, temperature=None, stream=False,
on_chunk=None) -> (text, TurnStats)`: model из `self._config.model`; при `None`
`max_tokens`/`temperature` берутся из конфига; без `on_chunk` стриминг не выполняется; при
стриминге обязателен `stream_options={"include_usage": True}` и извлечение usage из финального
чанка (как в `ask()`); метод не трогает `_history`, не вызывает `save_turn`, сводку и facts,
ничего не пишет в store; исключения провайдера пробрасывает вызывающему.

**FR-31. Учёт task-вызовов.** Task-вызовы не пишут `messages`/`turns`, не меняют счётчики и
статистику чата, идут мимо `_try_compress` и `_try_update_facts`. Usage (`model`,
prompt/completion/cache hit/miss tokens, `finish_reason`, `cost_usd`, `attempts`) сохраняется в
`payload_json` события. Секреты и полные служебные промпты не пишутся. Суммарная статистика
стоимости задачи доступна в `Diagnostics / Task`. Бюджеты стадий — константы в
`task_prompts.py` (план, шаг, валидация + retry-бюджеты).

### 4.5 UI

**FR-32. Режим Diagnostics / Task.** Третий вариант radio `ui_mode`; порядок опций: `Chat`
(первый, по умолчанию), `Diagnostics / Memory`, `Diagnostics / Task`. Существующие панели
`Chat` и `Diagnostics / Memory` не меняются — в `Chat` добавляется только карточка задачи.

**FR-33. Карточка задачи в Chat.** Карточка — первая в `with st.bottom:`, не оборачивает строку
профиля; строка профиля и `st.chat_input` сохраняют порядок и вложенность; новых expander'ов в
`app.main` в `Chat` нет; ключи виджетов карточки с префиксом `task_`; при отрисовке API не
вызывается; карточка рендерится только при открытом чате.

Вертикальный индикатор стадий (planning, execution, validation, done столбиком, тонкая
вертикальная линия между ними):

- пройденные: opacity 0.45, font-size 0.92rem, text-decoration line-through, символ ✓,
  остаются видимыми;
- текущая: opacity 1, font-size 1.10rem, font-weight 700, активный маркер ●;
- будущие: opacity 0.45–0.55, font-size 0.90rem, text-decoration none, пустой маркер ○;
- pause/blocked не меняют подсвеченную стадию; рядом badge RUNNING/PAUSED/BLOCKED/COMPLETED/
  CANCELLED;
- под текущей стадией показываются `current_step` и `expected_action`; для blocked — причина и
  ожидаемое действие пользователя;
- состояние различается не только цветом: контраст + размер + начертание + зачёркивание +
  значок + текстовый badge;
- длинный текст сокращается, полный открывается отдельно (модальное окно);
- бюджет высоты карточки ≤ ~150 px при 100% zoom (4 строки стадий ≤0.95rem включая badge +
  строка действий + одна строка деталей).

**FR-34. Create task.** Кнопка в карточке открывает модальный `st.dialog` с полями Title
(обязательно), Goal (обязательно), Task brief (необязательно, многострочно), Workflow
(selectbox, в Дне 13 только Default workflow); ошибки валидации — в диалоге; успех →
`TASK_CREATED`, `active_task` = новая задача. В чате может быть несколько задач; New task
создаёт новую, старая остаётся в списке.

**FR-35. Действия в Chat.** В `Chat` показываются только действия из `can_apply`; нажатие
действия выполняет соответствующий use case и перерисовывает карточку.

**FR-36. Завершение задачи.** После завершения: компактный итог, badge COMPLETED, кнопка
Open full result (модальное окно с полным `final_result` в `st.code` — встроенное копирование —
и кнопкой перехода в `Diagnostics / Task`), кнопка New task.

**FR-37. Diagnostics / Task.** Task list текущего чата (title, stage, status, version, updated)
+ Open (записывает `active_task:{chat_id}`); индикатор и поля задачи; Event timeline; Artifacts
по kind/revision; Workflow (read-only); Context packet preview (Block / Role / Content preview /
≈ tokens / ≈ cost; стоимость входных токенов по cache-miss ставке текущего окна peak/off-peak,
подпись «estimate, not billing»); Task usage (суммарно по задаче). Пользовательские профили
остаются только в `Diagnostics / Memory`. Expander'ы в `Diagnostics / Task` разрешены.

**FR-38. Fallback карточки.** Объективный триггер fallback: при окне 1024×768 должно оставаться
видимым не менее 3 сообщений истории, иначе карточка переносится в сайдбар (компактная секция
над настройками чата). Обязательна ручная визуальная проверка на обычной и уменьшенной ширине.

**FR-39. README и видео.** `README.md` получает раздел с описанием состояния задачи,
расширения Дня 12 (профиль в task-пакетах), инструкций по UI и сценария демонстрационного
видео; видео записывает пользователь.

## 5. Нефункциональные требования

**NFR-01. Без новых зависимостей.** Только stdlib + `streamlit` + `openai` + `dotenv`.

**NFR-02. Слои.** Доменный слой без Streamlit и без SQL; UI не меняет `stage` напрямую;
SQLite-репозиторий не решает, какой переход допустим.

**NFR-03. Идемпотентная миграция без потери данных.**

**NFR-04. Тестируемость.** Unit FSM, интеграция с временной SQLite, FakeClient, AppTest; тесты
без сети и без настоящего `.env`.

**NFR-05. Соединение на операцию.** WAL, `busy_timeout`, `PRAGMA foreign_keys` на каждом
соединении.

**NFR-06. Без секретов.** Никаких секретов и полных служебных промптов в событиях и артефактах;
сообщения об ошибках усечены.

**NFR-07. Доступность.** Различение состояний не только цветом.

**NFR-08. Расширяемость.** Стадии, инструкции, исполнители, validation policy и будущие модели —
в `WorkflowProfile`.

**NFR-09. Регрессия.** Проходит весь существующий набор `test.bat` и `smoke_test.bat`;
фактическое число тестов фиксируется прогоном.

**NFR-10. Оценки стоимости.** Только ≈ по `pricing.py`.

**NFR-11. Бюджет UI.** Карточка ≤ ~150 px и не сокращает видимую историю ниже 3 сообщений на
1024×768.

## 6. Модель состояния

### 6.1 Task

| Поле | Тип / значения | Смысл |
|---|---|---|
| `id` | INTEGER PK | Идентификатор задачи |
| `chat_id` | FK `chats` ON DELETE CASCADE | Чат-владелец |
| `workflow_profile_id` | FK `workflow_profiles` без каскада | Профиль workflow |
| `title` | TEXT | Название задачи |
| `goal` | TEXT | Цель |
| `stage` | planning \| execution \| validation \| done | Стадия FSM |
| `status` | active \| paused \| blocked \| completed \| cancelled | Статус |
| `current_step` | TEXT | Человекочитаемая подпись текущего шага |
| `current_step_index` | INTEGER NULL, 1-based | Номер шага в плане |
| `expected_action_type` | см. FR-08 | Тип ожидаемого действия |
| `expected_action_text` | TEXT | Подпись ожидаемого действия |
| `pause_reason` | TEXT | Причина pause/block |
| `version` | INTEGER | Версия для optimistic locking |
| `created_at`, `updated_at` | TEXT | Время |

### 6.2 Сущности

- **TaskArtifact** (`task_artifacts`) — неизменяемый артефакт: `task_id`, `stage`, `kind`,
  `revision`, `content` (JSON), `created_at`.
- **TaskEvent** (`task_events`) — append-only событие: `task_id`, `event_type`, `from_stage`,
  `to_stage`, `from_status`, `to_status`, `payload_json`, `idempotency_key`, `created_at`.
- **WorkflowProfile** (`workflow_profiles`) — профиль workflow: `name` UNIQUE, `display_name`,
  JSON-поля стадий, инструкций, исполнителей, моделей и validation, `is_default` (частичный
  UNIQUE, где `is_default=1`).

## 7. Инварианты

**INV-01.** `stage=done ⇔ status=completed`; `done` и `cancelled` терминальны.

**INV-02.** `paused`/`blocked`/`cancelled` не меняют `stage`.

**INV-03.** Resume возвращает ту же задачу и ту же стадию, не создаёт новую задачу и не
повторяет завершённые шаги.

**INV-04.** Переход + запись события + запись артефактов выполняются одной транзакцией; при
сбое — полный откат.

**INV-05.** `version` увеличивается ровно на +1 при любом изменении полей `Task` (`PAUSE`,
`RESUME`, `BLOCK`, `UNBLOCK`, `CANCEL`, `PLAN_CREATED`, `PLAN_REJECTED`, `PLAN_ACCEPTED`,
`STEP_COMPLETED`, `EXECUTION_FINISHED`, `VALIDATION_PASSED`, `VALIDATION_FAILED`, изменение
`current_step`/`current_step_index`/`expected_action`); `API_ERROR` и `RETRY` version не меняют;
успешный Retry увеличивает version через своё действие.

**INV-06.** Дублей событий и артефактов нет: preconditions FSM до записи + optimistic locking по
`version` + `UNIQUE(task_id, kind, revision)` + `UNIQUE(task_id, idempotency_key)` + один коммит.

**INV-07.** Артефакт никогда не перезаписывается; повторный шаг даёт новую ревизию.

**INV-08.** При ошибке API артефакт не создаётся, `stage`/`current_step`/`current_step_index`/
`version` не меняются; записывается только событие `API_ERROR`.

**INV-09.** `stage` меняется только событиями переходов, не UI.

**INV-10.** Служебные блоки (system prompt, invariants, profile, workflow-инструкции, task
snapshot) не становятся элементами working/long-term памяти и facts.

**INV-11.** Завершённость шагов определяется только по артефактам (правило FR-22).

## 8. Правило завершённости и переделки шагов

См. FR-22. Кратко: завершённость шага определяется парой «последний успешный
`execution_result`» ↔ «последняя блокирующая `validation_result`». Побеждает артефакт с
большим `id`. Новая успешная ревизия шага автоматически снимает ожидание переделки;
`execution_result` пишется только при успехе, поэтому неуспешная попытка не меняет наборы шагов.

## 9. Таблица переходов

Столбцы: событие → условие (precondition) → stage → status → version → запись в журнал →
вердикт.

| Событие | Условие | stage → stage | status → status | version | Журнал | Вердикт |
|---|---|---|---|---|---|---|
| `TASK_CREATED` | Задачи нет | new → planning | new → active | 1 (новая) | `TASK_CREATED` (idempotency_key NULL) | Допустимо |
| `PLAN_CREATED` | planning + expected_action=run_planning | planning → planning | active → active | +1 | `PLAN_CREATED` | Допустимо |
| `PLAN_CREATED` | Есть план / expected_action=confirm_plan | — | — | — | — | Отклоняется |
| `PLAN_REJECTED` | planning + expected_action=confirm_plan | planning → planning | active → active | +1 | `PLAN_REJECTED` | Допустимо |
| `PLAN_ACCEPTED` | planning + expected_action=confirm_plan | planning → execution | active → active | +1 | `PLAN_ACCEPTED` | Допустимо |
| `STEP_COMPLETED` | execution + expected_action=run_step + `current_step_index=i` + шаг i не завершён (ожидание переделки разрешено) | execution → execution | active → active | +1 | `STEP_COMPLETED` | Допустимо |
| `STEP_COMPLETED` | Нет `run_step`, нет `current_step_index`, шаг завершён и не дефектный, или сверх плана | — | — | — | — | Отклоняется |
| `EXECUTION_FINISHED` | execution + expected_action=finish_execution + все шаги завершены | execution → validation | active → active | +1 | `EXECUTION_FINISHED` | Допустимо |
| `EXECUTION_FINISHED` | Нет `finish_execution` или есть незавершённые шаги | — | — | — | — | Отклоняется |
| `VALIDATION_PASSED` | validation + expected_action=run_validation + вердикт passed | validation → done | active → completed | +1 | `VALIDATION_PASSED` | Допустимо |
| `VALIDATION_FAILED` | validation + expected_action=run_validation + вердикт failed с дефектами | validation → execution | active → active | +1 | `VALIDATION_FAILED` | Допустимо |
| `VALIDATION_*` | Нет `run_validation` или невалидный вердикт | — | — | — | — | Отклоняется |
| `PAUSE` | planning\|execution\|validation + active | stage → stage | active → paused | +1 | `PAUSE` | Допустимо |
| `PAUSE` | Из paused/blocked | — | — | — | — | Отклоняется |
| `RESUME` | paused | stage → stage | paused → active | +1 | `RESUME` | Допустимо |
| `RESUME` | Не из paused | — | — | — | — | Отклоняется |
| `BLOCK` | planning\|execution\|validation + active\|paused + есть причина и ожидаемое действие | stage → stage | * → blocked | +1 | `BLOCK` | Допустимо |
| `BLOCK` | Без причины или без ожидаемого действия | — | — | — | — | Отклоняется |
| `UNBLOCK` | blocked | stage → stage | blocked → active | +1 | `UNBLOCK` | Допустимо |
| `CANCEL` | planning\|execution\|validation + active\|paused\|blocked + `confirmed=true` | stage → stage | * → cancelled | +1 | `CANCEL` | Допустимо |
| `CANCEL` | Без `confirmed` | — | — | — | — | Отклоняется |
| `API_ERROR` | Есть активная задача | stage → stage | status → status | без изменений | `API_ERROR` (idempotency_key NULL) | Допустимо |
| `RETRY` | Последнее событие — `API_ERROR` | stage → stage | status → status | без изменений | `RETRY` (idempotency_key NULL) | Допустимо |
| `RETRY` | Без предшествующего `API_ERROR` | — | — | — | — | Отклоняется |
| Любое | Из done/cancelled | — | — | — | — | Отклоняется |
| Любое | Несовпадение `version` | — | — | — | — | `TaskVersionConflictError` |

Ошибка отклонённого перехода — `InvalidTransitionError`; при отклонении не пишется ничего.

## 10. SQLite и миграция

### 10.1 Изменения существующего кода

Изменения существующих таблиц отсутствуют. Единственная правка существующего кода —
аддитивное публичное свойство `ChatStore.db_path`.

### 10.2 Новые объекты

```text
workflow_profiles (id, name UNIQUE, display_name, stages_json, instructions_json,
  executors_json, models_json, validation_json, is_default + частичный UNIQUE(is_default)
  WHERE is_default=1, created_at, updated_at)

tasks (id, chat_id FK chats ON DELETE CASCADE, workflow_profile_id FK workflow_profiles
  без каскада, title, goal, stage TEXT, status TEXT CHECK IN
  ('active','paused','blocked','completed','cancelled'), current_step TEXT NOT NULL DEFAULT '',
  current_step_index INTEGER, expected_action_type TEXT NOT NULL DEFAULT 'none',
  expected_action_text TEXT NOT NULL DEFAULT '', pause_reason TEXT NOT NULL DEFAULT '',
  version INTEGER NOT NULL DEFAULT 1, created_at, updated_at;
  индексы (chat_id, id) и частичный (chat_id, status)
  WHERE status IN ('active','paused','blocked'))

task_artifacts (id, task_id FK tasks ON DELETE CASCADE, stage, kind, revision, content,
  created_at; UNIQUE(task_id, kind, revision))

task_events (id, task_id FK tasks ON DELETE CASCADE, event_type, from_stage, to_stage,
  from_status, to_status, payload_json, idempotency_key, created_at;
  частичный UNIQUE(task_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
  триггер BEFORE UPDATE RAISE(ABORT))
```

### 10.3 Правила миграции

- `ChatStore()` → `TaskRepository(store.db_path)`.
- Все объекты создаются через `CREATE TABLE/INDEX/TRIGGER IF NOT EXISTS`.
- `PRAGMA foreign_keys = ON`, WAL, `busy_timeout` — на каждом соединении.
- Старые БД (Week 2, дни 7–13) открываются как раньше; старые чаты получают ноль задач.
- Удаление чата каскадно удаляет задачи, артефакты и события; long-term memory не затрагивается.
- Задача не связана с веткой.
- Откат — удаление нового кода и drop task-таблиц.

## 11. Ошибки и восстановление

- Ошибка API до первого токена, обрыв mid-stream, context overflow, обрезанный/невалидный JSON:
  `Task` не меняется, артефакта нет, пишется `API_ERROR`, UI предлагает Retry/Pause/Cancel
  (после overflow Retry доступен).
- Невалидный/пустой/обрезанный JSON плана или валидации: один повтор с увеличенным бюджетом,
  затем `API_ERROR(invalid_response)`.
- Обрезанный execution-шаг (`finish_reason length/max_tokens`): один повтор с увеличенным
  бюджетом, затем `API_ERROR(truncated)` без артефакта.
- Пауза действует между действиями; жёсткое закрытие приложения во время вызова не создаёт
  событий и артефактов; потерянный вызов повторяется через Retry.

## 12. UI / визуальный контракт

Полный контракт — FR-32…FR-38. Ключевые правила:

- `Chat` (первый и по умолчанию), `Diagnostics / Memory`, `Diagnostics / Task` — в этом порядке.
- В `Chat` добавляется только карточка задачи: первая в `st.bottom`, без новых expander'ов,
  ключи с префиксом `task_`, без API-вызовов при отрисовке.
- Вертикальный индикатор стадий с пройденными/текущей/будущими состояниями и badge.
- Состояния различаются не только цветом; бюджет высоты ≤ ~150 px.
- Fallback в сайдбар, если на 1024×768 видно меньше 3 сообщений истории.
- Create task — модальный диалог с валидацией Title/Goal.
- После завершения — компактный итог, `Open full result` в `st.code`, `New task`.
- `Diagnostics / Task` — list, timeline, artifacts, read-only workflow, packet preview
  («estimate, not billing»), task usage.

## 13. Критерии приёмки (Given/When/Then)

**AC-01 (FR-16, FR-01, FR-05).** Given пустая БД; When создаётся задача; Then задача имеет
stage=planning, status=active, version=1, expected_action_type=run_planning, current_step=«Planning»,
current_step_index=NULL, и в журнале ровно одно событие `TASK_CREATED`.

**AC-02 (FR-17).** Given planning/active + run_planning; When завершается run_planning с валидным
JSON; Then появляются `specification` rev1 и `plan` rev1, current_step=title шага 1,
current_step_index=1, expected_action=confirm_plan.

**AC-03 (FR-18).** Given plan создан; When PLAN_REJECTED, затем повторный PLAN_CREATED; Then
expected_action=run_planning после reject, старый plan сохранён, повторный план — plan rev2.

**AC-04 (FR-18).** Given planning + confirm_plan; When PLAN_ACCEPTED; Then stage=execution,
status=active, expected_action=run_step, current_step/current_step_index = первый незавершённый
шаг.

**AC-05 (FR-19, FR-02).** Given execution + run_step + current_step_index=i; When шаг i успешно
завершён; Then `execution_result` rev+1 содержит step_index=i и round=1, а Task не изменил stage.

**AC-06 (FR-19, FR-22).** Given есть ожидающие переделки шаги; When STEP_COMPLETED; Then
следующий current_step_index = min{ожидающие переделки}; если таких нет — min{незавершённые};
если нет ни тех, ни других — expected_action=finish_execution, current_step=«Execution».

**AC-07 (FR-20).** Given все шаги завершены и expected_action=finish_execution; When
EXECUTION_FINISHED; Then stage=validation, current_step=«Validation», current_step_index=NULL,
expected_action=run_validation. При незавершённых шагах переход отклоняется.

**AC-08 (FR-21).** Given validation/active + run_validation; When вердикт passed; Then
`validation_result` rev+1 с passed=true и пустыми defects, stage=done, status=completed,
expected_action=review_result, существует `final_result` rev1.

**AC-09 (FR-21).** Given validation/active + run_validation; When вердикт failed с дефектами;
Then `validation_result` rev+1, stage=execution, current_step_index=min{defects.step_index},
expected_action=run_step; переделываются только дефектные шаги (SC-02).

**AC-10 (FR-22, INV-11).** Given шаг i имеет `execution_result`, а позже — `validation_result` с
passed=false и i в defects; When проверяется завершённость; Then шаг i ожидает переделки; после
новой успешной ревизии с большим id шаг снова завершён.

**AC-11 (FR-06).** Given любое состояние из таблицы переходов; When отправлен недопустимый
переход (в том числе из done/cancelled); Then поднимается `InvalidTransitionError` и в БД не
появляется ни события, ни артефакта.

**AC-12 (FR-08).** Given создание, план, приём плана, завершение шага, конец execution,
валидация passed/failed, block и cancel; When вычисляются expected_action_text и badge; Then
значения совпадают с FR-08.

**AC-13 (FR-09).** Given каждое состояние из таблицы `can_apply`; When вызывается
`can_apply(task, last_event_type)`; Then возвращается ровно ожидаемый набор действий, а при
последнем `API_ERROR` дополнительно доступен `retry`.

**AC-14 (FR-23).** Given задача в planning/execution/validation; When PAUSE, полное закрытие и
повторный запуск приложения, затем RESUME; Then это та же задача с той же стадией,
current_step, current_step_index и expected_action; pause_reason очищен (INV-02, INV-03).

**AC-15 (FR-23, INV-03).** Given после restart выполнены шаги 1..i; When RESUME и продолжение;
Then новый шаг — i+1, повторно выполненные шаги не переделываются, новая задача не создаётся.

**AC-16 (FR-24).** Given planning/execution/validation; When BLOCK с причиной и ожидаемым
действием, затем UNBLOCK; Then после BLOCK status=blocked, stage не изменён, expected_action_type=
user_action; после UNBLOCK status=active, pause_reason пуст, а expected_action пересчитан по
состоянию.

**AC-17 (FR-25).** Given активная или paused/blocked задача; When CANCEL с confirmed=true; Then
status=cancelled, stage сохранён, expected_action=none, журнал и артефакты на месте; без
confirmed переход отклоняется.

**AC-18 (FR-12, INV-04).** Given переход, падающий на этапе записи артефакта; When транзакция
завершается ошибкой; Then ни Task, ни событие, ни артефакт не изменены (полный откат).

**AC-19 (FR-12, INV-05).** Given задача с version=v; When выполняется state-changing событие;
Then version=v+1; When выполняется API_ERROR или RETRY; Then version не изменяется.

**AC-20 (FR-12, INV-06).** Given успешный переход; When повторно отправляется тот же
idempotency_key или переход со старой version; Then дубль не создаётся, повторный коммит с той
же version даёт `TaskVersionConflictError`.

**AC-21 (FR-02, FR-03, FR-13, INV-07).** Given существующий артефакт или событие; When
выполняется UPDATE или `INSERT OR REPLACE`; Then операция отклоняется (API и триггер), а
повторный шаг создаёт новую ревизию, не перезаписывая старую.

**AC-22 (FR-11, NFR-03).** Given старая БД (дни 7–13) с чатами и данными; When она открывается
дважды; Then она открывается без ошибок, новые таблицы создаются один раз, данные не теряются,
старые чаты получают ноль задач.

**AC-23 (FR-14, FR-11).** Given чат с активной задачей; When чат удаляется; Then задачи,
артефакты и события удаляются каскадом, запись `active_task:{chat_id}` очищается, long-term
memory не затрагивается.

**AC-24 (FR-26, INV-08).** Given active execution-шаг; When провайдер падает до первого токена;
Then Task не изменён (stage/current_step/current_step_index/version), артефакта нет, записано
только `API_ERROR`, UI предлагает Retry/Pause/Cancel.

**AC-25 (FR-26).** Given streaming execution-шаг; When поток обрывается mid-stream; Then
placeholder очищен, частичный текст не стал артефактом и не попал в messages, записано
`API_ERROR(stream_error)`.

**AC-26 (FR-17, FR-21, FR-26).** Given run_planning или run_validation; When ответ — пустой,
невалидный или обрезанный JSON; Then делается один повтор с увеличенным бюджетом, и если он
тоже невалиден, пишется `API_ERROR(invalid_response)` без артефакта.

**AC-27 (FR-26).** Given execution-шаг завершился с finish_reason length/max_tokens; When
делается один повтор с увеличенным бюджетом; Then при повторе артефакт создаётся, а при
повторной обрезке пишется `API_ERROR(truncated)` без артефакта.

**AC-28 (FR-26).** Given последнее событие — `API_ERROR`; When RETRY, затем успешное
выполнение; Then успешный шаг создаёт `execution_result`, version увеличивается через своё
действие; RETRY без предшествующего `API_ERROR` отклоняется.

**AC-29 (FR-26).** Given context overflow от провайдера; When ошибка обработана; Then записано
`API_ERROR(context_overflow)`, статус задачи не изменён, Retry доступен.

**AC-30 (FR-27).** Given любое task-действие; When строится packet; Then блоки идут в порядке
1–9 из FR-27, а для accept_plan/reject_plan/finish_execution/pause/resume/block/unblock/cancel
LLM не вызывается.

**AC-31 (FR-27).** Given стадии planning/execution/validation; When выбираются артефакты; Then
planning получает task_brief, execution — specification + plan + дефекты последней validation,
validation — specification + plan + последнюю execution_result по каждому шагу + ограничения,
и только последние ревизии.

**AC-32 (FR-27).** Given `Diagnostics / Task`; When строится preview packet; Then ни одного
API-вызова не происходит, показаны блоки, ≈ токены и ≈ стоимость, а подпись сообщает, что это
оценка, а не счёт.

**AC-33 (FR-29).** Given planning и validation; When стадия запускается; Then вызов
нестриминговый. Given execution + stream=True + on_chunk; When шаг запускается; Then spinner
виден до первого чанка, текст рендерится в отдельный placeholder (не chat_message, не st.bottom),
при ошибке очищается, потоковый текст не сохраняется.

**AC-34 (FR-30).** Given FakeClient; When вызывается `complete(messages, ...)`; Then возвращается
`(text, TurnStats)`, model берётся из конфига, при `None` max_tokens/temperature — из конфига,
без on_chunk стриминга нет, при стриминге usage извлекается из финального чанка, `_history` и
store не изменяются, save_turn/сводка/facts не вызываются, исключения провайдера пробрасываются.

**AC-35 (FR-28).** Given одинаковые настройки чата; When сравниваются payload обычного чата до и
после изменения; Then он совпадает байт-в-байт (порядок profile → invariants), а
`format_invariants_block()` возвращает прежнюю строку инвариантов.

**AC-36 (FR-31, INV-10).** Given task-вызов; When он завершён; Then в `messages`/`turns`
ничего не добавилось, статистика чата не изменилась, сводка и facts не вызывались, usage сохранён
в `payload_json`, а секреты и полные служебные промпты в событиях отсутствуют.

**AC-37 (FR-32).** Given приложение запущено; When смотрится radio `ui_mode`; Then порядок опций —
`Chat`, `Diagnostics / Memory`, `Diagnostics / Task`, по умолчанию выбран `Chat`, панели
`Diagnostics / Memory` и `Chat` не изменились.

**AC-38 (FR-33).** Given открытый чат с задачей; When отрисовывается `Chat`; Then карточка — первая
в `st.bottom`, не оборачивает строку профиля, строка профиля и `st.chat_input` сохраняют порядок,
новых expander'ов в `app.main` в `Chat` нет, ключи виджетов начинаются с `task_`, и ни одного
API-вызова при отрисовке не сделано.

**AC-39 (FR-33, NFR-07).** Given задача в произвольном состоянии; When строится вертикальный
индикатор; Then пройденные/текущая/будущие стадии имеют прописанные стиль и маркеры, рядом
badge, состояния различаются не только цветом (размер, начертание, зачёркивание, значок,
текстовый badge).

**AC-40 (FR-38, NFR-11).** Given окно 1024×768; When открыт `Chat` с задачей; Then высота
карточки ≤ ~150 px, видно не менее 3 сообщений истории, иначе карточка уходит в сайдбар;
выполнена ручная проверка на обычной и уменьшенной ширине.

**AC-41 (FR-34).** Given карточка задачи; When открыт Create task и отправлена пустая форма;
Then ошибки Title/Goal показаны в диалоге; When форма заполнена; Then создаётся задача
(`TASK_CREATED`), `active_task` указывает на неё, а прежние задачи остаются в списке.

**AC-42 (FR-35, FR-09).** Given задача в состоянии X; When отрисован `Chat`; Then показаны
только действия из `can_apply`; нажатие действия выполняет use case и не меняет stage напрямую
(NFR-02).

**AC-43 (FR-36).** Given задача завершена; When открыт `Chat`; Then показан компактный итог,
badge COMPLETED, кнопка Open full result (полный `final_result` в `st.code`) и кнопка New task.

**AC-44 (FR-37).** Given `Diagnostics / Task`; When открыт чат с задачами; Then видны Task list
(title, stage, status, version, updated) с Open (записывает `active_task:{chat_id}`), индикатор и
поля задачи, Event timeline, Artifacts по kind/revision, read-only Workflow, packet preview с
«estimate, not billing» и Task usage; профили остаются только в `Diagnostics / Memory`.

**AC-45 (SC-06).** Given чат без задач в `Chat`; When открыт чат; Then показано компактное
приглашение создать задачу, а поведение и панели Дней 10–12 не изменены.

**AC-46 (NFR-09).** Given полный набор проекта; When запускаются `test.bat` и `smoke_test.bat`;
Then все тесты проходят, а существующие тесты не ослаблены; фактическое число тестов фиксируется
прогоном.

**AC-47 (FR-39).** Given завершённый День 13; When проверяется `README.md`; Then в нём есть раздел
с описанием состояния задачи, расширения Дня 12 (профиль в task-пакетах), UI и сценарий
демонстрационного видео.

**AC-48 (LIVE).** Given отдельное разрешение пользователя; When выполняется live-сценарий
create → run planning → accept → шаг 1 → Pause → полный перезапуск → Resume → шаг 2 → finish →
run validation → done; Then реальный план и вердикт парсятся, usage/finish_reason/≈ стоимость
видны в `Diagnostics / Task`, в messages/turns и статистике чата task-следов нет, состояние и
артефакты переживают перезапуск. До прохода — `LIVE_API_STATUS: READY_FOR_MANUAL_ACCEPTANCE`.

## 14. Принятые решения и отклонённые альтернативы

| Решение | Причина | Отклонённая альтернатива |
|---|---|---|
| FSM-события как единственный источник переходов | INV-09, тестируемость, предсказуемость | Прямое изменение `stage` из UI |
| Optimistic locking по `version` | INV-05, INV-06 без блокировок БД | Пессимистичная блокировка строки |
| Append-only журнал с триггером | Аудит и восстановление SC-03/SC-05 | Перезапись события при повторе |
| Неизменяемые ревизии артефактов | Переделка шагов без потери истории | UPDATE артефакта на месте |
| Завершённость шага по паре артефактов | Единственное правило переделки, INV-11 | Отдельный флаг «done» на шаге |
| `idempotency_key` только для state-changing событий | Двойная отправка формы и ретраи не создают дублей | Ключ для всех событий, включая API_ERROR |
| `active_task:{chat_id}` в `app_state` | Выбранная задача переживает перезапуск без новой таблицы | Колонка на `chats` |
| Per-stage context packet вместо полной истории | Контроль стоимости и предсказуемость промпта | Автоматическая отправка всей истории |
| Профиль включён в task-пакеты | Осознанное расширение Дня 12, зафиксировано в README | Полное исключение профиля из task-вызовов |
| Вертикальный индикатор + текстовый badge | NFR-07: состояние видно не только цветом | Только цветовая подсветка |
| Fallback карточки в сайдбар | NFR-11: не сокращать историю | Фиксированная карточка без fallback |
| Классифицированный `API_ERROR` без изменения Task | INV-08, честный Retry | Автоматическое сохранение частичного результата |
| Единый `can_apply` для домена и UI | Один источник правды об ожидаемом действии | Отдельные правила UI и домена |

## 15. Открытые вопросы

1. **Раскладка task-действий.** Точный порядок кнопок действий внутри карточки не зафиксирован
   и уточняется при визуальной приёмке (AC-40).
2. **Порог fallback.** Формулировка «не менее 3 сообщений истории на 1024×768» — объективный
   минимум; окончательная геометрия проверяется вручную.
3. **Будущие задачи в сайдбаре.** Полный список задач чата доступен в `Diagnostics / Task`;
   показывать ли его в сайдбаре, вопрос будущих дней.
4. **Стоимость preview.** Preview использует cache-miss ставку текущего окна peak/off-peak и
   подпись «estimate, not billing»; фактический счёт может отличаться.
