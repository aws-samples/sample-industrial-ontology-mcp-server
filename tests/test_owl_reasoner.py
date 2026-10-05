"""Tests for tools/owl_reasoner.py — OWL DL 추론 기반 검증 도구 테스트.

owlready2 + Java(HermiT/Pellet)를 모킹하여 단위 테스트.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

# owlready2는 import 시 SQLite 월드를 초기화하므로, 테스트 환경에서 실패할 수 있음
try:
    import owlready2  # noqa: F401
    _OWLREADY2_AVAILABLE = True
except Exception:
    _OWLREADY2_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _OWLREADY2_AVAILABLE,
    reason="owlready2 초기화 실패 (Java 미설치 또는 환경 문제)",
)


@pytest.fixture(autouse=True)
def _clear_hermit_cache():
    """테스트 격리: HermiT 결과 캐시를 매 테스트 전후로 비운다."""
    from tools.owl_reasoner import clear_hermit_cache
    clear_hermit_cache()
    yield
    clear_hermit_cache()


# ── _safe_unlink ──────────────────────────────────


class TestSafeUnlink:
    """임시 파일 안전 삭제."""

    @patch("os.path.exists", return_value=True)
    @patch("os.unlink")
    def test_deletes_existing_file(self, mock_unlink, mock_exists):
        from tools.owl_reasoner import _safe_unlink

        _safe_unlink("/tmp/test.owl")
        mock_unlink.assert_called_once_with("/tmp/test.owl")

    @patch("os.path.exists", return_value=False)
    @patch("os.unlink")
    def test_skips_nonexistent_file(self, mock_unlink, mock_exists):
        from tools.owl_reasoner import _safe_unlink

        _safe_unlink("/tmp/nonexistent.owl")
        mock_unlink.assert_not_called()

    def test_none_path(self):
        from tools.owl_reasoner import _safe_unlink

        # None이면 삭제하지 않아야 함
        _safe_unlink(None)  # should not raise

    @patch("os.path.exists", return_value=True)
    @patch("os.unlink", side_effect=PermissionError("file locked"))
    def test_permission_error_ignored(self, mock_unlink, mock_exists):
        """Windows에서 Java가 파일을 잡고 있으면 무시."""
        from tools.owl_reasoner import _safe_unlink

        _safe_unlink("/tmp/locked.owl")  # should not raise


# ── _cleanup_world ────────────────────────────────


class TestCleanupWorld:
    """owlready2 world 초기화."""

    @patch("tools.owl_reasoner.default_world")
    def test_cleanup_destroys_ontologies(self, mock_world):
        from tools.owl_reasoner import _cleanup_world

        mock_onto = MagicMock()
        mock_world.ontologies = {"test": mock_onto}

        _cleanup_world()
        mock_onto.destroy.assert_called_once()

    @patch("tools.owl_reasoner.default_world")
    def test_cleanup_handles_destroy_error(self, mock_world):
        """destroy 실패 시에도 계속 진행."""
        from tools.owl_reasoner import _cleanup_world

        mock_onto = MagicMock()
        mock_onto.destroy.side_effect = Exception("destroy failed")
        mock_world.ontologies = {"test": mock_onto}

        _cleanup_world()  # should not raise


# ── validate_owl_realisation opt-in ─────────────────


class TestValidateOwlRealisationOptIn:
    """Pellet 실현 검증은 명시적으로 활성화한 경우에만 실행한다."""

    def test_default_skips_before_loading_ttl(self, monkeypatch):
        """기본 경로에서 TTL 파싱과 Pellet JVM 호출을 모두 건너뛴다."""
        from tools import owl_reasoner

        monkeypatch.delenv("PELLET_REALISATION_ENABLED", raising=False)

        def fail_if_called(*_args, **_kwargs):
            raise AssertionError("기본 경로에서 Pellet 준비가 시작됐다")

        monkeypatch.setattr(owl_reasoner, "_load_ttl_content", fail_if_called)
        monkeypatch.setattr(owl_reasoner, "sync_reasoner_pellet", fail_if_called)

        result = json.loads(owl_reasoner.validate_owl_realisation())

        assert result["success"] is True
        assert result["skipped"] is True
        assert result["reasoner"] == "Pellet"
        assert "PELLET_REALISATION_ENABLED" in result["reason"]

    def test_explicit_true_runs_pellet(self, monkeypatch):
        """환경변수를 true로 설정하면 기존 Pellet 실현 경로를 실행한다."""
        from tools import owl_reasoner

        monkeypatch.setenv("PELLET_REALISATION_ENABLED", "true")
        monkeypatch.setattr(owl_reasoner, "_cleanup_world", lambda: None)
        monkeypatch.setattr(
            owl_reasoner,
            "_load_ttl_content",
            lambda *_args, **_kwargs: "ttl",
        )
        ontology = MagicMock()
        ontology.individuals.return_value = []
        monkeypatch.setattr(
            owl_reasoner,
            "_ttl_to_owlready",
            lambda _ttl: (ontology, None),
        )
        monkeypatch.setattr(
            owl_reasoner,
            "_collect_unsatisfiable",
            lambda _ontology: [],
        )
        called = []

        def record_pellet_call(**kwargs):
            called.append(kwargs)

        monkeypatch.setattr(
            owl_reasoner,
            "sync_reasoner_pellet",
            record_pellet_call,
        )

        result = json.loads(
            owl_reasoner.validate_owl_realisation(ttl_content="ttl")
        )

        assert result["success"] is True
        assert result["skipped"] is False
        assert result["reasoner"] == "Pellet"
        assert called == [
            {
                "infer_property_values": True,
                "infer_data_property_values": True,
            }
        ]


# ── _collect_unsatisfiable ────────────────────────


class TestCollectUnsatisfiable:
    """unsatisfiable class 수집."""

    @patch("tools.owl_reasoner.default_world")
    def test_empty_when_no_unsatisfiable(self, mock_world):
        from tools.owl_reasoner import _collect_unsatisfiable

        mock_world.inconsistent_classes.return_value = []
        result = _collect_unsatisfiable(MagicMock())
        assert result == []

    @patch("tools.owl_reasoner.default_world")
    def test_collects_unsatisfiable_classes(self, mock_world):
        from tools.owl_reasoner import Nothing, _collect_unsatisfiable

        mock_cls = MagicMock()
        mock_cls.iri = "http://example.org/BadClass"
        mock_cls.name = "BadClass"

        mock_world.inconsistent_classes.return_value = [Nothing, mock_cls]
        result = _collect_unsatisfiable(MagicMock())
        # Nothing은 필터링됨
        assert len(result) == 1
        assert result[0]["name"] == "BadClass"

    @patch("tools.owl_reasoner.default_world")
    def test_handles_exception(self, mock_world):
        from tools.owl_reasoner import _collect_unsatisfiable

        mock_world.inconsistent_classes.side_effect = Exception("error")
        result = _collect_unsatisfiable(MagicMock())
        assert result == []


# ── validate_owl_consistency ──────────────────────


class TestValidateOwlConsistency:
    """HermiT 일관성 검증 MCP 도구."""

    @patch("tools.owl_reasoner._cleanup_world")
    @patch("tools.owl_reasoner._safe_unlink")
    @patch("tools.owl_reasoner._ttl_to_owlready")
    @patch("tools.owl_reasoner.sync_reasoner_hermit")
    @patch("tools.owl_reasoner._collect_unsatisfiable", return_value=[])
    @patch("tools.owl_reasoner._collect_inferred_hierarchy", return_value=[])
    @patch("tools.owl_reasoner._load_ttl_content")
    def test_consistent_ontology(
        self, mock_load, mock_hierarchy, mock_unsat,
        mock_reasoner, mock_ttl_to_owl, mock_unlink, mock_cleanup,
    ):
        from tools.owl_reasoner import validate_owl_consistency

        mock_load.return_value = "@prefix owl: <http://www.w3.org/2002/07/owl#> ."

        # owlready2 모킹
        mock_onto = MagicMock()
        mock_onto.classes.return_value = []
        mock_onto.object_properties.return_value = []
        mock_onto.data_properties.return_value = []
        mock_ttl_to_owl.return_value = (mock_onto, "/tmp/test.owl")

        result = json.loads(validate_owl_consistency(ttl_content="test"))
        assert result["consistent"] is True
        assert result["reasoner"] == "HermiT"
        assert result["unsatisfiable_count"] == 0

    @patch("tools.owl_reasoner._cleanup_world")
    @patch("tools.owl_reasoner._safe_unlink")
    @patch("tools.owl_reasoner._ttl_to_owlready")
    @patch("tools.owl_reasoner.sync_reasoner_hermit")
    @patch("tools.owl_reasoner._collect_unsatisfiable", return_value=[])
    @patch("tools.owl_reasoner._collect_inferred_hierarchy", return_value=[])
    @patch("tools.owl_reasoner._load_ttl_content")
    def test_inconsistent_ontology(
        self, mock_load, mock_hierarchy, mock_unsat,
        mock_reasoner, mock_ttl_to_owl, mock_unlink, mock_cleanup,
    ):
        from owlready2 import OwlReadyInconsistentOntologyError

        from tools.owl_reasoner import validate_owl_consistency

        mock_load.return_value = "test"
        mock_onto = MagicMock()
        mock_onto.classes.return_value = []
        mock_onto.object_properties.return_value = []
        mock_onto.data_properties.return_value = []
        mock_ttl_to_owl.return_value = (mock_onto, "/tmp/test.owl")
        mock_reasoner.side_effect = OwlReadyInconsistentOntologyError()

        result = json.loads(validate_owl_consistency(ttl_content="test"))
        assert result["consistent"] is False
        assert "hint" in result

    @patch("tools.owl_reasoner._cleanup_world")
    @patch("tools.owl_reasoner._load_ttl_content")
    def test_java_not_found_error(self, mock_load, mock_cleanup):
        """Java 미설치 시 에러 응답."""
        from tools.owl_reasoner import validate_owl_consistency

        mock_load.side_effect = FileNotFoundError("T-Box not found")

        result = json.loads(validate_owl_consistency())
        assert result["success"] is False
        assert "error" in result

    @patch("tools.owl_reasoner._cleanup_world")
    @patch("tools.owl_reasoner._safe_unlink")
    @patch("tools.owl_reasoner._ttl_to_owlready")
    @patch("tools.owl_reasoner.sync_reasoner_hermit")
    @patch("tools.owl_reasoner._collect_unsatisfiable", return_value=[])
    @patch("tools.owl_reasoner._collect_inferred_hierarchy", return_value=[])
    @patch("tools.owl_reasoner._load_ttl_content")
    def test_metaclass_conflict_falls_back_to_no_property_inference(
        self, mock_load, mock_hierarchy, mock_unsat,
        mock_reasoner, mock_ttl_to_owl, mock_unlink, mock_cleanup,
    ):
        """owlready2 post-merge 단계 metaclass conflict 감지 + fallback.

        실제 스택트레이스에 `_apply_reasoning_results` 프레임이 있어야 post-merge
        bug 로 판정되므로, 테스트에서도 해당 프레임을 통과하는 함수를 만든다.
        """
        from tools.owl_reasoner import clear_hermit_cache, validate_owl_consistency
        clear_hermit_cache()

        mock_load.return_value = "@prefix owl: <http://www.w3.org/2002/07/owl#> ."
        mock_onto = MagicMock()
        mock_onto.classes.return_value = []
        mock_onto.object_properties.return_value = []
        mock_onto.data_properties.return_value = []
        mock_ttl_to_owl.return_value = (mock_onto, "/tmp/test.owl")

        # _is_post_merge_bug 는 traceback 에서 '_apply_reasoning_results' 문자열을 찾는다.
        # 이 프레임명이 traceback 에 등장하도록 함수 정의.
        def _apply_reasoning_results():
            raise TypeError("metaclass conflict: ...")

        call_state = {"n": 0}
        def _wrapper(*args, **kwargs):
            call_state["n"] += 1
            if call_state["n"] == 1:
                _apply_reasoning_results()
            return None
        mock_reasoner.side_effect = _wrapper

        result = json.loads(validate_owl_consistency(ttl_content="test"))
        assert result["success"] is True, f"result: {result}"
        assert result["consistent"] is True
        assert result["property_value_inference_degraded"] is True
        assert "hint_degraded" in result
        assert call_state["n"] == 2
        second_call_kwargs = mock_reasoner.call_args_list[1].kwargs
        assert second_call_kwargs.get("infer_property_values") is False

    @patch("tools.owl_reasoner._cleanup_world")
    @patch("tools.owl_reasoner._safe_unlink")
    @patch("tools.owl_reasoner._ttl_to_owlready")
    @patch("tools.owl_reasoner.sync_reasoner_hermit")
    @patch("tools.owl_reasoner._collect_unsatisfiable", return_value=[])
    @patch("tools.owl_reasoner._collect_inferred_hierarchy", return_value=[])
    @patch("tools.owl_reasoner._load_ttl_content")
    def test_degraded_flag_false_when_first_call_succeeds(
        self, mock_load, mock_hierarchy, mock_unsat,
        mock_reasoner, mock_ttl_to_owl, mock_unlink, mock_cleanup,
    ):
        """정상 경로에서는 property_value_inference_degraded=False + reasoner_post_merge_failed=False."""
        from tools.owl_reasoner import clear_hermit_cache, validate_owl_consistency
        clear_hermit_cache()

        mock_load.return_value = "test_degraded_false"
        mock_onto = MagicMock()
        mock_onto.classes.return_value = []
        mock_onto.object_properties.return_value = []
        mock_onto.data_properties.return_value = []
        mock_ttl_to_owl.return_value = (mock_onto, "/tmp/test.owl")

        result = json.loads(validate_owl_consistency(ttl_content="test"))
        assert result["property_value_inference_degraded"] is False
        assert result["reasoner_post_merge_failed"] is False
        assert "hint_degraded" not in result
        assert mock_reasoner.call_count == 1

    @patch("tools.owl_reasoner._cleanup_world")
    @patch("tools.owl_reasoner._safe_unlink")
    @patch("tools.owl_reasoner._ttl_to_owlready")
    @patch("tools.owl_reasoner.sync_reasoner_hermit")
    @patch("tools.owl_reasoner._collect_unsatisfiable", return_value=[])
    @patch("tools.owl_reasoner._collect_inferred_hierarchy", return_value=[])
    @patch("tools.owl_reasoner._load_ttl_content")
    def test_post_merge_bug_on_both_calls_marks_fully_degraded(
        self, mock_load, mock_hierarchy, mock_unsat,
        mock_reasoner, mock_ttl_to_owl, mock_unlink, mock_cleanup,
    ):
        """재시도도 post-merge 에러면 success=True + reasoner_post_merge_failed=True.

        실측 회귀: AttributeError 'X object has no attribute equivalent_to' 또는
        TypeError metaclass conflict 가 infer_property_values=False 에서도 동일하게
        나는 상황. 사용자가 "아무것도 안 보였다" 가 아니라 degraded 결과를 받도록.
        """
        from tools.owl_reasoner import clear_hermit_cache, validate_owl_consistency
        clear_hermit_cache()

        mock_load.return_value = "test_both_fail"
        mock_onto = MagicMock()
        mock_onto.classes.return_value = []
        mock_onto.object_properties.return_value = []
        mock_onto.data_properties.return_value = []
        mock_ttl_to_owl.return_value = (mock_onto, "/tmp/test.owl")

        def _apply_reasoning_results():
            raise AttributeError("'X' object has no attribute 'equivalent_to'")
        def _wrapper(*a, **kw):
            _apply_reasoning_results()
        mock_reasoner.side_effect = _wrapper

        result = json.loads(validate_owl_consistency(ttl_content="test_both_fail"))
        assert result["success"] is True
        assert result["property_value_inference_degraded"] is True
        assert result["reasoner_post_merge_failed"] is True
        assert "hint_degraded" in result
        assert mock_reasoner.call_count == 2


# ── classify_tbox ─────────────────────────────────


class TestClassifyTbox:
    """HermiT classification."""

    @patch("tools.owl_reasoner._cleanup_world")
    @patch("tools.owl_reasoner._safe_unlink")
    @patch("tools.owl_reasoner._ttl_to_owlready")
    @patch("tools.owl_reasoner.sync_reasoner_hermit")
    @patch("tools.owl_reasoner._collect_unsatisfiable", return_value=[])
    @patch("tools.owl_reasoner._load_ttl_content")
    def test_classification(
        self, mock_load, mock_unsat, mock_reasoner,
        mock_ttl_to_owl, mock_unlink, mock_cleanup,
    ):
        from domain.namespaces import DOMAIN_NS
        from tools.owl_reasoner import classify_tbox

        mock_load.return_value = "test"

        # 클래스 모킹 (추론 전/후 동일한 계층)
        mock_cls = MagicMock()
        mock_cls.iri = f"{DOMAIN_NS}EquipmentMaster"
        mock_cls.name = "EquipmentMaster"

        mock_parent = MagicMock()
        mock_parent.iri = f"{DOMAIN_NS}Thing"
        mock_parent.name = "Thing"
        mock_cls.is_a = [mock_parent]

        mock_onto = MagicMock()
        mock_onto.classes.return_value = [mock_cls]
        mock_ttl_to_owl.return_value = (mock_onto, "/tmp/test.owl")

        result = json.loads(classify_tbox(ttl_content="test"))
        assert result["reasoner"] == "HermiT"
        assert "explicit_hierarchy" in result
        assert "inferred_hierarchy" in result

    @patch("tools.owl_reasoner._cleanup_world")
    @patch("tools.owl_reasoner._safe_unlink")
    @patch("tools.owl_reasoner._ttl_to_owlready")
    @patch("tools.owl_reasoner.sync_reasoner_hermit")
    @patch("tools.owl_reasoner._load_ttl_content")
    def test_inconsistent_classification_error(
        self, mock_load, mock_reasoner, mock_ttl_to_owl,
        mock_unlink, mock_cleanup,
    ):
        """비일관 온톨로지 classification 시 에러."""
        from owlready2 import OwlReadyInconsistentOntologyError

        from tools.owl_reasoner import classify_tbox

        mock_load.return_value = "test"
        mock_onto = MagicMock()
        mock_onto.classes.return_value = []
        mock_ttl_to_owl.return_value = (mock_onto, "/tmp/test.owl")
        mock_reasoner.side_effect = OwlReadyInconsistentOntologyError()

        result = json.loads(classify_tbox(ttl_content="test"))
        assert result["success"] is False
        assert "error" in result


# ── hermit_consistency_check (내부 헬퍼) ──────────


class TestHermitConsistencyCheck:
    """내부 API: 생성 루프 통합용."""

    @patch("tools.owl_reasoner._cleanup_world")
    @patch("tools.owl_reasoner._safe_unlink")
    @patch("tools.owl_reasoner._ttl_to_owlready")
    @patch("tools.owl_reasoner.sync_reasoner_hermit")
    @patch("tools.owl_reasoner._collect_unsatisfiable", return_value=[])
    def test_consistent_returns_dict(
        self, mock_unsat, mock_reasoner, mock_ttl_to_owl,
        mock_unlink, mock_cleanup,
    ):
        from tools.owl_reasoner import hermit_consistency_check

        mock_onto = MagicMock()
        mock_ttl_to_owl.return_value = (mock_onto, "/tmp/test.owl")

        result = hermit_consistency_check("test ttl")
        assert result["consistent"] is True
        assert result["error"] is None

    @patch("tools.owl_reasoner._cleanup_world")
    @patch("tools.owl_reasoner._safe_unlink")
    @patch("tools.owl_reasoner._ttl_to_owlready")
    def test_exception_returns_error(self, mock_ttl_to_owl, mock_unlink, mock_cleanup):
        from tools.owl_reasoner import hermit_consistency_check

        mock_ttl_to_owl.side_effect = Exception("parse error")

        result = hermit_consistency_check("bad ttl")
        assert result["consistent"] is None
        assert "parse error" in result["error"]
