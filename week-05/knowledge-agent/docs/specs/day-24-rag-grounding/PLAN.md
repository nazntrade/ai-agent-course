# PLAN — Day 24: цитаты, источники и защита от галлюцинаций

Идентификатор задачи: `day-24-rag-grounding`.
Статус: **реализовано; план актуализирован по исправлениям D24**. Статус документа не заменяет независимую приёмку.
Дата: 2026-10-02.
Связанные документы: `SPEC.md` (требования R-01…R-20), `ACCEPTANCE.md` (критерии
D24-01…D24-20, соответствующие контракту C01…C30).

План трассируем до требований SPEC и acceptance criteria. Порядок шагов выбран от
устойчивого ядра к внешним деталям: контракты → конфиг → промпт/разбор → верификатор →
сервис → API → UI → harness → тесты → документация.

---

## 1. Файловая структура (реализована)

Прирост к структуре D23 (PLAN D23 §1); существующие retrieval/индексация, D22-чат и D23
фильтр/rewrite не переписываются, а расширяются обратно совместимо.

```
week-05/knowledge-agent/
  knowledge_agent/
    __main__.py              # build_chat_service: + проброс RAG_GROUNDING_ENABLED → ChatService(grounding_enabled=...)
                             #   (фактический build_chat_service, SPEC §2.3, §10)
    domain/
      contracts.py           # + Citation, GroundingResult, GroundedAnswer + to_dict();
                             #   + опциональный chunk_id в протоколе IndexStore.list_chunks (SPEC §11.3)
    config.py                # + RAG_GROUNDING_ENABLED, D24_DATA_PATH, D24_QUESTIONS_PATH (SPEC §10)
    chat/
      citations.py           # + GroundingVerifier, normalize_whitespace, parse_grounded_response
      prompts.py             # + grounded-rag-v1 (plain-v1/rag-v1/rewrite-v1 без изменений)
      chat_service.py        # + grounded-слой, passed-чанки с текстом, refusal/limitation/status;
                             #   параметр grounding_enabled (default=False сохраняет D22; app=§10)
    service/
      knowledge_service.py   # + опциональный chunk_id в list_chunks (аддитивно, SPEC §11.3)
    storage/
      sqlite_store.py        # + опциональный chunk_id-предикат в list_chunks (схема без изменений)
    api/
      routes.py              # + grounding-поле ChatRequest, chunk_id-фильтр чанков
    ui/
      index.html             # + панель Sources & citations, Show fragment
      app.js                 # + рендер цитат/статусов/переводов, раздельные состояния
      styles.css             # + перенос длинных строк, стили цитат
  harness/
    chat_stub.py             # + детерминированные grounded-ответы (аддитивно; D22/D23 сохранены)
    d24_grounding.py         # offline-проекции и 8 граничных фикстур (без сети)
    d24_live.py              # LIVE-раннер: 10 grounded-результатов + edge-cases
  tests/
    scenarios/
      d24-rag-grounding.py   # тонкий PRODUCT-сценарий → harness.d24_live.main
      d24-rag-grounding.json # {"schema_version":"test-scenario-v1","kind":"live"}
    unit/                    # + test_d24_citations, test_d24_grounding_prompt,
                             #   test_d24_chat_grounding, test_d24_api_grounding
    integration/             # + test_d24_grounding_end_to_end (grounded app-run, RAG_GROUNDING_ENABLED=1);
                             #   test_selected_profile_end_to_end / test_chat_end_to_end / test_d23_end_to_end
                             #   пиннятся RAG_GROUNDING_ENABLED=0 (SPEC §2.3, §11.5)
  README.md                  # + раздел «Day 24» (обновляет Coordinator после реализации)
```

`local-data/d24/` — каталог артефактов (уже под `local-data/`, в Git не попадает).
Имена модулей уточняются на реализации; контракты и границы — обязательны.

---

## 2. Порядок работ P-01…P-16

