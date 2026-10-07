# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Human-consent gate for host-persistent daemon-agent registration.

Installing OS services (systemd / launchd / schtasks) that survive reboots is a
**host-modifying, long-running** action — it must never happen without explicit
operator consent. (2026-05-28 incident: a Windows host silently received 5
scheduled-task registrations on a routine CLI run.) This module records the
operator's one-time decision so startup self-heal never surprises them, and so
they can opt in to **all** agents, **none** (don't ask again), or an
**à-la-carte** subset.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from axiom.infra.paths import get_user_state_dir

_CONSENT_FILE = "agents_consent.json"


@dataclass
class AgentConsent:
    """The operator's persisted decision about host-persistent agent services."""

    decided: bool = False           # has the operator been asked + answered?
    opted_out: bool = False         # "never" — do not install
    enabled: list[str] = field(default_factory=list)  # agents approved to run
    decided_version: str = ""       # product version at decision time (re-offer key)
    #: Agents that EXISTED when the decision was made. A decision covers
    #: what was on the table; an agent shipped afterwards was not declined,
    #: it was never offered. Empty on records written before this field, for
    #: which the two cannot be told apart — see `never_offered()`.
    known: list[str] = field(default_factory=list)


#: The tier an agent declares in its manifest to be on after a fresh install.
#: The rule, so a reviewer can check a manifest without reading this file: an
#: agent may be core when it reads only local state and writes only its own.
#: Anything that reaches GitHub, an inbox, a drive or a publishing target is
#: a consent decision the operator makes, not one the installer makes for them.
CORE_TIER = "core"


def default_enabled_agents(builtins_root: Path | None = None) -> list[str]:
    """Agents enabled on a fresh install, read from the manifests.

    `AgentConsent.enabled` defaulted to an empty list, so a new install
    dispatched nothing and the operator had to find `axi agents register`
    before the platform did anything for them. Measured on a real machine:
    six of seven agents had never run, release/RIVET for 23 days, which is
    why sixteen consecutive CI failures on main went unseen.

    The set is derived from `[agent] default_consent` rather than listed here,
    so adding an agent is one manifest edit and there is no second place to
    forget. Unreadable or unparseable manifests are skipped rather than fatal:
    a broken third-party extension must not stop the platform's own agents
    from starting.
    """
    import tomllib

    root = builtins_root or (Path(__file__).resolve().parents[1])
    out: list[str] = []
    for manifest in sorted(root.glob("*/axiom-extension.toml")):
        try:
            data = tomllib.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        agent = data.get("agent") or data.get("extension", {}).get("agent") or {}
        if agent.get("default_consent") == CORE_TIER:
            out.append(manifest.parent.name)
    return out


def _all_agent_names(builtins_root: Path | None = None) -> list[str]:
    """Every agent the platform ships, core tier or not.

    The decision's scope. `default_enabled_agents()` answers which are ON;
    this answers which were ASKED ABOUT, and an operator who said no to an
    opt-in agent should not be re-asked just because they said no.
    """
    import tomllib

    root = builtins_root or (Path(__file__).resolve().parents[1])
    out: list[str] = []
    for manifest in sorted(root.glob("*/axiom-extension.toml")):
        try:
            data = tomllib.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        agent = data.get("agent") or data.get("extension", {}).get("agent") or {}
        if agent:
            out.append(manifest.parent.name)
    return out


def consent_path() -> Path:
    """Where the operator's agent consent is recorded.

    The PLATFORM directory, not the branded one. There is exactly one
    Background Service on a machine and it runs as the platform, so the
    consent it reads is the platform's. A brand-scoped record meant `neut
    agents status` reported "opted out (no agents run)" — on a machine where
    consent had been granted under `axi` and all six agents were dispatching.
    A consent record that disagrees with what is actually running is worse
    than no display at all.
    """
    from axiom.infra.paths import get_platform_state_dir

    shared = get_platform_state_dir() / _CONSENT_FILE
    if not shared.exists():
        # Adopt a record written under a brand home before the paths were
        # unified, so a decision the operator already made is not silently
        # re-asked.
        try:
            branded = get_user_state_dir() / _CONSENT_FILE
            if branded.exists() and branded.resolve() != shared.resolve():
                shared.parent.mkdir(parents=True, exist_ok=True)
                shared.write_text(branded.read_text())
        except Exception:  # noqa: BLE001
            pass
    return shared


def current_version() -> str:
    """Installed product version, or '' if it can't be determined."""
    try:
        from importlib.metadata import version

        from axiom.infra.branding import get_branding

        return version(get_branding().package_name)
    except Exception:
        return ""


def _minor_tuple(v: str) -> tuple[int, int] | None:
    """(major, minor) from a version string; None if unparseable."""
    try:
        parts = v.strip().lstrip("v").split(".")
        return (int(parts[0]), int(parts[1]))
    except (ValueError, IndexError):
        return None


def load_consent() -> AgentConsent:
    """Load the persisted decision; a fresh/corrupt file reads as undecided.

    An UNDECIDED install gets the core tier rather than nothing. An operator
    who has decided keeps exactly what they decided — including an empty list,
    which is a real choice and is not overwritten here. The distinction matters:
    before this, "never asked" and "said no to everything" were the same state,
    and every fresh install silently landed in the second one.
    """
    try:
        d = json.loads(consent_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return AgentConsent(enabled=default_enabled_agents())
    consent = AgentConsent(
        decided=bool(d.get("decided", False)),
        opted_out=bool(d.get("opted_out", False)),
        enabled=[str(a) for a in d.get("enabled", [])],
        decided_version=str(d.get("decided_version", "")),
        known=[str(a) for a in d.get("known", [])],
    )
    # A decision covers what was decidable. An agent that reached the core
    # tier AFTER the operator decided was never on the table, so treating
    # their silence as a refusal is reading a choice they did not make — the
    # same error as treating a site that has declared nothing as having
    # declared the floor.
    #
    # Only applied where the record can prove what was offered. A record
    # without `known` predates the field, and there "declined" and "never
    # saw it" are genuinely indistinguishable; those surface through
    # `never_offered()` instead of being enabled behind the operator.
    if consent.decided and not consent.opted_out and consent.known:
        undecided = [
            a for a in default_enabled_agents()
            if a not in consent.known and a not in consent.enabled
        ]
        if undecided:
            consent.enabled = [*consent.enabled, *undecided]
    return consent


def never_offered(consent: AgentConsent | None = None) -> list[str]:
    """Core agents this operator has never been asked about.

    For a record written before `known` existed, a core agent missing from
    `enabled` is ambiguous: it may have been declined, or it may have
    shipped later. The platform will not guess in either direction — it
    does not enable them behind the operator, and it does not stay quiet
    either, because quiet is how three core agents came to have never run
    on a machine whose operator believed agents were working.

    Measured on a real machine 2026-10-02: decided at 0.47.0, enabled
    `hygiene` and `release`. `diagnostics`, `directory` and `vault` are core
    by manifest and are not in that list, so the service does not dispatch
    them. They are not agents that never ran — the tick log shows vault
    dispatched 110 times, last on 2026-09-21 — they are agents that STOPPED,
    and nothing in the platform says when or why.
    """
    c = consent if consent is not None else load_consent()
    if c.opted_out:
        return []          # a real "never", and it is not reopened here
    if c.known:
        return []          # the record can prove what was offered
    return [a for a in default_enabled_agents() if a not in c.enabled]


def save_consent(consent: AgentConsent) -> None:
    """Persist the decision. Best-effort; never raises."""
    path = consent_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "decided": consent.decided,
                    "opted_out": consent.opted_out,
                    "enabled": consent.enabled,
                    "decided_version": consent.decided_version,
                    # What was on the table. Without it, a later core agent
                    # cannot be told from one this operator declined.
                    "known": consent.known or sorted(_all_agent_names()),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass


def record_decision(
    *, enabled: list[str], opted_out: bool = False, version: str | None = None
) -> AgentConsent:
    """Build + persist a decision (enable a set, or opt out entirely).

    Stamps the current product version so an opt-out can be gently re-offered
    after a later upgrade (see ``should_reoffer_after_optout``).
    """
    previous = load_consent()
    consent = AgentConsent(
        decided=True,
        opted_out=opted_out,
        enabled=list(enabled),
        decided_version=current_version() if version is None else version,
        known=sorted(_all_agent_names()),
    )
    save_consent(consent)
    _journal_change(previous, consent)
    return consent


def journal_path() -> Path:
    """Append-only record of every consent change."""
    return consent_path().with_name("agents_consent_journal.jsonl")


def _journal_change(before: AgentConsent, after: AgentConsent) -> None:
    """Record what changed, so "why did this agent stop?" has an answer.

    The consent file holds only the CURRENT decision, so an agent that
    stopped being dispatched left no trace of when or why. On a real
    machine, `vault` and `diagnostics` stopped on 2026-09-21 and `directory`
    and `publishing` on 2026-09-07, and nothing anywhere could say what
    changed on either date — the tick log proved they had stopped and was
    silent on the cause.

    Best-effort and never raises: losing the journal must not stop an
    operator recording a decision. Values here are agent names, never
    credentials.
    """
    gained = sorted(set(after.enabled) - set(before.enabled))
    lost = sorted(set(before.enabled) - set(after.enabled))
    if not gained and not lost and before.opted_out == after.opted_out:
        return
    try:
        with journal_path().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": time.time(),
                "version": after.decided_version,
                "enabled_after": sorted(after.enabled),
                "gained": gained,
                "lost": lost,
                "opted_out": after.opted_out,
            }) + "\n")
    except OSError:
        pass


