"""Tests for tools/validation_support/checks/temporal_cardinality.py — Session 14."""
from __future__ import annotations

import pytest
from rdflib import XSD, BNode, Literal, URIRef
from rdflib.collection import Collection
from rdflib.namespace import OWL, RDF, RDFS

from domain.namespaces import DOMAIN_INST_NS, DOMAIN_NS
from domain.tbox_utils import _new_graph
from tools.validation_support.checks.temporal_cardinality import (
    check_cardinality_constraints,
    check_disjoint_class_violations,
    check_functional_violations,
    check_inference_sanity,
    check_property_completeness,
)


def _cls(n):
    return URIRef(f"{DOMAIN_NS}{n}")


def _inst(n):
    return URIRef(f"{DOMAIN_INST_NS}{n}")


def test_inference_sanity_no_inferred_file(tmp_path):
    r = check_inference_sanity(_new_graph(), 100,
                                inferred_path=str(tmp_path / "nope.ttl"))
    assert r["passed"] is False


def test_inference_sanity_increase():
    g = _new_graph()
    for i in range(50):
        g.add((_inst(f"I{i}"), RDF.type, _cls("C")))
    r = check_inference_sanity(g, 10, inferred_path="/dev/null")
    assert r["passed"] is True
    assert r["increase"] > 0


def test_property_completeness_no_dp():
    r = check_property_completeness(_new_graph(), _new_graph())
    assert r["passed"] is True


def test_property_completeness_detects_gaps():
    g = _new_graph()
    tbox = _new_graph()
    cls = _cls("Eq")
    dp = _cls("dp")
    tbox.add((cls, RDF.type, OWL.Class))
    tbox.add((dp, RDF.type, OWL.DatatypeProperty))
    tbox.add((dp, RDFS.domain, cls))
    # 10 인스턴스, 2개만 dp 값 있음 → 20% 완전성
    for i in range(10):
        inst = _inst(f"E{i}")
        g.add((inst, RDF.type, cls))
        if i < 2:
            g.add((inst, dp, Literal(f"v{i}")))
    r = check_property_completeness(g, tbox)
    assert len(r["incomplete_properties"]) == 1


def test_property_completeness_respects_csv_fill_rate(tmp_path, monkeypatch):
    """Conditionally-null CSV columns (예: NDTResults.Defect_Location) should
    not be flagged incomplete when their CSV fill rate is low by design."""
    from tools.validation_support.checks import temporal_cardinality as tc

    # Build a fake CSV dir where defect_location is 38% filled.
    csv_dir = tmp_path / "rawdata"
    csv_dir.mkdir()
    csv_path = csv_dir / "NDT_Results.csv"
    rows = ["Sample_ID,Defect_Location"]
    for i in range(62):
        rows.append(f"N{i:04d},")
    for i in range(38):
        rows.append(f"N{i+62:04d},Section_{i}")
    csv_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    monkeypatch.setattr(tc, "SOURCE_RAWDATA_DIR", str(csv_dir))
    tc.invalidate_csv_caches()

    # T-Box: NDTResults class with defectLocation DP
    g = _new_graph()
    tbox = _new_graph()
    NDT = _cls("NDTResults")
    dp = _cls("defectLocation")
    tbox.add((NDT, RDF.type, OWL.Class))
    tbox.add((dp, RDF.type, OWL.DatatypeProperty))
    tbox.add((dp, RDFS.domain, NDT))
    # 100 NDT instances, 38 have defectLocation
    for i in range(100):
        inst = _inst(f"NDT_{i:04d}")
        g.add((inst, RDF.type, NDT))
        if i < 38:
            g.add((inst, dp, Literal(f"Section_{i}")))

    r = tc.check_property_completeness(g, tbox)
    tc.invalidate_csv_caches()

    # defectLocation must not appear as incomplete — its 38% fill matches CSV baseline.
    for item in r.get("incomplete_properties", []):
        assert item["property"] != "defectLocation", (
            f"CSV-sparse columns should not be flagged: {item}"
        )


