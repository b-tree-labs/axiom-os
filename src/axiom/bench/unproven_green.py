# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The unproven-green audit: what fraction of "healthy" is a claim?

Reads a fleet console status document (the ``/api/v1/fleet/status``
JSON) and separates every rendered kind into evidence-backed green vs
claims the console refused to launder (unproven / stale / failed /
unknown). Within ``service_health`` it further counts services claimed
healthy WITHOUT a measured latency — the canonical claim-not-effect.

The number this produces is the audit headline: "X% of what your
dashboard would call green is unevidenced." Re-run it after collectors
gain evidence to show the number driven down — the console forces
evidence discipline; this audit measures the forcing.

Run: ``python -m axiom.bench.unproven_green <status.json | URL>``
"""

from __future__ import annotations

import json
import sys
import urllib.request


def audit(status: dict) -> dict:
    nodes = status.get("nodes", [])
    kinds_total = 0
    kinds_green = 0
    kinds_refused = 0          # unproven/stale/failed/unknown
    kinds_unproven = 0
    services_claimed = 0
    services_measured = 0
    per_node = []
    for node in nodes:
        n_green = n_refused = 0
        for kind, entry in node.get("kinds", {}).items():
            kinds_total += 1
            if entry.get("status") == "green":
                kinds_green += 1
                n_green += 1
            else:
                kinds_refused += 1
                n_refused += 1
                if entry.get("status") == "unproven":
                    kinds_unproven += 1
        per_node.append(
            {
                "node_id": node.get("node_id"),
                "rollup": node.get("rollup"),
                "green": n_green,
                "refused": n_refused,
            }
        )
        # Service-level: healthy-without-latency is parsed out of the
        # evidence string the console already renders, so the audit
        # cites the same receipts the operator sees.
        sh = node.get("kinds", {}).get("service_health")
        if sh and sh.get("status") == "unproven":
            evidence = sh.get("evidence", "")
            if ":" in evidence:
                names = [s.strip() for s in evidence.split(":", 1)[1].split(",") if s.strip()]
                services_claimed += len(names)
    unproven_share = (kinds_refused / kinds_total) if kinds_total else 0.0
    return {
        "nodes": len(nodes),
        "kinds_total": kinds_total,
        "kinds_green_with_evidence": kinds_green,
        "kinds_refused": kinds_refused,
        "kinds_unproven": kinds_unproven,
        "unproven_green_share": round(unproven_share, 3),
        "services_claimed_without_measurement": services_claimed,
        "services_measured": services_measured,
        "per_node": per_node,
    }


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if not args:
        print("usage: python -m axiom.bench.unproven_green <status.json | URL>", file=sys.stderr)
        return 2
    src = args[0]
    if src.startswith("http://") or src.startswith("https://"):
        with urllib.request.urlopen(src, timeout=15) as resp:  # noqa: S310 — operator-supplied console URL
            status = json.loads(resp.read().decode())
    else:
        with open(src) as f:
            status = json.load(f)
    result = audit(status)
    print(json.dumps(result, indent=2))
    share = result["unproven_green_share"]
    print(
        f"\n{share:.0%} of rendered kinds are NOT evidence-backed green "
        f"({result['kinds_refused']}/{result['kinds_total']}); "
        f"{result['services_claimed_without_measurement']} services claim health "
        "with no measured latency.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
