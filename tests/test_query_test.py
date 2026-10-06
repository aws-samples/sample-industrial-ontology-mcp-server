"""Tests for tools/query_test.py — CQ-based KG connectivity test."""

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from tools.query_test import (
    _cq_to_test_case,
    _domain_to_class_name,
    _generate_cq_report_html,
)
from tools.query_test import (
    test_domain_queries as _tool_test_domain_queries,
)
from tools.query_test import (
    validate_cq_tbox_only as _tool_validate_cqs,
)


@pytest.mark.parametrize(
    "domain,expected",
    [
        ("ghg_emission", "GhgEmission"),
        ("GHG_Emission", "GHGEmission"),
        ("NDT_Results", "NDTResults"),
        ("ndt_results", "NdtResults"),
        ("Process_Rolling", "ProcessRolling"),
        ("process_blast_furnace", "ProcessBlastFurnace"),
        ("EquipmentMaster", "EquipmentMaster"),
        ("CSV_Profile", "CSVProfile"),
        ("", ""),
    ],
)
def test_domain_to_class_name_preserves_acronyms(domain, expected):
    """Acronym preservation guard — GHG/NDT must not be lowercased to Ghg/Ndt.

    Regression for the camelCase normalization bug discovered in S12 of
    FULL_PIPELINE 2026-05-30: str.capitalize() collapses GHG→Ghg, breaking
    the abox_stats lookup since T-Box keeps the original CSV header casing.
    """
    assert _domain_to_class_name(domain) == expected


@pytest.fixture(autouse=True)
def _isolate_cq_feedback(tmp_path_factory, monkeypatch):
    """T3: Redirect cq_feedback.json to a per-session tmp path so test runs
    of test_domain_queries don't pollute data/generated/reports/cq_feedback.json.
    """
    tmp_dir = tmp_path_factory.mktemp("cq_feedback_isolation")
    fake_path = str(tmp_dir / "cq_feedback.json")
    monkeypatch.setattr("tools.cq_feedback.CQ_FEEDBACK_PATH", fake_path)


_SAMPLE_CQS = [
    {
        "id": "CQ01",
        "question_ko": "어떤 설비가 가장 많은 알람을 발생시키는가?",
        "difficulty": "easy",
        "domains": ["EquipmentMaster", "AlarmEvents"],
        "expected_answer_type": "list",
    },
    {
        "id": "CQ02",
        "question_ko": "설비별 월간 에너지 사용량은?",
        "difficulty": "medium",
        "domains": ["EquipmentMaster", "EnergyUsage"],
        "expected_answer_type": "count",
    },
]

_SAMPLE_SEM_DICT = {
    "classes": {
        "EquipmentMaster": {
            "datatype_properties": [{"name": "equipmentID"}, {"name": "equipmentName"}],
        },
        "AlarmEvents": {
            "datatype_properties": [{"name": "alarmCode"}],
        },
        "EnergyUsage": {
            "datatype_properties": [{"name": "usageKwh"}],
        },
    },
    "object_properties": {
        "hasAlarm": {"domain": "EquipmentMaster", "range": "AlarmEvents"},
        "hasEnergyUsage": {"domain": "EquipmentMaster", "range": "EnergyUsage"},
    },
}

_SAMPLE_ABOX_STATS = {
    "per_class": {
        "EquipmentMaster": {"instance_count": 50, "triples": 200},
        "AlarmEvents": {"instance_count": 120, "triples": 480},
        "EnergyUsage": {"instance_count": 80, "triples": 320},
    }
}


class TestCqToTestCase:
    """_cq_to_test_case: convert CQ to test case."""

    def test_basic_conversion(self):
        cq = _SAMPLE_CQS[0]
        tc = _cq_to_test_case(cq)
        assert tc["id"] == "cq_cq01"
        assert tc["difficulty"] == "easy"
        assert tc["domains"] == ["EquipmentMaster", "AlarmEvents"]
        assert tc["assertions"]["min_rows"] == 1

    def test_count_type_has_required_vars(self):
        cq = _SAMPLE_CQS[1]
        tc = _cq_to_test_case(cq)
        assert "cnt" in tc["assertions"]["required_vars"]
        assert "count" in tc["assertions"]["required_vars"]

    def test_category_from_domains(self):
        cq = {"id": "CQ99", "domains": ["A", "B", "C", "D"], "difficulty": "hard"}
        tc = _cq_to_test_case(cq)
        # Only first 3 domains joined
        assert tc["category"] == "A+B+C"

    def test_must_not_contain(self):
        tc = _cq_to_test_case(_SAMPLE_CQS[0])
        assert "DELETE" in tc["assertions"]["sparql_must_not_contain"]
        assert "INSERT" in tc["assertions"]["sparql_must_not_contain"]


