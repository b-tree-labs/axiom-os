# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi analytics`` — thin CLI wrapper over the analytics skill (ADR-056)."""
from __future__ import annotations

import argparse
import json
import sys

from .skills.compute import run


def _brand_cli() -> str:
    """The command the operator actually typed.

    Hardcoding "axi" tells a consumer distribution's operator to run a command
    that does not exist on their machine.
    """
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog=f"{_brand_cli()} analytics", description="Deterministic series math."
    )
    ap.add_argument("op", help="operation (sum, mean, std, linregress, integral, ...)")
    ap.add_argument("--series", required=True, help="inline list / JSON / CSV text, or @file")
    ap.add_argument("--column", default=None, help="column name/index for tabular/list-of-dicts")
    ap.add_argument("--params", default="{}", help="JSON object of op params")
    ap.add_argument("--source", default=None, help="provenance: originating tool/series")
    args = ap.parse_args(argv)
    series = args.series
    if series.startswith("@"):
        with open(series[1:]) as fh:
            series = fh.read()
    res = run({
        "op": args.op, "series": series, "column": args.column,
        "params": json.loads(args.params), "source": args.source,
    })
    print(json.dumps(res.value if res.ok else {"errors": res.errors}, indent=2))
    return res.exit_code


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
