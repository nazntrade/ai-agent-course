# SPEC — Day 20: межсерверная оркестрация MCP (мониторинг и уведомления)

Идентификатор задачи: `day-20-multi-mcp-orchestration`.
Статус: черновик архитектурного решения (Spec-Driven Development). Реализация не
начата; начинается только после `SPEC_GATE_STATUS: PASS` и явной команды
пользователя.
Источник задания: Task Contract пользователя (цель дня 20) и архитектурные
решения, зафиксированные ниже.

Соглашение о терминах документа: имя, помеченное **предлагаемое**, в текущем
коде отсутствует. Существующие имена (`mcp_server`, `agent.*`, `storage.*`,
`normalize_url`, `get_latest_search_run`) взяты из репозитория как есть.

## 1. Назначение

Пользователь ставит в чате мониторинг игровых новостей Xbox: «следи за новостями
Xbox и присылай мне в Telegram только то, что подходит под мои условия».
Требуется наблюдаемая оркестрация **двух независимых MCP-серверов** одной моделью:

```
пользователь → агент (LLM) → хост (backend) → McpHub
                                                 ├─ Server A (mcp_server)      : поиск/прогоны
                                                 └─ Server B (notifier_server) : подписки/уведомления
```

* **Server A** — существующий `python -m mcp_server` (порт 8765). По расписанию
  ищет и хранит прогоны, как в дне 18. Инструменты и контракты дней 16–19 не
  меняются.
* **Server B** — **предлагаемый** новый отдельный MCP-сервер
  `python -m notifier_server` (порт 8766). Проверяет, появился ли в результате
  сервера A пункт, отвечающий условиям пользователя, и отправляет уведомление в
  его Telegram.
* Агент через хост подключён к **обоим** серверам, **сам выбирает** их
  инструменты, проводит структурированные данные из A в B и инициирует отправку.
* В UI и в trace видно, какой инструмент **какого сервера** и в каком порядке
  вызван.

**Запрет внутреннего прямого вызова A→B.** Никакой инструмент сервера A не
вызывает инструменты сервера B (и наоборот) внутри себя. Передача данных идёт
только через модель в цикле `Orchestrator`, а наблюдаемая цепочка фиксируется в
trace и SSE.

## 2. Границы

### Входит

* Два процесса: существующий A (без изменения инструментов) и **предлагаемый** B
  (`notifier_server`, отдельная БД, свои инструменты, свой `tools/list`).
* **Предлагаемый** хост-модуль `agent/mcp_hub.py` и реализация протокола
  `agent.mcp_adapter.McpClient` поверх двух серверов.
* Необязательное поле `server` у `McpTool` (default `None`) и поля `server` в
  trace-событиях и SSE-событиях `tool_call`/`tool_result`.
* 6 новых инструментов сервера B (плюс обвязка схемы `chat_id` как в дне 18).
* **Предлагаемый** host-side монитор `agent/monitor.py` (фоновая работа без
  браузера).
* **Предлагаемая** отдельная БД B (`NOTIFIER_DB_PATH`, таблицы `watches`,
  `seen_items`, `deliveries`) со своим версионированием.
* Единственная сетевая граница B — **предлагаемый** `notifier_server/telegram.py`.
* HTTP API `/api/mcp/servers`, backend-endpoint подписок/доставок, UI-панель
  `Notification watches` и показ обоих серверов в `MCP status`.
* Новые переменные окружения в `.env.example`.
* Тесты UNIT/INT/RESTART/LIVE/UI и opt-in REAL Telegram.
* Документация дня 20.

### Не входит

* Изменение инструментов и контрактов сервера A (дни 16–19), включая
  `calculate`, `get_server_info`, `search_web`, `schedule_search_task`,
  `list_search_tasks`, `get_latest_search_run`, `stop_search_task`,
  `digest_search_results`, `save_report`.
* Изменение `.bat`-файлов, `deploy_vps.*`, governance-файлов.
* Изменение AI-SERVER-Control-Center, AI-Project-Templates.
* Развёртывание на VPS, `git commit`/`git push`/деплой.
* Вызов реального Tavily и реального Telegram в автоматических проверках.
* Многопользовательская изоляция, роли, квоты; несколько получателей Telegram.
* Переписывание действующих функций A; добавление новых инструментов в A.

## 3. Зафиксированные архитектурные решения

Ниже — 15 решений, обязательных к исполнению; обоснование приведено, но
противоречие им запрещено. Если реализация потребует отступления, Developer
возвращает `ARCHITECTURE_STATUS: PLAN_DEVIATION_REQUIRED`.

1. **Два процесса.** A = существующий `python -m mcp_server` (порт 8765,
   инструменты не менять). B = **предлагаемый** пакет `notifier_server`
   (`python -m notifier_server`), отдельный порт по умолчанию 8766, свой
   Streamable HTTP endpoint `/mcp`, свой `tools/list`. B — отдельный процесс со
   своими зависимостями и **не импортирует** `agent.*` (как `mcp_server`
   не импортирует `agent.*`).
2. **Хост-граница (`McpHub`).** Сейчас `agent/server.py::build_mcp_client`
   создаёт один `SdkMcpClient`, а `Orchestrator.__init__` принимает один
   `mcp_client`. **Предлагаемый** `agent/mcp_hub.py::McpHub` реализует протокол
   `McpClient`: держит несколько пронумерованных/помеченных серверов (A и B).
   Протокольные `probe()`/`status()`/`list_tools()` — **A-first**; объединённый
   список A+B отдаёт **только** `probe_servers()` (§4.3). `call_tool(name)`
   маршрутизирует по имени. Имена инструментов A и B не пересекаются. `McpTool`
   расширяется необязательным полем `server` (default `None`, чтобы существующие
   `FakeMcpClient` и тесты продолжали работать). `Orchestrator` пишет `server` в
   `tool_selected`/`tool_completed`, а SSE `tool_call`/`tool_result` несут поле
   `server`. Если B не сконфигурирован/недоступен, поведение дней 16–19
   сохраняется (работает только A).
3. **Шесть инструментов B** — структурированные, с model-facing docstrings,
   `chat_id` инжектится хостом и скрыт от модели (как в дне 18). Состав в §5.
4. **Первый прогон = точка отсчёта (baseline).** Первый `evaluate_run` для
   подписки помечает все подходящие items как seen и возвращает
   `is_baseline=true, should_notify=false`; старая выдача не рассылается.
5. **Дедупликация в БД B.** Отпечатки seen-items и таблица `deliveries` с
   уникальным ключом `(watch_id, kind, period_key)`. Повтор результата, повторная
   обработка прогона и перезапуск сервисов не создают дубль. Правило
   безопасности не зависит от модели или текста её ответа.
