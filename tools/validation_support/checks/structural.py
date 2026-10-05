"""Structural check 그룹.

KG의 구조적 무결성(양방향 OP, 공정 흐름, 고아 노드, 클래스 분포).
이들은 FK/semantic 검증 이전에 돌아야 하므로 validate_kg의 처음에 배치된다.

다른 그룹과의 관계:
- SharedCheckContext.inverse_pairs, steel_classes, typed_subjects 사용
- TIER_THRESHOLDS (validation_support.thresholds)
- DOMAIN_NS 네임스페이스 / TBOX_PATH (설정)
"""
from __future__ import annotations

import logging
import os
from collections import defaultdict

from rdflib import OWL, RDF, RDFS, Graph, URIRef

from config import TBOX_PATH
from domain.namespaces import DOMAIN_NS
from tools.validation_support.common import (
    SharedCheckContext,
    local,
    validate_prop_name,
)
from tools.validation_support.thresholds import get_tier_thresholds

logger = logging.getLogger(__name__)


def _inverseof_compatibility(tbox: Graph, fwd_name: str, inv_name: str) -> bool:
    """inverseOf 쌍의 domain/range 호환성 검사.

    range(fwd) ⊆ domain(inv) 호환 AND domain(fwd) ⊆ range(inv) 호환이면 True.
    subclass 체인도 고려. 이 체크에 통과하지 못하는 쌍은 T-Box 결함으로,
    bidirectional 검증 대상에서 제외된다 (ensure_inverse_triples 와 동일 기준).
    """
    from rdflib import URIRef
    fwd_uri = URIRef(DOMAIN_NS + fwd_name)
    inv_uri = URIRef(DOMAIN_NS + inv_name)

    # owl:Thing as domain/range is the universal class — it imposes no real
    # constraint, so it is compatible with anything. It must NOT be confused
    # with an undeclared (empty) domain/range, which the conservative branch
    # below treats as an incomplete declaration. We signal "universal" with a
    # None sentinel. Mirrors tools/tacit_op_validator._is_compatible so the KG
    # validator and the tacit OP validator agree on owl:Thing semantics.
    def _domain_classes(p, pred):
        vals = set(tbox.objects(p, pred))
        if OWL.Thing in vals:
            return None  # universal → compatible with anything
        return {x for x in vals
                if isinstance(x, URIRef) and str(x).startswith(DOMAIN_NS)}

    def _dom(p):
        return _domain_classes(p, RDFS.domain)

    def _rng(p):
        return _domain_classes(p, RDFS.range)

    subclass_of: dict = {}
    for sub, _, sup in tbox.triples((None, RDFS.subClassOf, None)):
        if isinstance(sub, URIRef) and isinstance(sup, URIRef):
            subclass_of.setdefault(sub, set()).add(sup)

    def _ancestors(cls):
        seen = {cls}
        stack = [cls]
        while stack:
            cur = stack.pop()
            for p in subclass_of.get(cur, ()):
                if p not in seen:
                    seen.add(p)
                    stack.append(p)
        return seen

    def _compat(a, b) -> bool:
        """호환성 판정.

        - 한 쪽이라도 universal(None, owl:Thing): 제약 없음 → True
        - 양쪽 모두 비어 있으면: 제약 없음 → True (기존 동작 유지)
        - 한 쪽만 비어 있으면: T-Box 선언 불완전 → False (보수적 skip)
        - 양쪽 모두 있으면: subclass 체인 기반 호환성 검사
        """
        if a is None or b is None:
            return True
        if not a and not b:
            return True
        if not a or not b:
            return False
        for ca in a:
            anc_a = _ancestors(ca)
            for cb in b:
                if cb in anc_a or ca in _ancestors(cb):
                    return True
        return False

    fd, fr = _dom(fwd_uri), _rng(fwd_uri)
    id_, ir = _dom(inv_uri), _rng(inv_uri)
    return _compat(fr, id_) and _compat(fd, ir)


