# PLAN — Day 23: реранкинг и фильтрация

Идентификатор задачи: `day-23-rag-filtering`.
Статус: **ожидает spec review** (план реализации; код, тесты и `.bat` этим документом не
создаются).
Дата: 2026-10-02.
Связанные документы: `SPEC.md` (требования R-01…R-18), `ACCEPTANCE.md` (критерии
D23-01…D23-15, соответствующие D23-C01…D23-C15).

План трассируем до требований SPEC и acceptance criteria. Порядок шагов выбран от
устойчивого ядра к внешним деталям: контракты → конфиг → фильтр/бюджет → rewrite →
сервис → API → UI → harness/eval → тесты → документация.

---

## 1. Файловая структура (предлагаемая)

Прирост к структуре D22 (PLAN D22 §1); существующие retrieval/индексация и D22-чат не
переписываются, а расширяются обратно совместимо.

```
week-05/knowledge-agent/
  knowledge_agent/
    domain/
      contracts.py           # + QueryRewriter, RewriteResult, RagMode-хелперы
    config.py                # + RAG_*/D23_* настройки (SPEC §10)
    chat/
      filtering.py           # НОВЫЙ: RelevanceFilter (порог, postfilter_top_k, причины)
      rewrite.py             # НОВЫЙ: ChatQueryRewriter (короткий ограниченный вызов)
      context.py             # + chunk_id отброшенных по бюджету (совместимо с D22)
      chat_service.py        # + D23-трасса, режимы A–D, compare_modes, пустой selected
      prompts.py             # + rewrite-v1 (без изменений plain-v1/rag-v1)
    api/
      routes.py              # + D23-поля ChatRequest, POST /api/chat/compare-modes
    __main__.py              # + сборка QueryRewriter в ChatService
    ui/
      index.html             # + большой чат, настройки отбора, панель 4 режимов
      app.js                 # + режимы A–D, search_query, сравнение четырёх режимов
      styles.css             # + стили настроек и 4-колоночного сравнения
  eval/
    d23/
      calibration-questions.json   # d23-calibration-questions-v1 (малый отдельный набор)
  harness/
    chat_stub.py             # + детерминированный rewrite и границы (совместимо с D22)
    d23_eval.py              # offline-агрегатор D23 (трасса/сравнение без сети)
    d23_live.py              # LIVE-раннер: калибровка + 40 ответов + трасса
  tests/
    scenarios/
      d23-rag-filtering.py   # тонкий PRODUCT-сценарий → harness.d23_live.main
      d23-rag-filtering.json # {"schema_version":"test-scenario-v1","kind":"live"}
    unit/                    # + test_d23_filtering, test_d23_rewrite, test_d23_context,
                             #   test_d23_chat_service, test_d23_api, test_d23_eval
    integration/             # + test_d23_end_to_end (embed_stub + chat_stub), D22-регрессия
  README.md                  # + раздел «Day 23» (обновляет Coordinator после реализации)
```

`local-data/d23/` — каталог артефактов (уже под `local-data/`, в Git не попадает).

Имена модулей уточняются на реализации; контракты и границы — обязательны.

---

## 2. Порядок работ P-01…P-13

