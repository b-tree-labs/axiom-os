# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Where a pending approval lives between the ask and the answer.

``ApprovalGate`` held its actions in a plain dictionary, so an action awaiting
human confirmation existed only for as long as the process that proposed it. A
restart, a crash, or a CLI invocation that simply ended took the queue with it.
That is tolerable for a single interactive command and wrong for anything that
pauses: an agent that asks permission and then forgets it asked has not asked.

It is also the thing standing between the platform and a durable checkpoint. A
graph that pauses mid-run for approval needs its pause to survive, and the
alternative — letting a foreign runtime keep that state in its own checkpointer
— forks the record of what an agent did. A safety case cannot cite two records.

Two stores, one interface.

:class:`InMemoryActionStore` is the old behaviour, kept and named rather than
left implicit. Tests want it, and so does a caller that genuinely has no
lifetime beyond the call.

:class:`FileActionStore` is durable, and callers opt into it. See
``ApprovalGate.__init__`` for why it is not the default.


Failing closed, and failing loudly
----------------------------------

The sibling precedent is ``FileDedupLog`` in notifications, which degrades to
"nothing is deduplicated" when its file is unreadable, on the reasoning that
losing suppression is noisy while losing an alert is fatal.

**This store must degrade the other way, and does.** An unreadable approval
store may never answer "no record" to a question about whether something was
approved, because every caller reads that as "not approved yet" or, worse,
builds a fresh queue on top of it. So a read failure raises
:class:`ApprovalStoreUnavailable` rather than returning empty.

That takes one explicit check rather than a bare ``try``, because the shared
``LockedJsonFile.read()`` helper *also* degrades the convenient way: it catches
``JSONDecodeError`` and hands back ``{}``. Wrapping it in a ``try`` looks like a
guard and catches nothing. See ``_read``.

Silence is the specific danger. Returning an empty queue is technically fail-
closed — nothing runs — but it presents as "there is nothing to approve", which
is indistinguishable from a healthy idle system. An operator watching an empty
queue while actions pile up unreadably is worse off than one seeing an error.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from axiom.infra.orchestrator.actions import Action, ActionStatus
from axiom.infra.state import LockedJsonFile

__all__ = [
    "DEFAULT_TTL_HOURS",
    "ActionStore",
    "ApprovalStoreUnavailable",
    "FileActionStore",
    "InMemoryActionStore",
    "default_approval_path",
]

#: How long a pending action stays actionable.
#:
#: Durability without expiry is its own hazard: an action proposed in one
#: context and approved a fortnight later is approved against a world that has
#: moved. Expiry is not rejection — the record says the answer never came, which
#: is a different fact from "a human said no" and matters when someone asks why
#: it did not run.
DEFAULT_TTL_HOURS = 72.0


class ApprovalStoreUnavailable(RuntimeError):
    """The approval store could not be read or written.

    Deliberately not caught and converted into an empty result anywhere in this
    module. A caller that cannot reach the approval record must stop, not
    proceed on the assumption that an absent record means an absent action.
    """


class ActionStore(Protocol):
    """Where actions live between submission and resolution."""

    def get(self, action_id: str) -> Action | None: ...

    def put(self, action: Action) -> None: ...

    def all(self) -> list[Action]: ...


class InMemoryActionStore:
    """Process-local. The original behaviour, now explicit rather than assumed.

    Correct for a single interactive command that resolves everything it
    proposes. Wrong for anything that pauses, which is why it is no longer the
    default.
    """

    def __init__(self) -> None:
        self._actions: dict[str, Action] = {}

    def get(self, action_id: str) -> Action | None:
        return self._actions.get(action_id)

    def put(self, action: Action) -> None:
        self._actions[action.action_id] = action

    def all(self) -> list[Action]:
        return list(self._actions.values())


def default_approval_path() -> Path:
    """Where the durable queue lives.

    ``get_user_state_dir()`` rather than a hand-rolled path, and this was wrong
    before it was right. It mirrored ``default_dedup_path``, which reads
    ``AXIOM_STATE_DIR`` and hardcodes ``~/.axi/state`` — and both halves of that
    are off. The platform's override is spelled ``AXI_STATE_DIR``, and the
    directory is branding-aware, so hardcoding ``.axi`` bakes in a consumer name
    the platform is supposed to take from branding.

    The part that actually bit: chat and the CLI verbs set
    ``SkillContext.state_dir`` to ``get_user_state_dir()``, i.e. ``~/.axi``,
    while this returned ``~/.axi/state``. An approval held through a skill
    context therefore landed one directory away from where ``axi approve``
    looked, and the queue read as empty — the precise failure this store exists
    to make impossible.
    """
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir() / "orchestrator" / "approvals.json"


