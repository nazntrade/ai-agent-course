# SPEC — Day 24: цитаты, источники и защита от галлюцинаций (Knowledge Agent)

Идентификатор задачи: `day-24-rag-grounding`.
Статус: **реализовано; спецификация синхронизирована с исправлениями D24**.
Документ описывает текущие контракты; фактические результаты и ограничения приёмки
фиксируются отдельно в README, тестовых протоколах и evidence, а не этим статусом.
Дата: 2026-10-02.
Базовое состояние: D21 (`day-21-document-indexing`), D22 (`day-22-first-rag-query`) и
D23 (`day-23-rag-filtering`) реализованы; фактическое состояние — `MODULE_STATE.md`.
Источник задания: Task Contract пользователя (D24) и замороженный acceptance-contract
`d24` (C01…C30). Документ самодостаточен и не требует обращения к переписке.

Компоненты D24 реализованы в репозитории. Формулировки «должно», «обязан» — нормативные
требования; обновление описания не ослабляет исходные критерии и не заменяет проверки.

---

## 1. Назначение

Развить RAG-поток D22/D23 так, чтобы каждый содержательный RAG-ответ **доказывал** свою
опору на документы: показывал использованные источники (`source` и `section`/`chunk_id`) и
**точные цитаты** из фрагментов, которые реально были переданы модели, а также честно
различал три вещи — существование источника, точность цитаты и смысловую поддержку ответа
цитатой. При недостаточном контексте приложение обязано честно сказать, что ответа нет в
доступных документах, и предложить полезное уточнение, не переходя незаметно к общим
знаниям. Технические сбои провайдера не должны маскироваться под «честное не знаю».

Наблюдаемый результат дня 24:

* пользователь задаёт вопрос в RAG-режиме и рядом с ответом видит использованные источники
  (`source`, `section`, `chunk_id`), точные цитаты из этих фрагментов и статус их проверки;
* источниками считаются **только** фрагменты, реально переданные модели после поиска,
  фильтрации D23 и лимита контекста, а не все кандидаты;
* модель может ссылаться только на переданные ей `chunk_id`; неизвестный `chunk_id` не
  подтверждается, а выдуманная цитата не подменяется похожим текстом и не отображается как
  успешно проверенная;
* цитата обязана реально присутствовать в соответствующем фрагменте; допустимая
  нормализация пробелов описана явно; пересказ и приблизительное совпадение цитатой не
  считаются;
* перевод цитаты обозначен как перевод, оригинал сохранён;
* при релевантности ниже порога приложение честно сообщает об отсутствии ответа в
  документах и предлагает уточнение; причина отказа и применённый порог сохранены; пустые
  источники и цитаты допустимы и не выдумываются;
* пустой ответ модели, обрезанная генерация, ошибка формата и недоступность провайдера —
  технические ошибки, а не «честный отказ»;
* сохранены индексирование D21, режим без RAG D22, rewrite/фильтрация/настройки и четыре
  режима сравнения D23; D21–D23 регрессии зелёные;
* на существующем корпусе и `eval/d22/questions.json` (9 вопросов по документу + 1 вне
  корпуса) сохранены 10 фактических результатов новой проверки на одном зафиксированном
  индексе и выбранном профиле, а также набор из восьми граничных случаев; независимый
  Tester сохраняет содержательную оценку каждого результата.

D24 **не заменяет** D21–D23: без-RAG D22 и смысл четырёх режимов D23 сохраняются; D24
добавляет к RAG-ответам слой проверяемого цитирования.

---

## 2. Границы и не-цели

### 2.1. Must (входит в D24)

* `GroundingVerifier` — детерминированная формальная проверка цитат: `source_exists` (ссылка
  на реально переданный `chunk_id`), `quote_verbatim` (точное присутствие цитаты в этом
  фрагменте с явной нормализацией пробелов), сохранение перевода с оригиналом.
* `GroundedAnswer` — структурированный ответ grounded-RAG: текст ответа, список цитат,
  признак честного отказа и необязательное ограничение (частичный ответ).
* Новый промпт `grounded-rag-v1`, где модель обязана вернуть JSON-объект с ответом и
  цитатами, скопированными из переданных фрагментов; контекст остаётся недоверенными
  данными (защита от инструкций внутри документов).
* Явное различение трёх проверок: источник существует, цитата точная, смысл ответа
  подтверждён цитатой. Автоматическая формальная проверка **не** выставляет смысловую
  метку; смысловая поддержка оценивается вручную (Tester) и помечена `meaning_check:
  "not_performed"`.
* Источники ответа = `retrieval.passed` (реально переданные после D23-фильтра и бюджета);
  `source`/`section` берутся из `metadata` индекса.
* Честный отказ при пустом `selected` (ниже порога) и при `insufficient=true` от модели:
  детерминированное сообщение, предложение уточнения, сохранённые `reason` и `threshold`,
  допустимые пустые `sources`/`citations`.
* Различение технических ошибок (пустой ответ, `finish_reason=length`, ошибка формата JSON,
  недоступность/ошибка/таймаут провайдера) и честного отказа.
* HTTP API: `grounding`-поля в `ChatAnswer`, опциональное поле запроса `grounding` **только в
  `ChatRequest`** (compare/compare-modes его не получают, §11.5), опциональный фильтр
  `chunk_id` в `GET /api/index-versions/{id}/chunks` для раскрытия фрагмента (§11.3).
* UI: чат первым блоком, увеличенные поля, настройки, кнопки, метрики, индикатор ожидания
  (сохранены); панель источников и цитат рядом с ответом, раскрытие соответствующего
  фрагмента, перенос длинных строк, раздельные состояния «ошибка проверки» и «недостаток
  информации»; новые элементы — на английском.
* Harness: аддитивный grounded-режим `harness/chat_stub.py` (§16.1), offline-проектор
  `harness/d24_grounding.py`, LIVE-раннер `harness/d24_live.py`, сценарий
  `tests/scenarios/d24-rag-grounding.{py,json}` (`kind: live`).
* Тесты UNIT/INT, offline-границы, регрессия D21–D23, ручной UI.

### 2.2. Later / явные не-цели D24

MCP, Wikipedia importer, ANN/векторная БД, ML-reranker (cross-encoder) и вообще новый
обучаемый компонент, отдельная NLI/entailment-модель для автоматической смысловой проверки,
OCR, многосессионная память, полный многошаговый чат, resume/инкрементальная переиндексация,
удаление версий индекса, multi-process/Postgres/Docker, автоматическое скачивание моделей,
изменение retrieval-логики D21. D24 не реализует автоматическую проверку смысла: это
осознанное ограничение (§7.4).

### 2.3. Границы изменений

* **Не изменяются**: D21 HTTP API (`/api/health`, `/api/collections`, `/api/index/*`,
  `/api/search`, `/api/compare`) по путям, параметрам и формам, схема и семантика индексов,
  происхождение чанков, изоляция коллекций, `KnowledgeService.search`. Единственное
  аддитивное расширение D21-контракта — опциональный `chunk_id` у
  `/api/index-versions/{id}/chunks` (§11.3): протокол `IndexStore.list_chunks`
  (`domain/contracts.py`) и `KnowledgeService.list_chunks` получают проброс этого параметра
  (дополнительный SQL-предикат равенства), схема индекса не меняется.
* **Сохраняются без изменения семантики**: D22 `without_rag` («Без RAG»), D22
  `POST /api/chat/compare` (ветви остаются `rag-v1`/`plain-v1`, grounded-слой к compare **не**
  применяется, §11.5), `/api/chat-runs*`, `chat-run-v1`/`chat-eval-v1`; D23 rewrite,
  фильтрация, настройки, трасса и четыре режима A–D, `POST /api/chat/compare-modes` (имена
  режимов, настройки и трасса не меняются; grounded-слой аддитивен, §11.2/§11.5).
* **Управляемое изменение D24**: при включённом grounding (сервисный параметр
  `grounding_enabled`, в приложении — `RAG_GROUNDING_ENABLED`, по умолчанию `1`) RAG-ответы
  **`/api/chat`, `/api/chat/stream` и `/api/chat/compare-modes`** формируются шаблоном
  `grounded-rag-v1` и несут `answer.grounding`. `POST /api/chat/compare` этим изменением
  **не затрагивается** и всегда использует `rag-v1` (§11.5). При `RAG_GROUNDING_ENABLED=0`
  (или `grounding=false` в `ChatRequest`) восстанавливается ровно прежний D22-путь `rag-v1`
  со свободным текстом. Без-RAG всегда использует `plain-v1` и не несёт `grounding`.
  Прямая сборка `ChatService` без параметра `grounding_enabled` сохраняет D22-поведение
  (`rag-v1`); приложение передаёт настройку через `build_chat_service` (§10), поэтому
  существующие UNIT-тесты D21–D23 остаются зелёными.
