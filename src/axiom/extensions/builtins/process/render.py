# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Plain-text rendering of process skill results, shared by the CLI and chat.

Every rendering ends with real next-step commands taken from the result.
"""

from __future__ import annotations

from typing import Any

from axiom.infra.cli_format import Column, kv_line, table


def _fmt(v: Any, suffix: str = "") -> str:
    return "-" if v is None else f"{v:g}{suffix}" if isinstance(v, (int, float)) else str(v)


def render(verb: str, value: dict[str, Any]) -> str:
    lines: list[str] = []
    if verb == "ls":
        rows = value["processes"]
        if not rows:
            lines.append("No registered processes.")
        cells = []
        for r in rows:
            who = r["agent"] or r["extension"] or r["session"] or "-"
            flag = f"  !! {','.join(r['findings'])}" if r.get("findings") else ""
            cells.append(
                (
                    r["id"],
                    f"pid {r['pid']}",
                    r["kind"],
                    r["state"],
                    f"up {_fmt(r['elapsed_s'], 's')}",
                    f"cpu {_fmt(r['cpu_s'], 's')}",
                    who,
                    f"{r['purpose']}{flag}",
                )
            )
        if cells:
            lines.extend(table(cells, [Column()] * 7 + [Column(wrap=True)], indent=0))
        for u in value.get("unregistered", []):
            lines.append(f"unregistered pid {u['pid']}  carries the harness mark, no live record")
        if value.get("damaged"):
            lines.append(f"damaged records (skipped): {', '.join(value['damaged'])}")
    elif verb == "show":
        for k, v in value["process"].items():
            lines.append(kv_line(k, _fmt(v), key_width=16))
        for f in value["findings"]:
            lines.append(f"  finding {f['kind']}: {f['detail']}")
    elif verb == "stop":
        lines.append(
            f"{value['action']}: {value.get('plan') or value.get('reason') or ''}".rstrip(": ")
        )
    elif verb == "sweep":
        lines.append(
            f"mode={value['mode']} checked={value['checked']} "
            f"findings={len(value['findings'])} acted={len(value['actions'])}"
        )
        found = []
        for f in value["findings"]:
            tag = "auto" if f["auto"] else "kill" if f["killable"] else "info"
            found.append((f"[{tag}]", f["kind"], f["record_id"] or f["agent"] or "", f["detail"]))
        if found:
            lines.extend(table(found, [Column(), Column(), Column(), Column(wrap=True)]))
        for a in value["actions"]:
            lines.append(f"  action: {a.get('action')} {a.get('id') or a.get('agent')}")
        if value["damaged"]:
            lines.append(f"damaged records (skipped): {', '.join(value['damaged'])}")
    elif verb == "panic":
        lines.append(f"PANIC ({value['mode']})")
        for a in value["agents"]:
            lines.append(f"  agent {a['agent']}: {a['action']}")
        for p in value["processes"]:
            lines.append(f"  process {p.get('id')} pid {p.get('pid')}: {p['action']}")
        if value["fallback"]:
            lines.append(
                "  registry damaged: used the env-mark fallback scan "
                f"({', '.join(value['damaged'])})"
            )
        if not value["agents"] and not value["processes"]:
            lines.append("  nothing to stop")
    elif verb == "resume":
        lines.append(f"{value['agent']}: {value['action']}")
    if value.get("next"):
        lines.append("")
        lines.append("Next:")
        lines.extend(f"  {n}" for n in value["next"])
    return "\n".join(lines)
