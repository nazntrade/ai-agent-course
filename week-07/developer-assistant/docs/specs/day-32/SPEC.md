# D32 — Автоматическое AI-ревью кода

DESIGN_STATUS: READY_FOR_INDEPENDENT_REVIEW
TASK_CLASS: PRODUCT
SPEC_GATE_STATUS: NOT_ASSESSED

## 1. Источник, авторизация и границы

Официальное задание D32: пайплайн автоматически анализирует новый PR, получает diff и изменённые файлы, использует RAG по документации и коду и возвращает текст ревью: потенциальные баги, архитектурные проблемы и рекомендации. Минимум — автоматический запуск на PR; результат сдаётся как видео и код.

Владелец прямо поручил выполнить всё задание, проверить реальные DeepSeek и Local, добавить уведомления в Telegram, создать подробный Word-учебник с настоящими скриншотами и широкоформатное MP4 с подробными объяснениями. Это действующая авторизация на реализацию и ограниченные реальные вызовы выбранных сервисов. Опубликованный учебный модуль Developer Assistant развивается на месте. Публикация разрешена владельцем; её выполняет основной исполнитель после независимых проверок, не дочерние роли.

Продуктовая область: существующий week-07/developer-assistant и один учебный workflow в .github/workflows. Не менять другие недели, роли, permissions и корневую governance. Сохранить работающие D31 чат, /help, docs RAG, genuine MCP, System Tools, Tavily, расписания, историю, Stop и выбранные модели. Ни приватная инфраструктура, ни реальные ключи, настройки или пути компьютера не входят в публичный код и доказательства. Приложение штатно загружает ключи; агенты не читают .env или local-data/settings.json.

Полноценная проверка реального PR/Actions обязательна. Отсутствующая облачная конфигурация остаётся конкретным BLOCKED/NOT_ASSESSED, а не заменяется локальным mock. Исполнитель проверяет возможность безопасной установки нужных GitHub Actions secrets без раскрытия значений; наличие секрета до preflight не предполагается.

## 2. Две точки запуска, один проверяющий движок

Автоматическая цепочка:
PR opened / synchronize / reopened → событие pull_request_target и доверенный base workflow GitHub Actions → PR metadata, base/head SHA, changed files и diff → commit-scoped RAG по docs и коду → DeepSeek API → валидация ответа → review.json + review.md, job summary и один обновляемый PR-комментарий → уведомление владельцу.

Ручная цепочка:
чат приложения → выбрать PR и Local либо DeepSeek → тот же collector/retrieval/review engine → сохранённый отчёт с источниками в чате → по включённому уведомлению Telegram.

GitHub запускает наш код, а не сам анализирует PR. Cloud runner не обращается к локальной модели домашнего компьютера. CI использует явно настроенный DeepSeek; Local анализ выполняется в приложении с его существующим runtime acquire/use/release. Нет скрытого переключения моделей или запуска частного сервера. Self-hosted runner и доступ домашнего компьютера из публичной сети не входят в D32.

Предпочтительные компоненты: assistant/github_review.py (GitHub reader), assistant/review.py (snapshot, review corpus, bounded engine, result contract), assistant/review_cli.py (CI CLI), интеграция в существующие Service/Store/API/UI. Разделение допустимо уточнить без усложнения контракта; одинаковая семантика CLI и UI обязательна. Сохранять Provider.completion и его stream/finish validation; review не выдаёт модели shell, web, sender или write tools.

## 3. PR snapshot и безопасный GitHub reader

Для каждого запуска сохранять repository, PR number, base_sha, head_sha, title, URL, changed-file manifest, время и provider. Принимать GitHub PR URL либо явно выбранный repository + integer PR number, валидировать owner/repo/number и официальный HTTPS origin. Произвольный URL, credentials в URL, redirects на неофициальный origin, traversal и shell arguments запрещены.

Получать metadata и пагинированный files API, затем diff либо воспроизводимый diff фиксированных снимков. Использовать relative paths, additions/deletions/status/old filename, hunk координаты old/new и список пропущенных данных. Snapshot читает docs/code именно нужного commit, не текущую изменённую рабочую папку и не mutable branch name. Base обозначает прежнюю версию, head — предлагаемую; нельзя смешивать их под одним label.

