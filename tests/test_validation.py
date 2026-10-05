"""Tests for tools/validation.py — uses rdflib locally, no AWS mocking needed."""

import json
from unittest.mock import patch

from tools.validation import analyze_tbox, check_quality_rules, validate_shacl, validate_ttl_syntax
from tools.validation_core import _check_cardinality_with_completeness


class TestValidateTtlSyntax:
    def test_valid_ttl(self, sample_tbox_ttl):
        result = json.loads(validate_ttl_syntax(sample_tbox_ttl))
        assert result["success"] is True
        assert result["triples"] > 0
        assert "subjects" in result
        assert "predicates" in result
        assert "objects" in result

    def test_invalid_ttl(self, invalid_ttl):
        result = json.loads(validate_ttl_syntax(invalid_ttl))
        assert result.get("success") is not True or "error" in result

    def test_empty_string(self):
        result = json.loads(validate_ttl_syntax(""))
        # 빈 문자열이면 기본 T-Box 파일 로드 (존재 시 트리플 > 0)
        assert result["success"] is True

    def test_abox_ttl(self, sample_abox_ttl):
        result = json.loads(validate_ttl_syntax(sample_abox_ttl))
        assert result["success"] is True
        assert result["triples"] > 0


class TestCheckQualityRules:
    def test_clean_tbox(self, sample_tbox_ttl):
        result = json.loads(check_quality_rules(sample_tbox_ttl))
        assert result["issues_count"] == 0
        assert result["critical"] == 0
        assert result["statistics"]["classes"] == 2
        assert result["statistics"]["object_properties"] == 1
        assert result["statistics"]["data_properties"] == 2

    def test_circular_property_detected(self, sample_tbox_circular):
        result = json.loads(check_quality_rules(sample_tbox_circular))
        assert result["critical"] >= 1
        circular_issues = [i for i in result["issues"] if i["rule"] == "circular_property"]
        assert len(circular_issues) >= 1
        assert circular_issues[0]["severity"] == "critical"

    def test_missing_label_detected(self, sample_tbox_missing_labels):
        result = json.loads(check_quality_rules(sample_tbox_missing_labels))
        missing_label_issues = [i for i in result["issues"] if i["rule"] == "missing_label"]
        # Should detect missing @en label
        en_missing = [i for i in missing_label_issues if "@en" in i["message"]]
        assert len(en_missing) >= 1

    def test_missing_domain_range_detected(self, sample_tbox_missing_domain_range):
        result = json.loads(check_quality_rules(sample_tbox_missing_domain_range))
        domain_issues = [i for i in result["issues"] if i["rule"] == "missing_domain"]
        range_issues = [i for i in result["issues"] if i["rule"] == "missing_range"]
        assert len(domain_issues) >= 1
        assert len(range_issues) >= 1

    def test_invalid_ttl_returns_error(self, invalid_ttl):
        result = json.loads(check_quality_rules(invalid_ttl))
        assert "error" in result

    def test_statistics_present(self, sample_tbox_ttl):
        result = json.loads(check_quality_rules(sample_tbox_ttl))
        stats = result["statistics"]
        assert "classes" in stats
        assert "object_properties" in stats
        assert "data_properties" in stats
        assert "total_triples" in stats

    def test_subclass_cycle_detected(self):
        """6. subClassOf 순환 A⊂B⊂A → critical."""
        from domain.namespaces import DOMAIN_NS, NS_PREFIX
        ttl = f"""\
@prefix {NS_PREFIX}: <{DOMAIN_NS}> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .

{NS_PREFIX}:ClassA a owl:Class ; rdfs:subClassOf {NS_PREFIX}:ClassB ;
    rdfs:label "A"@en, "A"@ko ; rdfs:comment "A"@ko .
{NS_PREFIX}:ClassB a owl:Class ; rdfs:subClassOf {NS_PREFIX}:ClassA ;
    rdfs:label "B"@en, "B"@ko ; rdfs:comment "B"@ko .
"""
        result = json.loads(check_quality_rules(ttl))
        cycle_issues = [i for i in result["issues"] if i["rule"] == "subclass_cycle"]
        assert len(cycle_issues) >= 1
        assert cycle_issues[0]["severity"] == "critical"

    def test_self_subclass_detected(self):
        """8. A subClassOf A → critical."""
        from domain.namespaces import DOMAIN_NS, NS_PREFIX
        ttl = f"""\
@prefix {NS_PREFIX}: <{DOMAIN_NS}> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .

{NS_PREFIX}:SelfRef a owl:Class ; rdfs:subClassOf {NS_PREFIX}:SelfRef ;
    rdfs:label "Self"@en, "자기"@ko ; rdfs:comment "자기참조"@ko .
"""
        result = json.loads(check_quality_rules(ttl))
        self_issues = [i for i in result["issues"] if i["rule"] == "self_subclass"]
        assert len(self_issues) >= 1
        assert self_issues[0]["severity"] == "critical"

    def test_equivalent_class_cycle_detected(self):
        """19. A≡B≡C≡A → critical."""
        from domain.namespaces import DOMAIN_NS, NS_PREFIX
        ttl = f"""\
@prefix {NS_PREFIX}: <{DOMAIN_NS}> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .

{NS_PREFIX}:EqA a owl:Class ; owl:equivalentClass {NS_PREFIX}:EqB ;
    rdfs:label "A"@en, "A"@ko ; rdfs:comment "A"@ko .
{NS_PREFIX}:EqB a owl:Class ; owl:equivalentClass {NS_PREFIX}:EqC ;
    rdfs:label "B"@en, "B"@ko ; rdfs:comment "B"@ko .
{NS_PREFIX}:EqC a owl:Class ; owl:equivalentClass {NS_PREFIX}:EqA ;
    rdfs:label "C"@en, "C"@ko ; rdfs:comment "C"@ko .
"""
        result = json.loads(check_quality_rules(ttl))
        eq_issues = [i for i in result["issues"] if i["rule"] == "equivalent_class_cycle"]
        assert len(eq_issues) >= 1
        assert eq_issues[0]["severity"] == "critical"

    def test_self_inverse_detected(self):
        """9. P inverseOf P → high."""
        from domain.namespaces import DOMAIN_NS, NS_PREFIX
        ttl = f"""\
@prefix {NS_PREFIX}: <{DOMAIN_NS}> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .

{NS_PREFIX}:Cls a owl:Class ;
    rdfs:label "C"@en, "C"@ko ; rdfs:comment "C"@ko .
{NS_PREFIX}:selfInv a owl:ObjectProperty ;
    owl:inverseOf {NS_PREFIX}:selfInv ;
    rdfs:domain {NS_PREFIX}:Cls ; rdfs:range {NS_PREFIX}:Cls ;
    rdfs:label "self"@en, "자기"@ko ; rdfs:comment "자기역"@ko .
"""
        result = json.loads(check_quality_rules(ttl))
        si_issues = [i for i in result["issues"] if i["rule"] == "self_inverse"]
        assert len(si_issues) >= 1
        assert si_issues[0]["severity"] == "high"

    def test_multiple_domains_detected(self):
        """7. OP에 domain 2개 → high."""
        from domain.namespaces import DOMAIN_NS, NS_PREFIX
        ttl = f"""\
@prefix {NS_PREFIX}: <{DOMAIN_NS}> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .

{NS_PREFIX}:A a owl:Class ; rdfs:label "A"@en, "A"@ko ; rdfs:comment "A"@ko .
{NS_PREFIX}:B a owl:Class ; rdfs:label "B"@en, "B"@ko ; rdfs:comment "B"@ko .
{NS_PREFIX}:multiDom a owl:ObjectProperty ;
    rdfs:domain {NS_PREFIX}:A, {NS_PREFIX}:B ;
    rdfs:range {NS_PREFIX}:A ;
    rdfs:label "multi"@en, "다중"@ko ; rdfs:comment "다중도메인"@ko .
"""
        result = json.loads(check_quality_rules(ttl))
        md_issues = [i for i in result["issues"] if i["rule"] == "multiple_domains"]
        assert len(md_issues) >= 1
        assert md_issues[0]["severity"] == "high"

    def test_isolated_class_detected(self):
        """5. 고립 클래스 → warning."""
        from domain.namespaces import DOMAIN_NS, NS_PREFIX
        ttl = f"""\
@prefix {NS_PREFIX}: <{DOMAIN_NS}> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .

{NS_PREFIX}:Lonely a owl:Class ;
    rdfs:label "Lonely"@en, "고립"@ko ; rdfs:comment "연결 없음"@ko .
"""
        result = json.loads(check_quality_rules(ttl))
        iso_issues = [i for i in result["issues"] if i["rule"] == "isolated_class"]
        assert len(iso_issues) >= 1
        assert iso_issues[0]["severity"] == "warning"


    def test_disjoint_presence_detects_gap_vs_rules_file(self, tmp_path, monkeypatch):
        """R11-M4: rules/domain/disjoint_groups.json 에 N 그룹 선언됐는데 T-Box 에
        AllDisjointClasses 선언이 절반 이하면 WARN. R10 meta-audit 에서 delete_disjoint
        mutator 가 0/2 잡힌 blind-spot 을 메우기 위한 새 규칙."""
        from domain.namespaces import DOMAIN_NS, NS_PREFIX

        # rules/domain/disjoint_groups.json 에 3 그룹 선언했다고 가정
        rules_file = tmp_path / "disjoint_groups.json"
        rules_file.write_text(json.dumps({
            "groups": [
                {"label": "A", "classes": ["ClassA1", "ClassA2"]},
                {"label": "B", "classes": ["ClassB1", "ClassB2"]},
                {"label": "C", "classes": ["ClassC1", "ClassC2"]},
            ]
        }))
        monkeypatch.setattr("tools.validation_core.DISJOINT_GROUPS_PATH", str(rules_file))

        # T-Box 에는 AllDisjointClasses 0 개 (delete_disjoint mutation 과 유사 상황).
        # rules 의 3 그룹 모두 T-Box 에 존재 → expected=3, found=0 → WARN 트리거.
        ttl = f"""\
@prefix {NS_PREFIX}: <{DOMAIN_NS}> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .

{NS_PREFIX}:ClassA1 a owl:Class ; rdfs:label "A1"@en, "A1"@ko ; rdfs:comment "c"@ko .
{NS_PREFIX}:ClassA2 a owl:Class ; rdfs:label "A2"@en, "A2"@ko ; rdfs:comment "c"@ko .
{NS_PREFIX}:ClassB1 a owl:Class ; rdfs:label "B1"@en, "B1"@ko ; rdfs:comment "c"@ko .
{NS_PREFIX}:ClassB2 a owl:Class ; rdfs:label "B2"@en, "B2"@ko ; rdfs:comment "c"@ko .
{NS_PREFIX}:ClassC1 a owl:Class ; rdfs:label "C1"@en, "C1"@ko ; rdfs:comment "c"@ko .
{NS_PREFIX}:ClassC2 a owl:Class ; rdfs:label "C2"@en, "C2"@ko ; rdfs:comment "c"@ko .
{NS_PREFIX}:link a owl:ObjectProperty ; rdfs:domain {NS_PREFIX}:ClassA1 ;
    rdfs:range {NS_PREFIX}:ClassA2 ; rdfs:label "link"@en, "link"@ko ; rdfs:comment "c"@ko .
"""
        result = json.loads(check_quality_rules(ttl))
        presence_issues = [i for i in result["issues"] if i["rule"] == "disjoint_presence"]
        assert len(presence_issues) >= 1, f"expected disjoint_presence issue, got: {result['issues']}"
        # 완전 누락 (0 개) → critical. mutation_runner 가 이걸 FAIL 로 매핑해 catch.
        assert presence_issues[0]["severity"] == "critical"
        assert "3" in presence_issues[0]["message"]  # expected 3 groups
        assert "0" in presence_issues[0]["message"]  # found 0

    def test_disjoint_presence_passes_when_all_groups_declared(self, tmp_path, monkeypatch):
        """rules 에 선언된 모든 그룹이 T-Box AllDisjointClasses 로 있으면 PASS.
        expected=3, found=3 → 누락 없음."""
        from domain.namespaces import DOMAIN_NS, NS_PREFIX

        rules_file = tmp_path / "disjoint_groups.json"
        rules_file.write_text(json.dumps({
            "groups": [
                {"label": "A", "classes": ["ClassA1", "ClassA2"]},
                {"label": "B", "classes": ["ClassB1", "ClassB2"]},
                {"label": "C", "classes": ["ClassC1", "ClassC2"]},
            ]
        }))
        monkeypatch.setattr("tools.validation_core.DISJOINT_GROUPS_PATH", str(rules_file))

        ttl = f"""\
@prefix {NS_PREFIX}: <{DOMAIN_NS}> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .

{NS_PREFIX}:ClassA1 a owl:Class ; rdfs:label "A1"@en, "A1"@ko ; rdfs:comment "c"@ko .
{NS_PREFIX}:ClassA2 a owl:Class ; rdfs:label "A2"@en, "A2"@ko ; rdfs:comment "c"@ko .
{NS_PREFIX}:ClassB1 a owl:Class ; rdfs:label "B1"@en, "B1"@ko ; rdfs:comment "c"@ko .
{NS_PREFIX}:ClassB2 a owl:Class ; rdfs:label "B2"@en, "B2"@ko ; rdfs:comment "c"@ko .
{NS_PREFIX}:ClassC1 a owl:Class ; rdfs:label "C1"@en, "C1"@ko ; rdfs:comment "c"@ko .
{NS_PREFIX}:ClassC2 a owl:Class ; rdfs:label "C2"@en, "C2"@ko ; rdfs:comment "c"@ko .
{NS_PREFIX}:link a owl:ObjectProperty ; rdfs:domain {NS_PREFIX}:ClassA1 ;
    rdfs:range {NS_PREFIX}:ClassA2 ; rdfs:label "link"@en, "link"@ko ; rdfs:comment "c"@ko .

[] a owl:AllDisjointClasses ; owl:members ( {NS_PREFIX}:ClassA1 {NS_PREFIX}:ClassA2 ) .
[] a owl:AllDisjointClasses ; owl:members ( {NS_PREFIX}:ClassB1 {NS_PREFIX}:ClassB2 ) .
[] a owl:AllDisjointClasses ; owl:members ( {NS_PREFIX}:ClassC1 {NS_PREFIX}:ClassC2 ) .
"""
        result = json.loads(check_quality_rules(ttl))
        presence_issues = [i for i in result["issues"] if i["rule"] == "disjoint_presence"]
        assert len(presence_issues) == 0, f"unexpected issue: {presence_issues}"


