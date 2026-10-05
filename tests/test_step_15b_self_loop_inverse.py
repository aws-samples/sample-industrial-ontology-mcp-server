"""Step 15b self-loop 제거가 **이름만 같은 정상 프로퍼티** 를 지우지 않는지 회귀 가드.

배경 (2026-08-08 실측): ``_remove_self_loop_fk_ops`` 는 ``has{Class}`` 가
domain==range 인 self-loop 이면 제거하고, 이어서 ``is{Class}Of`` 를 **이름으로
재구성해** 함께 지웠다. 그 프로퍼티가 self-loop 인지, 애초에 관련이 있는지조차
확인하지 않았다.

``is{Class}Of`` 는 이 리포의 표준 inverse 명명 규칙이라 **domain≠range 인 완전히
정상적인 관계** 가 같은 이름을 갖는 일이 흔하다. 실측 재현: ``isEquipmentOf``
(domain=Equipment, range=Plant, 한글 라벨 보유) 가 ``hasEquipment`` self-loop 하나
때문에 선언·range·라벨까지 통째로 삭제됐다. A-Box 가 그 관계로 적재한 트리플은
T-Box 에 선언이 없는 orphan OP 가 된다.

이 모듈은 358 LOC 에 ``g.remove`` 6곳인데 직접 테스트가 0건이었다. 유일한 커버리지는
``improve_tbox`` 를 통과하며 카운터가 ``>= 1`` 인지 보는 3건뿐이었다 — **무엇이
보존돼야 하는지** 를 확인하는 테스트가 없었다.
"""
from __future__ import annotations

import pytest
from rdflib import RDF, RDFS, Graph, Literal, URIRef
from rdflib.namespace import OWL

from tools.quality_steps.step_15b_fk_op_autocreate import _remove_self_loop_fk_ops

STEEL = "http://example.com/steel-ontology#"


def _c(name: str) -> URIRef:
    return URIRef(STEEL + name)


def _base_graph() -> Graph:
    """``hasEquipment`` self-loop (제거 대상) + Equipment/Plant 클래스."""
    g = Graph()
    for cls in ("Equipment", "Plant"):
        g.add((_c(cls), RDF.type, OWL.Class))
    g.add((_c("hasEquipment"), RDF.type, OWL.ObjectProperty))
    g.add((_c("hasEquipment"), RDFS.domain, _c("Equipment")))
    g.add((_c("hasEquipment"), RDFS.range, _c("Equipment")))
    return g


def _add_inverse(g: Graph, domain: str, range_: str, *, declared: bool = False) -> None:
    g.add((_c("isEquipmentOf"), RDF.type, OWL.ObjectProperty))
    g.add((_c("isEquipmentOf"), RDFS.domain, _c(domain)))
    g.add((_c("isEquipmentOf"), RDFS.range, _c(range_)))
    g.add((_c("isEquipmentOf"), RDFS.label, Literal("설비가 속한 공장", lang="ko")))
    if declared:
        g.add((_c("hasEquipment"), OWL.inverseOf, _c("isEquipmentOf")))


def _declared(g: Graph, name: str) -> bool:
    return (_c(name), RDF.type, OWL.ObjectProperty) in g


def test_name_matching_but_valid_property_is_preserved():
    """THE REGRESSION: domain≠range 인 정상 프로퍼티는 이름이 같아도 보존한다."""
    g = _base_graph()
    _add_inverse(g, "Equipment", "Plant")          # 정상 관계 (self-loop 아님)

    stats = _remove_self_loop_fk_ops(g, STEEL)

    assert stats["self_loop_ops_removed"] == 1, "self-loop 자체는 제거돼야 한다"
    assert not _declared(g, "hasEquipment")
    assert _declared(g, "isEquipmentOf"), (
        "이름만 같은 정상 프로퍼티가 삭제됐다 — A-Box 트리플이 미선언 OP 가 된다"
    )
    # 부속 트리플까지 온전해야 한다 (라벨·range 를 잃으면 SPARQL·시각화가 깨진다)
    assert (_c("isEquipmentOf"), RDFS.range, _c("Plant")) in g
    assert list(g.objects(_c("isEquipmentOf"), RDFS.label)), "라벨이 사라졌다"


def test_inverse_that_is_itself_a_self_loop_is_removed():
    """POSITIVE: inverse 도 domain==range 면 함께 제거 (기능 보존)."""
    g = _base_graph()
    _add_inverse(g, "Equipment", "Equipment")

    stats = _remove_self_loop_fk_ops(g, STEEL)

    assert stats["self_loop_ops_removed"] == 1
    assert not _declared(g, "isEquipmentOf")


def test_declared_inverse_of_the_self_loop_is_removed():
    """POSITIVE: self-loop 의 ``owl:inverseOf`` 로 명시 선언됐으면 제거.

    판정이 op 트리플 삭제 **이전** 에 일어나야 한다 — 나중에 보면
    ``owl:inverseOf`` 링크가 이미 사라져 근거를 잃는다 (개발 중 실제로 겪었다).
    """
    g = _base_graph()
    _add_inverse(g, "Equipment", "Plant", declared=True)

    stats = _remove_self_loop_fk_ops(g, STEEL)

    assert stats["self_loop_ops_removed"] == 1
    assert not _declared(g, "isEquipmentOf")


def test_non_self_loop_has_property_is_untouched():
    """domain≠range 인 ``has*`` 는 애초에 제거 대상이 아니다."""
    g = Graph()
    for cls in ("Equipment", "Plant"):
        g.add((_c(cls), RDF.type, OWL.Class))
    g.add((_c("hasEquipment"), RDF.type, OWL.ObjectProperty))
    g.add((_c("hasEquipment"), RDFS.domain, _c("Plant")))
    g.add((_c("hasEquipment"), RDFS.range, _c("Equipment")))
    _add_inverse(g, "Equipment", "Plant")

    stats = _remove_self_loop_fk_ops(g, STEEL)

    assert stats["self_loop_ops_removed"] == 0
    assert _declared(g, "hasEquipment") and _declared(g, "isEquipmentOf")


@pytest.mark.parametrize("other_ns", ["http://elsewhere.org/x#"])
def test_foreign_namespace_property_is_untouched(other_ns):
    """남의 네임스페이스 프로퍼티는 건드리지 않는다."""
    g = _base_graph()
    foreign = URIRef(other_ns + "hasEquipment")
    g.add((foreign, RDF.type, OWL.ObjectProperty))
    g.add((foreign, RDFS.domain, URIRef(other_ns + "Equipment")))
    g.add((foreign, RDFS.range, URIRef(other_ns + "Equipment")))

    _remove_self_loop_fk_ops(g, STEEL)

    assert (foreign, RDF.type, OWL.ObjectProperty) in g
