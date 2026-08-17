# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for TRIAGE's CLI failure diagnosis catalog.

Closes the gap surfaced 2026-05-03: when `axi chat` failed with
"can't find bonsai," TRIAGE had no listener and no pattern, so the
next CLI invocation knew nothing. The pattern in this module makes
the failure → diagnosis → next-run-prompt loop concrete.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from axiom.extensions.builtins.diagnostics import cli_diagnoses


def _bonsai_chat_failure_event(
    *,
    error_type: str = "OSError",
    error_message: str = "[Errno 22] Invalid argument: bonsai-1.7b.gguf",
    command: str = "chat",
    extra_env: dict | None = None,
) -> dict:
    """Build a representative cli.arg_error event for the bonsai case."""
    env = {
        "python": "3.14.3",
        "platform": "macOS",
        "cwd": "/Users/example/Projects/workspace/axiom",
        "neut_version": "0.13.0",
    }
    if extra_env:
        env.update(extra_env)
    return {
        "command": command,
        "argv": ["axi", command],
        "error_type": error_type,
        "error_message": error_message,
        "traceback": (
            'Traceback (most recent call last):\n'
            '  File ".../chat/cli.py", line 12, in run\n'
            '    raise OSError(22, "Invalid argument", "bonsai-1.7b.gguf")\n'
            "OSError: [Errno 22] Invalid argument: 'bonsai-1.7b.gguf'\n"
        ),
        "fingerprint": "chat:OSError:bonsai",
        "recovered": False,
        "environment": env,
        "timestamp": datetime.now(UTC).isoformat(),
    }


class TestBonsaiPattern:
    """The originating pattern: bonsai-not-found should match + suggest qwen."""

    def test_bonsai_chat_failure_matches(self) -> None:
        event = _bonsai_chat_failure_event()

        diagnosis = cli_diagnoses.match_failure(event)

        assert diagnosis is not None
        assert diagnosis.pattern_id == "bonsai-deprecated"
        assert "bonsai" in diagnosis.summary.lower()
        assert "qwen" in diagnosis.remedy.lower()

    def test_bonsai_match_includes_concrete_remedy(self) -> None:
        event = _bonsai_chat_failure_event()

        diagnosis = cli_diagnoses.match_failure(event)

        assert diagnosis is not None
        # The remedy must be actionable, not vague: it should name the
        # specific config file the user edits + the replacement.
        assert "llm-providers.toml" in diagnosis.remedy
        assert "qwen-local" in diagnosis.remedy or "qwen2.5" in diagnosis.remedy

    def test_bonsai_match_high_confidence(self) -> None:
        event = _bonsai_chat_failure_event()

        diagnosis = cli_diagnoses.match_failure(event)

        # Bonsai gguf in the error message is unambiguous → high confidence.
        assert diagnosis.confidence >= 0.9

    def test_bonsai_message_only_no_traceback_still_matches(self) -> None:
        """Match should work on error_message alone — the traceback may
        be empty in some failure paths."""
        event = _bonsai_chat_failure_event()
        event["traceback"] = ""

        diagnosis = cli_diagnoses.match_failure(event)

        assert diagnosis is not None
        assert diagnosis.pattern_id == "bonsai-deprecated"


