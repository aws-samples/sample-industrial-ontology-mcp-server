"""step_12h — BFO 범주 충돌 제거가 **조상 선언자**까지 도달하는지 고정.

## 왜 이 테스트가 필요한가

처음 구현은 "그 클래스가 **직접** 선언한 IOF 부모만 제거" 였다. 충돌 부모를 2~3홉
위 조상이 선언한 경우 **아무도 처리하지 않는다** — 그 조상 자신은 자기 조상에 정보
계보가 없어 판정에서 빠지기 때문이다. 실측 (2026-08-18):

    EquipmentManagement ⊑ iof:MaterialArtifact          ← 물질 (선언자)
     ├ EquipmentAsset ─┬ TagMaster       ⊑ MasterData (정보)
     │                 └ EquipmentMaster ⊑ MasterData (정보)
     └ EquipmentEvent ─┬ EquipmentStatus ⊑ TransactionRecord (정보)
                       └ AlarmEvents     ⊑ iof:Event (occurrent)

HermiT 은 4개를 unsat 으로 판정했는데 이 스텝은 **0건 제거**를 보고했다. 카운터가
0 인 것과 결함이 없는 것은 다르다 — 그 구분을 여기서 고정한다.
"""
from __future__ import annotations

import os

import pytest
from rdflib import RDFS, Graph, URIRef

from tools.quality_steps import step_12h_iof_category_conflict as step
from tools.quality_steps._base import StepContext

S = "http://example.com/steel-ontology#"
IOF = "https://spec.industrialontologies.org/ontology/construct/"
MATERIAL = URIRef(IOF + "MaterialArtifact")
INFO = URIRef(IOF + "DescriptiveInformationContentEntity")


def _reference_available() -> bool:
    import config

    base = getattr(config, "SOURCE_REFERENCE_DIR", None) or os.path.join(
        "data", "source", "reference",
    )
    if not os.path.isdir(base):
        return False
    ref = Graph()
    loaded = 0
    for fname in os.listdir(base):
        if fname.lower().endswith((".rdf", ".owl", ".ttl")):
            try:
                ref.parse(os.path.join(base, fname))
                loaded += 1
            except Exception:  # noqa: BLE001
                pass
    if not loaded:
        return False
    # 판정이 실제로 가능한지 — MaterialArtifact 가 material 축으로 잡히는가.
    return bool(list(ref.objects(MATERIAL, RDFS.subClassOf)))


