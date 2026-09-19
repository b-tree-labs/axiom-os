# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Local small-model advisor contributor (opt-in).

After a turn that used tools or ended in an error, asks the LOCAL small
model, the same Ollama endpoint the router's sensitivity classifier uses
(``routing.ollama_base`` / ``routing.ollama_model``), for one short next
step, and OFFERS it through the advisor hook. It never reaches a cloud
provider and never auto-starts anything: if Ollama is not already
listening, the answer is ``None``.

Off by default. ``chat.advisor.slm`` must be true before any session makes
a model call it did not opt into.
"""

from __future__ import annotations

import hashlib
import json
import logging
import urllib.error
import urllib.request
from typing import Any

from .advisor import Advice, AdviceContext

log = logging.getLogger(__name__)

SLM_SETTING = "chat.advisor.slm"
SLM_TIMEOUT_S = 0.3
MAX_SLM_CHARS = 120

_DEFAULT_BASE = "http://localhost:11434"
_DEFAULT_MODEL = "llama3.2:1b"

_SYSTEM = (
    "You are a terse assistant embedded in a command-line chat. Given the last "
    "command, its outcome and the tools it used, suggest ONE short next step for "
    "the operator, at most 100 characters. Reply with the suggestion only: no "
    "preamble, no quotes, no markdown."
)


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _settings() -> Any:
    from axiom.extensions.builtins.settings.store import SettingsStore

    return SettingsStore()


def slm_enabled() -> bool:
    """``chat.advisor.slm``; a settings hiccup reads as OFF."""
    try:
        return _truthy(_settings().get(SLM_SETTING, False))
    except Exception:
        return False


def set_slm_enabled(flag: bool) -> None:
    _settings().set(SLM_SETTING, bool(flag), scope="global")


def _ollama_target() -> tuple[str, str]:
    try:
        store = _settings()
        base = str(store.get("routing.ollama_base", _DEFAULT_BASE) or _DEFAULT_BASE)
        model = str(store.get("routing.ollama_model", _DEFAULT_MODEL) or _DEFAULT_MODEL)
    except Exception:
        base, model = _DEFAULT_BASE, _DEFAULT_MODEL
    return base.rstrip("/"), model


def _local_complete(prompt: str, *, system: str, timeout_s: float) -> str | None:
    """One short completion from the local Ollama endpoint, or ``None``.

    Straight ``urllib`` against ``/api/generate`` with a hard timeout, the
    way ``axiom.llm.routing_health`` probes it. No auto-start, no retry.
    """
    base, model = _ollama_target()
    payload = json.dumps(
        {
            "model": model,
            "system": system,
            "prompt": prompt[:2000],
            "stream": False,
            "options": {"num_predict": 40, "temperature": 0.2},
        }
    ).encode()
    req = urllib.request.Request(
        f"{base}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read())
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        OSError,
        ValueError,
    ) as exc:
        log.debug("local SLM unavailable: %s", exc)
        return None
    text = data.get("response") if isinstance(data, dict) else None
    return text if isinstance(text, str) and text.strip() else None


def _key(ctx: AdviceContext) -> str:
    digest = hashlib.sha1(f"{ctx.command}\x00{ctx.outcome}".encode()).hexdigest()
    return f"slm:{digest[:16]}"


def local_slm_advice(ctx: AdviceContext) -> Advice | None:
    """Contributor: one <=120-char next step from the local small model.

    Only after a turn that used tools or ended in an error; only when
    ``chat.advisor.slm`` is on; only via the local Ollama path.
    """
    if not slm_enabled():
        return None
    if ctx.outcome != "error" and not ctx.tool_names:
        return None
    prompt = (
        f"Command: {ctx.command}\n"
        f"Outcome: {ctx.outcome}\n"
        f"Tools used: {', '.join(ctx.tool_names) or 'none'}\n"
        f"Elapsed: {ctx.elapsed_ms} ms"
    )
    text = _local_complete(prompt, system=_SYSTEM, timeout_s=SLM_TIMEOUT_S)
    if not text:
        return None
    text = " ".join(text.split()).strip("\"'` ")[:MAX_SLM_CHARS]
    if not text:
        return None
    return Advice(text=text, source="local_slm", key=_key(ctx))
