# TicketDesk API

Справочник текущего учебного API; wire contract определяется dispatcher.

## Legacy resolve example

POST `/api/tickets/T-7/close`

```json
{"note":"Done", "version":4}
```

Успешный ответ: `id`, `state`, `note`, `version`.
Ошибки завершения и concurrency здесь пока не описаны.

## Consumers

Resolve использует общий ticket_client; ticket_cli и ticket_view передают
текст и показанную версию через него. Это функции без сетевого транспорта.