def check_bidirectional_op(
    g: Graph, tbox: Graph, *, shared: SharedCheckContext | None = None,
) -> dict[str, object]:
    """1. 모든 inverseOf 쌍의 트리플 수 대칭성 전수 검증.

    각 쌍에 대해 Python set-diff 대신 SPARQL FILTER NOT EXISTS 로 누락
    방향을 Rust 엔진이 탐지한다 (전체 그래프 Python 순회 제거).

    **호환성 필터**: T-Box 가 inverseOf 로 선언했더라도 range/domain 이
    서로 맞물리지 않는 쌍은 T-Box 자체 결함이므로 bidirectional 검증 대상에서
    제외하고 incompatible_pairs 로 별도 보고 (ensure_inverse_triples 와 동일
    기준). 이 경우 passed 판정에는 반영하지 않아 T-Box 결함이 A-Box
    bidirectional 검증 실패로 잘못 퍼지는 것을 방지.

    shared가 제공되면 T-Box inverse_pairs 캐시 재사용.
    """
    if shared is not None:
        raw_pairs = list(shared.inverse_pairs)
    else:
        raw_pairs = []
        for prop in tbox.subjects(RDF.type, OWL.ObjectProperty):
            if not str(prop).startswith(DOMAIN_NS):
                continue
            for inv in tbox.objects(prop, OWL.inverseOf):
                if str(inv).startswith(DOMAIN_NS):
                    fwd_local = local(str(prop))
                    inv_local = local(str(inv))
                    if fwd_local and inv_local:
                        raw_pairs.append((fwd_local, inv_local))

    # ── owl:inverseOf 타입 오류 축 ──────────────────────────────────────
    #
    # ## 왜 별 축인가 (2026-09-04 S9.5 실측)
    #
    # ``owl:inverseOf`` 는 **프로퍼티끼리** 맺는 관계다. 대상이 클래스면 OWL 2 DL
    # 위반이고 역방향 자재화가 깨진다. 그런데 이 검사는 그것을 아래 호환성 필터의
    # ``incompatible_pairs`` 로 흘려보냈고, 그 버킷은 **의도적으로 passed 에 반영되지
    # 않는다** (T-Box 결함이 A-Box 양방향성 실패로 번지지 않게 하려는 설계).
    # 그래서 mutation 이 통과했다::
    #
    #   add_spurious_inverse: equipmentStatusRefersToEquipment owl:inverseOf
    #                         AirEmissionMonitoring (클래스!)  → 무발화
    #
    # 표적은 A-Box 6,000행이 흐르는 **살아있는** OP 였으므로 계측 문제가 아니라
    # 판정 문제였다. 두 사유를 갈라야 한다:
    #
    #   타입 오류        inverseOf 가 프로퍼티가 아닌 것을 가리킴 → **passed 에 반영**
    #   domain/range 불일치  실재하는 두 프로퍼티의 모델링 이견 → 기존대로 보고만
    #
    # 외래 네임스페이스 대상은 판정하지 않는다 — IOF 프로퍼티의 역관계를 선언하는
    # 것은 정당하고 그 선언이 이 T-Box 에 없는 것이 정상이다 (raw_pairs 수집이
    # 이미 도메인 NS 로 제한하는 것과 같은 기준).
    #
    # baseline 실측 (2026-09-04): inverseOf 82건 전부 도메인-도메인, 타입 오류 0.
    declared_ops = {p for p in tbox.subjects(RDF.type, OWL.ObjectProperty)}
    type_errors: list[dict] = []
    for subj, obj in tbox.subject_objects(OWL.inverseOf):
        if not (isinstance(subj, URIRef) and isinstance(obj, URIRef)):
            continue
        if not (str(subj).startswith(DOMAIN_NS) and str(obj).startswith(DOMAIN_NS)):
            continue                          # 외래 대상은 이 T-Box 의 책임이 아니다
        for side, node in (("subject", subj), ("object", obj)):
            if node not in declared_ops and len(type_errors) < 10:
                type_errors.append({
                    "side": side,
                    "property": local(str(subj)),
                    "target": local(str(obj)),
                    "note": "owl:inverseOf 는 ObjectProperty 끼리 맺어야 한다 — "
                            "클래스/DP/미선언 IRI 를 가리키면 역방향 자재화가 깨진다",
                })
    type_error_pairs = {
        (e["property"], e["target"]) for e in type_errors
    }

    # 호환성 필터
    pairs: list[tuple[str, str]] = []
    incompatible: list[tuple[str, str]] = []
    for fwd, inv in raw_pairs:
        if (fwd, inv) in type_error_pairs:
            continue                          # 타입 오류로 이미 보고 — 이중 분류 방지
        if _inverseof_compatibility(tbox, fwd, inv):
            pairs.append((fwd, inv))
        else:
            incompatible.append((fwd, inv))

    if not pairs and not incompatible:
        return {"name": "ObjectProperty 양방향 연결",
                "passed": not type_errors,
                "checked_pairs": 0, "total_pairs": len(raw_pairs), "missing": [],
                "missing_inverse_count": 0, "incompatible_pairs": [],
                "inverseof_type_errors": type_errors,
                "inverseof_type_error_count": len(type_errors)}

    missing: list[dict] = []
    checked = 0

    for fwd, inv in pairs:
        validate_prop_name(fwd)
        validate_prop_name(inv)
        checked += 1

        fwd_iri = f"<{DOMAIN_NS}{fwd}>"
        inv_iri = f"<{DOMAIN_NS}{inv}>"

        # 단일 쿼리로 forward / inverse count + 양방향 누락 count + 샘플까지
        count_q = f"""
            SELECT
              (COUNT(*) AS ?fwd_total)
              (SUM(IF(EXISTS {{ ?o {inv_iri} ?s }}, 0, 1)) AS ?missing_inv)
            WHERE {{ ?s {fwd_iri} ?o }}
        """
        inv_count_q = f"""
            SELECT
              (COUNT(*) AS ?inv_total)
              (SUM(IF(EXISTS {{ ?o {fwd_iri} ?s }}, 0, 1)) AS ?missing_fwd)
            WHERE {{ ?s {inv_iri} ?o }}
        """
        fwd_row = list(g.query(count_q))
        inv_row = list(g.query(inv_count_q))

        fwd_total = int(fwd_row[0][0]) if fwd_row and fwd_row[0][0] is not None else 0
        missing_inv = int(fwd_row[0][1]) if fwd_row and fwd_row[0][1] is not None else 0
        inv_total = int(inv_row[0][0]) if inv_row and inv_row[0][0] is not None else 0
        missing_fwd = int(inv_row[0][1]) if inv_row and inv_row[0][1] is not None else 0

        if missing_inv or missing_fwd:
            sample_q = f"""
                SELECT ?s ?o WHERE {{
                    ?s {fwd_iri} ?o .
                    FILTER NOT EXISTS {{ ?o {inv_iri} ?s }}
                }} LIMIT 5
            """
            samples = [(str(r[0]), str(r[1])) for r in g.query(sample_q)]
            missing.append({
                "pair": (fwd, inv),
                "forward_count": fwd_total,
                "inverse_count": inv_total,
                "missing_inverse_count": missing_inv,
                "missing_forward_count": missing_fwd,
                "samples": samples,
            })

    if type_errors:
        logger.warning(
            "owl:inverseOf 타입 오류 %d건 — 프로퍼티가 아닌 대상을 가리킨다: %s. "
            "이 축을 분리하기 전에는 incompatible_pairs 로 흘러가 "
            "passed 에 반영되지 않았다)",
            len(type_errors),
            [f"{e['property']} → {e['target']} ({e['side']})" for e in type_errors[:5]],
        )

    return {
        "name": "ObjectProperty 양방향 연결",
        "passed": len(missing) == 0 and not type_errors,
        "checked_pairs": checked,
        # 타입 오류는 incompatible_pairs 와 달리 **passed 에 반영된다** — 모델링
        # 이견이 아니라 OWL 2 DL 위반이다 (위 블록 주석 참조).
        "inverseof_type_errors": type_errors,
        "inverseof_type_error_count": len(type_errors),
        # total_pairs = 호환성 필터 전 전체 inverseOf 쌍 수 (T-Box 선언 기준).
        # 단위 테스트와 하위 호환 유지. 실제 검증 대상은 checked_pairs.
        "total_pairs": len(raw_pairs),
        "incompatible_pairs": [
            {"fwd": f, "inv": i,
             "note": "range/domain 불일치로 T-Box 결함 — check_quality_rules 또는 T-Box 재생성으로 해결"}
            for f, i in incompatible
        ],
        "incompatible_count": len(incompatible),
        "missing": missing,
        "missing_inverse_count": sum(m["missing_inverse_count"] for m in missing),
    }


