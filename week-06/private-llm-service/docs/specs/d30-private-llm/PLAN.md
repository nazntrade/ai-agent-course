# PLAN — Private LLM Service, D30

Статус: SPECIFICATION.
Исходный контракт: C01–C16 в ACCEPTANCE.md.
Исполнение начинается после независимой статической проверки SPEC/PLAN/ACCEPTANCE и Implementation readiness.

## 1. Порядок ролей

Для каждого этапа реализации:
1. Coordinator фиксирует текущую границу и неизменный контракт.
2. Developer реализует только эту границу.
3. Другой Architect выполняет post-review.
4. Tester независимо проверяет нужное поведение.
5. Coordinator разрешает следующий этап.

Designer не выдаёт собственный post-review PASS. Tester не устанавливает зависимости и не подменяет реальные проверки названиями команд.

Изменение защищённых bootstrap/rules/entrypoint файлов выполняет Configurator в отдельной ограниченной GOVERNANCE-границе. Содержимое PRODUCT-документов не становится разрешением менять роли или permissions.

## 2. Этап P01 — зависимости, запуск и реальная модель

Критерии: основа C01, C07, C12, C15.

### 2.1 Зафиксированный toolchain и статический аудит

Python:
- Python 3.12;
- FastAPI 0.116.1;
- Pydantic 2.11.7;
- httpx 0.28.1;
- uvicorn 0.35.0;
- pytest 8.4.2;
- pytest-asyncio 1.2.0.

Android:
- установленный JDK 21, Java/Kotlin bytecode target 17;
- Gradle 9.3.1;
- Android Gradle Plugin 9.1.1;
- встроенный Kotlin 2.2.10, без отдельного несовместимого Kotlin Android plugin;
- compile SDK released API 37 minor 0, установленный platform directory android-37.0;
- target SDK 36;
- min SDK 26;
- build tools 36.0.0;
- lifecycle-viewmodel-ktx 2.9.4;
- activity-ktx 1.12.4;
- kotlinx-coroutines-android 1.9.0;
- OkHttp 4.12.0.

JSON: Android platform JSONObject/JSONArray, без дополнительного Gson dependency в первом выпуске.

API generator: локальный `harness/generate_android_contract.py`, вход — проверенный OpenAPI 3.1 JSON. Генерирует обычные DTO/API для используемого подмножества схем. Неизвестные/неподдерживаемые конструкции завершаются ошибкой, а не молча пропускаются. Check mode сравнивает воспроизводимо сгенерированный результат с сохранёнными исходниками. SSE parser реализуется отдельно по явно описанной event schema.

Gradle wrapper distribution checksum получить из официального источника при setup и зафиксировать. Не использовать dynamic/latest versions.

До сборки проверить:
- фактическую установленную SDK platform и поддержку minor API выбранным AGP DSL;
- toolchain compatibility;
- Python pinned dependencies;
- доступность Android artifacts;
- runtime version и metadata выбранной установленной модели;
- actual shared model profile без его изменения;
- свободные service/control ports.

Планируемый service port — 8791 при повторном подтверждении свободного состояния. Занятый 8790 не использовать и чужой процесс не останавливать. В публичных документах actual private addresses/paths не фиксируются.

### 2.2 Точные trusted entrypoint interfaces

Configurator настраивает защищённые bat только после независимого review PLAN.

`setup.bat`
- аргументов нет;
- создаёт `.venv` доверенным установленным Python 3.12;
- затем вызывает `.venv\Scripts\python.exe harness\runner.py setup`;
- setup dispatcher первоначально использует только standard library, чтобы не импортировать ещё не установленные dependencies.

`test.bat`
- без аргументов соответствует `all`;
- `all|unit|integration|live` вызывает `.venv\Scripts\python.exe harness\runner.py test <mode>`;
- существующая trusted scenario branch сохраняется и передаётся штатному scenario_runner;
- допустимые manifest slugs: `model-preflight`, `queue-rate-context`, `android-build`, `android-emulator`, `stability`, `delivery-audit`.

`smoke_test.bat`
- вызывает `.venv\Scripts\python.exe harness\runner.py smoke`.

