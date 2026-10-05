"""Jury required_fixes 를 TTL 에 자동 적용하는 경량 엔진.

Jury 단계에서 받은 action 리스트를 rdflib Graph 에 반영해 다음 라운드 또는 최종
TTL 에 수정이 반영되도록 한다. 복잡한 블랭크노드 리스트 조작(예: owl:members 제거,
union range 확장)은 **이번 구현에서 제외** 하고 skipped 목록에 사유 기록 — 에이전트가
사용자에게 "이 N건은 수동 검토 필요" 로 안내.

지원 action:
- add_object_property: property/domain/range [+ label_en/label_ko]
- add_inverse_property: property/inverseOf
- add_restriction: class/onProperty + (minCardinality|cardinality|someValuesFrom|hasValue)

호출 예:
    result = apply_jury_fixes(ttl, required_fixes)
    result["applied"]  # list[{"action": ..., "status": "ok"}]
    result["skipped"]  # list[{"action": ..., "reason": "..."}]
    result["ttl"]      # 수정된 TTL 문자열
"""
from __future__ import annotations

import logging
from typing import Any

from rdflib import OWL, RDF, RDFS, XSD, BNode, Graph, Literal, Namespace, URIRef
from rdflib.collection import Collection

from domain.namespaces import DOMAIN_NS
from domain.tbox_utils import _new_graph
from domain.uri_conventions import is_reserved_local as _is_reserved_local

logger = logging.getLogger(__name__)

_STEEL = Namespace(DOMAIN_NS)
_DCTERMS = Namespace("http://purl.org/dc/terms/")
_SUPPORTED_ACTIONS = {
    "add_object_property",
    "add_inverse_property",
    "add_restriction",
    "add_disjoint_classes",
    "add_class",
    "add_subclass",
    "add_subclass_batch",
    "add_subclass_axioms",
    "add_triple",
    "add_triples",
    "delete_class",
    "add_datatype_property",
    "modify_subclass",
    "delete_triple",
    "remove_triple",
    "remove_all_restrictions",
    "remove_annotations",
    "remove_object_property",
    "remove_class",
    "add_axiom",
    "keep_no_domain",
    "modify_triple",
    "modify_triples",
    "rename_uri",
    # R28 신규: Jury 가 production_ready=false 로 막혔던 주요 action 들
    "fix_namespace_bulk",
    "fix_namespace",
    "split_property",
    "replace_triple",
    "remove_duplicate",
    "add_class_hierarchy",
    "add_iof_mappings",
    "add_functional_property",
    # R28 D: LLM 이 발명하는 별칭들 — 기존 핸들러로 라우팅 (dispatch 에서 처리)
    "replace_iri_batch",
    "fix_iri_prefix",
    "fix_iri_prefix_in_all_restrictions",
    "rename_namespace",
    "rewrite_uri_batch",
    "add_intermediate_classes",
    "add_abstract_classes",
    "introduce_abstract_hierarchy",
    "remove_resource",
    "delete_resource",
    "delete_property",
    "remove_property",
    "add_iof_mapping",
    "link_to_iof",
    "deduplicate_property",
    "verify_ttl_syntax",
    "validate_ttl",
    "run_validation",
}


def _as_uri(name: str, graph: Graph | None = None) -> URIRef:
    """'steel:hasX' / 'hasX' / 'iof-core:X' / 절대 URI → URIRef.

    이전 구현은 콜론 앞을 **판별 없이 버리고** 무조건 도메인 네임스페이스에
    붙였다 (``graph_ns[name.split(":", 1)[1]]``). 그래서 Jury 가
    ``range="iof-core:MaterialArtifact"`` 를 보내면 IOF 클래스가 아니라 선언되지
    않은 ``steel:MaterialArtifact`` 를 가리켰다 — 이 함수는 add_object_property /
    add_inverse_property / add_restriction(someValuesFrom·hasValue) 등 Jury 의
    **주요 쓰기 경로 10곳** 에서 쓰이므로 피해가 가장 넓었다.

    이제 :func:`domain.graph_utils.resolve_entity_name` 에 위임한다. ``graph`` 를
    주면 그래프 바인딩까지 활용하고, 없으면 정적 외래 prefix 표로 판정한다.
    """
    from domain.graph_utils import resolve_entity_name
    return resolve_entity_name(name, graph)


def _humanize_local_name(local: str) -> str:
    """camelCase/PascalCase local name → 소문자 공백 분리 문자열 (공용 헬퍼 별칭)."""
    from domain.graph_utils import humanize_local_name
    return humanize_local_name(local)


def _add_labels(g: Graph, subject: URIRef, label_en: str | None,
                 label_ko: str | None) -> None:
    """라벨 주입. label_en/label_ko 가 없으면 local name 에서 자동 생성.

    이미 동일 언어 태그의 rdfs:label 이 있으면 덮어쓰지 않는다.
    Jury action 에서 label_* 를 빠뜨린 신규 엔티티도 기본 라벨을 확보해
    check_quality_rules missing_label HIGH 를 방지.
    """
    existing = list(g.objects(subject, RDFS.label))
    langs = {
        getattr(lab, "language", None)
        for lab in existing if isinstance(lab, Literal)
    }

    effective_en = label_en or _humanize_local_name(
        str(subject).split("#")[-1].split("/")[-1]
    )
    if "en" not in langs and effective_en:
        g.add((subject, RDFS.label, Literal(effective_en, lang="en")))

    if "ko" not in langs:
        if label_ko:
            g.add((subject, RDFS.label, Literal(label_ko, lang="ko")))
        elif effective_en:
            # 한국어는 영문 그대로 placeholder (improve_tbox Q2 의 _KO_HINTS 가 대체)
            g.add((subject, RDFS.label, Literal(effective_en, lang="ko")))


def _apply_add_object_property(g: Graph, action: dict) -> tuple[bool, str]:
    prop = action.get("property")
    dom = action.get("domain")
    rng = action.get("range")
    if not (prop and dom and rng):
        return False, "property/domain/range 중 누락"
    if _is_reserved_local(prop):
        return False, (
            f"reserved local name '{prop}' — owlready2/HermiT 충돌 방지. "
            "더 도메인-특화된 이름(예: alarmType, equipmentLocation)으로 바꾸세요."
        )
    p = _as_uri(prop, g)
    dom_uri, rng_uri = _as_uri(dom, g), _as_uri(rng, g)
    # 같은 (domain, range) 를 이미 잇는 OP 가 있으면 **동의어를 만들지 않는다**.
    # LLM 은 T-Box 의 일부만 보고 판단하므로 이미 있는 관계를 다시 요청한다
    # (2026-08-10 실측: Jury 가 요청한 15쌍 중 14쌍이 이미 존재). 그대로 추가하면
    # step_22d 중복 OP 게이트가 악화되고, CSV FK 컬럼은 하나뿐이라 A-Box 는 그중
    # 하나만 채운다 — 나머지는 값 0건이라 "빈 관계로 질의하면 0건이 정답처럼"
    # 반환되는 함정이 된다.
    if p not in set(g.subjects(RDF.type, OWL.ObjectProperty)):
        existing = _ops_linking(g, dom_uri, rng_uri)
        if existing:
            return False, (
                f"이미 존재: {dom}→{rng} 를 잇는 OP "
                f"{sorted(str(e).split('#')[-1] for e in existing)[:3]} — "
                "동의어 추가는 중복 OP 게이트를 악화시킨다"
            )
    g.add((p, RDF.type, OWL.ObjectProperty))
    g.add((p, RDFS.domain, dom_uri))
    g.add((p, RDFS.range, rng_uri))
    _add_labels(g, p, action.get("label_en"), action.get("label_ko"))
    # CSV 컬럼 출처는 A-Box 가 컬럼↔프로퍼티를 잇는 확정 근거다 (Step 12e 게이트).
    # DSL 쪽은 이미 기록하는데 이 경로는 버리고 있었다 — 두 엔진의 계약 불일치.
    source_col = _extract_source_column(action)
    if source_col:
        g.add((p, _DCTERMS.source, Literal(source_col)))
    comment_ko = action.get("comment_ko") or action.get("comment")
    if comment_ko and not list(g.objects(p, RDFS.comment)):
        g.add((p, RDFS.comment, Literal(str(comment_ko), lang="ko")))
    return True, "ok"


#: LLM 이 CSV 컬럼 출처로 쓰는 키 표기 변종. 실측 (서버 로그 2026-07~08):
#: `dcterms:source` 198회 / `source_column` 24회 / `source` / `sourceColumn`.
#: 한 곳만 보면 나머지 표기가 조용히 버려진다 — OP·DP 두 경로가 같은 표를 쓴다.
_SOURCE_KEYS: tuple[str, ...] = (
    # ``fk_column`` 은 ObjectProperty 의 FK 근거 키다 (프롬프트
    # ``add_object_property`` 가 필수로 요구). DP 의 ``source`` 와 같은 추출 경로를
    # 쓰지 않으면 그 지시가 조용히 폐기된다.
    "source", "dcterms:source", "source_column", "sourceColumn", "csv_column",
    "fk_column",
)


def _extract_source_column(action: dict) -> str:
    """action 에서 CSV 컬럼 출처를 뽑아 **헤더와 비교 가능한 형태** 로 정리한다.

    값에 Turtle 표기가 섞여 오므로 (`"Event_ID"^^xsd:string`) 정리를 거친다 —
    A-Box 는 이 값을 CSV 헤더와 직접 비교하기 때문에 그대로 쓰면 매칭이 영구히
    실패한다.
    """
    from domain.graph_utils import clean_source_column

    for key in _SOURCE_KEYS:
        raw = action.get(key)
        if raw:
            cleaned = clean_source_column(raw)
            if cleaned:
                return cleaned
    return ""


