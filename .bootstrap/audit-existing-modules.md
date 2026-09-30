# Аудит существующих модулей по контракту bootstrap v1

Дата: 30 сентября 2026. Проверка полностью read-only; ни один файл опубликованных модулей не изменён, генератор к ним не применялся. Результаты получены доверенным `Test-ProjectBootstrap.ps1 -AuditExisting -TemplateType CourseModule` с курсовым корнем наследования.

| Модуль | Scaffold | Недостающие файлы относительно модуля |
| --- | --- | --- |
| `week-01/llm-api-project` | Не соответствует новому каркасу | `MODULE_RULES.md`, `MODULE_STATE.md`, `setup.bat`, `test.bat`, `smoke_test.bat` |
| `week-02/first-agent` | Не соответствует новому каркасу | `MODULE_RULES.md`, `MODULE_STATE.md`, `setup.bat`, `test.bat`, `smoke_test.bat` |
| `week-03/memory-state-agent` | Не соответствует новому каркасу | `MODULE_RULES.md`, `MODULE_STATE.md`, `setup.bat` |
| `week-04/day-16-mcp-agent` | Не соответствует новому каркасу | `MODULE_RULES.md`, `MODULE_STATE.md` |
| `week-05/knowledge-agent` | Готовность файлов подтверждена | Нет |

Дополнительно для `week-05/knowledge-agent` выполнен этап Implementation с `-SpecPath docs/specs/day-21-document-indexing`: `ready: true`, `issues: []`, `auditOnly: true`. Проверены наполненные правила, объявленный стек, наследование конфигурации/ролей, доступ к четырём точкам запуска, отсутствие `CONFIGURE_ME` и наличие непустого комплекта спецификации. Приложение и реальные API не запускались; независимая приёмка приложения, актуальный SPEC review и runtime-допуск этим аудитом не подтверждаются.

Старые модули внесены в явный список `legacyProjects` курсовой `.project-bootstrap-policy.json`. Для новых совпадений `week-*/*` требуется штатный marker и полный процесс. Результат аудита не регистрирует новый проект и не отменяет правила опубликованных этапов. В `MODULE_STATE.md` week 5 могут оставаться исторические сведения: аудит не переписывает отчёты прошлой работы.
