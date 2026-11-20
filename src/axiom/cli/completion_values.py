# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Real values for Tab, read from a cache on this computer and never from the network.

Completing nouns, verbs and flags tells somebody what they *can* type. Completing
the site's own roles, channels and days tells them what is *there*, which is the
question they actually have. Those values live on the bound node, and a TAB press
that waited on a node would make the shell feel broken whenever the VPN is down.

So a press reads one JSON file and nothing else. When that file is missing or
older than its time-to-live, the press starts a refresh in the background and
answers with what it has (possibly nothing): static completions still work, and
the next press has the values. A refresh is a separate, detached process with a
hard deadline and a lock, so a burst of presses starts one refresh, not twenty,
and a node that never answers costs a process that exits on its own.

The values come from **providers**: functions that take a :class:`ProviderContext`
and return ``{kind: [values]}`` plus optional ``{site: {kind: [values]}}``. The
platform ships the data platform's (sites, roles, channels, feeds, days with
data, promoted kit verbs). A product adds its own (a twin's shadows, say) under
the ``axiom.completion_values`` entry-point group. A provider that raises costs
its own kinds only.

Which parameter completes with which kind is not decided here. It is read from
the skill registry when the completion tree is built (:mod:`axiom.cli.command_space`),
so a flag completes live values only because the skill behind it says that is
what the parameter is.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: The cache file, under ``<user state dir>/cache/``.
CACHE_NAME = "completion-values.json"
#: Seconds a cache is fresh. A refresh after this is started in the background.
DEFAULT_TTL_S = 15 * 60
#: Seconds a started refresh holds the lock, so presses in a burst start one.
LOCK_S = 120
#: Seconds a background refresh may run before it exits on its own.
REFRESH_DEADLINE_S = 60
#: At most this many values of one kind are kept (a shell lists them all).
MAX_VALUES = 400
#: Entry-point group a product registers its providers under.
ENTRY_POINT_GROUP = "axiom.completion_values"
#: Environment variable that names the cache file (tests, and the background child).
CACHE_ENV = "AXI_COMPLETION_CACHE"
TTL_ENV = "AXI_COMPLETION_TTL"

#: Every value kind a parameter can complete with. A provider may return others;
#: these are the ones the platform knows how to describe.
KINDS: dict[str, str] = {
    "site": "a site this install can read",
    "role": "a quantity the site answers (see `data roles`)",
    "channel": "a channel at the site (see `data channels`)",
    "feed": "a feed at the site",
    "day": "a UTC day the site has readings for",
    "kit_verb": "a promoted kit verb you may run",
    "shadow": "one of your shadows",
    "log": "a log on this node, by its handle (see `log list`)",
}

#: Kinds whose values belong to one site, so a ``--site`` already typed narrows them.
PER_SITE: frozenset[str] = frozenset({"channel", "feed", "day", "role", "kit_verb"})


# --------------------------------------------------------------------------- where


def cache_path() -> Path:
    """``<user state dir>/cache/completion-values.json`` (never the working directory)."""
    override = os.environ.get(CACHE_ENV)
    if override:
        return Path(override)
    from axiom.infra.paths import get_user_state_dir

    return get_user_state_dir() / "cache" / CACHE_NAME


def ttl_s() -> float:
    try:
        return float(os.environ.get(TTL_ENV) or DEFAULT_TTL_S)
    except ValueError:
        return float(DEFAULT_TTL_S)


# --------------------------------------------------------------------------- the cache


@dataclass
class ValueCache:
    """What the last refresh learned. Empty is a valid cache: it means 'nothing known'."""

    fetched_at: float = 0.0
    binding: dict[str, str] = field(default_factory=dict)
    values: dict[str, list[str]] = field(default_factory=dict)
    per_site: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.values and not self.per_site

    def age_s(self, now: float | None = None) -> float:
        if not self.fetched_at:
            return float("inf")
        return max(0.0, (now or time.time()) - self.fetched_at)

    def stale(self, now: float | None = None) -> bool:
        return self.age_s(now) > ttl_s()

    def get(self, kind: str, *, site: str | None = None) -> list[str]:
        """Values of ``kind``: the site's own when a site is named and known, else every site's."""
        if site and kind in PER_SITE:
            mine = (self.per_site.get(site) or {}).get(kind)
            if mine is not None:
                return list(mine)
        return list(self.values.get(kind) or [])

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": 1,
            "fetched_at": self.fetched_at,
            "fetched": datetime.fromtimestamp(self.fetched_at, UTC).isoformat(timespec="seconds")
            if self.fetched_at else "",
            "binding": dict(self.binding),
            "values": {k: list(v) for k, v in self.values.items()},
            "per_site": {s: {k: list(v) for k, v in d.items()} for s, d in self.per_site.items()},
            "errors": list(self.errors),
        }

    @classmethod
    def from_dict(cls, data: Any) -> ValueCache:
        if not isinstance(data, dict) or data.get("v") != 1:
            return cls()

        def _strs(seq: Any) -> list[str]:
            return [str(x) for x in seq if isinstance(x, (str, int, float))] if isinstance(seq, list) else []

        values = {str(k): _strs(v) for k, v in (data.get("values") or {}).items()}
        per_site = {
            str(s): {str(k): _strs(v) for k, v in (d or {}).items()}
            for s, d in (data.get("per_site") or {}).items()
            if isinstance(d, dict)
        }
        binding = {str(k): str(v) for k, v in (data.get("binding") or {}).items()}
        try:
            fetched = float(data.get("fetched_at") or 0.0)
        except (TypeError, ValueError):
            fetched = 0.0
        return cls(fetched, binding, values, per_site, _strs(data.get("errors") or []))


