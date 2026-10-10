# SPEC — Private LLM Service, D30

Статус: SPECIFICATION.
Класс задачи: PRODUCT.
Модуль: private-llm-service.
Исходный контракт: acceptance-contract-v2, C01–C16 в ACCEPTANCE.md.

## 1. Результат и границы

Создать приватный HTTP-сервис локальной LLM и отдельное лёгкое Android-приложение.

Приложение позволяет:
- настроить адрес сервиса и токен устройства;
- создавать, переименовывать и удалять беседы;
- отправлять сообщения и получать ответы;
- видеть ожидание, загрузку модели, подготовку контекста, генерацию и конечный результат;
- отменять запрос;
- восстанавливать состояние после поворота экрана, перезапуска и обрыва соединения.

Модель выбирается владельцем сервера через существующий AI-SERVER. Android не передаёт имя модели, upstream URL, ключ AI-SERVER, владельца или system prompt.

Первый выпуск текстовый. Голос, изображения, RAG, переключение моделей клиентом и облачные AI-провайдеры не входят в выпуск.

UI приложения — English. README и эксплуатационные инструкции — Russian. Публичные исходники, документы и результаты проверок используют нейтральные значения без личных путей, адресов, имён и содержимого частной переписки.

Имеющийся эмулятор является выбранным устройством проверки APK. Он не заменяет проверку внешней сети.

## 2. Компоненты

1. AI-SERVER:
   - выбирает и запускает установленную локальную модель;
   - передаёт дочернему процессу актуальную среду подключения;
   - выдаёт, обновляет и освобождает test lease;
   - предоставляет аутентифицированный подсчёт реального prompt;
   - сохраняет существующее управление моделями и запуск других проектов.

2. Python gateway:
   - FastAPI, Pydantic 2, httpx;
   - SQLite через sqlite3, версионируемые миграции;
   - аутентификация устройств и разделение владельцев;
   - беседы, сообщения, задания и идемпотентность;
   - один worker генерации, ограниченная очередь;
   - точный бюджет контекста;
   - snapshot, SSE и резервный polling;
   - безопасная остановка и восстановление после перезапуска.

3. Android:
   - один app module, Kotlin, XML Views;
   - ViewModel, Coroutines/Flow, OkHttp;
   - обычные DTO/API генерируются из OpenAPI 3.1;
   - SQLiteOpenHelper для небольшого локального кэша и pending requests;
   - Android Keystore и AES-GCM для токена устройства;
   - ручной parser SSE по явно заданной схеме.

4. Harness:
   - изолированные fake upstream и fake clock;
   - проверки SQLite, scheduler, HTTP, клиента и lifecycle;
   - отдельные выбранные LIVE-сценарии;
   - сохранение проверяемых JSON-артефактов.

## 3. Структура

- gateway/app — HTTP, domain, storage, scheduler, context, upstream, lifecycle.
- gateway/tests — unit и integration.
- android — исходники приложения, wrapper, manifests, resources и тесты.
- harness — сценарии, сбор результатов и проверка manifests.
- docs/specs/d30-private-llm — SPEC, PLAN, ACCEPTANCE.
- docs/operations — установка, pairing, запуск, LAN, HTTPS, troubleshooting.
- docs/artifacts — объявленные нейтральные JSON-результаты.
- .runtime — локальные данные; исключены из публикации и архива исходников.

Модули domain/storage/upstream не зависят от Android. Контракт HTTP один для приложения, тестов и документации.

## 4. Подключение к существующему AI-SERVER

Не создавать отдельный process-local импорт менеджера моделей: он получил бы другой токен и независимое состояние.

Добавить валидируемый optional named launch profile для модуля внутри уже найденного course project. Существующий default launch profile сохраняется.

Выбор профиля:
- только из локальной конфигурации;
- не принимает произвольную команду или произвольный workdir от HTTP-клиента;
- workdir разрешается внутри выбранного project root;
- executable проверяется по текущим ограничениям app runner;
- неизвестный profile отклоняется;
- environment не возвращается клиенту и не записывается в отчёт.

