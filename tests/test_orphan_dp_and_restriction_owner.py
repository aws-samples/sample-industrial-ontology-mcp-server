"""껍데기 DP 제거(12f) + restriction 소유자 분리(19b) 회귀 가드.

두 문제가 함께 카디널리티 위반을 만들었다 (2026-07-25 실측).

**문제 1 — restriction BNode 공유 (19b)**
Architect 가 하나의 ``owl:Restriction`` BNode 를 여러 클래스의 ``rdfs:subClassOf``
에 재사용하면 스콜렘화(Step 19) 가 첫 번째 클래스 이름으로 URI 를 만들고, 나머지
클래스는 **남의 이름표를 상속** 한다:

    steel:MaterialSpecA_operationStartDatetime_minCardinality
      ← MaterialSpecA (이름 주인)
      ← MaterialB        (남의 제약을 강요받음 → 카디널리티 위반)

**문제 2 — domain·출처 없는 껍데기 DP (12f)**
S2 가 만든 DP 중 ``rdfs:domain`` 도 ``dcterms:source`` 도 없는 것이 6개 있었다
(operationStartDatetime, facilityOperationCode, scarfing* 등). 어느 클래스 속성인지
어느 컬럼에서 왔는지 알 수 없어 A-Box 사용 0건인데, minCardinality 제약에서
참조되어 **실데이터로 절대 만족할 수 없는 필수 조건** 이 됐다.
"""
from __future__ import annotations

from rdflib import OWL, RDF, RDFS, BNode, Literal, Namespace, URIRef

from domain.tbox_utils import _new_graph
from tools.quality_steps import step_12f_orphan_dp_prune as s12f
from tools.quality_steps import step_19b_restriction_owner_split as s19b
from tools.quality_steps._base import StepContext

STEEL = "http://example.com/steel-ontology#"
DCTERMS = Namespace("http://purl.org/dc/terms/")


# ── 19b: restriction 소유자 분리 ───────────────────────────────────────


def _shared_named_restriction():
    """두 클래스가 하나의 named restriction 을 공유하는 그래프."""
    g = _new_graph()
    spec = URIRef(STEEL + "MaterialSpecA")
    split = URIRef(STEEL + "MaterialB")
    dp = URIRef(STEEL + "operationStartDatetime")
    for cls in (spec, split):
        g.add((cls, RDF.type, OWL.Class))
    g.add((dp, RDF.type, OWL.DatatypeProperty))
    restriction = URIRef(
        STEEL + "MaterialSpecA_operationStartDatetime_minCardinality",
    )
    g.add((restriction, RDF.type, OWL.Restriction))
    g.add((restriction, OWL.onProperty, dp))
    g.add((restriction, OWL.minCardinality, Literal(1)))
    g.add((spec, RDFS.subClassOf, restriction))
    g.add((split, RDFS.subClassOf, restriction))   # 남의 제약 상속
    return g, restriction


def test_borrower_gets_its_own_restriction_clone():
    """이름 주인이 아닌 클래스는 자기 이름의 사본을 받는다."""
    g, original = _shared_named_restriction()

    result = s19b.apply(g, StepContext(domain_ns=STEEL))
    assert result.stats["restrictions_split"] == 1

    split = URIRef(STEEL + "MaterialB")
    spec = URIRef(STEEL + "MaterialSpecA")
    clone = URIRef(STEEL + "MaterialB_operationStartDatetime_minCardinality")
    assert (split, RDFS.subClassOf, clone) in g
    assert (split, RDFS.subClassOf, original) not in g
    assert (spec, RDFS.subClassOf, original) in g, "이름 주인은 그대로 유지"
    # 사본이 원본과 같은 내용을 갖는다
    assert g.value(clone, OWL.minCardinality) == Literal(1)


def test_no_shared_restriction_remains():
    """분리 후 2개 이상 클래스가 공유하는 restriction 이 없다."""
    g, _ = _shared_named_restriction()
    s19b.apply(g, StepContext(domain_ns=STEEL))

    shared = [
        r for r in g.subjects(RDF.type, OWL.Restriction)
        if len(list(g.subjects(RDFS.subClassOf, r))) > 1
    ]
    assert shared == []