def test_property_completeness_uses_uri_anchor_for_foreign_types():
    """URI prefix anchored to a real class wins over foreign types injected by the reasoner.

    Simulates the ProductionResult-vs-ProcessSteelmakingFurnace situation: a
    ProcessSteelmakingFurnace instance may be asserted as ProductionResult via
    restriction inference, but PR's DPs must not count against PSF instances.
    """
    g = _new_graph()
    tbox = _new_graph()
    PSF = _cls("ProcessSteelmakingFurnace")
    PR = _cls("ProductionResult")
    dp = _cls("yieldRatePercent")
    tbox.add((PSF, RDF.type, OWL.Class))
    tbox.add((PR, RDF.type, OWL.Class))
    tbox.add((dp, RDF.type, OWL.DatatypeProperty))
    tbox.add((dp, RDFS.domain, PR))  # DP belongs to PR
    # 10 PSF-prefixed instances carrying BOTH PSF and PR (reasoner inflation)
    for i in range(10):
        inst = _inst(f"ProcessSteelmakingFurnace_P{i:03d}")
        g.add((inst, RDF.type, PSF))
        g.add((inst, RDF.type, PR))
    # None of them carry yieldRatePercent — that DP belongs to real PR records
    r = check_property_completeness(g, tbox)
    # PR should not report these 10 PSF-anchored instances as 0% filled
    # because their URI anchors them to PSF, not PR.
    for item in r.get("incomplete_properties", []):
        assert item["class"] != "ProductionResult" or item.get("instances", 0) == 0, (
            f"PSF-anchored instances must not inflate PR stats: {item}"
        )


def test_property_completeness_uses_most_specific_type():
    """OWL RL 추론이 parent 타입을 전파해도, DP fill 은 most-specific class 로만 귀속된다."""
    g = _new_graph()
    tbox = _new_graph()
    parent = _cls("Animal")
    child = _cls("Dog")
    dp = _cls("dpName")
    tbox.add((parent, RDF.type, OWL.Class))
    tbox.add((child, RDF.type, OWL.Class))
    tbox.add((child, RDFS.subClassOf, parent))
    tbox.add((dp, RDF.type, OWL.DatatypeProperty))
    tbox.add((dp, RDFS.domain, child))  # domain = Dog only
    # 5 Dog 인스턴스 모두 Animal + Dog 타입 보유 (OWL RL 전파 시뮬레이션), DP 값도 모두 있음
    for i in range(5):
        inst = _inst(f"Dog{i}")
        g.add((inst, RDF.type, child))
        g.add((inst, RDF.type, parent))
        g.add((inst, dp, Literal(f"name{i}")))
    r = check_property_completeness(g, tbox)
    # 모든 인스턴스가 Dog (most-specific) 기준으로 100% 채워짐 — 미달 항목 없어야 함
    assert r["passed"] is True, f"got {r}"
    assert r["incomplete_count"] == 0


def test_functional_no_fp_declarations():
    r = check_functional_violations(_new_graph(), _new_graph())
    assert r["passed"] is True
    assert "선언 없음" in r["message"]


def test_functional_detects_duplicates():
    g = _new_graph()
    tbox = _new_graph()
    p = _cls("hasID")
    tbox.add((p, RDF.type, OWL.FunctionalProperty))
    s = _inst("A")
    g.add((s, p, Literal("v1")))
    g.add((s, p, Literal("v2")))  # 위반
    r = check_functional_violations(g, tbox)
    assert r["passed"] is False
    assert r["violation_count"] == 1


def test_cardinality_no_restrictions():
    r = check_cardinality_constraints(_new_graph(), _new_graph())
    assert r["passed"] is True


def test_cardinality_min_violation():
    g = _new_graph()
    tbox = _new_graph()
    cls = _cls("Eq")
    has_p = _cls("hasPart")
    tbox.add((cls, RDF.type, OWL.Class))
    # Restriction BNode: min 1 cardinality on hasPart
    rest = BNode()
    tbox.add((cls, RDFS.subClassOf, rest))
    tbox.add((rest, OWL.onProperty, has_p))
    tbox.add((rest, OWL.minCardinality, Literal(1, datatype=XSD.nonNegativeInteger)))
    # 인스턴스: hasPart 없음
    inst = _inst("E1")
    g.add((inst, RDF.type, cls))
    r = check_cardinality_constraints(g, tbox)
    assert r["passed"] is False


