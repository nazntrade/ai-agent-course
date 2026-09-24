"""Shared SQLite persistence for chats, messages, scheduled tasks and runs.

The package is pure standard library and is imported by both the FastAPI
backend and the MCP server process, which share one database file. Business
limits stay in the service layer (``agent.chats`` / ``mcp_server.tasks``); the
repositories here only read and write rows.
"""
