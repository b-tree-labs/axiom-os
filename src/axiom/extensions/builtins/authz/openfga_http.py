# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The live OpenFGA client — ``check`` for GUARD, ``write``/``read`` for the seam.

ADR-083 wrote the substrate against an injected :class:`FgaCheckClient` and left
the "thin adapter over the real server" for when a server existed. ADR-103's
reconciler likewise wrote against a :class:`TupleStore` port with only test
implementations. This module is both adapters in one object, over OpenFGA's
plain HTTP API (no SDK — three endpoints, one bearer header):

* :meth:`OpenFgaHttpClient.check`        → ``POST /stores/{id}/check``
* :meth:`OpenFgaHttpClient.list_members` → ``POST /stores/{id}/read`` (paginated)
* :meth:`OpenFgaHttpClient.write`        → ``POST /stores/{id}/write`` (chunked ≤ 100)

plus the bootstrap a fresh server needs (``create_store``,
``write_authorization_model`` with the starter model as JSON, ``healthy``).

Configuration is environment- or settings-driven so a node turns the substrate
on without code::

    AXIOM_OPENFGA_URL=http://127.0.0.1:8080
    AXIOM_OPENFGA_STORE_ID=01J…            # from `create_store` (or the OpenFGA CLI)
    AXIOM_OPENFGA_MODEL_ID=01J…            # optional: pin a model; else the store's latest
    AXIOM_OPENFGA_TOKEN_FILE=~/.config/axiom/openfga.token   # optional preshared key, 0600
    AXIOM_OPENFGA_ON_ERROR=abstain|deny    # ADR-083 default abstain; strict nodes deny

or the same keys under ``authz.openfga.*`` in ``axi settings``. With nothing
configured :func:`maybe_register_substrate` is a no-op and GUARD keeps its
``NullSubstrate`` — behaviour-preserving until a server is pointed at.

Consistency defaults to ``HIGHER_CONSISTENCY`` on the decision path (ADR-083).
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from axiom.extensions.builtins.authz.openfga import OpenFgaSubstrate
from axiom.extensions.builtins.authz.substrate import SubstrateDecision

_LOG = logging.getLogger("axiom.authz.openfga")

#: OpenFGA refuses more than this many tuples in one write request.
WRITE_CHUNK = 100
READ_PAGE = 100
HIGHER_CONSISTENCY = "HIGHER_CONSISTENCY"
MINIMIZE_LATENCY = "MINIMIZE_LATENCY"

Transport = Callable[[str, str, Mapping[str, Any] | None], Mapping[str, Any]]
"""``(method, path, json_body) -> json``; raises :class:`OpenFgaError` on a non-2xx."""


class OpenFgaError(RuntimeError):
    """The server refused or could not be reached. ``status`` is the HTTP code
    (``0`` for transport failures)."""

    def __init__(self, message: str, *, status: int = 0, body: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.body = body


# ---------------------------------------------------------------- transport


class HttpxTransport:
    """The production transport: ``httpx`` (a base dependency), one client,
    optional bearer, short timeout — an OpenFGA outage must fail fast so the
    substrate's ``on_error`` policy applies rather than a request hanging."""

    def __init__(self, base_url: str, *, token: str | None = None, timeout: float = 5.0) -> None:
        import httpx

        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout)

    def __call__(self, method: str, path: str, body: Mapping[str, Any] | None) -> Mapping[str, Any]:
        import httpx

        try:
            resp = self._client.request(method, path, json=dict(body) if body is not None else None)
        except httpx.HTTPError as exc:
            raise OpenFgaError(f"openfga unreachable: {exc}") from exc
        if resp.status_code >= 300:
            raise OpenFgaError(
                f"openfga {method} {path} → {resp.status_code}",
                status=resp.status_code,
                body=resp.text[:500],
            )
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError as exc:
            raise OpenFgaError(f"openfga {method} {path}: non-JSON body") from exc


# ---------------------------------------------------------------- the client


def _key(user: str, relation: str, obj: str) -> dict[str, str]:
    return {"user": user, "relation": relation, "object": obj}


