"""S2 토론 기록 영속화 — 라운드 궤적을 디스크에 남긴다.

## 왜 필요한가

S2 는 라운드마다 22종의 진단 필드를 성실히 계산한다 (이슈 델타 / veto / no_progress /
jury 적용·실패 사유 / CQ 궤적 / 품질 지표). 그런데 그 전부가
``state["debate_log"]`` → 도구 응답 JSON → **인메모리** ``JobRegistry``
(``common.py`` ``max_finished=8``) 로만 흐르고 **디스크에 한 글자도 남지 않았다**
(``multi_agent_tbox.py`` 의 파일 쓰기는 TTL 2개 + heartbeat 1개뿐).

결과 (2026-08-22 감사 실측):

- ``quality_history.json`` 에 S2 항목 **0건** — 59분이 품질에 기여했는지 측정 불가.
- 잡이 evict 되거나 세션이 끝나면 5라운드 토론 기록이 **소멸**한다.
- "같은 지적이 몇 라운드 반복됐나" 를 답하려면 서버 로그를 grep 해야 한다.
- 영속되는 유일한 S2 요약은 에이전트가 손으로 타이핑한 체크포인트 산문이다.

사용자의 원 질문이 "토론 과정에서 문제가 잘 수정되고 있는지 모르겠다" 였다 —
그 판단 불가 자체가 이 결함이다. 이 모듈은 **품질을 직접 올리지 않는다.** 앞선
수정들(브래킷 IRI / 게이트 부활 / Jury 스키마 / noop 분류)의 효과를 다음 실행에서
**측정 가능하게** 만드는 것이 목적이다.

## 설계

``compromise_audit.py`` 와 같은 관행을 따른다 (age-out + 크기 상한 + atomic write).
사본이 아니라 같은 패턴을 쓰는 이유는 운영자가 두 파일을 같은 방식으로 다루게
하기 위함이다.

- **age-out**: ``_MAX_AGE_DAYS`` 이전 run 은 버린다. 파싱 실패는 fail-open (보존).
- **크기 상한**: 최근 ``_MAX_RUNS`` 개만 유지.
- **atomic**: ``.tmp`` → ``os.replace``.
- **fail-open**: 저장 실패가 S2 산출물 저장을 막지 않는다. 기록은 부가 기능이다.

테스트 오염 방지: 경로를 **옵션 인자 + 모듈 상수 폴백** 형태로 둔다. 이 형태는
``tests/conftest.py`` 의 autouse fixture 가 monkeypatch 로 가로챌 수 있다 —
``compromise_audit`` 이 그 형태가 아니었을 때 테스트 2개가 배포 감사 파일에 써서
실측 10건을 픽스처로 축출한 사고가 있었다.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

#: Project root/data/generated/tbox/debate_log.json
DEBATE_LOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "generated",
    "tbox",
    "debate_log.json",
)

#: 보관할 run 수 — S2 는 회당 15~40분이라 10회면 수 주치 이력이다.
_MAX_RUNS = 10

#: age-out 기준. compromise_audit 과 동일하게 7일.
_MAX_AGE_DAYS = 7


def _load(path: str | None = None) -> dict:
    """저장된 기록 로드. 없거나 깨졌으면 빈 구조 (graceful degradation)."""
    p = path or DEBATE_LOG_PATH
    if not os.path.exists(p):
        return {"version": 1, "runs": []}
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("runs"), list):
            return {"version": 1, "runs": []}
        return data
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("debate_log.json 로드 실패 — 빈 구조 반환: %s", e)
        return {"version": 1, "runs": []}


def _save(data: dict, path: str | None = None) -> None:
    """age-out + 크기 상한 + atomic write."""
    p = path or DEBATE_LOG_PATH
    os.makedirs(os.path.dirname(p), exist_ok=True)

    cutoff = datetime.now() - timedelta(days=_MAX_AGE_DAYS)
    kept: list[dict] = []
    for run in data.get("runs", []):
        raw = run.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(raw)
            ts_naive = ts.replace(tzinfo=None) if ts.tzinfo else ts
            if ts_naive >= cutoff:
                kept.append(run)
        except (ValueError, TypeError):
            # 포맷 드리프트로 사용자 데이터를 조용히 버리지 않는다 (fail-open).
            kept.append(run)
    if len(kept) > _MAX_RUNS:
        kept = kept[-_MAX_RUNS:]

    payload = {"version": 1, "runs": kept}
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)


def _issue_trace(rounds: list[dict]) -> list[dict]:
    """라운드별 이슈를 지문으로 묶어 **몇 라운드 반복됐는지** 표로 만든다.

    이것이 이 모듈의 실질적 산출물이다. 사용자가 30초 안에 답을 얻어야 하는
    질문은 "같은 지적이 계속 반복되는가" 이고, 라운드 dict 를 눈으로 훑어서는
    알 수 없다. 지문별로 등장 라운드를 모아 ``rounds_seen`` 내림차순으로 준다.

    지문 계산은 :func:`tools.multi_agent_tbox._issue_fingerprint` 에 위임한다 —
    사본을 만들면 판정이 갈린다.
    """
    try:
        from tools.multi_agent_tbox import _issue_fingerprint
    except Exception as exc:  # noqa: BLE001 — 추적은 부가 정보다
        logger.debug("지문 함수 import 실패, 이슈 추적 생략: %s", exc)
        return []

    by_fp: dict[str, dict] = {}
    for entry in rounds or []:
        # 라운드 entry 가 dict 가 아닐 수 있다 (인프라 중단 경로에서 섞인 값).
        # 여기서 죽으면 fail-open 이 발동해 **기록 전체가 사라진다** — 파싱 가능한
        # 라운드는 살려야 한다.
        if not isinstance(entry, dict):
            logger.debug("라운드 entry 가 dict 아님, 건너뜀: %r", type(entry))
            continue
        rnd = entry.get("round")
        for agent in ("validator", "sme"):
            side = entry.get(agent)
            if not isinstance(side, dict):
                continue
            for issue in side.get("issues") or []:
                if not isinstance(issue, dict):
                    continue
                fp = _issue_fingerprint(issue)
                rec = by_fp.setdefault(fp, {
                    "fingerprint": fp,
                    "agent": agent,
                    "severity": issue.get("severity", ""),
                    "target": issue.get("target", "") or issue.get("class", ""),
                    # str() 강제 — LLM 이 symptom 에 int/list/dict 를 내면 슬라이싱이
                    # TypeError 로 죽고, 호출부의 blanket except 가 **정상 라운드
                    # 궤적까지 통째로** 안내 HTML 로 강등한다 (2026-08-22 실측).
                    "summary": str(issue.get("symptom") or issue.get("text")
                                   or issue.get("issue") or "")[:120],
                    "rounds": [],
                    "deferred_to_s3": bool(issue.get("deferred_to_s3")),
                    # category 는 승인 재판정(_blocking_issues)의 두 판정축 중
                    # 하나다. 저장하지 않으면 "그 이슈가 합의를 막았는가" 를 로그로
                    # 재구성할 수 없다 — 2026-08-26 감사에서 실제로 막혔다
                    # (deferred_to_s3 만 있어 category 축 효과를 측정 불가).
                    "category": str(issue.get("category") or ""),
                })
                if rnd not in rec["rounds"]:
                    rec["rounds"].append(rnd)
    trace = []
    for rec in by_fp.values():
        rec["rounds_seen"] = len(rec["rounds"])
        trace.append(rec)
    trace.sort(key=lambda r: (-r["rounds_seen"], str(r.get("target", ""))))
    return trace


def _issue_recurrence(rounds: list[dict]) -> list[dict]:
    """이슈 반복을 **사람이 읽는 형태로** 집계한다 (보고서 표용).

    ## 두 축이 이제 같은 판정을 쓴다 (2026-08-22 정정)

    이 함수가 처음 들어올 때는 지문 축과 **다른** 축을 쓴다고 적혀 있었다.
    그때는 사실이었다 — 지문이 ``symptom 200자 + severity + target 원문`` 이라
    LLM 재표현에 갈렸고(90 distinct / 2R+ **0건**), 그래서 같은 실행의
    ``veto_lock`` 잔존 2건과 **모순된 화면** 이 됐다.

    그 뒤 ``_issue_fingerprint`` 와 ``_issue_key`` 를 공용
    :func:`tools.multi_agent_tbox._issue_identity` 축으로 통일했다
    (category + 엔티티 원자 + 텍스트 60자 해시). 실측 재집계: 두 축이 동일하게
    **82 distinct / 2R+ 9건** 이고 과잉 병합은 **0건** 이다.

    그러므로 이 함수는 이제 "다른 축" 이 아니라 **같은 축의 다른 표현** 이다:
    :func:`_issue_trace` 는 지문 해시로 묶어 ``persistent_issue_count`` 를 내고,
    이 함수는 같은 그룹을 심각도·제기자·증상 샘플과 함께 표로 낸다. 보고서가
    사람에게 "무엇이 몇 라운드 반복됐나" 를 보여주는 쪽이다.

    키 계산은 :func:`tools.multi_agent_tbox._issue_key` 에 위임한다 — 사본을
    만들면 veto 판정과 보고서 판정이 갈린다.
    """
    try:
        from tools.multi_agent_tbox import _issue_key
    except Exception as exc:  # noqa: BLE001 — 집계는 부가 정보다
        logger.debug("_issue_key import 실패, 반복 집계 생략: %s", exc)
        return []

    _rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    by_key: dict[str, dict] = {}
    for entry in rounds or []:
        if not isinstance(entry, dict):
            continue
        rnd = entry.get("round")
        for agent in ("validator", "sme"):
            side = entry.get(agent)
            if not isinstance(side, dict):
                continue
            for issue in side.get("issues") or []:
                if not isinstance(issue, dict):
                    continue
                key = _issue_key(issue)
                sev = str(issue.get("severity", "") or "").lower()
                rec = by_key.setdefault(key, {
                    "key": key,
                    "category": str(issue.get("category", "") or ""),
                    "target": str(issue.get("target", "") or ""),
                    "agents": [],
                    "max_severity": sev,
                    # str() 강제 — 이유는 :146 주석 참조 (비-str symptom 이 섹션을 날린다).
                    "summary": str(issue.get("symptom") or issue.get("text")
                                   or issue.get("issue") or "")[:160],
                    "rounds": [],
                })
                if agent not in rec["agents"]:
                    rec["agents"].append(agent)
                if _rank.get(sev, 0) > _rank.get(rec["max_severity"], 0):
                    rec["max_severity"] = sev
                if rnd not in rec["rounds"]:
                    rec["rounds"].append(rnd)
    out = []
    for rec in by_key.values():
        rec["rounds_seen"] = len(rec["rounds"])
        out.append(rec)
    out.sort(key=lambda r: (-r["rounds_seen"],
                            -_rank.get(r["max_severity"], 0),
                            str(r.get("target", ""))))
    return out


def summarize_latest_run(path: str | None = None) -> dict | None:
    """최근 S2 실행을 **렌더 가능한 형태** 로 요약. 기록이 없으면 ``None``.

    S13 보고서(:mod:`tools.report`)가 이 함수만 호출하면 되도록, 라운드 dict 의
    중첩 필드 추출을 여기서 끝낸다 (보고서에 판독 로직 사본을 만들지 않는다).

    Returns:
        ``None`` (기록 없음) 또는
        ``{"timestamp", "total_rounds", "consensus_reached", "veto_lock_triggered",
        "duration_seconds", "statistics", "architect_initial", "infra_abort",
        "persistent_issue_count", "distinct_issue_count", "rounds": [...],
        "recurring": [...], "runs_recorded": int}``.
    """
    data = _load(path)
    runs = data.get("runs") or []
    if not runs:
        return None
    run = runs[-1]
    if not isinstance(run, dict):
        return None

    rounds_out: list[dict] = []
    for entry in run.get("rounds") or []:
        if not isinstance(entry, dict):
            continue
        v = entry.get("validator") or {}
        s = entry.get("sme") or {}
        cq = entry.get("cq_runtime") or {}
        rev = entry.get("revision") or {}
        jury = entry.get("jury_fixes_summary") or {}
        veto = entry.get("veto_lock") or {}
        qm = entry.get("quality_metrics") or {}
        rounds_out.append({
            "round": entry.get("round"),
            "validator_issues": v.get("issues_count"),
            "validator_critical_high": v.get("critical_high"),
            "validator_approved": bool(v.get("approved")),
            "validator_parse_error": bool(v.get("parse_error")),
            "sme_issues": s.get("issues_count"),
            "sme_critical_high": s.get("critical_high"),
            "sme_approved": bool(s.get("approved")),
            "sme_parse_error": bool(s.get("parse_error")),
            "cq_coverage_pct": cq.get("coverage_pct"),
            "cq_unanswerable": cq.get("unanswerable_count"),
            "cq_block_data_gap": len(entry.get("cq_block_data_gap") or []),
            "triples": rev.get("triples"),
            "no_progress": bool(rev.get("no_progress")),
            "no_progress_streak": rev.get("no_progress_streak"),
            "jury": {
                "requested": jury.get("requested"),
                "applied": jury.get("applied"),
                "noop": jury.get("noop"),
                "skipped": jury.get("skipped"),
                "failed": jury.get("failed"),
                "failed_reasons": [
                    str(d.get("reason", ""))[:200]
                    for d in (jury.get("failed_details") or [])
                    if isinstance(d, dict)
                ][:3],
            } if jury else None,
            "veto_persistent_validator": veto.get("persistent_validator"),
            "veto_persistent_sme": veto.get("persistent_sme"),
            "veto_lock_released": bool(entry.get("veto_lock_released")),
            "fair": (qm.get("fair") or {}).get("overall"),
            "farber": (qm.get("farber") or {}).get("overall"),
            "jury_final_production_ready": (
                (entry.get("jury_final") or {}).get("production_ready")
                if entry.get("jury_final") else None
            ),
        })

    recurring = _issue_recurrence(run.get("rounds") or [])
    return {
        "timestamp": run.get("timestamp"),
        "total_rounds": run.get("total_rounds"),
        "consensus_reached": bool(run.get("consensus_reached")),
        "veto_lock_triggered": bool(run.get("veto_lock_triggered")),
        "veto_persistent_targets": run.get("veto_persistent_targets") or [],
        "duration_seconds": run.get("duration_seconds"),
        "statistics": run.get("statistics") or {},
        "architect_initial": run.get("architect_initial") or {},
        "infra_abort": run.get("infra_abort"),
        # **저장된 값이 아니라 지금 다시 센다.** 파일의 카운터는 그 run 이 돌던
        # 시점의 지문 알고리즘으로 계산됐다. 지문을 개선하면 (2026-08-22:
        # category+엔티티+텍스트60 으로 통일) 옛 run 의 저장값과 새 집계가
        # 어긋나 **한 화면에 모순된 두 숫자** 가 뜬다 (실측: 저장값
        # persistent=0 인데 재집계 recurring=9). 재집계가 항상 현재 판정을
        # 반영하므로 그것을 쓰고, 저장값은 `*_recorded` 로 남겨 비교 가능하게 한다.
        "persistent_issue_count": sum(
            1 for r in recurring if r["rounds_seen"] >= 2
        ),
        "distinct_issue_count": len(recurring),
        "persistent_issue_count_recorded": run.get("persistent_issue_count"),
        "distinct_issue_count_recorded": len(run.get("issue_trace") or []),
        "rounds": rounds_out,
        "recurring": [r for r in recurring if r["rounds_seen"] >= 2],
        "recurring_distinct": len(recurring),
        "runs_recorded": len(runs),
    }


def append_debate_run(
    *,
    rounds: list[dict],
    consensus_reached: bool,
    veto_lock_triggered: bool,
    veto_targets: list[str] | None = None,
    statistics: dict | None = None,
    architect_initial: dict | None = None,
    duration_seconds: float | None = None,
    infra_abort: dict | None = None,
    save_guard: dict | None = None,
    timestamp: str | None = None,
    path: str | None = None,
) -> dict[str, Any]:
    """한 번의 S2 실행 기록을 append 하고 저장한 요약을 돌려준다.

    **fail-open**: 어떤 예외도 호출부로 전파하지 않는다. 이 기록이 실패해도
    T-Box 저장은 진행돼야 한다.

    Returns:
        ``{"saved": bool, "path": str, "runs": int, "persistent_issues": int}``
        — 응답에 실어 운영자가 기록 여부를 확인할 수 있게 한다. 실패 시
        ``{"saved": False, "error": "..."}``.
    """
    p = path or DEBATE_LOG_PATH
    try:
        trace = _issue_trace(rounds)
        persistent = [t for t in trace if t["rounds_seen"] >= 2]
        run = {
            "timestamp": timestamp or datetime.now().isoformat(),
            "consensus_reached": bool(consensus_reached),
            "veto_lock_triggered": bool(veto_lock_triggered),
            "veto_persistent_targets": list(veto_targets or [])[:10],
            "total_rounds": len(rounds or []),
            "duration_seconds": duration_seconds,
            "statistics": statistics or {},
            "architect_initial": architect_initial or {},
            "infra_abort": infra_abort,
            "save_guard": save_guard,
            # 라운드 원본 — 이슈 본문 포함 (슬림화는 호출부에서 이미 됨).
            "rounds": rounds or [],
            # 사후 판독용 파생 표: "같은 지적이 몇 라운드 반복됐나".
            "issue_trace": trace,
            "persistent_issue_count": len(persistent),
        }
        data = _load(p)
        data.setdefault("runs", []).append(run)
        _save(data, p)
        logger.info(
            "S2 토론 기록 저장 → %s (run %d개 보관, 라운드 %d, 2회+ 반복 이슈 %d건)",
            p, len(_load(p).get("runs", [])), run["total_rounds"], len(persistent),
        )
        return {
            "saved": True,
            "path": p,
            "runs": len(data.get("runs", [])),
            "persistent_issues": len(persistent),
        }
    except Exception as exc:  # noqa: BLE001 — 기록 실패가 저장을 막지 않는다
        logger.warning("S2 토론 기록 저장 실패 (무시하고 진행): %s", exc)
        return {"saved": False, "error": f"{type(exc).__name__}: {str(exc)[:200]}"}


__all__ = ("DEBATE_LOG_PATH", "append_debate_run", "summarize_latest_run")
