# ACCEPTANCE — Day 23: реранкинг и фильтрация

Идентификатор задачи: `day-23-rag-filtering`.
Статус: **ожидает spec review**.
Дата: 2026-10-02.
Связанные документы: `SPEC.md` (требования R-01…R-18), `PLAN.md` (шаги P-01…P-14).

Каждый критерий D23-NN трассируется к требованию `R-NN` (SPEC §18), к шагу `P-NN`
(PLAN §2) и к замороженному acceptance-contract `D23-CNN` (D23-NN ↔ D23-CNN).

---

## 1. Легенда уровней и правило «mock/stub ≠ LIVE»

Каноническая шкала уровней: **UNIT / INT / LIVE / MANUAL / UI**.

| Уровень | Что подтверждает | Изоляция |
| --- | --- | --- |
| **UNIT** | Логика ядра на моках/fake, без сети и `.env` | `test.bat unit` |
| **INT** | Реальные процессы сервиса + реальный HTTP loopback + `harness/embed_stub.py` и `harness/chat_stub.py` (без внешней сети) | `test.bat integration`, `smoke_test.bat` |
| **LIVE** | Реальный retrieval через pull-embedding и генерация через выбранный полный chat-профиль либо явный standalone | `test.bat scenario d23-rag-filtering`; `MODEL_CHECK_KIND: NETWORK` для remote, `LOCAL` для local |
| **UI** | Фактическая проверка интерфейса в браузере | браузер через `run_app.bat` |
| **MANUAL** | Ручной прогон Developer/Tester | вручную |

**Правило:** mock/stub/fake **никогда** не маркируется как inference и не выдаётся за LIVE.
Unit-тест с fake/stub подтверждает логику, но не реальный контракт внешнего провайдера.

**Провайдерская граница** (`finish_reason`/`done_reason`, usage, streaming, лимиты):
Developer моделирует реальные граничные ответы (обрыв `length`, отсутствие usage, частичный
usage, ошибку провайдера) и закрепляет их постоянными regression-тестами; критерий считается
проверенным на достаточном уровне только после LIVE-проверки внешней границы Tester-ом на
выбранном реальном chat-провайдере. Embeddings проверяются отдельно и не выдаются за
генерацию.

Реальный выбранный профиль D23 — **remote** (`deepseek/deepseek-flash`, reasoning low),
поэтому LIVE маркируется `MODEL_CHECK_KIND: NETWORK`, а LOCAL-статусы не выставляются.

---

## 2. Таблица критериев D23-01…D23-15

