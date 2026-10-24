# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The Postgres this generator creates is the one that grew to 528 GB.

Every property that kept that database alive on a live node was hand drift from
this file, applied with kubectl and recorded nowhere:

- ``strategy: Recreate``. The generator emitted none, so the default
  RollingUpdate applied. On a ReadWriteOnce volume a rolling update starts the
  new pod before the old one releases the volume, and the rollout hangs.
- a 16Gi memory limit. The generator says 1Gi.
- a 1Gi ``/dev/shm``. The generator gives none, so Kubernetes' 64 MB applies,
  and parallel VACUUM on a large table fails with "could not resize shared
  memory segment".
- memory settings sized to the limit. The generator set none, so Postgres used
  128 MB of shared_buffers — it reads the host, not the container.

The dangerous combination is the last two together. With tuning applied by hand
on top of a hand-raised limit, re-running this generator puts the 1Gi limit back
while the 4GB buffer pool stays, and the database is OOM-killed at start.
Settings and the limit they are sized against must come from one place, which is
what these tests hold.
"""

from __future__ import annotations

import pytest

yaml = pytest.importorskip("yaml")

from axiom.extensions.builtins.signals.bootstrap import Bootstrap, BootstrapConfig  # noqa: E402
from axiom.infra.store_health import recommended_postgres_settings  # noqa: E402


def _docs(**cfg):
    manifest = Bootstrap(BootstrapConfig(**cfg))._get_postgres_manifest()
    return [d for d in yaml.safe_load_all(manifest) if d]


def _deployment(**cfg):
    return next(d for d in _docs(**cfg) if d["kind"] == "Deployment")


def _container(**cfg):
    return _deployment(**cfg)["spec"]["template"]["spec"]["containers"][0]


def _bytes(q: str) -> int:
    for suffix, mult in (("Gi", 1024**3), ("Mi", 1024**2), ("Ki", 1024)):
        if q.endswith(suffix):
            return int(float(q[: -len(suffix)]) * mult)
    return int(q)


def _args(container) -> dict[str, str]:
    raw = container.get("args") or []
    pairs = [raw[i + 1] for i in range(0, len(raw) - 1, 2) if raw[i] == "-c"]
    return dict(p.split("=", 1) for p in pairs)


def test_the_manifest_is_valid_yaml():
    kinds = [d["kind"] for d in _docs()]
    assert "Deployment" in kinds and "PersistentVolumeClaim" in kinds


def test_a_database_on_a_single_attach_volume_recreates_rather_than_rolls():
    """A rolling update on ReadWriteOnce waits forever for a volume the old pod
    still holds."""
    assert _deployment()["spec"]["strategy"]["type"] == "Recreate"


def test_shared_memory_is_sized_for_parallel_maintenance():
    spec = _deployment()["spec"]["template"]["spec"]
    shm = [v for v in spec["volumes"] if v.get("emptyDir", {}).get("medium") == "Memory"]
    assert shm, "no memory-backed /dev/shm volume: Kubernetes' 64 MB default applies"
    mounts = {m["mountPath"] for m in spec["containers"][0]["volumeMounts"]}
    assert "/dev/shm" in mounts


@pytest.mark.parametrize("limit", ["2Gi", "16Gi", "64Gi"])
def test_settings_agree_with_the_limit_they_run_under(limit):
    """The OOM case: settings and limit from different sources."""
    container = _container(postgres_memory_limit=limit)
    assert container["resources"]["limits"]["memory"] == limit
    want = recommended_postgres_settings(_bytes(limit))
    got = _args(container)
    for key, value in want.items():
        assert got.get(key) == value, (key, got.get(key), value)


def test_the_buffer_pool_never_exceeds_the_limit():
    """The invariant the OOM violates, checked directly rather than through
    the sizing function's arithmetic."""
    for limit in ("1Gi", "2Gi", "16Gi"):
        sb = _args(_container(postgres_memory_limit=limit))["shared_buffers"]
        sb_bytes = _bytes(sb.replace("GB", "Gi").replace("MB", "Mi"))
        assert sb_bytes < _bytes(limit) / 2, (limit, sb)


def test_the_default_stays_light_enough_for_a_laptop():
    """This generator also bootstraps developer machines. A production-sized
    buffer pool there costs real memory on a 16 GB laptop."""
    container = _container()
    assert _bytes(container["resources"]["limits"]["memory"]) <= 4 * 1024**3


def test_a_server_can_declare_the_cpu_it_actually_has():
    """Re-generating with the old fixed 2000m would have cut the node from 8."""
    assert _container(postgres_cpu_limit="8")["resources"]["limits"]["cpu"] == "8"
