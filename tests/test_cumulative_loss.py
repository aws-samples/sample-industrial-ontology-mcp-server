"""Tests for M11 Cumulative Loss Rate — 누적 보존률 계산 및 보고서 섹션 테스트."""

import json
from unittest.mock import patch

# ── _load_loss_budget 누적 보존률 테스트 ─────────────


class TestCumulativePreservationRate:
    """_load_loss_budget()가 누적 보존률(cumulative_preservation_rate)을 정확히 계산하는지 검증."""

    def test_single_stage_preservation(self, tmp_path):
        """단일 단계(inference)만 있을 때 보존률이 그대로 반영된다."""
        from tools.kg_validation import _load_loss_budget

        inferred_dir = tmp_path / "inferred"
        inferred_dir.mkdir()
        (inferred_dir / "inference_loss_manifest.json").write_text(json.dumps({
            "phase": "owl_rl_inference",
            "losses": {"self_sameAs_pruned": {"count": 5, "description": "t"}},
            "preservation_score": {"meaningful_triples_ratio": 0.95},
        }))
        inferred_path = str(inferred_dir / "all_inferred.ttl")

        with patch("tools.kg_validation.INFERRED_PATH", inferred_path), \
             patch("tools.kg_validation.GENERATED_ABOX_DIR", str(tmp_path / "nonexistent")):
            budget = _load_loss_budget()

        assert budget is not None
        assert budget["cumulative_preservation_rate"] == 0.95

    def test_two_stages_multiply(self, tmp_path):
        """A-Box + inference 2단계의 보존률이 곱으로 합산된다."""
        from tools.kg_validation import _load_loss_budget

        # A-Box (fidelity 방식)
        abox_dir = tmp_path / "abox"
        abox_dir.mkdir()
        (abox_dir / "abox_loss_manifest.json").write_text(json.dumps({
            "losses": {"unmapped_columns": {"count": 1, "description": "t"}},
            "fidelity_score": {"meaningful_triples_ratio": 0.98},
        }))

        # Inference (preservation 방식)
        inferred_dir = tmp_path / "inferred"
        inferred_dir.mkdir()
        (inferred_dir / "inference_loss_manifest.json").write_text(json.dumps({
            "phase": "owl_rl_inference",
            "losses": {"self_sameAs_pruned": {"count": 2, "description": "t"}},
            "preservation_score": {"meaningful_triples_ratio": 0.95},
        }))
        inferred_path = str(inferred_dir / "all_inferred.ttl")

        with patch("tools.kg_validation.INFERRED_PATH", inferred_path), \
             patch("tools.kg_validation.GENERATED_ABOX_DIR", str(abox_dir)):
            budget = _load_loss_budget()

        assert budget is not None
        # 0.98 * 0.95 = 0.931
        assert budget["cumulative_preservation_rate"] == 0.931

    def test_three_stages_multiply(self, tmp_path):
        """A-Box + inference + LPG 3단계의 보존률이 곱으로 합산된다."""
        from tools.kg_validation import _load_loss_budget

        # A-Box
        abox_dir = tmp_path / "abox"
        abox_dir.mkdir()
        (abox_dir / "abox_loss_manifest.json").write_text(json.dumps({
            "losses": {"unmapped_columns": {"count": 1, "description": "t"}},
            "fidelity_score": {"meaningful_triples_ratio": 0.98},
        }))

        # Inference
        inferred_dir = tmp_path / "inferred"
        inferred_dir.mkdir()
        (inferred_dir / "inference_loss_manifest.json").write_text(json.dumps({
            "phase": "owl_rl_inference",
            "losses": {"self_sameAs_pruned": {"count": 2, "description": "t"}},
            "preservation_score": {"meaningful_triples_ratio": 0.95},
        }))

        # LPG
        neo4j_dir = inferred_dir / "neo4j"
        neo4j_dir.mkdir()
        (neo4j_dir / "lpg_loss_manifest.json").write_text(json.dumps({
            "losses": {"owl_axiom_exclusions": {"count": 3, "description": "t"}},
            "preservation_score": {"meaningful_triples_ratio": 0.90},
        }))

        inferred_path = str(inferred_dir / "all_inferred.ttl")

        with patch("tools.kg_validation.INFERRED_PATH", inferred_path), \
             patch("tools.kg_validation.GENERATED_ABOX_DIR", str(abox_dir)):
            budget = _load_loss_budget()

        assert budget is not None
        # 0.98 * 0.95 * 0.90 = 0.8379
        assert budget["cumulative_preservation_rate"] == 0.8379

    def test_no_preservation_data_no_cumulative(self, tmp_path):
        """보존률 데이터가 없으면 cumulative_preservation_rate 키가 없다."""
        from tools.kg_validation import _load_loss_budget

        abox_dir = tmp_path / "abox"
        abox_dir.mkdir()
        (abox_dir / "abox_loss_manifest.json").write_text(json.dumps({
            "losses": {"unmapped_columns": {"count": 1, "description": "t"}},
            "fidelity_score": {},
        }))

        inferred_dir = tmp_path / "inferred"
        inferred_dir.mkdir()
        inferred_path = str(inferred_dir / "all_inferred.ttl")

        with patch("tools.kg_validation.INFERRED_PATH", inferred_path), \
             patch("tools.kg_validation.GENERATED_ABOX_DIR", str(abox_dir)):
            budget = _load_loss_budget()

        assert budget is not None
        assert "cumulative_preservation_rate" not in budget

    def test_scalar_preservation_value(self, tmp_path):
        """preservation이 dict가 아닌 스칼라 float인 경우에도 정상 처리."""
        from tools.kg_validation import _load_loss_budget

        inferred_dir = tmp_path / "inferred"
        inferred_dir.mkdir()
        (inferred_dir / "inference_loss_manifest.json").write_text(json.dumps({
            "phase": "owl_rl_inference",
            "losses": {"self_sameAs_pruned": {"count": 1, "description": "t"}},
            "preservation_score": {"meaningful_triples_ratio": 0.97},
        }))

        # LPG with scalar preservation
        neo4j_dir = inferred_dir / "neo4j"
        neo4j_dir.mkdir()
        (neo4j_dir / "lpg_loss_manifest.json").write_text(json.dumps({
            "losses": {"owl_axiom_exclusions": {"count": 1, "description": "t"}},
            "preservation_score": {"meaningful_triples_ratio": 0.92},
        }))

        inferred_path = str(inferred_dir / "all_inferred.ttl")

        with patch("tools.kg_validation.INFERRED_PATH", inferred_path), \
             patch("tools.kg_validation.GENERATED_ABOX_DIR", str(tmp_path / "nonexistent")):
            budget = _load_loss_budget()

        assert budget is not None
        # 0.97 * 0.92 = 0.8924
        assert budget["cumulative_preservation_rate"] == 0.8924

    def test_overall_fallback_key(self, tmp_path):
        """meaningful_triples_ratio가 없고 overall 키만 있을 때 fallback 동작."""
        from tools.kg_validation import _load_loss_budget

        inferred_dir = tmp_path / "inferred"
        inferred_dir.mkdir()
        (inferred_dir / "inference_loss_manifest.json").write_text(json.dumps({
            "phase": "owl_rl_inference",
            "losses": {"self_sameAs_pruned": {"count": 1, "description": "t"}},
            "preservation_score": {"overall": 0.88},
        }))

        inferred_path = str(inferred_dir / "all_inferred.ttl")

        with patch("tools.kg_validation.INFERRED_PATH", inferred_path), \
             patch("tools.kg_validation.GENERATED_ABOX_DIR", str(tmp_path / "nonexistent")):
            budget = _load_loss_budget()

        assert budget is not None
        assert budget["cumulative_preservation_rate"] == 0.88