class TestNonMatching:
    """Patterns must be specific — false-positive on unrelated errors is worse
    than no diagnosis at all (it teaches users to ignore the surface)."""

    def test_unrelated_error_returns_none(self) -> None:
        event = {
            "command": "ext",
            "argv": ["axi", "ext", "list"],
            "error_type": "FileNotFoundError",
            "error_message": "[Errno 2] No such file: '/nope/manifest.toml'",
            "traceback": "",
            "fingerprint": "ext:FileNotFoundError:nope",
            "recovered": False,
            "environment": {},
            "timestamp": "2026-05-03T20:00:00Z",
        }

        assert cli_diagnoses.match_failure(event) is None

    def test_keyword_qwen_alone_does_not_trigger_bonsai(self) -> None:
        """Mentioning qwen in an error must not collide with the bonsai pattern."""
        event = {
            "command": "chat",
            "argv": ["axi", "chat"],
            "error_type": "ConnectionRefusedError",
            "error_message": "failed to start qwen2.5-7b: connection refused",
            "traceback": (
                "Traceback (most recent call last):\n"
                "  File '.../gateway.py', line 1, in fetch\n"
                "ConnectionRefusedError: localhost:8080\n"
            ),
            "fingerprint": "chat:ConnectionRefusedError:qwen",
            "recovered": False,
            "environment": {},
            "timestamp": "2026-05-03T20:00:00Z",
        }

        diagnosis = cli_diagnoses.match_failure(event)

        assert diagnosis is None or diagnosis.pattern_id != "bonsai-deprecated"

    def test_empty_event_returns_none(self) -> None:
        assert cli_diagnoses.match_failure({}) is None


class TestDiagnosisShape:
    def test_diagnosis_has_stable_fingerprint(self) -> None:
        """Two events with the same root cause → same diagnosis fingerprint,
        so the pre-command surface dedupes."""
        e1 = _bonsai_chat_failure_event()
        e2 = _bonsai_chat_failure_event(
            error_message="OSError: bonsai-1.7b.gguf cannot be opened"
        )

        d1 = cli_diagnoses.match_failure(e1)
        d2 = cli_diagnoses.match_failure(e2)

        assert d1 is not None and d2 is not None
        assert d1.fingerprint == d2.fingerprint

    def test_diagnosis_serialisable_to_dict(self) -> None:
        event = _bonsai_chat_failure_event()
        diagnosis = cli_diagnoses.match_failure(event)
        assert diagnosis is not None

        payload = diagnosis.to_dict()

        # Required fields a CLI surface or log consumer expects.
        for k in (
            "pattern_id",
            "summary",
            "remedy",
            "confidence",
            "fingerprint",
            "matched_at",
        ):
            assert k in payload


class TestCatalogShape:
    """The catalog must be enumerable — so a future audit (Coverage Manifest
    meta-row §4.2) can ask 'what failure modes does TRIAGE know about?'."""

    def test_catalog_is_iterable(self) -> None:
        patterns = list(cli_diagnoses.PATTERN_CATALOG)
        assert len(patterns) >= 1

    def test_catalog_entries_have_id_and_matcher(self) -> None:
        for entry in cli_diagnoses.PATTERN_CATALOG:
            assert hasattr(entry, "pattern_id")
            assert callable(entry.matcher)

    @pytest.mark.parametrize("required_id", [
        "bonsai-deprecated",
        "no-llm-provider-configured",
        "missing-api-key",
        "local-llamafile-down",
        "state-dir-permission-denied",
        "extension-module-import-error",
    ])
    def test_required_patterns_present(self, required_id: str) -> None:
        ids = {entry.pattern_id for entry in cli_diagnoses.PATTERN_CATALOG}
        assert required_id in ids


def _event(
    *,
    command: str = "ext",
    error_type: str = "RuntimeError",
    error_message: str = "",
    traceback: str = "",
) -> dict:
    return {
        "command": command,
        "argv": ["axi", command],
        "error_type": error_type,
        "error_message": error_message,
        "traceback": traceback,
        "fingerprint": f"{command}:{error_type}:x",
        "recovered": False,
        "environment": {},
        "timestamp": "2026-05-03T20:00:00Z",
    }


# ---------------------------------------------------------------------------
# Pattern: no-llm-provider-configured
# ---------------------------------------------------------------------------


