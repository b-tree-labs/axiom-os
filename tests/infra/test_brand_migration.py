# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""The platform reads platform-named settings, and says so when it meets an old one.

``axiom.infra.brand_migration`` is the single place that still knows the
retired names a downstream product left in the platform. Behaviour under test:

- :func:`getenv` returns the value of the platform name, and falls through to
  the caller's default when it is unset;
- a retired name set with no replacement produces exactly one notice naming
  both names, on stderr *and* through the platform logger;
- a retired name that carries a credential is a hard error instead, because
  continuing means running unauthenticated with a credential the operator
  believed was in force;
- no notice, on either tier, ever contains the value;
- a retired name set *alongside* its replacement never overrides the
  replacement, and never errors: the credential is in force under the new name;
- every renamed variable is read under its new name at its real call site;
- ``get_project_root`` anchors on the platform's own dot-directory, and says so
  when it walks past a retired one;
- the guard in :func:`_consumer_name_sites` fails on a consumer-named
  environment variable or directory literal anywhere in non-test source, passes
  on the tree as it stands, and fails on a stale exemption.
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

import pytest

from axiom.infra import brand_migration
from axiom.infra.brand_migration import (
    CONSUMER_DIR_RE,
    CONSUMER_ENV_RE,
    CONSUMER_NAME_EXEMPT,
    LEGACY_ENV_VARS,
    LEGACY_PROJECT_DIR,
    LegacyEnvVarError,
    getenv,
)

SRC_ROOT = Path(__file__).resolve().parents[2] / "src"


@pytest.fixture(autouse=True)
def _fresh_notices():
    """Every test starts with an empty once-only ledger."""
    brand_migration.reset_notices()
    yield
    brand_migration.reset_notices()


@pytest.fixture
def clean_env(monkeypatch):
    """Clear every name in the map, new and retired, for this test."""
    for new, spec in LEGACY_ENV_VARS.items():
        monkeypatch.delenv(new, raising=False)
        monkeypatch.delenv(spec.legacy, raising=False)
    return monkeypatch


# ---------------------------------------------------------------------------
# The map itself
# ---------------------------------------------------------------------------


class TestTheMap:
    def test_every_entry_names_a_platform_variable_and_a_retired_one(self):
        for new, spec in LEGACY_ENV_VARS.items():
            assert new.startswith(("AXIOM_", "AXI_")), new
            assert CONSUMER_ENV_RE.fullmatch(spec.legacy), spec.legacy

    def test_no_two_entries_share_a_retired_name(self):
        legacy = [spec.legacy for spec in LEGACY_ENV_VARS.values()]
        assert len(legacy) == len(set(legacy))

    def test_every_entry_carries_a_reason(self):
        assert all(spec.reason.strip() for spec in LEGACY_ENV_VARS.values())

    def test_the_secret_bearing_entries_are_the_credential_ones(self):
        """Only a credential gets the hard-error tier; a path or a URL does not."""
        secrets = {new for new, spec in LEGACY_ENV_VARS.items() if spec.secret}
        assert secrets == {"AXIOM_RAG_EMBED_KEY", "AXIOM_UPDATE_REGISTRY_TOKEN"}

    def test_every_platform_name_is_actually_read_somewhere_in_source(self):
        """A map entry whose new name no call site reads is a claim gone false."""
        text = "\n".join(
            p.read_text(encoding="utf-8")
            for p in sorted((SRC_ROOT / "axiom").rglob("*.py"))
            if "__pycache__" not in p.parts and p.name != "brand_migration.py"
        )
        missing = [new for new in LEGACY_ENV_VARS if f'"{new}"' not in text]
        assert not missing, f"mapped but never read: {missing}"


# ---------------------------------------------------------------------------
# getenv
# ---------------------------------------------------------------------------


