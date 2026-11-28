# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The numbers a reader of the Timeline may see (#2224, ADR-271; the key, ADR-273).

The journal numbers every row from one identity shared by every lane and every site, and an audit
row carries the audit chain's number. A reader who may read only some logs would see either number
jump past rows they cannot read, which shows how many there are and when they were written.

- A **whole-site reader** (:func:`whole_site`): at every site in their scope, every declared log
  is readable by the one LD-1 check (``journal.attestations``). Their responses are unchanged.
- Everyone else gets rows :func:`shown` for them: ``seq`` replaced by ``key``, an opaque token;
  an audit row's chain number removed; an attestation row's per-log number as ``number``. Positions
  (cursors, stream ids, ``after``, ``resumed_at``, ``last``) are tokens too.

A token is a keyed pseudo-random permutation (a four-round Feistel network over HMAC-SHA256) of the
number and a tag, under a node key kept in the vault (:data:`VAULT_NAME`, created there on first
use, never written elsewhere): the same number always gives the same token, so a page can
deduplicate rows by it, and a token that does not decode is refused (:func:`position`). The journal
keeps its ``seq`` inside, for ordering, paging and resume; the chain keeps the record ``seq``.

Without the vault the journal fails closed: the partial form still holds, with no row keys and no
positions; a token is refused (:class:`CursorUnavailable`) and so is a partial reader's raw number.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
import threading
from collections.abc import Callable
from typing import Any

from . import rights

log = logging.getLogger("axiom.journal.numbers")

#: The node key's name in the vault (KEEP's store, the one ``attest.presence`` keeps its location
#: secrets in). Created on first use; never written anywhere else, logged or returned.
VAULT_NAME = "journal.cursor-key"
#: Every token starts with this, so a raw number is never mistaken for one.
PREFIX = "k"
_ROUNDS = 4

_key_lock = threading.Lock()
_key: bytes | None = None


class BadCursor(ValueError):
    """A cursor or stream id that is not a token this node issued, or a raw number from a partial
    reader."""


class CursorUnavailable(BadCursor):
    """The vault could not give the node key, so no token can be made or read (fail closed)."""


def _vault_store() -> Any:
    from axiom.extensions.builtins.secrets.foreign.store import ForeignCredentialStore
    from axiom.infra.paths import get_user_state_dir

    return ForeignCredentialStore(get_user_state_dir())


#: Opens the vault store the key lives in; a seam for tests, as ``attest.presence`` has.
_store_factory: Callable[[], Any] = _vault_store


def set_store_factory(factory: Callable[[], Any] | None) -> None:
    """Where the key is kept (tests inject a store); None restores the vault."""
    global _store_factory
    _store_factory = factory or _vault_store
    forget_key()


def _key_from_vault() -> bytes:
    store = _store_factory()
    if not store.exists(VAULT_NAME):
        store.set(VAULT_NAME, secrets.token_hex(32).encode("ascii"), provider="journal")
    with store.get(VAULT_NAME) as secret:
        return bytes.fromhex(secret.as_str())


def _node_key(*, fresh: bool = False) -> bytes:
    """The node key from the vault, created there on first use. :class:`CursorUnavailable` when
    the vault cannot answer: nothing falls back to a file or the environment."""
    global _key
    with _key_lock:
        if _key is not None and not fresh:
            return _key
        try:
            _key = _key_from_vault()
        except Exception as exc:  # noqa: BLE001 - any vault failure is the same refusal
            _key = None
            log.warning("journal: the cursor key could not be read from the vault (%s)", type(exc).__name__)
            raise CursorUnavailable("cursors are unavailable: the vault did not answer") from None
        return _key


def forget_key() -> None:
    """Read the node key again on next use (tests, a rotated key)."""
    global _key
    with _key_lock:
        _key = None


def _f(key: bytes, i: int, half: bytes) -> bytes:
    return hmac.new(key, bytes([i]) + half, hashlib.sha256).digest()[:8]


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b, strict=True))


def _encode(key: bytes, n: int) -> str:
    raw = int(n).to_bytes(8, "big", signed=True)
    left, right = raw, hmac.new(key, b"tag" + raw, hashlib.sha256).digest()[:8]
    for i in range(_ROUNDS):
        left, right = right, _xor(left, _f(key, i, right))
    return PREFIX + base64.urlsafe_b64encode(left + right).decode().rstrip("=")


def _decode(key: bytes, block: bytes) -> int | None:
    left, right = block[:8], block[8:]
    for i in reversed(range(_ROUNDS)):
        left, right = _xor(right, _f(key, i, left)), left
    if not hmac.compare_digest(right, hmac.new(key, b"tag" + left, hashlib.sha256).digest()[:8]):
        return None
    return int.from_bytes(left, "big", signed=True)


def token(n: int) -> str:
    """The opaque token for a journal position; :class:`CursorUnavailable` without the vault."""
    return _encode(_node_key(), n)


def try_token(n: Any) -> str | None:
    """A token, or None when the vault cannot give the key (the position is then not offered)."""
    try:
        return token(int(n))
    except CursorUnavailable:
        return None


