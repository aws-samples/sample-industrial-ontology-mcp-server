"""R10 end-to-end smoke test: runs mutation stages against the live T-Box.

Marked slow; run via `pytest -m slow tests/test_r10_smoke_e2e.py`.
"""
import os

import pytest

from config import TBOX_PATH

pytestmark = pytest.mark.slow


def test_s4_5_then_meta_audit_surfaces_m5_blind_spot(tmp_path):
    if not os.path.exists(TBOX_PATH):
        pytest.skip("No live T-Box present in this checkout")

    from tools.meta_audit import run_meta_audit_impl
    from tools.mutation_runner import run_tbox_mutations

    runs_dir = tmp_path / "meta_audit" / "runs" / "smoke"
    run_tbox_mutations(TBOX_PATH, out_dir=str(runs_dir))
    assert (runs_dir / "tbox.json").exists()

    history_path = tmp_path / "quality_history.json"
    history_path.write_text("[]")
    audit_path = tmp_path / "meta_audit" / "audit.json"
    result = run_meta_audit_impl(
        runs_root=str(tmp_path / "meta_audit" / "runs"),
        history_path=str(history_path),
        out_path=str(audit_path),
    )
    # Expected: M5 annotation mutants exist in catalog and at least one uncaught
    m5_blind = [s for s in result["blind_spots"] if s["category"] == "M5"]
    assert m5_blind, (
        "expected M5 blind-spot as per spec section 6 success criterion 2 -- "
        f"observed blind_spots: {result['blind_spots']}"
    )
