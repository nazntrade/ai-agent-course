# Неделя 4 — MCP Agent

`week-04/day-16-mcp-agent` — независимый проект недели 4. В отличие от предыдущих недель,
это не чат поверх одной модели: здесь важна вторая половина архитектуры агента —
**как приложение реально отдаёт и исполняет инструменты по стандартному протоколу MCP**.

## День 16. Локальный MCP-агент

### Задача дня

Собрать учебного агента, который работает с **настоящим MCP-сервером, запущенным отдельным
процессом**: локальная модель сама выбирает MCP-инструмент, приложение реально вызывает его
по Streamable HTTP и возвращает результат модели для финального ответа.

Обязательная часть (граница дня):

- MCP-сервер отдельным процессом: loopback, свой порт, транспорт **Streamable HTTP**, endpoint `/mcp`;
- два **read-only** инструмента: `calculate` и `get_server_info`;
- CLI discovery: настоящее MCP-соединение, пагинация списка инструментов, отчёт и корректный exit code;
- backend (FastAPI) с `health`, статусом MCP, списком инструментов и SSE-чатом;
- браузерный чат без сборки и без CDN;
- провайдер модели как отдельная граница (локальный Qwen, OpenAI-compatible);
- live E2E: `Qwen → MCP-инструмент → Qwen`.

Вне обязательной части: VPS/SSH/nginx/systemd/домен/TLS, внешняя облачная модель как runtime,
БД, регистрация, OAuth, Docker, mutating tools, доступ MCP к shell/Git/файловой системе.

### Наше расширение

Кроме минимума дня проект содержит:

- **trace** в JSONL (`request_start`, `mcp_connect`, `mcp_list_tools`, `model_request`,
  `tool_selected`, `tool_completed`, `model_reported`, `request_done`, `request_error`) с санитайзером: в trace
  не попадают ключи, `Authorization`, env, system prompt, полный текст пользователя
  (пишется только число символов) и абсолютные пути;
- **harness** для автоматического lifecycle реальных процессов: сам поднимает MCP-сервер,
  backend и (при необходимости) локальную модель, ждёт readiness, останавливает **только свои**
  PID и складывает артефакты в `.runs/<timestamp>-<label>/`;
- **уровни проверок** UNIT / INT (реальный процесс + реальный HTTP) / LIVE (реальная модель) /
  UI (Playwright + системный браузер);
- **переиспользование** существующего общего runtime `qa/lib/*` через `harness/qa_bridge.py`
  (конфигурация локальной модели, launcher, readiness, браузер, порты) — каталог `qa/` не изменялся;
- **degraded-сценарии**: недоступный MCP, недоступная модель, timeout, невалидные аргументы
  инструмента, неизвестный инструмент, отсутствие инструмента в запросе.

### Архитектура

```
Browser (static/index.html, app.js, styles.css)
        │  HTTP + SSE (same origin)
        ▼
Host / backend  ── agent/server.py, agent/__main__.py  (FastAPI + uvicorn)
        │  /api/health  /api/mcp/status  /api/mcp/tools  /api/chat/stream
        │
        ├── agent/orchestrator.py   tool-call loop, ChatEvent, лимит раундов, per-session lock
        ├── agent/sessions.py       история в памяти процесса (без БД)
        ├── agent/trace.py          JSONL + sanitize
        │
        ├── MCP client boundary ── agent/mcp_adapter.py   ← единственное место, знающее MCP
        │        │  Streamable HTTP, endpoint /mcp
        │        ▼
        │   MCP server (отдельный процесс) ── mcp_server/server.py, mcp_server/__main__.py
        │        ├── calculate(operation, a, b)   add|subtract|multiply|divide
        │        └── get_server_info()            name, version, status, uptime_seconds
        │
        └── model boundary ── agent/provider.py
                 │  OpenAI-compatible HTTP
                 ▼
            локальный Qwen (127.0.0.1:8080/v1)
```

