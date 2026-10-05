"""T-Box 기반 tacit rule op direction 검증기.

2026-05-10 회귀 분석: `rules/domain/tacit_rules.json` 의 rule 이 source/target 과
반대 방향 op 를 지정하면 `generate_tacit_from_rules` 가 그대로 TTL 로 출력
→ T-Box domain/range 와 정반대 방향 triple 이 A-Box 에 삽입되어
validate_kg 의 domain/range 정합성 / OP 양방향 연결 check 가 실패.

본 모듈은 rule 파서가 호출할 validator:
  1. op 가 선언된 T-Box 에서 domain/range 조회.
  2. source_class ⊆ domain(op) AND target_class ⊆ range(op) → valid.
  3. 반대 방향 (source ⊆ range, target ⊆ domain) → inverse op 로 자동 교정.
  4. 교정 불가 (inverse 없거나 inverse 도 불일치) → invalid 마커 반환.

Empty T-Box 입력은 backward-compat 을 위해 통과 (모든 rule 은 `valid=True`).
"""
from __future__ import annotations

import logging

from rdflib import Graph, URIRef
from rdflib.namespace import OWL, RDFS

# owl:Thing is the universal superclass — any class is implicitly its subclass.
# A domain/range of {owl:Thing} therefore imposes no real constraint and must
# be treated the same as an unspecified (empty) domain/range.
_OWL_THING = OWL.Thing

logger = logging.getLogger(__name__)

# 도메인 네임스페이스 prefix (철강 샘플 기본값; 다른 도메인은 tbox parse 시
# 자동으로 해당 prefix 의 URI 로 풀림).
_DEFAULT_DOMAIN_PREFIX = "http://example.com/steel-ontology#"


def _ancestors(tbox: Graph, cls_uri: URIRef) -> set[URIRef]:
    """transitive subClassOf 상위 클래스 집합 (자신 포함)."""
    seen = {cls_uri}
    stack = [cls_uri]
    while stack:
        cur = stack.pop()
        for parent in tbox.objects(cur, RDFS.subClassOf):
            if isinstance(parent, URIRef) and parent not in seen:
                seen.add(parent)
                stack.append(parent)
    return seen


def _op_uri(op_local: str, domain_prefix: str = _DEFAULT_DOMAIN_PREFIX) -> URIRef:
    return URIRef(domain_prefix + op_local)


def _cls_uri(cls_local: str, domain_prefix: str = _DEFAULT_DOMAIN_PREFIX) -> URIRef:
    return URIRef(domain_prefix + cls_local)


def _op_exists(tbox: Graph, op_local: str, domain_prefix: str) -> bool:
    uri = _op_uri(op_local, domain_prefix)
    return (uri, None, None) in tbox


def _domain_range(
    tbox: Graph, op_local: str, domain_prefix: str,
) -> tuple[set[URIRef], set[URIRef]]:
    uri = _op_uri(op_local, domain_prefix)
    doms = {x for x in tbox.objects(uri, RDFS.domain) if isinstance(x, URIRef)}
    rngs = {x for x in tbox.objects(uri, RDFS.range) if isinstance(x, URIRef)}
    return doms, rngs


def _is_compatible(cls_uri: URIRef, dr_set: set[URIRef], tbox: Graph) -> bool:
    """cls 가 dr_set (domain 또는 range) 안의 임의 클래스의 하위면 True.

    dr_set 이 비어있으면 제약 없음 → True (관대한 동작).
    dr_set 이 owl:Thing 을 포함하면 모든 클래스가 그 하위이므로 → True
    (owl:Thing 은 universal superclass — 미지정 domain/range 와 동일 취급).
    """
    if not dr_set:
        return True
    if _OWL_THING in dr_set:
        return True
    ancestors = _ancestors(tbox, cls_uri)
    return any(dr in ancestors for dr in dr_set)


def _find_inverse(tbox: Graph, op_local: str, domain_prefix: str) -> str | None:
    """T-Box 에서 op 의 inverse 선언을 찾는다. 없으면 None."""
    uri = _op_uri(op_local, domain_prefix)
    for inv in tbox.objects(uri, OWL.inverseOf):
        if isinstance(inv, URIRef) and str(inv).startswith(domain_prefix):
            return str(inv)[len(domain_prefix):]
    # 반대 방향 선언도 확인
    for s in tbox.subjects(OWL.inverseOf, uri):
        if isinstance(s, URIRef) and str(s).startswith(domain_prefix):
            return str(s)[len(domain_prefix):]
    return None


