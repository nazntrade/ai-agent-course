# D34 — Приёмка полного исходного задания

ACCEPTANCE_STATUS: NOT_ASSESSED  
SPEC_GATE_STATUS: NOT_ASSESSED

## Правила оценки

Замороженный acceptance-contract-v2 ниже — единственный продуктовый контракт D34-01..21. Все критерии required=true, owner=agent; в том числе третий code scenario, DOCX, MP4, оба provider и публикация. Полнота не заменяется старым предварительным списком, подзадачным PASS или статической проверкой документов. Для стадии SPECIFICATION независимый Tester использует отдельный specification-contract; этот файл определяет будущую полную приёмку продукта и не утверждает, что методы уже выполнены.

LIVE означает фактический выбранный provider и настоящие SDK tools/list/tools/call. BROWSER — взаимодействие с настоящим app, запущенным доверенным run_app.bat; прочитанный HTML/API и health сами по себе недостаточны. OFFLINE — реальные проверки actual bytes/state на своих TEMP fixtures без env/сети. MANUAL — независимое чтение целых outputs против полной source truth, а не только citations/формат. ARTIFACT — реальные файлы и визуальный QA. GIT — actual scope/remote/whole status авторизованным исполнителем.

Каждый критерий требует собственного JSON под frozen glob `local-data/d34-evidence/D34-XX-*.json`, 1..32 непустых records; они находятся вне source-review и не содержат секретов/частной инфраструктуры. Оценка по умолчанию NOT_ASSESSED. Independent PASS содержит refs на все совпавшие файлы artifact и содержательное обоснование с observed facts. Старый FAIL сохраняется и объясняется свежим исправлением/свидетельством, не перезаписью. Evidence existence/схема/exit0 не равны истинности результата. Полные разрешённые answers/tool results читаются; private reasoning не входит в evidence.

## Предварительно определённые процедуры и оракулы

Все сценарные команды из PLAN пока планируются. Они выполняются через существующий `test.bat scenario <slug>` после реализации; обычная регрессия через `test.bat`, smoke через `smoke_test.bat`. Реальный browser используется отдельно и сохраняет provenance. Методы frozen check не понижаются до static/unit. Достаточность оценивается независимым Tester; недоступность обязательного метода даёт конкретный BLOCKED/NOT_ASSESSED.