Для выбранной локальной модели дочерний gateway получает согласованный AI_TEST_MODEL_* profile, включая base URL, model ID, API key и lease URL. Использовать уже существующее построение полного test environment; не дублировать получение ключа.

Отсутствующий profile даёт явную configuration error. Частичный profile отклоняется. Stub не считывает реальный profile или .env.

Модель не включается при обычном запуске gateway. Cold start выполняется при первом принятом задании.

## 5. Точный подсчёт контекста

Расширение AI-SERVER: узкий аутентифицированный endpoint `/v1/token-count` внутри выбранного local-model proxy.

Основной путь подсчёта — нативный runtime endpoint `POST /v1/chat/completions/input_tokens`. В установленном runtime commit он зарегистрирован и использует тот же OpenAI-compatible chat processing, что и inference. Это предпочтительнее самостоятельного соединения apply-template/tokenize.

Gateway формирует один нормализованный chat-completion body. Подсчёт и inference получают одинаковые messages и влияющие на template/tokenization параметры. Клиент Android не может задавать эти параметры. Нельзя после подсчёта незаметно изменить system message, reasoning/template kwargs или содержимое prompt.

Wrapper:
1. Проверяет bearer token, разрешённую структуру и фактический размер тела.
2. Проверяет действующий lease именно выбранной модели.
3. Удерживает use reference на протяжении запросов к runtime.
4. Передаёт согласованный body в `/v1/chat/completions/input_tokens`.
5. Принимает только корректный положительный integer `input_tokens`.
6. Получает фактическую вместимость slot из `/props.default_generation_settings.n_ctx`.
7. Возвращает только token count, effective slot capacity и безопасную информацию о методе подсчёта.
8. Освобождает use reference в finally.

Не возвращать rendered prompt, внутренний путь модели, upstream credentials или содержимое messages в diagnostic logs.

JSON профиля модели не доказывает actual slot capacity. Вместимость берётся из работающего runtime и подтверждается preflight.

В preflight один и тот же нормализованный body проходит native count и короткий inference. Полученный count сопоставляется с фактическим `usage.prompt_tokens`/prompt evaluation runtime. Несовпадение расследуется до основной реализации; оценка по символам запрещена.

Резервный `/apply-template` + `/tokenize` допускается только для отдельно подтверждённой capability конкретного runtime. До использования доказать точные add_special/parse_special, generation prefix и одинаковые template kwargs через сопоставление с фактической prompt evaluation. Не включать резерв автоматически при auth error, timeout, malformed response или произвольной ошибке upstream.

Отключение reasoning допускается только при подтверждённой одинаковой обработке count и inference. Shared model profile не меняется. При отсутствии доказательства сохраняются поддерживаемые настройки с описанием фактического поведения.

Нормализованный inference body использует `stream=true` и подтверждённый `stream_options.include_usage`. Native input_tokens endpoint получает тот же согласованный body; transport/usage options не меняют messages или template kwargs и не используются для приблизительного подсчёта.

Preflight подтверждает, что native count принимает этот body, а streaming usage.prompt_tokens/prompt evaluation совпадает с input_tokens. Usage нельзя выводить из длины отображённого ответа.

## 6. Модель данных

SQLite — источник истины. Foreign keys включены; WAL, busy timeout и короткие транзакции. Одна служба с одним worker process.

Таблицы:
- owners: ID, enabled, timestamps;
- devices: ID, owner ID, token hash, label, revoked timestamp;
- conversations: owner ID, title, revision, deleted timestamp, active job ID;
- messages: conversation ID, role, text, job ID, completion status, timestamps;
- jobs: owner/conversation/device IDs, state, state version, enqueue order, deadlines, output, error code, finish reason;
- idempotency: owner/key unique, canonical request hash, job ID, expiry/tombstone;
- events: job ID, seq, state version, event type, snapshot payload, timestamp;
- schema_migrations.

