import json

from tools.meta_audit import (
    compute_blind_spots,
    compute_dead_checks,
    compute_pairwise_correlation,
    compute_sensitivity_matrix,
    run_meta_audit_impl,
)


def _fake_run(mutants):
    return {"mutants": mutants}


def test_sensitivity_matrix_counts_per_category():
    runs = [_fake_run([
        {"mutant_id": "M1_.../delete_domain", "category": "M1", "applied": True,
         "caught_by": ["domain_range_conformance"]},
        {"mutant_id": "M1_.../swap_domain", "category": "M1", "applied": True,
         "caught_by": ["domain_range_conformance", "shacl"]},
        {"mutant_id": "M5_.../delete_label", "category": "M5", "applied": True,
         "caught_by": []},
    ])]
    matrix = compute_sensitivity_matrix(runs)
    assert matrix["domain_range_conformance"]["M1"] == 1.0
    assert matrix["shacl"]["M1"] == 0.5
    for _check, row in matrix.items():
        assert row.get("M5", 0.0) == 0.0


def test_blind_spots_detects_uncaught_category():
    runs = [_fake_run([
        {"mutant_id": "M5_a", "category": "M5", "applied": True, "caught_by": []},
        {"mutant_id": "M5_b", "category": "M5", "applied": True, "caught_by": []},
        {"mutant_id": "M1_a", "category": "M1", "applied": True,
         "caught_by": ["domain_range_conformance"]},
    ])]
    spots = compute_blind_spots(runs)
    m5 = [s for s in spots if s["category"] == "M5"]
    assert len(m5) == 1
    assert m5[0]["mutants"] == 2
    assert m5[0]["caught_by_any_check"] == 0
    assert not any(s["category"] == "M1" for s in spots)


def test_pairwise_correlation_flags_coocurring_checks():
    history = [
        {"failed_checks": ["A", "B"]},
        {"failed_checks": ["A", "B"]},
        {"failed_checks": ["A", "B"]},
        {"failed_checks": ["A", "B"]},
        {"failed_checks": ["A", "B", "C"]},
    ]
    runs = []
    corr = compute_pairwise_correlation(runs, history, min_samples=4)
    pair = next((p for p in corr if set(p["pair"]) == {"A", "B"}), None)
    assert pair is not None
    assert pair["cooccurrence"] >= 0.9
    assert not any(set(p["pair"]) == {"A", "C"} for p in corr)


def test_pairwise_correlation_distinguishes_history_and_mutation_sources():
    """Pair supported by history ≥ min_samples → redundant_candidate (strong).
    Pair supported only by mutation runs → cooccurrence_candidate (weaker).
    Signal sources must not be mixed into a single counter."""
    # Mutation-only signal: A+B co-occur in 5 mutant catches, zero history
    runs = [_fake_run([
        {"applied": True, "caught_by": ["A", "B"]},
        {"applied": True, "caught_by": ["A", "B"]},
        {"applied": True, "caught_by": ["A", "B"]},
        {"applied": True, "caught_by": ["A", "B"]},
        {"applied": True, "caught_by": ["A", "B"]},
    ])]
    corr = compute_pairwise_correlation(runs, [], min_samples=5)
    ab = next((p for p in corr if set(p["pair"]) == {"A", "B"}), None)
    assert ab is not None
    assert ab["source"] == "mutation"
    assert ab["interpretation"] == "cooccurrence_candidate"

    # History signal only: C+D co-occur in 5 history entries, zero mutations
    history2 = [{"failed_checks": ["C", "D"]} for _ in range(5)]
    corr2 = compute_pairwise_correlation([], history2, min_samples=5)
    cd = next((p for p in corr2 if set(p["pair"]) == {"C", "D"}), None)
    assert cd is not None
    assert cd["source"] == "history"
    assert cd["interpretation"] == "redundant_candidate"


def test_dead_checks_zero_history_zero_mutation():
    history = [{"failed_checks": ["A"]}]
    runs = [{"mutants": [{"applied": True, "caught_by": ["A"]}]}]
    checks_seen = {"A", "B"}
    dead = compute_dead_checks(runs, history, checks_seen)
    names = [d["check"] for d in dead]
    assert "B" in names
    assert "A" not in names


def test_run_meta_audit_impl_produces_complete_artifact(tmp_path):
    runs_dir = tmp_path / "runs" / "20260427T120000Z"
    runs_dir.mkdir(parents=True)
    (runs_dir / "tbox.json").write_text(json.dumps({
        "mutants": [
            {"mutant_id": "M1_a", "category": "M1", "applied": True,
             "caught_by": ["domain_range_conformance"],
             "baseline_summary": {"syntax": "PASS", "quality": "PASS"},
             "mutant_summary":  {"syntax": "PASS", "quality": "PASS"},
             "duration_s": 1.5},
            {"mutant_id": "M5_a", "category": "M5", "applied": True,
             "caught_by": [],
             "baseline_summary": {"syntax": "PASS"},
             "mutant_summary":  {"syntax": "PASS"},
             "duration_s": 0.5},
        ]
    }))
    out = tmp_path / "audit.json"
    history_path = tmp_path / "quality_history.json"
    history_path.write_text(json.dumps([
        {"failed_checks": []},
    ]))

    run_meta_audit_impl(
        runs_root=str(tmp_path / "runs"),
        history_path=str(history_path),
        out_path=str(out),
        window_commits=30,
    )
    assert out.exists()
    written = json.loads(out.read_text())
    assert "sensitivity_matrix" in written
    assert "blind_spots" in written
    assert "pairwise_correlation" in written
    assert "dead_checks" in written
    assert "roi_column" in written
    assert any(s["category"] == "M5" for s in written["blind_spots"])