Ключевая граница: **только `agent/mcp_adapter.py` импортирует MCP SDK**. Доменная логика
работает с собственными типами (`McpTool`, `McpStatus`, `McpCallResult`, `McpError`), поэтому
замена транспорта не затрагивает оркестратор. Каждый запрос открывает короткую MCP-сессию:
discovery (`probe`) или вызов инструмента, а затем закрывает её; общих и кэшированных сессий нет.

### Место Qwen в архитектуре

Qwen — это **только поставщик решений**, а не исполнитель. Модель получает список инструментов
в формате OpenAI tools и возвращает `tool_call` с именем инструмента и аргументами.

**LLM сама не выполняет HTTP-вызовы.** Сетевой вызов MCP делает приложение
(`agent/mcp_adapter.py`), затем backend подставляет результат инструмента в диалог и просит
модель сформировать финальный ответ. Именно поэтому в trace видно два обращения к модели:
`model_request(tool_selection)` и `model_request(final_answer)`.

### Что реализовано

- MCP-сервер: `MCPServer` из MCP Python SDK v2, `python -m mcp_server`, Streamable HTTP, занятый порт → сообщение и exit 2 (чужой процесс не трогается);
- `calculate` возвращает structured-результат `{operation, a, b, result}`; деление на ноль —
  контролируемая `ToolError("Division by zero is not allowed")`, `eval` не используется;
- `get_server_info` не раскрывает env, пути и секреты;
- MCP-инструменты не имеют доступа к shell, Git и файловой системе;
- discovery CLI: `CONNECTED`, `PROTOCOL_VERSION`, `SERVER_INFO`, `TOOLS_COUNT` и на каждый
  инструмент `TOOL`/`DESCRIPTION`/`INPUT_SCHEMA`; пагинация до `next_cursor = None`, сессия
  всегда закрывается; ошибка → `ERROR_CATEGORY: unreachable|timeout|protocol|invalid_response` и exit 2;
- backend: `/api/health`, `/api/mcp/status`, `/api/mcp/tools` (при недоступном MCP — HTTP 200 и
  `connected: false`), `POST /api/chat/stream` (SSE), `/openapi.json` (OpenAPI 3.1), `/` — статика;
- SSE-события: `status`, `delta`, `tool_call`, `tool_result`, `done`, `error`; keep-alive `: ping`;
  при `error` событие `done` не отправляется; недоступный MCP определяется **до** вызова модели;
- UI (английский): поле ввода, `Send`, Enter — отправка, Shift+Enter — новая строка;
  в истории чата остаются только сообщения пользователя и финальные ответы ассистента:
  технические `status`/`tool_call`/`tool_result` обновляют loader и собираются в свёрнутый
  по умолчанию блок `Technical details` (raw JSON и вызовы инструментов не показываются в
  самом ответе), loader исчезает после первого текстового токена, финальный ответ выводится
  потоково; справа блок `MCP status` (connected, protocol version, tools count, раскрываемый
  список инструментов) и одна понятная ошибка на событие.

### Установка

```
cd week-04\day-16-mcp-agent
setup.bat
```

`setup.bat` создаёт `.venv` в каталоге проекта, ставит зависимости из `requirements.txt`
(идемпотентно: повторный запуск ничего не переустанавливает), при отсутствии `.env` печатает
подсказку и завершается кодом 0; ошибка установки — код 2. Глобальных установок нет.

Для чата с моделью нужен локальный `.env` с обязательным API key. Порядок первого запуска:

1. Скопировать `.env.example` в `.env` (`copy .env.example .env`).
2. Заполнить значение локального API key в `.env` (переменная `LOCAL_LLM_API_KEY`, если
   `AGENT_MODEL_API_KEY_ENV` не переопределён).
3. Не добавлять `.env` в Git: он уже указан в `.gitignore`; в репозитории хранится только
   `.env.example` без настоящих значений. Настоящий `.env` создаёт и заполняет только пользователь.
4. Запустить локальную модель Qwen (`127.0.0.1:8080/v1`).
5. Запустить `run_app.bat`.