Секрет AI-SERVER не сохраняется в SQLite. В БД устройства хранится хеш высокоэнтропийного токена; plaintext показывается только при целевом одноразовом pairing.

Admission сохраняет пользовательское сообщение вместе с заданием. В контекст включаются только завершённые пары user/assistant. Частичные, отменённые и failed задания исключены.

После restart незавершённые задания атомарно переводятся в failed с `service_restarted`. Автоматическая повторная inference запрещена. Запрос с прежним idempotency key возвращает прежний конечный результат.

## 7. HTTP API

OpenAPI 3.1 является контрактом и источником обычных Android DTO/API.

Public:
- GET /healthz — только нечувствительная liveness.

Authenticated:
- GET /v1/status;
- GET /v1/me;
- GET, POST /v1/conversations;
- GET, PATCH, DELETE /v1/conversations/{id};
- GET /v1/conversations/{id}/messages;
- POST /v1/conversations/{id}/requests;
- GET /v1/requests/{id};
- GET /v1/requests/by-key/{key};
- POST /v1/requests/{id}/cancel;
- GET /v1/requests/{id}/snapshot;
- GET /v1/requests/{id}/events.

Bearer token определяет владельца. Любой lookup проверяет ownership. Чужой ресурс возвращает 404 без раскрытия его существования.

Admission body: text и expected_revision. Idempotency-Key обязателен. Клиент не выбирает модель, owner, upstream или system prompt.

Responses:
- 202 — новое задание;
- повтор с тем же key/payload — прежнее задание;
- 409 — key/payload conflict, revision conflict или busy conversation;
- 413 — фактический размер тела превышен;
- 429 с Retry-After — rate или capacity limit, с различимым error code;
- 503 — unavailable/configuration failure, без private details.

Превышение точного context может обнаружиться после cold start: тогда уже принятое задание становится failed с `context_too_long` до inference. Не обещать синхронный HTTP 422 для такого случая.

## 8. Атомарный admission и изменения беседы

Порядок:
1. ограничить фактические bytes тела;
2. проверить auth, ownership и структуру;
3. найти idempotency record;
4. при совпадении вернуть прежнее задание без новых квот;
5. проверить revision, tombstone и active job;
6. проверить очередь и owner waiting limit;
7. проверить token bucket;
8. в одной транзакции списать quota, создать job/message/idempotency, обновить revision и active job;
9. commit, затем уведомить worker.

Ни одной сетевой операции внутри SQLite transaction.

Одновременно в одной беседе допускается только одно незавершённое задание. Переименование меняет metadata/revision и не отменяет работу. Delete создаёт tombstone, отменяет работу и предотвращает позднее сохранение ответа. Завершение проверяет tombstone и active job ID атомарно; удалённая беседа не возрождается.

## 9. Scheduler, лимиты и состояния

Defaults, после подтверждения реального runtime:
- одна активная генерация;
- FIFO waiting capacity 8;
- не более 2 waiting jobs одного владельца;
- token bucket capacity 2, refill 5/min;
- фактическое HTTP body limit 128 KiB;
- title 80 chars, text parsing cap 32000 chars;
- до 200 активных бесед на владельца;
- page size максимум 100;
- queue deadline 180 s;
- bounded model acquire timeout больше runtime startup timeout с небольшим запасом;
- generation timeout 300 s;
- idle model lease release 15 min.

Token bucket допускает burst и ограничивает среднюю частоту. Это не rolling-window «строго 5 запросов за любую минуту». Clock инъецируется в тесты; отрицательное elapsed не даёт refill.

Состояния:
queued → loading_model → preparing_context → running → completed.
Из любого незавершённого состояния допускаются cancelling → cancelled или failed.

Каждая видимая мутация увеличивает state_version. Deadline queued перестаёт отсчитываться после claim worker; cold startup имеет отдельный bounded timeout.

## 10. Бюджет и история

Минимальная целевая actual capacity — 8192 tokens на slot.

Исходные ограничения:
- prompt cap 6144;
- output cap 1024;
- safety reserve 512.

