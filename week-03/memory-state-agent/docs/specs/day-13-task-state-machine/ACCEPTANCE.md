# ACCEPTANCE — Day 13 Task State Machine

Чек-лист приёмки состояния задачи в `week-03/memory-state-agent` по команде пользователя
`ДЕЛАЕМ`. Документ самодостаточен: идентификаторы `AC`, требования `FR`/`NFR` и уровни проверки
определены здесь и в `SPEC.md`.

## 1. Уровни проверки

| Код | Уровень | Что означает |
|---|---|---|
| **U** | Unit с моками | Чистая логика домена и форматтеров без I/O |
| **I** | Интеграция с временной SQLite | Реальное хранилище в `tempfile`, без сети |
| **F** | FakeClient | Сценарии агента и стадий с поддельным клиентом, без сети |
| **UI** | AppTest | Streamlit-приложение через `streamlit.testing.v1.AppTest` |
| **R** | Ручная проверка | Реальный запуск приложения, включая полный перезапуск |
| **API** | Реальный внешний провайдер | Контролируемый платный проход только с разрешения пользователя |

Правила: `TEST_STATUS: PASS` возможен только когда все обязательные критерии проверены на
достаточном уровне. Если для обязательного критерия нужен реальный API, но нет разрешения,
Tester возвращает `TEST_STATUS: BLOCKED` по этому критерию. Отсутствие live-вызова не считается
дефектом и не делает `TEST_STATUS: BLOCKED`, если критерий явно помечен как `API` и его место
занимает `LIVE_API_STATUS: READY_FOR_MANUAL_ACCEPTANCE`.

## 2. Чек-лист AC

