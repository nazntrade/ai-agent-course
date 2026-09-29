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

- `fixed` — скользящее окно ~500 токенов с overlap 75;
- `structure` — границы реальных разделов, упаковка абзацев, `max_tokens=800`,
  `min_tokens=64`, overlap 0.

### Команды (из каталога модуля)

| Команда | Назначение |
| --- | --- |
| `setup.bat` | создать `.venv` и установить `requirements.txt` |
| `test.bat unit` | unit-тесты (без сети и `.env`) |
| `test.bat integration` | интеграционные тесты через локальный stub |
| `test.bat live` | opt-in LIVE: реальный Ollama `embeddinggemma:300m` |
| `test.bat acceptance` | агрегатор: unit/integration/restart/interrupt и статистика PDF |
| `smoke_test.bat` | stub + backend, полный HTTP-сценарий и проверка UI |
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
3. Дождаться прогресса; готовая версия индекса помечается активной.
4. Ввести запрос в **Search** и посмотреть фрагменты; клик по чанку показывает metadata.
5. **Compare** строит таблицу `fixed` vs `structure` на одном тексте.
6. Переключение активной версии — кнопкой **Set active** (тот же `PUT`, что и в API).

## Корпус D21-01

Основной источник — PDF «A Survey on Large Language Model based Autonomous Agents»
(текстовый слой, 42 страницы). Путь к файлу задаёт пользователь; сам файл в Git не
попадает (каталог `local-data/` игнорируется).

Фактическая проверка (`test.bat acceptance`, извлечение через `pdfplumber`, `pdf-v3`):

- `page_count = 42`, `useful_pages = 42`, `useful_chars ≈ 148 547`, язык `en`;
- порог полезной страницы `PDF_USEFUL_PAGE_MIN_CHARS = 500` преодолевают все страницы;
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
(`test.bat acceptance`, только чтение): `PDF_ORDER` для страниц 3, 4, 10–13, 32,
`PDF_FRAGMENT` — сырые слова/символы и итоговый абзац `1 Introduction`,
`PDF_CHUNKS` — текст новых чанков `fixed`/`structure`. Остаются артефакты исходного
текстового слоя в подписях к рисункам/таблицам и наложенных правках (например,
отдельные full-width строки на стр. 32); порядок чтения они не ломают.
Fallback `pypdfium2` остаётся упрощённым и не применяет масштабируемый порог — это
осознанное ограничение (используется только если `pdfplumber` недоступен).

## Измеренное сравнение `fixed` vs `structure` (LIVE)

Один реальный прогон локальной моделью `embeddinggemma:300m` (Ollama 0.34.4,
dimension 768, digest `85462619…79f1`) на одном PDF, одна embedding-конфигурация,
`excluded_roles = ["references"]`:

| Метрика | `fixed` | `structure` |
| --- | --- | --- |
| chunks | 50 | 61 |
| tokens min/median/p95/max | н/д* | н/д* |
| overlap_overhead | н/д* | н/д* |
| section_crossing_ratio | н/д* | н/д* |
| build_seconds | 6.18 | 6.95 |
| input_tokens (usage провайдера) | 27 217 | 23 144 |
| embed_latency median | 447.4 ms | 396.2 ms |
| chunks_per_second | 8.09 | 8.78 |
| vector_bytes (50×768×4 / 61×768×4) | 153 600 | 187 392 |

`*` Для `pdf-v3` полная разбивка токенов, `overlap_overhead` и
`section_crossing_ratio` offline-диагностикой не экспонируются (значения `н/д`); на
предыдущем `pdf-v2`-прогоне они были `fixed` 174/500/500/500 (overlap 0.175,
crossing 0.340), `structure` 36/347/574/631 (overlap 0.000, crossing 0.000).

Вывод: `fixed` даёт меньше чанков с максимальным перекрытием и пересекает разделы
(`section_crossing_ratio ≈ 0.35`); `structure` не пересекает разделы и не тратит
токены на overlap, но формирует больше мелких чанков на коротких секциях. Значения —
результат одного запуска на учебном корпусе, а не бенчмарк. Output tokens и output
tok/s для embedding не измеряются (`н/д`): у embedding-модели нет генерации текста.
Времена (`build_seconds`, latency) зависят от прогрева модели и приведены по последнему
прогону.

