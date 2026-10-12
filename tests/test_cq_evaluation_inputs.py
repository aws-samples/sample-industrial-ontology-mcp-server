"""CQ 입력 오류가 정상 평가와 원본 분모를 훼손하지 않는지 검증한다."""

import copy
import json
from unittest.mock import Mock

import pytest
from rdflib import Graph

from domain.namespaces import DOMAIN_NS
from tools import competency_questions as cq_tools
from tools import multi_agent_tbox as multi
from tools import query_test

_VALID = {"id": "valid", "question_ko": "설비 목록", "domains": ["Machine"]}
_REJECTED = "REJECTED_CQ_PAYLOAD"
_INVALID = [
    None,
    _REJECTED,
    ["Machine"],
    {"id": "bad", "domains": False},
    {"id": "bad", "domains": 0},
    {"id": "bad", "domains": ""},
    {"id": "bad", "domains": {_REJECTED: "Machine"}},
    {"id": "bad", "domains": ["Machine", {_REJECTED: True}]},
    {"id": "bad", "domains": ["Machine", None, 12]},
]
_CONSUMERS = ["tbox", "abox", "inferred", "s12", "tbox_only", "answerability", "runtime"]


@pytest.fixture
def evaluation(tmp_path, monkeypatch):
    """실제 소형 그래프와 임시 CQ·통계·보고서 경로를 사용한다."""
    ttl = f"""
@prefix ex: <{DOMAIN_NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
ex:Machine a owl:Class .
ex:machine1 a ex:Machine .
"""
    cq_path = tmp_path / "cqs.json"
    tbox_path = tmp_path / "tbox.ttl"
    tbox_path.write_text(ttl)
    sem_dict = {"classes": {"Machine": {}}, "object_properties": {}}
    sem_path = tmp_path / "dictionary.json"
    sem_path.write_text(json.dumps(sem_dict))
    stats = {"per_class": {"Machine": {"instance_count": 1}}}
    (tmp_path / "abox_stats.json").write_text(json.dumps(stats))
    monkeypatch.setattr(cq_tools, "_CQ_PATH", str(cq_path))
    monkeypatch.setattr(query_test, "_CQ_PATH", str(cq_path))
    monkeypatch.setattr(cq_tools, "TBOX_PATH", str(tbox_path))
    monkeypatch.setattr(cq_tools, "SEMANTIC_DICT_PATH", str(sem_path))
    monkeypatch.setattr("config.TBOX_PATH", str(tbox_path))
    monkeypatch.setattr("config.GENERATED_DIR", str(tmp_path / "generated"))
    monkeypatch.setattr("config.GENERATED_ABOX_DIR", str(tmp_path))
    monkeypatch.setattr("tools.cq_feedback.CQ_FEEDBACK_PATH", str(tmp_path / "feedback.json"))
    monkeypatch.setattr(cq_tools, "_load_table_class_map", lambda: {})
    monkeypatch.setattr(query_test, "_load_table_class_map", lambda: {})
    monkeypatch.setattr(query_test, "_load_semantic_dict", lambda: sem_dict)
    monkeypatch.setattr(query_test, "_load_abox_stats", lambda: stats)
    monkeypatch.setattr(cq_tools, "_load_tbox_entities", lambda: ({"machine"}, set()))
    graph = Graph().parse(data=ttl, format="turtle")
    monkeypatch.setattr("tools.sparql_local._get_graph", lambda _: (graph, "test graph"))

    def run(consumer, entries):
        original = copy.deepcopy(entries)
        cq_path.write_text(json.dumps(entries))
        saved = cq_path.read_bytes()
        if consumer == "runtime":
            result = multi._cq_runtime_check(ttl, entries)
        elif consumer == "s12":
            result = json.loads(query_test.test_domain_queries(verify_joins=False))
        elif consumer == "tbox_only":
            result = json.loads(query_test.validate_cq_tbox_only())
        elif consumer == "answerability":
            result = json.loads(cq_tools.validate_competency_questions())
        else:
            result = json.loads(cq_tools.check_cq_coverage(consumer))
        assert entries == original
        assert cq_path.read_bytes() == saved
        return result

    return run


