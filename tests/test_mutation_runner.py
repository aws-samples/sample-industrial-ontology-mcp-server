import json

import pytest
from rdflib import Graph
from rdflib.namespace import RDFS

from tools.mutation_runner import apply_mutator, discover_mutators, resolve_targets


def test_catalog_discovers_all_mutators():
    mutators = discover_mutators("rules/mutations")
    names = sorted(m.name for m in mutators)
    assert "M1_domain_range/delete_domain" in names
    assert "M1_domain_range/swap_domain" in names
    assert "M1_domain_range/drop_union_member" in names
    assert all(m.category == "M1" for m in mutators if m.name.startswith("M1_"))


def test_resolve_targets_picks_properties_by_sorted_uri():
    g = Graph().parse("tests/fixtures/mutation_minimal_tbox.ttl", format="turtle")
    targets = resolve_targets(g, "?TARGET_PROP", limit=3)
    uris = [str(t) for t in targets]
    assert uris == sorted(uris)  # deterministic
    assert len(targets) <= 3
    assert any("hasPlant" in u or "serialNumber" in u for u in uris)


def test_apply_delete_domain_removes_triple():
    g = Graph().parse("tests/fixtures/mutation_minimal_tbox.ttl", format="turtle")
    muts = [m for m in discover_mutators("rules/mutations") if m.name.endswith("delete_domain")]
    assert len(muts) == 1
    triples_before = len(list(g.triples((None, RDFS.domain, None))))
    mutated, info = apply_mutator(g, muts[0])
    triples_after = len(list(mutated.triples((None, RDFS.domain, None))))
    assert triples_after == triples_before - 1
    assert info["applied"] is True
    assert info["target"]  # not empty
    assert info["triples_removed"] == 1


def test_apply_mutator_no_effect_is_applied_false():
    """Regression: SPARQL that resolves placeholders but changes zero triples
    must report applied=False with reason=no_effect, not a false-positive True.

    Rationale: the sensitivity matrix uses `applied` as the denominator.
    An applied=True with zero changes would silently inflate catch-rate calculations.
    """
    from rdflib import Graph

    from tools.mutation_runner import Mutator, apply_mutator

    # Construct a graph where ex:TheProp exists but has NO rdfs:domain triple.
    g = Graph()
    g.parse(data="""
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix ex:  <http://example.org/#> .
        ex:TheProp a owl:ObjectProperty .
    """, format="turtle")

    # Synthesize a mutator that would only delete something if a domain existed.
    m = Mutator(
        name="M_test/noop_delete",
        category="M",
        path="/synthetic",
        sparql="""
            PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
            DELETE { ?TARGET_PROP rdfs:domain ?dom }
            WHERE  { ?TARGET_PROP rdfs:domain ?dom }
        """,
    )
    _, info = apply_mutator(g, m)
    assert info["applied"] is False
    assert info["reason"] == "no_effect"
    assert info["triples_removed"] == 0
    assert info["triples_added"] == 0
    # target is still recorded for debugging
    assert info["target"]


def test_tbox_validator_keys_are_short_aliases():
    """Spec requires short alias check names (syntax/quality/hermit/classify/shacl)
    so 관련 단계 sensitivity matrix + 관련 단계 report render consistently.
    Regression guard: renaming registry keys back to MCP tool names would break
    every downstream consumer."""
    from tools.mutation_runner import _run_tbox_validators
    result = _run_tbox_validators("tests/fixtures/mutation_minimal_tbox.ttl")
    assert set(result.keys()) == {"syntax", "quality", "hermit", "classify", "shacl"}


def test_run_tbox_mutations_produces_delta_json(tmp_path):
    from tools.mutation_runner import run_tbox_mutations

    tbox = "tests/fixtures/mutation_minimal_tbox.ttl"
    out_dir = tmp_path / "run"
    result = run_tbox_mutations(
        tbox_path=tbox,
        catalog_dir="rules/mutations",
        out_dir=str(out_dir),
    )
    assert result["total_mutants"] >= 3  # at least M1 * 3
    assert result["applied"] >= 1
    assert "mutants" in result
    # Find at least one mutant that was applied (applied=True) and inspect it.
    applied_mutants = [m for m in result["mutants"] if m.get("applied")]
    assert applied_mutants, "expected at least one applied mutant"
    m0 = applied_mutants[0]
    assert "baseline_summary" in m0 and "mutant_summary" in m0
    assert "caught_by" in m0 and isinstance(m0["caught_by"], list)
    assert (out_dir / "tbox.json").exists()
    written = json.loads((out_dir / "tbox.json").read_text())
    assert written["total_mutants"] == result["total_mutants"]


