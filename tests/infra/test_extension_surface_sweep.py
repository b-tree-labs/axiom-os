# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Fleet-wide guards for the gaps that cost a live credential.

`axi vault renew` — the unattended pass that rotates a token BEFORE it expires —
was unreachable four different ways at once, and each failure alone would have
been caught by one of the checks below. It took all four to lose a credential:

1. Its CLI called the skill directly instead of through `invoke_capability`, so
   it produced no authority decision, no audit record and no telemetry.
2. `vault.renew` was registered nowhere, so it was not a capability at all —
   invisible to MCP, chat, agents, and to the capability discovery block.
3. `vault audit` and `vault list` were stubs that printed "not yet implemented"
   and returned **exit 0**, so any caller was told the audit passed.
4. Its verbs declared no `SkillSpec`, so they had no description and no
   surfaces, and an assistant asked to handle an expiring credential had no way
   to learn they existed. It reached for a raw git credential helper instead,
   got the wrong token of the eight on that host, and concluded there was none.

These are guards over the whole builtin fleet, not over vault, because vault was
not unique.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

import axiom.extensions.builtins as builtins_pkg

ROOT = Path(builtins_pkg.__file__).parent

#: Extensions with a `skills/` package whose CLI does NOT route through
#: `invoke_capability`. Every verb in these bypasses the authority gate, the
#: action audit and the capability series.
#:
#: This list may only ever SHRINK. It is not an exemption — it is the balance
#: outstanding, written down so it cannot quietly grow while nobody is counting.
KNOWN_BYPASSES = frozenset({"analytics", "connect", "memory", "principal"})

#: Past-tense claims that assert an effect. A branch that prints one of these
#: AND admits "not yet implemented" is reporting an action it did not take.
#:
#: The distinction matters: a READ verb that lists nothing and says the feature
#: does not exist yet is honest ("No peers connected — no shared resources"),
#: and flagging it would train people to ignore this guard. What is not honest
#: is "Join request submitted to <url>" when no request was sent.
_ACTION_CLAIMS = (
    "submitted", "created", "registered", "deployed", "published",
    "enrolled", "revoked", "rotated", "scheduled", "sent",
)

KNOWN_STUB_ZERO: frozenset[str] = frozenset()


def _clis():
    for ext in sorted(p for p in ROOT.iterdir() if p.is_dir() and not p.name.startswith("_")):
        cli = ext / "cli.py"
        if cli.exists():
            yield ext.name, ext, cli.read_text(encoding="utf-8")


def test_no_new_extension_bypasses_the_dispatch_chokepoint():
    found = {
        name for name, ext, text in _clis()
        if (ext / "skills").is_dir() and "invoke_capability" not in text
    }
    new = found - KNOWN_BYPASSES
    assert not new, (
        "new extensions bypassing invoke_capability (no authority gate, no "
        f"audit, no telemetry): {sorted(new)}"
    )
    fixed = KNOWN_BYPASSES - found
    assert not fixed, (
        f"these no longer bypass the chokepoint — remove them from "
        f"KNOWN_BYPASSES so the list keeps shrinking: {sorted(fixed)}"
    )


def test_no_stub_claims_an_action_it_did_not_take():
    """A verb that admits it is unimplemented must not also report success at
    having done the thing."""
    found = set()
    for name, _ext, text in _clis():
        for window in re.finditer(r"not yet implemented[\s\S]{0,600}?return 0", text):
            chunk = window.group(0).lower()
            if any(claim in chunk for claim in _ACTION_CLAIMS):
                found.add(name)
    new = found - KNOWN_STUB_ZERO
    assert not new, (
        "unimplemented verbs reporting a completed action and exiting 0: "
        f"{sorted(new)}"
    )


def test_the_action_claim_guard_can_fire():
    """Negative control. The previous version of this guard matched any
    'not yet implemented ... return 0' and would have flagged an honest empty
    listing too — a guard that cries wolf gets muted, which is how the real one
    survived."""
    sample = 'print("Join request submitted")\n    # not yet implemented\n    return 0'
    assert any(c in sample.lower() for c in _ACTION_CLAIMS)
    honest = 'print("No peers connected")\n    # not yet implemented\n    return 0'
    assert not any(c in honest.lower() for c in _ACTION_CLAIMS)


def test_no_cli_hands_a_skill_a_null_context():
    """The crash that took `vault renew` down. A skill signature reading
    `ctx: SkillContext | None = None` says None is acceptable; the bodies then
    dereference `ctx.state_dir` unconditionally."""
    offenders = [
        f"{name}/cli.py"
        for name, _ext, text in _clis()
        if re.search(r"\.run\(\s*[\w\[\]\"'.]+\s*,\s*None\s*\)", text)
    ]
    assert not offenders, f"CLI handlers passing a null skill context: {offenders}"


