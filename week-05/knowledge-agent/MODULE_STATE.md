# Состояние модуля `knowledge-agent`

Обновлено: 2026-10-01.

## Цель

Текущий этап — приёмка реализованного D22 «Первый RAG запрос»: генерация ответа поверх retrieval D21, режимы с RAG и без RAG, сравнение ответов и 10 контрольных вопросов. Полная приёмка D22 ещё не завершена.

Базовое состояние — реализованный D21 (`day-21-document-indexing`): локальный SQLite-индекс явно указанного корпуса (PDF + TXT/MD) и поиск по фрагментам с происхождением.

## Статус

- Базовый D21 реализован и закоммичен (`b4b3561`); текущее рабочее дерево содержит последующую работу D22. Чистота и окончательный HEAD проверяются отдельно перед публикацией.
- Продуктовый код присутствует: `knowledge_agent/**` (domain, text, sources, chunking, embedding, storage, service, api, ui), `harness/**`, `tests/**`.
- Четыре доверенные точки входа (`setup`, `test`, `smoke_test`, `run_app`) присутствуют.
- D22: SPEC/PLAN/ACCEPTANCE в `docs/specs/day-22-first-rag-query/` и реализация присутствуют. Подключение выбранного `AI_TEST_MODEL_*` chat-профиля реализовано и технически проверено независимым Tester. Полная приёмка D22 остаётся PARTIAL по качеству ответов; видео D22 не записано.
- Configurator фиксирует состояние по отчётам Coordinator/Developer/Architect; приведённые ниже продуктовые прогоны не являются собственными проверками Configurator.

## Сделано

- Реализован D21: поток `SourceAdapter → Document → Chunker → Embedder → IndexStore → KnowledgeService → FastAPI + static UI`.
- Два адаптера источников (PDF `pdfplumber` с fallback `pypdfium2`, TXT/MD), два chunker-а (`FixedChunker` ~500/75, `StructureChunker` ~800/64, overlap 0).
- `OllamaEmbedder` поверх Ollama `POST /api/embed`, модель `embeddinggemma:300m`; локальный SQLite-индекс (текст, metadata, бинарные float32-векторы); manifest и полный набор metadata чанка.
- HTTP API (health, collections, index build/versions/chunks, search, compare) и статический UI без сборки и CDN.
- Harness: `embed_stub.py`, `smoke.py`, `live_embed.py`, `acceptance.py`, `restart_check.py`, `interrupt_check.py`.
- Базовые D21-тесты и фикстуры (`mini_pdf.py`, `sample.txt`, `sample.md`) сохранены; тесты D22 и выбранного профиля дополняют наборы `tests/unit` и `tests/integration`.
- README с измеренным LIVE-сравнением `fixed` vs `structure` (прогон 30.09.2026).
- D22: генерация ответов в обоих режимах, сравнительные прогоны и сохранение результатов, выбранный OpenAI-compatible chat-профиль; embeddings остаются независимыми. Lifecycle harness приобретает собственный lease и освобождает его в finally; заимствованную модель и lease родителя не освобождает.
- SPEC/PLAN/ACCEPTANCE обновлены под выбранный профиль; Architect сообщил PASS.

## Далее

- Разобрать качество ответов и retrieval по сохранённым 20 реальным ответам и корректным source-backed эталонам; замечания перечислены ниже.
- При отдельном запросе пользователя подготовить видео D22 после проверки качества.
- Не объявлять полный D22 ACCEPTED: технический runtime/lifecycle принят, но качество ответов PARTIAL и видео NOT_RUN.

## Открытые вопросы

- D22: выбранная модель передаётся через AI_TEST_MODEL_*; embeddings остаются самостоятельным контрактом, секреты не фиксируются в governance.
- D22: 10 вопросов, 20 реальных ответов и UI проверены. Эталоны в evaluation questions JSON исправлены Developer по действительному источнику; дальнейшая оценка должна использовать исправленные source-backed критерии, а не ошибочные цели.
- Реальный путь к PDF-источнику задаёт пользователь; в конфиге он пуст, корпус в Git не попадает.
- Доступность и версия локального Ollama и модели `embeddinggemma:300m` не гарантируются; README фиксирует состояние на 30.09.2026.

## Известные проблемы

