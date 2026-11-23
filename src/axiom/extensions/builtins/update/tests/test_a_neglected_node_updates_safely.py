# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A node left alone for months still updates safely, whatever goes wrong on the way.

Real virtual environments and a real pip on wheels built here. The cases are
the ones an unattended node meets: power lost during the switch, the disk
filling, the package index gone, a release needing a newer Python, an update
landing while a pass is running, and an update nobody approved for weeks.
"""

from __future__ import annotations

import subprocess
import sys
import time as _time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from axiom.extensions.builtins.update import swap
from axiom.extensions.builtins.update.tests.test_a_node_updates_by_swapping_and_rolls_back import (
    _wheel,
)


@pytest.fixture(scope="module")
def wheels(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("wheels")
    _wheel(d, "1.0.0")
    _wheel(d, "1.0.1")
    _wheel(d, "1.1.0")
    _wheel(d, "2.0.0", requires_python=">=99")
    return d


CHECK = [["nodecheck"]]


def _install(root, v, wheels, **kw):
    return swap.apply_update(root, f"nodecheck=={v}", v, checks=CHECK, find_links=wheels, **kw)


# -- power lost during the switch ------------------------------------------------


def test_power_lost_after_the_switch_is_resolved_on_the_next_start(tmp_path, wheels):
    root = tmp_path / "node"
    _install(root, "1.0.0", wheels)
    # Everything an update does up to and including the switch, then the
    # process dies before the post-switch check: what a pulled plug leaves.
    swap._begin(root, "1.0.0", "1.1.0")
    _install_only(root, "1.1.0", wheels)
    swap._point(root, "1.1.0")
    assert swap.current_version(root) == "1.1.0"

    out = swap.recover(root, post_checks=[["sh", "-c", "exit 1"]])
    assert out is not None and out.status == "rolled_back"
    assert swap.current_version(root) == "1.0.0"
    assert swap.recover(root, post_checks=CHECK) is None  # nothing pending any more


def test_power_lost_after_the_switch_with_a_healthy_release_keeps_it(tmp_path, wheels):
    root = tmp_path / "node"
    _install(root, "1.0.0", wheels)
    swap._begin(root, "1.0.0", "1.1.0")
    _install_only(root, "1.1.0", wheels)
    swap._point(root, "1.1.0")
    out = swap.recover(root, post_checks=CHECK)
    assert out.status == "updated" and swap.current_version(root) == "1.1.0"


def test_power_lost_during_the_install_leaves_the_running_version(tmp_path, wheels):
    root = tmp_path / "node"
    _install(root, "1.0.0", wheels)
    swap._begin(root, "1.0.0", "1.1.0")
    (root / "venvs" / "1.1.0").mkdir(parents=True)  # half-built, never completed
    out = swap.recover(root, post_checks=CHECK)
    assert out.status == "rolled_back" and swap.current_version(root) == "1.0.0"
    assert not (root / "venvs" / "1.1.0").exists()


def test_a_hard_kill_mid_update_is_consistent_on_the_next_start(tmp_path, wheels):
    """A real process killed with SIGKILL partway through, then a fresh start."""
    root = tmp_path / "node"
    _install(root, "1.0.0", wheels)
    code = (
        "import sys; sys.path[:0] = sys.argv[3:];"
        "from axiom.extensions.builtins.update import swap;"
        "from pathlib import Path;"
        "swap.apply_update(Path(sys.argv[1]), 'nodecheck==1.1.0', '1.1.0', checks=[['sleep','30']], find_links=Path(sys.argv[2]))"
    )
    p = subprocess.Popen([sys.executable, "-c", code, str(root), str(wheels), *sys.path])
    deadline = _time.time() + 60
    while _time.time() < deadline and not (root / "venvs" / "1.1.0" / swap.COMPLETE).exists():
        _time.sleep(0.2)
    _time.sleep(0.5)  # now inside the pre-switch check
    p.kill()
    p.wait()
    assert swap.current_version(root) == "1.0.0"
    swap.recover(root, post_checks=CHECK)
    assert swap.current_version(root) == "1.0.0"
    # And the next attempt simply works.
    assert _install(root, "1.1.0", wheels).status == "updated"


# -- the disk ---------------------------------------------------------------------


def test_too_little_free_space_refuses_before_touching_anything(tmp_path, wheels):
    root = tmp_path / "node"
    _install(root, "1.0.0", wheels)
    out = _install(root, "1.1.0", wheels, min_free_bytes=10**18)
    assert out.status == "insufficient_space"
    assert "free" in out.detail and swap.current_version(root) == "1.0.0"
    assert not (root / "venvs" / "1.1.0").exists()


# -- the package index ------------------------------------------------------------


def test_an_unreachable_index_retries_and_alerts_after_days(tmp_path, wheels):
    root = tmp_path / "node"
    _install(root, "1.0.0", wheels)
    out = swap.apply_update(root, "nodecheck==1.1.0", "1.1.0", checks=CHECK, index_url="http://127.0.0.1:9/simple", timeout=60)
    assert out.status == "install_failed" and swap.current_version(root) == "1.0.0"
    first = datetime.fromisoformat(swap.last_outcome(root)["at"])
    assert swap.stuck_since(root, "1.1.0") == first
    assert not swap.needs_alert(root, "1.1.0", days=3, now=first + timedelta(days=2))
    assert swap.needs_alert(root, "1.1.0", days=3, now=first + timedelta(days=4))
    assert _install(root, "1.1.0", wheels).status == "updated"
    assert swap.stuck_since(root, "1.1.0") is None


# -- a newer Python ---------------------------------------------------------------


def test_a_release_needing_a_newer_python_is_refused_with_the_way_out(tmp_path, wheels):
    root = tmp_path / "node"
    _install(root, "1.0.0", wheels)
    out = _install(root, "2.0.0", wheels)
    assert out.status == "python_too_old"
    assert "needs Python >=99" in out.detail and "uv python install 99`" in out.detail
    assert swap.current_version(root) == "1.0.0"


@pytest.mark.parametrize(("spec", "hint"), [(">=3.13", "3.13"), (">3.12", "3.13"), ("==3.14.*", "3.14"), (">=99", "99")])
def test_the_hint_names_a_python_that_satisfies_the_release(spec, hint):
    assert swap._install_hint(spec) == hint


def test_a_newer_interpreter_can_be_named_for_the_new_version(tmp_path, wheels):
    root = tmp_path / "node"
    _install(root, "1.0.0", wheels)
    out = _install(root, "1.1.0", wheels, python=sys.executable)
    assert out.status == "updated"


# -- an update during a running pass ----------------------------------------------


def test_a_version_in_use_is_never_pruned(tmp_path, wheels):
    root = tmp_path / "node"
    _install(root, "1.0.0", wheels)
    with swap.in_use(root):
        assert _install(root, "1.0.1", wheels).status == "updated"
        assert _install(root, "1.1.0", wheels).status == "updated"
        # 1.0.0 is two versions back, but the running pass still holds it.
        assert (root / "venvs" / "1.0.0" / "bin" / "nodecheck").exists()
    swap.prune(root)
    assert not (root / "venvs" / "1.0.0").exists()


# -- the default policy ------------------------------------------------------------


def test_the_default_policy_takes_fixes_and_asks_about_features():
    assert swap.DEFAULT_POLICY == "auto-patch"
    assert swap.decide(swap.DEFAULT_POLICY, "1.17.0", "1.17.4", approved=False) == "apply"
    assert swap.decide(swap.DEFAULT_POLICY, "1.17.0", "1.18.0", approved=False) == "ask"


def test_a_long_neglected_node_takes_every_fix_it_missed_in_one_step(tmp_path, wheels):
    root = tmp_path / "node"
    _install(root, "1.0.0", wheels)
    assert swap.decide("auto-patch", "1.0.0", "1.0.1", approved=False) == "apply"
    assert _install(root, "1.0.1", wheels).status == "updated"


def _install_only(root: Path, version: str, wheels: Path) -> None:
    venv = root / "venvs" / version
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True, capture_output=True)
    subprocess.run([str(venv / "bin" / "python"), "-m", "pip", "install", "-q", "--no-index",
                    "--find-links", str(wheels), f"nodecheck=={version}"], check=True, capture_output=True)
    (venv / swap.COMPLETE).write_text("")


def test_a_hard_kill_after_the_switch_is_finished_on_the_next_start(tmp_path, wheels):
    """Killed while the post-switch check runs: the switch happened, the verdict did not."""
    root = tmp_path / "node"
    _install(root, "1.0.0", wheels)
    code = (
        "import sys; sys.path[:0] = sys.argv[3:];"
        "from axiom.extensions.builtins.update import swap;"
        "from pathlib import Path;"
        "swap.apply_update(Path(sys.argv[1]), 'nodecheck==1.1.0', '1.1.0', checks=[['nodecheck']],"
        " post_checks=[['sleep','30']], find_links=Path(sys.argv[2]))"
    )
    p = subprocess.Popen([sys.executable, "-c", code, str(root), str(wheels), *sys.path])
    deadline = _time.time() + 60
    while _time.time() < deadline and swap.current_version(root) != "1.1.0":
        _time.sleep(0.1)
    _time.sleep(0.3)
    p.kill()
    p.wait()
    assert swap.current_version(root) == "1.1.0" and (root / "pending.json").exists()
    out = swap.recover(root, post_checks=CHECK)
    assert out.status == "updated" and swap.current_version(root) == "1.1.0"
    assert not (root / "pending.json").exists()



def test_a_cutover_hands_over_before_current_moves(tmp_path, wheels):
    root = tmp_path / "node"
    _install(root, "1.0.0", wheels)
    seen = []

    def overlap(new_env):
        # the service is still on the old version while the new one is proven
        seen.append((swap.current_version(root), (new_env / swap.COMPLETE).exists()))
        return None

    assert _install(root, "1.1.0", wheels, cutover=overlap).status == "updated"
    assert seen == [("1.0.0", True)] and swap.current_version(root) == "1.1.0"


def test_a_cutover_that_rolls_back_leaves_the_old_version_running(tmp_path, wheels):
    root = tmp_path / "node"
    _install(root, "1.0.0", wheels)
    out = _install(root, "1.1.0", wheels, cutover=lambda env: "the new collector published 0 readings")
    assert out.status == "rolled_back_at_cutover" and "0 readings" in out.detail
    assert swap.current_version(root) == "1.0.0" and not (root / "venvs" / "1.1.0").exists()
