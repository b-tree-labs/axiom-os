# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Retention classes, their platform floors, and recorded deletions (MVP-22-A2).

The platform defines the classes and enforces their floors (PRD §14.12, decided
2026-10-30):

=============  =====================================
Class          Floor
=============  =====================================
system         about 1 year
operational    at least 5 years
qualification  the current cycle plus one
lifetime       the life of the site or its licence
legal_hold     until released
=============  =====================================

A site may only **lengthen** a floor (MVP-08-A3): a shorter value is refused,
and the refusal names the floor it would have broken. A deletion that destroys
content before its floor is refused; after it, the deletion is **recorded**, as
a row the owning extension keeps and as a link in the tamper-evident action
chain (``infra.audit_trail``), written *before* anything is destroyed so that a
deletion nobody can attest never happens.

Nothing here knows what is being kept. A product maps its logs and items to
classes; the platform keeps the floors.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any


class RetentionClass(str, Enum):
    SYSTEM = "system"
    OPERATIONAL = "operational"
    QUALIFICATION = "qualification"
    LIFETIME = "lifetime"
    LEGAL_HOLD = "legal_hold"

    @classmethod
    def from_str(cls, value: str) -> RetentionClass:
        normalized = (value or "").strip().lower().replace(" ", "_").replace("-", "_")
        for member in cls:
            if member.value == normalized:
                return member
        candidates = ", ".join(m.value for m in cls)
        raise ValueError(f"unknown retention class {value!r}; candidates: {candidates}")


#: What an item gets when nothing says otherwise: an uploaded record is
#: operational until a person or a product says it is something else.
DEFAULT_CLASS = RetentionClass.OPERATIONAL

#: A qualification cycle when a site has not declared its own.
DEFAULT_QUALIFICATION_CYCLE = timedelta(days=2 * 365)

_YEAR = timedelta(days=365)

#: ``None`` means no time floor ends it: lifetime ends with the site, a legal
#: hold ends when it is released. Neither is ever deletable by age.
PLATFORM_FLOORS: Mapping[RetentionClass, timedelta | None] = {
    RetentionClass.SYSTEM: _YEAR,
    RetentionClass.OPERATIONAL: 5 * _YEAR + timedelta(days=1),  # five years, leap day included
    RetentionClass.QUALIFICATION: 2 * DEFAULT_QUALIFICATION_CYCLE,  # the current cycle + 1
    RetentionClass.LIFETIME: None,
    RetentionClass.LEGAL_HOLD: None,
}

_UNTIMED_REASON = {
    RetentionClass.LIFETIME: "it is kept for the life of the site",
    RetentionClass.LEGAL_HOLD: "it is under a legal hold until released",
}


class RetentionRefused(PermissionError):
    """A deletion, a site setting or a reclassification the floors do not allow."""

    def __init__(self, message: str, *, retention_class: RetentionClass | None = None,
                 floor: timedelta | None = None, until: datetime | None = None) -> None:
        super().__init__(message)
        self.retention_class = retention_class
        self.floor = floor
        self.until = until


def describe_floor(floor: timedelta | None) -> str:
    if floor is None:
        return "no time floor"
    days = floor.days
    if days >= 365 and days % 365 in (0, 1):
        years = days // 365
        return f"{years} year" + ("s" if years != 1 else "")
    return f"{days} days"


# ---------------------------------------------------------------- site policy


@dataclass(frozen=True)
class SitePolicy:
    """A site's floors: the platform's, lengthened where the site said so."""

    floors: Mapping[RetentionClass, timedelta | None] = field(default_factory=lambda: dict(PLATFORM_FLOORS))
    qualification_cycle: timedelta = DEFAULT_QUALIFICATION_CYCLE

    def floor(self, retention_class: RetentionClass) -> timedelta | None:
        return self.floors.get(retention_class, PLATFORM_FLOORS[retention_class])


def site_policy(
    overrides: Mapping[str, Any] | None = None, *, qualification_cycle_days: int | None = None
) -> SitePolicy:
    """Validate a site's retention settings into a policy (MVP-08-A3).

    ``overrides`` maps a class to a floor in days. A value shorter than the
    platform floor is refused with the floor named; a class without a time floor
    (lifetime, legal hold) cannot be given one.
    """
    floors: dict[RetentionClass, timedelta | None] = dict(PLATFORM_FLOORS)
    cycle = DEFAULT_QUALIFICATION_CYCLE
    if qualification_cycle_days is not None:
        cycle = timedelta(days=int(qualification_cycle_days))
        if cycle <= timedelta(0):
            raise RetentionRefused("a qualification cycle is a positive number of days")
        floors[RetentionClass.QUALIFICATION] = 2 * cycle
    for raw_class, raw_days in (overrides or {}).items():
        cls = RetentionClass.from_str(str(raw_class))
        platform = PLATFORM_FLOORS[cls]
        if platform is None:
            raise RetentionRefused(
                f"{cls.value} has no time floor ({_UNTIMED_REASON[cls]}); a site cannot set one",
                retention_class=cls,
            )
        try:
            asked = timedelta(days=int(raw_days))
        except (TypeError, ValueError):
            raise RetentionRefused(f"{cls.value} retention is a number of days, got {raw_days!r}",
                                   retention_class=cls) from None
        minimum = floors[cls] or platform
        if asked < minimum:
            raise RetentionRefused(
                f"{cls.value} retention of {asked.days} days is shorter than the platform floor "
                f"of {describe_floor(minimum)} ({minimum.days} days); a site may only lengthen it",
                retention_class=cls, floor=minimum,
            )
        floors[cls] = asked
    return SitePolicy(floors=floors, qualification_cycle=cycle)


