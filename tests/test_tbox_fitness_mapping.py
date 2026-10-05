"""T-Box Fitness 판정 회귀 가드 — 매핑 경유 + 추상 클래스 제외.

배경 (2026-07-25 실측): `check_tbox_fitness` 가 **클래스명과 CSV 파일명의 글자
유사도** 로만 데이터 소스 유무를 판정했다. 테이블명이 시스템 코드
(`SOURCE_TABLE_002`) 이고 클래스가 도메인 용어(`ProcessStepA`) 면 글자가
전혀 겹치지 않아:

  - fitness 2.9% (34개 중 1개만 매칭 — `SRC_TBL_13` 에 "materialA" 이 우연히 포함돼
    `MaterialA` 만 걸렸다)
  - `under_covered` 에 13개 테이블 전부가 "미커버" 로 올라옴
  - 실제로는 13개 테이블이 A-Box 에 100% 적재된 상태

두 가지를 고쳤다:
  1. `rules/domain/table_class_mapping.json` 의 명시적 매핑을 step 0 으로 조회
  2. 자식만 있고 자기 DP 가 없는 **추상 상위 클래스는 분모에서 제외** — 파이프라인
     이 스스로 만들라고 요구하는 계층(design_patterns.json, step_12) 을 감점 사유로
     쓰면 지표가 구조적으로 달성 불가능해진다
"""
from __future__ import annotations

import json

from rdflib import OWL, RDF, RDFS, URIRef

from domain.tbox_utils import _new_graph
from tools.validation_support.checks.semantic import check_tbox_fitness

STEEL = "http://example.com/steel-ontology#"


def _write_mapping(tmp_path, mapping: dict[str, str]):
    """rules/domain/table_class_mapping.json 스텁을 만들고 경로를 반환."""
    rules = tmp_path / "rules"
    rules.mkdir(exist_ok=True)
    (rules / "table_class_mapping.json").write_text(
        json.dumps({"table_class_mapping": mapping, "table_pk_columns": {}}),
        encoding="utf-8",
    )
    return str(rules)


def test_mapping_rescues_code_named_tables(tmp_path, monkeypatch):
    """시스템 코드 테이블명이 매핑을 통해 도메인 클래스와 연결된다."""
    import tools.abox_generation as ab

    csv_dir = tmp_path / "rawdata"
    csv_dir.mkdir()
    (csv_dir / "SOURCE_TABLE_002.csv").write_text(
        "ID_COL_2\nH1\n", encoding="utf-8",
    )

    monkeypatch.setattr(ab, "_RULES_DIR", _write_mapping(
        tmp_path, {"SOURCE_TABLE_002": "steel:ProcessStepA"},
    ))
    monkeypatch.setattr(ab, "_TABLE_CLASS_MAP_CACHE", None)
    monkeypatch.setattr(ab, "_TABLE_CLASS_MAP_MTIME", 0.0)

    g = _new_graph()
    g.add((URIRef(STEEL + "ProcessStepA"), RDF.type, OWL.Class))

    result = check_tbox_fitness(
        g, source_rawdata_dir=str(csv_dir), source_tacit_dir=str(tmp_path),
    )
    assert result["classes_with_source"] == 1, (
        "매핑을 경유하지 않으면 글자가 안 닮아 0으로 집계된다"
    )
    assert result["fitness_pct"] == 100.0
    assert result["under_covered"] == [], (
        "매핑된 테이블이 '미커버' 로 오보고되면 안 된다"
    )


def test_abstract_parent_excluded_from_denominator(tmp_path, monkeypatch):
    """자식만 있고 DP 가 없는 추상 클래스는 채점 대상에서 빠진다."""
    import tools.abox_generation as ab

    csv_dir = tmp_path / "rawdata"
    csv_dir.mkdir()
    (csv_dir / "T_ONE.csv").write_text("COL\n1\n", encoding="utf-8")

    monkeypatch.setattr(ab, "_RULES_DIR", _write_mapping(
        tmp_path, {"T_ONE": "steel:Concrete"},
    ))
    monkeypatch.setattr(ab, "_TABLE_CLASS_MAP_CACHE", None)
    monkeypatch.setattr(ab, "_TABLE_CLASS_MAP_MTIME", 0.0)

    g = _new_graph()
    concrete = URIRef(STEEL + "Concrete")
    abstract = URIRef(STEEL + "AbstractParent")
    g.add((concrete, RDF.type, OWL.Class))
    g.add((abstract, RDF.type, OWL.Class))
    g.add((concrete, RDFS.subClassOf, abstract))   # 추상 = 자식 있음, DP 없음

    result = check_tbox_fitness(
        g, source_rawdata_dir=str(csv_dir), source_tacit_dir=str(tmp_path),
    )
    assert "AbstractParent" in result["abstract_parents_excluded"]
    assert result["tbox_classes"] == 2
    assert result["scored_classes"] == 1
    assert result["fitness_pct"] == 100.0


def test_parent_with_own_dp_stays_in_denominator(tmp_path, monkeypatch):
    """자식이 있어도 자기 DP 를 가진 클래스는 실체이므로 채점한다.

    예: MaterialA 은 MaterialB 을 자식으로 두지만 자기 컬럼 184개를 갖는다.
    """
    import tools.abox_generation as ab

    csv_dir = tmp_path / "rawdata"
    csv_dir.mkdir()
    monkeypatch.setattr(ab, "_RULES_DIR", _write_mapping(tmp_path, {}))
    monkeypatch.setattr(ab, "_TABLE_CLASS_MAP_CACHE", None)
    monkeypatch.setattr(ab, "_TABLE_CLASS_MAP_MTIME", 0.0)

    g = _new_graph()
    parent = URIRef(STEEL + "ParentWithData")
    child = URIRef(STEEL + "Child")
    g.add((parent, RDF.type, OWL.Class))
    g.add((child, RDF.type, OWL.Class))
    g.add((child, RDFS.subClassOf, parent))
    dp = URIRef(STEEL + "parentWithDataValue")
    g.add((dp, RDF.type, OWL.DatatypeProperty))
    g.add((dp, RDFS.domain, parent))

    result = check_tbox_fitness(
        g, source_rawdata_dir=str(csv_dir), source_tacit_dir=str(tmp_path),
    )
    assert "ParentWithData" not in result["abstract_parents_excluded"]
    assert result["scored_classes"] == 2


def test_camel_and_substring_paths_still_work(tmp_path, monkeypatch):
    """매핑이 없어도 기존 이름 유사도 경로가 동작한다 (하위 호환)."""
    import tools.abox_generation as ab

    csv_dir = tmp_path / "rawdata"
    csv_dir.mkdir()
    (csv_dir / "Air_Emission_Monitoring.csv").write_text("C\n1\n", encoding="utf-8")

    monkeypatch.setattr(ab, "_RULES_DIR", _write_mapping(tmp_path, {}))
    monkeypatch.setattr(ab, "_TABLE_CLASS_MAP_CACHE", None)
    monkeypatch.setattr(ab, "_TABLE_CLASS_MAP_MTIME", 0.0)

    g = _new_graph()
    g.add((URIRef(STEEL + "AirEmissionMonitoring"), RDF.type, OWL.Class))

    result = check_tbox_fitness(
        g, source_rawdata_dir=str(csv_dir), source_tacit_dir=str(tmp_path),
    )
    assert result["classes_with_source"] == 1
    assert result["fitness_pct"] == 100.0