* **Аддитивность для app-run INT.** Приложение реально запускается как
  `python -m knowledge_agent` (`knowledge_agent/__main__.py`, `build_chat_service`), а дефолт
  §10 — `RAG_GROUNDING_ENABLED=1`, поэтому app-run INT, поднимающие настоящее приложение, по
  умолчанию попадают в grounded-слой. Стратегия фиксируется явно:
  * существующие D22/D23 app-run INT, проверяющие плоскую форму `rag-v1`/свободный текст,
    пиннятся `RAG_GROUNDING_ENABLED=0`: `tests/integration/test_selected_profile_end_to_end.py`
    (fake-провайдер возвращает свободный текст `Selected answer` и ждёт `200` для `with_rag`,
    строки 64–77), `tests/integration/test_chat_end_to_end.py` (в т. ч. `answer.citations.valid`,
    строка 203) и `tests/integration/test_d23_end_to_end.py` (четыре режима/фильтр/rewrite —
    D23-регрессия);
  * канонический grounded app-run INT — новый
    `tests/integration/test_d24_grounding_end_to_end.py` на дефолтном
    `RAG_GROUNDING_ENABLED=1` (`python -m knowledge_agent` + `embed_stub` + grounded-`chat_stub`):
    grounded `/api/chat`, `/api/chat/stream`, `/api/chat/compare-modes`, `answer.grounding` и
    источники ⊆ `passed`;
  * grounded-стаб (`harness/chat_stub.py`, §16.1) кладёт в поле `answer` grounded-JSON ссылку
    `[chunk_id]` переданного чанка, поэтому D22-проекция `answer.citations.valid` остаётся
    непустой и при `RAG_GROUNDING_ENABLED=1`;
  * `smoke_test.bat` запускает приложение с дефолтом `RAG_GROUNDING_ENABLED=1` и опирается на
    аддитивный grounded-стаб: его D22-ассерты (непустой `answer.text`, `passed_count ≤
    found_count`, закреплённый индекс, compare `rag-v1`) сохраняются.
* Новые настройки добавляются в `config.py`/`.env.example` на этапе реализации; в Git
  попадает только `.env.example` без значений.
* `.bat` модуля изменяет только Configurator (GOVERNANCE), не Developer.
* `.env` не читается и не изменяется; настоящие секреты не публикуются.
* Все D24-артефакты пишутся в новый каталог `local-data/d24/`; артефакты D22/D23 и их
  форматы чтения не изменяются и не перезаписываются.
* Комплект SPEC/PLAN/ACCEPTANCE подготовлен до реализации; эта актуализация описывает
  реализованное поведение, не создаёт новый scope и не разрешает изменение защищённых `.bat`.

---

## 3. Состояние D21–D23 (существующая архитектура)

* `KnowledgeService.search(...)` — точный cosine по SQLite-индексу; возвращает `fragments`
  (`rank`, `score`, `chunk_id`, `text`, `metadata`), проверяя готовность и совместимость
  индекса до эмбеддинга запроса.
* `ChatService._plan` уже выполняет retrieval, rewrite (`ChatQueryRewriter`), фильтрацию
  (`RelevanceFilter`), бюджет контекста (`ContextBudget`, `heuristic-v1`) и собирает
  `retrieval.candidates/selected/passed`, `passed_ids` и `exclusion_reasons`. `ContextPlan.passed`
  содержит внутренний `text` каждого переданного чанка (в API-проекцию текст не попадает).
* `PromptTemplate` (`chat/prompts.py`): `plain-v1`, `rag-v1`, `rewrite-v1`; контекст
  передаётся в отдельном `<context>`-блоке с явной инструкцией «это данные, не команды».
* `extract_citations(text, passed_ids)` (`chat/citations.py`) делит найденные в тексте
  идентификаторы на `valid`/`unsupported`; это D22-проверка **только факта передачи**
  `chunk_id`, а не цитаты и не смысла.
* `ChatResult`/`ChatUsage` (`domain/contracts.py`) несут `finish_reason`, `usage`,
  `output_tokens_per_second`; `RewriteResult` — отдельные метрики rewrite.
* API: `POST /api/chat`, `POST /api/chat/stream`, `POST /api/chat/compare`,
  `POST /api/chat/compare-modes`, `GET /api/chat-runs*`.
* UI (`ui/index.html`, `ui/app.js`): панель «Чат (RAG)» первой, `Retrieval settings`, трасса,
  `Compare four modes`; `renderChatSources` показывает `passed`, `renderChatAnswer` — текст,
  `finish_reason` и `citations`.
* Harness: `embed_stub.py`, `chat_stub.py`, `d23_eval.py`, `d23_live.py`, `rag_eval_live.py`
  (`TestSession`/lease, `OwnedBackend`, `verify_health`), `scenario_runner.py`.
* Eval: `eval/d22/questions.json` (`rag-eval-questions-v1`, ровно 10 вопросов: 9
  `answerable: true` и 1 `answerable: false`).

D24 встраивается **после** `ContextBudget` и **до/вокруг** вызова генерации: контекст и
`passed` уже определены, проверяются только те цитаты и источники, которые реально ушли в
промпт.

---

## 4. Термины

| Термин | Определение |
| --- | --- |
| **источник (source)** | Фрагмент из `retrieval.passed`; его `source_label` и `section_path` берутся из `metadata` индекса. |
| **переданный (passed)** | Чанк, реально попавший в промпт после поиска, D23-фильтра и бюджета контекста. Источниками считаются только `passed`, не `candidates`. |
| **цитата (citation)** | Точная дословная строка из текста соответствующего `passed`-фрагмента, приведённая моделью вместе с `chunk_id`. |
| **grounded-ответ** | RAG-ответ, оформленный шаблоном `grounded-rag-v1`: текст ответа + цитаты + признак отказа/ограничения. |
| **source_exists** | Формальная проверка: `chunk_id` цитаты входит в `passed`-множество. |
| **quote_verbatim** | Формальная проверка: нормализованная цитата является подстрокой нормализованного текста этого фрагмента. |
| **meaning_supported** | Смысловая поддержка утверждения цитатой. D24 её **не** проверяет автоматически; поле `null`, `meaning_check="not_performed"`; оценивает Tester вручную. |
| **нормализация пробелов** | Явное преобразование перед сравнением: NFC, замена U+00A0 и любых Unicode-пробельных последовательностей на один ASCII-пробел, обрезка краёв (§7.2). Сравнение регистрозависимое. |
| **перевод цитаты** | Необязательное поле `translation`: перевод цитаты, использованный в ответе; `quote` при этом хранит оригинал, `is_translation=true`. |
| **выдуманная цитата** | Цитата, которой нет в соответствующем фрагменте (в т. ч. при верном `chunk_id`). Никогда не исправляется подстановкой и не помечается `verified`. |
| **честный отказ** | Ответ `200` с `insufficient_sources=true`: приложение сообщает, что ответа нет в доступных документах, и предлагает уточнение. |
| **техническая ошибка** | Пустой ответ, обрезанная генерация, ошибка формата grounded-JSON, ошибка/таймаут/недоступность провайдера. Не является честным отказом. |
| **исчерпывающая проверка** | Формальные проверки D24; не заменяют смысловую оценку. |

---

## 5. Архитектура, компоненты и направление зависимостей

### 5.1. Компоненты (реализовано)

* **`GroundingVerifier`** (`knowledge_agent/chat/citations.py`, расширение) — чистый
  детерминированный модуль над `passed`-чанками: нормализует цитату, проверяет
  `source_exists` и `quote_verbatim`, сохраняет перевод/оригинал, формирует
  `Citation`/`GroundingResult`. Не знает про сеть, HTTP и БД.
* **`GroundedAnswer`/`Citation`/`GroundingResult`** (контракты в
  `knowledge_agent/domain/contracts.py`) — структуры grounded-слоя и их `to_dict()`.
* **`grounded-rag-v1`** (`knowledge_agent/chat/prompts.py`) — `PromptTemplate` (по образцу
  `rag-v1`), требующий JSON-ответа с `answer` и `citations`; `rag-v1`/`plain-v1`/`rewrite-v1`
  сохраняются.
* **`parse_grounded_response`** (`knowledge_agent/chat/citations.py` или `chat_service.py`) —
  строгий разбор JSON без «починки»: неверный формат → типизированная `ChatInvalidResponse`.
* **Расширение `ChatService`** (`chat/chat_service.py`) — включение grounded-шаблона,
  сохранение внутренних `passed`-чанков с текстом, проверка цитат, статус `grounding`,
  честный отказ, `limitation`, различение технических ошибок; всё аддитивно к D23.
