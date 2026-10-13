# D32 — План реализации

PLAN_STATUS: READY_FOR_INDEPENDENT_REVIEW

## Последовательность и зависимости

1. SPEC gate. Независимый Tester статически сверяет SPEC/PLAN/ACCEPTANCE с официальным заданием и владельцем; сохраняет заключение. Coordinator фиксирует полный контракт, делит работу и выполняет Implementation preflight. До этого нет реализации и LIVE.
2. Рискованные границы (D32-01, 07, 09). После gate маленький реальный preflight: GitHub repository/Actions доступны; application-owned проверка выбранных Local/DeepSeek; безопасная возможность установить CI secrets и разрешённый publisher. Ничего из .env не читать/выводить. Заранее обозначить, что публикация реализации в base нужна до демонстрационного PR. Не превращать отсутствующий cloud secret в «локальная проверка достаточно».
3. Slice A — collector + snapshot corpus (D32-02,03,08). Реализовать GitHub metadata/files/diff fixed base/head, ограничения, coverage manifest и отдельный docs+code FTS5 corpus. Offline fixtures: multi-page files, deletion/rename, binary/large patch, excluded secrets, related function/API rules и несовпадение docs/head.
4. Slice B — один review engine и CLI (D32-04,05,06,10). Использовать существующий Provider без tool permissions и implicit fallback; JSON findings contract, coordinates/source validation, категории Markdown, explicit failure/partial, usage/time, bounded units/retry. Golden known bugs + benign/injection cases, сохранённый corpus provenance.
5. Slice C — GitHub automation (D32-01,07,08,09). Workflow pull_request_target opened/synchronize/reopened из trusted base, checkout точного event base SHA для движка и dependencies, минимальные contents:read/pull-requests:write permissions, concurrency, DeepSeek secret из Actions environment, CLI, artifacts/job summary и один head-labelled comment. Fork явно skip до secret-dependent шагов. Head только bounded API text; не checkout/import/install/run head scripts/dependencies и не брать workflow из head. PR title/body/path читаются из event JSON/валидированного API payload, без shell run interpolation. Подготовить neutral README setup инструкции с понятными secret names, без значений.
6. Slice D — chat UI/import/manual review (D32-11,12). Подключить repository/PR list/Sync к проекту, review provider Local/DeepSeek, report cards/history/details/source view и Stop. Источник CI/manual и stale/partial различимы. Bounded polling не порождает модельные вызовы и повторные сообщения. Старые D31 features проходят затронутую регрессию.
7. Slice E — owner notification (D32-13). Только configured Telegram owner, saved validated result, short summary/link/status и dedup across CI/import; delivery uncertainty видна. Авторизованное небольшое LIVE уведомление с безопасным текстом и реально проверенным ответом Telegram.
8. Architect post-review актуальных продуктовых изменений; затем независимый Tester offline/LIVE/browser критериев. Реально выполнить known-bug и safe-case на DeepSeek и Local; независимая содержательная оценка, никаких fake PASS. Без конфликта writers исправлять обнаруженные проблемы и повторять только необходимые проверки.
9. Публикационная граница. Основной исполнитель после разрешённых проверок публикует базовую реализацию. Затем отдельная demo branch/PR меняет исключительно безопасный учебный fixture; настоящий opened Actions run возвращает review. Исправляющий commit вызывает synchronize rerun; отмена старого head/обновление одного comment и import проверяются. Test evidence для ещё непроверенного CI сохраняется NOT_ASSESSED до реального run; базовая публикация сама не означает полную сдачу.
10. Интеграционная приёмка D32-01..16 и regression D31. Проверить настоящие Actions logs/artifacts/comment, оба providers, UI и owner Telegram. Полнота исходного задания обязательна; все blockers/partial сохранять.
11. Word + видео (D32-14,15). Скриншоты фактических GitHub/UI/results; Word понятным языком от theory к точной реализации и weekly notes. Видео real landscape 1920x1080 MP4 с подробными объясняющими captions; открыть результат и проверить читаемость/полноту. Если меняется UI/flow после записи — заменить устаревшие материалы.
12. Финал (D32-16,13). Проверить публичные staged files и отсутствие секретов/частных данных, commit/push проверенные оставшиеся изменения; remote + full tracked/untracked audit. Основной исполнитель sends authorized honest completion Telegram once. Финальный отчёт перечисляет результаты независимой приёмки и пути к документу/видео.

## Предлагаемые точки продукта

- assistant/github_review.py, assistant/review.py, assistant/review_cli.py — небольшой независимый engine и adapters.
- Store/Service/API — persistence и единый worker/cancellation; таблицы или payload поля для repository, review jobs, manifests и deliveries.
- Новые API располагаются рядом с текущими /api/projects/... и /api/tasks/...; строгие Pydantic bodies, provider enum local/deepseek, никакого shell/url произвола.
- Один .github/workflows/day32-review.yml; pull_request_target trigger из trusted base, executable CLI/dependencies по exact base SHA, параметры repository/PR/SHA из проверенного event JSON/API metadata, без прямой shell-интерполяции PR полей.
- README.md, docs/architecture.md, docs/api.md и docs/data-schema.json дополняются фактическим D32 поведением. Старые доказательства D31 не переписываются.
- tests — offline fixtures и meaningful regression; сценарии LIVE/UI сохраняют обезличенный evidence. test.bat остаётся обычной доверенной точкой; новые постоянные dispatcher/governance записи только через отдельный разрешённый маршрут, если они действительно нужны.

## Проверки на каждом slice

Независимое ревью части не заменяет итоговую интеграцию. Каждый slice имеет requirements IDs, artifact/observed proof и отрицательный сценарий. Один активный продуктовый writer; архитектурный reviewer и Tester не меняют реализацию. Нет повторного полного аудита неизменённых файлов после каждого маленького протокола.

Golden set стартует с 10–12 небольших кейсов: неправильная арифметика скидки; API schema mismatch; cross-file caller mismatch; leakage project scope; late result after Stop; docs/source disagreement; harmless rename/refactor; docs-only benign change; prompt injection; bad citation; partial diff; unavailable provider. Reviewer заранее фиксирует ожидаемые существенные находки и допустимые uncertainties. Это учебная проверка достаточности, не гарантия безошибочности на любом PR.

## Риски и обязательная реакция

Cloud секреты/permissions: explicit prerequisite, безопасный application-owned setup либо точный blocker. Keyword RAG: representative retrieval evidence и honest missing context. Provider output: не обходить schema/output limits ради green. Public repo: все head-files untrusted, секреты доступны только trusted engine. CI Telegram ambiguity: persist/report uncertainty, no blind resend. GitHub updated head: cancelled/stale output остаётся историей, не current. Downloaded artifact: bounded archive member/path/schema validation либо import marked PR comment envelope, не распаковка arbitrary data.