| AC | Уровень | Проверяемое поведение | Тест / проверка |
|---|---|---|---|
| AC-01 | F, I | TASK_CREATED: planning/active, version=1, run_planning, «Planning», current_step_index=NULL, ровно одно событие | `tests/test_task_orchestrator.py`, `tests/test_task_storage.py` |
| AC-02 | F | PLAN_CREATED: specification rev1 + plan rev1, шаг 1, confirm_plan | `tests/test_task_orchestrator.py` |
| AC-03 | F | PLAN_REJECTED → run_planning, старый plan сохранён, повторный план rev2 | `tests/test_task_orchestrator.py` |
| AC-04 | F | PLAN_ACCEPTED → execution/active, run_step, первый незавершённый шаг | `tests/test_task_orchestrator.py` |
| AC-05 | F, I | STEP_COMPLETED: execution_result rev+1 (step_index, round), stage не изменён | `tests/test_task_orchestrator.py`, `tests/test_task_storage.py` |
| AC-06 | U, F | Продвижение: ожидающие переделки → незавершённые → finish_execution | `tests/test_tasks.py`, `tests/test_task_orchestrator.py` |
| AC-07 | F | EXECUTION_FINISHED только при всех завершённых шагах → validation | `tests/test_task_orchestrator.py` |
| AC-08 | F | VALIDATION_PASSED → done/completed, review_result, final_result rev1 | `tests/test_task_orchestrator.py` |
| AC-09 | F | VALIDATION_FAILED → execution, current_step_index=min(defects), run_step, переделка только дефектных | `tests/test_task_orchestrator.py` |
| AC-10 | U, I | Правило завершённости/переделки по pair артефактов, снятие переделки новой ревизией | `tests/test_tasks.py`, `tests/test_task_storage.py` |
| AC-11 | U | Недопустимые переходы (включая из done/cancelled) → InvalidTransitionError, ничего не записано | `tests/test_tasks.py` |
| AC-12 | U | expected_action_text и badge для всех событий FR-08 | `tests/test_tasks.py` |
| AC-13 | U | Наборы can_apply и дополнительный retry после API_ERROR | `tests/test_tasks.py` |
| AC-14 | I, R | PAUSE, полный перезапуск, RESUME: та же задача, стадия, шаг, expected action; pause_reason пуст | `tests/test_task_storage.py` + реальный перезапуск приложения |
| AC-15 | F, I | RESUME продолжает с i+1, не повторяет завершённые шаги, не создаёт задачу | `tests/test_task_orchestrator.py`, `tests/test_task_storage.py` |
| AC-16 | F | BLOCK требует причину и действие; UNBLOCK пересчитывает expected_action, stage сохранён | `tests/test_task_orchestrator.py` |
| AC-17 | F | CANCEL только с confirmed=true; stage сохранён, expected_action=none, журнал и артефакты на месте | `tests/test_task_orchestrator.py` |
| AC-18 | I | Сбой транзакции → полный откат Task, события и артефакта | `tests/test_task_storage.py` |
| AC-19 | I, U | version +1 для state-changing событий; API_ERROR и RETRY не меняют version | `tests/test_task_storage.py`, `tests/test_tasks.py` |
| AC-20 | I | Дубль по idempotency_key не создаётся; старая version → TaskVersionConflictError | `tests/test_task_storage.py` |
| AC-21 | I | Артефакты/события append-only (триггер, запрет INSERT OR REPLACE), повторный шаг → новая ревизия | `tests/test_task_storage.py` |
| AC-22 | I | Двойное открытие старой БД (дни 7–13): идемпотентно, данные целы, старые чаты без задач | `tests/test_task_storage.py` |
| AC-23 | I | Удаление чата каскадно удаляет задачи/артефакты/события, чистит active_task, не трогает long-term memory | `tests/test_task_storage.py` |
| AC-24 | F | Ошибка до первого токена: Task не изменён, артефакта нет, только API_ERROR, UI предлагает Retry/Pause/Cancel | `tests/test_task_orchestrator.py` + `tests/test_task_ui.py` |
| AC-25 | F, UI | Обрыв mid-stream: placeholder очищен, частичный текст не сохранён, API_ERROR(stream_error) | `tests/test_task_orchestrator.py`, `tests/test_task_ui.py` |
| AC-26 | F | Пустой/невалидный/обрезанный JSON плана и валидации: один повтор, затем API_ERROR(invalid_response) | `tests/test_task_orchestrator.py` |
| AC-27 | F | Обрезанный execution-шаг: один повтор, затем API_ERROR(truncated) без артефакта | `tests/test_task_orchestrator.py` |
| AC-28 | F | RETRY только после API_ERROR; успешный retry создаёт артефакт и меняет version через действие | `tests/test_task_orchestrator.py` |
| AC-29 | F | context overflow → API_ERROR(context_overflow), статус не изменён, Retry доступен | `tests/test_task_orchestrator.py` |
| AC-30 | U | Порядок блоков packet 1–9; для не-LLM действий вызова нет | `tests/test_task_context.py` |
| AC-31 | U | Выбор артефактов по стадиям, только последние ревизии | `tests/test_task_context.py` |
| AC-32 | U, F | Preview без API-вызова: блоки, ≈ токены, ≈ стоимость, «estimate, not billing» | `tests/test_task_context.py`, `tests/test_task_orchestrator.py` |
| AC-33 | F, UI | Planning/validation нестриминговые; execution стримит только при stream=True и on_chunk; spinner и placeholder | `tests/test_task_orchestrator.py`, `tests/test_task_ui.py` |
| AC-34 | F | `complete(messages, ...)`: сигнатура, model/лимиты из конфига, usage из финального чанка, store не тронут, исключения проброшены | `tests/test_task_orchestrator.py` |
| AC-35 | U | Chat payload Дней 10–12 совпадает байт-в-байт; `format_invariants_block()` — прежняя строка | `tests/test_task_context.py` |
| AC-36 | I, F | Task-вызовы не пишут messages/turns и не меняют статистику чата; usage в payload_json; секретов и полных промптов нет | `tests/test_task_storage.py`, `tests/test_task_orchestrator.py` |
| AC-37 | UI | Порядок radio: Chat, Diagnostics / Memory, Diagnostics / Task; Chat по умолчанию; прежние панели не изменены | `tests/test_task_ui.py` |
| AC-38 | UI | Карточка первая в st.bottom, не оборачивает профиль, chat_input порядок, ключи task_, без API-вызовов | `tests/test_task_ui.py` |
| AC-39 | UI, R | Вертикальный индикатор: пройденные/текущая/будущие стили, badge, различие не только цветом | `tests/test_task_ui.py` + ручная визуальная проверка |
| AC-40 | R | Бюджет высоты ≤ ~150 px; на 1024×768 видно ≥ 3 сообщений, иначе fallback в сайдбар; проверка на обычной и уменьшенной ширине | Ручная визуальная проверка |
| AC-41 | UI | Create task: ошибки в диалоге, успех → TASK_CREATED и active_task, старая задача остаётся | `tests/test_task_ui.py` |
| AC-42 | UI | В Chat только действия из can_apply, stage напрямую не меняется | `tests/test_task_ui.py` |
| AC-43 | UI | Завершение: компактный итог, COMPLETED, Open full result (st.code), New task | `tests/test_task_ui.py` |
| AC-44 | UI | Diagnostics / Task: list + Open (active_task), индикатор и поля, timeline, artifacts, workflow read-only, packet preview, task usage | `tests/test_task_ui.py` |
| AC-45 | UI, R | Чат без задач показывает приглашение; поведение Дней 10–12 не изменено | `tests/test_task_ui.py` + ручная проверка |
| AC-46 | API-level (набор) | `test.bat` и `smoke_test.bat` проходят; существующие тесты не ослаблены; число тестов зафиксировано | `.\week-03\memory-state-agent\test.bat`, `.\week-03\memory-state-agent\smoke_test.bat` |
| AC-47 | R | README: состояние задачи, расширение Дня 12, UI, сценарий видео | Чтение `README.md` + ручная сверка |
| AC-48 | API | Live-сценарий SC-01 на реальном провайдере | Только с отдельного разрешения пользователя |

