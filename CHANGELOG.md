# Changelog

All notable changes to IronCore are documented here. This project adheres to
[Semantic Versioning](https://semver.org/).

## [0.2.2] — 2026-07-18

### Changed
- **The PyPI distribution is named `ironcore-cli`.** PyPI refused the bare name
  `ironcore` as "too similar to an existing project" — its similarity guard
  collapses separators, so the unrelated `iron-core` (Iron.io API wrappers)
  occupies the same slot. Only the distribution name moved: the console script
  is still `ironcore`, the import package is still `ironcore`, the repository is
  still `IronCore`, and no code changed. `pip install ironcore-cli` then
  `ironcore doctor`. This is the first version published to PyPI; 0.2.1 and
  earlier are installable from the GitHub releases and from source.

## [0.2.1] — 2026-07-18

The fit-for-strangers release. 0.2.0 shipped the features; a six-lens release audit
then found that the seam a newcomer actually touches was the weak part — `doctor`
reported all-green against a server that could not serve a token, the offline demo
never reached an installed environment, an interrupted first-run probe could brick
the next boot, and `docs/SAFETY.md` documented a project-config autonomy ceiling the
code did not implement. All of that is fixed and regression-tested here. The app also
got a real visual design: one palette, colour used semantically and only semantically,
and nine screenshots in the README that show what you actually get.

### Added
- **Slash commands can return styled text, so the report card reads its own
  verdicts** (CONTRACTS.md §6, additive). A handler returns `str` *or*
  `rich.text.Text`; `str` stays the default and every existing command is
  unchanged. `commands.plain(result)` is the accessor for anything that wants
  characters rather than spans. The mechanism exists because the two screens
  that carry a *verdict* had no way to say so: `/envelope` rendered
  `SELECTED` and `REJECTED (0.19 short)` in exactly the same grey, on the one
  image the README uses to prove the product's thesis.
  - **`/envelope`** is now colour-coded by outcome. The rung a measurement
    selected is green, a rejected rung and its shortfall are red, the floor and
    the also-ran fallback recede to grey, the honest-vs-advertised context gap is
    graded, and the section headings and verdict carry weight. Green means one
    thing only — *a real measurement cleared a bar* — so an unprobed or seeded
    profile shows none of it: its floor selection, its verdict and its context
    ratio all render amber and grey, because nothing there has been measured yet.
  - **`/goal check`** colours its payoff line: `Goal stop-condition MET` green,
    `UNMET` red. The line whose entire purpose is proving "done" is a test result
    used to arrive in the same grey as the ack above it.
  - Colour never carries meaning alone, and the plain-text card is unchanged to
    the byte: `render_report_card(profile)` is now literally
    `render_report_card_text(profile).plain`, so the coloured card and the ASCII
    one that gets piped, redirected or pasted into a GitHub issue cannot drift.
    It stays ASCII, keeps every word, and every existing test pins both views.
  - **Safety:** styled results are composed programmatically (`Text.append` with
    an explicit style) and never via `Text.from_markup`. A model id of
    `[red]evil[/]` prints those characters and arms no colour — pinned by
    `tests/test_report_card.py::test_report_card_never_interprets_markup`.

### Changed
- **`ironcore demo`, `doctor` and the report card read as the same product as the
  app.** The TUI got a palette; everything printed *outside* it was still one
  undifferentiated grey, with `======` and `+-`/`|` ASCII furniture standing in
  for structure. `ironcore/term.py` now carries the same palette and the same
  rules the app theme states, and the two are pinned together by
  `tests/test_term.py` so they cannot drift apart.
  - **`ironcore demo`** renders the session the way the TUI renders it: a real
    rule under the masthead, the autonomy mode as a chip, the request in the
    accent, and each tool call as a card with a risk-coloured rule down its left
    edge, a `READ`/`WRITE` chip, muted arguments, a red/green diff and a
    green `✓ ok` / red `✗ error`. `verify passed` and `stop_reason: done` are
    green because they are the evidence the run turned on.
  - **`ironcore doctor`** colours its marker column — green `[ok]`, steel `[--]`,
    amber `[!!]`, bold red `[FAIL]` — and dims the indented follow-up under each
    finding so advice reads as attached to the line it belongs to. The wording,
    the markers and the exit codes are untouched: styling is derived from the
    text, so it cannot reword what doctor says. The one exception is noted under
    Fixed below.
  - **The `/envelope` report card** gains an outcome column: every ladder rung
    now says `SELECTED`, `ok, fallback`, `REJECTED (0.19 short)` or
    `floor (always works)` instead of leaving a bare `below` as the only sign a
    rung was thrown out, and the columns line up. It stays ASCII and plain-text
    on purpose — it is pasted into issues and rendered by the transcript.
  - Colour follows the stream: on a terminal you get it, piped or redirected you
    do not, so `ironcore doctor > report.txt` is plain text and every string the
    test suite pins is byte-identical. `NO_COLOR` and `FORCE_COLOR` are obeyed
    ([docs/CONFIG.md](docs/CONFIG.md) §10). Box-drawing degrades to ASCII as a
    set on a stream that cannot encode it (a redirected Windows console is
    cp1252), so a redirect can never fail halfway through a transcript.
  - The screenshot generator captures the CLI shots *with* colour, by asking a
    piped run for exactly the byte stream an interactive terminal would get; and
    it now widens the exported SVG's font stack to terminal fonts that are
    actually installed, so box-drawing rasterizes as continuous lines instead of
    the dotted ones Courier New produced.
- **The report card no longer invents measurements it does not have.** A ladder
  rung that was never scored said `REJECTED (0.95 short)`, which reads as a
  measurement that failed; it now says `not probed`, distinct from a probe that
  really ran and scored 0.0.
- **The TUI has a coherent visual design.** It rendered as one flat grey wall:
  tool cards were dim monochrome blue with no structure, mode changes and gate
  outcomes were the same colour as everything else, and the approval modal's
  `thick` border drew as a chunky orange checkerboard. There is now one palette
  (`ironcore/tui/theme.py` — a cool slate ground, one ember accent, semantic
  green/red/blue) registered as a real `textual.theme.Theme`, so the whole app
  spends design tokens instead of ad-hoc colours.

  Colour is **semantic only**, and escalates rather than decorates — the calm,
  common case stays flat and the elevated case fills, so a single `WRITE` card
  stands out of a wall of `READ` cards and the current autonomy posture is
  unmissable. Nothing is encoded in colour *alone*: every chip keeps its word,
  every result keeps its `ok`/`error` text, every diff line keeps its `+`/`-`,
  and Rich drops colour automatically when stdout is not a TTY. Specifically:
  - **Tool cards** get a risk-coloured left rule and a faint panel (not a
    border — a bordered box costs two columns per card and a scrolling column
    of boxes reads as a form), a bold tool name, a `READ`/`WRITE`/`EXEC`/`NET`
    chip, dim arguments and a green/red result line.
  - **The approval modal** loses the `thick` border for a risk-coloured `round`
    one with a border title, and gains a plain-language statement of what the
    risk class actually does ("this changes files in your workspace"). Its three
    actions are flat text so the diff stays the loudest thing on screen, and the
    scrim is translucent so the tool card that raised the ask stays readable.
  - **The status bar's mode chip** is coloured by autonomy: `plan` blue,
    `manual` grey, `accept-edits` filled amber, `auto` filled red.
  - **Mode changes** are announced with the same colour the chip just took.
  - **The transcript** gets vertical rhythm between turns, and system notes have
    their leading `[tag]` lifted out (red when the tag reports a failure).
  - **The empty state** is a three-line masthead instead of a single grey line,
    and the transcript is bottom-aligned like a shell, so a short session sits
    above the input rather than stranded against a void.
- **Slash commands are echoed into the transcript.** A command session rendered
  as an unbroken column of grey results with the question missing — you could
  not tell which output answered what. The typed line is now shown as your own
  line above its result. Display only: slash commands are still not recorded to
  the session transcript.
- **The README screenshots are reshot against the new design, and four of the
  nine now show a fuller session.** Several shots framed a modal or a command
  against a mostly empty transcript, which read as a dead app rather than as
  work in progress: the approval gate and the `/goal` run now open on a real
  session, the safety-mode cycle is followed by the two-turn investigation
  instead of a single turn, and the resume picker lists nine recorded sessions
  rather than four. These are scenario changes in `tools/make_screenshots.py`
  only — every pixel is still a render of the shipping UI, and nothing renders
  differently for capture.

### Fixed
- **Doctor's "unprobed" line no longer wraps through its own marker column.** It
  was a single 133-character sentence, so in any normal terminal it folded back
  to column 0 and broke the one column a reader scans. It is now a finding plus
  an indented remedy, the shape every other multi-line check already used.
- **The README no longer documents an install command that 404s.** The Install
  section offered a concrete versioned wheel URL
  (`…/releases/download/v0.2.0/ironcore-0.2.0-py3-none-any.whl`) hedged with
  "once a release is tagged". There are no tags and no releases, so that URL was
  dead — verified 404 — and a skimmer copies the concrete command, not the hedge.
  Replaced with a link to the releases page phrased to read correctly both before
  and after the first release exists, so it cannot rot into a lie either way.
- **A git-free install path is documented.** `pip install git+https://…` was the
  only documented path and it shells out to git to clone, which contradicted the
  neighbouring claim that git is a soft dependency. Both halves are now precise:
  git is soft **at runtime** (no snapshots, so `/undo`/`/redo` do nothing) but
  **required** by the `git+https` form, and
  `pip install https://github.com/RealDealCPA-VR/IronCore/archive/refs/heads/main.zip`
  installs the same code with no git at all. Verified in clean venvs with every
  git directory stripped from `PATH`: the zip form yields `ironcore 0.2.0`, the
  `git+https` form fails with "Cannot find command 'git'".
- **Three of the eight environment variables were unreadable in the README.** Its
  table compressed the role overrides into
  `IRONCORE_ROLE_PLANNER · _CODER · _SUMMARIZER · _VERIFIER`, so
  `IRONCORE_ROLE_CODER`, `_SUMMARIZER` and `_VERIFIER` appeared under no name you
  could copy or grep. Each now has its own row. The guard derived from
  `_apply_env`'s own mapping covered `docs/CONFIG.md` only; it now covers the
  README too, matching full names, so this exact shorthand cannot come back.
- **`ironcore init --force` no longer silently eats a hand-edited config.** It
  now copies the existing file to a sibling `config.toml.bak` before writing the
  template over it, and says on stdout where the backup went. A file that is
  unchanged from the template is not backed up (nothing to lose, no litter), and
  if the backup cannot be written — unwritable directory, a directory sitting on
  the `.bak` path — the overwrite is **refused** rather than performed, because
  losing the original is the exact failure being prevented. One `.bak` slot,
  overwritten each time: it always means "the config as it was before the last
  `--force`". Found by a newcomer who lost their `model =` edit re-running `init`
  to start clean, then could not explain why `doctor` had regressed.

### Changed
- **The sdist is roughly 70% smaller** — about 1.96MB → 0.59MB — by excluding
  `docs/img` from it. The nine README screenshots were 1.5MB of the tarball and
  its single largest component, yet nothing in the distribution reads them: the
  README references them by absolute `raw.githubusercontent` URL so they render
  on PyPI and any mirror. They remain in the repo and on GitHub. The wheel never
  contained them and is unchanged in content.
- **A ninth screenshot shows `ironcore demo`** — the first command a newcomer
  runs, and previously the only headline path with no picture. Real captured
  output, generated by `tools/make_screenshots.py` like the other eight.

## [0.2.0] — 2026-07-17

The moonshots release: every bet in the README's Moonshots section has landed —
five that mold the harness *deeper* to the measured model, two that take it
beyond text, and one that opens it up — plus the two envelope upgrades shipped
since 0.1.0 (instant-on profiling and real server-side guided decoding).
**1772 tests, offline-first (no model, no network).**

### Molds deeper
- **Model-aware tokenization.** The probe battery now *measures* each model's
  chars-per-token ratio (a `TOKEN-RATIO` probe: known-char filler docs vs the
  server's reported `prompt_tokens`), and the context composer + compaction
  predicate budget with it — the universal `chars/4` guess remains only as the
  honest default for servers that don't report usage. The `/envelope` report
  card gained a `Token ratio:` line.
- **Live model swaps.** `/model <name>` re-points the *running* session
  mid-conversation: a measured model hot-swaps its profile instantly from the
  on-disk envelope cache; an unmeasured one runs on floor defaults while it is
  seeded and deep-probed in the background. The cache remembers every model
  you've measured, and `/model` marks them.
- **A model per role, each measured.** The `[roles]` config now routes the turn
  loop itself — Plan-mode turns run on the planner model, execution turns on
  the coder, compaction on the summarizer — each with *its own* capability
  envelope from the shared cache, so every routed call uses that model's
  measured wire protocol, context window, and sampling (floor defaults,
  honestly, until a role model is measured). `/envelope` shows per-role status.
- **Best-of-N escape hatches.** When the model dead-ends at a seam with a
  *mechanical* verifier — a tool call that won't parse, a patch that won't
  apply — the engine resamples up to `[engine] best_of_n` candidates at raised
  temperature and races them: the first that parses / applies in-memory
  re-enters the normal safety gate; losers are discarded, and every candidate
  is charged to the turn budget. Off by default (`best_of_n = 1`).
- **The self-improvement loop.** Every session records mechanical evidence per
  model — did tool calls parse at the active rung, did edits apply, did
  verification pass, did the turn drift — into an outcome ledger next to the
  envelope cache, and at session start a deterministic tuner conservatively
  *lowers* any ladder score the live evidence contradicts (downgrade-only:
  upgrades are never applied, they earn a "run `/probe`" hint). The report card
  and `/envelope` say `tuned` honestly; off switch: `[envelope] auto_tune`.
- **Guided decoding — the real `strict_json` rung.** A model the envelope routes
  to `strict_json` is now driven with server-side constrained decoding: the
  engine sends `response_format` (a json-schema pinning output to one
  `{"tool","args"}` call) so the model emits *guaranteed* well-formed tool calls
  instead of best-effort ones — with a `done` action so a constrained model can
  still finish a turn, the raw JSON scaffold suppressed from the transcript, and
  a clean ladder-down to the IRONCALL text floor if the server can't constrain.
  The `Provider` gained additive `response_format`/`extra_body` knobs (vLLM
  `guided_json`/llama.cpp GBNF via `extra_body`), and the capability probe now
  measures *guided* reliability so the ladder routes here only when it works.
- **Instant-on profiling.** On first use of an unprobed model, IronCore now
  seeds a *usable* capability profile in ~1 second from endpoint introspection
  (Ollama `/api/show` for the real context window + capability detection) — the
  first turn runs with the model's true window and native tool-calling instead
  of the conservative floor — then deepens the measurement with the full probe
  battery in the background and hot-swaps the refined profile in. A `source`
  field (`default`/`seeded`/`probed`) makes the report card and `doctor` honest
  about whether values are introspected guesses or measurements. Configurable
  via `[envelope] instant_seed` / `auto_probe`.

### Beyond text
- **Vision — image inputs for screenshots/diagrams.** A new `read_image` tool
  lets the model actually look at a workspace PNG/JPEG/GIF/WEBP: the bytes ride
  the conversation as OpenAI image content-parts (base64 data URIs, so Ollama
  and vLLM vision models both work). The capability is *measured, not assumed*
  — seeded from Ollama's `/api/show` capabilities (`[envelope] vision`
  overrides for endpoints without introspection), and a text-only model gets an
  honest "no vision capability" error instead of a hallucination. The composer
  budgets attached images and keeps only the newest two; the report card gained
  a `Vision: yes|no` line.
- **MCP tool servers.** `[mcp.servers.<name>]` config entries connect stdio
  MCP servers through a dependency-free JSON-RPC client (spawned directly,
  never via a shell); their tools register as `mcp__<server>__<tool>` at NET
  risk — never auto-approved, denied in Plan mode, and only present at all
  when `safety.network_tools = true`. Output is fenced UNTRUSTED like every
  tool, and `ironcore doctor` reports the configured server lineup.

### Extensibility
- **Drop-in plugins.** Providers, tools, slash commands, probes, and edit
  formats now plug in as standard Python entry points (`ironcore.providers` /
  `ironcore.tools` / `ironcore.commands` / `ironcore.probes` /
  `ironcore.edit_formats`): `pip install` a plugin distribution next to
  IronCore and its tools register behind the *same* safety gate, its provider
  builds when `provider.type` names it (role routing and `/model` swaps
  included), its probes join `/probe`'s battery, and its edit formats join
  `edit_file`. Built-ins win every name clash, a broken plugin is skipped and
  reported by `ironcore doctor` — never a crashed boot — and
  `[plugins] enabled = false` turns discovery off. Author guide:
  [`docs/PLUGINS.md`](docs/PLUGINS.md).

## [0.1.0] — 2026-07-16

The first release: a complete, interactive terminal coding agent built for
open-source models. Every phase of the build plan ([TODO.md](TODO.md)) is
shipped, validated by an independent adversarial review with execution-verified
probes, and proof-tested end-to-end against real files, subprocesses, and git.
**1223 tests, offline-first (no model, no network).**

### The Capability Envelope
- Measured model profiling — `CTX-HONESTY`, `RETENTION`, `TOOL-FORM`,
  `JSON-STRICT`, `EDIT-FORMAT`, `CODE-SMOKE` probes fill a `CapabilityProfile`
  that picks the tool protocol, edit format, context budget, and anchor cadence
  per model, all with mechanical scoring.
- Adapter ladders: native → strict-JSON → **IRONCALL** text protocol for tool
  calls; unified-diff → search/replace → whole-file for edits; each degrades to
  a format the model can actually produce, never failing the task.

### Turn engine
- The deterministic `COMPOSE → CALL → PARSE → GATE → EXECUTE → OBSERVE → VERIFY
  → DONE` loop: the harness owns all state and re-presents it, so the model
  never has to remember.
- Malformed-output repair with protocol laddering; a verification loop that
  feeds failures back once and **refuses to report unverified work as done**
  (`goal-unmet`); budget/runaway protection; micro-stepping and history
  compaction.

### Safety kernel
- Four modes (Plan / Manual / Accept-Edits / Auto) cycled with Shift+Tab; a
  Mode×Risk policy where network is never auto-allowed and Plan denies all
  mutation.
- Path jail, command policy (deny-lists in every mode + risky-pattern
  escalation), an approval broker with turn-scoped grants, secret redaction,
  a prompt-injection guard, and byte-exact git-snapshot undo.

### Tools & providers
- read / list / glob / grep, a fuzzy deterministic patcher with jailed atomic
  writes, a cross-platform shell with process-tree kill, and a gated network
  fetch.
- One streaming OpenAI-compatible client (Ollama, vLLM, llama.cpp, LM Studio,
  OpenRouter, Together, Groq) with retries and key redaction; Ollama
  introspection; role routing; endpoint capability detection.

### Interactive TUI
- A streaming Textual app: transcript with live tool cards, an approval modal
  with a colored diff viewer, Shift+Tab mode switching, a slash-command palette,
  and resumable sessions.
- Slash commands: `/goal`, `/loop`, `/model`, `/init`, `/compact`, `/undo`,
  `/redo`, `/review`, `/memory`, `/workflow`, `/help`, `/mode`, `/version`.

### Workflows & memory
- Deterministic multi-agent workflows (YAML): fanout / foreach / reduce over
  fresh-context subagents, harness-controlled flow, per-item failure isolation,
  and three built-ins (`review`, `migrate`, `explain-repo`). Subagents inherit
  the session mode — a Plan session can never mutate through a workflow.
- `IRONCORE.md` project memory injected into the system context; handoff blocks
  written on compaction and session end; JSONL session transcripts with resume.

### Distribution
- Pure-Python wheel (`pip` / `uv tool` / `pipx`), CI on Ubuntu + Windows across
  Python 3.11 and 3.13 with an 85% coverage gate on the core packages, and a
  tag-triggered PyPI release via trusted publishing.
- `ironcore demo` — a fully offline narrated walkthrough of a real session.

<!--
Release links are only added once the tag actually exists. 0.1.0 and 0.2.0 were never
tagged and are not being tagged retroactively, so they have no link on purpose — a link
that 404s is worse than no link. Their sections stay as the record of what landed when;
v0.2.1 is the first tag, and it ships everything in all three. Pushing a `v*` tag runs
.github/workflows/release.yml, which creates the GitHub Release (sdist + wheel attached)
using the matching section above as its notes.
-->

[0.2.1]: https://github.com/RealDealCPA-VR/IronCore/releases/tag/v0.2.1
