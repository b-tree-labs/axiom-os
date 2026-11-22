# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The database image is a product file, and every extension it carries can be created (#1598).

A node's database image installed the TimescaleDB engine pinned and its loader
unpinned. The loader owns ``timescaledb.control``, so apt brought a newer one
and the control file named a default version the image had no install script
for. An existing database hid it; ``CREATE EXTENSION`` on a new one, and a
disaster-recovery restore, failed.

These tests read the files. The build itself is the other half: it starts a
throwaway cluster and creates every required extension in an empty database,
so an image that cannot do that is never tagged (CI builds it in
``node-chart.yml``).
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
IMAGE = ROOT / "infra" / "images" / "axiom-db"
CHART = ROOT / "infra" / "charts" / "axiom-node"
WORKFLOW = ROOT / ".github" / "workflows" / "node-chart.yml"

#: What the platform creates: the engine and toolkit for time series, and the
#: vector type the retrieval store uses.
REQUIRED = {"timescaledb", "timescaledb_toolkit", "vector"}


def _dockerfile() -> str:
    return (IMAGE / "Dockerfile").read_text()


def _args() -> dict[str, str]:
    return dict(re.findall(r"^ARG (\w+)=(\S+)$", _dockerfile(), re.M))


def test_engine_and_loader_are_pinned_to_one_release():
    text = _dockerfile()
    assert re.search(r"timescaledb-2-postgresql-16=\$\{TIMESCALEDB_VERSION\}", text)
    assert re.search(r"timescaledb-2-loader-postgresql-16=\$\{TIMESCALEDB_VERSION\}", text), (
        "the loader owns timescaledb.control: unpinned, apt brings a newer one and "
        "CREATE EXTENSION fails on a new database"
    )
    assert re.search(r"timescaledb-toolkit-postgresql-16=\S*\$\{TOOLKIT_VERSION\}", text)
    assert {"TIMESCALEDB_VERSION", "TOOLKIT_VERSION"} <= set(_args())


def test_no_timescale_package_is_left_to_float():
    installs = " ".join(re.findall(r"apt-get install[^&]*", _dockerfile()))
    packages = [t for t in installs.split() if t.startswith("timescaledb")]
    assert packages, "no timescale package found in the install step"
    floating = [p for p in packages if "=" not in p]
    assert not floating, f"unpinned: {floating}"


def test_the_build_creates_every_required_extension_in_an_empty_database():
    text = _dockerfile()
    script = (IMAGE / "check-extensions.sh").read_text()
    assert "check-extensions.sh" in text and re.search(r"RUN .*check-extensions", text)
    for ext in REQUIRED:
        assert re.search(rf"\b{ext}\b", script), f"{ext} is not created by the build check"
    assert "CREATE EXTENSION" in script
    assert "pg_available_extension_versions" in script


def test_the_image_tag_changes_when_its_files_do():
    tag, digest = (IMAGE / "TAG").read_text().splitlines()[-1].split()
    now = hashlib.sha256(
        b"".join((IMAGE / f).read_bytes() for f in ("Dockerfile", "check-extensions.sh"))
    ).hexdigest()
    assert now == digest, (
        "the database image's files changed: give it a new tag in "
        f"infra/images/axiom-db/TAG ('<tag> {now}')"
    )
    assert re.fullmatch(r"\d+\.\d+\.\d+", tag)


def test_the_image_is_built_on_pull_requests_and_published_from_main():
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    builds = [
        j
        for j in jobs.values()
        if any("infra/images/axiom-db" in (s.get("run") or "") for s in j.get("steps", []))
    ]
    assert len(builds) >= 2, "one job builds it on every change, one publishes it from main"


def test_the_chart_default_runs_the_same_timescaledb_release():
    """The chart named 2.17.2 while the node ran 2.28. Same release on both."""
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    image = values["roles"]["medallion"]["image"]
    version = _args()["TIMESCALEDB_VERSION"]
    assert re.search(rf":{re.escape(version)}-pg16$", image), (image, version)
