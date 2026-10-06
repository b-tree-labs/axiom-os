# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Per-kind lifecycle scaffolding templates for `ext init --template <kind>`
(ADR-005 rung 1). Each kind delivers one pure function to fill in plus two tests
— an example and a prove-it-can-fire guard that must go red when you gut the
function. Bodies use the literal token __NAME__.

**Each kind writes the canonical compound layout and then overlays its rule.**
These used to write a flat four-file sketch, and the sketch failed five lint
rules — AEOS010 (no pyproject.toml), AEOS021 (a `kind` key the manifest schema
rejects), AEOS031 (no Python package), AEOS050 (no standard test) and AEOS070
(no mcp block). `ext init` prints `ext lint` as its own next step, so picking a
template and following the printed instruction produced five failures
attributable to nothing the author had done. A colleague hit it during
onboarding on 2026-10-01 while writing a monitor, which was the one
contribution we had asked for.

So the layout comes from `scaffold.create`, which is the one place that knows
what AEOS requires, and this module writes only what is specific to the kind.
Two scaffolders with their own idea of the layout is how one of them drifts out
of conformance without anybody editing it.

The lifecycle kind is no longer written into the manifest. `[extension].kind`
is not in the AEOS schema — the linter was right to reject it — so the kind
lives where it is true: the module name, the README, and the rule itself.

**Nothing written here may name a command that does not exist.** The README
opened by naming the command that wrote it, `neut ext new`, and sent the reader
to `neut dev up`; neither has ever existed in any of these CLIs. A generated
document is followed literally by whoever receives it, so
``tests/test_a_scaffold_passes_the_lint_that_follows_it.py`` checks every
command a scaffold writes down against the real verb registry — including the
ones in this docstring's own examples, which is why they are not in backticks
in the generated text."""
from __future__ import annotations

import re
from pathlib import Path


# ---- per-kind main module ---------------------------------------------------
_CONFORM_MAIN = '''# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""__NAME__ — a bronze->silver normalizer (ADR-023). Fill in the mapping.

One pure function of one dict: the platform owns the walk, the upsert, the schema.
Dry-run a bronze row through it with `__CLI__ data conform_try`. Do NOT set
site/schema_ref/row_hash — `conform_rows` fills those in."""
from __future__ import annotations
from collections.abc import Iterable
from typing import Any

SCHEMA_REF = "__NAME__/v1"
UNITS: dict[str, str] = {}  # channel -> unit; declare what your source emits

def __NAME__(record: dict[str, Any]) -> Iterable[dict[str, Any]]:
    row = record["row"]
    ts = row["ts"]
    out: list[dict[str, Any]] = []
    for channel, value in (row.get("values") or {}).items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue  # non-numeric channel: not a signal
        out.append({
            "feed": row.get("feed", "__NAME__"),
            "channel": str(channel),
            "ts": ts,
            "value": float(value),
            "unit": UNITS.get(str(channel)),
            "source_class": row.get("source_class", "measured"),
        })
    return out
'''

_CONFORM_TEST = '''# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from __NAME__.__MAINMOD__ import __NAME__  # noqa: E402

def _rec(values):
    return {"schema_ref": "__NAME__/v1", "row_hash": "h0",
            "row": {"feed": "__NAME__", "ts": "2026-01-01T00:00:00Z", "values": values}}

def test_emits_one_signal_per_numeric_channel():
    rows = list(__NAME__(_rec({"reading": 1.5, "label": "skip-me"})))
    assert len(rows) == 1
    assert rows[0]["channel"] == "reading" and rows[0]["value"] == 1.5

def test_can_fire_a_planted_reading_is_emitted():
    # prove-it-can-fire: gut the normalizer and THIS goes red.
    rows = list(__NAME__(_rec({"reading": 42.0})))
    assert any(r["value"] == 42.0 for r in rows), \\
        "normalizer emitted nothing for a real reading — a check that cannot fire is the bug"