def test_single_owner_restriction_untouched():
    """소유 클래스가 하나면 건드리지 않는다."""
    g = _new_graph()
    cls = URIRef(STEEL + "Order")
    dp = URIRef(STEEL + "idCol1")
    g.add((cls, RDF.type, OWL.Class))
    g.add((dp, RDF.type, OWL.DatatypeProperty))
    restriction = URIRef(STEEL + "Order_idCol1_minCardinality")
    g.add((restriction, RDF.type, OWL.Restriction))
    g.add((restriction, OWL.onProperty, dp))
    g.add((restriction, OWL.minCardinality, Literal(1)))
    g.add((cls, RDFS.subClassOf, restriction))
    before = len(g)

    result = s19b.apply(g, StepContext(domain_ns=STEEL))
    assert result.stats["restrictions_split"] == 0
    assert len(g) == before


def test_skolemize_splits_shared_bnode():
    """근본 수정 — 스콜렘화 단계가 BNode 공유를 클래스별로 복제한다."""
    from tools.ontology_quality import _skolemize_bnodes

    g = _new_graph()
    spec = URIRef(STEEL + "MaterialSpecA")
    split = URIRef(STEEL + "MaterialB")
    dp = URIRef(STEEL + "operationStartDatetime")
    for cls in (spec, split):
        g.add((cls, RDF.type, OWL.Class))
    g.add((dp, RDF.type, OWL.DatatypeProperty))
    bnode = BNode()
    g.add((bnode, RDF.type, OWL.Restriction))
    g.add((bnode, OWL.onProperty, dp))
    g.add((bnode, OWL.minCardinality, Literal(1)))
    g.add((spec, RDFS.subClassOf, bnode))
    g.add((split, RDFS.subClassOf, bnode))     # 공유

    _skolemize_bnodes(g)

    shared = [
        r for r in g.subjects(RDF.type, OWL.Restriction)
        if len(list(g.subjects(RDFS.subClassOf, r))) > 1
    ]
    assert shared == [], "BNode 공유가 클래스별로 분리돼야 한다"
    for cls_local in ("MaterialSpecA", "MaterialB"):
        owned = [
            str(r) for r in g.objects(URIRef(STEEL + cls_local), RDFS.subClassOf)
        ]
        assert any(cls_local in r for r in owned), (
            f"{cls_local} 이 자기 이름의 restriction 을 가져야 한다"
        )


# ── 12f: 껍데기 DP 제거 ────────────────────────────────────────────────


def _orphan_dp_graph():
    """domain·출처 없는 DP + 그것을 필수로 참조하는 restriction."""
    g = _new_graph()
    cls = URIRef(STEEL + "MaterialSpecA")
    g.add((cls, RDF.type, OWL.Class))
    orphan = URIRef(STEEL + "operationStartDatetime")
    g.add((orphan, RDF.type, OWL.DatatypeProperty))     # domain·source 없음
    restriction = URIRef(
        STEEL + "MaterialSpecA_operationStartDatetime_minCardinality",
    )
    g.add((restriction, RDF.type, OWL.Restriction))
    g.add((restriction, OWL.onProperty, orphan))
    g.add((restriction, OWL.minCardinality, Literal(1)))
    g.add((cls, RDFS.subClassOf, restriction))
    return g, orphan, restriction


def test_prunes_orphan_dp_and_its_restriction(tmp_path, monkeypatch):
    """A-Box 미사용이면 DP 와 참조 restriction 을 함께 제거한다."""
    import config

    abox = tmp_path / "a_box.ttl"
    abox.write_text("# no usage\n", encoding="utf-8")
    monkeypatch.setattr(config, "ABOX_PATH", str(abox))

    g, orphan, restriction = _orphan_dp_graph()
    result = s12f.apply(g, StepContext(domain_ns=STEEL))

    assert result.stats["orphan_dps_pruned"] == 1
    assert "operationStartDatetime" in result.stats["pruned_names"]
    assert (orphan, RDF.type, OWL.DatatypeProperty) not in g
    assert (restriction, RDF.type, OWL.Restriction) not in g, (
        "DP 만 지우고 restriction 을 남기면 참조 대상 없는 필수 제약이 된다"
    )