PR-файлы всегда данные: не выполнять head-код, не устанавливать его dependencies, не импортировать модуль из head и не запускать его тесты с ключами. Используется pull_request_target (opened, synchronize, reopened): workflow берётся из доверенной base-ветки, движок и его dependencies checkout по точному event base SHA. PR head workflow/dependencies не исполняются, даже если автор PR изменяет workflow, requirements или review engine. Все недоверенные поля PR передаются через event JSON/проверенный API payload; title/body/path не интерполируются в shell run. Реализация review feature должна быть опубликована в базовой ветке до демонстрационного PR; намеренный дефект живёт только в отдельном учебном fixture, не в работающем приложении. Checkout PR merge/head не является доверенным исполнением.

Первая поддерживаемая автоматическая область — PR из веток того же репозитория; их содержимое всё равно недоверенное. Fork PR явно SKIPPED до обращения к модели или любым secret-dependent шагам, независимо от технической доступности secrets в pull_request_target. pull_request_target разрешён исключительно при описанном trusted-base исполнении: head только bounded API text data; никакого head checkout/import/install/test execution. Secrets загружаются только процессом приложения/CI, не попадают в prompt, artifacts, комментарии или Telegram. GitHub token иметь минимальные contents:read и pull-requests:write для чтения и одного комментария; недоступность write permission отражается отдельно от результата анализа.

Файлы с secret names, runtime data, binary/NUL, symlink/submodule metadata и неподдерживаемыми форматами не индексируются. Для каждого исключения/лимита сохранять путь и причину без чтения секрета. Если исключение оставляет часть PR непроверенной, coverage PARTIAL, не COMPLETE.

## 4. RAG для документации и кода

D31 docs corpus сохраняется прежним. D32 использует отдельный corpus namespace repository + commit + kind, чтобы не загрязнять /help и не подменять актуальные project documents. Документация — README, docs и API/data schemas, исключая отчёты проверок и спецификации процесса. Код — bounded supported text sources, близкие объявления, callers и tests. Учитывать нейтральные правила проектного scope.

Индексировать snapshot manifest и chunks с source_id, относительным path, sha/version, source_kind (doc/code/test), start/end line, текстом и hash. SQLite FTS5/BM25 достаточно для учебного проекта: embeddings/вторая локальная модель не обязательны. Retrieval использует changed paths, symbols и hunk текст; всегда включать diff и подбирать docs/code контекст для него. Изменённый файл сам по себе не заменяет retrieval связанных исходников и документации.

Ограничивать число файлов, размер каждого файла, число/размер chunk, prompt и output по явной конфигурации. Нельзя молча обрезать diff или context. Разбивать допустимый PR по файлам/логическим группам; сохранять рассмотренные hunks и unresolved coverage. После файловых проверок учитывать межфайловые зависимости. Малый PR может проходить один review request; большой не превращается в пяти-ролевой бесконечный цикл.

Если контекст не найден или противоречив, фиксировать limitation и требуемую проверку. Citation означает доступный источник, а не доказанную истинность замечания. Хранить источник для открытия в UI без неограниченного доступа к snapshot файлам.

## 5. Контракт результата и честное качество

review.json schema_version, review_id, repository/pr/base_sha/head_sha, origin (ci/manual), provider, created, execution_status, coverage, limitations, reviewed_files/hunks, sources, findings, summary, usage и elapsed_s. execution_status: completed, failed, cancelled, skipped; coverage: COMPLETE/PARTIAL/NOT_ASSESSED. Quality по умолчанию NOT_ASSESSED, независимая содержательная оценка сохраняется отдельно.

Finding: category bug / architecture / recommendation, severity high / medium / low, relative path, сторона и line либо конкретный range, title, explanation, consequence, recommended_fix, supporting source_ids и uncertainty. Для общепроектной архитектурной рекомендации разрешён связанный файл/range, но не выдуманный diff-line. Markdown включает три явно названных раздела даже при пустом finding category. Пустой список не означает независимое «ошибок нет»: это только «модель не сообщила замечаний в указанной области».

Строго валидировать JSON/schema, категории, source IDs, координаты и принадлежность snapshot. Неполный stream/finish_reason=length, невалидный JSON, придуманные references и отсутствующие обязательные поля — failed либо явное непроверенное ограничение, никогда clean review. Не публиковать raw reasoning. Ошибки временной сети/429 могут иметь до трёх ограниченных попыток с bounded delay; ошибки прав/схемы/постоянный отказ не повторять бесконечно. Измерять usage/latency по реальным данным; cost только когда известны тариф и usage, иначе unknown.

Содержательное качество проверяется человеком/независимым Tester по исходным требованиям и golden cases, а не по длине ответа, наличию citations, самооценке или числу пройденных unit tests. В отчёте не обещать безошибочность моделей и не скрывать ограничения выбранной keyword retrieval.

