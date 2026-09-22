"""Write the OpenAPI 3.1 snapshot of the backend into ``docs/openapi.json``.

The snapshot is the committed contract of the HTTP API. ``test.bat openapi``
regenerates it; the drift test compares a structural skeleton of the generated
specification with the committed file, so a real contract change is caught.
"""

from __future__ import annotations

import json
from pathlib import Path

from agent.server import create_app
from agent.settings import PROJECT_ROOT, load_settings

SNAPSHOT_PATH = PROJECT_ROOT / "docs" / "openapi.json"

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "options", "head")


def generate_spec() -> dict:
    """Build the OpenAPI document of the application."""
    return create_app(load_settings()).openapi()


def skeleton(spec: dict) -> dict:
    """Return the structural, order-independent summary of a specification."""
    paths = {}
    for path, operations in (spec.get("paths") or {}).items():
        paths[path] = sorted(
            method.lower() for method in operations if method.lower() in HTTP_METHODS
        )
    info = spec.get("info") or {}
    return {
        "openapi": spec.get("openapi"),
        "info": {"title": info.get("title"), "version": info.get("version")},
        "paths": paths,
        "schemas": sorted((spec.get("components") or {}).get("schemas", {}).keys()),
    }


def write_snapshot(path=SNAPSHOT_PATH) -> Path:
    """Write the generated specification with a trailing newline."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(generate_spec(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def main(argv=None) -> int:
    """Entry point of ``test.bat openapi``."""
    target = write_snapshot()
    print(f"OpenAPI snapshot written: {target.name}")
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
