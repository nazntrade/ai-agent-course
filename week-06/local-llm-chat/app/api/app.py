"""FastAPI application factory (SPEC 8.1)."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..errors import AppError
from .routes import create_router


def create_app(service: object, *, ui_dir: str | Path | None = None, title: str = "Local LLM Chat") -> FastAPI:
    app = FastAPI(title=title, version=__version__, description="D26 local Gemma / network DeepSeek chat.")
    app.include_router(create_router(service))

    @app.exception_handler(AppError)
    async def app_error_handler(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"error": exc.to_dict()})

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        # Never echo raw input: it can carry absolute paths or secret fragments.
        errors = [
            {"type": e.get("type"), "loc": list(e.get("loc", ())), "msg": e.get("msg")}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={"error": {"code": "invalid_request", "message": "The request payload is invalid.", "details": {"errors": errors}}},
        )

    @app.exception_handler(Exception)
    async def unexpected_error_handler(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={"error": {"code": "internal_error", "message": f"Unexpected server error ({type(exc).__name__})."}},
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
