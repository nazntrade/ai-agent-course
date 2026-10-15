# TicketDesk

Учебный сервис обращений с in-memory состоянием. Это чистый Python контракт,
а не запущенный HTTP-сервер. `ticket_api.py` содержит актуальный dispatcher.

## Завершение обращения

Клиент отправляет POST `/api/tickets/{ticket_id}/close` с `note` и `version`.
Ожидаемый ответ содержит `id`, `state`, `note`, `version`.

См. `docs/API.md`, `PROJECT_GUIDE.md`, `RELEASE_NOTES.md` и contract tests.
CLI и view используют общий адаптер `ticket_client.py` для resolve.
Отдельные list/filter операции должны сохранить поведение.
