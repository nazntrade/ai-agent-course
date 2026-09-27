# ACCEPTANCE — Day 20: межсерверная оркестрация MCP (мониторинг и уведомления)

Уровни проверки: **UNIT** (без сети, `test.bat`), **INT** (реальные процессы A и
B + реальный HTTP + `tools/list` каждого + loopback-фейк Telegram,
`smoke_test.bat`), **RESTART** (фактический перезапуск B, `harness/`),
**LIVE** (реальная локальная модель, `test.bat acceptance`), **UI** (Playwright +
системный браузер), **REAL** (настоящий Telegram, только opt-in), **VPS**
(ручной чек-лист).

Каждый критерий трассируется к реализации в `PLAN.md` и к решению в `SPEC.md`.
Команды и harness-шаги дня 20 встраиваются в существующие режимы; `.bat` не
меняются.

## 1. Таблица критериев

| ID | Требование | Уровень | Команда / harness-шаг | Ожидаемый наблюдаемый результат |
| --- | --- | --- | --- | --- |
| D20-01 | Два процесса: A (`python -m mcp_server`, 8765, инструменты не изменены) и B (`python -m notifier_server`, 8766, `/mcp`, свой `tools/list`); B не импортирует `agent.*` | UNIT + INT | `tests/test_notifier_server.py`; `smoke_test.bat` (шаг B) | Оба процесса поднимаются, `tools/list` каждого непустой; B без импорта `agent.*` (source-guard) |
| D20-02 | `McpHub` реализует `McpClient`: `probe_servers()` (параллельно) / `probe_server(label)` → `ServerProbe(label,status,tools)`; протокольные методы A-first; `McpStatus`/`FakeMcpClient` не меняются; коллизия имён → `connected:false`/`protocol`; пустой `MCP_NOTIFIER_URL` → `not_configured`, B не опрашивается; `McpTool.server` default `None` | UNIT | `tests/test_mcp_hub.py` | `ServerProbe` на каждый сервер; объединённый список с `server`; коллизия → controlled `protocol`; пустой URL не делает сетевого вызова; `FakeMcpClient` работает без `server` |
| D20-03 | `Orchestrator` пишет `server` в `tool_selected`/`tool_completed` (и SSE только когда сервер известен); `mcp_connect` — по записи на сервер, `mcp_list_tools` — агрегат с `per_server`; legacy без hub байт-совместим с днями 16–19 | UNIT + LIVE | `tests/test_orchestrator.py`; `MCP_PROBE_REGRESSION`; `NOTIFICATIONS_LIVE_STATUS` | Две записи `mcp_connect` (A и B), одна `mcp_list_tools` с `per_server={"A":9,"B":6}`; в trace/SSE `server=A|B`; `MCP_PROBE_REGRESSION` ожидает **4** POST (= 2 × сервера) |
| D20-04 | При недоступном/несконфигурированном B поведение дней 16–19 сохраняется (работает только A); B-down ветки не дают 5xx | UNIT + INT | `tests/test_mcp_hub.py`, `tests/test_orchestrator.py`, `tests/test_notifier_api.py`; `smoke_test.bat` | A отвечает, чат на A проходит; `/api/mcp/servers` → B `connected:false`; `/api/chats/{id}/watches` → HTTP 200 `available:false`; без 5xx |
| D20-05 | В `tools/list` B 6 инструментов; каждый возвращает structured | UNIT + INT | `tests/test_notifier_tools.py`; `smoke_test.bat` (`NOTIFIER_INTEGRATION_STATUS`) | Ровно 6 имён: `create_notification_watch`, `list_notification_watches`, `evaluate_run`, `send_notification`, `get_delivery_status`, `stop_notification_watch`; каждый ответ — dict |
| D20-06 | `create_notification_watch` сохраняет явный критерий и расписания, возвращает их | UNIT + INT | `tests/test_notifier_watches.py`; `NOTIFIER_INTEGRATION_STATUS` | Ответ содержит `keywords`/`exclude`/`interval_seconds`/`summary_interval_seconds`; повторное чтение возвращает те же значения |
| D20-07 | `evaluate_run` принимает весь результат A, детерминированно применяет критерий, возвращает `{status,new_items,matched_count,known_count,is_baseline,should_notify,note}`; `error`/`empty`/`pending` честно; malformed `run`/неизвестный status → structured `error`, чужой watch → `unknown_watch`, **без** `ToolError`; `pending`/`error` двигают только `next_check_at` (без `last_check_at`), baseline (`ok`/`empty`) и проверка без новых items двигают `last_check_at`/`next_check_at`; при `should_notify=true` расписание не двигается | UNIT + INT | `tests/test_notifier_watches.py` | Ответ содержит все поля; на `error`/`empty`/`pending` `new_items: []`, `should_notify: false`; malformed/чужой watch → structured status, исключения нет; при `should_notify=true` `next_check_at` и `last_check_at` не изменились |
| D20-08 | Baseline поглощается **только** первым прогоном со `status in {"ok","empty"}`: `is_baseline=true, should_notify=false`; `pending`/`error` baseline не поглощают; старая выдача не рассылается | UNIT + INT | `tests/test_notifier_watches.py` | `ok`/`empty` поглощают baseline, seen вырос, доставки нет; `pending`/`error` двигают только `next_check_at`, `last_check_at` остаётся `NULL`, поэтому повторный достоверный прогон снова baseline |
| D20-09 | `send_notification` — единственная точка отправки и записи seen/delivery; `duplicate` только при существующей `status='sent'`; `failed`/`not_configured` → `UPDATE` (`attempts+1`, `updated_at`); `not_required` строку не создаёт; stopped/чужой watch → `ToolError`; двигает расписание; `delivery_id` | UNIT + INT + LIVE | `tests/test_notifier_watches.py`, `tests/integration/test_notifier_live.py`; `NOTIFICATIONS_LIVE_STATUS` | Повтор терминального ключа → `duplicate`; после `failed` повтор создаёт попытку, а не вечный дубль; `summary` `period_key=floor(now/summary_interval_seconds)`; LIVE PASS только при `send_notification.structured.status=="sent"` **и** URL из `tests/support/fake_search.py::RESULT_URLS` в payload fake Telegram |
| D20-10 | Seen-отпечатки пишутся только при фактической обработке; неполный ход не «съедает» уведомление; retry-семантика по статусам | UNIT | `tests/test_notifier_watches.py`, `tests/test_monitor.py` | Если `send_notification` не вызывался вообще — расписание не двинуто, следующий тик повторяет тот же чек; при `failed`/`not_configured` seen-set не записан, items остаются новыми, расписание двинуто, повтор по `interval_seconds`; `sorted`-набор seen до/после `evaluate_run` равен |
| D20-11 | Отдельная БД B (`NOTIFIER_DB_PATH`), таблицы `watches`/`seen_items`/`deliveries`, UNIQUE `(watch_id, kind, period_key)`, своё версионирование; не пишет в БД A | UNIT | `tests/test_notifier_db.py` | Схема создаётся в отдельном файле; повторный INSERT ключа отклоняется; БД A не открывается/не меняется |
| D20-12 | Состояние B переживает рестарт; повтор результата/прогона/рестарт не создаёт дубль | RESTART | `harness/notifier_restart.py` (`NOTIFIER_RESTART_STATUS`) | После рестарта B подписки/seen/deliveries на месте; повторный send → `duplicate` |
| D20-13 | Telegram: токен/получатель читает только B; получатель не аргумент модели; единственная сетевая граница `notifier_server/telegram.py`; токен вне `repr`/чата/trace/логов/репозитория; пустой токен → `not_configured` без сети | UNIT + INT | `tests/test_notifier_telegram.py`; `tests/integration/test_notifier_live.py` | `repr(config)` без токена; пустой токен → `not_configured` и 0 сетевых вызовов; trace/лог не содержат токен |
| D20-14 | Монитор: перечисляет `ChatService.list_chats()` + host→B `list_notification_watches`, фильтрует `active`/`next_check_at<=now`; при первом B-down прерывает тик (без модели/БД B); `Orchestrator.run(..., trigger, watch_id, system_prompt, allowed_tools, require_result)`; неполный ход → только `monitor_incomplete{watch_id,reason}` вместо `request_done`; `request_start.trigger`; тик default 30 c (clamp 1..3600); `NOTIFIER_MONITOR_ENABLED` default on | UNIT + LIVE | `tests/test_monitor.py`, `tests/test_orchestrator.py`; `NOTIFICATIONS_MONITOR_LIVE_STATUS` | В trace `request_start{trigger:"monitor",watch_id}`; неполный ход пишет `monitor_incomplete` (причины `evaluate_run_absent`/`send_notification_missing`/`send_notification_failed`) и не пишет `request_done`; monitor-LIVE сценарий см. §2 |
| D20-15 | HTTP: `/api/mcp/status` и `/api/mcp/tools` сохраняют семантику A; `GET /api/mcp/servers` — один `probe_servers()`; `GET /api/chats/{chat_id}/watches`: неизвестный chat → 404 `chat_not_found` (до B), B down → HTTP 200 `{available:false,error}`, не 5xx | UNIT + INT | `tests/test_notifier_api.py`, `tests/test_openapi_snapshot.py`; `NOTIFIER_INTEGRATION_STATUS` | `/api/mcp/servers` возвращает A и B одним probe; при остановленном B → `connected:false` и `/watches` → `available:false`; неизвестный chat → 404 без обращения к B; `/api/mcp/status` по-прежнему про A |
| D20-16 | UI: `MCP status` показывает оба сервера; строки `Technical details` помечены `[A]`/`[B]`; панель `Notification watches`; доставка не выдаётся за успешную по тексту модели | UI + source-guard | `tests/test_notifier_ui.py`; `NOTIFIER_SERVERS_UI_STATUS`, `NOTIFICATION_UI_STATUS` | В UI два сервера; в `Technical details` видны `[A]` и `[B]`; статус доставки берётся из B |
| D20-17 | Отпечаток по `mcp_server/reports.py::normalize_url`; логика не дублируется | UNIT | `tests/test_notifier_watches.py` | Отпечатки URL с `#fragment`/завершающим `/`/разным регистром совпадают; в B нет копии `normalize_url` (source-guard) |
| D20-18 | `.env.example` содержит новые переменные без настоящих значений, включая `NOTIFIER_MONITOR_TICK_SECONDS` (default 30 c, clamp 1..3600) и `NOTIFIER_LOAD_DOTENV` (default 1); harness передаёт `NOTIFIER_LOAD_DOTENV=0` | UNIT | `tests/test_notifier_config.py` | Ключи присутствуют; `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` пустые; процесс B читает `NOTIFIER_LOAD_DOTENV`, `agent/settings.py` не читает `TELEGRAM_*` |
| D20-19 | Ограничение «только последний прогон» закрывается seen-множеством B без новых инструментов A; пропуск исчезнувших items задокументирован | UNIT + документация | `tests/test_tools.py` (число инструментов A = 9), `tests/test_notifier_watches.py`; ревью SPEC §14 | A всё ещё 9 инструментов; seen-set накапливается между прогонами; SPEC фиксирует ограничение |
| D20-20 | LIVE: реальная локальная Qwen ведёт цепочку A→B; PASS только при `send_notification.structured.status=="sent"` **и** URL из `RESULT_URLS` в payload fake Telegram (не по одному `tool_completed.ok=true`); Tavily и Telegram — фейки; в отчёте фактическая модель | LIVE | `test.bat acceptance` (`NOTIFICATIONS_LIVE_STATUS`) | В trace реальные `tool_selected` A и B по порядку; fake Telegram‑payload содержит URL из `tests/support/fake_search.py::RESULT_URLS`; в отчёте названа фактическая `AGENT_MODEL_NAME`; отмечено, что это не модель ролей Control Center |
| D20-21 | INT: реальные процессы A и B, реальный HTTP, `tools/list` каждого, loopback-фейк Telegram | INT | `smoke_test.bat` (`NOTIFIER_INTEGRATION_STATUS`) | Оба сервера отвечают по HTTP; каждый отдаёт свой `tools/list`; fake Telegram получил вызов, сеть наружу не шла |
| D20-22 | UI (Playwright): оба сервера и строки вызовов A и B | UI | `NOTIFIER_SERVERS_UI_STATUS` | Скриншот и DOM-проверка: два сервера, строки `[A]`/`[B]` |
| D20-23 | Opt-in REAL Telegram, иначе `BLOCKED`; реальный Tavily остаётся `BLOCKED` | REAL | `harness/acceptance.py` вызывает `harness/notifier_real_live.py` (`NOTIFIER_REAL_ALLOW=1`) | Без opt-in — `NOTIFIER_REAL_STATUS: BLOCKED` (не FAIL); с opt-in — фактическая доставка и число вызовов |
| D20-24 | Внутренний прямой вызов A→B запрещён; оркестрация только через `McpHub` и модель | UNIT + LIVE | `tests/test_mcp_hub.py` (source-guard), `NOTIFICATIONS_LIVE_STATUS` | В trace цепочка идёт через `tool_selected(server)`, а не через вложенный вызов внутри сервера; в коде B нет обращений к A |
| D20-25 | Регрессии дней 16–19 сохранены | UNIT + INT + LIVE + UI | `test.bat`; `smoke_test.bat`; `test.bat acceptance` | Существующие статусы (`MCP_*`, `SEARCH_*`, `TASKS_*`, `COMPOSITION_*`, `REPORTS_*`, `*_UI_STATUS`) зелёные; инструменты A и 9 контрактов не изменены |
| D20-26 | Ж/д сценарий — только в документации; отмечает необходимость достоверного источника наличия/цены; сниппеты не считаются проверкой | Документация | ревью SPEC §17 | SPEC §17 содержит формулировку ограничения; кода нет |
| D20-27 | Не изменяются `.bat`, `deploy_vps.*`, governance-файлы, Control Center, Templates; нет VPS-развёртывания, commit/push/деплоя; автоматика не вызывает реальный Tavily/Telegram | Границы | `git status --short`; ревью diff | В diff нет перечисленных файлов; автоматические тесты не делают реальных сетевых вызовов |
| D20-28 | Накопленные за простой/рестарт монитора прогоны: A хранит до 50/задание, но обрабатывается **ровно последний**; промежуточные сознательно не «догоняются» (no backfill); items последнего (не seen) уведомляются, items только промежуточных пропущены, seen не повторяются; baseline не рассылает историческую выдачу, если ещё не поглощён | UNIT + INT | `tests/test_notifier_watches.py`, `tests/integration/test_notifier_live.py`, `harness/notifier_restart.py` | Несколько `run`-payload/прогонов → используется только последний (`new_items` из последнего, промежуточных нет); уже seen не повторяются; baseline при непоглощении не отправляет |
| D20-29 | Разные новые материалы в одном периоде не подавляются: для `new_items` `period_key` **контентный** (хэш множества отпечатков), не временной; два разных материала → два ключа и две доставки `sent`; повтор того же → `duplicate`; временной ключ только у `summary`; один вызов с двумя items — одна доставка | UNIT + INT | `tests/test_notifier_watches.py` (UNIT) + `tests/integration/test_notifier_live.py` (**INT**: реальные A+B, два разных материала → два `period_key`/две строки `deliveries` `sent`; повтор → `duplicate`, loopback-фейк Telegram) | UNIT: два разных множества в одном периоде → две строки `deliveries` `sent`; INT: через реальные A+B та же картина, повтор → `duplicate` без сети; `summary` использует `floor(now/summary_interval_seconds)`; два вызова → два сообщения |

