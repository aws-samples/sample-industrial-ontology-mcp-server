"""CQ 평가 소비자의 입력 오류 격리: 검수자 독립 계약 테스트.

승인된 CQ 평가 정합성 계약을 공개 도구와 S2 평가 진입점에서만 확인한다.

- 원본 CQ 배열의 모든 항목은 **분모와 순서** 에 남는다. 잘못된 항목은 각 API 의 기존
  실패 표현 (FAIL / gap / NOT_ANSWERABLE / unanswerable) 으로 남고, 원본 1-based
  ``source_index`` 와 고정 사유를 가진 ``input_error`` 진단을 단다.
- 잘못된 입력은 객체가 아닌 CQ, null·배열이 아닌 domains (false/0/빈 문자열/dict 포함),
  배열의 비문자열 원소다. 거부된 원문은 보고서·feedback·모델 입력·로그로 옮기지 않는다.
- domains 누락/null/[] 은 각 소비자의 **기존** 빈 배열 동작과 같다.
- 루트가 배열이 아니면 명시적 문서 오류다. 빈 배열 루트는 기존 정책을 유지한다.
- S2 는 원본 목록으로 평가하고 프롬프트에는 기존 필터 결과만 보낸다. 입력 오류는 OP
  skeleton 이나 온톨로지 수정 요청이 되지 않는다.
- S12 feedback 은 ``invalid_input`` 을 쓰고, 같은 id 의 현재 행 하나라도 실패하면 그 id 를
  resolved 로 기록하지 않는다.

진단 배치 (중첩 dict 또는 평면 필드) 는 구현 선택이므로 ``_error_meta`` 가 두 형태를 모두
읽는다. 모든 입력·출력 경로는 ``tmp_path`` 로 돌리고 배포 산출물 쓰기 가드는 그대로 둔다.
"""
from __future__ import annotations

import copy
import json
import logging
from datetime import datetime

import pytest
from rdflib import RDF, Graph, URIRef

import config
from domain.namespaces import DOMAIN_NS
from tools import competency_questions as cqmod
from tools import multi_agent_tbox as mt
from tools import query_test as qt

# ── 고정 세계: Alpha─Beta 는 연결, Gamma 는 고립·인스턴스 0 ─────────────────

_TBOX_TTL = f"""@prefix ex: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
ex:Alpha a owl:Class .
ex:Beta a owl:Class .
ex:Gamma a owl:Class .
ex:alphaHasBeta a owl:ObjectProperty ; rdfs:domain ex:Alpha ; rdfs:range ex:Beta .
"""

_ABOX_STATS = {"per_class": {
    "Alpha": {"instance_count": 2},
    "Beta": {"instance_count": 1},
    "Gamma": {"instance_count": 0},
}}

_SEM_DICT = {
    "classes": {},
    "object_properties": {"alphaHasBeta": {"domain": "Alpha", "range": "Beta"}},
}

#: 거부된 원문에만 넣는 표지. 어떤 출력·로그에도 나오면 안 된다.
_LEAK = "LEAK"

_VALID_OK = {"id": "CQ-OK", "question_ko": "알파와 베타", "domains": ["Alpha", "Beta"]}

#: 공용 혼합 목록. 위치(1-based) → 기대: 1 정상 / 4 실제 갭 / 8·9·10 빈 도메인 / 나머지 입력 오류.
_MIXED = [
    _VALID_OK,
    "LEAK-CQ-TEXT-0001",
    {"id": "CQ-STR-A", "question_ko": "문자열 도메인 가", "domains": "LEAKSTRA0002"},
    {"id": "CQ-GAP", "question_ko": "알파와 감마", "domains": ["Alpha", "Gamma"]},
    {"id": "CQ-DICT", "question_ko": "사전 도메인", "domains": {"Alpha": "LEAK-DICT-0003", "Beta": 1}},
    {"id": "CQ-FALSE", "question_ko": "거짓 도메인", "domains": False},
    {"id": "CQ-ELEM", "question_ko": "원소 오류", "domains": ["Alpha", {"LEAK-ELEM-0004": 1}]},
    {"id": "CQ-NULL", "question_ko": "널 도메인", "domains": None},
    {"id": "CQ-EMPTY", "question_ko": "빈 도메인", "domains": []},
    {"id": "CQ-MISSING", "question_ko": "도메인 없음"},
    {"id": "CQ-STR-B", "question_ko": "문자열 도메인 나", "domains": "LEAKSTRB0005"},
]
_MIXED_INVALID = (2, 3, 5, 6, 7, 11)

