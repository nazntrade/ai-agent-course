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

## День 17. Веб-поиск в MCP-агенте

### Задача дня

Добавить в **существующий MCP-сервер** собственный инструмент поиска в интернете вокруг
внешнего поискового API **Tavily Search** и добиться его реального вызова агентом из
браузерного чата. Архитектура дня 16 сохраняется: тот же отдельный MCP-сервер на Streamable
HTTP, тот же backend с MCP-клиентом и потоковым чатом, те же `calculate` и `get_server_info`.

Обязательная часть (граница дня):

- третий MCP-инструмент `search_web` в том же сервере (запрос + лимит результатов);
- компактный **structured**-результат: названия, ссылки и краткие описания;
- ключ внешнего API хранится только на сервере;
- обработаны пустая выдача, отсутствие ключа, тайм-аут и ошибки API — без вымышленных
  ссылок и без утечки секретов;
- по «найди в интернете …» агент даёт несколько ссылок с пояснениями, для свежих данных
  использует поиск, и не утверждает, что прочитал страницы целиком, если получил только
  поисковые описания (snippets);
- новый инструмент работает и с локальным Qwen, и с DeepSeek на VPS через существующий
  интерфейс `ModelProvider`; модельная часть ради поиска не перестраивалась.

Вне границы дня: развёртывание на VPS, реальные платные вызовы Tavily в автотестах,
mutating tools, доступ MCP к shell/Git/файловой системе.

### Что реализовано

| Файл | Назначение |
| --- | --- |
| `mcp_server/config.py` | `SearchConfig` и `resolve_search_config`; ключ с `repr=False`; `.env` читает только MCP-сервер; гейт `MCP_LOAD_DOTENV` |
| `mcp_server/web_search.py` | **единственная граница сети**: `Transport`/`UrllibTransport` (stdlib), `WebSearchService`, `SearchError`; `POST /search` к Tavily с `Authorization: Bearer`, санитизация ответа и лимиты |
| `mcp_server/tools.py` | тонкий `search_web(query, max_results=0)`; `calculate` и `get_server_info` не изменены |
| `mcp_server/server.py` | регистрация третьего инструмента |
| `agent/orchestrator.py` | изменён **только текст** `SYSTEM_PROMPT`: использовать `search_web` для свежих данных, давать ссылки, не выдумывать факты и не утверждать о прочтении страниц |
| `.env.example` | блок `MCP_SEARCH_*` (ключ пустой, без плейсхолдера) |
| `docs/specs/day-17-web-search/` | SPEC.md, PLAN.md, ACCEPTANCE.md (критерии D17-01…D17-15) |
| `tests/test_web_search.py`, `tests/support/fake_search.py`, `tests/integration/test_search_live.py` | unit-тесты поиска, loopback-фейк поискового API и интеграционный сценарий |
| `harness/live_mcp.py` | `SEARCH_INTEGRATION_STATUS` поверх фейкового поискового сервера |
| `harness/live_e2e.py` | `--scenario {arithmetic,search}` (по умолчанию arithmetic — поведение дня 16) |
| `harness/acceptance.py` | шаг «live search E2E» (реальный Qwen + UI) со статусами `SEARCH_LIVE_STATUS` / `SEARCH_UI_STATUS` |
| `harness/tavily_live.py` | реальный Tavily только opt-in (`TAVILY_LIVE_ALLOW=1`), иначе `TAVILY_STATUS: BLOCKED` |

Интеграция поиска изначально была сделана на Brave Search API и позже переведена на Tavily
отдельной правкой: внешний контракт `search_web`, чат, потоковый ответ, Technical details,
`calculate` и `get_server_info` при этом сохранены, модельная часть не менялась. Отчёты о
прогонах прежней версии остались в `docs/specs/day-17-web-search/ACCEPTANCE.md` как
историческая справка.

Файлы `.bat` (доверенные точки входа) **не изменялись**: они защищены политикой прав, поэтому
новые режимы не добавлялись, а LIVE+UI-проверка поиска встроена в существующий
`test.bat acceptance`.

### Контракт `search_web`

