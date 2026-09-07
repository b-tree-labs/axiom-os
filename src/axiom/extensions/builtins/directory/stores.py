# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tuple stores the sync can project into.

* :class:`JsonFileTupleStore` — a file-backed :class:`TupleStore` for nodes
  without an OpenFGA server (dev, tests, the interim on a single node). Same
  semantics as the real store: ``write`` refuses to add an existing tuple or
  delete a missing one, so a reconciler bug that would corrupt OpenFGA fails
  here first.
* :func:`resolve_tuple_store` — the environment seam: ``AXIOM_DIRECTORY_TUPLE_STORE``
  = ``json:<path>`` or ``openfga`` (the ``authz`` OpenFGA client, when this
  build carries it and ``AXIOM_OPENFGA_*`` is configured).
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from axiom.extensions.builtins.directory.reconcile import RelationTuple, TupleStore

TUPLE_STORE_ENV = "AXIOM_DIRECTORY_TUPLE_STORE"


class TupleStoreError(RuntimeError):
    """A write the store must refuse (duplicate add, missing delete)."""


class JsonFileTupleStore:
    """``{"tuples": [[user, relation, object], …]}`` on disk, atomic 0600 writes."""

    def __init__(self, path: str | os.PathLike) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    # -- persistence
    def _load(self) -> set[tuple[str, str, str]]:
        if not self._path.is_file():
            return set()
        raw = json.loads(self._path.read_text(encoding="utf-8") or "{}")
        return {tuple(t) for t in raw.get("tuples", []) if len(t) == 3}

    def _save(self, tuples: set[tuple[str, str, str]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_name(self._path.name + ".tmp")
        payload = json.dumps({"tuples": sorted(list(t) for t in tuples)}, indent=2) + "\n"
        tmp.write_text(payload, encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._path)

    # -- TupleStore
    def list_members(self, object: str, relation: str) -> list[str]:
        with self._lock:
            return sorted(u for u, r, o in self._load() if o == object and r == relation)

    def write(
        self, adds: Iterable[RelationTuple] = (), deletes: Iterable[RelationTuple] = ()
    ) -> None:
        adds, deletes = list(adds), list(deletes)
        if not adds and not deletes:
            return
        with self._lock:
            tuples = self._load()
            for t in deletes:
                key = (t.user, t.relation, t.object)
                if key not in tuples:
                    raise TupleStoreError(f"cannot delete a tuple that does not exist: {key}")
                tuples.discard(key)
            for t in adds:
                key = (t.user, t.relation, t.object)
                if key in tuples:
                    raise TupleStoreError(f"tuple already exists: {key}")
                tuples.add(key)
            self._save(tuples)

    # -- inspection
    def all_tuples(self) -> list[tuple[str, str, str]]:
        with self._lock:
            return sorted(self._load())

    def __len__(self) -> int:
        with self._lock:
            return len(self._load())


_: TupleStore = JsonFileTupleStore("unused")


def resolve_tuple_store(env: Mapping[str, str] | None = None) -> TupleStore | None:
    """The store named by ``AXIOM_DIRECTORY_TUPLE_STORE``, or ``None`` when unset.

    ``json:<path>`` → :class:`JsonFileTupleStore`. ``openfga`` → the ``authz``
    extension's OpenFGA client built from ``AXIOM_OPENFGA_*`` — raises a clear
    error when this build has no such client or the substrate is unconfigured,
    rather than syncing into nothing.
    """
    env = os.environ if env is None else env
    raw = (env.get(TUPLE_STORE_ENV) or "").strip()
    if not raw:
        return None
    if raw.startswith("json:"):
        path = raw[len("json:") :].strip()
        if not path:
            raise ValueError(f"{TUPLE_STORE_ENV}=json:<path> needs a path")
        return JsonFileTupleStore(os.path.expanduser(path))
    if raw == "openfga":
        try:
            from axiom.extensions.builtins.authz.openfga_http import (  # type: ignore[import-not-found]
                client_from_config,
                resolve_config,
            )
        except ImportError as exc:
            raise RuntimeError(
                "this build has no OpenFGA client (authz.openfga_http); "
                "upgrade axiom or use AXIOM_DIRECTORY_TUPLE_STORE=json:<path>"
            ) from exc
        cfg = resolve_config(env)
        if cfg is None:
            raise ValueError(f"{TUPLE_STORE_ENV}=openfga but AXIOM_OPENFGA_URL/STORE_ID are unset")
        return client_from_config(cfg)
    raise ValueError(f"unknown {TUPLE_STORE_ENV} {raw!r} (json:<path> | openfga)")


def describe_store(store: Any) -> str:
    if isinstance(store, JsonFileTupleStore):
        return f"json:{store._path}"
    return type(store).__name__


__all__ = [
    "TUPLE_STORE_ENV",
    "JsonFileTupleStore",
    "TupleStoreError",
    "describe_store",
    "resolve_tuple_store",
]