Допустимый prompt — минимум настроенного prompt cap и actual slot capacity минус output reserve и safety reserve.

Всегда сохранять fixed system message и текущий user request. Удалять из prompt старейшие завершённые пары до попадания в точный бюджет. История SQLite не удаляется.

Если system + новый запрос не помещаются, завершить job `context_too_long` до генерации. Snapshot содержит `history_truncated`, использованные/доступные tokens и понятную ошибку.

В context поступает только final assistant content. Provider reasoning хранится отдельно либо не сохраняется и никогда автоматически не добавляется в следующий prompt. finish_reason=length виден пользователю как output limit.

## 11. Lease и отмена

Lease acquire/renew/release выполняются по существующему borrowed-safe API AI-SERVER. Inference и token-count передают lease ID. Не использовать прямой inference без lease и не вызывать unload других моделей.

Один manager gateway удерживает lease для активной работы и ограниченного idle периода. Renew выполняется задолго до expiry и не блокируется генерацией.

Cold acquire — отдельная защищённая task. Отмена клиентского задания не отменяет shared startup future. При позднем успешном acquire manager получает lease и освобождает его в предусмотренном finally/idle/shutdown. Нельзя потерять поздно полученный lease.

Queued cancel не вызывает модель. Running cancel закрывает собственный upstream request, фиксирует terminal cancellation и исключает partial response из истории контекста.

Остановка gateway сначала прекращает admission, отменяет jobs, ждёт bounded pending acquire, затем освобождает принадлежащие ему leases. Опциональный launcher profile получает authenticated graceful stop; Windows force termination допустим только после deadline и только для собственного процесса. Lease expiry остаётся аварийной страховкой, не заменяет обычный finally.

## 12. Потоковый ответ, snapshot и SSE

Текущий runtime и AI-SERVER proxy поддерживают streaming. Gateway использует upstream `stream=true`. `stream_options={"include_usage":true}` включается после подтверждения capability установленного runtime.

Non-streaming допустим только при явно подтверждённом отсутствии streaming capability выбранного backend. Auth errors, timeout, malformed stream и обрыв соединения не включают автоматический non-streaming fallback.

### Upstream parser

httpx читает response потоково. Parser:
- использует incremental UTF-8 decoder;
- корректно обрабатывает символы и SSE-разделители, разбитые между сетевыми chunks;
- поддерживает CRLF/LF, comments и стандартное объединение data lines;
- ограничивает размер отдельного события и суммарного ответа;
- проверяет JSON structure и ожидаемый единственный choice;
- принимает role-only и пустые metadata chunks без изменения текста;
- собирает только разрешённый assistant content;
- не добавляет отдельные reasoning fields в видимый ответ или будущий context;
- принимает usage-only события согласно подтверждённой схеме runtime.

Не удалять произвольные `<think>`-подобные фрагменты из обычного content регулярным выражением. Разделение reasoning/content подтверждается preflight и обработчиком установленного runtime.

Для успешного завершения требуется:
1. допустимый finish_reason (`stop` или `length`);
2. завершающий `[DONE]`;
3. корректный непустой assistant answer;
4. отсутствие protocol/transport errors;
5. отсутствие победившей отмены или удаления беседы.

EOF без `[DONE]`, ранний `[DONE]` без finish_reason, malformed JSON/UTF-8, неизвестный finish_reason и пустой итог дают terminal failure. Получение нескольких content chunks само по себе не означает completed.

`length` сохраняет ответ и показывает output-limit warning. Unsupported tool-call output не становится успешным текстовым ответом.

### Authoritative partial snapshots

Gateway накапливает partial content в job, отдельно от завершённых сообщений. Обновления публикуются bounded coalescing: не чаще одного snapshot за 100 ms, с обязательной отправкой актуального snapshot при смене фазы и terminal transition.

Каждая опубликованная мутация content/state увеличивает state_version. Metadata и heartbeat его не меняют. Сохранение partial snapshot, version и event согласовано транзакционно.