_policy_provider: Callable[[str | None], SitePolicy] | None = None


def set_policy_provider(provider: Callable[[str | None], SitePolicy] | None) -> None:
    """Bind where a site's policy comes from (Settings › Data and retention; tests)."""
    global _policy_provider
    _policy_provider = provider


def policy_for(site: str | None) -> SitePolicy:
    """The site's policy, never shorter than the platform's. A provider fault falls back to the platform floors."""
    if _policy_provider is not None:
        try:
            got = _policy_provider(site)
        except Exception:  # noqa: BLE001 - the platform floors are always a safe answer
            return SitePolicy()
        # A provider cannot shorten a floor either: every value is re-checked.
        safe: dict[RetentionClass, timedelta | None] = {}
        for cls, platform in PLATFORM_FLOORS.items():
            asked = got.floor(cls)
            safe[cls] = None if platform is None else max(platform, asked or platform)
        return SitePolicy(floors=safe, qualification_cycle=got.qualification_cycle)
    return SitePolicy()


# ---------------------------------------------------------------- deletion


def deletable_after(
    retention_class: RetentionClass, created_at: datetime, *, policy: SitePolicy | None = None
) -> datetime | None:
    """The first instant content of this class may be destroyed; ``None`` when never by age."""
    floor = (policy or SitePolicy()).floor(retention_class)
    if floor is None:
        return None
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return created_at + floor


def check_deletion(
    retention_class: RetentionClass,
    created_at: datetime,
    *,
    now: datetime | None = None,
    policy: SitePolicy | None = None,
    holds: Iterable[str] = (),
) -> None:
    """Refuse destroying content its floor or a hold still keeps; return quietly otherwise."""
    held = [h for h in holds if h]
    if held:
        raise RetentionRefused(f"it is under {' and '.join(held)}; it cannot be deleted forever",
                               retention_class=retention_class)
    if retention_class in _UNTIMED_REASON:
        raise RetentionRefused(f"{_UNTIMED_REASON[retention_class]}; it cannot be deleted forever",
                               retention_class=retention_class)
    until = deletable_after(retention_class, created_at, policy=policy)
    now = now or datetime.now(UTC)
    if until is not None and now < until:
        floor = (policy or SitePolicy()).floor(retention_class)
        raise RetentionRefused(
            f"its retention class is {retention_class.value}, kept at least {describe_floor(floor)}; "
            f"it may be deleted forever from {until.date().isoformat()}",
            retention_class=retention_class, floor=floor, until=until,
        )


def check_reclassify(old: RetentionClass, new: RetentionClass, *, release_hold: bool = False) -> None:
    """A class change may lengthen what is kept, never shorten it; a legal hold ends only by release."""
    if old == new:
        return
    if old == RetentionClass.LEGAL_HOLD and not release_hold:
        raise RetentionRefused("a legal hold ends only when it is released", retention_class=old)
    if old == RetentionClass.LEGAL_HOLD:
        return
    if new in (RetentionClass.LIFETIME, RetentionClass.LEGAL_HOLD):
        return
    if old == RetentionClass.LIFETIME:
        raise RetentionRefused("lifetime retention cannot be shortened", retention_class=old)
    old_floor, new_floor = PLATFORM_FLOORS[old], PLATFORM_FLOORS[new]
    if old_floor is not None and new_floor is not None and new_floor < old_floor:
        raise RetentionRefused(
            f"{new.value} keeps less than {old.value} ({describe_floor(new_floor)} against "
            f"{describe_floor(old_floor)}); retention may only lengthen",
            retention_class=old, floor=old_floor,
        )


DELETION_SKILL = "retention.delete"


def record_deletion(
    *,
    kind: str,
    subject: str,
    principal: str,
    site: str | None,
    retention_class: RetentionClass,
    forever: bool,
    detail: Mapping[str, Any] | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    """Append the deletion to the tamper-evident action chain and return the link.

    Raises on failure, on purpose: the caller writes this BEFORE destroying
    anything, so a deletion that cannot be attested does not happen.
    """
    from axiom.infra.audit_trail import record_invocation
    from axiom.infra.paths import get_user_state_dir

    params = {
        "kind": kind,
        "subject": subject,
        "retention_class": retention_class.value,
        "forever": bool(forever),
        **dict(detail or {}),
    }
    record = record_invocation(
        # The same chain every skill invocation lands in (``SkillRegistry``), so
        # ``axi audit`` reads deletions beside everything else.
        state_dir=Path(state_dir) if state_dir is not None else get_user_state_dir(),
        skill=f"{DELETION_SKILL}.{kind}",
        params=params,
        principal=principal,
        site=site,
        outcome="deleted-forever" if forever else "deleted",
        errors_count=0,
    )
    return {"seq": record["seq"], "hash": record["hash"], "params_digest": record["params_digest"]}


__all__ = [
    "DEFAULT_CLASS",
    "DELETION_SKILL",
    "PLATFORM_FLOORS",
    "RetentionClass",
    "RetentionRefused",
    "SitePolicy",
    "check_deletion",
    "check_reclassify",
    "deletable_after",
    "describe_floor",
    "policy_for",
    "record_deletion",
    "set_policy_provider",
    "site_policy",
]
