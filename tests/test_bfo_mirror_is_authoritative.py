"""동봉 BFO 미러가 **탐지 능력을 갖춘 진본**인지 고정.

## 왜 이 테스트가 필요한가

`onto_path` 로컬 미러를 등록해 `owl:imports` 를 오프라인 해석하게 만든 순간,
**탐지기 하나가 조용히 죽었다**. owlready2 는 `onto_path` 를 **IRI 의 마지막 경로
조각 = 파일명**으로만 매칭하고 버전을 보지 않는다. 그래서 IOF Core 가 요구하는
`obo/bfo/2020/bfo.owl` 요청에 동봉 `bfo.owl` 이 무엇이든 대입된다.

실측 2026-08-18 (진본 교체 전, 동봉본은 BFO **1.1** 563 트리플):

| 조건 | 원인 재주입 시 unsat |
|------|---------------------|
| imports 가 네트워크로 해석 (fix 전) | **4** (AlarmEvents/EquipmentMaster/EquipmentStatus/TagMaster) |
| onto_path + BFO 1.1 스텁 (fix 후) | **0** ← 탐지기 사망 |
| onto_path + 진짜 BFO 2020 | **4** ← 복구 |

BFO 1.1 은 `snap#`/`span#` 레거시 IRI 만 쓰고 숫자 `BFO_*` 가 **0개**라, 84개
`disjointWith` 가 우리 그래프와 접점이 없어 영구 무발화였다. 즉 "추론기가 열렸다" 가
"공리를 얻었다" 를 뜻하지 않는다 — 이 리포가 반복해 겪은 실패 양식
(`project_gates_scored_declarations_not_effects`) 의 새 사례를 **내가 직접 만들었다**.

Core.rdf 자체의 `disjointWith` 쌍으로 만든 control probe 는 스텁으로도 unsat 1 을
낸다. 그래서 "탐지 경로가 죽었다" 가 아니라 **BFO 축만** 죽어 있었다 — 카운터나
consistency 플래그로는 절대 구별되지 않는다.
"""
from __future__ import annotations

import os
import re

import pytest
from rdflib import OWL, RDF, RDFS, Graph, URIRef

_OBO = "http://purl.obolibrary.org/obo/"
#: step_12h 의 축 판정이 의존하는 IRI — 미러에 이것이 없으면 판정이 무발화한다.
_REQUIRED_BFO_TERMS = (
    "BFO_0000002",  # continuant
    "BFO_0000003",  # occurrent
    "BFO_0000004",  # independent continuant
    "BFO_0000015",  # process
    "BFO_0000031",  # generically dependent continuant
)


def _mirror_path() -> str | None:
    import config

    base = getattr(config, "SOURCE_REFERENCE_DIR", None) or os.path.join(
        "data", "source", "reference",
    )
    p = os.path.join(base, "bfo.owl")
    return p if os.path.exists(p) else None


@pytest.fixture(scope="module")
def bfo() -> Graph:
    p = _mirror_path()
    if p is None:
        pytest.skip("BFO 미러 없음")
    g = Graph()
    g.parse(p)
    return g