class TestNoLLMProviderPattern:
    @pytest.mark.parametrize("msg", [
        "No LLM providers available or all failed.",
        "ValueError: no providers configured in gateway",
        "Configuration error: providers list is empty",
    ])
    def test_matches_common_phrasings(self, msg: str) -> None:
        d = cli_diagnoses.match_failure(_event(error_message=msg))
        assert d is not None
        assert d.pattern_id == "no-llm-provider-configured"

    def test_remedy_is_actionable(self) -> None:
        d = cli_diagnoses.match_failure(_event(error_message="No LLM providers available"))
        assert d is not None
        assert "axi config" in d.remedy or "llm-providers.toml" in d.remedy

    def test_does_not_match_generic_error(self) -> None:
        d = cli_diagnoses.match_failure(_event(error_message="some unrelated thing"))
        assert d is None or d.pattern_id != "no-llm-provider-configured"


# ---------------------------------------------------------------------------
# Pattern: missing-api-key
# ---------------------------------------------------------------------------


class TestMissingApiKeyPattern:
    def test_matches_keyerror_anthropic(self) -> None:
        d = cli_diagnoses.match_failure(_event(
            error_type="KeyError",
            error_message="'ANTHROPIC_API_KEY'",
            traceback="KeyError: 'ANTHROPIC_API_KEY' not found in environment\n",
        ))
        assert d is not None
        assert d.pattern_id == "missing-api-key:ANTHROPIC_API_KEY"

    def test_matches_missing_provider_env(self) -> None:
        d = cli_diagnoses.match_failure(_event(
            error_message="PRIVATE_LLM_API_KEY is not set",
        ))
        assert d is not None
        assert d.pattern_id == "missing-api-key:PRIVATE_LLM_API_KEY"
        assert "PRIVATE_LLM_API_KEY" in d.remedy

    def test_distinct_keys_distinct_fingerprints(self) -> None:
        """Two unset keys should not collide under one diagnosis fingerprint —
        they need different fixes."""
        d1 = cli_diagnoses.match_failure(_event(
            error_message="ANTHROPIC_API_KEY not set"))
        d2 = cli_diagnoses.match_failure(_event(
            error_message="OPENAI_API_KEY not set"))
        assert d1 is not None and d2 is not None
        assert d1.fingerprint != d2.fingerprint

    def test_doc_mention_of_api_key_does_not_match(self) -> None:
        """A coincidental mention of '*_API_KEY' without an absence signal
        must not trigger — false positives teach users to ignore the surface."""
        d = cli_diagnoses.match_failure(_event(
            error_message=(
                "See https://example.com/docs about ANTHROPIC_API_KEY — "
                "everything was fine"
            ),
        ))
        assert d is None or not d.pattern_id.startswith("missing-api-key")


# ---------------------------------------------------------------------------
# Pattern: local-llamafile-down
# ---------------------------------------------------------------------------


