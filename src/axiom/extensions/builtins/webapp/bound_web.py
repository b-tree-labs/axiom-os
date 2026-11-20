# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The web app on a bound install forwards shared reads to its site node (ADR-207).

The browser talks only to this install's ``/api/v1``. When the one serving mode
(``axiom.infra.serving_mode``) says *bound*, the shared reads below are answered by the bound node,
through its data door (``POST /data/query``), with the binding's key attached here from the vault.
This install never connects to the site's database, the key never reaches the browser, and nothing
here can write.

* :data:`WEB_READS` is the allowlist: each web read and the data door verbs it uses. A read not
  on it is not forwarded; a verb not in ``read_target.FOLLOWS_THE_BINDING`` or ``KIT_READS`` is
  refused before any HTTP call (:class:`BoundDoor`).
* The answers come from the node figure lane, the node-backed figure source
  (``scidisplay.node_figure_source.NodeFigureSource``), built here from the binding and given
  :class:`BoundDoor` as its door, so it reads through the same allowlist and the same key as every
  other bound read. Day calendars are kept in memory only (rule 9).
* A node that cannot be reached is a 503 that names the site and the node (rule 2). Nothing local
  is substituted. A cached answer says its age.
* The cache is in memory, bounded, and cleared whenever the binding changes (rule 9).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

log = logging.getLogger(__name__)

#: The web reads a bound install forwards, and the data door verbs each one uses. Adding one is a
#: change to this list and to its test; nothing is forwarded by pattern.
WEB_READS: dict[str, tuple[str, ...]] = {
    "sites": ("data.aggregate",),
    "channels": ("data.aggregate",),
    "revision": ("data.aggregate",),
    "dates": ("data.aggregate", "data.series"),
    "series": ("data.aggregate", "data.series"),
    "figure": ("data.aggregate", "data.series"),
}

#: Seconds a forwarded answer is reused. Short: the node's own revision moves underneath it.
CACHE_TTL_S = 30.0
#: Forwarded answers held at once. Bounded, so a laptop does not accumulate a site's data.
CACHE_MAX_ENTRIES = 256

#: What every response says in test mode (rule 3).
TEST_DATA_NOTE = "TEST DATA: invented sample data, not a real site"


class ForwardRefused(RuntimeError):
    """A forwarded read that was not, or could not be, answered. ``status`` is the HTTP code."""

    def __init__(self, message: str, *, status: int = 503) -> None:
        super().__init__(message)
        self.status = status


class Lane(Protocol):
    """What the web app asks the node figure lane. ``NodeFigureSource`` answers all of it."""

    def sites(self) -> list[dict[str, Any]]: ...
    def channels(self, site: str) -> list[dict[str, Any]]: ...
    def revision(self, site: str) -> str: ...
    def dates(self, site: str, tokens: Any, feed: str = "") -> list[dict[str, Any]]: ...
    def figure(self, site: str, tokens: Any, **kwargs: Any) -> dict[str, Any]: ...
    def resolve(self, site: str, tokens: Any, feed: str = "") -> list[Any]: ...
    def read(self, site: str, chans: list[Any], start: Any, end: Any, bucket: int) -> dict: ...


def _allowed_verbs() -> frozenset[str]:
    from axiom.extensions.builtins.data_platform.read_target import FOLLOWS_THE_BINDING, KIT_READS

    return frozenset(FOLLOWS_THE_BINDING) | frozenset(KIT_READS)


#: Reads this install has open against the bound node at once. Under the node's interactive
#: per-principal limit (8), so a busy page queues here instead of being refused there.
_IN_FLIGHT = threading.BoundedSemaphore(6)
#: How a node says "wait" rather than "no".
_WAIT_MARKERS = ("429", "rate limit", "too many concurrent calls")


