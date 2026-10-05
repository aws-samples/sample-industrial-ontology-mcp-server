"""Step 1 — AllDisjointClasses 완전화.

본문 ontology_quality.py 의 Step 1 블록을 그대로 모듈로 옮김.

- 기존 pairwise disjointWith 제거 (DISJOINT_GROUPS 멤버 한정)
- 기존 AllDisjointClasses bnode 정리
- DISJOINT_GROUPS 별로 새 AllDisjointClasses 추가 (parent-child 충돌 클래스 제외)
"""
from __future__ import annotations

import logging

import rdflib
from rdflib import OWL, RDF, BNode, Graph, URIRef

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)


def apply(g: Graph, ctx: StepContext) -> StepResult:
    from tools.ontology_quality import (
        DISJOINT_GROUPS,
        DOMAIN_NS_OBJ,
        _compute_steel_parents_map,
    )

    before = len(g)
    steel_str = ctx.domain_ns

    all_disjoint_classes = set()
    for group in DISJOINT_GROUPS:
        for cls in group:
            all_disjoint_classes.add(DOMAIN_NS_OBJ[cls])

    for s, p, o in list(g.triples((None, OWL.disjointWith, None))):
        if s in all_disjoint_classes or o in all_disjoint_classes:
            g.remove((s, p, o))
            g.remove((o, p, s))

    # 설정 그룹에 **속한 클래스를 다루는** 노드만 재구축 대상으로 지운다.
    #
    # 예전엔 모든 ``owl:AllDisjointClasses`` 를 무조건 지우고 ``DISJOINT_GROUPS`` 로만
    # 다시 세웠다. 그래서 S2 Jury (``jury_fixes._apply_add_disjoint_classes``),
    # ``rules/domain/tbox_manual_additions.ttl`` (step_30), tacit 입력이 **설정 밖 클래스**
    # 에 대해 저작한 공리가 대체 없이 사라졌다 — Validator 가 요구해 Jury 가 넣은
    # 수정이 S3 에서 조용히 되돌려지고, stats 는 파괴된 것을 언급하지 않는다
    # (2026-08-08 규명). 바로 위 pairwise 루프는 이미 설정 범위로 한정돼 있어
    # 두 표기의 처리가 비대칭이었다.
    preserved_external = 0
    orphan_members_removed = 0
    for bnode in list(g.subjects(RDF.type, OWL.AllDisjointClasses)):
        # ``owl:members`` 가 **여러 개** 붙어 있을 수 있다. skolemize 가 같은 멤버
        # 집합을 가진 노드를 한 IRI 로 병합할 때 각자의 리스트가 둘 다 붙기
        # 때문이다. ``g.value`` 는 첫 리스트만 돌려주므로 예전에는 나머지가 정리되지
        # 않고 **rest-only 체인** (rdf:first 없음) 으로 남았고, HermiT 가 그것을
        # "멤버 0개 DisjointClasses" 로 읽어 **로드 자체를 거부** 했다
        # (실측 2026-08-18: 배포 T-Box 3개 노드, "A DisjointClasses axiom in OWL 2
        # DL must have at least two classes as parameters").
        member_lists = list(g.objects(bnode, OWL.members))
        if len(member_lists) > 1:
            # 정상 리스트(가장 긴 것) 하나만 남기고 나머지는 완전히 제거한다.
            def _len(node) -> int:
                n, seen = 0, set()
                while node is not None and node != RDF.nil and node not in seen:
                    seen.add(node)
                    if next(g.objects(node, RDF.first), None) is not None:
                        n += 1
                    node = next(g.objects(node, RDF.rest), None)
                return n

            keep = max(member_lists, key=_len)
            for extra in member_lists:
                if extra is keep:
                    continue
                g.remove((bnode, OWL.members, extra))
                node, seen = extra, set()
                while node is not None and node != RDF.nil and node not in seen:
                    seen.add(node)
                    nxt = next(g.objects(node, RDF.rest), None)
                    for t in list(g.triples((node, None, None))):
                        g.remove(t)
                    node = nxt
                orphan_members_removed += 1
            member_lists = [keep]
        members_node = member_lists[0] if member_lists else None
        touches_config = False
        if members_node is not None:
            try:
                for member in rdflib.collection.Collection(g, members_node):
                    if member in all_disjoint_classes:
                        touches_config = True
                        break
            except Exception as e:  # noqa: BLE001 — 판정 불가 시 보존
                logger.debug("AllDisjointClasses members 읽기 실패: %s", e)
        if not touches_config:
            # 설정과 무관한 외부 저작 공리 — 재구축이 대체하지 않으므로 보존한다.
            preserved_external += 1
            continue
        for s, p, o in list(g.triples((bnode, None, None))):
            if p == OWL.members:
                try:
                    rdflib.collection.Collection(g, o).clear()
                except Exception as e:
                    logger.debug(
                        "AllDisjointClasses members collection clear 실패: %s", e
                    )
            g.remove((s, p, o))
    if preserved_external:
        logger.info(
            "Step 1: 설정 밖 클래스의 외부 저작 AllDisjointClasses %d개 보존 "
            "(Jury/수동추가/tacit 저작분)", preserved_external,
        )
    if orphan_members_removed:
        logger.warning(
            "Step 1: 중복 owl:members 리스트 %d개 제거 — 한 "
            "AllDisjointClasses 노드에 리스트가 둘 이상 붙어 있었다 "
            "(skolemize 병합 잔여물). HermiT 는 그 형태를 '멤버 0개 "
            "DisjointClasses' 로 읽고 로드를 거부한다.",
            orphan_members_removed,
        )

    _parent_closure = _compute_steel_parents_map(g, steel_str)

    def _is_ancestor(child_uri: URIRef, ancestor_uri: URIRef) -> bool:
        seen: set[URIRef] = set()
        stack = [child_uri]
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            for pa in _parent_closure.get(node, set()):
                if pa == ancestor_uri:
                    return True
                stack.append(pa)
        return False

    # 그래프의 subClassOf 만 보면 **아직 만들어지지 않은** 계층을 놓친다.
    # 이 스텝(1)은 서브그룹 생성(14)보다 먼저 돌기 때문에, 설정이 부모-자식으로
    # 지정한 쌍이 그래프에는 형제로 보인다. 그대로 disjoint 로 선언하면 step_14 가
    # 그 계층을 영구히 만들 수 없다 (실측 2026-08-11: EquipmentAsset 와 그 자식
    # EquipmentMaster/TagMaster 가 같은 disjoint 그룹에 들어가 서브그룹 생성이
    # 차단되고, 다른 경로로 붙은 부모와 겹쳐 unsatisfiable 이 됐다).
    _declared_child_of: dict[str, set[str]] = {}
    try:
        from tools.ontology_quality import (
            _load_hierarchy_config,
            _load_sub_group_config,
        )
        for _subgroups in _load_sub_group_config().values():
            for _sg in _subgroups:
                for _child in (_sg.get("children") or []):
                    _declared_child_of.setdefault(_child, set()).add(_sg["class"])
        # 중간 추상 클래스(step_12)가 붙일 부모도 같은 이유로 반영한다.
        for _cfg in _load_hierarchy_config().values():
            _parent = _cfg.get("class")
            for _table in (_cfg.get("children_tables") or []):
                _child = _table.replace("_", "")
                if _parent:
                    _declared_child_of.setdefault(_child, set()).add(_parent)
    except Exception as exc:  # noqa: BLE001 — 설정 없으면 그래프 판정만 쓴다
        logger.debug("design_patterns 계층 로드 실패 (그래프 판정만 사용): %s", exc)

    def _declared_parent_child(a: str, b: str) -> bool:
        return b in _declared_child_of.get(a, ()) or a in _declared_child_of.get(b, ())

    adj_count = 0
    adj_skipped_parent_child = 0
    for group in DISJOINT_GROUPS:
        existing = [
            c for c in group
            if (DOMAIN_NS_OBJ[c], RDF.type, OWL.Class) in g
        ]
        filtered: list[str] = []
        for cls_name in existing:
            cls_uri = DOMAIN_NS_OBJ[cls_name]
            conflict = False
            for other in filtered:
                other_uri = DOMAIN_NS_OBJ[other]
                if (_is_ancestor(cls_uri, other_uri)
                        or _is_ancestor(other_uri, cls_uri)
                        or _declared_parent_child(cls_name, other)):
                    conflict = True
                    adj_skipped_parent_child += 1
                    break
            if not conflict:
                filtered.append(cls_name)
        if len(filtered) < 2:
            continue
        bnode = BNode()
        g.add((bnode, RDF.type, OWL.AllDisjointClasses))
        members = BNode()
        col = rdflib.collection.Collection(g, members)
        for cls_name in filtered:
            col.append(DOMAIN_NS_OBJ[cls_name])
        g.add((bnode, OWL.members, members))
        adj_count += 1

    return StepResult(
        name="step_01_disjoint_complete",
        stats={
            "disjoint_groups_added": adj_count,
            "disjoint_skipped_parent_child": adj_skipped_parent_child,
            # 재구축이 건드리지 않고 보존한 외부 저작 공리 수 (Jury/수동추가/tacit).
            "external_disjoint_preserved": preserved_external,
            # 한 노드에 둘 이상 붙어 있던 owl:members 리스트 제거 수 —
            # HermiT 로드를 막던 skolemize 병합 잔여물.
            "orphan_disjoint_members_removed": orphan_members_removed,
        },
        triples_delta=len(g) - before,
        step_number=1,
        step_label="AllDisjointClasses",
    )
