"""Q1~Q5 improve_tbox / jury_fixes / validation 강화 회귀 테스트."""
from __future__ import annotations

from rdflib import OWL, RDF, RDFS, Graph, Literal, Namespace, URIRef

from tools.ontology_quality import (
    _inject_missing_labels,
    _local_name_to_human,
    _synthesize_korean_label,
    improve_tbox,
)

DOMAIN_NS = Namespace("http://example.com/steel-ontology#")
STEEL_STR = str(DOMAIN_NS)


def _parse(ttl: str) -> Graph:
    g = Graph()
    g.parse(data=ttl, format="turtle")
    return g


# ── Q1: Transitive + inverseOf 충돌 자동 수정 ─────────

def test_q1_transitive_with_inverse_is_removed():
    ttl = f"""\
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix steel: <{STEEL_STR}> .

steel:A a owl:Class ; rdfs:label "A"@en, "A"@ko ; rdfs:comment "A"@ko .
steel:followsProcess a owl:ObjectProperty, owl:TransitiveProperty ;
    rdfs:domain steel:A ; rdfs:range steel:A ;
    rdfs:label "follows"@en, "뒤따름"@ko ;
    rdfs:comment "공정 순서"@ko ;
    owl:inverseOf steel:precedes .
steel:precedes a owl:ObjectProperty ;
    rdfs:domain steel:A ; rdfs:range steel:A ;
    rdfs:label "precedes"@en, "선행"@ko ;
    rdfs:comment "공정 순서 역방향"@ko .
"""
    improved_ttl, stats = improve_tbox(ttl)
    g = _parse(improved_ttl)
    assert (DOMAIN_NS.followsProcess, RDF.type, OWL.TransitiveProperty) not in g
    # inverseOf 는 유지
    assert (DOMAIN_NS.followsProcess, OWL.inverseOf, DOMAIN_NS.precedes) in g
    assert stats.get("antipattern_transitive_with_inverse_removed", 0) >= 1


def test_q1_pure_transitive_is_preserved():
    """inverseOf 가 없는 TransitiveProperty 는 건드리지 않아야 한다."""
    ttl = f"""\
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix steel: <{STEEL_STR}> .

steel:A a owl:Class ; rdfs:label "A"@en, "A"@ko ; rdfs:comment "A"@ko .
steel:isPartOf a owl:ObjectProperty, owl:TransitiveProperty ;
    rdfs:domain steel:A ; rdfs:range steel:A ;
    rdfs:label "is part of"@en, "부분"@ko ;
    rdfs:comment "전체-부분"@ko .
"""
    improved_ttl, stats = improve_tbox(ttl)
    g = _parse(improved_ttl)
    assert (DOMAIN_NS.isPartOf, RDF.type, OWL.TransitiveProperty) in g
    assert stats.get("antipattern_transitive_with_inverse_removed", 0) == 0


# ── Q2: 라벨 자동 생성 ─────────────────────────────

def test_q2_missing_labels_are_injected():
    g = Graph()
    g.add((DOMAIN_NS.DataQualityCode, RDF.type, OWL.Class))
    g.add((DOMAIN_NS.hasEquipment, RDF.type, OWL.ObjectProperty))
    result = _inject_missing_labels(g, STEEL_STR)
    assert result["added_en"] >= 2
    assert result["added_ko"] >= 2
    en_labels = {
        str(label)
        for label in g.objects(DOMAIN_NS.DataQualityCode, RDFS.label)
        if isinstance(label, Literal) and label.language == "en"
    }
    assert "data quality code" in en_labels


def test_q2_existing_labels_are_preserved():
    g = Graph()
    g.add((DOMAIN_NS.A, RDF.type, OWL.Class))
    g.add((DOMAIN_NS.A, RDFS.label, Literal("Existing A", lang="en")))
    result = _inject_missing_labels(g, STEEL_STR)
    # @en 은 있으므로 추가 안 함, @ko 만 추가
    assert result["added_en"] == 0
    assert result["added_ko"] == 1
    en_labels = [
        str(label)
        for label in g.objects(DOMAIN_NS.A, RDFS.label)
        if isinstance(label, Literal) and label.language == "en"
    ]
    assert en_labels == ["Existing A"]


