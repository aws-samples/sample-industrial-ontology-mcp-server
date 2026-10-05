"""Regression: 리뷰어가 S3 자동화 항목으로 토론을 정체시키지 않는가.

2026-08-19 실측 (S2 58.5분): 이슈 수가 12→14→12→14 로 **전혀 수렴하지 않았고**,
veto lock 8개 target 중 **7개가 S3 (improve_tbox_quality) 가 자동으로 하는 일**
이었다. Architect 는 S2 단계에서 CSV FK 스캔·config 참조 같은 결정적 로직이 없어
그것을 잘 못 하고, 리뷰어는 매 라운드 같은 지적을 반복했다.

그 사이 **진짜 모델링 오류는 아무도 지적하지 않았다** — 직교하는 두 분류 축을 한
``AllDisjointClasses`` 에 섞어 6개 클래스가 unsatisfiable 이 됐는데, 리뷰어 4라운드
동안 언급이 0건이었다.

**이 파일이 주장하는 것**: 프롬프트가 (1) 침묵을 지시하지 않고 severity 하향만
지시하는가, (2) S3 자동화가 **없는** 항목을 자동화 대상으로 잘못 알리지 않는가,
(3) 양쪽 리뷰어에 동일 블록이 주입되는가.

블록을 "지적 금지" 로 만들면 S3 실패 시 아무도 모른다 — 이 리포에서 "게이트가
조용해서 결함이 덮인" 사고가 반복됐다.
"""

import pytest

from tools.multi_agent_prompts import (
    _S3_AUTOMATED_ITEMS,
    s3_deferred_block,
    sme_static_prefix,
    validator_static_prefix,
)


def _sme() -> str:
    return sme_static_prefix(
        cq_text="CQ1: ...", csv_summary="- T1: a,b", cross_domain_block="   - x",
    )


# ── 배선: 양쪽 리뷰어가 같은 블록을 받는가 ───────────────────────────────

@pytest.mark.parametrize("prompt_name", ["validator", "sme"])
def test_block_injected_into_both_reviewers(prompt_name):
    """Validator/SME 모두에 주입된다 — 한쪽만 알면 다른 쪽이 계속 정체시킨다."""
    prompt = validator_static_prefix() if prompt_name == "validator" else _sme()
    block = s3_deferred_block()
    # 블록의 제목 줄이 실제로 렌더링돼야 한다 (함수만 만들고 주입을 빠뜨리는 회귀).
    head = block.splitlines()[0]
    assert head in prompt, f"{prompt_name} 프롬프트에 블록이 주입되지 않았다"


def test_single_source_no_copy():
    """블록은 한 곳에서만 생성된다 (사본이 갈라지면 한쪽만 낡는다)."""
    import inspect

    import tools.multi_agent_prompts as mp

    src = inspect.getsource(mp)
    # 정의 1 + 호출 2(validator/sme) 이상
    assert src.count("s3_deferred_block()") >= 3, (
        "블록이 두 프롬프트에서 공유되지 않는다"
    )


# ── 침묵시키지 않는가 (이 블록의 최대 리스크) ───────────────────────────

def test_instructs_to_report_not_to_stay_silent():
    """"보고하라" 를 명시한다 — 침묵 지시가 되면 S3 실패를 아무도 모른다."""
    block = s3_deferred_block()
    assert "보고하라" in block
    assert "침묵하지 마라" in block


def test_does_not_forbid_reporting():
    """"지적하지 마라" 류의 금지 표현이 없다."""
    block = s3_deferred_block()
    for forbidden in ("지적하지 마", "보고하지 마", "무시하라", "언급하지 마"):
        assert forbidden not in block, (
            f"침묵 지시가 들어있다: {forbidden!r} — S3 실패가 조용해진다"
        )


def test_lowers_severity_instead_of_hiding():
    """severity 하향 + deferred 태깅을 지시한다 (기록은 남긴다)."""
    block = s3_deferred_block()
    assert "deferred_to_s3" in block, "deferred 태깅 지시가 없다"
    assert "medium" in block, "severity 하향 기준이 없다"


def test_blocks_approval_veto_on_deferred_items_only():
    """deferred 항목**만으로** approved=false 를 내지 말라고 지시한다."""
    block = s3_deferred_block()
    assert "approved: false" in block
    assert "이 항목만으로" in block, (
        "다른 항목까지 승인 거부를 막는 것으로 읽힐 수 있다"
    )


# ── 정확성: 없는 자동화를 있다고 알리지 않는가 ──────────────────────────

