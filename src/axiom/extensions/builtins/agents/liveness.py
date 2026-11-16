# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""What each agent last said, and when it said it would say it again.

The producer behind the agent-activity surface. Everything here is read from
something the node already writes down:

* ``lastReportAt`` is the LATEST of two independent records, because neither
  is complete and neither is wrong — see below.
* ``cadenceSeconds`` is ``heartbeat_interval`` from the agent's manifest, and
  ONLY when the manifest actually contains the key — see below.

TWO SOURCES, AND WHY BOTH. The shared dispatcher keeps
``agents/.background-service/state.json``, a map of extension name to the epoch
it last fired that agent. It is uniform and covers every agent in its roster.
Separately, some agents keep their own ledger — ``agents/rivet/heartbeat.jsonl``
and the like — which is a per-agent convention rather than a platform one, and
most agents keep nothing.

They disagree, materially. On this machine the dispatcher records ``release``
last fired 22 days ago while RIVET's own ledger shows it wrote 9 hours ago:
that agent is being fired by something other than the shared dispatcher. So
reading only the dispatcher reports a demonstrably live agent as dark, and
reading only the ledgers loses every agent that keeps none.

``lastReportAt`` is therefore the MOST RECENT evidence from either, which is
what the field actually claims — the last time we know this agent did
something. It is not an average and not a preference between the two.

What that does hide, stated because it should not be discovered later: an
agent the dispatcher has stopped firing but which still runs on its own timer
looks perfectly healthy here. "Last dispatched" and "last reported" are two
facts and this surface carries one field. The gap belongs to whatever reports
on the schedule substrate itself.

Nothing is inferred, and a fact the node does not hold is left out rather than
filled in. That is not fastidiousness: the surface this feeds is built on
silence never being health, and its classifier refuses to say anything about a
silence when no cadence was declared. Handing it a made-up cadence would
convert "nothing can be said about this quiet" into a confident count of missed
reports, which is the one failure the surface exists to prevent.

THE DEFAULT IS THE TRAP. ``AgentDef.heartbeat_interval`` is declared ``int =
300``, so the parsed object always carries a number and an agent that declared
no interval is indistinguishable from one that declared exactly five minutes.
Reading the parsed value would therefore invent a declaration for every agent
that never made one. So the manifest's ``[agent]`` table is re-read here and
the key's PRESENCE is what decides whether a cadence is reported at all.

``stoppedByOperator`` is the CONSENT RECORD, and leaving it out produced a
false finding on the first day this shipped.

The reasoning for omitting it was that the heartbeat dispatcher is one shared
service, so there is no per-agent stop an operator could have performed. That
is wrong. ``~/.axi/agents_consent.json`` is exactly a per-agent operator
decision: when ``decided`` is set, only the agents in ``enabled`` are ever
dispatched, and on this machine that list held one name. Five agents that
nobody had turned on were therefore reported as DARK — a surface built on
silence never being health, manufacturing six weeks of alarming silence out
of a choice somebody made on purpose.

Chosen silence is the one exception the classifier already had a state for,
and the producer simply was not telling it. Not reported:

* ``busy`` — nothing records an in-flight tick.

A CONFOUND WORTH KNOWING: because that dispatcher is shared, if it stops then
every agent goes quiet at once and this returns fourteen dark agents for one
cause. The surface will read that as fourteen findings. Surfacing the
dispatcher itself is the fix and it is not in this module yet.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: How far back from the end of a ledger to look for the final record. The
#: ledgers run to tens of thousands of lines (rivet passed 17,000), so they are
#: read from the tail rather than parsed whole. 64 KiB is far more than one
#: record and cheap enough to do for every agent on every request.
_TAIL_BYTES = 8192


def default_agents_dir() -> Path:
    """Where the heartbeat ledgers live."""
    root = os.environ.get("AXIOM_STATE_DIR") or os.environ.get("AXI_HOME")
    return (Path(root) if root else Path.home() / ".axi") / "agents"