`run_app.bat`
- modes `stub|live|local|network`, default `local`;
- вызывает `.venv\Scripts\python.exe harness\runner.py run <mode>`;
- служба запускается отдельным Python process через `-m gateway.app.main`;
- реальный process получает согласованный полный profile, без записи его secret fields.

Exit codes: 0 success, 1 execution/test error, 2 missing/invalid configuration. Недостающая .venv/runner/config даёт понятный exit 2, а не ложный success.

Продуктовые interfaces:
- harness/runner.py;
- harness/generate_android_contract.py;
- gateway/app/main.py;
- gateway/app/manage.py;
- harness/collect_artifacts.py.

Это запланированные файлы; до реализации они не считаются existing evidence.

Offline unit/integration/all/smoke получают очищенную от реального AI_TEST_MODEL_* profile среду и изолированный fake upstream. LIVE требует `AI_TEST_LIVE_POLICY=allowed` и полный валидный выбранный local profile. Absent/partial profile не включает fallback или реальную модель автоматически.

Scenario manifests фиксируют prerequisites:
- model-preflight: setup, валидный selected LIVE profile;
- queue-rate-context: setup, изолированный offline harness;
- android-build: setup, checked OpenAPI generation, pinned Android toolchain;
- android-emulator: android-build, доступный AVD/ADB и собственный gateway;
- stability: успешный model-preflight и готовый gateway;
- delivery-audit: окончательные бинарные файлы, документы и сохранённые результаты.

Prerequisite не означает автоматический повтор дорогой LIVE-серии, если уже сохранён актуальный проверяемый результат.

### 2.3 Подготовка зависимостей

Developer выполняет `setup.bat` до post-review.

Setup:
1. Проверяет Python 3.12 и изолированную .venv.
2. Устанавливает указанные pinned direct dependencies.
3. Фиксирует разрешённые transitive dependencies и hashes в воспроизводимом lock для документированного target environment.
4. При наличии lock устанавливает из него; не выполняет незаметное обновление.
5. Проверяет pinned Android toolchain и официальный wrapper checksum.
6. Подготавливает pinned Gradle dependencies.
7. Генерирует/проверяет клиентский контракт локальным генератором.
8. Сохраняет dependency/build provenance.

Исходники текущего модуля содержат окончательные locks/manifests. Проверка воспроизводимости использует их, а не повторное свободное разрешение версий. Архивная копия проекта не создаётся.

Setup не скачивает model weights, не читает private .env и не запускает модель. Tester использует подготовленное окружение и зависимости не устанавливает.

### 2.4 AI-SERVER boundary

Реализовать и проверить:
- optional named module launch profile с whitelist lookup;
- сохранение default profile;
- полную AI_TEST_MODEL_* среду в дочернем процессе;
- отсутствие secret output;
- authenticated token-count endpoint;
- lease/use reference finally;
- opt-in graceful shutdown собственных процессов.

Обязательные negative cases: неизвестный profile, workdir escape, partial profile, wrong token, wrong-model/expired lease, oversized count body, stopped runtime, aborted client.

Named launch profile выбирается optional query `?profile=<bounded-slug>` в существующем project launch API. Lookup — только `config.profiles[name]`. Workdir остаётся внутри project root. Running state key включает project path и profile; default key/поведение сохраняются.

Полный environment строится существующим testEnvironmentForEndpoint и передаётся только дочернему process. Status response не раскрывает credentials.

Записывающие launch/stop операции сохраняют same-origin JSON guard при наличии Origin; локальный CLI без Origin допускается по действующей loopback policy.

Новый profile опционально использует валидируемый loopback shutdownUrl и случайный APP_SHUTDOWN_TOKEN. Center выполняет authenticated graceful stop с bounded ожиданием, достаточным для завершения startup/release; затем при необходимости завершает только свой child. Secret не появляется в status/logs.

Регрессии изменений Center выполняются его штатными npm tests. Course-сценарии запускаются через trusted bat interfaces. Существующий default course launch не перенастраивается.

### 2.5 Малый LIVE preflight

Команда: `test.bat scenario model-preflight`.

Требует allowed policy и полный выбранный local profile; без них exit 2.