> **Для применения исправленного извлечения нужна пересборка индекса.** Причина
> пересборки `pdf-v3`: устранена склейка слова при переносе с промежуточной строкой.
> Для `1 Introduction` было `...enhance the agent capabildifferent ity to complete
> tasks...`, стало `...enhance the agent capability to complete different tasks...`
> (слово `capability` соединено; `different` сохранён отдельным токеном, не удалён).
> `chunks` 50/60 → 50/61, `useful_chars` 148 549 → 148 547, разделов 26 → 27
> (`6.6 Efficiency` теперь распознаётся). Поиск `planning in LLM agents` возвращает
> тематические фрагменты из `2.1.2 Memory Module` и `2.1.3 Planning Module`.
>
> Версия извлечения PDF поднята до `pdf-v3`; per-source `extraction_version` входит в
> `fingerprint`, но не в сравнение совместимости при поиске. Поэтому сама смена
> `pdf-v2` на `pdf-v3` не гарантирует `409 index_incompatible`: отказ возникает при
> расхождении полей `COMPATIBILITY_FIELDS`, включая `corpus_schema_version` и
> `normalization_version`. Эти два поля входят и в `fingerprint`, и в сравнение
> совместимости; текущая версия корпуса — `corpus-v3`.
> Первый `POST /api/index/build` тех же байтов с новым `fingerprint` создаёт **новую**
> версию индекса — **без** удаления БД (старые `pdf-v1`/`pdf-v2` сохранены). Повтор
> при неизменном `fingerprint` готовой версии возвращает `reused:true` с тем же
> `index_version_id`.
> `CORPUS_SCHEMA_VERSION`/`normalization_version` можно менять и дальше: колонка
> `normalization_version` добавляется к существующей БД на месте (`ALTER TABLE`).

**Версия обработки документа** (детали в manifest `pipeline`): для каждого источника —
`extraction_version` (`pdf-v3`/`text-v1`), плюс нормализация текста
`NORMALIZATION_VERSION` (`norm-v1`) с описанием `pipeline.normalization_detail`.
`manifest.sources[]` версий извлечения не содержит (SPEC §6.4 их и не требует): там
только `label`/`kind`/`content_sha256`/метрики, а сами версии перечислены в
`pipeline.extraction_versions` и в `fingerprint`. Поле `pipeline.normalization` содержит
именно версию нормализации. `normalization_version` — дополнительное поле сверх
буквального списка SPEC §10; оно зафиксировано в `manifest.pipeline` и участвует в
сравнении совместимости (`409 index_incompatible`).

## Поведение внешнего провайдера

На реальном Ollama 0.34.4 `POST /api/show` не содержит поля `digest`; digest модели
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

Тесты не читают `.env` и не ходят в сеть. Реальные вызовы Ollama — только opt-in.
Игнорируемые артефакты (`.venv/`, `local-data/`, БД, кэши) в Git не попадают.

## Сценарий видео (кратко)

1. Запуск `run_app.bat`; при отсутствии модели — `Embedding: unreachable` и подсказка
   `ollama pull embeddinggemma:300m`.
2. После pull — сборка с прогрессом и числом чанков (остальные счётчики доступны в API/manifest).
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
  Реальные per-source версии извлечения перечислены в `pipeline.extraction_versions`
  и входят в `fingerprint`; `manifest.sources[]` их не содержит (SPEC §6.4).
- `chunking/common.build_chunk` принимает `section_level`/`section_role`, которые не
  используются внутри (мёртвые параметры, P3); на результат не влияют.
- UI: значения чанков в таблице версий и сравнение показывают `chunks`; отдельные
  счётчики `sources/documents/sections` и переключатель `Active index only` пока не
  выведены/не подключены (P3). Фрагменты рендерятся через `textContent`, но заголовки
  карточек собираются `innerHTML` из данных собственных документов (P3).
- Неизвестный `schema_version` (в т.ч. нечисловое значение) даёт `store_schema_unsupported`,
  а не общий `internal_error`.
- `compare` помечает `comparable: false` и возвращает `note` (UI показывает его), если
  версии построены на разных источниках или embedding identity (метрики тогда
  несопоставимы).
- Корпус, индексы и модели в Git не добавляются; коммиты и push агентом не выполняются.