class TestValidateShacl:
    def test_without_shapes(self, sample_tbox_ttl):
        result = json.loads(validate_shacl(sample_tbox_ttl))
        assert "conforms" in result

    def test_with_shapes_conforming(self, sample_abox_ttl, sample_shacl_shapes):
        result = json.loads(validate_shacl(sample_abox_ttl, shapes_ttl=sample_shacl_shapes))
        assert "conforms" in result
        # Our sample A-Box has equipmentID so should conform
        assert result["conforms"] is True

    def test_with_shapes_violation(self, sample_shacl_shapes):
        # A-Box instance with wrong datatype for equipmentID (xsd:integer vs xsd:string)
        from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS, NS_INST_PREFIX, NS_PREFIX
        bad_abox = f"""\
@prefix {NS_PREFIX}: <{DOMAIN_NS}> .
@prefix {NS_INST_PREFIX}: <{DOMAIN_INST_NS}> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

{NS_INST_PREFIX}:EquipmentMaster_EQ002 a {NS_PREFIX}:EquipmentMaster ;
    {NS_PREFIX}:equipmentID 12345 .
"""
        result = json.loads(validate_shacl(bad_abox, shapes_ttl=sample_shacl_shapes))
        assert result["conforms"] is False
        assert result["violations_count"] >= 1

    def test_invalid_ttl(self, invalid_ttl):
        result = json.loads(validate_shacl(invalid_ttl))
        assert "error" in result

    def test_empty_data(self):
        result = json.loads(validate_shacl(""))
        assert "conforms" in result
        assert result["conforms"] is True