class TestMirrorIsBfo2020NotALegacyStub:
    def test_defines_the_numeric_bfo_terms_step_12h_depends_on(self, bfo: Graph):
        """step_12h 의 축 판정 IRI 가 실제로 정의돼 있다.

        이 주장이 깨지면 `_axis()` 는 언제나 None 을 돌려주고 범주 충돌 제거가
        영구 no-op 이 된다 — 카운터 0 이 '결함 0' 으로 읽힌다.
        """
        missing = [
            t for t in _REQUIRED_BFO_TERMS
            if (URIRef(_OBO + t), None, None) not in bfo
        ]
        assert missing == [], (
            f"BFO 미러가 {missing} 를 정의하지 않는다 — step_12h 의 BFO 축 판정이 "
            "영구 무발화하고, 정보/물질 혼합 계보가 게이트를 그냥 통과한다"
        )

    def test_is_not_the_legacy_ifomis_namespace(self, bfo: Graph):
        """NEGATIVE: 레거시 BFO 1.x (ifomis snap#/span#) 가 아니다.

        파일명이 같아 owlready2 가 조용히 대입하므로, 이름이 아니라 **내용**으로
        판정해야 한다.
        """
        onts = {str(s) for s in bfo.subjects(RDF.type, OWL.Ontology)}
        assert not any(
            o.startswith(("http://www.ifomis.org/", "https://www.ifomis.org/"))
            for o in onts
        ), (
            f"레거시 BFO 1.x 가 미러에 있다 (owl:Ontology={onts}) — 숫자 BFO_* 가 "
            "없어 배타성 공리가 우리 그래프에 닿지 않는다"
        )
        numeric = sum(
            1 for s in set(bfo.subjects()) if re.search(r"BFO_\d+", str(s))
        )
        assert numeric >= 50, (
            f"숫자 BFO_* subject 가 {numeric}개뿐 — 스텁이 대입된 상태다"
        )

    def test_carries_the_continuant_occurrent_exclusivity(self, bfo: Graph):
        """배타성 공리가 **실제로** 있다 (선언이 아니라 효과의 전제).

        disjointness 가 없으면 정보/물질 혼합은 unsat 이 되지 않는다.
        """
        groups = list(bfo.subjects(RDF.type, OWL.AllDisjointClasses))
        pairs = list(bfo.triples((None, OWL.disjointWith, None)))
        assert groups or pairs, "미러에 배타성 공리가 전무하다"
        # 그 공리가 숫자 BFO_* 를 상대로 서술돼야 우리 그래프에 닿는다.
        touched = {
            str(o) for _, _, o in pairs if re.search(r"BFO_\d+", str(o))
        }
        for grp in groups:
            for _, _, lst in bfo.triples((grp, OWL.members, None)):
                touched |= {
                    str(x) for x in bfo.objects(lst, None)
                    if re.search(r"BFO_\d+", str(x))
                }
        assert touched, (
            "배타성 공리가 숫자 BFO_* 를 전혀 언급하지 않는다 — 접점이 없어 "
            "영구 무발화한다"
        )

    def test_the_terms_our_tbox_reaches_are_defined_locally(self, bfo: Graph):
        """T-Box → IOF → BFO 로 도달하는 IRI 가 미러에 정의돼 있다.

        도달은 하는데 정의가 없으면 고립 리프이고, 그 침묵이 통과로 읽힌다.
        """
        import config

        base = os.path.dirname(_mirror_path() or "")
        if not (os.path.exists(config.TBOX_PATH) and base):
            pytest.skip("T-Box 또는 미러 없음")
        tbox = Graph()
        tbox.parse(config.TBOX_PATH, format="turtle")
        ref = Graph()
        for f in ("Core.rdf", "Maintenance.rdf", "SupplyChain.rdf"):
            p = os.path.join(base, f)
            if os.path.exists(p):
                ref.parse(p)

        def ancestors(node: URIRef, g: Graph, seen: set | None = None) -> set:
            seen = seen or set()
            if node in seen:
                return set()
            seen.add(node)
            out = {node}
            for p in g.objects(node, RDFS.subClassOf):
                if isinstance(p, URIRef):
                    out |= ancestors(p, g, seen)
            return out

        iof_ns = "https://spec.industrialontologies.org/ontology/construct/"
        reached: set[str] = set()
        for o in tbox.objects(None, RDFS.subClassOf):
            if isinstance(o, URIRef) and str(o).startswith(iof_ns):
                reached |= {
                    str(a) for a in ancestors(o, ref) if str(a).startswith(_OBO)
                }
        if not reached:
            pytest.skip("T-Box 가 IOF 를 통해 BFO 에 닿지 않는다")
        undefined = sorted(
            r for r in reached if (URIRef(r), None, None) not in bfo
        )
        assert undefined == [], (
            f"도달하지만 미러에 정의되지 않은 BFO IRI: {undefined[:5]} — "
            "공리 없는 고립 리프다"
        )
