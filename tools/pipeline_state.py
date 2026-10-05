"""파이프라인 체크포인트 — 단계별 완료 상태 + 입력 해시 추적."""

from __future__ import annotations

import glob
import hashlib
import json
import logging
import os
import time
from datetime import datetime

from config import (
    ABOX_PATH,
    COMPETENCY_QUESTIONS_PATH,
    GENERATED_DIR,
    GOLDEN_QUERIES_PATH,
    INFERRED_PATH,
    SEMANTIC_DICT_PATH,
    SOURCE_RAWDATA_DIR,
    SOURCE_TACIT_DIR,
    TBOX_PATH,
)
from tools.common import atomic_write

logger = logging.getLogger(__name__)

_STATE_PATH = os.path.join(GENERATED_DIR, "pipeline_state.json")

# 각 단계가 의존하는 입력 파일 키
_STEP_DEPS: dict[str, list[str]] = {
    "S0_CQ": ["csv_mtime"],
    "S1_DATA": ["csv_mtime"],
    # S2 는 CQ 정의와 rules/domain 다수(fk_patterns·disjoint_groups·design_patterns
    # ·table_class_mapping 등)를 프롬프트 입력으로 읽는다. 그 축이 없어 SME 가 CQ 나
    # 설정을 고쳐도 S2 가 skippable 이었다.
    "S2_TBOX": ["csv_mtime", "tacit_mtime", "cq_mtime", "rules_fingerprint"],
    # S3 는 CSV 를 직접 읽는 스텝이 여럿이다 (step_11b 클래스 합성 / step_12d DP 주입
    # / step_12i DP range 실측 / step_13b 카디널리티 실측) 그리고 step_29 는
    # property_chains.json, step_30 은 tbox_manual_additions.ttl 을 병합한다.
    "S3_IMPROVE": ["tbox_mtime", "tacit_mtime", "csv_mtime", "rules_fingerprint"],
    # SHACL shape 정본은 rules/policy/tbox_shapes.ttl 이다.
    "S4_VALIDATE": ["tbox_mtime", "rules_fingerprint"],
    # mutant 카탈로그는 rules/mutations/**/*.sparql 이다.
    "S4_5_MUTATION": ["tbox_mtime", "rules_fingerprint"],
    # S5 의 축이 **자기 출력 하나뿐** 이었다. 생성기는 규칙이 참조하는 CSV 를 읽고
    # rules/domain/tacit_rules.json 을 읽는다. 실측 2026-09-02: tacit_rules.json 이
    # 2026-08-30 에 수정됐는데 어느 축에도 없어 S5 가 영구 skippable 이었다.
    #
    # ⚠️ tbox_mtime 은 넣지 않는다. S2→S3 가 매 실행 T-Box 를 재작성하므로 S5 가
    #    항상 재실행 대상이 되고, S5 는 T-Box 를 op 방향 검증에만 쓴다. T-Box 소비부
    #    지문이 필요하면 별 축으로 만들어야 한다 (여기서는 범위 밖).
    "S5_TACIT": ["tacit_mtime", "csv_mtime", "rules_fingerprint"],
    "S6_VIS": ["tbox_mtime"],
    # 기능 단계 — 딕셔너리 선행 단계. T-Box 만 읽어 vocabulary contract 생성
    # (include_stats=False). A-Box 생성기가 이 contract 를 참조해 class-specific
    # DP 이름을 강제 준수. 파일은 SEMANTIC_DICT_PATH 하나 — S10 에서 v2 (통계
    # 포함) 로 덮어쓰기.
    "S6_5_DICT_V1": ["tbox_mtime"],
    # R1B: S7 은 vocabulary contract 를 의존한다 — 다만 **파일 mtime 이 아니라
    # contract 내용의 지문**이다. S10 이 같은 파일을 v2 로 덮어쓰므로 mtime 을 쓰면
    # A-Box 를 건드리지 않았는데도 S7 이 영구 무효화됐다 (2026-08-30 격리 실험).
    # tacit_mtime 추가 근거(실측 2026-09-02): generate_abox 가 tacit TTL 을
    # a_box.ttl 에 **병합** 한다. a_box.ttl 의 관계 트리플 90,094 중 51,223(56.9%)이
    # tacit 유래이고, 15개 술어 전부 A-Box 독자 생성분이 0건이다. 따라서 tacit 편집은
    # A-Box 를 바꾸는 입력이다.
    "S7_ABOX": ["tbox_mtime", "csv_mtime", "tacit_mtime", "dict_contract_fingerprint"],
    "S8_INFERENCE": ["tbox_mtime", "abox_mtime", "tacit_mtime"],
    # S8.5 SWRL (opt-in via SWRL_ENABLED=true) — OWL RL 추론 이후 SWRL 규칙을
    # Pellet 으로 적용해 all_inferred.ttl 에 파생 triple 을 append.
    # rules/swrl/*.swrl 파일 변경 시 재실행. SWRL_ENABLED=false 이면 no-op.
    "S8_5_SWRL": ["inferred_mtime", "swrl_mtime"],
    # S9_KG_VALIDATE: 기본 모드(``use_inferred=False``)의 ``load_graph`` 는
    # **T-Box + A-Box + tacit** 을 파싱하고 ``all_inferred.ttl`` 을 열지 않는다
    # (domain/tbox_utils.py::load_graph). 그런데 의존 축이 ``inferred_mtime`` 하나였다 —
    # 자기가 읽는 세 파일은 보지 않고, 읽지 않는 파일 하나만 봤다.
    # 실측: T-Box 만 touch 해도 ``skippable=True`` 로 보고돼, 세대가 어긋난 T-Box 로
    # 검증을 건너뛸 수 있었다. ``use_inferred=True`` 경로를 위해 inferred 도 유지한다.
    "S9_KG_VALIDATE": [
        "tbox_mtime", "abox_mtime", "tacit_mtime", "inferred_mtime",
        # 임계·규칙 정본: quality_thresholds / value_ranges / common_dp / fk_patterns.
        "rules_fingerprint",
    ],
    # S9_OWL_SANITY: HermiT consistency + OWL 2 profile + entailment 골든 셋 회귀.
    # validate_kg 의 선언적 체크가 놓치는 **논리적 모순 / 프로파일 위반 /
    # 기대 추론 회귀** 를 검출. WARN-only (S4.5/S9.5 와 동일 정책) — 실패해도
    # 파이프라인은 계속.
    # run_entailment_regression 의 골든 셋은 rules/domain/entailment_golden.json 이다.
    # (validate_owl_consistency 는 T-Box 전용이다 — owl_reasoner 에 ABOX_PATH 참조가
    #  0건이므로 abox_mtime 은 넣지 않는다.)
    "S9_OWL_SANITY": ["tbox_mtime", "inferred_mtime", "rules_fingerprint"],
    # S9_POST_MEASURE: 추론 결과의 5-메트릭 스코어(measure_instance_quality) +
    # 추론 delta 샘플 기록. 트렌드 추적 용도 — quality_history 에 기록되어
    # get_pipeline_quality_history 로 조회 가능.
    # 축이 inferred 하나였다. measure_instance_quality 는 기본 ``use_inferred=False``
    # 로 ``load_graph`` 를 부르고, 그것은 **T-Box + A-Box + tacit** 을 병합한다
    # (domain/tbox_utils.py::load_graph). 즉 자기가 읽는 세 파일이 축에 없었다 —
    # 00b973f 가 S9_KG_VALIDATE 에 대해 고친 것과 같은 결함의 형제다.
    # 실측 2026-09-02: T-Box 가 2일 앞선 상태에서 이 단계만 skippable=True 였고,
    # 그래서 96.2 점이 어느 세대의 값인지 알 수 없었다.
    "S9_POST_MEASURE": ["inferred_mtime", "tbox_mtime", "abox_mtime", "tacit_mtime"],
    # validate_kg 를 mutant 마다 재호출하므로 S9_KG_VALIDATE 와 같은 축을 갖는다.
    "S9_5_KG_MUTATION": [
        "inferred_mtime", "tbox_mtime", "abox_mtime", "tacit_mtime",
        "rules_fingerprint",
    ],
    "S10_DICT": ["tbox_mtime", "abox_mtime"],
    # 검증 대상이 딕셔너리 **본문** 이다. contract 지문(DP 이름 집합)만으로는
    # 통계·조인 힌트 변경을 못 본다.
    "S11_DICT_VALIDATE": ["tbox_mtime", "dict_fingerprint"],
    # S12 는 CQ 정의(COMPETENCY_QUESTIONS_PATH)와 딕셔너리를 읽고, 조인 검증 시
    # sparql_local._get_graph → load_graph 로 tacit 까지 병합한 그래프를 쓴다.
    "S12_QUERY_TEST": [
        "tbox_mtime", "abox_mtime", "tacit_mtime", "cq_mtime", "dict_fingerprint",
    ],
    # S12.5 — SME-frozen golden SPARQL 회귀. 있으면 자동 실행, 없으면 skip.
    # WARN-only: 실패해도 파이프라인은 S13 으로 진행.
    # golden_queries.json 이 없으면 golden_mtime = 0 이고, SME 가 처음 넣는 순간
    # 값이 생겨 이 단계가 정확히 그때 재실행 대상이 된다.
    "S12_5_GOLDEN_REGRESSION": [
        "tbox_mtime", "abox_mtime", "inferred_mtime", "golden_mtime",
    ],
    # report.py::_collect_tacit_stats 가 SOURCE_TACIT_DIR 을 훑는다.
    "S13_REPORT": ["tbox_mtime", "abox_mtime", "inferred_mtime", "tacit_mtime"],
    # Neo4j LPG (NEO4J_DEPLOY 워크플로우, 선택)
    "N1_LPG_CONVERT": ["inferred_mtime"],
    "N2_LPG_DICT": ["lpg_csv_mtime"],
    "N3_LPG_DEPLOY": ["lpg_csv_mtime"],
}

