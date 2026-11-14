# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Work across many repositories, rolled up to program items as evidence.

A program's home tracker holds one issue per program item. The work itself
happens elsewhere: issues and pull requests in the deployment's work
repositories (``program.work_repos``). This module is the domain-free
judgement that joins the two, plus the cross-link to a second, read-only
tracker. Nothing here performs I/O; the sources hand it observations.

**Linking is explicit, never guessed.** A work issue or pull request rolls up
to a program item only when its title or body names it, one of three ways:

- ``<tracker-name>#N`` or ``<owner>/<tracker-name>#N`` (the tracker repo);
- the tracker issue's full URL (``https://<host>/<owner>/<name>/issues/N``);
- a ``Program: <url>`` line, whose URL is a tracker issue or a status-page
  anchor (``…#item-<id>``).

Recent work that names no program item is an ``unlinked_work`` finding.

**Progress is evidence, cited.** Per item the evidence is the linked open and
closed issues and the merged pull requests, each with its URL. From it
:func:`assess` derives a percent and a delivery state (``not_started`` |
``in_progress`` | ``done``), and :func:`apply_evidence` applies them under the
program's update policy, always recording the basis and the evidence links.
A change the policy will not apply (a decrease, a step backward, or any change
under ``propose``) becomes a ``progress_proposal`` finding. **Dates are never
applied:** a date the evidence disagrees with is a ``date_proposal`` finding
for the item's owner.

**The cross-link to a second tracker.** A program item's issue may end with a
line like ``GitLab tracker: <url>``. Only the URL is read
(:func:`parse_tracker_crosslink`); it is resolved against the declared
``program.tracker_sources`` and the linked item's *state* is carried. Its
title and body are not copied — the second tracker may hold text the home
tracker must not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from ..model import DELIVERY_VALUES, EVIDENCE_MODES

#: Findings the work-repository pass owns and recomputes each cycle.
WORK_FINDING_KINDS = (
    "unlinked_work",
    "crosslink_unresolved",
    "progress_proposal",
    "date_proposal",
    "repo_excluded_as_mirror",
    "repo_lineage_ambiguous",
    "link_proposed",
)

#: The delivery vocabulary evidence can set (``status`` stays the owner's
#: proposed/committed date commitment and is never written from evidence).
_DELIVERY_RANK = {value: rank for rank, value in enumerate(DELIVERY_VALUES)}

#: How many evidence links a basis cites before it says "and N more".
_MAX_CITES = 20


# ---------------------------------------------------------------------------
# Observations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkItem:
    """One issue or pull request in a work repository, normalized.

    ``text`` (title + body) is held only so links can be found; it never
    reaches the data file. ``done_at`` is the merge time of a pull request or
    the close time of an issue.
    """

    repo: str
    kind: str  # "issue" | "pr"
    number: int | str
    state: str  # issue: open|closed; pr: open|closed|merged
    url: str = ""
    text: str = ""
    updated_at: str | None = None
    done_at: str | None = None
    #: Label names and milestone title, read only to propose a link for
    #: unlinked work; like ``text`` they never reach the data file.
    labels: tuple[str, ...] = ()
    milestone: str | None = None

    @property
    def key(self) -> str:
        return f"{self.repo}#{self.number}"

    @property
    def is_evidence(self) -> bool:
        """Issues (open or closed) and merged pull requests are evidence; an
        open or abandoned pull request is not yet."""
        return self.kind == "issue" or self.state == "merged"

    def evidence_entry(self) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "repo": self.repo,
            "kind": self.kind,
            "number": self.number,
            "state": self.state,
            "url": self.url,
        }
        if self.done_at:
            entry["at"] = self.done_at
        return entry


# ---------------------------------------------------------------------------
# The cross-link line on a program item
# ---------------------------------------------------------------------------

_CROSSLINK_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?P<label>[A-Za-z][\w .-]{0,40}?)\s+tracker\s*:\s*<?(?P<url>https?://[^\s>]+)>?\s*$",
    re.IGNORECASE,
)


_WORK_URL_RE = re.compile(r"https?://[^/\s]+/([\w.-]+/[\w.-]+)/(?:issues|pull)/(\d+)\b")


