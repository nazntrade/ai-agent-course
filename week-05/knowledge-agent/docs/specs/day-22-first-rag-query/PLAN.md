# PLAN — Day 22: первый RAG-запрос

Идентификатор задачи: `day-22-first-rag-query`.
Статус: **ожидает spec review** (план реализации; код, тесты и `.bat` этим документом не
создаются).
Дата: 2026-10-01.
Связанные документы: `SPEC.md` (требования R-01…R-20), `ACCEPTANCE.md` (критерии
D22-01…D22-15).

План трассируем до требований SPEC и acceptance criteria. Порядок шагов выбран от
устойчивого ядра к внешним деталям: контракты → конфиг → chat-адаптер → контекст/шаблоны →
ChatService → хранение → API → UI → harness/eval → документация.

---

## 1. Файловая структура (предлагаемая)

Прирост к структуре D21 (PLAN D21 §1); существующие файлы retrieval/индексации не
переписываются.

```
week-05/knowledge-agent/
  knowledge_agent/
    domain/
      contracts.py           # + ChatModel, ChatMessage, ChatModelIdentity, ChatUsage,
                             #   ChatResult, ChatModelEvent, ChatStreamEvent, ChatRunStore
      errors.py              # + ChatError и подклассы, ContextOverflow
    config.py                # + CHAT_* настройки (SPEC §8)
    chat/                    # НОВЫЙ пакет
      __init__.py
      ollama_chat.py         # OllamaChatModel: preflight, chat, stream_chat, usage, finish_reason
      context.py             # ContextBudget (heuristic-v1, отбор passed, overflow)
      prompts.py             # PromptTemplate: plain-v1, rag-v1; обращения к недоверенным данным
      citations.py           # извлечение [chunk_id], валидация по passed
      run_store.py           # FileChatRunStore: chat-run-v1, chat-eval-v1, runs.jsonl
      chat_service.py        # ChatService: режимы, сравнение, вызовы KnowledgeService.search
    api/
      routes.py              # + POST /api/chat, /api/chat/stream, /api/chat/compare,
                             #   /api/chat-runs*, health.chat
    __main__.py              # + сборка OllamaChatModel/FileChatRunStore/ChatService
    ui/
      index.html             # + панель чата (русский)
      app.js                 # + три действия, streaming, источники, ошибки
      styles.css             # + стили панели чата
  eval/
    d22/
      questions.json         # rag-eval-questions-v1: 10 вопросов вне корпуса (SPEC §14.1)
  harness/
    chat_stub.py             # детерминированный chat-провайдер (НЕ inference), границы провайдера
    rag_eval.py              # прогон 10 вопросов в обоих режимах → chat-run-v1 + сводка
    live_chat.py             # opt-in LIVE: один реальный вызов локальной chat-модели
  tests/
    unit/                    # + test_chat_contracts, test_chat_model, test_context_budget,
                             #   test_prompts_citations, test_chat_service, test_chat_run_store,
                             #   test_chat_api_contract, test_chat_config
    integration/             # + test_chat_end_to_end (embed_stub + chat_stub), D21-регрессия
  .env.example               # + CHAT_* без значений
  README.md                  # + раздел «Day 22»
```

`local-data/chat-runs/` — каталог результатов (уже под `local-data/`, в Git не попадает).

Имена модулей уточняются на реализации; контракты и границы — обязательны.

---

## 2. Порядок работ P-01…P-10

| ID | Шаг | Артефакты | Требование |
| --- | --- | --- | --- |
| P-01 | Chat-контракты и ошибки | `domain/contracts.py`, `domain/errors.py` | SPEC §5, §7; R-01, R-02, R-03, R-16 |
| P-02 | Конфигурация `CHAT_*` | `config.py`, `.env.example` | SPEC §8; R-02, R-03, R-09, R-16 |
| P-03 | `OllamaChatModel` | `chat/ollama_chat.py` | SPEC §16; R-02, R-03, R-15, R-16, R-19 |
| P-04 | Контекст, промпты, цитаты | `chat/context.py`, `chat/prompts.py`, `chat/citations.py` | SPEC §9, §10; R-06, R-09, R-10 |
| P-05 | `ChatService` (режимы, сравнение) | `chat/chat_service.py` | SPEC §5.3, §11.2, §13; R-01, R-02, R-04, R-05, R-06, R-08, R-09 |
| P-06 | Хранение прогонов/оценок | `chat/run_store.py` | SPEC §15; R-12, R-13, R-14 |
| P-07 | HTTP API и сборка приложения | `api/routes.py`, `__main__.py` | SPEC §11; R-02, R-03, R-05, R-13, R-14, R-16 |
| P-08 | UI (русский, три действия, streaming) | `ui/index.html`, `ui/app.js`, `ui/styles.css` | SPEC §12; R-07, R-08 |
| P-09 | Eval-набор, stub и harness | `eval/d22/questions.json`, `harness/chat_stub.py`, `harness/rag_eval.py`, `harness/live_chat.py` | SPEC §14, §15.4, §16; R-01, R-11, R-12, R-17, R-18, R-19, R-20 |
| P-10 | README, 10 пар, сценарий видео | `README.md` | SPEC §18; R-19, R-20 |

