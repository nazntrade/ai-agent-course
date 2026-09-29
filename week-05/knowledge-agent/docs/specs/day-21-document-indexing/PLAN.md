# PLAN — Day 21: индексация документов и базовый поиск

Идентификатор задачи: `day-21-document-indexing`.
Статус: **ожидает spec review** (план реализации; код не создаётся этим документом).
Дата: 2026-09-29.
Связанные документы: `SPEC.md` (требования), `ACCEPTANCE.md` (критерии D21-01…D21-12).

План трассируем до требований SPEC и acceptance criteria. Порядок шагов выбран от
устойчивого ядра к внешним деталям: domain → text → chunkers → store → service →
embedder → pdf → API → UI → harness.

---

## 1. Файловая структура (предлагаемая)

```
week-05/knowledge-agent/
  README.md                      # публичная инструкция (русский)
  requirements.txt               # фиксирует зависимости (§16 SPEC)
  .env.example                   # переменные без значений
  setup.bat / test.bat / smoke_test.bat / run_app.bat   # создаёт Configurator (см. §10)
  knowledge_agent/
    __init__.py
    __main__.py                  # точка входа приложения (uvicorn app, см. §10.4)
    config.py                    # load_settings(env: Mapping | None)
    domain/
      __init__.py
      models.py                  # SourceRef, ExtractionInfo, Section, Document, ChunkMetadata, Chunk
      errors.py                  # таксономия ошибок SPEC §7.2
      contracts.py               # SourceAdapter, Tokenizer, Chunker, Embedder, EmbedderIdentity,
                                 # EmbedBatchResult, IndexStore, check_index_compatibility
    text/
      __init__.py
      tokenizer.py               # Tokenizer v1 (regex \w+|[^\w\s], Unicode)
      normalize.py               # render_markdown(document), NFC/лигатуры
    sources/
      __init__.py
      text_source.py             # TextSourceAdapter (TXT/MD)
      pdf_source.py              # PdfSourceAdapter (pdfplumber, fallback pypdfium2)
      pdf_layout.py              # колонки, колонтитулы, дегифенация, offset-карта
    chunking/
      __init__.py
      fixed.py                   # FixedChunker (500/75)
      structure.py               # StructureChunker (800/64, overlap 0)
    embedding/
      __init__.py
      ollama_embedder.py         # OllamaEmbedder + preflight + префиксы + батчи
    storage/
      __init__.py
      sqlite_store.py            # SqliteIndexStore (схема, WAL, активация, дедуп)
      schema.py                  # schema_meta, DDL, schema_version
    service/
      __init__.py
      knowledge_service.py       # KnowledgeService
    api/
      __init__.py
      app.py                     # FastAPI, OpenAPI 3.1
      routes.py                  # /api/* (SPEC §11)
    ui/
      index.html                 # static UI
      app.js
      styles.css
  harness/
    embed_stub.py                # INT-stub (stdlib HTTP, sha256-векторы, инъекция ошибок)
    smoke.py                     # INT-сценарий: коллекции, build, переключение, поиск
    live_embed.py                # opt-in LIVE: реальный Ollama (test.bat live)
    acceptance.py                # агрегатор ручных/opt-in сценариев, сводные статусы
    restart_check.py             # фактический рестарт процесса (D21-05)
    interrupt_check.py           # прерывание build (D21-10)
  tests/
    fixtures/
      mini.pdf                   # синтетический минимальный PDF (без новых зависимостей)
      sample.txt
      sample.md
    unit/...
    integration/...
```

Имена модулей уточняются на реализации; контракты и границы — обязательны.

---

## 2. Порядок работ P-01…P-11

| ID | Шаг | Артефакты | Требование |
| --- | --- | --- | --- |
| P-01 | Domain-контракты | `domain/models.py`, `domain/errors.py`, `domain/contracts.py` | SPEC §6, §7 |
| P-02 | Токенизатор и нормализация | `text/tokenizer.py`, `text/normalize.py` | SPEC §8.4 |
| P-03 | Chunker-ы | `chunking/fixed.py`, `chunking/structure.py` | D21-02 |
| P-04 | SQLite store | `storage/schema.py`, `storage/sqlite_store.py` | SPEC §9 |
| P-05 | KnowledgeService | `service/knowledge_service.py` | SPEC §5, §7 |
| P-06 | Embedder | `embedding/ollama_embedder.py` | SPEC §8 |
| P-07 | PDF-адаптер | `sources/pdf_source.py`, `sources/pdf_layout.py`, `sources/text_source.py` | D21-01, D21-04 |
| P-08 | HTTP API + OpenAPI + точка входа | `api/app.py`, `api/routes.py`, `__main__.py` | SPEC §11 |
| P-09 | UI | `ui/*` | SPEC §12 |
| P-10 | Harness | `harness/embed_stub.py`, `harness/smoke.py`, `harness/live_embed.py`, `harness/acceptance.py`, `harness/restart_check.py`, `harness/interrupt_check.py` | §5 |
| P-11 | Документация, сравнение, README | `README.md`, отчёт | D21-12 |

