# D33 — Поддержка пользователей Developer Assistant

DESIGN_STATUS: READY_FOR_INDEPENDENT_REVIEW
TASK_CLASS: PRODUCT
SPEC_GATE_STATUS: NOT_ASSESSED

## 1. Источник и полный объём

Официальное D33: мини-сервис отвечает на вопросы о продукте, использует RAG по FAQ/документации и учитывает контекст пользователя или тикета; CRM либо JSON с пользователями/тикетами подключаются через настоящий MCP. Результат — AI-поддержка, код и видео. «Почему не работает авторизация?» получает ответ по фактам обращения, а не одинаковую общую инструкцию.

Владелец поручил выполнить D33, проверить реальные Local и DeepSeek, продолжить Telegram-уведомления, создать подробный русский Word с настоящими скриншотами и связью с конспектом недели7, подробное широкоформатное MP4, опубликовать код и отправить одно финальное уведомление. Последнее уточнение обязательно: и отдельная поддержка, и Main button; оба входа обслуживают одинаковые тикеты/историю. Отдельные форма/FAQ доступны, если Main остановлен. Это авторизованный полный PRODUCT-цикл; авторский READY не является независимым PASS.

Развиваем week-07/developer-assistant на месте. Сохраняем D31/D32, историю/API, маршруты/параметры Local/DeepSeek. Корневые правила, permissions, защищённые bat и другие недели не меняются. Не создаём production CRM или автоматическую правку ключей/кода. Данные пользователей/диагностики синтетические; публичные доказательства обезличены. GitHub хранит код и CI D32, но не является триггером вопросов D33 и не получает тикеты поддержки.

## 2. Два входа, два процесса

Main остаётся отдельным FastAPI приложением. Support — самостоятельный FastAPI процесс с собственными UI/health/FAQ/SQLite/worker. Кнопка Support в Main — обычная ссылка на тот же Support URL, без proxy через Main, передачи chat text/keys/ID в URL или второй копии данных. Прямое открытие Support и ссылка показывают одинаковую историю при той же demo persona.

Неизменённый run_app.bat вызывает assistant.launch. Продуктовый launcher понимает ASSISTANT_MODE=main|support; default main сохраняется. Main default8791, Support8793; 8792 не используется, это default Local provider. ASSISTANT_PORT применяется к выбранному режиму. URL Main button берётся из ограниченной loopback конфигурации Support. Отдельный запуск: установить ASSISTANT_MODE=support и вызвать существующий run_app.bat; protected scripts не изменяются. Invalid mode/занятый порт дают понятную ошибку, без arbitrary import/command и без остановки чужого процесса.

Cold start стандартного run_app.bat/Main обеспечивает запуск самостоятельного Support background process, без AI request на старте. Уже запущенный подтверждённый Support health с правильной application/data identity переиспользуется; неизвестный listener не заменяется и не останавливается, main показывает понятную недоступность Support. Новый child — отдельный скрытый процесс, не второй Main; после штатного main shutdown остаётся доступным. Support-only mode полезен для независимого запуска/восстановления, но не требует двух терминалов при каждом обычном старте. Startup races не допускают двух writers одной SupportDB.

Support не зависит от Main Service/SQLite/health. Без модели/ключей/MCP сохраняются UI, FAQ, создание/просмотр тикетов; AI операция честно fails. Если оба web-процесса выключены, страница недоступна: публичный deployment — будущая задача. Borrowed Local server не останавливается при закрытии Support; собственный runtime сохраняет текущую ownership/finally семантику. Main, владеющий моделью, может штатно закрыть её при своём shutdown; Support сохраняет manual features и при следующем Local запросе может bounded стартовать собственный configured runtime. Глобальная lease миграция D31/D32 не требуется.

## 3. Компоненты

