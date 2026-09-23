# PLAN — Day 17: веб-поиск в MCP-агенте

План реализации утверждён Architect и выполняется явным заданием пользователя.
Каждый пункт трассируется к критериям приёмки из `ACCEPTANCE.md`.

## 1. Новые и изменённые файлы

```
mcp_server/config.py          NEW  SearchConfig, dotenv, resolve_search_config
mcp_server/web_search.py      NEW  Transport, WebSearchService, default_service
mcp_server/tools.py           EDIT search_web, валидация, SearchError → ToolError
mcp_server/server.py          EDIT регистрация search_web, docstrings
agent/orchestrator.py         EDIT текст SYSTEM_PROMPT (логика не меняется)
.env.example                  EDIT блок MCP_SEARCH_*, TAVILY_API_KEY
docs/specs/day-17-web-search/{SPEC,PLAN,ACCEPTANCE}.md   NEW
tests/support/fake_search.py  NEW  FakeSearchServer, FAKE_API_KEY, маркеры
tests/test_web_search.py      NEW  UNIT конфиг/сервис/ошибки/санитизация
tests/test_tools.py           EDIT тесты search_web
tests/test_mcp_inprocess.py   EDIT search_web в tools/list и structured/is_error
tests/test_orchestrator.py    EDIT тест текста SYSTEM_PROMPT
tests/test_harness.py         EDIT SEARCH_TRACE_CHAIN, tools-list, SSE и key_isolation
tests/integration/test_search_live.py   NEW  INT (RUN_LIVE_MCP=1)
harness/live_e2e.py           EDIT --scenario {arithmetic,search}, UI anchors, key isolation
harness/live_mcp.py           EDIT FakeSearchServer + SEARCH_INTEGRATION_STATUS
harness/acceptance.py         EDIT шаг live search E2E (LIVE + UI)
harness/tavily_live.py        NEW  REAL opt-in (ручной standalone)
```

## 2. Порядок реализации

1. `mcp_server/config.py` → `mcp_server/web_search.py` → `tools.search_web` →
   регистрация в `mcp_server/server.py`.
2. Текст `SYSTEM_PROMPT`.
3. `tests/support/fake_search.py` → unit-тесты → интеграционный тест.
4. Harness:    `live_e2e --scenario search`, `live_mcp`, `acceptance` (шаг search),
   `tavily_live`.
5. Документация и `.env.example`.

## 3. Ключевые решения

* **Изоляция ключа.** Ключ читается и используется только в процессе
  MCP-сервера. Backend, оркестратор, trace и UI о нём не знают; в модель уходит
  лишь structured-результат. `SearchConfig.api_key` объявлен с `repr=False`.
* **Сеть в одном модуле.** `mcp_server/web_search.py` — единственное место, где
  выполняется исходящий HTTP; `Transport` инъектируется, что делает возможным
  unit-тесты без сети.
* **Один запрос, без ретраев.** Как в решении Architect: одна попытка на вызов.
* **Ошибка — не результат.** Любой сбой API/транспорта превращается в
  `SearchError` с фиксированной безопасной фразой; выдуманных ссылок нет.
* **Пустая выдача — успех** с `count: 0` и честной формулировкой.
* **Схема.** `max_results: int = 0`, а не `int | None`, чтобы SDK сохранил
  `integer` в JSON-Schema после санитайза.
* **Ленивый сервис.** `default_service()` строит и кэширует сервис при первом
  вызове; `tools/list` не читает конфигурацию и не создаёт сервис.
* **Изоляция тестов.** Unit-тесты передают `dotenv=False` и подменяют
  транспорт. Интеграционный и LIVE-сценарии запускают MCP-ребёнка с
  `MCP_LOAD_DOTENV=0` и фейковым ключом, указывая на loopback
  `FakeSearchServer`.
* **Совместимость дня 16.** `calculate`/`get_server_info`, SSE-поток, trace и UI
  не меняются. `verify_trace(records, request_id)` получил default-параметр
  `chain`, поэтому вызов из дня 16 и его тесты работают без изменений. Дефолтный
  сценарий `live_e2e` — `arithmetic`.

## 4. Тесты

