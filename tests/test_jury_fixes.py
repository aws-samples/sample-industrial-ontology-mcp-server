"""apply_jury_fixes 단위 테스트."""
from __future__ import annotations

from rdflib import OWL, RDF, RDFS, Graph, Namespace, URIRef

from tools.jury_fixes import apply_jury_fixes

DOMAIN_NS = "http://example.com/steel-ontology#"

_BASE_TTL = """\
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix steel: <http://example.com/steel-ontology#> .

steel:A a owl:Class .
steel:B a owl:Class .
"""


def _parse(ttl: str) -> Graph:
    g = Graph()
    g.parse(data=ttl, format="turtle")
    return g


def test_empty_fixes_returns_unchanged_ttl():
    out = apply_jury_fixes(_BASE_TTL, [])
    assert out["applied"] == [] and out["skipped"] == [] and out["failed"] == []
    assert out["ttl"] == _BASE_TTL


def test_add_object_property_minimal():
    out = apply_jury_fixes(_BASE_TTL, [{
        "action": "add_object_property",
        "property": "steel:hasThing",
        "domain": "steel:A",
        "range": "steel:B",
    }])
    assert len(out["applied"]) == 1
    g = _parse(out["ttl"])
    ST = Namespace(DOMAIN_NS)
    assert (ST.hasThing, RDF.type, OWL.ObjectProperty) in g
    assert (ST.hasThing, RDFS.domain, ST.A) in g
    assert (ST.hasThing, RDFS.range, ST.B) in g


def test_add_object_property_with_labels():
    out = apply_jury_fixes(_BASE_TTL, [{
        "action": "add_object_property",
        "property": "steel:connects",
        "domain": "steel:A", "range": "steel:B",
        "label_en": "connects", "label_ko": "연결함",
    }])
    g = _parse(out["ttl"])
    labels = list(g.objects(Namespace(DOMAIN_NS).connects, RDFS.label))
    assert any("connects" in str(label) for label in labels)
    assert any("연결함" in str(label) for label in labels)


def test_add_object_property_missing_field_fails():
    out = apply_jury_fixes(_BASE_TTL, [{
        "action": "add_object_property",
        "property": "steel:p",
        # domain/range 누락
    }])
    assert out["applied"] == []
    assert len(out["failed"]) == 1
    assert "누락" in out["failed"][0]["reason"]


def test_add_inverse_property_creates_both_directions():
    out = apply_jury_fixes(_BASE_TTL, [{
        "action": "add_inverse_property",
        "property": "steel:hasPart",
        "inverseOf": "steel:partOf",
    }])
    assert len(out["applied"]) == 1
    g = _parse(out["ttl"])
    ST = Namespace(DOMAIN_NS)
    assert (ST.hasPart, OWL.inverseOf, ST.partOf) in g
    assert (ST.partOf, OWL.inverseOf, ST.hasPart) in g


def test_add_restriction_min_cardinality():
    out = apply_jury_fixes(_BASE_TTL, [{
        "action": "add_restriction",
        "class": "steel:A",
        "onProperty": "steel:hasThing",
        "minCardinality": 1,
    }])
    assert len(out["applied"]) == 1
    g = _parse(out["ttl"])
    ST = Namespace(DOMAIN_NS)
    # A subClassOf 가 blank restriction 하나를 갖는지
    parents = list(g.objects(ST.A, RDFS.subClassOf))
    assert len(parents) == 1
    r = parents[0]
    assert (r, RDF.type, OWL.Restriction) in g
    assert (r, OWL.onProperty, ST.hasThing) in g
    # minCardinality 리터럴
    mc = list(g.objects(r, OWL.minCardinality))
    assert mc and int(mc[0]) == 1


def test_add_restriction_some_values_from():
    out = apply_jury_fixes(_BASE_TTL, [{
        "action": "add_restriction",
        "class": "steel:A",
        "onProperty": "steel:hasThing",
        "someValuesFrom": "steel:B",
    }])
    assert len(out["applied"]) == 1
    g = _parse(out["ttl"])
    ST = Namespace(DOMAIN_NS)
    r = list(g.objects(ST.A, RDFS.subClassOf))[0]
    assert (r, OWL.someValuesFrom, ST.B) in g


