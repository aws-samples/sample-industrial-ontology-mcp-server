"""Tests for quality history tracking (M12) in tools/kg_validation.py."""

import json
import os
from unittest.mock import patch

import pytest

from domain.tbox_utils import _new_graph

# ── _parse_score ─────────────────────────────────


class TestParseScore:
    """점수 문자열 파싱."""

    def test_normal(self):
        from tools.kg_validation import _parse_score
        assert _parse_score("18/20") == 18

    def test_full(self):
        from tools.kg_validation import _parse_score
        assert _parse_score("20/20") == 20

    def test_zero(self):
        from tools.kg_validation import _parse_score
        assert _parse_score("0/20") == 0

    def test_invalid_string(self):
        from tools.kg_validation import _parse_score
        assert _parse_score("abc") == 0

    def test_empty_string(self):
        from tools.kg_validation import _parse_score
        assert _parse_score("") == 0

    def test_no_slash(self):
        from tools.kg_validation import _parse_score
        assert _parse_score("18") == 18


# ── _detect_regression ───────────────────────────


class TestDetectRegression:
    """직전 실행 대비 regression 감지."""

    def test_no_regression_when_improved(self):
        from tools.kg_validation import _detect_regression
        history = [
            {"score": "15/20", "failed_checks": ["A", "B"]},
            {"score": "18/20", "failed_checks": ["A"]},
        ]
        assert _detect_regression(history) is None

    def test_no_regression_when_same(self):
        from tools.kg_validation import _detect_regression
        history = [
            {"score": "18/20", "failed_checks": ["A"]},
            {"score": "18/20", "failed_checks": ["A"]},
        ]
        assert _detect_regression(history) is None

    def test_regression_detected(self):
        from tools.kg_validation import _detect_regression
        history = [
            {"score": "18/20", "failed_checks": ["A"]},
            {"score": "15/20", "failed_checks": ["A", "B", "C"]},
        ]
        result = _detect_regression(history)
        assert result is not None
        assert result["regression"] is True
        assert result["delta"] == -3
        assert result["current_score"] == "15/20"
        assert result["previous_score"] == "18/20"
        assert "B" in result["new_failures"]
        assert "C" in result["new_failures"]

    def test_denominator_change_alone_is_not_a_regression(self):
        """check 를 추가해 분모가 늘어도 실패 수가 같으면 회귀가 아니다.

        실측 (2026-08-24): 23번째 check(스키마 참조 무결성) 추가로 ``21/22`` →
        ``22/23`` 이 됐다. 통과 수만 비교하면 새 check 가 새 결함을 드러낸 것과
        기존 검증이 나빠진 것을 구별할 수 없다 — 분모가 다르면 **실패 수** 로 본다.
        """
        from tools.kg_validation import _detect_regression
        history = [
            {"score": "21/22", "failed_checks": ["X"]},
            {"score": "22/23", "failed_checks": ["X"]},
        ]
        assert _detect_regression(history) is None

    def test_denominator_change_with_more_failures_is_a_regression(self):
        """분모가 늘고 **실패도 늘면** 회귀다 (과잉 관용 방지)."""
        from tools.kg_validation import _detect_regression
        history = [
            {"score": "21/22", "failed_checks": ["X"]},
            {"score": "21/23", "failed_checks": ["X", "Y"]},
        ]
        result = _detect_regression(history)
        assert result is not None
        assert "Y" in result["new_failures"]

    def test_denominator_change_with_fewer_failures_is_not_a_regression(self):
        from tools.kg_validation import _detect_regression
        history = [
            {"score": "20/22", "failed_checks": ["X", "Y"]},
            {"score": "22/23", "failed_checks": ["X"]},
        ]
        assert _detect_regression(history) is None

    def test_single_entry_no_regression(self):
        from tools.kg_validation import _detect_regression
        history = [{"score": "18/20", "failed_checks": []}]
        assert _detect_regression(history) is None

    def test_empty_history(self):
        from tools.kg_validation import _detect_regression
        assert _detect_regression([]) is None