| ID | Процедура / уровень | Что независимый проверяющий должен увидеть; опровергающий случай |
|---|---|---|
| D34-01 | BROWSER trusted app; P5/P7 | Выбор Project files, проекта и Local/DeepSeek; цели в текущем чате; работающие Activity/Files/Diff/Checks, история после reopen. Переключение не подменяет project/provider старой карточки |
| D34-02 | LIVE `d34-capability`, `d34-goals` | Whole actual selected-provider message→typed tool call IDs/args→SDK list/call→results→follow-up, реальные file hashes/bytes. Нет file steps в user goal, host-injected calls, canned solution или task-string routing; второй проект с другими именами проверяет generic engine |
| D34-03 | OFFLINE `d34-files-controls` + LIVE goals | Late-line fact после 200 получен моделью; >1 search page, complete metadata честны; actual missing/no-match/oversized/binary и длинные строки. Неполный traversal не становится «все файлы проверены» |
| D34-04 | LIVE goals + MANUAL full report/source | Impact читает ≥3 distinct files, самостоятельно находит implementation, всех relevant consumers/tests/docs, explicit rule conflicts; настоящий migration report поддержан версиями/строками. Независимый полный inventory классифицирует каждый missing/wrong/unsupported fact |
| D34-05 | LIVE goals + MANUAL whole docs/diff | ≥3 прочитанных files; реальные README/API edits + truthful changelog; route/body/version/error policies соответствуют полному source. Unreleased вместо выдуманной истории, нет unsupported test success/endpoints/capabilities |
| D34-06 | LIVE goals + MANUAL + OFFLINE `d34-content-controls` | ≥3 file reads; narrow actual client edit на baseline, correct request/response/concurrency/error handling; проверенные unchanged unrelated list/filter behavior. Изменение API/tests для оправдания старого client проваливается; execution limitations видны |
| D34-07 | OFFLINE controls + BROWSER + LIVE | Model create/edit пишет настоящие persisted draft bytes; server diff из immutable baseline/current files, new files/MISSING и readback; reopened run сохраняет версии. Prose patch без файла недостаточен |
| D34-08 | OFFLINE `d34-apply-controls` + BROWSER | One selected reviewed revision Apply; exact target hashes и source/read/search inventory dependencies rechecked. Original existing edits сохраняются, concurrent dependency/target change → conflict до записи, stale token →409; successful/no-op outcomes соответствуют bytes |
| D34-09 | OFFLINE apply controls + BROWSER recovery | Fault между journal/несколькими replacements/receipt + owned restart; before/after/foreign reconciliation, никакого false completed. Guarded Undo/rollback возвращают только exact after state; later user edit не затёрт, conflicting multi-file preflight не меняет ничего |
| D34-10 | OFFLINE file/apply controls | Traversal, drive/UNC/case aliases/ADS/reserved/absolute, symlink/junction/reparse/hardlinked targets, stale parent, malformed schema/args; синтетический injection просит secret/shell/delete/publish и получает deny. Проверка не читает реальные secret bytes |
| D34-11 | OFFLINE workflow controls + LIVE goals | Finite calls/bytes/time учитываются между steps; goal/constraints/versioned evidence переживают compaction; valid paging/readback разрешены, бессмысленные повторы остановлены; budget/output exhaustion → incomplete, не придуманный final success |
| D34-12 | OFFLINE workflow/apply controls + BROWSER | Два job/tab/process на same canonical workspace не пишут одновременно; queued/running Stop, late provider/MCP response не writes; restart interrupted/recovery явен; stale project/chat/run/revision requests отклонены |
| D34-13 | LIVE capability/goals каждого provider | Оба настоящих выбранных Local и DeepSeek проходят три whole scenarios; truthful safe settings/selected model/protocol provenance; no fallback/global config change, failed outcomes сохранены. Lease finally и borrowed runtime preservation видны; mock/другой provider не закрывают критерий |
| D34-14 | LIVE `d34-repeat` + OFFLINE content/bytes | Основные goals повторяются с own clean immutable fixture; documentation/client повтор на обновлённом workspace no-op по смыслу, без duplicated changelog/redundant destructive edits; не требовать одинаковых слов |
| D34-15 | MANUAL + OFFLINE content controls | Отдельные syntax/static/actual trusted tests/draft readiness/application/quality. Нарочно неверный, но валидный и снабжённый правдоподобной citation отчёт не принят; нет доверия ослабленным tests; unexecuted/unsupported остаются NOT_ASSESSED |
| D34-16 | Full OFFLINE `test.bat`, smoke + representative BROWSER | D31 help/RAG/MCP, D32 PR workflow и D33 independent Support реально доступны/дают ожидаемое поведение; прежние chats/history/model settings/data сохраняются. Изолированные данные, без mutation частных данных и остановки чужого runtime |
| D34-17 | LIVE+BROWSER actual `run_app.bat` | Текущий trusted launch идентифицирован по owned process + UI/health; missing/invalid config и unavailable tools/model дают видимые сохранённые ошибки. Открытое старое приложение или один порт не доказательство |
| D34-18 | ARTIFACT full DOCX QA | Русский D34_Учебная_документация.docx в согласованной Desktop папке; теория, эта реализация, data/tool flow, причины design, hands-on и фактически прочитанные week7 notes connections. Actual screenshots; render и inspection каждой страницы, final copy SHA-256 |
| D34-19 | ARTIFACT+BROWSER actual recording | D34_Демонстрация.mp4 в согласованной папке; wide target1920×1080, Russian explanation/captions; actual UI goals/tools/≥3files/real draft/diff/checks/Apply/recovery/repeat, все сценарии и providers. Полный decode/streams + visual chapters; fake/recreated UI нельзя принять |
| D34-20 | PROCESS | Current readiness receipts/spec hashes, independent static PASS до кода, один product writer, другой current post-review, role reports и неизменные все21 критерия в final integration. Никакой author's READY/subtask PASS как total acceptance |
| D34-21 | GIT authorized root | Initial snapshot→точная sanitized staged область необходимых code/tests/docs/results→actual authorized commit/push→target remote head equality→whole Course tracked/untracked clean. Runtime/private data исключены; скрытые/чужие остатки исключают clean claim |

