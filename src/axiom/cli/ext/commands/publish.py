# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""``axi ext publish`` — author-side: build + scan + validate + sign + register.

The publish verb is the union of Phase 3's earlier units:

1. :mod:`axi ext scan` (Unit 2) — baseline policy gate.
2. :mod:`axi ext validate` (existing Tier 2 verb) — manifest ↔ pyproject
   consistency + public API imports.
3. :mod:`axi ext sign` (Unit 3) — build the tarball + ed25519 signature +
   attestation.
4. :mod:`axi ext registry_backend` (Unit 1) — land the artifact in
   ``$AXIOM_HOME/registry/`` (or an override).

Refuses to publish if:

- The manifest's ``version`` does not match any git tag — hard fail unless
  ``--no-tag-check``. Silently skipped when the extension is not in a git
  worktree.
- ``scan`` reports a hard failure (or warnings under ``--strict-scan``).
- ``validate`` fails.
- The ``(name, version)`` is already in the registry (unless
  ``--allow-overwrite``).

``--dry-run`` runs build + scan + sign but never writes to the registry.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from axiom.cli.ext._output import console, next_steps
from axiom.cli.ext.commands.scan import scan_extension
from axiom.cli.ext.commands.sign import sign_artifact
from axiom.cli.ext.commands.validate import run_standard_tests, validate_extension
from axiom.cli.ext.provider import CliContext
from axiom.cli.ext.registry_backend import (
    ArtifactRecord,
    RegistryPath,
    get,
    put,
)


def _brand_cli() -> str:
    """The command the operator actually typed.

    These lines said "axi" unconditionally, so a consumer distribution's CLI
    told the operator to run a command that does not exist on their machine.
    """
    try:
        from axiom.infra.branding import get_branding

        return get_branding().cli_name
    except Exception:  # noqa: BLE001
        return "axi"


def _load_manifest(ext_path: Path) -> dict:
    manifest_path = ext_path / "axiom-extension.toml"
    with manifest_path.open("rb") as fh:
        return tomllib.load(fh)


