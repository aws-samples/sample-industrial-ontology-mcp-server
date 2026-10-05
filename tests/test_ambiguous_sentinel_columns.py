"""A-Box 적재: 모호한 NULL 센티널("-")이 실제 값인 컬럼 회귀 가드.

`_NULL_SENTINELS` 에는 `"-"` 가 들어 있어, 컬럼별 판정이 없으면 A-Box 생성기는
값 `-` 를 전부 NULL 로 간주한다. 그런데 공차처럼 부호를 별도 컬럼에 담는
테이블에서는 `-` 가 **마이너스 부호** 다. 가상의 예:

    TOL_SIGN_1 : {'-', '+'}         (distinct 2)
    TOL_SIGN_2 : {'-', '0', '+'}    (distinct 3)

이런 컬럼에서 `-` 를 NULL 로 거르면 **음수 부호만 KG 에서 사라지고** 양수만
남는다. 부호가 없으면 공차 하한을 해석할 수 없는데도 **에러 없이 조용히**
빠진다 (`sentinel_filtered_count` 에만 집계된다).

반대로 이름 같은 자유 텍스트 컬럼 (예: `NAME_COL_1`) 은 서로 다른 값이 많고
`-` 가 드물게 섞이므로, 그 `-` 는 진짜 플레이스홀더다. 따라서 값 하나만 보고
판정할 수 없고 **컬럼의 값 어휘** 로 갈라야 한다: 닫힌 소수 집합(부호) vs
자유 텍스트.
"""
from __future__ import annotations

from tools.abox_generation import (
    _AMBIGUOUS_SENTINELS,
    _SENTINEL_VALUE_COLUMN_MAX_DISTINCT,
    _detect_sentinel_value_columns,
    _format_value,
)


def _rows(col: str, values: list[str]) -> list[dict]:
    return [{col: v} for v in values]


# --------------------------------------------------------------------------
# _detect_sentinel_value_columns
# --------------------------------------------------------------------------

def test_sign_column_detected_as_value_column():
    """{-, +} 는 닫힌 부호 집합 → '-' 는 값."""
    rows = _rows("TOL_SIGN_1", ["-"] * 30 + ["+"] * 20)
    assert _detect_sentinel_value_columns(rows) == {"tol_sign_1"}


def test_three_symbol_sign_column_detected():
    """세 기호 어휘 {-, 0, 1} 도 닫힌 집합이다."""
    rows = _rows("MEASURE_COL_1", ["-"] * 3 + ["0"] * 10 + ["1"] * 12)
    assert _detect_sentinel_value_columns(rows) == {"measure_col_1"}


def test_free_text_column_not_detected():
    """이름 컬럼에 드물게 섞인 '-' 는 플레이스홀더이므로 값으로 승격하지 않는다."""
    names = [f"NAME_{i}" for i in range(50)]
    rows = _rows("NAME_COL_1", names + ["-", "-"])
    assert _detect_sentinel_value_columns(rows) == set()


def test_column_without_sentinel_not_detected():
    """'-' 가 아예 없으면 대상 아님 (불필요한 예외를 만들지 않는다)."""
    rows = _rows("SOME_FLAG", ["Y"] * 10 + ["N"] * 10)
    assert _detect_sentinel_value_columns(rows) == set()


def test_boundary_at_max_distinct():
    """임계치 경계: distinct == 상한이면 값 컬럼, 초과하면 아니다."""
    n = _SENTINEL_VALUE_COLUMN_MAX_DISTINCT
    inside = _rows("C", ["-"] + [f"v{i}" for i in range(n - 1)])
    outside = _rows("C", ["-"] + [f"v{i}" for i in range(n)])
    assert _detect_sentinel_value_columns(inside) == {"c"}
    assert _detect_sentinel_value_columns(outside) == set()


def test_empty_rows_safe():
    assert _detect_sentinel_value_columns([]) == set()


def test_blank_values_do_not_count_toward_vocabulary():
    """빈 문자열은 어휘가 아니다 — 빈 값 많은 부호 컬럼도 감지돼야 한다."""
    rows = _rows("TOL_SIGN_1", ["-"] * 10 + [""] * 500 + ["+"] * 3)
    assert _detect_sentinel_value_columns(rows) == {"tol_sign_1"}


def test_multiple_columns_judged_independently():
    """한 테이블 안에서 부호 컬럼과 텍스트 컬럼이 각각 판정된다."""
    rows = [
        {"TOL_SIGN_1": "-", "NAME_COL_1": f"N{i}"} for i in range(20)
    ] + [{"TOL_SIGN_1": "+", "NAME_COL_1": "-"}]
    assert _detect_sentinel_value_columns(rows) == {"tol_sign_1"}


# --------------------------------------------------------------------------
# _format_value — the flag must actually change the outcome
# --------------------------------------------------------------------------

def test_format_value_drops_dash_by_default():
    """기본 동작은 불변 — 하위 호환 (기존 도메인의 '-' 플레이스홀더 필터 유지)."""
    assert _format_value("-", "someProp") is None


def test_format_value_keeps_dash_when_column_is_value_column():
    """THE REGRESSION: 부호 컬럼의 '-' 는 리터럴로 남아야 한다."""
    lit = _format_value("-", "orderDimToleranceTolSign1",
                        sentinel_is_value=True)
    assert lit is not None
    assert str(lit) == "-"


def test_format_value_keeps_dash_with_string_range():
    """T-Box range 가 xsd:string 이어도 통과 (부호 DP 의 일반적인 선언과 같은 조건)."""
    lit = _format_value("-", "orderDimToleranceTolSign1",
                        tbox_range="http://www.w3.org/2001/XMLSchema#string",
                        sentinel_is_value=True)
    assert lit is not None and str(lit) == "-"


