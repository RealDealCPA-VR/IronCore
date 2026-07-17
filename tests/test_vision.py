"""Vision (MS-6): engine e2e with MockProvider — the attach flow, the honest
degrade, the settings override, the __init__ vision_check wiring, the text
floor carrier, and the MS-3 routed-coder binding. Zero network, zero model."""

from __future__ import annotations

import asyncio
import base64

from ironcore.config.settings import Settings
from ironcore.core.engine import TurnEngine
from ironcore.core.events import ToolCallFinished, TurnCompleted
from ironcore.core.roles import RoleRouter
from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.base import CompletionResult, Message, ToolCall
from ironcore.providers.mock import MockProvider
from ironcore.safety.modes import Mode
from ironcore.tools.default import build_default_registry

# a real 1x1 transparent PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR4nGNgYGBgAAAABQABh6FO1AAAAABJRU5ErkJggg=="
)


def _profile(*, vision: bool, protocol: str = "native") -> CapabilityProfile:
    tp = {"native": 1.0} if protocol == "native" else {}
    return CapabilityProfile(
        model_id="mock", honest_context=8192, tool_protocols=tp, vision=vision
    )


def _script(protocol: str = "native"):
    """model: call read_image, then stop with a short summary."""
    if protocol == "native":
        first = CompletionResult(
            message=Message(
                role="assistant",
                tool_calls=[
                    ToolCall(id="c1", name="read_image", arguments={"path": "shot.png"})
                ],
            ),
            finish_reason="tool_calls",
        )
    else:  # text floor: the call rides an ironcall block
        first = CompletionResult(
            message=Message(
                role="assistant",
                content=(
                    "```ironcall\n"
                    '{"tool": "read_image", "args": {"path": "shot.png"}}\n'
                    "```"
                ),
            )
        )
    return [first, CompletionResult(message=Message(role="assistant", content="a tiny image"))]


def _engine(tmp_path, script, profile, *, settings=None, roles=None):
    (tmp_path / "shot.png").write_bytes(PNG)
    settings = settings or Settings()
    provider = MockProvider(list(script))
    engine = TurnEngine(
        provider,
        build_default_registry(settings, tmp_path),
        settings,
        profile,
        Mode.AUTO,  # read_image is READ risk: auto-allowed inside the jail
        workspace=tmp_path,
        snapshots=None,
        roles=roles,
    )
    return engine, provider


def drive(engine, user_input="look at shot.png"):
    events = []

    async def _run():
        async for ev in engine.run_turn(user_input):
            events.append(ev)

    asyncio.run(_run())
    return events


def _image_messages(provider):
    return [m for call in provider.calls for m in call if m.images]


# --- attach flow --------------------------------------------------------------


def test_image_reaches_the_provider_wire_as_a_user_carrier(tmp_path):
    engine, provider = _engine(tmp_path, _script(), _profile(vision=True))
    events = drive(engine)

    assert isinstance(events[-1], TurnCompleted) and events[-1].stop_reason == "done"
    finished = [e for e in events if isinstance(e, ToolCallFinished)]
    assert finished and finished[0].result.ok

    carriers = [m for m in provider.calls[-1] if m.images]
    assert carriers, "the second provider call must carry the attached image"
    carrier = carriers[0]
    assert carrier.role == "user"  # portable across native/strict_json/text floors
    assert carrier.images[0].media_type == "image/png"
    assert base64.b64decode(carrier.images[0].base64) == PNG
    assert "read_image" in carrier.content  # the honest [n image(s) from ...] note


def test_text_floor_carrier_is_still_a_user_message_with_images(tmp_path):
    engine, provider = _engine(
        tmp_path, _script("text_protocol"), _profile(vision=True, protocol="text")
    )
    events = drive(engine)

    assert isinstance(events[-1], TurnCompleted) and events[-1].stop_reason == "done"
    carriers = _image_messages(provider)
    assert carriers and all(m.role == "user" for m in carriers)
    assert base64.b64decode(carriers[0].images[0].base64) == PNG


