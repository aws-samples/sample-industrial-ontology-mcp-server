"""Step 22f — ObjectProperty **근거(grounding)** 게이트 (read-only 측정).

## 왜 필요한가 — DP 는 4중 방어, OP 는 0중이었다

DP 는 "CSV 컬럼과 연결됐는가" 를 강제·역기록·게이팅·딕셔너리 표시까지 네 겹으로
확인한다. OP 에는 그 넷이 **하나도 없었다**:

  | | DP | OP |
  |-|----|----|
  | ``dcterms:source`` 보유율 | 86.6% | 15.3% |
  | A-Box 사용률 | 68% | 6% |
  | 역기록 스텝 | ``step_12d3`` | 없음 |
  | 커버리지 게이트 | ``step_12e`` | (``fk_op_coverage`` 가 깨져 있었다) |

그래서 S2 가 도메인 지식으로 **상상한** 관계가 CSV FK 대조 없이 그대로 배포됐다.
실측 (2026-08-12): CSV FK 로 표현 가능한 ``(source, target)`` 쌍은 **46개** 인데
OP 를 **229개** 선언하고 A-Box 가 쓰는 것은 **14개** 였다. S2 작성 160개 중 4개(2%)
만 쓰이는데 DP 는 같은 산출물에서 68% 가 쓰인다 — OP 축만 무너져 있었다.

원인은 프롬프트 구조다: ``04-property-rules.md`` 의 원칙 2("도메인 관계 추론")가
CSV FK 없이 24개 관계 쌍을 열거하고, 원칙 5가 **모든** OP 에 역방향을 요구해 개수를
2배로 만든다 (준수율 89%).

## 이 스텝은 삭제하지 않는다 — 표시하고 센다

값 0건 OP 를 지우는 것은 위험하다: tacit 이 쓰거나, ``owl:Restriction`` 의
``onProperty`` 대상이거나, 미래 CSV 가 채울 수 있다. 실측으로 근거를 네 갈래로
나눠 세고, **어떤 근거도 없는 것** 만 보고한다:

  G1 A-Box·master·tacit 이 실제로 쓴다        (가장 강한 근거)
  G2 ``owl:Restriction`` 의 ``onProperty`` 대상
  G3 ``rules/*.json`` 설정이 이름을 언급
  G4 값 있는 OP 의 ``owl:inverseOf`` / ``owl:equivalentProperty`` 짝

실측 (step_15c 적용 후 238개): G1 51 / G2 182 / G3 35 → **근거 없음 39개**.

## 이 스텝은 22e **뒤** 에 있어야 한다 (2026-08-30 규명)

이전에는 ``step_22e_duplicate_op_prune`` **앞** 이었고 주석은 "OP 가 모두 만들어진
뒤에 근거를 세야 한다" 고 적혀 있었다. 그런데 22e 는 OP 를 **삭제**한다 (중복 그룹의
정본 하나만 남긴다). 그래서 이 게이트가 세는 그래프는 **배포되지 않는 중간 상태**
였다:

======================= ============ ==============
시점                     OP 개수       무근거
======================= ============ ==============
22f (측정, 22e 앞)        158          **41**
22e 후 (실제 배포본)       156          **39**
======================= ============ ==============

그 2건 차이가 임계(40)를 넘겨 ``test_op_grounding_gate`` 의 baseline 테스트가
**선재 실패** 였다. 게이트가 자기가 막으려는 대상(배포 T-Box)이 아니라 그 전 단계를
재고 있었으므로, 그 실패는 품질 악화가 아니라 **측정 지점 오류**였다 — 이 리포의
"guard 가 다른 단계를 비교했다" 와 같은 형태다.

같은 일이 다시 생기지 않도록 ``op_grounding_measured_after_prune`` 을 stats 에
남긴다: 뒤에 OP 를 지우는 스텝이 또 들어오면 이 값이 ``False`` 가 되어 드러난다.

환경변수:
  - ``TBOX_OP_GROUNDING_GATE``: ``warn`` (default) | ``fail``
  - ``TBOX_OP_GROUNDING_MAX``: 허용 무근거 OP 수 (default 40 — 현 실측 39 를
    기준선으로 두어 **악화만** 잡는다. 줄이려면 프롬프트를 고쳐야 한다)

기본이 ``warn`` 이고 임계치가 현 수준인 이유: 무근거 OP 는 프롬프트가 만든 것이라
S3 가 고칠 수 없다. 게이트의 목적은 **지금보다 나빠지는 것을 막고 수치를 드러내는
것** 이다. 임계치를 0 으로 두면 매 실행 FAIL 이라 아무도 보지 않게 된다.
"""
from __future__ import annotations

import json
import logging
import os

from rdflib import OWL, RDF, Graph, URIRef