def test_format_value_flag_does_not_revive_other_sentinels():
    """예외는 '-' 에 한정 — 'N/A' 나 'NULL' 은 플래그와 무관하게 계속 필터."""
    for junk in ("N/A", "null", "none", "nan", "미정", "해당없음"):
        assert _format_value(junk, "p", sentinel_is_value=True) is None, junk


def test_ambiguous_set_is_narrow():
    """'-' 만 모호 취급 — 목록이 넓어지면 조용한 오적재 위험이 커진다."""
    assert frozenset({"-"}) == _AMBIGUOUS_SENTINELS


def test_format_value_still_drops_empty_and_whitespace():
    assert _format_value("", "p", sentinel_is_value=True) is None
    assert _format_value("   ", "p", sentinel_is_value=True) is None


# --------------------------------------------------------------------------
# 검증기도 같은 판정을 써야 한다 — 문자열 패턴 체크의 '-' 면제
# --------------------------------------------------------------------------
#
# 적재기가 부호 값을 정당하게 넣었는데 문자열 패턴 체크가 `INVALID_PATTERNS` 에
# 든 '-' 를 무조건 "무효 문자열" 로 신고하면, 적재기가 보존한 부호 값이 곧바로
# S9 FAIL 로 되돌아온다. 두 쪽이 같은 근거 (`dcterms:source` → 컬럼 → 값 어휘)
# 로 판정해야 한다.

import csv as _csv  # noqa: E402
import os as _os  # noqa: E402

from rdflib import OWL, RDF, RDFS, XSD, Graph, Literal, Namespace, URIRef  # noqa: E402

from domain.tbox_utils import _new_graph  # noqa: E402
from tools.validation_support.checks import statistical as st  # noqa: E402

_STEEL = "http://example.com/steel-ontology#"
_INST = "http://example.com/steel-ontology/instances#"
_DCTERMS = Namespace("http://purl.org/dc/terms/")


def _sign_tbox() -> Graph:
    g = _new_graph()
    cls = URIRef(_STEEL + "OrderToleranceSpec")
    g.add((cls, RDF.type, OWL.Class))
    dp = URIRef(_STEEL + "orderDimToleranceTolSign1")
    g.add((dp, RDF.type, OWL.DatatypeProperty))
    g.add((dp, RDFS.domain, cls))
    g.add((dp, RDFS.range, XSD.string))
    g.add((dp, _DCTERMS.source, Literal("TOL_SIGN_1")))
    return g


def _sign_abox(dashes: int, plusses: int) -> Graph:
    g = _new_graph()
    cls = URIRef(_STEEL + "OrderToleranceSpec")
    dp = URIRef(_STEEL + "orderDimToleranceTolSign1")
    for i in range(dashes + plusses):
        s = URIRef(f"{_INST}OrderToleranceSpec_{i:05d}")
        g.add((s, RDF.type, cls))
        g.add((s, dp, Literal("-" if i < dashes else "+")))
    return g


def _stage_sign_csv(tmp_path, monkeypatch, *, dashes=90, plusses=10):
    path = _os.path.join(str(tmp_path), "TOL.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = _csv.writer(f)
        w.writerow(["TOL_SIGN_1"])
        for i in range(dashes + plusses):
            w.writerow(["-" if i < dashes else "+"])
    monkeypatch.setattr(st, "SOURCE_RAWDATA_DIR", str(tmp_path), raising=False)
    import config
    monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", str(tmp_path))
    from tools.validation_support.checks import temporal_cardinality as tc
    tc._dp_source_columns.cache_clear() if hasattr(
        tc._dp_source_columns, "cache_clear") else None


def test_dps_where_dash_is_data_resolves_sign_column(tmp_path, monkeypatch):
    _stage_sign_csv(tmp_path, monkeypatch)
    assert st._dps_where_dash_is_data(_sign_tbox()) == \
        {"orderDimToleranceTolSign1"}


def test_dps_where_dash_is_data_empty_without_tbox():
    assert st._dps_where_dash_is_data(None) == set()


def test_string_patterns_exempts_sign_dashes(tmp_path, monkeypatch):
    """THE REGRESSION: 부호 '-' 는 '무효 문자열' 로 신고되지 않아야 한다."""
    _stage_sign_csv(tmp_path, monkeypatch)
    result = st.check_string_patterns(_sign_abox(90, 10), tbox=_sign_tbox())
    assert result["invalid_warning_count"] == 0, result["warnings"]
    assert result["passed"] is True
    assert result["dash_values_exempted"] == 90
    assert result["dash_as_data_properties"] == ["orderDimToleranceTolSign1"]


def test_string_patterns_still_flags_dash_without_tbox(tmp_path, monkeypatch):
    """tbox 미제공 시엔 면제 근거가 없으므로 기존 동작 유지 (하위 호환)."""
    _stage_sign_csv(tmp_path, monkeypatch)
    result = st.check_string_patterns(_sign_abox(90, 10))
    assert result["invalid_warning_count"] == 1
    assert result["dash_values_exempted"] == 0


def test_string_patterns_still_flags_other_invalid_values(tmp_path, monkeypatch):
    """면제는 '-' 한정 — 같은 DP 의 'null' 값은 계속 무효로 신고."""
    _stage_sign_csv(tmp_path, monkeypatch)
    g = _sign_abox(90, 9)
    dp = URIRef(_STEEL + "orderDimToleranceTolSign1")
    g.add((URIRef(f"{_INST}OrderToleranceSpec_99999"), dp, Literal("null")))
    result = st.check_string_patterns(g, tbox=_sign_tbox())
    assert result["invalid_warning_count"] == 1
    bad = [w for w in result["warnings"] if w["issue"] == "무효 문자열 값"][0]
    assert bad["invalid_count"] == 1        # the 'null', not the 90 dashes