def parse_work_links(body: Any, *, tracker_repo: str) -> tuple[str, ...]:
    """The work a program item's body names, as ``owner/repo#n`` keys.

    Only issue and pull-request URLs count, and links into the tracker itself
    (a parent, a sibling item) are not work. Nothing else of the body is kept.
    """
    if not isinstance(body, str):
        return ()
    tracker = tracker_repo.lower()
    keys: list[str] = []
    for repo, num in _WORK_URL_RE.findall(body):
        if repo.lower() == tracker:
            continue
        key = f"{repo}#{num}".lower()
        if key not in keys:
            keys.append(key)
    return tuple(keys)


def parse_tracker_crosslink(body: Any) -> str | None:
    """The URL on the body's last non-empty line when that line reads
    ``<Label> tracker: <url>`` (e.g. ``GitLab tracker: https://…``), else
    ``None``. Nothing else of the body is returned."""
    if not isinstance(body, str):
        return None
    lines = [line for line in body.splitlines() if line.strip()]
    if not lines:
        return None
    match = _CROSSLINK_RE.match(lines[-1])
    return match.group("url") if match else None


#: Values that leave an ``Owner:`` / ``Done when:`` line unfilled.
_UNFILLED = frozenset(
    {"", "tbd", "tbc", "?", "??", "-", "--", "none", "n/a", "na", "todo", "unknown", "unassigned", "nobody", "open"}
)


def is_unfilled(value: str) -> bool:
    """A placeholder rather than a value: ``TBD``, ``unassigned``, ``to be decided ...``."""
    norm = " ".join(value.lower().strip(" .:;").split())
    return norm in _UNFILLED or norm.startswith(("to be ", "tbd ", "tbc ", "not yet "))
_FIELD_LINE = re.compile(r"^\s*(?:-\s*)?(?P<name>owner|done when|parent|epic)\s*:\s*(?P<value>.*?)\s*$", re.IGNORECASE)
_TASK_LINE = re.compile(r"^\s*[-*]\s*\[[ xX]\]\s*(?P<rest>.*)$")


def _tracker_refs(text: str, tracker_repo: str) -> list[str]:
    """Issue numbers in ``text`` that point into the tracker: ``#N``,
    ``owner/name#N`` for the tracker repo, or its issue URL."""
    refs: list[str] = []
    repo = tracker_repo.lower()
    for owner_repo, num in re.findall(r"https?://[^/\s]+/([\w.-]+/[\w.-]+)/(?:-/)?issues/(\d+)\b", text):
        if not repo or owner_repo.lower() == repo:
            refs.append(num)
    for owner_repo, num in re.findall(r"(?<![\w./-])([\w.-]+/[\w.-]+)#(\d+)\b", text):
        if repo and owner_repo.lower() == repo:
            refs.append(num)
    refs.extend(re.findall(r"(?<![\w/#])#(\d+)\b", text))
    out: list[str] = []
    for ref in refs:
        if ref not in out:
            out.append(ref)
    return out


def parse_body_structure(
    body: Any, *, tracker_repo: str = ""
) -> tuple[tuple[str, ...] | None, str | None, tuple[str, ...], str | None]:
    """What a program item's body says about its place in the program, and nothing else.

    Returns ``(fields, parent, children, owner)``: which of ``owner`` /
    ``done_when`` are filled lines (``Owner: <someone>``, ``Done when:
    <condition>``); the tracker issue a ``Parent:`` or ``Epic:`` line names;
    the tracker issues the body's task list names (an epic's children); and the
    value of the first filled ``Owner:`` line (a name to resolve against the
    program's people, never trusted as a principal by itself). ``fields`` is ``None`` when
    there is no body at all, so a check never reads "no body" as "missing line".
    The body text itself is not kept.
    """
    if not isinstance(body, str):
        return None, None, (), None
    fields: list[str] = []
    owner: str | None = None
    owner_seen = False
    parent: str | None = None
    children: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.replace("*", "").replace("__", "")
        task = _TASK_LINE.match(raw_line)
        if task:
            for ref in _tracker_refs(task.group("rest"), tracker_repo):
                if ref not in children:
                    children.append(ref)
            continue
        match = _FIELD_LINE.match(line)
        if not match:
            continue
        name = match.group("name").lower()
        value = match.group("value").strip()
        if name in ("parent", "epic"):
            refs = _tracker_refs(value, tracker_repo)
            if refs and parent is None:
                parent = refs[0]
        else:
            key = "done_when" if name == "done when" else "owner"
            if key == "owner" and not owner_seen:
                owner_seen = True  # only the first Owner: line counts
                owner = None if is_unfilled(value) else value[:120]
            if not is_unfilled(value) and key not in fields:
                fields.append(key)
    return tuple(fields), parent, tuple(children), owner


