# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The persisted capability series — what turns a claim into a number.

Every surface now publishes a content-free projection when a capability is
invoked, but a bus event nobody stores measures nothing. This is the store.

**Append-only JSONL under the state dir**, the same posture the action audit
chain takes, and for the same reason: it works on a laptop with no database.
Most installs are that laptop. A measurement that only functioned where Postgres
does would miss exactly the students, researchers and operators this is meant to
serve, and would flatter us by sampling only the nodes we run ourselves.

What it answers:

* **which capabilities have ever been reached for** — this feeds the discovery
  block, so using one drops it out and the block shrinks toward empty. That is
  the recursion closing: usage shapes the block, the block shapes usage, and
  nobody writes prose at either end.
* **how long into a session the first platform call happens, and whether one
  happens at all.** Measured against the session that built this: over a hundred
  tool calls, the platform reached for essentially never. That is the honest
  baseline, and it should stay reportable rather than become a thing we stop
  mentioning once the number improves.

The series inherits the projection's privacy guarantee by construction: only the
projection's own fields are written, so a caller passing ``args`` or ``result``
cannot turn this into the place content leaks back in.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Any

from axiom.infra.skill_dispatch import TELEMETRY_FIELDS

log = logging.getLogger(__name__)

#: Fields the series carries beyond the shared projection. ``session_id`` is what
#: makes "did this session use the platform at all" answerable; ``ts`` is what
#: makes "how long until it did" answerable. Neither carries content.
_SERIES_FIELDS: frozenset[str] = frozenset(TELEMETRY_FIELDS) | {"session_id", "ts"}


#: Relocates the state dir the series and the discovery block default to.
#: Operationally useful (state on a different volume), and it is also what
#: makes anything reading the series testable: without it, a test that built an
#: MCP server read the developer's real ``~/.axi`` and its output varied by
#: machine and by what that person had run this week.
STATE_DIR_ENV = "AXIOM_STATE_DIR"


def default_state_dir() -> Path:
    """Where the series lives when a caller does not say."""
    import os

    override = os.environ.get(STATE_DIR_ENV, "").strip()
    return Path(override) if override else Path.home() / ".axi"


def telemetry_path(state_dir: str | Path) -> Path:
    """Where the series lives under a state dir."""
    return Path(state_dir) / "telemetry" / "capabilities.jsonl"


def record_capability_event(
    event: Mapping[str, Any], *, state_dir: str | Path
) -> None:
    """Append one capability invocation to the series.

    Never raises. Telemetry is an observation about a turn, not part of it, and
    a full disk or an unwritable path must not fail somebody's conversation —
    the same contract the publisher holds.

    Only :data:`_SERIES_FIELDS` are written. A caller handing us ``args`` or
    ``result`` gets them dropped rather than persisted: the projection is
    content-free by construction and this is the second place that has to stay
    true, because a store is where a leak becomes permanent.
    """
    try:
        from axiom.infra.state import locked_append_jsonl

        if not telemetry_enabled():
            # Checked at the WRITE, not only at subscribe time. A process armed
            # before the operator declined would otherwise keep appending for
            # its whole lifetime, and the off switch would appear to work while
            # the file grew.
            return

        record = {k: v for k, v in dict(event).items() if k in _SERIES_FIELDS}
        record.setdefault("ts", time.time())
        if record.get("ts") is None:
            record["ts"] = time.time()

        path = telemetry_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        locked_append_jsonl(path, record)
    except Exception:  # noqa: BLE001 - see the docstring: never raise into a turn
        log.debug("capability telemetry not recorded", exc_info=True)


def _read(state_dir: str | Path) -> list[dict[str, Any]]:
    """Every readable record. A malformed line is skipped, not fatal.

    A series that refused to be read because one line was truncated by a crash
    would lose the whole measurement to protect a single row.
    """
    path = telemetry_path(state_dir)
    out: list[dict[str, Any]] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            out.append(record)
    return out


def capability_usage(*, state_dir: str | Path) -> set[str]:
    """Every capability ever reached for on this install.

    Feeds :func:`axiom.memory.rendering.capabilities_for_block`, whose block is
    the difference between this and what is installed.

    A FAILED invocation counts. Discovery is about whether someone knew to try
    the capability at all; one that was found and then failed is a bug, not a
    discovery gap, and hiding it here would send us to fix the wrong thing.
    """
    return {
        str(r.get("tool_name"))
        for r in _read(state_dir)
        if str(r.get("tool_name") or "")
    }


#: Surfaces where a PERSON is present. A background runner touching every
#: capability is not somebody discovering them — the runner surface exists
#: precisely because nobody was at a terminal to be told.
INTERACTIVE_SURFACES: frozenset[str] = frozenset({"chat", "cli", "mcp"})

