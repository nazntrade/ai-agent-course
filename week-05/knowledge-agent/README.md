# Knowledge Agent

`knowledge-agent` — независимый учебный модуль курса Week 5 (`week-05/knowledge-agent`).
Модуль наследует правила курсового корня (`AGENTS.md`, `PROJECT_RULES.md`) и не имеет
собственных `opencode.json` и ролей агентов.

## Что наследуется

- `AGENTS.md` курсового корня — переносимые правила агентов.
- `PROJECT_RULES.md` курсового корня — правила курса и выбранный профиль `Backend AI`.
- Роли агентов и маршрутизация из курсового корня.

## Что содержит модуль

- `MODULE_RULES.md`, `MODULE_STATE.md` — дельта правил и состояние модуля.
- `setup.bat`, `test.bat`, `smoke_test.bat`, `run_app.bat` — доверенные точки входа.
- `knowledge_agent/` — продуктовый код (domain, text, sources, chunking, embedding,
  storage, service, api, ui).
- `harness/` — stub-embedder и сценарии (smoke, live, acceptance, restart, interrupt).
- `tests/unit`, `tests/integration` — тесты; `.env.example` — заглушки переменных.
- `docs/specs/day-21-document-indexing/` — утверждённая спецификация D21.

## Day 21 — индексация документов и поиск по фрагментам

Задача D21: пользователь указывает **явный путь** к источнику через конфиг или UI,
модуль извлекает текст, разбивает его на секции и чанки, считает локальные embeddings
моделью `embeddinggemma:300m` (Ollama `POST /api/embed`) и сохраняет их в локальный
SQLite-индекс. Поиск возвращает **фрагменты** с происхождением; генерации ответа нет.

Поток: `SourceAdapter → Document → Chunker → Embedder → IndexStore → KnowledgeService →
FastAPI + static UI`. Ядро не знает про SQLite, Ollama и PDF-библиотеки.

Две стратегии чанкинга работают на одном очищенном тексте и одной embedding-конфигурации:

- `fixed` — скользящее окно 500 лексических единиц с overlap 75;
- `structure` — границы реальных разделов, упаковка абзацев, `max_tokens=800`,
  `min_tokens=64`, overlap 0.

### Команды (из каталога модуля)

| Команда | Назначение |
| --- | --- |
| `setup.bat` | создать `.venv` и установить `requirements.txt` |
| `test.bat unit` | unit-тесты (без сети и `.env`) |
| `test.bat integration` | интеграционные тесты через локальный stub |
| `test.bat live` | opt-in LIVE: реальный Ollama `embeddinggemma:300m` |
| `test.bat acceptance` | агрегатор unit/integration/restart/interrupt; LIVE и read-only аудит отдельно opt-in |
| `smoke_test.bat` | stub + backend с TEMP БД; HTTP-сценарий и UI-assets, без браузерной проверки |
| `run_app.bat` | запуск приложения (режимы `all`, `api`, `ui`, `stub`) |

### Конфигурация (`.env`)

`.env` приложением не создаётся; шаблон без значений — `.env.example`. Ключевые
переменные: `KNOWLEDGE_HOST`, `KNOWLEDGE_PORT` (по умолчанию `8770`), `KNOWLEDGE_DB_PATH`,
`KNOWLEDGE_SOURCE_PATH` (пусто), `EMBED_BASE_URL`, `EMBED_MODEL`, `EMBED_BATCH_SIZE`,
`EMBED_TIMEOUT_SECONDS`, `CHUNK_*`, `PDF_USEFUL_PAGE_MIN_CHARS`. Приложение не сканирует
диск: путь к файлу задаёт пользователь.

### Как пользоваться (UI)

1. Запустить `run_app.bat` (при отсутствии Ollama UI всё равно стартует и показывает
   `Embedding: unreachable` с подсказкой `ollama pull embeddinggemma:300m`).
2. Создать коллекцию, добавить источник (явный путь к PDF/TXT/MD) и нажать **Build**.
3. Дождаться ready; первая готовая версия становится активной, дальнейшие выбираются через Set active.
4. Ввести запрос в **Search** и посмотреть фрагменты; клик по чанку показывает metadata.
5. **Compare** строит таблицу `fixed` vs `structure` на одном тексте.
6. Переключение активной версии — кнопкой **Set active** (тот же `PUT`, что и в API).

## Корпус D21-01

Основной источник — PDF «A Survey on Large Language Model based Autonomous Agents»
(текстовый слой, 42 страницы). Путь к файлу задаёт пользователь; сам файл в Git не
попадает (каталог `local-data/` игнорируется).

Извлечение через `pdfplumber` (`pdf-v3`); свежий LIVE 30.09.2026 отдельно проверяет объём после исключений:

- `page_count = 42`, `useful_pages = 42`, `useful_chars ≈ 148 547`, язык `en`;
- порог `PDF_USEFUL_PAGE_MIN_CHARS = 500` преодолевают все 42 страницы до исключений;
- **33 полезные индексируемые страницы** после исключения библиографии, проверено свежим LIVE;
- обнаружено 27 разделов (26 `body` + 1 `references`); реальные заголовки:
  `1 Introduction`, `2.1 Agent Architecture Design`, `2.1.1 Profiling Module`,
  `7 Conclusion` и т. д.;
- раздел `References` исключается из индексации явно: в manifest записывается
  `excluded_roles: ["references"]` (строка `References` в этом издании склеена с первой
  ссылкой, остаток текста сохраняется как тело секции).

**Вывод:** полезного текста ≥ 20 страниц, поэтому второй источник
(`https://arxiv.org/pdf/2312.10997`) из SPEC §17.4 **не добавлялся** — корпуса достаточно.

Порядок чтения двухколоночного текста восстанавливается (левая колонка → правая,
full-width заголовки/подписи как разделители). Водораздел колонок определяется как
x-позиция с минимальным числом пересекающих слов, а строка, смешавшая левую и правую
ячейки на одной базовой линии, делится по зазору
(`detect_column_valley`/`_split_line_at_valley`/`order_lines`).
Повторяющиеся колонтитулы удаляются (на этом файле удалено 41 строка), переносы
слов восстанавливаются.

В `pdf-v3` исправлена отдельная ошибка переноса с промежуточной строкой. Слово со
смешанными шрифтами получало bbox выше остальных слов строки, и группировка строк
только по `top` (допуск 2.5 pt) делала его отдельной строкой между половинами
перенесённого слова; `join_paragraphs` затем склеивал половину с этим словом. Теперь
`group_lines` объединяет слова по совпадающей **базовой линии либо `top`** в пределах
допуска: перенос соединяется с настоящим продолжением, а промежуточное слово
(`different` на стр. 3) сохраняется отдельным токеном и не удаляется. Для плотной
вёрстки без пробельных глифов используется масштабируемый порог
`extract_words(x_tolerance=1.0, x_tolerance_ratio=0.15)`: слова, разделённые зазором
всего ~2.8 pt, разделяются, а слова с трекингом не режутся — проверено фикстурами
`tests/fixtures/mini_pdf.py` и тестами `tests/unit/test_sources.py`; случай с
промежуточной строкой закреплён `tests/unit/test_pdf_two_column.py`. Офлайн-диагностика
(`RUN_KNOWLEDGE_READONLY_AUDIT=1 test.bat acceptance`, только чтение): `PDF_ORDER` для страниц 3, 4, 10–13, 32,
`PDF_FRAGMENT` — сырые слова/символы и итоговый абзац `1 Introduction`,
`PDF_CHUNKS` — текст новых чанков `fixed`/`structure`. Остаются артефакты исходного
текстового слоя в подписях к рисункам/таблицам и наложенных правках (например,
отдельные full-width строки на стр. 32); порядок чтения они не ломают.
Fallback `pypdfium2` остаётся упрощённым и не применяет масштабируемый порог — это
осознанное ограничение (используется только если `pdfplumber` недоступен).

