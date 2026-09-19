# ADR-090 — PRESS LaTeX→PDF generation provider (Tectonic)

**Status:** Proposed — 2026-06-02
**Owner:** @ben
**Related:** ADR-056 (skill-as-function — PRESS skills), ADR-059 (connector-first — PRESS providers), ADR-063 (generated artifacts). Supersedes nothing.

## Context

PRESS generates publishable artifacts from **markdown** sources: `pandoc-docx`
(→ .docx) and `pandoc-pdf` (markdown → HTML → WeasyPrint → branded .pdf). Both
*re-typeset* content through a markdown pipeline.

That pipeline cannot handle **author-owned LaTeX projects** — a `.tex` document
that owns its own document class and style (e.g. a journal template such as
TMLR, NeurIPS, or an arXiv preprint). For those, the document must be compiled
*as written*: its `\documentclass`, local `.sty`/`.bst`, `\input`s,
`\includegraphics`, and bibliography passes preserved exactly. Routing such a
document through markdown would discard the very formatting the venue requires.

This gap is concrete: a portfolio paper destined for TMLR was authored in a
non-LaTeX tool, and there was no way inside our tooling to compile the required
LaTeX template to a verified PDF — the compile had to be outsourced to Overleaf,
so "does it compile?" was unanswerable from the platform.

## Decision

Add a third generation provider, **`latex-pdf`**, that compiles a LaTeX project
to PDF with **Tectonic**, registered like the others
(`PublisherFactory.register("generation", "latex-pdf", LatexPdfProvider)`) and
mapped from the user-facing formats `latex`/`tex` in `PublisherEngine`. A `.tex`
source with no explicit `--format` **auto-routes** to it (markdown
pre-processing — Mermaid, frontmatter — is a no-op for `.tex` and skipped).

### Engine: Tectonic (not full TeX Live, not pandoc `--pdf-engine`)

- **Tectonic** is a single self-contained binary that auto-fetches exactly the
  packages a document needs (no multi-GB TeX Live image), caches them for
  reproducible offline re-runs, and runs the XeTeX engine. It is the right fit
  for a containerized agent: small, deterministic, zero manual package
  management.
- **Full TeX Live + latexmk** was rejected: it bloats the runtime image by GBs
  for marginal package coverage Tectonic fetches on demand.
- **pandoc `--pdf-engine`** was rejected: it still drives *markdown → LaTeX*, so
  it cannot compile an author's existing `.tex` project — the exact case this
  provider exists for.

### Behavior
- Input: a `.tex` entry file, or a directory containing `main.tex`.
- Compiles in a scratch dir (no `.aux`/`.log` litter in the source tree); copies
  only the PDF out. `cwd` is the entry's directory so local `.sty`/`.bst` and
  relative `\includegraphics` resolve.
- Non-zero exit → `RuntimeError` with the tectonic stderr tail (the real LaTeX
  error, not just "exit 1"). Non-fatal warnings (overfull boxes, undefined refs)
  ride along in `GenerationResult.warnings`.
- `supports_watermark()` is `False` — the document owns its styling; we do not
  edit the author's LaTeX to inject a stamp.

## Consequences
- **+** PRESS can now compile-verify author LaTeX (journals, arXiv) end-to-end —
  "does it compile?" is answerable in-platform.
- **+** Caught real defects on first use (unrepresentable Unicode glyphs, a
  longtable conversion artifact) that a markdown pipeline would have hidden.
- **−** Adds a system dependency (the `tectonic` binary) to the runtime image and
  dev setup. Tests skip when it is absent; the provider raises an actionable
  install message at call time.
- **−** Tectonic's default fonts (Computer Modern) don't carry every Unicode
  glyph; documents using literal `→`/`α` should encode them as LaTeX commands
  (`$\rightarrow$`, `$\alpha$`) or load a Unicode font via `fontspec`. This is a
  document concern, not a provider one.
