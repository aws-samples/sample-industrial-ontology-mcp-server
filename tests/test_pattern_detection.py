"""Tests for tools/pattern_detection.py — failure pattern detection, causal rules."""

import json
from unittest.mock import patch

from tools.pattern_detection import (
    analyze_causal_rules,
    detect_failure_patterns,
)


def _mock_query_factory(results_map: dict):
    """Create a mock _query function that returns results based on query content."""
    def mock_query(g, sparql):
        for key, result in results_map.items():
            if key in sparql:
                return result
        return []
    return mock_query


class TestDetectFailurePatterns:
    """detect_failure_patterns: alarm-based failure detection."""

    @patch("tools.pattern_detection._load_graph")
    @patch("tools.pattern_detection._query")
    def test_no_patterns_defined(self, mock_query, mock_load):
        mock_load.return_value = None
        # First query looks for FailurePattern -> empty
        mock_query.return_value = []
        result = json.loads(detect_failure_patterns())
        assert "FailurePattern" in result["message"]
        assert result["alerts"] == []

    @patch("tools.pattern_detection._load_graph")
    @patch("tools.pattern_detection._query")
    def test_pattern_with_alert(self, mock_query, mock_load):
        mock_load.return_value = None
        patterns = [
            {"pattern": "http://ex.org/Pattern1", "label": "Overtemp",
             "threshold": "3", "window": None, "failureType": "Overheat",
             "description": "Too hot"},
        ]
        alerts_raw = [
            {"equipment": "http://ex.org/EQ001", "pattern": "http://ex.org/Pattern1",
             "failureType": "Overheat", "threshold": "3",
             "description": "Too hot", "alarmCount": "5", "window": None},
        ]
        all_status = [
            {"equipment": "http://ex.org/EQ001", "pattern": "http://ex.org/Pattern1",
             "failureType": "Overheat", "threshold": "3", "alarmCount": "5"},
        ]
        call_count = [0]
        def side_effect(g, sparql):
            call_count[0] += 1
            if call_count[0] == 1:
                return patterns
            if call_count[0] == 2:
                return alerts_raw
            if call_count[0] == 3:
                return []  # orphaned patterns
            return all_status
        mock_query.side_effect = side_effect

        result = json.loads(detect_failure_patterns())
        assert result["patterns_defined"] == 1
        assert result["alerts_triggered"] >= 1
        assert result["alerts"][0]["severity"] == "CRITICAL"

    @patch("tools.pattern_detection._load_graph")
    @patch("tools.pattern_detection._query")
    def test_no_alerts_when_below_threshold(self, mock_query, mock_load):
        mock_load.return_value = None
        patterns = [
            {"pattern": "http://ex.org/Pattern1", "label": "X",
             "threshold": "10", "window": None, "failureType": "Y",
             "description": "desc"},
        ]
        # alarmCount=2 < threshold=10 -> should not trigger
        alerts_raw = [
            {"equipment": "http://ex.org/EQ001", "pattern": "http://ex.org/Pattern1",
             "failureType": "Y", "threshold": "10",
             "description": "desc", "alarmCount": "2", "window": None},
        ]
        all_status = [
            {"equipment": "http://ex.org/EQ001", "pattern": "http://ex.org/Pattern1",
             "failureType": "Y", "threshold": "10", "alarmCount": "2"},
        ]
        call_count = [0]
        def side_effect(g, sparql):
            call_count[0] += 1
            if call_count[0] == 1:
                return patterns
            if call_count[0] == 2:
                return alerts_raw
            if call_count[0] == 3:
                return []
            return all_status
        mock_query.side_effect = side_effect

        result = json.loads(detect_failure_patterns())
        assert result["alerts_triggered"] == 0

    @patch("tools.pattern_detection._load_graph")
    @patch("tools.pattern_detection._query")
    def test_orphaned_patterns_detected(self, mock_query, mock_load):
        mock_load.return_value = None
        patterns = [
            {"pattern": "http://ex.org/Pattern1", "label": "X",
             "threshold": "3", "window": None, "failureType": "Y",
             "description": "desc"},
        ]
        orphaned = [
            {"pattern": "http://ex.org/OrphanedPattern", "label": "Orphan"},
        ]
        call_count = [0]
        def side_effect(g, sparql):
            call_count[0] += 1
            if call_count[0] == 1:
                return patterns
            if call_count[0] == 2:
                return []  # alerts
            if call_count[0] == 3:
                return orphaned
            return []  # all_status
        mock_query.side_effect = side_effect

        result = json.loads(detect_failure_patterns())
        assert result["orphaned_patterns"] == 1

    @patch("tools.pattern_detection._load_graph", side_effect=RuntimeError("Graph load failed"))
    def test_error_handling(self, mock_load):
        result = json.loads(detect_failure_patterns())
        assert result["success"] is False


class TestAnalyzeCausalRules:
    """analyze_causal_rules: operational rule causal chain analysis."""

    @patch("tools.pattern_detection._load_graph")
    @patch("tools.pattern_detection._query")
    def test_no_rules(self, mock_query, mock_load):
        mock_load.return_value = None
        mock_query.return_value = []
        result = json.loads(analyze_causal_rules())
        assert result["rules_total"] == 0
        assert result["hint"] is not None

    @patch("tools.pattern_detection._load_graph")
    @patch("tools.pattern_detection._query")
    def test_rules_found(self, mock_query, mock_load):
        mock_load.return_value = None
        rules = [
            {"rule": "http://ex.org/Rule1", "label": "Temp drop",
             "causeParam": "Hot_Air_Temp_C", "effectParam": "Iron_Quality",
             "causeDir": "decrease", "effectDir": "decrease",
             "description": "열풍온도 하강 영향", "process": "http://ex.org/BF"},
        ]
        mock_query.return_value = rules
        result = json.loads(analyze_causal_rules())
        assert result["rules_total"] == 1
        assert "Hot_Air_Temp_C" in result["rules"][0]["cause"]

    @patch("tools.pattern_detection._load_graph")
    @patch("tools.pattern_detection._query")
    def test_causal_chain_tracking(self, mock_query, mock_load):
        mock_load.return_value = None
        rules = [
            {"rule": "http://ex.org/R1", "label": "R1",
             "causeParam": "Hot_Air_Temp_C", "effectParam": "Iron_Quality",
             "causeDir": "decrease", "effectDir": "decrease",
             "description": "", "process": None},
            {"rule": "http://ex.org/R2", "label": "R2",
             "causeParam": "Iron_Quality", "effectParam": "Product_Grade",
             "causeDir": "decrease", "effectDir": "decrease",
             "description": "", "process": None},
        ]
        mock_query.return_value = rules
        result = json.loads(analyze_causal_rules(
            parameter_name="Hot_Air_Temp_C", direction="decrease"))
        assert result["rules_total"] == 2
        assert result["causal_chains"] is not None
        assert len(result["causal_chains"]) >= 1
        # Should trace Hot_Air_Temp_C -> Iron_Quality -> Product_Grade
        chain_text = " ".join(result["causal_chains"])
        assert "Iron_Quality" in chain_text
        assert "Product_Grade" in chain_text

    @patch("tools.pattern_detection._load_graph", side_effect=RuntimeError("fail"))
    def test_error_handling(self, mock_load):
        result = json.loads(analyze_causal_rules())
        assert result["success"] is False
