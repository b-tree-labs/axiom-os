# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The cheap guards, defined once (LOOP-S2, LOOP-S3; ADR-257 D7).

What cannot be taken back once it leaves the machine is checked before the push and
again by the server. These checks take seconds and need no database:

- **root-dirs**: no unexpected directory at the repository root;
- **public-surface**: no forbidden term on the surface the public mirror publishes;
- **naming**: the platform names no customer and learns no consumer's vocabulary;
- **adr-numbers**: every ADR number is unique;
- **secrets**: no credential format in the lines being pushed.

The pre-push hook and ``loop-guards.yml`` both run ``axi release guards``, so the hook
and the server cannot drift: a push that skipped the hook (``--no-verify``, a web
edit, an agent without the hook) is caught on the server, and a red guards run on main
is a required check failing, which the automatic revert treats like any other.

The pytest guards run with ``--noconftest`` and plugin autoload off: they need only
``pytest`` and the source tree, so a bare server runner and a laptop run the very same
command. A guard that cannot run blocks; it never passes silently.

The run also computes the change's tier from the tier map (``tiers.py``) and reports
it. Nothing is refused for the tier in this phase.

The secret scan reads only the added lines in ``base..head``, so a fixture already on
main is not re-reported, and it never prints the matched text. A test fixture line that
must look like a credential carries ``secret-scan: allow``.

The server does not repeat what the hook did. A hook run that passes every guard
(``--stamp``) writes a git note on the head, ``Guards-Passed: <tree> <guards-version>``,
under ``refs/notes/axiom-guards``, and pushes it. ``loop-guards.yml`` asks
``python -m axiom.extensions.builtins.release.guards --stamped <sha>`` before installing
anything, and skips the guards when that commit's tree carries a stamp for the guards it
has now. Every other push runs them: no stamp (the hook was skipped, or GitHub made a
commit whose tree nobody stamped) or a stamp for other guards. A forged stamp gains
nothing: the automatic revert still judges main's real CI.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

from axiom.extensions.builtins.release.tiers import ChangeTier, load_tier_map

ALLOW_MARK = "secret-scan: allow"
#: Where a hook's pass is stamped, one note per stamped commit.
NOTES_REF = "refs/notes/axiom-guards"
STAMP_PREFIX = "Guards-Passed:"
_ZERO = "0" * 40

Runner = Callable[..., subprocess.CompletedProcess]


@dataclass(frozen=True)
class Guard:
    id: str
    title: str
    kind: str  # "pytest" | "script" | "secret-scan"
    targets: tuple[str, ...]
    why: str


GUARDS: tuple[Guard, ...] = (
    Guard(
        "root-dirs",
        "no unexpected root directory",
        "pytest",
        ("tests/test_repo_hygiene.py::TestRootDirPolicy::test_no_unexpected_root_dirs",),
        "a stray root directory ships in the public mirror and the wheel",
    ),
    Guard(
        "public-surface",
        "no forbidden term on the public surface",
        "pytest",
        ("tests/test_mirror.py::test_public_surface_has_no_forbidden_terms",),
        "the public mirror cannot be recalled once published",
    ),
    Guard(
        "naming",
        "the platform names no customer and no consumer vocabulary",
        "pytest",
        ("tests/test_axiom_names_no_domain.py",),
        "a customer or deployment id in the platform cannot be recalled once pushed",
    ),
    Guard(
        "adr-numbers",
        "ADR numbers are unique",
        "script",
        ("scripts/lint_adr_numbers.py",),
        "an ADR number is an identifier other documents cite",
    ),
    Guard(
        "secrets",
        "no credential in the pushed lines",
        "secret-scan",
        (),
        "a pushed credential is exposed and must be rotated",
    ),
)

#: The flags that make the pytest guards independent of the suite's conftest and plugins.
PYTEST_FLAGS = ("-q", "--tb=line", "-rf", "--noconftest", "-p", "no:cacheprovider", "-o", "addopts=")


@dataclass
class GuardResult:
    id: str
    title: str
    ok: bool
    detail: str = ""

    def line(self) -> str:
        mark = "✓" if self.ok else "✗"
        return f"{mark} {self.id}: {self.title}" + (f" — {self.detail}" if self.detail else "")


@dataclass
class GuardReport:
    base: str
    head: str
    results: list[GuardResult] = field(default_factory=list)
    tier: ChangeTier | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(r.ok for r in self.results)

    def result(self, guard_id: str) -> GuardResult:
        return next(r for r in self.results if r.id == guard_id)

    def render(self) -> str:
        lines = [r.line() for r in self.results]
        if self.tier is not None:
            lines.append(f"  {self.tier.line()} (reported only; nothing is refused for the tier yet)")
        else:
            lines.append("  no tier map (.axiom/tiers.toml); the tier is not computed")
        lines.extend(f"  note: {note}" for note in self.notes)
        failed = [r.id for r in self.results if not r.ok]
        lines.append("guards: passed" if not failed else f"guards: FAILED ({', '.join(failed)})")
        return "\n".join(lines) + "\n"

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "base": self.base,
            "head": self.head,
            "results": [asdict(r) for r in self.results],
            "tier": self.tier.to_dict() if self.tier is not None else None,
            "notes": list(self.notes),
        }


