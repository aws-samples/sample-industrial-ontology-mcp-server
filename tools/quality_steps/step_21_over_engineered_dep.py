"""Step 21 — Mark over-engineered abstract classes as deprecated.

자식 클래스도 없고, 어떤 restriction 도 참조하지 않으며, disjoint group 멤버도
아니고, 어떤 property 의 domain/range 로도 사용 안 되는 phantom abstract class
를 ``owl:deprecated true`` + ``rdfs:seeAlso "over_engineered: ..."`` 로 마킹.

삭제하지 **않음** — tacit 이나 미래 CSV 가 채울 가능성 보존. 단지 downstream
시각화/검색이 이 노이즈 클래스를 필터링할 수 있게 한다.
"""
from __future__ import annotations

import logging

from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def apply(g: Graph, ctx: StepContext) -> StepResult:
    before = len(g)
    deprecated_count = 0
    undeprecated_count = 0
    error: str | None = None
    try:
        steel_str = ctx.domain_ns
        # **익명 클래스 표현식을 제외한다.** skolemize 된 ``Union_*`` /
        # ``owl:Restriction`` 은 IRI 가 있어도 명명 클래스가 아니다. 예전에는
        # 그것들이 "자식·용처 없음" 으로 판정돼 ``owl:deprecated`` 가 붙었고,
        # 실제로는 ``ManufacturingProcessStep owl:equivalentClass`` 가 참조 중이라
        # 게이트가 ``deprecated_reference`` high 를 냈다 (2026-08-19 실측: 이
        # 자기충족 루프가 check_quality_rules 를 baseline FAIL 로 고정시켰다).
        from domain.graph_utils import named_domain_classes

        all_classes = sorted(named_domain_classes(g, steel_str), key=str)
        # property 의 domain/range 로 쓰인 클래스
        used_by_props: set[URIRef] = set()
        for _, _, o in g.triples((None, RDFS.domain, None)):
            if isinstance(o, URIRef):
                used_by_props.add(o)
        for _, _, o in g.triples((None, RDFS.range, None)):
            if isinstance(o, URIRef):
                used_by_props.add(o)
        # restriction 이 참조하는 클래스
        for pred in (OWL.onClass, OWL.someValuesFrom, OWL.allValuesFrom):
            for _, _, o in g.triples((None, pred, None)):
                if isinstance(o, URIRef):
                    used_by_props.add(o)
        # 자식이 있는 클래스
        has_child: set[URIRef] = set()
        for _, _, parent in g.triples((None, RDFS.subClassOf, None)):
            if isinstance(parent, URIRef):
                has_child.add(parent)
        # disjoint group 멤버
        in_disjoint: set[URIRef] = set()
        for adc in g.subjects(RDF.type, OWL.AllDisjointClasses):
            members_node = g.value(adc, OWL.members)
            if members_node is None:
                continue
            try:
                from rdflib.collection import Collection as _Coll
                for m in _Coll(g, members_node):
                    if isinstance(m, URIRef):
                        in_disjoint.add(m)
            except Exception:
                pass
        for cls in all_classes:
            in_use = cls in has_child or cls in used_by_props or cls in in_disjoint
            already = (cls, OWL.deprecated, Literal(True)) in g
            if in_use:
                # **되살린다.** 이전 실행에서 "자식·용처 없음" 으로 마킹됐지만
                # 그 뒤 step_12/14 가 자식을 붙였으면 그 판정은 더 이상 참이 아니다.
                # 마킹만 하고 해제하지 않으면 S3 를 두 번 돌린 T-Box 에서
                # ``deprecated_reference`` high 가 뜨고, 그것이
                # ``check_quality_rules`` 를 **baseline FAIL** 로 고정해
                # mutation 검출을 죽인다 (2026-08-22 실측: 5건 —
                # EnergyConsumption / EnergyInfrastructure / AtmosphericMonitoring /
                # WasteNoiseMonitoring / LandWaterMonitoring 이 각각 자식 2~4개를
                # 얻은 뒤에도 deprecated 로 남아 있었다).
                #
                # 이 스텝이 **자기가 붙인 표지만** 되살린다 — 사람이 손으로 넣은
                # deprecated 는 ``over_engineered:`` seeAlso 가 없으므로 보존된다.
                if already and any(
                    isinstance(o, Literal) and str(o).startswith("over_engineered:")
                    for o in g.objects(cls, RDFS.seeAlso)
                ):
                    g.remove((cls, OWL.deprecated, Literal(True)))
                    for obj in list(g.objects(cls, RDFS.seeAlso)):
                        if (isinstance(obj, Literal)
                                and str(obj).startswith("over_engineered:")):
                            g.remove((cls, RDFS.seeAlso, obj))
                    undeprecated_count += 1
                continue
            if already:
                continue
            g.add((cls, OWL.deprecated, Literal(True)))
            g.add((cls, RDFS.seeAlso,
                   Literal("over_engineered: no children, no usage")))
            deprecated_count += 1
        if undeprecated_count:
            logger.info(
                "Over-engineered 마킹 해제 %d건 — 이전 실행 이후 자식/용처가 "
                "생겼다. 남겨두면 deprecated_reference high 로 품질 게이트가 "
                "baseline FAIL 이 되어 mutation 검출이 죽는다",
                undeprecated_count,
            )
    except Exception as e:
        logger.warning("Over-engineered 마킹 실패: %s", e)
        error = str(e)
    return StepResult(
        name="step_21_over_engineered_dep",
        stats={"over_engineered_deprecated": deprecated_count,
               "over_engineered_undeprecated": undeprecated_count},
        triples_delta=len(g) - before,
        error=error,
        step_number=21,
        step_label="over_engineered_deprecation",
    )
