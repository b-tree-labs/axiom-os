# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Every chart of the demo gallery on one page, drawn by the platform renderer: a contact sheet.

    python scripts/charts_demo_sheet.py OUT_DIR [--tokens path/to/tokens.css]

Writes ``OUT_DIR/sheet.html``: one card per kind (``data-kind``), the SVG the Charts page draws,
through the same chart-data verb, from the sample data shipped with the platform. Pass the
appkit design tokens so the sheet themes as the page does (``data-theme`` light or dark). The
measured layout check (appkit ``tools/visual/chart-layout.mjs``) and screenshots read this page.
Reads no store and writes nothing else.
"""

from __future__ import annotations

import argparse
import html
import tempfile
from pathlib import Path



def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("out", type=Path)
    ap.add_argument(
        "--tokens", type=Path, default=None, help="appkit's tokens.css, to theme the sheet"
    )
    args = ap.parse_args()

    from axiom.extensions.builtins.charts import demo
    from axiom.extensions.builtins.charts.kinds_group import WIDE
    from axiom.extensions.builtins.charts import kinds_group as kg
    from axiom.extensions.builtins.scidisplay.chart_kinds import render_spec

    state = Path(tempfile.mkdtemp(prefix="charts-demo-sheet-"))
    group = demo.groups(state)[0]
    cards = []
    for g in group["graphs"]:
        wide = g["kind"] in WIDE
        drawn = render_spec(g["spec"], width=1390 if wide else 440, resolve=kg.resolver(state))
        cards.append(
            f'<figure class="card{" wide" if wide else ""}" data-kind="{html.escape(g["kind"])}">'
            f"<figcaption>{html.escape(g['kind'])}</figcaption>{drawn.svg}</figure>"
        )
    tokens = args.tokens.read_text(encoding="utf-8") if args.tokens else ""
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{html.escape(group["name"])}</title>
<style>{tokens}
body{{margin:0;padding:16px;background:var(--ground,#f1f0ec);color:var(--ink,#111);font-family:system-ui}}
.grid{{display:grid;grid-template-columns:repeat(3,460px);gap:16px;align-items:stretch}}
.card{{margin:0;padding:8px;border:1px solid var(--rule,#ddd);border-radius:8px;background:var(--panel,#fff)}}
.card.wide{{grid-column:1/-1}}
figcaption{{font:12px ui-monospace,monospace;color:var(--ink-3,#666);margin-bottom:4px}}
.card svg{{max-width:100%;height:auto;display:block}}
</style></head><body><h1 style="font-size:18px">{html.escape(group["name"])}</h1>
<div class="grid">{"".join(cards)}</div></body></html>"""
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "sheet.html").write_text(page, encoding="utf-8")
    print(args.out / "sheet.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
