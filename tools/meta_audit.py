"""Meta-audit — aggregates mutation runs + quality history into recommendations."""
from __future__ import annotations

import glob
import json
import os
import re
import time
from collections import defaultdict
from itertools import combinations

from config import GENERATED_DIR
from tools.common import error_response, resolve_child_path

#: ``run_meta_audit`` 가 쓰는 감사 파일 이름의 timestamp 형식 (``%Y%m%dT%H%M%SZ``).
_AUDIT_TIMESTAMP_RE = re.compile(r"[0-9]{8}T[0-9]{6}Z")


def compute_sensitivity_matrix(runs: list[dict]) -> dict[str, dict[str, float]]:
    """For each (check, category), compute catch_rate = caught / applied."""
    applied_by_cat: dict[str, int] = defaultdict(int)
    caught: dict[tuple[str, str], int] = defaultdict(int)
    checks_seen: set[str] = set()

    for run in runs:
        for m in run.get("mutants", []):
            if not m.get("applied"):
                continue
            cat = m.get("category", "?")
            applied_by_cat[cat] += 1
            for check in m.get("caught_by", []):
                caught[(check, cat)] += 1
                checks_seen.add(check)
            for check in m.get("baseline_summary", {}):
                checks_seen.add(check)

    matrix: dict[str, dict[str, float]] = {}
    for check in sorted(checks_seen):
        row: dict[str, float] = {}
        for cat, total in applied_by_cat.items():
            if total == 0:
                continue
            row[cat] = round(caught.get((check, cat), 0) / total, 3)
        matrix[check] = row
    return matrix


def compute_blind_spots(runs: list[dict]) -> list[dict]:
    """Categories where >= 1 applied mutant was caught by zero checks."""
    by_cat_stats: dict[str, dict[str, int]] = defaultdict(lambda: {"mutants": 0, "caught": 0})
    for run in runs:
        for m in run.get("mutants", []):
            if not m.get("applied"):
                continue
            cat = m.get("category", "?")
            by_cat_stats[cat]["mutants"] += 1
            if m.get("caught_by"):
                by_cat_stats[cat]["caught"] += 1
    out: list[dict] = []
    for cat, s in sorted(by_cat_stats.items()):
        if s["caught"] < s["mutants"]:
            out.append({
                "category": cat,
                "mutants": s["mutants"],
                "caught_by_any_check": s["caught"],
                "recommendation": ("add_annotation_check_or_accept_blindspot"
                                   if cat == "M5" else "investigate_coverage_gap"),
            })
    return out


def compute_mutant_coverage(runs: list[dict]) -> tuple[list[dict], list[dict]]:
    """distinct mutant 단위 **합집합** 커버리지 — "이 결함을 어디서든 잡는가".

    ## 왜 필요한가 (2026-09-05 실측)

    ``compute_blind_spots`` 는 mutant **인스턴스**(run × mutant)를 센다. 그런데 두 감사
    단계는 서로 다른 검증기 집합을 돌린다:

        S4.5  syntax / quality / hermit / classify / shacl   (T-Box 5종)
        S9.5  validate_kg 25 check                           (KG)

    그래서 한쪽만 잡는 결함이 **양쪽 기록에 각각 한 번씩** 세어져 비율이 절반쯤으로
    희석된다. 실측: ``M6/swap_range`` 는 S9.5 가 잡고 S4.5 는 놓치는데, 카테고리 M6 는
    3/16 으로 보여 "coverage_gap 조사" 권고가 붙는다 — 실제로는 그 mutant 가 커버된다.

    반대로 **어느 단계도 잡지 못하는** mutant 는 카테고리 비율에 묻힌다. 그것이
    이 감사가 답해야 하는 유일한 질문이므로 별 축으로 낸다.

    Returns:
        ``(mutant_coverage, uncovered)`` — 앞은 distinct mutant 전체, 뒤는 어느
        단계에서도 잡히지 않은 것만. 둘 다 mutant_id 로 정렬한다.
    """
    agg: dict[str, dict] = {}
    for run in runs:
        for m in run.get("mutants", []):
            if not m.get("applied"):
                continue
            mid = m.get("mutant_id", "?")
            entry = agg.setdefault(mid, {
                "mutant_id": mid,
                "category": m.get("category", "?"),
                "applied_runs": 0,
                "caught_runs": 0,
                "caught_by_stages": set(),
                "seen_stages": set(),
                "blinded_checks": set(),
            })
            entry["applied_runs"] += 1
            stage = m.get("stage") or "?"
            entry["seen_stages"].add(stage)
            if m.get("caught_by"):
                entry["caught_runs"] += 1
                entry["caught_by_stages"].add(stage)
            entry["blinded_checks"].update(m.get("blinded_checks") or [])

    coverage: list[dict] = []
    for mid in sorted(agg):
        e = agg[mid]
        coverage.append({
            "mutant_id": mid,
            "category": e["category"],
            "applied_runs": e["applied_runs"],
            "caught_runs": e["caught_runs"],
            # 어느 단계에서든 한 번이라도 잡혔으면 커버된 것으로 본다.
            "covered": bool(e["caught_by_stages"]),
            "caught_by_stages": sorted(e["caught_by_stages"]),
            "seen_stages": sorted(e["seen_stages"]),
            "blinded_checks": sorted(e["blinded_checks"]),
        })
    uncovered = [c for c in coverage if not c["covered"]]
    return coverage, uncovered