def _ops_linking(g: Graph, dom_uri: URIRef, rng_uri: URIRef) -> set[URIRef]:
    """``(domain, range)`` 쌍을 잇는 기존 OP 집합 (공용 헬퍼 별칭)."""
    from domain.graph_utils import ops_linking
    return ops_linking(g, dom_uri, rng_uri)


def _apply_add_inverse_property(g: Graph, action: dict) -> tuple[bool, str]:
    """inverseOf 관계 추가. 누락된 쪽의 domain/range 는 swap 으로 자동 채움.

    e.g., (hasSurfaceQuality, domain=ProductMaster, range=SurfaceQuality) 가
    이미 있고 surfaceQualityOf 에 domain/range 가 없으면
    (surfaceQualityOf, domain=SurfaceQuality, range=ProductMaster) 로 swap.
    이 동작은 check_quality_rules 의 missing_domain/range HIGH 를 방지한다.
    """
    prop = action.get("property")
    inv = action.get("inverseOf")
    if not (prop and inv):
        return False, "property/inverseOf 누락"
    p = _as_uri(prop, g)
    i = _as_uri(inv, g)

    # 이 핸들러는 **``add_object_property`` 가 거부한 쌍을 되살리는 우회로** 였다.
    # ``_swap_domain_range`` 가 상대의 domain/range 를 뒤집어 채우므로, 신규
    # 프로퍼티가 결국 기존 OP 와 같은 ``(domain, range)`` 를 갖게 된다 — 실측
    # (2026-08-11): ``isBOf(B→A)`` 가 있는 그래프에 ``add_inverse_property
    # (synonymAtoB, inverseOf=isBOf)`` 를 주면 ``(A→B)`` 가
    # ``{hasB, synonymAtoB}`` 로 늘어난다. 같은 배치에서 :159 가 이미 거부한
    # 중복이 여기로 들어온다.
    #
    # 그래서 **새 프로퍼티일 때만** 쌍 점유를 검사한다. 이미 선언된 두 OP 를
    # inverseOf 로 잇는 것은 정당한 보강이므로 막지 않는다 (그것이 이 핸들러의
    # 본래 목적 — missing_domain/range HIGH 방지).
    for new_prop, counterpart in ((p, i), (i, p)):
        if (new_prop, RDF.type, OWL.ObjectProperty) in g:
            continue                      # 기존 OP — 보강 대상이므로 통과
        # 신규 프로퍼티가 얻게 될 (domain, range) = 상대의 (range, domain)
        doms = [
            o for o in g.objects(counterpart, RDFS.range) if isinstance(o, URIRef)
        ]
        rngs = [
            o for o in g.objects(counterpart, RDFS.domain) if isinstance(o, URIRef)
        ]
        for dom_uri in doms:
            for rng_uri in rngs:
                taken = _ops_linking(g, dom_uri, rng_uri) - {new_prop}
                if taken:
                    return False, (
                        f"({str(dom_uri).split('#')[-1]}→"
                        f"{str(rng_uri).split('#')[-1]}) 쌍은 "
                        f"{sorted(str(t).split('#')[-1] for t in taken)[:3]} 가 이미 "
                        "잇는다 — 동의어는 중복 게이트를 악화시킨다 (이미 존재)"
                    )

    # 양쪽이 이미 OP 로 선언되지 않았으면 추가(안전).
    g.add((p, RDF.type, OWL.ObjectProperty))
    g.add((i, RDF.type, OWL.ObjectProperty))
    g.add((p, OWL.inverseOf, i))
    g.add((i, OWL.inverseOf, p))

    # swap domain/range: p → i 방향
    _swap_domain_range(g, p, i)
    # 역방향: i → p (p 쪽이 비어 있고 i 쪽이 채워진 경우 대비)
    _swap_domain_range(g, i, p)
    return True, "ok"


def _swap_domain_range(g: Graph, source: URIRef, target: URIRef) -> None:
    """source 의 rdfs:domain 을 target 의 rdfs:range 로, 역도 마찬가지로 주입.

    이미 target 에 값이 있으면 덮어쓰지 않는다. inverse OP 가 domain/range
    를 누락한 경우의 자동 보완용.
    """
    if list(g.triples((target, RDFS.range, None))):
        pass  # target.range 이미 있음
    else:
        for _, _, d in g.triples((source, RDFS.domain, None)):
            g.add((target, RDFS.range, d))
    if list(g.triples((target, RDFS.domain, None))):
        return
    for _, _, r in g.triples((source, RDFS.range, None)):
        g.add((target, RDFS.domain, r))


def _apply_add_restriction(g: Graph, action: dict) -> tuple[bool, str]:
    """owl:Restriction 또는 owl:hasKey 를 추가한다.

    type=owl:hasKey 로 지정되면 (class, owl:hasKey, rdf:List[properties]) 를
    생성하고 Restriction blank node 는 만들지 않는다.
    """
    cls = action.get("class")
    rtype = action.get("type")
    # hasKey 경로 — class + properties(list) 면 충분
    if rtype in ("owl:hasKey", "hasKey"):
        if not cls:
            return False, "hasKey 에는 class 필수"
        props = action.get("properties") or []
        if not isinstance(props, list) or not props:
            return False, "hasKey 에는 properties list(>=1) 필수"
        c = _resolve_prefixed_or_uri(cls, g)
        key_list = BNode()
        Collection(g, key_list, [_resolve_prefixed_or_uri(p, g) for p in props])
        g.add((c, OWL.hasKey, key_list))
        return True, "ok"

    on_prop = action.get("onProperty")
    if not (cls and on_prop):
        return False, "class/onProperty 누락"
    c = _as_uri(cls, g)
    op = _as_uri(on_prop, g)

    # Domain-compatibility guard. A restriction of the form
    #   C rdfs:subClassOf [ onProperty P; ... ]
    # is only coherent when instances of C can legally participate in P, i.e.
    # the declared rdfs:domain of P must be C itself or an ancestor/descendant.
    # Without this guard, the jury routinely attaches minCardinality=1 on
    # unrelated properties (observed: MaintenanceHistory subClassOf
    # [onProperty hasEquipment; minCardinality 1] while hasEquipment's real
    # domain is EnergyEfficiency), which makes 100% of those instances violate
    # the restriction.
    op_domains = [d for d in g.objects(op, RDFS.domain)
                  if isinstance(d, URIRef)]
    if op_domains:
        compatible = False
        for d in op_domains:
            if d == c:
                compatible = True
                break
            # ancestor chain: c ⊆ d
            if (c, RDFS.subClassOf, d) in g:
                compatible = True
                break
            # descendant chain: d ⊆ c (restriction on parent class)
            if (d, RDFS.subClassOf, c) in g:
                compatible = True
                break
        if not compatible:
            return False, (
                f"restriction domain-incompatible: class={cls} "
                f"onProperty={on_prop} domain={[str(d).split('#')[-1] for d in op_domains]}"
            )

    r = BNode()
    g.add((r, RDF.type, OWL.Restriction))
    g.add((r, OWL.onProperty, op))
    added = False
    if "minCardinality" in action:
        g.add((r, OWL.minCardinality,
               Literal(int(action["minCardinality"]), datatype=XSD.nonNegativeInteger)))
        added = True
    if "cardinality" in action:
        g.add((r, OWL.cardinality,
               Literal(int(action["cardinality"]), datatype=XSD.nonNegativeInteger)))
        added = True
    if "maxCardinality" in action:
        g.add((r, OWL.maxCardinality,
               Literal(int(action["maxCardinality"]), datatype=XSD.nonNegativeInteger)))
        added = True
    if "someValuesFrom" in action:
        g.add((r, OWL.someValuesFrom, _as_uri(action["someValuesFrom"], g)))
        added = True
    if "hasValue" in action:
        # hasValue 는 값이 URI 일 수도, 리터럴일 수도 있음. 우선 URI 로 시도.
        raw = action["hasValue"]
        if isinstance(raw, str) and (raw.startswith("http") or ":" in raw):
            g.add((r, OWL.hasValue, _as_uri(raw, g)))
        else:
            g.add((r, OWL.hasValue, Literal(raw)))
        added = True
    if not added:
        return False, "restriction 의 제약 필드(minCardinality/cardinality/someValuesFrom 등) 누락"
    g.add((c, RDFS.subClassOf, r))
    return True, "ok"


def _resolve_prefixed_or_uri(raw: str, graph: Graph) -> URIRef:
    """prefixed name(iof-core:X) 을 graph 의 prefix binding 으로 resolve.

    공용 해석기의 얇은 별칭 — 이 모듈의 50여 개 핸들러가 이 이름으로 부른다.
    (예전에는 이 함수가 자체 구현이면서 미등록 prefix 를 ``_as_uri`` 로 폴백해,
    **정확한 구현이 뭉개는 구현에 위임** 하는 구조였다.)
    """
    from domain.graph_utils import resolve_entity_name
    return resolve_entity_name(raw, graph)


def _apply_add_disjoint_classes(g: Graph, action: dict) -> tuple[bool, str]:
    members = action.get("members") or []
    if not isinstance(members, list) or len(members) < 2:
        return False, "members list(>=2) 누락"
    member_uris = [_resolve_prefixed_or_uri(m, g) for m in members]
    # AllDisjointClasses 블랭크노드 생성
    adc = BNode()
    g.add((adc, RDF.type, OWL.AllDisjointClasses))
    # owl:members 는 rdf:List (컬렉션). rdflib Collection 사용.
    list_root = BNode()
    Collection(g, list_root, member_uris)
    g.add((adc, OWL.members, list_root))
    return True, "ok"


