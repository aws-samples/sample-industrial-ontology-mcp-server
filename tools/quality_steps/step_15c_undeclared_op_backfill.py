"""Step 15c — A-Box·tacit 이 쓰는 **미선언 OP** 를 T-Box 에 선언.

## 왜 필요한가

A-Box 생성기는 FK 컬럼 이름에서 ObjectProperty 이름을 **동적으로 만들고 T-Box 에
선언돼 있는지 검사하지 않는다**. tacit TTL 도 `tacit_rules.json` 이 정한 이름을
그대로 쓴다. 그 결과 데이터에는 관계가 있는데 T-Box 는 그 이름을 모른다.

실측 (2026-08-12, 병합 A-Box 725,730 트리플을 rdflib 로 파싱):
object 가 IRI 인 도메인 술어 51종 중 **37종이 T-Box 미선언** 이고 그것들이
**52,471 트리플** 을 쓴다. 최다는 ``hasBlastFurnaceProduct`` 8,640 /
``equipmentStatusOfEquipment`` 6,000 / ``gasEnergyAtMonitoringPoint`` 5,304.

같은 클래스쌍에 **선언된** OP 는 값이 0건인 경우가 많다 — 즉 이름만 어긋났다:

  EquipmentMaster→EquipmentStatus: 선언 ``hasEquipmentStatus`` 0행 /
                                   미선언 ``equipmentStatusOfEquipment`` 6,000행

## 무엇이 조용히 잘못되는가

1. **질의가 0행을 정답처럼 반환한다.** 딕셔너리·CQ·골든쿼리는 선언된 이름만 아니까
   그것으로 질의하고, 데이터는 다른 이름에 있다. "KG 에 그 관계가 없다" 는 잘못된
   결론으로 이어진다.
2. **domain/range 제약이 없다.** 미선언 술어는 어떤 데이터든 위반 없이 통과한다 —
   ``check_domain_range_conformance`` 는 T-Box 에 선언된 OP 만 검사한다.
3. ``check_undeclared_op`` (validate_kg 22번째 check) 가 이미 37건을 잡지만 **S9 에서
   사후 보고만** 하고 T-Box 에 반영되지 않았다. 이 스텝이 그 신호를 소비한다.

## 왜 rename 이 아니라 선언 주입인가

미선언 이름을 선언된 유령 이름으로 **바꾸는**(rename) 안은 위험하다: 유령 OP 중
다수가 ``owl:Restriction`` 의 ``onProperty`` 대상이라 이름을 바꾸면 그 Restriction
이 dangling 이 된다. 반대로 A-Box 를 고치는 것은 725,730 트리플 재생성이다.

그래서 **T-Box 에 선언을 추가** 한다 (비파괴). 같은 쌍에 값 0건인 선언 OP 가 있으면
``owl:equivalentProperty`` 로 이어 붙여, 기존 이름으로 질의해도 추론이 연결하게
한다 — 기존 이름·Restriction·딕셔너리 항목이 전부 살아남는다.

domain/range 는 **실제 데이터에서 관측한 타입** 으로 정한다 (추측 금지). 관측
타입이 여러 개면 domain/range 를 붙이지 않고 선언만 한다 — 틀린 제약은 없는
제약보다 나쁘다.

환경변수 ``TBOX_UNDECLARED_OP_BACKFILL=false`` 로 끌 수 있다.
"""
from __future__ import annotations

import logging
import os
from collections import defaultdict

from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef

from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)

#: 한 번에 주입할 상한. 폭주 방지용 — 실측 37개이므로 넉넉하다.
_MAX_INJECT = 200

#: ``(파일 서명) -> 관측 결과`` 캐시. A-Box 는 725,730 트리플이라 rdflib 파싱이
#: 20초 넘게 걸린다. ``improve_tbox`` 를 반복 호출하는 경로(테스트 스위트, S4-FAIL
#: 자동수정 루프, TBOX_INCREMENTAL)에서 그 비용이 곱해진다 — 실측: 이 스텝을
#: 추가한 뒤 전체 스위트가 25분 → 2시간 36분이 됐다.
#: ``step_12f._ABOX_USED_CACHE`` / ``graph_utils._ABOX_USED_CACHE`` 와 같은 계약:
#: 파일 mtime+size 서명이 바뀌면 자동 무효화된다.
_OBSERVED_CACHE: dict[tuple, dict[str, dict] | None] = {}


