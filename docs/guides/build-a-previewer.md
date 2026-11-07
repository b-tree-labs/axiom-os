# Build a previewer

A previewer decides how a file looks in the Library and in chat. You write a
small Python function that returns **data**, a representation such as a table,
a tree or a chart, and the client draws it with the platform's own components.
Your code never runs in the browser, which is what makes a contributed previewer
safe to install.

Decision record: [ADR-213](../adrs/adr-213-upload-is-a-composable-extraction-pipeline.md) D12.
Reference: `axiom.extensions.builtins.library.preview.contract`.

## 1. The contract

```python
def preview(ctx: PreviewContext) -> dict | None
```

`ctx` gives you the stored file (`ctx.path`, read-only), its `file_name`,
`size`, `sha256` and `mime`, and `ctx.extractions`: what every extractor found,
keyed by extractor name (each value is that extractor's result as JSON). Most
good previewers read the extraction, not the file: the work of parsing is
already done.

Return a representation, or `None` for "not mine after all" (the next previewer
is tried). Never raise on a malformed or hostile file; return `None`.

| `type` | Use it for | Required |
|---|---|---|
| `media` | images, PDF, video, audio | `media` (`image`/`pdf`/`video`/`audio`), `alt`, `src = "content"` |
| `text` | text and code | `text`, `language` (for highlighting), `truncated` |
| `table` | tabular data | `columns` (each with a `name`, optional `type` and `unit`), `rows` (at most 200), `row_count` |
| `tree` | nested structure | `nodes`: `{name, kind, detail?, children?, peek?}` |
| `listing` | archive contents | `entries`: `{path, size, kind}` |
| `chart` | anything with a series | `title`, `x` and `y` axes with `label` (and `unit`, `scale`), `series` with `name`, `points` as `[x, y]`, `kind` |
| `summary` | key facts | `fields`: `{label, value, unit?}`, optional `tables` |
| `composite` | several views | `parts`: `{title, preview}` |
| `html` | rich output nothing else fits | `html` (sanitize it with `preview.sanitize.clean_html`), `title` |

Colours are theme tokens (`var(--accent)`, `series-1`), never `#rrggbb`, so the
preview works in light and dark. A `chart` is drawn by the same component the
Charts page uses, with an "Open in Charts" action.

## 2. A minimal example

```python
# mypack/previewers.py
from axiom.extensions.builtins.library.preview.contract import PreviewContext


def widget_log(ctx: PreviewContext) -> dict | None:
    found = ctx.extractions.get("mypack.extract.widget_log")
    if not found:
        return None
    readings = found["tables"][0]
    return {
        "type": "composite",
        "parts": [
            {"title": "Summary", "preview": {"type": "summary", "fields": [
                {"label": "Run", "value": found["metadata"].get("run", "?")},
                {"label": "Readings", "value": readings["row_count"]},
            ]}},
            {"title": "Readings", "preview": {
                "type": "chart", "title": "Readings",
                "x": {"label": "Time", "scale": "time"},
                "y": {"label": "Value", "unit": "kPa"},
                "series": [{"name": "pressure", "unit": "kPa", "kind": "line",
                            "points": [[r[0], r[1]] for r in readings["rows"]]}],
            }},
        ],
    }
```

## 3. Register it

In your extension's `axiom-extension.toml` (a data kit: in `kit.toml`):

```toml
[[previewers]]
name     = "mypack.preview.widget_log"     # <scope>.preview.<name>
version  = "1.0.0"
summary  = "Widget logs: run summary and a pressure chart"
entry    = "mypack.previewers:widget_log"
produces = ["composite"]
priority = 50
[previewers.match]
extractors = ["mypack.extract.widget_log"]  # the extractor results it reads
extensions = [".wlog"]
[previewers.limits]
timeout_s = 5
max_bytes = 50_000_000
```

Contributed previewers are tried before the core ones (scope first), then by
`priority`, then by how specifically they match. `GET /api/v1/library/previewers`
lists what a node has.

## 4. Test it

Every previewer must pass the conformance suite: no network, bounded time and
memory, a valid and safe representation, accessible names, theme tokens, and an
injection fixture that comes back valid or declined.

```python
from axiom.extensions.builtins.library.preview.conformance import assert_conformant
from axiom.extensions.builtins.library.preview.contract import PreviewContext


def test_widget_log_previewer_is_conformant(tmp_path):
    fixture = tmp_path / "run.wlog"
    fixture.write_text("WIDGET-LOG v1\n...")
    ctx = PreviewContext(path=fixture, file_name="run.wlog", size=fixture.stat().st_size, sha256="0" * 64,
                         mime="text/plain", extractions={"mypack.extract.widget_log": {...}})
    assert_conformant(widget_log, MANIFEST, [ctx], tmp_dir=tmp_path)
```

Use small synthetic fixtures you wrote yourself. Then upload a file in a local
Library and open it: the preview should look right in both themes.

## 5. When nothing fits

If no representation fits, return `None` and the client shows the "can't
preview" state with the file's identity and what the extractors found. That is
a fine first step: add the extractor first, and the previewer when you know what
people want to see.