class TestGetenv:
    def test_returns_the_platform_variables_value(self, clean_env):
        clean_env.setenv("AXIOM_RAG_EMBED_URL", "https://embed.example:42000")
        assert getenv("AXIOM_RAG_EMBED_URL") == "https://embed.example:42000"

    def test_returns_the_default_when_nothing_is_set(self, clean_env):
        assert getenv("AXIOM_RAG_EMBED_URL") is None
        assert getenv("AXIOM_RAG_EMBED_URL", "fallback") == "fallback"

    def test_an_unmapped_name_behaves_like_os_environ_get(self, clean_env):
        clean_env.setenv("AXIOM_NOT_IN_THE_MAP", "v")
        assert getenv("AXIOM_NOT_IN_THE_MAP") == "v"
        assert getenv("AXIOM_ALSO_NOT_IN_THE_MAP", "d") == "d"

    def test_the_retired_name_is_never_read_as_a_value(self, clean_env):
        clean_env.setenv("NEUT_EMBED_URL", "https://old.example:42000")
        assert getenv("AXIOM_RAG_EMBED_URL", "unset") == "unset"


class TestTheNoticeForARetiredName:
    def test_it_names_both_the_old_name_and_the_new_one(self, clean_env, capsys):
        clean_env.setenv("NEUT_SCRATCH_DIR", "/srv/scratch")
        getenv("AXIOM_HYGIENE_SCRATCH_DIR")

        err = capsys.readouterr().err
        assert "NEUT_SCRATCH_DIR" in err
        assert "AXIOM_HYGIENE_SCRATCH_DIR" in err

    def test_it_is_emitted_exactly_once_however_often_the_value_is_read(self, clean_env, capsys):
        clean_env.setenv("NEUT_SCRATCH_DIR", "/srv/scratch")
        for _ in range(5):
            getenv("AXIOM_HYGIENE_SCRATCH_DIR")

        assert capsys.readouterr().err.count("NEUT_SCRATCH_DIR") == 1

    def test_it_also_reaches_the_platform_logger(self, clean_env, caplog):
        """Stderr is what the operator sees; the log is what a snapshot keeps."""
        clean_env.setenv("NEUT_SCRATCH_DIR", "/srv/scratch")
        with caplog.at_level(logging.WARNING, logger="axiom.infra.brand_migration"):
            getenv("AXIOM_HYGIENE_SCRATCH_DIR")

        assert any("NEUT_SCRATCH_DIR" in r.getMessage() for r in caplog.records)

    def test_it_never_prints_the_value(self, clean_env, capsys):
        clean_env.setenv("NEUT_SCRATCH_DIR", "/srv/a-very-distinctive-path")
        getenv("AXIOM_HYGIENE_SCRATCH_DIR")

        assert "a-very-distinctive-path" not in capsys.readouterr().err

    def test_each_variable_gets_its_own_notice(self, clean_env, capsys):
        clean_env.setenv("NEUT_SCRATCH_DIR", "/srv/scratch")
        clean_env.setenv("NEUT_GITLAB_PROJECT", "group/proj")
        getenv("AXIOM_HYGIENE_SCRATCH_DIR")
        getenv("AXIOM_GITLAB_PROJECT")

        err = capsys.readouterr().err
        assert "NEUT_SCRATCH_DIR" in err
        assert "NEUT_GITLAB_PROJECT" in err


class TestARetiredCredential:
    def test_it_raises_rather_than_running_unauthenticated(self, clean_env):
        clean_env.setenv("NEUT_EMBED_KEY", "sk-secret-value")
        with pytest.raises(LegacyEnvVarError):
            getenv("AXIOM_RAG_EMBED_KEY")

    def test_the_error_names_both_names(self, clean_env):
        clean_env.setenv("NEUT_EMBED_KEY", "sk-secret-value")
        with pytest.raises(LegacyEnvVarError) as excinfo:
            getenv("AXIOM_RAG_EMBED_KEY")

        assert "NEUT_EMBED_KEY" in str(excinfo.value)
        assert "AXIOM_RAG_EMBED_KEY" in str(excinfo.value)

    def test_the_error_never_carries_the_value(self, clean_env):
        clean_env.setenv("NEUT_REGISTRY_TOKEN", "glpat-do-not-echo-me")
        with pytest.raises(LegacyEnvVarError) as excinfo:
            getenv("AXIOM_UPDATE_REGISTRY_TOKEN")

        assert "glpat-do-not-echo-me" not in str(excinfo.value)


