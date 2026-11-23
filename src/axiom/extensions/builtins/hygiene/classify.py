# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""LLM-assisted classification of hygiene candidates.

The deterministic storage policy decides the obvious cases (superseded
``dist``, stale logs, merged-PR caches). This module is for leftovers
whose name or shape is not in the policy. It asks the Axiom LLM gateway
(the same path RIVET's fixer uses; a site can point the config at
OpenRouter via ``AXIOM_TIDY_LLM_CONFIG``).

The API key never appears in logs, audit events, or returned values.
If no provider is configured, classification is skipped and the caller
keeps the deterministic result.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_log = logging.getLogger("axiom.hygiene.classify")

TASK = "hygiene_classify"
_SECRET_ENV = re.compile(r"(TOKEN|KEY|SECRET|PASSWORD|CREDENTIAL)", re.IGNORECASE)
_SECRET_ASSIGN = re.compile(
    r"(?i)((?:TOKEN|KEY|SECRET|PASSWORD|CREDENTIAL)[\w-]*)\s*[=:]\s*\S+"
)
_ALLOWED = frozenset({"autonomous", "approval", "keep"})

SYSTEM = (
    "You classify GitHub Actions / repo hygiene leftovers for TIDY. "
    "Reply with a JSON array of objects: "
    '{"id": "...", "decision": "autonomous"|"approval"|"keep", "rationale": "..."}. '
    "autonomous = low-risk and rebuildable, safe to delete under policy. "
    "approval = irreversible, unclear, or outside policy. "
    "keep = should not be deleted. Never mention credentials or secrets."
)


@dataclass(frozen=True)
class Classification:
    id: str
    decision: str
    rationale: str
    source: str  # "llm" | "skipped"


Completer = Callable[..., Any]


def _redact(text: str) -> str:
    """Strip secret-shaped assignments so a model error cannot echo a key."""
    text = _SECRET_ASSIGN.sub(r"\1=REDACTED", text)
    return _SECRET_ENV.sub("REDACTED", text)


def _parse(text: str) -> list[Classification]:
    raw = (text or "").strip()
    if not raw:
        return []
    try:
        start = raw.find("[")
        end = raw.rfind("]")
        payload = json.loads(raw[start : end + 1] if start != -1 and end != -1 else raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    out: list[Classification] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id") or "")
        decision = str(item.get("decision") or "").lower()
        rationale = _redact(str(item.get("rationale") or ""))[:300]
        if cid and decision in _ALLOWED:
            out.append(Classification(cid, decision, rationale, "llm"))
    return out


def default_completer() -> Completer | None:
    """Gateway from ``AXIOM_TIDY_LLM_CONFIG`` or the default config. None if unavailable."""
    try:
        from axiom.llm.gateway import Gateway

        config_dir = os.environ.get("AXIOM_TIDY_LLM_CONFIG") or os.environ.get("AXIOM_RIVET_LLM_CONFIG") or ""
        return Gateway(config_dir=Path(config_dir) if config_dir else None)
    except Exception as exc:  # noqa: BLE001
        _log.info("hygiene classifier has no gateway: %s", _redact(str(exc)))
        return None


def classify_candidates(
    candidates: list[dict[str, Any]],
    *,
    completer: Completer | None = None,
    complete: Callable[..., Any] | None = None,
) -> list[Classification]:
    """Classify leftovers. Empty candidates or a missing model returns []."""
    if not candidates:
        return []
    prompt = json.dumps(candidates, sort_keys=True, default=str)[:8000]
    if complete is None:
        gw = completer if completer is not None else default_completer()
        if gw is None:
            return []
        try:
            resp = gw.complete(prompt, system=SYSTEM, task=TASK, max_tokens=800)
        except Exception as exc:  # noqa: BLE001 — deterministic policy still stands
            _log.warning("hygiene classifier failed: %s", _redact(str(exc)))
            return []
        if not getattr(resp, "success", True):
            _log.info("hygiene classifier skipped: %s", _redact(str(getattr(resp, "error", "") or "unsuccessful")))
            return []
        text = getattr(resp, "text", "") or ""
    else:
        text = complete(prompt)
    return _parse(text)