class BoundDoor:
    """The figure lane's door: reads only, through ``read_target.ask``, the one bound transport.

    ``read_target.ask`` resolves the key from the binding's vault reference (or the selected
    audience's), offers it only to the bound node, and never opens a database here.
    """

    def __init__(self, site: str, face_url: str) -> None:
        self.site = site
        self.face_url = face_url

    def query(self, verb: str, params: dict[str, Any]) -> dict[str, Any]:
        if verb not in _allowed_verbs():
            # A write (``data.kit_submit``) or anything unlisted never leaves this machine here.
            raise ForwardRefused(f"`{verb}` is not a read this install forwards", status=403)
        from axiom.extensions.builtins.data_platform import read_target

        target = read_target.ReadTarget("binding", site=self.site, face_url=self.face_url)
        # A figure is several chunked reads at once, and the node's interactive limit answers a
        # burst with 429. That is "wait", not "refused": waited out a few times, as the lane's own
        # DataDoor does, before it is reported.
        for attempt in range(4):
            # Held under the node's per-principal concurrency limit: one page asks for a figure
            # (chunked) and several day calendars at once, all with the binding's one key.
            with _IN_FLIGHT:
                result = read_target.ask(target, verb=verb, params=dict(params))
            if result.ok or not any(m in e for e in result.errors for m in _WAIT_MARKERS):
                break
            time.sleep(0.5 * (attempt + 1))
        if not result.ok:
            message = "; ".join(result.errors) or f"{verb}: refused by {self.site}"
            # The node refusing a site the key does not hold is an answer about the
            # caller (403), not a node that failed to answer (503).
            raise ForwardRefused(message, status=403 if _refuses_the_site(message) else 503)
        value = result.value
        return value if isinstance(value, dict) else {}


def _refuses_the_site(message: str) -> bool:
    """The node's words for "this key reads another site", as ``node_figure_source`` matches them."""
    try:
        from axiom.extensions.builtins.scidisplay.node_figure_source import _REFUSAL_MARKERS
    except ImportError:  # pragma: no cover - the lane is absent and says so elsewhere
        return False
    return any(m in message for m in _REFUSAL_MARKERS)


def _default_lane_factory(door: BoundDoor) -> Lane:
    """The node figure lane, from ``scidisplay.node_figure_source`` (feat/figure-source-from-a-node)."""
    try:
        from axiom.extensions.builtins.scidisplay.node_figure_source import NodeFigureSource
    except ImportError:
        raise ForwardRefused(
            "this install cannot draw the bound site's figures yet: the node figure lane "
            "(scidisplay.node_figure_source) is not installed. Nothing local is shown instead.",
            status=503,
        ) from None
    # Memory only: a forwarded day calendar is the site's data and does not go to disk here.
    return NodeFigureSource(door=door, dates_cache_path="")


_lane_factory: Callable[[BoundDoor], Lane] = _default_lane_factory


def set_lane_factory(factory: Callable[[BoundDoor], Lane] | None) -> None:
    """Test seam (and the one place a product could supply its own lane)."""
    global _lane_factory
    _lane_factory = factory or _default_lane_factory
    reset()


@dataclass
class _Entry:
    at: float
    value: Any


class _State:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.fingerprint = ""
        self.lane: Lane | None = None
        self.cache: OrderedDict[tuple, _Entry] = OrderedDict()


_STATE = _State()


def reset() -> None:
    """Forget the lane and every cached answer (a rebind, an unbind, or a test)."""
    with _STATE.lock:
        _STATE.fingerprint = ""
        _STATE.lane = None
        _STATE.cache.clear()


def _mode():
    from axiom.infra.serving_mode import resolve_mode

    return resolve_mode()


def forwarding() -> bool:
    """True when this install's web reads are answered by the bound node.

    Every web read asks this first, so an unbind (or a rebind) is noticed on the next request and
    what was cached for the old binding is dropped then, not when a bound read next happens.
    """
    mode = _mode()
    _sync(mode)
    return mode.bound


def _sync(mode) -> None:
    from axiom.infra.serving_mode import binding_fingerprint

    fp = binding_fingerprint(mode)
    with _STATE.lock:
        if fp != _STATE.fingerprint:
            # Rule 9: a different binding (or none) means nothing read for the old one is kept.
            _STATE.cache.clear()
            _STATE.lane = None
            _STATE.fingerprint = fp


