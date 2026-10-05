"""프로퍼티별 완전성 — 희소 컬럼 면제가 `dcterms:source` 로 해석되는지 회귀 가드.

배경 (2026-07-26 실측): `check_property_completeness` 는 "CSV 원본이 원래 희소한
optional 컬럼" 을 위반으로 오보고하지 않도록 임계치를 CSV 충진율까지 낮추는 면제
장치를 갖고 있다. 그런데 조회 키가 **DP local-name 을 컬럼명으로 가정**했다:

    체크의 조회 키 : slabspecificationtypecol1   ← DP 이름
    CSV 사전의 키  : typecol1                    ← 컬럼명
    → 영구 miss

Path B (class-specific DP) 전환 이후 모든 DP 에 클래스 접두사가 붙으므로 이
경로는 **구조적으로 매칭이 불가능**했다. 면제 장치가 통째로 죽어 실데이터가
정상인데도 862건이 위반으로 보고됐다 (S9 19/22 중 1건). 실측 대조:

    materialSpecATypeCol1  보고 9.4%  = CSV 원본 9.4%
    materialASpecFlagCol1                       보고 48.4% = CSV 원본 48.4%

해법은 이름 추측을 버리고 T-Box 의 `dcterms:source` 표기를 쓰는 것 —
컬럼 코드는 Oracle 스키마에서 오므로 LLM 작명과 무관하게 안정적이다.

**두 번째 함정 (같은 수정 중 발견)**: 컬럼명 색인은 같은 이름이 여러 테이블에
있으면 **가장 낮은 충진율** 을 남긴다. 경보를 올릴 때는 보수적이라 안전하지만
**경보를 면제할 때는 위험하다**. 실측: `TIMESTAMP_COL_1` 는 SRC_TBL_13 에서
2.0% 인데 다른 테이블에서 0.11% 라, 면제 기준선이 0.11% 로 내려가 20배 적재
누락도 통과시킨다. 그래서 `(클래스, 컬럼)` 으로 좁힌 색인을 먼저 조회한다.
"""
from __future__ import annotations

import csv
import os

import pytest
from rdflib import OWL, RDF, RDFS, XSD, Literal, Namespace, URIRef

from domain.tbox_utils import _new_graph
from tools.validation_support.checks import temporal_cardinality as tc

STEEL = "http://example.com/steel-ontology#"
INST = "http://example.com/steel-ontology/instances#"
DCTERMS = Namespace("http://purl.org/dc/terms/")


def _clear_caches() -> None:
    """Clear the process-global fill-rate caches.

    Tolerates a monkeypatched replacement (a plain lambda has no cache_clear).
    """
    for fn in (tc._csv_column_fill_rate, tc._csv_class_column_fill_rate,
               tc._csv_class_column_filled_count, tc._table_class_map,
               tc._null_markers):
        clear = getattr(fn, "cache_clear", None)
        if clear is not None:
            clear()


@pytest.fixture(autouse=True)
def _isolate_cache():
    """The fill-rate cache is process-global; clear around every test."""
    _clear_caches()
    yield
    _clear_caches()