6. **Фоновая работа без браузера.** Host-side монитор (**предлагаемый**
   `agent/monitor.py`): по расписанию (`NOTIFIER_MONITOR_TICK_SECONDS`) для
   каждой активной подписки, у которой наступил `check_interval_seconds`, хост
   запускает **тот же** pipeline `Orchestrator` (тот же провайдер модели) с
   синтетическим monitor-промптом и ограниченным подмножеством инструментов,
   чтобы **модель** выбрала цепочку A→B. Один monitor-ход = одна трасса с
   `trigger=monitor` (и `watch_id`/`chat_id`), где видно
   `mcp_connect`/`tool_selected(server=A|B)`/`tool_completed` в правильном
   порядке. **Контракт monitor-хода в `Orchestrator.run`:**

   ```python
   async def run(self, request_id, session, user_message, *,
                 trigger="chat", watch_id=None, system_prompt=None,
                 allowed_tools=None, on_tool_result=None,
                 require_result=None) -> AsyncIterator[ChatEvent]
   ```

   При заданном `require_result` неполный monitor-ход пишет **только**
   `monitor_incomplete{watch_id, reason}` **вместо** `request_done`. Полнота =
   `evaluate_run` вызван и (`should_notify=false` **или** `send_notification` со
   статусом `sent`/`duplicate`). Причины: `evaluate_run_absent`,
   `send_notification_missing`, `send_notification_failed`. Успех не
   объявляется, seen-set не меняется. **Retry-семантика:** если
   `send_notification` **не вызывался вообще**, расписание не двинуто —
   следующий тик повторяет тот же чек; если send вызван со статусом
   `failed`/`not_configured`, расписание двинуто, seen-set не записан, items
   остаются новыми, и повтор происходит по обычному расписанию
   (`interval_seconds`), а не каждым тиком. `request_start` получает поле
   `trigger` (`chat`/`monitor`). Включение: `NOTIFIER_MONITOR_ENABLED`
   (default on).
7. **Telegram — безопасность.** Токен `TELEGRAM_BOT_TOKEN` и получатель
   `TELEGRAM_CHAT_ID` читает **только** процесс B; получатель привязан к
   настроенному пользователю и **не является** аргументом инструмента (модель не
   может задать произвольный `chat_id`). Единственная сетевая граница B —
   `notifier_server/telegram.py` (base URL `NOTIFIER_TELEGRAM_API_BASE_URL`,
   default `https://api.telegram.org`, в тестах подменяется loopback-фейком).
   Токен исключён из `repr()`, не попадает в чат, trace, логи, репозиторий и
   видео; ошибки санитизированы. Пустой токен → честный `not_configured`, без
   сети и без выдуманного успеха. До живой отправки пользователь должен сам
   начать диалог с ботом.
8. **Хранение B — отдельная БД.** Default `data/day20-notifier.sqlite3`, env
   `NOTIFIER_DB_PATH`; свои таблицы `watches`, `seen_items`, `deliveries`.
   Не дублирует `runs`/`tasks`/`chats` сервера A и не пишет в общий файл A.
   Схема B со своим версионированием; данные переживают перезапуск. На VPS путь
   вне деплой-каталога.
9. **Ограничение дня 18 («доступен только последний прогон») закрывается явно.**
   Дополнительных инструментов в A не добавляем; функцию сравнения решает
   персистентное seen-множество B. Осознанное ограничение: items, появившиеся и
   исчезнувшие из последнего прогона между проверками, могут быть пропущены
   (§14).
10. **HTTP API/UI.** Сохранить существующие `/api/mcp/status` и
    `/api/mcp/tools` (семантика сервера A — чтобы не сломать тесты/OpenAPI дней
    16–19). Добавить `GET /api/mcp/servers` — список обоих серверов (`label`,
    url/loopback host:port, `connected`, `protocol_version`, server
    name/version, `tools_count`, `error`). UI-блок `MCP status` показывает оба
    сервера; строки `Technical details` показывают сервер (`[A]`/`[B]`). Панель
    подписок/доставок (`Notification watches`, English) для выбранного чата
    читает состояние через B (host→B) и корректно обрабатывает недоступность B.
    Доставка не выдаётся за успешную по тексту модели.
11. **Нормализация URL для отпечатков** переиспользует существующую логику
    `mcp_server/reports.py::normalize_url`, не дублируется.
12. **Конфигурация** (добавить в `.env.example` без настоящих значений):
    `MCP_NOTIFIER_URL` (default `http://127.0.0.1:8766/mcp`),
    `MCP_NOTIFIER_HOST`/`MCP_NOTIFIER_PORT`, `NOTIFIER_DB_PATH`,
    `TELEGRAM_BOT_TOKEN` (пусто), `TELEGRAM_CHAT_ID` (пусто),
    `NOTIFIER_TELEGRAM_API_BASE_URL`, `NOTIFIER_MONITOR_ENABLED`,
    `NOTIFIER_MONITOR_TICK_SECONDS` (default 30 c, clamp 1..3600),
    `NOTIFIER_LOAD_DOTENV` (default 1). **`NOTIFIER_LOAD_DOTENV=0`** запрещает
    процессу B читать локальный `.env` (как `MCP_LOAD_DOTENV` у A): harness и
    тесты **обязаны** передавать `NOTIFIER_LOAD_DOTENV=0`, фиктивный токен и
    loopback base URL, чтобы не задеть реальный `.env` и сеть.
    `agent/settings.py` **не** читает `TELEGRAM_*` (токен доступен только B).
13. **Уровни проверок** — UNIT, INT (`smoke_test.bat`), RESTART, LIVE (реальная
    локальная Qwen), UI (Playwright), opt-in REAL Telegram. Тестируемая модель
    приложения (`AGENT_MODEL_NAME`) — не то же самое, что модель ролей в Control
    Center; в отчёте называется фактическая модель live-прогона. Обязательные
    проверки запускаются только через доверенные `.bat` (`test.bat`,
    `smoke_test.bat`, `test.bat acceptance`); `.bat` не меняются, проверки
    встраиваются в существующие режимы через `harness/`.
14. **Вне границ:** изменение `.bat`, `deploy_vps.*`, governance-файлов,
    AI-SERVER-Control-Center, AI-Project-Templates; VPS-развёртывание;
    commit/push/деплой; переписывание действующих функций A; вызов реального
    Tavily/Telegram в автоматике.
15. **Ж/д билеты** — только как развитие в документации (§17).

## 4. Архитектура

### 4.1 Server A (без изменений)

`mcp_server/server.py::MCPServer` регистрирует 9 инструментов, `run()` поднимает
Streamable HTTP на 8765 и планировщик `mcp_server/scheduler.py`. Прогоны
хранятся в общем файле A (`storage/db.py`, schema v3) и читаются через
`get_latest_search_run` (`mcp_server/tasks.py`). День 20 **не добавляет** в A
инструментов и не меняет его контракты.

### 4.2 Server B (предлагаемый пакет `notifier_server`)

