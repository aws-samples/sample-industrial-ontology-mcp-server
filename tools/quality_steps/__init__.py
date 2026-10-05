"""improve_tbox 의 43 step 을 모듈별로 분리한 list-driven pipeline.

Phase 진행:
- Phase 1~7 (~2026-05-17): 본문 inline 블록 → step_*.py 추출 (43 모듈)
- Phase 8 (2026-05-18, 완료): list-driven pipeline runner 도입.

코디네이터 (improve_tbox) 의 5단 hook (실행 순서):
- ``_PRE_STEPS``: graph 로드 직후. Phase 9+ 확장 hook (현재 비어 있음).
- ``_MAIN_PRE_STEP9``: Step 0~8 (안티패턴 / disjoint / inverseOf / 메타데이터).
- ``_STEP9_GROUP``: Step 9~9f. 묶음 change_log entry 1건만 기록.
- ``_MAIN_POST_STEP9``: Step 10~16 (FK / tacit / abstract / cross-domain / scope).
- ``_POST_STEPS``: Step 17~29 (label / completeness / bnode / dedup / OntoClean / hasKey).

각 step 모듈은 ``apply(g, ctx) -> StepResult`` 를 export. 본 모듈에서 명시
순서로 import 후 5개 리스트 중 하나에 등록한다. ``run_step_pipeline`` /
``run_step_pipeline_grouped`` 가 list 를 순회하며 stats / change_log 누적,
``RuntimeError`` 는 호출자로 전파, 그 외 예외는 log + skip.
"""
from __future__ import annotations

import logging

from rdflib import Graph

from tools.quality_steps import (
    step_00_antipatterns,
    step_01_disjoint_complete,
    step_02_inverse_bidirectional,
    step_02b_iof_namespace_migrate,
    step_03_op_domain_default,
    step_04_korean_comment_merge,
    step_05_comment_code_cleanup,
    step_06_mps_union,
    step_07_metadata,
    step_07b_imports_resolvable,
    step_08_xsd_date_to_datetime,
    step_09_shared_domain_fill,
    step_09a2_foreign_ns_domain_fix,
    step_09a_inverse_range_promotion,
    step_09b_op_range_fix,
    step_09c_dp_range_xsd,
    step_09d_phantom_class_fix,
    step_09e_multi_domain_rollup,
    step_09f_union_redundant_cleanup,
    step_10_shared_fk_op_domain,
    step_11_tacit_class,
    step_11b_csv_class_synthesis,
    step_11c_table_name_class_merge,
    step_11d_duplicate_source_class_merge,
    step_12_intermediate_abstract,
    step_12b_class_specific_dp,
    step_12c_coverage_gate,
    step_12d2_source_literal_cleanup,
    step_12d3_dp_source_backfill,
    step_12d_csv_dp_inject,
    step_12e_dp_source_gate,
    step_12f_orphan_dp_prune,
    step_12g_dp_source_split,
    step_12h_iof_category_conflict,
    step_12i_dp_range_csv_reality,
    step_12j_collection_literal_cleanup,
    step_12k_duplicate_source_claim_resolve,
    step_13_pk_functional_someValuesFrom,
    step_13b_cardinality_data_reality,
    step_13c_op_max_cardinality_direction,
    step_13d_op_functional_fanout_audit,
    step_13e_existential_axiom_audit,
    step_14_subgroup_creation,
    step_14b_disjoint_axis_split,
    step_15_cross_domain_ops,
    step_15b_fk_op_autocreate,
    step_15c_undeclared_op_backfill,
    step_15d_op_source_backfill,
    step_16_scope_emission,
    step_17_label_synthesis,
    step_18_completeness,
    step_19_bnode_skolemize,
    step_19b_restriction_owner_split,
    step_20_restriction_dedup,
    step_21_over_engineered_dep,
    step_21b_dead_stub_prune,
    step_21c_anon_expr_deprecated_cleanup,
    step_22_dup_op,
    step_22b_subprop_cycle,
    step_22c_subclass_cycle,
    step_22d_duplicate_op_gate,
    step_22e_duplicate_op_prune,
    step_22f_op_grounding_gate,
    step_22g_direction_collapse,
    step_23_dup_dp,
    step_23c_cross_domain_subprop,
    step_24_abstract_category,
    step_24b_odp_auto_apply,
    step_24c_undeclared_schema_ref_prune,
    step_25_ontoclean,
    step_26_module_membership,
    step_27_owl_haskey,
    step_28_disjoint_union,
    step_29_property_chain,
    step_30_tbox_manual_additions,
    step_31_code_meaning_comment,
    step_31b_unfillable_class_audit,
)
from tools.quality_steps._base import StepContext, StepFn, StepResult

