"""Step 13b — 데이터 충진율에 맞지 않는 minCardinality 완화 회귀 가드.

배경 (2026-07-25 실측): S2 Architect(LLM) 는 CSV **헤더만** 받고 값의 충진율은
보지 못한다. 그래서 실제로는 비어 있는 행이 많은 컬럼에도 필수 제약을 선언했다:

    steel:ProcessStepA_processStepAALblwGsTp_minCardinality  owl:minCardinality 1
      → 출처 TYPE_COL_3 는 794행 중 522행만 채워짐 (65.7%)
      → ProcessStepA 인스턴스 272개 카디널리티 위반 (validate_kg FAIL)

원본 CSV 가 그런 상태이므로 실데이터로는 절대 통과할 수 없는 제약이다. 이 단계가
CSV 를 실측해 만족 불가한 제약을 제거한다.

**임계치는 완화 여부를 결정하지 않는다.** minCardinality 는 인스턴스 하나만
비어도 위반이므로 충진율 < 100% 면 항상 완화한다. 실측 반례:
``processStepABlwMethTp`` 는 794행 중 793행(99.9%) 채움 → 임계치 95% 를 통과했지만
빈 1행이 그대로 validate_kg 위반으로 남았다. 임계치는 로그 사유 구분용이다.

**제약은 자식에게 상속되므로 자식 CSV 도 본다.** ``Order.idCol1`` 는 Order
테이블에서 100% 채워졌지만 자식 ``PlanRecordB`` 의 테이블(SRC_TBL_04) 에는
``ID_COL_1`` 컬럼이 아예 없어 약 800 인스턴스가 전부 위반했다. 자손 중 하나라도
컬럼을 갖지 않으면 **PK 라도** 완화한다 — 부모의 PK 가 자식의 PK 는 아니다.

보수 원칙: PK 는 (상속 문제가 없는 한) 보존하고, **출처 표기가 없어** 이름 유추만
가능한 경우엔 건드리지 않는다. 반면 출처가 명시됐는데 그 컬럼이 자기 클래스
테이블에 없으면 (실측: ``Order.keyCol1`` ← ``KEY_COL_1`` 은 다른 테이블 컬럼)
A-Box 가 채울 경로가 없어 전 인스턴스 위반이므로 완화한다 — 근거 없음이 아니라
근거 있는 오류다.
"""
from __future__ import annotations

import json

from rdflib import OWL, RDF, RDFS, Literal, Namespace, URIRef

from domain.tbox_utils import _new_graph
from tools.quality_steps import step_13b_cardinality_data_reality as s13b
from tools.quality_steps._base import StepContext

STEEL = "http://example.com/steel-ontology#"
DCTERMS = Namespace("http://purl.org/dc/terms/")


def _setup(tmp_path, monkeypatch, rows: list[dict], pk: list[str]):
    """CSV + 매핑 스텁을 만들고 모듈 캐시를 초기화."""
    import config
    import tools.abox_generation as ab

    rawdata = tmp_path / "rawdata"
    rawdata.mkdir(exist_ok=True)
    header = list(rows[0].keys())
    lines = [",".join(header)]
    lines += [",".join(r.get(c, "") for c in header) for r in rows]
    (rawdata / "T_FACT.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")

    rules = tmp_path / "rules"
    rules.mkdir(exist_ok=True)
    (rules / "table_class_mapping.json").write_text(json.dumps({
        "table_class_mapping": {"T_FACT": "steel:Fact"},
        "table_pk_columns": {"T_FACT": pk},
    }), encoding="utf-8")

    monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", str(rawdata))
    monkeypatch.setattr(ab, "_RULES_DIR", str(rules))
    monkeypatch.setattr(ab, "_TABLE_CLASS_MAP_CACHE", None)
    monkeypatch.setattr(ab, "_TABLE_CLASS_MAP_MTIME", 0.0)
    monkeypatch.setattr(ab, "_TABLE_PK_COLUMNS_CACHE", None)
    monkeypatch.setattr(ab, "_TABLE_PK_COLUMNS_MTIME", 0.0)


