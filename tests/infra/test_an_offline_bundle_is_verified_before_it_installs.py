# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""An offline bundle is checked before anything from it is used.

A machine with no network installs from a directory somebody carried in. The
manifest lists every file with its sha256, and a signature covers the manifest
(ADR-109: keyless, bound to the release workflow). The installer runs nothing
from a bundle that fails either check, and nothing it runs reaches a network.

Commands are recorded by a fake runner: no cluster, registry or signer is
touched here.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from axiom.infra.deploy import offline_bundle as ob

MODULE = Path(ob.__file__)


def make_bundle(tmp_path: Path, *, sizes=("small", "medium"), arch="amd64") -> Path:
    """A small stand-in bundle with the real layout and a real manifest."""
    root = tmp_path / "bundle"
    files = {
        "tools/helm": b"helm",
        "tools/cosign": b"cosign",
        "tools/k3d": b"k3d",
        "tools/k3s": b"k3s",
        "base/k3s-install.sh": b"#!/bin/sh\n",
        f"base/k3s-airgap-images-{arch}.tar.zst": b"system images",
        "charts/axiom-node-1.2.3.tgz": b"node chart",
        "charts/axiom-cluster-1.2.3.tgz": b"cluster chart",
        "values/axiom-node/values-small.yaml": b"profile: small\n",
        "values/axiom-node/values-medium.yaml": b"profile: medium\n",
        "values/axiom-cluster/values-small.yaml": b"{}\n",
        "values/axiom-cluster/values-medium.yaml": b"{}\n",
        "images/runtime.tar": b"runtime",
        "images/wheelhouse.tar": b"wheels",
        "images/k3s.tar": b"k3s image",
        "images/prometheus.tar": b"prometheus",
        "wheelhouse/pkg-1.0-py3-none-any.whl": b"wheel",
        "trust/trusted_root.json": b"{}",
    }
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    meta = {
        "version": "1.2.3",
        "arch": arch,
        "sizes": list(sizes),
        "packages": {"pin": "pkg==1.0", "version": "1.0"},
        "signing": {
            "issuer": "https://issuer.example",
            "identity": "https://forge.example/o/r/.github/workflows/publish.yml@refs/tags/v{version}",
        },
        "charts": {
            "axiom-node": "charts/axiom-node-1.2.3.tgz",
            "axiom-cluster": "charts/axiom-cluster-1.2.3.tgz",
        },
        "images": [
            {"ref": "ghcr.io/o/axiom-runtime:0.5.0", "file": "images/runtime.tar", "sizes": ["small", "medium"]},
            {"ref": "axiom-wheelhouse:1.0", "file": "images/wheelhouse.tar", "sizes": ["small", "medium"], "wheelhouse": True},
            {"ref": "rancher/k3s:v1.31.5-k3s1", "file": "images/k3s.tar", "sizes": ["small"], "group": "k3d"},
            {"ref": "quay.io/prometheus/prometheus:v3", "file": "images/prometheus.tar", "sizes": ["medium"]},
        ],
    }
    ob.write_manifest(root, ob.build_manifest(root, meta))
    return root


class Recorder:
    """Stands in for subprocess.run: records every command, answers from a table."""

    def __init__(self, answers=None):
        self.calls: list[list[str]] = []
        self.envs: list[dict] = []
        self.answers = answers or {}

    def __call__(self, argv, env=None, **_kw):
        self.calls.append([str(a) for a in argv])
        self.envs.append(dict(env or {}))
        for needle, (code, out) in self.answers.items():
            if needle in " ".join(str(a) for a in argv):
                return subprocess.CompletedProcess(argv, code, out, "")
        return subprocess.CompletedProcess(argv, 0, "", "")


def sign(root: Path) -> None:
    (root / ob.SIGNATURE).write_text("{}")


# --- the manifest -----------------------------------------------------------


def test_a_bundle_as_built_verifies(tmp_path):
    root = make_bundle(tmp_path)
    manifest = ob.verify_manifest(root)
    assert manifest["version"] == "1.2.3"
    listed = {f["path"] for f in manifest["files"]}
    assert "images/runtime.tar" in listed and ob.MANIFEST not in listed


def test_a_changed_file_is_refused_and_named(tmp_path):
    root = make_bundle(tmp_path)
    (root / "images/runtime.tar").write_bytes(b"runtimf")  # same size, one byte different
    with pytest.raises(ob.BundleError, match="images/runtime.tar"):
        ob.verify_manifest(root)


