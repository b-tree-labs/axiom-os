# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""The offline install bundle: build it where there is a network, verify and
install it where there is none.

A bundle is a directory for one release and one architecture. It holds both
charts (``infra/charts/axiom-node`` and the cluster base it relies on), every
image they name at the sizes it serves (ADR-215: small on K3D, medium on k3s),
the k3s and K3D pieces those sizes start from, a wheelhouse for the Python
packages a role installs on first boot, a manifest with the sha256 of every
file, and a signature over the manifest (ADR-109: keyless, bound to the release
workflow at the release's tag). What goes in is listed in
``infra/bundle/offline-bundle.toml``.

    python -m axiom.infra.deploy.offline_bundle plan  --arch amd64
    python -m axiom.infra.deploy.offline_bundle build --version 1.2.3 --arch amd64 --out DIR
    python3 DIR/install-from-bundle.py verify
    python3 DIR/install-from-bundle.py install --size medium -f my-values.yaml

The bundle carries a copy of this file as ``install-from-bundle.py``, so the
verify and install halves use the standard library only (Python 3.8 or later)
and run on a machine where nothing of ours is installed yet. The installer
checks the manifest and the signature before it uses anything, and everything
it then runs is a file the manifest lists.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

SCHEMA = 1
KIND = "axiom-offline-bundle"
MANIFEST = "manifest.json"
SIGNATURE = "manifest.json.sigstore.json"
INSTALLER = "install-from-bundle.py"
TRUSTED_ROOT = "trust/trusted_root.json"
COSIGN = "tools/cosign"
ARCHES = ("amd64", "arm64")
#: Files that are about the manifest and so cannot be listed in it.
UNLISTED = (MANIFEST, SIGNATURE)
K3S_IMAGES_DIR = "/var/lib/rancher/k3s/agent/images"
K3S_KUBECONFIG = "/etc/rancher/k3s/k3s.yaml"


class BundleError(Exception):
    """The bundle, or what was asked of it, is refused. The message says why."""


def _run(argv, env=None):
    """Run a command, with ``env`` added to the environment. Never a shell."""
    full = dict(os.environ)
    full.update(env or {})
    return subprocess.run([str(a) for a in argv], env=full, text=True, capture_output=True)


def _must(run, argv, env=None, what=""):
    r = run(argv, env=env)
    if r.returncode != 0:
        tail = ((r.stdout or "") + (r.stderr or ""))[-1500:]
        raise BundleError(f"{what or ' '.join(str(a) for a in argv[:4])} failed ({r.returncode}):\n{tail}")
    return r


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


# --- the manifest -----------------------------------------------------------


def _walk(root: Path):
    """Every file under ``root`` as (relative posix path, is a symbolic link)."""
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            p = Path(base) / name
            if p.is_symlink():
                yield p.relative_to(root).as_posix(), True
            elif p.is_file():
                yield p.relative_to(root).as_posix(), False


def build_manifest(root: Path, meta: dict) -> dict:
    """The manifest for the files now under ``root``: ``meta`` plus every
    file's path, size and sha256. A symbolic link is refused: the manifest
    could not say what it would point to on another machine."""
    root = Path(root)
    files = []
    for rel, link in sorted(_walk(root)):
        if link:
            raise BundleError(f"symbolic link in the bundle: {rel}")
        if rel in UNLISTED:
            continue
        p = root / rel
        files.append({"path": rel, "sha256": sha256_of(p), "size": p.stat().st_size})
    manifest = dict(meta)
    manifest.update({"schema": SCHEMA, "kind": KIND, "files": files})
    return manifest


def write_manifest(root: Path, manifest: dict) -> Path:
    path = Path(root) / MANIFEST
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return path


def verify_manifest(root: Path) -> dict:
    """Read the manifest and check the directory against it, both ways: every
    listed file is present with the listed sha256, and nothing else is there.
    Raises :class:`BundleError` naming each file that is wrong."""
    root = Path(root)
    path = root / MANIFEST
    if not path.is_file():
        raise BundleError(f"no manifest: {path} does not exist")
    try:
        manifest = json.loads(path.read_text())
    except ValueError as e:
        raise BundleError(f"the manifest is not JSON: {e}") from e
    if not isinstance(manifest, dict) or manifest.get("kind") != KIND or manifest.get("schema") != SCHEMA:
        raise BundleError(f"the manifest is not a schema {SCHEMA} {KIND} manifest")

    problems: dict[str, list[str]] = {}
    listed: dict[str, dict] = {}
    for entry in manifest.get("files") or []:
        rel = str(entry.get("path", ""))
        parts = Path(rel).parts
        if not rel or Path(rel).is_absolute() or ".." in parts or rel in UNLISTED:
            problems.setdefault("outside the bundle", []).append(rel)
            continue
        listed[rel] = entry

    present = {}
    for rel, link in _walk(root):
        if link:
            problems.setdefault("symbolic link", []).append(rel)
        elif rel not in UNLISTED:
            present[rel] = root / rel
    for rel in sorted(set(listed) - set(present)):
        problems.setdefault("missing", []).append(rel)
    for rel in sorted(set(present) - set(listed)):
        problems.setdefault("not in the manifest", []).append(rel)
    for rel in sorted(set(present) & set(listed)):
        entry = listed[rel]
        if present[rel].stat().st_size != entry.get("size") or sha256_of(present[rel]) != entry.get("sha256"):
            problems.setdefault("changed", []).append(rel)
    if problems:
        lines = [f"{kind}: {', '.join(names)}" for kind, names in sorted(problems.items())]
        raise BundleError("the bundle does not match its manifest\n  " + "\n  ".join(lines))
    return manifest


def verify_signature(
    root: Path,
    manifest: dict,
    *,
    run=_run,
    allow_unsigned: bool = False,
    identity: str | None = None,
    issuer: str | None = None,
    cosign: Path | None = None,
    trusted_root: Path | None = None,
) -> str:
    """Check the signature over the manifest. Returns ``"verified"``, or
    ``"unsigned"`` when there is none and the operator allowed that.

    The check is cosign's, against a Sigstore bundle and a trusted root that
    are both files, so nothing is fetched. The signer must be the release
    workflow at this manifest's own version: a signature made for another
    release does not vouch for this one. ``identity`` (with ``{version}``)
    replaces the one the manifest names, for an operator who holds the expected
    identity from somewhere other than the bundle.
    """
    root = Path(root)
    sig = root / SIGNATURE
    if not sig.is_file():
        if allow_unsigned:
            return "unsigned"
        raise BundleError(
            f"the bundle is not signed ({SIGNATURE} is absent). A bundle built by hand has no "
            "signature; pass --allow-unsigned to use one you built yourself."
        )
    signing = manifest.get("signing") or {}
    identity = (identity or signing.get("identity") or "").replace("{version}", str(manifest.get("version", "")))
    issuer = issuer or signing.get("issuer") or ""
    if not identity or not issuer:
        raise BundleError("the signature cannot be checked: no signer identity or issuer (pass --identity and --issuer)")
    cosign = Path(cosign) if cosign else root / COSIGN
    if not cosign.is_file():
        raise BundleError(f"the signature cannot be checked: cosign is not at {cosign} (pass --cosign)")
    trusted_root = Path(trusted_root) if trusted_root else root / TRUSTED_ROOT
    if not trusted_root.is_file():
        raise BundleError(f"the signature cannot be checked: no trusted root at {trusted_root} (pass --trusted-root)")
    try:
        os.chmod(cosign, 0o755)
    except OSError:
        pass
    r = run(
        [
            str(cosign),
            "verify-blob",
            "--bundle",
            str(sig),
            "--trusted-root",
            str(trusted_root),
            "--certificate-identity",
            identity,
            "--certificate-oidc-issuer",
            issuer,
            str(root / MANIFEST),
        ]
    )
    if r.returncode != 0:
        tail = ((r.stdout or "") + (r.stderr or "")).strip()[-800:]
        raise BundleError(f"the manifest's signature does not verify as {identity}:\n{tail}")
    return "verified"


# --- which images a rendered chart names -----------------------------------

_IMAGE_KEY = re.compile(r"""(?m)^[\s-]*[\w./-]*[iI]mage:[ \t]*["']?([^\s"'#]+)""")
_IMAGE_ARG = re.compile(r"""--[\w-]*(?:image|reloader)[\w-]*=([^\s"',\]]+)""")
_IMAGE = re.compile(r"^[a-z0-9][\w.-]*(?::\d+)?(?:/[\w.-]+)*(?::[\w.-]+)?(?:@sha256:[0-9a-f]{64})?$")


def normalize_ref(ref: str) -> str:
    """One spelling per image: Docker Hub's implied registry and ``library/``
    are dropped, so ``docker.io/library/nginx:1`` and ``nginx:1`` compare equal."""
    for prefix in ("index.docker.io/", "docker.io/"):
        if ref.startswith(prefix):
            ref = ref[len(prefix) :]
    if ref.startswith("library/"):
        ref = ref[len("library/") :]
    return ref


def image_refs_in(rendered: str) -> set[str]:
    """Every image reference in rendered chart text: any key ending in
    ``image`` with a reference as its value (containers, annotations, custom
    resources), and operator arguments that name an image. Read as text, so it
    needs no YAML parser where the installer runs."""
    found = set()
    for value in _IMAGE_KEY.findall(rendered) + _IMAGE_ARG.findall(rendered):
        if _IMAGE.match(value) and ("/" in value or ":" in value):
            found.add(normalize_ref(value))
    return found


# --- the declaration (build side) ------------------------------------------


def load_declaration(path: Path) -> dict:
    import tomllib  # Python 3.11+; only the build side reads the declaration

    decl = tomllib.loads(Path(path).read_text())
    if decl.get("schema") != SCHEMA:
        raise BundleError(f"{path}: schema {decl.get('schema')!r} is not {SCHEMA}")
    for key in ("download", "image", "referenced_not_bundled"):
        decl.setdefault(key, [])
    return decl


def default_groups(decl: dict) -> list[str]:
    return [name for name, g in decl["groups"].items() if g.get("default", True)]


def _groups(decl: dict, groups) -> set[str]:
    if groups is None:
        return set(default_groups(decl))
    if groups == "all":
        return set(decl["groups"])
    unknown = set(groups) - set(decl["groups"])
    if unknown:
        raise BundleError(f"unknown group: {', '.join(sorted(unknown))} (known: {', '.join(decl['groups'])})")
    return set(groups)


def images_for(decl: dict, sizes, groups=None) -> list[dict]:
    """The images for ``sizes``. ``groups`` is a list of names, ``"all"``, or
    ``None`` for the groups that are on by default."""
    wanted = _groups(decl, groups)
    return [i for i in decl["image"] if i["group"] in wanted and set(i["sizes"]) & set(sizes)]


def downloads_for(decl: dict, sizes) -> list[dict]:
    return [d for d in decl["download"] if set(d["sizes"]) & set(sizes)]


def download_url(d: dict, arch: str) -> str:
    url = d["url"]
    return (url[arch] if isinstance(url, dict) else url).replace("{arch}", arch)


def image_file(ref: str) -> str:
    return "images/" + re.sub(r"[^\w.-]+", "_", normalize_ref(ref)) + ".tar"


def _check_arch(arch: str) -> None:
    if arch not in ARCHES:
        raise BundleError(f"architecture {arch!r} is not one of {', '.join(ARCHES)}")


def plan(decl: dict, arch: str, sizes=None, groups=None) -> dict:
    """What a build would fetch, without fetching anything."""
    _check_arch(arch)
    sizes = list(sizes or decl["sizes"])
    return {
        "arch": arch,
        "sizes": sizes,
        "groups": sorted(_groups(decl, groups)),
        "downloads": [
            {"name": d["name"], "version": d["version"], "url": download_url(d, arch), "sha256": d["sha256"][arch]}
            for d in downloads_for(decl, sizes)
        ],
        "images": [
            {"ref": i["ref"], "group": i["group"], "sizes": i["sizes"], "file": image_file(i["ref"])}
            for i in images_for(decl, sizes, groups)
        ],
    }


# --- build ------------------------------------------------------------------


def _fetch(url: str, dest: Path) -> None:
    with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as f:  # noqa: S310 (https, pinned by sha256)
        shutil.copyfileobj(r, f, 1 << 20)


def _download(d: dict, arch: str, out: Path, fetch) -> None:
    dest = out / d["dest"].replace("{arch}", arch)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        got = Path(tmp) / "download"
        fetch(download_url(d, arch), got)
        digest = sha256_of(got)
        if digest != d["sha256"][arch]:
            raise BundleError(
                f"{d['name']} {d['version']} for {arch}: sha256 is {digest}, the declaration pins {d['sha256'][arch]}"
            )
        member = d.get("member")
        if member:
            with tarfile.open(got) as tar:
                src = tar.extractfile(member.replace("{arch}", arch))
                if src is None:
                    raise BundleError(f"{d['name']}: {member} is not a file in the archive")
                with open(dest, "wb") as f:
                    shutil.copyfileobj(src, f)
        else:
            shutil.copyfile(got, dest)
    os.chmod(dest, int(d.get("mode", "0644"), 8))


def _image_platform(run, ref: str) -> str:
    r = run(["docker", "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", ref])
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def _save_image(img: dict, arch: str, repo: Path, out: Path, run) -> dict:
    """Pull the image for the bundle's architecture (or build it from the
    declared context when the registry has none for it) and save it."""
    ref, want = img["ref"], f"linux/{arch}"
    origin = "pulled"
    pulled = run(["docker", "pull", "--platform", want, ref]).returncode == 0
    if not pulled or _image_platform(run, ref) != want:
        if not img.get("build"):
            raise BundleError(f"{ref}: no image for {want} could be pulled, and the declaration names no build")
        _must(
            run,
            ["docker", "buildx", "build", "--platform", want, "--load", "-t", ref, str(repo / img["build"])],
            what=f"building {ref} for {want}",
        )
        origin = "built"
        if _image_platform(run, ref) != want:
            raise BundleError(f"{ref}: the build did not produce a {want} image")
    rel = image_file(ref)
    (out / rel).parent.mkdir(parents=True, exist_ok=True)
    _must(run, ["docker", "save", "-o", str(out / rel), ref], what=f"saving {ref}")
    return {"ref": ref, "file": rel, "sizes": img["sizes"], "group": img["group"], "origin": origin}


def _wheelhouse(img: dict, python_image: str, pin: str, arch: str, out: Path, run) -> dict:
    """Resolve the pinned packages into wheels inside the runtime image (the
    interpreter and platform the roles install with), prove they install with
    the network off, and build the image that serves them to the node."""
    want = f"linux/{arch}"
    wheels = out / "wheelhouse"
    wheels.mkdir(parents=True, exist_ok=True)
    base = ["docker", "run", "--rm", "--platform", want, "--user", "0"]
    _must(
        run,
        base + ["-v", f"{wheels}:/wheelhouse", "--entrypoint", "python", python_image]
        + ["-m", "pip", "wheel", "--quiet", "--no-cache-dir", "--wheel-dir", "/wheelhouse"] + pin.split(),
        what=f"resolving {pin} into wheels",
    )
    _must(
        run,
        base + ["--network", "none", "-v", f"{wheels}:/wheelhouse:ro", "--entrypoint", "sh", python_image, "-c"]
        + [f"python -m venv /tmp/v && /tmp/v/bin/pip install --quiet --no-index --find-links /wheelhouse {pin}"],
        what=f"installing {pin} from the wheelhouse with the network off",
    )
    rel = image_file(img["ref"])
    (out / rel).parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        dockerfile = Path(tmp) / "Dockerfile"
        dockerfile.write_text(
            f"FROM {python_image}\n"
            "COPY wheelhouse /wheelhouse\n"
            'ENTRYPOINT ["python", "-m", "http.server", "8080", "--directory", "/wheelhouse"]\n'
        )
        _must(
            run,
            ["docker", "buildx", "build", "--platform", want, "-f", str(dockerfile), "-t", img["ref"]]
            + ["--output", f"type=docker,dest={out / rel}", str(out)],
            what=f"building {img['ref']}",
        )
    return {
        "ref": img["ref"], "file": rel, "sizes": img["sizes"], "group": img["group"],
        "origin": "built", "wheelhouse": True,
    }  # fmt: skip


def _package_charts(repo: Path, out: Path, version: str, sizes, run) -> dict:
    import yaml  # build side only

    charts = {}
    with tempfile.TemporaryDirectory() as tmp:
        # helm's own repository list is left alone: the cluster base's
        # dependencies are resolved against a list that lives for this build.
        env = {
            "HELM_REPOSITORY_CONFIG": str(Path(tmp) / "repositories.yaml"),
            "HELM_REPOSITORY_CACHE": str(Path(tmp) / "cache"),
        }
        cluster = repo / "infra" / "charts" / "axiom-cluster"
        deps = yaml.safe_load((cluster / "Chart.yaml").read_text()).get("dependencies") or []
        for n, url in enumerate(sorted({d["repository"] for d in deps})):
            _must(run, ["helm", "repo", "add", f"dep-{n}", url], env=env, what=f"adding the chart repository {url}")
        _must(run, ["helm", "dependency", "build", str(cluster)], env=env, what="fetching the cluster base's charts")
        for name in ("axiom-node", "axiom-cluster"):
            chart = repo / "infra" / "charts" / name
            dest = out / "charts"
            dest.mkdir(parents=True, exist_ok=True)
            _must(
                run,
                ["helm", "package", str(chart), "--version", version, "--destination", str(dest)],
                env=env,
                what=f"packaging {name}",
            )
            rel = f"charts/{name}-{version}.tgz"
            if not (out / rel).is_file():
                raise BundleError(f"helm package did not write {rel}")
            charts[name] = rel
            for size in sizes:
                values = out / "values" / name / f"values-{size}.yaml"
                values.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(chart / f"values-{size}.yaml", values)
    return charts


def build(
    decl: dict,
    repo: Path,
    out: Path,
    *,
    version: str,
    arch: str,
    sizes=None,
    groups=None,
    trusted_root: Path | None = None,
    run=_run,
    fetch=_fetch,
) -> dict:
    """Assemble the bundle in ``out`` and write its manifest. Needs a network,
    ``helm`` and ``docker``. The signature is added afterwards by whoever holds
    the signing identity (the release workflow); see ``verify``."""
    import yaml  # build side only

    _check_arch(arch)
    repo, out = Path(repo).resolve(), Path(out).resolve()
    sizes = list(sizes or decl["sizes"])
    if set(sizes) - set(decl["sizes"]):
        raise BundleError(f"sizes {sizes} are not all in the declaration's {decl['sizes']}")
    if out.exists() and any(out.iterdir()):
        raise BundleError(f"{out} is not empty")
    out.mkdir(parents=True, exist_ok=True)

    values = yaml.safe_load((repo / "infra" / "charts" / "axiom-node" / "values.yaml").read_text())
    packages = {"pin": values["packages"]["pin"], "version": str(values["packages"]["version"])}
    python = decl["python"]
    selected = images_for(decl, sizes, groups)
    wheel_images = [i for i in selected if i.get("wheelhouse")]
    for i in wheel_images:
        if i["ref"] != f"{python['wheelhouse_image']}:{packages['version']}":
            raise BundleError(f"{i['ref']} is not the wheelhouse for the chart's packages.version {packages['version']}")

    for d in downloads_for(decl, sizes):
        _download(d, arch, out, fetch)
    charts = _package_charts(repo, out, version, sizes, run)
    images = [_save_image(i, arch, repo, out, run) for i in selected if not i.get("wheelhouse")]
    for i in wheel_images:
        images.append(_wheelhouse(i, python["image"], packages["pin"], arch, out, run))
    if trusted_root:
        (out / TRUSTED_ROOT).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(trusted_root, out / TRUSTED_ROOT)
    shutil.copyfile(Path(__file__), out / INSTALLER)
    (out / "README.txt").write_text(
        f"Axiom offline install bundle {version}, linux/{arch}, sizes: {', '.join(sizes)}.\n\n"
        f"  python3 {INSTALLER} verify\n"
        f"  python3 {INSTALLER} install --size <{'|'.join(sizes)}> --dry-run\n"
        f"  python3 {INSTALLER} install --size <{'|'.join(sizes)}> -f my-values.yaml\n\n"
        "Nothing here needs a network. Docker must already be on the machine for the small size.\n"
    )
    meta = {
        "version": version,
        "arch": arch,
        "os": "linux",
        "sizes": sizes,
        "groups": sorted(_groups(decl, groups)),
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(os.environ.get("SOURCE_DATE_EPOCH", time.time())))),
        "packages": packages,
        "signing": dict(decl["signing"]),
        "charts": charts,
        "images": images,
        "tools": {d["name"]: d["version"] for d in downloads_for(decl, sizes)},
    }
    manifest = build_manifest(out, meta)
    write_manifest(out, manifest)
    return manifest


