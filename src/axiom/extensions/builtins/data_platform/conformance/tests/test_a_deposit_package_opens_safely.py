# Copyright (c) 2026 The University of Texas at Austin
# Copyright (c) 2026 B-Tree Labs
# SPDX-License-Identifier: Apache-2.0
"""A deposit's packaging is read from its bytes, and a hostile one is refused.

A partner changed how a day's export was packaged, under a new name and with no
notice, and the lane declined every file because it matched on names. These tests
hold the replacement: any packaging opens the same way, and a deposit is treated
as what it is, input from outside the platform.
"""

from __future__ import annotations

import bz2
import gzip
import io
import lzma
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from axiom.extensions.builtins.data_platform.conformance.deposits import Deposit
from axiom.extensions.builtins.data_platform.conformance.discovery import PACKAGE_GROUP
from axiom.extensions.builtins.data_platform.conformance.packages import (
    Codec,
    Layout,
    Limits,
    OpenerRegistry,
    UnknownOpener,
    UnsafePackage,
    default_openers,
    open_package,
    open_parts,
)

SAMPLES = b"ts,value\n2026-11-03T00:00:00+00:00,1.5\n"
CATALOGUE = b"id,name\n1,flow\n"
LAYOUT = Layout.declared(
    {"samples": ["*samples.csv", "nightly-*.csv"], "catalogue": "*channels.csv"}
)


