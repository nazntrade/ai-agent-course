# SPEC — Day 23: реранкинг и фильтрация (Knowledge Agent)

Идентификатор задачи: `day-23-rag-filtering`.
Статус: **ожидает spec review** (реализация не выполнена; код, тесты, `.bat` и модели в этом
документе не создаются и не запускаются — только спецификация).
Дата: 2026-10-02.
Базовое состояние: D21 (`day-21-document-indexing`) и D22 (`day-22-first-rag-query`)
реализованы; фактическое состояние — `MODULE_STATE.md`.
Источник задания: Task Contract пользователя (D23) и замороженный acceptance-contract
`d23` (D23-C01…D23-C15). Документ самодостаточен и не требует обращения к переписке.

Термины, помеченные **предлагаемое**, на момент спецификации отсутствуют в репозитории и
создаются этой задачей. Формулировки «должно», «обязан» — нормативные требования.

---

## 1. Назначение

Развить D22-поток `вопрос → retrieval → контекст → генерация` вторым этапом после поиска:
добавить **фильтр релевантности** по настраиваемому порогу cosine similarity и
отключаемый **query rewrite**, реализовать и сравнить **четыре режима** RAG, сохранить
подробную трассу отбора (кандидаты, scores, выбранные и переданные чанки, причины
исключения) и провести LIVE-оценку на существующих 10 вопросах D22 во всех четырёх режимах.

Наблюдаемый результат дня 23:

* пользователь задаёт порог релевантности и top-K до/после фильтрации и видит, какие
  чанки отсеяны и почему;
* доступны четыре режима: (A) обычный RAG без фильтра и rewrite — baseline; (B) RAG с
  фильтром; (C) RAG с rewrite; (D) RAG с rewrite и фильтром; режимы сравниваются на одном
  закреплённом индексе и одних настройках модели;
* query rewrite переформулирует только поисковый запрос; генерация отвечает на исходный
  вопрос; исходный и поисковый запросы сохраняются;
* расход и задержка rewrite учитываются отдельно от генерации и входят в общий итог;
* если фильтр отсёк все чанки — понятное сообщение об отсутствии подходящих источников,
  без подмены режима обычным RAG;
* D21 retrieval, индексы, происхождение, `«Без RAG»`, `«С RAG»` и сравнение D22 сохранены;
  D21–D22 регрессии зелёные;
* результаты LIVE-оценки (40 ответов) и калибровка порога сохранены в фиксированных файлах.

D23 **не заменяет** D22: baseline D23 — это обычный RAG (`with_rag` без фильтра и rewrite),
а не режим `without_rag`. Режим «Без RAG» остаётся отдельной функцией D22 и не входит в
четыре сравниваемых режима.

---

## 2. Границы и не-цели

### 2.1. Must (входит в D23)

* `RelevanceFilter` — детерминированный фильтр кандидатов по порогу cosine similarity,
  применяемый после существующего `KnowledgeService.search`.
* Настраиваемые top-K **до** фильтра (`prefilter_top_k`, сколько кандидатов запросить у
  поиска) и **после** фильтра (`postfilter_top_k`, сколько оставить).
* `QueryRewriter` — контракт ядра; реализация `ChatQueryRewriter` поверх выбранного
  chat-профиля (того же, что и генерация ответа).
* Четыре режима RAG и их сравнение: A (plain RAG), B (RAG + filter), C (RAG + rewrite),
  D (RAG + rewrite + filter).
* Расширенная трасса отбора: `original_query`, `search_query`, `candidates`, `selected`,
  `passed`, `exclusion_reasons` (`threshold` / `top_k` / `context_budget`).
* Явное поведение при отсечении всех чанков: понятное сообщение, без подмены обычным RAG.
* Раздельные метрики rewrite и генерации; общий итог включает rewrite.
* HTTP API: расширение `POST /api/chat` и `POST /api/chat/stream` опциональными
  D23-полями и новый `POST /api/chat/compare-modes` для четырёх режимов.
* UI: большой чат сверху, настройки отбора, показ использованного поискового запроса,
  отдельное сравнение четырёх режимов (ответы, источники, метрики).
* Калибровочный набор (`eval/d23/calibration-questions.json`) и зафиксированный порог.
* LIVE-прогон 10 вопросов D22 × 4 режима = 40 ответов через доверенную точку входа.
* Тесты UNIT/INT, offline-сценарий, регрессия D21–D22, ручной UI.

### 2.2. Later / явные не-цели D23

MCP, Wikipedia importer, ANN/векторная БД, полноценный ML-reranker (cross-encoder), OCR,
многосессионная память, полный многошаговый чат, resume/инкрементальная переиндексация,
удаление версий, multi-process/Postgres/Docker, автоматическое скачивание моделей,
изменение retrieval-логики D21 (точный cosine остаётся). «Реранкинг» в D23 реализуется как
**пороговая фильтрация по score + показ трассы**, без нового ранжирующего ML-компонента.

### 2.3. Границы изменений

* **Не изменяются**: D21 HTTP API (`/api/health` embedding-часть, `/api/collections`,
  `/api/index/*`, `/api/index-versions/*`, `/api/search`, `/api/compare`), схема и семантика
  индексов, происхождение чанков, изоляция коллекций, `KnowledgeService.search`.
* **Сохраняются без изменения контракта**: D22 `without_rag` («Без RAG»), D22 `with_rag`
  («С RAG») по умолчанию (без включённого фильтра/rewrite), D22 `POST /api/chat/compare`,
  `/api/chat-runs*`, `chat-run-v1`/`chat-eval-v1`.
* Новые настройки добавляются в `config.py`/`.env.example` на этапе реализации; в Git
  попадает только `.env.example` без значений.
* `.bat` модуля изменяет только Configurator (GOVERNANCE), не Developer.
* `.env` не читается и не изменяется; настоящие секреты не публикуются.
* В рамках **этой** задачи (SPECIFICATION) создаются ровно три документа; код приложения,
  тесты, `.bat` и приложение не создаются и не запускаются.

---

## 3. Состояние D21–D22 (существующая архитектура)

* `KnowledgeService.search(collection_id, query, top_k, index_version_id, strategy)` —
  точный cosine по локальному SQLite-индексу, возвращает отсортированные `fragments`
  (`rank`, `score`, `chunk_id`, `text`, `metadata`), проверяя готовность и совместимость
  индекса до эмбеддинга запроса.
* `ChatService.chat/compare/stream` — планирование (`_plan`) уже делает retrieval, отбор
  `passed` через `ContextBudget` (`heuristic-v1`), сборку промпта `rag-v1`/`plain-v1` и
  сохранение `chat-run-v1` через `FileChatRunStore`.
* `ChatModel` (`OllamaChatModel`/`OpenAIChatModel`) — контракт генерации с
  `identity()/preflight()/is_available()/chat()/stream_chat()`, `ChatResult` с
  `finish_reason`/`usage`/`output_tokens_per_second`.