class TestAnalyzeTbox:
    @patch("tools.local_artifacts.read_tbox")
    def test_happy_path(self, mock_read_tbox, sample_tbox_ttl):
        mock_read_tbox.return_value = sample_tbox_ttl
        result = json.loads(analyze_tbox())
        assert result["classes_count"] == 2
        assert result["object_properties_count"] == 1
        assert result["data_properties_count"] == 2
        # Verify class details
        class_uris = [c["uri"] for c in result["classes"]]
        assert any("EquipmentMaster" in u for u in class_uris)
        assert any("EquipmentStatus" in u for u in class_uris)

    @patch("tools.local_artifacts.read_tbox")
    def test_object_property_details(self, mock_read_tbox, sample_tbox_ttl):
        mock_read_tbox.return_value = sample_tbox_ttl
        result = json.loads(analyze_tbox())
        obj_props = result["object_properties"]
        assert len(obj_props) == 1
        assert any("EquipmentMaster" in str(p["domain"]) for p in obj_props)
        assert any("EquipmentStatus" in str(p["range"]) for p in obj_props)


class TestCheckCardinalityWithCompleteness:
    """CWA/OWA 브릿지: completenessStatus에 따라 카디널리티 위반 심각도 분기."""

    def test_closed_min_cardinality_violation_is_error(self):
        """closed (CWA) + minCardinality 위반 -> severity='error'."""
        result = _check_cardinality_with_completeness(
            cls_uri="steel:EquipmentMaster",
            completeness="closed",
            restriction_type="minCardinality",
            expected=1,
            actual=0,
        )
        assert result is not None
        assert result["severity"] == "error"
        assert result["completeness"] == "closed"

    def test_open_min_cardinality_violation_is_info(self):
        """open (OWA) + minCardinality 위반 -> severity='info'."""
        result = _check_cardinality_with_completeness(
            cls_uri="steel:QualitySpec",
            completeness="open",
            restriction_type="minCardinality",
            expected=1,
            actual=0,
        )
        assert result is not None
        assert result["severity"] == "info"
        assert result["completeness"] == "open"

    def test_inferred_returns_none(self):
        """inferred -> 검증 스킵 (None 반환)."""
        result = _check_cardinality_with_completeness(
            cls_uri="steel:DerivedClass",
            completeness="inferred",
            restriction_type="minCardinality",
            expected=1,
            actual=0,
        )
        assert result is None
