"""Step 22e — 중복 ObjectProperty 제거 (정본 하나만 남긴다).

Step 22 (`_consolidate_duplicate_ops`) 는 같은 ``(domain, range)`` 중복 OP 를
``rdfs:subPropertyOf`` 로 묶기만 하고 **삭제하지 않는다** — 기존 이름으로도 질의가
되게 하려는 의도였다. 그러나 실측 결과 그 대가가 컸다 (2026-07-26):

  - 중복 그룹 **35개** / 초과 OP **66개** 가 T-Box 에 남는다.
  - CSV FK 컬럼은 방향당 하나뿐이라 A-Box 는 그중 **하나만** 채운다. 나머지는 값
    0건이고, 빈 쪽으로 질의하면 **0건이 정답처럼** 반환된다 (에러 없음).
  - 어느 이름이 채워지는지가 T-Box 직렬화 순서에 좌우돼, 무관한 변경에도 뒤집혔다
    (``isSlabOf`` 13,222건 → 0건). 저장해 둔 SPARQL 이 한꺼번에 깨진다.
  - subPropertyOf 전파는 **추론을 돌린 뒤에만** 효과가 있어, A-Box 원본만 보는
    도구·사람에게는 여전히 빈 관계로 보인다.

**정본 선정이 SME 판단을 요구하지 않는 근거** (실측): 36개 중복 그룹 전부 그 방향을
만들 수 있는 CSV FK 컬럼이 **최대 1개**다. 컬럼이 하나면 표현 가능한 사실도 하나이므로
그룹 내 OP 는 정의상 동의어다. 데이터가 실제로 두 갈래인 경우
(``origin`` ↔ ``destination``) 는 Step 22d 의 ``_has_opposing_directions`` 가 걸러
이 단계에 오지 않는다.

정본 선정 규칙 (높은 점수 우선, 결정적):

===========================================  =====  ===========================
기준                                          점수   근거
===========================================  =====  ===========================
range 클래스명이 이름에 포함                    +10   무엇과 연결되는지 드러남
domain 클래스명이 이름에 포함                    +6   방향이 드러남
domain 이 range 보다 이름에서 앞               +5   방향이 올바른 순서
서술어가 구체적 (produce/consume/…)             +3   "has~/is~Of" 보다 의미 명확
rdfs:comment 보유                              +2   문서화된 것 선호
외부 설정·CQ 가 참조                            +2   기존 참조 보존 (품질보다 약하게)
range 클래스명 없음                            -10   대상을 알 수 없는 범용 이름
이름 단어 2개 이하 (isProducedBy 류)            -4   모호
===========================================  =====  ===========================

동점은 ``(A-Box 사용량, 짧은 이름, 알파벳)`` 으로 깬다 — 전부 결정적이므로 재실행
시 같은 정본이 나온다.

제거는 **선언 트리플 전체**를 지운다 (rdf:type / label / comment / domain / range /
subPropertyOf / inverseOf / 이 OP 를 참조하는 다른 트리플). Restriction 의
``owl:onProperty`` 가 제거 대상을 가리키면 정본으로 **치환**한다 — 그대로 두면
dangling reference 가 되어 추론기가 실패한다.

환경변수:
  - ``TBOX_DUP_OP_PRUNE``: ``off`` (default) | ``on``
    기본이 off 인 이유: 이 단계는 T-Box 어휘를 실제로 삭제하므로, 도메인 담당자가
    선정 결과를 확인한 뒤 켜야 한다. 켜기 전 Step 22d 로그로 규모를 확인하라.
  - ``TBOX_DUP_OP_PRUNE_KEEP``: ``이름1,이름2`` — 규칙 결과를 무시하고 강제로 정본
    지정 (SME 가 정식 이름을 알려준 경우). 그룹당 하나만 유효.
"""
from __future__ import annotations

import collections
import glob
import logging
import os
import re

from rdflib import OWL, RDF, RDFS, Graph, URIRef

from domain.rules_paths import rules_json_glob
from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)

_CONCRETE_PREDICATES = ("produce", "consume", "process", "apply", "specif", "tolerance")


def _local(uri: str) -> str:
    return uri.split("#")[-1].split("/")[-1]


def _name_words(name: str) -> list[str]:
    """camelCase 이름을 소문자 단어 리스트로 분해."""
    return [w.lower() for w in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])", name)]


