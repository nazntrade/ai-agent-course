# DEMO_PLAN — канонический демонстрационный план Day 15

Документ описывает демонстрационный сценарий Day 15 и, главное, границу между
**внешними переходами workflow** и **execution-планом**. Он дополняет
`ACCEPTANCE.md` (раздел «Обязательный сквозной сценарий») и `SPEC.md`, не меняя
FSM, статусы и журнал событий.

## 1. Цель демонстрации

Показать на одной задаче, что автомат состояний контролируется снаружи:

- недопустимое действие не выполняется и не вызывает провайдера;
- отказ объясняет причину и перечисляет действия, разрешённые сейчас;
- отказ попадает в отдельный append-only аудит, не меняя состояние, версию и
  `task_events`;
- `Pause → полный перезапуск → Resume` возвращает ту же задачу, стадию и шаг.

## 2. Внешние переходы до execution

Эти переходы **происходят вне плана** и завершаются до первого `Run step`.
Они не являются шагами плана:

1. `create_task` — задача создаётся в стадии `planning`, `status = active`,
   ожидается `Run planning`;
2. `run_planning` — модель формирует план, появляются `specification` и `plan`,
   ожидается `confirm_plan`;
3. `Review plan` — пользователь читает план;
4. `accept_plan` — `planning → execution`, текущим становится первый шаг.

`run_planning` в этом сценарии — внешний переход, а не шаг плана: он
выполняется до того, как план вообще существует.

## 3. Execution-план

План — это результат `run_planning`; в стадии `execution` выполняются только его
шаги. Поэтому каждый шаг плана обязан быть действием или проверкой **самой
задачи**, совместимой со стадией `execution`.

Канонический демо-план (модуль `task_demo.py`, константа `DEMO_EXECUTION_PLAN`):

| index | Шаг | Что это |
|---|---|---|
| 1 | `Зафиксировать доступные действия` | Проверка самой задачи |
| 2 | `Проверить отклонение недопустимого действия` | Вызвать запрещённое действие и зафиксировать отказ |
| 3 | `Сверить состояние задачи и аудит` | Убедиться, что состояние и версия не изменились, а отказ записан в аудит |

План строго валиден: `summary`, непустые `acceptance_criteria`, непрерывные
`index` с 1. Он проходит `task_prompts.parse_plan_response` без послаблений.

**Что запрещено в шагах плана.** Шаги не описывают сам workflow и его переходы:
`planning`/планирование, формирование/проверку/просмотр/принятие/утверждение
плана, запуск или завершение `execution`, `validation`. Такие шаги невыполнимы
после `Accept plan` и дают ложное «не подтверждено» на этапе проверки.

## 4. Внешние переходы после execution

После завершения всех шагов снова идут внешние переходы, а не шаги:

1. `finish_execution` — все шаги завершены, `execution → validation`;
2. `run_validation` — проверка результата по критериям приёмки; при успехе
   `validation → done/completed`, при дефектах возврат в `execution` только к
   дефектным шагам.

Упорядоченный набор внешних переходов зафиксирован в
`task_demo.DEMO_EXTERNAL_TRANSITIONS` и делится на
`before_execution` (`create_task`, `run_planning`, `accept_plan`) и
`after_execution` (`finish_execution`, `run_validation`).

## 5. Что именно демонстрируется

- **Transition guard.** Панель `Diagnostics / Task → Transition guard` показывает
  текущее состояние, `Allowed now`, решения по каждому действию и последние
  отказы; она не вызывает провайдера и не меняет задачу. Единственный виджет
  панели — безопасная кнопка `Test Finish execution`: её явный клик вызывает
  production-guard и при мягком отказе добавляет ровно одну append-only строку
  в аудит `task_transition_attempts`, не меняя `stage`/`status`/`version`,
  `current step`, `task_events` и артефакты.
- **Отказы.** Недопустимое действие даёт `noop`: провайдер не вызывается,
  `stage`/`status`/`version`/артефакты не меняются. Причина — например
  `expected_action_mismatch` до утверждения плана, `progress_incomplete` для
  `finish_execution` с неполными шагами, `task_is_terminal` после `done`.
