# SPEC — Day 21: индексация документов и базовый поиск (Knowledge Agent)

Идентификатор задачи: `day-21-document-indexing`.
Статус: **ожидает spec review** (реализация не выполнена; код, `.bat` и индекс в этом
документе не создаются — только спецификация).
Дата: 2026-09-29.
Источник задания: Task Contract пользователя (D21) и архитектурные решения Architect
(обозначены как A–K ниже). Документ самодостаточен и не требует обращения к переписке.

Термины, помеченные **предлагаемое**, на момент спецификации отсутствуют в репозитории и
создаются этой задачей. Формулировки вида «должно», «обязан» — нормативные требования.

---

## 1. Назначение

Независимый учебный модуль Week 5 (`week-05/knowledge-agent`) должен позволить
пользователю проиндексировать **явно указанный через конфиг** корпус документов
(PDF с текстовым слоем + маленький TXT/MD как второй источник) и выполнять по нему
локальный семантический поиск с просмотром найденных фрагментов и их происхождения.

Наблюдаемый результат дня 21:

* пользователь указывает путь к файлу-источнику через конфиг (приложение **не сканирует
  диск и не угадывает путь**);
* приложение извлекает текст, разбивает его на секции и чанки, считает локальные
  embeddings и сохраняет их в локальный SQLite-индекс;
* UI показывает прогресс, счётчики, чанки и метаданные, а также таблицу сравнения
  стратегий чанкинга;
* поиск возвращает **фрагменты** (а не сгенерированный ответ); генерация ответа моделью
  в D21 не обязательна;
* два chunker-а (`fixed`, `structure`) работают на одном очищенном тексте и одной
  embedding-конфигурации, что делает сравнение воспроизводимым;
* клиент может переключать активную коллекцию/индекс как параметр, без переписывания
  UI и поисковой логики.