from config import ABOX_PATH
from domain.rules_paths import rules_json_glob
from tools.quality_steps._base import (
    OP_REMOVING_STEPS,
    StepContext,
    StepResult,
    is_registered_after,
)

logger = logging.getLogger(__name__)

_DEFAULT_MAX_UNGROUNDED = 40


#: 하위호환 별칭 — 정본은 ``_base.OP_REMOVING_STEPS`` 다. 목록을 모듈별로 복사하면
#: 갱신을 빠뜨린다 (2026-08-30: 22f 사본만 고치고 22d 를 놓쳤다).
_OP_REMOVING_STEPS = OP_REMOVING_STEPS


def _is_after_op_removing_steps() -> bool:
    """이 스텝이 OP 삭제 스텝들보다 **뒤** 에 등록됐는가.

    판정 로직은 ``_base.is_registered_after`` 에 있다 — 2026-08-30 에 같은 결함이
    ``step_22d`` 에도 있음이 드러났고, 여기 사본을 둔 탓에 그것을 놓쳤다. 판정기를
    한 곳에 두면 새 게이트도 같은 계약을 쓴다.
    """
    return is_registered_after(
        "step_22f_op_grounding_gate", *_OP_REMOVING_STEPS,
    )


def _local(node) -> str:
    return str(node).split("#")[-1].split("/")[-1]


def _config_mentioned_names() -> set[str]:
    """``rules/*.json`` 이 언급하는 이름 집합.

    설정이 이름을 적었다면 SME 가 의도한 관계다 — 데이터가 아직 없어도 근거로 본다.
    JSON 구조를 몰라도 되도록 문자열 전수를 훑는다 (과다 인정 쪽으로 안전).
    """
    names: set[str] = set()
    for path in rules_json_glob():
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
        except Exception as exc:  # noqa: BLE001 — 설정 하나가 깨져도 계속
            logger.debug("step_22f: %s 로드 skip: %s", path, exc)
            continue
        _collect_strings(raw, names)
    return names