def _local(node) -> str:
    return str(node).split("#")[-1].split("/")[-1]


def _observed_op_usage(domain_ns: str) -> dict[str, dict] | None:
    """A-Box·master·tacit 에서 **object 가 IRI 인** 도메인 술어를 관측.

    텍스트 스캔이 아니라 그래프 파싱이 필요하다 — 술어 이름만 세면 클래스명·DP 가
    섞여 과다 계수된다 (실측: 텍스트 스캔 333종 vs 실제 OP 51종).

    Returns:
        ``{op_local: {"count": n, "domains": {cls}, "ranges": {cls}}}``.
        ``None`` — 스캔 대상 파일이 없거나 파싱 실패 (판정 불가 → 호출부는 주입
        보류). 0건과 판정 불가를 구분하지 않으면 "미선언 없음" 으로 오독한다.
    """
    import glob

    try:
        from config import ABOX_PATH, SOURCE_DIR
    except Exception as exc:  # noqa: BLE001
        logger.debug("step_15c: 경로 설정 조회 실패: %s", exc)
        return None

    paths = [ABOX_PATH, os.path.join(os.path.dirname(ABOX_PATH), "master_data.ttl")]
    try:
        paths.extend(sorted(glob.glob(os.path.join(SOURCE_DIR, "tacit", "*.ttl"))))
    except Exception as exc:  # noqa: BLE001
        logger.debug("step_15c: tacit 경로 조회 실패: %s", exc)
    existing = [p for p in paths if os.path.exists(p)]
    if not existing:
        # A-Box 생성 전 (S3 는 S7 보다 앞선다) — 정상 상태이므로 빈 관측.
        return {}

    signature = (domain_ns,) + tuple(
        (p, os.stat(p).st_mtime, os.stat(p).st_size) for p in existing
    )
    if signature in _OBSERVED_CACHE:
        return _OBSERVED_CACHE[signature]

    merged = Graph()
    for path in existing:
        try:
            merged.parse(path, format="turtle")
        except Exception as exc:  # noqa: BLE001
            logger.warning("step_15c: %s 파싱 실패 — 주입 보류: %s", path, exc)
            _OBSERVED_CACHE[signature] = None
            return None

    types: dict[URIRef, set[str]] = defaultdict(set)
    for subj, _, obj in merged.triples((None, RDF.type, None)):
        if isinstance(obj, URIRef) and str(obj).startswith(domain_ns):
            types[subj].add(_local(obj))

    # 인스턴스 IRI 접두로 클래스를 유추하기 위한 색인. tacit 이 만든 주체
    # 4,320개는 ``rdf:type`` 이 없는데 (실측: ``ProcessContinuousCasting_…_EQ045``
    # 가 타입 선언 0건) IRI 접두가 A-Box 컨벤션상 클래스명이다. 타입이 없으면
    # domain 을 못 정하고, 그러면 ``step_09_shared_domain_fill`` 이 ``owl:Thing``
    # 폴백을 채워 이 리포가 방금 없앤 사각지대가 되살아난다.
    known_classes = {
        _local(c) for c in merged.subjects(RDF.type, None)
    } | {
        _local(o) for _, _, o in merged.triples((None, RDF.type, None))
        if isinstance(o, URIRef) and str(o).startswith(domain_ns)
    }

    def _infer_type(node) -> set[str]:
        """관측 타입. 없으면 인스턴스 IRI 접두에서 유추한다."""
        observed = types.get(node)
        if observed:
            return observed
        if not isinstance(node, URIRef):
            return set()
        local = _local(node)
        # ``ClassName_suffix…`` 컨벤션 — 가장 긴 일치를 고른다 (Process 와
        # ProcessContinuousCasting 이 둘 다 접두일 수 있다).
        best = ""
        for cls in known_classes:
            if local.startswith(cls + "_") and len(cls) > len(best):
                best = cls
        return {best} if best else set()

    usage: dict[str, dict] = {}
    for subj, pred, obj in merged:
        if pred == RDF.type or not isinstance(obj, URIRef):
            continue
        if not str(pred).startswith(domain_ns):
            continue
        meta = usage.setdefault(
            _local(pred), {"count": 0, "domains": set(), "ranges": set()},
        )
        meta["count"] += 1
        meta["domains"].update(_infer_type(subj))
        meta["ranges"].update(_infer_type(obj))
    _OBSERVED_CACHE[signature] = usage
    return usage