def _external_references(names: set[str]) -> set[str]:
    """rules/ 및 query_tests/ 가 참조하는 OP 이름 — 제거 시 영향이 있는 것."""
    found: set[str] = set()
    if not names:
        return found
    base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    # rules JSON 은 카테고리 하위 폴더에 있다 — ``rules/*.json`` 은 0건이 되고,
    # 0건은 "어떤 설정도 이 OP 를 안 쓴다" 로 읽혀 SME 선언 OP 를 지운다.
    candidates = list(rules_json_glob())
    candidates += glob.glob(
        os.path.join(base, "data", "source", "query_tests", "**", "*"), recursive=True,
    )
    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
        except OSError:
            continue
        for name in names:
            if name not in found and re.search(rf"\b{re.escape(name)}\b", text):
                found.add(name)
    return found


def _abox_usage() -> dict[str, int]:
    from tools.ontology_quality import _load_abox_op_usage
    try:
        return _load_abox_op_usage() or {}
    except Exception as exc:  # noqa: BLE001
        logger.debug("A-Box OP usage 로드 실패 (0 으로 간주): %s", exc)
        return {}


def _forced_keep() -> set[str]:
    raw = os.getenv("TBOX_DUP_OP_PRUNE_KEEP") or ""
    return {p.strip() for p in raw.split(",") if p.strip()}


def _score(
    name: str, src: str, tgt: str, *, has_comment: bool,
    externally_referenced: bool,
) -> int:
    """정본 적합도 — 모듈 docstring 의 표와 일치."""
    joined = "".join(_name_words(name))
    src_l, tgt_l = src.lower(), tgt.lower()
    has_src, has_tgt = src_l in joined, tgt_l in joined
    score = 0
    if has_tgt:
        score += 10
    else:
        score -= 10
    if has_src:
        score += 6
    if has_src and has_tgt and joined.index(src_l) < joined.index(tgt_l):
        score += 5
    if any(k in joined for k in _CONCRETE_PREDICATES):
        score += 3
    if has_comment:
        score += 2
    if externally_referenced:
        score += 2
    if len(_name_words(name)) <= 2:
        score -= 4
    return score


def _duplicate_groups(g: Graph, domain_ns: str) -> dict[tuple[str, str], list[URIRef]]:
    """Step 22d 와 같은 기준으로 중복 그룹 수집 (방향 대립 그룹은 제외)."""
    from tools.ontology_quality import (
        _directional_tokens_in_labels,
        _has_opposing_directions,
    )

    buckets: dict[tuple[str, str], list[URIRef]] = collections.defaultdict(list)
    for op in g.subjects(RDF.type, OWL.ObjectProperty):
        if not (isinstance(op, URIRef) and str(op).startswith(domain_ns)):
            continue
        domains = sorted(
            _local(str(d)) for d in g.objects(op, RDFS.domain)
            if isinstance(d, URIRef) and str(d).startswith(domain_ns)
        )
        ranges = sorted(
            _local(str(r)) for r in g.objects(op, RDFS.range)
            if isinstance(r, URIRef) and str(r).startswith(domain_ns)
        )
        if len(domains) != 1 or len(ranges) != 1:
            continue
        buckets[(domains[0], ranges[0])].append(op)

    result: dict[tuple[str, str], list[URIRef]] = {}
    for key, ops in buckets.items():
        if len(ops) < 2:
            continue
        if _has_opposing_directions([_directional_tokens_in_labels(g, o) for o in ops]):
            continue          # origin ↔ destination 류는 의미가 실제로 갈린다
        result[key] = ops
    return result


