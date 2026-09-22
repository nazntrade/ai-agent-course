# ACCEPTANCE — Day 16: локальный MCP-агент

Критерии приёмки с идентификаторами, требуемым уровнем проверки и ссылкой на
проверяющий тест. Единица уровня проверки: **UNIT** (без сети), **INT**
(реальный процесс + реальный HTTP), **LIVE** (реальная модель), **UI**
(Playwright + системный браузер).

## Функциональные критерии

| ID | Критерий | Уровень | Проверка |
| --- | --- | --- | --- |
| AC-01 | MCP-сервер — отдельный процесс, loopback, свой порт, Streamable HTTP | INT | `harness/live_mcp.py`, `tests/integration/test_mcp_live.py` |
| AC-02 | `calculate` поддерживает add/subtract/multiply/divide, типизированные аргументы, structured result | UNIT | `tests/test_tools.py`, `tests/test_mcp_inprocess.py` |
| AC-03 | Деление на ноль — контролируемая ошибка, без падения сервера | UNIT + INT | `tests/test_tools.py`, `tests/integration/test_mcp_live.py` |
| AC-04 | `get_server_info` возвращает name/version/status/uptime и не раскрывает env/пути/секреты | UNIT | `tests/test_tools.py`, `tests/test_mcp_inprocess.py` |
| AC-05 | MCP не имеет доступа к shell/Git/ФС; `eval` не используется | UNIT | `tests/test_tools.py`, ревизия `mcp_server/*` |
| AC-06 | Discovery CLI: настоящее MCP-соединение, pagination до `next_cursor=None`, закрытие сессии | UNIT + INT | `tests/test_mcp_adapter.py`, `harness/live_mcp.py` |
| AC-07 | Вывод CLI: `CONNECTED`, `PROTOCOL_VERSION`, `SERVER_INFO`, `TOOLS_COUNT`, tool/description/schema | UNIT + INT | `tests/test_discovery_cli.py`, `harness/live_mcp.py` |
| AC-08 | Ошибка CLI: категория + безопасное сообщение + exit 2, без ключей/env | UNIT + INT | `tests/test_discovery_cli.py`, `harness/live_mcp.py` |
| AC-09 | `GET /api/health`, `/api/mcp/status`, `/api/mcp/tools`, `/openapi.json` | UNIT + INT | `tests/test_api.py`, `tests/integration/test_backend_live.py` |
| AC-10 | Список tools совпадает с реальным MCP | INT | `tests/integration/test_backend_live.py` |
| AC-11 | `/api/mcp/tools` при недоступном MCP — HTTP 200 и `connected:false` | UNIT + INT | `tests/test_api.py`, `tests/integration/test_backend_live.py` |
| AC-12 | `POST /api/chat/stream` — SSE с порядком `tool_call → tool_result → delta → done` | UNIT + INT | `tests/test_api.py`, `tests/integration/test_backend_live.py` |
| AC-13 | Недоступный MCP определяется до вызова модели (`mcp_unavailable`) | UNIT + INT | `tests/test_orchestrator.py`, `tests/integration/test_backend_live.py` |
| AC-14 | Недоступная модель — контролируемая ошибка `model_unreachable` | UNIT + INT | `tests/test_api.py`, `tests/integration/test_backend_live.py` |
| AC-15 | Повторный запрос в той же сессии учитывает историю | UNIT + INT | `tests/test_api.py`, `tests/integration/test_backend_live.py` |
| AC-16 | UI: поле ввода, кнопка, Enter, история сессии, loader → потоковый текст | UI | `harness/live_e2e.py --ui` |
| AC-17 | UI: блок MCP status (connected, protocol version, tools count, раскрываемый список) | UI | `harness/live_e2e.py --ui` |
| AC-18 | UI: понятные ошибки MCP и модели; нет credentials/headers в браузере | UNIT + UI | `tests/integration/test_backend_live.py`, `harness/live_e2e.py --ui` |
| AC-19 | Trace: `mcp_connect → mcp_list_tools → model_request(tool_selection) → tool_selected(calculate) → tool_completed(ok) → model_request(final_answer) → request_done` | LIVE | `harness/live_e2e.py` |
| AC-20 | Живая модель реально вызывает MCP-tool, результат возвращается модели, финальный ответ математически верен | LIVE | `harness/live_e2e.py` |
| AC-21 | Trace не содержит ключей, Authorization, env, system prompt, полного текста пользователя, абсолютных путей | UNIT + LIVE | `tests/test_trace.py`, `harness/live_e2e.py` |
| AC-40 | Отсутствующая конфигурация модели → `model_not_configured`; HTTP-запрос к модели не выполняется и MCP не опрашивается | UNIT + INT | `tests/test_orchestrator.py`, `tests/test_api.py`, `tests/integration/test_backend_live.py` |
| AC-41 | Матрица категорий ошибок модели: нет ключа, unreachable, timeout, 401/403, 404/unknown model, protocol, прочий HTTP; 401 не склеивается с unreachable и сообщения без секретов | UNIT | `tests/test_provider.py` |
| AC-42 | Один chat request и `/api/mcp/tools` открывают ровно один MCP probe (одна сессия, один `tools/list`) | UNIT + INT | `tests/test_orchestrator.py`, `tests/test_api.py`, `tests/test_mcp_adapter.py`, `harness/live_mcp.py` |
| AC-43 | Запрошенная модель — канонический `qwen3.8-27b-local`; имя из QA-конфига только фиксируется как `qa_model_configured` | LIVE | `harness/live_e2e.py` |
| AC-44 | UI: loader со `.stage`, прогресс в закрытом `details.technical`, ответ в `.text` без `MCP tool call`/`Status:`, loader удалён | UI | `harness/live_e2e.py --ui` |
| AC-45 | UI при недоступном MCP: ровно один понятный пузырь ошибки без дублей и внутренних деталей, pill `disconnected`, UI восстанавливается, trace без `model_request`/`tool_selected`/`tool_completed` | UI | `harness/mcp_unavailable_e2e.py`; шаг `test.bat acceptance` (`harness/acceptance.py`, строка `MCP_UNAVAILABLE_UI_STATUS`) |
| AC-46 | `mcp-types==2.2.0` — прямая зависимость (`from mcp_types import REQUEST_TIMEOUT` в `agent/mcp_adapter.py`), совместима с `mcp==2.2.0` | UNIT + ревизия | `requirements.txt`, `tests/test_sdk_contract.py` |