def test_unknown_action_is_skipped():
    out = apply_jury_fixes(_BASE_TTL, [{
        "action": "expand_restriction_range",  # 여전히 미지원 (블랭크노드 리스트 편집 필요)
        "class": "steel:X",
    }])
    assert out["applied"] == []
    assert len(out["skipped"]) == 1
    assert "미지원" in out["skipped"][0]["reason"]


def test_mixed_batch_preserves_individual_statuses():
    """성공 2 + 실패 2 (존재하지 않는 트리플 삭제 + 필수 필드 누락) 를 올바르게 구분."""
    out = apply_jury_fixes(_BASE_TTL, [
        {"action": "add_object_property", "property": "steel:ok1",
         "domain": "steel:A", "range": "steel:B"},
        # 존재하지 않는 트리플 삭제 → failed
        {"action": "delete_triple", "subject": "steel:A",
         "predicate": "rdfs:label", "object": "nonexistent"},
        {"action": "add_restriction", "class": "steel:A",
         "onProperty": "steel:p", "minCardinality": 1},
        {"action": "add_object_property", "property": "steel:broken"},  # missing fields
    ])
    assert len(out["applied"]) == 2
    # "변경할 것이 없었다"(존재하지 않는 트리플 삭제)는 noop, 진짜 결함(필드
    # 누락)만 failed. 둘을 한 통에 담으면 적용률을 판단할 수 없다 — 실제로
    # 2026-08-10 실행의 {applied:4, failed:37} 이 그래서 해석 불가였다.
    assert len(out["failed"]) == 1, out["failed"]   # 필드 누락
    assert len(out["noop"]) == 1, out["noop"]       # 존재하지 않는 트리플


def test_bad_base_ttl_reports_all_skipped():
    out = apply_jury_fixes("not valid turtle @@", [{
        "action": "add_object_property",
        "property": "steel:p", "domain": "steel:A", "range": "steel:B",
    }])
    assert out["applied"] == []
    assert len(out["skipped"]) == 1
    assert "ttl parse error" in out["skipped"][0]["reason"]


def test_non_dict_action_recorded_as_failed():
    out = apply_jury_fixes(_BASE_TTL, ["not a dict"])
    assert len(out["failed"]) == 1
    assert "dict" in out["failed"][0]["reason"]


def test_resulting_ttl_is_parseable():
    out = apply_jury_fixes(_BASE_TTL, [{
        "action": "add_object_property",
        "property": "steel:hasThing",
        "domain": "steel:A",
        "range": "steel:B",
    }])
    # 결과 TTL 은 반드시 재파싱 가능해야 함 (유효 Turtle)
    _parse(out["ttl"])


# ── 확장 action 5종 ─────────────────────────────────

def test_add_disjoint_classes_creates_all_disjoint_node():
    out = apply_jury_fixes(_BASE_TTL, [{
        "action": "add_disjoint_classes",
        "members": ["steel:A", "steel:B"],
    }])
    assert len(out["applied"]) == 1
    g = _parse(out["ttl"])
    adc_nodes = list(g.subjects(RDF.type, OWL.AllDisjointClasses))
    assert len(adc_nodes) == 1


def test_add_disjoint_classes_requires_at_least_two_members():
    out = apply_jury_fixes(_BASE_TTL, [{
        "action": "add_disjoint_classes",
        "members": ["steel:A"],
    }])
    assert out["applied"] == []
    assert len(out["failed"]) == 1


def test_add_class_with_super_class_and_comment():
    out = apply_jury_fixes(_BASE_TTL, [{
        "action": "add_class",
        "class": "steel:DataEntity",
        "superClass": "owl:Thing",
        "comment": "모든 데이터 엔티티의 최상위 추상 클래스",
    }])
    assert len(out["applied"]) == 1
    g = _parse(out["ttl"])
    ST = Namespace(DOMAIN_NS)
    assert (ST.DataEntity, RDF.type, OWL.Class) in g
    assert (ST.DataEntity, RDFS.subClassOf, OWL.Thing) in g
    comments = list(g.objects(ST.DataEntity, RDFS.comment))
    assert any("데이터 엔티티" in str(c) for c in comments)