Отдельный процесс, Streamable HTTP на 8766, endpoint `/mcp`, свой `tools/list`.
Собственные зависимости и собственная БД. Не импортирует `agent.*`. Внутри —
сервисный слой (`watches`/`evaluate`/`deliver`), репозиторий БД, конфигурация и
единственная сетевая граница `telegram.py`.

### 4.3 McpHub (предлагаемый `agent/mcp_hub.py`)

**Hub-API (явный):**

```
ServerProbe (новый wrapper, предлагаемый)
  label: str            # "A" | "B"
  status: McpStatus     # существующий тип, НЕ расширяется
  tools: list[McpTool]  # у каждого McpTool поле server = label

McpClient (Protocol, agent/mcp_adapter.py) — A-first семантика
  probe()  -> (McpStatus, list[McpTool])
  status() -> McpStatus
  list_tools() -> list[McpTool]
  call_tool(name, arguments) -> McpCallResult

McpHub
  probe_servers() -> list[ServerProbe]   # ПАРАЛЛЕЛЬНО через asyncio.gather
  probe_server(label) -> ServerProbe     # один сервер
  # протокольные методы сохраняют A-first семантику (см. ниже)
  probe()/status()/list_tools()/call_tool()
```

* **`probe_servers()`** опрашивает все сконфигурированные серверы параллельно
  (`asyncio.gather`) и возвращает по `ServerProbe` на каждый. `probe_server(label)`
  опрашивает один сервер. `McpStatus` **не расширяется**; агрегация живёт во
  wrapper'е `ServerProbe`, а не в общем типе.
* **Протокольные методы A-first.** `probe()`, `status()`, `list_tools()`
  возвращают результат сервера A (при наличии A); `call_tool(name)`
  маршрутизируется по имени, а агрегированный `tools/list` (A + B с полем
  `server`) используется оркестратором через `probe_servers()`.
  `/api/mcp/status` и `/api/mcp/tools` благодаря этому сохраняют семантику A
  (решение 10, §12).
* **Коллизия имён инструментов** — контролируемая ошибка конфигурации
  (`connected=false`, category `protocol`), **не** «тихий» выбор победителя.
* **Пустой `MCP_NOTIFIER_URL`** → слот B `connected:false`,
  `error.category="not_configured"`; B при этом **не опрашивается** (нет
  попытки соединения). Инструменты A остаются доступны.
* `McpTool.server` — необязательное поле, default `None`; существующие
  `FakeMcpClient` (`tests/support/fakes.py`) и `McpStatus` продолжают
  конструироваться/читаться без изменений. `FakeMcpClient` **не меняется**.
* Если B недоступен, инструменты A остаются доступны, а ошибка B попадает в
  `/api/mcp/servers` как `connected:false` (решение 2, §12).

### 4.4 Потоки: чат и монитор

```
Канал чата:
  POST /api/chat/stream
    → Orchestrator.run(trigger="chat")
      → Hub.probe_servers() → tools A + B (с полем server)
      → модель выбирает: schedule_search_task(A) … get_latest_search_run(A)
                          → evaluate_run(B) → send_notification(B)
      → SSE tool_call/tool_result(server) → delta → done
      → trace: request_start{trigger:"chat"}, mcp_connect, tool_selected(server),
               tool_completed(server), request_done

Канал монитора (без браузера):
  Monitor tick (NOTIFIER_MONITOR_TICK_SECONDS)
    → для активной подписки с наступившим check_interval_seconds (§11):
      Orchestrator.run(request_id, session, user_message,
                       trigger="monitor", watch_id=<id>,
                       system_prompt=<monitor>, allowed_tools=<A+B subset>,
                       require_result=<completeness check>)
      → модель выбирает A:get_latest_search_run → B:evaluate_run → B:send_notification
      → trace того же формата + trigger="monitor" + watch_id
      → неполный ход → monitor_incomplete{watch_id, reason} ВМЕСТО request_done
```

Оба канала используют **один и тот же** `Orchestrator` и **один и тот же**
провайдер модели. Различается только `trigger` и подмножество инструментов.

## 5. Новые MCP-инструменты сервера B

Общие правила: конкретные возвращаемые типы `dict[str, Any]` (иначе SDK отдаёт
текст), ошибки — `ToolError` с санитизированным текстом, `chat_id` инжектится
хостом (`agent/orchestrator.py::_run_tools` перезаписывает значение модели) и
скрыт из модели (`agent/tool_schema.py::HIDDEN_INJECTED_ARGUMENTS`). Итого в B
**6 инструментов**.

### 5.1 `create_notification_watch(query, keywords, exclude=[], interval_seconds=0, summary_interval_seconds=0, source_task_id="", chat_id="")`

Сохраняет подписку: что отслеживать, **явный** критерий «подходящий», частоту
проверки и частоту регулярной сводки. Параметр `exclude` — явный, со значением по
умолчанию `[]` (см. §6).

Валидация по порядку:

1. пустой/неизвестный `chat_id` → `ToolError("This tool needs an active chat context")`;
2. `query` — непустая строка после `strip`;
3. `keywords` — непустой список непустых строк (нормализуются: `strip`, регистр
   сохраняется, сравнение — регистронезависимое); `exclude` — список непустых
   строк (может быть пустым, default `[]`);
4. `interval_seconds` и `summary_interval_seconds` — integer (не bool), не ниже
   минимума (согласуется с днём 18: `TASK_MIN_INTERVAL_SECONDS = 60`); иначе
   `ToolError` с понятным текстом;
5. `source_task_id` — опциональная ссылка на задание A (может быть пустой).

Успех: `{watch_id, status:"active", query, keywords, exclude, interval_seconds,
summary_interval_seconds, source_task_id, created_at (ISO-8601 UTC),
next_check_at, note}`. При создании **`next_check_at = now`** (немедленный первый
baseline-чек); **критерий и расписание сохраняются и возвращаются** явно, а не
выводятся моделью.

### 5.2 `list_notification_watches(chat_id="")`

Список подписок с состоянием: `{count, watches:[{watch_id, query, keywords,
exclude, interval_seconds, summary_interval_seconds, status, created_at,
next_check_at, last_check_at, seen_count, last_delivery}]}`. Пусто →
`{count:0, watches:[], note:"No notification watches in this chat."}`.

### 5.3 `evaluate_run(watch_id, run, chat_id="")`

Принимает **весь** структурированный результат `get_latest_search_run` сервера A.
Детерминированно применяет критерий, сравнивает с сохранёнными отпечатками
(fingerprint по нормализованному URL, иначе по title), возвращает:

```json
{
  "status": "ok|empty|error|pending|unknown_watch",
  "new_items": [{"title": "...", "url": "..."}],
  "matched_count": 0,
  "known_count": 0,
  "is_baseline": false,
  "should_notify": false,
  "note": "..."
}
```

Правила:

* входные `run.status = error|empty|pending` обрабатываются честно (нет
  выдуманных новостей): `new_items: []`, `should_notify: false`;
