# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Where a data read runs.

``axi data`` (and ``neut data``, the same command) is the query surface. It
is not a local command. A read follows the current binding when this install
does not hold the medallion, and it opens the medallion on this install when
it does.

The kit sandbox is not a fallback. ``kit-up``, ``kit-try`` and ``kit-down``
name that database themselves. A snapshot is a point-in-time subset pulled
from a bound node and materialized into a sandbox later; a data read does not
silently become one.

The binding file is the one a site bind already writes, ``site-bindings.json``
in the platform state directory. This module reads it and does not rewrite it,
so a laptop that bound before this rule existed keeps that binding.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from axiom.infra.skills import SkillResult

from ._dsn import DSN_ENV_NAMES

STATE_FILENAME = "site-bindings.json"


class ReadError(RuntimeError):
    """A data read that followed a binding and could not."""


@dataclass(frozen=True)
class ReadTarget:
    """``medallion`` opens this install's database. ``binding`` asks the bound node."""

    where: str
    site: str = ""
    face_url: str = ""
    reason: str = ""


def bindings_path() -> Path:
    from axiom.infra.paths import get_platform_state_dir

    return get_platform_state_dir() / STATE_FILENAME


def holds_medallion() -> bool:
    """This install is the node that serves the medallion.

    Declaring ``data_platform`` says so. A configured database URL says so too:
    that is how a node already in service points at its medallion, including
    one that has not written a function list yet. The kit sandbox URL is not
    one of those names, so bringing a sandbox up does not make this install
    the medallion.
    """
    import os

    from axiom.infra.node_functions import load

    if "data_platform" in load().active_functions:
        return True
    return any(os.environ.get(name) for name in DSN_ENV_NAMES)


def current_binding() -> tuple[str, str] | None:
    """``(site, face_url)`` of the current binding, or None.

    A missing or unreadable file is no binding. It is not an error: an install
    that never bound keeps today's medallion default.
    """
    path = bindings_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    bindings = data.get("bindings")
    if not isinstance(bindings, dict):
        return None
    raw = bindings.get(data.get("current") or "")
    if not isinstance(raw, dict):
        return None
    site = str(raw.get("site") or "").strip()
    face = str(raw.get("face_url") or "").strip().rstrip("/")
    if not site or not face:
        return None
    return site, face


def resolve_read_target() -> ReadTarget:
    """The medallion on this install, unless a binding is the only place data lives."""
    if holds_medallion():
        return ReadTarget("medallion", reason="this install holds the medallion")
    bound = current_binding()
    if bound is not None:
        site, face = bound
        return ReadTarget(
            "binding",
            site=site,
            face_url=face,
            reason=f"current binding {site} at {face}",
        )
    return ReadTarget("medallion", reason="no binding; the medallion on this install")


#: The reads a bound install sends to its node rather than answering itself. The node's
#: ``/data/query`` serves the same names (``read_api``), each held to the caller's site there.
FOLLOWS_THE_BINDING: tuple[str, ...] = (
    "data.tables",
    "data.describe",
    "data.aggregate",
    "data.series",
    "data.roles",
    "data.compare",
    "data.uncertainty_coverage",
)


#: The promoted-kit reads a bound install sends to its node (``kit-verbs``, ``kit-charts``,
#: ``kit-automations``, ``kit-verb``). The node's ``/data/query`` serves the same names, each held
#: there to the site on the caller's verified credential.
KIT_READS: tuple[str, ...] = (
    "data.kit_verbs",
    "data.kit_verb",
    "data.kit_charts",
    "data.kit_automations",
)


def refuse_when_bound(verb: str, params: dict[str, Any] | None = None) -> SkillResult | None:
    """A plain refusal when ``verb`` would read this machine's database on behalf of a binding.

    A verb that opens a database itself, rather than going through :func:`ask`, would answer a
    bound install's question from whatever database this machine happens to have, the kit
    sandbox's server or a developer's own, and say nothing about it. On an install that does not
    hold the medallion and is bound to a site, that verb refuses instead and names where the data
    is. An explicit ``dsn`` is the caller naming a database, so it still runs.

    Returns ``None`` when the verb may run here.
    """
    if (params or {}).get("dsn"):
        return None
    target = resolve_read_target()
    if target.where != "binding":
        return None
    # ``tables`` reaches a person as the gold ``catalog``; name the verb they would type.
    follow = ", ".join(
        "catalog" if v == "data.tables" else v.split(".", 1)[1] for v in FOLLOWS_THE_BINDING
    )
    return SkillResult(
        ok=False,
        errors=[
            f"`{verb}` was not run: this install does not hold the medallion, it is bound to "
            f"{target.site} at {target.face_url}, and `{verb}` reads a database directly, so it "
            f"would answer for this machine rather than for {target.site}. Run it on that node, "
            f"or ask through a read that follows the binding: {follow}."
        ],
    )


