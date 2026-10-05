"""기능 단계 — improve_tbox_quality Step 12b class-specific DP validator 검증.

_enforce_class_specific_dps 가 generic DP / orphan DP / owl:Thing domain DP 를
탐지하고 환경변수 TBOX_STRICT_CLASS_SPECIFIC 에 따라 warn/rename/remove 적용.
"""
from __future__ import annotations

import os
from unittest.mock import patch

from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef

from domain.tbox_utils import _new_graph

STEEL_STR = "http://example.com/steel-ontology#"


def _cls(name: str) -> URIRef:
    return URIRef(STEEL_STR + name)


def _dp(name: str) -> URIRef:
    return URIRef(STEEL_STR + name)


def _minimal_graph_with_generic_dp() -> Graph:
    """hasValue (generic, domain class 1개) 가 포함된 최소 T-Box."""
    g = _new_graph()
    # 클래스
    g.add((_cls("EquipmentMaster"), RDF.type, OWL.Class))
    # generic DP — hasValue
    g.add((_dp("hasValue"), RDF.type, OWL.DatatypeProperty))
    g.add((_dp("hasValue"), RDFS.domain, _cls("EquipmentMaster")))
    # 정상 class-specific DP — 대조군
    g.add((_dp("equipmentMasterId"), RDF.type, OWL.DatatypeProperty))
    g.add((_dp("equipmentMasterId"), RDFS.domain, _cls("EquipmentMaster")))
    return g


def test_warn_mode_default_keeps_dp_but_records_stats():
    """TBOX_STRICT_CLASS_SPECIFIC 미설정 → warn 모드, T-Box 유지."""
    from tools.ontology_quality import _enforce_class_specific_dps
    g = _minimal_graph_with_generic_dp()
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("TBOX_STRICT_CLASS_SPECIFIC", None)
        stats = _enforce_class_specific_dps(g, STEEL_STR)

    assert stats["class_specific_mode"] == "warn"
    assert stats["class_specific_violations_generic_name"] == 1
    assert stats["class_specific_renamed"] == 0
    assert stats["class_specific_removed"] == 0
    # T-Box 는 유지 — hasValue 여전히 존재
    assert (_dp("hasValue"), RDF.type, OWL.DatatypeProperty) in g


def test_rename_mode_injects_class_prefix():
    """rename 모드: hasValue (domain=EquipmentMaster 단일) → equipmentMasterValue."""
    from tools.ontology_quality import _enforce_class_specific_dps
    g = _minimal_graph_with_generic_dp()
    with patch.dict(os.environ, {"TBOX_STRICT_CLASS_SPECIFIC": "rename"}):
        stats = _enforce_class_specific_dps(g, STEEL_STR)

    assert stats["class_specific_mode"] == "rename"
    assert stats["class_specific_renamed"] == 1
    # 원본 hasValue 삭제, equipmentMasterValue 존재
    assert (_dp("hasValue"), RDF.type, OWL.DatatypeProperty) not in g
    assert (_dp("equipmentMasterValue"), RDF.type, OWL.DatatypeProperty) in g
    # domain 도 이동
    assert (_dp("equipmentMasterValue"), RDFS.domain, _cls("EquipmentMaster")) in g


def test_remove_mode_deletes_generic_dp():
    """remove 모드: hasValue 관련 triple 전부 삭제."""
    from tools.ontology_quality import _enforce_class_specific_dps
    g = _minimal_graph_with_generic_dp()
    with patch.dict(os.environ, {"TBOX_STRICT_CLASS_SPECIFIC": "remove"}):
        stats = _enforce_class_specific_dps(g, STEEL_STR)

    assert stats["class_specific_mode"] == "remove"
    assert stats["class_specific_removed"] == 1
    assert (_dp("hasValue"), RDF.type, OWL.DatatypeProperty) not in g
    # hasValue domain 도 삭제됨
    assert (_dp("hasValue"), RDFS.domain, _cls("EquipmentMaster")) not in g
    # 정상 DP 는 유지
    assert (_dp("equipmentMasterId"), RDF.type, OWL.DatatypeProperty) in g


def test_orphan_dp_detected():
    """domain 없는 DP (orphan) 도 위반으로 감지."""
    from tools.ontology_quality import _enforce_class_specific_dps
    g = _new_graph()
    g.add((_dp("timestamp"), RDF.type, OWL.DatatypeProperty))
    # domain 없음

    with patch.dict(os.environ, {"TBOX_STRICT_CLASS_SPECIFIC": "warn"}):
        stats = _enforce_class_specific_dps(g, STEEL_STR)

    # timestamp 는 generic 이름은 아니지만 orphan
    assert stats["class_specific_violations_orphan"] == 1
    # sample 에 기록
    samples = stats["class_specific_sample_violations"]
    assert any(v["dp"] == "timestamp" and "orphan_domain" in v["reasons"]
               for v in samples)


