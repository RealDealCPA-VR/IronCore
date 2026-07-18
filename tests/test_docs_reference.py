"""The reference docs are pinned to the code, not to whoever last edited them.

Every doc in ``docs/`` had drifted from the implementation by v0.2.0: SPEC listed a
WRITE tool (``apply_patch``) that is not a tool at all and omitted ``read_image`` plus
the whole ``mcp__<server>__<tool>`` family; the config surface was documented nowhere
in full, so ``provider.api_key`` — required by every hosted endpoint the README
advertises — was undiscoverable; ARCHITECTURE's state table omitted a file the app
writes to the user's disk; PLUGINS' sample project could not be built.

Prose cannot be trusted to stay true, so these tests DERIVE the expected content from
the code (pydantic models, the tool registry, the probe battery, the entry-point group
names) and fail when the docs stop matching. A new config key with no CONFIG.md row is
a red suite, which is the only mechanism that has ever kept a reference honest.

Offline and platform-neutral: nothing here starts a server, spawns a model, or touches
anything outside the repo and pytest's tmp dirs.
"""

from __future__ import annotations

import inspect
import re
import shutil
import sys
import tomllib
from pathlib import Path

import pytest
from pydantic import BaseModel

from ironcore.config import settings as settings_module
from ironcore.config.settings import Settings
from ironcore.envelope.probes import PROBES
from ironcore.plugins import _GROUPS
from ironcore.tools.default import build_default_registry

REPO = Path(__file__).resolve().parent.parent
DOCS = REPO / "docs"


def _read(*parts: str) -> str:
    return REPO.joinpath(*parts).read_text(encoding="utf-8")


CONFIG_MD = _read("docs", "CONFIG.md")
SPEC_MD = _read("docs", "SPEC.md")
MODELS_MD = _read("docs", "MODELS.md")
ARCH_MD = _read("docs", "ARCHITECTURE.md")
PLUGINS_MD = _read("docs", "PLUGINS.md")
DOCS_INDEX_MD = _read("docs", "README.md")
README_MD = _read("README.md")
AGENTS_MD = _read("AGENTS.md")
PROTOCOLS_MD = _read("docs", "PROTOCOLS.md")


def _sections() -> dict[str, type[BaseModel]]:
    """The TOML sections and their models, straight off ``Settings``."""
    out: dict[str, type[BaseModel]] = {}
    for name, field in Settings.model_fields.items():
        model = field.annotation
        if isinstance(model, type) and issubclass(model, BaseModel):
            out[name] = model
    return out


def _toml_blocks(text: str) -> list[str]:
    return re.findall(r"```toml\n(.*?)```", text, re.DOTALL)


# ---------------------------------------------------------------------------
# CONFIG.md — the reference that did not exist
# ---------------------------------------------------------------------------


def test_every_config_section_has_a_section_in_config_md() -> None:
    """A whole `[envelope]` section was missing from every doc file."""
    missing = [name for name in _sections() if f"[{name}]" not in CONFIG_MD]
    assert not missing, f"docs/CONFIG.md documents no [section] for: {missing}"


def test_every_config_key_is_documented_in_config_md() -> None:
    """Walk the pydantic models so a new key cannot ship undocumented.

    This is the drift guard: `auto_probe`, `instant_seed` and `provider.api_key`
    appeared in NO doc file at v0.2.0.
    """
    undocumented: list[str] = []
    for section, model in _sections().items():
        for key in model.model_fields:
            if f"`{key}`" not in CONFIG_MD:
                undocumented.append(f"{section}.{key}")
    assert not undocumented, f"docs/CONFIG.md is missing keys: {undocumented}"


def test_mcp_server_keys_are_documented_too() -> None:
    """`[mcp.servers.<name>]` keys live one model deeper than the section walk."""
    for key in settings_module.MCPServerSettings.model_fields:
        assert f"`{key}`" in CONFIG_MD, f"docs/CONFIG.md is missing mcp server key {key!r}"


def test_api_key_is_documented_as_required_by_hosted_endpoints() -> None:
    """The original finding: a user pointing at OpenRouter/Together/Groq got a 401
    with no thread to pull, because api_key was documented nowhere."""
    assert "api_key" in CONFIG_MD
    assert "ironcore-local" in CONFIG_MD, "the placeholder default must be stated"
    hosted = re.search(r"\| `api_key` \|.*", CONFIG_MD)
    assert hosted is not None
    assert "REQUIRE" in hosted.group(0) or "require" in hosted.group(0), (
        "CONFIG.md must say hosted endpoints require a real key"
    )


