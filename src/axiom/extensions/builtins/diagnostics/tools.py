# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Doctor Agent tools — read, edit, test, git operations.

Six tools following the ToolDef + execute() pattern. The doctor uses
these directly (not via the chat tool registry) so they are self-contained.

Safety invariants:
- edit_file enforces path allowlist + line limits
- run_tests only runs pytest (no arbitrary shell)
- git_commit_fix creates worktree branches, never touches main
"""

from __future__ import annotations

import difflib
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from axiom import REPO_ROOT as _REPO_ROOT

_RUNTIME_DIR = _REPO_ROOT / "runtime"
_DOCTOR_DIR = _RUNTIME_DIR / "doctor"
_BACKUPS_DIR = _DOCTOR_DIR / "backups"
_LOG_PATH = _RUNTIME_DIR / "logs" / "cli_events.jsonl"

# --- Safety constants ---

ALLOWED_EDIT_PREFIXES = [
    "src/axiom/extensions/builtins/signals/",
    "src/axiom/extensions/builtins/chat/",
    "tests/",
]

BLOCKED_EDIT_PREFIXES = [
    "src/axiom/extensions/builtins/diagnostics/",
    "src/axiom/infra/",
    "src/axiom/axiom_cli.py",
]

MAX_EDIT_LINES = 50

# Per-session edit tracking (reset per DoctorAgent instance)
_session_edit_count = 0
_session_max_edits = 3


def reset_session_edits(max_edits: int = 3) -> None:
    """Reset the per-session edit counter. Called by DoctorAgent on init."""
    global _session_edit_count, _session_max_edits
    _session_edit_count = 0
    _session_max_edits = max_edits


# --- Tool definitions (OpenAI function-calling format) ---

TOOL_DEFS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file_with_lines",
            "description": (
                "Read a file with line numbers. Optionally specify a line range. "
                "Returns numbered lines for precise editing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path from repo root.",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "First line to read (1-indexed). Omit to read from start.",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Last line to read (inclusive). Omit to read to end.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": (
                "Search for a regex pattern across the repository. "
                "Returns matching file paths and line numbers with context."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex pattern to search for.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Subdirectory to search in (relative to repo root). Omit for full repo.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of matches to return. Default 20.",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_error_log",
            "description": (
                "Read recent entries from cli_events.jsonl. "
                "Optionally filter by fingerprint to find similar past errors."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "fingerprint": {
                        "type": "string",
                        "description": "Filter by error fingerprint. Omit for all recent.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max entries to return. Default 10.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace a range of lines in a file with new content. "
                "Path must be in the allowlist. Max 50 lines changed per edit. "
                "Max 3 files edited per session. Creates a backup before editing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path from repo root.",
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "First line to replace (1-indexed, inclusive).",
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "Last line to replace (1-indexed, inclusive).",
                    },
                    "new_content": {
                        "type": "string",
                        "description": "Replacement content (replaces lines start_line through end_line).",
                    },
                },
                "required": ["path", "start_line", "end_line", "new_content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": (
                "Run pytest on a specific test file or directory. "
                "Returns pass/fail status and output. 60-second timeout."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path to test file or directory.",
                    },
                    "extra_args": {
                        "type": "string",
                        "description": "Additional pytest arguments (e.g., '-k test_name').",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_commit_fix",
            "description": (
                "Create a git branch and commit the fix. Only call this after "
                "tests pass. Creates branch doctor/fix-{fingerprint}. "
                "Soft dependency on git — returns skipped if git unavailable."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "fingerprint": {
                        "type": "string",
                        "description": "Error fingerprint for branch naming.",
                    },
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of relative paths to commit.",
                    },
                    "message": {
                        "type": "string",
                        "description": "Commit message.",
                    },
                },
                "required": ["fingerprint", "files", "message"],
            },
        },
    },
]


# --- Tool execution ---

def execute(name: str, params: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a tool call to the appropriate handler."""
    handlers = {
        "read_file_with_lines": _exec_read_file,
        "search_files": _exec_search_files,
        "read_error_log": _exec_read_error_log,
        "edit_file": _exec_edit_file,
        "run_tests": _exec_run_tests,
        "git_commit_fix": _exec_git_commit_fix,
    }
    handler = handlers.get(name)
    if not handler:
        return {"error": f"Unknown tool: {name}"}
    try:
        return handler(params)
    except Exception as e:
        return {"error": f"{name} failed: {e}"}


