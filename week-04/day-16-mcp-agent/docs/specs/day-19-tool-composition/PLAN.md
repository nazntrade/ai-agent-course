# PLAN — Day 19: композиция MCP-инструментов

План выполнен по архитектурному решению Architect. Пункты трассируются к
критериям `ACCEPTANCE.md`.

## 1. Новые и изменённые файлы

| Файл | Статус | Назначение |
| --- | --- | --- |
| `storage/db.py` | EDIT | `SCHEMA_VERSION = 3`, таблица `reports`, `MIGRATIONS[3]` |
| `storage/reports.py` | NEW | `ReportRepository` (insert + prune, list, get, безопасный разбор `sources_json`) |
| `mcp_server/reports.py` | NEW | `normalize_url`, `sanitize_sources`, `build_summary`, `compute_digest_id`, `build_digest`, `ReportService.digest/save`, ленивый `default_report_service` |
| `mcp_server/tools.py` | EDIT | 2 тонких инструмента с model-facing docstrings; прежние не изменены |
| `mcp_server/server.py` | EDIT | регистрация 9 инструментов |
| `agent/orchestrator.py` | EDIT | `DEFAULT_MAX_TOOL_ROUNDS = 5`, `SYSTEM_PROMPT` (+ композиция) |
| `agent/settings.py` | EDIT | `AGENT_MAX_TOOL_ROUNDS` (clamp 2..10, default 5) |
| `agent/server.py` | EDIT | передача `max_tool_rounds`; endpoints отчётов и Pydantic-модели |
| `agent/chats.py` | EDIT | `reports_for_chat`, `report_for_chat`, категория `report_not_found` |
| `static/index.html` | EDIT | панель `Saved reports` |
| `static/app.js` | EDIT | `loadReports`/`renderReports`, ленивая деталь, safe-рендер |
| `static/styles.css` | EDIT | стили `.reports`, `.report-card`, `.report-summary` и др. |
| `.env.example` | EDIT | `AGENT_MAX_TOOL_ROUNDS=5` |
| `tests/test_reports.py` | NEW | UNIT сервиса и репозитория |
| `tests/test_reports_api.py` | NEW | UNIT HTTP API отчётов |
| `tests/test_reports_ui.py` | NEW | source-guard UI |
| `tests/test_storage.py` | EDIT | v3, миграция v2→v3, каскад `reports` |
| `tests/test_mcp_inprocess.py` | EDIT | 9 инструментов, композиция через реальный SDK |
| `tests/test_tools.py`, `tests/test_orchestrator.py`, `tests/test_settings.py`, `tests/test_openapi_snapshot.py`, `tests/test_harness.py` | EDIT | делегирование, 3-шаговая цепочка, лимит раундов, новые пути и статусы |
| `tests/support/fakes.py` | EDIT | 9 инструментов |
| `tests/support/fake_search.py` | EDIT | маркер `__test_duplicates__` |
| `tests/integration/test_reports_live.py` | NEW | INT: реальный MCP + loopback поиск + реальный HTTP + общая БД |
| `tests/integration/test_backend_live.py`, `test_mcp_live.py`, `test_tasks_live.py` | EDIT | счётчики 7 → 9 |
| `harness/live_mcp.py` | EDIT | `REPORTS_INTEGRATION_STATUS`, `REPORTS_RESTART_STATUS` |
| `harness/reports_restart.py` | NEW | рестарт backend и MCP, отчёт остаётся |
| `harness/reports_real_live.py` | NEW | REAL opt-in (`REPORTS_REAL_ALLOW=1`) |
| `harness/live_e2e.py` | EDIT | `--scenario composition`, `run_ui_reports_e2e`, `COMPOSITION_LIVE_STATUS`, `COMPOSITION_NO_SAVE_LIVE_STATUS`, `REPORTS_UI_STATUS` |
| `harness/acceptance.py` | EDIT | шаг composition + агрегация |
| `docs/openapi.json` | EDIT (regen) | новые endpoints отчётов |
| `docs/specs/day-19-tool-composition/*` | NEW | этот комплект |
| `README.md` (week-04) | EDIT | раздел «День 19» |

Ни один `.bat` не изменяется.

## 2. Порядок реализации

1. Хранилище: `storage/db.py`, `storage/reports.py` → `tests/test_reports.py`,
   `tests/test_storage.py`.
2. Обработка/сохранение: `mcp_server/reports.py`, `mcp_server/tools.py`,
   `mcp_server/server.py` → `tests/test_tools.py`, `tests/test_mcp_inprocess.py`.
3. Лимит раундов и промпт: `agent/orchestrator.py`, `agent/settings.py`,
   `agent/server.py`, `.env.example` → `tests/test_orchestrator.py`,
   `tests/test_settings.py`.
4. HTTP API: `agent/chats.py`, `agent/server.py` → `tests/test_reports_api.py`,
   `tests/test_openapi_snapshot.py`.
5. UI: `static/*` → `tests/test_reports_ui.py`.
6. INT/RESTART: `tests/support/*`, `tests/integration/test_reports_live.py`,
   `harness/live_mcp.py`, `harness/reports_restart.py`.
7. LIVE + UI + REAL: `harness/live_e2e.py`, `harness/acceptance.py`,
   `harness/reports_real_live.py`, `tests/test_harness.py`.
8. Документация: `docs/openapi.json` (`test.bat openapi`), README, этот комплект.
9. Полный прогон: `test.bat` → `smoke_test.bat` → `test.bat acceptance`.

## 3. Ключевые технические решения