# ───────────────────────────────────────────────────────────────────
# Phase 8: 본문 inline call 23개를 list 로 등록 (호출 순서 = 본문 순서).
# Step 9 series (9~9f) 8개는 _STEP9_GROUP 으로 분리 — 묶음 change_log
# entry 1건만 기록.
# ───────────────────────────────────────────────────────────────────
_MAIN_PRE_STEP9: list[StepFn] = [
    step_00_antipatterns.apply,
    step_01_disjoint_complete.apply,
    step_02_inverse_bidirectional.apply,
    # 2b 는 계층 생성(12/14)·IOF 정렬(12) **앞** — 구 IOF IRI 를 먼저 정본으로
    # 바꿔야 이후 스텝이 참조를 실제로 해소된 것으로 취급한다. 상수 교정만으로는
    # 파일에 박힌 문자열이 안 바뀐다(실측: 해소율 12%→14% 로 거의 불변).
    step_02b_iof_namespace_migrate.apply,
    step_03_op_domain_default.apply,
    step_04_korean_comment_merge.apply,
    step_05_comment_code_cleanup.apply,
    step_06_mps_union.apply,
    step_07_metadata.apply,
    # 7b 는 7 직후 — 7 이 imports 를 보강한 **뒤** 해석 불가한 대상을 걷어낸다.
    # 용어 네임스페이스가 imports 에 남으면 추론기가 네트워크로 나가 실패하고,
    # HermiT 이 온톨로지를 열지 못해 **S9.1 게이트가 통째로 사라진다**
    # (실측 2026-08-18: "Cannot download …/construct/" → 제거 후 unsat 4건 노출).
    step_07b_imports_resolvable.apply,
    step_08_xsd_date_to_datetime.apply,
]

_STEP9_GROUP: list[StepFn] = [
    step_09_shared_domain_fill.apply,
    step_09a_inverse_range_promotion.apply,
    step_09a2_foreign_ns_domain_fix.apply,
    step_09b_op_range_fix.apply,
    step_09c_dp_range_xsd.apply,
    step_09d_phantom_class_fix.apply,
    step_09e_multi_domain_rollup.apply,
    step_09f_union_redundant_cleanup.apply,
]

