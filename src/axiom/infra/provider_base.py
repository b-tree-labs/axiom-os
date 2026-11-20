# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Shared base classes for all platform provider/factory types.

Every configurable provider in the platform — LLM, log sink, storage, signal
source, publisher, issue tracker — carries the same forensic identity and
exposes the same operational contract. This module is the single source of
that shared behaviour.

Two abstractions:

    ProviderIdentityMixin
        Pure mixin; no __init__. Works with @dataclass via __post_init__.
        Adds: uid, config_hash, instance_id, identity property.
        Subclasses declare _log_prefix and _fingerprint_fields.

    ProviderBase(ProviderIdentityMixin, ABC)
        Full base for non-dataclass providers (ABCs, plain classes).
        Adds: config dict handling, required-field validation, available(),
              describe(), _logger, handles_sensitive_data.

The four-layer identity model (see ADR-012):

    uid               Stable unique identifier — persistent across renames and
                      restarts when stored in config. The true runtime key.
                      Auto-generated (UUID4) if absent from config; a WARNING
                      is emitted so the operator can persist it.
    display_name      Human-readable label (config key: "name"). Shown in
                      UI and logs for readability. May change freely without
                      breaking forensic correlation — uid is the stable key.
    config_hash       8-char SHA-256 fingerprint of identity-relevant config
                      fields. Stable while config unchanged; changes on drift.
    instance_id       UUID4 per instantiation. Intentionally NOT stable across
                      restarts — distinguishes reloads in forensic timelines.

Usage in a non-dataclass provider:

    from axiom.infra.provider_base import ProviderBase

    class MyStorageProvider(ProviderBase):
        _log_prefix = "storage_provider"
        _fingerprint_fields = ("bucket", "region")
        _required_config = ("bucket",)

        def upload(self, path): ...

    p = MyStorageProvider({"name": "S3 Primary", "uid": "a1b2c3d4-...", "bucket": "my-bucket"})
    logger.info("Uploading", extra=p.identity)
    # → {"storage_provider": "S3 Primary", "storage_provider_uid": "a1b2c3d4-...", ...}

Usage in a @dataclass provider (identity mixin only):

    from dataclasses import dataclass, field
    from axiom.infra.provider_base import ProviderIdentityMixin

    @dataclass
    class LLMProvider(ProviderIdentityMixin):
        name: str
        uid: str = ""           # from config; auto-generated if blank
        endpoint: str = ""
        model: str = ""
        _log_prefix: ClassVar[str] = "llm_provider"
        _fingerprint_fields: ClassVar[tuple] = ("endpoint", "model")
        config_hash: str = field(default="", init=False)
        instance_id: str = field(default="", init=False)

        def __post_init__(self):
            uid_was_generated = self._compute_identity(
                {"uid": self.uid, "endpoint": self.endpoint, "model": self.model}
            )
            self.uid = self.uid or self._generated_uid  # pick up auto-generated value
            if uid_was_generated:
                import logging
                logging.getLogger(__name__).warning(
                    "Provider '%s' has no 'uid' in config — generated uid=%s. "
                    "Add uid = \\"%s\\" to persist it across restarts.",
                    self.name, self.uid, self.uid,
                )
