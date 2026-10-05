"""Compromise audit sidecar — `_architect_compromise` 결정 과정의 누적 기록.

데이터 흐름:
  `_architect_compromise` 완료 직후 → `append_compromise_audit(artifact)` 호출
  → `data/generated/tbox/compromise_audit.json` 에 누적.

저장 artifact 스키마 (한 iteration):
  {
    "timestamp": "...ISO-8601...",
    "consensus_reached": bool,
    "rounds_conducted": int,
    "total_issues": int,
    "decisions_by_priority": {
      "≥70_accepted": int, "40-69_partial": int, "<40_rejected": int,
    },
    "persistent_issues_count": int,
    "overall_rationale": str,
    "decisions": [...],
  }

T3 `tools/cq_feedback.py` 패턴 재사용:
  - max_iterations 10 (size bound)
  - max_age_days 7 (age-out)
  - atomic write (`.tmp` + `os.replace`)
  - 손상된 파일 → graceful degrade (빈 구조)

실패는 항상 non-blocking — `_architect_compromise` 경로에서 try/except 로 감싸
compromise 결과 자체를 망가뜨리지 않는다.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Project root/data/generated/tbox/compromise_audit.json
COMPROMISE_AUDIT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "generated",
    "tbox",
    "compromise_audit.json",
)

_MAX_ITERATIONS = 10
_MAX_AGE_DAYS = 7


def _load_audit(path: str | None = None) -> dict:
    """Load persisted audit. Returns empty default on missing/malformed file.

    Graceful degradation: JSON decode error → empty structure so the compromise
    path is never blocked by a corrupted audit file.
    """
    p = path or COMPROMISE_AUDIT_PATH
    if not os.path.exists(p):
        return {"version": 1, "iterations": []}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "iterations" not in data:
            return {"version": 1, "iterations": []}
        if not isinstance(data.get("iterations"), list):
            return {"version": 1, "iterations": []}
        return data
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("compromise_audit.json 로드 실패 — 빈 구조 반환: %s", e)
        return {"version": 1, "iterations": []}


def _save_audit(data: dict, path: str | None = None) -> None:
    """Persist audit with age-out + size bound + atomic write.

    Age-out: iterations older than ``_MAX_AGE_DAYS`` are dropped.
    Size bound: only the most recent ``_MAX_ITERATIONS`` are kept.
    Atomic: write to ``.tmp`` first then ``os.replace`` to the final path.
    """
    p = path or COMPROMISE_AUDIT_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)

    cutoff = datetime.now() - timedelta(days=_MAX_AGE_DAYS)
    iterations = data.get("iterations", [])

    # Age-out: drop iterations older than cutoff. Parse failures keep the row
    # (fail-open — ISO format drift shouldn't silently drop user data).
    filtered: list[dict] = []
    for it in iterations:
        ts_raw = it.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_raw)
            # tz-naive comparison — cutoff is naive
            ts_naive = ts.replace(tzinfo=None) if ts.tzinfo else ts
            if ts_naive >= cutoff:
                filtered.append(it)
        except (ValueError, TypeError):
            filtered.append(it)

    # Size bound: keep newest _MAX_ITERATIONS
    if len(filtered) > _MAX_ITERATIONS:
        filtered = filtered[-_MAX_ITERATIONS:]

    data["iterations"] = filtered

    # Atomic write via tmp + replace
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def _bucket_decisions_by_priority(decisions: list[dict]) -> dict:
    """priority_score 구간별 이슈 분포 집계 (LLM 결정과 무관).

    Returns:
        {
          "by_score": {"high_70+": int, "mid_40_69": int, "low_<40": int},
          "by_decision": {"accept": int, "partial": int, "reject": int, "other": int},
        }

    by_score: priority_score 구간별 카운트 — 결정적 규칙 기준 이슈 분포.
    by_decision: LLM 이 실제로 내린 결정별 카운트 — 감사 시 "고 priority
    이슈를 LLM 이 reject 했나?" 추적 가능. 두 집계를 분리해 "규칙 권장" 과
    "LLM 실제 결정" 의 불일치를 노출.
    """
    by_score = {"high_70+": 0, "mid_40_69": 0, "low_<40": 0}
    by_decision = {"accept": 0, "partial": 0, "reject": 0, "other": 0}
    for d in decisions or []:
        try:
            score = int(d.get("priority_score", 0) or 0)
        except (TypeError, ValueError):
            score = 0
        if score >= 70:
            by_score["high_70+"] += 1
        elif score >= 40:
            by_score["mid_40_69"] += 1
        else:
            by_score["low_<40"] += 1

        dec = str(d.get("decision", "") or "").lower()
        if dec in ("accept", "partial", "reject"):
            by_decision[dec] += 1
        else:
            by_decision["other"] += 1

    return {"by_score": by_score, "by_decision": by_decision}


def append_compromise_audit(
    artifact: dict,
    path: str | None = None,
) -> None:
    """Append a compromise iteration record to the audit sidecar.

    Called at the end of `_architect_compromise` via try/except non-blocking.
    Caller provides the raw artifact (consensus/decisions/rationale); this
    function:
      1. Auto-adds ``timestamp`` if absent
      2. Auto-computes ``decisions_by_priority`` if absent
      3. Auto-computes ``total_issues``, ``persistent_issues_count`` if absent
      4. Triggers age-out + size bound on save

    Args:
        artifact: dict with at least ``decisions`` and ``overall_rationale``.
            Additional keys are preserved.
        path: Override for test isolation. Defaults to ``COMPROMISE_AUDIT_PATH``.
    """
    data = _load_audit(path)

    # Copy so we don't mutate caller's dict
    iteration = dict(artifact)

    if "timestamp" not in iteration:
        iteration["timestamp"] = datetime.now().isoformat()

    decisions = iteration.get("decisions", []) or []
    if "decisions_by_priority" not in iteration:
        iteration["decisions_by_priority"] = _bucket_decisions_by_priority(decisions)

    if "total_issues" not in iteration:
        iteration["total_issues"] = len(decisions)

    if "persistent_issues_count" not in iteration:
        # priority_table 이 함께 전달됐으면 age_rounds>=2 건수, 아니면 0
        priority_table = iteration.get("_priority_table") or []
        iteration["persistent_issues_count"] = sum(
            1 for p in priority_table if int(p.get("age_rounds", 1) or 1) >= 2
        )

    data["iterations"].append(iteration)
    _save_audit(data, path)


def clear_audit(path: str | None = None) -> None:
    """Remove the audit file. Test/reset helper; not called by the pipeline."""
    p = path or COMPROMISE_AUDIT_PATH
    if os.path.exists(p):
        os.remove(p)