| ID (контракт) | R (SPEC §18) | P (PLAN §2) | Критерий | Компонент | Уровень | Способ проверки | Порог/наблюдение |
| --- | --- | --- | --- | --- | --- | --- | --- |
| D23-01 (D23-C01) | R-01, R-02 | P-01, P-02, P-03, P-07 | После поиска применяется фильтр релевантности с настраиваемым порогом cosine; top-K настраивается до и после фильтрации | `RelevanceFilter`, `ChatService`, `ChatRequest`, config | UNIT + INT | `test.bat unit`, `test.bat integration`: менять `min_score`/`prefilter_top_k`/`postfilter_top_k` и наблюдать изменение `selected`/`passed` | при росте порога `passed` не растёт; `selected ⊆ candidates`; при `postfilter_top_k` меньше — `selected` усечён; правило слияния §7.1: D22-совместимый запрос без D23-полей → `prefilter=postfilter=legacy top_k`, `found_count == top_k`; `search` D21 не изменён |
| D23-02 (D23-C02) | R-03 | P-01, P-02, P-06, P-07, P-08, P-10 | Реализованы и сравниваются четыре режима A (plain RAG), B (+фильтр), C (+rewrite), D (+rewrite+фильтр); baseline — обычный RAG | `ChatService.compare_modes`, `POST /api/chat/compare-modes`, UI | UNIT + INT + UI | UNIT/INT: один индекс, одни настройки, пустая история → 4 различимые ветки; UI: панель сравнения | 4 ветки присутствуют; `comparison_kind="four_modes"`; `same_model` и `same_settings` true; baseline A = `with_rag` без фильтра/rewrite |
| D23-03 (D23-C03) | R-04 | P-01, P-05, P-06 | Rewrite переформулирует только поисковый запрос; генерация отвечает на исходный вопрос; оба запроса сохранены | `ChatQueryRewriter`, `ChatService`, trace | UNIT + INT | UNIT/INT: при `use_rewrite=true` `original_query != search_query`, а сообщение генерации содержит `original_query` | `original_query` и `search_query` в трассе; retrieval использует `search_query`; генерация — `original_query` |
| D23-04 (D23-C04) | R-05, R-17 | P-01, P-05, P-09 | Rewrite — короткий ограниченный вызов без эталонов/готовых ответов/истории соседних режимов; при ошибке fallback на исходный запрос с сохранённой причиной | `rewrite-v1`, `ChatQueryRewriter` | UNIT (+ LIVE граница провайдера) | UNIT на fake-`ChatModel`: ошибка/таймаут/пустой/`length` → fallback; проверка содержимого сообщений rewrite | промпт rewrite не содержит `expected_facts`, ответов, истории; `fallback=true`, `search_query=original_query`, `reason` непуст; ограничение длины применено |
| D23-05 (D23-C05) | R-06 | P-03, P-04, P-06, P-07, P-10 | Сохраняются original/search query, кандидаты, scores, выбранные и переданные чанки, причины исключения; различаются threshold/top-K/бюджет | trace в `chat-run-v1`, `trace-sample.json` | INT + LIVE | `test.bat integration` и LIVE-сценарий: чтение сохранённой трассы | `candidates` — массив; `exclusion_reasons` — объект с ключами `threshold`/`top_k`/`context_budget`; `passed ⊆ selected ⊆ candidates`; один `chunk_id` только в одной категории |
| D23-06 (D23-C06) | R-07 | P-06, P-09 | Если фильтр отсекает все чанки — понятное сообщение об отсутствии подходящих источников; режим не подменяется обычным RAG | `ChatService`, UI | UNIT + INT + UI | UNIT/INT: порог, отсекающий всё; UI: сообщение | `retrieval.passed=[]`; `insufficient_sources=true`; модель не вызвана (счётчик=0); повторный запрос без фильтра не выполняется |
| D23-07 (D23-C07) | R-08, R-18 | P-02, P-07, P-13 | Сохранены индексы, метаданные, происхождение, существующий поиск, «Без RAG», «С RAG» и сравнение D22; регрессии D21–D22 зелёные | D21/D22 API и сервис, тесты | UNIT + INT | `test.bat unit`, `test.bat integration`, `smoke_test.bat`: существующие D21–D22 сценарии | `/api/search`, `/api/index/*`, `/api/compare`, `/api/chat` без D23-полей, `/api/chat/compare` не изменились; все D21–D22 тесты проходят |
| D23-08 (D23-C08) | R-09 | P-09 | UI: большой чат сверху; понятные настройки отбора; показ использованного поискового запроса; отдельное сравнение четырёх режимов с ответами, источниками и метриками | `ui/*` | UI + MANUAL | `run_app.bat` и проверка в браузере | чат — первая и увеличенная панель; есть режим A–D, порог, top-K до/после; виден `search_query` (и fallback); сравнение 4 режимов с ответами/источниками/метриками работает; новые элементы D23 — англ. (`Retrieval settings`/`Search query`/`Trace`/`Compare four modes`), существующие русские панели D22 не переведены (SPEC §12.1) |
| D23-09 (D23-C09) | R-10, R-17 | P-05, P-06 | Расход и задержка rewrite учитываются отдельно от генерации и входят в общий результат | `ChatService` metrics, `RewriteResult` | UNIT + LIVE | UNIT: раздельные поля; LIVE: фактические значения из прогона | плоский `usage` = генерация (D22-совместимо), rewrite-метрики в `rewrite.usage`/`rewrite.latency_ms`; `latency_ms.chat` = генерация; `latency_ms.total` включает rewrite; значения не смешаны; отсутствие usage → `null` |
| D23-10 (D23-C10) | R-11 | P-08, P-11, P-12 | LIVE-оценка на 10 вопросах D22 во всех четырёх режимах: один закреплённый индекс, одинаковые настройки модели ответа, пустая история; результаты сохранены | `harness/d23_live.py`, `tests/scenarios/d23-rag-filtering.*` | LIVE | `test.bat scenario d23-rag-filtering` на выбранной remote-модели | 10×4=40 ответов ≥40; `modes` ровно 4; один `index_version_id`; `threshold` совпадает с калибровкой; `MODEL_CHECK_KIND: NETWORK` |
| D23-11 (D23-C11) | R-12 | P-11, P-12 | Порог откалиброван на отдельном небольшом наборе и зафиксирован до итогового сравнения | `eval/d23/calibration-questions.json`, `harness/d23_live.py` | LIVE/MANUAL | LIVE-сценарий + инспекция `calibration.json` | `calibration_set` — не 10 вопросов D22; `threshold` зафиксирован; `comparison.json.threshold == calibration.json.threshold` |
| D23-12 (D23-C12) | R-13 | P-12, P-14 | Раздельно оценены релевантность поиска, полнота фактов и подтверждение источников; показаны улучшения/ухудшения с вниманием к Q06/Q07/Q08 и вопросу вне корпуса (Q10) | `quality-assessment.md` | MANUAL (Developer пишет, Tester независимо проверяет) | Инспекция сохранённых 40 ответов и источников по трём разделам | три раздела независимы; зафиксированы улучшения и ухудшения; явно разобраны Q06/Q07/Q08/Q10; непустой ответ/chunk_id сами по себе не выданы за качество |
| D23-13 (D23-C13) | R-14 | P-12, P-14 | Если подход не даёт полезного улучшения — причина разобрана и исправлена; результат отражён | `quality-assessment.md`, повторный прогон | MANUAL + LIVE | Инспекция: разбор причины и внесённое исправление, подтверждённое повторным прогоном | при наличии улучшения — оно зафиксировано; при отсутствии — причина, исправление и подтверждающий повторный прогон |
| D23-14 (D23-C14) | R-15, R-17, R-18 | P-01, P-04, P-06, P-10, P-13 | Проверены ошибки, граничные настройки, пустой и обрезанный ответ, отмена и регрессии D21–D22 | tests, `ContextBudget`, `ChatService` | UNIT + INT (+ LIVE границы) | Точечные UNIT/INT сценарии; LIVE — границы провайдера; INT-отмена по §7.4; UNIT/INT регрессия `found_count == top_k` | порог 0/1; top-K 0/1; тесный бюджет; ошибка/таймаут chat; пустой и `finish_reason=length`; отмена (разрыв клиента: `done` не отправлен, сервер жив, нет фиктивной успешной записи, провайдерский stream закрыт); D21–D22 тесты зелёные |
| D23-15 (D23-C15) | R-16, R-17 | P-09, P-12 | Фактический запуск/перезапуск приложения и live-сценарий через доверенную точку входа | `run_app.bat`, `test.bat scenario d23-rag-filtering` | LIVE + UI + MANUAL | Developer, затем независимо Tester | `run_app.bat` реально запускается и UI доступен; live-сценарий выполняется; оба режима проверки (Developer и Tester) |

