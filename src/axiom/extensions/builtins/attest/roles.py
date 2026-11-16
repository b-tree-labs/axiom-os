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

A role can also be held for one log, with an expiry (#1804, ADR-262)::

    [[log_role]]
    principal = "@p:site-a"
    site = "site-a"
    logbook = "ops_log"
    role = "importer"
    until = "2026-12-31"

``importer`` is the only such role. It is granted only for one log and only with an expiry,
is never a signing role, and is never implied by an administration role, a signing role or a
gate pair: :func:`holds_log_role` reads these rows and nothing else.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from datetime import UTC, date, datetime, time
from pathlib import Path

from .logbooks import PLATFORM_ADMIN_ROLES

_ROLE = re.compile(r"^[a-z0-9_]+$")

#: Roles held for one log, never for a site, and only with an expiry (#1804).
LOG_ROLES = frozenset({"importer"})


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


def _load_log_roles(state_dir: Path) -> list[dict]:
    path = roles_file(state_dir)
    if not path.exists():
        return []
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    return [
        {"principal": a["principal"], "site": a["site"], "logbook": a["logbook"],
         "role": a["role"], "until": str(a["until"])}
        for a in data.get("log_role") or []
    ]  # fmt: skip


def _q(value: str) -> str:
    return json.dumps(value)  # a TOML basic string is a JSON string for these values


def _save(state_dir: Path, rows: list[dict], log_rows: list[dict] | None = None) -> None:
    if log_rows is None:
        log_rows = _load_log_roles(state_dir)
    path = roles_file(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Signing roles per site (attest). Written by `axi log role`; every",
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
    for r in log_rows:
        lines += [
            "[[log_role]]",
            *(f"{k} = {_q(r[k])}" for k in ("principal", "site", "logbook", "role", "until")),
            "",
        ]
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".roles.", suffix=".toml")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    os.replace(tmp, path)


def _log(state_dir: Path, action: str, principal: str, site: str, role: str, by: str,
         **scope: str) -> None:  # fmt: skip
    entry = {
        "at": datetime.now(UTC).isoformat(),
        "action": action,
        "principal": principal,
        "site": site,
        "role": role,
        "by": by,
        **scope,
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


def parse_until(value: str | None) -> datetime:
    """The instant a log role stops counting: the end of a date (UTC), or an ISO 8601 time."""
    text_ = str(value or "").strip()
    try:
        if len(text_) == 10:
            return datetime.combine(date.fromisoformat(text_), time.max, tzinfo=UTC)
        when = datetime.fromisoformat(text_.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"--until {value!r} is not a date (YYYY-MM-DD) or an ISO 8601 time") from None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def _grant_log_role(principal: str, site: str, role: str, *, logbook: str | None,
                    until: str | None, by: str, state_dir: Path) -> None:  # fmt: skip
    if not logbook:
        raise ValueError(
            f"{role!r} is held for one log, never for a whole site: name it with --log LOG"
        )
    if not until:
        raise ValueError(
            f"{role!r} needs an expiry: give --until DATE. Importing writes history onto a log's "
            "chain that nobody signed, so the right to do it is granted for a stated time and "
            "ends by itself (#1804)"
        )
    ends = parse_until(until)
    if ends <= datetime.now(UTC):
        raise ValueError(f"--until {until} is in the past; an expiry must be later than now")
    rows = [r for r in _load_log_roles(state_dir)
            if (r["principal"], r["site"], r["logbook"], r["role"]) != (principal, site, logbook, role)]  # fmt: skip
    rows.append({"principal": principal, "site": site, "logbook": logbook, "role": role,
                 "until": str(until)})  # fmt: skip
    _save(state_dir, _load(state_dir), rows)
    _log(state_dir, "grant", principal, site, role, by, logbook=logbook, until=str(until))


def holds_log_role(principal: str, site: str, logbook: str, role: str, *, state_dir: Path,
                   now: datetime | None = None) -> tuple[bool, str]:  # fmt: skip
    """``(True, "")`` when ``principal`` holds ``role`` for ``logbook`` at ``site`` now, else
    ``(False, why)``. Only an explicit, unexpired log-role row counts (ADR-262)."""
    now = now or datetime.now(UTC)
    for r in _load_log_roles(state_dir):
        if (r["principal"], r["site"], r["logbook"], r["role"]) == (principal, site, logbook, role):
            if parse_until(r["until"]) <= now:
                return False, f"{principal}'s {role} role for {logbook} expired on {r['until']}"
            return True, ""
    return False, f"{principal} holds no {role} role for {logbook} at {site}"


def log_assignments(site: str, *, state_dir: Path) -> list[dict]:
    return sorted((r for r in _load_log_roles(state_dir) if r["site"] == site),
                  key=lambda r: (r["logbook"], r["principal"]))  # fmt: skip


def grant(principal: str, site: str, role: str, *, by: str, state_dir: Path,
          logbook: str | None = None, until: str | None = None) -> None:  # fmt: skip
    _check(principal, role)
    if role in LOG_ROLES:
        _grant_log_role(principal, site, role, logbook=logbook, until=until, by=by,
                        state_dir=state_dir)  # fmt: skip
        return
    if logbook or until:
        raise ValueError(
            f"--log and --until are for a log role ({sorted(LOG_ROLES)}); {role!r} is a signing "
            "role, held at a site"
        )
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


def revoke(principal: str, site: str, role: str, *, by: str, state_dir: Path,
           logbook: str | None = None) -> None:  # fmt: skip
    if role in LOG_ROLES:
        rows = _load_log_roles(state_dir)
        keep = [r for r in rows if not (r["principal"] == principal and r["site"] == site
                and r["role"] == role and (logbook is None or r["logbook"] == logbook))]  # fmt: skip
        if len(keep) == len(rows):
            raise ValueError(f"{principal} does not hold {role!r} for {logbook or 'any log'} at {site}")
        _save(state_dir, _load(state_dir), keep)
        _log(state_dir, "revoke", principal, site, role, by, logbook=logbook or "*")
        return
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


#: The site setting that pairs a gate role with a signing role (#1800):
#: ``{"attest": {"gate_roles": {"<gate role>": "<signing role>"}}}``.
GATE_PAIRS_KEY = "attest.gate_roles"


def gate_pairs(site_id: str) -> dict[str, str]:
    """The site's explicit pairs, gate role to signing role (#1800, decided by the owner). Only
    what the site declares: nothing is inferred from a role's name, and a pair that names an
    administration role on either side is ignored, because administration holds no authority
    in a log (ADR-142 rule 8)."""
    from . import site_settings
    from .logbooks import PLATFORM_ADMIN_ROLES

    raw = site_settings.value(site_id, GATE_PAIRS_KEY, None)
    if not isinstance(raw, dict):
        return {}
    return {
        str(g): str(s) for g, s in raw.items()
        if isinstance(s, str) and _ROLE.match(str(s)) and str(g) not in PLATFORM_ADMIN_ROLES
        and str(s) not in PLATFORM_ADMIN_ROLES and str(s) not in LOG_ROLES
    }  # fmt: skip


def unpaired(site_id: str, gate_roles: tuple[str, ...] | list[str]) -> list[str]:
    """The gate roles a person holds that the site pairs with no signing role."""
    pairs = gate_pairs(site_id)
    return sorted(g for g in gate_roles if g not in pairs)


def roles_for(
    principal: str, site_id: str, *, state_dir: Path, gate_roles: tuple[str, ...] = ()
) -> tuple[str, ...]:
    """The signing roles ``principal`` holds at ``site_id``: those granted in the log's role
    table, and those the site pairs with a gate role the person's sign-in carries (#1800)."""
    granted: tuple[str, ...] = ()
    for r in _load(state_dir):
        if r["principal"] == principal and r["site"] == site_id:
            granted = tuple(r["roles"])
            break
    pairs = gate_pairs(site_id) if gate_roles else {}
    paired = tuple(pairs[g] for g in gate_roles if g in pairs)
    return tuple(dict.fromkeys(granted + paired))