* **API** (`api/routes.py`) — опциональное поле `grounding` **только** в `ChatRequest`
  (`CompareRequest`/`CompareModesRequest` его не получают), `grounding` в `ChatAnswer`,
  опциональный `chunk_id` в списке чанков (§11.3).
* **UI** (`ui/index.html`, `ui/app.js`, `ui/styles.css`) — панель источников и цитат,
  раскрытие фрагмента, перенос строк, раздельные состояния.
* **Harness** — `harness/chat_stub.py` (существующий; аддитивно получает детерминированные
  grounded-ответы: grounded-режим выбирается по маркеру grounded-шаблона в system-сообщении,
  иначе сохраняются прежние D22/D23-ответы; граничные варианты — через заголовок
  `X-Stub-Fail`, §16.1), `harness/d24_grounding.py` (offline-проекции и граничные фикстуры),
  `harness/d24_live.py` (10 результатов + 8 граничных случаев),
  `tests/scenarios/d24-rag-grounding.{py,json}`.

### 5.2. Направление зависимостей

`GroundingVerifier` и структуры grounded-слоя живут в ядре и не зависят от провайдера.
`ChatService` зависит от контракта `ChatModel` и вызывает верификатор; HTTP/UI зависят от
API. `KnowledgeService.search` и D21 domain не меняются; `KnowledgeService.list_chunks` и
`IndexStore.list_chunks` аддитивно расширяются опциональным `chunk_id` (§11.3) без изменения
схемы индекса.
Загрузка grounded-текста провайдера и разбор JSON сосредоточены в одном месте, чтобы
форматная ошибка была типизирована, а не «размазана» по слоям.

### 5.3. Место D24 в существующем потоке

```
вопрос ─► [D23: rewrite ─► search ─► filter ─► ContextBudget] ─► passed + тексты
                                                                    │
                              grounded-rag-v1: контекст = passed (недоверенные данные)
                                                                    ▼
                                      ChatModel ─► JSON {answer, citations, insufficient}
                                                                    │
                       parse_grounded_response ──(формат/пусто/length)──► техническая ошибка
                                                                    ▼
                       GroundingVerifier(passed) ─► Citation[] + статусы (source_exists/quote_verbatim)
                                                                    ▼
                              answer {text, grounding{citations, status, reason, threshold}}
                                                                    │
                                       retrieval.passed = источники для UI (без текста)
```

* `grounding=false` или `RAG_GROUNDING_ENABLED=0` → прежний путь `rag-v1` со свободным
  текстом; `answer.grounding` отсутствует.
* `mode=without_rag` → `plain-v1`, `grounding` отсутствует, источников нет.
* Бюджет контекста учитывает фактический выбранный системный промпт: `GROUNDED_RAG.system`
  при grounding, `RAG.system` без него, а не старый короткий промпт для обоих путей.
  `mandatory_texts=[template.system, question]`; резерв ответа и safety margin сохраняются.
  Оценка остаётся `heuristic-v1`, не точным tokenizer-подсчётом. Если обязательные данные
  не помещаются, возвращается `context_overflow` (422) до вызова модели.
* Пустой `selected` при `use_filter=true` → детерминированный отказ **до** вызова модели
  (как D23), расширенный `reason="below_threshold"` и `threshold`.

---

## 6. Контракты данных

### 6.1. Grounded-ответ модели (`grounded-rag-v1`, реализовано)

```json
{
  "answer": "<текст ответа на языке вопроса; для опоры на фрагмент использует [chunk_id]>",
  "citations": [
    {
      "chunk_id": "<id из контекста>",
      "quote": "<дословная строка из этого фрагмента, язык оригинала>",
      "translation": "<необязательный перевод цитаты>"
    }
  ],
  "insufficient": false,
  "limitation": null
}
```

Инструкции генерации требуют краткого ответа и прямой опоры каждого содержательного
фактического утверждения на связанную дословную цитату. Имена и фактические числа
допустимы, только если они присутствуют в соответствующей цитате; свойство нескольких
подходов требует отдельного подтверждения для каждого. При частичном контексте модель
возвращает только подтверждённую часть с `limitation`, при отсутствии ответа —
`insufficient=true`. Это инструкция модели, а не автоматическая смысловая проверка:
`meaning_check="not_performed"` сохраняется, семантический вывод требует отдельной оценки.
Правила разбора:

* Модель обязана вернуть **один** JSON-объект. Допускается обрамление ровно одним блоком
  ```json … ```; содержимое блока разбирается как есть.
* `answer` — непустая строка; `citations` — массив объектов с непустыми `chunk_id` и `quote`;
  `translation` — строка либо отсутствует; `insufficient` — boolean (default `false`);
  `limitation` — строка либо `null`.
* Любое отклонение от формата (не JSON, обрезанный/незавершённый JSON, отсутствует
  `answer`, `answer` пуст, `citations` не массив, поля неверного типа) — **ошибка формата** →
  `chat_invalid_response` (503) с `details.format="grounded_json"`, запись не выглядит
  успешной. Текст не «дочиняется» и кавычки не заменяются.
* **Порядок (приоритет)**: сначала выполняется разбор формата, затем классификация
  `finish_reason`. Если разбор не удался, ошибка формата возвращается **независимо** от
  `finish_reason` (§8.2). Только **успешно разобранный** grounded-объект с
  `finish_reason="length"` переходит в ветку «обрезанная генерация»: `truncated=true`,
  `grounding.status="failed"`, `reason="truncated_generation"`. Пустой ответ модели — всегда
  `chat_invalid_response`. Ошибка формата, пустой ответ и `finish_reason=length` —
  технические ошибки, а не отказ (§8). Формат проверяется **до** верификации и до
  классификации truncated.

### 6.2. `Citation` (проекция проверки, реализовано)

```json
{
  "chunk_id": "<id>",
  "source": "<metadata.source_label>",
  "section": "<metadata.section_path>",
  "page_start": 3,
  "page_end": 4,
  "quote": "<оригинал>",
  "translation": null,
  "is_translation": false,
  "source_exists": true,
  "quote_verbatim": true,
  "meaning_supported": null,
  "status": "verified",
  "reason": null
}
```

* `source`/`section` берутся **только** из `metadata` переданного чанка; текст модели для
  них не используется (R-04).
* `source_exists=false` → `status="unknown_chunk_id"`, `reason="chunk_id_not_passed"`.
* `source_exists=true`, но цитата не найдена → `status="quote_mismatch"`,
  `reason="quote_not_in_chunk"`.
* `is_translation=true` при наличии непустого `translation`; `quote` всегда оригинал.
* `meaning_supported` в D24 всегда `null` (`meaning_check="not_performed"`); метка смысла не
  выставляется формальной проверкой (R-08).

### 6.3. `GroundingResult` в `answer.grounding` (реализовано)

```json
{
  "status": "verified",
  "reason": null,
  "threshold": 0.45,
  "meaning_check": "not_performed",
  "limitation": null,
  "citations": [{"...": "Citation §6.2"}],
  "refusal": null,
  "inline_unsupported": [],
  "inline_missing_quote": []
}
```

`status`:

| Значение | Когда |
| --- | --- |
| `verified` | `insufficient=false`, ответ не обрезан, структурированные `citations` непусты и все `verified`, нет нарушений inline-ссылок, `(limitation or "").strip()` пуст |
| `partial` | есть хотя бы одна `verified` структурированная цитата и есть неуспешная цитата, нарушение inline-ссылки либо непустое после trim ограничение |
| `failed` | нет структурированных цитат (`no_citations` независимо от `limitation`), либо ни одной подтверждённой цитаты, либо ответ обрезан (`truncated_generation`) |
| `refused` | честный отказ (§8), пустые или неполные цитаты допустимы |
| `not_checked` | grounding выключен |

`reason` — устойчивый код: `null`, `no_citations`, `quote_mismatch`, `unknown_chunk_id`,
`unsupported_citation`, `missing_quote`, `truncated_generation`, `below_threshold`,
`model_insufficient`. `threshold` — применённый
порог D23 (число). `refusal` — `{"reason", "message", "threshold"}` при `status="refused"`,
иначе `null`.

`inline_unsupported` содержит распознанные ссылки `[chunk_id]` из текста ответа,
не входящие в реально переданные чанки. `inline_missing_quote` содержит ссылки на
переданные чанки без соответствующей **verified** структурированной цитаты.
При непустых citations `unsupported_citation` имеет приоритет над `missing_quote`;
они исключают общий `verified` (при наличии подтверждённых цитат — `partial`, иначе `failed`).
Если citations пусты, приоритет — `failed/no_citations`, даже при непустом limitation.
Пустая строка и пробельный limitation сами по себе не понижают корректный ответ до partial;
непустое ограничение даёт partial только при наличии хотя бы одной подтверждённой цитаты.
Честный `insufficient=true` остаётся refused; массивы нарушений сохраняются для диагностики.
Проверка inline-ссылок не является автоматической проверкой смысла.