_ALL_STEPS = list(_STEP_DEPS.keys())


def _get_file_mtime(path: str) -> float:
    """파일 수정 시간을 반환. 없으면 0."""
    return os.path.getmtime(path) if os.path.exists(path) else 0


def _per_table_fingerprint() -> dict[str, tuple[float, int]]:
    """테이블별 (mtime, size) 지문. 일부 테이블만 바뀌었을 때 세분화 판정용.

    파일명(basename without .csv)을 키로 사용. 누락/접근 실패는 (0, 0).
    """
    out: dict[str, tuple[float, int]] = {}
    try:
        paths = sorted(glob.glob(os.path.join(SOURCE_RAWDATA_DIR, "*.csv")))
    except OSError:
        return out
    for p in paths:
        try:
            st = os.stat(p)
            name = os.path.basename(p)[:-4]  # strip .csv
            out[name] = (st.st_mtime, st.st_size)
        except OSError:
            continue
    return out


def changed_tables_since(prev_fp: dict | None) -> list[str]:
    """이전 상태 대비 변경된 테이블명 목록을 반환. prev_fp가 없으면 '전부 변경'.

    pipeline_state.json의 csv_per_table 필드와 비교용. save_step() 호출 직후 내부에서
    즉시 갱신되는 값.
    """
    curr = _per_table_fingerprint()
    if not prev_fp:
        return sorted(curr.keys())
    prev_normalized = {k: tuple(v) for k, v in prev_fp.items()}
    changed: list[str] = []
    for name, sig in curr.items():
        if prev_normalized.get(name) != sig:
            changed.append(name)
    # 이전에는 있었지만 삭제된 테이블도 변경 취급.
    for name in prev_normalized:
        if name not in curr:
            changed.append(name)
    return sorted(set(changed))


