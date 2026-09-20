# ACCEPTANCE — Day 15 Controlled State Transitions

Чек-лист приёмки усиления FSM Day 13/14 в `week-03/memory-state-agent`.
Документ самодостаточен: идентификаторы `AC`, требования `FR`/`NFR`/`INV` и
уровни проверки определены здесь и в `SPEC.md`.

`SPEC_GATE_STATUS: PASS`: реализация велась по прямому указанию пользователя
(«После плана не останавливайтесь»).

## 1. Уровни проверки

| Код | Уровень | Что означает |
|---|---|---|
| **U** | Unit | Чистый домен и форматтеры без I/O |
| **I** | Integration temp SQLite | Реальное хранилище в `tempfile`, без сети |
| **F** | FakeClient | Сценарии оркестратора с поддельным клиентом |
| **UI** | AppTest | Streamlit-приложение через `streamlit.testing.v1.AppTest` |
| **R** | Ручная проверка | Реальный запуск приложения, включая полный перезапуск |

Правила: `TEST_STATUS: PASS` возможен только когда все обязательные критерии
проверены на достаточном уровне. Отсутствие live-вызова не требуется: Day 15 не
зависит от фактического ответа провайдера. Если для обязательного критерия нужен
UI или перезапуск, но нет возможности, Tester возвращает `TEST_STATUS: BLOCKED`
по этому критерию, а не полный PASS.

## 2. Чек-лист AC

| AC | Уровень | Проверяемое поведение | Тест / проверка |
|---|---|---|---|
| AC-01 | U | `ACTION_LABELS` покрывает `DOMAIN_ACTIONS`+`UI_ACTIONS`, единственный источник | `tests/test_tasks.py` |
| AC-02 | U | `explain_transition` не расходится с `can_apply`; формат отказа | `tests/test_tasks.py` |
| AC-03 | U | Специфичные причины для terminal/paused/blocked/mismatch/progress/retry | `tests/test_tasks.py` |
| AC-04 | I | DDL/reopen, round-trip, limit, триггер, изоляция, каскад | `tests/test_task_storage.py` |
| AC-05 | F, I | Отказ = NOOP без провайдера и без изменения состояния; hard-конфликт прежний | `tests/test_task_orchestrator.py` |
| AC-06 | F, I | `transition_guard_report` read-only | `tests/test_task_orchestrator.py` |
| AC-07 | U | Форматтеры панели и CSS-селектор | `tests/test_task_ui.py` |
| AC-08 | UI | Панель `Transition guard` read-only; запись только по явному probe-клику | `tests/test_task_ui.py` |
| AC-09 | UI | Подсветка рекомендованного действия, запрещённые не отрисованы | `tests/test_task_ui.py` |
| AC-10 | UI | Активный чат без `▶` и с точным CSS | `tests/test_app_ui.py` |
| AC-11 | Набор | `test.bat` и `smoke_test.bat` проходят, число тестов зафиксировано | `.\week-03\memory-state-agent\test.bat`, `.\week-03\memory-state-agent\smoke_test.bat` |
| AC-12 | R | Документация Day 15 самодостаточна и синхронна runtime-guard (обе поверхности, ретро-рамка, loopback) | Чтение `docs/specs/day-15-transition-control/` |
| AC-13 | U, F, I, UI | Граница «факты storage ↔ текст LLM»: модель не именует вымышленные ID/события, факты прикрепляет код, валидация снимком, retry → `invalid_response` | `tests/test_task_prompts.py`, `tests/test_task_orchestrator.py`, `tests/test_task_context.py`, `tests/test_task_ui.py` |
| AC-14 | U, F, I, UI | Достоверность execution-шага: недоказанная попытка (`unconfirmed_attempt`) и вымышленный `audit` id отклоняются; read-only Guard только условно; probe пишет только append-only аудит с реальным reason+id | `tests/test_task_prompts.py`, `tests/test_task_orchestrator.py`, `tests/test_task_ui.py` |

`Набор` у AC-11 означает обязательный агрегирующий прогон.

## 3. Обязательный сквозной сценарий

Сценарий выполняется на уровнях F/UI (автоматически) и R (вручную).

**Внешние переходы vs execution-план.** Сценарий разделяет два разных уровня:

- **Внешние переходы** выполняются вне плана: до `execution` идут
  `create_task` → `run_planning` → `Review plan`/`accept_plan`, после
  `execution` — `finish_execution` → `run_validation`. Это переходы FSM, а не
  шаги плана.