| ID | Шаг | Артефакты | Требование | Уровень |
| --- | --- | --- | --- | --- |
| P-01 | D23-контракты и ошибки | `domain/contracts.py`, `domain/errors.py` | SPEC §5, §6, §8; R-01…R-05 | UNIT |
| P-02 | Конфигурация `RAG_*`/`D23_*` | `config.py`, `.env.example` | SPEC §10; R-01, R-03, R-05 | UNIT |
| P-03 | `RelevanceFilter` | `chat/filtering.py` | SPEC §7.1; R-01, R-06 | UNIT |
| P-04 | Расширение `ContextBudget` (chunk_id отброшенных) | `chat/context.py` | SPEC §6.3, §7.1; R-06 | UNIT |
| P-05 | `QueryRewriter` + `rewrite-v1` + отдельный rewrite-`ChatModel` (timeout/лимит) | `chat/rewrite.py`, `chat/prompts.py` | SPEC §6.2, §7.2; R-04, R-05 | UNIT |
| P-06 | `ChatService`: трасса, режимы A–D, `compare_modes`, пустой `selected`, слияние top-K, метрики, отмена/закрытие stream | `chat/chat_service.py` | SPEC §5.3, §6.3–6.4, §7.1–7.4, §9; R-03, R-04, R-06, R-07, R-10 | UNIT |
| P-07 | HTTP API: D23-поля, слияние legacy top-K и `/api/chat/compare-modes` | `api/routes.py` | SPEC §7.1, §11; R-01, R-03, R-06 | UNIT + INT |
| P-08 | Сборка приложения: базовый `ChatModel` + отдельный rewrite-`ChatModel` и `ChatQueryRewriter` | `__main__.py` | SPEC §5, §7.2; R-03, R-04 | UNIT |
| P-09 | UI: большой чат, настройки, search query, 4-режимное сравнение | `ui/index.html`, `ui/app.js`, `ui/styles.css` | SPEC §12; R-09 | UI + MANUAL |
| P-10 | Harness: stub rewrite/границы, offline-агрегатор | `harness/chat_stub.py`, `harness/d23_eval.py` | SPEC §16; R-03, R-06, R-15 | INT |
| P-11 | Калибровочный набор | `eval/d23/calibration-questions.json` | SPEC §13.3; R-12 | MANUAL |
| P-12 | LIVE-раннер и сценарий | `harness/d23_live.py`, `tests/scenarios/d23-rag-filtering.{py,json}` | SPEC §13; R-11…R-17 | LIVE |
| P-13 | Тесты по уровням, отмена SSE и регрессия | `tests/unit/*`, `tests/integration/*` | SPEC §7.1, §7.4, §16; R-15 | UNIT + INT |
| P-14 | Документация Day 23 | `README.md` | MODULE_RULES §6; R-08, R-09 | MANUAL (Coordinator) |

Зависимости: P-01 → P-02; P-01 → P-03 → P-04 → P-06; P-01 → P-05 → P-06; P-06 → P-07 → P-08;
P-07 → P-09; P-03/P-06 → P-10; P-11 → P-12; P-06/P-07 → P-13; всё → P-14.

---

## 3. Ключевые решения

* **HTTP-контракт и схемы артефактов — только в SPEC §11 и §13.** PLAN и код ссылаются на
  SPEC и не дублируют пути и схемы.
* **Обратная совместимость**: D23-поля опциональны, дефолты выключены
  (`RAG_FILTER_ENABLED=0`, `RAG_REWRITE_ENABLED=0`); режим A семантически равен D22
  `with_rag`; `without_rag` и D22 `compare` не затронуты.
* **`KnowledgeService.search` не меняется**: фильтр применяется над уже возвращёнными
  кандидатами в `ChatService`; top-K до фильтрации — это `top_k` аргумент `search`.
* **`RelevanceFilter` — чистая детерминированная функция**: только `score`/`rank`/`chunk_id`;
  тестируется без сети.
* **`QueryRewriter` — контракт ядра**; `ChatQueryRewriter` зависит от `ChatModel`
  (тот же выбранный профиль, но **отдельный экземпляр** с коротким timeout/лимитом — §7.2),
  возвращает `RewriteResult`; ядро не знает деталей Ollama/OpenAI-compatible.
* **Rewrite-промпт изолирован**: только инструкция + вопрос; эталоны/ответы/история не
  передаются (проверяется UNIT-тестом на содержимое сообщений).
* **Пустой `selected`** → детерминированное сообщение сервиса без вызова модели;
  record `usage=null`, `latency_ms.chat=null`, `answer.insufficient_sources=true`; повторный
  запрос без фильтра запрещён.
* **Раздельные метрики (F1)**: плоский record-level `usage` = usage генерации (форма D22);
  rewrite-метрики — в `rewrite.usage`/`rewrite.latency_ms`; `latency_ms.chat` = генерация;
  `latency_ms.total` включает rewrite. Совместимо с чтением `record['usage']['input_tokens']`
  (`tests/integration/test_selected_profile_end_to_end.py:76`) и `ui/app.js`.
* **Слияние top-K (F2)**: D22-совместимый запрос без D23-полей сохраняет legacy
  `top_k`/`CHAT_TOP_K` (`prefilter=postfilter=legacy`), поэтому режим A ≡ D22 `with_rag`;
  `RAG_*_TOP_K` применяются только к явно D23-запросам и `/api/chat/compare-modes`.