def _plan(g: Graph, domain_ns: str) -> list[dict]:
    """그룹별 {keep, remove} 계획. 파일을 수정하지 않는다."""
    groups = _duplicate_groups(g, domain_ns)
    all_names = {_local(str(o)) for ops in groups.values() for o in ops}
    external = _external_references(all_names)
    usage = _abox_usage()
    forced = _forced_keep()

    plan: list[dict] = []
    for (src, tgt), ops in sorted(groups.items()):
        def _key(op: URIRef, *, src: str = src, tgt: str = tgt) -> tuple:
            # 루프 변수를 **기본값으로 바인딩** 한다. 클로저가 늦게 바인딩되면
            # 모든 그룹이 마지막 (src, tgt) 로 채점되는 조용한 오작동이 된다
            # (여기서는 sorted 가 즉시 호출돼 드러나지 않지만, 호출이 지연되는
            # 리팩터링 한 번으로 깨진다 — ruff B023).
            name = _local(str(op))
            # **실제 데이터 보유가 이름 점수를 이긴다.** usage 를 _score 뒤에 두면
            # 이름 규칙이 실측을 압도해 값 있는 프로퍼티가 삭제된다 — 실측
            # (2026-08-11): hasSoilImpact(점수 -6, A-Box+tacit 4,228건) 가
            # hasSoilMonitoringReport(점수 +14, 0건) 에게 져서 remove 대상이 됐다.
            # 그 결과가 이 스텝이 막으려던 "빈 관계로 질의하면 0건" 이다.
            #
            # 이름 점수는 **둘 다 0건일 때** 만 판정 근거로 쓴다 (그때는 어느 쪽도
            # 데이터 근거가 없으므로 명명 규칙이 최선의 신호다).
            return (
                1 if name in forced else 0,
                usage.get(name, 0),
                _score(
                    name, src, tgt,
                    has_comment=bool(list(g.objects(op, RDFS.comment))),
                    externally_referenced=name in external,
                ),
                -len(name),
                name,
            )

        ranked = sorted(ops, key=_key, reverse=True)
        keep, remove = ranked[0], ranked[1:]
        plan.append({
            "domain": src,
            "range": tgt,
            "keep": _local(str(keep)),
            "keep_uri": keep,
            "remove": [_local(str(o)) for o in remove],
            "remove_uris": remove,
            "abox_values_relocated": sum(usage.get(_local(str(o)), 0) for o in remove),
        })
    return plan


def _inverse_direction_matches(g: Graph, partner: URIRef, keep: URIRef) -> bool:
    """``partner`` 가 ``keep`` 의 역관계로 성립하는가 — domain/range 가 뒤바뀐 쌍인가.

    이름이 아니라 **선언된 방향** 으로 판정한다. 라벨 토큰은 domain==range 인 관계에서
    무력하고, 방향이 안 맞는 것을 ``inverseOf`` 로 이으면 추론기가 OWL RL prp-inv1 로
    **틀린 방향의 트리플을 파생** 한다 (이 리포에서 반대 방향 공리 4개가 conformance ·
    HermiT · quality_rules 를 모두 통과한 이력이 있다).

    한쪽이라도 domain/range 를 선언하지 않았으면 ``False`` — 판정 불가일 때 잇지 않는다.
    """
    p_dom = set(g.objects(partner, RDFS.domain))
    p_rng = set(g.objects(partner, RDFS.range))
    k_dom = set(g.objects(keep, RDFS.domain))
    k_rng = set(g.objects(keep, RDFS.range))
    if not (p_dom and p_rng and k_dom and k_rng):
        return False
    return p_dom == k_rng and p_rng == k_dom


def _prune(g: Graph, entry: dict) -> tuple[int, int]:
    """한 그룹의 remove 대상을 그래프에서 완전히 제거.

    Returns:
        ``(삭제 트리플 수, 정본으로 재연결한 inverseOf 수)``.
    """
    keep = entry["keep_uri"]
    removed = 0
    rewired = 0
    for victim in entry["remove_uris"]:
        # ① Restriction 등이 이 OP 를 onProperty 로 참조하면 정본으로 치환.
        #    삭제만 하면 dangling reference 가 남아 추론기가 실패한다.
        #
        # ② ``owl:inverseOf`` 도 같은 이유로 **치환** 한다 (2026-08-26 실측). 예전에는
        #    아래 무조건 삭제 분기로 떨어져 짝 쪽 선언만 남고 링크가 끊겼다. 그러면
        #    역방향 OP 8개가 고아가 되고 — 선언은 있으나 inverseOf 가 없어
        #    ``load_graph()`` 의 ``ensure_inverse_triples()`` 가 채우지 못한다 —
        #    S9 프로퍼티 커버리지가 96.4% → 93.8% 로 떨어졌다. 짝이 데이터를 잃는
        #    것은 중복 정리의 의도가 아니다 (지우려는 것은 **이름** 이지 관계가 아니다).
        #
        #    방향이 호환될 때만 잇는다 — 안 맞는 것을 이으면 prp-inv1 이 틀린 방향
        #    트리플을 파생한다.
        for subj, pred in list(g.subject_predicates(victim)):
            if pred == OWL.onProperty:
                g.remove((subj, pred, victim))
                g.add((subj, pred, keep))
                continue
            if pred == OWL.inverseOf and subj != keep:
                g.remove((subj, pred, victim))
                if _inverse_direction_matches(g, subj, keep):
                    g.add((subj, OWL.inverseOf, keep))
                    rewired += 1
                else:
                    removed += 1
                    logger.info(
                        "Step 22e: %s owl:inverseOf %s 를 정본 %s 로 잇지 않았다 — "
                        "domain/range 방향이 호환되지 않는다 (틀린 방향 파생 방지)",
                        _local(str(subj)), _local(str(victim)), _local(str(keep)),
                    )
                continue
            g.remove((subj, pred, victim))
            removed += 1
        # ③ 이 OP 가 주어인 트리플 전부 (type/label/comment/domain/range/…).
        #    victim 이 주어인 inverseOf 도 여기서 지워지는데, 그 대상(짝)은 위 ②의
        #    반대 방향으로 이미 처리됐거나 정본 자신이므로 추가 재연결이 필요 없다.
        for pred, obj in list(g.predicate_objects(victim)):
            if pred == OWL.inverseOf and obj != keep:
                partner = obj
                g.remove((victim, pred, obj))
                removed += 1
                # 짝 → 정본 링크가 아직 없고 방향이 맞으면 잇는다 (한쪽만 선언된 경우).
                if (
                    isinstance(partner, URIRef)
                    and (partner, OWL.inverseOf, keep) not in g
                    and (keep, OWL.inverseOf, partner) not in g
                    and _inverse_direction_matches(g, partner, keep)
                ):
                    g.add((partner, OWL.inverseOf, keep))
                    rewired += 1
                continue
            g.remove((victim, pred, obj))
            removed += 1
    return removed, rewired