_MAIN_POST_STEP9: list[StepFn] = [
    step_10_shared_fk_op_domain.apply,
    step_11_tacit_class.apply,
    step_11b_csv_class_synthesis.apply,
    # 11c 는 클래스 합성(11b) 직후, 중간 추상 클래스 생성(12) 전에 실행해야
    # 한다 — 병합을 미루면 12/14 가 테이블명 클래스까지 계층에 편입시킨다.
    step_11c_table_name_class_merge.apply,
    # 11d 는 11c 직후 — 테이블명 병합으로 이름이 정리된 뒤 출처 집합을 봐야
    # 같은 실체의 중복을 정확히 판별한다. 12(중간 추상 클래스) 전에 끝내야
    # 죽은 클래스가 계층에 편입되지 않는다.
    step_11d_duplicate_source_class_merge.apply,
    step_12_intermediate_abstract.apply,
    # 12h 는 step_12 (iof_parent 적용) **직후** — 그 스텝이 붙인 IOF 부모와
    # S2 LLM 직접 매핑이 충돌하면 BFO 배타성 때문에 unsatisfiable 이 된다
    # (실측: namespace 교정으로 참조가 해소되자 unsat 8건이 드러났다).
    step_12h_iof_category_conflict.apply,
    step_12b_class_specific_dp.apply,
    step_12d_csv_dp_inject.apply,
    # 12d-2 는 12d 직후·12e(출처 게이트) 앞 — 게이트는 존재 여부만 세므로
    # Turtle 표기가 섞인 값도 "표기됨" 으로 집계한다. 측정 전에 정리해야
    # 커버리지 수치가 실제 사용 가능한 표기를 뜻한다.
    step_12d2_source_literal_cleanup.apply,
    # 12d-3 은 12d2(리터럴 정리) 직후·12c/12e(게이트) 앞 — 순서가 계약이다.
    # 12d2 앞에 두면 오염된 값("steel:string")을 "이미 표기됨"으로 읽어 건너뛰고,
    # 그 뒤 12d2 가 그 값을 지워 **표기가 영구히 사라진다**. 게이트 앞에 둬야
    # 커버리지 수치가 실제 사용 가능한 표기를 뜻한다.
    step_12d3_dp_source_backfill.apply,
    step_12c_coverage_gate.apply,
    # 12e 는 12d (CSV DP 주입) 이후에 측정해야 한다 — 주입된 DP 도 출처 표기
    # 대상이므로, 앞에 두면 실제보다 낮은 커버리지를 보고한다.
    step_12e_dp_source_gate.apply,
    # 12g 는 12e 측정 직후 — 게이트 로그가 정리 전 규모를 보여준 뒤
    # 중복 출처 표기를 정리한다 (전용 DP 가 있는 컬럼만).
    step_12g_dp_source_split.apply,
    # 12k 는 12g(한 DP → 여러 컬럼) 직후 — **반대 방향**(여러 DP → 한 컬럼)을 본다.
    # 12g 가 먼저 돌아야 그쪽이 해소할 수 있는 중복은 이미 사라진 상태에서 센다.
    # A-Box 생성기는 이 충돌을 만나면 authoritative source 매핑을 **양쪽 다 버리고**
    # 이름 추측 폴백에 맡긴다 (2026-08-29: 그 폴백이 실패해 FK 리터럴 3개가 0건).
    step_12k_duplicate_source_claim_resolve.apply,
    # 12f 는 12e(출처 게이트) 직후·13(카디널리티 제약 생성) 전 —
    # domain·출처 없는 껍데기 DP 를 먼저 치워야 13 이 그 DP 에 필수
    # 제약을 걸지 않는다.
    step_12f_orphan_dp_prune.apply,
    # 12i 는 12g(출처 정리)·12f(껍데기 정리) 뒤 — 정리된 (DP, 컬럼) 쌍에만
    # 값 검사를 돌려야 곧 사라질 DP 를 위반으로 세지 않는다. 그리고 13
    # (someValuesFrom 필수 제약) **앞** 이어야 한다: range 가 값과 어긋난 DP 에
    # 필수 제약을 걸면 A-Box 가 그 트리플을 버리므로 만족 불가능한 공리가 된다.
    step_12i_dp_range_csv_reality.apply,
    # 12j 는 28(disjointUnionOf 승격) **앞** 이어야 한다 — 28 의 멱등 가드는
    # "parent 에 disjointUnionOf 가 이미 있으면 skip" 인데, 오염된 리터럴도 그
    # 판정에 걸린다. 먼저 정리해야 28 이 정상 리스트를 만들 수 있다.
    step_12j_collection_literal_cleanup.apply,
    step_13_pk_functional_someValuesFrom.apply,
    # 13b 는 13 직후 — 13 이 만든/보존한 카디널리티 제약까지 데이터 충진율로
    # 검증해야 한다. 20(restriction dedup) 전에 끝내야 지운 제약이 되살아나지 않는다.
    step_13b_cardinality_data_reality.apply,
    # 13c 는 13b 직후 — 13b 가 **minCardinality + DP** 만 보므로 **maxCardinality +
    # OP** 축은 어떤 스텝도 데이터와 대조하지 않았다 (2026-08-30: 이번 S2 가 만든
    # maxCardinality 32개 중 1개가 FK 방향과 반대였고, S4 는 A-Box 를 로드하지 않아
    # consistent:true 를 냈다). 20(restriction dedup) 앞이어야 지운 제약이 되살아나지
    # 않는 것도 13b 와 같다.
    step_13c_op_max_cardinality_direction.apply,
    # 13d 는 13c 직후 — **OP** 의 owl:FunctionalProperty 선언을 tacit 실측 팬아웃과
    # 대조한다. 13 은 DP 에만 functional 을 붙이고 13b·13c 는 cardinality restriction
    # 축만 보므로, S2 가 선언한 OP-functional 47개를 **어떤 스텝도 데이터와 대조하지
    # 않았다** (2026-09-05: hasStackEquipment 가 tacit 팬아웃 8 인데 functional 로
    # 선언돼 주어 1,560개 전부 위반).
    step_13d_op_functional_fanout_audit.apply,
    # 13e 는 13(someValuesFrom 생성) 뒤·20(restriction dedup) 앞 — 13 의 근거 판정은
    # 자기 생성분만 지키므로 S2 가 들여온 공리는 무검증이었다 (2026-09-05: 필수참여
    # 위반 3쌍 중 근거 없는 2건이 S2 출력 스냅샷에 이미 존재). 20 앞이어야 지운 공리가
    # 되살아나지 않는 것은 13b·13c 와 같은 이유다.
    step_13e_existential_axiom_audit.apply,
    step_14_subgroup_creation.apply,
    # 14b 는 12/14 가 계층을 다 세운 **뒤** — 그 스텝들의 가드는 자기가 추가하는
    # 부모만 보므로, S2 가 이미 만들어 둔 disjoint 충돌은 아무도 정리하지 않는다.
    # 계층이 확정된 시점에 남은 충돌이 곧 HermiT 의 unsat 이다 (2026-08-19 실측:
    # 직교 축을 섞은 그룹 하나가 unsat 6건을 만들었다).
    step_14b_disjoint_axis_split.apply,
    step_15_cross_domain_ops.apply,
    step_15b_fk_op_autocreate.apply,
    # 15c 는 15/15b 가 만들 것을 다 만든 **뒤** 여야 한다 — 그 스텝들이 생성한
    # 이름과 A-Box 가 쓰는 이름이 겹치면 주입할 필요가 없다. 중복 게이트(22d)
    # 앞이어야 새로 선언된 OP 가 집계에 반영된다.
    step_15c_undeclared_op_backfill.apply,
    # 15d 는 15/15b/15c 가 OP 를 다 만든 **뒤** — 그 스텝들이 만든 OP 도 출처
    # 역기록 대상이다. 앞에 두면 방금 생성된 OP 를 놓쳐 근거율이 낮게 남는다
    # (DP 쪽 step_12d3 를 게이트 앞에 둔 것과 같은 이유).
    step_15d_op_source_backfill.apply,
    step_16_scope_emission.apply,
]