## Измеренное сравнение `fixed` vs `structure` (LIVE)

Свежий изолированный прогон 30.09.2026: `test.bat live`, exit 0,
`LIVE_CLEANUP: PASS`, `EMBEDDING_LIVE_STATUS: PASS`. Ollama 0.35.0,
`embeddinggemma:300m`, dimension 768, digest `85462619…79f1`.
Один PDF, одна конфигурация embeddings, `pdf-v3`, `references` исключены,
33 полезные страницы тела. SQLite создаётся в новом TEMP-каталоге;
каждая стратегия действительно вычисляет векторы, прежняя рабочая БД не используется.

| Метрика | `fixed` | `structure` |
| --- | --- | --- |
| chunks | 50 | 61 |
| лексические единицы min/median/p95/max | 168/500/500/500 | 36/341/574/631 |
| overlap_overhead | 0.175058 | 0 |
| section_crossing_ratio | 0.34 | 0 |
| build_seconds | 3.608613 | 3.708038 |
| input_tokens (usage Ollama) | 27 217 | 23 144 |
| embed_latency median | 276.542 ms | 237.227 ms |
| chunks_per_second | 13.855739 | 16.450747 |
| vector_bytes | 153 600 | 187 392 |

Размеры чанков измеряет `lexical-v1`, а не tokenizer EmbeddingGemma.
`input_tokens` — отдельный фактический usage провайдера. Минимум Structure 64 —
порог упаковки; короткие самостоятельные секции могут остаться меньше него.
`db_size_bytes` отражает только основной SQLite-файл и не включает WAL, поэтому
не используется как размер всего индекса; `vector_bytes` отражает сохранённые float32.
Времена получены на прогретой модели и не являются SLA или benchmark.
Для embeddings output tokens и скорость генерации текста неприменимы.

Fixed использует меньше векторов, но повторяет около 17.5% лексического текста
и пересекает границы разделов в 34% чанков. Structure сохраняет границы и
тратит меньше фактических входных токенов, но создаёт больше векторов.
На запрос `planning in LLM agents` Structure первым вернул Planning Module;
Fixed первым вернул текст диаграммы из Memory Module. Это полезный пример
влияния chunking и PDF-артефактов, а не доказательство универсального превосходства.
В браузере английский и русский вопросы о памяти вернули тематические фрагменты
Memory Module. Полной размеченной оценки качества retrieval ещё нет.

### Идентичность обработки и сохранение старых данных

Fingerprint включает упорядоченные источники, содержимое и происхождение,
effective extraction version, normalization/exclusion policy, chunking и embeddings.
TXT и Markdown различаются как `text-v1:plain` и `text-v1:markdown`.
Идентичность parsed document/section также учитывает processing policy;
новая сборка не изменяет разделы ранее готовой версии.
`manifest.sources[]` хранит per-source `source_id` и `extraction_version`;
`pipeline.extraction_versions` содержит сводку. Нормализация — `norm-v1`,
корпус — `corpus-v3`. Настройки совместимости при поиске проверяются до
вычисления embedding запроса; изменение источника создаёт новую версию,
но не означает само по себе несовместимость embedding-пространства.

Сравнение проверяет digest очищенного корпуса, обработку и embedding identity.
Ready-версии с legacy manifest без достоверного digest остаются читаемыми,
но не выдаются за сопоставимые с новыми. API безопасно проецирует legacy
абсолютные labels/URI без изменения сохранённых строк и ID.
Повтор неизменной ready-сборки возвращает `reused:true`, прежний ID
и не создаёт новых строк; обе стратегии проверены свежим LIVE.
Пересборка после исправления извлечения создаёт новую версию без удаления старой БД.

## Day 22 — первый RAG-запрос

Задача D22 (`day-22-first-rag-query`) добавляет поверх D21-retrieval генерацию ответа
отдельной локальной chat-моделью Ollama (`POST /api/chat`), режимы **с RAG** и **без RAG**,
сравнение ответов и оценку на наборе из 10 вопросов. D21-retrieval, индексы, происхождение
и изоляция коллекций не изменены; старый API продолжает работать.

Поток: `вопрос → KnowledgeService.search (D21, без изменений) → ContextBudget (heuristic-v1)
→ PromptTemplate → OllamaChatModel → citations → FileChatRunStore`.

Компоненты (`knowledge_agent/chat/`):

- `ollama_chat.py` — `OllamaChatModel` (preflight `/api/version`, `/api/tags`, `/api/show`;
  `chat` и `stream_chat`), stdlib `urllib` с инъектируемым transport, новых зависимостей нет;
- `context.py` — бюджет `heuristic-v1` (`ceil(utf-8 bytes / CHAT_CONTEXT_CHARS_PER_TOKEN)`),
  резерв под ответ, отбор `passed ⊆ found`, `context_overflow` без вызова модели;
- `prompts.py` — `plain-v1` и `rag-v1`; найденные чанки — недоверенные данные в отдельном
  блоке `<context>`, не в системной инструкции;
- `citations.py` — извлечение `[chunk_id]` и проверка по реально переданным `passed`;
- `run_store.py` — неизменяемые прогоны `chat-run-v1` и ручные оценки `chat-eval-v1`;
- `chat_service.py` — режимы, сравнение, streaming и сохранение.

### Конфигурация (`.env`)

`CHAT_MODEL` по умолчанию **пуст**: приложение не угадывает модель и не скачивает её
(`ollama pull` не выполняется). Ollama endpoint — `CHAT_BASE_URL` (по умолчанию
`http://127.0.0.1:11434`). Остальные настройки: `CHAT_TIMEOUT_SECONDS`,
`CHAT_MAX_OUTPUT_TOKENS`, `CHAT_CONTEXT_TOKENS`, `CHAT_TEMPERATURE`, `CHAT_SEED`,
`CHAT_TOP_K`, `CHAT_CONTEXT_CHARS_PER_TOKEN`, `CHAT_RUNS_PATH`. `EMBED_MODEL` и
`CHAT_MODEL` независимы: `embeddinggemma:300m` используется только для embeddings.

### Команды (из каталога модуля)

