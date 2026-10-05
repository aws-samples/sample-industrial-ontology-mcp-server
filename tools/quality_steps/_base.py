"""improve_tbox 분할 — Step 공통 dataclass + 진입점 시그니처.

`improve_tbox` 의 43 step (0~29 + sub-step 09a/09a2/09b/.../15b/22b/24b) 을
모듈로 추출해 이 모듈의 StepContext / StepResult 를 공유하도록 한다. 각 step
은 ``apply(g, ctx) -> StepResult`` 시그니처를 따르며, 코디네이터
(`improve_tbox`) 의 list-driven pipeline (Phase 8) 이 5개 list (`_PRE_STEPS`,
`_MAIN_PRE_STEP9`, `_STEP9_GROUP`, `_MAIN_POST_STEP9`, `_POST_STEPS`) 의 step
을 순서대로 실행하면서 stats 를 누적한다.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rdflib import Graph


@dataclass
class StepContext:
    """모든 Step 이 공유하는 진행 컨텍스트.

    - ``domain_ns``: DOMAIN_NS 캐시 — Step 마다 import 회피
    - ``change_log``: 누적 change log (각 Step 의 delta 추적)
    - ``shared``: Step 간 데이터 통신 캐시 (예: pk_index, lonely_disjoint_count).
      대부분의 Step 은 graph 자체로 통신하지만, 일부 (Step 0a → 1, Step 13 → 27)
      는 명시적 통신이 필요. shared dict 에 step 별 키로 저장.
    """

    domain_ns: str
    change_log: list[dict] = field(default_factory=list)
    shared: dict[str, Any] = field(default_factory=dict)


@dataclass
class StepResult:
    """Step 실행 결과 — improve_tbox 의 stats dict 에 병합되는 단위.

    - ``name``: 모듈 식별자 (예: ``step_0d_transitive_inverse``).
    - ``stats``: 해당 step 이 측정한 카운터/플래그 (improve_tbox 의 stats 에 update)
    - ``triples_delta``: ``len(g)`` 변화량 (음수 가능 — step 1 처럼 재구축 시)
    - ``error``: 정책상 ``warn`` 으로 처리된 step 의 실패 메시지 (None 이면 정상)
    - ``step_number``: 본문 change_log 에 기록되던 정수/문자열 step 번호.
      tests 가 ``e["step"] >= 18`` 같은 비교를 하므로 본문과 동일 형식 보존.
    - ``step_label``: 본문 change_log 의 ``name`` 키 (예: ``bnode_skolemization``).
      tests 가 ``e["name"] == "bnode_skolemization"`` 으로 lookup.
    """

    name: str
    stats: dict[str, Any] = field(default_factory=dict)
    triples_delta: int = 0
    error: str | None = None
    step_number: int | str | None = None
    step_label: str | None = None


# Step 함수 시그니처: ``apply(g, ctx) -> StepResult``.
# Phase 2 부터 각 step 모듈이 이 signature 를 export.
StepFn = Callable[[Graph, StepContext], StepResult]


#: OP 를 **삭제**하는 스텝. OP 를 세는 read-only 게이트는 이들보다 뒤에 등록돼야
#: 배포본을 잰다. **목록을 모듈별로 복사하지 말 것** — 2026-08-30 에 22f 만 고치고
#: 22d 를 놓친 원인이 사본이었다. 새 삭제 스텝이 생기면 여기만 고친다.
OP_REMOVING_STEPS = (
    "step_22e_duplicate_op_prune",
    "step_21b_dead_stub_prune",
)


def is_registered_after(step_module: str, *earlier_modules: str) -> bool:
    """``step_module`` 이 ``earlier_modules`` 보다 **뒤** 에 등록됐는가.

    **왜 공용인가**: read-only 게이트는 자기가 판정하려는 **배포 산출물** 을 재야
    한다. 뒤에 삭제·정리 스텝이 있으면 배포되지 않는 중간 상태를 재게 되고, 그
    수치로 baseline 을 넘기거나 (``TBOX_*_GATE=fail`` 이면) 한 줄 뒤에 지워질 것
    때문에 S3 를 중단시킨다. 2026-08-30 에 같은 결함이 **두 게이트에서** 확인됐다:

    ===== ===================================== ==============================
    게이트  등록 위치에서 본 수치                    22e 후 (배포본)
    ===== ===================================== ==============================
    22f   무근거 OP 41 (baseline 40 초과)          39 → PASS
    22d   중복 그룹 31 / 중복 OP 35                0 / 0
    ===== ===================================== ==============================

    22f 를 고칠 때 사본을 만들었더니 22d 의 같은 결함을 놓쳤다. 판정기를 한 곳에
    두면 새 게이트도 같은 계약을 쓰고, 새 삭제 스텝이 생겼을 때 갱신 지점이 하나다.

    등록 순서를 **소스에서 읽어** 판정한다 — 목록을 손으로 적으면 순서를 바꿀 때
    빠뜨리고, 그 누락이 "배포본이 아닌 것을 재는" 상태로 조용히 돌아간다.

    판정 불가 시 ``True`` — 이 값은 진단이고, 읽기 실패로 게이트가 경고를 쏟으면
    실제 신호가 묻힌다.
    """
    try:
        import inspect

        from tools import quality_steps

        source = inspect.getsource(quality_steps)
        self_pos = source.index(f"{step_module}.apply")
    except Exception:  # noqa: BLE001
        return True
    for name in earlier_modules:
        pos = source.find(f"{name}.apply")
        if pos == -1:
            continue
        if pos > self_pos:
            return False
    return True