'''

_ALERT_MAIN = '''# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""__NAME__ — a telemetry monitor (alerting). Fill in the rule.

`check(client)` is pure + unit-testable with a fake client (dry-run with
check(now=past)); `run(client)` delivers alerts via HERALD once promoted."""
from __future__ import annotations
from typing import Any

CHANNEL = "example_channel"
THRESHOLD = 100.0

def check(client: Any) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []
    value = client.latest(CHANNEL)
    if value is not None and value > THRESHOLD:
        alerts.append({"summary": f"{CHANNEL} = {value} exceeded {THRESHOLD}", "severity": "warn"})
    return alerts

def run(client: Any) -> int:
    alerts = check(client)
    # deliver each alert via HERALD (`__CLI__ notifications`) here; wired at promotion
    return len(alerts)
'''

_ALERT_TEST = '''# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from __NAME__.__MAINMOD__ import check  # noqa: E402

class _FakeClient:
    def __init__(self, v): self._v = v
    def latest(self, channel): return self._v

def test_no_alert_when_within_range():
    assert check(_FakeClient(1.0)) == []

def test_can_fire_a_planted_breach_alerts():
    alerts = check(_FakeClient(9999.0))
    assert alerts, "check() returned no alert for a planted breach — a check that cannot fire is the bug"
'''

_ANALYTICS_MAIN = '''# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""__NAME__ — a read->compute->emit analytics job. Fill in the computation."""
from __future__ import annotations
from typing import Any

CHANNEL = "example_channel"

def compute(client: Any) -> dict[str, Any]:
    series = list(client.series(CHANNEL))
    return {"n": len(series), "mean": (sum(series) / len(series)) if series else None}
'''

_ANALYTICS_TEST = '''# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from __NAME__.__MAINMOD__ import compute  # noqa: E402

class _FakeClient:
    def __init__(self, s): self._s = s
    def series(self, channel): return self._s

def test_empty_series_is_honest():
    assert compute(_FakeClient([]))["mean"] is None

def test_can_fire_computes_over_a_planted_series():
    out = compute(_FakeClient([2.0, 4.0, 6.0]))
    assert out["n"] == 3 and out["mean"] == 4.0, \\
        "compute returned nothing over a real series — a check that cannot fire is the bug"
'''

_INGEST_MAIN = '''# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""__NAME__ — a batch/push ingestion provider. Fill in the parse.

