# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Run a follow-up's CHECK at due time (ADR-196 D4).

A check is a registered, read-only skill plus arguments. It is re-verified as
read-only at fire time (a skill's declaration can change after the follow-up
was made), run headless with a time bound, and its result is reduced to an
allowlist before it can reach a journal or a message: scalar and short-string
fields only, keys that look like credentials dropped, credential-format values
replaced by a typed marker. A secret value cannot ride a check's output.
"""

from __future__ import annotations

import concurrent.futures
import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from axiom.infra.principal import PrincipalContext
from axiom.infra.skills import SkillContext

from . import followup_model as fm

CHECK_TIMEOUT_S = 20.0
MAX_FIELDS = 20
MAX_VALUE_CHARS = 200
MAX_ERRORS = 2
MAX_BYTES = 4096


def _reduce(value: Any) -> tuple[dict[str, Any], int]:
    """Allowlist a skill's result value. Returns (fields, redactions)."""
    out: dict[str, Any] = {}
    redacted = 0

    def put(key: str, v: Any) -> None:
        nonlocal redacted
        if len(out) >= MAX_FIELDS or fm.secretish_key(key.rsplit(".", 1)[-1]):
            return
        if isinstance(v, bool) or v is None or isinstance(v, (int, float)):
            out[key] = v
        elif isinstance(v, str):
            text, n = fm.scrub_text(" ".join(v.split())[:MAX_VALUE_CHARS])
            redacted += n
            out[key] = text

    if isinstance(value, Mapping):
        for k, v in value.items():
            if isinstance(v, Mapping):
                for k2, v2 in v.items():
                    if not isinstance(v2, (Mapping, list, tuple)):
                        put(f"{k}.{k2}", v2)
            else:
                put(str(k), v)
    elif value is not None:
        put("result", value)
    return out, redacted


def run_check(
    row: Mapping[str, Any], registry: Any, state_dir: Any, *, timeout_s: float = CHECK_TIMEOUT_S
) -> dict[str, Any]:
    """The check's outcome as an allowlisted, bounded dict. Never raises."""
    name = row.get("check_skill")
    if not name:
        return {"ran": False}
    spec = registry.spec(name)
    if spec is None:
        return {"ran": False, "skill": name, "error": "the check skill is not registered on this node"}
    if spec.effective_risk != "read" or spec.side_effects is not False:
        return {"ran": False, "skill": name, "error": "the check skill is no longer declared read-only; not run"}

    ctx = SkillContext(
        registry=registry, state_dir=state_dir, logger=logging.getLogger("axiom.program.followup.check"),
        user_prompt=None, principal=PrincipalContext(row["created_by"]), actor="@clerk:local", surface="cli",
    )
    args = dict(row.get("check_args") or {})
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="followup-check")
    started = datetime.now().timestamp()
    try:
        fut = pool.submit(registry.invoke, name, args, ctx)
        try:
            result = fut.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError:
            return {"ran": True, "skill": name, "ok": None, "timed_out": True,
                    "error": f"the check did not answer within {int(timeout_s)} s"}
        except Exception as exc:  # noqa: BLE001 - a failing check is a finding, not a crash
            return {"ran": True, "skill": name, "ok": False, "error": f"the check raised {type(exc).__name__}"}
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    fields, redacted = _reduce(result.value)
    errors = []
    for e in (result.errors or [])[:MAX_ERRORS]:
        text, n = fm.scrub_text(" ".join(str(e).split())[:MAX_VALUE_CHARS])
        redacted += n
        errors.append(text)
    out: dict[str, Any] = {"ran": True, "skill": name, "ok": bool(result.ok), "fields": fields,
                           "seconds": round(datetime.now().timestamp() - started, 2)}
    if errors:
        out["errors"] = errors
    if redacted:
        out["redactions"] = redacted
    return out


def render(check: Mapping[str, Any]) -> str:
    """The check, as the lines a reminder carries."""
    if not check.get("ran") and not check.get("error"):
        return ""
    head = f"Check ({check.get('skill')}): "
    if check.get("timed_out"):
        return head + "no answer. " + check.get("error", "")
    if check.get("error") and not check.get("fields"):
        return head + ("could not run. " if not check.get("ran") else "failed. ") + check["error"]
    lines = [head + ("passed" if check.get("ok") else "FAILED")]
    for k, v in (check.get("fields") or {}).items():
        lines.append(f"  {k}: {v}")
    for e in check.get("errors") or []:
        lines.append(f"  error: {e}")
    return "\n".join(lines)


__all__ = ["CHECK_TIMEOUT_S", "render", "run_check"]
