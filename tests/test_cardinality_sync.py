"""Tests for tools/cardinality_sync.py — #8."""
from __future__ import annotations

import json

from domain.namespaces import DOMAIN_NS
from domain.tbox_utils import _new_graph
from tools.cardinality_sync import (
    _extract_owl_cardinalities,
    _extract_shacl_cardinalities,
    check_cardinality_sync,
)

TBOX = f"""@prefix steel: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

steel:EqM a owl:Class ;
    rdfs:subClassOf [
        a owl:Restriction ;
        owl:onProperty steel:hasID ;
        owl:cardinality "1"^^xsd:nonNegativeInteger
    ] .

steel:hasID a owl:DatatypeProperty ; rdfs:domain steel:EqM .
"""


SHAPES_MATCHING = f"""@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix steel: <{DOMAIN_NS}> .

[] a sh:NodeShape ;
    sh:targetClass steel:EqM ;
    sh:property [
        sh:path steel:hasID ;
        sh:minCount 1 ;
        sh:maxCount 1
    ] .
"""


SHAPES_MISMATCH = f"""@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix steel: <{DOMAIN_NS}> .

[] a sh:NodeShape ;
    sh:targetClass steel:EqM ;
    sh:property [
        sh:path steel:hasID ;
        sh:minCount 0 ;
        sh:maxCount 5
    ] .
"""


SHAPES_SHACL_ONLY = f"""@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix steel: <{DOMAIN_NS}> .

[] a sh:NodeShape ;
    sh:targetClass steel:Other ;
    sh:property [
        sh:path steel:hasName ;
        sh:minCount 1
    ] .
"""


def test_extract_owl_cardinality():
    g = _new_graph()
    g.parse(data=TBOX, format="turtle")
    out = _extract_owl_cardinalities(g)
    assert (f"{DOMAIN_NS}EqM", f"{DOMAIN_NS}hasID") in out
    entry = out[(f"{DOMAIN_NS}EqM", f"{DOMAIN_NS}hasID")]
    assert entry.get("exact") == 1


def test_extract_shacl_cardinality():
    g = _new_graph()
    g.parse(data=SHAPES_MATCHING, format="turtle")
    out = _extract_shacl_cardinalities(g)
    assert (f"{DOMAIN_NS}EqM", f"{DOMAIN_NS}hasID") in out
    entry = out[(f"{DOMAIN_NS}EqM", f"{DOMAIN_NS}hasID")]
    assert entry["min"] == 1
    assert entry["max"] == 1


def test_check_sync_consistent(tmp_path):
    tbox = tmp_path / "t.ttl"
    shapes = tmp_path / "s.ttl"
    tbox.write_text(TBOX, encoding="utf-8")
    shapes.write_text(SHAPES_MATCHING, encoding="utf-8")
    report = check_cardinality_sync(str(tbox), str(shapes))
    assert report["consistent_count"] == 1
    assert report["inconsistent"] == []


def test_check_sync_inconsistent(tmp_path):
    tbox = tmp_path / "t.ttl"
    shapes = tmp_path / "s.ttl"
    tbox.write_text(TBOX, encoding="utf-8")
    shapes.write_text(SHAPES_MISMATCH, encoding="utf-8")
    report = check_cardinality_sync(str(tbox), str(shapes))
    assert len(report["inconsistent"]) == 1
    inc = report["inconsistent"][0]
    assert "min" in inc["diff_keys"] or "max" in inc["diff_keys"]


def test_check_sync_shacl_only(tmp_path):
    tbox = tmp_path / "t.ttl"
    shapes = tmp_path / "s.ttl"
    tbox.write_text(TBOX, encoding="utf-8")
    shapes.write_text(SHAPES_SHACL_ONLY, encoding="utf-8")
    report = check_cardinality_sync(str(tbox), str(shapes))
    assert report["shacl_only"]
    assert report["owl_only"]


def test_mcp_tool_runs(tmp_path):
    from tools.cardinality_sync import check_shacl_owl_cardinality_sync

    tbox = tmp_path / "t.ttl"
    shapes = tmp_path / "s.ttl"
    tbox.write_text(TBOX, encoding="utf-8")
    shapes.write_text(SHAPES_MATCHING, encoding="utf-8")
    raw = check_shacl_owl_cardinality_sync(str(tbox), str(shapes))
    data = json.loads(raw)
    assert data["success"] is True
    assert data["severity"] == "ok"