### 2.1. Must / Later

* **Must** — D23-01…D23-15 и всё из SPEC §2.1.
* **Later** — SPEC §2.2 и §19: MCP, Wikipedia importer, ANN/векторная БД, ML-reranker,
  OCR, многосессионная память, полный многошаговый чат, resume, удаление версий,
  multi-process/Postgres/Docker, автоматическое скачивание моделей.

### 2.2. Трассировка D23→R→P→контракт

| D23 | R | P | Контракт |
| --- | --- | --- | --- |
| D23-01 | R-01, R-02 | P-01, P-02, P-03, P-07 | D23-C01 |
| D23-02 | R-03 | P-01, P-02, P-06, P-07, P-08, P-10 | D23-C02 |
| D23-03 | R-04 | P-01, P-05, P-06 | D23-C03 |
| D23-04 | R-05, R-17 | P-01, P-05, P-09 | D23-C04 |
| D23-05 | R-06 | P-03, P-04, P-06, P-07, P-10 | D23-C05 |
| D23-06 | R-07 | P-06, P-09 | D23-C06 |
| D23-07 | R-08, R-18 | P-02, P-07, P-13 | D23-C07 |
| D23-08 | R-09 | P-09 | D23-C08 |
| D23-09 | R-10, R-17 | P-05, P-06 | D23-C09 |
| D23-10 | R-11 | P-08, P-11, P-12 | D23-C10 |
| D23-11 | R-12 | P-11, P-12 | D23-C11 |
| D23-12 | R-13 | P-12, P-14 | D23-C12 |
| D23-13 | R-14 | P-12, P-14 | D23-C13 |
| D23-14 | R-15, R-17, R-18 | P-01, P-04, P-06, P-10, P-13 | D23-C14 |
| D23-15 | R-16, R-17 | P-09, P-12 | D23-C15 |