#: S2 원본 목록. 기존 프롬프트 필터는 2·3 만 제외한다 (5 의 비문자열 원소는 통과).
_S2_DOC = [
    {"id": "S2-OK", "question_ko": "q-ok", "domains": ["Alpha", "Beta"]},
    {"id": "S2-BAD", "question_ko": "q-bad", "domains": "LEAKS2STR0010"},
    "LEAK-S2-CQ-0011",
    {"id": "S2-GAP", "question_ko": "q-gap", "domains": ["Alpha", "Gamma"]},
    {"id": "S2-MIX", "question_ko": "q-mix", "domains": ["Alpha", {"LEAK-S2-ELEM-0012": 1}]},
    {"id": "S2-NULL", "question_ko": "q-null", "domains": None},
]
_S2_PROMPT_POSITIONS = (1, 4, 5, 6)

_JURY_JSON = json.dumps({
    "production_ready": False, "required_fixes": [], "decisions": [],
    "jury_issues": [], "rationale": "test",
})


# ── 환경 ─────────────────────────────────────────────────────────────


def _abox_graph() -> Graph:
    """Alpha─Beta 링크 1건을 가진 A-Box 그래프 (Gamma 인스턴스 없음)."""
    g = Graph()
    alpha, beta = URIRef("urn:test:alpha-1"), URIRef("urn:test:beta-1")
    g.add((alpha, RDF.type, URIRef(DOMAIN_NS + "Alpha")))
    g.add((beta, RDF.type, URIRef(DOMAIN_NS + "Beta")))
    g.add((alpha, URIRef(DOMAIN_NS + "alphaHasBeta"), beta))
    return g


def _install_world(base, monkeypatch):
    """T-Box·A-Box 통계·딕셔너리·보고서·feedback 경로를 ``base`` 아래로 돌린다."""
    base.mkdir(parents=True, exist_ok=True)
    tbox = base / "t_box.ttl"
    tbox.write_text(_TBOX_TTL, encoding="utf-8")
    abox_dir = base / "abox"
    abox_dir.mkdir(exist_ok=True)
    (abox_dir / "abox_stats.json").write_text(json.dumps(_ABOX_STATS), encoding="utf-8")
    sem = base / "semantic_dictionary.json"
    sem.write_text(json.dumps(_SEM_DICT), encoding="utf-8")
    generated = base / "generated"
    generated.mkdir(exist_ok=True)

    monkeypatch.setattr(cqmod, "TBOX_PATH", str(tbox))
    monkeypatch.setattr(config, "TBOX_PATH", str(tbox))
    monkeypatch.setattr(cqmod, "SEMANTIC_DICT_PATH", str(sem))
    monkeypatch.setattr(qt, "SEMANTIC_DICT_PATH", str(sem))
    monkeypatch.setattr(config, "GENERATED_ABOX_DIR", str(abox_dir))
    monkeypatch.setattr(config, "GENERATED_DIR", str(generated))
    monkeypatch.setattr("tools.cq_feedback.CQ_FEEDBACK_PATH", str(base / "cq_feedback.json"))
    # 저장소 rules 의 테이블 매핑과 무관하게 이름을 그대로 해석한다.
    monkeypatch.setattr(cqmod, "_load_table_class_map", lambda: {})
    monkeypatch.setattr(qt, "_load_table_class_map", lambda: {})
    monkeypatch.setattr(
        "tools.sparql_local._get_graph", lambda *_a, **_k: (_abox_graph(), "test-graph"),
    )
    monkeypatch.setattr(qt.webbrowser, "open", lambda *_a, **_k: None)


