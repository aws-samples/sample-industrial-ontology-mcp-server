"""Tests for tools/competency_questions.py — CQ generation, reading, classification."""

import json
from unittest.mock import patch

from domain.tbox_utils import _new_graph
from tools.competency_questions import (
    _classify_cq_type,
    _find_connecting_op,
    _find_connecting_path,
    generate_competency_questions,
    read_competency_questions,
)

_SAMPLE_CQS = [
    {
        "id": "CQ01",
        "question_ko": "어떤 설비가 가장 많은 알람을 발생시키는가?",
        "question_en": "Which equipment has the most alarms?",
        "difficulty": "easy",
        "domains": ["EquipmentMaster", "AlarmEvents"],
        "expected_answer_type": "list",
        "business_value": "정비 우선순위 결정",
    },
    {
        "id": "CQ02",
        "question_ko": "고로 공정에서 열풍온도가 하강하면 어떤 품질 지표에 영향을 주는가?",
        "question_en": "What quality metrics are affected when hot air temperature drops in the blast furnace?",
        "difficulty": "hard",
        "domains": ["ProcessMaster", "QualitySpec"],
        "expected_answer_type": "causal",
        "business_value": "공정 인과 관계 파악",
    },
    {
        "id": "CQ03",
        "question_ko": "설비별 월간 에너지 사용량 합계는 얼마인가?",
        "question_en": "What is the monthly total energy usage per equipment?",
        "difficulty": "medium",
        "domains": ["EquipmentMaster", "EnergyUsage"],
        "expected_answer_type": "count",
        "business_value": "에너지 비용 최적화",
    },
]


class TestClassifyCqType:
    """_classify_cq_type: Wisniewski CQ type classification."""

    def test_count_type_korean(self):
        assert _classify_cq_type("몇 개의 설비가 있는가?") == "count"

    def test_count_type_english(self):
        assert _classify_cq_type("How many alarms occurred?") == "count"

    def test_list_type_korean(self):
        assert _classify_cq_type("어떤 설비가 가동 중인가?") == "list"

    def test_boolean_type_korean(self):
        assert _classify_cq_type("EQ001은 가동 중인가?") == "boolean"

    def test_comparison_type(self):
        assert _classify_cq_type("A공정과 B공정의 에너지 효율 비교") == "comparison"

    def test_aggregation_type(self):
        assert _classify_cq_type("설비별 평균 가동률은?") == "aggregation"

    def test_temporal_type(self):
        assert _classify_cq_type("언제 마지막 정비가 수행되었는가?") == "temporal"

    def test_causal_type(self):
        assert _classify_cq_type("왜 품질 불량이 발생하는가?") == "causal"

    def test_default_to_list(self):
        assert _classify_cq_type("설비 목록 정보") == "list"


class TestFindConnectingOp:
    """_find_connecting_op: ObjectProperty lookup between two classes."""

    def test_exact_match(self):
        obj_props = {
            "hasEquipmentStatus": {
                "domain": "EquipmentMaster",
                "range": "EquipmentStatus",
            }
        }
        assert _find_connecting_op("EquipmentMaster", "EquipmentStatus", obj_props) == "hasEquipmentStatus"

    def test_reverse_direction_match(self):
        obj_props = {
            "hasEquipmentStatus": {
                "domain": "EquipmentMaster",
                "range": "EquipmentStatus",
            }
        }
        assert _find_connecting_op("EquipmentStatus", "EquipmentMaster", obj_props) == "hasEquipmentStatus"

    def test_prefix_match(self):
        obj_props = {
            "hasEquipmentStatus": {
                "domain": "EquipmentMaster",
                "range": "EquipmentStatus",
            }
        }
        # "Equipment" is a prefix of "EquipmentMaster"
        result = _find_connecting_op("equipment", "equipmentstatus", obj_props)
        assert result == "hasEquipmentStatus"

    def test_no_match(self):
        obj_props = {
            "hasEquipmentStatus": {
                "domain": "EquipmentMaster",
                "range": "EquipmentStatus",
            }
        }
        assert _find_connecting_op("ProcessMaster", "QualitySpec", obj_props) is None

    def test_empty_obj_props(self):
        assert _find_connecting_op("A", "B", {}) is None