def test_a_missing_file_is_refused_and_named(tmp_path):
    root = make_bundle(tmp_path)
    (root / "charts/axiom-node-1.2.3.tgz").unlink()
    with pytest.raises(ob.BundleError, match=r"missing.*charts/axiom-node-1.2.3.tgz"):
        ob.verify_manifest(root)


def test_a_file_the_manifest_does_not_list_is_refused(tmp_path):
    root = make_bundle(tmp_path)
    (root / "images/extra.tar").write_bytes(b"not in the manifest")
    with pytest.raises(ob.BundleError, match=r"not in the manifest.*images/extra.tar"):
        ob.verify_manifest(root)


def test_a_symlink_is_refused(tmp_path):
    root = make_bundle(tmp_path)
    (root / "images/link.tar").symlink_to(root / "images/runtime.tar")
    with pytest.raises(ob.BundleError, match="symbolic link"):
        ob.verify_manifest(root)


@pytest.mark.parametrize("bad", ["../outside", "/etc/passwd", "images/../../outside"])
def test_a_manifest_path_outside_the_bundle_is_refused(tmp_path, bad):
    root = make_bundle(tmp_path)
    manifest = json.loads((root / ob.MANIFEST).read_text())
    manifest["files"].append({"path": bad, "sha256": "0" * 64, "size": 1})
    (root / ob.MANIFEST).write_text(json.dumps(manifest))
    with pytest.raises(ob.BundleError, match="outside the bundle"):
        ob.verify_manifest(root)


def test_a_bundle_without_a_manifest_is_refused(tmp_path):
    root = make_bundle(tmp_path)
    (root / ob.MANIFEST).unlink()
    with pytest.raises(ob.BundleError, match="no manifest"):
        ob.verify_manifest(root)


def test_a_manifest_of_another_schema_is_refused(tmp_path):
    root = make_bundle(tmp_path)
    manifest = json.loads((root / ob.MANIFEST).read_text())
    manifest["schema"] = 99
    (root / ob.MANIFEST).write_text(json.dumps(manifest))
    with pytest.raises(ob.BundleError, match="schema"):
        ob.verify_manifest(root)


# --- the signature over the manifest ---------------------------------------


def test_an_unsigned_bundle_is_refused(tmp_path):
    root = make_bundle(tmp_path)
    run = Recorder()
    with pytest.raises(ob.BundleError, match="not signed"):
        ob.verify_signature(root, ob.verify_manifest(root), run=run)
    assert run.calls == []


def test_an_unsigned_bundle_installs_only_when_the_operator_says_so(tmp_path):
    root = make_bundle(tmp_path)
    run = Recorder()
    assert ob.verify_signature(root, ob.verify_manifest(root), run=run, allow_unsigned=True) == "unsigned"
    assert run.calls == []


def test_the_signature_is_checked_offline_against_the_release_workflow_at_this_version(tmp_path):
    root = make_bundle(tmp_path)
    sign(root)
    run = Recorder()
    assert ob.verify_signature(root, ob.verify_manifest(root), run=run) == "verified"
    (argv,) = run.calls
    assert argv[0] == str(root / "tools/cosign") and argv[1] == "verify-blob"
    flags = dict(zip(argv[2:-1:2], argv[3:-1:2]))
    assert flags["--bundle"] == str(root / ob.SIGNATURE)
    # The trusted root comes from a file, so nothing is fetched to verify.
    assert flags["--trusted-root"] == str(root / "trust/trusted_root.json")
    assert flags["--certificate-oidc-issuer"] == "https://issuer.example"
    # A signature made for another release does not vouch for this manifest.
    assert flags["--certificate-identity"].endswith("publish.yml@refs/tags/v1.2.3")
    assert argv[-1] == str(root / ob.MANIFEST)


def test_the_operator_can_name_the_identity_instead_of_trusting_the_manifests(tmp_path):
    root = make_bundle(tmp_path)
    sign(root)
    run = Recorder()
    ob.verify_signature(
        root, ob.verify_manifest(root), run=run, identity="https://mine.example/wf@refs/tags/v{version}"
    )
    assert "https://mine.example/wf@refs/tags/v1.2.3" in run.calls[0]


def test_a_signature_that_does_not_verify_is_refused(tmp_path):
    root = make_bundle(tmp_path)
    sign(root)
    run = Recorder({"verify-blob": (1, "no matching signatures")})
    with pytest.raises(ob.BundleError, match="signature"):
        ob.verify_signature(root, ob.verify_manifest(root), run=run)


