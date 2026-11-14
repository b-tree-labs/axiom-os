# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Who must approve a cohort, rollout or stage-plan change (LOOP-F23, ADR-257 D5).

A policy says how many people other than the author approve, and which roles they hold
(any role when none is named). Two places declare one, per environment:

* **a flag**, in its manifest: ``approvals = { production = { approvals = 1, roles = [...] } }``;
* **an environment**, for every flag that declares nothing there, in the settings
  ``features.approvals.<env>.approvals`` / ``.roles`` / ``.two_person``.

``two_person = true`` is ``approvals >= 1``: the author never counts. A change held by a
policy is recorded in ``flags.changes`` (``awaiting_approval``); another person approves it
with ``flags.approve`` (offered only at the command line, never to an AI client, and
refused for a platform agent); the author then runs the same call again and it applies,
once. The kill switch is never held (LOOP-F6).

An approver's roles come from the platform's one permission model: the directory's groups
for that principal mapped to roles by the deployment's group-to-role map (ADR-103), the
same facts the authorization layer reads.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from axiom.governance.roles import canonical_roles, display_name

from .flag_registry import ApprovalPolicy, FlagDef

_env_policies: dict[str, Any] | None = None
_role_resolver: Callable[[str], Iterable[str]] | None = None


def set_environment_policies(policies: Mapping[str, Any] | None) -> None:
    """Pin the environment policies (tests), or ``None`` to read the settings."""
    global _env_policies
    _env_policies = None if policies is None else dict(policies)


def set_role_resolver(fn: Callable[[str], Iterable[str]] | None) -> None:
    """Pin how an approver's roles are found (tests), or ``None`` for the directory."""
    global _role_resolver
    _role_resolver = fn


def _setting(key: str) -> Any:
    try:
        from axiom.extensions.builtins.settings.store import SettingsStore

        return SettingsStore().get(key)
    except Exception:  # noqa: BLE001 - no settings store means no environment policy
        return None


def environment_policy(environment: str) -> ApprovalPolicy:
    """The policy every flag in ``environment`` gets unless it declares its own."""
    if _env_policies is not None:
        raw = _env_policies.get(environment)
        return ApprovalPolicy.from_mapping(raw, source=f"environment {environment}") if raw else ApprovalPolicy()
    prefix = f"features.approvals.{environment}"
    raw = {k: _setting(f"{prefix}.{k}") for k in ("approvals", "roles", "two_person")}
    raw = {k: v for k, v in raw.items() if v not in (None, "", [])}
    if not raw:
        return ApprovalPolicy()
    return ApprovalPolicy.from_mapping(raw, source=f"setting {prefix}")


def policies_for(flags: Iterable[FlagDef], environment: str) -> list[ApprovalPolicy]:
    """Every policy that holds a change touching ``flags`` in ``environment``: each flag's own,
    else the environment's. Policies that ask for nobody are left out."""
    default = environment_policy(environment)
    out: list[ApprovalPolicy] = []
    for flag in flags:
        policy = flag.approvals.get(environment) or default
        if policy.approvals and policy not in out:
            out.append(policy)
    return out


def _name(handle: str) -> str:
    return str(handle or "").strip().lstrip("@").split(":")[0].lower()


def roles_of(principal: str) -> tuple[str, ...]:
    """The roles ``principal`` holds: the directory's groups mapped to roles (ADR-103)."""
    if _role_resolver is not None:
        return tuple(str(r) for r in _role_resolver(principal))
    from axiom.extensions.builtins.directory import PrincipalRef
    from axiom.extensions.builtins.directory.config import load_directory_config

    cfg = load_directory_config()
    if cfg.provider is None:
        return ()
    groups = cfg.provider.groups_for(PrincipalRef(subject=principal, provider=cfg.provider_name))
    return tuple(cfg.role_map.roles_for([g.id for g in groups]))


def person_refusal(approver: str, author: str) -> str | None:
    """Why ``approver`` may not approve ``author``'s change, or ``None``.

    The author never approves their own change, and a platform agent is not a person. A
    roster that cannot be read refuses every approval rather than letting an agent through.
    """
    name = _name(approver)
    if not name:
        return "an approval needs a named person"
    if name == _name(author):
        return f"{approver} is the change's author; another person approves it"
    try:
        from axiom.agents import roster

        agents = {r.handle.lower() for r in roster.load_roster()}
    except Exception:  # noqa: BLE001
        return "cannot tell an agent from a person: the agent roster could not be read"
    if not agents:
        return "cannot tell an agent from a person: the agent roster is empty"
    if name in agents:
        return f"{approver} is a platform agent, and an approval needs a person"
    return None


def counted(policy: ApprovalPolicy, approvals: Iterable[Mapping[str, Any]], author: str) -> int:
    """The approvals that count toward ``policy``: by someone other than the author, holding a role it names."""
    seen: set[str] = set()
    for a in approvals:
        name = _name(a.get("by", ""))
        if not name or name == _name(author) or name in seen:
            continue
        # Current ids on both sides: ``operator`` is ``site-admin`` (ADR-261).
        if policy.roles and not set(canonical_roles(a.get("roles") or ())) & set(canonical_roles(policy.roles)):
            continue
        seen.add(name)
    return len(seen)


def describe(policy: ApprovalPolicy) -> str:
    who = f" holding {' or '.join(display_name(r) for r in policy.roles)}" if policy.roles else ""
    return (f"{policy.source or 'the policy'} needs {policy.approvals} approval(s) by someone other than "
            f"the author{who}")


def outstanding(policies: Iterable[ApprovalPolicy], approvals: Iterable[Mapping[str, Any]], author: str) -> list[str]:
    """What each policy still needs, in words; empty when every one is met."""
    approvals = list(approvals)
    out: list[str] = []
    for p in policies:
        have = counted(p, approvals, author)
        if have < p.approvals:
            out.append(f"{describe(p)}; it has {have}")
    return out


__all__ = [
    "counted",
    "describe",
    "environment_policy",
    "outstanding",
    "person_refusal",
    "policies_for",
    "roles_of",
    "set_environment_policies",
    "set_role_resolver",
]