@dataclass
class OpenFgaHttpClient:
    """Satisfies both ``authz.openfga.FgaCheckClient`` and
    ``directory.reconcile.TupleStore``. Thread-safe as far as the transport is."""

    transport: Transport
    store_id: str
    model_id: str | None = None
    consistency: str = HIGHER_CONSISTENCY

    @classmethod
    def connect(
        cls,
        url: str,
        *,
        store_id: str,
        model_id: str | None = None,
        token: str | None = None,
        timeout: float = 5.0,
        consistency: str = HIGHER_CONSISTENCY,
    ) -> OpenFgaHttpClient:
        return cls(
            transport=HttpxTransport(url, token=token, timeout=timeout),
            store_id=store_id,
            model_id=model_id,
            consistency=consistency,
        )

    # -- FgaCheckClient --------------------------------------------------
    def check(
        self,
        *,
        user: str,
        relation: str,
        object: str,
        contextual_tuples: tuple[tuple[str, str, str], ...] = (),
    ) -> bool:
        body: dict[str, Any] = {
            "tuple_key": _key(user, relation, object),
            "consistency": self.consistency,
        }
        if contextual_tuples:
            body["contextual_tuples"] = {"tuple_keys": [_key(*t) for t in contextual_tuples]}
        if self.model_id:
            body["authorization_model_id"] = self.model_id
        out = self.transport("POST", f"/stores/{self.store_id}/check", body)
        return bool(out.get("allowed", False))

    # -- TupleStore --------------------------------------------------------
    def list_members(self, object: str, relation: str) -> list[str]:
        """Every ``user`` with ``relation`` on ``object`` — the reconciler's
        "current" set. Walks continuation tokens; never returns a partial page
        as if it were the whole truth."""
        users: list[str] = []
        token: str | None = None
        while True:
            body: dict[str, Any] = {
                "tuple_key": {"object": object, "relation": relation},
                "page_size": READ_PAGE,
                "consistency": self.consistency,
            }
            if token:
                body["continuation_token"] = token
            out = self.transport("POST", f"/stores/{self.store_id}/read", body)
            for t in out.get("tuples", []) or []:
                key = t.get("key") or {}
                if key.get("user"):
                    users.append(str(key["user"]))
            token = out.get("continuation_token") or None
            if not token:
                return users

    def write(self, adds: Iterable[Any] = (), deletes: Iterable[Any] = ()) -> None:
        """Apply adds and deletes in chunks of ≤ :data:`WRITE_CHUNK`.

        Deletes go first, chunk by chunk, then adds — so an interrupted batch
        can only ever under-grant (the reconciler's ``split_writes`` invariant,
        honoured here regardless). An empty call is a no-op, not a request.
        """
        del_keys = [_key(t.user, t.relation, t.object) for t in deletes]
        add_keys = [_key(t.user, t.relation, t.object) for t in adds]
        for i in range(0, len(del_keys), WRITE_CHUNK):
            self._write_chunk(deletes=del_keys[i : i + WRITE_CHUNK])
        for i in range(0, len(add_keys), WRITE_CHUNK):
            self._write_chunk(writes=add_keys[i : i + WRITE_CHUNK])

    def _write_chunk(
        self, *, writes: list[dict] | None = None, deletes: list[dict] | None = None
    ) -> None:
        if not writes and not deletes:
            return
        body: dict[str, Any] = {}
        if writes:
            body["writes"] = {"tuple_keys": writes}
        if deletes:
            body["deletes"] = {"tuple_keys": deletes}
        if self.model_id:
            body["authorization_model_id"] = self.model_id
        self.transport("POST", f"/stores/{self.store_id}/write", body)

    # -- bootstrap ---------------------------------------------------------
    def healthy(self) -> bool:
        try:
            out = self.transport("GET", "/healthz", None)
        except OpenFgaError:
            return False
        return str(out.get("status", "")).upper() == "SERVING"

    def write_authorization_model(
        self, type_definitions: list[dict], *, schema_version: str = "1.1"
    ) -> str:
        """Publish a model (JSON form) to this store; returns its id and pins
        it on the client."""
        out = self.transport(
            "POST",
            f"/stores/{self.store_id}/authorization-models",
            {"schema_version": schema_version, "type_definitions": type_definitions},
        )
        model_id = str(out.get("authorization_model_id") or "")
        if not model_id:
            raise OpenFgaError("openfga returned no authorization_model_id")
        self.model_id = model_id
        return model_id