def test_every_env_var_read_by_settings_is_documented() -> None:
    """Derived from `_apply_env`'s own mapping — eight vars, four of which
    (`IRONCORE_ROLE_*`) were documented in no file at all."""
    source = inspect.getsource(settings_module._apply_env)
    env_vars = sorted(set(re.findall(r"IRONCORE_[A-Z_]+", source)))
    assert len(env_vars) == 8, f"expected 8 env vars, found {env_vars}"
    missing = [var for var in env_vars if var not in CONFIG_MD]
    assert not missing, f"docs/CONFIG.md is missing env vars: {missing}"


def test_config_md_states_the_precedence_chain() -> None:
    for layer in ("~/.ironcore/config.toml", ".ironcore/config.toml", "IRONCORE_"):
        assert layer in CONFIG_MD


def test_config_md_documents_the_real_scalar_defaults() -> None:
    """A documented default that disagrees with the model is worse than none."""
    wrong: list[str] = []
    for section, model in _sections().items():
        for key, field in model.model_fields.items():
            default = field.default
            if isinstance(default, bool):
                rendered = "true" if default else "false"
            elif isinstance(default, str):
                rendered = f'"{default}"'
            elif isinstance(default, int):
                rendered = str(default)
            else:
                continue  # None / PydanticUndefined / factories: nothing to render
            row = re.search(rf"\| `{re.escape(key)}` \|[^|]*\|([^|]*)\|", CONFIG_MD)
            if row is None or rendered not in row.group(1):
                wrong.append(f"{section}.{key} (should show {rendered})")
    assert not wrong, f"docs/CONFIG.md default column disagrees with the models: {wrong}"


def test_the_annotated_config_example_parses_and_is_exactly_the_defaults() -> None:
    """The copy-paste config a stranger is handed must be real TOML that IronCore
    accepts — and, since every line shows a default, must load to `Settings()`."""
    blocks = _toml_blocks(CONFIG_MD)
    assert blocks, "CONFIG.md must carry a complete annotated config.toml"
    full = max(blocks, key=len)
    parsed = tomllib.loads(full)
    assert set(parsed) == set(_sections()) - {"mcp"}, (
        "the annotated example should show every section except the commented-out [mcp]"
    )
    assert Settings.model_validate(parsed) == Settings(), (
        "the annotated example claims to show defaults; it does not"
    )


# ---------------------------------------------------------------------------
# SPEC.md §6.1 — the tool table vs. the real registry
# ---------------------------------------------------------------------------


def _registered_tool_names(*, network: bool) -> set[str]:
    settings = Settings()
    settings.safety.network_tools = network
    registry = build_default_registry(settings, REPO)
    return {tool.name for tool in registry.all()}


def test_spec_tool_table_lists_every_registered_tool() -> None:
    """`read_image` shipped in v0.2 and appears in no version of the table."""
    missing = [name for name in _registered_tool_names(network=True) if f"`{name}`" not in SPEC_MD]
    assert not missing, f"docs/SPEC.md §6.1 omits registered tools: {missing}"


def test_apply_patch_is_not_advertised_as_a_registered_tool() -> None:
    """It never existed as a tool: `tools/patch.py` is the internal applier that
    `edit_file` calls. The SPEC table listed it as a registered WRITE tool."""
    assert "apply_patch" not in _registered_tool_names(network=True)
    disclaimers = ("internal", "never a registered tool", "not** a registered tool")
    # The blockquote front matter is a changelog ABOUT this correction, not a claim
    # about the tool lineup; scan the spec body only.
    body = SPEC_MD.split("\n---\n", 1)[1]
    for line in body.splitlines():
        if "apply_patch" not in line:
            continue
        assert any(mark in line.lower() for mark in disclaimers), (
            f"SPEC still presents apply_patch as a registered tool: {line!r}"
        )


def test_spec_tool_table_invents_no_tools() -> None:
    """Every tool-shaped name in the §6.1 table must resolve to a real tool."""
    table = SPEC_MD.split("### 6.1")[1].split("### 6.2")[0]
    real = _registered_tool_names(network=True)
    claimed = {
        name
        for name in re.findall(r"`([a-z_][a-z0-9_]*)`", table)
        if name not in {"safety", "network_tools", "true", "tools", "patch"}
    }
    invented = {name for name in claimed if name not in real}
    assert not invented, f"docs/SPEC.md §6.1 lists tools that are not registered: {invented}"


