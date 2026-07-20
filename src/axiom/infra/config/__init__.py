# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""`axiom.infra.config` — externalized configuration with watching, validation,
locks, and audit-receipt composition.

Per AEOS §2.13 (state-externalization rule) + ADR-065 (schema-bilingual
config primitive): extensions declare schema, the watcher feeds runtime
updates, the registry is the single source of truth ``get_value`` reads
from. No agent caches configuration in memory.

The five-verb facade below is the Python-dict entry path. ADR-065 adds
an equivalent JSON Schema entry via
:func:`register_schema_from_jsonschema` (re-exported here); the rest of
the primitive (registry, watcher, observer, receipt hook) is shared.

TODO: the original docstring cited "ADR-058 (runtime model)"; current
ADR-058 is the Agent Standards Registry. ADR-065 carries the runtime-
model description for config now.

Five verbs are the entire surface most callers touch::

    from axiom.infra.config import (
        register_schema,     # one-time at extension import
        get_value,           # always reads current value
        write_value,         # api-driven mutation
        observe,             # subscribe to changes
        lock,                # commit a value (compliance lock)
    )

The watcher and receipt-hook plumbing live below; consumers don't
configure them.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable

from axiom.infra.config.jsonschema_loader import (
    load_jsonschema,
    register_schema_from_jsonschema,
)
from axiom.infra.config.observer import (
    ObserverCallback,
    observe,
)
from axiom.infra.config.registry import (
    ChangeRecord,
    ConfigRegistry,
    Field,
    Lock,
    LockedConfigError,
    SchemaError,
    get_registry,
)
from axiom.infra.config.watcher import (
    Watcher,
    load_config_file,
    make_watcher,
)

log = logging.getLogger("axiom.infra.config")

# ---------------------------------------------------------------------------
# Public five-verb facade
# ---------------------------------------------------------------------------


def register_schema(
    extension: str, fields: dict[str, type | dict[str, Any]]
) -> None:
    """Register configurable fields for an extension.

    Each entry's key is the extension-relative field name; each value
    is either a type (uses defaults for classification + lockable) or
    a dict of field options::

        register_schema("expman", {
            "sla_window_hours": int,
            "compliance_recipient": {
                "type": str,
                "classification": "regulated",
                "lockable": True,
                "default": "@compliance:example-org",
            },
        })
    """
    reg = get_registry()
    field_objs: list[Field] = []
    for name, spec in fields.items():
        fq = f"{extension}.{name}"
        if isinstance(spec, type):
            field_objs.append(Field(name=fq, type=spec))
        elif isinstance(spec, dict):
            field_objs.append(
                Field(
                    name=fq,
                    type=spec["type"],
                    default=spec.get("default"),
                    classification=spec.get("classification", "internal"),
                    lockable=spec.get("lockable", True),
                    description=spec.get("description", ""),
                )
            )
        else:
            raise SchemaError(
                f"register_schema {fq!r}: spec must be a type or dict"
            )
    reg.register(*field_objs)


def get_value(key: str, *, default: Any = None) -> Any:
    """Return the current value of a registered config key.

    Returns ``default`` (or the field's declared default) if unset.
    Always reads the *current* state; never caches.
    """
    return get_registry().get(key, default=default)


def write_value(
    key: str,
    value: Any,
    *,
    actor: str = "(unknown)",
    source: str = "api",
    override_capability: object | None = None,
) -> ChangeRecord:
    """Validate + lock-check + commit a value mutation."""
    return get_registry().write(
        key,
        value,
        actor=actor,
        source=source,
        override_capability=override_capability,
    )


def lock(
    key: str,
    *,
    locked_by: str,
    reason: str,
    override_capability_pattern: str | None = None,
) -> None:
    """Commit a value: subsequent ``write_value`` requires an override.

    The keystore session is the right caller — it knows the capability
    requirements + can sign the lock record. This module just stores
    + checks the predicate.
    """
    from datetime import datetime, timezone

    get_registry().lock_key(
        Lock(
            key=key,
            locked_by=locked_by,
            locked_at=datetime.now(timezone.utc),
            reason=reason,
            override_capability_pattern=override_capability_pattern,
        )
    )


def unlock(key: str, *, override_capability: object) -> None:
    """Remove a lock; the caller is responsible for capability check."""
    get_registry().unlock_key(key, override_capability=override_capability)


def lock_status(key: str) -> Lock | None:
    return get_registry().lock_status(key)