def _rules_fingerprint() -> str:
    """``rules/`` 아래 **엔진이 읽는** 설정 파일들의 내용 지문.

    왜 필요한가: SME 가 ``rules/domain/tacit_rules.json`` 이나
    ``tbox_manual_additions.ttl`` 을 고쳐도 어느 단계도 무효화되지 않았다 (실측
    2026-09-02: 축 목록에 rules 계열이 **0개**). ``tbox_manual_additions.ttl`` 은
    step_30 이 결정적으로 병합하는 SME 탈출구인데, 그것을 편집하고 S3 를
    건너뛰면 편집이 배포본에 도달하지 않는다.

    ⚠️ mtime 이 아니라 **(상대경로, size, mtime)** 를 정렬해 해시한다. 파일 하나가
    추가·삭제돼도 값이 바뀐다 — ``glob`` 이 0건을 정상 반환하는 함정
    (``rules_json_glob`` docstring 참조) 을 지문 축에서도 막기 위함이다.

    제외: ``*.example.*`` / ``*.suggested.json`` — 참조용 사본이라 편집이
    파이프라인 산출물에 영향을 주지 않는다. 이것을 넣으면 예시 파일 손질이
    S2(46분) 재실행을 요구한다.
    """
    from domain.rules_paths import RULES_ROOT, rules_json_glob

    paths: set[str] = set(rules_json_glob())
    # JSON 이 아닌 엔진 입력 — glob 이 ``*.json`` 전용이라 여기서 명시한다.
    for pattern in (
        os.path.join("domain", "tbox_manual_additions.ttl"),
        os.path.join("policy", "tbox_shapes.ttl"),
        os.path.join("mutations", "**", "*.sparql"),
    ):
        paths.update(glob.glob(os.path.join(RULES_ROOT, pattern), recursive=True))

    h = hashlib.sha256()
    for path in sorted(paths):
        name = os.path.basename(path)
        if ".example." in name or name.endswith(".suggested.json"):
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue
        rel = os.path.relpath(path, RULES_ROOT)
        h.update(f"{rel}:{st.st_size}:{st.st_mtime}\n".encode())
    return h.hexdigest()[:16]


def _dict_fingerprint() -> str:
    """시맨틱 딕셔너리 **본문 전체**의 지문.

    ``dict_contract_fingerprint`` 는 A-Box 생성기가 읽는 DP 이름 집합만 본다.
    S11(딕셔너리 검증)·S12(NL→SPARQL 참조)는 본문 전체를 소비하므로 축이 다르다.
    """
    try:
        with open(SEMANTIC_DICT_PATH, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()[:16]
    except OSError:
        return ""


def _dict_contract_fingerprint() -> str:
    """S7 이 딕셔너리에서 읽는 **vocabulary contract** 의 안정 지문.

    S6.5(v1) 와 S10(v2) 가 **같은 파일**(``SEMANTIC_DICT_PATH``) 을 쓰므로 파일
    mtime 을 의존 키로 쓰면 S7 이 자기 하위 단계에 의해 영구 무효화된다
    (``_current_input_state`` 의 ``dict_v1_mtime`` 주석 참조).

    S7 이 실제로 소비하는 것은 ``classes.*.datatype_properties`` 의 **이름 집합**
    이다 (``abox_generation._load_dict_contract``). v2 는 여기에 통계를 덧붙이지만
    이름 집합은 그대로이므로, 그 집합만 해싱하면:

      · v1 → v2 덮어쓰기: 지문 **불변** → A-Box 재생성 불필요 (정확)
      · S2 재생성으로 DP 이름 변경: 지문 **변경** → A-Box 재생성 필요 (정확)

    파일이 없거나 손상되면 빈 문자열 — 그 상태를 "지문 0" 같은 값과 구분한다.
    """
    if not os.path.exists(SEMANTIC_DICT_PATH):
        return ""
    try:
        with open(SEMANTIC_DICT_PATH, encoding="utf-8") as handle:
            data = json.load(handle)
        classes = data.get("classes") or {}
        if not isinstance(classes, dict):
            return ""
        parts: list[str] = []
        for cls_name in sorted(classes):
            info = classes[cls_name]
            if not isinstance(info, dict):
                continue
            props = info.get("datatype_properties") or {}
            if not isinstance(props, dict):
                continue
            parts.append(f"{cls_name}:{','.join(sorted(props))}")
        return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:16]
    except Exception as exc:  # noqa: BLE001 — 지문 실패가 파이프라인을 막지 않는다
        logger.warning("dict contract 지문 계산 실패: %s", exc)
        return ""


