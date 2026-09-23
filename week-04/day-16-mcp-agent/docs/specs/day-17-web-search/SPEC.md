# SPEC — Day 17: веб-поиск в MCP-агенте

Идентификатор задачи: `day-17-web-search`.
Статус: реализация выполнена по утверждённому архитектурному решению Architect
и явному заданию пользователя.

## 1. Назначение

Добавить в существующий MCP-сервер (день 16) третий read-only tool
`search_web` вокруг внешнего поискового API Tavily Search и добиться его
реального вызова агентом из браузерного чата: модель сама выбирает
`search_web`, приложение реально вызывает его по MCP, возвращает модели
компактные сниппеты, и финальный ответ содержит ссылки на источники.

## 2. Границы

Входит:

* `search_web` в MCP-сервере: обязательный `query`, опциональный `max_results`,
  structured-результат `title`/`url`/`description`;
* конфигурация только для процесса MCP-сервера (ключ не покидает его);
* обработка пустой выдачи, отсутствия ключа, тайм-аута и ошибок API без
  вымышленных ссылок и без утечки секретов;
* сохранение архитектуры дня 16: отдельный MCP-процесс, Streamable HTTP,
  backend MCP-клиент, потоковый чат, loader, закрытые Technical details;
* работа и с локальным Qwen, и с DeepSeek через существующий `ModelProvider`
  (модельную часть ради поиска не перестраиваем).

Не входит: база данных, mutating tools, VPS/SSH/nginx, изменение контрактов
`agent/provider.py`, `agent/mcp_adapter.py`, `agent/server.py`,
`agent/tool_schema.py`, `static/*`, `requirements.txt`, `docs/openapi.json`,
`smoke_test.bat`, `run_app.bat`.

## 3. Компоненты

| Компонент | Файл | Ответственность |
| --- | --- | --- |
| Конфиг поиска | `mcp_server/config.py` | `SearchConfig`, собственный `load_dotenv_if_present`, `resolve_search_config`; ключ вне `repr` |
| Клиент/сервис | `mcp_server/web_search.py` | `HttpResponse`, `Transport`/`UrllibTransport`, `TransportError`, `SearchError`, `WebSearchService`, ленивый `default_service`; единственное место с сетью |
| Tool | `mcp_server/tools.py` | `search_web(query, max_results=0)`, валидация, `SearchError → ToolError`; `calculate`/`get_server_info` не изменены |
| Сервер | `mcp_server/server.py` | регистрация `search_web` поверх SDK v2 |
| Промпт | `agent/orchestrator.py` | текст `SYSTEM_PROMPT`: использовать `search_web`, не утверждать о прочтении страниц, давать ссылки |
| Фейк поиска | `tests/support/fake_search.py` | loopback-сервер, корректный заголовок, маркеры граничных случаев |
| Harness | `harness/live_e2e.py`, `harness/live_mcp.py`, `harness/acceptance.py`, `harness/tavily_live.py` | LIVE/UI-сценарии (фейк), агрегированный acceptance и реальный Tavily (opt-in) |

## 4. Контракт `search_web`

* `query: str` — required, непустая строка после `strip`; длиннее 600 символов —
  усечение. Иначе `ToolError("Argument 'query' must be a non-empty string")`.
* `max_results: int = 0` — не `int | None` (иначе тип теряется при санитайзе
  схемы). `0` — серверный default; `≤ 0` — default; больше cap — cap;
  не-int (в том числе `bool`) — `ToolError("Argument 'max_results' must be an
  integer")`.
* Успех: `{"query", "count", "results": [{"title", "url", "description"}],
  "more_results_available", "note": "Snippets only; the pages were not
  opened."}`.
* Пусто: `{"query", "count": 0, "results": [],
  "more_results_available": false, "note": "No results found for this query."}`
  — успех, не ошибка.
* `more_results_available` — **всегда `false`**, и при непустой, и при пустой
  выдаче. Tavily не отдаёт эквивалентного признака, поэтому контракт дня 16
  сохраняется осознанно, а значение не выводится из `count`/`max_results`.
