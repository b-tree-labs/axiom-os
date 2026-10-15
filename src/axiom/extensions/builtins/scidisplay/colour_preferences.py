# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

""""Always draw this channel in blue", remembered.

A colour derived from a channel's name and its unit is the same answer every
time, which is most of what a code needs. What it cannot do is agree with a
person who has already decided. Someone who has read one channel off a blue line
for ten years should be able to say so once and have every figure after it obey — including the ones a colleague draws from the same account, and
the ones drawn next year.

So this is a file, not a session. Two lines of API:

    remember("Power", "blue")
    load()                       -> {"Power": "#0072b2"}

## What it is not

It is not a place for a colour that belongs to a FIGURE. A chart document
carries `series_colours` for that, and a document's colours travel with the
document — into a share link, into a paper, onto someone else's screen. This
file never leaves the machine it is on, so a colour that has to survive being
sent belongs in the document, and `load()` is only ever a default the document
can overrule.

## Failure

A preferences file that cannot be read is an error, not an empty dictionary.
Drawing in the derived colours while a person's own settings sit unread in a
file is the quiet kind of wrong this platform keeps finding: the figure looks
fine, and it is not the figure they asked for. Callers that must not fail are
expected to catch and SAY so.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from axiom.infra.paths import get_user_state_dir

from .chart_colour import NAMED_COLOURS, normalise_colour

__all__ = [
    "PreferencesError",
    "forget",
    "load",
    "path",
    "remember",
]

#: The table the preferences live under, so the file can grow other chart
#: settings later without this one having to move.
SECTION = "colours"

_FILE = "chart-preferences.toml"

_HEADER = """\
# Chart preferences.
#
# Written by `--always-colour CHANNEL=COLOUR` on a chart command. Hand edits
# are fine; the file is read, not parsed loosely, so a typo is reported rather
# than ignored.
#
# A colour here is a DEFAULT. A chart document that declares its own colours
# overrules it, because a document's colours travel with it and these do not.
"""


class PreferencesError(ValueError):
    """The preferences file exists and could not be used."""


def path() -> Path:
    """Where the preferences live. Never spelled as a literal elsewhere."""
    return get_user_state_dir() / _FILE


def load() -> dict[str, str]:
    """Every remembered colour, as ``{channel: "#rrggbb"}``.

    Raises :class:`PreferencesError` when the file exists and cannot be used.
    An absent file is not a failure — nobody has expressed a preference yet.
    """
    where = path()
    if not where.exists():
        return {}
    try:
        document = tomllib.loads(where.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PreferencesError(f"{where} could not be read: {exc}") from exc
    raw = document.get(SECTION) or {}
    if not isinstance(raw, dict):
        raise PreferencesError(
            f"{where}: [{SECTION}] must be a table of channel to colour"
        )
    out: dict[str, str] = {}
    for channel, colour in raw.items():
        try:
            out[str(channel)] = normalise_colour(colour)
        except ValueError as exc:
            raise PreferencesError(f"{where}: {channel}: {exc}") from exc
    return out


def _write(values: dict[str, str]) -> Path:
    where = path()
    where.parent.mkdir(parents=True, exist_ok=True)
    lines = [_HEADER, f"[{SECTION}]"]
    for channel in sorted(values):
        # Quoted keys: a channel is a name from somebody else's instrument and
        # routinely carries dots and colons, which bare TOML keys cannot.
        escaped = channel.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'"{escaped}" = "{values[channel]}"')
    body = "\n".join(lines) + "\n"
    # Written beside and moved into place, so an interrupted write never leaves
    # a half-file that the next read would report as a typo.
    scratch = where.with_suffix(".toml.writing")
    scratch.write_text(body, encoding="utf-8")
    scratch.replace(where)
    return where


def remember(channel: str, colour: str) -> str:
    """Always draw *channel* in *colour*. Returns the colour as stored.

    The colour is normalised on the way in, so the file holds ``#rrggbb`` and a
    name nobody can resolve is refused here rather than at the next chart.
    """
    name = str(channel).strip()
    if not name:
        raise PreferencesError("a channel with no name cannot be given a colour")
    stored = normalise_colour(colour)
    values = load()
    values[name] = stored
    _write(values)
    return stored


def forget(channel: str) -> bool:
    """Stop pinning *channel*. ``True`` if something was actually forgotten."""
    values = load()
    if str(channel).strip() not in values:
        return False
    del values[str(channel).strip()]
    _write(values)
    return True


def names() -> list[str]:
    """The colour names a person may type, for a message that has to list them."""
    return sorted(NAMED_COLOURS)
