"""S2 CQ 입력 오류 정지 계약: 검수자 독립 실행 테스트.

승인된 계약만 S2 라운드·저장 진입점에서 확인한다.

- 런타임 CQ 평가와 원본 분모는 그대로 두고, 결정적 입력 오류는
  ``round_log.cq_block_input_error`` 로만 분리한다 (fixable/data_gap/OP 수정 입력 아님).
- 런타임 비입력 갭과 SME ``unanswerable_cqs`` 를 함께 분류한다. 중복 제거는 내용 기준
  (JSON list 와 tuple 동일 취급) 이고 CQ id 기준이 아니다.
- 입력 오류만 남고 두 리뷰어가 실질 승인했으며 고칠 갭·차단·veto·파싱 불확실성이 없을 때만
  첫 라운드부터 ``invalid_cq_input`` 으로 멈춘다. 이때 Architect 수정과 Jury 를 부르지 않는다.
- 저장 결과는 ``success=False, partial=True, needs_input=True`` 와 정적 진단, S2 재실행
  안내를 담고, RDF 상태는 ``debate_stopped_invalid_cq_input`` 이다.

모델·CSV·rules 접근은 전부 로컬 스텁이고, 모든 쓰기 경로는 ``tmp_path`` 로 돌린다.
배포 산출물 쓰기 가드는 그대로 둔다. 필드 배치가 구현 선택인 곳은 이름에 덜 의존하게 읽는다.
"""
from __future__ import annotations

import copy
import json
import time
from collections import Counter

import pytest
from rdflib import Graph, URIRef

import config
from domain.namespaces import DOMAIN_NS
from tools import competency_questions as cqmod
from tools import debate_log_store as dls
from tools import multi_agent_tbox as mt

# ── 고정 세계: Alpha─Beta 연결, Gamma 고립. FK 근거는 Alpha─Gamma 쌍뿐 ──────────

_TBOX_TTL = f"""@prefix ex: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
ex:Alpha a owl:Class .
ex:Beta a owl:Class .
ex:Gamma a owl:Class .
ex:alphaHasBeta a owl:ObjectProperty ; rdfs:domain ex:Alpha ; rdfs:range ex:Beta .
"""

#: 거부된 원문에만 넣는 표지. 어떤 산출물에도 나오면 안 된다.
_LEAK = "LEAK"

#: ``_cq_entry_error`` 의 고정 사유. 입력 오류는 이 문구로만 식별한다.
_STATIC_REASONS = (
    "expected an object",
    "domains must be a list or null",
    "domains entries must be strings",
)

_OK = {"id": "CQ-OK", "question_ko": "알파와 베타", "domains": ["Alpha", "Beta"]}
#: FK 근거 없는 Beta─Gamma 갭 → data_gap.
_DATA = {"id": "CQ-DATA", "question_ko": "베타와 감마", "domains": ["Beta", "Gamma"]}
#: FK 근거 있는 Alpha─Gamma 갭 → fixable. 같은 id 의 입력 오류 항목과 짝을 이룬다.
_FIX = {"id": "CQ-DUP", "question_ko": "알파와 감마", "domains": ["Alpha", "Gamma"]}
_DUP_BAD = {"id": "CQ-DUP", "question_ko": "중복 id 문자열", "domains": "LEAK-DUP-04"}
_STR = {"id": "CQ-STR", "question_ko": "문자열 도메인", "domains": "LEAK-STR-01"}
_TEXT = "LEAK-TEXT-02"
_ELEM = {"id": "CQ-ELEM", "question_ko": "원소 오류", "domains": ["Alpha", {"LEAK-ELEM-03": 1}]}

#: 입력 오류 + 데이터 갭만 있는 목록. 원본 위치 3·4·5 가 입력 오류다.
_INPUT_ONLY = [_OK, _DATA, _STR, _TEXT, _ELEM]
_INPUT_ONLY_IDS = Counter({"CQ-STR": 1, "invalid-cq-4": 1, "CQ-ELEM": 1})

