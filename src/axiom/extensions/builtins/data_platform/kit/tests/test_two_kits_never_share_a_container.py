# Copyright (c) 2026 The University of Texas at Austin
# SPDX-License-Identifier: Apache-2.0
"""Two kits for the same tenant never share a local medallion container.

The container name came from the tenant alone. Under pytest-xdist the
integration module ran on two workers, each started its own kit for the same
tenant, and the second `docker run --name` failed with exit 125 (2026-10-06,
main red after #1123). Two checkouts of one site repo on one laptop collide
the same way. The name now also carries the kit's own directory.
"""

from __future__ import annotations

from axiom.extensions.builtins.data_platform import kit
from axiom.extensions.builtins.data_platform.kit.medallion import _container


def test_same_tenant_different_folders_get_different_containers(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    kit.init(a, "rig-site")
    kit.init(b, "rig-site")
    name_a, name_b = _container(kit.load(a)), _container(kit.load(b))
    assert name_a != name_b
    assert name_a.startswith("axiom-kit-rig-site-")


def test_the_same_folder_keeps_its_name(tmp_path):
    kit.init(tmp_path, "rig-site")
    assert _container(kit.load(tmp_path)) == _container(kit.load(tmp_path))