---

## 3. Сохраняемые артефакты (из замороженного контракта)

| Artifact id | Путь | Формат | Кол-во | Обязательные assertions |
| --- | --- | --- | --- | --- |
| `d23-trace` | `week-05/knowledge-agent/local-data/d23/trace-sample.json` | json | 1..1 | `/original_query` exists; `/search_query` exists; `/candidates` type array; `/exclusion_reasons` type object |
| `d23-comparison` | `week-05/knowledge-agent/local-data/d23/comparison.json` | json | 1..1 | `/schema_version` equals `d23-comparison-v1`; `/threshold` type number; `/modes` type array; `/modes` minItems 4; `/modes` maxItems 4; `/answers` type array; `/answers` minItems 40 |
| `d23-calibration` | `week-05/knowledge-agent/local-data/d23/calibration.json` | json | 1..1 | `/schema_version` equals `d23-calibration-v1`; `/threshold` type number; `/calibration_set` type array; `/calibration_set` minItems 1 |
| `d23-quality` | `week-05/knowledge-agent/local-data/d23/quality-assessment.md` | text | 1..1 | — |

Артефакты сохраняются Developer-ом и сохраняются между прогонами; Tester читает
фактическое содержимое (не только факт существования) и даёт `artifact`-ссылки на каждый
совпавший файл. `local-data/` в Git не попадает — это не освобождает от сохранения и
предъявления артефактов.

---

## 4. Граничные и ошибочные сценарии

