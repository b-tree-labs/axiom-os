# Security Policy

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Instead, use one of:

- **GitHub private vulnerability reporting** — on this repo, go to the
  **Security** tab → **Report a vulnerability** (preferred; keeps the report
  private and tracked).
- **Email** — **security@axiom-os.ai** with details and, if possible, a
  reproduction.

Please include:

- A description of the vulnerability and its impact
- Steps to reproduce (a proof-of-concept if you have one)
- Affected version(s) / commit, and any relevant configuration

## Coordinated disclosure

- We aim to **acknowledge within a few business days** and to keep you updated as
  we triage and fix.
- We follow **coordinated disclosure**: please give us a reasonable window
  (target **90 days**, or sooner once a fix ships) before public disclosure. We'll
  work with you on timing.
- **Safe harbor:** we won't pursue or support legal action against good-faith
  research that respects this policy, avoids privacy violations and service
  disruption, and only interacts with accounts/data you own or have permission to
  test. When in doubt, ask first.
- We're happy to credit you in the advisory unless you prefer to remain anonymous.

## Supply-chain integrity

Software supply chains are a real attack surface (a maintainer's trust is part of
the attack surface). Our posture:

- **Releases publish via PyPI Trusted Publishing (OIDC)** — no long-lived API
  tokens that can leak. The release workflow builds, **installs and smoke-tests
  the wheel in a clean environment**, publishes, then verifies the version is
  resolvable from PyPI before creating the GitHub Release.
- **Provenance** — releases are tied to a tagged commit and a GitHub Actions run;
  the build is reproducible from source (`python -m build`).
- **Dependencies** are reviewed on update; we keep the runtime dependency set
  small and avoid pulling unvetted transitive code into the base install.
- **Maintainers** use 2FA and signed (DCO) commits; merge access is limited.
- If you believe a **dependency or a published artifact** has been tampered with
  (typosquat, unexpected maintainer change, malicious version), report it the same
  way as a vulnerability — that's in scope.

## What is (and isn't) in scope

In scope: anything that lets someone bypass authorization, read data they
shouldn't, execute code, escalate privileges, or compromise a build/release.

Usually **not** a vulnerability on its own: missing best-practice headers with no
demonstrated impact, self-inflicted issues requiring an already-compromised host,
or findings that depend on a misconfiguration we document against. When unsure,
report it and let us decide.

## Supported versions

Axiom is pre-1.0 and moving quickly. Security fixes land on `main` and ship in
the next release on PyPI (`axiom-os-lm`). Please test against the latest release
or `main` before reporting.

## Scope

This policy covers the Axiom platform in this repository. Extensions distributed
as separate packages, and domain products built on top of Axiom, are covered by
their own repositories' policies.