def load(path: Path | None = None) -> ValueCache:
    """The cache on disk, or an empty one. One file read; never the network; never raises."""
    try:
        with open(path or cache_path(), encoding="utf-8") as fh:
            return ValueCache.from_dict(json.load(fh))
    except (OSError, ValueError):
        return ValueCache()


def save(cache: ValueCache, path: Path | None = None) -> Path:
    """Write the cache atomically, so a press never reads half a file."""
    path = path or cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".completion-values-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cache.to_dict(), fh, indent=1, sort_keys=True)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


# --------------------------------------------------------------------------- the binding


def local_binding() -> dict[str, str]:
    """``{"site", "face_url"}`` of the current site binding, from the local file, or ``{}``.

    A file read, so it is available on a cold cache: a bound laptop completes its
    own site on the very first press.
    """
    try:
        from axiom.extensions.builtins.data_platform.read_target import current_binding

        bound = current_binding()
    except Exception:  # noqa: BLE001 - no binding is the ordinary case
        return {}
    if not bound:
        return {}
    return {"site": bound[0], "face_url": bound[1]}


# --------------------------------------------------------------------------- reading (the TAB press)


def values_for(
    kinds: Iterable[str],
    *,
    site: str | None = None,
    cache: ValueCache | None = None,
    refresh: bool = True,
) -> list[str]:
    """Every known value of any of ``kinds``, the cache's order kept, duplicates dropped.

    Never blocks on the network. When the cache is cold or stale and ``refresh`` is
    set, a background refresh is started and this returns what is known now.
    """
    kinds = [k for k in kinds if k]
    cache = cache if cache is not None else load()
    if refresh and cache.stale():
        start_background_refresh()
    out: list[str] = []
    for kind in kinds:
        found = cache.get(kind, site=site)
        if kind == "site" and not found:
            bound = local_binding().get("site")
            found = [bound] if bound else []
        for value in found:
            if value and value not in out:
                out.append(value)
    return out


class LiveValues:
    """An argcomplete completer for parameters whose skill says what they hold."""

    def __init__(self, kinds: Iterable[str]) -> None:
        self.kinds = tuple(kinds)

    def __call__(self, prefix: str = "", parsed_args: Any = None, **_kw: Any) -> list[str]:
        site = getattr(parsed_args, "site", None) if parsed_args is not None else None
        try:
            found = values_for(self.kinds, site=site if isinstance(site, str) else None)
        except Exception:  # noqa: BLE001 - a completer that raises kills every completion
            return []
        return [v for v in found if v.startswith(prefix or "")]

    def __repr__(self) -> str:  # argcomplete prints completers in debug output
        return f"LiveValues({', '.join(self.kinds)})"


# --------------------------------------------------------------------------- refreshing


@dataclass
class ProviderContext:
    """What a provider may use. ``binding`` is the local binding (maybe empty)."""

    binding: dict[str, str]
    deadline: float
    say: Callable[[str], None] = lambda _m: None

    def time_left(self) -> float:
        return max(0.0, self.deadline - time.monotonic())


Provider = Callable[[ProviderContext], Mapping[str, Any]]


def _builtin_providers() -> list[tuple[str, Provider]]:
    from axiom.extensions.builtins.attest.completion_values import collect as logs
    from axiom.extensions.builtins.data_platform.completion_values import collect

    return [("data", collect), ("attest", logs)]


def _entry_point_providers() -> list[tuple[str, Provider]]:
    out: list[tuple[str, Provider]] = []
    try:
        from importlib.metadata import entry_points

        for ep in entry_points(group=ENTRY_POINT_GROUP):
            try:
                out.append((ep.name, ep.load()))
            except Exception:  # noqa: BLE001 - one broken product provider costs its own kinds
                continue
    except Exception:  # noqa: BLE001
        pass
    return out


def providers() -> list[tuple[str, Provider]]:
    seen: set[str] = set()
    out: list[tuple[str, Provider]] = []
    for name, fn in _builtin_providers() + _entry_point_providers():
        if name not in seen and callable(fn):
            seen.add(name)
            out.append((name, fn))
    return out