class TestTestDomainQueries:
    """test_domain_queries: CQ connectivity verification."""

    @patch("tools.query_test._CQ_PATH", "/nonexistent/cq.json")
    def test_missing_cq_file(self):
        result = json.loads(_tool_test_domain_queries(open_report=False, verify_joins=False))
        assert result["success"] is False
        assert "hint" in result

    @patch("tools.query_test.webbrowser.open")
    @patch("tools.query_test.os.makedirs")
    @patch("builtins.open")
    @patch("tools.query_test._load_abox_stats", return_value=_SAMPLE_ABOX_STATS)
    @patch("tools.query_test._load_semantic_dict", return_value=_SAMPLE_SEM_DICT)
    @patch("tools.query_test.os.path.exists")
    def test_happy_path_with_abox_stats(self, mock_exists, mock_dict, mock_stats,
                                        mock_builtin_open, mock_makedirs, mock_browser,
                                        tmp_path):
        import io
        cq_json = json.dumps(_SAMPLE_CQS)
        mock_exists.side_effect = lambda p: True
        # builtins.open mock: return CQ file for reading, and a writable for report
        cq_reader = io.StringIO(cq_json)
        cq_reader.name = "cq.json"
        report_writer = io.StringIO()
        report_writer.name = "report.html"
        call_count = [0]
        def open_side_effect(path, *args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return cq_reader
            # Report write
            return MagicMock(__enter__=MagicMock(return_value=report_writer),
                            __exit__=MagicMock(return_value=False))
        mock_builtin_open.side_effect = open_side_effect

        with patch("config.GENERATED_DIR", str(tmp_path)):
            result = json.loads(
                _tool_test_domain_queries(open_report=False, verify_joins=False)
            )
        assert "summary" in result
        assert result["summary"]["total"] == 2
        assert result["summary"]["bedrock_calls"] == 0
        assert result["summary"]["passed"] + result["summary"]["failed"] == 2

    @patch("tools.query_test.webbrowser.open")
    @patch("tools.query_test._load_abox_stats", return_value=_SAMPLE_ABOX_STATS)
    @patch("tools.query_test._load_semantic_dict", return_value=_SAMPLE_SEM_DICT)
    def test_all_pass_when_stats_and_ops_present(self, mock_dict, mock_stats, mock_browser, tmp_path):
        # Write CQ file
        cq_file = tmp_path / "competency_questions.json"
        cq_file.write_text(json.dumps(_SAMPLE_CQS))
        with patch("tools.query_test._CQ_PATH", str(cq_file)), \
             patch("config.GENERATED_DIR", str(tmp_path)):
            result = json.loads(_tool_test_domain_queries(open_report=False, verify_joins=False))
        assert result["summary"]["passed"] == 2
        assert result["summary"]["pass_rate"] == 100.0

    @patch("tools.query_test.webbrowser.open")
    @patch("tools.query_test._load_abox_stats", return_value={"per_class": {}})
    @patch("tools.query_test._load_semantic_dict", return_value=_SAMPLE_SEM_DICT)
    def test_fail_when_no_instances(self, mock_dict, mock_stats, mock_browser, tmp_path):
        # abox_stats has empty per_class -> zero instance counts
        cq_file = tmp_path / "competency_questions.json"
        cq_file.write_text(json.dumps(_SAMPLE_CQS))
        with patch("tools.query_test._CQ_PATH", str(cq_file)), \
             patch("config.GENERATED_DIR", str(tmp_path)):
            result = json.loads(_tool_test_domain_queries(open_report=False, verify_joins=False))
        assert result["summary"]["failed"] == 2


class TestVerifyClassJoin:
    """R11-M6: 3-단계 fallback JOIN 검증 — OP 방향 불일치에도 두 클래스가
    실제로 연결되는지 판정."""

    def _mini_graph(self):
        """2 클래스 + OP 하나 (역방향으로만 존재) 로 구성된 테스트 그래프."""
        from rdflib import Graph
        g = Graph()
        # T-Box: class_a, class_b, hasB (a→b 방향)
        # A-Box: 역방향 사용 — b_1 hasB a_1 (즉 b_1 이 subject, a_1 이 object).
        # 기존 JOIN (?x a A ; :hasB ?y . ?y a B) 는 0건, 역방향 시도하면 1건.
        g.parse(data="""
@prefix steel: <http://example.org/#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .

steel:a_1 a steel:ClassA .
steel:b_1 a steel:ClassB .
steel:b_1 steel:hasConnection steel:a_1 .
""", format="turtle")
        return g

    def test_verify_join_finds_reverse_direction(self):
        """원 방향 실패 시 역방향을 시도해 매치를 찾는다."""
        from tools.query_test import _verify_class_join

        g = self._mini_graph()
        result = _verify_class_join(g, "ClassA", "hasConnection", "ClassB",
                                     ns_prefix="steel",
                                     ns_uri="http://example.org/#")
        assert result["passed"] is True
        # 방향 정보 기록 (debug 용)
        assert result["direction"] in ("reverse", "any_path")
        assert result["count"] >= 1

    def test_verify_join_finds_any_path_when_op_name_wrong(self):
        """정확한 OP 이름이 틀렸어도 두 클래스가 어떤 OP 로든 연결되면 PASS."""
        from tools.query_test import _verify_class_join
        g = self._mini_graph()
        # CQ 가 존재하지 않는 OP 이름 (`wrongOpName`) 을 참조
        result = _verify_class_join(g, "ClassA", "wrongOpName", "ClassB",
                                     ns_prefix="steel",
                                     ns_uri="http://example.org/#")
        assert result["passed"] is True
        assert result["direction"] == "any_path"

    def test_verify_join_fails_when_no_connection(self):
        """두 클래스 인스턴스 간 어떤 OP 도 없으면 FAIL."""
        from rdflib import Graph

        from tools.query_test import _verify_class_join
        g = Graph()
        g.parse(data="""
@prefix steel: <http://example.org/#> .
steel:a_1 a steel:ClassA .
steel:b_1 a steel:ClassB .
""", format="turtle")
        result = _verify_class_join(g, "ClassA", "hasConnection", "ClassB",
                                     ns_prefix="steel",
                                     ns_uri="http://example.org/#")
        assert result["passed"] is False
        assert result["count"] == 0

    def test_verify_join_finds_2hop_indirect_path(self):
        """직접 연결은 없지만 중간 클래스를 경유해 연결되면 2-hop fallback 으로 PASS.

        시나리오: AlarmEvents → TagMaster → EquipmentStatus (TagMaster 경유).
        실제 철강 데이터에서 관찰된 FK 패턴.
        """
        from rdflib import Graph

        from tools.query_test import _verify_class_join
        g = Graph()
        g.parse(data="""
@prefix steel: <http://example.org/#> .
steel:alarm_1 a steel:AlarmEvents .
steel:tag_1 a steel:TagMaster .
steel:status_1 a steel:EquipmentStatus .

# AlarmEvents → TagMaster
steel:alarm_1 steel:forTag steel:tag_1 .
# TagMaster → EquipmentStatus (역방향도 OK)
steel:status_1 steel:hasTag steel:tag_1 .
""", format="turtle")
        # 직접 AlarmEvents ↔ EquipmentStatus 연결 없음, 하지만 TagMaster 경유
        result = _verify_class_join(g, "AlarmEvents", "nonexistentOp", "EquipmentStatus",
                                     ns_prefix="steel",
                                     ns_uri="http://example.org/#",
                                     max_hops=2)
        assert result["passed"] is True
        assert result["direction"] == "2_hop"
        assert result["count"] >= 1


class TestGenerateCqReportHtml:
    """_generate_cq_report_html: HTML output structure."""

    def test_html_contains_pass_fail(self):
        results = [
            {"id": "CQ01", "status": "PASS", "question": "Test Q",
             "difficulty": "easy", "domains": ["A"],
             "checks": [{"check": "check1", "passed": True, "detail": "ok"}],
             "checks_passed": 1, "checks_total": 1, "failures": []},
        ]
        summary = {"total": 1, "passed": 1, "failed": 0, "pass_rate": 100.0,
                    "duration_seconds": 0.1}
        html = _generate_cq_report_html(results, summary)
        assert "PASS" in html
        assert "CQ01" in html
        assert "100.0%" in html

    def test_html_shows_failures(self):
        results = [
            {"id": "CQ02", "status": "FAIL", "question": "Fail Q",
             "difficulty": "hard", "domains": ["X"],
             "checks": [{"check": "check1", "passed": False, "detail": "missing"}],
             "checks_passed": 0, "checks_total": 1,
             "failures": ["check1 -- missing"]},
        ]
        summary = {"total": 1, "passed": 0, "failed": 1, "pass_rate": 0.0,
                    "duration_seconds": 0.5}
        html = _generate_cq_report_html(results, summary)
        assert "FAIL" in html
        assert "0.0%" in html


class TestImprovementSuggestions:
    """test_domain_queries improvement_suggestions for failed CQs."""

    @patch("tools.query_test.webbrowser.open")
    @patch("tools.query_test._load_abox_stats", return_value={"per_class": {}})
    @patch("tools.query_test._load_semantic_dict", return_value=_SAMPLE_SEM_DICT)
    def test_suggests_missing_instances(self, mock_dict, mock_stats, mock_browser, tmp_path):
        cq_file = tmp_path / "competency_questions.json"
        cq_file.write_text(json.dumps(_SAMPLE_CQS))
        with patch("tools.query_test._CQ_PATH", str(cq_file)), \
             patch("config.GENERATED_DIR", str(tmp_path)):
            result = json.loads(_tool_test_domain_queries(open_report=False, verify_joins=False))
        suggestions = result.get("improvement_suggestions", [])
        assert len(suggestions) > 0
        types = {s["type"] for s in suggestions}
        assert "missing_instances" in types

    @patch("tools.query_test.webbrowser.open")
    @patch("tools.query_test._load_abox_stats", return_value=_SAMPLE_ABOX_STATS)
    @patch("tools.query_test._load_semantic_dict", return_value={"classes": {}, "object_properties": {}})
    def test_suggests_missing_connections(self, mock_dict, mock_stats, mock_browser, tmp_path):
        cq_file = tmp_path / "competency_questions.json"
        cq_file.write_text(json.dumps(_SAMPLE_CQS))
        with patch("tools.query_test._CQ_PATH", str(cq_file)), \
             patch("config.GENERATED_DIR", str(tmp_path)):
            result = json.loads(_tool_test_domain_queries(open_report=False, verify_joins=False))
        suggestions = result.get("improvement_suggestions", [])
        conn_suggestions = [s for s in suggestions if s["type"] == "missing_connection"]
        assert len(conn_suggestions) > 0

    @patch("tools.query_test.webbrowser.open")
    @patch("tools.query_test._load_abox_stats", return_value=_SAMPLE_ABOX_STATS)
    @patch("tools.query_test._load_semantic_dict", return_value=_SAMPLE_SEM_DICT)
    def test_no_suggestions_when_all_pass(self, mock_dict, mock_stats, mock_browser, tmp_path):
        cq_file = tmp_path / "competency_questions.json"
        cq_file.write_text(json.dumps(_SAMPLE_CQS))
        with patch("tools.query_test._CQ_PATH", str(cq_file)), \
             patch("config.GENERATED_DIR", str(tmp_path)):
            result = json.loads(_tool_test_domain_queries(open_report=False, verify_joins=False))
        suggestions = result.get("improvement_suggestions", [])
        assert len(suggestions) == 0


class TestValidateCompetencyQuestions:
    """validate_cq_tbox_only: T-Box-only CQ answerability check."""

    @patch("tools.query_test._CQ_PATH", "/nonexistent/cq.json")
    def test_missing_cq_file(self):
        result = json.loads(_tool_validate_cqs())
        assert result["success"] is False

    @patch("tools.query_test.os.path.exists")
    def test_missing_tbox_file(self, mock_exists):
        mock_exists.side_effect = lambda p: "competency" in p
        with patch("builtins.open", MagicMock(return_value=MagicMock(
            __enter__=MagicMock(return_value=MagicMock(
                read=MagicMock(return_value=json.dumps(_SAMPLE_CQS))
            )),
            __exit__=MagicMock(return_value=False)
        ))):
            result = json.loads(_tool_validate_cqs())
        assert result["success"] is False


# ── 선언 축 vs 데이터 축 (2026-08-30) ─────────────────────────────────────
#
# 5b 연결성 체크는 domain/range 만 보고 is_populated 를 읽지 않는다. 그래서 A-Box
# 0행 OP 를 하나 선언하면 CQ 통과가 뒤집힌다. 반대실험 실측:
#
#     현 딕셔너리(121 OP)                      8/12 = 66.7%
#     + 0행 OP blastFurnaceProcessUsesTag      9/12 = 75.0%   ← 데이터 증가 0
#
# 배포 이력에서 실제로 일어났다 — 8/30 baseline 의 CQ11 은 planFulfilledByResult(0행)
# → inventoryTransactionRelatedProductionPlan(0행) 즉 **0행 × 0행 멀티홉**으로 통과했다.
#
# 통과율을 깎는 대신(그러면 66.7% → 58.3%) **축을 나눠 보고**한다: 통과 판정은 그대로
# 두고 schema_only_connections 로 "이 통과가 유령에 기대는가" 를 드러낸다.


def test_op_is_live_treats_missing_stats_as_live():
    """THE TRAP: 키 부재는 "판정 불가" 이고 산 것으로 취급해야 한다.

    ``.get("is_populated")`` 를 truthy 로 보면 S6.5 v1 딕셔너리(통계 없음)와 테스트
    픽스처에서 **전 OP 가 죽은 것으로** 판정돼 파이프라인이 통째로 실패한다.
    """
    from tools.query_test import _op_is_live

    assert _op_is_live({}) is True, "통계 없는 v1 딕셔너리에서 전 OP 를 죽였다"
    assert _op_is_live(None) is True
    assert _op_is_live({"triple_count": 0}) is True, (
        "is_populated 키가 없으면 판정 불가 — 죽었다고 단정하면 안 된다"
    )


def test_op_is_live_detects_explicit_zero_row():
    """``is_populated: False`` 는 죽은 것으로 판정한다."""
    from tools.query_test import _op_is_live

    assert _op_is_live({"is_populated": False, "triple_count": 0}) is False
    assert _op_is_live({"is_populated": True, "triple_count": 6000}) is True


def test_deployed_run_reports_schema_only_connections(tmp_path, monkeypatch):
    """실측 고정: 배포 산출물로 돌리면 유령 의존이 드러난다.

    단위 픽스처가 통과해도 실제 딕셔너리에서 무발화일 수 있다 (이 리포의 "산출물로
    확인하라"). 현 딕셔너리에는 ``is_populated=False`` OP 가 13개 있고 그중 일부가
    쌍 판정을 떠받친다.

    배포 딕셔너리를 **읽는** 것이 이 테스트의 목적이지만, ``test_domain_queries`` 는
    부수효과로 배포 ``reports/query_test_report.html`` 을 쓴다. 읽기는 유지하고
    쓰기만 tmp 로 돌린다 — 이 테스트가 주장하는 것은 반환 데이터이지 보고서 파일이
    아니다.
    """
    import json

    from config import SEMANTIC_DICT_PATH
    from tools.query_test import test_domain_queries

    if not os.path.exists(SEMANTIC_DICT_PATH):
        pytest.skip("배포 딕셔너리 없음")
    with open(SEMANTIC_DICT_PATH, encoding="utf-8") as handle:
        ops = json.load(handle).get("object_properties") or {}
    if not any(v.get("is_populated") is False for v in ops.values()):
        pytest.skip("0행 OP 가 없어 이 축이 발동하지 않는다")

    # 보고서는 ``resolve_generated_path`` 가 호출 시점의 ``config.GENERATED_DIR`` 로
    # 해석한다. 딕셔너리 경로는 import 시점 상수라 이 patch 와 무관하게 배포본을 읽는다.
    monkeypatch.setattr("config.GENERATED_DIR", str(tmp_path))
    data = json.loads(test_domain_queries(verify_joins=False, open_report=False))
    summary = data["summary"]
    assert data["report_path"].startswith(str(tmp_path)), data["report_path"]
    assert "schema_only_connections" in summary, (
        "선언 축 카운터가 없다 — 통과율 상승이 데이터인지 선언인지 구분 불가"
    )
    assert summary["schema_only_connections"] > 0, (
        f"0행 OP 가 {sum(1 for v in ops.values() if v.get('is_populated') is False)}개 "
        f"있는데 유령 의존이 0으로 보고됐다"
    )
    assert "cqs_with_schema_only_pass" in summary
    # per-CQ 사영에도 실려야 한다 — summary 총계만으로는 어느 CQ 인지 모른다.
    flagged = [r for r in data["results"] if r.get("schema_only_connections")]
    assert flagged, "per-CQ 사영에서 필드가 누락됐다"


def test_pass_rate_is_not_lowered_by_the_new_axis(tmp_path, monkeypatch):
    """PRESERVATION: 축 분리가 통과 판정을 바꾸지 않는다.

    is_populated 필터를 **통과 조건**에 넣으면 66.7% → 58.3% 로 떨어진다. 그것은
    별개 결정이며 이 커밋의 범위가 아니다 — 여기서는 가시성만 확보한다.
    """
    import json

    from config import SEMANTIC_DICT_PATH
    from tools.query_test import test_domain_queries

    if not os.path.exists(SEMANTIC_DICT_PATH):
        pytest.skip("배포 딕셔너리 없음")
    # 보고서는 ``resolve_generated_path`` 가 호출 시점의 ``config.GENERATED_DIR`` 로
    # 해석한다. 딕셔너리 경로는 import 시점 상수라 이 patch 와 무관하게 배포본을 읽는다.
    monkeypatch.setattr("config.GENERATED_DIR", str(tmp_path))
    data = json.loads(test_domain_queries(verify_joins=False, open_report=False))
    summary = data["summary"]
    assert data["report_path"].startswith(str(tmp_path)), data["report_path"]
    # 유령에 기대 통과한 CQ 가 있어도 그 CQ 는 여전히 PASS 다.
    ghost_pass = [
        r for r in data["results"]
        if r.get("schema_only_connections") and r["status"] == "PASS"
    ]
    if ghost_pass:
        assert summary["cqs_with_schema_only_pass"] == len(ghost_pass)


def test_join_verification_failures_carry_feedback_keys():
    """THE REGRESSION: 5e 실패가 cq_feedback 제안을 남긴다.

    5e 는 ``_sparql_result_count``/``_sparql_error`` 를 심지 않아 제안 분기가
    ``None == 0`` → False 로 미스했다. 실측: 배포 cq_feedback.json 5 iteration 의
    제안 40건이 전부 ``missing_connection``, ``failed_query`` 0건. 8/29
    iteration(failed=3)에 CQ11 제안이 없는데 그 CQ 의 유일한 실패가 5e 조인 0건이었다.
    """
    import inspect

    from tools import query_test

    src = inspect.getsource(query_test.test_domain_queries)
    # 5e 의 두 append 지점이 피드백 키를 심는지 소스로 확인 (그 경로는 대용량 그래프
    # 로드를 요구해 단위 테스트로 재현하기 어렵다).
    assert src.count("_join_verification") >= 3, (
        "5e 가 피드백 키를 심지 않는다 — 실패가 영구 비가시가 된다"
    )
    assert "empty_connection" in src, (
        "5e 실패가 missing_connection 과 같은 타입으로 합쳐졌다 — 지문 병합으로 "
        "age_iterations 가 부푼다"
    )
    assert "unclassified_failure" in src, (
        "분류되지 않은 실패의 폴백이 없다 — 새 체크 종류가 조용히 소실된다"
    )
