# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Make contributed HTML inert: an allowlist of tags and attributes, nothing that runs.

The ``html`` representation is rendered only in a sandboxed iframe with no
same-origin, so even an escape would land in an opaque origin with no script.
This is the second wall: script, event handlers, ``javascript:`` URLs, frames,
objects, forms and styles that can fetch are removed on the node before the
representation leaves it.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

ALLOWED_TAGS = {
    "a", "abbr", "b", "blockquote", "br", "caption", "code", "dd", "div", "dl", "dt", "em", "figcaption", "figure",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr", "i", "img", "kbd", "li", "mark", "ol", "p", "pre", "q", "s", "samp",
    "small", "span", "strong", "sub", "sup", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "u", "ul",
}
VOID = {"br", "hr", "img"}
DROP_WITH_CONTENT = {"script", "style", "iframe", "object", "embed", "noscript", "template", "svg", "math", "form", "select", "textarea"}
ALLOWED_ATTRS = {"href", "title", "alt", "colspan", "rowspan", "scope", "class", "lang", "dir", "src", "width", "height"}
_SAFE_URL = re.compile(r"^(https?:|#|data:image/(png|jpeg|gif|webp);base64,)", re.I)


class _Cleaner(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.dropping = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in DROP_WITH_CONTENT:
            self.dropping += 1
            return
        if self.dropping or tag not in ALLOWED_TAGS:
            return
        kept = []
        for name, value in attrs:
            if name not in ALLOWED_ATTRS or value is None:
                continue
            v = value.strip()
            if name in ("href", "src") and not _SAFE_URL.match(v):
                continue
            if name == "src" and not v.lower().startswith("data:image/"):
                continue  # no remote fetches from a preview
            kept.append(f' {name}="{html.escape(v, quote=True)}"')
        if tag == "a":
            kept.append(' rel="noopener noreferrer nofollow" target="_blank"')
        self.out.append(f"<{tag}{''.join(kept)}>")

    def handle_endtag(self, tag: str) -> None:
        if tag in DROP_WITH_CONTENT:
            self.dropping = max(0, self.dropping - 1)
            return
        if self.dropping or tag not in ALLOWED_TAGS or tag in VOID:
            return
        self.out.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self.dropping:
            self.out.append(html.escape(data, quote=False))


def clean_html(raw: str) -> str:
    c = _Cleaner()
    c.feed(raw or "")
    c.close()
    return "".join(c.out)


__all__ = ["ALLOWED_ATTRS", "ALLOWED_TAGS", "clean_html"]
