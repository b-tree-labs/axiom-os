# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi ext install <name>[@<version>]`` — registry -> local install.

Flow:

1. Resolve name/version via the registry backend.
2. Verify the ed25519 signature using ``axiom.cli.ext.signing``.
3. Unpack the tarball under ``$AXIOM_HOME/extensions/<name>-<version>/``.
4. Run ``<venv>/bin/pip install <unpacked_path>`` (skippable via
   ``--no-pip`` / ``AXIOM_INSTALL_NO_PIP=1`` — test seam only).
5. Record the install in ``$AXIOM_HOME/state.json``.

If anything after step 2 fails, we roll back both the install directory
and the state record so a subsequent ``axi ext list`` never shows a
broken install.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json as _json
import os
import shutil
import subprocess
import sys
import tarfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlparse

from axiom.cli.ext._output import console, error, next_steps, status
from axiom.cli.ext.commands.config import _axiom_home
from axiom.cli.ext.commands.install_batch import (
    BatchEntry,
    parse_requirements_file,
    resolve_version_spec,
)
from axiom.cli.ext.commands.show import parse_spec
from axiom.cli.ext.install_state import (
    InstallRecord,
    drop_install,
    get_installed,
    record_install,
)
from axiom.cli.ext.provider import CliContext
from axiom.cli.ext.registry_backend import (
    ArtifactRecord,
    RegistryPath,
    list_versions,
    read_index,
)
from axiom.cli.ext.registry_backend import (
    get as registry_get,
)
from axiom.cli.ext.signing import (
    default_public_key_path,
    load_public_key,
    trusted_keys_dir,
    verify_file,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def extensions_root() -> Path:
    """Return ``$AXIOM_HOME/extensions/`` — parent of all unpacked installs."""
    return _axiom_home() / "extensions"


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).isoformat(timespec="seconds")


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _resolve_version(name: str, version: str | None) -> str | None:
    if version is not None:
        return version
    idx = read_index()
    entry = (idx.get("extensions") or {}).get(name) or {}
    latest = entry.get("latest")
    return str(latest) if latest else None


def _verify_signature(record: ArtifactRecord) -> tuple[bool, str]:
    """Run the same verification logic ``show`` uses. Returns (ok, detail)."""
    if not record.sig_path.exists() or not record.artifact_path.exists():
        return False, "signature or artifact missing on disk"

    pub_path: Path | None = None
    sha = (record.attestation or {}).get("public_key_sha256")
    if sha:
        candidate = trusted_keys_dir() / f"{sha}.pub"
        if candidate.exists():
            pub_path = candidate
    if pub_path is None:
        default = default_public_key_path()
        if default.exists():
            pub_path = default
    if pub_path is None:
        return False, (
            "no public key available (no trusted-store pin, no self-trust key)"
        )

    try:
        pub = load_public_key(pub_path)
    except Exception as exc:  # noqa: BLE001
        return False, f"failed to load public key {pub_path}: {exc}"

    sig_hex = record.sig_path.read_text(encoding="utf-8").strip()
    ok = verify_file(pub, record.artifact_path, sig_hex)
    return ok, "verified" if ok else "signature does not match the artifact"


def _is_within(parent: Path, child: Path) -> bool:
    """Return True if ``child`` resolves inside ``parent``."""
    try:
        child_resolved = child.resolve()
        parent_resolved = parent.resolve()
        return str(child_resolved).startswith(str(parent_resolved) + os.sep) or (
            child_resolved == parent_resolved
        )
    except OSError:
        return False


