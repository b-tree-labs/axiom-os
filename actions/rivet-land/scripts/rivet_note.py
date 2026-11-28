# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The text of RIVET's one note on a pull request: honest, current, and deep-linked.

Pure functions over a ``Facts`` record, so every claim is testable without the forge. A
note says "RIVET is handling this" only when RIVET did something in the last 24 hours;
it says when it was written; an open note older than two hours is rewritten even when
its verdict is unchanged; a merged or closed pull request's note is finalised once; and
every check and next step is a link to the exact page. Standard library only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

#: An open note older than this is rewritten even when nothing about the verdict changed.
STALE_AFTER = timedelta(hours=2)
#: A RIVET action older than this is not evidence that RIVET is handling the pull request.
RECENT_ACTION = timedelta(hours=24)

_RUN = re.compile(r"^(https://github\.com/[^/]+/[^/]+/actions/runs/\d+)")


@dataclass(frozen=True)
class Check:
    name: str
    url: str


@dataclass(frozen=True)
class Action:
    at: datetime
    what: str
    url: str = ""


@dataclass(frozen=True)
class Facts:
    repo: str
    number: int
    now: datetime
    state: str = "open"  # open | merged | closed
    run_url: str = ""
    failing: list = field(default_factory=list)
    passed: int = 0
    running: int = 0
    last_action: Action | None = None
    merged_at: datetime | None = None
    merged_by: str = ""
    merge_sha: str = ""
    #: Set when the required checks have no run that counts (#2354): the plain sentence and
    #: the exact fix, from the lander's report.
    tests_not_run: str = ""
    tests_not_run_fix: str = ""


def terminal_key(state: str) -> str:
    return f"final-{state}"


def honest_state(group: str, facts: Facts) -> str:
    """The verdict's state, except that "RIVET is handling" needs evidence."""
    if group != "rivet":
        return group
    act = facts.last_action
    if act is not None and facts.now - act.at <= RECENT_ACTION:
        return "rivet"
    return "stalled"


def needs_rewrite(*, existing_key: str, new_key: str, updated_at: datetime, now: datetime, state: str) -> bool:
    """Whether to write the note again. A finished note is written once; an open one
    is rewritten when its verdict changed or when it is more than two hours old."""
    if state in {"merged", "closed"}:
        return existing_key != terminal_key(state)
    if existing_key != new_key:
        return True
    return now - updated_at > STALE_AFTER


def ago(then: datetime, now: datetime) -> str:
    seconds = max(int((now - then).total_seconds()), 0)
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{round(seconds / 60)} min ago"
    if seconds < 172800:
        return f"{round(seconds / 3600)} h ago"
    return f"{round(seconds / 86400)} days ago"


def stamp(when: datetime) -> str:
    return when.strftime("%Y-%m-%d %H:%M UTC")


def _pr(facts: Facts, number: int | str = "") -> str:
    base = f"https://github.com/{facts.repo}/pull/"
    return f"{base}{number or facts.number}"


def strip_link(facts: Facts) -> str:
    """Where the status image goes: the first failing job, else the checks tab."""
    if facts.failing:
        return facts.failing[0].url
    return f"{_pr(facts)}/checks"


def _run_of(facts: Facts) -> str:
    for check in facts.failing:
        found = _RUN.match(check.url or "")
        if found:
            return found.group(1)
    return ""


def _link_prs(text: str, facts: Facts) -> str:
    return re.sub(r"(?<![\w/\[])#(\d+)\b", lambda m: f"[#{m.group(1)}]({_pr(facts, m.group(1))})", text)


def _failing_lines(facts: Facts) -> list[str]:
    if not facts.failing:
        return []
    links = ", ".join(f"[{c.name}]({c.url})" for c in facts.failing)
    return [f"**Failing:** {links}", ""]