- Аргументы: `query: string` (обязателен, до 600 символов) и `max_results: integer = 0`
  (0 — серверный default, максимум 10).
- Успех: `{query, count, results: [{title, url, description}], more_results_available, note}`;
  `note` = `Snippets only; the pages were not opened.` Пустая выдача — это тоже успех
  (`count = 0`, `results = []`), а не выдуманный ответ. Tavily не отдаёт признак «есть ещё
  результаты», поэтому `more_results_available` всегда `false` — поле сохранено ради
  совместимости контракта.
- Ошибки — контролируемая `ToolError` с санитизированным текстом (нет ключа, заголовков,
  тела ответа и полного URL): `not configured` (нет ключа, сетевого вызова нет),
  `timed out`, `rejected (HTTP 401/403/432/433)`, `rate limit was reached (HTTP 429)`,
  `failed (HTTP …)`, `unreachable`, `unexpected response`.
- Страницы не открываются: инструмент возвращает только список сниппетов. Это же сказано
  модели в промпте.

### Переменные окружения (день 17)

Ключ читает только процесс MCP-сервера. Backend его не использует и не выводит; в модель,
UI, SSE, trace и логи он не попадает.

| Переменная | Назначение | Значение по умолчанию |
| --- | --- | --- |
| `MCP_SEARCH_API_KEY_ENV` | Имя переменной с ключом поиска | `TAVILY_API_KEY` |
| `TAVILY_API_KEY` | Ключ Tavily Search; пусто → `search_web` честно сообщает `not configured` | (пусто) |
| `MCP_SEARCH_BASE_URL` | База API; в тестах подменяется на loopback | `https://api.tavily.com` |
| `MCP_SEARCH_TIMEOUT_SECONDS` | Тайм-аут одного запроса, clamp 1…25 | `10` |
| `MCP_SEARCH_MAX_RESULTS` | Число результатов по умолчанию, 1…10 | `5` |
| `MCP_LOAD_DOTENV` | `0` — MCP-сервер не читает `.env` (используется harness) | `1` |

Конфиг поиска резолвится при первом вызове и кэшируется: после правки `.env` MCP-сервер
нужно перезапустить.

### Команды проверок (день 17)

```
SETUP:         setup.bat
UNIT:          test.bat                  (unit + in-process MCP, без сети)
MCP SMOKE:     smoke_test.bat            (реальный MCP + backend + INT, включая SEARCH_INTEGRATION_STATUS)
LIVE + UI:     test.bat acceptance       (unit → MCP-unavailable UI → live MCP → live arithmetic →
                                          live search + UI поиска)
UI (день 16):  test.bat live ui          (регрессия арифметики)
REAL Tavily:   .venv\Scripts\python.exe harness\tavily_live.py
               (ручной opt-in, только с TAVILY_LIVE_ALLOW=1 и реальным ключом)
```

Автотесты изолированы от внешней сети: поиск тестируется на loopback-фейке
`tests/support/fake_search.py`, реальный Tavily автоматически не вызывается.

### Результаты проверок (фактические)

| Проверка | Команда | Результат |
| --- | --- | --- |
| Unit + in-process MCP | `test.bat` | `UNIT_STATUS: PASS` — 298 тестов, 32 skipped |
| Реальный MCP + backend + поиск (INT) | `smoke_test.bat` | `MCP_INTEGRATION_STATUS: PASS` (11), `BACKEND_INTEGRATION_STATUS: PASS` (14), `SEARCH_INTEGRATION_STATUS: PASS` (7) |
| LIVE-поиск + UI + день 16 | `test.bat acceptance` | exit 0: `MCP_UNAVAILABLE_UI_STATUS: PASS`, live MCP PASS, `LIVE_LLM_STATUS: PASS`, `SEARCH_LIVE_STATUS: PASS`, `SEARCH_UI_STATUS: PASS`, `key_isolation: true` |
| UI арифметики (регрессия) | `test.bat live ui` | `LIVE_LLM_STATUS: PASS`, `UI_E2E_STATUS: PASS` |
| Реальный Tavily | ручной `harness\tavily_live.py` | **не проверено** (нет ключа и явного разрешения) → `TAVILY_STATUS: BLOCKED` |