| ID | Шаг | Артефакты | Требование | Уровень |
| --- | --- | --- | --- | --- |
| P-01 | D24-контракты: `Citation`, `GroundingResult`, `GroundedAnswer`, коды ошибок (`chat_invalid_response` details) | `domain/contracts.py`, `domain/errors.py` | SPEC §6, §8; R-01…R-08, R-14 | UNIT |
| P-02 | Конфигурация `RAG_GROUNDING_ENABLED`/`D24_*` и проброс настройки в приложение | `config.py`, `.env.example`, `knowledge_agent/__main__.py` (build_chat_service → `ChatService(grounding_enabled=...)`) | SPEC §2.3, §10; R-01, R-02, R-14 | UNIT |
| P-03 | `grounded-rag-v1` (JSON-ответ, защита от инструкций) + сохранение `rag-v1`/`plain-v1` | `chat/prompts.py` | SPEC §6.1, §7.5; R-08, R-09 | UNIT |
| P-04 | `parse_grounded_response` (строгий разбор JSON, fenced-блок, типизированная форматная ошибка) | `chat/citations.py` | SPEC §6.1, §8.2; R-14 | UNIT |
| P-05 | `GroundingVerifier` + `normalize_whitespace` (source_exists, quote_verbatim, перевод/оригинал, запрет подмены) | `chat/citations.py` | SPEC §7; R-03…R-08 | UNIT |
| P-06 | `ChatService`: grounded-слой, `passed`-тексты, `status`/`reason`/`threshold`, `refusal`, `limitation`, различение технических ошибок (порядок формат→truncated), `grounding_enabled` (default=False = D22; app=`RAG_GROUNDING_ENABLED`), compare исключён | `chat/chat_service.py`, `knowledge_agent/__main__.py` (проброс `RAG_GROUNDING_ENABLED`) | SPEC §5.3, §6.1, §6.3, §8, §11.5; R-01, R-02, R-10…R-14 | UNIT |
| P-07 | HTTP API: `grounding` в `ChatRequest`/`ChatAnswer` (только `ChatRequest`), `chunk_id`-фильтр чанков с аддитивным расширением контракта ядра `IndexStore.list_chunks`/`KnowledgeService.list_chunks`/`IndexStore`-реализации (SQL-предикат, без схемы) | `api/routes.py`, `domain/contracts.py`, `service/knowledge_service.py`, `storage/sqlite_store.py` | SPEC §11, §11.3, §11.5; R-01, R-14, R-15 | UNIT + INT |
| P-08 | UI: Sources & citations, перевод, Show fragment, перенос строк, раздельные состояния, англ. новые элементы | `ui/index.html`, `ui/app.js`, `ui/styles.css` | SPEC §12; R-15, R-16 | UI + MANUAL |
| P-09 | Offline harness: проекции + 8 граничных фикстур на stub/fake; аддитивный grounded-режим стаба | `harness/d24_grounding.py`, `harness/chat_stub.py` | SPEC §13.3, §16, §16.1; R-20 | UNIT/INT |
| P-10 | LIVE-раннер и сценарий: 10 grounded-результатов + merge edge-cases | `harness/d24_live.py`, `tests/scenarios/d24-rag-grounding.{py,json}` | SPEC §13; R-17…R-20 | LIVE |
| P-11 | UNIT-тесты D24 | `tests/unit/*` | SPEC §16; R-03…R-14 | UNIT |
| P-12 | INT-тесты D24 и регрессия D21–D23: grounded app-run INT `test_d24_grounding_end_to_end` при `RAG_GROUNDING_ENABLED=1`; существующие D22/D23 app-run INT пиннятся `RAG_GROUNDING_ENABLED=0` | `tests/integration/test_d24_grounding_end_to_end.py`, `tests/integration/*` | SPEC §2.3, §11.5, §16; R-02, R-12, R-14 | INT |
| P-13 | Восемь граничных сценариев (кейсы C27) | `tests/unit/*`, `tests/integration/*`, `harness/d24_grounding.py` | SPEC §13.3; R-20 | UNIT + INT (+LIVE) |
| P-14 | Сверка эталонов `eval/d22/questions.json` с документом (9+1) | `eval/d22/questions.json` (при необходимости — только эталонные поля), протокол | SPEC §13; R-17 | MANUAL |
| P-15 | Документация Day 24 | `README.md` | MODULE_RULES §6; R-20 | MANUAL (Coordinator) |
| P-16 | Приёмка: evidence, статусы, исправления в пределах задачи | отчёт/evidence | SPEC §18; R-18…R-20 | MANUAL |

