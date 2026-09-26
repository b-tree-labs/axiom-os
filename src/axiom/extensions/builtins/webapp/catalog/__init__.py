# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The serving projection: gold, made fast to read.

A catalog of what each site streams — one row per (site, stream, channel) with
its unit, row count and time span. The lakehouse stays the analytical source of
truth; this is the always-current projection the API reads in a millisecond
instead of aggregating gold on every page load.

Nothing here knows what a channel measures. That is the point: the vocabulary is
(site, stream, channel) per ADR-050, so a consumer supplies the meaning and the
platform supplies the serving tier.
"""

from axiom.extensions.builtins.webapp.catalog.models import Base, SiteCatalogChannel
from axiom.extensions.builtins.webapp.catalog.store import (
    read_channels,
    read_site_summaries,
    session_scope,
)
from axiom.extensions.builtins.webapp.catalog.sync import project_catalog

__all__ = [
    "Base",
    "SiteCatalogChannel",
    "project_catalog",
    "read_channels",
    "read_site_summaries",
    "session_scope",
]