## 2. Что подтверждается реальным запуском / fake-данными / BLOCKED

### Подтверждается реальным запуском

* **INT (D20-01, D20-05, D20-21)**: реальные процессы A и B, реальный HTTP,
  реальный `tools/list` каждого сервера. Telegram заменён loopback-фейком —
  сеть наружу не идёт.
* **RESTART (D20-12)**: фактический перезапуск процесса B и проверка
  персистентности БД B.
* **LIVE (D20-03, D20-09, D20-14, D20-20, D20-24)**: реальная локальная модель
  (Qwen) ведёт межсерверную цепочку `get_latest_search_run(A)` →
  `evaluate_run(B)` → `send_notification(B)`. Внешний Tavily и Telegram —
  фейки; это явно фиксируется в отчёте.
* **Monitor-LIVE (D20-14, D20-20)**: `NOTIFICATIONS_MONITOR_LIVE_STATUS`.
  Механизм новых items использует **существующие маркеры** фейка
  (`tests/support/fake_search.py`, `EMPTY_MARKER` и обычная выдача `RESULTS`);
  правка фейка не требуется. Шаги сценария:
   1. создать A-задание с запросом, содержащим `EMPTY_MARKER = "__test_empty__"`
      (пустая выдача); для сценария выставить `MCP_TASK_TICK_SECONDS=0.5` в
      окружении A;
   2. дождаться, пока host→A `get_latest_search_run` вернёт `status="empty"`
      (прогон реально записан, а не `pending`);
   3. **только после этого** создать watch **host→B напрямую**
      (`create_notification_watch`, `interval_seconds=60`, `next_check_at=now`), не
      через модель;
   4. дождаться первого monitor-чека — baseline: `evaluate_run` вызван,
      `is_baseline=true`, **отправки нет** (fake Telegram не получил сообщений);
   5. вызвать `stop_search_task` для пустого задания и создать **второй** A-таск с
      обычным запросом (без маркера): его `ok`-прогон отдаёт `RESULTS`
      (`RESULT_URLS`), которые подходят под `keywords` watch;
   6. дождаться второго due-чека (тик `NOTIFIER_MONITOR_TICK_SECONDS=1`,
      ожидание ≤ 75 c для `interval_seconds=60`);
   7. проверить в trace `trigger=monitor`, отсутствие `monitor_incomplete`,
      `send_notification.structured.status=="sent"` и payload fake Telegram,
      содержащий URL из `RESULT_URLS`.

  **Порядок обязателен.** Если создать watch раньше, чем записан прогон, первый
  чек может получить `pending`: baseline **не** поглотится (§7), расписание уедет
  на `+interval_seconds`, и следующий достоверный прогон поглотит baseline **без
  отправки** — сценарий станет ложным. Ожидание `status="empty"` до создания watch
  устраняет эту гонку и делает baseline детерминированным.

  PASS возможен только при совпадении всех шагов; иначе `FAIL`/`BLOCKED` (нет
  модели). Реальный Telegram не вызывается.