Зависимости: P-01 → P-02 → P-03; P-01 → P-04 → P-05; P-01 → P-06; P-04/P-06 → P-05;
P-02/P-03/P-04/P-06 → P-07; P-05 → P-08 → P-09; P-04..P-09 → P-10 → P-11.

---

## 3. Ключевые решения

* **HTTP-контракт (пути, параметры, формы ответов) — только в SPEC §11.** PLAN и код
  ссылаются на него и не дублируют пути, чтобы не возникало расхождений.
* Единый контракт `Document`/`Section` для обоих адаптеров; chunker и embedder не знают
  про формат источника.
* Префиксы `document_prefix`/`query_prefix` — только в `OllamaEmbedder`; двойной
  префикс → ошибка.
* `check_index_compatibility` — чистая функция; вызов до любого эмбеддинга запроса.
* `chunk_id` детерминирован (§6.2); `INSERT OR IGNORE` обеспечивает дедуп.
* Активация индекса атомарна в `BEGIN IMMEDIATE`; один build на процесс.
* Точный cosine — только для учебного корпуса; контракт `IndexStore` готов к ANN.
* Токенизатор v1 offline и детерминирован; единица (`token`/`char`) фиксируется в manifest.

---

## 4. Тесты по уровням и фикстуры

### 4.1. UNIT (без сети и `.env`)

* `tokenizer`, `normalize` — детерминизм, Unicode, дегифенация.
* `FixedChunker` — 500/75, отсутствие потери текста (I6).
* `StructureChunker` — границы секций, дробление длинных, `section_path`, `role`.
* `chunk_id` — устойчивость и изменение при смене params/section/ordinal.
* `check_index_compatibility` — каждое расхождение → `index_incompatible` с
  `{expected, actual}`; эмбеддинг запроса не вызывается.
* `SqliteIndexStore` — схема, WAL, `foreign_keys=ON`, `INSERT OR IGNORE`, дедуп
  (`{reused:true}`), атомарная активация, `fail_index` в одной транзакции,
  `mark_stale_builds_failed`.
* `OllamaEmbedder` с мок-HTTP — батчи, `truncate=false`, проверка count/dim/finite,
  `EmbeddingLengthError` без потери конца, двойной префикс, `input_tokens=null`.
* Таксономия ошибок источника: `SourceEmpty`, `SourceNoTextLayer`, `SourceEncrypted`,
  `SourceInvalid`, `SourceUnsupported`.
* `load_settings(env: Mapping|None)` — без чтения `.env`.

### 4.2. INT (реальные процессы, loopback; без внешней сети)

* `harness/embed_stub.py` как локальный HTTP-совместимый провайдер: полный цикл
  build → search на двух стратегиях.
* Две коллекции: изоляция и переключение active index (D21-07).
* Повторный импорт того же файла — без новых строк (D21-06).
* Прерванный build: `building` → `failed`/`interrupted`, old `active` не тронут (D21-10).
* Фактический рестарт процесса: индекс читается после перезапуска (D21-05).
* Пустой/битый/зашифрованный файл → 422 без падения сервиса (D21-09).
* Несовместимая конфигурация (изменённый prefix/dimension через stub) → 409 (D21-08).

### 4.3. Фикстуры

* **Синтетический минимальный PDF** — генерируется без новых зависимостей (байты PDF
  собираются стандартной библиотекой или заранее подготовленным крошечным файлом), чтобы
  проверить извлечение, колонки и offset-карту детерминированно.
* **TXT/MD** — маленький второй источник для проверки контракта `TextSourceAdapter`.
* **Реальный корпус** (42-стр. PDF) в Git **не** попадает; используется только в
  ручных/opt-in сценариях, путь задаёт пользователь.

---

## 5. Harness и режимы `.bat`, opt-in LIVE, restart/interrupt

* `test.bat` — режимы `unit` (по умолчанию), `integration`, `live`, `acceptance`
  (точное содержимое — §10). `unit`/`integration` без сети и `.env` (через stub);
  `live`/`acceptance` используют реальный Ollama **только по явному разрешению
  пользователя**.
* `smoke_test.bat` — поднимает сервис, гоняет INT-сценарий с stub, проверяет
  переключение коллекций и поиск.
* `run_app.bat` — фактический запуск UI (режимы — §10).
* `harness/embed_stub.py` — детерминированный stub; **никогда** не маркируется как
  inference.
