# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Shadow mode: record what the verifier says next to each landing decision, change nothing.

For every pull request the lander evaluates, one row: "would have skipped the
owner's GitHub review: yes / no / uncertain". The row goes beside the lander's own
event log (``<state>/agents/rivet/agreement/shadow.jsonl``). Nothing reads it to
decide: the lander's verdict, its holds and its landings are exactly what they were.
The replay compares the rows with what the owner then did on GitHub, which is the
evidence ADR-285 asks for before any autonomy is switched on.
"""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from . import paths, store
from .paths import default_projects_root
from .verify import Subject, Verdict, verify

KEY = "would_have_skipped_the_owners_github_review"
#: Pull requests the verifier would have let skip, with a known owner outcome, before "ready" can be said.
MINIMUM_SAMPLE = 20
#: At most this share of those may have been changed or rejected afterwards.
MAX_FALSE_SKIP_SHARE = 0.05

_KNOWN = {"approved_unchanged", "changed_after", "rejected"}


def shadow_path(state_dir: Path | str | None = None) -> Path:
    return paths.agreement_dir(state_dir) / "shadow.jsonl"


def subject_from_pull(pull: object, repo: str) -> Subject:
    return Subject(
        repo=repo,
        number=int(getattr(pull, "number", 0)),
        head_sha=str(getattr(pull, "head_sha", "")),
        head_ref=str(getattr(pull, "head_ref", "")),
        title=str(getattr(pull, "title", "")),
        labels=tuple(str(x) for x in (getattr(pull, "labels", ()) or ())),
        files=tuple(sorted(set(str(x) for x in (getattr(pull, "files", ()) or ())))),
        first_commit_at=None,
    )


def read_shadow(state_dir: Path | str | None = None) -> list[dict]:
    path = shadow_path(state_dir)
    if not path.is_file():
        return []
    rows = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rows.append(json.loads(raw))
        except ValueError:
            continue
    return rows


def row_for(subject: Subject, verdict: Verdict, now: datetime) -> dict:
    return {
        "time": now.isoformat(),
        "repo": subject.repo,
        "pr": subject.number,
        "head_sha": subject.head_sha,
        "decision": verdict.decision,
        KEY: verdict.skips_review,
        "record_id": verdict.record_id,
        "reasons": list(verdict.reasons),
        "detail": verdict.detail,
        "session_id": verdict.session_id,
        "named_actions": list(verdict.named_actions),
        "live_effect": "none",
    }


def observe(
    pull: object,
    *,
    repo: str,
    state_dir: Path | str | None,
    now: datetime | None = None,
) -> dict | None:
    """Record the verdict for one pull request. Returns the row, or None when there is nothing to say.

    Nothing is written on a machine with no prompt log (a CI runner, a checkout the hook
    never ran on). The same head with the same decision is recorded once.
    """
    if not store.prompt_log_path(state_dir).is_file():
        return None
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    subject = subject_from_pull(pull, repo)
    verdict = verify(
        subject, state_dir=state_dir, projects_root=default_projects_root(), now=moment
    )
    row = row_for(subject, verdict, moment)
    for seen in read_shadow(state_dir):
        if (seen.get("repo"), seen.get("pr"), seen.get("head_sha"), seen.get("decision")) == (
            repo,
            subject.number,
            subject.head_sha,
            verdict.decision,
        ):
            return None
    path = shadow_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
    return row


# --- what the owner then did on GitHub --------------------------------------


def _same(left: str, right: str) -> bool:
    return left.strip().lstrip("@").lower() == right.strip().lstrip("@").lower()


def owner_outcome(row: dict, gh: dict, *, owners: tuple[str, ...]) -> str:
    """One of approved_unchanged, changed_after, rejected, pending, merged_without_owner_review."""
    state = str(gh.get("state", "")).upper()
    reviews = [
        r for r in gh.get("reviews", ()) if any(_same(str(r.get("login", "")), o) for o in owners)
    ]
    if reviews and str(reviews[-1].get("state", "")).upper() == "CHANGES_REQUESTED":
        return "rejected"
    if state == "CLOSED":
        return "rejected"
    approvals = [r for r in reviews if str(r.get("state", "")).upper() == "APPROVED"]
    if approvals:
        head = str(row.get("head_sha", ""))
        if str(approvals[-1].get("commit", "")) == head and str(gh.get("head_oid", "")) == head:
            return "approved_unchanged"
        return "changed_after"
    return "merged_without_owner_review" if state == "MERGED" else "pending"


def fetch_github(repo: str, number: int) -> dict:
    """The owner-relevant facts of one pull request, read with the gh CLI."""
    done = subprocess.run(
        ["gh", "pr", "view", str(number), "-R", repo, "--json", "state,headRefOid,reviews"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    data = json.loads(done.stdout)
    return {
        "state": data.get("state", ""),
        "head_oid": data.get("headRefOid", ""),
        "reviews": [
            {
                "login": (r.get("author") or {}).get("login", ""),
                "state": r.get("state", ""),
                "commit": (r.get("commit") or {}).get("oid", ""),
            }
            for r in data.get("reviews", ())
        ],
    }


def compare(
    rows: list[dict],
    fetch: Callable[[str, int], dict],
    *,
    owners: tuple[str, ...],
) -> dict:
    """Each verdict counted against what the owner did. The latest row of each pull request counts."""
    latest: dict[tuple[str, int], dict] = {}
    for row in sorted(rows, key=lambda r: str(r.get("time", ""))):
        latest[(str(row.get("repo", "")), int(row.get("pr", 0)))] = row
    table: dict[str, Counter] = {}
    false_skips: list[dict] = []
    unreadable: list[dict] = []
    for (repo, number), row in latest.items():
        try:
            outcome = owner_outcome(row, fetch(repo, number), owners=owners)
        except Exception as exc:  # noqa: BLE001 - one unreadable pull request must not hide the rest
            unreadable.append({"repo": repo, "pr": number, "error": str(exc)[:120]})
            continue
        table.setdefault(str(row.get(KEY, "uncertain")), Counter())[outcome] += 1
        if row.get(KEY) == "yes" and outcome in {"changed_after", "rejected"}:
            false_skips.append({"repo": repo, "pr": number, "outcome": outcome})
    yes_known = sum(n for o, n in table.get("yes", Counter()).items() if o in _KNOWN)
    share = (len(false_skips) / yes_known) if yes_known else 0.0
    ready = yes_known >= MINIMUM_SAMPLE and share <= MAX_FALSE_SKIP_SHARE
    if yes_known:
        sentence = (
            f"{len(false_skips)} of {yes_known} pull requests the verifier would have let skip review "
            "were changed or rejected on GitHub afterwards."
        )
    else:
        sentence = (
            "No pull request the verifier would have let skip review has a known owner outcome yet."
        )
    return {
        "would_skip": {k: dict(v) for k, v in table.items()},
        "false_skips": false_skips,
        "unreadable": unreadable,
        "known_agreed_outcomes": yes_known,
        "minimum_sample": MINIMUM_SAMPLE,
        "false_skip_share": round(share, 3),
        "ready_to_go_live": ready,
        "sentence": sentence,
    }