def _tar(members: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as tar:
        for name, body in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return out.getvalue()


def _zip(members: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in members.items():
            archive.writestr(name, body)
    return out.getvalue()


def _write(tmp_path: Path, name: str, body: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(body)
    return path


def _read(path: Path, **kw) -> dict[str, bytes]:
    with open_package(path, **kw) as members:
        return {m.name: m.binary().read() for m in members}


BUNDLE = {"channels.csv": CATALOGUE, "nightly-20261103.csv": SAMPLES}


@pytest.mark.parametrize(
    ("name", "body"),
    [
        ("nightly-20261103.bz2", bz2.compress(_tar(BUNDLE))),
        ("renamed-with-no-hint", bz2.compress(_tar(BUNDLE))),
        ("day.tar.gz", gzip.compress(_tar(BUNDLE))),
        ("day.tar.xz", lzma.compress(_tar(BUNDLE))),
        ("day.tar", _tar(BUNDLE)),
        ("day.zip", _zip(BUNDLE)),
    ],
    ids=["named-bz2", "no-hint", "tar-gz", "tar-xz", "tar", "zip"],
)
def test_a_bundle_opens_from_its_bytes_whatever_it_is_called(tmp_path, name, body):
    assert _read(_write(tmp_path, name, body)) == BUNDLE


@pytest.mark.parametrize(
    ("name", "body", "part"),
    [
        ("x_samples.csv", SAMPLES, "x_samples.csv"),
        ("x_samples.csv.bz2", bz2.compress(SAMPLES), "x_samples.csv"),
        ("x_samples.csv.gz", gzip.compress(SAMPLES), "x_samples.csv"),
        ("x_samples.csv.xz", lzma.compress(SAMPLES), "x_samples.csv"),
    ],
    ids=["plain", "bz2", "gz", "xz"],  # gzip stamps the time: bytes as ids differ per worker
)
def test_a_loose_file_is_one_part_named_without_its_compression(tmp_path, name, body, part):
    assert _read(_write(tmp_path, name, body)) == {part: SAMPLES}


def test_a_declared_opener_is_composed_by_name(tmp_path):
    path = _write(tmp_path, "day.bin", bz2.compress(_tar(BUNDLE)))
    assert _read(path, opener="tar.bz2") == BUNDLE


@pytest.mark.parametrize("opener", ["rar", "tar.zst", "../../evil", "os.system"])
def test_a_declaration_can_only_name_what_is_registered(tmp_path, opener):
    path = _write(tmp_path, "day.bin", SAMPLES)
    with pytest.raises(UnknownOpener):
        _read(path, opener=opener)


def test_a_part_can_be_read_twice_and_out_of_order(tmp_path):
    path = _write(tmp_path, "day", bz2.compress(_tar(BUNDLE)))
    with open_package(path) as members:
        by_name = {m.name: m for m in members}
        assert by_name["nightly-20261103.csv"].binary().read() == SAMPLES
        assert by_name["channels.csv"].binary().read() == CATALOGUE
        assert by_name["nightly-20261103.csv"].binary().read() == SAMPLES


def test_a_part_reads_as_text_without_its_byte_order_mark(tmp_path):
    path = _write(tmp_path, "x_samples.csv", b"\xef\xbb\xbfts,value\n")
    with open_package(path) as members, members[0].text() as handle:
        assert handle.read() == "ts,value\n"


# --- what a hostile deposit gets ---------------------------------------------


def test_a_small_file_that_expands_without_end_is_stopped(tmp_path):
    bomb = bz2.compress(b"\0" * (4 * 1024 * 1024))
    path = _write(tmp_path, "day.bz2", bomb)
    with pytest.raises(UnsafePackage, match="limit"):
        _read(path, limits=Limits(max_expanded_bytes=1024 * 1024))


def test_the_limit_holds_while_a_bundle_is_only_being_scanned(tmp_path):
    """Listing a compressed tar decompresses it, so the cap must sit under the scan."""
    bomb = bz2.compress(_tar({"a.csv": b"\0" * (4 * 1024 * 1024), "b.csv": b"x"}))
    path = _write(tmp_path, "day.bz2", bomb)
    with (
        pytest.raises(UnsafePackage),
        open_package(path, limits=Limits(max_expanded_bytes=1 << 20)),
    ):
        pytest.fail("the package opened")


def test_a_header_that_claims_an_enormous_member_is_stopped(tmp_path):
    raw = bytearray(_tar({"a.csv": b"x" * 10}))
    forged = tarfile.TarInfo("a.csv")
    forged.size = 1 << 40
    raw[:512] = forged.tobuf(format=tarfile.GNU_FORMAT)
    path = _write(tmp_path, "day.bz2", bz2.compress(bytes(raw)))
    with (
        pytest.raises(UnsafePackage),
        open_package(path, limits=Limits(max_expanded_bytes=1 << 20)),
    ):
        pytest.fail("the package opened")


def test_reads_across_parts_share_one_budget(tmp_path):
    body = _tar({f"{i}.csv": b"x" * 1024 for i in range(4)})
    path = _write(tmp_path, "day.tar", body)
    with open_package(path, limits=Limits(max_expanded_bytes=2048)) as members:
        members[0].binary().read()
        members[1].binary().read()
        with pytest.raises(UnsafePackage):
            members[2].binary().read()


def test_a_package_of_too_many_members_is_refused(tmp_path):
    path = _write(tmp_path, "day.tar", _tar({f"{i}.csv": b"" for i in range(10)}))
    with pytest.raises(UnsafePackage, match="members"):
        _read(path, limits=Limits(max_members=5))


@pytest.mark.parametrize("name", ["../../etc/passwd", "/etc/passwd", "a/../../b.csv", "C:/x.csv"])
@pytest.mark.parametrize("make", [_tar, _zip])
def test_a_member_name_that_climbs_out_refuses_the_package(tmp_path, name, make):
    path = _write(tmp_path, "day.pkg", make({name: b"x"}))
    with pytest.raises(UnsafePackage, match="relative"):
        _read(path)


@pytest.mark.parametrize(
    "kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE]
)
def test_a_tar_link_or_device_refuses_the_package(tmp_path, kind):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as tar:
        info = tarfile.TarInfo("channels.csv")
        info.type = kind
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    path = _write(tmp_path, "day.tar", out.getvalue())
    with pytest.raises(UnsafePackage, match="plain file"):
        _read(path)


def test_a_zip_symlink_refuses_the_package(tmp_path):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        info = zipfile.ZipInfo("channels.csv")
        info.external_attr = 0o120777 << 16
        archive.writestr(info, "/etc/passwd")
    path = _write(tmp_path, "day.zip", out.getvalue())
    with pytest.raises(UnsafePackage, match="plain file"):
        _read(path)


def test_an_encrypted_zip_member_refuses_the_package(tmp_path):
    raw = bytearray(_zip({"a.csv": b"x"}))
    raw[raw.find(b"PK\x01\x02") + 8] |= 0x1  # the central directory's encrypted flag
    path = _write(tmp_path, "day.zip", bytes(raw))
    with pytest.raises(UnsafePackage, match="encrypted"):
        _read(path)


def test_a_package_inside_a_package_stays_opaque(tmp_path):
    inner = bz2.compress(_tar({"a.csv": b"x"}))
    path = _write(tmp_path, "day.tar", _tar({"inner.bz2": inner}))
    assert _read(path) == {"inner.bz2": inner}


@pytest.mark.parametrize(
    "body",
    [bz2.compress(_tar(BUNDLE))[:-40], gzip.compress(_tar(BUNDLE))[:-40]],
    ids=["bz2", "gz"],
)
def test_a_truncated_package_fails_loudly_and_is_not_a_short_one(tmp_path, body):
    path = _write(tmp_path, "day", body)
    with pytest.raises((EOFError, OSError, tarfile.TarError)):
        _read(path)


def test_opening_a_package_writes_nothing_to_disk(tmp_path):
    folder = tmp_path / "bronze"
    folder.mkdir()
    path = _write(folder, "day", bz2.compress(_tar(BUNDLE)))
    _read(path)
    assert [p.name for p in folder.iterdir()] == ["day"]


# --- who may add a provider --------------------------------------------------


def test_a_registered_codec_is_used_by_sniffing_and_by_name(tmp_path):
    def rot(path):
        return io.BytesIO(bytes(b ^ 0x55 for b in Path(path).read_bytes()[3:]))

    registry = OpenerRegistry()
    registry.register_codec(Codec("rot", b"ROT", rot, (".rot",)))
    body = b"ROT" + bytes(b ^ 0x55 for b in SAMPLES)
    path = _write(tmp_path, "x_samples.csv.rot", body)
    assert _read(path, registry=registry) == {"x_samples.csv": SAMPLES}
    assert _read(path, opener="rot", registry=registry) == {"x_samples.csv": SAMPLES}


def test_a_provider_cannot_replace_a_builtin():
    registry = default_openers()
    with pytest.raises(ValueError, match="already registered"):
        registry.register_codec(Codec("bz2", b"BZh", bz2.open))


def test_opener_providers_pass_the_portfolio_gate(monkeypatch):
    import axiom.extensions.builtins.data_platform.conformance.packages as packages

    seen = {}

    def fake(registry, *, group):
        seen["group"] = group
        return []

    monkeypatch.setattr(packages, "register_discovered", fake)
    packages.default_openers.cache_clear()
    try:
        packages.default_openers()
    finally:
        packages.default_openers.cache_clear()
    assert seen["group"] == PACKAGE_GROUP


# --- parts by role -----------------------------------------------------------


@dataclass
class _Sibling:
    name: str
    path: Path


def _deposit(path: Path, name: str, folder: tuple = ()) -> Deposit:
    return Deposit(
        record={"display_name": name, "source_path": f"/share/{name}"},
        path=path,
        connector="c",
        folder=folder,
    )


def test_roles_come_from_the_layout_not_from_the_reader(tmp_path):
    path = _write(tmp_path, "day", bz2.compress(_tar(BUNDLE)))
    with open_parts(_deposit(path, "nightly-20261103.bz2"), LAYOUT) as parts:
        assert [m.basename for m in parts.all("samples")] == ["nightly-20261103.csv"]
        assert [m.basename for m in parts.all("catalogue")] == ["channels.csv"]


def test_a_bundles_own_catalogue_beats_the_one_beside_it(tmp_path):
    old = _deposit(_write(tmp_path, "channels.csv", b"id,name\n1,old\n"), "channels.csv")
    path = _write(tmp_path, "day", bz2.compress(_tar(BUNDLE)))
    with open_parts(_deposit(path, "nightly-20261103.bz2", (old,)), LAYOUT) as parts:
        with parts.one("catalogue") as part, part.text() as handle:
            assert handle.read().encode() == CATALOGUE


def test_a_loose_file_finds_its_catalogue_in_the_folder(tmp_path):
    sibling = _deposit(_write(tmp_path, "d_channels.csv", CATALOGUE), "d_channels.csv")
    path = _write(tmp_path, "x_samples.csv.bz2", bz2.compress(SAMPLES))
    with open_parts(_deposit(path, "x_samples.csv.bz2", (sibling,)), LAYOUT) as parts:
        assert [m.name for m in parts.all("samples")] == ["x_samples.csv"]
        with parts.one("catalogue") as part, part.text() as handle:
            assert handle.read().encode() == CATALOGUE


def test_a_compressed_sibling_reads_like_a_plain_one(tmp_path):
    sibling = _deposit(
        _write(tmp_path, "d_channels.csv.gz", gzip.compress(CATALOGUE)), "d_channels.csv.gz"
    )
    path = _write(tmp_path, "x_samples.csv", SAMPLES)
    with open_parts(_deposit(path, "x_samples.csv", (sibling,)), LAYOUT) as parts:
        with parts.one("catalogue") as part, part.text() as handle:
            assert handle.read().encode() == CATALOGUE


def test_no_part_in_a_role_is_none_not_a_guess(tmp_path):
    path = _write(tmp_path, "x_samples.csv", SAMPLES)
    with open_parts(_deposit(path, "x_samples.csv"), LAYOUT) as parts:
        with parts.one("catalogue") as part:
            assert part is None


def test_two_catalogues_in_one_bundle_is_an_error_not_a_pick(tmp_path):
    body = _tar({"a_channels.csv": CATALOGUE, "b_channels.csv": CATALOGUE})
    path = _write(tmp_path, "day.tar", body)
    with open_parts(_deposit(path, "day.tar"), LAYOUT) as parts:
        with pytest.raises(ValueError, match="2 parts"), parts.one("catalogue"):
            pass


def test_an_archivers_shadow_files_are_never_a_part(tmp_path):
    body = _tar({"._channels.csv": b"\0\5\x16\7", "__MACOSX/._nightly-1.csv": b"x", **BUNDLE})
    path = _write(tmp_path, "day.tar", body)
    with open_parts(_deposit(path, "day.tar"), LAYOUT) as parts:
        assert [m.name for m in parts.all("catalogue")] == ["channels.csv"]
        assert [m.name for m in parts.all("samples")] == ["nightly-20261103.csv"]


@pytest.mark.parametrize(
    "raw",
    [{}, {"samples": []}, {"samples": [""]}, {"samples": [3]}, {"samples": ["../x.csv"]}],
)
def test_a_layout_refuses_a_shape_it_would_misread(raw):
    with pytest.raises(ValueError):
        Layout.declared(raw)