def test_owl_thing_domain_detected():
    """domain=owl:Thing 인 DP 도 위반."""
    from tools.ontology_quality import _enforce_class_specific_dps
    g = _new_graph()
    g.add((_dp("someProperty"), RDF.type, OWL.DatatypeProperty))
    g.add((_dp("someProperty"), RDFS.domain, OWL.Thing))

    with patch.dict(os.environ, {"TBOX_STRICT_CLASS_SPECIFIC": "warn"}):
        stats = _enforce_class_specific_dps(g, STEEL_STR)

    assert stats["class_specific_violations_owl_thing"] == 1


def test_class_specific_dp_passes_unchecked():
    """정상 class-specific DP (equipmentMasterId 등) 는 위반 카운트 증가 없음."""
    from tools.ontology_quality import _enforce_class_specific_dps
    g = _new_graph()
    g.add((_cls("EquipmentMaster"), RDF.type, OWL.Class))
    g.add((_dp("equipmentMasterId"), RDF.type, OWL.DatatypeProperty))
    g.add((_dp("equipmentMasterId"), RDFS.domain, _cls("EquipmentMaster")))

    with patch.dict(os.environ, {"TBOX_STRICT_CLASS_SPECIFIC": "remove"}):
        stats = _enforce_class_specific_dps(g, STEEL_STR)

    assert stats["class_specific_violations_generic_name"] == 0
    assert stats["class_specific_violations_orphan"] == 0
    assert stats["class_specific_violations_owl_thing"] == 0
    assert stats["class_specific_removed"] == 0
    # 여전히 존재
    assert (_dp("equipmentMasterId"), RDF.type, OWL.DatatypeProperty) in g


def test_rename_skips_when_target_already_exists():
    """rename 대상 이름이 이미 T-Box 에 있으면 rename skip (충돌 회피)."""
    from tools.ontology_quality import _enforce_class_specific_dps
    g = _minimal_graph_with_generic_dp()
    # 이미 equipmentMasterValue 존재 — hasValue rename 대상과 충돌
    g.add((_dp("equipmentMasterValue"), RDF.type, OWL.DatatypeProperty))
    g.add((_dp("equipmentMasterValue"), RDFS.domain, _cls("EquipmentMaster")))

    with patch.dict(os.environ, {"TBOX_STRICT_CLASS_SPECIFIC": "rename"}):
        stats = _enforce_class_specific_dps(g, STEEL_STR)

    # rename skip — 원본 hasValue 유지
    assert (_dp("hasValue"), RDF.type, OWL.DatatypeProperty) in g
    assert stats["class_specific_renamed"] == 0


def test_invalid_mode_falls_back_to_warn():
    """TBOX_STRICT_CLASS_SPECIFIC 이 알려지지 않은 값이면 warn 으로 폴백."""
    from tools.ontology_quality import _enforce_class_specific_dps
    g = _minimal_graph_with_generic_dp()
    with patch.dict(os.environ, {"TBOX_STRICT_CLASS_SPECIFIC": "yolo"}):
        stats = _enforce_class_specific_dps(g, STEEL_STR)

    assert stats["class_specific_mode"] == "warn"
    # T-Box 유지
    assert (_dp("hasValue"), RDF.type, OWL.DatatypeProperty) in g


def test_multiple_violation_reasons_tracked():
    """한 DP 가 여러 위반 사유 (generic + orphan) 을 동시에 갖으면 모두 기록."""
    from tools.ontology_quality import _enforce_class_specific_dps
    g = _new_graph()
    # hasStatus — generic 이름 + domain 없음 (double violation)
    g.add((_dp("hasStatus"), RDF.type, OWL.DatatypeProperty))

    with patch.dict(os.environ, {"TBOX_STRICT_CLASS_SPECIFIC": "warn"}):
        stats = _enforce_class_specific_dps(g, STEEL_STR)

    assert stats["class_specific_violations_generic_name"] == 1
    assert stats["class_specific_violations_orphan"] == 1
    # sample 에 두 reason 모두
    samples = stats["class_specific_sample_violations"]
    sv = next((v for v in samples if v["dp"] == "hasStatus"), None)
    assert sv is not None
    assert "generic_name" in sv["reasons"]
    assert "orphan_domain" in sv["reasons"]


def test_rename_preserves_domain_range_and_label():
    """rename 시 rdfs:domain / rdfs:range / rdfs:label 등 triple 도 같이 이동."""
    from rdflib import XSD

    from tools.ontology_quality import _enforce_class_specific_dps
    g = _new_graph()
    g.add((_cls("EquipmentMaster"), RDF.type, OWL.Class))
    g.add((_dp("hasValue"), RDF.type, OWL.DatatypeProperty))
    g.add((_dp("hasValue"), RDFS.domain, _cls("EquipmentMaster")))
    g.add((_dp("hasValue"), RDFS.range, XSD.decimal))
    g.add((_dp("hasValue"), RDFS.label, Literal("has value", lang="en")))

    with patch.dict(os.environ, {"TBOX_STRICT_CLASS_SPECIFIC": "rename"}):
        _enforce_class_specific_dps(g, STEEL_STR)

    new_uri = _dp("equipmentMasterValue")
    assert (new_uri, RDFS.domain, _cls("EquipmentMaster")) in g
    assert (new_uri, RDFS.range, XSD.decimal) in g
    assert (new_uri, RDFS.label, Literal("has value", lang="en")) in g
