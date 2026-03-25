from __future__ import annotations

import json
from typing import Any

import httpx

from app.config import settings
from app.schemas import MappingRule
from app.services.mapping import suggest_rule_for_label


def _extract_first_json_object(raw: str) -> dict[str, Any] | None:
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end < 0 or end <= start:
        return None
    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None


def _fallback_rules(
    labels: list[str],
    *,
    source_type: str,
    animations: list[str],
    preferred_render_mode: str,
    supported_modes: list[str],
    passthrough: bool,
) -> dict[str, MappingRule]:
    return {
        label: suggest_rule_for_label(
            label,
            animations,
            source_type=source_type,
            preferred_render_mode=preferred_render_mode,
            supported_modes=supported_modes,
            passthrough_model_tags=passthrough,
        )
        for label in labels
    }


async def suggest_rules_with_llm(
    *,
    source_type: str,
    labels: list[str],
    animations: list[str],
    supported_modes: list[str],
    preferred_render_mode: str,
    passthrough_model_tags: bool,
) -> dict[str, MappingRule]:
    if not labels:
        return {}

    if not settings.mapping_llm_enabled or not settings.system_basic_token:
        return _fallback_rules(
            labels,
            source_type=source_type,
            animations=animations,
            preferred_render_mode=preferred_render_mode,
            supported_modes=supported_modes,
            passthrough=passthrough_model_tags,
        )

    prompt = {
        "task": "Map agent semantic tags to device animations.",
        "source_type": source_type,
        "labels": labels,
        "device_animations": animations,
        "supported_modes": supported_modes,
        "preferred_render_mode": preferred_render_mode,
        "passthrough_model_tags": passthrough_model_tags,
        "constraints": {
            "must_only_use_device_animations_unless_passthrough": True,
            "output_json_only": True,
            "fallback_default": "neutral_blink",
        },
        "output_schema": {
            "rules": {
                "<label>": {
                    "animation": "string",
                    "render_mode": "string|null",
                    "intensity": "number",
                    "duration_ms": "integer|null",
                    "fallback": ["string"],
                }
            }
        },
    }

    request_payload = {
        "model": settings.system_basic_model,
        "temperature": 0.1,
        "messages": [
            {
                "role": "system",
                "content": "You are a strict JSON mapper for agent tags to device animations. Output JSON only.",
            },
            {
                "role": "user",
                "content": json.dumps(prompt, ensure_ascii=True),
            },
        ],
    }

    url = f"{settings.ai_gateway_url.rstrip('/')}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.system_basic_token}",
        "Content-Type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            res = await client.post(url, headers=headers, json=request_payload)
            res.raise_for_status()
            body = res.json()
    except Exception:
        return _fallback_rules(
            labels,
            source_type=source_type,
            animations=animations,
            preferred_render_mode=preferred_render_mode,
            supported_modes=supported_modes,
            passthrough=passthrough_model_tags,
        )

    content = ""
    try:
        content = body["choices"][0]["message"]["content"] or ""
    except Exception:
        content = ""

    parsed = _extract_first_json_object(content)
    if not parsed:
        return _fallback_rules(
            labels,
            source_type=source_type,
            animations=animations,
            preferred_render_mode=preferred_render_mode,
            supported_modes=supported_modes,
            passthrough=passthrough_model_tags,
        )

    raw_rules = parsed.get("rules", {}) if isinstance(parsed, dict) else {}
    out: dict[str, MappingRule] = {}
    for label in labels:
        candidate = raw_rules.get(label)
        if isinstance(candidate, dict):
            try:
                rule = MappingRule.model_validate(candidate)
                out[label] = rule
                continue
            except Exception:
                pass
        out[label] = suggest_rule_for_label(
            label,
            animations,
            source_type=source_type,
            preferred_render_mode=preferred_render_mode,
            supported_modes=supported_modes,
            passthrough_model_tags=passthrough_model_tags,
        )

    return out