# ── 두 스키마가 섞인 파일 (2026-08-30) ────────────────────────────────────
#
# ``quality_history.json`` 을 두 writer 가 공유한다:
#
#   append_quality_history (검증)          {"score": "19/25", "failed_checks": [...]}
#   pipeline_state._append_quality_history {"step": "S9_...", "metrics": {...}}
#
# ``detect_regression`` 이 ``history[-1]`` vs ``history[-2]`` 를 **위치** 로 비교하며
# ``.get("score", "0/0")`` 으로 폴백했으므로, 사이에 save_step 항목이 끼면 점수가
# 0/0 으로 파싱돼 비교가 무너졌다. 배포 파일 실측 (42항목 = 검증형 23 + save_step형
# 19, 검증형 10건이 save_step 항목을 직전 이웃으로 가짐):
#
#   · current_score 가 빈 문자열인 **유령 회귀 9건**
#   · 진짜 회귀 4건 중 가장 큰 낙폭(21/23 → 16/24, 체크 5개 동시 실패)을 **놓침**


class TestMixedSchemaHistory:
    """save_step 항목이 섞여도 검증 점수만 비교한다."""

    def test_save_step_entry_between_does_not_break_comparison(self):
        """THE REGRESSION: 사이에 끼어든 save_step 항목이 회귀를 가리지 않는다."""
        from tools.kg_validation import _detect_regression
        history = [
            {"score": "20/25", "failed_checks": ["X"]},
            {"step": "S2_TBOX", "metrics": {"classes": 83}},   # ← 끼어든 항목
            {"score": "18/25", "failed_checks": ["X", "Y", "Z"]},
        ]
        result = _detect_regression(history)
        assert result is not None, (
            "save_step 항목이 직전 이웃이라 회귀를 놓쳤다 (0/0 으로 파싱됨)"
        )
        assert result["previous_score"] == "20/25"
        assert result["current_score"] == "18/25"
        assert result["delta"] == -2
        assert set(result["new_failures"]) == {"Y", "Z"}

    def test_save_step_as_last_entry_is_not_a_ghost_regression(self):
        """마지막이 save_step 항목이면 유령 회귀를 만들지 않는다.

        예전에는 그 항목이 ``0/0`` 으로 읽혀 ``current_score: ""`` 인 회귀가
        보고됐다 (배포 파일에서 9건).
        """
        from tools.kg_validation import _detect_regression
        history = [
            {"score": "20/25", "failed_checks": []},
            {"score": "20/25", "failed_checks": []},
            {"step": "S9_POST_MEASURE", "metrics": {"overall_score": 97.4}},
        ]
        assert _detect_regression(history) is None, (
            "save_step 항목을 점수 0/0 으로 읽어 유령 회귀를 만들었다"
        )

    def test_only_save_step_entries_yields_none(self):
        """점수 항목이 2개 미만이면 판정하지 않는다 (미측정 ≠ 회귀 없음)."""
        from tools.kg_validation import _detect_regression
        history = [
            {"step": "S2_TBOX", "metrics": {}},
            {"step": "S3_IMPROVE", "metrics": {}},
        ]
        assert _detect_regression(history) is None

    def test_malformed_score_values_are_excluded(self):
        """빈 문자열·None·분모 0 은 점수 항목이 아니다.

        키 존재만 보면 이들이 통과해 ``0/0`` 비교가 되살아난다 — 그것이 원래 결함의
        기전이다.
        """
        from tools.validation_support.quality_history import score_entries
        history = [
            {"score": "19/25"},
            {"score": ""},
            {"score": None},
            {"score": "0/0"},
            {"score": "not a score"},
            {"step": "S9", "metrics": {}},
            "a string, not a dict",
            {"score": "18/25"},
        ]
        kept = score_entries(history)
        assert [e["score"] for e in kept] == ["19/25", "18/25"], kept

    def test_real_deployed_history_finds_the_hidden_regressions(self):
        """실측 고정: 배포 이력에서 은폐됐던 회귀가 드러난다.

        파일이 없으면 skip — 이 테스트는 산출물 대조이지 단위 계약이 아니다.
        """
        from config import GENERATED_ABOX_DIR
        from tools.validation_support.quality_history import (
            detect_regression,
            score_entries,
        )

        path = os.path.join(
            os.path.dirname(GENERATED_ABOX_DIR), "quality_history.json",
        )
        if not os.path.exists(path):
            pytest.skip("배포 quality_history.json 없음")
        with open(path, encoding="utf-8") as handle:
            history = json.load(handle)
        scored = score_entries(history)
        if len(scored) < 2:
            pytest.skip("점수 항목이 2개 미만")

        # 혼합 이력을 prefix 로 순회해도 유령(점수 빈 문자열) 회귀가 없어야 한다.
        ghosts = []
        for i in range(2, len(history) + 1):
            found = detect_regression(history[:i])
            if found and not found.get("current_score"):
                ghosts.append(found)
        assert not ghosts, f"유령 회귀 {len(ghosts)}건 — 점수 0/0 파싱이 남아 있다"