class TestLocalLlamafileDownPattern:
    def test_matches_chat_with_localhost_refused(self) -> None:
        """Most common shape: `axi chat` fails when local LLM server is
        down. Command is the discriminator, not the port number."""
        d = cli_diagnoses.match_failure(_event(
            command="chat",
            error_type="ConnectionError",
            error_message=(
                "HTTPConnectionPool(host='localhost', port=8080): "
                "Max retries exceeded with url: /v1/chat/completions "
                "(Caused by NewConnectionError(... Connection refused))"
            ),
        ))
        assert d is not None
        assert d.pattern_id == "local-llamafile-down"

    def test_matches_arbitrary_local_port(self) -> None:
        """Generalized matcher: any localhost port works. Avoids hardcoding
        Axiom-bundled vs ollama vs llamafile defaults."""
        d = cli_diagnoses.match_failure(_event(
            command="chat",
            error_message="Connection refused: 127.0.0.1:54321",
        ))
        assert d is not None
        assert d.pattern_id == "local-llamafile-down"

    def test_matches_when_blob_mentions_llm_keywords(self) -> None:
        """Even on a non-LLM-named command, an LLM-flavoured failure (e.g.,
        the gateway raised inside a different verb) should match."""
        d = cli_diagnoses.match_failure(_event(
            command="ext",
            error_message=(
                "gateway provider 'local-llamafile' refused connection: "
                "localhost:8080 (Connection refused)"
            ),
        ))
        assert d is not None
        assert d.pattern_id == "local-llamafile-down"

    def test_remote_endpoint_does_not_match(self) -> None:
        d = cli_diagnoses.match_failure(_event(
            command="chat",
            error_message=(
                "HTTPSConnectionPool(host='api.anthropic.com', port=443): "
                "Connection refused"
            ),
        ))
        assert d is None or d.pattern_id != "local-llamafile-down"

    def test_bonsai_takes_precedence(self) -> None:
        """When a connection-refused failure ALSO mentions bonsai, the
        bonsai-deprecated pattern is more specific and should win."""
        d = cli_diagnoses.match_failure(_event(
            command="chat",
            error_message=(
                "Connection refused: localhost:8081 "
                "(model: bonsai-1.7b.gguf)"
            ),
        ))
        assert d is not None
        assert d.pattern_id == "bonsai-deprecated"

    def test_no_localhost_no_match(self) -> None:
        """Connection refused without any localhost endpoint is too generic
        — could be a database, bus, peer node, etc."""
        d = cli_diagnoses.match_failure(_event(
            command="chat",
            error_message="Connection refused",
        ))
        assert d is None or d.pattern_id != "local-llamafile-down"

    def test_non_llm_command_with_generic_localhost_does_not_match(self) -> None:
        """`axi ext list` failing with a localhost connection refused is more
        likely a bus or database issue, not an LLM issue. Don't false-positive."""
        d = cli_diagnoses.match_failure(_event(
            command="ext",
            error_message="Connection refused: localhost:5432",
        ))
        assert d is None or d.pattern_id != "local-llamafile-down"


# ---------------------------------------------------------------------------
# Pattern: state-dir-permission-denied
# ---------------------------------------------------------------------------


class TestStateDirPermissionPattern:
    def test_matches_permission_error_axi_dir(self) -> None:
        d = cli_diagnoses.match_failure(_event(
            error_type="PermissionError",
            error_message="[Errno 13] Permission denied: '/Users/example/.axi/agents/tidy/sweep.jsonl'",
        ))
        assert d is not None
        assert d.pattern_id == "state-dir-permission-denied"

    def test_matches_neut_dir(self) -> None:
        d = cli_diagnoses.match_failure(_event(
            error_type="PermissionError",
            error_message="[Errno 13] Permission denied: '/home/user/.neut/identity/keypair.pem'",
        ))
        assert d is not None
        assert d.pattern_id == "state-dir-permission-denied"

    def test_remedy_includes_chown_command(self) -> None:
        d = cli_diagnoses.match_failure(_event(
            error_type="PermissionError",
            error_message="[Errno 13] Permission denied: '/Users/example/.axi/'",
        ))
        assert d is not None
        assert "chown" in d.remedy

    def test_permission_error_unrelated_path_does_not_match(self) -> None:
        d = cli_diagnoses.match_failure(_event(
            error_type="PermissionError",
            error_message="[Errno 13] Permission denied: '/var/log/system.log'",
        ))
        assert d is None or d.pattern_id != "state-dir-permission-denied"


# ---------------------------------------------------------------------------
# Pattern: extension-module-import-error
# ---------------------------------------------------------------------------


