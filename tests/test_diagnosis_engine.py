"""Tests for _diagnose_check_failure() — M10 Validation Diagnosis Engine."""

import json
from unittest.mock import patch

import pytest

from domain.tbox_utils import _new_graph
from tools.kg_validation import _diagnose_check_failure

# ── 20개 check name 전수 매핑 테스트 ─────────────────

# 실제 validate_kg()에서 반환하는 20개 check name
CHECK_NAMES = [
    "ObjectProperty 양방향 연결",
    "공정 흐름 체인",
    "고아 노드 탐지",
    "클래스별 인스턴스 수",
    "FK 참조 무결성",
    "추론 sanity check",
    "domain/range 타입 정합성",
    "프로퍼티 사용 커버리지",
    "값 범위 검증",
    "T-Box Fitness",
    "FK-OP Gap 분석",
    "프로퍼티별 완전성",
    "수치 이상치 탐지",
    "시간 정합성",
    "댕글링 참조 탐지",
    "문자열 패턴 검증",
    "Functional Property 위반",
    "Relationship Pattern 이상치",
    "카디널리티 제약 위반",
    "AllDisjointClasses 위반",
]

# 기대 카테고리 매핑
EXPECTED_CATEGORIES = {
    "ObjectProperty 양방향 연결": "schema_gap",
    "공정 흐름 체인": "data_quality",
    "고아 노드 탐지": "data_quality",
    "클래스별 인스턴스 수": "data_quality",
    "FK 참조 무결성": "fk_broken",
    "추론 sanity check": "inference_noise",
    "domain/range 타입 정합성": "schema_gap",
    "프로퍼티 사용 커버리지": "data_quality",
    "값 범위 검증": "data_quality",
    "T-Box Fitness": "schema_gap",
    "FK-OP Gap 분석": "fk_broken",
    "프로퍼티별 완전성": "data_quality",
    "수치 이상치 탐지": "data_quality",
    "시간 정합성": "data_quality",
    "댕글링 참조 탐지": "fk_broken",
    "문자열 패턴 검증": "data_quality",
    "Functional Property 위반": "data_quality",
    "Relationship Pattern 이상치": "data_quality",
    "카디널리티 제약 위반": "schema_gap",
    "AllDisjointClasses 위반": "inference_noise",
}

EXPECTED_AUTO_FIXABLE = {
    "ObjectProperty 양방향 연결": True,
    "domain/range 타입 정합성": True,
}


class TestDiagnoseCheckFailure:
    """_diagnose_check_failure 단위 테스트."""

    @pytest.mark.parametrize("check_name", CHECK_NAMES)
    def test_every_check_name_maps_to_nonempty_suggestion(self, check_name):
        """모든 check name이 비어있지 않은 fix_suggestion을 반환한다."""
        check = {"name": check_name, "passed": False}
        result = _diagnose_check_failure(check)
        assert result["fix_suggestion"], f"'{check_name}' has empty fix_suggestion"
        assert result["category"] != "unknown", f"'{check_name}' mapped to 'unknown'"

    @pytest.mark.parametrize("check_name", CHECK_NAMES)
    def test_every_check_name_has_expected_category(self, check_name):
        """모든 check name이 기대 카테고리에 매핑된다."""
        check = {"name": check_name, "passed": False}
        result = _diagnose_check_failure(check)
        expected = EXPECTED_CATEGORIES[check_name]
        assert result["category"] == expected, (
            f"'{check_name}': expected category '{expected}', got '{result['category']}'"
        )

    @pytest.mark.parametrize("check_name", CHECK_NAMES)
    def test_auto_fixable_flag(self, check_name):
        """auto_fixable 플래그가 기대값과 일치한다."""
        check = {"name": check_name, "passed": False}
        result = _diagnose_check_failure(check)
        expected = EXPECTED_AUTO_FIXABLE.get(check_name, False)
        assert result["auto_fixable"] == expected, (
            f"'{check_name}': expected auto_fixable={expected}, got {result['auto_fixable']}"
        )

    def test_return_structure(self):
        """반환 딕셔너리에 필수 키 3개가 존재한다."""
        check = {"name": "고아 노드 탐지", "passed": False}
        result = _diagnose_check_failure(check)
        assert "category" in result
        assert "fix_suggestion" in result
        assert "auto_fixable" in result

    def test_unknown_name_fallback(self):
        """미지의 check name이면 data_quality + name 포함 제안."""
        check = {"name": "알 수 없는 검증", "passed": False}
        result = _diagnose_check_failure(check)
        assert result["category"] == "data_quality"
        assert "알 수 없는 검증" in result["fix_suggestion"]
        assert result["auto_fixable"] is False

    def test_empty_name_fallback(self):
        """name 키가 빈 문자열이면 fallback 동작."""
        check = {"name": "", "passed": False}
        result = _diagnose_check_failure(check)
        assert result["category"] == "data_quality"
        assert result["fix_suggestion"] != ""

    def test_missing_name_key(self):
        """name 키 자체가 없어도 동작."""
        check = {"passed": False}
        result = _diagnose_check_failure(check)
        assert result["category"] == "data_quality"