def apply(g: Graph, ctx: StepContext) -> StepResult:
    if (os.getenv("TBOX_UNDECLARED_OP_BACKFILL") or "true").strip().lower() in (
        "false", "0", "off", "no",
    ):
        return StepResult(
            name="step_15c_undeclared_op_backfill",
            stats={"undeclared_op_backfill_disabled": True},
            step_number="15c", step_label="undeclared_op_backfill",
        )

    before = len(g)
    ns = ctx.domain_ns
    usage = _observed_op_usage(ns)
    if usage is None:
        logger.warning(
            "step_15c: A-Box 관측 실패 — 미선언 OP 주입 보류 (0건과 구분)",
        )
        return StepResult(
            name="step_15c_undeclared_op_backfill",
            stats={"undeclared_op_withheld_scan_failed": True},
            triples_delta=0,
            step_number="15c", step_label="undeclared_op_backfill",
        )

    declared = {
        _local(o) for o in g.subjects(RDF.type, OWL.ObjectProperty)
        if isinstance(o, URIRef) and str(o).startswith(ns)
    }
    # DP 로 선언된 이름은 건드리지 않는다 — object 가 IRI 인 것만 모았으므로
    # 겹칠 일이 드물지만, 겹치면 타입 충돌(OP+DP)이 되어 HermiT 가 막는다.
    declared_dps = {
        _local(o) for o in g.subjects(RDF.type, OWL.DatatypeProperty)
        if isinstance(o, URIRef) and str(o).startswith(ns)
    }

    injected: list[str] = []
    equiv_linked: list[str] = []
    skipped_ambiguous: list[str] = []
    # equivalence 를 **거부한** 사유별 기록. 거부는 조용히 지나가면 안 된다 —
    # 이 스텝이 손상을 만들었던 경로라 무엇을 안 이었는지 남겨야 감사 가능하다.
    skipped_inverse: list[str] = []
    skipped_opposing: list[str] = []
    skipped_ambiguous_equiv: list[str] = []
    for name, meta in sorted(usage.items(), key=lambda kv: -kv[1]["count"]):
        if name in declared or name in declared_dps:
            continue
        if len(injected) >= _MAX_INJECT:
            logger.warning(
                "step_15c: 주입 상한 %d 도달 — 남은 미선언 OP 는 건너뛴다",
                _MAX_INJECT,
            )
            break
        op_uri = URIRef(ns + name)
        g.add((op_uri, RDF.type, OWL.ObjectProperty))
        # 라벨은 **기존 라벨과 겹치지 않게** 만든다. ``humanize_local_name`` 만 쓰면
        # 같은 쌍의 동의어와 영문 라벨이 같아져 ``duplicate_label`` 경고가 늘어난다
        # (실측 2건). 동의어라 의미는 같지만 게이트는 그것을 알 수 없으므로,
        # 관측 출처를 라벨에 남겨 사람과 게이트 모두 구분할 수 있게 한다.
        existing_labels = {
            (str(o), o.language) for o in g.objects(None, RDFS.label)
            if isinstance(o, Literal)
        }
        label_en = _unique_label(_humanize(name), "en", existing_labels,
                                " (observed in data)")
        g.add((op_uri, RDFS.label, Literal(label_en, lang="en")))
        # 한글 라벨도 붙인다 (프롬프트가 모든 OP 에 @ko 를 요구한다). 겹치면 같은
        # 방식으로 구분자를 붙인다 — 실측: 영문만 처리했을 때 ``정비 이력 설비@ko``
        # 가 기존 동의어와 겹쳐 duplicate_label 이 남았다.
        label_ko = _unique_label(_humanize(name), "ko", existing_labels,
                                 " (데이터 관측)")
        g.add((op_uri, RDFS.label, Literal(label_ko, lang="ko")))
        g.add((op_uri, RDFS.comment, Literal(
            f"A-Box 가 {meta['count']:,} 트리플에서 사용 중인 관계 "
            "(step_15c 가 관측해 선언)", lang="ko",
        )))

        # domain/range 는 **관측된 타입이 유일할 때만** 붙인다. 여러 개면 붙이지
        # 않는다 — 틀린 제약은 없는 제약보다 나쁘고, owl:Thing 은 검사를 무력화한다.
        doms, rngs = meta["domains"], meta["ranges"]
        if len(doms) == 1:
            g.add((op_uri, RDFS.domain, URIRef(ns + next(iter(doms)))))
        if len(rngs) == 1:
            g.add((op_uri, RDFS.range, URIRef(ns + next(iter(rngs)))))
        if len(doms) > 1 or len(rngs) > 1:
            skipped_ambiguous.append(name)

        # 같은 (domain, range) 를 잇는 **값 0건** 선언 OP 가 있으면
        # owl:equivalentProperty 로 이어 붙인다 — 기존 이름으로 질의해도 추론이
        # 연결한다. rename 은 하지 않는다: 유령 OP 다수가 Restriction 의
        # onProperty 대상이라 이름을 바꾸면 그 Restriction 이 dangling 이 된다.
        if len(doms) == 1 and len(rngs) == 1:
            from tools.ontology_quality import (
                _directional_tokens_in_labels,
                _has_opposing_directions,
            )

            dom_uri = URIRef(ns + next(iter(doms)))
            rng_uri = URIRef(ns + next(iter(rngs)))
            candidates = []
            for peer in _ops_linking(g, dom_uri, rng_uri):
                peer_local = _local(peer)
                if peer_local == name or usage.get(peer_local, {}).get("count"):
                    continue                      # 자기 자신 / 값 있는 OP 는 제외

                # ① **역관계를 동일시하지 않는다.** (domain, range) 서명만 보면
                # 역관계도 peer 로 보인다 — self-referential OP (domain == range)
                # 는 특히 그렇다. ``ops_linking`` docstring 이 "domain == range 인
                # self-referential OP 는 역방향이 같은 쌍이라 호출부가 별도로
                # 판단해야 한다" 고 경고했는데 이 호출부가 이행하지 않았다.
                #
                # 실측 손상 (2026-08-15): followedBy 가 directlyFollows 와
                # directlyPrecedes **양쪽** 에 equivalentProperty 로 묶였고, 그 둘은
                # 서로 inverseOf 다. A≡B, A≡C, B=inv(C) ⟹ 세 OP 전부 대칭.
                # owlrl 로 재현: tacit 공정 흐름 3 트리플 → 6 트리플, 고로→제강이
                # 제강→고로 도 함의한다. S5 tacit 의 유일한 목적(공정 순서)이 무너진다.
                if (op_uri, OWL.inverseOf, peer) in g \
                        or (peer, OWL.inverseOf, op_uri) in g:
                    skipped_inverse.append(f"{name}≁{peer_local}")
                    continue

                # ② **반의어 쌍을 동일시하지 않는다.** origin↔destination 처럼
                # 라벨이 반대 방향을 가리키면 같은 (domain, range) 를 가져도
                # 다른 관계다. 판정은 공용 가드를 호출한다 (step_22d/22e 가 쓰는
                # 것과 **같은** 함수 — 사본을 만들면 갈라진다).
                #
                # 실측 손상 (2026-08-15): hasDestinationWarehouse 가
                # transportationHasOriginWarehouse 와 묶여, 운송 300건에서 "출발
                # 창고" 질의가 목적지를 반환한다 (TRP00001 실제 출발 WH001 은 KG 부재).
                if _has_opposing_directions([
                    _directional_tokens_in_labels(g, op_uri),
                    _directional_tokens_in_labels(g, peer),
                ]):
                    skipped_opposing.append(f"{name}≁{peer_local}")
                    continue
                candidates.append(peer)

            # ③ **후보가 여러 개면 잇지 않는다.** 어느 쪽이 진짜 동의어인지
            # 결정할 근거가 없다. 위 domain/range 처리(len(doms) > 1 이면 생략)와
            # 같은 원칙 — 틀린 공리는 없는 공리보다 나쁘다. 여러 후보를 다 이으면
            # 그것들끼리도 equivalent 가 되어(추론 전파) 서로 다른 관계가 뭉개진다.
            if len(candidates) > 1:
                skipped_ambiguous_equiv.append(
                    f"{name}→{sorted(_local(p) for p in candidates)}"
                )
            elif candidates:
                peer = candidates[0]
                g.add((op_uri, OWL.equivalentProperty, peer))
                g.add((peer, OWL.equivalentProperty, op_uri))
                equiv_linked.append(f"{name}≡{_local(peer)}")
        injected.append(name)

    if injected:
        logger.info(
            "Step 15c: A-Box 가 쓰지만 T-Box 에 없던 OP %d개 선언 (%s). "
            "미선언 술어는 domain/range 제약이 없어 어떤 데이터든 통과하고, "
            "딕셔너리·CQ 는 그 이름을 몰라 질의가 0행을 반환했다",
            len(injected), sorted(injected)[:5],
        )
    if equiv_linked:
        logger.info(
            "Step 15c: 값 0건 동의어와 owl:equivalentProperty 로 연결 %d건 (%s) — "
            "기존 이름으로 질의해도 추론이 연결한다",
            len(equiv_linked), sorted(equiv_linked)[:5],
        )
    if skipped_ambiguous:
        logger.info(
            "Step 15c: 관측 타입이 여러 개라 domain/range 를 붙이지 않은 OP %d개 "
            "(%s) — 틀린 제약은 없는 제약보다 나쁘다",
            len(skipped_ambiguous), sorted(skipped_ambiguous)[:5],
        )
    if skipped_inverse:
        logger.info(
            "Step 15c: **역관계라서** equivalentProperty 를 잇지 않은 쌍 %d건 (%s) — "
            "이었다면 관계가 대칭이 되어 방향이 사라진다",
            len(skipped_inverse), sorted(skipped_inverse)[:5],
        )
    if skipped_opposing:
        logger.info(
            "Step 15c: **반의어(origin↔destination 류)라서** equivalentProperty 를 "
            "잇지 않은 쌍 %d건 (%s) — 이었다면 반대 개념이 동일시된다",
            len(skipped_opposing), sorted(skipped_opposing)[:5],
        )
    if skipped_ambiguous_equiv:
        logger.info(
            "Step 15c: 동의어 후보가 여러 개라 equivalentProperty 를 잇지 않은 OP "
            "%d개 (%s) — 어느 쪽이 진짜인지 결정할 근거가 없다",
            len(skipped_ambiguous_equiv), sorted(skipped_ambiguous_equiv)[:5],
        )

    return StepResult(
        name="step_15c_undeclared_op_backfill",
        stats={
            "undeclared_ops_declared": len(injected),
            "undeclared_op_names": sorted(injected)[:20],
            "undeclared_op_equiv_linked": len(equiv_linked),
            "undeclared_op_ambiguous_types": len(skipped_ambiguous),
            "undeclared_op_equiv_skipped_inverse": len(skipped_inverse),
            "undeclared_op_equiv_skipped_opposing": len(skipped_opposing),
            "undeclared_op_equiv_skipped_ambiguous": len(skipped_ambiguous_equiv),
            "undeclared_op_equiv_skip_samples": (
                sorted(skipped_inverse + skipped_opposing)[:10]
            ),
        },
        triples_delta=len(g) - before,
        step_number="15c",
        step_label="undeclared_op_backfill",
    )


def _unique_label(base: str, lang: str, existing: set, suffix: str) -> str:
    """기존 라벨과 겹치지 않는 라벨.

    같은 쌍의 동의어와 라벨이 같아지면 ``duplicate_label`` 경고가 늘어난다. 의미는
    같지만 게이트는 그것을 알 수 없으므로, 관측 출처를 접미로 남겨 사람과 게이트
    모두 구분할 수 있게 한다.
    """
    if (base, lang) not in existing:
        return base
    return f"{base}{suffix}"


def _humanize(name: str) -> str:
    from domain.graph_utils import humanize_local_name
    return humanize_local_name(name)


def _ops_linking(g: Graph, dom_uri: URIRef, rng_uri: URIRef) -> set:
    from domain.graph_utils import ops_linking
    return ops_linking(g, dom_uri, rng_uri)
