"""OWL RL 추론 도구 — T-Box + A-Box 병합 후 로컬 추론

owlrl 라이브러리로 OWL 2 RL 프로파일 추론을 수행한다.
추론된 트리플을 별도 파일로 저장하여 원본과 분리.

추론 규칙:
- rdfs:subClassOf 체인 (A⊂B, B⊂C → A⊂C)
- rdfs:domain/range 타입 추론 (prop domain C, x prop y → x a C)
- owl:inverseOf (P inverse Q, x P y → y Q x)
- owl:TransitiveProperty (P(x,y), P(y,z) → P(x,z))
- owl:SymmetricProperty (P(x,y) → P(y,x))
- owl:equivalentClass / owl:equivalentProperty
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import resource
import sys
import time
from datetime import datetime

from rdflib import OWL, RDF, RDFS, BNode, Graph, Literal, URIRef
from reasonable import PyReasoner

from config import (
    ABOX_PATH,
    GENERATED_ABOX_DIR,
    GENERATED_TBOX_DIR,
    INFERRED_PATH,
    MASTER_DATA_PATH,
    TBOX_PATH,
)
from domain.namespaces import (
    DOMAIN_INST_NS,
    DOMAIN_INST_NS_OBJ,
    DOMAIN_NS,
    DOMAIN_NS_OBJ,
    sparql_iri,
)
from domain.tbox_utils import _new_graph
from domain.uri_conventions import local_name as _local_name
from tools.common import (
    JobRegistry,
    add_source_artifacts,
    atomic_write,
    atomic_write_json,
    error_response,
    resolve_child_path,
    source_stamp,
)

logger = logging.getLogger(__name__)

_LOCAL_INFERENCE_MAX_ESTIMATED_TRIPLES = 12_000_000


def _rss_mb() -> float:
    """현재 프로세스 RSS (MB). macOS bytes / Linux KB 자동 보정.

    POSIX ``ru_maxrss`` 단위가 OS 별로 다르다:
      - macOS / *BSD: bytes
      - Linux: KB (1024 byte)
    sys.platform 으로 분기해 어느 환경에서든 MB 단위로 정규화한다.
    """
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return rss / 1024 if sys.platform.startswith("linux") else rss / 1024 / 1024


def _stage(label: str, start_t: float, *, mb: float | None = None) -> None:
    """단계 종료 로그 한 줄 — tail -f 로 multi-hour 작업 추적 용이.

    `STAGE [name] elapsed=X.Xs RSS=Y MB` 포맷. 파이썬이 emit() 후 자동 flush
    되도록 server.py 의 _AutoFlushFileHandler 와 결합해야 실시간으로 보임.
    """
    elapsed = time.monotonic() - start_t
    rss_mb = mb if mb is not None else _rss_mb()
    logger.info("STAGE [%s] elapsed=%.1fs RSS=%.0f MB", label, elapsed, rss_mb)


def _opt_d_enabled() -> bool:
    """OPT-D 메모리 최적화 모드 활성 여부 (env OPT_D_ENABLED, 기본 OFF).

    OPT-D 는 rdflib 인메모리 그래프를 우회하고 reasonable.PyReasoner.load_file
    을 직접 호출. 메모리 사용량 ~3× 절감 + 비선형 Turtle 파싱 비용 회피.
    단점: rdflib graph 가 텅 비어있어 후처리 (restriction / pollution / prune /
    delta) 가 의미 없음 — SKIP_POST_RL_* env 와 함께 사용 권장.
    """
    return os.getenv("OPT_D_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")


def _flag_off(env_name: str) -> bool:
    """env 가 truthy 가 아니면 True (= 단계 실행). truthy 면 False (= 단계 우회).

    의도: ``if _flag_off("SKIP_POST_RL_X"):`` 패턴으로 후처리 단계를 게이트.
    기본값 빈 문자열 → True (단계 실행). 운영자가 SKIP=true 로 명시할 때만 False.
    """
    return os.getenv(env_name, "").strip().lower() not in ("1", "true", "yes", "on")


def _load_and_merge(tbox_path: str = "", abox_path: str = "") -> tuple[Graph, int]:
    """T-Box + A-Box + 암묵지 TTL을 하나의 그래프로 병합한다.

    OPT-D 모드 (env OPT_D_ENABLED=true): 빈 그래프 + tacit_count 만 반환.
    실제 적재는 _run_reasonable 의 reasoner.load_file 경로에서 진행. 후속
    단계는 빈 그래프를 받지만 SKIP_POST_RL_* env 로 후처리 우회 권장.

    기본 모드: 캐시된 그래프를 직접 수정(expand 등)할 수 있으므로,
    로드 직후 캐시를 무효화하여 다른 호출자가 수정된 그래프를 받지 않도록 한다.

    Returns:
        (병합 그래프, 로드된 암묵지 파일 수)
    """
    tbox = tbox_path or TBOX_PATH
    if not os.path.exists(tbox):
        raise FileNotFoundError(f"T-Box 파일이 없습니다: {tbox}")

    if _opt_d_enabled():
        # OPT-D: 빈 그래프 반환. tacit 파일 수만 카운트해서 응답에 포함.
        import glob as _glob

        from config import SOURCE_TACIT_DIR as _STD
        tacit_count = (
            len(_glob.glob(os.path.join(_STD, "*.ttl")))
            if os.path.isdir(_STD) else 0
        )
        logger.info("OPT-D 모드 — 빈 그래프 반환 (tacit %d files)", tacit_count)
        return _new_graph(), tacit_count

    from domain.tbox_utils import invalidate_graph_cache, load_graph

    g, tacit_count = load_graph(tbox_path=tbox_path, abox_path=abox_path)
    # 캐시된 그래프 참조를 DeductiveClosure가 in-place 수정하므로
    # 다른 호출자가 오염된 캐시를 받지 않도록 즉시 무효화
    invalidate_graph_cache()
    return g, tacit_count


def _extract_inferred(before_count: int, g: Graph) -> Graph:
    """추론 전후 차이에서 새로 추론된 트리플만 추출한다.

    완전한 차집합은 비용이 크므로, 추론 후 그래프에서
    steel:/steel-inst: 네임스페이스의 트리플만 필터링하여 반환한다.
    """
    inferred = _new_graph()
    # 네임스페이스 바인딩
    for prefix, ns in g.namespaces():
        inferred.bind(prefix, ns)

    steel_str = DOMAIN_NS
    inst_str = DOMAIN_INST_NS

    for s, p, o in g:
        s_str = str(s)
        if s_str.startswith(steel_str) or s_str.startswith(inst_str):
            inferred.add((s, p, o))

    return inferred


def _apply_owl_restrictions(g: Graph, tbox: Graph) -> dict:
    """OWL RL이 처리하지 못하는 restriction 기반 인스턴스 분류를 후처리한다.

    T-Box에서 owl:equivalentClass + owl:Restriction 패턴을 파싱하여,
    조건에 맞는 인스턴스에 rdf:type을 추가한다.

    T-Box 스캔(보통 수백 규모)은 Python에서 수행하고, 실제 A-Box
    scan/write는 Oxigraph Rust 엔진에 위임한다 (g.update / g.query).
    기존의 A-Box 전체 Python iteration을 제거해 S8 후처리 시간을 단축.

    지원 패턴:
    - owl:hasValue: 프로퍼티 값이 특정 값이면 해당 클래스 타입 부여
    - owl:someValuesFrom: 특정 클래스 타입의 값이 1건이라도 있으면 타입 부여
    - owl:allValuesFrom: 모든 URIRef 값이 target 타입이면 타입 부여 (OWA)
    - owl:minCardinality: 프로퍼티 값이 N개 이상이면 타입 부여

    Returns:
        {"hasValue": N, "someValuesFrom": N, "minCardinality": N, "total": N,
         "restriction_violations": [...], "restriction_violation_count": N}
    """
    stats: dict = {"hasValue": 0, "someValuesFrom": 0, "minCardinality": 0,
                   "allValuesFrom": 0, "total": 0}

    def _insert(update_body: str) -> int:
        before = len(g)
        g.update(update_body)
        return len(g) - before

    # ── 1. equivalentClass → Restriction 분류 (A-Box에 타입 부여) ──
    for cls in tbox.subjects(RDF.type, OWL.Class):
        if not isinstance(cls, URIRef):
            continue
        cls_n3 = sparql_iri(cls)

        for eq_node in tbox.objects(cls, OWL.equivalentClass):
            if (eq_node, RDF.type, OWL.Restriction) not in tbox:
                continue
            on_prop = tbox.value(eq_node, OWL.onProperty)
            if not on_prop:
                continue
            prop_n3 = sparql_iri(on_prop)

            # hasValue — 엄격 비교 + 문자열 폴백을 하나의 FILTER로 통합
            has_value = tbox.value(eq_node, OWL.hasValue)
            if has_value is not None:
                val_n3 = has_value.n3()
                val_str = str(has_value).replace("\\", "\\\\").replace('"', '\\"')
                added = _insert(
                    f"INSERT {{ ?inst a {cls_n3} }} "
                    f"WHERE {{ ?inst {prop_n3} ?v . "
                    f"FILTER (?v = {val_n3} || str(?v) = \"{val_str}\") }}"
                )
                stats["hasValue"] += added
                stats["total"] += added
                continue

            # someValuesFrom
            some_values = tbox.value(eq_node, OWL.someValuesFrom)
            if some_values is not None and isinstance(some_values, URIRef):
                tgt_n3 = sparql_iri(some_values)
                added = _insert(
                    f"INSERT {{ ?inst a {cls_n3} }} "
                    f"WHERE {{ ?inst {prop_n3} ?v . ?v a {tgt_n3} }}"
                )
                stats["someValuesFrom"] += added
                stats["total"] += added
                continue

            # allValuesFrom — URIRef 값 전부가 target 타입일 때 (Literal 값은 무시)
            all_values = tbox.value(eq_node, OWL.allValuesFrom)
            if all_values is not None and isinstance(all_values, URIRef):
                tgt_n3 = sparql_iri(all_values)
                added = _insert(
                    f"INSERT {{ ?inst a {cls_n3} }} "
                    f"WHERE {{ "
                    f"?inst {prop_n3} ?any . FILTER (isIRI(?inst)) "
                    f"FILTER NOT EXISTS {{ "
                    f"?inst {prop_n3} ?v . FILTER (isIRI(?v)) "
                    f"FILTER NOT EXISTS {{ ?v a {tgt_n3} }} "
                    f"}} }}"
                )
                stats["allValuesFrom"] += added
                stats["total"] += added
                continue

            # minCardinality — GROUP BY + HAVING
            min_card = (tbox.value(eq_node, OWL.minCardinality)
                        or tbox.value(eq_node, OWL.minQualifiedCardinality))
            if min_card is not None:
                try:
                    threshold = int(min_card)
                except (ValueError, TypeError):
                    logger.warning("Invalid minCardinality value: %s, skipping", min_card)
                    continue
                added = _insert(
                    f"INSERT {{ ?inst a {cls_n3} }} WHERE {{ "
                    f"SELECT ?inst WHERE {{ "
                    f"?inst {prop_n3} ?v . FILTER (isIRI(?inst)) "
                    f"}} GROUP BY ?inst HAVING (COUNT(?v) >= {threshold}) }}"
                )
                stats["minCardinality"] += added
                stats["total"] += added
                continue

    # ── 2. subClassOf → Restriction 위반 검출 (상속 체인 포함) ──
    # 상속 체인을 Python에서 풀어 (cls, restriction) 쌍을 만든 후, 각 쌍에
    # 대해 SPARQL SELECT 한 번으로 위반 인스턴스를 조회.
    def _collect_restrictions(cls_uri, visited=None):
        if visited is None:
            visited = set()
        if cls_uri in visited:
            return []
        visited.add(cls_uri)
        res = []
        for parent in tbox.objects(cls_uri, RDFS.subClassOf):
            if (parent, RDF.type, OWL.Restriction) in tbox:
                res.append(parent)
            elif isinstance(parent, URIRef):
                res.extend(_collect_restrictions(parent, visited))
        return res

    restriction_violations: list[dict] = []
    # 같은 위반을 여러 번 세지 않기 위한 dedup 키 집합.
    #
    # ``_collect_restrictions`` 는 조상 계층을 따라 올라가므로, 상위 클래스에 걸린
    # Restriction 은 그것을 상속하는 **모든 자손 클래스에서 다시** 도달된다. 위반
    # 여부는 ``(instance, property, target)`` 로 결정되는데 보고는 도달 경로마다
    # 한 건씩 쌓였다.
    #
    # 실측 (2026-08-17): 142,424 건으로 보고된 위반의 고유 축은 **90,586** — 1.57배
    # 부풀려 있었다. 원인은 ``ManufacturingProcessStep`` 의
    # ``directlyPrecedes``/``directlyFollows`` someValuesFrom 2개가 자손 5개에서
    # 각각 도달하는 것(51,838 축이 2개 클래스로 중복 보고).
    #
    # 어느 클래스를 통해 도달했는지는 위반의 **정체성이 아니라 경로** 다. 카운트가
    # 경로 수에 비례하면 계층을 깊게 만들 때마다 품질이 나빠 보인다.
    _seen_violations: set[tuple] = set()
    for cls in tbox.subjects(RDF.type, OWL.Class):
        if not isinstance(cls, URIRef):
            continue
        cls_local = _local_name(str(cls))
        cls_n3 = sparql_iri(cls)

        for parent in _collect_restrictions(cls):
            on_prop = tbox.value(parent, OWL.onProperty)
            if not on_prop:
                continue
            prop_local = _local_name(str(on_prop))
            prop_n3 = sparql_iri(on_prop)

            has_value = tbox.value(parent, OWL.hasValue)
            if has_value is not None:
                val_str_escaped = str(has_value).replace("\\", "\\\\").replace('"', '\\"')
                q = (
                    f"SELECT ?inst WHERE {{ "
                    f"?inst a {cls_n3} . FILTER (isIRI(?inst)) "
                    f"FILTER NOT EXISTS {{ ?inst {prop_n3} ?v . "
                    f"FILTER (str(?v) = \"{val_str_escaped}\") }} }}"
                )
                for row in g.query(q):
                    key = ("hasValue", str(row[0]), str(on_prop), str(has_value))
                    if key in _seen_violations:
                        continue
                    _seen_violations.add(key)
                    restriction_violations.append({
                        "class": cls_local,
                        "instance": _local_name(str(row[0])),
                        "restriction": "hasValue",
                        "property": prop_local,
                        "expected": str(has_value),
                    })
                continue

            some_values = tbox.value(parent, OWL.someValuesFrom)
            if some_values is not None and isinstance(some_values, URIRef):
                tgt_n3 = sparql_iri(some_values)
                tgt_local = _local_name(str(some_values))
                q = (
                    f"SELECT ?inst WHERE {{ "
                    f"?inst a {cls_n3} . FILTER (isIRI(?inst)) "
                    f"FILTER NOT EXISTS {{ "
                    f"?inst {prop_n3} ?v . FILTER (isIRI(?v)) "
                    f"?v a {tgt_n3} "
                    f"}} }}"
                )
                for row in g.query(q):
                    key = ("someValuesFrom", str(row[0]), str(on_prop),
                           str(some_values))
                    if key in _seen_violations:
                        continue
                    _seen_violations.add(key)
                    restriction_violations.append({
                        "class": cls_local,
                        "instance": _local_name(str(row[0])),
                        "restriction": "someValuesFrom",
                        "property": prop_local,
                        "expected_type": tgt_local,
                    })

    # 응답 비대화 방지: 위반은 인스턴스 수에 비례해 수만~수십만 건까지 쌓일 수
    # 있다. 전수를 응답 JSON 에 실으면 MB 급이 되어 MCP stdio 전송을 막는다
    # (2026-06-21 -32000 원인). 전체 카운트는 보존하되 샘플 50건만 노출한다.
    _VIOLATION_SAMPLE_CAP = 50
    stats["restriction_violation_count"] = len(restriction_violations)
    stats["restriction_violations"] = restriction_violations[:_VIOLATION_SAMPLE_CAP]
    if len(restriction_violations) > _VIOLATION_SAMPLE_CAP:
        stats["restriction_violations_truncated"] = True
    return stats


_PRUNE_PREFIXES = (
    "PREFIX owl: <http://www.w3.org/2002/07/owl#> "
    "PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#> "
)


def _prune_inference_noise(g: Graph) -> dict:
    """추론 후 노이즈 트리플을 제거한다.

    OWL RL 추론기가 생성하는 자명하거나 무의미한 트리플 4패턴을
    in-place로 제거하여 그래프 크기를 줄이고 다운스트림 품질을 높인다.

    Oxigraph store의 SPARQL DELETE 엔진(Rust)을 사용해 Python 전체
    iteration을 회피한다. Pattern 1~4는 SPARQL UPDATE로, Pattern 5(
    literal subject/predicate)는 Oxigraph가 애초에 거부하므로 생략하고
    Memory store 호환용으로만 가벼운 fallback을 둔다.

    패턴:
    1. self sameAs — (X, owl:sameAs, X)
    2. self equivalentClass — (X, owl:equivalentClass, X)
    3. implicit subClassOf owl:Thing / rdfs:Resource (URIRef만)
    4. BNode self subClassOf — (BNode, rdfs:subClassOf, BNode) where s == o
    5. Literal subject/predicate — Oxigraph에서 저장 불가, Memory store만 처리
    6a. DP literal 의 datatype 이 선언 range 와 불일치
    6b. OP object 가 literal (IRI 여야 함)
    6c. DP object 가 IRI/BNode (literal 이어야 함)

    6a~6c 는 어느 OWL 2 RL 규칙으로도 설명되지 않는 추론기 산출물이다. 남겨두면
    Functional / cardinality / domain-range check 를 오염시켜 **데이터 결함으로
    오보고** 되므로 (2026-07-26 materialBId 사례) 이 단계에서 제거한다.
    """
    size_before = len(g)
    counts = {
        "self_sameAs_removed": 0,
        "self_equivalentClass_removed": 0,
        "implicit_subClassOf_removed": 0,
        "bnode_self_ref_removed": 0,
        "literal_subject_removed": 0,
    }

    def _delete(update_body: str) -> int:
        before = len(g)
        g.update(_PRUNE_PREFIXES + update_body)
        return before - len(g)

    # Pattern 1: self sameAs
    counts["self_sameAs_removed"] = _delete(
        "DELETE WHERE { ?x owl:sameAs ?x }"
    )
    # Pattern 2: self equivalentClass
    counts["self_equivalentClass_removed"] = _delete(
        "DELETE WHERE { ?x owl:equivalentClass ?x }"
    )
    # Pattern 3: implicit subClassOf owl:Thing/rdfs:Resource (URIRef subject만)
    counts["implicit_subClassOf_removed"] = _delete(
        "DELETE { ?s rdfs:subClassOf ?o } "
        "WHERE { ?s rdfs:subClassOf ?o . "
        "FILTER (?o IN (owl:Thing, rdfs:Resource)) "
        "FILTER (isIRI(?s)) }"
    )
    # Pattern 4: BNode self subClassOf
    counts["bnode_self_ref_removed"] = _delete(
        "DELETE { ?x rdfs:subClassOf ?x } "
        "WHERE { ?x rdfs:subClassOf ?x . FILTER (isBlank(?x)) }"
    )

    # Pattern 5: Memory store fallback만 의미 있음. Oxigraph store는 bulk_load
    # 단계에서 literal subject/predicate를 reject하므로 항상 0. 그래도 기존
    # 호환을 위해 체크는 유지 (비용은 전체 scan이지만 실무에선 발생하지 않음).
    lit_to_remove = []
    for s, p, o in g:
        if isinstance(s, Literal) or isinstance(p, Literal):
            lit_to_remove.append((s, p, o))
    for triple in lit_to_remove:
        g.remove(triple)
    counts["literal_subject_removed"] = len(lit_to_remove)

    # Pattern 6a: range-incompatible DP triples. The `reasonable` engine
    # occasionally emits triples like `<GasEnergy_GS0310> steel:gasId "1220.26"^^xsd:decimal`
    # where the literal's datatype does not match the DP's declared range
    # (gasId's range is xsd:string). Such triples are not explained by any
    # OWL 2 RL rule and cross-pollute Functional/cardinality checks.
    # We delete DP triples whose literal datatype does not match the declared
    # range. Only xsd: datatype ranges are checked; untyped range skips.
    domain_ns = DOMAIN_NS
    counts["dp_range_mismatch_removed"] = _delete(
        "DELETE { ?s ?p ?v } "
        "WHERE { "
        "  ?s ?p ?v . "
        "  ?p a owl:DatatypeProperty . "
        "  ?p rdfs:range ?rng . "
        "  FILTER (isLiteral(?v)) "
        f"  FILTER (STRSTARTS(STR(?p), \"{domain_ns}\")) "
        "  FILTER (isIRI(?rng)) "
        "  FILTER (STRSTARTS(STR(?rng), \"http://www.w3.org/2001/XMLSchema#\")) "
        "  FILTER (DATATYPE(?v) != ?rng) "
        "}"
    )

    # Pattern 6b: literal objects on ObjectProperty. Reasoner occasionally
    # writes a literal where an object URI is expected (observed: EQ030
    # hasEquipmentStatus "1210.48"). OP triples must always have IRI objects.
    # 이중 선언 (DatatypeProperty + ObjectProperty 동시) 프로퍼티는 제외한다 —
    # 6b 와 6c 가 합작해 그 프로퍼티의 **모든** 트리플을 지운다 (2026-08-08 실측:
    # literal 1건 + IRI 1건이 각각 6b/6c 에 걸려 값이 0이 됐다). 이중 선언은
    # Architect/Jury 의 T-Box 오류이거나 owl:equivalentProperty 를 거친 OWL RL
    # 폐쇄의 산물이지 A-Box 데이터 결함이 아니다 — 데이터를 지우는 대신 보존하고
    # 별도 카운터로 보고해 T-Box 를 고치게 한다.
    _dual_typed_guard = (
        "  FILTER NOT EXISTS { ?p a owl:DatatypeProperty } "
    )
    counts["op_literal_object_removed"] = _delete(
        "DELETE { ?s ?p ?v } "
        "WHERE { "
        "  ?s ?p ?v . "
        "  ?p a owl:ObjectProperty . "
        "  FILTER (isLiteral(?v)) "
        + _dual_typed_guard +
        f"  FILTER (STRSTARTS(STR(?p), \"{domain_ns}\")) "
        "}"
    )

    # Pattern 6c: IRI/BNode objects on DatatypeProperty — the mirror image of 6b.
    # Observed 2026-07-26: `MaterialB_CD67890_241 steel:materialBId
    # <prov://...#row=10614>`, where the reasoner grafted a provenance IRI onto a
    # DP whose declared range is xsd:string. The A-Box holds no such triple.
    # Left in place this makes materialBId (an owl:FunctionalProperty) carry two
    # values and fail the Functional check, so it must be pruned alongside 6a/6b
    # rather than reported as a data defect.
    #
    # rdfs:range is deliberately NOT required here: unlike 6a (which compares a
    # literal's datatype against a declared xsd range) any non-literal object is
    # invalid on a DatatypeProperty by OWL 2 syntax alone.
    counts["dp_iri_object_removed"] = _delete(
        "DELETE { ?s ?p ?v } "
        "WHERE { "
        "  ?s ?p ?v . "
        "  ?p a owl:DatatypeProperty . "
        "  FILTER (!isLiteral(?v)) "
        "  FILTER NOT EXISTS { ?p a owl:ObjectProperty } "
        f"  FILTER (STRSTARTS(STR(?p), \"{domain_ns}\")) "
        "}"
    )

    # 이중 선언 프로퍼티 수 — 0 이 아니면 T-Box 를 고쳐야 한다는 신호다.
    # 6b/6c 가 보존한 이유를 감사 추적할 수 있게 남긴다 (제거 카운터가 아니므로
    # 이름에 ``_removed`` 를 쓰지 않는다 — total_pruned 합산에서 제외된다).
    counts["dual_typed_properties_preserved"] = len({
        row[0] for row in g.query(
            "SELECT DISTINCT ?p WHERE { "
            "  ?p a owl:DatatypeProperty . ?p a owl:ObjectProperty . "
            f"  FILTER (STRSTARTS(STR(?p), \"{domain_ns}\")) }}",
        )
    })
    if counts["dual_typed_properties_preserved"]:
        logger.warning(
            "추론 정리: DatatypeProperty·ObjectProperty 이중 선언 프로퍼티 %d개 — "
            "트리플을 보존했다 (6b/6c 합작 전삭제 방지). T-Box 선언을 고칠 것.",
            counts["dual_typed_properties_preserved"],
        )

    total = sum(v for k, v in counts.items() if k.endswith("_removed"))
    size_after = len(g)
    counts["total_pruned"] = total
    counts["graph_size_before"] = size_before
    counts["graph_size_after"] = size_after
    counts["reduction_pct"] = round((size_before - size_after) / max(size_before, 1) * 100, 2)
    return counts


def manifest_arithmetic_error(input_stats: dict, output_stats: dict) -> str | None:
    """Return why these manifest stats cannot come from a real run, or None if plausible.

    ``abox_triples`` is derived as ``triples_before - len(tbox)``, so a **negative**
    value means the graph handed to the reasoner was smaller than its own T-Box —
    impossible for a real run, but exactly what a test fixture produces when it mocks
    ``_load_and_merge`` with a 1-triple graph while the deployed T-Box is still read
    for its size.

    Measured on the deployed file (2026-09-01) after the suite ran::

        input: {tbox_triples: 4922, abox_triples: -4921, tacit_triples: 0}
        output: {total_triples: 1}

    The mtime-based staleness guard downstream cannot catch this: contamination always
    writes the manifest *now*, so it is never "older" than the inference output. The
    only defence at write time is the arithmetic itself.
    """
    for key in ("tbox_triples", "abox_triples", "tacit_triples"):
        value = input_stats.get(key)
        if isinstance(value, int) and value < 0:
            return f"input.{key}={value} — 트리플 수는 음수가 될 수 없다"
    total = output_stats.get("total_triples")
    if isinstance(total, int) and total < 0:
        return f"output.total_triples={total} — 트리플 수는 음수가 될 수 없다"
    return None


def _build_inference_loss_manifest(
    prune_stats: dict,
    pollution_removed: int,
    restriction_violation_count: int,
    input_stats: dict,
    output_stats: dict,
) -> dict:
    """추론 단계 loss manifest를 생성한다."""
    total_output = output_stats.get("total_triples", 1)
    total_noise = prune_stats.get("total_pruned", 0) + pollution_removed
    return {
        "phase": "owl_rl_inference",
        "timestamp": datetime.now().isoformat(),
        "input": input_stats,
        "output": output_stats,
        "losses": {
            "self_sameAs_pruned": {
                "count": prune_stats.get("self_sameAs_removed", 0),
                "description": "자기참조 owl:sameAs 제거",
            },
            "self_equivalentClass_pruned": {
                "count": prune_stats.get("self_equivalentClass_removed", 0),
                "description": "자기참조 owl:equivalentClass 제거",
            },
            "implicit_subClassOf_pruned": {
                "count": prune_stats.get("implicit_subClassOf_removed", 0),
                "description": "owl:Thing/rdfs:Resource 암시적 체인 제거",
            },
            "type_pollution_removed": {
                "count": pollution_removed,
                "description": "AllDisjointClasses 위반 제거",
            },
            "restriction_violations": {
                "count": restriction_violation_count,
                "description": "Restriction 불만족 인스턴스",
            },
        },
        "preservation_score": {
            "meaningful_triples_ratio": round(
                max(total_output - total_noise, 0) / max(total_output, 1), 4
            ),
            "noise_removed_ratio": round(total_noise / max(total_output, 1), 4),
        },
    }


def _estimate_input_triples(tbox_path: str, abox_path: str) -> int:
    """T-Box + A-Box + tacit/*.ttl 의 추정 triple 수를 빠르게 산출.

    로컬 추론 안전 한계를 확인하기 위한 line count 기반 휴리스틱이다.
    Turtle 은 보통 한 triple 당 1줄이고, 압축된 형태에서도 실제 triple 수의
    0.5~1배 범위다.

    How: 각 ttl 파일을 binary 로 열고 \\n 카운트. 빈 줄 / 주석 / @prefix 도
    포함되지만 큰 파일에서 비율 미미. 1MB chunk 단위로 read.

    Returns: 추정 triple 수. 파일 없으면 0.
    """
    import glob as _glob

    from config import SOURCE_TACIT_DIR as _STD
    candidates: list[str] = []
    tbox = tbox_path or TBOX_PATH
    abox = abox_path or ABOX_PATH
    if os.path.exists(tbox):
        candidates.append(tbox)
    if os.path.exists(abox):
        candidates.append(abox)
    if os.path.isdir(_STD):
        candidates.extend(_glob.glob(os.path.join(_STD, "*.ttl")))

    total_lines = 0
    for path in candidates:
        try:
            with open(path, "rb") as fh:
                while True:
                    chunk = fh.read(1024 * 1024)
                    if not chunk:
                        break
                    total_lines += chunk.count(b"\n")
        except OSError:
            continue
    return total_lines


def _inference_inputs_newer_than_output(tbox_path: str, abox_path: str) -> bool:
    """입력 파일(T-Box, A-Box, tacit/*.ttl) 중 하나라도 INFERRED_PATH보다 새로우면 True.

    전부 오래됐다 = 기존 all_inferred.ttl이 최신이므로 재추론 skip 가능.
    """
    import glob as _glob
    if not os.path.exists(INFERRED_PATH):
        return True
    out_mtime = os.path.getmtime(INFERRED_PATH)
    tbox = tbox_path or TBOX_PATH
    abox = abox_path or ABOX_PATH
    from config import SOURCE_TACIT_DIR as _STD
    candidates: list[str] = []
    if os.path.exists(tbox):
        candidates.append(tbox)
    if os.path.exists(abox):
        candidates.append(abox)
    if os.path.isdir(_STD):
        candidates.extend(_glob.glob(os.path.join(_STD, "*.ttl")))
    for p in candidates:
        try:
            if os.path.getmtime(p) > out_mtime:
                return True
        except OSError:
            return True
    return False


def _build_cache_skip_response(start: float) -> str:
    """입력이 출력보다 오래됐을 때 (캐시 hit) 반환할 응답 빌드."""
    duration = round(time.monotonic() - start, 3)
    file_size = os.path.getsize(INFERRED_PATH)
    logger.info("OWL RL 추론 스킵 (캐시 hit): INFERRED_PATH=%s", INFERRED_PATH)
    return json.dumps({
        "success": True,
        "engine": "cached",
        "skipped": True,
        "reason": "inference_cache_hit",
        "output_path": INFERRED_PATH,
        "output_size_bytes": file_size,
        "duration_seconds": duration,
        "hint": "force=True 로 재추론 가능",
    }, ensure_ascii=False, indent=2)


def _snapshot_pre_graph(g: Graph) -> tuple[str, frozenset]:
    """추론 전 그래프 스냅샷을 임시 파일 + 해시 set 으로 저장.

    Returns: (임시 파일 경로, pre triple hashes — delta 계산용).
    """
    import tempfile as _tempfile
    fd, snapshot_path = _tempfile.mkstemp(suffix=".nt")
    os.close(fd)
    try:
        g.serialize(snapshot_path, format="nt")
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(snapshot_path)
        raise
    return snapshot_path, frozenset(hash(t) for t in g)


def _analyze_explosion_risk(g: Graph) -> tuple[str, list[str]]:
    """TransitiveProperty / SymmetricProperty 비율로 추론 폭발 사전 위험도 평가.

    Returns: (risk_level, chain_warnings).
    """
    graph_size = max(len(g), 100)
    risk = "LOW"
    warnings: list[str] = []
    for tp in g.subjects(RDF.type, OWL.TransitiveProperty):
        triples = sum(1 for _ in g.triples((None, tp, None)))
        ratio = triples / graph_size
        if ratio > 0.10:
            risk = "HIGH"
            warnings.append(f"{_local_name(str(tp))}: {triples} ({ratio:.1%})")
        elif ratio > 0.02:
            if risk != "HIGH":
                risk = "MEDIUM"
            warnings.append(f"{_local_name(str(tp))}: {triples} ({ratio:.1%})")
    for sp in g.subjects(RDF.type, OWL.SymmetricProperty):
        triples = sum(1 for _ in g.triples((None, sp, None)))
        ratio = triples / graph_size
        if ratio > 0.05:
            if risk != "HIGH":
                risk = "MEDIUM"
            warnings.append(f"Symmetric {_local_name(str(sp))}: {triples} ({ratio:.1%})")
    if risk == "HIGH":
        logger.warning("추론 폭발 위험 HIGH — 대규모 체인: %s", warnings)
    return risk, warnings


def _run_reasonable(
    g: Graph,
    tbox_path: str = "",
    abox_path: str = "",
) -> int:
    """Rust reasonable OWL 2 RL closure 실행. 신규 추가된 triple 수 반환.

    기본 모드: g (rdflib Graph) 의 triple 을 reasoner 로 옮긴 뒤 closure 계산.
    OPT-D 모드 (env OPT_D_ENABLED=true): tbox/abox/tacit 을 reasoner.load_file
    로 직접 적재 (rdflib parse 우회). g 는 비어있는 채로 진입하고 closure
    결과를 chunk 단위로 g 에 추가.

    Args:
        g: rdflib Graph. 기본 모드에서는 입력 데이터 보유, OPT-D 에서는 빈
           그래프로 진입해 closure 결과를 받음.
        tbox_path / abox_path: OPT-D 모드에서 reasoner.load_file 호출에 사용.
           기본 모드에서는 무시 (g 에 이미 적재됨).

    Returns:
        g 에 새로 추가된 triple 수.
    """
    from tools.heartbeat import Heartbeat

    if _opt_d_enabled():
        # OPT-D: reasoner 가 직접 파일 적재 → reason() → chunk add to g.
        from tools.inference_optd import (
            build_or_get_tbox_closure,
            stream_load_inputs,
        )
        tbox = tbox_path or TBOX_PATH
        abox = abox_path or ABOX_PATH

        # T-Box closure cache (mtime-invalidated). T-Box 변경 안 되면 매 호출 0초 hit.
        # Caller 가 결과 path 를 직접 사용하지는 않지만, 캐시 워밍업 효과가
        # 후속 reasoner 의 subClassOf 체인 조회 비용을 줄임.
        t_tbc = time.monotonic()
        try:
            build_or_get_tbox_closure(tbox)
            _stage("tbox-closure-cache", t_tbc)
        except Exception as e:
            logger.warning("OPT-D: T-Box closure 캐시 실패 (무시 가능) — %s", e)

        reasoner = PyReasoner()
        logger.info("OPT-D: T-Box 로드 시작 — %s", os.path.basename(tbox))
        t0 = time.monotonic()
        reasoner.load_file(tbox)
        logger.info("OPT-D: T-Box 로드 완료 (%.1fs)", time.monotonic() - t0)

        if os.path.exists(abox):
            size_mb = os.path.getsize(abox) / 1024 / 1024
            logger.info("OPT-D: A-Box 로드 시작 — %s (%.0f MB)",
                        os.path.basename(abox), size_mb)
            t0 = time.monotonic()
            reasoner.load_file(abox)
            logger.info("OPT-D: A-Box 로드 완료 (%.1fs)", time.monotonic() - t0)

        tacit_count = stream_load_inputs(reasoner)
        logger.info("OPT-D: tacit 폴더 적재 완료 — %d files", tacit_count)

        with Heartbeat(stage="S8_INFERENCE", interval=60.0, tool_logger=logger):
            closure_triples = reasoner.reason()
        del reasoner  # Rust 측 메모리 즉시 회수

        len(closure_triples)

        # OPT-D closure 적재 — 두 단계 streaming:
        #   1) closure_triples (Python list, peak RSS 위험) → NT 파일 streaming
        #      write. chunk 단위 .n3() 호출 + del list[:chunk] 로 즉시 폐기.
        #   2) NT 파일 → Oxigraph store bulk_load. Rust 측에서 streaming 파싱.
        # 이전 구현은 closure_triples 를 통째 들고 있으면서 Python 단에서
        # g.add() 호출 (12M+ 회 함수 호출 + 객체 보유) → RSS 9.9 GB peak.
        # 본 경로는 RSS 4.7 GB 로 절감 (PR2 원본 OPT-D 측정값).
        from tools.inference_optd import (
            bulk_load_nt_into_graph,
            stream_closure_to_nt_file,
        )
        closure_nt_path = INFERRED_PATH.replace(
            "all_inferred.ttl", "all_inferred.closure.nt",
        )

        t_apply = time.monotonic()
        written = stream_closure_to_nt_file(
            closure_triples, closure_nt_path, logger=logger,
        )
        del closure_triples  # closure list 즉시 폐기 (메모리 회수)
        try:
            nt_size_mb = os.path.getsize(closure_nt_path) / 1024 / 1024
        except OSError:
            nt_size_mb = 0.0
        logger.info(
            "OPT-D: closure NT streaming 완료 — %d triples, %.0f MB",
            written, nt_size_mb,
        )
        _stage("closure-to-nt", t_apply)

        # NT → Oxigraph store. 기본 모드에서는 in-memory g 라 효과 제한적이지만,
        # disk-backed g (OXIGRAPH_STORE_PATH 또는 caller 가 disk_path 넘긴 경우)
        # 에서는 RocksDB 인덱스 직접 적재로 큰 메모리 절감.
        # 후처리 SKIP + NT 포맷 조합 (D shortcut, _serialize 단계에서 처리) 시
        # 이 단계의 결과는 _serialize_inferred_graph 가 hard link 로 활용.
        t_load = time.monotonic()
        added = bulk_load_nt_into_graph(g, closure_nt_path, logger=logger)
        logger.info(
            "OPT-D: NT → store bulk_load 완료 — 신규 %d triples", added,
        )
        _stage("nt-to-store", t_load)

        logger.info(
            "OWL 2 RL (reasonable, OPT-D) 완료: closure %d triples → g %d triples",
            written, added,
        )
        return added

    # 기본 모드: g 에 이미 적재된 데이터로 reasoner 구성 후 closure.
    reasoner = PyReasoner()
    reasoner.from_graph(g)
    with Heartbeat(stage="S8_INFERENCE", interval=60.0, tool_logger=logger):
        closure_triples = reasoner.reason()
    added = 0
    for t in closure_triples:
        if t not in g:
            g.add(t)
            added += 1
    logger.info(
        "OWL 2 RL (reasonable) 완료: closure %d triples, 신규 %d triples",
        len(closure_triples), added,
    )
    return added


def _remove_type_pollution(
    g: Graph, pre_virtual: Graph | None, tbox: Graph,
) -> tuple[int, list[dict]]:
    """AllDisjointClasses 위반 타입을 사후 제거.

    Returns: (removed_count, pollution_items).
    """
    if pre_virtual is None:
        return 0, []
    try:
        items = _detect_type_pollution(pre_virtual, g, tbox)
    except Exception as e:
        logger.warning("타입 오염 탐지 실패: %s", e)
        return 0, []
    removed = 0
    for item in items:
        inst_local = item.get("instance")
        polluted_local = item.get("polluted_type")
        inst_uri = DOMAIN_INST_NS_OBJ[inst_local] if inst_local else None
        polluted_uri = DOMAIN_NS_OBJ[polluted_local] if polluted_local else None
        if inst_uri and polluted_uri and (inst_uri, RDF.type, polluted_uri) in g:
            g.remove((inst_uri, RDF.type, polluted_uri))
            removed += 1
    return removed, items


#: 이번 실행이 실제로 쓴 사이드카 경로. ``_stamp_inferred_outputs`` 가 이것만 각인한다.
#:
#: ``_run_owl_rl_inference_sync`` 진입 시 비운다. 동시 실행은 이미
#: ``INFERRED_PATH`` 자체에서 충돌하므로 이 목록이 약한 고리는 아니다.
_WRITTEN_SIDECARS: list[str] = []


def _stamp_inferred_outputs(paths: list[str]) -> None:
    """직렬화 뒤, **이번 실행이 실제로 쓴** 사이드카에 출력 지문을 덧붙인다.

    ⚠️ 고정 이름 목록을 돌면 안 된다. 처음 그렇게 썼더니 이번 실행이 만들지 않은
    ``inference_contradictions.json`` (배포본 mtime 2026-08-15, 추론 산출물보다 16일
    낡음) 에 **현재 추론 결과의 지문**을 찍었다 — 계보를 기록하려고 만든 장치가
    거짓 계보를 만드는 셈이다. 쓰기 가드가 그 실제 쓰기를 잡아 드러났다.

    ``atomic_write_json`` 을 writer 로 넘기는 것도 같은 이유다. 이 모듈을 patch 해
    쓰기를 가로채는 테스트가 여럿이고, common 의 primitive 를 직접 쓰면 그 patch 를
    우회해 배포 트리에 실제로 쓴다.
    """
    for path in paths:
        if path:
            add_source_artifacts(
                path, writer=atomic_write_json, inferred=INFERRED_PATH,
            )


def _emit_delta_and_loss_manifest(
    g: Graph, pre_hashes: frozenset, prune_stats: dict,
    pollution_removed: int, restriction_stats: dict,
    triples_before: int, tbox: Graph, tacit_count: int,
) -> tuple[Graph, str, int]:
    """추론 delta 분리 저장 + Inference loss manifest 작성.

    Returns: (delta_graph, delta_path, delta_count).
    """
    delta_g = _new_graph()
    for prefix, ns in g.namespaces():
        delta_g.bind(prefix, ns)
    delta_count = 0
    for triple in g:
        if hash(triple) not in pre_hashes:
            delta_g.add(triple)
            delta_count += 1

    delta_path = INFERRED_PATH.replace("all_inferred.ttl", "inferred_delta.ttl")
    if delta_count > 0:
        from domain.tbox_utils import fast_serialize_turtle as _fst
        atomic_write(delta_path, _fst(delta_g))

    try:
        input_stats = {
            "tbox_triples": len(tbox),
            "abox_triples": triples_before - len(tbox),
            "tacit_triples": tacit_count,
        }
        output_stats = {"total_triples": len(g), "inferred_new": delta_count}
        # Refuse to persist stats that cannot come from a real run. Downstream
        # (``kg_validation._raw_triples_from_inference_manifest``) treats this file as
        # ground truth for "triples before inference", so a nonsense value there turns
        # the inference-sanity axis into a fabricated PASS rather than a FAIL — nobody
        # looks at a green gate.
        arithmetic_error = manifest_arithmetic_error(input_stats, output_stats)
        if arithmetic_error:
            raise ValueError(f"loss manifest 산술 불성립 — 저장 거부: {arithmetic_error}")
        manifest = _build_inference_loss_manifest(
            prune_stats,
            pollution_removed,
            restriction_stats.get("restriction_violation_count", 0),
            input_stats,
            output_stats,
        )
        # 소스 계보 각인. **입력만** 여기서 찍는다 — 이 함수는 step 7 이고
        # all_inferred.ttl 직렬화는 step 9 다. 여기서 출력을 해시하면 이전 세대
        # 파일이 찍힌다. 출력 지문은 직렬화 뒤 _stamp_inferred_outputs 가 덧붙인다.
        manifest["_source"] = source_stamp(
            tbox=TBOX_PATH, abox=ABOX_PATH, delta=delta_path,
        )
        manifest_path = os.path.join(
            os.path.dirname(INFERRED_PATH), "inference_loss_manifest.json",
        )
        atomic_write_json(manifest_path, manifest)
        _WRITTEN_SIDECARS.append(manifest_path)
    except Exception as e:
        logger.warning("Inference loss manifest 생성 실패: %s", e)

    return delta_g, delta_path, delta_count


def _emit_justifications_and_provenance(
    pre_virtual: Graph | None, delta_g: Graph, tbox: Graph, delta_count: int,
) -> tuple[str | None, dict | None]:
    """추론 정당화 추적 + PROV-O sidecar + I2 per-triple reification 기록.

    Returns: (justification_path, justification_summary) — 실패 시 (path, None).
    """
    justification_path = os.path.join(
        os.path.dirname(INFERRED_PATH), "inference_justifications.json",
    )
    pre_g = pre_virtual if pre_virtual is not None else _new_graph()
    try:
        justifications = _build_justifications_from_graphs(pre_g, delta_g, tbox)
        by_rule, unjustified_triples = _aggregate_justifications(justifications)
        justified_count = sum(
            v["count"] for k, v in by_rule.items()
            if k not in ("unknown", "already_exists")
        )
        unjustified_count = by_rule.get("unknown", {}).get("count", 0)
        summary = {
            "generated_at": datetime.now().isoformat(),
            "total_inferred": delta_count,
            "justified": justified_count,
            "unjustified": unjustified_count,
            "by_rule": by_rule,
            "unjustified_triples": unjustified_triples[:50],
            "_source": source_stamp(tbox=TBOX_PATH, abox=ABOX_PATH),
        }
        atomic_write_json(justification_path, summary)
        _WRITTEN_SIDECARS.append(justification_path)
        prov_path = _emit_prov_sidecar(summary)
        if prov_path:
            _emit_per_triple_reification(justifications, pre_g, tbox, prov_path)
        return justification_path, summary
    except Exception as e:
        logger.warning("추론 정당화 추적 실패: %s", e)
        return justification_path, None


def _aggregate_justifications(justifications: list[dict]) -> tuple[dict, list[dict]]:
    """rule 별 카운트 + 샘플 + unjustified triple 목록 집계."""
    by_rule: dict[str, dict] = {}
    unjustified: list[dict] = []
    for j in justifications:
        rule = j["rule"]
        entry = by_rule.setdefault(rule, {"count": 0, "sample": []})
        entry["count"] += 1
        if len(entry["sample"]) < 3:
            entry["sample"].append({
                "s": str(j["triple"][0]),
                "p": str(j["triple"][1]),
                "o": str(j["triple"][2]),
                "prerequisites": j["prerequisites"],
            })
        if rule == "unknown" and len(unjustified) < 50:
            unjustified.append({
                "s": str(j["triple"][0]),
                "p": str(j["triple"][1]),
                "o": str(j["triple"][2]),
            })
    return by_rule, unjustified


def _emit_prov_sidecar(summary: dict) -> str | None:
    """PROV-O TTL sidecar 작성. 실패 시 None."""
    try:
        from tools.provenance import write_inference_provenance
        prov_path = os.path.join(
            os.path.dirname(INFERRED_PATH), "inference_provenance.ttl",
        )
        n = write_inference_provenance(
            summary, prov_path, input_ttl_paths=[TBOX_PATH, ABOX_PATH],
        )
        logger.info("PROV-O 기록: %s (%d triples)", prov_path, n)
        return prov_path
    except Exception as e:
        logger.warning("PROV-O 기록 실패: %s", e)
        return None


def _emit_per_triple_reification(
    justifications: list[dict], pre_g: Graph, tbox: Graph, prov_path: str,
) -> None:
    """I2: 각 추론 triple 의 reification 을 기존 prov sidecar 에 append.

    환경변수: I2_PER_TRIPLE_JUSTIFICATION (기본 true) / I2_MAX_REIFY (rule 별 상한).

    I2_MAX_REIFY 기본값은 200000 (rule 별 상한) — 과거 unbounded(=0) 일 때
    대규모 추론에서 reification 그래프가 RSS 7.7GB 까지 치솟아 서버가 다운된
    이력이 있어 안전 상한을 둔다. 0 으로 명시하면 unbounded (옛 동작, 비권장).
    """
    if os.getenv("I2_PER_TRIPLE_JUSTIFICATION", "true").lower() == "false":
        return
    try:
        from rdflib import URIRef as _U

        from tools.provenance import build_per_triple_reification
        activity_uri = _U(
            f"urn:activity:inference_{datetime.now().strftime('%Y%m%dT%H%M%S')}",
        )
        max_reify = int(os.getenv("I2_MAX_REIFY", "200000"))
        reif_g = build_per_triple_reification(
            justifications, pre_g, tbox, activity_uri,
            max_entries=max_reify if max_reify > 0 else None,
        )
        if len(reif_g) == 0:
            # I2 = 추론 triple 근거(rule/confidence) reification. 용어: docs/reference/task-glossary.md
            logger.info("추론 근거 reification (I2): 대상 없음")
            return
        # 기존 prov sidecar 에 append (rule-level PROV-O 보존).
        existing_g = _new_graph()
        if os.path.exists(prov_path):
            try:
                existing_g.parse(prov_path, format="turtle")
            except Exception as e:
                logger.warning(
                    "기존 inference_provenance.ttl parse 실패 (overwrite): %s", e,
                )
        for t in reif_g:
            existing_g.add(t)
        existing_g.serialize(destination=prov_path, format="turtle")
        logger.info(
            "추론 근거 reification (I2): %d triples (%s)", len(reif_g), prov_path,
        )
    except Exception as e:
        logger.warning("추론 근거 reification (I2) 실패: %s", e)


def _serialize_inferred_graph(g: Graph) -> float:
    """all_inferred 결과 직렬화 — turtle (Oxigraph 가속) 또는 N-Triples.

    D shortcut: OPT-D 모드 + 후처리 모두 SKIP + NT 포맷 시 closure.nt 가
    이미 최종 결과와 동일하므로 추가 직렬화 없이 hard link 만 — final dump
    52초 + 디스크 쓰기 2.6 GB 절감 (PR2 원본 측정).

    Returns: 직렬화 소요 초.
    """
    t_start = time.monotonic()
    from domain.tbox_utils import fast_serialize_turtle, invalidate_graph_cache
    fmt = os.getenv("INFERENCE_SERIALIZE_FORMAT", "turtle").lower()
    is_nt_fmt = fmt in ("nt", "n-triples", "ntriples")

    # D shortcut 조건: OPT-D + 후처리 모두 SKIP + NT 포맷 + closure.nt 존재.
    # _flag_off(env) 가 True = "단계 실행" 이므로, 모두 SKIP 은 not _flag_off(...).
    closure_nt_path = INFERRED_PATH.replace(
        "all_inferred.ttl", "all_inferred.closure.nt",
    )
    all_post_skipped = (
        (not _flag_off("SKIP_POST_RL_RESTRICTIONS"))
        and (not _flag_off("SKIP_POST_RL_POLLUTION"))
        and (not _flag_off("SKIP_POST_RL_PRUNE"))
        and (not _flag_off("SKIP_POST_RL_DELTA"))
    )
    shortcut_used = False
    if (
        _opt_d_enabled() and all_post_skipped and is_nt_fmt
        and os.path.exists(closure_nt_path)
    ):
        try:
            if os.path.exists(INFERRED_PATH):
                os.unlink(INFERRED_PATH)
            os.link(closure_nt_path, INFERRED_PATH)
            shortcut_used = True
            logger.info(
                "OPT-D shortcut: closure.nt → INFERRED_PATH 직접 link "
                "(후처리 모두 SKIP + NT 포맷 — final dump 단계 생략)",
            )
        except OSError as e:
            logger.warning("OPT-D shortcut hard link 실패 → 정상 dump: %s", e)

    if not shortcut_used:
        if is_nt_fmt:
            import io as _io

            from pyoxigraph import DefaultGraph as _DG
            from pyoxigraph import RdfFormat as _Fmt
            inner = getattr(g.store, "_inner", None)
            if inner is not None:
                buf = _io.BytesIO()
                graphs = list(inner.named_graphs())
                src = graphs[0] if graphs else _DG()
                inner.dump(buf, _Fmt.N_TRIPLES, from_graph=src)
                atomic_write(INFERRED_PATH, buf.getvalue().decode("utf-8"))
            else:
                atomic_write(INFERRED_PATH, g.serialize(format="nt"))
        else:
            atomic_write(INFERRED_PATH, fast_serialize_turtle(g))

    # closure.nt 정리: INFERENCE_KEEP_CLOSURE_NT 가 truthy 가 아니면 삭제.
    # D shortcut 사용 시에는 hard link 라 inode 공유 — closure.nt 삭제해도
    # INFERRED_PATH 의 데이터는 그대로 유지됨.
    keep_closure = os.getenv("INFERENCE_KEEP_CLOSURE_NT", "").lower() in (
        "1", "true", "yes", "on",
    )
    if not keep_closure and os.path.exists(closure_nt_path):
        try:
            os.unlink(closure_nt_path)
            logger.info("OPT-D: closure.nt 정리 (INFERENCE_KEEP_CLOSURE_NT=true 로 보존 가능)")
        except OSError as e:
            logger.warning("closure.nt 정리 실패 (무시): %s", e)

    invalidate_graph_cache()
    return round(time.monotonic() - t_start, 3)


def _run_owl_rl_inference_sync(
    tbox_path: str = "", abox_path: str = "", fast_mode: bool = False,
    force: bool = False,
) -> str:
    """T-Box + A-Box + 암묵지를 병합하고 OWL RL 추론을 실행한다 (동기 본문).

    이 함수는 wall-clock 6분+ 가 걸린다(실측: reason 53s + post-rl-cleanup 146s
    + step10 42s 등, peak RSS 3.8GB — OOM 아님). MCP 진입점 ``run_owl_rl_inference``
    는 이 함수를 동기로 await 하지 않고 daemon 워커 스레드에서 돌린 뒤 job_id 만
    즉시 반환한다 — 단일 CallToolRequest 가 6분 응답을 끌면 stdio 클라이언트가
    per-request 타임아웃으로 stdin 을 닫아 서버가 EOF 로 종료되기 때문
    (2026-06-20 규명, ``-32000 Connection closed``). 테스트는 이 ``_sync`` 함수를
    직접 호출해 동기 검증을 유지한다.

    예상 소요시간: 8~15분 (75만 트리플 기준), fast_mode=True 시 ~2분.
    입력 파일(T-Box/A-Box/tacit)이 이전 all_inferred.ttl보다 오래됐으면 **스킵** 후
    캐시된 결과 경로를 반환한다 (force=True 시 무시하고 재실행).

    추론된 전체 그래프를 data/generated/inferred/all_inferred.ttl에 저장.
    data/source/tacit/*.ttl의 암묵지 TTL 파일도 자동으로 병합된다.
    subClassOf 체인, domain/range 타입 추론, inverseOf, TransitiveProperty 등을 수행.

    입력 추정 줄 수가 로컬 안전 한계 12M 을 넘으면 자동으로 원격 엔진에
    위임하지 않고 입력 축소 안내를 반환한다.

    응답 JSON 의 engine 필드: "reasonable" | "cached".

    Args:
        tbox_path: data/generated/tbox 아래 T-Box TTL 파일명.
        abox_path: data/generated/abox 아래 A-Box TTL 파일명.
        fast_mode: True면 마스터 데이터만 추론 (스키마 검증용, 빠름).
                   라우팅과 무관하게 항상 reasonable 사용.
        force: True면 mtime 캐시 무시하고 무조건 재실행.
    """
    pre_snapshot_path: str | None = None
    # 이번 실행이 쓴 사이드카만 각인하기 위해 목록을 비운다. 비우지 않으면 이전
    # 실행이 쓴 경로가 남아, 이번에 만들지 않은 파일에 현재 지문을 찍는다.
    _WRITTEN_SIDECARS.clear()
    try:
        start = time.monotonic()
        logger.info("=" * 60)
        logger.info("OWL RL 추론 시작 — initial RSS=%.0f MB", _rss_mb())
        logger.info("=" * 60)

        # 1. mtime 캐시 — fast_mode 는 입력 set 이 다르므로 캐시에서 제외.
        if (not force and not fast_mode
                and not _inference_inputs_newer_than_output(tbox_path, abox_path)):
            return _build_cache_skip_response(start)

        if fast_mode:
            if os.path.exists(MASTER_DATA_PATH):
                abox_path = MASTER_DATA_PATH
                logger.info("[Fast Mode] 마스터 데이터만 추론: %s", MASTER_DATA_PATH)
            else:
                logger.warning("[Fast Mode] master_data.ttl 없음, 전체 A-Box 사용")

        # 1.5. 로컬 안전 한계 — 초과 입력을 원격 엔진에 자동 위임하지 않는다.
        est_triples = _estimate_input_triples(tbox_path, abox_path)
        logger.info(
            "로컬 추론 입력 추정: %d 줄 (limit %d)",
            est_triples,
            _LOCAL_INFERENCE_MAX_ESTIMATED_TRIPLES,
        )
        if est_triples > _LOCAL_INFERENCE_MAX_ESTIMATED_TRIPLES:
            return error_response(
                (
                    "로컬 추론 입력이 안전 한계를 초과했습니다: "
                    f"{est_triples:,} > "
                    f"{_LOCAL_INFERENCE_MAX_ESTIMATED_TRIPLES:,}"
                ),
                hint="입력을 축소하거나 여러 로컬 실행으로 분할한 뒤 다시 시도하세요.",
                logger=logger,
            )

        # 2. T-Box + A-Box + 암묵지 병합
        t_load_start = time.monotonic()
        g, tacit_count = _load_and_merge(tbox_path, abox_path)
        t_load = round(time.monotonic() - t_load_start, 3)
        triples_before = len(g)
        has_abox = os.path.exists(abox_path or ABOX_PATH)
        _stage("input-load", t_load_start)

        # 3. 추론 전 스냅샷
        pre_snapshot_path, pre_hashes = _snapshot_pre_graph(g)

        # 4. 추론 폭발 위험 분석 + reasonable 실행
        explosion_risk, chain_warnings = _analyze_explosion_risk(g)
        graph_size = max(len(g), 100)

        t_owlrl_start = time.monotonic()
        _run_reasonable(g, tbox_path=tbox_path, abox_path=abox_path)
        t_owlrl = round(time.monotonic() - t_owlrl_start, 3)
        triples_after_rl = len(g)
        _stage("reason()", t_owlrl_start)

        expansion_capped = explosion_risk == "HIGH" and len(g) > 3 * graph_size
        if expansion_capped:
            logger.warning(
                "추론 폭발 감지: %d → %d 트리플 (%.1fx)",
                graph_size, len(g), len(g) / graph_size,
            )

        # 5. T-Box 로드 + Restriction 후처리 (SKIP_POST_RL_RESTRICTIONS 로 우회)
        t_restriction_start = time.monotonic()
        from domain.tbox_utils import fast_parse_turtle
        tbox = _new_graph()
        fast_parse_turtle(tbox, tbox_path or TBOX_PATH)
        if _flag_off("SKIP_POST_RL_RESTRICTIONS"):
            restriction_stats = _apply_owl_restrictions(g, tbox)
        else:
            logger.info("SKIP_POST_RL_RESTRICTIONS=true — Restriction 후처리 우회")
            restriction_stats = {
                "hasValue": 0, "someValuesFrom": 0, "minCardinality": 0,
                "allValuesFrom": 0, "total": 0, "skipped": True,
            }
        t_restriction = round(time.monotonic() - t_restriction_start, 3)

        # 6. 타입 오염 제거 + noise prune (SKIP_POST_RL_POLLUTION / PRUNE 로 우회)
        t_cleanup_start = time.monotonic()
        pre_virtual = _new_graph()
        try:
            pre_virtual.parse(pre_snapshot_path, format="nt")
        except Exception as e:
            logger.warning("pre snapshot 복원 실패: %s", e)
            pre_virtual = None
        if _flag_off("SKIP_POST_RL_POLLUTION"):
            pollution_removed, pollution_items = _remove_type_pollution(g, pre_virtual, tbox)
        else:
            logger.info("SKIP_POST_RL_POLLUTION=true — 타입 오염 제거 우회")
            pollution_removed, pollution_items = 0, []
        if _flag_off("SKIP_POST_RL_PRUNE"):
            prune_stats = _prune_inference_noise(g)
        else:
            logger.info("SKIP_POST_RL_PRUNE=true — noise prune 우회")
            prune_stats = {"total_pruned": 0, "skipped": True}
        if prune_stats.get("total_pruned", 0) > 0:
            logger.info(
                "추론 노이즈 제거: %d 트리플 (%s)",
                prune_stats["total_pruned"],
                ", ".join(f"{k}={v}" for k, v in prune_stats.items()
                          if k.endswith("_removed") and v > 0),
            )
        if pollution_items:
            _contradictions_path = os.path.join(
                os.path.dirname(INFERRED_PATH), "inference_contradictions.json",
            )
            _WRITTEN_SIDECARS.append(_contradictions_path)
            atomic_write_json(
                _contradictions_path,
                {
                    "generated_at": datetime.now().isoformat(),
                    "total_contradictions": len(pollution_items),
                    "records": pollution_items,
                    "_source": source_stamp(tbox=TBOX_PATH, abox=ABOX_PATH),
                },
            )
        t_cleanup = round(time.monotonic() - t_cleanup_start, 3)
        _stage("post-rl-cleanup", t_cleanup_start)

        triples_after = len(g)
        inferred_count = triples_after - triples_before

        # 7. delta + loss manifest (SKIP_POST_RL_DELTA 로 우회)
        if _flag_off("SKIP_POST_RL_DELTA"):
            delta_g, delta_path, delta_count = _emit_delta_and_loss_manifest(
                g, pre_hashes, prune_stats, pollution_removed,
                restriction_stats, triples_before, tbox, tacit_count,
            )
        else:
            logger.info("SKIP_POST_RL_DELTA=true — delta + loss manifest 우회")
            delta_g, delta_path, delta_count = _new_graph(), None, 0

        # 8. justification + PROV-O + I2 reification (SKIP_POST_RL_DELTA 와 묶음)
        if _flag_off("SKIP_POST_RL_DELTA"):
            justification_path, justification_summary = _emit_justifications_and_provenance(
                pre_virtual, delta_g, tbox, delta_count,
            )
        else:
            justification_path, justification_summary = None, None

        # 9. 직렬화
        t_serialize_start = time.monotonic()
        t_serialize = _serialize_inferred_graph(g)
        # 사이드카에 **출력** 지문을 덧붙인다. 여기서 하는 이유는 step 7~8 시점에
        # all_inferred.ttl 이 아직 이전 세대이기 때문이다 (add_source_artifacts
        # docstring 참조). 이 각인이 있어야 "매니페스트가 추론 결과보다 낡았다" 를
        # mtime 이 아니라 내용으로 판정할 수 있다 — 오염은 항상 지금 쓰이므로
        # mtime 으로는 절대 낡아 보이지 않는다.
        _stamp_inferred_outputs(list(_WRITTEN_SIDECARS))
        file_size = os.path.getsize(INFERRED_PATH)
        _stage("final-serialize", t_serialize_start)

        # 10. 추론 품질 분석 (pre_virtual 재사용)
        try:
            quality_result = _analyze_inference_quality_internal(
                pre_virtual if pre_virtual is not None else _new_graph(),
                g, tbox_path=tbox_path or TBOX_PATH,
            )
            inference_quality = {
                "type_pollution_count": quality_result.get("type_pollution", {}).get("count", 0),
                "explosion_level": quality_result.get("explosion", {}).get("level", "NORMAL"),
                "top_inferred_types": quality_result.get("top_inferred_types", [])[:5],
            }
        except Exception:
            inference_quality = None

        duration = round(time.monotonic() - start, 1)

        domain_str = DOMAIN_NS
        # ``rdf:type`` 의 object 를 전부 세면 **클래스가 아닌 것** 까지 섞인다.
        #
        # 이 T-Box 는 ``owl:Restriction`` 92개를 (익명 blank node 가 아니라) 이름 있는
        # IRI 로 선언한다 (예: ``AlarmEvents_hasAlarmTag_someValuesFrom``). 추론기가
        # ``someValuesFrom``/``minCardinality`` 를 만족하는 인스턴스에 그 IRI 를
        # ``rdf:type`` 으로 붙이므로, 단순 집계는 그것들을 "인스턴스를 가진 클래스" 로
        # 센다.
        #
        # 실측 (2026-08-17): 159 로 보고된 값의 실제 구성은
        #   owl:Class 선언        66   ← 진짜 도메인 클래스
        #   owl:Restriction       92   ← 클래스식에 이름만 붙인 것
        #   그 외                  1   ← 도메인 NS 아래 외부(IOF) 클래스
        # 즉 **2.4배 부풀려** 있었다. 커버리지·품질 점수의 분모로 쓰이면 실제보다
        # 좋게 보인다.
        #
        # ``owl:Restriction`` 여부로 판정한다 — 이름 패턴(``_someValuesFrom`` 접미)으로
        # 거르면 같은 이름 규칙을 쓰지 않는 도메인에서 조용히 no-op 이 된다.
        restriction_uris = {
            str(s) for s in tbox.subjects(RDF.type, OWL.Restriction)
        }
        typed_objects = {
            str(o) for o in g.objects(None, RDF.type)
            if str(o).startswith(domain_str)
        }
        classes = {
            _local_name(u) for u in typed_objects if u not in restriction_uris
        }
        named_restrictions_typed = len(typed_objects & restriction_uris)

        return json.dumps({
            "success": True,
            "engine": "reasonable",
            "fast_mode": fast_mode,
            "triples_before": triples_before,
            "triples_after_owl_rl": triples_after_rl,
            "triples_after": triples_after,
            "inferred_count": inferred_count,
            "inferred_ratio": round(inferred_count / triples_before * 100, 1) if triples_before > 0 else 0,
            "restriction_post_processing": restriction_stats,
            "duration_seconds": duration,
            "timing": {
                "load_seconds": t_load,
                "owlrl_seconds": t_owlrl,
                "restriction_seconds": t_restriction,
                "cleanup_seconds": t_cleanup,
                "serialize_seconds": t_serialize,
            },
            "type_pollution_removed": pollution_removed,
            "delta_path": delta_path,
            "delta_triples": delta_count,
            "output_path": INFERRED_PATH,
            "output_size_bytes": file_size,
            "has_abox": has_abox,
            "tacit_files_loaded": tacit_count,
            "classes_with_instances": len(classes),
            # 이름 있는 owl:Restriction 에 붙은 타입 — 클래스 카운트에서 제외한 수.
            # 0 이 아니면 그 T-Box 가 클래스식을 IRI 로 선언한다는 뜻이다.
            "named_restrictions_typed": named_restrictions_typed,
            "explosion_risk": explosion_risk,
            "expansion_capped": expansion_capped,
            "chain_warnings": chain_warnings,
            "noise_pruning": prune_stats,
            "inference_quality": inference_quality,
            "justification_path": justification_path if justification_summary else None,
            "justification_summary": {
                "justified": justification_summary["justified"],
                "unjustified": justification_summary["unjustified"],
                "by_rule": {k: v["count"] for k, v in justification_summary["by_rule"].items()},
            } if justification_summary else None,
        }, ensure_ascii=False, indent=2)

    except FileNotFoundError as e:
        return error_response(
            e, hint="T-Box가 필요합니다. generate_tbox로 먼저 생성하세요.", logger=logger,
        )
    except Exception as e:
        return error_response(e, logger=logger)
    finally:
        try:
            if pre_snapshot_path and os.path.exists(pre_snapshot_path):
                os.unlink(pre_snapshot_path)
        except OSError:
            pass


# ── 추론 잡(Job) 레지스트리 — 장기 실행 도구의 transport 타임아웃 회피 ──
#
# run_owl_rl_inference 는 wall-clock 6분+ 가 걸린다. MCP stdio 는 단일
# CallToolRequest 가 그만큼 응답을 안 주면 클라이언트(하네스)가 per-request
# 타임아웃으로 stdin 을 닫고, 그러면 server.py 가 EOF 로 정상 종료(exit 0)되어
# "-32000 Connection closed" + 프로세스 소멸로 관측된다 (2026-06-20 규명).
# async/to_thread 로는 못 고친다 — 응답이 여전히 6분 뒤이기 때문.
#
# 해법: 도구는 job_id 만 즉시(ms) 반환하고 실제 추론은 daemon 워커 스레드에서
# 돌린다. 클라이언트는 get_inference_status 로 폴링한다. 응답 윈도우 자체가
# 사라지므로 타임아웃이 발화하지 않는다.
# S8 추론 잡 레지스트리 (공용 JobRegistry 인스턴스). 도구-무관 로직은
# tools.common.JobRegistry 가 담고, 여기서는 inference 특화 부분만 유지:
# single-flight 키(_job_key)와 done 결과 슬림화(_slim_inference_result).
_INFERENCE_JOBS = JobRegistry(
    name="infer", poll_with="get_inference_status", logger=logger,
)


def _job_key(tbox_path: str, abox_path: str, fast_mode: bool, force: bool) -> str:
    """single-flight 키 — 동일 입력의 중복 실행을 한 잡으로 합치기 위함."""
    return f"{tbox_path}|{abox_path}|{int(fast_mode)}|{int(force)}"


def run_owl_rl_inference(
    tbox_path: str = "", abox_path: str = "", fast_mode: bool = False,
    force: bool = False,
) -> str:
    """T-Box + A-Box + 암묵지를 병합하고 OWL RL 추론을 **백그라운드로** 실행한다.

    ⚠️ 비동기 잡 패턴: 이 도구는 추론을 직접 끝까지 돌리지 않고, daemon 워커
    스레드를 띄운 뒤 **즉시 job_id 를 반환**한다(수십 ms). 실제 추론은 6분+
    걸리므로 동기 응답하면 MCP stdio 타임아웃으로 서버가 끊긴다(2026-06-20 규명).
    완료 여부·결과는 ``get_inference_status(job_id)`` 로 폴링하라 (30~60초 간격 권장).

    추론된 전체 그래프를 data/generated/inferred/all_inferred.ttl에 저장.
    data/source/tacit/*.ttl의 암묵지 TTL 파일도 자동으로 병합된다.
    subClassOf 체인, domain/range 타입 추론, inverseOf, TransitiveProperty 등을 수행.

    입력 추정 줄 수가 로컬 안전 한계 12M 을 넘으면 입력 축소 안내를 반환한다.
    원격 추론 엔진으로 자동 위임하지 않는다.

    Args:
        tbox_path: data/generated/tbox 아래 T-Box TTL 파일명.
        abox_path: data/generated/abox 아래 A-Box TTL 파일명.
        fast_mode: True면 마스터 데이터만 추론 (스키마 검증용, 빠름).
        force: True면 mtime 캐시 무시하고 무조건 재실행.

    Returns:
        {"started": bool, "job_id": str, "status": "running"|"reused",
         "poll_with": "get_inference_status", "message": str}
    """
    try:
        if tbox_path:
            tbox_path = resolve_child_path(
                GENERATED_TBOX_DIR,
                tbox_path,
                allowed_suffixes=(".ttl",),
            )
        if abox_path:
            abox_path = resolve_child_path(
                GENERATED_ABOX_DIR,
                abox_path,
                allowed_suffixes=(".ttl",),
            )
    except Exception as e:
        return error_response(e, logger=logger)

    key = _job_key(tbox_path, abox_path, fast_mode, force)
    return _INFERENCE_JOBS.dispatch(
        key=key,
        worker=lambda: _run_owl_rl_inference_sync(
            tbox_path, abox_path, fast_mode, force),
    )


def get_inference_status(job_id: str) -> str:
    """로컬 또는 GraphDB 추론 백그라운드 잡의 상태/결과를 조회한다 (즉시 반환).

    그래프를 만지지 않는 순수 dict 조회라 워커가 GIL 을 점유 중이어도 빠르게
    반환된다. status 가 done 이면 result 에 기존 run_owl_rl_inference 응답 JSON
    (engine/triples_after/output_path 등) 이 그대로 담긴다.

    Args:
        job_id: run_owl_rl_inference 또는 graphdb_run_inference 가 반환한 job_id.

    Returns:
        running: {"job_id", "status":"running", "elapsed_seconds"}
        done:    {"job_id", "status":"done", "elapsed_seconds", "result": {...}}
        failed:  {"job_id", "status":"failed", "error"}
        unknown: {"job_id", "status":"unknown", "error"}  (만료/오타)
    """
    if job_id.startswith("gdbinfer_"):
        # GraphDB 전용 poller는 공개 도구 표면에 없다. 유지 도구 하나가 두
        # 레지스트리를 job_id 접두로 분기해 백그라운드 응답 경로를 보존한다.
        from tools.remote.graphdb import _GRAPHDB_JOBS

        return _GRAPHDB_JOBS.status(job_id)

    # 도구-무관 조회 로직은 JobRegistry.status 에 위임. 로컬 추론 특화 부분은
    # done 결과의 거대 restriction_violations 를 cap 하는 slim_fn 주입뿐이다.
    return _INFERENCE_JOBS.status(job_id, slim_fn=_slim_inference_result)


def _slim_inference_result(result_obj: dict, *, list_cap: int = 50) -> None:
    """추론 result 의 비대한 리스트 필드를 in-place 로 요약한다.

    MCP stdio 는 응답을 한 줄(JSON-RPC)로 원자 전송하므로 MB 급 페이로드가
    파이프를 막아 연결을 끊는다. done 응답을 보내기 전, 인스턴스 수에 비례해
    커지는 리스트(restriction_violations 등)를 cap 개로 잘라 카운트만 보존한다.
    """
    if not isinstance(result_obj, dict):
        return
    rpp = result_obj.get("restriction_post_processing")
    if isinstance(rpp, dict):
        viols = rpp.get("restriction_violations")
        if isinstance(viols, list) and len(viols) > list_cap:
            rpp["restriction_violation_count"] = len(viols)
            rpp["restriction_violations"] = viols[:list_cap]
            rpp["restriction_violations_truncated"] = True


# ── 추론 정당화 추적 (Justification Tracing) ──────────


def _build_justifications(
    pre_triples: set[tuple],
    delta_triples: set[tuple],
    tbox: Graph,
) -> list[dict]:
    """추론된 각 delta 트리플에 대해 어떤 OWL RL 규칙이 생성했는지 역추적한다.

    owlrl DeductiveClosure는 justification을 제공하지 않으므로,
    T-Box 규칙과 pre 트리플을 기반으로 post-hoc 분류를 수행한다.

    Args:
        pre_triples: 추론 전 트리플 집합 (s, p, o) 튜플
        delta_triples: 새로 추론된 트리플 집합 (s, p, o) 튜플
        tbox: T-Box 그래프 (규칙 조회용)

    Returns:
        각 delta 트리플에 대한 justification dict 리스트:
        {"triple": (s,p,o), "rule": str, "prerequisites": [str], "confidence": str}
    """
    # ── 1. T-Box 인덱스 구축 (O(1) 조회용) ──────────
    # domain_map: {property_uri: {class_uri, ...}}
    domain_map: dict[URIRef, set[URIRef]] = {}
    for prop, _, cls in tbox.triples((None, RDFS.domain, None)):
        domain_map.setdefault(prop, set()).add(cls)

    # range_map: {property_uri: {class_uri, ...}}
    range_map: dict[URIRef, set[URIRef]] = {}
    for prop, _, cls in tbox.triples((None, RDFS.range, None)):
        range_map.setdefault(prop, set()).add(cls)

    # inverse_map: {property_uri: {inverse_property_uri, ...}} (양방향)
    inverse_map: dict[URIRef, set[URIRef]] = {}
    for p, _, q in tbox.triples((None, OWL.inverseOf, None)):
        inverse_map.setdefault(p, set()).add(q)
        inverse_map.setdefault(q, set()).add(p)

    # subclass_map: {super_class: {sub_class, ...}}
    subclass_map: dict[URIRef, set[URIRef]] = {}
    for sub, _, sup in tbox.triples((None, RDFS.subClassOf, None)):
        subclass_map.setdefault(sup, set()).add(sub)

    # transitive_props: TransitiveProperty 집합
    transitive_props: set[URIRef] = set()
    for tp in tbox.subjects(RDF.type, OWL.TransitiveProperty):
        transitive_props.add(tp)

    # symmetric_props: SymmetricProperty 집합
    symmetric_props: set[URIRef] = set()
    for prop in tbox.subjects(RDF.type, OWL.SymmetricProperty):
        symmetric_props.add(prop)

    # ── 2. pre 트리플 인덱스 (다차원 조회) ───────────
    # pre_by_subject: {subject: {(predicate, object), ...}}
    pre_by_subject: dict = {}
    # pre_by_object: {object: {(predicate, subject), ...}}
    pre_by_object: dict = {}
    # pre_by_predicate: {predicate: {(subject, object), ...}}
    pre_by_predicate: dict = {}

    for s, p, o in pre_triples:
        pre_by_subject.setdefault(s, set()).add((p, o))
        pre_by_object.setdefault(o, set()).add((p, s))
        pre_by_predicate.setdefault(p, set()).add((s, o))

    # ── 3. 각 delta 트리플 분류 ──────────────────────
    results: list[dict] = []

    for s, p, o in delta_triples:
        # 3a. already_exists: pre에 이미 있는 트리플
        # 방어적 가드: 정상적인 파이프라인에서 delta_triples는 pre_triples와
        # 겹치지 않아야 하지만, 직접 호출자가 필터링 없이 전달할 경우를 처리한다.
        if (s, p, o) in pre_triples:
            results.append({
                "triple": (s, p, o),
                "rule": "already_exists",
                "prerequisites": [],
                "confidence": "high",
            })
            continue

        # 3b~3f: 새로운 rdf:type 트리플에 대한 규칙 매칭
        if p == RDF.type:
            matched = False

            # 3b. rdfs_domain: (P, rdfs:domain, C) + (s, P, _) in pre → (s, type, C)
            for prop, domains in domain_map.items():
                if o in domains:
                    s_pairs = pre_by_subject.get(s, set())
                    for pp, _ in s_pairs:
                        if pp == prop:
                            results.append({
                                "triple": (s, p, o),
                                "rule": "rdfs_domain",
                                "prerequisites": [
                                    f"({s}, {prop}, _) in pre",
                                    f"({prop}, rdfs:domain, {o}) in tbox",
                                ],
                                "confidence": "high",
                            })
                            matched = True
                            break
                if matched:
                    break

            if matched:
                continue

            # 3c. rdfs_range: (P, rdfs:range, C) + (_, P, s) in pre → (s, type, C)
            for prop, ranges in range_map.items():
                if o in ranges:
                    s_as_obj = pre_by_object.get(s, set())
                    for pp, _ in s_as_obj:
                        if pp == prop:
                            results.append({
                                "triple": (s, p, o),
                                "rule": "rdfs_range",
                                "prerequisites": [
                                    f"(_, {prop}, {s}) in pre",
                                    f"({prop}, rdfs:range, {o}) in tbox",
                                ],
                                "confidence": "high",
                            })
                            matched = True
                            break
                if matched:
                    break

            if matched:
                continue

            # 3d. subclass_chain: (Sub, subClassOf, Super) + (s, type, Sub) in pre
            subs = subclass_map.get(o, set())
            if subs:
                s_pairs = pre_by_subject.get(s, set())
                for sub in subs:
                    if (RDF.type, sub) in s_pairs:
                        results.append({
                            "triple": (s, p, o),
                            "rule": "subclass_chain",
                            "prerequisites": [
                                f"({s}, rdf:type, {sub}) in pre",
                                f"({sub}, rdfs:subClassOf, {o}) in tbox",
                            ],
                            "confidence": "high",
                        })
                        matched = True
                        break

            if matched:
                continue

            # rdf:type이지만 매칭 안 됨 → unknown
            results.append({
                "triple": (s, p, o),
                "rule": "unknown",
                "prerequisites": [],
                "confidence": "low",
            })
            continue

        # 3e. inverse_of: (P, inverseOf, Q) + (o, Q, s) in pre → (s, P, o)
        inverses = inverse_map.get(p, set())
        if inverses:
            matched = False
            o_pairs = pre_by_subject.get(o, set())
            for inv_p in inverses:
                if (inv_p, s) in o_pairs:
                    results.append({
                        "triple": (s, p, o),
                        "rule": "inverse_of",
                        "prerequisites": [
                            f"({o}, {inv_p}, {s}) in pre",
                            f"({p}, owl:inverseOf, {inv_p}) in tbox",
                        ],
                        "confidence": "high",
                    })
                    matched = True
                    break
            if matched:
                continue

        # 3f. symmetric: P(x,y) in pre + P is SymmetricProperty → P(y,x)
        if p in symmetric_props and (p, s) in pre_by_subject.get(o, set()):
            results.append({
                "triple": (s, p, o),
                "rule": "symmetric",
                "prerequisites": [
                    f"({o}, {p}, {s}) in pre",
                    f"{p} a owl:SymmetricProperty",
                ],
                "confidence": "high",
            })
            continue

        # 3g. transitive_closure: P is TransitiveProperty + (s, P, y) + (y, P, z) in pre
        if p in transitive_props:
            p_pairs = pre_by_predicate.get(p, set())
            matched = False
            # (s, P, y) in pre를 찾고, (y, P, o) in pre인지 확인
            s_pairs = pre_by_subject.get(s, set())
            for pp, y in s_pairs:
                if pp == p and (y, o) in p_pairs:
                    results.append({
                        "triple": (s, p, o),
                        "rule": "transitive_closure",
                        "prerequisites": [
                            f"({s}, {p}, {y}) in pre",
                            f"({y}, {p}, {o}) in pre",
                        ],
                        "confidence": "high",
                    })
                    matched = True
                    break
            if matched:
                continue

        # 3h. unknown: 위 규칙에 매칭되지 않음
        results.append({
            "triple": (s, p, o),
            "rule": "unknown",
            "prerequisites": [],
            "confidence": "low",
        })

    return results


def _build_justifications_from_graphs(
    pre: Graph,
    delta: Graph,
    tbox: Graph,
) -> list[dict]:
    """SPARQL 기반 justification 재작성.

    기존 _build_justifications 는 pre/delta 를 set[tuple] 로 변환한 뒤
    Python 에서 5-규칙 매칭을 돌렸다 (수백만 delta 에서 수 초 ~ 수십 초).
    본 함수는 pre/delta 를 rdflib Graph 로 받아 Oxigraph SPARQL 엔진의
    SERVICE-less federation (공통 Graph = delta, pre 는 FILTER EXISTS/
    NOT EXISTS 대상) 으로 처리한다.

    단, rdflib 표준 SPARQL 은 여러 Graph 를 한 번에 조회하지 않으므로,
    pre + delta 를 단일 Oxigraph store 의 서로 다른 named graph 로 적재해
    그 위에서 CONSTRUCT/SELECT 를 실행한다. 반환 스키마는 기존과 동일:

        {"triple": (s,p,o), "rule": str, "prerequisites": [str],
         "confidence": "high"|"low"}

    기존 _build_justifications 는 set-interface 공개 시그니처가 테스트로
    고정돼 있어 그대로 유지하고, 대용량 파이프라인만 본 함수로 전환한다.
    """
    from pyoxigraph import NamedNode, Quad, Store

    # ── 1. Oxigraph Store 에 pre / delta 를 named graph 로 적재 ──────
    # rdflib Graph → Store.extend(Quad...) 는 triple 당 Python 튜플 생성
    # 비용이 있지만, 이후 SPARQL 실행이 Rust 로 전담하므로 순-Python 인덱스
    # 구축보다 여전히 빠르다. 또한 이 단계는 한 번만 수행된다.
    store = Store()
    pre_graph = NamedNode("urn:justif:pre")
    delta_graph = NamedNode("urn:justif:delta")
    tbox_graph = NamedNode("urn:justif:tbox")

    def _to_ox_term(term):
        # rdflib Literal/URIRef/BNode → pyoxigraph 동등 타입
        from pyoxigraph import BlankNode
        from pyoxigraph import Literal as OxLit
        from rdflib import BNode as _B
        from rdflib import Literal as _L
        from rdflib import URIRef as _U
        if isinstance(term, _U):
            return NamedNode(str(term))
        if isinstance(term, _B):
            return BlankNode(str(term))
        if isinstance(term, _L):
            if term.language:
                return OxLit(str(term), language=term.language)
            if term.datatype:
                return OxLit(str(term), datatype=NamedNode(str(term.datatype)))
            return OxLit(str(term))
        return NamedNode(str(term))

    def _bulk_load(g: Graph, target: NamedNode) -> None:
        # Oxigraph store → store 복제 시 fast path 로 RdfFormat.N_TRIPLES 를
        # serialize 한 뒤 bulk_load 하면 빠르다. Memory store / 일반 Graph
        # 는 Quad 리스트로 extend.
        inner = getattr(g.store, "_inner", None)
        if inner is not None:
            import io

            from pyoxigraph import DefaultGraph, RdfFormat
            # 원 그래프를 N-Triples 로 덤프 후 새 store 에 named graph 로 투입
            buf = io.BytesIO()
            try:
                # rdflib Oxigraph store 는 단일 named graph 에 저장됨
                src_graphs = list(inner.named_graphs())
                src = src_graphs[0] if src_graphs else DefaultGraph()
                inner.dump(buf, RdfFormat.N_TRIPLES, from_graph=src)
            except Exception:
                buf = io.BytesIO()
                inner.dump(buf, RdfFormat.N_TRIPLES, from_graph=DefaultGraph())
            buf.seek(0)
            store.bulk_load(buf, RdfFormat.N_TRIPLES, to_graph=target)
            return
        # Fallback: Memory store — rdflib 트리플을 Quad 로 변환 후 extend
        quads = [
            Quad(_to_ox_term(s), _to_ox_term(p), _to_ox_term(o), target)
            for s, p, o in g
        ]
        if quads:
            store.extend(quads)

    _bulk_load(pre, pre_graph)
    _bulk_load(delta, delta_graph)
    _bulk_load(tbox, tbox_graph)

    rdf_type = "<http://www.w3.org/1999/02/22-rdf-syntax-ns#type>"
    rdfs_subClassOf = "<http://www.w3.org/2000/01/rdf-schema#subClassOf>"
    rdfs_domain = "<http://www.w3.org/2000/01/rdf-schema#domain>"
    rdfs_range = "<http://www.w3.org/2000/01/rdf-schema#range>"
    owl_inverseOf = "<http://www.w3.org/2002/07/owl#inverseOf>"
    owl_TransitiveProperty = "<http://www.w3.org/2002/07/owl#TransitiveProperty>"
    owl_SymmetricProperty = "<http://www.w3.org/2002/07/owl#SymmetricProperty>"

    results: list[dict] = []
    classified: set[tuple] = set()

    def _ox_to_rdflib(term):
        """pyoxigraph term → rdflib term. .value 로 꺽쇠 없는 순수 값 추출."""
        cls_name = term.__class__.__name__
        if cls_name == "NamedNode":
            return URIRef(term.value)
        if cls_name == "BlankNode":
            return BNode(term.value)
        # Literal
        if getattr(term, "language", None):
            return Literal(term.value, lang=term.language)
        if getattr(term, "datatype", None):
            return Literal(term.value, datatype=URIRef(term.datatype.value))
        return Literal(term.value)

    def _ox_to_str(term) -> str:
        """prerequisites/JSON 용 문자열 표현 — 꺽쇠 제거."""
        return term.value if hasattr(term, "value") else str(term)

    def _run(query: str, rule: str, conf: str, build_prereqs) -> None:
        """query 가 ?s ?p ?o [?premise...] 를 바인딩해야 함."""
        for sol in store.query(query):
            s = sol["s"]
            p = sol["p"]
            o = sol["o"]
            key = (_ox_to_str(s), _ox_to_str(p), _ox_to_str(o))
            if key in classified:
                continue
            classified.add(key)
            results.append({
                "triple": (_ox_to_rdflib(s), _ox_to_rdflib(p), _ox_to_rdflib(o)),
                "rule": rule,
                "prerequisites": build_prereqs(sol),
                "confidence": conf,
            })

    # ── already_exists: delta ∩ pre ──────────────────────
    for sol in store.query(
        "SELECT ?s ?p ?o WHERE { "
        f"GRAPH <{delta_graph.value}> {{ ?s ?p ?o }} "
        f"GRAPH <{pre_graph.value}> {{ ?s ?p ?o }} "
        "}"
    ):
        s_t, p_t, o_t = sol["s"], sol["p"], sol["o"]
        key = (_ox_to_str(s_t), _ox_to_str(p_t), _ox_to_str(o_t))
        classified.add(key)
        results.append({
            "triple": (_ox_to_rdflib(s_t), _ox_to_rdflib(p_t), _ox_to_rdflib(o_t)),
            "rule": "already_exists",
            "prerequisites": [],
            "confidence": "high",
        })

    # ── rdfs_domain: (P, rdfs:domain, C) + (s, P, _) in pre → (s, type, C) ──
    _run(
        f"SELECT ?s ?p ?o ?prop WHERE {{ "
        f"GRAPH <{delta_graph.value}> {{ ?s ?p ?o . FILTER (?p = {rdf_type}) }} "
        f"GRAPH <{tbox_graph.value}> {{ ?prop {rdfs_domain} ?o }} "
        f"GRAPH <{pre_graph.value}> {{ ?s ?prop ?_anyv }} "
        f"}}",
        "rdfs_domain", "high",
        lambda sol: [
            f"({_ox_to_str(sol['s'])}, {_ox_to_str(sol['prop'])}, _) in pre",
            f"({_ox_to_str(sol['prop'])}, rdfs:domain, {_ox_to_str(sol['o'])}) in tbox",
        ],
    )

    # ── rdfs_range: (P, rdfs:range, C) + (_, P, s) in pre → (s, type, C) ──
    _run(
        f"SELECT ?s ?p ?o ?prop WHERE {{ "
        f"GRAPH <{delta_graph.value}> {{ ?s ?p ?o . FILTER (?p = {rdf_type}) }} "
        f"GRAPH <{tbox_graph.value}> {{ ?prop {rdfs_range} ?o }} "
        f"GRAPH <{pre_graph.value}> {{ ?_anyx ?prop ?s }} "
        f"}}",
        "rdfs_range", "high",
        lambda sol: [
            f"(_, {_ox_to_str(sol['prop'])}, {_ox_to_str(sol['s'])}) in pre",
            f"({_ox_to_str(sol['prop'])}, rdfs:range, {_ox_to_str(sol['o'])}) in tbox",
        ],
    )

    # ── subclass_chain: (Sub, subClassOf, Super) + (s, type, Sub) in pre ──
    _run(
        f"SELECT ?s ?p ?o ?sub WHERE {{ "
        f"GRAPH <{delta_graph.value}> {{ ?s ?p ?o . FILTER (?p = {rdf_type}) }} "
        f"GRAPH <{tbox_graph.value}> {{ ?sub {rdfs_subClassOf} ?o }} "
        f"GRAPH <{pre_graph.value}> {{ ?s {rdf_type} ?sub }} "
        f"}}",
        "subclass_chain", "high",
        lambda sol: [
            f"({_ox_to_str(sol['s'])}, rdf:type, {_ox_to_str(sol['sub'])}) in pre",
            f"({_ox_to_str(sol['sub'])}, rdfs:subClassOf, {_ox_to_str(sol['o'])}) in tbox",
        ],
    )

    # ── inverse_of: (P, inverseOf, Q) + (o, Q, s) in pre → (s, P, o) ──
    # inverseOf 는 양방향이므로 union 으로 양쪽 방향을 모두 커버
    _run(
        f"SELECT ?s ?p ?o ?inv WHERE {{ "
        f"GRAPH <{delta_graph.value}> {{ ?s ?p ?o }} "
        f"GRAPH <{tbox_graph.value}> {{ {{ ?p {owl_inverseOf} ?inv }} "
        f"UNION {{ ?inv {owl_inverseOf} ?p }} }} "
        f"GRAPH <{pre_graph.value}> {{ ?o ?inv ?s }} "
        f"}}",
        "inverse_of", "high",
        lambda sol: [
            f"({_ox_to_str(sol['o'])}, {_ox_to_str(sol['inv'])}, {_ox_to_str(sol['s'])}) in pre",
            f"({_ox_to_str(sol['p'])}, owl:inverseOf, {_ox_to_str(sol['inv'])}) in tbox",
        ],
    )

    # ── symmetric: P(x,y) in pre + P is SymmetricProperty → P(y,x) ──
    _run(
        f"SELECT ?s ?p ?o WHERE {{ "
        f"GRAPH <{delta_graph.value}> {{ ?s ?p ?o }} "
        f"GRAPH <{tbox_graph.value}> {{ ?p {rdf_type} {owl_SymmetricProperty} }} "
        f"GRAPH <{pre_graph.value}> {{ ?o ?p ?s }} "
        f"}}",
        "symmetric", "high",
        lambda sol: [
            f"({_ox_to_str(sol['o'])}, {_ox_to_str(sol['p'])}, {_ox_to_str(sol['s'])}) in pre",
            f"{_ox_to_str(sol['p'])} a owl:SymmetricProperty",
        ],
    )

    # ── transitive_closure: P is TransitiveProperty + (s,P,y) + (y,P,o) in pre ──
    _run(
        f"SELECT ?s ?p ?o ?y WHERE {{ "
        f"GRAPH <{delta_graph.value}> {{ ?s ?p ?o }} "
        f"GRAPH <{tbox_graph.value}> {{ ?p {rdf_type} {owl_TransitiveProperty} }} "
        f"GRAPH <{pre_graph.value}> {{ ?s ?p ?y . ?y ?p ?o }} "
        f"}}",
        "transitive_closure", "high",
        lambda sol: [
            f"({_ox_to_str(sol['s'])}, {_ox_to_str(sol['p'])}, {_ox_to_str(sol['y'])}) in pre",
            f"({_ox_to_str(sol['y'])}, {_ox_to_str(sol['p'])}, {_ox_to_str(sol['o'])}) in pre",
        ],
    )

    # ── unknown: 나머지 delta 트리플 ────────────────────
    for sol in store.query(f"SELECT ?s ?p ?o WHERE {{ GRAPH <{delta_graph.value}> {{ ?s ?p ?o }} }}"):
        s_t, p_t, o_t = sol["s"], sol["p"], sol["o"]
        key = (_ox_to_str(s_t), _ox_to_str(p_t), _ox_to_str(o_t))
        if key in classified:
            continue
        classified.add(key)
        results.append({
            "triple": (_ox_to_rdflib(s_t), _ox_to_rdflib(p_t), _ox_to_rdflib(o_t)),
            "rule": "unknown",
            "prerequisites": [],
            "confidence": "low",
        })

    return results


# ── 추론 품질 분석 ────────────────────────────────────


def _categorize_inferred_triples(pre: Graph, post: Graph) -> dict:
    """추론 전후 그래프 diff를 트리플 유형별로 분류한다."""
    from rdflib import OWL, RDFS

    categories = {
        "rdf_type": [],       # domain/range로 추론된 rdf:type
        "subclass": [],       # subClassOf 체인
        "inverse": [],        # inverseOf 추론
        "equivalent": [],     # equivalentClass/Property
        "other": [],          # 기타 (transitive, symmetric 등)
    }

    for s, p, o in post:
        if (s, p, o) in pre:
            continue
        if p == RDF.type:
            categories["rdf_type"].append((s, p, o))
        elif p == RDFS.subClassOf:
            categories["subclass"].append((s, p, o))
        elif p == OWL.equivalentClass or p == OWL.equivalentProperty:
            categories["equivalent"].append((s, p, o))
        elif p == OWL.inverseOf:
            # inverseOf 자체는 스키마 수준이지만, inverse로 추론된 인스턴스 트리플도 있음
            categories["inverse"].append((s, p, o))
        else:
            # inverseOf로 생성된 인스턴스 트리플인지 확인
            # (역방향 트리플이 존재하면 inverse 추론으로 분류)
            categories["other"].append((s, p, o))

    return categories


def _detect_type_pollution(pre: Graph, post: Graph, tbox: Graph) -> list[dict]:
    """domain/range 추론으로 인한 의도하지 않은 rdf:type 부여를 탐지한다.

    예: hasEquipment domain=ElectricalConsumption이면,
    ProcessBlastFurnace가 hasEquipment 사용 시 rdf:type ElectricalConsumption이 추론됨.
    AllDisjointClasses에 속한 클래스 간 교차 타입을 '오염'으로 판정.

    Oxigraph Rust SPARQL 엔진에 ?inst a ?polluted 스캔을 위임해 Python
    전체 트리플 iteration을 제거한다.
    """
    from rdflib import OWL

    steel_str = DOMAIN_NS
    inst_str = str(DOMAIN_INST_NS_OBJ)

    # ── 1. AllDisjointClasses 멤버 그룹 추출 (T-Box Python — 소규모) ──
    disjoint_groups: list[set[str]] = []
    for bnode in tbox.subjects(RDF.type, OWL.AllDisjointClasses):
        members_list = tbox.value(bnode, OWL.members)
        if members_list is None:
            continue
        group = set()
        from rdflib.collection import Collection
        try:
            for member in Collection(tbox, members_list):
                group.add(str(member))
        except Exception:  # noqa: BLE001 — 깨진 disjoint 목록은 해당 그룹만 건너뛴다
            pass
        if len(group) >= 2:
            disjoint_groups.append(group)

    if not disjoint_groups:
        return []

    # ── 2. 각 disjoint 그룹 멤버쌍에 대해 SPARQL로 충돌 인스턴스 조회 ──
    # pre에 존재하지 않고 post에만 있는 (?inst a ?polluted) 트리플 중
    # 같은 인스턴스가 pre에서 disjoint 멤버 중 다른 클래스를 이미 가진 경우.
    polluted: list[dict] = []
    seen: set[tuple[str, str]] = set()

    # Post-graph에서 steel 네임스페이스 인스턴스에 붙은 rdf:type을 한 번에 조회
    # (post는 pre의 superset이므로 pre에 있는 triple을 제외하려면
    # FILTER NOT EXISTS가 필요하지만, pre가 별도 그래프라 post SPARQL로
    # 직접 접근 불가 → SELECT 후 Python에서 pre 존재 확인).
    post_query = (
        "SELECT ?inst ?t WHERE { "
        "?inst a ?t . "
        f"FILTER (STRSTARTS(STR(?inst), \"{inst_str}\")) "
        f"FILTER (STRSTARTS(STR(?t), \"{steel_str}\")) "
        "}"
    )
    pre_type_cache: dict[URIRef, set[str]] = {}

    for row in post.query(post_query):
        inst, inferred_type_uri = row[0], row[1]
        if (inst, RDF.type, inferred_type_uri) in pre:
            continue
        inferred_type = str(inferred_type_uri)

        # 이 인스턴스의 pre 타입 집합을 캐싱
        if inst not in pre_type_cache:
            pre_type_cache[inst] = {
                str(t) for t in pre.objects(inst, RDF.type)
                if str(t).startswith(steel_str)
            }
        original_types = pre_type_cache[inst]

        for group in disjoint_groups:
            if inferred_type not in group:
                continue
            conflicting = original_types & group
            if not conflicting or inferred_type in original_types:
                continue
            key = (str(inst), inferred_type)
            if key in seen:
                break
            seen.add(key)
            local_polluted = _local_name(inferred_type)
            local_original = [_local_name(t) for t in sorted(conflicting)]
            polluted.append({
                "instance": _local_name(str(inst)),
                "original_types": local_original,
                "polluted_type": local_polluted,
                "reason": "domain/range 추론이 AllDisjointClasses 멤버 간 교차 타입을 생성",
                "conflicting_types": sorted(
                    _local_name(t) for t in (conflicting | {inferred_type})
                ),
                "resolution": "removed_polluted",
                "removed_type": local_polluted,
                "kept_type": local_original,
            })
            break

    return polluted


def _analyze_inference_quality_internal(
    pre: Graph, post: Graph, tbox_path: str = "",
) -> dict:
    """추론 품질 분석의 내부 로직. 이미 로드된 그래프를 받아 분석한다.

    Args:
        pre: 추론 전 그래프 (T-Box + A-Box + 암묵지)
        post: 추론 후 그래프 (all_inferred.ttl)
        tbox_path: T-Box 경로 (타입 오염 탐지용)

    Returns:
        분석 결과 dict
    """
    pre_count = len(pre)
    post_count = len(post)
    inferred_count = post_count - pre_count
    ratio = round(inferred_count / pre_count, 2) if pre_count > 0 else 0

    categories = _categorize_inferred_triples(pre, post)
    distribution = {k: len(v) for k, v in categories.items()}

    explosion_warning = ratio > 10
    explosion_level = (
        "CRITICAL" if ratio > 50
        else "HIGH" if ratio > 20
        else "WARNING" if ratio > 10
        else "NORMAL"
    )

    tbox = _new_graph()
    tbox.parse(tbox_path or TBOX_PATH, format="turtle")
    type_pollution = _detect_type_pollution(pre, post, tbox)

    steel_str = DOMAIN_NS
    type_freq: dict[str, int] = {}
    for _s, _p, o in categories["rdf_type"]:
        cls_name = _local_name(str(o)) if str(o).startswith(steel_str) else str(o)
        type_freq[cls_name] = type_freq.get(cls_name, 0) + 1
    top_inferred_types = sorted(type_freq.items(), key=lambda x: -x[1])[:20]

    return {
        "triples_before": pre_count,
        "triples_after": post_count,
        "inferred_count": inferred_count,
        "inferred_ratio": ratio,
        "explosion": {
            "detected": explosion_warning,
            "level": explosion_level,
            "hint": "domain/range가 과도하게 넓거나, TransitiveProperty 체인이 깊을 수 있습니다." if explosion_warning else None,
        },
        "distribution": distribution,
        "distribution_pct": {
            k: round(v / inferred_count * 100, 1) if inferred_count > 0 else 0
            for k, v in distribution.items()
        },
        "type_pollution": {
            "count": len(type_pollution),
            "severity": "CRITICAL" if len(type_pollution) > 10 else "WARNING" if type_pollution else "PASS",
            "samples": type_pollution[:20],
        },
        "top_inferred_types": [{"class": c, "count": n} for c, n in top_inferred_types],
    }


def analyze_inference_quality() -> str:
    """추론 결과의 내용적 품질을 분석한다.

    추론 전(T-Box + A-Box + 암묵지) 그래프와 추론 후(all_inferred.ttl)를 비교하여:
    1. 추론 폭발 탐지 — inferred/original 비율이 10배 이상이면 경고
    2. 예상외 타입 추론 — domain/range로 인한 AllDisjointClasses 충돌 (타입 오염)
    3. 추론 트리플 유형 분포 — rdf:type / subClassOf / inverse / equivalent / other
    4. 추론된 rdf:type 중 가장 빈번한 클래스 통계
    5. inferred_delta.ttl 요약 (파일 존재 시) — 추론 유일 트리플의 유형 분포

    run_owl_rl_inference 실행 후에 호출한다.
    """
    try:
        pre, _tacit_count = _load_and_merge()

        if not os.path.exists(INFERRED_PATH):
            return error_response(
                "추론 결과 파일이 없습니다.",
                hint="run_owl_rl_inference를 먼저 실행하세요.",
                logger=logger,
            )
        post = _new_graph()
        from domain.tbox_utils import fast_parse_turtle
        fast_parse_turtle(post, INFERRED_PATH)

        result = _analyze_inference_quality_internal(pre, post)
        result["success"] = True

        # #4: inferred_delta.ttl이 있으면 요약을 결과에 포함
        delta_path = INFERRED_PATH.replace("all_inferred.ttl", "inferred_delta.ttl")
        if os.path.exists(delta_path):
            try:
                result["delta_summary"] = _summarize_inferred_delta(delta_path)
            except Exception as _de:
                logger.warning("inferred_delta 요약 실패: %s", _de)

        return json.dumps(result, ensure_ascii=False, indent=2)

    except FileNotFoundError as e:
        return error_response(e, hint="T-Box/A-Box 파일이 필요합니다.", logger=logger)
    except Exception as e:
        return error_response(e, logger=logger)


def _summarize_inferred_delta(delta_path: str) -> dict:
    """inferred_delta.ttl의 트리플 유형 분포를 요약한다."""
    from rdflib import OWL as _O
    from rdflib import RDF as _R
    from rdflib import RDFS as _RS

    from domain.tbox_utils import fast_parse_turtle
    dg = _new_graph()
    fast_parse_turtle(dg, delta_path)
    total = len(dg)
    by_type: dict[str, int] = {}
    for _s, p, _o in dg:
        if p == _R.type:
            key = "rdf_type"
        elif p == _RS.subClassOf:
            key = "subclass"
        elif p == _O.inverseOf:
            key = "inverse_of"
        elif p == _O.equivalentClass:
            key = "equivalent_class"
        elif p == _O.equivalentProperty:
            key = "equivalent_property"
        else:
            key = "other"
        by_type[key] = by_type.get(key, 0) + 1
    return {
        "path": delta_path,
        "total_inferred_triples": total,
        "by_predicate_category": by_type,
    }


def read_inferred_delta(limit: int = 100) -> str:
    """inferred_delta.ttl에서 추론 전용 트리플을 읽어 요약 + 샘플 반환.

    run_owl_rl_inference가 생성하는 delta 파일(명시 트리플 제외, 순수 추론 결과)을
    조회한다. Semantic dictionary 구축이나 KG 감사 시 "어떤 지식이 LLM/CSV가 아닌
    OWL RL로 유래했는가?"를 역추적하는 데 사용.

    Args:
        limit: 반환 샘플 트리플 수 (기본 100).
    """
    try:
        delta_path = INFERRED_PATH.replace("all_inferred.ttl", "inferred_delta.ttl")
        if not os.path.exists(delta_path):
            return error_response(
                f"inferred_delta 파일이 없습니다: {delta_path}",
                hint="run_owl_rl_inference를 먼저 실행하세요.",
                logger=logger,
            )
        from domain.tbox_utils import fast_parse_turtle
        dg = _new_graph()
        fast_parse_turtle(dg, delta_path)
        summary = _summarize_inferred_delta(delta_path)
        samples = []
        for i, (s, p, o) in enumerate(dg):
            if i >= max(0, int(limit)):
                break
            samples.append({"s": str(s), "p": str(p), "o": str(o)})
        from tools.common import success_response
        return success_response({
            **summary,
            "samples": samples,
            "sample_limit": int(limit),
        })
    except Exception as e:
        return error_response(e, logger=logger)