Уровень `API-level (набор)` у AC-46 означает «обязательный агрегирующий прогон»: он
подтверждается реальным запуском `test.bat` и `smoke_test.bat`.

## 3. Обязательный сквозной сценарий

Обязательный сценарий выполняется на уровнях I/F (автоматически) и R (вручную), порядок шагов:

1. **create** — создаётся задача, TASK_CREATED (AC-01).
2. **planning** — run planning, принимается план (AC-02, AC-04).
3. **pause** — PAUSE, status=paused, stage и expected action сохранены (AC-14).
4. **restart** — приложение полностью закрывается и запускается снова (AC-14; уровень R).
5. **resume** — RESUME возвращает ту же задачу и стадию, продолжение с текущего шага (AC-14, AC-15).
6. **execution** — выполняются шаги; STEP_COMPLETED создаёт ревизии (AC-05, AC-06).
7. **validation failed** — VALIDATION_FAILED с дефектами, переделка только дефектных шагов (AC-09, AC-10).
8. **execution** — переделка дефектных шагов, новая ревизия снимает ожидание переделки (AC-10).
9. **validation passed** — VALIDATION_PASSED, done/completed, final_result (AC-08).
10. **done** — компактный итог, badge COMPLETED, Open full result, New task (AC-43).

Сценарий считается пройденным, только если шаг 4 выполнен фактическим перезапуском приложения, а
не перезагрузкой страницы или rerun.

## 4. Критерии визуальной приёмки

- **Вертикальный индикатор стадий** (AC-39): planning, execution, validation, done идут
  столбиком с тонкой вертикальной линией; пройденные — opacity 0.45, font-size 0.92rem,
  line-through, символ ✓; текущая — opacity 1, font-size 1.10rem, font-weight 700, маркер ●;
  будущие — opacity 0.45–0.55, font-size 0.90rem, без line-through, маркер ○. Рядом badge
  RUNNING/PAUSED/BLOCKED/COMPLETED/CANCELLED. pause/blocked не меняют подсвеченную стадию.
