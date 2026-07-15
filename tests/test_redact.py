"""Redaction pins: planted secrets die, benign text passes untouched, 1MB under 100ms."""

import time

import ironcore.safety.redact as redact_module
from ironcore.safety.redact import (
    KEY_PATTERNS,
    MIN_VALUE_LEN,
    VALUE_TOKEN,
    Redactor,
    redact_audit,
    redact_context,
    redact_transcript,
    set_default_redactor,
)

# --- ten distinct planted secrets: 5 key-shaped + 5 env-value literals ---
SK_KEY = "sk-Abc123Def456Ghi789Jkl012Mno345"
GHP_TOKEN = "ghp_AbCdEf0123456789GhIjKlMnOpQrStUv"
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
BEARER_TOKEN = "eyJhbGciOiJIUzI1NiJ9.payload-part_signature42"
PEM_BODY = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQC7Vn"
PEM_BLOCK = f"-----BEGIN RSA PRIVATE KEY-----\n{PEM_BODY}\n-----END RSA PRIVATE KEY-----"
ENV_SECRETS = {
    "API_KEY": "env-secret-alpha-9931",
    "DB_PASSWORD": "env-secret-bravo-8842",
    "SIGNING_SECRET": "env-secret-charlie-7753",
}
DOTENV_SECRETS = {
    "WEBHOOK_SECRET": "dotenv-secret-delta-6664",
    "SMTP_PASSWORD": "dotenv-secret-echo-5575",
}
PLANTED = [
    SK_KEY,
    GHP_TOKEN,
    AWS_KEY,
    BEARER_TOKEN,
    PEM_BODY,
    *ENV_SECRETS.values(),
    *DOTENV_SECRETS.values(),
]


def fixture_text() -> str:
    return (
        f"config loaded: openai={SK_KEY} github={GHP_TOKEN}\n"
        f"aws access key id {AWS_KEY} found in credentials\n"
        f"Authorization: Bearer {BEARER_TOKEN}\n"
        f"{PEM_BLOCK}\n"
        f"env dump: {' '.join(ENV_SECRETS.values())}\n"
        f"dotenv dump: {' '.join(DOTENV_SECRETS.values())}\n"
        "benign trailing line stays put\n"
    )


def test_ten_planted_secrets_zero_survivors():
    assert len(PLANTED) == len(set(PLANTED)) == 10
    out = Redactor.from_env(env=ENV_SECRETS, dotenv=DOTENV_SECRETS).redact(fixture_text())
    for secret in PLANTED:
        assert secret not in out, f"secret survived redaction: {secret[:12]}…"
    assert "benign trailing line stays put" in out


def test_labels_and_value_token_appear_in_output():
    out = Redactor.from_env(env=ENV_SECRETS, dotenv=DOTENV_SECRETS).redact(fixture_text())
    for label in ("openai-key", "github-token", "aws-access-key", "bearer-token", "private-key"):
        assert f"[redacted:{label}]" in out
    assert VALUE_TOKEN in out


def test_benign_text_unchanged():
    text = (
        "The quick brown fox jumps over sk-abc and ghp_xy (both too short).\n"
        "AKIA123 is short too; Bearer alone, and BEGIN PUBLIC KEY is fine.\n"
        "Ordinary code: def risky(n): return n - 1\n"
    )
    r = Redactor.from_env(env=ENV_SECRETS, dotenv=DOTENV_SECRETS)
    assert r.redact(text) == text


def test_empty_and_none_return_empty_string():
    r = Redactor(["some-long-secret-value"])
    assert r.redact("") == ""
    assert r.redact(None) == ""


def test_literal_overlap_longest_first_single_token():
    r = Redactor(["secretvalue1", "secretvalue12345"])
    out = r.redact("x secretvalue12345 y")
    assert out == f"x {VALUE_TOKEN} y"  # no leaked "2345" suffix
    assert r.redact("c secretvalue1 d") == f"c {VALUE_TOKEN} d"


def test_literal_values_with_regex_metacharacters():
    secret = "p@ss(word)+2026!"
    assert Redactor([secret]).redact(f"a {secret} b") == f"a {VALUE_TOKEN} b"


def test_patterns_active_without_any_env():
    for r in (Redactor(), Redactor.from_env(env={}, dotenv={})):
        out = r.redact(f"key={SK_KEY}")
        assert SK_KEY not in out
        assert "[redacted:openai-key]" in out


def test_github_pat_variant_redacted():
    pat = "github_pat_11ABCDEFG0123456789_abcdefghij0123456789"
    out = Redactor().redact(f"token {pat} end")
    assert pat not in out
    assert "[redacted:github-token]" in out


