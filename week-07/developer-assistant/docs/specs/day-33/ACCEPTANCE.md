# D33 — Замороженный полный контракт приёмки

CONTRACT_STATUS: FROZEN_FOR_IMPLEMENTATION_AFTER_INDEPENDENT_SPEC_REVIEW
SPEC_GATE_STATUS: NOT_ASSESSED

Все D33-01..16 обязательны и происходят из исходного задания и прямых уточнений владельца. Этот контракт не заменяется проверкой SPEC. Авторский READY/preflight/unit не является независимым PASS продукта.

## Полный acceptance-contract-v2

Ниже неизменный root contract: IDs/owner/requirement/source/check и все artifacts сохранены. JSONproof relative paths находятся в уже ignored local-data; runtimeSQLite/keys/rawsnapshots не evidenceartifact. DOCX/MP4 реально создаются в пользовательском outputdirectory; D33-14/15 JSON содержит hashcopy/render/videoinspectionproof. Publicsummary не раскрывает private localpaths.

```acceptance-contract
{
  "schema_version": "acceptance-contract-v2",
  "criteria": [
    {
      "id": "D33-01",
      "required": true,
      "owner": "agent",
      "requirement": "Separate support page and main-app Support button open the same support service/tickets; independent launch and non-AI operation survive main app shutdown.",
      "source": "D33 assignment and owner's approved separate support page plus in-app button, Local/DeepSeek tests, Telegram, detailed Word/video and standing publication authorization",
      "check": "BROWSER/LIVE: use both entries; stop only owned main process, keep support health+FAQ+ticket creation available; no conflicting port/process stopped.",
      "artifacts": [
        {
          "id": "d33-01-proof",
          "glob": "local-data/d33-evidence/D33-01-*.json",
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
      "id": "D33-02",
      "required": true,
      "owner": "agent",
      "requirement": "Real persistent ticket creation, list, conversation/follow-up and state distinguish answered from solved; reload/restart preserves history.",
      "source": "D33 assignment and owner's approved separate support page plus in-app button, Local/DeepSeek tests, Telegram, detailed Word/video and standing publication authorization",
      "check": "BROWSER/OFFLINE: create/reopen ticket, send follow-up, restart owned support service, verify exact history; only explicit owner confirmation may resolve.",
      "artifacts": [
        {
          "id": "d33-02-proof",
          "glob": "local-data/d33-evidence/D33-02-*.json",
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
      "id": "D33-03",
      "required": true,
      "owner": "agent",
      "requirement": "RAG retrieves relevant product FAQ/documentation with paths/versions and citations; no hardcoded answer or whole-FAQ-only prompt.",
      "source": "D33 assignment and owner's approved separate support page plus in-app button, Local/DeepSeek tests, Telegram, detailed Word/video and standing publication authorization",
      "check": "OFFLINE/LIVE/BROWSER: actual retrieval and displayed excerpts; changed/deleted FAQ reindex, Russian paraphrases, unrelated/no-document cases.",
      "artifacts": [
        {
          "id": "d33-03-proof",
          "glob": "local-data/d33-evidence/D33-03-*.json",
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
      "id": "D33-04",
      "required": true,
      "owner": "agent",
      "requirement": "Actual SDK MCP tools/list and tools/call retrieve bounded current user/ticket JSON context from separate server.",
      "source": "D33 assignment and owner's approved separate support page plus in-app button, Local/DeepSeek tests, Telegram, detailed Word/video and standing publication authorization",
      "check": "OFFLINE/LIVE: actual stdio MCP handshake/tool result; demonstrate current version after ticket update, wrong/malformed/missing data and unavailable MCP remain errors.",
      "artifacts": [
        {
          "id": "d33-04-proof",
          "glob": "local-data/d33-evidence/D33-04-*.json",
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
      "id": "D33-05",
      "required": true,
      "owner": "agent",
      "requirement": "Full model answers depend on correct ticket facts and earlier attempts; same question differs for known diagnostic cases.",
      "source": "D33 assignment and owner's approved separate support page plus in-app button, Local/DeepSeek tests, Telegram, detailed Word/video and standing publication authorization",
      "check": "LIVE Local and DeepSeek: explicit401 invalid_key, GitHub403 rate-limit, Local refused; independent whole-answer check against facts+FAQ, not schema/citations alone.",
      "artifacts": [
        {
          "id": "d33-05-proof",
          "glob": "local-data/d33-evidence/D33-05-*.json",
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
      "id": "D33-06",
      "required": true,
      "owner": "agent",
      "requirement": "Demo users are clearly synthetic; server-owned scope checks every relevant API/tool and blocks foreign ticket access.",
      "source": "D33 assignment and owner's approved separate support page plus in-app button, Local/DeepSeek tests, Telegram, detailed Word/video and standing publication authorization",
      "check": "OFFLINE/BROWSER: forge ticket/user IDs, conversation/source/job IDs; no foreign field leaks; persona selection is not represented as production authentication.",
      "artifacts": [
        {
          "id": "d33-06-proof",
          "glob": "local-data/d33-evidence/D33-06-*.json",
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
      "id": "D33-07",
      "required": true,
      "owner": "agent",
      "requirement": "Unknown facts/context are not invented; bounded follow-up and escalation, stale ticket/version results not treated as current.",
      "source": "D33 assignment and owner's approved separate support page plus in-app button, Local/DeepSeek tests, Telegram, detailed Word/video and standing publication authorization",
      "check": "OFFLINE/LIVE: missing diagnostic fact => appropriate question; MCP failure => honest failure; context/version switch and malicious ticket instructions cannot redirect capabilities.",
      "artifacts": [
        {
          "id": "d33-07-proof",
          "glob": "local-data/d33-evidence/D33-07-*.json",
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
      "id": "D33-08",
      "required": true,
      "owner": "agent",
      "requirement": "Selected Local and DeepSeek both work with preserved parameters and no hidden provider fallback; raw final answers and settings identity retained.",
      "source": "D33 assignment and owner's approved separate support page plus in-app button, Local/DeepSeek tests, Telegram, detailed Word/video and standing publication authorization",
      "check": "LIVE same-input cases for both, actual model alias/usage/time/source provenance; model unavailable still leaves manual FAQ/form usable and result is not PASS.",
      "artifacts": [
        {
          "id": "d33-08-proof",
          "glob": "local-data/d33-evidence/D33-08-*.json",
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
      "id": "D33-09",
      "required": true,
      "owner": "agent",
      "requirement": "Visible wait/streaming, Stop, queue and restart/late-result guards; model code/content not executed in browser or host.",
      "source": "D33 assignment and owner's approved separate support page plus in-app button, Local/DeepSeek tests, Telegram, detailed Word/video and standing publication authorization",
      "check": "BROWSER/OFFLINE: real in-flight Stop; noncooperative late-result control; safe rendering, bounded work and recovery, no endless repeated model rounds.",
      "artifacts": [
        {
          "id": "d33-09-proof",
          "glob": "local-data/d33-evidence/D33-09-*.json",
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
      "id": "D33-10",
      "required": true,
      "owner": "agent",
      "requirement": "Telegram support/escalation/report goes only to saved owner with explicit action/opt-in and honest delivery state.",
      "source": "D33 assignment and owner's approved separate support page plus in-app button, Local/DeepSeek tests, Telegram, detailed Word/video and standing publication authorization",
      "check": "LIVE fixed-owner acknowledgement/messageID; OFFLINE duplicate/uncertain/stale/failed/cancelled no fake send; no secret/contact/full foreign ticket in message.",
      "artifacts": [
        {
          "id": "d33-10-proof",
          "glob": "local-data/d33-evidence/D33-10-*.json",
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
      "id": "D33-11",
      "required": true,
      "owner": "agent",
      "requirement": "First run, separate support data and MCP lifecycles are reproducible using existing trusted entrypoint without dependencies/keys forcing non-AI features closed.",
      "source": "D33 assignment and owner's approved separate support page plus in-app button, Local/DeepSeek tests, Telegram, detailed Word/video and standing publication authorization",
      "check": "LIVE/OFFLINE: independent launch via run_app.bat product mode; missing model/key exposes useful support UI, fresh isolated support dataset, borrowed provider not stopped.",
      "artifacts": [
        {
          "id": "d33-11-proof",
          "glob": "local-data/d33-evidence/D33-11-*.json",
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
      "id": "D33-12",
      "required": true,
      "owner": "agent",
      "requirement": "Existing D31/D32 UI, APIs, persisted user data, provider settings and review/help functionality preserved.",
      "source": "D33 assignment and owner's approved separate support page plus in-app button, Local/DeepSeek tests, Telegram, detailed Word/video and standing publication authorization",
      "check": "OFFLINE affected/full suite + BROWSER/LIVE representative help/branch/review history/button; independent support DB/corpus avoid corrupting prior history.",
      "artifacts": [
        {
          "id": "d33-12-proof",
          "glob": "local-data/d33-evidence/D33-12-*.json",
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
      "id": "D33-13",
      "required": true,
      "owner": "agent",
      "requirement": "Complete SPEC/PLAN/ACCEPTANCE, readiness and architecture/spec independent reviews before implementation; incremental single writer.",
      "source": "D33 assignment and owner's approved separate support page plus in-app button, Local/DeepSeek tests, Telegram, detailed Word/video and standing publication authorization",
      "check": "STATIC project preflight and content hashes; no source edits before PASS; current post-architecture and final independent integration cover full frozen contract.",
      "artifacts": [
        {
          "id": "d33-13-proof",
          "glob": "local-data/d33-evidence/D33-13-*.json",
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
      "id": "D33-14",
      "required": true,
      "owner": "agent",
      "requirement": "Detailed Russian teaching DOCX in Current task explains theory and actual support implementation, data senders/receivers, JSON/MCP/RAG, models, Telegram/GitHub role and week7, with real screenshots.",
      "source": "D33 assignment and owner's approved separate support page plus in-app button, Local/DeepSeek tests, Telegram, detailed Word/video and standing publication authorization",
      "check": "ARTIFACT: actual final DOCX rendered all pages inspected, factual walkthroughs match code/observations, source/destination hashes equal; GitHub not falsely claimed as D33 query trigger.",
      "artifacts": [
        {
          "id": "d33-14-proof",
          "glob": "local-data/d33-evidence/D33-14-*.json",
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
      "id": "D33-15",
      "required": true,
      "owner": "agent",
      "requirement": "Published reproducible code and detailed wide high-quality MP4 in Current task demonstrate every D33 requirement, both entries, RAG/MCP context and useful answers.",
      "source": "D33 assignment and owner's approved separate support page plus in-app button, Local/DeepSeek tests, Telegram, detailed Word/video and standing publication authorization",
      "check": "ARTIFACT/BROWSER: genuine recordings,1920x1080 MP4 subtitles/explanation readable, full decode/timeline checks; actual scenario/version/negative limits accurately represented.",
      "artifacts": [
        {
          "id": "d33-15-proof",
          "glob": "local-data/d33-evidence/D33-15-*.json",
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
      "id": "D33-16",
      "required": true,
      "owner": "agent",
      "requirement": "Full independent acceptance, sanitized commit/push+remote verification and clean task scope; one final truthful owner Telegram notification after completion.",
      "source": "D33 assignment and owner's approved separate support page plus in-app button, Local/DeepSeek tests, Telegram, detailed Word/video and standing publication authorization",
      "check": "Independent all criteria judged; publish only authorized product/spec/tests/sanitized evidence, remote==HEAD and whole tracked/untracked accounted; copied outputs verified; completion notification receipt.",
      "artifacts": [
        {
          "id": "d33-16-proof",
          "glob": "local-data/d33-evidence/D33-16-*.json",
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

## Доказательства и смысл

Proof включает criterion IDs, actualmethod/scenario, source/ticket/contextversions, provider/alias дляLIVE, actualobservedresult/wholeanswer, independentjudgment/limitations. Метод frozencheck не меняется: BROWSER — фактический UI, LIVE — настоящийmodel/MCP/service/Telegram, неstub. Oneproof может закрыватьнесколькоcriteriaсexplicitmapping. Fullintegration включает Word/video/publication/completion.

D33-05/07/08: независимое чтение целых ответов обеихмоделей поpositive/negativegroundtruth. JSON/citations/HTTPsuccess не semanticPASS; syntheticcase не реальнаядиагностикааккаунта. ProductqualityNOT_ASSESSED сохраняется, independenttestjudgment отдельно. Ошибочныеответы нестереть/groundtruth неослабить радиPASS.

D33-01 actualtwoentries иshutdownownedMainwithSupporthealthFAQcreatealive. D33-04 currentJSONMCPreadback новогоticket иfollowupversion, неstaticseed. D33-06 forgedIDs всех relevantAPI/tool иUIepoch; demo selector неproductionauth. D33-08/11 model/MCPdown оставляетmanualflow.

D33-09 требуетadversarialnoncooperativelatecompletionoffline иactualinflightbrowserStop. D33-10 acknowledgement/messageID, dedup/uncertain неровно«отправлено». D33-16 finalcompletion — однаотдельнаяотправкапослеfullrequirements, нетестовоесообщение.

## Specification gate

Independent static Tester доproductcode проверяет fulloriginalscope/frozen16criteria, согласованность3docs, readySpecification, actualSDK/entrypointfeasibility, JSONbridgefreshness, scope/secrecy/state/version/runtime, sufficientLIVE/BROWSER/negativechecks и artifactformat. Review hashes всех3docs. Авторdesign неself-accept; staticPASS разрешаетследующуюстадию, неD33productAccepted.