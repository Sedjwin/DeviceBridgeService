"""DeviceBridgeService — hardware abstraction layer for AI agents."""
import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db, init_db
from app.models import Device, DeviceCapability, DeviceExecutionLog, DevicePresence, EmbodimentSession
from app.routers import audio, devices, execute, logs, presence
from app.routers import groups, embodiment
from app.schemas import StatsOut
from app.services.presence_manager import presence_manager
from app.services.embody_tool_sync import register_embody_tools
from app.services.embodiment_manager import expire_timed_out_sessions
from app.database import AsyncSessionLocal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

_STATIC   = Path(__file__).parent / "static"
_DATA_DIR = Path(__file__).parent.parent / "data"


# ── Stats ─────────────────────────────────────────────────────────────────────

async def _get_stats(db: AsyncSession) -> StatsOut:
    now      = datetime.now(timezone.utc).replace(tzinfo=None)
    today    = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)

    total_devices  = await db.scalar(select(func.count()).select_from(Device))
    online         = await db.scalar(select(func.count()).select_from(Device).where(Device.status == "online"))
    offline        = await db.scalar(select(func.count()).select_from(Device).where(Device.status == "offline"))
    unknown        = await db.scalar(select(func.count()).select_from(Device).where(Device.status == "unknown"))
    total_caps     = await db.scalar(select(func.count()).select_from(DeviceCapability))
    synced_caps    = await db.scalar(select(func.count()).select_from(DeviceCapability).where(DeviceCapability.tg_tool_id.isnot(None)))
    execs_today    = await db.scalar(select(func.count()).select_from(DeviceExecutionLog).where(DeviceExecutionLog.created_at >= today))
    execs_7d       = await db.scalar(select(func.count()).select_from(DeviceExecutionLog).where(DeviceExecutionLog.created_at >= week_ago))
    failed_7d      = await db.scalar(select(func.count()).select_from(DeviceExecutionLog).where(DeviceExecutionLog.created_at >= week_ago, DeviceExecutionLog.status == "failed"))
    active_embody  = await db.scalar(
        select(func.count()).select_from(EmbodimentSession).where(EmbodimentSession.state != "released")
    )

    return StatsOut(
        total_devices=total_devices or 0,
        online=online or 0,
        offline=offline or 0,
        unknown=unknown or 0,
        total_capabilities=total_caps or 0,
        synced_capabilities=synced_caps or 0,
        active_presences=presence_manager.count(),
        active_embodiment_sessions=active_embody or 0,
        executions_today=execs_today or 0,
        executions_7d=execs_7d or 0,
        failed_7d=failed_7d or 0,
    )


# ── Background task: session timeout sweep ────────────────────────────────────

async def _session_timeout_task() -> None:
    """
    Background task — runs every 30 seconds.
    Releases any EmbodimentSession where permission_plan='timeout' and expires_at < now().
    """
    logger.info("DBS: session timeout sweep task started")
    while True:
        await asyncio.sleep(30)
        try:
            async with AsyncSessionLocal() as db:
                count = await expire_timed_out_sessions(db)
                if count:
                    logger.info("DBS: timeout sweep released %d session(s)", count)
        except Exception as exc:
            logger.warning("DBS: timeout sweep error: %s", exc)


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    await init_db()
    logger.info("DeviceBridgeService starting on %s:%d", settings.host, settings.port)

    # Register embody.* tools in ToolGateway (idempotent)
    try:
        registered, failed = await register_embody_tools()
        logger.info(
            "DBS: embody.* tool registration: %d registered, %d failed", registered, failed
        )
    except Exception as exc:
        logger.warning("DBS: embody.* tool registration failed: %s", exc)

    # Start background tasks
    timeout_task = asyncio.create_task(_session_timeout_task())

    yield

    timeout_task.cancel()
    try:
        await timeout_task
    except asyncio.CancelledError:
        pass
    logger.info("DeviceBridgeService shutting down.")


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="DeviceBridgeService",
    description="Hardware abstraction layer — connects AI agents to physical and virtual devices.",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Existing routers
app.include_router(devices.router)
app.include_router(execute.router)
app.include_router(presence.router)
app.include_router(audio.router)
app.include_router(logs.router)

# New routers
app.include_router(groups.router)
app.include_router(embodiment.router)


@app.get("/api/stats", response_model=StatsOut, tags=["stats"])
async def stats(db: AsyncSession = Depends(get_db)):
    return await _get_stats(db)


@app.post("/api/auth/login", tags=["auth"], include_in_schema=False)
async def proxy_login(body: dict):
    """Proxy UserManager login for the admin panel."""
    import httpx
    from fastapi import HTTPException
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{settings.usermanager_url}/auth/login",
                json=body,
                timeout=5.0,
            )
        return r.json()
    except Exception as exc:
        raise HTTPException(503, f"UserManager unavailable: {exc}")


@app.get("/health", include_in_schema=False)
async def health():
    return {"status": "ok", "service": "DeviceBridgeService", "version": "2.0.0"}


if _STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    @app.get("/", include_in_schema=False)
    async def admin_ui():
        return FileResponse(str(_STATIC / "admin.html"))
