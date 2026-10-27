# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""What a person would plausibly do next — one ranked list for every surface.

The chat face of the platform's "every result leads to its follow-up" rule.
The CLI already prints a result's follow-ups (the ADR-185 ``chain.next``
envelope); this ranks the same kind of thing ACROSS a person's state, so the
chat home and the chat slide-out can offer it as cards, and ``axi next`` and
the MCP tool can return the same list.

Modelled on the reference product's action cards (SoilMetrix
``api/routes/field_hand.py`` ``_build_suggested_cards`` @ dd3a7602b), keeping
its rules:

- **Card 1 is the single most forward-looking prompt.**
- **Beta-honest.** A card is offered only when what it leads to exists here
  and the data it needs is present. Nothing on failure: showing nothing beats
  offering what cannot be delivered.
- **At most five**, ranked by a transparent priority, each with its ``why``.

Three inputs, as asked for:

- **app state** — the view context the slide-out carries (surface, label,
  focused). Only its LABEL is ever shown; the context rides as
  ``view_context`` (the two-channel contract);
- **the person** — their principal and roles; nothing is offered that their
  role may not do, and nothing is read that is not theirs;
- **their history** — the follow-ups the platform itself declared after their
  recent actions (``SkillSpec.next_steps``), and a conversation left recently.

Domain vocabulary is a hook: a consumer contributes candidates under the
``axiom.next_providers`` entry-point group.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

MAX_CARDS = 5
#: A conversation is worth picking up when it was left a while ago, but not long ago.
PICK_UP_AFTER = timedelta(minutes=15)
PICK_UP_WITHIN = timedelta(days=7)


@dataclass(frozen=True)
class Card:
    """One offer. ``kind`` is ``prompt`` (seeds the composer; the person reviews
    and sends) or ``action`` (opens a conversation, a route, or names a skill)."""

    id: str
    label: str
    kind: str
    why: str
    priority: int = 0
    prompt: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("priority")
        return d


@dataclass
class Asker:
    """Who is asking, and from where."""

    principal: str = ""
    roles: frozenset[str] = frozenset()
    view: dict[str, Any] | None = None


Provider = Callable[[Asker], Iterable[Card]]


# -- providers ----------------------------------------------------------------


def from_view(asker: Asker) -> list[Card]:
    """What the person is looking at. Generic words: a domain says more through
    its own provider."""
    view = asker.view or {}
    label = str(view.get("label") or "").strip()
    if not label:
        return []
    return [
        Card(
            id="view-next",
            label=f"What should I look at next in {label}?",
            kind="prompt",
            why="From what you have open",
            priority=9,
            prompt=f"What should I look at next in {label}?",
        ),
        Card(
            id="view-changed",
            label=f"What changed in {label} recently?",
            kind="prompt",
            why="From what you have open",
            priority=6,
            prompt=f"What changed in {label} recently?",
        ),
    ]


def from_conversations(asker: Asker, *, now: datetime | None = None) -> list[Card]:
    """A conversation the person left a while ago and may want to pick up."""
    if not asker.principal:
        return []  # nobody's history is read without knowing whose it is
    from axiom.infra.orchestrator.conversation_store import resolve_conversation_store

    store = resolve_conversation_store(None)
    now = now or datetime.now(UTC)
    for meta in store.list_meta(principal=asker.principal, limit=5):
        raw = meta.get("updated_at")
        try:
            when = datetime.fromisoformat(str(raw)) if raw else None
        except ValueError:
            when = None
        if when is None:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        age = now - when
        title = (meta.get("title") or "").strip()
        if PICK_UP_AFTER <= age <= PICK_UP_WITHIN and title and title != "New Chat":
            return [
                Card(
                    id=f"pick-up-{meta['id']}",
                    label=f"Pick up “{title}”",
                    kind="action",
                    why="You left this conversation recently",
                    priority=7,
                    payload={"conversation_id": str(meta["id"])},
                )
            ]
    return []


