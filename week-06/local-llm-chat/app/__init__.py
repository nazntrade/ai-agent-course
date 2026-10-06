"""local-llm-chat: D26 chat with local Gemma 12B and network DeepSeek.

The package keeps the week-05 separation of concerns: provider adapters,
local-process ownership, context building, RAG retrieval and dialogue storage
are isolated from the API/UI layer.
"""

__version__ = "0.1.0"
