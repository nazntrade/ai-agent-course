# ACCEPTANCE — Day 19: композиция MCP-инструментов

Уровни проверки: **UNIT** (без сети), **INT** (реальные процессы MCP и backend +
реальный HTTP + loopback `FakeSearchServer` + тестовая SQLite), **RESTART**
(фактический перезапуск процессов), **LIVE** (реальная локальная модель),
**UI** (Playwright + системный браузер), **REAL** (настоящий Tavily, только
opt-in), **VPS** (ручной чек-лист).

| ID | Критерий | Уровень | Проверка |
| --- | --- | --- | --- |
| D19-01 | В `tools/list` 9 инструментов; 2 новых возвращают structured | UNIT + INT | `tests/test_mcp_inprocess.py`, `tests/test_tools.py`; `tests/integration/test_reports_live.py` |
| D19-02 | topic/query и URL совпадают на входе `search_web`, в дайджесте, в `save_report` и в сохранённом отчёте | UNIT + INT | `tests/test_reports.py`; `tests/integration/test_reports_live.py` (`test_identity_is_preserved_across_the_chain`) |
| D19-03 | Пустая выдача → digest `status: empty`, `save_report` отклонён, отчёт не создан | UNIT + INT | `tests/test_reports.py`; `tests/integration/test_reports_live.py` |
| D19-04 | Ошибка Tavily (401/429/timeout/not configured) → обработка/сохранение недостижимы, ложного отчёта нет | UNIT + INT | `tests/test_reports.py`, `tests/test_web_search.py`; `tests/integration/test_reports_live.py` |
| D19-05 | Дубликаты URL (fragment/регистр/завершающий `/`) дедуплицируются; недублирующийся `#fragment` остаётся в отображаемом URL | UNIT + INT | `tests/test_reports.py` (`test_unique_fragment_url_keeps_the_fragment_in_the_output`, `test_urls_differing_only_by_fragment_or_slash_are_deduplicated`); `tests/integration/test_reports_live.py` |
| D19-06 | Некорректные/слишком большие данные → санитайзинг (`html.unescape`, zero-width, bare URL, markup runs), лимиты, контролируемые `ToolError`; пустая/полностью отфильтрованная выдача → `status:"empty"` | UNIT + INT | `tests/test_reports.py` |
| D19-07 | Небезопасный/произвольный путь невозможен; чужой чат недостижим; выход из каталога невозможен | UNIT + INT | `tests/test_reports.py`, `tests/test_reports_api.py`; `tests/integration/test_reports_live.py` |
| D19-08 | Текст-инструкция внутри сниппета — инертные данные | UNIT + INT | `tests/test_reports.py`; `tests/integration/test_reports_live.py` |
| D19-09 | Нет ложного «сохранено» при ошибке/пустой выдаче; `digest_id` обязателен; `digest_id` привязан к каноническому (URL-сортированному) summary и normalized URL | UNIT + INT + LIVE | `tests/test_reports.py`; `tests/integration/test_reports_live.py`; `COMPOSITION_LIVE_STATUS` |
| D19-10 | 3-шаговая цепочка + финальный ответ проходит на дефолте; цикл ограничен | UNIT | `tests/test_orchestrator.py` (`ToolCompositionTest`, `test_default_round_limit_is_five`) |
| D19-11 | Отчёты переживают F5, закрытие/открытие браузера, рестарт backend и MCP | RESTART + UI | `harness/reports_restart.py` (`REPORTS_RESTART_STATUS`); `run_ui_reports_e2e` (reload) |
| D19-12 | Чат-изоляция отчётов | UNIT + INT + UI | `tests/test_reports_api.py`; `tests/integration/test_reports_live.py`; `REPORTS_UI_STATUS` (switch) |
| D19-13 | Удаление чата каскадно удаляет отчёты (БД + API 404) | UNIT + INT + UI | `tests/test_storage.py`, `tests/test_reports_api.py`; `tests/integration/test_reports_live.py` |
| D19-14 | HTTP API отчётов и OpenAPI 3.1 обновлены, drift зелёный | UNIT | `tests/test_reports_api.py`, `tests/test_openapi_snapshot.py`; `test.bat openapi` |
| D19-15 | UI `Saved reports`, открытие отчёта, empty state, Refresh, появление без F5, переключение чата | UI + source-guard | `tests/test_reports_ui.py`; `REPORTS_UI_STATUS` |
| D19-16 | LIVE: одно сообщение → 3 настоящих вызова → ответ с доступом к отчёту; отчёт виден через API/UI | LIVE + UI | `COMPOSITION_LIVE_STATUS`, `REPORTS_UI_STATUS` |
| D19-17 | LIVE «найди и кратко расскажи» → search+digest, отчёт НЕ создан | LIVE | `COMPOSITION_NO_SAVE_LIVE_STATUS` |
| D19-18 | Дни 16–18 сохранены (инструменты, SSE, Technical details, задания/планировщик, лимит 5 чатов, история, discovery) | UNIT + INT + LIVE + UI | `test.bat`; `smoke_test.bat`; `MCP_UNAVAILABLE_UI_STATUS`, `SEARCH_*`, `TASKS_*`, `CHATS_UI_STATUS` |
| D19-19 | Реальный Tavily только opt-in, ≤ 5 запросов, фактическое число | REAL | `.venv\Scripts\python.exe harness\reports_real_live.py` (`REPORTS_REAL_ALLOW=1`) |
| D19-20 | VPS — отчёты в `AGENT_DB_PATH` вне деплой-каталога; чек-лист оператора | VPS | ручной чек-лист SPEC §9 |
| D19-21 | Ключ Tavily отсутствует в отчётах/HTTP/trace/UI/логах | UNIT + INT | `tests/test_web_search.py`, `tests/test_tools.py`; `tests/integration/test_reports_live.py`; `KEY_ISOLATION` |
| D19-22 | Nav/boilerplate-сниппеты (`####`, `\|`, префиксы `Skip to content` и т. п.) отсеиваются; в дайджесте нет `#`/`\|`; сохраняется ≤ 3 пунктов в порядке выдачи | UNIT + INT | `tests/test_reports.py` (`test_noisy_results_are_filtered_and_only_clean_ones_remain`, `test_at_most_three_clean_items_are_kept_in_search_order`, `test_nav_prefix_snippet_is_filtered`); `tests/integration/test_reports_live.py` (`test_noisy_search_is_filtered_and_saves_clean_summary`, `test_many_results_are_truncated_to_three`) |
| D19-23 | Первый `save_report` принимает переформатированный или отсутствующий `summary` (сервер пересобирает канонический); подмена topic/URL/текста sources → `digest_id` mismatch, 0 записей | UNIT + INT | `tests/test_reports.py` (`test_reformatted_summary_still_saves_canonical_summary`, `test_missing_summary_still_saves`, `test_source_text_tampering_is_rejected`, `test_topic_or_url_tampering_is_rejected`); `tests/integration/test_reports_live.py` (`test_first_save_accepts_a_reformatted_summary`, `test_tampered_topic_is_rejected_over_mcp`, `test_tampered_source_title_is_rejected_over_mcp`, `test_tampered_source_url_is_rejected_over_mcp`) |
| D19-24 | Markdown-рендер: continuation-строки и одна пустая строка между пунктами не разрывают `<ol>`; первый номер задаёт `ol.start`; новый список после абзаца — отдельный | UNIT (DOM) | `tests/test_markdown_ui.py`; `tests/test_reports_ui.py` (guard `<ol>`) |
| D19-25 | LIVE/UI: ответ с 3 пунктами рендерится как один список 1,2,3; отчёт показывает нумерованные ссылки | LIVE + UI | `run_ui_search_e2e` (`ordered_lists/ordered_items/ordered_starts`), `run_ui_reports_e2e` (`REPORTS_UI_STATUS`) |

