# ADR-109 — Build provenance: every published artifact is attested, keylessly

**Status:** Accepted (ratifies existing behaviour) — 2026-09-10
**Owner:** @ben
**Related:** [spec-aeos-0.1](../specs/spec-aeos-0.1.md) §3.6 (signed releases by
default — this ADR enforces its first half and names the second as unmet),
ADR-039 (Ed25519 signing of *computed results* — a different scheme for a
different purpose), ADR-022 (site install layout).
Supersedes nothing.

## Context

Every package this platform publishes to PyPI already carries a signed provenance
attestation. Nobody decided that in writing.

`pypa/gh-action-pypi-publish` mints [PEP 740](https://peps.python.org/pep-0740/)
attestations by default when a workflow publishes through Trusted Publishing with
`id-token: write`. The signing is Sigstore's keyless flow: a short-lived
certificate is issued by Fulcio against the workflow's OIDC identity, used once,
and logged to the Rekor transparency log. There is no signing key, so there is no
key to leak, rotate, or lose.

Verified on PyPI 2026-09-09:

| package | attested to |
|---|---|
| this platform's own distribution | its repository, via `publish.yml` |
| a downstream domain package, two releases checked | its own repository, via a publish job inside its CI workflow |

Each bundle carries an x509 chain binding the artifact to a repository, a
workflow file, and a ref. The downstream case matters because it shows the
guarantee travels: a consumer that publishes through the same action inherits
it without configuring anything, which is also how it can lose it without
noticing.

**It is not that nobody decided; it is that nothing enforces it.** AEOS §3.6,
"Signed releases by default", already requires exactly this, and is explicit
about a second half:

> Every AEOS release is signed via Sigstore's keyless OIDC flow. Installers
> verify signatures before executing extension code. Unsigned extensions install
> only with explicit `--allow-unsigned` override. This is a direct response to
> the ClawHavoc incident.

So the standard mandates it. What is missing is any connection between that
sentence and the build. `sigstore`, `cosign`, `rekor` and `fulcio` appear in no
workflow and no test; the publish step here is a bare `uses:` with no `with:`
block at all, so the guarantee rests on a third party's default rather than on
our requirement; and the only trace in a build log is a notice line reading
`Generating and uploading digital attestations`.

We therefore satisfy the first sentence of §3.6 by accident, and the second and
third not at all. There is no installer verification and no `--allow-unsigned`
gate to override.

A guarantee that exists only as somebody else's default has a specific failure
mode: it can be removed without anything going red. Set `attestations: false`,
drop `id-token: write`, or swap the action for a `twine upload`, and every test
still passes, CI stays green, the release still publishes, and the artifacts
silently stop being attested. Nobody finds out until someone downstream asks us
to prove where a wheel came from.

That is the shape this codebase already has a name for: a check that cannot fail.

## Decision

**D1 — Build provenance is a property of the platform, not a convenience.**
Every artifact published from this repository is attested. Publishing without
attestation is a defect, not a configuration preference. This restates AEOS
§3.6 rather than adding policy; what is new is that something now checks.

**D2 — Keyless, via Trusted Publishing.** We use OIDC-based Trusted Publishing
with Sigstore-backed PEP 740 attestations, and we do not hold a signing key. A
long-lived key is a liability we would have to protect, rotate and eventually
explain; a short-lived certificate bound to a workflow identity is a better
statement about where the artifact came from and cannot be stolen from a
developer's laptop.

**D3 — The guarantee is asserted explicitly, not inherited.** The publish
workflow states `attestations: true` even though it is the current default, and
a test reads the workflow and fails if the declaration, the OIDC permission, or
the publishing action is missing. A default we depend on is written down as an
intention, so that removing it takes a deliberate act that a reviewer sees.

**D4 — The verification half of §3.6 is unmet, and this ADR does not close it.**
We sign what we publish; we do not check what we install. `pip install` on a
node, the canary updater and the site manifest all take a wheel on trust, and no
installer refuses an unsigned extension. That is a standing gap against our own
standard, not merely a nice-to-have, and naming it is the point — a half-kept
requirement that reads as kept is worse than one openly outstanding. Signing
without verifying protects consumers of our packages rather than us.

## Consequences

### Positive

- The claim "this wheel was built by this workflow from this repository" is
  checkable by anyone, without asking us.
- No signing key exists, so no key management, rotation policy, or breach story.
- The intention is now legible to a reviewer of the workflow file rather than
  being an invisible property of an action's defaults.
- The test converts a silent removal into a red build.

### Negative

- The chain depends on GitHub's OIDC issuer, Fulcio and Rekor being available at
  publish time. A Sigstore outage blocks a release rather than degrading it, and
  we accept that: an unattested release is worse than a late one.
- A repository that publishes from a different workflow, or from a fork, gets no
  attestation and the test does not know to look for it. The test guards the
  workflows it can see.

### Honest about what this does not buy

An attestation says *where a wheel was built*. It does not say the source was
reviewed, the dependencies were pinned, or the build was reproducible. It is a
provenance claim, not a safety claim, and it should not be cited as one.

## Compliance with project conventions

Domain-agnostic: this ADR names no reactor, facility, site or consumer. A
consumer inherits the decision by publishing through the same action, but not
the check — a repository that publishes from a job inside a shared CI workflow
needs its own guard, and one additionally needs to assert that the publish job
stays gated on a version tag, since that workflow also runs on every push.

## Open questions (carry forward, not blockers)

1. **Verification on install.** Should `pip install` on a node verify PyPI
   provenance before the wheel lands? `pip` cannot do this natively today;
   `pypi-attestations` can, out of band. The site manifest is the natural place
   to require it, and ADR-022 is the natural place to say so.
2. **The extension registry is where §3.6 actually bites.** Container images and
   the site wheel feed are unattested, but the registry matters most: an
   extension is code that runs inside the platform, and §3.6 was written in
   response to an incident of exactly that shape. Installer verification and the
   `--allow-unsigned` override are both unimplemented.
3. **Do we ever check Rekor?** An attestation nobody verifies is a receipt in a
   drawer. Verifying at least our own releases, on a schedule, would make the
   guarantee observable rather than assumed.