Зависимости: P-01 → P-02 → P-03; P-01 → P-04 → P-05; P-01 → P-06; P-03/P-04/P-05/P-06 →
P-07; P-07 → P-08; P-03/P-05 → P-09; всё → P-10.

---

## 3. Ключевые решения

* **HTTP-контракт (пути, параметры, формы ответов) и хранение результатов — только в
  SPEC §11 и §15.** PLAN и код ссылаются на SPEC и не дублируют пути и схемы.
* `ChatService` **не дублирует retrieval**: вызывает существующий
  `KnowledgeService.search` и пробрасывает его ошибки (`index_not_ready`,
  `index_incompatible`, `embedding_*`) без превращения в ответ без RAG.
* `ChatModel` — контракт ядра; `OllamaChatModel` использует stdlib `urllib` с инъектируемым
  `transport` (как `OllamaEmbedder`), новых зависимостей нет.
* `without_rag` не обращается к `IndexStore`/retrieval/embedder вообще; ветвление происходит до
  любого retrieval-вызова.
* `index_version_id` разрешается один раз и фиксируется; для `compare` — до запуска веток,
  при отсутствии ready/совместимого индекса возвращается 409 без частичного прогона.
* Сравнение создаёт по отдельному списку сообщений на ветку; ответ одной ветки не попадает
  в историю другой.
* Промпт-шаблоны версионируются `template_id` + хэш содержимого; в отчёт пишутся оба
  шаблона и различия политики режимов.
* Источники — недоверенные данные: отдельный блок контекста и системная инструкция;
  цитаты валидируются по `passed`.
* Бюджет контекста — `heuristic-v1`, резерв `CHAT_MAX_OUTPUT_TOKENS`; `lexical-v1` не
  используется как chat-токены; переполнение — явное (`context_overflow`/`chat_length_error`).
* Прогоны неизменяемы; ручная оценка — отдельный файл `chat-eval-v1`.
* Модели не скачиваются автоматически; `CHAT_MODEL` по умолчанию пуст, ошибка явная.

---

## 4. Тесты по уровням и фикстуры

### 4.1. UNIT (без сети и `.env`)

* Контракты/ошибки: коды и HTTP-статусы SPEC §7.2; `to_dict` без секретов.
* `OllamaChatModel` на mock-transport:
  * обычный ответ: `text`, `finish_reason`, usage, latency;
  * `done_reason = "length"` → `finish_reason="length"`, `truncated=true`;
  * отсутствие `prompt_eval_count`/`eval_count` → `usage=null`, значения не выдуманы;
  * частичный usage (есть только `eval_count`) сохраняется как есть;
  * `output_tokens_per_second` считается только при наличии `eval_count` и `eval_duration`;
  * 404 «model not found» → `chat_model_missing` с `hint`, без автоскачивания;
  * timeout → `chat_timeout`; 5xx/битый JSON → `chat_unavailable`/`chat_invalid_response`;
  * streaming-агрегация NDJSON: токены складываются, финальный чанк несёт usage/done;
  * preflight без инференса (`/api/version`, `/api/tags`, `/api/show`).
* `ContextBudget`: `heuristic-v1`; отбор `passed ⊆ found`; `dropped_chunks`; `context_overflow`
  422; `max_context_tokens` только уменьшает бюджет; `lexical-v1` не используется.
* `prompts`/`citations`: сборка `plain-v1`/`rag-v1`; отдельный блок контекста; извлечение
  `[chunk_id]`; `valid`/`unsupported` относительно `passed`; отсутствие склейки текста чанка
  с системным сообщением.