def compute_pairwise_correlation(
    runs: list[dict],
    history: list[dict],
    min_samples: int = 5,
    threshold: float = 0.9,
) -> list[dict]:
    """Pairs of checks that FAIL together >= threshold fraction of the time.

    Co-occurrence signal comes from (a) history entries' failed_checks field,
    (b) mutation runs' caught_by sets.
    """
    # Track signal sources separately so we can distinguish "these two checks are
    # genuinely redundant in production" (history signal) from "these two share a
    # mutant catcher" (synthetic signal). A pair only earns `redundant_candidate`
    # when history carries ≥ min_samples; mutation-only pairs get a weaker
    # `cooccurrence_candidate` label so humans reviewing recommendations see the
    # difference.
    pair_cooccur_history: dict[tuple[str, str], int] = defaultdict(int)
    pair_cooccur_mutation: dict[tuple[str, str], int] = defaultdict(int)
    single_fire_history: dict[str, int] = defaultdict(int)
    single_fire_mutation: dict[str, int] = defaultdict(int)

    def _observe(fail_set: list[str], *, source: str):
        fail_set = sorted(set(fail_set))
        singles = single_fire_history if source == "history" else single_fire_mutation
        pairs = pair_cooccur_history if source == "history" else pair_cooccur_mutation
        for c in fail_set:
            singles[c] += 1
        for a, b in combinations(fail_set, 2):
            pairs[(a, b)] += 1

    for h in history:
        fails = h.get("failed_checks") or []
        if fails:
            _observe(fails, source="history")
    for run in runs:
        for m in run.get("mutants", []):
            c = m.get("caught_by") or []
            if c:
                _observe(c, source="mutation")

    out: list[dict] = []
    all_pairs = set(pair_cooccur_history) | set(pair_cooccur_mutation)
    for (a, b) in all_pairs:
        hist_min = min(single_fire_history[a], single_fire_history[b])
        mut_min = min(single_fire_mutation[a], single_fire_mutation[b])
        hist_co = pair_cooccur_history[(a, b)]
        mut_co = pair_cooccur_mutation[(a, b)]

        # Prefer history signal when it clears min_samples
        if hist_min >= min_samples and hist_co / hist_min >= threshold:
            out.append({
                "pair": [a, b],
                "cooccurrence": round(hist_co / hist_min, 3),
                "sample_size": hist_min,
                "source": "history",
                "interpretation": "redundant_candidate",
            })
        elif mut_min >= min_samples and mut_co / mut_min >= threshold:
            out.append({
                "pair": [a, b],
                "cooccurrence": round(mut_co / mut_min, 3),
                "sample_size": mut_min,
                "source": "mutation",
                "interpretation": "cooccurrence_candidate",
            })
    return sorted(out, key=lambda p: -p["cooccurrence"])


#: 한 check 를 "죽었다" 고 말하기 위해 요구하는 **최소 노출 횟수**.
#:
#: ``caught_by`` 가 0 인 것은 두 가지를 뜻할 수 있다: (1) 그 check 가 결함을 못 잡는다,
#: (2) 그 check 를 자극하는 mutant 가 애초에 적용되지 않았다. 둘을 구별하지 않으면
#: 표본 부족이 곧 사형 선고가 된다.
#:
#: 실측 (2026-08-25): S9.5 KG 단계는 applied mutant 가 **2개** 뿐인데 21개 KG check
#: 전부가 그 2개만 겪었다. 그 상태로 판정하면 26개 중 23~24개가 dead 로 지목되고,
#: 그중 오탐이 아닌 것은 사실상 1개다 (오탐률 ≈ 95%). 지목된 목록에는 ``FK 참조
#: 무결성``·``domain/range 타입 정합성``·``카디널리티 제약 위반``·``AllDisjointClasses
#: 위반`` 같은 핵심 게이트가 들어 있었다.
#: 실측 (2026-08-25): 적용된 mutant 는 **삭제 27 / 추가 2** 로 삭제가 압도한다.
#: OWL 은 단조(monotonic) 논리라 **공리를 지우면 함의가 약해질 뿐** 이다 — 삭제로는
#: HermiT 가 unsatisfiable 을 보고할 수 없다. 그래서 삭제 mutant 를 노출로 세면
#: ``hermit``/``classify`` 가 27회 노출로 임계를 넘어 ``consider_removal`` 로 샜다.
#: 둘 다 오탐이고, 특히 ``hermit`` 은 이 리포에서 가장 하중이 큰 T-Box 게이트다
#: (HermiT 침묵이 unsat 4건을 덮은 이력이 있다).
#:
#: 그래서 노출은 **온톨로지를 강화하는 mutant** (트리플을 추가하는 것) 만 센다.
_MIN_MUTATION_EXPOSURE = 8