`retrieval.passed[]` остаётся D23-проекцией (без `text`); UI использует его как список
источников, а `answer.grounding.citations[]` — как проверенные цитаты.

### 6.4. Совместимость `ChatAnswer`

* Плоская форма D22 сохраняется: `schema_version`, `run_id`, `mode`, `usage`, `latency_ms`
  (`retrieval`/`context`/`chat`/`total`), `retrieval`, `answer.text`, `answer.finish_reason`,
  `answer.truncated`, `answer.citations.{valid,unsupported}`, `answer.insufficient_sources`.
* `answer.grounding` — **аддитивное** поле, присутствует только при grounded-RAG; для
  `without_rag`/выключенном grounding отсутствует.
* `answer.citations.{valid,unsupported}` сохраняют D22-смысл (идентификаторы в тексте ответа
  и `passed_ids`) и вычисляются как раньше; richer-проверка живёт в `answer.grounding`.
* `POST /api/chat/compare-modes`: у каждой ветки `branch.answer.grounding` (§11.2);
  `comparison.prompt_templates.generation` отражает фактический шаблон; имена режимов,
  настройки и трасса D23 не меняются, grounded-слой аддитивен.
* `POST /api/chat/compare`: `answer.grounding` **не добавляется**; ветви остаются
  `rag-v1`/`plain-v1` (§11.5).

---

## 7. Семантика проверки цитат

### 7.1. Три проверки (R-08)

1. **Источник существует** — `chunk_id` входит в реально переданные `passed_ids`
   (`source_exists`).
2. **Цитата точная** — нормализованная цитата является подстрокой нормализованного текста
   этого фрагмента (`quote_verbatim`).
3. **Смысл ответа подтверждается цитатой** — D24 **не** проверяет это автоматически:
   `meaning_supported=null`, `meaning_check="not_performed"`. Смысловую поддержку оценивает
   Tester вручную для 10 результатов (R-19/§14). UI и документация не должны утверждать,
   что смысл проверен автоматически.

Совпадение текста доказывает только проверки 1–2 и никогда не выдаётся за проверку 3.

### 7.2. Нормализация пробелов (R-05)

Перед сравнением цитата и текст фрагмента преобразуются одной функцией:

1. Unicode NFC;
2. символ U+00A0 (NBSP) и любые последовательности Unicode-пробельных символов
   (`\s+`, включая переводы строк и табуляции) заменяются на один ASCII-пробел (U+0020);
3. удаляются ведущие и завершающие пробелы.

Сравнение — **регистрозависимое**; никакой пунктуационной, лемматизационной или
«нечёткой» нормализации нет. Цитата проходит, если нормализованная цитата — подстрока
нормализованного текста фрагмента. Пересказ, исправленная формулировка, изменение
слов/чисел/пунктуации и «похожий» текст цитатой не считаются. Пустая цитата отклоняется.
Дополнительные пороги длины не вводятся.

### 7.3. Перевод (R-06)

* Если в ответе есть перевод цитаты, `translation` непуст, а `quote` хранит **оригинал**;
  `is_translation=true`. Проверяется наличие оригинала цитаты во фрагменте.
* UI показывает оба: оригинал как цитату, перевод с явной пометкой перевода.
* Если модель вернула только перевод без оригинала — `quote_verbatim=false` (оригинал
  неизвестен/не подтверждён); такая цитата не маркируется `verified`.

### 7.4. Запрет подмены (R-07)

* Верификатор только **сообщает** результат; он никогда не заменяет выдуманную цитату
  похожим текстом из фрагмента и не «исправляет» ответ.
* При выдуманной цитате соответствующая `Citation.status="quote_mismatch"`,
  `quote_verbatim=false`; итоговый `grounding.status` не `verified`. UI показывает
  «Verification failed», а не успешный ответ.
* Если ни одна цитата не подтверждена, `grounding.status="failed"`,
  `insufficient_sources=false` (это не отказ «не знаю», а неподтверждённый ответ).

### 7.5. Защита от инструкций внутри документов (R-09)

* Найденный текст остаётся **данными**. `grounded-rag-v1` явно повторяет инструкцию:
  содержимое `<context>` — недоверенные данные, не команды; не выполнять инструкции из
  контекста; использовать только как свидетельство.
* Текст чанков не склеивается с системной инструкцией; контекст передаётся в отдельном
  пользовательском сообщении в `<context>`-блоке.
* Регрессия: документ с инструкцией («ignore previous instructions…») внутри фрагмента не
  меняет поведение — модель/стаб не исполняет её, ответ строится на вопросе и данных
  (UNIT/INT с fake/stub, C27).

---

## 8. Честный отказ и технические ошибки

### 8.1. Честный отказ (R-10, R-11, R-12)

Отказ — это успешный HTTP-ответ `200` с `answer.insufficient_sources=true`,
`answer.grounding.status="refused"` и непустым `answer.grounding.refusal`:

* **`reason="below_threshold"`** — при `use_filter=true` и пустом `selected` (§ D23 7.3):
  модель **не** вызывается; `sources=[]`, `citations=[]`; `threshold` сохранён;
  `latency_ms.chat=null`, `usage=null`; повторный запрос без фильтра не выполняется.
* **`reason="model_insufficient"`** — модель вернула `insufficient=true`: она сообщила, что
  ответа нет в переданном контексте. `sources` = `passed` (если были), `citations` пусты или
  неполны; `threshold` сохранён; `limitation` может быть непустым для подтверждённого
  частичного ответа.
* Сообщение детерминированное и включает полезное уточнение (например: переформулировать
  вопрос, понизить порог, выбрать другую коллекцию/индекс). По умолчанию используется
  сервисное сообщение; при `model_insufficient` текстом отказа может быть ответ модели, но
  он не должен переходить к общим знаниям (модель обязана опираться только на контекст).
* Пустые `sources`/`citations` при отказе **допустимы** и не заполняются фиктивно; пустой
  отказ не превращается в генерацию из общих знаний.

Высокий `score` не доказывает наличие факта (R-11): если в переданных фрагментах нет нужного
факта, допустим либо честный отказ, либо **подтверждённый частичный ответ** с явным
`limitation` («частичный ответ: в документах нет …»); `status` при этом `partial`, а не
`verified`.

### 8.2. Технические ошибки (R-14)

| Ситуация | Поведение |
| --- | --- |
| Пустой ответ модели | `chat_invalid_response` (503); запись не выглядит успешной |
| Ошибка формата grounded-JSON (в т. ч. обрезанный JSON) | `chat_invalid_response` (503) с `details.format="grounded_json"` |
| `finish_reason="length"` и grounded-JSON **успешно разобран** | `answer.truncated=true`; `grounding.status="failed"` с `reason="truncated_generation"`; **не** `insufficient_sources` |
| `finish_reason="length"` и grounded-JSON **не разобран** | ошибка формата (§6.1): `chat_invalid_response` (503); формат имеет приоритет над truncated |
| Недоступность/ошибка/таймаут провайдера | типизированные `chat_unavailable`/`chat_timeout`/`chat_model_missing` (503), не маскируются отказом |
| Ошибки индекса/retrieval | D22/D23-коды (`index_*`, `embedding_*`), не маскируются |

**Порядок обработки:** формат → truncated → верификация. Обрезанный, но успешно разобранный
объект не получает `verified`; неразобранный JSON не классифицируется как
`truncated_generation`, а является ошибкой формата. Техническая ошибка никогда не
выставляется как честный отказ «не знаю», и наоборот. Обрезанный или неподтверждённый ответ
не получает `grounding.status="verified"`.

### 8.3. Таксономия ошибок (дополнение)

Форма как D21/D22: `{"error": {"code", "message", "details"?}}`.

| Код | HTTP | Ситуация |
| --- | --- | --- |
| `chat_invalid_response` | 503 | пустой ответ модели, ошибка формата grounded-JSON |
| `chat_unavailable`/`chat_timeout`/`chat_model_missing`/`chat_length_error` | 503 | D22-ошибки провайдера |
| `invalid_request`/`invalid_threshold` | 422 | D23-валидация без изменений |
| `index_not_ready`/`index_incompatible`/`embedding_*` | 409/503 | retrieval D21/D22 без изменений |

Сообщения санитизированы: ключи/содержимое `.env`, base URL, lease ID и абсолютные
локальные пути не попадают в ответы, записи и отчёты.

---

## 9. Метрики