* UNIT: `tests/test_web_search.py` (конфиг, defaults, override, clamp,
  `MCP_LOAD_DOTENV`, `repr`, форма запроса, успех/пусто/malformed, ошибки,
  санитизация, пустой ключ ⇒ транспорт не вызван), `tests/test_tools.py`,
  `tests/test_mcp_inprocess.py`.
* INT: `tests/integration/test_search_live.py` (реальный MCP-процесс,
  реальный HTTP, `FakeSearchServer`: успех/пусто/401/429/timeout, нет ключа в
  результате), запускается `harness/live_mcp.py` и `smoke_test.bat`.
* LIVE: `harness/live_e2e.py --scenario search` — реальный Qwen, trace-цепочка с
  `tool_selected(search_web)`, ответ содержит ≥ 2 URL из tool-результата;
  `harness/acceptance.py` дополнительно проверяет `key_isolation` по
  SSE-тексту, `trace.jsonl`, `mcp_server.log`, `backend.log`.
* UI: тот же harness с `--ui` — anchors ≥ 2, loader, закрытый Technical details.
* REAL: `harness/tavily_live.py` — opt-in, без разрешения/ключа → `BLOCKED`.

Пост-review уточнение: результаты `FakeSearchServer` сделаны осмысленными для
самого вопроса («Python Official Documentation» и т. п.), URL остаются
детерминированными `https://docs.example.test/1..3`. Без этого локальная модель
иногда считала сниппеты заглушками и не ссылалась на них, из-за чего D17-10
был недетерминированным. Сама проверка не ослаблена: по-прежнему требуются
≥ 2 URL из tool-результата в ответе.

## 5. Точки входа и маршрут проверки

Новые режимы в `test.bat` **не добавляются**: файл защищён permission-правилом
(`edit: **/*.bat = deny`). LIVE- и UI-проверка поиска заведена в существующий
Python-harness `harness/acceptance.py`, который уже запускается доверенным
`test.bat acceptance`. Маршрут проверки:

```
test.bat                 unit + in-process MCP (UNIT)
smoke_test.bat           harness/live_mcp.py (INT, SEARCH_INTEGRATION_STATUS)
test.bat acceptance      harness/acceptance.py: unit → MCP-unavailable UI →
                         live MCP → live arithmetic E2E → live search E2E
                         (LIVE_LLM_STATUS, SEARCH_LIVE_STATUS, SEARCH_UI_STATUS)
test.bat live [ui]       регрессия дня 16 (arithmetic), без изменений
```

Шаг «live search E2E» в `harness/acceptance.py` запускает
`harness/live_e2e.py --scenario search --ui` с `RUN_LIVE_LLM=1` и теми же
browser-env ключами, что и MCP-unavailable шаг. Отсутствие системного браузера
не роняет acceptance: `live_e2e` отдаёт `UI_E2E_STATUS: BLOCKED`, а код возврата
определяется LLM-частью. Недоступная модель → prerequisite (exit 2).

`harness/tavily_live.py` — отдельный ручной opt-in (реальный Tavily), в `test.bat`
он не добавляется:

```
.venv\Scripts\python.exe harness\tavily_live.py   (требует TAVILY_LIVE_ALLOW=1)
```

## 6. Отклонения

Согласованное отступление от первоначального п.5 плана: правка `test.bat`
невозможна (protected `.bat`), поэтому режимы `search`/`tavily` не добавляются.
LIVE+UI-проверка поиска перенесена в `harness/acceptance.py`; реальный Tavily
остаётся standalone-скриптом. Остальные решения утверждённого плана выполнены
без функциональных отклонений.

## 7. Миграция провайдера поиска (день 17, решение Architect)

Первоначальная реализация дня 17 использовала Brave Search API. По решению
Architect провайдер заменён на Tavily Search API: `Transport` из одного метода
`get` заменён на `post_json`, `SEARCH_PATH = "/search"`, тело
`{query, search_depth, max_results}`, ответ `query` + `results[].content`.
Внешний контракт `search_web`, браузерный чат, потоковый ответ, Technical
details, `calculate` и `get_server_info` сохранены. Единственное осознанное
изменение контракта — `more_results_available` всегда `false`, поскольку Tavily
не отдаёт эквивалентного признака.
