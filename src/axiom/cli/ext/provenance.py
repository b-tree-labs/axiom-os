# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Sigstore/PEP 740 provenance verification for remote installs — ADR-109 §D4.

ADR-109 established that everything this platform publishes to PyPI carries a
signed PEP 740 provenance attestation (Sigstore keyless, minted by Trusted
Publishing), and named the second half of AEOS §3.6 / §9.2 unmet: nothing
verified those attestations at install time. This module is that second half.

The trust model, in one paragraph: a PEP 740 attestation binds a distribution
file to the *identity that built it* — a GitHub repository + workflow, proven
by a short-lived Fulcio certificate and logged to Rekor. Verification therefore
needs an **expected identity** to compare against; verifying "against whoever
signed it" would be a rubber stamp. The expected identity comes from, in
order: an explicit ``--publisher`` override, a pin in
``$AXIOM_HOME/trusted-publishers.json``, or the built-in pins for the
platform's own distributions. No identity -> refusal, not trust-on-first-use.

Fail-closed doctrine (the non-negotiables):

- A missing verifier dependency (``pypi-attestations``) REFUSES remote
  installs and says what to install. It never warn-and-continues.
- The dependency check runs before any network fetch, so an unverifiable
  machine never even pulls the provenance document.
- Every refusal is a distinct exception type naming what failed, so callers
  can print "identity mismatch" vs "missing attestation" vs "network error"
  rather than a generic failure.
- The ``--no-verify`` escape hatch is not silent: callers record it via
  :func:`record_verification_bypass`, the same receipt pattern as the
  pre-push hook's ``~/.axi/pre-push-bypass.log``.

