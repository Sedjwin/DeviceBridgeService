"""Execution log endpoints."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models import DeviceExecutionLog
from app.schemas import ExecutionLogOut

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("", response_model=list[ExecutionLogOut])
async def list_logs(
    device_id: Optional[str] = Query(None),
    status:    Optional[str] = Query(None),
    limit:     int           = Query(100, le=500),
    offset:    int           = Query(0),
    db: AsyncSession         = Depends(get_db),
):
    q = select(DeviceExecutionLog).order_by(desc(DeviceExecutionLog.created_at))
    if device_id:
        q = q.where(DeviceExecutionLog.device_id == device_id)
    if status:
        q = q.where(DeviceExecutionLog.status == status)
    q = q.offset(offset).limit(limit)
    result = await db.execute(q)
    return result.scalars().all()


@router.get("/stats", response_model=dict)
async def log_stats(db: AsyncSession = Depends(get_db)):
    from datetime import datetime, timedelta, timezone
    now      = datetime.now(timezone.utc).replace(tzinfo=None)
    today    = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)

    total     = await db.scalar(select(func.count()).select_from(DeviceExecutionLog))
    today_cnt = await db.scalar(select(func.count()).select_from(DeviceExecutionLog).where(DeviceExecutionLog.created_at >= today))
    week_cnt  = await db.scalar(select(func.count()).select_from(DeviceExecutionLog).where(DeviceExecutionLog.created_at >= week_ago))
    failed_7d = await db.scalar(select(func.count()).select_from(DeviceExecutionLog).where(DeviceExecutionLog.created_at >= week_ago, DeviceExecutionLog.status == "failed"))

    return {
        "total": total or 0,
        "today": today_cnt or 0,
        "week":  week_cnt or 0,
        "failed_7d": failed_7d or 0,
    }