* **Детерминированный digest без модели и БД.** Композицию собирает backend:
  модель лишь передаёт whole-объекты между шагами.
* **Whole-object как аргумент.** `digest_search_results` принимает весь
  результат `search_web`, `save_report` — весь digest; UI-copy аргументов
  фильтрует вложенные объекты (`_arguments_for_ui`), поэтому в trace видно `{}`,
  а реальные аргументы уходят как есть.
* **Доверенные границы.** `save_report` не имеет аргумента пути; запись только в
  `reports` текущего `chat_id`; `digest_id` пересчитывается перед записью.
* **Один писатель на таблицу** (матрица владения как в дне 18): MCP пишет
  `reports`, backend читает и каскадно удаляет через `chats`.
* **Ленивое открытие БД** сохраняется: `tools/list` и `digest_search_results`
  файл не создают.
* **Промпт как гарантия порядка.** Реальная модель не детерминирована, поэтому
  `SYSTEM_PROMPT` явно требует обязательный `save_report` при слове «save»
  и запрещает повторный `search_web`.

## 4. Тесты по уровням

* **UNIT**: сервис/repo отчётов, HTTP API, source-guard UI, schema v3, 9
  инструментов, 3-шаговая цепочка с дефолтным лимитом, `tool_round_limit`,
  промпт, settings clamp, OpenAPI drift, harness-верификаторы.
* **INT**: `tests/integration/test_reports_live.py` — реальный MCP + loopback
  `FakeSearchServer` + реальный HTTP + общая тестовая БД: 9 инструментов,
  identity topic/URL через цепочку, пустая выдача, ошибка Tavily, дубликаты,
  инъекция в сниппете, изоляция чатов, каскад, отсутствие ложного «сохранено».
* **RESTART**: `harness/reports_restart.py` — рестарт backend и MCP, отчёт
  остаётся доступным.
* **LIVE + UI**: `harness/live_e2e.py --scenario composition --ui` — одно
  сообщение → 3 настоящих вызова → финальный ответ; сценарий B не создаёт
  отчёт; панель `Saved reports`.
* **REAL**: `harness/reports_real_live.py` (opt-in `REPORTS_REAL_ALLOW=1`).
* **VPS**: ручной чек-лист оператора (SPEC §9).

## 5. Точки входа

```
test.bat                  UNIT
smoke_test.bat            INT + RESTART (REPORTS_INTEGRATION_STATUS, REPORTS_RESTART_STATUS)
test.bat acceptance       LIVE + UI (COMPOSITION_LIVE_STATUS, COMPOSITION_NO_SAVE_LIVE_STATUS, REPORTS_UI_STATUS)
test.bat openapi          обновление docs/openapi.json
Ручной opt-in REAL:       .venv\Scripts\python.exe harness\reports_real_live.py
```

## 6. Риски и меры

| Риск | Мера |
| --- | --- |
| Модель не вызовет digest/save | явный обязательный порядок в `SYSTEM_PROMPT` и imperative docstring `save_report`; LIVE-проверка |
| Модель не передаст объект unchanged | docstrings требуют unchanged; identity URL проверяется INT и LIVE |
| Ложное «сохранено» | `save_report` отклоняет не-ok digest и несовпадение `digest_id` |
| Разметка в сниппетах | очистка символов, лимиты, `textContent`, ссылки только http/https |
| Регрессия дней 16–18 | полный набор UNIT/INT/LIVE сохранён, счётчики инструментов обновлены |

## 7. Ревизия 2 (дельта к реализации)

Причина — лог `logs/trace.jsonl`, request `2f43eb0e…`: `digest_id` биндил
`summary`, а модель переформатировала длинный многострочный текст; nav/markdown
(`####`, `|`) проходили в дайджест; markdown-рендер разрывал `<ol>` на
continuation-строках и нумеровал каждый блок с 1.

Изменения (без изменения `.bat`, governance, `storage/db.py`,
`storage/reports.py`, `agent/server.py`, `agent/chats.py`, `docs/openapi.json`):

* `mcp_server/reports.py`: константа `MAX_SOURCES = 3`, `NAV_MAX_LENGTH = 120`,
  `NAV_PREFIXES`; `_clean_text` (html.unescape, zero-width, bare URL, markup
  runs); nav/boilerplate-фильтр (`filtered_removed`); дедуп после фильтра;
  `canonical_digest` — `digest_id` по каноническому (URL-сортированному)
  summary; `save` больше не сохраняет `summary` модели, а пересобирает его из
  санитизированных sources; порядок валидации a′; `EMPTY_SUMMARY_MESSAGE`
  удалён.
* `mcp_server/tools.py`: docstrings digest/save (до 3 пунктов, nav отсеивается,
  сервер пересобирает summary).
* `static/markdown-render.js`: `ORDERED_RE` с захватом номера, `ol.start`,
  continuation-строки и одна пустая строка (loose list) внутри одного списка.
* `static/app.js`: источники отчёта — `<ol class="report-sources">`.
* `agent/orchestrator.py`: промпт — один ordered-список без перезапуска
  нумерации; digest передаётся в `save_report` без правок.
* Тесты/фикстуры: `NOISY_SEARCH_RESULT`/`NOISY_RESULTS` (`example.test`),
  nav-фильтрация и кап ≤ 3, save при переформатированном/отсутствующем summary,
  подмена → отказ, DOM-тесты `<ol>`, guard `createElement("ol")`, проверки
  промпта, `ordered_lists/ordered_items/ordered_starts` в UI search E2E.

Новые критерии — D19-22…D19-25 (см. `ACCEPTANCE.md`).
