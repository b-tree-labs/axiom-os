# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One human, one owner identity — and a way to notice when that stops holding.

Found in a real ledger: 11,170 fragments under one identity and 105 more
stranded across five other spellings of the same person — a work email, a
personal email, a git commit address, a machine-derived handle, and one
malformed value that welded a ``@name:context`` prefix onto an email address.
None of it deliberate. Several independent principal resolvers, each with its
own fallback chain, meant the spelling depended on which code path wrote.

Nothing reported it. It was found by hand, months later, because a query came
back empty and someone went looking. That is the part this addresses: a ledger
that silently partitions is worse than one that refuses a bad write, because
the memories are still there and simply cannot be found. Recall degrades with
no error, no warning, and no symptom except an answer that is quietly thinner
than it should be.

Two defences, because the failures differ:

**Validation** stops a malformed identity being minted at all. ``@a@b.org`` is
not a judgement call — it is two naming conventions at once, and no resolver
meant to produce it.

**Drift detection** catches the legitimate-looking ones. A second valid
spelling of the same person is indistinguishable from a second person, so it
is reported rather than rejected: the platform cannot know that two addresses
are one human, and guessing would merge people who are genuinely distinct.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Matrix-style handle: ``@name:context``, one ``@``, one ``:``.
_HANDLE = re.compile(r"^@[a-z0-9][a-z0-9._-]*:[a-z0-9][a-z0-9._-]*$", re.IGNORECASE)

#: Bare address form. Deliberately permissive on the domain; the point is to
#: reject the shapes that indicate a resolver bug, not to police email syntax.
_ADDRESS = re.compile(r"^[a-z0-9][a-z0-9._%+-]*@[a-z0-9][a-z0-9.-]*\.[a-z]{2,}$", re.IGNORECASE)

#: Below this share of the ledger, an identity is a tail rather than a peer —
#: the shape of an accident, not of a second real user of the same node.
_MINORITY_SHARE = 0.25


def is_valid_principal(value: str | None) -> bool:
    """Whether ``value`` is a well-formed principal in either convention."""
    if not value or not value.strip():
        return False
    candidate = value.strip()
    return bool(_HANDLE.match(candidate) or _ADDRESS.match(candidate))


def normalize_principal(value: str | None) -> str:
    """Best-effort canonical spelling; "" when there is nothing to work with.

    Only repairs unambiguous damage. A stray ``@`` in front of an address is
    unambiguous — the address form takes no prefix, and the handle form takes
    no second ``@``. Case is folded for addresses, which are case-insensitive
    by specification, and left alone for handles, which are not this module's
    to reinterpret.
    """
    if not value or not value.strip():
        return ""
    candidate = value.strip()
    if candidate.startswith("@") and _ADDRESS.match(candidate[1:]):
        candidate = candidate[1:]
    if _ADDRESS.match(candidate):
        return candidate.lower()
    return candidate


@dataclass(frozen=True)
class OwnerDrift:
    """What a ledger's owner identities look like, and whether that is a problem."""

    dominant: str
    minority: tuple[str, ...]
    malformed: tuple[str, ...]
    stranded: int
    total: int
    has_drift: bool
    summary: str = field(default="")


def detect_owner_drift(
    owner_counts: dict[str, int], *, ignore: set[str] | None = None
) -> OwnerDrift:
    """Report whether one ledger is carrying several spellings of one owner.

    ``ignore`` excludes owners that are legitimately not the user — a system
    actor, a test fixture — so their presence is not mistaken for the problem
    this looks for.
    """
    counts = {k: v for k, v in (owner_counts or {}).items() if k not in (ignore or set())}
    if not counts:
        return OwnerDrift("", (), (), 0, 0, False, "No owned fragments.")

    total = sum(counts.values())
    dominant = max(counts, key=lambda k: counts[k])
    malformed = tuple(sorted(k for k in counts if not is_valid_principal(k)))
    minority = tuple(
        sorted(
            k for k in counts
            if k != dominant and counts[k] / total < _MINORITY_SHARE
        )
    )
    stranded = sum(counts[k] for k in minority)
    has_drift = bool(minority) or bool(malformed)

    if not has_drift:
        summary = f"One owner identity across {total:,} fragments."
    else:
        parts = []
        if stranded:
            parts.append(
                f"{stranded:,} of {total:,} fragments are owned by "
                f"{len(minority)} other spelling(s) of an identity and will not "
                f"be found by queries running as {dominant!r}"
            )
        if malformed:
            parts.append(f"malformed owner(s): {', '.join(malformed)}")
        summary = ". ".join(parts) + "."

    return OwnerDrift(
        dominant=dominant, minority=minority, malformed=malformed,
        stranded=stranded, total=total, has_drift=has_drift, summary=summary,
    )


__all__ = [
    "OwnerDrift",
    "detect_owner_drift",
    "is_valid_principal",
    "normalize_principal",
]