class TestConflictDeclaredByAncestor:
    """충돌 부모를 조상이 선언한 경우도 끊는다 (원래 놓친 경로)."""

    def test_removes_conflict_declared_two_hops_up(self):
        if not _reference_available():
            pytest.skip("IOF/BFO reference 없음")
        g = Graph()
        # 조상이 물질 계보를 선언하고, 손자가 정보 계보를 갖는다.
        g.add((URIRef(S + "EquipmentManagement"), RDFS.subClassOf, MATERIAL))
        g.add((URIRef(S + "EquipmentAsset"), RDFS.subClassOf,
               URIRef(S + "EquipmentManagement")))
        g.add((URIRef(S + "TagMaster"), RDFS.subClassOf,
               URIRef(S + "EquipmentAsset")))
        g.add((URIRef(S + "TagMaster"), RDFS.subClassOf,
               URIRef(S + "MasterData")))
        g.add((URIRef(S + "MasterData"), RDFS.subClassOf, INFO))

        result = step.apply(g, StepContext(domain_ns=S))

        assert (URIRef(S + "EquipmentManagement"), RDFS.subClassOf, MATERIAL) not in g, (
            "조상이 선언한 충돌 부모가 남았다 — 손자 클래스가 unsat 이 된다"
        )
        assert result.stats["iof_category_conflicts_removed"] >= 1

    def test_preserves_the_information_lineage(self):
        """NEGATIVE: 유지해야 하는 정보 계보는 건드리지 않는다.

        "카운터 ≥ 1" 만 주장하면 양쪽 다 지우는 구현도 통과한다.
        """
        if not _reference_available():
            pytest.skip("IOF/BFO reference 없음")
        g = Graph()
        g.add((URIRef(S + "EquipmentManagement"), RDFS.subClassOf, MATERIAL))
        g.add((URIRef(S + "TagMaster"), RDFS.subClassOf,
               URIRef(S + "EquipmentManagement")))
        g.add((URIRef(S + "TagMaster"), RDFS.subClassOf, URIRef(S + "MasterData")))
        g.add((URIRef(S + "MasterData"), RDFS.subClassOf, INFO))

        step.apply(g, StepContext(domain_ns=S))

        assert (URIRef(S + "MasterData"), RDFS.subClassOf, INFO) in g
        assert (URIRef(S + "TagMaster"), RDFS.subClassOf,
                URIRef(S + "MasterData")) in g
        assert (URIRef(S + "TagMaster"), RDFS.subClassOf,
                URIRef(S + "EquipmentManagement")) in g, (
            "도메인 계층 간선을 지웠다 — IOF 부모만 끊어야 한다"
        )

    def test_leaves_pure_material_hierarchy_alone(self):
        """NEGATIVE: 정보 계보가 **없는** 순수 물질 계층은 정당하다."""
        if not _reference_available():
            pytest.skip("IOF/BFO reference 없음")
        g = Graph()
        g.add((URIRef(S + "PhysicalPlant"), RDFS.subClassOf, MATERIAL))
        g.add((URIRef(S + "Furnace"), RDFS.subClassOf, URIRef(S + "PhysicalPlant")))

        result = step.apply(g, StepContext(domain_ns=S))

        assert (URIRef(S + "PhysicalPlant"), RDFS.subClassOf, MATERIAL) in g
        assert result.stats["iof_category_conflicts_removed"] == 0

    def test_is_idempotent(self):
        if not _reference_available():
            pytest.skip("IOF/BFO reference 없음")
        g = Graph()
        g.add((URIRef(S + "EquipmentManagement"), RDFS.subClassOf, MATERIAL))
        g.add((URIRef(S + "TagMaster"), RDFS.subClassOf,
               URIRef(S + "EquipmentManagement")))
        g.add((URIRef(S + "TagMaster"), RDFS.subClassOf, URIRef(S + "MasterData")))
        g.add((URIRef(S + "MasterData"), RDFS.subClassOf, INFO))
        step.apply(g, StepContext(domain_ns=S))
        snapshot = set(g)
        second = step.apply(g, StepContext(domain_ns=S))
        assert set(g) == snapshot
        assert second.stats["iof_category_conflicts_removed"] == 0


class TestDeployedTboxHasNoCategoryConflict:
    def test_no_domain_class_mixes_information_and_material(self):
        """실측 회귀: 배포 T-Box 에 정보/물질 혼합 계보가 없다.

        이 조건이 깨지면 HermiT unsat 이 나고, unsat 클래스에 대해 추론기는
        **아무것도** 말하지 않는다 (조용한 전손).
        """
        import config

        if not (os.path.exists(config.TBOX_PATH) and _reference_available()):
            pytest.skip("배포 T-Box 또는 reference 없음")
        g = Graph()
        g.parse(config.TBOX_PATH, format="turtle")

        def ancestors(node: URIRef, seen: set | None = None) -> set:
            seen = seen or set()
            if node in seen:
                return set()
            seen.add(node)
            out = {node}
            for p in g.objects(node, RDFS.subClassOf):
                if isinstance(p, URIRef):
                    out |= ancestors(p, seen)
            return out

        bad: list[str] = []
        for cls in {s for s in g.subjects(RDFS.subClassOf, None)
                    if isinstance(s, URIRef) and str(s).startswith(S)}:
            anc = {str(a) for a in ancestors(cls)}
            if MATERIAL and str(MATERIAL) in anc and any(
                "InformationContentEntity" in a for a in anc
            ):
                bad.append(str(cls)[len(S):])
        assert bad == [], (
            f"정보/물질 계보를 동시에 상속하는 클래스가 있다 (unsat): "
            f"{sorted(bad)[:6]}"
        )
