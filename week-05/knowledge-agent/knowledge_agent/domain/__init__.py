"""Domain layer: data models, error taxonomy and abstract contracts.

This package is the stable core. It must not import SQLite, Ollama, PDF or
HTTP libraries; external details depend on these contracts, never the reverse.
"""