# --- Handlers ---

def _exec_read_file(params: dict[str, Any]) -> dict[str, Any]:
    """Read a file with line numbers."""
    rel_path = params.get("path", "")
    if not rel_path:
        return {"error": "path is required"}

    target = (_REPO_ROOT / rel_path).resolve()
    try:
        target.relative_to(_REPO_ROOT)
    except ValueError:
        return {"error": "Path is outside the repository."}

    if not target.exists():
        return {"error": f"File not found: {rel_path}"}
    if not target.is_file():
        return {"error": f"Not a file: {rel_path}"}

    try:
        lines = target.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        return {"error": f"Could not read {rel_path}: {e}"}

    start = max(1, params.get("start_line", 1))
    end = min(len(lines), params.get("end_line", len(lines)))

    numbered = []
    for i in range(start - 1, end):
        numbered.append(f"{i + 1:4d} | {lines[i]}")

    content = "\n".join(numbered)
    # Truncate if huge
    if len(content) > 12000:
        content = content[:12000] + "\n... (truncated)"

    return {
        "path": rel_path,
        "content": content,
        "total_lines": len(lines),
        "range": f"{start}-{end}",
    }


def _exec_search_files(params: dict[str, Any]) -> dict[str, Any]:
    """Regex search across the repo using grep."""
    pattern = params.get("pattern", "")
    if not pattern:
        return {"error": "pattern is required"}

    search_path = _REPO_ROOT
    sub = params.get("path", "")
    if sub:
        search_path = (_REPO_ROOT / sub).resolve()
        try:
            search_path.relative_to(_REPO_ROOT)
        except ValueError:
            return {"error": "Path is outside the repository."}

    max_results = params.get("max_results", 20)

    try:
        result = subprocess.run(
            ["grep", "-rn", "--include=*.py", "-E", pattern, str(search_path)],
            capture_output=True, text=True, timeout=15,
            cwd=str(_REPO_ROOT),
        )
    except FileNotFoundError:
        return {"error": "grep not available"}
    except subprocess.TimeoutExpired:
        return {"error": "Search timed out (15s)"}

    matches = []
    for line in result.stdout.splitlines()[:max_results]:
        # Strip repo root prefix for cleaner output
        line = line.replace(str(_REPO_ROOT) + "/", "")
        matches.append(line)

    return {
        "matches": matches,
        "count": len(matches),
        "truncated": len(result.stdout.splitlines()) > max_results,
    }


def _exec_read_error_log(params: dict[str, Any]) -> dict[str, Any]:
    """Read recent entries from cli_events.jsonl."""
    if not _LOG_PATH.exists():
        return {"entries": [], "count": 0}

    fingerprint = params.get("fingerprint", "")
    limit = params.get("limit", 10)

    entries = []
    try:
        for line in _LOG_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                if fingerprint:
                    data = event.get("data", {})
                    if data.get("fingerprint") != fingerprint:
                        continue
                entries.append(event)
            except json.JSONDecodeError:
                continue
    except Exception as e:
        return {"error": f"Could not read log: {e}"}

    # Return most recent entries
    entries = entries[-limit:]
    return {"entries": entries, "count": len(entries)}


def _validate_edit_path(rel_path: str) -> str | None:
    """Validate a path against the allowlist/blocklist. Returns error or None."""
    # Check blocklist first
    for prefix in BLOCKED_EDIT_PREFIXES:
        if rel_path.startswith(prefix) or rel_path == prefix.rstrip("/"):
            return f"Blocked: cannot edit files in {prefix}"

    # Check allowlist
    allowed = False
    for prefix in ALLOWED_EDIT_PREFIXES:
        if rel_path.startswith(prefix):
            allowed = True
            break
    if not allowed:
        return (
            f"Not in allowlist. Allowed prefixes: {ALLOWED_EDIT_PREFIXES}. "
            f"Got: {rel_path}"
        )

    return None


