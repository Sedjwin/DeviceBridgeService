"""WLED device adapter — HTTP REST + UDP DNRGB real-time streaming."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import math
import socket
import time
from typing import Any

import httpx

from app.adapters.base import DeviceAdapter

# ── WLED DNRGB protocol constants ─────────────────────────────────────────────
_DNRGB_PROTOCOL  = 4      # DNRGB: offset + RGB data
_TIMEOUT_SECS    = 3      # WLED reverts to normal after this many seconds of no packets
# Max LEDs per UDP packet: 1472 (UDP payload limit) - 4 (DNRGB header) = 1468 bytes / 3 = 489 LEDs.
# Using 489 keeps packets within standard MTU and sends 9 packets for a 64×64 matrix
# instead of 16 (with the old 256 limit), reducing the inter-packet window WLED
# can refresh within.
_CHUNK_SIZE      = 489


class WLEDAdapter(DeviceAdapter):
    """
    Adapter for WLED-based devices (ESP32 LED matrix panels, strips, etc.).

    Capabilities handled:
      display_image     — decode + resize image, push via DNRGB UDP
      display_text      — render text with PIL, push via DNRGB UDP
      display_animation — loop base64 PNG frames via DNRGB UDP
      set_effect        — WLED HTTP /json/state
      clear             — WLED HTTP /json/state (all black)
    """

    def __init__(self, host: str, connection: dict[str, Any]):
        super().__init__(host, connection)
        self.http_port = connection.get("http_port", 80)
        self.udp_port  = connection.get("udp_port", 21324)
        self.width     = connection.get("width", 64)
        self.height    = connection.get("height", 64)
        self._base_url = f"http://{host}:{self.http_port}"

    # ── Connectivity ──────────────────────────────────────────────────────────

    async def ping(self) -> tuple[bool, float | None, dict[str, Any]]:
        t0 = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{self._base_url}/json/info")
            latency_ms = (time.monotonic() - t0) * 1000
            if r.status_code == 200:
                return True, round(latency_ms, 1), r.json()
            return False, None, {}
        except Exception:
            return False, None, {}

    async def fetch_live_manifest(self) -> dict[str, Any] | None:
        """Auto-generate a manifest from /json/info."""
        online, _, info = await self.ping()
        if not online:
            return None

        leds = info.get("leds", {})
        matrix = leds.get("matrix", {})
        w = matrix.get("w", self.width)
        h = matrix.get("h", self.height)
        fps = leds.get("fps", 30)

        return {
            "display": {"width": w, "height": h, "type": "rgb_matrix", "max_fps": fps},
            "audio": None,
            "input": None,
            "capabilities": _WLED_CAPABILITIES,
            "_wled_info": {
                "version": info.get("ver"),
                "name": info.get("name"),
                "led_count": leds.get("count"),
            },
        }

    # ── Execution dispatcher ──────────────────────────────────────────────────

    async def execute(self, capability: str, payload: dict[str, Any]) -> dict[str, Any]:
        if capability == "display_image":
            return await self._display_image(payload)
        if capability == "display_text":
            return await self._display_text(payload)
        if capability == "display_animation":
            return await self._display_animation(payload)
        if capability == "set_effect":
            return await self._set_effect(payload)
        if capability == "clear":
            return await self._clear()
        raise ValueError(f"Unknown WLED capability: {capability}")

    # ── Capability implementations ────────────────────────────────────────────

    async def _display_image(self, payload: dict[str, Any]) -> dict[str, Any]:
        image_b64 = payload.get("image_b64") or payload.get("image")
        if not image_b64:
            raise ValueError("display_image requires 'image_b64' (base64-encoded PNG or JPEG)")

        pixels = _decode_image_to_pixels(image_b64, self.width, self.height)
        await asyncio.get_event_loop().run_in_executor(
            None, self._send_frame_udp, pixels
        )
        return {"ok": True, "pixels_sent": len(pixels)}

    async def _display_text(self, payload: dict[str, Any]) -> dict[str, Any]:
        text     = payload.get("text", "")
        color    = _parse_color(payload.get("color", "#FF8800"))
        bg_color = _parse_color(payload.get("bg_color", "#000000"))
        scroll   = payload.get("scroll", True)
        speed    = max(1, min(100, int(payload.get("speed", 40))))
        font_size = int(payload.get("font_size", 16))

        if not text:
            raise ValueError("display_text requires 'text'")

        if scroll:
            frames = _render_scrolling_text(text, color, bg_color, self.width, self.height, font_size)
            frame_delay = 0.05 * (101 - speed) / 100  # 0.0005s – 0.05s per frame
            await asyncio.get_event_loop().run_in_executor(
                None, self._send_frames_udp, frames, frame_delay
            )
            return {"ok": True, "frames": len(frames), "scroll": True}
        else:
            pixels = _render_static_text(text, color, bg_color, self.width, self.height, font_size)
            await asyncio.get_event_loop().run_in_executor(
                None, self._send_frame_udp, pixels
            )
            return {"ok": True, "scroll": False}

    async def _display_animation(self, payload: dict[str, Any]) -> dict[str, Any]:
        frames_b64 = payload.get("frames", [])
        fps        = max(1, min(60, int(payload.get("fps", 15))))
        loop_count = max(1, min(10, int(payload.get("loop_count", 1))))

        if not frames_b64:
            raise ValueError("display_animation requires 'frames' (list of base64 PNG strings)")

        frame_delay = 1.0 / fps
        decoded = [_decode_image_to_pixels(f, self.width, self.height) for f in frames_b64]

        all_frames = decoded * loop_count
        await asyncio.get_event_loop().run_in_executor(
            None, self._send_frames_udp, all_frames, frame_delay
        )
        return {"ok": True, "frames": len(decoded), "loops": loop_count, "fps": fps}

    async def _set_effect(self, payload: dict[str, Any]) -> dict[str, Any]:
        effect_id  = int(payload.get("effect_id", 0))
        palette_id = int(payload.get("palette_id", 0))
        color      = payload.get("color", "#FF8800")
        r, g, b    = _parse_color(color)
        brightness = int(payload.get("brightness", 128))

        body = {
            "on": True,
            "bri": brightness,
            "seg": [{
                "fx": effect_id,
                "pal": palette_id,
                "col": [[r, g, b], [0, 0, 0], [0, 0, 0]],
            }],
        }
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(f"{self._base_url}/json/state", json=body)
        return {"ok": r.status_code < 300, "http_status": r.status_code}

    async def _clear(self) -> dict[str, Any]:
        body = {"on": True, "bri": 0, "seg": [{"col": [[0, 0, 0]]}]}
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(f"{self._base_url}/json/state", json=body)
        # Also send a black DNRGB frame to instantly clear
        pixels = [(0, 0, 0)] * (self.width * self.height)
        await asyncio.get_event_loop().run_in_executor(
            None, self._send_frame_udp, pixels
        )
        return {"ok": r.status_code < 300}

    # ── DNRGB UDP helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _build_dnrgb_packets(pixels: list[tuple[int, int, int]]) -> list[bytes]:
        """
        Pre-build all DNRGB packets for a frame as a list of bytes objects.
        Building upfront means the send loop contains only sendto() calls —
        no Python computation between sends — which minimises the inter-packet
        gap that WLED can fire a refresh within.
        """
        packets = []
        for start in range(0, len(pixels), _CHUNK_SIZE):
            chunk = pixels[start:start + _CHUNK_SIZE]
            data  = bytearray([_DNRGB_PROTOCOL, _TIMEOUT_SECS, (start >> 8) & 0xFF, start & 0xFF])
            for r, g, b in chunk:
                data.extend((r, g, b))
            packets.append(bytes(data))
        return packets

    def _send_frame_udp(self, pixels: list[tuple[int, int, int]]) -> None:
        """Send a single frame via WLED DNRGB UDP protocol."""
        packets = self._build_dnrgb_packets(pixels)
        addr    = (self.host, self.udp_port)
        sock    = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, len(packets) * 1500)
        try:
            for pkt in packets:
                sock.sendto(pkt, addr)
        finally:
            sock.close()

    def _send_frames_udp(self, frames: list[list[tuple[int, int, int]]], frame_delay: float) -> None:
        """
        Send multiple frames via DNRGB with a delay between each.
        All packet data is pre-built before the send loop starts so that
        per-frame bursts contain no Python overhead between sendto() calls.
        """
        all_frame_packets = [self._build_dnrgb_packets(pixels) for pixels in frames]
        addr = (self.host, self.udp_port)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 9 * 1500)
        try:
            for packets in all_frame_packets:
                for pkt in packets:
                    sock.sendto(pkt, addr)
                time.sleep(frame_delay)
        finally:
            sock.close()


# ── Image helpers ─────────────────────────────────────────────────────────────

def _decode_image_to_pixels(
    image_b64: str,
    width: int,
    height: int,
) -> list[tuple[int, int, int]]:
    """Decode a base64 image and resize to (width, height). Returns flat RGB list."""
    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError("Pillow is required for image display: pip install Pillow")

    # Strip data URI prefix if present
    if "," in image_b64:
        image_b64 = image_b64.split(",", 1)[1]

    raw = base64.b64decode(image_b64)
    img = Image.open(io.BytesIO(raw)).convert("RGB")
    img = img.resize((width, height), Image.LANCZOS)
    img = img.transpose(Image.FLIP_LEFT_RIGHT)
    return list(img.getdata())


def _render_static_text(
    text: str,
    color: tuple[int, int, int],
    bg_color: tuple[int, int, int],
    width: int,
    height: int,
    font_size: int,
) -> list[tuple[int, int, int]]:
    """Render text centered on a (width × height) canvas."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise RuntimeError("Pillow is required for text display")

    img  = Image.new("RGB", (width, height), bg_color)
    draw = ImageDraw.Draw(img)
    font = _get_font(font_size)

    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (width  - tw) // 2
    y = (height - th) // 2
    draw.text((x, y), text, fill=color, font=font)
    img = img.transpose(Image.FLIP_LEFT_RIGHT)
    return list(img.getdata())