class TestBothNamesSet:
    def test_the_replacement_wins(self, clean_env):
        clean_env.setenv("NEUT_EMBED_URL", "https://old.example")
        clean_env.setenv("AXIOM_RAG_EMBED_URL", "https://new.example")
        assert getenv("AXIOM_RAG_EMBED_URL") == "https://new.example"

    def test_a_credential_set_under_both_names_does_not_raise(self, clean_env):
        clean_env.setenv("NEUT_EMBED_KEY", "old-key")
        clean_env.setenv("AXIOM_RAG_EMBED_KEY", "new-key")
        assert getenv("AXIOM_RAG_EMBED_KEY") == "new-key"

    def test_an_empty_replacement_still_counts_as_set(self, clean_env):
        """``AXIOM_RAG_EMBED_URL=""`` is a deliberate 'off', not an absence."""
        clean_env.setenv("NEUT_EMBED_URL", "https://old.example")
        clean_env.setenv("AXIOM_RAG_EMBED_URL", "")
        assert getenv("AXIOM_RAG_EMBED_URL", "unset") == ""

    def test_the_operator_is_still_told_the_old_one_is_ignored(self, clean_env, capsys):
        clean_env.setenv("NEUT_EMBED_URL", "https://old.example")
        clean_env.setenv("AXIOM_RAG_EMBED_URL", "https://new.example")
        getenv("AXIOM_RAG_EMBED_URL")

        err = capsys.readouterr().err
        assert "NEUT_EMBED_URL" in err
        assert "AXIOM_RAG_EMBED_URL" in err

    def test_that_notice_never_prints_either_value(self, clean_env, capsys):
        clean_env.setenv("NEUT_EMBED_KEY", "old-secret")
        clean_env.setenv("AXIOM_RAG_EMBED_KEY", "new-secret")
        getenv("AXIOM_RAG_EMBED_KEY")

        err = capsys.readouterr().err
        assert "old-secret" not in err
        assert "new-secret" not in err


# ---------------------------------------------------------------------------
# The renamed variables, at their real call sites
# ---------------------------------------------------------------------------