def check_process_flow(
    g: Graph, *, shared: SharedCheckContext | None = None,
) -> dict[str, object]:
    """2. 암묵지 공정 흐름 체인 확인 (도메인 무관).

    shared 파라미터는 시그니처 통일용.
    """
    from domain.namespaces import NS_PREFIX as _NS
    from tools.validation_support.common import query as _query

    # _NS는 고정 ontology prefix이며 RDFLib SPARQL만 실행한다.
    rows = _query(g, f'SELECT ?from ?to WHERE {{ ?from {_NS}:followedBy ?to }}')  # nosec B608
    steps = _query(g, f'SELECT ?s WHERE {{ ?s a {_NS}:ManufacturingProcessStep }}')

    chain = [(r["from"], r["to"]) for r in rows]
    step_count = len(steps)

    if step_count == 0 and len(chain) == 0:
        return {
            "name": "공정 흐름 체인",
            "passed": True,
            "message": "해당 도메인에 공정 흐름 없음 (ManufacturingProcessStep/followedBy 미정의)",
            "steps": 0,
            "links": 0,
            "start_nodes": [],
            "end_nodes": [],
            "chain": [],
        }

    sources = {local(f) for f, _ in chain}
    targets = {local(t) for _, t in chain}
    start_nodes = sources - targets
    end_nodes = targets - sources

    return {
        "name": "공정 흐름 체인",
        "passed": step_count >= 2 and len(chain) >= 1,
        "steps": step_count,
        "links": len(chain),
        "start_nodes": sorted(start_nodes),
        "end_nodes": sorted(end_nodes),
        "chain": [f"{local(f)} → {local(t)}" for f, t in chain],
    }


