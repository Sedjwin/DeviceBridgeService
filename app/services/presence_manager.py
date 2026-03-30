"""In-memory presence tracker with DB backing."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


class PresenceManager:
    """
    Tracks which device is active for each agent session.
    In-memory for fast lookups; DB is the persistent record.
    """

    def __init__(self):
        # session_id → {device_id, agent_id, acquired_at}
        self._active: dict[str, dict[str, Any]] = {}

    def acquire(self, session_id: str, device_id: str, agent_id: str) -> None:
        self._active[session_id] = {
            "device_id":   device_id,
            "agent_id":    agent_id,
            "acquired_at": datetime.now(timezone.utc),
        }

    def release(self, session_id: str) -> str | None:
        """Release presence; returns the device_id that was released, or None."""
        entry = self._active.pop(session_id, None)
        return entry["device_id"] if entry else None

    def transfer(self, session_id: str, new_device_id: str) -> str | None:
        """Move a session to a new device. Returns old device_id or None."""
        entry = self._active.get(session_id)
        if entry:
            old = entry["device_id"]
            entry["device_id"]   = new_device_id
            entry["acquired_at"] = datetime.now(timezone.utc)
            return old
        return None

    def get(self, session_id: str) -> dict[str, Any] | None:
        return self._active.get(session_id)

    def get_device(self, session_id: str) -> str | None:
        entry = self._active.get(session_id)
        return entry["device_id"] if entry else None

    def all_active(self) -> list[dict[str, Any]]:
        return [
            {"session_id": sid, **data}
            for sid, data in self._active.items()
        ]

    def count(self) -> int:
        return len(self._active)


# Singleton used across the app
presence_manager = PresenceManager()