def body_structure_fields(body: Any, tracker_repo: str = "") -> dict[str, Any]:
    """:func:`parse_body_structure` as the keyword arguments a ``TrackerIssue`` takes."""
    fields, parent, children, owner = parse_body_structure(body, tracker_repo=tracker_repo)
    return {"body_fields": fields, "parent": parent, "children": children, "body_owner": owner}


def declared_tracker_sources(program: dict[str, Any]) -> list[dict[str, Any]]:
    raw = program.get("tracker_sources")
    if not isinstance(raw, list):
        return []
    return [s for s in raw if isinstance(s, dict) and isinstance(s.get("host"), str)]


#: An issue path on a tracker. GitLab hands out ``/-/work_items/N`` as well as
#: ``/-/issues/N`` for the same item, so both name it.
_ISSUE_PATH = re.compile(r"^/(?P<project>.+?)/(?:-/)?(?:issues|work_items)/(?P<ref>\d+)/?$")


def resolve_crosslink(url: str, sources: list[dict[str, Any]]) -> tuple[dict[str, Any], int] | None:
    """Which declared source a cross-link URL points into, and the issue ref.

    Matches on host and project path (``project`` in the source config,
    compared case-insensitively). ``None`` when no declared source covers the
    URL — reported as ``crosslink_unresolved``, never guessed."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    match = _ISSUE_PATH.match(parts.path or "")
    if not match:
        return None
    host = (parts.hostname or "").lower()
    project = match.group("project").strip("/").lower()
    for source in sources:
        if str(source.get("host", "")).lower() != host:
            continue
        declared = source.get("project")
        if isinstance(declared, str) and declared.strip("/").lower() == project:
            return source, int(match.group("ref"))
    return None


# ---------------------------------------------------------------------------
# Finding program links in work
# ---------------------------------------------------------------------------


def find_program_refs(text: str, tracker_repo: str, tracker_host: str = "github.com") -> set[tuple[str, str]]:
    """Every program link in ``text``: ``("issue", "<n>")`` for a tracker
    issue, ``("item", "<id>")`` for a status-page anchor on a ``Program:``
    line. Empty when the text names nothing — the caller reports that, it
    does not guess."""
    if not text or "/" not in tracker_repo:
        return set()
    owner, name = tracker_repo.split("/", 1)
    refs: set[tuple[str, str]] = set()

    short = re.compile(
        rf"(?<![\w./-])(?:{re.escape(owner)}/)?{re.escape(name)}#(\d+)\b", re.IGNORECASE
    )
    refs.update(("issue", n) for n in short.findall(text))

    full = re.compile(
        rf"https?://{re.escape(tracker_host)}/{re.escape(owner)}/{re.escape(name)}/issues/(\d+)\b",
        re.IGNORECASE,
    )
    refs.update(("issue", n) for n in full.findall(text))

    for line in text.splitlines():
        match = re.match(r"^\s*Program\s*:\s*<?(\S+?)>?\s*$", line, re.IGNORECASE)
        if match:
            anchor = re.search(r"#item-([\w.-]+)$", match.group(1))
            if anchor:
                refs.add(("item", anchor.group(1)))
    return refs


def _item_index(raw: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    by_id: dict[str, dict[str, Any]] = {}
    by_issue: dict[str, dict[str, Any]] = {}
    for item in raw.get("schedule", []):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        by_id[item["id"]] = item
        if item.get("issue") is not None:
            by_issue[str(item["issue"])] = item
    return by_id, by_issue


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _recent(item: WorkItem | dict[str, Any], now: datetime, window_days: int) -> bool:
    stamp = item.updated_at if isinstance(item, WorkItem) else item.get("updated_at")
    when = _parse_ts(stamp)
    return when is not None and now - when <= timedelta(days=window_days)


def roll_up(
    raw: dict[str, Any],
    work: list[WorkItem],
    *,
    tracker_repo: str,
    tracker_host: str = "github.com",
    now: datetime,
    window_days: int = 14,
    prior_findings: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Attach linked work to program items as evidence (mutating ``raw``)
    and return the ``unlinked_work`` findings.

    Evidence accumulates across cursor deltas: an observed work item first
    leaves every item's evidence (its links may have changed), then joins
    the item(s) it names now. A prior ``unlinked_work`` finding stands until
    its work is observed linked, or ages out of the window.
    """
    by_id, by_issue = _item_index(raw)
    observed = {w.key for w in work}
    # A link can run either way: work names the item, or the item names the work.
    named_by: dict[str, list[dict[str, Any]]] = {}
    for item in by_id.values():
        tracker = item.get("tracker") if isinstance(item.get("tracker"), dict) else {}
        for key in tracker.get("work_links") or []:
            if isinstance(key, str) and item not in named_by.setdefault(key.lower(), []):
                named_by[key.lower()].append(item)

    for item in by_id.values():
        evidence = item.get("evidence")
        if isinstance(evidence, dict) and isinstance(evidence.get("work"), list):
            kept = [e for e in evidence["work"] if f"{e.get('repo')}#{e.get('number')}" not in observed]
            if len(kept) != len(evidence["work"]):
                evidence["work"] = kept

    findings: list[dict[str, Any]] = []
    unlinked: list[WorkItem] = []
    for w in work:
        if w.kind == "pr" and w.state == "closed":
            continue  # abandoned — neither evidence nor unlinked work
        targets: list[dict[str, Any]] = list(named_by.get(w.key.lower(), []))
        unbound: list[str] = []
        for kind, value in sorted(find_program_refs(w.text, tracker_repo, tracker_host)):
            target = by_issue.get(value) if kind == "issue" else by_id.get(value)
            if target is None:
                unbound.append(f"{kind} {value}")
            elif target not in targets:
                targets.append(target)
        if targets:
            if w.is_evidence:
                for target in targets:
                    evidence = target.setdefault("evidence", {})
                    entries = evidence.setdefault("work", [])
                    entries.append(w.evidence_entry())
                    entries.sort(key=lambda e: (str(e.get("repo")), str(e.get("kind")), str(e.get("number"))))
            continue
        if not _recent(w, now, window_days):
            continue
        detail = (
            f"{'pull request' if w.kind == 'pr' else 'issue'} {w.key} names "
            + (f"{', '.join(unbound)}, which no program item tracks" if unbound else "no program item")
        )
        findings.append(
            {
                "kind": "unlinked_work",
                "subject": w.key,
                "detail": detail,
                "url": w.url,
                "updated_at": w.updated_at,
            }
        )
        unlinked.append(w)

    fresh = {f["subject"] for f in findings}
    for prior in prior_findings or []:
        if not isinstance(prior, dict) or prior.get("kind") != "unlinked_work":
            continue
        subject = str(prior.get("subject"))
        if subject in fresh or subject in observed:
            continue  # recomputed this cycle (still unlinked, or now linked)
        if _recent(prior, now, window_days):
            findings.append(prior)

    standing = {str(f["subject"]) for f in findings}
    proposals = propose_links(raw, unlinked, tracker_repo=tracker_repo)
    proposed = {p["subject"] for p in proposals}
    for prior in prior_findings or []:
        # A proposal for work not re-read this cycle stands while that work
        # stays unlinked; its text is not held, so it cannot be recomputed.
        if not isinstance(prior, dict) or prior.get("kind") != "link_proposed":
            continue
        subject = str(prior.get("subject"))
        if subject in standing and subject not in observed and subject not in proposed:
            proposals.append(prior)
    return findings + proposals