def check_orphan_nodes(
    g: Graph, *, shared: SharedCheckContext | None = None,
) -> dict[str, object]:
    """3. 고아 노드 탐지 — 도메인 관계로 아무것과도 이어지지 않은 인스턴스.

    Oxigraph SPARQL 인덱스로 전체 순회보다 빠르게 집계.
    shared 제공 시 typed_subjects 캐시 활용.

    **연결의 정의가 좁아야 의미가 있다.** 예전엔 "``rdf:type`` 이 아닌 트리플의
    주어이거나, 어떤 IRI 객체이면 연결됨" 이었다. 그런데 A-Box 생성기는 모든
    인스턴스에 ``prov:wasDerivedFrom <row_uri>`` 를 무조건 붙이므로
    (abox_generation.py:3348), 그 한 줄이 조건을 만족해 **이 게이트는 파이프라인
    산출물에서 0 이 아닌 값을 낼 수 없었다** (2026-08-08 실측: 아무것과도 안 이어진
    인스턴스도 prov 트리플만으로 passed=True). DP 리터럴 하나만 있어도 같았다.

    그래서 FK 해석이 통째로 실패해 테이블 하나가 그래프에서 떨어져 나가도 S9 는
    조용했고, 운영자는 S12 에서 "질의가 0건" 으로 만나 질의 문제로 디버깅했다.

    이제 **도메인 네임스페이스의 IRI-valued 관계** 만 연결로 센다:
      - ``prov:``/``dcterms:`` 등 메타 프로퍼티 제외 (출처 표기는 관계가 아니다)
      - 리터럴 값 제외 (DP 는 그래프 연결이 아니다)
      - ``rdf:type`` 제외 (기존과 동일 — 클래스 소속은 관계가 아니다)
    """
    typed_entities: dict[URIRef, str] = {}
    for s, _, o in g.triples((None, RDF.type, None)):
        if (
            isinstance(s, URIRef) and isinstance(o, URIRef)
            and str(o).startswith(DOMAIN_NS) and s not in typed_entities
        ):
            typed_entities[s] = local(str(o))

    if not typed_entities:
        return {
            "name": "고아 노드 탐지",
            "passed": True,
            "orphan_count": 0,
            "samples": [],
        }

    has_outgoing: set[URIRef] = set()
    has_incoming: set[URIRef] = set()
    rdf_type_iri = f"<{RDF.type}>"
    # 도메인 프로퍼티 + IRI 객체만 — prov/dcterms 메타와 리터럴은 연결이 아니다.
    domain_pred = f'STRSTARTS(STR(?p), "{DOMAIN_NS}")'
    q_out = (
        "SELECT DISTINCT ?s WHERE { ?s ?p ?o . "
        f"FILTER(isIRI(?s) && isIRI(?o) && ?p != {rdf_type_iri} && {domain_pred}) }}"
    )
    q_in = (
        "SELECT DISTINCT ?o WHERE { ?s ?p ?o . "
        f"FILTER(isIRI(?o) && ?p != {rdf_type_iri} && {domain_pred}) }}"
    )
    for row in g.query(q_out):
        has_outgoing.add(row[0])
    for row in g.query(q_in):
        has_incoming.add(row[0])

    orphans = [e for e in typed_entities if e not in has_outgoing and e not in has_incoming]
    samples = [
        {"entity": local(str(e)), "type": typed_entities[e]} for e in orphans[:10]
    ]

    return {
        "name": "고아 노드 탐지",
        "passed": len(orphans) == 0,
        "orphan_count": len(orphans),
        "samples": samples,
    }


