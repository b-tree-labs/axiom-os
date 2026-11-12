# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Skill functions behind ``axi dev`` (ADR-056)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext

CALLER_GOAL = {"caller_goal": "str — one sentence: what you are trying to do"}


def open_vault(ctx: SkillContext):
    from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore

    return ForeignCredentialStore(Path(ctx.state_dir))


def _lane_value(held, checkout: Path) -> dict[str, Any]:
    return {
        "name": held.name,
        "front": held.front,
        "api": held.api,
        "database": held.database,
        "dsn_var": held.dsn_var,
        "isolated": held.isolated,
        "root": str(checkout),
    }


def held_lane(params: dict[str, Any]) -> dict[str, Any] | None:
    """This checkout's own lane if it already holds one; never claims."""
    from axiom.extensions.builtins.lane import naming
    from axiom.extensions.builtins.lane.registry import Registry
    from axiom.extensions.builtins.lane.skills import lanes_path

    checkout = naming.repo_root(Path(params.get("root") or Path.cwd()).resolve())
    if checkout is None:
        return None
    held = Registry(params.get("registry") or lanes_path()).get(naming.slug(checkout.name))
    if held is None or Path(held.root).resolve() != checkout.resolve():
        return None
    return _lane_value(held, checkout)


def topology_running(params: dict[str, Any], ctx) -> bool:
    from axiom.extensions.builtins.lane import naming

    from .. import graph

    checkout = naming.repo_root(Path(params.get("root") or Path.cwd()).resolve())
    return checkout is not None and graph.active(ctx, naming.slug(checkout.name)) is not None


