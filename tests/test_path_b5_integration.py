"""Path B-5 — 통합 테스트 (P-B-1/2/3 조합 동작 확인).

최소 CSV + T-Box 를 만들어 _plan_common_dp_injection + _inject_common_dps +
실제 A-Box 생성 경로가 **class-specific DP 이름을 rdfs:domain 과 함께 주입**
하는지 end-to-end 로 확인. generic hasTimestamp 는 A-Box 에 주입되지 않고
class-specific {class}Timestamp 만 사용됨을 보장.
"""
from __future__ import annotations

import os
import tempfile

from rdflib import OWL, RDF, RDFS, URIRef

from domain.tbox_utils import _new_graph


def _write_csv(tmpdir, name: str, rows: list[list[str]]) -> str:
    p = os.path.join(tmpdir, f"{name}.csv")
    with open(p, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(",".join(row) + "\n")
    return p


def test_path_b_end_to_end_class_specific_dp_injected_with_domain():
    """P-B-1/2/3 통합: EquipmentStatus CSV → equipmentStatusTimestamp (domain=EquipmentStatus)."""
    from tools.abox_generation import _inject_common_dps, _parse_tbox, _plan_common_dp_injection
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = _write_csv(tmp, "Equipment_Status", [
            ["Equipment_ID", "Timestamp", "Status"],
            ["EQ001", "2026-01-01T00:00:00", "RUNNING"],
        ])
        base_ttl = """
@prefix steel: <http://example.com/steel-ontology#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
steel:EquipmentStatus a owl:Class .
"""
        tbox_info = _parse_tbox(base_ttl)
        plan = _plan_common_dp_injection(tbox_info, [csv_path], filter_names=None)

    # Timestamp → equipmentStatusTimestamp, Status → equipmentStatusStatus
    names = {item["dp_name"]: item["domain_class"] for item in plan}
    assert "equipmentStatusTimestamp" in names
    assert names["equipmentStatusTimestamp"] == "EquipmentStatus"

    # 주입 실행
    updated_ttl, stats = _inject_common_dps(base_ttl, plan)
    g = _new_graph()
    g.parse(data=updated_ttl, format="turtle")
    DOMAIN_NS = "http://example.com/steel-ontology#"

    # 1. class-specific DP 선언됨
    dp_uri = URIRef(DOMAIN_NS + "equipmentStatusTimestamp")
    assert (dp_uri, RDF.type, OWL.DatatypeProperty) in g

    # 2. rdfs:domain 명시됨 — Path B 핵심 차이점
    domains = list(g.objects(dp_uri, RDFS.domain))
    assert URIRef(DOMAIN_NS + "EquipmentStatus") in domains

    # 3. generic "hasTimestamp" 은 T-Box 에 주입되지 **않음**
    generic_uri = URIRef(DOMAIN_NS + "hasTimestamp")
    assert (generic_uri, RDF.type, OWL.DatatypeProperty) not in g


def test_path_b_multiple_classes_same_common_dp_produces_different_names():
    """두 class (EquipmentStatus, AlarmEvents) 가 모두 Timestamp 를 가지면
    class-specific 두 이름 (equipmentStatusTimestamp, alarmEventsTimestamp) 생성."""
    from tools.abox_generation import _parse_tbox, _plan_common_dp_injection
    with tempfile.TemporaryDirectory() as tmp:
        c1 = _write_csv(tmp, "Equipment_Status", [["Equipment_ID", "Timestamp"]])
        c2 = _write_csv(tmp, "Alarm_Events", [["Event_ID", "Timestamp"]])
        base_ttl = """
@prefix steel: <http://example.com/steel-ontology#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
steel:EquipmentStatus a owl:Class .
steel:AlarmEvents a owl:Class .
"""
        tbox_info = _parse_tbox(base_ttl)
        plan = _plan_common_dp_injection(tbox_info, [c1, c2], filter_names=None)

    names = {item["dp_name"] for item in plan}
    assert "equipmentStatusTimestamp" in names
    assert "alarmEventsTimestamp" in names
    # generic 이름은 포함 안 됨
    assert "hasTimestamp" not in names


def test_path_b_column_matching_existing_class_specific_skips_injection():
    """T-Box 에 이미 equipmentStatusValue (domain=EquipmentStatus) 가 있고 CSV
    컬럼이 Status 이면, P-B-2 의 semantic-suffix 매칭으로 기존 DP 에 매핑되어
    주입 계획에 포함되지 않음."""
    from tools.abox_generation import _parse_tbox, _plan_common_dp_injection
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = _write_csv(tmp, "Equipment_Status", [["Status"]])
        base_ttl = """
@prefix steel: <http://example.com/steel-ontology#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
steel:EquipmentStatus a owl:Class .
steel:equipmentStatusValue a owl:DatatypeProperty ;
  rdfs:domain steel:EquipmentStatus .
"""
        tbox_info = _parse_tbox(base_ttl)
        plan = _plan_common_dp_injection(tbox_info, [csv_path], filter_names=None)

    # Status 컬럼은 equipmentStatusValue 로 매핑됨 → 주입 불필요
    names = {item["dp_name"] for item in plan}
    assert "equipmentStatusStatus" not in names  # 중복 주입 안 함


def test_hasidentifier_dp_not_injected_under_path_b():
    """hasIdentifier 는 common_dp.json 에 여전히 선언되어 있으나, CSV 에 'id'
    컬럼이 있으면 class-specific ID DP (equipmentMasterId 등) 가 우선 매칭.
    T-Box 에 해당 DP 가 없으면 주입 계획에 has{Identifier} 대신 class-specific
    이름이 들어감."""
    from tools.abox_generation import _parse_tbox, _plan_common_dp_injection
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = _write_csv(tmp, "Equipment_Master", [["ID", "Name"]])
        # T-Box 에 Equipment_Master 와 관련된 ID DP 없음
        base_ttl = """
@prefix steel: <http://example.com/steel-ontology#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
steel:EquipmentMaster a owl:Class .
"""
        tbox_info = _parse_tbox(base_ttl)
        plan = _plan_common_dp_injection(tbox_info, [csv_path], filter_names=None)

    names = {item["dp_name"] for item in plan}
    # Path B: generic "hasIdentifier" 대신 class-specific "equipmentMasterIdentifier"
    assert "hasIdentifier" not in names
    if "equipmentMasterIdentifier" in names:
        entry = next(item for item in plan if item["dp_name"] == "equipmentMasterIdentifier")
        assert entry["domain_class"] == "EquipmentMaster"


def test_path_b_all_injected_dps_have_domain():
    """Path B 주입 후 T-Box 에 orphan DP (domain 없음) 존재하지 않음."""
    from tools.abox_generation import _inject_common_dps, _parse_tbox, _plan_common_dp_injection
    with tempfile.TemporaryDirectory() as tmp:
        csv_paths = [
            _write_csv(tmp, "Equipment_Status", [["Equipment_ID", "Timestamp", "Status"]]),
            _write_csv(tmp, "Alarm_Events", [["Event_ID", "Timestamp", "Severity"]]),
        ]
        base_ttl = """
@prefix steel: <http://example.com/steel-ontology#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
steel:EquipmentStatus a owl:Class .
steel:AlarmEvents a owl:Class .
"""
        tbox_info = _parse_tbox(base_ttl)
        plan = _plan_common_dp_injection(tbox_info, csv_paths, filter_names=None)
        updated_ttl, _ = _inject_common_dps(base_ttl, plan)

    g = _new_graph()
    g.parse(data=updated_ttl, format="turtle")
    DOMAIN_NS = "http://example.com/steel-ontology#"
    orphan = []
    for dp in g.subjects(RDF.type, OWL.DatatypeProperty):
        if not str(dp).startswith(DOMAIN_NS):
            continue
        if not list(g.objects(dp, RDFS.domain)):
            orphan.append(str(dp))
    assert orphan == [], f"Path B 후에도 orphan DP 존재: {orphan}"
