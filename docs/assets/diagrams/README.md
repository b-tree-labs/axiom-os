# Diagram standard (rendered PNG)

Architecture and flow diagrams are **rendered PNGs**, authored in Mermaid and
committed next to their source. This supersedes the LR / inline-Mermaid guidance
in `docs/specs/spec-mermaid-best-practices.md`.

Rules:
- Author `<name>.mmd` here as `flowchart TD` (vertical / portrait for 8.5x11).
- **Every node is styled** via `classDef` (palette below). Quote every label and
  wrap long text with `<br/>` so the box sizes to the text.
- Text stays legible on light and dark: white on the coloured fills below.
- Render locally, no network:
  `mmdc -i <name>.mmd -o <name>.png -p puppeteer.json -b white -s 2`
- Commit both `<name>.mmd` and `<name>.png`; reference the PNG from docs by
  relative path, e.g. `![Architecture](../../docs/assets/diagrams/<name>.png)`.

Palette (copy into each `.mmd`):

    classDef core    fill:#0e7490,color:#ffffff,stroke:#083344,stroke-width:2px;
    classDef vega    fill:#6d28d9,color:#ffffff,stroke:#2e1065,stroke-width:2px;
    classDef data    fill:#166534,color:#ffffff,stroke:#052e16,stroke-width:2px;
    classDef accent  fill:#7c2d12,color:#ffffff,stroke:#431407,stroke-width:2px;
    classDef neutral fill:#334155,color:#ffffff,stroke:#0f172a,stroke-width:2px;
