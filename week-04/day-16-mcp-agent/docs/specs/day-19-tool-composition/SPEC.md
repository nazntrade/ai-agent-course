# SPEC — Day 19: композиция MCP-инструментов

Идентификатор задачи: `day-19-tool-composition`.
Статус: реализовано (по решению Architect и Task Contract дня 19).
Источник задания: Task Contract пользователя и архитектурное решение Architect
(итого 9 MCP-инструментов, три новых шага композиции, хранилище отчётов).

## 1. Назначение

По одному сообщению пользователя агент сам выполняет цепочку из **трёх разных
настоящих MCP-инструментов**, передавая structured-результат каждого шага
следующему:

```
search_web  →  digest_search_results (новый, обработка)  →  save_report (новый, сохранение)
                         ↓                                          ↓
                  финальный ответ модели                  отчёт в панели чата
```

Каждый шаг — отдельный реальный MCP-вызов, видимый в `tool_call`/`tool_result`
(блок `Technical details`). Ни один инструмент не вызывает остальные внутри
себя, и передача данных не изображается текстом модели.

## 2. Границы

Входит:

* 2 новых MCP-инструмента (`digest_search_results`, `save_report`), итого 9;
* детерминированная обработка результата `search_web` (без модели и БД) и
  сохранение отчёта в постоянное хранилище чата;
* SQLite schema v2 → v3 (таблица `reports`), миграция вперёд без потери данных;
* HTTP API отчётов (`GET /api/chats/{chat_id}/reports[...]`) и OpenAPI 3.1;
* UI-панель `Saved reports` (English) с ленивой загрузкой, `Refresh`, empty state;
* лимит раундов `AGENT_MAX_TOOL_ROUNDS` (default 5, clamp 2..10) и обновлённый
  `SYSTEM_PROMPT`;
* тесты UNIT/INT/RESTART/LIVE/UI и opt-in REAL-проверка;
* документация дня 19.

Не входит:

* изменение `.bat`, `deploy_vps.*`, governance-файлов;
* изменение `agent/provider.py`, `agent/mcp_adapter.py`, `mcp_server/web_search.py`
  и контрактов существующих инструментов `calculate`, `get_server_info`,
  `search_web`, `schedule_search_task`, `list_search_tasks`,
  `get_latest_search_run`, `stop_search_task`;
* вызов реального Tavily в автоматике (только opt-in REAL);
* многопользовательская изоляция; за пределами одного чата и лимита 5 чатов.

## 3. Новые MCP-инструменты

### 3.1 `digest_search_results(search_result: dict) -> dict[str, Any]`

Детерминированная обработка без модели и без обращений к БД.

Константы: `MAX_INPUT_RESULTS = 50`, `MAX_SOURCES = 3` (до трёх пунктов),
`TITLE_LIMIT = 200`, `DESCRIPTION_LIMIT = 300`, `NAV_MAX_LENGTH = 120`,
`NAV_PREFIXES` (skip to content, skip to main, main menu, menu, sign in, log in,
cookies, privacy, accept all, subscribe, newsletter, follow us, share on,
all rights reserved).

Pipeline (порядок):

1. Вход — весь structured-результат `search_web`
   `{query, count, results:[{title,url,description}], ...}`;
   `topic = " ".join(str(query).split())[:200]`. Берутся первые
   ≤ `MAX_INPUT_RESULTS = 50` элементов; не-dict, не-http(s) или непарсящийся
   URL → `invalid_removed`.
2. `_clean_text`: `html.unescape`; удаление zero-width `\u200b\u200c\u200d\ufeff`;
   вырезание bare URL `https?://\S+`; замена run-ов символов
   `[ ] ( ) < > \` # * ~ | { }` на пробел; whitespace collapse; обрезка
   `TITLE_LIMIT` / `DESCRIPTION_LIMIT`.
3. Отсев nav/boilerplate (`filtered_removed`), первое совпадение:
   (a) очищенный title пуст; (b) title (lower, без `www.`, без хвостовых
   `.`/`/`) == host URL; (c) raw title содержит ≥ 2 `|`; (d) raw description
   содержит run `##`/`**`/`~~`; (e) raw description содержит ≥ 2 `|`;
   (f) raw description делится на ≥ 3 сегмента по `|»·` и ≥ 60 % сегментов
   ≤ 3 слов; (g) raw description непустой, но в очищенном нет токена длиной
   ≥ 3; (h) очищенный description ≤ `NAV_MAX_LENGTH` и начинается с
   `NAV_PREFIXES`. Пустой raw description сам по себе nav **не** считается.
