"""The offline bundle job can build its images, and the chart's package pin is not stale.

v0.70.46's publish run failed in `offline_bundle`: the wheelhouse image is exported with
`buildx build --output type=docker,dest=...`, which the runner's default `docker` driver
refuses. The same run showed the bundle building `axiom-wheelhouse:0.70.6` for a 0.70.46
release: the chart's pin had not moved since 2026-10-26, so an air-gapped node would have
installed a 40-release-old Axiom from a bundle named for the new one.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
PUBLISH = ROOT / ".github" / "workflows" / "publish.yml"
NODE_CHART_WORKFLOW = ROOT / ".github" / "workflows" / "node-chart.yml"
CHART = ROOT / "infra" / "charts" / "axiom-node"
BUNDLE = ROOT / "infra" / "bundle" / "offline-bundle.toml"

# The pin may trail the release by a few releases, because the release PR is cut before its
# own package exists on PyPI. Past this many patch releases it is stale and must move.
MAX_PIN_LAG = 10


def _patch(version: str) -> tuple[int, int, int]:
    major, minor, patch = (int(p) for p in version.split(".")[:3])
    return major, minor, patch


def _job(name: str) -> str:
    text = PUBLISH.read_text()
    match = re.search(rf"^  {name}:\n(.*?)(?=^  \S+:\n|\Z)", text, re.S | re.M)
    assert match, f"no {name} job in publish.yml"
    return match.group(1)


def test_the_offline_bundle_job_sets_up_a_builder_that_can_export_an_image():
    job = _job("offline_bundle")
    setup = job.find("docker/setup-buildx-action@")
    build = job.find("offline_bundle.py build")
    assert setup != -1, "the bundle job needs a docker-container buildx builder"
    assert setup < build, "the builder must exist before the bundle is built"
    assert re.search(r"docker/setup-buildx-action@[0-9a-f]{40}", job), "pin the action by full sha"
    assert "driver: docker-container" in job


def test_the_chart_pin_is_not_stale():
    release = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    pin = values["packages"]["version"]
    assert values["packages"]["pin"] == f"axiom-os-lm=={pin}"
    assert _patch(pin)[:2] == _patch(release)[:2], (
        f"chart pins {pin}, release is {release}: a different minor"
    )
    lag = _patch(release)[2] - _patch(pin)[2]
    assert 0 <= lag <= MAX_PIN_LAG, (
        f"infra/charts/axiom-node/values.yaml pins axiom-os-lm {pin}; the release is {release}. "
        f"Move packages.pin, packages.version, Chart.yaml appVersion, the wheelhouse ref in "
        f"infra/bundle/offline-bundle.toml and the pins in node-chart.yml to a published release."
    )


def test_every_place_that_names_the_pin_names_the_same_one():
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    pin = values["packages"]["version"]
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
    assert chart["appVersion"] == pin
    decl = tomllib.loads(BUNDLE.read_text())
    wheelhouse = next(i for i in decl["image"] if i.get("wheelhouse"))
    assert wheelhouse["ref"] == f"{decl['python']['wheelhouse_image']}:{pin}"
    assert set(re.findall(r"axiom-os-lm==([0-9.]+)", NODE_CHART_WORKFLOW.read_text())) == {pin}