# -- git ----------------------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=False, stdin=subprocess.DEVNULL
    )
    if done.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])}: {(done.stderr or '').strip().splitlines()[-1:] or done.returncode}")
    return done.stdout


def resolve_base(root: Path, base: str, head: str) -> str:
    """The commit the pushed range starts from. Empty when none can be found."""
    candidates = [base] if base and base != _ZERO else []
    candidates += ["origin/main", f"{head}~1"]
    for candidate in candidates:
        try:
            if candidate == "origin/main":
                return _git(root, "merge-base", "origin/main", head).strip()
            return _git(root, "rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}").strip()
        except RuntimeError:
            continue
    return ""


def changed_files(root: Path, base: str, head: str) -> list[str]:
    if not base:
        return []
    out = _git(root, "diff", "--name-only", "--no-renames", "--diff-filter=d", base, head)
    return [line for line in out.splitlines() if line.strip()]


def added_lines(root: Path, base: str, head: str) -> list[tuple[str, int, str]]:
    """``(path, line number, text)`` for every line ``base..head`` adds."""
    if not base:
        return []
    out = _git(root, "diff", "--unified=0", "--no-color", "--no-ext-diff", "--no-renames", base, head)
    found: list[tuple[str, int, str]] = []
    path, number = "", 0
    for line in out.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else ""
        elif line.startswith("@@"):
            try:
                number = int(line.split("+", 1)[1].split(" ", 1)[0].split(",", 1)[0])
            except (IndexError, ValueError):
                number = 0
        elif line.startswith("+") and path:
            found.append((path, number, line[1:]))
            number += 1
    return found


def scan_added(root: Path, base: str, head: str) -> list[str]:
    """``path:line pattern`` for each credential format in the added lines. Never the text."""
    from axiom.infra.secret_scan import scan_text

    hits: list[str] = []
    for path, number, text in added_lines(root, base, head):
        if ALLOW_MARK in text:
            continue
        for finding in scan_text(text, location=path):
            hits.append(f"{path}:{number} {finding.pattern}")
    return hits


# -- running ---------------------------------------------------------------------------


def _attribute(output: str, guard: Guard) -> bool:
    """True when pytest's output reports a failure in one of ``guard``'s targets."""
    files = {target.split("::", 1)[0] for target in guard.targets}
    for line in output.splitlines():
        if line.startswith(("FAILED ", "ERROR ")):
            node = line.split(" ", 1)[1]
            if any(node.startswith(f) for f in files):
                return True
    return False


# -- the stamp: the server skips what the hook already passed -----------------------


def guards_version(root: Path) -> str:
    """The guards' identity: a hash of their one definition as it is in ``root``."""
    here = Path(root) / "src" / "axiom" / "extensions" / "builtins" / "release" / "guards.py"
    source = here if here.is_file() else Path(__file__)
    return hashlib.sha256(source.read_bytes()).hexdigest()[:16]


def stamp_line(tree: str, version: str) -> str:
    return f"{STAMP_PREFIX} {tree} {version}"


def write_stamp(root: Path, head: str = "HEAD") -> str:
    """Stamp ``head`` as passed; call only after every guard passed. Returns the stamp."""
    line = stamp_line(_git(root, "rev-parse", f"{head}^{{tree}}").strip(), guards_version(root))
    _git(root, "notes", f"--ref={NOTES_REF}", "add", "-f", "-m", line, head)
    return line


def find_stamp(root: Path, head: str = "HEAD") -> str | None:
    """The stamp covering ``head``'s tree for the guards in ``root`` now, else ``None``."""
    root = Path(root)
    try:
        want = stamp_line(_git(root, "rev-parse", f"{head}^{{tree}}").strip(), guards_version(root))
        listing = _git(root, "notes", f"--ref={NOTES_REF}", "list")
    except RuntimeError:
        return None
    blobs = [line.split()[0] for line in listing.splitlines() if line.strip()]
    if not blobs:
        return None
    done = subprocess.run(["git", "cat-file", "--batch"], cwd=root, input="\n".join(blobs) + "\n",
                          capture_output=True, text=True, check=False)
    return want if any(line.strip() == want for line in done.stdout.splitlines()) else None


