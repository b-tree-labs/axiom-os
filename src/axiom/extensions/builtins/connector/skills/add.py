# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``notifications.connector_add`` skill — interactive connector wizard.

Friction-killer track #3: ``axi notifications connector add <vendor>``
collapses the 5-step install into a single command. Per ADR-056, the
CLI verb is a thin wrapper over this function.

Params shape::

    {
        "vendor": "slack",                # required
        "name": "acme",                   # optional sub-slug, default "default"
        "no_test_send": False,            # optional
        "_answers": {...},                # optional — test override
        "_secret_put": callable,          # optional — test override
        "_test_send": callable,           # optional — test override
        "_browser_open": callable,        # optional — test override
    }

The leading-underscore params are escape hatches for tests + agent
callers that want to drive the wizard non-interactively; the CLI never
sets them.
"""

from __future__ import annotations

import logging
from typing import Any

from axiom.extensions.builtins.notifications.skills.send import _ctx
from axiom.extensions.builtins.connector.wizard import (
    ConnectorWizard,
    DictInputProvider,
    get_handler,
    list_vendors,
)
from axiom.infra.skills import SkillContext, SkillResult

_log = logging.getLogger(__name__)


def _default_secret_put(path: str, value: bytes) -> None:
    """Default secret-write path — uses the ``secrets`` extension's
    OpenBao-or-env-backed store.

    Kept lazy so the wizard tests don't pull in the secrets extension
    transitively (and so an install without ``secrets`` configured
    falls back gracefully).
    """
    try:
        from axiom.extensions.builtins.secrets import (
            SecretRef,
            SecretStoreRegistry,
            _default_config_for_scheme,
        )
    except ImportError:
        _log.warning(
            "secrets extension unavailable; secret %s not persisted", path
        )
        return

    # Default scheme: env for the bootstrap path. Operators pointing
    # ``AXIOM_SECRETS_DEFAULT_SCHEME=openbao`` get OpenBao instead.
    import os
    scheme = os.environ.get("AXIOM_SECRETS_DEFAULT_SCHEME", "env")
    try:
        provider_cls = SecretStoreRegistry.get(scheme)
    except KeyError:
        _log.warning(
            "no secret provider for scheme %r; secret %s not persisted",
            scheme,
            path,
        )
        return
    config = _default_config_for_scheme(scheme)
    store = provider_cls(config).open()
    ref = SecretRef(scheme=scheme, path=path)
    try:
        store.put(ref, value)
    except Exception as exc:  # noqa: BLE001
        _log.warning("failed to persist secret %s: %s", path, exc)


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    vendor = params.get("vendor")
    if not vendor:
        return SkillResult(
            ok=False, errors=["missing required param: vendor"]
        )

    # Fail fast on unknown vendor — so the operator gets the supported
    # list before the wizard tries to open a browser.
    try:
        get_handler(vendor)
    except KeyError as exc:
        return SkillResult(
            ok=False,
            errors=[str(exc)],
            value={"supported_vendors": list_vendors()},
        )

    name = params.get("name") or "default"
    no_test_send = bool(params.get("no_test_send"))

    # Wire the wizard's collaborators. The shared SendContext owns the
    # in-process channel-adapter registry — registering through it
    # means the next `axi notifications send` already sees the channel.
    send_ctx = _ctx()
    secret_put = params.get("_secret_put") or _default_secret_put
    test_send = params.get("_test_send")
    browser_open = params.get("_browser_open")

    answers = params.get("_answers")
    if answers is not None:
        input_provider = DictInputProvider(answers)
    else:
        from axiom.extensions.builtins.connector.wizard import (
            StdinInputProvider,
        )
        input_provider = StdinInputProvider()

    wiz_kwargs: dict[str, Any] = {
        "registry": send_ctx.registry,
        "secret_put": secret_put,
        "input_provider": input_provider,
    }
    if test_send is not None:
        wiz_kwargs["test_send"] = test_send
    if browser_open is not None:
        wiz_kwargs["browser_open"] = browser_open

    wiz = ConnectorWizard(**wiz_kwargs)
    result = wiz.run(vendor=vendor, name=name, no_test_send=no_test_send)

    actions: list[str] = []
    if result.secret_paths:
        actions.append(
            f"stored {len(result.secret_paths)} secret(s): "
            + ", ".join(result.secret_paths)
        )
    if result.ok:
        actions.append(f"registered {vendor}:{name}")
        if not no_test_send:
            actions.append(f"test send → {result.test_send_receipt}")

    value: dict[str, Any] = {
        "resource": "connector",
        "vendor": result.vendor,
        "name": result.name,
        "config": result.config,
        "secret_paths": result.secret_paths,
        "notes": result.notes,
        "test_send_receipt": result.test_send_receipt,
        "ready": result.ok,
    }

    return SkillResult(
        ok=result.ok,
        value=value,
        actions_taken=actions,
        errors=result.errors,
    )


__all__ = ["run"]
