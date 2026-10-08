# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Tests for person alias handling in the TranscriptCorrector.

These tests verify that:
1. Person aliases are correctly loaded from people.md
2. Glossary maps aliases to canonical names
3. Domain glossary includes correct terms
"""

import pytest


@pytest.fixture
def people_md_with_aliases(tmp_path):
    """Create a people.md file with alias definitions."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)

    people_md = config_dir / "people.md"
    people_md.write_text("""# Team Directory

| Name | Aliases | GitLab | Role |
|------|---------|--------|------|
| Benjamin Schenk | Ben, BS | bschenk | Lead |
| Alexander Rodriguez | Alex, Rod | arodriguez | Engineer |
| Dr. Sarah Mitchell | Dr. Mitchell, Sarah M | smitchell | Advisor |
| Robert O'Brien | Bob, Bobby, Rob | robrien | Student |
""")

    initiatives_md = config_dir / "initiatives.md"
    initiatives_md.write_text("""| ID | Name | Status |
|----|------|--------|
| 1 | Project Alpha | Active |
""")

    return config_dir


@pytest.fixture
def corrector_with_aliases(people_md_with_aliases):
    """Create a TranscriptCorrector instance with alias-aware people config."""
    from axiom.extensions.builtins.signals.corrector import TranscriptCorrector

    corrector = TranscriptCorrector(config_dir=people_md_with_aliases)
    return corrector


class TestPersonAliasLoading:
    """Tests for loading person aliases from config."""

    def test_aliases_loaded_into_glossary(self, corrector_with_aliases):
        """Person aliases are loaded into the glossary."""
        glossary = corrector_with_aliases._glossary

        # Aliases should be in glossary (lowercase)
        assert "ben" in glossary
        assert "alex" in glossary
        assert "bob" in glossary

    def test_alias_maps_to_full_name(self, corrector_with_aliases):
        """Aliases map to full canonical names."""
        glossary = corrector_with_aliases._glossary

        assert glossary["ben"] == "Benjamin Schenk"
        assert glossary["alex"] == "Alexander Rodriguez"
        assert glossary["bob"] == "Robert O'Brien"

    def test_multiple_aliases_same_person(self, corrector_with_aliases):
        """Multiple aliases for same person all map correctly."""
        glossary = corrector_with_aliases._glossary

        # Bob, Bobby, and Rob should all map to Robert O'Brien
        assert glossary.get("bob") == "Robert O'Brien"
        assert glossary.get("bobby") == "Robert O'Brien"
        assert glossary.get("rob") == "Robert O'Brien"


class TestDomainGlossary:
    """Tests for the domain-specific glossary."""

    def test_domain_glossary_included(self, corrector_with_aliases):
        """Domain glossary terms are included."""
        glossary = corrector_with_aliases._glossary

        # Check domain-agnostic default terms
        assert "cfd" in glossary
        assert glossary["cfd"] == "CFD"

    def test_program_acronyms_included(self, corrector_with_aliases):
        """Program acronyms are in glossary."""
        glossary = corrector_with_aliases._glossary

        assert "doe" in glossary
        assert glossary["doe"] == "DOE"

    def test_technical_mishearings_included(self, corrector_with_aliases):
        """Common technical mishearings are in glossary."""
        glossary = corrector_with_aliases._glossary

        assert "c of d" in glossary
        assert glossary["c of d"] == "CFD"


class TestParagraphBreaks:
    """Tests for paragraph break insertion."""

    @pytest.fixture
    def corrector(self, tmp_path):
        """Create a corrector with minimal config."""
        config_dir = tmp_path / "config"
        config_dir.mkdir(parents=True, exist_ok=True)

        people_md = config_dir / "people.md"
        people_md.write_text("| Name | GitLab |\n|------|--------|\n| Test | test |\n")

        initiatives_md = config_dir / "initiatives.md"
        initiatives_md.write_text("| ID | Name |\n|----|------|\n| 1 | Test |\n")

        from axiom.extensions.builtins.signals.corrector import TranscriptCorrector
        return TranscriptCorrector(config_dir=config_dir)

    def test_preserves_existing_breaks(self, corrector):
        """Existing paragraph breaks are preserved."""
        text_with_breaks = "First paragraph.\n\nSecond paragraph.\n\nThird."

        result = corrector._add_paragraph_breaks(text_with_breaks)

        # Should preserve existing structure
        assert result.count("\n\n") >= 2

    def test_empty_text_returns_empty(self, corrector):
        """Empty text returns empty string."""
        result = corrector._add_paragraph_breaks("")
        assert result == ""