# ---------------------------------------------------------------------------
# Proposing a link for unlinked work (deterministic; nothing is written)
# ---------------------------------------------------------------------------

#: Signal weights. A proposal's confidence is their sum, capped at 1.
_W_CROSS_REF = 0.6
_W_SAME_REPO = 0.2
_W_MILESTONE = 0.3
_W_LABEL = 0.1
_W_LABELS_MAX = 0.2
_W_TITLE = 0.4  # scaled by the term overlap (Jaccard)

#: Title term overlap (Jaccard) below which titles are no evidence.
TITLE_OVERLAP_THRESHOLD = 0.3
#: The least confidence a proposal needs; the best candidate must also beat the next.
MIN_PROPOSAL_CONFIDENCE = 0.3

#: Labels too common to say which item a piece of work belongs to.
_GENERIC_LABELS = frozenset(
    {
        "bug", "enhancement", "documentation", "docs", "question", "duplicate", "invalid",
        "wontfix", "help wanted", "good first issue", "dependencies", "chore", "feature",
    }
)
_STOPWORDS = frozenset(
    {"with", "from", "into", "that", "this", "when", "then", "than", "them", "they", "have",
     "will", "should", "would", "could", "about", "after", "before", "over", "under", "make",
     "more", "most", "some", "only", "also", "each", "every", "what", "which", "where", "while"}
)
_TERM = re.compile(r"[a-z0-9]+")


