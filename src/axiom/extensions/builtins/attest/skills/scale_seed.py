# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``attest.scale_seed``: ``<cli> log seed --scale N``, the scale fixture (LS-1, #1780). Dev only.

Writes N fictional entries across the twenty fixture logs at one site and the matching Timeline
rows (``attest.scale_fixture``), deterministic from ``--seed``. Refuses a production or staging
node, a bound node and any database that is not a lane's own or a test run's. ``--declare`` also
writes the fixture logs as a project-local extension (``.axi/extensions/scale_fixture``, ignored
by git) so a dev node started in this checkout lists them. CLI only: no agent seeds a database.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillResult

from .. import scale_fixture
from ._common import resolve_site


def run(params: dict[str, Any], ctx: Any = None) -> SkillResult:
    try:
        n = int(params.get("scale") or 0)
    except (TypeError, ValueError):
        n = 0
    if n < 1:
        return SkillResult(ok=False, errors=["give the number of entries: --scale N (N at least 1)"])
    site, err = resolve_site({"site": str(params.get("site") or "").strip() or None})
    if err:
        return SkillResult(ok=False, errors=[err])
    declare = Path.cwd() / ".axi" / "extensions" if params.get("declare") else None
    try:
        out = scale_fixture.seed(n, site=site, seed_value=int(params.get("seed") or 0),
                                 journal=not params.get("no_journal"), declare_in=declare)  # fmt: skip
    except scale_fixture.ScaleFixtureRefused as exc:
        return SkillResult(ok=False, errors=[str(exc)])
    said = [f"seeded {n:,} fictional entries across {len(out['logbooks'])} logs at {site} "
            f"in {out['database']} (seed {out['seed']}; {out['seals_per_log']} sealed periods per log)"]  # fmt: skip
    if "journal" in out:
        said.append(f"seeded {out['journal']['rows']:,} Timeline rows")
    if "declared" in out:
        said.append(f"declared the logs in {out['declared']} (restart the dev node to list them)")
    return SkillResult(ok=True, value=out, actions_taken=said)
