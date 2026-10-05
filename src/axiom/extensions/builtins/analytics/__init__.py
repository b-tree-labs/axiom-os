# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Deterministic series-math tool (ADR-113 Tiers 1-3)."""
from axiom.extensions.builtins.analytics.core import OPS, AnalyticsError, compute, resolve_series

__all__ = ["compute", "resolve_series", "OPS", "AnalyticsError"]