## 6. Публикация, повторность и Telegram

CLI создаёт одинаковый review.json/review.md, job summary и artifact даже при безопасно обработанном failed/partial результате; неуспешный статус CI не маскируется. PR-комментарий с marker и head SHA обновляется, не создаётся на каждый poll. Перед публикацией повторно получить current head; stale result хранится для истории, не выдаётся за текущее ревью. Новый head cancels obsolete Actions job через concurrency group; каждый report связан с точным SHA.

Приложение умеет выбрать репозиторий, получить список открытых PR, синхронизировать опубликованный review report и показать его в чате; ручная Sync доступна, bounded optional polling разрешено владельцем как часть подключения. Imported CI report не выдаётся за новую ручную Local проверку. Источник (GitHub/Manual), provider, SHA, coverage и delivery status видимы. Не доверять произвольному тексту комментария как валидному report: проверять schema, repository/PR/head и предназначенный marker.

Telegram sends fixed configured owner only. Уведомление содержит PR link/number, SHA, provider, краткие findings/status, ссылка на отчёт; без raw source dumps, ключей, произвольных адресатов и hidden reasoning. CI или приложение могут отправлять уведомление в одной выбранной точке; delivery key repository + PR + head + report origin/version предотвращает дубли, в том числе при импорте уже уведомлённого CI report. HTTP uncertainty остаётся uncertain; не делать слепые повторные отправки. Failure/partial также сообщаются честно; failure Telegram не переписывает review result.

После завершения всей задачи основной исполнитель отправляет отдельное правдивое уведомление владельцу «D32 завершён» с фактическим состоянием deliverables. Это явное поручение владельца, не model-controlled отправка. Если часть критериев не принята, сообщать фактический partial/blocker, а не «завершён».

## 7. UI, регрессии, доказательства и сдача

Существующий чат остаётся главным. В Workspace удобная секция Pull requests с repository, PR list/link, Sync, review provider и Review; report появляется читабельным turn/card в чате. Diff, sources, details доступны раскрываемо; видно ожидание/Stop, SHA и origin. No sounds/blink; UI English. /help и прочие D31 операции сохраняются.

Offline проверки используют временные repos/SQLite, fake credentials и GitHub/provider doubles. Golden cases включают known bug, cross-file contract regression, docs/code disagreement, benign change, prompt injection, malformed refs, incomplete/oversized diff, stale SHA, cancellation, provider error и notification dedup. LIVE минимум одинаковый известный баг и безопасное изменение на обоих выбранных providers с независимым чтением полного ответа; UI проверяется реальным браузером. Отдельно реальный PR должен вызвать настоящий Actions workflow и публикацию результата; новая revision должна повторить запуск.

Word подробно объясняет PR vs MR, ветка/commit/base/head, Actions trigger/runner/secrets, collector, RAG, provider, результат, comment/sync/Telegram и ограничения; соединяет фактическую реализацию с недельным конспектом. Только реальные скриншоты, весь document rendered и просмотрен. Видео landscape MP4 минимум 1920x1080 показывает running UI, настоящий PR, trigger/run/log-safe summary, changed files/diff, docs+code sources, findings, исправляющий commit и rerun, импорт/ручную проверку и Telegram. Читаемые captions объясняют что/куда/кому/зачем передаётся. Код и reproducing README готовы к публикации; tracked/untracked всей репозитории проверены, remote сверён после push.

Критерии и методы в ACCEPTANCE.md заморожены до Developer. Общий D32 PASS требует всех обязательных D32 и owner criteria; спецификация READY и отдельная фиксация не равны сдаче.

## 8. Уточнение интеграции с текущим исполнителем

Review jobs регистрируются в существующем Service.jobs и используют тот же Service.lock, а не второй параллельный worker. Общие Stop/close/restart guards охватывают review и /help; повтор после терминального состояния создаёт новый идентификатор, сохраняет историю и исходный PR/provider. Импорт завершённого CI report не занимает модель и не оживляет cancelled task. Settings/Provider выбранных моделей и их temperature/thinking остаются прежними; D32 не является задачей перенастройки моделей. Telegram formatter различает D31 help и D32 review, не маркирует новый отчёт старым днём.

GitHub Actions dependencies выбираются по официальным текущим release и закрепляются полными immutable commit SHA, проверенными исполнителем, вместо копирования устаревших версий из учебного слайда. Это dependency продукта, не изменение governance или расширение локальных permissions.