# Phase 2~3: 본문 step 17~29 가 마지막에 실행되므로 _POST_STEPS 에 등록.
# 순서는 본문과 동일 (17 → 18 → 19 → 20 → 21 → 22 → 22b → ... → 29).
# Step 30: graph 로드 직후 수동 추가분(rules/domain/tbox_manual_additions.ttl) 병합 —
# 고립 클래스 OP + 차원 클래스/OP/DP 를 복원해, 이후 후처리 스텝이 차원까지 처리.
_PRE_STEPS: list[StepFn] = [
    step_30_tbox_manual_additions.apply,
]
_POST_STEPS: list[StepFn] = [
    step_17_label_synthesis.apply,
    step_18_completeness.apply,
    step_19_bnode_skolemize.apply,
    # 19b 는 19(스콜렘화) 직후·20(dedup) 직전 — 여러 클래스가 공유하던
    # restriction 을 클래스별로 분리한다. 20 을 먼저 돌리면 의미가 같은
    # restriction 을 다시 합쳐 분리가 상쇄된다.
    step_19b_restriction_owner_split.apply,
    step_20_restriction_dedup.apply,
    step_21_over_engineered_dep.apply,
    # 21b 는 21 직후 — 21 이 deprecated 마킹을 끝낸 뒤에 봐야 "참조가 없다" 판정이
    # 확정된다. 계층 생성(12/14)·OP 정리(22 계열)보다 뒤라 그것들이 붙인 구조 신호도
    # 모두 반영된 상태를 본다.
    step_21b_dead_stub_prune.apply,
    # 21c 는 21/21b 직후 — 마킹이 끝난 뒤 봐야 이번 실행이 붙인 것까지 포함해
    # 판정이 확정된다. 생성 지점 가드(420b250)는 신규 쓰기만 막으므로, 이미
    # 파일에 박힌 익명 표현식의 deprecated 는 이 스텝만 걷어낼 수 있다 —
    # 그 잔재 1건이 check_quality_rules 를 baseline FAIL 로 고정시켜
    # mutation 검출률을 14.3% 로 떨어뜨렸다 (2026-08-19 실측).
    step_21c_anon_expr_deprecated_cleanup.apply,
    step_22_dup_op.apply,
    # 22g 는 22 직후 — 22 가 만드는 방향 소멸 링크를 정리한다. 22 의 가드는 신규
    # 추가만 막으므로 이미 파일에 박힌 링크는 이 스텝만 걷어낼 수 있다. 22b(순환)
    # 보다 앞에 두는 이유: 방향 소멸 링크를 먼저 걷어내면 22b 가 볼 SCC 가 줄어든다.
    step_22g_direction_collapse.apply,
    step_22b_subprop_cycle.apply,
    step_22c_subclass_cycle.apply,
    step_22e_duplicate_op_prune.apply,
    # 22d 도 22e **뒤** 여야 한다 — 22f 와 **같은 결함**이었다 (2026-08-30 2차 규명).
    #
    # 22d 는 read-only 게이트인데 22e 앞에 있었다. 22e 는 중복 OP 를 **삭제**하므로
    # (.env 의 ``TBOX_DUP_OP_PRUNE=on``) 22d 가 세던 것은 배포되지 않는 중간 상태다.
    # 08-19 산출물 실측:
    #
    #   22d 등록 위치 (측정)  : 그룹 31 / 중복 OP 35   ← 게이트가 본 것
    #   22e 후 (배포본)        : 그룹  0 / 중복 OP  0   ← 실제 산출물
    #
    # ``TBOX_DUP_OP_GATE=fail`` 이면 **한 줄 뒤에 지워질 중복** 때문에 S3 가
    # RuntimeError 로 중단된다 (``run_step_pipeline`` 이 전파한다). 이번 세대
    # T-Box 는 22d/22e 모두 0건이라 현재 실손실은 없지만, 위치 자체가 틀렸으므로
    # 22f 와 같이 고친다 — 다음 S2 가 중복을 다시 만들면 재발한다.
    step_22d_duplicate_op_gate.apply,
    # 22f 는 22e(중복 제거) **뒤** 여야 한다 — 2026-08-30 규명.
    #
    # 이전에는 22e 앞이었고 주석은 "OP 가 모두 만들어진 뒤에 근거를 세야 한다" 고
    # 적혀 있었다. 그런데 22e 는 OP 를 **삭제**한다 (중복 정본 하나만 남긴다).
    # 그래서 게이트가 세는 그래프는 **배포되지 않는 중간 상태** 였다:
    #
    #   22f 시점 (측정)   : OP 158 / 무근거 41  ← 게이트가 본 것
    #   22e 후 (배포본)    : OP 156 / 무근거 39  ← 실제 산출물
    #
    # 그 2건 차이가 임계(40)를 넘겨 ``test_pipeline_output_stays_within_the_baseline``
    # 이 선재 실패였다. 즉 게이트는 자기가 막으려는 대상(배포 T-Box)이 아니라
    # 그 전 단계를 재고 있었다 — 이 리포의 "guard 가 다른 단계를 비교했다" 와
    # 같은 형태다.
    #
    # 22e 뒤로 옮기면 "OP 가 모두 만들어진 뒤" 조건도 그대로 만족한다 (22e 는
    # 만들지 않고 지우기만 한다).
    step_22f_op_grounding_gate.apply,
    step_23_dup_dp.apply,
    step_23c_cross_domain_subprop.apply,
    step_24_abstract_category.apply,
    step_24b_odp_auto_apply.apply,
    # 24c 는 계층을 만드는 스텝(12/14/22c/24/24b) **전부 뒤** 여야 한다 — 그것들이
    # 부모를 추가하므로 앞에 두면 방금 만들어질 참조를 못 본다. 그리고 25(OntoClean)
    # **앞** 이어야 한다: 25 는 부모의 메타속성을 읽어 C2/C3 를 판정하므로 유령
    # 부모가 남아 있으면 실재하지 않는 노드를 기준으로 위반을 세거나 놓친다.
    step_24c_undeclared_schema_ref_prune.apply,
    step_25_ontoclean.apply,
    step_26_module_membership.apply,
    step_27_owl_haskey.apply,
    step_28_disjoint_union.apply,
    step_29_property_chain.apply,
    step_31_code_meaning_comment.apply,
    # 31b 는 파이프라인 **맨 끝** — 모든 정리·생성이 끝난 T-Box 를 봐야 한다.
    # 중간에 두면 아직 만들어지지 않은 정의 공리를 '없음' 으로 보고한다.
    step_31b_unfillable_class_audit.apply,
]