def _last_record(ledger: Path) -> dict[str, Any] | None:
    """The final JSON object in a JSONL file, or None.

    Reads the tail rather than the file. A partial first line is expected when
    the window lands mid-record and is discarded by the parse, which is why
    every candidate is tried from the end rather than only the last one: a
    truncated final write would otherwise hide a perfectly good record sitting
    one line above it.
    """
    try:
        size = ledger.stat().st_size
        if size == 0:
            return None
        with ledger.open("rb") as fh:
            if size > _TAIL_BYTES:
                fh.seek(-_TAIL_BYTES, os.SEEK_END)
            window = fh.read()
    except OSError:
        return None

    for line in reversed(window.decode("utf-8", errors="replace").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            return record
    return None


def _dispatch_times(directory: Path) -> dict[str, float]:
    """Extension name to the epoch the shared dispatcher last fired it.

    Keyed the same way the manifests are, so this joins to the declared
    cadences with no name translation. That matters: the agent PRINCIPAL
    (``@tidy``, ``@rivet``) is derived from ``[sender].display_name`` and does
    not match its extension name, so a producer keying on the wrong one of the
    two silently reports every agent as never having run.
    """
    state = directory / ".background-service" / "state.json"
    try:
        raw = json.loads(state.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        name: float(value)
        for name, value in raw.items()
        if isinstance(value, (int, float)) and not isinstance(value, bool)
    }


def _declared_cadences() -> dict[str, int]:
    """``heartbeat_interval`` per agent, for the agents that DECLARE one.

    An agent missing from this map declared no interval. That is reported as an
    absence rather than as the parser's 300-second default, because the two
    mean opposite things to anything reasoning about silence.
    """
    try:
        from axiom.extensions.discovery import discover_extensions
    except Exception:
        return {}

    try:
        extensions = [e for e in discover_extensions() if e.agent is not None]
    except Exception:
        return {}

    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - Python < 3.11
        return {}

    cadences: dict[str, int] = {}
    for ext in extensions:
        root = getattr(ext, "root", None)
        if root is None:
            continue
        for filename in ("axiom-extension.toml", "neut-extension.toml"):
            manifest = Path(root) / filename
            if not manifest.exists():
                continue
            try:
                with manifest.open("rb") as fh:
                    raw = tomllib.load(fh)
            except (OSError, ValueError):
                break
            section = raw.get("agent")
            if isinstance(section, dict) and "heartbeat_interval" in section:
                value = section["heartbeat_interval"]
                if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                    cadences[ext.name] = value
            break
    return cadences


def _unconsented() -> set[str] | None:
    """Agents an operator has NOT enabled, or None when nothing was decided.

    ``None`` and an empty set mean different things and the caller must not
    conflate them. No recorded decision means dispatch is unrestricted, so no
    agent's silence has been chosen and nothing should be marked stopped. A
    recorded decision naming some agents means every OTHER agent was stopped
    on purpose, which is a fact about a person rather than about the agent.
    """
    try:
        from axiom.extensions.builtins.agents.consent import load_consent
    except Exception:
        return None
    try:
        consent = load_consent()
    except Exception:
        return None
    if not consent.decided:
        return None
    if consent.opted_out:
        return set()  # everything is stopped; the caller marks all of them
    return set(consent.enabled)


def _site() -> str:
    """The context half of ``@name:context``."""
    return os.environ.get("AXIOM_SITE") or "local"


def agent_liveness(*, agents_dir: Path | str | None = None) -> list[dict[str, Any]]:
    """Every agent this node knows about, with when it last reported.

    The roster is the UNION of three sources, because each catches something
    the others miss. A declared agent extension belongs here even with no
    record at all — "no report has ever arrived" is a finding, and dropping
    the agent would hide it behind a shorter list. An agent in the
    dispatcher's roster belongs whether or not this tree declares it, since
    something installed it. And an agent with its own ledger belongs even
    with neither of the other two: it wrote, so it ran.
    """
    directory = Path(agents_dir) if agents_dir is not None else default_agents_dir()
    cadences = _declared_cadences()
    dispatched = _dispatch_times(directory)
    consented = _unconsented()
    site = _site()

    ledgers: dict[str, Path] = {}
    try:
        for child in sorted(directory.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            ledger = child / "heartbeat.jsonl"
            if ledger.exists():
                ledgers[child.name] = ledger
    except OSError:
        pass

    # The principal for an extension, so a ledger kept under the agent's own
    # name joins to the extension that declares it. Without this, `release`
    # and `rivet` are two rows describing one agent.
    principals = _agent_principals()
    by_principal = {principals.get(ext, ext): ext for ext in set(cadences) | set(dispatched)}

    names = set(cadences) | set(dispatched)
    for ledger_name in ledgers:
        if ledger_name not in by_principal:
            names.add(ledger_name)

    out: list[dict[str, Any]] = []
    for name in sorted(names):
        principal = principals.get(name, name)
        status: dict[str, Any] = {"agent": f"@{principal}:{site}", "lastReportAt": None}

        # The most recent evidence from either record. Neither source alone is
        # honest here; see the module docstring.
        candidates: list[datetime] = []
        epoch = dispatched.get(name)
        if epoch is not None:
            candidates.append(datetime.fromtimestamp(epoch, tz=UTC))
        ledger = ledgers.get(principal) or ledgers.get(name)
        if ledger is not None:
            record = _last_record(ledger)
            raw = record.get("ts") if isinstance(record, dict) else None
            parsed = _parse_ts(raw)
            if parsed is not None:
                candidates.append(parsed)
        if candidates:
            status["lastReportAt"] = max(candidates).isoformat()

        cadence = cadences.get(name)
        if cadence is not None:
            # Present only when declared. Absent is a different claim.
            status["cadenceSeconds"] = cadence

        # A person decided this one does not run here. Set only when a
        # decision was actually recorded — with none, nothing has been chosen
        # and calling the silence chosen would invent the operator's intent.
        if consented is not None and name not in consented:
            status["stoppedByOperator"] = True

        out.append(status)
    return out


def _agent_principals() -> dict[str, str]:
    """Extension name to its agent's principal, from ``[sender].display_name``.

    ADR-066 derives the canonical principal from the display name rather than
    declaring it, so `hygiene` is `@tidy` and `release` is `@rivet`. An
    extension with no `[sender]` keeps its own name.
    """
    try:
        import tomllib

        from axiom.extensions.discovery import discover_extensions
    except Exception:
        return {}
    try:
        extensions = [e for e in discover_extensions() if e.agent is not None]
    except Exception:
        return {}

    out: dict[str, str] = {}
    for ext in extensions:
        root = getattr(ext, "root", None)
        if root is None:
            continue
        for filename in ("axiom-extension.toml", "neut-extension.toml"):
            manifest = Path(root) / filename
            if not manifest.exists():
                continue
            try:
                with manifest.open("rb") as fh:
                    raw = tomllib.load(fh)
            except (OSError, ValueError):
                break
            sender = raw.get("sender")
            if isinstance(sender, dict):
                display = sender.get("display_name")
                if isinstance(display, str) and display.strip():
                    out[ext.name] = display.strip().lower()
            break
    return out


def _parse_ts(value: Any) -> datetime | None:
    """An ISO 8601 stamp from a ledger, or None. Naive stamps are read as UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


__all__ = ["agent_liveness", "default_agents_dir"]