#: Uses before a capability counts as part of a repertoire. One is a brush
#: against it, and treating that as discovery let a single stray invocation
#: hide a capability permanently — and let "call everything once" score as
#: total discovery.
MIN_USES_FOR_DISCOVERY = 2

#: How far back usage counts. Without decay the loop is a one-way ratchet that
#: can never re-surface a capability nobody has reached for in months, and a
#: ratchet cannot self-correct.
DISCOVERY_WINDOW_SECONDS = 60 * 60 * 24 * 90


def discovered_capabilities(
    *,
    state_dir: str | Path,
    principal: str | None = None,
    now: float | None = None,
    window_seconds: float = DISCOVERY_WINDOW_SECONDS,
    min_uses: int = MIN_USES_FOR_DISCOVERY,
    interactive_only: bool = True,
) -> set[str]:
    """Capabilities that are part of someone's repertoire.

    Deliberately stricter than "appears in the series", because adversarial
    stress broke that definition four ways:

    * **Per principal.** Usage is install-wide but discovery happens in a head.
      A student joining a shared node was shown nothing because a colleague had
      already used everything. Pass ``principal`` and they get their own view.
    * **Repeat use.** One invocation is a brush against a capability, not a
      repertoire. Counting it let a single stray call hide something forever,
      and made "loop over the registry once" a perfect score.
    * **Decay.** Usage outside the window stops counting, so a capability nobody
      has reached for in months returns to the block. A one-way ratchet cannot
      self-correct.
    * **Interactive surfaces only.** A heartbeat agent touching everything is
      not discovery.

    **What this still cannot tell you**, and the reason the loop is not yet
    evidence of efficacy: a capability leaves the block whether or not the block
    is why it got used. Shrinkage is consistent with the block working and with
    it being ignored entirely. Separating those is a counterfactual — the same
    task run with and without — and no amount of filtering here substitutes for
    it.
    """
    now = time.time() if now is None else now
    cutoff = now - window_seconds
    counts: dict[str, int] = {}
    for record in _read(state_dir):
        name = str(record.get("tool_name") or "")
        if not name:
            continue
        if principal is not None and str(record.get("principal") or "") != principal:
            continue
        if interactive_only and str(record.get("surface") or "") not in INTERACTIVE_SURFACES:
            continue
        ts = record.get("ts")
        if isinstance(ts, (int, float)) and float(ts) < cutoff:
            continue
        counts[name] = counts.get(name, 0) + 1
    return {name for name, n in counts.items() if n >= min_uses}


def session_discovery_stats(
    *,
    state_dir: str | Path,
    sessions: Collection[str],
    started_at: Mapping[str, float] | None = None,
) -> dict[str, Any]:
    """The two headline numbers, per session.

    ``sessions_with_a_call`` over ``sessions`` is the share of sessions that
    used the platform at all — the metric this session scores zero on.
    ``first_call_seconds`` is how long each session took to get there, which is
    the one that should fall as discovery improves.

    Sessions are passed in rather than derived from the series, because a
    session that made NO call leaves no record — and those are precisely the
    ones worth counting. Deriving the denominator from the data would silently
    drop every session we most need to see.
    """
    started_at = dict(started_at or {})
    first_ts: dict[str, float] = {}
    for record in _read(state_dir):
        sid = str(record.get("session_id") or "")
        ts = record.get("ts")
        if not sid or not isinstance(ts, (int, float)):
            continue
        if sid not in first_ts or ts < first_ts[sid]:
            first_ts[sid] = float(ts)

    first_call_seconds: dict[str, float] = {}
    for sid in sessions:
        if sid in first_ts and sid in started_at:
            first_call_seconds[sid] = round(first_ts[sid] - started_at[sid], 3)

    return {
        "sessions": len(sessions),
        "sessions_with_a_call": sum(1 for s in sessions if s in first_ts),
        "first_call_seconds": first_call_seconds,
    }


#: The gateway's own observation topic. Every CLI and MCP invocation goes
#: through :func:`~axiom.infra.skill_dispatch.invoke_capability`, which
#: publishes here with the content-free projection already applied.
GATEWAY_TOPIC = "tool.post_invoke"


