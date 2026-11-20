#!/usr/bin/env python3
"""Fail early, in plain words, when the pinned package cannot read the config.

Runs after ``pip install axiom-os-lm==<axiom-version>``. The installed package,
not a reimplementation, parses the file, so a key the pin does not know is
caught here and not in the middle of a landing pass.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    path = Path(argv[1] if len(argv) > 1 else ".axiom/release-lander.toml")
    if not path.is_file():
        print(f"::error::RIVET config {path} is missing. Copy the example from actions/rivet-land.")
        return 1
    from axiom.extensions.builtins.release.lander.config import parse_config

    try:
        config = parse_config(path.read_text(encoding="utf-8"))
    except Exception as exc:  # the package raises its own refusal type
        print(f"::error::the pinned axiom-os-lm cannot read {path}: {exc}")
        return 1
    if not config.approvers:
        print("::warning::[raci] approvers is empty: --act refuses to run without one.")
    print(f"RIVET config {path} reads cleanly (human_paths_mode={config.human_paths_mode}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
