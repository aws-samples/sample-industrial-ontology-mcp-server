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

    def test_empty_string(self, sample_tbox_ttl, tmp_path, monkeypatch):
        # 빈 문자열이면 기본 T-Box 경로를 읽는다. 생성 산출물 유무에 결과가 좌우되지
        # 않도록 기본 경로를 픽스처 파일로 돌린다.
        tbox_path = tmp_path / "t_box.ttl"
        tbox_path.write_text(sample_tbox_ttl, encoding="utf-8")
        monkeypatch.setattr("tools.validation_core.TBOX_PATH", str(tbox_path))
        result = json.loads(validate_ttl_syntax(""))
        assert result["success"] is True
        assert result["triples"] > 0

    def test_empty_string_without_default_tbox(self, tmp_path, monkeypatch):
        # 파이프라인을 돌리기 전에는 기본 T-Box 가 없다. 이때는 성공을 꾸며내지 않고
        # 오류를 반환해야 한다.
        missing = tmp_path / "t_box.ttl"
        monkeypatch.setattr("tools.validation_core.TBOX_PATH", str(missing))
        result = json.loads(validate_ttl_syntax(""))
        assert result["success"] is False
        assert str(missing) in result["error"]

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

    def test_empty_data(self, sample_tbox_ttl, tmp_path, monkeypatch):
        # 빈 입력은 기본 T-Box 경로를 검증한다. 생성 산출물 대신 픽스처 파일을 쓴다.
        tbox_path = tmp_path / "t_box.ttl"
        tbox_path.write_text(sample_tbox_ttl, encoding="utf-8")
        monkeypatch.setattr("tools.validation_core.TBOX_PATH", str(tbox_path))
        result = json.loads(validate_shacl(""))
        assert "conforms" in result
        assert result["conforms"] is True

    def test_empty_data_without_default_tbox(self, tmp_path, monkeypatch):
        # 기본 T-Box 가 없으면 conforms 를 보고하지 않고 오류를 반환한다.
        monkeypatch.setattr(
            "tools.validation_core.TBOX_PATH", str(tmp_path / "t_box.ttl"),
        )
        result = json.loads(validate_shacl(""))
        assert result["success"] is False
        assert "conforms" not in result


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


class TestValidateKgDescription:
    """``validate_kg`` docstring 은 MCP 도구 설명으로 게시된다.

    설명에 적힌 검증 목록이 실제 등록 check 와 어긋나면 사용자는 없는 검증을 믿거나
    있는 검증을 모른다. 목록의 괄호 key 집합이 레지스트리 key 집합과 같아야 한다.
    """

    def test_docstring_lists_exactly_registered_checks(self):
        import inspect
        import re

        from tools.kg_validation import _build_check_registry, validate_kg

        # lambda 는 실행하지 않으므로 그래프 없이 key 만 읽는다.
        registry = _build_check_registry(
            g=None, tbox=None, shared=None, class_tiers=None, raw_triple_count=0,
        )
        doc = inspect.getdoc(validate_kg)
        listed = re.findall(r"^\s*\d+\. .+? \(([a-z_]+)[,)]", doc, flags=re.MULTILINE)
        assert len(listed) == len(set(listed)), f"중복 항목: {listed}"
        assert sorted(listed) == sorted(registry.keys())
        assert f"{len(registry)}가지" in doc


class TestOpGroundingGateDiagnosis:
    """step_22f 경고가 A-Box 신호 부재를 S2 출력 결함으로 오진하지 않는가.

    S3 는 S7 앞이라 A-Box 근거를 이전 세대 파일로 판정한다. 파일이 없거나 판정이
    불가하면 CSV FK 로 채워질 OP 도 근거 없음으로 세므로, 메시지는 그 단서를 싣고
    S2 진단을 내지 않아야 한다. 신호가 정상일 때만 S2 진단을 낸다.
    """

    _S2_BLAME = "S2 출력이 그 절을 어겼거나"
    _THIN_CLUE = "op_grounding_abox_file_present=False"

    @staticmethod
    def _graph(op_names):
        from rdflib import Graph

        from domain.namespaces import DOMAIN_NS, NS_PREFIX

        body = "".join(f"{NS_PREFIX}:{name} a owl:ObjectProperty .\n" for name in op_names)
        g = Graph()
        g.parse(
            data=(
                f"@prefix {NS_PREFIX}: <{DOMAIN_NS}> .\n"
                "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n" + body
            ),
            format="turtle",
        )
        return g

    @staticmethod
    def _point_signal_paths(monkeypatch, abox_path, source_dir):
        import config
        from tools.quality_steps import step_22f_op_grounding_gate as gate

        monkeypatch.setattr(config, "ABOX_PATH", str(abox_path))
        monkeypatch.setattr(config, "SOURCE_DIR", str(source_dir))
        monkeypatch.setattr(gate, "ABOX_PATH", str(abox_path))
        monkeypatch.setattr(gate, "_config_mentioned_names", lambda: set())
        monkeypatch.setenv("TBOX_OP_GROUNDING_MAX", "0")
        return gate

    def _fail_message(self, monkeypatch, gate, op_names):
        import pytest

        from domain.namespaces import DOMAIN_NS
        from tools.quality_steps._base import StepContext

        monkeypatch.setenv("TBOX_OP_GROUNDING_GATE", "fail")
        with pytest.raises(RuntimeError) as excinfo:
            gate.apply(self._graph(op_names), StepContext(domain_ns=DOMAIN_NS))
        return str(excinfo.value)

    def test_missing_abox_message_carries_the_signal_clue(self, monkeypatch, tmp_path):
        gate = self._point_signal_paths(
            monkeypatch, tmp_path / "abox" / "a_box.ttl", tmp_path / "source",
        )
        message = self._fail_message(monkeypatch, gate, [f"op{i}" for i in range(45)])

        assert self._THIN_CLUE in message
        assert "S7" in message
        assert self._S2_BLAME not in message

    def test_missing_abox_warn_log_carries_the_signal_clue(
        self, monkeypatch, tmp_path, caplog,
    ):
        import logging

        from domain.namespaces import DOMAIN_NS
        from tools.quality_steps._base import StepContext

        gate = self._point_signal_paths(
            monkeypatch, tmp_path / "abox" / "a_box.ttl", tmp_path / "source",
        )
        monkeypatch.delenv("TBOX_OP_GROUNDING_GATE", raising=False)
        with caplog.at_level(logging.WARNING, logger=gate.__name__):
            stats = gate.apply(
                self._graph(["imaginedOp"]), StepContext(domain_ns=DOMAIN_NS),
            ).stats

        assert stats["op_grounding_abox_file_present"] is False
        warnings = [r.getMessage() for r in caplog.records if "WARN" in r.getMessage()]
        assert warnings, caplog.text
        assert self._THIN_CLUE in warnings[0]
        assert self._S2_BLAME not in warnings[0]

    def test_healthy_signal_keeps_the_s2_diagnosis(self, monkeypatch, tmp_path):
        from domain.namespaces import DOMAIN_NS

        abox_path = tmp_path / "abox" / "a_box.ttl"
        abox_path.parent.mkdir()
        abox_path.write_text(
            f"<{DOMAIN_NS}item1> <{DOMAIN_NS}unrelatedOp> <{DOMAIN_NS}item2> .\n",
            encoding="utf-8",
        )
        gate = self._point_signal_paths(monkeypatch, abox_path, tmp_path / "source")
        message = self._fail_message(monkeypatch, gate, ["imaginedOp"])

        assert self._S2_BLAME in message
        assert "op_grounding_abox_file_present" not in message
