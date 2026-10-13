# Developer Assistant

Независимый облегчённый учебный центр для работы с документацией выбранного проекта.
Одна локальная модель или явно выбранный DeepSeek, RAG, настоящий MCP и безопасные System Tools.
Приложение не зависит от других учебных недель или частных сервисов.

## День 31

Реализованы команда `/help`, индекс README + docs + API/data описаний, получение текущей
Git-ветки через отдельный MCP stdio сервер и ответы с относительными ссылками на источники.
Дополнения: задачи с коротким планом, очередь, Stop, история, импорт документации,
System Tools, поиск через Tavily MCP, планируемые задачи и отправка сохранённого отчёта владельцу в Telegram.
Исполнение завершённого запроса не означает независимую проверку правильности ответа.

## Запуск

Нужны Python 3.12+ и Git. В Windows запустите `setup.bat`, затем `run_app.bat`.
Первый запуск также создаёт отдельное `.venv`, если оно отсутствует.
Интерфейс открывается по адресу `http://localhost:8791` и остаётся доступным без API-ключей.
Зависимости устанавливаются из `requirements.txt`; статический UI не требует Node или CDN.

Создайте `.env` по `.env.example`. `DEEPSEEK_API_KEY`, `LOCAL_LLM_API_KEY`,
`TAVILY_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` штатно загружает только приложение.
Они никогда не индексируются и не публикуются. Telegram отправляет только в заданный чат.

Локальную OpenAI-compatible модель настройте в `local-data/settings.json` по
`config/settings.example.json`. По умолчанию ожидаются сервер localhost:8792/v1
и alias `local-model`. Можно использовать готовый сервер или указать собственный
llama.cpp executable/model/args; пути и веса остаются в локальных ignored данных.
Внешняя служба модели не останавливается приложением. Собственный runtime закрывается
при штатном завершении приложения. Неправильная или занятая служба не заменяется.

## Как пользоваться

Главный экран — чат: диалоги слева, сообщения в центре, поле ввода внизу.
Нажмите **New chat**, задайте название через **Rename**. **Delete** перемещает неактивный
чат в Recently deleted; восстановление доступно в Workspace → History. Сохранённые
запуски, источники и отчёты не удаляются. Перед удалением нужны Stop и Pause активных расписаний.

1. Выберите Selected project. По умолчанию открыт сам Developer Assistant.
2. Откройте **Workspace → Documents** и нажмите **Index README + docs**.
3. Напишите `/help Explain the project structure and the role of RAG and MCP.`
   Выберите Local либо DeepSeek и нажмите **Send** или Enter. Shift + Enter добавляет строку.
4. Ответ появится сообщением. **Sources**, **Plan**, **Run details** раскрываются у этого ответа:
   выдержки и SHA-256, план, реальный MCP result и полный журнал доступны по необходимости.
5. **Stop**, **Retry**, **Telegram report** относятся к конкретному запуску. Retry создаёт новый ID в том же чате.
6. **Workspace** сохраняет прежние функции: Documents (индекс/импорт/ветка/другой проект),
   Tools (дерево/чтение/поиск/доверенные тесты), Schedules и History.
7. **Import documentation** копирует .md/.txt/.json/.yaml в docs/imported; после импорта обновите индекс.
   Можно выбрать файл или вставить текст. Существующий файл не перезаписывается.
8. **Project /help** использует документацию, **Web · Tavily** — отдельный MCP-поиск по Интернету.
   **Options** позволяет явно включить дополнительное исследование через System Tools.

Чаты организуют сохранённые запуски. Контракт генерации остался прежним: каждый /help
использует собственный вопрос и найденные доказательства проекта; вся история автоматически
в модель не передаётся. Внутренние docs/specs и docs/evidence не входят в пользовательский RAG-корпус.

## Структура проекта

- `assistant/api.py` — HTTP API и loopback границы.
- `assistant/service.py` — план, один worker, bounded workflow, Stop.
- `assistant/store.py` — SQLite задачи, версии документов и FTS5 BM25 retrieval.
- `assistant/project_mcp.py` — отдельный SDK MCP server с git_branch/project_files/git_status.
- `assistant/tools.py` — SDK client, registry/executor, System Tools, Tavily и Telegram.
- `assistant/provider.py` — streaming выбранного Local или DeepSeek, без скрытого fallback.
- `assistant/scheduler.py` — сохраняемые разовые/повторные задачи и доставка готового отчёта.
- `assistant/runtime.py` — запуск только явно настроенного локального runtime.
- `static/` — простой English UI, без звуков, мигания или сборки frontend.
- `docs/architecture.md`, `docs/api.md`, `docs/data-schema.json` — документация для RAG.
- `tests/` — изолированные offline проверки, без настоящего `.env` и внешней сети.
- `local-data/` — приватная runtime-конфигурация, SQLite, reference; не в Git.

