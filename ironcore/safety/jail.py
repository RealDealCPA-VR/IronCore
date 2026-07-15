"""Path jail: the workspace-escape control (docs/SAFETY.md §2, threat T2).

Tools map every model-supplied path through ``resolve_jailed()`` before touching
the filesystem — in EVERY mode. The jail is enforced at the tool layer and is
mode-independent; the policy gate (policy.py) layers on top and can only
tighten, never loosen, what the jail decides.

Rules this module enforces:

- Stdlib only (safety package rule, docs/ARCHITECTURE.md §4). Pure functions,
  no state, no config.
- Containment is judged on RESOLVED real paths: workspace and candidate both go
  through ``Path.resolve()``, so a symlinked workspace works and a symlink
  inside the workspace that points outside is rejected.
- Relative candidates are anchored at the workspace root, never at the process
  CWD.
- Fail closed: path forms whose meaning depends on process state — Windows
  drive-relative (``C:file``) and rootless (``\\name``) paths resolve against
  per-drive CWDs — are violations, never guesses. Unresolvable input (embedded
  NUL, OS errors) is a violation too.
- Windows-semantics defense: any component the Win32 layer would silently
  rewrite (trailing dots/spaces — ``".. "`` opens as ``".."``) is rejected
  outright, because a lexical containment check would pass while the actual
  file API escapes.
- The workspace root itself is inside (it is the jail's floor, not a wall).
"""

from __future__ import annotations

import os
from pathlib import Path


class JailViolation(ValueError):
    """A candidate path escapes — or cannot be proven inside — the workspace."""


def resolve_jailed(
    workspace: str | os.PathLike[str], candidate: str | os.PathLike[str]
) -> Path:
    """Resolve ``candidate`` against ``workspace`` and prove containment.

    Returns the fully resolved absolute path iff its real path stays inside the
    resolved workspace; the workspace root itself is allowed. Raises
    :class:`JailViolation` for everything else: ``..`` traversal out, absolute
    paths outside, UNC/extended-length/drive-relative Windows forms, symlinks
    pointing out, and input that cannot be resolved at all.
    """
    try:
        ws = Path(workspace).resolve()
    except (OSError, ValueError) as exc:  # nothing can be proven inside an unresolvable root
        raise JailViolation(f"workspace is not resolvable: {workspace!r}") from exc

    cand = Path(candidate)
    if "\x00" in str(cand):  # deterministic across OS/Python versions; resolve() may vary
        raise JailViolation("path contains a NUL byte")

    # Drive-relative ("C:file") and rootless ("\\name") Windows forms resolve
    # against per-drive CWDs — CWD-dependent, hence nondeterministic: deny.
    if (cand.drive or cand.root) and not cand.is_absolute():
        raise JailViolation(f"drive-relative or rootless path: {str(candidate)!r}")

    if os.name == "nt":
        _reject_win32_rewrites(cand)

    target = cand if cand.is_absolute() else ws / cand
    try:
        resolved = target.resolve()
    except (OSError, ValueError) as exc:
        raise JailViolation(f"path is not resolvable: {str(candidate)!r}") from exc

    # is_relative_to compares by PurePath rules: component-wise (no naive string
    # prefixing) and case-insensitive on Windows. Equal-to-root counts as inside.
    if not resolved.is_relative_to(ws):
        raise JailViolation(f"path escapes the workspace: {str(candidate)!r} -> {resolved}")
    return resolved


def is_inside(workspace: str | os.PathLike[str], candidate: str | os.PathLike[str]) -> bool:
    """True iff ``candidate`` resolves inside ``workspace``. Never raises.

    The predicate form of :func:`resolve_jailed` for callers that want a check,
    not the path. Same rules; violations and garbage input are simply False.
    """
    try:
        resolve_jailed(workspace, candidate)
    except (ValueError, TypeError, OSError):  # JailViolation is a ValueError
        return False
    return True


def _reject_win32_rewrites(cand: Path) -> None:
    """Reject components Win32 would silently rewrite before the filesystem sees them.

    The Win32 layer strips trailing dots and spaces from path components, so a
    path that a lexical check keeps inside (e.g. ``".. \\x"``) can open as its
    stripped form (``"..\\x"``) and escape. No legitimate Windows file can even
    be created with such a name, so rejecting them all costs nothing.
    """
    parts = cand.parts[1:] if cand.anchor else cand.parts
    for part in parts:
        if part in (".", ".."):
            continue  # plain navigation — the resolver collapses these honestly
        if part != part.rstrip(". "):
            raise JailViolation(f"Win32-rewritten component {part!r} in {str(cand)!r}")