def _collect_strings(node, out: set[str]) -> None:
    if isinstance(node, str):
        out.add(node)
    elif isinstance(node, dict):
        for k, v in node.items():
            out.add(k)
            _collect_strings(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_strings(v, out)


def apply(g: Graph, ctx: StepContext) -> StepResult:
    ns = ctx.domain_ns
    ops = {
        _local(o) for o in g.subjects(RDF.type, OWL.ObjectProperty)
        if isinstance(o, URIRef) and str(o).startswith(ns)
    }
    if not ops:
        return StepResult(
            name="step_22f_op_grounding_gate",
            # 측정 지점 자기점검은 OP 유무와 무관한 **배선** 사실이므로 이 경로에도
            # 넣는다. 한쪽에만 있으면 소비자가 키 부재를 "정상" 으로 오독한다
            # (이 리포의 "필드 부재 ≠ 값 0" 과 같은 함정).
            stats={
                "op_grounding_checked": 0,
                "op_grounding_measured_after_prune": _is_after_op_removing_steps(),
                # 신호 품질도 **배선** 사실이므로 이 경로에 넣는다 (같은 이유).
                "op_grounding_abox_file_present": os.path.exists(ABOX_PATH),
            },
            triples_delta=0,
            step_number="22f", step_label="op_grounding_gate",
        )

    # G1 — A-Box·master·tacit 사용. 판정 불가면 ``None`` 이 오는데, 그때는 이 신호를
    # 쓰지 않는다 (게이트가 read-only 라 fail-closed 가 아니라 신호 생략이 맞다).
    from domain.graph_utils import abox_used_local_names

    used = abox_used_local_names(ops)
    signal_unavailable = used is None
    used = used or set()

    # A-Box 파일 자체가 있는가 — ``signal_unavailable`` 만으로는 세 상태가 구분되지
    # 않는다 (2026-08-30 실측): 정상 / A-Box 파일 부재이지만 tacit 이 있어 부분 신호 /
    # 신호 함수가 None. 파일이 없으면 ``abox_used_local_names`` 가 tacit 만 훑은
    # 집합을 반환하므로 ``signal_unavailable=False`` 인데도 근거가 얇다 — 그때
    # ``op_ungrounded`` 가 12 → 50 으로 튀는데 원인이 각인되지 않았다.
    #
    # S3 는 S7 **앞** 이므로 이 신호는 원래 이전 세대 A-Box 를 읽는다. 배포 산출물로
    # 대조한 결과 실손실은 0 이었지만 (12f/21b candidates=0), 신호가 **비어 있으면**
    # 위험하다: 강제로 공집합을 주면 op_ungrounded 가 58 로 올라 임계를 넘긴다.
    # 그래서 "세대 차이" 가 아니라 "신호 부재" 를 드러내는 필드를 남긴다.
    abox_file_present = os.path.exists(ABOX_PATH)

    # G2 — Restriction 의 onProperty 대상
    on_property = {
        _local(o) for _, _, o in g.triples((None, OWL.onProperty, None))
        if isinstance(o, URIRef)
    }
    # G3 — 설정 언급
    configured = _config_mentioned_names()
    # G4 — 값 있는 OP 의 inverseOf / equivalentProperty 짝
    paired: set[str] = set()
    for pred in (OWL.inverseOf, OWL.equivalentProperty):
        for subj, _, obj in g.triples((None, pred, None)):
            if not (isinstance(subj, URIRef) and isinstance(obj, URIRef)):
                continue
            a, b = _local(subj), _local(obj)
            if a in used:
                paired.add(b)
            if b in used:
                paired.add(a)

    grounded_by = {
        "abox_or_tacit": sorted(ops & used),
        "restriction_on_property": sorted(ops & on_property),
        "config_declared": sorted(ops & configured),
        "paired_with_used": sorted(ops & paired),
    }
    ungrounded = sorted(ops - used - on_property - configured - paired)

    mode = (os.getenv("TBOX_OP_GROUNDING_GATE") or "warn").strip().lower()
    try:
        max_ungrounded = int(
            os.getenv("TBOX_OP_GROUNDING_MAX") or _DEFAULT_MAX_UNGROUNDED,
        )
    except ValueError:
        max_ungrounded = _DEFAULT_MAX_UNGROUNDED

    passed = len(ungrounded) <= max_ungrounded
    stats: dict = {
        "op_grounding_checked": len(ops),
        "op_grounded_abox": len(grounded_by["abox_or_tacit"]),
        "op_grounded_restriction": len(grounded_by["restriction_on_property"]),
        "op_grounded_config": len(grounded_by["config_declared"]),
        "op_grounded_paired": len(grounded_by["paired_with_used"]),
        "op_ungrounded": len(ungrounded),
        "op_ungrounded_names": ungrounded[:20],
        "op_grounding_gate_mode": mode,
        "op_grounding_gate_max": max_ungrounded,
        "op_grounding_gate_passed": passed,
        "op_grounding_abox_signal_unavailable": signal_unavailable,
        # 근거 신호의 **품질** 을 따로 드러낸다 (위 abox_file_present 주석 참조).
        # signal_unavailable 만으로는 "정상" 과 "파일 없이 tacit 만" 이 같은 값이다.
        "op_grounding_abox_file_present": abox_file_present,
        # 이 게이트가 **배포되는 그래프** 를 재고 있는가. 뒤에 OP 를 지우는 스텝이
        # 들어오면 False 가 되어 측정 지점 오류가 드러난다 (모듈 docstring 참조).
        "op_grounding_measured_after_prune": _is_after_op_removing_steps(),
    }

    if not stats["op_grounding_measured_after_prune"]:
        logger.warning(
            "Step 22f: 이 게이트 **뒤** 에 OP 를 삭제하는 스텝이 있다 — 지금 세는 "
            "수치(%d)는 배포되는 T-Box 가 아니라 중간 상태다. 같은 "
            "배치로 baseline 이 2건 초과해 선재 실패였다. 등록 순서를 22e 뒤로 "
            "되돌려라 (tools/quality_steps/__init__.py).",
            len(ungrounded),
        )

    if not passed:
        message = (
            f"근거 없는 ObjectProperty {len(ungrounded)}개 (허용 {max_ungrounded}) — "
            f"A-Box·tacit 사용 {len(grounded_by['abox_or_tacit'])} / Restriction "
            f"{len(grounded_by['restriction_on_property'])} / 설정 "
            f"{len(grounded_by['config_declared'])} 중 어느 근거도 없다. "
            f"예: {ungrounded[:5]}. "
            "CSV FK 로 채울 경로가 없는 관계는 값 0건으로 남아 질의가 0행을 "
            "정답처럼 반환한다 — prompts/tbox-prompt-modules/04-property-rules.md "
            "원칙 2·5 가 CSV FK 대조 없이 관계를 요구하는 것이 근본 원인이다."
        )
        if mode == "fail":
            logger.error("Step 22f OP grounding gate FAIL: %s", message)
            raise RuntimeError(message)
        logger.warning("Step 22f OP grounding gate WARN: %s", message)
    elif signal_unavailable:
        logger.info(
            "Step 22f: A-Box 사용 판정 불가 — 근거 신호 3개(Restriction/설정/짝)로만 "
            "측정했다 (무근거 %d개)", len(ungrounded),
        )

    return StepResult(
        name="step_22f_op_grounding_gate",
        stats=stats,
        triples_delta=0,          # read-only
        step_number="22f",
        step_label="op_grounding_gate",
    )