Зависимости: P-01 → P-02; P-01 → P-03 → P-04 → P-05 → P-06; P-06 → P-07 → P-08;
P-05/P-06 → P-09; P-06/P-07/P-09 → P-10; P-03…P-09 → P-11/P-12/P-13; P-09/P-10 → P-13;
P-10/P-14 → P-16; P-02…P-13 → P-15.

---

## 3. Ключевые решения

* **HTTP-контракт и схемы артефактов — только в SPEC §11 и §13.** PLAN и код ссылаются на
  SPEC и не дублируют пути и схемы.
* **Аддитивность**: `answer.grounding` добавлен, D22/D23-поля сохранены; `grounding=false`
  или `RAG_GROUNDING_ENABLED=0` даёт ровно прежний `rag-v1`-путь. Без-RAG (`plain-v1`) не
  затрагивается.
* **Аддитивность для app-run INT**: приложение (`python -m knowledge_agent`) по умолчанию
  идёт с `RAG_GROUNDING_ENABLED=1`, поэтому grounded `/api/chat` проверяет отдельный app-run INT
  `test_d24_grounding_end_to_end`, а существующие D22/D23 app-run INT (`test_selected_profile_*`,
  `test_chat_end_to_end`, `test_d23_end_to_end`) пиннятся `RAG_GROUNDING_ENABLED=0`; grounded-стаб
  кладёт `[chunk_id]` в поле `answer`, сохраняя D22-проекцию `answer.citations.valid` (SPEC §2.3,
  §11.5, §16.1).
* **Источники = `passed`**: UI и артефакт используют `retrieval.passed`; кандидаты,
  отсеянные фильтром/бюджетом, в источники не попадают.
* **`source`/`section` из `metadata`**: верификатор берёт `source_label`/`section_path` из
  переданного фрагмента; текст модели для этого не используется.
* **Три проверки различимы**: `source_exists`, `quote_verbatim` — формальные;
  `meaning_supported=null`/`meaning_check="not_performed"` — смысл оценивает Tester вручную.
  Формальная валидация никогда не выставляет смысловую метку.
* **Нормализация пробелов — одна функция** (`normalize_whitespace`): NFC + схлопывание
  пробельных последовательностей в один ASCII-пробел + trim; сравнение регистрозависимое.
* **Форматная ошибка — техническая**: `parse_grounded_response` не «дочиняет» JSON и не
  заменяет кавычки; ошибка → `chat_invalid_response` (503).
* **Отказ ≠ ошибка**: `below_threshold` (пустой `selected`, модель не вызвана) и
  `model_insufficient` — `200` с `insufficient_sources=true`, пустые источники/цитаты
  допустимы, `refusal.reason`/`threshold` сохранены.
* **Технические ошибки отделены**: пустой ответ/формат → `chat_invalid_response`;
  `finish_reason=length` → `truncated=true`, `grounding.status="failed"`; провайдер →
  `chat_*` 503. Ни одна из них не выглядит честным отказом.
* **Границы grounded-слоя по endpoint-ам** (SPEC §11.5): grounding применяется к `/api/chat`,
  `/api/chat/stream`, `/api/chat/compare-modes`; `POST /api/chat/compare` **не затрагивается**
  (ветви `rag-v1`/`plain-v1`, `comparison.prompt_templates.with_rag="rag-v1"`), поэтому
  `harness/smoke.py` и INT-ассерты compare остаются зелёными. Поле запроса `grounding` есть
  только у `ChatRequest`; compare-modes следует `grounding_enabled` сервиса.