def should_reoffer_after_optout(consent: AgentConsent, version: str) -> bool:
    """True iff an opted-out operator should be re-asked because the product
    upgraded (major or minor) since they declined. Unparseable versions on
    either side -> stay quiet (never nag on ambiguity)."""
    if not consent.opted_out:
        return False
    prev = _minor_tuple(consent.decided_version)
    cur = _minor_tuple(version)
    if prev is None or cur is None:
        return False
    return cur > prev


def agents_to_self_heal(consent: AgentConsent, missing: list[str]) -> list[str]:
    """Which *missing* agents may be re-registered on startup WITHOUT a new
    prompt — i.e. ones the operator already approved.

    - Opted out, or never decided → none (caller must not install; for
      undecided, caller prompts first).
    - Decided + approved → the intersection of approved ∩ missing.
    """
    if consent.opted_out or not consent.decided:
        return []
    return [a for a in missing if a in consent.enabled]


def needs_prompt(consent: AgentConsent, missing: list[str]) -> bool:
    """True iff there are missing agents AND the operator hasn't decided yet.
    A prior decision (enable-some or opt-out) is respected without re-nagging."""
    return bool(missing) and not consent.decided


class Deferred(ValueError):
    """The operator wants to decide later, and nothing should be recorded.

    A distinct answer, because the alternatives were installing an
    operating-system service or closing the subject permanently, and somebody
    new to agents means neither. It subclasses ``ValueError`` so every caller
    that already treats a bad answer as "change nothing" keeps behaving
    correctly; the one caller that wants to say "I will ask again" can tell the
    two apart.

    Nothing is written for a deferral. A recorded "later" that reads as a
    decision on the next run is the failure this exists to avoid.
    """


