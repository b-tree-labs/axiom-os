# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``secrets`` — operational secret store, factory/provider pattern.

This extension is the single source of operational credentials for the rest
of the platform: database passwords, API tokens, OAuth session blobs,
DSN fragments. It is **not** the governance vault primitive (KEEP /
ADR-055) — see ``docs/decisions/adr-001-secrets-vs-keep.md`` for the
delineation.

Public surface (other extensions consume only these)::

    from axiom.extensions.builtins.secrets import resolve, SecretRef, Secret

    ref = SecretRef.parse("openbao://kv/data/example-host/dp1/db/password")
    with resolve(ref, ctx) as secret:
        connect(password=secret.value)

Providers register at import time via ``SecretStoreRegistry.register``.
SEC-2 ships the ``openbao`` + ``env`` providers and the resolve()
implementation; ``kubernetes`` (CSI) lands in SEC-3.
"""

from __future__ import annotations

import os

from .providers import (
    AWSSecretsManagerProvider,
    AzureKeyVaultProvider,
    Capabilities,
    EnvSecretStoreProvider,
    GCPSecretManagerProvider,
    KubernetesSecretStoreProvider,
    OpenBaoSecretStoreProvider,
    Secret,
    SecretRef,
    SecretStore,
    SecretStoreProvider,
    SecretStoreRegistry,
)

__all__ = (
    "AWSSecretsManagerProvider",
    "AzureKeyVaultProvider",
    "Capabilities",
    "EnvSecretStoreProvider",
    "GCPSecretManagerProvider",
    "KubernetesSecretStoreProvider",
    "OpenBaoSecretStoreProvider",
    "Secret",
    "SecretRef",
    "SecretStore",
    "SecretStoreProvider",
    "SecretStoreRegistry",
    "SecretStoreUnavailable",
    "default_provider",
    "default_scheme",
    "resolve",
)


def resolve(ref: SecretRef, ctx=None) -> Secret:  # noqa: ANN001 — ctx is SkillContext
    """Resolve a ``SecretRef`` to a ``Secret`` via the registered provider.

    Cross-extension consumption API — ``data_platform``, ``rag``,
    ``connect``, and future federation outbound all reach for a secret
    through this function rather than importing a concrete provider
    directly. The hop is what makes the audit trail land in one place
    and what keeps providers swappable per-install.

    The provider is selected by ``ref.scheme`` and resolved against the
    currently-configured provider config (a follow-up will wire that
    config; for SEC-2 we instantiate a default per-kind provider on the
    fly from environment / sensible defaults).
    """
    try:
        provider_cls = SecretStoreRegistry.get(ref.scheme)
    except KeyError as exc:
        raise KeyError(
            f"no SecretStoreProvider registered for scheme {ref.scheme!r}; "
            f"known: {SecretStoreRegistry.available_kinds()}"
        ) from exc

    config = _default_config_for_scheme(ref.scheme)
    provider = provider_cls(config)
    store = provider.open()
    return store.get(ref)


def _default_config_for_scheme(scheme: str) -> dict:
    """Minimal per-scheme config inferred from env. SEC-3 will add a
    proper config-file loader so multiple named providers can coexist."""
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
    if scheme == "gcp":
        return {
            "name": f"default-{scheme}",
            "project": (
                os.environ.get("AXIOM_GCP_PROJECT")
                or os.environ.get("GOOGLE_CLOUD_PROJECT")
                or None
            ),
        }
    if scheme == "aws":
        return {
            "name": f"default-{scheme}",
            "region": (
                os.environ.get("AXIOM_AWS_REGION")
                or os.environ.get("AWS_REGION")
                or os.environ.get("AWS_DEFAULT_REGION")
                or None
            ),
        }
    if scheme == "azure":
        cfg = {
            "name": f"default-{scheme}",
            "vault": (
                os.environ.get("AXIOM_AZURE_VAULT")
                or os.environ.get("AZURE_KEYVAULT_NAME")
                or None
            ),
        }
        tmpl = os.environ.get("AXIOM_AZURE_VAULT_URL_TEMPLATE")
        if tmpl:
            cfg["vault_url_template"] = tmpl
        return cfg
    return {"name": f"default-{scheme}"}


_DEFAULT_SCHEME = "openbao"


class SecretStoreUnavailable(RuntimeError):
    """The configured default SecretStore backend is not reachable.

    Raised by :func:`default_provider` when the default backend fails its
    ``available()`` preflight outside ``AXIOM_MODE=dev`` — the store fails
    *closed* rather than silently degrading to plaintext ``env``.
    """


def _mode() -> str:
    """Current deployment mode: ``dev`` | ``staging`` | ``production``."""
    try:
        from axiom.governance.mode import current_mode

        return current_mode()
    except Exception:
        raw = (os.environ.get("AXIOM_MODE") or "dev").strip().lower()
        return raw if raw in ("dev", "staging", "production") else "dev"


def default_scheme() -> str:
    """The configured default SecretStore backend scheme.

    Precedence: ``AXIOM_SECRETS_DEFAULT`` env (a config-file ``[secrets]
    default`` loader lands with SEC-3) → ``openbao``. OpenBao is the platform
    default per ADR-003 — self-hosted, enclave-appropriate, no cloud
    dependency — with the cloud managers (``aws``/``gcp``/``azure``) as opt-in
    overrides.
    """
    return (os.environ.get("AXIOM_SECRETS_DEFAULT") or _DEFAULT_SCHEME).strip().lower()


def default_provider() -> SecretStoreProvider:
    """Construct the configured default provider, **fail-closed** on preflight.

    The default backend (:func:`default_scheme`) must pass ``available()``. In
    ``AXIOM_MODE=dev`` an unavailable default degrades to the ``env`` provider
    (which logs its own plaintext-at-rest warning) so local work isn't blocked;
    in staging/production an unavailable default raises
    :class:`SecretStoreUnavailable` — never a silent plaintext fallback.
    """
    scheme = default_scheme()
    provider = SecretStoreRegistry.get(scheme)(_default_config_for_scheme(scheme))
    if provider.available():
        return provider
    if _mode() == "dev" and scheme != "env":
        return SecretStoreRegistry.get("env")(_default_config_for_scheme("env"))
    raise SecretStoreUnavailable(
        f"default SecretStore backend {scheme!r} is unavailable "
        f"(AXIOM_MODE={_mode()}); refusing to fall back to plaintext `env` "
        f"outside dev — reach/configure {scheme} or set AXIOM_SECRETS_DEFAULT."
    )
