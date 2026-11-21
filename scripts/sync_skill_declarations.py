#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Write each built-in skill's declarations into its manifest table (AEOS 0.2 section 4.11.3).

The ``SkillSpec`` in code is the source of truth; the manifest ``kind = "skill"``
table is its static projection, which ``axi ext lint`` reads without importing
anything. This regenerates the projection in place, preserving every other line
of the manifest, and adds a table for a registered skill that has none.

    python scripts/sync_skill_declarations.py vault chat      # rewrite those manifests
    python scripts/sync_skill_declarations.py --check vault   # exit 1 if any is stale
    python scripts/sync_skill_declarations.py --claimers      # every built-in claiming B or C
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from axiom.infra import skill_chain_lint as scl  # noqa: E402
from axiom.infra.skills_emit import _declaration_lines, _module_entry_for  # noqa: E402

_DECL = re.compile(r"^(risk|side_effects|idempotent|requires|produces|next_steps|recipes)\s*=")


def _qualified(name: str, ext: str) -> str:
    return name if "." in name else f"{ext}.{name}"


def rewrite(text: str, ext: str, specs: dict) -> str:
    lines = text.split("\n")
    heads = [i for i, ln in enumerate(lines) if ln.strip() == "[[extension.provides]]"]
    seen: set[str] = set()
    out: list[str] = []
    cursor = 0
    for h in heads:
        end = next((j for j in range(h + 1, len(lines)) if lines[j].startswith("[")), len(lines))
        block = lines[h:end]
        if not any(re.match(r'^kind\s*=\s*"skill"', ln) for ln in block):
            continue
        m = next((re.match(r'^name\s*=\s*"([^"]+)"', ln) for ln in block if re.match(r'^name\s*=', ln)), None)
        q = _qualified(m.group(1), ext) if m else ""
        if q not in specs:
            continue
        seen.add(q)
        kept = [ln for ln in block if not _DECL.match(ln)]
        last = max(i for i, ln in enumerate(kept) if ln.strip() and not ln.lstrip().startswith("#"))
        kept[last + 1 : last + 1] = _declaration_lines(specs[q])
        out.extend(lines[cursor:h])
        out.extend(kept)
        cursor = end
    out.extend(lines[cursor:])
    result = "\n".join(out)
    for q, spec in sorted(specs.items()):
        if q in seen:
            continue
        leaf = q.partition(".")[2] or q
        desc = (spec.description or "").replace('"', "'")
        block = [
            "",
            "[[extension.provides]]",
            'kind = "skill"',
            f'name = "{q}"',
            f'entry = "{_module_entry_for(spec)}"',
            f'path = "skills/{leaf}"',
            f'description = "{desc}"',
            *_declaration_lines(spec),
        ]
        result = result.rstrip("\n") + "\n" + "\n".join(block) + "\n"
    return result


def main(argv: list[str]) -> int:
    check = "--check" in argv
    registry, _unbound, _bad = scl.load_registry()
    exts = [a for a in argv if not a.startswith("--")]
    if "--claimers" in argv:
        exts += [e for e, lv in scl.claimed_levels().items() if lv != "A"]
    stale = 0
    for ext in sorted(set(exts)):
        specs = {q: registry.spec(q) for q in registry.list() if scl._location(registry, q)[0] == ext}  # noqa: SLF001
        specs = {q: s for q, s in specs.items() if s is not None}
        path = scl.builtins_root() / ext / "axiom-extension.toml"
        old = path.read_text(encoding="utf-8")
        new = rewrite(old, ext, specs)
        if new != old:
            stale += 1
            if not check:
                path.write_text(new, encoding="utf-8")
            print(("stale: " if check else "wrote: ") + str(path.relative_to(ROOT)))
    return 1 if (check and stale) else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