Плоская форма D22/D23 сохраняется: `usage` генерации (`input_tokens`/`output_tokens`/
`total_tokens` либо `null`), `latency_ms.{retrieval,context,chat,total}`, `rewrite.usage`/
`rewrite.latency_ms`. Grounded-слой не вводит новых таймеров и не смешивает метрики:
проверка цитат — детерминированная и не считает токены. `output_tokens_per_second`
относится к генерации; недоступные значения — `null`/«н/д».

---

## 10. Конфигурация

Настройки реализованы в `config.py`/`.env.example` при
реализации; настоящий `.env` не читается и не изменяется:

| Переменная | Default | Назначение |
| --- | --- | --- |
| `RAG_GROUNDING_ENABLED` | `1` | RAG-ответы `/api/chat`, `/api/chat/stream`, `/api/chat/compare-modes` используют `grounded-rag-v1` и несут `answer.grounding`; `POST /api/chat/compare` исключён; `0` возвращает D22-путь `rag-v1` со свободным текстом |
| `D24_DATA_PATH` | `local-data/d24` | Каталог артефактов D24 |
| `D24_QUESTIONS_PATH` | `eval/d22/questions.json` | Контрольные 10 вопросов (9+1) |

Правила: `RAG_GROUNDING_ENABLED=0` даёт ровно прежнее поведение D22 `with_rag` (свободный
текст, без `grounding`). Явное поле запроса `grounding` имеет приоритет над настройкой.
Без-RAG настройкой не затрагивается. `load_settings(env)` остаётся тестируемым и не читает
`.env`. Модели не скачиваются; chat-профиль — выбранный `AI_TEST_MODEL_*`.

### 10.1. Политика тестового профиля и LIVE (нормативно, D24 §6; покрывает S01)

Этот подраздел — нормативное требование; он обязателен для реализации и проверок D24.

* **Проверка policy до реальных действий.** Точка входа
  `test.bat scenario d24-rag-grounding` использует `harness/scenario_runner.py`, который
  требует **ровно** `AI_TEST_LIVE_POLICY=allowed` (`scenario_runner.py:37–43`,
  `validate_live_profile`): отсутствующая, пустая, legacy или иная политика → `ScenarioBlocked`
  → сценарий `BLOCKED`, реальные вызовы `NOT_RUN`. Раннер сообщает `D24_RUNNER_STATUS` через
  `report_live_blocked(...)` до acquire/inference. **«Standalone-авторизация вне панели» к этой
  точке входа не применяется**: сценарный запуск открывается только явным
  `AI_TEST_LIVE_POLICY=allowed` с полным `AI_TEST_MODEL_*`-профилем; без него — `BLOCKED`, без
  fallback и без обхода mock-прогоном.
* **`forbidden`/невалидная policy → BLOCKED.** Реальные вызовы помечаются `NOT_RUN`; offline
  часть выполняется; mock/stub не выдаётся за PASS; LIVE фиксируется как `BLOCKED`/`PARTIAL`.
  Неподдерживаемое значение policy трактуется как fail-closed (тоже `BLOCKED`).
* **Неполный профиль → BLOCKED без fallback.** Частичный, stub-only или отсутствующий
  `AI_TEST_MODEL_*` (KIND/BASE_URL/NAME/API_KEY) — явная ошибка до реализации/прогона, без
  молчаливой замены модели. `forbidden` не обходится другой моделью/fallback.
* **Lease acquire/use/release в `finally`.** Локальный управляемый runtime берёт lease через
  `AI_TEST_MODEL_LEASE_URL`, передаёт дочерним процессам готовый профиль
  (`AI_TEST_MODEL_PARENT_READY=1`) и освобождает свой lease в `finally` при успехе, ошибке и
  штатном прерывании. Дочерний процесс не освобождает lease родителя; borrowed/используемая
  другим потребителем модель не выгружается. Remote-профиль локально **не** запускается и не
  останавливается.
* **`MODEL_CHECK_KIND`.** `NETWORK` для выбранного remote-профиля (`deepseek/deepseek-flash`),
  `LOCAL` для local; без реального inference статус не выставляется. Токены проверяемой
  модели не смешиваются с токенами OpenCode-агента; один вызов не учитывается дважды.
* **Точка входа.** `test.bat scenario d24-rag-grounding` запускает
  `tests/scenarios/d24-rag-grounding.{py,json}` с `{"schema_version":"test-scenario-v1",
  "kind":"live"}` и `harness/d24_live.py`; offline-часть — `test.bat unit`,
  `test.bat integration`, `smoke_test.bat`.
* **Offline ≠ LIVE.** Обычные UNIT/INT выполняются offline, изолированы от сети и настоящего
  `.env` (mock/stub/fake никогда не маркируются как inference или LIVE). LIVE запускается
  только явным сценарием в рамках действующей policy; повторные платные прогоны
  автоматически не инициируются. Секреты, base URL, lease ID и абсолютные пути не
  публикуются.

---

## 11. HTTP API (единый источник истины)

HTTP-слой — FastAPI с OpenAPI 3.1. OpenAPI описывает параметры и общую объектную форму
ответов; вложенные поля `answer.grounding` пока не имеют отдельной строгой response-схемы
в OpenAPI. Их нормативный контракт задан §6.3 и подтверждается runtime UNIT/INT, а не
самим существованием OpenAPI-файла. Строгая типизация этой вложенной схемы не является
обязательным критерием D24.
**Этот раздел — единственный источник истины для
новых методов, путей, параметров и форм ответов.** `PLAN.md` ссылается сюда и не дублирует
пути.

### 11.1. `POST /api/chat` и `POST /api/chat/stream`

К `ChatRequest` (D23 §11.1) добавляется опциональное поле:

```json
{ "grounding": true }
```

* `grounding` — boolean либо `null`; default — параметр `grounding_enabled` экземпляра
  `ChatService`, который приложение заполняет из `RAG_GROUNDING_ENABLED` (§10). При
  `mode=without_rag` игнорируется.
* `prompt.template_id` в ответе — **фактический** шаблон генерации: `grounded-rag-v1` при
  включённом grounding, `rag-v1` при выключенном, `plain-v1` для `without_rag`.
* Тело ответа — прежний `ChatAnswer` (`chat-run-v1`): плоские метрики, `retrieval` D23,
  `answer` D22 дополнен `answer.grounding` (§6.3) при grounded-RAG.
* `POST /api/chat/stream`: порядок событий прежний (`start` → [`sources`] → `token`* →
  `done`/`error`); `done` несёт полный ответ с `answer.grounding`.
* D22-совместимый запрос (без `grounding`) при `RAG_GROUNDING_ENABLED=0` (или при сборке
  `ChatService` без `grounding_enabled`) не меняет `retrieval.found`/`found_count` и
  `usage`/`latency_ms.chat`.

### 11.2. `POST /api/chat/compare-modes`

Тело запроса (`CompareModesRequest`) не меняется: поля `grounding` в нём нет; grounding
следует параметру `grounding_enabled` экземпляра `ChatService` (в приложении —
`RAG_GROUNDING_ENABLED`, §10). При включённом grounding каждая из четырёх ветвей
`modes[].branch.answer` несёт `answer.grounding`, а `modes[].branch.prompt.template_id`
равен `grounded-rag-v1`; при выключенном — прежняя D22/D23-форма (`rag-v1`, без `grounding`).
`comparison.prompt_templates.rewrite` остаётся `rewrite-v1`, а
`comparison.prompt_templates.generation` отражает **фактический** шаблон генерации
(`grounded-rag-v1` или `rag-v1`). Имена режимов, настройки (одна модель, порог/топ-K) и
трасса D23 не меняются; grounded-слой аддитивен.

### 11.3. `GET /api/index-versions/{index_version_id}/chunks`

Добавляется опциональный фильтр `chunk_id` (строка). Расширение **аддитивно** и охватывает
ядро, сервис и хранилище: в протокол `IndexStore.list_chunks` (`domain/contracts.py:377–385`)
добавляется параметр `chunk_id: str | None = None`; `KnowledgeService.list_chunks`
(`service/knowledge_service.py`, типизация `IndexStore`/`Embedder`/`Chunker`/`Tokenizer` —
импорты строки 20–29, конструктор 54–69) пробрасывает его в хранилище; `SqliteIndexStore.list_chunks`
(`storage/sqlite_store.py`) применяет дополнительный SQL-предикат `chunk_id = ?`; схема индекса
не меняется. Значение по умолчанию `None` сохраняет прежние реализации и вызовы без изменений.

* без `chunk_id` поведение D21 не меняется: `offset`/`limit`/`document_id`/`section_path` и
  форма ответа прежние;
