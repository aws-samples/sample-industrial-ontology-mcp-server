"""X2 — CSV drift → 증분 T-Box 재실행 orchestration.

``monitor_csv_drift`` 보고서에서 added_value_threshold 를 넘는 테이블만
``update_tbox_incremental`` 로 보내 T-Box 를 최소한만 갱신한다.

A-Box partial 재생성은 본 도구 범위 밖 (future work) — T-Box 구조가
바뀌면 ``generate_abox`` 를 수동 재실행해야 한다는 힌트를 응답에 포함.
"""
from __future__ import annotations

import json
import logging
import time

from tools.common import error_response, success_response
from tools.drift_monitor import monitor_all_csvs
from tools.tbox_generation import update_tbox_incremental

logger = logging.getLogger(__name__)


def _total_added(table_report: dict) -> int:
    """이 테이블에서 **이전 스냅샷 대비** 새로 나타난 값의 수.

    ⚠️ ``had_previous`` 가 ``False`` 면 **0** 을 반환한다 — baseline 이 없는 상태에서
    모든 값은 "처음 본 값" 이지만 그것은 drift 가 아니라 **최초 관측**이다.

    2026-08-30 실측: ``data/generated/drift/`` 가 없는 상태(이 리포의 현재 상태)에서
    ``run_partial_pipeline_on_drift`` 를 부르면 **40개 전 테이블**이 drift 로 판정돼
    전부 ``update_tbox_incremental`` 로 넘어간다. 그 경로는 S2 의 ``save_guard``
    (표현력 손실률·산 링크 검사) 를 거치지 않고 T-Box 를 직접 쓰므로, cold start
    한 번이 T-Box 를 대량 재작성할 수 있다.
    """
    if not table_report.get("had_previous"):
        return 0
    return sum(
        int(d.get("added_count", 0) or 0)
        for d in (table_report.get("delta") or {}).values()
    )


def run_partial_pipeline_on_drift(
    rawdata_dir: str = "",
    added_value_threshold: int = 10,
    dry_run: bool = False,
) -> str:
    """CSV drift 감지 → 변경 테이블만 T-Box 증분 업데이트.

    Args:
        rawdata_dir: CSV 디렉터리 (비어있으면 SOURCE_RAWDATA_DIR).
        added_value_threshold: 이 개수 이상 신규 값이 추가된 테이블만 대상.
        dry_run: True 면 대상 테이블 리스트만 반환, T-Box 수정 없음.
            **drift baseline 스냅샷도 전진시키지 않는다** (2026-08-30). 예전에는
            프리뷰가 baseline 을 덮어써서, 대상을 미리 확인하는 행위 자체가 실제
            실행에 필요한 근거를 지웠다 — 실측: dry_run 호출 →
            ``incremental_tables=['T']``, 바로 다음 동일 호출 → ``[]``.

    Returns:
        {
          "drift_detected_tables": [...],   # 변화 있는 전체 테이블
          "incremental_tables": [...],      # threshold 이상 (T-Box 갱신 대상)
          "tbox_update_result": {...} | null,
          "duration_seconds": float,
          "note": str,
        }
    """
    try:
        start = time.monotonic()
        # ``dry_run`` 은 **읽기 전용** 이어야 한다 — baseline 을 전진시키면 프리뷰가
        # 실제 실행의 근거를 소모한다 (docstring 의 dry_run 항목 참조).
        report = monitor_all_csvs(rawdata_dir or None, save=not dry_run)
        per_table = report.get("per_table", []) or []

        drift_detected: list[str] = []
        incremental: list[str] = []
        # baseline 이 없어 판정할 수 없었던 테이블. 조용히 0으로 넘기면 "변화 없음"
        # 으로 오독되므로 응답에 따로 센다 (미측정 ≠ 변화 없음).
        no_baseline: list[str] = []
        for t in per_table:
            name = t.get("table") or ""
            if not t.get("had_previous"):
                no_baseline.append(name)
                continue
            total_added = _total_added(t)
            if total_added > 0:
                drift_detected.append(name)
            if total_added >= added_value_threshold:
                incremental.append(name)

        tbox_result: dict | None = None
        if incremental and not dry_run:
            raw = update_tbox_incremental(
                changed_tables=",".join(incremental),
            )
            try:
                tbox_result = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, json.JSONDecodeError):
                tbox_result = {"raw": str(raw)}

        duration = round(time.monotonic() - start, 2)
        notes = [
            "A-Box 는 자동 재생성되지 않음 — T-Box 구조 변경이 있으면 "
            "generate_abox 수동 재실행 권장.",
        ]
        if no_baseline:
            notes.append(
                f"테이블 {len(no_baseline)}개는 drift baseline 이 없어 판정하지 "
                f"않았다 (최초 관측 ≠ drift). 이번 실행이 baseline 을 만들었으므로 "
                f"다음 실행부터 판정된다. dry_run 이었다면 baseline 도 만들지 "
                f"않았으니 monitor_csv_drift 를 한 번 돌려라.",
            )
        return success_response({
            "drift_detected_tables": drift_detected,
            "incremental_tables": incremental,
            # baseline 부재로 판정 불가한 테이블 — "변화 없음" 과 구분한다.
            "no_baseline_tables": no_baseline,
            "tbox_update_result": tbox_result,
            "duration_seconds": duration,
            "note": " ".join(notes),
        })
    except Exception as e:
        return error_response(e, logger=logger)