#: mutation 카탈로그가 **자극할 수 없는** check — 노출 횟수와 무관하게 판정 불가.
#:
#: mutation 은 전부 SPARQL UPDATE 로 만들어지므로 산출물이 항상 유효한 Turtle 이다.
#: 따라서 ``syntax`` (TTL 구문 검증) 는 어떤 mutant 로도 발화하지 않는다 — 그것은
#: 검증이 죽은 것이 아니라 **그 축의 mutant 가 카탈로그에 없는** 것이다.
#: 실측 (2026-08-25): 적용된 mutant 카테고리는 M1~M7 뿐이고 구문 손상 축이 없다.
#:
#: 이 목록에 넣으면 ``no_probe_available`` 로 보고하고 제거를 권고하지 않는다.
#: 구문 축을 실제로 감사하려면 mutation 카탈로그에 손상 케이스를 추가해야 한다.
_UNSTIMULABLE_BY_MUTATION: frozenset[str] = frozenset({"syntax"})


def compute_dead_checks(
    runs: list[dict],
    history: list[dict],
    checks_seen: set[str],
) -> list[dict]:
    """Checks that never fired in history AND never caught a mutant.

    **노출량을 분모로 요구한다.** 아래 두 축을 구별해 보고한다:

    - ``insufficient_exposure`` — 그 check 가 겪은 applied mutant 가
      :data:`_MIN_MUTATION_EXPOSURE` 미만이다. 판정 불가이므로 제거를 권고하지
      **않는다**.
    - ``structurally_undetectable`` — baseline 이 이미 FAIL 이라 등급 상승을 만들 수
      없다. ``_caught_by`` 는 PASS→WARN/FAIL 강등만 세므로 이 상태에서는 어떤
      mutant 도 이 check 로 검출되지 않는다. 고칠 대상은 check 가 아니라 baseline 이다.
    - ``consider_removal`` — 충분히 노출됐고 baseline 도 건강한데 한 번도 발화하지
      않았다. 이때만 제거 후보다.

    ## 왜 history 신호를 단독으로 믿지 않는가

    ``history_fires`` 는 "그 check 가 **실패한** 횟수" 다 — 실행 여부가 아니라 실패
    여부다. 그래서 **KG 가 건강할수록 이 신호가 0 에 가까워진다.** 실측: 완벽하게
    건강한 이력(23/23 × 10회)을 넣으면 26개 중 **24개** 가 dead 로 지목됐다.
    목표가 건강한 KG 인데 건강해질수록 검증 제거를 권고하는 것은 원리적 결함이므로,
    history 는 **보조 신호** 로만 쓰고 노출량·baseline 축을 함께 본다.
    """
    history_fires: dict[str, int] = defaultdict(int)
    signal_entries = 0
    for h in history:
        if "failed_checks" not in h:
            # ``pipeline_state`` writer 의 ``{step, metrics, duration_seconds}``
            # 스키마는 이 신호를 담지 않는다. 분모에 세면 "실효 신호 5건" 이 아니라
            # "엔트리 5건" 을 세게 되어, save_step 만으로 게이트가 열린다.
            continue
        signal_entries += 1
        for c in h.get("failed_checks") or []:
            history_fires[c] += 1

    mutation_catches: dict[str, int] = defaultdict(int)
    exposure: dict[str, int] = defaultdict(int)
    baseline_failing: dict[str, int] = defaultdict(int)
    for run in runs:
        for m in run.get("mutants", []):
            if not m.get("applied"):
                continue          # 심어지지 않은 결함은 노출이 아니다
            # 노출은 두 조건을 **모두** 만족할 때만 센다:
            #
            #  1. **강화(추가)** mutant 여야 한다. 삭제는 OWL 단조성 때문에 일관성
            #     계열 check 를 자극할 수 없다 (위 _MIN_MUTATION_EXPOSURE 주석).
            #  2. 그 mutant 의 **카테고리가 이 check 를 자극할 수 있어야** 한다.
            #     M5(label/comment) 를 9번 돌린 run 에서 ``FK 참조 무결성`` 이
            #     "9회 시험했는데 못 잡았다" 로 집계되면 안 된다 — 그 mutant 는
            #     애초에 FK 축을 건드리지 않는다. ``_MUTATION_CATEGORY_EXPECTED_CATCHERS``
            #     가 이미 그 관련성 표를 갖고 있으므로 그것을 분모에 쓴다.
            #
            # 표에 없는 카테고리는 관련성을 알 수 없으므로 **세지 않는다** (보수적).
            # 노출이 과소 집계되면 판정이 "insufficient_exposure" 로 남아 제거를
            # 권고하지 않는다 — 오탐보다 미측정이 안전하다.
            strengthens = bool(m.get("triples_added"))
            relevant = set(
                _MUTATION_CATEGORY_EXPECTED_CATCHERS.get(m.get("category") or "", ()),
            )
            for c, status in (m.get("baseline_summary") or {}).items():
                if strengthens and c in relevant:
                    exposure[c] += 1
                if status != "PASS":
                    baseline_failing[c] += 1
            for c in m.get("caught_by") or []:
                mutation_catches[c] += 1

    dead: list[dict] = []
    for c in sorted(checks_seen):
        if history_fires[c] or mutation_catches[c]:
            continue                          # 한 번이라도 발화했다 → 살아 있다
        seen = exposure[c]
        if c in _UNSTIMULABLE_BY_MUTATION:
            verdict = "no_probe_available"
        elif baseline_failing[c] and baseline_failing[c] == seen:
            verdict = "structurally_undetectable"
        elif seen < _MIN_MUTATION_EXPOSURE:
            verdict = "insufficient_exposure"
        else:
            verdict = "consider_removal"
        dead.append({
            "check": c,
            "history_fires": 0,
            "mutation_catches": 0,
            "mutation_exposure": seen,
            "baseline_failing_runs": baseline_failing[c],
            "history_signal_entries": signal_entries,
            "recommendation": verdict,
        })
    return dead


