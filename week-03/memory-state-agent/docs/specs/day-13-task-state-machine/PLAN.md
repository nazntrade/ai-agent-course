# PLAN — Day 13 Task State Machine

План реализации состояния задачи в `week-03/memory-state-agent` по команде пользователя `ДЕЛАЕМ`.
Документ самодостаточен: он перечисляет этапы, файлы, порядок работ, риски и трассировку на
`FR`/`NFR`/`AC` из `SPEC.md`. Объём и поведение определяются `SPEC.md`; здесь — только как
именно они реализуются.

Порядок реализации: домен → storage → prompts → context → stage → orchestrator → UI →
app/storage/agent/pricing/memory → тесты → README.

## 1. Этапы работ

### Этап 1. Домен `tasks.py` (создать)

**Файл:** `tasks.py`.
**Содержимое:** константы стадий, статусов, типов событий, типов и текстов ожидаемых действий,
badges; dataclass-ы `Task`, `TaskArtifact`, `TaskEvent`, `WorkflowProfile`; таблица переходов;
`TaskStateMachine.can_apply` / `TaskStateMachine.apply`; исключения `InvalidTransitionError`,
`TransitionPayloadError`; результат `TransitionResult`.

- FR-01…FR-09, INV-01…INV-11; чистый модуль без Streamlit и SQL (NFR-02).
- AC-01…AC-13, AC-18…AC-21 (проверяются unit-тестами).

### Этап 2. Хранилище `task_storage.py` (создать)

**Файл:** `task_storage.py`.
**Содержимое:** `TaskRepository` с DDL `workflow_profiles` / `tasks` / `task_artifacts` /
`task_events`, partial-индексами, триггером `BEFORE UPDATE RAISE(ABORT)`, идемпотентной
миграцией, сидом default workflow; методы `create_task`, `get_task`/`list_tasks`, `append_event`,
`commit_transition`, `add_artifact`, `list_artifacts`/`list_events`,
`set_active_task_id`/`get_active_task_id`, `resolve_display_task`, `clear_selection`,
`get_task_usage`; `TaskVersionConflictError`.

- FR-10…FR-15; INV-04…INV-09; NFR-02, NFR-03, NFR-05.
- AC-05, AC-11, AC-14…AC-23, AC-47 (частично) — интеграционные тесты с временной SQLite.
- `PRAGMA foreign_keys`, WAL, `busy_timeout` на каждом соединении (NFR-05).

### Этап 3. Промпты и парсеры `task_prompts.py` (создать)

**Файл:** `task_prompts.py`.
**Содержимое:** промпты стадий planning / execution / validation; бюджеты (план, шаг, валидация
и retry-бюджеты); строгие парсеры плана и валидации; форматирование артефактов и дефектов для
промпта.

- FR-17, FR-21, FR-26, FR-31; NFR-08.
- AC-02, AC-08, AC-09, AC-26, AC-27.
- Парсеры отклоняют пустой, невалидный и обрезанный JSON; retry-бюджеты — константы (FR-31).

### Этап 4. Context packet `task_context.py` (создать)

**Файл:** `task_context.py`.
**Содержимое:** `StageContextBuilder`, собирающий блоки 1–9, выбор блоков по действию, выбор
артефактов по стадии, ≈ токены и ≈ стоимость блоков.

- FR-27, FR-28; NFR-10, NFR-06.
- AC-30, AC-31, AC-32, AC-35.
- Профиль включается в task-пакеты (расширение Дня 12, фиксируется в README).
- Preview не делает API-вызовов.

### Этап 5. Выполнение стадии `task_stage.py` (создать)

**Файл:** `task_stage.py`.
**Содержимое:** `StageExecutor` + `Completer`: нестриминговые planning/validation; стриминговый
execution при `config.stream=True` и `on_chunk`; классификация ошибок провайдера
(provider_error / stream_error / invalid_response / truncated / context_overflow); один повтор с
увеличенным бюджетом; сбор usage (model, tokens, cache hit/miss, finish_reason, cost_usd,
attempts).

