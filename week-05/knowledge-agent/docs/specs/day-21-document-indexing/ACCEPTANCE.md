# ACCEPTANCE — Day 21: индексация документов и базовый поиск

Идентификатор задачи: `day-21-document-indexing`.
Статус: **ожидает spec review**.
Дата: 2026-09-29.
Связанные документы: `SPEC.md` (требования), `PLAN.md` (порядок работ P-01…P-11).

Каждый критерий трассируется к требованию `R-NN` (SPEC §17.2) и к шагу `P-NN` в
`PLAN.md`; ссылки на `R-NN` приведены в колонке «R» таблицы §2.

---

## 1. Легенда уровней и правило «mock/stub ≠ LIVE»

Каноническая шкала уровней: **UNIT / INT / LIVE / MANUAL / UI**.

| Уровень | Что подтверждает | Изоляция |
| --- | --- | --- |
| **UNIT** | Логика ядра на моках/stub, без сети и `.env` | `test.bat unit` |
| **INT** | Реальные процессы сервиса + реальный HTTP + локальный `harness/embed_stub.py` (loopback), без внешней сети | `test.bat integration`, `smoke_test.bat` |
| **LIVE** | Реальная локальная модель `embeddinggemma:300m` через Ollama | opt-in (`test.bat live`/`acceptance`, `MODEL_CHECK_KIND: LOCAL`) |
| **UI** | Фактическая проверка интерфейса в браузере | браузер |
| **MANUAL** | Ручной прогон пользователем/Developer/Tester | вручную |

**Фактический рестарт — это метод, а не отдельный уровень:** он относится к уровню
`INT` (перезапуск процесса выполняет `harness/restart_check.py` внутри INT-сценария).

**Правило:** mock/stub **никогда** не маркируется как inference и не выдаётся за LIVE.
Unit-тест с `FakeClient`/stub подтверждает логику, но не реальный контракт внешнего
провайдера. Критерий, зависящий от фактического поведения Ollama (`digest`, `dimension`,
семантика batch/`truncate`, ошибки длины), считается проверенным только на уровне LIVE.
Метрики embedding: input tokens (если доступны), chunks/sec, latency; output tokens
генерации и output tok/s для embedding — н/д.

---

## 2. Таблица критериев D21-01…D21-12