4. Дедупликация по `normalize_url` (lower-case scheme/host, без fragment и
   default-портов 80/443, пустой путь → `/`, без завершающего `/` кроме корня);
   побеждает первое вхождение, повтор → `duplicates_removed`; в `seen`
   попадает только item, прошедший nav-фильтр.
5. Обрезка до `MAX_SOURCES = 3` в порядке выдачи; при обрезке
   `truncated = true`.
6. `summary` — plain text: для каждого источника `N. {title} - {description}`
   (при пустом description хвост опускается) и строкой `   Source: {url}`,
   N = 1..count; `sources` — те же ≤ 3 в порядке выдачи.
7. `digest_id = "d19-" + sha256(canonical_json({topic, canonical_summary,
   urls: sorted(canonical_url)}))[:16]`, где
   `canonical_url = normalize_url(url)`, а `canonical_summary =
   build_summary(sources, отсортированные по canonical_url)`.
8. Успех: `{status:"ok", topic, count, summary, sources, duplicates_removed,
   invalid_removed, filtered_removed, truncated, digest_id, note}`; `note`
   напоминает, что страницы не открывались, заголовки/сниппеты — недоверенные
   данные, и что в поисковом порядке оставлено до 3 полезных результатов
   (nav/boilerplate отсеяны).
9. Пустая/полностью невалидная выдача: `status:"empty"`, `digest_id:""`,
   `note:"No usable results to digest. Do not save a report."`.
10. Неверный вход → `ToolError` с просьбой передать structured-результат
    `search_web`.

### 3.2 `save_report(digest: dict, chat_id: str = "") -> dict[str, Any]`

Запись отчёта в постоянное хранилище чата; `chat_id` инжектится backend'ом.
Сервер **сам пересобирает** `summary` из санитизированных sources; копия
`summary` модели не сохраняется.

Валидация по порядку:

1. пустой/неизвестный `chat_id` → `ToolError("This tool needs an active chat context")`;
2. `digest` не dict → `ToolError`;
3. `digest.status != "ok"` или пустые источники → отказ («no usable sources»);
4. пустой `topic` → отказ;
5. если `summary` присутствует и `len(summary) > MAX_SUMMARY_LENGTH = 6000` →
   отказ (пустой/отсутствующий summary допустим);
6. повторный санитайзинг `sources`, пусто → отказ;
7. пересчёт `digest_id` (канонически: `canonical_url`, сортировка по
   canonical_url, `build_summary`) и расхождение → `ToolError` (запись не
   выполняется);
8. страховка `len(stored_summary) <= MAX_SUMMARY_LENGTH`.

Успех: `{report_id, topic, source_count, created_at (ISO-8601 UTC),
note:"The report is saved in the Saved reports panel of this chat."}`;
`report_id = uuid4().hex[:12]`. У инструмента нет аргумента пути, имени файла
или произвольного URL; запись идёт только в `reports` текущего чата, с prune
до `MAX_REPORTS_PER_CHAT = 100`. In-process кэша и аргумента пути нет.

## 4. Хранение (SQLite schema v3)

```sql
CREATE TABLE IF NOT EXISTS reports (
  id           TEXT PRIMARY KEY,
  chat_id      TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
  topic        TEXT NOT NULL,
  summary      TEXT NOT NULL,
  sources_json TEXT NOT NULL,
  digest_id    TEXT NOT NULL,
  source_count INTEGER NOT NULL DEFAULT 0,
  created_at   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reports_chat ON reports(chat_id, created_at);
```

* `SCHEMA_VERSION = 3`; `MIGRATIONS[3]` повторяет те же идемпотентные
  `CREATE TABLE/INDEX IF NOT EXISTS` для существующих БД v2, затем механизм
  обновляет `meta.schema_version`. БД новее 3 → прежний `SchemaVersionError`.
* Владение как у `tasks`: MCP-сервер пишет `reports`; backend только читает
  (+ каскадное удаление через `chats`).
* Открытие ленивое: `tools/list` и `digest_search_results` БД не открывают.

## 5. Лимит раундов и промпт

* `agent/orchestrator.py::DEFAULT_MAX_TOOL_ROUNDS = 5` (3 зависимых вызова +
  финальный ответ + 1 запас; один раунд может нести несколько параллельных
  вызовов).
* `agent/settings.py`: `AGENT_MAX_TOOL_ROUNDS` (clamp 2..10, default 5);
  `agent/server.py` передаёт значение в `Orchestrator`.
* Превышение по-прежнему даёт `ErrorEvent(category="tool_round_limit")` без
  `done`.