* `matched_count` — сколько items подходят под критерий; `known_count` — сколько
  из них уже в seen-set; `new_items` — подходящие и ещё не seen;
* **evaluate не меняет seen-set в обычном режиме**; исключение — baseline
  (§7); baseline поглощается **только** первым прогоном со
  `status in {"ok","empty"}`; `pending`/`error` baseline **не** поглощают (чек
  повторится, когда появится реальная выдача);
* **`evaluate_run` не бросает `ToolError` на пользовательские данные.**
  Malformed `run` (не-объект, без ожидаемых полей, неизвестный `status`) →
  structured `status:"error"` (не исключение); чужой/неизвестный `watch_id` →
  structured `status:"unknown_watch"`;
* **расписание и baseline двигаются согласованно:**
  * `pending`/`error` двигают **только** `next_check_at`
    (`= now + interval_seconds`); `last_check_at` остаётся `NULL`, поэтому
    baseline **не** поглощён и следующий достоверный прогон снова считается
    точкой отсчёта;
  * первый прогон со `status in {"ok","empty"}` (baseline) и обычная проверка
    без новых items выставляют `last_check_at=now` и
    `next_check_at=now+interval_seconds`;
  * при `should_notify=true` расписание **не** двигается (`last_check_at` и
    `next_check_at` не меняются) — ход «висит» до `send_notification`, иначе
    повторный чек мог бы «потерять» уведомление;
* evaluate не отправляет уведомление и не является точкой записи доставки.

### 5.4 `send_notification(watch_id, chat_id="", kind="new_items", items=[])`

**Единственное** место отправки в Telegram и **единственное** место записи
seen-set/доставки. Идемпотентно:

* ключ `(watch_id, kind, period_key)`;
* для `kind="new_items"` `period_key` — **контентный**: хэш множества отпечатков
  `items` (порядок не влияет), а **не** временной период. Поэтому два **разных**
  новых материала (разные множества отпечатков) в одном временном периоде дают
  **разные** `period_key` → две отдельные доставки `sent`, ни одна не подавлена;
* временной период применяется **только** к `kind="summary"`:
  `period_key = floor(now / summary_interval_seconds)` — по смыслу допускается
  одна сводка за период;
* повтор **того же** множества отпечатков → `duplicate` (без сети);
* при **частичном** пересечении ранее доставленный item уже в seen-set, поэтому
  `evaluate_run` вернёт только новые items: новый материал доставляется отдельным
  (контентным) ключом, старый не дублируется;
* если модель передала **оба** новых материала в **одном** вызове
  `send_notification`, это **одна** доставка (одно сообщение с двумя ссылками), а
  не подавление; ключ всё равно контентный. Если модель вызвала
  `send_notification` **дважды** с разными множествами — это **два** сообщения;
* статусы: `sent` / `not_required` / `duplicate` / `failed` / `not_configured`;
* возвращает `delivery_id`;
* **по stopped/чужому `watch_id` → `ToolError`** (контролируемая ошибка, не
  выдуманная доставка);
* **`send_notification` двигает расписание (`last_check_at`/`next_check_at`) во
  всех терминальных статусах**, включая `failed`/`not_configured` — чтобы
  упавшая доставка не «залипала» на каждом тике, а повторялась по расписанию
  (retry одной строки — §8).

Записывает seen-отпечатки **только при фактической обработке**. Поэтому неполный
ход агента (evaluate сказал `should_notify=true`, но send не вызван) не «съедает»
уведомление: `evaluate_run` не двинул расписание (§5.3), поэтому следующий тик
повторит тот же чек — это и есть согласованность со «следующий тик повторяет»
(решение 6). После того как ход завершён вызовом `send_notification`,
расписание двигается, и следующий чек наступает через `interval_seconds`.

### 5.5 `get_delivery_status(watch_id="", chat_id="")`

Последние доставки со статусами: `{count, deliveries:[{delivery_id, watch_id,
kind, status, period_key, items_count, error|null, created_at}]}`.

### 5.6 `stop_notification_watch(watch_id, chat_id="")`

Идемпотентная остановка: активная → `stopped`; уже остановленная → успех с
пояснением. Ошибка отправки в Telegram, пустой токен (`not_configured`) и статус
`duplicate` **не** считаются успешной доставкой и отражаются как есть.

## 6. Критерий «подходящий» и расписания

**Критерий «подходящий» (explicit, сохраняется и возвращается):**

* `keywords` — непустой список; item подходит, если **любой** keyword входит
  подстрокой в `title` **или** `description` без учёта регистра;
* `exclude` (опционально) — список; item **не подходит**, если любой exclude
  входит по тому же правилу;
* правило детерминированное и не зависит от модели, текста её ответа и от
  формулировок в сниппетах;
* заголовки/описания — недоверенные данные: критерий их только *сравнивает*,
  никогда не исполняет.

**Расписания (явные, сохраняемые):**

* `interval_seconds` — как часто подписка проверяется монитором;
* `summary_interval_seconds` — как часто допускается регулярная сводка
  (`kind="summary"`); период дедупликации `floor(now / summary_interval_seconds)`.

## 7. Первый прогон (baseline)

Первый `evaluate_run` для подписки:

* помечает все подходящие items как seen;
* возвращает `is_baseline=true, should_notify=false`;
* **вся старая выдача не рассылается.**

Baseline поглощается **только** первым прогоном со `status in {"ok","empty"}`:
`empty` — валидная точка отсчёта (подходящих items нет, но прогон состоялся).
`pending`/`error` baseline **не** поглощают: следующий достоверный прогон снова
считается baseline. Это осознанный контракт, а не побочный эффект: пользователь
подписывается на **будущие** новости и не получает лавину исторических ссылок.
`send_notification` для baseline не вызывается; baseline не создаёт доставку
`sent`.

## 8. Дедупликация и её ключи

* Хранится в БД B: отпечатки seen-items (`seen_items`) и таблица `deliveries` с
  уникальным `(watch_id, kind, period_key)`.
* **`duplicate` возвращается только если существующая строка имеет
  `status='sent'`.** Это закрывает дыру: упавшая/неотправленная попытка не должна
  навсегда блокировать повтор.
* При `failed`/`not_configured` — **повторная попытка через `UPDATE` той же
  строки** (`attempts+1`, `updated_at`), сетевой вызов повторяется; строка не
  дублируется (UNIQUE сохраняется).
* `not_required` строку **не создаёт** (уведомление не требовалось — нечего
  дедуплицировать).
* Повтор результата, повторная обработка прогона и перезапуск сервисов не
  создают дубль: терминально успешный второй `send_notification` с тем же ключом
  возвращает `duplicate` и не выполняет сетевой вызов.
* `new_items` — `period_key` **контентный** = хэш множества отпечатков `items`
  (порядок не влияет). Два **разных** множества в одном временном периоде →
  разные `period_key` → **две** доставки `sent`; повтор того же множества →
  `duplicate` (без сети). Контентный ключ не даёт одному новому материалу
  подавить другой.