| Сценарий | Ожидаемое поведение | Уровень |
| --- | --- | --- |
| Порог `0.0` | отсечение по порогу выключено; `selected` = первые `postfilter_top_k` кандидатов | UNIT |
| Порог `1.0` | обычно `selected=[]`; понятное сообщение, без подмены RAG | UNIT + INT |
| `postfilter_top_k > prefilter_top_k` | `invalid_request` 422; retrieval не выполняется | UNIT |
| `min_score` вне `[0,1]` | `invalid_threshold` 422 | UNIT |
| Несогласованный `rag_mode` и boolean-поля | `invalid_request` 422 | UNIT |
| top-K `0`/`1` | `0` → `invalid_request`; `1` → один кандидат/выбранный | UNIT |
| D22-совместимый `with_rag` с `top_k`, без D23-полей | `prefilter=postfilter=legacy top_k`, `search` вызван с `top_k=top_k`; stub с ровно `top_k` кандидатами → `found_count == top_k`; режим A ≡ D22 `with_rag` | UNIT + INT |
| Тесный бюджет контекста | `passed < selected`, `exclusion_reasons.context_budget` непуст; не ошибка | UNIT + INT |
| Инструкции+вопрос не помещаются | `context_overflow` 422; модель не вызвана | UNIT |
| Ошибка/таймаут rewrite | fallback на исходный запрос, `reason` сохранён; HTTP-ошибки нет | UNIT + INT |
| Rewrite дольше `RAG_REWRITE_TIMEOUT_SECONDS` | `chat_timeout` → fallback; генерация отвечает | UNIT + INT |
| Пустой ответ rewrite или `finish_reason=length` | `rewrite_invalid` → fallback | UNIT |
| `use_filter=true`, фильтр отсекает всё | `passed=[]`, детерминированное сообщение, генерация не вызвана, record `usage=null`, `latency_ms.chat=null`, `answer.insufficient_sources=true`; режим не подменяется | UNIT + INT + UI |
| Ошибка/таймаут/`chat_model_missing` генерации | `chat_*` 503, без маскировки | UNIT + INT |
| `finish_reason=length` генерации | `answer.truncated=true`; не ошибка | UNIT + LIVE |
| Отмена пользователем (разрыв клиента в streaming, §7.4) | `GeneratorExit` не превращается в `error`; `done` не отправляется; провайдерский stream закрыт; при чистом разрыве нет фиктивной успешной записи; сервер жив и обслуживает повторный запрос | INT |
| `with_rag`/B/C/D без ready-индекса | 409 `index_not_ready`; ответа без RAG нет | UNIT + INT |
| Несовместимая embedding-identity | 409 `index_incompatible` до эмбеддинга запроса | UNIT + INT |
| `compare-modes` без ready-индекса | 409; частичного прогона нет | UNIT + INT |
| Регрессия D21–D22 | существующие UNIT/INT/smoke зелёные; D22-контракт чата сохранён | UNIT + INT |
| Пользовательская БД | не затронута; тесты/harness работают в TEMP | INT |

---

## 5. Раздельная оценка качества (D23-12)

Оцениваются независимо (0/1/2, как D22 §15.3):

1. **Retrieval** — попали ли ожидаемые разделы/страницы/чанки в `candidates`/`selected`/
   `passed`; сравнение A/B/C/D; особое внимание Q06/Q07/Q08 (пропущенные источники).
2. **Содержание** — присутствие ожидаемых фактов; отсутствие выдумывания при
   `answerable:false` (Q10).
3. **Источники** — валидность цитат по `passed`, число `unsupported`, корректность
   происхождения.

Явно фиксируется: валидная цитата доказывает только факт передачи `chunk_id`, но не
смысловую правильность ответа. Непустой ответ, существующий `chunk_id` и успешная команда
сами по себе не доказывают качество. Протокол сохраняется в `quality-assessment.md` и
независимо проверяется Tester-ом.

---

## 6. Измерения LIVE

При реальном прогоне измеряются и указываются:

* `input_tokens`/`output_tokens`/`total_tokens` генерации — в плоском `usage`; rewrite — в
  `rewrite.usage`; при наличии, иначе `null`/«н/д»;
* generation latency — `latency_ms.chat`; rewrite latency — `rewrite.latency_ms`;
  `latency_ms.{retrieval,context,total}` (`total` включает rewrite);
* `finish_reason` ответов (rewrite и генерации); `truncated`;
* `MODEL_CHECK_KIND: NETWORK` для выбранного remote-профиля (`deepseek/deepseek-flash`);
* для 40 ответов — агрегаты по секциям (retrieval/facts/sources) и записи `comparison.json`.

Токены проверяемой модели не смешиваются с токенами OpenCode-агента; один вызов не
учитывается дважды. Output tokens и tok/s embedding не выдумываются.

---

## 7. Итоговые статусы

* `D23_UNIT_STATUS`
* `D23_INTEGRATION_STATUS`
* `D23_CALIBRATION_STATUS`
* `D23_COMPARISON_STATUS` (40 ответов)
* `D23_UI_STATUS`
* `D21_D22_REGRESSION_STATUS`
* `D23_QUALITY_STATUS` (MANUAL; раннер оставляет `NOT_ASSESSED`)
* `TEST_STATUS` (PASS только при проверке всех обязательных критериев на достаточном
  уровне; иначе FAIL/BLOCKED с указанием непроверенного)

