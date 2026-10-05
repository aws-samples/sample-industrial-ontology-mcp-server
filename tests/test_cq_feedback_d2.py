"""Tests for D2 — SPARQL 실행 실패 피드백 루프.

TDD: 테스트 먼저 작성 후 구현.

D2 확장:
  - `_classify_query_error(error, result_count)`: 에러/결과를 5개 카테고리로 분류
    (parse_error / timeout / unresolved_prefix / zero_results / ok)
  - `_build_failed_query_suggestion(cq_id, sparql, error, result_count)`:
    cq_feedback suggestion dict 생성 (type=failed_query)
  - `format_feedback_for_prompt`: "### 쿼리 실행 실패" 섹션 렌더링 지원
"""

from pathlib import Path

from tools.cq_feedback import (
    _build_failed_query_suggestion,
    _classify_query_error,
    format_feedback_for_prompt,
)

# ── _classify_query_error ──────────────────────────────────────────


class TestClassifyQueryError:
    """_classify_query_error: SPARQL 실행 결과를 카테고리로 분류."""

    def test_classify_parse_error(self):
        """ParseException 메시지 → parse_error."""
        err = Exception("ParseException: expected ';'")
        category = _classify_query_error(err)
        assert category == "parse_error"

    def test_classify_timeout(self):
        """TimeoutError → timeout."""
        err = TimeoutError("query timeout")
        category = _classify_query_error(err)
        assert category == "timeout"

    def test_classify_unresolved_prefix(self):
        """'prefix ... not defined' 메시지 → unresolved_prefix."""
        err = Exception("prefix ex: is not defined")
        category = _classify_query_error(err)
        assert category == "unresolved_prefix"

    def test_classify_zero_results(self):
        """error=None, result_count=0 → zero_results."""
        category = _classify_query_error(None, result_count=0)
        assert category == "zero_results"

    def test_classify_ok(self):
        """error=None, result_count=5 → ok."""
        category = _classify_query_error(None, result_count=5)
        assert category == "ok"


# ── _build_failed_query_suggestion ─────────────────────────────────


class TestBuildFailedQuerySuggestion:
    """_build_failed_query_suggestion: cq_feedback suggestion dict 생성."""

    def test_failed_query_suggestion_shape(self):
        """type=failed_query + cq_id + error_category + error_sample + class_hint."""
        sparql = """
            PREFIX steel: <http://example.org/steel#>
            SELECT ?e WHERE { ?e a steel:EquipmentMaster . }
        """
        err = Exception("ParseException: expected ';'")
        sug = _build_failed_query_suggestion(
            cq_id="CQ01", sparql=sparql, error=err, result_count=0
        )
        # Required keys present
        assert sug["type"] == "failed_query"
        assert sug["cq_id"] == "CQ01"
        assert sug["error_category"] == "parse_error"
        assert "error_sample" in sug
        assert "class_hint" in sug
        # class_hint extracted from steel: prefix local name
        assert sug["class_hint"] == "EquipmentMaster"
        # error_sample truncated to <= 200 chars
        assert len(sug["error_sample"]) <= 200


# ── format_feedback_for_prompt ─────────────────────────────────────


class TestFormatFeedbackFailedQuery:
    """format_feedback_for_prompt: failed_query 섹션 렌더링."""

    def test_format_feedback_includes_failed_query_section(self):
        """failed_query suggestion → '### 쿼리 실행 실패' 섹션 포함."""
        suggestions = [
            {
                "type": "failed_query",
                "cq_id": "CQ01",
                "error_category": "parse_error",
                "error_sample": "ParseException: expected ';'",
                "class_hint": "EquipmentMaster",
                "age_iterations": 1,
            }
        ]
        markdown = format_feedback_for_prompt(suggestions, pass_rate=60.0)
        assert "### 쿼리 실행 실패" in markdown
        # Category should surface
        assert "parse_error" in markdown
        # CQ id should surface
        assert "CQ01" in markdown


class TestClassHintUsesConfiguredNSPrefix:
    """class_hint 추출이 하드코딩된 'steel:' 가 아닌 domain.namespaces.NS_PREFIX 사용."""

    def test_extracts_hint_using_ns_prefix(self, monkeypatch):
        """NS_PREFIX='foo' 여도 foo:ClassName 형태를 잡아야 한다."""
        import domain.namespaces as ns_mod
        monkeypatch.setattr(ns_mod, "NS_PREFIX", "foo", raising=False)
        sparql = "SELECT ?e WHERE { ?e a foo:Machine . }"
        sug = _build_failed_query_suggestion(
            cq_id="CQ99", sparql=sparql, error=Exception("x"),
        )
        assert sug["class_hint"] == "Machine"

    def test_class_hint_falls_back_when_no_prefix_match(self):
        """SPARQL 에 NS_PREFIX 토큰이 없으면 class_hint 는 빈 문자열."""
        sug = _build_failed_query_suggestion(
            cq_id="CQ00", sparql="SELECT * WHERE { ?s ?p ?o }", error=None,
            result_count=0,
        )
        assert sug["class_hint"] == ""


class TestQueryTestIntegrationWired:
    """D2 통합: query_test.py 가 _build_failed_query_suggestion 를 실제로 호출하는지."""

    def test_query_test_module_imports_helper(self):
        """tools/query_test.py 소스에 _build_failed_query_suggestion import 가 있어야 한다."""
        import tools.query_test as qt_module

        src = Path(qt_module.__file__).read_text(encoding="utf-8")
        assert "_build_failed_query_suggestion" in src, (
            "query_test.py must invoke _build_failed_query_suggestion "
            "so SPARQL failures flow into cq_feedback"
        )
