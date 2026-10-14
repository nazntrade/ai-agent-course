# Tickets and support conversations

Support is an independent local service. Open it directly or use Support in the
main app. Both entries use the same support data, tickets and conversation.
The main app may be stopped while Support FAQ and the ticket form still work.
If both web services are stopped, the browser cannot reach either service.

## Synthetic users and context

All demo personas, plans and diagnostic examples are fictional. Choosing a
persona is a local demonstration, not production login or tenant security.
Within a selected demo session the service scopes tickets, answers, sources
and notifications to that persona. JSON snapshots are refreshed for each new
answer and read through an actual read-only MCP server. Support does not
inspect your computer, logs, private repository or real account automatically.

## Save, ask, stop and resolve

Save a new request without invoking AI. Ask AI explicitly with Local or
DeepSeek for a contextual answer. Follow-ups remain in the same conversation.
Stop cancels the active answer; a late result cannot resurrect it. An unfinished
request is marked interrupted after restart and is not retried automatically.
Receiving an answer does not resolve the problem. Confirm resolved only after
checking the proposed steps; Reopen permits another follow-up.

## Telegram and escalation

Escalate saves the ticket state without resolving it. Sending a summary to the
configured owner in Telegram is a separate explicit action. Only the saved
owner can receive it; neither the model nor a ticket chooses a recipient.
A current completed answer may be sent explicitly. Delivery is sent only after
Telegram acknowledges a message ID. Failed or uncertain delivery is displayed
honestly; an uncertain action is not automatically resent, avoiding duplicates.
No key, contact information or full log should be included in a summary.
