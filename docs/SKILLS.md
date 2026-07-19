# Skills — the SKILL.md open standard in IronCore

A **skill** is a folder with a `SKILL.md` file: a bit of YAML naming the skill, over a
Markdown body of standing instructions. It is the same on-disk shape Claude Code, Codex,
grok-build and ~20 other tools already read — so a skill you (or a repo) already wrote for
one of those works in IronCore unchanged, and vice-versa.

Skills answer "how do we do X here?" once, so you (and the model) do not re-explain a release
checklist, a migration procedure, or a house code-review rubric every session. IronCore adds
one twist to the standard: skills are **envelope-aware and harness-gated** — the catalog is
budget-fitted to your model's real context window, and clone-borne skills are confirmed before
first use.

## Where skills live

| Location | Trust | When |
|---|---|---|
| `~/.ironcore/skills/<name>/SKILL.md` | **trusted** (you authored it) | your machine-wide skills, every workspace |
| `<workspace>/.ironcore/skills/<name>/SKILL.md` | **first-use gated** (arrives with `git clone`) | skills a repo ships |

Turn on `[skills] compat_dirs = true` ([CONFIG.md](CONFIG.md) §10) and IronCore *also* reads
`.claude/skills`, `.codex/skills` and `.grok/skills` at both levels — so ecosystem skills need
no copying. User locations are scanned first, so a cloned project skill can never shadow one of
your own by name.

## Writing a SKILL.md

```markdown
---
name: release-checklist
description: Steps to cut a release — bump version, run the suite, tag, and push.
---

# Release checklist

1. Bump the version in `pyproject.toml` and `CHANGELOG.md`.
2. Run the full test suite and confirm it is green.
3. Commit as `chore(release): vX.Y.Z`.
4. Tag `vX.Y.Z` and push the tag.

If a step fails, stop and report — do not push a red build.
```

- **Frontmatter** (`---`-delimited YAML at the very top) carries `name` and `description`.
  `name` is what you type after `/skill` and what the model passes to `use_skill`; if you omit
  it, the folder name is used. `description` is the one-liner shown in the catalog — keep it
  under ~300 characters (it is capped) and write it so the model knows *when* to reach for the
  skill.
- **The body** (everything after the closing `---`) is the instructions. It is loaded lazily —
  only its short description rides every prompt; the full body is pulled in on demand.
- A malformed `SKILL.md` (no frontmatter, broken YAML, an unterminated fence) is **skipped with
  a reason** shown by `/skill`, never a crash — the same fail-safe discipline as plugins.

A ready-to-copy template lives at
[`examples/skills/hello-skill/SKILL.md`](../examples/skills/hello-skill/SKILL.md).

## Using a skill

Two paths, both loading the body only when it is actually needed:

- **You:** `/skill` lists what is discoverable; `/skill <name>` injects that skill's
  instructions into the next turn. For a project (clone-borne) skill the first `/skill <name>`
  shows a summary and asks you to confirm with `/skill run <name>` — after that it injects
  directly, and the model's `use_skill` path is unblocked too.
- **The model:** the skills catalog (name + one-liner each) is surfaced in the system prompt,
  and the model calls the READ-risk `use_skill(name=...)` tool to read a skill's full
  instructions before doing a task it covers.

On a small-context model the catalog **degrades honestly** — it shows the top few skills, or
none, rather than silently eating the window. Only trusted skills (yours, plus project skills
you have confirmed) ever appear in that model-facing catalog.

## Safety

Read [SAFETY.md](SAFETY.md) §11 for the full story. In short:

- A skill body is **display text**. Any script it references is run by the model through the
  ordinary command tool, so the **EXEC gate, deny-list and workspace jail apply unchanged** —
  a skill cannot smuggle execution past the kernel, and `use_skill` is READ-risk.
- Project skills are **confirmed before first use** (T8), and an unconfirmed one never reaches
  the model's context.
- IronCore never parses a `verify:` directive (or any control channel) out of a skill body —
  that path stays sourced from the project `IRONCORE.md` alone.

## Turning it off

`[skills] enabled = false` disables discovery entirely: no catalog, no `use_skill` tool, and
`/skill` reports that skills are off.