def _current_input_state() -> dict[str, object]:
    """현재 파이프라인 입력 파일들의 상태를 수집."""
    try:
        csv_mtime = max(
            (_get_file_mtime(f) for f in glob.glob(os.path.join(SOURCE_RAWDATA_DIR, "*.csv"))),
            default=0,
        )
    except OSError as e:
        logger.warning("CSV 디렉토리 접근 실패: %s", e)
        csv_mtime = 0
    try:
        tacit_mtime = max(
            (_get_file_mtime(f) for f in glob.glob(os.path.join(SOURCE_TACIT_DIR, "*.ttl"))),
            default=0,
        )
    except OSError as e:
        logger.warning("Tacit 디렉토리 접근 실패: %s", e)
        tacit_mtime = 0
    _lpg_nodes = os.path.join(GENERATED_DIR, "inferred", "neo4j", "nodes.csv")
    _lpg_rels = os.path.join(GENERATED_DIR, "inferred", "neo4j", "relationships.csv")
    # I4 — rules/swrl/*.swrl mtime (체크포인트 invalidation 용).
    try:
        from tools.swrl_inference import _get_swrl_mtime as _swrl_m
        swrl_mtime = _swrl_m()
    except Exception:
        swrl_mtime = 0.0
    return {
        "csv_mtime": csv_mtime,
        "tacit_mtime": tacit_mtime,
        "tbox_mtime": _get_file_mtime(TBOX_PATH),
        "abox_mtime": _get_file_mtime(ABOX_PATH),
        "inferred_mtime": _get_file_mtime(INFERRED_PATH),
        "swrl_mtime": swrl_mtime,
        # 기능 단계 — 시맨틱 딕셔너리 v1 의 mtime. S6.5 에서 저장된 후 S7 이 의존.
        #
        # ⚠️ **S7 의 의존 키로 쓰지 말 것** (2026-08-30 규명). S10 이 **같은 파일**을
        # v2 로 덮어쓰므로 이 값은 매 실행 갱신되고, 그러면 A-Box 를 한 트리플도
        # 건드리지 않았는데 S7 이 영구히 "재실행 필요" 가 된다 (격리 실험으로 확인:
        # S7 저장 직후 can_skip=True → S10 이 같은 경로에 쓰자 False). 예전 주석은
        # "같은 파일이라 mtime 갱신" 을 근거로 이 키를 S7 에 넣었는데, 그 갱신이
        # 바로 자기무효화의 원인이었다.
        #
        # 진단·표시용으로만 남긴다. S7 이 실제로 의존하는 것은 **contract 내용**
        # (``dict_contract_fingerprint``) 이다.
        "dict_v1_mtime": _get_file_mtime(SEMANTIC_DICT_PATH),
        # S7 이 딕셔너리에서 **실제로 읽는 것** 의 지문 —
        # ``classes.*.datatype_properties`` 의 이름 집합
        # (``abox_generation._load_dict_contract``). v1(통계 없음) → v2(통계 포함)
        # 로 덮어써도 이 집합은 같으므로 지문이 안정적이고, class-specific DP 이름이
        # 바뀌면 (S2 재생성 등) 값이 바뀌어 A-Box 재생성이 **정확히 그때만** 요구된다.
        "dict_contract_fingerprint": _dict_contract_fingerprint(),
        "lpg_csv_mtime": max(
            _get_file_mtime(_lpg_nodes), _get_file_mtime(_lpg_rels),
        ),
        # S2·S12 가 소비하는 CQ 정의 파일. **생산자(S0)에는 넣지 않는다** —
        # 넣으면 SME 가 손으로 넣은 CQ 를 S0 재생성이 덮어쓰는 방향이 된다.
        "cq_mtime": _get_file_mtime(COMPETENCY_QUESTIONS_PATH),
        # S12.5 골든 회귀의 입력. 파일이 없으면 0 이고, SME 가 처음 넣는 순간
        # 값이 생겨 S12.5 가 정확히 그때 재실행 대상이 된다.
        "golden_mtime": _get_file_mtime(GOLDEN_QUERIES_PATH),
        "rules_fingerprint": _rules_fingerprint(),
        "dict_fingerprint": _dict_fingerprint(),
        # 테이블별 지문. 일부만 바뀐 경우 changed_tables_since()로 세분화 판정 가능.
        "csv_per_table": _per_table_fingerprint(),
    }