# ───────────────────────────────────────────────────────────────────
# Phase 8: list-driven pipeline runner
# ───────────────────────────────────────────────────────────────────

_logger = logging.getLogger(__name__)


def _accumulate_redundant(stats: dict, result_stats: dict) -> None:
    """`_step{N}_redundant_delta` prefix 키를 antipattern_redundant_removed 로 누적.

    step 12/14 가 명시적으로 반환하던 누적 패턴을 helper 가 자동 처리한다.
    원본 키는 stats 에 남기지 않는다 (본문과 mechanical equivalence).
    """
    delta_keys = [k for k in result_stats
                  if k.startswith("_step") and k.endswith("_redundant_delta")]
    for k in delta_keys:
        delta = result_stats.pop(k, 0)
        stats["antipattern_redundant_removed"] = (
            stats.get("antipattern_redundant_removed", 0) + delta
        )


def run_step_pipeline(
    steps: list[StepFn],
    g: Graph,
    ctx: StepContext,
    stats: dict[str, object],
    change_log: list[dict[str, object]],
) -> None:
    """주어진 step 리스트를 순서대로 실행하며 stats / change_log 를 누적.

    각 step 은 자체 ``StepResult`` 를 반환하고, 본 helper 가 다음을 처리:
      - ``stats.update(result.stats)`` — 단, ``_step{N}_redundant_delta`` 는
        ``antipattern_redundant_removed`` 로 누적 후 제거.
      - ``change_log.append({...})`` — 본문과 동일한 5-key 구조.
      - step 실패 시 logger.error 후 다음 step 으로 진행 (본문 정책 보존).

    예외 정책:
      - ``RuntimeError`` 는 호출자에게 **전파** — step 12c coverage gate 처럼
        의도적 contract violation 신호 (TBOX_COVERAGE_GATE=fail 등) 보존.
      - 그 외 모든 ``Exception`` 은 logger.error 후 skip — Phase 8 이전 inline
        본문에는 없던 안전망이지만 step 모듈의 우발 버그가 파이프라인 전체를
        무너뜨리지 않게 한다.
    """
    for step_fn in steps:
        before = len(g)
        try:
            result = step_fn(g, ctx)
        except RuntimeError:
            raise
        except Exception as step_err:
            _logger.error(
                "Step %s 실패 (skip): %s",
                getattr(step_fn, "__module__", "?"),
                step_err,
            )
            continue

        _accumulate_redundant(stats, result.stats)
        stats.update(result.stats)

        step_key = (
            result.step_number if result.step_number is not None
            else result.name
        )
        label = (
            result.step_label if result.step_label is not None
            else result.name
        )
        change_log.append({
            "step": step_key,
            "name": label,
            "triples_before": before,
            "triples_after": len(g),
            "delta": result.triples_delta,
        })
        if result.error:
            _logger.warning("Step %s warn: %s", result.name, result.error)


