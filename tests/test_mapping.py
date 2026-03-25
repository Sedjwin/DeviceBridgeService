from app.schemas import MappingRule, TimelineEvent
from app.services.mapping import MappingContext, MappingEngine


def test_mapping_engine_fallbacks_to_supported_animation_and_mode() -> None:
    engine = MappingEngine()
    ctx = MappingContext(
        device_capabilities={
            "render_modes": ["shape"],
            "animations": ["neutral_blink", "scan_sweep"],
        },
        preferred_render_mode="model3d",
        emotion_map={
            "disdainful": MappingRule(
                animation="eye_narrow",
                render_mode="line",
                fallback=["neutral_blink"],
            )
        },
        action_map={
            "scan": MappingRule(animation="scan_sweep", render_mode="shape"),
        },
    )

    commands = engine.timeline_to_commands(
        [
            TimelineEvent(t=10, type="emotion", value="disdainful"),
            TimelineEvent(t=50, type="action", value="scan"),
        ],
        ctx,
    )

    assert len(commands) == 2
    assert commands[0]["payload"]["animation"] == "neutral_blink"
    assert commands[0]["payload"]["render_mode"] == "shape"
    assert commands[1]["payload"]["animation"] == "scan_sweep"
    assert commands[1]["payload"]["render_mode"] == "shape"