def test_pem_block_crlf_windows_line_endings():
    block = f"-----BEGIN PRIVATE KEY-----\r\n{PEM_BODY}\r\n-----END PRIVATE KEY-----"
    out = Redactor().redact(f"before\r\n{block}\r\nafter")
    assert PEM_BODY not in out
    assert "[redacted:private-key]" in out
    assert "before" in out and "after" in out


def test_from_env_skips_short_and_empty_values():
    env = {"SHORT": "abc", "EMPTY": "", "LONG_SECRET": "long-enough-secret"}
    r = Redactor.from_env(env=env, dotenv=None)
    assert r.redact("abc is fine here") == "abc is fine here"
    assert "long-enough-secret" not in r.redact("x long-enough-secret y")
    assert len("long-enough-secret") >= MIN_VALUE_LEN  # fixture sanity


def test_from_env_skips_path_like_keys_casefolded():
    path_value = r"C:\Tools\bin;C:\Windows\system32"
    r = Redactor.from_env(env={"Path": path_value}, dotenv=None)  # Windows spelling
    text = f"looking in {path_value} for exes"
    assert r.redact(text) == text


def test_dotenv_values_redacted_env_untouched_by_it():
    r = Redactor.from_env(env={}, dotenv={"TOKEN": "dotenv-planted-secret"})
    out = r.redact("t=dotenv-planted-secret")
    assert "dotenv-planted-secret" not in out
    assert VALUE_TOKEN in out


def test_from_env_defaults_to_process_env(monkeypatch):
    monkeypatch.setenv("IRONCORE_TEST_SECRET", "process-env-planted-secret")
    out = Redactor.from_env().redact("v=process-env-planted-secret")
    assert "process-env-planted-secret" not in out


def test_choke_point_helpers_share_the_default_redactor():
    set_default_redactor(Redactor(["installed-secret-0001"]))
    try:
        for helper in (redact_context, redact_transcript, redact_audit):
            out = helper(f"a installed-secret-0001 b {SK_KEY} c")
            assert "installed-secret-0001" not in out
            assert SK_KEY not in out
            assert helper(None) == ""
    finally:
        set_default_redactor(Redactor())  # restore the import-time default


def test_pattern_labels_are_pinned():
    # IC-502 (context) and transcript/audit renderers key off these names
    assert [label for label, _ in KEY_PATTERNS] == [
        "private-key",
        "openai-key",
        "github-token",
        "aws-access-key",
        "bearer-token",
    ]


def test_one_megabyte_benign_input_under_100ms():
    line = "def step(n):\n    return n * 3 + 1  # plain benign code, nothing key-shaped here\n"
    text = (line * (1_048_576 // len(line) + 1))[:1_048_576]
    r = Redactor(
        ["env-secret-alpha-9931", "env-secret-bravo-8842", "env-secret-charlie-7753",
         "dotenv-secret-delta-6664", "dotenv-secret-echo-5575"]
    )
    r.redact(text)  # warm-up
    best = min(_timed(r, text) for _ in range(3))
    assert best < 0.1, f"1MB redact took {best:.3f}s (must be linear-time safe)"
    assert r.redact(text) == text  # benign 1MB comes back byte-identical


def test_many_unclosed_pem_markers_do_not_redos():
    # adversarial: thousands of BEGIN markers with no matching END. A naive
    # lazy `.*?` body scans to end-of-string from every BEGIN — O(n^2). The
    # tempered body keeps it linear. This is the case the benign 1MB test
    # cannot exercise, so it is pinned separately.
    text = "-----BEGIN RSA PRIVATE KEY-----\nnope\n" * 20_000  # ~700 KB, no END
    r = Redactor()
    best = min(_timed(r, text) for _ in range(3))
    assert best < 0.2, f"pathological PEM redact took {best:.3f}s (ReDoS)"


def _timed(r: Redactor, text: str) -> float:
    start = time.perf_counter()
    r.redact(text)
    return time.perf_counter() - start


def test_no_secrets_in_redactor_repr():
    r = Redactor(["very-secret-value-123"])
    assert "very-secret-value-123" not in repr(r)


def test_import_time_default_is_pattern_only(monkeypatch):
    # a fresh default (no boot wiring) must still catch key-shaped secrets
    monkeypatch.setattr(redact_module, "_default_redactor", Redactor())
    assert "[redacted:aws-access-key]" in redact_module.redact_audit(f"id {AWS_KEY}")