* **UI (D20-16, D20-22)**: Playwright + системный браузер; оба сервера и строки
  вызовов A/B.

### Подтверждается на fake-данных

* Telegram во всех автоматических проверках — loopback-фейк. Реальный контракт
  Telegram API **не** подтверждается автоматикой (D20-23).
* Tavily в INT/LIVE — loopback-фейк (`tests/support/fake_search.py`). Реальный
  Tavily не вызывается (остаётся `BLOCKED`).
* **Накопленные за простой прогоны (D20-28)** проверяются на **fake-данных**:
  несколько `run`-payload через `evaluate_run` (UNIT) и несколько прогонов A на
  loopback-фейковом поиске (INT/RESTART); промежуточные items не отдаются, реальный
  Tavily не вызывается.
* **Разные новые материалы в одном периоде (D20-29)** проверяются на fake/fake
  Telegram: две доставки `sent` с разными контентными ключами, реальный Telegram
  не вызывается.
* Логика выбора инструментов моделью воспроизводима только на фиксированных
  промптах; UNIT-тесты используют `ScriptedProvider`, что подтверждает логику
  оркестратора, но не свободу выбора реальной модели.

### Остаётся BLOCKED

* **D20-23 REAL Telegram**: `BLOCKED` без `NOTIFIER_REAL_ALLOW=1` и явного
  разрешения пользователя. `harness/acceptance.py` вызывает
  `harness/notifier_real_live.py`; отсутствие opt-in даёт
  `NOTIFIER_REAL_STATUS: BLOCKED` и **не** переводит шаг в `FAIL`.