# ── 표본 상한이 검사 범위를 자르지 않는가 (2026-08-30) ────────────────────
#
# 예전에는 위반 20건에서 잘랐고 그 ``break`` 가 **restriction 루프 자체를** 빠져나갔다.
# 첫 공리가 20건을 채우면 나머지 공리는 아예 검사되지 않았고, 총계 필드도 없어 위반
# 규모를 알 수 없었다. 실측 (maxCardinality 공리 32개 / 실제 위반 50건):
#
#     checked_classes: 1    ← 32개 중 1개만 봤다
#     violations: 20        ← 전부 한 property
#     총계 필드: 없음
#
# 같은 파일의 functional check 는 이미 property 별 상한 + ``violation_count`` 를 쓴다.


def _axiom(tbox, cls_name: str, prop_name: str, limit: int = 1):
    """``cls ⊑ ≤limit prop`` 공리를 BNode restriction 으로 추가."""
    cls, prop = _cls(cls_name), _cls(prop_name)
    tbox.add((cls, RDF.type, OWL.Class))
    rest = BNode()
    tbox.add((cls, RDFS.subClassOf, rest))
    tbox.add((rest, OWL.onProperty, prop))
    tbox.add((rest, OWL.maxCardinality,
              Literal(limit, datatype=XSD.nonNegativeInteger)))
    return cls, prop


def _violating_instances(g, cls, prop, *, prefix: str, n: int, values: int = 3):
    """``prop`` 값을 ``values`` 개 갖는 ``cls`` 인스턴스 ``n`` 개 (max 1 위반)."""
    for i in range(n):
        inst = _inst(f"{prefix}{i}")
        g.add((inst, RDF.type, cls))
        for j in range(values):
            g.add((inst, prop, _inst(f"{prefix}{i}_v{j}")))


def test_sample_cap_does_not_stop_scanning_other_axioms():
    """THE REGRESSION: 앞 공리가 표본을 채워도 뒤 공리를 검사한다.

    공리 **5개** 에 각각 위반 25건을 둔다 (총 125). 예전 구현은 위반이 20건 쌓이면
    ``break`` 로 restriction 루프를 빠져나갔으므로 뒤쪽 공리를 **보지 못했다**.

    공리 개수가 중요하다: 2개만 두면 공리별 상한(5) × 2 = 10 이 전체 상한 20 에
    닿지 않아 옛 구현도 통과한다 (실측으로 확인 — mutation 이 생존했다). 옛 상한을
    실제로 넘기려면 공리 × 표본 > 20 이어야 한다.
    """
    g, tbox = _new_graph(), _new_graph()
    names = [(f"Cls{i}", f"hasP{i}") for i in range(5)]
    for i, (cls_name, prop_name) in enumerate(names):
        cls, prop = _axiom(tbox, cls_name, prop_name)
        _violating_instances(g, cls, prop, prefix=f"X{i}_", n=25)

    r = check_cardinality_constraints(g, tbox)
    assert r["passed"] is False
    assert r["checked_axioms"] == 5, r
    assert r["violated_axiom_count"] == 5, (
        f"뒤쪽 공리를 검사하지 않았다 — 표본 상한이 루프를 끊는다: "
        f"{r['violated_axioms']}"
    )
    assert r["violation_count"] == 125, r
    assert set(r["violated_axioms"]) == {f"{c}.{p}" for c, p in names}


def test_violation_count_reports_the_real_scale():
    """위반 **규모** 를 보고한다 — ``violations`` 길이로 판단하면 안 된다."""
    g, tbox = _new_graph(), _new_graph()
    cls, prop = _axiom(tbox, "Alpha", "hasA")
    _violating_instances(g, cls, prop, prefix="A", n=50)

    r = check_cardinality_constraints(g, tbox)
    assert r["violation_count"] == 50, r
    assert len(r["violations"]) < 50, "표본을 자르지 않았다 (응답이 비대해진다)"
    assert r["violated_axiom_count"] == 1


