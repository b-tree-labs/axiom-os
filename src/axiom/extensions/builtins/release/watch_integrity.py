# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""A watcher that is watching nothing must say so.

A CI watcher reported ``all_green`` on 17,513 of 17,545 heartbeats,
including one taken twenty minutes before somebody sat down and counted 54
failures in a fortnight. It was not broken and it was not asleep. Its watch
list named a PUBLIC MIRROR — real, pushed daily by the mirror job, and
therefore looking perfectly alive — and a repository that 404s. A second
config elsewhere defaulted to a third name that also 404s. Three repository
identities, none of them the ones anybody pushes to.

The loader's own comment said bad URLs are "silently skipped". That is the
whole defect in four words. Skipping is what turned a configuration mistake
into a green light, and a green light is the one output nobody investigates.

So the rule this module exists to enforce:

    **Green requires having looked. Zero observations is not zero problems.**

Three states, and the third is the one that was missing:

* ``green``  — targets resolved, pipelines observed, none failing.
* ``red``    — pipelines observed, some failing.
* ``blind``  — nothing observed, or a configured target did not resolve.
  Not a degree of green. A separate answer, with its own reason attached.

And because the commonest cause is a repo that MOVED rather than one that
never existed, resolution reports the new name when a host redirects, and
drift compares what is watched against what is actually being pushed to.
A rename should produce a sentence naming both names, not a silence.

Salvaged from ``feat/rivet-retro-and-watch-integrity`` (closed draft PR #1470,
commits a082fcf44 and 13e7022ea), where it was written and never wired in. The
heartbeat, ``ci_health`` and the outside watch now conclude through it.

No I/O here. The caller supplies a resolver, which is what makes every one
of these rules testable — and an untested rule about silence is the thing
that failed in the first place.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

#: What resolving a watch target concluded.
LIVE, MOVED, MISSING, UNPARSEABLE, UNREACHABLE = (
    "live", "moved", "missing", "unparseable", "unreachable",
)

#: What a heartbeat may conclude. `blind` is the one that was missing.
GREEN, RED, BLIND = "green", "red", "blind"

#: Resolutions that mean this target contributes no observation.
NOT_OBSERVING = (MOVED, MISSING, UNPARSEABLE, UNREACHABLE)


@dataclass(frozen=True)
class WatchTarget:
    """One entry from one of RIVET's watch configs."""

    identifier: str
    #: which config named it, so a fix has an address
    source: str = ""
    provider: str = "github"
    host: str = ""
    ref: str = "main"


@dataclass(frozen=True)
class Resolution:
    """What the host said about a target."""

    target: WatchTarget
    state: str
    resolved_to: str = ""
    why: str = ""

    @property
    def observing(self) -> bool:
        return self.state == LIVE

    def describe(self) -> str:
        where = f" (from {self.target.source})" if self.target.source else ""
        if self.state == MOVED:
            return (f"{self.target.identifier}{where} has MOVED to {self.resolved_to} — "
                    f"the watch list still names the old one, so it is watching nothing")
        if self.state == MISSING:
            return f"{self.target.identifier}{where} does not exist{': ' + self.why if self.why else ''}"
        if self.state == UNPARSEABLE:
            return f"{self.target.identifier}{where} is not a repository reference"
        if self.state == UNREACHABLE:
            return f"{self.target.identifier}{where} could not be reached: {self.why}"
        return f"{self.target.identifier}{where} is live"


def resolve_targets(
    targets: Sequence[WatchTarget],
    resolve: Callable[[WatchTarget], str | None],
) -> list[Resolution]:
    """Resolve every target, and never drop one.

    ``resolve`` returns the canonical identifier the host reports, or None
    when there is nothing there. It may raise; a host being down is
    ``unreachable``, which is distinct from a repo being gone — one is a
    problem with the network and the other with the configuration, and
    collapsing them sends somebody to the wrong place.
    """
    out: list[Resolution] = []
    for t in targets:
        if not t.identifier or "/" not in t.identifier:
            out.append(Resolution(t, UNPARSEABLE))
            continue
        try:
            actual = resolve(t)
        except Exception as exc:  # noqa: BLE001 - any transport failure
            out.append(Resolution(t, UNREACHABLE, why=f"{type(exc).__name__}: {exc}"))
            continue
        if not actual:
            out.append(Resolution(t, MISSING))
        elif actual != t.identifier:
            out.append(Resolution(t, MOVED, resolved_to=actual))
        else:
            out.append(Resolution(t, LIVE))
    return out


