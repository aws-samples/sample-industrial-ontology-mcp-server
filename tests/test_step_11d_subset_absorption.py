"""Step 11d pass-2 (스텁 흡수) 의 **보존 방향** 회귀 가드.

배경 (2026-08-08 실측): pass 2 는 출처 컬럼 집합이 진부분집합인 클래스를 흡수한다.
기존 테스트 10건은 pass 1 (완전 일치) 만 덮고 있었고, pass 2 는 어느 방향도
고정돼 있지 않았다 — 특히 **합치면 안 되는 것을 합치지 않는지** 가 비어 있었다.

판정이 "선언된" ``dcterms:source`` 집합에만 의존하는 것이 위험의 근원이다. 모든
DP 가 출처 표기를 갖는 것은 아니어서 (출하 T-Box 는 DP 295개 중 표기 0개),
표기가 부분적이면 컬럼이 많은 테이블도 1~2컬럼짜리 **인공 스텁** 으로 보인다. 그
상태에서 ``ITEM_CODE``/``PLANT_CODE`` 처럼 ERP 에 보편적인 컬럼 하나를 공유하면
서로 무관한 테이블이 부분집합으로 판정된다.

실측: 이 리포 40개 CSV 를 전수 헤더로 보면 해당 쌍이 0개지만, 표기 커버리지 40%
를 가정하면 12쌍이 조건을 만족하고 그중 ``Inventory_Status ⊂ Item_Master`` 같은
실제로 별개인 테이블이 병합 대상이 된다. 병합되면 흡수된 클래스가 T-Box 에서
사라지고 그 클래스를 domain/range 로 쓰던 OP 가 생존자로 조용히 재조준된다.

기본 활성 스텝이라 (``TBOX_MERGE_DUPLICATE_SOURCE_CLASSES`` default true) 로그
WARNING 한 줄 외에는 아무 경고도 없다.
"""
from __future__ import annotations

import pytest
from rdflib import RDF, RDFS, Graph, Literal, URIRef
from rdflib.namespace import OWL

from tools.quality_steps import step_11d_duplicate_source_class_merge as s11d
from tools.quality_steps._base import StepContext

STEEL = "http://example.com/steel-ontology#"
DCTERMS_SOURCE = URIRef("http://purl.org/dc/terms/source")


def _c(name: str) -> URIRef:
    return URIRef(STEEL + name)


def _graph_with(classes: dict[str, list[str]]) -> Graph:
    """{ClassName: [SOURCE_COLUMN, ...]} 로 T-Box 단편을 만든다."""
    g = Graph()
    for cls, columns in classes.items():
        g.add((_c(cls), RDF.type, OWL.Class))
        for idx, column in enumerate(columns):
            dp = _c(f"{cls[:1].lower()}{cls[1:]}Col{idx}")
            g.add((dp, RDF.type, OWL.DatatypeProperty))
            g.add((dp, RDFS.domain, _c(cls)))
            g.add((dp, DCTERMS_SOURCE, Literal(column)))
    return g


def _apply(g: Graph):
    return s11d.apply(g, StepContext(domain_ns=STEEL))


def _declared(g: Graph) -> set[str]:
    return {str(c)[len(STEEL):] for c in g.subjects(RDF.type, OWL.Class)}


@pytest.fixture
def distinct_tables(monkeypatch):
    """두 클래스가 매핑상 **서로 다른 테이블** 로 등록된 상태."""
    monkeypatch.setattr(
        s11d, "_class_to_table",
        lambda: {"ShippingLane": "Shipping_Lane", "ProductMaster": "Product_Master"},
    )


@pytest.fixture
def same_table(monkeypatch):
    """두 클래스가 같은 테이블(또는 미등록)인 상태 — 흡수가 정당한 경우."""
    monkeypatch.setattr(s11d, "_class_to_table", lambda: {})