@pytest.mark.parametrize("consumer", _CONSUMERS)
@pytest.mark.parametrize("invalid", _INVALID)
def test_invalid_cq_is_counted_and_following_valid_cq_is_evaluated(
    evaluation, consumer, invalid, caplog,
):
    """오류 항목을 실패로 남기고 다음 정상 CQ는 계속 평가한다."""
    result = evaluation(consumer, [invalid, _VALID])
    if consumer == "runtime":
        assert result["coverage_pct"] == 50.0
        assert result["answerable"] == ["valid"]
        assert len(result["unanswerable"]) == 1
        assert result["input_errors"][0]["source_index"] == 1
    else:
        assert result["success"], result
        summary = result["summary"]
        assert summary["total"] == 2
        if consumer in ("tbox", "abox", "inferred"):
            assert (summary["covered"], summary["gap"], summary["coverage_rate"]) == (1, 1, 50.0)
            rows = result["details"]
            assert [r["status"] for r in rows] == ["gap", "covered"]
        elif consumer == "answerability":
            assert (summary["answerable"], summary["not_answerable"], summary["answer_rate"]) == (1, 1, 50.0)
            rows = result["results"]
            assert [r["status"] for r in rows] == ["NOT_ANSWERABLE", "ANSWERABLE"]
            assert result["answerability"]["distribution"]["unanswerable"] == 1
            assert result["coverage"]["cq_classes"] == 1
        else:
            assert (summary["passed"], summary["failed"], summary["pass_rate"]) == (1, 1, 50.0)
            rows = result["results"]
            assert [r["status"] for r in rows] == ["FAIL", "PASS"]
        assert rows[0]["input_error"]["source_index"] == 1
        assert rows[1]["id"] == "valid"
        assert "input_error" not in rows[1]
    assert _REJECTED not in json.dumps(result)
    assert _REJECTED not in caplog.text


@pytest.mark.parametrize("consumer", _CONSUMERS)
@pytest.mark.parametrize("root", [None, False, 0, "", {}])
def test_non_array_document_is_an_explicit_error(evaluation, consumer, root):
    """거짓으로 평가되는 잘못된 루트도 빈 CQ 목록으로 오인하지 않는다."""
    result = evaluation(consumer, root)
    if consumer == "runtime":
        assert result["coverage_pct"] == 0.0
        assert result["unanswerable"]
        assert result["input_errors"]
    else:
        assert result["success"] is False
        assert "array" in result["error"]


@pytest.mark.parametrize("consumer", _CONSUMERS)
@pytest.mark.parametrize("domains", [{}, {"domains": None}])
def test_missing_and_null_domains_match_existing_empty_list_semantics(
    evaluation, consumer, domains,
):
    """선택 필드의 누락과 null은 해당 평가기의 기존 빈 배열 동작을 따른다."""
    empty = evaluation(consumer, [{"id": "empty", "domains": []}])
    optional = evaluation(consumer, [{"id": "empty", **domains}])
    if consumer == "runtime":
        assert optional == empty
    else:
        assert optional["success"], optional
        for key, value in empty["summary"].items():
            if "duration" not in key:
                assert optional["summary"][key] == value
        rows_key = "details" if consumer in ("tbox", "abox", "inferred") else "results"
        assert optional[rows_key] == empty[rows_key]


def test_runtime_input_errors_do_not_request_ontology_repairs(monkeypatch):
    """Jury에는 입력 정정 사유를 전달하고 OP 수정 대상으로 올리지 않는다."""
    ttl = f"@prefix ex: <{DOMAIN_NS}> . ex:Machine a <http://www.w3.org/2002/07/owl#Class> ."
    cqs = [None, {"id": "bad", "domains": ["Machine", None]}, _VALID]
    monkeypatch.setattr(cq_tools, "_load_table_class_map", lambda: {})
    runtime = multi._cq_runtime_check(ttl, cqs)
    assert multi._generate_op_skeletons_from_cq_gaps(runtime, cqs) == []
    invoke = Mock(return_value=json.dumps({
        "production_ready": False, "decisions": [], "required_fixes": [],
        "rationale": "CQ 입력 수정 필요",
    }))
    monkeypatch.setattr(multi, "_invoke_bedrock", invoke)
    multi._jury_decide(ttl, {"issues": []}, {"issues": []}, [], [], cq_runtime=runtime)
    prompt = invoke.call_args.args[0]
    assert "CQ 입력 오류" in prompt
    assert "위 CQ를 답변 가능하게 할 ObjectProperty/subClassOf" not in prompt


def test_input_feedback_keeps_source_rows_without_ontology_advice(evaluation):
    """같은 id의 입력 오류도 구분해 보존하고 Architect 수정 지시에서 제외한다."""
    from tools.cq_feedback import format_feedback_for_prompt, load_active_suggestions

    result = evaluation("s12", [
        {"id": "duplicate", "domains": False},
        {"id": "duplicate", "domains": ["Machine", None]},
        _VALID,
    ])
    assert len(result["improvement_suggestions"]) == 2
    active = load_active_suggestions()
    assert sorted(s["source_index"] for s in active) == [1, 2]
    assert format_feedback_for_prompt(active, pass_rate=result["summary"]["pass_rate"]) == ""
