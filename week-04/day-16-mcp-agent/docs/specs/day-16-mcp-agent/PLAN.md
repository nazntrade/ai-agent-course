# PLAN — Day 16: локальный MCP-агент

План описывает реализацию, тесты и точки входа. Каждый пункт трассируется к
критериям приёмки из `ACCEPTANCE.md`.

## 1. Файловая структура

```
week-04/day-16-mcp-agent/
├─ .env.example  .gitignore  requirements.txt
├─ setup.bat  run_app.bat  test.bat  smoke_test.bat
├─ discovery_cli.py
├─ mcp_server/{__init__.py,tools.py,server.py,__main__.py}
├─ agent/{__init__.py,settings.py,mcp_adapter.py,tool_schema.py,provider.py,
│         orchestrator.py,sessions.py,trace.py,server.py,__main__.py}
├─ static/{index.html,app.js,styles.css}
├─ harness/{__init__.py,qa_bridge.py,processes.py,run_dir.py,
│           live_mcp.py,live_e2e.py,acceptance.py,openapi_snapshot.py}
├─ tests/{__init__.py,test_*.py,support/,integration/}
├─ docs/openapi.json
└─ docs/specs/day-16-mcp-agent/{SPEC.md,PLAN.md,ACCEPTANCE.md,DEMO.md}
```

`harness/openapi_snapshot.py` — единственное дополнение к согласованному списку:
`test.bat openapi` должен чем-то обновлять `docs/openapi.json`.

## 2. Порядок реализации

1. `mcp_server/tools.py` → `mcp_server/server.py` → `mcp_server/__main__.py`.
2. `agent/mcp_adapter.py` (граница MCP) → `discovery_cli.py`.
3. `agent/settings.py`, `agent/trace.py`, `agent/sessions.py`,
   `agent/tool_schema.py`, `agent/provider.py`, `agent/orchestrator.py`.
4. `agent/server.py`, `agent/__main__.py`, `static/*`.
5. `harness/*`, конфигурация тестов, `docs/openapi.json`.
6. Точки входа `.bat` (см. приложение А).

## 3. Ключевые решения

* **Граница MCP.** Только `agent/mcp_adapter.py` импортирует `mcp`. SDK v2:
  высокоуровневый `mcp.Client(url, mode="auto")`; `probe()` открывает одну сессию
  (handshake + полный `tools/list`) и закрывает её. `status()` делегирует в
  `probe()`, `list_tools()` остаётся для discovery/tests. Ошибки →
  `McpError(category, safe message)`; `-32001` → timeout.
* **Pagination.** `_list_all` читает страницы до `next_cursor = None`, ограничен
  числом страниц и tool, чтобы некорректный сервер не зациклил клиент.
* **Схемы.** `to_openai_tools` валидирует имя, рекурсивно чистит JSON-Schema до
  известных ключей и подставляет пустой объект вместо непригодной схемы.
* **Провайдер.** Стриминг `openai.AsyncOpenAI`; tool calls собираются из чанков по
  `index`; `usage` может отсутствовать или быть частичным — числа не выдумываются.
  Ошибка → `ModelError` с одной категорией (нет ключа, unreachable, timeout, 401/403,
  404/unknown model, protocol, прочий HTTP); сообщения санитизированы. Флаг
  `configured`; при `false` `stream()` не делает HTTP-запрос.
* **Оркестратор.** `status → MCP → tools → model → tool → model → delta → done`;
  лимит 3 раунда; невалидный JSON аргументов → tool-сообщение с ошибкой и
  продолжение; `provider.configured=false` → `model_not_configured` до опроса MCP;
  недоступный MCP определяется до вызова модели; при `error` событие `done` не
  отправляется. Расхождение запрошенной и сообщённой модели → одно событие
  `model_reported` и поле `reported_model` в `request_done`.
* **SSE keep-alive.** Оркестратор работает в отдельной задаче, события идут через
  очередь: `asyncio.wait_for` по очереди не отменяет сам генератор.
* **UI.** Loader появляется синхронно; прогресс идёт в закрытый
  `details.technical`, а не в текст ответа; ошибка — одно сообщение.
