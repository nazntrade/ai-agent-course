"""``python -m agent`` entry point: serve the backend on the loopback host.

A busy backend port is a prerequisite error (exit code 2) and the process that
owns the port is never touched.
"""

from __future__ import annotations

import sys

import uvicorn

from agent.server import create_app, port_is_available
from agent.settings import load_settings

EXIT_OK = 0
EXIT_PREREQUISITE = 2


def main(argv=None) -> int:
    """Start the backend with uvicorn."""
    settings = load_settings()
    if not port_is_available(settings.backend_host, settings.backend_port):
        print(
            f"Backend error: {settings.backend_host}:{settings.backend_port} "
            "is already in use. Stop the process that owns the port or change "
            "BACKEND_PORT.",
            file=sys.stderr,
            flush=True,
        )
        return EXIT_PREREQUISITE
    app = create_app(settings)
    uvicorn.run(
        app,
        host=settings.backend_host,
        port=settings.backend_port,
        log_level=settings.log_level.lower(),
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