class TestCallSitesReadTheNewName:
    def test_hygiene_scratch_dir(self, clean_env, tmp_path):
        from axiom.extensions.builtins.hygiene.paths import resolve_base_dir

        clean_env.setenv("AXIOM_HYGIENE_SCRATCH_DIR", str(tmp_path / "scratch"))
        assert resolve_base_dir() == tmp_path / "scratch"

    def test_rag_embed_url(self, clean_env):
        from axiom.rag import embeddings

        clean_env.setenv("AXIOM_RAG_EMBED_URL", "https://embed.example:42000")
        assert embeddings._provider_configured() is True

    def test_rag_embed_model_and_key_reach_the_request(self, clean_env, monkeypatch):
        from axiom.rag import embeddings

        clean_env.setenv("AXIOM_RAG_EMBED_URL", "https://embed.example:42000")
        clean_env.setenv("AXIOM_RAG_EMBED_MODEL", "custom-embed")
        clean_env.setenv("AXIOM_RAG_EMBED_KEY", "sk-live")

        seen: dict = {}

        class _Resp:
            status_code = 200

            @staticmethod
            def json():
                return {"data": [{"index": 0, "embedding": [0.1, 0.2]}]}

        class _Requests:
            @staticmethod
            def post(url, **kwargs):
                seen.update(url=url, headers=kwargs["headers"], payload=kwargs["json"])
                return _Resp()

        monkeypatch.setitem(__import__("sys").modules, "requests", _Requests)

        out = embeddings._embed_remote(["hello"])

        assert out == [[0.1, 0.2]]
        assert seen["payload"]["model"] == "custom-embed"
        assert seen["headers"]["Authorization"] == "Bearer sk-live"

    def test_install_environment_override(self, clean_env):
        from axiom.extensions.builtins.install.installer import Environment, detect_environment

        envs = [Environment(name="staging"), Environment(name="prod", default=True)]
        clean_env.setenv("AXIOM_INSTALL_ENV", "staging")
        assert detect_environment(envs).name == "staging"

    def test_teams_session_dir(self, clean_env, tmp_path):
        from axiom.extensions.builtins.signals.extractors.teams_browser import (
            _resolve_session_dir,
        )

        clean_env.setenv("AXIOM_TEAMS_SESSION_DIR", str(tmp_path / "teams"))
        assert _resolve_session_dir() == tmp_path / "teams"

    def test_box_session_dir(self, clean_env, tmp_path):
        from axiom.extensions.builtins.publishing.providers.storage.box_browser import (
            BoxBrowserStorageProvider,
        )

        clean_env.setenv("AXIOM_BOX_SESSION_DIR", str(tmp_path / "box"))
        assert BoxBrowserStorageProvider().session_dir == tmp_path / "box"

    def test_gitlab_project(self, clean_env):
        from axiom.infra.subscribers.gitlab_issues import GitLabIssueProvider

        clean_env.setenv("AXIOM_GITLAB_PROJECT", "group/subgroup/proj")
        assert GitLabIssueProvider()._project_path == "group/subgroup/proj"

    def test_update_registry_url_and_token(self, clean_env, monkeypatch):
        from axiom.extensions.builtins.update import version_check

        clean_env.setenv("AXIOM_UPDATE_REGISTRY_URL", "https://registry.example/simple")
        clean_env.setenv("AXIOM_UPDATE_REGISTRY_TOKEN", "tok")

        seen: dict = {}

        class _Req:
            def __init__(self, url):
                seen["url"] = url
                self.headers: dict = {}

            def add_header(self, k, v):
                self.headers[k] = v

        monkeypatch.setattr("urllib.request.Request", _Req)
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop here")),
        )

        assert version_check.VersionChecker()._check_pypi_registry(1.0) is None
        assert seen["url"] == "https://registry.example/simple"

    def test_log_ring_capacity(self, clean_env, monkeypatch):
        from axiom.infra import axiom_logging

        monkeypatch.setattr(axiom_logging, "_ring_buffer", None)
        clean_env.setenv("AXIOM_LOG_RING_CAPACITY", "7")
        try:
            assert axiom_logging._get_or_create_ring()._buf.maxlen == 7
        finally:
            monkeypatch.setattr(axiom_logging, "_ring_buffer", None)

    def test_log_forensic_dir_and_cooldown_are_read_under_the_new_names(self):
        source = (SRC_ROOT / "axiom" / "infra" / "axiom_logging.py").read_text(encoding="utf-8")
        assert '"AXIOM_LOG_FORENSIC_DIR"' in source
        assert '"AXIOM_LOG_SNAPSHOT_COOLDOWN_S"' in source

    def test_user_state_dir_override(self, clean_env, tmp_path):
        from axiom.infra.paths import get_user_state_dir

        clean_env.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
        assert get_user_state_dir() == tmp_path / "state"

    def test_the_user_state_dir_name_is_not_derived_from_a_brand_at_read_time(
        self, clean_env, tmp_path
    ):
        """Operators need a stable variable name, not one that moves with branding."""
        from axiom.infra import branding
        from axiom.infra.paths import get_user_state_dir

        branding.register(branding.BrandingConfig(cli_name="acme", product_name="Acme"))
        try:
            clean_env.setenv("ACME_STATE_DIR", str(tmp_path / "wrong"))
            clean_env.setenv("AXI_STATE_DIR", str(tmp_path / "right"))
            assert get_user_state_dir() == tmp_path / "right"
        finally:
            branding.reset()