Независимая приёмка Tester: `test.bat`, `smoke_test.bat` и `test.bat acceptance` запущены
заново, результаты совпали; `tools/list` содержит `search_web` (`required=["query"]`,
`max_results` integer default 0); trace-цепочка `mcp_connect → mcp_list_tools →
model_request(tool_selection) → tool_selected(search_web) → tool_completed(ok) →
model_request(final_answer) → request_done(ok)`; в ответе 3 URL из tool-результата; UI —
2 ссылки, `details.technical` закрыт, loader исчез. Поиск по артефактам не нашёл значения
фейкового ключа. `TEST_STATUS: BLOCKED` — единственный непроверенный критерий D17-14
(реальный вызов Tavily), дефектов нет.

### Сценарий демонстрации (день 17)

1. Запустить `run_app.bat` (MCP-сервер + backend + UI).
2. Ввести `Find the official Python documentation online` — полученный ответ содержит ссылки
   с пояснениями; технические шаги (вызов `search_web`) собраны в свёрнутом `Technical details`.
3. Спросить про свежие данные (например, «что нового в Python 3.12») — агент использует поиск
   и приводит источники.
4. Спросить `What is 23 multiplied by 17?` — по-прежнему вызывается `calculate`.
5. Если ключ Tavily не задан, `search_web` сообщает, что поиск не настроен, и не выдумывает
   ссылки. Ключ добавляется в `.env` (`TAVILY_API_KEY`), после чего MCP-сервер
   перезапускается.

### Ограничения дня 17

- **Реальный Tavily Search не проверялся** автоматической проверкой: нет ключа и явного
  разрешения на реальные вызовы. Реальная внешняя граница остаётся неподтверждённой;
  `tavily_live.py` без `TAVILY_LIVE_ALLOW=1` возвращает `BLOCKED`.
- LIVE-сценарий использует детерминированный loopback-фейк поискового API, поэтому проверяет
  цепочку «модель → MCP → поиск → ответ со ссылками», но не контракт настоящего Tavily.
- Поведение модели «не утверждать о прочтении страниц» покрыто UNIT только на уровне текста
  промпта; фактическое поведение проверялось ручным просмотром ответа.
- Запуск приложения без `.env` и UI-поведение при отсутствии ключа автотестом не покрыты
  (остаются ручной проверкой).
- `test.bat acceptance` теперь всегда делает дополнительный LIVE-прогон с реальной моделью,
  поэтому время приёмки растёт.
- Запуск и демонстрация на DeepSeek/VPS по-прежнему не выполнялись; контракт `ModelProvider`
  не изменён, поэтому инструмент доступен любой модели, которую обслуживает этот провайдер.

## День 18. Фоновые задания и сохраняемые чаты

### Задача дня

Связать две части в том же проекте `week-04/day-16-mcp-agent`:

1. **Фоновые (отложенные/периодические) задания**, которые агент создаёт через MCP-инструмент.
   Задание выполняется на сервере независимо от открытого браузера, сохраняет результаты прогонов
   и позволяет агенту получить сводку.
2. **Сохраняемые чаты**: создание, переключение, переименование и удаление; не более пяти чатов;
   собственная история на чат и изоляция контекста; после перезагрузки страницы, рестарта backend
   и обновления проекта на VPS список и истории сохраняются.

Минимальный сценарий дня: «Каждый день ищи новости по теме X и показывай краткую сводку со
ссылками» — это периодический поиск по теме через уже работающий `search_web`; прогон сохраняется,
а агент показывает сохранённую сводку со ссылками. Соответствие формулировке задания зафиксировано
в `docs/specs/day-18-scheduled-tasks-and-chats/SPEC.md` §4.

Архитектура дней 16–17 сохранена: отдельный MCP-сервер процессом (Streamable HTTP, `/mcp`),
FastAPI-backend с SSE-чатом, единственная сетевая граница `mcp_server/web_search.py` (Tavily),
провайдер модели как отдельная граница, trace и harness.

### Что реализовано

