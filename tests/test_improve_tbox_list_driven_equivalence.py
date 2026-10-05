"""Phase 8 list-driven pipeline 마이그레이션 동작 보존 회귀 테스트.

본 테스트는 다음 두 가지를 보장한다:
  1. improve_tbox 가 산출하는 stats 의 핵심 키들이 안정적이다 (특수 누적 키 포함).
  2. change_log 의 step entry 가 (step, name, triples_before, triples_after,
     delta) 5-tuple 구조를 유지한다.
"""
from __future__ import annotations

import pytest

from tools.ontology_quality import improve_tbox

DOMAIN_NS = "http://www.steel.example/onto#"

_TBOX_FIXTURE = f"""\
@prefix steel: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

steel:EquipmentMaster a owl:Class ;
    rdfs:label "Equipment Master"@en, "설비 마스터"@ko ;
    rdfs:comment "설비 마스터"@ko .

steel:hasComponent a owl:ObjectProperty ;
    rdfs:label "has component"@en .

steel:equipmentMasterId a owl:DatatypeProperty ;
    rdfs:label "equipment master id"@en ;
    rdfs:domain steel:EquipmentMaster ;
    rdfs:range xsd:string .
"""


@pytest.fixture(scope="module")
def improve_result():
    return improve_tbox(_TBOX_FIXTURE)


def test_change_log_entries_have_required_keys(improve_result):
    _, stats = improve_result
    change_log = stats.get("change_log", [])
    assert change_log, "change_log entry 가 비어 있으면 모든 step 이 skip 된 것"
    for entry in change_log:
        assert {"step", "name", "triples_before", "triples_after", "delta"} <= set(entry)
        assert isinstance(entry["triples_before"], int)
        assert isinstance(entry["triples_after"], int)
        assert isinstance(entry["delta"], int)


def test_step_9_group_has_single_change_log_entry(improve_result):
    """Step 9~9f 8개 substep 은 묶음 entry 1건으로 기록된다."""
    _, stats = improve_result
    change_log = stats.get("change_log", [])
    step9_entries = [e for e in change_log if e.get("step") == 9]
    assert len(step9_entries) == 1, (
        f"step 9 묶음 entry 가 정확히 1건이어야 함, 실제 {len(step9_entries)}건: {step9_entries}"
    )
    assert step9_entries[0]["name"] == "domain_range_completion"
    # Defend against substep-leak regression — step 9 series substeps have
    # string step_numbers ("9a", "9a-2", ...) that won't match the int==9
    # filter above. If run_step_pipeline_grouped is bypassed on _STEP9_GROUP,
    # those substep entries leak as individual change_log rows. Plan §위험 #1.
    leaked = [
        e for e in change_log
        if str(e.get("step")) in {"9a", "9a-2", "9b", "9c", "9d", "9e", "9f"}
    ]
    assert leaked == [], f"step 9 substep entry 가 누출됨: {leaked}"


def test_step_12_redundant_accumulates_into_antipattern(improve_result):
    """Step 12/14 의 _step{N}_redundant_delta 는 antipattern_redundant_removed 로 누적되고 stats 에서 제거된다."""
    _, stats = improve_result
    assert "_step12_redundant_delta" not in stats
    assert "_step14_redundant_delta" not in stats
    assert "antipattern_redundant_removed" in stats
    assert isinstance(stats["antipattern_redundant_removed"], int)
    assert stats["antipattern_redundant_removed"] >= 0


def test_bnode_skolemization_step_present(improve_result):
    """기존 bnode_elimination 회귀 테스트와 동일 검증 — Phase 8 후에도 유지."""
    _, stats = improve_result
    change_log = stats.get("change_log", [])
    bnode_entries = [e for e in change_log if e.get("name") == "bnode_skolemization"]
    assert len(bnode_entries) == 1
    assert bnode_entries[0]["step"] >= 18


def test_total_triples_recorded(improve_result):
    _, stats = improve_result
    assert "total_triples" in stats
    assert isinstance(stats["total_triples"], int)
    assert stats["total_triples"] > 0