* **Реальный Tavily**: `BLOCKED` (вне дня 20; автоматика его не вызывает).
* **VPS-развёртывание**: `BLOCKED` (ручной чек-лист, SPEC §15; вне границ
  задачи).
* **D20-26 ж/д билеты**: реализации нет; требуется отдельный источник.

## 3. Регрессии дней 16–19

| Поведение | Проверка |
| --- | --- |
| Инструменты A: 9 контрактов, structured | `tests/test_tools.py`, `tests/test_mcp_inprocess.py` |
| SSE-контракт `status/delta/tool_call/tool_result/done/error`, keep-alive | `tests/test_api.py`, LIVE E2E |
| `Technical details` только внутри свёрнутого блока | `MCP_UNAVAILABLE_UI_STATUS`, UI E2E |
| Задания/планировщик, CAS, рестарт | `TASKS_INTEGRATION_STATUS`, `PERSISTENCE_RESTART_STATUS`, `SCHEDULER_RESTART_STATUS`, `TASKS_UI_STATUS` |
| Отчёты и панель `Saved reports` | `REPORTS_INTEGRATION_STATUS`, `REPORTS_RESTART_STATUS`, `REPORTS_UI_STATUS` |
| Композиция `search_web → digest → save_report` | `COMPOSITION_LIVE_STATUS`, `COMPOSITION_NO_SAVE_LIVE_STATUS` |
| Лимит 5 чатов, изоляция, история | `CHATS_UI_STATUS`, `tests/test_chats_api.py` |
| Discovery CLI, пагинация, exit codes | `tests/test_discovery_cli.py`, `smoke_test.bat` |
| Провайдер и адаптер MCP | `tests/test_provider.py`, `tests/test_mcp_adapter.py` |
| Probe regression: один probe = 2 × сервера | `MCP_PROBE_REGRESSION` — **4** POST при A+B (окно замера с `NOTIFIER_MONITOR_ENABLED=0`) |
| B-down ветки: чат на A, API и UI | `smoke_test.bat` + `tests/test_notifier_api.py`: `/api/mcp/servers` → `connected:false`, `/watches` → `available:false`, без 5xx |

