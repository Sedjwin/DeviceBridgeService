"""Adapter registry — maps protocol names to adapter classes."""
from __future__ import annotations

from typing import Any

from app.adapters.base import DeviceAdapter
from app.adapters.wled import WLEDAdapter
from app.adapters.http_device import HTTPDeviceAdapter

_ADAPTER_MAP: dict[str, type[DeviceAdapter]] = {
    "wled":      WLEDAdapter,
    "http_rest": HTTPDeviceAdapter,
    "pi_bridge": HTTPDeviceAdapter,  # Pi bridge uses same HTTP REST interface
}


def get_adapter(protocol: str, host: str, connection: dict[str, Any]) -> DeviceAdapter:
    """Instantiate the correct adapter for the given protocol."""
    cls = _ADAPTER_MAP.get(protocol)
    if cls is None:
        raise ValueError(
            f"Unsupported protocol '{protocol}'. "
            f"Supported: {sorted(_ADAPTER_MAP.keys())}"
        )
    return cls(host, connection)