def test_q2_local_name_to_human():
    assert _local_name_to_human("hasEquipmentMaster") == "has equipment master"
    assert _local_name_to_human("GHGEmission") == "ghg emission"
    assert _local_name_to_human("DataQualityCode") == "data quality code"


def test_q2_synthesize_korean_label_picks_up_known_words():
    assert "설비" in _synthesize_korean_label("has equipment master")


# ── Q3: DP domain 2개 → 공통 조상으로 롤업 ─────────

def test_q3_dp_with_two_domains_and_common_ancestor_is_merged():
    ttl = f"""\
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix steel: <{STEEL_STR}> .

steel:Parent a owl:Class ; rdfs:label "Parent"@en, "상위"@ko ; rdfs:comment "상위"@ko .
steel:ChildA a owl:Class ; rdfs:subClassOf steel:Parent ;
    rdfs:label "Child A"@en, "자식A"@ko ; rdfs:comment "자식A"@ko .
steel:ChildB a owl:Class ; rdfs:subClassOf steel:Parent ;
    rdfs:label "Child B"@en, "자식B"@ko ; rdfs:comment "자식B"@ko .
steel:sharedField a owl:DatatypeProperty ;
    rdfs:domain steel:ChildA ;
    rdfs:domain steel:ChildB ;
    rdfs:range xsd:string ;
    rdfs:label "shared field"@en, "공유 필드"@ko ;
    rdfs:comment "공유"@ko .
"""
    improved_ttl, stats = improve_tbox(ttl)
    g = _parse(improved_ttl)
    domains = list(g.objects(DOMAIN_NS.sharedField, RDFS.domain))
    # 공통 조상(Parent)으로 롤업되어 1개만 남아야 함
    assert len(domains) == 1
    assert domains[0] == DOMAIN_NS.Parent
    assert stats.get("dp_domain_merged_to_ancestor", 0) >= 1


def test_q3_ext_op_with_two_domains_is_merged_like_dp():
    """ObjectProperty 도 DP 와 동일 로직으로 공통 조상 롤업돼야 함."""
    ttl = f"""\
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix steel: <{STEEL_STR}> .

steel:Parent a owl:Class ; rdfs:label "Parent"@en, "상위"@ko ; rdfs:comment "상위"@ko .
steel:ChildA a owl:Class ; rdfs:subClassOf steel:Parent ;
    rdfs:label "A"@en, "A"@ko ; rdfs:comment "A"@ko .
steel:ChildB a owl:Class ; rdfs:subClassOf steel:Parent ;
    rdfs:label "B"@en, "B"@ko ; rdfs:comment "B"@ko .
steel:Target a owl:Class ; rdfs:label "Target"@en, "T"@ko ; rdfs:comment "T"@ko .
steel:hasTarget a owl:ObjectProperty ;
    rdfs:domain steel:ChildA ;
    rdfs:domain steel:ChildB ;
    rdfs:range steel:Target ;
    rdfs:label "has target"@en, "타겟"@ko ;
    rdfs:comment "타겟"@ko .
"""
    improved_ttl, stats = improve_tbox(ttl)
    g = _parse(improved_ttl)
    domains = list(g.objects(DOMAIN_NS.hasTarget, RDFS.domain))
    assert len(domains) == 1
    assert domains[0] == DOMAIN_NS.Parent
    assert stats.get("op_domain_merged_to_ancestor", 0) >= 1