def _apply_add_class(g: Graph, action: dict) -> tuple[bool, str]:
    """owl:Class 선언 추가. Jury 가 'class' 또는 'name' 필드로 전달 가능."""
    cls = action.get("class") or action.get("name")
    sup = action.get("superClass") or action.get("subClassOf")
    if not cls:
        return False, "class/name 누락"
    if _is_reserved_local(cls):
        return False, (
            f"reserved local name '{cls}' — owlready2/HermiT 충돌 방지. "
            "더 도메인-특화된 이름으로 바꾸세요."
        )
    c = _resolve_prefixed_or_uri(cls, g)
    g.add((c, RDF.type, OWL.Class))
    if sup:
        g.add((c, RDFS.subClassOf, _resolve_prefixed_or_uri(sup, g)))
    # comment 또는 comment_ko 모두 허용 (jury 출력 정규화)
    comment = action.get("comment") or action.get("comment_ko")
    if comment:
        g.add((c, RDFS.comment, Literal(comment, lang="ko")))
    comment_en = action.get("comment_en")
    if comment_en:
        g.add((c, RDFS.comment, Literal(comment_en, lang="en")))
    _add_labels(g, c, action.get("label_en"), action.get("label_ko"))
    return True, "ok"


def _apply_add_subclass(g: Graph, action: dict) -> tuple[bool, str]:
    cls = action.get("class")
    sup = action.get("superClass")
    if not (cls and sup):
        return False, "class/superClass 누락"
    c = _resolve_prefixed_or_uri(cls, g)
    s = _resolve_prefixed_or_uri(sup, g)
    g.add((c, RDFS.subClassOf, s))
    return True, "ok"


def _apply_modify_subclass(g: Graph, action: dict) -> tuple[bool, str]:
    """기존 rdfs:subClassOf 트리플을 모두 제거하고 새 superClass 로 교체.

    기존 부모가 블랭크노드(Restriction) 인 경우는 보존 — 제약은 유지해야 의미론 보존.
    """
    cls = action.get("class")
    new_sup = action.get("newSuperClass")
    if not (cls and new_sup):
        return False, "class/newSuperClass 누락"
    c = _resolve_prefixed_or_uri(cls, g)
    # 명명된 부모만 제거, 블랭크노드(Restriction)는 보존
    for _, _, parent in list(g.triples((c, RDFS.subClassOf, None))):
        if isinstance(parent, URIRef):
            g.remove((c, RDFS.subClassOf, parent))
    g.add((c, RDFS.subClassOf, _resolve_prefixed_or_uri(new_sup, g)))
    return True, "ok"


def _apply_delete_triple(g: Graph, action: dict) -> tuple[bool, str]:
    """s/p/o 트리플 삭제. object 생략 시 (s, p, *) 매칭 모든 트리플 제거.

    object 가 블랭크노드 리스트/Restriction 같은 구조(예: '[owl:Restriction; ...]')
    이면 **안전하게 스킵** — 블랭크노드 매칭은 별도 구현 필요.
    """
    subj = action.get("subject")
    pred = action.get("predicate")
    obj = action.get("object")
    if not (subj and pred):
        return False, "subject/predicate 누락"
    s = _resolve_prefixed_or_uri(subj, g)
    p = _resolve_prefixed_or_uri(pred, g)

    # object 생략 → (s, p, ?) 모두 제거 (단, 블랭크노드 object 는 보존)
    if obj is None:
        removed = 0
        for _, _, oo in list(g.triples((s, p, None))):
            if isinstance(oo, BNode):
                continue  # 복합 구조 보존
            g.remove((s, p, oo))
            removed += 1
        if removed == 0:
            return False, "해당 (subject, predicate) 트리플 없음"
        return True, f"ok ({removed} triples removed)"

    if isinstance(obj, str) and obj.lstrip().startswith("["):
        return False, "블랭크노드 object 는 현재 delete 미지원 (수동 검토 필요)"
    # object 는 URI 또는 리터럴. 예전엔 `":" in obj` 만 보고 IRI 로 해석해
    # `"true"^^xsd:string` 같은 **Turtle 리터럴 표기** 가 유령 IRI 가 됐고, 매칭이
    # 영구히 실패했다. 표기 차이(평문 vs @ko vs ^^xsd:string)도 흡수한다.
    from domain.graph_utils import match_object_in_graph, parse_object_term
    o = parse_object_term(obj, g)
    if o is None:
        return False, "object 값을 해석할 수 없음"
    matched = match_object_in_graph(g, s, p, o)
    if matched is None:
        return False, "해당 트리플이 존재하지 않음"

    # **마지막 domain/range 는 지우지 않는다.**
    #
    # ``rdfs:domain`` / ``rdfs:range`` 를 전부 제거하면 그 프로퍼티는 제약 없는
    # 껍데기가 된다 — A-Box 생성기는 domain 을 못 읽어 그 관계를 채우지 못하고
    # (``load_object_properties`` 가 도메인 NS 로 필터), conformance 검사도 그 축을
    # 건너뛰며, 중복 게이트는 집계에서 빼버린다. 즉 **선언은 남는데 아무도 보지
    # 않는 상태** 가 된다.
    #
    # 실측 (2026-08-14 S2): Jury 가 "같은 프로퍼티가 여러 domain-range 쌍에
    # 선언됐다" 고 오판해 ``remove_triple`` 12건을 요청했고, 그 결과
    # ``continuousCastingHasProduct`` / ``rollingHasProduct`` /
    # ``steelmakingHasProduct`` 3개가 domain·range 를 **완전히 잃었다** (R1 초안은
    # 0개였다). 실제로는 다중 선언이 아니었다 — Jury 가 T-Box 발췌만 보고 낸 오판이다.
    #
    # 교체 의도라면 ``modify_triple`` 을 쓰거나 add 를 먼저 하면 된다. 마지막 하나를
    # 지우는 요청은 거부하고 이유를 남긴다.
    if p in (RDFS.domain, RDFS.range):
        remaining = [
            x for x in g.objects(s, p)
            if x != matched and isinstance(x, URIRef)
        ]
        if not remaining:
            axis = "domain" if p == RDFS.domain else "range"
            return False, (
                f"마지막 rdfs:{axis} 삭제 거부 — 제거하면 {str(s).split(chr(35))[-1]} 가 "
                f"제약 없는 껍데기가 되어 A-Box 생성기·conformance 검사·중복 "
                f"게이트가 모두 그 프로퍼티를 무시한다. 교체 의도면 "
                f"modify_triple 을 쓰거나 새 {axis} 를 먼저 추가할 것"
            )

    g.remove((s, p, matched))
    return True, "ok"


def _apply_add_triple(g: Graph, action: dict) -> tuple[bool, str]:
    """Generic s/p/o add. Used by Jury for subclassOf, equivalentProperty,
    inverseOf and other declarations not covered by dedicated actions."""
    subj = action.get("subject")
    pred = action.get("predicate")
    obj = action.get("object")
    if not (subj and pred and obj):
        return False, "subject/predicate/object 누락"
    s = _resolve_prefixed_or_uri(subj, g)
    p = _resolve_prefixed_or_uri(pred, g)
    # `":" in obj` 만 보던 탓에 `"라벨"@ko` / `"true"^^xsd:string` 같은 Turtle
    # 리터럴 표기가 유령 IRI 가 됐다.
    from domain.graph_utils import clean_source_column, parse_object_term
    if p == _DCTERMS.source and isinstance(obj, str):
        # 이 값은 CSV 헤더와 직접 비교되므로 컬럼명만 남긴다.
        cleaned = clean_source_column(obj)
        if not cleaned:
            return False, "dcterms:source 값이 비어 기록하지 않음"
        o: URIRef | Literal = Literal(cleaned)
    else:
        o = parse_object_term(obj, g)
    if o is None:
        return False, "object 값을 해석할 수 없음"
    if (s, p, o) in g:
        return False, "이미 존재하는 트리플"
    g.add((s, p, o))
    return True, "ok"


def _apply_add_datatype_property(g: Graph, action: dict) -> tuple[bool, str]:
    """Add a DatatypeProperty with optional domain/range/label_en/label_ko."""
    prop = action.get("property")
    dom = action.get("domain")
    rng = action.get("range")
    if not prop:
        return False, "property 누락"
    if _is_reserved_local(prop):
        return False, (
            f"reserved local name '{prop}' — owlready2/HermiT 충돌 방지. "
            "더 도메인-특화된 이름(예: alarmType, equipmentLocation)으로 바꾸세요."
        )
    p = _resolve_prefixed_or_uri(prop, g)
    g.add((p, RDF.type, OWL.DatatypeProperty))
    if dom:
        g.add((p, RDFS.domain, _resolve_prefixed_or_uri(dom, g)))
    if rng:
        g.add((p, RDFS.range, _resolve_prefixed_or_uri(rng, g)))
    # CSV 컬럼 출처 — A-Box 가 컬럼↔DP 를 잇는 **확정 근거** 다 (Step 12e 게이트).
    # 이 경로는 모든 표기(source / dcterms:source / source_column / sourceColumn)를
    # 무시하고 버렸다 (실측: 5가지 표기 전부 `applied=1` 인데 기록은 0건). DSL 쪽
    # (multi_agent_tbox) 과 OP 쪽은 기록하는데 여기만 빠져 두 엔진의 계약이 갈렸다.
    source_col = _extract_source_column(action)
    if source_col:
        g.add((p, _DCTERMS.source, Literal(source_col)))
    comment = action.get("comment")
    if comment:
        g.add((p, RDFS.comment, Literal(comment, lang="ko")))
    _add_labels(g, p, action.get("label_en"), action.get("label_ko"))
    return True, "ok"