def apply(g: Graph, ctx: StepContext) -> StepResult:
    """Prune duplicate OPs down to one canonical per (domain, range)."""
    before = len(g)
    mode = (os.getenv("TBOX_DUP_OP_PRUNE") or "off").strip().lower()

    stats: dict = {"prune_mode": mode}
    try:
        plan = _plan(g, ctx.domain_ns)
    except Exception as exc:  # noqa: BLE001 — 계획 실패가 S3 를 막아선 안 된다
        logger.warning("Step 22e 중복 OP 계획 실패 (건너뜀): %s", exc)
        return StepResult(
            name="step_22e_duplicate_op_prune",
            stats={"error": str(exc), "prune_mode": mode},
            step_number=22,
            step_label="duplicate_op_prune",
        )

    stats["groups"] = len(plan)
    stats["prune_candidates"] = sum(len(e["remove"]) for e in plan)
    stats["plan"] = [
        {k: v for k, v in e.items() if not k.endswith("_uri") and not k.endswith("_uris")}
        for e in plan[:20]
    ]
    stats["abox_values_relocated"] = sum(e["abox_values_relocated"] for e in plan)

    if mode != "on":
        if plan:
            logger.info(
                "Step 22e 중복 OP %d개 (그룹 %d) 제거 후보 — TBOX_DUP_OP_PRUNE=on "
                "으로 켜면 삭제한다. 정본 예: %s",
                stats["prune_candidates"], len(plan),
                [f"{e['domain']}→{e['range']}: {e['keep']}" for e in plan[:3]],
            )
        return StepResult(
            name="step_22e_duplicate_op_prune",
            stats=stats,
            triples_delta=0,
            step_number=22,
            step_label="duplicate_op_prune",
        )

    pruned_ops = 0
    pruned_triples = 0
    rewired_inverses = 0
    for entry in plan:
        n_removed, n_rewired = _prune(g, entry)
        pruned_triples += n_removed
        rewired_inverses += n_rewired
        pruned_ops += len(entry["remove_uris"])
    stats["pruned_ops"] = pruned_ops
    stats["pruned_triples"] = pruned_triples
    # 제거 대상을 가리켰던 짝의 owl:inverseOf 를 정본으로 다시 이은 수. 0 이면 역방향
    # OP 가 고아가 됐을 수 있다 — S9 프로퍼티 커버리지로 확인하라.
    stats["inverse_rewired_to_keep"] = rewired_inverses
    logger.info(
        "Step 22e 중복 OP 제거: OP %d개 / 트리플 %d개 (그룹 %d). "
        "owl:inverseOf %d건을 정본으로 재연결했다. "
        "A-Box 값 %d건이 정본 이름으로 이동한다 — A-Box 재생성 필요.",
        pruned_ops, pruned_triples, len(plan), rewired_inverses,
        stats["abox_values_relocated"],
    )

    return StepResult(
        name="step_22e_duplicate_op_prune",
        stats=stats,
        triples_delta=len(g) - before,
        step_number=22,
        step_label="duplicate_op_prune",
    )