def main(argv: Sequence[str] | None = None) -> int:
    """``--stamped SHA``: exit 0 when the hook already passed the guards for SHA's tree (the
    server skips them), 1 when the guards must run. Needs only git and the source tree."""
    ap = argparse.ArgumentParser(prog="python -m axiom.extensions.builtins.release.guards")
    ap.add_argument("--stamped", required=True, metavar="SHA")
    ap.add_argument("--root", default=".")
    a = ap.parse_args(argv)
    found = find_stamp(Path(a.root), a.stamped)
    print(f"guards: the pre-push hook passed them for this tree ({found}); skipped" if found
          else "guards: no pre-push stamp for this tree and these guards; they run")
    return 0 if found else 1


def run_guards(
    root: Path,
    *,
    base: str = "",
    head: str = "HEAD",
    only: Sequence[str] | None = None,
    python: str | None = None,
    runner: Runner | None = None,
    stamp: bool = False,
) -> GuardReport:
    """Run the guards on ``root`` for the range ``base..head``. With ``stamp``, a run of
    every guard that passed is stamped on ``head`` (see :func:`write_stamp`)."""
    root = Path(root)
    runner = runner or subprocess.run
    python = python or sys.executable
    chosen = [g for g in GUARDS if not only or g.id in set(only)]
    resolved = resolve_base(root, base, head)
    report = GuardReport(base=resolved, head=head)
    if not resolved:
        report.notes.append("no base commit found: the secret scan and the tier saw no changed lines")

    pytest_guards = [g for g in chosen if g.kind == "pytest"]
    if pytest_guards:
        env = {**os.environ, "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"}
        args = [python, "-m", "pytest", *PYTEST_FLAGS, *(t for g in pytest_guards for t in g.targets)]
        done = runner(args, cwd=root, env=env, capture_output=True, text=True, check=False)
        output = f"{done.stdout or ''}\n{done.stderr or ''}"
        for guard in pytest_guards:
            if done.returncode == 0:
                report.results.append(GuardResult(guard.id, guard.title, True))
            elif done.returncode == 1:
                failed = _attribute(output, guard)
                report.results.append(
                    GuardResult(guard.id, guard.title, not failed, guard.why if failed else "")
                )
            else:
                report.results.append(
                    GuardResult(guard.id, guard.title, False, f"could not run (pytest exit {done.returncode})")
                )
        if done.returncode == 1 and all(r.ok for r in report.results):
            for result in report.results:
                result.ok = False
                result.detail = "pytest failed and named no guard; read its output"
        if done.returncode != 0:
            tail = [ln for ln in output.splitlines() if ln.strip()][-15:]
            report.notes.extend(tail)

    for guard in chosen:
        if guard.kind == "script":
            args = [python, *guard.targets]
            done = runner(args, cwd=root, capture_output=True, text=True, check=False)
            if done.returncode == 0:
                report.results.append(GuardResult(guard.id, guard.title, True))
            elif done.returncode == 127:
                report.results.append(GuardResult(guard.id, guard.title, False, f"could not run ({python}, exit 127)"))
            else:
                detail = ((done.stdout or "") + (done.stderr or "")).strip().splitlines()[-1:] or [guard.why]
                report.results.append(GuardResult(guard.id, guard.title, False, detail[0]))
        elif guard.kind == "secret-scan":
            try:
                hits = scan_added(root, resolved, head)
            except RuntimeError as exc:
                report.results.append(GuardResult(guard.id, guard.title, False, f"could not run: {exc}"))
                continue
            detail = "; ".join(hits[:10]) + (f" and {len(hits) - 10} more" if len(hits) > 10 else "")
            if hits:
                detail += f". Remove it and rotate it; a test fixture line may carry `{ALLOW_MARK}`"
            report.results.append(GuardResult(guard.id, guard.title, not hits, detail))

    order = {g.id: i for i, g in enumerate(GUARDS)}
    report.results.sort(key=lambda r: order.get(r.id, 99))
    try:
        tier_map = load_tier_map(root)
    except (ValueError, OSError) as exc:
        report.results.append(GuardResult("tier-map", "the tier map is readable", False, str(exc)))
        tier_map = None
    if tier_map is not None:
        try:
            report.tier = tier_map.classify(changed_files(root, resolved, head))
        except RuntimeError as exc:
            report.notes.append(f"tier not computed: {exc}")
    if stamp and report.ok and not only:
        try:
            report.notes.append(f"stamped: {write_stamp(root, head)}")
        except RuntimeError as exc:  # no stamp only means the server runs the guards again
            report.notes.append(f"not stamped: {exc}")
    return report


__all__ = ["ALLOW_MARK", "GUARDS", "NOTES_REF", "Guard", "GuardReport", "GuardResult", "find_stamp",
           "guards_version", "main", "run_guards", "scan_added", "stamp_line", "write_stamp"]


if __name__ == "__main__":
    sys.exit(main())
