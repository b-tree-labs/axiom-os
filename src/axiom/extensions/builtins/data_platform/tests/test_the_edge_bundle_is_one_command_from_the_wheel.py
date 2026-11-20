# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The ingest edge's deploy bundle comes out of the installed package in one command.

The person who deploys the edge is not on our network and has no checkout:
what they have is PyPI. ``axi data edge-bundle`` writes the folder the README
walks through, pinned to the release that wrote it, so there is no file to
transfer and no pin to edit by hand.

What has to hold:

- the folder is complete and its container installs *this* release, in both
  places the version appears (the Dockerfile and the image tag);
- a build that is not a published release refuses to pin one, unless told which;
- it never overwrites a folder it did not make;
- the tarball is reproducible (the same release gives the same bytes), so two
  people can compare checksums;
- the pieces agree with each other: every nginx zone and log format the block
  uses is declared in the limits file, and every file the README names is in the
  folder;
- nothing in it is a secret.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import stat
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from axiom.infra.skills import SkillContext, SkillRegistry

from ..skills import edge_bundle

FILES = {
    "Dockerfile", "compose.yaml", "entrypoint.sh", "edge-admin.sh", "edge.env.example",
    "dot-env.example", "sources.txt.example", "nginx-ingest-edge.conf",
    "nginx-ingest-edge-limits.conf", "nginx-ingest-edge-port80.conf", "README.md", "BUNDLE",
}


def _run(tmp_path, **params):
    ctx = SkillContext(registry=SkillRegistry(), state_dir=tmp_path / "state", logger=logging.getLogger("t"))
    return edge_bundle.run(params, ctx)


def test_the_folder_is_complete_and_pinned_to_the_release(tmp_path):
    out = tmp_path / "ingest-edge"
    r = _run(tmp_path, out=str(out), version="0.70.0")
    assert r.ok, r.errors
    assert {p.name for p in out.iterdir()} == FILES
    assert "ARG AXIOM_VERSION=0.70.0\n" in (out / "Dockerfile").read_text()
    compose = (out / "compose.yaml").read_text()
    assert "image: axiom-ingest-edge:0.70.0\n" in compose
    assert (out / "BUNDLE").read_text().split()[:2] == ["axiom-os-lm", "0.70.0"]
    assert r.value["version"] == "0.70.0"


def test_the_scripts_keep_their_executable_bit_and_parse(tmp_path):
    out = tmp_path / "ingest-edge"
    _run(tmp_path, out=str(out), version="0.70.0")
    for name in ("entrypoint.sh", "edge-admin.sh"):
        assert (out / name).stat().st_mode & stat.S_IXUSR, name
        assert subprocess.run(["sh", "-n", str(out / name)]).returncode == 0, name


def test_the_default_is_the_installed_release(tmp_path, monkeypatch):
    monkeypatch.setattr(edge_bundle, "_installed_version", lambda: "1.2.3")
    out = tmp_path / "b"
    assert _run(tmp_path, out=str(out)).ok
    assert "ARG AXIOM_VERSION=1.2.3\n" in (out / "Dockerfile").read_text()


@pytest.mark.parametrize("built", ["0.70.0+local", "0.70.1.dev3", "0.70.0rc1", "unknown"])
def test_a_build_that_is_not_a_release_will_not_pin_itself(tmp_path, monkeypatch, built):
    monkeypatch.setattr(edge_bundle, "_installed_version", lambda: built)
    r = _run(tmp_path, out=str(tmp_path / "b"))
    assert not r.ok and "--version" in " ".join(r.errors)
    assert not (tmp_path / "b").exists()


def test_a_version_that_is_not_a_release_number_is_refused_even_when_given(tmp_path):
    r = _run(tmp_path, out=str(tmp_path / "b"), version="0.70.0; rm -rf /")
    assert not r.ok


def test_it_will_not_write_into_a_folder_it_did_not_make(tmp_path):
    out = tmp_path / "mine"
    out.mkdir()
    (out / "notes.txt").write_text("keep me")
    r = _run(tmp_path, out=str(out), version="0.70.0")
    assert not r.ok and "force" in " ".join(r.errors).lower()
    assert (out / "notes.txt").read_text() == "keep me"
    assert not (out / "Dockerfile").exists()


def test_it_refreshes_a_bundle_it_made_itself(tmp_path):
    out = tmp_path / "ingest-edge"
    assert _run(tmp_path, out=str(out), version="0.70.0").ok
    assert _run(tmp_path, out=str(out), version="0.70.1").ok
    assert "ARG AXIOM_VERSION=0.70.1\n" in (out / "Dockerfile").read_text()


