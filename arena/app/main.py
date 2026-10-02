import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from shared.arena import ArenaError
from .api.routes import router


def create_app(service=None, token=None):
    @asynccontextmanager
    async def lifespan(app):
        secret = token or os.environ.get("ARENA_TOKEN", "")
        if len(secret) < 32:
            raise RuntimeError("ARENA_TOKEN must contain at least 32 characters")
        app.state.token = secret
        if service is None:
            from .runtime.docker import DockerRuntime
            from .runtime.executor import Executor
            from .service import ArenaService
            from .runtime.firewall import verify
            verify(require_management=True)
            runtime = DockerRuntime(os.environ.get("ARENA_CASES", "/app/arena/cases"),
                owner=os.environ["ARENA_OWNER"], publish_ip=os.environ["ARENA_GAME_IP"],
                public_host=os.environ["ARENA_PUBLIC_HOST"],
                game_port_min=int(os.environ.get("ARENA_GAME_PORT_MIN", "30000")),
                game_port_max=int(os.environ.get("ARENA_GAME_PORT_MAX", "39999")),
                reserved_ports={int(p) for p in os.environ.get("ARENA_RESERVED_PORTS", "22,80,443").split(",") if p},
                management_port=int(os.environ.get("ARENA_MANAGEMENT_PORT", "8443")))
            app.state.service = ArenaService(Executor(runtime.cases_directory, runtime))
        else:
            app.state.service = service
        await app.state.service.start()
        try:
            yield
        finally:
            await app.state.service.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(router)

    @app.middleware("http")
    async def private_response(request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ArenaError)
    async def arena_error(request, error):
        return JSONResponse({"code": error.code}, status_code=error.status)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, error):
        # Pydantic's default error body can echo the submitted RED objective.
        return JSONResponse({"code": "INVALID_REQUEST"}, status_code=422)

    return app


app = create_app()
