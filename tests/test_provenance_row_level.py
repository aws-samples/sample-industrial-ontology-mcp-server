"""A4 row-level provenance 단위 테스트.

- make_row_provenance_uri URI 포맷
- annotate_row_provenance 기본 / csvMtime / 파일 링크 / dedupe
- _get_csv_mtime_iso missing / valid file
"""
from __future__ import annotations

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF

from tools.provenance import (
    PROV,
    PROV_BASE,
    _get_csv_mtime_iso,
    annotate_row_provenance,
    build_row_uri_for_abox,
    make_row_provenance_uri,
)


def test_make_row_uri_format():
    """Row URI 는 prov://<table>#row=<N> 형태."""
    uri = make_row_provenance_uri("Equipment_Master", 42)
    assert str(uri) == "prov://Equipment_Master#row=42"


def test_build_row_uri_for_abox_alias():
    """build_row_uri_for_abox 는 make_row_provenance_uri 와 동일 URI 를 돌려준다."""
    u1 = build_row_uri_for_abox("Equipment_Master", 42)
    u2 = make_row_provenance_uri("Equipment_Master", 42)
    assert u1 == u2


def test_annotate_row_provenance_basic():
    """csv_path 없으면 type + csvTable + rowNumber 3 triple 만 추가."""
    sidecar = Graph()
    inst = URIRef("http://ex.org/steel-inst/EquipmentMaster_EQ001")
    added = annotate_row_provenance(
        sidecar, inst, "Equipment_Master", 42, csv_path=None,
    )
    assert added == 3  # type + csvTable + rowNumber

    row_uri = URIRef("prov://Equipment_Master#row=42")
    assert (row_uri, RDF.type, URIRef(f"{PROV}Entity")) in sidecar
    assert (
        row_uri,
        URIRef(f"{PROV_BASE}csvTable"),
        Literal("Equipment_Master"),
    ) in sidecar
    # rowNumber 는 xsd:integer 로 매립
    row_vals = list(sidecar.objects(row_uri, URIRef(f"{PROV_BASE}rowNumber")))
    assert len(row_vals) == 1
    assert int(str(row_vals[0])) == 42


def test_annotate_row_provenance_with_csv_mtime(tmp_path):
    """csv_path 제공 + 파일 존재 시 csvMtime + file link 추가 → 총 5 triple."""
    csv_file = tmp_path / "Test.csv"
    csv_file.write_text("header\ndata\n")

    sidecar = Graph()
    inst = URIRef("http://ex.org/steel-inst/Test_T001")
    added = annotate_row_provenance(
        sidecar, inst, "Test", 1, csv_path=str(csv_file),
    )
    assert added == 5  # 3 + mtime + file link

    row_uri = URIRef("prov://Test#row=1")
    mtime_triples = list(
        sidecar.triples(
            (row_uri, URIRef(f"{PROV_BASE}csvMtime"), None),
        )
    )
    assert len(mtime_triples) == 1
    # ISO-8601 format check
    mtime_value = str(mtime_triples[0][2])
    assert "T" in mtime_value  # ISO-8601 date-time separator

    # file link (file://.../Test.csv)
    file_links = list(
        sidecar.triples(
            (row_uri, URIRef(f"{PROV}wasDerivedFrom"), None),
        )
    )
    assert len(file_links) == 1
    assert str(file_links[0][2]).startswith("file://")
    assert "Test.csv" in str(file_links[0][2])


def test_annotate_row_provenance_dedupe():
    """같은 row URI 두 번 호출 시 두 번째는 0 추가."""
    sidecar = Graph()
    inst1 = URIRef("http://ex.org/steel-inst/Test_T001")
    inst2 = URIRef("http://ex.org/steel-inst/Test_T001_other")  # 같은 row 참조

    added1 = annotate_row_provenance(sidecar, inst1, "Test", 1)
    added2 = annotate_row_provenance(sidecar, inst2, "Test", 1)

    assert added1 == 3
    assert added2 == 0  # dedupe — 같은 row entity 는 한 번만


def test_get_csv_mtime_iso_missing_file():
    """존재하지 않는 CSV 경로는 None."""
    assert _get_csv_mtime_iso("/nonexistent/path.csv") is None


def test_get_csv_mtime_iso_valid_file(tmp_path):
    """존재하는 CSV 는 ISO-8601 UTC 반환."""
    csv_file = tmp_path / "Test.csv"
    csv_file.write_text("data")
    iso = _get_csv_mtime_iso(str(csv_file))
    assert iso is not None
    assert "T" in iso
    # UTC timezone (datetime.fromtimestamp(..., tz=timezone.utc).isoformat() 는 +00:00 suffix)
    assert iso.endswith("+00:00") or iso.endswith("Z")


def test_annotate_row_provenance_csv_path_missing(tmp_path):
    """csv_path 제공됐으나 파일 없으면 mtime/file link 미추가 (3 triple 만)."""
    sidecar = Graph()
    inst = URIRef("http://ex.org/steel-inst/Missing_M001")
    missing_path = str(tmp_path / "does_not_exist.csv")
    added = annotate_row_provenance(
        sidecar, inst, "Missing", 1, csv_path=missing_path,
    )
    assert added == 3  # file 없으면 3 triple 만


def test_annotate_row_provenance_dedupe_requires_metadata():
    """bare type triple 만 있고 metadata 없으면 dedupe skip 안 함 (완전성 보장)."""
    sidecar = Graph()
    inst = URIRef("http://ex.org/steel-inst/EquipmentMaster_EQ001")
    row_uri = make_row_provenance_uri("Test", 1)
    # 시뮬레이션: 이전 run 이 type triple 만 남기고 망가진 상태
    sidecar.add((row_uri, RDF.type, URIRef(f"{PROV}Entity")))

    # annotate_row_provenance 는 metadata 없음 → 완전 추가해야 (type 는 중복이지만 re-add)
    added = annotate_row_provenance(sidecar, inst, "Test", 1)
    assert added >= 3  # csvTable + rowNumber + (type re-add)

    # 두 번째 호출은 metadata 완전하므로 dedupe
    added2 = annotate_row_provenance(sidecar, inst, "Test", 1)
    assert added2 == 0