Backend до первого обращения к модели проверяет обязательную конфигурацию. Если `.env` отсутствует
или ключ не задан, чат не отправляет заведомо неавторизованный запрос, а показывает одно сообщение:
«The model is not configured. Copy .env.example to .env and set the API key.». Неясного `401`
в этом случае пользователь не увидит.

### Переменные окружения

Значения секретов в репозитории не хранятся — `.env` в `.gitignore`, в репозитории только
`.env.example`. Ключ модели берётся из переменной, имя которой задаёт `AGENT_MODEL_API_KEY_ENV`.

| Переменная | Назначение | Значение по умолчанию |
| --- | --- | --- |
| `AGENT_MODEL_BASE_URL` | OpenAI-compatible endpoint локальной модели | `http://127.0.0.1:8080/v1` |
| `AGENT_MODEL_NAME` | Имя модели | `qwen3.8-27b-local` |
| `AGENT_MODEL_API_KEY_ENV` | Имя переменной с ключом модели | `LOCAL_LLM_API_KEY` |
| `LOCAL_LLM_API_KEY` | Значение ключа (placeholder для локального сервера) | `local-e2e` |
| `AGENT_MODEL_TIMEOUT_SECONDS` | Таймаут обращения к модели | `120` |
| `MCP_SERVER_URL` | Endpoint MCP | `http://127.0.0.1:8765/mcp` |
| `MCP_SERVER_HOST` / `MCP_SERVER_PORT` | Адрес и порт MCP-сервера | `127.0.0.1` / `8765` |
| `MCP_CONNECT_TIMEOUT_SECONDS` | Таймаут подключения к MCP | `10` |
| `MCP_CALL_TIMEOUT_SECONDS` | Таймаут вызова инструмента | `30` |
| `BACKEND_HOST` / `BACKEND_PORT` | Адрес и порт backend | `127.0.0.1` / `8600` |
| `PUBLIC_BACKEND_URL` / `PUBLIC_FRONTEND_URL` | Публичные адреса (задел под VPS) | `http://127.0.0.1:8600` |
| `AGENT_TRACE_PATH` | Путь JSONL-trace | `logs/trace.jsonl` |
| `AGENT_LOG_LEVEL` | Уровень логирования | `INFO` |

Только для harness: `RUN_LIVE_MCP`, `RUN_LIVE_LLM`, `RUN_UI_E2E`, `MCP_TEST_PORT`,
`BACKEND_TEST_PORT`.

Канонический id модели для Day 16 — `qwen3.8-27b-local` (значение по умолчанию в
`agent/settings.py` и в `.env.example`). Приложение и harness всегда запрашивают именно его;
имя модели из локального конфига общей QA-инфраструктуры фиксируется только как диагностическое
поле `qa_model_configured` и как requested model не используется. Trace различает requested
(configured) и server-reported имя модели: при расхождении пишется событие `model_reported`
с обоими значениями, а поле `reported_model` попадает в `request_done`. Если в вашем реальном
`.env` осталось старое имя, измените переменную `AGENT_MODEL_NAME` на `qwen3.8-27b-local`
(значение ключа не показывайте и не коммитьте).

### Команды запуска

```
run_app.bat            :: MCP-сервер + backend + UI в браузере (режим all)
run_app.bat mcp        :: только MCP-сервер в текущем окне
run_app.bat backend    :: только backend
run_app.bat tools      :: discovery CLI по MCP
```

Порты: MCP `127.0.0.1:8765`, backend `127.0.0.1:8600`. Если MCP уже отвечает, `run_app.bat all`
его не перезапускает. По завершении останавливаются только процессы, запущенные самим скриптом;
занятый порт → сообщение и exit 2.

### Команды проверок