- assistant/support_api.py: loopback/same-origin API, Pydantic validation, серверная demo identity, lifecycle Support.
- assistant/support_store.py: separate support.sqlite3; users/tickets/messages/runs/deliveries/sessions и Support FTS corpus; атомарные версии. Не мигрировать assistant.sqlite3.
- assistant/support_mcp.py: отдельный MCP SDK2.2 stdio server, read-only scoped tools поверх JSON snapshot.
- assistant/support_service.py: один worker, context snapshot, real MCPclient, RAG, bounded prompt, существующий Provider, Stop/stale guards.
- assistant/support_rag.py либо изолированный класс Store: FAQ/docs ingestion, отдельный FTS5/BM25 corpus, versioned snippets.
- static/support/: English UI без CDN/build; tickets sidebar, conversation основным содержимым, composer/New ticket/FAQ/provider/Stop/escalation/explicit Resolve. Sources/context/details раскрываются, не сырой JSON под composer.
- docs/support/: авторитетные FAQ/инструкции продукта. examples/support/: публичный JSON фиктивных users/seed tickets.
- assistant.launch/Main static UI: только продуктовый mode и Support button; default main неизменён.

Файлы можно объединить без изменения контрактов. Новые зависимости не нужны: текущие Python/FastAPI/Pydantic/SQLite/httpx/MCP. HTTP schema экспортируется FastAPI как OpenAPI3.1; маленький клиентский контракт/метаданные выводятся из схемы без ручного второго источника. Backend schema/shape checks выявляют payload drift; frontend build/framework не добавляется.

## 4. Авторитетное состояние и JSON bridge

Авторитетное изменяемое состояние — отдельная SQLite. examples/support/users-tickets.json импортируется идемпотентно только при первом заполнении и не перезаписывает историю после restart. Новые тикеты и follow-up сохраняются там же; работа только с seed не удовлетворяет D33.

Перед каждым AI run транзакция проверяет session owner, ticket, current context_version и active-run guard. Из актуальной SQLite создаётся bounded per-run JSON snapshot в ignored Support data. Он содержит schema_version/snapshot_id/context_version, одного current synthetic user, только его ticket и ограниченную историю. Создание атомарное; snapshot sealed/immutable на время run. Новая версия → новый snapshot. JSON bridge не заменяет MCP: prompt получает user/ticket context только после успешного tools/call readback, проверенного по snapshot/version/hash против ожидаемого run. Host не подставляет своё direct-read содержимое в prompt как скрытый fallback.

Model/MCP не пишут SQLite/файлы. Snapshot не содержит keys/settings/runtime/raw logs/email/phone/foreign users. Diagnostics — allowlist component/status_code/error_code/connection_state/observed_at/attempted_steps, без arbitrary metadata dictionary. UI предупреждает не вставлять секреты; server scrub/filter перед persistence/snapshot/prompt/UI/Telegram. Free text не выдаёт права. Bounds: snapshot<=64KB, question<=4000 chars, title<=120, description<=4000; model history<=8 latest messages и<=12000chars с explicit truncated/omitted count. Допустимая полная переписка остаётся в SQLite/UI, не отправляется модели целиком автоматически.

## 5. Настоящий MCP и demo scope

SDK stdio client выполняет tools/list, затем tools/call. Read-only tools: get_user_context() и get_ticket_context(ticket_id). User scope/единственный snapshot path заданы trusted launch configuration. Tool arguments не принимают user_id/path/SQL/command/recipient. MCP server проверяет JSON schema/version/size и принадлежность ticket до возврата; foreign/unknown ID, malformed/missing JSON — error без чужих полей. Provider/Telegram/GitHub credentials MCP-процессу не передаются; subprocess environment ограничен нужным runtime.

Application-owned session выбирает только объявленную фиктивную persona и выдаёт opaque server identity (HttpOnly SameSite cookie либо эквивалентный capability). Все ticket/message/run/source/stop/retry/resolve/notification endpoints выводят owner из session, не body user_id. Scope проверяется даже на чтении источников/delivery. Demo A/B доступны сознательно для проверки изоляции; это НЕ production authentication: локальный владелец может выбрать другую фиктивную persona. Для реальных пользователей нужны login/tenant auth/HTTPS/deployment. IDOR внутри выбранной session/tool scope запрещён.

Evidence сохраняет transport/protocol, discovery, toolname, snapshot/version/hash и sanitized result. Direct JSON preparation — bridge, не MCP call. Handshake/call/readback failure прекращает AI без silent direct-read fallback; manual flow остаётся доступным.