def _terms(text: str) -> set[str]:
    return {t for t in _TERM.findall(text.lower()) if len(t) >= 4 and t not in _STOPWORDS}


def _item_terms(item: dict[str, Any]) -> set[str]:
    tracker = item.get("tracker") if isinstance(item.get("tracker"), dict) else {}
    parts = [item.get("label"), item.get("name"), tracker.get("title")]
    return _terms(" ".join(str(p) for p in parts if isinstance(p, str)))


def _work_refs(w: WorkItem) -> set[str]:
    """Other work this text points at, as ``<repo>#<n>`` keys."""
    refs: set[str] = set()
    for owner_repo, num in re.findall(r"(?<![\w.-])([\w.-]+/[\w.-]+)#(\d+)\b", w.text):
        refs.add(f"{owner_repo}#{num}".lower())
    for num in re.findall(r"(?<![\w/#])#(\d+)\b", w.text):
        refs.add(f"{w.repo}#{num}".lower())
    for owner_repo, num in re.findall(r"https?://[^/\s]+/([\w.-]+/[\w.-]+)/(?:issues|pull)/(\d+)\b", w.text):
        refs.add(f"{owner_repo}#{num}".lower())
    refs.discard(w.key.lower())
    return refs


def _level(confidence: float) -> str:
    return "high" if confidence >= 0.6 else "medium" if confidence >= 0.4 else "low"