def resolve_lane(params: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """This checkout's lane: the one it holds, or a new one claimed through the registry.

    Ports and the database name are the lane registry's to choose; nothing
    here picks either. A new lane is claimed with its own database
    (``axiom_dev_<checkout>``), so a dev run is isolated from the first run.
    """
    from axiom.extensions.builtins.lane import naming
    from axiom.extensions.builtins.lane.registry import Registry
    from axiom.extensions.builtins.lane.skills import claim, lanes_path

    root = Path(params.get("root") or Path.cwd()).resolve()
    checkout = naming.repo_root(root)
    if checkout is None:
        return {}, [f"{root} is not inside a git checkout"]
    name = naming.slug(checkout.name)
    reg_path = params.get("registry") or lanes_path()
    held = Registry(reg_path).get(name)
    ours = held is not None and Path(held.root).resolve() == checkout.resolve()
    if ours and held.isolated:
        return _lane_value(held, checkout), []
    if ours and held.note != "dev node":
        # Somebody claimed it unmanaged on purpose: say so rather than retake it.
        return _lane_value(held, checkout), []
    claimed = claim.run(
        {
            "root": str(checkout),
            "name": name,
            "prefix": "axiom_dev",
            "registry": params.get("registry"),
            # A lane an earlier dev engine claimed without a database is given
            # one: same ports (the registry keeps a lane's pair), now isolated.
            "replace": ours,
            "ports": list(held.ports) if ours else None,
            "note": "dev node",
        },
        None,
    )
    if not claimed.ok:
        return {}, claimed.errors
    return _lane_value(Registry(reg_path).get(name), checkout), []


def register(registry) -> None:
    from axiom.infra.skill_declarations import Precondition
    from axiom.infra.skills import SkillSpec

    from . import as_, check, db, dbserver, down, fault, shot, status, up

    specs = [
        (
            "dev.up",
            up.run,
            "Run this checkout's node and print the one URL to open.",
            {
                "root": "str",
                "email": "str",
                "sign_in": "str — a vault credential name, or 'none'",
                "sign_in_client": "list[str] — [issuer_url, client_id] of a public client",
                "with": "list[str]",
                "db_url": "str — the database URL (default: $AXIOM_DB_URL)",
                "restart": "bool",
                "timeout": "float",
                "kind": "str — local|site|edge (ADR-255)",
                "topology": "str — a topology name (dev.toml [[topology]] or a generic one)",
                "node": "str — run one node of the topology",
                "home": "str — an edge's home: a node of the running topology",
                "home_url": "str — an edge's home on another machine",
                "home_key": "str — the vault entry holding the key that home issued",
                "site": "str — the site an edge collects for",
                **CALLER_GOAL,
            },
            True,
            True,
        ),
        (
            "dev.down",
            down.run,
            "Stop this checkout's node and its web dev server.",
            {"root": "str", **CALLER_GOAL},
            True,
            True,
        ),
        (
            "dev.status",
            status.run,
            "Whether this checkout's node is up, and where.",
            {"root": "str", **CALLER_GOAL},
            False,
            True,
        ),
    ]
    base = {
        "root": "str — a checkout (default: the current directory)",
        "db_url": "str — the database SERVER (default: $AXIOM_DB_URL); the lane's own database name replaces its path",
        **CALLER_GOAL,
    }
    confirmed = Precondition(
        "confirmed",
        "the caller passed --yes: this drops and recreates the lane's own database",
        check=lambda p, c: bool(p.get("yes")),
        fix="pass --yes",
    )
    for name, fn, description, inputs, side_effects, idempotent in specs:
        registry.register_skill(
            SkillSpec(
                name=name,
                fn=fn,
                description=description,
                inputs=inputs,
                idempotent=idempotent,
                side_effects=side_effects,
                surfaces=("cli",),
            )
        )
    more = [
        (
            "dev.db_status",
            db.status,
            "This lane's database: heads per extension against this checkout, and any drift.",
            {**base},
            "read",
            (),
            False,
        ),
        (
            "dev.db_migrate",
            db.migrate,
            "Create this lane's database if absent and migrate it to this checkout's heads.",
            {**base},
            "reversible-write",
            (),
            True,
        ),
        (
            "dev.db_reset",
            db.reset,
            "Drop and recreate only this lane's own database, migrated for this checkout.",
            {**base, "yes": "bool", "database": "str"},
            "reversible-write",
            (confirmed,),
            False,
        ),
        (
            "dev.db_seed",
            db.seed,
            "Load the checkout's [dev].seed sample data into this lane's database.",
            {**base},
            "reversible-write",
            (),
            True,
        ),
        (
            "dev.check",
            check.run,
            "Prove the hot-reload loop: change one token, assert the dev server serves it within 3 s.",
            {**base, "timeout": "float", "topology": "str", "count": "int — readings per edge"},
            "reversible-write",
            (),
            True,
        ),
        (
            "dev.db_server_up",
            dbserver.up,
            "Start the one shared local Postgres (pgvector) every lane's database is created on.",
            {**CALLER_GOAL},
            "reversible-write",
            (),
            True,
        ),
        (
            "dev.db_server_down",
            dbserver.down,
            "Stop the shared dev Postgres (data is kept).",
            {**CALLER_GOAL},
            "reversible-write",
            (),
            True,
        ),
        (
            "dev.db_server_status",
            dbserver.status,
            "Whether the shared dev Postgres is running, and on which port.",
            {**CALLER_GOAL},
            "read",
            (),
            False,
        ),
        (
            "dev.fault",
            fault.run,
            "Inject a fault at an edge of the running topology (source-offline, home-down, relay-down, slow-link, clock-skew) or clear it.",
            {
                **CALLER_GOAL,
                "node": "str — an edge node",
                "fault": "str",
                "value": "float — seconds (slow-link, clock-skew)",
            },
            "reversible-write",
            (),
            True,
        ),
        (
            "dev.as",
            as_.run,
            "Sign in as a tenant's persona on that tenant's own host.",
            {**base, "target": "str — tenant[:persona]", "open": "bool"},
            "read",
            (),
            False,
        ),
        (
            "dev.shot",
            shot.run,
            "Run the checkout's screenshot command into ./.dev/shots/.",
            {**base},
            "reversible-write",
            (),
            True,
        ),
    ]
    for name, fn, description, inputs, risk, requires, idem in more:
        registry.register_skill(
            SkillSpec(
                name=name,
                fn=fn,
                description=description,
                inputs=inputs,
                idempotent=idem,
                side_effects=risk != "read",
                surfaces=("cli",),
                risk=risk,
                requires=requires,
            )
        )


__all__ = ["CALLER_GOAL", "open_vault", "register", "resolve_lane"]