class FileActionStore:
    """Durable, concurrency-safe, and loud when it cannot do its job.

    Reads on every lookup rather than caching, because the whole point is that
    another process wrote it: the CLI that approves an action is rarely the
    process that proposed it.

    Writes go through :class:`~axiom.infra.state.LockedJsonFile` per ADR-011.
    An unprotected read-modify-write here would lose approvals under exactly
    the conditions the store exists for, which is several processes touching
    one queue.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        ttl_hours: float = DEFAULT_TTL_HOURS,
        now: datetime | None = None,
    ) -> None:
        self._path = Path(path) if path else default_approval_path()
        self._ttl = timedelta(hours=ttl_hours)
        self._now_override = now

    def _now(self) -> datetime:
        return self._now_override or datetime.now(UTC)

    def _read(self) -> dict[str, dict]:
        if not self._path.exists():
            return {}
        try:
            with LockedJsonFile(self._path, exclusive=False) as handle:
                data = handle.read()
                raw = self._path.read_text(encoding="utf-8")
        except Exception as exc:
            raise ApprovalStoreUnavailable(
                f"cannot read the approval store at {self._path}: {exc}. "
                "Refusing to report an empty queue, which would be "
                "indistinguishable from having nothing to approve."
            ) from exc

        # LockedJsonFile.read() catches JSONDecodeError and returns {}. That is
        # right for a cache and wrong here, and it is not a hypothetical corner:
        # a truncated write or a half-synced file is the single likeliest way
        # this store breaks, and it is exactly the shape that would sail through
        # the guard above. So the empty result is checked against the bytes.
        if data == {} and raw.strip() not in ("", "{}"):
            raise ApprovalStoreUnavailable(
                f"the approval store at {self._path} holds {len(raw)} bytes that "
                "did not parse as JSON. Reporting that as an empty queue would "
                "read as 'nothing to approve'."
            )
        if data in (None, ""):
            return {}
        if not isinstance(data, dict):
            raise ApprovalStoreUnavailable(
                f"the approval store at {self._path} is not a JSON object; "
                "refusing to guess at its contents"
            )
        return data

    def _expired(self, record: dict) -> bool:
        if record.get("status") != ActionStatus.PENDING.value:
            return False
        created = record.get("created_at")
        if not created:
            return False
        try:
            when = datetime.fromisoformat(created)
        except ValueError:
            return False
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        return self._now() - when > self._ttl

    def get(self, action_id: str) -> Action | None:
        record = self._read().get(action_id)
        if record is None:
            return None
        action = Action.from_dict(record)
        if self._expired(record):
            # Expiry is not rejection. Nobody said no; the answer never came,
            # and a reader asking why this did not run deserves that
            # distinction rather than a rejection nobody made.
            action.status = ActionStatus.REJECTED
            action.error = (
                f"expired after {self._ttl.total_seconds() / 3600:.0f}h without an answer"
            )
        return action

    def put(self, action: Action) -> None:
        try:
            with LockedJsonFile(self._path, exclusive=True, strict=True) as handle:
                data = handle.read()
                if not isinstance(data, dict):
                    data = {}
                data[action.action_id] = action.to_dict()
                handle.write(data)
        except ApprovalStoreUnavailable:
            raise
        except Exception as exc:
            raise ApprovalStoreUnavailable(
                f"cannot record the action at {self._path}: {exc}. "
                "An action that was not recorded must not be treated as pending."
            ) from exc

    def all(self) -> list[Action]:
        return [self.get(aid) for aid in self._read()]  # type: ignore[misc]

    def purge_resolved(self) -> int:
        """Drop actions that are finished, and return how many went.

        The queue is a work list, not an audit log. The receipt chain is what
        records what happened; keeping every completed action here would make
        ``pending()`` slower forever and tempt someone to read history out of
        the wrong place.
        """
        keep = {}
        removed = 0
        with LockedJsonFile(self._path, exclusive=True, strict=True) as handle:
            data = handle.read()
            if not isinstance(data, dict):
                return 0
            for action_id, record in data.items():
                status = record.get("status")
                if status == ActionStatus.PENDING.value and not self._expired(record):
                    keep[action_id] = record
                else:
                    removed += 1
            handle.write(keep)
        return removed