* **Rewrite timeout (F3, вариант «б»)**: отдельный экземпляр `ChatModel` в `__main__.py`
  (`build_chat_service`) с `timeout=RAG_REWRITE_TIMEOUT_SECONDS` и коротким
  `max_output_tokens=RAG_REWRITE_MAX_OUTPUT_TOKENS`; контракт `ChatModel` D22 и адаптеры
  `openai_chat`/`ollama_chat` не меняются.
* **Отмена (F4)**: `stream_events` не ловит `GeneratorExit` широким `except Exception`;
  провайдерский `stream_chat` закрывается в `finally`; чистая отмена не сохраняет фиктивную
  успешную запись, сервер остаётся работоспособным.
* **Артефакты**: `local-data/d23/{calibration,comparison,trace-sample}.json` и
  `quality-assessment.md`; `comparison.json` использует порог из `calibration.json`.
* **Порог не подстраивается под 10 вопросов D22**: отдельный малый калибровочный набор.
* **Модели не скачиваются**; секреты, base URL и lease ID не публикуются.

---

## 4. Тесты по уровням и фикстуры

### 4.1. UNIT (без сети и `.env`)

* `RelevanceFilter`:
  * порог `0.0` — ничего не отсекает; порог `1.0` — обычно пустой `selected`;
  * `selected` отсортирован по `score` убыв., стабилен при равенстве;
  * `exclusion_reasons.threshold`/`top_k` корректны; один `chunk_id` только в одной
    категории;
  * `postfilter_top_k` меньше `prefilter_top_k`; пустой вход; ровно `postfilter_top_k`.
* `ChatQueryRewriter` на fake-`ChatModel`:
  * обычный rewrite: `search_query` отличается, `used=true`, latency/usage сохранены;
  * ошибка/таймаут/пустой ответ/`finish_reason=length` → `fallback=true`,
    `search_query=original_query`, `reason` заполнен;
  * в промпт rewrite не попадают `expected_facts`, готовые ответы, история режимов;
  * ограничение длины и отдельный таймаут применяются.
* Граница таймаута rewrite (F3): rewrite-`ChatModel` с mock transport/opener — в транспорт
  передан именно `RAG_REWRITE_TIMEOUT_SECONDS`, а `ChatTimeout` из rewrite-модели даёт
  `fallback` с причиной `chat_timeout`; контракт `ChatModel` D22 не изменён.
* Расширенный `ContextBudget`: `dropped` содержит `chunk_id`; поведение D22 не изменилось.
* `ChatService`:
  * режимы A/B/C/D дают различимые трассы и правильные `selected`/`passed`;
  * `compare_modes`: одна identity/настройки, раздельные пустые истории, 4 ветки,
    `comparison.threshold`/`prefilter_top_k`/`postfilter_top_k`;
  * `use_filter=true` и пустой `selected` → детерминированное сообщение, модель не вызвана
    (счётчик вызовов fake-chat = 0), `insufficient_sources=true`, record `usage=null` и
    `latency_ms.chat=null`;
  * `use_rewrite=false` → rewrite не вызывается (счётчик = 0);
  * слияние top-K (F2): D22-совместимый запрос (`with_rag`, без D23-полей) →
    `prefilter=postfilter=legacy top_k`, `search` вызван с `top_k=top_k`,
    `found_count == len(fragments)` (на stub ровно `top_k` → `found_count == top_k`);
  * `mode=without_rag` игнорирует D23-поля: ровно один вызов модели, роли `system`/`user`,
    шаблон `plain-v1` (существующий UNIT `test_chat_service` сохраняется);
  * раздельные метрики: плоский `usage` = генерация, rewrite в `rewrite.usage`/`latency_ms`,
    `latency_ms.total` включает rewrite;
  * `passed ⊆ selected ⊆ candidates`; `retrieval.found == candidates`;
  * валидация: `min_score` вне `[0,1]` → `invalid_threshold`; `postfilter_top_k >
    prefilter_top_k` → `invalid_request`; несогласованный `rag_mode` → `invalid_request`.
* API-валидация новых полей (Pydantic) и форма ошибок.

### 4.2. INT (реальные процессы, loopback; без внешней сети)