* API: `POST /api/chat`, `POST /api/chat/stream`, `POST /api/chat/compare`,
  `GET /api/chat-runs*`.
* UI: панель «Чат (RAG)» с кнопками «Без RAG», «С RAG», «Сравнить».
* Harness: `embed_stub.py`, `chat_stub.py`, `rag_eval.py`, `rag_eval_live.py`,
  `scenario_runner.py`; сценарий `tests/scenarios/rag-eval.{py,json}`.
* Eval: `eval/d22/questions.json` (`rag-eval-questions-v1`, 10 вопросов).

D23 встраивается между `search` и `ContextBudget`, не меняя ни `search`, ни дефолтное
поведение D22.

---

## 4. Термины

| Термин | Определение |
| --- | --- |
| **кандидат (candidate)** | Фрагмент, возвращённый `KnowledgeService.search` с `score`. |
| **выбранный (selected)** | Кандидат, прошедший фильтр порога и ограничение `postfilter_top_k`. |
| **переданный (passed)** | Выбранный чанк, реально попавший в промпт после бюджета контекста. `passed ⊆ selected ⊆ candidates`. |
| **порог (threshold / `min_score`)** | Минимальный cosine similarity кандидата; ниже — исключение с причиной `threshold`. |
| **prefilter_top_k** | Сколько кандидатов запрашивается у `search` (top-K до фильтрации). |
| **postfilter_top_k** | Максимум выбранных после фильтрации (top-K после фильтрации). |
| **режим D23** | Одна из комбинаций `use_filter`/`use_rewrite`: A/B/C/D. |
| **original_query** | Исходный вопрос пользователя; на него отвечает генерация. |
| **search_query** | Запрос, фактически отправленный в retrieval (после rewrite либо равный исходному). |
| **rewrite fallback** | Отказ rewrite: `search_query = original_query`, сохранена причина. |
| **трасса (trace)** | Сохранённая запись отбора: запросы, кандидаты, scores, selected/passed, причины исключения. |

---

## 5. Архитектура, компоненты и направление зависимостей

### 5.1. Компоненты (предлагаемое)

* **`RelevanceFilter`** (`knowledge_agent/chat/filtering.py`) — чистая функция/класс:
  `apply(candidates, threshold, postfilter_top_k) -> FilterResult`. Не знает про сеть, БД
  и HTTP. Возвращает `selected` и `exclusion_reasons` (`threshold`, `top_k`).
* **`QueryRewriter`** (контракт ядра в `domain/contracts.py`) и **`ChatQueryRewriter`**
  (`knowledge_agent/chat/rewrite.py`) — короткий ограниченный вызов `ChatModel`; возвращает
  `RewriteResult`. Rewrite использует **отдельный экземпляр `ChatModel`**, создаваемый в
  `__main__.py` с `timeout=RAG_REWRITE_TIMEOUT_SECONDS`,
  `max_output_tokens=RAG_REWRITE_MAX_OUTPUT_TOKENS` и
  `temperature=RAG_REWRITE_TEMPERATURE` (§7.2); контракт `ChatModel` D22 при этом не
  расширяется.
* **Расширение `ContextBudget`** (`knowledge_agent/chat/context.py`) — дополнительно
  сообщает `chunk_id` отброшенных по бюджету чанков (для `exclusion_reasons.context_budget`),
  сохраняя прежний контракт D22.
* **Расширение `ChatService`** (`knowledge_agent/chat/chat_service.py`) — D23-поля в
  `_plan`, сборка трассы, режимы A–D, метод `compare_modes`, корректная отмена и закрытие
  провайдерского stream (§7.4).
* **Расширение API** (`knowledge_agent/api/routes.py`) — опциональные D23-поля в
  `ChatRequest`/`StreamRequest` и `POST /api/chat/compare-modes`.
* **UI** (`knowledge_agent/ui/*`) — большой чат сверху, настройки отбора, показ
  `search_query`, панель сравнения четырёх режимов.
* **Harness** — `harness/chat_stub.py` (детерминированный rewrite/границы),
  `harness/d23_live.py` (LIVE-раннер: калибровка + 40 ответов), `tests/scenarios/d23-rag-filtering.{py,json}`.

### 5.2. Направление зависимостей

`RelevanceFilter`, `QueryRewriter` (контракт) и трасса живут в ядре. `ChatQueryRewriter`
зависит от `ChatModel`, но `ChatService` зависит только от контракта `QueryRewriter`.
HTTP/UI зависят от API. Ни `KnowledgeService.search`, ни `IndexStore`, ни D21 domain не
меняются по контракту.

### 5.3. Место D23 в существующем потоке

```
вопрос ─► [use_rewrite?] ─► QueryRewriter ─► search_query ─┐
                                                            ▼
                                       KnowledgeService.search(prefilter_top_k)
                                                            │ candidates + scores
                                                            ▼
                                   [use_filter?] ─► RelevanceFilter(threshold, postfilter_top_k)
                                                            │ selected + exclusion_reasons
                                                            ▼
                                        ContextBudget ─► passed (context_budget exclusions)
                                                            │
                                                            ▼
                        prompt (rag-v1, вопрос = original_query) ─► ChatModel ─► ответ
```

При `use_filter=false` шаг фильтра пропускается (`selected = candidates[:postfilter_top_k]`).
`prefilter_top_k`/`postfilter_top_k` разрешаются по правилу слияния §7.1: для
D22-совместимого запроса (без D23-полей) оба равны legacy `top_k`/`CHAT_TOP_K`, поэтому
режим A эквивалентен D22 `with_rag`. При `use_rewrite=false`
`search_query = original_query`, шаг rewrite пропускается.

---

## 6. Контракты данных

### 6.1. Режимы D23

| ID | `use_filter` | `use_rewrite` | Смысл |
| --- | --- | --- | --- |
| A | false | false | Обычный RAG (baseline); эквивалент D22 `with_rag` |
| B | true | false | RAG с фильтром порога |
| C | false | true | RAG с query rewrite |
| D | true | true | RAG с rewrite и фильтром |

`rag_mode` может задаваться явно (`A|B|C|D`) либо выводиться из двух boolean-полей. Явный
`rag_mode` и boolean-поля должны быть согласованы; несовпадение → `invalid_request`.

### 6.2. `RewriteResult` (предлагаемое)

```json
{
  "attempted": true,
  "used": true,
  "fallback": false,
  "reason": null,
  "original_query": "<raw question>",
  "search_query": "<reformulated query>",
  "template_id": "rewrite-v1",
  "template_hash": "<sha256>",
  "finish_reason": "stop",
  "usage": {"input_tokens": 120, "output_tokens": 11, "total_tokens": 131},
  "latency_ms": 812.4
}
```

* `attempted=false` — rewrite выключен (`use_rewrite=false`); `used=false`,
  `search_query = original_query`, `reason=null`.
