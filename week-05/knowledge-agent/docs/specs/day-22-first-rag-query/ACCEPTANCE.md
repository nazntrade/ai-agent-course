# ACCEPTANCE — Day 22: первый RAG-запрос

Идентификатор задачи: `day-22-first-rag-query`.
Статус: **ожидает spec review**.
Дата: 2026-10-01.
Связанные документы: `SPEC.md` (требования R-01…R-20), `PLAN.md` (шаги P-01…P-10).

Каждый критерий трассируется к требованию `R-NN` (SPEC §20) и к шагу `P-NN` в `PLAN.md`.

---

## 1. Легенда уровней и правило «mock/stub ≠ LIVE»

Каноническая шкала уровней: **UNIT / INT / LIVE / MANUAL / UI**.

| Уровень | Что подтверждает | Изоляция |
| --- | --- | --- |
| **UNIT** | Логика ядра на моках/stub, без сети и `.env` | `test.bat unit` |
| **INT** | Реальные процессы сервиса + реальный HTTP loopback + `harness/embed_stub.py` и `harness/chat_stub.py` (без внешней сети) | `test.bat integration`, `smoke_test.bat` |
| **LIVE** | Embedding через Ollama проверяется независимо; генерация через выбранный полный chat-профиль либо явный standalone Ollama | opt-in (`test.bat live`; chat `MODEL_CHECK_KIND: LOCAL` или `NETWORK`) |
| **UI** | Фактическая проверка интерфейса в браузере | браузер |
| **MANUAL** | Ручной прогон пользователем/Developer/Tester | вручную |

**Правило:** mock/stub/fake **никогда** не маркируется как inference и не выдаётся за LIVE.
Unit-тест с fake/stub подтверждает логику, но не реальный контракт внешнего провайдера.

**Провайдерская граница (`finish_reason`/`done_reason`, usage, streaming, лимиты):**
Developer моделирует реальные граничные ответы (обрыв `length`, отсутствие usage, частичный
usage, ошибку провайдера) и закрепляет их постоянными regression-тестами; критерий считается
проверенным только после LIVE-проверки внешней границы Tester-ом на выбранном реальном chat-провайдере либо явном Ollama; embeddings проверяются отдельно.

---

## 2. Таблица критериев D22-01…D22-15

