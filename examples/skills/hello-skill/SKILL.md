---
name: hello-skill
description: A template skill — copy this folder to .ironcore/skills/ and edit it to teach IronCore a reusable procedure.
---

# Hello, skill

This is an example [SKILL.md](../../../docs/SKILLS.md) — a reusable set of instructions
IronCore can pull into a turn on demand. Copy this folder to
`~/.ironcore/skills/<your-skill>/` (trusted, every workspace) or
`<workspace>/.ironcore/skills/<your-skill>/` (first-use confirmed) and replace the body with
your own steps.

## What a good skill body looks like

Write it as if briefing a capable teammate who has not seen this repo before:

1. State the goal in one sentence.
2. Give the concrete steps, in order, with the exact commands or file paths.
3. Say what "done" looks like, and what to do when a step fails.

For example, a real skill might say:

> To add a new provider adapter: create `ironcore/providers/<name>.py` subclassing the base
> Provider, register it in the provider registry, add a `tests/providers/test_<name>.py` that
> exercises `complete` and `stream` against `MockProvider`, then run
> `uv run --extra dev pytest tests/providers -q` and confirm it is green.

## Notes

- Only this short `description` rides every prompt; the body is loaded lazily when you run
  `/skill hello-skill` or the model calls `use_skill(name='hello-skill')`.
- If your skill references a script, the model runs it through the normal command tool — it is
  still gated by the mode/deny-list/jail. A skill cannot execute anything on its own.