* `fallback=true` — rewrite включён, но завершился ошибкой/таймаутом/пустым ответом;
  `used=false`, `search_query = original_query`, `reason` — устойчивый код причины
  (`chat_unavailable`, `chat_timeout`, `chat_invalid_response`, `chat_length_error`,
  `rewrite_invalid` — см. §8). Fallback **не является** ошибкой HTTP.
* `usage`/`finish_reason` — из ответа провайдера; отсутствие usage сохраняется как `null`,
  значения не выдумываются.
* `usage` и `latency_ms` внутри `RewriteResult` относятся **только к вызову rewrite** и не
  смешиваются с генерацией: генерация пишет плоский record-level `usage` и `latency_ms.chat`
  (совместимо с D22), rewrite — в этот объект (§9).
* Вызов ограничен по длине (`RAG_REWRITE_MAX_OUTPUT_TOKENS`) и по времени
  (`RAG_REWRITE_TIMEOUT_SECONDS`, отдельный rewrite-экземпляр `ChatModel`) — §7.2.

### 6.3. Трасса отбора (расширение `retrieval`)

Запись прогона и `trace-sample.json` содержат:

```json
{
  "original_query": "<raw question>",
  "search_query": "<query actually sent to retrieval>",
  "rewrite": {"...": "RewriteResult §6.2"},
  "candidates": [
    {"rank": 1, "score": 0.71, "chunk_id": "<id>", "metadata": {"...": "D21 §6.3"}}
  ],
  "selected": [
    {"rank": 1, "score": 0.71, "chunk_id": "<id>", "metadata": {"...": "..."}}
  ],
  "passed": [
    {"rank": 1, "chunk_id": "<id>", "estimated_tokens": 320, "metadata": {"...": "..."}}
  ],
  "found_count": 20,
  "selected_count": 3,
  "passed_count": 2,
  "exclusion_reasons": {
    "threshold": ["<chunk_id>", "..."],
    "top_k": ["<chunk_id>", "..."],
    "context_budget": ["<chunk_id>", "..."]
  }
}
```

Правила:

* `candidates` — все кандидаты из `search` (`prefilter_top_k` штук), с `rank` и `score`;
* `selected` — прошедшие порог и `postfilter_top_k`; при выключенном фильтре
  `selected == candidates[:postfilter_top_k]`;
* `passed` — реально переданные в промпт; `text` чанка в API-проекцию не попадает
  (сохраняется только внутри плана, как в D22);
* `exclusion_reasons.threshold` — `chunk_id` со `score < min_score` (пусто при выключенном
  фильтре);
* `exclusion_reasons.top_k` — `chunk_id`, прошедшие порог, но не вошедшие в
  `postfilter_top_k`;
* `exclusion_reasons.context_budget` — `chunk_id`, отброшенные бюджетом контекста;
* один `chunk_id` не может попасть в две категории исключений одновременно;
* при обратной совместимости D22 `retrieval.found` остаётся равным `candidates` (алиас),
  `retrieval.passed` остаётся как в D22;
* при D22-совместимом запросе (без D23-полей) `prefilter_top_k = legacy_top_k`, поэтому
  `found`/`found_count` не меняются (правило слияния §7.1; регрессия `found_count == top_k`).

### 6.4. `D23ComparisonResult` — сравнение четырёх режимов

```json
{
  "schema_version": "chat-run-v1",
  "run_id": "d23-<UTC timestamp>-<8 hex>",
  "created_at": "<iso8601>",
  "result_kind": "compare",
  "mode": "compare",
  "comparison_kind": "four_modes",
  "question": "<raw question>",
  "comparison": {
    "same_model": true,
    "same_settings": true,
    "index_version_id": "<pinned id>",
    "threshold": 0.55,
    "prefilter_top_k": 20,
    "postfilter_top_k": 5,
    "policy_differences": [
      "B and D apply the relevance threshold",
      "C and D replace the retrieval query with a rewritten one",
      "A is plain RAG (no filter, no rewrite)"
    ]
  },
  "modes": [
    {"id": "A", "use_filter": false, "use_rewrite": false, "branch": {"...": "ChatAnswer-подобные поля §6.2 D22"}},
    {"id": "B", "use_filter": true,  "use_rewrite": false, "branch": {"...": "..."}},
    {"id": "C", "use_filter": false, "use_rewrite": true,  "branch": {"...": "..."}},
    {"id": "D", "use_filter": true,  "use_rewrite": true,  "branch": {"...": "..."}}
  ],
  "errors": []
}
```

Гарантии (аналогично D22 §6.3): одна `ChatModelIdentity` и одни настройки во всех четырёх
ветках; у каждой ветки своя пустая история (ответ одной не попадает в другую); индекс
разрешается один раз и фиксируется; «победитель» не выводится.

---

## 7. Семантика фильтрации и rewrite

### 7.1. Фильтрация

1. `search` вызывается с `top_k = prefilter_top_k` (top-K до фильтрации).
2. При `use_filter=true` кандидаты со `score < min_score` исключаются (`threshold`).
3. Оставшиеся сортируются по `score` убыв. и обрезаются до `postfilter_top_k` (`top_k`).
4. `selected` передаются в `ContextBudget`; не поместившиеся — `context_budget`.

**Правило слияния legacy `top_k` и D23-полей (F2):**

1. `legacy_top_k` = `request.top_k`, если задан, иначе `CHAT_TOP_K` (существующий
   D22-дефолт).
2. `d23_requested` = истина, если в запросе присутствует хотя бы одно D23-поле
   (`use_filter`, `use_rewrite`, `prefilter_top_k`, `postfilter_top_k`, `min_score`,
   `rag_mode`).
3. `prefilter_top_k` = явный `prefilter_top_k`; иначе `RAG_PREFILTER_TOP_K`, если
   `d23_requested`; иначе `legacy_top_k`.
4. `postfilter_top_k` = явный `postfilter_top_k`; иначе `prefilter_top_k`.
5. D22-совместимый вызов `/api/chat` (ни одного D23-поля) даёт
   `prefilter_top_k = postfilter_top_k = legacy_top_k`, фильтр и rewrite выключены:
   `retrieval.found`/`found_count` не меняются, режим A ≡ D22 `with_rag`.

Регрессия (UNIT/INT): на D22-совместимом запросе с `top_k` stub-поиск возвращает ровно
`top_k` кандидатов, `found_count == top_k`, а `search` вызывается с `top_k=top_k`.
`RAG_PREFILTER_TOP_K` (config) применяется как default `prefilter_top_k` только к явно
D23-запросам (`/api/chat`, `/api/chat/compare-modes`), но не подменяет legacy `top_k` в
D22-совместимом вызове; `RAG_FILTER_TOP_K` (§11.2) — default `postfilter_top_k` только для
`compare-modes`, тогда как в `/api/chat` при отсутствии `postfilter_top_k` он равен
resolved `prefilter_top_k`.