def _apply_add_subclass_batch(g: Graph, action: dict) -> tuple[bool, str]:
    """Add rdfs:subClassOf for multiple children under a single parent."""
    parent = action.get("parent")
    children = action.get("children") or []
    if not parent or not isinstance(children, list) or not children:
        return False, "parent/children 누락"
    p = _resolve_prefixed_or_uri(parent, g)
    added = 0
    for child in children:
        if not isinstance(child, str):
            continue
        c = _resolve_prefixed_or_uri(child, g)
        if (c, RDFS.subClassOf, p) not in g:
            g.add((c, RDFS.subClassOf, p))
            added += 1
    if added == 0:
        return False, "모든 children 이 이미 parent 의 하위"
    return True, f"ok ({added} children)"


def _apply_add_subclass_axioms(g: Graph, action: dict) -> tuple[bool, str]:
    """Jury 가 쓰는 단수형 batch: subjects list + 동일 predicate + object.

    e.g. {subjects: [A, B, C], predicate: rdfs:subClassOf, object: X}
    → (A, subClassOf, X), (B, subClassOf, X), (C, subClassOf, X).
    """
    subjects = action.get("subjects") or []
    pred = action.get("predicate", "rdfs:subClassOf")
    obj = action.get("object")
    if not isinstance(subjects, list) or not subjects or not obj:
        return False, "subjects list / object 누락"
    p = _resolve_prefixed_or_uri(pred, g)
    o = _resolve_prefixed_or_uri(obj, g)
    added = 0
    for s_raw in subjects:
        if not isinstance(s_raw, str):
            continue
        s = _resolve_prefixed_or_uri(s_raw, g)
        if (s, p, o) not in g:
            g.add((s, p, o))
            added += 1
    if added == 0:
        return False, "모든 axiom 이 이미 존재"
    return True, f"ok ({added} axioms added)"


def _apply_delete_class(g: Graph, action: dict) -> tuple[bool, str]:
    """class 선언 + 관련 모든 트리플 제거 (remove_class 의 별칭).

    Jury 가 delete_class / remove_class 둘 다 쓰므로 alias 로 지원.
    """
    return _apply_remove_class(g, action)


def _apply_add_triples(g: Graph, action: dict) -> tuple[bool, str]:
    """여러 triple 을 일괄 추가. {subject, predicate, object} list 기대."""
    triples = action.get("triples") or []
    if not isinstance(triples, list) or not triples:
        return False, "triples list 누락"
    added = 0
    skipped = 0
    for t in triples:
        if not isinstance(t, dict):
            continue
        subj = t.get("subject")
        pred = t.get("predicate")
        obj = t.get("object")
        if not (subj and pred and obj):
            continue
        s = _resolve_prefixed_or_uri(subj, g)
        p = _resolve_prefixed_or_uri(pred, g)
        if isinstance(obj, str) and (obj.startswith(("http", "urn:")) or ":" in obj):
            o: URIRef | Literal = _resolve_prefixed_or_uri(obj, g)
        else:
            o = Literal(obj)
        if (s, p, o) in g:
            skipped += 1
            continue
        g.add((s, p, o))
        added += 1
    if added == 0:
        return False, f"추가된 triple 없음 (skipped {skipped})"
    return True, f"ok ({added} added, {skipped} already present)"


def _apply_remove_all_restrictions(g: Graph, action: dict) -> tuple[bool, str]:
    """class 의 subClassOf Restriction 중 onProperty 가 property 인 것을 모두 제거.

    property 가 빠지면 해당 class 의 모든 Restriction 을 제거.
    블랭크노드와 그 하위 트리플을 함께 삭제.
    """
    cls = action.get("class")
    prop = action.get("property")  # optional
    if not cls:
        return False, "class 누락"
    c = _resolve_prefixed_or_uri(cls, g)
    target_prop = _resolve_prefixed_or_uri(prop, g) if prop else None
    removed = 0
    for _, _, parent in list(g.triples((c, RDFS.subClassOf, None))):
        if not isinstance(parent, BNode):
            continue
        if (parent, RDF.type, OWL.Restriction) not in g:
            continue
        if target_prop is not None:
            on_prop = g.value(parent, OWL.onProperty)
            if on_prop != target_prop:
                continue
        # 블랭크노드의 모든 트리플 제거
        for _p, _o in list(g.predicate_objects(parent)):
            g.remove((parent, _p, _o))
        g.remove((c, RDFS.subClassOf, parent))
        removed += 1
    if removed == 0:
        return False, "제거할 Restriction 없음"
    return True, f"ok ({removed} restrictions removed)"


def _apply_remove_annotations(g: Graph, action: dict) -> tuple[bool, str]:
    """rdfs:label / rdfs:comment 같은 annotation 을 선택적으로 제거.

    targets=[{subject, predicate, value, [lang]}] 형식. value+lang 매칭이면
    정확히 그 Literal 만 제거, 그렇지 않으면 str 동등 비교.
    동일 언어 태그 annotation 이 여러 개일 때 **첫 1개만 유지**하고 나머지를
    제거하는 모드는 예약 (현재 단순 매칭).
    """
    targets = action.get("targets") or []
    if not isinstance(targets, list) or not targets:
        return False, "targets list 누락"
    removed = 0
    for t in targets:
        if not isinstance(t, dict):
            continue
        subj = t.get("subject")
        pred = t.get("predicate")
        value = t.get("value")
        lang = t.get("lang")
        if not (subj and pred and value is not None):
            continue
        s = _resolve_prefixed_or_uri(subj, g)
        p = _resolve_prefixed_or_uri(pred, g)
        # 'foo@ko' 형식이면 lang 추출
        if isinstance(value, str) and "@" in value and lang is None:
            base, _, tag = value.rpartition("@")
            if len(tag) <= 5 and tag.replace("-", "").isalpha():
                value, lang = base, tag
        for _, _, obj in list(g.triples((s, p, None))):
            if not isinstance(obj, Literal):
                continue
            obj_lang = getattr(obj, "language", None)
            if lang is not None and obj_lang != lang:
                continue
            if str(obj) != str(value):
                continue
            g.remove((s, p, obj))
            removed += 1
    if removed == 0:
        return False, "매칭되는 annotation 없음"
    return True, f"ok ({removed} annotations removed)"


def _apply_keep_no_domain(g: Graph, action: dict) -> tuple[bool, str]:
    """property 의 rdfs:domain 트리플을 모두 제거 (domain 제약 해제)."""
    prop = action.get("property")
    if not prop:
        return False, "property 누락"
    p = _resolve_prefixed_or_uri(prop, g)
    removed = 0
    for _, _, dom in list(g.triples((p, RDFS.domain, None))):
        g.remove((p, RDFS.domain, dom))
        removed += 1
    if removed == 0:
        return False, "해당 property 에 domain 트리플 없음"
    return True, f"ok ({removed} domain triples removed)"


def _apply_remove_class(g: Graph, action: dict) -> tuple[bool, str]:
    """owl:Class 선언 및 관련 모든 트리플 제거.

    subject/object 어느 쪽에서든 해당 class URI 를 참조하는 트리플 전부.
    이 class 를 domain/range 로 쓰는 프로퍼티는 그대로 유지됨 (프로퍼티 자체는
    보존). 호출자가 원하면 그 프로퍼티들도 별도 action 으로 제거할 것.
    """
    cls = action.get("class") or action.get("name")
    if not cls:
        return False, "class/name 누락"
    c = _resolve_prefixed_or_uri(cls, g)
    removed = 0
    for _, pp, oo in list(g.triples((c, None, None))):
        g.remove((c, pp, oo))
        removed += 1
    for ss, pp, _ in list(g.triples((None, None, c))):
        g.remove((ss, pp, c))
        removed += 1
    if removed == 0:
        return False, "해당 class 에 대한 트리플 없음"
    return True, f"ok ({removed} triples removed)"


def _apply_add_axiom(g: Graph, action: dict) -> tuple[bool, str]:
    """Jury 가 'add_axiom' naming 으로 보내는 axiom 을 dispatch.

    axiom_type=AllDisjointClasses 면 기존 add_disjoint_classes 로 위임.
    axiom_type=EquivalentClasses 는 pairwise equivalent 로 처리.
    """
    at = (action.get("axiom_type") or "").lower().replace("_", "")
    if at in ("alldisjointclasses", "disjointclasses"):
        return _apply_add_disjoint_classes(g, action)
    if at == "equivalentclasses":
        members = action.get("members") or []
        if not isinstance(members, list) or len(members) < 2:
            return False, "equivalentClasses: members list(>=2) 누락"
        uris = [_resolve_prefixed_or_uri(m, g) for m in members]
        added = 0
        for i in range(len(uris)):
            for j in range(i + 1, len(uris)):
                if (uris[i], OWL.equivalentClass, uris[j]) not in g:
                    g.add((uris[i], OWL.equivalentClass, uris[j]))
                    added += 1
        if added == 0:
            return False, "이미 동등 관계가 모두 선언됨"
        return True, f"ok ({added} equivalentClass pairs)"
    return False, f"미지원 axiom_type: {action.get('axiom_type')}"


