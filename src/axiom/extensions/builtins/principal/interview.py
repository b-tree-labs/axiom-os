# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The setup interview — who the harness works for, and how to reach them.

Spec §3. Resumable and re-runnable: answers are a plain dict, so a caller can
persist partial progress and come back. Nothing here verifies anything; the
interview produces an *unverified* principal by construction, and only the comms
test run (``verify.py``) can promote it.

The questions-in / answers-out shape deliberately mirrors ``comms.configure``
(ADR-074), which already drives a guided channel-policy interview. The boundary
between them: **this module owns who the person is and which endpoints are
proven; comms.configure owns channel-policy assembly.** They overlap on the
"which channels" question today, and the follow-up is for setup to delegate that
portion rather than ask it twice — filed rather than silently duplicated.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import (
    ChannelPreference,
    ContactEndpoint,
    PrincipalProfile,
    PrincipalStatus,
)

__all__ = ["Question", "QUESTIONS", "apply_answers", "next_question"]


@dataclass(frozen=True)
class Question:
    key: str
    prompt: str
    required: bool = True
    help: str = ""


QUESTIONS: tuple[Question, ...] = (
    Question(
        key="display_name",
        prompt="What name should this harness use for you?",
    ),
    Question(
        key="endpoints",
        prompt="How can it reach you? (one or more of email, chat, sms, inbox)",
        help="Each one is proven with a real message before it is used for anything.",
    ),
    Question(
        key="preferences",
        prompt="For each kind of topic, which channels in order of preference?",
        help="A topic class of '*' is the catch-all.",
    ),
    Question(
        key="quiet_hours",
        prompt="Any hours you would rather not be disturbed? (e.g. 22:00-07:00)",
        required=False,
        help="Low-urgency messages defer until the window ends; urgent ones still come through.",
    ),
)


def next_question(answers: dict) -> Question | None:
    """The first required question not yet answered, or None when done."""
    for q in QUESTIONS:
        if q.required and not answers.get(q.key):
            return q
    return None


def apply_answers(
    answers: dict, *, handle: str, directory_ref: str | None = None
) -> PrincipalProfile:
    """Build a principal profile from interview answers.

    ``handle`` is the platform's ``@name:context`` grammar, validated by the
    identity layer itself. Keying on anything else would leave this record unable
    to join to ``PrincipalContext`` — a reachability layer the identity layer
    cannot reach into is not part of the stack.
    """

    endpoints = [
        ContactEndpoint(kind=e["kind"], address=e["address"])
        for e in answers.get("endpoints") or []
        if isinstance(e, dict)
    ]
    preferences = [
        ChannelPreference(
            topic_class=p["topic_class"],
            ranked_kinds=list(p.get("ranked_kinds") or []),
            urgency_floor=int(p.get("urgency_floor", 5)),
        )
        for p in answers.get("preferences") or []
        if isinstance(p, dict)
    ]

    quiet = answers.get("quiet_hours")
    if isinstance(quiet, str) and "-" in quiet:
        start, _, end = quiet.partition("-")
        quiet = (start.strip(), end.strip())
    elif not isinstance(quiet, tuple):
        quiet = None

    principal = PrincipalProfile(
        handle=handle,
        display_name=answers.get("display_name") or "",
        directory_ref=directory_ref,
        endpoints=endpoints,
        preferences=preferences,
        quiet_hours=quiet,
    )
    # Answers alone never make a principal active — only a round trip does.
    principal.status = (
        PrincipalStatus.PENDING if next_question(answers) else PrincipalStatus.UNVERIFIED
    )
    return principal