| Команда | Назначение |
| --- | --- |
| `test.bat unit` | unit-тесты D21 + D22 (без сети и `.env`) |
| `test.bat integration` | INT через `embed_stub` + `chat_stub` (loopback, TEMP) |
| `test.bat live` | opt-in LIVE: реальные `embeddinggemma:300m` и выбранная `CHAT_MODEL` |
| `smoke_test.bat` | стабы + backend: D21-сценарий и чат в обоих режимах |
| `run_app.bat` | запуск приложения и панели «Чат (RAG)» |

### Три действия UI (панель «Чат (RAG)»)

Новые элементы D22 — на русском (явное требование пользователя); существующие панели D21
не переведены.

1. **«Без RAG»** — только вызов chat-модели, без retrieval и embedding; работает даже без
   готового индекса.
2. **«С RAG»** — retrieval D21 по выбранной коллекции/индексу; показываются источники —
   только реально переданные (`passed`) фрагменты с происхождением, и счётчики
   «найдено / передано»; обрезка (`finish_reason = length`) и цитаты видны отдельно.
3. **«Сравнить»** — две колонки (С RAG / Без RAG) с ответом, источниками, usage и
   задержкой; «победитель» не выводится. Одна модель и одинаковые настройки, у каждой
   ветки своя пустая история.

Ошибки показываются понятным кодом: `chat_model_missing` с подсказкой
`ollama pull <CHAT_MODEL>`, `index_not_ready`, `index_incompatible`, `context_overflow`,
`chat_timeout` и т. д. Если streaming недоступен, UI показывает индикатор ожидания и
итоговый ответ.

### Раздельная оценка

- **Retrieval** — попали ли ожидаемые разделы/страницы в `found` и `passed`.
- **Содержание** — присутствуют ли ожидаемые факты; для неотвечаемого вопроса
  фиксируется отсутствие выдумывания.
- **Источники** — валидны ли цитаты и нет ли ссылок на непереданные чанки; валидная
  цитата доказывает только факт передачи `chunk_id`, но не смысловую верность ответа.

Набор `eval/d22/questions.json` (`rag-eval-questions-v1`) содержит ровно 10 вопросов:
9 отвечаемых и 1 неотвечаемый. Он вне индексируемого корпуса; корпус в Git не попадает.

### Прогоны результатов

`CHAT_RUNS_PATH` (по умолчанию `local-data/chat-runs/`, в Git не попадает):
`runs.jsonl`, `<run_id>.json` (неизменяемые `chat-run-v1`),
`evaluations/<run_id>.json` (`chat-eval-v1`). Повторный запрос создаёт новый `run_id`.

### Исходная проверка до подключения выбранного профиля

- UNIT: 208 passed (`test.bat unit`), включая 72 новых D22-теста; INT: 13 passed
  (`test.bat integration`), включая 6 новых INT-сценариев чата; `smoke_test.bat`:
  `SMOKE_STATUS: PASS`.
- LIVE chat / 10 пар / LIVE UI в исходном прогоне: **н/д (не выполнено)** — тогда `CHAT_MODEL`
  не была задана, реальная установленная chat-модель не была подтверждена, поэтому LIVE-прогон
  не выполнялся и не выдаётся за выполненный. Stub/`chat_stub` — имитация, не inference.
  Последующие реальные результаты приведены ниже в разделе от 2026-10-01.
  Для повторения: выбранный полный тестовый профиль либо явный `CHAT_MODEL`, затем `test.bat live`; 10 пар —
  `harness/rag_eval.py --live` при запущенном backend с готовым индексом.

### Сценарий видео (кратко)

1. Запуск `run_app.bat`.
2. Вопрос → **«Без RAG»** → ответ.
3. Тот же вопрос → **«С RAG»** → ответ и источники (`passed`).
4. **«Сравнить»** → две колонки, шаблоны, usage, задержки.
5. Ошибка при отсутствии модели (`chat_model_missing` + подсказка `ollama pull …`).
6. Сохранённые прогоны: `GET /api/chat-runs` и ручная оценка `chat-eval-v1`.

### Границы

- **D21 не изменён**: `/api/health` (кроме добавления блока `chat`), `/api/collections`,
  `/api/index/*`, `/api/search`, `/api/compare`, `/api/index-versions/*`, схема и семантика
  индексов, происхождение и изоляция коллекций — без изменений.
- **Later** (не входит в D22): MCP, Wikipedia importer, ANN backend, reranker, OCR,
  многосессионная память, полный многошаговый чат, resume/инкрементальная переиндексация,
  удаление старых версий, multi-process/Postgres/Docker, автоматическое скачивание моделей.

## Day 23 — реранкинг и фильтрация

Задача D23 (`docs/specs/day-23-rag-filtering/SPEC.md`) добавляет к RAG второй этап после
поиска: **фильтр релевантности** с настраиваемым порогом cosine similarity и раздельными
`prefilter_top_k` / `postfilter_top_k`, а также **отключаемый query rewrite**. Сравниваются
четыре режима:

- **A** — обычный RAG без фильтра и rewrite (**baseline**, это `with_rag`, а не «Без RAG»);
- **B** — RAG + фильтр;
- **C** — RAG + rewrite;
- **D** — RAG + rewrite + фильтр.

Поток: `вопрос → [QueryRewriter] → KnowledgeService.search (D21, без изменений) →
[RelevanceFilter] → ContextBudget → PromptTemplate → ChatModel → citations`.
Retrieval D21, индексы, метаданные, происхождение, «Без RAG», «С RAG» и сравнение D22
сохранены; все D23-поля опциональны, дефолты выключены (`RAG_FILTER_ENABLED=0`,
`RAG_REWRITE_ENABLED=0`), поэтому D22-совместимый запрос не меняет `retrieval.found`.

Новые компоненты:

- `knowledge_agent/chat/filtering.py` — `RelevanceFilter`: порог + postfilter top-K,
  причины исключения разделены на `threshold` / `top_k` / `context_budget`;
- `knowledge_agent/chat/rewrite.py` — `ChatQueryRewriter`: переформулирует только поисковый
  запрос, генерация отвечает на исходный вопрос; промпт `rewrite-v1` без эталонов, готовых
  ответов и истории; при ошибке/таймауте/пустом ответе — fallback на исходный запрос
  с сохранённой причиной;
- `knowledge_agent/chat/chat_service.py` — режимы A–D, `compare_modes`, трасса
  (`original_query`, `search_query`, `candidates`+scores, `selected`, `passed`,
  `exclusion_reasons`), раздельные метрики `rewrite` и генерации;
- отдельный экземпляр `ChatModel` для rewrite с коротким timeout
  (`RAG_REWRITE_TIMEOUT_SECONDS`), создаётся в `build_chat_service`;
- API: D23-поля `ChatRequest` и `POST /api/chat/compare-modes`;
- UI: большой чат сверху, панель `Retrieval settings`, показ `Search query`, трасса и
  `Compare four modes`; новые элементы D23 — на английском, существующие русские панели D22
  не переведены.

Если фильтр отсекает все чанки, возвращается понятное «No relevant sources found» без вызова
модели и без подмены режима обычным RAG. Отмена (разрыв клиента) закрывает провайдерский
поток и не создаёт фиктивной успешной записи.

### Команды D23