def test_add_subclass_adds_parent_without_removing():
    # 기존 subClassOf 가 있어도 추가만 해야 함 (modify_subclass 가 교체 담당)
    base = _BASE_TTL + "\nsteel:A rdfs:subClassOf steel:X .\n"
    out = apply_jury_fixes(base, [{
        "action": "add_subclass",
        "class": "steel:A",
        "superClass": "iof-core:InformationContentEntity",
    }])
    assert len(out["applied"]) == 1
    g = _parse(out["ttl"])
    ST = Namespace(DOMAIN_NS)
    parents = set(g.objects(ST.A, RDFS.subClassOf))
    assert ST.X in parents
    assert any("InformationContentEntity" in str(p) for p in parents)


def test_modify_subclass_replaces_named_parent_only():
    # 기존 부모: steel:X (명명), _:bn (Restriction 블랭크)
    base = (_BASE_TTL +
            "\nsteel:A rdfs:subClassOf steel:X .\n"
            "steel:A rdfs:subClassOf [ a owl:Restriction ; "
            "owl:onProperty steel:hasZ ; owl:minCardinality 1 ] .\n")
    out = apply_jury_fixes(base, [{
        "action": "modify_subclass",
        "class": "steel:A",
        "newSuperClass": "steel:Y",
    }])
    assert len(out["applied"]) == 1
    g = _parse(out["ttl"])
    ST = Namespace(DOMAIN_NS)
    parents = list(g.objects(ST.A, RDFS.subClassOf))
    # 명명 부모는 steel:Y 하나만
    named = [p for p in parents if isinstance(p, URIRef)]
    assert named == [ST.Y]
    # 블랭크 Restriction 은 유지
    from rdflib import BNode as _BN
    assert any(isinstance(p, _BN) for p in parents)


def test_delete_triple_removes_uri_object_triple():
    """URI object 케이스: steel:A rdfs:subClassOf steel:X 를 제거."""
    base = _BASE_TTL + "\nsteel:A rdfs:subClassOf steel:X .\n"
    out = apply_jury_fixes(base, [{
        "action": "delete_triple",
        "subject": "steel:A",
        "predicate": "rdfs:subClassOf",
        "object": "steel:X",
    }])
    assert len(out["applied"]) == 1, out
    g = _parse(out["ttl"])
    ST = Namespace(DOMAIN_NS)
    parents = set(g.objects(ST.A, RDFS.subClassOf))
    assert ST.X not in parents


def test_delete_triple_absorbs_language_tag_notation():
    """평문으로 보낸 값이 ``@ko`` 리터럴과 매칭돼 삭제된다 (계약 변경).

    예전에는 표기가 다르면 매칭 실패였다. LLM 은 언어 태그·데이터타입을 빠뜨리고
    값만 보내므로, 정확 일치를 먼저 시도하고 없을 때만 같은 문자열 값으로 넓힌다.
    후보가 여럿이면 지우지 않는다 (``test_delete_triple_ambiguous_literal_is_refused``).
    """
    base = _BASE_TTL + "\nsteel:A rdfs:label \"unwanted\"@ko .\n"
    out = apply_jury_fixes(base, [{
        "action": "delete_triple",
        "subject": "steel:A",
        "predicate": "rdfs:label",
        "object": "unwanted",  # 그래프에는 @ko 태그가 붙어 있다
    }])
    assert not out["failed"], out["failed"]
    assert len(out["applied"]) == 1
    assert 'rdfs:label "unwanted"' not in out["ttl"]


def test_delete_triple_ambiguous_literal_is_refused():
    """같은 값이 여러 표기로 있으면 추측하지 않는다 (조용한 오삭제 방지)."""
    base = _BASE_TTL + '\nsteel:A rdfs:label "dup"@ko, "dup"@en .\n'
    out = apply_jury_fixes(base, [{
        "action": "delete_triple",
        "subject": "steel:A",
        "predicate": "rdfs:label",
        "object": "dup",
    }])
    assert len(out["applied"]) == 0
    assert out["ttl"].count('"dup"') == 2, "모호한데 지웠다"


def test_delete_triple_skips_bnode_expression():
    """object가 '[...]' 블랭크노드 표현이면 스킵."""
    out = apply_jury_fixes(_BASE_TTL, [{
        "action": "delete_triple",
        "subject": "steel:A",
        "predicate": "rdfs:subClassOf",
        "object": "[owl:Restriction; owl:onProperty steel:x]",
    }])
    assert out["applied"] == []
    assert len(out["failed"]) == 1
    assert "블랭크노드" in out["failed"][0]["reason"]