def load_state() -> dict:
    """저장된 파이프라인 상태를 로드."""
    if not os.path.exists(_STATE_PATH):
        return {"completed_steps": {}, "input_state": {}}
    try:
        with open(_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("Pipeline state file corrupted, starting fresh: %s", e)
        return {"completed_steps": {}, "input_state": {}}


def save_step(
    step_name: str,
    result_summary: dict | None = None,
    duration_seconds: float | None = None,
    quality_metrics: dict | None = None,
) -> str:
    """단계 완료를 기록한다.

    파이프라인 진행 중 각 단계가 PASS 되면 호출. 같은 step_name 으로 다시
    호출하면 덮어쓰기 (재실행 시 최신 결과 보존).

    Args:
        step_name: 체크포인트 키 (예: "S7_ABOX").
        result_summary: 단계의 핵심 산출물 요약 (선택).
        duration_seconds: 실측 소요 시간 (선택).
        quality_metrics: 품질 점수 (선택). 제공 시 quality_history.json 에도 누적.

    Returns:
        JSON: {"saved": step_name, "completed_at": "...", "total_completed": N}
    """
    state = load_state()
    current = _current_input_state()
    state["completed_steps"][step_name] = {
        "completed_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "result": result_summary,
        "duration_seconds": duration_seconds,
        # 이 단계가 **완료된 시점의** 입력 지문. 전역 스냅샷만 두면 나중 단계의
        # save_step 이 그것을 덮어써, 입력 변경으로 무효화됐던 앞 단계가 다시
        # "건너뛰기 가능" 으로 되살아난다 (2026-08-08 규명: S2 완료 → CSV 수정
        # → S7 완료 만으로 S2 가 skippable=True 로 복귀했고, T-Box 는 옛 CSV
        # 기준인데 재생성이 생략됐다).
        "input_state": current,
    }
    # 전역 스냅샷은 하위 호환 (예전 상태 파일 / per-step 지문이 없는 항목) 용도로
    # 유지한다.
    state["input_state"] = current
    os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
    atomic_write(_STATE_PATH, json.dumps(state, ensure_ascii=False, indent=2))
    if quality_metrics:
        _append_quality_history(step_name, quality_metrics, duration_seconds)
    return json.dumps({
        "saved": step_name,
        "completed_at": state["completed_steps"][step_name]["completed_at"],
        "total_completed": len(state["completed_steps"]),
    }, ensure_ascii=False)


def _append_quality_history(step_name: str, metrics: dict, duration: float | None = None,
                            path: str | None = None) -> None:
    """Append quality metrics to history file for trend tracking.

    ``path`` 는 테스트 격리용 옵션 인자다. 기본값은 여전히
    ``dirname(_STATE_PATH)`` 에서 파생한다 — 기존 테스트들이 ``_STATE_PATH`` 를
    monkeypatch 해서 이 파일을 격리하고 있으므로(``test_golden_queries.py:158``,
    ``test_checkpoint_per_step_snapshot.py:32``) 그 계약을 깨면 배포
    ``quality_history.json`` 이 테스트 픽스처로 오염된다.
    """
    history_path = path or os.path.join(
        os.path.dirname(_STATE_PATH), "quality_history.json")
    history = []
    if os.path.exists(history_path):
        try:
            with open(history_path, encoding="utf-8") as f:
                history = json.load(f)
        except (json.JSONDecodeError, ValueError):
            history = []
    history.append({
        "timestamp": datetime.now().isoformat(),
        "step": step_name,
        "metrics": metrics,
        "duration_seconds": duration,
    })
    # Keep last 100 entries
    history = history[-100:]
    atomic_write(history_path, json.dumps(history, ensure_ascii=False, indent=2))


def evaluate_skip(
    step_name: str,
    depends_on: list[str] | None = None,
    *,
    state: dict | None = None,
    current_input: dict | None = None,
) -> dict:
    """건너뛰기 판정을 **사유와 함께** 반환한다.

    반환 키:
      - ``skippable``: 건너뛸 수 있는가.
      - ``blocked_by``: 값이 달라진 축 이름 목록. 비어 있으면 차단 없음.
      - ``unverified_axes``: 저장된 지문에 **그 축 자체가 없는** 경우.

    ## 왜 ``unverified_axes`` 를 차단으로 세지 않는가

    이전 구현은 ``saved_input.get(key, 0) != current_input.get(key, 0)`` 였다.
    새 축을 추가하는 순간 기존 체크포인트에는 그 키가 없으므로 ``0`` 으로 읽히고,
    현재 값과 달라 **모든 단계가 한꺼번에 무효화** 된다. S2 는 46분 잡이라 아무것도
    바뀌지 않았는데 재실행 대상으로 뜨는 것은 순손실이다.

    키의 부재는 "그때는 이 축을 기록하지 않았다" 는 뜻이고, 그것으로 변경 여부를
    결론낼 수는 없다. 그래서 차단하지 않고 **미검증으로 드러낸다** — 이 리포에서
    반복된 "판정 불가를 판정으로 보고" 를 반대 방향으로 저지르지 않기 위함이다.
    다음 ``save_step`` 이 새 축을 기록하면 그 단계의 미검증은 자동으로 사라진다.
    """
    state = state if state is not None else load_state()
    entry = state.get("completed_steps", {}).get(step_name)
    if entry is None:
        return {"skippable": False, "blocked_by": [], "unverified_axes": []}

    # 이 단계 자신의 완료 시점 지문을 우선 사용한다. 없으면 (구 상태 파일) 전역
    # 스냅샷으로 폴백 — 그 경우 이전 동작과 동일하다.
    saved_input = (entry or {}).get("input_state") or state.get("input_state", {})
    current_input = (
        current_input if current_input is not None else _current_input_state()
    )

    if depends_on is None:
        depends_on = _STEP_DEPS.get(step_name, ["csv_mtime", "tacit_mtime"])

    blocked: list[str] = []
    unverified: list[str] = []
    for key in depends_on:
        if key not in saved_input:
            unverified.append(key)
            continue
        if saved_input.get(key) != current_input.get(key):
            blocked.append(key)

    if blocked:
        logger.info(
            "단계 %s: 입력 변경 감지 (%s), 재실행 필요", step_name, ", ".join(blocked),
        )
    if unverified:
        logger.info(
            "단계 %s: 축 미검증 (%s) — 저장된 지문에 그 키가 없다",
            step_name, ", ".join(unverified),
        )
    return {
        "skippable": not blocked,
        "blocked_by": blocked,
        "unverified_axes": unverified,
    }


def can_skip_step(step_name: str, depends_on: list[str] | None = None) -> bool:
    """이 단계를 건너뛸 수 있는지 확인.

    조건: 이전에 완료됨 AND 의존 입력이 변경되지 않음.
    사유가 필요하면 :func:`evaluate_skip` 를 쓴다.
    """
    return bool(evaluate_skip(step_name, depends_on)["skippable"])


def check_pipeline_state() -> str:
    """파이프라인 체크포인트 상태를 조회한다. 어떤 단계가 완료되었고 어떤 단계를 건너뛸 수 있는지 보고."""
    try:
        state = load_state()
        current = _current_input_state()

        result = []
        for step in _ALL_STEPS:
            completed = step in state.get("completed_steps", {})
            # 응답에 **skip 불가 사유가 없었다** (실측: grep blocked_by tools/ → 0건).
            # 원인을 알려면 서버 로그나 _STEP_DEPS 를 봐야 했다.
            verdict = evaluate_skip(
                step, _STEP_DEPS.get(step), state=state, current_input=current,
            )
            info = state.get("completed_steps", {}).get(step, {})
            result.append({
                "step": step,
                "completed": completed,
                "skippable": verdict["skippable"],
                "blocked_by": verdict["blocked_by"],
                "unverified_axes": verdict["unverified_axes"],
                "depends_on": _STEP_DEPS.get(step, []),
                "completed_at": info.get("completed_at"),
                "duration_seconds": info.get("duration_seconds"),
            })

        total_duration = sum(
            s.get("duration_seconds", 0) or 0
            for s in state.get("completed_steps", {}).values()
        )

        return json.dumps({
            "success": True,
            "steps": result,
            "total_duration_seconds": round(total_duration, 1),
            "input_state": current,
            "hint": (
                "skippable=true 인 단계는 입력이 변경되지 않아 건너뛸 수 있습니다. "
                "blocked_by 는 값이 달라진 축, unverified_axes 는 저장된 지문에 그 축이 "
                "없어 판정할 수 없는 축입니다 (차단하지 않습니다)."
            ),
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


def reset_pipeline_state() -> str:
    """파이프라인 체크포인트를 초기화한다. 다음 실행 시 모든 단계가 처음부터 실행된다."""
    try:
        if os.path.exists(_STATE_PATH):
            os.remove(_STATE_PATH)
        return json.dumps({"success": True, "message": "파이프라인 상태 초기화 완료"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "error": str(e)}, ensure_ascii=False)


def _extract_metric(metrics: dict, key: str) -> float | None:
    """중첩된 메트릭 구조에서 값을 추출한다."""
    if key in metrics:
        val = metrics[key]
        if isinstance(val, dict):
            return val.get("value")
        if isinstance(val, int | float):
            return val
        return None
    for _k, v in metrics.items():
        if isinstance(v, dict) and key in v:
            inner = v[key]
            return inner if isinstance(inner, int | float) else None
    return None


def _detect_quality_regressions(history: list) -> list[dict]:
    """이전 실행 대비 메트릭 하락을 자동 탐지한다."""
    if len(history) < 2:
        return []

    regressions = []
    step_history: dict[str, list[dict]] = {}
    for entry in history:
        step = entry.get("step", "")
        if step:
            step_history.setdefault(step, []).append(entry)

    _METRIC_KEYS = {
        "total_score": {"higher_is_better": True, "threshold": 5},
        "pass_rate": {"higher_is_better": True, "threshold": 10},
        "dit": {"higher_is_better": True, "threshold": 1},
        "rr": {"higher_is_better": True, "threshold": 0.05},
        "ar": {"higher_is_better": True, "threshold": 1},
        "annotation_completeness": {"higher_is_better": True, "threshold": 5},
        "axiom_richness": {"higher_is_better": True, "threshold": 0.5},
        "violations_count": {"higher_is_better": False, "threshold": 3},
        "critical": {"higher_is_better": False, "threshold": 1},
    }

    for step, entries in step_history.items():
        if len(entries) < 2:
            continue
        prev_m = entries[-2].get("metrics", {})
        curr_m = entries[-1].get("metrics", {})
        for key, cfg in _METRIC_KEYS.items():
            pv = _extract_metric(prev_m, key)
            cv = _extract_metric(curr_m, key)
            if pv is None or cv is None:
                continue
            thr = cfg["threshold"]
            regressed = (cv < pv - thr) if cfg["higher_is_better"] else (cv > pv + thr)
            if regressed:
                regressions.append({
                    "step": step, "metric": key,
                    "previous": pv, "current": cv,
                    "delta": round(cv - pv, 2),
                    "timestamp": entries[-1].get("timestamp", ""),
                    "severity": "critical" if abs(cv - pv) > thr * 2 else "warning",
                })
    return regressions


def get_pipeline_quality_history(last_n: int = 10) -> str:
    """파이프라인 품질 메트릭 추세를 조회한다 (save_step 기록 스키마).

    각 파이프라인 실행에서 저장된 품질 메트릭(T-Box 점수, KG 검증 결과, CQ 통과율 등)의
    이력을 반환하여 품질 추세를 파악할 수 있다.

    ``kg_validation.get_quality_history`` 와 **다른 스키마** 를 읽는다 (이쪽은
    ``save_step(quality_metrics=...)`` 이 남긴 ``metrics`` 항목, 저쪽은
    validate_kg 의 ``score`` 항목). 두 함수가 같은 이름이던 동안 이 쪽이 조용히
    등록에서 밀려 MCP 로 호출할 수 없었다 (2026-08-08 규명: FastMCP 는 동명 툴을
    경고만 남기고 첫 등록을 유지한다).

    Args:
        last_n: 조회할 최근 항목 수. 기본값 10.
    """
    history_path = os.path.join(os.path.dirname(_STATE_PATH), "quality_history.json")
    if not os.path.exists(history_path):
        return json.dumps({"success": True, "entries": [], "message": "품질 이력 없음. 파이프라인을 실행하면 자동으로 기록됩니다."}, ensure_ascii=False)
    try:
        with open(history_path, encoding="utf-8") as f:
            history = json.load(f)
    except (json.JSONDecodeError, ValueError):
        return json.dumps({"success": True, "entries": [], "message": "품질 이력 파일 손상"}, ensure_ascii=False)

    entries = history[-last_n:]
    regressions = _detect_quality_regressions(history)

    result = {
        "success": True,
        "total_entries": len(history),
        "showing": len(entries),
        "entries": entries,
    }
    if regressions:
        result["regressions"] = regressions
        result["regression_count"] = len(regressions)
        result["regression_summary"] = (
            f"{len(regressions)}건의 품질 회귀 감지: "
            + ", ".join(f"{r['step']}.{r['metric']}({r['delta']:+.1f})" for r in regressions[:5])
        )
    return json.dumps(result, ensure_ascii=False, indent=2)


#: Class-file major version -> JDK feature release. 45 == Java 1.1, then +1 each.
def _major_to_jdk(major: int) -> str:
    return str(major - 44) if major >= 49 else f"1.{major - 44}"


def _required_class_major() -> int | None:
    """Highest class-file version bundled in owlready2's Pellet jars.

    Measured rather than hardcoded: owlready2 0.50 shipped three ``LangRDFXML``
    classes recompiled at major 69 (Java 25) while patching CVE-2021-39239, so
    the floor moves with the upstream author's build JDK. Reading the jars keeps
    this check correct across future bumps. Costs ~0.25s (8 bytes per entry).
    """
    try:
        import struct
        import zipfile

        import owlready2

        pellet_dir = os.path.join(os.path.dirname(owlready2.__file__), "pellet")
        highest = 0
        for jar in glob.glob(os.path.join(pellet_dir, "*.jar")):
            with zipfile.ZipFile(jar) as zf:
                for entry in zf.infolist():
                    if not entry.filename.endswith(".class"):
                        continue
                    with zf.open(entry) as fh:
                        head = fh.read(8)
                    if len(head) >= 8:
                        highest = max(highest, struct.unpack(">H", head[6:8])[0])
        return highest or None
    except Exception:
        return None


def _runtime_class_major(java_exe: str) -> int | None:
    """``java.class.version`` of the given JVM, or None if it cannot be read."""
    try:
        import subprocess

        out = subprocess.run(
            [java_exe, "-XshowSettings:properties", "-version"],
            capture_output=True, text=True, timeout=30,
        )
        for line in (out.stderr + out.stdout).splitlines():
            if "java.class.version" in line:
                return int(float(line.split("=", 1)[1].strip()))
    except Exception:
        return None
    return None


def _check_java(java_exe: str) -> dict:
    """Report whether the configured JVM can actually run the bundled reasoners.

    ``os.path.exists`` alone reported ``ok`` while every Pellet-backed tool
    (``validate_owl_realisation``, S8.5 SWRL) failed with
    ``UnsupportedClassVersionError`` — a green light over a fully broken path.
    HermiT is unaffected because its classpath excludes the Jena jars, so an
    S4 pass is not evidence that Pellet works.
    """
    component = "Java (OWL 추론기)"
    if not java_exe:
        return {"component": component, "status": "not_configured", "detail": ""}
    if not os.path.exists(java_exe):
        return {"component": component, "status": "not_found",
                "detail": f"경로 없음: {java_exe}"}

    runtime = _runtime_class_major(java_exe)
    if runtime is None:
        return {"component": component, "status": "unknown",
                "detail": f"{java_exe} (버전 확인 실패)"}

    required = _required_class_major()
    if required is not None and runtime < required:
        return {
            "component": component,
            "status": "degraded",
            "detail": (
                f"{java_exe}: Java {_major_to_jdk(runtime)} (class {runtime}) — "
                f"Pellet 경로 사용 불가. owlready2 의 Jena jar 가 class {required} "
                f"(Java {_major_to_jdk(required)}) 를 요구한다. "
                "validate_owl_realisation / S8.5 SWRL 이 UnsupportedClassVersionError 로 "
                "실패한다 (HermiT 은 영향 없어 S4 는 통과하므로 신호가 되지 않는다)."
            ),
        }
    return {"component": component, "status": "ok",
            "detail": f"{java_exe}: Java {_major_to_jdk(runtime)} (class {runtime})"}


def health_check() -> str:
    """로컬 산출물을 읽고 설정된 Neo4j endpoint에 read-only probe를 수행한다.

    점검 항목:
    - 로컬 파일 (T-Box, A-Box, 추론 결과, 시맨틱 딕셔너리)
    - 외부 서비스 (Neo4j, Bedrock)

    파일이나 외부 서비스를 수정하지 않는다. Bedrock은 설정만 확인한다.
    """
    checks: list[dict] = []

    # 로컬 파일 존재
    for name, path in [
        ("T-Box", TBOX_PATH),
        ("A-Box", ABOX_PATH),
        ("Inferred", INFERRED_PATH),
        ("Semantic Dictionary", os.path.join(GENERATED_DIR, "semantic_dictionary.json")),
    ]:
        exists = os.path.exists(path)
        size = os.path.getsize(path) if exists else 0
        checks.append({
            "component": name,
            "status": "ok" if exists and size > 0 else ("empty" if exists else "missing"),
            "detail": f"{size:,} bytes" if exists else "파일 없음",
        })

    # CSV 데이터
    csv_count = len(glob.glob(os.path.join(SOURCE_RAWDATA_DIR, "*.csv")))
    checks.append({
        "component": "CSV 원본",
        "status": "ok" if csv_count > 0 else "missing",
        "detail": f"{csv_count}개 테이블",
    })

    # Neo4j
    from config import NEO4J_URI
    if NEO4J_URI:
        try:
            from tools.remote.neo4j import _get_driver
            driver = _get_driver()
            with driver.session() as session:
                session.run("RETURN 1").single()
            checks.append({"component": "Neo4j", "status": "ok", "detail": NEO4J_URI})
        except Exception as e:
            checks.append({"component": "Neo4j", "status": "error", "detail": str(e)[:80]})
    else:
        checks.append({"component": "Neo4j", "status": "not_configured", "detail": ""})

    # Bedrock
    from config import BEDROCK_MODEL_ID, BEDROCK_REGION
    checks.append({
        "component": "Bedrock",
        "status": "configured",
        "detail": f"{BEDROCK_MODEL_ID} ({BEDROCK_REGION})",
    })

    # Java
    from config import JAVA_EXE
    checks.append(_check_java(JAVA_EXE))

    # 파이프라인 상태
    if os.path.exists(_STATE_PATH):
        try:
            with open(_STATE_PATH, encoding="utf-8") as f:
                state = json.load(f)
            completed = sum(1 for v in state.get("steps", {}).values() if v.get("status") == "completed")
            total = len(state.get("steps", {}))
            checks.append({
                "component": "Pipeline State",
                "status": "ok",
                "detail": f"{completed}/{total} 단계 완료",
            })
        except Exception:
            checks.append({"component": "Pipeline State", "status": "error", "detail": "상태 파일 손상"})
    else:
        checks.append({"component": "Pipeline State", "status": "no_state", "detail": "파이프라인 미실행"})

    overall = "healthy" if all(c["status"] in ("ok", "configured", "not_configured", "no_state") for c in checks) else "degraded"
    return json.dumps({"success": True, "status": overall, "checks": checks}, ensure_ascii=False, indent=2)