# X3: Mutation category → expected catchers 매핑.
# `rules/mutations/M{1..7}_*/` SPARQL UPDATE 카탈로그가 무엇을 변조하는지 분석해
# 논리적으로 어떤 check 가 잡을 수 있는지 고정 매핑.
#
# 매핑 값은 **실제 `caught_by` 리스트에 나타나는 identifier** 여야 한다:
#   - T-Box mutation (S4.5): `_run_tbox_validators` 의 짧은 별칭
#     → "syntax" / "quality" / "hermit" / "classify" / "shacl"
#     (tools/mutation_runner.py:207-217)
#   - KG mutation (S9.5): `validate_kg` check dict 의 한국어 display name
#     → e.g. "FK 참조 무결성", "ObjectProperty 양방향 연결"
#     (tools/validation_support/checks/*.py 의 {"name": ...})
#
# fake function-style 이름 (e.g. "domain_range_conformance") 을 쓰면
# sensitivity_matrix / caught_by 와 cross-reference 되지 않아 실제로 catch 여부를
# 판단할 수 없다. cross-reference 검증은
# tests/test_x3_action_playbook_integration.py::test_expected_checks_are_real_check_names
# 가 강제한다. 매핑 튜닝 필요 시 이 상수만 수정.
_MUTATION_CATEGORY_EXPECTED_CATCHERS: dict[str, list[str]] = {
    # M1 domain/range: T-Box HermiT/SHACL (일관성 붕괴 시) + KG domain/range 체크
    "M1": ["hermit", "shacl", "domain/range 타입 정합성"],
    # M2 inverseOf: T-Box HermiT + KG 양방향 연결 체크
    "M2": ["hermit", "ObjectProperty 양방향 연결"],
    # M3 cardinality/functional: T-Box HermiT/SHACL + KG Functional/Cardinality
    "M3": ["hermit", "shacl",
           "Functional Property 위반", "카디널리티 제약 위반"],
    # M4 disjoint: T-Box HermiT/SHACL + KG AllDisjoint 체크 (M4 는 S9.5 비대상이나
    # 방어적으로 display name 포함 — 향후 KG 런에 포함될 경우 즉시 잡힘)
    "M4": ["hermit", "shacl", "AllDisjointClasses 위반"],
    # M5 annotation (label/comment): quality rules 가 missing_label/comment 감지
    #   (S4.5 T-Box 전용 — A-Box 로 전파 안 되므로 KG display name 없음)
    "M5": ["quality"],
    # M6 reference: 이름이 "reference" 라 FK 계열을 넣어 뒀지만, **세 mutator 는 전부
    #   T-Box 의 rdfs:range/domain 만 변조한다** (2026-09-05 실측, rules/mutations/M6_*):
    #
    #     add_self_loop      range := domain      (T-Box)
    #     rename_to_unknown  range := 미존재 IRI  (T-Box)
    #     swap_range         range := 외래 클래스 (T-Box)
    #
    #   A-Box FK 트리플을 건드리지 않으므로 ``FK 참조 무결성``(A-Box OP 대상이 실재
    #   하는가) / ``댕글링 참조 탐지`` / ``Closed-World FK 미해결`` 을 **자극할 수 없다.**
    #   그것들을 여기 넣어 두면 dead-check 이 "13회 시험했는데 못 잡았다" 로 집계해
    #   **핵심 게이트를 제거 후보로 인쇄한다** (실측: FK 참조 무결성 노출 13 / 검출 0
    #   → consider_removal). 이 파일의 위 주석이 M5 사례로 경고한 것과 같은 오류다.
    #
    #   실제로 잡은 것을 넣는다 (같은 실측의 caught_by):
    #     rename_to_unknown → 스키마 참조 무결성
    #     swap_range        → domain/range 타입 정합성
    #     add_self_loop     → (아무도 못 잡음 — 별건 탐지기 갭)
    "M6": ["syntax", "quality",
           "스키마 참조 무결성", "domain/range 타입 정합성"],
    # M7 restriction: someValuesFrom / onClass 변조 → T-Box HermiT/SHACL
    #   (S4.5 전용 — KG 카테고리 아님)
    "M7": ["hermit", "shacl"],
}