@dataclass
class Observation:
    """What a heartbeat is entitled to claim."""

    state: str
    reasons: list[str] = field(default_factory=list)
    targets_live: int = 0
    targets_total: int = 0
    pipelines_seen: int = 0

    @property
    def all_green(self) -> bool:
        """Only ever true when something was actually looked at.

        The property RIVET got wrong for 17,513 heartbeats. It is computed
        rather than set, so there is no code path that can assert green
        without an observation behind it.
        """
        return self.state == GREEN

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "all_green": self.all_green,
            "reasons": list(self.reasons),
            "targets_live": self.targets_live,
            "targets_total": self.targets_total,
            "pipelines_seen": self.pipelines_seen,
        }


def observe(
    resolutions: Sequence[Resolution],
    *,
    pipelines_seen: int,
    failures_seen: int,
) -> Observation:
    """The honest conclusion of one heartbeat."""
    live = sum(1 for r in resolutions if r.observing)
    broken = [r for r in resolutions if r.state in NOT_OBSERVING]
    reasons = [r.describe() for r in broken]

    if not resolutions:
        return Observation(BLIND, ["no repositories are configured to watch"], 0, 0, 0)
    if broken:
        # Even one unresolved target means the green is partial, and a
        # partial green reads as a whole one. Say blind and name it.
        return Observation(BLIND, reasons, live, len(resolutions), pipelines_seen)
    if pipelines_seen == 0:
        return Observation(
            BLIND,
            ["every target resolved, but no pipelines were observed — "
             "check the ref being watched, or whether CI runs at all here"],
            live, len(resolutions), 0,
        )
    if failures_seen:
        return Observation(RED, [], live, len(resolutions), pipelines_seen)
    return Observation(GREEN, [], live, len(resolutions), pipelines_seen)


def drift(
    resolutions: Sequence[Resolution],
    pushed_to: Iterable[str],
) -> list[str]:
    """Repositories being pushed to that nobody is watching.

    Resolution catches a target that broke. This catches the other
    direction — work moving to a repository the watch list never learnt
    about, which is what a migration or a fork looks like from here, and
    which no amount of checking the existing entries would reveal.
    """
    watched = {r.resolved_to or r.target.identifier for r in resolutions}
    return sorted({p for p in pushed_to if p and p not in watched})


def render(observation: Observation, drifted: Sequence[str] = ()) -> str:
    """One heartbeat, in words that distinguish quiet from blind."""
    if observation.state == GREEN:
        head = (f"green — {observation.pipelines_seen} pipeline(s) across "
                f"{observation.targets_live} repo(s), none failing")
    elif observation.state == RED:
        head = f"RED — {observation.pipelines_seen} pipeline(s) observed, some failing"
    else:
        head = (f"BLIND — watching {observation.targets_live}/{observation.targets_total} "
                f"repo(s), {observation.pipelines_seen} pipeline(s) seen. "
                f"This is not a clean run; it is no run.")
    lines = [head]
    lines += [f"  · {r}" for r in observation.reasons]
    if drifted:
        lines.append("  · being pushed to but not watched: " + ", ".join(drifted))
    return "\n".join(lines)


__all__ = [
    "BLIND", "GREEN", "LIVE", "MISSING", "MOVED", "NOT_OBSERVING", "RED",
    "UNPARSEABLE", "UNREACHABLE",
    "Observation", "Resolution", "WatchTarget",
    "drift", "observe", "render", "resolve_targets",
]
