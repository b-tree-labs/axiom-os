# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tenant simulation: one node, several sites, a dev account per persona.

Nothing here is a permission model. A persona is a name for a role the gate
already has; an account is a real gate account bound to its tenant's site
(``users.json`` ``site``, the ``:context`` of its principal handle); the node
is bounded to the declared sites with ``AXIOM_SERVED_SITES``
(``axiom.infra.site_scope``). What a persona can see is what the gate and the
site scope decide, exactly as in production. Passwords live in the vault.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from . import config

SITES_ENV = "AXI_DEV_SITES"
SERVED_SITES_ENV = "AXIOM_SERVED_SITES"


class TenantProblem(ValueError):
    """Said as is: what is wrong and the next command."""


def host_for(tenant_id: str, front: int) -> str:
    """``<tenant>.localhost:<front>``: ``*.localhost`` is loopback, and cookies are per host."""
    return f"http://{tenant_id}.localhost:{front}"


def resolve_config(
    root: Path, tenant: config.Tenant, env: dict[str, str] | None = None
) -> Path | None:
    """The tenant's config directory, or None when it declares none.

    From the checkout, else under ``$AXI_DEV_SITES``; never copied anywhere.
    A declared path that is absent is a problem with the fix named, not a skip.
    """
    if not tenant.config:
        return None
    env = os.environ if env is None else env
    base = Path(tenant.config).expanduser()
    candidates = [base if base.is_absolute() else (Path(root) / base).resolve()]
    if env.get(SITES_ENV) and not base.is_absolute():
        candidates.append(Path(env[SITES_ENV]).expanduser() / base.name)
    for c in candidates:
        if c.is_dir():
            return c
    raise TenantProblem(
        f"tenant {tenant.id!r}: its config {tenant.config!r} was not found. Clone that site "
        f"repository next to this checkout, or set {SITES_ENV} to the directory holding your site checkouts."
    )


def known_roles() -> set[str]:
    from axiom.extensions.builtins.webgate.role_bundles import default_bundle_registry

    reg = default_bundle_registry()
    from axiom.extensions.builtins.webgate.role_bundles import role_defined
    from axiom.governance.roles import LEGACY_ROLE_ALIASES

    # A retired id still names its successor's role, so it is not "unknown" (ADR-261).
    return set(reg.roles()) | {old for old, new in LEGACY_ROLE_ALIASES.items() if role_defined(reg, new)}


def personas_for(cfg: config.DevConfig) -> dict[str, str]:
    """Persona -> role, refusing a role the gate does not have."""
    known = known_roles()
    bad = {p: r for p, r in cfg.personas.items() if r not in known}
    if bad:
        raise TenantProblem(
            "dev.toml [personas] names roles the gate does not have: "
            + ", ".join(f"{p} -> {r}" for p, r in sorted(bad.items()))
            + f". Known roles: {', '.join(sorted(known))}."
        )
    return dict(cfg.personas)


def email_for(tenant_id: str, persona: str) -> str:
    return f"{persona}.{tenant_id}@dev.localhost"


def secret_for(lane: str, tenant_id: str, persona: str) -> str:
    return f"dev-gate-{lane}-{tenant_id}-{persona}"


def served_sites(cfg: config.DevConfig) -> str:
    return ",".join(sorted({t.site for t in cfg.tenants}))


def ensure_accounts(
    *, accounts: Path, lane: str, cfg: config.DevConfig, vault: Any
) -> list[dict[str, str]]:
    """One real gate account per tenant and persona, bound to the tenant's site."""
    from axiom.extensions.builtins.webgate.skills._accounts import generate_password
    from axiom.webauth import get_password_hash, load_user_records, upsert_user_record

    personas = personas_for(cfg)
    accounts.parent.mkdir(parents=True, exist_ok=True)
    have = {r["email"] for r in load_user_records(accounts)} if accounts.exists() else set()
    out: list[dict[str, str]] = []
    for t in cfg.tenants:
        for persona, role in sorted(personas.items()):
            email = email_for(t.id, persona)
            name = secret_for(lane, t.id, persona)
            if email in have and vault.exists(name):
                out.append(
                    {
                        "tenant": t.id,
                        "persona": persona,
                        "email": email,
                        "role": role,
                        "secret": name,
                    }
                )
                continue
            password = generate_password()
            vault.set(
                name,
                password.encode("utf-8"),
                notes=f"dev persona {persona} of tenant {t.id} on lane {lane}",
            )
            upsert_user_record(
                accounts,
                email=email,
                password_hash=get_password_hash(password),
                roles=[role],
                user_id=email,
                name=f"Sample {persona} ({t.id})",
                attributes={"sample": True, "dev_persona": persona},
                overwrite=True,
                site=t.site,
            )
            out.append(
                {"tenant": t.id, "persona": persona, "email": email, "role": role, "secret": name}
            )
    os.chmod(accounts, 0o600)
    return out