# ── 스콜렘화된 named restriction + 축 분리 (2026-08-30) ──────────────────
#
# 두 결함이 이 감사를 무의미하게 만들고 있었다:
#
# (1) ``_extract_owl_cardinalities`` 가 ``isinstance(restr, BNode)`` 로 익명
#     restriction 만 봤는데, S3 의 step_19_bnode_skolemize 가 restriction 을 명명
#     IRI 로 바꾼다. 배포 T-Box 실측: owl:Restriction 100개 전부 URIRef, BNode 0개 →
#     **추출 0개**. 빈 ``owl_only`` 가 "누락 없음" 으로 읽혔다.
#
# (2) 기본 shapes(rules/policy/tbox_shapes.ttl)는 **T-Box 메타 shape** 이다
#     ("모든 DP 는 rdfs:label 을 가져야 한다"). OWL cardinality 는 **A-Box 개체**
#     제약이라 두 축이 대응하지 않는다. (1)만 고치면 58개가 전부 owl_only 로
#     쏟아진다 — 결함 발견이 아니라 오발화다.

#: 스콜렘화된 형태 — restriction 이 도메인 IRI 를 갖는다 (S3 산출물의 실제 모양).
TBOX_SKOLEMIZED = f"""@prefix steel: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

steel:EqM a owl:Class ; rdfs:subClassOf steel:EqM_hasID_cardinality .
steel:EqM_hasID_cardinality a owl:Restriction ;
    owl:onProperty steel:hasID ;
    owl:cardinality "1"^^xsd:nonNegativeInteger .

steel:hasID a owl:DatatypeProperty ; rdfs:domain steel:EqM .
"""

#: 도메인 클래스를 target 으로 하지 않는 메타 shape (기본 shapes 파일의 형태).
SHAPES_META_ONLY = """@prefix sh: <http://www.w3.org/ns/shacl#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

[] a sh:NodeShape ;
    sh:targetClass owl:DatatypeProperty ;
    sh:property [ sh:path rdfs:label ; sh:minCount 1 ] .
"""


def test_named_restriction_is_extracted(tmp_path):
    """THE REGRESSION: 스콜렘화된 named restriction 도 추출한다.

    예전 BNode 필터는 배포 T-Box 에서 **0개** 를 추출했다 (100개 전부 URIRef).
    """
    tbox = _new_graph()
    tbox.parse(data=TBOX_SKOLEMIZED, format="turtle")
    extracted = _extract_owl_cardinalities(tbox)
    assert len(extracted) == 1, (
        f"명명 restriction 을 추출하지 못했다 — BNode 필터가 남아 있다: {extracted}"
    )
    key = (f"{DOMAIN_NS}EqM", f"{DOMAIN_NS}hasID")
    assert extracted[key]["exact"] == 1


def test_bnode_restriction_still_extracted(tmp_path):
    """PRESERVATION: 익명 restriction 도 계속 추출한다 (기존 동작).

    종류(BNode/IRI)가 아니라 **타입**(owl:Restriction)으로 판정해야 한다.
    """
    tbox = _new_graph()
    tbox.parse(data=TBOX, format="turtle")      # BNode 형태
    assert len(_extract_owl_cardinalities(tbox)) == 1


def test_meta_shapes_are_not_compared(tmp_path):
    """축 분리: 도메인 클래스를 target 으로 하지 않는 shape 은 비교에서 제외한다."""
    tbox = tmp_path / "t.ttl"
    shapes = tmp_path / "s.ttl"
    tbox.write_text(TBOX_SKOLEMIZED, encoding="utf-8")
    shapes.write_text(SHAPES_META_ONLY, encoding="utf-8")
    report = check_cardinality_sync(str(tbox), str(shapes))
    assert report["meta_shapes_skipped"] == 1, report
    assert report["shacl_shapes_comparable"] == 0
    assert report["comparison_possible"] is False
    assert report["note"], "비교 불가 사실이 보고되지 않았다"


def test_no_comparable_shape_yields_no_false_positives(tmp_path):
    """THE FALSE-POSITIVE GUARD: 비교 대상이 없으면 owl_only 를 인쇄하지 않는다.

    모든 OWL 공리가 "SHACL 에 없다" 는 것은 참이지만 결함이 아니라 **비교 대상
    부재**다. 인쇄하면 "shape 58개를 추가하라" 는 잘못된 권고가 된다 — 이 리포에는
    게이트가 열리자 핵심 게이트 23개를 제거 후보로 인쇄한 전례가 있다.
    """
    tbox = tmp_path / "t.ttl"
    shapes = tmp_path / "s.ttl"
    tbox.write_text(TBOX_SKOLEMIZED, encoding="utf-8")
    shapes.write_text(SHAPES_META_ONLY, encoding="utf-8")
    report = check_cardinality_sync(str(tbox), str(shapes))
    assert report["owl_only"] == [], (
        f"비교 대상이 없는데 오발화를 인쇄했다: {len(report['owl_only'])}건"
    )
    assert report["shacl_only"] == []
    assert report["total_checked"] == 0
    # 그러나 추출 규모는 보고한다 — 침묵과 "추출 0개" 를 구분해야 한다.
    assert report["owl_restrictions_extracted"] == 1