def _apply_remove_object_property(g: Graph, action: dict) -> tuple[bool, str]:
    """ObjectProperty 선언 및 그에 딸린 모든 axiom 을 제거.

    rdf:type, rdfs:domain, rdfs:range, owl:inverseOf, rdfs:label/comment,
    subPropertyOf 등 property URI 를 subject 또는 object 로 하는 모든 트리플 제거.
    해당 property 를 참조하는 Restriction 의 onProperty 도 함께 제거된다.
    """
    prop = action.get("property")
    if not prop:
        return False, "property 누락"
    p = _resolve_prefixed_or_uri(prop, g)
    removed = 0
    for _, pp, oo in list(g.triples((p, None, None))):
        g.remove((p, pp, oo))
        removed += 1
    for ss, pp, _ in list(g.triples((None, None, p))):
        g.remove((ss, pp, p))
        removed += 1
    if removed == 0:
        return False, "해당 property 에 대한 트리플 없음"
    return True, f"ok ({removed} triples removed)"


def _apply_rename_uri(g: Graph, action: dict) -> tuple[bool, str]:
    """그래프 내 URI 를 새 URI 로 일괄 교체 (subject, predicate, object 모두).

    from_uri/to_uri 는 prefixed 또는 절대 URI. invalid_uri_format 같은 이중
    prefix URI 를 정상 URI 로 고치는 용도.
    """
    from_raw = action.get("from")
    to_raw = action.get("to")
    if not (from_raw and to_raw):
        return False, "from/to 누락"
    # 양쪽 모두 같은 해석기를 쓴다. 예전엔 ``from`` 만 도메인 NS 를 하드코딩해
    # 비대칭이었고, ``from="iof-core:MaterialArtifact"`` 가 IOF 리소스가 아니라
    # 동명의 **도메인 클래스** 를 개명해 엉뚱한 엔티티를 바꿨다 (실측).
    from_uri = _resolve_prefixed_or_uri(from_raw, g)
    to_uri = _resolve_prefixed_or_uri(to_raw, g)
    count = 0
    for s, p, o in list(g.triples((from_uri, None, None))):
        g.remove((s, p, o))
        g.add((to_uri, p, o))
        count += 1
    for s, p, o in list(g.triples((None, from_uri, None))):
        g.remove((s, p, o))
        g.add((s, to_uri, o))
        count += 1
    for s, p, o in list(g.triples((None, None, from_uri))):
        g.remove((s, p, o))
        g.add((s, p, to_uri))
        count += 1
    if count == 0:
        return False, "해당 URI 를 참조하는 트리플 없음"
    return True, f"ok ({count} triples remapped)"


def _apply_modify_triples(g: Graph, action: dict) -> tuple[bool, str]:
    """기존 (s, p, old_object) 트리플을 (s, p, new_object) 로 교체.

    지원 포맷:
    - changes=[{subject, predicate(optional, default=rdfs:subClassOf),
                old_object, new_object}, ...]  (복수형)
    - {subject, predicate, old_object, new_object}  (Jury 가 단수형
      `modify_triple` 로 전달하는 경우)
    """
    changes = action.get("changes")
    # 단수형 지원: action 자체가 단일 change 스펙
    if not changes and "subject" in action and (
        "old_object" in action or "new_object" in action
    ):
        changes = [{
            "subject": action.get("subject"),
            "predicate": action.get("predicate", "rdfs:subClassOf"),
            "old_object": action.get("old_object"),
            "new_object": action.get("new_object"),
        }]
    if not isinstance(changes, list) or not changes:
        return False, "changes list 또는 subject+old_object+new_object 필요"
    replaced = 0
    for ch in changes:
        if not isinstance(ch, dict):
            continue
        subj = ch.get("subject")
        old_obj = ch.get("old_object")
        new_obj = ch.get("new_object")
        pred = ch.get("predicate", "rdfs:subClassOf")
        if not (subj and old_obj and new_obj):
            continue
        s = _resolve_prefixed_or_uri(subj, g)
        p = _resolve_prefixed_or_uri(pred, g)
        # old/new object 를 **무조건 IRI** 로 해석하던 탓에 리터럴 교체가 아예
        # 불가능했다. 실측: Jury 가 owl:deprecated 를 "true"^^xsd:string →
        # xsd:boolean 으로 고치려 했으나 양쪽이 유령 IRI 가 되어 "교체할 트리플
        # 없음" 으로 끝났고, deprecated 선언이 추론기에 인식되지 않는 타입으로 남았다.
        from domain.graph_utils import match_object_in_graph, parse_object_term
        old_o = parse_object_term(old_obj, g)
        new_o = parse_object_term(new_obj, g)
        if old_o is None or new_o is None:
            continue
        matched = match_object_in_graph(g, s, p, old_o)
        if matched is not None:
            g.remove((s, p, matched))
            g.add((s, p, new_o))
            replaced += 1
    if replaced == 0:
        return False, "교체할 트리플 없음"
    return True, f"ok ({replaced} triples replaced)"


def _apply_fix_namespace_bulk(g: Graph, action: dict) -> tuple[bool, str]:
    """이중 접두사 URI 일괄 교체.

    Architect 가 `<http://domain#steel:X>` 같은 이중 접두사 URI 를 만들면 정상
    `steel:X` 와 별개 리소스가 된다. 이 핸들러는 그래프의 모든 URIRef 를 훑어
    DOMAIN_NS 네임스페이스 prefix 를 포함하면서 steel: prefix 가 **내부에** 들어간
    형태(`<http://domain#steel:Local>`, `<http://domain#owl:Thing>` 포함)를 찾아
    올바른 URI 로 rewrite 한다.

    지원 패턴 — 콜론 앞이 **알려진 prefix 이름** 인 경우 전부:
    - `<DOMAIN_NS + "<domain-prefix>:" + Local>` → `<DOMAIN_NS + Local>`
    - `<DOMAIN_NS + "iof-core:" + Local>`        → IOF Core (외래 매핑 복구)
    - `<DOMAIN_NS + "owl:"/"rdfs:"/"rdf:"/"xsd:"/"dcterms:"/… + Local>` → 해당 NS

    예전 구현은 ``prefix_map`` 에 도메인 prefix 를 리터럴 ``"steel:"`` 로 박아
    **다른 도메인에서는 아무 것도 복구하지 못했고** (prefix=med 인 설정에서
    ``<…#med:Ghost>`` 가 그대로 남는 것을 실측), 외래 prefix 는 표에 없어
    ``<…#iof-core:X>`` 유령을 통과시켰다. 이제 판정과 복구 대상 네임스페이스를
    :func:`domain.graph_utils.split_embedded_prefix` 가 결정한다.
    """
    from domain.graph_utils import split_embedded_prefix

    rewrite_map: dict[URIRef, URIRef] = {}
    for node in set(g.subjects()) | set(g.predicates()) | set(g.objects()):
        if not isinstance(node, URIRef):
            continue
        split = split_embedded_prefix(str(node), g)
        if split is None:
            continue
        namespace, local = split
        rewrite_map[node] = URIRef(namespace + local)
    if not rewrite_map:
        return False, "이중 접두사 URI 없음"

    replaced = 0
    for old, new in rewrite_map.items():
        triples_s = list(g.triples((old, None, None)))
        for s, p, o in triples_s:
            g.remove((s, p, o))
            g.add((new, p, o))
            replaced += 1
        triples_o = list(g.triples((None, None, old)))
        for s, p, o in triples_o:
            g.remove((s, p, o))
            g.add((s, p, new))
            replaced += 1
        triples_p = list(g.triples((None, old, None)))
        for s, p, o in triples_p:
            g.remove((s, p, o))
            g.add((s, new, o))
            replaced += 1
    return True, f"ok ({len(rewrite_map)} URIs rewritten, {replaced} triples touched)"


