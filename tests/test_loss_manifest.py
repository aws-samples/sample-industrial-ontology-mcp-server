"""Tests for inference loss manifest — M2 Loss Manifest 기능 테스트.

_build_inference_loss_manifest() 함수와 run_owl_rl_inference(force=True) 통합,
_load_loss_budget() 3단계 확장을 검증한다.
"""

import json
import os
from unittest.mock import MagicMock, patch

from rdflib import OWL, RDF, Graph, Literal, URIRef

from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS
from domain.tbox_utils import _new_graph

# ── 헬퍼 ──────────────────────────────────────────


def _make_minimal_graph():
    """추론 테스트용 최소 그래프."""
    g = _new_graph()
    eq_cls = URIRef(f"{DOMAIN_NS}EquipmentMaster")
    g.add((eq_cls, RDF.type, OWL.Class))
    eq1 = URIRef(f"{DOMAIN_INST_NS}EquipmentMaster_EQ001")
    g.add((eq1, RDF.type, eq_cls))
    g.add((eq1, URIRef(f"{DOMAIN_NS}equipmentID"), Literal("EQ001")))
    return g


def _stub_tbox_load():
    """Keep the T-Box consistent with the mocked merged graph.

    ``_run_owl_rl_inference_sync`` derives ``abox_triples = triples_before - len(tbox)``.
    These tests mock ``_load_and_merge`` with a 3-triple graph but the T-Box is loaded
    through ``fast_parse_turtle``, which uses oxigraph's bulk loader rather than
    ``Graph.parse`` — so ``patch.object(Graph, "parse")`` does not stop it and the real
    **deployed** T-Box (4,922 triples) is read. That mixed fixture produced
    ``abox_triples = 3 - 4922 = -4919``, an arithmetic impossibility that the manifest
    now refuses to persist (``manifest_arithmetic_error``).

    Leaving the T-Box empty keeps the fixture internally coherent. What these tests
    assert is *that the manifest is written with the right shape*, not the triple counts.
    """
    return patch("domain.tbox_utils.fast_parse_turtle", side_effect=lambda g, *a, **k: g)


# ── _build_inference_loss_manifest 단위 테스트 ─────


