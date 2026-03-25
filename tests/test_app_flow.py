from __future__ import annotations

import threading
import uuid

from fastapi.testclient import TestClient

from app.main import app


def test_health() -> None:
    with TestClient(app) as client:
        res = client.get("/health")
        assert res.status_code == 200
        assert res.json()["status"] == "ok"


def test_device_ws_and_session_timeline_flow() -> None:
    device_id = f"esp32s3-{uuid.uuid4().hex[:8]}"
    agent_id = f"agent-{uuid.uuid4().hex[:8]}"

    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/device/{device_id}") as ws:
            ws.send_json(
                {
                    "type": "hello",
                    "name": "Waveshare 1.32",
                    "model": "esp32s3-waveshare-1.32-amoled",
                    "firmware_version": "0.1.0",
                    "capabilities": {
                        "render_modes": ["line", "shape"],
                        "animations": ["neutral_blink", "scan_sweep", "head_tilt"],
                        "audio_codecs": ["wav"],
                        "sample_rates": [22050],
                        "mic_enabled": True,
                        "mic_format": "pcm16",
                    },
                }
            )
            hello_ack = ws.receive_json()
            assert hello_ack["type"] == "hello.ack"

            mapping_payload = {
                "agent_id": agent_id,
                "preferred_render_mode": "line",
                "emotion_map": {
                    "curious": {
                        "animation": "head_tilt",
                        "render_mode": "line",
                        "fallback": ["neutral_blink"],
                    }
                },
                "action_map": {
                    "scan": {
                        "animation": "scan_sweep",
                        "render_mode": "shape",
                        "fallback": ["neutral_blink"],
                    }
                },
            }
            res = client.put(f"/api/devices/{device_id}/mappings", json=mapping_payload)
            assert res.status_code == 200

            start = client.post(
                "/api/sessions/start",
                json={
                    "agent_id": agent_id,
                    "device_id": device_id,
                    "upstream_session_id": "am-session-1",
                },
            )
            assert start.status_code == 200
            session_id = start.json()["session_id"]

            result_holder: dict = {}

            def post_timeline() -> None:
                result_holder["response"] = client.post(
                    f"/api/sessions/{session_id}/agent-timeline",
                    json={
                        "timeline": [
                            {"t": 0, "type": "emotion", "value": "curious"},
                            {"t": 250, "type": "action", "value": "scan"},
                        ]
                    },
                )

            thread = threading.Thread(target=post_timeline)
            thread.start()

            cmd1 = ws.receive_json()
            ws.send_json({"type": "ack", "command_id": cmd1["command_id"], "ok": True})

            cmd2 = ws.receive_json()
            ws.send_json({"type": "ack", "command_id": cmd2["command_id"], "ok": True})
            thread.join(timeout=5)

            timeline_res = result_holder["response"]
            assert timeline_res.status_code == 200
            assert timeline_res.json()["commands"] == 2

            ws.send_json(
                {
                    "type": "mic.chunk",
                    "session_id": session_id,
                    "audio_base64": "UklGRg==",
                    "sample_rate": 16000,
                }
            )
            mic_res = client.get(f"/api/sessions/{session_id}/mic")
            assert mic_res.status_code == 200
            assert mic_res.json()["status"] == "ok"

            stop = client.post(f"/api/sessions/{session_id}/stop")
            assert stop.status_code == 200
            assert stop.json()["active"] is False