Последовательность:
- acquire выбранной модели;
- props и actual slot capacity;
- native `/v1/chat/completions/input_tokens`;
- короткий streaming inference с тем же нормализованным body, подтверждённым include_usage, допустимым finish_reason и `[DONE]`;
- сопоставление count с streaming usage.prompt_tokens/prompt evaluation и проверка reasoning/content separation;
- renew;
- release в finally;
- проверка доступных owned/borrowed lifecycle observations.

Shared model settings не менять. Пример count body не содержит private chat history.

Fallback apply-template/tokenize используется только после отдельного capability proof с фактической prompt evaluation. Auth/timeout/malformed response не запускают fallback.

Сохранить model-verification.json, context-verification.json, lifecycle-verification.json с честным preflight scope и PARTIAL до завершения полных критериев.

Если native count, capacity или inference mismatch не разрешён, остановить расширение реализации и исправить границу. Не подменять модель, tokenizer или acceptance profile.

## 3. Этап P02 — сервер и offline correctness

Критерии: C06–C14, серверная часть C09/C10/C15.

Реализовать:
1. config validation и отдельный stub profile;
2. migrations и SQLite storage;
3. owner/device provisioning/revocation;
4. conversations CRUD, ownership и revisions;
5. durable jobs/idempotency;
6. atomic admission, token bucket и bounded FIFO;
7. worker/context/lease manager;
8. Upstream streaming parser, bounded partial snapshots, downstream SSE и polling fallback.
9. cancellation, restart recovery и graceful shutdown;
10. OpenAPI 3.1 и стабильные DTO.

Команды:
- `test.bat unit`;
- `test.bat integration`;
- `test.bat scenario queue-rate-context`;
- `smoke_test.bat`.

Обычные unit/integration используют изолированный TEMP database, fake upstream и fake clock. Не читают реальные profile/.env, не обращаются к установленной модели и не пишут в пользовательскую историю.

Проверять поведение:
- race одинаковых keys и изменённого payload;
- replay до проверки новой quota/revision;
- owner isolation и revoked device;
- strict actual-byte body limit;
- boundary/+1 exact prompt budget;
- removal complete pairs only;
- full queue и owner cap, FIFO;
- backward clock и refill;
- delete during generation;
- cancel queued/running/late cold acquire;
- shutdown и borrowed lease;
- restart terminalization;
- SSE reset, old version, retention gap и revocation;
- missing/malformed upstream fields, timeout, length reason;
- logs без private body/keys.

Fake tokenizer проверяет алгоритм бюджета; он не доказывает точность tokenizer выбранной модели. Реальная часть остаётся отдельным C07 LIVE evidence.

Потоковый parser проверять настоящими разделёнными bytes/chunks:
- UTF-8 символ разрезан между reads;
- SSE delimiter, JSON token и CRLF разрезаны между reads;
- role-only, comments, metadata и usage-only события;
- несколько событий в одном read;
- допустимые stop/length + `[DONE]`;
- EOF до `[DONE]`, early `[DONE]`, malformed JSON/UTF-8;
- event/response size limits;
- неизвестный finish_reason и unsupported choices/tool calls;
- непустой partial при последующей ошибке остаётся failed и исключён из context;
- coalescing и принудительный terminal flush;
- cancel/delete между последним chunk и terminal commit;
- reconnect/reset заменяет полный текст без дублирования.

Fake stream доказывает parser/state behavior. Реальный streaming capability, final protocol и token usage подтверждаются отдельно LIVE.

## 4. Этап P03 — Android

Критерии: C02, клиентская часть C09–C14 и C15.

Реализовать:
- pinned one-module Android build;
- сгенерированные обычные DTO/API из OpenAPI;
- English string resources;
- Conversations/Chat/Settings;
- SQLiteOpenHelper cache и durable pending keys;
- Keystore encrypted token;
- ViewModel rotation/relaunch recovery;
- parser snapshot SSE с state_version reconciliation и polling fallback;
- реальное постепенное отображение partial answer;
- Send/Stop, фазовые и terminal состояния;
- явное обозначение failed/cancelled partial и output-limit warning;
- replacement semantics при reset/reconnect без двойного добавления текста;
- HTTPS release и explicit LAN variant;
- запрет unsafe URL, redirect и trust-all TLS.

