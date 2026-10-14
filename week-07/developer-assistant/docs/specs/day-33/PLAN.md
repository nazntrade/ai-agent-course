# D33 — План согласованной архитектуры

PLAN_STATUS: READY_FOR_INDEPENDENT_REVIEW
SPEC_GATE_STATUS: NOT_ASSESSED

## До кода

1. Coordinator фиксирует полный frozen D33-01..16 и clean baseline. Specification preflight ready=true; Architect изучил actual launch/config/provider/runtime/MCP/RAG. Эти документы — дизайн, не реализация/PASS.
2. Independent static Tester проверяет полный scope и уточнения, согласованность SPEC/PLAN/ACCEPTANCE, SDK/JSONbridge/demo scope, checkmethods/artifacts. Review привязан к hashes всех трёх файлов. Исправления документов до кода.
3. После PASS Coordinator регистрирует последовательные подзадачи по зависимостям; Implementation readiness SpecPath docs/specs/day-33. User«Выполни» авторизует полный цикл без нового подтверждения. Одинwriter; protectedbat/rules/permissions не меняются. После registeredunit другойArchitect post-review и независимыйTester, затем fullintegration.

## A. Сервис/сохранение/bridge — D33-01,02,06,11,13

- Support mode в assistant.launch, unchanged run_app.bat/defaultMain, separateprocess/default8793, bounded mode/port/runtime ownership.
- Standard Main coldstart ensure/reuse independently surviving Support process by verifiedhealth; no modelcall/unknownlistener replacement, one SupportDBwriter.
- Scoped demo sessions, separate SupportStore users/tickets/messages/runs/deliveries/versions. Idempotentseed без историиoverwrite.
- Small real IMPLEMENTATION preflight: Support existingentrypoint рядомMain, isolateddata/health/create безkeys/model, SDKhandshake. Не являетсяfinalacceptance.
- Atomic current JSONsnapshot для новых/обновлённых обращений; owner/version confirmed, bounds/filtering, nosecretdata.
- Offline scope/IDOR, idempotentseed/restart, JSONschema/currentversion, expected_versionrace и ticketstatemeaning.

## B. MCP/RAG/AI/lifecycle — D33-03..09,11

- Separate real SDKMCPserver/scopedclient, tools/list+call, actualJSONreadback/version/hash, foreign/missing/malformed/unavailable errors.
- FAQ/docs allowlist, independentatomicFTS, snippets/BM25/русские aliases. Groundtruths отдельно отproductprompt/FAQ.
- SupportService one worker, bounded evidence, one generationround chosen existingProvider. Safe citations/final answer, no mutationtools/hiddenfallback.
- Queue/Stop/latecallback/versionguards/interruptedrestart, private reasoning excluded; NOT_ASSESSEDquality, completed неresolved.
- Offline injection/nocontext/truncation/version/noncooperativelatecompletion; realprovider checks послеstaticgates.

## C. Переписка/два входа — D33-01,02,03,06,07,09,11,12

- English Support Desk: ticketssidebar, chat/composer, Newticket/manualFAQ/SendAI понятны, sources/contextdetails, provider/Stop/Resolve/reopen.
- Main simpleSupportlink кsame independentservice. No copiedtickets/proxy/chattextinURL.
- UIepoch/selectionguards, waiting/streaming/safemodelrender, reload/personaswitch/history.
- BROWSER bothentries/newticket/followup/reload/restart/currentMCPsources; остановитьтолькоownedMain, SupportFAQ/form/createavailable, restoreMainforregression.

## D. Явные Telegram операции — D33-10

- Fixedownersummary/escalation с persistedintent/dedup/deliveryack отдельным отticketstate.
- Offline duplicate/uncertain/interrupted/stale/failed/cancelled. Одинboundedrealreport/escalation, messageID+duplicateaction; нерассылатьмассовыетесты.

## E. Независимая интеграция/регрессия — D33-01..13

- RealLocal/DeepSeek identicalsamequestionfixtures: invalid_key401, rate_limit403, Localrefused, missingfacts. Independentwholeanswerreviewagainstfacts/FAQ/attempts, неJSONформат.
- Newcreatedticket и followup получаютnewJSONMCPversion, неseedonly. Сохранитьalias/route/params/sourceversion/fullsafeanswer/elapsedusage/limitations.
- ДругойArchitect currentpostreview, developerfixes +meaningfulreproduction/regression, безreviewunchangedloops.
- Trustedtest.bat full/affectedoffline; Mainhelp/MCPbranch/history/PRreview representative BROWSER/LIVE, SupportclosepreservesborrowedLocal. Negativeproof нестеретьдоactualretest.

## F. Сдача/публикация — D33-14..16

- Actualscreenshots/recording, newdetailedDOCX и1920x1080MP4, oldartifactsunchanged. GitHubroletruthful:codepublication/D32CI, noD33questiontrigger.
- QAallDOCXpages/videoReadability/fullDecode/timeline; hashcopyoutputs. Independentartifactjudgment, нефайлыесть=PASS.
- Rootpublishauthorizedproduct/spec/tests/docs/sanitizedproofafterprivacyreview; remoteHEAD/fulltrackeduntrackedaccounted.
- OnefinalcompletionTelegramafteroutputs/independentacceptance/publication, receiptwithoutkeys/ownerID. Final16criteria, measuredlimitsvisible.

## Evidence

Artifacts конкретно объявлены в ACCEPTANCE; неDB/keys. Full runtime/snapshot ignored, publicsummaryrelative/sanitized. Evidence methods STATIC/OFFLINE/LIVE/BROWSER/ARTIFACT with version/provider/observedresult/independentjudgment. Passingunit/preflight неготовностьD33.