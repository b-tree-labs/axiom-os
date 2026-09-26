# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Per-kind lifecycle scaffolding templates for `neut ext init --template <kind>`
(ADR-005 rung 1). Each kind writes a flat, locally-runnable skeleton: one pure
function, colocated tests (an example + a prove-it-can-fire guard), a README, and
a manifest carrying [extension].kind. Reconciled from the neutron `ext new` fork
into Axiom's template registry (option a). Bodies use the literal token __NAME__."""
from __future__ import annotations

import re
from pathlib import Path


# ---- per-kind main module ---------------------------------------------------
_CONFORM_MAIN = '''# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""__NAME__ — a bronze->silver normalizer (ADR-023). Fill in the mapping.

One pure function of one dict: the platform owns the walk, the upsert, the schema.
Dry-run a bronze row through it with `conform_try`; run it end-to-end against
`neut dev up`. Do NOT set site/schema_ref/row_hash (conform_rows fills those)."""
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
            "stream": row.get("stream", "__NAME__"),
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
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from normalizer import __NAME__  # noqa: E402

def _rec(values):
    return {"schema_ref": "__NAME__/v1", "row_hash": "h0",
            "row": {"stream": "__NAME__", "ts": "2026-01-01T00:00:00Z", "values": values}}

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
    # deliver each alert via HERALD (axi notifications) here; wired at promotion
    return len(alerts)
'''

_ALERT_TEST = '''# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from monitor import check  # noqa: E402

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
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from job import compute  # noqa: E402

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

Yields (stream, record) samples the DAQ core lands in bronze; run against
`neut dev up` locally before promotion."""
from __future__ import annotations
from collections.abc import Iterable
from typing import Any

STREAM = "__NAME__"

def read(source: Iterable[str]) -> Iterable[tuple[str, dict[str, Any]]]:
    for line in source:
        line = line.strip()
        if not line:
            continue
        yield (STREAM, {"schema_ref": "__NAME__/v1", "row": {"stream": STREAM, "values": {"raw_len": float(len(line))}}})
'''

_INGEST_TEST = '''# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from provider import read  # noqa: E402

def test_blank_lines_skipped():
    assert list(read(["", "  "])) == []

def test_can_fire_a_planted_line_is_emitted():
    recs = list(read(["hello"]))
    assert len(recs) == 1 and recs[0][0] == "__NAME__", \\
        "provider yielded nothing for a real line — a check that cannot fire is the bug"
'''

_README = '''# __NAME__ — a `__KIND__` extension (scaffolded by `neut ext new`)

One pure function to fill in, colocated tests (an example + a prove-it-can-fire
guard). This is ADR-005 rung 1.

## The loop
1. `neut dev up` — a local synthetic bronze->silver->gold medallion (no node, no VPN).
2. Edit `__MAIN__` — fill in your `__KIND__` logic.
3. Dry-run: `__DRYRUN__`.
4. `pytest tests/` — the example test + the can-fire guard must stay green (and the
   guard must go RED if you gut the function — that is the point).
5. Promote up the ladder (ADR-005): local integration -> staging -> production ->
   prod-validation, through the gates.

See the RECIPE for this kind: __RECIPE__
'''

_MANIFEST = '''# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
[extension]
name = "__NAME__"
kind = "__KIND__"
version = "0.1.0"
description = "__NAME__ — a __KIND__ extension (scaffolded)"
license = "Apache-2.0"
owner = "ut-austin"
aeos_version = "0.1.0"
'''

_KINDS: dict[str, dict[str, str]] = {
    "conform":   {"main": "normalizer.py", "main_body": _CONFORM_MAIN, "test_body": _CONFORM_TEST,
                  "dryrun": "conform_try (paste a bronze row)", "recipe": "the conformance RECIPE.md (data_platform/conformance/RECIPE.md)"},
    "alerting":  {"main": "monitor.py", "main_body": _ALERT_MAIN, "test_body": _ALERT_TEST,
                  "dryrun": "check(now=past) replay", "recipe": "the monitor-authoring guide (rod-sentinel RECIPE)"},
    "analytics": {"main": "job.py", "main_body": _ANALYTICS_MAIN, "test_body": _ANALYTICS_TEST,
                  "dryrun": "run compute() against `neut dev up`", "recipe": "ADR-005 (analytics jobs)"},
    "ingestion": {"main": "provider.py", "main_body": _INGEST_MAIN, "test_body": _INGEST_TEST,
                  "dryrun": "read() a sample file, then `neut dev up` to conform", "recipe": "the console/Zoc ingest worked example"},
}

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,40}$")

def list_kinds() -> list[str]:
    return sorted(_KINDS)


def _sub(text: str, *, name: str, kind: str, spec: dict) -> str:
    return (text.replace("__NAME__", name).replace("__KIND__", kind)
                .replace("__MAIN__", spec["main"]).replace("__DRYRUN__", spec["dryrun"])
                .replace("__RECIPE__", spec["recipe"]))


def make_create(kind: str):
    """Return a Template.create(ext_dir, *, name, owner, license, description) for `kind`."""
    spec = _KINDS[kind]

    def create(ext_dir, *, name: str, owner: str = "", license: str = "Apache-2.0",
               description: str = "") -> None:
        root = Path(ext_dir)
        root.mkdir(parents=True, exist_ok=False)
        (root / spec["main"]).write_text(_sub(spec["main_body"], name=name, kind=kind, spec=spec), encoding="utf-8")
        tests = root / "tests"
        tests.mkdir()
        (tests / "__init__.py").write_text("", encoding="utf-8")
        (tests / f"test_{name}.py").write_text(_sub(spec["test_body"], name=name, kind=kind, spec=spec), encoding="utf-8")
        (root / "README.md").write_text(_sub(_README, name=name, kind=kind, spec=spec), encoding="utf-8")
        (root / "axiom-extension.toml").write_text(_sub(_MANIFEST, name=name, kind=kind, spec=spec), encoding="utf-8")

    return create