| Файл | Назначение |
| --- | --- |
| `storage/db.py` | общий SQLite-файл: pragmas (WAL, FK, busy_timeout), схема v2, миграция v1→v2, гейт версии, ленивое открытие |
| `storage/chats.py` | репозиторий чатов и сообщений (пишет только backend) |
| `storage/tasks.py` | репозиторий заданий и прогонов, CAS-захват слота, хранение не более 50 последних прогонов на задание (пишет только MCP-сервер) |
| `agent/chats.py` | `ChatService`: лимит 5, авто-заголовок, окно контекста, задания для UI |
| `agent/sessions.py` | `ChatSession` + `SessionRegistry` с per-chat блокировкой (вместо памяти на процесс) |
| `agent/server.py` | HTTP API чатов, `chat_id` в `POST /api/chat/stream`, единый формат ошибок |
| `agent/orchestrator.py` | окно последних сообщений, инжект `chat_id` после валидации, новый `SYSTEM_PROMPT`, trace `chat_id`/`context_messages` |
| `agent/tool_schema.py` | скрытие `chat_id` из схемы, которую видит модель |
| `mcp_server/tasks.py` | `TaskService`: создание/список/сводка/остановка, захват и выполнение прогона |
| `mcp_server/scheduler.py` | планировщик в процессе MCP-сервера (daemon-поток, устойчивость к ошибкам тика) |
| `mcp_server/tools.py` | 4 тонких инструмента заданий; `calculate`/`get_server_info`/`search_web` не изменены |
| `mcp_server/server.py` | регистрация 7 инструментов; планировщик запускается в `run()` |
| `static/index.html`, `app.js`, `styles.css` | панель чатов, панель заданий, `Clear chat` |
| `harness/scheduler_restart.py`, `harness/tasks_real_live.py` | проверки рестарта и opt-in реального Tavily |
| `harness/live_mcp.py`, `live_e2e.py`, `acceptance.py`, `mcp_unavailable_e2e.py` | INT/RESTART/LIVE/UI-сценарии дня 18 |
| `tests/test_storage.py`, `test_chats_api.py`, `test_tasks.py`, `test_scheduler.py`, `test_chats_tasks_ui.py`, `tests/integration/test_tasks_live.py` | новые тесты |
| `.env.example`, `.gitignore` | новые переменные окружения; `data/` в игнорируемых |
| `docs/specs/day-18-scheduled-tasks-and-chats/{SPEC,PLAN,ACCEPTANCE}.md` | спецификация, план и критерии приёмки |

Все `.bat` (доверенные точки входа) **не изменялись**: планировщик запускается вместе с
`python -m mcp_server`, а новые проверки встроены в существующие `test.bat`/`smoke_test.bat`/
`test.bat acceptance` через модули `harness/`.

### Хранение

Один общий файл SQLite задаётся переменной `AGENT_DB_PATH` (по умолчанию `<project>/data/day18.sqlite3`,
каталог `data/` в `.gitignore`). Файл читают оба процесса: backend владеет `chats`/`messages`, а
MCP-сервер — `tasks`/`runs`; каскадное удаление чата удаляет его сообщения, задания и прогоны.
Открытие ленивое: построение приложения, `tools/list` и discovery БД не создают. Схема v2
создаётся при первом обращении; существующая БД v1 обновляется той же транзакцией (добавляется
колонка `tasks.max_results INTEGER NOT NULL DEFAULT 0`), уже созданные задания получают `0` —
это серверный дефолт, а не замороженный лимит. Миграция односторонняя: более старая версия
приложения на обновлённой БД получает контролируемую ошибку без записи. WAL + `busy_timeout`
делают безопасным доступ двух локальных процессов к одному файлу.

### Контракт MCP-инструментов заданий

Всего инструментов 7 (3 прежних + 4 новых). `chat_id` подставляет backend после валидации
аргументов модели и **скрывает его из схемы, которую видит модель**, из UI-копии аргументов и из
trace; сервер отвергает пустой или чужой `chat_id`.