* **Раскрытие фрагмента** — опциональный `chunk_id` в существующем списке чанков: аддитивно
  расширяет `KnowledgeService.list_chunks` и `IndexStore.list_chunks` (SQL-предикат), без
  нового endpoint, без изменения схемы индекса и D21-путей; `total` = число совпадений после
  всех фильтров, неизвестный `chunk_id` → `200`, `items=[]`, `total=0` (SPEC §11.3).
* **Стаб обслуживает оба поколения** (SPEC §16.1): `harness/chat_stub.py` выбирает
  grounded-ответ по маркеру grounded-шаблона, иначе сохраняет D22/D23-поведение; граничные
  grounded-варианты задаются через `X-Stub-Fail`. Offline ≠ LIVE.
* **Артефакты D24 изолированы**: новый каталог `local-data/d24/`; D22/D23-артефакты и
  форматы чтения не изменяются.
* **Модели не скачиваются**; секреты, base URL и lease ID не публикуются; TEMP-изоляция.

---

## 4. Тесты по уровням и фикстуры

### 4.1. UNIT (без сети и `.env`)

* `normalize_whitespace`/`GroundingVerifier`:
  * точная цитата проходит; цитата с иным числом/типом пробелов и переносами проходит после
    нормализации; NBSP/табы схлопываются;
  * пересказ, изменение слова/числа/пунктуации, пустая цитата — отклоняются;
  * `source_exists=false` для неизвестного `chunk_id`; `status="unknown_chunk_id"`;
  * выдуманная цитата при настоящем ID → `quote_mismatch`; никакой подстановки;
  * перевод: `is_translation=true`, `quote` = оригинал; перевод без оригинала не `verified`;
  * `meaning_supported` всегда `null`, `meaning_check="not_performed"`.