| Команда | Назначение |
| --- | --- |
| `test.bat unit` | unit-тесты D21 + D22 + D23 (без сети и `.env`) |
| `test.bat integration` | INT через `embed_stub` + `chat_stub` (loopback, TEMP) |
| `test.bat scenario d23-rag-filtering` | полный D23 LIVE: калибровка порога + 10 вопросов × 4 режима |
| `run_app.bat` | запуск приложения и панелей D22/D23 |

Артефакты прогона сохраняются в `local-data/d23/` (в Git не попадают):
`calibration.json` (`d23-calibration-v1`), `comparison.json` (`d23-comparison-v1`),
`trace-sample.json` (`d23-trace-v1`) и `quality-assessment.md`.
Калибровочный набор — `eval/d23/calibration-questions.json`; порог фиксируется до итогового
сравнения и совпадает в `calibration.json` и `comparison.json`.

### D23: итог реализации

- UNIT: **389 passed** (`test.bat unit`); INT: **21 passed** (`test.bat integration`);
  `smoke_test.bat` — PASS; фактический запуск `run_app.bat` — backend ready, UI на
  `http://127.0.0.1:8770/`.
- LIVE (`MODEL_CHECK_KIND: NETWORK`, `deepseek/deepseek-flash`, reasoning low; embeddings —
  прежний `embeddinggemma:300m`): 10 вопросов D22 × 4 режима = **40 ответов** на одном
  закреплённом индексе `structure`, пустая история, одинаковые настройки ответа.
  `retrieval_passed_rate=0.95`, `unsupported_citation_rate=0.0`, `truncated_answers=0`,
  `insufficient_sources_answers=2` (в т. ч. вопрос вне корпуса D22-Q10).
- Пример раздельных метрик (Q01, режим C): rewrite usage `81/61/142`, rewrite latency
  `1009 ms`; generation usage `1823/349/2172`, `latency_ms.chat=2054 ms`,
  `latency_ms.total=3101 ms` (включает rewrite). `output_tokens_per_second` — **н/д**
  (remote-провайдер не отдаёт timings).
- Выбранный порог после калибровки — **0.45**. Порог `0.30` делал фильтр инертным; правило
  калибровки изменено на «наибольший порог, сохраняющий ≥1 релевантный чанк на каждый
  калибровочный вопрос». Лимит output rewrite поднят `64 → 1024` (при 64 reasoning-профиль
  всегда отдавал пустой ответ); после исправления rewrite успешен 20/20.
- Раздельная оценка (retrieval / полнота фактов / подтверждение источников) с вниманием к
  Q06/Q07/Q08 и вопросу вне корпуса — в `quality-assessment.md`.

### D23: границы и ограничения

- **D21–D22 не изменены по контракту**: `/api/search`, `/api/index/*`, `/api/compare`,
  `/api/chat`, `/api/chat/compare`, `usage` и `latency_ms.chat` генерации прежние.
- Независимое ревью D23 выполнено в реальном браузере с выбранной моделью: проверены
  четыре режима, порог, оба top-K, rewrite, источники, трасса и пустой фильтр без генерации.
  Исправлено переполнение карточек сравнения; проверены ширины 720/1024/1280/1920 px.
- Сохранённый LIVE-набор содержит 40 непустых результатов, без обрезанных генераций;
  rewrite использован 20/20 раз. `retrieval_passed_rate=0.95` означает долю непустых
  контекстов, а не точность ответов. Валидный citation ID не доказывает смысловую поддержку.
- Общего прироста полноты фактов относительно baseline на этом наборе не выявлено:
  фильтр отклоняет вопрос вне корпуса, но иногда теряет полезные сведения (например Q07).
  Проверка смысловой поддержки источниками выборочная, не полный аудит всех утверждений.
- `comparison.json` не хранит `section_path` в проекции; оценка разделов основана на
  содержании и счётчиках, что зафиксировано в `quality-assessment.md`.
- Гибридный/семантический reranker и ANN — по-прежнему Later; D23 ограничен cosine-порогом и
  запросным rewrite.

## Поведение внешнего провайдера

В исходном прогоне Ollama 0.34.4 `POST /api/show` не содержит поля `digest`; digest модели
возвращается в `GET /api/tags`. `OllamaEmbedder` учитывает это: digest берётся из
`/api/tags`, при наличии — из `/api/show`. Расхождение закреплено regression-тестом
`tests/unit/test_embedder.py::test_digest_comes_from_tags_when_show_omits_it`.
`dimension` берётся из `model_info.embedding_length` (768).

## Тесты и уровни проверки

- UNIT (`test.bat unit`): domain/chunkers/tokenizer/store/embedder/service/API на моках.
- INT (`test.bat integration`, `smoke_test.bat`): реальные процессы backend + stub,
  две коллекции, дедупликация, переключение active, страницы чанков, сравнение, UI-assets,
  отказ 422/409. Рестарт (`harness/restart_check.py`) и прерывание сборки
  (`harness/interrupt_check.py`) проверяются через `test.bat acceptance` (уровень INT).
- LIVE (`test.bat live`): embedding проверяется отдельно; chat через выбранный профиль
  либо явный Ollama, `MODEL_CHECK_KIND: LOCAL` или `NETWORK`.
- MANUAL/UI: запуск `run_app.bat`, сервировка статического UI и интерактивная проверка.

Тестовые entrypoints явно задают `KNOWLEDGE_SKIP_ENV_FILE=1`. UNIT работает без сети;
INT/smoke используют loopback stub и собственные TEMP данные. LIVE вызывает embedding Ollama
и выбранный chat provider только opt-in и закрывает SQLite до очистки TEMP. Обычный запуск приложения
по-прежнему может загружать `.env`. Acceptance включает включённый LIVE в automated
результат, но выдаёт `D21_ACCEPTANCE_STATUS: NOT_ASSESSED`: автоматические тесты
не подтверждают ручной UI или наличие обязательного видео. Read-only аудит рабочих
локальных артефактов включается только `RUN_KNOWLEDGE_READONLY_AUDIT=1`.
Игнорируемые артефакты (`.venv/`, `local-data/`, БД, кэши) в Git не попадают.

## Сценарий видео (кратко)

1. Запуск `run_app.bat`; при недоступном Ollama — `Embedding: unreachable`; при отсутствующей модели — отдельный badge и подсказка
   `ollama pull embeddinggemma:300m`.
2. С установленной моделью — сборка с прогрессом и счётчиками sources/documents/sections/chunks.
3. Поиск → фрагменты с происхождением; просмотр чанка и metadata.
4. Смена активной коллекции/версии индекса параметром.
5. Таблица сравнения `fixed` vs `structure`.
6. Отказ на несовместимой embedding-конфигурации (`index_incompatible`) и на битом файле.

## Ограничения

- Точный cosine рассчитан на маленький учебный корпус; ANN и масштабируемый поиск — Later.
- Wiki importer, полный chat RAG, reranker, OCR, resume/инкрементальная переиндексация
  вне D21.
