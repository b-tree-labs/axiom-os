# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Attribute captured tracker activity to principals, and detect drift.

This is the domain-free judgement the GitLab/GitHub feeders share once the
watcher primitive has fetched and deduped. It operates on a base program
(the node's data file — the authoritative structure: lanes, people, the
account map, the item ↔ issue bindings) plus normalized :class:`TrackerIssue`
and :class:`MergeRequest` observations, and produces an **overlaid** program
plus a list of findings.

Three decisions live here, all from the identity model Ben fixed 2026-10-06:

1. **Membership is a principal, attribution rides the account map.** A
   tracker action by ``npl436`` is attributed to the principal whose
   ``accounts[system] == "npl436"``. A person with no account on the system
   is not invisible — their items' real owner stays the principal and stays
   authoritative in the data file.

2. **The proxy-assignee rule.** For a tracker-bound item whose real owner has
   no account on the system, the *intended* assignee is the lane lead when
   the lead has one, else the deputy. The real owner is always named; the
   proxy is only who the item would be assigned *to* on the tracker. It is
   computed and recorded here — a later phase posts it; this phase never
   writes to the tracker.

3. **A missing account is an onboarding finding, not a silent gap** (R11's
   "owner with no account" orphan): one ``account_missing`` finding per such
   owner, carried on the drift/changes surface.

The overlay never clobbers the human-committed fields (``owner``, the
schedule dates, ``status``). It writes the live tracker facts into a
namespaced ``tracker`` sub-object, the attribution into ``activity`` and
``assignment``, and records a ``date_mismatch`` **finding** where the
tracker's due date disagrees with the item's committed date — detect, do not
overwrite.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..model import ProgramData

# ---------------------------------------------------------------------------
# Finding vocabulary (the capture half of drift, R11)
# ---------------------------------------------------------------------------

#: The closed set of findings the feeders produce. The data-file-only drift
#: kinds (``unowned_item`` …) live in ``status.py``; these are the ones that
#: need capture — attribution gaps, tracker/data divergence, mirror health.
#: Findings a *tracker* feeder (GitLab/GitHub issues) owns and recomputes.
TRACKER_FINDING_KINDS = ("account_missing", "date_mismatch", "untracked_issue")
#: Findings the *mirror* check owns and recomputes.
MIRROR_FINDING_KINDS = ("mirror_gap", "mirror_stale", "mirror_missing")
#: Findings the *crosslink-health* check owns and recomputes (R11 link health):
#: a bound issue that no longer resolves, a declared endpoint that is dead.
LINK_FINDING_KINDS = ("dead_link",)

CAPTURE_FINDING_KINDS = TRACKER_FINDING_KINDS + MIRROR_FINDING_KINDS + LINK_FINDING_KINDS


# ---------------------------------------------------------------------------
# Normalized observations (what a feeder's fetch yields, vendor-shape removed)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrackerIssue:
    """One tracker issue, normalized across vendors.

    ``ref`` is the value a schedule item's ``issue`` binding carries, so a
    feeder joins ``item.issue == issue.ref``.
    """

    ref: int | str
    title: str = ""
    state: str = "opened"  # "opened" | "closed"
    assignee: str | None = None  # the account username on the system
    milestone: str | None = None
    due: str | None = None  # ISO date
    labels: tuple[str, ...] = ()
    updated_at: str | None = None


@dataclass(frozen=True)
class MergeRequest:
    """One merge/pull request, normalized. ``issue_refs`` are the issues it
    closes or references, so it attaches to the matching schedule items."""

    ref: int | str
    title: str = ""
    state: str = "opened"  # "opened" | "merged" | "closed"
    author: str | None = None  # the account username on the system
    issue_refs: tuple[int | str, ...] = ()
    sha: str | None = None  # head commit — the content-identity key
    updated_at: str | None = None

    @property
    def landed(self) -> bool:
        return self.state == "merged"