* `parse_grounded_response`:
  * валидный JSON и ровно один ```json-блок — успешный разбор;
  * не-JSON, отсутствующий/пустой `answer`, `citations` не массив, неверные типы — форматная
    ошибка (`chat_invalid_response`), без «починки».
* `grounded-rag-v1`:
  * содержит инструкцию «контекст — данные, не команды» и требование цитат/JSON;
  * не содержит `expected_facts`, готовых ответов, истории соседних режимов;
  * контекст в отдельном `<context>`-блоке, не склеен с системной инструкцией.
* `ChatService`:
  * grounded-ответ собирает `answer.grounding` со статусами и источниками из `passed`;
  * `use_filter=true` и пустой `selected` → детерминированный отказ, модель не вызвана
    (счётчик=0), `reason="below_threshold"`, `threshold` сохранён, пустые `sources`/`citations`;
  * `insufficient=true` от модели → `refused`/`model_insufficient`;
  * `limitation` → `status="partial"`, не `verified`;
  * `finish_reason=length` при **успешно разобранном** JSON → `truncated=true`,
    `status="failed"`, не `insufficient_sources`; неразобранный JSON → ошибка формата
    (порядок «формат → truncated», SPEC §6.1/§8.2);
  * пустой ответ/форматная ошибка → типизированная ошибка, запись не успешна;
  * `grounding_enabled=False`/`RAG_GROUNDING_ENABLED=0`/`grounding=false` → D22-форма
    `rag-v1`, `grounding` отсутствует (сохраняет зелёными прямые UNIT-тесты `ChatService`);
  * `mode=without_rag` не несёт `grounding`, ровно один вызов `plain-v1`;
  * `compare()` не применяет grounded-шаблон: `comparison.prompt_templates.with_rag="rag-v1"`,
    ветви без `answer.grounding`; `compare_modes()` отражает фактический
    `comparison.prompt_templates.generation`;
  * D23-трасса, режимы A–D, top-K-слияние и раздельные метрики сохранены.
* API-валидация `grounding` и `chunk_id`-фильтра.

### 4.2. INT (реальные процессы, loopback; без внешней сети)

* `harness/embed_stub.py` + `harness/chat_stub.py`:
  * grounded-поток всех D23-режимов: показанные `sources` совпадают с `passed`, не с
    `candidates`;
  * `chat_stub` возвращает grounded-JSON с неизвестным `chunk_id`, выдуманной цитатой,
    корректной цитатой, `insufficient=true`, пустым/битым JSON, `finish_reason=length`;
  * `chunk_id`-фильтр `GET /api/index-versions/{id}/chunks` возвращает соответствующий
    фрагмент (`total=1`), неизвестный `chunk_id` → `200`, `items=[]`, `total=0`; `filters`
    эхом возвращает `chunk_id`;
  * `chat_stub` выбирает grounded-ответ по маркеру grounded-шаблона; D22/D23-запросы
    обслуживаются прежним свободным текстом (SPEC §16.1);
  * пустой `selected` при высоком пороге → `200`, `insufficient_sources=true`, генерации нет;
  * ошибка/таймаут провайдера → `chat_*` 503, не маскируется;
  * D21–D23 регрессия: `/api/search`, `/api/index/*`, `/api/compare`, `/api/collections`,
    `/api/chat` без D24-полей (при выключенном grounding/дефолте сервиса),
    `/api/chat/compare` с `comparison.prompt_templates.with_rag="rag-v1"` и без
    `answer.grounding`; `/api/chat/compare-modes` с неизменными именами/настройками/трассой.
* **App-run INT (`python -m knowledge_agent`, `Backend`) и дефолт `RAG_GROUNDING_ENABLED=1`:**
  * `test_d24_grounding_end_to_end.py` — grounded `/api/chat`, `/api/chat/stream` и
    `/api/chat/compare-modes` на дефолтном профиле: `answer.grounding`, `template_id="grounded-rag-v1"`,
    источники ⊆ `passed`, неизвестный/выдуманный `chunk_id` и битый JSON дают ожидаемые статусы;
  * `test_selected_profile_end_to_end.py` (fake-провайдер `Selected answer`, `with_rag` → `200`,
    строки 64–77), `test_chat_end_to_end.py` (`answer.citations.valid`, строка 203) и
    `test_d23_end_to_end.py` (четыре режима/фильтр/rewrite) пиннятся `RAG_GROUNDING_ENABLED=0`
    и остаются чистым D22/D23-регрессом на плоской `rag-v1`-форме;
  * `smoke_test.bat` работает на дефолте и опирается на аддитивный grounded-стаб.
* Изоляция: `KNOWLEDGE_DB_PATH` и `CHAT_RUNS_PATH` в TEMP; пользовательская БД не
  затрагивается.

### 4.3. Фикстуры

* `harness/chat_stub.py` — детерминированные grounded-ответы и инъекции: корректный JSON,
  неизвестный `chunk_id`, выдуманная цитата, `insufficient`, `limitation`, пустой ответ,
  битый JSON, `finish_reason=length`, ошибка/таймаут.
* `harness/d24_grounding.py` — offline-проекция результата в `grounding-results`-запись и
  генерация 8 кейсов `edge-cases.json` без сети.
* Корпус `agents-survey.pdf` в Git не попадает; путь задаёт пользователь; для INT
  используются синтетические TXT/MD из D21.

---

## 5. Harness и режимы `.bat` (создаёт Configurator)

Точки входа и их содержимое изменяет только Configurator (GOVERNANCE). Developer
реализует модули и PRODUCT-сценарий, на которые ссылаются bat. D24 использует
существующие точки входа и уже заявленный режим сценария:

* `test.bat unit` / `integration` — без сети и `.env`.
* `test.bat scenario d24-rag-grounding` — доверенный сценарий (`kind: live`), запускает
  `harness/d24_live.py`: policy проверяется до реальных действий; выбранный `AI_TEST_MODEL_*`
  профиль и `TestSession` управляют lease; backend — на своём свободном loopback-порту с
  TEMP-БД; артефакты пишутся в `local-data/d24/`.
* `smoke_test.bat` — INT-сценарий (stub).
* `run_app.bat` — фактический запуск UI с панелью цитат.

Правило: LIVE не запускается автоматически; `MODEL_CHECK_KIND` не выставляется без
реального вызова. `.bat` не изменяются этой задачей.

---

## 6. План LIVE-прогона (SPEC §13)

1. Policy: `harness/scenario_runner.py` требует **ровно** `AI_TEST_LIVE_POLICY=allowed`
   (`validate_live_profile`, `scenario_runner.py:37–43`); missing/empty/legacy/иная политика →
   `ScenarioBlocked` → `BLOCKED`, реальные вызовы `NOT_RUN`. «Standalone-авторизация вне панели»
   к `test.bat scenario` **не применяется** (SPEC §10.1). `report_live_blocked("D24_RUNNER_STATUS")`
   вызывается до любых реальных действий.
2. Профиль: требуется полный не-stub `AI_TEST_MODEL_*` (выбранный remote
   `deepseek/deepseek-flash`); частичный/отсутствующий — `BLOCKED` без fallback.
3. Lease: `TestSession` acquire/use/release в `finally`; remote-профиль локально не
   запускается/не останавливается; TEMP-БД и TEMP-каталог прогона; cleanup в `finally`.
4. Индекс: один закреплённый `structure`-индекс явно указанного существующего корпуса;
   `index_version_id` фиксируется и используется всеми 10 результатами.
5. Вопросы: ровно 10 из `eval/d22/questions.json` (9 `answerable:true` + 1 `answerable:false`),
   grounding включён; настройки ответа одинаковы; каждая запись — отдельный запрос без
   истории.
6. Сохранение: `grounding-results.json` (10 результатов, один `index_version_id`).
7. Границы: 8 кейсов `edge-cases.json`; offline-кейсы из `d24_grounding.py` merge с
   LIVE-наблюдениями (слабый контекст, отказ, граница провайдера).
8. Маркировка: `MODEL_CHECK_KIND: NETWORK` (remote); LOCAL-статусы не выставляются. Токены
   проверяемой модели не смешиваются с токенами OpenCode-агента; один вызов не учитывается
   дважды. `D24_QUALITY_STATUS: NOT_ASSESSED` — оценка остаётся за Tester.

---

## 7. План сохранения артефактов (точные пути)

| Артефакт | Путь | Схема/формат | Кто создаёт | Уровень |
| --- | --- | --- | --- | --- |
| Результаты | `week-05/knowledge-agent/local-data/d24/grounding-results.json` | `d24-grounding-v1` (JSON) | `harness/d24_live.py` | LIVE |
| Границы | `week-05/knowledge-agent/local-data/d24/edge-cases.json` | `d24-edge-cases-v1` (JSON) | `harness/d24_grounding.py` + `harness/d24_live.py` | UNIT/INT/LIVE |
| Оценка | `week-05/knowledge-agent/local-data/d24/quality-assessment.md` | Markdown | Tester (MANUAL) | MANUAL |

Обязательные ключи `grounding-results.json`: `schema_version="d24-grounding-v1"`,
`index_version_id` (строка), `results` (массив ≥10), а также `created_at`, `corpus_label`,
`source_sha256`, `index`, `model`, `embedding`, `grounding`, `threshold`, `summary`.

Обязательные ключи одной записи `results[]`: `question_id`, `question`, `answerable`,
`answer_text`, `finish_reason`, `truncated`, `insufficient_sources`, `grounding_status`,
`grounding_reason`, `limitation`, `verification.{source_exists,quote_verbatim,
meaning_supported,meaning_check}`, `sources[]` (`chunk_id`/`source`/`section`/pages),
`passed_chunk_ids[]`, `citations[]`, `refusal`, `error`, `usage`, `latency_ms`, `errors`.

Схема `edge-cases.json` (обязательные ключи): `schema_version="d24-edge-cases-v1"`,
`cases` (массив ≥8); каждая запись — `case_id`, `level`, `scenario`, `expected`, `observed`,
`status`.

`quality-assessment.md` не создаётся раннером и не подменяет LIVE-доказательства.

---

## 8. Ручные сценарии и UI

* `run_app.bat`: большой чат сверху → вопрос в RAG-режиме → ответ, блок
  `Sources & citations` (`source`, `section`, `chunk_id`, страницы), точные цитаты, пометка
  перевода, `Show fragment` для сверки, `Meaning support: not checked`.
* Раздельные состояния: `Verification failed` (некорректная цитата/неизвестный `chunk_id`)
  и `No relevant sources found` (недостаток информации).
* Новые элементы — англоязычные; существующие русские панели D22/D23 **не переводятся**;
  язык ответа — по вопросу.
* Длинные цитаты/ID переносятся, без горизонтальной обрезки; проверяются ширины
  ширины 720/1024/1280/1920 px.
* Отдельно проверяются: выключенный grounding (D22-форма), без-RAG (без цитат), четыре
  режима D23 с цитатами в колонках.

---

## 9. Риски и снижение

| Риск | Снижение |
| --- | --- |
| Выдуманная цитата выглядит успешной | `quote_verbatim`/`status`; запрет подмены; UI `Verification failed`; UNIT/INT |
| Форматный ответ модели ломает RAG | строгий разбор; типизированная `chat_invalid_response`; LIVE-граница; regression-fixture |
| Отказ смешивается с технической ошибкой | отдельные `refusal.reason` и коды/статусы; §8; INT-сценарии обоих состояний |
| Формальная проверка выдаётся за смысловую | `meaning_check="not_performed"`; UI-метка; ручная оценка Tester |
| Высокий score без факта подаётся как ответ | `limitation`/`partial`; отказ допустим; C12 |
| Регрессия D22/D23 из-за нового шаблона | `RAG_GROUNDING_ENABLED=0` восстанавливает `rag-v1`; grounding аддитивен; D21–D23 тесты |
| Источники показывают кандидатов, а не `passed` | UI/артефакт читают только `retrieval.passed`; UNIT/INT сравнение |
| Смысл режимов D23 подменён фильтрацией | трасса и режимы не меняются; D23-регрессия |
| Пользовательская БД/индекс затронуты | TEMP для `KNOWLEDGE_DB_PATH`/`CHAT_RUNS_PATH`; отдельный `local-data/d24` |
| Секреты/endpoint в отчёте | санитизация; ключи/lease не печатаются; `MODEL_CHECK_KIND` без endpoint |

---

## 10. Что нельзя проверить только чтением кода

Требуют фактического выполнения:

* реальный grounded-JSON выбранной remote-модели и его формат;
* точность цитат и `quote_verbatim` на реальных фрагментах корпуса;
* 10 результатов, один закреплённый индекс и сводные метрики;
* содержательная правильность ответов, полнота и смысловая поддержка (ручная оценка Tester);
* границы провайдера (пустой/обрезанный ответ, ошибка/таймаут/недоступность);
* UI в браузере (цитаты, перевод, `Show fragment`, перенос строк, раздельные состояния);
* `run_app.bat` и `test.bat scenario d24-rag-grounding`.

Эти пункты не считаются выполненными по умолчанию и помечаются «не проверено» до прогона.

---

## 11. Трассировка P→D24

| Шаг | R (SPEC §18) | D24 |
| --- | --- | --- |
| P-01 | R-01…R-08, R-14 | D24-01, D24-03…D24-08, D24-13 |
| P-02 | R-01, R-02, R-14 | D24-01, D24-02, D24-13 |
| P-03 | R-08, R-09 | D24-07, D24-08 |
| P-04 | R-14 | D24-13 |
| P-05 | R-03…R-08 | D24-03…D24-08 |
| P-06 | R-01, R-02, R-10…R-14 | D24-01, D24-02, D24-09…D24-13 |
| P-07 | R-01, R-14, R-15 | D24-01, D24-13, D24-14 |
| P-08 | R-15, R-16 | D24-14, D24-15 |
| P-09 | R-20 | D24-19 |
| P-10 | R-17…R-20 | D24-16…D24-20 |
| P-11 | R-03…R-14 | D24-03…D24-13 |
| P-12 | R-02, R-12, R-14 | D24-02, D24-11, D24-13 |
| P-13 | R-20 | D24-19 |
| P-14 | R-17 | D24-16 |
| P-15 | R-20 | D24-20 |
| P-16 | R-18…R-20 | D24-17…D24-20 |

---

## 12. Определение готовности (Definition of Done)

* Все Must-требования SPEC §2.1 реализованы; `answer.grounding` аддитивен.
* Критерии D24-01…D24-20 зелёные на требуемых уровнях (UNIT/INT/LIVE/UI/MANUAL).
* Артефакты `local-data/d24/{grounding-results.json,edge-cases.json}` сохранены
  Developer-ом; `quality-assessment.md` сохранён независимо Tester-ом.
* D21–D23 регрессия зелёная; `RAG_GROUNDING_ENABLED=0` (или `grounding_enabled=False`)
  восстанавливает D22-форму; `POST /api/chat/compare` не затрагивается grounding.
* App-run INT: grounded `/api/chat` проверяется `tests/integration/test_d24_grounding_end_to_end.py`
  при дефолтном `RAG_GROUNDING_ENABLED=1`; D22/D23 app-run INT пиннятся `RAG_GROUNDING_ENABLED=0`;
  `knowledge_agent/__main__.py` пробрасывает `RAG_GROUNDING_ENABLED` в `ChatService(grounding_enabled=...)`.
* `chunk_id`-фильтр `/api/index-versions/{id}/chunks` реализован аддитивно в
  `KnowledgeService.list_chunks`/`IndexStore.list_chunks`; неизвестный `chunk_id` → `200`,
  `items=[]`, `total=0`.
* Реальный запуск/UI проверяют Developer, затем независимо Tester.
* `TEST_STATUS`/evidence присваивает Tester; задача не принята при `FAIL`/`BLOCKED`.
* README обновлён итогами D24 (Coordinator) до финального отчёта.

---

## 13. Статус

`PLAN_STATUS: IMPLEMENTED_AND_SYNCHRONIZED`.

Порядок обязательных проверок после реализации: Developer → Architect post-review →
независимый Tester → приёмка. Реальные LIVE-проверки выполняются только в рамках
обязательного сценария и действующей авторизации; повторные платные прогоны автоматически
не инициируются.

## 14. Завершённый корректирующий цикл D24

Сохраняет исходные требования и критерии, уточняя P-05/P-06/P-11:

1. Контекстный бюджет использует фактически отправляемый `template.system` — grounded
   либо обычный RAG. Граница, на которой короткий RAG помещается, а grounded нет,
   должна дать `context_overflow`, а не скрытое превышение бюджета.
2. Inline `[chunk_id]` сверяются с passed и verified структурированными цитатами.
   `inline_unsupported`/`inline_missing_quote` и причины `unsupported_citation`/`missing_quote`
   отражаются в GroundingResult; нарушения исключают общий verified.
3. Нет citations — failed/no_citations независимо от null/пустого/непустого limitation.
   Пробельный limitation не понижает подтверждённый ответ; непустое ограничение даёт
   partial только при наличии подтверждённой цитаты.
4. Постоянные регрессии: `tests/unit/test_d24_defect_regressions.py`; покрытие включает
   границу бюджета, неизвестные/неподтверждённые inline-ссылки, варианты limitation,
   проекцию ChatService. Обязательные ожидаемые результаты — ACCEPTANCE §4.1.

OpenAPI сохраняет общую объектную response-схему; подробный grounding-контракт описан
в SPEC §6.3/§11 и проверяется runtime-тестами. Обновление этих документов не означает
нового LIVE-прогона и не подтверждает содержательное качество автоматически.