| ID | R (SPEC §20) | Критерий | Компонент | Уровень | Способ проверки | Порог |
| --- | --- | --- | --- | --- | --- | --- |
| D22-01 | R-01, R-02 | RAG-поток end-to-end: вопрос → `KnowledgeService.search` → ограниченный контекст с происхождением → отдельный `ChatModel` → ответ | `ChatService`, `ChatModel`, `POST /api/chat`, UI | UNIT + INT | INT: build индекса на stub, затем `with_rag`; сверить, что ответ получен, а `retrieval.passed` содержит реальные чанки | ответ непуст; `passed ⊆ found`; происхождение присутствует; retrieval D21 вызван ровно один раз |
| D22-02 | R-04 | `without_rag` без retrieval/embedding и без готового индекса | `ChatService`, режим | UNIT + INT | UNIT: счётчики вызовов fake-retrieval/embedder = 0; INT: запрос на пустой БД/без коллекции | 0 retrieval- и 0 embedding-вызовов; ответ получен |
| D22-03 | R-05 | `with_rag` требует ready-совместимый индекс; пиннинг; ошибки не маскируются | `ChatService`, `KnowledgeService.search` | UNIT + INT | UNIT+INT: без индекса → 409 `index_not_ready`; несовместимая identity → 409 `index_incompatible` до эмбеддинга; `index_version_id` в ответе | ошибка явная; ответа без RAG нет; id зафиксирован |
| D22-04 | R-06, R-09 | `found` и `passed` — разные поля; бюджет и отбор чанков | `ContextBudget`, `ChatAnswer` | UNIT + INT | UNIT: при тесном бюджете `passed < found`, `dropped_chunks>0`; INT: поля различаются и присутствуют | `passed ⊆ found`; оба поля заполнены; текст чанка не обрезан молча |
| D22-05 | R-02, R-03, R-16 | `ChatModel` контракт, таксономия ошибок, health, подсказка без автоскачивания | `ChatModel`, API, health | UNIT + INT + LIVE | UNIT: mock-transport; INT: `chat_stub` без модели; LIVE: реальная модель | коды/HTTP по SPEC §7.2; `chat_model_missing` с `hint`; автоскачивания нет; `health.chat` заполнен |
| D22-06 | R-15, R-19 | Внешняя граница провайдера: `finish_reason`/`length`, usage, streaming, лимиты | `ChatModel` provider adapter, `POST /api/chat/stream` | UNIT(fake) + LIVE | UNIT: модели реальных граничных ответов; LIVE: выбранный реальный chat-профиль либо явный Ollama | `length` → `truncated=true`; отсутствие/частичность usage не выдумывается; streaming даёт финальный usage; лимит → явная ошибка |
| D22-07 | R-09 | Оценка контекста, ограничения, переполнение; `lexical-v1` ≠ chat-токены | `ContextBudget` | UNIT | UNIT: `heuristic-v1`; `context_overflow` 422; `max_context_tokens` только уменьшает; `lexical-v1` не используется как бюджет | явная ошибка при переполнении; `budget_method` в ответе; молчаливой обрезки нет |
| D22-08 | R-10 | Недоверенные источники; валидация цитат | `prompts`, `citations` | UNIT + INT + LIVE | UNIT: инъекция в чанке → отдельный блок/роль, не системная инструкция; `[chunk_id]` вне `passed` → `unsupported` | `citations.valid` только из `passed`; `unsupported` фиксируется; склейки с system нет |
| D22-09 | R-08 | Паритет сравнения, раздельные пустые истории, без утечки, шаблоны в отчёте | `ChatService.compare`, `POST /api/chat/compare` | UNIT + INT + LIVE | UNIT: проверить входные messages каждой ветки и identity/options; INT: `compare` с stub; LIVE: 10 пар | одна модель/настройки; ответ A отсутствует в B; `comparison.prompt_templates` и `policy_differences` заполнены; отсутствие ready-индекса → 409 без частичного прогона |
| D22-10 | R-11 | Eval-набор 10 вопросов (9 отвечаемых + 1 неотвечаемый) вне индексируемого корпуса | `eval/d22/questions.json` | UNIT + MANUAL | UNIT: проверка схемы и счётчиков; MANUAL: вопросы прогнаны | ровно 10, один `answerable:false`; файл вне каталога индексируемого корпуса |
| D22-11 | R-13, R-14 | Экспорт прогона `chat-run-v1` и место хранения; новые endpoint | `FileChatRunStore`, API | UNIT + INT | UNIT: сериализация/чтение схем; INT: запись в TEMP `CHAT_RUNS_PATH`, `GET /api/chat-runs*` | файл `<run_id>.json` + `runs.jsonl`; поля SPEC §15.2; прогон неизменяем; 404 на отсутствующем |
| D22-12 | R-07 | UI: три действия, русский интерфейс, индикатор, ошибки, источники | `ui/*` | UI + MANUAL | Ручная проверка в браузере | видны «Без RAG», «С RAG», «Сравнить»; источники = `passed`; ошибки понятны; индикатор/поток присутствует |
| D22-13 | R-12 | Раздельная оценка retrieval/содержания/источников | `chat-eval-v1` | UNIT + MANUAL | UNIT: схема и валидация; MANUAL: заполнение по прогонам | три раздела независимы; ручная оценка хранится отдельно от неизменяемого прогона |
| D22-14 | R-01, R-17, R-18 | D21-регрессия и границы (нет MCP/ANN/памяти; TEMP-изоляция) | D21 API/сервис, тесты | UNIT + INT | Запуск существующих тестов D21 + проверка БД/путей | D21 API/индексы/происхождение не изменены; `KNOWLEDGE_DB_PATH`/`CHAT_RUNS_PATH` в TEMP; пользовательская БД не тронута |
| D22-15 | R-19, R-20 | Реальный запуск/UI, LIVE обоих режимов, 10 пар ответов, сценарий видео | `run_app.bat`, `harness/rag_eval.py --live` | LIVE + MANUAL + UI | Developer, затем независимо Tester | оба режима на реальной модели; 10 пар сохранены; фактические usage и tok/s или «н/д»; видео-сценарий воспроизводим |

### 2.1. Must / Later

* **Must** — D22-01…D22-15 и всё из SPEC §2.1.
* **Later** — SPEC §2.2: MCP, Wikipedia importer, ANN, reranker, OCR, многосессионная
  память, полный многошаговый чат, resume, удаление версий, multi-process/Postgres/Docker,
  автоматическое скачивание моделей.

### 2.2. Трассировка D22→P→R

Полная цепочка `D22-NN ↔ R-NN ↔ P-NN` (раздел SPEC и шаг PLAN — в SPEC §20 и PLAN §10):

