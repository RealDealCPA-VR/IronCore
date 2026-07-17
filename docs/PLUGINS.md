# Writing IronCore plugins

IronCore extends through standard Python entry points (MS-5). A plugin is an ordinary
pip-installable distribution: install it into the same environment as IronCore and its
contributions are discovered at boot — no IronCore changes, no registration calls.
The frozen surfaces live in [CONTRACTS.md §11](CONTRACTS.md); the trust model in
[SAFETY.md §8](SAFETY.md).

## The five groups

Declare any of these in your plugin's `pyproject.toml`:

```toml
[project]
name = "ironcore-myplugin"
version = "0.1.0"
dependencies = ["ironcore"]

[project.entry-points."ironcore.tools"]
word_count = "ironcore_myplugin.tools:build_tools"

[project.entry-points."ironcore.commands"]
mycmds = "ironcore_myplugin.commands:COMMANDS"

[project.entry-points."ironcore.probes"]
latency = "ironcore_myplugin.probes:build_probe"

[project.entry-points."ironcore.providers"]
myprov = "ironcore_myplugin.provider:MyProvider"

[project.entry-points."ironcore.edit_formats"]
rot13 = "ironcore_myplugin.formats:apply_rot13"
```

### Tools — `ironcore.tools`

The entry point is a **factory** called as `factory(settings, workspace)`; it returns a
`Tool` or a list/tuple of them:

```python
from ironcore.safety.risk import ToolRisk
from ironcore.tools.base import Tool, ToolResult

class WordCountTool(Tool):
    name = "word_count"                      # unique; builtins win a clash
    description = "Count words in a string. Example: word_count(text='a b c')."
    risk = ToolRisk.READ                     # one honest, worst-case class
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string", "description": "Text to count."}},
        "required": ["text"],
    }

    async def run(self, **kwargs) -> ToolResult:
        text = kwargs.get("text") or ""
        return ToolResult(ok=True, output=str(len(text.split())))

def build_tools(settings, workspace):
    return [WordCountTool()]
```

Rules (validated at load; a violation skips the tool with a reason):

- `risk` must be a real `ToolRisk` member — the engine gates every call through
  `decide(mode, risk)` exactly like a builtin. Declare the worst case honestly.
- A `ToolRisk.NET` tool is loaded only when `[safety] network_tools = true` — an off NET
  tool is never even registered (the `fetch_url` rule).
- Never print, never prompt, never self-gate (CONTRACTS §3). `ToolResult.output` is what
  the model sees; put anything the model must understand there.

### Slash commands — `ironcore.commands`

The entry point resolves directly to a `SlashCommand` or a tuple of them (the same
`COMMANDS` tuple convention the built-in command modules use):

```python
from ironcore.commands.base import SlashCommand

def _cmd_hello(ctx, args: str) -> str:
    return f"hello {args}".strip()

COMMANDS = (SlashCommand("hello", "say hello", "/hello [name]", _cmd_hello),)
```

Handlers are synchronous and must not block — schedule long work via
`ctx.extra["schedule"]` like the built-ins do. Builtins win a name clash.

### Probes — `ironcore.probes`

The entry point is a **zero-arg factory** returning one probe or a sequence. A probe
duck-types `envelope.runner.Probe`: string `id` (the built-in battery's ids are
reserved) and `title`, a `targets` list/tuple of dotted profile paths, and an async
`run(provider)` returning a `ProbeResult`:

```python
from ironcore.envelope.runner import ProbeResult

class LatencyProbe:
    id = "MYPLUGIN-LATENCY"
    title = "round-trip latency class"
    targets = ("sampling.latency_class",)

    async def run(self, provider):
        return ProbeResult(probe_id=self.id, scores={"sampling.latency_class": 1.0})

def build_probe():
    return LatencyProbe()
```

Plugin probes join `/probe` and the first-use auto-probe **after** the default battery.
They only *fill* profile fields via the runner's dotted-path merge — protocol and
edit-format selection stays `CapabilityProfile.recommended_*` (frozen, CONTRACTS §5).

### Providers — `ironcore.providers`

The entry point is a factory (a `Provider` subclass works) called exactly like the
built-in clients: `factory(base_url=..., api_key=..., model=...[, transport=...])`.
It is selected when `provider.type` in config equals the entry-point name:

```toml
[provider]
type = "myprov"          # your entry-point name; auto/ollama/openai are reserved
base_url = "http://localhost:9999/v1"
model = "my-model"
```

The factory runs through the registry's single build path, so per-role routing and
`/model` live swaps construct plugin providers too. Implement the frozen `Provider`
interface (CONTRACTS §2): raise `ProviderError` only, terminate streams with
`done`/`error`.

### Edit formats — `ironcore.edit_formats`

The entry point is a pure applier `apply(original_text, edit) -> PatchResult`,
registered under the entry-point name (lowercase slug, ≤ 32 chars; the built-in ladder
rungs are reserved):

```python
import codecs
from ironcore.tools.patch import PatchResult

def apply_rot13(original: str, edit: str) -> PatchResult:
    return PatchResult(ok=True, new_text=codecs.encode(edit, "rot13"))
```

The format appears in `edit_file`'s `format` enum. Appliers are wrapped defensively: an
exception or a non-`PatchResult` return becomes a mechanical failure and the file stays
byte-unchanged. Honest limits in v0.x: plugin formats are **never auto-recommended**
(the envelope ladders are closed), **never pre-verified by best-of-N resampling**, and
**never tuned** by the self-improvement loop — the model uses one only when explicitly
steered to.

## Fail-safety and inspection

- A broken plugin (import error, crashing factory, invalid contribution) is **skipped
  and recorded** — boot never crashes. Skips surface as boot notes in the TUI and in
  `ironcore doctor`, which prints what loaded and every skip with its reason.
- Discovery order is deterministic (entry points sorted by name).
- Builtins win every duplicate name — a plugin can never shadow `read_file`,
  `edit_file`, `read_image`, `/help`, or a builtin edit format.
- `[plugins] enabled = false` in config disables discovery entirely.

## Trust model (read this)

Installing a plugin distribution **is** the consent moment: `pip install` already
executed arbitrary code, and discovery only sees what you installed into IronCore's
environment. The loader isolates faults, but it cannot verify intent — a tool's declared
`ToolRisk` is trusted (SAFETY.md §8). Install plugins you trust, and audit what
`ironcore doctor` reports.