def test_resolve_iof_core_prefix_if_bound():
    """IOF prefix가 바인딩된 TTL에서 iof-core: URI 가 올바르게 resolve 되는지."""
    base = """\
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix steel: <http://example.com/steel-ontology#> .
@prefix iof-core: <https://spec.industrialontologies.org/ontology/core/Core/> .

steel:X a owl:Class .
"""
    out = apply_jury_fixes(base, [{
        "action": "add_subclass",
        "class": "steel:X",
        "superClass": "iof-core:InformationContentEntity",
    }])
    assert len(out["applied"]) == 1
    g = _parse(out["ttl"])
    ST = Namespace(DOMAIN_NS)
    iof = Namespace("https://spec.industrialontologies.org/ontology/core/Core/")
    assert (ST.X, RDFS.subClassOf, iof.InformationContentEntity) in g


def test_add_triple_generic():
    out = apply_jury_fixes(_BASE_TTL, [{
        "action": "add_triple",
        "subject": "steel:A",
        "predicate": "rdfs:subClassOf",
        "object": "steel:B",
    }])
    assert len(out["applied"]) == 1
    g = _parse(out["ttl"])
    ST = Namespace(DOMAIN_NS)
    assert (ST.A, RDFS.subClassOf, ST.B) in g


def test_add_triple_duplicate_is_skipped():
    out = apply_jury_fixes(_BASE_TTL, [{
        "action": "add_triple",
        "subject": "steel:A",
        "predicate": "rdf:type",
        "object": "owl:Class",
    }])
    # 이미 있는 트리플 → 결함이 아니라 no-op 으로 보고한다.
    assert len(out["applied"]) == 0
    assert len(out["failed"]) == 0, out["failed"]
    assert len(out["noop"]) == 1


def test_add_datatype_property_with_domain_range():
    out = apply_jury_fixes(_BASE_TTL, [{
        "action": "add_datatype_property",
        "property": "steel:temperatureC",
        "domain": "steel:A",
        "range": "xsd:decimal",
        "label_en": "temperature (C)",
        "label_ko": "온도",
    }])
    assert len(out["applied"]) == 1
    g = _parse(out["ttl"])
    ST = Namespace(DOMAIN_NS)
    assert (ST.temperatureC, RDF.type, OWL.DatatypeProperty) in g
    assert (ST.temperatureC, RDFS.domain, ST.A) in g


def test_add_subclass_batch_adds_all_children():
    base = _BASE_TTL + "\nsteel:C a owl:Class .\nsteel:D a owl:Class .\nsteel:Parent a owl:Class .\n"
    out = apply_jury_fixes(base, [{
        "action": "add_subclass_batch",
        "parent": "steel:Parent",
        "children": ["steel:A", "steel:C", "steel:D"],
    }])
    assert len(out["applied"]) == 1
    g = _parse(out["ttl"])
    ST = Namespace(DOMAIN_NS)
    assert (ST.A, RDFS.subClassOf, ST.Parent) in g
    assert (ST.C, RDFS.subClassOf, ST.Parent) in g
    assert (ST.D, RDFS.subClassOf, ST.Parent) in g


def test_add_subclass_batch_missing_children_fails():
    out = apply_jury_fixes(_BASE_TTL, [{
        "action": "add_subclass_batch",
        "parent": "steel:A",
        "children": [],
    }])
    assert len(out["applied"]) == 0
    assert len(out["failed"]) == 1


# ── R28 신규 actions ─────────────────────────────────────────────────────


