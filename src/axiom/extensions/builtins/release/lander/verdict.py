# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""One pull request's verdict in plain words: held or not, why, and who acts next (#2246).

The backlog report speaks in reason codes (``dependency``, ``try-cap``) because a
program reads it. A person reads the note on a pull request and the run summary,
so those say what the code means and what to do about it. Everything here reads
the report's JSON form (``BacklogReport.to_dict``), so the workflow's comment
script can use it without building the report again.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

#: The hidden marker on RIVET's one comment per pull request. The verdict key
#: rides inside it, so a later run can tell whether the note is still current.
MARKER = "<!-- rivet-lander:warn-only -->"
_KEY = re.compile(r"<!-- rivet-lander:warn-only(?: key=([0-9a-f]+))? -->")

_CATEGORY = {
    "dependency": "It changes dependency files (a manifest, a lock file or a Dockerfile), and a person approves dependency changes.",
    "workflow": "It changes CI workflow files, and a person approves CI changes.",
    "test-setup": "It changes test setup (conftest, pytest or tox settings, or CI config), and a person approves those.",
    "data": "It changes a migration, a schema or SQL, and a person approves data changes.",
    "security": "It touches security, authentication or secrets paths, and a person approves those.",
    "pricing": "It touches pricing or billing, and a person approves those.",
    "behavior": "It changes an agent behavior file, and a person approves those.",
}
_OTHER = {
    "draft": "It is a draft.",
    "conflict": "It conflicts with main.",
    "failing_checks": "A check is failing, and RIVET is trying to repair it.",
    "try-cap": "A check still fails after RIVET used its repair attempts.",
    "checks_unknown": "RIVET cannot read its checks yet.",
    "approval_reset": "Its approval was for an older commit, and new commits reset it.",
    "review_refused": "No reviewer on the roster may review a change from this author.",
}
_STEP = {
    "category": "A person with approval rights reviews the change and approves the pull request on GitHub. RIVET lands it on its next run once its checks are green.",
    "draft": "The author marks it ready for review when it is done.",
    "conflict": "The author merges main into the branch and resolves the conflict.",
    "try-cap": "A person reads the failing check and fixes it, or closes the pull request.",
    "approval_reset": "A person approves the current commit.",
    "review_refused": "A person reviews it, or adds a reviewer to the roster.",
}


def explain(reason: str) -> str:
    """One reason code as a sentence."""
    if reason in _CATEGORY:
        return _CATEGORY[reason]
    if reason in _OTHER:
        return _OTHER[reason]
    if reason.startswith("file_overlap:"):
        numbers = ", ".join(f"#{n}" for n in reason.split(":", 1)[1].split(",") if n)
        return f"It changes the same files as {numbers}, so RIVET lands them one at a time."
    if reason.startswith("waiting on "):
        return f"A review is requested from {reason[len('waiting on ') :]}."
    if reason.startswith("changes requested by "):
        return f"{reason[len('changes requested by ') :]} requested changes."
    return f'It touches paths this repository marks as "{reason}", and a person approves those.'


@dataclass(frozen=True)
class Verdict:
    number: int
    title: str
    #: ``lands``, ``checks``, ``rivet``, ``author``, ``person`` or ``superseded``.
    state: str
    headline: str
    why: tuple[str, ...]
    next_steps: tuple[str, ...]
    #: Who acts next: a login, ``the author``, ``a person with approval rights``,
    #: ``RIVET`` or ``nobody``.
    who: str
    draft: bool
    key: str


def _key(state: str, reasons: tuple[str, ...]) -> str:
    return hashlib.sha256((state + "|" + "|".join(sorted(reasons))).encode("utf-8")).hexdigest()[
        :12
    ]


def _review_who(reasons: tuple[str, ...]) -> tuple[str, str]:
    for reason in reasons:
        if reason.startswith("changes requested by "):
            who = reason[len("changes requested by ") :]
            return "the author", f"The author addresses {who}'s review and pushes the changes."
    for reason in reasons:
        if reason.startswith("waiting on "):
            who = reason[len("waiting on ") :]
            return who, f"{who} reviews it on GitHub."
    return "", ""


