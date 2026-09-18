# PLAN — Day 14 Structural Invariants

План реализации структурных инвариантов в `week-03/memory-state-agent`.
Документ самодостаточен: он перечисляет этапы, файлы, порядок работ, риски и
трассировку на `FR`/`NFR`/`INV`/`AC` из `SPEC.md`.

Порядок реализации: домен → storage → UI-форматтеры → интеграция agent/context →
orchestrator → task UI → app → тесты → документация. README обновляет Coordinator.

## 1. Этапы работ

### Этап 1. Домен `invariants.py` (создать)

**Содержимое:** константы scope/enforcement/kind/check/source/phase/event;
`Invariant`, `InvariantConflict`, `InvariantConflictError`; `validate_code`,
`normalize_invariant`, `select_applicable`, `format_structural_invariants_block`,
`find_request_conflict`, `find_action_conflict`, `find_transition_conflict`,
`evaluate_check`, `format_conflict_message`.

- FR-01…FR-07, INV-01, INV-02, INV-06; NFR-02.
- AC-01…AC-05. Чистый модуль без Streamlit/SQL/провайдера.

### Этап 2. Хранилище `invariant_storage.py` (создать)

**Содержимое:** `InvariantRepository`: DDL `invariants`/`invariant_events` +
триггер append-only, идемпотентная миграция, сидирование дефолтов по маркеру,
CRUD без удаления, `record_conflict`, `list_events`; `DuplicateInvariantCodeError`.

- FR-08…FR-14, INV-03…INV-05, INV-09, INV-10; NFR-03, NFR-05.
- AC-06…AC-09, AC-16 (частично).

### Этап 3. UI-форматтеры и секция `invariant_ui.py` (создать)

**Содержимое:** чистые форматтеры (`format_scope`, `invariant_rows`,
`format_active_restrictions`, `invariant_event_rows`, `format_invariant_details`,
`parse_surfaces`); Streamlit-рендер секции `Invariants` и формы управления.

- FR-19…FR-21; NFR-06.
- AC-19. Без `st.json`, без вложенных expander, ключи `invariant_`.

### Этап 4. Интеграция `agent.py` и `task_context.py` (изменить)

- `agent.py`: keyword-only `invariants`/`task_lookup`; `_applicable_invariants`;
  отказ в `ask` до payload/провайдера/`save_turn`; структурный блок в
  `_with_memory_blocks`.
- `task_context.py`: `BLOCK_STRUCTURAL_INVARIANTS` между `BLOCK_INVARIANTS` и
  `BLOCK_PROFILE`; kwarg `structural_invariants=()`.

- FR-15, FR-16; INV-07, INV-08; AC-10…AC-12.

### Этап 5. Use cases `task_orchestrator.py` (изменить)

**Содержимое:** `invariants` kwarg, `applicable_invariants`, `_build_packet`
передаёт структурный блок; `_guarded` проверяет hard-действие; `retry` не пишет
RETRY при отказе; `_commit` проверяет hard-переход; `STATUS_REFUSED`,
`ERROR_INVARIANT_CONFLICT`, `_refused`, `_record_conflict`.

- FR-17; INV-04, INV-05; AC-13…AC-18.

### Этап 6. Task UI `task_ui.py` (изменить)

**Содержимое:** компактная caption ограничений в карточке; таблица событий
инвариантов в журнале; `render_diagnostics_task(..., invariant_repository=None)` с
вызовом `invariant_ui.render_invariants_section`.

- FR-19…FR-21; AC-19.

### Этап 7. Приложение `app.py` (изменить)

**Содержимое:** `InvariantRepository` в `st.session_state`; единая
`_build_agent(chat_id)` с `invariants=`/`task_lookup=`; замена всех прежних
созданий `ChatAgent`; `except InvariantConflictError` в chat-обработчике;
передача репозитория в `render_diagnostics_task`; caption в Diagnostics/Memory.

- FR-18; AC-19.

### Этап 8. Тесты (создать)

- `tests/test_invariants.py` — unit домена: валидация, выбор/порядок, блок,
  триггеры, предикаты, сообщение, границы. Уровень U; AC-01…AC-05.
- `tests/test_invariant_storage.py` — integration temp SQLite: миграция/reopen,
  сиды один раз, версии/источник, события, деактивация, отсутствие delete, старая
  БД, append-only. Уровень I; AC-06…AC-09.
- `tests/test_invariant_context.py` — блок в chat payload и task packet, порядок,
  отсутствие блока без правил. Уровень U; AC-12.