def _write_cqs(base, monkeypatch, document):
    """CQ 문서를 쓰고 세 소비 모듈의 CQ 경로를 그 파일로 돌린다."""
    base.mkdir(parents=True, exist_ok=True)
    path = base / "competency_questions.json"
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
    for module in (cqmod, qt, mt):
        monkeypatch.setattr(module, "_CQ_PATH", str(path))
    return path


def _install_s2(base, monkeypatch, record: dict) -> None:
    """S2 초안·라운드의 외부 호출을 기록 스텁으로 바꾼다 (모델·CSV·rules 접근 없음)."""
    _install_world(base, monkeypatch)
    monkeypatch.setattr(mt, "TBOX_PATH", str(base / "s2_t_box.ttl"))
    monkeypatch.setattr(mt, "TBOX_BASELINE_PATH", str(base / "s2_t_box_baseline.ttl"))
    monkeypatch.setattr(mt, "_compute_round_quality_metrics", lambda *_a, **_k: {})
    monkeypatch.setattr(mt, "_generate_tbox_skeleton", lambda: "")
    monkeypatch.setattr(mt, "_load_anomaly_hints", lambda: "")
    monkeypatch.setattr(cqmod, "_load_csv_summary", lambda: "csv")
    # Alpha─Gamma 갭은 CSV FK 근거가 있는 실제 갭이다 → skeleton 대상.
    monkeypatch.setattr(mt, "_csv_fk_pairs", lambda: {("alpha", "gamma")})
    monkeypatch.setattr(
        "tools.tbox_generation.generate_tbox",
        lambda *_a, **_k: json.dumps({
            "success": True, "ttl": _TBOX_TTL,
            "statistics": {"classes": 3, "object_properties": 1, "data_properties": 0},
            "timing": {"total": 0.0},
        }),
    )

    def _required_ops(cqs, *_a, **_k):
        record["required_ops"].append(copy.deepcopy(cqs))
        return ""

    def _review(*_a, **_k):
        return {"issues": [], "approved": False, "summary": "ok"}

    def _sme(ttl, csv_summary, cqs, round_num, **_k):
        record["sme"].append(copy.deepcopy(cqs))
        return _review()

    def _fixes(ttl, fixes, *_a, **_k):
        record["skeletons"].extend(copy.deepcopy(fixes))
        return {"ttl": ttl, "applied": [], "skipped": [], "failed": [], "noop": []}

    def _bedrock(prompt, *_a, **kwargs):
        record["prompts"].append((kwargs.get("agent_role"), prompt))
        return _JURY_JSON

    monkeypatch.setattr(mt, "_analyze_cq_required_ops", _required_ops)
    monkeypatch.setattr(mt, "_validator_review", _review)
    monkeypatch.setattr(mt, "_sme_review", _sme)
    monkeypatch.setattr(mt, "_architect_revise", lambda ttl, *_a, **_k: ttl)
    monkeypatch.setattr("tools.jury_fixes.apply_jury_fixes", _fixes)
    monkeypatch.setattr(mt, "_invoke_bedrock", _bedrock)


def _new_record() -> dict:
    return {"required_ops": [], "sme": [], "skeletons": [], "prompts": []}


# ── 판정 헬퍼 ─────────────────────────────────────────────────────────


def _error_meta(node) -> list[tuple[object, object]]:
    """노드 아래 ``input_error`` 진단을 ``(source_index, reason)`` 으로 모은다 (순서 유지).

    행 단위 ``input_error`` (중첩 dict 또는 평면 필드) 와 결과 단위 ``input_errors``
    목록을 모두 읽는다. 배치는 구현 선택이고, 계약은 위치와 고정 사유뿐이다.
    """
    found: list[tuple[object, object]] = []

    def walk(value) -> None:
        if isinstance(value, dict):
            meta = value.get("input_error")
            if isinstance(meta, dict):
                found.append((meta.get("source_index"), meta.get("reason")))
            elif meta:
                reason = meta if isinstance(meta, str) else value.get("reason")
                found.append((value.get("source_index"), reason))
            for item in value.get("input_errors") or []:
                if isinstance(item, dict):
                    found.append((item.get("source_index"), item.get("reason")))
            for key, child in value.items():
                if key not in ("input_error", "input_errors"):
                    walk(child)
        elif isinstance(value, list | tuple):
            for child in value:
                walk(child)

    walk(node)
    return list(dict.fromkeys(found))