"""

from __future__ import annotations

import logging
import uuid
from abc import ABC
from pathlib import Path
from typing import Any

from axiom.infra.hash_utils import SHORT
from axiom.infra.hash_utils import fingerprint as hash_fingerprint

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ProviderIdentityMixin
# ---------------------------------------------------------------------------

class ProviderIdentityMixin:
    """Four-layer identity mixin for any provider type.

    Subclasses declare two class variables:

        _log_prefix: str
            Prefix for log record field names. E.g. "llm_provider" produces
            keys "llm_provider", "llm_provider_uid", "llm_provider_config_hash",
            and "llm_provider_instance" in the identity dict. Always use the
            specific entity type — never a bare "provider".

        _fingerprint_fields: tuple[str, ...]
            Config keys whose values are hashed to produce config_hash. Choose
            fields that meaningfully identify this provider's configuration —
            typically connection target, model/version, and access tier. Omit
            operational fields like "level", "enabled", "priority".

    Call _compute_identity(config_dict) once during construction (from
    __post_init__ for dataclasses, from __init__ for plain classes).

    Returns True if uid was auto-generated (not present in config), so
    callers can emit the appropriate warning.
    """

    _log_prefix: str = "provider"
    _fingerprint_fields: tuple[str, ...] = ()

    # Set by _compute_identity — declared here for type checkers
    uid: str
    config_hash: str
    instance_id: str
    name: str

    def _compute_identity(self, config: dict[str, Any]) -> bool:
        """Compute uid, config_hash, and instance_id from a config dict.

        Returns True if uid was auto-generated (not present in config).
        Callers that have a logger should emit a warning in that case so
        operators know to persist the uid in their config file.
        """
        provided_uid = config.get("uid", "")
        if provided_uid:
            self.uid = provided_uid
            uid_was_generated = False
        else:
            self.uid = str(uuid.uuid4())
            uid_was_generated = True

        fp_text = "|".join(str(config.get(f, "")) for f in self._fingerprint_fields)
        self.config_hash = hash_fingerprint(fp_text, length=SHORT)
        self.instance_id = uuid.uuid4().hex[:12]

        return uid_was_generated

    @property
    def identity(self) -> dict[str, str]:
        """Full identity dict for inclusion in log/audit records.

        Minimal form (most log records — readable, low volume):
            extra={"llm_provider": provider.name, "llm_provider_uid": provider.uid}

        Full form (session start, audit records):
            extra=provider.identity
        """
        return {
            self._log_prefix: self.name,
            f"{self._log_prefix}_uid": self.uid,
            f"{self._log_prefix}_config_hash": self.config_hash,
            f"{self._log_prefix}_instance": self.instance_id,
        }


# ---------------------------------------------------------------------------
# ProviderBase
# ---------------------------------------------------------------------------

class ProviderBase(ProviderIdentityMixin, ABC):
    """Full base class for non-dataclass platform providers.

    Handles the concerns every provider shares:
      - Config dict as the single constructor argument
      - Required field validation (declare in _required_config)
      - Four-layer identity (from ProviderIdentityMixin)
      - uid auto-generation warning (emitted here after logger is available)
      - Health check (available())
      - Human-readable description (describe())
      - Pre-wired logger (_logger)
      - Sensitive data flag (handles_sensitive_data)

    Subclasses MUST declare _log_prefix. They SHOULD declare
    _fingerprint_fields and _required_config. They MAY override
    available() and handles_sensitive_data.
    """

    _required_config: tuple[str, ...] = ()
    handles_sensitive_data: bool = False

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config

        # Display name: user-facing label, may change freely
        self.name: str = config.get("name") or type(self).__name__.lower()

        # Validate required fields early with a clear error
        missing = [f for f in self._required_config if f not in config]
        if missing:
            raise ValueError(
                f"{type(self).__name__} missing required config field(s): "
                + ", ".join(f"'{f}'" for f in missing)
            )

        # Compute four-layer identity
        uid_was_generated = self._compute_identity(config)

        # Logger — pre-wired, uses __name__ of the concrete class's module
        # to preserve namespace-based level filtering in logging.toml
        self._logger: logging.Logger = logging.getLogger(
            f"{type(self).__module__}.{type(self).__qualname__}"
        )

        # Warn if uid was not in config so operator can persist it
        if uid_was_generated:
            self._logger.warning(
                "Provider '%s' has no 'uid' in config — generated uid=%s. "
                'Add uid = "%s" to your config to persist it across restarts.',
                self.name, self.uid, self.uid,
            )

    def available(self) -> bool:
        """Return True if this provider is configured and reachable.

        Override to add connectivity checks (e.g., API key present, TCP probe).
        The default assumes the provider is always available after __init__
        succeeds. Callers should not cache this result — call it fresh before
        each use.
        """
        return True

    def describe(self) -> str:
        """Human-readable one-line description for CLI output and axi doctor.

        Format: "<log_prefix>:<name> [uid=<uid> config_hash=<hash>]"
        Subclasses may override to add type-specific details.
        """
        return f"{self._log_prefix}:{self.name} [uid={self.uid[:8]}… config_hash={self.config_hash}]"


# ---------------------------------------------------------------------------
# TOML uid back-fill helper — used by factory loaders
# ---------------------------------------------------------------------------

def ensure_provider_uids(path: Path, table_key: str = "") -> bool:
    """Scan a provider TOML config and write back any missing uids in-place.

    Generates a UUID4 for every entry that lacks a ``uid`` field, then
    rewrites the file using tomlkit (preserves comments and formatting).
    Returns True if any uids were written, False if the file was already
    complete.

    Args:
        path:       Path to the TOML config file.
        table_key:  Dotted key path to the list of provider entries within the
                    document.  E.g. ``"log.sinks"`` for ``[[log.sinks]]``,
                    ``"providers"`` for ``[[providers]]``, or ``""`` (default)
                    to treat the top-level value as the list itself.

    This should be called by factory loaders *before* they instantiate
    providers, so that the uid is present in the config dict by the time
    ``_compute_identity()`` runs — suppressing the runtime warning.

    Example (LogSinkFactory)::

        from axiom.infra.provider_base import ensure_provider_uids
        ensure_provider_uids(path, table_key="log.sinks")
        # … then load_from_toml as normal

    Example (Gateway)::

        ensure_provider_uids(path, table_key="providers")
    """
    path = Path(path)
    if not path.exists():
        return False

    try:
        import tomlkit
    except ImportError:
        _log.warning(
            "tomlkit not installed — cannot write provider uids back to %s. "
            "pip install tomlkit",
            path,
        )
        return False

    try:
        text = path.read_text(encoding="utf-8")
        doc = tomlkit.parse(text)
    except Exception as exc:
        _log.warning("ensure_provider_uids: failed to parse %s: %s", path, exc)
        return False

    # Navigate to the list of provider entries
    node: Any = doc
    if table_key:
        for key in table_key.split("."):
            try:
                node = node[key]
            except (KeyError, TypeError):
                return False  # key path absent — nothing to do

    if not isinstance(node, list):
        return False

    # Fill in missing uids
    wrote_any = False
    for entry in node:
        if isinstance(entry, dict) and not entry.get("uid"):
            entry["uid"] = str(uuid.uuid4())
            wrote_any = True

    # Detect duplicate uids (e.g. from copy-paste) — log ERROR for each dupe.
    # Cannot auto-fix: we don't know which entry is the "real" one.
    seen_uids: dict[str, str] = {}  # uid → display name of first occurrence
    for entry in node:
        if not isinstance(entry, dict):
            continue
        uid = entry.get("uid", "")
        name = entry.get("name", "<unnamed>")
        if not uid:
            continue
        if uid in seen_uids:
            _log.error(
                "Duplicate provider uid '%s' in %s: entries '%s' and '%s' share the same uid. "
                "The second entry will be skipped at load time. "
                "Edit the file and assign a unique uid to one of them.",
                uid, path, seen_uids[uid], name,
            )
        else:
            seen_uids[uid] = name

    if wrote_any:
        try:
            import os
            import tempfile
            new_text = tomlkit.dumps(doc)
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=path.parent, prefix=path.stem, suffix=".tmp"
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    f.write(new_text)
                os.replace(tmp_path, path)  # atomic on POSIX and Windows
            except Exception:
                os.unlink(tmp_path)
                raise
            _log.info("ensure_provider_uids: wrote missing uids to %s", path)
        except Exception as exc:
            _log.warning(
                "ensure_provider_uids: failed to write back to %s: %s", path, exc
            )
            return False

    return wrote_any