- FR-17, FR-21, FR-26, FR-29, FR-31; NFR-01, NFR-04, NFR-10.
- AC-24…AC-29, AC-33, AC-36.
- Стриминговый текст не пишется в `messages` и не становится артефактом.

### Этап 6. Use cases `task_orchestrator.py` (создать)

**Файл:** `task_orchestrator.py`.
**Содержимое:** use cases create/run_planning/reject_plan/accept_plan/run_step/finish_execution/
run_validation/pause/resume/block/unblock/cancel/retry; обработка ошибок API; пересчёт
`expected_action`; `preview packet`.

- FR-16…FR-26, FR-31, FR-09; INV-03, INV-04, INV-08; NFR-02.
- AC-01…AC-10, AC-14…AC-17, AC-24…AC-29.

### Этап 7. UI `task_ui.py` (создать)

**Файл:** `task_ui.py`.
**Содержимое:** карточка задачи, вертикальный индикатор стадий, badge, действия из `can_apply`,
create-task диалог, завершение задачи (итог, Open full result, New task), секции
`Diagnostics / Task` (list, timeline, artifacts, read-only workflow, packet preview, task usage),
чистые форматтеры.

- FR-32…FR-38; NFR-07, NFR-11.
- AC-37…AC-45.
- Ключи виджетов с префиксом `task_`; при отрисовке нет API-вызовов.

### Этап 8. Интеграция с существующим кодом (изменить)

**Файлы и изменения:**

- `app.py` — третий режим `Diagnostics / Task` и карточка в `Chat`; роль в `st.bottom`;
  обработчик удаления чата очищает `active_task:{chat_id}`; новых expander'ов в `Chat` нет.
  FR-32, FR-33, FR-14, FR-37, FR-38; NFR-02, NFR-11; AC-37, AC-38, AC-40, AC-44, AC-45.
- `storage.py` — аддитивное публичное свойство `ChatStore.db_path`. FR-11; AC-22.
- `agent.py` — аддитивный `ChatAgent.complete(...)`. FR-30; AC-34.
- `pricing.py` — аддитивная `estimate_tokens_cost(model, input_tokens, dt=None)` и уточнение
  docstring (pre-flight preview, not billing). FR-27, NFR-10; AC-32.
- `memory.py` — `format_invariants_block()` как единый источник строки инвариантов. FR-28;
  AC-35.
- `README.md` — раздел Дня 13, расширение Дня 12 (профиль в task-пакетах), инструкции UI и
  сценарий демонстрационного видео. FR-39; AC-47.

Инвариант сохранения поведения: chat-payload Дней 10–12 не меняется (FR-28, AC-35);
`context.py`, `strategies.py`, `facts.py`, `profile.py`, `stats.py`, `tokens.py`, `models.py`,
`app_logic.py`, `test.bat`, `smoke_test.bat`, `run_app.bat`, `requirements.txt` не изменяются.

### Этап 9. Тесты (создать)

**Файлы:**

- `tests/test_tasks.py` — unit FSM: переходы, preconditions, `can_apply`, expected actions,
  badges, инварианты, отклонённые переходы. FR-01…FR-09, FR-22; INV-01…INV-11;
  AC-01…AC-13, AC-18…AC-21 (уровень U).
- `tests/test_task_storage.py` — интеграция с временной SQLite: DDL, миграция, идемпотентность,
  version, `idempotency_key`, append-only, каскад, `active_task`, task usage. FR-10…FR-15;
  NFR-03, NFR-05; AC-05, AC-14…AC-23, AC-36.
- `tests/test_task_context.py` — порядок блоков 1–9, выбор артефактов, preview, ≈ токены и
  стоимость, равенство chat payload и `format_invariants_block`. FR-27, FR-28; NFR-10;
  AC-30…AC-32, AC-35.