# ── _append_quality_history ──────────────────────


class TestAppendQualityHistory:
    """검증 결과 이력 추가."""

    def test_creates_new_file(self, tmp_path):
        from tools.kg_validation import _append_quality_history

        history_path = str(tmp_path / "quality_history.json")
        fake_abox_dir = str(tmp_path / "abox")

        with patch("tools.kg_validation.GENERATED_ABOX_DIR", fake_abox_dir):
            result = {
                "score": "18/20",
                "passed": False,
                "triples": 1000,
                "source": "merge",
                "checks": [
                    {"name": "check_a", "passed": True},
                    {"name": "check_b", "passed": False},
                ],
            }
            _append_quality_history(result)

        assert os.path.exists(history_path)
        with open(history_path, encoding="utf-8") as f:
            history = json.load(f)
        assert len(history) == 1
        assert history[0]["score"] == "18/20"
        assert history[0]["failed_checks"] == ["check_b"]
        assert "timestamp" in history[0]

    def test_appends_to_existing(self, tmp_path):
        from tools.kg_validation import _append_quality_history

        history_path = str(tmp_path / "quality_history.json")
        fake_abox_dir = str(tmp_path / "abox")

        # Pre-populate with one entry
        with open(history_path, "w", encoding="utf-8") as f:
            json.dump([{"score": "15/20", "timestamp": "2025-01-01T00:00:00"}], f)

        with patch("tools.kg_validation.GENERATED_ABOX_DIR", fake_abox_dir):
            result = {
                "score": "18/20",
                "passed": False,
                "triples": 2000,
                "source": "inferred",
                "checks": [{"name": "c", "passed": True}],
            }
            _append_quality_history(result)

        with open(history_path, encoding="utf-8") as f:
            history = json.load(f)
        assert len(history) == 2
        assert history[1]["score"] == "18/20"

    def test_trims_to_100(self, tmp_path):
        from tools.kg_validation import _append_quality_history

        history_path = str(tmp_path / "quality_history.json")
        fake_abox_dir = str(tmp_path / "abox")

        # Pre-populate with 100 entries
        existing = [{"score": f"{i}/20", "timestamp": f"T{i}"} for i in range(100)]
        with open(history_path, "w", encoding="utf-8") as f:
            json.dump(existing, f)

        with patch("tools.kg_validation.GENERATED_ABOX_DIR", fake_abox_dir):
            result = {
                "score": "20/20",
                "passed": True,
                "triples": 3000,
                "source": "merge",
                "checks": [],
            }
            _append_quality_history(result)

        with open(history_path, encoding="utf-8") as f:
            history = json.load(f)
        assert len(history) == 100
        # Oldest entry trimmed, newest is last
        assert history[-1]["score"] == "20/20"
        # First entry should be index 1 from original (index 0 trimmed)
        assert history[0]["score"] == "1/20"

    def test_handles_corrupted_file(self, tmp_path):
        from tools.kg_validation import _append_quality_history

        history_path = str(tmp_path / "quality_history.json")
        fake_abox_dir = str(tmp_path / "abox")

        # Write invalid JSON
        with open(history_path, "w") as f:
            f.write("NOT JSON")

        with patch("tools.kg_validation.GENERATED_ABOX_DIR", fake_abox_dir):
            result = {
                "score": "10/20",
                "passed": False,
                "triples": 500,
                "source": "merge",
                "checks": [],
            }
            _append_quality_history(result)

        with open(history_path, encoding="utf-8") as f:
            history = json.load(f)
        assert len(history) == 1
        assert history[0]["score"] == "10/20"

    def test_handles_non_list_json(self, tmp_path):
        from tools.kg_validation import _append_quality_history

        history_path = str(tmp_path / "quality_history.json")
        fake_abox_dir = str(tmp_path / "abox")

        # Write a dict instead of list
        with open(history_path, "w", encoding="utf-8") as f:
            json.dump({"not": "a list"}, f)

        with patch("tools.kg_validation.GENERATED_ABOX_DIR", fake_abox_dir):
            result = {
                "score": "12/20",
                "passed": False,
                "triples": 800,
                "source": "merge",
                "checks": [],
            }
            _append_quality_history(result)

        with open(history_path, encoding="utf-8") as f:
            history = json.load(f)
        assert len(history) == 1
        assert history[0]["score"] == "12/20"


