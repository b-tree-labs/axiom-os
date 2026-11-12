# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Credential discovery — find credential material living outside the store.

The store can only rotate, audit, or expire what it knows about. Anything a
human pasted into a URL, an env file, or a shell profile is invisible to it, so
`secrets audit` reports a clean bill while an Owner-scoped token ages in a git
remote. That gap is what this closes.

Two registries, so neither locations nor vendors are hardcoded:

* **probes** — *where* credential material should not live (git remotes,
  credential files, env files, unit files, …). Adding a location kind is a
  registration.
* **matchers** — *what* credential material looks like (vendor prefixes, URL
  userinfo, …). Adding a vendor is a registration.

Findings never carry values — only a fingerprint, so the same credential can be
recognised in two places without either being printed, logged, or stored.

The same scan answers two questions, which is why they share a mechanism:
"what is unmanaged?" (findings whose fingerprint is not in the store) and "who
consumes credential X?" (findings whose fingerprint is X's).
"""

from axiom.extensions.builtins.secrets.discovery.matchers import (
    MATCHERS,
    Matcher,
    match_text,
    register_matcher,
)
from axiom.extensions.builtins.secrets.discovery.model import Finding, RawHit, fingerprint
from axiom.extensions.builtins.secrets.discovery.stale import StaleHolder, correlate_stale
from axiom.extensions.builtins.secrets.discovery.probes import (
    PROBES,
    EnvFileProbe,
    GitCredentialsProbe,
    GitRemoteProbe,
    ProcessEnvProbe,
    register_probe,
    run_probes,
)

__all__ = [
    "MATCHERS",
    "PROBES",
    "EnvFileProbe",
    "Finding",
    "GitCredentialsProbe",
    "GitRemoteProbe",
    "Matcher",
    "ProcessEnvProbe",
    "RawHit",
    "StaleHolder",
    "correlate_stale",
    "fingerprint",
    "match_text",
    "register_matcher",
    "register_probe",
    "run_probes",
]