def test_samples_are_spread_across_axioms():
    """표본이 공리별로 배분된다 — 한 공리가 표본을 독점하지 않는다.

    예전에는 전체 상한 20이라 첫 공리가 20칸을 다 먹었다. 그러면 보고서를 읽는
    사람이 다른 공리의 위반을 **하나도** 못 본다. 공리 5개를 두는 이유는 위 테스트와
    같다 (2개로는 옛 상한에 닿지 않아 mutation 이 생존한다).
    """
    g, tbox = _new_graph(), _new_graph()
    names = [(f"Cls{i}", f"hasP{i}") for i in range(5)]
    for i, (cls_name, prop_name) in enumerate(names):
        cls, prop = _axiom(tbox, cls_name, prop_name)
        _violating_instances(g, cls, prop, prefix=f"Y{i}_", n=40)

    r = check_cardinality_constraints(g, tbox)
    props = {v["property"] for v in r["violations"]}
    assert props == {p for _, p in names}, (
        f"앞쪽 공리가 표본을 독점했다 — 뒤 공리의 위반이 보고서에서 사라진다: {props}"
    )


def test_clean_graph_still_passes():
    """PRESERVATION: 위반이 없으면 통과하고 총계가 0이다.

    카운터를 늘리는 방향만 검사하면 오발화를 못 잡는다.
    """
    g, tbox = _new_graph(), _new_graph()
    cls, prop = _axiom(tbox, "Alpha", "hasA")
    for i in range(30):
        inst = _inst(f"OK{i}")
        g.add((inst, RDF.type, cls))
        g.add((inst, prop, _inst(f"OK{i}_v0")))     # 값 1개 — max 1 정합
    r = check_cardinality_constraints(g, tbox)
    assert r["passed"] is True
    assert r["violation_count"] == 0
    assert r["violated_axiom_count"] == 0
    assert r["checked_axioms"] == 1


def test_no_restrictions_reports_zero_axioms():
    """공리가 없으면 0으로 보고한다 (미측정 ≠ 위반 없음 구분용)."""
    r = check_cardinality_constraints(_new_graph(), _new_graph())
    assert r["passed"] is True
    assert r["checked_axioms"] == 0
    assert r["violation_count"] == 0


def test_disjoint_no_groups():
    r = check_disjoint_class_violations(_new_graph(), _new_graph())
    assert r["passed"] is True
    assert r["disjoint_groups"] == 0


def test_disjoint_detects():
    g = _new_graph()
    tbox = _new_graph()
    a = _cls("A")
    b = _cls("B")
    tbox.add((a, RDF.type, OWL.Class))
    tbox.add((b, RDF.type, OWL.Class))
    # AllDisjointClasses(A, B)
    dj = BNode()
    tbox.add((dj, RDF.type, OWL.AllDisjointClasses))
    member_node = BNode()
    Collection(tbox, member_node, [a, b])
    tbox.add((dj, OWL.members, member_node))
    # 같은 인스턴스가 두 클래스에 속함 — 위반
    inst = _inst("X1")
    g.add((inst, RDF.type, a))
    g.add((inst, RDF.type, b))
    r = check_disjoint_class_violations(g, tbox)
    assert r["passed"] is False
    assert r["disjoint_groups"] == 1


def test_reexports_from_kg_validation():
    from tools.kg_validation import (
        _check_cardinality_constraints,
        _check_disjoint_class_violations,
        _check_functional_violations,
        _check_inference_sanity,
        _check_property_completeness,
    )
    assert callable(_check_cardinality_constraints)
    assert callable(_check_disjoint_class_violations)
    assert callable(_check_functional_violations)
    assert callable(_check_inference_sanity)
    assert callable(_check_property_completeness)


