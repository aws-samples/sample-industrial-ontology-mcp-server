"""Step 15 — 크로스 도메인 ObjectProperty 추가 (RR 향상).

본문 ontology_quality.py 의 Step 15 블록을 그대로 모듈로 옮김.

도메인 간 의미적 관계를 추가하여 ObjectProperty 비율 (RR) 을 0.3+ 로 향상.
Tartir et al. (2005) OntoQA: RR = OP / (OP+DP), 정상 0.3~0.6.

주의: 본 모듈은 ``SOURCE_RAWDATA_DIR`` 를 안전하게 module-level import 로
참조한다. 원본 본문은 함수 스코프 ``from config import SOURCE_RAWDATA_DIR``
때문에 Step 10 등 상위 step 에서 ``UnboundLocalError`` latent bug 가 있었으나,
Step 10 추출 모듈 (step_10_shared_fk_op_domain) 이 의도적 no-op stub 으로
mechanical equivalence 를 보존한다. 이 두 step 의 latent bug 는 별도 PR 에서
일괄 수정하기 전까지 본 상태를 유지.
"""
from __future__ import annotations

import logging
import os
import re

from rdflib import OWL, RDF, RDFS, Graph, Literal

from domain.graph_utils import (
    abox_used_local_names,
    csv_fk_class_pairs,
    ops_linking,
)
from tools.quality_steps._base import StepContext, StepResult

logger = logging.getLogger(__name__)

#: FK 근거 게이트 모드. ``skip`` (default) 은 CSV FK 근거가 없고 A-Box·tacit 도
#: 쓰지 않는 쌍을 **만들지 않는다**. ``warn`` 은 예전처럼 만들고 카운트만 한다.
#: 도메인 지식 관계를 의도적으로 넣고 싶으면 ``warn`` 으로 되돌리거나
#: ``rules/domain/tbox_manual_additions.ttl`` 에 명시한다 (결정적·검토 가능).
_FK_GROUNDING_ENV = "TBOX_CROSS_DOMAIN_FK_GATE"


