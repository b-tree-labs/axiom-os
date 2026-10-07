# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A function must not run a subprocess, ignore the result, and return True.

Six of these were fixed one at a time — LaunchdProvider.start, stop and
uninstall, SystemdProvider.install and uninstall, and the update path's
dependency and migration steps. Six patches for one habit is five too many, so
this is the check that makes the seventh fail in CI instead of in production.

The shape it catches: a function that calls `subprocess.run(...)` as a bare
statement — discarding the returned CompletedProcess — and returns a literal
True somewhere. That is a function reporting an outcome it did not look at.

The check is a syntactic one: it sees the *shape*, not the semantics. A
function that discards a launch command and then polls for readiness has the
shape without the lie, because the poll supplies the observation. Those cases
belong in the allowlist with that reason stated, which is the point — the
allowlist is where an author has to write down why the shape is honest here,
and a reviewer gets to disagree.

Each entry carries its reason. "It would be annoying to fix" is not one of
them; if an entry stops being true, delete it rather than widen the rule. The
parametrised test at the bottom fails if an allowlisted function no longer
exists, so the list cannot rot into permanent silence.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

_SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "axiom"

#: (module suffix, function) -> why an unobserved True is honest here.
_ALLOWED = {
    ("extensions/builtins/signals/cli.py", "_play_audio_clip"):
        "Playback is cosmetic; a silent notification is not a false report of work.",
    ("extensions/builtins/signals/cli.py", "_play_audio_segment"):
        "Same: audio is decoration around a result delivered elsewhere.",
    ("extensions/builtins/signals/pgvector_store.py", "k3d_up"):
        "Developer-only cluster helper; the caller verifies readiness itself.",
    ("extensions/builtins/signals/pgvector_store.py", "k3d_down"):
        "Tearing down a dev cluster that is already gone is not a failure.",
    ("extensions/builtins/signals/pgvector_store.py", "k3d_delete"):
        "Same as k3d_down.",
    # These two do observe the step that decides the outcome. What they discard
    # is best-effort cleanup around it, and the decisive check is right there in
    # the function: install reads `enable`, uninstall reads `disable` and then
    # probes `_still_loaded`. Failing them would be the guard crying wolf.
    ("infra/services.py", "install"):
        "SystemdProvider.install reads the `enable` result; the discarded calls "
        "are a stale-timer cleanup and a daemon-reload whose effect the enable "
        "check covers.",
    ("infra/services.py", "uninstall"):
        "SystemdProvider.uninstall reads every `disable` result and then probes "
        "_still_loaded; the discarded call is the daemon-reload it probes after.",
    # These two discard the *launch* command and then poll for readiness. The
    # poll is the observation, and the True path is reachable only from a probe
    # that succeeded — so the discarded call carries no claim.
    ("setup/infra.py", "start_docker"):
        "Discards `open -a Docker`, then returns True only when `docker info` "
        "exits 0; the 60s timeout now returns False.",
    ("setup/infra.py", "_deploy_llm_server"):
        "Discards the kubectl apply, then returns True only when the pod reports "
        "Running; the timeout now returns False.",
}


def _is_unobserved_run(n: ast.AST) -> bool:
    """A bare `subprocess.run(...)` whose result nothing can see.

    `check=True` does not discard the result — it converts a non-zero exit into
    a CalledProcessError, which the caller must handle. Counting those as
    offences would make this guard cry wolf on correct code, and a guard that
    cries wolf gets deleted, which is worse than not having one.
    """
    if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)):
        return False
    fn = n.value.func
    if not (
        isinstance(fn, ast.Attribute)
        and fn.attr in ("run", "call")
        and isinstance(fn.value, ast.Name)
        and fn.value.id == "subprocess"
    ):
        return False
    for kw in n.value.keywords:
        if kw.arg == "check" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
            return False
    return True


def _offenders() -> list[tuple[str, str, int]]:
    found = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = str(path.relative_to(_SRC.parent.parent / "src" / "axiom"))
        if "/tests/" in str(path) or path.name.startswith("test_"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            discards = any(_is_unobserved_run(n) for n in ast.walk(node))
            if not discards:
                continue
            returns_true = any(
                isinstance(n, ast.Return)
                and isinstance(n.value, ast.Constant)
                and n.value.value is True
                for n in ast.walk(node)
            )
            if returns_true and (rel, node.name) not in _ALLOWED:
                found.append((rel, node.name, node.lineno))
    return found


def test_no_function_reports_success_it_did_not_observe():
    offenders = _offenders()
    assert not offenders, (
        "These functions run a subprocess, discard the result, and return True — "
        "reporting an outcome they never looked at:\n"
        + "\n".join(f"  {f}:{ln}  {fn}()" for f, fn, ln in offenders)
        + "\n\nEither read the result and return what actually happened, or add "
          "an entry to _ALLOWED in this file explaining why an unobserved True "
          "is honest there."
    )


def test_the_guard_can_fail():
    """A negative control: the detector must fire on the shape it targets."""
    src = (
        "import subprocess\n"
        "def pretend():\n"
        "    subprocess.run(['true'])\n"
        "    return True\n"
    )
    tree = ast.parse(src)
    fn = tree.body[1]
    discards = any(
        isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Attribute)
        and n.value.func.attr == "run"
        for n in ast.walk(fn)
    )
    returns_true = any(
        isinstance(n, ast.Return) and isinstance(n.value, ast.Constant)
        and n.value.value is True
        for n in ast.walk(fn)
    )
    assert discards and returns_true, "the detector would miss the canonical case"