def test_inverse_functional_not_claimed_as_automated():
    """``InverseFunctionalProperty`` 는 S3 스텝이 **없다** — 자동화 목록에 없어야.

    실측 veto target 8개 중 이 하나만 자동화 대상이 아니다. 목록에 넣으면 리뷰어가
    severity 를 낮추고, 아무도 고치지 않는 항목이 조용히 남는다.
    """
    joined = " ".join(_S3_AUTOMATED_ITEMS)
    assert "InverseFunctional" not in joined, (
        "S3 스텝이 없는 항목을 자동화 대상으로 알리고 있다"
    )
    # 그리고 예외임을 리뷰어에게 명시해야 한다.
    assert "InverseFunctionalProperty" in s3_deferred_block()


@pytest.mark.parametrize("item_kw,step", [
    ("inverseOf", "step_02_inverse_bidirectional"),
    ("dcterms:source", "step_15d_op_source_backfill"),
    ("domain/range", "step_03_op_domain_default"),
    ("IOF/BFO", "step_12h_iof_category_conflict"),
    ("Restriction", "step_13_pk_functional_someValuesFrom"),
    ("RR", "step_15_cross_domain_ops"),
    ("DIT", "step_12_intermediate_abstract"),
])
def test_each_claimed_item_has_a_real_step(item_kw, step):
    """자동화 목록의 각 항목에 **실제 존재하는** 스텝 모듈이 대응한다.

    추측 매핑을 넣으면 리뷰어가 severity 를 낮췄는데 실제로는 아무도 안 고친다.
    """
    import importlib
    import os

    joined = " ".join(_S3_AUTOMATED_ITEMS)
    assert item_kw in joined, f"목록에 {item_kw} 항목이 없다 (테스트 전제 붕괴)"
    path = os.path.join("tools", "quality_steps", f"{step}.py")
    assert os.path.exists(path), f"{step} 모듈이 없다 — 잘못된 매핑"
    mod = importlib.import_module(f"tools.quality_steps.{step}")
    assert hasattr(mod, "apply"), f"{step} 이 apply() 를 노출하지 않는다"


def test_claimed_steps_are_registered_in_pipeline():
    """자동화 목록이 근거로 삼는 스텝들이 실제 파이프라인에 등록돼 있다.

    등록되지 않은 스텝을 근거로 severity 를 낮추면 그 항목은 영구히 미해결이다.
    """
    import tools.quality_steps as qs

    registered = set()
    for lst in (qs._PRE_STEPS, qs._MAIN_PRE_STEP9, qs._STEP9_GROUP,
                qs._MAIN_POST_STEP9, qs._POST_STEPS):
        for fn in lst:
            registered.add(getattr(fn, "__module__", "").split(".")[-1])
    for step in ("step_02_inverse_bidirectional", "step_15d_op_source_backfill",
                 "step_12_intermediate_abstract", "step_14_subgroup_creation",
                 "step_15_cross_domain_ops", "step_13_pk_functional_someValuesFrom",
                 "step_12h_iof_category_conflict"):
        assert step in registered, f"{step} 이 파이프라인에 등록되지 않았다"


# ── S2 고유 항목: 실제 사고를 지적하도록 유도하는가 ─────────────────────

def test_directs_reviewers_to_s2_only_concerns():
    """S2 만 판단할 수 있는 항목으로 주의를 돌린다."""
    block = s3_deferred_block()
    for kw in ("클래스 경계", "분류 축의 직교성", "CQ 답변 가능성", "FK 해석"):
        assert kw in block, f"S2 고유 관점 누락: {kw}"


def test_warns_about_orthogonal_axis_disjoint_defect():
    """실제로 놓친 결함(직교 축 disjoint → unsat)을 명시적으로 경고한다.

    이 블록의 목적은 "잔소리를 줄인다" 가 아니라 **놓친 결함을 잡게 한다** 다.
    """
    block = s3_deferred_block()
    assert "unsatisfiable" in block
    assert "AllDisjointClasses" in block
    assert "A-Box" in block, "결함의 실제 영향(인스턴스 생성 불가)이 없다"


# ── 비용: 캐시 대상 prefix 를 과도하게 늘리지 않는가 ────────────────────

def test_block_size_is_bounded():
    """블록이 과도하게 커지지 않는다 (prefix 는 라운드마다 전송된다).

    캐시 대상이라 재청구는 없지만, 리뷰어가 카탈로그에 집중하고 본 책무를 놓치는
    회귀가 이 리포에 있었다 (R28 D: Jury 카탈로그 2K 토큰 → 빈 배열 반환).
    """
    block = s3_deferred_block()
    assert len(block) < 4000, (
        f"블록이 {len(block)}자 — 리뷰어가 본 검토보다 이 목록에 집중할 수 있다"
    )