@dataclass
class CaptureResult:
    """The overlaid program plus the findings the capture pass detected."""

    raw: dict[str, Any]
    findings: list[dict[str, Any]] = field(default_factory=list)

    @property
    def program(self) -> ProgramData:
        return ProgramData(raw=self.raw)


def _finding(kind: str, subject: Any, detail: str, **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "subject": subject, "detail": detail, **extra}


# ---------------------------------------------------------------------------
# The proxy-assignee rule
# ---------------------------------------------------------------------------


def intended_assignee(data: ProgramData, item: dict[str, Any], *, system: str) -> dict[str, Any]:
    """Who the item would be assigned to on the tracker, and why.

    - ``direct`` — the real owner has an account on the system.
    - ``lane_lead`` — the owner has none; the item's lane lead has one.
    - ``deputy`` — neither owner nor lane lead has one; the deputy does.
    - ``none`` — nobody in the chain has an account, so there is no proxy.

    The ``real_owner`` is always the item's own owner, named regardless.
    """
    owner = item.get("owner")
    real_owner = owner if isinstance(owner, str) else None

    direct = data.account(real_owner, system) if real_owner else None
    if direct:
        return {"real_owner": real_owner, "via": "direct", "account": direct, "proxy": None}

    lane_id = item.get("lane")
    lead = data.lane_lead(lane_id) if isinstance(lane_id, str) else None
    lead_account = data.account(lead, system) if lead else None
    if lead and lead_account:
        return {"real_owner": real_owner, "via": "lane_lead", "account": lead_account, "proxy": lead}

    deputy = data.deputy()
    deputy_account = data.account(deputy, system) if deputy else None
    if deputy and deputy_account:
        return {
            "real_owner": real_owner,
            "via": "deputy",
            "account": deputy_account,
            "proxy": deputy,
        }

    return {"real_owner": real_owner, "via": "none", "account": None, "proxy": None}


# ---------------------------------------------------------------------------
# Attribution + overlay
# ---------------------------------------------------------------------------


def _committed_date(item: dict[str, Any]) -> str | None:
    """The item's own committed date to compare the tracker's due against."""
    return item.get("end") or item.get("date")


def attribute_and_overlay(
    base: ProgramData,
    issues: list[TrackerIssue],
    mrs: list[MergeRequest],
    *,
    system: str,
) -> CaptureResult:
    """Overlay live tracker facts onto ``base`` and attribute activity.

    Returns a :class:`CaptureResult` whose ``raw`` is a deep copy of the base
    document with, per tracker-bound schedule item: a ``tracker`` facts block,
    an ``activity`` list of attributed MRs, and an ``assignment`` (the
    proxy-assignee computation). Findings cover missing accounts, tracker
    issues with no schedule item, and due-date disagreements.
    """
    raw = copy.deepcopy(base.raw)
    overlaid = ProgramData(raw=raw)

    issues_by_ref = {str(issue.ref): issue for issue in issues}

    # MRs grouped by the issue they reference.
    mrs_by_issue: dict[str, list[MergeRequest]] = {}
    for mr in mrs:
        for ref in mr.issue_refs:
            mrs_by_issue.setdefault(str(ref), []).append(mr)

    # --- overlay the delta onto the (carried-forward) base -----------------
    # Only items whose issue appears in this delta are touched; items overlaid
    # on a prior cycle keep their tracker block, so a cursor-delta read
    # converges rather than erasing what it did not re-fetch.
    for item in raw.get("schedule", []):
        if not isinstance(item, dict):
            continue
        issue_ref = item.get("issue")
        if issue_ref is None:
            continue  # unbound items are not assigned on the tracker

        # The proxy-assignee computation is data-derivable, so it is refreshed
        # for every tracker-bound item each cycle (owner/lead/deputy accounts
        # may have changed), independent of the delta.
        item["assignment"] = intended_assignee(overlaid, item, system=system)

        issue = issues_by_ref.get(str(issue_ref))
        if issue is None:
            continue  # outside this delta — leave the carried-forward facts

        item["tracker"] = {
            "system": system,
            "ref": issue.ref,
            "state": issue.state,
            "assignee": issue.assignee,
            "milestone": issue.milestone,
            "due": issue.due,
            "labels": list(issue.labels),
            "title": issue.title,
            "updated_at": issue.updated_at,
        }
        activity = [
            {
                "ref": mr.ref,
                "author_account": mr.author,
                "author": overlaid.principal_for_account(system, mr.author),
                "sha": mr.sha,
                "state": mr.state,
                "landed": mr.landed,
            }
            for mr in mrs_by_issue.get(str(issue_ref), [])
        ]
        if activity:
            item["activity"] = activity

    findings = capture_findings(overlaid, issues, system=system)
    return CaptureResult(raw=raw, findings=findings)