def _exec_edit_file(params: dict[str, Any]) -> dict[str, Any]:
    """Replace a range of lines in a file."""
    global _session_edit_count

    rel_path = params.get("path", "")
    start_line = params.get("start_line", 0)
    end_line = params.get("end_line", 0)
    new_content = params.get("new_content", "")

    if not rel_path:
        return {"error": "path is required"}
    if start_line < 1 or end_line < 1:
        return {"error": "start_line and end_line must be >= 1"}
    if end_line < start_line:
        return {"error": "end_line must be >= start_line"}

    # Check session limit
    if _session_edit_count >= _session_max_edits:
        return {"error": f"Session edit limit reached ({_session_max_edits} files)"}

    # Validate path
    path_err = _validate_edit_path(rel_path)
    if path_err:
        return {"error": path_err}

    target = (_REPO_ROOT / rel_path).resolve()
    try:
        target.relative_to(_REPO_ROOT)
    except ValueError:
        return {"error": "Path is outside the repository."}

    if not target.exists():
        return {"error": f"File not found: {rel_path}"}

    # Read current content
    try:
        original_text = target.read_text(encoding="utf-8")
        lines = original_text.splitlines(keepends=True)
    except Exception as e:
        return {"error": f"Could not read {rel_path}: {e}"}

    # Validate line range
    if start_line > len(lines):
        return {"error": f"start_line {start_line} > total lines {len(lines)}"}
    end_line = min(end_line, len(lines))

    # Check line limit
    new_lines = new_content.splitlines(keepends=True)
    # Ensure last line has newline
    if new_lines and not new_lines[-1].endswith("\n"):
        new_lines[-1] += "\n"
    lines_replaced = end_line - start_line + 1
    lines_added = len(new_lines)
    net_change = abs(lines_added - lines_replaced) + max(lines_added, lines_replaced)
    if net_change > MAX_EDIT_LINES:
        return {
            "error": (
                f"Edit too large: {net_change} lines affected "
                f"(max {MAX_EDIT_LINES}). Break into smaller edits."
            )
        }

    # Create backup
    _BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    backup_name = f"{ts}_{target.name}"
    backup_path = _BACKUPS_DIR / backup_name
    try:
        shutil.copy2(target, backup_path)
    except Exception as e:
        return {"error": f"Could not create backup: {e}"}

    # Apply edit
    new_file_lines = lines[:start_line - 1] + new_lines + lines[end_line:]
    new_text = "".join(new_file_lines)

    try:
        target.write_text(new_text, encoding="utf-8")
    except Exception as e:
        # Restore from backup
        shutil.copy2(backup_path, target)
        return {"error": f"Could not write {rel_path}: {e}"}

    _session_edit_count += 1

    # Generate unified diff
    diff = difflib.unified_diff(
        original_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=f"a/{rel_path}",
        tofile=f"b/{rel_path}",
    )
    diff_str = "".join(diff)

    return {
        "path": rel_path,
        "lines_replaced": f"{start_line}-{end_line}",
        "lines_added": lines_added,
        "backup": str(backup_path),
        "diff": diff_str,
        "edits_remaining": _session_max_edits - _session_edit_count,
    }


def _exec_run_tests(params: dict[str, Any]) -> dict[str, Any]:
    """Run pytest on a specific path."""
    rel_path = params.get("path", "")
    if not rel_path:
        return {"error": "path is required"}

    target = (_REPO_ROOT / rel_path).resolve()
    try:
        target.relative_to(_REPO_ROOT)
    except ValueError:
        return {"error": "Path is outside the repository."}

    if not target.exists():
        return {"error": f"Test path not found: {rel_path}"}

    # Check pytest is available
    pytest_bin = shutil.which("pytest")
    if not pytest_bin:
        return {"error": "pytest not available"}

    cmd = [pytest_bin, str(target), "-x", "--tb=short", "-q"]
    extra = params.get("extra_args", "")
    if extra:
        cmd.extend(extra.split())

    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=60,
            cwd=str(_REPO_ROOT),
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
    except subprocess.TimeoutExpired:
        return {"passed": False, "error": "Tests timed out (60s)", "output": ""}

    output = result.stdout + result.stderr
    # Truncate
    if len(output) > 4000:
        output = output[:4000] + "\n... (truncated)"

    return {
        "passed": result.returncode == 0,
        "returncode": result.returncode,
        "output": output,
    }


