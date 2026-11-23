# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Which serving mode this install is in: the one answer every surface reads (ADR-207).

There are two real modes and one labelled test mode:

* **hosted**: this install holds the medallion and serves a site. It is the site's own node.
* **bound**: this install does not hold the medallion and has a current binding. Shared data,
  documents and the model come from the bound node, through its serving tier, with the binding's
  key. Personal state (saved views, drafts, settings, the kit sandbox) stays here.
* **local**: neither. This machine's own platform, or nothing configured.
* **test**: ``AXIOM_SAMPLE_DATA`` is set. Invented sample data, always marked. It wins over a
  binding, because a sample answer that looks real is the failure worth preventing.

Chat (``chat.where``, ``chat.bound_model``, ``chat.bound_tools``), document retrieval
(``rag.store_factory``), the CLI data reads and MCP (``data_platform.read_target``) and the web app
(``GET /api/v1/binding`` and its forwarded reads) all read :func:`resolve_mode`. None of them reads
the binding file or the sample-data variable for itself, so they cannot disagree.

:func:`resolve_mode` is configuration only: no network, never raises. :func:`probe` is the one
network call, asking the bound node what it runs; a node that does not answer leaves the mode
``bound`` and unreachable, never quietly local.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlparse

log = logging.getLogger(__name__)

Mode = Literal["hosted", "bound", "local", "test"]

#: Set to ``1`` on a demo, rehearsal or test machine. The same variable chat already reads.
SAMPLE_DATA_ENV = "AXIOM_SAMPLE_DATA"

#: The audience a bound install reads as (rule 6): a role the binding offers under ``view_as``.
VIEW_AS_ENV = "AXIOM_VIEW_AS"

#: Seconds :func:`probe` waits for the node. Short: the chip polls it, and a slow node is reported
#: as slow rather than holding the page.
PROBE_TIMEOUT_S = 3.0

#: The API this install speaks. A node that needs more says so in ``min_client``.
CLIENT_API = "v1"