def parse_register_selection(
    raw: str, candidates: list[str]
) -> tuple[list[str], bool]:
    """Parse an interactive `agents register` answer into (enabled, opted_out).

    Accepts (case/space-insensitive):
      - ``a`` / ``all``            -> (all candidates, False)
      - ``n`` / ``none``           -> ([], True)  # opt out, don't ask again
      - ``l`` / ``later`` / empty  -> raises :class:`Deferred`
      - ``1,3`` / ``1 3``          -> 1-based picks from ``candidates``

    Empty input is a deferral rather than an error. Enter is the key somebody
    presses when they are not sure, so it has to mean the safe thing on purpose
    instead of falling through to a parse failure that happens to be harmless
    — a colleague pressed it during onboarding and could not tell afterwards
    whether he had opted out.

    Raises ``ValueError`` on an unrecognized or out-of-range token, which stays
    distinct from ``Deferred``: deferring on a typo would change nothing while
    reading as a deliberate choice.
    """
    s = raw.strip().lower()
    if not s or s in ("l", "later"):
        raise Deferred("decide later")
    if s in ("a", "all"):
        return list(candidates), False
    if s in ("n", "none"):
        return [], True
    picks: list[str] = []
    for tok in s.replace(",", " ").split():
        if not tok.isdigit():
            raise ValueError(f"not a number: {tok!r}")
        idx = int(tok)
        if not 1 <= idx <= len(candidates):
            raise ValueError(f"out of range: {idx}")
        name = candidates[idx - 1]
        if name not in picks:
            picks.append(name)
    if not picks:
        raise ValueError("no selection")
    return picks, False
