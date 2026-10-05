"""Semantic Drift Monitor — Q6.

CSV가 변경될 때마다 컬럼별 unique 값 집합의 **스냅샷**을 저장하고,
다음 실행에서 신규 값이 기존 T-Box 클래스/이전 값 분포와 얼마나
일관되는지 평가한다. T-Box가 정적 스냅샷에서 "살아있는 문서"로
진화하도록 드리프트 시그널을 제공.

이 버전은 **통계적 유사도** 기반 (임베딩 호출 없음):
- 각 CSV의 컬럼 단위 unique 값 집합을 해시로 스냅샷
- 다음 실행 시 delta(추가/삭제된 값) 계산
- 신규 값 vs 기존 값 집합 간 Jaccard + character n-gram 유사도
- threshold 미만 = "emerging concept" 플래그

스냅샷 저장: data/generated/drift/<table>.json
리포트: reports/drift_report.json
"""
from __future__ import annotations

import csv
import json
import logging
import os
from datetime import datetime

from config import GENERATED_REPORTS_DIR, SOURCE_RAWDATA_DIR
from tools.common import error_response, success_response

logger = logging.getLogger(__name__)

DRIFT_DIR = os.path.join(
    os.path.dirname(GENERATED_REPORTS_DIR.rstrip("/")), "drift",
)


def _snapshot_path(table: str) -> str:
    safe = table.replace("/", "_").replace(" ", "_")
    return os.path.join(DRIFT_DIR, f"{safe}.json")