| ID | R (SPEC §17.2) | Критерий | Компонент | Уровень | Способ проверки | Порог |
| --- | --- | --- | --- | --- | --- | --- |
| D21-01 | R-01, R-02 | Индексирован корпус полезного текста (один или несколько источников) | `PdfSourceAdapter`, `POST /api/index/build` (`sources[]`), manifest `sources[]`, UI | MANUAL + INT | сборка по `sources: [{path, label?}]`; очистка и подсчёт `useful_pages`/`useful_chars`; при <20 полезных страниц добавить второй источник `https://arxiv.org/pdf/2312.10997` отдельной записью `sources[]` | ≥ 20 useful-страниц суммарно по набору; manifest содержит упорядоченный `sources[]` (kind+content_sha256); пустые/непрочитанные страницы не считаются |
| D21-02 | R-02, R-11 | Два chunker-а на одном очищенном тексте | `FixedChunker`, `StructureChunker` | UNIT + INT | построение обоих индексов (`strategy=fixed` и `strategy=structure`) на одном тексте и одной embedding-конфигурации; сравнение | оба дают чанки; `fixed` ~500/75; `structure` границы разделов, overlap 0 |
| D21-03 | R-08, R-09, R-18 | Настоящие embeddings локальной модели | `OllamaEmbedder` | LIVE (`MODEL_CHECK_KIND: LOCAL`) | реальный `POST /api/embed` с `embeddinggemma:300m` | векторы конечны; dimension из `model_info.embedding_length`, при отсутствии — из первого ответа (SPEC §8.3); не stub |
| D21-04 | R-05, R-06, R-07 | Полные metadata/происхождение | `ChunkMetadata`, manifest, `sources.uri` | UNIT + LIVE/MANUAL | проверка полей §6.3 SPEC на реальном индексе и в БД | все поля присутствуют; `sources.uri` содержит явный локальный путь (**только** в локальной БД), а `source_uri` в metadata/API — санитизированный `public_uri` без абсолютного пути |
| D21-05 | R-12 | Чтение индекса после фактического рестарта | `SqliteIndexStore`, `KnowledgeService` | INT | фактический рестарт процесса методом `harness/restart_check.py` | после перезапуска поиск и чанки читаются; active сохранён |
| D21-06 | R-15 | Повторный импорт без дублей | `SqliteIndexStore` (`find_ready_index`), `POST /api/index/build` | UNIT + INT | повторный импорт неизменного набора через API | ответ `{reused:true, status:"ready"}` с тем же `index_version_id`; 0 новых строк; `chunk_id` те же |
| D21-07 | R-04, R-14 | Изоляция/переключение двух коллекций | `KnowledgeService`, UI | INT + UI | две коллекции, разные index version, переключение `PUT /api/collections/{collection_id}/active-index` (SPEC §11.1) | поиск scoped; переключение без правок UI/поиска |
| D21-08 | R-09, R-13, R-15 | Отказ при несовместимой модели | `check_index_compatibility` | UNIT + INT | поиск с отличающимися `model`/`digest`/`dimension`/`normalization`/`dtype`/`document_prefix`/`query_prefix`/`corpus_schema_version` | 409 `index_incompatible` `{expected, actual}`; эмбеддинг запроса не вызывается |
| D21-09 | R-10, R-16 | Отказ на пустом/битом файле | `SourceAdapter`, API | UNIT + INT | пустой/битый/зашифрованный/без текстового слоя PDF, пустой TXT | 422 с типизированным кодом; сервис не падает |
| D21-10 | R-15 | Прерванный build не ready/active | `SqliteIndexStore`, старт приложения | UNIT + INT | `harness/interrupt_check.py`; `mark_stale_builds_failed("interrupted")` | прерванный → `failed`/`interrupted`; old active не тронут; частичные чанки удалены |
| D21-11 | R-14, R-16, R-19, R-20 | Реальный запуск и UI | `run_app.bat`, UI | MANUAL + UI | фактический запуск; Developer и независимо Tester | UI открывается, сборка/прогресс/поиск/ошибки видны; при отсутствии Ollama — `Embedding: unreachable` |
| D21-12 | R-17, R-19 | Воспроизводимый README с измеренным сравнением и видео | README, manifest metrics | MANUAL | измеренные метрики `fixed` vs `structure`; сценарий видео | README воспроизводим по шагам; в нём фактические числа |

### 2.1. Must / Later

* **Must** — D21-01…D21-12 и всё из SPEC §2.1.
* **Later** — Wiki importer (формат ZIM/XML не установлен; не предполагать XML BZ2),
  внешний клиент/MCP, ANN, chat RAG, reranker, OCR, resume/инкрементальная переиндексация,
  удаление старых версий, multi-process/Postgres/Docker.

---

## 3. Граничные и ошибочные сценарии

| Сценарий | Ожидаемое поведение | Уровень |
| --- | --- | --- |
| Пустой файл / нет текста | `SourceEmpty`/`SourceNoTextLayer` → 422 | UNIT + INT |
| Битый/зашифрованный PDF | `SourceInvalid`/`SourceEncrypted` → 422 | UNIT + INT |
| Неподдерживаемый формат | `SourceUnsupported` → 422 | UNIT + INT |
| Ollama недоступен | `EmbeddingUnavailable` → 503; UI `unreachable`; приложение стартует | UNIT + INT |
| Таймаут embedding | `EmbeddingTimeout` → 503 (единый код с `EmbeddingUnavailable`, SPEC §7.3) | UNIT |
| Модель отсутствует | `EmbeddingModelMissing`; подсказка `ollama pull embeddinggemma:300m` | UNIT + LIVE |
| Слишком длинный вход | `EmbeddingLengthError`; явный split без потери конца (I6) | UNIT |
| Число/размерность/неfinite | `EmbeddingCountMismatch`/`EmbeddingDimensionMismatch`/`EmbeddingInvalidVector` | UNIT |
| Двойной префикс | `EmbeddingUsageError` | UNIT |
| Несовместимый индекс | 409 `index_incompatible` до эмбеддинга запроса | UNIT + INT |
| Неготовый индекс | 409 `index_not_ready` | UNIT + INT |
| Второй build | 409 `index_busy` | UNIT + INT |
| Неизвестный `schema_version` | 500 `store_schema_unsupported` | UNIT |
| Изменённые байты источника | новый source/document/fingerprint + новый index version | UNIT + INT |
| Поиск во время build | идёт по active ready (WAL), не блокируется | INT |