SSE downstream:
- event ID — монотонный seq;
- data — полный authoritative snapshot, включая partial content, state и state_version;
- event types snapshot, reset, terminal;
- bounded retention и heartbeat;
- Last-Event-ID восстанавливает сохранённые события;
- отсутствующий/устаревший cursor получает reset с текущим snapshot;
- auth проверяется при подключении и периодически; revocation закрывает поток;
- число соединений устройства ограничено.

Android заменяет отображаемый partial content содержимым snapshot, а не дописывает snapshot как delta. Старый state_version игнорируется. Reset, reconnect и polling fallback не создают повторов текста.

Polling snapshot остаётся резервом соединения клиента с gateway; он не выключает upstream streaming.

### Завершение и контекст

Только подтверждённый completed answer становится завершённым assistant message. Partial output failed/cancelled задания можно показать с явным статусом, но он никогда не включается в следующий context.

Terminal commit проверяет active job ID, tombstone и cancellation state. После terminal transition late chunks не изменяют данные. Гонка complete/cancel имеет один атомарно выбранный результат.

Running cancel закрывает собственный upstream request и выполняет lifecycle finally. Delete не позволяет позднему ответу возродить беседу.

## 13. Android

Экраны:
- Conversations: список, create, rename, delete confirmation;
- Chat: messages, input, Send, Stop, phases, errors, truncation/length;
- Settings: server URL, masked device token, connection check.

Min SDK 26. JDK 17 bytecode target. Installed JDK 21 допустим при совместимом toolchain. Точные версии Gradle, AGP, Kotlin, compile/target SDK и библиотек фиксируются статическим аудитом в build/lock; никаких latest или dynamic versions.

До POST durable pending record содержит key, payload/hash и conversation revision. После timeout приложение повторяет тот же key либо выполняет lookup; новый key создаётся только для явно нового запроса пользователя.

SQLite cache хранит snapshot и drafts. Токен хранится отдельно AES-GCM с ключом Android Keystore. Backup отключён; токен не попадает в logs, saved state, URI, clipboard automation или APK.

Release HTTPS variant запрещает cleartext. Явный LAN variant разрешает HTTP только валидируемому локальному адресу. URL без userinfo/query/fragment; разрешённая base path проверяется; redirects отключены. Не использовать trust-all certificates.

## 14. Приватность и сеть

Gateway по умолчанию слушает loopback. Network mode включается явно. Local model proxy/control panel остаётся loopback и не публикуется.

Внешний доступ — HTTPS reverse proxy или согласованный защищённый маршрут с действительными credentials и certificate validation. До подтверждения реальной внешней сети C03 не PASS.

Логи содержат request/job IDs, error codes, timing и counters, но не prompts, answers, bearer tokens, upstream key и private paths. Ошибки не возвращают traceback или внутренние provider details.

Private SQLite, pairing tokens, model weights, private configs и личная история не входят в source archive.

## 15. Первый рискованный preflight

До полной реализации приложения:
1. Проверить выбранный named launch profile и действительный полный environment.
2. Выполнить bounded acquire выбранной модели без unload остальных.
3. Получить runtime props и actual slot capacity.
4. Проверить native `/v1/chat/completions/input_tokens` на согласованном chat-completion body.
5. Выполнить один короткий streaming inference с тем же нормализованным body; подтвердить incremental content, допустимый finish_reason и `[DONE]`.
6. Сопоставить input_tokens с фактическим streaming usage.prompt_tokens/prompt evaluation; подтвердить раздельную обработку reasoning/content.
7. Проверить renew и finally release, включая controlled failure.
8. Сохранить model/context/lifecycle evidence.

Fallback apply-template/tokenize разрешается только после отдельного capability proof с тем же сопоставлением. Не заменять модель, не изменять shared profile, не угадывать токены и не выдавать profile JSON за runtime measurement.

Неудача этой границы устраняется до большой реализации. Результаты preflight остаются PARTIAL до завершения полных критериев C01/C07/C12.