- Работа зависит от локального Ollama и наличия модели `embeddinggemma:300m`.
- Часть текста рисунков/таблиц PDF извлекается с искажениями (см. выше).
- Группировка строк и определение колонок остаются эвристиками: если промежуточная
  вставка не совпадает с соседями ни по базовой линии, ни по `top`, возможна прежняя
  ошибка переноса; в текущем корпусе такого не выявлено.
- Fallback `pypdfium2`: упрощённое извлечение без масштабируемого порога разбиения слов.
- `manifest.pipeline.adapter` = `"multi"`, а `pipeline.extraction_version` берётся от
  **первого** документа; при смешанных источниках (PDF + TXT/MD) это лишь сводка.
  Реальные per-source версии записаны в `manifest.sources[]`, сводка — в
  `pipeline.extraction_versions`; версии входят в `fingerprint`.
- `chunking/common.build_chunk` принимает `section_level`/`section_role`, которые не
  используются внутри (мёртвые параметры, P3); на результат не влияют.
- UI показывает все счётчики, ожидание поиска, ошибки сборки и обновляет health.
  Недоступный Ollama и отсутствующая модель имеют разные статусы с подсказкой.
  Данные источников выводятся как текст; `Active index only` переключает версии,
  `Load more` позволяет просмотреть чанки после первых 50.
- Неизвестный `schema_version` (в т.ч. нечисловое значение) даёт `store_schema_unsupported`,
  а не общий `internal_error`.
- `compare` помечает `comparable: false` и возвращает `note` (UI показывает его), если
  версии построены на разном очищенном корпусе, processing или embedding identity (метрики тогда
  несопоставимы).
- Корпус, индексы и модели в Git не добавляются; коммиты и push агентом не выполняются.

### D22: выбранный тестовый профиль генерации

Проверяющий runner передаёт выбранную chat-модель через `AI_TEST_MODEL_*`.
Полный профиль (`KIND=local|remote`, `BASE_URL`, `NAME`, `API_KEY`) имеет приоритет
над standalone `CHAT_*`; частичный профиль вызывает явный отказ без подмены модели.
Генерация использует OpenAI-compatible API, embeddings остаются прежними через Ollama.
Без профиля сохраняется явная конфигурация chat через Ollama; модель не выбирается
и не скачивается автоматически. Ключи, адреса профиля и lifecycle ID не печатаются.

Для local LIVE/acceptance требуется защищённый lifecycle URL из окружения runner.
Внешний runtime выдаёт аренду модели; родитель передаёт готовую сессию детям,
обновляет TTL и освобождает её в `finally` после успеха, ошибки или прерывания.
Завершение чужих процессов запрещено: runtime решает, можно ли выгрузить модель
с учётом других пользователей. Приложение само не владеет standalone моделью.
Автотесты используют моки; реальный remote прогон обозначается `NETWORK`, local — `LOCAL`.
Отсутствующие provider usage/скорость остаются н/д; это не нули и не оценочные токены.

У выбранного local runtime с явно объявленными reasoning capabilities учебный chat-запрос отключает thinking для ограниченного output budget; это per-request настройка, сохранённая в run, без изменения глобальной модели. У remote/неизвестного runtime дополнительные поля не отправляются. Пустой (в том числе whitespace-only) ответ провайдера — ошибка `chat_invalid_response` при любом `finish_reason`; непустой ответ с `finish_reason=length` сохраняется как `truncated=true`. Пустой ответ никогда не засчитывается как успешный содержательный ответ.

### D22: текущий итог по фактическим результатам (актуально)

- UNIT: **347 passed** (`test.bat unit`); INT: **14 passed** (`test.bat integration`);
  `smoke_test.bat` — PASS; регрессия D21 — PASS (API, индексы и происхождение не изменены).
- LIVE 10 пар (`test.bat rag-eval`) на выбранном remote-профиле (`deepseek-flash`,
  `MODEL_CHECK_KIND: NETWORK`): `runner_status: COMPLETED`, 10 пар / 20 непустых ответов,
  `section_hit_rate=0.8889`, `page_hit_rate=0.8889`, `facts_present_rate=0.5676`,
  `unsupported_citation_rate=0.0`; неотвечаемый D22-Q10 корректно отклонён.
  `output_tokens_per_second` — н/д (провайдер не отдаёт timings).
- Бюджет генерации: при дефолтном `CHAT_MAX_OUTPUT_TOKENS=1024` reasoning-модель исчерпывает
  лимит и отдаёт пустой ответ — прогон корректно завершается `FAIL` (`pairs_completed=0`), без
  ложного `PASS`. Успешный прогон выполнен с явно повышенным `CHAT_MAX_OUTPUT_TOKENS=4096`;
  автоповышения бюджета нет.
- Ручные оценки `chat-eval-v1` по обеим веткам каждой из 10 пар сохранены отдельно от
  неизменяемых записей: 20 файлов `evaluations/<run_id>.json` в каталоге прогона
  `rag-eval-f6df68b8…` (материализуются `test.bat scenario d22-save-eval`). Для `with_rag`
  итог `pass` у Q03/Q04/Q05/Q10 и `partial` у Q01/Q02/Q06/Q07/Q08/Q09 (у Q06 ожидаемый
  раздел «2.2 Agent Capability Acquisition» не попал в `passed`); ветка `without_rag` —
  baseline (`retrieval`/`sources` не применимы).
- Повторный содержательный аудит всех 20 ответов исправил ручные оценки: RAG Q02 содержит
  2/4 ожидаемых фактов, Q07 — 1/4. Baseline оценивается по реально присутствующим фактам,
  а не автоматически получает ноль из-за отсутствия retrieval. Покрытие Q01–Q09:
  RAG **27/37 (0.7297)**, baseline **30/37 (0.8108)**. Это полнота, не доказательство
  опоры на источник. Для baseline grounded=false; retrieval/sources — N/A.
  RAG: 4 pass / 6 partial; baseline content-only: 7 pass / 1 partial / 2 fail.
  Sources score проверяет допустимые ID/provenance, а не смысл каждого утверждения.
  Автоматическая facts_present_rate выше — старая substring-метрика сохранённого
  неизменяемого summary; она не равна исправленной ручной оценке.
  Повторный offline-сценарий 20/20 байт-идемпотентен; исходные LIVE-записи не менялись.
- Независимая проверка UI 2026-10-01: настоящее приложение на копии SQLite, свежие
  Fixed 50 / Structure 61; три действия чата, источники passed, токены/задержки,
  ожидание, пустой вопрос и context_overflow проверены в браузере. JavaScript ошибок нет.
  DeepSeek Flash реально ответила в обоих режимах и сравнении; неизвестный вопрос
  корректно отклонён. Дополнительно LIVE: without_rag без коллекции; вредоносная
  инструкция в отдельном тестовом источнике не выполнена, ответ содержит исходный факт.
  Нет индекса — HTTP 409 index_not_ready; отсутствующий прогон — HTTP 404.
- Основная функция задания и усиление с 10 вопросами воспроизведены. Ограничения
  качества retrieval сохраняются и отражены в ручных оценках; технический PASS не
  означает идеальную полноту ответов. Редкие варианты частичного/отсутствующего usage
  проверены regression-тестами, но не все естественно наблюдались на текущем NETWORK
  провайдере. Универсальная полная проверка всех вариантов внешней границы не заявлена.