- `tests/test_invariant_agent.py` — FakeClient: отказ без вызова провайдера и без
  записи, advisory в payload, task-scope изоляция. Уровень F; AC-10, AC-11.
- `tests/test_invariant_orchestrator.py` — FakeClient + temp SQLite: hard-конфликт
  до действия/commit, отсутствие ложного артефакта, task state не изменён, retry,
  terminal noop, легитимный validation, деактивация. Уровень F/I; AC-13…AC-18.
- `tests/test_invariant_ui.py` — AppTest с bare client и temp store: секция
  `Invariants`, компактная строка, отказ при `chat_input`, CRUD, журнал. Уровень
  UI; AC-19.

### Этап 9. Документация (создать)

**Файлы:** `docs/specs/day-14-invariants/SPEC.md`, `PLAN.md`, `ACCEPTANCE.md`.

- FR-01…FR-22; AC-01…AC-21. Комплект самодостаточен и не требует переписки.

## 2. Порядок реализации и зависимости

| Шаг | Этап | Зависит от | Проверяемый результат |
|---|---|---|---|
| 1 | `invariants.py` | — | Unit домена проходит |
| 2 | `invariant_storage.py` | 1 | Integration temp SQLite проходит |
| 3 | `invariant_ui.py` | 1, 2 | Форматтеры и секция компилируются |
| 4 | `agent.py`, `task_context.py` | 1, 2 | Chat/task payload и отказ проходят |
| 5 | `task_orchestrator.py` | 2, 4 | Hard-конфликты действия/commit проходят |
| 6 | `task_ui.py` | 3, 5 | Секция и журнал отрисовываются |
| 7 | `app.py` | 5, 6 | Приложение запускается, отказ в Chat |
| 8 | `tests/*` | 1–7 | Полный `test.bat` проходит |
| 9 | `docs/specs/*` | 1–8 | Спецификация и приёмка готовы |

## 3. Риски и их митигация

| Риск | Влияние | Митигация | Связь |
|---|---|---|---|
| Hard-правило блокирует легитимный validation | Задача не завершается | `check_kind=no_validation_bypass` пропускает `action=run_validation`; regression-тест | AC-18 |
| Конфликт меняет состояние задачи | Нарушение INV-04 | Проверка до `apply_transition`/commit; тесты на версию/события | AC-14 |
| Ложный успешный артефакт при отказе | Нарушение INV-07 | `_commit` возвращает `refused` до записи; тест артефактов | AC-13, AC-14 |
| Отказ сохраняет сообщение/turn | Порча истории | Проверка в `ask` до `save_turn`; тест messages/turns | AC-10 |
| Миграция ломает старую БД | Потеря данных | `IF NOT EXISTS` без ALTER; тест Days 7–13 | AC-06 |
| Сиды воскрешают удалённое правило | Неожиданные запреты | Маркер `default_invariants_seeded` | AC-07 |
| Advisory блокирует | Нарушение INV-06 | Предикаты смотрят только hard; тест advisory | AC-04, AC-11 |
| Chat payload Дней 10–13 меняется без репозитория | Регрессия | Пустой блок → `None`; byte-identical тест | AC-12 |
| Секреты в журнале | Утечка | Усечение запроса ≤200; отказ без секретов | AC-09 |
| Стейл UI-виджетов | Неверная отрисовка | Префикс `invariant_`, отдельные ключи create/edit | AC-19 |

## 4. Что НЕ делается в этом объёме

- Новый provider/model, shell execution, distributed policy engine.
- Формальная верификация LLM, визуальный конструктор workflow, SOC, новое железо.
- Разрушительный reset данных.
- Изменение бизнес-логики FSM `tasks.py`.
- Governance-файлы и README (README — Coordinator).
- Реальные платные вызовы API в автотестах.

## 5. Регрессионные обязательства

- Полный `test.bat` и `smoke_test.bat` проходят; существующие тесты не ослабляются
  (NFR-07, AC-20).
- Поведение Дней 10–13 сохранено; chat payload без репозитория инвариантов
  байт-идентичен прежнему (AC-12).
- `task_events` остаётся append-only после миграции (AC-06).

## 6. Критерии завершения плана

План выполнен, когда созданы все перечисленные файлы, `test.bat` и
`smoke_test.bat` проходят, документация комплекта готова, а независимый Tester
подтверждает `TEST_STATUS: PASS` по `ACCEPTANCE.md`.