* с `chunk_id` возвращаются только совпадающие чанки (0 или 1) с полным `text` и `metadata`;
* `total` — число совпадений после применения **всех** фильтров (0 или 1), не общее число
  чанков версии; `offset`/`limit` при этом продолжают применяться (при `offset ≥ 1` и одном
  совпадении `items` пуст);
* неизвестный `chunk_id` — это не ошибка: `items=[]`, `total=0`, HTTP `200`;
* `filters` в ответе эхом возвращает `chunk_id` вместе с `document_id` и `section_path`.

Используется UI для раскрытия соответствующего цитате фрагмента; D21-контракт сохраняется
(параметр опционален, другие параметры/формы не меняются).

### 11.4. Существующие endpoint-ы

`GET /api/health`, `/api/collections`, `/api/index/*`, `/api/search`, `/api/compare`,
`/api/chat-runs*` — без изменений контракта. `POST /api/chat/compare` сохраняет семантику
D22 и **не** затрагивается grounding (§11.5). У `GET /api/index-versions/{id}/chunks`
добавлен только опциональный параметр `chunk_id` (§11.3); остальные `D21`-эндпоинты
`/api/index-versions/*` не меняются.

### 11.5. Матрица endpoint-ов по grounded-слою (R1)

| Endpoint | Поле запроса `grounding` | Поведение при `RAG_GROUNDING_ENABLED=1` (default) | `prompt`/`comparison` | Регресс-ассерты |
| --- | --- | --- | --- | --- |
| `POST /api/chat` (`with_rag`) | да (bool\|null) | `grounded-rag-v1`; `answer.grounding` | `prompt.template_id="grounded-rag-v1"` | существующие UNIT не проверяют `template_id` для `/api/chat`; app-run INT D22/D23 пиннятся `RAG_GROUNDING_ENABLED=0`, grounded `/api/chat` покрывает `tests/integration/test_d24_grounding_end_to_end.py`; при `grounding=false`/дефолте сервиса — `rag-v1` |
| `POST /api/chat/stream` | да (bool\|null) | как `/api/chat`; `done` несёт `answer.grounding` | `prompt.template_id` фактический | порядок событий прежний; smoke не проверяет шаблон stream |
| `POST /api/chat/compare` | **нет** | **не применяется**: ветви `rag-v1`/`plain-v1`, `answer.grounding` отсутствует | `comparison.prompt_templates.with_rag="rag-v1"`, `.without_rag="plain-v1"` | `harness/smoke.py:258` и `tests/integration/test_chat_end_to_end.py:231` остаются зелёными |
| `POST /api/chat/compare-modes` | **нет** | `grounded-rag-v1` в каждой ветви; `modes[].branch.answer.grounding` | `comparison.prompt_templates.generation` = `grounded-rag-v1`, `rewrite="rewrite-v1"` | D23 unit-тесты собирают `ChatService` без параметра → `rag-v1`; имена режимов/настройки/трасса не меняются |

`comparison.prompt_templates` для compare остаётся парой `with_rag`/`without_rag`; для
compare-modes — парой `rewrite`/`generation`. `mode=without_rag` всегда `plain-v1` и без
`grounding`. `grounding=false` в `ChatRequest` или `grounding_enabled=False` возвращают
`rag-v1`-путь D22.

**App-run additivity.** Поскольку `python -m knowledge_agent` использует дефолт
`RAG_GROUNDING_ENABLED=1` (§10), grounded `/api/chat` проверяется отдельным app-run INT
`tests/integration/test_d24_grounding_end_to_end.py`, а существующие D22/D23 app-run INT
(`test_selected_profile_end_to_end.py`, `test_chat_end_to_end.py`, `test_d23_end_to_end.py`)
пиннятся `RAG_GROUNDING_ENABLED=0` и остаются плоской `rag-v1`-регрессией; `smoke_test.bat`
работает на дефолте через аддитивный grounded-`chat_stub`, который кладёт `[chunk_id]` в поле
`answer` (§2.3, §16.1).

---

## 12. UI

### 12.1. Язык

Новые элементы D24 (панель цитат, статусы проверки, пометка перевода, раскрытие фрагмента,
сообщения о проверке/недостатке информации) пишутся **по-английски**. Существующие
русскоязычные панели D22/D23 («Чат (RAG)», «Без RAG», «С RAG», «Сравнить», `Retrieval
settings`) в этой задаче **не переводятся** (`AGENTS.md`: не переводить намеренно ещё не
переведённые экраны). Язык ответа — по языку вопроса пользователя.

### 12.2. Наблюдаемое поведение

* **Чат первым блоком** (существующая панель «Чат (RAG)») остаётся первой; увеличенные поля
  вопроса/ответа, настройки, кнопки, метрики и индикатор ожидания сохраняются.
* **Sources & citations** (новый блок, англ.) рядом с ответом: для каждого источника —
  `source` (`source_label`), `section` (`section_path`), `chunk_id`, страницы; для каждой
  цитаты — оригинал, пометка перевода (и перевод), статус `verified`/`quote_mismatch`/
  `unknown_chunk_id`.
* **Show fragment** (раскрытие): клик раскрывает соответствующий фрагмент (через
  `GET /api/index-versions/{id}/chunks?chunk_id=…`), чтобы пользователь мог сверить цитату.
* **Long lines**: длинные цитаты/`chunk_id` переносятся (CSS `overflow-wrap`), без
  горизонтальной обрезки.
* **Раздельные состояния** (R-15): «Verification failed» (некорректная цитата/неизвестный
  `chunk_id`) и «No relevant sources found / not available in the provided documents»
  (недостаток информации) отображаются разными сообщениями.
* **Meaning status**: UI явно показывает «Meaning support: not checked» и не выдаёт
  формальную проверку за смысловую.
* **Compare four modes**: в каждой колонке — тексты, источники и цитаты ветки с их статусами.
* Понятные ошибки: `chat_invalid_response` (формат/пустой ответ), `truncated` (обрезано),
  `chat_*`, `index_*`, `invalid_threshold`.

---

## 13. LIVE-оценка и сохраняемые артефакты

### 13.1. Место (фиксировано)

```
week-05/knowledge-agent/local-data/d24/
  grounding-results.json   # d24-grounding-v1 — 10 фактических результатов
  edge-cases.json          # d24-edge-cases-v1 — 8 граничных случаев
  quality-assessment.md    # независимая содержательная оценка (Tester, MANUAL)
```

`local-data/` уже в `.gitignore`; корпус, индексы и результаты в Git не попадают, но
объявленные артефакты сохраняются и предъявляются для приёмки. Артефакты D22/D23
(`local-data/chat-runs/…`, `local-data/d23/…`) и их форматы чтения **не изменяются**.

### 13.2. `grounding-results.json` (`d24-grounding-v1`)

```json
{
  "schema_version": "d24-grounding-v1",
  "created_at": "<iso8601>",
  "corpus_label": "agents-survey.pdf",
  "source_sha256": "<sha256>",
  "index_version_id": "<один закреплённый id>",
  "index": {"collection_id": "<id>", "index_version_id": "<тот же id>",
            "strategy": "structure", "fingerprint": "<sha256>"},
  "model": {"provider": "openai-compatible", "model": "deepseek/deepseek-flash",
            "kind": "remote", "settings": {"temperature": 0, "seed": 0,
            "num_predict": 4096, "num_ctx": 8192}},
  "embedding": {"model": "<embedding model>", "dimension": 768, "digest": "<...>"},
  "grounding": {"enabled": true, "meaning_check": "not_performed",
                "whitespace_normalization": "nfc-collapse-trim-v1"},
  "threshold": 0.45,
  "results": [
    {
      "question_id": "D22-Q01",
      "question": "<question>",
      "answerable": true,
      "run_id": "<id>",
      "index_version_id": "<тот же id>",
      "answer_text": "<text>",
      "finish_reason": "stop",
      "truncated": false,
      "insufficient_sources": false,
      "grounding_status": "verified",
      "grounding_reason": null,
      "limitation": null,
      "verification": {"source_exists": true, "quote_verbatim": true,
                       "meaning_supported": null, "meaning_check": "not_performed"},
      "sources": [
        {"chunk_id": "<id>", "source": "<source_label>", "section": "<section_path>",
         "page_start": 3, "page_end": 4, "rank": 1, "score": 0.61}
      ],
      "passed_chunk_ids": ["<id>"],
      "citations": [{"...": "Citation §6.2"}],
      "refusal": null,
      "error": null,
      "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
      "latency_ms": {"retrieval": 0.0, "context": 0.0, "chat": 0.0, "total": 0.0},
      "errors": []
    }
  ],
  "summary": {"...": "агрегаты §14, необязательно"}
}
```