- **Состояние не только цветом** (AC-39, NFR-07): различие обеспечивается контрастом, размером,
  начертанием, зачёркиванием, значком и текстовым badge.
- **Содержимое карточки**: под текущей стадией — `current_step` и ожидаемое действие; для
  blocked — причина и ожидаемое действие пользователя; длинный текст сокращается, полный
  открывается в модальном окне.
- **Бюджет высоты** (AC-40, NFR-11): ≤ ~150 px при 100% zoom (4 строки стадий ≤0.95rem включая
  badge, строка действий, одна строка деталей).
- **1024×768 и fallback** (AC-40): при таком окне видно не менее 3 сообщений истории; иначе
  карточка переносится в сайдбар (компактная секция над настройками чата).
- **Ручная проверка на обычной и уменьшенной ширине** (AC-40): обе ширины проверяются вручную,
  результат фиксируется в отчёте.
- **Chat с задачей** (AC-38): карточка — первая в `st.bottom`, строка профиля и `st.chat_input`
  сохраняют порядок, новых expander'ов в `app.main` нет, ключи виджетов начинаются с `task_`.

## 5. Критерий видео

- **Сценарий воспроизводим** (AC-47): README содержит пошаговый сценарий демонстрационного
  видео, каждому шагу соответствует наблюдаемый результат.
- **Сухой прогон выполнен**: сценарий выполнен без записи до фиксации кадров; выявленные
  расхождения исправлены.
- **Restart-safe Resume воспроизведён**: пауза, полное закрытие и повторный запуск приложения,
  затем resume с продолжением с текущего шага показаны в записи.
- **Видео записывает пользователь**: агент не записывает видео; он готовит сценарий и проверяет
  воспроизводимость.

## 6. Таблица трассировки AC → FR/NFR → тест/проверка