def test_q3_ext_thing_domain_removed_when_steel_domain_exists():
    """owl:Thing 이 다른 steel domain 과 함께 있으면 Thing 은 삭제되어야 함."""
    ttl = f"""\
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix steel: <{STEEL_STR}> .

steel:Alpha a owl:Class ; rdfs:label "Alpha"@en, "A"@ko ; rdfs:comment "A"@ko .
steel:scopeType a owl:DatatypeProperty ;
    rdfs:domain steel:Alpha ;
    rdfs:domain owl:Thing ;
    rdfs:range xsd:string ;
    rdfs:label "scope type"@en, "스코프 유형"@ko ;
    rdfs:comment "스코프"@ko .
"""
    improved_ttl, stats = improve_tbox(ttl)
    g = _parse(improved_ttl)
    domains = set(g.objects(DOMAIN_NS.scopeType, RDFS.domain))
    # Thing 은 제거되고 steel:Alpha 만 남아야 함
    assert OWL.Thing not in domains
    assert DOMAIN_NS.Alpha in domains
    assert stats.get("dp_thing_domain_removed", 0) >= 1


def test_q3_op_cross_namespace_domain_is_unioned():
    """R23: steel + 외부(iof-core) 네임스페이스에 걸친 multi-domain 은 unionOf 로 단일화돼야 함.

    과거 버그: `_resolve_multi_domain` 이 steel: 도메인만 필터링해 2개 중 1개만
    남으면 `len < 2` 로 조기 종료 → hasEquipment(steel:MaintenanceHistory,
    iof-core:ManufacturingProcess) 케이스가 high 이슈로 잔존.
    """
    ttl = f"""\
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix steel: <{STEEL_STR}> .
@prefix iof-core: <https://spec.industrialontologies.org/ontology/core/Core/> .

steel:MaintenanceHistory a owl:Class ; rdfs:label "MH"@en, "정비"@ko ; rdfs:comment "정비"@ko .
steel:EquipmentMaster a owl:Class ; rdfs:label "EM"@en, "설비"@ko ; rdfs:comment "설비"@ko .
steel:hasEquipment a owl:ObjectProperty ;
    rdfs:domain steel:MaintenanceHistory ;
    rdfs:domain iof-core:ManufacturingProcess ;
    rdfs:range steel:EquipmentMaster ;
    rdfs:label "has equipment"@en, "설비를 가짐"@ko ;
    rdfs:comment "설비"@ko .
"""
    improved_ttl, stats = improve_tbox(ttl)
    g = _parse(improved_ttl)
    domains = list(g.objects(DOMAIN_NS.hasEquipment, RDFS.domain))
    # 공통 조상 없으므로 unionOf 1개로 단일화
    assert len(domains) == 1, f"expected single domain, got {domains}"
    dom = domains[0]
    # unionOf BNode
    union_lists = list(g.objects(dom, OWL.unionOf))
    assert len(union_lists) == 1
    assert stats.get("op_domain_union_created", 0) >= 1


def test_q3_dp_with_two_domains_no_common_ancestor_uses_union():
    ttl = f"""\
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix steel: <{STEEL_STR}> .

steel:Alpha a owl:Class ; rdfs:label "Alpha"@en, "A"@ko ; rdfs:comment "A"@ko .
steel:Beta  a owl:Class ; rdfs:label "Beta"@en, "B"@ko ; rdfs:comment "B"@ko .
steel:field a owl:DatatypeProperty ;
    rdfs:domain steel:Alpha ;
    rdfs:domain steel:Beta ;
    rdfs:range xsd:string ;
    rdfs:label "field"@en, "필드"@ko ;
    rdfs:comment "필드"@ko .
"""
    improved_ttl, stats = improve_tbox(ttl)
    g = _parse(improved_ttl)
    domains = list(g.objects(DOMAIN_NS.field, RDFS.domain))
    # 공통 조상 없어 unionOf 1개로
    assert len(domains) == 1
    dom = domains[0]
    # BNode 이어야 함
    assert not isinstance(dom, URIRef) or str(dom).startswith("_:")
    # unionOf 가 존재
    union_lists = list(g.objects(dom, OWL.unionOf))
    assert len(union_lists) == 1
    assert stats.get("dp_domain_union_created", 0) >= 1