class TestDiagnoseWiredIntoValidateKg:
    """validate_kg() 결과에 diagnoses 필드 연결 테스트."""

    @patch("tools.kg_validation.INFERRED_PATH", "/nonexistent/path.ttl")
    @patch("tools.kg_validation.TBOX_PATH", "/nonexistent/tbox.ttl")
    @patch("tools.kg_validation._load_graph")
    @patch("os.path.exists")
    def test_no_diagnoses_when_all_pass(self, mock_exists, mock_load_graph):
        """모든 check 통과 시 diagnoses 키 없음."""
        from tools.kg_validation import validate_kg

        mock_exists.return_value = False
        mock_load_graph.return_value = _new_graph()

        result = json.loads(validate_kg(use_inferred=False))
        # 빈 그래프라도 일부 check는 자동 통과하므로 all-pass가 아닐 수 있음
        # diagnoses가 있으면 실패한 check가 있는 것이므로 구조만 검증
        if result["passed"]:
            assert "diagnoses" not in result
        else:
            assert "diagnoses" in result
            assert len(result["diagnoses"]) > 0

    @patch("tools.kg_validation.INFERRED_PATH", "/nonexistent/path.ttl")
    @patch("tools.kg_validation.TBOX_PATH", "/nonexistent/tbox.ttl")
    @patch("tools.kg_validation._load_graph")
    @patch("os.path.exists")
    def test_diagnoses_structure(self, mock_exists, mock_load_graph):
        """실패한 check에 대한 diagnoses 항목 구조 검증."""
        from tools.kg_validation import validate_kg

        mock_exists.return_value = False
        mock_load_graph.return_value = _new_graph()

        result = json.loads(validate_kg(use_inferred=False))
        if "diagnoses" in result:
            for d in result["diagnoses"]:
                assert "check_name" in d
                assert "category" in d
                assert "fix_suggestion" in d
                assert "auto_fixable" in d
                assert d["fix_suggestion"] != ""
            assert "auto_fixable_count" in result
            assert isinstance(result["auto_fixable_count"], int)

    @patch("tools.kg_validation.INFERRED_PATH", "/nonexistent/path.ttl")
    @patch("tools.kg_validation.TBOX_PATH", "/nonexistent/tbox.ttl")
    @patch("tools.kg_validation._load_graph")
    @patch("os.path.exists")
    def test_diagnoses_count_matches_failures(self, mock_exists, mock_load_graph):
        """diagnoses 수가 실패한 check 수와 일치."""
        from tools.kg_validation import validate_kg

        mock_exists.return_value = False
        mock_load_graph.return_value = _new_graph()

        result = json.loads(validate_kg(use_inferred=False))
        if "diagnoses" in result:
            failed_checks = [c for c in result["checks"] if not c["passed"]]
            assert len(result["diagnoses"]) == len(failed_checks)

    @patch("tools.kg_validation.INFERRED_PATH", "/nonexistent/path.ttl")
    @patch("tools.kg_validation.TBOX_PATH", "/nonexistent/tbox.ttl")
    @patch("tools.kg_validation._load_graph")
    @patch("os.path.exists")
    def test_auto_fixable_count_accuracy(self, mock_exists, mock_load_graph):
        """auto_fixable_count가 실제 auto_fixable=True 수와 일치."""
        from tools.kg_validation import validate_kg

        mock_exists.return_value = False
        mock_load_graph.return_value = _new_graph()

        result = json.loads(validate_kg(use_inferred=False))
        if "diagnoses" in result:
            actual_auto = sum(1 for d in result["diagnoses"] if d["auto_fixable"])
            assert result["auto_fixable_count"] == actual_auto