## Требования к приёмке

* Уровни не заменяются более слабыми: INT не считается за RESTART, LIVE не
  подменяется UNIT-тестом с фейком, REAL не заявляется PASS без ключа и
  разрешения.
* Автотесты не тратят платные вызовы; реальный Tavily — только D19-19.
* Тестовая БД — только в temp/`.runs`; реальная `data/` не трогается.

## Итог приёмки (Developer, ревизия 2 + покрытие ревизии 3)

| Проверка | Команда | Результат |
| --- | --- | --- |
| UNIT | `test.bat` | `UNIT_STATUS: PASS` — 554 теста OK, 58 skipped, exit 0 (58 skipped — только `tests/integration/*` при `RUN_LIVE_MCP` не задан; DOM-тесты Markdown и ref используемого браузера выполнялись) |
| UNIT (шаг acceptance) | `test.bat acceptance` | `UNIT_STATUS: PASS` — 525 тестов OK, 61 skipped (без браузерных DOM-классов, см. пояснение ниже) |
| INT + RESTART | `smoke_test.bat` | `MCP_INTEGRATION_STATUS: PASS` (11), `BACKEND_INTEGRATION_STATUS: PASS` (16), `SEARCH_INTEGRATION_STATUS: PASS` (7), `TASKS_INTEGRATION_STATUS: PASS` (9), `REPORTS_INTEGRATION_STATUS: PASS` (15), `PERSISTENCE_RESTART_STATUS: PASS`, `SCHEDULER_RESTART_STATUS: PASS`, `REPORTS_RESTART_STATUS: PASS` |
| LIVE + UI | `test.bat acceptance` | `MCP_UNAVAILABLE_UI_STATUS: PASS`, `LIVE_LLM_STATUS: PASS`, `SEARCH_LIVE_STATUS: PASS`/`SEARCH_UI_STATUS: PASS`, `TASKS_LIVE_STATUS: PASS`/`CHATS_UI_STATUS: PASS`/`TASKS_UI_STATUS: PASS`, `COMPOSITION_LIVE_STATUS: PASS`, `COMPOSITION_NO_SAVE_LIVE_STATUS: PASS`, `REPORTS_UI_STATUS: PASS`, `key_isolation: true` |
| REAL Tavily | `.venv\Scripts\python.exe harness\reports_real_live.py` | `REPORTS_REAL_STATUS: BLOCKED` — не запускался (нет `REPORTS_REAL_ALLOW=1`/разрешения) |
| VPS | ручной чек-лист SPEC §9 | `BLOCKED` (оператор) |