- `tests/test_task_orchestrator.py` — use cases и ошибки API на FakeClient: create, plan,
  step, validation, pause/resume, block/unblock, cancel, retry, overflow. FR-16…FR-26, FR-29…FR-31;
  AC-01…AC-10, AC-14…AC-17, AC-24…AC-29, AC-33, AC-34, AC-36.
- `tests/test_task_ui.py` — AppTest: режим `Diagnostics / Task`, карточка, действия из
  `can_apply`, диалог создания, завершение задачи, diagnostics-секции, отсутствие API-вызовов.
  FR-32…FR-38; NFR-07, NFR-11; AC-37…AC-45.

Все тесты изолированы от сети и настоящего `.env`, используют FakeClient и временную SQLite
(NFR-04, AC-46).

### Этап 10. README и сценарий видео

**Файл:** `README.md`.
**Содержимое:** назначение состояния задачи, FSM и статусы, артефакты и журнал, pause/resume,
block/unblock, cancel, retry, context packet, UI (`Chat` и `Diagnostics / Task`), расширение
Дня 12 (профиль в task-пакетах), ручной сценарий проверки и сценарий демонстрационного видео.

- FR-39, NFR-06; AC-46, AC-47, AC-48.

## 2. Порядок реализации и зависимости

| Шаг | Этап | Зависит от | Требования | Проверяемый результат |
|---|---|---|---|---|
| 1 | `tasks.py` | — | FR-01…FR-09; INV-01…INV-11; AC-01…AC-13, AC-18…AC-21; NFR-02 | Unit FSM проходит |
| 2 | `task_storage.py` | 1 | FR-10…FR-15; INV-04…INV-09; NFR-02, NFR-03, NFR-05; AC-05, AC-11, AC-14…AC-23 | Интеграция с временной SQLite проходит |
| 3 | `task_prompts.py` | — | FR-17, FR-21, FR-26, FR-31; NFR-08; AC-02, AC-08, AC-09, AC-26, AC-27 | Парсеры плана/валидации проходят |
| 4 | `task_context.py` | 1, 3 | FR-27, FR-28; NFR-06, NFR-10; AC-30, AC-31, AC-32, AC-35 | Порядок блоков и preview проходят |
| 5 | `task_stage.py` | 3, 4 | FR-17, FR-21, FR-26, FR-29, FR-31; NFR-01, NFR-04, NFR-10; AC-24…AC-29, AC-33, AC-36 | FakeClient-сценарии стадий проходят |
| 6 | `task_orchestrator.py` | 2, 5 | FR-09, FR-16…FR-26, FR-31; INV-03, INV-04, INV-08; NFR-02; AC-01…AC-10, AC-14…AC-17, AC-24…AC-29 | Use cases и ошибки API проходят |
| 7 | `task_ui.py` | 1, 6 | FR-32…FR-38; NFR-07, NFR-11; AC-37…AC-45 | AppTest карточки и diagnostics проходит |
| 8 | `app.py`, `storage.py`, `agent.py`, `pricing.py`, `memory.py` | 2, 6, 7 | FR-11, FR-14, FR-27, FR-28, FR-30, FR-32…FR-38; NFR-02, NFR-10, NFR-11; AC-22, AC-32, AC-34, AC-35, AC-37, AC-38, AC-40, AC-44, AC-45 | Интеграция в приложение, режим и complete |
| 9 | `tests/*` | 1–8 | FR-01…FR-38; NFR-04, NFR-09; AC-01…AC-47 | Полный `test.bat` проходит |
| 10 | `README.md` | 8, 9 | FR-39; NFR-06; AC-47, AC-48 | Раздел и сценарий видео готовы |

Порядок обязателен: домен определяет контракт раньше хранилища, хранилище — раньше use cases,
use cases — раньше UI; тесты пишутся параллельно этапам и прогоняются на каждом шаге.

## 3. Риски и их митигация

