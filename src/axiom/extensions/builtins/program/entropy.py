# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Program entropy: taxonomy checks, the coherence score, and consolidation proposals.

A tracker loses coherence a little at a time: an item filed without a lane, an
epic nobody attached to a program, a task whose epic was never named, a title
left as a placeholder, an item nobody has touched in months, work landing in a
repository no item names. None of these is an error on its own, and together
they erode discoverability and the certainty of what the program reports.
This module is the domain-free judgement CLERK uses to fight that (prd-program
R27). It performs no I/O and writes nothing: it reads a loaded program and
returns findings, a score and proposals.

**The taxonomy is Program -> Lane -> Epic -> Task, read from labels.** A lane is
not an item: it is a ``lane:<slug>`` label on items (and a ``lanes[]`` entry in
the data file). An ``epic`` label marks an item as an epic, a direct child of a
program; ``program:<slug>`` labels name the program an item belongs to. A task
names its epic either with a ``Parent:`` or ``Epic:`` line in its body or by
being listed in an epic's task list. The prefixes and the epic label are
configurable under ``program.taxonomy``.

**Precision over recall.** Every proposal states a fact the data shows, carries
the evidence it rests on, and is only ever proposed. A guess that might be
wrong is left out: a duplicate needs the same tracker issue or the same
non-trivial title, a lane is suggested only when every piece of linked work
sits in repositories that map to that one lane, and unlinked work is attributed
only when exactly one ``program.repo_map`` rule claims its repository.