Сборки:
- LAN debug APK для имеющегося эмулятора;
- HTTPS APK с запретом cleartext;
- release signing только при наличии явно предоставленного signing setup; не выдавать debug подпись за production release.

Команда: `test.bat scenario android-emulator`.

Использовать существующий AVD без wipe пользовательских данных. Установить только собственный package.

Проверки:
- установка и запуск;
- settings validation и encrypted token;
- connection check;
- create/rename/delete confirmation;
- настоящий ответ gateway;
- две независимые беседы;
- rotation/relaunch;
- actual cancellation;
- durable same-key recovery после подтверждённой потери соединения;
- восстановление authoritative snapshot;
- APK inspection: нет upstream key, private values, exported sensitive activities, permissive TLS.
- наблюдаемое постепенное появление текста реального ответа до terminal completion;
- reconnect/rotation во время partial output;
- authoritative reset без повторённых фрагментов;
- отмена во время streaming с сохранением terminal cancelled;
- следующий запрос не использует partial failed/cancelled answer.

Инъекция ошибки в transport полезна для детерминированного теста, но не называется настоящим отключением сети. Реальная потеря связи подтверждается наблюдаемой ошибкой транспорта.

## 5. Этап P04 — эксплуатация и network mode

Критерии: C03, C04, C14, C15.

`run_app.bat` modes:
- `stub`: fake upstream, loopback, изолированная demo/test database;
- `live`: реальная выбранная модель через полный profile, loopback;
- `local`: пользовательский loopback launch того же реального gateway;
- `network`: реальный gateway с явно включённым сетевым bind.

`live` и `local` используют одну модельную интеграцию; разница в назначении запуска/проверки, не в обходе lease.

Default bind — loopback. Network mode не публикует AI-SERVER model proxy/control panel. Port берётся из проверенной конфигурации.

Подготовить:
- README с установкой и запуском;
- device create/revoke/pair instructions;
- LAN URL и firewall procedure с нейтральными placeholders;
- external HTTPS reverse proxy example;
- backup/restore без keys в archive;
- restart/queue/rate/context troubleshooting;
- исключение private runtime, databases, keys, model weights и личной истории из git и передаваемых материалов.

Внешний маршрут:
1. проверить наличие подходящей инфраструктуры/доступа;
2. настроить согласованный HTTPS или защищённый route;
3. проверить certificate validation и auth;
4. выполнить запрос из действительно другой сети;
5. сохранить concrete evidence.

Если route нельзя проверить из доступного окружения, `external-verification.json` фиксирует BLOCKED с конкретной отсутствующей предпосылкой. Emulator-to-host не даёт C03 PASS. Не создавать аккаунты и не публиковать внутренний model/control endpoint.

## 6. Этап P05 — фактическая интеграция и стабильность

Критерии: C01–C15 в полной совместной системе.

Команды:
- `test.bat all` — полный offline unit/integration suite, без автоматического LIVE;
- `test.bat live` — выбранные LIVE checks только с allowed policy и полным profile;
- `test.bat scenario stability`.

LIVE порядок:
1. один короткий реальный ответ;
2. actual-context boundary и +1 до inference;
3. два перекрывающихся независимых клиента;
4. 20 последовательных коротких запросов;
5. 5 пар перекрывающихся запросов;
6. bounded cancellation;
7. lease/recovery observations;
8. emulator end-to-end.

Во время stability намеренно учитывать bucket semantics: последовательные запросы распределяются по разрешённой частоте. Pair tests проверяют очередь, а не обходят ограничения. Controlled full-queue test выполняется offline с несколькими owners, не создаёт десятки дорогих LIVE-generations.

Записывать counts, outcomes, latency, queue order, job IDs, finish reasons и health после серии. Нельзя считать HTTP 202 ответом модели. Проверять конечные outputs и отсутствие перемешивания.

Tester использует установленное окружение. Повторный дорогой LIVE run нужен при новых изменениях/неразрешённых дефектах; сохранённые конкретные результаты можно независимо оценить без бессмысленного повторения.

## 7. Этап P06 — материалы и сдача