def _graph_with_min_cardinality(dp_local: str, source_col: str) -> tuple:
    """Fact 클래스 + minCardinality 제약 그래프를 만든다."""
    g = _new_graph()
    cls = URIRef(STEEL + "Fact")
    g.add((cls, RDF.type, OWL.Class))
    dp = URIRef(STEEL + dp_local)
    g.add((dp, RDF.type, OWL.DatatypeProperty))
    g.add((dp, RDFS.domain, cls))
    g.add((dp, DCTERMS.source, Literal(source_col)))
    restriction = URIRef(STEEL + f"Fact_{dp_local}_minCardinality")
    g.add((restriction, RDF.type, OWL.Restriction))
    g.add((restriction, OWL.minCardinality, Literal(1)))
    g.add((restriction, OWL.onProperty, dp))
    g.add((cls, RDFS.subClassOf, restriction))
    return g, restriction


def test_relaxes_constraint_when_column_is_sparse(tmp_path, monkeypatch):
    """충진율이 임계치 미달이면 minCardinality 를 제거한다."""
    _setup(tmp_path, monkeypatch, [
        {"KEY": "1", "SPARSE": "a"},
        {"KEY": "2", "SPARSE": ""},
        {"KEY": "3", "SPARSE": ""},
        {"KEY": "4", "SPARSE": ""},
    ], pk=["KEY"])
    g, restriction = _graph_with_min_cardinality("factSparse", "SPARSE")

    result = s13b.apply(g, StepContext(domain_ns=STEEL))
    assert result.stats["relaxed_count"] == 1
    assert result.stats["relaxed"][0]["fill_ratio"] == 0.25
    assert result.stats["relaxed"][0]["reason"] == "below_threshold"
    assert (restriction, RDF.type, OWL.Restriction) not in g


def test_keeps_constraint_only_when_fully_filled(tmp_path, monkeypatch):
    """100% 채워진 컬럼만 필수 제약을 유지한다."""
    _setup(tmp_path, monkeypatch, [
        {"KEY": str(i), "DENSE": "v"} for i in range(20)
    ], pk=["KEY"])
    g, restriction = _graph_with_min_cardinality("factDense", "DENSE")

    result = s13b.apply(g, StepContext(domain_ns=STEEL))
    assert result.stats["relaxed_count"] == 0
    assert (restriction, RDF.type, OWL.Restriction) in g


def test_relaxes_near_full_column_above_threshold(tmp_path, monkeypatch):
    """임계치를 넘겨도 100% 가 아니면 완화한다.

    실측 반례: processStepABlwMethTp 는 794행 중 793행(99.9%) 채움 → 임계치 95% 는
    통과했지만 빈 1행이 그대로 validate_kg 카디널리티 위반으로 남았다.
    minCardinality 는 인스턴스 하나만 비어도 위반이기 때문이다.
    """
    rows = [{"KEY": str(i), "NEAR": "v"} for i in range(99)]
    rows.append({"KEY": "99", "NEAR": ""})       # 99% 채움
    _setup(tmp_path, monkeypatch, rows, pk=["KEY"])
    g, restriction = _graph_with_min_cardinality("factNear", "NEAR")

    result = s13b.apply(g, StepContext(domain_ns=STEEL))
    assert result.stats["relaxed_count"] == 1
    assert result.stats["relaxed"][0]["reason"] == "not_fully_filled"
    assert result.stats["relaxed"][0]["fill_ratio"] == 0.99
    assert (restriction, RDF.type, OWL.Restriction) not in g


def test_relaxes_when_declared_source_absent_from_class_table(
    tmp_path, monkeypatch,
):
    """출처가 명시됐으나 자기 테이블에 없는 컬럼이면 완화한다.

    실측: Order.keyCol1 의 출처 KEY_COL_1 은 Order 의 원본 테이블(SRC_TBL_01) 에 없고
    다른 테이블에만 있었다. A-Box 가 값을 채울 경로가 없어 전 인스턴스가 위반한다.
    """
    _setup(tmp_path, monkeypatch, [
        {"KEY": "1", "REAL_COL": "v"},
    ], pk=["KEY"])
    g, restriction = _graph_with_min_cardinality("factGhost", "COL_FROM_OTHER_TABLE")

    result = s13b.apply(g, StepContext(domain_ns=STEEL))
    assert result.stats["relaxed_count"] == 1
    record = result.stats["relaxed"][0]
    assert record["reason"] == "source_column_absent_in_class_table"
    assert record["column"] == "COL_FROM_OTHER_TABLE"
    assert record["fill_ratio"] is None
    assert (restriction, RDF.type, OWL.Restriction) not in g