* `harness/embed_stub.py` + расширенный `harness/chat_stub.py`:
  * полный D23-поток в каждом из режимов A/B/C/D через `POST /api/chat`;
  * `POST /api/chat/compare-modes` — четыре ветки, шаблоны/политики в `comparison`;
  * `POST /api/chat/stream` с D23-полями; `sources` несёт `search_query` и
    `selected_count`;
  * пустой `selected` при высоком пороге → `retrieval.passed=[]`, сообщение, ответ не
    подменяется;
  * rewrite fallback от stub (`chat_stub` возвращает ошибку/пустой ответ) с сохранённой
    причиной;
  * INT-граница rewrite (F3): `chat_stub` задерживает rewrite-ответ дольше короткого
    `RAG_REWRITE_TIMEOUT_SECONDS` → fallback на исходный запрос, генерация отвечает;
  * D22-совместимый `/api/chat` с `top_k` и без D23-полей → `retrieval.found`/`found_count
    == top_k` (stub возвращает `top_k` кандидатов), `search` получил `top_k=top_k`;
  * отмена (F4): открыть `/api/chat/stream`, прочитать `start` (и, при наличии, первый
    `token`), закрыть клиентское соединение до `done`; сервер жив, повторный запрос
    успешен, для прерванного `run_id` нет записи с непустым `answer`, провайдерский stream
    закрыт;
  * D21–D22 регрессия: `/api/search`, `/api/index/build`, `/api/compare`, `/api/collections`,
    `/api/chat` без D23-полей, `/api/chat/compare` работают как раньше.
* Изоляция: `KNOWLEDGE_DB_PATH` и `CHAT_RUNS_PATH` в TEMP; пользовательская БД не
  затрагивается.
* Offline `harness/d23_eval.py` формирует трассу/сравнение без сети (для fixture и
  regression).

### 4.3. Фикстуры

* `harness/chat_stub.py` — детерминированный rewrite (распознаёт rewrite-промпт и
  возвращает короткую переформулировку), плюс инъекция: `error`, `timeout`, пустой ответ,
  `finish_reason=length`.
* `eval/d23/calibration-questions.json` — малый отдельный набор (≥1 вопрос) с ожидаемыми
  релевантными разделами/чанками.
* `eval/d22/questions.json` — существующие 10 вопросов (не изменяются).
* Корпус `agents-survey.pdf` в Git не попадает; путь задаёт пользователь; для INT
  используются синтетические TXT/MD из D21.

---

## 5. Harness и режимы `.bat` (создаёт Configurator)

Точки входа и их содержимое изменяет только Configurator (GOVERNANCE). Developer
реализует модули и PRODUCT-сценарий, на которые ссылаются bat. D23 использует
существующие точки входа и уже заявленный режим сценария:

* `test.bat unit` / `integration` — без сети и `.env`.
* `test.bat scenario d23-rag-filtering` — доверенный сценарий (`kind: live`), запускает
  `harness/d23_live.py`: policy проверяется до реальных действий; выбранный `AI_TEST_MODEL_*`
  профиль и `TestSession` управляют lease; backend — на своём свободном loopback-порту с
  TEMP-БД; артефакты пишутся в `local-data/d23/`.
* `smoke_test.bat` — INT-сценарий (stub).
* `run_app.bat` — фактический запуск UI с панелью D23.

Правило: LIVE не запускается автоматически; `MODEL_CHECK_KIND` не выставляется без
реального вызова. Dot-файлы `.bat` не изменяются этой задачей.

---

## 6. План калибровки порога (SPEC §13.3)

1. `eval/d23/calibration-questions.json` (`d23-calibration-questions-v1`) — отдельный малый
   набор (не 10 вопросов D22), вне индексируемого корпуса; для каждого вопроса указаны
   ожидаемые релевантные разделы/чанки.
2. `harness/d23_live.py` на закреплённом индексе выполняет retrieval по калибровочным
   вопросам с реальным embedder и собирает `candidate_scores` и релевантность.
3. Перебираются `candidate_thresholds` (например, 0.40…0.70); для каждого считаются
   `kept`, `kept_relevant`, `dropped_relevant`, `precision`, `recall`.
4. Выбирается порог по зафиксированному правилу: наибольший порог, который сохраняет
   хотя бы один релевантный чанк для каждого калибровочного вопроса (фильтр активен, а не
   пропускает всех кандидатов); если такого нет — наибольший порог без `dropped_relevant`,
   иначе порог с наименьшим `dropped_relevant`. Порог записывается в `calibration.json`
   вместе с `selection_reason`.
5. Порог фиксируется **до** итогового сравнения: `comparison.json.threshold` берётся из
   `calibration.json.threshold`, а не подбирается по 10 вопросам D22.

---

## 7. План сохранения артефактов (точные пути)