Границы: `0.0 <= min_score <= 1.0` (вне диапазона → `invalid_threshold` 422);
`1 <= postfilter_top_k <= prefilter_top_k <= 50` (иначе `invalid_request` 422). Порог `0.0`
эквивалентен отключённому отсечению по порогу; порог `1.0` оставляет только точные
совпадения (обычно пусто → §7.3). Порядок кандидатов стабилен (сортировка по `score`, при
равенстве — по `rank` из `search`).

### 7.2. Query rewrite

* Rewrite-промпт (`rewrite-v1`) содержит **только** системную инструкцию «переформулируй
  вопрос для семантического поиска, верни одну строку» и текст исходного вопроса.
* В промпт **не попадают**: эталонные факты (`expected_facts`), готовые ответы, ответы
  соседних режимов, история D22, содержимое индекса, секреты.
* Вызов ограничен по длине `RAG_REWRITE_MAX_OUTPUT_TOKENS` и по времени
  `RAG_REWRITE_TIMEOUT_SECONDS`; `temperature=RAG_REWRITE_TEMPERATURE` (`0`). Результат
  нормализуется до одной строки; сам лимит output-токенов задан с запасом (1024), потому
  что выбранный reasoning-профиль расходует бюджет на внутреннее рассуждение и при 64
  токенах возвращает пустой ответ (`chat_invalid_response` → fallback).
* **Per-call timeout rewrite (решение F3, вариант «б»)**: rewrite получает отдельный
  экземпляр `ChatModel`, создаваемый в `__main__.py` (`build_chat_service`) с
  `timeout=RAG_REWRITE_TIMEOUT_SECONDS`, `max_output_tokens=RAG_REWRITE_MAX_OUTPUT_TOKENS`,
  `temperature=RAG_REWRITE_TEMPERATURE`. `ChatQueryRewriter` использует этот экземпляр;
  `ChatService` зависит только от контракта `QueryRewriter`. Существующий контракт
  `ChatModel.chat/stream_chat()` D22 и оба адаптера (`openai_chat`, `ollama_chat`) **не
  меняются**: адаптер уже применяет свой `timeout` к HTTP-вызову
  (`OllamaChatModel._request` → transport timeout; `OpenAIChatModel._request` →
  `urlopen(..., timeout=self.timeout)`). Так `RAG_REWRITE_TIMEOUT_SECONDS` реально
  ограничивает весь rewrite-вызов, отдельно от таймаута генерации.
* Проверка границы таймаута: UNIT с mock transport/opener фиксирует, что rewrite-адаптер
  передаёт в транспорт именно `RAG_REWRITE_TIMEOUT_SECONDS`, а `ChatTimeout` из
  rewrite-модели даёт `fallback` с причиной `chat_timeout`; INT с `chat_stub`, задержка
  которого превышает короткий `RAG_REWRITE_TIMEOUT_SECONDS`, подтверждает фактический
  fallback без влияния на генерацию.
* Результат нормализуется (trim, одна строка). Пустой результат, ответ-отказ, перенос
  строк или превышение лимита длины → `rewrite_invalid` и fallback на исходный запрос.
* Ошибка/таймаут провайдера → fallback с сохранённой причиной (§6.2).
* Генерация ответа **всегда** получает `original_query` в пользовательском сообщении;
  `search_query` используется только для `search`.
* Задержка и usage rewrite не смешиваются с генерацией (§9).

### 7.3. Фильтр отсекает всё

При `use_filter=true`, если `selected` пуст:

* retrieval продолжает вызываться (для трассы), `passed = []`;
* ответ — детерминированное сообщение сервиса об отсутствии подходящих источников;
  генерация модели **не** вызывается: record-level `usage = null`, `latency_ms.chat = null`
  (плоское поле генерации D22), `latency_ms.total` не включает генерацию;
  `answer.finish_reason = null`, `answer.insufficient_sources = true`; если rewrite
  выполнялся (режим D) до фильтра, его `rewrite.usage`/`rewrite.latency_ms` сохраняются
  отдельно в объекте `rewrite` (§9);
* режим **не** подменяется обычным RAG (не выполняется повторный запрос без фильтра);
* `exclusion_reasons.threshold` (или `top_k`) не пуст, что объясняет пустой `selected`;
* сообщение одинаково для B и D (при D в трассе дополнительно виден `search_query`).

Выбор детерминированного сообщения (без вызова модели) гарантирует отсутствие
галлюцинации и подмены режима; это осознанное решение D23.

### 7.4. Отмена (разрыв клиента в streaming)

* Отмена — это закрытие SSE-потребителя (обрыв соединения клиентом) либо отмена
  response-задачи ASGI. Python доставляет `GeneratorExit` в точку текущего `yield` внутри
  `stream_events`; `GeneratorExit`/`asyncio.CancelledError` являются `BaseException`,
  поэтому существующий `except Exception` в `stream_events` **не** превращает отмену в
  событие `error` и не создаёт фиктивную запись `internal_error`.
* `stream_events` оборачивает итерацию провайдерского `stream_chat` в `try/finally` и в
  `finally` явно закрывает итератор (`.close()`), освобождая HTTP-ответ/сокет провайдера
  (адаптеры дополнительно закрывают response в собственном `finally`).
* Наблюдаемое поведение отмены: событие `done` не отправляется; отмена не маскируется
  успешным ответом; при чистом разрыве (без предшествующей типизированной ошибки
  провайдера) запись `chat-run-v1` не сохраняется, поэтому в истории нет фиктивного
  успешного прогона. Если типизированная ошибка провайдера получена до разрыва, применяется
  обычный путь ошибки (`error`-событие и частичная запись с непустым `errors`).
* Сервер остаётся работоспособным: последующие запросы обслуживаются, `/api/health`
  отвечает 200.
* Проверка INT (P-13): открыть `POST /api/chat/stream` на loopback, прочитать `start`
  (и, при наличии, первый `token`), закрыть клиентское соединение до `done`; убедиться, что
  сервер жив, повторный запрос успешен, а для прерванного `run_id` нет записи с непустым
  `answer`. Критерий D23-C14 «отмена» считается проверенным только этим сценарием.

---

## 8. Таксономия ошибок (дополнение к D22)

Форма ошибок — как в D21/D22: `{"error": {"code", "message", "details"?}}`.

| Код | HTTP | Ситуация |
| --- | --- | --- |
| `invalid_threshold` | 422 | `min_score` вне `[0, 1]` |
| `invalid_request` | 422 | `postfilter_top_k > prefilter_top_k`; `rag_mode` не согласован с boolean-полями; иные некорректные D23-поля |
| `context_overflow` | 422 | инструкции + вопрос + overhead не помещаются (переиспользуется D22) |
| `chat_unavailable`/`chat_timeout`/`chat_model_missing`/`chat_invalid_response`/`chat_length_error` | 503 | ошибки провайдера генерации (D22) |
| `index_not_ready`/`index_incompatible`/`embedding_*` | 409/503 | ошибки retrieval (D22), не маскируются |