def subscribe_capability_telemetry(eventbus: Any, *, state_dir: str | Path) -> Any:
    """Persist capability invocations from every surface.

    **Two topics, because the surfaces genuinely publish on two.** Chat
    dispatches beneath ``invoke_capability`` — its turn loop needs the gateway's
    full payload on its own per-agent bus — so it publishes the shared
    projection on :data:`~axiom.infra.skill_dispatch.TELEMETRY_TOPIC` instead.
    CLI and MCP go through the chokepoint and publish on
    :data:`GATEWAY_TOPIC`.

    Subscribing to only the first is what the store did at first, and it meant
    every CLI and MCP invocation on every install was measured and then dropped.
    The gap was invisible because the surface exercised in the store's own tests
    was the one that worked.

    **The invariant this rests on:** a surface publishes on exactly ONE of these
    topics per invocation, per bus. It holds because chat's gateway dispatch
    goes to its per-agent bus while its projection goes to the process bus — not
    by accident, and `test_chat_does_not_double_publish_on_one_bus` is what
    keeps it true. If it ever broke, usage would inflate silently and a
    capability would drop out of the discovery block after a single real call.

    Subscribed ``fail_mode="ignore"`` on purpose. A subscriber that raises puts
    the whole payload back on the bus inside ``bus.errors`` — which is precisely
    how a content-free design springs a leak, and it would be this store, the
    one built to make the measurement safe, that caused it.

    Returns both subscriptions, so a caller can unsubscribe either.
    """
    from axiom.infra.skill_dispatch import TELEMETRY_TOPIC

    def _handler(_subject: str, payload: Mapping[str, Any]) -> None:
        if isinstance(payload, Mapping):
            record_capability_event(payload, state_dir=state_dir)

    return [
        eventbus.subscribe(
            topic, _handler, fail_mode="ignore", source="capability-telemetry"
        )
        for topic in (TELEMETRY_TOPIC, GATEWAY_TOPIC)
    ]


#: Subscriptions already made, keyed by (id(bus), state_dir). Startup paths get
#: called more than once in practice, and a second subscription would record
#: every invocation twice — silently inflating the very number this exists to
#: report honestly.
_ENABLED: dict[tuple[int, str], Any] = {}


#: Env var that declines the series. Anything in :data:`_OFF_VALUES` switches
#: it off; unset means on.
TELEMETRY_ENV = "AXIOM_CAPABILITY_TELEMETRY"

_OFF_VALUES: frozenset[str] = frozenset({"0", "off", "false", "no", "disabled"})


def telemetry_enabled(env: Mapping[str, str] | None = None) -> bool:
    """Whether the series should be collected on this install.

    The series is content-free and never leaves the machine, and it is still the
    operator's to decline — a measurement with no off switch is one somebody has
    to uninstall the platform to escape, and this runs on other people's
    laptops. Defaults to on, because a measurement nobody turns on measures
    nothing, and the honest number we most need is from installs we do not
    administer.
    """
    import os

    source = os.environ if env is None else env
    return str(source.get(TELEMETRY_ENV, "")).strip().lower() not in _OFF_VALUES


def enable_capability_telemetry(
    *, state_dir: str | Path | None = None, eventbus: Any = None
) -> Any:
    """Turn the loop on. Idempotent, and safe to call from any startup path.

    Defaults to the node-durable state dir and the process bus, so a caller that
    knows nothing about either still gets a working measurement.

    Returns ``None`` when the operator has declined the series, so a caller can
    tell "off" from "armed" without inspecting the environment itself.
    """
    from axiom.infra.bus import get_default_eventbus

    if not telemetry_enabled():
        return None

    resolved_dir = Path(state_dir) if state_dir is not None else default_state_dir()
    bus = eventbus if eventbus is not None else get_default_eventbus()
    key = (id(bus), str(resolved_dir))
    existing = _ENABLED.get(key)
    if existing is not None:
        return existing
    sub = subscribe_capability_telemetry(bus, state_dir=resolved_dir)
    _ENABLED[key] = sub
    return sub


def installed_capabilities(registry: Any) -> list[Any]:
    """Every capability this install actually has.

    Honest to the install by construction: the block can only ever advertise
    what the registry holds, so a chat-only node cannot promise the data verbs
    a fuller one carries.
    """
    for attr in ("all", "values", "specs"):
        getter = getattr(registry, attr, None)
        if callable(getter):
            try:
                out = getter()
            except Exception:  # noqa: BLE001 - a registry that will not enumerate
                return []
            # `SkillRegistry.specs()` returns name -> spec. Listing a mapping
            # yields its KEYS, and a name has no description, so every
            # capability was silently dropped and the block rendered empty
            # against a real install. Found by simulation; unit fixtures
            # returned lists and could not have caught it.
            if isinstance(out, Mapping):
                return list(out.values())
            return list(out)
    return []


