# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""KEEL's proposals, tracked by CLERK until a person approves, applies or declines them.

A data-growth finding is found every day until it is fixed. Alerting on it every
day turns it into noise. So the first time KEEL proposes a fix it:

1. opens the plan on the platform's approval queue (``skill_chain.open_plan``) when
   the fix has a capability that applies it, so a person can approve exactly it;
2. files one CLERK follow-up (``program.followup_add``);
3. remembers the plan_hash in its own small SQLite table next to the size samples.

On later days the same plan_hash is already tracked: no new follow-up, no new
alert. When the person approves the plan, the next run (or ``node.data_growth_open``)
marks the follow-up approved (``program.followup_mark``). When the approved plan
is applied, KEEL closes the follow-up with what ran (``program.followup_done``):
that is CLERK's record of the change.

Everything goes through the registry; KEEL never imports CLERK's code.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

FOLLOWUP_ADD = "program.followup_add"
FOLLOWUP_MARK = "program.followup_mark"
FOLLOWUP_DONE = "program.followup_done"
#: How long a person has before CLERK's ladder starts reminding them.
FOLLOWUP_DUE = "168h"
TRACKED = ("open", "approved")
_COLS = ("plan_hash", "code", "relation", "message", "capability", "followup_id", "state")


class Ledger:
    """Proposals KEEL has opened, in a small JSON file under the node's state directory
    (written atomically, as the maintenance windows are)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @classmethod
    def under(cls, state_dir: Path | str) -> Ledger:
        return cls(Path(state_dir) / "node" / "data_growth_proposals.json")

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            doc = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}
        return doc if isinstance(doc, dict) else {}

    def _rows(self, keep: Callable[[dict[str, Any]], bool]) -> list[dict[str, Any]]:
        rows = [r for r in self._load().values() if keep(r)]
        rows.sort(key=lambda r: r.get("at", 0.0))
        return [{k: r.get(k) for k in _COLS} for r in rows]

    def get(self, plan_hash: str) -> dict[str, Any] | None:
        r = self._load().get(plan_hash)
        return {k: r.get(k) for k in _COLS} if r else None

    def put(self, **row: Any) -> None:
        from axiom.infra.state_integrity import atomic_write_text

        doc = self._load()
        doc[row["plan_hash"]] = {**{k: row.get(k) for k in _COLS}, "at": time.time()}
        atomic_write_text(self.path, json.dumps(doc, sort_keys=True, indent=1))

    def tracked(self) -> list[dict[str, Any]]:
        return self._rows(lambda r: r.get("state") in TRACKED)


def _call(ctx: Any, name: str, params: dict[str, Any]) -> dict[str, Any]:
    from .skills._handoff import handoff

    return handoff(ctx, name, params)


def _open_plan(ctx: Any, capability: str, params: dict[str, Any]) -> None:
    try:
        from axiom.infra.skill_chain import open_plan

        open_plan(ctx, capability, params)
    except Exception:  # noqa: BLE001 - the follow-up still tracks it; calling the capability opens it
        pass


def track(ctx: Any, ledger: Ledger, findings: list[dict[str, Any]]) -> dict[str, Any]:
    """File one CLERK follow-up per new proposal; return which findings are new."""
    new: list[str] = []
    tracked: list[str] = []
    failed: list[str] = []
    for f in findings:
        if f.get("severity") not in ("warning", "critical"):
            continue
        fix = f.get("fix") or {}
        h = fix.get("plan_hash")
        if not h:
            continue
        have = ledger.get(h)
        if have and have["state"] in TRACKED:
            tracked.append(h)
            continue
        cap = fix.get("capability")
        if cap and getattr(ctx, "state_dir", None):
            _open_plan(ctx, cap, fix.get("params") or {})
        how = f"approve plan {h} for {cap}" if cap else "decide on the proposed fix"
        rec = _call(ctx, FOLLOWUP_ADD, {
            "what": f"KEEL proposes a fix: {f['message']} ({how})"[:280],
            "due": FOLLOWUP_DUE,
            "evidence": [{"label": f"plan_hash {h}", "url": ""}],
        })
        if rec["status"] != "ok":
            failed.append(f"{h}: {rec['status']} {rec.get('detail', '')}".strip())
            new.append(h)  # not tracked, so it still alerts: nothing is lost
            continue
        ledger.put(plan_hash=h, code=f["code"], relation=f["table"], message=f["message"],
                   capability=cap, followup_id=str((rec.get("value") or {}).get("id") or ""),
                   state="open")
        new.append(h)
    return {"new": new, "already_tracked": tracked, "not_filed": failed}


def sync_approvals(ctx: Any, ledger: Ledger, find_plan: Any = None) -> list[str]:
    """Mark the follow-up of every tracked proposal whose plan a person approved."""
    if find_plan is None:
        from axiom.infra.skill_chain import find_plan
    marked: list[str] = []
    for p in ledger.tracked():
        if p["state"] != "open" or not p["capability"]:
            continue
        try:
            action = find_plan(ctx, p["capability"], p["plan_hash"])
        except Exception:  # noqa: BLE001 - an unreadable queue changes nothing
            continue
        status = getattr(getattr(action, "status", None), "value", getattr(action, "status", None))
        if status != "approved":
            continue
        if p["followup_id"]:
            _call(ctx, FOLLOWUP_MARK, {"id": p["followup_id"], "mark": "approved",
                                       "note": f"plan {p['plan_hash']} approved"})
        ledger.put(**{**p, "state": "approved"})
        marked.append(p["plan_hash"])
    return marked


def close(ctx: Any, ledger: Ledger, plan_hash: str, note: str) -> dict[str, Any]:
    """The plan ran: close its follow-up with what ran, for CLERK's record."""
    have = ledger.get(plan_hash)
    if not have:
        return {"status": "untracked"}
    rec = {"status": "ok"}
    if have["followup_id"]:
        rec = _call(ctx, FOLLOWUP_DONE, {
            "id": have["followup_id"], "note": note[:1000],
            "evidence": [{"label": f"applied plan_hash {plan_hash}", "url": ""}],
        })
    ledger.put(**{**have, "state": "applied"})
    return {"status": rec["status"], "followup_id": have["followup_id"]}


__all__ = ["FOLLOWUP_ADD", "FOLLOWUP_DONE", "FOLLOWUP_MARK", "Ledger", "close", "sync_approvals",
           "track"]
