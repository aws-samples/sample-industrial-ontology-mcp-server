"""출처 컬럼 집합이 동일한 중복 클래스 병합(Step 11d) 회귀 가드.

배경 (2026-07-25 실측): 같은 CSV 를 파일명만 달리한 사본으로 두면
(``Facility_Master_A.csv`` / ``Facility_Master_B.csv`` — md5 동일)
Multi-Agent Architect 가 같은 실체에 클래스를 두 벌 만들었다:

    FacilityEquipmentMaster   DP 9개 (dcterms:source = {FACILITY_CD, ...})
    ProductionProcessMaster   DP 9개 (동일 집합)
    + 둘을 잇는 hasFacilityEquipmentMaster / isFacilityEquipmentMasterOf
      — 같은 데이터끼리의 참조라 실데이터로 채울 수 없고 사용 0건

근본 원인은 ``tools.common.dedupe_identical_csvs`` 로 S2/S7 양쪽에서 막았고,
이 단계는 이미 오염된 T-Box 복구 + 다른 경로 재발 시의 안전망이다.
"""
from __future__ import annotations

from rdflib import OWL, RDF, RDFS, Literal, Namespace, URIRef

from domain.tbox_utils import _new_graph
from tools.quality_steps import step_11d_duplicate_source_class_merge as s11d
from tools.quality_steps._base import StepContext

STEEL = "http://example.com/steel-ontology#"
DCTERMS = Namespace("http://purl.org/dc/terms/")

COLUMNS = ["FACILITY_CD", "OPER_KIND_FLAG", "SITE_CODE"]


def _build_duplicate_graph():
    """같은 출처 컬럼 집합을 갖는 두 클래스 + 이들을 잇는 OP 를 만든다."""
    g = _new_graph()
    for cls, prefix in (
        ("FacilityEquipmentMaster", "facilityEquipmentMaster"),
        ("ProductionProcessMaster", "productionProcessMaster"),
    ):
        cls_uri = URIRef(STEEL + cls)
        g.add((cls_uri, RDF.type, OWL.Class))
        for column in COLUMNS:
            camel = "".join(p.capitalize() for p in column.split("_"))
            dp = URIRef(STEEL + prefix + camel)
            g.add((dp, RDF.type, OWL.DatatypeProperty))
            g.add((dp, RDFS.domain, cls_uri))
            g.add((dp, DCTERMS.source, Literal(column)))
    # 두 클래스를 잇는 관계 — 병합 후 자기참조가 되어 무의미해진다.
    fwd = URIRef(STEEL + "hasFacilityEquipmentMaster")
    g.add((fwd, RDF.type, OWL.ObjectProperty))
    g.add((fwd, RDFS.domain, URIRef(STEEL + "ProductionProcessMaster")))
    g.add((fwd, RDFS.range, URIRef(STEEL + "FacilityEquipmentMaster")))
    return g


def test_merges_classes_with_identical_source_columns():
    """출처 집합이 같은 두 클래스가 하나로 합쳐진다."""
    g = _build_duplicate_graph()
    result = s11d.apply(g, StepContext(domain_ns=STEEL))

    assert result.stats["duplicate_classes_merged"] == 1
    assert result.stats["merge_map"] == {
        "ProductionProcessMaster": "FacilityEquipmentMaster",
    }
    assert (URIRef(STEEL + "ProductionProcessMaster"), RDF.type, OWL.Class) not in g
    assert (URIRef(STEEL + "FacilityEquipmentMaster"), RDF.type, OWL.Class) in g


def test_merges_duplicate_dps_so_only_one_remains_per_column():
    """클래스 병합 후 같은 컬럼을 가리키는 DP 가 하나만 남는다.

    step_23(중복 DP 통합)은 라벨 기준이라 이 케이스를 잡지 못한다 — 11d 가
    출처 컬럼 기준으로 직접 통합해야 한다.
    """
    g = _build_duplicate_graph()
    s11d.apply(g, StepContext(domain_ns=STEEL))

    canonical = URIRef(STEEL + "FacilityEquipmentMaster")
    remaining = [
        str(dp)[len(STEEL):]
        for dp in g.subjects(RDF.type, OWL.DatatypeProperty)
        if canonical in set(g.objects(dp, RDFS.domain))
    ]
    assert len(remaining) == len(COLUMNS), f"DP 가 중복 남음: {sorted(remaining)}"
    # 정본 클래스의 접두를 가진 이름이 살아야 한다.
    assert all(n.startswith("facilityEquipmentMaster") for n in remaining)


def test_drops_ops_that_become_self_referential():
    """병합으로 domain=range 가 된 OP 는 제거한다 (의미 없는 자기참조)."""
    g = _build_duplicate_graph()
    result = s11d.apply(g, StepContext(domain_ns=STEEL))

    assert "hasFacilityEquipmentMaster" in result.stats[
        "self_referential_ops_dropped"
    ]
    assert (
        URIRef(STEEL + "hasFacilityEquipmentMaster"), RDF.type, OWL.ObjectProperty,
    ) not in g