Инварианты: `results` содержит **не менее 10** записей; `question_id` покрывают ровно 10
вопросов `eval/d22/questions.json` (9 `answerable:true` + 1 `answerable:false`); все записи
получены на **одном** `index_version_id`; `index_version_id` и `index.index_version_id`
совпадают. `sources`/`passed_chunk_ids` отражают только `passed`; при честном отказе
допустимы пустые `sources`/`citations`, но `refusal.reason` и `refusal.threshold` сохранены.

### 13.3. `edge-cases.json` (`d24-edge-cases-v1`)

```json
{
  "schema_version": "d24-edge-cases-v1",
  "created_at": "<iso8601>",
  "index_version_id": "<pinned id|null>",
  "cases": [
    {"case_id": "unknown_chunk_id", "level": "UNIT", "scenario": "<...>",
     "expected": "<...>", "observed": "<...>", "status": "PASS"}
  ]
}
```

Обязательные восемь `case_id` (C27):

| `case_id` | Проверяемая ситуация | Уровень |
| --- | --- | --- |
| `unknown_chunk_id` | неизвестный `chunk_id` не подтверждается | UNIT (+INT) |
| `fabricated_quote_real_id` | выдуманная цитата при настоящем ID не становится `verified` | UNIT (+INT) |
| `answer_contradicts_real_quote` | ответ противоречит настоящей цитате — помечается как неподтверждённый | MANUAL/LIVE |
| `high_score_no_fact` | высокий `score` без нужного факта — отказ или `limitation` | INT/LIVE |
| `weak_context` | слабый контекст — честный отказ | UNIT+INT+LIVE |
| `instruction_inside_document` | инструкция внутри документа — данные, не команды | UNIT/INT |
| `empty_or_truncated_generation` | пустой/обрезанный ответ — техническая ошибка; порядок «формат → truncated»: неразобранный JSON → `chat_invalid_response`, разобранный с `finish_reason="length"` → `truncated=true`/`failed` | UNIT+INT(+LIVE) |
| `provider_error` | ошибка/недоступность провайдера — техническая ошибка | UNIT/INT(+LIVE) |

`cases` содержит **не менее 8** записей; каждая запись несёт фактическое `observed` и
`status` (`PASS`/`FAIL`/`NOT_RUN`), без выдуманных наблюдений. Offline-кейсы формирует
`harness/d24_grounding.py`; LIVE-наблюдения добавляет `harness/d24_live.py`. Для
`empty_or_truncated_generation` в `observed` фиксируются **обе** ветки порядка §6.1/§8.2:
(а) пустой ответ/неразобранный JSON → `chat_invalid_response` с `details.format`; (б)
успешно разобранный JSON с `finish_reason="length"` → `truncated=true`,
`grounding_status="failed"`, `reason="truncated_generation"`. Это же различение входит в
evidence Tester по C15/C27 (D24-13).

### 13.4. `quality-assessment.md`

Независимая содержательная оценка Tester по 10 фактическим результатам
`grounding-results.json`: для **каждого** результата — правильность источников и точность
цитат, соответствие смысла ответа цитатам, полнота, обоснованность отказа. Файл создаёт
Tester (artifact C24); Developer не принимает собственную реализацию и не подменяет оценку
структурной проверкой. Наличие файла, корректный JSON и допустимый `chunk_id` **не**
заменяют оценку (C25). Отказ на всех девяти документных вопросах не объявляется улучшением
качества (C26).

---

## 14. Оценка качества (раздельная)

Для каждого из 10 результатов оцениваются независимо (0/1/2, как D22 §15.3):

1. **Retrieval/источники** — относятся ли показанные `sources` к `passed`, совпадают ли
   `source`/`section` с `metadata`; нет ли источников, не переданных модели.
2. **Цитаты** — `source_exists`, `quote_verbatim`; нет ли подмены; переводы помечены и
   сохраняют оригинал.
3. **Смысл** — подтверждают ли цитаты существенные утверждения ответа (ручная оценка; поле
   `meaning_supported` автоматической проверкой не выставляется).
4. **Полнота** — присутствуют ли ожидаемые факты; для `answerable:false` — отсутствие
   выдумывания.
5. **Обоснованность отказа** — соответствует ли отказ реальному отсутствию ответа в
   документах, сохранены ли `reason` и `threshold`.

Непустой ответ, существующий `chunk_id` и успешная команда сами по себе не доказывают
качество. `grounding.status="verified"` доказывает только формальные проверки 1–2 и никогда —
смысл.

---

## 15. Инварианты и ограничения

### 15.1. Инварианты D24

| ID | Инвариант |
| --- | --- |
| G1 | Источниками считаются только `passed`-чанки, реально переданные модели после поиска/фильтра/бюджета. |
| G2 | `candidates ⊇ selected ⊇ passed`; показ источников не расширяет `passed`. |
| G3 | Модель ссылается только на переданные `chunk_id`; неизвестный не подтверждается. |
| G4 | `source`/`section` берутся из `metadata` индекса, не из текста модели. |
| G5 | Цитата должна присутствовать в фрагменте после явной нормализации пробелов; пересказ не цитата. |
| G6 | Перевод помечен, оригинал сохранён; перевод без оригинала не `verified`. |
| G7 | Выдуманная цитата не подменяется и не отображается как успешно проверенная. |
| G8 | Три проверки различимы; формальная валидация не выставляет смысловую метку. |
| G9 | Найденный текст — данные, не команды; защита D22 сохранена. |
| G10 | Ниже порога — честный отказ без генерации из общих знаний; `reason` и `threshold` сохранены. |
| G11 | Высокий `score` не доказывает факт; допустим отказ или `partial` с `limitation`. |
| G12 | Смысл режимов D23 не подменяется скрытой фильтрацией. |
| G13 | Пустой/обрезанный ответ, ошибка формата и ошибка провайдера — технические ошибки, не «не знаю». |
| G14 | D21–D23 контракты и трасса сохранены; grounded-слой аддитивен. |
| G15 | 10 результатов — на одном закреплённом индексе и одном профиле; старые артефакты не перезаписаны. |
| G16 | Модели не скачиваются; секреты, base URL и lease ID не публикуются; TEMP-изоляция. |

### 15.2. Ограничения

* Смысловая проверка автоматизирована **не** будет: D24 даёт формальные проверки и ручную
  оценку Tester; отдельная NLI/entailment-модель — Later.
* Формат grounded-JSON зависит от выбранной модели; при ошибке формата честно возвращается
  техническая ошибка, а не «успешный» ответ.
* Нормализация пробелов не покрывает эквивалентные формулировки; это осознанно.
* Полная защита от prompt-injection в чанках невозможна (сохраняются меры D22).
* Порог cosine и качество retrieval привязаны к выбранной embedding-модели и корпусу.

---

## 16. Тесты по уровням (кратко)

* **UNIT** (без сети и `.env`): `GroundingVerifier` (нормализация пробелов, точная цитата,
  пересказ/пустая цитата, `source_exists`, неизвестный `chunk_id`, перевод/оригинал);
  `parse_grounded_response` (валидный JSON, fenced-блок, ошибка формата, пустой `answer`);
  `ChatService` (grounding-слой, `status`/`reason`/`threshold`, `refusal`, `limitation`,
  различение технических ошибок; `RAG_GROUNDING_ENABLED=0` → D22-форма); `grounded-rag-v1`
  не содержит эталонов/ответов и сохраняет защиту от инструкций; API-валидация `grounding`.
* **INT** (loopback, `embed_stub` + `chat_stub`, без внешней сети, TEMP): grounded-поток всех
  D23-режимов; неизвестный `chunk_id`; выдуманная цитата; пустой `selected`; ошибка формата
  стаба; ошибка/таймаут провайдера; `chunk_id`-фильтр чанков; D21–D23 регрессия.
* **LIVE**: 10 вопросов `eval/d22/questions.json` (9+1) с grounding на одном закреплённом
  индексе и выбранном remote-профиле через `test.bat scenario d24-rag-grounding`; граничные
  случаи провайдера/слабого контекста.
* **MANUAL/UI**: `run_app.bat` — источники, цитаты, переводы, раскрытие фрагмента, перенос
  строк, раздельные состояния.
* **Политика/LIVE** (§10.1): policy проверяется до реальных действий; `forbidden`/невалидная
  policy → `BLOCKED` и `NOT_RUN`; lease acquire/use/release в `finally`; offline ≠ LIVE;
  `MODEL_CHECK_KIND` без реального вызова не выставляется.
* **Регрессия D21–D23**: `tests/unit`, `tests/integration`, `smoke_test.bat` зелёные.

Детализация — `PLAN.md` §4 и `ACCEPTANCE.md`.

### 16.1. Механика grounded-ответа `harness/chat_stub.py` (R-minor)

