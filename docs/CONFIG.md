# Configuration reference

> Every key IronCore reads, with its type, default, and what it actually does. Generated
> against `ironcore/config/settings.py` and pinned by `tests/test_docs_reference.py`, which
> fails if a model field stops being documented here. Companion to [SPEC.md](SPEC.md) §12.

## 1. Where config comes from

Four layers, later wins:

```
built-in defaults  ←  ~/.ironcore/config.toml  ←  <workspace>/.ironcore/config.toml  ←  IRONCORE_* env
      (this file)          "the user layer"            "the project layer"              (your shell)
```

- **You need no config file at all.** Every key below has a default; `ironcore` boots without
  one. `ironcore init` writes a fully commented starter file (`ironcore init --project` for
  the project layer; `--force` overwrites, first copying the existing file to a sibling
  `config.toml.bak` unless it is unchanged from the template — and refusing the overwrite
  outright if that backup cannot be written), and `ironcore doctor` prints which files it
  actually loaded.
- The **project layer is committable** — it arrives with a `git clone`, so it is the only
  untrusted layer. It may *lower* autonomy freely and may never raise it (§6).
- **Env wins over both** and is never clamped: it comes from your own shell, not from a
  cloned repo.
- Merging is per-key and deep: setting `[provider] model` in a project file leaves
  `base_url` from your user file alone.

Any unreadable or malformed file is a `ConfigError` naming the file (and line, for TOML
syntax errors) — never a traceback. A leading UTF-8 BOM is stripped before parsing.

## 2. `[provider]` — which model, at which endpoint

| Key | Type | Default | Meaning |
|---|---|---|---|
| `base_url` | str | `"http://localhost:11434/v1"` | Any OpenAI-compatible endpoint: Ollama, vLLM, llama.cpp's server, LM Studio, OpenRouter/Together/Groq. The path almost always ends in `/v1`. |
| `api_key` | str | `"ironcore-local"` | Sent as `Authorization: Bearer <key>` on **every** request. Local servers ignore it — which is why the placeholder default works. **Hosted endpoints require a real one**, and so do vLLM/llama.cpp started with `--api-key`; without it you get HTTP 401 and `doctor` says `endpoint rejected our API key`. Prefer `IRONCORE_API_KEY` in your shell over writing a real key into a file. |
| `model` | str | `"qwen3-coder:30b"` | The model id as the *server* names it. It must already exist there — `ironcore doctor` checks the endpoint's model list and tells you what you actually have. |
| `type` | str | `"auto"` | Which client to build. `"auto"` picks the Ollama client for an Ollama-looking endpoint (unlocking keep-alive and `/api/show` introspection) and the generic OpenAI-compatible client otherwise; `"ollama"` / `"openai"` force one. A plugin provider's entry-point name also goes here ([PLUGINS.md](PLUGINS.md)); an unknown value falls back to `auto` with a `doctor` warning rather than breaking boot. |

## 3. `[roles]` — a model per job

Unset (the default) means every role uses `provider.model`. All four roles live at the same
`base_url`. See [MODELS.md](MODELS.md) §5 for which splits are worth it.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `planner` | str \| unset | unset | Model for PLAN-mode turns. |
| `coder` | str \| unset | unset | Model for every other turn of the loop. |
| `summarizer` | str \| unset | unset | Model for compaction (`/compact`, auto-compaction). |
| `verifier` | str \| unset | unset | Model for `/review`. A *different-family* verifier decorrelates errors. |

A routed role runs on **its own** measured envelope from the shared cache. An unmeasured role
model honestly runs on floor defaults — measure it by switching to it once with
`/model <role-model>` and back. `/envelope` prints per-role status.

## 4. `[safety]` — how much the agent may do unasked

| Key | Type | Default | Meaning |
|---|---|---|---|
| `mode` | str | `"manual"` | Boot mode: `plan` · `manual` · `accept-edits` · `auto`. Shift+Tab cycles it live. Full policy table in [SAFETY.md](SAFETY.md) and `ironcore/safety/policy.py`. |
| `workspace_only` | bool | `true` | **Prompt text only.** The write jail in `tools/fs_write.py` runs unconditionally, so turning this off cannot let a write escape the workspace — it only drops the sentence from the system prompt. That is why it is not clamped (§6). |
| `network_tools` | bool | `false` | NET-risk tools (`fetch_url`, and every MCP tool) are not merely gated when this is off — they are **never registered**, so the model never sees them. NET is never auto-allowed in any mode even when on. |

## 5. `[envelope]` — how IronCore molds itself to your model