## Инструменты

**MCP**: настоящий subprocess stdio discovery и calls; git_branch различает обычную,
unborn и detached ветку, а также папку без Git. Ветка не задана строкой в приложении.
**System Tools**: дерево текстовых файлов, чтение до 160 строк, literal search
по первым 200 строкам файлов, trusted tests `smoke` или `unit` только для этого модуля.
Другой проект не получает разрешения выполнять свой код автоматически.
Нет универсального shell, удаления, произвольных записей или git push инструмента модели.
**Tavily**: отдельная явная web задача через официальный remote MCP. Её результаты
не выдаются за сведения из документации проекта.
**Telegram MCP**: отдельный stdio server send_self_report(report_id) отправляет сохранённый результат; повторная отправка
того же task ID предотвращается. Неопределённый результат доставки не повторяется автоматически.

## Проверки и ограничения

`test.bat` выполняет offline suite, `smoke_test.bat` — smoke. Проверки с настоящими
моделями/сетью выполняются отдельно только с разрешением владельца.
Keyword RAG не равен обучению модели и не гарантирует поиск по любым перефразировкам.
Наличие citation или успешного HTTP-запроса не является содержательной приёмкой.
Статусы задачи: queued/running/completed/failed/cancelled; quality по умолчанию NOT_ASSESSED.
После аварийного перезапуска незавершённые задачи становятся failed с причиной interrupted.

Прототип D31 помогает понимать проект. Он ещё не обещает автономно реализовывать
произвольные большие задания. План и подзадачи сохраняют зависимости; для будущего
изменения кода потребуется отдельный разрешённый контракт и независимая проверка.

## Планируемые задачи и отчёты

Введите вопрос, выберите Model и будущий момент Run at local time. Repeat after completion
равный 0 означает один запуск, положительное число — интервал в минутах после завершения.
Schedule question сохраняет задание в SQLite. Приложение должно оставаться открытым.
Свободный worker выполняет одно задание; пропущенные интервалы не повторяются пачкой.
Pause schedule запрещает будущие старты, Stop останавливает уже выбранный запуск.
После прерывания зафиксированного запуска расписание ставится на паузу для проверки истории.
Send completed report to my Telegram доставляет сохранённый готовый ответ только владельцу.
Отказ или неопределённая доставка не вызывают автоматической повторной отправки.

## Проверенное поведение D31

Штатный test.bat проходит 45 изолированных проверок. Независимо проверены живые Local и
DeepSeek ответы по источникам, настоящий MCP, импорт/версии, Stop, scope, ограниченный
цикл, Tavily с тремя URL и разовая задача с реальной Telegram-доставкой и дедупликацией.
Исторические модельные ошибки и Tavily 429 не стёрты и не считаются успешными примерами.
Сводка проверки — docs/evidence/day-31/verification.json. Учебный Word и демонстрационный
MP4 передаются отдельно; веса моделей, ключи, локальная база и полная reference-статья в код не входят.

## Сохранение прежней истории

Миграция создаёт Previous conversations для старых запусков проекта и привязывает
их через отдельную таблицу task_chats. Исходные payload, ID, ответы, версии источников
и Telegram delivery ID сохраняются. Новые чаты имеют собственные имена и связи с проектом.
Неактивное удаление — обратимый архив, а не физическое уничтожение доказательств.
Расписание привязано к чату: после архивирования его нельзя снова включить до восстановления.

## Day 32: automatic pull request review

D32 extends this same application. A GitHub pull request proposes changes from a
separate branch; the reviewer never merges or modifies that branch. The pipeline is
`PR event → fixed base/head diff → documentation + related code retrieval → selected
model → validated JSON/Markdown → GitHub comment, artifact and optional Telegram`.

Open **PR review** in the chat header or Workspace. Connect `owner/repository` and
the project directory inside it (this module defaults to `week-07/developer-assistant`).
Refresh lists open PRs. **Review with selected model** uses the composer Local or
DeepSeek choice and saves a turn into the current chat. **Sync GitHub report** imports
the CI result without invoking a model. Diff, coverage, source versions and data flow
expand below the report. Stop cancels the same single worker used by ordinary /help.
Source excerpts include repository, exact SHA, relative path, lines and content hash.
The optional **Watch GitHub reports for this chat** checkbox checks at most five open
PRs every 60 seconds while this browser page remains open. It only imports existing CI
reports, never invokes a model, does not overlap polling cycles, and pauses on a changed
project/chat/repository or an error. Imports target the captured chat, not a newly selected
one. Duplicate heads remain deduplicated. It is not a server-side scheduled model run.