def _person(number: int, title: str, reasons: tuple[str, ...]) -> Verdict:
    steps: list[str] = []
    who = ""
    review_who, review_step = _review_who(reasons)
    if review_step:
        steps.append(review_step)
        who = review_who
    plain = [
        r
        for r in reasons
        if r not in _OTHER
        and not r.startswith(("file_overlap:", "waiting on ", "changes requested by "))
    ]
    if plain and review_step and who != "the author":
        # The requested reviewer is the person who approves: one step, not two.
        steps = [
            f"{who} reviews the change and approves the pull request on GitHub. "
            "RIVET lands it on its next run once its checks are green."
        ]
    elif plain:
        steps.append(_STEP["category"])
        who = who or "a person with approval rights"
    for code in ("try-cap", "approval_reset", "review_refused", "conflict", "draft"):
        if code in reasons:
            steps.append(_STEP[code])
            who = who or ("the author" if code in {"conflict", "draft"} else "a person")
    return Verdict(
        number=number,
        title=title,
        state="person",
        headline="Held: this needs a person before RIVET can land it.",
        why=tuple(explain(r) for r in reasons),
        next_steps=tuple(dict.fromkeys(steps)) or ("A person looks at it.",),
        who=who or "a person",
        draft="draft" in reasons,
        key=_key("person", reasons),
    )


def verdict_for(report: dict, number: int) -> Verdict | None:
    """The verdict for ``number`` in a report's JSON form. None when it is not listed."""
    for item in report.get("superseded") or ():
        if int(item.get("number", 0)) == number:
            keeper = item.get("keeper")
            why = str(item.get("reason") or "")
            return Verdict(
                number,
                "",
                "superseded",
                f"Superseded by #{keeper}: RIVET suggests closing this one.",
                (f"{why[:1].upper()}{why[1:]}." if why and not why.endswith(".") else why,)
                if why
                else (),
                (f"The author closes it if #{keeper} covers the change.",),
                "the author",
                False,
                _key("superseded", (str(keeper),)),
            )
    sections = (
        ("ready", "lands"),
        ("waiting_on_ci", "checks"),
        ("rivet_handling", "rivet"),
        ("with_author", "author"),
        ("needs_human", "person"),
    )
    for section, state in sections:
        for item in report.get(section) or ():
            if int(item.get("number", 0)) != number:
                continue
            title = str(item.get("title") or "")
            reasons = tuple(str(r) for r in item.get("reasons") or ())
            if state == "person":
                return _person(number, title, reasons)
            why = tuple(explain(r) for r in reasons)
            if state == "lands":
                return Verdict(
                    number,
                    title,
                    state,
                    "Ready: RIVET lands this on its next run.",
                    why,
                    ("Nothing; RIVET merges it after the next merge to main or within six hours.",),
                    "nobody",
                    False,
                    _key(state, reasons),
                )
            if state == "checks":
                return Verdict(
                    number,
                    title,
                    state,
                    "Waiting for its checks to finish. Nobody needs to do anything.",
                    why,
                    ("Nothing yet.",),
                    "nobody",
                    False,
                    _key(state, reasons),
                )
            if state == "rivet":
                return Verdict(
                    number,
                    title,
                    state,
                    "RIVET is handling this. Nobody needs to do anything yet.",
                    why,
                    ("Nothing yet; RIVET says here if it needs a person.",),
                    "RIVET",
                    False,
                    _key(state, reasons),
                )
            return Verdict(
                number,
                title,
                state,
                "With the author: it is a draft.",
                why,
                (_STEP["draft"],),
                "the author",
                True,
                _key(state, reasons),
            )
    return None


def key_in(body: str) -> str:
    """The verdict key a note carries in its marker. Empty for an older note."""
    found = _KEY.search(body or "")
    return (found.group(1) or "") if found else ""