| D22 | R | P (PLAN §2) |
| --- | --- | --- |
| D22-01 | R-01, R-02 | P-01, P-05 |
| D22-02 | R-04 | P-05 |
| D22-03 | R-05 | P-05, P-07 |
| D22-04 | R-06, R-09 | P-04, P-05 |
| D22-05 | R-02, R-03, R-16 | P-01, P-02, P-03, P-07 |
| D22-06 | R-15, R-19 | P-03 |
| D22-07 | R-09 | P-02, P-04 |
| D22-08 | R-10 | P-04 |
| D22-09 | R-08 | P-05 |
| D22-10 | R-11 | P-09 |
| D22-11 | R-13, R-14 | P-06, P-07 |
| D22-12 | R-07 | P-08 |
| D22-13 | R-12 | P-06, P-09 |
| D22-14 | R-01, R-17, R-18 | P-09 |
| D22-15 | R-19, R-20 | P-09, P-10 |

---

## 3. Разделение оценки (R-12, D22-13)

Оценка выполняется раздельно и независимо:

1. **Retrieval** — попали ли ожидаемые разделы/страницы в `found` и `passed`; `section_hit`,
   `page_hit`, доля релевантных среди переданных.
2. **Содержание** — присутствуют ли ожидаемые факты; отсутствие выдумывания при
   `answerable:false`.
3. **Источники** — соответствуют ли цитаты `passed`; сколько `unsupported`; корректно ли
   происхождение.

Ссылки: `chat-eval-v1` (SPEC §15.3), сводка `rag-eval-summary-v1` (SPEC §15.4).
Явно фиксируется: валидная цитата доказывает только факт передачи `chunk_id`, но не
смысловую правильность ответа.

---

## 4. Граничные и ошибочные сценарии

| Сценарий | Ожидаемое поведение | Уровень |
| --- | --- | --- |
| `with_rag` без ready-индекса | 409 `index_not_ready`; ответа без RAG нет | UNIT + INT |
| Несовместимая embedding-identity | 409 `index_incompatible` до эмбеддинга запроса | UNIT + INT |
| Ошибка retrieval/embedding (Ollama недоступен) | `embedding_*` 503 пробрасывается; без fallback на non-RAG | UNIT + INT |
| `without_rag` без индекса | ответ получен; retrieval/embedding не вызваны | UNIT + INT |
| chat-модель отсутствует | `chat_model_missing` 503 + `hint`; без автоскачивания | UNIT + INT + LIVE |
| chat-endpoint недоступен | `chat_unavailable` 503 | UNIT + INT |
| Таймаут chat | `chat_timeout` 503 | UNIT |
| Провайдер отверг длинный вход | `chat_length_error` 503; без молчаливой обрезки | UNIT + LIVE |
| Контекст не помещается (pre-call) | `context_overflow` 422; модель не вызывается | UNIT |
| Много чанков при тесном бюджете | `passed < found`, `dropped_chunks > 0`; не ошибка | UNIT |
| `done_reason = length` | `answer.truncated = true`, `finish_reason = "length"`; не ошибка | UNIT + LIVE |
| Отсутствие `prompt_eval_count`/`eval_count` | `usage = null`; `prompt_tokens_actual = null`; значения не выдумываются | UNIT + LIVE |
| Частичный usage | сохраняется как есть | UNIT + LIVE |
| Streaming недоступен | UI показывает индикатор и итоговый ответ; без падения | INT + UI |
| Prompt-injection в чанке | текст остаётся данными в отдельном блоке; инструкция не исполняется | UNIT + LIVE |
| Цитата на непереданный `chunk_id` | попадает в `citations.unsupported` | UNIT + INT |
| `compare` без ready-индекса | 409; частичный прогон не создаётся | UNIT + INT |
| Записи прогонов отсутствуют | `GET /api/chat-runs*` → пустой список/404 | UNIT + INT |
| Пользовательская БД | не затронута; тесты/harness работают в TEMP | INT |

---

## 5. Запуск и артефакты

* Доверенные точки входа: `.\week-05\knowledge-agent\setup.bat`, `test.bat`,
  `smoke_test.bat`, `run_app.bat` (содержимое `.bat` изменяет Configurator, не Developer).
* Режимы: `test.bat unit` (по умолчанию), `integration` (stub, TEMP), `live` (opt-in,
  реальный Ollama), `acceptance` (агрегатор). Проверяются фактическим выполнением.
* **OpenAPI 3.1**: FastAPI отдаёт OpenAPI 3.1 (SPEC §11 — единственный источник истины
  для новых путей и форм); отдельный snapshot-тест в D22 не предусмотрен.
* **Отсутствие секретов и абсолютных путей**: API/UI/README/логи/прогоны не содержат
  содержимого `.env`, ключей и абсолютных локальных путей. `citations`/источники содержат
  только санитизированные метаданные.
* Тесты не читают `.env` и изолированы от сети; INT использует `embed_stub`/`chat_stub`;
  LIVE — только opt-in. `KNOWLEDGE_DB_PATH` и `CHAT_RUNS_PATH` указывают в TEMP.