def compute_action_playbook(
    blind_spots: list[dict],
    dead_checks: list[dict],
    pairwise_correlation: list[dict],
    uncovered_mutants: list[dict] | None = None,
) -> list[dict]:
    """Convert meta_audit findings into prioritised, actionable suggestions.

    Each entry carries priority (high/medium/low), type, issue description, and
    a suggested_action dict whose ``kind`` is either ``extend_check``,
    ``investigate_or_remove``, or ``consolidate_or_keep``. No auto-patch / no
    LLM — human review is required to act on any suggestion.

    Priority rules:
        - blind_spot with caught_by_any_check == 0 → high
        - blind_spot with catch_ratio < 0.5 (partial, < 50%) → medium
        - blind_spot with catch_ratio >= 0.5 (partial, 50%+) → low
        - blind_spot fully caught → excluded (defensive; upstream filters too)
        - dead_check → low
        - pairwise_correlation with interpretation == "redundant_candidate" → low
        - cooccurrence_candidate (weaker signal) → excluded

    Unknown category (매핑에 없음) 의 경우 ``catalog_path`` /
    ``investigation_hint`` 는 오해 소지가 있는 경로를 만들지 않도록 생략한다.
    """
    playbook: list[dict] = []

    # 0) 어느 단계도 잡지 못한 distinct mutant — **가장 정확한 조치 목록**.
    #
    # 카테고리 비율(blind_spots)은 두 감사 단계가 서로 다른 검증기를 돌리기 때문에
    # 희석된다: 한쪽만 잡는 결함이 양쪽 기록에 각각 세어져 ~50% 로 보인다. 반면
    # 여기 실리는 것은 "windows 안에서 어느 단계에서도 한 번도 안 잡힌" mutant 다 —
    # 그것이 곧 실제 사각지대다 (``compute_mutant_coverage`` docstring).
    for mc in uncovered_mutants or []:
        blinded = mc.get("blinded_checks") or []
        playbook.append({
            "priority": "high",
            "type": "uncovered_mutant",
            "mutant_id": mc.get("mutant_id"),
            "category": mc.get("category"),
            "issue": (
                f"{mc.get('mutant_id')} 는 적용 {mc.get('applied_runs')}회 동안 "
                f"**어느 단계에서도** 잡히지 않았다 (본 단계: "
                f"{', '.join(mc.get('seen_stages') or []) or '?'})"
                + (f". 대신 가드를 눈멀게 했다: {', '.join(blinded)}" if blinded else "")
            ),
            "suggested_action": {
                "kind": "extend_check",
                "investigation_hint": (
                    "먼저 이 mutant 가 **관측 가능한** 결함인지 판정하라 — 표적 술어가 "
                    "A-Box 0건이거나 공리를 제거만 하는 변조는 위반 축으로 볼 수 없다 "
                    "(S9.5 응답의 uncaught_reasons 참조). 관측 가능한데 안 잡히면 "
                    "그것이 진짜 탐지기 갭이다."
                ),
                "blinded_checks": blinded,
            },
            "playbook_reference": "post-workshop-guide.md §11 Step 2 (blind spot)",
        })

    # 1) Blind spots — map to expected catchers and classify priority
    for bs in blind_spots:
        mutants = bs.get("mutants", 0)
        caught = bs.get("caught_by_any_check", 0)
        # Defensive: exclude fully-caught categories even if caller passes them
        if mutants > 0 and caught >= mutants:
            continue
        cat = bs.get("category", "?")
        expected = _MUTATION_CATEGORY_EXPECTED_CATCHERS.get(cat, [])
        # 3단계 priority — catch ratio 기반.
        # 0 catch = 완전 blind, < 50% = 시급한 개선, >= 50% = 여유 있음.
        if caught == 0:
            priority = "high"
        elif mutants > 0 and caught / mutants < 0.5:
            priority = "medium"
        else:
            priority = "low"

        # Unknown category: investigation_hint / catalog_path 생략.
        # 매핑에 없는 카테고리는 사용자가 직접 분류해야 하며, 임의 경로를 제시하면
        # 존재하지 않는 디렉토리로 안내할 위험이 있음.
        if expected:
            suggested_action = {
                "kind": "extend_check",
                "investigation_hint": (
                    f"기대 catcher {expected} 중 하나를 확장하거나, "
                    f"rules/mutations/{cat}_* 패턴을 분석해 신규 check 설계"
                ),
                "catalog_path": f"rules/mutations/{cat}_*/*.sparql",
            }
        else:
            suggested_action = {"kind": "extend_check"}

        playbook.append({
            "priority": priority,
            "type": "blind_spot",
            "category": cat,
            "issue": (
                f"{cat} mutant {mutants}개 중 {caught}개만 감지됨"
            ),
            "expected_checks": expected,
            "suggested_action": suggested_action,
            "playbook_reference": (
                "post-workshop-guide.md §11 Step 2 (partial blind-spot)"
            ),
        })

    # 2) Dead checks — 판정별로 **다른 액션** 을 제안한다.
    #
    # 예전에는 모든 항목에 ``investigate_or_remove`` 를 붙였다. 그러면 표본 부족으로
    # 0 catch 인 것과 정말 죽은 것이 같은 권고를 받고, 그 권고가 S13 HTML 보고서에
    # "제거 후보" 로 인쇄된다. 실측 (2026-08-25): 그 목록에 ``FK 참조 무결성``·
    # ``domain/range 타입 정합성``·``카디널리티 제약 위반``·``AllDisjointClasses 위반``
    # 같은 핵심 게이트가 들어 있었다 (오탐률 ≈ 95%).
    _DEAD_ACTIONS = {
        "insufficient_exposure": {
            "kind": "increase_mutation_coverage",
            "investigation_question": (
                "이 check 축을 자극하는 mutant 가 몇 개 적용됐는가? "
                "노출이 부족하면 판정 자체가 불가능하다."
            ),
            "keep_if": "판정 불가 — 제거 검토 대상이 아니다",
            "remove_if": "(해당 없음)",
        },
        "structurally_undetectable": {
            "kind": "fix_baseline_first",
            "investigation_question": (
                "baseline 이 이미 FAIL 인가? 그렇다면 등급 상승이 불가능해 "
                "어떤 mutant 도 이 check 로 검출되지 않는다."
            ),
            "keep_if": "baseline 을 고치면 검출력이 돌아온다",
            "remove_if": "(해당 없음 — 고칠 대상은 check 가 아니라 baseline 이다)",
        },
        "no_probe_available": {
            "kind": "add_mutation_category",
            "investigation_question": (
                "이 check 를 자극하는 결함 유형이 mutation 카탈로그에 있는가?"
            ),
            "keep_if": "카탈로그에 그 축의 mutant 가 없을 뿐이다",
            "remove_if": "(해당 없음)",
        },
        "consider_removal": {
            "kind": "investigate_or_remove",
            "investigation_question": (
                "check 가 기대하는 트리거 조건이 현재 도메인/데이터에서 "
                "발생 가능한가?"
            ),
            "keep_if": "회귀 방어용으로 남겨둘 가치 있는 edge case check",
            "remove_if": "영구히 trigger 될 일 없음 (deprecated T-Box 패턴 등)",
        },
    }
    for dc in dead_checks:
        verdict = dc.get("recommendation", "consider_removal")
        action = _DEAD_ACTIONS.get(verdict, _DEAD_ACTIONS["consider_removal"])
        playbook.append({
            "priority": "low",
            "type": "dead_check",
            "check": dc.get("check"),
            "verdict": verdict,
            "issue": (
                f"check '{dc.get('check')}' 가 history + mutation 양쪽에서 0 catch "
                f"(노출 {dc.get('mutation_exposure', 0)}회, "
                f"판정: {verdict})"
            ),
            "suggested_action": action,
            "playbook_reference": (
                "post-workshop-guide.md §11 Step 2 (dead check)"
            ),
        })

    # 3) Redundant pairs — consolidate vs keep decision criteria
    for pair in pairwise_correlation:
        # Only redundant_candidate (history-backed) qualifies; weaker
        # cooccurrence_candidate signal is skipped to avoid noise.
        if pair.get("interpretation") != "redundant_candidate":
            continue
        pair_names = pair.get("pair", ["?", "?"])
        cooc = pair.get("cooccurrence", 0)
        playbook.append({
            "priority": "low",
            "type": "correlation_gap",
            "pair": pair_names,
            "issue": (
                f"{pair_names[0]} 와 {pair_names[1]} 가 "
                f"{int(cooc * 100)}% 동시 FAIL "
                f"(n={pair.get('sample_size')})"
            ),
            "suggested_action": {
                "kind": "consolidate_or_keep",
                "investigation_question": (
                    "두 check 가 의미론적으로 같은 결함을 검출하는가? "
                    "데이터상 같이 FAIL 하는 게 우연인가 필연인가?"
                ),
                "consolidate_if": "의미 중복 + 실행 시간 절약 가치 큼",
                "keep_if": "한쪽이 다른 쪽이 못 잡는 edge case 커버",
            },
            "playbook_reference": (
                "post-workshop-guide.md §11 Step 2 (correlation gap)"
            ),
        })

    # Priority ordering: high → medium → low, then type for stability
    priority_order = {"high": 0, "medium": 1, "low": 2}
    playbook.sort(key=lambda p: (
        priority_order.get(p.get("priority", "low"), 9),
        p.get("type", ""),
    ))
    return playbook


