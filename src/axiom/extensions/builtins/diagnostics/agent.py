# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""DoctorAgent — autonomous multi-turn diagnosis and patching loop.

Modeled on ChatAgent.turn() but
without the ApprovalGate. Tests are the gate, not humans. Circuit
breakers in subscriber.py prevent runaway.

Flow:
1. Build system prompt with error signal + project context
2. LLM reads code around traceback
3. LLM edits files (guarded by path allowlist + line limits)
4. Run tests — if fail, give up (retry handled by subscriber)
5. If tests pass → patch is live (editable install)
6. Try git commit (soft — no-op if git unavailable)
7. Return DoctorResult with diff recorded regardless of git
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field
from typing import Any

from axiom import REPO_ROOT as _REPO_ROOT
from axiom.extensions.builtins.diagnostics.tools import (
    TOOL_DEFS,
    execute,
    reset_session_edits,
)


@dataclass
class DoctorResult:
    """Structured result of a doctor diagnosis session."""

    fingerprint: str
    diagnosis: str = ""
    files_changed: list[str] = field(default_factory=list)
    patch_diff: str = ""
    tests_passed: bool = False
    tests_output: str = ""
    branch_name: str = ""
    commit_sha: str = ""
    status: str = "failed"  # patched | diagnosed | failed | llm_unavailable | skipped
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- System Prompt ---

_DOCTOR_SYSTEM_PROMPT = """\
You are the Neut Doctor Agent — an autonomous code repair system for the
Axiom CLI. You diagnose CLI errors and produce minimal, correct patches.

## Your Task

An error occurred in the Neut CLI. You have the full traceback, error message,
and environment context. Your job:

1. **Read** the relevant source code around the traceback to understand the bug.
2. **Diagnose** the root cause (not just the symptom).
3. **Edit** the minimum number of lines to fix it. Prefer surgical fixes.
4. **Test** your fix by running the relevant test file(s).
5. If tests pass, call **git_commit_fix** to create a branch with your fix.

## Constraints

- You can only edit files in: src/axiom/extensions/builtins/signals/, src/axiom/extensions/builtins/chat/, tests/
- You CANNOT edit: src/axiom/extensions/builtins/diagnostics/, src/axiom/infra/, src/axiom/axiom_cli.py
- Max 3 files edited per session, max 50 lines changed per edit
- You get {max_rounds} tool-use rounds total. Be efficient.
- If you cannot fix the bug, explain your diagnosis clearly — that's still valuable.

## Tools Available

- **read_file_with_lines**: Read source code with line numbers
- **search_files**: Search for patterns across the codebase
- **read_error_log**: Check for similar past errors
- **edit_file**: Replace a range of lines (creates backup automatically)
- **run_tests**: Run pytest on a specific file/directory
- **git_commit_fix**: Commit your fix to a doctor/fix-{{fingerprint}} branch

## Guidelines

- Read the file first. Understand context before editing.
- Check if the error has occurred before (read_error_log).
- When editing, provide the COMPLETE replacement lines, not partial.
- Run tests after editing. If they fail, you may try a different approach.
- Write a clear commit message: "doctor: fix {{error_type}} in {{command}} [{{fingerprint}}]"
- If you determine the error is in code you cannot edit, say so in your diagnosis.
"""

_RETRY_ADDENDUM = """
## Retry Context

Your previous fix was REJECTED by the independent reviewer. Their feedback:

{feedback}

The previous edit has been rolled back. Try a different approach that addresses
the reviewer's concerns. You have one more attempt.
"""