def test_package_reexports():
    from tools.validation_support.checks import (
        check_cardinality_constraints as a,
    )
    from tools.validation_support.checks import (
        check_disjoint_class_violations as b,
    )
    from tools.validation_support.checks import (
        check_functional_violations as c,
    )
    from tools.validation_support.checks import (
        check_inference_sanity as d,
    )
    from tools.validation_support.checks import (
        check_property_completeness as e,
    )
    for fn in (a, b, c, d, e):
        assert callable(fn)


# ── 추론 sanity: 자기비교를 판정으로 착각하지 않는가 (2026-08-31) ─────────
#
# validate_kg 는 기본이 use_inferred=False 이고 그 모드의 g 는 **추론 전** 그래프다.
# 예전에는 raw_triple_count = len(g) 로 같은 그래프를 양쪽에 넘겨 increase 가 항상 0 →
# 영구 FAIL 이었고, passed = (passed == total) 이므로 **기본 모드는 절대 통과할 수
# 없었다**. 게다가 mutation_runner 가 그 모드를 하드코딩해 S9.5 감사에서 이 체크는
# baseline 이 이미 FAIL — 어떤 mutant 도 잡지 못했다 (게이트가 구조적으로 무력).
#
# 설계가 아니라 회귀다: 원본(f2f7744)은 두 번째 그래프를 로드했고 344fd46 이 RSS 를
# 줄이려고 그것을 없애며 같은 그래프를 양쪽에 연결했다.
#
# 수정: raw 는 추론기가 기록한 실측값(inference_loss_manifest.json)을 쓰고, 받은
# 그래프가 raw 와 같으면 **판정하지 않고 그 사실을 보고**한다 (applicable: false).
# 미측정을 FAIL 로 내면 게이트가 상시 빨간불이 되어 아무도 보지 않는다.


def _sanity(inferred_n: int, raw_n: int, tmp_path):
    """추론 파일이 있는 상태로 check_inference_sanity 를 부른다."""
    marker = tmp_path / "all_inferred.ttl"
    marker.write_text("# exists", encoding="utf-8")
    g = _new_graph()
    for i in range(inferred_n):
        g.add((_inst(f"I{i}"), RDF.type, _cls("C")))
    return check_inference_sanity(g, raw_n, inferred_path=str(marker))


def test_same_graph_is_not_judged(tmp_path):
    """THE REGRESSION: raw 와 같은 크기면 판정하지 않는다 (FAIL 로 내지 않는다)."""
    r = _sanity(100, 100, tmp_path)
    assert r["applicable"] is False, r
    assert r["passed"] is True, "자기비교를 FAIL 로 냈다 — 게이트가 영구 빨간불이 된다"
    assert "use_inferred" in r["reason"], r["reason"]


def test_real_increase_is_judged(tmp_path):
    """PRESERVATION: 실제 증가가 있으면 판정한다 (게이트를 끈 것이 아니다)."""
    r = _sanity(160, 100, tmp_path)
    assert r["applicable"] is True
    assert r["passed"] is True
    assert r["increase"] == 60
    assert r["increase_ratio"] == "60.0%"


def test_shrinkage_is_distinguished_from_self_comparison(tmp_path):
    """추론 후 **줄어든** 경우는 자기비교와 다른 사유로 보고한다.

    둘 다 ``increase <= 0`` 이지만 원인이 다르다 — 하나는 호출자가 raw 를 넘긴 것이고
    하나는 추론 산출물이 이상한 것이다. 같은 문구로 뭉개면 후자를 놓친다.
    """
    r = _sanity(50, 1000, tmp_path)      # 절반 이하로 줄었다
    assert r["applicable"] is False
    assert "줄었다" in r["reason"], r["reason"]
    assert "use_inferred" not in r["reason"]


def test_unknown_raw_is_not_a_pass_by_zero(tmp_path):
    """raw 를 모르면 판정하지 않는다 — 0 을 분모로 쓰면 거짓 PASS 가 된다."""
    r = _sanity(1000, 0, tmp_path)
    assert r["applicable"] is False
    assert "알 수 없다" in r["reason"], r["reason"]
    assert r["raw_triples"] == 0