Правило: если для обязательного критерия (LIVE-граница, UI, 40 ответов, MANUAL-качество)
нет доступа/разрешения, возвращается `BLOCKED` по этому критерию, а не полный PASS.
Технический `TEST_STATUS` не заменяет общую приёмку проекта.

---

## 8. Регрессия D21–D22

* Существующие `tests/unit` и `tests/integration` D21–D22 остаются зелёными.
* `test.bat unit` и `test.bat integration` проходят без сети и `.env`.
* `smoke_test.bat` D21/D22-сценарий продолжает работать (коллекции, build, дедуп,
  переключение active, поиск, сравнение, UI-assets, D22-чат).
* D22 API (`/api/search`, `/api/index/build`, `/api/compare`, `/api/collections`,
  `/api/index-versions/*`, `/api/chat`, `/api/chat/stream`, `/api/chat/compare`,
  `/api/chat-runs*`) не изменён по путям, параметрам и формам при незаданных D23-полях.
* D21/D22 индексы и происхождение: повторная сборка того же корпуса не создаёт новый
  `ready` индекс; `chat-run-v1`/`chat-eval-v1` совместимы.
* D22-совместимый `/api/chat` (mode/`with_rag`, `top_k`, без D23-полей) сохраняет плоский
  `usage` (`record['usage']['input_tokens']`, совместимо с
  `tests/integration/test_selected_profile_end_to_end.py:76`) и `latency_ms.chat`; UI
  читает `record.usage.input_tokens`/`output_tokens`. `retrieval.found`/`found_count` не
  меняются (`found_count == top_k` при stub с `top_k` кандидатами).
* Baseline D23 (режим A) семантически равен D22 `with_rag` и не равен «Без RAG».

---

## 9. Чек-лист документации/реализации

- [ ] `SPEC.md`, `PLAN.md`, `ACCEPTANCE.md` прошли spec review (до реализации).
- [ ] D23-поля опциональны; дефолты выключены; поведение D22 сохранено.
- [ ] Четыре режима A–D реализованы и сравниваются через API/UI.
- [ ] Трасса сохраняет запросы, кандидатов, scores, selected/passed и причины исключения.
- [ ] Rewrite изолирован и имеет fallback с причиной; метрики rewrite отдельны: плоский
      `usage` = генерация; `rewrite.usage`/`rewrite.latency_ms`; `latency_ms.total` включает
      rewrite (совместимо с `test_selected_profile_end_to_end.py:76` и UI `record.usage`).
- [ ] Rewrite ограничен отдельным `ChatModel` с `RAG_REWRITE_TIMEOUT_SECONDS`; граница
      таймаута проверена UNIT/INT.
- [ ] D22-совместимый запрос без D23-полей сохраняет legacy `top_k` (`found_count == top_k`).
- [ ] Пустой `selected` даёт понятное сообщение без подмены RAG; record `usage=null`,
      `latency_ms.chat=null`, `answer.insufficient_sources=true`.
- [ ] Отмена SSE не маскируется успехом/`internal_error`; провайдерский stream закрыт;
      INT-проверка отмены выполнена.
- [ ] Новые элементы D23 — англ., существующие русские панели D22 не переведены.
- [ ] Калибровка зафиксирована отдельным набором до сравнения.
- [ ] `local-data/d23/{calibration,comparison,trace-sample}.json` и
      `quality-assessment.md` сохранены Developer-ом и предъявлены Tester-у.
- [ ] LIVE: 10×4 ответов, один индекс, одинаковые настройки, пустая история.
- [ ] Честно отмечено, что не проверено только чтением (реальные usage/`finish_reason`,
      streaming, качество retrieval/ответов, UI, `.bat`).

---

## 10. Открытые вопросы

* Конкретный порог фиксируется по результату калибровки на реальном индексе; в SPEC/PLAN
  он оставлен параметром.
* Улучшение от фильтра/rewrite не гарантировано: если полезного улучшения нет, D23-13
  требует разбора причины и исправления с повторным прогоном, а не ослабления критерия.
* Выбранный remote-профиль и его доступность проверяются LIVE-сценарием; базовые
  UNIT/INT остаются offline.