Регрессия по числу инструментов: день 20 **не** меняет набор инструментов A
(9). Объединённый `tools_count` при двух серверах — 15 (`per_server={"A":9,"B":6}`);
тесты, проверяющие именно A, остаются на 9, объединённый список проверяется
отдельно (D20-02, D20-03, D20-25). `harness/live_mcp.py` считает
`EXPECTED_MCP_POSTS_PER_PROBE = 2 × число сконфигурированных серверов` и включает
порт B в `require_free_ports`; `MCP_PROBE_REGRESSION` ожидает 4.

## 4. Формат фрагмента фактической trace межсерверной цепочки

Формат записи — как у существующего trace (`agent/trace.py`: одна JSONL-строка
на событие, `request_id` во всех записях). Новые поля — `server`, `trigger`,
`watch_id`, `monitor_incomplete` (см. SPEC §10). Пример ожидаемого фрагмента
(схематично, фактические значения времени и id различаются):

```
request_start(trigger=chat, chat_id=<id>)
mcp_connect(server=A, ok=True, duration_ms=...)
mcp_connect(server=B, ok=True, duration_ms=...)
mcp_list_tools(tools_count=15, per_server={"A":9,"B":6}, tool_names=[...])
model_request(phase=tool_selection, tools_offered=15)
tool_selected(server=A, tool=get_latest_search_run)
tool_completed(server=A, tool=get_latest_search_run, ok=True, result={...})
tool_selected(server=B, tool=evaluate_run)
tool_completed(server=B, tool=evaluate_run, ok=True,
               result={status:"ok", new_items:[...], is_baseline:false, should_notify:true})
tool_selected(server=B, tool=send_notification)
tool_completed(server=B, tool=send_notification, ok=True,
               result={delivery_id:"...", status:"sent"})
model_request(phase=final_answer)
request_done(ok=True)
```