def test_a_signed_bundle_with_no_verifier_is_refused(tmp_path):
    root = make_bundle(tmp_path)
    sign(root)
    with pytest.raises(ob.BundleError, match="cosign"):
        ob.verify_signature(root, ob.verify_manifest(root), run=Recorder(), cosign=tmp_path / "absent")


# --- the install -----------------------------------------------------------


def install(root, size, run, **kw):
    kw.setdefault("host_arch", "amd64")
    return ob.install(root, size=size, run=run, **kw)


def test_a_tampered_bundle_runs_nothing(tmp_path):
    root = make_bundle(tmp_path)
    sign(root)
    (root / "tools/helm").write_bytes(b"hell")
    run = Recorder()
    with pytest.raises(ob.BundleError, match="tools/helm"):
        install(root, "medium", run)
    assert run.calls == []


def test_an_unsigned_bundle_runs_nothing(tmp_path):
    root = make_bundle(tmp_path)
    run = Recorder()
    with pytest.raises(ob.BundleError, match="not signed"):
        install(root, "medium", run)
    assert run.calls == []


def test_the_signature_is_the_first_thing_run(tmp_path):
    root = make_bundle(tmp_path)
    sign(root)
    run = Recorder()
    install(root, "medium", run)
    assert run.calls[0][1] == "verify-blob"
    assert len(run.calls) > 3


@pytest.mark.parametrize("size", ["small", "medium"])
def test_nothing_the_install_runs_names_a_remote(tmp_path, size):
    """Every chart, image and binary is a path inside the verified bundle."""
    root = make_bundle(tmp_path)
    sign(root)
    run = Recorder()
    install(root, size, run)
    listed = {str(root / f["path"]) for f in ob.verify_manifest(root)["files"]}
    for argv in run.calls[1:]:
        for arg in argv:
            assert "://" not in arg, f"{argv} names a remote"
            for part in arg.split(":"):
                if part.startswith(str(root)) and Path(part).suffix:
                    assert part in listed, f"{arg} is not a file the manifest lists"
    joined = [" ".join(c) for c in run.calls]
    assert any("charts/axiom-cluster-1.2.3.tgz" in c for c in joined)
    assert any("charts/axiom-node-1.2.3.tgz" in c and "upgrade --install" in c for c in joined)


def test_medium_installs_k3s_from_the_bundle_without_a_download(tmp_path):
    root = make_bundle(tmp_path)
    sign(root)
    run = Recorder()
    install(root, "medium", run)
    k3s = [i for i, c in enumerate(run.calls) if c[-1].endswith("base/k3s-install.sh")]
    assert k3s, run.calls
    assert run.envs[k3s[0]]["INSTALL_K3S_SKIP_DOWNLOAD"] == "true"
    imported = [c[-1] for c in run.calls if c[:4] == ["k3s", "ctr", "images", "import"]]
    # Each size loads its own images: the K3D node image is not medium's.
    assert sorted(Path(p).name for p in imported) == ["prometheus.tar", "runtime.tar", "wheelhouse.tar"]


def test_small_creates_the_k3d_cluster_from_bundled_images(tmp_path):
    root = make_bundle(tmp_path)
    sign(root)
    run = Recorder()
    install(root, "small", run)
    joined = [" ".join(c) for c in run.calls]
    assert any(c.startswith("docker load") and c.endswith("images/k3s.tar") for c in joined)
    create = next(c for c in run.calls if c[1:3] == ["cluster", "create"])
    assert create[0] == str(root / "tools/k3d")
    assert "rancher/k3s:v1.31.5-k3s1" in create
    assert any("k3s-airgap-images-amd64.tar.zst:/var/lib/rancher/k3s/agent/images/" in a for a in create)
    imported = next(c for c in run.calls if c[1:3] == ["image", "import"])
    assert not any(a.endswith("prometheus.tar") for a in imported)


def test_the_node_is_told_to_install_packages_from_the_bundles_wheelhouse(tmp_path):
    root = make_bundle(tmp_path)
    sign(root)
    run = Recorder()
    install(root, "medium", run, values=["/site/values.yaml"])
    node = next(c for c in run.calls if "upgrade" in c and any(a.endswith("axiom-node-1.2.3.tgz") for a in c))
    assert "packages.wheelhouse.enabled=true" in node
    assert "packages.wheelhouse.image=axiom-wheelhouse:1.0" in node
    assert "packages.pin=pkg==1.0" in node and "packages.version=1.0" in node
    # The operator's values come after the size's, so they win.
    assert node.index("/site/values.yaml") > node.index(str(root / "values/axiom-node/values-medium.yaml"))