def _suggestion_index(suggestion: dict) -> object:
    """feedback 제안의 원본 위치 (중첩 진단 또는 평면 필드)."""
    meta = suggestion.get("input_error")
    if isinstance(meta, dict):
        return meta.get("source_index")
    return suggestion.get("source_index")


def _assert_rows(rows: list, document: list, invalid: tuple[int, ...], fail_statuses: set[str]) -> None:
    """행이 원본 분모·순서를 지키고 입력 오류 행만 원본 위치 진단을 갖는지 본다."""
    assert len(rows) == len(document), "원본 항목이 분모에서 빠지거나 늘었다"
    for position, (row, entry) in enumerate(zip(rows, document), 1):
        if isinstance(entry, dict) and "id" in entry:
            assert row["id"] == entry["id"], f"{position}번째 행 순서가 원본과 다르다"
        meta = _error_meta(row)
        if position in invalid:
            assert row["status"] in fail_statuses, f"{position}번째 입력 오류가 성공으로 보고됐다"
            assert {index for index, _ in meta} == {position}, f"{position}번째 원본 위치 진단 누락: {meta}"
            assert all(isinstance(reason, str) and reason for _, reason in meta)
        else:
            assert meta == [], f"{position}번째 정상 항목에 입력 오류 진단이 붙었다"


def _reason_at(rows: list, position: int) -> str:
    return _error_meta(rows[position - 1])[0][1]


def _assert_no_leak(*texts: str) -> None:
    for text in texts:
        assert _LEAK not in text, "거부된 원문이 출력·로그·모델 입력으로 옮겨졌다"


def _s2_round(base, monkeypatch, document, record: dict, rounds=((1, 3),)) -> tuple[dict, list]:
    """S2 초안을 만들고 지정 라운드를 돌려 ``(state, summaries)`` 를 돌려준다."""
    _install_s2(base, monkeypatch, record)
    _write_cqs(base, monkeypatch, document)
    state = mt._build_initial_draft("")
    assert "error" not in state, state
    summaries = [mt._run_one_debate_round(state, n, m) for n, m in rounds]
    return state, summaries


# ── 소비자 실행기 (T6·T7 공용) ─────────────────────────────────────────


def _run_consumer(consumer: str, base, monkeypatch, document) -> dict:
    """소비자 하나를 ``base`` 환경에서 실행해 JSON 결과를 돌려준다."""
    _install_world(base, monkeypatch)
    _write_cqs(base, monkeypatch, document)
    if consumer.startswith("coverage_"):
        return json.loads(cqmod.check_cq_coverage(phase=consumer.removeprefix("coverage_")))
    if consumer == "answerability":
        return json.loads(cqmod.validate_competency_questions())
    if consumer == "s12":
        return json.loads(qt.test_domain_queries(verify_joins=False))
    if consumer == "tbox_only":
        return json.loads(qt.validate_cq_tbox_only())
    if consumer == "s2_runtime":
        return json.loads(json.dumps(
            mt._cq_runtime_check(_TBOX_TTL, copy.deepcopy(document)), ensure_ascii=False,
        ))
    if consumer == "s2_round":
        record = _new_record()
        _, (summary,) = _s2_round(base, monkeypatch, document, record)
        return json.loads(json.dumps(summary["round_log"]["cq_runtime"], ensure_ascii=False))
    raise AssertionError(consumer)


_ROWS_KEY = {
    "coverage_tbox": "details", "answerability": "results", "s12": "results", "tbox_only": "results",
}
_PASSED_KEY = {
    "coverage_tbox": "covered", "answerability": "answerable", "s12": "passed", "tbox_only": "passed",
}

_VOLATILE = {"duration_seconds", "report_path", "domains"}


