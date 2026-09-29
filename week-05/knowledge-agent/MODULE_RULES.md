# Правила модуля `knowledge-agent`

Универсальные правила живут в курсовом корне, а не дублируются здесь. Этот файл содержит только дельту к правилам курсового корня.

## 1. Цель модуля

Модуль решает учебную задачу D21 (`day-21-document-indexing`): проиндексировать **явно указанный через конфиг** корпус документов (PDF с текстовым слоем и маленький TXT/MD как второй источник) и выполнять по нему локальный семантический поиск с просмотром найденных фрагментов и их происхождения.

Наблюдаемый результат: пользователь задаёт путь к источнику через конфиг (приложение не сканирует диск и не угадывает путь); модуль извлекает текст, разбивает его на секции и чанки, считает локальные embeddings и сохраняет их в локальный SQLite-индекс; поиск возвращает **фрагменты** с metadata, а не сгенерированный ответ.

## 2. Границы и не-цели

Входит в D21 (Must):

- два адаптера источников: текстовый PDF и маленький TXT/MD;
- два chunker-а: `FixedChunker` и `StructureChunker`;
- локальный embedder поверх Ollama `POST /api/embed`, модель `embeddinggemma:300m` (отдельная embedding-модель, не чат-модель);
- локальный SQLite-индекс: текст, metadata, бинарные float32-векторы;
- `KnowledgeService`, HTTP API с OpenAPI 3.1 и лёгкий static UI;
- manifest индекса и полный набор metadata чанка;
- `harness/embed_stub.py` — детерминированный stub-embedder (не inference);
- метрики и сравнение стратегий чанкинга;
- реальный запуск и перезапуск приложения (Developer, затем независимо Tester).

Осознанно не входит (Later): Wiki importer (формат ZIM/XML не установлен; XML BZ2 не предполагается), внешний клиент/MCP adapter, ANN backend, полный chat RAG, reranker, OCR, resume/инкрементальная переиндексация, удаление старых версий индекса, multi-process/Postgres/Docker. D21 не уходит в автоматический RAG и полный чат: генерация ответа моделью не обязательна.

## 3. Стек и версии

- Python + FastAPI + Pydantic + SQLite (профиль `Backend AI`).
- Тесты — Pytest; PDF — `pdfplumber` (основной) с fallback `pypdfium2`.
- UI — статический HTML/JS без шага сборки и без CDN.
- Точные версии фиксируются в `requirements.txt` на этапе реализации (SPEC §16); до реализации кода точные версии не заявляются.

## 4. Структура модуля

PLAN §1 (ответственности; имена модулей уточняются на реализации, границы обязательны):

- `knowledge_agent/__main__.py` — точка входа приложения (`python -m knowledge_agent`).
- `knowledge_agent/config.py` — `load_settings(env: Mapping | None)`.
- `knowledge_agent/domain/` — модели, ошибки и контракты ядра (`SourceAdapter`, `Tokenizer`, `Chunker`, `Embedder`, `IndexStore`, `check_index_compatibility`).
- `knowledge_agent/text/` — tokenizer v1 (offline, regex) и нормализация.
- `knowledge_agent/sources/` — `TextSourceAdapter`, `PdfSourceAdapter`, разбор PDF-раскладки.
- `knowledge_agent/chunking/` — `FixedChunker`, `StructureChunker`.
- `knowledge_agent/embedding/` — `OllamaEmbedder` (preflight, префиксы, батчи).
- `knowledge_agent/storage/` — `SqliteIndexStore`, схема и `schema_version`.
- `knowledge_agent/service/` — `KnowledgeService`.
- `knowledge_agent/api/`, `knowledge_agent/ui/` — FastAPI (SPEC §11) и статический UI (SPEC §12).
- `harness/` — `embed_stub.py`, `smoke.py`, `live_embed.py`, `acceptance.py`, `restart_check.py`, `interrupt_check.py`.
- `tests/` — `unit/`, `integration/`, `fixtures/`.
- `docs/specs/day-21-document-indexing/` — `SPEC.md`, `PLAN.md`, `ACCEPTANCE.md`.

## 5. Общие правила модуля

- Существующий модуль развивается на месте.
- Временные и архивные копии модуля запрещены.
- Завершённый и опубликованный этап модуля не изменяется, кроме точечного исправления фактической ошибки; отчёты прошлых этапов не переписываются.

## Классификация задач

- Модуль не имеет собственных ролей, `AGENTS.md` и `PROJECT_RULES.md`: они наследуются из курсового корня.
- Классификация `PRODUCT` / `GOVERNANCE` / `MIXED` / `DIAGNOSTICS` и маршрутизация определяются правилами курсового корня (`TASK_CLASS`); модуль эти правила не переопределяет.
- `MIXED`-задача останавливается Coordinator до делегирования со статусом `TASK_STATUS: STOPPED_FOR_SPLIT`.
- `DIAGNOSTICS` — полностью read-only задача с маршрутом Coordinator → Tester: файлы модуля не создаются, не изменяются, не удаляются и не переименовываются.
- Каждый новый запрос пользователя классифицируется заново.