def test_keeps_orphan_dp_that_abox_uses(tmp_path, monkeypatch):
    """domain 이 없어도 A-Box 에서 쓰이면 보존한다 (데이터 보호)."""
    import config

    abox = tmp_path / "a_box.ttl"
    abox.write_text(
        "steel-inst:x steel:operationStartDatetime \"YYYY-MM-DD\" .\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "ABOX_PATH", str(abox))

    g, orphan, _ = _orphan_dp_graph()
    result = s12f.apply(g, StepContext(domain_ns=STEEL))

    assert result.stats["orphan_dps_pruned"] == 0
    assert result.stats["kept_in_use"] == ["operationStartDatetime"]
    assert (orphan, RDF.type, OWL.DatatypeProperty) in g


def test_keeps_dp_with_domain_and_source(tmp_path, monkeypatch):
    """domain + 출처가 모두 있으면 후보에 들지 않는다."""
    import config

    abox = tmp_path / "a_box.ttl"
    abox.write_text("# empty\n", encoding="utf-8")
    monkeypatch.setattr(config, "ABOX_PATH", str(abox))

    g = _new_graph()
    cls = URIRef(STEEL + "Order")
    dp = URIRef(STEEL + "idCol1")
    g.add((cls, RDF.type, OWL.Class))
    g.add((dp, RDF.type, OWL.DatatypeProperty))
    g.add((dp, RDFS.domain, cls))
    g.add((dp, DCTERMS.source, Literal("ID_COL_1")))

    result = s12f.apply(g, StepContext(domain_ns=STEEL))
    assert result.stats["candidates"] == 0
    assert (dp, RDF.type, OWL.DatatypeProperty) in g


# ── 12f 유형 3: domain 은 있으나 출처 없고 미사용 ─────────────────────


def _stub_csv_mapping(tmp_path, monkeypatch, tables: dict[str, tuple[str, list[str]]]):
    """CSV + table_class_mapping 스텁. ``{table: (ClassName, [컬럼...])}``.

    유형 3 판정은 "클래스의 CSV 컬럼이 전부 다른 DP 로 선언됐는지" 를 보므로
    실제 rawdata 를 읽으면 테스트가 리포 데이터에 의존한다. 여기서 격리한다.
    """
    import json

    import config
    import tools.abox_generation as ab

    rawdata = tmp_path / "rawdata"
    rawdata.mkdir(exist_ok=True)
    mapping = {}
    for table, (cls_name, columns) in tables.items():
        (rawdata / f"{table}.csv").write_text(
            ",".join(columns) + "\n" + ",".join("v" for _ in columns) + "\n",
            encoding="utf-8",
        )
        mapping[table] = f"steel:{cls_name}"

    rules = tmp_path / "rules"
    rules.mkdir(exist_ok=True)
    (rules / "table_class_mapping.json").write_text(
        json.dumps({"table_class_mapping": mapping}), encoding="utf-8",
    )
    monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", str(rawdata))
    monkeypatch.setattr(ab, "_RULES_DIR", str(rules))
    monkeypatch.setattr(ab, "_TABLE_CLASS_MAP_CACHE", None)
    monkeypatch.setattr(ab, "_TABLE_CLASS_MAP_MTIME", 0.0)


def test_prunes_sourceless_unused_dp(tmp_path, monkeypatch):
    """domain 이 정상이어도 출처가 없고 미사용이면 제거한다.

    실측 (2026-07-25): MaterialSpecA.materialNumber 등 4개가 domain 은 있으나
    출처 표기가 없어 A-Box 가 값을 채울 경로가 없었다. 그런데 minCardinality 1 로
    선언돼 있어 해당 클래스 전 인스턴스가 구조적 위반 상태였다.
    """
    import config

    abox = tmp_path / "a_box.ttl"
    abox.write_text(
        "steel-inst:x steel:materialASpecMtlNo \"M1\" .\n", encoding="utf-8",
    )
    monkeypatch.setattr(config, "ABOX_PATH", str(abox))
    # CSV 컬럼은 MATERIAL_NO 하나뿐 — 아래 materialASpecMtlNo 가 이미 담당한다
    _stub_csv_mapping(
        tmp_path, monkeypatch, {"T_SPEC": ("MaterialSpecA", ["MATERIAL_NO"])},
    )

    g = _new_graph()
    cls = URIRef(STEEL + "MaterialSpecA")
    g.add((cls, RDF.type, OWL.Class))
    ghost = URIRef(STEEL + "materialNumber")
    g.add((ghost, RDF.type, OWL.DatatypeProperty))
    g.add((ghost, RDFS.domain, cls))              # domain 은 정상, 출처 없음
    restriction = URIRef(STEEL + "MaterialSpecA_materialNumber_minCardinality")
    g.add((restriction, RDF.type, OWL.Restriction))
    g.add((restriction, OWL.onProperty, ghost))
    g.add((restriction, OWL.minCardinality, Literal(1)))
    g.add((cls, RDFS.subClassOf, restriction))
    # 같은 값을 담는 실제 DP
    real = URIRef(STEEL + "materialASpecMtlNo")
    g.add((real, RDF.type, OWL.DatatypeProperty))
    g.add((real, RDFS.domain, cls))
    g.add((real, DCTERMS.source, Literal("MATERIAL_NO")))

    result = s12f.apply(g, StepContext(domain_ns=STEEL))

    assert result.stats["pruned_sourceless_names"] == ["materialNumber"]
    assert (ghost, RDF.type, OWL.DatatypeProperty) not in g
    assert (restriction, RDF.type, OWL.Restriction) not in g, (
        "만족 불가한 필수 제약도 함께 사라져야 한다"
    )
    assert (real, RDF.type, OWL.DatatypeProperty) in g


def test_keeps_sourceless_dp_that_abox_uses(tmp_path, monkeypatch):
    """출처가 없어도 A-Box 에서 쓰이면 보존한다."""
    import config

    abox = tmp_path / "a_box.ttl"
    abox.write_text(
        "steel-inst:x steel:legacyValue \"v\" .\n", encoding="utf-8",
    )
    monkeypatch.setattr(config, "ABOX_PATH", str(abox))
    _stub_csv_mapping(tmp_path, monkeypatch, {"T_ORDER": ("Order", ["ID_COL_1"])})

    g = _new_graph()
    cls = URIRef(STEEL + "Order")
    dp = URIRef(STEEL + "legacyValue")
    g.add((cls, RDF.type, OWL.Class))
    g.add((dp, RDF.type, OWL.DatatypeProperty))
    g.add((dp, RDFS.domain, cls))
    # CSV 의 유일한 컬럼은 다른 DP 가 담당 → legacyValue 는 유형 3 후보가 된다
    covered = URIRef(STEEL + "idCol1")
    g.add((covered, RDF.type, OWL.DatatypeProperty))
    g.add((covered, RDFS.domain, cls))
    g.add((covered, DCTERMS.source, Literal("ID_COL_1")))

    result = s12f.apply(g, StepContext(domain_ns=STEEL))
    assert result.stats["kept_in_use"] == ["legacyValue"]
    assert (dp, RDF.type, OWL.DatatypeProperty) in g


def test_sourceless_prune_can_be_disabled(tmp_path, monkeypatch):
    """유형 3 만 따로 끌 수 있다 (출처 표기 미도입 T-Box 보호)."""
    import config

    monkeypatch.setenv("TBOX_PRUNE_SOURCELESS_DPS", "false")
    abox = tmp_path / "a_box.ttl"
    abox.write_text("# empty\n", encoding="utf-8")
    monkeypatch.setattr(config, "ABOX_PATH", str(abox))

    g = _new_graph()
    cls = URIRef(STEEL + "Order")
    dp = URIRef(STEEL + "legacyValue")
    g.add((cls, RDF.type, OWL.Class))
    g.add((dp, RDF.type, OWL.DatatypeProperty))
    g.add((dp, RDFS.domain, cls))

    result = s12f.apply(g, StepContext(domain_ns=STEEL))
    assert result.stats.get("pruned_sourceless_names", []) == []
    assert (dp, RDF.type, OWL.DatatypeProperty) in g


def test_keeps_dp_with_source_annotation(tmp_path, monkeypatch):
    """출처 표기가 있으면 domain 이 없어도 후보에 들지 않는다."""
    import config

    abox = tmp_path / "a_box.ttl"
    abox.write_text("# empty\n", encoding="utf-8")
    monkeypatch.setattr(config, "ABOX_PATH", str(abox))

    g = _new_graph()
    dp = URIRef(STEEL + "someValue")
    g.add((dp, RDF.type, OWL.DatatypeProperty))
    g.add((dp, DCTERMS.source, Literal("SOME_COL")))

    result = s12f.apply(g, StepContext(domain_ns=STEEL))
    assert result.stats["candidates"] == 0
    assert (dp, RDF.type, OWL.DatatypeProperty) in g


def test_holds_off_when_abox_missing(tmp_path, monkeypatch):
    """A-Box 가 없으면 사용 여부를 알 수 없으므로 제거를 보류한다."""
    import config

    monkeypatch.setattr(config, "ABOX_PATH", str(tmp_path / "missing.ttl"))

    g, orphan, _ = _orphan_dp_graph()
    result = s12f.apply(g, StepContext(domain_ns=STEEL))

    assert result.stats["orphan_dps_pruned"] == 0
    assert "skipped" in result.stats
    assert (orphan, RDF.type, OWL.DatatypeProperty) in g


def test_can_be_disabled_by_env(tmp_path, monkeypatch):
    """환경변수로 끌 수 있다."""
    monkeypatch.setenv("TBOX_PRUNE_ORPHAN_DPS", "false")
    g, orphan, _ = _orphan_dp_graph()
    before = len(g)

    result = s12f.apply(g, StepContext(domain_ns=STEEL))
    assert "skipped" in result.stats
    assert len(g) == before


# ── 병합 부산물: 단일값 주석 상충 해소 ────────────────────────────────


def test_annotation_conflict_aligns_with_parent():
    """상충하는 OntoClean 주석은 **부모와 정합한 값** 을 남긴다.

    클래스 병합 시 두 클래스가 서로 다른 메타를 갖고 있었다면 병합 후 한 클래스가
    두 값을 동시에 갖는다 (실측: FacilityOperation identity ['+I','-I']).
    강한 값(+I) 을 무조건 남기면 -I 부모와의 조합이 OntoClean C3 위반이 되므로,
    부모가 선언한 값과 맞춘다.
    """
    from tools.validation_support.common import resolve_single_valued_annotations

    ns1 = Namespace(STEEL.replace("#", "-ontoclean#"))
    g = _new_graph()
    parent = URIRef(STEEL + "MasterData")
    child = URIRef(STEEL + "FacilityOperation")
    g.add((parent, RDF.type, OWL.Class))
    g.add((child, RDF.type, OWL.Class))
    g.add((child, RDFS.subClassOf, parent))
    g.add((parent, ns1.identity, Literal("-I")))
    g.add((child, ns1.identity, Literal("+I")))     # 병합 부산물
    g.add((child, ns1.identity, Literal("-I")))

    result = resolve_single_valued_annotations(g, {child})
    assert len(result) == 1
    assert result[0]["kept"] == "-I"
    assert result[0]["reason"] == "parent_aligned"
    assert [str(v) for v in g.objects(child, ns1.identity)] == ["-I"]


def test_annotation_conflict_prefers_weaker_without_parent():
    """부모 선언이 없으면 약한 주장(-) 을 남긴다 (없는 기준을 주장하지 않음)."""
    from tools.validation_support.common import resolve_single_valued_annotations

    ns1 = Namespace(STEEL.replace("#", "-ontoclean#"))
    g = _new_graph()
    cls = URIRef(STEEL + "Standalone")
    g.add((cls, RDF.type, OWL.Class))
    g.add((cls, ns1.rigidity, Literal("+R")))
    g.add((cls, ns1.rigidity, Literal("-R")))

    result = resolve_single_valued_annotations(g, {cls})
    assert result[0]["kept"] == "-R"
    assert result[0]["reason"] == "weaker_claim"


def test_annotation_single_value_untouched():
    """값이 하나면 건드리지 않는다."""
    from tools.validation_support.common import resolve_single_valued_annotations

    ns1 = Namespace(STEEL.replace("#", "-ontoclean#"))
    g = _new_graph()
    cls = URIRef(STEEL + "Order")
    g.add((cls, RDF.type, OWL.Class))
    g.add((cls, ns1.identity, Literal("-I")))
    before = len(g)

    assert resolve_single_valued_annotations(g, {cls}) == []
    assert len(g) == before


# ── 12f 유형 2: union domain 잉여 DP ───────────────────────────────────


def _union_domain_graph(*, with_substitutes: bool = True):
    """domain = MaterialA ∪ Order 인 DP + 각 클래스의 class-specific DP.

    실측 케이스 (2026-07-25): materialAQaGrade (MaterialA ∪ Order, GRADE_COL_1) 는 A-Box
    사용 0건인데 materialAGRADECOL1 13,339건 / orderGradeCol1 9,788건이 같은 컬럼을
    이미 담고 있었다.
    """
    from rdflib.collection import Collection

    g = _new_graph()
    materialA = URIRef(STEEL + "MaterialA")
    order = URIRef(STEEL + "Order")
    for cls in (materialA, order):
        g.add((cls, RDF.type, OWL.Class))

    union_dp = URIRef(STEEL + "materialAQaGrade")
    g.add((union_dp, RDF.type, OWL.DatatypeProperty))
    g.add((union_dp, DCTERMS.source, Literal("GRADE_COL_1")))
    union_class = BNode()
    g.add((union_class, RDF.type, OWL.Class))
    members = BNode()
    Collection(g, members, [materialA, order])
    g.add((union_class, OWL.unionOf, members))
    g.add((union_dp, RDFS.domain, union_class))

    if with_substitutes:
        for local, domain in (("materialAGRADECOL1", materialA), ("orderGradeCol1", order)):
            dp = URIRef(STEEL + local)
            g.add((dp, RDF.type, OWL.DatatypeProperty))
            g.add((dp, RDFS.domain, domain))
            g.add((dp, DCTERMS.source, Literal("GRADE_COL_1")))
    return g, union_dp


def test_prunes_redundant_union_domain_dp(tmp_path, monkeypatch):
    """멤버별 대체 DP 가 있고 자신은 미사용이면 union DP 를 제거한다."""
    import config

    abox = tmp_path / "a_box.ttl"
    abox.write_text(
        "steel-inst:s steel:materialAGRADECOL1 \"A\" .\n"
        "steel-inst:o steel:orderGradeCol1 \"A\" .\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "ABOX_PATH", str(abox))

    g, union_dp = _union_domain_graph()
    result = s12f.apply(g, StepContext(domain_ns=STEEL))

    assert result.stats["pruned_redundant_union_names"] == ["materialAQaGrade"]
    assert (union_dp, RDF.type, OWL.DatatypeProperty) not in g
    # 대체 DP 는 손대지 않는다 (데이터가 여기 있다)
    for local in ("materialAGRADECOL1", "orderGradeCol1"):
        assert (URIRef(STEEL + local), RDF.type, OWL.DatatypeProperty) in g
    # 목적지 없는 unionOf 리스트가 남지 않는다
    assert list(g.subjects(OWL.unionOf, None)) == []
    assert list(g.triples((None, RDF.first, None))) == []


def test_keeps_union_dp_without_substitute(tmp_path, monkeypatch):
    """대체 DP 가 없으면 union domain 이라도 보존한다 (지우면 컬럼이 사라진다)."""
    import config

    abox = tmp_path / "a_box.ttl"
    abox.write_text("# empty\n", encoding="utf-8")
    monkeypatch.setattr(config, "ABOX_PATH", str(abox))

    g, union_dp = _union_domain_graph(with_substitutes=False)
    result = s12f.apply(g, StepContext(domain_ns=STEEL))

    assert result.stats.get("pruned_redundant_union_names", []) == []
    assert (union_dp, RDF.type, OWL.DatatypeProperty) in g


def test_keeps_union_dp_that_abox_uses(tmp_path, monkeypatch):
    """대체 DP 가 있어도 union DP 에 값이 적재돼 있으면 보존한다."""
    import config

    abox = tmp_path / "a_box.ttl"
    abox.write_text(
        "steel-inst:s steel:materialAQaGrade \"A\" .\n", encoding="utf-8",
    )
    monkeypatch.setattr(config, "ABOX_PATH", str(abox))

    g, union_dp = _union_domain_graph()
    result = s12f.apply(g, StepContext(domain_ns=STEEL))

    assert result.stats["kept_in_use"] == ["materialAQaGrade"]
    assert (union_dp, RDF.type, OWL.DatatypeProperty) in g


def test_sourceless_dp_kept_when_columns_uncovered(tmp_path, monkeypatch):
    """미선언 CSV 컬럼이 남아 있으면 출처 없는 DP 를 제거하지 않는다.

    그 DP 가 미선언 컬럼을 담당할 의도였을 수 있다 — 지우면 컬럼이 영구히
    누락된다. 대신 dcterms:source 보강 대상으로 보고한다. 반대로 컬럼이 전부
    다른 DP 로 선언돼 있으면 담당 컬럼 없는 잉여이므로 제거한다.
    """
    import config

    abox = tmp_path / "a_box.ttl"
    abox.write_text("# empty\n", encoding="utf-8")
    monkeypatch.setattr(config, "ABOX_PATH", str(abox))
    _stub_csv_mapping(
        tmp_path, monkeypatch, {"T_FACT": ("Fact", ["COVERED_COL", "ORPHAN_COL"])},
    )

    g = _new_graph()
    cls = URIRef(STEEL + "Fact")
    g.add((cls, RDF.type, OWL.Class))
    # COVERED_COL 만 선언 → ORPHAN_COL 이 미선언으로 남는다
    declared = URIRef(STEEL + "factCoveredCol")
    g.add((declared, RDF.type, OWL.DatatypeProperty))
    g.add((declared, RDFS.domain, cls))
    g.add((declared, DCTERMS.source, Literal("COVERED_COL")))
    ghost = URIRef(STEEL + "factSomething")
    g.add((ghost, RDF.type, OWL.DatatypeProperty))
    g.add((ghost, RDFS.domain, cls))          # 출처 없음

    result = s12f.apply(g, StepContext(domain_ns=STEEL))

    assert result.stats.get("pruned_sourceless_names", []) == []
    assert "factSomething" in result.stats["sourceless_kept_uncovered_columns"]
    assert (ghost, RDF.type, OWL.DatatypeProperty) in g

    # 이제 ORPHAN_COL 까지 선언하면 factSomething 은 담당 컬럼 없는 잉여가 된다
    other = URIRef(STEEL + "factOrphanCol")
    g.add((other, RDF.type, OWL.DatatypeProperty))
    g.add((other, RDFS.domain, cls))
    g.add((other, DCTERMS.source, Literal("ORPHAN_COL")))

    result2 = s12f.apply(g, StepContext(domain_ns=STEEL))
    assert result2.stats["pruned_sourceless_names"] == ["factSomething"]
    assert (ghost, RDF.type, OWL.DatatypeProperty) not in g


def test_keeps_sourceless_dp_of_unmapped_class(tmp_path, monkeypatch):
    """CSV 에 매핑되지 않은 클래스의 DP 는 출처가 없어도 보존한다.

    암묵지(S5) 로 추가된 클래스나 추론 전용 클래스는 CSV 가 없는 것이 정상이다.
    컬럼 목록을 모르면 "담당 컬럼이 없다" 를 증명할 수 없으므로, 출처가 없다는
    이유만으로 지우면 도메인 지식이 사라진다 (2026-07-25: 이 가드가 없어
    test_ontology_quality 의 tagId 등 6개 테스트가 깨졌다).
    """
    import config

    abox = tmp_path / "a_box.ttl"
    abox.write_text("# empty\n", encoding="utf-8")
    monkeypatch.setattr(config, "ABOX_PATH", str(abox))
    # 매핑에는 Fact 만 있고 TacitConcept 은 없다
    _stub_csv_mapping(tmp_path, monkeypatch, {"T_FACT": ("Fact", ["COL_A"])})

    g = _new_graph()
    tacit = URIRef(STEEL + "TacitConcept")
    g.add((tacit, RDF.type, OWL.Class))
    dp = URIRef(STEEL + "tacitInsight")
    g.add((dp, RDF.type, OWL.DatatypeProperty))
    g.add((dp, RDFS.domain, tacit))            # CSV 없음, 출처 없음

    result = s12f.apply(g, StepContext(domain_ns=STEEL))

    assert result.stats.get("pruned_sourceless_names", []) == []
    assert (dp, RDF.type, OWL.DatatypeProperty) in g