_APPROVED_FALSE = {"issues": [], "approved": False, "summary": "차단 이슈 없음"}
_APPROVED_TRUE = {"issues": [], "approved": True, "summary": "승인"}
#: 실행 가능한 차단 이슈 (``tests/test_s2_early_stop.py`` 의 executable 형태).
_EXECUTABLE = {"severity": "critical", "category": "logic", "target": "OP 0",
               "fix": "add_object_property 로 추가하라"}
#: 차단은 아니지만 veto 후보가 되는 이슈 (``metric`` 범주).
_METRIC = {"severity": "critical", "category": "metric", "target": "metric 지표",
           "fix": "S3 지표 보강"}

_GUARD = {"phantom_iris_repaired": 0, "collapse_blocked": False, "probe": "guard-kept"}


# ── 환경 ─────────────────────────────────────────────────────────────


def _no_model(*_a, **_k):
    raise AssertionError("테스트가 실제 모델 호출 경로에 도달했다")


def _setup(base, monkeypatch, document, *, validator=None, sme=None, jury_ready=True):
    """S2 초안 state 를 만들고 외부 호출을 기록 스텁으로 바꾼다."""
    rec: dict = {"architect": 0, "roles": [], "fixes": []}
    base.mkdir(parents=True, exist_ok=True)
    tbox = base / "t_box.ttl"
    tbox.write_text(_TBOX_TTL, encoding="utf-8")
    abox_dir = base / "abox"
    abox_dir.mkdir(exist_ok=True)
    (abox_dir / "abox_stats.json").write_text(json.dumps({"per_class": {}}), encoding="utf-8")
    sem = base / "semantic_dictionary.json"
    sem.write_text(json.dumps({"classes": {}, "object_properties": {}}), encoding="utf-8")
    (base / "generated").mkdir(exist_ok=True)
    cq_path = base / "competency_questions.json"
    cq_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(cqmod, "TBOX_PATH", str(tbox))
    monkeypatch.setattr(config, "TBOX_PATH", str(tbox))
    monkeypatch.setattr(cqmod, "SEMANTIC_DICT_PATH", str(sem))
    monkeypatch.setattr(config, "GENERATED_ABOX_DIR", str(abox_dir))
    monkeypatch.setattr(config, "GENERATED_DIR", str(base / "generated"))
    monkeypatch.setattr("tools.cq_feedback.CQ_FEEDBACK_PATH", str(base / "cq_feedback.json"))
    monkeypatch.setattr(cqmod, "_load_table_class_map", lambda: {})
    monkeypatch.setattr(cqmod, "_load_csv_summary", lambda: "csv")
    for module in (cqmod, mt):
        monkeypatch.setattr(module, "_CQ_PATH", str(cq_path))

    monkeypatch.setattr(mt, "TBOX_PATH", str(base / "s2_t_box.ttl"))
    monkeypatch.setattr(mt, "TBOX_BASELINE_PATH", str(base / "s2_t_box_baseline.ttl"))
    monkeypatch.setattr(dls, "DEBATE_LOG_PATH", str(base / "debate_log.json"))
    monkeypatch.setattr(mt, "_record_s2_quality_history", lambda *_a, **_k: None)
    monkeypatch.setattr(mt, "_guard_before_save", lambda ttl: (ttl, dict(_GUARD)))
    monkeypatch.setattr(mt, "_compute_round_quality_metrics", lambda *_a, **_k: {})
    monkeypatch.setattr(mt, "_generate_tbox_skeleton", lambda: "")
    monkeypatch.setattr(mt, "_load_anomaly_hints", lambda: "")
    monkeypatch.setattr(mt, "_analyze_cq_required_ops", lambda *_a, **_k: "")
    monkeypatch.setattr(mt, "_csv_fk_pairs", lambda: {("alpha", "gamma")})
    monkeypatch.setattr(
        "tools.tbox_generation.generate_tbox",
        lambda *_a, **_k: json.dumps({
            "success": True, "ttl": _TBOX_TTL,
            "statistics": {"classes": 3, "object_properties": 1, "data_properties": 0},
            "timing": {"total": 0.0},
        }),
    )
    monkeypatch.setattr(
        mt, "_validator_review",
        lambda *_a, **_k: copy.deepcopy(validator or _APPROVED_FALSE),
    )
    monkeypatch.setattr(mt, "_sme_review", lambda *_a, **_k: copy.deepcopy(sme or _APPROVED_TRUE))

    def _revise(ttl, *_a, **_k):
        rec["architect"] += 1
        return ttl

    def _fixes(ttl, fixes, *_a, **_k):
        rec["fixes"].extend(copy.deepcopy(fixes))
        return {"ttl": ttl, "applied": [], "skipped": [], "failed": [], "noop": []}

    def _bedrock(_prompt, *_a, **kwargs):
        rec["roles"].append(kwargs.get("agent_role"))
        return json.dumps({
            "production_ready": jury_ready, "required_fixes": [], "decisions": [],
            "jury_issues": [], "rationale": "stub",
        })

    monkeypatch.setattr(mt, "_architect_revise", _revise)
    monkeypatch.setattr("tools.jury_fixes.apply_jury_fixes", _fixes)
    monkeypatch.setattr(mt, "_invoke_bedrock", _bedrock)
    for name in ("invoke_bedrock_with_metadata", "invoke_bedrock_text", "_bedrock_client"):
        monkeypatch.setattr(f"tools.bedrock.{name}", _no_model)
    monkeypatch.setenv("MULTI_AGENT_PARALLEL", "false")
    monkeypatch.delenv("S2_EARLY_STOP", raising=False)
    monkeypatch.delenv("S2_CQ_MAX_HOPS", raising=False)

    state = mt._build_initial_draft("")
    assert "error" not in state, state
    return state, rec