| Инструмент | Контракт |
| --- | --- |
| `schedule_search_task(query, interval_seconds, chat_id, max_results)` | создаёт периодический поиск; `max_results` 1..10 ограничивает число результатов каждого прогона (`0` — серверный дефолт, `>10` clamp до 10, отрицательное/нецелое — ошибка); минимальный интервал 60 с, максимум 30 дней (clamp), 3 задания на чат и 10 всего, дубликат не создаётся и лимит не меняет; при незаконфигурированном `search_web` — честная ошибка |
| `list_search_tasks(chat_id)` | список заданий чата с состоянием, расписанием и последним статусом |
| `get_latest_search_run(task_id, chat_id)` | последняя сводка: `ok` со ссылками, `empty`, `error` (сохранённый текст) или `pending`; только задания своего чата |
| `stop_search_task(task_id, chat_id)` | останавливает будущие прогоны; повторная остановка идемпотентна |

Первый прогон стартует в пределах тика планировщика (секунды), далее — каждые `interval_seconds`.
Ошибка или пустая выдача сохраняются как есть; выдуманная сводка не выдаётся. Сводку формирует
модель по запросу пользователя из сохранённого результата — фоновых вызовов модели нет.

### Планировщик, браузер и VPS

Планировщик живёт **в процессе MCP-сервера** — там же, где `search_web` и ключ Tavily. Открытый
браузер не является условием выполнения: прогон не требует ни UI, ни backend (INT-проверка
выполняет задание при остановленном backend). Расписание хранится в БД, поэтому после рестарта
процесс перечитывает его; пропущенный запуск выполняется ровно один раз (без backfill).
Захват слота — compare-and-swap в одной транзакции, что исключает двойной прогон при пересечении
тиков. Если чат удалён, планировщик больше не видит задание, а результат уже начатого прогона
отбрасывается; после удаления новые обращения к поисковому API не начинаются.

На VPS оба сервиса держатся процессным менеджером (systemd, `Restart=always`), а `AGENT_DB_PATH`
задаётся абсолютным путём вне деплой-каталога (например `/var/lib/day16/day18.sqlite3`), чтобы БД
переживала fast-forward деплой. Ключ Tavily остаётся только в MCP-сервисе. Отдельная задача
оператора — задать `AGENT_DB_PATH` в юнитах и убедиться, что задания идут без открытого браузера.

### Переменные окружения (день 18)

| Переменная | Назначение | По умолчанию |
| --- | --- | --- |
| `AGENT_DB_PATH` | путь к общему файлу SQLite; пусто → `<project>/data/day18.sqlite3` | (пусто) |
| `AGENT_CHAT_CONTEXT_MESSAGES` | сколько последних сообщений чата уходит в модель, clamp 2…100 | `20` |
| `MCP_TASK_TICK_SECONDS` | период тика планировщика, clamp 0.5…60 | `2` |

Значения из `.env` теперь читает и MCP-процесс (с учётом `MCP_LOAD_DOTENV=0`), поэтому backend и
MCP-сервер гарантированно открывают один и тот же файл БД.

### HTTP API чатов

`GET /api/chats`, `POST /api/chats` (при 5 чатах — `409 chat_limit` без автоудаления),
`PATCH /api/chats/{id}`, `DELETE /api/chats/{id}?force=`, `GET /api/chats/{id}/messages`,
`POST /api/chats/{id}/clear`, `GET /api/chats/{id}/tasks`. Управляемые ошибки используют единый
формат `{"detail": {"category", "message"}}`. `DELETE` без `force` при активных заданиях
возвращает `409 chat_has_active_tasks`; с `force=true` чат удаляется каскадом.

### Поведение для пользователя

- Слева — панель `Chats`: `New chat`, список чатов, переключение, переименование, удаление.
  При лимите 5 показывается понятное сообщение из backend, ничего не удаляется автоматически.
- Кнопка `Clear session` заменена на `Clear chat`: очищает только выбранный чат и не создаёт
  новый.
- Под чатом — панель заданий: состояние, интервал, последняя сводка со ссылками (не более
  `max_results` задания, но и не более 5) или сохранённая ошибка.
- Панель заданий обновляется автоматически раз в 10 секунд, пока страница открыта, а также сразу
  при переключении чата; кнопка `Refresh` сохранена. Опрос только читает `GET .../tasks`: он не
  запускает поиск, не вызывает модель и не расходует Tavily. Таймер опроса снимается при уходе
  со страницы, параллельные запросы не накапливаются.