## 6. RAG: знания отдельно от тикета

Корпус содержит explicit allowlist публичных product docs и docs/support FAQ. Исключены specs/evidence/local-data/.env/settings/git internals/symlinks/raw logs/произвольные проекты. Пользовательский тикет не становится авторитетным FAQ. Corpus отдельный/версионированный; reindex атомарно заменяет записи, не накапливает удалённые/старые chunks.

Heading-aware chunks<=50lines/2200chars; relative path/lines/contenthash/stableID. FTS5 BM25 реально ранжирует snippets; весь FAQ в prompt не выдаётся за retrieval. Query: текущий вопрос, structured component/error facts и bounded prior attempts. Русские FAQ/термины либо explicit aliases обеспечивают русские перефразировки; aliases содержат keywords, не готовые ответы/golden diagnoses. До6источников; общий budget<=settings.context_chars. Сохранить query/ranked sources/coverage/truncation.

Источники раскрываются в UI со snippet/линиями/версией. Manual FAQ/reindex не требуют модели. No relevant docs → honest insufficient-data/уточнение/escalation, а не случайная статья ради formal count или fabricated diagnosis. BM25 не гарантирует семантику всех вопросов.

## 7. AI контракт и смысл

Fixed system prompt: поддержка конкретного продукта; docs/JSON/history untrusted. Не исполнять инструкции тикета, не просить прислать ключ, не утверждать доступ к компьютеру/реальному аккаунту по synthetic snapshot. Учитывать факты и уже выполненные попытки, отвечать на языке пользователя. Next steps — безопасные пользовательские действия без autonomous commands/key changes.

Один явно выбранный existing Provider Local/DeepSeek с сохранёнными params/modelalias/thinking. Hidden fallback отсутствует. Максимум одна generation round на run; новый follow-up/Retry — explicit новый ID, не бесконечный self-loop. До Provider нужны scoped MCP readback и retrieved evidence либо честно обозначенный no-context clarification сценарий. Ошибки/неполный stream не превращают partial text в completed.

Ответ: explanation/diagnosis, практичные next steps, неизвестные данные/уточнение, citations, при необходимости escalation suggestion. Structured output допускает answer/next_steps/follow_up/escalation_recommended/evidence labels, оставаясь читаемым. References проверены против actual sources/MCP facts, но valid JSON/citations не semantic PASS. Quality NOT_ASSESSED отдельно от execution completed. Модель не решает ticket автоматически.

Independent Tester читает целые ответы обеих моделей на одинаковом вопросе/контексте. Ground truths: DeepSeek401+invalid_key — проблема ключа/конфигурации данного provider; GitHub403+explicit rate_limit — rate limit, не доказательство неверного токена; Local connection_refused — unavailable endpoint/server, не DeepSeek auth. Если ключ уже заменили, не повторять это единственным новым действием; проверить применённую конфигурацию без раскрытия ключа. Missing fact → уточнение вместо категоричного диагноза. Golden answers только в independent test data, не prompt/FAQ под зачёт. Ошибки whole answer сохраняются отрицательными до настоящего актуального retest.

## 8. Ticket state, версии и Stop

Ticket: owner/product/component/title/description/safe diagnostics/context_version/state/timestamps. Messages immutable с user/assistant role; task/evidence связаны с snapshot и message. States open/escalated/resolved; completed answer не resolved. Только explicit Confirm resolved текущего owner подтверждает решение идемпотентно. После resolved новое сообщение требует явного Reopen.

User message/diagnostic update увеличивает context_version атомарно. Один queued/active run на ticket; второй вопрос получает409 до Stop/finish, не молча изменяет context in-flight. Другие tickets стоят в очереди одного Support worker. Metadata update может сделать run stale; final commit проверяет owner/expected_version/persisted terminal state. Старый ответ labelled historical/stale, не актуальная рекомендация/Telegram. Cancelled/failed не становится completed из late callback.