* `ChatService` на fake-`ChatModel` и fake-`KnowledgeService`:
  * `without_rag` не вызывает retrieval/embedding (счётчики вызовов = 0);
  * `with_rag` без индекса → `index_not_ready`; ошибка поиска пробрасывается;
  * `passed`/`found` берутся из разных полей; `index_version_id` зафиксирован;
  * `compare`: одна identity/настройки; две отдельные истории; нет утечки; шаблоны и
    различия политики в `comparison`; отсутствие ready-индекса → 409 без прогона.
* `FileChatRunStore`: запись/чтение `chat-run-v1`; `runs.jsonl`; `chat-eval-v1`;
  неизменяемость прогона; `GET` отсутствующего → 404.
* `ChatRequest`/`ChatAnswer` Pydantic-валидация и форма ошибок API.
* `load_settings` с `CHAT_*` без чтения `.env`.

### 4.2. INT (реальные процессы, loopback; без внешней сети)

* `harness/embed_stub.py` + `harness/chat_stub.py`:
  * полный цикл: build индекса → `POST /api/chat` `with_rag` и `without_rag`;
  * `POST /api/chat/compare` — две ветки, шаблоны в отчёте;
  * `POST /api/chat/stream` — потоковые события, финальный `done` с usage;
  * `GET /api/chat-runs`, `GET /api/chat-runs/{id}`, `PUT/GET .../evaluation` (персистентность
    через TEMP `CHAT_RUNS_PATH`);
  * `chat_model_missing` от stub при отсутствии модели;
  * `with_rag` без ready-индекса → 409 `index_not_ready`;
  * несовместимая embedding-конфигурация → 409 `index_incompatible` до эмбеддинга запроса;
  * `without_rag` работает при пустой БД/без коллекции.
* Изоляция: `KNOWLEDGE_DB_PATH` и `CHAT_RUNS_PATH` в TEMP; пользовательская БД не
  затрагивается.
* D21-регрессия: `/api/search`, `/api/index/build`, `/api/compare`, `/api/collections`
  работают как раньше; существующие INT-сценарии зелёные.

### 4.3. Фикстуры

* `harness/chat_stub.py` — детерминированные ответы (не inference), с инъекцией:
  `done_reason=length`, отсутствие usage, частичный usage, `model_missing`, `unavailable`,
  `timeout`, streaming и не-streaming.
* `eval/d22/questions.json` — 10 вопросов (9 отвечаемых + 1 неотвечаемый).
* Корпус `agents-survey.pdf` в Git не попадает; путь задаёт пользователь; для INT
  используются синтетические TXT/MD из D21.

---

## 5. Harness и режимы `.bat` (создаёт Configurator)

Точки входа и их содержимое изменяет только Configurator (GOVERNANCE). Developer
реализует модули, на которые они ссылаются. D22 добавляет режимы в существующие `.bat`
(точное содержимое — на GOVERNANCE-этапе после review PLAN):

* `test.bat unit` / `integration` — без сети и `.env`; INT использует `embed_stub` +
  `chat_stub` и TEMP-БД/`CHAT_RUNS_PATH`.
* `test.bat live` — opt-in LIVE: реальный Ollama, `embeddinggemma:300m` + выбранная
  `CHAT_MODEL`.
* `test.bat acceptance` — агрегатор; read-only аудит отдельно opt-in.
* `smoke_test.bat` — INT-сценарий, включая чат в обоих режимах и сравнение (через stub).
* `run_app.bat` — фактический запуск UI; панель чата.

Правило: режимы LIVE не запускаются автоматически; `MODEL_CHECK_KIND: LOCAL` только при
реальном вызове локальной модели.

---

## 6. LIVE-сценарии и измерения

* Один вопрос в режимах `without_rag` и `with_rag` с реальной локальной chat-моделью.
* Проверка внешней границы: `finish_reason`/`done_reason`, наличие/отсутствие usage,
  streaming, лимиты контекста — на реальном Ollama.
* 10 пар ответов (`harness/rag_eval.py --live`) при закреплённом `index_version_id`.
* Измеряются фактические `input_tokens`/`output_tokens` (из usage провайдера) и
  `output_tokens_per_second` (из `eval_count`/`eval_duration`); недоступные значения — «н/д».
* Токены проверяемой локальной модели не смешиваются с токенами OpenCode-агента.

---

## 7. Ручные сценарии, UI и сценарий видео

* `run_app.bat`: вопрос → «Без RAG» → ответ; «С RAG» → ответ + источники (`passed`);
  «Сравнить» → две колонки, шаблоны, usage, задержки.