def test_keeps_constraint_without_source_annotation(tmp_path, monkeypatch):
    """출처 표기가 없으면 이름 유추뿐이라 근거가 약해 보류한다."""
    _setup(tmp_path, monkeypatch, [
        {"KEY": "1", "REAL_COL": "v"},
    ], pk=["KEY"])
    g, restriction = _graph_with_min_cardinality("factGhost", "IGNORED")
    # 출처 표기를 제거 — 이름 유추 경로만 남는다
    g.remove((URIRef(STEEL + "factGhost"), DCTERMS.source, None))

    result = s13b.apply(g, StepContext(domain_ns=STEEL))
    assert result.stats["relaxed_count"] == 0
    assert result.stats["unresolved_count"] == 1
    assert (restriction, RDF.type, OWL.Restriction) in g


def test_pk_constraint_is_always_preserved(tmp_path, monkeypatch):
    """PK 컬럼은 충진율이 낮게 측정돼도 제약을 보존한다."""
    _setup(tmp_path, monkeypatch, [
        {"KEY": "1", "OTHER": "x"},
        {"KEY": "", "OTHER": "y"},
        {"KEY": "", "OTHER": "z"},
    ], pk=["KEY"])
    g, restriction = _graph_with_min_cardinality("factKey", "KEY")

    result = s13b.apply(g, StepContext(domain_ns=STEEL))
    assert result.stats["relaxed_count"] == 0
    assert result.stats["kept_pk_count"] == 1
    assert (restriction, RDF.type, OWL.Restriction) in g


def test_warn_mode_reports_without_changing_graph(tmp_path, monkeypatch):
    """warn 모드는 보고만 하고 그래프를 수정하지 않는다."""
    monkeypatch.setenv("TBOX_CARDINALITY_DATA_CHECK", "warn")
    _setup(tmp_path, monkeypatch, [
        {"KEY": "1", "SPARSE": "a"},
        {"KEY": "2", "SPARSE": ""},
    ], pk=["KEY"])
    g, restriction = _graph_with_min_cardinality("factSparse", "SPARSE")
    before = len(g)

    result = s13b.apply(g, StepContext(domain_ns=STEEL))
    assert result.stats["mode"] == "warn"
    assert result.stats["relaxed_count"] == 1
    assert len(g) == before
    assert (restriction, RDF.type, OWL.Restriction) in g


def test_off_mode_skips(tmp_path, monkeypatch):
    """off 모드는 스텝 자체를 건너뛴다."""
    monkeypatch.setenv("TBOX_CARDINALITY_DATA_CHECK", "off")
    g, restriction = _graph_with_min_cardinality("factSparse", "SPARSE")
    before = len(g)

    result = s13b.apply(g, StepContext(domain_ns=STEEL))
    assert "skipped" in result.stats
    assert len(g) == before


def test_threshold_only_labels_severity(tmp_path, monkeypatch):
    """TBOX_CARDINALITY_MIN_FILL 은 완화 여부가 아니라 사유 라벨만 바꾼다.

    임계치를 낮춰도 100% 미만이면 여전히 완화한다 — 빈 행이 하나라도 있으면
    minCardinality 는 만족될 수 없기 때문이다. 임계치는 로그·통계에서 심각도를
    구분하는 용도다.
    """
    monkeypatch.setenv("TBOX_CARDINALITY_MIN_FILL", "0.2")
    _setup(tmp_path, monkeypatch, [
        {"KEY": "1", "SPARSE": "a"},
        {"KEY": "2", "SPARSE": ""},
        {"KEY": "3", "SPARSE": ""},
        {"KEY": "4", "SPARSE": ""},
    ], pk=["KEY"])
    g, restriction = _graph_with_min_cardinality("factSparse", "SPARSE")

    # 충진 25% > 임계치 20% → 사유는 not_fully_filled, 그래도 완화
    result = s13b.apply(g, StepContext(domain_ns=STEEL))
    assert result.stats["relaxed_count"] == 1
    assert result.stats["relaxed"][0]["reason"] == "not_fully_filled"
    assert result.stats["relaxed_by_reason"] == {"not_fully_filled": 1}
    assert (restriction, RDF.type, OWL.Restriction) not in g


# ── 상속 검사: 자식 클래스의 CSV 에 컬럼이 없는 경우 ──────────────────