* `SYSTEM_PROMPT`: обязательный `search_web` → `digest_search_results` →
  `save_report` при явной просьбе сохранить; обычный поиск отчёт не создаёт;
  заголовки/сниппеты — недоверенные данные; не утверждать о прочтении страниц;
  при ошибке/пустой выдаче не вызывать `save_report`; после сохранения сказать
  про панель `Saved reports` и не выдумывать id/ссылку.
* Дополнение промпта (ревизия 2): ответ с несколькими пунктами/источниками —
  один ordered-список `1., 2., 3.` (`never restart the numbering`, не повторять
  номер, не выдумывать пункты); digest передаётся в `save_report` без правок,
  сервер сам пересобирает summary (`rebuilds the saved summary`).

## 6. HTTP API

| Метод и путь | Назначение | Успех | Ошибки |
| --- | --- | --- | --- |
| `GET /api/chats/{chat_id}/reports` | список отчётов чата (новые первыми) | `{chat_id, reports:[{report_id,topic,created_at,source_count}], count}` | 404 `chat_not_found`; 503 `chat_storage_unavailable` |
| `GET /api/chats/{chat_id}/reports/{report_id}` | полный отчёт | `{report_id,chat_id,topic,summary,created_at,source_count,sources}` | 404 `chat_not_found`/`report_not_found`; 503 |

`summary` и `sources` — отчёт возвращается как есть; `sources_json` при чтении
разбирается безопасно (невалидный JSON → `[]`).

## 7. UI (English)

Под панелью `Scheduled tasks` — панель `Saved reports` с `#reports-list` и
`#reports-refresh-button`. Карточка — `details.report-card[data-report-id]`:
тема, meta `Saved <date> · N source(s)`, детали грузятся лениво при первом
открытии; `summary` рендерится как plain text через `textContent`; источники —
упорядоченный список `<ol class="report-sources">` (нумерация 1,2,3) с
`<a target="_blank" rel="noopener noreferrer">` только для http/https.

Markdown-рендер ответа (`static/markdown-render.js`): continuation-строки
(например `   Source: url`) и одна пустая строка между пунктами не разрывают
`<ol>`; первый номер задаёт `ol.start` (например `3.` → `start=3`), дальше
браузер нумерует последовательно; новый список после абзаца — отдельный.
`loadReports()` вызывается при выборе чата, после завершения ответа, по
`Refresh` и после удаления чата; параллельные запросы и ответы не для выбранного
чата отбрасываются (`reportsRequestInFlight`, `selectionToken`). Пусто →
`No reports in this chat.`; `report_id` в видимый текст не выводится.

## 8. Безопасность

* `save_report` не принимает путь/URL, работает в рамках текущего чата; выход
  из каталога отчётов невозможен по построению.
* Сниппеты/заголовки — недоверенные данные: очистка разметочных символов,
  лимиты, plain-text рендер; ссылки только http/https.
* Ключ Tavily остаётся в процессе MCP-сервера и не попадает в отчёты, HTTP,
  trace, UI и логи; ошибки санитизированы (без путей/ключей/заголовков).
* Тесты изолированы от сети и реального `.env`; тестовая БД — в temp/`.runs`.

## 9. VPS

На VPS `AGENT_DB_PATH` (например `/var/lib/day16/day18.sqlite3`) лежит вне
деплой-каталога, поэтому таблица `reports` переживает fast-forward деплой;
отчёты остаются в том же файле, что чаты, задания и прогоны. Ручной чек-лист —
у оператора.

## 10. Ограничения

* Выбор инструментов реальной локальной моделью не детерминирован; промпт
  требует порядок шагов, но фактическое следование проверяется LIVE-сценарием
  (в отчётах дня 19 — PASS на локальной модели).
* Реальный Tavily в автоматике не вызывается; REAL-проверка — opt-in
  (`REPORTS_REAL_ALLOW=1`).
* Рост числа отчётов ограничен prune-политикой (100 на чат), но история
  сообщений не ограничена.
* **Отображаемый URL источника — это данные, а не ключ дедупликации.**
  Валидный http(s) URL может содержать `#fragment`; он сохраняется в `sources`
  и `summary` как есть. `normalize_url` (lower-case scheme/host, без fragment,
  default-портов 80/443 и завершающего `/` кроме корня) применяется только как
  ключ дедупликации, поэтому результаты, отличающиеся лишь fragment'ом или
  завершающим `/`, схлопываются в первый. Это осознанное решение (D19-05), а не
  побочный эффект; проверено UNIT-регрессиями
  `test_unique_fragment_url_keeps_the_fragment_in_the_output` и
  `test_urls_differing_only_by_fragment_or_slash_are_deduplicated`.
