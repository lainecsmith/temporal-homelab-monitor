"""HTTP API for the dashboard. Run from the repo root:

    uv run uvicorn dashboard.app:app --port 8000   # then open http://localhost:8000

Binds to loopback by default. There is no authentication yet, and the signal endpoints
act on your hosts, so don't expose this to a network you don't trust."""
import asyncio
import os
from contextlib import asynccontextmanager, suppress
from pathlib import Path as FsPath

from fastapi import FastAPI, HTTPException, Path, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from common import CONTROLLER_QUEUE
from dashboard.errors import GatewayError
from dashboard.gateway import TemporalGateway
from dashboard.service import Fleet
from dashboard.store import Store

HOST = Path(pattern=r"^[a-z0-9][a-z0-9._-]{0,62}$")
STATIC = FsPath(__file__).with_name("static")


def build_fleet() -> Fleet:
    gateway = TemporalGateway(
        os.getenv("TEMPORAL_ADDRESS", "localhost:7233"),
        os.getenv("TEMPORAL_NAMESPACE", "default"),
        query_timeout_s=float(os.getenv("QUERY_TIMEOUT_S", "5")))
    return Fleet(
        gateway, Store(os.getenv("DASHBOARD_DB", "dashboard.sqlite3")), CONTROLLER_QUEUE,
        interval_s=float(os.getenv("POLL_INTERVAL_S", "15")),
        poller_max_age_s=float(os.getenv("CONTROLLER_POLLER_MAX_AGE_S", "60")))


def create_app(fleet: Fleet = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.fleet = fleet or build_fleet()
        poller = asyncio.create_task(app.state.fleet.run_forever())
        yield
        poller.cancel()
        with suppress(asyncio.CancelledError):
            await poller

    app = FastAPI(title="Homelab monitor", lifespan=lifespan)

    def fl(request: Request) -> Fleet:
        return request.app.state.fleet

    def http_error(e: GatewayError) -> HTTPException:
        status = {"not_found": 404, "invalid": 400}.get(e.kind, 503)
        return HTTPException(status, str(e))

    @app.get("/", include_in_schema=False)
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/health")
    async def health(request: Request):
        return fl(request).health

    @app.get("/api/hosts")
    async def hosts(request: Request):
        return fl(request).view()

    @app.get("/api/hosts/{host}")
    async def host(request: Request, host: str = HOST):
        found = fl(request).host_view(host)
        if found is None:
            raise HTTPException(404, "Unknown host.")
        return found

    @app.post("/api/poll")
    async def poll(request: Request):
        """Run a poll now instead of waiting for the next one. The page uses this for Retry
        and to pick up fresh data after a signal."""
        await fl(request).poll_once()
        return fl(request).view()

    @app.post("/api/hosts/{host}/refresh", status_code=202)
    async def refresh(request: Request, host: str = HOST):
        try:
            return await fl(request).signal(host, "force_refresh")
        except GatewayError as e:
            raise http_error(e)

    @app.post("/api/hosts/{host}/check-updates", status_code=202)
    async def check_updates(request: Request, host: str = HOST):
        try:
            return await fl(request).signal(host, "check_updates")
        except GatewayError as e:
            raise http_error(e)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    return app


app = create_app()