# ---------------------------------------------------------------------------
# Watcher bootstrap
# ---------------------------------------------------------------------------


def default_config_dir() -> Path:
    """``$AXIOM_CONFIG_DIR`` if set, else ``~/.axi/config``."""
    env = os.environ.get("AXIOM_CONFIG_DIR")
    if env:
        return Path(env)
    return Path.home() / ".axi" / "config"


_watcher: Watcher | None = None


def start_watching(
    directory: Path | None = None, *, prefer_polling: bool = False
) -> Watcher:
    """Start the filesystem watcher. Idempotent.

    Called once per process (typically by ``setup_extension`` or by the
    CLI bootstrap). Re-calls return the active watcher.
    """
    global _watcher
    if _watcher is not None:
        return _watcher
    dir_ = directory or default_config_dir()
    dir_.mkdir(parents=True, exist_ok=True)

    def _apply(path: Path, values: dict[str, Any]) -> None:
        """Per-file watcher callback. Best-effort; failures don't crash."""
        get_registry().load_dict(
            values, actor="(file)", source=f"file:{path}"
        )

    _watcher = make_watcher(dir_, _apply, prefer_polling=prefer_polling)
    _watcher.start()
    return _watcher


def stop_watching() -> None:
    global _watcher
    if _watcher is not None:
        _watcher.stop()
        _watcher = None


# ---------------------------------------------------------------------------
# Receipt hook (composes with axiom.governance — best-effort, no-op if absent)
# ---------------------------------------------------------------------------


def _wire_receipt_hook() -> Callable[[], None] | None:
    """Connect config change events to the governance fabric's receipt
    layer. Returns a deregister handle (or ``None`` if the fabric isn't
    importable in this process)."""
    try:
        from axiom.extensions.builtins.authz import DecideContext, decide
        from axiom.governance import (
            ActionEnvelope,
            ActionIntent,
            CapabilityToken,
            Classification,
            ProvenanceRef,
            ResourceRef,
            register_intent,
        )
        from axiom.governance.intent import REGISTERED_INTENTS
        from axiom.vega.identity.principal import Principal
    except Exception:
        return None

    # Register the config-mutation intent if not already.
    # ADR-065 PR-1: distinguish file-driven load / reload from api writes
    # so receipts answer "where did this change come from?" without log-grep.
    for intent_name in ("config.write", "config.load", "config.reload"):
        if intent_name not in REGISTERED_INTENTS:
            register_intent(intent_name)

    ctx = DecideContext()
    # Track which keys have ever been written via a file source — first
    # file-driven change is config.load; subsequent ones are config.reload.
    _seen_file_keys: set[str] = set()

    def _classify_intent(record: ChangeRecord) -> str:
        if record.source.startswith("file:"):
            if record.key not in _seen_file_keys:
                _seen_file_keys.add(record.key)
                return "config.load"
            return "config.reload"
        return "config.write"

    def _emit_receipt(record: ChangeRecord) -> None:
        try:
            actor_handle = (
                record.actor if record.actor.startswith("@") else "@system:local"
            )
            actor = Principal(
                handle=actor_handle, public_bytes=b"\x00" * 32
            )
            intent_name = _classify_intent(record)
            envelope = ActionEnvelope(
                actor=actor,
                capability=CapabilityToken.unscoped_test_token(subject=actor),
                classification=Classification.INTERNAL,
                context_fragment_id=f"config://{record.key}",
                provenance_parent=ProvenanceRef.synthetic("config_change"),
                federation_origin=None,
                intent=ActionIntent(intent_name),
                resource=ResourceRef.parse(f"config://{record.key}"),
                deadline=None,
                dedup_key=f"config:{record.key}:{record.changed_at.isoformat()}",
            )
            decide(envelope, ctx)
        except Exception:
            pass  # best-effort

    get_registry().add_listener(_emit_receipt)
    return lambda: get_registry().remove_listener(_emit_receipt)


__all__ = [
    "ChangeRecord",
    "ConfigRegistry",
    "Field",
    "Lock",
    "LockedConfigError",
    "ObserverCallback",
    "SchemaError",
    "Watcher",
    "default_config_dir",
    "get_registry",
    "get_value",
    "load_config_file",
    "load_jsonschema",
    "lock",
    "lock_status",
    "make_watcher",
    "observe",
    "register_schema",
    "register_schema_from_jsonschema",
    "start_watching",
    "stop_watching",
    "unlock",
    "write_value",
]