- D22: технические проверки профиля, UI и lifecycle PASS; качество RAG-ответов PARTIAL, видео D22 NOT_RUN.
- Замечания по качеству: Q03 смешивает понятия; Q04 содержит числовые утверждения с неподтверждёнными ссылками; Q06/Q09 — неполный retrieval. Три ответа завершены по length: Q05 в обоих режимах и Q08 с RAG. Это наблюдаемые ограничения конкретного реального прогона, а не успех полной приёмки.
- Точный cosine рассчитан только на маленький учебный корпус; ANN — Later.
- Часть текста рисунков/таблиц PDF извлекается с искажениями; fallback `pypdfium2` упрощён.
- `chunking/common.build_chunk` содержит неиспользуемые параметры (P3); на результат не влияют.

## Последняя фактическая проверка

Проверки ниже переданы Coordinator/Developer и сверяются отдельной приёмкой; Configurator в этом обновлении не запускал продуктовые тесты или модели. Исторические D21-результаты сохранены отдельно от новых D22-результатов.

- README фиксирует изолированный LIVE-прогон 30.09.2026: `test.bat live`, exit 0, `LIVE_CLEANUP: PASS`, `EMBEDDING_LIVE_STATUS: PASS`; Ollama 0.35.0, `embeddinggemma:300m`, dimension 768.
- `harness/acceptance.py` возвращает `D21_ACCEPTANCE_STATUS: NOT_ASSESSED`: автоматические тесты не подтверждают ручной UI-прогон и наличие видео.
- Полная приёмка D21 и любые новые прогоны подтверждаются только отдельным запуском (Developer, затем независимо Tester); в этой задаче они не выполнялись, поэтому PASS здесь не выставляется.

### D22: отчёты текущего этапа (2026-10-01)

- Architect review: PASS (по отчёту Coordinator).
- Автоматические продуктовые проверки: 240 unit-тестов и 14 integration-тестов PASS (по отчётам текущего этапа).
- Реальная выбранная Gemma: генерация с RAG и без RAG PASS; оба ответа завершены с finish_reason=stop, без усечения. Измеренная задержка — примерно 44 и 31 секунды соответственно (по отчёту Coordinator).
- Полный реальный прогон выбранной Gemma: 10 вопросов × 2 режима = 20 ответов; браузерный UI проверен (по отчёту Coordinator).
- Независимый Tester: техническая приёмка выбранного профиля/lifecycle PASS. Проверены ошибка при borrowed-модели и освобождение финального собственного PID после release; чужое использование сохранено.
- Качество D22: PARTIAL из-за замечаний Q03/Q04/Q06/Q09 и трёх length-усечений; видео D22 NOT_RUN.
- Source-backed эталоны evaluation questions JSON скорректированы Developer. Эти технические PASS не означают D22_ACCEPTANCE_STATUS: ACCEPTED.

## Решения

- Стек: Python + FastAPI + Pydantic + SQLite, статический HTML/JS без сборки и CDN (SPEC D21 §13, §16).
- Адаптеры между хранилищем и потребителем: `IndexStore` и `KnowledgeService` (SPEC D21 §5.3).
- Manifest индекса и полный набор metadata чанка (SPEC D21 §6.4, §6.3).
- Дедупликация через `fingerprint` и `INSERT OR IGNORE` (SPEC D21 §9.5).
- Атомарная активация индекса в `BEGIN IMMEDIATE`, один build на процесс (SPEC D21 §9.1–§9.2).
- Точный cosine — только для маленького корпуса; контракт `IndexStore` готов к ANN (SPEC D21 §9.3).
- D22: текущие архитектурные решения зафиксированы в существующем комплекте `docs/specs/day-22-first-rag-query/{SPEC,PLAN,ACCEPTANCE}.md`; реализация сохраняет retrieval D21, добавляет два режима генерации и сравнение 10 вопросов. Выбранный test runtime не заменяет embedding-модель.

## Test runtime contract (2026-10-01)

В MODULE_RULES зафиксирована ai-декларация, выбранный chat-профиль AI_TEST_MODEL_*, независимые embeddings и lease-finally. Полностью отсутствующий профиль допускает явный config; частичный профиль блокирует запуск без fallback. Governance preflight подтверждает декларацию и форму профиля, но не inference, UI или завершение приёмки.
