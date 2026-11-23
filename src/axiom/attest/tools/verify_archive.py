#!/usr/bin/env python3
# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0

"""Verify a sealed-period archive package with nothing but Python (unit LD-5, ADR-251 D7).

This file ships inside every archive package, beside ``verify.py``, whose canonical form and
Ed25519 verification it reuses (loaded from the same directory by path, so it runs with
``python -I -S`` on a machine with no Axiom installed, no network and no database).

Usage:
    python verify_archive.py PACKAGE      # the unpacked directory, or the .zip itself

It checks, and prints one JSON report:

- the manifest's package digest, and every file's SHA-256 against the manifest; a file the
  manifest does not name is refused;
- every record's digest and signatures, and the chain links from the first record of the period
  (``range.from_seq`` after ``range.start_prev``) to the head the manifest names;
- the seal: the record that closed the period is in the package and covers what it says;
- each attachment a record names is in the package under its hash, and hashes to it;
- each anchor: its signature, its root recomputed from the period's head and the audit path,
  and that the head it fixed is the record at that position; at least one anchor fixes the
  package's last record;
- the chain rows: one per record, agreeing with it.

Exit status is 0 when everything verifies and 1 otherwise.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
import zipfile
from pathlib import Path

FORMAT = "axiom-attest-archive/1"
ANCHOR_DOMAIN = b"axiom/attest/anchor/v1\n"
MANIFEST = "manifest.json"
_ROW_AGREES = ("attestation_id", "seq", "digest", "kind", "meaning")


def _load_verify():
    """``verify.py`` from beside this file, by path (it may not be importable by name)."""
    here = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("_archive_verify_core", here / "verify.py")
    if spec is None or spec.loader is None:
        raise SystemExit("verify.py is missing beside verify_archive.py")
    mod = importlib.util.module_from_spec(spec)
    was, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # leave the package as it came
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = was
    return mod


class _Package:
    """The package's files, from a directory or a zip, read one at a time."""

    def __init__(self, path):
        self.path = Path(path)
        self.zip = zipfile.ZipFile(self.path) if self.path.is_file() else None

    def names(self):
        if self.zip is not None:
            return sorted(n for n in self.zip.namelist() if not n.endswith("/"))
        # Python's own byte-code cache, made by running a script here, is not package content.
        return sorted(p.relative_to(self.path).as_posix() for p in self.path.rglob("*")
                      if p.is_file() and "__pycache__" not in p.relative_to(self.path).parts)

    def open(self, name):
        if self.zip is not None:
            return self.zip.open(name)
        return open(self.path / name, "rb")

    def read(self, name):
        with self.open(name) as fh:
            return fh.read()

    def sha256(self, name):
        h = hashlib.sha256()
        with self.open(name) as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        return h.hexdigest()

    def lines(self, name):
        with self.open(name) as fh:
            for raw in fh:
                line = raw.decode("utf-8").strip()
                if line:
                    yield json.loads(line)


def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def package_digest(manifest):
    """The digest over the manifest without its own ``package_digest``: it covers every file's
    hash, so it covers the package."""
    body = {k: v for k, v in manifest.items() if k != "package_digest"}
    return hashlib.sha256(canonical_json(body)).hexdigest()


def _leaf(core, head):
    body = {"logbook": head["logbook"], "seq": head["seq"], "digest": head["digest"]}
    return hashlib.sha256(b"\x00" + core.canonical_bytes(body)).digest()


def _root(core, head, path):
    node = _leaf(core, head)
    for side, sibling in path:
        other = bytes.fromhex(sibling)
        node = hashlib.sha256(b"\x01" + (other + node if side == "L" else node + other)).digest()
    return node.hex()


def _sig_ok(core, sig, keys, message):
    if not isinstance(sig, dict) or sig.get("alg") != "ed25519":
        return "malformed"
    public = keys.get(sig.get("key_id", ""))
    if public is None:
        return "unknown_key"
    try:
        raw = base64.b64decode(sig.get("sig", ""), validate=True)
    except (ValueError, TypeError):
        return "malformed"
    return None if core.ed25519_verify(public, message, raw) else "bad_signature"