def from_recent_actions(
    asker: Asker, *, registry: Any = None, recent: Callable[..., dict] | None = None
) -> list[Card]:
    """The follow-ups the platform declared after the person's recent actions."""
    if not asker.principal:
        return []
    if recent is None:
        from axiom.policy.action_ledger import recent_actions as recent
    rows = (recent(n=20, agent=asker.principal) or {}).get("actions") or []
    if not rows:
        return []
    if registry is None:
        registry = full_registry()
    cards: list[Card] = []
    seen: set[str] = set()
    for row in rows:
        spec = (
            registry.spec(str(row.get("name") or ""))
            if registry.has(str(row.get("name") or ""))
            else None
        )
        if spec is None or row.get("outcome") not in (None, "", "ok", "applied", "allowed"):
            continue
        for step in spec.next_steps or ():
            if step.when not in ("always", "ok") or step.target in seen:
                continue
            target = (
                registry.spec(step.target) if step.is_skill and registry.has(step.target) else None
            )
            if step.is_skill and target is None:
                continue  # beta-honest: it is not here
            if target is not None and target.effective_risk == "destructive":
                continue  # never a one-click card; it goes through plan / approve
            seen.add(step.target)
            cards.append(
                Card(
                    id=f"follow-{step.target}",
                    label=(target.description.split(".")[0] if target is not None else step.target)[
                        :80
                    ],
                    kind="action",
                    why=step.why or f"Usually follows {spec.name}",
                    priority=8,
                    payload={"skill": step.target} if step.is_skill else {"command": step.target},
                )
            )
    return cards[:3]


def default_first(asker: Asker) -> list[Card]:
    """When nothing else is known, the most forward-looking thing to ask."""
    return [
        Card(
            id="attention",
            label="What needs my attention today?",
            kind="prompt",
            why="A good first question when nothing else is open",
            priority=5,
            prompt="What needs my attention today?",
        )
    ]


PROVIDERS: dict[str, Provider] = {
    "view": from_view,
    "conversations": from_conversations,
    "recent_actions": from_recent_actions,
    "default": default_first,
}


def _extension_providers() -> list[Provider]:
    try:
        from importlib.metadata import entry_points

        out: list[Provider] = []
        for ep in entry_points(group="axiom.next_providers"):
            try:
                out.append(ep.load())
            except Exception as exc:  # noqa: BLE001 - one broken provider never blocks the rest
                log.warning("next provider %s failed to load: %s", ep.name, exc)
        return out
    except Exception:  # noqa: BLE001
        return []


_REGISTRY: Any = None


def full_registry() -> Any:
    """Every extension's skills, bound once per process (the same discovery the
    SKILL.md generator uses), so a declared follow-up can be checked to exist."""
    global _REGISTRY
    if _REGISTRY is None:
        from axiom.infra.skills_emit import bind_all_skills

        _REGISTRY = bind_all_skills()
    return _REGISTRY


def rank(cards: Iterable[Card], limit: int = MAX_CARDS) -> list[Card]:
    """Card 1 is the best PROMPT (the reference's rule); the rest by priority.
    Duplicates by label are dropped; at most ``limit``."""
    pool: list[Card] = []
    seen: set[str] = set()
    for card in sorted(cards, key=lambda c: c.priority, reverse=True):
        key = card.label.strip().lower()
        if key and key not in seen:
            seen.add(key)
            pool.append(card)
    first = next((c for c in pool if c.kind == "prompt"), None)
    ordered = ([first] if first else []) + [c for c in pool if c is not first]
    return ordered[: max(1, min(limit, MAX_CARDS))]


def suggest(
    asker: Asker,
    *,
    limit: int = MAX_CARDS,
    providers: dict[str, Provider] | None = None,
    extensions: bool = True,
) -> dict[str, Any]:
    """The ranked cards, and any source that could not be read (never fatal)."""
    table = dict(providers or PROVIDERS)
    if extensions:
        for i, fn in enumerate(_extension_providers()):
            table[f"ext{i}"] = fn
    cards: list[Card] = []
    skipped: dict[str, str] = {}
    for name, fn in table.items():
        try:
            cards.extend(fn(asker))
        except Exception as exc:  # noqa: BLE001 - nothing on failure, never a broken list
            log.warning("next provider %s failed: %s", name, exc)
            skipped[name] = f"{type(exc).__name__}: {exc}"
    return {"cards": [c.to_dict() for c in rank(cards, limit)], "skipped": skipped}