def test_values_that_name_an_image_the_bundle_lacks_stop_before_the_install(tmp_path):
    root = make_bundle(tmp_path)
    sign(root)
    rendered = 'image: "ghcr.io/o/axiom-runtime:0.5.0"\nimage: "ghcr.io/other/chat:1"\n'
    run = Recorder({"template": (0, rendered)})
    with pytest.raises(ob.BundleError, match="ghcr.io/other/chat:1"):
        install(root, "medium", run)
    assert not any("upgrade" in c for c in run.calls)


def test_a_size_the_bundle_was_not_built_for_is_refused(tmp_path):
    root = make_bundle(tmp_path, sizes=("small",))
    sign(root)
    run = Recorder()
    with pytest.raises(ob.BundleError, match="medium"):
        install(root, "medium", run)
    assert not any("upgrade" in c for c in run.calls)


def test_a_bundle_for_another_architecture_is_refused(tmp_path):
    root = make_bundle(tmp_path, arch="arm64")
    sign(root)
    run = Recorder()
    with pytest.raises(ob.BundleError, match="arm64"):
        install(root, "medium", run, host_arch="amd64")
    assert not any("upgrade" in c for c in run.calls)


def test_a_dry_run_prints_the_plan_and_changes_nothing(tmp_path, capsys):
    root = make_bundle(tmp_path)
    sign(root)
    run = Recorder()
    install(root, "medium", run, dry_run=True)
    # Verification still happens; nothing after it does.
    assert [c[1] for c in run.calls] == ["verify-blob"]
    assert "k3s-install.sh" in capsys.readouterr().out


def test_the_installer_needs_nothing_but_the_standard_library():
    """It runs on a machine where nothing of ours is installed yet."""
    tree = ast.parse(MODULE.read_text())
    top_level = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            top_level.add((node.module or "").split(".")[0])
    assert top_level <= set(sys.stdlib_module_names), top_level - set(sys.stdlib_module_names)