def _merge(into: dict[str, list[str]], kind: str, values: Any) -> None:
    if not isinstance(values, (list, tuple, set)):
        return
    have = into.setdefault(kind, [])
    for v in values:
        s = str(v).strip()
        if s and s not in have and len(have) < MAX_VALUES:
            have.append(s)


def refresh(
    *,
    path: Path | None = None,
    use: list[tuple[str, Provider]] | None = None,
    budget_s: float = REFRESH_DEADLINE_S - 5,
    say: Callable[[str], None] = lambda _m: None,
) -> ValueCache:
    """Ask every provider, then write the cache. Returns what was written.

    A provider that fails is recorded in ``errors`` and its kinds keep the values
    the previous cache had, so an offline node does not erase what Tab knew.
    """
    previous = load(path)
    binding = local_binding()
    ctx = ProviderContext(binding=binding, deadline=time.monotonic() + budget_s, say=say)
    fresh = ValueCache(fetched_at=time.time(), binding=binding)
    failed_kinds: set[str] = set()
    for name, fn in (use if use is not None else providers()):
        if ctx.time_left() <= 0:
            fresh.errors.append(f"{name}: no time left")
            continue
        try:
            got = fn(ctx) or {}
        except Exception as exc:  # noqa: BLE001 - one provider's failure is its own line
            fresh.errors.append(f"{name}: {type(exc).__name__}: {exc}"[:300])
            continue
        for kind, vals in (got.get("values") or {}).items():
            _merge(fresh.values, str(kind), vals)
        for site, kinds in (got.get("per_site") or {}).items():
            if not isinstance(kinds, Mapping):
                continue
            slot = fresh.per_site.setdefault(str(site), {})
            for kind, vals in kinds.items():
                _merge(slot, str(kind), vals)
        for err in got.get("errors") or []:
            fresh.errors.append(f"{name}: {err}"[:300])
        failed_kinds.update(str(k) for k in got.get("failed_kinds") or [])
    if binding.get("site"):
        _merge(fresh.values, "site", [binding["site"]])
    # Keep what was known for a kind no provider could answer this time.
    for kind, vals in previous.values.items():
        if kind not in fresh.values or kind in failed_kinds:
            fresh.values[kind] = list(vals)
    for site, kinds in previous.per_site.items():
        slot = fresh.per_site.setdefault(site, {})
        for kind, vals in kinds.items():
            if kind not in slot:
                slot[kind] = list(vals)
    save(fresh, path)
    return fresh


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".refreshing")


def _lock_held(path: Path, now: float | None = None) -> bool:
    try:
        started = _lock_path(path).stat().st_mtime
    except OSError:
        return False
    return (now or time.time()) - started < LOCK_S


def start_background_refresh(*, path: Path | None = None, spawn: Callable[..., Any] | None = None) -> bool:
    """Start one detached refresh unless one started in the last ``LOCK_S`` seconds.

    Returns True when a refresh was started. Never raises and never waits.
    """
    if os.environ.get("AXI_COMPLETION_NO_REFRESH"):
        return False
    try:
        path = path or cache_path()
        if _lock_held(path):
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        _lock_path(path).write_text(str(os.getpid()), encoding="utf-8")
        env = {k: v for k, v in os.environ.items()
               if not (k.startswith("_ARGCOMPLETE") or k.startswith("COMP_") or k == "_ARC_DEBUG")}
        env[CACHE_ENV] = str(path)
        argv = [sys.executable, "-m", "axiom.cli.completion_values", "refresh"]
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL, "env": env, "close_fds": True,
        }
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kwargs["start_new_session"] = True
        (spawn or subprocess.Popen)(argv, **kwargs)
        return True
    except Exception:  # noqa: BLE001 - a press must never fail because a refresh could not start
        return False


def _arm_deadline(seconds: float) -> None:
    """Exit the process after ``seconds`` whatever it is doing (a hung node, a hung import)."""
    timer = threading.Timer(seconds, lambda: os._exit(3))
    timer.daemon = True
    timer.start()


def main(argv: list[str] | None = None) -> int:
    """``python -m axiom.cli.completion_values refresh``: the background child."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] != ["refresh"]:
        print("usage: python -m axiom.cli.completion_values refresh", file=sys.stderr)
        return 2
    _arm_deadline(REFRESH_DEADLINE_S)
    path = cache_path()
    try:
        refresh(path=path)
    finally:
        try:
            _lock_path(path).unlink()
        except OSError:
            pass
    return 0


__all__ = [
    "CACHE_NAME",
    "DEFAULT_TTL_S",
    "ENTRY_POINT_GROUP",
    "KINDS",
    "LiveValues",
    "PER_SITE",
    "ProviderContext",
    "ValueCache",
    "cache_path",
    "load",
    "local_binding",
    "providers",
    "refresh",
    "save",
    "start_background_refresh",
    "values_for",
]


if __name__ == "__main__":
    raise SystemExit(main())
