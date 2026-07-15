"""/model (IC-801): list the endpoint's models, or switch the active model.

    /model            — list the models the endpoint serves, marking the current
    /model <name>     — switch ``settings.provider.model`` to ``<name>``

Listing calls ``Provider.list_models`` (async), so ``/model`` with no args
returns an ack and posts the formatted list via ``schedule``. Switching is a
synchronous settings mutation: v0.x has ONE endpoint / MANY models and the
registry caches providers by model name with no live mutation API (see the
IC-204 handoff), so a switch updates config and advises a re-probe rather than
hot-swapping the running provider. With no ``provider_registry`` (headless /
tests) listing is unavailable and the handler reports the configured model.
"""

from __future__ import annotations

from ironcore.commands.base import CommandContext, SlashCommand
from ironcore.providers.openai_compat import ProviderError


def _cmd_model(ctx: CommandContext, args: str) -> str:
    args = args.strip()
    settings = ctx.settings
    current = settings.provider.model

    if args:
        settings.provider.model = args
        return (
            f"Model switched to {args!r} (was {current!r}). The configured model is updated; "
            "the live endpoint connection re-points on the next session — run /probe to "
            "re-profile capabilities for the new model."
        )

    registry = ctx.extra.get("provider_registry")
    schedule = ctx.extra.get("schedule")
    if registry is None or schedule is None:
        return f"Current model: {current}\n(Listing models needs a live endpoint.)"

    provider = _default_provider(registry)
    if provider is None:
        return f"Current model: {current}\n(The endpoint connection is closed; cannot list.)"
    schedule(_list_models(provider, current))
    return f"Listing models at the endpoint… (current: {current})"


def _default_provider(registry):
    try:
        return registry.default
    except Exception:  # noqa: BLE001 — closed registry → no provider to list with
        return None


async def _list_models(provider, current: str) -> str:
    try:
        models = await provider.list_models()
    except ProviderError as exc:
        return f"Could not list models: {exc}"
    if not models:
        return "The endpoint reported no models."
    lines = ["Models at the endpoint:"]
    for model in models:
        if model == current:
            lines.append(f"  * {model}  (current)")
        else:
            lines.append(f"    {model}")
    if current not in models:
        lines.append(
            f"\nConfigured model {current!r} is not in the list (switch with /model <name>)."
        )
    return "\n".join(lines)


COMMANDS: tuple[SlashCommand, ...] = (
    SlashCommand(
        "model", "switch model / list models at the endpoint", "/model [name]", _cmd_model
    ),
)