def test_missing_inferred_file_still_fails(tmp_path):
    """PRESERVATION: 추론 파일이 아예 없으면 FAIL 이다 (기존 계약).

    이것은 "판정 불가" 가 아니라 "추론을 안 돌렸다" 이고, 파이프라인 순서 위반이므로
    드러나야 한다.
    """
    r = check_inference_sanity(
        _new_graph(), 100, inferred_path=str(tmp_path / "nope.ttl"))
    assert r["passed"] is False
    assert r["applicable"] is True


def test_manifest_raw_matches_the_merged_graph():
    """실측 고정: 매니페스트 raw 가 병합 그래프와 일치하는가 (축 정합).

    파일 라인수 근사는 이 축에 맞지 않는다 — ``ensure_inverse_triples`` 산출
    58,993 트리플이 어떤 파일에도 없어 10.7% 어긋난다. 매니페스트는 같은 실행에서
    같은 로더로 잰 값이라 오차가 0 이어야 한다.
    """
    import os

    from config import INFERRED_PATH
    from domain.tbox_utils import load_graph
    from tools.kg_validation import _raw_triples_from_inference_manifest

    manifest = os.path.join(os.path.dirname(INFERRED_PATH),
                            "inference_loss_manifest.json")
    if not (os.path.exists(manifest) and os.path.exists(INFERRED_PATH)):
        pytest.skip("추론 매니페스트 없음")
    raw = _raw_triples_from_inference_manifest()
    if raw <= 0:
        pytest.skip("매니페스트가 낡아 판정 불가 (그 자체는 정상 동작)")
    merged, _ = load_graph(use_inferred=False)
    error = abs(raw - len(merged)) / len(merged)
    assert error < 0.01, (
        f"매니페스트 raw {raw:,} vs 병합 그래프 {len(merged):,} — 오차 {error:.1%}. "
        f"축이 어긋나면 증가율이 부풀거나 줄어든다"
    )


def test_stale_manifest_is_refused():
    """낡은 매니페스트로는 판정하지 않는다 — 낡은 baseline 이 거짓 신호를 만든다."""
    import os
    import tempfile
    from unittest.mock import patch

    import tools.kg_validation as kv

    with tempfile.TemporaryDirectory() as tmp:
        inferred = os.path.join(tmp, "all_inferred.ttl")
        manifest = os.path.join(tmp, "inference_loss_manifest.json")
        with open(manifest, "w", encoding="utf-8") as handle:
            handle.write('{"input": {"tbox_triples": 1, "abox_triples": 2}}')
        with open(inferred, "w", encoding="utf-8") as handle:
            handle.write("# newer")
        # 매니페스트를 1시간 과거로
        old = os.path.getmtime(inferred) - 3600
        os.utime(manifest, (old, old))
        with patch.object(kv, "INFERRED_PATH", inferred):
            assert kv._raw_triples_from_inference_manifest() == 0, (
                "낡은 매니페스트를 판정 근거로 썼다"
            )


# ── 오염된 매니페스트 거부 (2026-09-01) ────────────────────────────
#
# mtime 비교는 **낡음** 한 방향만 본다. 실제로 일어난 사고는 반대 방향이다 — 테스트가
# 배포 매니페스트를 덮어쓰면 그 파일이 추론 산출물보다 **더 새것**이 되므로 낡음
# 가드는 영원히 발화하지 않는다. 아래 두 테스트는 그 두 축(산술 / 실행 동일성)을
# 각각 겨냥한다. 둘 다 실측 오염값을 픽스처로 쓴다.


def _write_manifest(directory, payload: dict) -> tuple[str, str]:
    """Write a manifest + a *newer* inference output, as contamination does."""
    import json as _json
    import os as _os

    inferred = _os.path.join(directory, "all_inferred.ttl")
    manifest = _os.path.join(directory, "inference_loss_manifest.json")
    with open(manifest, "w", encoding="utf-8") as handle:
        _json.dump(payload, handle)
    with open(inferred, "w", encoding="utf-8") as handle:
        handle.write("# inference output\n")
    # 오염은 항상 매니페스트를 *지금* 쓴다 → 추론 파일보다 새것으로 만든다.
    older = _os.path.getmtime(manifest) - 600
    _os.utime(inferred, (older, older))
    return inferred, manifest