- Модель получает только последние 20 сообщений выбранного чата; полная история видна в UI.
- Удаление чата с активным заданием сначала показывает предупреждение (HTTP 409), и только после
  подтверждения чат и его задания удаляются.

### Команды проверок (день 18)

```
SETUP:        setup.bat
UNIT:         test.bat                     (unit + in-process MCP, без сети)
MCP + INT:    smoke_test.bat               (реальный MCP + backend + loopback-фейк поиска,
                                            включая TASKS_INTEGRATION_STATUS,
                                            PERSISTENCE_RESTART_STATUS, SCHEDULER_RESTART_STATUS)
LIVE + UI:    test.bat acceptance          (arithmetic → search → tasks: TASKS_LIVE_STATUS,
                                            CHATS_UI_STATUS, TASKS_UI_STATUS)
REAL Tavily:  .venv\Scripts\python.exe harness\tasks_real_live.py
              (ручной opt-in, только с TASKS_REAL_ALLOW=1 и реальным ключом)
```

Автотесты изолированы от внешней сети и работают против loopback-фейка поиска; реальный Tavily
автоматически не вызывается.

### Результаты проверок (фактические)

| Проверка | Команда | Результат |
| --- | --- | --- |
| Unit + in-process MCP | `test.bat` | `UNIT_STATUS: PASS` — 448 тестов, 43 skipped, exit 0 |
| Реальный MCP + backend + поиск + задания | `smoke_test.bat` | `MCP_INTEGRATION_STATUS: PASS` (11), `BACKEND_INTEGRATION_STATUS: PASS` (16), `SEARCH_INTEGRATION_STATUS: PASS` (7), `TASKS_INTEGRATION_STATUS: PASS` (9), `PERSISTENCE_RESTART_STATUS: PASS`, `SCHEDULER_RESTART_STATUS: PASS` |
| LIVE + UI (реальная модель) | `test.bat acceptance` | `MCP_UNAVAILABLE_UI_STATUS: PASS` (+`TRACE_STATUS: PASS`), live MCP PASS, `LIVE_LLM_STATUS: PASS`, `SEARCH_LIVE_STATUS: PASS`, `SEARCH_UI_STATUS: PASS`, `TASKS_LIVE_STATUS: PASS`, `CHATS_UI_STATUS: PASS`, `TASKS_UI_STATUS: PASS`, `key_isolation: true` |
| Реальный Tavily для задания | ручной `harness\tasks_real_live.py` | **не проверено** (нет opt-in/ключа) → `TASKS_REAL_STATUS: BLOCKED` |
| VPS | ручной чек-лист SPEC §13 | **не проверено** (нужен оператор) |

Независимая приёмка Tester: `test.bat`, `smoke_test.bat` и `test.bat acceptance` запущены заново,
результаты совпали; подтверждены CRUD чатов, лимит 5 (HTTP 409 и сообщение в UI), изоляция
истории и контекста, `Clear chat` только выбранного чата, связь задания с чатом, прогон по
расписанию без открытого браузера, сохранение сводки и восстановление после рестарта, отсутствие
новых обращений к поисковому API после удаления чата, а также регрессия дней 16–17. Дополнительно
подтверждены авто-обновление панели заданий без нажатия `Refresh` (карточки и ссылки появились
сами после создания задания при уже открытой странице) и лимит результатов задания: ровно 3 ссылки
при `max_results=3` против 5 при серверном дефолте, при этом опрос панели не вызывал поиск
(счётчик запросов loopback-фейка за цикл опроса не менялся). Дефектов не
найдено. Итоговый `TEST_STATUS: BLOCKED` выставлен только из-за двух сознательно отложенных
критериев — D18-22 (реальный Tavily) и D18-23 (VPS); остальные критерии приняты на уровне
UNIT/INT/RESTART/LIVE/UI.

