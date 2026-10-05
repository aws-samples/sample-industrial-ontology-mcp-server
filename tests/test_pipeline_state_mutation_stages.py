"""R10 — verify S4.5/S9.5 mutation stages are registered in pipeline_state."""
from __future__ import annotations

from tools.pipeline_state import _ALL_STEPS, _STEP_DEPS


def test_s4_5_and_s9_5_registered():
    assert "S4_5_MUTATION" in _STEP_DEPS
    assert "S9_5_KG_MUTATION" in _STEP_DEPS
    assert "tbox_mtime" in _STEP_DEPS["S4_5_MUTATION"]
    assert "inferred_mtime" in _STEP_DEPS["S9_5_KG_MUTATION"]
    assert _ALL_STEPS.index("S4_5_MUTATION") > _ALL_STEPS.index("S4_VALIDATE")
    assert _ALL_STEPS.index("S4_5_MUTATION") < _ALL_STEPS.index("S5_TACIT")
    assert _ALL_STEPS.index("S9_5_KG_MUTATION") > _ALL_STEPS.index("S9_KG_VALIDATE")
    assert _ALL_STEPS.index("S9_5_KG_MUTATION") < _ALL_STEPS.index("S10_DICT")
