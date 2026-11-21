# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Pages of plain text as a PDF, in pure Python, with no dependency.

A printable form (a declared export form, a blank form for a binder) is already laid out as
text pages; this writes each page as one PDF page in a fixed-width font, so the command line,
an automation and the web print the same document from the same server-side code.

- **Deterministic.** No creation date, no random id: the same pages give the same bytes, so an
  evidence package stays byte-identical for one selection.
- **The standard Courier font**, which every PDF reader has; nothing is embedded. Its encoding
  is Latin-1, so a character outside it is printed as ``?`` and the page says so in its last
  line; the text rendering beside the PDF stays the exact one.
- **A4 portrait**, 9-point type, lines longer than the page wrapped, a page longer than the
  sheet continued on the next sheet with the same page label.
"""

from __future__ import annotations

from collections.abc import Iterable

#: A4 in points, the margin, the type size and the line height.
PAGE_W, PAGE_H = 595, 842
MARGIN = 42
SIZE = 9
LEADING = 11
#: Courier's advance is 0.6 em: how many characters fit across, and how many lines down.
COLUMNS = int((PAGE_W - 2 * MARGIN) / (SIZE * 0.6))
ROWS = int((PAGE_H - 2 * MARGIN) / LEADING)
LOSSY_NOTE = "Characters outside Latin-1 are shown as ?; the text rendering is exact."


def _wrap(line: str) -> list[str]:
    if not line:
        return [""]
    return [line[i : i + COLUMNS] for i in range(0, len(line), COLUMNS)]


def _sheets(page: str) -> list[list[str]]:
    lines = [w for raw in page.rstrip("\n").split("\n") for w in _wrap(raw.expandtabs(4))]
    return [lines[i : i + ROWS] for i in range(0, max(len(lines), 1), ROWS)] or [[]]


def _escape(line: str) -> tuple[bytes, bool]:
    lossy = False
    out = bytearray()
    for ch in line:
        code = ord(ch)
        if code > 255 or code < 32:
            lossy = True
            ch = "?"
        if ch in "()\\":
            out += b"\\"
        out += ch.encode("latin-1")
    return bytes(out), lossy


def text_to_pdf(pages: Iterable[str], *, title: str = "") -> bytes:
    """One PDF page (or more, for a long page) per text page, in order."""
    sheets = [sheet for page in pages for sheet in _sheets(page)] or [[]]
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # filled once the page tree exists
    tree = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>")
    kids = []
    for sheet in sheets:
        parts = [b"BT", f"/F1 {SIZE} Tf {LEADING} TL {MARGIN} {PAGE_H - MARGIN} Td".encode()]
        lossy = False
        for line in sheet:
            text, bad = _escape(line)
            lossy |= bad
            parts.append(b"(" + text + b") Tj T*")
        if lossy:
            parts.append(b"T* (" + _escape(LOSSY_NOTE)[0] + b") Tj")
        parts.append(b"ET")
        stream = b"\n".join(parts)
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        kids.append(add(
            f"<< /Type /Page /Parent {tree} 0 R /MediaBox [0 0 {PAGE_W} {PAGE_H}] "
            f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {content} 0 R >>".encode()
        ))  # fmt: skip
    objects[tree - 1] = (
        f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode()
    )
    objects[catalog - 1] = f"<< /Type /Catalog /Pages {tree} 0 R >>".encode()
    info = add(b"<< /Title (" + _escape(title)[0] + b") /Producer (axiom text_pdf) >>")

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for n, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R /Info {info} 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()  # fmt: skip
    return bytes(out)


__all__ = ["LOSSY_NOTE", "text_to_pdf"]