Local ``file://`` sources (the v0.1 registry, ``--from-url file://``,
builtins) are exempt: there is no PyPI provenance for a local tarball, and
those paths keep their existing ed25519 detached-signature verification.
The install verbs state that exemption in their output rather than implying
Sigstore coverage that is not there.
"""

from __future__ import annotations

import datetime as _dt
import getpass
import importlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from axiom.cli.ext.commands.config import _axiom_home

# ---------------------------------------------------------------------------
# Error taxonomy — one class per refusal category
# ---------------------------------------------------------------------------


class ProvenanceError(RuntimeError):
    """Base class for every provenance-verification refusal."""

    category = "provenance"


class VerifierUnavailable(ProvenanceError):
    """The ``pypi-attestations`` verifier is not importable. Fail closed."""

    category = "missing-dependency"


class UnknownPublisher(ProvenanceError):
    """No expected publisher identity is declared for the package."""

    category = "unknown-publisher"


class MissingAttestation(ProvenanceError):
    """PyPI has no PEP 740 provenance for the file (or an empty document)."""

    category = "missing-attestation"


class IdentityMismatch(ProvenanceError):
    """The provenance is signed, but not by the expected identity."""

    category = "identity-mismatch"


class AttestationInvalid(ProvenanceError):
    """Cryptographic verification of the attestation failed."""

    category = "attestation-invalid"


class ProvenanceNetworkError(ProvenanceError):
    """The provenance document could not be fetched (network, 5xx, ...)."""

    category = "network"


# ---------------------------------------------------------------------------
# Expected publisher identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ExpectedPublisher:
    """The identity a distribution's provenance must be signed under.

    ``repository`` is the GitHub ``owner/repo`` slug. ``workflow`` is the
    workflow filename (e.g. ``publish.yml``); when ``None`` any workflow in
    the pinned repository is accepted — repository-level trust, the same
    granularity PyPI Trusted Publishing scopes to at minimum. ``environment``
    optionally pins the GitHub Actions environment.
    """

    repository: str
    workflow: str | None = None
    environment: str | None = None

    @property
    def display(self) -> str:
        if self.workflow:
            return f"{self.repository}@{self.workflow}"
        return self.repository


def parse_publisher_spec(spec: str) -> ExpectedPublisher:
    """Parse ``owner/repo[@workflow.yml]`` into an :class:`ExpectedPublisher`."""
    repo, _, workflow = spec.partition("@")
    repo = repo.strip()
    if "/" not in repo or repo.startswith("/") or repo.endswith("/"):
        raise ValueError(
            f"publisher spec {spec!r} is not of the form owner/repo[@workflow.yml]"
        )
    return ExpectedPublisher(repository=repo, workflow=workflow.strip() or None)


#: Built-in pins for the platform's own PyPI distributions. The values match
#: the trusted publisher configured on pypi.org and asserted by
#: tests/release/test_build_provenance.py on the producing side.
PLATFORM_PUBLISHERS: dict[str, ExpectedPublisher] = {
    "axiom-os-lm": ExpectedPublisher(
        repository="b-tree-labs/axiom-os-private",
        workflow="publish.yml",
        environment="pypi",
    ),
}


def trusted_publishers_path() -> Path:
    """Return ``$AXIOM_HOME/trusted-publishers.json`` — the operator pin file."""
    return _axiom_home() / "trusted-publishers.json"


def load_trusted_publishers() -> dict[str, ExpectedPublisher]:
    """Read the operator's publisher pins. Missing/corrupt file -> empty map.

    (Corrupt-is-empty is safe here because an absent pin *refuses*; it can
    never silently widen trust.)
    """
    path = trusted_publishers_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    out: dict[str, ExpectedPublisher] = {}
    for name, entry in (data.get("publishers") or {}).items():
        if not isinstance(entry, dict):
            continue
        repository = entry.get("repository")
        if not repository:
            continue
        out[str(name)] = ExpectedPublisher(
            repository=str(repository),
            workflow=entry.get("workflow") or None,
            environment=entry.get("environment") or None,
        )
    return out


def resolve_expected_publisher(
    name: str, override: ExpectedPublisher | None = None
) -> ExpectedPublisher:
    """Resolve the expected identity for PyPI package ``name``.

    Order: explicit override -> ``trusted-publishers.json`` pin -> built-in
    platform pins. No match raises :class:`UnknownPublisher` — an unknown
    identity is a refusal, never trust-on-first-use.
    """
    if override is not None:
        return override
    pinned = load_trusted_publishers().get(name)
    if pinned is not None:
        return pinned
    builtin = PLATFORM_PUBLISHERS.get(name)
    if builtin is not None:
        return builtin
    raise UnknownPublisher(
        f"no expected publisher identity is declared for {name!r}; pass "
        f"--publisher owner/repo[@workflow.yml] or pin it under 'publishers' in "
        f"{trusted_publishers_path()} (trusted-publishers.json). Verification "
        "against an undeclared identity would accept anyone's signature."
    )


# ---------------------------------------------------------------------------
# Verifier dependency — fail closed
# ---------------------------------------------------------------------------


def require_verifier():
    """Import and return :mod:`pypi_attestations`, or refuse.

    This is the fail-closed gate: when the verifier is missing, remote
    installs REFUSE with the exact dependency to install. There is no
    warn-and-continue on this path.
    """
    try:
        return importlib.import_module("pypi_attestations")
    except ImportError as exc:
        raise VerifierUnavailable(
            "the Sigstore verifier is not installed, so this remote install is "
            "refused (verification is mandatory for remote sources per AEOS "
            "§9.2 / ADR-109). Install it with: pip install pypi-attestations "
            "(also available as the platform's 'verify' extra) — or, if you "
            "accept an unverified artifact, re-run with --no-verify (recorded "
            "to the bypass audit log)."
        ) from exc


# ---------------------------------------------------------------------------
# Provenance fetch (PyPI integrity API, PEP 740)
# ---------------------------------------------------------------------------


PROVENANCE_URL_TEMPLATE = "https://pypi.org/integrity/{name}/{version}/{filename}/provenance"
_PROVENANCE_ACCEPT = "application/vnd.pypi.integrity.v1+json"


def fetch_provenance(
    name: str, version: str, filename: str, *, timeout: float = 30.0
) -> dict[str, Any]:
    """Fetch the PEP 740 provenance document for one distribution file.

    404 means PyPI holds no attestation for the file — a
    :class:`MissingAttestation` refusal, distinct from
    :class:`ProvenanceNetworkError` (could not ask).
    """
    url = PROVENANCE_URL_TEMPLATE.format(name=name, version=version, filename=filename)
    try:
        resp = requests.get(url, headers={"Accept": _PROVENANCE_ACCEPT}, timeout=timeout)
    except requests.RequestException as exc:
        raise ProvenanceNetworkError(
            f"could not fetch provenance for {name} {version} from {url}: {exc}"
        ) from exc
    if resp.status_code == 404:
        raise MissingAttestation(
            f"PyPI has no PEP 740 provenance for {name} {version} ({filename}); "
            "the release was published without attestations. Refusing to "
            "install an unattested remote artifact."
        )
    if resp.status_code != 200:
        raise ProvenanceNetworkError(
            f"provenance fetch for {name} {version} returned HTTP "
            f"{resp.status_code} from {url}"
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise ProvenanceNetworkError(
            f"provenance response for {name} {version} was not valid JSON: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise ProvenanceNetworkError(
            f"provenance response for {name} {version} was not a JSON object"
        )
    return data


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VerificationResult:
    """A successful verification, for display + receipts."""

    name: str
    version: str
    filename: str
    repository: str
    workflow: str
    attestation_count: int


def _claimed_identity(bundle: dict[str, Any]) -> tuple[str, str, str, str]:
    pub = bundle.get("publisher") or {}
    return (
        str(pub.get("kind", "")),
        str(pub.get("repository", "")),
        str(pub.get("workflow", "")),
        str(pub.get("environment", "")),
    )


def matching_bundles(
    provenance: dict[str, Any], expected: ExpectedPublisher
) -> list[dict[str, Any]]:
    """Return the attestation bundles whose *claimed* publisher matches.

    This is the identity layer only — the claim is untrusted until
    :func:`verify_distribution` cryptographically binds it. Separating the
    two lets refusals say "signed by X, expected Y" instead of a generic
    verification failure.
    """
    out: list[dict[str, Any]] = []
    for bundle in provenance.get("attestation_bundles") or []:
        kind, repository, workflow, environment = _claimed_identity(bundle)
        if kind.lower() != "github":
            continue
        if repository.lower() != expected.repository.lower():
            continue
        if expected.workflow is not None and workflow != expected.workflow:
            continue
        if expected.environment is not None and environment != expected.environment:
            continue
        out.append(bundle)
    return out


def verify_distribution(
    dist_path: Path,
    *,
    name: str,
    version: str,
    expected: ExpectedPublisher,
    provenance: dict[str, Any] | None = None,
) -> VerificationResult:
    """Verify ``dist_path`` against its PEP 740 provenance and ``expected``.

    Order of operations (deliberate, fail-closed):

    1. Require the verifier dependency — before any network traffic.
    2. Fetch the provenance document (unless handed in).
    3. Identity layer: at least one bundle must *claim* the expected
       publisher, else :class:`IdentityMismatch` naming both sides.
    4. Cryptographic layer: every attestation in the matching bundles must
       verify against the expected identity via ``pypi-attestations``
       (Sigstore: Fulcio chain + Rekor inclusion + artifact digest), else
       :class:`AttestationInvalid`.

    Returns a :class:`VerificationResult`; raises a
    :class:`ProvenanceError` subclass otherwise. Never returns a "soft fail".
    """
    pa = require_verifier()

    if provenance is None:
        provenance = fetch_provenance(name, version, dist_path.name)

    bundles = provenance.get("attestation_bundles") or []
    if not bundles:
        raise MissingAttestation(
            f"the provenance document for {name} {version} contains no "
            "attestation bundles; refusing to install."
        )

    matched = matching_bundles(provenance, expected)
    if not matched:
        claimed = ", ".join(
            f"{repo or '?'}@{wf or '?'}"
            for _, repo, wf, _env in (_claimed_identity(b) for b in bundles)
        )
        raise IdentityMismatch(
            f"{name} {version} is attested, but not by the expected identity: "
            f"expected {expected.display}, provenance claims ({claimed}). "
            "If the publisher legitimately changed, update the pin in "
            f"{trusted_publishers_path()} or pass --publisher explicitly."
        )

    dist = pa.Distribution.from_file(dist_path)
    verified = 0
    for bundle in matched:
        _kind, repository, workflow, environment = _claimed_identity(bundle)
        identity = pa.GitHubPublisher(
            repository=expected.repository,
            # When the pin does not fix a workflow, hold the certificate to
            # the workflow the bundle claims — verify() then proves the claim.
            workflow=expected.workflow or workflow,
            environment=expected.environment or (environment or None),
        )
        for raw in bundle.get("attestations") or []:
            attestation = pa.Attestation.model_validate(raw)
            try:
                attestation.verify(identity, dist)
            except pa.VerificationError as exc:
                raise AttestationInvalid(
                    f"cryptographic verification failed for {name} {version} "
                    f"against {expected.display}: {exc}"
                ) from exc
            verified += 1

    if verified == 0:
        raise MissingAttestation(
            f"the matching provenance bundle for {name} {version} contains no "
            "attestations; refusing to install."
        )

    return VerificationResult(
        name=name,
        version=version,
        filename=dist_path.name,
        repository=expected.repository,
        workflow=expected.workflow or _claimed_identity(matched[0])[2],
        attestation_count=verified,
    )


# ---------------------------------------------------------------------------
# Bypass audit log — the --no-verify receipt
# ---------------------------------------------------------------------------


def bypass_log_path() -> Path:
    """Return ``$AXIOM_HOME/install-verify-bypass.log``.

    Same shape as the pre-push hook's ``~/.axi/pre-push-bypass.log``: an
    append-only TSV of every time someone chose to skip a mandatory gate.
    """
    return _axiom_home() / "install-verify-bypass.log"


def record_verification_bypass(
    *, name: str, version: str, source: str, reason: str
) -> Path:
    """Append a bypass receipt and return the log path.

    Best-effort on the *write* (an unwritable disk must not turn the loud
    path into a crash), but the caller has already warned loudly by the
    time this runs.
    """
    path = bypass_log_path()
    ts = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 — no user db in some containers
        user = os.environ.get("USER", "unknown")
    line = (
        f"{ts}\tpackage={name}\tversion={version}\tsource={source}"
        f"\tuser={user}\treason={reason}\n"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass
    return path


__all__ = [
    "PLATFORM_PUBLISHERS",
    "PROVENANCE_URL_TEMPLATE",
    "AttestationInvalid",
    "ExpectedPublisher",
    "IdentityMismatch",
    "MissingAttestation",
    "ProvenanceError",
    "ProvenanceNetworkError",
    "UnknownPublisher",
    "VerificationResult",
    "VerifierUnavailable",
    "bypass_log_path",
    "fetch_provenance",
    "load_trusted_publishers",
    "matching_bundles",
    "parse_publisher_spec",
    "record_verification_bypass",
    "require_verifier",
    "resolve_expected_publisher",
    "trusted_publishers_path",
    "verify_distribution",
]