class TestModuleImportErrorPattern:
    def test_matches_axiom_extension_module_missing(self) -> None:
        d = cli_diagnoses.match_failure(_event(
            error_type="ModuleNotFoundError",
            error_message="No module named 'axiom.extensions.builtins.broken'",
            traceback=(
                "Traceback (most recent call last):\n"
                "  File '.../axiom_cli.py', line 880, in _dispatch_extension\n"
                "    mod = importlib.import_module(module_path)\n"
                "ModuleNotFoundError: No module named 'axiom.extensions.builtins.broken'\n"
            ),
        ))
        assert d is not None
        assert d.pattern_id == "extension-module-import-error"

    def test_remedy_mentions_axi_ext_lint(self) -> None:
        d = cli_diagnoses.match_failure(_event(
            error_type="ModuleNotFoundError",
            error_message="No module named 'axiom.extensions.builtins.foo'",
            traceback="ext_info dispatch failed in _dispatch_extension",
        ))
        assert d is not None
        assert "axi ext lint" in d.remedy

    def test_unrelated_import_error_does_not_match(self) -> None:
        """A generic third-party import error from user code shouldn't
        false-positive."""
        d = cli_diagnoses.match_failure(_event(
            error_type="ModuleNotFoundError",
            error_message="No module named 'numpy'",
            traceback="from numpy import array\nModuleNotFoundError\n",
        ))
        assert d is None or d.pattern_id != "extension-module-import-error"

    def test_extracts_missing_module_name(self) -> None:
        d = cli_diagnoses.match_failure(_event(
            error_type="ModuleNotFoundError",
            error_message="No module named 'axiom.extensions.builtins.ghost'",
            traceback="_dispatch_extension calling axiom.extensions.builtins.ghost",
        ))
        assert d is not None
        assert "axiom.extensions.builtins.ghost" in d.summary


# ---------------------------------------------------------------------------
# Catalog ordering — earlier patterns win on overlap
# ---------------------------------------------------------------------------


class TestCatalogOrdering:
    def test_only_one_diagnosis_per_event(self) -> None:
        """An event that could match two patterns gets the first match
        only — match_failure returns on the first hit, not all hits."""
        # Bonsai + connection-refused on :8081 — bonsai is earlier.
        d = cli_diagnoses.match_failure(_event(
            error_message=(
                "Connection refused: localhost:8081 "
                "(loading bonsai-1.7b.gguf failed)"
            ),
        ))
        assert d is not None
        assert d.pattern_id == "bonsai-deprecated"


class TestAptKeyringPattern:
    """apt repo with a missing/empty signing keyring (observed 2026-06-22)."""

    def test_no_pubkey_gpg_error_matches(self) -> None:
        event = {
            "error_message": (
                "W: GPG error: https://prod-cdn.packages.k8s.io/.../deb InRelease: "
                "The following signatures couldn't be verified because the public "
                "key is not available: NO_PUBKEY 234654DA9A296436"
            ),
        }
        diagnosis = cli_diagnoses.match_failure(event)
        assert diagnosis is not None
        assert diagnosis.pattern_id == "apt-keyring-missing"

    def test_not_signed_error_matches(self) -> None:
        event = {
            "error_message": (
                "E: The repository 'https://pkgs.k8s.io/core:/stable:/v1.32/deb "
                "InRelease' is not signed."
            ),
        }
        diagnosis = cli_diagnoses.match_failure(event)
        assert diagnosis is not None
        assert diagnosis.pattern_id == "apt-keyring-missing"

    def test_remedy_mentions_dearmor_yes(self) -> None:
        event = {"error_message": "GPG error: ... NO_PUBKEY ABC repository"}
        diagnosis = cli_diagnoses.match_failure(event)
        assert diagnosis is not None
        assert "--yes --dearmor" in diagnosis.remedy

    def test_no_pubkey_without_apt_context_does_not_match(self) -> None:
        # A bare GPG verification failure with no apt/repo signal shouldn't fire.
        event = {"error_message": "could not be verified: NO_PUBKEY in commit signature"}
        diagnosis = cli_diagnoses.match_failure(event)
        # commit-signature text has no apt context token -> may match other
        # patterns or none, but must not be a false apt-keyring hit on
        # unrelated GPG usage.
        if diagnosis is not None:
            assert diagnosis.pattern_id != "apt-keyring-missing"
