# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The served-site rule the library shares with the CLI, reused rather than copied."""

from __future__ import annotations

from axiom.extensions.builtins.attest.service import AttestRefused
from axiom.extensions.builtins.attest.skills._common import resolve_site


def site_or_refuse(site: str | None) -> str:
    """One served site is the default; a site this node does not serve is refused."""
    resolved, error = resolve_site({"site": site})
    if error:
        raise AttestRefused(error.replace("name the site with --site", "name the site with site="))
    return resolved
