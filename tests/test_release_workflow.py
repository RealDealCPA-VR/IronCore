"""FIX-4: the release pipeline must be correct and safe to run before it is ever run.

`release.yml` had never executed once, so every gate in it was unproven. These tests pin
the properties that were actually broken:

* no tag-vs-version guard — `v0.2.1` against a pyproject reading `0.2.0` went green;
* `verify-install` asserted only the ``ironcore `` *prefix*, so it could not catch that;
* the only gate touching the wheel was ``ironcore --version``, which `cli.py` keeps
  deliberately import-light — so a dropped workflow YAML would ship to PyPI green;
* `publish` hard-failed its OIDC exchange until one-time PyPI setup was done, taking the
  whole run red and leaving nothing to install;
* `ci.yml` never built the package at all, and gated coverage on 3 of 8 packages.

GitHub Actions cannot be executed here, so these are structural assertions over the YAML
plus the executable halves of the same gates (module imports, workflow-YAML discovery,
CHANGELOG extraction) run for real against this checkout.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CI_YML = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE_YML = ROOT / ".github" / "workflows" / "release.yml"


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _jobs(path: Path) -> dict:
    return _load(path)["jobs"]


def _steps(path: Path, job: str) -> list[dict]:
    return _jobs(path)[job]["steps"]


def _run_text(path: Path, job: str) -> str:
    """Every `run:` script in a job, concatenated — what the runner will execute."""
    return "\n".join(s.get("run", "") for s in _steps(path, job))


# --- both workflows are valid YAML -----------------------------------------


@pytest.mark.parametrize("path", [CI_YML, RELEASE_YML], ids=["ci", "release"])
def test_workflow_is_parseable_yaml_with_jobs(path):
    doc = _load(path)
    assert isinstance(doc, dict), f"{path.name} did not parse to a mapping"
    assert doc["name"]
    assert doc["jobs"], f"{path.name} declares no jobs"


def test_release_fires_only_on_version_tags():
    doc = _load(RELEASE_YML)
    # PyYAML resolves the bare key `on` to the boolean True (YAML 1.1). Accept either.
    triggers = doc.get("on", doc.get(True))
    assert set(triggers) == {"push"}, "release must not fire on anything but a tag push"
    assert triggers["push"]["tags"] == ["v*"]
    assert "branches" not in triggers["push"]


# --- finding 1: the tag was a third, unchecked source of truth --------------


def test_release_guards_tag_against_pyproject_version():
    build = _run_text(RELEASE_YML, "build")
    assert "tomllib" in build and "pyproject.toml" in build, "guard must read pyproject"
    assert "$TAG" in build or "ref_name" in build, "guard must read the pushed tag"
    assert 'if [ "$V" != "$P" ]' in build, "guard must compare tag version to pyproject"
    assert "exit 1" in build


def test_tag_guard_runs_before_anything_is_built():
    """A mismatch must cost nothing. Guard first, or it is theatre."""
    names = [s.get("name", "") for s in _steps(RELEASE_YML, "build")]
    guard = next(i for i, n in enumerate(names) if "tag must match" in n.lower())
    build = next(i for i, n in enumerate(names) if n == "Build")
    assert guard < build, f"guard at {guard} must precede build at {build}: {names}"


def test_verify_install_asserts_the_exact_version_not_a_prefix():
    """The prefix check passed for ANY version, so it could never catch a bad tag."""
    text = _run_text(RELEASE_YML, "verify-install")
    assert 'EXPECTED="ironcore ${TAG#v}"' in text
    assert '[ "$OUT" != "$EXPECTED" ]' in text
    assert '"ironcore "*)' not in text, "the old prefix-only case statement is back"


# --- finding 2: the wheel gate never imported anything real ----------------


def _deep_smoke_requirements(text: str) -> None:
    assert "walk_packages" in text, "must import every module from the wheel"
    assert "discover_workflows" in text, "must prove the builtin workflow YAMLs shipped"
    assert "explain-repo" in text, "must name the builtin workflows it expects"
    assert "demo --smoke" in text, "must run a real offline session from the wheel"


def test_release_wheel_gate_is_deep_not_just_the_entry_point():
    _deep_smoke_requirements(_run_text(RELEASE_YML, "verify-install"))


def test_ci_package_job_runs_the_same_deep_wheel_gate():
    """Kept deliberately in sync: the two inline scripts must not drift apart."""
    _deep_smoke_requirements(_run_text(CI_YML, "package"))


def test_verify_install_never_checks_out_the_source():
    """With the source tree present, `import ironcore` resolves to it and the job
    proves nothing about the wheel."""
    for step in _steps(RELEASE_YML, "verify-install"):
        assert "checkout" not in step.get("uses", ""), "checkout defeats the wheel gate"


def test_verify_install_covers_all_three_supported_platforms():
    matrix = _jobs(RELEASE_YML)["verify-install"]["strategy"]["matrix"]["os"]
    assert {"ubuntu-latest", "windows-latest", "macos-latest"} == set(matrix)


# --- finding 3: a first tag push had to produce something installable ------


def test_github_release_job_attaches_the_dist_and_can_write():
    job = _jobs(RELEASE_YML)["github-release"]
    assert job["permissions"]["contents"] == "write"
    text = _run_text(RELEASE_YML, "github-release")
    assert "gh release create" in text and "dist/*" in text
    assert "--notes-file" in text, "release notes must come from the CHANGELOG"


def test_github_release_is_idempotent_so_a_rerun_is_safe():
    text = _run_text(RELEASE_YML, "github-release")
    assert "gh release view" in text, "must detect an existing release"
    assert "--clobber" in text, "re-upload must replace assets, not fail"


def test_publish_is_gated_so_an_unconfigured_first_tag_push_stays_green():
    """The whole point: PyPI Trusted Publishing is not set up yet. The publish job must
    SKIP (green), not fail an OIDC exchange that cannot succeed."""
    cond = _jobs(RELEASE_YML)["publish"]["if"]
    assert "vars.PYPI_TRUSTED_PUBLISHING" in cond, "publish must be opt-in via a variable"
    assert "refs/tags/" in cond, "publish must still be tag-only"


def test_github_release_does_not_depend_on_publish():
    """If the release waited on publish, gating publish would strand the release."""
    needs = _jobs(RELEASE_YML)["github-release"]["needs"]
    assert "publish" not in needs
    assert set(needs) == {"build", "verify-install"}


def test_publish_still_requires_the_verification_gates():
    assert set(_jobs(RELEASE_YML)["publish"]["needs"]) == {"build", "verify-install"}


def test_no_pypi_token_is_referenced_anywhere():
    """Trusted Publishing only — a stored token would be the one real secret here."""
    text = RELEASE_YML.read_text(encoding="utf-8")
    assert "id-token: write" in text
    assert "secrets.PYPI" not in text and "password:" not in text


# --- finding 4: ci.yml never built the package, and gated 3 of 8 packages --


def test_ci_has_a_package_job_so_regressions_surface_on_prs():
    jobs = _jobs(CI_YML)
    assert "package" in jobs, "ci.yml never built the package"
    text = _run_text(CI_YML, "package")
    assert "uv build" in text
    assert "twine check" in text


def test_ci_coverage_gate_covers_the_whole_package_at_90():
    text = _run_text(CI_YML, "test")
    assert "--cov=ironcore\n" in text or "--cov=ironcore " in text, "must measure all of it"
    assert "--cov=ironcore/core" not in text, "the narrow gate excluded half the moonshots"
    floor = int(re.search(r"--cov-fail-under=(\d+)", text).group(1))
    assert floor >= 90, f"coverage floor {floor} is below the measured 93%"


def test_ci_runs_on_macos_which_spec_claims_to_support():
    matrix = _jobs(CI_YML)["test"]["strategy"]["matrix"]
    platforms = set(matrix["os"]) | {e["os"] for e in matrix.get("include", [])}
    assert "macos-latest" in platforms


# --- the executable half: the gates, run for real against this checkout ----


def test_every_ironcore_module_imports_cleanly():
    """The subprocess half of the deep wheel gate. `ironcore --version` is import-light
    on purpose, so nothing else in the suite proves the whole package imports."""
    script = (
        "import importlib, pkgutil, sys, ironcore\n"
        "bad = []\n"
        "names = [m.name for m in pkgutil.walk_packages("
        "ironcore.__path__, 'ironcore.', bad.append)]\n"
        "for n in names:\n"
        "    try:\n"
        "        importlib.import_module(n)\n"
        "    except Exception as exc:\n"
        "        bad.append(f'{n}: {exc!r}')\n"
        "print('FAILED:' + repr(bad) if bad else f'OK {len(names)}')\n"
        "sys.exit(1 if bad else 0)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, f"modules failed to import:\n{proc.stdout}\n{proc.stderr}"


def test_builtin_workflow_yamls_are_present_and_discoverable():
    """The exact regression the deep wheel gate exists to catch: drop a YAML and the old
    pipeline shipped it to PyPI with every job green."""
    import ironcore
    from ironcore.workflows.schema import discover_workflows

    builtin = Path(ironcore.__file__).resolve().parent / "workflows" / "builtin"
    found = discover_workflows(builtin)
    assert {"review", "migrate", "explain-repo"} <= set(found), f"discovered only {found}"


# --- open-source furniture -------------------------------------------------


def test_security_policy_exists_with_a_private_channel():
    text = (ROOT / ".github" / "SECURITY.md").read_text(encoding="utf-8")
    assert "security/advisories/new" in text, "no private reporting channel"
    assert "do not open a public issue" in text.lower()
    assert "docs/SAFETY.md" in text, "scope must be anchored to the documented model"


def test_contributing_exists_and_is_aimed_at_humans():
    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    for command in ("uv sync --extra dev", "uv run --extra dev pytest", "ruff check ."):
        assert command in text, f"missing setup command: {command}"
    # The four load-bearing rules a stranger's PR gets rejected for.
    assert "MockProvider" in text and "offline" in text.lower()
    assert "Windows" in text
    assert "stdlib only" in text
    assert "docs/CONTRACTS.md" in text


def test_contributing_resolves_the_todo_dead_end():
    """PROTOCOLS step 2 is 'find your task in TODO.md' — which has 66 done tasks and zero
    open ones, so a human following the README dead-ended immediately."""
    text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    assert "no open tasks" in text.lower(), "must say the ledger is empty"
    assert "Open an issue first" in text, "must give humans a way to propose work"
    assert "AGENTS.md" in text, "must explain what the agent-facing variant is"


def test_todo_really_has_no_open_tasks():
    """Pins the premise of the test above — if the ledger refills, revisit that wording."""
    todo = (ROOT / "TODO.md").read_text(encoding="utf-8")
    assert "- [ ] " not in todo, "TODO.md has open tasks again; CONTRIBUTING.md now lies"


@pytest.mark.parametrize("name", ["bug_report.yml", "feature_request.yml"])
def test_issue_template_parses_and_asks_what_triage_needs(name):
    path = ROOT / ".github" / "ISSUE_TEMPLATE" / name
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert doc["name"] and doc["description"] and doc["body"]
    ids = {field.get("id") for field in doc["body"]}
    assert {"doctor", "model", "endpoint"} <= ids, f"{name} missing triage fields: {ids}"
    assert "ironcore doctor" in path.read_text(encoding="utf-8")


def test_bug_template_routes_security_reports_away_from_public_issues():
    text = (ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml").read_text(encoding="utf-8")
    assert "SECURITY.md" in text


# --- CHANGELOG: it is now the source of the release notes ------------------


def _changelog_section(version: str) -> str:
    """The exact extraction release.yml runs to build the GitHub Release notes."""
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    pattern = rf"^## \[{re.escape(version)}\].*?$(.*?)(?=^## |\Z)"
    match = re.search(pattern, text, re.DOTALL | re.MULTILINE)
    return match.group(1).strip() if match else ""


def test_changelog_has_a_section_for_the_version_about_to_be_tagged():
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    body = _changelog_section(version["project"]["version"])
    assert body, "release.yml would fall back to generic notes"
    assert "## [" not in body, "extraction leaked into the next version's section"


def test_changelog_does_not_advertise_a_demo_command_that_does_not_exist():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "python -m demo" not in text, "the shipped command is `ironcore demo`"
    assert "ironcore demo" in text


def test_changelog_has_no_release_link_that_404s():
    """0.1.0 was never tagged, so its release link pointed at nothing."""
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "[0.1.0]: https://github.com" not in text
