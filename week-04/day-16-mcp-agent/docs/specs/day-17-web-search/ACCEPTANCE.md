# ACCEPTANCE — Day 17: веб-поиск в MCP-агенте

Критерии приёмки с идентификаторами, требуемым уровнем проверки и ссылкой на
проверяющий тест. Единица уровня: **UNIT** (без сети), **INT** (реальный
MCP-процесс + реальный HTTP + loopback `FakeSearchServer`), **LIVE** (реальная
модель Qwen/DeepSeek), **UI** (Playwright + системный браузер), **REAL**
(настоящий Tavily Search, только opt-in).

| ID | Критерий | Уровень | Проверка |
| --- | --- | --- | --- |
| D17-01 | `search_web` в `tools/list`, `query` required, `max_results` integer optional | UNIT + INT | `tests/test_mcp_inprocess.py`, `tests/integration/test_search_live.py` |
| D17-02 | structured-успех `title`/`url`/`description` | UNIT + INT | `tests/test_web_search.py`, `tests/integration/test_search_live.py` |
| D17-03 | пустая выдача = ok/count 0, без выдуманных ссылок | UNIT + INT | `tests/test_web_search.py`, `tests/integration/test_search_live.py` |
| D17-04 | нет ключа → `not_configured`, сетевого вызова нет | UNIT | `tests/test_web_search.py` |
| D17-05 | timeout контролируемый | UNIT + INT | `tests/test_web_search.py`, `tests/integration/test_search_live.py` |
| D17-06 | 401/403/432/433/429/400/422/5xx → `api_error`, только статус | UNIT + INT | `tests/test_web_search.py`, `tests/integration/test_search_live.py` |
| D17-07 | ключ только в MCP-процессе; нет в модели/UI/trace/логе/`repr`; backend ключ не читает и не выводит | UNIT + INT + LIVE | `tests/test_web_search.py` (`repr`), `tests/test_tools.py`, `tests/integration/test_search_live.py` (нет ключа в результате), `test.bat acceptance` (`key_isolation` проверяет SSE-текст, `trace.jsonl`, `mcp_server.log`, `backend.log`) |
| D17-08 | `calculate`/`get_server_info` без изменений | UNIT + INT | `tests/test_tools.py`, `tests/test_mcp_inprocess.py`, `smoke_test.bat` |
| D17-09 | реальная модель вызывает `search_web` (trace-chain) | LIVE | `test.bat acceptance` (шаг live search E2E: `harness/live_e2e.py --scenario search --ui`) |
| D17-10 | ответ содержит ≥ 2 URL из tool-результата | LIVE + UI | `test.bat acceptance` (`SEARCH_LIVE_STATUS` + `SEARCH_UI_STATUS`) |
| D17-11 | модель не утверждает о прочтении страниц | UNIT (только текст промпта) + MANUAL (поведение) | `tests/test_orchestrator.py` проверяет только формулировки `SYSTEM_PROMPT`; фактическое поведение модели — только ручной просмотр ответа в UI, автотестом не покрыто |
| D17-12 | `ModelProvider` не изменён, работает с локальным Qwen | LIVE | `test.bat acceptance` (arithmetic и search), `git diff agent/provider.py` пуст |
| D17-13 | UI: loader, закрытый Technical details, ссылки | UI | `test.bat acceptance` (`SEARCH_UI_STATUS`) |
| D17-14 | реальный Tavily (opt-in) — BLOCKED без ключа/разрешения | REAL | ручной standalone: `.venv\Scripts\python.exe harness\tavily_live.py` (только с `TAVILY_LIVE_ALLOW=1`) |
| D17-15 | запуск без `.env` не падает, поиск честно сообщает ошибку | UNIT (только `not_configured`) + MANUAL | `tests/test_web_search.py` проверяет `not_configured` и отсутствие сетевого вызова; запуск приложения без `.env` и UI-поведение **не проверены** (остаются ручной проверкой) |

## Итог приёмки (исторический прогон Developer)

Результаты ниже получены на **прежней Brave-версии** дня 17 (до миграции на
Tavily по решению Architect) и сохранены как история. Числа тестов и статусы
относятся к той версии; они не подтверждают Tavily-версию и не переписываются
как факт для неё. Повторный прогон Tavily-версии выполняет Developer/Tester
отдельно.

