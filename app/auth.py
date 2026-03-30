from typing import Optional

import httpx
from fastapi import Header, HTTPException

from app.config import settings


async def get_principal(authorization: Optional[str] = Header(default=None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Not authenticated — provide Authorization: Bearer <token>")
    token = authorization[7:]
    try:
        async with httpx.AsyncClient() as client:
            r = await client.get(
                f"{settings.usermanager_url}/auth/validate",
                headers={"Authorization": f"Bearer {token}"},
                timeout=5.0,
            )
        data = r.json()
    except Exception:
        raise HTTPException(503, "UserManager unavailable")
    if not data.get("valid"):
        raise HTTPException(401, "Invalid or expired token")
    return data


async def get_admin_principal(authorization: Optional[str] = Header(default=None)) -> dict:
    principal = await get_principal(authorization)
    if not principal.get("is_admin", False):
        raise HTTPException(403, "Admin access required")
    return principal