def validate_op_direction(
    op: str,
    source_class: str,
    target_class: str,
    tbox_ttl: str,
    domain_prefix: str = _DEFAULT_DOMAIN_PREFIX,
) -> dict:
    """Rule 의 (source_class, op, target_class) 방향이 T-Box 와 맞는지 검증.

    Args:
        op: rule 이 지정한 ObjectProperty local name.
        source_class: rule 의 source (TTL 출력의 subject 클래스).
        target_class: rule 의 target (TTL 출력의 object 클래스).
        tbox_ttl: T-Box turtle 문자열. 빈 문자열이면 검증 skip (backward compat).
        domain_prefix: 도메인 네임스페이스 prefix.

    Returns:
        {
          "valid": bool,
          "op": 교정된 op local name (valid 일 때), or None (invalid),
          "corrected": True 면 inverse 로 자동 교정된 것,
          "original_op": 교정 전 op (corrected=True 일 때만 유의미),
          "reason": 사람 읽기 쉬운 설명,
        }
    """
    # Empty T-Box → 검증 skip (backward compat).
    if not tbox_ttl or not tbox_ttl.strip():
        return {
            "valid": True,
            "op": op,
            "corrected": False,
            "original_op": op,
            "reason": "T-Box not provided; validation skipped",
        }

    tbox = Graph()
    try:
        tbox.parse(data=tbox_ttl, format="turtle")
    except Exception as e:
        logger.warning("tacit op validator: T-Box parse failed: %s", e)
        return {
            "valid": True,
            "op": op,
            "corrected": False,
            "original_op": op,
            "reason": f"T-Box parse failed ({e}); validation skipped",
        }

    if not _op_exists(tbox, op, domain_prefix):
        # T-Box 가 provided 지만 op 가 없음 → 검증 불가로 간주하고 통과 (backward compat).
        # Rule 이 T-Box 선언을 기대하는 고엄격 모드가 필요하면 호출자가 별도 체크.
        logger.info(
            "tacit op validator: op '%s' not declared in T-Box — passing through",
            op,
        )
        return {
            "valid": True,
            "op": op,
            "corrected": False,
            "original_op": op,
            "reason": f"OP '{op}' not declared in T-Box; validation skipped",
        }

    src_uri = _cls_uri(source_class, domain_prefix)
    tgt_uri = _cls_uri(target_class, domain_prefix)

    # source 또는 target class 가 T-Box 에 선언 안 돼 있으면 검증 skip
    # — class mismatch 는 별도 check 로 감지. validator 는 OP 방향만 담당.
    src_declared = (src_uri, None, None) in tbox
    tgt_declared = (tgt_uri, None, None) in tbox
    if not src_declared or not tgt_declared:
        logger.info(
            "tacit op validator: class not declared in T-Box "
            "(src_declared=%s, tgt_declared=%s) — passing through",
            src_declared, tgt_declared,
        )
        return {
            "valid": True,
            "op": op,
            "corrected": False,
            "original_op": op,
            "reason": (
                "source/target class not fully declared in T-Box; "
                "direction validation skipped"
            ),
        }

    doms, rngs = _domain_range(tbox, op, domain_prefix)

    # 1. 정방향 체크: source ⊆ domain(op), target ⊆ range(op)
    if _is_compatible(src_uri, doms, tbox) and _is_compatible(tgt_uri, rngs, tbox):
        return {
            "valid": True,
            "op": op,
            "corrected": False,
            "original_op": op,
            "reason": "valid direction",
        }

    # 2. 반대 방향 체크: source ⊆ range(op), target ⊆ domain(op)
    # 즉 rule 이 op 를 반대로 썼으니 inverse 로 교정 가능한지 확인.
    reversed_match = (
        _is_compatible(src_uri, rngs, tbox)
        and _is_compatible(tgt_uri, doms, tbox)
    )
    if reversed_match:
        inv = _find_inverse(tbox, op, domain_prefix)
        if inv:
            # inverse 의 domain/range 가 정방향 정합성 만족하는지 재확인
            inv_doms, inv_rngs = _domain_range(tbox, inv, domain_prefix)
            if (
                _is_compatible(src_uri, inv_doms, tbox)
                and _is_compatible(tgt_uri, inv_rngs, tbox)
            ):
                return {
                    "valid": True,
                    "op": inv,
                    "corrected": True,
                    "original_op": op,
                    "reason": (
                        f"reversed direction detected; auto-corrected to inverse '{inv}'"
                    ),
                }
        # inverse 없거나 inverse 도 맞지 않음 — invalid
        return {
            "valid": False,
            "op": None,
            "corrected": False,
            "original_op": op,
            "reason": (
                f"OP direction reversed (no inverse available) — "
                f"source={source_class} ⊆ range, target={target_class} ⊆ domain"
            ),
        }

    # 3. 어느 방향과도 호환 안 됨
    return {
        "valid": False,
        "op": None,
        "corrected": False,
        "original_op": op,
        "reason": (
            f"OP '{op}' domain/range incompatible with "
            f"source={source_class} / target={target_class}"
        ),
    }
