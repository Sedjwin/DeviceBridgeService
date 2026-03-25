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


def _normalize_name(value: str) -> str:
    return value.strip().lower().replace("-", "_").replace(" ", "_")


def _tokens(value: str) -> set[str]:
    return {tok for tok in _normalize_name(value).split("_") if tok}


EMOTION_ALIASES: dict[str, set[str]] = {
    "happy": {"happy", "joy", "pleased", "satisfied"},
    "sad": {"sad", "down", "unhappy"},
    "angry": {"angry", "irritated", "annoyed"},
    "curious": {"curious", "interested", "inquisitive"},
    "bored": {"bored", "idle", "neutral"},
}

ACTION_ALIASES: dict[str, set[str]] = {
    "scan": {"scan", "sweep", "analyze"},
    "blink": {"blink", "eye"},
    "tilt": {"tilt", "lean", "angle"},
    "talk": {"talk", "speak", "mouth"},
    "idle": {"idle", "rest", "standby"},
}


def _semantic_score(label: str, animation: str, source_type: str) -> int:
    n_label = _normalize_name(label)
    n_anim = _normalize_name(animation)
    if n_label == n_anim:
        return 100

    score = 0
    if n_label in n_anim or n_anim in n_label:
        score += 35

    label_tokens = _tokens(label)
    anim_tokens = _tokens(animation)
    overlap = len(label_tokens.intersection(anim_tokens))
    score += overlap * 15

    alias_table = EMOTION_ALIASES if source_type == "emotion" else ACTION_ALIASES
    for alias_set in alias_table.values():
        if label_tokens.intersection(alias_set) and anim_tokens.intersection(alias_set):
            score += 20
            break

    return score


def _pick_render_mode(preferred_render_mode: str, supported_modes: list[str], animation: str) -> str | None:
    if preferred_render_mode in supported_modes:
        return preferred_render_mode
    anim_tokens = _tokens(animation)
    if "line" in anim_tokens and "line" in supported_modes:
        return "line"
    if "shape" in anim_tokens and "shape" in supported_modes:
        return "shape"
    if ("photo" in anim_tokens or "warp" in anim_tokens) and "photo_warp" in supported_modes:
        return "photo_warp"
    if ("3d" in anim_tokens or "mesh" in anim_tokens or "model" in anim_tokens) and "model3d" in supported_modes:
        return "model3d"
    return supported_modes[0] if supported_modes else None


def suggest_rule_for_label(
    label: str,
    animations: list[str],
    *,
    source_type: str,
    preferred_render_mode: str,
    supported_modes: list[str],
    passthrough_model_tags: bool = False,
) -> MappingRule:
    if passthrough_model_tags:
        # Device can interpret model-side directives directly.
        render_mode = _pick_render_mode(preferred_render_mode, supported_modes, label)
        return MappingRule(animation=_normalize_name(label), render_mode=render_mode, fallback=[DEFAULT_FALLBACK])

    normalized_label = _normalize_name(label)
    normalized_animations = {_normalize_name(name): name for name in animations}

    if normalized_label in normalized_animations:
        target = normalized_animations[normalized_label]
    else:
        target = ""
        best_score = -1
        for _, original in normalized_animations.items():
            score = _semantic_score(label, original, source_type)
            if score > best_score:
                best_score = score
                target = original
        if not target:
            for candidate in ("neutral_blink", "idle", DEFAULT_FALLBACK):
                if candidate in animations:
                    target = candidate
                    break
        if not target and animations:
            target = animations[0]
        if not target:
            target = DEFAULT_FALLBACK

    render_mode = _pick_render_mode(preferred_render_mode, supported_modes, target)
    fallback = [DEFAULT_FALLBACK] if target != DEFAULT_FALLBACK else []
    return MappingRule(animation=target, render_mode=render_mode, fallback=fallback)
