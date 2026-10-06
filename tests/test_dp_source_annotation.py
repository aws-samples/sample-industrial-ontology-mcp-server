"""DP 출처 컬럼 표기 (``dcterms:source``) 기반 컬럼↔DP 매핑 검증.

배경 (2026-07-25 실측): T-Box 생성기 (S2) 는 표준항목 사전의 권위 한글명을
근거로 DP 를 작명하는데 (`QTY_COL_1` → "설비 계측 유량"
→ ``processStepABottomNozzleFlowQty1``), A-Box 생성기는 컬럼명을 camelCase 로 기계
변환해 DP 를 찾았다. 두 이름 생성 경로가 서로를 몰라 CSV 2,570 컬럼 중 731 개가
조용히 폐기되고 T-Box DP 781 개가 값 0건 공백으로 남았다.

해결: T-Box 가 각 DP 에 유래 컬럼을 ``dcterms:source`` 로 선언하고
(``prompts/tbox-prompt-modules/04-property-rules.md`` 의 "DatatypeProperty source column" 절),
``_col_to_prop`` 이 이를 **1순위** 로 조회한다 (step 0). 본 테스트는 그 경로와
안전장치 (충돌 무효화 / domain 가드 / 폴백 하위호환) 를 고정한다.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest
from rdflib import OWL, RDF, RDFS, Literal, Namespace, URIRef

from domain.tbox_utils import _new_graph
from tools.abox_generation import _col_to_prop, _load_dict_contract, _parse_tbox
from tools.quality_steps import step_12e_dp_source_gate as s12e
from tools.quality_steps._base import StepContext

STEEL_STR = "http://example.com/steel-ontology#"
DCTERMS = Namespace("http://purl.org/dc/terms/")

TBOX_TTL = """
@prefix steel: <http://example.com/steel-ontology#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
@prefix dcterms: <http://purl.org/dc/terms/> .

steel:ProcessStepA a owl:Class .
steel:MaterialA a owl:Class .
steel:MaterialB a owl:Class ; rdfs:subClassOf steel:MaterialA .

# 의미 기반 작명 — transliteration 으로는 절대 도달 못하는 이름
steel:processStepABottomNozzleFlowQty1 a owl:DatatypeProperty ;
    dcterms:source "QTY_COL_1" ;
    rdfs:domain steel:ProcessStepA ; rdfs:range xsd:decimal .

# 같은 계열 다른 인덱스 — 접힘 없이 구분돼야 함
steel:processStepABottomNozzleFlowQty12 a owl:DatatypeProperty ;
    dcterms:source "QTY_COL_12" ;
    rdfs:domain steel:ProcessStepA ; rdfs:range xsd:decimal .

# 부모 클래스 선언 — 자식이 상속해 사용
steel:materialAQaGrade a owl:DatatypeProperty ;
    dcterms:source "GRADE_COL_1" ;
    rdfs:domain steel:MaterialA ; rdfs:range xsd:string .

# 충돌 — 두 DP 가 같은 (class, column) 주장
steel:dupA a owl:DatatypeProperty ; dcterms:source "DUP_COL" ;
    rdfs:domain steel:ProcessStepA ; rdfs:range xsd:string .
steel:dupB a owl:DatatypeProperty ; dcterms:source "DUP_COL" ;
    rdfs:domain steel:ProcessStepA ; rdfs:range xsd:string .

# domain 불일치 검증용 — MaterialA 전용
steel:materialAOnly a owl:DatatypeProperty ; dcterms:source "SLAB_ONLY_COL" ;
    rdfs:domain steel:MaterialA ; rdfs:range xsd:string .

# 출처 표기 없음 — 기존 transliteration 경로로 폴백해야 함
steel:processStepALegacyCol a owl:DatatypeProperty ;
    rdfs:domain steel:ProcessStepA ; rdfs:range xsd:string .
