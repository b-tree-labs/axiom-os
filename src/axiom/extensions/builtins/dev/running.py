# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What a dev node is running, said in one block (#1716, first slice).

A contributor ran ``dev up`` from a source clone with a site named in his
environment and could not tell what he was looking at. This block answers it,
the same on a fresh start, on "already running", in ``dev status`` and as JSON::

    Running: <product> from <checkout> (<version>)
      Platform: Axiom <version> from <clone path | installed package>
      Site look: none (sites are not applied locally yet, #1716)
      Data: local | bound to <site> at <host>
      Library: this lane's (<bronze>)[, not relayed to <site>] | <site>'s, relayed to <host>
      Database: <host:port/dbname>
      Sign in: SSO via <host>, or password as <email> | password as <email>

The product is whatever the branding names; nothing here names one.
"""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

#: The platform's own name and its distribution.
PLATFORM_NAME = "Axiom"
PLATFORM_DIST = "axiom-os-lm"
#: The variable that names the site an install is for.
SITE_VAR = "AXIOM_SITE"
#: Where applying a site locally is tracked.
ISSUE = "#1716"


def _pyproject_version(root: Path) -> str:
    try:
        data = tomllib.loads((Path(root) / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get("project", {}).get("version") or "")


def _dist_version(name: str) -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version(name)
    except PackageNotFoundError:
        return ""


# -- Running: the product ------------------------------------------------------


def product(checkout: Path) -> tuple[str, str]:
    """The product's own name (from its branding) and the checkout's version."""
    from axiom.infra.branding import discover_portfolio_members, get_branding

    brand = get_branding()
    name = (brand.product_name or PLATFORM_NAME).strip()
    found = _pyproject_version(checkout)
    if not found:
        cli = (brand.cli_name or "").strip()
        for member in discover_portfolio_members():
            if cli and member.cli_name == cli:
                found = _dist_version(member.package_name)
                break
    return name, found or "version unknown"


# -- Platform --------------------------------------------------------------------


def _clone_of(source_root: Path) -> Path:
    """The clone a source root belongs to: its parent when the root is ``src``."""
    return source_root.parent if source_root.name == "src" else source_root


def _editable_path(dist: Any) -> Path | None:
    try:
        raw = dist.read_text("direct_url.json") if dist is not None else None
        info = json.loads(raw) if raw else {}
    except (OSError, ValueError):
        return None
    if not (info.get("dir_info") or {}).get("editable"):
        return None
    parts = urlsplit(str(info.get("url") or ""))
    return Path(unquote(parts.path)) if parts.scheme == "file" and parts.path else None


def _platform_dist() -> Any:
    from importlib.metadata import PackageNotFoundError, distribution

    try:
        return distribution(PLATFORM_DIST)
    except PackageNotFoundError:
        return None


_UNSET: Any = object()


def platform(*, roots: list[Path], dist: Any = _UNSET) -> dict[str, Any]:
    """Which platform the node imports, and from where.

    The node puts its source roots ahead of the installed package, so a
    platform found on one of them is the one that runs. Otherwise an editable
    install names its clone (``direct_url.json``), and anything else is the
    installed package.
    """
    if dist is _UNSET:
        dist = _platform_dist()
    installed = str(getattr(dist, "version", "") or "") if dist is not None else ""
    for root in roots:
        if (Path(root) / "axiom" / "__init__.py").is_file():
            clone = _clone_of(Path(root))
            return {
                "name": PLATFORM_NAME,
                "version": _pyproject_version(clone) or installed or "version unknown",
                "source": "source",
                "path": str(clone),
            }
    clone = _editable_path(dist)
    if clone is not None:
        return {
            "name": PLATFORM_NAME,
            "version": _pyproject_version(clone) or installed or "version unknown",
            "source": "editable",
            "path": str(clone),
        }
    return {
        "name": PLATFORM_NAME,
        "version": installed or "version unknown",
        "source": "installed",
        "path": None,
    }


def platform_line(p: Mapping[str, Any]) -> str:
    where = p.get("path") or "installed package"
    return f"Platform: {p.get('name') or PLATFORM_NAME} {p.get('version') or ''} from {where}"


# -- Site look ---------------------------------------------------------------------


def site_look(*, env: Mapping[str, str], mode: Any) -> dict[str, Any]:
    """The site named for this node (variable, else binding); none is applied yet."""
    requested = (env.get(SITE_VAR) or "").strip()
    if not requested and getattr(mode, "bound", False):
        requested = str(getattr(mode, "site", "") or "").strip()
    return {"applied": False, "requested": requested or None, "issue": ISSUE}


def site_look_line(look: Mapping[str, Any]) -> str:
    issue = look.get("issue") or ISSUE
    if look.get("requested"):
        # A set variable must never silently look like it worked.
        return f"Site look: none: {look['requested']} is not applied locally yet ({issue})"
    return f"Site look: none (sites are not applied locally yet, {issue})"


# -- Data --------------------------------------------------------------------------


def data(mode: Any) -> dict[str, Any]:
    """Where the node's data comes from: the binding's site and face host, never more."""
    if not getattr(mode, "bound", False):
        return {"bound": False, "site": None, "host": None}
    face = str(getattr(mode, "face_url", "") or "")
    host = urlsplit(face).hostname or None
    return {"bound": True, "site": str(getattr(mode, "site", "") or "") or None, "host": host}


def data_line(d: Mapping[str, Any]) -> str:
    if not d.get("bound"):
        return "Data: local"
    where = f" at {d['host']}" if d.get("host") else ""
    return f"Data: bound to {d.get('site') or 'a site'}{where}"


# -- Library -----------------------------------------------------------------------


def library(env: Mapping[str, str], mode: Any) -> dict[str, Any]:
    """Where the Library files what is dropped into it (#1959): the lane's own unless the person said otherwise."""
    local = (env.get("AXIOM_LIBRARY_LOCAL") or "").strip().lower() in ("1", "true", "yes", "on")
    bound = bool(getattr(mode, "bound", False))
    return {
        "local": local or not bound,
        "root": env.get("AXIOM_LIBRARY_BRONZE_ROOT") or None,
        "site": str(getattr(mode, "site", "") or "") or None if bound else None,
        "host": (urlsplit(str(getattr(mode, "face_url", "") or "")).hostname or None) if bound else None,
    }


def library_line(lib: Mapping[str, Any]) -> str:
    if lib.get("local"):
        where = f" ({lib['root']})" if lib.get("root") else ""
        kept = f", not relayed to {lib['site']}" if lib.get("site") else ""
        return f"Library: this lane's{where}{kept}"
    host = f", relayed to {lib['host']}" if lib.get("host") else ""
    return f"Library: {lib.get('site') or 'the site'}'s{host}"


# -- Sign in -----------------------------------------------------------------------


def sign_in(
    *, email: str, sso_host: str | None, source: str | None = None, sso_reachable: bool = True
) -> dict[str, Any]:
    if sso_host:
        said = {"method": "sso", "host": sso_host, "email": email or None, "source": source or None}
        if not sso_reachable:
            # The line says what is enforced, not what was asked for (#1842).
            said["reachable"] = False
        return said
    return {"method": "password", "host": None, "email": email or None, "source": None}


def sign_in_line(s: Mapping[str, Any]) -> str:
    email = s.get("email") or "your git user.email (set it, or pass --email)"
    if s.get("method") == "sso" and s.get("host"):
        where = " (from the bound site)" if s.get("source") == "bound site" else ""
        if s.get("reachable") is False:
            return (
                f"Sign in: {s['host']}{where} cannot be reached, so SSO is refused "
                f"until it answers; a password works as {email}"
            )
        return f"Sign in: SSO via {s['host']}{where}, or with a password as {email}"
    return f"Sign in: password as {email}"


# -- the block -----------------------------------------------------------------------


def describe(
    *,
    product_name: str,
    product_version: str,
    checkout: Path,
    platform: Mapping[str, Any],
    env: Mapping[str, str],
    mode: Any,
    database: str,
    email: str,
    sso_host: str | None,
    sso_source: str | None = None,
    sso_reachable: bool = True,
) -> dict[str, Any]:
    """The block as plain JSON, so the web shell can show it later."""
    return {
        "product": {"name": product_name, "version": product_version, "checkout": str(checkout)},
        "platform": dict(platform),
        "site_look": site_look(env=env, mode=mode),
        "data": data(mode),
        "library": library(env, mode),
        "database": database or None,
        "sign_in": sign_in(
            email=email, sso_host=sso_host, source=sso_source, sso_reachable=sso_reachable
        ),
    }


def collect(
    *,
    checkout: Path,
    roots: list[Path],
    database: str,
    email: str,
    sso_host: str | None,
    sso_source: str | None = None,
    sso_reachable: bool = True,
    env: Mapping[str, str] | None = None,
    mode: Any = None,
) -> dict[str, Any]:
    """The block for a node about to start from ``checkout``. Never raises."""
    if mode is None:
        from axiom.infra import serving_mode

        mode = serving_mode.resolve_mode()
    try:
        name, version = product(checkout)
    except Exception:  # noqa: BLE001 - a branding problem must not stop a node
        name, version = PLATFORM_NAME, "version unknown"
    return describe(
        product_name=name,
        product_version=version,
        checkout=checkout,
        platform=platform(roots=roots),
        env=os.environ if env is None else env,
        mode=mode,
        database=database,
        email=email,
        sso_host=sso_host,
        sso_source=sso_source,
        sso_reachable=sso_reachable,
    )


def render(r: Mapping[str, Any]) -> list[str]:
    p = r.get("product") or {}
    lines = [
        f"Running: {p.get('name') or PLATFORM_NAME} from {p.get('checkout') or '?'}"
        f" ({p.get('version') or 'version unknown'})",
        platform_line(r.get("platform") or {}),
        site_look_line(r.get("site_look") or {}),
        data_line(r.get("data") or {}),
    ]
    if r.get("library"):
        lines.append(library_line(r["library"]))
    if r.get("database"):
        lines.append(f"Database: {r['database']}")
    lines.append(sign_in_line(r.get("sign_in") or {}))
    return [lines[0], *(f"  {line}" for line in lines[1:])]


def of_record(
    record: Mapping[str, Any], home: Path, root: Path, *, fallback_email: str = ""
) -> dict[str, Any]:
    """What a running node is, from its record; rebuilt for a record older than the block."""
    if isinstance(record.get("running"), dict):
        return dict(record["running"])
    from . import node

    checkout = Path(record.get("root") or root)
    return collect(
        checkout=checkout,
        roots=node.source_roots(checkout),
        database=str(record.get("database") or ""),
        email=str(record.get("email") or "") or node.owner_email(home) or fallback_email,
        sso_host=None,
    )


# -- a running node asked for something else ---------------------------------------


def mismatches(r: Mapping[str, Any], *, email: str = "", site: str = "") -> list[str]:
    """What a second ``dev up`` asked for that the running node is not.

    The running node keeps whoever started it; echoing the new request back
    told a contributor he was signed in as somebody he was not (#1791).
    """
    said: list[str] = []
    running_email = str((r.get("sign_in") or {}).get("email") or "")
    if email and running_email and email != running_email:
        said.append(f"Running as {running_email}; add --restart to switch to {email}")
    running_site = str((r.get("site_look") or {}).get("requested") or "")
    if site and site != running_site:
        now = running_site or "no site"
        said.append(f"Running with {now}; add --restart to switch to {site}")
    return said


__all__ = [
    "ISSUE",
    "PLATFORM_NAME",
    "SITE_VAR",
    "collect",
    "data",
    "data_line",
    "describe",
    "mismatches",
    "of_record",
    "platform",
    "platform_line",
    "product",
    "render",
    "sign_in",
    "sign_in_line",
    "site_look",
    "site_look_line",
]
