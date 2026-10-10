# ACCEPTANCE — Private LLM Service, D30

Статус: SPECIFICATION.
Контракт ниже заморожен. Требования не ослабляются из-за состояния окружения.

## 1. Правила приёмки

- PASS подтверждает требование целиком фактическими результатами.
- PARTIAL означает выполненную часть с явной оставшейся проверкой.
- BLOCKED означает конкретную недоступную предпосылку.
- FAIL означает наблюдаемое нарушение.
- NOT_ASSESSED означает отсутствие проверки.
- Наличие endpoint, файла, команды или успешной сборки не заменяет проверку поведения.
- Все результаты сохраняются в объявленные artifacts.
- JSON-артефакты нейтральные: без keys, private paths/IP, private prompts, истории и model weights.
- Для бинарных материалов проверять реальное содержимое; JSON manifest содержит hashes/sizes/logical names и отсылает к проверенному результату.
- Реальные private locations и исходная частная модельная привязка находятся в исключённом из публикации delivery report.
- При отсутствии orchestration tools не выдумывать tool receipts. Использовать saved artifacts, inspections и observations.
- Preflight artifacts помечаются PARTIAL до завершения полного критерия.
- Emulator-to-host не подтверждает внешнюю сеть C03.
- Автор design не выполняет собственный независимый post-review.

## 2. Замороженный исходный контракт

