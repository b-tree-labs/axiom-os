# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Mode detection — developer (source) vs operator (installed)."""

from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass
class EnvironmentMode:
    mode: str  # "developer" or "operator"
    axiom_version: str
    axiom_source: str  # path to source or "pypi"
    axiom_editable: bool
    # A domain consumer layer (discovered generically, not named here). Empty
    # when no consumer package is installed.
    consumer_version: str
    consumer_source: str
    consumer_editable: bool

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "axiom": {
                "version": self.axiom_version,
                "source": self.axiom_source,
                "editable": self.axiom_editable,
            },
            "consumer": {
                "version": self.consumer_version,
                "source": self.consumer_source,
                "editable": self.consumer_editable,
            },
        }


def detect_mode() -> EnvironmentMode:
    """Detect whether we're running from source (developer) or installed (operator)."""
    axiom_info = _check_package("axi-platform", "axiom")
    consumer_info = _detect_consumer_package()

    mode = "developer" if axiom_info["editable"] or consumer_info["editable"] else "operator"

    return EnvironmentMode(
        mode=mode,
        axiom_version=axiom_info["version"],
        axiom_source=axiom_info["source"],
        axiom_editable=axiom_info["editable"],
        consumer_version=consumer_info["version"],
        consumer_source=consumer_info["source"],
        consumer_editable=consumer_info["editable"],
    )


def _detect_consumer_package() -> dict:
    """Discover an installed domain-consumer package generically.

    Uses the ``axiom.portfolio_member`` entry-points (the same mechanism the
    Background Service uses) and inspects the first non-platform member, so the
    platform never hardcodes a specific consumer's package name. Returns the
    empty/"not installed" shape when no consumer is present.
    """
    empty = {"version": "", "source": "not installed", "editable": False}
    try:
        from axiom.infra.branding import discover_portfolio_members

        for member in discover_portfolio_members():
            pkg = getattr(member, "package_name", "") or ""
            if pkg and pkg != "axiom-os-lm":
                return _check_package(pkg, pkg.replace("-", "_"))
    except Exception:
        pass
    return empty


def _check_package(dist_name: str, import_name: str) -> dict:
    """Check a package's install mode and version."""
    info: dict = {"version": "", "source": "not installed", "editable": False}

    try:
        from importlib.metadata import distribution

        dist = distribution(dist_name)
        info["version"] = dist.version

        # Check for editable install
        direct_url = dist.read_text("direct_url.json")
        if direct_url:
            url_data = json.loads(direct_url)
            if url_data.get("dir_info", {}).get("editable", False):
                info["editable"] = True
                info["source"] = url_data.get("url", "").replace("file://", "")
                return info

        info["source"] = "pypi"
    except Exception:
        pass

    return info