def ask(target: ReadTarget, *, verb: str, params: dict[str, Any] | None = None) -> SkillResult:
    """Run ``verb`` on the bound node. Never opens a database on this machine."""
    if target.where != "binding" or not target.face_url:
        return SkillResult(ok=False, errors=["no binding to follow"])
    try:
        payload = _post(target, verb=verb, params=params or {})
    except ReadError as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    if not isinstance(payload, dict):
        return SkillResult(ok=False, errors=[_unreachable(target, "the node returned no result")])
    ok = bool(payload.get("ok", True))
    value = payload.get("value", payload)
    errors = [str(item) for item in payload.get("errors") or []]
    return SkillResult(ok=ok and not errors, value=value, errors=errors)


def _unreachable(target: ReadTarget, detail: str) -> str:
    return (
        f"this install does not hold the medallion. "
        f"The data read follows {target.site} at {target.face_url}. {detail}"
    )


def _binding_token_ref() -> str:
    """The vault reference the current binding was made with (``site bind --token-ref``), or empty."""
    try:
        data = json.loads(bindings_path().read_text(encoding="utf-8"))
        raw = data["bindings"][data["current"]]
        return str(raw.get("token_ref") or "").strip()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return ""


def _key(face_url: str) -> str:
    """The read key: the one the binding names, else the keys page's ``<first label>-key``, else empty.

    The binding's own reference comes first. Binding proves that key against the node, so a
    read that looked anywhere else could send a different key, or none, to the node it was
    just proven against.
    """
    ref = _binding_token_ref()
    if ref:
        try:
            from axiom.extensions.builtins.secrets import SecretRef, resolve

            secret = resolve(SecretRef.parse(ref))
            value = getattr(secret, "value", secret)
            if isinstance(value, bytes):
                value = value.decode("utf-8")
            if str(value).strip():
                return str(value).strip()
        except Exception:  # noqa: BLE001 — fall back to the keys page's entry below
            pass
    host = urlparse(face_url).hostname or ""
    label = host.split(".")[0] if host else ""
    if not label:
        return ""
    try:
        from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
        from axiom.infra.paths import get_user_state_dir

        with ForeignCredentialStore(get_user_state_dir()).get(f"{label}-key") as secret:
            return secret.as_str().strip()
    except Exception:  # noqa: BLE001 — no vault or no key is a failed read, not a crash
        return ""


def _origin(url: str) -> tuple[str, str, int]:
    parts = urlparse(url)
    scheme = (parts.scheme or "").lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    return scheme, (parts.hostname or "").lower(), port or (443 if scheme == "https" else 80)


def bound_key_for(url: str) -> str:
    """The current binding's key when ``url`` is on the bound node, else empty.

    Anything else that talks to the bound node (document retrieval, for one) sends the key a data
    read sends, resolved by the same :func:`_key`. Only to that node: a key is never offered to a
    host the binding does not name, so a store URL pointed somewhere else gets nothing.
    """
    bound = current_binding()
    if bound is None or not url or _origin(url) != _origin(bound[1]):
        return ""
    return _key(bound[1])


#: Verbs that are writes at the door (``/data/submit``, an invoke), never reads.
SUBMITS = frozenset({"data.kit_submit", "data.kit_withdraw"})


#: Seconds a data read waits for the bound node. ``AXIOM_DATA_READ_TIMEOUT`` changes it.
DEFAULT_READ_TIMEOUT = 30.0
READ_TIMEOUT_ENV = "AXIOM_DATA_READ_TIMEOUT"
READ_TIMEOUT_S = DEFAULT_READ_TIMEOUT  # the former name