* **Trace.** Санитайзер отбрасывает ключи-секреты, обрезает строки, ограничивает
  вложенность; текст пользователя пишется только как `message_chars`.
* **Harness.** Останавливает только свои PID (`taskkill /F /T /PID`); занятый порт —
  prerequisite exit 2; артефакты — в `.runs/<timestamp>-<label>/`. `live_e2e`
  запрашивает канонический `DEFAULT_MODEL_NAME`, а имя из QA-конфига пишет как
  `qa_model_configured`. `live_mcp` дополнительно проверяет, что один no-tool
  chat request открывает ровно один MCP probe (два `POST` в backend-логе).

## 4. Тесты

* Unit (без сети): `tests/test_sdk_contract.py` (первый, контракт SDK v2),
  `test_settings.py`, `test_tools.py`, `test_mcp_inprocess.py`,
  `test_mcp_adapter.py` (включая DI-счётчик сессий `probe()`),
  `test_tool_schema.py`, `test_provider.py` (матрица категорий, включая
  «401 не склеивается с unreachable»), `test_orchestrator.py`
  (`probe_calls == 1`, `model_not_configured`, `model_reported`), `test_trace.py`,
  `test_discovery_cli.py`, `test_api.py` (один probe на `/api/mcp/tools`),
  `test_openapi_snapshot.py`, `test_harness.py`.
* Integration (`RUN_LIVE_MCP=1`): `tests/integration/test_mcp_live.py` (реальный
  MCP по HTTP: list/valid/invalid/0/unknown/reconnect/timeout/unreachable),
  `tests/integration/test_backend_live.py` (реальный backend-процесс: health,
  status, tools, SSE, no-tool, повторный запрос, оба контролируемых отказа).
* Live LLM E2E: `harness/live_e2e.py` (trace-цепочка, корректный результат, UI).
* Acceptance: `harness/acceptance.py` — unit → live MCP → live E2E.

## Приложение А. Точки входа `.bat`

Файлы `setup.bat`, `test.bat`, `smoke_test.bat`, `run_app.bat` защищены
permission-правилом (`edit: **/*.bat = deny`), поэтому их создаёт Configurator.
Ниже — согласованное содержимое.

### setup.bat

Создаёт `.venv`, ставит `requirements.txt`, подсказывает про `.env.example`,
exit 0/2. Шаблон — `week-03/memory-state-agent/test.bat`, но с `exit /b 2` на
ошибке установки и без запуска тестов.

### test.bat

```
test.bat                 -> unit + in-process MCP: python -m unittest discover -s tests -t .
test.bat live [ui]       -> RUN_LIVE_MCP=1, RUN_LIVE_LLM=1 (и RUN_UI_E2E=1 при ui),
                            затем python harness/live_e2e.py [--ui]
test.bat acceptance      -> python harness/acceptance.py
test.bat openapi         -> python -m harness.openapi_snapshot
```

Общее: `PYTHONUTF8=1`, `PYTHONIOENCODING=utf-8`, `cd /d "%~dp0"`, автосоздание
`.venv` и установка зависимостей при первом запуске, exit 0/1/2, печать строк
`LIVE_LLM_STATUS:` и `UI_E2E_STATUS:` (при наличии).

### smoke_test.bat

Проверяет свободные тестовые порты и запускает `python harness/live_mcp.py`;
exit-код runner-а пробрасывается (0/1/2). Свои процессы останавливает сам harness.

### run_app.bat

Режимы: `all` (по умолчанию), `mcp`, `backend`, `tools`.

* `all`: если MCP URL уже отвечает — не трогать; иначе поднять `python -m mcp_server`
  (captured PID), дождаться readiness, поднять `python -m agent`, дождаться
  `/api/health`, открыть UI, по выходу остановить только свой PID.
* `mcp`: MCP-сервер в foreground.
* `backend`: только backend.
* `tools`: `python discovery_cli.py`.

Занятый порт → сообщение и exit 2, чужие процессы не завершаются. Никаких
абсолютных путей: только `%~dp0` и относительные пути.

## 5. Отклонения

Отклонений от утверждённого архитектурного решения нет. Добавлен только
`harness/openapi_snapshot.py` (см. п. 1).
