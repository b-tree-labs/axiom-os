# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Tests for the Phase 4 install-state module.

``$AXIOM_HOME/state.json`` is the source of truth for axi-managed
installs. The six Phase 4 verbs read/write it through this module.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from axiom.cli.ext.install_state import (
    InstallRecord,
    drop_install,
    get_installed,
    list_installed,
    read_state,
    record_install,
    state_path,
    write_state,
)


@pytest.fixture
def axiom_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "axiom_home"
    home.mkdir()
    monkeypatch.setenv("AXIOM_HOME", str(home))
    monkeypatch.delenv("AXIOM_REGISTRY_URL", raising=False)
    return home


def _fake_record(name: str = "greeter", version: str = "0.1.0") -> InstallRecord:
    return InstallRecord(
        name=name,
        version=version,
        installed_at="2026-04-22T12:00:00Z",
        install_path=f"/tmp/fake/{name}-{version}",
        artifact_sha256="deadbeef",
        signature_sha256="cafebabe",
        registry_url="file:///tmp/registry",
    )


# ---------------------------------------------------------------------------
# Env resolution
# ---------------------------------------------------------------------------


def test_state_path_follows_axiom_home(axiom_home: Path) -> None:
    assert state_path() == axiom_home / "state.json"


def test_read_state_on_missing_file_returns_seed(axiom_home: Path) -> None:
    state = read_state()
    assert state == {"schema_version": 1, "installed": {}}


def test_list_installed_on_missing_state_returns_empty(axiom_home: Path) -> None:
    assert list_installed() == []


# ---------------------------------------------------------------------------
# Schema + atomic writes
# ---------------------------------------------------------------------------


def test_write_state_roundtrips(axiom_home: Path) -> None:
    payload = {
        "schema_version": 1,
        "installed": {
            "greeter": {
                "version": "0.1.0",
                "installed_at": "2026-04-22T12:00:00Z",
                "install_path": "/tmp/fake/greeter-0.1.0",
                "artifact_sha256": "deadbeef",
                "signature_sha256": "cafebabe",
                "registry_url": "file:///tmp/registry",
            }
        },
    }
    write_state(payload)
    assert read_state() == payload


def test_state_file_is_pinned_schema_v1(axiom_home: Path) -> None:
    record_install(_fake_record())
    data = json.loads((axiom_home / "state.json").read_text(encoding="utf-8"))
    assert data["schema_version"] == 1


def test_unsupported_schema_version_raises(axiom_home: Path) -> None:
    (axiom_home / "state.json").write_text(
        json.dumps({"schema_version": 99, "installed": {}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="schema_version"):
        read_state()


def test_corrupt_state_file_treated_as_empty(axiom_home: Path) -> None:
    (axiom_home / "state.json").write_text("this is not json", encoding="utf-8")
    assert read_state() == {"schema_version": 1, "installed": {}}


def test_atomic_write_never_leaves_tempfile(axiom_home: Path) -> None:
    write_state({"schema_version": 1, "installed": {}})
    leftovers = list(axiom_home.glob("*.tmp"))
    assert leftovers == []


def test_atomic_write_crash_preserves_previous(
    axiom_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If os.replace fails, the previous state file survives untouched."""
    import axiom.cli.ext.install_state as mod

    record_install(_fake_record("foo", "0.1.0"))
    before = read_state()

    real_replace = mod.os.replace

    def exploding(src, dst):  # noqa: ANN001
        if Path(dst).name == "state.json":
            raise OSError("simulated crash")
        return real_replace(src, dst)

    monkeypatch.setattr(mod.os, "replace", exploding)
    with pytest.raises(OSError, match="simulated"):
        record_install(_fake_record("bar", "0.2.0"))

    assert read_state() == before


# ---------------------------------------------------------------------------
# Record CRUD
# ---------------------------------------------------------------------------


def test_record_install_upserts(axiom_home: Path) -> None:
    record_install(_fake_record("greeter", "0.1.0"))
    got = get_installed("greeter")
    assert got is not None
    assert got.version == "0.1.0"

    # Upsert with a newer version: the old row is replaced, not appended.
    record_install(_fake_record("greeter", "0.2.0"))
    got = get_installed("greeter")
    assert got is not None
    assert got.version == "0.2.0"
    # Still exactly one row.
    all_records = list_installed()
    assert [r.name for r in all_records] == ["greeter"]


def test_get_installed_missing_returns_none(axiom_home: Path) -> None:
    assert get_installed("nope") is None


def test_list_installed_sorts_by_name(axiom_home: Path) -> None:
    record_install(_fake_record("zeta", "0.1.0"))
    record_install(_fake_record("alpha", "0.1.0"))
    record_install(_fake_record("mu", "0.1.0"))
    names = [r.name for r in list_installed()]
    assert names == ["alpha", "mu", "zeta"]


def test_drop_install_returns_dropped_record(axiom_home: Path) -> None:
    rec = _fake_record("greeter", "0.3.1")
    record_install(rec)
    dropped = drop_install("greeter")
    assert dropped is not None
    assert dropped.version == "0.3.1"
    assert get_installed("greeter") is None


def test_drop_install_missing_returns_none(axiom_home: Path) -> None:
    assert drop_install("nope") is None


def test_install_record_roundtrip_preserves_all_fields(axiom_home: Path) -> None:
    rec = InstallRecord(
        name="greeter",
        version="1.2.3",
        installed_at="2026-04-22T12:00:00Z",
        install_path="/tmp/fake/greeter-1.2.3",
        artifact_sha256="a" * 64,
        signature_sha256="b" * 64,
        registry_url="file:///tmp/registry",
    )
    record_install(rec)
    got = get_installed("greeter")
    assert got == rec
