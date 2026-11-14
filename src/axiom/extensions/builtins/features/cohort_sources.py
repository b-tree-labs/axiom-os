# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Large explicit cohorts: bulk import, and external groups a cohort follows (LOOP-F16).

A cohort's members are kept in the record (``flags.cohorts``), never in a flag, and
counted. They arrive three ways:

* listed when the cohort is set (``principals`` / ``accounts``);
* imported in bulk from a file or a list (``flags.cohort_import``): one member per
  line, the first column of a CSV, or a JSON list; a header line naming the column is
  skipped, as are blank lines and ``#`` comments;
* followed from an external group through a **source**: a cohort with
  ``source = {kind, ...}`` takes its principals from the source when it is set and again
  every ``refresh_every`` (the platform scheduler's ``flags.rollout_tick`` runs the
  refresh; ``flags.cohort_refresh`` runs it now). A refresh that fails keeps the last
  members and records why.

Sources are pluggable (:func:`register_source`). The platform ships ``directory``: a group
of the identity provider the node's directory seam is configured for (ADR-103:
``AXIOM_DIRECTORY_PROVIDER`` = ``local`` or ``entra``), which must be able to list members.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

#: The most members one cohort may list.
MAX_MEMBERS = 100_000
_HEADERS = frozenset({"principal", "principals", "account", "accounts", "member", "members", "id", "subject"})

Source = Callable[[Mapping[str, Any]], list[str]]


class SourceError(ValueError):
    """A source is malformed, unknown, or could not be read; the message says why."""


def _directory(spec: Mapping[str, Any]) -> list[str]:
    from axiom.extensions.builtins.directory import GroupRef
    from axiom.extensions.builtins.directory.config import load_directory_config

    cfg = load_directory_config()
    if cfg.provider is None:
        why = "; ".join(cfg.errors) or f"the directory provider is {cfg.provider_name}"
        raise SourceError(f"the directory cannot be read: {why}")
    if not cfg.can_enumerate:
        raise SourceError(f"the {cfg.provider_name} directory cannot list a group's members")
    ref = GroupRef(id=str(spec["group"]), provider=cfg.provider_name)
    if hasattr(cfg.provider, "resolve_group") and cfg.provider.resolve_group(ref) is None:
        raise SourceError(f"the directory has no group {spec['group']!r}")
    return [p.subject for p in cfg.provider.members_of(ref)]


_BUILTIN: dict[str, tuple[Source, tuple[str, ...]]] = {"directory": (_directory, ("group",))}
_sources: dict[str, tuple[Source, tuple[str, ...]]] = dict(_BUILTIN)


def register_source(kind: str, fn: Source, *, needs: tuple[str, ...] = ()) -> None:
    """Add a source kind: ``fn(spec) -> [principal, ...]``, raising on failure."""
    _sources[kind] = (fn, needs)


def reset_sources() -> None:
    _sources.clear()
    _sources.update(_BUILTIN)


def kinds() -> list[str]:
    return sorted(_sources)


def check_source(spec: Any) -> dict[str, Any] | None:
    """The source, validated; ``None`` for a listed cohort."""
    if spec in (None, "", {}):
        return None
    if not isinstance(spec, Mapping) or not spec.get("kind"):
        raise SourceError("a source is {kind, ...}, for example {kind: directory, group: <group id>}")
    kind = str(spec["kind"])
    if kind not in _sources:
        raise SourceError(f"no cohort source {kind!r}; the sources here: {', '.join(kinds())}")
    missing = [k for k in _sources[kind][1] if not spec.get(k)]
    if missing:
        raise SourceError(f"a {kind} source needs {', '.join(missing)}")
    return dict(spec)


def fetch(spec: Mapping[str, Any]) -> list[str]:
    """The members the source names now, or :class:`SourceError`."""
    fn, _ = _sources[str(spec["kind"])]
    try:
        members = [str(m).strip() for m in fn(spec) if str(m).strip()]
    except SourceError:
        raise
    except Exception as exc:  # noqa: BLE001 - any failure of a source is reported, never raised raw
        raise SourceError(f"the {spec['kind']} source failed: {type(exc).__name__}: {exc}") from exc
    if len(members) > MAX_MEMBERS:
        raise SourceError(f"the source lists {len(members)} members; a cohort holds at most {MAX_MEMBERS}")
    return members


def parse_members(text: str, *, name: str = "") -> list[str]:
    """Members from a file's text: a JSON list, CSV (first column) or one per line."""
    stripped = text.strip()
    if name.endswith(".json") or stripped.startswith(("[", "{")):
        try:
            doc = json.loads(stripped or "[]")
        except json.JSONDecodeError as exc:
            raise SourceError(f"{name or 'the list'} is not valid JSON: {exc.msg}") from exc
        if isinstance(doc, Mapping):
            doc = doc.get("members") or doc.get("principals") or []
        if not isinstance(doc, list):
            raise SourceError("a JSON member list is a list, or {members: [...]}")
        return [str(m).strip() for m in doc if str(m).strip()]
    out: list[str] = []
    for i, row in enumerate(csv.reader(io.StringIO(text))):
        if not row:
            continue
        cell = row[0].strip()
        if not cell or cell.startswith("#") or (i == 0 and cell.lower() in _HEADERS):
            continue
        out.append(cell)
    return out


def due(cohort: Mapping[str, Any], now: datetime) -> bool:
    """Whether a cohort that follows a source is due for its refresh."""
    every = cohort.get("refresh_every_s")
    if not cohort.get("source") or not every:
        return False
    last = cohort.get("refreshed_at")
    if not last:
        return True
    at = datetime.fromisoformat(last)
    at = at if at.tzinfo else at.replace(tzinfo=UTC)
    return (now - at).total_seconds() >= int(every)


__all__ = [
    "MAX_MEMBERS",
    "SourceError",
    "check_source",
    "due",
    "fetch",
    "kinds",
    "parse_members",
    "register_source",
    "reset_sources",
]