def test_the_tarball_is_reproducible_and_its_checksum_is_reported(tmp_path):
    a = _run(tmp_path, out=str(tmp_path / "a" / "ingest-edge"), version="0.70.0", tar=True)
    b = _run(tmp_path, out=str(tmp_path / "b" / "ingest-edge"), version="0.70.0", tar=True)
    assert a.ok and b.ok, (a.errors, b.errors)
    ta, tb = Path(a.value["tarball"]), Path(b.value["tarball"])
    assert ta.name == "ingest-edge-0.70.0.tar.gz"
    assert ta.read_bytes() == tb.read_bytes()
    assert a.value["sha256"] == hashlib.sha256(ta.read_bytes()).hexdigest()
    with tarfile.open(ta) as t:
        names = t.getnames()
        assert {n.split("/")[0] for n in names} == {"ingest-edge"}
        assert {n.split("/", 1)[1] for n in names if "/" in n} == FILES
        script = t.getmember("ingest-edge/edge-admin.sh")
        assert script.mode & 0o111 and script.uid == 0 and script.mtime == 0


def test_the_command_is_wired_to_the_cli(tmp_path):
    out = tmp_path / "ingest-edge"
    p = subprocess.run(
        [sys.executable, "-m", "axiom.extensions.builtins.data_platform", "--json", "edge-bundle",
         "--out", str(out), "--version", "0.70.0"],
        capture_output=True, text=True, timeout=120,
    )
    assert p.returncode == 0, (p.stdout[-500:], p.stderr[-800:])
    assert json.loads(p.stdout)["ok"] is True
    assert (out / "README.md").exists()


# -- the pieces agree with each other ----------------------------------------


@pytest.fixture
def bundle(tmp_path):
    out = tmp_path / "ingest-edge"
    assert _run(tmp_path, out=str(out), version="0.70.0").ok
    return out


def test_every_zone_and_log_format_the_block_uses_is_declared(bundle):
    block = (bundle / "nginx-ingest-edge.conf").read_text()
    limits = (bundle / "nginx-ingest-edge-limits.conf").read_text()
    used_zones = set(re.findall(r"limit_(?:req|conn)\s+(?:zone=)?(\w+)", block))
    declared = set(re.findall(r"limit_(?:req|conn)_zone\s+\S+\s+zone=(\w+):", limits))
    assert used_zones and used_zones <= declared, (used_zones, declared)
    used_formats = set(re.findall(r"access_log\s+\S+\s+(\w+)", block))
    formats = set(re.findall(r"^log_format\s+(\w+)", limits, re.M))
    assert used_formats and used_formats <= formats


def test_the_block_serves_only_the_two_prefixes_and_never_appends_the_forwarded_address(bundle):
    block = (bundle / "nginx-ingest-edge.conf").read_text()
    assert re.findall(r"^\s*location\s+(\S+)\s*\{", block, re.M) == ["/ingest/", "/edge/"]
    assert "$proxy_add_x_forwarded_for" not in block
    assert block.count("proxy_set_header   X-Forwarded-For   $remote_addr;") == 2


def test_the_port_80_block_acts_before_the_servers_redirect_can(bundle):
    # A server-level `return` runs before any `location` is chosen, so a
    # `location` here would never be reached; an `if` placed above it is.
    text = (bundle / "nginx-ingest-edge-port80.conf").read_text()
    code = "\n".join(line for line in text.splitlines() if not line.strip().startswith("#"))
    assert "location" not in code and "if ($uri" in code and "return 404" in code
    assert "ABOVE" in text


def test_the_log_format_leaves_out_the_query_string(bundle):
    limits = (bundle / "nginx-ingest-edge-limits.conf").read_text()
    fmt = limits[limits.index("log_format"):limits.index(";", limits.index("log_format") + 200) + 1]
    assert "$uri" in fmt and "$request " not in fmt and "$request_uri" not in fmt and "$args" not in fmt


def test_every_file_the_readme_names_is_in_the_folder(bundle):
    readme = (bundle / "README.md").read_text()
    # A file in the folder, not a place it is copied to (a name after a slash).
    named = set(re.findall(r"(?<![/\w.-])([\w-]+(?:\.[\w-]+)*\.(?:conf|sh|example|yaml))", readme))
    named -= {"nginx.conf", "default.conf"}
    missing = {n for n in named if not (bundle / n).exists() and "<" not in n}
    assert not missing, missing


def test_compose_reads_files_that_exist_or_are_made_from_examples(bundle):
    compose = (bundle / "compose.yaml").read_text()
    assert "env_file: edge.env" in compose and (bundle / "edge.env.example").exists()
    assert "EDGE_CONFIG_DIR" in compose and (bundle / "dot-env.example").exists()
    assert (bundle / "entrypoint.sh").exists() and "COPY entrypoint.sh" in (bundle / "Dockerfile").read_text()


def test_the_container_is_locked_down(bundle):
    compose = (bundle / "compose.yaml").read_text()
    for needle in ("read_only: true", "cap_drop:", "no-new-privileges:true", "pids_limit:",
                   "max-size:", '"127.0.0.1:8787:8787"'):
        assert needle in compose, needle


def test_nothing_in_it_is_a_secret(bundle):
    for p in bundle.iterdir():
        text = p.read_text()
        assert not re.search(r"axk_[0-9a-f]{12}_", text), p.name
        assert "secret_hash" not in text and "BEGIN PRIVATE KEY" not in text, p.name