def _normalize(value):
    """실행마다 달라지는 값과 원본 domains 반향을 지워 결과를 비교 가능하게 만든다."""
    if isinstance(value, dict):
        return {k: _normalize(v) for k, v in value.items() if k not in _VOLATILE}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


# ── T1: 커버리지 3단계 ────────────────────────────────────────────────


@pytest.mark.parametrize("phase", ["tbox", "abox", "inferred"])
def test_coverage_phases_keep_raw_denominator_order_and_positions(phase, tmp_path, monkeypatch, caplog):
    """입력 오류는 gap 으로 원래 자리에 남고, 정상 갭·빈 도메인 판정은 그대로다."""
    _install_world(tmp_path, monkeypatch)
    path = _write_cqs(tmp_path, monkeypatch, _MIXED)
    before = path.read_bytes()

    with caplog.at_level(logging.DEBUG):
        raw = cqmod.check_cq_coverage(phase=phase)
    result = json.loads(raw)

    assert result["success"] is True, result
    details = result["details"]
    _assert_rows(details, _MIXED, _MIXED_INVALID, {"gap"})
    assert [row["status"] for row in details] == [
        "covered", "gap", "gap", "gap", "gap", "gap", "gap", "covered", "covered", "covered", "gap",
    ]
    summary = result["summary"]
    assert (summary["total"], summary["covered"], summary["gap"]) == (11, 4, 7)
    assert summary["coverage_rate"] == round(4 / 11 * 100, 1)
    assert len(result["gaps"]) == 7
    # 같은 종류의 오류는 원문과 무관한 같은 사유를 쓴다.
    assert _reason_at(details, 3) == _reason_at(details, 11)
    _assert_no_leak(raw, caplog.text)
    assert path.read_bytes() == before, "평가가 입력 파일을 바꿨다"


# ── T2: 전체 답변가능성 ───────────────────────────────────────────────


def test_answerability_keeps_invalid_rows_not_answerable(tmp_path, monkeypatch, caplog):
    """dict domains 처럼 키가 정상 클래스여도 성공으로 세지 않고, 실제 0건 갭은 계속 실패한다."""
    _install_world(tmp_path, monkeypatch)
    _write_cqs(tmp_path, monkeypatch, _MIXED)

    with caplog.at_level(logging.DEBUG):
        raw = cqmod.validate_competency_questions()
    result = json.loads(raw)

    assert result["success"] is True, result
    rows = result["results"]
    _assert_rows(rows, _MIXED, _MIXED_INVALID, {"NOT_ANSWERABLE"})
    ok, bad = "ANSWERABLE", "NOT_ANSWERABLE"
    assert [row["status"] for row in rows] == [ok, bad, bad, bad, bad, bad, bad, ok, ok, ok, bad]
    summary = result["summary"]
    assert (summary["total"], summary["answerable"], summary["not_answerable"]) == (11, 4, 7)
    assert summary["answer_rate"] == round(4 / 11 * 100, 1)

    per_cq = result["answerability"]["per_cq"]
    assert len(per_cq) == 11, "answerability 분포가 원본 분모를 잃었다"
    assert [per_cq[p - 1]["answerability"] for p in _MIXED_INVALID] == ["unanswerable"] * 6
    assert sum(result["answerability"]["distribution"].values()) == 11
    assert _reason_at(rows, 3) == _reason_at(rows, 11)
    _assert_no_leak(raw, caplog.text)


# ── T3: S4 T-Box 전용 검증 ────────────────────────────────────────────