**Absent is absent.** A check that needs data a feeder did not capture (an
item's body lines, an activity time) is skipped for that item, never treated
as a failure.
"""

from __future__ import annotations

import fnmatch
import hashlib
import re
from datetime import datetime, timedelta
from typing import Any

from .model import ProgramData

#: Label conventions, overridable under ``program.taxonomy``.
DEFAULT_TAXONOMY: dict[str, Any] = {
    "lane_prefix": "lane:",
    "program_prefix": "program:",
    "epic_label": "epic",
}

#: Titles that say nothing about the work. Compared after normalization
#: (lowercase, punctuation removed), so only an exact match counts.
PLACEHOLDER_TITLES = frozenset(
    {
        "tbd", "todo", "to do", "untitled", "new issue", "new item", "placeholder", "title",
        "wip", "xxx", "test", "temp", "tmp", "draft", "fixme", "issue", "task", "epic", "item",
    }
)
#: A numbered placeholder (``Task 3``, ``Untitled 2``) or an unfilled template
#: field (``<title>``, ``{name}``, ``[TBD]``).
_PLACEHOLDER_RE = re.compile(
    r"^(?:(?:untitled|task|issue|item|epic|new issue|placeholder)\s*\d+|.*<\s*[a-z][a-z _-]*\s*>.*|.*\{\s*[a-z][a-z _-]*\s*\}.*|\[\s*tbd\s*\])$",
    re.IGNORECASE,
)

#: How long an open item may go without activity before it is stale.
DEFAULT_STALE_DAYS = 30

#: Taxonomy finding kinds, in report order.
TAXONOMY_KINDS = (
    "no_lane",
    "multiple_lanes",
    "epic_without_program",
    "task_without_epic",
    "missing_owner_line",
    "missing_done_when",
    "placeholder_title",
)

#: Proposal kinds, in digest order.
PROPOSAL_KINDS = (
    "duplicate_item",
    "stale_item",
    "missing_owner",
    "missing_lane",
    "rehome_lane",
    "link_unlinked_work",
    "broken_crosslink",
    "taxonomy_fix",
)

_BODY_FIELDS = ("owner", "done_when")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def taxonomy_config(program: dict[str, Any]) -> dict[str, Any]:
    """The label conventions: the defaults overlaid with ``program.taxonomy``."""
    declared = program.get("taxonomy") if isinstance(program.get("taxonomy"), dict) else {}
    config = dict(DEFAULT_TAXONOMY)
    for key in DEFAULT_TAXONOMY:
        value = declared.get(key)
        if isinstance(value, str) and value:
            config[key] = value
    extra = declared.get("placeholder_titles")
    config["placeholder_titles"] = sorted(
        PLACEHOLDER_TITLES | {_norm(t) for t in extra if isinstance(t, str)}
        if isinstance(extra, list)
        else PLACEHOLDER_TITLES
    )
    config["declared"] = bool(declared)
    return config


def stale_days(program: dict[str, Any]) -> int:
    """``program.entropy.stale_days``, else :data:`DEFAULT_STALE_DAYS`."""
    entropy = program.get("entropy") if isinstance(program.get("entropy"), dict) else {}
    value = entropy.get("stale_days")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else DEFAULT_STALE_DAYS


def repo_rules(program: dict[str, Any]) -> list[dict[str, Any]]:
    """The ``program.repo_map`` rules: ``{"repo": <owner/name or glob>, "lane"?, "epic"?}``."""
    raw = program.get("repo_map")
    if not isinstance(raw, list):
        return []
    return [r for r in raw if isinstance(r, dict) and isinstance(r.get("repo"), str) and r["repo"]]


def rules_for_repo(program: dict[str, Any], repo: str) -> list[dict[str, Any]]:
    """Every rule whose ``repo`` pattern matches ``repo`` (case-insensitive)."""
    name = repo.lower()
    return [r for r in repo_rules(program) if fnmatch.fnmatchcase(name, r["repo"].lower())]


def lane_for_repo(program: dict[str, Any], repo: str) -> str | None:
    """The one lane the matching rules agree on, or ``None`` (no rule, or rules disagree)."""
    lanes = {r["lane"] for r in rules_for_repo(program, repo) if isinstance(r.get("lane"), str)}
    return next(iter(lanes)) if len(lanes) == 1 else None


def epic_for_repo(program: dict[str, Any], repo: str) -> str | None:
    """The one epic item the matching rules agree on, or ``None``."""
    epics = {r["epic"] for r in rules_for_repo(program, repo) if isinstance(r.get("epic"), str)}
    return next(iter(epics)) if len(epics) == 1 else None


def validate_config(program: dict[str, Any], lane_ids: list[str], errors: list[str]) -> None:
    """Shape-check ``taxonomy``, ``repo_map``, ``entropy`` and ``cadence`` (all optional)."""
    taxonomy = program.get("taxonomy")
    if taxonomy is not None:
        if not isinstance(taxonomy, dict):
            errors.append("program.taxonomy: must be an object when present")
        else:
            for key in DEFAULT_TAXONOMY:
                if key in taxonomy and not (isinstance(taxonomy[key], str) and taxonomy[key]):
                    errors.append(f"program.taxonomy.{key}: must be a non-empty string")
            titles = taxonomy.get("placeholder_titles")
            if titles is not None and not (isinstance(titles, list) and all(isinstance(t, str) for t in titles)):
                errors.append("program.taxonomy.placeholder_titles: must be a list of strings")
    repo_map = program.get("repo_map")
    if repo_map is not None:
        if not isinstance(repo_map, list):
            errors.append("program.repo_map: must be a list of rules when present")
        else:
            for i, rule in enumerate(repo_map):
                where = f"program.repo_map[{i}]"
                if not isinstance(rule, dict) or not isinstance(rule.get("repo"), str) or "/" not in rule["repo"]:
                    errors.append(f"{where}: must be an object whose repo is 'owner/name' (globs allowed)")
                    continue
                if rule.get("lane") is None and rule.get("epic") is None:
                    errors.append(f"{where}: names neither a lane nor an epic, so it attributes nothing")
                lane = rule.get("lane")
                if lane is not None and lane not in lane_ids:
                    errors.append(f"{where}.lane: unknown lane {lane!r}")
                epic = rule.get("epic")
                if epic is not None and not (isinstance(epic, str) and epic):
                    errors.append(f"{where}.epic: must be a schedule item id")
    entropy = program.get("entropy")
    if entropy is not None:
        if not isinstance(entropy, dict):
            errors.append("program.entropy: must be an object when present")
        else:
            days = entropy.get("stale_days")
            if days is not None and not (isinstance(days, int) and not isinstance(days, bool) and days > 0):
                errors.append("program.entropy.stale_days: must be a positive integer")
    cadence = program.get("cadence")
    if cadence is not None:
        if not isinstance(cadence, dict):
            errors.append("program.cadence: must be an object when present")
        else:
            minutes = cadence.get("sync_minutes")
            if minutes is not None and not (isinstance(minutes, int) and not isinstance(minutes, bool) and minutes >= 1):
                errors.append("program.cadence.sync_minutes: must be an integer of at least 1")
            for key in ("enabled", "consolidate_enabled"):
                if key in cadence and not isinstance(cadence[key], bool):
                    errors.append(f"program.cadence.{key}: must be true or false")
            schedule = cadence.get("consolidate")
            if schedule is not None and not (isinstance(schedule, str) and schedule.strip()):
                errors.append("program.cadence.consolidate: must be a schedule string (cron, @weekly, ...)")


# ---------------------------------------------------------------------------
# Reading an item
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def _tracker(item: dict[str, Any]) -> dict[str, Any]:
    tracker = item.get("tracker")
    return tracker if isinstance(tracker, dict) else {}


def item_labels(item: dict[str, Any]) -> list[str]:
    """The item's labels: the tracker's, plus any the data file states."""
    labels = [label for label in _tracker(item).get("labels") or [] if isinstance(label, str)]
    own = item.get("labels")
    if isinstance(own, list):
        labels.extend(label for label in own if isinstance(label, str) and label not in labels)
    return labels


def _prefixed(labels: list[str], prefix: str) -> list[str]:
    p = prefix.lower()
    return sorted({label[len(prefix):].strip() for label in labels if label.lower().startswith(p) and label[len(prefix):].strip()})


def lanes_of(item: dict[str, Any], tax: dict[str, Any]) -> list[str]:
    """Every lane the item claims: its ``lane:<slug>`` labels and its data-file ``lane``."""
    lanes = set(_prefixed(item_labels(item), tax["lane_prefix"]))
    if isinstance(item.get("lane"), str) and item["lane"]:
        lanes.add(item["lane"])
    return sorted(lanes)


def programs_of(item: dict[str, Any], tax: dict[str, Any]) -> list[str]:
    return _prefixed(item_labels(item), tax["program_prefix"])


def is_epic(item: dict[str, Any], tax: dict[str, Any]) -> bool:
    epic = tax["epic_label"].lower()
    return any(label.lower() == epic for label in item_labels(item))


def is_open(item: dict[str, Any]) -> bool:
    """Open unless the tracker says closed or the evidence says delivered."""
    return _tracker(item).get("state") not in ("closed",) and item.get("delivery") != "done"


def title_of(item: dict[str, Any]) -> str:
    title = _tracker(item).get("title")
    if isinstance(title, str) and title.strip():
        return title
    return str(item.get("label") or "")


def is_placeholder(title: str, tax: dict[str, Any]) -> bool:
    norm = _norm(title)
    if not norm:
        return True
    return norm in set(tax["placeholder_titles"]) or bool(_PLACEHOLDER_RE.match(title.strip()))


def _ref(value: Any) -> str | None:
    return str(value) if value is not None and str(value) != "" else None


def _parents(data: ProgramData, tax: dict[str, Any]) -> dict[str, str]:
    """Item id -> its epic's issue ref: from the item's own ``Parent:``/``Epic:``
    line, else from an epic's task list that names the item's issue."""
    listed: dict[str, str] = {}
    for item in data.schedule:
        if isinstance(item, dict) and is_epic(item, tax) and item.get("issue") is not None:
            for child in _tracker(item).get("children") or []:
                listed.setdefault(str(child), str(item["issue"]))
    parents: dict[str, str] = {}
    for item in data.schedule:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        own = _ref(_tracker(item).get("parent"))
        via_list = listed.get(str(item.get("issue"))) if item.get("issue") is not None else None
        if own or via_list:
            parents[item["id"]] = own or via_list  # type: ignore[assignment]
    return parents


def _by_issue(data: ProgramData) -> dict[str, dict[str, Any]]:
    return {str(i["issue"]): i for i in data.schedule if isinstance(i, dict) and i.get("issue") is not None}


def _hierarchy_in_use(data: ProgramData, tax: dict[str, Any]) -> bool:
    """Epic/program checks apply once a program uses the convention at all: it
    declares ``program.taxonomy``, or some item carries the epic label or a
    program label. A tracker that has never heard of epics is not told every
    item lacks one."""
    if tax.get("declared"):
        return True
    return any(
        isinstance(i, dict) and (is_epic(i, tax) or programs_of(i, tax)) for i in data.schedule
    )


# ---------------------------------------------------------------------------
# Taxonomy validation (wired into program.validate)
# ---------------------------------------------------------------------------


def _finding(kind: str, item: dict[str, Any], detail: str, **extra: Any) -> dict[str, Any]:
    out = {"kind": kind, "subject": item.get("id"), "detail": detail}
    if item.get("issue") is not None:
        out["issue"] = item["issue"]
    out.update(extra)
    return out


def taxonomy_findings(data: ProgramData) -> list[dict[str, Any]]:
    """Every open item that breaks the Program -> Lane -> Epic -> Task convention.

    Closed items are skipped (nothing to act on). Body-line checks run only on
    items whose body a feeder captured (``tracker.body_fields``).
    """
    tax = taxonomy_config(data.program)
    hierarchy = _hierarchy_in_use(data, tax)
    parents = _parents(data, tax)
    findings: list[dict[str, Any]] = []
    for item in data.schedule:
        if not isinstance(item, dict) or not is_open(item):
            continue
        labels = item_labels(item)
        lanes = lanes_of(item, tax)
        if not lanes:
            findings.append(_finding(
                "no_lane", item, f"names no lane (no {tax['lane_prefix']}<slug> label and no data-file lane)",
                labels=labels,
            ))
        elif len(lanes) > 1:
            findings.append(_finding(
                "multiple_lanes", item, f"claims {len(lanes)} lanes ({', '.join(lanes)}); an item belongs to one",
                lanes=lanes, labels=labels,
            ))
        if hierarchy:
            epic = is_epic(item, tax)
            programs = programs_of(item, tax)
            if epic and not programs:
                findings.append(_finding(
                    "epic_without_program", item,
                    f"carries the {tax['epic_label']!r} label but no {tax['program_prefix']}<slug> label",
                    labels=labels,
                ))
            # A program root is an item with a program label, no epic label and no
            # parent; anything else that is not an epic is a task and needs one.
            is_root = bool(programs) and not epic and item.get("id") not in parents
            if not epic and not is_root and item.get("id") not in parents:
                findings.append(_finding(
                    "task_without_epic", item,
                    "is neither an epic nor a program, and names no epic (no Parent:/Epic: line, "
                    "and no epic's task list includes it)",
                    labels=labels,
                ))
        fields = _tracker(item).get("body_fields")
        if isinstance(fields, list):
            if "owner" not in fields:
                findings.append(_finding("missing_owner_line", item, "body has no filled 'Owner:' line"))
            if "done_when" not in fields:
                findings.append(_finding("missing_done_when", item, "body has no filled 'Done when:' line"))
        title = title_of(item)
        if is_placeholder(title, tax):
            findings.append(_finding("placeholder_title", item, f"title {title!r} is a placeholder", title=title))
    order = {kind: i for i, kind in enumerate(TAXONOMY_KINDS)}
    return sorted(findings, key=lambda f: (order[f["kind"]], str(f["subject"])))


# ---------------------------------------------------------------------------
# Activity and evidence
# ---------------------------------------------------------------------------


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def last_activity(item: dict[str, Any]) -> datetime | None:
    """The newest activity time the data shows for an item, or ``None`` (unknown)."""
    stamps = [_parse_ts(_tracker(item).get("updated_at"))]
    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    stamps.extend(_parse_ts(e.get("at")) for e in evidence.get("work") or [] if isinstance(e, dict))
    basis = item.get("progress_basis") if isinstance(item.get("progress_basis"), dict) else {}
    stamps.extend(_parse_ts(b.get("at")) for b in basis.values() if isinstance(b, dict))
    known = [s for s in stamps if s is not None]
    return max(known) if known else None


def evidence_repos(item: dict[str, Any]) -> list[str]:
    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    repos = [str(e.get("repo")) for e in evidence.get("work") or [] if isinstance(e, dict) and e.get("repo")]
    for key in _tracker(item).get("work_links") or []:
        if isinstance(key, str) and "#" in key:
            repos.append(key.split("#", 1)[0])
    return repos


def has_evidence_link(item: dict[str, Any]) -> bool:
    """Linked work (evidence or a body link to it) or attributed activity."""
    return bool(evidence_repos(item)) or bool(item.get("activity"))


def _cite(item: dict[str, Any], data: ProgramData) -> list[str]:
    """Where to look: the item's tracker issue, and up to five evidence URLs."""
    cites: list[str] = []
    tracker = data.tracker()
    if item.get("issue") is not None and tracker.get("host"):
        repo = tracker.get("repo") or tracker.get("project_id")
        cites.append(f"{tracker['host']}/{repo}#{item['issue']}")
    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    cites.extend(str(e["url"]) for e in (evidence.get("work") or [])[:5] if isinstance(e, dict) and e.get("url"))
    return cites


def _capture_findings(data: ProgramData, *kinds: str) -> list[dict[str, Any]]:
    capture = data.raw.get("capture") if isinstance(data.raw.get("capture"), dict) else {}
    return [f for f in capture.get("findings") or [] if isinstance(f, dict) and f.get("kind") in kinds]


# ---------------------------------------------------------------------------
# Coherence (program.status --scope coherence)
# ---------------------------------------------------------------------------


def _program_of(item: dict[str, Any], tax: dict[str, Any], parents: dict[str, str],
                by_issue: dict[str, dict[str, Any]]) -> list[str]:
    own = programs_of(item, tax)
    if own:
        return own
    parent = parents.get(str(item.get("id")))
    if parent and parent in by_issue:
        return programs_of(by_issue[parent], tax)
    return []


def coherence(data: ProgramData, *, now: datetime, explain: bool = False) -> dict[str, Any]:
    """The coherence score of the program, and of each ``program:<slug>`` within it.

    ``score`` is the share of open items that have exactly one lane, an owner,
    and an evidence link. Beside it: how many open items have no owner, which
    are stale (no activity in ``program.entropy.stale_days``), how much recent
    repository work names no item (``unlinked_work``), and how many tracker
    issues are not on the schedule. Deterministic; with ``explain`` each count
    lists the items behind it.
    """
    tax = taxonomy_config(data.program)
    window = stale_days(data.program)
    parents = _parents(data, tax)
    by_issue = _by_issue(data)
    by_id = {i["id"]: i for i in data.schedule if isinstance(i, dict) and isinstance(i.get("id"), str)}

    def blank() -> dict[str, Any]:
        return {"open": [], "coherent": [], "no_lane": [], "no_owner": [], "no_evidence": [],
                "stale": [], "activity_unknown": [], "unlinked_work": []}

    overall = blank()
    groups: dict[str, dict[str, Any]] = {}
    cutoff = now - timedelta(days=window)
    for item in data.schedule:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not is_open(item):
            continue
        targets = [overall] + [groups.setdefault(p, blank()) for p in _program_of(item, tax, parents, by_issue)]
        lanes = lanes_of(item, tax)
        owned = bool(item.get("owner"))
        linked = has_evidence_link(item)
        seen = last_activity(item)
        for g in targets:
            g["open"].append(item["id"])
            if len(lanes) == 1 and owned and linked:
                g["coherent"].append(item["id"])
            if not lanes:
                g["no_lane"].append(item["id"])
            if not owned:
                g["no_owner"].append(item["id"])
            if not linked:
                g["no_evidence"].append(item["id"])
            if seen is None:
                g["activity_unknown"].append(item["id"])
            elif seen < cutoff:
                g["stale"].append(item["id"])

    for finding in _capture_findings(data, "unlinked_work"):
        subject = str(finding.get("subject"))
        overall["unlinked_work"].append(subject)
        epic = epic_for_repo(data.program, subject.split("#", 1)[0])
        if epic and epic in by_id:
            for p in _program_of(by_id[epic], tax, parents, by_issue):
                groups.setdefault(p, blank())["unlinked_work"].append(subject)
    untracked = [str(f.get("subject")) for f in _capture_findings(data, "untracked_issue")]

    def render(g: dict[str, Any]) -> dict[str, Any]:
        total = len(g["open"])
        out: dict[str, Any] = {
            "score": round(len(g["coherent"]) / total, 3) if total else None,
            "open_items": total,
            "coherent_items": len(g["coherent"]),
            "counts": {k: len(v) for k, v in g.items() if k not in ("open", "coherent")},
        }
        if explain:
            out["items"] = {k: sorted(v) for k, v in g.items()}
        return out

    result: dict[str, Any] = {
        "definition": (
            "score = share of open items with exactly one lane, an owner, and an evidence link "
            "(linked work or attributed activity); stale = open with no activity in the window"
        ),
        "stale_days": window,
        "overall": render(overall),
        "programs": {slug: render(g) for slug, g in sorted(groups.items())},
        "untracked_issues": len(untracked),
    }
    if explain:
        result["untracked_issue_refs"] = sorted(untracked)
    return result


# ---------------------------------------------------------------------------
# Consolidation proposals (program.consolidate)
# ---------------------------------------------------------------------------


def _proposal(kind: str, subject: str, action: str, detail: str, evidence: list[str], *,
              suggested: dict[str, Any] | None = None, apply: str | None = None,
              owner: Any = None) -> dict[str, Any]:
    pid = hashlib.sha256(f"{kind}|{subject}|{action}|{sorted((suggested or {}).items())}".encode()).hexdigest()[:12]
    return {
        "id": pid,
        "kind": kind,
        "subject": subject,
        "action": action,
        "detail": detail,
        "evidence": evidence,
        "suggested": suggested,
        "apply": apply,
        "owner": owner,
    }


def _single_lane_from_work(data: ProgramData, item: dict[str, Any]) -> tuple[str | None, list[str]]:
    """The one lane every evidence repository maps to (``None`` if any does not, or they differ)."""
    repos = sorted(set(evidence_repos(item)))
    if not repos:
        return None, []
    lanes = {lane_for_repo(data.program, repo) for repo in repos}
    if len(lanes) != 1 or None in lanes:
        return None, repos
    return next(iter(lanes)), repos


def proposals(data: ProgramData, *, now: datetime) -> list[dict[str, Any]]:
    """One consolidation pass: every change CLERK would suggest, with evidence.

    Nothing here is applied. Each proposal says what to do, the evidence it
    rests on, the suggested value when the data settles one, and the exact
    program verb that applies it when the change is the data file's to make.
    """
    tax = taxonomy_config(data.program)
    window = stale_days(data.program)
    out: list[dict[str, Any]] = []
    open_items = [i for i in data.schedule if isinstance(i, dict) and isinstance(i.get("id"), str) and is_open(i)]
    by_id = {i["id"]: i for i in data.schedule if isinstance(i, dict) and isinstance(i.get("id"), str)}

    # -- duplicates: the same tracker issue, or the same non-trivial title ----
    seen_issue: dict[str, list[dict[str, Any]]] = {}
    for item in data.schedule:
        if isinstance(item, dict) and item.get("issue") is not None:
            seen_issue.setdefault(str(item["issue"]), []).append(item)
    for ref, items in sorted(seen_issue.items()):
        if len(items) > 1:
            ids = sorted(str(i.get("id")) for i in items)
            out.append(_proposal(
                "duplicate_item", "+".join(ids), "merge",
                f"items {', '.join(ids)} are all bound to tracker issue {ref}; keep one",
                [f"issue {ref}"], suggested={"keep": ids[0], "remove": ids[1:]},
                apply=f"program item remove --id {ids[-1]}",
            ))
    by_title: dict[str, list[dict[str, Any]]] = {}
    for item in open_items:
        title = title_of(item)
        norm = _norm(title)
        if len(norm.split()) >= 2 and not is_placeholder(title, tax):
            by_title.setdefault(norm, []).append(item)
    for norm, items in sorted(by_title.items()):
        if len(items) > 1:
            ids = sorted(str(i["id"]) for i in items)
            if len({str(i.get("issue")) for i in items}) == 1 and items[0].get("issue") is not None:
                continue  # already reported as the same issue
            out.append(_proposal(
                "duplicate_item", "+".join(ids), "merge",
                f"open items {', '.join(ids)} have the same title {title_of(items[0])!r}",
                [c for i in items for c in _cite(i, data)][:6], suggested={"keep": ids[0], "remove": ids[1:]},
            ))

    # -- stale: open, and no activity in the window --------------------------
    cutoff = now - timedelta(days=window)
    for item in open_items:
        seen = last_activity(item)
        if seen is None or seen >= cutoff:
            continue
        days = (now - seen).days
        evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
        work = [e for e in evidence.get("work") or [] if isinstance(e, dict)]
        issues = [e for e in work if e.get("kind") == "issue"]
        delivered = bool(work) and all(e.get("state") in ("closed", "merged") for e in work) and (
            not issues or all(e.get("state") == "closed" for e in issues)
        )
        action = "close" if delivered else "refresh"
        why = (
            "all of its linked work is finished, so it may be done"
            if delivered
            else "confirm it is still planned and update it, or close it"
        )
        out.append(_proposal(
            "stale_item", str(item["id"]), action,
            f"{item['id']} has had no activity for {days} days (last {seen.date().isoformat()}); {why}",
            _cite(item, data), suggested={"last_activity": seen.isoformat(), "days": days},
            owner=item.get("owner"),
        ))

    # -- missing owner / missing lane / lane where the work is ---------------
    for item in open_items:
        lanes = lanes_of(item, tax)
        if not item.get("owner"):
            lead = data.lane_lead(lanes[0]) if len(lanes) == 1 else None
            out.append(_proposal(
                "missing_owner", str(item["id"]), "assign an owner",
                f"{item['id']} has no owner" + (f"; its lane lead is {lead}" if lead else ""),
                _cite(item, data), suggested={"owner": lead, "basis": "lane lead"} if lead else None,
                apply=f"program item reassign --id {item['id']} --owner {lead}" if lead else None,
            ))
        inferred, repos = _single_lane_from_work(data, item)
        if not lanes:
            out.append(_proposal(
                "missing_lane", str(item["id"]), "set a lane",
                f"{item['id']} has no lane"
                + (f"; all of its linked work is in {', '.join(repos)}, which program.repo_map puts in lane {inferred}" if inferred else ""),
                _cite(item, data), suggested={"lane": inferred, "repos": repos} if inferred else None,
                apply=f"program item edit --id {item['id']} --lane {inferred}" if inferred else None,
                owner=item.get("owner"),
            ))
        elif len(lanes) == 1 and inferred and inferred != lanes[0] and len(repos) >= 2:
            out.append(_proposal(
                "rehome_lane", str(item["id"]), "move to the lane where its work is",
                f"{item['id']} is in lane {lanes[0]}, but all of its linked work ({', '.join(repos)}) "
                f"is in repositories program.repo_map puts in lane {inferred}",
                _cite(item, data), suggested={"lane": inferred, "from": lanes[0], "repos": repos},
                apply=f"program item edit --id {item['id']} --lane {inferred}",
                owner=item.get("owner"),
            ))

    # -- unlinked work a repo_map rule attributes to one epic ----------------
    for finding in _capture_findings(data, "unlinked_work"):
        subject = str(finding.get("subject"))
        epic_id = epic_for_repo(data.program, subject.split("#", 1)[0])
        epic = by_id.get(epic_id) if epic_id else None
        if epic is None or not is_open(epic):
            continue
        tracker = data.tracker()
        ref = f"{tracker.get('repo')}#{epic['issue']}" if epic.get("issue") is not None and tracker.get("repo") else None
        out.append(_proposal(
            "link_unlinked_work", subject, "link to its epic",
            f"{subject} names no program item; program.repo_map attributes its repository to epic {epic_id}"
            + (f" (name {ref} in its body to link it)" if ref else ""),
            [str(finding.get("url") or subject)], suggested={"epic": epic_id, "ref": ref},
            owner=epic.get("owner"),
        ))

    # -- broken cross-links a feeder confirmed --------------------------------
    for finding in _capture_findings(data, "dead_link", "crosslink_unresolved"):
        if finding.get("kind") == "dead_link" and finding.get("verified") is False:
            continue
        subject = str(finding.get("subject"))
        evidence = [str(v) for v in (finding.get("url"), finding.get("ref")) if v is not None]
        out.append(_proposal(
            "broken_crosslink", subject, "repair or remove the link", str(finding.get("detail") or ""),
            evidence, suggested=finding.get("proposed_fix") if isinstance(finding.get("proposed_fix"), dict) else None,
        ))

    # -- the rest of the taxonomy (no_lane is missing_lane above) ------------
    for finding in taxonomy_findings(data):
        if finding["kind"] == "no_lane":
            continue
        item = by_id.get(str(finding["subject"])) or {}
        out.append(_proposal(
            "taxonomy_fix", f"{finding['subject']}:{finding['kind']}", finding["kind"].replace("_", " "),
            f"{finding['subject']} {finding['detail']}", _cite(item, data), owner=item.get("owner"),
        ))

    order = {kind: i for i, kind in enumerate(PROPOSAL_KINDS)}
    return sorted(out, key=lambda p: (order[p["kind"]], p["subject"]))


def digest_id(items: list[dict[str, Any]]) -> str:
    """A content id for a set of proposals: equal sets, equal ids, so an unchanged
    digest is never delivered twice."""
    return hashlib.sha256("|".join(sorted(p["id"] for p in items)).encode()).hexdigest()[:16]


__all__ = [
    "DEFAULT_STALE_DAYS",
    "DEFAULT_TAXONOMY",
    "PROPOSAL_KINDS",
    "TAXONOMY_KINDS",
    "coherence",
    "digest_id",
    "epic_for_repo",
    "has_evidence_link",
    "is_epic",
    "is_open",
    "is_placeholder",
    "item_labels",
    "lane_for_repo",
    "lanes_of",
    "last_activity",
    "programs_of",
    "proposals",
    "repo_rules",
    "rules_for_repo",
    "stale_days",
    "taxonomy_config",
    "taxonomy_findings",
    "validate_config",
]