- **Restart-safe Resume.** `Pause`, полное закрытие приложения, `run_app.bat`
  заново и `Resume`: та же задача, стадия и шаг, `version` +1, без дублей.

## 6. Defect-fixtures «как НЕ надо» и runtime-guard

### 6.1 Живой дефект полного плана

Дефект воспроизведён по реальным данным задачи владельца
«Day 15 — ручная приёмка переходов» (обезличены в `task_demo.REPORTED_TASK_GOAL`).
После `Run planning` провайдер вернул план rev 1, в котором часть шагов
невыполнима в стадии `execution`:

| index | Шаг (дефект) | Класс нарушения |
|---|---|---|
| 3 | `Выполнить и проверить validation` | `requires_other_stage` — validation наступает после execution |
| 4 | `Проверить завершение задачи` | `requires_other_stage` — done/finish_execution это переход FSM, а не шаг |
| 5 | `На тестовой копии состояния …` | `missing_entity` — такой сущности в UI нет |

Полный живой план сохранён обезличенно в `task_demo.REPORTED_LIVE_PLAN`: шаги
1–2 — обычные execution-действия, шаги 3–5 — дефектные; текст шага 5 частично
реконструирован. Более короткий отчётный fixture
`task_demo.REPORTED_PLAN_REV1` фиксирует три шага, переигрывающих стадию
`planning`.

`Accept plan` переводит задачу в `execution`, поэтому такие шаги невыполнимы и
проверка объявляет их не подтверждёнными. **Ключевое требование: задача не
может одновременно находиться в `planning` и `execution`** — план не должен
требовать обеих стадий сразу.

### 6.1.1 Третий живой дефект: semантический loopback

Итерация 3 фиксирует ещё один живой дефект: план прошёл guard, но его шаги и
критерии приёмки описывают переходы, которые к моменту проверки ещё не могли
состояться. Обезличенная реконструкция —
`task_demo.REPORTED_LOOPBACK_PLAN`:

| Поверхность | index | Формулировка (дефект) | Класс нарушения |
|---|---|---|---|
| step | 3 | `Вернуть задачу в planning и повторить переход` | `nonexistent_transition` — возврата в planning в продукте нет |
| step | 4 | `Подтвердить событие VALIDATION_PASSED` | `requires_other_stage` — validation наступает после execution |
| step | 5 | `Убедиться, что задача завершена (done)` | `requires_other_stage` — done наступает после validation |
| criterion | 1 | `Событие VALIDATION_PASSED подтверждено.` | `requires_other_stage` — validation ещё не наступала |
| criterion | 2 | `Задача завершена.` | `requires_other_stage` — done ещё не наступал |

Шаги 1–2 — обычные execution-действия и остаются чистыми. Каждый из шагов 4–5 и
критериев 1–2 подтверждает уже состоявшееся событие, которого на момент
планирования и выполнения быть не может, поэтому проверка снова дала бы ложное
«не подтверждено».

### 6.2 Темпоральная модель и проверяемые поверхности

Guard оценивает **две поверхности** — `steps` и `acceptance_criteria` — по
разным временным рамкам. На момент планирования уже прошли `TASK_CREATED`,
`PLAN_CREATED`, `PLAN_ACCEPTED` (plan/spec rev 1). Шаги выполняются в
`execution`, а критерии приёмки оцениваются в `validation` **после**
`EXECUTION_FINISHED`. Поэтому `STEP_COMPLETED`/`EXECUTION_FINISHED` для
критериев — уже прошедший факт, а для шага проверка результатов прошлых шагов
легитимна; токенами они не банятся. Токены `validation`/`done`/`VALIDATION_*`
остаются будущими на обеих поверхностях.

`task_prompts.plan_violations(plan)` возвращает `PlanViolation(index, title,
reason, location)` (`location` = `step` или `criterion`) и различает по
precedence: **HARD → future_stage → missing_entity → nonexistent_transition →
prior_stage**. `plan_step_violations(plan)` остаётся steps-only представлением.

