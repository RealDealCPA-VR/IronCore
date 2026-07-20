# Changelog

All notable changes to IronCore are documented here. This project adheres to
[Semantic Versioning](https://semver.org/).

## [0.3.2] — 2026-07-20

### Added
- **Update notifier — a gentle "a newer version is available" nudge.** A new
  `ironcore/update.py` checks PyPI for a newer release of the `ironcore-cli`
  distribution and, when one exists, prints a **one-line** upgrade hint. It
  **never auto-installs** — a CLI that runs shell commands and edits files leaves
  the user in control of what version they run — so it only ever prints the
  command (`pip install -U ironcore-cli` / `uv tool upgrade ironcore-cli` /
  `pipx upgrade ironcore-cli`). The check is **fail-silent** (any network/DNS/
  timeout/bad-JSON error reads as "no update", never a traceback), **cached** for
  a day at `~/.ironcore/update-check.json` (an atomic write, so a normal launch
  inside the window does not dial), **short-timeout**, and **opt-out** via a new
  `[update] check` setting (default `true`). It surfaces in exactly two places,
  and **neither runs in a non-interactive / headless / CI context**: `ironcore
  doctor` prints `[--] update: <v> available …` / `[ok] up to date (<v>)` (only
  when stdout is a real terminal; offline is never a `doctor` failure), and the
  TUI posts a single boot-style note in the background at startup. Headless
  `ironcore exec` and any non-TTY path never dial PyPI. Version comparison uses
  `packaging.version` (PEP 440, pre-release aware). Documented in `docs/CONFIG.md`
  §12, the `ironcore init` starter config, and the README's new "Updating"
  section. 31 new offline tests (`tests/test_update.py` + TUI/doctor additions),
  all inject the fetch so nothing dials.

### Fixed
- **Update notifier: declared its `packaging` dependency so a stock install's
  `ironcore doctor` no longer crashes.** `ironcore/update.py`'s `is_newer` imports
  `packaging.version`, but `packaging` was not in the runtime `dependencies` — it
  was only reachable in the dev env because `pytest` pulls it in, so the whole
  green suite hid the gap. In a stock install (`pip`/`pipx`/`uv tool install
  ironcore-cli` into a clean env), `packaging` was absent, so `ironcore doctor`
  raised `ModuleNotFoundError: No module named 'packaging'` and exited 1 — a
  doctor failure caused solely by the notifier — and the TUI boot nudge was
  silently dead. `packaging>=23.0` is now a declared runtime dependency.
  Defense in depth: `is_newer`'s import moved inside its `try` (a future prune
  degrades to "no nudge", not a crash) and `doctor`'s `_report_update` is wrapped
  fail-silent, so the maintenance ping can never be the reason a doctor run ends
  in a traceback. Three offline regression tests pin the declaration and both
  guards.

## [0.3.1] — 2026-07-20

First release published to PyPI: `pip install ironcore-cli`. No code change from
0.3.0 — a plumbing retry. 0.3.0's publish job ran before the PyPI Trusted Publisher
was fully configured, so it built and shipped a GitHub Release but never reached
PyPI. A published PyPI version cannot be re-uploaded once its release run has
failed, so the retry ships under a new patch number; 0.3.0's GitHub Release keeps
its attached wheels. (The distribution is `ironcore-cli` because PyPI refused the
bare name `ironcore` as too similar to the unrelated `iron-core`; the command and
the import are still `ironcore`.)

## [0.3.0] — 2026-07-19

The frontier-parity release. A three-lens review measured IronCore against OpenAI
Codex CLI and xAI grok-build: the engine, safety kernel and envelope stack came out
frontier-grade (a dozen attempted jail/gate/injection bypasses all failed), but the
2026 *platform* layer was missing. This release closes it — **skills (the `SKILL.md`
open standard), headless `ironcore exec`, `AGENTS.md`/`CLAUDE.md` compatibility, and a
`web_search` tool** — and fixes the concrete bugs the review surfaced (`/loop` never
ran, gitignoring `.ironcore/` silently killed undo, `/goal verify:` didn't arm the
engine, the task wasn't auto-pinned as durable state) plus a security finding (the
`verify:` command now goes through the policy gate). 1903 offline tests.

### Added
- **Headless exec — `ironcore exec "<prompt>"` (PKG-5).** IronCore is now
  scriptable: `exec` runs one turn against the real engine and renders its event
  stream (`ironcore/headless.py`) with no TUI. In the default human mode the
  model's streamed text goes to **stdout** and every other event (tool calls,
  approvals, verify/repair status, the completion line) to **stderr**, so
  `ironcore exec "…" > answer.txt` captures only the answer; `--json` emits one
  serialized event per line to stdout for a machine consumer (the `core/events`
  dataclasses are an additive contract). Default `--mode plan` is read-only and
  CI-safe; `--mode` raises it. Approvals fail closed and invent **no new
  decision path**: the engine's own `ApprovalBroker` is built with `timeout=0`,
  so any `ask` gate (there is no human to prompt) resolves through the broker's
  existing timeout-DENY, with a one-line hint on stderr. Exit codes: **0** on
  `TurnCompleted`, **1** on `TurnError`, **2** on a `ConfigError` during setup.
  Stays import-light like `doctor`/`demo`/`init` (the engine is lazy-imported in
  the handler).
