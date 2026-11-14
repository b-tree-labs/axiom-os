# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0

"""``aws`` SecretStoreProvider — AWS Secrets Manager.

AWS-native operational secret store. Authenticates via the standard
boto3 credential chain (instance/task IAM roles, ``AWS_*`` env vars,
shared config/credentials files, SSO) — no credential handling lives
in this provider.

``SecretRef`` shape::

    aws://<secret-id>                       # AWSCURRENT (default)
    aws://<secret-id>?stage=AWSPREVIOUS     # a specific staging label
    aws://<secret-id>?version=<uuid>        # a specific VersionId
    aws://<region>/<secret-id>              # region in the ref
    aws://<region>/<secret-id>?stage=AWSPREVIOUS

``<secret-id>`` is the Secrets Manager name or ARN. A leading
``<region>/`` segment is optional sugar; the region otherwise comes from
provider config (``region=``) or the ambient boto3 session.

Overlap-window rotation maps directly onto Secrets Manager's own model.
SM keeps multiple versions of a secret, each tagged with staging
*labels*. During rotation both the new and the prior secret stay valid:

    AWSCURRENT   -> the freshly-rotated value
    AWSPREVIOUS  -> the value it replaced (still accepted by the backend)

``resolve_overlap()`` returns *every* value a verifier should accept
right now (AWSCURRENT first, then AWSPREVIOUS) so a consumer keeps
working whether the caller presents the new or the old secret. A missed
manual flip therefore never outages: the old secret is honored until
SM's own label retirement removes the ``AWSPREVIOUS`` label.

Note on the ``version`` field: ``SecretRef.version`` is an ``int`` in the
shared protocol, but Secrets Manager VersionIds are UUIDs. We thread a
UUID VersionId through the ``?version=`` *query* (string) instead, and
expose staging labels through ``?stage=``. The integer ``ref.version``
field is unused by this provider.

boto3 is imported lazily so the wheel stays light for installs that
don't use this provider (declared via the ``[storage]`` extra, which
already pins ``boto3``).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, ClassVar

from ..providers.protocol import (
    Capabilities,
    Secret,
    SecretRef,
    SecretStore,
    SecretStoreProvider,
)

_log = logging.getLogger(__name__)

# Staging labels Secrets Manager applies during rotation. The overlap
# window is exactly {AWSCURRENT, AWSPREVIOUS}: both are accepted by the
# backend until rotation completes and the old label is retired.
_STAGE_CURRENT = "AWSCURRENT"
_STAGE_PREVIOUS = "AWSPREVIOUS"


class _AWSSecretsManagerStore:
    """Runtime client. Wraps a boto3 ``secretsmanager`` client."""

    capabilities = Capabilities(
        read=True,
        write=True,
        delete=True,
        list_paths=True,
        # SM versions are UUIDs, not the protocol's int versions; we
        # advertise False for the int-versioned read contract and expose
        # version/stage selection through the query instead.
        versions=False,
        dynamic_credentials=False,
        # SM drives rotation itself (Lambda + schedule); rotate() asks SM
        # to run a rotation now rather than implementing one in-process.
        rotation=True,
        audit_stream=True,  # CloudTrail
        encryption_at_rest=True,  # KMS, AWS-managed key by default
    )

    def __init__(self, *, default_region: str | None, client: Any) -> None:
        self._default_region = default_region
        self._client = client

    # ---- helpers --------------------------------------------------------

    def _secret_id(self, ref: SecretRef) -> str:
        """``aws://<id>`` or ``aws://<region>/<id>`` -> SM SecretId.

        A leading ``<region>/`` segment is optional sugar. ARNs (which
        start with ``arn:``) and plain names both pass through unchanged
        after the optional region prefix is stripped.
        """
        path = ref.path
        if not path:
            raise ValueError(f"aws SecretRef missing secret id: {ref!r}")
        if "/" in path and not path.startswith("arn:"):
            head, tail = path.split("/", 1)
            if _looks_like_region(head) and tail:
                return tail
        return path

    def _version_kwargs(self, ref: SecretRef) -> dict[str, str]:
        """Translate ``?version=`` / ``?stage=`` into get_secret_value kwargs.

        Precedence: explicit VersionId wins; else a staging label; else
        the backend default (AWSCURRENT).
        """
        kwargs: dict[str, str] = {}
        version_id = ref.query.get("version")
        stage = ref.query.get("stage")
        if version_id:
            kwargs["VersionId"] = version_id
        elif stage:
            kwargs["VersionStage"] = stage
        return kwargs

    def _get_raw(self, secret_id: str, **kwargs: str) -> Secret:
        try:
            resp = self._client.get_secret_value(SecretId=secret_id, **kwargs)
        except Exception as exc:  # noqa: BLE001 — translate SDK errors
            code = _sdk_status(exc)
            if code == 404:
                raise KeyError(
                    f"aws: no secret {secret_id!r} ({kwargs or 'AWSCURRENT'})"
                ) from exc
            if code in (401, 403):
                raise PermissionError(
                    f"aws denied {secret_id!r}: {exc}"
                ) from exc
            raise RuntimeError(
                f"aws get_secret_value failed for {secret_id!r}: {exc}"
            ) from exc

        value = _payload_bytes(resp)
        return Secret(
            value=value,
            metadata={
                "backend": "aws_secrets_manager",
                "secret_id": secret_id,
                "arn": resp.get("ARN"),
                "version_id": resp.get("VersionId"),
                "stages": list(resp.get("VersionStages", []) or []),
            },
            lease_id=None,
            version=None,  # SM VersionIds are UUIDs; see module docstring
        )

    # ---- SecretStore Protocol ------------------------------------------

    def get(self, ref: SecretRef) -> Secret:
        secret_id = self._secret_id(ref)
        return self._get_raw(secret_id, **self._version_kwargs(ref))

    def resolve_overlap(self, ref: SecretRef) -> list[Secret]:
        """Every secret value a verifier should accept *right now*.

        Returns ``[AWSCURRENT]`` plus ``[AWSPREVIOUS]`` when one exists,
        in acceptance order. This is the load-bearing primitive for
        rotation-without-outage: a consumer that checks an incoming
        secret against this list keeps working across the entire overlap
        window, regardless of whether the new or old value is presented.

        If the ref pins an explicit ``?version=`` / ``?stage=``, overlap
        is meaningless — we return just that single requested value.
        """
        if ref.query.get("version") or ref.query.get("stage"):
            return [self.get(ref)]

        secret_id = self._secret_id(ref)
        out: list[Secret] = [
            self._get_raw(secret_id, VersionStage=_STAGE_CURRENT)
        ]
        try:
            previous = self._get_raw(secret_id, VersionStage=_STAGE_PREVIOUS)
        except KeyError:
            # No prior version yet (never rotated) — current only.
            return out
        # Skip the duplicate when CURRENT and PREVIOUS point at the same
        # VersionId (can happen immediately post-create).
        cur_vid = out[0].metadata.get("version_id")
        prev_vid = previous.metadata.get("version_id")
        if prev_vid and prev_vid != cur_vid:
            out.append(previous)
        return out

    def put(self, ref: SecretRef, value: bytes) -> None:
        """Idempotent upsert. Creates the secret if absent, else stages a
        new AWSCURRENT version (the prior one auto-demotes to AWSPREVIOUS
        — SM gives us the overlap window for free on every write)."""
        secret_id = self._secret_id(ref)
        try:
            self._client.describe_secret(SecretId=secret_id)
        except Exception as exc:  # noqa: BLE001
            if _sdk_status(exc) == 404:
                self._client.create_secret(Name=secret_id, SecretBinary=value)
                return
            raise RuntimeError(
                f"aws describe_secret failed for {secret_id!r}: {exc}"
            ) from exc
        # Existing secret: new version becomes AWSCURRENT, old -> AWSPREVIOUS.
        self._client.put_secret_value(SecretId=secret_id, SecretBinary=value)

    def delete(self, ref: SecretRef) -> None:
        secret_id = self._secret_id(ref)
        # ForceDeleteWithoutRecovery keeps delete() deterministic for
        # tests and ops; SM's default 30-day recovery window would leave
        # the name reserved and confuse idempotent re-creates.
        self._client.delete_secret(
            SecretId=secret_id, ForceDeleteWithoutRecovery=True
        )

    def list_paths(self, prefix: str) -> list[str]:
        # Optional region sugar in the prefix mirrors _secret_id.
        name_prefix = prefix
        if "/" in prefix and not prefix.startswith("arn:"):
            head, tail = prefix.split("/", 1)
            if _looks_like_region(head):
                name_prefix = tail
        try:
            paginator = self._client.get_paginator("list_secrets")
            pages = paginator.paginate()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"aws list_secrets failed: {exc}") from exc
        out: list[str] = []
        for page in pages:
            for s in page.get("SecretList", []):
                name = s.get("Name", "")
                if name and (not name_prefix or name.startswith(name_prefix)):
                    out.append(name)
        return sorted(out)

    def lease(self, ref: SecretRef, ttl_seconds: int) -> Secret:  # pragma: no cover
        raise PermissionError(
            "aws Secrets Manager does not issue leased credentials; use "
            "STS / IAM roles for short-lived AWS credentials"
        )

    def rotate(self, ref: SecretRef) -> None:
        """Ask Secrets Manager to run rotation now.

        This does not rotate in-process — it triggers SM's configured
        rotation Lambda. The secret must already have rotation configured
        (``RotationLambdaARN`` + schedule); otherwise SM rejects the call
        and we surface it. The overlap window (AWSCURRENT/AWSPREVIOUS) is
        produced by SM as a side effect, which is exactly what
        ``resolve_overlap`` consumes.
        """
        secret_id = self._secret_id(ref)
        try:
            self._client.rotate_secret(SecretId=secret_id)
        except Exception as exc:  # noqa: BLE001
            code = _sdk_status(exc)
            if code in (401, 403):
                raise PermissionError(
                    f"aws denied rotate on {secret_id!r}: {exc}"
                ) from exc
            raise RuntimeError(
                f"aws rotate_secret failed for {secret_id!r}: {exc}"
            ) from exc


def _looks_like_region(token: str) -> bool:
    """Heuristic: ``us-east-1`` / ``eu-west-2`` shape. Keeps the optional
    ``aws://<region>/<id>`` sugar from swallowing names that contain a
    slash but no region prefix."""
    parts = token.split("-")
    return (
        len(parts) == 3
        and parts[0].isalpha()
        and parts[1].isalpha()
        and parts[-1].isdigit()
    )


def _payload_bytes(resp: Mapping[str, Any]) -> bytes:
    """Secrets Manager returns either SecretString or SecretBinary."""
    if resp.get("SecretBinary") is not None:
        binary = resp["SecretBinary"]
        return binary if isinstance(binary, bytes) else bytes(binary)
    s = resp.get("SecretString")
    if s is None:
        return b""
    return s.encode("utf-8") if isinstance(s, str) else bytes(s)


def _sdk_status(exc: Exception) -> int | None:
    """Best-effort mapping of a botocore error to an HTTP-like status so
    callers branch on 404 / 403 uniformly, without importing botocore at
    module load."""
    # botocore.exceptions.ClientError carries .response["Error"]["Code"].
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code", "")
        if code in ("ResourceNotFoundException",):
            return 404
        if code in ("AccessDeniedException", "UnauthorizedException"):
            return 403
        meta = response.get("ResponseMetadata", {})
        http = meta.get("HTTPStatusCode")
        if isinstance(http, int):
            return http
    name = type(exc).__name__
    if name in ("ResourceNotFoundException", "NotFound"):
        return 404
    if name in ("AccessDeniedException", "UnauthorizedException", "Forbidden"):
        return 403
    return None


class AWSSecretsManagerProvider(SecretStoreProvider):
    """Factory for the AWS Secrets Manager store."""

    _log_prefix = "secret_store_provider"
    _fingerprint_fields = ("region",)
    kind: ClassVar[str] = "aws"
    capabilities: ClassVar[Capabilities] = _AWSSecretsManagerStore.capabilities

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self._region: str | None = config.get("region") or None
        # Test seam: tests inject a fake/moto client; production builds
        # one from the ambient boto3 session at first use.
        self._client: Any = config.get("_client")

    def _build_client(self) -> Any:
        """Lazily build the boto3 client. Imports boto3 on first call."""
        if self._client is not None:
            return self._client
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise RuntimeError(
                "aws_secrets_manager provider requires the 'boto3' package; "
                "pip install axiom-os-lm[storage]"
            ) from exc
        kwargs: dict[str, Any] = {}
        if self._region:
            kwargs["region_name"] = self._region
        self._client = boto3.client("secretsmanager", **kwargs)
        return self._client

    def open(self) -> SecretStore:  # type: ignore[override]
        return _AWSSecretsManagerStore(
            default_region=self._region,
            client=self._build_client(),
        )

    def available(self) -> bool:  # type: ignore[override]
        """True iff boto3 is importable. We don't probe the API here
        (every ``available()`` call would cost a round-trip); the first
        ``get()`` validates auth + reachability."""
        try:
            import importlib

            importlib.import_module("boto3")
            return True
        except Exception:
            return False


__all__ = ["AWSSecretsManagerProvider"]