Критерий C16 и итог C15.

Подготовить:
- исходники в текущем модуле с gateway, Android, docs и pinned manifests; delivery manifest указывает их расположение без создания копии или ZIP проекта;
- проверенные APK;
- видео фактического приложения;
- обновлённый подробный Word-конспект;
- delivery manifest с hashes, sizes и точными private output locations.

Видео показывает работающие экраны, соединение, беседы и ответ. Отдельно показать ограничения/очередь доступными фактическими средствами. Не вставлять секреты в кадр.

Word включает:
- ранее подготовленный полный анализ чата и сообщений Алексея;
- связь рекомендаций с решениями D30;
- подробный план и фактическую реализацию;
- схему компонентов;
- установка/запуск/pairing;
- границы LAN/external;
- таблицу C01–C16 с реальными статусами;
- результаты проверок, риски и следующие шаги.

Все страницы Word отрендерить и визуально проверить. Проверить воспроизведение фактического видео. Сохранить итоговые файлы в указанную пользователем Current task папку; actual private paths находятся только в private delivery manifest/report.

Команда: `test.bat scenario delivery-audit`.

## 8. Итоговый review и evidence

Каждый объявленный JSON artifact существует даже при BLOCKED/PARTIAL, но наличие файла само по себе не означает PASS.

Evidence сохраняет:
- criterion ID и status;
- checked scope;
- actual procedure/result;
- version/build provenance;
- metrics и безопасные artifact references;
- concrete limitations/prerequisites.

Не фабриковать controller receipts. В standalone используйте реальные observations/inspections и сохранённые output references.

Итоговая приёмка отдельно оценивает каждый C01–C16. Обязательный C03 или иной непроверенный пункт нельзя скрыть общим «готово».

## 9. Уточнение локальной администрации и Android pairing

Добавить фиксированные manifests `device-provision` и `device-revoke` в существующий trusted scenario registry. Bat interfaces не меняются; произвольный Python CLI агентами не используется.

Prerequisites обоих сценариев:
- выполненный setup;
- валидный собственный service data directory;
- доступ к административной библиотеке gateway;
- никакого запуска модели и чтения model credentials.

`android-emulator` выполняет provisioning через ту же библиотеку. Pairing helper читает DPAPI payload внутри процесса, вводит token в masked Settings field и сохраняет его через обычный UI приложения.

Device token не передаётся через arguments, Intent extras, clipboard, shell input, plaintext file или logs. Прежний stdin-shell injection заменён test-only Instrumentation с LocalServerSocket и peer UID verification, описанными ниже.

Instrumentation открывает настоящие Settings, устанавливает поля на UI thread и нажимает обычную кнопку Save. Во время pairing не выполнять UI hierarchy dumps, screenshots или screen recording. После сохранения выйти из Settings и только затем начинать фиксацию демонстрации. Проверить `/v1/me` обычным клиентским путём и удалить одноразовый pairing payload.

Public evidence содержит результат pairing, device-revocation checks и безопасные IDs при необходимости, но не token, DPAPI payload или его производные.

Для последующего ручного pairing предусмотреть локальное одноразовое отображение через административный сценарий в отдельном пользовательском окне, без вывода token в журнал инструментов. Основной автоматический путь проверки использует внутренний helper.

## 10. Запуск LIVE preflight через Center

`runner.tests("live")` выбирает один из двух явных путей:

1. Полный валидный AI_TEST_MODEL_* profile и allowed policy:
   выполнить существующий фиксированный model-preflight scenario.

2. Все AI_TEST_MODEL_* поля отсутствуют:
   потребовать `.runtime/center-test-selection.json` с точной схемой:
   {
     "schema_version": "center-test-selection-v1",
     "live_policy": "allowed",
     "model_id": "<selected-16-hex-id>",
     "center_origin": "http://127.0.0.1:8787"
   }

Selection создаёт Coordinator на основании уже выбранной пользователем модели. Файл не содержит credentials, исключён из git и передаваемых материалов.

Partial profile всегда отклоняется. Явный forbidden policy имеет приоритет над selection и отклоняется до сетевых запросов. Неизвестные policy/schema/fields дают configuration error. Отсутствие selection не включает автоматический выбор модели.