| Артефакт | Путь | Схема/формат | Кто создаёт | Уровень |
| --- | --- | --- | --- | --- |
| Калибровка | `week-05/knowledge-agent/local-data/d23/calibration.json` | `d23-calibration-v1` (JSON) | `harness/d23_live.py` | LIVE |
| Сравнение | `week-05/knowledge-agent/local-data/d23/comparison.json` | `d23-comparison-v1` (JSON) | `harness/d23_live.py` | LIVE |
| Трасса | `week-05/knowledge-agent/local-data/d23/trace-sample.json` | `d23-trace-v1` (JSON) | `harness/d23_live.py` (или offline) | LIVE/INT |
| Оценка | `week-05/knowledge-agent/local-data/d23/quality-assessment.md` | Markdown | Developer (MANUAL) | MANUAL |

Схема `comparison.json` (обязательные ключи): `schema_version="d23-comparison-v1"`,
`threshold` (number), `modes` (массив ровно из 4 записей A/B/C/D), `answers` (массив ≥40
записей 10×4). Дополнительно: `created_at`, `corpus_label`, `source_sha256`, `index`,
`model`, `embedding`, `prefilter_top_k`, `postfilter_top_k`, `summary`.

Метрики `answers[]` (F1): `usage` — плоский usage **генерации**; `latency_ms` —
`retrieval`/`context`/`chat`/`total` (`total` включает rewrite); rewrite-метрики — только в
`rewrite.usage`/`rewrite.latency_ms`. `usage.generation`/`usage.rewrite` не вводятся
(SPEC §9, §13.2).

Схема `calibration.json` (обязательные ключи): `schema_version="d23-calibration-v1"`,
`threshold` (number), `calibration_set` (массив ≥1), `candidate_thresholds`, `results`,
`selection_reason`.

Схема `trace-sample.json` (обязательные ключи): `original_query`, `search_query`,
`candidates` (массив), `exclusion_reasons` (объект с `threshold`/`top_k`/`context_budget`),
`selected`, `passed`, `counts`, `rewrite`.

---

## 8. LIVE-сценарий и измерения (SPEC §13)

* Один закреплённый индекс (`structure`) на зарегистрированном корпусе `agents-survey.pdf`;
  все 4 режима используют ровно этот `index_version_id`.
* Единые настройки выбранной модели ответа (`deepseek/deepseek-flash`, reasoning low);
  каждая ветка — пустая история (отдельный запрос без истории).
* 10 вопросов D22 × 4 режима = 40 ответов; сохраняются `comparison.json`.
* Отдельно сохраняется показательная трасса с включённым фильтром (`trace-sample.json`).
* Измеряются фактические usage/latency rewrite и генерации; недоступные — «н/д».
  `MODEL_CHECK_KIND: NETWORK` (выбранный remote-профиль); LOCAL-статусы не выставляются.
* Reasoning-профилю нужен запас output: runner поднимает `chat_max_output_tokens` до
  минимума `4096` (одинаково для всех четырёх режимов); фактическое значение записано в
  `comparison.json`. Это не меняет настройки сравнения между режимами (SPEC §13.2).
* Токены проверяемой модели не смешиваются с токенами OpenCode-агента.
* Ресурсы: `TestSession` (acquire/use/release в finally; remote не запускает/не
  останавливает локальный runtime), TEMP-БД и TEMP-каталог прогона; cleanup в finally.

---

## 9. Ручные сценарии и UI

* `run_app.bat`: большой чат сверху → выбор режима A–D и настроек (порог, top-K до/после)
  → вопрос → ответ, источники (`passed`), показанный `search_query` и трасса
  (кандидаты/выбранные/переданные, причины исключения).
* Новые элементы D23 — англоязычные (`Retrieval settings`, `Search query`, `Trace`,
  `Compare four modes`); существующие русские панели D22 (в т.ч. «Чат (RAG)», «Без RAG»,
  «С RAG», «Сравнить») **не переводятся** (SPEC §12.1, F5d).
* Отдельная панель сравнения четырёх режимов: четыре колонки с ответами, источниками и
  метриками (`rewrite.usage`/`rewrite.latency_ms` и generation `usage`/`latency_ms.chat`),
  пометки `finish_reason`/`truncated`.
* Ошибки: `invalid_threshold`, `postfilter_top_k > prefilter_top_k`,
  `context_overflow`, `chat_*`, `index_*`; пустой `selected` — понятное сообщение
  «No relevant sources found».