def _exec_git_commit_fix(params: dict[str, Any]) -> dict[str, Any]:
    """Create a branch and commit the fix. Soft dependency on git."""
    from axiom.infra.git import safe_git_env

    fingerprint = params.get("fingerprint", "")
    files = params.get("files", [])
    message = params.get("message", "")

    if not fingerprint or not files or not message:
        return {"error": "fingerprint, files, and message are required"}

    # Check git is available
    git_bin = shutil.which("git")
    if not git_bin:
        return {"skipped": True, "reason": "git not available"}

    # Check we're in a git repo
    try:
        subprocess.run(
            [git_bin, "rev-parse", "--git-dir"],
            capture_output=True, check=True, timeout=5,
            cwd=str(_REPO_ROOT),
            env=safe_git_env(_REPO_ROOT),
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return {"skipped": True, "reason": "not a git repository"}

    branch_name = f"doctor/fix-{fingerprint}"

    try:
        # Create and checkout a new branch (from current HEAD)
        subprocess.run(
            [git_bin, "checkout", "-b", branch_name],
            capture_output=True, check=True, timeout=10,
            cwd=str(_REPO_ROOT),
            env=safe_git_env(_REPO_ROOT),
        )

        # Stage the changed files
        for f in files:
            abs_path = (_REPO_ROOT / f).resolve()
            try:
                abs_path.relative_to(_REPO_ROOT)
            except ValueError:
                continue
            subprocess.run(
                [git_bin, "add", str(abs_path)],
                capture_output=True, check=True, timeout=5,
                cwd=str(_REPO_ROOT),
                env=safe_git_env(_REPO_ROOT),
            )

        # Commit
        subprocess.run(
            [git_bin, "commit", "-m", message],
            capture_output=True, text=True, check=True, timeout=10,
            cwd=str(_REPO_ROOT),
            env=safe_git_env(_REPO_ROOT),
        )

        # Get commit SHA
        sha_result = subprocess.run(
            [git_bin, "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True, timeout=5,
            cwd=str(_REPO_ROOT),
            env=safe_git_env(_REPO_ROOT),
        )
        commit_sha = sha_result.stdout.strip()

        # Switch back to previous branch
        subprocess.run(
            [git_bin, "checkout", "-"],
            capture_output=True, timeout=5,
            cwd=str(_REPO_ROOT),
            env=safe_git_env(_REPO_ROOT),
        )

        return {
            "branch": branch_name,
            "commit_sha": commit_sha,
            "files_committed": files,
        }

    except subprocess.CalledProcessError as e:
        # Clean up: try to switch back
        try:
            subprocess.run(
                [git_bin, "checkout", "-"],
                capture_output=True, timeout=5,
                cwd=str(_REPO_ROOT),
                env=safe_git_env(_REPO_ROOT),
            )
        except Exception:
            pass
        stderr = e.stderr.decode() if isinstance(e.stderr, bytes) else str(e.stderr or "")
        return {"error": f"git operation failed: {stderr}"}
    except subprocess.TimeoutExpired:
        return {"error": "git operation timed out"}


def rollback_file(rel_path: str) -> dict[str, Any]:
    """Restore a file from the most recent backup. Used by retry_handler."""
    if not _BACKUPS_DIR.exists():
        return {"error": "No backups directory"}

    filename = Path(rel_path).name
    # Find most recent backup matching this filename
    backups = sorted(
        [b for b in _BACKUPS_DIR.iterdir() if b.name.endswith(f"_{filename}")],
        reverse=True,
    )
    if not backups:
        return {"error": f"No backup found for {filename}"}

    target = (_REPO_ROOT / rel_path).resolve()
    try:
        target.relative_to(_REPO_ROOT)
    except ValueError:
        return {"error": "Path is outside the repository."}

    try:
        shutil.copy2(backups[0], target)
        return {"restored": rel_path, "from_backup": str(backups[0])}
    except Exception as e:
        return {"error": f"Restore failed: {e}"}
