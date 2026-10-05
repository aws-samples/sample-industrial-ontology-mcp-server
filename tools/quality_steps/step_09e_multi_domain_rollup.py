"""Step 9e — Property domain 다중 → 공통 조상으로 롤업 (DP + OP 공통).

본문 ontology_quality.py 의 Step 9e 블록을 그대로 모듈로 옮김.

SHACL 룰 ``exactly one rdfs:domain`` 위반 해결. 공통 조상이 없으면
``owl:unionOf`` BNode 로 합친다. ObjectProperty 도 동일 로직 (multiple_domains
HIGH 룰).
"""
from __future__ import annotations

import logging

from rdflib import OWL, RDF, RDFS, BNode, Graph, URIRef

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def apply(g: Graph, ctx: StepContext) -> StepResult:
    before = len(g)
    steel_str = ctx.domain_ns

    def _ancestors_of(cls_uri: URIRef) -> set[URIRef]:
        visited: set[URIRef] = set()
        stack: list[URIRef] = [cls_uri]
        while stack:
            cur = stack.pop()
            if cur in visited:
                continue
            visited.add(cur)
            for parent in g.objects(cur, RDFS.subClassOf):
                if isinstance(parent, URIRef):
                    stack.append(parent)
        return visited

    def _resolve_multi_domain(prop: URIRef) -> tuple[bool, bool]:
        """prop 의 domain 이 2개 이상이면 단일화.

        OWL DL 에서 여러 rdfs:domain 선언은 intersection 으로 해석되어
        hasEquipment(steel:X, iof:Y) 처럼 cross-namespace 도 문제. 따라서
        steel/외부 네임스페이스 구분 없이 URIRef 도메인을 모두 대상으로 처리.

        Returns: (merged_to_ancestor, union_created). 둘 다 False 면 처리 안 함.
        """
        all_domains = list(g.objects(prop, RDFS.domain))
        uri_domains = [d for d in all_domains if isinstance(d, URIRef)]
        non_thing = [d for d in uri_domains if d != OWL.Thing]
        if non_thing and OWL.Thing in uri_domains:
            g.remove((prop, RDFS.domain, OWL.Thing))
            uri_domains = non_thing
        if len(uri_domains) < 2:
            return (False, False)
        ancestor_sets = [_ancestors_of(d) for d in uri_domains]
        common = set.intersection(*ancestor_sets)
        common.discard(OWL.Thing)
        common -= set(uri_domains)
        if common:
            # 가장 좁은 공통 조상 (자식 수 최소) 를 고른다. 동률이면 **URI 사전순**
            # 으로 확정한다 — 예전엔 `next(iter(common))` 로 시작해 strict `<` 로만
            # 갱신했기 때문에 동률 시 승자가 set 순회 순서, 즉 PYTHONHASHSEED 에
            # 달려 있었다 (2026-08-08 규명: 6개 시드에서 Asset/PhysicalThing 이
            # 번갈아 선택됨). domain 이 바뀌면 OWL RL prp-dom 이 다른 타입을
            # 부여하므로 서버 재기동만으로 추론 결과가 달라진다 — 이 리포가
            # 내세우는 결정적 재현성 위반이다.
            chosen = min(
                common,
                key=lambda c: (len(list(g.subjects(RDFS.subClassOf, c))), str(c)),
            )
            for d in uri_domains:
                g.remove((prop, RDFS.domain, d))
            g.add((prop, RDFS.domain, chosen))
            return (True, False)
        from rdflib.collection import Collection as _Coll
        union_node = BNode()
        list_head = BNode()
        _Coll(g, list_head, list(uri_domains))
        g.add((union_node, RDF.type, OWL.Class))
        g.add((union_node, OWL.unionOf, list_head))
        for d in uri_domains:
            g.remove((prop, RDFS.domain, d))
        g.add((prop, RDFS.domain, union_node))
        return (False, True)

    dp_domain_merged = 0
    dp_domain_union_created = 0
    op_domain_merged = 0
    op_domain_union_created = 0
    dp_thing_removed = 0
    op_thing_removed = 0

    for prop in set(g.subjects(RDF.type, OWL.DatatypeProperty)):
        if not (isinstance(prop, URIRef) and str(prop).startswith(steel_str)):
            continue
        had_thing = OWL.Thing in list(g.objects(prop, RDFS.domain))
        merged, union_ = _resolve_multi_domain(prop)
        if had_thing and OWL.Thing not in list(g.objects(prop, RDFS.domain)):
            dp_thing_removed += 1
        if merged:
            dp_domain_merged += 1
        elif union_:
            dp_domain_union_created += 1

    for prop in set(g.subjects(RDF.type, OWL.ObjectProperty)):
        if not (isinstance(prop, URIRef) and str(prop).startswith(steel_str)):
            continue
        had_thing = OWL.Thing in list(g.objects(prop, RDFS.domain))
        merged, union_ = _resolve_multi_domain(prop)
        if had_thing and OWL.Thing not in list(g.objects(prop, RDFS.domain)):
            op_thing_removed += 1
        if merged:
            op_domain_merged += 1
        elif union_:
            op_domain_union_created += 1

    return StepResult(
        name="step_09e_multi_domain_rollup",
        stats={
            "dp_domain_merged_to_ancestor": dp_domain_merged,
            "dp_domain_union_created": dp_domain_union_created,
            "op_domain_merged_to_ancestor": op_domain_merged,
            "op_domain_union_created": op_domain_union_created,
            "dp_thing_domain_removed": dp_thing_removed,
            "op_thing_domain_removed": op_thing_removed,
        },
        triples_delta=len(g) - before,
        step_number="9e",
        step_label="multi_domain_rollup",
    )
