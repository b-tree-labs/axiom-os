# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Site-manifest authority rule loader (ADR-114 §2) — the *whether* / effect gate.

ADR-114 §1 (the identity gate) answers *who* may call a tool. This answers
*whether* a call proceeds, holds for approval, or is refused — by loading a site
operator's declarative rules into the tool ``DecideContext``, layered on top of
the built-in open posture (:func:`axiom.infra.authority.open_tool_rule`).

The file is node-durable TOML, mirroring ``~/.axi/plinth/backup_policy.toml``.
Each ``[[rule]]`` maps a tool (or ``"*"``) to a disposition::

    # ~/.axi/authority/site_rules.toml
    [[rule]]
    name = "hold-egress-writes"        # optional; surfaces in the receipt
    tool = "press.publish"             # tool://press.publish   ("*" = all tools)
    disposition = "propose"            # permit | deny | propose | require_capability
    priority = 100                     # optional (default 0)
    actor = "*"                        # optional; @name:context or "*" (default "*")
    surface = "mcp"                    # optional (ADR-114 §3); cli|mcp|agent_tool|*
                                       #   omit = any surface

``deny > propose > require_capability > permit``, so a site rule outranks the
open permit by *disposition* regardless of priority. Nothing is removed: a tool
with no site rule still permits (this is not a default-deny).

Fail-closed on presence: a policy file that exists but cannot be fully parsed is
a loud error — the operator intended a policy — and is treated as *present* so
the hook's exception path denies rather than passing through. A partial /
half-parsed policy is never applied (any bad entry aborts the whole load).
"""
from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

from axiom.extensions.builtins.authz.rules import Rule
from axiom.governance.intent import IntentPattern
from axiom.governance.resource import ResourcePattern
from axiom.infra.authority import TOOL_INVOKE_INTENT, TOOL_RESOURCE_SCHEME

log = logging.getLogger("axiom.infra.authority_rules")

#: Override the site-policy path (tests, non-default deployments).
SITE_RULES_ENV = "AXIOM_AUTHORITY_SITE_RULES"

_VALID_DISPOSITIONS = frozenset({"permit", "deny", "propose", "require_capability"})
#: Surface names a rule may scope to (ADR-114 §3). ``"*"`` is also accepted and
#: means "any surface" (equivalent to omitting the key).
_VALID_SURFACES = frozenset({"cli", "mcp", "agent_tool", "*"})


class SiteRuleError(ValueError):
    """A site policy file exists but could not be parsed into a complete rule set."""


@dataclass(frozen=True)
class SitePolicy:
    """The outcome of a load: the rules, and whether a policy file was present.

    ``present`` is the fail-closed signal — true whenever a policy file exists,
    even if it was malformed (in which case ``rules`` is empty).
    """

    rules: tuple[Rule, ...]
    present: bool
    path: Path


def default_site_rules_path() -> Path:
    """``~/.axi/authority/site_rules.toml`` — the node-durable policy home."""
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir() / "authority" / "site_rules.toml"


def _resolve_path(path: Path | None) -> Path:
    if path is not None:
        return path
    override = os.environ.get(SITE_RULES_ENV, "").strip()
    return Path(override) if override else default_site_rules_path()


def _rule_from_entry(entry: object, index: int) -> Rule:
    if not isinstance(entry, dict):
        raise SiteRuleError(f"rule[{index}] must be a table, got {type(entry).__name__}")
    tool = str(entry.get("tool", "")).strip()
    if not tool:
        raise SiteRuleError(f"rule[{index}] missing required 'tool'")
    disposition = str(entry.get("disposition", "")).strip()
    if disposition not in _VALID_DISPOSITIONS:
        raise SiteRuleError(
            f"rule[{index}] ({tool!r}) has invalid disposition {disposition!r}; "
            f"expected one of {sorted(_VALID_DISPOSITIONS)}"
        )
    priority = entry.get("priority", 0)
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise SiteRuleError(f"rule[{index}] ({tool!r}) priority must be an integer")
    actor = str(entry.get("actor", "*")).strip() or "*"
    name = str(entry.get("name", "")).strip() or f"site:{disposition}:{tool}"
    surface_raw = entry.get("surface", None)
    surface_pattern: str | None
    if surface_raw is None:
        surface_pattern = None
    else:
        surface_pattern = str(surface_raw).strip() or None
        if surface_pattern is not None and surface_pattern not in _VALID_SURFACES:
            raise SiteRuleError(
                f"rule[{index}] ({tool!r}) has invalid surface {surface_raw!r}; "
                f"expected one of {sorted(_VALID_SURFACES)}"
            )
    return Rule(
        name=name,
        intent_pattern=IntentPattern(TOOL_INVOKE_INTENT),
        actor_pattern=actor,
        resource_pattern=ResourcePattern(f"{TOOL_RESOURCE_SCHEME}://{tool}"),
        surface_pattern=surface_pattern,
        disposition=disposition,  # type: ignore[arg-type]
        priority=priority,
    )


def load_site_policy(path: Path | None = None) -> SitePolicy:
    """Load the site policy from ``path`` (or the resolved default).

    Absent file → empty, ``present=False`` (the fail-open default is unchanged).
    Present + valid → the parsed rules, ``present=True``.
    Present + malformed → :class:`SiteRuleError` (the caller decides how loud);
    a partial policy is never returned.
    """
    p = _resolve_path(path)
    if not p.exists():
        return SitePolicy(rules=(), present=False, path=p)
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise SiteRuleError(f"cannot read site policy {p}: {type(exc).__name__}: {exc}") from exc
    entries = raw.get("rule", [])
    if not isinstance(entries, list):
        raise SiteRuleError(f"site policy {p}: [[rule]] must be an array of tables")
    rules = tuple(_rule_from_entry(e, i) for i, e in enumerate(entries))
    return SitePolicy(rules=rules, present=True, path=p)


__all__ = [
    "SITE_RULES_ENV",
    "SitePolicy",
    "SiteRuleError",
    "default_site_rules_path",
    "load_site_policy",
]