| Риск | Влияние | Митигация | Связь |
|---|---|---|---|
| Дубли событий/артефактов при повторной отправке | Нарушение INV-06, неверная переделка шагов | preconditions + version + UNIQUE + `idempotency_key` в одной транзакции | FR-12; INV-06; AC-20 |
| Гонка `version` между rerunn'ами Streamlit | `TaskVersionConflictError` показывается пользователю | Оптимистичная блокировка, перечитывание задачи и безопасный retry действия | FR-12; AC-20 |
| Изменение chat payload Дней 10–12 | Регрессия существующего поведения | `format_invariants_block()` как единый источник, тест байт-в-байт | FR-28; AC-35 |
| Task-вызовы попадают в `messages`/`turns` и статистику чата | Порча истории и статистики | `complete()` не пишет в store, отдельный учёт в `payload_json` | FR-30, FR-31; AC-36 |
| Частичный результат при ошибке API | Нарушение INV-08 | Артефакт только при успехе, `API_ERROR` без изменения Task | FR-26; AC-24, AC-25 |
| Обрезанный JSON плана/валидации | Некорректное продвижение стадий | Строгий парсер + один повтор с увеличенным бюджетом | FR-17, FR-21; AC-26 |
| Карточка вытесняет историю на 1024×768 | Нарушение NFR-11 | Бюджет высоты ≤ ~150 px и fallback в сайдбар | FR-38; AC-40 |
| Состояние теряется при перезапуске | Нарушение INV-03 | Всё состояние в SQLite, resume не пересоздаёт задачу | FR-23; AC-14, AC-15 |
| Миграция ломает старую БД | Потеря данных | `IF NOT EXISTS`, отсутствие изменений существующих таблиц, повторное открытие | FR-11; NFR-03; AC-22 |
| Секреты и полные промпты в событиях | Утечка | Усечение сообщений, запрет полных служебных промптов | NFR-06; AC-36 |
| Стейл UI-виджетов между задачами и rerunn'ами | Неверная отрисовка или потеря выбора | Ключи с префиксом `task_` и nonce формы | FR-33, FR-34; AC-38, AC-41 |

## 4. Что НЕ делается в этом объёме

- Разные модели/provider на стадию (только точка расширения).
- Дополнительные workflow-профили в UI и визуальный редактор workflow.
- Автоматический `MemoryExtractor`, фоновые воркеры, распределённая очередь.
- Jira-подобные функции и выполнение shell-команд задачей.
- Release/security gate, SAST/DAST/SCA/SBOM.
- Полноценный coding-agent.
- Публичное удаление задач/событий и редактирование артефактов.
- Изменения `context.py`, `strategies.py`, `facts.py`, `profile.py`, `stats.py`, `tokens.py`,
  `models.py`, `app_logic.py`, `test.bat`, `smoke_test.bat`, `run_app.bat`, `requirements.txt`.
- Изменения `PROJECT_RULES.md` (раздел SDD — задача Configurator).
- Реальные платные вызовы API в автотестах (LIVE-приёмка — отдельно и только с разрешения
  пользователя).

## 5. Регрессионные обязательства

- Полный `test.bat` проходит; существующие тесты не ослабляются (NFR-09, AC-46).
- `smoke_test.bat` проходит (NFR-09, AC-46).
- Поведение Дней 10–12 сохранено: стратегии контекста, слои памяти, профили, ветки, streaming,
  статистика токенов и стоимости, безопасное поведение при ошибках API (FR-28, AC-35).

## 6. Критерии завершения плана

План выполнен, когда созданы все перечисленные файлы, обновлён `README.md`, `test.bat` и
`smoke_test.bat` проходят, а независимый Tester подтверждает `TEST_STATUS: PASS` по
`ACCEPTANCE.md`. До отдельного разрешения пользователя LIVE-статус остаётся
`LIVE_API_STATUS: READY_FOR_MANUAL_ACCEPTANCE`.