- **Execution-план** содержит только действия и проверки самой задачи,
  выполнимые в стадии `execution`; шагов про сам workflow (planning,
  формирование/принятие плана, запуск/завершение execution, validation) в нём
  быть не должно — иначе после `Accept plan` они невыполнимы и дают ложное
  «не подтверждено». Guard проверяет **обе поверхности** — `steps` и
  `acceptance_criteria`: критерии оцениваются в `validation` после
  `EXECUTION_FINISHED`, поэтому не могут требовать будущих `validation`/`done`/
  `VALIDATION_*`, а прошлые стадии называются только как уже состоявшиеся факты
  по Event timeline/артефактам. Задача не может одновременно находиться в
  `planning` и `execution`; возврат в `planning` в плане допустим только как
  проверка запрета через `Transition guard`/аудит отказов, а не как реальный
  переход.
- **Runtime-guard.** Такой план отклоняется **до** `PLAN_CREATED`:
  `task_prompts.parse_plan_response` бросает `ExecutionIncompatiblePlanError`
  (наследник `ValueError`), оркестратор записывает
  `API_ERROR(invalid_response)` без артефакта `plan`, состояние и версия задачи
  не меняются, а `task_stage` повторяет вызов один раз с корректирующим
  `plan_retry_feedback` (секции «Недопустимые шаги:»/«Недопустимые критерии
  приёмки:»). Проверки прошлых стадий допустимы только
  ретроспективно (Event timeline/артефакты) или как внешние переходы
  (`finish_execution`, `run_validation`) вне плана.

Канонический демо-сценарий (цель, внешние переходы, чистый execution-план,
defect-fixtures «как НЕ надо» — `REPORTED_PLAN_REV1`, `REPORTED_LIVE_PLAN` и
`REPORTED_LOOPBACK_PLAN` (шаги 3–5 и оба критерия) — и runtime-guard) описан в
[`DEMO_PLAN.md`](DEMO_PLAN.md) и закреплён константами `task_demo.py`
(`DEMO_EXECUTION_PLAN`, `REPORTED_PLAN_REV1`, `REPORTED_LIVE_PLAN`,
`REPORTED_LOOPBACK_PLAN`, `CORRECTED_PLAN_EXAMPLE`), регрессионными тестами
`tests/test_task_demo.py`, `tests/test_tasks.py`, `tests/test_task_orchestrator.py`
и правилом в `TASK_PLANNING_SYSTEM_PROMPT` (включая напоминание в action message
`build_plan_messages`).

Дальше проверяются шаги сквозного сценария:

1. **Недопустимое действие** — на стадии planning вызывается `run_step`; ответ
   `noop`, провайдер не вызван, состояние не изменено, отказ записан (AC-05).
2. **Причина и allowed** — сообщение содержит `expected_action_mismatch` и
   список действий, разрешённых сейчас (AC-02, AC-03).
3. **Аудит** — отказ виден в `Diagnostics / Task → Transition guard`, но его нет
   в `task_events` и он не меняет `version` (AC-04, AC-08).
4. **Терминальное состояние** — после done/cancel любое доменное действие даёт
   `noop` с причиной `task_is_terminal`, провайдер не вызван (AC-03, AC-05).
5. **Restart-safe Resume** — pause, полный перезапуск приложения, resume: та же
   задача и шаг, `version` +1, без дублей (AC-05).
6. **Сайдбар** — активный чат без `▶`, с мягкой зелёной подсветкой (AC-10).

**Факты storage ↔ текст LLM (AC-13).** Результат шага не владеет журналом:
фактические `stage`/`status`/`current_step`, id событий и аудит отказов
прикрепляет код блоком `task_facts`; текст, который выдумывает `EVT-...`,
ссылается на несуществующий номер события, заявляет чужую стадию/статус или
отрицает записанное событие, отклоняется (`StepFactsViolationError`),
повторяется один раз с `step_retry_feedback` и при повторе даёт
`API_ERROR(invalid_response)` без артефакта и без изменения
stage/current_step/version. Чистый текст с реальными фактами не блокируется; UI
показывает code-rendered строку журнала (`id/type/created_at/from→to`) под
текстом шага.