```acceptance-contract
{"schema_version":"acceptance-contract-v2","criteria":[{"id":"C01","required":true,"owner":"agent","requirement":"Выбранная локальная Gemma через AI-SERVER реально загружается и отвечает; фактический context runtime и lifecycle подтверждены.","source":"User model; D30 service","check":"LIVE: bounded selected-model inference and effective context, acquire/renew/release with borrowed safety","artifacts":[{"id":"evidence-model","glob":"docs/artifacts/model-verification.json","format":"json","count":{"min":1,"max":1},"assertions":[]}]},{"id":"C02","required":true,"owner":"agent","requirement":"Лёгкий Android APK устанавливается на имеющийся эмулятор, через сеть обращается к домашнему сервису и позволяет создавать, переименовывать и удалять беседы.","source":"User APK CRUD LAN; user amendment installed emulator","check":"UI/E2E: emulator installation, actual controls, host-network chat response","artifacts":[{"id":"evidence-android","glob":"docs/artifacts/android-verification.json","format":"json","count":{"min":1,"max":1},"assertions":[]}]},{"id":"C03","required":true,"owner":"agent","requirement":"Предусмотрен защищённый доступ к приватному чату из другой сети; реальная проверка внешнего маршрута требуется отдельно от loopback эмулятора.","source":"Alex external IP/client-server guidance; D30 network access","check":"LIVE: HTTPS via genuinely external network, valid auth and certificate; report concrete unavailable prerequisite honestly","artifacts":[{"id":"evidence-external","glob":"docs/artifacts/external-verification.json","format":"json","count":{"min":1,"max":1},"assertions":[]}]},{"id":"C04","required":true,"owner":"agent","requirement":"Два одновременных клиента получают ответы без падения, потерянных сообщений и перемешивания бесед; очередь допустима.","source":"Alex at least two connections; D30 multiple requests","check":"LIVE: overlapping independent client jobs and complete outputs inspected","artifacts":[{"id":"evidence-concurrency","glob":"docs/artifacts/concurrency-verification.json","format":"json","count":{"min":1,"max":1},"assertions":[]}]},{"id":"C05","required":true,"owner":"agent","requirement":"Стабильность подтверждена 20 последовательными запросами и 5 парами перекрывающихся запросов к выбранной модели.","source":"Detailed reviewed user plan; D30 stability","check":"LIVE: bounded small-output run measuring outcomes, latency and service survival","artifacts":[{"id":"evidence-stability","glob":"docs/artifacts/stability-verification.json","format":"json","count":{"min":1,"max":1},"assertions":[]}]},{"id":"C06","required":true,"owner":"agent","requirement":"Ограничение частоты действует по владельцу с понятным 429/Retry-After и безопасным повтором; token bucket semantics описаны точно.","source":"D30 rate limit; plan","check":"INT: deterministic clock boundary/burst/refill and authenticated replay; LIVE bounded observation","artifacts":[{"id":"evidence-rate","glob":"docs/artifacts/rate-verification.json","format":"json","count":{"min":1,"max":1},"assertions":[]}]},{"id":"C07","required":true,"owner":"agent","requirement":"Контекст ограничивается точным токенизатором с реальным chat template и резервом ответа; обрезаются только старые завершённые пары, слишком большой новый запрос отклоняется до генерации.","source":"D30 max context; plan","check":"INT exact-budget boundary/+1, LIVE actual template/tokenizer and effective slot capacity","artifacts":[{"id":"evidence-context","glob":"docs/artifacts/context-verification.json","format":"json","count":{"min":1,"max":1},"assertions":[]}]},{"id":"C08","required":true,"owner":"agent","requirement":"Одна активная генерация, ограниченная FIFO очередь и лимит владельца работают атомарно; переполнение возвращает понятную ошибку.","source":"D30 stability; plan","check":"INT controlled upstream queue/full/race; LIVE real overlapping jobs","artifacts":[{"id":"evidence-queue","glob":"docs/artifacts/queue-verification.json","format":"json","count":{"min":1,"max":1},"assertions":[]}]},{"id":"C09","required":true,"owner":"agent","requirement":"Серверная история SQLite и Android локальное состояние сохраняются после перезапуска; переименование и удаление не возрождают данные.","source":"User conversations; plan","check":"INT persistence/revisions/delete race and UI emulator relaunch","artifacts":[{"id":"evidence-persistence","glob":"docs/artifacts/persistence-verification.json","format":"json","count":{"min":1,"max":1},"assertions":[]}]},{"id":"C10","required":true,"owner":"agent","requirement":"Повтор после обрыва сети с тем же idempotency key возвращает прежнее задание; изменённое содержимое с тем же key даёт 409, сообщения не дублируются.","source":"Plan reliable mobile chat","check":"INT concurrent replay/crash recovery; UI or E2E network interruption and recovery","artifacts":[{"id":"evidence-replay","glob":"docs/artifacts/replay-verification.json","format":"json","count":{"min":1,"max":1},"assertions":[]}]},{"id":"C11","required":true,"owner":"agent","requirement":"Токены устройств отзываются, хранятся защищённо и разделяют владельцев; нет доступа к чужим беседам/заданиям и утечки серверного ключа в APK.","source":"Private service; plan","check":"INT auth/revocation/ownership; APK/config/log inspection; Android Keystore implementation","artifacts":[{"id":"evidence-auth","glob":"docs/artifacts/auth-verification.json","format":"json","count":{"min":1,"max":1},"assertions":[]}]},{"id":"C12","required":true,"owner":"agent","requirement":"Холодный старт, ошибка модели, перезапуск сервиса и остановка безопасно завершают задания и lease; чужая модель не выключается.","source":"Private model service; Course lifecycle rules","check":"INT delayed acquire/cancellation/shutdown and borrowed leases; LIVE selected runtime lifecycle","artifacts":[{"id":"evidence-lifecycle","glob":"docs/artifacts/lifecycle-verification.json","format":"json","count":{"min":1,"max":1},"assertions":[]}]},{"id":"C13","required":true,"owner":"agent","requirement":"Отмена queued/running запроса работает; частичный/failed ответ не попадает в следующий контекст, ограниченный output отмечается.","source":"Plan usable lightweight chat","check":"INT cancellation races/length; UI actual cancel; LIVE bounded cancellation where possible","artifacts":[{"id":"evidence-cancel","glob":"docs/artifacts/cancel-verification.json","format":"json","count":{"min":1,"max":1},"assertions":[]}]},{"id":"C14","required":true,"owner":"agent","requirement":"HTTP API валидирует размер и параметры, не пишет содержимое/ключи в логи; внешний HTTPS и разрешённый LAN HTTP имеют явные границы.","source":"Private service; D30 HTTP API","check":"INT actual byte cap, unsafe URL/redirect and log tests; static packaging security; LIVE health/chat","artifacts":[{"id":"evidence-privacy","glob":"docs/artifacts/privacy-verification.json","format":"json","count":{"min":1,"max":1},"assertions":[]}]},{"id":"C15","required":true,"owner":"agent","requirement":"Исходники сервиса и APK, воспроизводимые зависимости, trusted setup/test/run, инструкции и измеренный build manifest готовы.","source":"D30 Code; user do all","check":"Build APK; independent offline regression and actual trusted startup; read instructions and hash produced files","artifacts":[{"id":"evidence-build","glob":"docs/artifacts/build-verification.json","format":"json","count":{"min":1,"max":1},"assertions":[]}]},{"id":"C16","required":true,"owner":"agent","requirement":"Подготовлены видео демонстрации и обновлённый подробный Word конспект с планом, решениями и фактическими результатами в Current task; мысли о новом задании продублированы пользователю.","source":"D30 Video+Code; User Word destination and detail","check":"Inspect actual video and rendered Word all pages; saved delivery manifest verifies hashes/paths, no fabricated external acceptance","artifacts":[{"id":"evidence-delivery","glob":"docs/artifacts/delivery-verification.json","format":"json","count":{"min":1,"max":1},"assertions":[]}]}]}
```

