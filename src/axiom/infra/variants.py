# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The web app's variants: one codebase, three compositions side by side.

A variant is a composition of the same release: bare Axiom (appkit's default
app on sample data), the bare product (the product's app configuration) and the
site (the product plus the site's overlay). Each runs on its own port and path,
with its own session realm, flag set and app-state tenants, over one shared data
platform (roadmap §4; the build-out spec's VAR rows).

A variant is a ``[variant.<name>]`` record. The three below are the defaults; a
deployment overrides any field in ``variants.toml`` in its config directory
(``$AXIOM_VARIANTS_CONFIG`` names another file)::

    [variant.product]
    path = "/v/example-product/"
    realm = "example-product-bare"
    tenants = ["variant-example-product"]

    [variant.site.flags]
    library = "on"

The running variant is named by ``AXIOM_VARIANT``, which ``axi app run`` sets
for each child it supervises along with the session realm and cookie the
session layer reads (``axiom.webauth.session``). Nothing here starts anything.

Two kinds of variant, and the difference is load-bearing:

* **isolated** (the bare ones): every caller is a guest of the variant's
  tenant. Uploads land in that tenant's corpus, chat and search read only it
  plus the community corpus, and stored flag overrides are the variant's own.
* **in place** (the site): it takes over the install that is already running.
  It keeps the node's home sites, accepts the sessions people already hold (no
  forced re-auth) and reads the flag overrides already stored.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

VARIANT_ENV = "AXIOM_VARIANT"
CONFIG_ENV = "AXIOM_VARIANTS_CONFIG"
CONFIG_FILENAME = "variants.toml"

#: The session layer's knobs (``axiom.webauth.session``), set per child.
SESSION_REALM_ENV = "AXIOM_SESSION_REALM"
SESSION_COOKIE_ENV = "AXIOM_SESSION_COOKIE"
SESSION_LEGACY_ENV = "AXIOM_SESSION_ACCEPT_LEGACY"
#: Where the front door mounts the variant; the app reads it to build links.
BASE_PATH_ENV = "AXIOM_APP_BASE_PATH"

NAMES: tuple[str, ...] = ("axiom", "product", "site")

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
_TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class VariantError(ValueError):
    """A variant record that cannot be run as written."""


@dataclass(frozen=True)
class Variant:
    name: str
    title: str
    port: int
    path: str
    realm: str
    cookie: str
    #: The app-state tenants. For an isolated variant the first is the tenant
    #: every caller belongs to; an in-place variant keeps the install's sites.
    tenants: tuple[str, ...] = ()
    isolated: bool = True
    in_place: bool = False
    #: The variant's flag layer: between the environment and the node override.
    flags: Mapping[str, str] = field(default_factory=dict)
    #: The http serve profile, when the composition needs one.
    profile: str = ""
    #: What renders: a description for the plan, not a switch.
    composition: str = ""

    @property
    def tenant(self) -> str | None:
        """The one tenant every caller of an isolated variant belongs to."""
        return self.tenants[0] if self.isolated and self.tenants else None

    @property
    def override_namespace(self) -> str | None:
        """Prefix for stored flag overrides; ``None`` reads the install's own."""
        return None if self.in_place else self.name

    def child_env(self) -> dict[str, str]:
        """The environment a supervised copy of this variant runs with."""
        env = {
            VARIANT_ENV: self.name,
            SESSION_REALM_ENV: self.realm,
            SESSION_COOKIE_ENV: self.cookie,
            BASE_PATH_ENV: self.path,
        }
        if self.in_place:
            env[SESSION_LEGACY_ENV] = "1"
        # The tenant is not exported as ``AXIOM_SITE``: the variants share one
        # data platform, and the site a gold read is bounded by is the install's.
        # The tenant scopes app state only, read through ``variant_tenant()``.
        return env

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "port": self.port,
            "path": self.path,
            "realm": self.realm,
            "cookie": self.cookie,
            "tenants": list(self.tenants),
            "isolated": self.isolated,
            "in_place": self.in_place,
            "flags": dict(self.flags),
            "profile": self.profile,
            "composition": self.composition,
        }


#: Ports and paths decided by the owner 2026-10-30 (roadmap OD-10); the site
#: names its product's path, realm and tenant in its own ``variants.toml``.
DEFAULTS: dict[str, Variant] = {
    "axiom": Variant(
        name="axiom",
        title="Bare Axiom",
        port=8960,
        path="/v/axiom/",
        realm="axiom-bare",
        cookie="axiom_s_axiom",
        tenants=("variant-axiom",),
        composition="appkit DefaultApp on sample data",
    ),
    "product": Variant(
        name="product",
        title="Bare product",
        port=8961,
        path="/v/product/",
        realm="product-bare",
        cookie="axiom_s_product",
        tenants=("variant-product",),
        composition="the product's app configuration on its samples",
    ),
    "site": Variant(
        name="site",
        title="Site",
        port=8962,
        path="/",
        realm="site",
        cookie="axiom_s_site",
        isolated=False,
        in_place=True,
        composition="the product plus the site's overlay (brand, tenants, flags)",
    ),
}


def _config_path() -> Path:
    named = (os.environ.get(CONFIG_ENV) or "").strip()
    if named:
        return Path(named)
    from axiom.infra.config import default_config_dir

    return default_config_dir() / CONFIG_FILENAME


def _merge(base: Variant, raw: Mapping[str, Any]) -> Variant:
    known = {f for f in Variant.__dataclass_fields__ if f != "name"}
    unknown = sorted(set(raw) - known)
    if unknown:
        raise VariantError(f"[variant.{base.name}] has unknown field(s): {', '.join(unknown)}")
    changes: dict[str, Any] = dict(raw)
    if "tenants" in changes:
        changes["tenants"] = tuple(str(t) for t in changes["tenants"] or ())
    if "flags" in changes:
        changes["flags"] = {str(k): str(v) for k, v in dict(changes["flags"] or {}).items()}
    if "port" in changes:
        changes["port"] = int(changes["port"])
    return replace(base, **changes)


def validate(v: Variant) -> Variant:
    if not _NAME_RE.match(v.name):
        raise VariantError(f"{v.name!r} is not a usable variant name")
    if not 1 <= v.port <= 65535:
        raise VariantError(f"variant {v.name}: port {v.port} is out of range")
    if not v.path.startswith("/") or not v.path.endswith("/"):
        raise VariantError(f"variant {v.name}: path {v.path!r} must start and end with '/'")
    if not v.realm.strip():
        raise VariantError(f"variant {v.name}: a realm is required")
    if not re.match(r"^[A-Za-z0-9_]+$", v.cookie):
        raise VariantError(f"variant {v.name}: cookie {v.cookie!r} is not a cookie name")
    for t in v.tenants:
        if not _TENANT_RE.match(t):
            raise VariantError(f"variant {v.name}: tenant {t!r} is not a usable site id")
    if v.isolated and not v.tenants:
        raise VariantError(f"variant {v.name} is isolated and names no tenant")
    if v.isolated and v.in_place:
        raise VariantError(f"variant {v.name} cannot be both isolated and in place")
    return v


def load_variants(config: Mapping[str, Any] | Path | str | None = None) -> dict[str, Variant]:
    """The defaults with the deployment's ``[variant.<name>]`` records applied.

    Two variants that share a port, a path, a realm, a cookie or a tenant are
    refused: each of those is what keeps one variant out of another.
    """
    if config is None or isinstance(config, (str, Path)):
        from axiom.infra.toml_compat import load_toml

        data = load_toml(Path(config) if config is not None else _config_path())
    else:
        data = dict(config)
    records = data.get("variant") or {}
    out = dict(DEFAULTS)
    for name, raw in records.items():
        if not isinstance(raw, Mapping):
            raise VariantError(f"[variant.{name}] is not a table")
        base = out.get(name) or Variant(name=name, title=name, port=0, path="", realm="", cookie="")
        out[name] = _merge(base, raw)
    for v in out.values():
        validate(v)
    for attr in ("port", "path", "realm", "cookie"):
        seen: dict[Any, str] = {}
        for v in out.values():
            key = getattr(v, attr)
            if key in seen:
                raise VariantError(f"variants {seen[key]} and {v.name} share {attr} {key!r}")
            seen[key] = v.name
    owners: dict[str, str] = {}
    for v in out.values():
        for t in v.tenants:
            if t in owners:
                raise VariantError(f"variants {owners[t]} and {v.name} share tenant {t!r}")
            owners[t] = v.name
    return out


def get_variant(name: str, variants: Mapping[str, Variant] | None = None) -> Variant:
    table = variants if variants is not None else load_variants()
    try:
        return table[name]
    except KeyError:
        raise VariantError(f"no variant {name!r}; choose from {', '.join(sorted(table))}") from None


def active_variant() -> Variant | None:
    """The variant this process serves (``AXIOM_VARIANT``), or ``None`` for a plain install.

    A named variant that cannot be loaded raises: a process told it is a
    variant and quietly serving as a plain install would widen every scope the
    variant exists to narrow.
    """
    name = (os.environ.get(VARIANT_ENV) or "").strip()
    if not name:
        return None
    return get_variant(name)


def variant_tenant() -> str | None:
    """The tenant every caller of the running variant belongs to, if it is isolated."""
    v = active_variant()
    return v.tenant if v else None


__all__ = [
    "BASE_PATH_ENV",
    "CONFIG_ENV",
    "DEFAULTS",
    "NAMES",
    "SESSION_COOKIE_ENV",
    "SESSION_LEGACY_ENV",
    "SESSION_REALM_ENV",
    "VARIANT_ENV",
    "Variant",
    "VariantError",
    "active_variant",
    "get_variant",
    "load_variants",
    "validate",
    "variant_tenant",
]
