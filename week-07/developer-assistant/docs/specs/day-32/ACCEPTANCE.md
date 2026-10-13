# D32 — Критерии независимой приёмки

ACCEPTANCE_STATUS: NOT_ASSESSED

Все критерии обязательны. PASS требует фактического доказательства заявленного метода. READY автора, выполненный HTTP запрос, citation object, общее число unit tests и static code review не заменяют LIVE/BROWSER/CI или содержательную оценку. Failures/partial/NOT_ASSESSED остаются открытыми до реальной проверки.

| ID | Источник и требование | Достаточная независимая проверка |
|---|---|---|
| D32-01 | Задание: автоматический pipeline на новый PR | CI: настоящий учебный PR автоматически вызывает GitHub Actions pull_request_target/opened; новый commit автоматически вызывает pull_request_target/synchronize, поддерживается reopened. Сохранены URL run, event/action, PR и base/head SHA, времена и проверенный итог. Ручной CLI не заменяет это. |
| D32-02 | Задание: получает diff и изменённые файлы | CI/LIVE collector manifest совпадает с настоящим PR: пути, status, hunks, additions/deletions и fixed base/head. Offline пагинация, rename/deletion, missing patch и oversized/binary не пропускаются молча. |
| D32-03 | Задание: RAG документация + код | У известного PR inspect sources/prompt показывает retrieved README/docs/API и related code/test, relative paths/lines/hashes/SHA. Сам diff не выдаётся за retrieved corpus. Independent substantive finding опирается на реально поддерживающий source. |
| D32-04 | Задание: текст ревью с багами, архитектурой и рекомендациями | Полный опубликованный Markdown имеет все три раздела; содержательная независимая оценка known bug и cross-file/docs contract case подтверждает ожидаемые реальные проблемы и полезные рекомендации. Пустая категория явно допустима, выдуманные находки не обязательны. |
| D32-05 | Владелец: реальные DeepSeek и Local | LIVE одинаковые known-bug и benign изменения на явно выбранных DeepSeek и Local через один engine. Сохранены provider/model route, SHA/sources, полный санитизированный ответ, elapsed/usage и независимое сравнение с ground truth. Никакой скрытой замены provider. |
| D32-06 | Честность, structured evidence и quality | Offline malformed JSON/reference/path/line, truncated stream, insufficient context, conflicting docs, injection и unavailable provider; status не становится clean/PASS. Quality отдельно NOT_ASSESSED до независимой оценки. Golden benign controls проверены на ложные обвинения. |
| D32-07 | Реальные GitHub результаты | CI artifact review.json/review.md и job summary доступны; PR comment с marker/head соответствует валидированному review. Обновление того же PR не плодит комментарии. Публикация denial ясно отражена, а не «отправлено». |
| D32-08 | Fixed scope и partial coverage | Offline два repos/две revisions не смешивают corpus/results; excluded secret/binary/oversized и missing hunks вызывают явные limitations/PARTIAL. UI/report показывает покрытие и реально рассмотренные файлы. Из .env/секретов ничего не считывается. |
| D32-09 | Trusted CI и безопасная конфигурация | Static + CI проверяют pull_request_target workflow из base и exact event base SHA checkout движка/dependencies; malicious head workflow/requirements/review engine не исполняются. PR JSON fields не интерполируются в shell. Минимальные contents:read/pull-requests:write; fork safe skip до secret-dependent шагов. Ключи отсутствуют в logs/artifacts/comments/prompt; секреты конфигурирует разрешённый application-owned процесс. Никакого публичного домашнего runner. |
| D32-10 | Bounded execution, cancellation, stale head | Offline остановка in-flight request, late-result guard, retry bounds, restart recovery; CI новый head cancels obsolete run/не публикует его как current. Stale report импортируется только как stale history. Permission/format failure не зацикливается. |
| D32-11 | Владелец: текущий chat UI умеет PR review | BROWSER running app: выбрать репозиторий, список PR, Sync, проверить выбранной моделью, открыть findings/diff/sources, ожидание и Stop. CI/manual origin и SHA видны; кнопки/API реально работают, HTML чтение недостаточно. |
| D32-12 | Сохранить D31 | Offline affected suite + BROWSER/LIVE representative /help docs citation/current MCP branch, chat create/rename/archive/history, provider selection/Stop, существующие workspace/schedule/tools/search/report операции. Новая функция не повреждает прежнюю БД и docs-only index. |
| D32-13 | Владелец: уведомления Telegram и финальное уведомление | LIVE saved real review → fixed owner notification получает Telegram success/message ID. Повтор/poll/import не дублируют отправку; error/uncertainty сохранены. После полной приёмки основной исполнитель отправляет одно правдивое completion notification; без подмены адресата/ключей. |
| D32-14 | Владелец: подробный Word учебник | Готовый .docx в пользовательском output directory; реальная реализация и скриншоты, PR/MR, Actions/GitHub, secrets без значений, RAG docs/code, Local/DeepSeek dataflow, reports/import/Telegram, связь с weekly notes. Все rendered pages просмотрены, объяснение без необъяснённых скачков. |
| D32-15 | Задание+владелец: код и подробное видео | Код/README reproducible; настоящий landscape MP4 1920x1080 в output directory, проверен playback. Показывает actual PR auto run, diff+files, docs+code RAG, три раздела review, fixing commit/rerun и текущий UI/уведомление. Читаемые подробные captions объясняют передачу данных и результат. |
| D32-16 | Публикация и полный исходный scope | Независимая финальная интеграция всех критериев; основной исполнитель проверяет privacy/public staged scope, commit/push, совпадение remote, full tracked/untracked status. Не утверждать D32 complete при открытом обязательном CI/LIVE/artifact criterion. |

## Доказательства

Достаточный набор публичных обезличенных proof: docs/evidence/day-32/verification.json с критериями и автором независимой проверки, PR/run URLs и commit SHA, collector/retrieval manifest, sanitized model reviews/golden assessment, UI screenshots и уведомление delivery status/message ID. Файлы большого сырого telemetry и локальные скриншоты могут остаться ignored; опубликованный summary обязан содержать фактический результат, а не raw secret data.

Evidence records scenario, method OFFLINE/LIVE/BROWSER/CI/ARTIFACT, actual provider, source version and coverage, observed outcome, independent judgment and limitations. Одна проверка может доказать несколько criteria при явном mapping. В финале разделять technical execution, model semantic assessment и product acceptance.

## Specification gate

Статический Tester до реализации проверяет полноту source/owner scope, реализуемость выбранного стека, явные cloud prerequisites, safety/publication границу, согласованность всех документов, достаточность негативных сценариев и evidence методов. READY_FOR_INDEPENDENT_REVIEW автора не является gate PASS.