Monitor-ход (неполный):

```
request_start(trigger=monitor, watch_id=<id>, chat_id=<id>)
mcp_connect(server=A, ok=True, duration_ms=...)
mcp_connect(server=B, ok=True, duration_ms=...)
model_request(phase=tool_selection)
tool_selected(server=A, tool=get_latest_search_run)
tool_completed(server=A, tool=get_latest_search_run, ok=True, result={...})
tool_selected(server=B, tool=evaluate_run)
tool_completed(server=B, tool=evaluate_run, ok=True,
               result={status:"ok", new_items:[...], should_notify:true})
monitor_incomplete(watch_id=<id>, reason="send_notification_missing")
```

Полный monitor-ход вместо `monitor_incomplete` завершается `request_done(ok=True)`
после `send_notification` со статусом `sent`/`duplicate`.

Верификатор цепочки (по образцу `harness/live_e2e.py::verify_trace`) проверяет
упорядоченную подпоследовательность и непустые обязательные поля; для дня 20
добавляется проверка поля `server` у `tool_selected`/`tool_completed`.

## 5. Требования к приёмке

* Уровни не заменяются более слабыми: INT не считается за RESTART, LIVE не
  подменяется UNIT-тестом с фейком, REAL не заявляется `PASS` без opt-in и
  разрешения.
* Автотесты не тратят платные вызовы и не обращаются к реальному Telegram или
  Tavily.
* БД тестов — только в `.runs`/temp; реальные `data/day18.sqlite3` и
  `data/day20-notifier.sqlite3` тестами не трогаются.
* Тестируемая модель приложения — `AGENT_MODEL_NAME`; это **не** модель ролей
  Control Center. В отчёте live-прогона называется фактическая модель.
* Итоговый `TEST_STATUS` и независимую проверку выставляет Tester; Developer и
  Coordinator не подтверждают собственную реализацию.

## 6. Итог приёмки

Заполняется после реализации (Developer, затем Tester). До реализации —
`TEST_STATUS: BLOCKED` по критериям D20-20…D20-23 (нужны реальные процессы,
локальная модель и opt-in), `D20-12` — после `smoke_test.bat`, остальные — по
факту прогонов.
