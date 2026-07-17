"""TurnEngine × RoleRouter (MS-3): the turn loop routes per role, measured.

Full turns through the real engine with per-role MockProviders: PLAN-mode turns
go to the planner, execution turns to the coder, compaction to the summarizer —
and every routed call composes/samples against the ROLE model's own envelope
(protocol fragment, max_tokens window), not the primary's. Zero-config engines
are byte-identical to the pre-router loop, and a broken router degrades to the
primary pair instead of crashing the turn.
"""

from __future__ import annotations

import asyncio

from ironcore.config.settings import ProviderSettings, RoleModels, Settings
from ironcore.core.engine import TurnEngine
from ironcore.core.events import TurnCompleted
from ironcore.core.roles import RoleRouter
from ironcore.envelope.profile import CapabilityProfile
from ironcore.providers.base import CompletionResult, Message
from ironcore.providers.mock import MockProvider
from ironcore.providers.registry import ProviderRegistry
from ironcore.safety.modes import Mode
from ironcore.tools.default import build_default_registry

# --------------------------------------------------------------------------- #
# helpers (test_engine.py pattern)
# --------------------------------------------------------------------------- #


def _profile(model: str = "mock", *, native: bool = True, ctx: int = 8192) -> CapabilityProfile:
    tp = {"native": 1.0} if native else {}
    return CapabilityProfile(model_id=model, honest_context=ctx, tool_protocols=tp)


def _text(content: str) -> CompletionResult:
    return CompletionResult(message=Message(role="assistant", content=content))


def _settings(**roles) -> Settings:
    return Settings(
        provider=ProviderSettings(
            base_url="http://testserver/v1", api_key="sk-unit-test", model="big-70b"
        ),
        roles=RoleModels(**roles),
    )


def _engine(
    tmp_path,
    provider: MockProvider,
    settings: Settings,
    profile: CapabilityProfile,
    *,
    mode: Mode = Mode.MANUAL,
    roles: RoleRouter | None = None,
) -> TurnEngine:
    tools = build_default_registry(settings, tmp_path)
    return TurnEngine(
        provider, tools, settings, profile, mode,
        workspace=tmp_path, snapshots=None, roles=roles,
    )


def drive(engine: TurnEngine, user_input: str) -> list:
    events: list = []

    async def _run():
        async for ev in engine.run_turn(user_input):
            events.append(ev)

    asyncio.run(_run())
    return events


def _system_of(calls: list[list[Message]]) -> str:
    return "\n".join(m.content for m in calls[0] if m.role == "system")


# --- zero-config: no router → the primary pair, exactly as before ------------


def test_zero_config_engine_sends_the_loop_call_to_the_primary(tmp_path):
    primary = MockProvider([_text("done")])
    engine = _engine(tmp_path, primary, _settings(), _profile("big-70b"))
    assert engine.roles is None
    events = drive(engine, "say done")
    assert len(primary.calls) == 1
    assert [e for e in events if isinstance(e, TurnCompleted)][0].stop_reason == "done"


# --- coder routing: the role's provider AND the role's protocol --------------


def test_coder_role_routes_the_loop_call_with_the_roles_own_protocol(tmp_path):
    primary = MockProvider()  # native-capable primary; must receive NOTHING
    coder = MockProvider([_text("done")])
    settings = _settings(coder="tiny-7b")
    router = RoleRouter(
        settings,
        providers={"coder": coder},
        profiles={"tiny-7b": _profile("tiny-7b", native=False)},  # the FLOOR protocol
    )
    engine = _engine(tmp_path, primary, settings, _profile("big-70b"), roles=router)
    drive(engine, "say done")
    assert primary.calls == []
    assert len(coder.calls) == 1
    # the IRONCALL teaching fragment proves protocol came from the ROLE's floor
    # profile — the primary's profile would have selected native (no fragment).
    assert "ironcall" in _system_of(coder.calls)


# --- planner seam: PLAN-mode turns think on the planner model ----------------


def test_plan_mode_routes_to_the_planner_and_manual_falls_back(tmp_path):
    primary = MockProvider([_text("executed")])
    planner = MockProvider([_text("thought")])
    settings = _settings(planner="deep-70b")
    router = RoleRouter(
        settings,
        providers={"planner": planner},
        profiles={"deep-70b": _profile("deep-70b")},
    )
    plan_engine = _engine(
        tmp_path, primary, settings, _profile("big-70b"), mode=Mode.PLAN, roles=router
    )
    drive(plan_engine, "think about it")
    assert len(planner.calls) == 1 and primary.calls == []

    # same router, MANUAL mode: coder is unset → the PRIMARY pair executes
    manual_engine = _engine(
        tmp_path, primary, settings, _profile("big-70b"), mode=Mode.MANUAL, roles=router
    )
    drive(manual_engine, "do it")
    assert len(primary.calls) == 1 and len(planner.calls) == 1


# --- summarizer seam: in-turn compaction runs on the routed summarizer -------


def test_compaction_routes_to_the_summarizer_provider(tmp_path):
    summary = "Context: x\nChanged: y\nVerified: not verified\nNext: z\nGotchas: none"
    primary = MockProvider([_text("done")])
    summarizer = MockProvider([_text(summary)])
    settings = _settings(summarizer="small-8b")
    router = RoleRouter(
        settings,
        providers={"summarizer": summarizer},
        profiles={"small-8b": _profile("small-8b")},
    )
    # a tiny primary window + pre-stuffed history trips should_compact at turn start
    engine = _engine(
        tmp_path, primary, settings, _profile("big-70b", ctx=256), roles=router
    )
    engine._conversation = [Message(role="user", content="x" * 2000)]
    drive(engine, "continue")
    assert len(summarizer.calls) == 1  # exactly the compaction call
    assert "compaction summarizer" in _system_of(summarizer.calls)
    assert len(primary.calls) == 1  # the loop call stayed on the primary
    assert "compaction summarizer" not in _system_of(primary.calls)
    assert engine._conversation[0].content.startswith("# Compacted history")


# --- per-role window: sampling max_tokens sized by the ACTIVE profile --------


def test_routed_coder_window_sizes_max_tokens(tmp_path):
    primary = MockProvider()
    coder = MockProvider([_text("done")])
    settings = _settings(coder="tiny-7b")
    router = RoleRouter(
        settings,
        providers={"coder": coder},
        profiles={"tiny-7b": _profile("tiny-7b", ctx=2048)},
    )
    engine = _engine(
        tmp_path, primary, settings, _profile("big-70b", ctx=8192), roles=router
    )
    drive(engine, "say done")
    # 15% of the ROLE's 2048-token honest context — not the primary's 8192
    assert coder.last_sampling is not None
    assert coder.last_sampling.max_tokens == max(256, int(2048 * 0.15)) == 307


# --- crash-safety: a closed registry degrades to the primary pair ------------


def test_closed_registry_turn_completes_on_the_primary(tmp_path):
    settings = _settings(coder="tiny-7b")
    registry = ProviderRegistry(settings, provider_factory=lambda **kw: MockProvider())
    asyncio.run(registry.close_all())
    primary = MockProvider([_text("done")])
    router = RoleRouter(settings, registry=registry)
    engine = _engine(tmp_path, primary, settings, _profile("big-70b"), roles=router)
    events = drive(engine, "say done")
    assert len(primary.calls) == 1
    assert [e for e in events if isinstance(e, TurnCompleted)][0].stop_reason == "done"