def discovery_block(
    registry: Any,
    *,
    state_dir: str | Path,
    principal: str | None = None,
    budget_lines: int = 14,
) -> str:
    """The whole loop in one call: installed, minus used, rendered.

    This is what a startup path invokes to produce the block an assistant reads.
    Returns "" when there is no gap left, which is the end state worth having:
    discovery succeeded, so it stops spending context on every session.
    """
    from axiom.memory.rendering import capabilities_for_block, render_capability_block

    # discovered_capabilities, not capability_usage: the block must not be
    # emptied by a colleague's usage, a heartbeat agent, or a single stray call.
    used = discovered_capabilities(state_dir=state_dir, principal=principal)
    entries = capabilities_for_block(installed_capabilities(registry), used=used)
    return render_capability_block(entries, budget_lines=budget_lines)


#: Calls needed before a percentile is reported. A p95 over four calls is the
#: fourth-slowest call wearing a percentile's name, and once printed it gets
#: quoted.
MIN_LATENCY_SAMPLES = 30


def observed_latency(
    *,
    state_dir: str | Path,
    capability: str | None = None,
    surfaces: Collection[str] | None = None,
    min_samples: int = MIN_LATENCY_SAMPLES,
) -> dict[str, Any]:
    """What capability calls actually cost on this install.

    The governance-overhead benchmark reports its cost as a percentage of "a
    typical tool call (~100 ms)" — a denominator nobody measured. This is the
    one that was measured, on the machine making the claim.

    Returns ``n`` always and the percentiles only once there are enough samples
    to mean anything; below that the percentiles are ``None`` and ``note`` says
    why. Returning a shaped number from an unshaped sample is how an assumed
    figure gets laundered into a measured one.
    """
    values = [
        float(r["latency_ms"])
        for r in _read(state_dir)
        if isinstance(r.get("latency_ms"), (int, float))
        and (capability is None or str(r.get("tool_name") or "") == capability)
        and (surfaces is None or str(r.get("surface") or "") in set(surfaces))
    ]
    values.sort()
    n = len(values)
    if n < min_samples:
        return {
            "n": n,
            "p50_ms": None,
            "p95_ms": None,
            "p99_ms": None,
            "note": f"insufficient samples: {n} < {min_samples}",
        }

    def _pct(fraction: float) -> float:
        # Nearest-rank. Interpolating between two observed calls invents a
        # latency that no call had, which is the wrong trade for a series whose
        # whole point is that the numbers are real.
        index = max(0, math.ceil(fraction * n) - 1)
        return values[index]

    return {
        "n": n,
        "p50_ms": _pct(0.50),
        "p95_ms": _pct(0.95),
        "p99_ms": _pct(0.99),
        "note": "",
    }


def governance_share(
    *, overhead_us: float, observed: Mapping[str, Any]
) -> dict[str, Any]:
    """What fraction of a real call the governance layers account for.

    ``overhead_us`` is the benchmark's governed-minus-bare delta; ``observed``
    is an :func:`observed_latency` result.

    **Two denominators, and they are not interchangeable.** The benchmark's
    headline ratio divides by BARE work — the action body with governance
    stubbed out — so it answers "how much longer does governance make this
    take". An observed call already CONTAINS the overhead, so dividing by it
    answers "how much of what the user waited for was governance". Both read as
    "percent overhead" and they differ by exactly the overhead. Both are
    returned, named, so neither can be quoted as the other.

    Returns ``None`` shares when the sample is too small, rather than falling
    back to the assumed 100 ms — a fallback would reproduce the invented
    denominator while wearing the word "observed".

    The product inherits the weaker of its two inputs: the benchmark is a single
    machine, single process, one run, and the observed series is whatever this
    install happened to do. Neither is a population estimate.
    """
    p50 = observed.get("p50_ms")
    note = (
        "share_of_observed divides by the measured call (which contains the "
        "overhead); share_of_bare_work divides by the call minus the overhead, "
        "matching the benchmark's own denominator"
    )
    if not isinstance(p50, (int, float)) or p50 <= 0:
        return {
            "share_of_observed": None,
            "share_of_bare_work": None,
            "observed_p50_ms": p50,
            "overhead_ms": round(overhead_us / 1000.0, 4),
            "n": observed.get("n", 0),
            "note": observed.get("note") or "insufficient observed latency",
        }

    overhead_ms = overhead_us / 1000.0
    bare_ms = float(p50) - overhead_ms
    return {
        "share_of_observed": overhead_ms / float(p50),
        # Undefined when the overhead exceeds the whole observed call: that
        # means the benchmark and the series disagree about what is being
        # measured, and a negative or infinite "percent overhead" would hide
        # the disagreement behind a number.
        "share_of_bare_work": (overhead_ms / bare_ms) if bare_ms > 0 else None,
        "observed_p50_ms": p50,
        "overhead_ms": round(overhead_ms, 4),
        "n": observed.get("n", 0),
        "note": note,
    }