---

## 6. Измерения LIVE

При реальном прогоне измеряются и указываются:

* `input_tokens`/`output_tokens` (из usage Ollama) при наличии; иначе «н/д»;
* `output_tokens_per_second` из `eval_count`/`eval_duration`; иначе «н/д»;
* задержки `retrieval`/`context`/`chat`/`total`;
* `finish_reason` ответа;
* для 10 пар — сводка `rag-eval-summary-v1` и ручные оценки `chat-eval-v1`.

Токены проверяемой локальной модели не смешиваются с токенами OpenCode-агента; один вызов
не учитывается дважды. Output tokens и tok/s embedding не выдумываются (для embedding
неприменимы).

---

## 7. Итоговые статусы

* `UNIT_D22_STATUS`
* `INTEGRATION_D22_STATUS`
* `CHAT_LIVE_STATUS` (только при `MODEL_CHECK_KIND: LOCAL`; иначе описательно — не
  запускалось / MOCK)
* `RETRIEVAL_LIVE_STATUS`
* `RAG_EVAL_PAIRS_STATUS` (10 пар)
* `UI_D22_STATUS`
* `D21_REGRESSION_STATUS`
* `TEST_STATUS` (PASS только при проверке всех обязательных критериев на достаточном уровне;
  иначе FAIL/BLOCKED с указанием непроверенного)

Правило: если для обязательного критерия (LIVE внешней границы, UI, 10 пар) нет доступа
или разрешения, возвращается `BLOCKED` по этому критерию, а не полный PASS.

---

## 8. Регрессия D21

* Существующие `tests/unit` и `tests/integration` D21 остаются зелёными.
* `test.bat unit` и `test.bat integration` проходят без сети и `.env`.
* `smoke_test.bat` D21-сценарий продолжает работать (коллекции, build, дедуп, переключение
  active, поиск, сравнение, UI-assets).
* D21 API (`/api/search`, `/api/index/build`, `/api/compare`, `/api/collections`,
  `/api/index-versions/*`) не изменён по путям, параметрам и формам; происхождение и
  изоляция коллекций сохранены.
* Расширение `GET /api/health` блоком `chat` не ломает существующих потребителей.

---

## 9. Чек-лист видео/README

- [ ] README воспроизводим: шаги запуска и проверки D22 (русский; UI D22 — русский по
      требованию пользователя, существующие панели D21 — английские).
- [ ] Указано, что `CHAT_MODEL` — независимая настройка, а `embeddinggemma:300m` — только
      embedding; модели не скачиваются автоматически.
- [ ] Указаны фактические измеренные числа LIVE (usage, tok/s, задержки) или «н/д» с причиной.
- [ ] Описаны три действия UI, индикатор ожидания, источники и понятные ошибки.
- [ ] Отмечено, что корпус не в Git, путь задаёт пользователь, тесты изолированы от `.env`/сети.
- [ ] Отмечены границы: D21 не изменяется; MCP/ANN/reranker/память — Later.
- [ ] Видео-сценарий: запуск → вопрос без RAG → вопрос с RAG (источники) → сравнение →
      ошибка при отсутствии модели → сохранённые прогоны (SPEC/PLAN §7).
- [ ] Честно отмечено, что не проверено только чтением (версия Ollama для chat, реальные
      `done_reason`/usage/streaming, лимиты, качество retrieval, содержание ответов, UI,
      `.bat`, видео).

## Дополнение D22-PROFILE
D22-P01 (UNIT/INT): complete профиль выбирает chat provider; partial fail closed, секрет не выводится; standalone Ollama сохраняется.
D22-P02 (UNIT/INT/LIVE): lease acquire once, parent release finally success/error/interrupt, inherited children never release; release failure nonzero, shared runtime ownership preserved.
D22-P03 (UNIT/INT/LIVE): incremental token event before provider EOF, usage/finish_reason faithfully mapped.
D22-P04 (UNIT): without_rag overflow makes zero inference calls.
D22-P05 (LIVE/UI): выбранная local модель отвечает в двух режимах; эмбеддинг идентифицирован отдельно; реальные 10 пар и UI, local provenance, runtime освобождён без нарушения другого пользователя.

Поправка: реальный complete remote профиль удовлетворяет chat LIVE с MODEL_CHECK_KIND: NETWORK; LOCAL статусы не выставляются для remote. Acquire/release/renew секреты и lease ID не попадают в отчёт.


D22-P06 (UNIT/INT/LIVE): поддерживаемый local профиль ограничивает reasoning per request, не повышая output budget; remote/unknown capability не получают дополнительные поля; empty+length не подменяется ответом или успешным LIVE.