def capture_findings(
    program: ProgramData, observed_issues: list[TrackerIssue], *, system: str
) -> list[dict[str, Any]]:
    """The capture-half findings, computed over the (overlaid) program.

    Standing by construction: ``account_missing`` and ``date_mismatch`` are
    re-evaluated over every tracker-bound item each cycle (reading the item's
    carried-forward ``tracker`` block), so a cursor-delta read does not drop a
    finding it did not re-fetch, and a finding clears the cycle its condition
    does. ``untracked_issue`` is reported for the issues observed this cycle
    that bind to no item.
    """
    findings: list[dict[str, Any]] = []
    account_missing_seen: set[str] = set()

    for item in program.schedule:
        if not isinstance(item, dict) or item.get("issue") is None:
            continue
        assignment = item.get("assignment") or intended_assignee(program, item, system=system)

        owner = assignment.get("real_owner")
        if owner and assignment.get("via") != "direct" and owner not in account_missing_seen:
            account_missing_seen.add(owner)
            proxy = assignment.get("proxy")
            detail = (
                f"{owner} owns a {system}-tracked item but has no {system} account; "
                + (
                    f"proxy assignee is {proxy} (via {assignment.get('via')})"
                    if proxy
                    else "no lane lead or deputy has one either, so there is no proxy"
                )
            )
            findings.append(
                _finding(
                    "account_missing",
                    owner,
                    detail,
                    system=system,
                    proxy=proxy,
                    via=assignment.get("via"),
                    state="pending",
                )
            )

        tracker = item.get("tracker") or {}
        due = tracker.get("due") if isinstance(tracker, dict) else None
        committed = _committed_date(item)
        if due and committed and due != committed:
            findings.append(
                _finding(
                    "date_mismatch",
                    item.get("id"),
                    f"item committed date {committed} disagrees with tracker due {due}",
                    committed=committed,
                    tracker_due=due,
                )
            )

    # Orphans: an observed tracker issue with no schedule item (R11's third
    # orphan shape — active work with nothing tracking it).
    bound_refs = {
        str(item.get("issue"))
        for item in program.schedule
        if isinstance(item, dict) and item.get("issue") is not None
    }
    for issue in observed_issues:
        if str(issue.ref) not in bound_refs:
            findings.append(
                _finding(
                    "untracked_issue",
                    issue.ref,
                    f"{system} issue {issue.ref} ({issue.title!r}) has no schedule item",
                    system=system,
                )
            )

    return findings


# ---------------------------------------------------------------------------
# Crosslink health (R11 link health — no dead links)
# ---------------------------------------------------------------------------


