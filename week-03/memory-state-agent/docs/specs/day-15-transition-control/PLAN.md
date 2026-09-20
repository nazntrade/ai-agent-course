# PLAN — Day 15 Controlled State Transitions

План усиления FSM Day 13/14 в `week-03/memory-state-agent`. Документ
самодостаточен: он перечисляет этапы, файлы, порядок работ, риски и трассировку
на `FR`/`NFR`/`INV`/`AC` из `SPEC.md`.

Порядок реализации: домен → storage → orchestrator → task UI → app → тесты →
документация. README обновляет Coordinator.

**Разрешение пользователя.** Реализация ведётся по прямому указанию пользователя
(«После плана не останавливайтесь»), то есть `SPEC_GATE` пройден без отдельной
остановки на утверждение.

## 1. Этапы работ

### Этап 1. Домен `tasks.py` (изменить)

**Содержимое:** перенос `ACTION_LABELS` из `task_ui`; reason-коды и
`REFUSAL_REASONS`/`REASON_TEXTS`; `TransitionDecision`;
`explain_transition`, `format_allowed_actions`, `format_refusal`. `can_apply` и
транзишн-хендлеры не меняются.

- FR-01…FR-05; INV-01; NFR-02.
- AC-01…AC-03. Чистый модуль без Streamlit/SQL/провайдера.

### Этап 2. Хранилище `task_storage.py` (изменить)

**Содержимое:** append-only таблица `task_transition_attempts`, индекс и триггер
`BEFORE UPDATE`; `TransitionAttempt`; `record_transition_attempt` (только
INSERT, `TaskNotFoundError` без задачи); `list_transition_attempts` (последние N
в хронологическом порядке).

- FR-06…FR-08; INV-03, INV-07; NFR-03, NFR-05.
- AC-04.

### Этап 3. Use cases `task_orchestrator.py` (изменить)

**Содержимое:** `TransitionGuardReport`; `transition_guard_report`; объяснённый
`_noop` с аудитом best-effort и reason-оверрайдами; аудит отказанного commit;
`_refused` не пишет новую таблицу.

- FR-09…FR-13; INV-02, INV-04; NFR-08.
- AC-05, AC-06.

### Этап 4. Task UI `task_ui.py` (изменить)

**Содержимое:** импорт `ACTION_LABELS`; `recommended_action`,
`is_review_plan_recommended`, `recommended_action_css`, `guard_decision_rows`,
`transition_attempt_rows`; `_render_transition_guard` и вызов сразу после
`_render_task_fields`; CSS-подсветка рекомендованного действия в
`_render_active_card`.

- FR-14…FR-16; INV-05, INV-06.
- AC-07…AC-09.

### Этап 5. Приложение `app.py` (изменить)

**Содержимое:** подпись активного чата без `▶`; CSS активного чата с точным
селектором до цикла чатов.

- FR-17; INV-06.
- AC-10.

### Этап 6. Тесты (обновить/добавить)

- `tests/test_tasks.py` — unit: консистентность `explain_transition`↔`can_apply`,
  причины, `format_refusal`, покрытие `ACTION_LABELS`. Уровень U; AC-01…AC-03.
- `tests/test_task_storage.py` — integration temp SQLite: миграция/reopen,
  round-trip, limit, триггер, изоляция от `task_events`, каскад. Уровень I;
  AC-04.
- `tests/test_task_orchestrator.py` — FakeClient: red-path сценарии, restart
  pause/resume без дублей, `transition_guard_report`. Уровень F/I; AC-05, AC-06.
- `tests/test_task_ui.py` — unit форматтеров и AppTest панели/подсветки. Уровень
  U/UI; AC-07…AC-09.
- `tests/test_app_ui.py` — AppTest сайдбара: подпись и CSS активного чата.
  Уровень UI; AC-10.

### Этап 7. Документация (создать)

**Файлы:** `docs/specs/day-15-transition-control/SPEC.md`, `PLAN.md`,
`ACCEPTANCE.md`.

- FR-01…FR-18; AC-01…AC-12. Комплект самодостаточен.

## 2. Порядок реализации и зависимости

| Шаг | Этап | Зависит от | Проверяемый результат |
|---|---|---|---|
| 1 | `tasks.py` | — | Unit домена проходит |
| 2 | `task_storage.py` | 1 | Integration temp SQLite проходит |
| 3 | `task_orchestrator.py` | 1, 2 | Red-path сценарии проходят |
| 4 | `task_ui.py` | 1, 3 | Панель и подсветка компилируются |
| 5 | `app.py` | 4 | Сайдбар запускается |
| 6 | `tests/*` | 1–5 | Полный `test.bat` проходит |
| 7 | `docs/specs/*` | 1–6 | Спецификация и приёмка готовы |

## 3. Риски и их митигация

| Риск | Влияние | Митигация | Связь |
|---|---|---|---|
| `explain_transition` разойдётся с `can_apply` | Неверная подсказка | Unit-тест консистентности на матрице состояний и действий | AC-02 |
| Отказ запишется в `task_events` | Порча журнала и версии | Аудит только в `task_transition_attempts`; тест изоляции | AC-04 |
| Hard-инвариант перестанет блокировать | Нарушение Day 14 | `_refused` без новой таблицы; существующие тесты зелёные | AC-05 |
| Аудит сломает действие | Потеря результата | Запись best-effort в `try/except pass` | AC-05 |
| Двойная машина состояний | Дрейф логики | Причины и allowed берутся из `can_apply` | INV-01 |
| CSS заденет `chat_42` при `chat_4` | Неверная подсветка | Точный селектор `class~=`, тест на отсутствие `class*=` | AC-10 |
| `st.json`/вложенный expander в панели | Регрессия UI | AppTest-проверки отсутствия | AC-08 |
| Миграция ломает старую БД | Потеря данных | Только `CREATE ... IF NOT EXISTS` | AC-04 |
| Секреты в аудите | Утечка | В аудите только коды/подписи/состояние | NFR-06 |

## 4. Что НЕ делается в этом объёме

- Вторая машина состояний, изменение `can_apply`/хендлеров/`task_events`.
- Изменение hard-инвариантов Day 14.
- Governance-файлы и README (README — Coordinator).
- Реальные платные вызовы API в автотестах.

## 5. Регрессионные обязательства

- Полный `test.bat` и `smoke_test.bat` проходят; существующие тесты не
  ослабляются (NFR-07, AC-11).
- Поведение Day 13/14 сохранено; hard-инвариант `no_validation_bypass`
  продолжает блокировать обход.
- `task_events` остаётся append-only; новая таблица аддитивна.

## 6. Критерии завершения плана

План выполнен, когда все этапы реализованы, `test.bat` и `smoke_test.bat`
проходят, документация готова, а независимый Tester подтверждает
`TEST_STATUS: PASS` по `ACCEPTANCE.md`.