def run_step_pipeline_grouped(
    steps: list[StepFn],
    g: Graph,
    ctx: StepContext,
    stats: dict[str, object],
    change_log: list[dict[str, object]],
    group_step: int | str,
    group_label: str,
) -> None:
    """여러 step 을 단일 묶음 change_log entry 로 기록 (Step 9 series 전용).

    각 step 의 stats 는 정상 누적되지만, change_log 에는 group 단위 1건만
    추가된다. ``triples_before`` / ``triples_after`` / ``delta`` 는 group
    전체의 변화량으로 계산.

    예외 정책: ``run_step_pipeline`` 과 동일 — ``RuntimeError`` 는 전파,
    그 외는 skip. 단, group entry 는 RuntimeError 전파 시 기록되지 않는다
    (의도된 abort).
    """
    group_before = len(g)
    for step_fn in steps:
        try:
            result = step_fn(g, ctx)
        except RuntimeError:
            raise
        except Exception as step_err:
            _logger.error(
                "Step %s 실패 (skip): %s",
                getattr(step_fn, "__module__", "?"),
                step_err,
            )
            continue
        _accumulate_redundant(stats, result.stats)
        stats.update(result.stats)
        if result.error:
            _logger.warning("Step %s warn: %s", result.name, result.error)

    change_log.append({
        "step": group_step,
        "name": group_label,
        "triples_before": group_before,
        "triples_after": len(g),
        "delta": len(g) - group_before,
    })


__all__ = [
    "StepContext", "StepResult", "StepFn",
    "_PRE_STEPS", "_POST_STEPS",
    "_MAIN_PRE_STEP9", "_STEP9_GROUP", "_MAIN_POST_STEP9",
    "run_step_pipeline", "run_step_pipeline_grouped",
]
