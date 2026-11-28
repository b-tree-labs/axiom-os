# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A conservative reading of a short reply. Rules only, no model call.

The reading can say yes, say no/hold, or say it cannot tell. Only a short reply
that opens with an affirmative phrase and carries no hedge is a yes. Anything
that looks like a question, a condition or a long explanation cannot be told, and
"cannot tell" blocks. Quoted text and code blocks are removed first, so words the
owner pasted from somewhere else are never the owner's words.
"""

from __future__ import annotations

import re

YES = "yes"
HOLD = "hold"
UNCLEAR = "unclear"

#: A yes this short is a reply to the message just above it; a longer one is a conversation.
MAX_YES_WORDS = 14

_FENCE = re.compile(r"```.*?(```|$)", re.DOTALL)
_PASTED = re.compile(r"<pasted_content\b.*?(</pasted_content[^>]*>|$)", re.DOTALL)
_IMAGE = re.compile(r"\[Image[^\]]*\]")

_AFFIRM_OPENINGS = (
    "yes",
    "yep",
    "yeah",
    "yup",
    "sure",
    "ok",
    "okay",
    "approved",
    "approve",
    "go ahead",
    "go for it",
    "go",
    "do it",
    "build it",
    "ship it",
    "land it",
    "merge it",
    "tag it",
    "release it",
    "deploy it",
    "looks good",
    "lgtm",
    "sounds good",
    "proceed",
    "confirmed",
    "agreed",
    "correct",
    "that works",
    "perfect",
    "great",
    "good",
    "fine",
    "please do",
    "please proceed",
    "make it so",
    "run it",
    "start",
    "begin",
    "continue",
)
_OPENING = re.compile(
    r"^(?:"
    + "|".join(re.escape(p) for p in sorted(_AFFIRM_OPENINGS, key=len, reverse=True))
    + r")\b[\s,.!:;-]*",
    re.IGNORECASE,
)
_HEDGE = re.compile(
    r"\b(but|except|only|unless|if|though|although|however|maybe|perhaps|not|wait|hold|before|first|"
    r"instead|rather|what|why|how|when|where|which|who|should|could|would|can|do you|are you|is it)\b|n't|\?",
    re.IGNORECASE,
)
_FOLLOWING = frozenset(
    [
        "and",
        "then",
        "also",
        "it",
        "that",
        "this",
        "them",
        "these",
        "those",
        "all",
        "both",
        "everything",
        "everything's",
        "please",
        "now",
        "thanks",
        "thank",
        "you",
        "land",
        "lands",
        "merge",
        "merged",
        "tag",
        "tags",
        "build",
        "builds",
        "ship",
        "release",
        "deploy",
        "push",
        "open",
        "the",
        "pr",
        "prs",
        "pull",
        "request",
        "go",
        "ahead",
        "do",
        "yes",
        "yep",
        "yeah",
        "ok",
        "okay",
        "sure",
        "approve",
        "approved",
        "looks",
        "good",
        "great",
        "proceed",
        "with",
        "as",
        "is",
        "one",
        "two",
        "three",
        "work",
        "plan",
        "change",
        "changes",
        "up",
        "me",
        "for",
        "team",
        "over",
        "done",
        "really",
        "definitely",
        "absolutely",
        "exactly",
        "right",
        "correct",
        "perfect",
        "fine",
        "s",
        "ll",
        "ve",
    ]
)
_HOLD = re.compile(
    r"\b(hold|wait|stop|pause|cancel|abort|revert|undo|scrap|never ?mind|reject|rejected|"
    r"don't|do not|dont|not yet|not now)\b|^\W*(no|nope|nah)(?=\W*$|[,.!;:]|\s+(wait|stop|don't|do|not|hold|let|please|cancel|revert))",
    re.IGNORECASE,
)
#: "no problem", "no worries" and the like are not a refusal.
_NOT_A_NO = re.compile(r"\bno (problem|worries|rush|doubt)\b", re.IGNORECASE)


def owner_words(text: str) -> str:
    """The reply without quoted lines, code fences, pasted blocks or image markers."""
    text = _FENCE.sub(" ", text)
    text = _PASTED.sub(" ", text)
    text = _IMAGE.sub(" ", text)
    kept = [line for line in text.splitlines() if not line.lstrip().startswith(">")]
    return re.sub(r"\s+", " ", " ".join(kept)).strip()


def reading(text: str) -> str:
    """``yes``, ``hold`` (a no, a stop, a wait) or ``unclear``."""
    words = owner_words(text)
    if not words:
        return UNCLEAR
    lowered = _NOT_A_NO.sub(" ", words)
    opening = _OPENING.match(lowered)
    if opening is None:
        # A refusal that does not open with a yes-word is still a refusal.
        return HOLD if len(words.split()) <= MAX_YES_WORDS and _HOLD.search(lowered) else UNCLEAR
    rest = lowered[opening.end() :]
    if len(words.split()) > MAX_YES_WORDS or "?" in words:
        return UNCLEAR
    if _HEDGE.search(rest):
        return UNCLEAR
    # What follows a yes may only be more agreement or an action word the owner is
    # approving. A new instruction ("go look at the dashboard") is not a yes.
    tokens = re.findall(r"[a-z0-9']+", rest)
    if any(token not in _FOLLOWING for token in tokens):
        return UNCLEAR
    return YES


def is_hold(text: str, *, pr: int, branch: str = "", work_terms: tuple[str, ...] = ()) -> bool:
    """Does a later message take the agreement back? Errs toward yes, because a void only blocks.

    A short message with a hold word voids, wherever it was said. A longer one voids
    only when it names this pull request, its branch or its work.
    """
    words = owner_words(text)
    if not words or not _HOLD.search(_NOT_A_NO.sub(" ", words)):
        return False
    if len(words.split()) <= 12:
        return True
    lowered = words.lower()
    if re.search(rf"(?<![\w/])#{pr}\b|/pull/{pr}\b|\bpr {pr}\b|\bpull request {pr}\b", lowered):
        return True
    if branch and branch.lower() in lowered:
        return True
    hits = [t for t in work_terms if t and t.lower() in lowered]
    return len(hits) >= 2


# --- naming the work and the irreversible actions ---------------------------

_STOP = frozenset(
    [
        "the",
        "a",
        "an",
        "and",
        "or",
        "of",
        "to",
        "for",
        "in",
        "on",
        "at",
        "by",
        "with",
        "from",
        "this",
        "that",
        "it",
        "its",
        "is",
        "are",
        "be",
        "as",
        "into",
        "onto",
        "per",
        "via",
        "add",
        "adds",
        "added",
        "make",
        "makes",
        "build",
        "builds",
        "change",
        "changes",
        "new",
        "use",
        "uses",
        "using",
        "work",
    ]
)

#: Each irreversible action and the words that name it. A yes to a message that
#: names none of these does not cover the action.
ACTION_WORDS: dict[str, tuple[str, ...]] = {
    "release": (
        "release",
        "releasing",
        "publish",
        "ship the release",
        "cut the release",
        "cut a release",
    ),
    "tag": ("tag", "tags", "tagging", "tagged", "retag"),
    "node_change": (
        "deploy",
        "deploying",
        "rollout",
        "roll out",
        "roll back",
        "rollback",
        "node",
        "restart",
        "install on",
    ),
    "secrets": (
        "secret",
        "secrets",
        "credential",
        "credentials",
        "vault",
        "rotate",
        "rotation",
        "api key",
        "token",
    ),
    "delete_data": (
        "delete",
        "deleting",
        "drop",
        "purge",
        "wipe",
        "truncate",
        "destroy",
        "remove the data",
    ),
}


def content_words(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9][a-z0-9_/.\-]{2,}", text.lower())
    return [w for w in dict.fromkeys(words) if w not in _STOP and len(w) >= 4]


def names_work(message: str, *, work: str, pr: int, branch: str = "") -> bool:
    """Does the assistant's message say what the work is? A ticket, the branch, or most of its words."""
    lowered = message.lower()
    if re.search(rf"(?<![\w/])#{pr}\b|/pull/{pr}\b", lowered):
        return True
    if branch and branch.lower() in lowered:
        return True
    tickets = re.findall(r"#\d{2,}", work)
    if tickets:
        return all(t in message for t in tickets)
    wanted = content_words(work)
    if not wanted:
        return False
    present = [w for w in wanted if w in lowered]
    if len(wanted) == 1:
        return bool(present)
    return len(present) >= 2 and len(present) / len(wanted) >= 0.6


def names_action(text: str, action: str) -> bool:
    words = ACTION_WORDS.get(action, (action.replace("_", " "),))
    lowered = text.lower()
    return any(re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", lowered) for w in words)
