# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""``program.render`` — the data file becomes one static status page.

Stdlib only, one built-in template, one HTML file in the output directory.
The page is the data file made readable: every item id anchors a row
(``id="item-<id>"``), every link the file states is on the page, and
everything that came from the file is escaped on the way in. What the file
does not say, the page does not say.
"""

from __future__ import annotations

from html import escape
from pathlib import Path
from typing import Any

from axiom.infra.skills import SkillContext, SkillResult

from ..model import ProgramData, ProgramError, load_program
from .status import _item_view

PAGE_NAME = "status.html"

_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{
    --bg: #ffffff; --ink: #1a1d21; --muted: #5c6670;
    --line: #d9dee3; --chip: #eef1f4; --bar: #3b6ea5;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #15181b; --ink: #e8eaed; --muted: #9aa4ad;
      --line: #32383e; --chip: #23282d; --bar: #6b9fd4;
    }}
  }}
  body {{ background: var(--bg); color: var(--ink);
         font: 16px/1.5 system-ui, sans-serif;
         margin: 0 auto; max-width: 56rem; padding: 2rem 16px; }}
  h1 {{ font-size: 1.5rem; margin: 0 0 .25rem; }}
  h2 {{ font-size: 1.1rem; margin: 2rem 0 .5rem;
        border-bottom: 1px solid var(--line); padding-bottom: .25rem; }}
  p.meta {{ color: var(--muted); margin: 0 0 1rem; }}
  ul {{ list-style: none; margin: 0; padding: 0; }}
  li {{ border: 1px solid var(--line); border-radius: 6px;
        padding: .6rem .8rem; margin: 0 0 .5rem; }}
  .id {{ font-family: ui-monospace, monospace; color: var(--muted);
         font-size: .85rem; }}
  .chip {{ background: var(--chip); border-radius: 999px;
           font-size: .8rem; padding: .1rem .6rem; margin-left: .5rem; }}
  .who, .when {{ color: var(--muted); font-size: .9rem; }}
  .bar {{ background: var(--chip); border-radius: 4px; height: 6px;
          margin-top: .4rem; overflow: hidden; }}
  .bar > span {{ background: var(--bar); display: block; height: 100%; }}
  .links {{ font-size: .85rem; margin-top: .3rem; }}
  .links a, .links span.ref {{ margin-right: .75rem; }}
</style>
</head>
<body>
<h1>{title}</h1>
<p class="meta">{meta}</p>
{sections}
</body>
</html>
"""


def _dates_text(item: dict[str, Any]) -> str:
    dates = item["dates"]
    if dates["date"]:
        return escape(dates["date"])
    if dates["start"] or dates["end"]:
        return escape(f"{dates['start'] or '?'} → {dates['end'] or '?'}")
    return ""


def _links_html(item: dict[str, Any]) -> str:
    parts: list[str] = []
    for link in item["links"]:
        if link["kind"] == "url":
            href = escape(link["href"], quote=True)
            parts.append(f'<a href="{href}">{escape(link["field"])}</a>')
        elif link["kind"] == "tracker":
            ref = f"{link['host']}/{link['project']}#{link['ref']}"
            parts.append(f'<span class="ref">{escape(str(ref))}</span>')
    if not parts:
        return ""
    return '<div class="links">' + " ".join(parts) + "</div>"


def _item_html(item: dict[str, Any]) -> str:
    bits: list[str] = [f'<li id="item-{escape(str(item["id"]), quote=True)}">']
    bits.append(f'<span class="id">{escape(str(item["id"]))}</span> ')
    bits.append(f"<strong>{escape(str(item['label']))}</strong>")
    if item["status"] is not None:
        bits.append(f'<span class="chip">{escape(str(item["status"]))}</span>')
    line2: list[str] = []
    if item["owner"] is not None:
        line2.append(f'<span class="who">{escape(str(item["owner"]))}</span>')
    when = _dates_text(item)
    if when:
        line2.append(f'<span class="when">{when}</span>')
    if line2:
        bits.append("<div>" + " · ".join(line2) + "</div>")
    if item["pct"] is not None:
        bits.append(
            f'<div class="bar" title="{item["pct"]}%">'
            f'<span style="width:{int(item["pct"])}%"></span></div>'
        )
    bits.append(_links_html(item))
    bits.append("</li>")
    return "".join(bits)


def _sections(data: ProgramData) -> str:
    """One section per lane, in the file's lane order, plus one for items
    that state no lane. A lane with no items still gets its heading — an
    empty lane is a fact worth seeing."""
    items = [_item_view(entry, data, fmt="full") for entry in data.schedule]
    by_lane: dict[Any, list[str]] = {}
    for view in items:
        by_lane.setdefault(view.get("lane"), []).append(_item_html(view))

    out: list[str] = []
    for lane in data.lanes:
        rows = by_lane.pop(lane["id"], [])
        name = escape(str(lane.get("name", lane["id"])))
        out.append(f"<h2>{name}</h2>\n<ul>\n" + "\n".join(rows) + "\n</ul>")
    leftover = [row for _lane, rows in by_lane.items() for row in rows]
    if leftover:
        out.append("<h2>Unlaned</h2>\n<ul>\n" + "\n".join(leftover) + "\n</ul>")
    return "\n".join(out)


def run(params: dict[str, Any], ctx: SkillContext) -> SkillResult:
    data_path = Path(params.get("data") or ctx.state_dir / "program" / "data.json")
    out_dir = Path(params.get("out") or ctx.state_dir / "program" / "site")

    try:
        data = load_program(data_path)
    except ProgramError as exc:
        return SkillResult(ok=False, errors=[str(exc)])

    program = data.program
    meta_bits = [f"as of {program['as_of']}"] if program.get("as_of") else []
    if program.get("deputy"):
        meta_bits.append(f"deputy {program['deputy']}")
    program_links = _links_html(
        {
            "links": [
                {"kind": "url", "field": field, "href": value}
                for field, value in program.items()
                if isinstance(value, str) and value.startswith(("http://", "https://"))
            ]
        }
    )

    html = _PAGE.format(
        title=escape(str(program.get("name", program.get("id", "Program")))),
        meta=escape(" · ".join(meta_bits)) + program_links,
        sections=_sections(data),
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    page = out_dir / PAGE_NAME
    page.write_text(html, encoding="utf-8")

    return SkillResult(
        ok=True,
        value={"page": str(page), "items": len(data.schedule)},
        actions_taken=[f"wrote {page}"],
    )