def test_fix_namespace_bulk_rewrites_double_prefix():
    """`<...#steel:X>` 같은 이중 접두사 URI 를 올바른 steel:X 로 rewrite."""
    bad_ttl = """\
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix steel: <http://example.com/steel-ontology#> .

<http://example.com/steel-ontology#steel:EquipmentMaster> a owl:Class .
<http://example.com/steel-ontology#steel:hasAlarm> a owl:ObjectProperty ;
    rdfs:domain <http://example.com/steel-ontology#steel:EquipmentMaster> ;
    rdfs:range steel:AlarmEvents .
steel:AlarmEvents a owl:Class .
"""
    out = apply_jury_fixes(bad_ttl, [{"action": "fix_namespace_bulk", "target": "all"}])
    assert len(out["applied"]) == 1, out
    g = _parse(out["ttl"])
    ST = Namespace(DOMAIN_NS)
    # 올바른 URI 로 이전됐는지 검증
    assert (ST.EquipmentMaster, RDF.type, OWL.Class) in g
    assert (ST.hasAlarm, RDF.type, OWL.ObjectProperty) in g
    assert (ST.hasAlarm, RDFS.domain, ST.EquipmentMaster) in g
    # 이중 접두사 URI 는 전부 사라졌어야 함
    bad_uri = URIRef("http://example.com/steel-ontology#steel:EquipmentMaster")
    assert (bad_uri, RDF.type, OWL.Class) not in g


def test_fix_namespace_bulk_returns_false_when_no_violation():
    out = apply_jury_fixes(_BASE_TTL, [{"action": "fix_namespace_bulk"}])
    # 고칠 유령이 없으면 no-op (reason: "이중 접두사 URI 없음"). 깨끗한 입력에
    # 대해 failed 로 세면 정상 T-Box 가 실패로 보고된다.
    assert len(out["applied"]) == 0
    assert len(out["failed"]) == 0, out["failed"]
    assert len(out["noop"]) == 1


def test_split_property_with_splits_field():
    """splits 리스트가 주어지면 원본 삭제 + 분리 프로퍼티 생성."""
    ttl_with_shared = _BASE_TTL + """
steel:sampleId a owl:FunctionalProperty, owl:DatatypeProperty ;
    rdfs:domain steel:A ;
    rdfs:domain steel:B .
"""
    out = apply_jury_fixes(ttl_with_shared, [{
        "action": "split_property",
        "target": "steel:sampleId",
        "splits": [
            {"name": "steel:aSampleId", "domain": "steel:A", "type": "FunctionalProperty"},
            {"name": "steel:bSampleId", "domain": "steel:B", "type": "FunctionalProperty"},
        ],
    }])
    assert len(out["applied"]) == 1, out
    g = _parse(out["ttl"])
    ST = Namespace(DOMAIN_NS)
    # 원본 삭제
    assert (ST.sampleId, RDF.type, OWL.FunctionalProperty) not in g
    # 분리 프로퍼티 생성
    assert (ST.aSampleId, RDF.type, OWL.FunctionalProperty) in g
    assert (ST.aSampleId, RDFS.domain, ST.A) in g
    assert (ST.bSampleId, RDFS.domain, ST.B) in g


def test_split_property_without_splits_deletes_only():
    """splits 미제공 — 원본만 삭제, Architect 재선언 안내."""
    ttl_with_target = _BASE_TTL + """
steel:badProp a owl:FunctionalProperty ; rdfs:domain steel:A .
"""
    out = apply_jury_fixes(ttl_with_target, [{
        "action": "split_property", "target": "steel:badProp",
    }])
    assert len(out["applied"]) == 1, out
    assert "splits 필드 누락" in out["applied"][0]["status"]


def test_replace_triple_updates_single_triple():
    ttl = _BASE_TTL + """
steel:p a owl:ObjectProperty ; rdfs:range steel:A .
"""
    out = apply_jury_fixes(ttl, [{
        "action": "replace_triple",
        "target": "steel:p",
        "predicate": "rdfs:range",
        "old_object": "steel:A",
        "new_object": "steel:B",
    }])
    assert len(out["applied"]) == 1, out
    g = _parse(out["ttl"])
    ST = Namespace(DOMAIN_NS)
    assert (ST.p, RDFS.range, ST.B) in g
    assert (ST.p, RDFS.range, ST.A) not in g


def test_add_functional_property_batch():
    ttl = _BASE_TTL + """
steel:p1 a owl:DatatypeProperty .
steel:p2 a owl:DatatypeProperty .
"""
    out = apply_jury_fixes(ttl, [{
        "action": "add_functional_property",
        "target": "steel:p1, steel:p2",
    }])
    assert len(out["applied"]) == 1, out
    g = _parse(out["ttl"])
    ST = Namespace(DOMAIN_NS)
    assert (ST.p1, RDF.type, OWL.FunctionalProperty) in g
    assert (ST.p2, RDF.type, OWL.FunctionalProperty) in g


