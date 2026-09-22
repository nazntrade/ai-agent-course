# SPEC — Day 16: локальный MCP-агент

Идентификатор задачи: `day-16-mcp-agent`.
Статус: реализация по утверждённому плану Architect.

## 1. Назначение

Учебный проект, демонстрирующий работу прикладного агента с **настоящим
MCP-сервером** отдельным процессом: модель (локальный Qwen, OpenAI-compatible)
самостоятельно выбирает MCP-tool, приложение реально вызывает его по
Streamable HTTP и возвращает результат модели для финального ответа.

## 2. Границы

Входит:

* MCP-сервер отдельным процессом (loopback, собственный порт, Streamable HTTP);
* два read-only tool: `calculate` и `get_server_info`;
* CLI discovery по MCP;
* backend (FastAPI) с health/status/tools и SSE-чатом;
* браузерный чат без сборки и CDN;
* provider abstraction (локальный Qwen, OpenAI-compatible);
* live E2E `Qwen → MCP tool → Qwen`.

Не входит: VPS/SSH/nginx/systemd/домен/TLS, внешняя облачная модель как runtime,
БД, регистрация, OAuth, Docker, mutating tools, доступ MCP к shell/Git/ФС.

## 3. Компоненты

| Компонент | Файл | Ответственность |
| --- | --- | --- |
| MCP tools | `mcp_server/tools.py` | чистые функции `calculate`, `get_server_info`; SDK `ToolError` |
| MCP сервер | `mcp_server/server.py` | `MCPServer` поверх `mcp.server.MCPServer` (v2), регистрация tool, `python -m mcp_server`, exit 2 при занятом порту |
| Discovery CLI | `discovery_cli.py` | настоящее MCP-соединение, pagination, отчёт, exit 0/2 |
| Настройки | `agent/settings.py` | env/`.env`, значения по умолчанию, `model_configured`, ключ не попадает в `repr` |
| Граница MCP | `agent/mcp_adapter.py` | единственное место, знающее MCP; `McpTool`, `McpStatus`, `McpError`, `McpCallResult`, `SdkMcpClient.probe()` |
| Схемы | `agent/tool_schema.py` | MCP schema → OpenAI tools, sanitize/validate аргументов |
| Провайдер | `agent/provider.py` | `ModelProvider`, `OpenAICompatibleProvider`, sanitized ошибки |
| Оркестратор | `agent/orchestrator.py` | tool-call loop, `ChatEvent`, лимит раундов, per-session lock |
| Сессии | `agent/sessions.py` | in-memory история без БД |
| Trace | `agent/trace.py` | JSONL + sanitize |
| Backend | `agent/server.py`, `agent/__main__.py` | FastAPI, SSE, статика, `/openapi.json` |
| UI | `static/index.html`, `static/app.js`, `static/styles.css` | чат, loader, MCP status, ошибки |
| Harness | `harness/*.py` | smoke/E2E, lifecycle только своих процессов, run-каталог |

## 4. Контракты

### 4.1 MCP

* SDK: `mcp==2.2.0` (v2), транспорт Streamable HTTP, endpoint `/mcp`, только loopback.
* Клиент: высокоуровневый `mcp.Client` с `mode="auto"` (сначала `server/discover`,
  иначе — handshake `initialize`); согласованный protocol version — `2026-07-28`.
* `probe()` открывает **одну** сессию: handshake + полный `tools/list` за один вызов;
  `status()` делегирует в `probe()`, `list_tools()` остаётся для discovery/tests.
* Tools:
  * `calculate(operation: add|subtract|multiply|divide, a: number, b: number)`
    → structured `{operation, a, b, result}`; деление на ноль →
    SDK `ToolError("Division by zero is not allowed")`; `eval` не используется;
  * `get_server_info()` → `{name, version, status, uptime_seconds}`.
    Без env, путей, секретов.
* Сервер не имеет доступа к shell, Git и файловой системе.
* Bind-ошибка или занятый порт → сообщение и exit 2, чужой процесс не трогается.

### 4.2 Discovery CLI

```
python discovery_cli.py [--url URL] [--check] [--json]
```

Успех: `CONNECTED`, `PROTOCOL_VERSION`, `SERVER_INFO name=… version=…`,
`TOOLS_COUNT`, далее `TOOL`/`DESCRIPTION`/`INPUT_SCHEMA` на каждый tool.
Pagination выполняется до `next_cursor = None`; сессия всегда закрывается.
Ошибка: `CONNECTED: false`, `ERROR_CATEGORY: unreachable|timeout|protocol|invalid_response`,
`ERROR: <safe>`, exit 2. `--check` — тихий exit 0/2.

