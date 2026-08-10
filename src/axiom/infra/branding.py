# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Axiom branding registry.

Domain products (e.g. a consumer extension) register their branding here before
invoking Axiom's CLI. All user-facing text in Axiom reads from the active
branding, so domain products get a white-labeled *identity* (banner, product
name, their own commands) without any code duplication.

Per ADR-048 (distribution model), branding white-labels identity — it does
NOT rebrand the borrowed platform tools (the agents, the Background Service);
those keep neutral, stable names the way coreutils/systemd do across Linux
distros. Branding also drives **tier-scoped visibility**: the platform brand
does not surface a domain product's extensions, and a domain distribution
inherits the platform base but not sibling products (see
``discover_portfolio_members`` and the extension-discovery visibility filter).

Usage (domain product's CLI entry point)::

    from axiom.infra.branding import BrandingConfig, register

    register(BrandingConfig(
        cli_name="acme",
        product_name="Acme OS",
        mascot_name="Ace",
        tagline="The intelligence platform for your domain",
        package_name="acme-os",
    ))

    from axiom.axiom_cli import main
    main()

If no branding is registered, Axiom defaults to Axi.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass  # pylint: disable=too-many-instance-attributes
class BrandingConfig:
    """Branding configuration for a product built on Axiom.

    All fields have Axiom/Axi defaults. Override only what you need.
    """

    # CLI identity
    cli_name: str = "axi"
    product_name: str = "Axiom"
    mascot_name: str = "Axi"
    tagline: str = "The intelligent operations platform"
    package_name: str = "axiom-os-lm"

    # Terminal colors (ANSI escape codes)
    accent_color: str = "\033[38;2;0;207;255m"  # Axiom cyan
    engine_color: str = "\033[38;2;255;140;0m"  # Engine orange
    eye_color: str = "\033[38;2;0;255;255m"  # Sensor cyan

    # Optional overrides
    # If set, called instead of the default Axi banner
    banner_fn: Callable[[], None] | None = None
    # Module path for default chat agent (None = use built-in neut_agent)
    agent_module: str | None = None

    # Infrastructure cluster name — always the framework's, not the consumer's.
    # Consumers adopt the framework cluster rather than creating their own.
    cluster_name: str = "axi-local"

    # Shell alias comment written to .bashrc/.zshrc
    shell_comment: str | None = None

    # Git URL used by the self-update command (None = disable self-update)
    update_repo_url: str | None = None

    def __post_init__(self) -> None:
        if self.shell_comment is None:
            self.shell_comment = f"{self.product_name} CLI shortcut"


# ---------------------------------------------------------------------------
# Registry — last registered wins (domain products load after axiom)
# ---------------------------------------------------------------------------

_registered: list[BrandingConfig] = []
_DEFAULT = BrandingConfig()


def register(config: BrandingConfig) -> None:
    """Register domain branding. Call this before importing the CLI entry point."""
    _registered.append(config)


def get_branding() -> BrandingConfig:
    """Return active branding. Domain product if registered, else Axi default."""
    return _registered[-1] if _registered else _DEFAULT


def reset() -> None:
    """Clear registered branding (used in tests)."""
    _registered.clear()


# ---------------------------------------------------------------------------
# Portfolio member metadata — entry-points group `axiom.portfolio_member`
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PortfolioMember:
    """Identity of an Axiom-portfolio package installed in this venv.

    Discovered at runtime via the ``axiom.portfolio_member`` entry-points
    group. Each portfolio package self-declares — axi-platform never
    needs to know about future products (a consumer extension, X-Foo, Y-Bar, etc).
    """

    package_name: str
    product_name: str
    wrapper_binary: str  # console_script the package installs (e.g., Axiom-Background-Service)


def _portfolio_metadata() -> dict:
    """axi-platform's self-declaration. Returned via the entry-points group.

    Each portfolio package implements its own version of this function
    and declares it in its pyproject.toml under
    ``[project.entry-points."axiom.portfolio_member"]``. A consumer extension will
    add an analog (its own Background-Service); future products
    register themselves the same way.
    """
    return {
        "package_name": "axiom-os-lm",
        "product_name": "Axiom",
        "wrapper_binary": "Axiom-Background-Service",
    }


def discover_portfolio_members() -> list[PortfolioMember]:
    """Enumerate all installed Axiom-portfolio packages.

    Iterates the ``axiom.portfolio_member`` entry-points group. Returns
    one PortfolioMember per package that declared itself. Never raises;
    a package whose entry-point is broken is logged and skipped (the
    cleanup logic still works for the remaining packages).

    Order is install order (most-recently-installed last per
    importlib.metadata's discovery). Used by the Background Service
    cleanup pass to find cross-brand stale registrations and by the
    brand-priority resolver (most-recently-installed wins by default).
    """
    import logging
    from importlib.metadata import entry_points

    log = logging.getLogger(__name__)
    members: list[PortfolioMember] = []
    try:
        eps = entry_points(group="axiom.portfolio_member")
    except Exception as exc:
        log.warning("portfolio_member entry-points lookup failed: %s", exc)
        return members

    for ep in eps:
        try:
            fn = ep.load()
            meta = fn() if callable(fn) else fn
            members.append(
                PortfolioMember(
                    package_name=meta["package_name"],
                    product_name=meta["product_name"],
                    wrapper_binary=meta["wrapper_binary"],
                )
            )
        except Exception as exc:
            log.warning("portfolio_member %r failed to load: %s", ep.name, exc)
    return members