def apply(g: Graph, ctx: StepContext) -> StepResult:
    import config  # monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", ...) 동적 반영
    from tools.ontology_quality import (
        _CROSS_DOMAIN_OP_META,
        DOMAIN_NS_OBJ,
        _load_cross_domain_op_meta,
        _load_cross_domain_ops,
    )

    # 설정(design_patterns.json)이 domain/range 를 직접 주면 그것이 우선한다.
    # 코드의 _CROSS_DOMAIN_OP_META 는 데모 철강 예시 폴백이며, 도메인을 바꾸면
    # 이름만 교체되고 메타가 남아 전량 skip 된다 (2026-07-25 실측: 교집합 0).
    _op_meta = {**_CROSS_DOMAIN_OP_META, **_load_cross_domain_op_meta()}
    SOURCE_RAWDATA_DIR = config.SOURCE_RAWDATA_DIR

    before = len(g)
    steel_str = ctx.domain_ns
    cross_ops_added = 0
    cross_ops_skipped_duplicate = 0
    cross_ops_dangling_inverse_cleaned = 0
    cross_ops_kept_abox_used = 0
    cross_ops_skipped_no_fk = 0
    cross_ops_skipped_no_fk_names: list[str] = []
    fk_pairs_unavailable = False
    # try 블록 앞에서 초기화한다 — stats dict 는 except 이후에도 평가되므로
    # 블록 안에서만 대입하면 조기 예외 시 NameError 로 스텝 결과를 잃는다.
    fk_gate_mode = (os.getenv(_FK_GROUNDING_ENV) or "skip").strip().lower()
    abox_used_unavailable = False
    skipped_no_meta: list[str] = []
    try:
        existing_ops = {
            str(p) for p in g.subjects(RDF.type, OWL.ObjectProperty)
            if str(p).startswith(steel_str)
        }
        existing_classes = {
            str(c) for c in g.subjects(RDF.type, OWL.Class)
            if str(c).startswith(steel_str)
        }

        _csv_backed_classes: set[str] = set()
        try:
            if os.path.isdir(SOURCE_RAWDATA_DIR):
                for _fn in os.listdir(SOURCE_RAWDATA_DIR):
                    if _fn.lower().endswith(".csv"):
                        _base = _fn.rsplit(".", 1)[0]
                        _csv_backed_classes.add(_base)
                        _csv_backed_classes.add(_base.replace("_", ""))
        except Exception as e:
            logger.debug("CSV-backed class 스캔 실패 (기존 동작 유지): %s", e)

        _cross_domain_op_names = _load_cross_domain_ops()
        _CROSS_DOMAIN_OPS = []
        for op_name in _cross_domain_op_names:
            meta = _op_meta.get(op_name)
            if not meta:
                skipped_no_meta.append(op_name)
                continue
            dom_name, rng_name, lbl_en, lbl_ko, cmt_ko, inv_name = meta
            _CROSS_DOMAIN_OPS.append(
                (op_name, dom_name, rng_name, lbl_en, lbl_ko, cmt_ko, inv_name)
            )

        # A-Box 사용량은 **한 번만** 조회한다 (500MB+ 순차 읽기). 판정 불가면
        # ``None`` 이 오는데, 그때는 억제-취소 신호를 쓰지 않는다 — 억제 자체는
        # 비파괴적(생성 안 함)이므로 여기서는 fail-closed 가 아니라 기존 동작을
        # 유지하는 것이 맞다. 대신 stats 로 드러낸다.
        _candidate_names = {n for spec in _CROSS_DOMAIN_OPS for n in (spec[0], spec[6])}
        abox_used = abox_used_local_names(_candidate_names)
        if abox_used is None:
            abox_used_unavailable = True
            logger.warning(
                "Step 15: A-Box 사용 판정 불가 — 중복 억제-취소 신호 없이 진행한다 "
                "(값 있는 OP 가 억제될 수 있다)",
            )

        # FK 근거 게이트. CSV FK 가 잇지 않는 쌍은 **데이터에 근거가 없는 관계** 다.
        #
        # 실측 (2026-08-14, 이 스텝 1회 실행): 신규 OP 53개 중 CSV FK 근거가 있는
        # 것은 8개, A-Box·tacit 이 쓰는 것은 14개, **둘 다 없는 것이 39개** 였다
        # (correlatedWithWaste / hasChemicalAnalysisTagMaster / storedIn / …).
        # 그 결과 OP 사용률이 39.1% → 35.1% 로 떨어지고 RR 은 0.323 → 0.412 로
        # 올랐다 — 게이트가 요구하는 지표는 좋아지는데 실제 정합은 나빠진다.
        #
        # 무근거 OP 는 (1) 질의가 0건을 정답처럼 돌려주고 (2) 딕셔너리·CQ 를
        # 오염시키고 (3) 중복 쌍을 만들어 A-Box 이름 선택을 흔든다.
        #
        # ``None`` 은 판정 불가이므로 게이트를 적용하지 않는다 — 0건과 혼동하면
        # 정당한 주입까지 전부 막는다.
        fk_pairs = csv_fk_class_pairs()
        if fk_pairs is None:
            fk_pairs_unavailable = True
            logger.warning(
                "Step 15: CSV FK 쌍 판정 불가 — FK 근거 게이트를 적용하지 않는다",
            )
        def _clean_dangling_inverse(op_u, inv_u) -> int:
            """건너뛰는 쌍의 이름을 가리키는 **기존 dangling inverseOf** 를 정리.

            선언만 막고 참조를 남기면 그 참조가 영구히 허공을 가리킨다 (실측:
            hasGHGAirEmissionMonitoring → isAirEmissionMonitoringOfGHG 1건).

            **양방향으로** 지운다. orphan 이 object 인 트리플만 지우면
            ``orphan owl:inverseOf X`` (orphan 이 subject) 는 남는데, 그 subject 는
            OP 로 선언되지 않았으므로 step_22d 가 세지 않는다 — 게이트에 보이지
            않는 dangling 이 영구히 남는다.

            중복 skip 경로와 FK 근거 skip 경로가 **같은 정리** 를 해야 한다. 예전엔
            중복 경로에만 있어서, FK 게이트가 건너뛴 쌍의 dangling 이 남았다
            (2026-08-15 실측: test_step_15_never_leaves_a_dangling_inverse_reference).
            """
            cleaned = 0
            for orphan in (op_u, inv_u):
                if (orphan, RDF.type, OWL.ObjectProperty) in g:
                    continue
                for subj, _, _ in list(g.triples((None, OWL.inverseOf, orphan))):
                    g.remove((subj, OWL.inverseOf, orphan))
                    cleaned += 1
                for _, _, obj in list(g.triples((orphan, OWL.inverseOf, None))):
                    g.remove((orphan, OWL.inverseOf, obj))
                    cleaned += 1
            return cleaned

        def _norm_cls(name: str) -> str:
            return name.replace("_", "").lower()

        def _fk_grounded(dom_name: str, rng_name: str) -> bool:
            """CSV FK 가 이 두 클래스를 잇는가 (방향 무관).

            FK 컬럼은 한쪽 테이블에만 존재하므로 방향을 따지면 정당한 쌍을 놓친다.
            """
            if not fk_pairs:
                return False
            a, b = _norm_cls(dom_name), _norm_cls(rng_name)
            return (a, b) in fk_pairs or (b, a) in fk_pairs

        for op_name, dom_name, rng_name, lbl_en, lbl_ko, cmt_ko, inv_name in _CROSS_DOMAIN_OPS:
            op_uri = DOMAIN_NS_OBJ[op_name]
            inv_uri = DOMAIN_NS_OBJ[inv_name]
            dom_uri = DOMAIN_NS_OBJ[dom_name]
            rng_uri = DOMAIN_NS_OBJ[rng_name]

            if (str(dom_uri) not in existing_classes
                    or str(rng_uri) not in existing_classes):
                continue
            if _csv_backed_classes:
                _dom_has_csv = any(
                    dom_name.lower() in cn.lower() or cn.lower() in dom_name.lower()
                    for cn in _csv_backed_classes
                )
                _rng_has_csv = any(
                    rng_name.lower() in cn.lower() or cn.lower() in rng_name.lower()
                    for cn in _csv_backed_classes
                )
                if not (_dom_has_csv and _rng_has_csv):
                    continue

            # FK 근거 게이트 — 데이터에 근거가 없는 쌍은 만들지 않는다.
            # **A-Box·tacit 이 이 이름을 쓰면 예외** 다: 값이 있는 관계를 막으면
            # T-Box 미선언 술어가 되어 중복보다 나쁘다 (아래 억제-취소와 같은 논리).
            # 판정 불가(fk_pairs is None)면 게이트를 적용하지 않는다.
            if (not fk_pairs_unavailable) and fk_gate_mode != "warn":
                _live_names = (
                    {n for n in (op_name, inv_name) if n in abox_used}
                    if abox_used else set()
                )
                if not _fk_grounded(dom_name, rng_name) and not _live_names:
                    cross_ops_skipped_no_fk += 1
                    if len(cross_ops_skipped_no_fk_names) < 40:
                        cross_ops_skipped_no_fk_names.append(
                            f"{op_name}({dom_name}→{rng_name})"
                        )
                    logger.info(
                        "cross-domain OP 쌍 생성 건너뜀 (FK 근거 없음): %s / %s "
                        "(%s↔%s) — CSV FK 가 두 클래스를 잇지 않고 A-Box·tacit 도 "
                        "이 이름을 쓰지 않는다. 도메인 지식으로 넣으려면 "
                        "rules/domain/tbox_manual_additions.ttl 에 명시하라",
                        op_name, inv_name, dom_name, rng_name,
                    )
                    # 중복 skip 과 **동일하게** dangling 참조를 정리한다.
                    cross_ops_dangling_inverse_cleaned += _clean_dangling_inverse(
                        op_uri, inv_uri,
                    )
                    continue

            # 이름이 달라도 **같은 (domain, range) 를 잇는 OP 가 이미 있으면
            # 만들지 않는다**. 예전엔 이름만 봐서 동의어가 쌓였다 (실측: 한 번의
            # S3 에서 cross_domain_ops_added 53개, 중복 OP 21 → 87). CSV FK 컬럼은
            # 하나뿐이라 A-Box 는 그중 하나만 채우고 나머지는 값 0건으로 남는다.
            #
            # **판정은 쌍(pair) 단위다 — 다리(leg) 단위로 하면 안 된다.** 정방향은
            # ``owl:inverseOf inv_uri`` 를 쓰므로, 역방향만 건너뛰면 그 참조가
            # 선언되지 않은 이름을 가리키는 **dangling inverseOf** 가 된다. 실측
            # (2026-08-11): leg 단위 가드로 dangling 이 0 → 10 → 15 로 늘었고, 어떤
            # validator 도 잡지 못했다 (check_quality_rules 변화 없음 / SHACL
            # conforms / HermiT consistent — dangling 이름은 A-Box 사용량이 0 이라
            # undeclared_op 체크의 사각지대다). 그 결과 정상 쌍의 역방향 트리플이
            # 사라져 "빈 관계로 질의하면 0건" 이라는 이 가드의 방지 목표 자체가
            # 재현된다.
            #
            # 그래서 두 다리 중 **하나라도** 이미 점유돼 있으면 쌍 전체를 건너뛴다.
            #
            # ``domain == range`` (self-referential) 는 별도 예외가 필요 없다:
            # 두 조회가 같은 집합을 돌려주므로 OR 결과가 동일하다. leg 단위
            # 판정에서는 예외가 필요했지만 쌍 단위에서는 죽은 분기다.
            forward_taken = ops_linking(g, dom_uri, rng_uri)
            inverse_taken = ops_linking(g, rng_uri, dom_uri)
            pair_taken = forward_taken or inverse_taken
            forward_new = str(op_uri) not in existing_ops
            inverse_new = str(inv_uri) not in existing_ops

            # **A-Box 가 이 이름으로 실제 트리플을 만들었으면 억제하지 않는다.**
            # 중복 억제의 목적은 "값 0건인 빈 관계" 를 없애는 것인데, 값이 있는
            # 쪽을 억제하면 그 관계가 T-Box 미선언 술어로 남아 같은 문제를 더 크게
            # 만든다. 실측 (2026-08-11): 억제 대상 43개 중 8개가 A-Box 값을 보유하고
            # (realTimeDataOf 36,000 / isEnergyProfileOf 1,500 / …), 그 8개는 모두
            # **자기 쌍에서 값을 가진 유일하거나 최다인 OP** 였다 —
            # RealTimeData→TagMaster 의 생존자 isTagOfRealTimeData 는 0건이다.
            # 전체 재실행 시 미선언 트리플이 52,471 → 91,117 로 늘어난다.
            if pair_taken and (forward_new or inverse_new) and abox_used:
                live = {n for n in (op_name, inv_name) if n in abox_used}
                if live:
                    logger.info(
                        "cross-domain OP 쌍 억제 취소: %s (%s↔%s) — A-Box 가 이 "
                        "이름으로 트리플을 만들었다. 값 있는 쪽을 억제하면 미선언 "
                        "술어가 되어 중복보다 나쁘다",
                        sorted(live), dom_name, rng_name,
                    )
                    cross_ops_kept_abox_used += 1
                    pair_taken = set()

            if pair_taken and (forward_new or inverse_new):
                logger.info(
                    "cross-domain OP 쌍 생성 건너뜀: %s / %s (%s↔%s) — 같은 쌍을 "
                    "잇는 OP %s 가 이미 있다. 한쪽만 만들면 owl:inverseOf 가 "
                    "미선언 이름을 가리켜 역방향 트리플이 사라진다",
                    op_name, inv_name, dom_name, rng_name,
                    sorted(
                        str(d).split("#")[-1] for d in (forward_taken | inverse_taken)
                    )[:3],
                )
                cross_ops_skipped_duplicate += 1
                cross_ops_dangling_inverse_cleaned += _clean_dangling_inverse(
                    op_uri, inv_uri,
                )
                continue

            if forward_new:
                g.add((op_uri, RDF.type, OWL.ObjectProperty))
                g.add((op_uri, RDFS.label, Literal(lbl_en, lang="en")))
                g.add((op_uri, RDFS.label, Literal(lbl_ko, lang="ko")))
                g.add((op_uri, RDFS.comment, Literal(cmt_ko, lang="ko")))
                g.add((op_uri, RDFS.domain, dom_uri))
                g.add((op_uri, RDFS.range, rng_uri))
                g.add((op_uri, OWL.inverseOf, inv_uri))
                existing_ops.add(str(op_uri))
                cross_ops_added += 1

            if inverse_new:
                inv_lbl_en = (
                    inv_name[0].lower()
                    + re.sub(r"(?<!^)(?=[A-Z])", " ", inv_name[1:]).lower()
                )
                g.add((inv_uri, RDF.type, OWL.ObjectProperty))
                g.add((inv_uri, RDFS.label, Literal(inv_lbl_en, lang="en")))
                g.add((inv_uri, RDFS.label, Literal(f"{lbl_ko} (역)", lang="ko")))
                g.add((inv_uri, RDFS.comment, Literal(f"{cmt_ko} (역방향)", lang="ko")))
                g.add((inv_uri, RDFS.domain, rng_uri))
                g.add((inv_uri, RDFS.range, dom_uri))
                g.add((inv_uri, OWL.inverseOf, op_uri))
                existing_ops.add(str(inv_uri))
                cross_ops_added += 1

    except Exception as e:
        logger.warning("크로스 도메인 OP 추가 실패: %s", e)

    if skipped_no_meta:
        # 설정이 이름만 주고 domain/range 메타가 없으면 OP 를 만들 수 없다. 조용히
        # 넘기면 "cross-domain OP 0개" 가 정상처럼 보이므로 반드시 보고한다
        # (2026-07-25: 배포 설정 22개 전부가 이 경로로 사라졌다).
        logger.warning(
            "Step 15: domain/range 메타가 없어 건너뛴 cross-domain OP %d개 — "
            "design_patterns.json 의 cross_domain_ops 를 "
            '{"name","domain","range"} 객체 형태로 보강 필요: %s',
            len(skipped_no_meta), sorted(skipped_no_meta)[:10],
        )

    return StepResult(
        name="step_15_cross_domain_ops",
        stats={
            "cross_domain_ops_added": cross_ops_added,
            "cross_domain_ops_skipped_duplicate": cross_ops_skipped_duplicate,
            "cross_domain_dangling_inverse_cleaned": cross_ops_dangling_inverse_cleaned,
            "cross_domain_ops_kept_abox_used": cross_ops_kept_abox_used,
            "cross_domain_abox_signal_unavailable": abox_used_unavailable,
            "cross_domain_ops_skipped_no_fk": cross_ops_skipped_no_fk,
            "cross_domain_ops_skipped_no_fk_names": cross_ops_skipped_no_fk_names,
            "cross_domain_fk_gate_mode": fk_gate_mode,
            "cross_domain_fk_signal_unavailable": fk_pairs_unavailable,
            "skipped_no_meta_count": len(skipped_no_meta),
            "skipped_no_meta": sorted(skipped_no_meta)[:20],
        },
        triples_delta=len(g) - before,
        step_number=15,
        step_label="cross_domain_OPs",
    )