def test_preserves_intentional_recursive_ops():
    """TransitiveProperty / SymmetricProperty 는 자기참조여도 보존한다."""
    g = _build_duplicate_graph()
    follow = URIRef(STEEL + "followedBy")
    canonical = URIRef(STEEL + "FacilityEquipmentMaster")
    g.add((follow, RDF.type, OWL.ObjectProperty))
    g.add((follow, RDF.type, OWL.TransitiveProperty))
    g.add((follow, RDFS.domain, canonical))
    g.add((follow, RDFS.range, canonical))

    result = s11d.apply(g, StepContext(domain_ns=STEEL))
    assert "followedBy" not in result.stats["self_referential_ops_dropped"]
    assert (follow, RDF.type, OWL.ObjectProperty) in g


def test_does_not_merge_classes_with_different_sources():
    """출처 집합이 다르면 이름이 비슷해도 병합하지 않는다."""
    g = _new_graph()
    for cls, column in (("ClassA", "COL_ONE"), ("ClassB", "COL_TWO")):
        cls_uri = URIRef(STEEL + cls)
        g.add((cls_uri, RDF.type, OWL.Class))
        for extra in (column, "SHARED_COL"):
            dp = URIRef(STEEL + cls.lower() + extra.replace("_", ""))
            g.add((dp, RDF.type, OWL.DatatypeProperty))
            g.add((dp, RDFS.domain, cls_uri))
            g.add((dp, DCTERMS.source, Literal(extra)))

    result = s11d.apply(g, StepContext(domain_ns=STEEL))
    assert result.stats["duplicate_classes_merged"] == 0
    assert (URIRef(STEEL + "ClassA"), RDF.type, OWL.Class) in g
    assert (URIRef(STEEL + "ClassB"), RDF.type, OWL.Class) in g


def test_ignores_classes_without_source_annotations():
    """출처 표기가 없으면 판별 근거가 없어 건드리지 않는다."""
    g = _new_graph()
    for cls in ("NoSourceA", "NoSourceB"):
        cls_uri = URIRef(STEEL + cls)
        g.add((cls_uri, RDF.type, OWL.Class))
        dp = URIRef(STEEL + cls.lower() + "Value")
        g.add((dp, RDF.type, OWL.DatatypeProperty))
        g.add((dp, RDFS.domain, cls_uri))

    result = s11d.apply(g, StepContext(domain_ns=STEEL))
    assert result.stats["duplicate_classes_merged"] == 0


def test_can_be_disabled_by_env(monkeypatch):
    """환경변수로 끌 수 있다 (감사·비교 실행용)."""
    monkeypatch.setenv("TBOX_MERGE_DUPLICATE_SOURCE_CLASSES", "false")
    g = _build_duplicate_graph()
    before = len(g)

    result = s11d.apply(g, StepContext(domain_ns=STEEL))
    assert "skipped" in result.stats
    assert len(g) == before


# ── 근본 예방: 사본 CSV 자체를 파이프라인 입력에서 제거 ────────────────


def test_dedupe_keeps_one_of_identical_csvs(tmp_path):
    """내용이 같은 CSV 는 하나만 채택하고, 사전순 첫 번째를 정본으로 삼는다."""
    from tools.common import dedupe_identical_csvs

    payload = "COL_A,COL_B\n1,2\n"
    alpha = tmp_path / "Alpha_Table.csv"
    zulu = tmp_path / "Zulu_Copy.csv"
    alpha.write_text(payload, encoding="utf-8")
    zulu.write_text(payload, encoding="utf-8")

    kept, skipped = dedupe_identical_csvs(sorted([str(alpha), str(zulu)]))
    assert [p.split("/")[-1] for p in kept] == ["Alpha_Table.csv"]
    assert skipped == {"Zulu_Copy": "Alpha_Table"}


def test_dedupe_prefers_mapped_table_as_canonical(tmp_path):
    """table_class_mapping 에 등록된 쪽이 사전순을 이기고 정본이 된다."""
    from tools.common import dedupe_identical_csvs

    payload = "COL_A,COL_B\n1,2\n"
    alpha = tmp_path / "Alpha_Table.csv"
    zulu = tmp_path / "Zulu_Copy.csv"
    alpha.write_text(payload, encoding="utf-8")
    zulu.write_text(payload, encoding="utf-8")

    kept, skipped = dedupe_identical_csvs(
        sorted([str(alpha), str(zulu)]), preferred_tables={"Zulu_Copy"},
    )
    assert [p.split("/")[-1] for p in kept] == ["Zulu_Copy.csv"]
    assert skipped == {"Alpha_Table": "Zulu_Copy"}


def test_dedupe_keeps_distinct_csvs(tmp_path):
    """내용이 다르면 모두 유지한다 (오탐 방지)."""
    from tools.common import dedupe_identical_csvs

    one = tmp_path / "One.csv"
    two = tmp_path / "Two.csv"
    one.write_text("COL_A\n1\n", encoding="utf-8")
    two.write_text("COL_A\n2\n", encoding="utf-8")

    kept, skipped = dedupe_identical_csvs(sorted([str(one), str(two)]))
    assert len(kept) == 2
    assert skipped == {}