@pytest.mark.slow
def test_run_kg_mutations_samples_and_writes_kg_json(tmp_path):
    """S9.5 variant: T-Box mutation + A-Box reuse + validate_kg rerun.

    Uses an empty A-Box so the 25 checks run quickly; baseline vs mutant
    deltas may be trivial (PASS→PASS) but the summary schema must still be
    populated and kg.json must be written.
    """
    from pathlib import Path

    from tools.mutation_runner import run_kg_mutations

    tbox = "tests/fixtures/mutation_minimal_tbox.ttl"
    abox = str(tmp_path / "empty_abox.ttl")
    Path(abox).write_text("")  # empty A-Box — KG checks run, most return PASS trivially
    out_dir = tmp_path / "run"
    result = run_kg_mutations(
        tbox_path=tbox,
        abox_path=abox,
        sample_size=4,  # smaller for test speed
        out_dir=str(out_dir),
    )
    assert result["sample_size"] <= 4
    assert len(result["mutants"]) <= 4
    assert (out_dir / "kg.json").exists()
    # Summary schema sanity check.
    written = json.loads((out_dir / "kg.json").read_text())
    assert written["tbox_path"] == tbox
    assert written["abox_path"] == abox
    assert "duration_s" in written
    assert "mutants" in written and isinstance(written["mutants"], list)


def test_kg_sample_categories_are_pinned():
    """S9.5 표본 카테고리를 고정한다 — 카탈로그가 커져도 조용히 따라가지 않는다.

    ## 2026-09-05: M4 / M7 추가 (예전 기대값의 전제가 반박됐다)

    예전 이 테스트는 M7 제외를 주장하며 근거를 둘 들었다. 둘 다 실측으로 무너졌다:

    1. "HermiT timeout" — S9.5 는 HermiT 을 **돌리지 않는다** (validate_kg 만 돈다).
       게다가 카테고리가 늘면 ``per_cat = sample_size // len(_KG_CATEGORIES)`` 때문에
       기본 ``sample_size=8`` 에서 표본이 8 → 6 으로 **줄어든다**. 비용 근거가 아니다.
    2. "validate_owl_consistency 가 S4.5 에서 이미 커버한다" — 2026-09-05 S4.5 실측에서
       ``M7/some_to_all`` 은 ``hermit`` PASS → PASS 로 **잡히지 않았다**.

    그 사이 구조적 결과가 관측됐다: ``some_to_all`` 은 someValuesFrom 40건을 전부
    allValuesFrom 으로 바꿔 ``필수참여 공리 충족`` 을 판정 불가로 만드는데, 그 mutant 를
    적용하는 단계(S4.5)는 validate_kg 를 돌리지 않는다 — **KG 체크를 눈멀게 하는 변조가
    KG 체크를 돌리는 단계에서 한 번도 적용되지 않았다** (meta_audit 15 runs 에서
    ``some_to_all`` 의 seen_stages = ['S4.5'] 뿐).

    판정 기준은 "validate_kg 에 그 축을 보는 체크가 있는가" 다:
      M4 → ``AllDisjointClasses 위반`` + 계층을 읽는 체크들
      M7 → ``필수참여 공리 충족`` / ``카디널리티 제약 위반``

    M5(annotation) 는 제외를 유지한다 — validate_kg 에 라벨/주석 축이 없다.
    """
    from tools.mutation_runner import _KG_CATEGORIES

    assert set(_KG_CATEGORIES) == {"M1", "M2", "M3", "M4", "M6", "M7"}
    assert "M5" not in _KG_CATEGORIES, (
        "M5(annotation) 는 validate_kg 에 대응 축이 없다 — 넣으면 노출만 늘고 "
        "판정은 불가하다"
    )


def test_catalog_has_21_mutators_across_7_categories():
    muts = discover_mutators("rules/mutations")
    assert len(muts) == 21, f"expected 21, got {len(muts)}"
    cats = {m.category for m in muts}
    assert cats == {"M1", "M2", "M3", "M4", "M5", "M6", "M7"}
    for cat in cats:
        cat_muts = [m for m in muts if m.category == cat]
        assert len(cat_muts) == 3, f"category {cat} has {len(cat_muts)} (expected 3)"


def test_run_tbox_mutations_is_byte_equal_twice(tmp_path):
    """Spec §6.5: same T-Box + same catalog → byte-equal tbox.json across runs.

    Protects against non-deterministic iteration order in placeholder resolution,
    glob enumeration, or dict ordering leaking into the serialized artifact.
    """
    from tools.mutation_runner import run_tbox_mutations
    tbox = "tests/fixtures/mutation_minimal_tbox.ttl"
    out1 = tmp_path / "run1"
    out2 = tmp_path / "run2"
    run_tbox_mutations(tbox, out_dir=str(out1))
    run_tbox_mutations(tbox, out_dir=str(out2))
    a = json.loads((out1 / "tbox.json").read_text())
    b = json.loads((out2 / "tbox.json").read_text())
    # Drop timestamp + duration (non-deterministic by design)
    for d in (a, b):
        d.pop("timestamp", None)
        d.pop("duration_s", None)
        for m in d.get("mutants", []):
            m.pop("duration_s", None)
    assert a == b, "non-determinism detected in mutation runner output"
