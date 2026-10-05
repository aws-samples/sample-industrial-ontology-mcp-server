"""CQ 실패 피드백 영속 저장 + 다음 T-Box 생성에 주입.

데이터 흐름:
  S12 test_domain_queries → append_iteration() → cq_feedback.json
  S2 _build_initial_draft → load_active_suggestions() → Architect prompt

Age-out / dedupe / rotation 방지 로직으로 LLM 혼란 방지:
  - max_age_days (7): 오래된 iteration 자동 삭제
  - max_iterations (10): 파일 크기 bound
  - dedupe by (type, class_a, class_b): 같은 구조 결함은 최신 1건만 주입
  - age_iterations: 반복 실패 횟수 추적 (3회 이상이면 LLM 난해 패턴으로 표시)

모든 I/O 는 atomic write (`.tmp` + os.replace) 로 파일 손상 방지.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

# Project root/data/generated/reports/cq_feedback.json
CQ_FEEDBACK_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "generated",
    "reports",
    "cq_feedback.json",
)

_MAX_ITERATIONS = 10
_MAX_AGE_DAYS = 7

# Circuit breaker: if the most recent N iterations show no pass_rate
# improvement, skip re-injecting suggestions (avoids LLM self-reference loop).
_CIRCUIT_BREAKER_WINDOW = 3
# Stubborn drop: a suggestion that has survived this many consecutive
# iterations is considered unsolvable by the LLM — dropped from the active
# set so subsequent runs don't rotate on the same structural gap.
_STUBBORN_AGE_THRESHOLD = 5


def _load_feedback() -> dict:
    """Load persisted feedback. Returns empty default on missing/malformed file.

    Graceful degradation: JSON decode error or missing keys → empty structure
    so existing pipeline is never blocked by a corrupted feedback file.
    """
    if not os.path.exists(CQ_FEEDBACK_PATH):
        return {"version": 1, "iterations": []}
    try:
        with open(CQ_FEEDBACK_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "iterations" not in data:
            return {"version": 1, "iterations": []}
        if not isinstance(data.get("iterations"), list):
            return {"version": 1, "iterations": []}
        return data
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("cq_feedback.json 로드 실패 — 빈 구조 반환: %s", e)
        return {"version": 1, "iterations": []}


def _save_feedback(data: dict) -> None:
    """Persist feedback with age-out + size bound + atomic write.

    Age-out: iterations older than _MAX_AGE_DAYS are dropped.
    Size bound: only the most recent _MAX_ITERATIONS are kept.
    Atomic: write to ``.tmp`` first then os.replace to the final path.
    """
    os.makedirs(os.path.dirname(CQ_FEEDBACK_PATH), exist_ok=True)

    cutoff = datetime.now() - timedelta(days=_MAX_AGE_DAYS)
    iterations = data.get("iterations", [])

    # Age-out: drop iterations older than cutoff. Parse failures keep the row
    # (fail-open — user data shouldn't be silently dropped on ISO format drift).
    filtered: list[dict] = []
    for it in iterations:
        ts_raw = it.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_raw)
            # Strip tz for comparison (cutoff is naive)
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
    tmp = CQ_FEEDBACK_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CQ_FEEDBACK_PATH)


def _sug_key(sug: dict) -> tuple:
    """Dedupe / carryover key for a suggestion.

    Combines (type, class_a_or_class, class_b) so both missing_connection
    (has class_a+class_b) and missing_instances/missing_dp_values
    (has class) produce a stable tuple.

    For `failed_query` suggestions we fall back to (type, cq_id,
    error_category) so the same failing CQ with the same error category is
    only injected once per iteration.
    """
    if sug.get("type") == "failed_query":
        return (
            "failed_query",
            sug.get("cq_id", ""),
            sug.get("error_category", ""),
        )
    return (
        sug.get("type", ""),
        sug.get("class", sug.get("class_a", "")),
        sug.get("class_b", ""),
    )


def _classify_query_error(
    error: Exception | None, result_count: int = 1
) -> str:
    """Categorize a SPARQL execution outcome.

    Returns one of:
      - ``parse_error``       — syntax / ParseException / "expected ..."
      - ``timeout``           — TimeoutError or "timeout" in message
      - ``unresolved_prefix`` — "prefix ... not defined/undefined"
      - ``zero_results``      — no error but result_count == 0
      - ``ok``                — no error and result_count > 0

    Heuristic ordering: specific keyword matches (timeout, prefix) run
    before the generic "parse" catch-all so that prefixed-but-parseable
    errors aren't mislabelled as parse_error.
    """
    if error is None:
        return "zero_results" if result_count == 0 else "ok"
    name = type(error).__name__
    msg = str(error).lower()
    if "timeout" in name.lower() or "timeout" in msg:
        return "timeout"
    if "prefix" in msg and ("not" in msg or "undefined" in msg):
        return "unresolved_prefix"
    if "parse" in name.lower() or "parse" in msg or "expected" in msg:
        return "parse_error"
    # Unknown errors default to parse_error — surfaces the raw message to
    # the Architect prompt without a silent drop.
    return "parse_error"


def _build_failed_query_suggestion(
    cq_id: str,
    sparql: str,
    error: Exception | None,
    result_count: int = 0,
) -> dict:
    """Build a cq_feedback suggestion for a failed SPARQL execution.

    The suggestion shape mirrors the existing missing_connection / etc.
    entries (type + cq_id + age_iterations is attached by
    ``append_iteration``). The extra fields are:

      - ``error_category`` — from ``_classify_query_error``
      - ``error_sample``   — first 200 chars of the error message (truncated
        to keep prompt size bounded)
      - ``class_hint``     — first ``steel:LocalName`` local-name found in
        the SPARQL (heuristic; empty string when no match)
    """
    import re

    category = _classify_query_error(error, result_count)
    error_msg = str(error) if error else ""
    # Extract first mentioned domain class using project-configured NS prefix.
    # NS_PREFIX 가 비어있으면 도메인 설정 자체가 깨진 상황 — class hint 없이 진행
    # (silent corruption 회피: 임의 default 로 폴백하지 않음).
    from domain.namespaces import NS_PREFIX
    if NS_PREFIX:
        pattern = re.compile(rf"\b{re.escape(NS_PREFIX)}:([A-Z][A-Za-z0-9_]*)")
        m = pattern.search(sparql or "")
        class_hint = m.group(1) if m else ""
    else:
        class_hint = ""
    return {
        "type": "failed_query",
        "cq_id": cq_id,
        "error_category": category,
        "error_sample": error_msg[:200],
        "class_hint": class_hint,
    }


def append_iteration(
    pass_rate: float,
    total_cqs: int,
    passed: int,
    failed: int,
    suggestions: list[dict],
    resolved_cq_ids: list[str] | None = None,
) -> None:
    """Record a S12 iteration outcome into the feedback file.

    Called at the end of ``test_domain_queries``. Each suggestion is tagged
    with ``age_iterations`` = how many consecutive iterations it has appeared
    (for rotation detection in the Architect prompt).

    Args:
        pass_rate: 0-100 percentage of CQs that passed
        total_cqs: total CQ count evaluated
        passed: count of PASS CQs
        failed: count of FAIL CQs
        suggestions: improvement_suggestions list from ``test_domain_queries``
        resolved_cq_ids: CQ ids that were failing previously and now PASS
    """
    data = _load_feedback()
    prior = data.get("iterations", [])

    # Compute age_iterations by walking prior iterations newest-first and
    # incrementing when the same _sug_key was seen.
    for sug in suggestions:
        key = _sug_key(sug)
        age = 1
        for prev_it in reversed(prior):
            match_age = max(
                (
                    s.get("age_iterations", 1)
                    for s in prev_it.get("suggestions", [])
                    if _sug_key(s) == key
                ),
                default=None,
            )
            if match_age is not None:
                age = match_age + 1
                break
        sug["age_iterations"] = age

    iteration = {
        "timestamp": datetime.now().isoformat(),
        "pass_rate": pass_rate,
        "total_cqs": total_cqs,
        "passed": passed,
        "failed": failed,
        "suggestions": suggestions,
        "resolved_from_previous": resolved_cq_ids or [],
    }

    data["iterations"].append(iteration)
    _save_feedback(data)


def load_active_suggestions() -> list[dict]:
    """Return the most recent iteration's deduped suggestions.

    Used by the S2 Architect prompt builder. Results sorted by descending
    age_iterations so LLM sees the most stubborn failures first.

    - Only the latest iteration is returned (older rows are kept for trend
      analysis but injecting all of them would flood the prompt).
    - Dedupe by (type, class_a_or_class, class_b): the same structural gap
      shouldn't appear twice even if multiple CQs surfaced it.

    **Circuit breaker**: 최근 ``_CIRCUIT_BREAKER_WINDOW`` iterations 동안
    pass_rate 가 개선되지 않으면 빈 리스트 반환 (LLM 자기참조 루프 방지,
    수동 개입 신호). 또한 age >= ``_STUBBORN_AGE_THRESHOLD`` 인 suggestion
    은 제거 (LLM 이 해결 못한 지 너무 오래됨).
    """
    data = _load_feedback()
    iterations = data.get("iterations", [])
    if not iterations:
        return []

    # Circuit breaker: if the latest pass_rate is no better than the best
    # (max) of the previous (_CIRCUIT_BREAKER_WINDOW - 1) iterations, we're
    # stuck — the LLM keeps oscillating rather than improving. Returning an
    # empty list signals to the Architect prompt builder to skip the feedback
    # section rather than re-inject suggestions the LLM has repeatedly failed
    # to resolve (e.g. pass_rate 60 → 55 → 60 shows no net progress).
    if len(iterations) >= _CIRCUIT_BREAKER_WINDOW:
        recent_rates = [
            it.get("pass_rate", 0.0) for it in iterations[-_CIRCUIT_BREAKER_WINDOW:]
        ]
        if recent_rates[-1] <= max(recent_rates[:-1]):
            logger.warning(
                "[CQ Feedback] circuit breaker tripped — 최근 %d iterations "
                "pass_rate %s, 수동 개입 필요",
                _CIRCUIT_BREAKER_WINDOW, recent_rates,
            )
            return []

    latest = iterations[-1]
    suggestions = latest.get("suggestions", []) or []

    # Stubborn drop: age_iterations >= threshold means the LLM failed to fix
    # the same gap too many times in a row — stop feeding it back.
    suggestions = [
        s for s in suggestions
        if s.get("age_iterations", 1) < _STUBBORN_AGE_THRESHOLD
    ]

    seen: set[tuple] = set()
    unique: list[dict] = []
    for sug in sorted(suggestions, key=lambda s: -s.get("age_iterations", 1)):
        key = _sug_key(sug)
        if key in seen:
            continue
        seen.add(key)
        unique.append(sug)
    return unique


def format_feedback_for_prompt(
    suggestions: list[dict],
    pass_rate: float | None = None,
) -> str:
    """Render suggestions as a markdown section for the Architect prompt.

    Returns an empty string when no suggestions are active — callers can
    unconditionally concatenate the result without guarding.

    Grouping: by ``type`` (missing_connection / missing_instances /
    missing_dp_values / other). Caps each group at 10 lines to avoid
    context bloat. Suggestions with age_iterations >= 3 get a ⚠️반복 badge
    to flag patterns the previous Architect runs failed to fix.
    """
    if not suggestions:
        return ""

    by_type: dict[str, list[dict]] = {}
    for s in suggestions:
        by_type.setdefault(s.get("type", "other"), []).append(s)

    lines: list[str] = ["## 이전 실행 피드백 (CQ 테스트 결과)"]
    if pass_rate is not None:
        lines.append(
            f"\n지난 iteration 통과율: **{pass_rate}%**. "
            "다음 CQ 들이 실패 — T-Box 설계 시 반영:\n"
        )

    type_labels = {
        "missing_connection": "### 누락된 OP 연결",
        "missing_instances": (
            "### 인스턴스 누락 (A-Box 생성 실패 — T-Box 관점에선 주로 클래스 매핑 이슈)"
        ),
        "missing_dp_values": "### DP 값 없음 / DP 스키마 누락",
        "failed_query": "### 쿼리 실행 실패 (SPARQL 파싱/타임아웃/무결과)",
    }

    for t, items in by_type.items():
        lines.append(f"\n{type_labels.get(t, f'### {t}')} ({len(items)}건)")
        for s in items[:10]:
            age = s.get("age_iterations", 1)
            age_mark = " ⚠️반복" if age >= 3 else ""
            cq_id = s.get("cq_id", "?")
            if t == "missing_connection":
                lines.append(
                    f"- {s.get('class_a', '?')} ↔ {s.get('class_b', '?')} "
                    f"(CQ {cq_id}{age_mark})"
                )
            elif t == "failed_query":
                lines.append(
                    f"- CQ {cq_id}: {s.get('error_category', '?')} "
                    f"({s.get('class_hint', '?')}{age_mark}) — "
                    f"{s.get('error_sample', '')[:80]}"
                )
            else:
                lines.append(f"- {s.get('class', '?')} (CQ {cq_id}{age_mark})")

    lines.append(
        "\n**지침**: 위 연결/속성을 T-Box 에 명시적으로 포함하세요. "
        "도메인적으로 의미 있는 ObjectProperty 로 모델링 (직접 관계 vs 중간 이벤트 클래스)."
    )
    return "\n".join(lines)


def clear_feedback() -> None:
    """Remove the feedback file. Test/reset helper; not called by the pipeline."""
    if os.path.exists(CQ_FEEDBACK_PATH):
        os.remove(CQ_FEEDBACK_PATH)
