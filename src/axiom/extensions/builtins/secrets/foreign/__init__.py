# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Foreign-credential surface (issue #667).

Named third-party credentials (GitLab PATs, webhook URLs, HMAC keys):
values in the OS keychain via the SecretStoreProvider factory, metadata
(names, issuer, expiry, git wiring) in a 0600 JSON index. Rotation goes
through the RotationProvider factory (`gitlab-pat` API rotation first,
`guided` interactive fallback) and journals to the #665 action ledger.
"""

from .rotation_providers import (
    ForeignRotationError,
    ForeignRotationOutcome,
    GitLabPatProvider,
    GuidedRotationProvider,
    build_rotation_provider,
    rotation_provider_kinds,
)
from .scrub import scrub_candidates
from .store import ForeignCredentialStore, declared_secret_names

__all__ = [
    "ForeignCredentialStore",
    "ForeignRotationError",
    "ForeignRotationOutcome",
    "GitLabPatProvider",
    "GuidedRotationProvider",
    "build_rotation_provider",
    "declared_secret_names",
    "rotation_provider_kinds",
    "scrub_candidates",
]
