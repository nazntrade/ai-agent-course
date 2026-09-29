"""FastAPI application factory (SPEC 11)."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..domain.errors import KnowledgeError
from ..service.knowledge_service import KnowledgeService
from .routes import create_router


def create_app(
    service: KnowledgeService,
    *,
    ui_dir: str | Path | None = None,
    title: str = "Knowledge Agent",
) -> FastAPI:
    app = FastAPI(
        title=title,
        version=__version__,
        description="Local document indexing and fragment search (Day 21).",
    )
    app.include_router(create_router(service))

    @app.exception_handler(KnowledgeError)
    async def knowledge_error_handler(_: Request, exc: KnowledgeError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"error": exc.to_dict()})

    @app.exception_handler(KeyError)
    async def key_error_handler(_: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse(
            status_code=404,
            content={
                "error": {
                    "code": "not_found",
                    "message": "The requested resource was not found.",
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Never echo raw input/ctx: validation errors can carry absolute paths
        # or other sensitive payload fragments (invariant I7).
        errors = [
            {
                "type": error.get("type"),
                "loc": list(error.get("loc", ())),
                "msg": error.get("msg"),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "The request payload is invalid.",
                    "details": {"errors": errors},
                }
            },
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={
                "error": {
                    "code": "internal_error",
                    "message": f"Unexpected server error ({type(exc).__name__}).",
                }
            },
        )

    static_dir = Path(ui_dir) if ui_dir else Path(__file__).resolve().parent.parent / "ui"
    if static_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(static_dir)), name="assets")

        @app.get("/", include_in_schema=False)
        def ui_index() -> FileResponse:
            return FileResponse(static_dir / "index.html")

        @app.get("/index.html", include_in_schema=False)
        def ui_index_file() -> FileResponse:
            return FileResponse(static_dir / "index.html")

    return app
