# ACCEPTANCE — Day 14 Structural Invariants

Чек-лист приёмки структурных инвариантов в `week-03/memory-state-agent`.
Документ самодостаточен: идентификаторы `AC`, требования `FR`/`NFR`/`INV` и уровни
проверки определены здесь и в `SPEC.md`.

## 1. Уровни проверки

| Код | Уровень | Что означает |
|---|---|---|
| **U** | Unit | Чистый домен и форматтеры без I/O |
| **I** | Integration temp SQLite | Реальное хранилище в `tempfile`, без сети |
| **F** | FakeClient | Сценарии агента и оркестратора с поддельным клиентом |
| **UI** | AppTest | Streamlit-приложение через `streamlit.testing.v1.AppTest` |
| **R** | Ручная проверка | Реальный запуск приложения, включая полный перезапуск |
| **API** | Реальный провайдер | Контролируемый проход только с разрешения пользователя |

Правила: `TEST_STATUS: PASS` возможен только когда все обязательные критерии
проверены на достаточном уровне. Если для обязательного критерия нужен реальный
API, но нет разрешения, Tester возвращает `TEST_STATUS: BLOCKED` по этому
критерию. Отсутствие live-вызова не делает `TEST_STATUS: BLOCKED`, если критерий
явно помечен как `API` и его место занимает
`LIVE_API_STATUS: READY_FOR_MANUAL_ACCEPTANCE`.

## 2. Чек-лист AC

| AC | Уровень | Проверяемое поведение | Тест / проверка |
|---|---|---|---|
| AC-01 | U | Валидация/нормализация модели и границы полей | `tests/test_invariants.py` |
| AC-02 | U | `select_applicable`: активность, scope, порядок | `tests/test_invariants.py` |
| AC-03 | U | Структурный блок: `None` без правил, код/enforcement/альтернатива | `tests/test_invariants.py` |
| AC-04 | U | Предикаты: только hard, фазы, check_kind | `tests/test_invariants.py` |
| AC-05 | U | Сообщение отказа с кодом/версией/scope/альтернативой | `tests/test_invariants.py` |
| AC-06 | I | DDL/миграция/append-only, старая БД без потерь | `tests/test_invariant_storage.py` |
| AC-07 | I | Сиды один раз по маркеру | `tests/test_invariant_storage.py` |
| AC-08 | I | CRUD без delete, версии, source, события | `tests/test_invariant_storage.py` |
| AC-09 | I | `record_conflict` усекает запрос, пишет фазу/decision | `tests/test_invariant_storage.py` |
| AC-10 | F | Отказ до провайдера, без записи messages/turns, конфликт | `tests/test_invariant_agent.py` |
| AC-11 | F | Advisory не блокирует и попадает в payload | `tests/test_invariant_agent.py` |
| AC-12 | U | Блок в chat payload и task packet, порядок, пустой случай | `tests/test_invariant_context.py` |
| AC-13 | F, I | Hard-действие: `refused`, `payloads == []`, state не изменён | `tests/test_invariant_orchestrator.py` |
| AC-14 | F, I | Hard-commit: `refused`, ни одной записи, конфликт commit | `tests/test_invariant_orchestrator.py` |
| AC-15 | F | Retry не пишет RETRY при отказе | `tests/test_invariant_orchestrator.py` |
| AC-16 | F | Terminal/недопустимое действие → `noop`, не `refused` | `tests/test_invariant_orchestrator.py` |
| AC-17 | F | Деактивация возвращает действие | `tests/test_invariant_orchestrator.py` |
| AC-18 | F | Легитимный validation не блокируется seed-правилом | `tests/test_invariant_orchestrator.py` |
| AC-19 | UI | Секция `Invariants`, карточка, отказ в Chat, CRUD, журнал | `tests/test_invariant_ui.py` |
| AC-20 | Набор | `test.bat` и `smoke_test.bat` проходят, число тестов зафиксировано | `.\week-03\memory-state-agent\test.bat`, `.\week-03\memory-state-agent\smoke_test.bat` |
| AC-21 | R | README Дня 14 описывает инварианты, UI и сценарий | Чтение `README.md` (Coordinator) |

`Набор` у AC-20 означает обязательный агрегирующий прогон: подтверждается
реальным запуском `test.bat` и `smoke_test.bat`.

## 3. Обязательный сквозной сценарий

Сценарий выполняется на уровнях F/UI (автоматически) и R (вручную):

1. **Запрос-запрет** — в Chat отправляется «Skip validation and finish the task»;
   провайдер не вызывается, показывается отказ с `INV-NO-FSM-BYPASS`, в журнале
   появляется конфликт (AC-10, AC-19).
