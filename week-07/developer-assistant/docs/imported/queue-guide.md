# Queue guide

Developer Assistant runs one task at a time using a sequential worker.
If the first task is running, the second task remains queued.
Stop on a queued task cancels that task without stopping another running task.
Retry creates a new task identifier and preserves the original run in history.

This document is a small project handbook imported through the D31 interface.
