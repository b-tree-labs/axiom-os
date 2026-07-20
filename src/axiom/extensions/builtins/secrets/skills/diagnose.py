# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``secrets.diagnose`` — operator pre-flight for the secret-store wiring.

Two modes:

- **walk** (no ``ref``): iterate every kind in ``SecretStoreRegistry``,
  construct it with the per-scheme default config, probe ``available()``.
  Reports one item per registered kind.

- **resolve** (``ref`` provided): parse the ref, look up its scheme's
  provider, construct it, probe ``available()``, then attempt
  ``open().get(ref)``. Reports a single item.

The diagnose surface is a pre-flight, not a secret-dump. We surface the
LENGTH of any resolved value, never the value itself.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..providers.protocol import SecretRef
from ..providers.registry import SecretStoreRegistry


def _default_config_for_scheme(scheme: str) -> dict:
    # Mirrors ``secrets.__init__._default_config_for_scheme``. Kept
    # local so the diagnose skill doesn't import the resolve()
    # entry point (cleaner test isolation, no circulars).
    import os
    if scheme == "openbao":
        return {
            "name": f"default-{scheme}",
            "url": os.environ.get("AXIOM_OPENBAO_URL", "http://localhost:8200"),
            "token": os.environ.get("AXIOM_OPENBAO_TOKEN", ""),
            "mount": os.environ.get("AXIOM_OPENBAO_MOUNT", "kv"),
        }
    if scheme == "env":
        return {
            "name": f"default-{scheme}",
            "prefix": os.environ.get("AXIOM_ENV_SECRET_PREFIX", ""),
        }
    if scheme == "kubernetes":
        return {
            "name": f"default-{scheme}",
            "kube_context": os.environ.get("AXIOM_KUBE_CONTEXT") or None,
            "in_cluster": bool(os.environ.get("KUBERNETES_SERVICE_HOST")),
        }
    return {"name": f"default-{scheme}"}


def _probe_kind(kind: str) -> dict[str, Any]:
    """Construct + check availability of a single registered kind."""
    item: dict[str, Any] = {
        "kind": kind,
        "registered": True,
        "constructible": False,
        "available": False,
        "error": None,
    }
    try:
        provider_cls = SecretStoreRegistry.get(kind)
    except KeyError as exc:
        item["registered"] = False
        item["error"] = str(exc)[:200]
        return item
    try:
        provider = provider_cls(_default_config_for_scheme(kind))
        item["constructible"] = True
    except Exception as exc:  # noqa: BLE001 - operator surface
        item["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return item
    try:
        item["available"] = bool(provider.available())
    except Exception as exc:  # noqa: BLE001 - operator surface
        item["available"] = False
        item["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    return item


def _probe_ref(ref_str: str) -> dict[str, Any]:
    """Parse, construct, probe, resolve a specific SecretRef."""
    item: dict[str, Any] = {
        "ref": ref_str,
        "scheme": None,
        "registered": False,
        "constructible": False,
        "available": False,
        "resolved": False,
        "value_length": None,
        "error": None,
    }
    try:
        ref = SecretRef.parse(ref_str)
    except Exception as exc:  # noqa: BLE001 - operator surface
        item["error"] = f"parse: {type(exc).__name__}: {str(exc)[:200]}"
        return item
    item["scheme"] = ref.scheme

    try:
        provider_cls = SecretStoreRegistry.get(ref.scheme)
        item["registered"] = True
    except KeyError as exc:
        item["error"] = str(exc)[:200]
        return item

    try:
        provider = provider_cls(_default_config_for_scheme(ref.scheme))
        item["constructible"] = True
    except Exception as exc:  # noqa: BLE001 - operator surface
        item["error"] = f"construct: {type(exc).__name__}: {str(exc)[:200]}"
        return item

    try:
        item["available"] = bool(provider.available())
    except Exception as exc:  # noqa: BLE001 - operator surface
        item["available"] = False
        item["error"] = f"available: {type(exc).__name__}: {str(exc)[:200]}"
        return item

    try:
        store = provider.open()
        secret = store.get(ref)
        # NEVER write secret.value into the result. Length only.
        item["resolved"] = True
        item["value_length"] = len(secret.value)
    except Exception as exc:  # noqa: BLE001 - operator surface
        item["resolved"] = False
        item["error"] = f"resolve: {type(exc).__name__}: {str(exc)[:200]}"
    return item


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    ref = params.get("ref")
    errors: list[str] = []

    if ref:
        items = [_probe_ref(ref)]
        ok = items[0]["resolved"] is True
        if not ok and items[0].get("error"):
            errors.append(items[0]["error"])
    else:
        kinds = SecretStoreRegistry.available_kinds()
        items = [_probe_kind(k) for k in kinds]
        ok = bool(items) and all(i["available"] for i in items)
        for i in items:
            if not i["available"] and i.get("error"):
                errors.append(f"{i['kind']}: {i['error']}")
        if not items:
            ok = False
            errors.append("no SecretStoreProvider kinds registered")

    return SkillResult(
        ok=ok,
        value={"resource": "diagnose", "items": items},
        errors=errors,
    )
