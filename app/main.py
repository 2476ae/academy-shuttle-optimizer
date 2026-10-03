"""HTTP API and pages for the shuttle service.

Run with ``python -m app`` (or ``uvicorn app.main:app``) and open
http://localhost:8000.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import socket
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .service import NOTE_MAX, ServiceError, Settings, ShuttleService

STATIC = Path(__file__).parent / "static"
COOKIE = "shuttle_session"
COOKIE_DAYS = 180
OPEN_API = {"/api/login", "/api/logout", "/api/session"}
MAX_FAILURES = 10  # wrong codes per address ...
FAILURE_WINDOW = 600  # ... per this many seconds


class NewRequest(BaseModel):
    branch: str = Field(examples=["A"])
    dest: str = Field(examples=["D1"])
    count: int = Field(ge=1, le=20, examples=[2])
    ready_in: float = Field(default=0, ge=0, le=60, description="몇 분 뒤에 준비되는지")
    note: str = Field(default="", max_length=200, description=f"선택 메모 (앞 {NOTE_MAX}자만 저장)")


class DriverMove(BaseModel):
    to: str | None = None  # depart: go somewhere other than the suggested stop
    at: str | None = None  # arrive: ended up somewhere other than planned


class DriverBoard(BaseModel):
    boarded: list[str] = Field(default_factory=list, description="실제로 탄 요청")


class DriverLocate(BaseModel):
    stop: str


class Login(BaseModel):
    code: str = Field(max_length=100)


class DemoReset(BaseModel):
    policy: str | None = None
    speed: float | None = Field(default=None, gt=0, le=600)
    demo_requests: bool | None = None
    manual: bool | None = None
    seed: int | None = None
    start: str | None = Field(default=None, pattern=r"^\d{1,2}:\d{2}$")


def lan_address() -> str | None:
    """This computer's address on the local network (no packets are sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
    except OSError:
        return None
    return None if ip.startswith("127.") else ip


def session_token(code: str) -> str:
    """Cookie value that proves the academy code was entered on this device."""
    return hmac.new(code.encode("utf-8"), b"shuttle-staff-v1", hashlib.sha256).hexdigest()


def create_app(service: ShuttleService | None = None, tick_seconds: float = 0.5) -> FastAPI:
    svc = service or ShuttleService(Settings.from_env())
    failures: dict[str, list[float]] = {}

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

    def signed_in(request: Request) -> bool:
        code = svc.settings.access_code
        if not code:
            return True
        token = request.cookies.get(COOKIE, "")
        return hmac.compare_digest(token, session_token(code))

    @app.middleware("http")
    async def require_code(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and path not in OPEN_API and not signed_in(request):
            return JSONResponse({"detail": "학원 코드를 먼저 입력해 주세요."}, status_code=401)
        return await call_next(request)

    @app.exception_handler(ServiceError)
    async def service_error(_: Request, exc: ServiceError):
        return JSONResponse({"detail": exc.message}, status_code=exc.status)

    # ----------------------------------------------------------------- access

    @app.get("/api/session")
    def session(request: Request):
        return {"required": bool(svc.settings.access_code), "ok": signed_in(request)}

    @app.post("/api/login")
    def login(body: Login, request: Request):
        code = svc.settings.access_code
        if not code:
            return {"ok": True}
        who = request.client.host if request.client else "?"
        now = time.time()
        recent = [t for t in failures.get(who, []) if now - t < FAILURE_WINDOW]
        if len(recent) >= MAX_FAILURES:
            failures[who] = recent
            raise ServiceError("코드를 너무 많이 틀렸습니다. 10분 뒤에 다시 시도해 주세요.", 429)
        if not hmac.compare_digest(body.code.strip().encode("utf-8"), code.encode("utf-8")):
            failures[who] = [*recent, now]
            raise ServiceError("학원 코드가 맞지 않습니다.", 401)
        failures.pop(who, None)
        res = JSONResponse({"ok": True})
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        res.set_cookie(COOKIE, session_token(code), max_age=COOKIE_DAYS * 86400, httponly=True, samesite="lax", secure=secure)
        return res

    @app.post("/api/logout")
    def logout():
        res = JSONResponse({"ok": True})
        res.delete_cookie(COOKIE)
        return res

    # ------------------------------------------------------------------ API

    @app.get("/api/config")
    def config():
        return svc.config()

    @app.get("/api/state")
    def state():
        return svc.snapshot()

    @app.get("/api/setup-hints")
    def setup_hints(request: Request):
        ip = lan_address()
        port = request.url.port
        return {"lan": [f"http://{ip}:{port}" if port else f"http://{ip}"] if ip else []}

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
        return svc.create_request(body.branch, body.dest, body.count, body.ready_in, body.note)

    @app.post("/api/requests/{rid}/cancel")
    def cancel_request(rid: str):
        return svc.cancel_request(rid)

    @app.post("/api/driver/depart")
    def driver_depart(body: DriverMove | None = None):
        svc.driver_depart((body or DriverMove()).to)
        return svc.snapshot()

    @app.post("/api/driver/arrive")
    def driver_arrive(body: DriverMove | None = None):
        svc.driver_arrive((body or DriverMove()).at)
        return svc.snapshot()

    @app.post("/api/driver/board")
    def driver_board(body: DriverBoard):
        svc.driver_board(body.boarded)
        return svc.snapshot()

    @app.post("/api/driver/locate")
    def driver_locate(body: DriverLocate):
        svc.driver_locate(body.stop)
        return svc.snapshot()

    @app.post("/api/demo/reset")
    def demo_reset(body: DemoReset):
        if svc.settings.mode != "demo":
            raise ServiceError("실제 운행 중에는 처음부터 다시 시작할 수 없습니다.", 409)
        svc.reset(**body.model_dump())
        return svc.snapshot()

    # ---------------------------------------------------------------- pages

    def page(name: str):
        """Endpoint serving one fixed HTML file (no parameters, so nothing in the URL picks the file)."""

        def endpoint():
            return FileResponse(STATIC / name, media_type="text/html", headers={"Cache-Control": "no-cache"})

        return endpoint

    for route, name in {
        "/": "index.html",
        "/teacher": "teacher.html",
        "/driver": "driver.html",
        "/board": "board.html",
        "/login": "login.html",
        "/setup": "setup.html",
    }.items():
        app.add_api_route(route, page(name), include_in_schema=False)

    @app.get("/manifest.webmanifest", include_in_schema=False)
    def manifest():
        return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return FileResponse(STATIC / "icon.svg", media_type="image/svg+xml")

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()