# --- install ----------------------------------------------------------------


def machine_arch() -> str:
    machine = platform.machine().lower()
    return {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine, machine)


def _helm_args(root: Path, manifest: dict, chart: str, size: str) -> list[str]:
    return [str(root / manifest["charts"][chart]), "-f", str(root / "values" / chart / f"values-{size}.yaml")]


def _node_args(root: Path, manifest: dict, size: str, values, sets) -> list[str]:
    args = _helm_args(root, manifest, "axiom-node", size)
    for v in values or []:
        args += ["-f", str(v)]
    for s in sets or []:
        args += ["--set", s]
    # Last, so they win: the wheelhouse holds exactly the bundle's pin.
    wheel = next((i["ref"] for i in manifest["images"] if i.get("wheelhouse")), None)
    if wheel:
        packages = manifest["packages"]
        args += ["--set", "packages.wheelhouse.enabled=true", "--set", f"packages.wheelhouse.image={wheel}"]
        args += ["--set-string", f"packages.pin={packages['pin']}", "--set-string", f"packages.version={packages['version']}"]
    return args


def install_steps(
    root: Path,
    manifest: dict,
    size: str,
    *,
    values=None,
    sets=None,
    release: str = "node",
    namespace: str = "axiom",
    cluster_name: str = "axiom",
    existing_cluster: bool = False,
    kubeconfig: str | None = None,
) -> list[dict]:
    """The commands an install runs, in order, as ``{what, argv, env, check}``.
    Every chart, image and binary is a file in the bundle."""
    root = Path(root)
    arch = manifest["arch"]
    helm = str(root / "tools" / "helm")
    images = [i for i in manifest["images"] if size in i["sizes"]]
    files = [str(root / i["file"]) for i in images if i.get("group") != "k3d"]
    airgap = root / "base" / f"k3s-airgap-images-{arch}.tar.zst"
    cluster_chart = _helm_args(root, manifest, "axiom-cluster", size)
    node_chart = _node_args(root, manifest, size, values, sets)
    steps: list[dict] = []

    def step(what, argv, env=None, check=None):
        steps.append({"what": what, "argv": [str(a) for a in argv], "env": dict(env or {}), "check": check})

    # Before anything changes: what would be installed names only bundled images.
    step("render the cluster base", [helm, "template", "base"] + cluster_chart + ["-n", "axiom-system"], check="images")
    step("render the node", [helm, "template", release] + node_chart + ["-n", namespace], check="images")

    if size == "medium":
        kube = kubeconfig or K3S_KUBECONFIG
        if not existing_cluster:
            step("install the k3s binary", ["install", "-m", "0755", root / "tools" / "k3s", "/usr/local/bin/k3s"])
            step("create k3s's images directory", ["mkdir", "-p", K3S_IMAGES_DIR])
            step("give k3s its system images", ["cp", airgap, K3S_IMAGES_DIR + "/"])
            step(
                "install k3s as a service, with no download",
                ["sh", root / "base" / "k3s-install.sh"],
                env={"INSTALL_K3S_SKIP_DOWNLOAD": "true"},
            )
            # `kubectl wait` fails at once while the node is not registered yet, so it is retried.
            ready = "k3s kubectl wait --for=condition=Ready node --all --timeout=10s >/dev/null 2>&1"
            step("wait for the node", ["sh", "-c", f"i=0; until {ready}; do i=$((i+1)); [ $i -lt 60 ] || exit 1; sleep 5; done"])
        for f in files:
            step(f"load {Path(f).name}", ["k3s", "ctr", "images", "import", f])
    else:
        kube = kubeconfig or str(Path.home() / ".kube" / f"k3d-{cluster_name}.yaml")
        k3d = str(root / "tools" / "k3d")
        helpers = [i for i in images if i.get("group") == "k3d"]
        node_image = next((i["ref"] for i in helpers if "/k3s:" in i["ref"]), None)
        if not existing_cluster:
            if not node_image:
                raise BundleError("the bundle holds no k3s node image for K3D; it was built without the k3d group")
            env = {}
            for i in helpers:
                step(f"load {Path(i['file']).name} into Docker", ["docker", "load", "-i", root / i["file"]])
                if "k3d-tools:" in i["ref"]:
                    env["K3D_IMAGE_TOOLS"] = i["ref"]
                if "k3d-proxy:" in i["ref"]:
                    env["K3D_IMAGE_LOADBALANCER"] = i["ref"]
            step(
                "create the K3D cluster from the bundled images",
                [k3d, "cluster", "create", cluster_name, "--image", node_image]
                + ["--volume", f"{airgap}:{K3S_IMAGES_DIR}/{airgap.name}", "--wait"]
                + ["--kubeconfig-update-default=false", "--kubeconfig-switch-context=false"],
                env=env,
            )
            step("write the cluster's kubeconfig", [k3d, "kubeconfig", "write", cluster_name, "--output", kube], env=env)
            if files:
                step("load the images into the cluster", [k3d, "image", "import", "-c", cluster_name] + files, env=env)
        elif files:
            step("load the images into the cluster", [k3d, "image", "import", "-c", cluster_name] + files)

    env = {"KUBECONFIG": kube}
    step(
        "install the cluster base",
        [helm, "upgrade", "--install", "base"] + cluster_chart
        + ["-n", "axiom-system", "--create-namespace", "--wait", "--timeout", "20m"],
        env=env,
    )
    step(
        "install the node",
        [helm, "upgrade", "--install", release] + node_chart + ["-n", namespace, "--create-namespace"],
        env=env,
    )
    return steps