def compute_roi(
    runs: list[dict],
    history: list[dict],
) -> list[dict]:
    """For each check, catches-per-second if duration data available.

    FAIL transitions weighted 2x WARN transitions.
    """
    weighted_catches: dict[str, float] = defaultdict(float)
    durations: dict[str, list[float]] = defaultdict(list)
    for run in runs:
        for m in run.get("mutants", []):
            if not m.get("applied"):
                continue
            dur = m.get("duration_s", 0.0)
            base = m.get("baseline_summary", {})
            mut = m.get("mutant_summary", {})
            for check in m.get("caught_by") or []:
                w = 2.0 if mut.get(check) == "FAIL" and base.get(check) != "FAIL" else 1.0
                weighted_catches[check] += w
                durations[check].append(dur)
    out: list[dict] = []
    for c in sorted(weighted_catches):
        avg = sum(durations[c]) / max(1, len(durations[c]))
        out.append({
            "check": c,
            "weighted_catches": weighted_catches[c],
            "avg_duration_s": round(avg, 3),
            "catches_per_sec": round(weighted_catches[c] / max(avg, 0.001), 3),
        })
    return out


def _load_mutation_runs(runs_root: str) -> list[dict]:
    """Load all tbox.json and kg.json under runs_root."""
    runs: list[dict] = []
    for path in sorted(glob.glob(os.path.join(runs_root, "*", "*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                runs.append(json.load(f))
        except (json.JSONDecodeError, OSError):
            continue
    return runs


def _load_history(history_path: str, window_commits: int) -> list[dict]:
    if not os.path.exists(history_path):
        return []
    try:
        with open(history_path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    return data[-window_commits:]


def run_meta_audit_impl(
    runs_root: str,
    history_path: str,
    out_path: str,
    window_commits: int = 30,
    min_history: int = 5,
) -> dict:
    runs = _load_mutation_runs(runs_root)
    history = _load_history(history_path, window_commits)

    checks_seen: set[str] = set()
    for r in runs:
        for m in r.get("mutants", []):
            for c in m.get("baseline_summary", {}):
                checks_seen.add(c)

    insufficient = len(history) < min_history
    # Compute each derived artifact once and reuse for action_playbook to avoid
    # double evaluation (X3: run_meta_audit_impl 통합).
    blind_spots_result = compute_blind_spots(runs)
    # distinct mutant 단위 합집합 — 카테고리 비율이 두 감사 단계 때문에 희석되는 것을
    # 보정한다 (``compute_mutant_coverage`` docstring). history 부족과 무관하게 낸다:
    # mutation run 만으로 계산되므로 min_history 게이트를 적용할 이유가 없다.
    mutant_coverage, uncovered_mutants = compute_mutant_coverage(runs)
    pairwise_result = (
        [] if insufficient
        else compute_pairwise_correlation(runs, history)
    )
    dead_checks_result = (
        [] if insufficient
        else compute_dead_checks(runs, history, checks_seen)
    )
    artifact = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "window_commits": window_commits,
        "history_entries": len(history),
        "history_insufficient": insufficient,
        "runs_consumed": len(runs),
        "sensitivity_matrix": compute_sensitivity_matrix(runs),
        "blind_spots": blind_spots_result,
        # 인스턴스 기준(blind_spots) 과 distinct mutant 합집합 기준을 **둘 다** 낸다.
        # 앞은 추세용, 뒤는 "이 결함을 어디서든 잡는가" 의 답이다.
        "mutant_coverage": mutant_coverage,
        "uncovered_mutants": uncovered_mutants,
        "mutant_coverage_summary": {
            "distinct_applied": len(mutant_coverage),
            "covered": len(mutant_coverage) - len(uncovered_mutants),
            "uncovered": len(uncovered_mutants),
        },
        "pairwise_correlation": pairwise_result,
        "dead_checks": dead_checks_result,
        "roi_column": compute_roi(runs, history),
        "action_playbook": compute_action_playbook(
            blind_spots_result, dead_checks_result, pairwise_result,
            uncovered_mutants,
        ),
    }
    if insufficient:
        artifact["notes"] = {
            "correlation": "insufficient_history",
            "dead_checks": "insufficient_history",
        }

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(artifact, f, ensure_ascii=False, indent=2, sort_keys=True)

    # Update latest.json symlink (skip on Windows)
    latest = os.path.join(os.path.dirname(out_path), "latest.json")
    try:
        if os.path.islink(latest) or os.path.exists(latest):
            os.unlink(latest)
        os.symlink(os.path.basename(out_path), latest)
    except OSError:
        with open(latest, "w", encoding="utf-8") as f:
            json.dump(artifact, f, ensure_ascii=False, indent=2, sort_keys=True)

    return artifact


def run_meta_audit(window_commits: int = 30) -> str:
    """Run meta-audit against all mutation runs + last N quality-history entries.

    Writes data/generated/meta_audit/<timestamp>.json + latest.json.
    """
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_dir = os.path.join(GENERATED_DIR, "meta_audit")
    out_path = os.path.join(out_dir, f"{ts}.json")
    runs_root = os.path.join(out_dir, "runs")
    history_path = os.path.join(GENERATED_DIR, "quality_history.json")
    result = run_meta_audit_impl(runs_root, history_path, out_path, window_commits)
    return json.dumps(result, ensure_ascii=False, indent=2)


def get_meta_audit_history() -> str:
    """List all past meta-audit runs, newest first."""
    out_dir = os.path.join(GENERATED_DIR, "meta_audit")
    files = sorted(glob.glob(os.path.join(out_dir, "*.json")), reverse=True)
    files = [f for f in files if not f.endswith("latest.json")]
    return json.dumps([os.path.basename(f) for f in files], ensure_ascii=False)


def read_meta_audit(timestamp: str = "latest") -> str:
    """Read a specific audit run. Default: latest.json.

    Args:
        timestamp: ``latest`` 또는 ``run_meta_audit`` 가 만든 ``YYYYMMDDTHHMMSSZ``
            형식. ``get_meta_audit_history`` 가 돌려주는 ``.json`` 파일명도 받는다.
            그 밖의 값과 meta_audit 디렉터리 밖을 가리키는 symlink 는 거부한다.
    """
    out_dir = os.path.join(GENERATED_DIR, "meta_audit")
    stem = timestamp.strip() if isinstance(timestamp, str) else ""
    if stem.endswith(".json"):
        stem = stem[: -len(".json")]
    if stem != "latest" and not _AUDIT_TIMESTAMP_RE.fullmatch(stem):
        return error_response(
            "timestamp 는 'latest' 또는 YYYYMMDDTHHMMSSZ 형식이어야 합니다."
        )
    try:
        path = resolve_child_path(out_dir, f"{stem}.json", allowed_suffixes=(".json",))
    except ValueError as exc:
        return error_response(exc)
    if not os.path.exists(path):
        return json.dumps({"error": "not_found", "path": path})
    with open(path, encoding="utf-8") as f:
        return f.read()