### 4.3 HTTP API

| Метод | Путь | Ответ |
| --- | --- | --- |
| GET | `/api/health` | `{status, service, version}` |
| GET | `/api/mcp/status` | `{connected, protocol_version, server, tools_count, error, checked_at}` |
| GET | `/api/mcp/tools` | `{connected, tools[], count, protocol_version, server, error}`; при недоступном MCP — HTTP 200 и `connected:false` |
| POST | `/api/chat/stream` | `text/event-stream` |
| GET | `/openapi.json` | OpenAPI 3.1 |
| GET | `/` | статика UI |

OpenAPI 3.1 — источник истины, снимок хранится в `docs/openapi.json`.

### 4.4 SSE

Формат: `event: <name>\ndata: <json>\n\n`, keep-alive `: ping` примерно раз в 15 с.

События: `status{request_id, stage}`, `delta{text}`, `tool_call{tool, arguments, round}`,
`tool_result{tool, ok, summary, duration_ms}`, `done{request_id, finish_reason, total_ms}`,
`error{request_id, category, message}`.

`category ∈ {model_not_configured, model_unreachable, model_timeout,
model_credentials_rejected, model_not_found, model_protocol_error,
model_http_error, mcp_unavailable, mcp_timeout, invalid_tool_arguments,
tool_round_limit, internal}`. При `error` событие `done` не отправляется.
Недоступный MCP определяется до вызова модели, а отсутствующая конфигурация
модели (`model_not_configured`) — до опроса MCP; в обоих случаях HTTP-запрос к
модели не выполняется. Сообщения санитизированы (без URL, ключей, заголовков);
для `model_not_found` допустимо имя модели.

### 4.5 UI

* Loader (spinner + `.stage`) создаётся сразу после user-пузыря, до `fetch`.
* `status`/`tool_call` обновляют только caption loader и копятся в одном
  `<details class="technical">` (закрыт по умолчанию, вне текста ответа).
* Первый `delta` удаляет loader и создаёт `.text`; `done`/`error` тоже удаляют
  loader. При `done` без текста пустой пузырь не остаётся.
* `error` — одно сообщение: backend-сообщение как есть; hint добавляется только
  для `model_not_configured` и `mcp_unavailable`.

### 4.6 Trace

JSONL, одна строка — одно событие: `request_start`, `mcp_connect`, `mcp_list_tools`,
`model_request{phase}`, `model_reported{requested_model, reported_model}` (только
при расхождении), `tool_selected`, `tool_completed`, `request_done`,
`request_error`. Запрещено писать: API-ключ, `Authorization`, env, system prompt,
полный текст пользователя (только `message_chars`), абсолютные пути.

## 5. Состояние

* История чата — только в памяти процесса backend, per-session `asyncio.Lock`.
* Trace — JSONL-файл, путь из `AGENT_TRACE_PATH`.
* БД нет. Секреты — только в окружении.

## 6. Конфигурация (defaults)

`AGENT_MODEL_BASE_URL=http://127.0.0.1:8080/v1`, `AGENT_MODEL_NAME=qwen3.8-27b-local`
(канонический id), `AGENT_MODEL_API_KEY_ENV=LOCAL_LLM_API_KEY`; если переменная
не задана или пуста — `model_configured=false` и `model_not_configured`
(никакого placeholder-ключа), `AGENT_MODEL_TIMEOUT_SECONDS=120`,
`MCP_SERVER_URL=http://127.0.0.1:8765/mcp`, `MCP_SERVER_HOST=127.0.0.1`,
`MCP_SERVER_PORT=8765`, `MCP_CONNECT_TIMEOUT_SECONDS=10`,
`MCP_CALL_TIMEOUT_SECONDS=30`, `BACKEND_HOST=127.0.0.1`, `BACKEND_PORT=8600`,
`PUBLIC_BACKEND_URL=http://127.0.0.1:8600`, `PUBLIC_FRONTEND_URL=http://127.0.0.1:8600`,
`AGENT_TRACE_PATH=logs/trace.jsonl`, `AGENT_LOG_LEVEL=INFO`. Только harness:
`RUN_LIVE_MCP`, `RUN_LIVE_LLM`, `RUN_UI_E2E`, `MCP_TEST_PORT=8766`,
`BACKEND_TEST_PORT=8601`.

`.env` не создаётся автоматически; в репозитории только `.env.example`.

## 7. Повторное использование

`qa/lib/config.py`, `qa/lib/discovery.py`, `qa/lib/local_llm.py`,
`qa/lib/live_session.py`, `qa/lib/browser.py`, `qa/lib/app_process.py`
подключаются через `harness/qa_bridge.py`. `qa/` не изменяется.