---

## 4. Запуск и артефакты

* Доверенные точки входа: `.\week-05\knowledge-agent\setup.bat`, `test.bat`,
  `smoke_test.bat`, `run_app.bat` (создаёт **Configurator**, не Developer; точное
  содержимое — PLAN §10).
* Режимы `test.bat`: `unit` (по умолчанию), `integration`, `live`, `acceptance`
  (PLAN §10.2). Проверяется фактическим выполнением, а не чтением: создание окружения и
  установка зависимостей (`setup.bat`), UNIT (`test.bat unit`), INT
  (`test.bat integration`, `smoke_test.bat`), запуск UI (`run_app.bat`).
* **OpenAPI drift**: спецификация OpenAPI 3.1 из FastAPI сохраняется в репозиторий;
  проверка, что артефакт не дрейфует относительно кода (snapshot-тест); пути и формы
  ответов сверяются с SPEC §11 как единственным источником истины.
* **Отсутствие секретов и абсолютных путей**: проверка, что API/UI/README/логи/manifest
  не содержат содержимого `.env`, ключей и абсолютных локальных путей (I7).
  `sources.uri` с явным локальным путём допускается **только** в локальной БД; наружу
  (metadata/API) идёт санитизированный `public_uri`/`source_label` (D21-04).
* Тесты не читают `.env` и изолированы от сети (I9); реальный Ollama — только opt-in.

---

## 5. Метрики и сравнение

Измеряются и публикуются в `manifest_json` и UI (SPEC §14.4):

* `chunks count`;
* min/median/p95/max длины (токены и символы);
* `overlap_overhead`, `section_crossing_ratio`;
* `parse_seconds`, `embed_seconds`, `store_seconds`, `build_seconds`, `chunks_per_second`;
* `embed_latency` median/p95;
* `input_tokens` (или `null`, если провайдер не вернул);
* `db_size_bytes`, `vector_bytes`.

**Сравнение стратегий** — на одинаковом очищенном тексте и одной embedding-конфигурации;
таблица сравнения в UI и README. Output tokens генерации и output tok/s для embedding —
н/д (не выдумывать).

---

## 6. Итоговые статусы

* `UNIT_STATUS`
* `INTEGRATION_STATUS`
* `EMBEDDING_LIVE_STATUS` (только при `MODEL_CHECK_KIND: LOCAL`; иначе описательно —
  не запускалось / MOCK)
* `RESTART_STATUS`
* `UI_E2E_STATUS`
* `MANUAL_METRICS_STATUS`
* `TEST_STATUS` (PASS только при проверке всех обязательных критериев на достаточном
  уровне; иначе FAIL/BLOCKED с указанием непроверенного)

Правило: если для обязательного критерия нужен реальный Ollama/UI, но нет разрешения или
доступа, возвращается `BLOCKED` по этому критерию, а не полный PASS. Значения токенов
проверяемой локальной модели не смешиваются с токенами OpenCode-агента; один вызов не
учитывается дважды.

---

## 7. Чек-лист видео/README

- [ ] README воспроизводим: точные шаги запуска и проверки (русский, UI — английский).
- [ ] Указаны измеренные числа сравнения `fixed` vs `structure` (не оценки).
- [ ] Отмечено, что корпус не в Git, путь задаёт пользователь, сеть/`.env` не нужны.
- [ ] Отмечено отсутствие коммитов/пушей и отсутствие приватных данных.
- [ ] Видео-сценарий: запуск → отсутствие модели (подсказка) → pull → сборка с прогрессом
      → поиск → просмотр происхождения → смена коллекции → таблица сравнения → отказ на
      несовместимой модели и битом файле.
- [ ] Честно отмечено, что не проверено только чтением (версия Ollama, digest/dimension,
      семантика batch/truncate, качество пользовательского PDF, порядок чтения издания,
      UI в браузере, рестарт/прерывание, тайминги, `.bat`, видео).