def test_add_class_hierarchy_with_hierarchy_field():
    out = apply_jury_fixes(_BASE_TTL, [{
        "action": "add_class_hierarchy",
        "hierarchy": [
            {"parent": "steel:AbstractEnv",
             "children": ["steel:A", "steel:B"]},
        ],
    }])
    assert len(out["applied"]) == 1, out
    g = _parse(out["ttl"])
    ST = Namespace(DOMAIN_NS)
    assert (ST.AbstractEnv, RDF.type, OWL.Class) in g
    assert (ST.A, RDFS.subClassOf, ST.AbstractEnv) in g
    assert (ST.B, RDFS.subClassOf, ST.AbstractEnv) in g


def test_add_iof_mappings_with_mappings_field():
    out = apply_jury_fixes(_BASE_TTL, [{
        "action": "add_iof_mappings",
        "mappings": [
            {"class": "steel:A",
             "iof_class": "https://spec.industrialontologies.org/ontology/core/Core/Process"},
        ],
    }])
    assert len(out["applied"]) == 1, out
    g = _parse(out["ttl"])
    ST = Namespace(DOMAIN_NS)
    iof = URIRef("https://spec.industrialontologies.org/ontology/core/Core/Process")
    assert (ST.A, RDFS.subClassOf, iof) in g


def test_remove_duplicate_removes_all_targets():
    ttl = _BASE_TTL + """
steel:dup1 a owl:DatatypeProperty ; rdfs:label "dup"@ko .
steel:dup2 a owl:DatatypeProperty ; rdfs:label "dup"@ko .
"""
    out = apply_jury_fixes(ttl, [{
        "action": "remove_duplicate",
        "target": "steel:dup1, steel:dup2",
    }])
    assert len(out["applied"]) == 1, out
    g = _parse(out["ttl"])
    ST = Namespace(DOMAIN_NS)
    assert (ST.dup1, RDF.type, OWL.DatatypeProperty) not in g
    assert (ST.dup2, RDF.type, OWL.DatatypeProperty) not in g


# ── R28 D: LLM 이 발명하는 action 이름 alias 라우팅 ─────────────────────


def test_alias_replace_iri_batch_routes_to_fix_namespace_bulk():
    """LLM 이 `replace_iri_batch` 라고 부르면 fix_namespace_bulk 로 라우팅."""
    bad_ttl = _BASE_TTL + """
<http://example.com/steel-ontology#steel:BadClass> a owl:Class .
"""
    out = apply_jury_fixes(bad_ttl, [{"action": "replace_iri_batch"}])
    assert len(out["applied"]) == 1, out
    g = _parse(out["ttl"])
    ST = Namespace(DOMAIN_NS)
    assert (ST.BadClass, RDF.type, OWL.Class) in g


def test_alias_add_intermediate_classes_routes_to_hierarchy():
    out = apply_jury_fixes(_BASE_TTL, [{
        "action": "add_intermediate_classes",
        "hierarchy": [
            {"parent": "steel:XGroup", "children": ["steel:A", "steel:B"]},
        ],
    }])
    assert len(out["applied"]) == 1, out
    g = _parse(out["ttl"])
    ST = Namespace(DOMAIN_NS)
    assert (ST.A, RDFS.subClassOf, ST.XGroup) in g


def test_alias_remove_resource_routes_to_delete_class():
    """remove_resource → delete_class: 그래프에서 클래스 완전 제거."""
    ttl = _BASE_TTL + """
steel:Victim a owl:Class ; rdfs:label "victim"@ko .
"""
    out = apply_jury_fixes(ttl, [{
        "action": "remove_resource",
        "class": "steel:Victim",
    }])
    assert len(out["applied"]) == 1, out
    g = _parse(out["ttl"])
    ST = Namespace(DOMAIN_NS)
    assert (ST.Victim, RDF.type, OWL.Class) not in g


def test_verify_ttl_syntax_is_ack_noop():
    """verify_ttl_syntax 는 파이프라인 하류 검증 전용 — ack 로 applied 마크."""
    out = apply_jury_fixes(_BASE_TTL, [{"action": "verify_ttl_syntax"}])
    assert len(out["applied"]) == 1, out
    assert "ack" in out["applied"][0]["status"].lower()