def _lane(mode) -> Lane:
    with _STATE.lock:
        if _STATE.lane is None:
            _STATE.lane = _lane_factory(BoundDoor(mode.site, mode.face_url))
        return _STATE.lane


def _check_skew(read: str, mode) -> None:
    from axiom.infra.serving_mode import PROBES, skew

    node = PROBES.get(mode)
    if not node.reachable:
        raise ForwardRefused(
            f"{mode.site} at {mode.node} is unreachable, so `{read}` was not answered. "
            f"{node.error} Nothing local is shown instead.", status=503)
    _notes, disabled = skew(node, needed=WEB_READS)
    if read in disabled:
        raise ForwardRefused(
            f"`{read}` is off: {mode.site} at {mode.node} does not serve it "
            f"(node version {node.version or 'unknown'}).", status=501)


def forward(read: str, call: Callable[[Lane], Any], *, key: tuple = ()) -> dict[str, Any]:
    """Answer ``read`` from the bound node. Raises :class:`ForwardRefused`, never answers locally.

    Returns ``{"value": ..., "served": {...}}``; ``served`` says the site and node, and the cached
    answer's age when it came from the cache.
    """
    if read not in WEB_READS:
        raise ForwardRefused(f"`{read}` is not a read this install forwards", status=403)
    mode = _mode()
    _sync(mode)
    if not mode.bound:
        raise ForwardRefused("this install is not bound to a site node", status=409)
    cache_key = (read, *key)
    now = time.monotonic()
    with _STATE.lock:
        hit = _STATE.cache.get(cache_key)
        if hit is not None and now - hit.at < CACHE_TTL_S:
            _STATE.cache.move_to_end(cache_key)
            return {"value": hit.value, "served": _served(mode, cached_age_s=round(now - hit.at, 1))}
    _check_skew(read, mode)
    lane = _lane(mode)
    try:
        value = call(lane)
    except ForwardRefused:
        raise
    except Exception as exc:  # noqa: BLE001 - a lane failure is the node's answer, said plainly
        if type(exc).__name__ == "FigureNote":
            raise ForwardRefused(str(exc), status=404) from None
        if type(exc).__name__ == "SiteRefused":
            # Refused, not empty: the key's own site is all it reads (ADR-207, gold scope).
            raise ForwardRefused(str(exc), status=403) from None
        raise ForwardRefused(
            f"{mode.site} at {mode.node} did not answer `{read}`: {exc}. Nothing local is shown "
            "instead.", status=503) from None
    with _STATE.lock:
        _STATE.cache[cache_key] = _Entry(time.monotonic(), value)
        while len(_STATE.cache) > CACHE_MAX_ENTRIES:
            _STATE.cache.popitem(last=False)
    return {"value": value, "served": _served(mode)}


def _served(mode, *, cached_age_s: float | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"mode": "bound", "site": mode.site, "node": mode.node,
                           "key_site": mode.key_site}
    if mode.view_as:
        out["view_as"] = mode.view_as
    if cached_age_s is not None:
        out["cached_age_s"] = cached_age_s
    return out


def mark_test_data(body: dict[str, Any]) -> dict[str, Any]:
    """Rule 3: in test mode every response says so, in a field and in its notes."""
    from axiom.infra.serving_mode import resolve_mode

    if resolve_mode().test_data:
        body = dict(body)
        body["test_data"] = True
        notes = list(body.get("notes") or [])
        if TEST_DATA_NOTE not in notes:
            notes.insert(0, TEST_DATA_NOTE)
        body["notes"] = notes
    return body


__all__ = [
    "BoundDoor",
    "CACHE_MAX_ENTRIES",
    "CACHE_TTL_S",
    "ForwardRefused",
    "Lane",
    "TEST_DATA_NOTE",
    "WEB_READS",
    "forward",
    "forwarding",
    "mark_test_data",
    "reset",
    "set_lane_factory",
]