def _hasvalue_is_reachable(g: Graph, props: list, values: list) -> bool:
    """``hasValue`` 공리가 A-Box 에서 실제로 만족될 수 있는가.

    ``onProperty`` 가 **그 값을 갖는 트리플이 하나라도** A-Box 에 있으면 도달 가능하다.
    없으면 그 정의 클래스는 추론을 몇 번 돌려도 영구히 0건이다.

    ## 왜 이 판정이 필요한가 (2026-08-27 실측)

    정의 클래스 22개 중 **9개가 값 불일치로 영구 0건**이다:

        alarmEventsSeverity        공리 "HIGH"     데이터 "High"(138)      대소문자
        inventoryTransactionType  공리 "IN"       데이터 "Inbound"(177)   어휘 불일치
        realTimeDataQualityCode   공리 "BAD"      데이터 0/1/2            어휘 불일치
        failureCauseResolutionStatus 공리 "Open"  데이터 Pending/Recurring 값 부재
        airEmissionMonitoringPollutantType        A-Box 트리플 **0건**    DP 자체 부재

    이 판정이 없으면 게이트가 그것들을 "추론이 채울 것" 으로 세어 **결함을 정상으로
    위장**한다. 값을 데이터에 맞춰 고치는 것은 해법이 아니다 — 공리의 의미를 데이터에
    맞추는 것이고 "지표 매수" 다. 게이트가 알리고 사람이 판단해야 한다.

    ``str`` 비교를 쓰는 이유: A-Box 는 평문 리터럴(``"Running"``)을 쓰고 T-Box 공리는
    타입 리터럴(``"Running"^^xsd:string``)을 쓴다. RDF 1.1 에서 둘은 같은 term 이지만
    rdflib 의 ``==`` 는 datatype 을 구분하므로, 문자열 값으로 비교해야 실제 추론기
    동작(둘 다 분류함 — 실측)과 일치한다.
    """
    if not props or not values:
        return True          # 판정 근거 없음 → 막지 않는다
    wanted = {str(v) for v in values}
    for prop in props:
        for obj in g.objects(None, prop):
            if str(obj) in wanted:
                return True
    return False


