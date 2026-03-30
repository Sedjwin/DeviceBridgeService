"""DeviceBridgeService — hardware abstraction layer for AI agents."""
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
from app.models import Device, DeviceCapability, DeviceExecutionLog, DevicePresence
from app.routers import audio, devices, execute, logs, presence
from app.schemas import StatsOut
from app.services.presence_manager import presence_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

_STATIC   = Path(__file__).parent / "static"
_DATA_DIR = Path(__file__).parent.parent / "data"


async def _get_stats(db: AsyncSession) -> StatsOut:
    now      = datetime.now(timezone.utc).replace(tzinfo=None)
    today    = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)

    total_devices       = await db.scalar(select(func.count()).select_from(Device))
    online              = await db.scalar(select(func.count()).select_from(Device).where(Device.status == "online"))
    offline             = await db.scalar(select(func.count()).select_from(Device).where(Device.status == "offline"))
    unknown             = await db.scalar(select(func.count()).select_from(Device).where(Device.status == "unknown"))
    total_caps          = await db.scalar(select(func.count()).select_from(DeviceCapability))
    synced_caps         = await db.scalar(select(func.count()).select_from(DeviceCapability).where(DeviceCapability.tg_tool_id.isnot(None)))
    execs_today         = await db.scalar(select(func.count()).select_from(DeviceExecutionLog).where(DeviceExecutionLog.created_at >= today))
    execs_7d            = await db.scalar(select(func.count()).select_from(DeviceExecutionLog).where(DeviceExecutionLog.created_at >= week_ago))
    failed_7d           = await db.scalar(select(func.count()).select_from(DeviceExecutionLog).where(DeviceExecutionLog.created_at >= week_ago, DeviceExecutionLog.status == "failed"))

    return StatsOut(
        total_devices=total_devices or 0,
        online=online or 0,
        offline=offline or 0,
        unknown=unknown or 0,
        total_capabilities=total_caps or 0,
        synced_capabilities=synced_caps or 0,
        active_presences=presence_manager.count(),
        executions_today=execs_today or 0,
        executions_7d=execs_7d or 0,
        failed_7d=failed_7d or 0,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    await init_db()
    logger.info("DeviceBridgeService starting on %s:%d", settings.host, settings.port)
    yield
    logger.info("DeviceBridgeService shutting down.")


app = FastAPI(
    title="DeviceBridgeService",
    description="Hardware abstraction layer — connects AI agents to physical and virtual devices.",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(devices.router)
app.include_router(execute.router)
app.include_router(presence.router)
app.include_router(audio.router)
app.include_router(logs.router)


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
    return {"status": "ok", "service": "DeviceBridgeService", "version": "1.0.0"}


if _STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

    @app.get("/", include_in_schema=False)
    async def admin_ui():
        return FileResponse(str(_STATIC / "admin.html"))