def marker(verdict: Verdict | None) -> str:
    return f"<!-- rivet-lander:warn-only key={verdict.key} -->" if verdict else MARKER


def note_lines(verdict: Verdict) -> list[str]:
    """The note's opening: the verdict, why, what to do, and who."""
    lines = [f"**RIVET: {verdict.headline}**", ""]
    if verdict.why:
        lines.append("**Why:** " + " ".join(verdict.why))
        lines.append("")
    if verdict.who not in {"nobody", "RIVET"}:
        lines.append(f"**Who acts next:** {verdict.who}")
        lines.append("")
    if len(verdict.next_steps) == 1:
        lines.append("**What to do:** " + verdict.next_steps[0])
    else:
        lines.append("**What to do:**")
        lines.extend(f"- {step}" for step in verdict.next_steps)
    return lines


_HELD_STATES = frozenset({"person", "superseded"})


def all_verdicts(report: dict) -> list[Verdict]:
    numbers: list[int] = []
    for section in (
        "superseded",
        "needs_human",
        "rivet_handling",
        "with_author",
        "waiting_on_ci",
        "ready",
    ):
        for item in report.get(section) or ():
            number = int(item.get("number", 0))
            if number and number not in numbers:
                numbers.append(number)
    found = (verdict_for(report, n) for n in numbers)
    return [v for v in found if v is not None]


def refresh_plan(report: dict, existing: dict[int, str]) -> list[tuple[int, str]]:
    """``(number, "post" | "edit")`` for each note a refresh should write.

    ``existing`` maps a pull request that already has a note to the key that
    note carries. A note whose key still matches is left alone. A held pull
    request with no note gets one, unless it is a draft. Nothing else is posted.
    """
    plan: list[tuple[int, str]] = []
    for verdict in all_verdicts(report):
        if verdict.number in existing:
            if existing[verdict.number] != verdict.key:
                plan.append((verdict.number, "edit"))
        elif verdict.state in _HELD_STATES and not verdict.draft:
            plan.append((verdict.number, "post"))
    return plan


def _row(verdict: Verdict) -> str:
    title = verdict.title.replace("|", "\\|")
    if len(title) > 70:
        title = title[:69] + "…"
    why = " ".join(verdict.why).replace("|", "\\|") or "-"
    return f"| #{verdict.number} | {title} | {why} | {verdict.who} |"


def summary_markdown(report: dict, backlog_text: str = "") -> str:
    """The run summary: who must act first, then what lands, then counts."""
    verdicts = all_verdicts(report)
    held = [v for v in verdicts if v.state in _HELD_STATES and not v.draft]
    lines = ["## RIVET lander", ""]
    lines.append(f"### Needs a person ({len(held)})")
    if held:
        lines += [
            "",
            "| Pull request | Title | Why it is held | Who acts next |",
            "|---|---|---|---|",
        ]
        lines += [_row(v) for v in held]
    else:
        lines.append("Nothing is waiting on a person.")
    ready = [v for v in verdicts if v.state == "lands"]
    lines += ["", f"### Ready to land ({len(ready)})"]
    lines.append(", ".join(f"#{v.number}" for v in ready) if ready else "None.")
    rivet = [v for v in verdicts if v.state == "rivet"]
    checks = [v for v in verdicts if v.state == "checks"]
    drafts = [v for v in verdicts if v.draft]
    counts = []
    if rivet:
        counts.append(
            f"RIVET is handling {len(rivet)} ({', '.join(f'#{v.number}' for v in rivet)})"
        )
    if checks:
        counts.append(f"{len(checks)} waiting on checks")
    if drafts:
        counts.append(f"{len(drafts)} drafts with their authors")
    if counts:
        lines += ["", "### Everything else", "; ".join(counts) + "."]
    if backlog_text.strip():
        lines += [
            "",
            "<details><summary>Full backlog report</summary>",
            "",
            "```",
            backlog_text.strip(),
            "```",
            "",
            "</details>",
        ]
    return "\n".join(lines) + "\n"