Правила:

* Ошибка rewrite **не** является ошибкой HTTP — это fallback с сохранённой причиной.
* `finish_reason="length"` не ошибка: `answer.truncated = true` (D22), включая rewrite-ответ
  (rewrite-ответ с `length` не принимается как валидная переформулировка → fallback).
* Ошибки индекса/retrieval в режимах B/C/D не маскируются ответом без RAG.
* Сообщения санитизированы: ключи/содержимое `.env`, base URL и lease ID не попадают в
  ответы, записи и отчёты.

---

## 9. Метрики: rewrite отдельно от генерации

Плоский record-level `usage` остаётся **usage генерации** и сохраняет форму D22 —
`{"input_tokens", "output_tokens", "total_tokens"}` либо `null`; он **никогда** не
становится вложенным объектом `{generation, rewrite}`. Rewrite-метрики живут в top-level
объекте `rewrite` (`RewriteResult` §6.2): `rewrite.usage` и `rewrite.latency_ms`.

`latency_ms` сохраняет D22-ключи `retrieval`, `context`, `chat` и добавляет `total`:

```json
{
  "latency_ms": {
    "retrieval": 12.4,
    "context": 0.5,
    "chat": 3200.1,
    "total": 4025.4
  },
  "rewrite": {
    "...": "RewriteResult §6.2 (attempted/used/fallback/reason/finish_reason)",
    "usage": {"input_tokens": 120, "output_tokens": 11, "total_tokens": 131},
    "latency_ms": 812.4
  },
  "usage": {"input_tokens": 1987, "output_tokens": 145, "total_tokens": 2132}
}
```

* `latency_ms.chat` — задержка генерации и **остаётся** D22-совместимым ключом (не
  «может»); отдельное поле `generation` в записи не вводится.
* `latency_ms.total = retrieval + context + chat + rewrite`; при выключенном rewrite
  `rewrite = 0`, и сумма совпадает с D22. `total` **включает** rewrite.
* `rewrite.usage`/`rewrite.latency_ms` относятся только к rewrite-вызову; недоступные
  значения — `null`/`0.0`; с генерацией не смешиваются.
* `output_tokens_per_second` относится к генерации; rewrite-скорость отдельно не выводится.
* Совместимость D22: плоский `usage` читается существующим UI (`ui/app.js`:
  `record.usage.input_tokens`/`output_tokens`) и интеграционным тестом
  `tests/integration/test_selected_profile_end_to_end.py:76`
  (`record['usage']['input_tokens'] == 30`); D23 сохраняет ровно этот путь. Добавление
  top-level `rewrite` и `latency_ms.total` аддитивно и не ломает эти чтения.
* Для D22-совместимого запроса (режим A, rewrite не запускался):
  `rewrite.attempted=false`, `rewrite.usage=null`, `rewrite.latency_ms=0.0`, а
  `usage`/`latency_ms.chat` совпадают с D22.

---

## 10. Конфигурация

Новые настройки (предлагаемые; добавляются в `config.py`/`.env.example` на этапе
реализации, `.env` не создаётся):

| Переменная | Default | Назначение |
| --- | --- | --- |
| `RAG_FILTER_ENABLED` | `0` | D23-дефолт фильтра (0 — как D22) |
| `RAG_MIN_SCORE` | `0.0` | Порог cosine similarity |
| `RAG_PREFILTER_TOP_K` | `20` | default `prefilter_top_k` для явно D23-запроса/`compare-modes`; D22-совместимый запрос наследует legacy `top_k`/`CHAT_TOP_K` (§7.1) |
| `RAG_FILTER_TOP_K` | `5` | default `postfilter_top_k` для `/api/chat/compare-modes` и UI-сравнения; в `/api/chat` при отсутствии `postfilter_top_k` он равен resolved `prefilter_top_k` (§7.1) |
| `RAG_REWRITE_ENABLED` | `0` | D23-дефолт rewrite |
| `RAG_REWRITE_MAX_OUTPUT_TOKENS` | `1024` | Ограничение длины переформулировки; 64 недостаточно для reasoning-профиля (см. §7.2) |
| `RAG_REWRITE_TIMEOUT_SECONDS` | `30` | Таймаут rewrite-вызова |
| `RAG_REWRITE_TEMPERATURE` | `0` | Воспроизводимость rewrite |
| `D23_DATA_PATH` | `local-data/d23` | Каталог артефактов D23 |
| `D23_CALIBRATION_QUESTIONS_PATH` | `eval/d23/calibration-questions.json` | Калибровочный набор |

`RAG_REWRITE_TIMEOUT_SECONDS`/`RAG_REWRITE_MAX_OUTPUT_TOKENS`/`RAG_REWRITE_TEMPERATURE`
применяются к отдельному rewrite-экземпляру `ChatModel` в `__main__.py` (§7.2); контракт
`ChatModel` D22 при этом не меняется.

Правила: дефолты D23 выключены (`RAG_FILTER_ENABLED=0`, `RAG_REWRITE_ENABLED=0`), поэтому
поведение D22 не меняется. `RAG_FILTER_ENABLED`/`RAG_REWRITE_ENABLED` реально применяются
как fallback-дефолты, когда запрос не задаёт `use_filter`/`use_rewrite`; явно переданные
поля запроса имеют приоритет (при обоих `0` получается режим A и поведение D22, K2).
Новые настройки перечислены в `.env.example` как закомментированные заглушки без секретов.
`load_settings(env)` остаётся тестируемым и не читает `.env`. Модели не скачиваются
автоматически; chat-профиль — выбранный `AI_TEST_MODEL_*`.

---

## 11. HTTP API (единый источник истины)

HTTP-слой — FastAPI с OpenAPI 3.1. **Этот раздел — единственный источник истины для
новых методов, путей, параметров и форм ответов.** `PLAN.md` ссылается сюда и не
дублирует пути.

### 11.1. Расширение `POST /api/chat` и `POST /api/chat/stream`

К существующему `ChatRequest` (D22 §11.2) добавляются опциональные поля:

```json
{
  "use_filter": false,
  "use_rewrite": false,
  "prefilter_top_k": 20,
  "postfilter_top_k": 5,
  "min_score": null,
  "rag_mode": null
}
```

* `use_filter`/`use_rewrite` — по умолчанию `false` (поведение D22).
* `prefilter_top_k`/`postfilter_top_k` — целые `1..50`; разрешаются по правилу слияния
  §7.1: при отсутствии обоих D23-полей наследуется legacy `top_k`/`CHAT_TOP_K`, поэтому
  D22-совместимый вызов не меняет `retrieval.found`; `postfilter_top_k` по умолчанию равен
  `prefilter_top_k` (регрессия `found_count == top_k`).
* `min_score` — `0.0..1.0`; default `RAG_MIN_SCORE`.
* `rag_mode` — `A|B|C|D|null`; при задании должен совпадать с boolean-полями, иначе
  `invalid_request`.