def _write_csv(path: str, header: list[str], rows: list[list[str]]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def _build_tbox(*, with_source: bool = True) -> object:
    """One class with a sparse DP; `dcterms:source` optionally declared."""
    g = _new_graph()
    cls = URIRef(STEEL + "MaterialSpecA")
    g.add((cls, RDF.type, OWL.Class))
    dp = URIRef(STEEL + "materialSpecATypeCol1")
    g.add((dp, RDF.type, OWL.DatatypeProperty))
    g.add((dp, RDFS.domain, cls))
    g.add((dp, RDFS.range, XSD.string))
    if with_source:
        g.add((dp, DCTERMS.source, Literal("TYPE_COL_1")))
    return g


def _build_abox(total: int, filled: int) -> object:
    g = _new_graph()
    cls = URIRef(STEEL + "MaterialSpecA")
    dp = URIRef(STEEL + "materialSpecATypeCol1")
    for i in range(total):
        s = URIRef(f"{INST}MaterialSpecA_{i:05d}")
        g.add((s, RDF.type, cls))
        if i < filled:
            g.add((s, dp, Literal("A")))
    return g


def _stage_csv(tmp_path, monkeypatch, *, total: int, filled: int,
               map_class: bool = True) -> None:
    """Write a rawdata CSV whose column has the given fill ratio.

    ``map_class``: 테이블→클래스 매핑도 스텁한다. 매핑이 없으면 이 클래스가
    "소스 테이블 없는 파생 분류" 로 분류돼 판정에서 제외되므로
    (``derived_classes_skipped``), 완전성 로직 자체를 검증할 수 없다.
    실제 ``rules/domain/table_class_mapping.json`` 을 읽으면 픽스처 클래스가 없어 항상
    제외된다 — 반드시 스텁해야 한다.
    """
    path = os.path.join(str(tmp_path), "OWN_TABLE.csv")
    rows = [["x" if i < filled else ""] for i in range(total)]
    _write_csv(path, ["TYPE_COL_1"], rows)
    monkeypatch.setattr(tc, "SOURCE_RAWDATA_DIR", str(tmp_path))
    if map_class:
        monkeypatch.setattr(tc, "_table_class_map",
                            lambda: {"OWN_TABLE": "MaterialSpecA"})
    _clear_caches()


# --------------------------------------------------------------------------
# _csv_column_fill_rate — dual key registration
# --------------------------------------------------------------------------

def test_fill_rate_registers_upper_column_code(tmp_path, monkeypatch):
    """The raw column code (UPPER, underscores kept) must be a lookup key."""
    _stage_csv(tmp_path, monkeypatch, total=100, filled=10)
    rates = tc._csv_column_fill_rate()
    assert "TYPE_COL_1" in rates
    assert rates["TYPE_COL_1"] == pytest.approx(0.10)


def test_fill_rate_keeps_legacy_transliteration_key(tmp_path, monkeypatch):
    """Legacy key must survive so unannotated T-Boxes still get the exemption."""
    _stage_csv(tmp_path, monkeypatch, total=100, filled=10)
    rates = tc._csv_column_fill_rate()
    assert "typecol1" in rates
    assert rates["typecol1"] == pytest.approx(0.10)


def test_fill_rate_prefers_lowest_across_tables(tmp_path, monkeypatch):
    """Same column in two tables → conservative (lowest) rate wins."""
    _write_csv(os.path.join(str(tmp_path), "a.csv"), ["COL_X"],
               [["v"] for _ in range(10)])                       # 100%
    _write_csv(os.path.join(str(tmp_path), "b.csv"), ["COL_X"],
               [["v"]] + [[""] for _ in range(9)])               # 10%
    monkeypatch.setattr(tc, "SOURCE_RAWDATA_DIR", str(tmp_path))
    _clear_caches()
    rates = tc._csv_column_fill_rate()
    assert rates["COL_X"] == pytest.approx(0.10)


def test_fill_rate_treats_null_markers_as_empty(tmp_path, monkeypatch):
    """'NULL'/'N/A'/'NaN' are placeholders, not values."""
    _write_csv(os.path.join(str(tmp_path), "a.csv"), ["COL_X"],
               [["v"], ["NULL"], ["N/A"], ["nan"]])
    monkeypatch.setattr(tc, "SOURCE_RAWDATA_DIR", str(tmp_path))
    _clear_caches()
    assert tc._csv_column_fill_rate()["COL_X"] == pytest.approx(0.25)


# --------------------------------------------------------------------------
# _dp_source_columns
# --------------------------------------------------------------------------

def test_dp_source_columns_maps_name_to_upper_column():
    mapping = tc._dp_source_columns(_build_tbox(with_source=True))
    assert mapping["materialSpecATypeCol1"] == \
        "TYPE_COL_1"


def test_dp_source_columns_empty_without_annotation():
    assert tc._dp_source_columns(_build_tbox(with_source=False)) == {}


def test_dp_source_columns_skips_ambiguous_multi_source():
    """Two source columns on one DP → no defensible baseline, so skip it."""
    g = _build_tbox(with_source=True)
    dp = URIRef(STEEL + "materialSpecATypeCol1")
    g.add((dp, DCTERMS.source, Literal("OTHER_COLUMN")))
    assert "materialSpecATypeCol1" not in tc._dp_source_columns(g)


# --------------------------------------------------------------------------
# End-to-end: the exemption must actually fire
# --------------------------------------------------------------------------

def test_sparse_column_not_flagged_when_source_annotated(tmp_path, monkeypatch):
    """THE REGRESSION: A-Box fill == CSV fill must not be reported incomplete."""
    _stage_csv(tmp_path, monkeypatch, total=100, filled=9)
    result = tc.check_property_completeness(
        _build_abox(total=100, filled=9), _build_tbox(with_source=True))
    assert result["incomplete_count"] == 0, result["incomplete_properties"]
    assert result["passed"] is True
    assert result["threshold_relaxed"] == 1
    assert result["dp_source_annotated"] == 1


def test_sparse_column_flagged_without_source_annotation(tmp_path, monkeypatch):
    """No annotation and a class-prefixed name → nothing to resolve, so it fails.

    This documents the pre-fix behaviour as the *fallback*, not as correct:
    without provenance there is genuinely no way to know the column's baseline.
    """
    _stage_csv(tmp_path, monkeypatch, total=100, filled=9)
    result = tc.check_property_completeness(
        _build_abox(total=100, filled=9), _build_tbox(with_source=False))
    assert result["incomplete_count"] == 1
    assert result["dp_source_annotated"] == 0
    assert result["threshold_relaxed"] == 0


def test_real_loss_still_flagged(tmp_path, monkeypatch):
    """A-Box fill far below the CSV baseline is a genuine defect — keep failing."""
    _stage_csv(tmp_path, monkeypatch, total=100, filled=90)   # CSV 90%
    result = tc.check_property_completeness(
        _build_abox(total=100, filled=5), _build_tbox(with_source=True))  # A-Box 5%
    assert result["incomplete_count"] == 1
    hit = result["incomplete_properties"][0]
    assert hit["completeness_pct"] == 5.0
    assert hit["source_column"] == "TYPE_COL_1"


def test_source_pointing_at_absent_column_falls_back(tmp_path, monkeypatch):
    """Annotation naming a column absent from the CSV dir must not silently pass."""
    _write_csv(os.path.join(str(tmp_path), "OWN_TABLE.csv"), ["UNRELATED"],
               [["v"] for _ in range(10)])
    monkeypatch.setattr(tc, "SOURCE_RAWDATA_DIR", str(tmp_path))
    # 클래스는 매핑돼 있어야 판정 대상이 된다 (_stage_csv 주석 참조).
    monkeypatch.setattr(tc, "_table_class_map",
                        lambda: {"OWN_TABLE": "MaterialSpecA"})
    _clear_caches()
    result = tc.check_property_completeness(
        _build_abox(total=100, filled=9), _build_tbox(with_source=True))
    assert result["incomplete_count"] == 1
    assert result["threshold_relaxed"] == 0


# --------------------------------------------------------------------------
# Class scoping — the unscoped "lowest rate wins" rule must not waive alarms
# --------------------------------------------------------------------------

def _stage_two_tables(tmp_path, monkeypatch) -> None:
    """Same column in two tables at very different fill rates.

    OWN table (mapped to MaterialSpecA) = 20%; FOREIGN table = 1%.
    An unscoped baseline would relax the threshold to 1% and hide real loss.
    """
    own = os.path.join(str(tmp_path), "OWN_TABLE.csv")
    foreign = os.path.join(str(tmp_path), "FOREIGN_TABLE.csv")
    _write_csv(own, ["TYPE_COL_1"],
               [["v" if i < 20 else ""] for i in range(100)])
    _write_csv(foreign, ["TYPE_COL_1"],
               [["v" if i < 1 else ""] for i in range(100)])
    monkeypatch.setattr(tc, "SOURCE_RAWDATA_DIR", str(tmp_path))
    monkeypatch.setattr(
        tc, "_table_class_map",
        lambda: {"OWN_TABLE": "MaterialSpecA", "FOREIGN_TABLE": "OtherClass"})
    _clear_caches()


def test_scoped_rate_uses_owning_table(tmp_path, monkeypatch):
    _stage_two_tables(tmp_path, monkeypatch)
    scoped = tc._csv_class_column_fill_rate()
    assert scoped[("MaterialSpecA", "TYPE_COL_1")] == \
        pytest.approx(0.20)
    assert scoped[("OtherClass", "TYPE_COL_1")] == pytest.approx(0.01)


def test_scoping_still_flags_loss_hidden_by_global_minimum(tmp_path, monkeypatch):
    """THE SECOND REGRESSION: a foreign table's sparsity must not waive our alarm.

    A-Box holds 5% but this class's own table holds 20% → genuine load loss.
    The unscoped index would report the baseline as 1% and pass.
    """
    _stage_two_tables(tmp_path, monkeypatch)
    result = tc.check_property_completeness(
        _build_abox(total=100, filled=5), _build_tbox(with_source=True))
    assert result["incomplete_count"] == 1
    assert result["incomplete_properties"][0]["effective_threshold"] == 20.0


def test_scoping_waives_when_own_table_is_sparse(tmp_path, monkeypatch):
    """Mirror case: A-Box matches its own table's 20% → not a defect."""
    _stage_two_tables(tmp_path, monkeypatch)
    result = tc.check_property_completeness(
        _build_abox(total=100, filled=20), _build_tbox(with_source=True))
    assert result["incomplete_count"] == 0
    assert result["threshold_relaxed"] == 1


def test_unmapped_class_falls_back_to_global_index(tmp_path, monkeypatch):
    """No table→class mapping at all → unscoped index still applies."""
    _stage_csv(tmp_path, monkeypatch, total=100, filled=9)
    monkeypatch.setattr(tc, "_table_class_map", lambda: {})
    _clear_caches()
    result = tc.check_property_completeness(
        _build_abox(total=100, filled=9), _build_tbox(with_source=True))
    assert result["incomplete_count"] == 0
    assert result["threshold_relaxed"] == 1


# --------------------------------------------------------------------------
# NULL markers must match the loader's, or filtered rows read as load loss
# --------------------------------------------------------------------------

def test_null_markers_match_loader_except_ambiguous():
    """체크의 NULL 목록 = 적재기 목록 − 컬럼별 판정 대상('-')."""
    from tools.abox_generation import _AMBIGUOUS_SENTINELS, _NULL_SENTINELS

    assert tc._null_markers() == frozenset(_NULL_SENTINELS) - _AMBIGUOUS_SENTINELS
    # 'undefined' is the marker that actually caused the false FAIL.
    assert "undefined" in tc._null_markers()
    # '-' must NOT be global: sign columns keep it as data.
    assert "-" not in tc._null_markers()


def test_is_filled_rejects_loader_sentinels():
    for junk in ("", "   ", "NULL", "undefined", "N/A", "none", "nan", "미정", "해당없음"):
        assert tc._is_filled(junk) is False, junk
    for real in ("CODEVAL01", "0", "-", "+"):
        assert tc._is_filled(real) is True, real


def test_undefined_rows_not_counted_as_csv_baseline(tmp_path, monkeypatch):
    """THE REGRESSION: 'undefined' 행은 CSV 기준선에서 빠져야 한다.

    실측: MaterialA.ID_COL_4 13,339행 중 6,784행이 문자열 'undefined'.
    적재기는 건너뛰는데 체크는 값으로 세어 CSV 100% → A-Box 49.1% 를 손실로 신고.
    """
    path = os.path.join(str(tmp_path), "OWN_TABLE.csv")
    rows = [["CODEVAL01"] if i < 49 else ["undefined"] for i in range(100)]
    _write_csv(path, ["TYPE_COL_1"], rows)
    monkeypatch.setattr(tc, "SOURCE_RAWDATA_DIR", str(tmp_path))
    monkeypatch.setattr(tc, "_table_class_map",
                        lambda: {"OWN_TABLE": "MaterialSpecA"})
    _clear_caches()
    scoped = tc._csv_class_column_fill_rate()
    assert scoped[("MaterialSpecA", "TYPE_COL_1")] == \
        pytest.approx(0.49)
    # A-Box mirrors the loader (49 filled) → no defect.
    result = tc.check_property_completeness(
        _build_abox(total=100, filled=49), _build_tbox(with_source=True))
    assert result["incomplete_count"] == 0


# --------------------------------------------------------------------------
# Derived classes (no CSV table of their own) must not be failed
# --------------------------------------------------------------------------

def test_class_without_source_table_is_skipped(tmp_path, monkeypatch):
    """FacilityOperation 류: 소스 테이블이 없으면 기준선이 없으므로 판정 제외."""
    _stage_csv(tmp_path, monkeypatch, total=100, filled=9)
    monkeypatch.setattr(tc, "_table_class_map",
                        lambda: {"OTHER_TABLE": "SomeOtherClass"})
    _clear_caches()
    result = tc.check_property_completeness(
        _build_abox(total=100, filled=9), _build_tbox(with_source=True))
    assert result["passed"] is True
    assert result["incomplete_count"] == 0
    assert result["derived_classes_skipped"] == ["MaterialSpecA"]
    assert result["checked_classes"] == 0


def test_mapped_class_still_judged(tmp_path, monkeypatch):
    """반대 방향 가드: 소스 테이블이 있는 클래스는 계속 판정된다."""
    _stage_two_tables(tmp_path, monkeypatch)
    result = tc.check_property_completeness(
        _build_abox(total=100, filled=5), _build_tbox(with_source=True))
    assert result["derived_classes_skipped"] == []
    assert result["checked_classes"] == 1
    assert result["incomplete_count"] == 1


# --------------------------------------------------------------------------
# Exact-count escape hatch — percentage shortfall ≠ loss
# --------------------------------------------------------------------------

def _stage_mapped_csv(tmp_path, monkeypatch, *, total: int, filled: int) -> None:
    path = os.path.join(str(tmp_path), "OWN_TABLE.csv")
    _write_csv(path, ["TYPE_COL_1"],
               [["v" if i < filled else ""] for i in range(total)])
    monkeypatch.setattr(tc, "SOURCE_RAWDATA_DIR", str(tmp_path))
    monkeypatch.setattr(tc, "_table_class_map",
                        lambda: {"OWN_TABLE": "MaterialSpecA"})
    _clear_caches()


def test_filled_count_index_matches_csv(tmp_path, monkeypatch):
    _stage_mapped_csv(tmp_path, monkeypatch, total=100, filled=25)
    counts = tc._csv_class_column_filled_count()
    assert counts[("MaterialSpecA", "TYPE_COL_1")] == 25


def test_no_fail_when_abox_holds_every_csv_value(tmp_path, monkeypatch):
    """THE REGRESSION: 분모가 1 커져 반올림으로 미달해도 손실이 아니다.

    실측: OrderToleranceSpec.MEASURE_COL_2 는 CSV 4,990건을
    전부 적재했는데 인스턴스가 약 1.2만 → 약 1.2만 이라 41.05% < 41.06% 로 FAIL.

    여기서는 임계치가 CSV 비율(25%)로 내려간 상태에서 A-Box 분모만 1 큰
    24.8% 를 만들어 같은 경계를 재현한다.
    """
    _stage_mapped_csv(tmp_path, monkeypatch, total=100, filled=25)
    result = tc.check_property_completeness(
        _build_abox(total=101, filled=25), _build_tbox(with_source=True))
    assert result["incomplete_count"] == 0
    assert result["fully_loaded_below_threshold"] == 1
    assert result["passed"] is True


def test_still_fails_when_values_actually_missing(tmp_path, monkeypatch):
    """탈출구는 '전량 적재' 에만 적용 — 한 건이라도 빠지면 계속 FAIL."""
    _stage_mapped_csv(tmp_path, monkeypatch, total=100, filled=25)
    result = tc.check_property_completeness(
        _build_abox(total=100, filled=20), _build_tbox(with_source=True))
    assert result["incomplete_count"] == 1
    assert result["fully_loaded_below_threshold"] == 0
    assert result["incomplete_properties"][0]["csv_filled"] == 25