class TestGenerateCompetencyQuestions:
    """generate_competency_questions: Bedrock-powered CQ generation."""

    @patch("tools.competency_questions._load_tacit_summary", return_value="암묵지 없음")
    @patch("tools.competency_questions._load_csv_summary", return_value="")
    def test_no_csv_returns_error(self, mock_csv, mock_tacit):
        result = json.loads(generate_competency_questions(count=5, auto_approved=True))
        assert result["success"] is False
        assert "CSV" in result["error"]

    @patch("tools.competency_questions.atomic_write")
    @patch("tools.competency_questions.invoke_bedrock_text")
    @patch("tools.competency_questions._load_tacit_summary", return_value="암묵지 없음")
    @patch("tools.competency_questions._load_csv_summary",
           return_value="- EquipmentMaster (100행): equip_id, name")
    def test_happy_path(self, mock_csv, mock_tacit, mock_bedrock, mock_write):
        mock_bedrock.return_value = json.dumps(_SAMPLE_CQS)
        result = json.loads(generate_competency_questions(count=3, auto_approved=True))
        assert result["success"] is True
        assert result["count"] == 3
        assert len(result["questions"]) == 3
        assert "difficulty_distribution" in result

    @patch("tools.competency_questions.atomic_write")
    @patch("tools.competency_questions.invoke_bedrock_text")
    @patch("tools.competency_questions._load_tacit_summary", return_value="암묵지 없음")
    @patch("tools.competency_questions._load_csv_summary",
           return_value="- EquipmentMaster (100행): equip_id")
    def test_json_in_code_block_extracted(self, mock_csv, mock_tacit, mock_bedrock, mock_write):
        mock_bedrock.return_value = "```json\n" + json.dumps(_SAMPLE_CQS) + "\n```"
        result = json.loads(generate_competency_questions(count=3, auto_approved=True))
        assert result["success"] is True
        assert result["count"] == 3

    @patch("tools.competency_questions.atomic_write")
    @patch("tools.competency_questions.invoke_bedrock_text")
    @patch("tools.competency_questions._load_tacit_summary", return_value="암묵지 없음")
    @patch("tools.competency_questions._load_csv_summary",
           return_value="- EquipmentMaster (100행): equip_id")
    def test_defaults_assigned_for_missing_fields(self, mock_csv, mock_tacit, mock_bedrock, mock_write):
        minimal_cqs = [{"question_ko": "테스트 질문", "domains": ["A"]}]
        mock_bedrock.return_value = json.dumps(minimal_cqs)
        result = json.loads(generate_competency_questions(count=1, auto_approved=True))
        assert result["success"] is True
        cq = result["questions"][0]
        assert cq["id"] == "CQ01"
        assert cq["difficulty"] == "medium"
        assert cq["expected_answer_type"] == "list"

    @patch("tools.competency_questions.atomic_write")
    @patch("tools.competency_questions.invoke_bedrock_text")
    @patch("tools.competency_questions._load_tacit_summary", return_value="암묵지 없음")
    @patch("tools.competency_questions._load_csv_summary",
           return_value="- EquipmentMaster (100행): equip_id")
    def test_difficulty_bias_warning(self, mock_csv, mock_tacit, mock_bedrock, mock_write):
        # All easy -> should trigger warning about bias
        biased_cqs = [
            {"id": f"CQ{i:02d}", "question_ko": f"질문{i}", "domains": ["A"],
             "difficulty": "easy"}
            for i in range(1, 6)
        ]
        mock_bedrock.return_value = json.dumps(biased_cqs)
        result = json.loads(generate_competency_questions(count=5, auto_approved=True))
        assert result["success"] is True
        assert "warnings" in result
        assert any("편중" in w or "Hard" in w for w in result["warnings"])

    @patch("tools.competency_questions.invoke_bedrock_text", side_effect=RuntimeError("Bedrock timeout"))
    @patch("tools.competency_questions._load_tacit_summary", return_value="암묵지 없음")
    @patch("tools.competency_questions._load_csv_summary",
           return_value="- EquipmentMaster (100행): equip_id")
    def test_bedrock_error(self, mock_csv, mock_tacit, mock_bedrock):
        result = json.loads(generate_competency_questions(count=3, auto_approved=True))
        assert result["success"] is False
        assert "error" in result


class TestReadCompetencyQuestions:
    """read_competency_questions: load saved CQs from file."""

    @patch("tools.competency_questions._CQ_PATH", "/nonexistent/path/cq.json")
    def test_file_not_found(self):
        result = json.loads(read_competency_questions())
        assert result["success"] is False
        assert "hint" in result

    @patch("tools.competency_questions._CQ_PATH")
    def test_happy_path(self, mock_path, tmp_path):
        cq_file = tmp_path / "competency_questions.json"
        cq_file.write_text(json.dumps(_SAMPLE_CQS), encoding="utf-8")
        mock_path.__str__ = lambda s: str(cq_file)
        with patch("tools.competency_questions._CQ_PATH", str(cq_file)):
            result = json.loads(read_competency_questions())
        assert result["count"] == 3
        assert len(result["questions"]) == 3


class TestBfsConnectivity:
    """_find_connecting_path: BFS-based class connectivity via T-Box ObjectProperties."""

    def test_finds_2hop_connection(self):
        """직접 OP 없는 클래스도 중간 클래스 경유로 연결 감지."""
        from rdflib import OWL, RDF, RDFS, Namespace

        DOMAIN_NS = Namespace("https://w3id.org/steel/ontology/")

        tbox = _new_graph()
        tbox.add((DOMAIN_NS.hasProcess, RDF.type, OWL.ObjectProperty))
        tbox.add((DOMAIN_NS.hasProcess, RDFS.domain, DOMAIN_NS.Equipment))
        tbox.add((DOMAIN_NS.hasProcess, RDFS.range, DOMAIN_NS.Process))
        tbox.add((DOMAIN_NS.hasMaintenanceRecord, RDF.type, OWL.ObjectProperty))
        tbox.add((DOMAIN_NS.hasMaintenanceRecord, RDFS.domain, DOMAIN_NS.Process))
        tbox.add((DOMAIN_NS.hasMaintenanceRecord, RDFS.range, DOMAIN_NS.Maintenance))

        result = _find_connecting_path("Equipment", "Maintenance", tbox, max_hops=3)
        assert result is not None
        assert result["hops"] == 2

    def test_no_connection_returns_none(self):
        """연결 경로 없으면 None 반환."""

        tbox = _new_graph()
        result = _find_connecting_path("A", "B", tbox, max_hops=3)
        assert result is None