Yields (feed, record) samples the core lands in bronze. Exercise it with the
colocated tests before promotion."""
from __future__ import annotations
from collections.abc import Iterable
from typing import Any

FEED = "__NAME__"

def read(source: Iterable[str]) -> Iterable[tuple[str, dict[str, Any]]]:
    for line in source:
        line = line.strip()
        if not line:
            continue
        yield (FEED, {"schema_ref": "__NAME__/v1", "row": {"feed": FEED, "values": {"raw_len": float(len(line))}}})
'''

_INGEST_TEST = '''# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from __NAME__.__MAINMOD__ import read  # noqa: E402

def test_blank_lines_skipped():
    assert list(read(["", "  "])) == []

def test_can_fire_a_planted_line_is_emitted():
    recs = list(read(["hello"]))
    assert len(recs) == 1 and recs[0][0] == "__NAME__", \\
        "provider yielded nothing for a real line — a check that cannot fire is the bug"
'''

_README = '''# __NAME__ — a `__KIND__` extension

One pure function to fill in, with two tests beside it: an example, and a
prove-it-can-fire guard. This is ADR-005 rung 1.

## The loop
1. Edit `__NAME__/__MAIN__` — fill in your `__KIND__` logic.
2. `__CLI__ ext test` — the example and the can-fire guard must stay green, and the
   guard must go RED if you gut the function. That is what it is for.
3. Dry-run: __DRYRUN__.
4. `__CLI__ ext doctor` — lint, conformance, tests and environment in one pass.
5. Promote up the ladder (ADR-005): local integration -> staging -> production
   -> prod-validation, through the gates.

See the recipe for this kind: __RECIPE__
'''

_KINDS: dict[str, dict[str, str]] = {
    "conform":   {"main": "normalizer.py", "main_body": _CONFORM_MAIN, "test_body": _CONFORM_TEST,
                  "dryrun": "`__CLI__ data conform_try` with a bronze row pasted in", "recipe": "the conformance RECIPE.md (data_platform/conformance/RECIPE.md)"},
    "alerting":  {"main": "monitor.py", "main_body": _ALERT_MAIN, "test_body": _ALERT_TEST,
                  "dryrun": "call `check()` with a fake client, as the test does", "recipe": "the monitor-authoring guide (rod-sentinel RECIPE)"},
    "analytics": {"main": "job.py", "main_body": _ANALYTICS_MAIN, "test_body": _ANALYTICS_TEST,
                  "dryrun": "call `compute()` with a fake client, as the test does", "recipe": "ADR-005 (analytics jobs)"},
    "ingestion": {"main": "provider.py", "main_body": _INGEST_MAIN, "test_body": _INGEST_TEST,
                  "dryrun": "`read()` a sample file, then `__CLI__ data conform_try` on a row", "recipe": "the console/Zoc ingest worked example"},
}

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")

def list_kinds() -> list[str]:
    return sorted(_KINDS)


def _brand_cli() -> str:
    """The command the person who ran the scaffolder actually types.

    A consumer distribution renames the CLI, so scaffolded text that says `axi`
    unconditionally hands a consumer-layer user instructions for a command that is not
    theirs. This module already carried that lesson in `drift.py` and the ext
    verbs; the templates were written without it.

    Mixing the two names inside one document is the same defect seen from the
    reader's side: a page that opens with one command and continues with another
    reads as two products, and the reader cannot tell which one they installed.
    """
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


def _sub(text: str, *, name: str, kind: str, spec: dict) -> str:
    """Substitute the literal tokens. ``__MAINMOD__`` is the main module's
    importable name — the tests import the rule from inside the package now,
    rather than off a flat directory that AEOS does not allow. ``__CLI__`` is the
    branded command name, so one scaffold never names two CLIs."""
    return (text.replace("__NAME__", name).replace("__KIND__", kind)
                .replace("__CLI__", _brand_cli())
                .replace("__MAINMOD__", spec["main"].removesuffix(".py"))
                .replace("__MAIN__", spec["main"]).replace("__DRYRUN__", spec["dryrun"])
                .replace("__RECIPE__", spec["recipe"]))


def make_create(kind: str):
    """Return a Template.create(ext_dir, *, name, owner, license, description).

    The layout is the compound scaffold's, which is the one place that knows
    what AEOS requires; this adds the kind's rule and its tests on top. Two
    scaffolders each holding their own idea of the layout is how one of them
    drifts out of conformance without anybody editing it — which is exactly
    what had happened.
    """
    spec = _KINDS[kind]

    def create(ext_dir, *, name: str, owner: str = "", license: str = "Apache-2.0",
               description: str = "") -> None:
        from axiom.cli.ext.templates import scaffold

        root = Path(ext_dir)
        scaffold.create(
            root,
            name=name,
            owner=owner,
            license=license,
            description=description or f"{name} — a {kind} extension (scaffolded)",
        )

        # The rule lives in the package, so the tests can import it the way
        # anything else would and the published extension actually ships it.
        (root / name / spec["main"]).write_text(
            _sub(spec["main_body"], name=name, kind=kind, spec=spec), encoding="utf-8"
        )
        # Beside the standard conformance test rather than instead of it: the
        # standard test is what AEOS050 asks for and this is what the author
        # came for.
        (root / "tests" / "unit_tests" / f"test_{kind}_rule.py").write_text(
            _sub(spec["test_body"], name=name, kind=kind, spec=spec), encoding="utf-8"
        )
        (root / "README.md").write_text(
            _sub(_README, name=name, kind=kind, spec=spec), encoding="utf-8"
        )

    return create
