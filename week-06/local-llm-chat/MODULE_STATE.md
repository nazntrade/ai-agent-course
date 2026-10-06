# Состояние модуля `local-llm-chat`

## Цель

Текущая цель — реализовать приложение D26 (`app/`, `requirements.txt`, harness/тесты) и провести LIVE-проверку после настройки точек запуска и утверждения спецификации.

## Статус

Подготовка завершена: точки запуска настроены (GOVERNANCE E1), спецификация утверждена; далее IMPLEMENTATION.

## Сделано

- Каркас CourseModule создан штатным генератором `New-ProjectFromTemplate.ps1`; `-Stage Scaffold` — PASS.
- Заполнен `MODULE_RULES.md` (разделы 1–4, 8–12, 14–18, test-runtime-json) и `MODULE_STATE.md`.
- Созданы SPEC/PLAN/ACCEPTANCE (`docs/specs/day-26-local-llm/`); SPEC_GATE_STATUS и ARCHITECTURE_REVIEW — PASS.
- GOVERNANCE E1: четыре bat настроены по PLAN, заглушки `CONFIGURE_ME` удалены; RAG-режим закреплён как включаемый (правка правил/состояния модуля).

## Далее

- Реализация приложения (PRODUCT-стадия, IMPLEMENTATION): `app/`, `requirements.txt`, harness/тесты.
- LIVE-проверка реальной Gemma и DeepSeek через настроенные bat (PLAN E8).

## Открытые вопросы

- Точные версии зависимостей фиксируются в `requirements.txt` на этапе реализации.
- Конкретный сетевой провайдер DeepSeek и параметры локальной модели уточняются на стадии реализации.

## Известные проблемы

Известные ограничения перечислены в `MODULE_RULES.md` (раздел 18). Учебные: доступность сетевого провайдера и тип локальной модели зависят от окружения.

## Последняя фактическая проверка

`Test-ProjectBootstrap.ps1 -Stage Scaffold` — PASS (подтверждено пользователем). `-Stage Specification` до заполнения давал `MODULE_RULES.md не заполнен`. Финальную `-Stage Specification` выполняет независимый Tester.

## Решения

- Стек наследуется из `week-05/knowledge-agent`: Python + FastAPI + Pydantic + SQLite, статический HTML/JS UI, Pytest.
- RAG и инструменты знания (индексация, поиск, фильтрация, query rewrite, источники, цитаты) переносятся из `week-05` и сохраняются как отдельный включаемый RAG-режим; по умолчанию чат отвечает без RAG (SPEC R1.3, R5.1, R5.2).
- AI-runtime: applicability=ai, profileRole=chat, provider=openai-compatible (по контракту `test-runtime-json`).