* `summary` — единственный вид, где `period_key` временной:
  `floor(now / summary_interval_seconds)` (один период = одна доставка).
* Одна доставка может содержать несколько items (несколько ссылок в одном
  сообщении) — это по-прежнему одна строка `deliveries` с одним контентным
  ключом; разные вызовы с разными множествами создают разные строки.
* **Правило безопасности не зависит от модели/текста ответа**: дедупликация
  проверяется до сети и до записи seen-set; ложное «отправлено» из текста модели
  не создаёт доставку.
* Отпечаток item: `normalize_url(url)` из `mcp_server/reports.py::normalize_url`;
  если URL непригоден — отпечаток по нормализованному title (§решение 11).

## 9. Хранение B

Отдельная БД: default `data/day20-notifier.sqlite3`, env `NOTIFIER_DB_PATH`. Своё
версионирование (по образцу `storage/db.py`: `SCHEMA_VERSION`, forward-only
`MIGRATIONS`, `meta.schema_version`, `SchemaVersionError` для более новой БД).
Владелец — только процесс B.

```sql
CREATE TABLE IF NOT EXISTS watches (
  id                      TEXT PRIMARY KEY,
  chat_id                 TEXT NOT NULL,
  query                   TEXT NOT NULL,
  keywords_json           TEXT NOT NULL,
  exclude_json            TEXT NOT NULL DEFAULT '[]',
  interval_seconds        INTEGER NOT NULL,
  summary_interval_seconds INTEGER NOT NULL,
  source_task_id          TEXT NOT NULL DEFAULT '',
  status                  TEXT NOT NULL CHECK (status IN ('active','stopped')),
  created_at              REAL NOT NULL,
  updated_at              REAL NOT NULL,
  next_check_at           REAL NOT NULL,
  last_check_at           REAL,
  last_delivery_status    TEXT
);

CREATE TABLE IF NOT EXISTS seen_items (
  watch_id   TEXT NOT NULL,
  fingerprint TEXT NOT NULL,
  title      TEXT NOT NULL,
  url        TEXT NOT NULL DEFAULT '',
  seen_at    REAL NOT NULL,
  PRIMARY KEY (watch_id, fingerprint)
);

CREATE TABLE IF NOT EXISTS deliveries (
  id          TEXT PRIMARY KEY,
  watch_id    TEXT NOT NULL,
  chat_id     TEXT NOT NULL,
  kind        TEXT NOT NULL CHECK (kind IN ('new_items','summary')),
  period_key  TEXT NOT NULL,
  status      TEXT NOT NULL CHECK (
                status IN ('sent','not_required','duplicate','failed','not_configured')),
  items_json  TEXT NOT NULL DEFAULT '[]',
  error       TEXT,
  attempts    INTEGER NOT NULL DEFAULT 1,
  created_at  REAL NOT NULL,
  updated_at  REAL NOT NULL,
  UNIQUE (watch_id, kind, period_key)
);
```

Колонки `attempts` и `updated_at` обслуживают retry по S6: `UPDATE` той же строки
при `failed`/`not_configured` увеличивает `attempts` и обновляет `updated_at`;
`duplicate` возможен только для строки со `status='sent'`.

Имена таблиц/полей — **предлагаемые**; точные типы фиксирует PLAN. Владение: БД B
не пересекается с БД A; `watches`/`seen_items`/`deliveries` не дублируют
`runs`/`tasks`/`chats`. Реальные временные метки — epoch (REAL), в ответах
инструментов — ISO-8601 UTC.

## 10. Trace-поля и порядок межсерверной цепочки

Новые/изменённые trace-поля (**предлагаемые**):

| Событие | Форма | Поля |
| --- | --- | --- |
| `request_start` | как раньше | `trigger` (`chat`/`monitor`), плюс `watch_id` для monitor |
| `mcp_connect` | **ОДНА запись на сервер** | `server` (`A`/`B`), `ok`, identity (`protocol_version`/`server_name`), `duration_ms` |
| `mcp_list_tools` | **ОДНА агрегированная запись** | объединённый `tool_names`, объединённый `tools_count` (15), новое `per_server: {"A":9,"B":6}` |
| `tool_selected` | как раньше + `server` | `server` добавляется только когда сервер известен |
| `tool_completed` | как раньше + `server` | `server` добавляется только когда сервер известен |
| `monitor_incomplete` | новое | `watch_id`, `reason` |

SSE-события `tool_call`/`tool_result` получают поле `server` **только когда
сервер известен**. Для legacy-клиента без hub (один сервер, `McpTool.server is
None`) поведение **байт-совместимо с днями 16–19**: поле `server` не появляется.

**Совместимость с `harness/live_e2e.py::verify_trace`.** Верификатор проверяет
упорядоченную подпоследовательность и пропускает несовпавшие записи. Две записи
`mcp_connect` (A и B) совместимы: цепочка требует `mcp_connect{ok:True}` и найдёт
её; неуспешная запись B (`ok:False`) просто пропускается, последующие шаги
матчатся дальше. Агрегированный `mcp_list_tools` сохраняет обязательные поля
(`event`, `tool_names`), поэтому `verify_tools_listed` продолжает работать —
объединённый `tool_names` содержит инструмент A, который он ищет.

Пример фрагмента фактической trace межсерверной цепочки (чат, схематично):

```
request_start(trigger=chat)
mcp_connect(server=A, ok=True, duration_ms=...)
mcp_connect(server=B, ok=True, duration_ms=...)
mcp_list_tools(tools_count=15, per_server={"A":9,"B":6}, tool_names=[...])
model_request(phase=tool_selection)
tool_selected(server=A, tool=get_latest_search_run) → tool_completed(server=A, ok=True)
tool_selected(server=B, tool=evaluate_run)           → tool_completed(server=B, ok=True)
tool_selected(server=B, tool=send_notification)      → tool_completed(server=B, ok=True)
model_request(phase=final_answer) → request_done(ok=True)
```

Monitor-ход отличается `trigger=monitor` и `watch_id`; неполный ход вместо
`request_done` фиксирует `monitor_incomplete` (§11).

## 11. Фоновая работа монитора

* **Предлагаемый** `agent/monitor.py`, host-side; живёт в процессе backend.
* Тик `NOTIFIER_MONITOR_TICK_SECONDS` (default 30 c, clamp 1..3600); включение
  `NOTIFIER_MONITOR_ENABLED` (default on).
* **Перечисление подписок (S8).** Монитор берёт чаты через
  `ChatService.list_chats()` (≤5) и для каждого чата делает host→B
  `list_notification_watches(chat_id)`, затем фильтрует `active` и
  `next_check_at <= now`. На **первом же** недоступном B монитор **прерывает
  тик** (одна неудачная сессия, без 5 повторов), **не** вызывает модель и **не**
  открывает БД B напрямую.