Расхождение счётчиков `test.bat` и unit-шага `test.bat acceptance` (было
549 против 520, стало 554 против 525 после регрессионных тестов ревизии 3;
разница неизменна — 29) объяснимо и не является пропуском продуктовых тестов
дня 19: acceptance запускает unit-набор через `sanitized_env()`, который
оставляет только allow-list переменных и тем самым убирает браузерные пути
(`PROGRAMFILES`, `PROGRAMFILES(X86)`, `PROGRAMW6432`, `HOMEDRIVE`). Без них
`qa_browser.launch_browser` бросает `PrerequisiteError` в `setUpClass`, и три
DOM-класса (`MarkdownRendererDomTest` — 21, `SearchSpinnerBrowserTest` — 5,
`LinkStyleBrowserTest` — 3, всего 29) пропускаются целиком: каждый класс даёт
ровно один skip (skipped 58 → 61), а его методы не попадают в `Ran`
(554 − 29 = 525). Все остальные модули выполняются в обоих прогонах. Причина
проверена по коду `harness/acceptance.py` (`_run` → `sanitized_env`) и
`harness/processes.py` (`KEEP_ENV_KEYS`).

Доказательства ревизии 2 и покрытия ревизии 3:

* `REPORTS_INTEGRATION_STATUS: PASS` (15) включает `test_noisy_search_is_filtered_and_saves_clean_summary`
  (2 из 4 результатов, `filtered_removed = 2`, нет `#`/`|`), `test_many_results_are_truncated_to_three`
  (`count = 3`, `truncated = true`), `test_first_save_accepts_a_reformatted_summary`
  (первый `save_report` с переписанным `summary` принят, в БД серверный canonical summary), а также
  INT-регрессии подмены через реальный MCP: `test_tampered_topic_is_rejected_over_mcp`,
  `test_tampered_source_title_is_rejected_over_mcp`, `test_tampered_source_url_is_rejected_over_mcp`
  (ответ `ok = false` с `digest_id`-mismatch, `GET /api/chats/{chat_id}/reports` → `count: 0`).