```
SETUP:        setup.bat                    (создать .venv и поставить зависимости)
UNIT:         test.bat                     (unit + in-process MCP, без сети)
MCP SMOKE:    smoke_test.bat               (реальный MCP по HTTP + реальный backend + CLI)
LIVE LLM E2E: test.bat live                (нужен локальный Qwen)
LIVE + UI:    test.bat live ui             (плюс Playwright и системный браузер)
NEGATIVE UI:  .venv\Scripts\python.exe harness\mcp_unavailable_e2e.py
                                           (ручной standalone-запуск: реальный Chrome/Edge,
                                           backend без MCP)
ACCEPTANCE:   test.bat acceptance          (unit → MCP-unavailable browser E2E →
                                           live MCP → live E2E)
OPENAPI:      test.bat openapi             (обновить docs/openapi.json)
REGRESSION:   qa\run_local_e2e.bat TESTS   (из корня репозитория; тесты общего QA-runtime)
```

Артефакты каждого прогона harness: `.runs/<timestamp>-<label>/` — `trace.jsonl`,
`mcp_server.log`, `backend.log`, `report.json`, `screenshots/`.

### Фактически проверенные версии

Окружение: Windows 11, Python 3.12, локальный venv `.venv` внутри проекта.

Прямые зависимости (`requirements.txt`, закреплены точными `==` после первого успешного
`setup.bat`; транзитивные не пиннигованы):

| Пакет | Версия |
| --- | --- |
| mcp | 2.2.0 |
| mcp-types | 2.2.0 |
| fastapi | 0.141.1 |
| uvicorn | 0.53.0 |
| openai | 3.17.0 |
| python-dotenv | 1.2.3 |
| httpx | 0.28.1 |
| playwright | 1.63.0 |

`mcp-types==2.2.0` закреплён прямой зависимостью: `agent/mcp_adapter.py` делает
`from mcp_types import REQUEST_TIMEOUT`, поэтому пакет не должен зависеть от того, что его
принесёт транзитивно `mcp==2.2.0` (хотя совместимо: `mcp==2.2.0` требует `mcp-types==2.2.0`).

Проект мигрирован на стабильный MCP Python SDK v2: клиент использует `mcp.Client`
(Streamable HTTP), сервер — `mcp.server.MCPServer` с транспортом
`run(transport="streamable-http", host=..., port=...)`. Фактически согласованная
protocol version при соединении v2-клиента и v2-сервера — `2026-07-28` (в коде не хардкодится
и сверяется с константой установленного SDK).

### Результаты проверок

| Проверка | Команда | Результат |
| --- | --- | --- |
| Установка окружения | `setup.bat` (дважды) | exit 0; повторный запуск идемпотентен, ставит `mcp==2.2.0` и `mcp-types==2.2.0` |
| UNIT + in-process MCP | `test.bat` | 207 тестов OK, 25 skipped (`UNIT_STATUS: PASS`), exit 0 |
| Реальный MCP по HTTP | `smoke_test.bat` | 11 сценариев OK (`MCP_INTEGRATION_STATUS: PASS`), один `probe` на запрос, exit 0 |
| Реальный backend по HTTP | `smoke_test.bat` | 14 сценариев OK (`BACKEND_INTEGRATION_STATUS: PASS`), exit 0 |
| Discovery CLI | `run_app.bat tools` (без сервера) | `CONNECTED: false`, `ERROR_CATEGORY: unreachable`, exit 2 |
| Discovery CLI | `discovery_cli.py` против реального MCP | `CONNECTED`, `PROTOCOL_VERSION: 2026-07-28`, `SERVER_INFO`, `TOOLS_COUNT: 2`, exit 0 |
| Live Qwen E2E | `test.bat live` | `LIVE_LLM_STATUS: PASS`, exit 0 |
| UI (Playwright + msedge) | `test.bat live ui` | `UI_E2E_STATUS: PASS`, exit 0 |
| UI negative (MCP недоступен) | шаг внутри `test.bat acceptance` (также standalone `.venv\Scripts\python.exe harness\mcp_unavailable_e2e.py`) | `MCP_UNAVAILABLE_UI_STATUS: PASS`, `TRACE_STATUS: PASS`, exit 0; реальный msedge, 1 ошибка на запрос, trace без `model_request`/`tool_selected`/`tool_completed` |
| Acceptance | `test.bat acceptance` | unit 207 PASS → MCP-unavailable UI PASS → live MCP PASS → live E2E PASS, exit 0 |
| Regression общего QA-runtime | `qa\run_local_e2e.bat TESTS` (из корня репозитория) | 239 тестов OK, `OK: all qa tests passed.`, exit 0 |