* `harness/restart_check.py` — фактический рестарт процесса (D21-05).
* `harness/interrupt_check.py` — принудительное прерывание build (D21-10).
* LIVE-статус: `MODEL_CHECK_KIND: LOCAL`, реальная модель `embeddinggemma:300m`;
  измерения: input tokens (если доступны), chunks/sec, latency; output tokens и
  output tok/s — н/д.

Штатные `.bat` (`setup/test/smoke_test/run_app`) создаёт **Configurator**, не Developer
(§10).

---

## 6. Метрики: формулы, сбор, публикация

* Длины чанков: `tokens = Tokenizer.tokenize(chunk.text)`; `chars = len(chunk.text)`;
  min/median/p95/max по обоим.
* `overlap_overhead = (Σ overlap_tokens) / (Σ unique_content_tokens)`.
* `section_crossing_ratio = chunks_with_more_than_one_section / total_chunks`.
* Время: `parse_seconds`, `embed_seconds`, `store_seconds`, `build_seconds`;
  `chunks_per_second = chunks / build_seconds`.
* `embed_latency` median/p95 по батчам.
* `input_tokens` — из usage провайдера, иначе `null` (не выдумывать).
* `db_size_bytes` — размер файла БД; `vector_bytes = count × dimension × 4`.
* Сравнение стратегий — на **одинаковых** границах разделов и одной embedding-конфигурации.
* Публикация — в `manifest_json.counts/metrics` и в таблице сравнения UI; в README —
  измеренные значения.

---

## 7. Ручные сценарии и сценарий видео

* Реальный запуск (`run_app.bat`): выбор коллекции/стратегии, сборка, прогресс,
  просмотр чанка и metadata, поиск фрагментов.
* Реальная индексация 42-стр. PDF локальным `embeddinggemma:300m` (opt-in) — измеренные
  метрики и сравнение `fixed` vs `structure`.
* Отсутствие Ollama: UI показывает `Embedding: unreachable` и подсказку
  `ollama pull embeddinggemma:300m`.
* Сценарий видео (кратко): запуск → отсутствие модели (подсказка) → pull → сборка с
  прогрессом → поиск → просмотр происхождения → смена коллекции → таблица сравнения →
  отказ на несовместимой модели и на битом файле. Видео/README — см. ACCEPTANCE §7.

---

## 8. Трассировка P→D21

| Шаг | R-требования (SPEC §17.2) | D21 |
| --- | --- | --- |
| P-01, P-04 | R-05, R-06, R-07, R-12, R-15 | D21-04, D21-06, D21-10 |
| P-02, P-03 | R-02, R-11 | D21-02 |
| P-05 | R-04, R-12, R-13, R-14 | D21-05, D21-07, D21-08 |
| P-06 | R-08, R-09, R-10 | D21-03, D21-08 |
| P-07 | R-01, R-02, R-06, R-10 | D21-01, D21-04, D21-09 |
| P-08, P-09 | R-14, R-16 | D21-07, D21-11 |
| P-10 | R-12, R-15, R-18 | D21-05, D21-10, D21-12 |
| P-11 | R-01, R-17, R-19 | D21-01, D21-12 |

---

## 9. Риски и снижение

### 9.1. Риски PDF

| Риск | Снижение |
| --- | --- |
| Скан без текстового слоя | `SourceNoTextLayer` (422); OCR — Later |
| Неверный порядок колонок | детекция «долины» по x-профилю; full-width заголовки; порядок left→right; ручная проверка на издании |
| Колонтитулы | y-полосы 8%, частота ≥60% |
| Дегифенация | `-` + строчная; метрика по словам |
| Лигатуры / NFC | нормализация NFC, разложение лигатур; проверка на фикстурах |
| Формулы/таблицы | сохранение координат и warning; не гарантируется корректный текст |
| References/appendix | явное исключение `references` через `excluded_roles` |
| Дрейф версии библиотеки | фиксация версии в `requirements.txt`; запись `extraction_version` |
| Производительность 42 стр. | батчи, измерение parse/embed/store, отказ от лишних перепроходов |
| Зашифрованный/битый PDF | `SourceEncrypted`/`SourceInvalid` (422) |
| TOCTOU (файл изменён между hash и чтением) | hash и чтение из одного открытого потока; повторная сверка `content_sha256` |
| Сдвиг offset-карты страниц | page offset-карта, per-section `start_bbox`; тест на фикстуре |
| Лицензия/приватность корпуса | публичные arxiv-источники; корпус не в Git; нет частных названий |

### 9.2. Что нельзя проверить только чтением кода

Явно требуют фактического выполнения (а не чтения спецификации/кода):