# ── get_quality_history MCP tool ─────────────────


class TestGetQualityHistory:
    """get_quality_history() MCP 도구 테스트."""

    def test_no_history_file(self, tmp_path):
        from tools.kg_validation import get_quality_history

        fake_abox_dir = str(tmp_path / "abox")
        with patch("tools.kg_validation.GENERATED_ABOX_DIR", fake_abox_dir):
            result = json.loads(get_quality_history())
        assert result["success"] is True
        assert result["history"] == []
        assert "이력 없음" in result["message"]

    def test_returns_last_n(self, tmp_path):
        from tools.kg_validation import get_quality_history

        history_path = str(tmp_path / "quality_history.json")
        fake_abox_dir = str(tmp_path / "abox")

        entries = [
            {"score": f"{i}/20", "timestamp": f"T{i}", "failed_checks": []}
            for i in range(5)
        ]
        with open(history_path, "w", encoding="utf-8") as f:
            json.dump(entries, f)

        with patch("tools.kg_validation.GENERATED_ABOX_DIR", fake_abox_dir):
            result = json.loads(get_quality_history(last_n=3))
        assert result["success"] is True
        assert result["total_entries"] == 5
        assert result["showing"] == 3
        assert len(result["history"]) == 3
        # Should be the last 3
        assert result["history"][0]["score"] == "2/20"

    def test_returns_all_when_last_n_zero(self, tmp_path):
        from tools.kg_validation import get_quality_history

        history_path = str(tmp_path / "quality_history.json")
        fake_abox_dir = str(tmp_path / "abox")

        entries = [{"score": f"{i}/20", "timestamp": f"T{i}", "failed_checks": []} for i in range(5)]
        with open(history_path, "w", encoding="utf-8") as f:
            json.dump(entries, f)

        with patch("tools.kg_validation.GENERATED_ABOX_DIR", fake_abox_dir):
            result = json.loads(get_quality_history(last_n=0))
        assert result["showing"] == 5

    def test_includes_regression_info(self, tmp_path):
        from tools.kg_validation import get_quality_history

        history_path = str(tmp_path / "quality_history.json")
        fake_abox_dir = str(tmp_path / "abox")

        entries = [
            {"score": "18/20", "timestamp": "T1", "failed_checks": ["A"]},
            {"score": "15/20", "timestamp": "T2", "failed_checks": ["A", "B", "C"]},
        ]
        with open(history_path, "w", encoding="utf-8") as f:
            json.dump(entries, f)

        with patch("tools.kg_validation.GENERATED_ABOX_DIR", fake_abox_dir):
            result = json.loads(get_quality_history())
        assert result["regression"] is not None
        assert result["regression"]["regression"] is True
        assert result["regression"]["delta"] == -3

    def test_no_regression_key_when_improving(self, tmp_path):
        from tools.kg_validation import get_quality_history

        history_path = str(tmp_path / "quality_history.json")
        fake_abox_dir = str(tmp_path / "abox")

        entries = [
            {"score": "15/20", "timestamp": "T1", "failed_checks": ["A", "B"]},
            {"score": "18/20", "timestamp": "T2", "failed_checks": ["A"]},
        ]
        with open(history_path, "w", encoding="utf-8") as f:
            json.dump(entries, f)

        with patch("tools.kg_validation.GENERATED_ABOX_DIR", fake_abox_dir):
            result = json.loads(get_quality_history())
        assert result["regression"] is None