Шаг «MCP-unavailable browser E2E» в acceptance поднимает **только backend** (URL MCP указывает на
свободный loopback-порт, слушателя нет), открывает реальный системный Chromium (msedge/chrome)
через Playwright и проверяет негативный UI-сценарий. При недоступном MCP пользователь видит
ровно одно сообщение об ошибке — например
`The MCP server is not reachable (127.0.0.1:8791) Start it (run_app.bat) and try again.`:
понятно, что недоступен именно MCP, есть действие (запустить сервер), нет raw JSON, внутренних
`Status:`/`tool_call`, traceback, полного URL и секретов. Pill в блоке `MCP status` — `disconnected`
(класс `pill bad`), композер остаётся доступным, повторная отправка добавляет ровно одну новую
ошибку. Trace запроса содержит `mcp_connect(ok:false)` и `request_error(category=mcp_unavailable)`
и не содержит обращений к модели — сбой MCP определяется до вызова модели.

25 skipped в unit-режиме — это integration-модули, которые требуют реальных процессов и
включаются только при `RUN_LIVE_MCP=1`; в обычном прогоне они не обращаются ни к сети, ни к модели.

### Сценарий демонстрации

1. `setup.bat`, затем `run_app.bat` — в браузере открывается чат, справа блок `MCP status`
   с `connected` и списком из двух инструментов.
2. Ввести `What is 23 multiplied by 17?` и нажать Enter.
3. Ожидаемо: сразу появляется loader; технические `status`/`tool_call`/`tool_result` обновляют
   его caption, но в историю чата не попадают; с первым текстовым токеном loader исчезает и
   потоково появляется финальный ответ по существу (например, `23 × 17 = 391`). В истории
   остаются только сообщения пользователя и ответы ассистента.
4. Что реально происходит внутри: модель выбирает `calculate` → приложение вызывает
   `calculate(operation=multiply, a=23, b=17)` по MCP → tool result `391` возвращается модели →
   модель формирует финальный потоковый ответ. Эти шаги при необходимости видны в свёрнутом блоке
   `Technical details` под ответом (raw JSON — только при осознанном раскрытии) и в trace.
5. Остановить MCP-сервер и повторить запрос: появляется ровно одно понятное сообщение про
   недоступный MCP с действием «запустить сервер» (например
   `The MCP server is not reachable (127.0.0.1:8791) Start it (run_app.bat) and try again.`),
   без внутренних деталей и дублей. Нажать `Refresh` в блоке MCP status: `disconnected` и
   категория ошибки.

Доказательство выбора инструмента видно в trace: `model_request(tool_selection)` →
`tool_selected(calculate, …)` → `tool_completed(ok, result 391)` →
`model_request(final_answer)` → `request_done(ok)`.

### Troubleshooting