# ── Q4: jury_fixes 기본 라벨 주입 ─────────────────

def test_q4_add_object_property_auto_labels_when_missing():
    from tools.jury_fixes import apply_jury_fixes
    ttl = f"""\
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix steel: <{STEEL_STR}> .

steel:A a owl:Class .
steel:B a owl:Class .
"""
    result = apply_jury_fixes(ttl, [{
        "action": "add_object_property",
        "property": "steel:producesSurfaceQuality",
        "domain": "steel:A", "range": "steel:B",
        # label_en / label_ko 생략 → 자동 생성
    }])
    assert len(result["applied"]) == 1
    g = _parse(result["ttl"])
    labels = list(g.objects(DOMAIN_NS.producesSurfaceQuality, RDFS.label))
    langs = {
        getattr(label, "language", None)
        for label in labels
        if isinstance(label, Literal)
    }
    assert "en" in langs
    assert "ko" in langs
    en_text = next(
        str(label)
        for label in labels
        if getattr(label, "language", None) == "en"
    )
    assert "produces" in en_text and "quality" in en_text


def test_q4_explicit_labels_are_kept():
    from tools.jury_fixes import apply_jury_fixes
    ttl = f"""@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix steel: <{STEEL_STR}> .
steel:A a owl:Class . steel:B a owl:Class .
"""
    result = apply_jury_fixes(ttl, [{
        "action": "add_object_property",
        "property": "steel:foo",
        "domain": "steel:A", "range": "steel:B",
        "label_en": "explicit", "label_ko": "명시",
    }])
    g = _parse(result["ttl"])
    labels = {
        getattr(label, "language", None): str(label)
        for label in g.objects(DOMAIN_NS.foo, RDFS.label)
        if isinstance(label, Literal)
    }
    assert labels["en"] == "explicit"
    assert labels["ko"] == "명시"


# ── Q5: invalid_uri_format 룰 + double prefix 자동 수정 ───

def test_q5_invalid_uri_format_rule_detects_double_prefix():
    from tools.validation_core import _rule_naming_property
    ttl_uri_with_colon = URIRef(STEEL_STR + "steel:hasEquipment")
    normal_uri = URIRef(STEEL_STR + "hasEquipment")
    op_set = {ttl_uri_with_colon, normal_uri}
    issues = _rule_naming_property(STEEL_STR, op_set, set())
    # double prefix → high severity invalid_uri_format
    invalid = [i for i in issues if i["rule"] == "invalid_uri_format"]
    assert len(invalid) == 1
    assert invalid[0]["severity"] == "high"
    assert "double prefix" in invalid[0]["message"].lower()


def test_q5_improve_tbox_fixes_double_prefix_uri():
    ttl = f"""\
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix steel: <{STEEL_STR}> .

steel:A a owl:Class ; rdfs:label "A"@en, "A"@ko ; rdfs:comment "A"@ko .
steel:B a owl:Class ; rdfs:label "B"@en, "B"@ko ; rdfs:comment "B"@ko .
<{STEEL_STR}steel:hasEquipment> a owl:ObjectProperty ;
    rdfs:domain steel:A ; rdfs:range steel:B ;
    rdfs:label "has equipment"@en, "설비를 가진"@ko ;
    rdfs:comment "설비"@ko .
"""
    improved_ttl, stats = improve_tbox(ttl)
    g = _parse(improved_ttl)
    bad = URIRef(STEEL_STR + "steel:hasEquipment")
    good = URIRef(STEEL_STR + "hasEquipment")
    # 잘못된 URI 는 더 이상 type OP 가 아니어야 하며, 정상 URI 로 리다이렉트됨.
    assert (bad, RDF.type, OWL.ObjectProperty) not in g
    assert (good, RDF.type, OWL.ObjectProperty) in g
    assert stats.get("antipattern_double_prefix_fixed", 0) >= 1