## Независимая предметная истина

До LIVE fixture golden checker, full inventory и release facts фиксируются hash-ами вне readable/writable project scope модели. Модель может читать настоящие contract tests как evidence, но не ослаблять проверяющий oracle. Истина включает current route `POST /api/tickets/{ticket_id}/resolve`, непустой trimmed resolution, expected_version, response status/resolution/version, version conflict, already-resolved policy/no automatic reopen; все actual consumers/tests/docs, late-line invariant и PROJECT_GUIDE conflicts. Существующие unrelated filters должны сохранить входы/выходы. Изменение fact set после отрицательного ответа требует новой явной baseline version и объяснения, не скрытого подгона.

Oracle semantics v2 — уточнение без изменения D34-01..21: route/body/status/resolution/version и trim являются контрактом SERVER wire. Adapter обязан строить правильный resolve request, использовать показанную expected_version и читать новые wire поля, но может передавать исходный непустой padded text на сервер, который сам trim-ит его, либо выполнять эквивалентный trim. Публичный результат adapter может сохранять прежние state/note через чтение body.status/body.resolution; новая status/resolution projection допустима только при совместимости actual consumers. Не требовать конкретного слоя нормализации или переименования публичного результата при одинаковой API-семантике. Сигнатуры, реальные значения id/version/text/status, явные ошибки/concurrency/no automatic retry/reopen и unrelated filters/order/inputs сохраняются. Baseline fingerprint RELEASE_NOTES остаётся проверкой происхождения fixture/release facts; сам draft RELEASE_NOTES не является запрещённым contract edit. Truthful related Unreleased updates допустимы и проверяются отдельно целиком; это не утверждение их истины. Draft server/tests/PROJECT_GUIDE остаются запрещёнными для оправдания старого клиента. Старые checker verdicts сохраняются с прежним hash; после независимого static gate используется явная новая версия oracle и новый hash, а не переписывание доказательств.

Для impact: вручную сопоставить каждый consumer/claim и priority migration step с полным source; для docs: прочитать целые README/API/changelog и computed diff; для client: прочитать actual patch и оценить request/response/concurrency plus unrelated behavior trusted checks. Ошибочный маршрут при настоящей citation — всё равно FAIL. Список не заменяется количеством tool calls или словом «проверено».

Достаточная provider matrix: Local и DeepSeek отдельно выполняют impact/documentation/client на чистых baseline; основные impact/documentation повторяются с baseline; documentation/client повторяются на уже обновлённом workspace. Цели без file names/tool steps. Эти реальные runs плюс BROWSER фиксируют model-led initiative; offline дополнительно проверяет bounds/conflict/stop/links и content counterexamples. Документация/видео показывают то же наблюдаемое поведение, а не заранее подготовленный diff.

## Итоговый независимый отчёт

Отчёт acceptance-evidence-v2 содержит точные исходные21 ID без добавлений/дубликатов, status PASS/PARTIAL/FAIL/BLOCKED/NOT_ASSESSED, evidence refs на все matched files объявленного artifact и причины каждого не-PASS. Технический TEST_STATUS отделён от substantive_result и общего завершения. Whole task ACCEPTED только при independent sufficient PASS всех21, включая артефакты/remote/whole repo. Если PASS недоказуем — продолжить доступную разрешённую работу и назвать конкретный остаток, не снять criterion.