* версия Ollama (`GET /api/version`);
* `digest` и `dimension` модели (`POST /api/show`, `model_info.embedding_length`);
* семантика batch/`truncate`/ошибки длины на реальном endpoint;
* качество пользовательского PDF (порядок чтения, колонки, колонтитулы);
* порядок чтения конкретного издания;
* UI в браузере (прогресс, таблица сравнения, ошибки);
* рестарт и прерывание на живых процессах;
* тайминги и метрики (parse/embed/store, latency);
* `.bat`-точки входа (создают, ставят зависимости, запускают);
* видео и README-сценарий.

Эти пункты помечаются «не проверено» до фактического прогона и не считаются
выполненными по умолчанию.

---

## 10. Приложение A: точное содержимое `.bat` (создаёт Configurator)

Developer в D21 `.bat` **не создаёт**: файлы защищены permission-правилом
(`edit: **/*.bat = deny`), поэтому их создаёт **Configurator** (governance-граница).
Ниже — точное согласованное содержимое; Developer реализует ровно эти точки входа и
ссылающиеся на них модули.

Общее для всех `.bat`: `cd /d "%~dp0"`, `PYTHONUTF8=1`, `PYTHONIOENCODING=utf-8`,
автосоздание `.venv` и установка `requirements.txt` при первом запуске, только
относительные пути и `%~dp0` (никаких абсолютных путей). Порты/хосты берутся из
конфига: `KNOWLEDGE_HOST` (default `127.0.0.1`), `KNOWLEDGE_PORT` (default `8770`;
свободный порт, не совпадающий с backend Week 4). Значения перечислены в `.env.example`.

### 10.1. `setup.bat`

Создаёт `.venv`, ставит зависимости из `requirements.txt`, при отсутствии `.env`
печатает подсказку скопировать `.env.example` (сам `.env` с секретами не создаёт).
Тесты не запускает. Коды выхода: `0` — успех, `2` — ошибка создания окружения/установки.

### 10.2. `test.bat`

```
test.bat               -> режим unit (по умолчанию): python -m pytest tests/unit
test.bat unit          -> python -m pytest tests/unit
test.bat integration   -> python -m pytest tests/integration
                          (harness/embed_stub.py, без сети и без .env)
test.bat live          -> RUN_EMBED_LIVE=1 python harness/live_embed.py
                          (opt-in; реальный Ollama/embeddinggemma:300m)
test.bat acceptance    -> python harness/acceptance.py
                          (агрегатор статусов; LIVE только при opt-in)
```

Проверки: `unit` — логика ядра на stub; `integration` — реальный HTTP к stub
(loopback), две коллекции, дедуп, прерывание build, отказ 422/409;
`live` — реальный локальный embedding (`MODEL_CHECK_KIND: LOCAL`).
Коды выхода: `0`/`1`/`2`; печатаются строки `UNIT_STATUS:`, `INTEGRATION_STATUS:`,
`EMBEDDING_LIVE_STATUS:`, `TEST_STATUS:` (когда применимо).

### 10.3. `smoke_test.bat`

Поднимает backend `python -m knowledge_agent` (`knowledge_agent/__main__.py`) на
`KNOWLEDGE_HOST:KNOWLEDGE_PORT` и `harness/embed_stub.py`, затем запускает
`python harness/smoke.py`. Сценарий: создание двух коллекций, build `fixed` и
`structure`, переключение active index параметром `PUT /api/collections/{collection_id}/active-index`,
поиск фрагментов, повторный импорт без дублей (`reused:true`), прерывание build. Exit-код runner-а
пробрасывается (`0`/`1`/`2`); свои процессы harness останавливает сам, чужие не трогает.

### 10.4. `run_app.bat`

```
run_app.bat            -> режим all (по умолчанию)
run_app.bat api        -> только backend
run_app.bat ui         -> открыть UI без запуска сервера
run_app.bat stub       -> поднять harness/embed_stub.py и backend
```

* `all`: поднять `harness/embed_stub.py` только если `EMBED_BASE_URL` указывает на
  stub, иначе использовать реальный Ollama из `EMBED_BASE_URL`; поднять backend
  `python -m knowledge_agent` (`knowledge_agent/__main__.py`); дождаться
  `GET /api/health`; открыть в браузере
  `http://127.0.0.1:<KNOWLEDGE_PORT>/`; по выходу остановить только свой PID.
* `api`: backend в foreground.
* `ui`: открыть UI, сервер не запускать.
* `stub`: поднять stub и backend (для ручной проверки без Ollama).

При отсутствии `.env`/Ollama UI всё равно стартует и показывает
`Embedding: unreachable` и подсказку `ollama pull embeddinggemma:300m`. Занятый порт →
сообщение и exit `2`; чужие процессы не завершаются.

Все точки входа соответствуют PROJECT_RULES: `.\week-05\knowledge-agent\*.bat`.