def test_tbox_only_validator_fails_malformed_domains_without_repair_advice(tmp_path, monkeypatch, caplog):
    """입력 오류는 FAIL 이고, T-Box 수정 제안은 정상 CQ 의 실제 갭에서만 나온다."""
    _install_world(tmp_path, monkeypatch)
    _write_cqs(tmp_path, monkeypatch, _MIXED)

    with caplog.at_level(logging.DEBUG):
        raw = qt.validate_cq_tbox_only()
    result = json.loads(raw)

    assert result["success"] is True, result
    rows = result["results"]
    _assert_rows(rows, _MIXED, _MIXED_INVALID, {"FAIL"})
    assert [row["status"] for row in rows] == [
        "PASS", "FAIL", "FAIL", "FAIL", "FAIL", "FAIL", "FAIL", "PASS", "PASS", "PASS", "FAIL",
    ]
    assert result["summary"]["total"] == 11
    assert (result["summary"]["passed"], result["summary"]["failed"]) == (4, 7)

    by_type = {s["type"]: s for s in result["suggestions"]}
    assert "missing_classes" not in by_type, "입력 오류가 클래스 추가 요청이 됐다"
    assert by_type["missing_connections"]["pairs"] == ["Alpha↔Gamma"]
    _assert_no_leak(raw, caplog.text)


# ── T4: S12 중복 id 와 feedback resolved ──────────────────────────────


_S12_DOC = [
    {"id": "CQ-DUP", "question_ko": "중복 정상", "domains": ["Alpha", "Beta"]},
    {"id": "CQ-DUP", "question_ko": "중복 오류", "domains": {"Alpha": "LEAK-DUP-0006"}},
    {"id": "CQ-OK", "question_ko": "정상", "domains": ["Alpha", "Beta"]},
    "LEAK-S12-CQ-0007",
    {"id": "CQ-GAP", "question_ko": "실제 갭", "domains": ["Alpha", "Gamma"]},
    {"id": "CQ-NULL", "question_ko": "널", "domains": None},
]


def test_s12_duplicate_id_with_failing_row_is_not_resolved(tmp_path, monkeypatch, caplog):
    """같은 id 에 FAIL 행이 있으면 resolved 가 아니고, 입력 오류는 invalid_input 으로만 남는다."""
    from tools.cq_feedback import format_feedback_for_prompt, load_active_suggestions

    _install_world(tmp_path, monkeypatch)
    feedback_path = tmp_path / "cq_feedback.json"
    feedback_path.write_text(json.dumps({"version": 1, "iterations": [{
        "timestamp": datetime.now().isoformat(),
        "pass_rate": 0.0, "total_cqs": 3, "passed": 0, "failed": 3,
        "suggestions": [
            {"cq_id": cq_id, "type": "missing_connection", "class_a": "Alpha",
             "class_b": "Beta", "action": "이전 실패"}
            for cq_id in ("CQ-DUP", "CQ-OK", "CQ-GAP")
        ],
        "resolved_from_previous": [],
    }]}), encoding="utf-8")
    _write_cqs(tmp_path, monkeypatch, _S12_DOC)

    with caplog.at_level(logging.DEBUG):
        raw = qt.test_domain_queries(verify_joins=False)
    result = json.loads(raw)

    assert result["success"] is True, result
    rows = result["results"]
    _assert_rows(rows, _S12_DOC, (2, 4), {"FAIL"})
    assert [row["status"] for row in rows] == ["PASS", "FAIL", "PASS", "FAIL", "FAIL", "PASS"]
    summary = result["summary"]
    assert (summary["total"], summary["passed"], summary["failed"]) == (6, 3, 3)

    suggestions = result["improvement_suggestions"]
    invalid = [s for s in suggestions if s.get("type") == "invalid_input"]
    assert sorted(_suggestion_index(s) for s in invalid) == [2, 4]
    assert all(s["type"] == "invalid_input" for s in suggestions if s.get("cq_id") == "CQ-DUP")
    assert "missing_instances" in {s["type"] for s in suggestions if s.get("cq_id") == "CQ-GAP"}

    latest = json.loads(feedback_path.read_text(encoding="utf-8"))["iterations"][-1]
    assert latest["resolved_from_previous"] == ["CQ-OK"], "실패 행이 있는 중복 id 를 resolved 로 기록했다"
    assert (latest["total_cqs"], latest["passed"], latest["failed"]) == (6, 3, 3)

    # 다음 S2 Architect 프롬프트: 정상 갭은 남고 입력 오류 CQ 는 수정 요청이 되지 않는다.
    prompt = format_feedback_for_prompt(load_active_suggestions(), pass_rate=summary["pass_rate"])
    assert "Gamma" in prompt
    assert "CQ-DUP" not in prompt

    report = (tmp_path / "generated" / "reports" / "query_test_report.html").read_text(encoding="utf-8")
    _assert_no_leak(raw, feedback_path.read_text(encoding="utf-8"), report, prompt, caplog.text)


