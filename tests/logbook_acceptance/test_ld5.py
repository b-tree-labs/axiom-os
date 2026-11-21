# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Acceptance tests for unit LD-5: archive a sealed period and verify it offline (#1762,
ADR-251 D7).

The world is LD-1's (one signed record in the demo log at site-a) and LD-4's blob store, plus a
second record carrying an attachment and a range seal over both, signed, which anchors the site.
The demo log is given a ``[seal]`` for the test and put back after. The archive store is a local
directory through the storage-provider abstraction.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import logging
import subprocess
import sys
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

fastapi = pytest.importorskip("fastapi")

from axiom.extensions.builtins.attest import (  # noqa: E402
    archives,
    attachments,
    gold,
    index,
    registry,
    service,
    store,
)
from axiom.extensions.builtins.attest import doors as log_doors  # noqa: E402
from axiom.extensions.builtins.attest.logbooks import PeriodDecl, SealDecl  # noqa: E402
from axiom.extensions.builtins.attest.skills import bind_default  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_api import OP1, OP2, as_  # noqa: E402
from axiom.extensions.builtins.attest.tests.test_signing import _sign  # noqa: E402
from axiom.infra import audit_trail  # noqa: E402
from axiom.infra.principal import PrincipalContext  # noqa: E402
from axiom.infra.skill_dispatch import invoke_capability  # noqa: E402
from axiom.infra.skills import SkillContext  # noqa: E402
from axiom.infra.storage import provider_from_config  # noqa: E402
from axiom.infra.switchplan import SwitchRefused  # noqa: E402

from . import test_ld1 as ld1  # noqa: E402
from . import test_ld4 as ld4  # noqa: E402
from .test_ld1 import LOG, ME, SITE  # noqa: E402

world = ld1.world
restore_log = ld1.restore_log
blob_store = ld4.blob_store

ROOT = Path(__file__).resolve().parents[2]
SUMMARY = "Two rounds, both in order."
OTHER = "@op2:site-a"


def _sealable(**over):
    book = registry.get(LOG)
    seal = SealDecl(by_roles=("operator",), posture="sso", fresh_within_seconds=None, boundary="range")
    registry.register(replace(book, seal=seal, **over))


@pytest.fixture
def arch(world, restore_log, blob_store, node_signer, tmp_path, monkeypatch):
    """Records #1 (LD-1's) and #2 (a photo attached), sealed by #3, which anchored the site."""
    _sealable()
    monkeypatch.setenv("AXI_CHANGE_LEDGER", str(tmp_path / "changes.jsonl"))
    cold = tmp_path / "cold"
    archives.set_target(provider_from_config({"base_dir": str(cold)}))
    photo = ld4._signed(node_signer, (ld4.PHOTO, "image/png", {"name": "pump.png"}))
    draft = service.create_seal_draft(site_id=SITE, logbook=LOG, boundary="range", from_seq=1, to_seq=2,
                                      summary=SUMMARY, origin="human", for_principal=ME)  # fmt: skip
    sealed = _sign(draft, node_signer)
    assert sealed.status == "signed" and sealed.anchor is not None
    yield {**world, "photo": photo, "seal": sealed.record, "cold": cold, "tmp": tmp_path, "blobs": blob_store}
    archives.set_target(None)
    archives.set_target(None, name="vault")
    archives._packages.clear()  # noqa: SLF001


def _ctx(state: Path, handle: str | None, surface: str = "cli") -> SkillContext:
    principal = PrincipalContext(handle=handle, posture="sso", assured=True) if handle else None
    return SkillContext(registry=bind_default(), state_dir=state, logger=logging.getLogger("ld5"),
                        principal=principal, actor=handle or "", surface=surface)  # fmt: skip


def _skill(w, name, params, handle=ME, surface="cli"):
    c = _ctx(w["state"], handle, surface)
    return invoke_capability(c.registry, name, params, c, surface=surface)


def _package(w, built) -> Path:
    """The stored package, as a person would download it."""
    return next(p for p in w["cold"].rglob("*.zip") if built["package_digest"] in p.name)


def _unpack(w, built, name="pkg") -> Path:
    out = w["tmp"] / name
    with zipfile.ZipFile(_package(w, built)) as z:
        z.extractall(out)
    return out