def position(value: str) -> int:
    """The journal position a token stands for; :class:`BadCursor` when it is not one."""
    if not isinstance(value, str) or not value.startswith(PREFIX):
        raise BadCursor("not a cursor this node issued")
    body = value[len(PREFIX):]
    try:
        block = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    except (ValueError, TypeError) as exc:
        raise BadCursor("not a cursor this node issued") from exc
    if len(block) != 16:
        raise BadCursor("not a cursor this node issued")
    n = _decode(_node_key(), block)
    if n is None:
        # Another process may have created the key first: read it from the vault once more.
        n = _decode(_node_key(fresh=True), block)
    if n is None:
        raise BadCursor("not a cursor this node issued")
    return n


# -- who sees the journal's number ------------------------------------------------------------


def whole_site(caller: rights.Caller) -> bool:
    """Whether ``caller`` may read every declared log at every site in their scope (the one LD-1
    check, through the Timeline's door). An unbounded caller on a node bound to no site is one.
    Raises ``PolicyUnavailable`` when GUARD cannot decide, as every Timeline door does."""
    from axiom.extensions.builtins.attest import registry

    sites = caller.sites
    if sites is None:
        from axiom.infra.site_scope import deployment_sites

        served = deployment_sites()
        if served is None:
            return True
        sites = frozenset(served)
    if not sites:
        return False
    books = registry.all_logbooks()
    return all(rights.door_reason(caller, site, b.id) is None for site in sorted(sites) for b in books)


def form(caller: rights.Caller) -> bool | None:
    """:func:`whole_site`, or None when GUARD cannot decide it. Undecided is answered in the partial
    form (nothing is shown that a partial reader may not see), and a position the client sent back
    is still read, so a stream that loses GUARD ends as it always has rather than at subscribe."""
    from axiom.governance.doors import PolicyUnavailable

    try:
        return whole_site(caller)
    except PolicyUnavailable:
        return None


def read_position(value: Any, *, whole: bool | None) -> int | None:
    """A cursor or ``Last-Event-ID`` as the journal position it names. A whole-site reader may send
    a raw number, as before (and so may a caller whose form GUARD could not decide: a number sent
    back tells its sender nothing); anyone may send a token. :class:`BadCursor` otherwise."""
    if value in (None, ""):
        return None
    text = str(value).strip()
    if text.startswith(PREFIX):
        return position(text)
    if whole is not False:
        try:
            return int(text)
        except ValueError as exc:
            raise BadCursor("not a cursor this node issued") from exc
    raise BadCursor("not a cursor this node issued")


def out_position(n: Any, *, whole: bool | None) -> Any:
    """A position as a response carries it: unchanged for a whole-site reader, else a token."""
    if whole is True or n is None:
        return n
    return try_token(n)


def _log_label(logbook: str) -> str:
    from axiom.extensions.builtins.attest import registry
    from axiom.extensions.builtins.attest.logbooks import LogbookError

    try:
        return registry.get(logbook).display or logbook
    except LogbookError:
        return logbook


def _number(row: dict[str, Any]) -> dict[str, Any] | None:
    """An attestation row's per-log number (the record's ``seq``); None for every other row."""
    ref = row.get("ref") or {}
    if row.get("lane") != "attestations" or ref.get("seq") is None:
        return None
    logbook = str(ref.get("logbook") or row.get("logbook") or "")
    n = int(ref["seq"])
    label = _log_label(logbook)
    return {"n": n, "logbook": logbook, "label": label, "text": f"#{n} in {label}"}


def shown(row: dict[str, Any], *, whole: bool | None) -> dict[str, Any]:
    """A row as this reader may see it: unchanged for a whole-site reader."""
    if whole is True:
        return row
    out: dict[str, Any] = {}
    for k, v in row.items():
        # Without the vault a row has no key: the partial form still holds (fail closed).
        out["key" if k == "seq" else k] = try_token(v) if k == "seq" else v
    ref = out.get("ref")
    if isinstance(ref, dict) and ref.get("chain") is not None and "seq" in ref:
        # An audit chain's number is node-wide too: the row keeps its hash, never the number.
        out["ref"] = {k: v for k, v in ref.items() if k != "seq"}
        out["source_id"] = None
        out["link"] = None
    out["number"] = _number(row)
    return out


def shown_page(page: dict[str, Any], *, whole: bool | None) -> dict[str, Any]:
    """A page (``query`` or ``after``) as this reader may see it."""
    if whole is True:
        return page
    out = dict(page)
    out["rows"] = [shown(r, whole=False) for r in page.get("rows") or ()]
    for k in ("next_cursor", "last"):
        if k in out:
            out[k] = out_position(out[k], whole=False)
    return out


__all__ = [
    "BadCursor",
    "CursorUnavailable",
    "PREFIX",
    "VAULT_NAME",
    "forget_key",
    "form",
    "out_position",
    "position",
    "read_position",
    "shown",
    "set_store_factory",
    "shown_page",
    "token",
    "try_token",
    "whole_site",
]