def _setup_parent_child(tmp_path, monkeypatch):
    """부모(Order)/자식(PlanRecordB) 이 서로 다른 CSV 를 모델링하는 스텁.

    실측 (2026-07-25): Order.idCol1 (ID_COL_1, Order 테이블 100% 채움) 의 필수
    제약이 PlanRecordB 로 상속됐으나 PlanRecordB 의 원본 테이블 SRC_TBL_04 에는
    ID_COL_1 컬럼이 없어 약 800 인스턴스 전부가 위반했다.
    """
    import config
    import tools.abox_generation as ab

    rawdata = tmp_path / "rawdata"
    rawdata.mkdir(exist_ok=True)
    (rawdata / "T_ORDER.csv").write_text(
        "ID_COL_1,QTY\nO1,5\n", encoding="utf-8",
    )
    (rawdata / "T_CHORD.csv").write_text(       # ID_COL_1 없음
        "KEY_COL_3,KEY_COL_4\nC1,H1\n", encoding="utf-8",
    )

    rules = tmp_path / "rules"
    rules.mkdir(exist_ok=True)
    (rules / "table_class_mapping.json").write_text(json.dumps({
        "table_class_mapping": {
            "T_ORDER": "steel:Order",
            "T_CHORD": "steel:PlanRecordB",
        },
        "table_pk_columns": {
            "T_ORDER": ["ID_COL_1"],
            "T_CHORD": ["KEY_COL_3"],
        },
    }), encoding="utf-8")

    monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", str(rawdata))
    monkeypatch.setattr(ab, "_RULES_DIR", str(rules))
    monkeypatch.setattr(ab, "_TABLE_CLASS_MAP_CACHE", None)
    monkeypatch.setattr(ab, "_TABLE_CLASS_MAP_MTIME", 0.0)
    monkeypatch.setattr(ab, "_TABLE_PK_COLUMNS_CACHE", None)
    monkeypatch.setattr(ab, "_TABLE_PK_COLUMNS_MTIME", 0.0)


def _graph_parent_constraint(*, with_child: bool = True):
    """Order 에 걸린 minCardinality + (선택) PlanRecordB 자식 관계."""
    g = _new_graph()
    parent = URIRef(STEEL + "Order")
    g.add((parent, RDF.type, OWL.Class))
    dp = URIRef(STEEL + "idCol1")
    g.add((dp, RDF.type, OWL.DatatypeProperty))
    g.add((dp, RDFS.domain, parent))
    g.add((dp, DCTERMS.source, Literal("ID_COL_1")))
    restriction = URIRef(STEEL + "Order_idCol1_minCardinality")
    g.add((restriction, RDF.type, OWL.Restriction))
    g.add((restriction, OWL.minCardinality, Literal(1)))
    g.add((restriction, OWL.onProperty, dp))
    g.add((parent, RDFS.subClassOf, restriction))
    if with_child:
        child = URIRef(STEEL + "PlanRecordB")
        g.add((child, RDF.type, OWL.Class))
        g.add((child, RDFS.subClassOf, parent))
    return g, restriction


def test_relaxes_when_subclass_table_lacks_column(tmp_path, monkeypatch):
    """자식 CSV 에 컬럼이 없으면 PK 제약이라도 완화한다.

    부모의 PK 가 자식의 PK 는 아니므로 PK 보존 규칙보다 상속 검사가 앞선다.
    """
    _setup_parent_child(tmp_path, monkeypatch)
    g, restriction = _graph_parent_constraint()

    result = s13b.apply(g, StepContext(domain_ns=STEEL))

    assert result.stats["relaxed_count"] == 1
    record = result.stats["relaxed"][0]
    assert record["reason"] == "column_absent_in_subclass_table"
    assert record["starved_subclasses"] == ["PlanRecordB"]
    assert result.stats["kept_pk_count"] == 0
    assert (restriction, RDF.type, OWL.Restriction) not in g


def test_keeps_pk_constraint_without_starved_subclass(tmp_path, monkeypatch):
    """자식이 없으면 (또는 자식 CSV 에 컬럼이 있으면) PK 제약을 유지한다."""
    _setup_parent_child(tmp_path, monkeypatch)
    g, restriction = _graph_parent_constraint(with_child=False)

    result = s13b.apply(g, StepContext(domain_ns=STEEL))

    assert result.stats["relaxed_count"] == 0
    assert result.stats["kept_pk"] == ["idCol1"]
    assert (restriction, RDF.type, OWL.Restriction) in g