# --- degrade flow -------------------------------------------------------------


def test_no_vision_degrades_honestly_and_nothing_reaches_the_wire(tmp_path):
    engine, provider = _engine(tmp_path, _script(), _profile(vision=False))
    events = drive(engine)

    finished = [e for e in events if isinstance(e, ToolCallFinished)]
    assert finished and not finished[0].result.ok
    assert "no vision capability" in finished[0].result.error
    assert _image_messages(provider) == []  # nothing image-shaped ever left
    # the MODEL saw the truth (fed-back output), not an empty failure to guess at
    framed = "\n".join(m.content for call in provider.calls for m in call)
    assert "no vision capability" in framed


# --- settings override --------------------------------------------------------


def test_envelope_vision_true_overrides_a_false_profile(tmp_path):
    settings = Settings.model_validate({"envelope": {"vision": True}})
    engine, provider = _engine(
        tmp_path, _script(), _profile(vision=False), settings=settings
    )
    drive(engine)
    assert _image_messages(provider)  # override wins: the image attached


def test_envelope_vision_false_overrides_a_true_profile(tmp_path):
    settings = Settings.model_validate({"envelope": {"vision": False}})
    engine, provider = _engine(
        tmp_path, _script(), _profile(vision=True), settings=settings
    )
    events = drive(engine)
    finished = [e for e in events if isinstance(e, ToolCallFinished)]
    assert finished and not finished[0].result.ok
    assert _image_messages(provider) == []


def test_envelope_vision_parses_from_config_and_defaults_to_none():
    assert Settings().envelope.vision is None
    assert Settings.model_validate({"envelope": {"vision": True}}).envelope.vision is True


# --- wiring -------------------------------------------------------------------


def test_init_wires_the_registry_tool_to_the_engine_check(tmp_path):
    engine, _ = _engine(tmp_path, [], _profile(vision=True))
    tool = engine.tools.get("read_image")
    assert tool is not None
    assert tool.vision_check == engine._vision_enabled


def test_init_never_clobbers_a_custom_vision_check(tmp_path):
    settings = Settings()
    tools = build_default_registry(settings, tmp_path)
    def custom() -> bool:
        return True

    tools.get("read_image").vision_check = custom
    engine = TurnEngine(
        MockProvider([]),
        tools,
        settings,
        _profile(vision=False),
        Mode.AUTO,
        workspace=tmp_path,
        snapshots=None,
    )
    assert engine.tools.get("read_image").vision_check is custom


def test_hot_swapped_profile_flips_vision_live(tmp_path):
    # app.py does `engine.profile = seed` when a background seed lands: the
    # check reads the profile live, so the flip needs no re-wiring.
    engine, _ = _engine(tmp_path, [], _profile(vision=False))
    assert engine._vision_enabled() is False
    engine.profile = _profile(vision=True)
    assert engine._vision_enabled() is True


# --- MS-3 routing: the ACTIVE coder's capability governs attachment -----------


def test_routed_coder_vision_governs_attachment(tmp_path):
    settings = Settings.model_validate({"roles": {"coder": "vlm"}})
    coder = MockProvider(_script())
    router = RoleRouter(
        settings,
        providers={"coder": coder},
        profiles={
            "vlm": CapabilityProfile(
                model_id="vlm",
                honest_context=8192,
                tool_protocols={"native": 1.0},
                vision=True,
            )
        },
    )
    # the PRIMARY profile has no vision; the routed coder does — the coder wins
    engine, primary = _engine(tmp_path, [], _profile(vision=False), roles=router)
    events = drive(engine)

    assert isinstance(events[-1], TurnCompleted) and events[-1].stop_reason == "done"
    assert primary.calls == []  # the routed coder took the loop
    carriers = _image_messages(coder)
    assert carriers and carriers[0].role == "user"
    assert base64.b64decode(carriers[0].images[0].base64) == PNG