## 16. Неопределённости приёмки

- Наличие и маршрут внешнего HTTPS пока не подтверждены.
- Shared model profile сохраняется, пока отдельный non-mutating runtime profile не доказан безопасным.
- Реальная вместимость и совпадение template проверяются implementation preflight.
- Emulator-to-host проверка описывается фактически; её нельзя назвать другой внешней сетью.

## 17. Локальное provisioning и revoke

Административные действия доступны через trusted scenario dispatcher:
- `test.bat scenario device-provision`;
- `test.bat scenario device-revoke`.

Публичного HTTP admin endpoint нет. Сценарии используют внутреннюю административную библиотеку gateway и короткие SQLite transactions. Конкурентная работа gateway сохраняется через штатные WAL/busy timeout/constraints.

Device provisioning:
1. Проверить конфигурацию рабочего data directory и локальные права.
2. Выбрать существующего владельца либо явно создать нового.
3. Сгенерировать случайный device token не менее 256 bits.
4. Сохранить только token hash в devices.
5. Сохранить одноразовый pairing payload в `.runtime/pairing/initial-device.dpapi`, защищённый Windows DPAPI текущего пользователя.
6. Вернуть безопасный статус и device ID; token не выводить в stdout/stderr/logs/evidence.

Pairing directory имеет ограниченный ACL; payload исключён из git и передаваемых материалов. Не перезаписывать существующий payload без явной обработки его статуса. До подтверждённого pairing действует TTL 15 минут: неиспользованное устройство отзывается, payload удаляется. После успешного pairing payload удаляется, действующий device token остаётся только в Android Keystore-protected storage и в виде hash на сервере.

Device revoke принимает идентификатор устройства через административный сценарий, атомарно устанавливает revoked timestamp и удаляет соответствующий pending pairing payload. Не отзывает остальные устройства владельца. Следующий authenticated request отклоняется; SSE закрывается при ближайшей проверке revocation.

Android-emulator scenario вызывает ту же библиотеку provisioning внутренне, создавая принадлежащее сценарию test device. Он не отзывает и не заменяет пользовательские устройства. Созданные IDs фиксируются в private scenario state для точной последующей cleanup.

Токен расшифровывается только внутри harness для pairing. Он не передаётся модели, не включается в tool-call arguments, command previews, публичные JSON или screenshots.

## Test-only instrumentation pairing

Pairing выполняет отдельный androidTest APK с custom android.app.Instrumentation. Test APK подписывается тем же сертификатом, что проверяемый main APK; targetPackage соответствует установленному variant. Совпадение сертификатов проверяется перед запуском.

Main APK содержит только обычный Settings flow. В нём отсутствуют instrumentation runner, test pairing listener и test-only API.

Instrumentation:
1. Открывает test-only LocalServerSocket с уникальным bounded localabstract name.
2. Host harness создаёт `adb forward tcp:0 localabstract:<name>`.
3. Проверяет, что выделенный host listener привязан только к loopback.
4. Принимает соединение и до чтения payload проверяет LocalSocket.getPeerCredentials().uid.
5. Разрешает только фактически подтверждённый adbd UID 0 или 2000. Обычный application UID отклоняется.
6. Читает length-prefixed JSON с ограничением 4096 bytes и bounded deadline.
7. На UI thread открывает настоящие Settings, устанавливает поля и нажимает обычный Save.
8. Ожидает успешную production `/v1/me` проверку и ожидаемое устройство.
9. Возвращает только безопасный success/error code.

Socket name — идентификатор тестовой сессии, а не credential; его можно передать в instrumentation arguments. Device token не передаётся через arguments, Intent extras, clipboard, shell input, plaintext file или logs.

Host helper расшифровывает DPAPI pairing payload внутри процесса и передаёт token только через соединение. Не выводит payload в tool output, exceptions или evidence.

Разрешена одна успешная pairing операция. Число отклонённых соединений и общее время ожидания ограничены. Sockets и собственный adb forward закрываются в finally.

