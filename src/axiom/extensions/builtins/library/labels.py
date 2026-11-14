# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Export-control labels and retention classes on library items (MVP-22, W11).

The model is the platform's (``axiom.governance.classification`` for the label,
``axiom.governance.retention`` for the class and its floor); this module is
where the library applies it to an item:

- **Proposed, then confirmed.** The EC hook's verdict proposes a label. A
  person confirms it, at upload (the uploader states it) or afterwards
  (``label_item``). With the ``library_labels`` flag on, an item without a
  confirmed label is **not ingested** (MVP-16-A2), so it cannot be retrieved,
  and it cannot be exported (MVP-22-A1).
- **Enforced at retrieval.** A confirmed label is stamped on the item's chunks,
  so the retriever compares it with the person's clearance (MVP-06-A1); the
  library's own search applies the same comparison to the item.
- **Deletion honours the floor.** Moving an item to Deleted files keeps its
  bytes and is recorded; deleting it *forever* is refused before its class's
  floor, under a legal hold and under an export-control hold, and is recorded
  in the tamper-evident chain before anything is destroyed (MVP-22-A2).
- **Sample items** are labelled ``sample``, never indexed, never exported
  (MVP-25-A5).

The flag only gates the new refusals; the label and the class are recorded and
shown either way, so turning it on later finds every item already labelled.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

from axiom.governance import classification as gc
from axiom.governance import retention as gr

log = logging.getLogger(__name__)

FLAG = "library_labels"

#: ``index_status`` of an item waiting for a person to confirm its label.
AWAITING_LABEL = "awaiting-label"
#: ``index_status`` of a sample item, which is never indexed.
SAMPLE = "sample"

EC_HOLD = "an export-control hold"


class SampleRefused(PermissionError):
    """A sample item is shown, never retrieved or exported (MVP-25-A5)."""

    def __init__(self, act: str = "exported") -> None:
        super().__init__(f"a sample item is never {act}")


def enforced() -> bool:
    """Whether the label gates are on (the ``library_labels`` flag, ADR-210)."""
    from axiom.extensions.builtins.flags.service import env_override

    pinned = env_override(FLAG)
    if pinned is not None:
        return pinned == "on"
    try:
        from axiom.extensions.builtins.flags.service import FlagService

        return FlagService().is_enabled(FLAG, node=os.environ.get("AXIOM_NODE") or None)
    except Exception:  # noqa: BLE001 - an unreadable flag store leaves the gates where the default puts them
        return False


# ------------------------------------------------------------------ reading an item


def label_of(item: Any) -> gc.ExportControlLabel | None:
    return gc.ExportControlLabel.from_json(getattr(item, "ec_label", None))


def confirmed(item: Any) -> gc.ExportControlLabel | None:
    label = label_of(item)
    return label if label is not None and label.confirmed else None


def class_of(item: Any) -> gr.RetentionClass:
    raw = getattr(item, "retention_class", None)
    if not raw:
        return gr.DEFAULT_CLASS
    try:
        return gr.RetentionClass.from_str(raw)
    except ValueError:
        return gr.DEFAULT_CLASS


def holds_of(item: Any) -> list[str]:
    return [EC_HOLD] if (getattr(item, "ec", None) or {}).get("verdict") == "held" else []


def _aware(value: datetime | None) -> datetime:
    value = value or datetime.now(UTC)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def label_json(item: Any) -> dict[str, Any] | None:
    label = label_of(item)
    if label is None:
        return None
    return {**label.to_json(), "confirmed": label.confirmed}


def retention_json(item: Any) -> dict[str, Any]:
    cls = class_of(item)
    policy = gr.policy_for(getattr(item, "site", None))
    until = gr.deletable_after(cls, _aware(getattr(item, "created_at", None)), policy=policy)
    return {
        "class": cls.value,
        "floor": gr.describe_floor(policy.floor(cls)),
        "deletable_from": until.isoformat() if until is not None else None,
        "holds": holds_of(item),
    }


# ------------------------------------------------------------------ writing an item


def clean_options(ec_label: Any = None, retention_class: Any = None, sample: Any = None) -> dict[str, Any]:
    """Validate what an upload says about its label, class and sample-ness; refuse nonsense up front."""
    out: dict[str, Any] = {}
    if ec_label not in (None, ""):
        gc.Classification.from_str(str(ec_label))  # ValueError names the candidates
        out["ec_label"] = str(ec_label).strip().lower()
    if retention_class not in (None, ""):
        out["retention_class"] = gr.RetentionClass.from_str(str(retention_class)).value
    if sample not in (None, "", False, "false", "0"):
        out["sample"] = True
    return out


def apply_upload_options(item: Any, caller: Any, opts: dict[str, Any]) -> None:
    """At ``stored``: the uploader's stated label is a confirmation, by them."""
    if opts.get("ec_label"):
        item.ec_label = gc.confirmed_label(opts["ec_label"], by=caller.principal, basis="stated at upload").to_json()
    item.retention_class = opts.get("retention_class") or gr.DEFAULT_CLASS.value
    item.sample = bool(opts.get("sample"))


def propose(item: Any, verdict: str) -> None:
    """After the EC hook: propose a label from its verdict unless a person already confirmed one."""
    if confirmed(item) is not None:
        return
    item.ec_label = gc.propose_label(verdict).to_json()


def index_refusal(item: Any) -> tuple[str, str] | None:
    """``(status, reason)`` when this item must not reach a retrieval corpus, else ``None``."""
    if getattr(item, "sample", False):
        return SAMPLE, "a sample item is never retrieved"
    if enforced() and confirmed(item) is None:
        proposed = label_of(item)
        hint = f" (proposed: {proposed.classification.value})" if proposed is not None else ""
        return AWAITING_LABEL, f"no confirmed export-control label{hint}; confirm one to make it searchable"
    return None


def stamp(item: Any, store: Any) -> None:
    """Put the confirmed label on the item's chunks, where the retriever reads it."""
    label = confirmed(item)
    if label is None or store is None or not getattr(item, "source_path", ""):
        return
    setter = getattr(store, "set_document_classification", None)
    if setter is None:
        return
    setter(item.source_path, label.classification.value, corpus=item.corpus)


def may_retrieve(clearance: gc.Classification, item: Any) -> bool:
    """Whether a person with ``clearance`` may get this item back from a search."""
    if getattr(item, "sample", False):
        return False
    label = confirmed(item)
    if label is None:
        return not enforced()
    return gc.permits(clearance, label)


def check_export(clearance: gc.Classification, item: Any) -> None:
    """Refuse exporting (downloading) an item that is a sample, unlabelled or above clearance."""
    if getattr(item, "sample", False):
        raise SampleRefused("exported")
    label = confirmed(item)
    if label is None:
        if enforced():
            raise gc.Unlabelled("exported", getattr(item, "title", ""))
        return
    if not gc.permits(clearance, label):
        raise gc.AboveClearance(label.classification, clearance)


__all__ = [
    "AWAITING_LABEL",
    "EC_HOLD",
    "FLAG",
    "SAMPLE",
    "SampleRefused",
    "apply_upload_options",
    "check_export",
    "class_of",
    "clean_options",
    "confirmed",
    "enforced",
    "holds_of",
    "index_refusal",
    "label_json",
    "label_of",
    "may_retrieve",
    "propose",
    "retention_json",
    "stamp",
]