@runtime_checkable
class LinkChecker(Protocol):
    """A read-only resolver for crosslink health. Every method is total and
    tri-state: ``True`` resolves, ``False`` is confirmed dead (e.g. a 404),
    ``None`` could not be verified (unreachable / forbidden / an error).

    ``None`` is the load-bearing value: an unreachable checker reports
    ``unverified``, never ``healthy`` — a link that could not be checked is
    neither confirmed dead nor rounded up to synced (the ADR-166 / R11 absence
    posture)."""

    def issue_readable(self, ref: int | str) -> bool | None:
        """Does the bound tracker ``issue`` still exist / is it readable?"""
        ...

    def url_resolves(self, url: str) -> bool | None:
        """Does a declared endpoint URL resolve (read-only GET/HEAD)?"""
        ...


@dataclass
class LinkHealth:
    """The crosslink-health pass result: the confirmed-dead ``findings``, how
    many links were checked, which could not be verified, and whether the pass
    verified cleanly (every checked link resolved and none was unverifiable)."""

    findings: list[dict[str, Any]] = field(default_factory=list)
    checked: int = 0
    unverified: list[dict[str, Any]] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        """True only when the pass actually confirmed every link's state —
        something was checked and nothing came back unverifiable. A pass that
        verified nothing is **not** healthy."""
        return self.checked > 0 and not self.unverified

    def as_block(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "dead": len(self.findings),
            "unverified": len(self.unverified),
            "verified": self.verified,
            "basis": "live" if self.checked and not self.unverified else "unverified",
        }


def check_links(
    program: ProgramData,
    checker: LinkChecker | None,
    *,
    canonical: str | None = None,
) -> LinkHealth:
    """Confirm the program's crosslinks still resolve (R11 link health).

    Two link classes, checked read-only:

    - each tracker-bound schedule item's ``issue`` binding (the issue exists /
      is readable) → a confirmed-dead one is a ``dead_link`` finding whose
      ``subject`` is the item and which carries the bad ``ref``;
    - each declared ``program.endpoints`` URL → a confirmed-dead one is a
      ``dead_link`` finding whose ``subject`` is the endpoint name.

    **Detection only.** A ``dead_link`` records the correct backlink target
    under ``proposed_fix`` (the program's ``canonical`` endpoint) for the later
    posting phase; nothing here writes to the tracker. An absent ``checker`` —
    or one that returns ``None`` — is honoured as ``unverified``: the link is
    not reported dead *or* healthy.
    """
    health = LinkHealth()

    for item in program.schedule:
        if not isinstance(item, dict):
            continue
        ref = item.get("issue")
        if ref is None:
            continue
        verdict = checker.issue_readable(ref) if checker is not None else None
        subject = item.get("id")
        if verdict is None:
            health.unverified.append({"link": "issue", "subject": subject, "ref": ref})
            continue
        health.checked += 1
        if verdict is False:
            health.findings.append(
                _finding(
                    "dead_link",
                    subject,
                    f"issue binding {ref!r} does not resolve (the issue is "
                    "gone or unreadable)",
                    link="issue",
                    ref=ref,
                    verified=True,
                    proposed_fix={"canonical": canonical} if canonical else None,
                )
            )

    for name, url in program.endpoints().items():
        verdict = checker.url_resolves(url) if checker is not None else None
        if verdict is None:
            health.unverified.append({"link": "endpoint", "subject": name, "url": url})
            continue
        health.checked += 1
        if verdict is False:
            health.findings.append(
                _finding(
                    "dead_link",
                    name,
                    f"declared endpoint {name!r} ({url}) does not resolve",
                    link="endpoint",
                    url=url,
                    verified=True,
                    proposed_fix=(
                        {"canonical": canonical} if canonical and url != canonical else None
                    ),
                )
            )

    return health