def read_timeout() -> float:
    """How long a data read waits for the bound node: ``AXIOM_DATA_READ_TIMEOUT`` seconds, else 30."""
    import os

    raw = (os.environ.get(READ_TIMEOUT_ENV) or "").strip()
    try:
        value = float(raw) if raw else DEFAULT_READ_TIMEOUT
    except ValueError:
        return DEFAULT_READ_TIMEOUT
    return value if value > 0 else DEFAULT_READ_TIMEOUT


def _has_window(params: dict[str, Any]) -> bool:
    """A window was given: as the friendly flags, or as the ``window`` dict ``series`` and
    ``aggregate`` take (``--start``/``--end`` reach those verbs folded into it)."""
    if any(params.get(k) for k in ("start", "end", "day", "last", "yesterday")):
        return True
    window = params.get("window")
    return isinstance(window, dict) and any(window.get(k) for k in ("start", "end"))


def _names_a_feed(params: dict[str, Any]) -> bool:
    if any(params.get(k) for k in ("feed", "channel", "channels")):
        return True
    return "feed" in str(params.get("filter") or "").lower()


def _narrower(verb: str, params: dict[str, Any]) -> str:
    """The narrower question to try, in the command's own flags.

    A read that already had a window is never told to add one: it is told to narrow it, to name
    a feed (a filter on the feed is what lets the node use the signal index), or to ask for a
    coarser bucket.
    """
    asks = []
    bucketed = verb in ("data.series", "data.show")
    if not _has_window(params):
        asks.append("add a window (--last 1h, or --start/--end)")
    elif verb not in ("data.tables", "data.describe", "data.roles"):
        asks.append("narrow the window (a later --start or an earlier --end)")
    if not _names_a_feed(params):
        if _has_window(params) and ("table" in params or "filter" in params):
            asks.append("add a feed filter (--filter \"feed = '<feed>'\")")
        else:
            asks.append("name one feed or channel (--feed, --channel)")
    if bucketed and _has_window(params):
        asks.append("ask for a coarser bucket (--bucket '1 hour')")
    return " or ".join(asks) or "ask for less at once"


def _timed_out(target: ReadTarget, verb: str, params: dict[str, Any], timeout: float) -> ReadError:
    return ReadError(
        f"timed out: {target.site} at {target.face_url} did not answer `{verb}` within {timeout:g} s. "
        f"The node is reachable but the read is too wide for it. Try a narrower question: "
        f"{_narrower(verb, params)}, so it can use the signal index. "
        f"To wait longer, set {READ_TIMEOUT_ENV}=<seconds>."
    )


def _post(target: ReadTarget, *, verb: str, params: dict[str, Any]) -> dict[str, Any]:
    import urllib.error
    import urllib.request

    # A kit submission is the one write; it has its own path, which stays invoke at the gate.
    url = f"{target.face_url}/data/{'submit' if verb in SUBMITS else 'query'}"
    body = json.dumps({"verb": verb, "params": params}).encode("utf-8")
    headers = {"content-type": "application/json"}
    key = _key(target.face_url)
    if key:
        headers["authorization"] = f"Bearer {key}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    timeout = read_timeout()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except TimeoutError:
        # Raised by the read itself, after the connection opened: it was a raw traceback.
        raise _timed_out(target, verb, params, timeout) from None
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ReadError(
                _unreachable(target, "That node does not serve data reads yet.")
            ) from None
        detail = ""
        try:
            detail = str(json.loads(exc.read().decode("utf-8")).get("detail") or "")
        except Exception:  # noqa: BLE001 - a body that is not JSON just leaves the code
            pass
        raise ReadError(_unreachable(
            target, f"The node answered HTTP {exc.code}" + (f": {detail}" if detail else "."))) from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise _timed_out(target, verb, params, timeout) from None
        raise ReadError(_unreachable(target, f"The node could not be reached ({exc.reason}).")) from None
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise ReadError(_unreachable(target, "The node did not return JSON.")) from None
    if not isinstance(parsed, dict):
        raise ReadError(_unreachable(target, "The node did not return a result."))
    return parsed


__all__ = [
    "DEFAULT_READ_TIMEOUT",
    "FOLLOWS_THE_BINDING",
    "KIT_READS",
    "READ_TIMEOUT_ENV",
    "ReadError",
    "ReadTarget",
    "STATE_FILENAME",
    "ask",
    "bindings_path",
    "bound_key_for",
    "current_binding",
    "holds_medallion",
    "read_timeout",
    "refuse_when_bound",
    "resolve_read_target",
]