The measurement machinery ([MODELS.md](MODELS.md)). All three switches are on by default;
turning them off is safe and only costs adaptivity.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `auto_probe` | bool | `true` | Measure an unprobed model in the background on first launch (~80 short calls). Off = stay on floor-conservative defaults until you run `/probe` yourself. |
| `instant_seed` | bool | `true` | Before the deep probe, seed a *usable* provisional profile in ~1s from endpoint introspection (Ollama `/api/show` for the real window, capability detection for native tool-calling). Off = the first turns run on the floor while the probe works. |
| `auto_tune` | bool | `true` | The self-improvement loop: record live mechanical outcomes per model and, at session start, conservatively **lower** any ladder score the evidence contradicts (downgrade-only — an upgrade needs a real `/probe`). Off = nothing recorded, nothing tuned. |
| `vision` | bool \| unset | unset | Force image attachment on or off. Unset (the default) trusts the profile's seeded/measured `vision` flag. Set `true` for an endpoint without introspection serving a VL model (e.g. vLLM); setting `false` disables vision on a model that really has it. |

## 6. Autonomy: what a cloned project config may not do

The project layer arrives with `git clone`, so `Settings.load` clamps it (SAFETY.md T8/T9/T10,
CONTRACTS.md §7). It may always *lower* autonomy. It may never:

| Attempted from the project layer | What happens |
|---|---|
| raise `safety.mode` above your user layer's rank (`plan` < `manual` < `accept-edits` < `auto`) | clamped to your ceiling |
| `safety.network_tools = true` when you did not enable it | forced back to `false` |
| `plugins.enabled = true` when you disabled it | forced back to `false` |
| declare a new `[mcp.servers.<name>]` | dropped entirely |
| redefine an MCP server you declared | ignored, except `enabled = false` (a lowering) |

The ceiling is your **effective** user layer: your TOML if it sets the key, the built-in
default if it does not — so an absent `~/.ironcore/config.toml` is a floor, not an exemption.
Every clamp prints a note (in `doctor`, and as a TUI boot note) naming what was asked for and
where to grant it. `IRONCORE_MODE` and Shift+Tab are never clamped — they are you at the
keyboard. A ceiling value IronCore cannot rank or coerce (`mode = "Manual"`,
`network_tools = "yes please"`, a `[safety]` that is not a table) fails the load loudly
naming your file, rather than silently leaving the untrusted layer's value standing.

## 7. `[engine]` — turn-loop knobs

| Key | Type | Default | Meaning |
|---|---|---|---|
| `best_of_n` | int, 1–5 | `1` | Total candidate budget per turn at the *mechanically verified* seams (a tool call the repair ladder gives up on; an edit that will not apply). `1` = disabled, no extra provider calls. `N` races up to N−1 resampled candidates; each still passes the safety gate and is charged to the turn budget. |

## 8. `[mcp]` — Model Context Protocol tool servers

The section holds exactly one key, `servers`: a table of tables, written as
`[mcp.servers.<name>]` — one per server, where `<name>` becomes the tool-name prefix.
Their tools register as `mcp__<server>__<tool>` at
`ToolRisk.NET`, so **nothing appears unless `safety.network_tools = true`**. Read SAFETY.md
§10 first: every configured server is spawned **at launch** to enumerate its tools, so
configuring one is the consent moment.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `command` | str \| unset | unset | The executable to spawn (stdio transport). Resolved through `shutil.which` and run **without a shell** — on Windows a bare `npx` correctly resolves to `npx.CMD` via PATHEXT, so write the portable bare name. Either `command` or `url` is required. |
| `args` | list[str] | `[]` | Arguments for `command`. |
| `env` | table | `{}` | Extra environment for the child. Values support `${VAR}` placeholders expanded from *your* environment at load time, so secrets stay out of a committable file. A bare `$VAR` is left literal; an unset or empty `${VAR}` skips that server with a note instead of handing four literal characters to the child. |
| `url` | str \| unset | unset | Accepted so http-transport configs parse, but such entries are **skipped with a note** — v0.x connects stdio servers only. |
| `timeout_s` | float > 0 | `30.0` | Per-request timeout for that server. |
| `enabled` | bool | `true` | `false` skips the server entirely (and is the one project-layer override that is honored). |

## 9. `[plugins]` — entry-point extensions

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | bool | `true` | Discover `ironcore.*` entry points at boot ([PLUGINS.md](PLUGINS.md)). Default on because `pip install` was already the consent moment. `false` is the hardened-setup switch: entry points are never consulted at all. |

## 10. `[skills]` — the SKILL.md open standard

A *skill* is a `<dir>/SKILL.md` file — YAML frontmatter (`name` + `description`) over a
Markdown body of instructions, the same shape Claude Code / Codex / grok-build read (authoring
guide: [SKILLS.md](SKILLS.md)). Discovery reads your `~/.ironcore/skills/` (trusted) and the
workspace's `.ironcore/skills/` (clone-borne, so first use is confirmed). A compact catalog
rides the system prompt; the full body is lazy-loaded via `use_skill` or `/skill`. Skills are
**inert Markdown** carrying no autonomy — any script they reference runs through the model's own
`run_command` under the EXEC gate — so this section is **not** under the autonomy ceiling (§6).

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | bool | `true` | Discover skills at all. `false` = no catalog, no `use_skill` tool, and `/skill` reports it is off — the hardened-setup switch. |
| `compat_dirs` | bool | `false` | Also read `.claude` / `.codex` / `.grok` `/skills` dirs (at both your home and the workspace), so a skill authored for one of those tools works here unchanged. Off by default; opt in for zero-setup ecosystem compatibility. |