Цель формулирует пользователь; инструменты имеют самостоятельное назначение, а не
жёстко зашитый «номер шага» (см. §3, комментарии #3999/#4026).

### 1.1. Схема потока (A)

```
PdfSourceAdapter / TextSourceAdapter
        └─► Document / Section (единый контракт)          [ядро]
                 └─► Chunker (FixedChunker | StructureChunker)
                          └─► Embedder (OllamaEmbedder)
                                   └─► IndexStore (SqliteIndexStore)
                                            └─► KnowledgeService
                                                     └─► FastAPI + static UI
```

Ядро (domain + service) не знает ни про SQLite, ни про Ollama, ни про PDF-библиотеки.
Хранилище и внешние потребители соединены адаптерами: замена backend-а индекса или
клиента не требует правок UI и поисковой логики (K, комментарии #3331–3340).

---

## 2. Границы и не-цели

### 2.1. Must (входит в D21)

* Корпус полезного текста объёмом **не менее 20–30 страниц** (см. §17, D21-01).
* Два адаптера источников: текстовый PDF и маленький TXT/MD (второй источник — для
  проверки контракта).
* Два chunker-а: `FixedChunker` и `StructureChunker` (§7, §14).
* Локальный embedder поверх Ollama `POST /api/embed`, модель `embeddinggemma:300m`.
* Локальный SQLite-индекс: текст, metadata, бинарные float32-векторы.
* `KnowledgeService` с независимым API и лёгкий static UI.
* HTTP API с OpenAPI 3.1 (FastAPI).
* Manifest индекса и полный набор метаданных чанка.
* Отдельный `harness/embed_stub.py` — детерминированный stub-embedder (не inference).
* Метрики и сравнение стратегий.
* Реальный запуск и перезапуск приложения выполняют Developer, затем независимо Tester.

### 2.2. Later (вне D21, сохраняется как задел)

Wiki importer (формат ZIM/XML не установлен; **не предполагать XML BZ2**), внешний
клиент/MCP adapter, ANN backend, полный chat RAG, reranker, OCR, resume/инкрементальная
переиндексация, удаление старых версий индекса, multi-process/Postgres/Docker.

### 2.3. Явное не-требование D21

* «Автоматический RAG» и полный чат не реализуются: пока работаем над качеством чанков
  и поиска по фрагментам, embeddings и сохранение из #4082 не отменяются (комментарий #4125).
* Универсальный номер шага не зашивается в tool descriptions (комментарии #3999/#4026).
* Сложность не строится ради паттернов: границы обосновываются инвариантами и
  наблюдаемыми метриками (комментарии #4107/#4114–4118).

### 2.4. Границы изменений

Создаются только три файла этой спецификации. Governance-файлы, недели `week-01..04`,
`README.md`, `STACK_PROFILES.md`, `run_app.bat`/`test.bat`/`setup.bat`/`smoke_test.bat`
других недель — не изменяются. `.env` не создаётся; в Git попадает только `.env.example`.

---

## 3. Комментарии Алексея Гладкова как ограничения/инварианты

Эти положения трактуются как обязательные инварианты, а не как пожелания:

| Источник | Трактовка в D21 |
| --- | --- |
| #4125 | Инвариант I-RAG: D21 не уходит в автоматический RAG и полный чат. Search возвращает фрагменты; генерация ответа не обязательна. embeddings и сохранение из #4082 сохраняются. |
| #3999, #4026 | Цель формулирует пользователь; инструменты (`SourceAdapter`, `Chunker`, `Embedder`, `IndexStore`, `KnowledgeService`) имеют самостоятельное назначение. Универсальный номер шага не зашивается в описания. |
| #4107, #4114–4118 | Качество проверяется через инварианты (§14, I1–I10), ограничения и наблюдаемые метрики (§14.4). Границы обосновываются; сложность не создаётся ради паттернов. |
| #3331–3340 | Между хранилищем (`IndexStore`) и потребителем (`KnowledgeService`/UI) стоят адаптеры. Смена backend-а индекса или клиента не требует переписывания UI и поисковой логики. |

---

## 4. Термины

Различать пять сущностей строго (B):

| Термин | Определение | Ключ/идентификация |
| --- | --- | --- |
| **source** | Конкретный файл на диске, выбранный пользователем через конфиг. | `content_sha256` (байты файла) |
| **document** | Извлечённое из source представление (текст + структура + координаты). Один source может дать один document текущей версией извлечения. | `document_id`, `extraction_version` |
| **section** | Реальный раздел документа (заголовок/иерархия), **не страница**. | `section_path`, `level`, `role` |
| **chunk** | Индексируемая единица текста с метаданными и вектором. | устойчивый `chunk_id` (§6.4) |
| **collection / corpus** | Пользовательская единица группировки и поиска; владеет указателем на active index version. | `collection_id` |
| **index version** | Неизменяемый артефакт индексации со статусом `building → ready → failed`. | `index_version_id` |

Страница — это атрибут происхождения (`page_start`/`page_end`), а **не** единица
секционирования и **не** chunk.

---

## 5. Архитектура, компоненты и адаптеры (A)

### 5.1. Компоненты

* **SourceAdapter** — единый контракт чтения источника в `Document`. Реализации:
  `PdfSourceAdapter`, `TextSourceAdapter`.
* **Chunker** — единый контракт разбиения `Document`/`Section` в `Chunk[]`.
  Реализации: `FixedChunker`, `StructureChunker`.
* **Embedder** — единый контракт вычисления векторов. Реализация: `OllamaEmbedder`
  (префиксы документа/запроса применяет **только он**).
* **IndexStore** — единый контракт хранения/поиска. Реализация: `SqliteIndexStore`.
  Контракт позволяет позднее добавить ANN backend без правок ядра.
* **KnowledgeService** — независимый прикладной сервис (build/find_active/search/
  list_chunks/статусы). Не знает про SQLite и HTTP.
* **FastAPI + static UI** — тонкая адаптерная оболочка над `KnowledgeService`.

### 5.2. Направление зависимостей

Зависимости направлены от внешних деталей к устойчивому ядру: `PdfSourceAdapter`,
`TextSourceAdapter`, `OllamaEmbedder`, `SqliteIndexStore` зависят от контрактов ядра,
но ядро не зависит от них. UI зависит только от `KnowledgeService`.

### 5.3. Адаптеры хранилище↔потребитель (#3331–3340)

`IndexStore` — адаптер между SQLite-деталями и `KnowledgeService`. `KnowledgeService` —
адаптер между ядром и HTTP/UI. Внешний клиент меняет `collection_id`/`index_version_id`
**параметром запроса**, без изменения кода UI и поиска. Будущий Wiki importer использует
тот же контракт документов (`SourceAdapter → Document`), не меняя chunker/embedder/store.

---

## 6. Контракты данных (A, B, D)

### 6.1. Базовые структуры

* `SourceRef` — `source_id`, `uri`, `public_uri`, `label` (basename), `kind`
  (`pdf`|`text`), `content_sha256`, `size_bytes`.
  * `uri` — **явный локальный путь** к файлу, заданный пользователем; хранится
    **только** в локальной БД (`sources.uri`) и никогда не публикуется в API/UI/логах
    (инвариант I7).
  * `public_uri` — **санитизированное публичное** значение (пользовательский алиас или
    относительная ссылка); именно оно попадает в metadata/API как `source_uri`.
* `ExtractionInfo` — `extraction_version`, `adapter`, `page_count`, `useful_pages`,
  `useful_chars`, `language`, `warnings[]`.
* `Section` — `section_id`, `section_path` (реальный путь раздела), `level`, `role`
  (например `body`|`references`|`appendix`), `start_bbox` (для PDF), `page_start`,
  `page_end`, `text`.
* `Document` — `document_id`, `source` (`SourceRef`), `extraction` (`ExtractionInfo`),
  `sections[]`, `normalized_markdown` (опционально, для ненадёжной структуры).
* `ChunkMetadata` — полный набор происхождения (см. §6.3).
* `Chunk` — `chunk_id`, `text`, `token_count`, `char_count`, `metadata` (`ChunkMetadata`),
  `vector` (опционально на этапе сборки).

### 6.2. `chunk_id` (устойчивость)

`chunk_id` детерминирован и вычисляется из конкатенации:
`corpus_schema_version + document_id + strategy + params + section_path + ordinal + text_hash`.
Свойства: повторная индексация неизменного текста даёт **те же** `chunk_id`; изменение
байтов источника меняет document/fingerprint (§9.5).

### 6.3. Metadata чанка (обязательные поля)

| Поле | Описание |
| --- | --- |
| `source_uri` | **санитизированное публичное** значение = `SourceRef.public_uri` (пользовательский алиас/относительная ссылка). Абсолютный локальный путь сюда **не попадает**; он хранится только в `sources.uri` локальной БД. |
| `source_label` | basename источника; абсолютный путь **не** публикуется |
| `title` / `document` | человекочитаемый заголовок/идентификатор документа |
| `section_path` | реальный путь раздела (не страница) |
| `page_start`, `page_end` | диапазон страниц для PDF (для текстового источника — `null`) |
| `content_sha256` / `content_version` | хэш и версия содержимого |
| `language` | язык текста |
| `chunk_id` | устойчивый идентификатор |

### 6.4. Manifest индекса (D)

Manifest хранится в `index_versions.manifest_json` и содержит:

* `manifest_schema_version`, `corpus_schema_version`;
* `index_version_id`, `collection_id`;
* статус и таймстемпы (`created_at`, `started_at`, `finished_at`);
* `pipeline`: `adapter`, `extraction_version`, `normalization`;
* `chunking`: `strategy`, `unit`, `tokenizer`, `params`;
* `embedding`: `provider`, `base_url`, `endpoint_version`, `api`, `model`, `digest`,
  `dimension`, `dtype`, `normalization`, `document_prefix`, `query_prefix`,
  `truncate=false`, `batch_size` (префиксы разделены — см. §10);
* `sources[]`: упорядоченный набор источников с `label`, `kind`, `content_sha256`
  (полный хэш байтов), `page_count`, `useful_pages`, `useful_chars`, `language`;
* `excluded_roles` (например `["references"]` при явном исключении);
* `counts`: источники/документы/секции/чанки;
* `metrics` (§14.4);
* `warnings[]`.

---

## 7. Контракты интерфейсов и ошибки (A, C, I)

### 7.1. Python-уровневые контракты

`SourceRef`, `ExtractionInfo`, `Section`, `Document`, `ChunkMetadata`, `Chunk`,
`SourceAdapter`, `Tokenizer`, `Chunker`, `EmbedderIdentity`, `EmbedBatchResult`,
`Embedder`, `IndexStore`, `check_index_compatibility`, `KnowledgeService`.

Ответственности:

* `SourceAdapter.extract(source_ref) -> Document`; поднимает типизированные ошибки
  источника.
* `Tokenizer.tokenize(text) -> list[str]` (детерминированный лексический v1, §8.4).
* `Chunker.chunk(document) -> list[Chunk]`.
* `Embedder.identity() -> EmbedderIdentity`;
  `Embedder.embed_documents(texts) -> EmbedBatchResult`;
  `Embedder.embed_query(text) -> EmbedBatchResult`. Префиксы применяет только Embedder.
* `IndexStore`: создание/активация/поиск/получение чанков и статусов, полностью scoped
  по `collection_id`/`index_version_id`.

### 7.2. Таксономия ошибок

* Источник: `SourceUnreadable`, `SourceUnsupported`, `SourceEmpty`, `SourceNoTextLayer`,
  `SourceInvalid`, `SourceEncrypted`.
* Embedding: `EmbeddingUnavailable`, `EmbeddingTimeout`, `EmbeddingModelMissing`,
  `EmbeddingLengthError`, `EmbeddingDimensionMismatch`, `EmbeddingCountMismatch`,
  `EmbeddingInvalidVector`, `EmbeddingUsageError`.
* Индекс: `index_incompatible`, `index_not_ready`, `index_busy`,
  `store_schema_unsupported`.

### 7.3. Таблица отказов (I)

| Код | HTTP | Ситуация |
| --- | --- | --- |
| bad source (любой `Source*`) | 422 | пустой/битый/зашифрованный PDF, нет текстового слоя |
| `EmbeddingUnavailable`/`EmbeddingTimeout`/`EmbeddingModelMissing` | 503 | Ollama недоступен/таймаут/нет модели (единый код) |
| `index_incompatible` | 409 | расхождение identity/fingerprint (§10) |
| `index_not_ready` | 409 | поиск/активация по неготовому индексу |
| `index_busy` | 409 | второй build в процессе (§9.4) |
| `store_schema_unsupported` | 500 | неизвестный `schema_version` SQLite |

Сообщения санитизированы: используется `source_label` вместо абсолютного пути, ключи и
содержимое `.env` не попадают в ответ.

---

## 8. Embedding-контракт (E, K)

### 8.1. Провайдер и модель

Локальный Ollama, endpoint `POST /api/embed`, модель `embeddinggemma:300m` —
**отдельная embedding-модель**, не чат-модель и не модель ролей OpenCode.

### 8.2. Preflight (без вызова инференса)

До выбора модели приложение фиксирует фактическую доступность и формат входов:

1. `GET /api/version` — доступность и версия сервиса;
2. `GET /api/tags` — наличие модели `embeddinggemma:300m`;
3. `POST /api/show` — `digest`, `model_info.embedding_length` (dimension), формат;
4. опциональный `--infer` opt-in — реальный вызов (только вручную, не в тестах);
5. legacy `/api/embeddings` — как fallback при отсутствии `/api/embed`.

Preflight **не** выполняет инференс по умолчанию. Результат (версия, digest, dimension)
фиксируется в manifest. Недоступность Ollama не мешает старту приложения: UI показывает
`Embedding: unreachable` и подсказку `ollama pull embeddinggemma:300m`; модель тянет
пользователь.

### 8.3. Правила вызова

* Батчи (`batch_size`, default 16);
* `truncate=false`;
* проверка числа векторов, размерности и конечности (finite) каждого значения;
* ошибка длины → **явное** разбиение/отказ без потери конца текста
  (`EmbeddingLengthError`), не молчаливая обрезка;
* префиксы `document_prefix` (для документов) и `query_prefix` (для запросов) задаёт
  **один** `Embedder`; runtime повторно их не добавляет; двойной префикс →
  `EmbeddingUsageError`;
* dimension берётся из `model_info.embedding_length`, иначе из первого ответа;
* dtype `float32` little-endian, нормализация `l2` выполняется **один раз** в Embedder.

### 8.4. Токенизатор (решение B3)

`Tokenizer` v1 — детерминированный лексический, offline: regex `\w+|[^\w\s]` в Unicode.
Единицы `token`/`char` фиксируются в manifest. Единица по умолчанию — token.

### 8.5. Stub-embedder (не inference)

`harness/embed_stub.py` — stdlib HTTP, детерминированные векторы от `sha256(text)` с
инъекцией ошибок. Явно маркируется как **не inference**; никогда не выдаётся за реальный
embedding.

---

## 9. Хранение: SQLite, manifest, статусы, активация, дедупликация (C, D, F, G)

### 9.1. Схема (C)

Таблицы: `schema_meta`, `collections`, `sources`, `documents`, `sections`,
`index_versions`, `index_documents`, `chunks`.

* `index_versions`: `status` CHECK (`building`|`ready`|`failed`), `fingerprint`,
  embedding identity, `document_prefix`, `query_prefix`, `manifest_json`, `counts`,
  `metrics`, `progress_json`, `error`.
* `chunks`: `metadata_json`, `vector` BLOB (`float32` LE),
  `PRIMARY KEY(index_version_id, chunk_id)`.
* Режимы: WAL, `foreign_keys=ON`.
* Уникальный индекс: один `ready` на `(collection_id, fingerprint)`.
* Активация атомарна в `BEGIN IMMEDIATE` и только для `ready`.
* Неизвестный `schema_version` → честный отказ `store_schema_unsupported`.

### 9.2. Жизненный цикл (F)

* Один build на процесс; второй → 409 `index_busy`.
* Прогресс обновляется в `progress_json`; UI поллит прогресс.
* `fail_index` удаляет частичные чанки **в той же транзакции** и переводит версию в
  `failed`.
* При старте приложения вызывается `mark_stale_builds_failed("interrupted")`: зависшие
  `building` помечаются `failed`; **active/ready не трогаются**.
* Поиск во время build идёт по активной ready-версии (WAL), не блокируется.

### 9.3. Точный поиск vs масштаб

Точный cosine допустим только для **маленького** учебного корпуса и не выдаётся за
масштабируемый поиск. Контракт `IndexStore` позволяет позднее добавить ANN backend.

### 9.4. Один процесс сервиса

Без Docker, кластера и отдельной векторной БД. SQLite — единственное хранилище.

### 9.5. Дедупликация (G)

* `fingerprint` включает упорядоченный набор источников (`kind`+`content_sha256`
  каждого в порядке `sources[]`) вместе с chunking/embedding-параметрами и
  `corpus_schema_version`.
* `fingerprint` → `find_ready_index`; повторный **неизменный** импорт → `{reused:true}`
  без новых строк (D21-06); ответ `POST /api/index/build` повторяет существующий
  `index_version_id` со `status: "ready"` (§11.1).
* Чанки вставляются `INSERT OR IGNORE` по `(index_version_id, chunk_id)`.
* Изменение байтов любого источника, его порядка или набора → новый
  source/document/fingerprint и новый index version.

---

## 10. Совместимость и отказы (E)

`check_index_compatibility` — **чистая функция**; сравнивает:

* `model`, `digest`, `dimension`, `normalization`, `dtype`, `document_prefix`,
  `query_prefix`, `corpus_schema_version`.

Правила:

* любое расхождение → 409 `index_incompatible` с `{expected, actual}`;
* запрос **не** эмбеддится до прохождения проверки (нет «поиска запросом одной модели по
  векторам другой»);
* смена модели — только через **новый index version** + новый `fingerprint`; нельзя
  молча искать несовместимым запросом.

---

## 11. HTTP API и OpenAPI 3.1

`KnowledgeService` отделён от UI; HTTP-слой — FastAPI с OpenAPI 3.1.
**Этот раздел — единственный источник истины для методов, путей, параметров и форм
ответов.** `PLAN.md` ссылается сюда (§3) и не дублирует пути, чтобы не возникало
расхождений.

Базовые инварианты контракта:

* **Активный индекс** — это **указатель коллекции** `active_index_version_id`
  (`collections.active_index_version_id`), а не отдельный ресурс «active collection».
  Переключение оформляется установкой active index version коллекции.
* **Chunk scoped по `index_version_id`** (`chunk_id` не глобален), поэтому чанки
  выдаются вложенным ресурсом index version, а не `GET /api/chunks/{chunk_id}`.
* `POST /api/index/build`, `POST /api/search`, `GET /api/compare` принимают явные
  `collection_id` и/или `index_version_id` и `strategy`; ничего не берётся из
  неявного глобального состояния.
* Секреты и абсолютные пути не возвращаются; наружу идёт `source_uri`
  (санитизированный `public_uri`), §6.3.

### 11.1. Операции

| Метод и путь | Параметры / тело | Успешный ответ |
| --- | --- | --- |
| `GET /api/health` | — | `{status: "ok"\|"degraded", embedding: {reachable: bool, model: str, digest: str\|null, dimension: int\|null, hint: str\|null}}` |
| `GET /api/collections` | — | `{collections: [{collection_id, name, active_index_version_id: str\|null, counts: Counts}]}` |
| `POST /api/collections` | `{name}` | `{collection_id, name, active_index_version_id: null, counts: Counts}` |
| `PUT /api/collections/{collection_id}/active-index` | `{index_version_id}` | `{collection_id, active_index_version_id}` (409 `index_not_ready`, если версия не `ready`; 404 неизвестная коллекция/версия) |
| `GET /api/collections/{collection_id}/index-versions` | — | `{index_versions: [IndexVersionSummary]}` (status, strategy, fingerprint, counts, metrics, error) |
| `GET /api/index-versions/{index_version_id}` | — | `IndexVersionSummary` + `progress` + `manifest` |
| `POST /api/index/build` | `{collection_id, sources: [{path, label?}], strategy}` | `{index_version_id, status: "building"\|"ready", reused: bool}`. При неизменном повторном импорте — существующий `index_version_id`, `reused: true`, `status: "ready"`; иначе `reused: false`, `status: "building"`. 409 `index_busy`; 422 ошибки источника; `path` — явный путь пользователя, не публикуется |
| `GET /api/index-versions/{index_version_id}/chunks` | query `offset` (≥0, default 0), `limit` (1..200, default 50), `document_id` (опц.), `section_path` (опц.) | `{items: [Chunk], total, offset, limit, filters: {document_id, section_path}}` |
| `POST /api/search` | `{collection_id, index_version_id?, strategy?, query, top_k}` | `SearchResponse` (§11.3); при явном `index_version_id` `strategy` необязателен; иначе обязателен и выбирает active-индекс коллекции этой стратегии, при отсутствии выбора — active коллекции (409 `index_not_ready`); 409 `index_incompatible` до эмбеддинга запроса |
| `GET /api/compare` | query `collection_id` (обяз.), `index_version_id` (опц.; иначе active), `strategies=fixed,structure` | `{collection_id, strategies: [CompareRow]}` — строки с counts и metrics на одинаковых границах разделов |

Сборка допускает **один или несколько источников**: `sources` — упорядоченный список
`{path, label?}`; порядок значим и входит в `fingerprint` (§9.5). Каждый источник даёт
свой `source`/`document`; `label` по умолчанию — basename файла; абсолютные пути наружу
не возвращаются.

`strategy` имеет **канонические значения `fixed`|`structure`** (нижний регистр) в
API/manifest/fingerprint. Приоритет: при явном `index_version_id` `strategy`
необязателен и на выбор не влияет; `strategy` обязателен только когда индекс выбирается
по стратегии (build или выбор active-индекса коллекции по стратегии).

`Counts` — перечисляемые поля: `{sources, documents, sections, chunks}`.
`IndexVersionSummary`: `{index_version_id, collection_id, strategy, status,
fingerprint, created_at, started_at, finished_at, counts: Counts, metrics: Metrics,
error: str\|null}`.
`Metrics` — перечисляемые поля (§14.4): `{chunk_tokens: {min, median, p95, max},
chunk_chars: {min, median, p95, max}, overlap_overhead, section_crossing_ratio,
parse_seconds, embed_seconds, store_seconds, build_seconds, chunks_per_second,
embed_latency_median_ms, embed_latency_p95_ms, input_tokens: int\|null,
db_size_bytes, vector_bytes}`.

### 11.2. Форма чанка

```json
{
  "chunk_id": "<stable id>",
  "index_version_id": "<id>",
  "text": "<chunk text>",
  "token_count": 498,
  "char_count": 2010,
  "metadata": {
    "source_label": "survey.pdf",
    "source_uri": "<public alias / relative ref>",
    "document_id": "<id>",
    "title": "<document title>",
    "section_path": "2.1 Memory",
    "page_start": 5,
    "page_end": 6,
    "language": "en",
    "content_sha256": "<sha256>"
  }
}
```

### 11.3. Форма ответа поиска

```json
{
  "query": "<raw query>",
  "collection_id": "<id>",
  "index_version_id": "<id>",
  "strategy": "Structure",
  "top_k": 5,
  "fragments": [
    {
      "rank": 1,
      "score": 0.81,
      "chunk_id": "<stable id>",
      "text": "<fragment text>",
      "metadata": { "...": "как в §11.2" }
    }
  ],
  "counts": {"indexed_chunks": 1234, "returned": 5},
  "metrics": { "...": "как Metrics в §11.1" }
}
```

Фрагменты сортируются по убыванию `score` (точный cosine, §9.3). Возвращаются
**фрагменты**, а не сгенерированный ответ (инвариант I-RAG, #4125).

### 11.4. Форма ошибок

```json
{
  "error": {
    "code": "index_incompatible",
    "message": "<sanitized message>",
    "details": {"expected": {"model": "...", "dimension": 768},
                 "actual":   {"model": "...", "dimension": 1024}}
  }
}
```

HTTP-коды — по таблице §7.3; `details` опционален и не содержит секретов, ключей и
абсолютных путей. Форма ошибки одинакова для всех операций.

---

## 12. UI

Лёгкий static UI (без шага сборки, без CDN), английский интерфейс:

* чатовая лента/панель поиска: запрос → найденные фрагменты с происхождением;
* выбор коллекции и стратегии (канонические значения `fixed`/`structure`; в UI могут
  отображаться как Fixed/Structure);
* прогресс сборки и счётчики (sources/documents/sections/chunks);
* просмотр чанка и его metadata (`section_path`, страницы, hash, language);
* таблица сравнения стратегий на одинаковом очищенном тексте и одной embedding-конфигурации;
* явные ошибки (`Embedding: unreachable`, `index_incompatible`, `index_busy` и т. д.)
  с подсказкой, например `ollama pull embeddinggemma:300m`;
* переключение active index version коллекции **без** переписывания UI и поисковой
  логики (через `PUT /api/collections/{collection_id}/active-index`, SPEC §11.1).

Search возвращает фрагменты; генерация ответов не обязательна (инвариант I-RAG, #4125).

---

## 13. Конфигурация и defaults (K)

* `.env` не создаётся приложением; в репозиторий добавляется только `.env.example` без
  настоящих значений.
* `load_settings(env: Mapping | None)` — тестируемый доступ к конфигу без чтения `.env`.
* Дефолты: `KNOWLEDGE_DB_PATH`, `KNOWLEDGE_HOST=127.0.0.1`, `KNOWLEDGE_PORT=8770`
  (свободный порт, не совпадающий с backend Week 4), `KNOWLEDGE_SOURCE_PATH` (пусто —
  путь задаёт пользователь), `EMBED_*`, `EMBED_BATCH_SIZE=16`,
  `EMBED_TIMEOUT_SECONDS=60`, `CHUNK_*`, `PDF_USEFUL_PAGE_MIN_CHARS=500`.
* `.env.example` перечисляет `KNOWLEDGE_HOST`/`KNOWLEDGE_PORT` (в т.ч.
  `KNOWLEDGE_PORT=8770`) и прочие ключи без настоящих значений.
* Приложение **не сканирует диск**; источник выбирает пользователь явным путём через
  конфиг.

---

## 14. Инварианты, ограничения и метрики

### 14.1. Инварианты (J)

| ID | Инвариант |
| --- | --- |
| I1 | Все векторы имеют размер `version × dimension × 4` байт и конечны (finite). |
| I2 | `active` всегда `ready`; активация — одной транзакцией. |
| I3 | `failed`/`building` невидимы для поиска и не меняют `active`. |
| I4 | Поиск не смешивает embedding identity. |
| I5 | `chunk_id` детерминирован. |
| I6 | Текст не теряется при split (ошибка длины не режет конец). |
| I7 | Нет абсолютных путей/секретов в API/UI/README/логах. |
| I8 | Повторный импорт не создаёт строк. |
| I9 | Тесты без `.env` и сети. |
| I10 | Один build на процесс, WAL. |

### 14.2. Ограничения чанкинга

* `fixed`: ~500 токенов, overlap ~75; единица — детерминированный лексический
  токенизатор v1 (offline).
* `structure`: границы разделов/абзацев, дробление слишком длинных секций; страницы —
  **не** секции. `max_tokens=800`, `min_tokens=64`, overlap `0`, страховочный
  `max_chars = 4 × max_tokens`.
* Двухколоночный PDF: порядок чтения, переносы, колонтитулы, сохранение координат (§17).
* При ненадёжной структуре — проверяемая нормализованная Markdown-предобработка
  `render_markdown(document)` с сохранением происхождения.
* Сравнение стратегий — на одинаковом очищенном тексте и одной embedding-конфигурации.

### 14.3. Состояние прерванной сборки

Предусматривается `failed`/`interrupted` (§9.2, D21-10). Полный resume — Later.

### 14.4. Метрики (D, требовании 11)

* `chunks count`;
* min/median/p95/max длины чанка (токены **и** символы);
* `overlap_overhead`;
* `section_crossing_ratio`;
* размер индекса и время индексации (`parse_seconds`, `embed_seconds`, `store_seconds`,
  `build_seconds`, `chunks_per_second`);
* `embed_latency` median/p95;
* `input_tokens` (или `null`, если провайдер не вернул);
* `db_size_bytes`, `vector_bytes`;
* сравнение нескольких стратегий при **одинаковых** границах разделов.

Метрики embedding: input tokens (если доступны), chunks/sec, latency. Output tokens
генерации текста и output tok/s для embedding **не выдумываются** — н/д.

---

## 15. Безопасность и приватность

* `.env`/секреты не читаются тестами и не публикуются; тесты изолированы от сети и
  настоящего `.env`.
* В публичную спецификацию не переносятся частные названия, локальные пути и устройство
  инфраструктуры; используются нейтральные учебные примеры.
* Корпус, индексы и модели **не попадают в Git**.
* Приложение не получает произвольный доступ к диску; путь к файлу выбирает пользователь
  явно; абсолютный путь не публикуется (используется `source_label`).
* Платные/внешние API не вызываются; модель не загружается автоматически.
* Реальный локальный embedding — отдельный ручной/доверенный opt-in сценарий;
  mock/stub не выдаётся за inference.

---

## 16. Зависимости

* Стек Week 5 (`Backend AI`): Python + FastAPI + Pydantic + SQLite + Pytest; UI —
  статический HTML/JS без сборки и CDN.
* PDF: `pdfplumber` (MIT; bbox, чистый Python) как основной; `pypdfium2` как fallback.
* HTTP-клиент к Ollama — через штатные зависимости проекта.
* Новые зависимости добавляются только с обоснованием; фактический список фиксируется в
  `requirements.txt` и в файловой структуре `PLAN` §1.

---

## 17. Открытые решения и трассировка R→D21

### 17.1. Открытые решения (дефолты, K)

| Решение | Дефолт | Обоснование |
| --- | --- | --- |
| PDF-библиотека | `pdfplumber`, fallback `pypdfium2` | MIT, bbox и координаты, чистый Python |
| Двухколоночность | детекция вертикальной «долины» по x-профилю; full-width заголовки; порядок left→right | сохранение порядка чтения без внешних ML-моделей |
| Колонтитулы | повтор по y-полосам 8% при частоте ≥60% | эвристика, проверяемая метрикой |
| Дегифенация | `-` + строчная | восстановление слов при переносе |
| Структура | нумерация заголовков + размер шрифта + ALL-CAPS; `section_path`/`level`/`role`; `references` исключается явно через `excluded_roles`; fallback single-column + warning + нормализованный Markdown | проверяемость и сохранение происхождения |
| Токенизатор | детерминированный лексический `Tokenizer` v1 (regex `\w+|[^\w\s]`, Unicode) | offline, воспроизводимость |
| Параметры `fixed` | 500/75 | базовый дефолт |
| Параметры `structure` | max_tokens=800, min_tokens=64, overlap=0, max_chars=4×max_tokens | дробление длинных секций без потери текста |
| Ollama preflight | `GET /api/version`, `GET /api/tags`, `POST /api/show` (+digest, embedding_length), опциональный `--infer`, legacy `/api/embeddings` | проверка без инференса |
| Префиксы | `document_prefix`/`query_prefix` форматирует только `OllamaEmbedder`; двойной префикс → `EmbeddingUsageError` | один источник префиксов |
| dtype/normalization | float32 LE, l2 один раз в Embedder | согласованность векторов |
| Stub | `harness/embed_stub.py`, stdlib HTTP, векторы от `sha256(text)`, инъекция ошибок | изолированные тесты без сети |
| Конфиг | дефолты §13; `.env` не создаётся, только `.env.example`; `load_settings(env: Mapping\|None)` | тестируемость и безопасность |

### 17.2. Идентификаторы требований R-01… и матрица R→D21

Каждое нормативное требование SPEC имеет стабильный идентификатор `R-NN`. Трассировка
идёт `R-NN → раздел SPEC → D21-NN`; без ссылки на `R-NN` требование не считается
трассируемым.

| ID | Требование | Раздел SPEC | D21 |
| --- | --- | --- | --- |
| R-01 | Корпус ≥ 20–30 страниц полезного текста; явный путь через конфиг | §2.1, §17.4 | D21-01 |
| R-02 | Текстовый PDF + TXT/MD как второй источник | §2.1, §5.1 | D21-01, D21-02 |
| R-03 | Wiki importer — Later; формат ZIM/XML не предполагается | §2.2 | Later |
| R-04 | Поток `SourceAdapter → Document → Chunker → Embedder → IndexStore`; `KnowledgeService`; адаптеры между хранилищем и потребителем; смена collection/index без правок UI/поиска | §5 | D21-07 |
| R-05 | Различение source/document/section/chunk/collection/index version | §4 | D21-04 |
| R-06 | Полный набор metadata чанка, включая `section_path`, страницы, hash | §6.3 | D21-04 |
| R-07 | Manifest: schema/corpus version, chunking, embedding, counts, metrics, warnings | §6.4 | D21-04 |
| R-08 | Локальный Ollama `POST /api/embed`, `embeddinggemma:300m`, preflight без инференса | §8.1–§8.2 | D21-03 |
| R-09 | Батчи, `truncate=false`, проверка count/dim/finite; префиксы только в Embedder | §8.3 | D21-03, D21-08 |
| R-10 | `EmbeddingLengthError` без потери конца текста | §8.3 | D21-09 |
| R-11 | Fixed ~500/75 и Structure (800/64, overlap 0); страницы не секции; двухколоночный PDF; Markdown-fallback; сравнение на одном тексте | §14.2, §17.1 | D21-02 |
| R-12 | SQLite с текстом/metadata/float32-векторами; точный cosine только для малого корпуса; `IndexStore` ANN-ready; один процесс | §9 | D21-05 |
| R-13 | Смена модели → новый совместимый index; `check_index_compatibility` до эмбеддинга запроса | §10 | D21-08 |
| R-14 | Лёгкий UI (коллекция/стратегия, прогресс, счётчики, чанки, сравнение, ошибки); search → фрагменты; API отделён от UI | §11, §12 | D21-07, D21-11 |
| R-15 | Инварианты I1–I10 | §14.1 | D21-06, D21-08, D21-10 |
| R-16 | Таблица отказов/HTTP-кодов; санитизация; отсутствие Ollama не мешает старту | §7.3, §15 | D21-09, D21-11 |
| R-17 | Метрики и измеренное сравнение стратегий | §14.4 | D21-12 |
| R-18 | Тесты без `.env`/сети; LIVE — только opt-in; mock/stub ≠ inference | §15 | D21-03, D21-12 |
| R-19 | Комментарии Гладкова #4125/#3999/#4026/#4107/#4114–4118/#3331–3340 как инварианты | §3 | D21-11, D21-12 |
| R-20 | Реальный запуск/перезапуск выполняют Developer и независимо Tester | §2.1 | D21-11 |

### 17.3. Маппинг D21-01…D21-12 (полностью раскрыт в ACCEPTANCE.md)

| ID | Суть | R-ссылки | Уровень |
| --- | --- | --- | --- |
| D21-01 | полезный корпус ≥20 useful-страниц; при <20 добавить arxiv 2312.10997 | R-01, R-02 | MANUAL + INT |
| D21-02 | два chunker-а | R-02, R-11 | UNIT + INT |
| D21-03 | настоящие embeddings | R-08, R-09, R-18 | LIVE (MODEL_CHECK_KIND: LOCAL) |
| D21-04 | полные metadata/происхождение | R-05, R-06, R-07 | UNIT + LIVE/MANUAL |
| D21-05 | чтение после фактического рестарта | R-12 | INT |
| D21-06 | повторный импорт без дублей | R-15 | UNIT + INT |
| D21-07 | изоляция/переключение двух коллекций | R-04, R-14 | INT + UI |
| D21-08 | отказ при несовместимой модели | R-09, R-13, R-15 | UNIT + INT |
| D21-09 | отказ на пустом/битом файле | R-10, R-16 | UNIT + INT |
| D21-10 | прерванный build не ready/active и не вредит старому | R-15 | UNIT + INT |
| D21-11 | реальный запуск и UI (Developer и независимо Tester) | R-14, R-16, R-19, R-20 | MANUAL + UI |
| D21-12 | воспроизводимый README с измеренным сравнением и сценарием видео | R-17, R-19 | MANUAL |

### 17.4. Корпус и Must/Later

Корпус (R-01): основной источник — «A Survey on Large Language Model based
Autonomous Agents», 42 стр., `https://arxiv.org/pdf/2308.11432v7`. Если после очистки
полезного текста < 20–30 страниц — добавить второй источник
`https://arxiv.org/pdf/2312.10997`. Объём **нельзя** подменять числом пустых/непрочитанных
страниц. Фактический файл выбирает пользователь (явный путь через конфиг).

Must/Later: Must — всё перечисленное в §2.1 и D21-01…D21-12; Later — Wiki importer
(R-03), внешний клиент/MCP, ANN, chat RAG, reranker, OCR, resume, удаление старых версий,
multi-process/Postgres/Docker.

### 17.5. Трассировка решений Architect → разделы

A → §5; B → §4/§6; C → §9; D → §6.4; E → §10; F → §9.2; G → §9.5; H → §5.3/§12;
I → §7.3/§15; J → §14.1; K → §8/§13/§17.1.