### D22: история — первая фактическая проверка выбранного профиля (2026-10-01, до исправлений)

- UNIT: **240 passed**, INT: **14 passed**; smoke с изолированной БД — PASS.
- Реальная локальная chat-модель семейства Gemma, 12B, проверена через выбранный
  профиль; `embeddinggemma:300m` использована отдельно для embeddings.
- Сохранены **20 HTTP-прогонов: 10 вопросов × 2 режима** на одном корпусе с одинаковой
  chat identity и настройками. Это подтверждает выполнение сравнения, не качество
  всех ответов. Три ответа завершились `finish_reason=length`: Q05 в обоих режимах
  и Q08 с RAG; они честно отмечены как обрезанные.
- UI: потоковый ответ, два валидных источника `chunk_id` и сравнение двух режимов —
  PASS. Выгрузка собственной тестовой модели после завершения подтверждена;
  отдельно проверены ошибочное завершение и сохранение модели при другом пользователе.
- Эталон разделов и страниц eval исправлен по самому PDF. Страницы — физические,
  начиная с 1. Старые автоматические section/page hit rates были рассчитаны по
  неверному эталону; их нельзя использовать как итоговую оценку.

Ограничения качества фактического прогона: Q06 — пропуск нужного раздела retrieval;
Q04 — числовая ссылка статьи `[61]` не является валидированной ссылкой `chunk_id`;
в Q03 есть небольшая смысловая неточность о ручном конструировании памяти.
Эталон не подгонялся под ответы, пропуск Q06 не считается успешным поиском.
Полная содержательная приёмка D22 не объявлена завершённой. Видео D22: **NOT_RUN**.

Регрессия пустого ответа (исправление ложного PASS): в сохранённых LIVE-прогонах 9 из 20
ответов были пустыми (`finish_reason=length`, `output_tokens=1024`) — reasoning выбранной
remote-модели исчерпывал output-бюджет, а `harness/rag_eval.py` и runner засчитывали такие
пары выполненными (`RAG_EVAL_PAIRS_STATUS: PASS`). Теперь пустой ответ — `chat_invalid_response`:
`harness/rag_eval.py` останавливается с `FAIL` при первом пустом или ошибочном ответе,
`harness/rag_eval_live.py` не выставляет `COMPLETED` при пустых ответах. При исчерпании
бюджета оператор выбирает модель без reasoning либо явно повышает `CHAT_MAX_OUTPUT_TOKENS`
и перезапускает `test.bat rag-eval`; автоматических повторов нет.

### D22: разрешение LIVE по выбранной тестовой модели

Выбранная тестовая модель разрешает необходимые ограниченные LIVE-проверки задания: local и remote, включая платный API выбранного провайдера, без повторного вопроса. Пункт «Запрещено» передаёт `AI_TEST_LIVE_POLICY=forbidden`: реальные вызовы блокируются до HTTP, аренды и inference, в том числе при старом профиле или включённых RUN-флагах. Агенты отражают решение, модель и выполненные/пропущенные проверки в отчёте.

`test.bat live` при запрете возвращает код 3. Обычные unit/integration и smoke остаются изолированными проверками с заглушками. Acceptance выполняет offline-часть; запрещённый запрошенный LIVE получает BLOCKED и код 3. Выбор модели разрешает проверку, но не запускает её автоматически. Вне панели отсутствие флага сохраняет прежний standalone-сценарий.

### Разрешённый запуск полной проверки D22

`test.bat rag-eval` выполняет 10 вопросов в двух режимах на выбранном в AI-CENTER тестовом профиле. Команда создаёт собственный временный индекс корпуса из `eval/d22/questions.json`, запускает отдельный backend на свободном loopback-порту и завершает только свои ресурсы. Уже открытое приложение на 8770 и пользовательская БД не используются.

Выбор модели разрешает обязательный LIVE; «Запрещено» останавливает реальный сценарий с кодом 3. Модель не скачивается и не заменяется другой. Результаты сохраняются в отдельной папке `local-data/chat-runs/rag-eval-…`: 20 записей ответов, сводка 10 пар и receipt с происхождением модели/индекса и результатом очистки. Успешное выполнение 10 пар не заменяет содержательную оценку ответов по ACCEPTANCE: качество отдельно отмечено `NOT_ASSESSED` до такой оценки.

Для новых проверок предусмотрена команда `test.bat scenario <slug>`. Она принимает только имя зарегистрированного сценария из `tests/scenarios`: Python-файл и одноимённый JSON с `schema_version: test-scenario-v1` и `kind: live` либо `offline`. Произвольные пути, команды и дополнительные аргументы не поддерживаются. LIVE требует разрешённого полного профиля; offline не получает модельные ключи и профиль. Новые сценарии — обычные тестовые файлы Developer, которые проходят Architect и независимого Tester; менять защищённый bat для каждой проверки не требуется.

## Day 24 — цитаты, источники и защита от галлюцинаций

Задача D24 (`docs/specs/day-24-rag-grounding/SPEC.md`) добавляет к RAG-ответу
**grounding**: рядом с ответом показываются использованные источники (`source`,
`section`, `chunk_id`) и **точные цитаты** из реально переданных модели фрагментов,
подтверждающие существенные утверждения. Grounding включается `RAG_GROUNDING_ENABLED`
(по умолчанию `1`); `0` возвращает точное поведение D22 `rag-v1`. Режим без RAG,
rewrite/фильтр/настройки и четыре режима D23 сохранены.

Ключевые правила:

- **Источники — ровно `retrieval.passed`**: только фрагменты, реально переданные модели
  после поиска, фильтрации и лимита контекста; кандидаты в источники не попадают.
- **Три раздельные проверки**: `source_exists` (ссылка на переданный `chunk_id`),
  `quote_verbatim` (цитата дословно в своём фрагменте) и осмысловая поддержка.
  Смысловая проверка моделью D24 **не выполняется**: `meaning_supported = null`,
  `meaning_check = "not_performed"`; совпадение текста не выдаётся за доказательство смысла.
- **Нормализация пробелов** описана явно: NFC, схлопывание Unicode-пробельных
  последовательностей в один пробел, trim; сравнение регистрозависимое. Пересказ и
  приблизительное совпадение точной цитатой не считаются; выдуманная цитата не
  подменяется похожим текстом, а ответ не показывается успешно проверенным.
- **Перевод** цитаты помечается как перевод с сохранением оригинала.
- **Найденный текст — данные, не команды**: инструкции внутри документов не исполняются.
- **Честный отказ** при `below_threshold` / `model_insufficient`: причина и порог
  сохраняются, источники и цитаты остаются пустыми без выдумывания, пользователь
  получает полезное уточнение. Пустой ответ, обрезанная генерация, ошибка формата и
  недоступность провайдера — технические ошибки (`chat_invalid_response` и др.),
  а не успешный отказ «не знаю».