## 11. `[tools]` — tool configuration

The one key here configures the NET-risk `web_search` tool. Like `fetch_url`, `web_search`
is **never registered unless `safety.network_tools = true`** (§4), and every call ASKS even
in AUTO — NET is never auto-allowed — with the resolved endpoint shown in the approval
preview. So `search_url` is **not** under the autonomy ceiling (§6): a cloned project
pointing it elsewhere escalates nothing, and you see the URL on every approval.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `search_url` | str | `"https://html.duckduckgo.com/html/"` | The HTML search endpoint `web_search` queries. The query is sent as `?q=…`; the response is parsed for `title`/`url`/`snippet` results, capped and secret-redacted. Point it at your own [SearXNG](https://searxng.org/) instance if you prefer. An **empty string** leaves `web_search` unregistered while `fetch_url` stays. |

## 12. Environment variables

All eight are read by `config/settings.py`; each overrides the same key in every file layer,
and env is never clamped. An empty value is ignored (it does not blank the key).

| Variable | Sets |
|---|---|
| `IRONCORE_BASE_URL` | `provider.base_url` |
| `IRONCORE_MODEL` | `provider.model` |
| `IRONCORE_API_KEY` | `provider.api_key` |
| `IRONCORE_MODE` | `safety.mode` |
| `IRONCORE_ROLE_PLANNER` | `roles.planner` |
| `IRONCORE_ROLE_CODER` | `roles.coder` |
| `IRONCORE_ROLE_SUMMARIZER` | `roles.summarizer` |
| `IRONCORE_ROLE_VERIFIER` | `roles.verifier` |

Two variables IronCore does not define but does obey, because they are the cross-tool
convention for terminal colour. They change nothing about behaviour or output *text* —
only whether escape codes are written:

| Variable | Effect |
|---|---|
| `NO_COLOR` | Set to anything: `doctor`, `demo` and `init` print with no colour, even on a terminal. |
| `FORCE_COLOR` | Set to anything: colour is written even when stdout is a pipe or a file. On Windows this also forces ANSI escape codes rather than the legacy console colour API, which a redirect cannot carry. |

Without either, colour follows the stream: on a terminal you get it, piped or redirected you
do not, so `ironcore doctor > report.txt` is always plain text.

## 13. A complete config.toml

Every key, every default, annotated. `ironcore init` writes a shorter commented version of
this to `~/.ironcore/config.toml`.

```toml
[provider]
base_url = "http://localhost:11434/v1"   # any OpenAI-compatible server; usually ends in /v1
model    = "qwen3-coder:30b"             # must already exist on that server
api_key  = "ironcore-local"              # local servers ignore it; hosted ones REQUIRE a real
                                         # key -- prefer IRONCORE_API_KEY over a file
type     = "auto"                        # "auto" | "ollama" | "openai" | a plugin's name

[roles]                                  # optional; unset = provider.model does every job
# planner    = "llama3.3:70b"
# coder      = "qwen2.5-coder:7b"
# summarizer = "qwen3:8b"
# verifier   = "gemma3:27b"

[safety]
mode           = "manual"                # plan | manual | accept-edits | auto
workspace_only = true                    # prompt text; the write jail runs regardless
network_tools  = false                   # true also registers fetch_url and all MCP tools

[envelope]
auto_probe   = true                      # measure an unprobed model in the background
instant_seed = true                      # ~1s provisional profile from introspection first
auto_tune    = true                      # downgrade-only tuning from live evidence
# vision     = true                      # UNSET by default = trust the measured flag

[engine]
best_of_n = 1                            # 1 = off; up to 5 candidates raced per turn

[plugins]
enabled = true                           # false = never consult entry points

[skills]
enabled     = true                       # false = no catalog, no use_skill, /skill off
compat_dirs = false                      # true also reads .claude/.codex/.grok /skills dirs

[tools]
search_url = "https://html.duckduckgo.com/html/"   # web_search endpoint; needs network_tools;
                                         # "" = no web_search tool

# [mcp.servers.filesystem]               # NET-risk: needs safety.network_tools = true,
# command   = "npx"                      # and is SPAWNED AT LAUNCH (SAFETY.md §10)
# args      = ["-y", "@modelcontextprotocol/server-filesystem", "."]
# env       = { API_TOKEN = "${MY_TOKEN}" }   # ${VAR} comes from your shell
# timeout_s = 30.0
# enabled   = true
```

## 14. Checking what actually loaded

```
ironcore doctor
```

prints the config files it found, the **effective** model and mode, every clamp note, whether
the endpoint answers and has your model, and whether the envelope cache is writable. It exits
non-zero when something would really break, so `ironcore doctor && ironcore` is a usable
setup gate. When it says something you do not understand, see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md).
