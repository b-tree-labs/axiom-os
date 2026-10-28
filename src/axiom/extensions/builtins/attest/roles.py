# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Where a signer's roles come from.

A person never states their own roles; the node looks them up. Until the
authorization seam carries site relations (ADR-146), the lookup is a file an
administrator maintains, ``<state_dir>/attest/roles.toml``::

    [[assignment]]
    principal = "@op1:site-a"
    site = "site-a"
    roles = ["operator"]

An administrator can assign roles but holds no logbook authority themselves
(ADR-142 rule 8): the signing check discards administration roles whatever
this file says. A missing file means nobody holds a role, so signing refuses.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from .logbooks import PLATFORM_ADMIN_ROLES

_ROLE = re.compile(r"^[a-z0-9_]+$")


def roles_file(state_dir: Path) -> Path:
    return state_dir / "attest" / "roles.toml"


def changes_file(state_dir: Path) -> Path:
    """Append-only log of every grant and revoke, with who made it."""
    return state_dir / "attest" / "role_changes.jsonl"


def _load(state_dir: Path) -> list[dict]:
    path = roles_file(state_dir)
    if not path.exists():
        return []
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    return [
        {"principal": a["principal"], "site": a["site"], "roles": list(a.get("roles") or [])}
        for a in data.get("assignment") or []
    ]


def _q(value: str) -> str:
    return json.dumps(value)  # a TOML basic string is a JSON string for these values


def _save(state_dir: Path, rows: list[dict]) -> None:
    path = roles_file(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Signing roles per site (attest). Written by `axi attest role`; every",
        "# change is also in role_changes.jsonl with who made it.",
        "",
    ]
    for r in rows:
        if not r["roles"]:
            continue
        lines += [
            "[[assignment]]",
            f"principal = {_q(r['principal'])}",
            f"site = {_q(r['site'])}",
            "roles = [" + ", ".join(_q(x) for x in r["roles"]) + "]",
            "",
        ]
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".roles.", suffix=".toml")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    os.replace(tmp, path)


def _log(state_dir: Path, action: str, principal: str, site: str, role: str, by: str) -> None:
    entry = {
        "at": datetime.now(UTC).isoformat(),
        "action": action,
        "principal": principal,
        "site": site,
        "role": role,
        "by": by,
    }
    with open(changes_file(state_dir), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _check(principal: str, role: str) -> None:
    from axiom.vega.identity.principal import parse_handle

    try:
        _name, context = parse_handle(principal or "")
    except ValueError as exc:
        raise ValueError(f"{principal!r} is not a principal handle: {exc}") from None
    if not context:
        raise ValueError(f"{principal!r}: a signer's handle names its context, @name:context")
    if not _ROLE.match(role or ""):
        raise ValueError(f"role {role!r} must be an id, [a-z0-9_]+")
    if role in PLATFORM_ADMIN_ROLES:
        raise ValueError(
            f"{role!r} is a platform administration role and confers no logbook authority "
            "(ADR-142 rule 8); signing would discard it"
        )


def grant(principal: str, site: str, role: str, *, by: str, state_dir: Path) -> None:
    _check(principal, role)
    rows = _load(state_dir)
    row = next((r for r in rows if r["principal"] == principal and r["site"] == site), None)
    if row is None:
        row = {"principal": principal, "site": site, "roles": []}
        rows.append(row)
    if role in row["roles"]:
        return
    row["roles"].append(role)
    _save(state_dir, rows)
    _log(state_dir, "grant", principal, site, role, by)


def revoke(principal: str, site: str, role: str, *, by: str, state_dir: Path) -> None:
    rows = _load(state_dir)
    row = next((r for r in rows if r["principal"] == principal and r["site"] == site), None)
    if row is None or role not in row["roles"]:
        raise ValueError(f"{principal} does not hold {role!r} at {site}")
    row["roles"].remove(role)
    _save(state_dir, rows)
    _log(state_dir, "revoke", principal, site, role, by)


def assignments(site: str, *, state_dir: Path) -> list[dict]:
    return sorted(
        (r for r in _load(state_dir) if r["site"] == site and r["roles"]),
        key=lambda r: r["principal"],
    )


def roles_for(principal: str, site_id: str, *, state_dir: Path) -> tuple[str, ...]:
    for r in _load(state_dir):
        if r["principal"] == principal and r["site"] == site_id:
            return tuple(r["roles"])
    return ()