class DoctorAgent:
    """Autonomous diagnosis and patching agent."""

    MAX_ROUNDS = 6
    MAX_EDITS = 3
    MAX_EDIT_LINES = 50

    def __init__(self, gateway: Any, bus: Any):
        self.gateway = gateway
        self.bus = bus
        self._files_changed: list[str] = []
        self._diffs: list[str] = []

    def diagnose_and_patch(self, error_signal: dict[str, Any]) -> DoctorResult:
        """Run the full diagnosis → edit → test → commit loop."""
        fingerprint = error_signal.get("fingerprint", "unknown")
        reset_session_edits(self.MAX_EDITS)

        system = self._build_system_prompt(error_signal)
        messages = self._build_initial_messages(error_signal)

        diagnosis = ""

        for _round in range(self.MAX_ROUNDS):
            response = self.gateway.complete_with_tools(
                messages=messages,
                system=system,
                tools=TOOL_DEFS,
                max_tokens=4096,
                task="doctor",
            )

            if not response.success:
                return DoctorResult(
                    fingerprint=fingerprint,
                    status="llm_unavailable",
                    error=response.error or "LLM call failed",
                )

            # No tool calls — LLM is done (final text is the diagnosis)
            if not response.tool_use:
                diagnosis = response.text
                break

            # Accumulate text as part of diagnosis
            if response.text:
                diagnosis += response.text + "\n"

            # Process tool calls
            tool_results = self._process_tools(response)

            # Build messages for next round
            messages.append(self._assistant_message(response))
            for tool_id, name, result in tool_results:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "name": name,
                    "content": json.dumps(result),
                })

        # Determine outcome
        tests_passed = self._check_tests_passed(messages)
        tests_output = self._extract_test_output(messages)
        patch_diff = "\n".join(self._diffs)

        # Try git commit if tests passed
        branch_name = ""
        commit_sha = ""
        if tests_passed and self._files_changed:
            git_result = execute("git_commit_fix", {
                "fingerprint": fingerprint,
                "files": self._files_changed,
                "message": f"doctor: fix {error_signal.get('error_type', 'error')} "
                           f"in {error_signal.get('command', 'unknown')} [{fingerprint}]",
            })
            branch_name = git_result.get("branch", "")
            commit_sha = git_result.get("commit_sha", "")

        if tests_passed and self._files_changed:
            status = "patched"
        elif self._files_changed:
            status = "diagnosed"  # Edited but tests failed
        elif diagnosis:
            status = "diagnosed"  # Diagnosis only, no editable fix
        else:
            status = "failed"

        return DoctorResult(
            fingerprint=fingerprint,
            diagnosis=diagnosis.strip(),
            files_changed=list(self._files_changed),
            patch_diff=patch_diff,
            tests_passed=tests_passed,
            tests_output=tests_output,
            branch_name=branch_name,
            commit_sha=commit_sha,
            status=status,
        )

    def retry_with_feedback(
        self, error_signal: dict[str, Any], feedback: str,
    ) -> DoctorResult:
        """Retry diagnosis with reviewer feedback. Previous edit rolled back."""
        fingerprint = error_signal.get("fingerprint", "unknown")
        reset_session_edits(self.MAX_EDITS)

        system = self._build_system_prompt(error_signal)
        system += _RETRY_ADDENDUM.format(feedback=feedback)
        messages = self._build_initial_messages(error_signal)

        diagnosis = ""

        for _round in range(self.MAX_ROUNDS):
            response = self.gateway.complete_with_tools(
                messages=messages,
                system=system,
                tools=TOOL_DEFS,
                max_tokens=4096,
                task="doctor",
            )

            if not response.success:
                return DoctorResult(
                    fingerprint=fingerprint,
                    status="llm_unavailable",
                    error=response.error or "LLM call failed",
                )

            if not response.tool_use:
                diagnosis = response.text
                break

            if response.text:
                diagnosis += response.text + "\n"

            tool_results = self._process_tools(response)
            messages.append(self._assistant_message(response))
            for tool_id, name, result in tool_results:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_id,
                    "name": name,
                    "content": json.dumps(result),
                })

        tests_passed = self._check_tests_passed(messages)
        tests_output = self._extract_test_output(messages)
        patch_diff = "\n".join(self._diffs)

        branch_name = ""
        commit_sha = ""
        if tests_passed and self._files_changed:
            git_result = execute("git_commit_fix", {
                "fingerprint": fingerprint,
                "files": self._files_changed,
                "message": f"doctor: fix {error_signal.get('error_type', 'error')} "
                           f"in {error_signal.get('command', 'unknown')} [{fingerprint}] (retry)",
            })
            branch_name = git_result.get("branch", "")
            commit_sha = git_result.get("commit_sha", "")

        if tests_passed and self._files_changed:
            status = "patched"
        elif diagnosis:
            status = "diagnosed"
        else:
            status = "failed"

        return DoctorResult(
            fingerprint=fingerprint,
            diagnosis=diagnosis.strip(),
            files_changed=list(self._files_changed),
            patch_diff=patch_diff,
            tests_passed=tests_passed,
            tests_output=tests_output,
            branch_name=branch_name,
            commit_sha=commit_sha,
            status=status,
        )

    # --- Internal ---

    def _build_system_prompt(self, error_signal: dict[str, Any]) -> str:
        parts: list[str] = []

        # Layer 1 — TRIAGE persona (persona.md → identity).  Best-effort.
        try:
            from pathlib import Path as _Path

            from axiom.agents.persona_loader import load_agent_persona

            _persona_dir = _Path(__file__).parent / "agents" / "triage"
            _persona_text = load_agent_persona(_persona_dir)
            if _persona_text:
                parts.append(_persona_text)
        except Exception:
            pass

        parts.append(_DOCTOR_SYSTEM_PROMPT.format(max_rounds=self.MAX_ROUNDS))

        # Load CLAUDE.md for project conventions
        claude_md = _REPO_ROOT / "CLAUDE.md"
        if claude_md.exists():
            try:
                content = claude_md.read_text(encoding="utf-8")[:4000]
                parts.append(f"\n## Project Conventions (CLAUDE.md excerpt)\n{content}")
            except OSError:
                pass

        return "\n".join(parts)

    def _build_initial_messages(self, error_signal: dict[str, Any]) -> list[dict]:
        """Build the initial user message from the error signal."""
        msg = (
            f"## Error Signal\n\n"
            f"**Command:** `{' '.join(error_signal.get('argv', []))}`\n"
            f"**Error type:** `{error_signal.get('error_type', 'unknown')}`\n"
            f"**Error message:** {error_signal.get('error_message', '')}\n"
            f"**Fingerprint:** `{error_signal.get('fingerprint', '')}`\n"
            f"**Recovered:** {error_signal.get('recovered', False)}\n\n"
            f"### Traceback\n```\n{error_signal.get('traceback', 'No traceback available')}\n```\n\n"
            f"### Environment\n"
        )
        env = error_signal.get("environment", {})
        for k, v in env.items():
            msg += f"- **{k}:** `{v}`\n"

        msg += (
            "\n\nDiagnose this error, fix the root cause, and run tests. "
            "Start by reading the file(s) mentioned in the traceback."
        )

        return [{"role": "user", "content": msg}]

    def _process_tools(
        self, response: Any,
    ) -> list[tuple[str, str, dict[str, Any]]]:
        """Execute tool calls and track state."""
        results = []
        for tool_block in response.tool_use:
            result = execute(tool_block.name, tool_block.input)

            # Track edits
            if tool_block.name == "edit_file" and "error" not in result:
                path = result.get("path", "")
                if path and path not in self._files_changed:
                    self._files_changed.append(path)
                diff = result.get("diff", "")
                if diff:
                    self._diffs.append(diff)

            # Print progress if interactive
            if sys.stdout.isatty():
                self._print_progress(tool_block.name, result)

            results.append((tool_block.tool_id, tool_block.name, result))
        return results

    def _assistant_message(self, response: Any) -> dict:
        """Build the assistant message with tool_calls for the next round."""
        msg: dict[str, Any] = {"role": "assistant", "content": response.text or ""}
        if response.tool_use:
            msg["tool_calls"] = [
                {
                    "id": t.tool_id,
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "arguments": json.dumps(t.input),
                    },
                }
                for t in response.tool_use
            ]
        return msg

    def _check_tests_passed(self, messages: list[dict]) -> bool:
        """Check if any run_tests call in the conversation returned passed=True."""
        for msg in messages:
            if msg.get("role") == "tool" and msg.get("name") == "run_tests":
                try:
                    data = json.loads(msg.get("content", "{}"))
                    if data.get("passed"):
                        return True
                except (json.JSONDecodeError, TypeError):
                    continue
        return False

    def _extract_test_output(self, messages: list[dict]) -> str:
        """Extract the most recent test output from the conversation."""
        for msg in reversed(messages):
            if msg.get("role") == "tool" and msg.get("name") == "run_tests":
                try:
                    data = json.loads(msg.get("content", "{}"))
                    return data.get("output", "")
                except (json.JSONDecodeError, TypeError):
                    continue
        return ""

    def _print_progress(self, tool_name: str, result: dict[str, Any]) -> None:
        """Print concise progress to stdout for interactive sessions."""
        if tool_name == "read_file_with_lines":
            path = result.get("path", "?")
            rng = result.get("range", "")
            print(f"  doctor: reading {path} [{rng}]", file=sys.stderr)
        elif tool_name == "search_files":
            count = result.get("count", 0)
            print(f"  doctor: search found {count} matches", file=sys.stderr)
        elif tool_name == "edit_file":
            if "error" in result:
                print(f"  doctor: edit blocked — {result['error']}", file=sys.stderr)
            else:
                path = result.get("path", "?")
                remaining = result.get("edits_remaining", "?")
                print(f"  doctor: edited {path} ({remaining} edits remaining)", file=sys.stderr)
        elif tool_name == "run_tests":
            passed = result.get("passed", False)
            status = "PASSED" if passed else "FAILED"
            print(f"  doctor: tests {status}", file=sys.stderr)
        elif tool_name == "git_commit_fix":
            if result.get("skipped"):
                print(f"  doctor: git skipped ({result.get('reason', '')})", file=sys.stderr)
            elif "error" in result:
                print(f"  doctor: git failed — {result['error']}", file=sys.stderr)
            else:
                branch = result.get("branch", "")
                print(f"  doctor: committed to {branch}", file=sys.stderr)