`chat_stub.py` расширяется аддитивно, чтобы один и тот же INT/smoke-прогон обслуживал и
D22/D23, и D24:

* **Выбор режима ответа.** Стаб определяет grounded-запрос по маркеру grounded-шаблона в
  system-сообщении (наличие требования вернуть JSON с `answer`/`citations`). Для
  `grounded-rag-v1` он возвращает детерминированный JSON-объект grounded-формы; для
  `rag-v1`/`plain-v1`/`rewrite-v1` сохраняется прежнее свободное текстовое поведение
  (в т. ч. rewrite-детекция), поэтому D22/D23-ассерты не меняются.
* **Цитаты из контекста.** В grounded-режиме `citations[].quote` собирается из реального
  текста соответствующего `<context>`-блока: `chunk_id` берётся из строки `[chunk_id: …]`, а
  цитата — из **тела чанка**, то есть строк после метаданных `section_path`/`pages`/
  `source_label` (`chat/prompts.py:43–56`), чтобы `quote_verbatim`/`source_exists` были
  детерминированно истинны. Поле `answer` grounded-JSON содержит ссылку `[chunk_id]`
  переданного чанка, сохраняя D22-проекцию `answer.citations.valid` непустой при
  `RAG_GROUNDING_ENABLED=1`.
* **Граничные варианты.** Часть режимов `X-Stub-Fail` уже существует и переиспользуется без
  изменения семантики: `empty`, `length`, `unavailable`, `timeout`, `missing_model`,
  `length_done` (`chat_stub.py:145–148`), `no_usage`/`partial_usage` (`chat_stub.py:175–180`).
  D24 **аддитивно** добавляет grounded-варианты: неизвестный `chunk_id`, выдуманная цитата,
  `insufficient=true`, `limitation`, битый/неполный JSON. В grounded-режиме существующий
  `length_done` обязан возвращать **разбираемый** grounded-JSON (чтобы ветка «формат →
  truncated» упражнялась: разобранный JSON + `finish_reason="length"`), а `empty` и битый JSON —
  неразбираемый вывод (форматная ошибка). Они питают UNIT/INT-кейсы C15/C27 и `edge-cases.json`.
* **Offline-изоляция.** Стаб — не inference; результат не маркируется `MODEL_CHECK_KIND`.

---

## 17. Открытые решения (дефолты)

| Решение | Предлагаемый дефолт | Обоснование |
| --- | --- | --- |
| Grounding по умолчанию | `RAG_GROUNDING_ENABLED=1` для `with_rag` в `/api/chat`, `/api/chat/stream`, `/api/chat/compare-modes`; `/api/chat/compare` исключён; `0` восстанавливает D22-путь | D24 требует видимые цитаты в RAG-ответах; флаг и исключение compare сохраняют прежний путь для регрессии |
| Формат grounded-ответа | один JSON-объект (допускается ```json-блок) | строгая разборка, форматная ошибка типизирована |
| Нормализация | NFC + схлопывание пробелов + trim, регистрозависимо | просто, детерминированно, без ложных совпадений |
| Смысловая проверка | не автоматизируется (`meaning_check="not_performed"`) | отдельная entailment-модель — Later; не выдавать формальную проверку за смысловую |
| Честный отказ | `200` + `insufficient_sources=true`, пустые источники допустимы | отказ не ошибка; причина и порог сохранены |
| Технические ошибки | отдельные коды/статусы | ошибка провайдера/формата не маскируется под «не знаю» |
| Раскрытие фрагмента | опциональный `chunk_id` в списке чанков | минимальное аддитивное расширение D21-контракта |
| Артефакты | `local-data/d24/{grounding-results,edge-cases}.json` + `quality-assessment.md` | фиксированные пути контракта |

---

## 18. Трассировка R→контракт

| ID | Требование | Контракт |
| --- | --- | --- |
| R-01 | Ответ показывает текст, источники (`source`/`section`/`chunk_id`) и точные цитаты; источники = только `passed` | C01, C02 |
| R-02 | Сохранены индексирование D21, без-RAG D22, rewrite/фильтрация/настройки и четыре режима D23 | C03 |
| R-03 | Модель ссылается только на переданные `chunk_id`; неизвестный не подтверждается | C04 |
| R-04 | `source`/`section` — из `metadata` индекса, не из текста модели | C05 |
| R-05 | Цитата реально присутствует во фрагменте; нормализация пробелов явная; пересказ не принимается | C06 |
| R-06 | Перевод обозначен, оригинал сохранён | C07 |
| R-07 | Выдуманная цитата не подменяется и не отображается успешной | C08 |
| R-08 | Различаются три проверки; текст не доказывает смысл; нет обещания смысловой проверки | C09 |
| R-09 | Защита от инструкций внутри документов сохранена | C10 |
| R-10 | Ниже порога — честный отказ и уточнение, без общих знаний | C11 |
| R-11 | Высокий `score` не доказывает факт; отказ или `partial` с ограничением | C12 |
| R-12 | Честный отказ допускает пустые источники/цитаты; `reason` и `threshold` сохранены | C13 |
| R-13 | Смысл режимов D23 не подменяется скрытой фильтрацией | C14 |
| R-14 | Пустой/обрезанный ответ, ошибка формата, ошибка провайдера — технические ошибки | C15 |
| R-15 | UI: чат первым блоком, поля/настройки/кнопки/метрики/индикатор; источники и цитаты; раскрытие фрагмента; перенос строк; раздельные состояния; язык | C16, C17, C18, C19 |
| R-16 | UI проверен с честным обозначением предела | C20 |
| R-17 | 10 вопросов (9+1) из существующего корпуса и `eval/d22/questions.json`; эталоны сверены с документом | C21, C22 |
| R-18 | 10 результатов на одном индексе/профиле; старые результаты/форматы сохранены; файл/JSON/ID ≠ оценка | C23, C25 |
| R-19 | Tester независимо оценивает каждый результат; отказ на всё не улучшение | C24, C26 |
| R-20 | Восемь граничных случаев; точки входа/политика/lease; evidence; README | C27, C28, C29, C30 |

### 18.1. Маппинг D24-01…D24-20 (полностью раскрыт в ACCEPTANCE.md)

| ID | Суть | R | Контракт |
| --- | --- | --- | --- |
| D24-01 | Grounded-ответ: текст + источники + точные цитаты; источники = `passed` | R-01 | C01, C02 |
| D24-02 | Сохранение D21–D23 | R-02 | C03 |
| D24-03 | Только переданные `chunk_id`; `source`/`section` из metadata | R-03, R-04 | C04, C05 |
| D24-04 | Точная цитата и нормализация; пересказ отклонён | R-05 | C06 |
| D24-05 | Перевод с сохранением оригинала | R-06 | C07 |
| D24-06 | Выдуманная цитата не подменяется | R-07 | C08 |
| D24-07 | Три проверки различимы | R-08 | C09 |
| D24-08 | Защита от инструкций в документе | R-09 | C10 |
| D24-09 | Честный отказ ниже порога + уточнение | R-10 | C11 |
| D24-10 | Высокий score без факта; `limitation` | R-11 | C12 |
| D24-11 | Причина и порог отказа сохранены | R-12 | C13 |
| D24-12 | Смысл режимов D23 сохранён | R-13 | C14 |
| D24-13 | Технические ошибки ≠ отказ | R-14 | C15 |
| D24-14 | UI: сохранение и цитаты | R-15 | C16, C17, C18, C19 |
| D24-15 | UI проверен с честным пределом | R-16 | C20 |
| D24-16 | 10 вопросов и сверка эталонов | R-17 | C21, C22 |
| D24-17 | 10 результатов на одном индексе | R-18 | C23, C25 |
| D24-18 | Независимая оценка Tester | R-19 | C24, C26 |
| D24-19 | Восемь граничных случаев | R-20 | C27 |
| D24-20 | Политика/lease, evidence, README | R-20 | C28, C29, C30 |

---

## 19. Что НЕ входит (Later)

MCP, Wikipedia importer, ANN/векторная БД, ML-reranker, отдельная entailment/NLI-модель для
автоматической смысловой проверки, OCR, многосессионная память, полный многошаговый чат,
resume/инкрементальная переиндексация, удаление версий индекса, multi-process/Postgres/
Docker, автоматическое скачивание моделей. Эти пункты в D24 не реализуются.

---

## 20. Статус

`SPEC_STATUS: IMPLEMENTED_AND_SYNCHRONIZED`.

Реализация и регрессии присутствуют. Нормативный контракт актуализирован по текущему коду:
фактический промпт в бюджете, согласованность inline-ссылок со структурированными цитатами,
невозможность повысить ответ без цитат до partial через limitation. Этот статус не заменяет
независимую техническую, содержательную и браузерную приёмку.
