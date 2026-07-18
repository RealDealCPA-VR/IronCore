# Troubleshooting

> Start here: **`ironcore doctor`**. It checks Python, your config files, the endpoint, the
> configured model, the envelope cache, git, MCP and plugins, prints a remedy under every
> failing line, and exits non-zero when something would really break — so
> `ironcore doctor && ironcore` is a usable setup gate. Every section below is keyed to the
> exact line doctor prints.

Config keys referenced here are documented in [CONFIG.md](CONFIG.md). "your config file"
means `~/.ironcore/config.toml` unless a project `<workspace>/.ironcore/config.toml` also
exists — doctor prints the paths it used.

---

## `[--] endpoint not reachable: http://localhost:11434/v1/models`

Nothing is listening. IronCore does not run a model — it talks to a server you run.

- **Ollama:** `ollama serve` (it also starts on demand when you run `ollama run <model>`).
- **llama.cpp:** `llama-server -m <model.gguf> --port 8080`, then set
  `base_url = "http://localhost:8080/v1"`.
- **vLLM:** `vllm serve <model>`, default `http://localhost:8000/v1`.
- **LM Studio:** start the local server from its Developer tab and copy the URL it shows.

Then re-run `ironcore doctor`. Want to see IronCore work *right now* with no server at all?
`ironcore demo` runs a real, fully offline session against the mock provider.

## `[FAIL] model qwen3-coder:30b is not available at ... (from provider.model)`

The endpoint answered, but does not have that model. Doctor prints the models you *do* have
directly under this line. IronCore's shipped default is a large model most people have not
pulled, so this is the most common first failure.

- Pull it: `ollama pull qwen3-coder:30b` (~18 GB), **or**
- point at something you already have: set `[provider] model` in your config file, or
  `IRONCORE_MODEL=<name>` for one session.

Model ids must match what the *server* calls them — copy one from doctor's list verbatim.
A smaller model is not a failure case: IronCore measures whatever you point it at and
degrades the protocol, not the outcome ([MODELS.md](MODELS.md)).

## `[FAIL] provider.base_url is not a usable URL` / `got HTTP 404 ... is this an OpenAI-compatible endpoint?` / `answered, but not with an OpenAI model list`

The URL is wrong, or points at something that is not an OpenAI-compatible API.

- It needs a scheme: `http://localhost:11434/v1`, not `localhost:11434`.
- It almost always ends in **`/v1`** — doctor says so explicitly when yours does not.
  Ollama's native port answers on `/` too, but that is a *different* API; IronCore speaks
  the OpenAI-compatible one.
- Doctor probes exactly `{base_url}/models`. Try that URL in a browser or with `curl`: a
  JSON list of models means the URL is right.

## `[FAIL] endpoint rejected our API key: HTTP 401`

The server wants authentication and did not get a usable key. Doctor sends the same
`Authorization: Bearer <key>` header the app does, so this is a real answer, not a probe
artifact.

- **Hosted endpoints** (OpenRouter, Together, Groq) always need a real key.
- **Local servers started with `--api-key`** need the matching value.

Set `IRONCORE_API_KEY` in your shell (preferred — it keeps the key out of every file) or
`[provider] api_key` in your config. The default `"ironcore-local"` is a placeholder that
local servers ignore; it is not a credential.

## `[!!] git not found -- /undo, /redo and change-set snapshots are disabled`

IronCore still runs. What you lose is the safety net: every turn that writes normally makes
a shadow git snapshot (a private repo under `.ironcore/snapshots/`, never touching your own
index or branches) so `/undo` can restore byte-exact. Without git, edits are not reversible
from inside IronCore.

Install git and re-run doctor. Until then, prefer `manual` mode and review each diff.

## `[!!] the cached profile for '<model>' was corrupt (an interrupted write?)`

A capability cache under `~/.ironcore/envelopes/` was unreadable — most often because the
first-run probe was interrupted. IronCore moves it aside to `<slug>.json.corrupt` (renamed,
never deleted), reads the model as unprobed, and measures again, so this heals itself.
Nothing is lost but the measurement. Delete the `.corrupt` file whenever you like.

If probing itself is unwelcome (a metered endpoint, a slow machine), turn it off:
`[envelope] auto_probe = false`. IronCore then stays on floor-conservative defaults, which
work — just slower and more cautiously — until you run `/probe`.

## `[--] mcp: 1 server(s) configured ... stay unregistered until safety.network_tools = true`

Working as designed. MCP tools are NET-risk, and NET tools are not merely gated when
`network_tools` is off — they are never registered. Set `[safety] network_tools = true` in
your **user** config to enable them (a project config cannot turn it on — see CONFIG.md §6),
and read [SAFETY.md](SAFETY.md) §10 first: configured servers are spawned at launch.

`[FAIL] mcp <name>: command 'x' not found on PATH` means the launcher is missing — install
it (`npm i -g <package>` for the `npx`-based servers). Write the **bare** command name
(`npx`, not `npx.cmd`): it is resolved with `shutil.which`, which honors PATHEXT on Windows,
so the bare name is both correct and portable.

## `[FAIL] config: ...` — the app will not start

A TOML file is malformed or holds an invalid value. The message names the file and, for
syntax errors, the line. Common causes: a smart quote pasted from a web page, a missing
closing `"`, `mode = manual` without quotes, or a mode spelled `"Manual"` (values are
lowercase: `plan`, `manual`, `accept-edits`, `auto`).

You can always start from a known-good file: `ironcore init --force` rewrites the commented
starter config.

## `[safety] project config requested mode 'auto'; clamped to your ceiling 'manual'`

Not an error — the autonomy ceiling. The repository you are in ships a
`.ironcore/config.toml` asking for more autonomy than your own config grants, and a cloned
file may never raise autonomy (CONFIG.md §6, SAFETY.md T8). To grant it: press Shift+Tab for
this session, or set the key in your **own** `~/.ironcore/config.toml`.

## Nothing is wrong, but the model behaves badly

- `/envelope` shows what IronCore measured — honest context, chosen wire protocol, chosen
  edit format, vision, and whether the numbers are `default`, `seeded`, `probed` or `tuned`.
- `/probe` re-measures. A model that just got a new quantization or template deserves it.
- Small models drift: keep `/goal` set, expect micro-stepping, and read
  [MODELS.md](MODELS.md) §4 for what to expect at each weight class.
- A model that fails at native tool calls is not a dead end — the envelope walks it down to
  `strict_json` and then to the IRONCALL text floor, which always works.

## Still stuck

Open an issue with the **full output of `ironcore doctor`** plus your model and endpoint
(the issue template asks for exactly this). Doctor redacts your API key from everything it
prints, so its output is safe to paste. Security-relevant reports go through the private
channel in [`.github/SECURITY.md`](../.github/SECURITY.md) instead.
