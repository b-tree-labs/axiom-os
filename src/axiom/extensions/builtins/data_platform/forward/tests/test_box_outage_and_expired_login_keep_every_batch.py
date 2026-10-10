# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""Box unreachable, or the Box login expired: every batch waits, and the reason says what to do.

Contingencies C-03 (network to Box lost) and C-21 (Box authorization expires).
Both run the real rclone against Box's real API:

- an expired, unusable login makes Box's own token endpoint refuse it, and
  the forwarder says the login must be reconnected, not that Box is down;
- a dead network path (rclone through a proxy nothing listens on) is reported
  as unreachable, with nothing for the person to fix;
- in both, the cursor does not move, and after recovery every batch lands
  upstream exactly once.

Needs rclone and outbound HTTPS to api.box.com; skipped without them.
"""

from __future__ import annotations

import json
import shutil
import socket

import pytest

from axiom.extensions.builtins.data_platform.forward import (
    BoxDropTarget,
    Forwarder,
    IntakeTarget,
    LocalOutbox,
)
from axiom.extensions.builtins.data_platform.forward.tests.test_a_local_archive_forwards_upstream_exactly_once import (  # noqa: E501
    _collect_box,
    world,  # noqa: F401 - pytest fixture
)
from axiom.extensions.builtins.data_platform.sources.edge.puller import FileCursor


def _box_reachable() -> bool:
    try:
        socket.create_connection(("api.box.com", 443), timeout=5).close()
        return True
    except OSError:
        return False


pytestmark = [
    pytest.mark.skipif(
        shutil.which("rclone") is None, reason="rclone is needed for the drop target"
    ),
    pytest.mark.skipif(not _box_reachable(), reason="needs outbound HTTPS to api.box.com"),
]

EXPIRED = json.dumps(
    {
        "access_token": "expired",
        "token_type": "Bearer",
        "refresh_token": "revoked-refresh-token",
        "expiry": "2020-01-01T00:00:00Z",
    }
)
CURRENT = json.dumps(
    {
        "access_token": "not-a-real-token",
        "token_type": "Bearer",
        "refresh_token": "not-real",
        "expiry": "2099-01-01T00:00:00Z",
    }
)


def _forwarder(local, upstream, up_key, source, tmp, box):
    return Forwarder(
        LocalOutbox(local.root / "outbox", bronze_root_for=local.bronze_root),
        cursor=FileCursor(tmp / "cursor.json"),
        intake=IntakeTarget(upstream.url, token=up_key, probe_source=source),
        box=box,
        status_path=tmp / "status.json",
        up_after=1,
        down_after=1,
    )


def test_an_expired_box_login_says_reconnect_and_keeps_every_batch(world, tmp_path):  # noqa: F811
    local, upstream, local_key, up_key, _box_dir, source = world
    upstream.down()
    local.push(local_key, source, "run-0")
    saved: list[str] = []
    box = BoxDropTarget(
        "Partner Deposit/_rows", load_token=lambda: EXPIRED, save_token=saved.append
    )
    r = _forwarder(local, upstream, up_key, source, tmp_path, box).forward_once()
    assert r.current == "waiting" and r.after == 0
    assert "Box login expired or revoked" in r.reason and "reconnect" in r.reason.lower(), r.reason
    assert "rclone config" not in r.reason  # the person is told our step, not rclone's
    assert saved == []  # a refused login is not written back as if it were new


def test_box_unreachable_waits_says_so_and_lands_once_after(world, tmp_path):  # noqa: F811
    local, upstream, local_key, up_key, box_dir, source = world
    upstream.down()
    pushed = local.push(local_key, source, "run-0") + local.push(local_key, source, "run-1")
    dead = BoxDropTarget(
        "Partner Deposit/_rows",
        load_token=lambda: CURRENT,
        save_token=lambda _t: None,
        env={"HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9"},
    )
    r = _forwarder(local, upstream, up_key, source, tmp_path, dead).forward_once()
    assert r.current == "waiting" and r.after == 0
    assert "Box unreachable" in r.reason and "reconnect" not in r.reason.lower(), r.reason

    # Box comes back (here: a reachable drop folder): every batch lands once.
    back = _forwarder(local, upstream, up_key, source, tmp_path, BoxDropTarget(str(box_dir)))
    assert back.forward_once().sent_box == 2
    upstream.up()
    assert _collect_box(upstream, up_key, box_dir) == pushed
    assert _collect_box(upstream, up_key, box_dir) == 0  # a second collection lands nothing
