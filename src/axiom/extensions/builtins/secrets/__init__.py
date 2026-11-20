# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The vault's storage module — operational secret store, factory/provider pattern.

Since ADR-218 this is an internal module of the one vault component, owned by
KEEP (``axi vault``); it has no manifest or noun of its own, and its import
path is kept because other repositories import it. It is the single source of
operational credentials for the rest of the platform: database passwords, API
tokens, OAuth session blobs, DSN fragments. Its API stays distinct from the
capability primitive (ADR-055): values are not capabilities (see
``docs/decisions/adr-001-secrets-vs-keep.md``, superseded in part by ADR-218).

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
import uuid

from .providers import (
    AWSSecretsManagerProvider,
    AzureKeyVaultProvider,
    Capabilities,
    EnvSecretStoreProvider,
    FileSecretStoreProvider,
    GCPSecretManagerProvider,
    KeychainSecretStoreProvider,
    KeychainUnavailable,
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
    "FileSecretStoreProvider",
    "GCPSecretManagerProvider",
    "KeychainSecretStoreProvider",
    "KeychainUnavailable",
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

    ``foreign://<name>`` is a credential stored with ``axi vault set <name>``:
    it resolves through the vault's own store (whichever backend this host's
    vault uses), so a connector can name the entry it was set up with instead
    of guessing that backend's scheme.
    """
    if ref.scheme == "foreign":
        from axiom.infra.paths import get_user_state_dir

        from .foreign.store import ForeignCredentialStore

        return ForeignCredentialStore(get_user_state_dir()).get(ref.path)
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


# Namespace for the built-in providers' stable identity. There is exactly one
# default provider per scheme, so its uid is a function of the scheme and
# nothing else. Deriving it here rather than asking every operator to paste a
# UUID into a config file is what makes the identity hold everywhere (ADR-012:
# `uid` is the stable primary key; `config_hash` is what moves when config
# drifts; `instance_id` is deliberately per-load).
_DEFAULT_PROVIDER_NS = uuid.UUID("6f3a1d2e-9c47-5b18-a0d6-7e2b4c8f1a35")


def default_provider_uid(scheme: str) -> str:
    """Stable uid for the built-in provider of ``scheme``.

    Derived from the scheme alone: a rotated token or a re-pointed endpoint is
    the same provider with different config, and that difference belongs in
    ``config_hash``, not in the key its audit records are filed under.
    """
    return str(uuid.uuid5(_DEFAULT_PROVIDER_NS, f"axiom:default-secret-provider:{scheme}"))


def _default_config_for_scheme(scheme: str) -> dict:
    """Per-scheme config with a stable provider identity stamped on.

    Every branch of :func:`_scheme_config` gets its ``uid`` here, so adding a
    scheme cannot reintroduce a provider that renames itself on every restart.
    """
    return {"uid": default_provider_uid(scheme), **_scheme_config(scheme)}


def _scheme_config(scheme: str) -> dict:
    """Minimal per-scheme config inferred from env. SEC-3 will add a
    proper config-file loader so multiple named providers can coexist.

    """
    import os
    if scheme == "openbao":
        # The environment, else the platform's vault settings file (vault_settings), so a process
        # started outside a shell (an MCP server under a desktop app) finds the same vault.
        from . import vault_settings

        return {
            "name": f"default-{scheme}",
            "url": vault_settings.openbao_url(),
            "token": vault_settings.openbao_token(),
            "mount": vault_settings.openbao_mount(),
        }
    if scheme == "env":
        return {
            "name": f"default-{scheme}",
            "prefix": os.environ.get("AXIOM_ENV_SECRET_PREFIX", ""),
        }
    if scheme == "keychain":
        return {
            "name": f"default-{scheme}",
            "service": os.environ.get(
                "AXIOM_KEYCHAIN_SERVICE", "axiom-secrets",
            ),
        }
    if scheme == "file":
        from axiom.infra.paths import get_user_state_dir
        return {
            "name": f"default-{scheme}",
            "path": os.environ.get("AXIOM_FILE_SECRETS_PATH")
            or str(get_user_state_dir() / "secrets" / "foreign-values.json"),
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