def test_distinct_tables_sharing_columns_are_not_merged(distinct_tables):
    """THE REGRESSION: 별개 테이블이 공유 컬럼 때문에 병합되면 안 된다.

    ``ShippingLane`` 이 관계를 하나 갖고 있으면 op_degree 가 이겨서 **컬럼이 많은
    ``ProductMaster`` 가 흡수돼 사라졌다** (수정 전 실측).
    """
    g = _graph_with({
        "ShippingLane": ["PLANT_CODE", "PRODUCT_ID"],
        "ProductMaster": ["PLANT_CODE", "PRODUCT_ID", "GRADE", "WEIGHT", "LENGTH"],
        "Customer": ["PARTY_KEY", "PARTY_LABEL", "REGION", "TIER"],
    })
    # 스텁이 KG 허브 — 이 조건에서 정본 선택이 뒤집혔다.
    g.add((_c("laneServes"), RDF.type, OWL.ObjectProperty))
    g.add((_c("laneServes"), RDFS.domain, _c("ShippingLane")))
    g.add((_c("laneServes"), RDFS.range, _c("Customer")))

    result = _apply(g)

    assert result.stats["duplicate_classes_merged"] == 0, (
        f"별개 테이블이 병합됐다: {result.stats.get('merge_map')}"
    )
    assert {"ShippingLane", "ProductMaster"} <= _declared(g), (
        "테이블 하나가 T-Box 에서 사라졌다 — A-Box 가 그 클래스 인스턴스를 못 만든다"
    )
    assert result.stats["skipped_distinct_tables"], "보류 사유가 stats 에 남아야 한다"


def test_junction_table_not_absorbed_into_fact_table(monkeypatch):
    """정션 테이블은 팩트 테이블의 스텁이 아니다 (컬럼이 겹치는 것이 정상)."""
    monkeypatch.setattr(
        s11d, "_class_to_table",
        lambda: {"ItemSupplierMap": "Item_Supplier_Map", "PurchaseOrder": "Purchase_Order"},
    )
    g = _graph_with({
        "ItemSupplierMap": ["ITEM_CODE", "VENDOR_KEY"],
        "PurchaseOrder": ["ITEM_CODE", "VENDOR_KEY", "QTY", "PRICE", "ORDER_DATE"],
    })
    result = _apply(g)
    assert result.stats["duplicate_classes_merged"] == 0
    assert {"ItemSupplierMap", "PurchaseOrder"} <= _declared(g)


def test_pass1_does_not_merge_across_registered_tables(monkeypatch):
    """pass 1 (완전 일치) 도 등록 테이블이 다르면 병합하지 않는다.

    출처 표기가 부분적이면 서로 다른 테이블의 **선언 집합이 우연히 같아질** 수 있다.
    """
    monkeypatch.setattr(
        s11d, "_class_to_table",
        lambda: {"InventoryStatus": "Inventory_Status", "ItemMaster": "Item_Master"},
    )
    g = _graph_with({
        "InventoryStatus": ["ITEM_CODE", "WAREHOUSE_CODE"],
        "ItemMaster": ["ITEM_CODE", "WAREHOUSE_CODE"],
    })
    result = _apply(g)
    assert result.stats["duplicate_classes_merged"] == 0, (
        f"pass 1 이 별개 테이블을 합쳤다: {result.stats.get('merge_map')}"
    )
    assert {"InventoryStatus", "ItemMaster"} <= _declared(g)
    assert result.stats["skipped_distinct_tables_pass1"]


def test_genuine_stub_is_absorbed(same_table):
    """POSITIVE: 같은 테이블을 스텁/구체로 이중 모델링한 것은 흡수한다 (기능 보존)."""
    g = _graph_with({
        "FacilityOperation": ["FACILITY_CD"],
        "FacilityEquipmentMaster": [
            "FACILITY_CD", "OPER_KIND_FLAG", "SITE_CODE", "PROC_NAME",
        ],
    })
    result = _apply(g)
    assert result.stats["duplicate_classes_merged"] == 1, (
        "정당한 스텁 흡수가 막혔다 — 가드가 과하게 넓다"
    )
    assert result.stats["stub_absorptions"], "흡수 내역이 기록돼야 한다"
    assert len(_declared(g)) == 1


def test_hierarchy_pair_is_preserved(same_table):
    """부모-자식 계층은 흡수하지 않는다 (자식이 부모 컬럼 일부만 재선언할 수 있다)."""
    g = _graph_with({
        "MaterialB": ["MATERIAL_NO"],
        "MaterialA": ["MATERIAL_NO", "GRADE_CD", "WEIGHT", "LENGTH"],
    })
    g.add((_c("MaterialB"), RDFS.subClassOf, _c("MaterialA")))
    result = _apply(g)
    assert result.stats["duplicate_classes_merged"] == 0
    assert {"MaterialA", "MaterialB"} <= _declared(g)


def test_unmapped_classes_still_absorbed(same_table):
    """매핑에 없는 클래스는 기존 판정을 유지한다 (가드가 no-op)."""
    g = _graph_with({
        "StubThing": ["SHARED_CD"],
        "ConcreteThing": ["SHARED_CD", "A_COL", "B_COL", "C_COL"],
    })
    result = _apply(g)
    assert result.stats["duplicate_classes_merged"] == 1