def _render_scrolling_text(
    text: str,
    color: tuple[int, int, int],
    bg_color: tuple[int, int, int],
    width: int,
    height: int,
    font_size: int,
) -> list[list[tuple[int, int, int]]]:
    """
    Render text on a wide canvas, then generate frames by scrolling a
    width-pixel window from right to left.
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        raise RuntimeError("Pillow is required for text display")

    font  = _get_font(font_size)
    draw_tmp = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    bbox  = draw_tmp.textbbox((0, 0), text, font=font)
    tw    = bbox[2] - bbox[0]
    th    = bbox[3] - bbox[1]
    canvas_w = tw + width * 2  # padding on each side

    canvas = Image.new("RGB", (canvas_w, height), bg_color)
    draw   = ImageDraw.Draw(canvas)
    y = (height - th) // 2
    draw.text((width, y), text, fill=color, font=font)
    canvas = canvas.transpose(Image.FLIP_LEFT_RIGHT)

    frames = []
    for x in range(canvas_w - width, -1, -1):
        crop   = canvas.crop((x, 0, x + width, height))
        frames.append(list(crop.getdata()))
    return frames


def _get_font(size: int):
    """Try to load a decent font; fall back to PIL default."""
    try:
        from PIL import ImageFont
        return ImageFont.load_default(size=size)
    except (AttributeError, TypeError):
        # Older Pillow — load_default() takes no size argument
        from PIL import ImageFont
        return ImageFont.load_default()


# ── Color helpers ─────────────────────────────────────────────────────────────

def _parse_color(color: Any) -> tuple[int, int, int]:
    """Parse '#RRGGBB', 'rgb(r,g,b)', [r,g,b], or (r,g,b) → (r, g, b) ints."""
    if isinstance(color, (list, tuple)) and len(color) >= 3:
        return (int(color[0]), int(color[1]), int(color[2]))
    if isinstance(color, str):
        color = color.strip()
        if color.startswith("#"):
            h = color.lstrip("#")
            if len(h) == 3:
                h = "".join(c * 2 for c in h)
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
        if color.startswith("rgb"):
            parts = color.replace("rgb(", "").replace(")", "").split(",")
            return (int(parts[0]), int(parts[1]), int(parts[2]))
    return (255, 136, 0)  # default orange


# ── Standard WLED capability definitions ──────────────────────────────────────

_WLED_CAPABILITIES: list[dict] = [
    {
        "name": "display_image",
        "description": "Display an image on the LED matrix. Accepts PNG or JPEG as base64.",
        "parameters": {
            "image_b64": {
                "type": "string",
                "required": True,
                "description": "Base64-encoded PNG or JPEG (data URIs accepted). Will be resized to fit the matrix.",
            }
        },
    },
    {
        "name": "display_text",
        "description": "Show text on the LED matrix, optionally scrolling across the display.",
        "parameters": {
            "text":      {"type": "string",  "required": True,  "description": "Text to display."},
            "color":     {"type": "string",  "default": "#FF8800", "description": "Hex colour e.g. #FF0000"},
            "bg_color":  {"type": "string",  "default": "#000000", "description": "Background hex colour"},
            "scroll":    {"type": "boolean", "default": True,   "description": "Scroll text across display"},
            "speed":     {"type": "integer", "default": 40,     "description": "Scroll speed 1–100"},
            "font_size": {"type": "integer", "default": 16,     "description": "Font size in pixels"},
        },
    },
    {
        "name": "display_animation",
        "description": "Play a frame-by-frame animation on the LED matrix.",
        "parameters": {
            "frames":     {"type": "array",   "required": True,  "description": "List of base64-encoded PNG frames"},
            "fps":        {"type": "integer", "default": 15,     "description": "Playback speed in frames/second"},
            "loop_count": {"type": "integer", "default": 1,      "description": "Number of times to loop (max 10)"},
        },
    },
    {
        "name": "set_effect",
        "description": "Activate a WLED built-in effect by ID. Effect IDs 0–197 are available.",
        "parameters": {
            "effect_id":  {"type": "integer", "required": True,  "description": "WLED effect ID (0 = solid colour)"},
            "palette_id": {"type": "integer", "default": 0,      "description": "WLED palette ID"},
            "color":      {"type": "string",  "default": "#FF8800", "description": "Primary hex colour"},
            "brightness": {"type": "integer", "default": 128,    "description": "Brightness 0–255"},
        },
    },
    {
        "name": "clear",
        "description": "Clear the display (turn off all LEDs).",
        "parameters": {},
    },
]