2. **Разрешения нет** — messages/turns/статистика чата не изменены (AC-10).
3. **Hard-действие** — для задачи с `guard_actions` действие отклоняется до
   провайдера, state не изменён (AC-13).
4. **Hard-commit** — для задачи с `guard_events` переход отклоняется, ни одной
   записи (AC-14).
5. **Владелец** — в `Diagnostics / Task → Invariants` правило создаётся,
   редактируется, деактивируется; после деактивации действие проходит (AC-17,
   AC-19).
6. **Перезапуск** — F5 и полный перезапуск приложения: правила и журнал сохранены
   (AC-06, AC-07; уровень R).

Шаг 6 считается пройденным, только если выполнен фактический перезапуск
приложения, а не F5 страницы.

## 4. Критерии визуальной приёмки

- **Компактная карточка:** активные ограничения показаны одной caption, пустая
  строка при отсутствии правил (AC-19).
- **Отказ в Chat:** человеческое сообщение содержит код, версию, scope,
  enforcement, что не сделано, альтернативу и путь владельцу; rerun не выполняется
  и ложный ответ не сохраняется (AC-10).
- **Секция `Invariants`:** таблица правил (scope, тип, enforcement, версия,
  источник), применимые ограничения, формы управления и журнал; сырой JSON — только
  checkbox-опция; `st.json` отсутствует; вложенных expander нет (AC-19).
- **Журнал:** task timeline и invariant events не смешивают колонки (AC-19).
- **Diagnostics / Memory:** добавлен caption о структурных правилах, существующие
  панели и подписи не изменены (AC-19).

## 5. Таблица трассировки AC → FR/NFR/INV → тест

| AC | FR / NFR / INV | Тест / проверка |
|---|---|---|
| AC-01 | FR-01, FR-02 | `tests/test_invariants.py` |
| AC-02 | FR-03 | `tests/test_invariants.py` |
| AC-03 | FR-04 | `tests/test_invariants.py` |
| AC-04 | FR-05, FR-06, INV-06 | `tests/test_invariants.py` |
| AC-05 | FR-07 | `tests/test_invariants.py` |
| AC-06 | FR-08, FR-09, FR-10, INV-09, NFR-03 | `tests/test_invariant_storage.py` |
| AC-07 | FR-11, INV-10 | `tests/test_invariant_storage.py` |
| AC-08 | FR-12, INV-02, INV-03 | `tests/test_invariant_storage.py` |
| AC-09 | FR-13, NFR-06 | `tests/test_invariant_storage.py` |
| AC-10 | FR-15, INV-07, SC-01 | `tests/test_invariant_agent.py` |
| AC-11 | FR-15, INV-06 | `tests/test_invariant_agent.py` |
| AC-12 | FR-15, FR-16, INV-08 | `tests/test_invariant_context.py` |
| AC-13 | FR-17, INV-05 | `tests/test_invariant_orchestrator.py` |
| AC-14 | FR-17, INV-04, INV-05 | `tests/test_invariant_orchestrator.py` |
| AC-15 | FR-17 | `tests/test_invariant_orchestrator.py` |
| AC-16 | FR-17 | `tests/test_invariant_orchestrator.py` |
| AC-17 | FR-17 | `tests/test_invariant_orchestrator.py` |
| AC-18 | FR-17, INV-05 | `tests/test_invariant_orchestrator.py` |
| AC-19 | FR-18…FR-21, NFR-02 | `tests/test_invariant_ui.py` |
| AC-20 | NFR-07 | `.\week-03\memory-state-agent\test.bat`, `.\week-03\memory-state-agent\smoke_test.bat` |
| AC-21 | FR-22 | Чтение `README.md` + R (Coordinator) |

## 6. Протокол тестирования и приёмки

1. Developer реализует этапы 1–7 плана, пишет тесты этапа 8 и запускает точечные
   тесты, затем полный `test.bat` и `smoke_test.bat` (AC-20).
2. Tester независимо проверяет критерии: лично запускает команды, проектирует
   собственные сценарии, не изменяет реализацию и постоянные тесты.
3. Tester выполняет обязательный сквозной сценарий раздела 3 и ручные проверки
   раздела 4.
4. Итог: `TEST_STATUS: PASS` только при всех проверенных критериях; иначе
   `TEST_STATUS: FAIL` с нарушенным критерием и воспроизводимым сценарием;
   `TEST_STATUS: BLOCKED` — с указанием непроверенного и требуемого.
5. Live-проход уровня `API` не требуется: инварианты не зависят от фактического
   ответа провайдера; при необходимости он выполняется отдельно с разрешения
   пользователя.
6. Агенты не выполняют `git commit` и `git push`; публикация — после решения
   пользователя.