* Ошибки — только `ToolError` с санитизированным текстом (без ключа, заголовков,
  тела ответа и полного URL):
  * нет ключа → `Web search is not configured on this server (the search API key
    is missing). Do not invent results.` (транспорт не вызывается);
  * timeout → `The web search request timed out.`;
  * 401/403/432/433 → `The web search service rejected the request (HTTP
    <status>).`;
  * 429 → `The web search rate limit was reached (HTTP 429).`;
  * 400/422 и любой прочий не-200 → `The web search service failed (HTTP
    <status>).`;
  * connect/DNS → `The web search service is unreachable.`;
  * не JSON / нет `results[]` → `The web search service returned an unexpected
    response.`.
* Санитизация: whitespace склеивается; `title ≤ 200`, `description ≤ 300`;
  число результатов `≤ cap` (default 5, максимум 10); URL только `http`/`https`;
  если после фильтрации пусто — пустой успех; непустая строка `query` из ответа
  эхом попадает в результат, иначе остаётся нормализованный запрос.
* Ошибка API никогда не превращается в выдуманный результат.

## 5. Внешний API Tavily

`POST https://api.tavily.com/search`, заголовки `Authorization: Bearer <key>` и
`Accept: application/json`, JSON-тело `query` (обязателен, ≤ 600 символов),
`search_depth=basic`, `max_results`. Ответ 200: необязательное эхо `query`
(строка) и `results[]` с `title`, `url`, `content` (может быть пустым),
`score`; `content` отображается в `description`. Ошибки: 401/403/432/433
(доступ/квота), 429 (rate limit), 400/422 (валидация) и прочий не-200. Ретраи
не выполняются. Тело ответа и ключ никогда не попадают в текст ошибки или лог.

## 6. Конфигурация (читает только MCP-сервер)

| Переменная | Default | Поведение |
| --- | --- | --- |
| `MCP_SEARCH_API_KEY_ENV` | `TAVILY_API_KEY` | имя переменной с ключом |
| `TAVILY_API_KEY` | пусто | пусто → `not_configured` |
| `MCP_SEARCH_BASE_URL` | `https://api.tavily.com` | не http(s) → default |
| `MCP_SEARCH_TIMEOUT_SECONDS` | `10.0` | clamp 1..25 |
| `MCP_SEARCH_MAX_RESULTS` | `5` | clamp 1..10 |
| `MCP_LOAD_DOTENV` | `1` | `0` — не читать `.env` |

Конфиг резолвится один раз при первом вызове tool и кэшируется; при
`tools/list` сервис не создаётся. `.env` ищется по пути
`Path(__file__).resolve().parent.parent / ".env"`, `override=False`, значения
process env приоритетнее.

## 7. Безопасность

* Ключ хранится только в процессе MCP-сервера; в модель, UI, ответы, trace,
  логи и `repr` он не попадает. Backend ключ поиска не читает и нигде не
  использует и не выводит: при `run_app.bat` оба процесса видят один и тот же
  `.env`, но значение поискового ключа обрабатывает только MCP-сервер.
* LIVE-сценарий дополнительно проверяет `key_isolation`: фейковый ключ
  отсутствует в SSE-тексте, `trace.jsonl`, `mcp_server.log` и `backend.log`.
* Модель получает только `title`/`url`/`description` и не может открыть
  страницы: результат — сниппеты, а не их полный текст.
* Промпт запрещает утверждать о прочтении страниц и выдумывать факты, цитаты и
  ссылки, которых не было в tool-результате.
* Тесты изолированы от сети и реального `.env`; реальный Tavily вызывается
  только отдельным opt-in режимом.

## 8. Маршрут проверки

Новые режимы в `test.bat` не добавляются (файл защищён permission-правилом
`edit: **/*.bat = deny`). Поиск проверяется существующими доверенными точками
входа:

* `test.bat` — UNIT и in-process MCP;
* `smoke_test.bat` — INT: реальный MCP-процесс + реальный HTTP + loopback
  `FakeSearchServer` (`SEARCH_INTEGRATION_STATUS`);
* `test.bat acceptance` — LIVE и UI: шаг live search E2E запускает
  `harness/live_e2e.py --scenario search --ui` и печатает `SEARCH_LIVE_STATUS` и
  `SEARCH_UI_STATUS`; отсутствие браузера даёт `SEARCH_UI_STATUS: BLOCKED` и не
  роняет acceptance.

Реальный Tavily — отдельный ручной opt-in
`.venv\Scripts\python.exe harness\tavily_live.py` (требует `TAVILY_LIVE_ALLOW=1`);
без ключа/разрешения он возвращает `TAVILY_STATUS: BLOCKED`.