def test_the_mcp_tool_family_is_documented_with_its_risk_rule() -> None:
    """A whole family of tools a user can add was in no reference doc."""
    for doc, name in ((SPEC_MD, "SPEC.md"), (CONFIG_MD, "CONFIG.md")):
        assert "mcp__" in doc, f"docs/{name} never mentions the mcp__ tool family"
        assert "network_tools" in doc


def test_docs_recommend_portable_mcp_commands() -> None:
    """`command = "npx.cmd"` breaks copy-paste on Linux/macOS for no benefit."""
    for doc, name in ((SPEC_MD, "SPEC.md"), (CONFIG_MD, "CONFIG.md")):
        assert "npx.cmd" not in doc, f"docs/{name} still shows the non-portable npx.cmd"


@pytest.mark.skipif(sys.platform != "win32", reason="PATHEXT resolution is Windows-only")
def test_a_bare_command_name_resolves_to_a_cmd_shim_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proves the claim the docs now make: `shutil.which` honors PATHEXT, so a bare
    `npx` finds `npx.CMD` and the docs need not print a Windows-only spelling.
    `tools/mcp.py` resolves through exactly this call before spawning."""
    shim = tmp_path / "faketool.cmd"
    shim.write_text("@echo hi", encoding="utf-8")
    monkeypatch.setenv("PATH", str(tmp_path))
    resolved = shutil.which("faketool")
    assert resolved is not None and Path(resolved).suffix.lower() == ".cmd"


# ---------------------------------------------------------------------------
# MODELS.md — the probe battery, the seed, the outcome ledger
# ---------------------------------------------------------------------------


def test_models_and_spec_list_every_real_probe() -> None:
    """`TOKEN-RATIO` shipped in v0.2 and SPEC §4.1 never learned about it."""
    ids = [probe.id for probe in PROBES]
    for doc, name in ((MODELS_MD, "MODELS.md"), (SPEC_MD, "SPEC.md")):
        missing = [probe_id for probe_id in ids if probe_id not in doc]
        assert not missing, f"docs/{name} omits probes: {missing}"


def test_the_docs_invent_no_probes() -> None:
    """Vision is SEEDED from `/api/show`, not probed — a `VISION` row in the probe
    table would be a plausible-looking lie."""
    real = {probe.id for probe in PROBES}
    table = MODELS_MD.split("## 2. The probe suite")[1].split("### 2.1")[0]
    claimed = set(re.findall(r"`([A-Z][A-Z-]{3,})`", table))
    assert claimed <= real, f"docs/MODELS.md §2 lists non-existent probes: {claimed - real}"


def test_vision_is_documented_as_seeded_not_measured() -> None:
    assert "no VISION probe" in MODELS_MD or "not probed" in MODELS_MD
    assert "capabilities" in MODELS_MD, "the /api/show capabilities array is the source"


def test_the_source_field_values_are_documented() -> None:
    for value in ("default", "seeded", "probed", "tuned"):
        assert f"`{value}`" in MODELS_MD, f"docs/MODELS.md never explains source={value!r}"


def test_the_outcome_ledger_is_documented_with_its_path_and_off_switch() -> None:
    """Zero mentions of the self-improvement loop existed in MODELS.md at v0.2.0."""
    assert "outcomes.json" in MODELS_MD
    assert "auto_tune" in MODELS_MD
    assert "downgrade" in MODELS_MD.lower()


def test_models_md_has_no_unshipped_future_tense() -> None:
    """"runners land in IC-602..604" described shipped code as pending work."""
    assert "runners land in" not in MODELS_MD


# ---------------------------------------------------------------------------
# ARCHITECTURE.md — the module map and the state the app writes
# ---------------------------------------------------------------------------


def test_every_package_is_in_the_module_map() -> None:
    packages = {
        path.name
        for path in (REPO / "ironcore").iterdir()
        if path.is_dir() and (path / "__init__.py").exists()
    }
    missing = [pkg for pkg in packages if f"`{pkg}/`" not in ARCH_MD]
    assert not missing, f"docs/ARCHITECTURE.md §2 omits packages: {missing}"


def test_the_moonshot_modules_are_in_the_module_map() -> None:
    """Post-moonshot modules with rules of their own were absent from the map."""
    for module in ("core/roles.py", "envelope/outcomes.py", "tools/mcp.py"):
        assert f"`{module}`" in ARCH_MD, f"docs/ARCHITECTURE.md never mentions {module}"


def test_every_persistent_file_is_in_the_state_ownership_table() -> None:
    """The outcome ledger is a file the app writes to the user's disk unasked and
    the state table did not list it. Anything persistent must be declared."""
    table = ARCH_MD.split("## 5. State ownership")[1]
    for artifact in (
        "state.json",
        "sessions",
        "envelopes",
        "outcomes.json",
        "audit",
        "snapshots",
        "config.toml",
        "IRONCORE.md",
    ):
        assert artifact in table, f"docs/ARCHITECTURE.md §5 omits {artifact}"


# ---------------------------------------------------------------------------
# PLUGINS.md — the sample project an author copies
# ---------------------------------------------------------------------------


def test_the_sample_plugin_pyproject_is_buildable() -> None:
    """As written it had no `[build-system]`, so `pip install .` on the sample
    could not produce a distribution at all."""
    sample = tomllib.loads(_toml_blocks(PLUGINS_MD)[0])
    assert "build-system" in sample, "the sample pyproject cannot be built"
    assert sample["build-system"]["requires"], "build-system needs a backend requirement"
    assert sample["build-system"]["build-backend"]
    assert sample["project"]["name"] and sample["project"]["version"]


def test_the_sample_plugin_declares_the_five_frozen_entry_point_groups() -> None:
    sample = tomllib.loads(_toml_blocks(PLUGINS_MD)[0])
    declared = set(sample["project"]["entry-points"])
    assert declared == set(_GROUPS), f"sample groups {declared} != frozen {set(_GROUPS)}"


# ---------------------------------------------------------------------------
# Cross-document integrity
# ---------------------------------------------------------------------------


def test_the_docs_index_lists_every_doc() -> None:
    docs = {path.name for path in DOCS.glob("*.md")} - {"README.md"}
    missing = [name for name in sorted(docs) if f"({name})" not in DOCS_INDEX_MD]
    assert not missing, f"docs/README.md does not index: {missing}"


def test_no_doc_links_to_a_file_that_does_not_exist() -> None:
    """Cheap guard against the reference rotting into 404s."""
    broken: list[str] = []
    targets = [(DOCS / name, DOCS) for name in (p.name for p in DOCS.glob("*.md"))]
    targets += [(REPO / "AGENTS.md", REPO), (REPO / "README.md", REPO)]
    for path, base in targets:
        for link in re.findall(r"\]\(([^)#][^)]*)\)", path.read_text(encoding="utf-8")):
            if link.startswith(("http://", "https://", "mailto:")):
                continue
            if not (base / link.split("#")[0]).exists():
                broken.append(f"{path.name} -> {link}")
    assert not broken, f"broken relative links: {broken}"


def test_contributor_protocol_does_not_mandate_a_readme_section_that_is_gone() -> None:
    """AGENTS.md step 4 and PROTOCOLS.md §7 both ordered contributors to update
    "the README roadmap table and the Quickstart status note". Neither survives in
    README.md, so the instruction was unfollowable."""
    for doc, name in ((AGENTS_MD, "AGENTS.md"), (PROTOCOLS_MD, "docs/PROTOCOLS.md")):
        if "roadmap" in doc.lower():
            assert "roadmap" in README_MD.lower(), (
                f"{name} tells contributors to update a README roadmap that does not exist"
            )


def test_the_protocol_points_contributors_at_the_config_reference() -> None:
    """The reference only stays true if changing a key obliges you to update it."""
    for doc, name in ((AGENTS_MD, "AGENTS.md"), (PROTOCOLS_MD, "docs/PROTOCOLS.md")):
        assert "CONFIG.md" in doc, f"{name} never mentions docs/CONFIG.md"


def test_the_milestone_table_marks_the_shipped_version_shipped() -> None:
    """v0.2 was described as "phase 9-10: workflows, sessions/resume, project
    memory" — all of which shipped in v0.1, while the moonshots that ARE v0.2
    appeared nowhere."""
    version = tomllib.loads(_read("pyproject.toml"))["project"]["version"]
    series = "v" + ".".join(version.split(".")[:2])
    table = SPEC_MD.split("## 15. Milestones")[1]
    row = next((line for line in table.splitlines() if line.startswith(f"| {series} ")), None)
    assert row is not None, f"docs/SPEC.md §15 has no row for the shipped {series}"
    assert "shipped" in row.lower(), f"{series} row does not say it shipped: {row!r}"
    assert "moonshot" in row.lower(), f"{series} row does not describe what it shipped: {row!r}"
