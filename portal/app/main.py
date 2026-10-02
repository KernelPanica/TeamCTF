from contextlib import asynccontextmanager
from contextlib import suppress
import asyncio
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from .database import make_engine
from .access import router as access_router
from .lobby import router as lobby_router
from .arena_provider import configured_provider
from .arena_reconcile import reconcile_forever


def create_app(database_url=None, arena_provider=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.engine = make_engine(database_url)
        try:
            if arena_provider is not None:
                app.state.arena_provider = arena_provider
                yield
            else:
                async with configured_provider() as provider:
                    app.state.arena_provider = provider
                    task = asyncio.create_task(reconcile_forever(app.state.engine, provider)) if provider else None
                    try:
                        yield
                    finally:
                        if task:
                            task.cancel()
                            with suppress(asyncio.CancelledError):
                                await task
        finally:
            app.state.engine.dispose()

    app = FastAPI(lifespan=lifespan)
    app.include_router(access_router)
    app.include_router(lobby_router)
    frontend = Path(__file__).resolve().parents[1] / "frontend"
    app.mount("/static", StaticFiles(directory=frontend), name="static")

    @app.get("/", include_in_schema=False)
    def lobby():
        return FileResponse(frontend / "index.html")

    @app.get("/health")
    def health():
        try:
            with app.state.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        return {"status": "ok"}

    return app


app = create_app()