- **`web_search` tool (PKG-5).** A second NET tool beside `fetch_url`
  (`ironcore/tools/search.py`): it queries a configurable HTML search endpoint
  (`[tools] search_url` — a SearXNG instance or the DuckDuckGo HTML endpoint, the
  default) and returns the top results as text (title · url · snippet). Results
  are parsed with the stdlib HTML parser (linear, no regex backtracking on
  adversarial markup), capped, and **secret-redacted** before they reach the
  model or the transcript. It inherits the NET policy untouched — registered
  **only** when `safety.network_tools` is true (and a non-empty `search_url` is
  set), and every call ASKS even in AUTO (NET is never auto-allowed).
- **Skills — the `SKILL.md` open standard (PKG-4).** IronCore now discovers, surfaces and
  invokes skills: a `<dir>/SKILL.md` file (YAML `name`/`description` frontmatter over a
  Markdown instruction body), the same on-disk shape Claude Code, Codex and grok-build read
  — so a skill authored for any of them works here unchanged. Discovery
  (`ironcore/skills.py`, modeled on `plugins.py`) scans `~/.ironcore/skills/` (trusted) and
  the workspace's `.ironcore/skills/` (clone-borne, gated); `[skills] compat_dirs = true`
  additionally reads `.claude`/`.codex`/`.grok` `/skills` dirs for zero-setup ecosystem
  compatibility. A malformed `SKILL.md` is skipped with a reason, never a crash.
  - **Surfacing:** a compact catalog (name + one-liner each) rides the SYSTEM share beside
    project memory, charged via `estimate_tokens` against the *measured* `honest_context` —
    the envelope-native twist: on a tiny-context model it degrades to top-N (or nothing)
    rather than silently eating the window. The composer budget invariant is provably
    unchanged (a new `skills_catalog=` param, default `()`).
  - **Invocation (both lazy-body per the standard):** `/skill` lists skills and `/skill
    <name>` injects one's body into the next turn; the model reads a skill via the new
    READ-risk `use_skill(name=...)` tool, riding the existing tool loop / transcript / audit.
  - **Safety:** user skills are trusted like `IRONCORE.md`; a **project skill is confirmed
    once per workspace before first use** (T8, the pattern `/workflow` uses) and never
    reaches the model-facing catalog until approved. A skill body is display text — any
    script it references runs through the EXEC-gated command tool, and no `verify:` directive
    is ever parsed out of a skill (that path stays sourced from the project `IRONCORE.md`
    alone). Off switch: `[skills] enabled = false`. A copy-ready template ships at
    `examples/skills/hello-skill/`; the authoring guide is `docs/SKILLS.md`.
- **Instruction-file compat + user-global memory (PKG-3).** When a workspace has
  no `IRONCORE.md`, project memory now falls back to an existing `AGENTS.md`,
  then `CLAUDE.md` (first found wins) — so a repo cloned with a frontier
  instruction file gets first-run value instead of being silently ignored
  (ironically the IronCore repo itself ships an `AGENTS.md` the product used to
  overlook). A **user-global** `~/.ironcore/IRONCORE.md` is composed alongside
  the project file — user-global first, then project — within the *same*
  SYSTEM-share budget, each honestly truncated so a tiny-context model degrades
  gracefully rather than dropping a source silently. A lone source stays
  byte-identical to before (verbatim, no labels); only when both are present are
  they joined under `##` provenance labels. **Security:** the fallback widens
  *display* memory only. The `verify:` directive is still sourced from the
  project `IRONCORE.md` **alone** (`core/verify.py` reads that one file
  directly) — never from `AGENTS.md`/`CLAUDE.md`/user-global — because a verify
  command executes unattended after the first edit, so a cloned repo must not be
  able to arm one. Zero-config; the engine call site is unchanged.
- **Auto-pinned objective (engine M1).** On a session's first turn the goal is
  seeded from the opening prompt (a normalized one-line copy) when `/goal` did
  not set one first — so `state.goal` is durable and the standing-context anchor
  always carries a real objective instead of "Goal: (none set)". The goal is
  also re-presented as a compact one-line system message on the off-cadence
  turns where the full anchor is not injected, so a compaction can never leave
  the model without its objective ("re-present, don't rely on recall"). No model
  call is added to the hot path; the goal line and the full anchor are mutually
  exclusive and share the anchor budget, so the context budget invariant is
  unchanged.