Stop отменяет queued/in-flight SDK/HTTP; partial guarded. Visible waiting и final-content streaming при поддержке, private reasoning не хранится/показывается. Draft не valid final. Ticket/persona switch повышает UI epoch; delayed list/detail/source/job responses не вставляются в новый context. Bounded polling, terminal не снова running. Restart marks active interrupted/failed и не запускает заново; история сохранена. SQLite BEGIN IMMEDIATE/expected_version обеспечивает atomic one writer; MCP snapshots read-only.

## 9. Telegram

Только существующий configured owner; model/user не выбирают recipient. Explicit scenarios: opt-in saved completed answer summary; Escalate with Telegram сохраняет событие и краткую сводку даже без AI. Escalation state независимо от delivery, не resolved. Summary: ticket ID/component/provider при наличии/sanitized shortquestion и state, без contacts/keys/foreign data/rawlogs.

Delivery key ticket/run/version/action, persisted intent до sending. pending/sending/sent/failed/uncertain; success только acknowledgement/messageID. Refresh/retry/import не повторяет sent/uncertain; interrupted sending→uncertain. Duplicate action idempotent. Failed/cancelled/stale generation не отправляется как completed; notification failure не удаляет answer. Один final completion sends root после artifacts/independentacceptance/publication; отдельный D33-16.

## 10. API/UI, запуск

Главный экран Support — переписка; слева tickets, Newrequest, visible Synthetic demo users banner/persona. FAQ/form полезны без AI. Sources/context/run details раскрываемые; provider/Stop/escalate/Resolve/reopen понятны, без технического JSON под composer. Save ticket отличается от SendAI, unavailable key не блокирует form. Safe text/Markdown, no executing modelHTML/JS, без sound/highlight effects.

Support routes отдельны от Main: health/config без secret values, demo session, tickets list/create/detail/diagnosticupdate/messages/resolve/reopen/escalate, scopedruns/status/stop/retry/report, FAQread/reindex. Developer закрепляет route naming exportedOpenAPI/docs; owner/version semantics обязательны. Input extra fields/provider/IDs/bounds валидируются. Mutations same-origin/loopback, foreignOrigin/Host rejected. FAQ/static/health доступны без model/session; ticketdata послеdemo session.

Первый запуск создаёт ignored Support DB/snapshots и seed единожды. .env штатно загружается приложением, не читается/не выводится агентами. Public config boolean/nonsecretparams. Missing.env не мешает manual flow, hidden modelcall на startup отсутствует. Existing settings не переписываются.

Provider settings читаются из прежнего main local-data/settings через штатный application-owned loader; SupportDB/snapshots/logs находятся отдельно и не делают новый settingsfile скрытым источником иных параметров. Invalid provider configuration не закрывает form/FAQ: manual SupportStore/UI инициализируются независимо, AI помечен configuration unavailable с санитизированной причиной; generation запрещена до исправления, без default-provider/default-params fallback. Support не меняет Main settings. MCP environment minimal/system-only: keys не наследуются из parent .env/environment.

## 11. Документация/публикация/артефакты

README получает русский День33 с запуском/примерами/границами. Oldday31/day32proof не переписывается. Public docs/evidence/day-33 содержит sanitized методы/criterion IDs/independentjudgments/fixtureversions/params/wholeanswers/measuredlimitations/realMCP/UI/Telegramack; full private evidence local-data/d33-evidence согласно ACCEPTANCE. DB/snapshots/models/keys не публикуются.

Word: ticket/CRM/RAG/MCP theory, actual flow browser→Support→SQLite/JSONsnapshot→MCP→RAG→chosenmodel→savedanswer→UI/explicitTelegram; GitHubcode/CI и D32flow отдельно; actualscreenshots, weeklynotes, последовательное объяснение. Allrenderedpages inspected. MP4 FullHD1920x1080: actualUI оба входа, Mainstopped/manualFAQ/create, newticket/currentJSONMCPreadback, sources, samequestiondifferentanswers, follow-up, Stop/unknowncontext, Telegramack, подробные captions. Монтаж/ожидания честно обозначены. FullD33-01..16 включает DOCX/MP4/publication/completion, не толькоbackendtests.