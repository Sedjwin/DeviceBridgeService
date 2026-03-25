from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.schemas import MappingRule, TimelineEvent


DEFAULT_FALLBACK = "neutral_blink"


@dataclass(slots=True)
class MappingContext:
    device_capabilities: dict[str, Any]
    preferred_render_mode: str
    emotion_map: dict[str, MappingRule]
    action_map: dict[str, MappingRule]


class MappingEngine:
    def __init__(self, global_fallback_order: list[str] | None = None) -> None:
        self.global_fallback_order = global_fallback_order or ["line", "shape", "photo_warp", "model3d"]

    def choose_render_mode(self, preferred_mode: str, supported_modes: list[str]) -> str:
        if preferred_mode in supported_modes:
            return preferred_mode
        for mode in self.global_fallback_order:
            if mode in supported_modes:
                return mode
        return "line"

    def _resolve_rule(self, value: str, mapping: dict[str, MappingRule], animations: list[str]) -> MappingRule:
        rule = mapping.get(value)
        if rule and rule.animation in animations:
            return rule
        if rule:
            for fallback_name in rule.fallback:
                if fallback_name in animations:
                    return MappingRule(animation=fallback_name, render_mode=rule.render_mode)
        if DEFAULT_FALLBACK in animations:
            return MappingRule(animation=DEFAULT_FALLBACK)
        return MappingRule(animation=animations[0] if animations else DEFAULT_FALLBACK)

    def timeline_to_commands(self, timeline: list[TimelineEvent], ctx: MappingContext) -> list[dict[str, Any]]:
        caps = ctx.device_capabilities or {}
        supported_modes = caps.get("render_modes", ["line"])
        animations = caps.get("animations", [DEFAULT_FALLBACK])
        commands: list[dict[str, Any]] = []

        for event in timeline:
            if event.type not in {"emotion", "action"}:
                continue
            source_map = ctx.emotion_map if event.type == "emotion" else ctx.action_map
            rule = self._resolve_rule(str(event.value), source_map, animations)
            render_mode = self.choose_render_mode(rule.render_mode or ctx.preferred_render_mode, supported_modes)
            commands.append(
                {
                    "type": "avatar.anim",
                    "payload": {
                        "at_ms": event.t,
                        "source_type": event.type,
                        "source_value": str(event.value),
                        "animation": rule.animation,
                        "render_mode": render_mode,
                        "intensity": rule.intensity,
                        "duration_ms": rule.duration_ms,
                    },
                }
            )
        return commands
