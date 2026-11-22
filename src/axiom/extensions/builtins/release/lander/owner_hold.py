# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A pull request that is finished and waiting for its owner: the review hold.

The lander lands what is green and marked ready, so a draft used to be the only
way to keep a finished pull request from landing while its owner read it. A
draft also skips the full suite and reads as unfinished. A hold says the other
thing: the work is done and its owner has not released it yet.

A pull request is held when it carries one of ``hold.labels`` (default
``hold: owner review``), or, when ``hold.owner_review_request`` is on, when a
review request to an owner is still pending. Both facts are already in what the
lander reads for every pull request, so judging a hold costs no forge call.

A held pull request is left exactly as it is, draft or ready and whatever its
checks say: not landed, not claimed, not updated, not handed to a fixer, and not
closed and reopened to start CI. Taking the label off (or answering the review
request) is the whole release: the next pass treats it like any other.

The report lists held pull requests in their own group with an age. The forge's
list call gives label names with no time, and reading when a label was applied
is one more call per pull request, so the age shown is the pull request's own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from axiom.extensions.builtins.release.lander.config import LanderConfig
from axiom.extensions.builtins.release.lander.model import OpenPull
from axiom.extensions.builtins.release.lander.reviews import same_login
from axiom.infra.state import locked_append_jsonl

#: The report reason, and the category of the line raised to a person.
HELD_FOR_OWNER_REVIEW = "held_for_owner_review"
#: The report group's title.
GROUP_TITLE = "Waiting for owner review"
#: What the age in the report is counted from.
AGE_FROM = "pull_request_opened"

LABEL = "label"
REVIEW_REQUEST = "review_request"


@dataclass(frozen=True)
class Hold:
    """Why one pull request is held: by a label, or by a review request to an owner."""

    by: str
    why: str
    release: str

    @property
    def reason(self) -> str:
        return HELD_FOR_OWNER_REVIEW

    @property
    def detail(self) -> str:
        """The reason and its cause on one line, for an outcome or a block."""
        return f"{HELD_FOR_OWNER_REVIEW}: {self.why}"


def owners(config: LanderConfig, repo: str = "") -> tuple[str, ...]:
    """Logins whose pending review request is a hold.

    ``hold.owners`` when it is set. Otherwise the account the repository belongs
    to, which is a person only for a personally owned repository: one owned by
    an organisation lists its owners.
    """
    if config.hold_owners:
        return config.hold_owners
    account = str(repo or "").partition("/")[0].strip()
    return (account,) if account and "/" in str(repo) else ()


def hold_for(pull: OpenPull, config: LanderConfig, *, repo: str = "") -> Hold | None:
    """The hold on one pull request, or None. Reads only what the lander already fetched."""
    wanted = {label.casefold() for label in config.hold_labels}
    for label in pull.labels:
        if label.casefold() in wanted:
            return Hold(LABEL, f'label "{label}"', "remove the label to release it")
    if config.hold_on_owner_review_request:
        asked = owners(config, repo)
        for login in pull.pending_human_reviewers:
            if any(same_login(login, owner) for owner in asked):
                return Hold(
                    REVIEW_REQUEST,
                    f"review requested from {login}",
                    "their review, or withdrawing the request, releases it",
                )
    return None


def age_hours(pull: OpenPull, now: datetime | None = None) -> float:
    """Hours since the pull request was opened (see the module note on label times)."""
    moment = now or datetime.now(UTC)
    return round(max(0.0, (moment - pull.created_at).total_seconds()) / 3600, 1)


def format_age(hours: float) -> str:
    whole = int(hours)
    if whole < 1:
        return "under 1h"
    days, rest = divmod(whole, 24)
    if not days:
        return f"{rest}h"
    return f"{days}d {rest}h" if rest else f"{days}d"


def row(pull: OpenPull, hold: Hold, now: datetime | None = None) -> dict:
    """The JSON form of one held pull request."""
    hours = age_hours(pull, now)
    return {
        "number": pull.number,
        "title": pull.title,
        "head_sha": pull.head_sha,
        "draft": pull.draft,
        "reason": hold.reason,
        "held_by": hold.by,
        "why": hold.why,
        "age_hours": hours,
        "age_from": AGE_FROM,
        "sentence": f"open {format_age(hours)}, held by {hold.why}; {hold.release}",
    }


def raise_to_person(state_dir: Path, repo: str, pull: OpenPull, hold: Hold) -> str:
    """File one held pull request where the lander already raises things to a person.

    The same line, in the same event log, as a head CI never ran on
    (``ci_start.raise_to_person``): one "raise an ask" call can replace both
    bodies without touching their callers.
    """
    locked_append_jsonl(
        Path(state_dir) / "agents" / "rivet" / "lander.jsonl",
        {
            "action": "needs-person",
            "category": HELD_FOR_OWNER_REVIEW,
            "repo": repo,
            "pr": pull.number,
            "head_sha": pull.head_sha,
            "held_by": hold.by,
            "failure_reason": hold.why,
            "next_route": "human",
        },
    )
    return hold.detail
