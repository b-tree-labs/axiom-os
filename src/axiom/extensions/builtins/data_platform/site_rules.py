# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A site's cross-channel rules, read as DATA and never imported as code.

## Why not an entry point, which is how normalizers arrive

Because normalizer discovery says not to, and the reason applies here with more
force. From `conformance/discovery.py`:

    one conform process reads every tenant's bronze rows in one interpreter, so
    a normalizer contributed by a site package would be one institution's code
    executing against another institution's data

Entry points are honoured only for distributions that declare portfolio
membership — platform code. A site package is exactly what that boundary
excludes, and these rules are *more* dangerous than a normalizer, not less: a
verdict NULLs a stored value. Importing a site's loader into the conform process
to obtain them would open the door that boundary exists to keep shut.

So a site declares its rules and the platform reads them. The format is the
platform's (`company.rules_from`), the judgement is the site's, and nothing
crosses but JSON.

## Where they live, and why there

``<state_dir>/fault-rules/<site>.json`` — beside the connector registry that
already tells conform which site a connector belongs to. A per-site file rather
than one shared file so that installing, correcting or withdrawing one site's
declaration cannot touch another's, and so the absence of a file is a clean
"this site has declared nothing" rather than a missing key somebody has to
interpret.

The shape::

    {"unit_suffixes": ["degc", "w"],
     "rules": [{"kind": "...", "reason": "company....", ...}]}

``unit_suffixes`` is the site's, because which units its thresholds carry is a
fact about its instruments. The platform still enforces that a threshold key
bears one of them.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Directory under the state dir holding one declaration per site.
RULES_DIR = "fault-rules"


def declaration_path(site: str, state_dir: Path | str | None) -> Path:
    """Where `site`'s declaration would be. Not required to exist."""
    root = Path(state_dir).expanduser() if state_dir else Path("~/.axi").expanduser()
    return root / RULES_DIR / f"{site}.json"


def rules_for_site(site: str, state_dir: Path | str | None) -> list[Any]:
    """`site`'s declared rules, or `[]` when it has declared none.

    Raises on a declaration that exists and is malformed, because a site that
    wrote a rule believes its instrument is being checked. The caller counts the
    failure per site and keeps going, so one bad declaration cannot stop another
    tenant's rows from conforming.
    """
    path = declaration_path(site, state_dir)
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise ValueError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path} is not valid JSON: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"{path} must hold an object with a 'rules' list")
    entries = raw.get("rules") or []
    if not entries:
        return []
    suffixes = tuple(str(x) for x in (raw.get("unit_suffixes") or ()))

    from .company import rules_from

    return rules_from(entries, unit_suffixes=suffixes)


def any_declared(state_dir: Path | str | None) -> bool:
    """Whether ANY site has declared rules, in one directory listing.

    Asked before the conform pass wraps its upsert at all. Validation buffers a
    frame at a time, and while that is cheap it is not free — an install where
    nobody has declared anything should pay exactly nothing and behave exactly as
    it did before any of this existed. Checking is one `iterdir`; guessing would
    cost every row of every pass forever.
    """
    root = declaration_path("x", state_dir).parent
    try:
        return any(p.suffix == ".json" for p in root.iterdir())
    except OSError:
        return False


def lookup(state_dir: Path | str | None):
    """A `site -> rules` callable that reads each declaration once.

    Cached because a conform pass judges one frame per instant and would
    otherwise re-read and re-parse the same small file hundreds of thousands of
    times. The cache lives for the pass, so a declaration corrected between
    passes takes effect on the next one without a restart.
    """
    seen: dict[str, list[Any]] = {}

    def _for(site: str) -> list[Any]:
        if site not in seen:
            seen[site] = rules_for_site(site, state_dir)
        return seen[site]

    return _for


__all__ = ["RULES_DIR", "any_declared", "declaration_path", "lookup", "rules_for_site"]