def _apply_split_property(g: Graph, action: dict) -> tuple[bool, str]:
    """다중 domain 선언된 프로퍼티(PK/FK 혼용)를 domain 별로 분리.

    Jury 가 제공하는 필드:
    - target: 분리 대상 프로퍼티 (e.g. "steel:sampleId")
    - splits (우선) 또는 description 파싱: 리스트 of {"name", "domain", "type"}
      type ∈ "FunctionalProperty" | "ObjectProperty" | "DatatypeProperty"
      (optional: "range", "label_ko", "label_en")

    splits 가 없으면 실패 반환 (free-text description 파싱은 LLM 의존).
    Jury prompt 에 splits 필드 요청하도록 변경 필요 — 이번엔 fallback 으로
    delete 만 수행하고 나머지 add_object_property / add_functional_property 는
    별도 action 으로 오도록 skipped 처리.
    """
    from domain.graph_utils import clean_source_column

    target = action.get("target")
    splits = action.get("splits")
    if not target:
        return False, "target 누락"
    target_uri = _resolve_prefixed_or_uri(target, g)
    if not splits:
        # 원본 target 의 모든 트리플 삭제 + skipped 안내.
        triples = list(g.triples((target_uri, None, None)))
        if not triples:
            return False, f"대상 프로퍼티 {target} 이 그래프에 없음"
        for s, p, o in triples:
            g.remove((s, p, o))
        return True, (
            f"ok (deleted {len(triples)} triples; splits 필드 누락으로 신규 "
            "프로퍼티는 생성 안 됨 — Architect 가 add_functional_property / "
            "add_object_property 로 보강 필요)"
        )
    # splits 가 제공된 경우 — 신규 프로퍼티 선언 + 원본 제거.
    # 원본의 CSV 출처는 삭제 전에 확보해 각 split 에 물려준다.
    original_source = ""
    for _o in g.objects(target_uri, _DCTERMS.source):
        cleaned = clean_source_column(str(_o))
        if cleaned:
            original_source = cleaned
            break
    created = 0
    for sp in splits:
        if not isinstance(sp, dict):
            continue
        name = sp.get("name")
        domain = sp.get("domain")
        ptype = sp.get("type", "FunctionalProperty")
        if not (name and domain):
            continue
        new_uri = _resolve_prefixed_or_uri(name, g)
        dom_uri = _resolve_prefixed_or_uri(domain, g)
        if ptype == "FunctionalProperty":
            g.add((new_uri, RDF.type, OWL.FunctionalProperty))
            g.add((new_uri, RDF.type, OWL.DatatypeProperty))
        elif ptype == "ObjectProperty":
            g.add((new_uri, RDF.type, OWL.ObjectProperty))
        elif ptype == "DatatypeProperty":
            g.add((new_uri, RDF.type, OWL.DatatypeProperty))
        g.add((new_uri, RDFS.domain, dom_uri))
        if "range" in sp:
            rng_uri = _resolve_prefixed_or_uri(sp["range"], g)
            g.add((new_uri, RDFS.range, rng_uri))
        # CSV 출처를 물려준다. split 은 **같은 컬럼** 을 domain 별로 쪼개는 작업이라
        # (PK/FK 혼용 프로퍼티 분리) 각 조각이 원본과 같은 컬럼에서 온다. 아래에서
        # 원본을 삭제하므로 여기서 넘기지 않으면 출처가 통째로 사라진다 — A-Box 는
        # 그 컬럼을 이름 추측으로만 찾게 된다. split 별 명시가 있으면 그것을 우선.
        #
        # **단 상속은 domain 이 서로 다를 때만 안전하다.** A-Box 출처 인덱스는
        # ``(domain_class, UPPER(column))`` 키라, 같은 domain 의 split 두 개가 같은
        # 컬럼을 주장하면 어느 쪽이 값을 받을지 비결정적이다 (실측 2026-08-11:
        # domain 이 모두 ``A`` 인 split 2개가 둘 다 ``COL_X`` 를 물려받았다).
        # 그 경우 상속을 생략해 A-Box 가 이름 근사 폴백을 타게 한다 — 조용한
        # 오적재보다 폴백이 낫다. split 별 명시 출처는 의도된 값이므로 그대로 쓴다.
        explicit_source = _extract_source_column(sp)
        split_source = explicit_source
        if not split_source and original_source:
            same_domain_rivals = sum(
                1 for other in splits
                if isinstance(other, dict)
                and other is not sp
                and not _extract_source_column(other)
                and other.get("domain") == domain
            )
            if same_domain_rivals:
                logger.warning(
                    "split_property: %s 의 split 중 domain=%s 가 %d개 더 있어 "
                    "출처 '%s' 상속을 생략한다 — 같은 (class, column) 키를 여러 DP "
                    "가 주장하면 A-Box 가 어느 쪽에 값을 넣을지 비결정적이다",
                    target, domain, same_domain_rivals, original_source,
                )
            else:
                split_source = original_source
        if split_source:
            g.add((new_uri, _DCTERMS.source, Literal(split_source)))
        _add_labels(g, new_uri, sp.get("label_en"), sp.get("label_ko"))
        created += 1
    # 원본 삭제
    for s, p, o in list(g.triples((target_uri, None, None))):
        g.remove((s, p, o))
    return True, f"ok (created {created} split properties, original {target} removed)"


def _apply_replace_triple(g: Graph, action: dict) -> tuple[bool, str]:
    """단일 트리플 교체. modify_triple 의 명시적 alias (Jury prompt 파라미터 호환).

    필드: target, predicate, old_object, new_object
    """
    return _apply_modify_triples(g, {
        "changes": [{
            "subject": action.get("target"),
            "predicate": action.get("predicate"),
            "old_object": action.get("old_object"),
            "new_object": action.get("new_object"),
        }],
    })


#: 프로퍼티 특성 action → 부여할 OWL 타입.
#: 프롬프트(``multi_agent_prompts.py``)가 세 action 을 모두 지시하는데 예전에는
#: functional 하나만 핸들러가 있었다 — 나머지 둘은 레지스트리에 없어 Jury 가
#: 지시해도 조용히 버려졌다.
_PROPERTY_CHARACTERISTICS = {
    "add_functional_property": OWL.FunctionalProperty,
    "add_inverse_functional_property": OWL.InverseFunctionalProperty,
    "add_transitive_property": OWL.TransitiveProperty,
    "add_symmetric_property": OWL.SymmetricProperty,
}

#: 프로퍼티 이름이 실릴 수 있는 필드 — **프롬프트가 지시하는 이름을 먼저** 본다.
#:
#: 2026-08-19 실측: 프롬프트는 ``{{name}}`` 을 지시하는데(multi_agent_prompts.py
#: 282~284, 303행 예시 포함) 핸들러는 ``target`` 만 읽어, Jury 가 규칙대로 채운
#: ``name`` 이 전부 버려졌다 — S2 한 라운드에서 required_fixes 19건이
#: "target 누락" 으로 실패했다 (128건 중 실효 적용 73%). LLM 잘못이 아니라
#: 프롬프트-코드 계약 불일치다. 이 리포에서 반복된 유형이므로(지시가 조용히
#: 폐기됨) 양쪽 이름을 모두 받아들이고, 프롬프트 쪽을 정본으로 둔다.
_PROPERTY_NAME_FIELDS = ("name", "target", "property", "properties")


def _apply_property_characteristic(
    g: Graph, action: dict, owl_type=None,
) -> tuple[bool, str]:
    """프로퍼티에 OWL 특성(Functional/InverseFunctional/Transitive/Symmetric) 부여.

    이름 필드는 ``name`` / ``target`` / ``property`` / ``properties`` 를 모두 받고,
    문자열이면 comma-split 한다 (Jury 는 "steel:planId, steel:resultId, ..." 형태로
    여러 개를 한꺼번에 넘기는 경향). 리스트도 그대로 처리한다.

    ``owl_type`` 이 None 이면 ``action["action"]`` 으로 조회한다 — 레지스트리가
    ``functools.partial`` 없이 같은 함수를 여러 action 에 매핑할 수 있게 한다.
    """
    if owl_type is None:
        raw_name = str(action.get("action", ""))
        owl_type = _PROPERTY_CHARACTERISTICS.get(raw_name.lower())
        if owl_type is None:
            # 표기 변종(camelCase 등)을 정규형으로 접어 한 번 더 조회한다.
            # ``apply_jury_fixes`` 는 진입 시 normalize 를 돌리므로 그 경로로는
            # 이미 정규형이 오지만, 이 핸들러를 **직접** 호출하는 코드(테스트/
            # 다른 스텝)가 원본 표기를 넘길 수 있다. 조회 실패 시 조용히
            # FunctionalProperty 로 떨어지면 transitive 지시가 의미가 다른 공리로
            # 바뀐다 — 종류를 못 맞히면 부여하지 않고 실패로 보고한다.
            try:
                from tools.jury_action_normalizer import canonical_action_name
                owl_type = _PROPERTY_CHARACTERISTICS.get(
                    str(canonical_action_name(raw_name)).lower(),
                )
            except Exception:  # noqa: BLE001 — 정규화 실패는 판정 불가로 취급
                owl_type = None
        if owl_type is None:
            return False, (
                f"프로퍼티 특성 종류를 판별할 수 없다: action={raw_name!r}. "
                f"지원: {sorted(_PROPERTY_CHARACTERISTICS)}"
            )

    raw = None
    for field in _PROPERTY_NAME_FIELDS:
        val = action.get(field)
        if val:
            raw = val
            break
    if not raw:
        return False, (
            f"프로퍼티 이름 누락 — {list(_PROPERTY_NAME_FIELDS)} 중 하나가 필요하다"
        )

    if isinstance(raw, list | tuple | set):
        names = [str(x).strip() for x in raw if str(x).strip()]
    else:
        names = [t.strip() for t in str(raw).split(",") if t.strip()]

    label = str(owl_type).split("#")[-1]
    added = 0
    for n in names:
        uri = _resolve_prefixed_or_uri(n, g)
        if (uri, RDF.type, owl_type) not in g:
            g.add((uri, RDF.type, owl_type))
            added += 1
    if added == 0:
        # **"이미 선언" 마커를 쓴다.** 이 사유는 결함이 아니라 no-op 이다 —
        # ``_NOOP_MARKERS`` 와 문구가 어긋나면 중복 지시가 ``failed`` 로 집계돼
        # 실패율이 부풀고, 게이트가 정상 동작을 결함으로 보고한다.
        return False, f"이미 선언됨 ({label}) — 대상 {len(names)}개 모두"
    return True, f"ok ({added} properties marked {label})"


def _apply_add_functional_property(g: Graph, action: dict) -> tuple[bool, str]:
    """owl:FunctionalProperty 부여 (하위 호환 유지 — 기존 호출자/테스트용)."""
    return _apply_property_characteristic(g, action, OWL.FunctionalProperty)


