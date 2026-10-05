"""CQ ``domains`` (원본 테이블명) → T-Box 클래스명 해석 회귀 가드.

배경 (2026-07-25 실측): CQ 의 ``domains`` 는 원본 CSV 테이블명
(``SOURCE_TABLE_007``) 을 담는데, Multi-Agent T-Box 협업의
여러 지점이 밑줄만 제거해 (``TBSOURCETABLE007``) 클래스명으로
사용했다. 그 결과:

  1. ``_cq_runtime_check`` 가 존재하는 클래스를 못 찾아 CQ 를 미답변으로 오판
  2. ``_generate_op_skeletons_from_cq_gaps`` 가 그 오판을 근거로 테이블명을
     domain/range 로 갖는 OP 를 주입
  3. 후처리가 참조된 클래스를 실제로 생성 → T-Box 계층에 테이블명 클래스 6개
     오염 (S2 재생성 실측)

해결: ``rules/domain/table_class_mapping.json`` 을 경유하는
``_resolve_domain_to_class`` 를 공용으로 사용. 미등록 테이블은 기존 밑줄 제거로
폴백해 하위 호환 유지.
"""
from __future__ import annotations

import pytest

from tools.competency_questions import (
    _load_table_class_map,
    _resolve_domain_to_class,
)
from tools.multi_agent_tbox import (
    _cq_runtime_check,
    _generate_op_skeletons_from_cq_gaps,
)

TBOX_TTL = """
@prefix steel: <http://example.com/steel-ontology#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

steel:ProcessResultD a owl:Class .
steel:ProcessStepD a owl:Class .
steel:Order a owl:Class .
"""

# 실제 CQ 의 domains — 전부 원본 테이블명.
CQ_WITH_TABLE_DOMAINS = [{
    "id": "CQ01",
    "domains": [
        "SOURCE_TABLE_007",   # → ProcessResultD
        "SOURCE_TABLE_008",   # → ProcessStepD
        "SOURCE_TABLE_001",    # → Order
    ],
}]


def _looks_like_table_name(value: str) -> bool:
    """테이블명이 클래스명 자리에 새어들어왔는지 판별."""
    return (value.upper().startswith(("TB", "SOURCETABLE", "SOURCE_TABLE"))
            and any(c.isdigit() for c in value))


def test_step_11c_merges_table_name_classes_and_derived_names(monkeypatch):
    """Step 11c 가 테이블명 클래스와 그 이름을 품은 파생 엔티티를 정리한다.

    파생 엔티티까지 봐야 하는 이유: Step 19 (skolemization) 가 restriction URI 를
    ``{클래스}_{프로퍼티}_{타입}`` 으로 조립하므로 오염된 클래스명이 스콜렘
    이름에 굳는다 (``TBSOURCETABLE007..._isOrderedBy_someValuesFrom``).
    """
    import re

    from rdflib import OWL, RDF, RDFS, URIRef

    from domain.tbox_utils import _new_graph
    from tools.quality_steps import step_11c_table_name_class_merge as s11c
    from tools.quality_steps._base import StepContext

    steel = "http://example.com/steel-ontology#"
    # 실제 매핑은 도메인 종속이라 **구조만** 확인하고, 병합 검증은 스텁으로 한다.
    real = s11c._load_table_name_aliases()
    # 별칭은 "밑줄 제거형 테이블명 != 매핑된 클래스명" 인 항목에서만 생긴다.
    # 두 값이 같은 매핑(예: Failure_Cause → FailureCause)만 있으면 비는 것이 정상이므로
    # 개수 대신 **형태** 만 검사한다 — 도메인에 따라 0개일 수 있다.
    assert all(a.isalnum() for a in real), \
        "별칭 키는 밑줄이 제거된 테이블명 형태여야 한다"

    mangled = "TBSOURCETABLE007"
    monkeypatch.setattr(s11c, "_load_table_name_aliases",
                        lambda: {mangled: "ProcessResultD"})

    g = _new_graph()
    canonical = URIRef(steel + "ProcessResultD")
    polluted = URIRef(steel + mangled)
    g.add((canonical, RDF.type, OWL.Class))
    g.add((polluted, RDF.type, OWL.Class))
    # 오염 클래스를 range 로 쓰는 OP — 병합 후에도 살아 있어야 한다.
    op = URIRef(steel + "isOrderedBy")
    g.add((op, RDF.type, OWL.ObjectProperty))
    g.add((op, RDFS.range, polluted))
    # 스콜렘 restriction 이름에 오염 클래스명이 굳은 경우.
    skolem = URIRef(steel + f"{mangled}_isOrderedBy_someValuesFrom")
    g.add((skolem, RDF.type, OWL.Class))
    g.add((canonical, RDFS.subClassOf, skolem))

    result = s11c.apply(g, StepContext(domain_ns=steel))
    assert result.stats["merge_map"][mangled] == "ProcessResultD"
    assert result.stats["derived_entities_renamed"] >= 1

    remaining = {
        str(t)[len(steel):]
        for t in set(g.subjects()) | set(g.objects()) | set(g.predicates())
        if isinstance(t, URIRef) and str(t).startswith(steel)
    }
    assert not [n for n in remaining if re.search(r"TB[A-Z0-9]{6,}", n.upper())]
    # OP 는 살아 있고 range 가 정식 클래스로 옮겨졌어야 한다.
    assert (op, RDFS.range, canonical) in g