**Достоверность попытки (AC-14).** Текст, заявляющий совершённую/отклонённую/
записанную в аудит попытку перехода при пустом `task_facts.refusals`, отклоняется
(`unconfirmed_attempt`) и проходит тот же retry → `invalid_response`; названный
`action`/`reason` из непустого снимка подтверждает факт, а условная форма
read-only Guard («…сейчас был бы отклонён…») при пустом аудите допустима.
Вымышленная ссылка `audit #N` даёт `fabricated_reference`. Кнопка
`Test Finish execution` вызывает production-guard: при отказе появляется ровно
одна append-only строка `task_transition_attempts`, `reason` и `audit id`
берутся из storage, а `stage/status/version/current_step`/`task_events`/
артефакты не меняются; провайдер не вызывается.

Шаг 5 считается пройденным только при фактическом перезапуске приложения.

## 4. Критерии визуальной приёмки

- **Карточка:** рекомендованное действие подсвечено зелёным `#2e7d32`; набор
  кнопок равен `allowed_actions` и не расширяется (AC-09).
- **Панель `Transition guard`:** текущее состояние, allowed, таблица решений,
  безопасная кнопка `Test Finish execution`, таблица последних отказов, caption об
  отдельном append-only аудите, там же реальный `reason`/`audit id` probe; без
  других кнопок, форм, `st.json` и вложенных expander. Явный клик — единственная
  запись панели, и только в append-only аудит (AC-08, AC-14).
- **Сайдбар:** активный чат отличается фоном `rgba(46,125,50,0.18)` и рамкой
  `rgba(46,125,50,0.75)`, а не символом `▶` (AC-10).
- **Точность селекторов:** используется `class~=`, поэтому `chat_4` не задевает
  `chat_42`, а `run_step` не задевает более длинный ключ (AC-07, AC-10).

## 5. Таблица трассировки AC → FR/NFR/INV → тест

| AC | FR / NFR / INV | Тест / проверка |
|---|---|---|
| AC-01 | FR-01 | `tests/test_tasks.py` |
| AC-02 | FR-02, FR-03, FR-04, FR-05, INV-01 | `tests/test_tasks.py` |
| AC-03 | FR-04 | `tests/test_tasks.py` |
| AC-04 | FR-06, FR-07, FR-08, INV-03, INV-07, NFR-03, NFR-05 | `tests/test_task_storage.py` |
| AC-05 | FR-09, FR-10, FR-11, FR-12, INV-02, INV-04 | `tests/test_task_orchestrator.py` |
| AC-06 | FR-13, NFR-08 | `tests/test_task_orchestrator.py` |
| AC-07 | FR-14, INV-06 | `tests/test_task_ui.py` |
| AC-08 | FR-15, INV-05 | `tests/test_task_ui.py` |
| AC-09 | FR-16 | `tests/test_task_ui.py` |
| AC-10 | FR-17, INV-06 | `tests/test_app_ui.py` |
| AC-11 | NFR-07 | `.\week-03\memory-state-agent\test.bat`, `.\week-03\memory-state-agent\smoke_test.bat` |
| AC-12 | FR-18 | Чтение `docs/specs/day-15-transition-control/` |
| AC-13 | FR-19, FR-20, INV-08 | `tests/test_task_prompts.py`, `tests/test_task_orchestrator.py`, `tests/test_task_context.py`, `tests/test_task_ui.py` |
| AC-14 | FR-21, FR-22, FR-23, INV-05, INV-08 | `tests/test_task_prompts.py`, `tests/test_task_orchestrator.py`, `tests/test_task_ui.py` |

## 6. Протокол тестирования и приёмки

1. Developer реализует этапы 1–5 плана, обновляет тесты этапа 6, запускает
   точечные тесты, затем полный `test.bat` и `smoke_test.bat` (AC-11).
2. Tester независимо проверяет критерии: лично запускает команды, проектирует
   собственные сценарии, не изменяет реализацию и постоянные тесты.
3. Tester выполняет обязательный сквозной сценарий раздела 3 и, где возможно,
   ручные проверки раздела 4.
4. Итог: `TEST_STATUS: PASS` только при всех проверенных критериях; иначе
   `TEST_STATUS: FAIL` с нарушенным критерием и воспроизводимым сценарием;
   `TEST_STATUS: BLOCKED` — с указанием непроверенного и требуемого.
5. Live-проход уровня API не требуется: Day 15 не зависит от фактического
   ответа провайдера.
6. Агенты не выполняют `git commit` и `git push`; публикация — после решения
   пользователя.