* Индикатор ожидания и потоковый вывод (если поддерживается).
* Ошибки: отсутствие модели (`chat_model_missing` + подсказка), отсутствие индекса
  (`index_not_ready`), несовместимость (`index_incompatible`), переполнение
  (`context_overflow`), обрезка (`finish_reason=length`).
* Сценарий видео (кратко): запуск → выбор коллекции/индекса → вопрос без RAG → вопрос с RAG
  (источники) → сравнение → ошибка при отсутствии модели → обрезка/лимит (если воспроизводимо)
  → сохранённые прогоны. Видео/README — см. ACCEPTANCE §9.

---

## 8. Риски и снижение

| Риск | Снижение |
| --- | --- |
| `CHAT_MODEL` не установлена/не выбрана | приложение стартует; health и API дают `chat_model_missing` с подсказкой; автоскачивания нет |
| Медленная генерация / таймаут | `CHAT_TIMEOUT_SECONDS`; streaming; индикатор ожидания; таймаут → `chat_timeout` |
| Контекст не помещается из-за ошибочной оценки | консервативный `heuristic-v1`; резерв; явный `context_overflow`; обработка `chat_length_error` |
| `done_reason=length` скрыт | явные `truncated`/`finish_reason` в ответе, UI и прогоне |
| Отсутствие usage у некоторых версий Ollama | `usage=null`; значения не выдумываются; `prompt_tokens_actual=null` |
| Утечка prompt-injection из чанка | отдельный блок контекста, системная инструкция, UNIT-тесты обрамления |
| Ложная интерпретация валидной цитаты | явная пометка: валидная цитата ≠ смысловая правильность; раздельная оценка |
| Сравнение с разными настройками/индексом | пиннинг индекса и снимок identity/настроек; `same_model`/`same_settings` в отчёте |
| Тесты затрагивают пользовательскую БД | TEMP для `KNOWLEDGE_DB_PATH` и `CHAT_RUNS_PATH` в тестах/harness |
| Зависимость от реальной модели в CI | UNIT использует fake/stub; LIVE только opt-in |
| Изменение D21 поведения | retrieval вызывается как есть; существующие D21-тесты — регрессия |

---

## 9. Что нельзя проверить только чтением кода

Требуют фактического выполнения:

* доступность и версия Ollama для chat (`/api/version`, `/api/tags`, `/api/show`);
* реальный `done_reason`, usage и streaming `/api/chat`;
* лимиты контекста реальной модели;
* `output_tokens_per_second` (реальные `eval_count`/`eval_duration`);
* качество retrieval на пользовательском `agents-survey.pdf`;
* содержательная правильность ответов и валидность цитат (ручная оценка);
* UI в браузере (три действия, потоковый вывод, ошибки, источники);
* 10 пар ответов и сводная оценка;
* `.bat`-точки входа и сценарий видео.

Эти пункты не считаются выполненными по умолчанию и помечаются «не проверено» до прогона.

---

## 10. Трассировка P→D22

| Шаг | R-требования (SPEC §20) | D22 |
| --- | --- | --- |
| P-01 | R-01, R-02, R-03, R-16 | D22-01, D22-05 |
| P-02 | R-02, R-03, R-09, R-16 | D22-05, D22-07 |
| P-03 | R-02, R-03, R-15, R-16, R-19 | D22-05, D22-06 |
| P-04 | R-06, R-09, R-10 | D22-04, D22-07, D22-08 |
| P-05 | R-01, R-02, R-04, R-05, R-06, R-08, R-09 | D22-01, D22-02, D22-03, D22-04, D22-09 |
| P-06 | R-12, R-13, R-14 | D22-11, D22-13 |
| P-07 | R-02, R-03, R-05, R-13, R-14, R-16 | D22-03, D22-05, D22-11 |
| P-08 | R-07, R-08 | D22-12 |
| P-09 | R-01, R-11, R-12, R-17, R-18, R-19, R-20 | D22-10, D22-13, D22-14, D22-15 |
| P-10 | R-19, R-20 | D22-15 |

## P-11. Тестовый профиль
R-21…R-24: валидатор окружения; отдельный OpenAI-compatible adapter; lease runner вокруг live/acceptance; incremental transport; budget обеих ветвей; mocked regression и реальный local opt-in прогон. Embedding, D21 и существующие данные сохраняются.

SPEC_GATE_STATUS: PASS — отдельное архитектурное ревью дополнения R-21…R-24 выполнено до реализации. Product regression: selected-profile validation, inherited lease, success/error/interrupt cleanup, incremental SSE/NDJSON, plain budget; API test with loopback fake selected-provider added.