def test_the_bundle_carries_its_own_installer(tmp_path):
    """The copy in the bundle runs by path, with no package on the machine."""
    root = make_bundle(tmp_path)
    copy = root / ob.INSTALLER
    copy.write_bytes(MODULE.read_bytes())
    ob.write_manifest(root, ob.build_manifest(root, json.loads((root / ob.MANIFEST).read_text())))
    r = subprocess.run(
        [sys.executable, "-I", str(copy), "verify", str(root), "--allow-unsigned"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    (root / "images/runtime.tar").write_bytes(b"changed")
    r = subprocess.run(
        [sys.executable, "-I", str(copy), "verify", str(root), "--allow-unsigned"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert r.returncode != 0 and "images/runtime.tar" in r.stderr


# --- the build, with the network, helm and docker stood in for -------------

REPO = Path(__file__).resolve().parents[2]
DECLARATION = REPO / "infra" / "bundle" / "offline-bundle.toml"


class Builder:
    """Stands in for helm and docker: writes the files they would write."""

    def __init__(self, platforms=None, unpullable=()):
        self.calls: list[list[str]] = []
        self.platforms = platforms or {}
        self.unpullable = set(unpullable)

    def __call__(self, argv, env=None):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        out, code = "", 0
        if argv[:2] == ["helm", "package"]:
            name, version = Path(argv[2]).name, argv[argv.index("--version") + 1]
            Path(argv[argv.index("--destination") + 1], f"{name}-{version}.tgz").write_bytes(b"chart")
        elif argv[:2] == ["docker", "pull"]:
            code = 1 if argv[-1] in self.unpullable else 0
        elif argv[:3] == ["docker", "image", "inspect"]:
            out = self.platforms.get(argv[-1], self.arch)
        elif argv[:2] == ["docker", "save"]:
            Path(argv[3]).write_bytes(argv[-1].encode())
        elif argv[:3] == ["docker", "buildx", "build"]:
            if "--load" in argv:
                self.platforms[argv[argv.index("-t") + 1]] = argv[argv.index("--platform") + 1]
            for a in argv:
                if a.startswith("type=docker,dest="):
                    Path(a.split("dest=", 1)[1]).write_bytes(b"wheelhouse image")
        elif argv[:2] == ["docker", "run"] and "wheel" in argv:
            mount = argv[argv.index("-v") + 1].split(":")[0]
            Path(mount, "pkg-1.0-py3-none-any.whl").write_bytes(b"wheel")
        return subprocess.CompletedProcess(argv, code, out, "")


def fake_fetch(decl, arch, wrong=None):
    """Hands back content whose sha256 the (patched) declaration expects."""
    import hashlib

    for d in decl["download"]:
        d.pop("member", None)
        body = d["name"].encode()
        d["sha256"] = {arch: hashlib.sha256(body).hexdigest()}

    def fetch(url, dest):
        name = next(d["name"] for d in decl["download"] if ob.download_url(d, arch) == url)
        Path(dest).write_bytes(b"something else" if name == wrong else name.encode())

    return fetch


def build_with_fakes(tmp_path, arch="amd64", wrong=None, **builder):
    decl = ob.load_declaration(DECLARATION)
    run = Builder(**builder)
    run.arch = f"linux/{arch}"
    out = tmp_path / "out"
    manifest = ob.build(decl, REPO, out, version="9.9.9", arch=arch, run=run, fetch=fake_fetch(decl, arch, wrong))
    return out, manifest, run


def test_a_built_bundle_verifies_and_holds_what_an_install_needs(tmp_path):
    out, manifest, _run = build_with_fakes(tmp_path)
    assert ob.verify_manifest(out) == manifest
    listed = {f["path"] for f in manifest["files"]}
    for needed in (
        "charts/axiom-node-9.9.9.tgz",
        "charts/axiom-cluster-9.9.9.tgz",
        "values/axiom-node/values-small.yaml",
        "values/axiom-cluster/values-medium.yaml",
        "tools/helm",
        "tools/cosign",
        "tools/k3d",
        "tools/k3s",
        "base/k3s-install.sh",
        "base/k3s-airgap-images-amd64.tar.zst",
        "wheelhouse/pkg-1.0-py3-none-any.whl",
        ob.INSTALLER,
    ):
        assert needed in listed, needed
    assert {i["file"] for i in manifest["images"]} <= listed
    # The plan an install would follow uses only what was built.
    for size in manifest["sizes"]:
        for step in ob.install_steps(out, manifest, size):
            for arg in step["argv"]:
                if arg.startswith(str(out)) and ":" not in arg:
                    assert Path(arg).relative_to(out).as_posix() in listed, arg


def test_the_wheelhouse_is_proven_with_the_network_off_before_it_ships(tmp_path):
    _out, _manifest, run = build_with_fakes(tmp_path)
    offline = [c for c in run.calls if c[:2] == ["docker", "run"] and "--network" in c]
    assert offline and offline[0][offline[0].index("--network") + 1] == "none"
    assert "--no-index" in offline[0][-1]


def test_a_download_that_is_not_the_pinned_file_is_refused(tmp_path):
    with pytest.raises(ob.BundleError, match="k3s .*sha256"):
        build_with_fakes(tmp_path, wrong="k3s")


def test_an_image_the_registry_lacks_for_this_architecture_is_built_or_refused(tmp_path):
    runtime = "ghcr.io/b-tree-labs/axiom-runtime:0.5.0"
    # The registry answers with the other architecture: the declared build is used.
    _out, manifest, run = build_with_fakes(tmp_path, arch="arm64", platforms={runtime: "linux/amd64"})
    assert next(i for i in manifest["images"] if i["ref"] == runtime)["origin"] == "built"
    assert any(c[:3] == ["docker", "buildx", "build"] and "linux/arm64" in c and runtime in c for c in run.calls)
    # No build declared: the bundle is not made with a wrong-architecture image in it.
    with pytest.raises(ob.BundleError, match="nginx"):
        build_with_fakes(tmp_path / "b", arch="arm64", platforms={"nginx:1.27.3-alpine": "linux/amd64"})


def test_the_plan_lists_what_a_build_fetches_and_fetches_nothing():
    decl = ob.load_declaration(DECLARATION)
    plan = ob.plan(decl, "arm64")
    assert all("arm64" in d["url"] or d["name"] in ("k3s-install",) for d in plan["downloads"])
    refs = {i["ref"] for i in plan["images"]}
    assert "ghcr.io/open-webui/open-webui:v0.10.2" not in refs
    assert "ghcr.io/open-webui/open-webui:v0.10.2" in {i["ref"] for i in ob.plan(decl, "arm64", groups="all")["images"]}
    with pytest.raises(ob.BundleError, match="riscv"):
        ob.plan(decl, "riscv64")