def test_arithmetically_impossible_manifest_is_refused(tmp_path):
    """음수 트리플 수를 담은 매니페스트로는 판정하지 않는다.

    실측 (2026-09-01): 전체 스위트가 배포 매니페스트를 이렇게 덮었다::

        input: {tbox_triples: 4922, abox_triples: -4921, tacit_triples: 0}

    ``abox_triples`` 는 ``triples_before - len(tbox)`` 이므로 음수는 "추론기에 넘긴
    그래프가 자기 T-Box 보다 작다" 는 뜻 — 실행에서 나올 수 없다. 그 합(=1)을 raw 로
    쓰면 증가율이 8,284만% 로 부풀어 **그럴듯한 PASS** 가 된다. FAIL 이 아니라
    초록불이라 아무도 보지 않는다.
    """
    from unittest.mock import patch

    import tools.kg_validation as kv

    inferred, _ = _write_manifest(str(tmp_path), {
        "input": {"tbox_triples": 4922, "abox_triples": -4921, "tacit_triples": 0},
        "output": {"total_triples": 1, "inferred_new": 0},
    })
    with patch.object(kv, "INFERRED_PATH", inferred):
        assert kv._raw_triples_from_inference_manifest() == 0, (
            "산술이 성립하지 않는 매니페스트를 판정 근거로 썼다 — 음수 트리플 수는 "
            "테스트 오염의 서명이다"
        )


def test_manifest_from_a_different_run_is_refused(tmp_path):
    """다른 실행의 매니페스트로는 판정하지 않는다 (실행 동일성).

    산술이 성립하더라도 그 매니페스트가 **지금 로드한 추론 그래프를 만든 실행**의
    것이 아니면 판정 근거가 없다. ``output.total_triples`` 가 그 실행이 만든 그래프
    크기이므로 호출자가 로드한 크기와 대조한다.
    """
    from unittest.mock import patch

    import tools.kg_validation as kv

    inferred, _ = _write_manifest(str(tmp_path), {
        "input": {"tbox_triples": 100, "abox_triples": 400, "tacit_triples": 0},
        "output": {"total_triples": 500, "inferred_new": 0},
    })
    with patch.object(kv, "INFERRED_PATH", inferred):
        # 같은 실행이라고 주장하는 크기 → 판정한다.
        assert kv._raw_triples_from_inference_manifest(500) == 500
        # 5% 안쪽 오차는 허용 (직렬화/로더 차이).
        assert kv._raw_triples_from_inference_manifest(510) == 500
        # 전혀 다른 크기 → 다른 실행이므로 판정 포기.
        assert kv._raw_triples_from_inference_manifest(1_305_608) == 0, (
            "다른 실행의 매니페스트로 판정했다 — 오염이 mtime 가드를 통과하는 경로다"
        )
        # 인자를 안 주면 대조를 건너뛴다 (use_inferred=False 경로는 len(g) 가 추론
        # 그래프가 아니므로 대조할 수 없다).
        assert kv._raw_triples_from_inference_manifest(None) == 500


def test_inference_refuses_to_persist_impossible_stats():
    """생성 지점도 막는다 — 가드는 신규 쓰기만 막으므로 두 벌이 필요하다.

    소비 지점 방어만으로는 오염 파일이 디스크에 남아 다른 소비자
    (``semantic_dictionary`` 의 ``inference_preservation_rate``) 가 계속 읽는다.
    """
    from tools.inference import manifest_arithmetic_error

    assert manifest_arithmetic_error(
        {"tbox_triples": 4922, "abox_triples": -4921, "tacit_triples": 0},
        {"total_triples": 1},
    ) is not None, "음수 abox_triples 를 저장 가능으로 판정했다"

    assert manifest_arithmetic_error(
        {"tbox_triples": 4922, "abox_triples": 823_511, "tacit_triples": 51_242},
        {"total_triples": 1_305_608},
    ) is None, "정상 실행값을 거부했다 (오발화)"