# ---------------------------------------------------------------------------
# The on-disk anchor
# ---------------------------------------------------------------------------


class TestProjectAnchor:
    def test_the_project_dir_name_comes_from_the_active_branding(self):
        from axiom.infra import branding
        from axiom.infra.paths import project_dir_name

        assert project_dir_name() == ".axi"
        branding.register(branding.BrandingConfig(cli_name="acme"))
        try:
            assert project_dir_name() == ".acme"
        finally:
            branding.reset()

    def test_the_platform_dot_directory_anchors_the_project_root(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AXIOM_ROOT", raising=False)
        from axiom.infra.paths import get_project_root

        project = tmp_path / "proj"
        (project / ".axi").mkdir(parents=True)
        nested = project / "a" / "b"
        nested.mkdir(parents=True)

        assert get_project_root(nested) == project

    def test_a_retired_anchor_does_not_silently_become_the_project_root(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.delenv("AXIOM_ROOT", raising=False)
        from axiom.infra.paths import get_project_root

        project = tmp_path / "proj"
        (project / LEGACY_PROJECT_DIR).mkdir(parents=True)
        nested = project / "a"
        nested.mkdir(parents=True)

        assert get_project_root(nested) == nested

    def test_walking_past_a_retired_anchor_says_so_naming_both(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("AXIOM_ROOT", raising=False)
        from axiom.infra.paths import get_project_root

        project = tmp_path / "proj"
        (project / LEGACY_PROJECT_DIR).mkdir(parents=True)
        nested = project / "a"
        nested.mkdir(parents=True)

        get_project_root(nested)

        err = capsys.readouterr().err
        assert LEGACY_PROJECT_DIR in err
        assert ".axi" in err

    def test_the_project_state_dir_hangs_off_the_root(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AXIOM_ROOT", raising=False)
        from axiom.infra.paths import get_project_state_dir

        project = tmp_path / "proj"
        (project / ".git").mkdir(parents=True)

        assert get_project_state_dir(project) == project / ".axi"

    def test_a_retired_project_state_dir_is_reported_once(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("AXIOM_ROOT", raising=False)
        from axiom.infra.paths import get_project_state_dir

        project = tmp_path / "proj"
        (project / ".git").mkdir(parents=True)
        (project / LEGACY_PROJECT_DIR).mkdir(parents=True)

        for _ in range(3):
            get_project_state_dir(project)

        assert capsys.readouterr().err.count("no longer reads it") == 1

    def test_no_notice_when_the_platform_dir_is_the_one_on_disk(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.delenv("AXIOM_ROOT", raising=False)
        from axiom.infra.paths import get_project_state_dir

        project = tmp_path / "proj"
        (project / ".git").mkdir(parents=True)
        (project / ".axi").mkdir(parents=True)

        get_project_state_dir(project)

        assert capsys.readouterr().err == ""

    def test_a_consumer_brand_keeps_its_own_directory_without_a_notice(
        self, tmp_path, monkeypatch, capsys
    ):
        """A downstream product's own dot-directory is not a retired name."""
        monkeypatch.delenv("AXIOM_ROOT", raising=False)
        from axiom.infra import branding
        from axiom.infra.paths import get_project_state_dir

        branding.register(branding.BrandingConfig(cli_name=LEGACY_PROJECT_DIR[1:]))
        try:
            project = tmp_path / "proj"
            (project / ".git").mkdir(parents=True)
            (project / LEGACY_PROJECT_DIR).mkdir(parents=True)

            assert get_project_state_dir(project) == project / LEGACY_PROJECT_DIR
            assert capsys.readouterr().err == ""
        finally:
            branding.reset()


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """Ids of the string constants that are docstrings, not values."""
    out: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        body = getattr(node, "body", None)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            out.add(id(body[0].value))
    return out


def _consumer_name_sites(root: Path) -> dict[str, list[tuple[int, str, str]]]:
    """Every consumer-named env var or directory literal under *root*.

    Keyed by path relative to ``src/``, valued by ``(lineno, kind, literal)``.
    Docstrings are skipped: prose that carries the name changes no behaviour and
    rides along with whatever item next touches the file. Test trees are skipped
    for the same reason a test may set a retired variable on purpose.
    """
    found: dict[str, list[tuple[int, str, str]]] = {}
    for path in sorted((root / "axiom").rglob("*.py")):
        if "__pycache__" in path.parts or "tests" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docs = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docs:
                continue
            kinds = []
            if CONSUMER_ENV_RE.search(node.value):
                kinds.append("env")
            if CONSUMER_DIR_RE.search(node.value):
                kinds.append("dir")
            if kinds:
                rel = path.relative_to(root).as_posix()
                found.setdefault(rel, []).append((node.lineno, "+".join(kinds), node.value[:60]))
    return found


class TestTheGuard:
    def test_the_tree_carries_no_unexempted_consumer_name(self):
        found = _consumer_name_sites(SRC_ROOT)
        unexpected = {rel: sites for rel, sites in found.items() if rel not in CONSUMER_NAME_EXEMPT}
        assert not unexpected, (
            "consumer-named environment variable or directory literal in "
            "non-test source:\n"
            + "\n".join(
                f"  {rel}:{lineno} [{kind}] {literal!r}"
                for rel, sites in sorted(unexpected.items())
                for lineno, kind, literal in sites
            )
            + "\n\nRead settings through axiom.infra.brand_migration.getenv under a "
            "platform name, and build project paths with "
            "axiom.infra.paths.get_project_state_dir()."
        )

    def test_the_exemption_list_has_no_stale_entries(self):
        found = _consumer_name_sites(SRC_ROOT)
        stale = sorted(set(CONSUMER_NAME_EXEMPT) - set(found))
        assert not stale, (
            f"CONSUMER_NAME_EXEMPT names files with no consumer name left: {stale}. "
            "Drop the entries."
        )

    def test_every_exemption_carries_a_reason(self):
        assert all(reason.strip() for reason in CONSUMER_NAME_EXEMPT.values())

    def test_the_migration_module_is_the_only_infra_exemption(self):
        infra = sorted(p for p in CONSUMER_NAME_EXEMPT if p.startswith("axiom/infra/"))
        assert infra == ["axiom/infra/brand_migration.py"]

    def test_an_introduced_env_var_would_be_caught(self, tmp_path):
        """Prove it bites: a new consumer-named read in a fresh file is a violation."""
        pkg = tmp_path / "axiom"
        pkg.mkdir()
        (pkg / "offender.py").write_text(
            'import os\n\n\ndef f():\n    return os.environ.get("NEUT_NEW_THING", "")\n',
            encoding="utf-8",
        )

        found = _consumer_name_sites(tmp_path)

        assert found == {"axiom/offender.py": [(5, "env", "NEUT_NEW_THING")]}

    def test_an_introduced_directory_literal_would_be_caught(self, tmp_path):
        pkg = tmp_path / "axiom"
        pkg.mkdir()
        (pkg / "offender.py").write_text(
            'from pathlib import Path\n\nP = Path(".neut") / "cache"\n',
            encoding="utf-8",
        )

        found = _consumer_name_sites(tmp_path)

        assert list(found) == ["axiom/offender.py"]
        assert found["axiom/offender.py"][0][1] == "dir"

    def test_a_docstring_is_not_a_violation(self, tmp_path):
        """Prose rides along with the next item to touch the file."""
        pkg = tmp_path / "axiom"
        pkg.mkdir()
        (pkg / "prose.py").write_text(
            '"""Reads NEUT_OLD_THING from .neut/ once upon a time."""\n',
            encoding="utf-8",
        )

        assert _consumer_name_sites(tmp_path) == {}

    def test_a_test_tree_is_not_scanned(self, tmp_path):
        pkg = tmp_path / "axiom" / "tests"
        pkg.mkdir(parents=True)
        (pkg / "test_x.py").write_text('X = "NEUT_OLD_THING"\n', encoding="utf-8")

        assert _consumer_name_sites(tmp_path) == {}

    def test_a_module_path_that_merely_contains_the_name_is_not_a_directory(self, tmp_path):
        """``tools.neut_cli`` is an identifier, not a dot-directory."""
        pkg = tmp_path / "axiom"
        pkg.mkdir()
        (pkg / "m.py").write_text('CMD = "python -m tools.neut_cli"\n', encoding="utf-8")

        assert _consumer_name_sites(tmp_path) == {}

    def test_a_stale_exemption_fails_the_stale_check(self):
        """The stale check is the half that keeps the list honest."""
        found = _consumer_name_sites(SRC_ROOT)
        pretend = dict(CONSUMER_NAME_EXEMPT)
        pretend["axiom/infra/gone_away.py"] = "no longer exists"

        assert sorted(set(pretend) - set(found)) == ["axiom/infra/gone_away.py"]


class TestTheGuardPatterns:
    @pytest.mark.parametrize(
        "literal",
        ["NEUT_EMBED_URL", "env://NEUT_PG_PASSWORD", "set NEUT_X to enable"],
    )
    def test_env_pattern_matches(self, literal):
        assert CONSUMER_ENV_RE.search(literal)

    @pytest.mark.parametrize("literal", ["AXIOM_RAG_EMBED_URL", "neutral", "NEUTRON"])
    def test_env_pattern_does_not_match(self, literal):
        assert not CONSUMER_ENV_RE.search(literal)

    @pytest.mark.parametrize(
        "literal", [".neut", "~/.neut/credentials", ".neut/publisher/workflow.yaml"]
    )
    def test_dir_pattern_matches(self, literal):
        assert CONSUMER_DIR_RE.search(literal)

    @pytest.mark.parametrize("literal", ["tools.neut_cli", ".neutron", "a.neuter", ".axi"])
    def test_dir_pattern_does_not_match(self, literal):
        assert not CONSUMER_DIR_RE.search(literal)


class TestNoConsumerNameOutsideTheMigrationModule:
    def test_the_retired_names_live_in_exactly_one_module(self):
        """Nineteen call sites knowing the old names is what this replaced."""
        offenders = []
        for path in sorted((SRC_ROOT / "axiom").rglob("*.py")):
            if "__pycache__" in path.parts or "tests" in path.parts:
                continue
            rel = path.relative_to(SRC_ROOT).as_posix()
            if rel in CONSUMER_NAME_EXEMPT:
                continue
            text = path.read_text(encoding="utf-8")
            if any(spec.legacy in text for spec in LEGACY_ENV_VARS.values()):
                offenders.append(rel)
        assert offenders == []


def test_the_probe_regexes_are_anchored_the_way_the_map_expects():
    """Guard against a pattern loose enough to match the platform's own names."""
    assert re.search(CONSUMER_ENV_RE, "AXIOM_LOG_RING_CAPACITY") is None
    assert re.search(CONSUMER_DIR_RE, str(Path.home() / ".axi")) is None


def test_a_retired_name_never_wins_over_the_platform_name(monkeypatch, tmp_path):
    """Every retired name set at once still leaves the platform name in charge."""
    for spec in LEGACY_ENV_VARS.values():
        monkeypatch.setenv(spec.legacy, "sentinel")
    monkeypatch.setenv("AXI_STATE_DIR", str(tmp_path / "state"))
    brand_migration.reset_notices()

    from axiom.infra.paths import get_user_state_dir

    assert get_user_state_dir() == tmp_path / "state"
