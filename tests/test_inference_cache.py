"""S8 OWL RL 추론 결과 mtime 캐시 테스트."""
from __future__ import annotations

import json
import os

from tools.inference import (
    _inference_inputs_newer_than_output,
)
from tools.inference import (
    _run_owl_rl_inference_sync as run_owl_rl_inference,
)


def _touch(path: str, mtime_offset: float = 0) -> None:
    with open(path, "a", encoding="utf-8"):
        pass
    if mtime_offset:
        now = os.path.getmtime(path)
        os.utime(path, (now + mtime_offset, now + mtime_offset))


def test_helper_true_when_output_missing(tmp_path):
    from unittest.mock import patch
    with patch("tools.inference.INFERRED_PATH", str(tmp_path / "nope.ttl")):
        assert _inference_inputs_newer_than_output("", "") is True


def test_helper_true_when_input_newer_than_output(tmp_path):
    from unittest.mock import patch
    out = tmp_path / "all_inferred.ttl"
    tbox = tmp_path / "t_box.ttl"
    _touch(str(out), mtime_offset=-100)  # old output
    _touch(str(tbox), mtime_offset=+100)  # newer input
    with patch("tools.inference.INFERRED_PATH", str(out)), \
         patch("tools.inference.TBOX_PATH", str(tbox)), \
         patch("tools.inference.ABOX_PATH", str(tmp_path / "missing.ttl")), \
         patch("config.SOURCE_TACIT_DIR", str(tmp_path / "tacit_missing")):
        assert _inference_inputs_newer_than_output("", "") is True


def test_helper_false_when_all_inputs_older_than_output(tmp_path):
    from unittest.mock import patch
    tbox = tmp_path / "t_box.ttl"
    _touch(str(tbox), mtime_offset=-100)  # old input
    out = tmp_path / "all_inferred.ttl"
    _touch(str(out))  # fresh output
    with patch("tools.inference.INFERRED_PATH", str(out)), \
         patch("tools.inference.TBOX_PATH", str(tbox)), \
         patch("tools.inference.ABOX_PATH", str(tmp_path / "missing.ttl")), \
         patch("config.SOURCE_TACIT_DIR", str(tmp_path / "tacit_missing")):
        assert _inference_inputs_newer_than_output("", "") is False


def test_run_inference_uses_cache_when_fresh(tmp_path):
    """입력 mtime < 출력 mtime 이면 skip + 캐시 hit JSON 반환."""
    from unittest.mock import patch
    tbox = tmp_path / "t_box.ttl"
    abox = tmp_path / "a_box.ttl"
    out = tmp_path / "all_inferred.ttl"
    for p in (tbox, abox):
        _touch(str(p), mtime_offset=-200)
    _touch(str(out))  # fresh output

    with patch("tools.inference.INFERRED_PATH", str(out)), \
         patch("tools.inference.TBOX_PATH", str(tbox)), \
         patch("tools.inference.ABOX_PATH", str(abox)), \
         patch("config.SOURCE_TACIT_DIR", str(tmp_path / "tacit_missing")):
        result = json.loads(run_owl_rl_inference())
    assert result["success"] is True
    assert result.get("skipped") is True
    assert result["reason"] == "inference_cache_hit"


def test_run_inference_force_bypasses_cache(tmp_path):
    """force=True 면 캐시가 유효해도 실제 추론 경로 진입 (이후 _load_and_merge 에러로 빠져나옴)."""
    from unittest.mock import patch
    tbox = tmp_path / "t_box.ttl"
    out = tmp_path / "all_inferred.ttl"
    _touch(str(tbox), mtime_offset=-200)
    _touch(str(out))

    def _raise(*a, **kw):
        raise FileNotFoundError("forced path reached _load_and_merge")

    with patch("tools.inference.INFERRED_PATH", str(out)), \
         patch("tools.inference.TBOX_PATH", str(tbox)), \
         patch("tools.inference.ABOX_PATH", str(tmp_path / "missing.ttl")), \
         patch("config.SOURCE_TACIT_DIR", str(tmp_path / "tacit_missing")), \
         patch("tools.inference._load_and_merge", side_effect=_raise):
        result = json.loads(run_owl_rl_inference(force=True))
    # force=True 는 캐시 무시 → _load_and_merge 호출 경로로 진입 → 에러 반환
    assert result["success"] is False