def test_step_11c_is_noop_without_pollution():
    """오염이 없으면 그래프를 건드리지 않는다 (멱등·안전)."""
    from rdflib import OWL, RDF, URIRef

    from domain.tbox_utils import _new_graph
    from tools.quality_steps import step_11c_table_name_class_merge as s11c
    from tools.quality_steps._base import StepContext

    steel = "http://example.com/steel-ontology#"
    g = _new_graph()
    g.add((URIRef(steel + "ProcessResultD"), RDF.type, OWL.Class))
    before = len(g)

    result = s11c.apply(g, StepContext(domain_ns=steel))
    assert result.stats["table_name_classes_merged"] == 0
    assert result.stats["derived_entities_renamed"] == 0
    assert len(g) == before


def test_mapping_resolves_source_tables_to_domain_classes():
    """table_class_mapping 을 경유하면 원본 테이블명이 도메인 클래스로 해석된다.

    매핑은 도메인마다 다르므로 실제 파일 값에 기대지 않고 스텁을 주입한다
    (실제 파일에 기대면 도메인 전환 시 테스트가 깨진다).
    """
    mapping = {
        "SOURCE_TABLE_007": "ProcessResultD",
        "SOURCE_TABLE_003": "MaterialADetail",
    }
    assert _resolve_domain_to_class("SOURCE_TABLE_007", mapping) == "ProcessResultD"
    assert _resolve_domain_to_class("SOURCE_TABLE_003", mapping) == "MaterialADetail"


def test_real_mapping_file_loads_and_resolves():
    """실제 ``rules/domain/table_class_mapping.json`` 이 로드되고 값을 해석한다.

    이름은 도메인 종속이라 검사하지 않고 **구조** 만 본다 — 매핑이 비어 있지
    않고, 등록된 테이블은 밑줄 제거 폴백이 아닌 실제 클래스명으로 해석되는지.
    """
    mapping = _load_table_class_map()
    assert mapping, "table_class_mapping.json 이 비어 있다"

    # 모든 등록 테이블이 선언된 클래스명으로 해석돼야 한다.
    for table, expected in mapping.items():
        assert _resolve_domain_to_class(table, mapping) == expected

    # 폴백과 매핑을 구분하려면 "밑줄 제거형 != 클래스명" 인 항목이 필요하다.
    # 그런 항목이 없는 도메인(둘이 우연히 같은 매핑만 존재)에서는 구분 불가이므로 skip.
    distinguishable = [
        (t, c) for t, c in mapping.items()
        if c.split(":")[-1] != t.replace("_", "")
    ]
    if not distinguishable:
        pytest.skip("이 도메인 매핑은 폴백 결과와 구분되지 않는다 (밑줄 제거형 == 클래스명)")
    table, expected = distinguishable[0]
    assert _resolve_domain_to_class(table, mapping) != table.replace("_", "")