@pytest.mark.parametrize("entry", sorted(_ALLOWED))
def test_every_allowlist_entry_still_exists(entry):
    """An allowlist that outlives its code silently stops guarding."""
    rel, fn = entry
    path = _SRC / rel
    assert path.exists(), f"allowlisted {rel} is gone — remove the entry"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert fn in names, f"allowlisted {rel}::{fn} no longer exists — remove the entry"

# ---------------------------------------------------------------------------
# Second dialect: "absent" and "unreadable" handled as the same fact
# ---------------------------------------------------------------------------

# These two sets are deliberately narrow, and the narrowness is why this check
# currently reports nothing: `KeyError`, `ValueError` and `TypeError` were tried
# and removed because they are genuinely ambiguous — `KeyError` is "absent" for a
# dict and "unreadable" for a malformed payload, and including them flagged
# correct code like `_grounded_text`. A check that cries wolf gets deleted, so
# precision wins here.
#
# The cost is real and worth stating: shapes built on those exceptions are NOT
# covered. Zero offenders means "none of the unambiguous shape", not "the class
# is gone". The canary test below is what keeps this honest.

#: Exceptions that mean "there is nothing here".
_ABSENT = {"FileNotFoundError", "IsADirectoryError", "NotADirectoryError"}
#: Exceptions that mean "there is something here and we could not read it".
_UNREADABLE = {"JSONDecodeError", "UnicodeDecodeError", "BadZipFile"}

#: (module suffix, line-anchored function) -> why collapsing the two is honest.
_CONFLATION_ALLOWED = {
    ("agents/learning.py", "_read_patterns"):
        "Learned patterns are a cache rebuilt from observation; losing them "
        "degrades suggestions rather than destroying a record.",
    ("extensions/builtins/data_platform/sources/box/oauth_auth.py",
     "_load_cached_access"):
        "An unreadable token cache correctly means 'no usable token' — the "
        "caller refreshes, which is the right and harmless fallback.",
    ("extensions/builtins/publishing/providers/feedback/docx_comments.py",
     "_parse_comments"):
        "A .docx we cannot open has no comments we can honestly report; the "
        "caller surfaces the empty result to a human who has the file.",
}


def _conflations() -> list[tuple[str, str, int]]:
    found = []
    for path in sorted(_SRC.rglob("*.py")):
        if "/tests/" in str(path) or path.name.startswith("test_"):
            continue
        rel = str(path.relative_to(_SRC))
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for h in ast.walk(fn):
                if not isinstance(h, ast.ExceptHandler) or h.type is None:
                    continue
                items = h.type.elts if isinstance(h.type, ast.Tuple) else [h.type]
                names = {
                    e.id if isinstance(e, ast.Name) else getattr(e, "attr", "")
                    for e in items
                }
                if not (names & _ABSENT and names & _UNREADABLE):
                    continue
                if len(h.body) != 1:
                    continue
                st = h.body[0]
                if not (isinstance(st, ast.Return)
                        and isinstance(st.value, (ast.Constant, ast.Dict, ast.List))):
                    continue
                if (rel, fn.name) not in _CONFLATION_ALLOWED:
                    found.append((rel, fn.name, h.lineno))
    return found


def test_absent_and_unreadable_are_not_the_same_fact():
    """A missing file has no data. An unreadable one has data we cannot read.

    Handling both in one `except` and returning the same default is what let a
    corrupt approval queue, a corrupt propagation queue and a corrupt user
    glossary each read as empty — and then be overwritten by the next
    read-modify-write.
    """
    offenders = _conflations()
    assert not offenders, (
        "These handlers treat 'file is absent' and 'file is unreadable' as the "
        "same outcome:\n"
        + "\n".join(f"  {f}:{ln}  {fn}()" for f, fn, ln in offenders)
        + "\n\nSeparate them, or use LockedJsonFile(..., strict=True), or add "
          "an entry to _CONFLATION_ALLOWED saying why one answer serves both."
    )


@pytest.mark.parametrize("entry", sorted(_CONFLATION_ALLOWED))
def test_every_conflation_allowlist_entry_still_exists(entry):
    rel, fn = entry
    path = _SRC / rel
    assert path.exists(), f"allowlisted {rel} is gone — remove the entry"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert fn in names, f"allowlisted {rel}::{fn} no longer exists — remove it"


def test_the_conflation_guard_can_fail():
    """Negative control: the detector must fire on the canonical shape.

    Without this, a narrowed detector that matches nothing is indistinguishable
    from a codebase with nothing to match — which is the exact failure this file
    exists to catch, turned on itself.
    """
    src = (
        "import json\n"
        "def load(path):\n"
        "    try:\n"
        "        return json.loads(path.read_text())\n"
        "    except (FileNotFoundError, json.JSONDecodeError):\n"
        "        return {}\n"
    )
    tree = ast.parse(src)
    fn = tree.body[1]
    handler = next(n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler))
    items = handler.type.elts
    names = {e.id if isinstance(e, ast.Name) else e.attr for e in items}
    assert names & _ABSENT and names & _UNREADABLE, (
        "the detector no longer recognises the shape it was written for"
    )
