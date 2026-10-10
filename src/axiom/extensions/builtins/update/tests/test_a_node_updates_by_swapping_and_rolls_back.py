# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A node takes an update by building it beside the running version, and keeps the old one.

Real virtual environments and a real pip, installing wheels built here, so
nothing about the swap is imagined: a version that fails its checks before the
switch is never switched to, and one that fails after it is switched back.
"""

from __future__ import annotations

import base64
import hashlib
import os
import zipfile
from datetime import datetime, time
from pathlib import Path

import pytest

from axiom.extensions.builtins.update import swap


def _wheel(out: Path, version: str, exit_code: int = 0, requires_python: str | None = None) -> Path:
    """A minimal wheel for `nodecheck` whose console script prints its version."""
    name = "nodecheck"
    dist = f"{name}-{version}.dist-info"
    files = {
        f"{name}/__init__.py": (
            f"import sys\n__version__ = {version!r}\n"
            f"def main():\n    print(__version__)\n    sys.exit({exit_code})\n"
        ),
        f"{dist}/METADATA": (
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
            + (f"Requires-Python: {requires_python}\n" if requires_python else "")
        ),
        f"{dist}/WHEEL": "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        f"{dist}/entry_points.txt": f"[console_scripts]\n{name} = {name}:main\n",
    }
    path = out / f"{name}-{version}-py3-none-any.whl"
    record = []
    with zipfile.ZipFile(path, "w") as z:
        for arc, text in files.items():
            data = text.encode()
            z.writestr(arc, data)
            digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
            record.append(f"{arc},sha256={digest},{len(data)}")
        record.append(f"{dist}/RECORD,,")
        z.writestr(f"{dist}/RECORD", "\n".join(record) + "\n")
    return path


@pytest.fixture(scope="module")
def wheels(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("wheels")
    _wheel(d, "1.0.0")
    _wheel(d, "1.1.0")
    _wheel(d, "1.2.0", exit_code=1)  # installs fine, fails when it runs
    return d


def _check(root: Path) -> list[list[str]]:
    return [["nodecheck"]]


def test_an_update_switches_only_after_its_checks_pass(tmp_path, wheels):
    root = tmp_path / "node"
    first = swap.apply_update(root, "nodecheck==1.0.0", "1.0.0", checks=[["nodecheck"]], find_links=wheels)
    assert first.status == "updated" and swap.current_version(root) == "1.0.0"

    out = swap.apply_update(root, "nodecheck==1.1.0", "1.1.0", checks=[["nodecheck"]], find_links=wheels)
    assert out.status == "updated", out.detail
    assert (out.from_version, out.to_version) == ("1.0.0", "1.1.0")
    assert swap.current_version(root) == "1.1.0"
    assert (root / "venvs" / "1.0.0").is_dir()  # the previous version is kept


def test_a_version_that_fails_its_checks_is_never_switched_to(tmp_path, wheels):
    root = tmp_path / "node"
    swap.apply_update(root, "nodecheck==1.1.0", "1.1.0", checks=[["nodecheck"]], find_links=wheels)
    out = swap.apply_update(root, "nodecheck==1.2.0", "1.2.0", checks=[["nodecheck"]], find_links=wheels)
    assert out.status == "rejected_before_switch"
    assert swap.current_version(root) == "1.1.0"
    assert "nodecheck" in out.detail


def test_a_version_that_fails_after_the_switch_is_switched_back(tmp_path, wheels):
    root = tmp_path / "node"
    swap.apply_update(root, "nodecheck==1.1.0", "1.1.0", checks=[["nodecheck"]], find_links=wheels)
    # The pre-switch checks pass; the post-switch health check is what fails.
    out = swap.apply_update(
        root, "nodecheck==1.2.0", "1.2.0", checks=[["python", "-c", "pass"]],
        post_checks=[["nodecheck"]], find_links=wheels,
    )
    assert out.status == "rolled_back", out.detail
    assert swap.current_version(root) == "1.1.0"
    assert swap.last_outcome(root)["status"] == "rolled_back"


def test_the_switch_is_one_rename(tmp_path, wheels):
    root = tmp_path / "node"
    swap.apply_update(root, "nodecheck==1.0.0", "1.0.0", checks=[["nodecheck"]], find_links=wheels)
    link = root / "current"
    assert link.is_symlink() and os.readlink(link).endswith("1.0.0")


def test_a_failed_install_changes_nothing(tmp_path, wheels):
    root = tmp_path / "node"
    swap.apply_update(root, "nodecheck==1.0.0", "1.0.0", checks=[["nodecheck"]], find_links=wheels)
    out = swap.apply_update(root, "nodecheck==9.9.9", "9.9.9", checks=[["nodecheck"]], find_links=wheels)
    assert out.status == "install_failed"
    assert swap.current_version(root) == "1.0.0"
    assert not (root / "venvs" / "9.9.9").exists()


@pytest.mark.parametrize(
    ("policy", "current", "candidate", "approved", "expected"),
    [
        ("hold", "1.0.0", "1.0.1", True, "hold"),
        ("approve", "1.0.0", "1.1.0", False, "ask"),
        ("approve", "1.0.0", "1.1.0", True, "apply"),
        ("auto-patch", "1.0.0", "1.0.1", False, "apply"),
        ("auto-patch", "1.0.0", "1.1.0", False, "ask"),
        ("auto-patch", "1.0.0", "1.1.0", True, "apply"),
        ("approve", "1.1.0", "1.0.9", True, "hold"),  # never downgrade by policy
    ],
)
def test_the_policy_decides(policy, current, candidate, approved, expected):
    assert swap.decide(policy, current, candidate, approved=approved) == expected


def test_outside_the_window_an_approved_update_waits():
    window = (time(2, 0), time(4, 0))
    assert swap.decide("approve", "1.0.0", "1.1.0", approved=True, now=datetime(2026, 10, 8, 14, 0), window=window) == "wait"
    assert swap.decide("approve", "1.0.0", "1.1.0", approved=True, now=datetime(2026, 10, 8, 3, 0), window=window) == "apply"


# ADR-182 D5a: an update is recorded as a change before it acts, so downtime
# that overlaps it is attributed to us.


def test_an_update_is_recorded_as_a_change_with_its_outcome(tmp_path, wheels):
    from axiom.infra.change_intent import changes

    root = tmp_path / "node"
    swap.apply_update(root, "nodecheck==1.0.0", "1.0.0", checks=[["nodecheck"]], find_links=wheels)
    swap.apply_update(root, "nodecheck==1.2.0", "1.2.0", checks=[["nodecheck"]], find_links=wheels)
    recorded = [(c.kind, c.outcome) for c in changes()]
    assert recorded == [("update", "updated"), ("update", "rejected_before_switch")]
    assert "1.0.0" in changes()[1].subject and "1.2.0" in changes()[1].subject


def test_an_update_to_the_running_version_is_not_a_change(tmp_path, wheels):
    from axiom.infra.change_intent import changes

    root = tmp_path / "node"
    swap.apply_update(root, "nodecheck==1.0.0", "1.0.0", checks=[["nodecheck"]], find_links=wheels)
    swap.apply_update(root, "nodecheck==1.0.0", "1.0.0", checks=[["nodecheck"]], find_links=wheels)
    assert len(changes()) == 1
