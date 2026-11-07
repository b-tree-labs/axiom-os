# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Who arrival is for, declared rather than guessed.

Walking stage 0 found that the digest refuses to send without a recipient
— correctly, since a digest with nobody to reach is not a send — and that
nothing anywhere names one. So arrival was blocked on a fact the platform
had no place to record.

It also found that a manifest-declared schedule (``[[extension.schedule]]``)
carries no params, so a cadence armed that way cannot supply a recipient
either. The audience therefore has to be *declared state*, not an argument.

The shape is deliberately the backup policy's, down to the file layout
(``<state>/receipts/digest_audience.toml``), because that is the pattern
this platform already uses for "a cadence a deployment owns": a dataclass,
a validator that returns errors rather than raising, a loader that returns
``None`` when nothing is declared, and a saver that writes TOML a person
can read and edit. A second shape for the same job would be a second thing
to learn.

Two rules worth stating, both the construct's:

**Absence is a declaration too.** No file means "nobody has said", which
is different from "nobody wants this", and the digest's refusal names the
file rather than saying "recipient is required" and stopping. A person who
reads the refusal should know what to write and where.

**A cadence a deployment owns is not a cadence we hardcode.** The schedule
string lives here rather than in the extension manifest, because 07:00
daily is a choice about somebody's morning, not a property of the software.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from axiom.infra.paths import get_user_state_dir

#: The cadence a deployment gets if it declares an audience and no schedule.
#: Daily, in the morning, because the digest is a day's worth of context
#: rather than an alert — and because a cadence nobody chose should be the
#: least surprising one, not the most frequent.
#:
#: A bare cron string, which is the dialect ``schedule.formats.parse``
#: detects. Not ``"cron:0 7 * * *"`` — a prefixed form fails detection, and
#: the backup policy's schedule strings are bare for the same reason.
DEFAULT_SCHEDULE = "0 7 * * *"


@dataclass(frozen=True)
class Audience:
    """Who the digest reaches, how often, and where it points them."""

    #: Principals the digest is sent to. One send per recipient, so
    #: suppression stays per-person (see ``skills.digest._dedup_key``).
    recipients: tuple[str, ...] = ()
    #: A PULSE cadence string — ``cron:...`` or ``interval:...``.
    schedule: str = DEFAULT_SCHEDULE
    #: The link to the surface. Empty means omit it, never fake it.
    where: str = ""
    #: Scope, as the API route scopes. Empty = every site this node sees.
    site: str = ""
    #: False keeps the declaration on disk while stopping the sends, which
    #: is what somebody going on leave wants and is not the same as
    #: deleting who the audience is.
    enabled: bool = True
    #: Unknown keys, kept so a round trip does not silently drop what a
    #: future version wrote.
    extra: dict = field(default_factory=dict)


def validate_audience(audience: Audience) -> list[str]:
    """Problems with a declaration, as sentences. Never raises.

    Returned rather than thrown because the caller is usually about to
    print them next to the file they came from, and because a half-valid
    policy should report every problem at once instead of one per run.
    """
    errors: list[str] = []
    if audience.enabled and not audience.recipients:
        errors.append(
            "enabled = true but recipients is empty — an audience with nobody "
            "in it cannot receive anything"
        )
    for who in audience.recipients:
        if not who.startswith("@"):
            errors.append(f"recipient {who!r}: principals are written @name or @name:context")
    if not audience.schedule:
        errors.append("schedule is empty — write a cadence such as '0 7 * * *'")
    else:
        # The schedule extension's own codec, not data_platform's
        # `cadence_for` wrapper around it. Reaching into another
        # consumer's module for generic schedule vocabulary is how that
        # vocabulary ends up owned by whoever needed it first.
        from axiom.extensions.builtins.schedule.formats import parse as parse_cadence

        try:
            parse_cadence(audience.schedule)
        except Exception as exc:  # noqa: BLE001 — report, never raise
            errors.append(f"schedule: not a cadence this platform can parse ({exc})")
    if audience.where and not audience.where.startswith(("http://", "https://")):
        errors.append(
            f"where {audience.where!r}: a link the digest can print, or empty — "
            "a digest that points somewhere wrong is worse than one pointing nowhere"
        )
    return errors


def audience_path(*, state_dir: Path | None = None) -> Path:
    """Where the declaration lives."""
    base = state_dir or get_user_state_dir()
    return base / "receipts" / "digest_audience.toml"


def load_audience(*, state_dir: Path | None = None) -> Audience | None:
    """The declared audience, or ``None`` when nobody has declared one.

    ``None`` is not an error and not an empty audience. It means the
    question has not been answered, which is what the digest's refusal
    should say.
    """
    path = audience_path(state_dir=state_dir)
    if not path.exists():
        return None
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    blob = data.get("digest_audience") or {}
    known = {"recipients", "schedule", "where", "site", "enabled"}
    raw_recipients = blob.get("recipients") or []
    if isinstance(raw_recipients, str):
        # One recipient written without brackets. Accept it rather than
        # silently reading a string as a list of characters.
        raw_recipients = [raw_recipients]
    return Audience(
        recipients=tuple(str(r).strip() for r in raw_recipients if str(r).strip()),
        schedule=str(blob.get("schedule") or DEFAULT_SCHEDULE),
        where=str(blob.get("where") or ""),
        site=str(blob.get("site") or ""),
        enabled=bool(blob.get("enabled", True)),
        extra={k: v for k, v in blob.items() if k not in known},
    )


def save_audience(audience: Audience, *, state_dir: Path | None = None) -> Path:
    """Persist the declaration as TOML; returns the written path."""
    path = audience_path(state_dir=state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    def _s(value: str) -> str:
        return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'

    lines = ["[digest_audience]"]
    lines.append(f"enabled = {'true' if audience.enabled else 'false'}")
    lines.append("recipients = [" + ", ".join(_s(r) for r in audience.recipients) + "]")
    lines.append(f"schedule = {_s(audience.schedule)}")
    if audience.where:
        lines.append(f"where = {_s(audience.where)}")
    if audience.site:
        lines.append(f"site = {_s(audience.site)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def no_audience_declared(*, state_dir: Path | None = None) -> str:
    """The sentence a refusal uses. Names the file, so the reader knows
    what to write and where rather than only that something is missing."""
    return (
        "no recipient passed and no audience declared — write one at "
        f"{audience_path(state_dir=state_dir)} (a [digest_audience] table with "
        'recipients = ["@someone"]), or pass recipient= for a one-off'
    )


__all__ = [
    "DEFAULT_SCHEDULE",
    "Audience",
    "audience_path",
    "load_audience",
    "no_audience_declared",
    "save_audience",
    "validate_audience",
]