def test_unknown_action_still_skipped():
    """alias 가 없는 완전히 새 이름은 여전히 skipped (안전 장치)."""
    out = apply_jury_fixes(_BASE_TTL, [{"action": "completely_new_invention"}])
    assert len(out["applied"]) == 0
    assert len(out["skipped"]) == 1


def test_jury_prompt_includes_action_catalog():
    """jury_static_prefix 에 지원 action 목록이 들어가 LLM 이 새 이름을 발명하지 않게 유도."""
    from tools.multi_agent_prompts import jury_static_prefix

    p = jury_static_prefix()
    # 핵심 action 들이 목록에 노출되어야
    assert "fix_namespace_bulk" in p
    assert "add_class_hierarchy" in p
    assert "split_property" in p
    # 규칙: 새 이름 발명 금지 시그니처
    assert "새 이름 발명 금지" in p
    # 치환 권장 매핑 포함
    assert "replace_iri_batch" in p and "add_intermediate_classes" in p


# ── 마지막 domain/range 삭제 거부 ───────────────────


_DR_TTL = """\
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix steel: <http://example.com/steel-ontology#> .

steel:A a owl:Class .
steel:B a owl:Class .
steel:C a owl:Class .

steel:onlyOne a owl:ObjectProperty ;
    rdfs:domain steel:A ;
    rdfs:range steel:B .

steel:hasTwo a owl:ObjectProperty ;
    rdfs:domain steel:A, steel:C ;
    rdfs:range steel:B .
"""


def test_delete_last_domain_is_refused():
    """마지막 rdfs:domain 삭제는 거부 — 지우면 제약 없는 껍데기가 된다.

    실측 (2026-08-14 S2): Jury 가 "다중 domain-range 선언" 으로 오판해
    remove_triple 을 냈고 continuousCastingHasProduct / rollingHasProduct /
    steelmakingHasProduct 3개가 domain·range 를 완전히 잃었다. 그렇게 되면
    A-Box 생성기(load_object_properties)가 그 OP 를 못 읽어 관계가 비고,
    conformance 검사와 중복 게이트도 그 축을 건너뛴다.
    """
    out = apply_jury_fixes(_DR_TTL, [{
        "action": "remove_triple",
        "subject": "steel:onlyOne",
        "predicate": "rdfs:domain",
        "object": "steel:A",
    }])
    g = _parse(out["ttl"])
    assert (URIRef(DOMAIN_NS + "onlyOne"), RDFS.domain,
            URIRef(DOMAIN_NS + "A")) in g, "마지막 domain 이 삭제됐다"
    assert not out["applied"], f"거부돼야 하는데 적용됐다: {out['applied']}"
    reasons = " ".join(str(x) for x in (out["failed"] + out["skipped"]))
    assert "domain" in reasons and "껍데기" in reasons, (
        f"거부 이유가 설명되지 않았다: {reasons}"
    )


def test_delete_last_range_is_refused():
    out = apply_jury_fixes(_DR_TTL, [{
        "action": "remove_triple",
        "subject": "steel:onlyOne",
        "predicate": "rdfs:range",
        "object": "steel:B",
    }])
    g = _parse(out["ttl"])
    assert (URIRef(DOMAIN_NS + "onlyOne"), RDFS.range,
            URIRef(DOMAIN_NS + "B")) in g, "마지막 range 가 삭제됐다"
    assert not out["applied"]


def test_delete_redundant_domain_still_works():
    """domain 이 2개면 하나는 지울 수 있다 (NEGATIVE 방향).

    가드가 정당한 삭제까지 막으면 Jury 의 중복 정리 기능이 죽는다.
    """
    out = apply_jury_fixes(_DR_TTL, [{
        "action": "remove_triple",
        "subject": "steel:hasTwo",
        "predicate": "rdfs:domain",
        "object": "steel:C",
    }])
    g = _parse(out["ttl"])
    prop = URIRef(DOMAIN_NS + "hasTwo")
    assert (prop, RDFS.domain, URIRef(DOMAIN_NS + "C")) not in g, (
        "잉여 domain 삭제가 막혔다 — 가드가 과잉 차단한다"
    )
    assert (prop, RDFS.domain, URIRef(DOMAIN_NS + "A")) in g
    assert out["applied"], f"정당한 삭제가 적용되지 않았다: {out}"