def test_extraction_scale_is_reported(tmp_path):
    """``owl_only: []`` 만으로는 "누락 없음" 과 "추출 실패" 가 구분되지 않는다."""
    tbox = tmp_path / "t.ttl"
    shapes = tmp_path / "s.ttl"
    tbox.write_text(TBOX_SKOLEMIZED, encoding="utf-8")
    shapes.write_text(SHAPES_MATCHING, encoding="utf-8")
    report = check_cardinality_sync(str(tbox), str(shapes))
    assert report["owl_restrictions_extracted"] == 1
    assert report["shacl_shapes_comparable"] == 1
    assert report["comparison_possible"] is True
    assert report["consistent_count"] == 1, report


def test_real_inconsistency_is_still_caught(tmp_path):
    """PRESERVATION: 대조 가능한 shape 이 있으면 불일치를 잡는다.

    축 분리로 오발화를 없앴는데 진짜 불일치까지 못 잡으면 게이트를 끈 것이다.
    """
    tbox = tmp_path / "t.ttl"
    shapes = tmp_path / "s.ttl"
    tbox.write_text(TBOX_SKOLEMIZED, encoding="utf-8")
    shapes.write_text(SHAPES_MISMATCH, encoding="utf-8")
    report = check_cardinality_sync(str(tbox), str(shapes))
    assert report["comparison_possible"] is True
    assert len(report["inconsistent"]) == 1, report


def test_deployed_tbox_extracts_axioms_and_reports_no_false_positives():
    """실측 고정: 배포 T-Box 로 돌려도 추출 > 0 이고 오발화가 없다.

    단위 픽스처가 통과해도 배포 산출물에서 무발화일 수 있다 (이 리포의 "산출물로
    확인하라"). 기본 shapes 는 메타 shape 이므로 comparison_possible=False 가 정답.
    """
    import os

    from config import TBOX_PATH

    if not os.path.exists(TBOX_PATH):
        import pytest
        pytest.skip("배포 T-Box 없음")
    report = check_cardinality_sync()
    assert report["owl_restrictions_extracted"] > 0, (
        "배포 T-Box 에서 공리를 하나도 추출하지 못했다 — BNode 필터가 되살아났다"
    )
    assert report["owl_only"] == [], (
        f"오발화 {len(report['owl_only'])}건 — 메타 shape 을 대조 대상으로 셌다"
    )


def test_severity_is_not_ok_when_comparison_impossible(tmp_path):
    """대조 불가를 ``ok`` 로 보고하지 않는다.

    2026-08-30: 오발화를 막으려고 "대조 가능한 shape 이 0개면 비교를 건너뛴다" 로
    고쳤는데, severity 는 세 리스트가 비었는지만 봐서 "58개 공리를 추출했지만 대조
    대상이 없어 아무것도 검증하지 못했다" 가 **ok** 로 나왔다. ``ok`` 는 "대조했고
    일치했다" 로 읽히고, 그 오독이 정확히 예전 BNode 필터가 만든 상태다
    ("검증기 실패 = 검증 없음").
    """
    from tools.cardinality_sync import check_shacl_owl_cardinality_sync

    tbox = tmp_path / "t.ttl"
    shapes = tmp_path / "s.ttl"
    tbox.write_text(TBOX_SKOLEMIZED, encoding="utf-8")
    shapes.write_text(SHAPES_META_ONLY, encoding="utf-8")
    data = json.loads(check_shacl_owl_cardinality_sync(str(tbox), str(shapes)))
    assert data["comparison_possible"] is False
    assert data["severity"] != "ok", (
        "대조하지 못했는데 ok 로 보고했다 — 침묵이 PASS 로 읽힌다"
    )
    assert data["severity"] == "info", data["severity"]
    assert data["note"]


def test_severity_ok_only_when_actually_compared(tmp_path):
    """PRESERVATION: 실제로 대조해 일치하면 ``ok`` 다.

    ``info`` 를 무조건 쓰면 "일치했다" 신호를 잃는다.
    """
    from tools.cardinality_sync import check_shacl_owl_cardinality_sync

    tbox = tmp_path / "t.ttl"
    shapes = tmp_path / "s.ttl"
    tbox.write_text(TBOX_SKOLEMIZED, encoding="utf-8")
    shapes.write_text(SHAPES_MATCHING, encoding="utf-8")
    data = json.loads(check_shacl_owl_cardinality_sync(str(tbox), str(shapes)))
    assert data["comparison_possible"] is True
    assert data["severity"] == "ok", data


def test_severity_critical_survives_the_new_branch(tmp_path):
    """PRESERVATION: 불일치는 여전히 critical 이다."""
    from tools.cardinality_sync import check_shacl_owl_cardinality_sync

    tbox = tmp_path / "t.ttl"
    shapes = tmp_path / "s.ttl"
    tbox.write_text(TBOX_SKOLEMIZED, encoding="utf-8")
    shapes.write_text(SHAPES_MISMATCH, encoding="utf-8")
    data = json.loads(check_shacl_owl_cardinality_sync(str(tbox), str(shapes)))
    assert data["severity"] == "critical", data