def propose_links(
    raw: dict[str, Any], unlinked: list[WorkItem], *, tracker_repo: str
) -> list[dict[str, Any]]:
    """A ``link_proposed`` finding per unlinked work item whose evidence
    points at one program item more than any other.

    Evidence is deterministic and comes only from what this cycle already
    holds (no reads): a cross-reference to work that is already an item's
    evidence, or to the item's id; the same repository already linked to the
    item; a shared milestone; shared non-generic labels; title terms that
    overlap above :data:`TITLE_OVERLAP_THRESHOLD`. Nothing is written; the
    proposal waits for consent like every other finding.
    """
    by_id, _ = _item_index(raw)
    if not unlinked or not by_id:
        return []
    owner_of: dict[str, set[str]] = {}
    repos_of: dict[str, set[str]] = {}
    for item_id, item in by_id.items():
        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        for entry in evidence.get("work") or []:
            if not isinstance(entry, dict):
                continue
            owner_of.setdefault(f"{entry.get('repo')}#{entry.get('number')}".lower(), set()).add(item_id)
            repos_of.setdefault(str(entry.get("repo")).lower(), set()).add(item_id)
    terms = {item_id: _item_terms(item) for item_id, item in by_id.items()}

    proposals: list[dict[str, Any]] = []
    for w in unlinked:
        refs = _work_refs(w)
        lowered = w.text.lower()
        title = _terms(w.text.split("\n", 1)[0])
        w_labels = {label.lower() for label in w.labels} - _GENERIC_LABELS
        scored: list[tuple[float, str, list[dict[str, Any]]]] = []
        for item_id, item in by_id.items():
            signals: list[dict[str, Any]] = []
            via = sorted(r for r in refs if item_id in owner_of.get(r, ()))
            mentions_id = (
                len(item_id) >= 4
                and any(ch in item_id for ch in "-_0123456789")
                and re.search(rf"(?<![\w-]){re.escape(item_id.lower())}(?![\w-])", lowered)
            )
            if via or mentions_id:
                detail = f"references {', '.join(via)}, already evidence for {item_id}" if via else f"names {item_id}"
                signals.append({"signal": "cross_reference", "weight": _W_CROSS_REF, "detail": detail})
            if item_id in repos_of.get(w.repo.lower(), ()):
                signals.append(
                    {"signal": "same_repo", "weight": _W_SAME_REPO, "detail": f"{w.repo} already has work linked to {item_id}"}
                )
            tracker = item.get("tracker") if isinstance(item.get("tracker"), dict) else {}
            milestone = tracker.get("milestone")
            if w.milestone and isinstance(milestone, str) and milestone.lower() == w.milestone.lower():
                signals.append({"signal": "milestone", "weight": _W_MILESTONE, "detail": f"same milestone {milestone!r}"})
            shared = sorted(w_labels & {str(label).lower() for label in tracker.get("labels") or []})
            if shared:
                signals.append(
                    {"signal": "labels", "weight": min(_W_LABELS_MAX, _W_LABEL * len(shared)), "shared": shared,
                     "detail": f"shared labels {', '.join(shared)}"}
                )
            union = title | terms[item_id]
            overlap = len(title & terms[item_id]) / len(union) if union else 0.0
            if overlap >= TITLE_OVERLAP_THRESHOLD:
                common = sorted(title & terms[item_id])
                signals.append(
                    {"signal": "title_terms", "weight": round(_W_TITLE * overlap, 2), "terms": common,
                     "detail": f"title shares {', '.join(common)} ({overlap:.2f} overlap)"}
                )
            if signals:
                scored.append((round(min(1.0, sum(s["weight"] for s in signals)), 2), item_id, signals))
        scored.sort(key=lambda row: (-row[0], row[1]))
        if not scored or scored[0][0] < MIN_PROPOSAL_CONFIDENCE:
            continue
        if len(scored) > 1 and scored[1][0] == scored[0][0]:
            continue  # tied: the evidence does not say which item
        confidence, item_id, signals = scored[0]
        issue = by_id[item_id].get("issue")
        proposals.append(
            {
                "kind": "link_proposed",
                "subject": w.key,
                "item": item_id,
                "confidence": confidence,
                "level": _level(confidence),
                "evidence": signals,
                "detail": f"{w.key} most likely belongs to {item_id} ({_level(confidence)} confidence); "
                "proposed only, nothing is linked until someone agrees",
                "url": w.url,
                "suggested_ref": f"{tracker_repo}#{issue}" if issue is not None else None,
                "owner": by_id[item_id].get("owner"),
            }
        )
    return proposals


# ---------------------------------------------------------------------------
# Progress from evidence, and the update policy
# ---------------------------------------------------------------------------


def _cites(entries: list[dict[str, Any]]) -> list[str]:
    return [e["url"] for e in entries if isinstance(e.get("url"), str) and e["url"]][:_MAX_CITES]


