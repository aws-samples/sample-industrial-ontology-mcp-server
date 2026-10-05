"""R22: pipeline_state 에 S9_OWL_SANITY / S9_POST_MEASURE 등록 회귀 테스트.

validate_kg 가 놓치는 HermiT consistency / OWL 2 profile / entailment 골든
회귀 를 잡기 위한 S9_OWL_SANITY, 그리고 measure_instance_quality + inferred
delta 를 quality_history 에 trend 로 기록하는 S9_POST_MEASURE 가 파이프라인
선언에 포함돼 있어야 한다.
"""
from __future__ import annotations

import pytest

from tools.pipeline_state import _ALL_STEPS, _STEP_DEPS


class TestR22StagesRegistered:
    def test_owl_sanity_registered(self):
        assert "S9_OWL_SANITY" in _STEP_DEPS
        assert "S9_OWL_SANITY" in _ALL_STEPS

    def test_post_measure_registered(self):
        assert "S9_POST_MEASURE" in _STEP_DEPS
        assert "S9_POST_MEASURE" in _ALL_STEPS

    def test_owl_sanity_depends_on_tbox_and_inferred(self):
        deps = _STEP_DEPS["S9_OWL_SANITY"]
        # HermiT / profile 모두 T-Box 기반, entailment 는 inferred
        assert "tbox_mtime" in deps
        assert "inferred_mtime" in deps

    def test_post_measure_depends_on_inferred(self):
        deps = _STEP_DEPS["S9_POST_MEASURE"]
        assert "inferred_mtime" in deps


class TestKgValidateDependsOnWhatItActuallyReads:
    """THE REGRESSION: S9 의 의존 축이 자기가 읽는 파일을 가리켜야 한다.

    ``validate_kg`` 기본 모드(``use_inferred=False``)의 ``load_graph`` 는 **T-Box + A-Box
    + tacit** 을 파싱하고 ``all_inferred.ttl`` 은 열지 않는다. 그런데 의존 축이
    ``["inferred_mtime"]`` 하나였다 — 읽는 파일은 안 보고 안 읽는 파일만 봤다.

    실측 피해: T-Box 만 바뀐 상태에서 ``skippable=True`` 로 보고돼, 세대가 어긋난 T-Box
    (배포 T-Box 가 A-Box·추론보다 3시간 뒤인 상태가 실제로 있었다) 로 검증을 건너뛸 수
    있었다.
    """

    def test_kg_validate_depends_on_its_own_inputs(self):
        deps = _STEP_DEPS["S9_KG_VALIDATE"]
        for axis in ("tbox_mtime", "abox_mtime", "tacit_mtime"):
            assert axis in deps, (
                f"{axis} 가 없다 — validate_kg 기본 모드가 그 파일을 파싱하는데 변경을 "
                f"감지하지 못한다 (현재 {deps})"
            )
        assert "inferred_mtime" in deps, "use_inferred=True 경로가 추론 갱신을 놓친다"

    def test_tbox_change_makes_kg_validate_rerun(self, tmp_path, monkeypatch):
        """PRESERVATION + 방향 검사: 저장 직후엔 skip 가능, T-Box 가 바뀌면 재실행."""
        import json
        import os
        import time

        import tools.pipeline_state as ps
        from config import TBOX_PATH

        if not os.path.exists(TBOX_PATH):
            pytest.skip("배포 T-Box 없음")

        monkeypatch.setattr(
            ps, "_STATE_PATH", str(tmp_path / "pipeline_state.json"), raising=False,
        )
        ps.save_step("S9_KG_VALIDATE", {"score": "23/25"})

        def _skippable() -> bool:
            steps = json.loads(ps.check_pipeline_state())["steps"]
            return next(s for s in steps if s["step"] == "S9_KG_VALIDATE")["skippable"]

        assert _skippable() is True, "입력 무변경인데 재실행을 요구했다 (과잉 반응)"

        # T-Box 만 미래 시각으로 — S2/S3 가 다시 쓴 상황.
        original = (os.path.getatime(TBOX_PATH), os.path.getmtime(TBOX_PATH))
        future = time.time() + 5
        os.utime(TBOX_PATH, (future, future))
        try:
            assert _skippable() is False, (
                "T-Box 가 바뀌었는데 skip 가능으로 보고했다 — 세대가 어긋난 T-Box 로 "
                "검증을 건너뛴다"
            )
        finally:
            os.utime(TBOX_PATH, original)


class TestR22StageOrdering:
    def test_new_stages_between_validate_and_mutation(self):
        """선언 순서: S9_KG_VALIDATE → S9_OWL_SANITY → S9_POST_MEASURE → S9_5_KG_MUTATION"""
        order = _ALL_STEPS
        i_validate = order.index("S9_KG_VALIDATE")
        i_sanity = order.index("S9_OWL_SANITY")
        i_measure = order.index("S9_POST_MEASURE")
        i_mutation = order.index("S9_5_KG_MUTATION")
        assert i_validate < i_sanity < i_measure < i_mutation, (
            f"순서 이상: validate={i_validate}, sanity={i_sanity}, "
            f"measure={i_measure}, mutation={i_mutation}"
        )