"""


def _info() -> dict:
    return _parse_tbox(TBOX_TTL)


# ── step 0: 출처 표기 기반 매핑 ────────────────────────────────────────


def test_source_annotation_resolves_semantic_dp_name():
    """의미 기반 DP 이름을 컬럼 코드로 찾아낸다 (transliteration 불가 케이스)."""
    info = _info()
    assert _col_to_prop("QTY_COL_1", "ProcessStepA", info, {}) == (
        "processStepABottomNozzleFlowQty1"
    )


def test_numbered_series_indices_stay_distinct():
    """번호 계열이 하나의 DP 로 접히지 않는다 (2 vs 12 혼동 방지)."""
    info = _info()
    assert _col_to_prop("QTY_COL_12", "ProcessStepA", info, {}) == (
        "processStepABottomNozzleFlowQty12"
    )


def test_source_lookup_is_case_insensitive_on_column():
    """CSV 헤더 대소문자가 흔들려도 매칭된다 (표기는 원문 유지가 원칙)."""
    info = _info()
    assert _col_to_prop("qty_col_1", "ProcessStepA", info, {}) == (
        "processStepABottomNozzleFlowQty1"
    )


def test_subclass_inherits_parent_declared_source():
    """부모 클래스가 선언한 DP 를 서브클래스가 상속해 사용할 수 있다."""
    info = _info()
    assert _col_to_prop("GRADE_COL_1", "MaterialA", info, {}) == "materialAQaGrade"
    assert _col_to_prop("GRADE_COL_1", "MaterialB", info, {}) == "materialAQaGrade"


def test_conflicting_sources_are_both_rejected():
    """한 (class, column) 을 두 DP 가 주장하면 어느 쪽도 신뢰하지 않는다.

    정본 판별이 불가능한 상태에서 임의로 하나를 고르면 값이 조용히 잘못된 DP 로
    적재된다. 폴백에 맡기고 Step 12e 가 경고한다.
    """
    info = _info()
    assert ("ProcessStepA", "DUP_COL") in info["dp_source_conflicts"]
    assert _col_to_prop("DUP_COL", "ProcessStepA", info, {}) is None


def test_domain_guard_still_applies_to_source_matches():
    """출처가 일치해도 domain 호환 실패면 거부 — 타입 오염 방지."""
    info = _info()
    assert _col_to_prop("SLAB_ONLY_COL", "ProcessStepA", info, {}) is None
    assert _col_to_prop("SLAB_ONLY_COL", "MaterialA", info, {}) == "materialAOnly"


def test_unannotated_dp_falls_back_to_transliteration():
    """출처 표기 없는 DP 는 기존 경로로 동작한다 (하위 호환)."""
    info = _info()
    assert not any(
        col == "LEGACY_COL" for _, col in info["dp_by_source"]
    )
    assert _col_to_prop("LEGACY_COL", "ProcessStepA", info, {}) == "processStepALegacyCol"


def test_parse_tbox_indexes_sources_by_class_and_column():
    """인덱스 키는 (domain class, UPPER(column)) — 동명 컬럼의 클래스 분리."""
    info = _info()
    assert info["dp_by_source"][("ProcessStepA", "QTY_COL_1")] == (
        "processStepABottomNozzleFlowQty1"
    )
    assert info["dp_by_source"][("MaterialA", "GRADE_COL_1")] == "materialAQaGrade"


# ── 딕셔너리 계약서 경로 ──────────────────────────────────────────────


def test_dict_contract_indexes_source_columns(tmp_path):
    """딕셔너리가 실어온 source_columns 가 contract 조회 키로 등재된다."""
    import json

    dict_path = tmp_path / "semantic_dictionary.json"
    dict_path.write_text(json.dumps({
        "classes": {
            "ProcessStepA": {
                "datatype_properties": {
                    "processStepABottomNozzleFlowQty1": {
                        "range": "xsd:decimal",
                        "source_columns": ["QTY_COL_1"],
                    },
                },
            },
        },
    }), encoding="utf-8")

    contract = _load_dict_contract(str(dict_path))
    # 원래 DP 이름 키 + 출처 컬럼 camel 키 양쪽 모두 존재
    assert contract["ProcessStepA"]["processstepabottomnozzleflowqty1"] == (
        "processStepABottomNozzleFlowQty1"
    )
    assert contract["ProcessStepA"]["qtycol1"] == (
        "processStepABottomNozzleFlowQty1"
    )


# ── Step 12e 게이트 ───────────────────────────────────────────────────


def _gate_graph(with_source: int, without_source: int):
    """DP 개수를 지정해 게이트 입력 그래프를 만든다."""
    g = _new_graph()
    cls = URIRef(STEEL_STR + "ProcessStepA")
    g.add((cls, RDF.type, OWL.Class))
    for i in range(with_source):
        dp = URIRef(f"{STEEL_STR}dpWith{i}")
        g.add((dp, RDF.type, OWL.DatatypeProperty))
        g.add((dp, RDFS.domain, cls))
        g.add((dp, DCTERMS.source, Literal(f"COL_WITH_{i}")))
    for i in range(without_source):
        dp = URIRef(f"{STEEL_STR}dpNone{i}")
        g.add((dp, RDF.type, OWL.DatatypeProperty))
        g.add((dp, RDFS.domain, cls))
    return g


def test_gate_reports_coverage_and_passes_above_threshold(monkeypatch):
    monkeypatch.setenv("TBOX_DP_SOURCE_GATE", "warn")
    monkeypatch.setenv("TBOX_DP_SOURCE_THRESHOLD", "0.9")
    result = s12e.apply(_gate_graph(19, 1), StepContext(domain_ns=STEEL_STR))
    assert result.stats["dp_total"] == 20
    assert result.stats["dp_with_source"] == 19
    assert result.stats["coverage"] == 0.95
    assert result.stats["gate_passed"] is True
    assert result.error is None
    assert result.triples_delta == 0  # read-only


def test_gate_warns_below_threshold_without_raising(monkeypatch):
    monkeypatch.setenv("TBOX_DP_SOURCE_GATE", "warn")
    monkeypatch.setenv("TBOX_DP_SOURCE_THRESHOLD", "0.95")
    result = s12e.apply(_gate_graph(5, 15), StepContext(domain_ns=STEEL_STR))
    assert result.stats["gate_passed"] is False
    assert result.error and "커버리지" in result.error
    assert result.stats["missing_by_class"]["ProcessStepA"] == 15


def test_gate_fails_hard_in_fail_mode(monkeypatch):
    monkeypatch.setenv("TBOX_DP_SOURCE_GATE", "fail")
    monkeypatch.setenv("TBOX_DP_SOURCE_THRESHOLD", "0.95")
    with pytest.raises(RuntimeError, match="커버리지"):
        s12e.apply(_gate_graph(5, 15), StepContext(domain_ns=STEEL_STR))


def test_coverage_match_uses_source_annotation():
    """Step 12c/12d 커버리지 판정이 출처 표기로 확정된다.

    이 가드가 없으면 S2 의 한글명 기반 DP (DATE_COL_1 →
    processStepABeChargeTapDoneDate) 를 '미커버' 로 오판해 Step 12d 가 UGLY 중복
    (processStepADATECOL1) 을 주입한다 — 2026-07-25 실측 1,300개.
    """
    from tools.ontology_quality import (
        _collect_class_dp_source_columns,
        _count_class_dps,
        _coverage_match,
    )

    g = _new_graph()
    cls = URIRef(STEEL_STR + "ProcessStepA")
    g.add((cls, RDF.type, OWL.Class))
    dp = URIRef(STEEL_STR + "processStepABeChargeTapDoneDate")
    g.add((dp, RDF.type, OWL.DatatypeProperty))
    g.add((dp, RDFS.domain, cls))
    g.add((dp, DCTERMS.source, Literal("DATE_COL_1")))

    declared = _count_class_dps(g, STEEL_STR, "ProcessStepA")
    sources = _collect_class_dp_source_columns(g, STEEL_STR, "ProcessStepA")
    assert sources == {"DATE_COL_1"}

    # 이름 유사도만으로는 못 찾는다 (회귀의 근본 원인).
    assert not _coverage_match("date_col_1", "processStepA", declared)
    # 출처 표기를 넘기면 확정 커버.
    assert _coverage_match("date_col_1", "processStepA", declared, sources)
    # 과매칭 방지 — 무관 컬럼은 여전히 미커버.
    assert not _coverage_match("some_unrelated_col", "processStepA", declared, sources)


def test_coverage_match_falls_back_without_annotations():
    """표기 없는 T-Box 에서는 기존 이름 유사도 경로가 그대로 동작한다."""
    from tools.ontology_quality import (
        _collect_class_dp_source_columns,
        _count_class_dps,
        _coverage_match,
    )

    g = _new_graph()
    cls = URIRef(STEEL_STR + "MaterialA")
    g.add((cls, RDF.type, OWL.Class))
    dp = URIRef(STEEL_STR + "materialAWidth")
    g.add((dp, RDF.type, OWL.DatatypeProperty))
    g.add((dp, RDFS.domain, cls))

    declared = _count_class_dps(g, STEEL_STR, "MaterialA")
    sources = _collect_class_dp_source_columns(g, STEEL_STR, "MaterialA")
    assert sources == set()
    assert _coverage_match("width", "materialA", declared, sources)


def test_architect_dsl_records_source_on_new_dp():
    """Architect 의 add_datatype_property 가 source 를 dcterms:source 로 기록한다.

    라운드 수정으로 추가되는 DP 도 초안 DP 와 동일하게 표기돼야 Step 12e 게이트를
    통과하고 A-Box 가 해당 컬럼을 적재할 수 있다.
    """
    from tools.multi_agent_tbox import _apply_high_level_instructions

    base = (
        "@prefix steel: <http://example.com/steel-ontology#> .\n"
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .\n"
        "steel:ProcessStepA a owl:Class .\n"
    )
    out = _apply_high_level_instructions(base, [{
        "action": "add_datatype_property",
        "name": "processStepATapTemp",
        "domain": "ProcessStepA",
        "range": "xsd:decimal",
        "label_ko": "출강온도",
        "source": "STAGE1_TEMP_COL",
    }])
    info = _parse_tbox(out)
    assert info["dp_by_source"][("ProcessStepA", "STAGE1_TEMP_COL")] == "processStepATapTemp"
    assert _col_to_prop("STAGE1_TEMP_COL", "ProcessStepA", info, {}) == "processStepATapTemp"


def test_gate_flags_duplicate_source_claims(monkeypatch):
    monkeypatch.setenv("TBOX_DP_SOURCE_GATE", "warn")
    g = _gate_graph(2, 0)
    cls = URIRef(STEEL_STR + "ProcessStepA")
    clash = URIRef(STEEL_STR + "dpClash")
    g.add((clash, RDF.type, OWL.DatatypeProperty))
    g.add((clash, RDFS.domain, cls))
    g.add((clash, DCTERMS.source, Literal("COL_WITH_0")))  # dpWith0 과 충돌
    result = s12e.apply(g, StepContext(domain_ns=STEEL_STR))
    assert result.stats["duplicate_source_count"] == 1
    assert "ProcessStepA/COL_WITH_0" in result.stats["duplicate_sources_sample"]


# ── 프롬프트 인용 계약 ─────────────────────────────────────────────────
#
# 04-property-rules.md 에는 번호 붙은 원칙이 없고 절 제목만 있다. 게이트 메시지가
# 번호나 없는 제목을 가리키면 사용자는 근거를 찾지 못한다.

_REPO = Path(__file__).resolve().parent.parent
_PROPERTY_RULES = _REPO / "prompts" / "tbox-prompt-modules" / "04-property-rules.md"
_QUALITY_STEPS = _REPO / "tools" / "quality_steps"
_SECTION_CITATION = re.compile(r'\\?"([A-Za-z][A-Za-z ]*[A-Za-z])\\?" 절')
_NUMBERED_PRINCIPLE = re.compile(r"원칙\s*\d")


def _property_rule_headings() -> set[str]:
    text = _PROPERTY_RULES.read_text(encoding="utf-8")
    return {line.lstrip("#").strip() for line in text.splitlines() if line.startswith("#")}


def _assert_cites_existing_sections(text: str) -> None:
    assert not _NUMBERED_PRINCIPLE.search(text), f"번호 원칙을 인용한다: {text}"
    cited = set(_SECTION_CITATION.findall(text))
    assert cited, f"04-property-rules.md 절 제목 인용이 없다: {text}"
    missing = cited - _property_rule_headings()
    assert not missing, f"04-property-rules.md 에 없는 절: {sorted(missing)}"


def test_gate_failure_message_cites_existing_prompt_sections(monkeypatch):
    """커버리지 미달 RuntimeError 가 실제 절 제목으로 근거를 안내한다."""
    monkeypatch.setenv("TBOX_DP_SOURCE_GATE", "fail")
    monkeypatch.setenv("TBOX_DP_SOURCE_THRESHOLD", "0.95")
    with pytest.raises(RuntimeError) as excinfo:
        s12e.apply(_gate_graph(5, 15), StepContext(domain_ns=STEEL_STR))
    _assert_cites_existing_sections(str(excinfo.value))


def test_multi_column_warning_cites_existing_prompt_section(monkeypatch, caplog):
    """한 DP 가 여러 컬럼을 주장할 때의 WARN 도 실제 절 제목을 가리킨다."""
    monkeypatch.setenv("TBOX_DP_SOURCE_GATE", "warn")
    g = _gate_graph(1, 0)
    g.add((URIRef(STEEL_STR + "dpWith0"), DCTERMS.source, Literal("COL_OTHER")))
    with caplog.at_level(logging.WARNING, logger=s12e.logger.name):
        result = s12e.apply(g, StepContext(domain_ns=STEEL_STR))
    assert result.stats["multi_column_dp_count"] == 1
    warnings = [r.getMessage() for r in caplog.records if "컬럼 여럿" in r.getMessage()]
    assert len(warnings) == 1
    _assert_cites_existing_sections(warnings[0])


def test_quality_step_prompt_citations_name_existing_sections():
    """04-property-rules.md 를 인용하는 S3 스텝은 번호 원칙 대신 실제 절 제목을 쓴다."""
    citing = [
        path for path in sorted(_QUALITY_STEPS.glob("*.py"))
        if "04-property-rules" in path.read_text(encoding="utf-8")
    ]
    assert citing
    headings = _property_rule_headings()
    for path in citing:
        src = path.read_text(encoding="utf-8")
        assert not _NUMBERED_PRINCIPLE.search(src), f"{path.name}: 번호 원칙 인용"
        missing = set(_SECTION_CITATION.findall(src)) - headings
        assert not missing, f"{path.name}: 04-property-rules.md 에 없는 절 {sorted(missing)}"
