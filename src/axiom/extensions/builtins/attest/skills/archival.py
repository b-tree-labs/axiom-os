# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``attest.archival_tick``: the schedule's daily pass that archives and tombstones what each log's
declared rule says is due (ADR-290 D8). It erases only while the log store is over its declared budget
(``AXIOM_LOG_BUDGET_BYTES``), and only content past its rule and outside any hold."""

from __future__ import annotations

from typing import Any

from axiom.infra.skills import SkillResult

from .. import archival


def tick(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    acts = archival.run_all()
    # Then, only while the store is over its declared budget, erase what is past its rule.
    relief = archival.relieve_pressure()
    acts += relief["acts"]
    done = [a for a in acts if a["act"] != "refused"]
    refused = [a for a in acts if a["act"] == "refused"]
    return SkillResult(
        ok=True,
        value={
            "acts": [
                {k: v for k, v in a.items() if k != "record"}
                | ({"record_seq": a["record"]["seq"]} if "record" in a else {})
                for a in acts
            ],
            "pressure": relief["pressure"],
            "still_over": relief["still_over"],
        },
        actions_taken=[
            f"{a['act']} {a['logbook']} at {a['site_id']} (seal {a['seal_id']})" for a in done
        ],
        errors=[
            f"{a['logbook']} at {a['site_id']}: {a.get('step')} refused: {a['reason']}"
            for a in refused
        ]
        + (
            [
                f"{relief['pressure']}, and nothing more is past its declared rule: raise the budget or the disk"
            ]
            if relief["still_over"]
            else []
        ),
    )


__all__ = ["tick"]