| Проверка | Команда | Результат (Brave-версия) |
| --- | --- | --- |
| Unit + in-process MCP | `.\test.bat` | `UNIT_STATUS: PASS` — 295 тестов, 32 skipped (полный браузерный DOM-набор, включая `key_isolation`) |
| Smoke (MCP+backend+search INT) | `.\smoke_test.bat` | `MCP_INTEGRATION_STATUS: PASS` (11), `BACKEND_INTEGRATION_STATUS: PASS` (14), `SEARCH_INTEGRATION_STATUS: PASS` (7) |
| LIVE + UI поиска и дня 16 | `.\test.bat acceptance` | PASS (exit 0): `MCP_UNAVAILABLE_UI_STATUS: PASS`, live MCP PASS, `LIVE_LLM_STATUS: PASS`, `SEARCH_LIVE_STATUS: PASS`, `SEARCH_UI_STATUS: PASS`, `key_isolation: true` |
| UI arithmetic (день 16, регрессия) | `.\test.bat live ui` | `LIVE_LLM_STATUS: PASS`, `UI_E2E_STATUS: PASS` |
| REAL Brave (историческая Brave-версия) | ручной `.venv\Scripts\python.exe harness\brave_live.py` | **не запускался**: без ключа/явного разрешения возвращал `BRAVE_STATUS: BLOCKED` (exit 2) |
| REAL Tavily (текущая версия) | ручной `.venv\Scripts\python.exe harness\tavily_live.py` | **не запускался**: без ключа/явного разрешения возвращает `TAVILY_STATUS: BLOCKED` (exit 2); реальный Tavily-ключ не читался |

В историческом (Brave) прогоне внутри `test.bat acceptance` шаг unit сообщал
279 тестов: компонент acceptance
запускает unit-набор через `sanitized_env()` без browser-путей, поэтому
`MarkdownRendererDomTest` пропускается классом (1 skip вместо 16 прогонов).
Это штатное поведение дня 16, не регрессия; полный набор — `.\test.bat`.
LIVE-шаг сообщает `urls_found` = 3 (`docs.example.test/1..3`) и
`key_isolation: true`.

### Детализация по критериям

* **Проверено UNIT**: D17-01..D17-08, D17-11 (только текст промпта), D17-15
  (`not_configured`), плюс логика `SEARCH_TRACE_CHAIN`/`verify_tools_listed`/
  `verify_search_sse`/`key_is_isolated` в `tests/test_harness.py` и opt-in гейт
  `harness/tavily_live.py`.
* **Проверено INT** (`smoke_test.bat`, реальный MCP-процесс + реальный HTTP +
  loopback `FakeSearchServer`): D17-01, D17-02, D17-03, D17-05, D17-06, D17-07,
  D17-08.
* **Проверено LIVE** (`test.bat acceptance`): D17-09 — реальный Qwen вызвал
  `search_web`; trace-цепочка `mcp_connect → mcp_list_tools → model_request
  (tool_selection) → tool_selected(search_web) → tool_completed(ok, непустой
  result) → model_request(final_answer) → request_done(ok)`; `tools/list`
  содержит `search_web`; SSE без `error`; в ответе 3 URL из tool-результата;
  `key_isolation: true` (ключ отсутствует в SSE-тексте, `trace.jsonl`,
  `mcp_server.log`, `backend.log`).
* **Проверено UI** (`test.bat acceptance`, `SEARCH_UI_STATUS: PASS`): 2 anchors
  в `.text` с href `https://docs.example.test/1..2`, `details.technical` закрыт,
  loader удалён, один пузырь ответа.
* **D17-11**: формулировки `SYSTEM_PROMPT` покрыты UNIT; фактическое поведение
  модели просмотрено вручную по скриншоту ответа («Here's what the web search
  returned:» со ссылками, без утверждений о прочтении страниц) — автотестом не
  покрыто.
* **Проверено LIVE/UI для дня 16** (регрессия архитектуры): `test.bat live ui`
  зелёный, `agent/provider.py` не изменён (D17-12).
* **НЕ проверено**: D17-14 (реальный Tavily) — не запускался, PASS не заявлен;
  D17-15 UI/no-`.env`-часть — только UNIT `not_configured`.

Mock-проверки не заменяют LIVE и UI уровни; реальный Tavily не считается
проверенным без отдельного разрешения пользователя.