def sample_data_mode() -> bool:
    """True when this install declares it runs on invented sample data."""
    return (os.environ.get(SAMPLE_DATA_ENV) or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ServingMode:
    """The resolved mode, and what a surface needs to say where its answers come from."""

    mode: Mode
    site: str = ""
    #: The bound node's host, for the chip. Empty unless bound.
    node: str = ""
    face_url: str = ""
    #: The site the key was minted for (the binding's ``key_site``, else its site).
    key_site: str = ""
    #: The audience being viewed as, when one was selected and the binding offers it.
    view_as: str = ""
    view_as_options: tuple[str, ...] = ()
    reason: str = ""
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def bound(self) -> bool:
        return self.mode == "bound"

    @property
    def test_data(self) -> bool:
        return self.mode == "test"


def _binding_record() -> dict[str, Any]:
    """The current binding's raw record, or empty. Never raises."""
    from axiom.extensions.builtins.data_platform import read_target

    try:
        data = json.loads(read_target.bindings_path().read_text(encoding="utf-8"))
        raw = data["bindings"][data["current"]]
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _view_as_entries(record: dict[str, Any]) -> dict[str, str]:
    """``{role: token_ref}`` the binding offers to view as."""
    out: dict[str, str] = {}
    for item in record.get("view_as") or []:
        if isinstance(item, dict):
            role = str(item.get("role") or "").strip()
            ref = str(item.get("token_ref") or "").strip()
            if role and ref:
                out[role] = ref
    return out


def view_as_token_ref() -> str:
    """The vault reference of the selected audience's key, or empty (use the binding's own)."""
    wanted = (os.environ.get(VIEW_AS_ENV) or "").strip()
    if not wanted:
        return ""
    return _view_as_entries(_binding_record()).get(wanted, "")


def _hosted_site() -> tuple[bool, str]:
    """``(serves a site, the site to show)``.

    A node that serves several sites shows none by name: the mode report is public, and naming
    one hosted site to another would be the disclosure ``site_scope`` refuses everywhere else.
    """
    try:
        from axiom.infra.site_scope import deployment_sites

        sites = deployment_sites()
    except Exception:  # noqa: BLE001 - a report must never raise
        sites = None
    if sites:
        return True, (next(iter(sites)) if len(sites) == 1 else "")
    home = (os.environ.get("AXIOM_SITE") or "").strip()
    return bool(home), home


def resolve_mode() -> ServingMode:
    """The one serving mode. Configuration only; never raises."""
    try:
        return _resolve()
    except Exception as exc:  # noqa: BLE001 - an unreadable configuration is local, and says so
        log.debug("serving mode not resolvable: %s", exc)
        return ServingMode("local", reason=f"the serving mode could not be read ({type(exc).__name__})")


def _resolve() -> ServingMode:
    # Through the module, not a from-import, so a test that replaces the primitive replaces it here.
    from axiom.extensions.builtins.data_platform import read_target

    if sample_data_mode():
        return ServingMode("test", reason=f"{SAMPLE_DATA_ENV} is set: invented sample data, not a real site")
    if read_target.holds_medallion():
        serves, site = _hosted_site()
        if serves:
            return ServingMode("hosted", site=site, key_site=site,
                               reason="this install holds the medallion and serves "
                                      + (site or "its sites"))
        return ServingMode("local", reason="this install holds the medallion")
    bound = read_target.current_binding()
    if bound is None:
        return ServingMode("local", reason="no binding; this machine's platform")
    site, face = bound
    record = _binding_record()
    offered = _view_as_entries(record)
    wanted = (os.environ.get(VIEW_AS_ENV) or "").strip()
    notes: tuple[str, ...] = ()
    view_as = ""
    if wanted and wanted in offered:
        view_as = wanted
    elif wanted:
        notes = (f"{VIEW_AS_ENV}={wanted} is not offered by this binding; reading with its own key",)
    return ServingMode(
        "bound",
        site=site,
        node=urlparse(face).hostname or face,
        face_url=face,
        key_site=str(record.get("key_site") or site),
        view_as=view_as,
        view_as_options=tuple(sorted(offered)),
        reason=f"current binding {site} at {face}",
        notes=notes,
    )


def binding_fingerprint(mode: ServingMode | None = None) -> str:
    """Changes whenever what a forwarded answer would be read as changes (rule 9: cleared on unbind)."""
    m = mode or resolve_mode()
    return "|".join((m.mode, m.site, m.face_url, m.key_site, m.view_as))


# ---- the words a surface shows ------------------------------------------------------------------


def sources(mode: ServingMode | None = None) -> dict[str, str]:
    """Where data, documents and the model come from, in words. Never raises."""
    m = mode or resolve_mode()
    if m.mode == "test":
        data = "invented sample data (test mode)"
    elif m.mode == "bound":
        data = f"{m.site} via {m.node}"
    elif m.mode == "hosted":
        data = f"{m.site} on this node"
    else:
        try:
            from axiom.extensions.builtins.data_platform import read_target

            data = "this machine" if read_target.holds_medallion() else "none configured"
        except Exception:  # noqa: BLE001
            data = "none configured"
    try:
        from axiom.rag.store_factory import resolve_store_url

        store = resolve_store_url()
    except Exception:  # noqa: BLE001
        store = None
    if store is not None and store.site:
        documents = f"{m.node or store.url} (site and community corpora)"
    elif store is not None and store.url:
        documents = "this machine" + (
            " (opted in; not the site's)" if m.mode == "bound" else "")
    else:
        documents = "none configured"
    model = f"the node's model at {m.node}" if m.mode == "bound" else "this machine's providers"
    return {"data": data, "documents": documents, "model": model}


# ---- reachability -------------------------------------------------------------------------------


@dataclass(frozen=True)
class NodeProbe:
    """What the bound node said about itself, or why it said nothing."""

    reachable: bool
    api: str = ""
    version: str = ""
    min_client: str = ""
    #: The data door verbs the node serves; empty when it did not say (an older node).
    data_reads: tuple[str, ...] = ()
    error: str = ""
    offline: bool = False
    checked_at: str = ""
    #: Something the report should say about how the node was reached, e.g. that its front door
    #: refused the version read and the data door answered instead.
    note: str = ""


def _http_get(url: str, headers: dict[str, str], timeout: float) -> tuple[int, dict[str, Any]]:
    """GET JSON. Module-level so a test can put a fake node behind it."""
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            status = response.status
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    try:
        body = json.loads(raw) if raw else {}
    except ValueError:
        body = {}
    return status, body if isinstance(body, dict) else {}


def _offline(exc: BaseException) -> bool:
    """A name that does not resolve is the usual sign of no network at all (rule 12)."""
    import socket

    reason = getattr(exc, "reason", exc)
    return isinstance(reason, socket.gaierror)


def probe(mode: ServingMode | None = None, *, timeout: float = PROBE_TIMEOUT_S) -> NodeProbe:
    """Ask the bound node what it runs. Never raises; never sends the key anywhere but that node."""
    m = mode or resolve_mode()
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    if not m.bound or not m.face_url:
        return NodeProbe(reachable=True, checked_at=now)
    from axiom.extensions.builtins.data_platform import read_target

    headers = {"accept": "application/json"}
    key = read_target.bound_key_for(m.face_url)
    if key:
        headers["authorization"] = f"Bearer {key}"
    try:
        status, body = _http_get(f"{m.face_url}/api/v1/version", headers, timeout)
    except Exception as exc:  # noqa: BLE001 - said plainly, never raised
        offline = _offline(exc)
        what = "this machine appears to be offline" if offline else "the node could not be reached"
        return NodeProbe(reachable=False, offline=offline, checked_at=now,
                         error=f"{m.site} at {m.node}: {what} ({type(exc).__name__}).")
    if status in (401, 403) and key and _data_door_answers(m):
        # A front door can gate /api/v1/version behind a browser session and strip the bearer
        # (a site's nginx does this for its /api/ pages). The data door is the contract a bound
        # read uses, and it answered the key, so the node is reachable; what it runs is unknown,
        # which disables nothing (rule 7). Said, so the front door gets fixed rather than hidden.
        return NodeProbe(reachable=True, checked_at=now, note=(
            f"{m.node} refused /api/v1/version to the binding's key (HTTP {status}) and its data "
            "door answered it; the node's version is unknown. Route /api/v1/version through the "
            "front door's bearer verify."))
    if status >= 400:
        return NodeProbe(reachable=False, checked_at=now,
                         error=f"{m.site} at {m.node} answered HTTP {status} to /api/v1/version.")
    reads = body.get("data_reads")
    return NodeProbe(
        reachable=True,
        api=str(body.get("api") or ""),
        version=str(body.get("platform") or body.get("version") or ""),
        min_client=str(body.get("min_client") or ""),
        data_reads=tuple(str(v) for v in reads) if isinstance(reads, list) else (),
        checked_at=now,
    )


def _data_door_answers(m: ServingMode) -> bool:
    """Whether the bound node's data door answers the binding's key. One cheap read; never raises."""
    try:
        from axiom.extensions.builtins.data_platform import read_target

        target = read_target.ReadTarget("binding", site=m.site, face_url=m.face_url)
        return bool(read_target.ask(target, verb="data.tables", params={}).ok)
    except Exception:  # noqa: BLE001 - a probe never raises
        return False


def _version_tuple(text: str) -> tuple[int, ...]:
    parts: list[int] = []
    for piece in text.strip().lstrip("v").split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def client_version() -> str:
    try:
        from axiom import __version__

        return str(__version__)
    except Exception:  # noqa: BLE001
        return ""


def skew(node: NodeProbe, *, needed: dict[str, tuple[str, ...]] | None = None) -> tuple[list[str], set[str]]:
    """``(notes, disabled)``: what the node does not support, said, and which reads to disable.

    ``needed`` maps a read to the data door verbs it uses. A read whose verbs the node did not list
    is disabled; everything else keeps working (rule 7). A node that lists nothing is an older node
    that did not say, so nothing is disabled on that account.
    """
    notes: list[str] = []
    disabled: set[str] = set()
    if not node.reachable:
        return notes, disabled
    if node.note:
        notes.append(node.note)
    if node.api and node.api != CLIENT_API:
        notes.append(f"the node speaks API {node.api}; this install speaks {CLIENT_API}")
    mine = client_version()
    if node.min_client and mine and _version_tuple(mine) < _version_tuple(node.min_client):
        notes.append(f"the node asks for a client at least {node.min_client}; this install is {mine}")
    if node.data_reads and needed:
        served = set(node.data_reads)
        for read, verbs in needed.items():
            missing = [v for v in verbs if v not in served]
            if missing:
                disabled.add(read)
                notes.append(f"{read} is off: the node (version {node.version or 'unknown'}) "
                             f"does not serve {', '.join(missing)}")
    return notes, disabled


class _ProbeCache:
    """One probe per binding per few seconds, so a polling chip does not become a load test."""

    def __init__(self, ttl: float = 10.0) -> None:
        self.ttl = ttl
        self._at = 0.0
        self._key = ""
        self._value: NodeProbe | None = None

    def get(self, mode: ServingMode) -> NodeProbe:
        key = binding_fingerprint(mode)
        now = time.monotonic()
        if self._value is not None and key == self._key and now - self._at < self.ttl:
            return self._value
        self._value, self._key, self._at = probe(mode), key, now
        return self._value

    def clear(self) -> None:
        self._value, self._key, self._at = None, "", 0.0


PROBES = _ProbeCache()


__all__ = [
    "CLIENT_API",
    "Mode",
    "NodeProbe",
    "PROBES",
    "SAMPLE_DATA_ENV",
    "ServingMode",
    "VIEW_AS_ENV",
    "binding_fingerprint",
    "client_version",
    "probe",
    "resolve_mode",
    "sample_data_mode",
    "skew",
    "sources",
    "view_as_token_ref",
]