Поток: `вопрос → retrieval (D21) → фильтр/rewrite (D23) → ContextBudget →
grounded-шаблон (grounded-rag-v1) → ChatModel → строгий разбор JSON → GroundingVerifier →
answer.grounding`. Матрица endpoint-ов: `/api/chat` и `/api/chat/stream` — grounded по
`ChatRequest.grounding`/дефолту; `/api/chat/compare` не затрагивается (всегда `rag-v1`/
`plain-v1`, без `answer.grounding`); `/api/chat/compare-modes` — по `grounding_enabled`,
`comparison.prompt_templates.generation` отражает фактический шаблон.

Новые компоненты: `chat/citations.py` (`normalize_whitespace`, `parse_grounded_response`,
`GroundingVerifier`), `chat/prompts.py` (`grounded-rag-v1`), `domain/contracts.py`
(`Citation`, `GroundingResult`, аддитивный `chunk_id` в `IndexStore.list_chunks`),
`config.py`/`__main__.py` (`RAG_GROUNDING_ENABLED` → `ChatService(grounding_enabled=...)`),
`api/routes.py` (`GET /api/index-versions/{id}/chunks?chunk_id=`), UI-блок
`Sources & citations` с раскрытием фрагмента, раздельными состояниями ошибки и
недостатка информации, переносом длинных строк и английскими подписями новых элементов.

### Команды D24

| Команда | Назначение |
| --- | --- |
| `test.bat unit` | unit-тесты D21 + D22 + D23 + D24 (без сети и `.env`) |
| `test.bat integration` | INT через `embed_stub` + grounded `chat_stub` (loopback, TEMP) |
| `test.bat scenario d24-rag-grounding` | LIVE: 10 вопросов + 8 граничных кейсов на одном индексе |
| `smoke_test.bat` | стабы + backend: D21/D22/D23-сценарий и D24-UI |
| `run_app.bat` | запуск приложения и панели ответа с источниками и цитатами |

Артефакты прогона — `local-data/d24/` (в Git не попадает):
`grounding-results.json` (`d24-grounding-v1`, 10 результатов на одном `index_version_id`)
и `edge-cases.json` (`d24-edge-cases-v1`, 8 кейсов). Независимая содержательная оценка
10 результатов сохраняется Tester-ом в `quality-assessment.md`.

### D24: итог реализации

- UNIT: **443 passed** (`test.bat unit`); INT: **28 passed** (`test.bat integration`);
  `smoke_test.bat` — `SMOKE_STATUS: PASS`; регрессия D21–D23 — PASS.
- LIVE (`MODEL_CHECK_KIND: NETWORK`, выбранный remote-профиль; embeddings — прежний
  `embeddinggemma:300m`): `test.bat scenario d24-rag-grounding` —
  `D24_RESULTS: 10 (answered 10)`, `D24_EDGE_CASES: 8 (non-pass 0)`,
  `SCENARIO_STATUS: PASS`, один `index_version_id`. Итог 10 результатов:
  2 verified / 6 partial / 2 refused. `quality-assessment.md` формирует независимый Tester.
- Фактический запуск `run_app.bat stub`: backend поднялся, health OK, UI открыт и
  отдаёт D24-элементы; свои процессы остановлены. Полноценный интерактивный браузерный
  рендеринг агенту недоступен — `D24_UI_STATUS: PARTIAL`.

### D24: ограничения

- Смысловая поддержка ответа цитатами **не проверяется моделью** в D24: показывается
  только формальная валидность источника и дословность цитаты; `meaning_supported`
  остаётся `null`. Совпадение текста не доказывает смысл.
- Выбранный remote-провайдер иногда обрамляет grounded-JSON прозой; разбор принимает
  ровно один top-level JSON-объект, не «дочиняет» JSON и отвергает два объекта.
  Для D24-раннера поднят output-бюджет (`num_predict=16384`, `num_ctx=65536`) только
  для собственного прогона; это не меняет глобальные настройки приложения.
- `D24_UI_STATUS: PARTIAL`: проверены фактический запуск и раздача HTML/JS и статическая
  разметка; интерактивная проверка ширин и раскрытия фрагмента в браузере не выполнялась.
- Качество 10 ответов (1 отказ `model_insufficient` на документном вопросе, 6 частичных
  с `limitation`) — фактическое содержание прогона; независимая оценка и решение об
  улучшениях выполняются Tester-ом, не подменяются структурной валидацией.
- Гибридный/семантический reranker и ANN — Later; D24 ограничен формальной проверкой
  источников и цитат.

### D24: финальная проверка после исправлений

Предыдущие результаты выше сохранены как история первого прогона.

- Свежая независимая проверка: **463 unit passed**; ранее **28 integration passed** на неизменённых HTTP/процессных границах.
- Исправлены расчёт ContextBudget по фактическому GROUNDED_RAG, проверка inline-ссылок, недопустимый PASS без цитат при limitation и смешение технического обрыва с отказом по недостатку источников. Добавлены постоянные регрессионные проверки.
- Усилен grounded-промпт: существенные утверждения подтверждаются соответствующими дословными цитатами; свойства одного подхода не распространяются на другой без доказательства.
- Приложение проверено интерактивно в браузере: реальный русский вопрос, источники и цитата, раскрытие исходного чанка; режим B с порогом 1.0 передаёт 0 из 20 кандидатов и отказывает без генерации. Проверка выполнена в отдельной временной базе, пользовательские данные не изменялись.
- Новый LIVE-прогон десяти вопросов на свежем Structure-индексе: 46 цитат, 45 точных, одно несовпадение честно отклонено. Статусы: 2 verified, 6 partial, 2 refused, 0 обрезаний. Происхождение всех источников корректно; смысл и полнота оценены независимо вручную.
- Ограничения сохраняются: одна неточная цитата Q03; неполнота Q05–Q07 из-за выбранного контекста; отказ Q09 обоснован относительно переданных фрагментов, но не доказывает отсутствия ответа во всём документе. Q10 корректно отказывает на отсутствующую метрику. Автоматической проверки смыслового следования нет; безусловный PASS качества всех ответов не заявляется.

## Day 25 — мини-чат с RAG и памятью задачи

### Актуальное состояние после проверки (2026-10-03)

Реализация и технические проверки завершены; полная содержательная приёмка остаётся PARTIAL. Оценка качества: **PARTIAL**. Ни успешная команда, ни существующая
цитата не подменяют эту оценку. Предыдущие промежуточные результаты сохранены в локальной
истории; приведённые здесь сведения относятся к последней проверке.

**Проверено:** UNIT **643 passed**, INT **34 passed**, `SMOKE_STATUS: PASS`. Регрессия
охватывает D21–D24, HTTP API, сохранение и перезапуск, изоляцию диалогов, конкуренцию,
повторную отправку, ошибки провайдера и формальные проверки источников. Интерфейс проверен
интерактивно в браузере на отдельной временной базе со стабами: создание/переименование/
подтверждение удаления, история и память после обновления, раскрытие источника и основания
памяти, смена диалога, панель RAG, растущий ввод; ширины 720/1024/1280/1920 без горизонтального
переполнения. Стабы подтверждают UI и обмен данными, а качество реальной модели проверено
отдельно по LIVE-артефактам.