Разница 448 тестов в `test.bat` и 424 в unit-шаге `test.bat acceptance` объяснима и не является
пропуском тестов дня 18: компонент acceptance запускает unit-набор через `sanitized_env()` без
браузерных путей, поэтому три DOM-класса (`MarkdownRendererDomTest` — 16 тестов,
`SearchSpinnerBrowserTest` — 5, `LinkStyleBrowserTest` — 3, всего 24) пропускаются целиком:
`448 − 24 = 424`, а число skipped растёт с 43 до 46 (по одному на класс). Все модули дня 18
выполняются в обоих прогонах.

### Локальная модель в LIVE-прогонах

`harness/live_e2e.py` сам поднимает локальную модель и останавливает её после прогона. По
артефакту прогона `TASKS` (`.runs/<timestamp>-live-e2e-tasks/report.json`): запрошенная модель —
`qwen3.8-27b-local`, сконфигурированная на endpoint — `qwen3.8-27iq4xs`, `started_by_harness: true`,
`endpoint_running_before_run: false`, `model_ready: true`. Модель сгенерировала ответ (события
`delta`, `done` с `finish_reason: stop`) и вызвала `schedule_search_task`, а затем
`get_latest_search_run`; в ответе — 3 ссылки из tool-результата. Это доказанный локальный
LIVE-прогон, а не имитация. В том же прогоне UI-часть доказала авто-обновление панели:
`auto_refreshed: true`, `link_counts: {"limited": 3, "defaulted": 5}`, `switch_immediate: true`,
`poll_is_read_only: true` (счётчик запросов фейка до/после цикла опроса — 3/3).

### Сценарий демонстрации (день 18)

1. `setup.bat`, затем `run_app.bat` — открывается чат, слева панель `Chats`, под чатом панель
   заданий.
2. Создать новый чат, переименовать его, переключиться между чатами и убедиться, что истории не
   смешиваются (удаление одного чата не затрагивает другой).
3. Ввести: `Every day search for the latest Python news and show me a short summary with links.`
   Модель вызывает `schedule_search_task`; в панели заданий появляется задание, первый прогон
   стартует в пределах пары секунд, а панель обновляется сама (без нажатия `Refresh`).
4. Спросить: `Show me the latest summary of my scheduled search.` — модель вызывает
   `get_latest_search_run`, ответ содержит ссылки из сохранённой сводки.
5. Перезапустить приложение (`run_app.bat`), обновить страницу: чат, история и результат задания
   на месте.
6. Создать шестой чат — появляется понятное сообщение о лимите 5. Удалить чат с активным
   заданием — сначала предупреждение, после подтверждения задание и чат исчезают.

### Ограничения дня 18

- **Реальный Tavily не проверялся**: нет opt-in и ключа; `harness/tasks_real_live.py` без
  `TASKS_REAL_ALLOW=1` возвращает `TASKS_REAL_STATUS: BLOCKED`. Реальная внешняя граница остаётся
  неподтверждённой, как и в дне 17.
- **VPS-развёртывание не выполнялось**: чек-лист (systemd, `AGENT_DB_PATH` вне деплой-каталога,
  круглосуточная работа) выполняет оператор; автоматически он не проверяется.
- Рост таблицы сообщений не ограничен (лимит заданий и прогонов — есть); отдельная задача при
  необходимости.
- Мгновенная отмена уже начатого сетевого вызова Tavily невозможна (stdlib HTTP без отмены);
  гарантируется отсутствие **новых** вызовов после удаления чата.
- Индексы-идентификаторы `chat_id`/`task_id` не выводятся в интерфейсе, но и не являются секретами;
  многопользовательской изоляции нет (однопользовательский прототип, как и ранее).
- История прежних дней 16–17 хранилась только в памяти и не мигрирует в БД: после перехода на
  сохраняемые чаты старые in-memory сессии не восстанавливаются.
- Результаты задания ограничены сохранённым `max_results` (1..10), но панель рендерит не более
  5 ссылок: при `max_results` 6..10 в карточке видно 5, при этом полный результат прогона сохранён
  в `runs`. Выравнивание панельного капа с `SEARCH_MAX_RESULTS_CAP` — отдельное необязательное
  улучшение.
- Миграция схемы односторонняя: после обновления до v2 более старая версия приложения на той же
  БД откажется писать (контролируемая `SchemaVersionError`); backend и MCP-сервер обновляются
  вместе.
