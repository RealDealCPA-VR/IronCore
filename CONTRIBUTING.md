# Contributing to IronCore

Thanks for being here. This file is for **humans**. If you are an AI agent working in this
repo, read [`AGENTS.md`](AGENTS.md) instead — it is the same discipline expressed as an
orchestration protocol.

## Get set up

You need [uv](https://docs.astral.sh/uv/) and Python 3.11+. You do **not** need a model, a
GPU, or a network connection to develop IronCore.

```bash
git clone https://github.com/RealDealCPA-VR/IronCore
cd IronCore
uv sync --extra dev
```

Then confirm the baseline is green *before* you change anything:

```bash
uv run --extra dev pytest        # the whole suite, offline, ~40s
uv run --extra dev ruff check .  # must be clean
```

If that is red on a fresh clone, that is a bug — please report it. Do not build on red.

See it actually run, with no model installed:

```bash
uv run ironcore demo     # a narrated real session against MockProvider
uv run ironcore doctor   # what IronCore thinks of your machine and config
```

## The four rules that get PRs rejected

These are not style preferences. They are load-bearing, and CI enforces all four.

1. **Every test runs offline.** No network, no real model, no API key. Use
   `MockProvider` (`ironcore/providers/mock.py`). A test that needs a model is a test
   nobody can run.
2. **Windows is first-class.** No POSIX-only paths, no `/tmp`, no assuming `/` — use
   `pathlib`. Mind CRLF in fixtures. CI runs Ubuntu, Windows and macOS; "works on my
   Linux" is not done.
3. **The safety kernel is not negotiable.** Every tool is gated through
   `decide(mode, risk)`. NET risk is *never* auto-allowed in any mode. `ironcore/safety/`
   imports **stdlib only** — no third-party, no other IronCore package. If your change
   makes safety import something, the change is wrong.
4. **Respect the dependency direction** ([`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)
   §4): nothing imports `tui/`; providers never import `tools/`, `core/`, or `commands/`.

Also: line length 100, and the interfaces in [`docs/CONTRACTS.md`](docs/CONTRACTS.md) are
**frozen**. If you must change one, the CONTRACTS.md edit ships in the *same commit* with a
migration note — additive if at all possible.

## Proposing work

[`TODO.md`](TODO.md) is the build ledger for the original v0.1/v0.2 push. **Every task in
it is done — there are no open tasks to claim.** It is history now, useful for seeing how
a piece was built and why. Do not wait for it to refill.

So, to contribute:

1. **Open an issue first** for anything beyond an obvious fix — a bug report, or a feature
   proposal saying what a user can do afterwards that they cannot do today. This is
   cheaper than a rejected PR, and it is where scope gets agreed.
2. **Small and obvious?** Typo, broken link, clear bug with a clear fix — just open the PR.
3. **Read the spec before proposing behavior changes.** [`docs/SPEC.md`](docs/SPEC.md) is
   binding, and [`docs/SAFETY.md`](docs/SAFETY.md) §7 lists things IronCore will
   deliberately *not* build. A PR that crosses §7 will be declined no matter how good the
   code is.
4. **Found a security hole? Do not open an issue.** See
   [`SECURITY.md`](.github/SECURITY.md).

## Sending a PR

- **A fix without a regression test is not a fix.** Write the test so that it *fails*
  before your change and passes after. Say so in the PR description.
- Keep the diff to one logical change. Do not reformat neighbours, upgrade dependencies,
  or fix unrelated things "while you're in there".
- Comments explain *constraints*, not narration — why this is the way it is, and what
  breaks if you change it.
- Run the full baseline before you push:

  ```bash
  uv run --extra dev ruff check .
  uv run --extra dev pytest
  ```

- In the PR, state what you actually ran and what you observed. If something is unverified,
  say "not verified" and why. Claiming verification that did not happen is the one
  unforgivable violation here.

## How this repo thinks

IronCore is built by the protocol it implements: **the repo is the memory.** If you
learned it, decided it, or broke it, it goes in a file before you stop — not in a PR
comment that no one will find in six months.

That is why the ritual documents exist, and why they are worth reading even though they
are addressed to agents:

| File | What it is |
|---|---|
| [`AGENTS.md`](AGENTS.md) | The same contribution rules, written for an AI agent |
| [`docs/PROTOCOLS.md`](docs/PROTOCOLS.md) | Handoff/pickup protocol — how state moves between contributors |
| [`HANDOFF.md`](HANDOFF.md) | The running log of what changed, verified, and why |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Package layout and the dependency rules |
| [`docs/CONTRACTS.md`](docs/CONTRACTS.md) | Frozen interfaces |

You are not required to write a `HANDOFF.md` block for a normal PR — a good PR description
does the same job. Maintainers and agents doing multi-session work do write them.

## License

By contributing, you agree your contributions are licensed under the
[MIT License](LICENSE).