def create_store(transport: Transport, name: str) -> str:
    """``POST /stores`` — returns the new store id."""
    out = transport("POST", "/stores", {"name": name})
    store_id = str(out.get("id") or "")
    if not store_id:
        raise OpenFgaError("openfga returned no store id")
    return store_id


def list_stores(transport: Transport) -> list[dict]:
    return list((transport("GET", "/stores", None) or {}).get("stores", []) or [])


# ---------------------------------------------------------------- starter model (JSON)

_USER_OR_GROUP_MEMBER = [{"type": "user"}, {"type": "group", "relation": "member"}]


def starter_type_definitions() -> list[dict]:
    """``fga/starter.fga`` in the JSON shape the HTTP API accepts (schema 1.1):
    ``user``; ``group.member``; ``resource.owner ⊂ editor ⊂ viewer`` plus the
    explicit ``blocked`` deny relation the substrate checks first."""
    return [
        {"type": "user", "relations": {}, "metadata": None},
        {
            "type": "group",
            "relations": {"member": {"this": {}}},
            "metadata": {
                "relations": {"member": {"directly_related_user_types": _USER_OR_GROUP_MEMBER}}
            },
        },
        {
            "type": "resource",
            "relations": {
                "owner": {"this": {}},
                "editor": {
                    "union": {"child": [{"this": {}}, {"computedUserset": {"relation": "owner"}}]}
                },
                "viewer": {
                    "union": {"child": [{"this": {}}, {"computedUserset": {"relation": "editor"}}]}
                },
                "blocked": {"this": {}},
            },
            "metadata": {
                "relations": {
                    rel: {"directly_related_user_types": _USER_OR_GROUP_MEMBER}
                    for rel in ("owner", "editor", "viewer", "blocked")
                }
            },
        },
    ]


# ---------------------------------------------------------------- configuration

URL_ENV = "AXIOM_OPENFGA_URL"
STORE_ENV = "AXIOM_OPENFGA_STORE_ID"
MODEL_ENV = "AXIOM_OPENFGA_MODEL_ID"
TOKEN_ENV = "AXIOM_OPENFGA_TOKEN"
TOKEN_FILE_ENV = "AXIOM_OPENFGA_TOKEN_FILE"
ON_ERROR_ENV = "AXIOM_OPENFGA_ON_ERROR"
SETTINGS_PREFIX = "authz.openfga"

_ON_ERROR = {"abstain": SubstrateDecision.ABSTAIN, "deny": SubstrateDecision.DENY}


@dataclass(frozen=True)
class OpenFgaConfig:
    url: str
    store_id: str
    model_id: str | None = None
    token: str | None = field(default=None, repr=False)
    on_error: SubstrateDecision = SubstrateDecision.ABSTAIN
    consistency: str = HIGHER_CONSISTENCY
    timeout: float = 5.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> OpenFgaConfig | None:
        """``None`` when unset. A URL without a store id is refused loudly —
        a half-configured substrate must not silently become no substrate."""
        env = os.environ if env is None else env
        url = (env.get(URL_ENV) or "").strip()
        store = (env.get(STORE_ENV) or "").strip()
        if not url and not store:
            return None
        if not url or not store:
            raise ValueError(f"{URL_ENV} and {STORE_ENV} must be set together")
        token = (env.get(TOKEN_ENV) or "").strip() or None
        token_file = (env.get(TOKEN_FILE_ENV) or "").strip()
        if token is None and token_file:
            token = Path(os.path.expanduser(token_file)).read_text(encoding="utf-8").strip() or None
        on_error_raw = (env.get(ON_ERROR_ENV) or "abstain").strip().lower()
        if on_error_raw not in _ON_ERROR:
            raise ValueError(f"{ON_ERROR_ENV} must be abstain or deny, not {on_error_raw!r}")
        return cls(
            url=url,
            store_id=store,
            model_id=(env.get(MODEL_ENV) or "").strip() or None,
            token=token,
            on_error=_ON_ERROR[on_error_raw],
        )

    @classmethod
    def from_settings(cls, settings: Any) -> OpenFgaConfig | None:
        """The same keys under ``authz.openfga.*`` in the settings store
        (``get(key, default)`` shape). ``None`` when unset."""
        url = str(settings.get(f"{SETTINGS_PREFIX}.url", "") or "").strip()
        store = str(settings.get(f"{SETTINGS_PREFIX}.store_id", "") or "").strip()
        if not url and not store:
            return None
        if not url or not store:
            raise ValueError(f"{SETTINGS_PREFIX}.url and .store_id must be set together")
        on_error_raw = str(
            settings.get(f"{SETTINGS_PREFIX}.on_error", "abstain") or "abstain"
        ).lower()
        if on_error_raw not in _ON_ERROR:
            raise ValueError(f"{SETTINGS_PREFIX}.on_error must be abstain or deny")
        return cls(
            url=url,
            store_id=store,
            model_id=str(settings.get(f"{SETTINGS_PREFIX}.model_id", "") or "").strip() or None,
            token=str(settings.get(f"{SETTINGS_PREFIX}.token", "") or "").strip() or None,
            on_error=_ON_ERROR[on_error_raw],
        )