def test_a_declared_heartbeat_names_a_verb_that_exists():
    """`heartbeat_command` is what the background service runs unattended. A
    command that does not parse fails silently on a schedule nobody watches."""
    from axiom.extensions.builtins.vault import cli as vault_cli

    broken = []
    for name, ext, _text in _clis():
        manifest = ext / "axiom-extension.toml"
        if not manifest.exists():
            continue
        agent = (tomllib.loads(manifest.read_text(encoding="utf-8")).get("agent") or {})
        hb = str(agent.get("heartbeat_command", "")).strip()
        if not hb:
            continue
        parts = hb.split()
        if parts[0] != name.replace("_", "-") and parts[0] != name:
            continue  # verb belongs to a differently-named noun; not ours to parse
        if name == "vault":
            choices = {"list", "issue", "revoke", "audit", "resolve", "sweep",
                       "renew", "steward"}
            if parts[1] not in choices:
                broken.append(f"{name}: '{hb}' -> unknown subcommand {parts[1]!r}")
    assert not broken, broken
    assert vault_cli is not None


@pytest.mark.parametrize("capability", ["vault.resolve", "vault.audit"])
def test_the_credential_read_verbs_are_reachable_capabilities(capability):
    """Registered, described, and projected to MCP.

    Without this an assistant cannot learn they exist, which is the whole reason
    a raw credential helper got reached for instead.
    """
    from axiom.extensions.builtins.vault import skills

    registry = skills.bind_default()
    assert registry.has(capability), f"{capability} is not registered"
    spec = registry.spec(capability)
    assert spec is not None, f"{capability} has no SkillSpec (no description, no surfaces)"
    assert spec.description.strip(), f"{capability} has no description to discover it by"
    assert "mcp" in (spec.surfaces or ()), f"{capability} is not projected to MCP"
    assert spec.side_effects is False, (
        f"{capability} is exposed on MCP, so it must be declared read-only"
    )


def test_no_credential_WRITE_verb_is_projected_to_mcp():
    """Reading credential metadata over a protocol is fine; rotating, setting or
    revealing one is not. This is the line that keeps the read verbs safe to
    expose at all."""
    from axiom.extensions.builtins.vault import skills

    registry = skills.bind_default()
    for name in ("vault.renew", "vault.sweep"):
        spec = registry.spec(name)
        if spec is None:
            continue  # legacy registration carries no surfaces — not projected
        assert "mcp" not in (spec.surfaces or ()), f"{name} must stay off MCP"


def test_federation_join_does_not_claim_an_action_it_did_not_take():
    """The worst instance the sweep found.

    `axi federation join <url>` printed "Join request submitted to <url>" and
    returned 0 while the handshake protocol does not exist. Nothing was
    submitted. Vault's stubs at least said "not yet implemented" in the headline;
    this one reported a completed action, on a security-adjacent surface, to a
    caller with no way to tell.
    """
    from axiom.extensions.builtins.federation import cli as fed_cli

    source = Path(fed_cli.__file__).read_text(encoding="utf-8")
    assert "Join request submitted" not in source, (
        "federation join claims a submission that never happens"
    )


# --- the systemic half: capabilities nothing can discover ---------------------

#: Registered capabilities carrying no `SkillSpec` — no description, no declared
#: surfaces. They are reachable only if you already know the CLI verb, because
#: nothing can list them: not MCP, not the agent tool loop, not the capability
#: discovery block.
#:
#: This is the systemic form of the vault incident. An assistant asked to handle
#: an expiring credential could not learn that `vault resolve` or the thirteen
#: `secrets.*` verbs existed, so it reached for a raw git credential helper.
#:
#: A RATCHET, not an allowlist: the number may only fall. Every spec added moves
#: one capability into reach of every surface at once.
MAX_UNDISCOVERABLE = 58


def _bound_registry():
    import importlib
    import pkgutil

    from axiom.infra.skills import SkillRegistry

    registry = SkillRegistry()
    for mod_info in pkgutil.iter_modules(builtins_pkg.__path__):
        try:
            mod = importlib.import_module(
                f"axiom.extensions.builtins.{mod_info.name}.skills"
            )
        except Exception:
            continue
        binder = getattr(mod, "bind", None)
        if callable(binder):
            try:
                binder(registry)
            except Exception:
                continue
    return registry


def test_undiscoverable_capabilities_only_ever_decrease():
    registry = _bound_registry()
    names = sorted(registry._skills.keys())
    undiscoverable = [n for n in names if registry.spec(n) is None]

    assert len(undiscoverable) <= MAX_UNDISCOVERABLE, (
        f"{len(undiscoverable)} capabilities carry no SkillSpec, up from "
        f"{MAX_UNDISCOVERABLE}. A capability with no description and no declared "
        f"surfaces cannot be found by anything: {sorted(undiscoverable)[:8]}..."
    )
    assert len(undiscoverable) >= 0
    if len(undiscoverable) < MAX_UNDISCOVERABLE:
        pytest.fail(
            f"good news, and the ratchet has to move with it: "
            f"{len(undiscoverable)} undiscoverable now, lower than "
            f"MAX_UNDISCOVERABLE={MAX_UNDISCOVERABLE}. Lower the constant so the "
            f"gain is locked in."
        )


def test_the_ratchet_is_measuring_something_real():
    """Negative control. If the binder silently failed, every count would read
    zero and the ratchet would pass forever while measuring nothing."""
    registry = _bound_registry()
    assert len(registry._skills) > 50, (
        "extension skills did not bind — the ratchet above is measuring an "
        "empty registry"
    )
    assert any(
        registry.spec(n) is not None for n in registry._skills
    ), "no specs found at all"