def verify(path):
    """The report for the package at ``path`` (a directory or a zip)."""
    core = _load_verify()
    pkg = _Package(path)
    report = {"ok": False, "format": FORMAT, "package_digest": None, "records": 0, "attachments": 0,
              "anchors": 0, "head": None, "reason": None, "at_seq": None}

    def bad(reason, seq=None):
        report.update(reason=reason, at_seq=seq)
        return report

    names = pkg.names()
    if MANIFEST not in names:
        return bad("manifest_missing")
    manifest = json.loads(pkg.read(MANIFEST).decode("utf-8"))
    if manifest.get("format") != FORMAT:
        return bad("unknown_format")
    report["package_digest"] = manifest.get("package_digest")
    if package_digest(manifest) != manifest.get("package_digest"):
        return bad("package_digest_mismatch")
    files = manifest.get("files") or {}
    extra = sorted(set(names) - set(files) - {MANIFEST})
    if extra:
        return bad("unlisted_file:" + extra[0])
    for name, want in sorted(files.items()):
        if name not in names:
            return bad("file_missing:" + name)
        if pkg.sha256(name) != want:
            return bad("file_altered:" + name)

    keys = {k: base64.b64decode(v) for k, v in json.loads(pkg.read("keys.json")).items()}
    rng = manifest["range"]
    records = list(pkg.lines("records.jsonl"))
    ok, checked, last_seq, last_digest, bad_seq, reason = core.verify_records(
        records, keys, rng["from_seq"], rng["start_prev"])
    if not ok:
        return bad(reason, bad_seq)
    report["records"] = checked
    if not records or (last_seq, last_digest) != (manifest["head"]["seq"], manifest["head"]["digest"]):
        return bad("head_mismatch", last_seq)
    if last_seq != rng["last_seq"]:
        return bad("range_mismatch", last_seq)
    by_seq = {r["seq"]: r for r in records}
    for r in records:
        if r.get("site_id", manifest["site_id"]) != manifest["site_id"] or r.get("logbook", manifest["logbook"]) != manifest["logbook"]:
            return bad("foreign_record", r["seq"])
        if r.get("personal_sig") is not None:
            why = _sig_ok(core, r["personal_sig"], keys, core.SIGNING_DOMAIN + bytes.fromhex(r["digest"]))
            if why:
                return bad("personal_" + why, r["seq"])

    seal = manifest["seal"]
    sealed = by_seq.get(seal["seq"])
    if sealed is None or sealed.get("attestation_id") != seal["attestation_id"] or sealed.get("kind") != "seal":
        return bad("seal_missing", seal["seq"])
    covers = ((sealed.get("content") or {}).get("seal") or {}).get("covers") or {}
    if covers != manifest["covers"]:
        return bad("seal_covers_mismatch", seal["seq"])
    covered = by_seq.get(covers.get("head_seq"))
    if covered is None or covered["digest"] != covers.get("head_digest"):
        return bad("sealed_head_mismatch", covers.get("head_seq"))
    summary = json.loads(pkg.read("summary.json"))
    if summary.get("summary") != (sealed.get("content") or {}).get("summary"):
        return bad("summary_mismatch", seal["seq"])

    attached = 0
    for r in records:
        evidence = r.get("evidence") or {}
        for ref in evidence.get("attachments") or ():
            sha = ref.get("sha256", "")
            name = f"attachments/sha256/{sha}"
            if name not in files:
                return bad("attachment_missing:" + sha, r["seq"])
            if files[name] != sha:
                return bad("attachment_altered:" + sha, r["seq"])
            attached += 1
    report["attachments"] = attached

    fixes_last = False
    count = 0
    for a in pkg.lines("anchors.jsonl"):
        head = a.get("head") or {}
        if head.get("logbook") != manifest["logbook"]:
            return bad("anchor_foreign_head")
        at = by_seq.get(head.get("seq"))
        if at is None or at["digest"] != head.get("digest"):
            return bad("anchor_head_mismatch", head.get("seq"))
        if _root(core, head, a.get("proof") or []) != a.get("root"):
            return bad("anchor_root_mismatch", head.get("seq"))
        why = _sig_ok(core, a.get("node_sig"), keys, ANCHOR_DOMAIN + bytes.fromhex(a["root"]))
        if why:
            return bad("anchor_" + why, head.get("seq"))
        fixes_last = fixes_last or head.get("seq") == last_seq
        count += 1
    report["anchors"] = count
    if sorted(manifest.get("anchors") or []) != sorted(a["anchor_id"] for a in pkg.lines("anchors.jsonl")):
        return bad("anchors_mismatch")
    if not fixes_last:
        return bad("no_anchor_fixes_the_head", last_seq)

    rows = list(pkg.lines("rows.jsonl"))
    if len(rows) != len(records):
        return bad("rows_mismatch")
    for row, rec in zip(rows, records):
        if any(row.get(k) != rec.get(k) for k in _ROW_AGREES):
            return bad("row_disagrees", rec["seq"])

    report.update(ok=True, head={"seq": last_seq, "digest": last_digest})
    return report


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python verify_archive.py PACKAGE (a directory or a .zip)", file=sys.stderr)
        return 2
    try:
        report = verify(args[0])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        report = {"ok": False, "format": FORMAT, "reason": f"unreadable: {type(exc).__name__}: {exc}"}
    print(json.dumps(report, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