def _apply_add_class_hierarchy(g: Graph, action: dict) -> tuple[bool, str]:
    """중간 추상 클래스 계층 추가.

    필드:
    - hierarchy (권장): list of {"parent", "children": [...]}
      각 child 는 rdfs:subClassOf parent. parent 는 owl:Class 로 선언.
    - fallback: description 만 주어지면 skipped (LLM free-text 파싱 불가).
    """
    hierarchy = action.get("hierarchy")
    if not hierarchy or not isinstance(hierarchy, list):
        return False, "hierarchy 리스트 누락 — description 기반 자동 생성 불가"
    added = 0
    for entry in hierarchy:
        if not isinstance(entry, dict):
            continue
        parent = entry.get("parent")
        children = entry.get("children") or []
        if not (parent and children):
            continue
        p_uri = _resolve_prefixed_or_uri(parent, g)
        if (p_uri, RDF.type, OWL.Class) not in g:
            g.add((p_uri, RDF.type, OWL.Class))
            added += 1
        for child in children:
            c_uri = _resolve_prefixed_or_uri(child, g)
            if (c_uri, RDFS.subClassOf, p_uri) not in g:
                g.add((c_uri, RDFS.subClassOf, p_uri))
                added += 1
    if added == 0:
        return False, "hierarchy 에서 추가할 엔티티 없음"
    return True, f"ok ({added} class hierarchy triples added)"


def _apply_add_iof_mappings(g: Graph, action: dict) -> tuple[bool, str]:
    """IOF 표준 클래스 매핑 추가.

    필드:
    - mappings (권장): list of {"class", "iof_class", "relation"?}
      relation 기본값 "rdfs:subClassOf". iof_class 는 IOF IRI 그대로.
    """
    mappings = action.get("mappings")
    if not mappings or not isinstance(mappings, list):
        return False, "mappings 리스트 누락 — description 기반 자동 생성 불가"
    added = 0
    for m in mappings:
        if not isinstance(m, dict):
            continue
        cls = m.get("class")
        iof = m.get("iof_class")
        rel = m.get("relation", "rdfs:subClassOf")
        if not (cls and iof):
            continue
        c_uri = _resolve_prefixed_or_uri(cls, g)
        iof_uri = _resolve_prefixed_or_uri(iof, g)
        rel_uri = _resolve_prefixed_or_uri(rel, g)
        if (c_uri, rel_uri, iof_uri) not in g:
            g.add((c_uri, rel_uri, iof_uri))
            added += 1
    if added == 0:
        return False, "mappings 에서 추가할 매핑 없음 (이미 존재 또는 필수 필드 누락)"
    return True, f"ok ({added} IOF mappings added)"


def _apply_remove_duplicate(g: Graph, action: dict) -> tuple[bool, str]:
    """의미 중복 프로퍼티 정리. target 리스트의 모든 트리플 삭제 (첫번째는 유지).

    Jury 가 주는 target 은 단일 문자열 또는 comma 분리 리스트. 모호하므로
    **전부 삭제**: 중복이라는 의미는 "하나만 남겨라" 지만 어떤게 canonical 인지
    결정 불가. 전부 지우고 Architect 에게 canonical 재선언 요구.
    """
    target = action.get("target", "")
    if not target:
        return False, "target 누락"
    names = [t.strip() for t in str(target).split(",") if t.strip()]
    total = 0
    for n in names:
        uri = _resolve_prefixed_or_uri(n, g)
        triples = list(g.triples((uri, None, None))) + list(g.triples((None, None, uri)))
        for s, p, o in triples:
            g.remove((s, p, o))
            total += 1
    if total == 0:
        return False, f"대상 프로퍼티({target}) 트리플 없음"
    return True, f"ok (removed {total} triples for {len(names)} duplicates)"


_DISPATCH = {
    "add_object_property": _apply_add_object_property,
    "add_inverse_property": _apply_add_inverse_property,
    "add_restriction": _apply_add_restriction,
    "add_disjoint_classes": _apply_add_disjoint_classes,
    "add_class": _apply_add_class,
    "add_subclass": _apply_add_subclass,
    "add_subclass_batch": _apply_add_subclass_batch,
    "add_subclass_axioms": _apply_add_subclass_axioms,
    "add_triple": _apply_add_triple,
    "add_triples": _apply_add_triples,
    "delete_class": _apply_delete_class,
    "add_datatype_property": _apply_add_datatype_property,
    "modify_subclass": _apply_modify_subclass,
    "modify_triples": _apply_modify_triples,
    # Jury 가 단수형 naming 을 쓰는 경우의 별칭 (동일 핸들러가 단건/배치 모두 처리)
    "modify_triple": _apply_modify_triples,
    "rename_uri": _apply_rename_uri,
    "delete_triple": _apply_delete_triple,
    # remove_triple 은 delete_triple 의 별칭 (jury naming 호환)
    "remove_triple": _apply_delete_triple,
    "remove_all_restrictions": _apply_remove_all_restrictions,
    "remove_annotations": _apply_remove_annotations,
    "remove_object_property": _apply_remove_object_property,
    "remove_class": _apply_remove_class,
    "add_axiom": _apply_add_axiom,
    "keep_no_domain": _apply_keep_no_domain,
    # Jury LLM 이 underscore-less / camelCase 로 보내는 variant 도 수용한다.
    # 이전 run 관찰: add_objectproperty 5건, delete_objectproperty 1건이
    # skipped_actions 으로 빠져 hasItemMaster/hasProductMaster/hasMonitoringPointMaster
    # 같은 FK OP 가 T-Box 에 누락된 채 A-Box 가 생성됨 → S9 CW master 고립
    # 60.89% regression. DSL 스펙 고정은 LLM 이 어차피 지키지 않으므로 alias 수용.
    "add_objectproperty": _apply_add_object_property,
    "addObjectProperty": _apply_add_object_property,
    "add_datatypeproperty": _apply_add_datatype_property,
    "addDatatypeProperty": _apply_add_datatype_property,
    "delete_objectproperty": _apply_remove_object_property,
    "deleteObjectProperty": _apply_remove_object_property,
    "remove_objectproperty": _apply_remove_object_property,
    "add_inverseproperty": _apply_add_inverse_property,
    "addInverseProperty": _apply_add_inverse_property,
    "add_disjointclasses": _apply_add_disjoint_classes,
    "addDisjointClasses": _apply_add_disjoint_classes,
    "delete_class_triple": _apply_delete_triple,
    # R28 신규: Jury skipped_actions 에 반복 등장하던 것들.
    "fix_namespace_bulk": _apply_fix_namespace_bulk,
    "fix_namespace": _apply_fix_namespace_bulk,  # 동일 엔진 (부분 집합 처리)
    "split_property": _apply_split_property,
    "replace_triple": _apply_replace_triple,
    "remove_duplicate": _apply_remove_duplicate,
    "add_class_hierarchy": _apply_add_class_hierarchy,
    "add_iof_mappings": _apply_add_iof_mappings,
    "add_functional_property": _apply_property_characteristic,
    # 프롬프트(multi_agent_prompts.py 283~284)가 지시하는데 레지스트리에 없어서
    # Jury 가 지시해도 조용히 버려졌다. owl_type 은 action 이름으로 조회된다.
    "add_inverse_functional_property": _apply_property_characteristic,
    "add_transitive_property": _apply_property_characteristic,
    "add_symmetric_property": _apply_property_characteristic,
    # R28 D: LLM 이 매번 발명하는 action 이름의 별칭. 의미가 명확히 동일한
    # 것들은 기존 핸들러로 라우팅. 의미가 다르거나(verify_ttl_syntax 검증 전용)
    # 안전하게 처리 못하는 것은 _apply_noop_ack 으로 "수용은 했으나 수행 안함"
    # 을 applied 로 마크해 skipped_actions 리스트에서 빠지게 한다.
    "replace_iri_batch": _apply_fix_namespace_bulk,
    "fix_iri_prefix": _apply_fix_namespace_bulk,
    "fix_iri_prefix_in_all_restrictions": _apply_fix_namespace_bulk,
    "rename_namespace": _apply_fix_namespace_bulk,
    "rewrite_uri_batch": _apply_fix_namespace_bulk,
    "add_intermediate_classes": _apply_add_class_hierarchy,
    "add_abstract_classes": _apply_add_class_hierarchy,
    "introduce_abstract_hierarchy": _apply_add_class_hierarchy,
    "remove_resource": _apply_delete_class,
    "delete_resource": _apply_delete_class,
    "delete_property": _apply_remove_object_property,
    "remove_property": _apply_remove_object_property,
    "add_iof_mapping": _apply_add_iof_mappings,
    "link_to_iof": _apply_add_iof_mappings,
    "deduplicate_property": _apply_remove_duplicate,
    "verify_ttl_syntax": None,  # 검증 전용 — _noop 로 처리
    "validate_ttl": None,
    "run_validation": None,
}


def _apply_noop_ack(g: Graph, action: dict) -> tuple[bool, str]:
    """실행할 그래프 변경이 없는 Jury action 을 수용만 한다.

    예: verify_ttl_syntax 같이 "검증 후 다음 단계" 지시. jury_fixes 는 검증을 하지
    않으며 (상위 파이프라인이 validate_ttl_syntax 로 수행), skipped 로 두면 사용자
    혼란. "ack: handled by downstream pipeline" 로 applied 마크.
    """
    return True, "ack (downstream pipeline 이 자동 수행, jury_fixes 단계 no-op)"


# None 으로 표시된 핸들러를 _apply_noop_ack 로 치환.
for _k, _v in list(_DISPATCH.items()):
    if _v is None:
        _DISPATCH[_k] = _apply_noop_ack


