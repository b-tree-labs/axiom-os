"""The node says when its local models disagree with each other (ADR-140).

Found on a production node, 2026-10-31: the code pinned one quick model, the
unit that ensures models pulled a different one, the serving env named the
pinned one, and the pulled model did not fit on the GPU beside the big engine.
Three sources, no comparison, a twelve-second "quick" tier. These tests hold
that every one of those disagreements is named, and that agreement is quiet.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from axiom.llm import local_models
from axiom.llm import model_alignment as ma

TAGS = {
    "models": [
        {"name": "gemma2:2b"},
        {"name": "nomic-embed-text:latest"},
        {"name": "qwen2.5:7b"},
        {"name": "gemma3:4b"},
    ]
}


def _unit(dir_: Path, *pulls: str, name: str = "axiom-ollama-models.service") -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    lines = [
        "[Service]",
        "ExecStartPre=/bin/sh -c 'until curl -sf http://127.0.0.1:11434/api/tags; do sleep 2; done'",
    ]
    lines += [f"ExecStart=/usr/local/bin/ollama pull {p}" for p in pulls]
    (dir_ / name).write_text("\n".join(lines) + "\n")
    return dir_


def _env(dir_: Path, name: str, **vars_: str) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    (dir_ / name).write_text("".join(f"{k}={v}\n" for k, v in vars_.items()))
    return dir_


@pytest.fixture
def pins():
    return ma.pinned()


def test_the_pins_are_the_three_roles(pins):
    assert pins == {
        "quick": local_models.QUICK,
        "reasoning": local_models.REASONING,
        "embed": local_models.EMBED,
    }
    assert set(local_models.OLLAMA_ENSURE_MODELS) == set(pins.values())


def test_a_unit_that_pulls_an_unpinned_model_is_named(tmp_path):
    units = _unit(tmp_path / "units", "gemma3:4b", "nomic-embed-text")
    env = _env(
        tmp_path / "env",
        "axiom-serve.env",
        NEUT_FAST_MODEL="gemma2:2b",
        STUDIO_ASK_MODEL="site-llm-v0.31",
        RAG_GEN_MODEL="qwen",
    )
    a = ma.check(unit_dirs=[units], env_dir=env, tags=TAGS)
    codes = {(f.code, f.message.split()[0]) for f in a.findings}
    assert ("ensure-unit-pulls-unpinned", "axiom-ollama-models.service") in codes
    assert any(
        f.code == "ensure-unit-pulls-unpinned" and "gemma3:4b" in f.message for f in a.findings
    )
    missing = {f.message.split()[3] for f in a.findings if f.code == "ensure-unit-missing-pin"}
    assert missing == {"gemma2:2b", "qwen2.5:7b"}
    # the env names the pinned model, the aliases are not Ollama tags: nothing to say about the env
    assert not [f for f in a.findings if f.code.startswith("env-")]
    assert a.ok


def test_an_env_variable_off_the_pin_warns_and_one_not_installed_fails(tmp_path):
    units = _unit(tmp_path / "units", *local_models.OLLAMA_ENSURE_MODELS)
    env = _env(
        tmp_path / "env", "axiom-serve.env", NEUT_FAST_MODEL="gemma3:4b", OTHER_MODEL="phi9:1b"
    )
    a = ma.check(unit_dirs=[units], env_dir=env, tags=TAGS)
    by_code = {f.code: f for f in a.findings}
    assert "gemma3:4b" in by_code["env-model-off-pin"].message
    assert by_code["env-model-off-pin"].severity == "warning"
    assert "phi9:1b" in by_code["env-model-not-installed"].message
    assert by_code["env-model-not-installed"].severity == "error"
    assert not a.ok


def test_latest_is_the_same_model():
    assert ma.norm("nomic-embed-text:latest") == "nomic-embed-text"
    assert ma.installed_models({"models": [{"name": "nomic-embed-text:latest"}]}) == {
        "nomic-embed-text"
    }


def test_agreement_is_quiet(tmp_path):
    units = _unit(tmp_path / "units", *local_models.OLLAMA_ENSURE_MODELS)
    env = _env(
        tmp_path / "env",
        "serve.env",
        NEUT_FAST_MODEL=local_models.QUICK,
        RAG_EMBED_MODEL=local_models.EMBED,
    )
    a = ma.check(unit_dirs=[units], env_dir=env, tags=TAGS)
    assert a.findings == []
    assert a.ok


def test_a_pin_missing_from_ollama_is_an_error(tmp_path):
    units = _unit(tmp_path / "units", *local_models.OLLAMA_ENSURE_MODELS)
    a = ma.check(
        unit_dirs=[units], env_dir=tmp_path / "env", tags={"models": [{"name": "gemma2:2b"}]}
    )
    errors = {f.message.split()[1] for f in a.findings if f.code == "pin-not-installed"}
    assert errors == {"reasoning", "embed"}
    assert not a.ok


def test_backup_and_example_env_files_are_not_live(tmp_path):
    units = _unit(tmp_path / "units", *local_models.OLLAMA_ENSURE_MODELS)
    env = tmp_path / "env"
    _env(env, "serve.env.bak-20261031", NEUT_FAST_MODEL="gemma3:4b")
    _env(env, "serve.env.example", NEUT_FAST_MODEL="gemma3:4b")
    _env(env, "serve.env", NEUT_FAST_MODEL=local_models.QUICK)
    a = ma.check(unit_dirs=[units], env_dir=env, tags=TAGS)
    assert [d[1] for d in a.declared] == ["NEUT_FAST_MODEL"]
    assert a.findings == []


def test_an_unreachable_ollama_is_said_not_guessed(tmp_path):
    def boom():
        raise OSError("connection refused")

    units = _unit(tmp_path / "units", *local_models.OLLAMA_ENSURE_MODELS)
    a = ma.check(unit_dirs=[units], env_dir=tmp_path / "env", tags=boom)
    assert a.installed is None
    assert [f.code for f in a.findings] == ["ollama-unreachable"]
    assert a.ok


def test_no_ensure_unit_is_information_not_failure(tmp_path):
    a = ma.check(unit_dirs=[tmp_path / "nounits"], env_dir=tmp_path / "env", tags=TAGS)
    assert [f.code for f in a.findings] == ["no-ensure-unit"]


def test_doctor_reports_the_worst_finding(monkeypatch):
    from axiom.cli import doctor

    def disagree(**_):
        a = ma.Alignment(pins=ma.pinned(), ensured={}, declared=[], installed=set())
        a.findings = [
            ma.Finding("warning", "ensure-unit-pulls-unpinned", "unit pulls x", "remove it"),
            ma.Finding("error", "pin-not-installed", "quick missing", "ollama pull q"),
        ]
        return a

    monkeypatch.setattr(ma, "check", disagree)
    r = doctor.check_model_alignment()
    assert r.status is doctor.CheckStatus.ERROR
    assert "quick missing" in r.summary
    assert r.fix_hint == "ollama pull q"
    assert r.detail and len(r.detail["findings"]) == 2


def test_doctor_is_ok_when_everything_agrees(monkeypatch):
    from axiom.cli import doctor

    monkeypatch.setattr(
        ma,
        "check",
        lambda **_: ma.Alignment(pins=ma.pinned(), ensured={}, declared=[], installed=set()),
    )
    r = doctor.check_model_alignment()
    assert r.status is doctor.CheckStatus.OK
    assert local_models.QUICK in r.summary
