# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A Box login that rotates on use is written back, never left on disk.

Box refresh tokens are single use: each refresh returns a new one. A login
handed to rclone read-only stops working at its first refresh. The stand-in
rclone below behaves like the real one on refresh: it rewrites the token in
the config file it was given.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from axiom.extensions.builtins.data_platform.forward import BoxDropTarget

FAKE_RCLONE = """#!/usr/bin/env python3
import sys, re
args = sys.argv[1:]
conf = args[args.index("--config") + 1]
text = open(conf).read()
open(conf, "w").write(re.sub(r"token = .*", 'token = {"refresh_token":"rotated"}', text))
open(sys.argv[0] + ".seen", "w").write(text)
if "rcat" in args:
    sys.stdin.buffer.read()
sys.exit(0)
"""


def test_the_rotated_login_is_saved_and_the_config_file_is_gone(tmp_path):
    rc = tmp_path / "rclone"
    rc.write_text(FAKE_RCLONE)
    rc.chmod(rc.stat().st_mode | stat.S_IEXEC)
    saved: list[str] = []
    box = BoxDropTarget("Partner/_rows", load_token=lambda: '{"refresh_token":"first"}',
                        save_token=saved.append, rclone=str(rc))
    assert box.probe().ok
    assert saved == ['{"refresh_token":"rotated"}']
    seen = Path(str(rc) + ".seen").read_text()
    assert "first" in seen and "type = box" in seen          # rclone got the login through its config
    assert not any(p.name.startswith("axiom-forward-") for p in Path(os.environ.get("TMPDIR", "/tmp")).glob("axiom-forward-*"))


def test_an_unchanged_login_is_not_rewritten(tmp_path):
    rc = tmp_path / "rclone"
    rc.write_text("#!/bin/sh\nexit 0\n")
    rc.chmod(rc.stat().st_mode | stat.S_IEXEC)
    saved: list[str] = []
    box = BoxDropTarget("x", load_token=lambda: "same", save_token=saved.append, rclone=str(rc))
    box.probe()
    assert saved == []


def test_a_login_made_through_our_app_is_refreshed_with_that_app(tmp_path):
    """rclone refreshes with the client id and secret in its config; a login issued to UT's own OAuth
    app must be refreshed with that app's credentials, or Box refuses it."""
    rc = tmp_path / "rclone"
    rc.write_text(FAKE_RCLONE)
    rc.chmod(rc.stat().st_mode | stat.S_IEXEC)
    box = BoxDropTarget("x", load_token=lambda: "tok", save_token=lambda _v: None, rclone=str(rc),
                        load_app=lambda: {"client_id": "app-id", "client_secret": "app-secret"})
    assert box.probe().ok
    seen = Path(str(rc) + ".seen").read_text()
    assert "client_id = app-id" in seen and "client_secret = app-secret" in seen


def test_without_an_app_rclones_own_is_used(tmp_path):
    rc = tmp_path / "rclone"
    rc.write_text(FAKE_RCLONE)
    rc.chmod(rc.stat().st_mode | stat.S_IEXEC)
    box = BoxDropTarget("x", load_token=lambda: "tok", save_token=lambda _v: None, rclone=str(rc))
    box.probe()
    assert "client_id" not in Path(str(rc) + ".seen").read_text()