def _not_run_lines(facts: Facts) -> list[str]:
    """A caution above everything, so a person approving sees that no test ran."""
    if not facts.tests_not_run:
        return []
    fix = f" **Fix:** {facts.tests_not_run_fix}" if facts.tests_not_run_fix else ""
    said = facts.tests_not_run.removeprefix("tests: not run. ")
    return ["> [!CAUTION]", f"> **tests: not run.** {said}{fix}", ""]


def _footer(facts: Facts) -> str:
    bits = [f"as of {stamp(facts.now)}"]
    if facts.run_url:
        bits.append(f"[lander run]({facts.run_url})")
    return "<sub>" + " · ".join(bits) + "</sub>"


def _state_text(state: str, verdict, facts: Facts) -> tuple[str, str, list[str]]:
    """(headline, who acts next, next-step lines)."""
    run = _run_of(facts)
    merge_box = f"[the merge box]({_pr(facts)}#partial-pull-merging)"
    rerun = f"[re-run the failed jobs]({run})" if run else f"[open the checks]({_pr(facts)}/checks)"
    if state == "stalled":
        return (
            "A check is failing and nothing is repairing it.",
            "the author",
            [f"The author can {rerun}, or update the branch in {merge_box}."],
        )
    if state == "rivet":
        act = facts.last_action
        what = f"RIVET {act.what} {ago(act.at, facts.now)}"
        what = f"[{what}]({act.url})" if act.url else what
        return (f"{what}. Waiting on the result.", "RIVET",
                [f"Nothing yet. If it stays red, the author can {rerun}."])
    if state == "lands":
        return ("Ready: RIVET merges this on its next pass.", "nobody",
                [f"Nothing. To merge sooner, use {merge_box}."])
    if state == "checks":
        return ("Its checks are still running.", "nobody",
                [f"Nothing yet. [Watch the checks]({_pr(facts)}/checks)."])
    return ("", "", [])


def render(verdict, facts: Facts) -> str:
    """The note's text after its marker: verdict, evidence, links, and when it was written."""
    if facts.state in {"merged", "closed"}:
        if facts.state == "merged":
            sha = facts.merge_sha
            at = f" {ago(facts.merged_at, facts.now)}" if facts.merged_at else ""
            by = f" by {facts.merged_by}" if facts.merged_by else ""
            where = f" as [`{sha[:8]}`](https://github.com/{facts.repo}/commit/{sha})" if sha else ""
            head = f"**Merged**{at}{by}{where}. Nothing left for RIVET to do."
        else:
            head = "**Closed** without merging. Nothing left for RIVET to do."
        return "\n".join([head, "", _footer(facts)]) + "\n"

    state = honest_state(verdict.state, facts)
    if state in {"stalled", "rivet", "lands", "checks"}:
        headline, who, steps = _state_text(state, verdict, facts)
        # The verdict's own reason ("RIVET is trying to repair it") is a claim; say only what is true.
        why = ["RIVET has taken no action on this pull request in the last 24 hours (no fix attempt, no branch update)."] \
            if state == "stalled" else ([] if state == "rivet" else [w for w in verdict.why])
    else:  # person, author, superseded: the verdict's own words
        headline = getattr(verdict, "headline", "") or ""
        who, steps, why = verdict.who, list(verdict.next_steps), [w for w in verdict.why]
    headline = re.sub(r"^RIVET:\s*", "", headline)

    lines = _not_run_lines(facts) + [f"**{headline}**", ""]
    lines += _failing_lines(facts)
    if why:
        lines += ["**Why:** " + _link_prs(" ".join(why), facts), ""]
    if who not in {"nobody", "RIVET", ""}:
        lines += [f"**Who acts next:** {who}", ""]
    if len(steps) == 1:
        lines += ["**What to do:** " + _link_prs(steps[0], facts)]
    else:
        lines += ["**What to do:**"] + [f"- {_link_prs(s, facts)}" for s in steps]
    lines += ["", _footer(facts)]
    return "\n".join(lines) + "\n"