def _safe_extract_tar(artifact: Path, target_dir: Path) -> None:
    """Extract ``artifact`` into ``target_dir`` refusing path traversal.

    We validate every member's resolved path lies inside ``target_dir``.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(artifact, "r:gz") as tar:
        for member in tar.getmembers():
            dest = (target_dir / member.name).resolve()
            if not _is_within(target_dir, dest):
                raise RuntimeError(
                    f"refusing to extract path-escaping member {member.name!r}"
                )
        # Some Pythons warn about extraction security; pass filter='data'
        # when available (3.12+) for the safe default.
        try:
            tar.extractall(target_dir, filter="data")  # type: ignore[arg-type]
        except TypeError:
            tar.extractall(target_dir)


@dataclass
class InstallPlan:
    """Resolved plan for an install — surfaces the key decisions up front."""

    name: str
    version: str
    registry_url: str
    install_path: Path
    replacing: InstallRecord | None = None


# ---------------------------------------------------------------------------
# Core flow
# ---------------------------------------------------------------------------


def _pip_install(path: Path, *, announce) -> tuple[int, str]:
    """Run ``<venv>/bin/pip install <path>`` and capture output.

    Returns ``(returncode, combined_output)``. Never raises: the caller
    decides rollback policy.
    """
    pip_bin = Path(sys.executable).parent / "pip"
    cmd = [str(pip_bin), "install", str(path)]
    announce(f"$ {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return 1, f"pip invocation failed: {exc}"
    output = (result.stdout or "") + (result.stderr or "")
    return result.returncode, output


def _pip_skipped() -> bool:
    return bool(os.environ.get("AXIOM_INSTALL_NO_PIP"))


def install_extension(
    name: str,
    *,
    version: str | None = None,
    force: bool = False,
    no_pip: bool = False,
    dry_run: bool = False,
    announce=None,
) -> InstallRecord | InstallPlan:
    """Install an extension from the registry.

    Returns the :class:`InstallRecord` on success, or an :class:`InstallPlan`
    when ``dry_run=True``. Raises :class:`RuntimeError` on failure with a
    descriptive message.
    """
    announce = announce or (lambda msg: None)

    resolved_version = _resolve_version(name, version)
    if resolved_version is None:
        raise RuntimeError(
            f"{name} not found in the registry; "
            "run `axi ext search <query>` to find extensions."
        )

    record = registry_get(name, resolved_version)
    if record is None:
        raise RuntimeError(
            f"{name}@{resolved_version} not found in the registry; "
            "run `axi ext search <query>` to find extensions."
        )

    registry_url = f"file://{RegistryPath.resolve().root}"
    install_path = extensions_root() / f"{name}-{resolved_version}"

    existing = get_installed(name)
    replacing: InstallRecord | None = None
    if existing is not None:
        if existing.version == resolved_version and not force:
            raise RuntimeError(
                f"{name} {resolved_version} is already installed at "
                f"{existing.install_path}; use `axi ext update` to refresh "
                "or pass --force to re-install the same version."
            )
        replacing = existing

    if dry_run:
        return InstallPlan(
            name=name,
            version=resolved_version,
            registry_url=registry_url,
            install_path=install_path,
            replacing=replacing,
        )

    # -- Signature verification -------------------------------------------
    ok, detail = _verify_signature(record)
    announce(f"signature: {detail}")
    if not ok:
        raise RuntimeError(
            f"signature verification failed for {name} {resolved_version}: "
            f"{detail}; refusing to install."
        )

    # -- Drop the older install if we're replacing it ---------------------
    if replacing is not None:
        old_path = Path(replacing.install_path)
        if old_path.exists():
            shutil.rmtree(old_path, ignore_errors=True)
        drop_install(name)

    # -- If same-version exists (force), wipe the directory first. --------
    if install_path.exists():
        shutil.rmtree(install_path)

    # -- Unpack -----------------------------------------------------------
    try:
        _safe_extract_tar(record.artifact_path, install_path)
    except Exception as exc:
        if install_path.exists():
            shutil.rmtree(install_path, ignore_errors=True)
        raise RuntimeError(f"failed to unpack {record.artifact_path}: {exc}")

    # -- Hashes for the state record --------------------------------------
    artifact_sha = _hash_file(record.artifact_path)
    signature_sha = _hash_file(record.sig_path) if record.sig_path.exists() else ""

    new_record = InstallRecord(
        name=name,
        version=resolved_version,
        installed_at=_utc_now_iso(),
        install_path=str(install_path),
        artifact_sha256=artifact_sha,
        signature_sha256=signature_sha,
        registry_url=registry_url,
    )
    record_install(new_record)

    # -- pip install ------------------------------------------------------
    if no_pip or _pip_skipped():
        announce("pip: skipped (AXIOM_INSTALL_NO_PIP or --no-pip)")
    else:
        # The tarball unpacks as <name>-<version>/<name>-<version>/... when
        # built by ``axi ext sign``. Look one level in for pyproject if the
        # install_path doesn't itself contain one.
        pip_target = install_path
        nested = install_path / f"{name}-{resolved_version}"
        if not (install_path / "pyproject.toml").exists() and (
            nested / "pyproject.toml"
        ).exists():
            pip_target = nested

        rc, output = _pip_install(pip_target, announce=announce)
        if rc != 0:
            # Roll back: drop state + wipe dir.
            drop_install(name)
            if install_path.exists():
                shutil.rmtree(install_path, ignore_errors=True)
            raise RuntimeError(
                f"pip install failed (exit {rc}); rolled back install. "
                f"Output (tail):\n{output[-1000:]}"
            )

    return new_record


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class InstallProvider:
    """Built-in provider for ``axi ext install <name>[@<version>]``."""

    verb = "install"
    description = "Install an extension from the registry"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        # ``spec`` is optional when -r is used instead.
        parser.add_argument(
            "spec",
            nargs="?",
            default=None,
            help="Extension name, optionally with @<version>",
        )
        parser.add_argument(
            "-r",
            "--requirements",
            dest="requirements",
            default=None,
            help="Install every extension listed in this requirements file",
        )
        parser.add_argument(
            "--from-url",
            dest="from_url",
            default=None,
            help=(
                "Install directly from a file:// artifact URL "
                "(https:// is plumbed but not yet supported)"
            ),
        )
        parser.add_argument(
            "--version",
            default=None,
            help="Pin a specific version (alternative to @<version>)",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-install over an existing same-version install",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print what would happen; don't touch disk or pip",
        )
        parser.add_argument(
            "--registry",
            dest="registry_override",
            default=None,
            help="Override the registry URL (file:// only at v0.1)",
        )
        # Hidden — test seam only. Real users never pass this.
        parser.add_argument(
            "--no-pip",
            action="store_true",
            help=argparse.SUPPRESS,
        )

    def run(self, args: argparse.Namespace, context: CliContext) -> int:
        if args.registry_override is not None:
            if not args.registry_override.startswith("file://"):
                print(
                    f"axi ext install: --registry must use the file:// scheme "
                    f"(got {args.registry_override!r})."
                )
                return 1
            os.environ["AXIOM_REGISTRY_URL"] = args.registry_override

        # -- batch flow via -r --------------------------------------------
        if args.requirements is not None:
            return _run_requirements(args)

        # -- direct-url flow via --from-url -------------------------------
        if args.from_url is not None:
            return _run_from_url(args)

        if args.spec is None:
            error(
                "axi ext install: missing extension name",
                hint="pass a name (`axi ext install <name>`) or use -r <reqs>",
            )
            return 2

        try:
            name, spec_version = parse_spec(args.spec)
        except ValueError as exc:
            print(f"axi ext install: {exc}")
            return 2
        version = args.version or spec_version

        def _announce(msg: str) -> None:
            print(msg)

        try:
            result = install_extension(
                name,
                version=version,
                force=args.force,
                no_pip=args.no_pip,
                dry_run=args.dry_run,
                announce=_announce,
            )
        except RuntimeError as exc:
            print(f"axi ext install: {exc}")
            return 1

        if args.dry_run:
            assert isinstance(result, InstallPlan)
            print(
                f"dry-run: would install {result.name} {result.version} to "
                f"{result.install_path} (registry: {result.registry_url})"
            )
            if result.replacing is not None:
                print(
                    f"  would replace: {result.replacing.name} "
                    f"{result.replacing.version} at "
                    f"{result.replacing.install_path}"
                )
            return 0

        assert isinstance(result, InstallRecord)
        con = console()
        con.print(
            f"Installed {result.name} {result.version} from "
            f"{result.registry_url} (signature verified)"
        )
        con.print("")
        next_steps(
            [
                "axi ext list                 # Verify it's there",
                f"axi ext run {result.name}          # Run its default cmd",
            ]
        )
        return 0


def _run_from_url(args: argparse.Namespace) -> int:
    """Install directly from a URL (file:// today, https:// reserved)."""
    parsed = urlparse(args.from_url)
    scheme = parsed.scheme.lower()
    con = console()

    if scheme == "https":
        error(
            "axi ext install: scheme 'https' not yet supported (pending "
            "remote registry work)",
            hint="use file://<path> for v0.1",
        )
        return 2
    if scheme != "file":
        error(
            f"axi ext install: unsupported --from-url scheme {scheme!r}",
            hint="only file:// and https:// are recognized at v0.1",
        )
        return 2

    artifact_path = Path(unquote(parsed.path))
    # Allow the user to pass the containing directory too; in that case look
    # for the single .tar.gz.
    if artifact_path.is_dir():
        tarballs = sorted(artifact_path.glob("*.tar.gz"))
        if len(tarballs) != 1:
            error(
                f"axi ext install: expected exactly one .tar.gz in {artifact_path}, "
                f"found {len(tarballs)}",
                hint="point --from-url at the specific .tar.gz",
            )
            return 2
        artifact_path = tarballs[0]

    if not artifact_path.exists():
        error(
            f"axi ext install: artifact not found: {artifact_path}",
            hint="check the URL path",
        )
        return 2

    # Auto-create $AXIOM_HOME if missing — narrate the create, don't require
    # the user to run a separate setup step.
    home = _axiom_home()
    if not home.exists():
        home.mkdir(parents=True, exist_ok=True)
        status("info", "axiom_home", f"created $AXIOM_HOME at {home}/")

    # Locate the companion files. First prefer siblings next to the artifact;
    # also accept the "registry layout" where attestation.json sits next to
    # the .tar.gz under <name>/<version>/.
    stem = artifact_path.name
    if stem.endswith(".tar.gz"):
        base = stem[: -len(".tar.gz")]
    else:
        base = artifact_path.stem

    sig_path = artifact_path.with_name(f"{base}.tar.gz.sig")
    if not sig_path.exists():
        error(
            f"axi ext install: signature not found for {artifact_path.name}",
            hint=f"expected {sig_path.name} next to the artifact",
        )
        return 2

    # Attestation: try the two known filenames.
    attestation_path: Path | None = None
    candidates = [
        artifact_path.with_name(f"{base}.attestation.json"),
        artifact_path.with_name("attestation.json"),
    ]
    for c in candidates:
        if c.exists():
            attestation_path = c
            break

    attestation: dict = {}
    if attestation_path is not None:
        try:
            attestation = _json.loads(attestation_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            attestation = {}

    status("info", "fetched", str(artifact_path))

    # Build an ArtifactRecord-shaped view so we can reuse _verify_signature.
    from axiom.cli.ext.registry_backend import ArtifactRecord
    record = ArtifactRecord(
        name=base.rsplit("-", 1)[0] if "-" in base else base,
        version=base.rsplit("-", 1)[1] if "-" in base else "0.0.0",
        manifest_path=artifact_path.parent / "manifest.toml",
        artifact_path=artifact_path,
        sig_path=sig_path,
        attestation=attestation,
    )
    ok, detail = _verify_signature(record)
    if not ok:
        error(f"axi ext install: signature verification failed ({detail})")
        return 1
    status("pass", "verified", "signature matches artifact")

    # Unpack + peek at the manifest to confirm name/version.
    staging = home / "__from_url_stage" / base
    if staging.exists():
        shutil.rmtree(staging)
    try:
        _safe_extract_tar(artifact_path, staging)
    except Exception as exc:
        error(f"axi ext install: failed to unpack {artifact_path}: {exc}")
        shutil.rmtree(staging, ignore_errors=True)
        return 1
    status("info", "unpacked", str(staging))

    # Find the manifest inside the unpacked tree — it may be at the root or
    # one level in (the standard ``axi ext sign`` tarball uses a nested dir).
    manifest_candidates = [
        staging / "axiom-extension.toml",
        *staging.glob("*/axiom-extension.toml"),
    ]
    manifest_file = next((p for p in manifest_candidates if p.exists()), None)
    if manifest_file is None:
        error(
            f"axi ext install: no axiom-extension.toml found in {artifact_path.name}",
            hint="the archive does not look like an AEOS extension",
        )
        shutil.rmtree(staging, ignore_errors=True)
        return 1
    try:
        with manifest_file.open("rb") as fh:
            manifest = tomllib.load(fh)
    except Exception as exc:  # noqa: BLE001
        error(f"axi ext install: failed to parse manifest: {exc}")
        shutil.rmtree(staging, ignore_errors=True)
        return 1
    ext_block = manifest.get("extension") or {}
    real_name = ext_block.get("name")
    real_version = ext_block.get("version")
    if not real_name or not real_version:
        error(
            "axi ext install: manifest missing name/version",
            hint="the archive looks corrupted",
        )
        shutil.rmtree(staging, ignore_errors=True)
        return 1

    # Move the staged tree to the permanent install location.
    install_path = extensions_root() / f"{real_name}-{real_version}"
    existing = get_installed(real_name)
    if existing is not None:
        if existing.version == real_version and not args.force:
            error(
                f"axi ext install: {real_name} {real_version} already installed at "
                f"{existing.install_path}",
                hint="pass --force to re-install, or use `axi ext update`",
            )
            shutil.rmtree(staging, ignore_errors=True)
            return 1
        if Path(existing.install_path).exists():
            shutil.rmtree(existing.install_path, ignore_errors=True)
        drop_install(real_name)
    if install_path.exists():
        shutil.rmtree(install_path)
    install_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(staging), str(install_path))
    # If we left any empty staging parents behind, clean them up.
    try:
        staging.parent.rmdir()
    except OSError:
        pass

    artifact_sha = _hash_file(artifact_path)
    signature_sha = _hash_file(sig_path)
    new_record = InstallRecord(
        name=real_name,
        version=real_version,
        installed_at=_utc_now_iso(),
        install_path=str(install_path),
        artifact_sha256=artifact_sha,
        signature_sha256=signature_sha,
        registry_url=args.from_url,
    )
    record_install(new_record)

    # pip install (respecting --no-pip / env var).
    if args.no_pip or _pip_skipped():
        status("info", "pip", "skipped (AXIOM_INSTALL_NO_PIP or --no-pip)")
    else:
        pip_target = install_path
        nested = install_path / f"{real_name}-{real_version}"
        if not (install_path / "pyproject.toml").exists() and (
            nested / "pyproject.toml"
        ).exists():
            pip_target = nested
        rc, output = _pip_install(pip_target, announce=lambda m: None)
        if rc != 0:
            drop_install(real_name)
            shutil.rmtree(install_path, ignore_errors=True)
            error(
                f"axi ext install: pip install failed (exit {rc})",
                hint=f"tail: {output[-500:]}",
            )
            return 1
        status("pass", "pip installed", f"{real_name} {real_version}")

    con.print("")
    con.print(
        f"Installed {real_name} {real_version} from {args.from_url} "
        "(signature verified)"
    )
    con.print("")
    next_steps(
        [
            "axi ext list                 # Verify it's there",
            f"axi ext run {real_name}          # Run its default cmd",
        ]
    )
    return 0


def _run_requirements(args: argparse.Namespace) -> int:
    """Handle ``axi ext install -r <reqs>``.

    Parses the file, resolves versions, installs each entry, and prints a
    single batch summary at the end. Per-entry failures are captured and
    reported without aborting the batch.
    """
    reqs_path = Path(args.requirements)
    if not reqs_path.exists():
        error(
            f"axi ext install: requirements file not found: {reqs_path}",
            hint="check the path (`-r <reqs.txt>`) or create the file",
        )
        return 2
    try:
        entries = parse_requirements_file(reqs_path)
    except ValueError as exc:
        error(f"axi ext install: {exc}")
        return 2

    con = console()

    # Resolve versions first so --dry-run can show the plan.
    resolved: list[tuple[BatchEntry, str | None]] = []
    for entry in entries:
        versions = list_versions(entry.name)
        resolved_version = resolve_version_spec(entry.spec, versions)
        resolved.append((entry, resolved_version))

    if args.dry_run:
        for entry, version in resolved:
            if version is None:
                status("fail", entry.name, f"no match for spec {entry.spec or '(any)'}")
            else:
                status(
                    "info",
                    entry.name,
                    f"would install {version} (spec {entry.spec or '(any)'})",
                )
        con.print("")
        con.print(f"dry-run: {len(resolved)} entry(ies) in plan")
        return 0

    announce = lambda msg: None  # noqa: E731 — quiet sub-install noise
    installed = 0
    failed = 0
    skipped = 0
    errors: list[tuple[str, str]] = []

    for entry, version in resolved:
        if version is None:
            failed += 1
            errors.append(
                (entry.name, f"no registry version matched spec {entry.spec or '(any)'}")
            )
            status("fail", entry.name, errors[-1][1])
            continue
        existing = get_installed(entry.name)
        if existing is not None and existing.version == version and not args.force:
            skipped += 1
            status("info", entry.name, f"already at {version}; skipped")
            continue
        try:
            install_extension(
                entry.name,
                version=version,
                force=args.force,
                no_pip=args.no_pip,
                dry_run=False,
                announce=announce,
            )
        except RuntimeError as exc:
            failed += 1
            errors.append((entry.name, str(exc)))
            status("fail", entry.name, str(exc))
            continue
        installed += 1
        status("pass", entry.name, f"installed {version}")

    con.print("")
    con.print(
        f"batch: {installed} installed, {failed} failed, "
        f"{skipped} skipped (already installed)"
    )
    return 0 if failed == 0 else 1


__all__ = [
    "InstallPlan",
    "InstallProvider",
    "extensions_root",
    "install_extension",
]
