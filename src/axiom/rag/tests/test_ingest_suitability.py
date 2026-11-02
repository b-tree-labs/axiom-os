# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Some content should never reach a vector index.

A live corpus reached 5,304,997 chunks with 92,690 distinct checksums — 98.25%
duplicate. Five sensor-log files accounted for 2.8 million of them. The
million-copy chunk was 686 bytes of this:

    168   167.9   167.9   167.8   167.7   167.8   167.8   167.8 ...

Flow-loop telemetry, ingested as prose, sliced into windows, each embedded into
a 768-dimension float32 vector and indexed. It is already structured data and
belongs in the signals tables; embedding it destroys the structure and answers
no question a vector search is good at.

Deleting the rows treats the symptom. Nothing stopped this being embedded and
nothing stopped the same chunk being written a million times, so both recur.

The guard has to cut in both directions. One that admits nothing would be
"safe" and would silently stop the corpus working; the tests below hold both
ends.
"""

from __future__ import annotations

from axiom.rag.ingest_suitability import (
    DISTINCT_TOKEN_RATIO_LIMIT,
    MIN_TOKENS,
    NUMERIC_FRACTION_LIMIT,
    SINGLE_CHAR_TOKEN_LIMIT,
    assess_chunk,
    numeric_fraction,
    screen_chunks,
    sql_measure_lateral,
    unsuitable_predicate_sql,
    unsuitable_reason_sql,
)

TELEMETRY = (
    "168     167.9   167.9   167.8   167.7   167.8   167.8   167.8   167.8   "
    "167.8   167.8   167.8   167.8   167.9   168     168     167.9   167.8"
)

PROSE = (
    "The reactor scrammed at 14:32 following a rod drift alarm on the transient "
    "rod. Operators confirmed the shutdown was orderly and the coolant loop "
    "remained within its analyzed envelope throughout the transient."
)

MIXED = (
    "Peak power during the run reached 1.04 MW at 14:47, which is within the "
    "0.99 to 1.09 MW band the analysis allows. The operator logged the event."
)


def test_telemetry_is_refused():
    """The case that cost 70 GB."""
    verdict = assess_chunk(TELEMETRY)
    assert verdict.suitable is False
    assert "numeric" in verdict.reason.lower()


def test_prose_is_admitted():
    """The negative control, and the more important one: a guard that admits
    nothing stops the corpus working while looking cautious."""
    assert assess_chunk(PROSE).suitable is True


def test_prose_containing_numbers_is_admitted():
    """Real engineering prose is full of figures. Refusing it would drop exactly
    the quantitative content the platform exists to answer over."""
    verdict = assess_chunk(MIXED)
    assert verdict.suitable is True, verdict.reason


def test_numeric_fraction_measures_tokens_not_characters():
    """A long number is one token. Counting characters would make any document
    with a few timestamps look like telemetry."""
    assert numeric_fraction("1234567890 alpha beta gamma") == 0.25
    assert numeric_fraction("alpha beta") == 0.0


def test_an_empty_chunk_is_refused_and_says_why():
    verdict = assess_chunk("   ")
    assert verdict.suitable is False
    assert "empty" in verdict.reason.lower()


def test_a_chunk_of_repeated_identical_tokens_is_refused():
    """A window of one repeated reading carries no retrievable distinction, and
    a million of them crowd out everything else."""
    verdict = assess_chunk("167.8 " * 40)
    assert verdict.suitable is False


def test_the_verdict_carries_the_numbers_behind_it():
    """A refusal a human cannot check is one they will switch off."""
    verdict = assess_chunk(TELEMETRY)
    assert 0.0 <= verdict.numeric_fraction <= 1.0
    assert verdict.distinct_token_ratio is not None
    assert verdict.tokens > 0


def test_the_threshold_is_adjustable_and_the_default_is_stated():
    """A site with genuinely numeric prose can move it, and the default is not
    hidden in a conditional."""
    from axiom.rag.ingest_suitability import NUMERIC_FRACTION_LIMIT

    assert 0.5 <= NUMERIC_FRACTION_LIMIT <= 0.95
    assert assess_chunk(TELEMETRY, numeric_limit=1.01).suitable is True


# --- dedup on write -----------------------------------------------------------


def test_a_repeated_checksum_is_refused_once_seen():
    """The same chunk was written 1,036,802 times from one file. The checksum
    column to prevent it already existed and was never consulted."""
    from axiom.rag.ingest_suitability import DedupFilter

    f = DedupFilter()
    assert f.accept("abc") is True
    assert f.accept("abc") is False
    assert f.accept("def") is True
    assert f.seen == 3 and f.rejected == 1


def test_the_dedup_filter_reports_what_it_stopped():
    """A filter that silently drops is indistinguishable from a bug."""
    from axiom.rag.ingest_suitability import DedupFilter

    f = DedupFilter()
    for _ in range(5):
        f.accept("same")
    assert f.rejected == 4
    assert "4" in f.summary()


# --- broken OCR: found by validating against the real corpus ------------------

OCR_MUSH = (
    "i o n s tlnat gave t h e I-owest f u e l c y c l e c o s t , and then, "
    "without a p p r e c i a b l y i n c r e a s i n g t h i s c o s t"
)


def test_broken_ocr_is_refused_by_its_own_name():
    """Found by running the guard over the real corpus, not over fixtures.

    A 1966 scanned report produced chunks like the above: a failed OCR pass that
    spaced every character. It is genuinely unsuitable — it would pollute
    retrieval — but the distinct-token rule refused it incidentally, reporting
    "a repeated reading carries no retrievable distinction", which is not what
    is wrong with it.

    A refusal whose stated reason does not match the defect is one an operator
    will read as a bug and switch off.
    """
    verdict = assess_chunk(OCR_MUSH)
    assert verdict.suitable is False
    assert "ocr" in verdict.reason.lower() or "character" in verdict.reason.lower()


def test_ordinary_prose_with_short_words_is_not_mistaken_for_ocr():
    """The control. English is full of one and two letter words, and a rule
    that counted them would refuse most writing."""
    verdict = assess_chunk(
        "It is a fact that we do not go to the lab on a day when it is shut, "
        "so the run was on the next day and it went as we had set it up."
    )
    assert verdict.suitable is True, verdict.reason


def test_the_real_1966_report_is_largely_admitted():
    """Measured: 197 of 200 sampled chunks from that report pass. The guard
    must not be refusing a historical document wholesale."""
    good = (
        "The reactor was operated at power for a total of 1,247 hours during "
        "the report period. Fuel element inspections were completed on schedule "
        "and no anomalies were identified in the graphite reflector assembly."
    )
    assert assess_chunk(good).suitable is True


# --- the status console: found when the OCR rule mislabelled it ---------------

STATUS_CONSOLE = (
    "t 4 Radial Two      Open      3 3 Operator logged in:            1       "
    "Beam Port 5 thru 1   Secure   3 3   Rod withdrawn   2   Bridge   1"
)


def test_reactor_status_console_is_not_called_ocr():
    """A fixed-width status dump is mostly one-DIGIT readings. The first
    version of the OCR rule counted any single-character token and refused 19
    of these in a 400-chunk sample under the words "a failed OCR pass" — which
    is not what is wrong with them, and they are plausibly retrievable
    ("when was Beam Port 5 open?").

    Single digits belong to the numeric rule, which owns the judgement about
    whether a chunk is readings rather than writing.
    """
    verdict = assess_chunk(STATUS_CONSOLE)
    assert "ocr" not in verdict.reason.lower(), verdict.reason


def test_ocr_detection_still_fires_on_isolated_letters():
    """The control in the other direction: narrowing to letters must not make
    the rule inert."""
    assert assess_chunk(OCR_MUSH).suitable is False
    assert "letter" in assess_chunk(OCR_MUSH).reason.lower()


# --- screen_chunks: the seam ingest actually calls -----------------------------


class _Chunk:
    def __init__(self, text):
        self.text = text


def test_screen_chunks_drops_repeats_and_reports_both_kinds():
    chunks = [
        _Chunk("The reactor was operated at power for 1,247 hours this period."),
        _Chunk("The reactor was operated at power for 1,247 hours this period."),
        _Chunk("168 167.9 167.9 167.8 167.7 167.8 167.8 167.8 167.9 168.0 168.1"),
        _Chunk("Fuel element inspections completed on schedule with no anomalies."),
    ]
    kept, report = screen_chunks(chunks)
    assert [c.text for c in kept] == [chunks[0].text, chunks[3].text]
    assert report.offered == 4
    assert report.kept == 2
    assert report.refused_duplicate == 1
    assert report.refused_unsuitable == 1
    assert "numeric" in str(report.by_reason)


def test_screen_chunks_can_be_turned_off_and_then_changes_nothing():
    """The escape hatch has to be exact. An operator who disables the guard to
    unblock an ingest must get the pre-guard behaviour, not a quieter guard."""
    chunks = [_Chunk("1 2 3 4 5 6"), _Chunk("1 2 3 4 5 6")]
    kept, report = screen_chunks(chunks, enabled=False)
    assert len(kept) == 2
    assert report.kept == 2
    assert report.refused == 0


def test_screen_report_summary_names_the_numbers():
    """A drop an operator cannot account for reads as data loss."""
    _, report = screen_chunks([_Chunk("9 9 9 9 9 9 9 9")])
    assert "1 offered" in report.summary()
    assert "0 kept" in report.summary()


def test_screen_chunks_on_an_empty_list_is_not_an_error():
    kept, report = screen_chunks([])
    assert kept == []
    assert report.offered == 0
    assert report.summary() == ""


# --- the guard as ingest actually calls it ------------------------------------


def test_ingest_stats_report_names_refused_chunks():
    """A guard whose effect never reaches the operator's screen is a guard
    nobody can tell is working, or misfiring."""
    from axiom.rag.ingest import IngestStats

    stats = IngestStats(chunks_refused=12, refused_by_reason={"numeric": 9, "ocr": 3})
    report = stats.drop_report()
    assert "12 chunks refused" in report
    assert "numeric=9" in report


def test_ingest_stats_add_accumulates_refusals_across_files():
    from axiom.rag.ingest import IngestStats

    total = IngestStats()
    total += IngestStats(chunks_refused=3, refused_by_reason={"numeric": 3})
    total += IngestStats(chunks_refused=2, refused_by_reason={"numeric": 1, "ocr": 1})
    assert total.chunks_refused == 5
    assert total.refused_by_reason == {"numeric": 4, "ocr": 1}


def test_a_clean_run_says_nothing_about_the_guard():
    """No refusals must leave the report empty, or every ingest grows noise
    that trains people to skip it."""
    from axiom.rag.ingest import IngestStats

    assert IngestStats(files_indexed=4, chunks_created=40).drop_report() == ""


# --- end to end through the real chunker and the real ingest_file -------------
#
# The fixtures are synthesised, not copied. The thresholds were calibrated
# against a live 5.3-million-chunk corpus (numbers in
# docs/working/rag-storage-milestones-2026-09-23.md), but that corpus holds
# partner loop data and site safety-analysis text, and this repository has a
# daily public mirror. What travels here is the SHAPE, measured from it.


def _telemetry_file(tmp_path, lines=60):
    """A sensor log of the shape that reached 1,036,802 chunks from one file:
    tab-separated one-decimal readings drifting slowly around a setpoint."""
    rows = []
    value = 167.5
    for i in range(lines):
        value += ((i * 7) % 5 - 2) * 0.1
        rows.append("\t".join(f"{value + (j % 3) * 0.1:.1f}" for j in range(120)))
    path = tmp_path / "loop_readings.txt"
    path.write_text("\n".join(rows))
    return path


def _prose_file(tmp_path, paragraphs=105):
    """Engineering prose of comparable size — including figures, because a
    guard that refuses quantitative writing drops exactly what the platform
    exists to answer over."""
    body = []
    for i in range(paragraphs):
        body.append(
            f"During period {i + 1} the reactor was operated at power for a total of "
            f"{1200 + i * 3} hours. Fuel element inspections were completed on schedule "
            f"and the graphite reflector assembly showed no measurable degradation. "
            f"Coolant inlet temperature averaged {38 + i % 4} degrees Celsius against a "
            f"licensed limit of 48 degrees, and the reciprocal source multiplication "
            f"factor was determined by positive period measurements before each startup."
        )
    path = tmp_path / "progress_report.txt"
    path.write_text("\n\n".join(body))
    return path


class _RecordingStore:
    """Stands in for RAGStore. Records what ingest tried to write, which is the
    only thing this test is about."""

    def __init__(self):
        self.written = []

    def get_document(self, rel_path):
        return None

    def upsert_chunks(self, chunks, embeddings, **kw):
        self.written.extend(chunks)


def test_end_to_end_a_telemetry_file_adds_no_chunks(tmp_path, monkeypatch):
    """M4a's first half. The file that motivated this guard contributed
    1,036,802 chunks to one index. Through the real chunker and the real
    ingest_file, it must now contribute none."""
    from axiom.rag import ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "embed_texts", lambda texts: None)
    store = _RecordingStore()
    stats = ingest_mod.ingest_file(_telemetry_file(tmp_path), store)

    assert store.written == [], "telemetry reached the index"
    assert stats.files_indexed == 0
    assert stats.chunks_created == 0
    assert stats.chunks_refused > 0
    assert "numeric" in stats.refused_by_reason


def test_end_to_end_prose_of_similar_size_still_ingests_fully(tmp_path, monkeypatch):
    """M4a's second half, and the one that matters more. A guard that admits
    nothing is as broken as one that admits everything, and it fails silently.

    Both files are written in the same test run at comparable size, so a
    threshold change that starts refusing prose fails HERE rather than in a
    quiet corpus six months later.
    """
    from axiom.rag import ingest as ingest_mod

    monkeypatch.setattr(ingest_mod, "embed_texts", lambda texts: None)
    telemetry = _telemetry_file(tmp_path)
    prose = _prose_file(tmp_path)
    # Comparable size is the whole point of the control, so it is asserted
    # rather than assumed: a fixture that drifts small would let a
    # threshold change pass here and refuse real documents in the field.
    assert abs(telemetry.stat().st_size - prose.stat().st_size) < prose.stat().st_size * 0.25

    store = _RecordingStore()
    stats = ingest_mod.ingest_file(prose, store)

    assert stats.files_indexed == 1
    assert stats.chunks_created > 0
    assert stats.chunks_refused == 0, f"prose was refused: {stats.refused_by_reason}"
    assert len(store.written) == stats.chunks_created


def test_end_to_end_the_guard_can_be_switched_off(tmp_path, monkeypatch):
    """The escape hatch, proven through the same path an operator would use."""
    from axiom.rag import ingest as ingest_mod
    from axiom.rag.ingest_suitability import GUARD_ENV

    monkeypatch.setattr(ingest_mod, "embed_texts", lambda texts: None)
    monkeypatch.setenv(GUARD_ENV, "0")
    store = _RecordingStore()
    stats = ingest_mod.ingest_file(_telemetry_file(tmp_path), store)

    assert stats.files_indexed == 1
    assert stats.chunks_refused == 0
    assert store.written, "disabling the guard must restore the pre-guard behaviour"


# --- the SQL face of the same rules -------------------------------------------


def test_sql_predicate_embeds_the_live_thresholds():
    """The reaper that removes already-stored chunks and the guard that keeps
    new ones out must apply the same rules, or the corpus converges on
    whichever is laxer.

    They cannot drift here because the SQL is generated from the constants.
    This asserts that generation, so a threshold edited in one place cannot
    quietly leave the other behind.
    """
    sql = unsuitable_predicate_sql()
    assert str(NUMERIC_FRACTION_LIMIT) in sql
    assert str(DISTINCT_TOKEN_RATIO_LIMIT) in sql
    assert str(SINGLE_CHAR_TOKEN_LIMIT) in sql
    assert str(MIN_TOKENS) in sql


def test_sql_reason_arms_are_in_the_same_order_as_python():
    """A chunk can trip several rules. Both faces must report the same one, or
    the same row is 'numeric' to the reaper and 'OCR' to ingest."""
    sql = unsuitable_reason_sql()
    # The quoted literals, not the bare words: "numeric" also appears in the
    # ::numeric casts, which would make this pass for the wrong reason.
    order = [sql.index(f"'{k}'") for k in ("too-short", "ocr", "numeric", "repeated-reading")]
    assert order == sorted(order), "SQL CASE arms diverge from assess_chunk's order"


def test_sql_is_parameter_free():
    """It is interpolated into a maintenance statement, so it must carry no
    placeholder that a caller could forget to bind."""
    sql = unsuitable_predicate_sql() + unsuitable_reason_sql()
    assert "%s" not in sql
    assert "?" not in sql


def test_sql_measures_tokenize_each_row_once():
    """The first version called regexp_split_to_array four times per row — once
    per measure — and a full pass over 5.3 million chunks had not finished in
    25 minutes. A maintenance query nobody can afford to run is a guard that
    only applies to new content.

    One split, one unnest.
    """
    sql = sql_measure_lateral()
    assert sql.count("regexp_split_to_array") == 1
    assert sql.count("unnest(") == 1


def test_sql_measures_still_expose_every_name_the_predicate_uses():
    """The predicate and the measures are written separately and joined by
    name. A rename in one is a runtime error in the other, not a test failure,
    so the join is asserted here."""
    lateral = sql_measure_lateral()
    for name in ("n", "numeric_n", "distinct_n", "single_n"):
        assert f"AS {name}" in lateral, name
