"""Maintenance scenario: regenerate the committed OpenAPI snapshot offline.

The API surface changed with the additive D25 dialogue routes, so the generated
``knowledge_agent/api/openapi.json`` snapshot must be regenerated from the app
factory. This scenario performs no network or model call and is run through the
trusted ``test.bat scenario`` dispatcher.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from knowledge_agent.api.app import create_app
from tests.helpers import make_service

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    with tempfile.TemporaryDirectory() as temp:
        service, store = make_service(Path(temp))
        try:
            app = create_app(service, ui_dir=Path(temp) / "no-ui")
            schema = TestClient(app).get("/openapi.json").json()
        finally:
            store.close()
    snapshot = ROOT / "knowledge_agent" / "api" / "openapi.json"
    snapshot.write_text(
        json.dumps(schema, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    print("OPENAPI_SNAPSHOT: written")
    return 0


raise SystemExit(main())