class TestBuildInferenceLossManifest:
    """_build_inference_loss_manifest() 반환값 구조 및 계산 검증."""

    def test_returns_required_top_level_keys(self):
        """반환 dict에 phase, timestamp, input, output, losses, preservation_score 존재."""
        from tools.inference import _build_inference_loss_manifest

        result = _build_inference_loss_manifest(
            prune_stats={"self_sameAs_removed": 0, "self_equivalentClass_removed": 0,
                         "implicit_subClassOf_removed": 0, "total_pruned": 0},
            pollution_removed=0,
            restriction_violation_count=0,
            input_stats={"tbox_triples": 100, "abox_triples": 500, "tacit_triples": 10},
            output_stats={"total_triples": 700, "inferred_new": 90},
        )

        assert result["phase"] == "owl_rl_inference"
        assert "timestamp" in result
        assert result["input"] == {"tbox_triples": 100, "abox_triples": 500, "tacit_triples": 10}
        assert result["output"] == {"total_triples": 700, "inferred_new": 90}
        assert "losses" in result
        assert "preservation_score" in result

    def test_losses_structure(self):
        """losses에 5개 카테고리가 존재하고 각각 count, description 키를 가진다."""
        from tools.inference import _build_inference_loss_manifest

        result = _build_inference_loss_manifest(
            prune_stats={"self_sameAs_removed": 5, "self_equivalentClass_removed": 3,
                         "implicit_subClassOf_removed": 10, "total_pruned": 18},
            pollution_removed=2,
            restriction_violation_count=7,
            input_stats={"tbox_triples": 100, "abox_triples": 500, "tacit_triples": 10},
            output_stats={"total_triples": 700, "inferred_new": 90},
        )

        expected_loss_keys = {
            "self_sameAs_pruned",
            "self_equivalentClass_pruned",
            "implicit_subClassOf_pruned",
            "type_pollution_removed",
            "restriction_violations",
        }
        assert set(result["losses"].keys()) == expected_loss_keys

        for key, entry in result["losses"].items():
            assert "count" in entry, f"losses[{key}] missing 'count'"
            assert "description" in entry, f"losses[{key}] missing 'description'"

    def test_loss_counts_match_inputs(self):
        """각 loss 카테고리의 count가 입력 파라미터와 일치."""
        from tools.inference import _build_inference_loss_manifest

        result = _build_inference_loss_manifest(
            prune_stats={"self_sameAs_removed": 5, "self_equivalentClass_removed": 3,
                         "implicit_subClassOf_removed": 10, "total_pruned": 18},
            pollution_removed=2,
            restriction_violation_count=7,
            input_stats={"tbox_triples": 100, "abox_triples": 500, "tacit_triples": 10},
            output_stats={"total_triples": 700, "inferred_new": 90},
        )

        losses = result["losses"]
        assert losses["self_sameAs_pruned"]["count"] == 5
        assert losses["self_equivalentClass_pruned"]["count"] == 3
        assert losses["implicit_subClassOf_pruned"]["count"] == 10
        assert losses["type_pollution_removed"]["count"] == 2
        assert losses["restriction_violations"]["count"] == 7

    def test_preservation_score_calculation(self):
        """preservation_score의 meaningful/noise 비율이 정확히 계산된다."""
        from tools.inference import _build_inference_loss_manifest

        # total_noise = total_pruned(18) + pollution_removed(2) = 20
        # total_output = 700
        # meaningful_triples_ratio = (700 - 20) / 700 = 0.9714
        # noise_removed_ratio = 20 / 700 = 0.0286
        result = _build_inference_loss_manifest(
            prune_stats={"self_sameAs_removed": 5, "self_equivalentClass_removed": 3,
                         "implicit_subClassOf_removed": 10, "total_pruned": 18},
            pollution_removed=2,
            restriction_violation_count=0,
            input_stats={"tbox_triples": 100, "abox_triples": 500, "tacit_triples": 10},
            output_stats={"total_triples": 700, "inferred_new": 90},
        )

        ps = result["preservation_score"]
        assert ps["meaningful_triples_ratio"] == round((700 - 20) / 700, 4)
        assert ps["noise_removed_ratio"] == round(20 / 700, 4)

    def test_zero_output_no_division_error(self):
        """total_triples가 0이어도 ZeroDivisionError 발생하지 않는다."""
        from tools.inference import _build_inference_loss_manifest

        result = _build_inference_loss_manifest(
            prune_stats={"self_sameAs_removed": 0, "self_equivalentClass_removed": 0,
                         "implicit_subClassOf_removed": 0, "total_pruned": 0},
            pollution_removed=0,
            restriction_violation_count=0,
            input_stats={"tbox_triples": 0, "abox_triples": 0, "tacit_triples": 0},
            output_stats={"total_triples": 0, "inferred_new": 0},
        )

        ps = result["preservation_score"]
        assert ps["meaningful_triples_ratio"] == 0.0
        assert ps["noise_removed_ratio"] == 0.0

    def test_all_zero_stats(self):
        """모든 통계가 0이면 losses 전부 count=0."""
        from tools.inference import _build_inference_loss_manifest

        result = _build_inference_loss_manifest(
            prune_stats={"self_sameAs_removed": 0, "self_equivalentClass_removed": 0,
                         "implicit_subClassOf_removed": 0, "total_pruned": 0},
            pollution_removed=0,
            restriction_violation_count=0,
            input_stats={"tbox_triples": 0, "abox_triples": 0, "tacit_triples": 0},
            output_stats={"total_triples": 100, "inferred_new": 50},
        )

        for entry in result["losses"].values():
            assert entry["count"] == 0

        ps = result["preservation_score"]
        assert ps["meaningful_triples_ratio"] == 1.0
        assert ps["noise_removed_ratio"] == 0.0