def _offline(path: Path, scripts: Path | None = None) -> subprocess.CompletedProcess:
    """The package's own verifier, on a machine with no Axiom: isolated, no site packages, no
    environment (so no database address either). ``scripts``: where its two scripts were unpacked
    when ``path`` is the zip itself."""
    script = (scripts or path) / "verify_archive.py"
    return subprocess.run([sys.executable, "-I", "-S", str(script), str(path)], cwd=str(script.parent),
                          env={}, capture_output=True, text=True, timeout=300, check=False)  # fmt: skip


def _committed(w):
    built = archives.build(SITE, LOG, by=ME)
    archives.verify(built["archive_id"], by=ME)
    archives.commit(built["archive_id"], owner=ME)
    assert archives.soak_check(built["archive_id"], owner=ME) == "soaked"
    return archives.get(built["archive_id"])


def _retired(w):
    a = _committed(w)
    return archives.retire(a["archive_id"], approver=ME)


def _reads(w, door_id):
    return [r for r in audit_trail.read_chain(audit_trail.audit_path(w["state"]))
            if (r.get("access") or {}).get("door") == door_id]  # fmt: skip


# -- LD-5-A1: verifies offline with its stdlib-only script ---------------------------------------


@pytest.mark.acceptance("LD-5-A1")
def test_ld5_a1_the_package_verifies_offline_with_no_axiom_and_no_database(arch, monkeypatch):
    built = archives.build(SITE, LOG, by=ME)
    pkg = _unpack(arch, built)
    # No Axiom on that machine: the interpreter the check runs in cannot import it.
    probe = subprocess.run([sys.executable, "-I", "-S", "-c", "import axiom"], env={}, cwd=str(pkg),
                           capture_output=True, text=True, check=False)  # fmt: skip
    assert probe.returncode != 0 and "No module named 'axiom'" in probe.stderr
    for target in (pkg, _package(arch, built)):  # unpacked, and the zip itself
        done = _offline(target, pkg)
        assert done.returncode == 0, done.stdout + done.stderr
        report = json.loads(done.stdout)
        assert report["ok"] and report["package_digest"] == built["package_digest"]
        assert (report["records"], report["attachments"]) == (3, 1) and report["anchors"] >= 1
        assert report["head"] == {"seq": arch["seal"]["seq"], "digest": arch["seal"]["digest"]}
    # And in this process with the database gone: the verifier never asks for one.
    def no_database(*_a, **_k):
        raise AssertionError("the offline verifier asked for the database")

    monkeypatch.setattr(store, "session_scope", no_database)
    spec = importlib.util.spec_from_file_location("ld5_offline", pkg / "verify_archive.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.verify(pkg)["ok"]


def _rewrite(pkg: Path, name: str, mutate) -> None:
    """Change a file and re-seal the manifest around it, as a forger with the package would: the
    file and package digests then agree, so only the signatures, links and anchors can catch it."""
    path = pkg / name
    path.write_bytes(mutate(path.read_bytes()))
    man = json.loads((pkg / "manifest.json").read_text())
    import hashlib

    man["files"][name] = hashlib.sha256(path.read_bytes()).hexdigest()
    man["package_digest"] = archives.package_digest(man)
    (pkg / "manifest.json").write_text(json.dumps(man))


@pytest.mark.acceptance("LD-5-A1")
def test_ld5_a1_every_digest_signature_link_attachment_and_anchor_is_checked(arch):
    built = archives.build(SITE, LOG, by=ME)
    sha = attachments.refs_of(arch["photo"])[0]["sha256"]

    def outcome(name, mutate):
        pkg = _unpack(arch, built, name=f"t-{abs(hash(name))}")
        mutate(pkg)
        done = _offline(pkg)
        return done.returncode, json.loads(done.stdout)["reason"]

    def body(b):  # change a word the person signed
        return b.replace(b'"reading":"ok"', b'"reading":"no"', 1)

    assert outcome("record", lambda p: _rewrite(p, "records.jsonl", body)) == (1, "digest_mismatch")

    def drop_first(b):  # a link: lose the first record
        return b.split(b"\n", 1)[1]

    assert outcome("link", lambda p: _rewrite(p, "records.jsonl", drop_first)) == (1, "seq_gap")
    blob = f"attachments/sha256/{sha}"
    assert outcome("blob", lambda p: (p / blob).write_bytes(b"x"))[1] == f"file_altered:{blob}"
    assert outcome("blob-resealed", lambda p: _rewrite(p, blob, lambda b: b + b"x"))[1] == f"attachment_altered:{sha}"

    def lose_blob(p):
        (p / blob).unlink()
        man = json.loads((p / "manifest.json").read_text())
        del man["files"][blob]
        man["package_digest"] = archives.package_digest(man)
        (p / "manifest.json").write_text(json.dumps(man))

    assert outcome("blob-gone", lose_blob)[1] == f"attachment_missing:{sha}"

    def forge_anchor(b):
        a = json.loads(b)
        a["root"] = "0" * 64
        return (json.dumps(a) + "\n").encode()

    assert outcome("anchor", lambda p: _rewrite(p, "anchors.jsonl", forge_anchor))[1] == "anchor_root_mismatch"
    assert outcome("extra", lambda p: (p / "notes.txt").write_text("hi"))[1] == "unlisted_file:notes.txt"
    # The package names this log only: no other log's head travels in an anchor.
    pkg = _unpack(arch, built, name="clean")
    for line in (pkg / "anchors.jsonl").read_text().splitlines():
        a = json.loads(line)
        assert set(a) == {"anchor_id", "created_at", "root", "node_sig", "head", "proof"}
        assert a["head"]["logbook"] == LOG


# -- LD-5-A2: retire refuses without a stored verify report; a person's act through the runner ---


@pytest.mark.acceptance("LD-5-A2")
def test_ld5_a2_retire_refuses_without_a_stored_verify_report_for_this_digest(arch):
    built = archives.build(SITE, LOG, by=ME)
    with pytest.raises(archives.ArchiveRefused, match="no stored verify report"):
        archives.retire(built["archive_id"], approver=ME)
    # Committing verifies first: the runner's gate is the stored report.
    a = _committed(arch)
    assert a["status"] == "archived" and a["verified"]
    # A report stored for another digest is not a report for this package.
    with store.session_scope() as s:
        row = s.get(archives.AttestArchive, a["archive_id"])
        row.verify_report = {**row.verify_report, "package_digest": "f" * 64}
        s.commit()
    with pytest.raises(archives.ArchiveRefused, match="no stored verify report"):
        archives.retire(a["archive_id"], approver=ME)
    assert archives.get(a["archive_id"])["status"] == "archived"


@pytest.mark.acceptance("LD-5-A2")
def test_ld5_a2_retire_is_a_named_persons_act_through_the_runner(arch):
    a = _committed(arch)
    for nobody in ("", "@system", "system"):
        with pytest.raises(SwitchRefused, match="approver"):
            archives.retire(a["archive_id"], approver=nobody)
    done = archives.retire(a["archive_id"], approver=OTHER)
    assert (done["status"], done["retired_by"]) == ("retired", OTHER)
    rec = archives.plan(a["archive_id"], owner=ME).record
    assert rec.phase == "retired" and rec.approver == OTHER and rec.retire_class == "reclaim"
    steps = [e["step"] for e in rec.events]
    assert steps.index("prove") < steps.index("committed") < steps.index("retired")
    # Through the skill, the person names the package they retire.
    b = _skill(arch, "attest.archive_retire", {"archive_id": a["archive_id"], "package_digest": "0" * 64})
    assert not b.ok and "not this archive's package" in b.errors[0]


@pytest.mark.acceptance("LD-5-A2")
def test_ld5_a2_a_retire_with_no_change_record_is_refused(arch, tmp_path):
    a = _committed(arch)
    (tmp_path / "changes.jsonl").write_text("")  # the change record is gone
    with pytest.raises(archives.ArchiveRefused, match="change record"):
        archives.retire(a["archive_id"], approver=ME)
    assert archives.get(a["archive_id"])["status"] == "archived"


# -- LD-5-A3: after retire, every door rehydrates from the package, same labels, same check -------


@pytest.mark.acceptance("LD-5-A3")
def test_ld5_a3_after_retire_every_door_serves_the_record_from_the_package(arch):
    a = _retired(arch)
    source = f"archive:sha256:{a['package_digest']}"
    rec, labels = arch["rec"], {"classification": "internal", "export_controlled": False}
    assert archives.manifest(a["archive_id"])["labels"] == labels
    # The HTTP record door.
    c = arch["client"]
    c.cookies.clear()
    r = as_(c, OP1).get(f"/api/v1/attest/{LOG}/records/{rec['attestation_id']}?site={SITE}")
    assert r.status_code == 200 and r.json()["record"] == rec
    got = [x for x in audit_trail.read_chain(audit_trail.audit_path(arch["state"]))
           if x.get("skill") == "attest.read.record"][-1]["access"]  # fmt: skip
    assert (got["source"], got["labels"], got["door"]) == (source, labels, "attest.http.record")
    # The records door, the show skill (by id and by log), the reference: all from the package.
    page = as_(c, OP1).get(f"/api/v1/attest/{LOG}/records?site={SITE}")
    assert page.status_code == 200, page.text
    shown = _skill(arch, "attest.show", {"attestation_id": rec["attestation_id"]})
    assert shown.ok and rec["digest"] in json.dumps(shown.value), shown
    assert _reads(arch, "attest.show")[-1]["access"]["source"] == source
    listed = _skill(arch, "attest.show", {"site": SITE, "logbook": LOG})
    assert listed.ok and _reads(arch, "attest.show")[-1]["access"]["source"] == source
    ref = _skill(arch, "attest.reference", {"attestation_id": rec["attestation_id"]})
    assert ref.ok, ref.errors
    assert _reads(arch, "attest.reference")[-1]["access"]["source"] == source
    # The proof that the package is the source: with it out of reach, the period is not served.
    moved = arch["cold"].with_name("cold-away")
    arch["cold"].rename(moved)
    archives._packages.clear()  # noqa: SLF001
    with pytest.raises(Exception):  # noqa: B017 - the store is gone; nothing falls back silently
        service.record(rec["attestation_id"])
    moved.rename(arch["cold"])
    assert service.record(rec["attestation_id"]) == rec


@pytest.mark.acceptance("LD-5-A3")
def test_ld5_a3_an_archived_record_is_refused_by_the_same_check(arch, monkeypatch):
    a = _retired(arch)
    registry.register(replace(registry.get(LOG), readers=("auditor",)))
    monkeypatch.setattr("axiom.extensions.builtins.attest.access_requests.knowable", lambda *_a, **_k: False)
    c = arch["client"]
    c.cookies.clear()
    r = as_(c, OP2).get(f"/api/v1/attest/{LOG}/records/{arch['rec']['attestation_id']}?site={SITE}")
    assert (r.status_code, r.json()["code"]) == (404, "not_found")
    denied = [x for x in _reads(arch, "attest.http.record") if x["outcome"] == "denied"][-1]["access"]
    assert denied.get("source") is None and denied["ids"] == [] and denied["count"] == 0
    # The archives listing tells the refused person nothing, not even a count.
    r = as_(c, OP2).get(f"/api/v1/attest/{LOG}/archives?site={SITE}")
    assert r.status_code == 404 and set(r.json()) == {"detail", "code"}
    assert a["package_digest"] not in r.text


@pytest.mark.acceptance("LD-5-A3")
def test_ld5_a3_retire_drops_fine_vectors_and_keeps_the_seals(arch):
    index.refresh([(SITE, LOG)])
    _retired(arch)
    # Since LD-8 the period's fine rows go entirely; the seal's summary stays indexed.
    rows = {r["seq"]: r for r in index.rows_for_tests() if r["logbook"] == LOG}
    assert set(rows) == {arch["seal"]["seq"]} and rows[arch["seal"]["seq"]]["embedding"] is not None
    assert index.rebuild() and {r["seq"]: r["embedding"] is None for r in index.rows_for_tests()
                                if r["logbook"] == LOG} == {3: False}  # fmt: skip


# -- LD-5-A4: the manifest -----------------------------------------------------------------------


@pytest.mark.acceptance("LD-5-A4")
def test_ld5_a4_the_manifest_names_everything_the_period_is(arch):
    built = archives.build(SITE, LOG, by=ME)
    man = json.loads(zipfile.ZipFile(_package(arch, built)).read("manifest.json"))
    seal = arch["seal"]
    assert (man["logbook"], man["site_id"]) == (LOG, SITE)
    assert man["range"] == {"from_seq": 1, "to_seq": 2, "last_seq": seal["seq"], "start_prev": "0" * 64}
    assert man["covers"] == seal["content"]["seal"]["covers"] and man["period"] == {"boundary": "range"}
    assert man["head"] == {"seq": seal["seq"], "digest": seal["digest"]}
    assert man["anchors"] == [a["anchor_id"] for a in service.anchors(SITE)]  # the seal's anchor
    assert man["labels"] == {"classification": "internal", "export_controlled": False}
    assert "retention" in man and man["package_digest"] == archives.package_digest(man) == built["package_digest"]
    assert set(man["files"]) == {"records.jsonl", "rows.jsonl", "keys.json", "anchors.jsonl", "summary.json",
                                 "verify.py", "verify_archive.py",
                                 f"attachments/sha256/{attachments.refs_of(arch['photo'])[0]['sha256']}"}
    assert json.loads(zipfile.ZipFile(_package(arch, built)).read("summary.json"))["summary"] == SUMMARY


# -- LD-5-A5: export-controlled logs archive only to controlled storage ---------------------------


@pytest.mark.acceptance("LD-5-A5")
def test_ld5_a5_an_export_controlled_log_refuses_a_target_that_is_not_controlled(arch, monkeypatch, tmp_path):
    _sealable(export_controlled=True)
    with pytest.raises(archives.ArchiveRefused, match="controlled storage"):
        archives.build(SITE, LOG, by=ME)
    assert not list(arch["cold"].rglob("*.zip"))  # nothing was written
    vault = tmp_path / "vault"
    archives.set_target(provider_from_config({"base_dir": str(vault)}), storage_class="controlled", name="vault")
    built = archives.build(SITE, LOG, by=ME, target_name="vault")
    assert built["storage_class"] == "controlled" and list(vault.rglob("*.zip"))
    # The class is configuration, beside the provider's own.
    archives.set_target(None)
    monkeypatch.setenv("AXIOM_STORAGE_ATTEST_ARCHIVE",
                       json.dumps({"base_dir": str(tmp_path / "x"), "storage_class": "controlled"}))
    assert archives.target().storage_class == "controlled"


# -- LD-5-A6: the sealing schedule comes from the declared cadence ---------------------------------


@pytest.fixture
def pulse():
    import contextlib

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from axiom.extensions.builtins.schedule import store as pulse_store
    from axiom.extensions.builtins.schedule.db_models import Base

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    maker = sessionmaker(engine, future=True)

    @contextlib.contextmanager
    def provider():
        s = maker()
        try:
            yield s
        finally:
            s.close()

    pulse_store.set_provider(provider)
    yield
    pulse_store.reset_provider()
    engine.dispose()


@pytest.mark.acceptance("LD-5-A6")
def test_ld5_a6_the_seal_schedule_follows_the_declared_cadence(world, restore_log, pulse, monkeypatch):
    from axiom.extensions.builtins.attest import switches
    from axiom.extensions.builtins.schedule import api as pulse_api

    monkeypatch.setenv("AXIOM_SITE_TZ", "UTC")
    monkeypatch.setenv("AXIOM_FLAG_SHIFT_LOGS", "on")  # a log with a clock period is a shift log
    switches.reset()
    day = PeriodDecl(name="day", starts_key=None, starts=("06:00", "18:00"), grace_key=None, grace_minutes=30)
    book = registry.get(LOG)
    registry.register(replace(book, periods={"day": day}, seal=SealDecl(
        by_roles=("operator",), posture="sso", fresh_within_seconds=None, boundary="day")))  # fmt: skip
    [cadence] = archives.seal_cadences(SITE, registry.get(LOG))
    assert (cadence.kind, cadence.cron, cadence.tz) == ("cron", "30 6,18 * * *", "UTC")
    got = _skill(world, "attest.seal_schedule", {"site": SITE, "logbook": LOG})
    assert got.ok and len(got.value["added"]) == 1, got
    listed = [s for s in pulse_api.list_schedules() if s.state == "active"]
    assert [(s.name, s.cadence_kind) for s in listed] == [(f"Seal due: {LOG} at {SITE} @ 30 6,18 * * *", "cron")]
    assert archives.schedule_seals(SITE, [LOG])["added"] == []  # idempotent
    # A log with no cadence seals only by hand: nothing scheduled, and the old entry goes.
    _sealable()
    assert archives.seal_cadences(SITE, registry.get(LOG)) == []
    assert archives.schedule_seals(SITE, [LOG])["cancelled"] == [listed[0].name]
    assert [s for s in pulse_api.list_schedules() if s.state == "active"] == []


# -- LD-5-A7: gold rebuilt from the package equals gold from the chain ----------------------------


@pytest.mark.acceptance("LD-5-A7")
def test_ld5_a7_gold_rebuilt_from_the_package_equals_gold_from_the_chain(arch):
    built = archives.build(SITE, LOG, by=ME)
    from_chain = gold.partition(SITE, LOG, built["from_seq"], built["last_seq"])
    from_package = archives.gold_rows_from_package(built["archive_id"])
    assert len(from_chain) == len(from_package) == 3
    assert gold.partition_digest(from_chain) == gold.partition_digest(from_package)
    for a, b in zip(from_chain, from_package, strict=True):  # digest for digest
        assert a["digest"] == b["digest"] and a["content"] == b["content"]


# -- Must refuse -----------------------------------------------------------------------------------


@pytest.mark.acceptance("LD-5-A1")
def test_ld5_refuses_a_package_that_omits_an_attachment_a_record_names(arch):
    sha = attachments.refs_of(arch["photo"])[0]["sha256"]
    (arch["blobs"] / SITE / "sha256" / sha).unlink()
    with pytest.raises(archives.ArchiveRefused, match=f"#2 names attachment sha256:{sha}"):
        archives.build(SITE, LOG, by=ME)
    assert not list(arch["cold"].rglob("*.zip"))


@pytest.mark.acceptance("LD-5-A3")
def test_ld5_refuses_an_archive_read_outside_the_door(arch):
    built = archives.build(SITE, LOG, by=ME)
    log_doors.clear()
    with pytest.raises(archives.ArchiveRefused, match="through a door"):
        archives.read_package(built["archive_id"])
    caller = log_doors.LogCaller(handle=ME, state_dir=arch["state"])
    assert log_doors.check("attest.archives", caller, log_doors.target(SITE, LOG), transport="cli").allowed
    assert [r["seq"] for r in archives.read_package(built["archive_id"])] == [1, 2, 3]
    log_doors.clear()


@pytest.mark.acceptance("LD-5-A5")
def test_ld5_refuses_a_package_written_through_a_vendor_client(arch):
    class VendorClient:  # a bucket client, not the provider abstraction
        def upload(self, *a, **k):
            raise AssertionError("written around the abstraction")

    archives.set_target(VendorClient())
    with pytest.raises(archives.ArchiveRefused, match="storage-provider abstraction"):
        archives.build(SITE, LOG, by=ME)
    # And the code names no vendor and no client: the provider is configuration.
    tree = ast.parse((ROOT / "src/axiom/extensions/builtins/attest/archives.py").read_text())
    imported = {n.names[0].name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import)}
    imported |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not imported & {"boto3", "botocore", "google", "azure", "minio"}


@pytest.mark.acceptance("LD-5-A2")
def test_ld5_the_archive_skill_is_for_a_person_who_may_close_the_log(arch):
    refused = _skill(arch, "attest.archive", {"site": SITE, "logbook": LOG}, handle=OTHER)
    assert not refused.ok and "close it out" in refused.errors[0]
    done = _skill(arch, "attest.archive", {"site": SITE, "logbook": LOG})
    assert done.ok, done.errors
    assert done.value["status"] == "archived" and done.value["verified"]
    listed = _skill(arch, "attest.archives", {"site": SITE, "logbook": LOG})
    assert listed.ok and [x["archive_id"] for x in listed.value["archives"]] == [done.value["archive_id"]]