Если SELinux, peer credentials или loopback binding не позволяют проверить границу, pairing завершается ошибкой. Запрещены adb root, отключение SELinux/permissions и fallback на token argv.

Во время pairing не выполняются screenshots, hierarchy dumps и recording. Token EditText не сохраняет plaintext через saved instance state. При ошибке поле очищается и Settings закрывается.

После успешной активации удалить одноразовый DPAPI payload. Завершить instrumentation, удалить только собственный test APK и заново открыть main APK обычным launcher flow. Только затем выполнять screenshots/recording и проверять сохранённое Keystore состояние.

Negative case можно проверить соединением из instrumentation process с фактически подтверждённым обычным application UID: сервер отклоняет его до чтения payload.

Механизм подтверждается стандартными API Instrumentation и LocalSocket и отдельным instrumentation test APK. Документация подтверждает механизм, а не результат E2E:
- https://developer.android.com/reference/android/app/Instrumentation
- https://developer.android.com/reference/android/net/LocalSocket
- https://source.android.com/docs/core/tests/development/instr-app-e2e

## Только явный запуск

Новый gateway запускается исключительно явным вызовом `run_app.bat network`, пользовательского shortcut или соответствующего named launch profile. Старт AI-SERVER/Windows, boot, reconnect, discovery, health checks и восстановление приложения не запускают gateway или выбранную модель.

Не создавать startup hooks, scheduled tasks, autolaunch, автоматический restart или background warm-up.

Модель загружается только для явно отправленного нового запроса либо явно запущенного разрешённого LIVE-сценария. Connection check, polling, SSE reconnect и idempotency replay не инициируют новую генерацию. После перезапуска незавершённые задания завершаются ошибкой без автоматического повторения.

Автозапуск может быть добавлен только по отдельному будущему указанию пользователя.

## Изолированные Android и LIVE integration runs

Добавить два явно запускаемых Center-owned finite profiles:
- private-llm-android-tests → runner integration-child android-emulator;
- private-llm-integration-tests → runner integration-child integration-live.

Команды и сценарии фиксированы в configuration. HTTP-клиент не передаёт executable, произвольные args, data directory или provider credentials.

Полный настоящий AI_TEST_MODEL_* environment получает только Center-owned child и необходимые процессы его собственного subtree. Внешний orchestrator получает run ID, readiness и безопасные результаты.

Каждый child создаёт свежий TemporaryDirectory с отдельными data и pairing directories. APP_PAIRING_DIR поддерживается Settings.from_environment; отсутствие override сохраняет прежний normal default. Integration child принудительно задаёт оба свежих пути и отклоняет обращения за пределы принадлежащего ему fixture root.

Admin provisioning/confirmation/revoke используют существующую библиотеку с явным fixture pairing directory. Обычные service SQLite, устройства, история и pending DPAPI payload не читаются и не изменяются.

Android integration использует e2e applicationIdSuffix с теми же main sources и matching test certificate. Пользовательский package/data не очищается и не заменяется. Instrumentation и LocalServerSocket сохраняют ранее утверждённые ограничения. Отличающийся test application ID явно указан в evidence.

Каждый finite child имеет собственный loopback control endpoint: GET /healthz и authenticated POST /internal/shutdown. Используется существующий Center shutdownUrl mechanism и APP_SHUTDOWN_TOKEN. Stop отменяет suite, прекращает admission, завершает собственный gateway/worker и освобождает leases в finally. Late acquire остаётся защищённым до безопасного release. Чужие profiles/processes/models не завершаются.

Execution watchdog — 1800 s; отдельный cleanup deadline — 330 s. Общий предел child — 2130 s. Existing Center shutdown cap 330 s не повышается. Preflight profile сохраняет прежнее отдельное назначение и deadline.

Timeout, incomplete cleanup и недостаточное количество завершённых запросов дают конкретный неуспешный результат. Старые artifacts не используются как результаты текущего run.

Запуск только явный: никакого boot/startup/reconnect/autorestart.
