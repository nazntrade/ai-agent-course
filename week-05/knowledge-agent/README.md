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
- LIVE (`test.bat live`): реальная модель, `MODEL_CHECK_KIND: LOCAL`.
- MANUAL/UI: запуск `run_app.bat`, сервировка статического UI и интерактивная проверка.

Тестовые entrypoints явно задают `KNOWLEDGE_SKIP_ENV_FILE=1`. UNIT работает без сети;
INT/smoke используют loopback stub и собственные TEMP данные. LIVE вызывает локальную
Ollama только opt-in и закрывает SQLite до очистки TEMP. Обычный запуск приложения
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
