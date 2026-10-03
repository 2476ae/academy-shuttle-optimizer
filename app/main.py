"""HTTP API and pages for the shuttle service.

Run with ``python -m app`` (or ``uvicorn app.main:app``) and open
http://localhost:8000.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .service import ServiceError, Settings, ShuttleService

STATIC = Path(__file__).parent / "static"


class NewRequest(BaseModel):
    branch: str = Field(examples=["A"])
    dest: str = Field(examples=["D1"])
    count: int = Field(ge=1, le=20, examples=[2])
    ready_in: float = Field(default=0, ge=0, le=60, description="몇 분 뒤에 준비되는지")


class DemoReset(BaseModel):
    policy: str | None = None
    speed: float | None = Field(default=None, gt=0, le=600)
    demo_requests: bool | None = None
    manual: bool | None = None
    seed: int | None = None
    start: str | None = Field(default=None, pattern=r"^\d{1,2}:\d{2}$")


def create_app(service: ShuttleService | None = None, tick_seconds: float = 0.5) -> FastAPI:
    svc = service or ShuttleService(Settings.from_env())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async def ticker():
            while True:
                svc.tick()
                await asyncio.sleep(tick_seconds)

        task = asyncio.create_task(ticker())
        try:
            yield
        finally:
            task.cancel()

    app = FastAPI(title="하원 셔틀", lifespan=lifespan)
    app.state.service = svc

    @app.exception_handler(ServiceError)
    async def service_error(_: Request, exc: ServiceError):
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    # ------------------------------------------------------------------ API

    @app.get("/api/config")
    def config():
        return svc.config()

    @app.get("/api/state")
    def state():
        return svc.snapshot()

    @app.get("/api/stream")
    async def stream(request: Request):
        async def events():
            last = None
            while not await request.is_disconnected():
                payload = json.dumps(svc.snapshot(), ensure_ascii=False)
                if payload != last:
                    last = payload
                    yield f"data: {payload}\n\n"
                await asyncio.sleep(1.0)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/requests", status_code=201)
    def create_request(body: NewRequest):
        return svc.create_request(body.branch, body.dest, body.count, body.ready_in)

    @app.post("/api/requests/{rid}/cancel")
    def cancel_request(rid: str):
        return svc.cancel_request(rid)

    @app.post("/api/driver/depart")
    def driver_depart():
        svc.driver_depart()
        return svc.snapshot()

    @app.post("/api/driver/arrive")
    def driver_arrive():
        svc.driver_arrive()
        return svc.snapshot()

    @app.post("/api/demo/reset")
    def demo_reset(body: DemoReset):
        if svc.settings.mode != "demo":
            raise ServiceError("실제 운행 중에는 처음부터 다시 시작할 수 없습니다.", 409)
        svc.reset(**body.model_dump())
        return svc.snapshot()

    # ---------------------------------------------------------------- pages

    def page(name: str):
        return FileResponse(STATIC / name, media_type="text/html")

    @app.get("/", include_in_schema=False)
    def index():
        return page("index.html")

    @app.get("/teacher", include_in_schema=False)
    def teacher():
        return page("teacher.html")

    @app.get("/driver", include_in_schema=False)
    def driver():
        return page("driver.html")

    @app.get("/board", include_in_schema=False)
    def board():
        return page("board.html")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return FileResponse(STATIC / "icon.svg", media_type="image/svg+xml")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()