* `mode="without_rag"`: D23-поля игнорируются (фильтр и rewrite не применяются), выполняется
  ровно один вызов модели по шаблону `plain-v1` (роли `system`/`user`); поведение и запись
  остаются D22-совместимыми (существующий UNIT `test_chat_service` сохраняется).
* Тело ответа — прежний `ChatAnswer` (`chat-run-v1`): плоский `usage` генерации (§9),
  `retrieval` расширен полями §6.3, top-level `rewrite` и `latency_ms.total` присутствуют
  всегда (для A/выключенного rewrite — `attempted=false`).
* `POST /api/chat/stream`: порядок событий прежний; `sources` дополнительно несёт
  `search_query` и счётчики `selected_count`; при пустом `selected` и включённом фильтре
  событие `done` несёт запись с `retrieval.passed=[]` и детерминированным сообщением; при
  разрыве клиента действует семантика отмены §7.4.

### 11.2. `POST /api/chat/compare-modes`

Тело (`CompareModesRequest`):

```json
{
  "collection_id": "<id>",
  "index_version_id": "<id|null>",
  "strategy": "fixed|structure|null",
  "question": "<1..2000 chars>",
  "prefilter_top_k": 20,
  "postfilter_top_k": 5,
  "max_context_tokens": null,
  "min_score": null,
  "save_run": true
}
```

`prefilter_top_k`/`postfilter_top_k` по умолчанию берутся из `RAG_PREFILTER_TOP_K`/
`RAG_FILTER_TOP_K`; `min_score` — из `RAG_MIN_SCORE`. Это явно D23-эндпоинт, поэтому здесь
применяются D23-дефолты (правило слияния §7.1 относится к D22-совместимому `/api/chat`).

Успешный ответ — `D23ComparisonResult` (§6.4), сохраняется как `chat-run-v1`
(`result_kind="compare"`, `mode="compare"`, `comparison_kind="four_modes"`). Индекс
разрешается один раз; при отсутствии ready/совместимого индекса — 409 без частичного
прогона. `without_rag` в четыре режима не входит.

### 11.3. Существующие endpoint-ы

`GET /api/health`, `/api/collections`, `/api/index/*`, `/api/index-versions/*`,
`/api/search`, `/api/compare`, `/api/chat/compare`, `/api/chat-runs*` — без изменений
контракта. D23 использует их как есть.

---

## 12. UI

### 12.1. Язык

Новые элементы D23 (настройки отбора, показ поискового запроса, панель сравнения четырёх
режимов) пишутся **по-английски** — по правилу пользовательского UI из `PROJECT_RULES.md`
(«пользовательский UI — по-английски») и `AGENTS.md` (Code language). Существующие
русскоязычные панели D22 («Чат (RAG)», «Без RAG», «С RAG», «Сравнить») в этой задаче
**не переводятся** (`AGENTS.md`: не переводить намеренно ещё не переведённые экраны).
Технические идентификаторы режимов (`A`–`D`, `use_filter`, `threshold`) остаются
английскими строками протокола.

### 12.2. Наблюдаемое поведение

* **Большой чат сверху**: существующая панель «Чат (RAG)» (русский текст, не переводится)
  остаётся первой и увеличивается (больше места для ответа и источников).
* **Retrieval settings** (новый блок, англ.): выбор режима A/B/C/D (или переключатели
  `Filter`/`Rewrite`), поле `Min score`, поля `Prefilter top-K` / `Postfilter top-K`.
* **Search query** (новый элемент, англ.): при включённом rewrite видно `search_query`
  рядом с исходным вопросом и признак fallback (и его причину).
* **Trace** (новый блок, англ.): число кандидатов/выбранных/переданных и причины
  исключения по категориям.
* **Compare four modes** (новая панель, англ.): четыре колонки (A–D), в каждой — ответ,
  источники (`passed`), метрики (`rewrite`/generation `usage`/latency), пометка
  `finish_reason`/`truncated`.
* Понятные ошибки: `invalid_threshold`, `context_overflow`, `chat_*`, `index_*`; тексты
  новых D23-сообщений — на английском.
* При пустом `selected` — понятное английское сообщение (например
  «No relevant sources found»), без подмены обычным RAG.

---

## 13. LIVE-оценка и сохраняемые артефакты

### 13.1. Место (фиксировано)

```
week-05/knowledge-agent/local-data/d23/
  calibration.json       # d23-calibration-v1 — калибровка порога
  comparison.json        # d23-comparison-v1 — 40 ответов (10 вопросов × 4 режима)
  trace-sample.json      # d23-trace-v1 — сохранённая трасса одного показательного прогона
  quality-assessment.md  # ручная оценка (MANUAL), текст
```

`local-data/` уже в `.gitignore`; корпус, индексы и результаты в Git не попадают, но
объявленные артефакты сохраняются и предъявляются для приёмки.

### 13.2. `comparison.json` (`d23-comparison-v1`)

```json
{
  "schema_version": "d23-comparison-v1",
  "created_at": "<iso8601>",
  "corpus_label": "agents-survey.pdf",
  "source_sha256": "<sha256>",
  "index": {"collection_id": "<id>", "index_version_id": "<pinned id>",
            "strategy": "structure", "fingerprint": "<sha256>"},
  "model": {"provider": "openai-compatible", "model": "deepseek/deepseek-flash",
            "kind": "remote", "settings": {"temperature": 0, "seed": null,
            "num_predict": 1024, "num_ctx": 8192}},
  "embedding": {"model": "<embedding model>", "dimension": 768, "digest": "<...>"},
  "threshold": 0.55,
  "prefilter_top_k": 20,
  "postfilter_top_k": 5,
  "modes": [
    {"id": "A", "use_filter": false, "use_rewrite": false},
    {"id": "B", "use_filter": true,  "use_rewrite": false},
    {"id": "C", "use_filter": false, "use_rewrite": true},
    {"id": "D", "use_filter": true,  "use_rewrite": true}
  ],
  "answers": [
    {
      "question_id": "D22-Q01", "mode": "A", "run_id": "<id>",
      "original_query": "<question>", "search_query": "<question>",
      "rewrite": {"...": "§6.2, включая rewrite.usage/rewrite.latency_ms"},
      "answer_text": "<text>", "finish_reason": "stop", "truncated": false,
      "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
      "latency_ms": {"retrieval": 0.0, "context": 0.0, "chat": 0.0, "total": 0.0},
      "retrieval": {"found_count": 0, "selected_count": 0, "passed_count": 0,
                    "selected_chunk_ids": [], "passed_chunk_ids": [],
                    "exclusion_reasons": {"threshold": [], "top_k": [],
                                          "context_budget": []}},
      "citations": {"valid": [], "unsupported": []},
      "errors": []
    }
  ],
  "summary": {"...": "агрегаты по секциям §14, необязательно"}
}
```

