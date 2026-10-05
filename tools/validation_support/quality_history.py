"""Quality score history — append / parse / regression.

## 이 파일은 **두 종류의 항목** 이 섞인 파일을 읽는다 (2026-08-30 규명)

``quality_history.json`` 을 두 writer 가 공유한다:

============================================ ==================================
writer                                        항목 형태
============================================ ==================================
``append_quality_history`` (이 파일)           ``{"score": "19/25", "failed_checks": [...]}``
``pipeline_state._append_quality_history``     ``{"step": "S9_...", "metrics": {...}}``
============================================ ==================================

``detect_regression`` 은 ``history[-1]`` vs ``history[-2]`` 를 **위치** 로 비교하면서
``.get("score", "0/0")`` 으로 폴백했다. 그래서 사이에 ``save_step`` 항목이 끼면
점수가 **0/0 으로 파싱**돼 비교가 무너졌다. 배포 파일 실측 (42항목 = 검증형 23 +
save_step 형 19, 검증형 10건이 save_step 항목을 직전 이웃으로 가짐):

  · ``current_score`` 가 **빈 문자열** 인 유령 회귀 **9건** 을 만들고
  · 진짜 회귀 4건 중 가장 큰 낙폭 (``21/23 → 16/24``, 체크 5개가 한꺼번에 실패)을
    **놓쳤다**

검증형 항목만 비교하면 보고되지 않은 진짜 회귀 4건이 드러난다::

    22/23 → 20/23   (고아 노드, 스키마 참조 무결성)
    21/23 → 16/24   (domain/range, 미선언 DP, 미선언 OP …)
    20/24 → 19/24   (추론 sanity)
    20/25 → 18/25   (FK 참조 무결성, 추론 sanity)

그래서 ``detect_regression`` 은 **점수 항목만** 골라 비교한다. 파일을 분리하지
않는 이유: 두 writer 의 소비자가 각각 있고 (``get_quality_history`` 는 전체를
보여준다), 읽는 쪽에서 필터하면 기존 파일도 그대로 해석된다.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime

from config import GENERATED_ABOX_DIR

logger = logging.getLogger(__name__)


def parse_score(score_str: str) -> int:
    """'18/20' → 18. 파싱 실패 시 0."""
    try:
        return int(score_str.split("/")[0])
    except (ValueError, IndexError):
        return 0


def parse_total(score_str: str) -> int:
    """``'18/20'`` → 20. 파싱 실패 시 0."""
    try:
        return int(score_str.split("/")[1])
    except (ValueError, IndexError):
        return 0


def score_entries(history: list) -> list[dict]:
    """점수를 담은 항목만 — ``save_step`` 항목을 제외한다 (모듈 docstring 참조).

    ``score`` 키가 있고 그 값이 ``N/M`` 으로 파싱되는 항목만 남긴다. 키 존재만
    보면 빈 문자열·None 이 통과해 ``0/0`` 비교를 되살린다 (그것이 원래 결함이다).
    """
    out: list[dict] = []
    for entry in history:
        if not isinstance(entry, dict):
            continue
        raw = entry.get("score")
        if not isinstance(raw, str) or "/" not in raw:
            continue
        if parse_total(raw) <= 0:
            continue
        out.append(entry)
    return out


def detect_regression(history: list) -> dict | None:
    """직전 **검증 실행** 대비 점수 회귀 + 신규 실패 check 감지.

    **분모가 바뀌면 통과 수만으로 비교하지 않는다.** check 를 추가하면 전체 수가
    늘면서 통과 수가 그대로여도(또는 새 check 가 실패하면 줄어들어) 회귀로 오탐한다.
    실측 (2026-08-24): 23번째 check(스키마 참조 무결성) 추가 시 ``22/23`` 이 되는데,
    이전 기록이 ``21/22`` 라면 통과 수 비교로는 회귀가 아니지만 반대 방향의 오탐도
    쉽게 발생한다. 분모가 다르면 **실패 수** 로 비교한다 — 새 check 가 새 결함을
    드러낸 것과 기존 검증이 나빠진 것을 구분하려면 그것이 옳은 축이다.

    **비교 대상은 점수 항목만이다.** 같은 파일에 ``save_step`` 항목이 섞여 있고
    (모듈 docstring 참조) 예전에는 위치로 비교해 그것을 ``0/0`` 으로 읽었다 —
    유령 회귀 9건을 만들고 가장 큰 낙폭(``21/23 → 16/24``)을 놓쳤다.
    """
    scored = score_entries(history)
    if len(scored) < 2:
        return None
    current = scored[-1]
    previous = scored[-2]
    cur_score = parse_score(current.get("score", "0/0"))
    prev_score = parse_score(previous.get("score", "0/0"))
    cur_total = parse_total(current.get("score", "0/0"))
    prev_total = parse_total(previous.get("score", "0/0"))

    if cur_total and prev_total and cur_total != prev_total:
        # 분모 변경 — 실패 수로 판정한다.
        regressed = (cur_total - cur_score) > (prev_total - prev_score)
    else:
        regressed = cur_score < prev_score

    if regressed:
        cur_failures = set(current.get("failed_checks", []))
        prev_failures = set(previous.get("failed_checks", []))
        return {
            "regression": True,
            "current_score": current.get("score", ""),
            "previous_score": previous.get("score", ""),
            "delta": cur_score - prev_score,
            "new_failures": sorted(cur_failures - prev_failures),
        }
    return None


def append_quality_history(result: dict, *, base_dir: str | None = None) -> None:
    """검증 결과를 quality_history.json에 append (최근 100건 유지).

    Args:
        result: 검증 결과 dict.
        base_dir: history 저장 기준 디렉토리. None이면 GENERATED_ABOX_DIR의
            부모 사용. 테스트에서 주입 가능.
    """
    if base_dir is None:
        base_dir = os.path.dirname(GENERATED_ABOX_DIR)
    history_path = os.path.join(base_dir, "quality_history.json")
    history: list = []
    if os.path.exists(history_path):
        try:
            with open(history_path, encoding="utf-8") as f:
                history = json.load(f)
            if not isinstance(history, list):
                history = []
        except Exception:
            history = []

    entry = {
        "timestamp": datetime.now().isoformat(),
        "score": result.get("score", ""),
        "passed": result.get("passed", False),
        "triples": result.get("triples", 0),
        "source": result.get("source", ""),
        "failed_checks": [
            c["name"] for c in result.get("checks", []) if not c["passed"]
        ],
    }

    history.append(entry)
    if len(history) > 100:
        history = history[-100:]

    try:
        with open(history_path, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning("Quality history 저장 실패: %s", e)