# ── 보고서 HTML 정보 보존률 섹션 테스트 ─────────────


class TestReportInformationPreservation:
    """report.py의 _build_html()이 정보 보존률 섹션을 올바르게 렌더링하는지 검증."""

    def test_preservation_section_rendered(self, sample_tbox_ttl, sample_abox_ttl, tmp_path):
        """loss_budget 데이터가 있으면 정보 보존률 테이블이 HTML에 포함된다."""
        from tools.report import _build_html, _collect_all_data

        tbox_file = tmp_path / "tbox" / "t_box.ttl"
        tbox_file.parent.mkdir()
        tbox_file.write_text(sample_tbox_ttl)
        abox_file = tmp_path / "abox" / "a_box.ttl"
        abox_file.parent.mkdir()
        abox_file.write_text(sample_abox_ttl)
        inferred_file = tmp_path / "inferred" / "all_inferred.ttl"
        inferred_file.parent.mkdir()
        inferred_file.write_text("# empty\n")
        dict_file = tmp_path / "semantic_dictionary.json"
        dict_file.write_text("{}")
        tacit_dir = tmp_path / "tacit"
        tacit_dir.mkdir()
        rawdata_dir = tmp_path / "rawdata"
        rawdata_dir.mkdir()

        loss_budget = {
            "stages": {
                "abox_generation": {
                    "total_loss_items": 3,
                    "fidelity": {"meaningful_triples_ratio": 0.98},
                },
                "inference": {
                    "noise_pruned": 10,
                    "preservation": {"meaningful_triples_ratio": 0.95},
                },
            },
            "total_loss_items": 13,
            "cumulative_preservation_rate": 0.931,
        }

        with patch("tools.report.TBOX_PATH", str(tbox_file)), \
             patch("tools.report.ABOX_PATH", str(abox_file)), \
             patch("tools.report.INFERRED_PATH", str(inferred_file)), \
             patch("tools.report.SEMANTIC_DICT_PATH", str(dict_file)), \
             patch("tools.report.SOURCE_TACIT_DIR", str(tacit_dir)), \
             patch("tools.report.SOURCE_RAWDATA_DIR", str(rawdata_dir)), \
             patch("tools.report._collect_dict_validation", return_value={"passed": True, "summary": {}, "coverage": {}, "sections": {}, "issues": []}), \
             patch("tools.report._collect_dict_stats", return_value={"classes": 0, "object_properties": 0, "functional_properties": 0, "anti_patterns": 0, "common_patterns": 0, "question_templates": 0}):
            data = _collect_all_data()

        data["loss_budget"] = loss_budget
        data["tbox_vis_path"] = ""
        html = _build_html(data)

        assert "정보 보존률" in html
        assert "A-Box 생성" in html
        assert "OWL RL 추론" in html
        assert "98.0%" in html
        assert "95.0%" in html
        assert "93.1%" in html
        assert "누적 보존률" in html

    def test_no_loss_budget_no_section(self, sample_tbox_ttl, sample_abox_ttl, tmp_path):
        """loss_budget가 None이면 정보 보존률 섹션이 렌더링되지 않는다."""
        from tools.report import _build_html, _collect_all_data

        tbox_file = tmp_path / "tbox" / "t_box.ttl"
        tbox_file.parent.mkdir()
        tbox_file.write_text(sample_tbox_ttl)
        abox_file = tmp_path / "abox" / "a_box.ttl"
        abox_file.parent.mkdir()
        abox_file.write_text(sample_abox_ttl)
        inferred_file = tmp_path / "inferred" / "all_inferred.ttl"
        inferred_file.parent.mkdir()
        inferred_file.write_text("# empty\n")
        dict_file = tmp_path / "semantic_dictionary.json"
        dict_file.write_text("{}")
        tacit_dir = tmp_path / "tacit"
        tacit_dir.mkdir()
        rawdata_dir = tmp_path / "rawdata"
        rawdata_dir.mkdir()

        with patch("tools.report.TBOX_PATH", str(tbox_file)), \
             patch("tools.report.ABOX_PATH", str(abox_file)), \
             patch("tools.report.INFERRED_PATH", str(inferred_file)), \
             patch("tools.report.SEMANTIC_DICT_PATH", str(dict_file)), \
             patch("tools.report.SOURCE_TACIT_DIR", str(tacit_dir)), \
             patch("tools.report.SOURCE_RAWDATA_DIR", str(rawdata_dir)), \
             patch("tools.report._collect_dict_validation", return_value={"passed": True, "summary": {}, "coverage": {}, "sections": {}, "issues": []}), \
             patch("tools.report._collect_dict_stats", return_value={"classes": 0, "object_properties": 0, "functional_properties": 0, "anti_patterns": 0, "common_patterns": 0, "question_templates": 0}):
            data = _collect_all_data()

        data["loss_budget"] = None
        data["tbox_vis_path"] = ""
        html = _build_html(data)

        assert "정보 보존률" not in html

    def test_lpg_stage_label(self, sample_tbox_ttl, sample_abox_ttl, tmp_path):
        """LPG 변환 단계의 한국어 라벨이 올바르게 표시된다."""
        from tools.report import _build_html, _collect_all_data

        tbox_file = tmp_path / "tbox" / "t_box.ttl"
        tbox_file.parent.mkdir()
        tbox_file.write_text(sample_tbox_ttl)
        abox_file = tmp_path / "abox" / "a_box.ttl"
        abox_file.parent.mkdir()
        abox_file.write_text(sample_abox_ttl)
        inferred_file = tmp_path / "inferred" / "all_inferred.ttl"
        inferred_file.parent.mkdir()
        inferred_file.write_text("# empty\n")
        dict_file = tmp_path / "semantic_dictionary.json"
        dict_file.write_text("{}")
        tacit_dir = tmp_path / "tacit"
        tacit_dir.mkdir()
        rawdata_dir = tmp_path / "rawdata"
        rawdata_dir.mkdir()

        loss_budget = {
            "stages": {
                "lpg_conversion": {
                    "total_loss_items": 5,
                    "preservation": {"meaningful_triples_ratio": 0.91},
                },
            },
            "total_loss_items": 5,
        }

        with patch("tools.report.TBOX_PATH", str(tbox_file)), \
             patch("tools.report.ABOX_PATH", str(abox_file)), \
             patch("tools.report.INFERRED_PATH", str(inferred_file)), \
             patch("tools.report.SEMANTIC_DICT_PATH", str(dict_file)), \
             patch("tools.report.SOURCE_TACIT_DIR", str(tacit_dir)), \
             patch("tools.report.SOURCE_RAWDATA_DIR", str(rawdata_dir)), \
             patch("tools.report._collect_dict_validation", return_value={"passed": True, "summary": {}, "coverage": {}, "sections": {}, "issues": []}), \
             patch("tools.report._collect_dict_stats", return_value={"classes": 0, "object_properties": 0, "functional_properties": 0, "anti_patterns": 0, "common_patterns": 0, "question_templates": 0}):
            data = _collect_all_data()

        data["loss_budget"] = loss_budget
        data["tbox_vis_path"] = ""
        html = _build_html(data)

        assert "LPG 변환" in html
        assert "91.0%" in html
        assert "누적 보존률" not in html  # cumulative not set

    def test_na_preservation_displays_string(self, sample_tbox_ttl, sample_abox_ttl, tmp_path):
        """보존률 데이터가 없으면 N/A로 표시된다."""
        from tools.report import _build_html, _collect_all_data

        tbox_file = tmp_path / "tbox" / "t_box.ttl"
        tbox_file.parent.mkdir()
        tbox_file.write_text(sample_tbox_ttl)
        abox_file = tmp_path / "abox" / "a_box.ttl"
        abox_file.parent.mkdir()
        abox_file.write_text(sample_abox_ttl)
        inferred_file = tmp_path / "inferred" / "all_inferred.ttl"
        inferred_file.parent.mkdir()
        inferred_file.write_text("# empty\n")
        dict_file = tmp_path / "semantic_dictionary.json"
        dict_file.write_text("{}")
        tacit_dir = tmp_path / "tacit"
        tacit_dir.mkdir()
        rawdata_dir = tmp_path / "rawdata"
        rawdata_dir.mkdir()

        loss_budget = {
            "stages": {
                "abox_generation": {
                    "total_loss_items": 2,
                    "fidelity": {},  # no meaningful_triples_ratio
                },
            },
            "total_loss_items": 2,
        }

        with patch("tools.report.TBOX_PATH", str(tbox_file)), \
             patch("tools.report.ABOX_PATH", str(abox_file)), \
             patch("tools.report.INFERRED_PATH", str(inferred_file)), \
             patch("tools.report.SEMANTIC_DICT_PATH", str(dict_file)), \
             patch("tools.report.SOURCE_TACIT_DIR", str(tacit_dir)), \
             patch("tools.report.SOURCE_RAWDATA_DIR", str(rawdata_dir)), \
             patch("tools.report._collect_dict_validation", return_value={"passed": True, "summary": {}, "coverage": {}, "sections": {}, "issues": []}), \
             patch("tools.report._collect_dict_stats", return_value={"classes": 0, "object_properties": 0, "functional_properties": 0, "anti_patterns": 0, "common_patterns": 0, "question_templates": 0}):
            data = _collect_all_data()

        data["loss_budget"] = loss_budget
        data["tbox_vis_path"] = ""
        html = _build_html(data)

        assert "N/A" in html