| AC | FR / NFR | Тест / проверка |
|---|---|---|
| AC-01 | FR-16, FR-01, FR-05, INV-01 | `tests/test_task_orchestrator.py`, `tests/test_task_storage.py` |
| AC-02 | FR-17, FR-03 | `tests/test_task_orchestrator.py` |
| AC-03 | FR-18 | `tests/test_task_orchestrator.py` |
| AC-04 | FR-18, FR-09 | `tests/test_task_orchestrator.py` |
| AC-05 | FR-19, FR-02, INV-05 | `tests/test_task_orchestrator.py`, `tests/test_task_storage.py` |
| AC-06 | FR-19, FR-22 | `tests/test_tasks.py`, `tests/test_task_orchestrator.py` |
| AC-07 | FR-20, INV-11 | `tests/test_task_orchestrator.py` |
| AC-08 | FR-21, INV-01 | `tests/test_task_orchestrator.py` |
| AC-09 | FR-21, FR-22, SC-02 | `tests/test_task_orchestrator.py` |
| AC-10 | FR-22, INV-11 | `tests/test_tasks.py`, `tests/test_task_storage.py` |
| AC-11 | FR-06, INV-09 | `tests/test_tasks.py` |
| AC-12 | FR-08 | `tests/test_tasks.py` |
| AC-13 | FR-09 | `tests/test_tasks.py` |
| AC-14 | FR-23, INV-02, INV-03 | `tests/test_task_storage.py` + R |
| AC-15 | FR-23, INV-03 | `tests/test_task_orchestrator.py`, `tests/test_task_storage.py` |
| AC-16 | FR-24, INV-02 | `tests/test_task_orchestrator.py` |
| AC-17 | FR-25, SC-05 | `tests/test_task_orchestrator.py` |
| AC-18 | FR-12, INV-04 | `tests/test_task_storage.py` |
| AC-19 | FR-12, INV-05 | `tests/test_task_storage.py`, `tests/test_tasks.py` |
| AC-20 | FR-12, INV-06 | `tests/test_task_storage.py` |
| AC-21 | FR-02, FR-03, FR-13, INV-07 | `tests/test_task_storage.py` |
| AC-22 | FR-11, FR-15, NFR-03 | `tests/test_task_storage.py` |
| AC-23 | FR-14, FR-11, INV-10 | `tests/test_task_storage.py` |
| AC-24 | FR-26, INV-08, SC-03 | `tests/test_task_orchestrator.py`, `tests/test_task_ui.py` |
| AC-25 | FR-26, FR-29 | `tests/test_task_orchestrator.py`, `tests/test_task_ui.py` |
| AC-26 | FR-17, FR-21, FR-26 | `tests/test_task_orchestrator.py` |
| AC-27 | FR-26, FR-17 | `tests/test_task_orchestrator.py` |
| AC-28 | FR-26, INV-05 | `tests/test_task_orchestrator.py` |
| AC-29 | FR-26 | `tests/test_task_orchestrator.py` |
| AC-30 | FR-27 | `tests/test_task_context.py` |
| AC-31 | FR-27 | `tests/test_task_context.py` |
| AC-32 | FR-27, NFR-10 | `tests/test_task_context.py`, `tests/test_task_orchestrator.py` |
| AC-33 | FR-29, FR-04 | `tests/test_task_orchestrator.py`, `tests/test_task_ui.py` |
| AC-34 | FR-30, NFR-04 | `tests/test_task_orchestrator.py` |
| AC-35 | FR-28, NFR-09 | `tests/test_task_context.py` |
| AC-36 | FR-31, INV-10, NFR-06 | `tests/test_task_storage.py`, `tests/test_task_orchestrator.py` |
| AC-37 | FR-32 | `tests/test_task_ui.py` |
| AC-38 | FR-33 | `tests/test_task_ui.py` |
| AC-39 | FR-33, NFR-07 | `tests/test_task_ui.py` + R |
| AC-40 | FR-38, NFR-11 | R (обычная и уменьшенная ширина) |
| AC-41 | FR-34, FR-16 | `tests/test_task_ui.py` |
| AC-42 | FR-35, FR-09, NFR-02 | `tests/test_task_ui.py` |
| AC-43 | FR-36 | `tests/test_task_ui.py` |
| AC-44 | FR-37, FR-15 | `tests/test_task_ui.py` |
| AC-45 | SC-06, FR-32 | `tests/test_task_ui.py` + R |
| AC-46 | NFR-09 | `.\week-03\memory-state-agent\test.bat`, `.\week-03\memory-state-agent\smoke_test.bat` |
| AC-47 | FR-39, NFR-06 | Чтение `README.md` + R |
| AC-48 | SC-01, LIVE | Реальный провайдер, только с разрешения пользователя |

## 7. Протокол тестирования и приёмки

1. Developer реализует этапы 1–10 плана и запускает точечные тесты, затем полный `test.bat` и
   `smoke_test.bat` уровня AC-46.
2. Tester независимо проверяет критерии из этого чек-листа: лично запускает команды, не
   ограничивается тестами Developer, проектирует собственные сценарии и не изменяет реализацию
   и постоянные тесты.
3. Tester выполняет обязательный сквозной сценарий раздела 3 и ручные проверки раздела 4.
4. Итог: `TEST_STATUS: PASS` только при всех проверенных критериях; иначе `TEST_STATUS: FAIL`
   с нарушенным критерием, ожидаемым и фактическим поведением и воспроизводимым сценарием;
   `TEST_STATUS: BLOCKED` — с указанием непроверенного и требуемого для проверки.
5. Live-проход уровня `API` (AC-48) выполняется отдельно, после явного разрешения пользователя;
   до него фиксируется `LIVE_API_STATUS: READY_FOR_MANUAL_ACCEPTANCE`, после успеха — `PASS`,
   при расхождении — `FAIL` с постоянным обезличенным regression-fixture без секретов и
   персональных данных.
6. Задача не принята при `TEST_STATUS: FAIL` или `TEST_STATUS: BLOCKED`. Агенты не выполняют
   `git commit` и `git push`; публикация — только после решения пользователя.