def test_relaxes_when_subclass_column_partially_filled(tmp_path, monkeypatch):
    """자식 CSV 에 컬럼이 있어도 100% 가 아니면 완화한다.

    컬럼 유무만 보면 "있으니 통과" 로 판정해 자식의 빈 행이 그대로 위반으로
    남는다 — 부모 기준 100% 는 자식 기준 100% 를 보장하지 않는다.
    """
    import config
    import tools.abox_generation as ab

    rawdata = tmp_path / "rawdata"
    rawdata.mkdir(exist_ok=True)
    (rawdata / "T_ORDER.csv").write_text("ID_COL_1,QTY\nO1,5\n", encoding="utf-8")
    # 자식은 ID_COL_1 를 갖지만 2행 중 1행이 비어 있다 (50%)
    (rawdata / "T_CHORD.csv").write_text(
        "KEY_COL_3,ID_COL_1\nC1,O1\nC2,\n", encoding="utf-8",
    )
    rules = tmp_path / "rules"
    rules.mkdir(exist_ok=True)
    (rules / "table_class_mapping.json").write_text(json.dumps({
        "table_class_mapping": {
            "T_ORDER": "steel:Order",
            "T_CHORD": "steel:PlanRecordB",
        },
        "table_pk_columns": {"T_ORDER": ["ID_COL_1"], "T_CHORD": ["KEY_COL_3"]},
    }), encoding="utf-8")
    monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", str(rawdata))
    monkeypatch.setattr(ab, "_RULES_DIR", str(rules))
    monkeypatch.setattr(ab, "_TABLE_CLASS_MAP_CACHE", None)
    monkeypatch.setattr(ab, "_TABLE_CLASS_MAP_MTIME", 0.0)
    monkeypatch.setattr(ab, "_TABLE_PK_COLUMNS_CACHE", None)
    monkeypatch.setattr(ab, "_TABLE_PK_COLUMNS_MTIME", 0.0)

    g, restriction = _graph_parent_constraint()
    result = s13b.apply(g, StepContext(domain_ns=STEEL))

    assert result.stats["relaxed_count"] == 1
    record = result.stats["relaxed"][0]
    assert record["reason"] == "column_absent_in_subclass_table"
    assert record["subclass_fill_ratios"] == {"PlanRecordB": 0.5}
    assert (restriction, RDF.type, OWL.Restriction) not in g


def test_keeps_constraint_when_subclass_fully_filled(tmp_path, monkeypatch):
    """자식 CSV 도 100% 채워져 있으면 제약을 유지한다."""
    import config
    import tools.abox_generation as ab

    rawdata = tmp_path / "rawdata"
    rawdata.mkdir(exist_ok=True)
    (rawdata / "T_ORDER.csv").write_text("ID_COL_1,QTY\nO1,5\n", encoding="utf-8")
    (rawdata / "T_CHORD.csv").write_text(
        "KEY_COL_3,ID_COL_1\nC1,O1\nC2,O2\n", encoding="utf-8",
    )
    rules = tmp_path / "rules"
    rules.mkdir(exist_ok=True)
    (rules / "table_class_mapping.json").write_text(json.dumps({
        "table_class_mapping": {
            "T_ORDER": "steel:Order",
            "T_CHORD": "steel:PlanRecordB",
        },
        "table_pk_columns": {"T_ORDER": ["ID_COL_1"], "T_CHORD": ["KEY_COL_3"]},
    }), encoding="utf-8")
    monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", str(rawdata))
    monkeypatch.setattr(ab, "_RULES_DIR", str(rules))
    monkeypatch.setattr(ab, "_TABLE_CLASS_MAP_CACHE", None)
    monkeypatch.setattr(ab, "_TABLE_CLASS_MAP_MTIME", 0.0)
    monkeypatch.setattr(ab, "_TABLE_PK_COLUMNS_CACHE", None)
    monkeypatch.setattr(ab, "_TABLE_PK_COLUMNS_MTIME", 0.0)

    g, restriction = _graph_parent_constraint()
    result = s13b.apply(g, StepContext(domain_ns=STEEL))

    assert result.stats["relaxed_count"] == 0
    assert result.stats["kept_pk"] == ["idCol1"]
    assert (restriction, RDF.type, OWL.Restriction) in g