def resolve_config(
    env: Mapping[str, str] | None = None, settings: Any = None
) -> OpenFgaConfig | None:
    """Environment first (a deploy pins it), then the settings store."""
    cfg = OpenFgaConfig.from_env(env)
    if cfg is not None:
        return cfg
    if settings is None:
        try:
            from axiom.extensions.builtins.settings.store import SettingsStore

            settings = SettingsStore()
        except Exception:  # noqa: BLE001 — no settings store on this node
            return None
    try:
        return OpenFgaConfig.from_settings(settings)
    except Exception as exc:  # noqa: BLE001
        if isinstance(exc, ValueError):
            raise
        return None


def client_from_config(
    cfg: OpenFgaConfig, *, transport: Transport | None = None
) -> OpenFgaHttpClient:
    transport = transport or HttpxTransport(cfg.url, token=cfg.token, timeout=cfg.timeout)
    return OpenFgaHttpClient(
        transport=transport,
        store_id=cfg.store_id,
        model_id=cfg.model_id,
        consistency=cfg.consistency,
    )


def substrate_from_config(
    cfg: OpenFgaConfig, *, transport: Transport | None = None, mapper: Any = None
) -> OpenFgaSubstrate:
    client = client_from_config(cfg, transport=transport)
    kwargs: dict[str, Any] = {"client": client, "on_error": cfg.on_error}
    if mapper is not None:
        kwargs["mapper"] = mapper
    return OpenFgaSubstrate(**kwargs)


def maybe_register_substrate(
    ctx: Any,
    *,
    env: Mapping[str, str] | None = None,
    settings: Any = None,
    transport: Transport | None = None,
) -> bool:
    """Point a ``DecideContext`` at OpenFGA when one is configured.

    Returns True when the substrate was installed, False when nothing is
    configured (GUARD keeps ``NullSubstrate``). A half-configured setup raises
    — silently running without the substrate a deployment asked for is the
    failure ADR-083 exists to prevent. Never touches the network here; the
    first ``check`` does.
    """
    cfg = resolve_config(env, settings)
    if cfg is None:
        return False
    ctx.substrate = substrate_from_config(cfg, transport=transport)
    _LOG.info(
        "GUARD substrate: OpenFGA at %s store=%s on_error=%s",
        cfg.url,
        cfg.store_id,
        cfg.on_error.name.lower(),
    )
    return True


__all__ = [
    "HIGHER_CONSISTENCY",
    "MINIMIZE_LATENCY",
    "MODEL_ENV",
    "ON_ERROR_ENV",
    "READ_PAGE",
    "SETTINGS_PREFIX",
    "STORE_ENV",
    "TOKEN_ENV",
    "TOKEN_FILE_ENV",
    "URL_ENV",
    "WRITE_CHUNK",
    "HttpxTransport",
    "OpenFgaConfig",
    "OpenFgaError",
    "OpenFgaHttpClient",
    "Transport",
    "client_from_config",
    "create_store",
    "list_stores",
    "maybe_register_substrate",
    "resolve_config",
    "starter_type_definitions",
    "substrate_from_config",
]