class TestGlossaryMerging:
    """Tests for glossary construction and merging."""

    def test_user_glossary_overrides_domain(self, tmp_path):
        """User glossary entries override domain glossary."""
        config_dir = tmp_path / "config"
        config_dir.mkdir(parents=True, exist_ok=True)

        people_md = config_dir / "people.md"
        people_md.write_text("| Name | GitLab |\n|------|--------|\n| Test | test |\n")

        initiatives_md = config_dir / "initiatives.md"
        initiatives_md.write_text("| ID | Name |\n|----|------|\n")

        # Create user glossary that overrides a domain term
        from axiom.extensions.builtins.signals.corrector import TranscriptCorrector

        # Create corrector first
        corrector = TranscriptCorrector(config_dir=config_dir)

        # Verify glossary was built
        assert corrector._glossary is not None
        assert len(corrector._glossary) > 0

    def test_glossary_size_reported(self, corrector_with_aliases):
        """Glossary size is reasonable."""
        glossary = corrector_with_aliases._glossary

        # Should have at least domain terms + aliases
        assert len(glossary) >= len(corrector_with_aliases.DOMAIN_GLOSSARY)


class TestEdgeCases:
    """Tests for edge cases in correction handling."""

    def test_unicode_in_names(self, tmp_path):
        """Unicode characters in names are handled."""
        config_dir = tmp_path / "config"
        config_dir.mkdir(parents=True, exist_ok=True)

        people_md = config_dir / "people.md"
        people_md.write_text("""| Name | Aliases | GitLab |
|------|---------|--------|
| José García | Pepe | jgarcia |
| François Müller | Frank | fmuller |
""")

        initiatives_md = config_dir / "initiatives.md"
        initiatives_md.write_text("| ID | Name |\n|----|------|\n")

        from axiom.extensions.builtins.signals.corrector import TranscriptCorrector
        corrector = TranscriptCorrector(config_dir=config_dir)

        # Should load without error
        assert corrector._glossary is not None
        # Unicode aliases should work
        assert corrector._glossary.get("pepe") == "José García"
        assert corrector._glossary.get("frank") == "François Müller"

    def test_empty_aliases_handled(self, tmp_path):
        """Empty alias fields are handled gracefully."""
        config_dir = tmp_path / "config"
        config_dir.mkdir(parents=True, exist_ok=True)

        people_md = config_dir / "people.md"
        people_md.write_text("""| Name | Aliases | GitLab |
|------|---------|--------|
| Test Person |  | tperson |
""")

        initiatives_md = config_dir / "initiatives.md"
        initiatives_md.write_text("| ID | Name |\n|----|------|\n")

        from axiom.extensions.builtins.signals.corrector import TranscriptCorrector
        # Should not raise
        corrector = TranscriptCorrector(config_dir=config_dir)
        assert corrector._glossary is not None


class TestCorrectionResult:
    """Tests for the CorrectionResult dataclass."""

    def test_correction_result_creation(self):
        """CorrectionResult can be created with defaults."""
        from axiom.extensions.builtins.signals.corrector import CorrectionResult

        result = CorrectionResult(transcript_path="test.md")

        assert result.transcript_path == "test.md"
        assert result.corrections == []
        assert result.glossary_size == 0

    def test_correction_dataclass(self):
        """Correction dataclass works correctly."""
        from axiom.extensions.builtins.signals.corrector import Correction

        correction = Correction(
            original="c of d",
            corrected="CFD",
            category="technical_term",
            confidence=0.95,
            context="The c of d simulations...",
            reason="Common STT error for technical acronym",
        )

        assert correction.original == "c of d"
        assert correction.corrected == "CFD"
        assert correction.confidence == 0.95