def test_unknown_table_falls_back_to_underscore_strip():
    """미등록 테이블은 기존 동작(밑줄 제거)으로 폴백한다."""
    assert _resolve_domain_to_class("Equipment_Master", {}) == "EquipmentMaster"


#: 테스트용 테이블→클래스 매핑. 실제 파일은 도메인 종속이라 스텁을 주입한다.
_STUB_MAP = {
    "SOURCE_TABLE_007": "ProcessResultD",
    "SOURCE_TABLE_008": "ProcessStepD",
    "SOURCE_TABLE_001": "Order",
}


@pytest.fixture
def stub_table_map(monkeypatch):
    """``_load_table_class_map`` 을 스텁으로 교체."""
    import tools.competency_questions as cq
    monkeypatch.setattr(cq, "_load_table_class_map", lambda: dict(_STUB_MAP))
    monkeypatch.setattr(cq, "_TABLE_CLASS_MAP", dict(_STUB_MAP), raising=False)
    yield


def test_cq_runtime_check_recognises_mapped_classes(stub_table_map):
    """CQ 도메인이 매핑을 거쳐 T-Box 클래스로 인식된다.

    미해석 시 '클래스 누락' 이 아니라 '경로 없음' 으로 보고돼야 한다 —
    클래스는 존재하고 OP 만 없는 상태이므로.
    """
    result = _cq_runtime_check(TBOX_TTL, CQ_WITH_TABLE_DOMAINS)
    reasons = " ".join(reason for _, reason in result["unanswerable"])
    assert "클래스 누락" not in reasons
    assert "processresultd" in reasons.lower() or "processstepd" in reasons.lower()
    # 테이블명이 이유 문자열에 노출되지 않아야 한다.
    assert "sourcetable" not in reasons.lower()


def test_skeleton_injection_never_references_table_names(stub_table_map, monkeypatch):
    """주입되는 OP 의 domain/range 가 실제 클래스명이어야 한다.

    이 가드가 없으면 T-Box 계층에 테이블명 클래스가 생성된다.

    FK 근거 게이트(2026-08-14 신설)는 이 테스트의 관심사가 아니므로 **판정 불가**
    (``None``) 로 스텁해 비활성화한다 — 픽스처 클래스는 실제 CSV 에 없어 게이트가
    정당하게 주입을 막고, 그러면 이 테스트가 검증하려는 "테이블명 유출" 경로에
    도달하지 못한다. 게이트 자체는 ``tests/test_cq_skeleton_fk_gate.py`` 가 고정한다.
    """
    monkeypatch.setattr("tools.multi_agent_tbox._csv_fk_pairs", lambda: None)
    runtime = _cq_runtime_check(TBOX_TTL, CQ_WITH_TABLE_DOMAINS)
    skeletons = _generate_op_skeletons_from_cq_gaps(runtime, CQ_WITH_TABLE_DOMAINS)
    assert skeletons, "경로 부재 CQ 에 대해 skeleton 이 생성돼야 한다"

    leaked = {
        value
        for action in skeletons
        for value in action.values()
        if isinstance(value, str) and _looks_like_table_name(value)
    }
    assert not leaked, f"테이블명이 클래스명으로 유출됨: {leaked}"

    # domain/range 를 명시하는 액션은 실제 클래스명을 써야 한다.
    for action in skeletons:
        if "domain" in action:
            assert action["domain"] in {"ProcessResultD", "ProcessStepD", "Order"}
            assert action["range"] in {"ProcessResultD", "ProcessStepD", "Order"}