- **HARD** (`REASON_REQUIRES_OTHER_STAGE`, `PLAN_STEP_HARD_MARKERS`) —
  безусловные фразы стадийных переходов: `run_validation`/«выполнить
  validation», `finish_execution`/«завершить execution», «завершить задачу»,
  «проверить done», «сформировать план», «принять/утвердить/отклонить план» и т. п.;
- **future_stage** (`REASON_REQUIRES_OTHER_STAGE`) — item подтверждает или
  заново выполняет будущую стадию: ID-токены `validation_passed`/
  `validation_failed`; связка «стади» с `validation`/`валидац`; стемы действий
  (`подтверд|убед|провер|свер|зафиксир|…`) с `validation`/`done`/`completed`;
  adjacency «действие + в/на/по + execution/validation»; `задач` × `заверш` и
  «задача выполнена»; статусные фразы `в done`, `статус completed` и т. п.
  (`execution` сознательно не входит в пары, иначе ловится «Проверить стадию
  execution»);
- **missing_entity** (`REASON_MISSING_ENTITY`) — несуществующая сущность:
  «тестовая копия», `test copy`, «копия состояния»;
- **nonexistent_transition** (`REASON_NONEXISTENT_TRANSITION`) — попытка вернуть
  задачу в `planning` (`planning`/«планировани» × `вернут|возврат|откат|
  перевед|перейти|назад|обратно|→|->`). Единственное исключение — framework
  guard-frame: `Transition guard`, «аудит отказов», «аудит попыток»,
  «диагностик», `refusal audit` (не голые `отказ`/`запрещ`/`отклон`);
- **prior_stage** (`REASON_REQUIRES_OTHER_STAGE`) — item заново выполняет
  прошлую стадию (`проверка planning/планирования`, «проверить стадию
  planning/планирования», `plan_accepted`, `plan_created`). Прошлую стадию
  можно проверять только в ретроспективной рамке (`Event timeline`,
  `timeline`, «журнал», «истори», «артефакт»/`artifact`).

Code-verb стемы (`реализ|добав|настро|…`) снимают future/prior-нарушения: задача,
которая сама реализует validation или планировщик, не блокируется. HARD-фразы
безусловны. Одиночные слова `planning`/`validation`/«валидация» в маркеры не
входят, поэтому нейтральные шаги «Реализовать проверку обхода validation»,
«Добавить стадию validation» и «Написать тесты для планировщика» принимаются.

### 6.3 Правило ретроспективных проверок

Прошлую стадию в плане можно проверять только как уже состоявшийся факт — по
`Event timeline` или сохранённым артефактам (`PLAN_ACCEPTED`, `plan rev 1`).
Запрет возврата в `planning` допустимо проверять через `Transition guard` или
аудит отказов: это проверка запрета, а не реальный переход. Исправленный план
`task_demo.CORRECTED_PLAN_EXAMPLE`:

| Поверхность | index | Формулировка (исправлено) | Что проверяется |
|---|---|---|---|
| step | 1 | `Проверить событие PLAN_ACCEPTED в Event timeline` | Событие уже в журнале — состоявшийся факт |
| step | 2 | `Сверить артефакт plan rev 1` | Сохранённый артефакт предыдущей стадии |
| step | 3 | `Проверить через Transition guard, что возврат в planning отклоняется` | Запрет перехода, а не сам переход |
| criterion | 1 | `Событие PLAN_ACCEPTED зафиксировано в Event timeline.` | Прошедший факт с ретро-рамкой |
| criterion | 2 | `Артефакт plan rev 1 доступен для сверки.` | Прошедший факт |
| criterion | 3 | `Возврат в planning отклонён и записан в аудит отказов.` | Зафиксированный запрет |

Критерии приёмки формулируются только про результаты самой задачи и уже
прошедшие факты; `validation`, `done` и `VALIDATION_*` не бывают ни шагами, ни
критериями. Такие формулировки выполнимы в `execution` и не требуют возврата в
`planning`.

### 6.4 Runtime-проверка, а не только промпт