def assess(item: dict[str, Any]) -> dict[str, Any]:
    """What the evidence says about one item: a ``pct`` and a ``delivery``
    (each ``None`` when the evidence says nothing), the ``basis`` sentence,
    the ``evidence`` URLs it rests on, and ``done_at`` when it is done."""
    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    work = [e for e in (evidence.get("work") or []) if isinstance(e, dict)]
    issues = [e for e in work if e.get("kind") == "issue"]
    closed = [e for e in issues if e.get("state") == "closed"]
    merged = [e for e in work if e.get("kind") == "pr" and e.get("state") == "merged"]
    tracker = item.get("tracker") if isinstance(item.get("tracker"), dict) else {}
    own_closed = tracker.get("state") == "closed"

    parts: list[str] = []
    if issues:
        parts.append(f"{len(closed)} of {len(issues)} linked issues closed")
    if merged:
        parts.append(f"{len(merged)} merged pull request{'s' if len(merged) != 1 else ''}")
    cites = _cites(closed + merged + [e for e in issues if e not in closed])

    if own_closed:
        done_stamps = sorted(str(e["at"]) for e in closed + merged if e.get("at"))
        return {
            "pct": 100,
            "delivery": "done",
            "basis": "; ".join([f"program issue #{item.get('issue')} is closed", *parts]),
            "evidence": cites,
            "done_at": done_stamps[-1] if done_stamps else None,
        }
    if not issues and not merged:
        return {"pct": None, "delivery": None, "basis": "", "evidence": [], "done_at": None}

    pct = None
    if issues:
        # Never 100 while the program issue is open: closing it is the owner's call.
        pct = min(95, (100 * len(closed)) // len(issues))
    delivery = "in_progress" if (closed or merged) else "not_started"
    return {"pct": pct, "delivery": delivery, "basis": "; ".join(parts), "evidence": cites, "done_at": None}


def _proposal(kind: str, item: dict[str, Any], field: str, current: Any, proposed: Any, basis: str, cites: list[str], why: str) -> dict[str, Any]:
    return {
        "kind": kind,
        "subject": f"{item.get('id')}:{field}",
        "detail": f"{item.get('id')} {field}: evidence suggests {proposed!r} (now {current!r}); {why}",
        "item": item.get("id"),
        "field": field,
        "current": current,
        "proposed": proposed,
        "basis": basis,
        "evidence": cites,
        "owner": item.get("owner"),
    }


def apply_evidence(raw: dict[str, Any], *, mode: str, ts: str) -> list[dict[str, Any]]:
    """Apply (or propose) evidence-derived percent and delivery, and propose
    dates, for every schedule item. Mutates ``raw``; returns the proposal
    findings.

    Under ``apply`` a percent only rises and delivery only steps forward;
    anything else is proposed. Under ``propose`` everything is proposed. An
    applied change records ``progress_basis[field]`` = the basis sentence,
    the evidence URLs and the time — the citation every write carries.
    Dates (``date`` / ``end``) are only ever proposed, for the owner.
    """
    mode = mode if mode in EVIDENCE_MODES else "propose"
    findings: list[dict[str, Any]] = []
    for item in raw.get("schedule", []):
        if not isinstance(item, dict):
            continue
        verdict = assess(item)
        basis, cites = verdict["basis"], verdict["evidence"]

        for field, rank in (("pct", lambda v: v), ("delivery", lambda v: _DELIVERY_RANK.get(v, -1))):
            proposed = verdict[field]
            current = item.get(field)
            if proposed is None or proposed == current:
                continue
            forward = current is None or rank(proposed) > rank(current)
            if mode == "apply" and forward:
                item[field] = proposed
                item.setdefault("progress_basis", {})[field] = {
                    "value": proposed,
                    "basis": basis,
                    "evidence": cites,
                    "at": ts,
                }
            else:
                why = "the policy proposes only" if mode == "propose" else "a step backward is never applied"
                findings.append(_proposal("progress_proposal", item, field, current, proposed, basis, cites, why))

        done_at = verdict.get("done_at")
        if verdict["delivery"] == "done" and done_at:
            field = "end" if "end" in item else "date" if "date" in item else None
            committed = item.get(field) if field else None
            finished = str(done_at)[:10]
            if committed and committed != finished:
                findings.append(
                    _proposal(
                        "date_proposal", item, field, committed, finished, basis, cites,
                        "dates are the owner's to change; proposed only",
                    )
                )
    return findings


__all__ = [
    "WORK_FINDING_KINDS",
    "DELIVERY_VALUES",
    "EVIDENCE_MODES",
    "WorkItem",
    "parse_tracker_crosslink",
    "declared_tracker_sources",
    "resolve_crosslink",
    "find_program_refs",
    "roll_up",
    "propose_links",
    "parse_work_links",
    "parse_body_structure",
    "body_structure_fields",
    "is_unfilled",
    "TITLE_OVERLAP_THRESHOLD",
    "MIN_PROPOSAL_CONFIDENCE",
    "assess",
    "apply_evidence",
]