## 6. Документация модуля

- В модуле один постоянный `README.md`.
- Главный заголовок `README.md` — постоянное название модуля; он не меняется от этапа к этапу.
- Новый этап добавляется в `README.md` новым разделом; номер этапа указывается в заголовке раздела.
- `README.md` обновляется после каждого этапа; отчёт ведётся накопительно.
- Отчёты и `README.md` — по-русски, пользовательский UI — по-английски.

## 7. Запуск и интерфейс

- Для запускаемого модуля сохраняется доверенный `run_app.bat`.
- Для человеческого AI-интерфейса показывается индикатор ожидания; при поддержке streaming ответ выводится потоково, иначе — итоговым ответом после завершения запроса.
- Тесты запускаются прямой командой без лишних pipeline; удаление файлов — только отдельной командой с подтверждением.
- Игнорируемые артефакты тест-раннера (кэши, временные базы данных, coverage) вручную не удаляются.

## 8. Команды

Все команды выполняются из каталога модуля. Режимы и коды возврата — по PLAN §10.

| Скрипт | Режимы | Коды возврата |
| --- | --- | --- |
| `setup.bat` | — | `0` — окружение готово; `2` — ошибка создания окружения/установки |
| `test.bat` | `unit` (по умолчанию), `integration`, `live`, `acceptance` | `0` — пройдено; `1` — ошибка тестов; `2` — ошибка окружения/неизвестный режим |
| `smoke_test.bat` | — | `0` — пройдено; `1` — ошибка; `2` — ошибка окружения/занятый порт |
| `run_app.bat` | `all` (по умолчанию), `api`, `ui`, `stub` | `0` — остановлено нормально; `1` — ошибка; `2` — ошибка окружения/занятый порт |

- `test.bat unit` → `pytest tests\unit` (без сети и `.env`); `integration` → `pytest tests\integration` (stub, без сети и `.env`); `live` → `RUN_EMBED_LIVE=1` + `harness\live_embed.py` (opt-in, реальный Ollama); `acceptance` → `harness\acceptance.py`. Печатаются `UNIT_STATUS:`, `INTEGRATION_STATUS:`, `EMBEDDING_LIVE_STATUS:`, `TEST_STATUS:` где применимо.
- `smoke_test.bat` поднимает `harness\embed_stub.py` и backend `python -m knowledge_agent` на `KNOWLEDGE_HOST:KNOWLEDGE_PORT`, дожидается `GET /api/health`, затем запускает `harness\smoke.py`; exit-код runner-а пробрасывается.
- `run_app.bat all` — stub при `EMBED_BASE_URL` на stub, иначе реальный Ollama; backend, ожидание `/api/health`, открытие UI; `api` — backend в foreground; `ui` — только UI; `stub` — stub и backend.
- Любая точка входа останавливает только собственные процессы и не завершает чужие.

## 9. Окружение и секреты

- `.env` приложением не создаётся; заглушки без значений — в `.env.example`.
- `KNOWLEDGE_HOST` (default `127.0.0.1`), `KNOWLEDGE_PORT` (default `8770`, свободный порт, не совпадающий с backend Week 4), `KNOWLEDGE_DB_PATH`, `KNOWLEDGE_SOURCE_PATH` (по умолчанию пусто — путь задаёт пользователь).
- Embedding: `EMBED_BASE_URL`, `EMBED_MODEL=embeddinggemma:300m`, `EMBED_BATCH_SIZE=16`, `EMBED_TIMEOUT_SECONDS=60`, префиксы `document_prefix`/`query_prefix`.
- Chunking: `CHUNK_*`.
- `PDF_USEFUL_PAGE_MIN_CHARS=500`.
- Секреты хранятся только в `.env`; модуль не читает и не печатает значения `.env`.

## 10. Данные и миграции

- Единственное хранилище — локальный SQLite (WAL, `foreign_keys=ON`); без Docker, кластера и отдельной векторной БД.
- Схема версионируется через `schema_version`; миграций в D21 нет.
- Неизвестный `schema_version` → честный отказ `store_schema_unsupported` (500), без молчаливого продолжения.
- Корпус, индексы и модели в Git не попадают.

## 11. API и сеть