Guard — это **runtime**-проверка, а не только текстовое правило промпта.
`task_prompts.parse_plan_response` после структурных проверок вызывает
`plan_violations` и при непустом результате бросает
`ExecutionIncompatiblePlanError` (наследник `ValueError`, поэтому существующие
`except ValueError` не меняются). Сообщение ошибки компактно: `step N (reason)` /
`criterion N (reason)`, максимум 4 записи и `… +K more`, без полного текста
предметов (и обрезается `validate_error_message` до 200 символов). Полный список
нарушений уходит в feedback следующей попытки: `task_stage._run_structured` при
`ValueError` парсера повторяет вызов с `messages + plan_retry_feedback(exc)`.
Feedback содержит секции «Недопустимые шаги:» и «Недопустимые критерии приёмки:»
и инструкции про ретро-рамку, запрет validation/done в критериях и допустимый
loopback только через `Transition guard`/аудит отказов. Поэтому невыполнимый план
отклоняется **до** `PLAN_CREATED`: пишется `API_ERROR(invalid_response)` без
артефакта `plan`, состояние и версия задачи не меняются, а пользователю доступен
`Retry`.

`task_demo.workflow_step_titles(plan)` — тонкое steps-only представление того же
guard для fixture-тестов: `tuple(v.title for v in plan_step_violations(plan))`.
`WORKFLOW_STEP_MARKERS` остаётся производным экспортом фразовых и стем-маркеров
для совместимости импортов тестов.

## 7. Где это проверяется

- `tests/test_task_demo.py` — уровень U: чистота `DEMO_EXECUTION_PLAN` и
  `CORRECTED_PLAN_EXAMPLE`, детекция `REPORTED_PLAN_REV1` и `REPORTED_LIVE_PLAN`
  (ровно шаги 3–5), `plan_violations(REPORTED_LOOPBACK_PLAN)` (шаги 3–5 и оба
  критерия), steps-only вид `plan_step_violations`, guard-frame и ретро-рамка
  (PLAN_ACCEPTED), приём нейтрального code-verb плана, отклонение
  `REPORTED_PLAN_REV1` через `parse_plan_response`, порядок
  `DEMO_EXTERNAL_TRANSITIONS`, устойчивые фразы правил в
  `TASK_PLANNING_SYSTEM_PROMPT` (критерии и loopback) и напоминание в action
  message `build_plan_messages`;
- `tests/test_tasks.py` — уровень U: `parse_plan_response` отклоняет
  `REPORTED_LIVE_PLAN` и `REPORTED_LOOPBACK_PLAN` с обеими локациями
  (`step`/`criterion`), компактное сообщение (≤200, без полных текстов) и feedback
  с секциями шагов и критериев; принимает исправленный и нейтральный планы;
  malformed-проверки не ослаблены;
- `tests/test_task_orchestrator.py` — уровень F: плохой live/loopback-план →
  корректирующий retry с feedback во втором payload → `PLAN_CREATED` с одним
  артефактом `plan` и `attempts=2`; плохой+плохой →
  `API_ERROR(invalid_response)` без артефакта, состояние и версия не изменены,
  доступен `Retry`;
- `tests/test_task_demo.py` — уровень F: `FakeClient` проходит демо-сценарий
  целиком, каждый шаг выполняется в `execution`, провайдер вызывается только на
  стадиях, а до `Accept plan` execution-действие отклоняется; отдельно
  проверяется, что planning-запрос, собранный `StageContextBuilder`, содержит
  правило про `execution` и проверку стадий как фактов;
- `TASK_PLANNING_SYSTEM_PROMPT` требует, чтобы шаги плана были выполнимы в
  стадии `execution`, помечает `planning` как этап формирования плана,
  запрещает шаги про workflow, предписывает проверять предыдущие стадии как уже
  состоявшиеся факты, ограничивает критерии приёмки прошедшими фактами и
  запрещает одновременное `planning` и `execution`; возврат в `planning` в плане
  допустим только как проверка запрета через `Transition guard`/аудит отказов.