The automatic workflow is `.github/workflows/day32-review.yml`. It triggers on
`pull_request_target` opened/synchronize/reopened and only same-repository PRs whose
changes include this module. It checks out the exact trusted **base** commit, installs
base dependencies and reads head files only via REST. It does not execute/import PR
code, install head dependencies, accept shell commands from PR fields or run on a home
computer. Fork PRs skip the secret-dependent job. Actions are pinned to reviewed SHAs.

Repository Actions **secrets** required for CI: `DEEPSEEK_API_KEY`. Optional owner
notifications: `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Configure these through
GitHub settings, never commit their values. `GITHUB_TOKEN` is provided by Actions with
contents read and pull-requests write. Optional Actions variable `D32_DEEPSEEK_MODEL`
selects the model; default `deepseek-flash`. Manual reviews preserve the application’s
existing Local/DeepSeek parameters and never switch providers silently. Cloud runners
cannot reach a model at this PC’s loopback address; Local review therefore runs here.

Public repository reads work without authentication but GitHub limits request rates.
The optional **Use existing Git login** owner action verifies that the repository
matches the selected project's Git origin, uses the normal noninteractive OS Git
credential helper, and keeps authentication in memory scoped to that repository.
It never returns or stores the credential, exports keys, or asks the model to authenticate.
Alternatively an owner can configure `GITHUB_TOKEN` for the application environment.

The trusted workflow invokes `python -m assistant.review_cli --event "$GITHUB_EVENT_PATH"
--provider deepseek --publish --telegram --output local-data/review-output`. Event fields
are read and validated as JSON; they are not inserted into shell commands. The same CLI
supports `--repo owner/repository --pr 1 --scope project/directory` for an authorized
manual setup. The Actions job summary and `d32-review` artifact retain review.md/json;
one owned marked PR comment is updated for a new head. A superseded head cannot publish
as current, and imports label older results STALE. A validated saved review can trigger
a short fixed-owner Telegram message. Persisted intent prevents blind duplicate sends;
uncertain delivery remains uncertain, and notification failure does not erase analysis.

The reviewer retrieves a **separate FTS5 corpus** of fixed documentation and code
snapshots. The D31 README/docs index remains unchanged. Small per-file review units
carry relevant docs, siblings/callers and changed code; only selected excerpts go to the
model. It processes at most 12 selected files, 12 documents, 20 related code paths,
8 retrieved chunks per unit, and a 12000-character patch per file. Missing/binary/
oversized/truncated patches, unsupported/deleted-only cases, omitted selected files and
missing context are explicit PARTIAL limitations. No provider calls are made for a unit
without both documentation and code context. Context discovery is bounded; full semantic
understanding of the repository is not promised. Transient provider failures retry at
most three times; schema/permission failures are not looped.

Findings carry category, severity, changed HEAD path/line, explanation, impact,
recommendation and supplied evidence IDs. Malformed JSON, fake source IDs and invalid
line coordinates fail validation. A completed execution is **not** a semantic PASS:
quality stays `NOT_ASSESSED` until independent assessment. Empty categories mean no
substantiated finding, not guaranteed bug-free code. Reviewer tools cannot execute PR
scripts or obey instructions embedded inside code comments/documents.
The report separates execution_status, coverage_status, collection_limitations and
model_limitations. For example, all files can be technically processed while the model
states that tests were not run: coverage can be complete, the semantic limitation stays
visible, and quality does not become PASS. Overall review status remains partial when
limitations are present; that label does not silently erase completed file processing.

`examples/review_demo` is an isolated benign quotation fixture with its contract in
`docs/review-demo.md`. A demonstration PR may introduce a wrong discount formula or
rename `total` while its caller still expects it. The working app does not import this
fixture, and faulty demo code must not merge. Correct changes provide false-positive
controls. `tests/test_review.py` verifies scope, snapshots, pagination, retrieval, output
validation, Stop/restart, shared-worker serialization and notification/import dedup.
Offline fixtures do not prove LIVE model accuracy or an actual Actions run; those require
separate evidence. D31 historical evidence remains in `docs/evidence/day-31`.
