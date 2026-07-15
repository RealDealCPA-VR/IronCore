"""Secret redaction for the three outbound choke points (docs/SAFETY.md §6, threat T4).

Secrets leave the machine through exactly three surfaces: context sent to a
provider, transcript rendering, and audit lines. Each surface calls its named
helper — ``redact_context``, ``redact_transcript``, ``redact_audit`` — so call
sites are explicit about which boundary they are scrubbing. Rules this module
enforces:

- Two detection layers, both always on: literal secret VALUES (process env +
  parsed ``.env`` values, injected as mappings — this module never reads
  files; a caller parses ``.env``) and compiled key-shaped patterns
  (``sk-``, ``ghp_``/``github_pat_``, ``AKIA``, ``Bearer``, PEM private-key
  blocks). The pattern layer runs even when no environment was collected.
- Literal values are replaced longest-first so an overlapping pair collapses
  into ONE ``[redacted:value]`` token instead of leaking a suffix.
- Linear-time safe: every pattern is a fixed literal prefix followed by
  non-nested quantifiers — no catastrophic backtracking. A 1 MB benign input
  must redact in under 100 ms (pinned by ``tests/test_redact.py``).
- ``NON_SECRET_ENV_KEYS`` is a noise filter (PATH and friends), never a
  safety control: a key-shaped value under a skipped key is still caught by
  the pattern layer.
- Stdlib only (safety package rule, docs/ARCHITECTURE.md §4).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping

#: Literal values shorter than this are never collected: redacting tiny
#: strings ("true", "8080") would shred benign text far more than it protects.
MIN_VALUE_LEN = 8

#: Replacement token for literal-value hits; pattern hits carry their label.
VALUE_TOKEN = "[redacted:value]"

#: Env keys whose values are machine configuration (paths, locale, shell),
#: not secrets — skipping them keeps redaction from mangling every mention of
#: C:\Windows. Compared casefolded (Windows env keys are case-insensitive).
#: Noise filter ONLY: key-shaped values under these keys are still caught by
#: KEY_PATTERNS, so this list must never be treated as a security control.
NON_SECRET_ENV_KEYS: frozenset[str] = frozenset(
    {
        "path",
        "pathext",
        "psmodulepath",
        "comspec",
        "systemroot",
        "systemdrive",
        "windir",
        "home",
        "homepath",
        "homedrive",
        "userprofile",
        "appdata",
        "localappdata",
        "programdata",
        "programfiles",
        "programfiles(x86)",
        "programw6432",
        "temp",
        "tmp",
        "tmpdir",
        "pwd",
        "oldpwd",
        "shell",
        "term",
        "lang",
        "computername",
        "hostname",
        "processor_identifier",
        "virtual_env",
    }
)

#: Key-shaped secret patterns, compiled once, each with a stable label that
#: becomes its replacement token ``[redacted:<label>]``. Every pattern is a
#: literal prefix + character-class quantifiers, so matching is linear. The
#: PEM pattern goes first so a whole block collapses into one token before
#: the narrower token patterns can nibble at its base64 body; its lazy body
#: scan costs one O(n) forward pass per BEGIN marker, nothing worse.
KEY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private-key",
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----"
            r".*?"
            r"-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    ("openai-key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("github-token", re.compile(r"ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}")),
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("bearer-token", re.compile(r"Bearer\s+[A-Za-z0-9._\-]{16,}")),
)


class Redactor:
    """Replaces literal secret values and key-shaped tokens in text.

    Built from a set of literal secret values; the key-shaped KEY_PATTERNS
    are always active regardless of what (if anything) was collected. The
    values themselves are kept on a private attribute and never appear in
    ``repr()`` — a Redactor must be safe to log.
    """

    def __init__(self, values: Iterable[str] = ()) -> None:
        # longest-first: at any position the alternation tries longer literals
        # before their substrings, so overlaps collapse into one token
        ordered = sorted({v for v in values if v}, key=lambda v: (-len(v), v))
        self._values: tuple[str, ...] = tuple(ordered)
        self._literal_re: re.Pattern[str] | None = (
            re.compile("|".join(re.escape(v) for v in ordered)) if ordered else None
        )

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        dotenv: Mapping[str, str] | None = None,
    ) -> Redactor:
        """Collect literal secret values from the environment and a parsed .env.

        ``env`` defaults to ``os.environ``; pass a dict in tests. ``dotenv``
        is the already-parsed key→value mapping of a .env file — this module
        never reads files. Values shorter than MIN_VALUE_LEN are skipped in
        both sources; env keys in NON_SECRET_ENV_KEYS are skipped as noise
        (.env entries are presumed secret, so only the length filter applies).
        """
        source: Mapping[str, str] = os.environ if env is None else env
        values = {
            value
            for key, value in source.items()
            if len(value) >= MIN_VALUE_LEN and key.casefold() not in NON_SECRET_ENV_KEYS
        }
        values.update(v for v in (dotenv or {}).values() if len(v) >= MIN_VALUE_LEN)
        return cls(values)

    def redact(self, text: str | None) -> str:
        """One logical pass: literal values first (longest-first), then patterns.

        Falsy input (None, "") returns "" — a redaction boundary must never
        raise on the empty case.
        """
        if not text:
            return ""
        if self._literal_re is not None:
            text = self._literal_re.sub(VALUE_TOKEN, text)
        for label, pattern in KEY_PATTERNS:
            text = pattern.sub(f"[redacted:{label}]", text)
        return text


#: Pattern-only until boot installs an env-derived instance via
#: set_default_redactor(); key-shaped secrets are caught even before wiring.
_default_redactor = Redactor()


def set_default_redactor(redactor: Redactor) -> None:
    """Install the process-wide default Redactor (boot: ``Redactor.from_env()``)."""
    global _default_redactor
    _default_redactor = redactor


def redact_context(text: str | None) -> str:
    """Choke point 1: outbound context, applied before provider calls (IC-502)."""
    return _default_redactor.redact(text)


def redact_transcript(text: str | None) -> str:
    """Choke point 2: transcript rendering — what the user (and logs) see."""
    return _default_redactor.redact(text)


def redact_audit(text: str | None) -> str:
    """Choke point 3: audit lines — previews must be scrubbed before disk."""
    return _default_redactor.redact(text)