* UNIT-регрессии fragment-границы (SPEC §10): `test_unique_fragment_url_keeps_the_fragment_in_the_output`
  и `test_urls_differing_only_by_fragment_or_slash_are_deduplicated` — отображаемый URL сохраняется
  как данные, дедупликация идёт по `normalize_url` только как по ключу.
* UI search E2E (`.runs/20260925-172446-live-e2e-search/report.json`): `ui_channel: msedge`,
  `ordered_lists: 1`, `ordered_items: 2`, `ordered_starts: [1]`, `ui_e2e_status: PASS` —
  фактический ответ модели содержал **2 пункта**, отрендеренные как один `<ol>`.
  Отдельные DOM-тесты в `tests/test_markdown_ui.py` подтвердили 3 пункта с
  continuation `Source:` в одном `<ol>` с `start = 1`, `3.` → `start = 3`,
  пустые строки (loose list) и новый список после абзаца. Эти тесты не
  подтверждают критерий D19-25 на уровне LIVE с ровно тремя пунктами.
* Композиция LIVE (`.runs/20260925-172728-live-e2e-composition/report.json`):
  `save_report` принят первым вызовом, `stored_urls` = 3, `identity_ok: true`;
  `REPORTS_UI_STATUS: PASS` (отчёт открыт, 2 ссылки, F5/Refresh/switch).

LIVE-прогоны выполнил `harness/live_e2e.py`, который сам поднял и остановил
локальную модель. По артефакту `.runs/20260925-162745-live-e2e-composition/`:
запрошенная модель `qwen3.8-27b-local` на loopback-endpoint,
`started_by_harness: true`, `model_ready: true`, `probe_model_count: 1`,
`qa_model_configured: qwen3.8-27b-iq4xs`.

Фактическая цепочка сценария A (trace, request `02d70ac367f14f92beb8806549b9fd69`):

```
mcp_connect(ok) → mcp_list_tools(9) → model_request(tool_selection)
→ tool_selected(search_web, "Kotlin news latest") → tool_completed(ok)
→ tool_selected(digest_search_results) → tool_completed(ok, digest_id d19-b9248f3fe21114e0)
→ tool_selected(save_report) → tool_completed(ok, report_id 915446943194)
→ model_request(final_answer) → request_done(ok)
```

Сохранённый отчёт: `topic = "Kotlin news latest"`, `source_count = 3`,
URL источников совпадают с `RESULT_URLS` фейкового поиска
(`https://docs.example.test/1..3`, `identity_ok: true`). Сценарий B
(`request 1cfbc16d03c74581baa5187432be7e84`) выполнил `search_web` +
`digest_search_results` и **не** вызвал `save_report`; число отчётов в чате — 0.

## Независимая приёмка

Выполняется Tester (`TEST_STATUS` в итоговом отчёте Coordinator). Уровни
REAL (D19-19) и VPS (D19-20) остаются `BLOCKED` без явного opt-in/оператора.
D19-25 подтверждён частично: DOM-тест проверил список 1,2,3, а LIVE/UI
показал два пункта; LIVE/UI-проверка ровно трёх пунктов ещё не выполнена.
