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


def consent_path() -> Path:
    return get_user_state_dir() / _CONSENT_FILE


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
    """Load the persisted decision; a fresh/corrupt file reads as undecided."""
    try:
        d = json.loads(consent_path().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return AgentConsent()
    return AgentConsent(
        decided=bool(d.get("decided", False)),
        opted_out=bool(d.get("opted_out", False)),
        enabled=[str(a) for a in d.get("enabled", [])],
        decided_version=str(d.get("decided_version", "")),
    )


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
    consent = AgentConsent(
        decided=True,
        opted_out=opted_out,
        enabled=list(enabled),
        decided_version=current_version() if version is None else version,
    )
    save_consent(consent)
    return consent


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


def parse_register_selection(
    raw: str, candidates: list[str]
) -> tuple[list[str], bool]:
    """Parse an interactive `agents register` answer into (enabled, opted_out).

    Accepts (case/space-insensitive):
      - ``a`` / ``all``            -> (all candidates, False)
      - ``n`` / ``none``           -> ([], True)  # opt out, don't ask again
      - ``1,3`` / ``1 3``          -> 1-based picks from ``candidates``

    Raises ``ValueError`` on empty input or an unrecognized / out-of-range
    token so the caller can treat it as "cancel, change nothing".
    """
    s = raw.strip().lower()
    if not s:
        raise ValueError("no selection")
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