def _load_csv_unique_values(csv_path: str, max_unique: int = 5000) -> dict:
    """컬럼별 unique 값 집합. max_unique 초과 컬럼은 샘플 + 카운트만 기록."""
    per_col: dict[str, dict] = {}
    with open(csv_path, encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        headers = list(reader.fieldnames or [])
        uniques: dict[str, set] = {h: set() for h in headers}
        for row in reader:
            for h in headers:
                v = (row.get(h) or "").strip()
                if v and len(uniques[h]) < max_unique:
                    uniques[h].add(v)
    for h, vs in uniques.items():
        per_col[h] = {"count": len(vs), "values": sorted(vs)}
    return {"headers": headers, "columns": per_col}


def _ngrams(s: str, n: int = 3) -> set[str]:
    s = s.lower()
    if len(s) < n:
        return {s}
    return {s[i : i + n] for i in range(len(s) - n + 1)}


def _ngram_max_similarity(value: str, reference: set[str]) -> float:
    """value와 reference 집합 내 가장 가까운 값의 Jaccard(n-gram)."""
    if not reference:
        return 0.0
    vg = _ngrams(value)
    best = 0.0
    for ref in reference:
        rg = _ngrams(ref)
        if not vg and not rg:
            continue
        sim = len(vg & rg) / max(len(vg | rg), 1)
        if sim > best:
            best = sim
    return round(best, 3)


def snapshot_csv(csv_path: str, *, save: bool = True) -> dict:
    """CSV 현재 상태를 이전 스냅샷과 비교하고 (선택적으로) 새 스냅샷을 저장.

    반환: {table, delta: {added_values, removed_values},
           drift_scores, previous_timestamp, snapshot_saved}

    ## 측정이 자기 근거를 소모했다 (2026-08-30 규명)

    예전에는 delta 계산 직후 **무조건** 스냅샷을 덮어썼다. 그래서 같은 CSV 를 두 번
    재면 두 번째는 drift 0건이 된다. 격리 실험::

        call1 (baseline 확립)   had_previous=False
        call2 (신규 값 1개 추가)  added=1
        call3 (같은 CSV)         added=0   ← 근거가 소모됐다

    특히 ``run_partial_pipeline_on_drift(dry_run=True)`` 는 "대상 테이블만 보고
    T-Box 는 건드리지 않는다" 고 문서화돼 있는데, 그 **프리뷰 행위 자체가** 실제
    실행에 필요한 근거를 지웠다. 운영자가 미리 확인하면 정작 실행할 때 대상이
    비어 있다.

    이 리포의 같은 축: 측정 스텝은 자기가 판정하려는 대상을 바꾸면 안 된다
    (게이트가 삭제 스텝 앞에서 재서 배포 안 되는 상태를 측정한 사례).

    Args:
        save: ``False`` 면 delta 만 계산하고 스냅샷을 갱신하지 않는다 (읽기 전용
            프리뷰). 기본 ``True`` — 정기 모니터링은 baseline 을 전진시켜야 한다.
    """
    table = os.path.basename(csv_path).replace(".csv", "")
    current = _load_csv_unique_values(csv_path)

    prev_path = _snapshot_path(table)
    prev: dict | None = None
    if os.path.exists(prev_path):
        try:
            with open(prev_path, encoding="utf-8") as f:
                prev = json.load(f)
        except Exception:
            prev = None

    delta: dict[str, dict] = {}
    drift_scores: dict[str, dict] = {}
    for col, info in current["columns"].items():
        cur_vals = set(info["values"])
        prev_vals = set()
        if prev and "columns" in prev and col in prev["columns"]:
            prev_vals = set(prev["columns"][col].get("values", []))
        added = sorted(cur_vals - prev_vals)
        removed = sorted(prev_vals - cur_vals)
        delta[col] = {
            "added_count": len(added),
            "removed_count": len(removed),
            "added_sample": added[:10],
            "removed_sample": removed[:10],
        }
        # 신규 값 vs 기존 값 유사도
        if added and prev_vals:
            sims = [_ngram_max_similarity(v, prev_vals) for v in added[:20]]
            avg_sim = round(sum(sims) / len(sims), 3)
            emerging = [
                {"value": v, "max_sim": s}
                for v, s in zip(added[:20], sims, strict=False)
                if s < 0.3
            ]
            drift_scores[col] = {
                "avg_similarity": avg_sim,
                "emerging_values": emerging,
            }

    # 현재 상태 저장 — ``save=False`` 면 건너뛴다 (docstring 의 근거 소모 문제).
    if save:
        os.makedirs(DRIFT_DIR, exist_ok=True)
        with open(prev_path, "w", encoding="utf-8") as f:
            json.dump({
                **current,
                "snapshot_at": datetime.now().isoformat(),
            }, f, ensure_ascii=False, indent=2)

    return {
        "table": table,
        "had_previous": prev is not None,
        "previous_snapshot_at": prev.get("snapshot_at") if prev else None,
        "delta": delta,
        "drift_scores": drift_scores,
        # baseline 을 전진시켰는가. 이 값이 없으면 "왜 두 번째 호출이 0건인가" 를
        # 추적할 수 없다 (부수효과는 응답에 드러나야 한다).
        "snapshot_saved": save,
    }


def monitor_all_csvs(rawdata_dir: str | None = None, *, save: bool = True) -> dict:
    """모든 CSV 의 drift 를 측정 (선택적으로 baseline 전진).

    Args:
        save: ``False`` 면 스냅샷을 갱신하지 않는다 — 프리뷰 호출이 실제 실행에
            필요한 근거를 소모하지 않게 한다 (``snapshot_csv`` docstring 참조).
    """
    import glob
    rd = rawdata_dir or SOURCE_RAWDATA_DIR
    files = sorted(glob.glob(os.path.join(rd, "*.csv")))
    per_table = [snapshot_csv(p, save=save) for p in files]

    total_added = sum(
        sum(d["added_count"] for d in r["delta"].values())
        for r in per_table
    )
    total_emerging = sum(
        sum(len(v["emerging_values"]) for v in r.get("drift_scores", {}).values())
        for r in per_table
    )
    return {
        "tables": len(per_table),
        "total_added_values": total_added,
        "total_emerging_values": total_emerging,
        "per_table": per_table,
        # baseline 전진 여부 — 프리뷰 호출과 정기 모니터링을 구분한다.
        "snapshot_saved": save,
    }


def monitor_csv_drift(rawdata_dir: str = "", save_snapshot: bool = True) -> str:
    """CSV 드리프트 모니터 — 신규 값 + 기존 분포 대비 유사도 (Q6).

    각 CSV 컬럼의 unique 값 스냅샷을 저장하고, 이전 실행 대비 증감 +
    신규 값의 n-gram Jaccard 유사도(기존 값 대비)를 계산. 0.3 미만은
    emerging concept 후보로 분류.

    Args:
        rawdata_dir: CSV 디렉터리 (비어있으면 SOURCE_RAWDATA_DIR).
        save_snapshot: ``True`` (기본) 면 baseline 을 전진시킨다 — 정기 모니터링의
            정상 동작이다. ``False`` 면 **읽기 전용** 으로 delta 만 본다.

            baseline 전진은 되돌릴 수 없다 (``data/generated/drift/`` 는
            ``.gitignore`` 대상이라 이전 스냅샷이 남지 않는다). 같은 drift 를 두 번
            보고 싶거나 다른 도구가 그 근거를 쓸 예정이면 ``False`` 로 부르라 —
            ``run_partial_pipeline_on_drift(dry_run=True)`` 가 그렇게 한다
            (``snapshot_csv`` docstring 의 근거 소모 실측 참조).
    """
    try:
        report = monitor_all_csvs(rawdata_dir or None, save=save_snapshot)
        try:
            os.makedirs(GENERATED_REPORTS_DIR, exist_ok=True)
            with open(os.path.join(GENERATED_REPORTS_DIR, "drift_report.json"),
                      "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("drift_report 저장 실패: %s", e)
        return success_response(report)
    except Exception as e:
        return error_response(f"drift monitor 실패: {e}", logger=logger)