## Полный замороженный продуктовый контракт

```acceptance-contract
{
  "schema_version": "acceptance-contract-v2",
  "criteria": [
    {
      "id": "D34-01",
      "required": true,
      "owner": "agent",
      "requirement": "Chat-first Project files mode supports selected project and explicit Local/DeepSeek, retained history, collapsible activity/files/diff/checks.",
      "source": "D34 assignment plus user-approved Project maintenance plan and explicit Word/video request; standing publication authorization",
      "check": "BROWSER: actual trusted launched app, select mode/project/provider, run goals and inspect controls/results.",
      "artifacts": [
        {
          "id": "d34-01-proof",
          "glob": "local-data/d34-evidence/D34-01-*.json",
          "format": "json",
          "count": {
            "min": 1,
            "max": 32
          },
          "assertions": []
        }
      ]
    },
    {
      "id": "D34-02",
      "required": true,
      "owner": "agent",
      "requirement": "Model initiates actual typed MCP file tools from goal-level requests without user-provided file steps, canned routing or injected fake tool calls.",
      "source": "D34 assignment plus user-approved Project maintenance plan and explicit Word/video request; standing publication authorization",
      "check": "LIVE: actual SDK tools/list and tools/call; full selected-provider tool-call/result transcript binds real file paths/content/hashes.",
      "artifacts": [
        {
          "id": "d34-02-proof",
          "glob": "local-data/d34-evidence/D34-02-*.json",
          "format": "json",
          "count": {
            "min": 1,
            "max": 32
          },
          "assertions": []
        }
      ]
    },
    {
      "id": "D34-03",
      "required": true,
      "owner": "agent",
      "requirement": "Read and search multiple UTF-8 project files with ranges/pagination, beyond line200; report exclusions/truncation/coverage honestly.",
      "source": "D34 assignment plus user-approved Project maintenance plan and explicit Word/video request; standing publication authorization",
      "check": "OFFLINE+LIVE: late-line facts, >one results page, missing/no-match/oversized/binary cases; never claim all with incomplete coverage.",
      "artifacts": [
        {
          "id": "d34-03-proof",
          "glob": "local-data/d34-evidence/D34-03-*.json",
          "format": "json",
          "count": {
            "min": 1,
            "max": 32
          },
          "assertions": []
        }
      ]
    },
    {
      "id": "D34-04",
      "required": true,
      "owner": "agent",
      "requirement": "Impact scenario reads >=3distinct files, identifies API implementation/consumers/tests/docs and project-rule conflicts, creates source-supported migration report.",
      "source": "D34 assignment plus user-approved Project maintenance plan and explicit Word/video request; standing publication authorization",
      "check": "LIVE+MANUAL: goal without file instructions on known fixture; judge complete report against actual full source and invariants, classify missing facts.",
      "artifacts": [
        {
          "id": "d34-04-proof",
          "glob": "local-data/d34-evidence/D34-04-*.json",
          "format": "json",
          "count": {
            "min": 1,
            "max": 32
          },
          "assertions": []
        }
      ]
    },
    {
      "id": "D34-05",
      "required": true,
      "owner": "agent",
      "requirement": "Documentation scenario reads >=3distinct files, updates actual README/API documentation and creates/updates truthful changelog from actual project facts.",
      "source": "D34 assignment plus user-approved Project maintenance plan and explicit Word/video request; standing publication authorization",
      "check": "LIVE+MANUAL: inspect whole produced files and computed diff; no invented endpoint, test success, history or capability.",
      "artifacts": [
        {
          "id": "d34-05-proof",
          "glob": "local-data/d34-evidence/D34-05-*.json",
          "format": "json",
          "count": {
            "min": 1,
            "max": 32
          },
          "assertions": []
        }
      ]
    },
    {
      "id": "D34-06",
      "required": true,
      "owner": "agent",
      "requirement": "Third client scenario reads >=3distinct files and makes narrow actual client-code edits consistent with current API while preserving unrelated behavior.",
      "source": "D34 assignment plus user-approved Project maintenance plan and explicit Word/video request; standing publication authorization",
      "check": "LIVE+MANUAL+OFFLINE: actual file changes on fixture, substantive contract checks and unchanged unrelated behavior; execution limitations explicit.",
      "artifacts": [
        {
          "id": "d34-06-proof",
          "glob": "local-data/d34-evidence/D34-06-*.json",
          "format": "json",
          "count": {
            "min": 1,
            "max": 32
          },
          "assertions": []
        }
      ]
    },
    {
      "id": "D34-07",
      "required": true,
      "owner": "agent",
      "requirement": "Model tools create and edit real draft files; diff is computed from actual immutable baseline and current contents, including new files and readback.",
      "source": "D34 assignment plus user-approved Project maintenance plan and explicit Word/video request; standing publication authorization",
      "check": "OFFLINE+BROWSER+LIVE: inspect persisted bytes/diff outside assistant prose; reopening run retains file evidence.",
      "artifacts": [
        {
          "id": "d34-07-proof",
          "glob": "local-data/d34-evidence/D34-07-*.json",
          "format": "json",
          "count": {
            "min": 1,
            "max": 32
          },
          "assertions": []
        }
      ]
    },
    {
      "id": "D34-08",
      "required": true,
      "owner": "agent",
      "requirement": "One Apply action checks current source read-dependencies and target hashes, preserves existing user edits and applies only the selected reviewed draft.",
      "source": "D34 assignment plus user-approved Project maintenance plan and explicit Word/video request; standing publication authorization",
      "check": "OFFLINE+BROWSER: successful apply, concurrent source/target changes and no-op; inspect original file bytes, conflicts and exact applied state.",
      "artifacts": [
        {
          "id": "d34-08-proof",
          "glob": "local-data/d34-evidence/D34-08-*.json",
          "format": "json",
          "count": {
            "min": 1,
            "max": 32
          },
          "assertions": []
        }
      ]
    },
    {
      "id": "D34-09",
      "required": true,
      "owner": "agent",
      "requirement": "Undo and interrupted application use guarded recovery/journal, never overwrite later user edits or label a partially applied batch complete.",
      "source": "D34 assignment plus user-approved Project maintenance plan and explicit Word/video request; standing publication authorization",
      "check": "OFFLINE+BROWSER: multi-file interruption/restart/recovery and guarded undo; inspect actual before/after original files and recorded outcomes.",
      "artifacts": [
        {
          "id": "d34-09-proof",
          "glob": "local-data/d34-evidence/D34-09-*.json",
          "format": "json",
          "count": {
            "min": 1,
            "max": 32
          },
          "assertions": []
        }
      ]
    },
    {
      "id": "D34-10",
      "required": true,
      "owner": "agent",
      "requirement": "Enforce project/draft boundaries, excluded secrets/config/Git/dependencies/links, tool argument validation and no arbitrary shell/delete/publication capability.",
      "source": "D34 assignment plus user-approved Project maintenance plan and explicit Word/video request; standing publication authorization",
      "check": "OFFLINE: traversal, Windows absolute/drive/UNC/case paths, symlink/junction escapes, malformed tools and untrusted file injection; no real secrets read.",
      "artifacts": [
        {
          "id": "d34-10-proof",
          "glob": "local-data/d34-evidence/D34-10-*.json",
          "format": "json",
          "count": {
            "min": 1,
            "max": 32
          },
          "assertions": []
        }
      ]
    },
    {
      "id": "D34-11",
      "required": true,
      "owner": "agent",
      "requirement": "Finite workflow decomposes goal into small dependent steps, retains original requirements and versioned evidence, stops unproductive repeated actions.",
      "source": "D34 assignment plus user-approved Project maintenance plan and explicit Word/video request; standing publication authorization",
      "check": "OFFLINE+LIVE: bounded calls/bytes/time, meaningful retry and context behavior; budget exhaustion is incomplete, not fabricated completion.",
      "artifacts": [
        {
          "id": "d34-11-proof",
          "glob": "local-data/d34-evidence/D34-11-*.json",
          "format": "json",
          "count": {
            "min": 1,
            "max": 32
          },
          "assertions": []
        }
      ]
    },
    {
      "id": "D34-12",
      "required": true,
      "owner": "agent",
      "requirement": "Single writer per workspace; Stop cancels queued/running work, late callbacks cannot write/apply; restart/project switch keeps runs scoped and interrupted work explicit.",
      "source": "D34 assignment plus user-approved Project maintenance plan and explicit Word/video request; standing publication authorization",
      "check": "OFFLINE+BROWSER: overlapping jobs, stop during tool/model work, late response, reopen/restart and stale project/tab requests.",
      "artifacts": [
        {
          "id": "d34-12-proof",
          "glob": "local-data/d34-evidence/D34-12-*.json",
          "format": "json",
          "count": {
            "min": 1,
            "max": 32
          },
          "assertions": []
        }
      ]
    },
    {
      "id": "D34-13",
      "required": true,
      "owner": "agent",
      "requirement": "Both selected real Local and DeepSeek complete required goal-driven file scenarios with correct provider protocol/settings, no silent fallback or global configuration changes.",
      "source": "D34 assignment plus user-approved Project maintenance plan and explicit Word/video request; standing publication authorization",
      "check": "LIVE: small implementation-stage capability probe then sufficient whole real scenarios; preserve whole answers/tool evidence/settings and negative outcomes; no remote/local substitution.",
      "artifacts": [
        {
          "id": "d34-13-proof",
          "glob": "local-data/d34-evidence/D34-13-*.json",
          "format": "json",
          "count": {
            "min": 1,
            "max": 32
          },
          "assertions": []
        }
      ]
    },
    {
      "id": "D34-14",
      "required": true,
      "owner": "agent",
      "requirement": "Clean-baseline runs reproduce correct outcomes; repeating goal on already updated workspace avoids duplicate changelog and destructive redundant edits.",
      "source": "D34 assignment plus user-approved Project maintenance plan and explicit Word/video request; standing publication authorization",
      "check": "LIVE+OFFLINE: reset own immutable fixture, repeat required scenarios, then no-op repeat on updated files; semantic correctness not byte-identical wording.",
      "artifacts": [
        {
          "id": "d34-14-proof",
          "glob": "local-data/d34-evidence/D34-14-*.json",
          "format": "json",
          "count": {
            "min": 1,
            "max": 32
          },
          "assertions": []
        }
      ]
    },
    {
      "id": "D34-15",
      "required": true,
      "owner": "agent",
      "requirement": "Quality, syntax/static checks, actual trusted tests, draft readiness and actual application are separate; independent content review judges the original goal.",
      "source": "D34 assignment plus user-approved Project maintenance plan and explicit Word/video request; standing publication authorization",
      "check": "MANUAL+OFFLINE: deliberately wrong but valid/cited output cannot become accepted; unsupported or unexecuted checks stay NOT_ASSESSED.",
      "artifacts": [
        {
          "id": "d34-15-proof",
          "glob": "local-data/d34-evidence/D34-15-*.json",
          "format": "json",
          "count": {
            "min": 1,
            "max": 32
          },
          "assertions": []
        }
      ]
    },
    {
      "id": "D34-16",
      "required": true,
      "owner": "agent",
      "requirement": "D31 help/RAG/MCP, D32 PR workflow, D33 independent support and existing chats/data/model settings are preserved.",
      "source": "D34 assignment plus user-approved Project maintenance plan and explicit Word/video request; standing publication authorization",
      "check": "OFFLINE+BROWSER: appropriate full regression plus representative actual existing feature behavior; no private data mutation or borrowed runtime shutdown.",
      "artifacts": [
        {
          "id": "d34-16-proof",
          "glob": "local-data/d34-evidence/D34-16-*.json",
          "format": "json",
          "count": {
            "min": 1,
            "max": 32
          },
          "assertions": []
        }
      ]
    },
    {
      "id": "D34-17",
      "required": true,
      "owner": "agent",
      "requirement": "Actual trusted launch works, absent/invalid configuration and unavailable model/tools give explicit states with saved evidence, no false success.",
      "source": "D34 assignment plus user-approved Project maintenance plan and explicit Word/video request; standing publication authorization",
      "check": "LIVE+BROWSER: run_app.bat actual process/UI health and representative unavailable/config error; owned-process lifecycle only.",
      "artifacts": [
        {
          "id": "d34-17-proof",
          "glob": "local-data/d34-evidence/D34-17-*.json",
          "format": "json",
          "count": {
            "min": 1,
            "max": 32
          },
          "assertions": []
        }
      ]
    },
    {
      "id": "D34-18",
      "required": true,
      "owner": "agent",
      "requirement": "Russian Word tutorial explains theory and this implementation, data/tool flow, design reasons and hands-on use, with actual screenshots and week7-note connections.",
      "source": "D34 assignment plus user-approved Project maintenance plan and explicit Word/video request; standing publication authorization",
      "check": "ARTIFACT: save D34_Учебная_документация.docx in requested Desktop Current task; render and inspect every page, verify screenshots/source claims and final copy hash.",
      "artifacts": [
        {
          "id": "d34-18-proof",
          "glob": "local-data/d34-evidence/D34-18-*.json",
          "format": "json",
          "count": {
            "min": 1,
            "max": 32
          },
          "assertions": []
        }
      ]
    },
    {
      "id": "D34-19",
      "required": true,
      "owner": "agent",
      "requirement": "Wide good-quality MP4 demonstrates all D34 requirements and agreed scenarios in the actual launched app, goals/tools/multiple files/persisted edits/diff/checks/repeat.",
      "source": "D34 assignment plus user-approved Project maintenance plan and explicit Word/video request; standing publication authorization",
      "check": "ARTIFACT+BROWSER: actual recorded UI evidence; target1920x1080 MP4 with Russian explanation/captions, full decode/stream checks and visual chapter review; save D34_Демонстрация.mp4 in requested folder.",
      "artifacts": [
        {
          "id": "d34-19-proof",
          "glob": "local-data/d34-evidence/D34-19-*.json",
          "format": "json",
          "count": {
            "min": 1,
            "max": 32
          },
          "assertions": []
        }
      ]
    },
    {
      "id": "D34-20",
      "required": true,
      "owner": "agent",
      "requirement": "Current Architect design/static spec gate before implementation, single product writer, current post-review and independent whole-task acceptance cover all original criteria.",
      "source": "D34 assignment plus user-approved Project maintenance plan and explicit Word/video request; standing publication authorization",
      "check": "PROCESS: current bootstrap receipts, spec/source hashes, role reports, complete immutable contract and sufficient per-criterion independent evidence; no subtask PASS mistaken for completion.",
      "artifacts": [
        {
          "id": "d34-20-proof",
          "glob": "local-data/d34-evidence/D34-20-*.json",
          "format": "json",
          "count": {
            "min": 1,
            "max": 32
          },
          "assertions": []
        }
      ]
    },
    {
      "id": "D34-21",
      "required": true,
      "owner": "agent",
      "requirement": "Necessary sanitized code/tests/docs/results are committed/pushed by authorized root publisher; target remote matches and whole Course repository is clean.",
      "source": "D34 assignment plus user-approved Project maintenance plan and explicit Word/video request; standing publication authorization",
      "check": "GIT: compare initial snapshot, exact staged scope and privacy audit, actual push/ls-remote verification and tracked/untracked status across whole repository.",
      "artifacts": [
        {
          "id": "d34-21-proof",
          "glob": "local-data/d34-evidence/D34-21-*.json",
          "format": "json",
          "count": {
            "min": 1,
            "max": 32
          },
          "assertions": []
        }
      ]
    }
  ]
}
```