---

## 10. Риски и снижение

| Риск | Снижение |
| --- | --- |
| Порог, подобранный под eval-набор, искажает оценку | отдельный малый калибровочный набор; фиксация до сравнения |
| Пустой `selected` подменяется обычным RAG | явный детерминированный ответ; повторный запрос без фильтра запрещён; UNIT/INT |
| Rewrite утекает эталонами/ответами | изолированный `rewrite-v1`; UNIT-тест содержимого сообщений |
| Rewrite падает и ломает запрос | fallback на исходный запрос с сохранённой причиной |
| Регрессия D22 из-за новых полей | дефолты выключены; поля опциональны; D21–D22 тесты — регрессия |
| Legacy `top_k` теряется при D23-полях | правило слияния §7.1; режим A ≡ D22 `with_rag`; UNIT/INT `found_count == top_k` |
| Смешение метрик rewrite и генерации | плоский `usage` = генерация; rewrite в `rewrite.usage`/`rewrite.latency_ms`; `latency_ms.total` включает rewrite; UNIT. Совместимость с `test_selected_profile_end_to_end.py:76` и UI `record.usage` |
| Rewrite висит дольше генерации | отдельный rewrite-`ChatModel` с коротким `RAG_REWRITE_TIMEOUT_SECONDS`; UNIT/INT граница таймаута |
| Отмена оставляет фиктивный ответ | `GeneratorExit` не глушится; провайдерский stream закрывается в `finally`; INT-проверка отмены |
| Расхождение контракта провайдера (usage/`length`) на remote | реальные граничные ответы моделируются; LIVE-проверка выбранной remote-модели; regression-fixture |
| Затраты на 40 LIVE-ответов | один прогон на закреплённом индексе; повторные LIVE не выполняются автоматически |
| Тесты затрагивают пользовательскую БД | TEMP для `KNOWLEDGE_DB_PATH`/`CHAT_RUNS_PATH` |
| Совместимость `chat-run-v1` с 4 режимами | отдельный `comparison_kind="four_modes"`; `result_kind`/`mode` остаются `compare` |

---

## 11. Что нельзя проверить только чтением кода

Требуют фактического выполнения:

* реальный cosine-score и срабатывание порога на пользовательском `agents-survey.pdf`;
* качество `search_query` после rewrite на выбранной remote-модели;
* реальные usage/`finish_reason`/streaming выбранного remote-провайдера;
* 40 ответов и сводные метрики;
* содержательная правильность ответов, полнота фактов и валидность цитат (ручная оценка);
* UI в браузере (настройки, search query, 4-режимное сравнение);
* `run_app.bat` и `test.bat scenario d23-rag-filtering`.

Эти пункты не считаются выполненными по умолчанию и помечаются «не проверено» до прогона.

---

## 12. Трассировка P→D23

| Шаг | R (SPEC §18) | D23 |
| --- | --- | --- |
| P-01 | R-01…R-05, R-17 | D23-01, D23-03, D23-04, D23-14 |
| P-02 | R-01, R-03, R-05 | D23-01, D23-02, D23-04 |
| P-03 | R-01, R-06 | D23-01, D23-05 |
| P-04 | R-06 | D23-05, D23-14 |
| P-05 | R-04, R-05, R-17 | D23-03, D23-04, D23-09 |
| P-06 | R-03, R-04, R-06, R-07, R-10 | D23-02, D23-03, D23-05, D23-06, D23-09 |
| P-07 | R-01, R-03, R-06 | D23-01, D23-02, D23-05 |
| P-08 | R-03, R-04 | D23-02, D23-03 |
| P-09 | R-09 | D23-08 |
| P-10 | R-03, R-06, R-15 | D23-02, D23-05, D23-14 |
| P-11 | R-12 | D23-11 |
| P-12 | R-11…R-17 | D23-10, D23-11, D23-12, D23-15 |
| P-13 | R-15 | D23-14 |
| P-14 | R-08, R-09 | D23-07, D23-08 |

---

## 13. Статус

`PLAN_STATUS: AWAITING_REVIEW`.

Порядок обязательных проверок после реализации: Developer → Architect post-review →
независимый Tester → приёмка. Реальные LIVE-проверки выполняются только в рамках
обязательного сценария и действующей авторизации; повторные платные прогоны автоматически
не инициируются.