* Для каждой подходящей подписки запускает **тот же** `Orchestrator` с тем же
  провайдером, `trigger="monitor"`, `system_prompt=<monitor>`,
  `allowed_tools` — **ограниченное** подмножество (A: `get_latest_search_run`;
  B: `evaluate_run`, `send_notification`), и `require_result` (S7), чтобы
  **модель** построила цепочку.
* Один monitor-ход = одна трасса (`trigger=monitor`, `watch_id`, `chat_id`).
* **Полнота хода** = `evaluate_run` вызван и (`should_notify=false` или
  `send_notification` со статусом `sent`/`duplicate`). Неполный ход → в трассе
  **только** `monitor_incomplete{watch_id, reason}` вместо `request_done`;
  причины: `evaluate_run_absent`, `send_notification_missing`,
  `send_notification_failed`. Успех не объявляется.
* **Retry-семантика «следующий тик повторяет»** относится **только** к случаю,
  когда `send_notification` **не вызывался вообще**: при `should_notify=true`
  `evaluate_run` не двигает расписание (§5.3), поэтому неполный ход оставляет
  `next_check_at` в прошлом — следующий тик повторит тот же чек. Если
  `send_notification` был вызван со статусом `failed`/`not_configured`, он
  двигает расписание (как любой терминальный статус, §5.4); seen-set не записан,
  items остаются новыми, и повтор происходит по обычному расписанию
  (`interval_seconds`), а не каждым тиком.
* Монитор не требует открытого браузера и не зависит от UI.
* Дублирование нагрузки монитора и ручного чата исключается идемпотентными
  ключами доставок (§8) и per-chat/per-watch сериализацией.

## 12. HTTP API и UI

Сохраняются без изменения семантики (сервер A):

| Метод и путь | Назначение |
| --- | --- |
| `GET /api/mcp/status` | статус сервера A (как дни 16–19) |
| `GET /api/mcp/tools` | `tools/list` сервера A (как дни 16–19) |

Добавляются (**предлагаемые**):

| Метод и путь | Назначение | Успех | Ошибки |
| --- | --- | --- | --- |
| `GET /api/mcp/servers` | список обоих серверов | `{servers:[ServerInfo...]}`; один вызов `probe_servers()` | 200 с `connected:false` для недоступного |
| `GET /api/chats/{chat_id}/watches` | подписки/доставки чата через B (host→B) | `{chat_id, available:true, watches, deliveries}` | неизвестный chat → **404 `chat_not_found`** (до обращения к B); B недоступен → **HTTP 200** `{available:false, error:{category}}` |

Правила:

* `/api/mcp/servers` делает **ровно один** `probe_servers()` (не по вызову на
  сервер из UI).
* `GET /api/chats/{chat_id}/watches` сначала проверяет чат; неизвестный чат
  отвечает **404 `chat_not_found`** и не обращается к B (в т.ч. когда B
  недоступен).
* Недоступность B — это **не** 5xx: HTTP 200 с `available:false` и
  санитизированным `error.category`.
* UI **без периодического опроса**: панель `Notification watches` обновляется
  при выборе чата и по кнопке `Refresh` (≤2 host→B вызовов на действие:
  подписки + доставки).

`ServerInfo = {label, url|host:port, connected, protocol_version, server:{name,
version}, tools_count, error}`. Недоступность B **не** роняет `/api/mcp/status` и
**не** выдаётся за успех: endpoint возвращает `connected:false` и санитизированную
`error.category`.

UI (English):

* блок `MCP status` показывает **оба** сервера (A и B) с их статусом и счётчиком
  инструментов;
* строки `Technical details` показывают сервер: `[A]`/`[B]`;
* панель `Notification watches` для выбранного чата: подписки, расписание,
  последняя доставка и её статус; корректный empty state и состояние
  «B недоступен»;
* **доставка не выдаётся за успешную по тексту модели**: статус берётся из
  `delivery.status` (B), а не из ответа LLM.

## 13. Безопасность Telegram

* `TELEGRAM_BOT_TOKEN` и `TELEGRAM_CHAT_ID` читает **только** процесс B.
* Получатель привязан к настроенному пользователю и **не** является аргументом
  инструмента: `chat_id` инструментов — это id чата приложения, а не Telegram
  chat id; модель не может отправить сообщение произвольному получателю.
* Единственная сетевая граница B — `notifier_server/telegram.py`; base URL
  `NOTIFIER_TELEGRAM_API_BASE_URL` (default `https://api.telegram.org`),
  в тестах подменяется loopback-фейком. Реального сетевого вызова в автоматике
  нет.
* Токен исключён из `repr()` (frozen dataclass, `field(repr=False)` как в
  `agent/settings.py` и `mcp_server/config.py`), не попадает в чат, trace, логи,
  HTTP-ответы, репозиторий и видео; ошибки санитизированы (без URL, заголовков,
  тела ответа и токена).
* Пустой токен → честный `not_configured`, **без** сети и **без** выдуманного
  успеха.