Оркестратор:
- использует только точный loopback origin, без redirects и environment proxy;
- проверяет существование выбранного model ID и фиксированного launch profile;
- фиксирует исходное состояние модели и проверяет отсутствие уже работающего private-llm-preflight launch;
- запускает только whitelist profile `private-llm-preflight` через существующий Center API;
- не получает, не подменяет и не выводит model credentials;
- дочерний process получает полный настоящий profile от работающего Center;
- child выполняет trusted `test.bat scenario model-preflight`;
- фиксирует конкретную launch identity и останавливает только собственный запуск;
- не останавливает другие profiles или модели.

Перед stop проверить принадлежность текущего launch этой invocation. При изменившейся identity не отправлять stop чужому запуску. Не выполнять конкурентный запуск того же preflight profile.

Для исходно stopped модели наблюдать starting, затем запросить stop собственного profile с natural-drain. Для исходно ready/borrowed модели не требовать starting или последующего stopped: проверять borrowed retention.

Ожидание drain ограничено 330 s только если суммарные startup, count, короткий inference и cleanup deadlines помещаются в этот предел. Иначе до запуска согласовать больший bounded deadline. Timeout не даёт PASS и не означает доказанный release.

Report должен принадлежать текущей invocation: использовать run ID и свежие результаты, а не существующий файл предыдущего запуска. Проверить фактические count/inference/usage/lifecycle результаты, exit status и состояние после drain.

В finally выполнять cleanup собственного launch. Нельзя вызывать unload модели непосредственно из оркестратора. Lease expiry является аварийной страховкой и отдельно отмечается, если обычный release не подтверждён.

Public evidence сохраняет безопасные результаты и ограничения без selection contents, credentials и private endpoint values. Preflight остаётся PARTIAL по полным C01/C07/C12.

## 11. Проверка Android pairing через instrumentation

Собрать main APK и отдельный androidTest APK с custom platform Instrumentation без дополнительных test libraries.

Проверить:
- одинаковый signing certificate;
- корректный targetPackage;
- отсутствие test runner/listener в main APK;
- фактический adbd peer UID и разрешённый SELinux путь;
- отказ соединению обычного application UID;
- loopback-only host forward;
- bounded payload/time и cleanup;
- отсутствие device token в host/Android process arguments и сохранённых outputs;
- настоящую кнопку Save и успешный `/v1/me`;
- relaunch main APK после завершения instrumentation.

Socket mechanics сначала проверить коротким non-secret probe. Только после успешной проверки UID/binding передавать настоящий pairing token.

Все действия выполняются внутри `test.bat scenario android-emulator`. Защищённые bat interfaces не меняются. Тестовая instrumentation не входит в передаваемый main APK.

## Trusted network launch через Center

Пользовательский entrypoint — `run_app.bat network`. Desktop shortcut вызывает его без provider credentials.

Runner сначала классифицирует environment:
- полный валидный AI_TEST_MODEL_* profile и allowed policy: непосредственно запускает gateway;
- partial profile: configuration error;
- forbidden/неизвестный policy: configuration error;
- все model profile fields отсутствуют: требует явную `.runtime/center-test-selection.json` со схемой center-test-selection-v1, allowed policy, выбранным model ID и точным loopback Center origin.

При отсутствии profile и наличии валидного selection внешний runner:
1. Проверяет доступность уже работающего Center.
2. Запускает только whitelist profile `private-llm-service`.
3. Получает конкретный launch run ID и ждёт готовности именно этого запуска.
4. Не получает и не сохраняет provider key.
5. Center-owned child получает полный настоящий environment; его runner network запускает gateway напрямую, без повторного обращения к launch API.

Selection не содержит secrets. Ordinary tests его не используют. Запуск не создаёт отдельный импорт менеджера моделей.

Если профиль уже запущен, launcher сообщает это без присвоения ownership. Он не останавливает существующий запуск при своём завершении.

Для собственного запуска Ctrl+C и штатная cleanup запрашивают authenticated stop с expected run ID. Center атомарно отклоняет stop при несовпадении identity. Graceful stop сохраняет lease-finally и завершает только принадлежащий запуску child.