def install(
    root: Path,
    *,
    size: str,
    run=_run,
    allow_unsigned: bool = False,
    identity: str | None = None,
    issuer: str | None = None,
    cosign: Path | None = None,
    trusted_root: Path | None = None,
    host_arch: str | None = None,
    dry_run: bool = False,
    out=None,
    **options,
) -> list[dict]:
    """Verify the bundle, then install from it. Nothing from the bundle is run
    before the manifest and the signature have been checked."""
    root = Path(root).resolve()
    say = lambda line: print(line, file=out or sys.stdout)  # noqa: E731
    manifest = verify_manifest(root)
    signed = verify_signature(
        root, manifest, run=run, allow_unsigned=allow_unsigned,
        identity=identity, issuer=issuer, cosign=cosign, trusted_root=trusted_root,
    )  # fmt: skip
    say(f"bundle {manifest['version']} linux/{manifest['arch']}: {len(manifest['files'])} files match the manifest; signature {signed}")
    if size not in manifest["sizes"]:
        raise BundleError(f"this bundle was built for {', '.join(manifest['sizes'])}, not {size}")
    here = host_arch or machine_arch()
    if not dry_run and here != manifest["arch"]:
        raise BundleError(f"this bundle is for {manifest['arch']}; this machine is {here}")
    steps = install_steps(root, manifest, size, **options)
    bundled = {normalize_ref(i["ref"]) for i in manifest["images"] if size in i["sizes"]}
    for tool in ("helm", "k3d", "k3s"):
        if (root / "tools" / tool).is_file():
            os.chmod(root / "tools" / tool, 0o755)
    for s in steps:
        shown = " ".join([f"{k}={v}" for k, v in s["env"].items()] + s["argv"])
        if dry_run:
            say(f"# {s['what']}\n{shown}")
            continue
        say(f"== {s['what']}")
        r = _must(run, s["argv"], env=s["env"], what=s["what"])
        if s["check"] == "images":
            lacking = image_refs_in(r.stdout or "") - bundled
            if lacking:
                raise BundleError(
                    "these values name images the bundle does not hold for this size, so their pods could "
                    f"never start here: {', '.join(sorted(lacking))}. Switch the role off, or build a bundle "
                    "with the group that holds them."
                )
    return steps