* До живой отправки пользователь должен сам начать диалог с ботом (иначе Telegram
  вернёт ошибку «bot can't initiate conversation»); это фиксируется в README/UI,
  не в коде инструмента.
* `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` в `.env.example` пустые; настоящие
  значения — только в `.env` (не читается и не коммитится).
* **Изоляция окружения:** процесс B читает `.env` только при
  `NOTIFIER_LOAD_DOTENV=1`; тесты и harness обязаны передавать
  `NOTIFIER_LOAD_DOTENV=0`, фиктивный токен и loopback base URL. `agent/settings.py`
  **не** читает `TELEGRAM_*` — ни один TELEGRAM-секрет не попадает в backend,
  модель, trace или UI.

## 14. Ограничение «только последний прогон» и его решение

* A хранит до **50 последних прогонов на задание**
  (`storage/tasks.py::MAX_RUNS_PER_TASK`), но единственный инструмент чтения
  `get_latest_search_run` отдаёт **только последний** прогон; инструменты в A
  **не добавляются** (§2).
* **Накопленные за простой/рестарт монитора.** Если фоновый агент (монитор) был
  остановлен и за это время у задания A накопилось несколько прогонов, монитор
  после рестарта обрабатывает **ровно последний** прогон. Промежуточные
  накопленные прогоны **сознательно не «догоняются»** (no backfill) — их не
  отдаёт никакой инструмент A.
* **Итог обработки:**
  * items, присутствующие в **последнем** прогоне, подходящие по критерию и ещё
    не в seen-set → обрабатываются и уведомляются;
  * items, бывшие **только** в промежуточных прогонах и отсутствующие в
    последнем → **сознательно пропущены** (ограничение ниже);
  * уже seen items → **не повторяются**.
* **Baseline это не затрагивает.** Baseline поглощается первым прогоном со
  `status in {"ok","empty"}` (§7) и к моменту простоя, как правило, уже пройден.
  Если baseline ещё **не** поглощён (watch создан, монитор сразу остановлен, а
  прогоны накопились), первый **достоверный** прогон после рестарта всё ещё
  трактуется как baseline и **не рассылает** историческую выдачу.
* **Осознанное ограничение:** items, появившиеся в промежуточном прогоне и
  исчезнувшие к моменту проверки, могут быть **пропущены** — последний прогон их
  уже не содержит. Это принимается: A не хранит полную историю items, а
  добавление такого инструмента в A выходит за границы (§2). Ограничение
  документируется и проверяется как ожидаемое поведение, а **не дефект**.
* **Компенсация:** следующий достоверный прогон приносит актуальную выдачу;
  старые ссылки не рассылаются как «новые». Проверка — D20-28.

## 15. VPS

VPS-развёртывание **не выполняется** в этой задаче; `deploy_vps.*` не меняется.
Чек-лист оператора (ручной, после отдельного решения пользователя):

1. Запустить третий сервис B (`notifier_server`) под process manager с
   `Restart=always`, отдельный порт 8766.
2. `NOTIFIER_DB_PATH` задать абсолютным путём вне деплой-каталога (например
   `/var/lib/day16/day20-notifier.sqlite3`) в юните B; каталог принадлежит
   пользователю сервиса.
3. `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` — только в окружении сервиса B
   (EnvironmentFile), права файла ограничены.
4. `MCP_NOTIFIER_URL` в backend-юните указывает на loopback-порт B.
5. Проверить, что `/api/mcp/servers` показывает A и B, а недоступность B даёт
   `connected:false`, не 5xx.
6. Убедиться, что существующий деплой A/backend не требует изменений (A не
   переписывается).
7. Резервное копирование БД B отдельно от БД A.

## 16. Ограничения

* Выбор инструментов реальной локальной моделью не детерминирован; промпт
  требует порядок, но фактическое следование проверяется LIVE/UI.
* Реальный Tavily и реальный Telegram в автоматике не вызываются; REAL Telegram
  — только opt-in (`NOTIFIER_REAL_ALLOW=1`), реальный Tavily остаётся BLOCKED.
* Пропуск items, появившихся и исчезнувших между проверками (§14).
* **Накопленные за простой монитора прогоны:** обрабатывается только последний
  прогон, промежуточные не «догоняются» (no backfill); если baseline ещё не
  поглощён, первый достоверный прогон после рестарта трактуется как baseline и
  историческую выдачу не рассылает (§14, D20-28).
* Единственная доставка — Telegram; другие каналы (e-mail, webhook) и несколько
  получателей вне границ.
* **Provenance A→B не подтверждается (S12).** Данные из A в B идут через модель:
  B доверяет переданному `run`/`items` и не может доказать их происхождение.
  Компенсация — acceptance проверяет **payload фейкового Telegram** (URL из
  детерминированного `tests/support/fake_search.py::RESULT_URLS`), а **не текст**
  модели; PASS на одном `tool_completed.ok=true` недостаточен.
* **Порты (S10).** `smoke_test.bat` задаёт `MCP_TEST_PORT=8766` для сервера A,
  поэтому B в smoke **не** может брать default 8766. Введён отдельный
  `MCP_NOTIFIER_TEST_PORT` (default 8768); `require_free_ports([...])` включает
  порт B; handshake B подтверждается по server name; `MCP_NOTIFIER_URL` backend
  строится из этого порта. `EXPECTED_MCP_POSTS_PER_PROBE = 2 × число
  сконфигурированных серверов` (в smoke A+B → **4**); окно замера выполняется с
  `NOTIFIER_MONITOR_ENABLED=0`, чтобы фоновый монитор не исказил счёт.
* **Изоляция окружения (S9).** `NOTIFIER_LOAD_DOTENV=0` обязателен в тестах и
  harness; иначе B прочитает реальный `.env`. `NOTIFIER_MONITOR_TICK_SECONDS`
  default 30 c, clamp 1..3600.
* **Объединённый tools_count.** Если B доступен, `tools_count` в агрегированном
  `mcp_list_tools` = 15 (A: 9 + B: 6, `per_server`); тесты/снимки, жёстко
  ожидающие `9`, разделяются на «инструменты A» и «объединённый список».
* `NOTIFIER_MONITOR_ENABLED` по умолчанию `on`: тесты обязаны изолировать сеть и
  Telegram-фейк, иначе monitor может сделать сетевой вызов. В unit-тестах монитор
  **не стартует** (`create_app(..., enable_monitor=False)`).
* Монитор вызывает модель в фоне: без доступной модели monitor-ход честно
  фиксируется как неполный, без выдуманной доставки.
* **Тема уведомлений в автоматике.** Детерминированный `fake_search` отдаёт
  Python-фикстуры, поэтому LIVE/INT-сценарий уведомлений мониторит эту тему;
  Xbox-сценарий из §1 остаётся пользовательским демо-прогоном.

## 17. Будущее развитие (тренировочный сценарий ж/д билетов)

Развитие документируется, но **не** реализуется в дне 20. Сценарий «найди
ж/д билеты на четверых (двое взрослых, двое детей)» требует достоверного
**источника наличия** четырёх подходящих мест и итоговой цены на всех
пассажиров. Поисковые сниппеты (как в `search_web`) такой проверкой **не
являются**: они не подтверждают актуальное наличие мест и цену. Реализация
потребует отдельного источника/провайдера и отдельного SPEC; в дне 20 фиксируется
как известное ограничение и направление развития.

## 18. Критерии приёмки (D20-01 … D20-29)

Полные уровни проверки и команды — в `ACCEPTANCE.md`; трассировка реализации — в
`PLAN.md`.

| ID | Требование |
| --- | --- |
| D20-01 | Два процесса: A (`python -m mcp_server`, 8765, инструменты не изменены) и B (**предлагаемый** `python -m notifier_server`, 8766, `/mcp`, свой `tools/list`); B не импортирует `agent.*` |
| D20-02 | **Предлагаемый** `agent/mcp_hub.py::McpHub`: `probe_servers()` (параллельно, `asyncio.gather`) и `probe_server(label)` возвращают `ServerProbe(label,status,tools)`; протокольные методы A-first; `McpStatus`/`FakeMcpClient` не меняются; коллизия имён → `connected=false`/category `protocol`; пустой `MCP_NOTIFIER_URL` → B `connected:false`/`not_configured`, B не опрашивается; `McpTool.server` default `None` |
| D20-03 | `Orchestrator` пишет `server` в `tool_selected`/`tool_completed` (и SSE `tool_call`/`tool_result`) только когда сервер известен; legacy без hub байт-совместим с днями 16–19; `mcp_connect` — по записи на сервер; `mcp_list_tools` — агрегат `per_server` |
| D20-04 | При недоступном/несконфигурированном B поведение дней 16–19 сохраняется (работает только A); пустой URL → B `not_configured`; `/api/mcp/servers` → `connected:false`; `/watches` → `available:false`; без 5xx |
| D20-05 | В `tools/list` B 6 инструментов; каждый возвращает structured |
| D20-06 | `create_notification_watch` сохраняет **явный** критерий (`keywords`, регистронезависимо по title/description, опц. `exclude`) и расписания, возвращает их |
| D20-07 | `evaluate_run` принимает весь результат A, детерминированно применяет критерий, возвращает `{status,new_items,matched_count,known_count,is_baseline,should_notify,note}`; `error`/`empty`/`pending` — честно; malformed `run`/неизвестный status → structured `status:"error"`, чужой watch → `status:"unknown_watch"`, **без** `ToolError`; seen-set в обычном режиме не меняется; расписание двигается только при `should_notify=false` |
| D20-08 | Baseline поглощается **только** первым прогоном со `status in {"ok","empty"}`: помечает подходящие items seen, `is_baseline=true, should_notify=false`; `pending`/`error` baseline не поглощают; старая выдача не рассылается |
| D20-09 | `send_notification` — единственная точка отправки и записи seen/delivery; идемпотентность по `(watch_id, kind, period_key)`; `duplicate` только при существующей `status='sent'`, при `failed`/`not_configured` — повторный `UPDATE` (`attempts+1`), `not_required` строку не создаёт; по stopped/чужому watch → `ToolError`; двигает расписание во всех терминальных статусах; возвращает `delivery_id` |
| D20-10 | Seen-отпечатки пишутся только при фактической обработке; неполный ход не «съедает» уведомление, следующий тик повторяет |
| D20-11 | Отдельная БД B (`NOTIFIER_DB_PATH`, default `data/day20-notifier.sqlite3`): `watches`, `seen_items`, `deliveries`, UNIQUE `(watch_id, kind, period_key)`, своё версионирование; не пишет в БД A |
| D20-12 | Состояние B переживает рестарт; повтор результата/прогона/рестарт не создаёт дубль; `failed`/`not_configured` повторяются через `UPDATE` (`attempts+1`), а не блокируются навсегда |
| D20-13 | Telegram: токен/получатель читает только B (в `.env` только при `NOTIFIER_LOAD_DOTENV=1`); получатель не аргумент модели; единственная сетевая граница `notifier_server/telegram.py`; токен вне `repr`/чата/trace/логов/репозитория; `agent/settings.py` не читает `TELEGRAM_*`; пустой токен → `not_configured` без сети |
| D20-14 | **Предлагаемый** `agent/monitor.py`: перечисляет подписки через `ChatService.list_chats()` (≤5) + host→B `list_notification_watches`, фильтрует `active`/`next_check_at<=now`; при первом недоступном B прерывает тик (одна сессия, без модели и без открытия БД B); `Orchestrator.run(..., trigger="monitor", watch_id, system_prompt, allowed_tools, require_result)`; неполный ход → только `monitor_incomplete{watch_id,reason}` (`evaluate_run_absent`/`send_notification_missing`/`send_notification_failed`) вместо `request_done`; `request_start.trigger`; тик default 30 c (clamp 1..3600); `NOTIFIER_MONITOR_ENABLED` default on |
| D20-15 | HTTP: `/api/mcp/status` и `/api/mcp/tools` сохраняют семантику A; `GET /api/mcp/servers` — один `probe_servers()`; `GET /api/chats/{chat_id}/watches`: неизвестный chat → 404 `chat_not_found` (до B), B недоступен → HTTP 200 `{available:false,error}`; UI без периодического опроса (≤2 host→B вызовов на действие) |
| D20-16 | UI: `MCP status` показывает оба сервера; строки `Technical details` помечены `[A]`/`[B]`; панель `Notification watches`; доставка не выдаётся за успешную по тексту модели |
| D20-17 | Отпечаток по `mcp_server/reports.py::normalize_url`; логика не дублируется |
| D20-18 | `.env.example` содержит `MCP_NOTIFIER_URL`, `MCP_NOTIFIER_HOST/PORT`, `NOTIFIER_DB_PATH`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `NOTIFIER_TELEGRAM_API_BASE_URL`, `NOTIFIER_MONITOR_ENABLED`, `NOTIFIER_MONITOR_TICK_SECONDS` (default 30 c, clamp 1..3600), `NOTIFIER_LOAD_DOTENV` (default 1) без настоящих значений; harness передаёт `NOTIFIER_LOAD_DOTENV=0` |
| D20-19 | Общая формулировка ограничения «только последний прогон»: закрывается seen-множеством B без новых инструментов A; конкретное поведение при накоплении/рестарте — D20-28 |
| D20-20 | LIVE: реальная локальная Qwen ведёт цепочку A→B (`get_latest_search_run` → `evaluate_run` → `send_notification`); внешние Tavily и Telegram — фейки; в отчёте — фактическая модель |
| D20-21 | INT: реальные процессы A и B, реальный HTTP, `tools/list` каждого, loopback-фейк Telegram |
| D20-22 | UI (Playwright): оба сервера и строки вызовов A и B |
| D20-23 | Opt-in REAL Telegram (`NOTIFIER_REAL_ALLOW=1`), иначе `BLOCKED`; реальный Tavily остаётся `BLOCKED` |
| D20-24 | Внутренний прямой вызов A→B запрещён; оркестрация только через `McpHub` и модель, что видно в trace |
| D20-25 | Регрессии дней 16–19 (инструменты A, SSE, `Technical details`, задания/планировщик, отчёты, лимит 5 чатов, discovery) сохранены |
| D20-26 | Ж/д сценарий — только в документации; отмечает необходимость достоверного источника наличия/цены; сниппеты не считаются проверкой |
| D20-27 | Не изменяются `.bat`, `deploy_vps.*`, governance-файлы, Control Center, Templates; нет VPS-развёртывания, commit/push/деплоя; автоматика не вызывает реальный Tavily/Telegram |
| D20-28 | Накопленные за простой/рестарт монитора прогоны: A хранит до 50 прогонов/задание (`storage/tasks.py::MAX_RUNS_PER_TASK`), но `get_latest_search_run` отдаёт только последний; монитор обрабатывает ровно последний, промежуточные сознательно не «догоняются» (no backfill); items последнего прогона, не seen, уведомляются; items только промежуточных прогонов пропущены; уже seen не повторяются; baseline не рассылает историческую выдачу, если ещё не поглощён (§14/§7) |
| D20-29 | Разные новые материалы в одном периоде не подавляются: для `kind="new_items"` `period_key` **контентный** (хэш множества отпечатков), а не временной период; два разных материала → два разных `period_key` и две доставки `sent`; повтор того же множества → `duplicate` (без сети); временной `period_key` применяется только к `kind="summary"`; один вызов с двумя items — одна доставка, два вызова с разными множествами — два сообщения (§5.4/§8) |