## Граничные и ошибочные сценарии

| ID | Сценарий | Уровень | Проверка |
| --- | --- | --- | --- |
| AC-22 | Запрос без tool | UNIT + INT | `tests/test_orchestrator.py`, `tests/integration/test_backend_live.py` |
| AC-23 | Невалидные аргументы tool | UNIT + INT | `tests/test_orchestrator.py`, `tests/integration/test_mcp_live.py` |
| AC-24 | Unknown tool | INT | `tests/integration/test_mcp_live.py` |
| AC-25 | MCP недоступен до и после пользовательского запроса | INT | `tests/integration/test_backend_live.py` |
| AC-26 | Модель недоступна | UNIT + INT | `tests/test_api.py`, `tests/integration/test_backend_live.py` |
| AC-27 | Unicode/русский текст | UNIT | `tests/test_api.py` |
| AC-28 | Pagination с `next_cursor` | UNIT | `tests/test_mcp_adapter.py` |
| AC-29 | Timeout MCP | UNIT + INT | `tests/test_mcp_adapter.py`, `tests/integration/test_mcp_live.py` |
| AC-30 | Отмена/закрытие streaming клиентом не оставляет ресурсов | UNIT | `tests/test_orchestrator.py` |
| AC-31 | Лимит раундов tool-call | UNIT | `tests/test_orchestrator.py` |
| AC-32 | Занятый порт — prerequisite exit 2, чужие процессы не затрагиваются | UNIT + MANUAL | `tests/test_harness.py`, `run_app.bat` |

## Критерии запуска и артефактов

| ID | Критерий | Уровень | Проверка |
| --- | --- | --- | --- |
| AC-33 | `setup.bat` создаёт окружение и ставит зависимости, exit 0/2 | MANUAL | фактический запуск |
| AC-34 | `test.bat` — unit-набор зелёный | UNIT | `.\week-04\day-16-mcp-agent\test.bat` |
| AC-35 | `smoke_test.bat` — live MCP smoke зелёный | INT | `.\week-04\day-16-mcp-agent\smoke_test.bat` |
| AC-36 | `run_app.bat` поднимает MCP+backend+UI и корректно останавливает свои процессы | MANUAL | фактический запуск |
| AC-37 | `qa\run_local_e2e.bat TESTS` остаётся зелёным | UNIT | `qa\run_local_e2e.bat TESTS` |
| AC-38 | OpenAPI 3.1 `docs/openapi.json` соответствует приложению | UNIT | `tests/test_openapi_snapshot.py` |
| AC-39 | Нет абсолютных машинных путей и литеральных ключей в файлах, тестах, логах, trace и отчётах | ревизия | `tests/test_trace.py`, ревизия diff |

## Итог приёмки

* `UNIT_STATUS`: результат `test.bat`.
* `MCP_INTEGRATION_STATUS` и `BACKEND_INTEGRATION_STATUS`: результат
  `smoke_test.bat`.
* `LIVE_LLM_STATUS`: `PASS` либо `BLOCKED` с точной причиной.
* `UI_E2E_STATUS`: `PASS`, `BLOCKED` или `MANUAL_REQUIRED` с точным ручным сценарием.
* Mock-проверки не заменяют LIVE и UI уровни.