Инварианты: `modes` содержит ровно четыре записи; `answers` содержит **не менее 40**
(10 вопросов × 4 режима); все ответы получены на **одном закреплённом**
`index_version_id`, при одинаковых настройках модели и с пустой историей; `threshold`
совпадает с зафиксированным в `calibration.json`.

Воспроизводимость LIVE: для выбранного reasoning-профиля runner поднимает
`chat_max_output_tokens` до безопасного минимума `4096` (иначе reasoning расходует бюджет
и ответы пусты); это значение **одинаково для всех четырёх режимов** и явно записано в
`comparison.json.model.max_output_tokens`/`settings.num_predict`. Между режимами настройки
сравнения не различаются.

Метрики: `answers[].usage` — **плоский** usage генерации (D22-совместимый, не вложенный);
`answers[].latency_ms.chat` — задержка генерации, `answers[].latency_ms.total` включает
rewrite; rewrite-метрики — только в `answers[].rewrite.usage`/`.latency_ms`. Это устраняет
противоречие §9/§13.2: comparison.json не вводит `usage.generation`/`usage.rewrite`.

### 13.3. `calibration.json` (`d23-calibration-v1`)

```json
{
  "schema_version": "d23-calibration-v1",
  "created_at": "<iso8601>",
  "corpus_label": "agents-survey.pdf",
  "calibration_set": [
    {"question_id": "D23-CAL-01", "question": "<question>",
     "relevant_sections": ["..."], "relevant_chunk_ids": ["..."],
     "candidate_scores": [0.71, 0.66, 0.42]}
  ],
  "candidate_thresholds": [0.40, 0.50, 0.55, 0.60, 0.70],
  "results": [
    {"threshold": 0.40, "kept": 12, "kept_relevant": 3, "dropped_relevant": 0,
     "precision": 0.25, "recall": 1.0}
  ],
  "threshold": 0.55,
  "selection_reason": "<why this threshold was frozen>"
}
```

`calibration_set` — отдельный небольшой набор (не 10 вопросов D22), находится вне
индексируемого корпуса. Порог фиксируется до итогового сравнения: `comparison.json`
использует `calibration.threshold`.

### 13.4. `trace-sample.json` (`d23-trace-v1`)

Один показательный прогон с включённым фильтром: содержит `original_query`,
`search_query`, `candidates`, `selected`, `passed`, `exclusion_reasons`, `counts`,
`rewrite`. Сохраняется LIVE-раннером (или offline-интеграционным сценарием) для
доказательства трассы.

### 13.5. `quality-assessment.md`

Ручная оценка Developer: сравнение четырёх режимов по сохранённым 40 ответам и источникам,
раздельно по релевантности поиска, полноте фактов и подтверждению источников; особое
внимание Q06/Q07/Q08 и вопросу вне корпуса Q10; зафиксированы улучшения и ухудшения;
если полезного улучшения нет — разбор причины и внесённое исправление с повторным прогоном.
Файл не создаётся раннером и не подменяет LIVE-доказательства.

---

## 14. Оценка качества (раздельная)

Оцениваются независимо (0/1/2, как D22 §15.3):

1. **Retrieval** — попали ли ожидаемые разделы/страницы/чанки в `candidates`/`selected`/
   `passed`; сравнение режимов A–D; особое внимание Q06/Q07/Q08 (пропущенные источники).
2. **Содержание** — присутствие ожидаемых фактов; отсутствие выдумывания при
   `answerable:false` (Q10).
3. **Источники** — валидность цитат по `passed`, число `unsupported`, корректность
   происхождения.

Непустой ответ, существующий `chunk_id` и успешная команда **сами по себе** не доказывают
качество. Автоматический агрегатор формирует сводку, но не заменяет ручную оценку.

---

## 15. Инварианты и ограничения

### 15.1. Инварианты D23

| ID | Инвариант |
| --- | --- |
| K1 | D21/D22 API, индексы, происхождение и изоляция коллекций не изменены. |
| K2 | Дефолты D23 выключены: поведение D22 `with_rag`/`without_rag`/`compare` сохраняется. |
| K3 | `passed ⊆ selected ⊆ candidates`; поля трассы раздельны. |
| K4 | `original_query` и `search_query` сохраняются; генерация отвечает на `original_query`. |
| K5 | Rewrite-промпт не содержит эталонов, готовых ответов и истории соседних режимов. |
| K6 | Ошибка/таймаут/пустой rewrite → fallback с сохранённой причиной, не HTTP-ошибка. |
| K7 | Пустой `selected` при включённом фильтре → понятное сообщение, без подмены обычным RAG. |
| K8 | Причины исключения разделены: `threshold` / `top_k` / `context_budget`. |
| K9 | Плоский record-level `usage` — usage генерации (D22); rewrite-метрики — в `rewrite.usage`/`rewrite.latency_ms`; `latency_ms.chat` = генерация; `latency_ms.total` включает rewrite. |
| K10 | Baseline D23 = обычный RAG (A), а не `without_rag`. |
| K11 | Четыре режима сравниваются на одном индексе и одних настройках, с пустой историей. |
| K12 | Порог калибруется на отдельном наборе и фиксируется до сравнения. |
| K13 | Модели не скачиваются; секреты и base URL не публикуются. |
| K14 | Тестовые данные изолированы в TEMP; пользовательская БД не затрагивается. |
| K15 | Отмена (разрыв клиента/`GeneratorExit`) не маскируется успехом или `internal_error`; провайдерский stream закрывается; сервер остаётся работоспособным. |
| K16 | D22-совместимый запрос без D23-полей сохраняет `retrieval.found` и режим A ≡ D22 `with_rag` (правило слияния §7.1). |

### 15.2. Ограничения

* «Реранкинг» D23 — пороговый отбор по cosine + трасса, без ML-reranker.
* Порог cosine зависит от embedding-модели и корпуса; калибровка привязана к
  закреплённому индексу и не переносится автоматически.
* Rewrite — недетерминированная генерация; `temperature=0`/`seed` не гарантируют
  побитовую воспроизводимость.
* Полная защита от prompt-injection в чанках невозможна (сохраняются меры D22).
* Точный cosine D21 остаётся для маленького корпуса; ANN — Later.

---

## 16. Тесты по уровням (кратко)

* **UNIT** (без сети и `.env`): `RelevanceFilter` (порог, top-K, причины исключения);
  `ChatQueryRewriter` на fake-`ChatModel` (обычный rewrite, fallback при ошибке/таймауте/
  пустом/`length`, отсутствие эталонов и истории в промпте); расширенный `ContextBudget`
  (chunk_id отброшенных); `ChatService` (трасса, режимы A–D, `compare_modes`, пустой
  `selected`, раздельные метрики); правило слияния top-K (D22-совместимый запрос →
  `search` с legacy `top_k`, `found_count == top_k`); граница таймаута rewrite (mock
  transport получает `RAG_REWRITE_TIMEOUT_SECONDS`); валидация новых API-полей.