# ── T5: S2 런타임 판정 ────────────────────────────────────────────────


def test_runtime_check_counts_invalid_entries_in_source_order(monkeypatch):
    """입력 오류는 원래 순서대로 unanswerable 에 남고 정상 갭 사유는 바뀌지 않는다."""
    monkeypatch.setattr(cqmod, "_load_table_class_map", lambda: {})

    result = mt._cq_runtime_check(_TBOX_TTL, copy.deepcopy(_S2_DOC))

    assert result["answerable"] == ["S2-OK"]
    assert result["coverage_pct"] == round(1 / 6 * 100, 1), "분모가 원본 6건이 아니다"
    ids = [item[0] for item in result["unanswerable"]]
    assert len(ids) == 4, "null domains 는 기존 [] 처럼 어느 목록에도 들지 않아야 한다"
    assert (ids[0], ids[2], ids[3]) == ("S2-BAD", "S2-GAP", "S2-MIX")
    gap_reason = result["unanswerable"][2][1]
    assert "gamma" in gap_reason.lower() and "경로 없음" in gap_reason
    assert [index for index, _ in _error_meta(result)] == [2, 3, 5]
    _assert_no_leak(json.dumps(result, ensure_ascii=False, default=str))


# ── T6: null / 누락 domains 호환 ─────────────────────────────────────


_COMPAT_CONSUMERS = [
    "coverage_tbox", "coverage_abox", "coverage_inferred",
    "answerability", "s12", "tbox_only", "s2_runtime",
]


@pytest.mark.parametrize("variant", ["null", "missing"])
@pytest.mark.parametrize("consumer", _COMPAT_CONSUMERS)
def test_null_and_missing_domains_match_existing_empty_list(consumer, variant, tmp_path, monkeypatch):
    """domains=null·누락은 각 소비자의 기존 domains=[] 결과와 같다."""
    def document(kind: str) -> list:
        entry = {"id": "CQ-E", "question_ko": "빈 도메인"}
        if kind == "empty":
            entry["domains"] = []
        elif kind == "null":
            entry["domains"] = None
        return [copy.deepcopy(_VALID_OK), entry]

    expected = _run_consumer(consumer, tmp_path / "empty", monkeypatch, document("empty"))
    actual = _run_consumer(consumer, tmp_path / variant, monkeypatch, document(variant))

    assert expected.get("success", True) is True, expected
    assert _normalize(actual) == _normalize(expected)


# ── T7: 루트 문서와 전부 오류 ─────────────────────────────────────────


_ROOT_CONSUMERS = ["coverage_tbox", "answerability", "s12", "tbox_only", "s2_round"]
_ROOTS = {
    "empty_array": [],
    "all_invalid": ["LEAK-ROOT-CQ-0020", {"id": "CQ-ZERO", "question_ko": "영", "domains": 0}],
    "object_root": {},
    "wrapped_root": {"questions": [_VALID_OK]},
    "string_root": "",
}
#: 기존 정책: 빈 배열 루트를 오류로 보는 소비자 (그 밖은 0건 성공).
_EMPTY_ARRAY_IS_ERROR = {"coverage_tbox", "answerability"}