# ── validate_kg integration with history ─────────


class TestValidateKgHistory:
    """validate_kg()가 품질 이력을 저장하는지 통합 검증."""

    @patch("tools.kg_validation.INFERRED_PATH", "/nonexistent/path.ttl")
    @patch("tools.kg_validation.TBOX_PATH", "/nonexistent/tbox.ttl")
    @patch("tools.kg_validation._load_graph")
    def test_validate_kg_saves_history(self, mock_load_graph, tmp_path):
        """validate_kg 실행 후 quality_history.json이 생성된다."""

        from tools.kg_validation import validate_kg

        mock_load_graph.return_value = _new_graph()

        fake_abox_dir = str(tmp_path / "abox")
        history_path = str(tmp_path / "quality_history.json")

        _real_exists = os.path.exists

        def _selective_exists(path):
            if path in ("/nonexistent/path.ttl", "/nonexistent/tbox.ttl"):
                return False
            return _real_exists(path)

        with patch("tools.kg_validation.GENERATED_ABOX_DIR", fake_abox_dir), \
             patch("os.path.exists", side_effect=_selective_exists):
            result = json.loads(validate_kg(use_inferred=False))

        assert result["success"] is True
        assert _real_exists(history_path)
        with open(history_path, encoding="utf-8") as f:
            history = json.load(f)
        assert len(history) == 1
        assert history[0]["score"] == result["score"]

    @patch("tools.kg_validation.INFERRED_PATH", "/nonexistent/path.ttl")
    @patch("tools.kg_validation.TBOX_PATH", "/nonexistent/tbox.ttl")
    @patch("tools.kg_validation._load_graph")
    @patch("os.path.exists")
    def test_validate_kg_regression_key_absent_first_run(self, mock_exists, mock_load_graph, tmp_path):
        """첫 실행 시 regression 키가 없다."""

        from tools.kg_validation import validate_kg

        mock_exists.return_value = False
        mock_load_graph.return_value = _new_graph()

        fake_abox_dir = str(tmp_path / "abox")

        with patch("tools.kg_validation.GENERATED_ABOX_DIR", fake_abox_dir):
            result = json.loads(validate_kg(use_inferred=False))

        assert "regression" not in result

    @patch("tools.kg_validation.INFERRED_PATH", "/nonexistent/path.ttl")
    @patch("tools.kg_validation.TBOX_PATH", "/nonexistent/tbox.ttl")
    @patch("tools.kg_validation._load_graph")
    @patch("os.path.exists")
    def test_validate_kg_does_not_break_on_history_failure(self, mock_exists, mock_load_graph, tmp_path):
        """이력 저장 실패 시에도 validate_kg는 정상 반환한다."""

        from tools.kg_validation import validate_kg

        mock_exists.return_value = False
        mock_load_graph.return_value = _new_graph()

        # Use a non-writable path to force history save failure
        fake_abox_dir = "/nonexistent/deeply/nested/abox"

        with patch("tools.kg_validation.GENERATED_ABOX_DIR", fake_abox_dir):
            result = json.loads(validate_kg(use_inferred=False))

        assert result["success"] is True
        assert "score" in result
