"""TurnEngine (IC-502 + IC-605): the deterministic turn loop.

State machine for one user turn (SPEC §5, docs/ARCHITECTURE.md #3):

    COMPOSE   build the context window from harness-owned state via
              ``composer.compose`` — system prompt + anchors + working-set +
              compacted history + input. The model NEVER has to remember; we
              re-present (SPEC §5.2, IC-501).
    CALL      stream from the provider using the envelope-selected tool
              protocol and sampling policy (IC-605/IC-607).
    PARSE     extract tool calls — native provider ``tool_calls`` OR, on the
              ``text_protocol`` floor, ``ironcall.parse`` of the completion
              text. Malformed output → REPAIR (SPEC §5.4, IC-503).
    GATE      ``safety.policy.decide`` → command policy (EXEC) → jail
              (out-of-workspace READ, SAFETY T4) → injection downgrade. ``ask``
              emits ``ApprovalRequired`` and awaits the broker; ``deny`` frames
              a refusal back to the model.
    EXECUTE   snapshot before the first mutation, run the tool, truncate + wrap
              the output as untrusted DATA, flag injection for the next gate.
    OBSERVE   append the result; loop to CALL until the model stops requesting
              tools or a budget/loop cap trips (SPEC §5.6, IC-506).
    VERIFY    after WRITE/EXEC activity, run the verifier (SPEC §5.5, IC-504).
    DONE      emit ``TurnCompleted`` with an EVIDENCE-BASED ``stop_reason``.

Invariants (frozen — docs/CONTRACTS.md #Engine):
* No tool executes without a GATE decision (``safety.policy.decide``).
* Every provider call goes through ``composer.compose`` — no ad-hoc messages.
* The engine is UI-agnostic: it emits ``core.events`` and awaits approval
  futures; it never prints or prompts.
* ``TurnCompleted.stop_reason`` is computed from tool evidence, never model text.

The four judgement calls (repair / verify / budget / micro-step) are delegated
to the collaborators in ``core.protocols``; the defaults make this fully
runnable today, and IC-503..506 swap them in.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

from ironcore.config.settings import Settings
from ironcore.core import guided, ironcall
from ironcore.core.approvals import ApprovalBroker
from ironcore.core.budgets import Budget
from ironcore.core.compact import compact, should_compact
from ironcore.core.composer import RESPONSE_HEADROOM_SHARE, compose, load_project_memory
from ironcore.core.events import (
    ApprovalRequired,
    Event,
    TextDelta,
    ToolCallFinished,
    ToolCallRequested,
    TurnCompleted,
    TurnError,
    TurnStarted,
)
from ironcore.core.protocols import (
    BudgetTracker,
    RepairAction,
    RepairPolicy,
    StepPlanner,
    Verifier,
)
from ironcore.core.repair import LadderRepairPolicy, frame_error
from ironcore.core.sampling import resolve_sampling
from ironcore.core.state import SessionState, state_path
from ironcore.core.steps import PlanStepPlanner
from ironcore.core.verify import CommandVerifier
from ironcore.envelope.profile import CapabilityProfile
from ironcore.memory.handoff import Handoff, append_handoff, handoff_from_summary
from ironcore.providers.base import Message, Provider, ToolCall
from ironcore.providers.openai_compat import ProviderError
from ironcore.safety.commands import classify_command
from ironcore.safety.injection import (
    UNTRUSTED_PREAMBLE,
    Flag,
    detect_injection,
    downgrade_for_flag,
    wrap_untrusted,
)
from ironcore.safety.jail import JailViolation, is_inside, resolve_jailed
from ironcore.safety.modes import Mode
from ironcore.safety.policy import Decision, decide
from ironcore.safety.risk import ToolRisk
from ironcore.safety.snapshots import SnapshotError
from ironcore.tools.base import Tool, ToolRegistry

if TYPE_CHECKING:  # annotations only — the router is injected, never constructed here
    from ironcore.core.roles import RoleRouter

#: Short, honest floor system prompt. IRONCORE.md / envelope templates layer on
#: top later (IC-501 memory, IC-802 /init); this is the always-present base.
DEFAULT_SYSTEM_PROMPT = (
    "You are IronCore, a terminal coding agent working inside a user's project "
    "workspace. Use the available tools to inspect files, make changes, and run "
    "commands; the harness applies edits and runs commands for you and returns "
    "their results. Work in small, verifiable steps and do only what the user "
    "asked. Prefer reading before writing. When you are done, stop calling tools "
    "and give a short summary."
)

#: Engine-side cap on a single tool's output before it is wrapped and fed back
#: (tools truncate too; this is defense-in-depth for the context budget).
MAX_TOOL_OUTPUT_CHARS = 20_000

#: Working-set re-presentation caps (SPEC §5.2): MRU-touched files only.
_WORKING_SET_MAX_FILES = 8
_WORKING_SET_MAX_BYTES = 64_000

#: On compaction (SPEC §11.2), keep this many most-recent messages verbatim
#: after the distilled summary.
_KEEP_RECENT = 6

#: fs tools whose ``path`` arg names a single file worth carrying in the working set.
_FS_PATH_TOOLS = frozenset({"read_file", "write_file", "edit_file"})

#: Default handoff file, written under the workspace on compaction / session end.
HANDOFF_FILENAME = "HANDOFF.md"

#: Sentinel distinguishing "caller passed no handoff_path" (→ ``<ws>/HANDOFF.md``)
#: from an explicit ``None`` (→ handoff writing disabled). See ``TurnEngine``.
_AUTO_HANDOFF: object = object()


def _merge_usage(total: dict[str, int], usage: dict) -> None:
    """Accumulate integer usage counters (prompt/completion/total tokens)."""
    for key, value in usage.items():
        if isinstance(value, int):
            total[key] = total.get(key, 0) + value


class TurnEngine:
    """Drives one user turn to completion, emitting ``core.events`` (SPEC §5).

    Collaborators default to the ``core.protocols`` Default* implementations, so
    the engine is fully runnable and testable without IC-503..506. ``approvals``
    defaults to a fresh broker; ``snapshots`` is optional (mutating turns simply
    skip the shadow-git snapshot when it is ``None``).
    """

    def __init__(
        self,
        provider: Provider,
        tools: ToolRegistry,
        settings: Settings,
        profile: CapabilityProfile,
        mode: Mode = Mode.MANUAL,
        *,
        workspace: str | Path,
        approvals: ApprovalBroker | None = None,
        snapshots: object | None = None,
        repair: RepairPolicy | None = None,
        verifier: Verifier | None = None,
        budget: BudgetTracker | None = None,
        planner: StepPlanner | None = None,
        session: SessionState | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        handoff_path: str | Path | None = _AUTO_HANDOFF,  # type: ignore[assignment]
        author: str | None = None,
        roles: RoleRouter | None = None,
    ) -> None:
        self.provider = provider
        self.tools = tools
        self.settings = settings
        self.profile = profile
        self.mode = mode
        #: per-role (provider, profile) routing (MS-3, additive — CONTRACTS §4);
        #: None (the default) keeps every call on the primary pair.
        self.roles = roles
        self.workspace = Path(workspace)
        self.approvals = approvals if approvals is not None else ApprovalBroker()
        self.snapshots = snapshots
        # The full phase-5 collaborators are the defaults; callers may still
        # inject their own (or the simpler protocols.Default* impls) for tests.
        self.repair: RepairPolicy = repair if repair is not None else LadderRepairPolicy()
        self.verifier: Verifier = verifier if verifier is not None else CommandVerifier()
        self.budget: BudgetTracker = budget if budget is not None else Budget()
        self.planner: StepPlanner = planner if planner is not None else PlanStepPlanner()
        self.system_prompt = system_prompt
        # Handoff sink (SPEC §11.3): default to <workspace>/HANDOFF.md, an explicit
        # None disables all handoff writes. Author labels the block (session/model id).
        if handoff_path is _AUTO_HANDOFF:
            self.handoff_path: Path | None = self.workspace / HANDOFF_FILENAME
        elif handoff_path is None:
            self.handoff_path = None
        else:
            self.handoff_path = Path(handoff_path)
        self.handoff_author = author or f"ironcore/{self.profile.model_id or 'unknown'}"
        if session is not None:
            self.state = session
        else:
            self.state, _warning = SessionState.load(state_path(self.workspace))
        #: running session message list (in-memory; transcript JSONL is IC-706).
        self._conversation: list[Message] = []
        #: injection verdict on the PREVIOUS tool output, carried across the loop.
        self._pending_flag: Flag = Flag.NONE

    def repoint(self, provider: Provider, profile: CapabilityProfile) -> None:
        """Live model swap (MS-2, additive — docs/CONTRACTS.md §4): re-point the
        provider + capability profile between provider calls and follow the new
        model in ``handoff_author``. Protocol selection still flows exclusively
        through ``profile.recommended_*`` (read at turn start). The OLD provider
        stays open by design — the ``ProviderRegistry`` owns provider lifecycle
        (``close_all``), and a kept instance makes swap-back instant."""
        self.provider = provider
        self.profile = profile
        self.handoff_author = f"ironcore/{profile.model_id or 'unknown'}"

    def _binding(self, role: str) -> tuple[Provider, CapabilityProfile]:
        """The ACTIVE ``(provider, profile)`` pair for ``role`` (MS-3).

        A routed role gets its own provider AND that model's own envelope; an
        unrouted role (or no router at all) follows the engine's primary pair —
        so ``repoint`` swaps and background seed/probe hot-swaps keep flowing
        to every unrouted role, and zero-config behavior is unchanged."""
        if self.roles is not None:
            routed = self.roles.resolve(role)
            if routed is not None:
                return routed
        return self.provider, self.profile

    # -- the loop -------------------------------------------------------------

    async def run_turn(self, user_input: str) -> AsyncIterator[Event]:
        """Drive one user turn to completion, yielding events as they occur."""
        state = self.state
        state.mode = self.mode
        turn = state.turn_count
        turn_id = f"t{turn}"
        self._pending_flag = Flag.NONE
        self.approvals.begin_turn(turn)
        self.budget.start_turn()
        yield TurnStarted(turn_id=turn_id, mode=self.mode.value)

        self._conversation.append(Message(role="user", content=user_input))

        # Per-role routing (MS-3): the main-loop call is the PLANNER when the
        # session is in PLAN mode (the engine's read-only think/propose step)
        # and the CODER otherwise; compaction routes to the SUMMARIZER at its
        # call site below. The ACTIVE pair is resolved once per turn; protocol,
        # window budgets, and sampling all read the ACTIVE profile, so a routed
        # role runs on ITS measured envelope, not the primary model's.
        loop_role = "planner" if self.mode is Mode.PLAN else "coder"
        provider, profile = self._binding(loop_role)
        protocol = profile.recommended_tool_protocol()
        repair_attempt = 0
        any_executed = False
        any_denied = False
        did_write = False
        did_mutate = False
        snapshotted = False
        verify_fed_back = False
        compacted = False
        stop_reason = "done"
        usage_total: dict[str, int] = {}
        last_text = ""
        fatal: dict | None = None

        try:
            while True:
                cap = self.budget.check()
                if cap is not None:
                    stop_reason = cap
                    break

                # COMPACT (SPEC §11.2): ONCE per turn, under context pressure,
                # distill older history into one handoff-grade summary + the
                # recent tail. The summarizer call counts against the budget so a
                # compaction storm can't bypass runaway protection (SPEC §5.6/T5).
                if not compacted and should_compact(self._conversation, profile=profile):
                    compacted = True
                    summary = await compact(
                        self._conversation,
                        provider=self._binding("summarizer")[0],
                        model=self.settings.roles.summarizer or "",
                    )
                    self._conversation = [summary, *self._conversation[-_KEEP_RECENT:]]
                    self.budget.record_call(0)
                    # A compaction IS a handoff-grade summary (SPEC §11.3): persist
                    # it. Once per turn (the `not compacted` guard above), best-effort
                    # — a failed write must never crash the turn or alter its events.
                    self._write_compaction_handoff(summary)

                text_protocol = protocol == "text_protocol"
                is_guided = protocol == "strict_json"
                # The text floor and the guided strict_json rung both emit the
                # call in-band (no native tool specs) and are fed results with the
                # same ironresult framing; only their compose/parse shapes differ.
                text_frame = text_protocol or is_guided
                # IRONCORE.md project memory rides the system share (IC-1003);
                # re-read per turn so mid-session /init and /memory edits land.
                memory = load_project_memory(self.workspace, profile=profile)
                messages = compose(
                    state,
                    profile=profile,
                    settings=self.settings,
                    system_prompt=self._system_prompt(protocol, profile=profile),
                    working_set=self._working_set(),
                    history=self._conversation,
                    user_input="",
                    memory=memory,
                )
                sampling = resolve_sampling(profile, kind="tool", attempt=repair_attempt)
                sampling = replace(sampling, max_tokens=self._headroom_tokens(profile))
                tool_specs = None if text_frame else self.tools.specs()
                # strict_json constrains the server to emit exactly one JSON tool
                # call; the model emits it directly, so we send no native specs.
                response_format = (
                    guided.tool_call_response_format(self.tools.specs()) if is_guided else None
                )

                # -- CALL (stream) --------------------------------------------
                text_parts: list[str] = []
                native_calls: list[ToolCall] = []
                usage: dict = {}
                stream_error: dict | None = None
                # Forward response_format ONLY when guided constrains it; a
                # non-guided call stays byte-identical (some providers/test
                # doubles predate the seam and accept no such kwarg).
                stream_kwargs: dict = {"tools": tool_specs, "sampling": sampling}
                if response_format is not None:
                    stream_kwargs["response_format"] = response_format
                async for ev in provider.stream(messages, **stream_kwargs):
                    if ev.kind == "text":
                        text_parts.append(ev.text)
                        # Guided text IS the JSON tool-call scaffold, not prose:
                        # accumulate it into full_text but never stream it out.
                        if not is_guided:
                            yield TextDelta(turn_id=turn_id, text=ev.text)
                    elif ev.kind == "tool_call" and ev.tool_call is not None:
                        native_calls.append(ev.tool_call)
                    elif ev.kind == "usage":
                        usage = dict(ev.data)
                    elif ev.kind == "error":
                        stream_error = dict(ev.data)
                        break
                full_text = "".join(text_parts)
                last_text = full_text or last_text
                _merge_usage(usage_total, usage)
                self.budget.record_call(int(usage.get("total_tokens", 0)))

                # non-repairable transport/provider failure ends the turn hard.
                if stream_error is not None and not stream_error.get("repairable", False):
                    fatal = stream_error
                    break

                # -- PARSE ----------------------------------------------------
                repair_error: str | None = None
                repair_raw = ""
                calls: list[ToolCall] = []
                if stream_error is not None:  # repairable stream error
                    repair_error = self._stream_repair_message(stream_error)
                    repair_raw = str(stream_error.get("raw", full_text))
                elif text_protocol:
                    parsed = ironcall.parse(full_text)
                    if parsed.warning:
                        yield TextDelta(turn_id=turn_id, text=f"\n[repair] {parsed.warning}\n")
                    if parsed.error is not None:
                        repair_error, repair_raw = parsed.error, full_text
                    else:
                        calls = parsed.calls
                elif is_guided:
                    gparsed = guided.parse_guided_tool_call(full_text)
                    if gparsed.error is not None:
                        # a malformed body feeds the existing repair loop; a
                        # LADDER_DOWN from strict_json pins protocol to the text
                        # floor, which is exactly the correct fallback.
                        repair_error, repair_raw = gparsed.error, full_text
                    elif gparsed.done:
                        # the model finished: show its summary prose (not the raw
                        # JSON) and fall into the existing "no calls -> stop" path.
                        yield TextDelta(turn_id=turn_id, text=gparsed.message)
                        calls = []
                    elif gparsed.call is not None:
                        calls = [gparsed.call]
                else:
                    calls = native_calls

                self._conversation.append(
                    Message(
                        role="assistant",
                        content=full_text,
                        tool_calls=list(native_calls) if not text_frame else [],
                    )
                )

                # -- REPAIR (SPEC §5.4) ---------------------------------------
                if repair_error is not None:
                    action = self.repair.decide(
                        attempt=repair_attempt, error=repair_error, raw=repair_raw, rung=protocol
                    )
                    yield TextDelta(turn_id=turn_id, text=f"\n[repair] {repair_error}\n")
                    if action == RepairAction.GIVE_UP:
                        stop_reason = "error"
                        break
                    if action == RepairAction.LADDER_DOWN:
                        protocol = "text_protocol"
                    repair_attempt += 1
                    self._conversation.append(
                        Message(
                            role="user",
                            content=frame_error(repair_error, repair_raw, protocol),
                        )
                    )
                    continue

                # -- no tool calls → the model wants to stop -----------------
                if not calls:
                    # VERIFY (SPEC §5.5): after mutations, run the checker; feed a
                    # failure back to the model ONCE, then surface honestly. The
                    # stop_reason stays evidence-based; a failing verify is
                    # reported, never silently swallowed (SAFETY T7).
                    if did_mutate:
                        # verify after ANY mutation (WRITE or EXEC), SPEC §5.5
                        vr = await self.verifier.verify(
                            self.workspace, self.settings, state, did_mutate
                        )
                        if not vr.ok:
                            yield TextDelta(turn_id=turn_id, text=f"\n[verify] {vr.summary}\n")
                            if not verify_fed_back:
                                verify_fed_back = True
                                self._conversation.append(
                                    Message(
                                        role="user",
                                        content=(
                                            "Verification failed after your changes:\n"
                                            f"{vr.summary}\nFix it, then stop."
                                        ),
                                    )
                                )
                                continue
                            # fed back once and STILL failing: the engine must not
                            # report success on unverified work (SAFETY T7, SPEC §5.5).
                            stop_reason = "goal-unmet"
                            break
                    stop_reason = "denied" if (any_denied and not any_executed) else "done"
                    break

                # -- GATE + EXECUTE each requested call -----------------------
                loop_stop = False
                for call in calls:
                    reason = self.budget.note_tool(call.name, call.arguments)
                    if reason is not None:
                        stop_reason = reason
                        loop_stop = True
                        break

                    tool = self.tools.get(call.name)
                    if tool is None:
                        any_denied = True
                        yield ToolCallRequested(
                            turn_id=turn_id, call=call, risk="unknown", decision="deny"
                        )
                        self._feed_refusal(call, text_frame, f"unknown tool {call.name!r}")
                        continue

                    decision = self._gate(call, tool)
                    yield ToolCallRequested(
                        turn_id=turn_id, call=call, risk=tool.risk.value, decision=str(decision)
                    )

                    if decision == Decision.ASK:
                        preview = self._preview(call, tool)
                        yield ApprovalRequired(
                            turn_id=turn_id, call=call, risk=tool.risk.value, preview=preview
                        )
                        answer = await self.approvals.request(
                            preview, risk=tool.risk.value, turn=turn, key=tool.name
                        )
                        if answer.decision != "approve":
                            any_denied = True
                            self._feed_refusal(
                                call, text_frame, answer.reason or "denied by user"
                            )
                            continue
                    elif decision == Decision.DENY:
                        any_denied = True
                        self._feed_refusal(
                            call, text_frame, "denied by policy in the current mode"
                        )
                        continue

                    # -- EXECUTE ----------------------------------------------
                    if tool.risk in (ToolRisk.WRITE, ToolRisk.EXEC):
                        did_mutate = True
                        did_write = did_write or tool.risk is ToolRisk.WRITE
                        if self.snapshots is not None and not snapshotted:
                            snapshotted = True
                            try:
                                self.snapshots.snapshot(f"turn {turn}: before edits")
                            except SnapshotError as exc:
                                yield TextDelta(
                                    turn_id=turn_id, text=f"\n[snapshot skipped: {exc}]\n"
                                )

                    result = await tool.run(**call.arguments)
                    any_executed = True
                    if tool.name in _FS_PATH_TOOLS and result.ok:
                        rel = self._relpath(call.arguments.get("path"))
                        if rel is not None:
                            state.touch(rel)
                    raw_output = result.output or ""
                    self._pending_flag = detect_injection(raw_output)
                    wrapped = wrap_untrusted(self._truncate(raw_output), source=tool.name)
                    self._conversation.append(
                        self._tool_message(call, wrapped, text_frame, ok=result.ok)
                    )
                    yield ToolCallFinished(turn_id=turn_id, call=call, result=result)

                if loop_stop:
                    break
                # OBSERVE: loop back to CALL.
        except ProviderError as exc:
            fatal = {"reason": "provider_error", "message": str(exc)}

        # -- micro-step (VERIFY now runs inside the loop, at the clean stop) ---
        if fatal is None and state.plan_steps and any_executed:
            self.planner.advance(state, last_text or "tool activity")

        # -- DONE -------------------------------------------------------------
        state.turn_count += 1
        self.approvals.end_turn()
        try:
            state.save(state_path(self.workspace))
        except OSError:
            pass  # state persistence is best-effort; a full disk must not crash the turn

        if fatal is not None:
            message = str(fatal.get("message") or fatal.get("reason") or "provider error")
            yield TurnError(turn_id=turn_id, message=message, data=dict(fatal))
            return
        yield TurnCompleted(turn_id=turn_id, usage=usage_total, stop_reason=stop_reason)

    # -- handoff lifecycle (SPEC §11.3) ---------------------------------------

    def _write_compaction_handoff(self, summary: Message) -> None:
        """Append a handoff derived from a compaction summary. Best-effort and
        decoupled: a ``None`` sink is a no-op, and an ``OSError`` (full disk, bad
        path) is swallowed so the turn is never disturbed. The summary already
        carries the five handoff sections, so ``handoff_from_summary`` just re-homes
        them; a mechanical-fallback digest is wrapped whole into Context."""
        if self.handoff_path is None:
            return
        handoff = handoff_from_summary(
            self.handoff_author,
            summary.content,
            next_steps="resume this session to continue (ironcore --resume).",
        )
        try:
            append_handoff(self.handoff_path, handoff)
        except OSError:
            pass  # persistence is best-effort; never crash the turn

    def end_session(self) -> Handoff:
        """Write a FINAL handoff for this session and return it (SPEC §11.3).

        The TUI/CLI calls this once on quit. It never runs the model: the block is
        built deterministically from the goal and last known state, so it is safe to
        call with no activity at all (fresh state → a valid "nothing happened yet"
        block). The write is best-effort (disabled when ``handoff_path`` is None,
        ``OSError`` swallowed); the Handoff is returned either way so a caller can
        still log or display it."""
        state = self.state
        context = (
            f"Session ended at turn {state.turn_count} in {self.mode.value} mode. "
            f"Goal: {state.goal or '(none recorded)'}."
        )
        working = ", ".join(state.working_set[:_WORKING_SET_MAX_FILES]) or "none"
        changed = f"Working set (MRU): {working}."
        if state.plan_steps:
            done = min(state.plan_cursor, len(state.plan_steps))
            changed += f" Plan: {done}/{len(state.plan_steps)} step(s) done."
        notes: list[str] = []
        if self._pending_flag is not Flag.NONE:
            notes.append(f"last tool output flagged {self._pending_flag.name} for injection")
        if state.plan_steps and state.plan_cursor < len(state.plan_steps):
            notes.append("plan not complete")
        handoff = Handoff(
            author=self.handoff_author,
            context=context,
            changed=changed,
            verified="not verified — session-end handoff (no verification run here)",
            next_steps="resume this session to continue (ironcore --resume).",
            gotchas="; ".join(notes) or "none",
        )
        if self.handoff_path is not None:
            try:
                append_handoff(self.handoff_path, handoff)
            except OSError:
                pass  # best-effort; quitting must not fail on a bad handoff path
        return handoff

    # -- gate -----------------------------------------------------------------

    def _gate(self, call: ToolCall, tool: Tool) -> Decision:
        """Compose the gate: mode policy → command policy → jail-read → injection.

        EXEC tightens via ``classify_command`` (which composes ``decide`` itself);
        an out-of-workspace READ escalates ALLOW→ASK (SAFETY T4); a HOT/SUSPICIOUS
        flag on the PREVIOUS tool output downgrades the next ALLOW→ASK in AUTO.
        Layering is tighten-only by construction.
        """
        risk = tool.risk
        if risk is ToolRisk.EXEC:
            command = call.arguments.get("command")
            if isinstance(command, str) and command.strip():
                decision = classify_command(command, self.mode)
            else:
                decision = decide(self.mode, risk)
        else:
            decision = decide(self.mode, risk)
            if risk is ToolRisk.READ and decision is Decision.ALLOW:
                path = call.arguments.get("path")
                if isinstance(path, str) and path and not is_inside(self.workspace, path):
                    decision = Decision.ASK  # reads outside the jail may leak secrets
        return downgrade_for_flag(self._pending_flag, self.mode, decision)

    # -- context assembly -----------------------------------------------------

    def _system_prompt(
        self,
        protocol: str = "native",
        *,
        text_protocol: bool | None = None,
        profile: CapabilityProfile | None = None,
    ) -> str:
        """Base prompt + the standing untrusted-data rule; steer the model to the
        edit format the envelope measured as most reliable for it (IC-605). On the
        ``text_protocol`` floor prepend the IRONCALL teaching fragment (SPEC §6.3);
        on the ``strict_json`` rung prepend the guided-JSON fragment — both hand
        the model the tool docs the wire form itself omits.

        ``protocol`` is the active tool-call rung. ``text_protocol`` is a
        backward-compat bool alias for existing callers (True -> the text floor);
        new code passes ``protocol``. ``profile`` is the ACTIVE role's envelope
        (MS-3) — the edit-format steer must match the model actually called;
        ``None`` keeps the primary ``self.profile`` (back-compat)."""
        if text_protocol is not None:
            protocol = "text_protocol" if text_protocol else "native"
        if profile is None:
            profile = self.profile
        prompt = f"{self.system_prompt}\n\n{UNTRUSTED_PREAMBLE}"
        if self.tools.get("edit_file") is not None:
            fmt = profile.recommended_edit_format()
            prompt += (
                f"\n\nWhen you change a file with edit_file, prefer format={fmt!r} — "
                "it is the edit format this model applies most reliably."
            )
        if protocol == "text_protocol":
            fragment = ironcall.render_system_fragment(self.tools.specs())
            prompt = f"{fragment}\n\n{prompt}"
        elif protocol == "strict_json":
            fragment = guided.render_json_system_fragment(self.tools.specs())
            prompt = f"{fragment}\n\n{prompt}"
        return prompt

    def _headroom_tokens(self, profile: CapabilityProfile | None = None) -> int:
        """Response budget the composer reserves (15% of honest context). The
        ACTIVE role's profile (MS-3) sizes it; ``None`` = the primary profile."""
        if profile is None:
            profile = self.profile
        return max(256, int(profile.honest_context * RESPONSE_HEADROOM_SHARE))

    def _working_set(self) -> dict[str, str]:
        """MRU-touched workspace files, re-presented as DATA (SPEC §5.2)."""
        ws: dict[str, str] = {}
        for rel in self.state.working_set[:_WORKING_SET_MAX_FILES]:
            try:
                target = resolve_jailed(self.workspace, rel)
            except JailViolation:
                continue
            try:
                text = target.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            ws[Path(rel).as_posix()] = text[:_WORKING_SET_MAX_BYTES]
        return ws

    def _relpath(self, path: object) -> str | None:
        """Workspace-relative posix path if ``path`` is inside the jail, else None."""
        if not isinstance(path, str) or not path:
            return None
        try:
            resolved = resolve_jailed(self.workspace, path)
        except JailViolation:
            return None
        try:
            return resolved.relative_to(self.workspace.resolve()).as_posix()
        except ValueError:
            return None

    # -- feeding results back to the model ------------------------------------

    def _tool_message(
        self, call: ToolCall, content: str, text_frame: bool, *, ok: bool
    ) -> Message:
        """Frame a tool outcome for the model: an ``ironresult`` block on the text
        floor or the guided ``strict_json`` rung (``text_frame`` true), a native
        ``tool`` role message for native function-calling."""
        if text_frame:
            return Message(role="user", content=ironcall.render_result(call.id, content, ok))
        return Message(role="tool", content=content, tool_call_id=call.id, name=call.name)

    def _feed_refusal(self, call: ToolCall, text_frame: bool, reason: str) -> None:
        """Append a framed refusal so a denied call is answered, not left dangling."""
        self._conversation.append(
            self._tool_message(call, f"[denied] {reason}", text_frame, ok=False)
        )

    @staticmethod
    def _stream_repair_message(err: dict) -> str:
        """Model-facing repair message from a repairable stream error event."""
        reason = err.get("reason", "malformed output")
        raw = err.get("raw")
        if raw:
            return f"your tool call was malformed ({reason}); the unparsable text was: {raw}"
        return f"your response was cut off or malformed ({reason}); send it again, complete."

    @staticmethod
    def _truncate(text: str) -> str:
        if len(text) <= MAX_TOOL_OUTPUT_CHARS:
            return text
        dropped = len(text) - MAX_TOOL_OUTPUT_CHARS
        return text[:MAX_TOOL_OUTPUT_CHARS] + f"\n... [truncated: {dropped} more chars]"

    def _preview(self, call: ToolCall, tool: Tool) -> str:
        """Human-readable exact effect for the approval modal (SAFETY §4)."""
        args = call.arguments
        if tool.risk is ToolRisk.EXEC:
            return f"$ {args.get('command', '')}"
        if tool.risk is ToolRisk.NET:
            return f"{args.get('method', 'GET')} {args.get('url', '')}"
        if tool.name == "write_file":
            content = args.get("content", "")
            size = len(content) if isinstance(content, str) else 0
            return f"write_file {args.get('path', '')} ({size} bytes)"
        if tool.name == "edit_file":
            path, fmt, edit = args.get("path", ""), args.get("format", ""), args.get("edit", "")
            return f"edit_file {path} [{fmt}]\n{edit}"
        if tool.risk is ToolRisk.READ:
            return f"{tool.name} {args.get('path', '')} (outside workspace)"
        return f"{tool.name} {args}"