## 3. Дополнительные конкретные проверки

### C01 / C07 / C12

Подтверждать фактическую выбранную модель, version/runtime provenance и actual slot capacity. Exact count сверяется с одинаковым prompt/template пути inference. Проверить границу и +1. В borrowed case модель остаётся работающей после release клиента; owned case не удерживается после безопасного завершения без иной retention.

Не считать profile JSON доказательством actual capacity. Не заменять release наблюдением, что процесс однажды завершился.

### C02 / C09 / C10 / C13

Проверить реальные Android controls и данные после relaunch. После потери соединения persisted key используется повторно; количество server jobs/messages остаётся прежним. Вторая попытка с изменённым payload даёт 409.

Отмена видна в UI и на сервере. Failed/cancelled/partial answer не входит в следующий context. После delete поздний ответ не возрождает беседу.

### C04 / C05 / C08

Два клиента должны иметь перекрывающийся период незавершённых заданий. Принятие 202 само по себе не является ответом модели. Проверить terminal content, attribution по job/conversation, отсутствие потерянных сообщений и живой сервис после серии.

Статистику stability собирать по 20 последовательным запросам и 5 парам. Rate limiter не отключается ради такого run.

### C06 / C11 / C14

Детерминированные clock tests подтверждают burst/refill/Retry-After. Повтор существующего key не списывает новую quota. Проверить фактические bytes, не только Content-Length.

Проверить owner isolation, revoked tokens, secret-free APK и logs. Для HTTPS сохраняется стандартная certificate validation; LAN HTTP включается явно.

### C15 / C16

Сборка проверяется реально, зависимости pinned, commands воспроизводимы. Проверить исходники текущего модуля и передаваемые файлы: они не раскрывают databases, keys, model weights и частную переписку. Архивная копия проекта не создаётся.

Видео воспроизводится и показывает фактическую работу. Word проверяется после render всех страниц. Delivery manifest сверяет hashes и sizes окончательных файлов. Непроверенный внешний доступ обозначен явно.

### Наследуемое требование streaming

При поддерживающем runtime проверить фактический потоковый вывод:
- upstream streaming, корректный incremental parser и terminal protocol;
- видимые partial snapshots в Android до completed;
- reset/reconnect без повторённого текста;
- cancellation и ошибки не превращают partial в завершённый context;
- exact native count соответствует фактическому streaming prompt usage.

Статический флаг stream=true не доказывает работающий поток. Non-streaming допускается только при сохранённом доказательстве отсутствия capability, а не как fallback после ошибки.

## 4. Независимый итог

Tester выдаёт acceptance-evidence-v2 по каждому C01–C16 и сопровождает его коротким человеческим выводом. Каждый статус имеет конкретные saved artifact references, inspections или observations.

Структура output и корректный JSON не дают общий PASS. Обязательный невыполненный критерий сохраняет свой реальный статус.
