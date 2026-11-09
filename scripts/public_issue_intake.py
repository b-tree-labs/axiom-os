# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Twice-daily intake of public axiom-os issues into the private tracker.

A public issue is copied only when it is a legitimate product report. A request
to add a backdoor, bypass a control, or carry out an attack is refused: the
private tracker gets a pointer and a reason, never a work item that implements
the request. Uncertain reports wait for a person.

This is not a vulnerability-disclosure channel. Those stay on SECURITY.md.
"""

from __future__ import annotations

import json
import re
import sys

# Requests to build an attack, not reports that a control misbehaved.
_ATTACK = re.compile(
    r"("
    r"\b(write|build|add|implement|ship)\b.{0,40}\b(backdoor|exploit|payload|shellcode|malware)\b"
    r"|\b(how (do i|to)|please)\b.{0,40}\b(hack|exploit|steal|exfiltrate)\b"
    r"|\bbypass\b.{0,40}\b(auth(entication|orization)?|rsicc|export control|safeguard|gate)\b"
    r"|\bdisable\b.{0,40}\b(auth(entication|orization)?|security check|safeguard)\b"
    r")",
    re.IGNORECASE | re.DOTALL,
)

_VULN_REPORT = re.compile(
    r"\b(cve-\d|vulnerability|security (issue|bug|report))\b",
    re.IGNORECASE,
)


def classify(title: str, body: str) -> dict:
    """Return action accept, refuse, or review. Refuse never includes a fix plan."""
    text = f"{title}\n{body or ''}"
    if _ATTACK.search(text):
        return {
            "action": "refuse",
            "reason": "asks for an attack or a control bypass, not a product fix",
        }
    if _VULN_REPORT.search(text) and "suggested fix" not in text.lower():
        return {
            "action": "review",
            "reason": "may be a vulnerability report; do not copy, point at SECURITY.md",
        }
    if len((body or "").strip()) < 40:
        return {"action": "review", "reason": "too thin to tell a real report from noise"}
    return {"action": "accept", "reason": "reads as a product bug or request"}


def main(argv: list[str] | None = None) -> int:
    raw = sys.stdin.read()
    try:
        item = json.loads(raw)
    except json.JSONDecodeError:
        print("expected a JSON object with title and body", file=sys.stderr)
        return 2
    verdict = classify(str(item.get("title") or ""), str(item.get("body") or ""))
    json.dump(verdict, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