# ── run_owl_rl_inference 통합 — manifest 저장 검증 ─


class TestInferenceLossManifestIntegration:
    """run_owl_rl_inference(force=True)에서 inference_loss_manifest.json이 올바르게 저장되는지 검증."""

    @patch("tools.inference.analyze_inference_quality")
    @patch("tools.inference.atomic_write_json")
    @patch("tools.inference.atomic_write")
    @patch("domain.tbox_utils.invalidate_graph_cache")
    @patch("tools.inference.os.path.exists")
    @patch("tools.inference.os.path.getsize", return_value=1024)
    @patch("tools.inference.PyReasoner")
    @patch("tools.inference._load_and_merge")
    @patch("tools.inference._detect_type_pollution")
    def test_manifest_written_on_success(
        self, mock_detect, mock_merge, mock_closure, mock_getsize,
        mock_exists, mock_invalidate, mock_write, mock_write_json, mock_quality,
    ):
        """추론 성공 시 inference_loss_manifest.json이 atomic_write_json으로 기록된다."""
        from tools.inference import INFERRED_PATH
        from tools.inference import _run_owl_rl_inference_sync as run_owl_rl_inference

        g = _make_minimal_graph()
        mock_merge.return_value = (g, 1)
        mock_exists.return_value = True
        mock_closure.return_value = MagicMock()
        mock_detect.return_value = []
        mock_quality.return_value = json.dumps({
            "type_pollution": {"count": 0},
            "explosion": {"level": "NORMAL"},
            "top_inferred_types": [],
        })

        with patch.object(Graph, "parse"), _stub_tbox_load():
            result = json.loads(run_owl_rl_inference(force=True))

        assert result["success"] is True

        manifest_path = os.path.join(
            os.path.dirname(INFERRED_PATH), "inference_loss_manifest.json"
        )
        write_calls = mock_write_json.call_args_list
        manifest_calls = [c for c in write_calls if c[0][0] == manifest_path]
        # 이 경로에는 쓰기가 **두 번** 온다. 1) 매니페스트 2) 직렬화 뒤 출력 지문
        # 각인. 2단인 이유는 사이드카가 all_inferred.ttl 보다 먼저 쓰이기 때문이다 —
        # 그 시점에 출력을 해시하면 이전 세대 파일이 찍힌다
        # (tools/common.py::add_source_artifacts docstring).
        #
        # ⚠️ 호출 **횟수**를 고정하지 않는다. 각인 패스는 파일이 이미 존재할 때만
        # 오므로 배포 트리 상태에 따라 1 또는 2 다 — 이 테스트는 `== 1` 로 통과했다가
        # 매니페스트가 격리에서 돌아오자 깨졌다. 환경 의존 단언이었다.
        assert len(manifest_calls) >= 1, (
            f"매니페스트가 최소 1회 기록돼야 함, "
            f"실제: {[c[0][0] for c in write_calls]}"
        )

        written = manifest_calls[0][0][1]
        assert written["phase"] == "owl_rl_inference"
        assert "losses" in written
        assert "preservation_score" in written
        # 1회차에 각인되는 것은 **입력** 지문이다 (출력은 아직 이전 세대다)
        assert set(written["_source"]["artifacts"]) == {"tbox", "abox", "delta"}
        if len(manifest_calls) > 1:
            assert "inferred" in manifest_calls[1][0][1]["_source"]["artifacts"]

    @patch("tools.inference.analyze_inference_quality")
    @patch("tools.inference.atomic_write_json")
    @patch("tools.inference.atomic_write")
    @patch("domain.tbox_utils.invalidate_graph_cache")
    @patch("tools.inference.os.path.exists")
    @patch("tools.inference.os.path.getsize", return_value=1024)
    @patch("tools.inference.PyReasoner")
    @patch("tools.inference._load_and_merge")
    @patch("tools.inference._detect_type_pollution")
    def test_manifest_contains_correct_input_stats(
        self, mock_detect, mock_merge, mock_closure, mock_getsize,
        mock_exists, mock_invalidate, mock_write, mock_write_json, mock_quality,
    ):
        """manifest의 input 필드에 tbox_triples, abox_triples, tacit_triples가 포함된다."""
        from tools.inference import INFERRED_PATH
        from tools.inference import _run_owl_rl_inference_sync as run_owl_rl_inference

        g = _make_minimal_graph()
        mock_merge.return_value = (g, 2)  # tacit_count=2
        mock_exists.return_value = True
        mock_closure.return_value = MagicMock()
        mock_detect.return_value = []
        mock_quality.return_value = json.dumps({
            "type_pollution": {"count": 0},
            "explosion": {"level": "NORMAL"},
            "top_inferred_types": [],
        })

        with patch.object(Graph, "parse"), _stub_tbox_load():
            result = json.loads(run_owl_rl_inference(force=True))

        assert result["success"] is True

        manifest_path = os.path.join(
            os.path.dirname(INFERRED_PATH), "inference_loss_manifest.json"
        )
        write_calls = mock_write_json.call_args_list
        manifest_calls = [c for c in write_calls if c[0][0] == manifest_path]
        # 각인 패스는 파일 존재에 의존하므로 횟수를 고정하지 않는다 (위 주석 참조)
        assert len(manifest_calls) >= 1

        written = manifest_calls[0][0][1]
        inp = written["input"]
        assert "tbox_triples" in inp
        assert "abox_triples" in inp
        assert "tacit_triples" in inp
        # 픽스처가 실행으로서 성립해야 한다 — 음수 트리플 수는 T-Box 만 실물로 읽는
        # 혼합 mock 의 신호이고, 그 값이 배포 파일로 새어 나가 게이트를 오작동시켰다.
        assert inp["abox_triples"] >= 0, (
            f"픽스처 산술 불성립: abox_triples={inp['abox_triples']} — mock 한 병합 "
            f"그래프보다 T-Box 가 크다 (T-Box 를 실물로 읽고 있다)"
        )

    @patch("tools.inference.analyze_inference_quality")
    @patch("tools.inference.atomic_write")
    @patch("domain.tbox_utils.invalidate_graph_cache")
    @patch("tools.inference.os.path.exists")
    @patch("tools.inference.os.path.getsize", return_value=1024)
    @patch("tools.inference.PyReasoner")
    @patch("tools.inference._load_and_merge")
    @patch("tools.inference._detect_type_pollution")
    def test_manifest_failure_does_not_break_inference(
        self, mock_detect, mock_merge, mock_closure, mock_getsize,
        mock_exists, mock_invalidate, mock_write, mock_quality,
    ):
        """_build_inference_loss_manifest 실패 시에도 추론 결과는 정상 반환된다."""
        from tools.inference import _run_owl_rl_inference_sync as run_owl_rl_inference

        g = _make_minimal_graph()
        mock_merge.return_value = (g, 0)
        mock_exists.return_value = True
        mock_closure.return_value = MagicMock()
        mock_detect.return_value = []
        mock_quality.return_value = json.dumps({
            "type_pollution": {"count": 0},
            "explosion": {"level": "NORMAL"},
            "top_inferred_types": [],
        })

        # atomic_write에서 manifest 경로일 때만 에러 발생시키기

        def selective_fail(path, content):
            if "loss_manifest" in path:
                raise OSError("Disk full")

        mock_write.side_effect = selective_fail

        with patch.object(Graph, "parse"):
            # 모든 atomic_write가 실패하지만 추론 자체는 성공해야 함
            # 실제로는 manifest 외의 write도 실패하므로, 더 정교하게 모킹
            pass

        # 대신 _build_inference_loss_manifest를 직접 예외 발생시키는 테스트
        with patch("tools.inference._build_inference_loss_manifest", side_effect=RuntimeError("test")):
            mock_write.side_effect = None  # 원래대로
            with patch.object(Graph, "parse"):
                result = json.loads(run_owl_rl_inference(force=True))

        assert result["success"] is True


