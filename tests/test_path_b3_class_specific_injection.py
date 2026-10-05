"""Path B-3 — on-demand class-specific DP 주입 검증.

_plan_common_dp_injection 이 generic "hasTimestamp" 대신
class-specific "equipmentStatusTimestamp" 를 계획에 담고,
_inject_common_dps 가 rdfs:domain 을 명시 주입하는지 확인.
"""
from __future__ import annotations

import os
import tempfile

from rdflib import OWL, RDF, RDFS, URIRef

from domain.tbox_utils import _new_graph


def _write_csv(tmpdir, name: str, headers: list[str]) -> str:
    p = os.path.join(tmpdir, f"{name}.csv")
    with open(p, "w", encoding="utf-8") as f:
        f.write(",".join(headers) + "\n")
    return p


def _minimal_tbox_info(class_props: dict):
    dt_props = {}
    dp_domains: dict[str, set[str]] = {}
    for cls, props in class_props.items():
        for key, name in props.items():
            dt_props[key] = name
            dp_domains.setdefault(key, set()).add(cls)
    return {
        "classes": {cls.lower(): cls for cls in class_props},
        "datatype_properties": dt_props,
        "object_properties": {},
        "class_props": class_props,
        "dp_ranges": {},
        "dp_domains": dp_domains,
        "subclass_of": {},
    }


def test_plan_uses_class_specific_name():
    """EquipmentStatus + Timestamp 컬럼 → equipmentStatusTimestamp 생성."""
    from tools.abox_generation import _plan_common_dp_injection
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = _write_csv(tmp, "Equipment_Status", ["Equipment_ID", "Timestamp", "Status"])
        tbox_info = _minimal_tbox_info({"EquipmentStatus": {}})
        plan = _plan_common_dp_injection(tbox_info, [csv_path], filter_names=None)
    # 계획에 class-specific 이름이 있어야 함
    names = [item["dp_name"] for item in plan]
    assert "equipmentStatusTimestamp" in names
    # domain_class 도 채워졌는지
    entry = next(item for item in plan if item["dp_name"] == "equipmentStatusTimestamp")
    assert entry["domain_class"] == "EquipmentStatus"
    # 역추적 메타데이터
    assert entry["_derived_from_common"] == "hasTimestamp"


def test_plan_deduplicates_same_class_column():
    """같은 (class, dp_name) 쌍은 한 번만 계획."""
    from tools.abox_generation import _plan_common_dp_injection
    with tempfile.TemporaryDirectory() as tmp:
        # 같은 class 에 2개 csv 가 모두 Timestamp 컬럼
        csv1 = _write_csv(tmp, "Equipment_Status", ["Timestamp", "A"])
        _write_csv(tmp, "Equipment_Status_Extra", ["Timestamp", "B"])
        # filter_names 로 Equipment_Status 만 지정 — 한번만 처리
        tbox_info = _minimal_tbox_info({"EquipmentStatus": {}})
        plan = _plan_common_dp_injection(tbox_info, [csv1], filter_names=None)
    matches = [item for item in plan if item["dp_name"] == "equipmentStatusTimestamp"]
    assert len(matches) == 1


def test_inject_adds_rdfs_domain():
    """Path B-3: _inject_common_dps 가 rdfs:domain 을 명시 주입."""
    from tools.abox_generation import _inject_common_dps
    # 최소 T-Box
    base_ttl = """
@prefix steel: <http://example.com/steel-ontology#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
steel:EquipmentStatus a owl:Class .
"""
    plan = [{
        "dp_name": "equipmentStatusTimestamp",
        "range": "http://www.w3.org/2001/XMLSchema#dateTime",
        "domain_class": "EquipmentStatus",
        "label_en": "equipment status timestamp",
        "label_ko": "설비 상태 시각",
        "comment_en": "", "comment_ko": "",
        "source_columns": [("Equipment_Status", "Timestamp")],
        "_derived_from_common": "hasTimestamp",
    }]
    updated_ttl, stats = _inject_common_dps(base_ttl, plan)
    assert stats["injected"] == 1

    # rdfs:domain 이 실제로 주입됐는지 확인
    g = _new_graph()
    g.parse(data=updated_ttl, format="turtle")
    DOMAIN_NS = "http://example.com/steel-ontology#"
    dp_uri = URIRef(DOMAIN_NS + "equipmentStatusTimestamp")
    assert (dp_uri, RDF.type, OWL.DatatypeProperty) in g
    # domain 이 EquipmentStatus 로 명시되어 있는지
    domains = list(g.objects(dp_uri, RDFS.domain))
    assert URIRef(DOMAIN_NS + "EquipmentStatus") in domains


def test_inject_same_dp_multiple_classes_extends_domain():
    """동일 이름 DP 가 여러 class 에 주입 계획되면 같은 DP 에 rdfs:domain 만 추가."""
    from tools.abox_generation import _inject_common_dps
    base_ttl = """
@prefix steel: <http://example.com/steel-ontology#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
steel:Foo a owl:Class .
steel:Bar a owl:Class .
"""
    # 동일 dp_name = fooTimestamp 가 두 class 에 매핑 (이론적 케이스)
    plan = [
        {"dp_name": "sharedDp", "range": "http://www.w3.org/2001/XMLSchema#string",
         "domain_class": "Foo", "label_en": "", "label_ko": "",
         "comment_en": "", "comment_ko": "", "source_columns": [], "_derived_from_common": ""},
        {"dp_name": "sharedDp", "range": "http://www.w3.org/2001/XMLSchema#string",
         "domain_class": "Bar", "label_en": "", "label_ko": "",
         "comment_en": "", "comment_ko": "", "source_columns": [], "_derived_from_common": ""},
    ]
    updated_ttl, stats = _inject_common_dps(base_ttl, plan)
    g = _new_graph()
    g.parse(data=updated_ttl, format="turtle")
    DOMAIN_NS = "http://example.com/steel-ontology#"
    dp_uri = URIRef(DOMAIN_NS + "sharedDp")
    domains = set(g.objects(dp_uri, RDFS.domain))
    # 양쪽 class 모두 domain 에 포함
    assert URIRef(DOMAIN_NS + "Foo") in domains
    assert URIRef(DOMAIN_NS + "Bar") in domains


def test_inject_skips_when_dp_and_domain_already_declared():
    """이미 T-Box 에 (DP, domain) 쌍이 있으면 중복 주입 안 함."""
    from tools.abox_generation import _inject_common_dps
    base_ttl = """
@prefix steel: <http://example.com/steel-ontology#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
steel:Equipment a owl:Class .
steel:equipmentTimestamp a owl:DatatypeProperty ;
  rdfs:domain steel:Equipment .
"""
    plan = [{
        "dp_name": "equipmentTimestamp", "range": "http://www.w3.org/2001/XMLSchema#dateTime",
        "domain_class": "Equipment", "label_en": "", "label_ko": "",
        "comment_en": "", "comment_ko": "", "source_columns": [], "_derived_from_common": "hasTimestamp",
    }]
    _, stats = _inject_common_dps(base_ttl, plan)
    # 이미 (equipmentTimestamp, Equipment) 선언 있음 → 0 injected
    assert stats["injected"] == 0


def test_inject_empty_plan_returns_original():
    """빈 plan → stats 0, TTL 변경 없음."""
    from tools.abox_generation import _inject_common_dps
    base_ttl = "@prefix steel: <http://example.com/steel-ontology#> .\n"
    updated, stats = _inject_common_dps(base_ttl, [])
    assert stats["injected"] == 0
    assert stats["by_name"] == {}
    assert updated == base_ttl