def check_class_instance_count(
    g: Graph, *, class_tiers: dict[str, str] | None = None,
    tbox: Graph | None = None,
    shared: SharedCheckContext | None = None,
    tbox_path: str | None = None,
) -> dict[str, object]:
    """4. 클래스별 인스턴스 수 기본 검증.

    class_tiers 제공 시 inferred 티어(no_instance_allowed=True) 클래스는
    no_instance 목록에서 제외.

    Args:
        tbox_path: T-Box 경로 override (테스트에서 주입 가능).
    """
    class_counts: dict[str, int] = defaultdict(int)
    for _, _, o in g.triples((None, RDF.type, None)):
        if isinstance(o, URIRef) and str(o).startswith(DOMAIN_NS):
            class_counts[local(str(o))] += 1
    class_counts = dict(class_counts)

    if shared is not None:
        tbox_classes = set(shared.steel_classes)
    else:
        tbox_classes = set()
        path = tbox_path if tbox_path is not None else TBOX_PATH
        if tbox is None and os.path.exists(path):
            from domain.tbox_utils import load_tbox
            tbox = load_tbox(path)
        if tbox is not None:
            for cls in tbox.subjects(RDF.type, OWL.Class):
                if isinstance(cls, URIRef) and str(cls).startswith(DOMAIN_NS):
                    tbox_classes.add(local(str(cls)))

    no_instances = sorted(tbox_classes - set(class_counts.keys()))

    if class_tiers is not None:
        tier_thresholds = get_tier_thresholds()
        inferred_allowed = {
            cls for cls, tier in class_tiers.items()
            if tier_thresholds.get(tier, {}).get("no_instance_allowed", False)
        }
        no_instances = [c for c in no_instances if c not in inferred_allowed]

    # 이슈 3: no_instance 를 "restriction 으로 추론 가능" vs "순수 고아" 2분류.
    # hasValue / someValuesFrom / onClass 제약을 가진 클래스는 reasoner 가
    # A-Box 인스턴스를 자동 분류할 수 있어 truly_orphan 이 아니다.
    #
    # **두 축을 모두 본다** (2026-08-27). 예전에는 ``rdfs:subClassOf`` 만 스캔했고,
    # 그 결과 ``owl:equivalentClass`` 로 정의된 클래스 22개를 전부 놓쳤다. 실측:
    #
    #     subClassOf 축 derivable      22개  ← EquipmentStatus / GHGEmission 등 (부모)
    #     equivalentClass 축 derivable 22개  ← 값 기반 서브클래스 (교집합 **0**)
    #     게이트 보고: no_instance_derivable 0 / truly_orphan 28
    #
    # 두 집합이 전혀 다른 클래스다. 놓친 22개는 정확히 "값 기반 서브클래스" 이고,
    # 그것들이 "순수 고아" 로 오보고돼 ``passed=False`` 를 만들었다.
    #
    # 방향 차이가 있다는 점이 중요하다: 값으로 개체를 **분류** 하는 것은
    # ``equivalentClass`` 뿐이다 (``subClassOf`` 는 역방향 추론을 하지 않는다 —
    # `reasonable` 실측). 그래도 두 축을 함께 세는 이유는 이 판정의 질문이 "추론이
    # 채울 여지가 있는가" 이고, ``subClassOf`` 쪽 someValuesFrom/onClass 도 다른
    # 규칙으로 개체를 끌어올 수 있기 때문이다. 과소 판정(고아 오보고)이 과대 판정보다
    # 나쁘다 — 정상 설계를 결함으로 읽으면 게이트 신뢰가 깨진다.
    derivable: set[str] = set()
    # hasValue 공리가 가리키는 값이 A-Box 에 없어 **영구히 채워지지 않는** 정의 클래스.
    # derivable 과 구분해야 한다 — 전자는 "추론이 채운다", 후자는 "공리가 죽어 있다".
    unreachable_defs: set[str] = set()
    if tbox is not None:
        from rdflib import Literal as _Literal
        _RESTR_BODY = (
            OWL.hasValue, OWL.someValuesFrom, OWL.allValuesFrom, OWL.onClass,
        )
        for cls_name in no_instances:
            cls_uri = URIRef(str(DOMAIN_NS) + cls_name)
            for pred in (RDFS.subClassOf, OWL.equivalentClass):
                for target in tbox.objects(cls_uri, pred):
                    # Turtle 소스가 문자열로 저장된 공리는 추론에 무의미하다 —
                    # 그것을 derivable 로 세면 결함을 정상으로 위장한다.
                    if isinstance(target, _Literal):
                        continue
                    if (target, RDF.type, OWL.Restriction) not in tbox:
                        continue
                    if not any((target, body, None) in tbox for body in _RESTR_BODY):
                        continue
                    # ``hasValue`` 는 **그 값이 A-Box 에 실재해야** 도달 가능하다.
                    #
                    # 실측 (2026-08-27): 정의 클래스 22개 중 **9개가 값 불일치로 영구
                    # 0건**이다 — 공리는 `alarmEventsSeverity = "HIGH"` 인데 데이터는
                    # `"High"`, `inventoryTransactionType = "IN"` 인데 `"Inbound"`,
                    # `realTimeDataQualityCode = "BAD"` 인데 숫자 코드 `0/1/2`.
                    # 그것을 derivable 로 세면 **도달 불가 공리를 정상으로 위장**하고,
                    # 이 게이트가 유일한 발견 경로였는데 조용해진다.
                    #
                    # 값을 데이터에 맞춰 고치는 것은 해법이 아니다 — 공리의 의미를
                    # 데이터에 맞추는 것이고, 이 리포가 금지하는 "지표 매수" 다. 게이트가
                    # 알려주고 사람이 판단해야 한다.
                    values = list(tbox.objects(target, OWL.hasValue))
                    if values:
                        props = list(tbox.objects(target, OWL.onProperty))
                        if not _hasvalue_is_reachable(g, props, values):
                            unreachable_defs.add(cls_name)
                            continue
                    derivable.add(cls_name)
                    break
                if cls_name in derivable:
                    break

    # 추상 상위 클래스는 인스턴스가 없는 것이 **정상 설계** 이므로 분모·판정에서
    # 제외한다. 상세 근거는 ``find_abstract_parent_classes`` docstring 참조
    # (실측: truly_orphan 12개가 전부 추상 클래스였다 — 실제 갭 0).
    abstract_parents: set[str] = set()
    if tbox is not None:
        from tools.validation_support.common import find_abstract_parent_classes
        abstract_parents = find_abstract_parent_classes(
            tbox, candidates=set(no_instances),
        )

    no_instance_truly_orphan = sorted(
        set(no_instances) - derivable - abstract_parents,
    )
    no_instance_derivable = sorted(derivable)

    # 비율은 "인스턴스가 있어야 하는데 없는" 클래스만 센다. 분모에서도 추상
    # 클래스를 빼야 지표가 달성 가능해진다.
    #
    # ``derivable`` (someValuesFrom/hasValue restriction 보유 → reasoner 가 A-Box
    # 인스턴스를 자동 분류) 도 제외한다. 추론 전 그래프에서 비어 있는 것이
    # 정상이고, 이미 위에서 truly_orphan 과 구분해 놓았는데 비율에 넣으면 같은
    # 판정을 두 번 하는 셈이다. 추론 후에도 비면 '추론 sanity check' 가 잡는다.
    scored_classes = tbox_classes - abstract_parents - derivable
    unexpected_empty = set(no_instances) - abstract_parents - derivable
    no_instance_ratio = len(unexpected_empty) / max(len(scored_classes), 1)
    return {
        "name": "클래스별 인스턴스 수",
        "passed": no_instance_ratio <= 0.1,
        "total_classes_with_instances": len(class_counts),
        "tbox_classes": len(tbox_classes),
        "scored_classes": len(scored_classes),
        "abstract_parents_excluded": sorted(abstract_parents),
        "no_instance_classes": no_instances,
        "no_instance_derivable": no_instance_derivable,
        # hasValue 공리가 A-Box 에 없는 값을 가리켜 **영구히 채워지지 않는** 정의 클래스.
        # truly_orphan 에도 포함되지만 원인이 다르므로 따로 노출한다 — "공리가 없다" 와
        # "공리가 죽은 값을 가리킨다" 는 다른 조치를 요구한다 (후자는 값 어휘 대조).
        "no_instance_unreachable_axiom": sorted(unreachable_defs),
        "no_instance_truly_orphan": no_instance_truly_orphan,
        "no_instance_ratio": round(no_instance_ratio * 100, 1),
        "smallest_5": sorted(class_counts.items(), key=lambda x: x[1])[:5],
    }