def apply_jury_fixes(ttl: str, required_fixes: list[dict]) -> dict[str, Any]:
    """required_fixes 를 TTL 에 적용. 성공/실패/미지원 건을 구분해 리턴.

    Args:
        ttl: 현재 T-Box TTL 문자열.
        required_fixes: Jury 또는 Architect compromise 가 돌려준 action dict 리스트.

    입력 action 은 **먼저 정규화**된다 (``jury_action_normalizer``). LLM 이 쓰는
    키 표기(``uri`` / ``filler`` / ``restriction_type``)와 핸들러가 읽는 키
    (``class`` / ``someValuesFrom`` / ``type``)가 달라 2026-08-10 실행에서
    required_fixes 41건 중 **37건이 폐기**됐기 때문이다.

    Returns:
        {
          "ttl": 수정된 TTL,
          "applied": [{"action": ..., "status": "ok"}],
          "skipped": [{"action": ..., "reason": "..."}],
          "failed":  [{"action": ..., "reason": "..."}],
          "noop":    [{"action": ..., "reason": "..."}],  # 이미 원하는 상태
        }

        ``noop`` 은 ``failed`` 와 **구분**한다. "이미 선언돼 있다" 를 실패로 세면
        운영자가 적용률을 판단할 수 없고, 실제로 그래서 41건 중 얼마가 진짜
        결함인지 알 수 없었다.
    """
    from tools.jury_action_normalizer import normalize_jury_actions

    applied: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []
    noop: list[dict] = []

    if not required_fixes:
        return {"ttl": ttl, "applied": applied, "skipped": skipped,
                "failed": failed, "noop": noop}

    required_fixes, rejected = normalize_jury_actions(required_fixes)
    failed.extend(rejected)

    try:
        g = _new_graph()
        g.parse(data=ttl, format="turtle")
    except Exception as e:
        logger.error("TTL 파싱 실패로 jury fixes 전체 스킵: %s", e)
        return {
            "ttl": ttl, "applied": [], "skipped": [
                {"action": a, "reason": f"ttl parse error: {e}"} for a in required_fixes
            ],
            "failed": [], "noop": [],
        }

    for action in required_fixes:
        if not isinstance(action, dict):
            failed.append({"action": action, "reason": "action 이 dict 가 아님"})
            continue
        name = action.get("action", "")
        # 게이트 기준은 **dispatch 표** 다. 예전엔 손으로 관리하는
        # _SUPPORTED_ACTIONS(50개)를 봐서 dispatch(62개)의 표기 변종 12개가
        # 영구히 도달 불가였다 — 그중 FK-OP alias 는 S9 회귀 60.89% 를 고치려
        # 추가된 것인데 한 번도 실행되지 않았다.
        if name not in _DISPATCH:
            # 정규화가 camelCase 를 접은 뒤에도 dispatch 에 그 표기가 없을 수 있다
            # (예: dispatch 는 delete_objectproperty, 정규형은 delete_object_property).
            # 밑줄을 제거한 형태로 한 번 더 찾아 표기 차이를 흡수한다.
            squashed = name.replace("_", "")
            alt = next((k for k in _DISPATCH if k.replace("_", "").lower()
                        == squashed.lower()), None)
            if alt:
                name = alt
        if name not in _DISPATCH:
            skipped.append({
                "action": action,
                "reason": f"미지원 action: {name}. 수동 검토 필요.",
            })
            continue
        # **정책 거부는 엔진 경계에서 강제한다.** 이전에는 DSL 엔진만 IFP 를
        # 거부하고 이 엔진은 적용했다 — 두 엔진이 같은 action 에 다른 정책을 가진
        # 상태였고, ``_route_jury_fixes`` 가 IFP 를 DSL 로 보내서 **우연히** 막히고
        # 있었다. 그 라우팅 한 줄이 바뀌면 정책이 조용히 뒤집힌다 (2026-08-30 규명).
        # 우연에 의존하지 않도록 여기서도 같은 답을 낸다.
        policy_reason = _policy_rejection_reason(name)
        if policy_reason:
            skipped.append({"action": action, "reason": policy_reason})
            continue
        try:
            ok, msg = _DISPATCH[name](g, action)
        except Exception as e:
            failed.append({"action": action, "reason": f"{type(e).__name__}: {e}"})
            continue
        if ok:
            applied.append({"action": action, "status": msg})
        elif _is_noop_reason(msg):
            noop.append({"action": action, "reason": msg})
        else:
            failed.append({"action": action, "reason": msg})

    if failed or skipped:
        # 예전엔 이 모듈 전체에 logger 호출이 1개뿐이라 37건 실패가 로그에
        # INFO 정수 하나로만 남았다. 사유를 남겨 운영자가 원인을 볼 수 있게 한다.
        logger.warning(
            "jury fixes: 적용 %d / no-op %d / 실패 %d / 미지원 %d. 실패 상세: %s",
            len(applied), len(noop), len(failed), len(skipped),
            [(f["action"].get("action", "?") if isinstance(f.get("action"), dict)
              else "?", str(f.get("reason"))[:70]) for f in (failed + skipped)[:8]],
        )

    new_ttl = g.serialize(format="turtle")
    return {
        "ttl": new_ttl,
        "applied": applied,
        "skipped": skipped,
        "failed": failed,
        "noop": noop,
    }


#: "이미 원하는 상태" 를 뜻하는 핸들러 사유 — 실패가 아니라 no-op 이다.
#:
#: ## 왜 이 목록이 어긋나면 위험한가
#:
#: ``failed`` 버킷에는 성질이 다른 두 가지가 섞인다:
#:
#: 1. **계약 위반** — LLM 이 필드 이름/필수값을 몰랐다 (``members list(>=2) 누락``).
#:    프롬프트를 고쳐야 한다.
#: 2. **no-op** — 지시는 올바르지만 이미 그 상태다 (``모든 children 이 이미 parent
#:    의 하위``). 고칠 것이 없다.
#:
#: 둘을 한 버킷에 담으면 "프롬프트를 고쳐서 실패가 줄었는가" 를 측정할 수 없다.
#: 실측 (2026-08-22): 핸들러가 반환하는 no-op 성격 사유 22개 중 **14개가 마커에
#: 걸리지 않아** ``failed`` 로 집계됐다. 최종 Jury 로그의 실패 3건 중
#: ``hierarchy 에서 추가할 엔티티 없음`` 은 중복 지시(=정상)였는데 실패로 보고됐다.
#:
#: ## 문자열 매칭의 한계
#:
#: 이 방식은 핸들러가 문구를 바꾸면 조용히 어긋난다 (``_apply_property_characteristic``
#: 의 주석이 이미 그 위험을 경고한다). 근본 해결은 핸들러가 열거형을 반환하는
#: 것이지만 50여 개 반환 타입 변경이라 별건이다. 그 사이의 안전장치로
#: ``tests/test_jury_noop_classification.py`` 가 **소스에서 사유 문자열을 전수
#: 추출해** 새 no-op 사유가 마커 없이 추가되면 실패한다.
#:
#: ## 등록 기준 (엄격)
#:
#: "대상이 없어서 할 일이 없었다" 만 no-op 다. **필수 필드 누락·해석 실패는
#: 계약 위반이므로 절대 넣지 않는다** — 넣으면 LLM 의 스키마 오류가 통계에서
#: 사라져 프롬프트 결함이 영구히 안 보인다.
_NOOP_MARKERS: tuple[str, ...] = (
    # 이미 원하는 상태
    "이미 존재", "이미 선언", "이미 동등 관계가 모두 선언",
    "모든 axiom 이 이미 존재", "이미 parent 의 하위",
    # 대상이 그래프에 없어 지울/바꿀 것이 없다
    "해당 트리플이 존재하지 않", "교체할 트리플 없", "제거할 트리플 없",
    "해당 URI 를 참조하는 트리플 없", "이중 접두사 URI 없",
    "해당 class 를 참조하는 트리플 없", "해당 class 에 대한 트리플 없",
    "해당 property 에 대한 트리플 없", "해당 property 에 domain 트리플 없",
    "해당 (subject, predicate) 트리플 없",
    "제거할 Restriction 없", "매칭되는 annotation 없",
    "이 그래프에 없", "트리플 없음",
    # 배치 지시에서 추가할 잔여 항목이 없다 (전부 이미 존재)
    "추가할 엔티티 없", "추가할 매핑 없", "추가된 triple 없",
)


def _policy_rejection_reason(action_name: str) -> str | None:
    """이 action 이 **정책상 거부**되는가 → 이유 또는 None.

    정본은 :mod:`tools.action_registry` 다 (사본 금지). 레지스트리를 못 불러오면
    거부하지 않는다 — 이 함수는 정책 강제이고, import 실패로 정상 수정이 막히면
    더 나쁘다. 대신 레지스트리 존재를 회귀 테스트로 고정한다.
    """
    try:
        from tools.action_registry import ACTION_ALIASES, POLICY_REJECTED
    except Exception:  # noqa: BLE001
        return None
    canonical = ACTION_ALIASES.get(action_name, action_name)
    reason = POLICY_REJECTED.get(canonical) or POLICY_REJECTED.get(action_name)
    if not reason:
        return None
    return f"정책상 거부된 action ({canonical}): {reason}"


def _is_noop_reason(msg: str) -> bool:
    """핸들러 실패 사유가 '변경할 것이 없었다' 인가 (진짜 결함과 구분).

    등록 기준과 드리프트 위험은 :data:`_NOOP_MARKERS` docstring 참조.
    """
    text = str(msg or "")
    return any(marker in text for marker in _NOOP_MARKERS)


__all__ = ("apply_jury_fixes",)