@pytest.mark.parametrize("root", sorted(_ROOTS))
@pytest.mark.parametrize("consumer", _ROOT_CONSUMERS)
def test_root_documents_and_all_invalid_arrays(consumer, root, tmp_path, monkeypatch, caplog):
    """비배열 루트는 문서 오류, 전부 오류인 배열은 0% 결과, 빈 배열은 기존 정책."""
    with caplog.at_level(logging.DEBUG):
        result = _run_consumer(consumer, tmp_path / root, monkeypatch, _ROOTS[root])
    _assert_no_leak(json.dumps(result, ensure_ascii=False), caplog.text)

    if consumer == "s2_round":
        if root == "empty_array":
            assert result["coverage_pct"] == 100.0
        elif root == "all_invalid":
            assert (result["coverage_pct"], result["unanswerable_count"]) == (0.0, 2)
        else:
            assert result["coverage_pct"] != 100.0, "비배열 루트가 CQ 100% 로 보고됐다"
        return

    if root == "empty_array":
        if consumer in _EMPTY_ARRAY_IS_ERROR:
            assert result["success"] is False
        else:
            assert result["success"] is True and result["summary"]["total"] == 0
        return
    if root == "all_invalid":
        assert result["success"] is True, result
        _assert_rows(result[_ROWS_KEY[consumer]], _ROOTS[root], (1, 2),
                     {"gap", "NOT_ANSWERABLE", "FAIL"})
        assert result["summary"]["total"] == 2
        assert result["summary"][_PASSED_KEY[consumer]] == 0
        return

    assert result["success"] is False, f"비배열 루트가 성공으로 보고됐다: {result}"
    if consumer in _EMPTY_ARRAY_IS_ERROR:
        empty = _run_consumer(consumer, tmp_path / "baseline-empty", monkeypatch, [])
        assert result["error"] != empty["error"], "비배열 루트를 빈 목록 오류로 뭉갰다"


# ── T8: S2 원본 평가 ↔ 프롬프트 필터 분리 ────────────────────────────


def test_s2_round_evaluates_raw_inventory_while_prompts_stay_filtered(tmp_path, monkeypatch, caplog):
    """S2 런타임 분모는 원본 6건, 프롬프트는 기존 필터 4건, 입력 오류는 수정 요청이 아니다."""
    record = _new_record()
    path_doc = copy.deepcopy(_S2_DOC)
    expected_prompt = [copy.deepcopy(_S2_DOC[p - 1]) for p in _S2_PROMPT_POSITIONS]

    with caplog.at_level(logging.DEBUG):
        state, (first, final) = _s2_round(
            tmp_path, monkeypatch, path_doc, record, rounds=((1, 3), (3, 3)),
        )
    cq_path = tmp_path / "competency_questions.json"

    # 프롬프트 경로는 기존 필터 계약 그대로다.
    assert state["cqs"] == expected_prompt
    assert record["required_ops"] == [expected_prompt]
    assert record["sme"] and all(cqs == expected_prompt for cqs in record["sme"])

    # 평가 경로는 원본 분모를 쓴다: 답변 가능 1 / 원본 6, 미답변 = 입력 오류 3 + 실제 갭 1.
    for summary in (first, final):
        runtime = summary["round_log"]["cq_runtime"]
        assert runtime["coverage_pct"] == round(1 / 6 * 100, 1)
        assert runtime["unanswerable_count"] == 4

    # 입력 오류는 OP skeleton 이 되지 않는다 (FK 근거 있는 실제 갭만 주입).
    assert record["skeletons"], "실제 FK 갭의 skeleton 이 사라졌다"
    assert {s.get("source") for s in record["skeletons"]} == {"cq_gap:S2-GAP"}

    # Jury 의 CQ 수정 요청 블록에는 실제 갭만 있다.
    jury_prompts = [prompt for role, prompt in record["prompts"] if role == "jury"]
    assert jury_prompts, "최종 라운드 Jury 가 호출되지 않았다"
    block = jury_prompts[-1].split("CQ Reachability", 1)[1].split("\n## ", 1)[0]
    requested = [line[2:].split(":", 1)[0].strip() for line in block.splitlines() if line.startswith("- ")]
    assert requested == ["S2-GAP"], f"입력 오류가 온톨로지 수정 요청이 됐다: {requested}"
    assert "16.7" in block

    _assert_no_leak(
        *(prompt for _, prompt in record["prompts"]),
        json.dumps(state["debate_log"], ensure_ascii=False, default=str),
        caplog.text,
    )
    assert json.loads(cq_path.read_text(encoding="utf-8")) == _S2_DOC, "S2 가 입력 파일을 바꿨다"