# ---------------------------------------------------------------------------
# Mirror agreement (the GitHub source is the mirror-check home)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MirrorPair:
    """A declared origin ↔ mirror repository pair (deployment config)."""

    origin_host: str
    origin_repo: str
    mirror_host: str
    mirror_repo: str

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> MirrorPair | None:
        origin = raw.get("origin") or {}
        mirror = raw.get("mirror") or {}
        if not isinstance(origin, dict) or not isinstance(mirror, dict):
            return None
        o_repo, m_repo = origin.get("repo"), mirror.get("repo")
        if not (isinstance(o_repo, str) and isinstance(m_repo, str)):
            return None
        return cls(
            origin_host=str(origin.get("host", "")),
            origin_repo=o_repo,
            mirror_host=str(mirror.get("host", "")),
            mirror_repo=m_repo,
        )

    @property
    def subject(self) -> str:
        return f"{self.origin_repo}->{self.mirror_repo}"


def declared_mirror_pairs(data: ProgramData) -> list[MirrorPair]:
    """The deployment's declared mirror pairs, from ``program.mirrors``.

    The pairs themselves are deployment data (real repo names live in config,
    never in code); this only reads the shape.
    """
    raw = data.program.get("mirrors")
    if not isinstance(raw, list):
        return []
    pairs = [MirrorPair.from_raw(p) for p in raw if isinstance(p, dict)]
    return [p for p in pairs if p is not None]


def check_mirror(
    pairs: list[MirrorPair],
    commits: dict[tuple[str, str], set[str] | None],
    *,
    recent: dict[str, bool | None] | None = None,
    tracker_repo: tuple[str, str] | None = None,
    expect_mirror: bool = False,
) -> list[dict[str, Any]]:
    """Confirm each declared pair agrees, by commit identity.

    ``commits`` maps ``(host, repo) -> {sha, …}`` (or ``None`` when that side
    could not be read). A commit present through both origin and mirror is
    the *same* commit — the sets intersect on SHA, so nothing is
    double-counted (the ADR-162 rule). A mirror missing commits the origin
    has is a ``mirror_gap``; ``recent[pair.subject] is False`` is a
    ``mirror_stale``. A ``tracker_repo`` that no declared pair covers, when a
    mirror is expected, is a ``mirror_missing``.
    """
    recent = recent or {}
    findings: list[dict[str, Any]] = []

    covered: set[tuple[str, str]] = set()
    for pair in pairs:
        covered.add((pair.origin_host, pair.origin_repo))
        covered.add((pair.mirror_host, pair.mirror_repo))

        origin = commits.get((pair.origin_host, pair.origin_repo))
        mirror = commits.get((pair.mirror_host, pair.mirror_repo))
        if origin is None or mirror is None:
            # Unverified — a side could not be read. Not "in sync": say so.
            findings.append(
                _finding(
                    "mirror_stale",
                    pair.subject,
                    "mirror agreement could not be verified: a side was unreadable",
                    verified=False,
                )
            )
            continue

        missing = origin - mirror  # commits the origin has that the mirror lacks
        if missing:
            findings.append(
                _finding(
                    "mirror_gap",
                    pair.subject,
                    f"{len(missing)} commit(s) present on the origin are absent from the mirror",
                    missing=len(missing),
                )
            )
        if recent.get(pair.subject) is False:
            findings.append(
                _finding(
                    "mirror_stale",
                    pair.subject,
                    "the mirror job has not run recently",
                )
            )

    if expect_mirror and tracker_repo is not None and tracker_repo not in covered:
        findings.append(
            _finding(
                "mirror_missing",
                tracker_repo[1],
                f"repository {tracker_repo[1]!r} is tracked on {tracker_repo[0]!r} but no "
                "mirror pair covers it, and a mirror is expected",
            )
        )

    return findings


__all__ = [
    "CAPTURE_FINDING_KINDS",
    "TRACKER_FINDING_KINDS",
    "MIRROR_FINDING_KINDS",
    "LINK_FINDING_KINDS",
    "TrackerIssue",
    "MergeRequest",
    "CaptureResult",
    "MirrorPair",
    "LinkChecker",
    "LinkHealth",
    "intended_assignee",
    "attribute_and_overlay",
    "capture_findings",
    "declared_mirror_pairs",
    "check_mirror",
    "check_links",
]