# --- command line -----------------------------------------------------------


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Build, verify and install the offline install bundle.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def build_side(p):
        p.add_argument("--declaration", type=Path, help="default: infra/bundle/offline-bundle.toml")
        p.add_argument("--repo", type=Path, help="the source tree (default: the one this file is in)")
        p.add_argument("--arch", required=True, choices=ARCHES)
        p.add_argument("--size", action="append", help="repeatable; default: every size the declaration serves")
        p.add_argument("--group", action="append", help="repeatable, adds to the default groups; `all` for every group")

    p = sub.add_parser("plan", help="list what a build would fetch; fetches nothing")
    build_side(p)
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("build", help="assemble a bundle and write its manifest (needs a network, helm, docker)")
    build_side(p)
    p.add_argument("--version", required=True, help="the release this bundle is for, without the leading v")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--trusted-root", type=Path, help="Sigstore trusted root to ship, for checking the signature offline")

    def verify_side(p):
        p.add_argument("bundle", type=Path, nargs="?", default=Path(__file__).resolve().parent)
        p.add_argument("--allow-unsigned", action="store_true", help="accept a bundle with no signature")
        p.add_argument("--identity", help="the signer to expect, instead of the one the manifest names")
        p.add_argument("--issuer")
        p.add_argument("--cosign", type=Path)
        p.add_argument("--trusted-root", type=Path)

    verify_side(sub.add_parser("verify", help="check a bundle against its manifest and signature"))
    p = sub.add_parser("install", help="verify, then install from the bundle with no network")
    verify_side(p)
    p.add_argument("--size", required=True, choices=("small", "medium"))
    p.add_argument("-f", "--values", action="append", default=[], help="your values, after the size's (repeatable)")
    p.add_argument("--set", dest="sets", action="append", default=[])
    p.add_argument("--release", default="node")
    p.add_argument("--namespace", default="axiom")
    p.add_argument("--cluster-name", default="axiom", help="the K3D cluster's name (small)")
    p.add_argument("--existing-cluster", action="store_true", help="the cluster is already there: load and install only")
    p.add_argument("--kubeconfig")
    p.add_argument("--dry-run", action="store_true", help="verify, then print the commands and run none")
    a = ap.parse_args(argv)

    try:
        if a.cmd in ("plan", "build"):
            repo = a.repo or _repo_root()
            decl = load_declaration(a.declaration or repo / "infra" / "bundle" / "offline-bundle.toml")
            groups = None
            if a.group:
                groups = "all" if "all" in a.group else sorted(set(default_groups(decl)) | set(a.group))
            if a.cmd == "plan":
                result = plan(decl, a.arch, a.size, groups)
                if a.json:
                    print(json.dumps(result, indent=2))
                else:
                    for d in result["downloads"]:
                        print(f"download  {d['name']} {d['version']}  {d['url']}")
                    for i in result["images"]:
                        print(f"image     [{i['group']}: {','.join(i['sizes'])}]  {i['ref']}")
                return 0
            manifest = build(
                decl, repo, a.out, version=a.version, arch=a.arch, sizes=a.size, groups=groups,
                trusted_root=a.trusted_root,
            )  # fmt: skip
            total = sum(f["size"] for f in manifest["files"])
            print(f"{a.out}: {len(manifest['files'])} files, {total / 1e9:.2f} GB; manifest written, not yet signed")
            return 0
        if a.cmd == "verify":
            manifest = verify_manifest(a.bundle)
            signed = verify_signature(
                a.bundle, manifest, allow_unsigned=a.allow_unsigned, identity=a.identity,
                issuer=a.issuer, cosign=a.cosign, trusted_root=a.trusted_root,
            )  # fmt: skip
            print(f"bundle {manifest['version']} linux/{manifest['arch']}: {len(manifest['files'])} files match the manifest; signature {signed}")
            return 0
        install(
            a.bundle, size=a.size, allow_unsigned=a.allow_unsigned, identity=a.identity, issuer=a.issuer,
            cosign=a.cosign, trusted_root=a.trusted_root, dry_run=a.dry_run, values=a.values, sets=a.sets,
            release=a.release, namespace=a.namespace, cluster_name=a.cluster_name,
            existing_cluster=a.existing_cluster, kubeconfig=a.kubeconfig,
        )  # fmt: skip
        return 0
    except BundleError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