- HTTP API (FastAPI, OpenAPI 3.1) отделён от UI; `KnowledgeService` не знает про SQLite и HTTP.
- Методы, пути, параметры и формы ответов — единственный источник истины SPEC §11.
- Сеть ограничена loopback (`127.0.0.1`); внешние вызовы — только локальный Ollama `POST /api/embed` (плюс preflight `GET /api/version`, `GET /api/tags`, `POST /api/show`, legacy `/api/embeddings`).
- Недоступность Ollama не мешает старту: UI показывает `Embedding: unreachable` и подсказку `ollama pull embeddinggemma:300m`.
- Секреты и абсолютные локальные пути наружу не возвращаются (инвариант I7).

## 12. UI/UX

- Панель поиска/чатовая лента: запрос → найденные фрагменты с происхождением.
- Выбор коллекции и стратегии (канонические `fixed`/`structure`).
- Прогресс сборки и счётчики (`sources`/`documents`/`sections`/`chunks`).
- Просмотр чанка и metadata (`section_path`, страницы, hash, language).
- Таблица сравнения стратегий на одинаковом очищенном тексте и одной embedding-конфигурации.
- Явные ошибки (`Embedding: unreachable`, `index_incompatible`, `index_busy` и т. д.) с подсказкой.
- Search возвращает фрагменты; генерации ответов нет. Переключение active index version коллекции — параметром, без правок UI и поисковой логики.

## 13. Дисциплина тестирования

- Тесты не читают настоящий `.env` и не зависят от реального API-ключа; сетевые вызовы изолированы или замоканы.
- Реальные API-вызовы — только с явного разрешения пользователя, ограниченным сценарием.
- Не ослаблять и не удалять проверки ради успешного прогона; изменение ожидаемого поведения должно следовать из задания.
- Проверять наблюдаемое поведение и требования, а не устройство реализации.
- Уровень проверки должен соответствовать Task Contract; unit-тест с моками не подтверждает реальный контракт внешнего провайдера.
- Tester возвращает `TEST_STATUS: PASS` только после проверки всех обязательных критериев на достаточном уровне; иначе `FAIL` или `BLOCKED`.

## 14. Definition of Done

- Критерии ACCEPTANCE D21-01…D21-12 зелёные на требуемых уровнях (UNIT/INT/LIVE/MANUAL/UI).
- `TEST_STATUS` присваивает Tester; задача не принята при `FAIL`/`BLOCKED`.
- Реальный запуск и UI проверяют Developer, затем независимо Tester (D21-11).
- app-run и D21 не считаются готовыми до реализации продуктового кода и фактического прогона точек входа.

## 15. Уровни тестирования

- Каноническая шкала — UNIT / INT / LIVE / MANUAL / UI (ACCEPTANCE §1).
- UNIT: логика ядра на моках/stub, без сети и `.env` (`test.bat unit`).
- INT: реальные процессы и HTTP loopback с `harness/embed_stub.py`, без внешней сети (`test.bat integration`, `smoke_test.bat`); фактический рестарт — метод уровня INT.
- LIVE: реальная локальная модель через Ollama (`test.bat live`/`acceptance`), маркируется `MODEL_CHECK_KIND: LOCAL`.
- MANUAL/UI: ручной прогон и проверка интерфейса в браузере.
- mock/stub **никогда** не выдаётся за inference и не подтверждает LIVE-контракт.

## 16. Регистрация в корневом `opencode.json`

- Отдельные записи для `week-05/knowledge-agent/*.bat` в корневую `opencode.json` не добавлялись и не требуются: четыре точки входа модуля уже покрыты действующими рекурсивными правилами (`**/setup.bat`, `**/test.bat`, `**/smoke_test.bat`, `**/run_app.bat` на `edit`; формы `.\*\setup.bat*` и т. п. на `bash`).
- Правки `opencode.json` при конфигурации модуля не выполнялись.
- Дата фиксации: 2026-09-29; статус — как есть (для этого пункта перезапуск OpenCode не требовался).

## 17. Что нельзя менять

- Governance-файлы курсового корня: `AGENTS.md`, `PROJECT_RULES.md`, `opencode.json`, `.opencode/**`.
- `MODULE_RULES.md`, `MODULE_STATE.md` и `*.bat` модуля изменяет только Configurator через точечные разрешения; Developer их не редактирует.
- Утверждённые после spec gate `docs/specs/day-21-document-indexing/{SPEC,PLAN,ACCEPTANCE}.md`.
- `.env` и секреты.

## 18. Известные ограничения

- Точный cosine допустим только для маленького учебного корпуса и не выдаётся за масштабируемый поиск (ANN — Later; контракт `IndexStore` ANN-ready).
- Wiki importer, внешний клиент/MCP, полный chat RAG, reranker, OCR, resume/инкрементальная переиндексация — вне D21.
- Работа зависит от локального Ollama и наличия модели `embeddinggemma:300m`; доступность и версия не гарантируются.
- Продуктовый код на момент конфигурации не реализован: точки входа ссылаются на будущие модули и фактически не запускались.
