# Security Policy

IronCore runs shell commands proposed by a language model, edits files on your disk, and
can spawn MCP servers. Its safety model is documented and deliberately invites scrutiny
([`docs/SAFETY.md`](../docs/SAFETY.md)) — so it needs a way to hear about holes in that
model **privately**, before they are public.

## Reporting a vulnerability

**Do not open a public issue for a security report.**

Use GitHub's private vulnerability reporting:

> **[Security tab](https://github.com/RealDealCPA-VR/IronCore/security/advisories/new)** →
> *Report a vulnerability*

That opens a private advisory visible only to you and the maintainers. If the link 404s,
private reporting has not been enabled yet on this repository — open a public issue that
says only *"requesting a private security contact, no details"* and a maintainer will open
a draft advisory and invite you to it. Never put the details in that placeholder issue.

### What to include

- The version (`ironcore --version`) and how it was installed.
- Which safety boundary you believe is crossed — name the section of
  [`docs/SAFETY.md`](../docs/SAFETY.md) if you can.
- A minimal reproduction. A config file plus the model output that triggers it is ideal;
  you do not need a real model — `MockProvider` can stand in for one.
- What an attacker gains: file read/write outside the jail, command execution without an
  approval, network egress that was never approved, secret disclosure.

### What to expect

- **Acknowledgement within 7 days.** This is a small project; that is a best effort, not
  a contractual SLA.
- We will confirm the issue, agree a fix and a disclosure timeline with you, and credit
  you in the advisory and CHANGELOG unless you ask us not to.
- Please give us a reasonable window to ship a fix before public disclosure.

## Scope

**In scope** — anything that defeats a boundary [`docs/SAFETY.md`](../docs/SAFETY.md)
claims to hold:

- Escaping the **path jail** — reading or writing outside the workspace.
- Executing a command **without the approval the mode/risk gate requires**, or getting a
  NET-risk tool auto-approved (network is never auto-allowed, in any mode).
- **Plan mode mutating anything** — including through a workflow subagent.
- **Prompt injection** from tool output or a fetched page that escapes the UNTRUSTED
  fence and drives the harness.
- **Secret disclosure** — API keys or redacted values reaching the transcript, the
  session log, the audit log, or the provider.
- Sandbox escape from a **plugin** or an **MCP server** beyond the privileges
  [`docs/SAFETY.md`](../docs/SAFETY.md) §8 and §10 grant it.
- Undo/snapshot corruption that silently loses a user's work.

**Out of scope:**

- Anything requiring the attacker to already control your config file, your `PATH`, or
  your machine.
- The model producing wrong, insulting, or low-quality code. That is a capability
  problem, not a vulnerability.
- Damage done by a command **you explicitly approved**, or by running in Auto mode with
  `network_tools = true`. Those are documented trade-offs
  ([`docs/SAFETY.md`](../docs/SAFETY.md) §9), not bugs — if you think the *documentation*
  understates the risk, that is a normal issue and a welcome one.
- Vulnerabilities in a third-party model server (Ollama, vLLM, llama.cpp). Report those
  upstream; if IronCore makes them *worse*, that part is in scope.
- Denial of service against your own machine via a runaway loop — budgets bound this, and
  a tighter bound is a normal issue.

## Supported versions

Only the latest released version is supported. IronCore is pre-1.0: fixes ship in a new
release, not as patches to old ones.

---

<sub>**Maintainer setup:** enable Settings → Code security → *Private vulnerability
reporting* so the advisory link above resolves. Until then the fallback paragraph is the
only private path, and it costs a reporter a round trip.</sub>
