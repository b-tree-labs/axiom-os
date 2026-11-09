# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The oldest sender an intake accepts, and how a sender says what it runs.

An intake declares a requirement such as ``collector-pkg>=1.17.1`` (``>=``,
``>`` or ``==``; no third-party parser, because every sender imports this) in
``AXIOM_INGEST_MIN_CLIENT`` (several are separated by ``;``). Every sender
names the distributions it runs in one header, ``X-Client-Versions:
collector-pkg/1.17.1 axiom-os-lm/0.67.5``. A sender below the requirement is
answered 426 with :func:`check`'s body, which names the requirement, what the
sender reported, and the command that updates it.

426 and not a 4xx the transmitter treats as definitive: those dead-letter the
rows, and an out-of-date node must never lose data for being out of date. A
sender that reports nothing is treated as older than any declared minimum,
because every sender recent enough to matter reports.
"""

from __future__ import annotations

import os
import re

_REQ = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*(>=|==|>)\s*([0-9][0-9A-Za-z.]*)\s*$")


def _version(text: str) -> tuple[int, ...] | None:
    """A release version as a tuple of its leading numbers, or ``None`` if it has none."""
    parts = []
    for piece in str(text).split("."):
        m = re.match(r"(\d+)", piece)
        if not m:
            break
        parts.append(int(m.group(1)))
    return tuple(parts) or None


def _cmp(a: tuple[int, ...], b: tuple[int, ...]) -> int:
    n = max(len(a), len(b))
    a, b = a + (0,) * (n - len(a)), b + (0,) * (n - len(b))
    return (a > b) - (a < b)

#: The requirement(s) an intake enforces. Unset: every sender is accepted.
MIN_CLIENT_ENV = "AXIOM_INGEST_MIN_CLIENT"
#: The command an out-of-date sender is told to run. Default: ``pip install -U '<req>'``.
UPDATE_COMMAND_ENV = "AXIOM_INGEST_UPDATE_COMMAND"
#: The header a sender names its distributions in.
HEADER = "X-Client-Versions"
#: How often a sender that was told to update asks again (an intake may relax its minimum).
UPDATE_RECHECK_S = 3600.0
#: The status an out-of-date sender is answered with.
STATUS = 426


def declared() -> str:
    return os.environ.get(MIN_CLIENT_ENV, "").strip()


def _reported(header: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for token in (header or "").split():
        name, _, version = token.partition("/")
        if name and version:
            out[name.lower()] = version
    return out


def check(header: str, requirement: str | None = None) -> dict | None:
    """``None`` when the sender is acceptable, otherwise the 426 body."""
    requirement = declared() if requirement is None else requirement.strip()
    if not requirement:
        return None
    reported = _reported(header)
    for raw in [r.strip() for r in requirement.split(";") if r.strip()]:
        m = _REQ.match(raw)
        if not m:
            continue  # a malformed declaration is the operator's to fix, not the sender's
        name, op, want = m.group(1), m.group(2), _version(m.group(3))
        have = reported.get(name.lower())
        ok = False
        got = _version(have) if have is not None else None
        if got is not None and want is not None:
            c = _cmp(got, want)
            ok = c >= 0 if op == ">=" else (c > 0 if op == ">" else c == 0)
        if not ok:
            command = os.environ.get(UPDATE_COMMAND_ENV) or f"pip install -U '{raw}'"
            return {
                "code": "update_required",
                "requirement": raw,
                "running": f"{name}/{have}" if have is not None else None,
                "command": command,
                "detail": (
                    f"this intake needs {raw}; this node reports "
                    f"{have or 'no version'}. Its data is kept on the node and "
                    "sent once it is updated."
                ),
            }
    return None


def client_versions(extra: tuple[str, ...] = ()) -> str:
    """This process's own ``X-Client-Versions`` value: the platform, the product, and ``extra``."""
    import importlib.metadata as md

    names: list[str] = ["axiom-os-lm"]
    try:
        from axiom.infra.branding import get_branding

        pkg = getattr(get_branding(), "package_name", "") or ""
        if pkg and pkg not in names:
            names.insert(0, pkg)
    except Exception:  # noqa: BLE001 - branding is optional here
        pass
    names += [n for n in extra if n not in names]
    tokens = []
    for name in names:
        try:
            tokens.append(f"{name}/{md.version(name)}")
        except md.PackageNotFoundError:
            continue
    return " ".join(tokens)


__all__ = ["HEADER", "MIN_CLIENT_ENV", "STATUS", "UPDATE_COMMAND_ENV", "UPDATE_RECHECK_S", "check", "client_versions", "declared"]