| Симптом | Причина и действие |
| --- | --- |
| `setup.bat` завершился кодом 2 | Нет Python 3 в `PATH` или не удалось скачать зависимости: проверьте `py -3 --version` и сеть, затем запустите снова |
| `run_app.bat` сообщает, что порт занят (exit 2) | Порт используют другой процесс: остановите его самостоятельно или измените `MCP_SERVER_PORT`/`BACKEND_PORT`. Чужие процессы скрипт не завершает |
| В блоке `MCP status` — `disconnected` | MCP-сервер не запущен: выполните `run_app.bat mcp` в отдельном окне или перезапустите `run_app.bat` |
| `LIVE_LLM_STATUS: BLOCKED` | Локальная модель не отвечает и не была запущена: проверьте `AGENT_MODEL_BASE_URL` / `QA_LOCAL_LLM_*` и доступность локального сервера модели |
| `The model is not configured.` | Нет `.env` или пустой API key: скопируйте `.env.example` в `.env` и заполните `LOCAL_LLM_API_KEY`, затем перезапустите backend |
| `model_unreachable` в чате | Приложение работает, но endpoint модели недоступен (connection refused/network): запросы к MCP при этом продолжают работать |
| `model_credentials_rejected` в чате | Endpoint ответил `401/403`: проверьте API key в `.env`; это отдельная категория и не смешивается с unreachable |
| `model_timeout` / `model_not_found` / `model_protocol_error` / `model_http_error` | Соответственно timeout, неизвестное имя модели, некорректный ответ провайдера и прочая HTTP-ошибка; на одно событие приходится одно сообщение |
| `UI_E2E_STATUS: MANUAL_REQUIRED` / `BLOCKED` | Не установлен системный браузер для Playwright: выполните ручную проверку из шага 5 сценария демонстрации; API-часть проверяется отдельно |
| В ответе нет вызова инструмента | Модель не выбрала инструмент: повторите запрос, сформулировав его как явную арифметическую задачу |

### Security notes

- Секреты только в `.env` (в `.gitignore`); в репозитории только `.env.example` без настоящих значений.
  Настоящий `.env` отсутствует в `git status` и в артефактах прогонов; его создаёт и заполняет
  только пользователь, агенты его не читают и не изменяют.
- Сообщения об ошибках модели не содержат API key, `Authorization` и URL endpoint: одно событие
  даёт одну понятную категорию (`model_not_configured` / `model_unreachable` / `model_timeout` /
  `model_credentials_rejected` / `model_not_found` / `model_protocol_error` / `model_http_error`).
- Trace санитизируется: не пишутся API-ключи, `Authorization`, env, system prompt, полный текст
  пользователя и абсолютные пути.
- В ответах API и в DOM не появляются учётные данные и заголовки авторизации.
- MCP-инструменты read-only и не имеют доступа к shell, Git и файловой системе; `eval` не используется.
- Весь трафик модели и MCP остаётся на loopback; внешняя облачная модель как runtime не используется.
- Публичный интернет-сервис в рамках дня не разворачивается; доступ к VPS/SSH/nginx/systemd не выполнялся.

### Ограничения

- История чата хранится только в памяти backend: перезапуск процесса очищает сессии. БД нет.
- Процессы запускаются на loopback и не предназначены для внешнего доступа в текущем виде.
- Harness рассчитан на Windows (`taskkill`, `CREATE_NO_WINDOW`, `powershell`) и не портирован на Linux.
- Обязательный уровень дня — один Host, один MCP-сервер и один пользователь; multi-user изоляции
  и rate limiting нет.
- `run_app.bat all` требует интерактивного окна (пауза до нажатия клавиши), поэтому проверяется
  вручную; автоматически проверяются те же процессы через harness.
- Канонический id модели — `qwen3.8-27b-local`; trace различает requested (configured) и
  server-reported имя и пишет `model_reported` только при расхождении.

### Следующий этап: VPS deployment

VPS-развёртывание **не выполнялось** и намеренно не входит в Day 16. Как следующий этап
проект уже подготовлен частично: конфигурация вынесена в переменные окружения
(включая `PUBLIC_BACKEND_URL`/`PUBLIC_FRONTEND_URL`), UI работает same-origin и обращается к API
по относительным путям, есть `/api/health` для health-check, SSE отправляется с заголовком
`X-Accel-Buffering: no`, а провайдер модели отделён от логики агента.

Чего для VPS пока нет и что придётся добавить отдельной задачей: TLS/домен, reverse proxy,
процессный менеджер, аутентификация и ограничение доступа, персистентность истории,
мультипользовательская изоляция, ограничения частоты запросов и портирование harness на Linux.