* **INT** (loopback, `embed_stub` + `chat_stub`, без внешней сети, TEMP): полный D23-поток
  в четырёх режимах; `/api/chat/compare-modes`; `search_query` в трассе; пустой `selected`;
  границы; отмена SSE (разрыв клиента, §7.4); D21–D22 регрессия.
* **LIVE**: калибровка на малом наборе, закреплённый индекс, 40 ответов на выбранной
  remote-модели через `test.bat scenario d23-rag-filtering`.
* **MANUAL/UI**: `run_app.bat`: большой чат, настройки, показ `search_query`, сравнение
  четырёх режимов, ошибки.
* **Регрессия D21–D22**: существующие `tests/unit`, `tests/integration`, `smoke_test.bat`
  остаются зелёными.

Детализация — `PLAN.md` §4 и `ACCEPTANCE.md`.

---

## 17. Открытые решения (дефолты)

| Решение | Предлагаемый дефолт | Обоснование |
| --- | --- | --- |
| Порог по умолчанию | `0.0` (как D22) | не менять поведение D22 без явного запроса |
| prefilter/postfilter top-K | `20`/`5` для D23-запросов; legacy `top_k` для D22-совместимых | шире пул кандидатов, но режим A ≡ D22 `with_rag` (правило слияния §7.1) |
| Метод фильтра | чистый порог по `score` | детерминированно, offline, без обучения |
| Rewrite-модель | отдельный экземпляр `ChatModel` (`__main__.py`) с коротким timeout/лимитом | per-call timeout без изменения контракта D22 и адаптеров (§7.2) |
| Формат rewrite | одна строка; output-лимит `RAG_REWRITE_MAX_OUTPUT_TOKENS=1024` | короткий ограниченный вызов с запасом для reasoning-профиля |
| Пустой `selected` | детерминированное сообщение без вызова модели | исключает подмену режима и галлюцинацию |
| Артефакты | `local-data/d23/*.json` + `quality-assessment.md` | фиксированные пути из контракта |
| Калибровка | отдельный набор `eval/d23/calibration-questions.json` | не подстраивать порог под 10 вопросов D22 |

---

## 18. Трассировка R→D23

| ID | Требование | U (задание D23) | D23 |
| --- | --- | --- | --- |
| R-01 | Фильтр релевантности после поиска с настраиваемым порогом; top-K до/после фильтрации | 1 | D23-01 |
| R-02 | Фильтр опционален; `search` D21 не изменён | 1, 6 | D23-01, D23-07 |
| R-03 | Четыре режима A–D, сравнение на одном индексе/настройках/пустой истории | 2 | D23-02 |
| R-04 | Rewrite меняет только поисковый запрос; генерация отвечает на исходный; оба сохранены | 3 | D23-03 |
| R-05 | Короткий ограниченный rewrite без эталонов/ответов/истории; fallback с причиной | 3 | D23-04 |
| R-06 | Трасса: original/search query, кандидаты, scores, selected/passed, причины исключения | 4 | D23-05 |
| R-07 | Фильтр отсёк всё → понятное отсутствие источников, без подмены обычным RAG | 5 | D23-06 |
| R-08 | Сохранены D21–D22 индексы, метаданные, происхождение, поиск, «Без RAG», «С RAG», compare | 6 | D23-07 |
| R-09 | UI: большой чат, настройки отбора, показ search query, сравнение четырёх режимов | 7 | D23-08 |
| R-10 | Расход/задержка rewrite отдельно и в общем итоге | 8 | D23-09 |
| R-11 | LIVE-оценка 10 вопросов D22 × 4 режима = 40 ответов, закреплённый индекс, пустая история | 9 | D23-10 |
| R-12 | Калибровка порога на отдельном наборе, фиксация до сравнения | 10 | D23-11 |
| R-13 | Раздельная оценка retrieval/фактов/источников; улучшения/ухудшения; Q06/Q07/Q08/Q10 | 11 | D23-12 |
| R-14 | Нет полезного улучшения → разбор причины и исправление с повторным прогоном | 11 | D23-13 |
| R-15 | Ошибки/границы: порог 0/1, top-K 0/1, бюджет, chat error/timeout, пустой/length, отмена; регрессии | 12 | D23-14 |
| R-16 | Доверенные точки входа: run_app.bat, test.bat scenario d23-rag-filtering; stub ≠ LIVE | 7, 9–12 | D23-15 |
| R-17 | Граница провайдера: finish_reason/usage/streaming/limits без выдумывания | кросс | D23-04, D23-09, D23-14 |
| R-18 | Границы: нет MCP/ANN/reranker/памяти; без скачивания моделей; TEMP-изоляция | 6, 12 | D23-07, D23-14 |

### 18.1. Маппинг D23-01…D23-15 (полностью раскрыт в ACCEPTANCE.md)

| ID | Суть | R | Контракт |
| --- | --- | --- | --- |
| D23-01 | Фильтр порога + top-K до/после | R-01, R-02 | D23-C01 |
| D23-02 | Четыре режима и сравнение | R-03 | D23-C02 |
| D23-03 | Rewrite только поиска, оба запроса сохранены | R-04 | D23-C03 |
| D23-04 | Ограниченный rewrite, изоляция промпта, fallback | R-05 | D23-C04 |
| D23-05 | Полная трасса и типы причин исключения | R-06 | D23-C05 |
| D23-06 | Пустой результат фильтра | R-07 | D23-C06 |
| D23-07 | Сохранение D21–D22, регрессии | R-08 | D23-C07 |
| D23-08 | UI: чат, настройки, search query, сравнение | R-09 | D23-C08 |
| D23-09 | Метрики rewrite отдельно | R-10 | D23-C09 |
| D23-10 | LIVE 40 ответов | R-11 | D23-C10 |
| D23-11 | Калибровка порога | R-12 | D23-C11 |
| D23-12 | Раздельная оценка качества | R-13 | D23-C12 |
| D23-13 | Разбор и исправление при отсутствии улучшения | R-14 | D23-C13 |
| D23-14 | Ошибки, границы, отмена, регрессии | R-15 | D23-C14 |
| D23-15 | Реальный запуск и live-сценарий через точки входа | R-16, R-17 | D23-C15 |

---

## 19. Что НЕ входит (Later)

MCP, Wikipedia importer, ANN/векторная БД, ML-reranker (cross-encoder), OCR,
многосессионная память, полный многошаговый чат, resume/инкрементальная переиндексация,
удаление старых версий индекса, multi-process/Postgres/Docker, автоматическое скачивание
моделей. Эти пункты в D23 не реализуются.

---

## 20. Статус

`SPEC_STATUS: AWAITING_REVIEW`.

Реализация, тесты и `.bat` этой задачей не создаются и не запускаются. Переход к
реализации возможен только после spec review и соответствующего допуска.
