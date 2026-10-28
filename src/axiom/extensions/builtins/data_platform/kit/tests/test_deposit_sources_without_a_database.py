# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A kit whose bronze is mirrored FILES, not ingested rows.

A tenant whose producer deposits whole files into a shared folder declares the
source ``kind = "deposit"`` and writes a deposit normalizer beside the row
one. Everything here runs with no database: the manifest parse, the loader's
refusal when the normalizer is missing, and ``kit-check``'s static findings
over a sample deposit.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from axiom.extensions.builtins.data_platform import kit
from axiom.extensions.builtins.data_platform.kit.check import normalizer_findings
from axiom.extensions.builtins.data_platform.kit.project import KitError
from axiom.extensions.builtins.data_platform.kit.runner import load_normalizers

DEPOSIT_SOURCE = """
[sources.loop-share]
kind = "deposit"
schema_ref = "example/archive-v1"
"""

NORMALIZER = '''
"""A deposit normalizer: one mirrored file to canonical rows."""

def archive(deposit):
    if deposit.suffix != ".csv":
        return
    for line in deposit.path.read_text(encoding="utf-8").splitlines():
        ts, value = line.split(",")
        yield {"feed": "rig", "channel": "temperature.tank", "ts": ts,
               "value": float(value), "unit": "degC", "source_class": "measured"}


def register_deposits(registry):
    registry.register("example/archive-v1", archive)
'''


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    kit.init(tmp_path, "rig-site")
    manifest = tmp_path / "data" / "kit.toml"
    manifest.write_text(manifest.read_text(encoding="utf-8") + DEPOSIT_SOURCE, encoding="utf-8")
    return tmp_path


def _write_sample_deposit(repo: Path, name: str, body: bytes) -> None:
    bronze = repo / "data" / "samples" / "bronze"
    sha = hashlib.sha256(body).hexdigest()
    content = bronze / "_content" / sha[:2] / sha
    content.parent.mkdir(parents=True, exist_ok=True)
    content.write_bytes(body)
    rec_dir = bronze / "loop-share" / "_records" / "2026-10-01"
    rec_dir.mkdir(parents=True, exist_ok=True)
    (rec_dir / "item-1.json").write_text(
        json.dumps(
            {
                "item_id": "item-1",
                "display_name": name,
                "content_sha256": sha,
                "disposition": "allow",
                "fetched_at": "2026-10-01T08:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )


def test_a_deposit_source_with_no_deposit_normalizer_is_refused_by_name(repo):
    with pytest.raises(KitError, match="example/archive-v1"):
        load_normalizers(kit.load(repo))


def test_a_source_kind_the_kit_does_not_know_is_refused(tmp_path):
    kit.init(tmp_path, "rig-site")
    manifest = tmp_path / "data" / "kit.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8")
        + '\n[sources.x]\nkind = "stream"\nschema_ref = "a/b"\n',
        encoding="utf-8",
    )
    with pytest.raises(KitError, match="stream"):
        kit.load(tmp_path)


def test_check_runs_the_sample_deposit_and_is_clean(repo):
    (repo / "data" / "conform" / "archive.py").write_text(NORMALIZER, encoding="utf-8")
    _write_sample_deposit(repo, "run.csv", b"2026-10-01T00:00:00+00:00,20.5")
    blocking, _notes = normalizer_findings(kit.load(repo))
    assert blocking == []


def test_check_names_the_deposit_whose_normalizer_raises(repo):
    (repo / "data" / "conform" / "archive.py").write_text(NORMALIZER, encoding="utf-8")
    _write_sample_deposit(repo, "bad.csv", b"not a reading")
    blocking, _notes = normalizer_findings(kit.load(repo))
    assert any("bad.csv" in b and "raised" in b for b in blocking)


def test_check_catches_a_naive_timestamp_in_a_deposit(repo):
    (repo / "data" / "conform" / "archive.py").write_text(NORMALIZER, encoding="utf-8")
    _write_sample_deposit(repo, "run.csv", b"2026-10-01T00:00:00,20.5")
    blocking, _notes = normalizer_findings(kit.load(repo))
    assert any("no timezone" in b for b in blocking)


UNITLESS = NORMALIZER.replace('"unit": "degC"', '"unit": None')
UNDECLARED_UNITS = """
[units.undeclared]
reason = "the logger does not record units; pending the instrument sheet"
channels = ["temperature.*"]
"""


def test_a_unitless_channel_fails_the_check_once_by_channel(repo):
    (repo / "data" / "conform" / "archive.py").write_text(UNITLESS, encoding="utf-8")
    _write_sample_deposit(
        repo, "run.csv", b"2026-10-01T00:00:00+00:00,1\n2026-10-01T00:00:01+00:00,2"
    )
    blocking, _notes = normalizer_findings(kit.load(repo))
    unit_lines = [b for b in blocking if "no unit" in b]
    assert len(unit_lines) == 1, "one finding per channel, never per row"
    assert unit_lines[0].startswith("temperature.tank: 2 row(s) carry no unit")
    assert "[units.undeclared]" in unit_lines[0], "the finding says how to declare it"


def test_declaring_the_unit_unknown_in_kit_toml_passes_with_its_reason(repo):
    (repo / "data" / "conform" / "archive.py").write_text(UNITLESS, encoding="utf-8")
    _write_sample_deposit(repo, "run.csv", b"2026-10-01T00:00:00+00:00,1")
    manifest = repo / "data" / "kit.toml"
    manifest.write_text(manifest.read_text(encoding="utf-8") + UNDECLARED_UNITS, encoding="utf-8")
    blocking, notes = normalizer_findings(kit.load(repo))
    assert blocking == []
    assert any("knowingly undeclared" in n and "instrument sheet" in n for n in notes)


def test_a_pattern_that_matches_nothing_is_reported(repo):
    (repo / "data" / "conform" / "archive.py").write_text(NORMALIZER, encoding="utf-8")
    _write_sample_deposit(repo, "run.csv", b"2026-10-01T00:00:00+00:00,1")
    manifest = repo / "data" / "kit.toml"
    manifest.write_text(manifest.read_text(encoding="utf-8") + UNDECLARED_UNITS, encoding="utf-8")
    blocking, notes = normalizer_findings(kit.load(repo))
    assert blocking == []
    assert any("matched no unitless channel" in n for n in notes)


def test_an_undeclared_list_with_no_reason_is_refused(repo):
    manifest = repo / "data" / "kit.toml"
    manifest.write_text(
        manifest.read_text(encoding="utf-8") + '\n[units.undeclared]\nchannels = ["x"]\n',
        encoding="utf-8",
    )
    with pytest.raises(KitError, match="no `reason`"):
        kit.load(repo)