# ── _load_loss_budget 3단계 확장 테스트 ─────────────


class TestLoadLossBudgetInferenceStage:
    """_load_loss_budget()가 inference_loss_manifest.json을 로드하여 3단계 budget을 구성하는지 검증."""

    def test_loads_inference_manifest(self, tmp_path):
        """inference_loss_manifest.json이 있으면 budget.stages.inference에 반영된다."""
        from tools.kg_validation import _load_loss_budget

        manifest = {
            "phase": "owl_rl_inference",
            "losses": {
                "self_sameAs_pruned": {"count": 5, "description": "test"},
                "self_equivalentClass_pruned": {"count": 3, "description": "test"},
                "implicit_subClassOf_pruned": {"count": 10, "description": "test"},
                "type_pollution_removed": {"count": 2, "description": "test"},
                "restriction_violations": {"count": 7, "description": "test"},
            },
            "preservation_score": {
                "meaningful_triples_ratio": 0.9714,
                "noise_removed_ratio": 0.0286,
            },
        }

        manifest_path = tmp_path / "inference_loss_manifest.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False))

        inferred_path = str(tmp_path / "all_inferred.ttl")

        with patch("tools.kg_validation.INFERRED_PATH", inferred_path), \
             patch("tools.kg_validation.GENERATED_ABOX_DIR", str(tmp_path / "nonexistent")):
            budget = _load_loss_budget()

        assert budget is not None
        assert "inference" in budget["stages"]
        inf_stage = budget["stages"]["inference"]
        assert inf_stage["noise_pruned"] == 27  # 5+3+10+2+7
        assert inf_stage["type_pollution"] == 2
        assert inf_stage["preservation"]["meaningful_triples_ratio"] == 0.9714
        assert budget["total_loss_items"] == 27

    def test_inference_and_abox_combined(self, tmp_path):
        """A-Box + inference manifest 모두 있으면 합산된다."""
        from tools.kg_validation import _load_loss_budget

        # A-Box manifest
        abox_dir = tmp_path / "abox"
        abox_dir.mkdir()
        abox_manifest = {
            "losses": {
                "unmapped_columns": {"count": 3, "description": "test"},
                "fk_referential_failures": {"count": 1, "description": "test"},
                "type_coercion_failures": {"count": 0, "description": "test"},
                "duplicate_pk_rows": {"count": 0, "description": "test"},
            },
            "fidelity_score": {"row_coverage": 0.99},
        }
        (abox_dir / "abox_loss_manifest.json").write_text(
            json.dumps(abox_manifest, ensure_ascii=False)
        )

        # Inference manifest
        inf_manifest = {
            "phase": "owl_rl_inference",
            "losses": {
                "self_sameAs_pruned": {"count": 10, "description": "test"},
                "self_equivalentClass_pruned": {"count": 0, "description": "test"},
                "implicit_subClassOf_pruned": {"count": 5, "description": "test"},
                "type_pollution_removed": {"count": 1, "description": "test"},
                "restriction_violations": {"count": 0, "description": "test"},
            },
            "preservation_score": {"meaningful_triples_ratio": 0.98, "noise_removed_ratio": 0.02},
        }
        inferred_dir = tmp_path / "inferred"
        inferred_dir.mkdir()
        (inferred_dir / "inference_loss_manifest.json").write_text(
            json.dumps(inf_manifest, ensure_ascii=False)
        )

        inferred_path = str(inferred_dir / "all_inferred.ttl")

        with patch("tools.kg_validation.INFERRED_PATH", inferred_path), \
             patch("tools.kg_validation.GENERATED_ABOX_DIR", str(abox_dir)):
            budget = _load_loss_budget()

        assert budget is not None
        assert "abox_generation" in budget["stages"]
        assert "inference" in budget["stages"]
        # A-Box: 3+1+0+0=4, Inference: 10+0+5+1+0=16, total=20
        assert budget["total_loss_items"] == 4 + 16

    def test_no_inference_manifest_still_loads_abox(self, tmp_path):
        """inference manifest가 없어도 기존 A-Box/LPG 로드는 정상 동작한다."""
        from tools.kg_validation import _load_loss_budget

        abox_dir = tmp_path / "abox"
        abox_dir.mkdir()
        abox_manifest = {
            "losses": {
                "unmapped_columns": {"count": 2, "description": "test"},
            },
            "fidelity_score": {},
        }
        (abox_dir / "abox_loss_manifest.json").write_text(
            json.dumps(abox_manifest, ensure_ascii=False)
        )

        inferred_dir = tmp_path / "inferred"
        inferred_dir.mkdir()
        inferred_path = str(inferred_dir / "all_inferred.ttl")
        # inference_loss_manifest.json은 없음

        with patch("tools.kg_validation.INFERRED_PATH", inferred_path), \
             patch("tools.kg_validation.GENERATED_ABOX_DIR", str(abox_dir)):
            budget = _load_loss_budget()

        assert budget is not None
        assert "abox_generation" in budget["stages"]
        assert "inference" not in budget["stages"]
        assert budget["total_loss_items"] == 2

    def test_empty_stages_returns_none(self, tmp_path):
        """어떤 manifest도 없으면 None 반환."""
        from tools.kg_validation import _load_loss_budget

        inferred_dir = tmp_path / "inferred"
        inferred_dir.mkdir()
        inferred_path = str(inferred_dir / "all_inferred.ttl")

        with patch("tools.kg_validation.INFERRED_PATH", inferred_path), \
             patch("tools.kg_validation.GENERATED_ABOX_DIR", str(tmp_path / "nonexistent")):
            budget = _load_loss_budget()

        assert budget is None

    def test_all_three_stages(self, tmp_path):
        """A-Box + LPG + Inference 3단계 모두 로드 시 합산."""
        from tools.kg_validation import _load_loss_budget

        # A-Box
        abox_dir = tmp_path / "abox"
        abox_dir.mkdir()
        (abox_dir / "abox_loss_manifest.json").write_text(json.dumps({
            "losses": {"unmapped_columns": {"count": 1, "description": "t"}},
            "fidelity_score": {},
        }))

        # Inferred dir + inference manifest
        inferred_dir = tmp_path / "inferred"
        inferred_dir.mkdir()
        (inferred_dir / "inference_loss_manifest.json").write_text(json.dumps({
            "phase": "owl_rl_inference",
            "losses": {"self_sameAs_pruned": {"count": 2, "description": "t"}},
            "preservation_score": {},
        }))

        # LPG
        neo4j_dir = inferred_dir / "neo4j"
        neo4j_dir.mkdir()
        (neo4j_dir / "lpg_loss_manifest.json").write_text(json.dumps({
            "losses": {"owl_axiom_exclusions": {"count": 3, "description": "t"}},
            "preservation_score": {},
        }))

        inferred_path = str(inferred_dir / "all_inferred.ttl")

        with patch("tools.kg_validation.INFERRED_PATH", inferred_path), \
             patch("tools.kg_validation.GENERATED_ABOX_DIR", str(abox_dir)):
            budget = _load_loss_budget()

        assert budget is not None
        assert set(budget["stages"].keys()) == {"abox_generation", "inference", "lpg_conversion"}
        assert budget["total_loss_items"] == 1 + 2 + 3
