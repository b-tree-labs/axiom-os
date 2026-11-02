# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""How a conversation names itself — succinct, stable, from its substance.

Ported in behaviour from the reference product's ``_generate_conversation_title``
(SoilMetrix ``api/services/chat_service.py`` @ dd3a7602b), whose UX is the bar:

- **Deterministic.** No model call: a title is computed from the words, so it
  costs nothing, never fails, and two runs agree.
- **Re-titled while automatic.** After every assistant reply the title is
  recomputed from the first few user messages and the reply, so the name
  improves as the conversation finds its subject. A first-message prefix cut
  at 60 characters — what this platform did — named a chat after however it
  happened to start, forever.
- **Locked by a rename.** A person who renamed a chat chose that name; it is
  never overwritten (``title_source == "manual"``).
- **Domain rules are a hook, not a table here.** The reference's first pass is
  a rule table in that product's own vocabulary. That is a product's
  vocabulary. Here a deployment registers callables under the
  ``axiom.chat_title_rules`` entry-point group; the first to return a title
  wins, and the generic keyword fallback follows.

One thing added: a SEED PREFIX is removed first. A chat opened from a view or
a case starts "About case 41 — pump bearing temperature. Claims: …", and the
reader's question is the part after it, not the boilerplate.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable

log = logging.getLogger(__name__)

DEFAULT_TITLE = "New Chat"
#: The reference's limit: a rail row shows about this much before it ellipsises.
MAX_CHARS = 44
#: How many user messages a title is computed from (the reference reads four).
USER_MESSAGES_READ = 4

#: The reference's stopwords, plus the question scaffolding a chat starts with
#: ("did", "was", "show me", "summarize") that names nothing.
STOPWORDS = frozenset(
    [
        "a",
        "about",
        "after",
        "again",
        "all",
        "am",
        "an",
        "and",
        "any",
        "anything",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "get",
        "give",
        "had",
        "has",
        "have",
        "help",
        "here",
        "how",
        "i",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "list",
        "me",
        "my",
        "no",
        "not",
        "of",
        "on",
        "one",
        "or",
        "our",
        "please",
        "should",
        "show",
        "so",
        "some",
        "summarize",
        "tell",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "they",
        "this",
        "to",
        "us",
        "use",
        "using",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "yes",
        "you",
        "your",
        "explain",
        "find",
        "also",
    ]
)

TitleRule = Callable[[str], "str | None"]

_SEED = re.compile(r"^\s*about\s+(?:case\s+\S+\s*[—–-]\s*)?(?P<subject>[^.\n]*)", re.IGNORECASE)


def strip_seed(text: str) -> str:
    """The reader's subject from a seeded opening ("About case 41 — X. Claims: …" → "X")."""
    m = _SEED.match(text or "")
    if m and m.group("subject").strip():
        return m.group("subject").strip()
    return text or ""


def _case(word: str) -> str:
    """Title-case a word, but keep one a person wrote with deliberate capitals
    (API, pH, JSON) as written."""
    if any(c.isupper() for c in word[1:]) or word.isupper() and len(word) > 1:
        return word
    return word[:1].upper() + word[1:].lower()


def keywords(text: str, limit: int = 5) -> list[str]:
    words = re.findall(r"[A-Za-z0-9]+(?:[-'][A-Za-z0-9]+)*", text or "")
    out: list[str] = []
    seen: set[str] = set()
    for word in words:
        lower = word.lower()
        if lower in STOPWORDS or len(lower) < 2 or lower in seen:
            continue
        seen.add(lower)
        out.append(_case(word))
        if len(out) >= limit:
            break
    return out


def compact(title: str, max_chars: int = MAX_CHARS) -> str:
    """Whole words up to the limit, never a word cut in half."""
    title = re.sub(r"\s+", " ", title or "").strip(" -:,.?")
    if len(title) <= max_chars:
        return title or DEFAULT_TITLE
    out = ""
    for word in title.split():
        candidate = f"{out} {word}".strip()
        if len(candidate) > max_chars:
            break
        out = candidate
    return out or title[:max_chars].rstrip()


def _rules() -> list[TitleRule]:
    try:
        from importlib.metadata import entry_points

        found = []
        for ep in entry_points(group="axiom.chat_title_rules"):
            try:
                found.append(ep.load())
            except Exception as exc:  # noqa: BLE001 - one broken rule never blocks titling
                log.warning("title rule %s failed to load: %s", ep.name, exc)
        return found
    except Exception:  # noqa: BLE001 - no entry points is the generic engine alone
        return []


def auto_title(
    user_messages: Iterable[str],
    assistant_text: str = "",
    *,
    rules: Iterable[TitleRule] | None = None,
) -> str:
    """A succinct, stable title from what the conversation is about."""
    users = [strip_seed(m) for m in user_messages if m and m.strip()][:USER_MESSAGES_READ]
    joined = " ".join(users).strip()
    if not joined:
        return DEFAULT_TITLE
    text = f"{joined} {assistant_text or ''}"
    for rule in _rules() if rules is None else rules:
        try:
            named = rule(text)
        except Exception as exc:  # noqa: BLE001 - a rule that throws is skipped, not fatal
            log.warning("title rule failed: %s", exc)
            continue
        if named and named.strip():
            return compact(named)
    words = keywords(joined)
    return compact(" ".join(words)) if words else DEFAULT_TITLE


def is_automatic(title: str, context: dict | None) -> bool:
    """May the conversation's title still be recomputed?

    The reference's rule (``_is_auto_title``), exactly: yes while the title is
    empty, the placeholder, or was set by this engine; never once a person
    named it. A title with no recorded source is treated as KEPT — that is
    every conversation saved before this engine existed, and re-titling those
    would rename people's existing chats the next time they spoke in them.
    """
    source = (context or {}).get("title_source")
    if source == "manual":
        return False
    t = (title or "").strip()
    return not t or t == DEFAULT_TITLE or source == "auto"
