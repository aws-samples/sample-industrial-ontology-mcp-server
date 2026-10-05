"""Oracle-First Quality — SME 고정 golden query 회귀 테스트 (P1).

LLM이 생성한 CQ로 LLM 결과를 채점하는 자기참조 순환을 끊기 위해,
data/source/query_tests/golden_queries.json의 `golden_sparql`을 외부 기준으로
삼아 파이프라인 실행마다 SPARQL을 돌리고 결과/통과율을 이력에 누적한다.

특징:
- CQ와 독립된 게이트: test_domain_queries는 탐색용, 이 도구가 규율용
- 결과 이력: data/generated/reports/golden_history.json
- 회귀 탐지: 이전 실행 대비 통과→실패 전환 목록 리포트
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime

from config import GENERATED_REPORTS_DIR, GOLDEN_QUERIES_PATH
from domain.namespaces import prepend_prefixes
from tools.common import error_response

logger = logging.getLogger(__name__)

_GOLDEN_PATH = GOLDEN_QUERIES_PATH
_HISTORY_PATH = os.path.join(GENERATED_REPORTS_DIR, "golden_history.json")


# SME에게 보여줄 입력 가이드. add_golden_queries / run_golden_queries 에러 메시지에서 재사용.
_GOLDEN_SCHEMA_EXAMPLE: list[dict] = [
    {
        "id": "equipment_failure_count",
        "question": "설비별 고장 발생 건수는?",
        "category": "count",
        "difficulty": "easy",
        "domains": ["Failure_Cause", "Equipment_Master"],
        # 이 예시는 **SME 가 복사해서 시작점으로 쓰는 템플릿** 이므로 실제로
        # 동작하고 결함 시 FAIL 해야 한다. 예전 예시는 둘 다 아니었다
        # (2026-08-22 실측):
        #
        # 1. ``steel:occurrenceDateTime`` 은 T-Box 에 **존재하지 않는다**
        #    (grep 0건 — 실제 이름은 ``failureCauseOccurrenceDateTime``).
        # 2. **GROUP BY 없는 순수 집계는 매칭 0건에도 1행을 반환한다**
        #    (``[('0',)]``). 그래서 ``min_rows: 1`` 이 항상 통과했고, T-Box 를
        #    통째로 비워도 PASS 하는 **죽은 게이트** 였다. GROUP BY 를 붙이면
        #    0건일 때 0행이 되어 게이트가 실제로 발화한다.
        # 3. 날짜 리터럴을 템플릿에 박지 않는다 — 예시의 창(2024-01)은 샘플
        #    데이터 구간(2025-09)과 어긋나 이름만 고치면 **항상 0행 = 항상 FAIL**
        #    이 된다. 기간 조건은 SME 가 자기 데이터에 맞춰 넣을 몫이다.
        # 4. **A-Box 가 실제로 채우는 방향** 을 쓴다. ``hasFailureCause`` 는 T-Box
        #    에 선언돼 있지만 A-Box 는 다른 방향만 채운다. 빈 동의어로 질의하면
        #    0행이 "정답처럼" 반환돼 게이트가 조용히 죽는다 — 골든 쿼리를 쓸 때 가장
        #    흔한 함정이다.
        # 5. **OP 이름은 S2 재생성마다 갈린다.** 2026-09-05: 여기 박아 뒀던
        #    ``isFailureCauseOf`` 가 세대 교체로 사라졌고(grep 0건) 채워지는 방향은
        #    ``failureCauseRefersToEquipment`` (132 트리플) 가 됐다. rules/ 와 소스는
        #    git 추적인데 T-Box 는 gitignore 라 이 드리프트는 구조적이다 — 그래서
        #    ``test_example_property_names_exist_in_tbox`` 가 선언 여부와 **채워지는
        #    방향인지** 를 함께 주장한다. 실패하면 배포 T-Box 에서 이름을 다시 재고
        #    이 블록을 갱신하라. 실측 44행 (merge 828,850 triples).
        "golden_sparql": (
            "SELECT ?equipId (COUNT(?f) AS ?total) WHERE {\n"
            "  ?f a steel:FailureCause ;\n"
            "     steel:failureCauseRefersToEquipment ?eq ;\n"
            "     steel:failureCauseOccurrenceDateTime ?ts .\n"
            "  ?eq steel:equipmentMasterId ?equipId .\n"
            "}\n"
            "GROUP BY ?equipId"
        ),
        "assertions": {
            # 실측 44행 (merge 843,033 triples). 데이터가 줄어도 통과하도록
            # 보수적으로 잡되 **0 은 금지** — 0 은 "아무것도 검사 안 함" 이다.
            "min_rows": 10,
            "required_vars": ["equipId", "total"],
            "sparql_must_contain": [
                "failureCauseRefersToEquipment", "COUNT", "GROUP BY",
            ],
            "sparql_must_not_contain": ["DELETE", "INSERT"],
        },
    },
]


def _input_guide() -> str:
    """사용자에게 golden_queries.json 생성을 가이드하는 멀티라인 힌트 문자열."""
    return (
        "golden_queries.json 은 SME가 검증한 golden SPARQL 을 회귀 게이트로 쓰는 외부 기준입니다.\n"
        "LLM 자동 생성은 자기참조 순환(LLM 결과를 LLM 기준으로 채점)을 만들어 피해야 합니다.\n"
        "\n"
        "생성 방법:\n"
        "  1) add_golden_queries(user_provided='<JSON 배열>') 으로 직접 입력\n"
        "  2) 또는 data/source/query_tests/golden_queries.json 에 수기 편집\n"
        "\n"
        "각 케이스 필수 필드: id, question, difficulty, domains, golden_sparql, assertions.\n"
        "assertions: min_rows, required_vars, sparql_must_contain, sparql_must_not_contain.\n"
        "스키마 예시는 golden_queries_schema_example() 도구로 확인 가능."
    )


def _load_golden_cases() -> list[dict]:
    if not os.path.exists(_GOLDEN_PATH):
        return []
    with open(_GOLDEN_PATH, encoding="utf-8") as f:
        return json.load(f)


def _validate_case(case: dict, idx: int) -> list[str]:
    """단일 case 의 필수 필드/타입 검증. 반환: 에러 메시지 리스트 (빈 리스트면 OK)."""
    errors: list[str] = []
    required_keys = ("id", "question", "difficulty", "domains", "golden_sparql", "assertions")
    for k in required_keys:
        if k not in case:
            errors.append(f"case[{idx}] 필수 키 누락: {k}")
    if "domains" in case and not isinstance(case["domains"], list):
        errors.append(f"case[{idx}].domains 는 list 여야 합니다.")
    if "assertions" in case and not isinstance(case["assertions"], dict):
        errors.append(f"case[{idx}].assertions 는 dict 여야 합니다.")
    if "golden_sparql" in case and not str(case["golden_sparql"]).strip():
        errors.append(f"case[{idx}].golden_sparql 이 비어있습니다.")
    errors.extend(_validate_row_bound(case.get("assertions"), idx))
    return errors


#: ``_evaluate_assertions`` 가 인식하는 assertion 키. 그 밖의 키는 조용히 무시되므로
#: 오타(``min_row``)가 게이트를 무력화한다 — 입력 시점에 알린다.
_KNOWN_ASSERTIONS = frozenset({
    "min_rows", "max_rows", "required_vars",
    "sparql_must_contain", "sparql_must_not_contain",
})


def _validate_row_bound(assertions, idx: int) -> list[str]:
    """행 수 경계가 **실질적으로** 있는지 검사.

    ``_evaluate_assertions`` (:241) 는 ``min_rows = a.get("min_rows", 0)`` 이므로
    키가 없거나 오타면 **0행을 정답으로 등록** 한다 — 회귀 게이트가 아니라 항상
    통과하는 장식이 된다 (2026-08-22 실측: ``min_rows`` 누락 / ``"min_row"`` 오타
    둘 다 ``_validate_case`` 를 에러 0건으로 통과했다).

    **``min_rows>=1`` 을 무조건 요구하지는 않는다.** ``max_rows: 0`` ("위반이 0건
    이어야 한다") 는 정당한 장르이고 이미 지원된다 (실측: 0행 → PASS / 1행 →
    ``max_rows: got 1 > 0`` FAIL). 그래서 **둘 중 하나라도 유효하면** 통과시킨다.

    ``bool`` 을 배제하는 이유: ``isinstance(True, int)`` 가 참이라 ``min_rows: true``
    가 1 로 통과해 버린다.
    """
    if not isinstance(assertions, dict):
        return []                       # 타입 오류는 호출부가 이미 보고한다
    def _int_at_least(key: str, floor: int) -> bool:
        v = assertions.get(key)
        return isinstance(v, int) and not isinstance(v, bool) and v >= floor

    out: list[str] = []
    if not (_int_at_least("min_rows", 1) or _int_at_least("max_rows", 0)):
        out.append(
            f"case[{idx}].assertions 에 유효한 행 수 경계가 없습니다 "
            f"(min_rows>=1 또는 max_rows>=0 중 하나 필수). min_rows 누락·0 은 "
            f"0행을 정답으로 등록해 회귀 게이트를 무력화합니다."
        )
    unknown = sorted(set(assertions) - _KNOWN_ASSERTIONS)
    if unknown:
        out.append(
            f"case[{idx}].assertions 에 알 수 없는 키: {unknown} — "
            f"평가기가 조용히 무시하므로 오타면 게이트가 죽습니다 "
            f"(인식 키: {sorted(_KNOWN_ASSERTIONS)})"
        )
    return out


def check_golden_queries_exist() -> str:
    """저장된 golden query set(golden_queries.json) 존재/개수를 반환.

    run_golden_queries 를 실행하기 전에 에이전트가 먼저 호출해 케이스 유무를 파악한다.
    없으면 사용자에게 입력을 요청(add_golden_queries)하도록 유도한다.
    """
    try:
        if not os.path.exists(_GOLDEN_PATH):
            return json.dumps({
                "success": True, "exists": False, "count": 0,
                "path": _GOLDEN_PATH, "input_guide": _input_guide(),
            }, ensure_ascii=False, indent=2)
        cases = _load_golden_cases()
        return json.dumps({
            "success": True,
            "exists": len(cases) > 0,
            "count": len(cases),
            "path": _GOLDEN_PATH,
            "input_guide": _input_guide() if not cases else None,
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return error_response(e, logger=logger)


def golden_queries_schema_example() -> str:
    """사용자/에이전트가 golden_queries.json 스키마를 배울 수 있도록 예시 1건을 반환.

    add_golden_queries 로 넘길 JSON 배열의 형식을 그대로 보여준다.
    """
    return json.dumps({
        "success": True,
        "example": _GOLDEN_SCHEMA_EXAMPLE,
        "input_guide": _input_guide(),
    }, ensure_ascii=False, indent=2)


def add_golden_queries(user_provided: str, append: bool = True) -> str:
    """사용자가 제공한 golden query 케이스를 golden_queries.json 에 저장한다.

    LLM 자동 생성은 제공하지 않는다 (golden query 는 SME 검증 자산).
    에이전트는 check_golden_queries_exist() 가 exists=False 를 반환하면
    사용자에게 입력을 요청하고, 받은 JSON 배열을 이 도구로 저장한다.

    Args:
        user_provided: JSON 배열 문자열. 각 원소는 id/question/difficulty/domains/
                       golden_sparql/assertions 를 포함해야 한다.
        append: True(기본) 면 기존 케이스 뒤에 추가, False 면 전체 대체.
    """
    try:
        if not (user_provided or "").strip():
            return error_response(
                "user_provided 가 비어있습니다.",
                hint=_input_guide(),
                logger=logger,
            )
        try:
            data = json.loads(user_provided)
        except json.JSONDecodeError as je:
            return error_response(
                f"user_provided 파싱 실패: {je}",
                hint=("유효한 JSON 배열이어야 합니다. 예시는 "
                      "golden_queries_schema_example() 로 확인하세요."),
                logger=logger,
            )
        if not isinstance(data, list):
            return error_response(
                "user_provided 는 JSON 배열이어야 합니다.",
                hint=_input_guide(),
                logger=logger,
            )

        # 필드 검증
        all_errors: list[str] = []
        for i, case in enumerate(data):
            if not isinstance(case, dict):
                all_errors.append(f"case[{i}] 가 dict 가 아닙니다.")
                continue
            all_errors.extend(_validate_case(case, i))
        if all_errors:
            return error_response(
                "golden query 검증 실패:\n- " + "\n- ".join(all_errors),
                hint=_input_guide(),
                logger=logger,
            )

        # id 중복 체크 (append 모드)
        existing = _load_golden_cases() if append else []
        existing_ids = {c.get("id") for c in existing}
        dup_ids = [c["id"] for c in data if c["id"] in existing_ids]
        if dup_ids:
            return error_response(
                f"id 중복: {dup_ids}. append=False 로 전체 대체하거나 id 를 바꾸세요.",
                hint=_input_guide(),
                logger=logger,
            )

        merged = existing + data if append else data
        os.makedirs(os.path.dirname(_GOLDEN_PATH), exist_ok=True)
        with open(_GOLDEN_PATH, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)

        return json.dumps({
            "success": True,
            "path": _GOLDEN_PATH,
            "added": len(data),
            "total": len(merged),
            "mode": "append" if append else "replace",
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return error_response(e, logger=logger)


def _evaluate_assertions(rows: list[dict], case: dict) -> tuple[bool, list[str]]:
    """assertion 5종 평가: min_rows, required_vars, sparql_must_contain,
    sparql_must_not_contain, max_rows (옵션).
    반환: (passed, failures)
    """
    a = case.get("assertions", {})
    failures: list[str] = []

    min_rows = a.get("min_rows", 0)
    if len(rows) < min_rows:
        failures.append(f"min_rows: got {len(rows)} < {min_rows}")

    max_rows = a.get("max_rows")
    if max_rows is not None and len(rows) > max_rows:
        failures.append(f"max_rows: got {len(rows)} > {max_rows}")

    if rows:
        required = a.get("required_vars", [])
        missing = [v for v in required if v not in rows[0]]
        if missing:
            failures.append(f"required_vars missing: {missing}")

    sparql = case.get("golden_sparql", "")
    for tok in a.get("sparql_must_contain", []):
        if tok not in sparql:
            failures.append(f"sparql_must_contain missing: {tok}")
    for tok in a.get("sparql_must_not_contain", []):
        if tok in sparql:
            failures.append(f"sparql_must_not_contain present: {tok}")

    return len(failures) == 0, failures


def _run_single(case: dict, source: str) -> dict:
    """단일 case 실행. egress 거부나 실행 실패는 ERROR 로 기록.

    golden_sparql 은 사용자 입력이므로 PREFIX 를 붙인 최종 문자열에 egress 가드를
    적용한 뒤에만 그래프를 로드하고 실행한다.
    """
    from domain.sparql_templates import format_sparql_results
    from tools.sparql_local import _get_graph, _reject_query_egress

    start = time.monotonic()
    try:
        full_query = prepend_prefixes(case["golden_sparql"])
        _reject_query_egress(full_query)
        g, _load_msg = _get_graph(source)
        results = g.query(full_query)
        rows = format_sparql_results(results)
        duration = round(time.monotonic() - start, 3)
    except Exception as e:
        return {
            "id": case["id"],
            "status": "ERROR",
            "error": f"{type(e).__name__}: {str(e)[:150]}",
            "duration_s": round(time.monotonic() - start, 3),
            "row_count": 0,
            "failures": [],
        }

    passed, failures = _evaluate_assertions(rows, case)
    return {
        "id": case["id"],
        "status": "PASS" if passed else "FAIL",
        "difficulty": case.get("difficulty", ""),
        "category": case.get("category", ""),
        "row_count": len(rows),
        "duration_s": duration,
        "failures": failures,
        "sample_row": rows[0] if rows else None,
    }


def _load_history() -> list[dict]:
    if not os.path.exists(_HISTORY_PATH):
        return []
    try:
        with open(_HISTORY_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _append_history(entry: dict) -> None:
    os.makedirs(os.path.dirname(_HISTORY_PATH), exist_ok=True)
    hist = _load_history()
    hist.append(entry)
    # 최근 100건만 유지
    if len(hist) > 100:
        hist = hist[-100:]
    with open(_HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(hist, f, ensure_ascii=False, indent=2)


def _detect_regressions(current: list[dict], history: list[dict]) -> dict:
    """직전 실행과 비교해 PASS→FAIL, PASS→ERROR 로 전환된 case 탐지."""
    if not history:
        return {"has_previous": False, "regressions": [], "recoveries": []}

    prev_entry = history[-1]
    prev_by_id = {r["id"]: r for r in prev_entry.get("results", [])}

    regressions: list[dict] = []
    recoveries: list[dict] = []
    for cur in current:
        prev = prev_by_id.get(cur["id"])
        if not prev:
            continue
        if prev["status"] == "PASS" and cur["status"] != "PASS":
            regressions.append({
                "id": cur["id"],
                "from": prev["status"],
                "to": cur["status"],
                "failures": cur.get("failures", []),
            })
        elif prev["status"] != "PASS" and cur["status"] == "PASS":
            recoveries.append({"id": cur["id"], "from": prev["status"]})

    return {
        "has_previous": True,
        "previous_timestamp": prev_entry.get("timestamp"),
        "regressions": regressions,
        "recoveries": recoveries,
    }


def run_golden_queries(source: str = "merge", record_history: bool = True) -> str:
    """SME-frozen golden query set을 실행하고 assertion 평가 + 이력 누적 (P1).

    test_domain_queries와 구별: 이 도구는 data/source/query_tests/golden_queries.json
    의 golden_sparql을 직접 실행하는 외부 기준 게이트. LLM 자기참조 평가와
    분리되어 회귀 감지 신뢰도 확보.

    예상 소요시간: 5~30초 (케이스 수 × 쿼리 복잡도)
    Bedrock 호출: 0회

    Args:
        source: SPARQL 실행 그래프 — "merge"(T-Box+A-Box+tacit) 또는 "inferred".
        record_history: True면 결과를 data/generated/reports/golden_history.json에 append.

    Returns:
        JSON: {success, summary, results, regression}

    응답의 next_step 필드는 다음 파이프라인 단계 힌트 (S13_REPORT) 다. 단 이 도구는
    save_step 을 자체 호출하지 않는다 — 에이전트가 결과를 확인한 뒤
    save_step("S12_5_GOLDEN_REGRESSION", {...}) 로 체크포인트 기록 책임.
    MCP 도구 + 에이전트 상태머신 분리 원칙 (CLAUDE.md 규칙 3) 준수.
    """
    try:
        cases = _load_golden_cases()
        if not cases:
            return error_response(
                "Golden query set이 없습니다 — 회귀 게이트를 실행할 기준이 없습니다.",
                hint=_input_guide(),
                logger=logger,
            )

        start_all = time.monotonic()
        results = [_run_single(c, source) for c in cases]
        duration_all = round(time.monotonic() - start_all, 2)

        passed = sum(1 for r in results if r["status"] == "PASS")
        failed = sum(1 for r in results if r["status"] == "FAIL")
        errored = sum(1 for r in results if r["status"] == "ERROR")
        total = len(results)
        pass_rate = round(passed / max(total, 1) * 100, 1)

        summary = {
            "total": total,
            "passed": passed,
            "failed": failed,
            "errored": errored,
            "pass_rate": pass_rate,
            "duration_s": duration_all,
            "source": source,
        }

        history = _load_history()
        regression = _detect_regressions(results, history)

        entry = {
            "timestamp": datetime.now().isoformat(),
            "source": source,
            "summary": summary,
            "results": results,
            "regression": regression,
        }
        if record_history:
            try:
                _append_history(entry)
            except Exception as e:
                logger.warning("golden_history 기록 실패: %s", e)

        return json.dumps({
            "success": True,
            "summary": summary,
            "results": results,
            "regression": regression,
            "next_step": "S13_REPORT",
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return error_response(
            f"golden query 실행 실패: {e}",
            hint="sparql_local로 쿼리 단독 실행 가능한지 먼저 확인하세요.",
            logger=logger,
        )


def skip_golden_regression(reason: str = "not configured") -> str:
    """Golden regression 단계를 명시적으로 skip 하고 체크포인트 기록.

    golden queries 가 없거나 의도적으로 건너뛸 때 에이전트가 호출. S13 진행
    가능하도록 save_step 호출. S5 tacit 의 skip_tacit_knowledge 와 동일 패턴.

    주의: 체크포인트 의존성은 TBOX/ABOX/inferred 파일 mtime 이므로, 사용자가 나중에
    golden_queries.json 을 추가해도 자동 감지되지 않는다. 에이전트는 매 파이프라인
    실행마다 check_golden_queries_exist() 를 먼저 호출해 현재 상태를 확인할 것
    (CLAUDE.md 규칙 4 참조).

    Args:
        reason: skip 사유 (로그/보고서용). 예: "not configured", "user skipped"

    Returns:
        JSON: {success, skipped, reason, next_step}
    """
    try:
        from tools.pipeline_state import save_step
        save_step("S12_5_GOLDEN_REGRESSION", {
            "skipped": True,
            "reason": reason,
        }, duration_seconds=0.0)
        return json.dumps({
            "success": True,
            "skipped": True,
            "reason": reason,
            "next_step": "S13_REPORT",
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return error_response(f"skip 기록 실패: {e}", logger=logger)


def get_golden_history(limit: int = 10) -> str:
    """golden query 실행 이력을 최근순으로 반환 (P1).

    Args:
        limit: 반환할 최근 실행 수 (기본 10).
    """
    hist = _load_history()
    recent = hist[-limit:] if limit > 0 else hist
    return json.dumps({
        "success": True,
        "total_runs": len(hist),
        "returned": len(recent),
        "runs": [
            {
                "timestamp": r.get("timestamp"),
                "source": r.get("source"),
                "summary": r.get("summary", {}),
                "regression_count": len(r.get("regression", {}).get("regressions", [])),
            }
            for r in recent
        ],
    }, ensure_ascii=False, indent=2)