- **`/goal verify:` now arms the engine's in-turn stop-condition.** Attached
  checks are mirrored onto the durable `state.goal_verify`, and the engine's
  verifier prioritizes them above IRONCORE.md / auto-detect — so a check attached
  via `/goal` genuinely holds the turn open ("won't call itself done until it
  passes") even in a workspace with no `pytest`/`npm`/`cargo` markers. Matches
  what SPEC §5.5 already promised.
- **`/help <command>` prints that command's usage.** The per-command syntax
  strings (`/goal verify:`, `/workflow run`, `/loop 5m`, `/model <name>`) were
  registered but unreachable from inside the product — `/help` only ever showed
  the one-line summaries. `/help <name>` now prints the named command's usage +
  summary (with a nearest-match hint on a miss); a bare `/help` still lists the
  whole index and key reference.

### Fixed
- **`/loop` actually runs now.** The command parsed intervals and registered a
  loop, but the app implemented neither `register_loop` nor `stop_loop`, so
  every registration fell through to "stored; runs when the session drives it"
  and *nothing ever executed*. The TUI now drives a real loop: a registered
  loop re-submits its prompt as a genuine turn on its interval (self-paced loops
  re-submit when the prior tick completes), a tick never fires while a turn is
  running, and `/loop stop` cancels the driver. Ticks ride the ordinary turn
  path, so they are gated, rendered, and session-recorded like any other turn.
- **Gitignoring `.ironcore/` no longer silently kills undo/redo.** The natural
  way to quiet the `?? .ironcore/` line in `git status` is to add `.ironcore/`
  to `.gitignore` — which, on git ≥2.50, made every `snapshot()` exit 1 ("paths
  ignored by .gitignore … Use -f") because the shadow-index add named an ignored
  path explicitly, so `/undo` and `/redo` quietly degraded to "[snapshot
  skipped]". The snapshot store now detects an already-ignored `.ironcore` and
  drops the redundant exclude pathspec (letting `.gitignore` do the excluding),
  so undo/redo keep working byte-exactly. It does **not** use `--force`, which
  would have started capturing the user's other ignored files.
- **`read_image` failure reasons are visible to the model.** Only the no-vision
  refusal was mirrored into `ToolResult.output`; unsupported-format, missing-
  file, too-big and missing-`path` reasons lived only in `.error` (UI-facing),
  so the model received an *empty* failed result and blind-retried the same
  doomed call. Every failure branch now carries its actionable reason in
  `output` as well, so the model can self-correct.

### Security
- **Verify commands are gated through the deny-list before they run.** A
  `verify:` line in a cloned repo's IRONCORE.md is repo-borne, unsandboxed
  execution that fires automatically after the first edit in accept-edits/auto.
  Every verify command now passes `classify_command` first: a deny-listed
  command (`rm -rf /`, `curl | sh`, …) is refused and never executed, in every
  mode; a risky-pattern command (`git push`, `sudo`, …) is skipped with a note
  rather than run unattended in the autonomous modes. Either way the turn fails
  closed — an unverifiable turn is never reported as done (SAFETY T7).

## [0.2.3] — 2026-07-19

### Changed
- **First release published to PyPI: `pip install ironcore-cli`.** No code
  changed from 0.2.2 — this is a plumbing retry. 0.2.2's publish job ran the
  moment its tag landed, which was before the PyPI Trusted Publisher had been
  registered, so the OIDC exchange was rejected and PyPI never received the
  artifact. A PyPI version cannot be re-uploaded once its release run has
  failed in CI, so the retry ships under a new patch number. Everything else
  about 0.2.2 stands, and its GitHub Release keeps its attached wheels.

## [0.2.2] — 2026-07-18

### Changed
- **The PyPI distribution is named `ironcore-cli`.** PyPI refused the bare name
  `ironcore` as "too similar to an existing project" — its similarity guard
  collapses separators, so the unrelated `iron-core` (Iron.io API wrappers)
  occupies the same slot. Only the distribution name moved: the console script
  is still `ironcore`, the import package is still `ironcore`, the repository is
  still `IronCore`, and no code changed. `pip install ironcore-cli` then
  `ironcore doctor`. (This version never reached PyPI — its publish job ran
  before the Trusted Publisher was registered and failed the OIDC exchange. See
  0.2.3.)

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

[0.3.2]: https://github.com/RealDealCPA-VR/IronCore/releases/tag/v0.3.2
[0.3.1]: https://github.com/RealDealCPA-VR/IronCore/releases/tag/v0.3.1
[0.3.0]: https://github.com/RealDealCPA-VR/IronCore/releases/tag/v0.3.0
[0.2.1]: https://github.com/RealDealCPA-VR/IronCore/releases/tag/v0.2.1