Тестовые backend изолируют индекс, историю/память и сохранённые ответы до открытия БД.
Унаследованные рабочие пути заменяются TEMP-путями; явно заданный выход за тестовый каталог
отклоняется до запуска. Smoke удаляет только свой проверенный временный каталог.

**LIVE:** актуально выбранный пользователем DeepSeek Flash, effort Low, NETWORK,
policy allowed, grounding включён. Low передаётся доверенным прокси Центра; он не выключает
мышление и не увеличивает лимит приложения. D25 использует отдельные значения
`D25_CHAT_CONTEXT_TOKENS=32768`, `D25_CHAT_MAX_OUTPUT_TOKENS=8192` для выбранного remote.
Для выбранного local автоматическая цель — 16384/3072 с учётом реального окна сервера;
без выбранного профиля сохраняются существующие CHAT-настройки. Автоматический reserve
оставляет место входу, явно заданные несовместимые лимиты дают понятную ошибку.
Лимиты одиночного API D22–D24 остаются прежними; необязательный UI-лимит задаётся явно.

- A: `d25-a-20261003T152356Z-87989ca5`, 12 ходов; техническое завершение — PASS, автоматические проверки — `FAIL`; смысл оценивается отдельно по полным ответам.
- B: `d25-b-20261003T152523Z-92ca62d3`, 12 ходов; техническое завершение — PASS, автоматические проверки — `PASS`; смысл оценивается отдельно по полным ответам.

Полные ответы, память, поиск, источники и диагностика находятся в
`local-data/d25/scenario-{a,b}.json`; независимая оценка с отпечатками —
`local-data/d25/quality-review-codex-final-20261003T154535Z.json`. Смысловые поля сырого прогона остаются `NOT_ASSESSED`: отдельный
проверенный протокол не переписывает историю выдачи модели. Рабочая пользовательская БД
в LIVE/INT/UI-проверках не использовалась. Фикстуры содержат публичный обзор агентов и
искусственные вопросы; диагностические файлы и корпус не публикуются в Git.

**Ограничения:** A09 безопасно отклонил неподтверждённый документами ответ, но завершился citation_failed; A11 слишком широко связывает обучение без fine-tuning с памятью. Некоторые верные по PDF детали покрыты возвращёнными цитатами не полностью. Сценарий B подтверждает архитектурный план, а не готовое локальное production-развёртывание. Цели и актуальные условия удержаны во всех 24 шагах; пустых ответов, транспортных ошибок и обрывов нет.

### Как пользоваться и что происходит

1. Запустить `run_app.bat` с явно настроенной chat-моделью. Для проверок агентами Центр
   передаёт выбранный `AI_TEST_MODEL_*` профиль автоматически. Без модели приложение
   показывает понятную подсказку; оно не выбирает провайдера и не скачивает модель молча.
2. В правой панели создать/выбрать коллекцию, добавить явные PDF/TXT/MD пути и нажать Build.
   Документы проходят extraction → chunking → embeddings → SQLite-индекс D21. Embeddings
   остаются независимыми от модели, которая формулирует ответ.
3. Слева создать диалог; внизу отправить сообщение (`Enter`, перенос — `Shift+Enter`).
   История и память принадлежат этому диалогу. Поле ввода растёт до предела и прокручивается.
4. Приложение сохраняет явно заданную цель, условия и уточнения с основанием в пользовательском
   сообщении; затем на каждом новом ходе делает свежий retrieval. Отсылка «как это связано?»
   разрешается с помощью истории/цели; неоднозначная отсылка вызывает уточнение.
5. Контекст включает инструкции, подтверждённую память, подходящую историю и найденные
   фрагменты в пределах бюджета. Цель и активные условия не отбрасываются молча.
6. Модель формирует ответ с выбранными ссылками на реальные фрагменты. Короткие evidence-id
   декодируются только в явно выбранную дословную цитату текущего контекста; сервер заново
   проверяет настоящий chunk-id, точный текст и inline-ссылки. Нет нечёткого исправления цитат
   или приписывания источников автоматически. Для ошибочного формата разрешена одна попытка
   исправления; ошибки, пустые/усечённые ответы не считаются успешными.
7. Ответ, источники, метрики и новая память фиксируются вместе с ходом. Если сеть потеряет
   ответ, повтор того же сообщения использует прежний client-turn-id и не создаёт дубль.
8. Память можно поправить/отменить в UI. Изменение одного условия сохраняет остальные;
   основание открывает полный исходный пользовательский текст даже за пределами текущей
   страницы истории. Старые сообщения подгружаются кнопкой Load older messages.

**История, память задачи и документы различаются.** История хранит сообщения; task-state
хранит подтверждённые цель/условия/уточнения; RAG-индекс хранит документы. Ответ модели,
догадка или фраза из PDF не становятся подтверждённым пользовательским условием. Для
явной команды запомнить условие/перечислить условия применяется детерминированная сводка
с происхождением `confirmed_task_memory`; документальные факты из такой памяти не сочиняются.

**Формальная цитата не доказывает смысл.** UI показывает точность/происхождение источника
отдельно от смысловой поддержки, которая без отдельной проверки остаётся not assessed.
Предложения перенести идеи обзора агентов в RAG-план помечаются как интерпретация.
Planning — возможное агентное расширение, а не обязательный компонент всякого RAG.

### Хранение, интерфейс и предыдущие дни

Диалоги и task-state — отдельная SQLite-БД `DIALOGUE_DB_PATH`; индекс —
`KNOWLEDGE_DB_PATH`. Запись хода и версии памяти атомарная, конфликт версии памяти
возвращает 409. Перезапуск сохраняет данные; A/B и параллельные диалоги изолированы.
Сохранённые одиночные `chat-run-v1` не переносятся в диалоги и не перезаписываются.

Справа остаются коллекции/индексация, два chunker-а, поиск и сравнение, RAG без rewrite/с
rewrite, порог и top-K до/после фильтра, трасса retrieval и цитаты D21–D24. Одиночное
сравнение с RAG/без RAG и сравнение четырёх режимов вынесены в раскрывающиеся панели.
Правую панель можно скрыть, расширив чат. Обычный D25 top-K по умолчанию 10; явно выбранные
параметры фильтра сохраняются. Desktop-панели прокручиваются внутри рабочей области.

### Команды и проверяемые результаты

| Команда | Назначение |
| --- | --- |
| `test.bat unit` | Offline UNIT D21–D25 |
| `test.bat integration` | TEMP/loopback INT, включая рестарт и миграцию |
| `test.bat scenario d25-scenario-a` | Разрешённый LIVE: 12 ходов обучения теме |
| `test.bat scenario d25-scenario-b` | Разрешённый LIVE: 12 ходов планирования |
| `run_app.bat` | Приложение с явно настроенной chat-моделью |

Ожидания фиксируются заранее в `eval/d25/scenario-{a,b}-expectations.json`. Новые реальные
прогоны сохраняют историю старых файлов, контрольные суммы и versioned диагностику без
ключей и текста рассуждений. Сценарий не получает PASS по числу непустых полей. Повторный
платный прогон нужен при изменении проверяемого поведения, а не только ради иной роли.