def _in_git_repo(ext_path: Path) -> bool:
    from axiom.infra.git import safe_git_env
    if not (ext_path / ".git").exists():
        # Could still be in a parent git dir. Ask git directly.
        try:
            result = subprocess.run(
                ["git", "-C", str(ext_path), "rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                text=True,
                timeout=5,
                env=safe_git_env(ext_path),
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0 and result.stdout.strip() == "true"
    return True


def _git_tags(ext_path: Path) -> list[str]:
    from axiom.infra.git import safe_git_env
    try:
        result = subprocess.run(
            ["git", "-C", str(ext_path), "tag", "-l"],
            capture_output=True,
            text=True,
            timeout=5,
            env=safe_git_env(ext_path),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if result.returncode != 0:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _tag_matches_version(tags: list[str], version: str) -> bool:
    """Return True if any tag equals the version or ``v<version>``."""
    candidates = {version, f"v{version}"}
    return any(t in candidates for t in tags)


@dataclass(frozen=True)
class PublishCheck:
    """Whether a consumer can resolve what was just published."""

    verified: bool
    checked: bool
    detail: str


def verify_published(name: str, version: str, *, get=None) -> PublishCheck:
    """Confirm the published extension resolves the way a consumer resolves it.

    `put()` returning is not a consumer being able to install. An index can be
    written and not flushed; a record can point at a path that was cleaned up;
    a version can be normalised on write and not on read. In each case the
    publisher sees success and the next person to install sees nothing, which
    is a gap found at their expense rather than ours.

    `get()` is the consumer's own read path, so using it turns "the registry
    accepted this" into "a consumer can resolve this".

    Never raises: verification runs after a successful publish, and a publish
    that failed because its audit failed would be the worse bug.
    """
    if get is None:
        from axiom.cli.ext.registry_backend import get as _get

        get = _get

    try:
        record = get(name, version)
    except Exception as exc:  # noqa: BLE001 - auditing must not undo the publish
        return PublishCheck(
            verified=False, checked=False,
            detail=f"could not read the registry back to confirm: {exc}",
        )

    if record is None:
        return PublishCheck(
            verified=False, checked=True,
            detail=(
                f"the registry cannot resolve {name} {version} immediately after "
                "publishing it — a consumer installing now would not find it"
            ),
        )

    artifact = getattr(record, "artifact_path", None)
    if artifact is not None and not Path(artifact).exists():
        return PublishCheck(
            verified=False, checked=True,
            detail=(
                f"{name} {version} resolves, but its artifact is missing at "
                f"{artifact} — a consumer would fail at install rather than here"
            ),
        )

    return PublishCheck(
        verified=True, checked=True,
        detail=f"{name} {version} resolves and its artifact is present",
    )

def publish_extension(
    ext_path: Path,
    *,
    registry_override: str | None = None,
    strict_scan: bool = False,
    allow_overwrite: bool = False,
    skip_tag_check: bool = False,
    strict_tag_check: bool = False,
    dry_run: bool = False,
    yes: bool = False,
    announce=None,
    prompt=None,
) -> ArtifactRecord:
    """End-to-end publish.

    Returns the :class:`ArtifactRecord` of the published version. Raises on
    failure with a descriptive message the caller should surface as-is.
    ``announce`` and ``prompt`` are hooks for the CLI layer — tests drive
    the flow through this function directly.
    """
    announce = announce or (lambda msg: None)
    prompt = prompt or (lambda msg: "n")

    # -- Env override for the registry (also validated here). ---------------
    if registry_override is not None:
        if not registry_override.startswith("file://"):
            raise ValueError(
                f"--registry must use the file:// scheme "
                f"(got {registry_override!r}); remote registries are a later "
                "provider override."
            )
        os.environ["AXIOM_REGISTRY_URL"] = registry_override

    manifest = _load_manifest(ext_path)
    ext_block = manifest.get("extension", {})
    name = ext_block.get("name") or ext_path.name
    version = ext_block.get("version", "0.0.0")

    # -- Tag check ----------------------------------------------------------
    # Default behaviour is a warning (early-dev flows rarely have a tag
    # pinned to the manifest version). ``--strict-tag-check`` opts back in
    # to the old fail-hard behaviour. ``--no-tag-check`` / ``skip_tag_check``
    # is honored for back-compat but no longer required in the common case.
    if _in_git_repo(ext_path):
        tags = _git_tags(ext_path)
        if not _tag_matches_version(tags, version):
            if strict_tag_check:
                raise RuntimeError(
                    f"manifest version {version!r} has no matching git tag "
                    f"(looked for {version!r} or 'v{version}'); tag the "
                    "release with `git tag v<version>` or drop "
                    "--strict-tag-check"
                )
            if not skip_tag_check:
                announce(
                    f"warning: no git tag matches manifest version {version!r} "
                    f"(looked for {version!r} or 'v{version}'); publishing "
                    "anyway — pass --strict-tag-check to block."
                )

    # -- Overwrite guard ----------------------------------------------------
    existing = get(name, version)
    if existing is not None and not allow_overwrite:
        raise RuntimeError(
            f"{name} {version} is already published at {existing.artifact_path}; "
            "pass --allow-overwrite to replace (re-publishing a released "
            "version is strongly discouraged outside dev workflows)"
        )

    # -- Scan ---------------------------------------------------------------
    scan_result = scan_extension(ext_path)
    if scan_result.hard_failure:
        failed = [c for c in scan_result.checks if c.severity == "fail"]
        summary = "; ".join(f"{c.check}: {c.detail}" for c in failed[:3])
        raise RuntimeError(f"scan hard failure — publish blocked. {summary}")
    if strict_scan and scan_result.has_warnings:
        warned = [c for c in scan_result.checks if c.severity == "warn"]
        summary = "; ".join(f"{c.check}: {c.detail}" for c in warned[:3])
        raise RuntimeError(
            f"scan warnings present under --strict-scan — publish blocked. {summary}"
        )
    announce(f"scan: {len(scan_result.checks)} check(s) — ok")

    # -- Validate -----------------------------------------------------------
    validate_results = validate_extension(ext_path)
    # run_standard_tests is called separately by `axi ext validate`; reuse it
    # here so we don't ship an extension whose own standard tests fail.
    validate_results.append(run_standard_tests(ext_path))
    failed = [r for r in validate_results if not r.ok]
    if failed:
        summary = "; ".join(f"{r.check}: {r.detail}" for r in failed[:3])
        raise RuntimeError(f"validate failed — publish blocked. {summary}")
    announce(f"validate: {len(validate_results)} check(s) — ok")

    # -- Sign (build + sign + attestation) ----------------------------------
    sign_result = sign_artifact(
        ext_path,
        yes=yes,
        announce=announce,
        prompt=prompt,
    )
    announce(f"sign: {sign_result['artifact'].name}")

    if dry_run:
        announce(
            f"dry-run: would publish {name} {version} to "
            f"{RegistryPath.resolve().root}"
        )
        return ArtifactRecord(
            name=name,
            version=version,
            manifest_path=ext_path / "axiom-extension.toml",
            artifact_path=sign_result["artifact"],
            sig_path=sign_result["signature"],
            attestation=(
                sign_result.get("manifest", {})
            ),
        )

    # -- Register -----------------------------------------------------------
    record = put(
        name,
        version,
        ext_path / "axiom-extension.toml",
        sign_result["artifact"],
        sign_result["signature"],
        sign_result.get("manifest") or {},
    )
    # "Published" is taken to mean a consumer can install it, so confirm that
    # rather than announcing on the strength of put() having returned.
    check = verify_published(name, version)
    if check.verified:
        announce(
            f"Published {name} {version} to file://{RegistryPath.resolve().root}"
        )
    elif check.checked:
        raise RuntimeError(
            f"publish did not take: {check.detail}. The registry write "
            "returned without error, so this needs looking at rather than "
            "retrying."
        )
    else:
        # Unverified is not failed. The publish may well be fine; what is not
        # fine is saying so without having looked.
        announce(
            f"Published {name} {version} to file://{RegistryPath.resolve().root} "
            f"— UNCONFIRMED: {check.detail}"
        )
    return record


class PublishProvider:
    """Built-in provider for ``axi ext publish [<path>]``."""

    verb = "publish"
    description = "Author-side: build + scan + sign + register"

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "path",
            nargs="?",
            default=None,
            help="Path to the extension (default: current working directory)",
        )
        parser.add_argument(
            "--yes",
            action="store_true",
            help="Auto-generate the signing key on first use without prompting",
        )
        parser.add_argument(
            "--strict-scan",
            action="store_true",
            help="Treat scan warnings as failures",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Build + scan + sign, but do not write to the registry",
        )
        parser.add_argument(
            "--registry",
            dest="registry_override",
            default=None,
            help="Override the registry URL (file:// only at v0.1)",
        )
        parser.add_argument(
            "--allow-overwrite",
            action="store_true",
            help=(
                "Permit re-publishing an existing (name, version) in the "
                "registry. Strongly discouraged outside dev workflows."
            ),
        )
        parser.add_argument(
            "--strict-tag-check",
            action="store_true",
            help=(
                "Require the manifest version to match a git tag "
                "(default: warn-only; use this for release builds)"
            ),
        )
        parser.add_argument(
            "--no-tag-check",
            action="store_true",
            help=argparse.SUPPRESS,
        )

    def run(self, args: argparse.Namespace, context: CliContext) -> int:
        target = Path(args.path).resolve() if args.path else context.cwd
        con = console()
        if not (target / "axiom-extension.toml").exists():
            con.print(
                f"{_brand_cli()} ext publish: {target} does not look like an extension "
                "(no axiom-extension.toml); aborting"
            )
            return 1

        def _announce(msg: str) -> None:
            con.print(msg)

        def _prompt(msg: str) -> str:
            try:
                return input(msg)
            except EOFError:
                return ""

        if args.no_tag_check:
            # Deprecated: the default behaviour now matches --no-tag-check.
            # Tell the user once and move on so existing scripts don't break.
            con.print(
                f"{_brand_cli()} ext publish: --no-tag-check is deprecated — the default "
                "behaviour is already warn-only. Pass --strict-tag-check to "
                "restore the old fail-hard check."
            )

        try:
            record = publish_extension(
                target,
                registry_override=args.registry_override,
                strict_scan=args.strict_scan,
                allow_overwrite=args.allow_overwrite,
                skip_tag_check=args.no_tag_check,
                strict_tag_check=args.strict_tag_check,
                dry_run=args.dry_run,
                yes=args.yes,
                announce=_announce,
                prompt=_prompt,
            )
        except ValueError as exc:
            con.print(f"{_brand_cli()} ext publish: {exc}")
            return 1
        except RuntimeError as exc:
            con.print(f"{_brand_cli()} ext publish: {exc}")
            return 1
        except FileNotFoundError as exc:
            con.print(f"{_brand_cli()} ext publish: {exc}")
            return 1

        if not args.dry_run:
            con.print("")
            next_steps(
                [
                    f"{_brand_cli()} ext show {record.name}          # Confirm the registry entry",
                    f"{_brand_cli()} ext install {record.name}       # Validate consumer install",
                ]
            )
        return 0


__all__ = ["PublishProvider", "publish_extension"]