# ── _collect_all_data 통합 테스트 ─────────────


class TestCollectAllDataLossBudget:
    """_collect_all_data()가 loss_budget을 정상 수집하는지 검증."""

    def test_loss_budget_collected(self, sample_tbox_ttl, sample_abox_ttl, tmp_path):
        """_load_loss_budget이 호출되어 data['loss_budget']에 저장된다."""
        from tools.report import _collect_all_data

        tbox_file = tmp_path / "tbox" / "t_box.ttl"
        tbox_file.parent.mkdir()
        tbox_file.write_text(sample_tbox_ttl)
        abox_file = tmp_path / "abox" / "a_box.ttl"
        abox_file.parent.mkdir()
        abox_file.write_text(sample_abox_ttl)
        inferred_file = tmp_path / "inferred" / "all_inferred.ttl"
        inferred_file.parent.mkdir()
        inferred_file.write_text("# empty\n")
        dict_file = tmp_path / "semantic_dictionary.json"
        dict_file.write_text("{}")
        tacit_dir = tmp_path / "tacit"
        tacit_dir.mkdir()
        rawdata_dir = tmp_path / "rawdata"
        rawdata_dir.mkdir()

        mock_budget = {"stages": {"inference": {}}, "total_loss_items": 5}

        with patch("tools.report.TBOX_PATH", str(tbox_file)), \
             patch("tools.report.ABOX_PATH", str(abox_file)), \
             patch("tools.report.INFERRED_PATH", str(inferred_file)), \
             patch("tools.report.SEMANTIC_DICT_PATH", str(dict_file)), \
             patch("tools.report.SOURCE_TACIT_DIR", str(tacit_dir)), \
             patch("tools.report.SOURCE_RAWDATA_DIR", str(rawdata_dir)), \
             patch("tools.report._collect_dict_validation", return_value=None), \
             patch("tools.report._collect_dict_stats", return_value=None), \
             patch("tools.kg_validation._load_loss_budget", return_value=mock_budget):
            data = _collect_all_data()

        assert "loss_budget" in data
        assert data["loss_budget"] == mock_budget

    def test_loss_budget_exception_returns_none(self, sample_tbox_ttl, sample_abox_ttl, tmp_path):
        """_load_loss_budget에서 예외 발생 시 loss_budget이 None으로 설정된다."""
        from tools.report import _collect_all_data

        tbox_file = tmp_path / "tbox" / "t_box.ttl"
        tbox_file.parent.mkdir()
        tbox_file.write_text(sample_tbox_ttl)
        abox_file = tmp_path / "abox" / "a_box.ttl"
        abox_file.parent.mkdir()
        abox_file.write_text(sample_abox_ttl)
        inferred_file = tmp_path / "inferred" / "all_inferred.ttl"
        inferred_file.parent.mkdir()
        inferred_file.write_text("# empty\n")
        dict_file = tmp_path / "semantic_dictionary.json"
        dict_file.write_text("{}")
        tacit_dir = tmp_path / "tacit"
        tacit_dir.mkdir()
        rawdata_dir = tmp_path / "rawdata"
        rawdata_dir.mkdir()

        with patch("tools.report.TBOX_PATH", str(tbox_file)), \
             patch("tools.report.ABOX_PATH", str(abox_file)), \
             patch("tools.report.INFERRED_PATH", str(inferred_file)), \
             patch("tools.report.SEMANTIC_DICT_PATH", str(dict_file)), \
             patch("tools.report.SOURCE_TACIT_DIR", str(tacit_dir)), \
             patch("tools.report.SOURCE_RAWDATA_DIR", str(rawdata_dir)), \
             patch("tools.report._collect_dict_validation", return_value=None), \
             patch("tools.report._collect_dict_stats", return_value=None), \
             patch("tools.kg_validation._load_loss_budget", side_effect=RuntimeError("test")):
            data = _collect_all_data()

        assert "loss_budget" in data
        assert data["loss_budget"] is None