# ── 판독 헬퍼 ─────────────────────────────────────────────────────────


def _norm(item):
    """JSON list 와 tuple 을 같은 값으로 본다."""
    return tuple(item) if isinstance(item, list | tuple) else item


def _entry_id(item):
    if isinstance(item, dict):
        return item.get("cq_id") or item.get("id")
    if isinstance(item, list | tuple):
        return item[0]
    return item


def _entry_reason(item) -> str:
    if isinstance(item, dict):
        return str(item.get("reason") or "")
    if isinstance(item, list | tuple):
        return str(item[1]) if len(item) > 1 else ""
    return str(item)


def _is_input_error(item) -> bool:
    return any(reason in _entry_reason(item) for reason in _STATIC_REASONS)


def _bucket(round_log: dict, key: str) -> Counter:
    return Counter(_norm(item) for item in round_log.get(key) or [])


def _walk(node):
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list | tuple):
        for value in node:
            yield from _walk(value)


def _pairs(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key, value
            yield from _pairs(value)
    elif isinstance(node, list | tuple):
        for value in node:
            yield from _pairs(value)


def _saved_status(path: str) -> dict[str, str]:
    """저장된 T-Box 의 ``multi_agent_debate:`` 상태 리터럴을 key=value 로 푼다."""
    g = Graph()
    g.parse(path, format="turtle")
    prefix = "multi_agent_debate: "
    texts = [
        str(o) for o in g.objects(None, URIRef("http://purl.org/dc/terms/description"))
        if str(o).startswith(prefix)
    ]
    assert len(texts) == 1, texts
    return dict(part.split("=", 1) for part in texts[0][len(prefix):].split(" | "))


def _assert_input_only_stop(summary: dict, state: dict, rec: dict) -> None:
    log = summary["round_log"]
    assert summary["break_loop"] is True, "입력 전용 라운드가 종료되지 않았다"
    assert summary["consensus"] is False
    assert summary.get("stop_reason") == "invalid_cq_input"
    assert state["early_stop_reason"] == "invalid_cq_input"
    assert (log.get("early_stop") or {}).get("reason") == "invalid_cq_input"
    assert state["consensus_reached"] is False, "입력 오류가 남았는데 합의가 승인됐다"
    assert rec["architect"] == 0, "입력 전용 종료에서 Architect 수정을 불렀다"
    assert rec["roles"] == [], f"입력 전용 종료에서 Jury·절충 호출이 일어났다: {rec['roles']}"
    assert rec["fixes"] == [], "입력 전용 종료에서 OP 수정이 주입됐다"


def _assert_not_input_stop(summary: dict, state: dict) -> None:
    log = summary["round_log"]
    assert summary.get("stop_reason") != "invalid_cq_input"
    assert state.get("early_stop_reason") != "invalid_cq_input"
    assert (log.get("early_stop") or {}).get("reason") != "invalid_cq_input"
    assert summary["consensus"] is False and state["consensus_reached"] is False


# ── T1: 입력 전용 라운드 종료 (첫 라운드 / 마지막 라운드 / 기존 스위치 off) ─────


@pytest.mark.parametrize(
    ("round_num", "max_rounds", "early_stop_env"),
    [(1, 3, None), (3, 3, None), (1, 3, "off")],
    ids=["first-round", "final-round", "expressibility-switch-off"],
)
def test_input_only_round_stops_without_architect_or_jury(
    round_num, max_rounds, early_stop_env, tmp_path, monkeypatch,
):
    """입력 오류와 데이터 갭만 남으면 원본 분모를 유지한 채 곧바로 ``invalid_cq_input`` 으로 멈춘다."""
    state, rec = _setup(tmp_path, monkeypatch, copy.deepcopy(_INPUT_ONLY))
    if early_stop_env:
        monkeypatch.setenv("S2_EARLY_STOP", early_stop_env)

    summary = mt._run_one_debate_round(state, round_num, max_rounds)
    log = summary["round_log"]

    # 8fb236e 평가 분모는 그대로다: 답변 가능 1 / 원본 5, 미답변 = 입력 오류 3 + 데이터 갭 1.
    runtime = log["cq_runtime"]
    assert (runtime["total"], runtime["coverage_pct"], runtime["unanswerable_count"]) == (5, 20.0, 4)

    # 입력 오류는 별도 버킷에만 있고, 데이터 갭은 따로 보인다.
    assert Counter(_entry_id(i) for i in log.get("cq_block_input_error") or []) == _INPUT_ONLY_IDS
    assert all(_is_input_error(i) for i in log["cq_block_input_error"])
    assert [_entry_id(i) for i in log.get("cq_block_data_gap") or []] == ["CQ-DATA"]
    assert not log.get("cq_block_fixable"), "입력 오류가 고칠 수 있는 갭으로 분류됐다"

    _assert_input_only_stop(summary, state, rec)
    assert _LEAK not in json.dumps(state["debate_log"], ensure_ascii=False, default=str)


# ── T2: 저장 결과·RDF 상태·토론 기록 요약 ─────────────────────────────


@pytest.mark.parametrize("scenario", ["invalid_input", "no_expressible_control"])
def test_saved_result_marks_needs_input_without_changing_other_stops(scenario, tmp_path, monkeypatch):
    """입력 전용 종료는 needs_input 결과로 저장되고, 입력 오류 없는 기존 조기 종료 의미는 그대로다."""
    if scenario == "invalid_input":
        state, rec = _setup(tmp_path, monkeypatch, copy.deepcopy(_INPUT_ONLY))
        summary = mt._run_one_debate_round(state, 1, 3)
        assert summary["break_loop"] is True
    else:
        state, rec = _setup(tmp_path, monkeypatch, [copy.deepcopy(_OK)])
        summary = mt._run_one_debate_round(state, 1, 3)
        assert summary["break_loop"] is False and rec["architect"] == 1
        state["early_stop_reason"] = "no_expressible_blocking_fix"

    result = json.loads(mt._finalize_and_save(state, time.monotonic()))
    status = _saved_status(mt.TBOX_PATH)
    assert result["save_guard"] == _GUARD, "저장 가드 진단이 응답에서 사라졌다"
    assert result["debate"]["consensus_reached"] is False
    assert status["consensus_reached"] == "false"

    if scenario == "no_expressible_control":
        assert status["debate_status"] == "debate_stopped_no_expressible_fix"
        assert status["early_stop_reason"] == "no_expressible_blocking_fix"
        assert result["success"] is True and result["partial"] is False
        assert not result.get("needs_input")
        assert result["debate"]["early_stop_reason"] == "no_expressible_blocking_fix"
        return

    assert status["debate_status"] == "debate_stopped_invalid_cq_input"
    assert status["early_stop_reason"] == "invalid_cq_input"
    assert (result["success"], result["partial"], result.get("needs_input")) == (False, True, True)
    assert result["debate"]["early_stop_reason"] == "invalid_cq_input"

    # 라운드 원본 밖에도 정적 입력 오류 진단과 S2 재실행 안내가 있어야 한다.
    outside = copy.deepcopy(result)
    outside["debate"].pop("rounds", None)
    error_lists = [
        node for node in _walk(outside)
        if isinstance(node, list) and node and all(_is_input_error(e) for e in node)
    ]
    assert any(len(node) == 3 for node in error_lists), f"정적 입력 오류 진단 누락: {error_lists}"
    for node in error_lists:
        indexes = {e.get("source_index") for e in node if isinstance(e, dict) and "source_index" in e}
        assert indexes in (set(), {3, 4, 5}), f"원본 위치가 틀렸다: {indexes}"
    assert any(
        isinstance(text, str) and "S2" in text and "CQ" in text for text in _walk(outside)
    ), "CQ 입력 수정 후 S2 재실행 안내가 없다"
    saved_text = (tmp_path / "s2_t_box.ttl").read_text(encoding="utf-8")
    assert _LEAK not in json.dumps(result, ensure_ascii=False) and _LEAK not in saved_text

    # 토론 기록 요약은 종료 사유와 입력 오류 수를 유지한다.
    digest = dls.summarize_latest_run(str(tmp_path / "debate_log.json"))
    assert digest is not None
    assert "invalid_cq_input" in [v for v in _walk(digest) if isinstance(v, str)]
    assert any("input_error" in str(k) and v == 3 for k, v in _pairs(digest)), digest


# ── T3: 런타임·SME 갭 병합, 내용 기준 중복 제거, 남은 작업 시 비종료 ─────────


@pytest.mark.parametrize("scenario", ["mixed_round1", "mixed_round2", "sme_gap_with_input_errors"])
def test_runtime_and_sme_gaps_merge_without_id_filtering(scenario, tmp_path, monkeypatch):
    """같은 id 의 실제 갭과 SME 전용 갭이 사라지지 않고, 고칠 작업이 남으면 입력 전용 종료가 아니다."""
    if scenario == "sme_gap_with_input_errors":
        document = [_OK, _STR, _ELEM]
        sme_gaps = [["CQ-SME2", "SME: 설비 이력 연결 불명"]]
        round_num = 1
    else:
        document = [_OK, _FIX, _DUP_BAD, _ELEM]
        sme_gaps = None
        round_num = 1 if scenario == "mixed_round1" else 2
    state, rec = _setup(tmp_path, monkeypatch, copy.deepcopy(document))

    runtime_now = mt._cq_runtime_check(state["current_ttl"], state["cq_evaluation_cqs"])
    true_gaps = [item for item in runtime_now["unanswerable"] if not _is_input_error(item)]
    if sme_gaps is None:
        assert [_entry_id(i) for i in true_gaps] == ["CQ-DUP"], "고정 세계의 실제 갭 전제가 깨졌다"
        sme_gaps = [
            list(true_gaps[0]),                                       # 런타임과 같은 내용 (list 형태)
            ["CQ-DUP", "SME: 감마 이력 해석 불가"],                   # 같은 id, 다른 사유
            ["CQ-SME", "beta → gamma 경로 없음 (SME 판단: CSV FK 부재)"],  # SME 전용 데이터 갭
            "CQ-OPAQUE 설명형 사유",                                  # 해석 불가 사유는 보수적으로 fixable
        ]
    sme = {**_APPROVED_FALSE, "unanswerable_cqs": sme_gaps}
    monkeypatch.setattr(mt, "_sme_review", lambda *_a, **_k: copy.deepcopy(sme))

    summary = mt._run_one_debate_round(state, round_num, 3)
    log = summary["round_log"]

    # 런타임 평가 수치는 SME 병합과 무관하게 8fb236e 그대로다.
    runtime = log["cq_runtime"]
    assert runtime["total"] == len(document)
    assert runtime["unanswerable_count"] == len(runtime_now["unanswerable"])

    if scenario == "sme_gap_with_input_errors":
        assert Counter(_entry_id(i) for i in log.get("cq_block_input_error") or []) == Counter(
            {"CQ-STR": 1, "CQ-ELEM": 1},
        )
        assert _bucket(log, "cq_block_fixable") == Counter({("CQ-SME2", "SME: 설비 이력 연결 불명"): 1})
        assert not log.get("cq_block_data_gap")
    else:
        assert Counter(_entry_id(i) for i in log.get("cq_block_input_error") or []) == Counter(
            {"CQ-DUP": 1, "CQ-ELEM": 1},
        )
        assert _bucket(log, "cq_block_fixable") == Counter({
            _norm(true_gaps[0]): 1,
            ("CQ-DUP", "SME: 감마 이력 해석 불가"): 1,
            "CQ-OPAQUE 설명형 사유": 1,
        }), "SME 근거 유실, id 기준 필터, 또는 내용 중복 제거 실패"
        assert _bucket(log, "cq_block_data_gap") == Counter(
            {("CQ-SME", "beta → gamma 경로 없음 (SME 판단: CSV FK 부재)"): 1},
        )
        # 실제 FK 갭은 여전히 OP 수정 입력이다.
        assert rec["fixes"] and {f.get("source") for f in rec["fixes"]} == {"cq_gap:CQ-DUP"}

    # 고칠 갭이 남았으므로 정상 작업을 계속한다.
    _assert_not_input_stop(summary, state)
    assert summary["break_loop"] is False
    assert rec["architect"] == 1, "남은 작업이 있는데 Architect 수정이 생략됐다"
    assert "jury" not in rec["roles"], "고칠 갭이 남았는데 합의 심판을 불렀다"


# ── T4: 파싱 실패·차단 이슈·veto 는 입력 전용이 아니고, 오류 없는 흐름은 그대로 ────


@pytest.mark.parametrize(
    "scenario", ["validator_parse_error", "sme_executable_blocker", "veto_lock", "no_error_control"],
)
def test_other_blockers_are_not_mistaken_for_input_only(scenario, tmp_path, monkeypatch):
    """입력 오류가 남아도 다른 불확실성이 있으면 종료 사유를 입력 전용으로 붙이지 않는다."""
    validator, sme, round_num = None, None, 1
    document = copy.deepcopy(_INPUT_ONLY)
    if scenario == "validator_parse_error":
        validator = {"issues": [], "approved": False, "parse_error": True, "summary": "파싱 실패"}
    elif scenario == "sme_executable_blocker":
        sme = {"issues": [dict(_EXECUTABLE)], "approved": False, "summary": "차단"}
    elif scenario == "veto_lock":
        sme = {"issues": [dict(_METRIC)], "approved": False, "summary": "지표"}
        round_num = 2
    else:
        document = [copy.deepcopy(_OK)]
        round_num = 2
    state, rec = _setup(tmp_path, monkeypatch, document, validator=validator, sme=sme)
    if scenario == "veto_lock":
        state["prev_sme_issues"] = [dict(_METRIC)]

    summary = mt._run_one_debate_round(state, round_num, 3)
    log = summary["round_log"]

    if scenario == "no_error_control":
        # 오류 없는 흐름: 기존대로 Jury 승인 합의, 입력 오류 흔적 없음.
        assert summary["consensus"] is True and summary["break_loop"] is True
        assert state["consensus_reached"] is True
        assert summary.get("stop_reason") is None and state.get("early_stop_reason") is None
        assert rec["roles"] == ["jury"] and rec["architect"] == 0
        assert not log.get("cq_block_input_error")
        return

    if scenario == "veto_lock":
        assert state["veto_lock_triggered"] is True, "veto 전제가 성립하지 않았다"
        assert "jury" not in rec["roles"]
    _assert_not_input_stop(summary, state)
    assert summary["break_loop"] is False and rec["architect"] == 1
    # 종료와 무관하게 입력 오류 분리는 유지된다.
    assert Counter(_entry_id(i) for i in log.get("cq_block_input_error") or []) == _INPUT_ONLY_IDS
    assert not any(_is_input_error(i) for i in log.get("cq_block_fixable") or [])