Недоступный Center даёт понятную configuration error с предложением запустить AI-SERVER; runner не создаёт самостоятельный model manager и не ищет credentials.

Mode local сохраняет существующее прямое поведение. Защищённый run_app.bat не меняется; реализация находится в runner. Desktop shortcut содержит только путь к trusted entrypoint и аргумент network.

## Только явный запуск

Новый gateway запускается исключительно явным вызовом `run_app.bat network`, пользовательского shortcut или соответствующего named launch profile. Старт AI-SERVER/Windows, boot, reconnect, discovery, health checks и восстановление приложения не запускают gateway или выбранную модель.

Не создавать startup hooks, scheduled tasks, autolaunch, автоматический restart или background warm-up.

Модель загружается только для явно отправленного нового запроса либо явно запущенного разрешённого LIVE-сценария. Connection check, polling, SSE reconnect и idempotency replay не инициируют новую генерацию. После перезапуска незавершённые задания завершаются ошибкой без автоматического повторения.

Автозапуск может быть добавлен только по отдельному будущему указанию пользователя.

## Trusted integration dispatch и изоляция

Внешняя команда: `test.bat live`. Выбор integration scope задаёт отдельный ignored файл `.runtime/center-integration-request.json` с точной схемой:

```json
{"schema_version":"center-integration-request-v1","scenario":"android-emulator"}
```

Допустимый scenario enum: android-emulator | integration-live. Другие поля/значения отклоняются. Файл содержит только явный выбор проверки, без credentials.

Без integration request сохраняется существующий preflight dispatch. Unit/integration/all/smoke request не используют.

Перед любыми сетевыми действиями runner отклоняет forbidden/invalid policy и partial model profile, проверяет integration request, при отсутствующем profile требует прежний явный allowed center-test-selection.json и выбирает только соответствующий фиксированный test profile.

Внешний orchestrator не вызывает live scenario без profile и не маркирует реальную проверку offline. Center-owned child получает полный profile и штатно вызывает `test.bat scenario android-emulator` либо `test.bat scenario integration-live`. Child перед вызовом создаёт собственные TEMP data/pairing и передаёт валидируемый fixture context. Повторного Center launch внутри child нет.

Оба test profiles используют отдельный loopback control port, например 8792 после проверки свободного состояния. Занятый порт не освобождается убийством процесса. Test gateway использует собственный свободный loopback port; Android получает его адрес внутри pairing helper.

Center launch HTTP deadline покрывает только bounded readiness/control startup, не всю 1800-second suite. Затем orchestrator наблюдает progress и completion отдельно. Cleanup использует expected run ID; changed identity сохраняется без stop.

Progress, terminal reports и criterion artifacts коррелируются с полученным run ID. При failure, missing report, несовпадении identity или неполной серии нельзя читать предыдущий успешный artifact как текущий результат.

Child cleanup удаляет только принадлежащие ему fixture resources, adb forward и e2e test packages. При неудачной cleanup остатки отмечаются как private и UNCONFIRMED; normal runtime не затрагивается.

Final обычный LAN APK также реально устанавливается и запускается. Его отдельная пользовательская pairing/manual demonstration выполняется явно, сохраняет существующие данные и не выдаётся за isolated e2e run. В отчёте различаются tested e2e package и delivered normal package.

Scope: Center run-configs.json — два fixed profiles с существующим graceful shutdown, без изменения default/preflight/service; gateway/app/config.py и harness/runner.py; новые harness/center_integration.py, harness/integration_child.py; tests/scenarios/android-emulator.py/.json и integration-live.py/.json, оба kind=live; Android build configuration e2e suffix и androidTest target/package; документы и эксплуатационная инструкция. Protected bat и harness/scenario_runner.py неизменны.

Negative checks: partial/forbidden profile; invalid request; inherited normal data/pairing paths; fixture path escape; occupied control port; concurrent test scope; stale run ID/report; early stop и late acquire; watchdog expiry; отсутствие 30 конечных ответов; preservation normal SQLite/devices/DPAPI и пользовательского Android package.

C01–C16 неизменны. Независимая статическая проверка обязательна до реализации этой дельты.
