"""세대 간 공리 인벤토리 — 이름 축이 못 보는 **제거** 를 잡는가.

## 왜 (2026-09-05 실측)

mutation 감사에서 미검출로 남은 mutant 의 사유 절반이 ``weakening_only`` 다 — 제약이
사라지면 **위반** 검사로는 볼 것이 없다. 그런데 ``compare_tbox_baseline`` 은 클래스·
OP/DP **이름** 과 domain/range 만 비교했다. 배포 T-Box 에 4종을 적용했을 때 이름 축
변화가 전부 **0** 이었다::

    delete_inverse     (-1 트리플)  이름축 변화 없음
    toggle_functional  (-1)         없음
    delete_disjoint    (-4)         없음
    remove_parent      (-3)         없음

즉 세대 사이에 공리가 사라져도 어떤 축도 보고하지 않았다. 인벤토리 비교가 그것을 보는
유일한 경로다.

## 이 파일이 주장하는 것

인벤토리가 **제거를 잡고**, 주석 변경처럼 공리가 아닌 것에는 **발화하지 않는다**.
후자가 중요하다 — 아무 변화에나 반응하면 세대 간 비교가 소음이 된다.
"""
from __future__ import annotations

import json
import os

import pytest
from rdflib import RDF, RDFS, Graph, Literal, URIRef
from rdflib.namespace import OWL

from domain.namespaces import DOMAIN_NS
from tools.tbox_metrics import axiom_inventory


def D(name: str) -> URIRef:
    return URIRef(DOMAIN_NS + name)


# ── 계수 방식 ──────────────────────────────────────────────────────────


def test_predicate_kinds_count_triples():
    g = Graph()
    g.add((D("A"), RDFS.subClassOf, D("B")))
    g.add((D("A"), RDFS.subClassOf, D("C")))
    g.add((D("p"), OWL.inverseOf, D("q")))
    inv = axiom_inventory(g)
    assert inv["subClassOf"] == 2
    assert inv["inverseOf"] == 1
    assert inv["someValuesFrom"] == 0


def test_type_kinds_count_subjects():
    g = Graph()
    g.add((D("p"), RDF.type, OWL.FunctionalProperty))
    g.add((D("q"), RDF.type, OWL.FunctionalProperty))
    g.add((D("d1"), RDF.type, OWL.AllDisjointClasses))
    inv = axiom_inventory(g)
    assert inv["FunctionalProperty"] == 2
    assert inv["AllDisjointClasses"] == 1
    assert inv["TransitiveProperty"] == 0


def test_foreign_axioms_are_counted():
    """외래 온톨로지에서 온 공리도 센다 — 잃으면 그것도 회귀다.

    도메인 네임스페이스로 걸러내면 IOF 부모를 잃은 것을 못 본다.
    """
    iof = URIRef("https://spec.industrialontologies.org/ontology/construct/Foo")
    g = Graph()
    g.add((D("A"), RDFS.subClassOf, iof))
    assert axiom_inventory(g)["subClassOf"] == 1


def test_all_kinds_present_in_result():
    """모든 종류가 키로 존재해야 한다 — 0 과 '키 없음' 을 섞으면 delta 가 깨진다."""
    inv = axiom_inventory(Graph())
    assert inv, "빈 그래프에서 빈 dict 를 냈다"
    assert all(v == 0 for v in inv.values())
    for expected in ("subClassOf", "inverseOf", "FunctionalProperty",
                     "AllDisjointClasses", "someValuesFrom", "allValuesFrom",
                     "maxCardinality", "propertyChainAxiom"):
        assert expected in inv, expected


# ── 실물 mutator 대조 ──────────────────────────────────────────────────


@pytest.mark.parametrize(("mutant", "kind", "sign"), [
    ("M2_inverse/delete_inverse", "inverseOf", -1),
    ("M3_cardinality/toggle_functional", "FunctionalProperty", -1),
    ("M4_disjoint/delete_disjoint", "AllDisjointClasses", -1),
    ("M4_disjoint/remove_parent", "subClassOf", -1),
])
def test_weakening_mutants_show_negative_delta(mutant, kind, sign):
    """감사에서 ``weakening_only`` 로 남은 mutant 가 이 축에서는 보인다."""
    from config import TBOX_PATH

    if not os.path.exists(TBOX_PATH):
        pytest.skip("배포 T-Box 없음")
    from tools.mutation_runner import apply_mutator, discover_mutators

    catalog = {m.name: m for m in discover_mutators("rules/mutations")}
    if mutant not in catalog:
        pytest.skip(f"mutator 없음: {mutant}")
    base = Graph()
    base.parse(TBOX_PATH, format="turtle")
    before = axiom_inventory(base)
    mutated, info = apply_mutator(base, catalog[mutant])
    if not info.get("applied"):
        pytest.skip(f"적용 불가: {info.get('reason')}")
    after = axiom_inventory(mutated)
    delta = after[kind] - before[kind]
    assert delta < 0, (
        f"{mutant} 이 {kind} 를 줄였는데 인벤토리 delta 가 {delta} 다"
    )
    assert sign < 0


def test_some_to_all_shows_the_swap():
    """∃→∀ 는 감소와 증가가 **쌍으로** 보여야 한다 (약화가 아니라 의미 교체)."""
    from config import TBOX_PATH

    if not os.path.exists(TBOX_PATH):
        pytest.skip("배포 T-Box 없음")
    from tools.mutation_runner import apply_mutator, discover_mutators

    catalog = {m.name: m for m in discover_mutators("rules/mutations")}
    name = "M7_restriction/some_to_all"
    if name not in catalog:
        pytest.skip(f"mutator 없음: {name}")
    base = Graph()
    base.parse(TBOX_PATH, format="turtle")
    before = axiom_inventory(base)
    mutated, info = apply_mutator(base, catalog[name])
    if not info.get("applied"):
        pytest.skip("적용 불가")
    after = axiom_inventory(mutated)
    assert after["someValuesFrom"] < before["someValuesFrom"]
    assert after["allValuesFrom"] > before["allValuesFrom"]


def test_annotation_mutant_does_not_fire():
    """PRESERVATION: 라벨/주석 변경은 공리가 아니다 — 발화하면 소음이 된다."""
    from config import TBOX_PATH

    if not os.path.exists(TBOX_PATH):
        pytest.skip("배포 T-Box 없음")
    from tools.mutation_runner import apply_mutator, discover_mutators

    catalog = {m.name: m for m in discover_mutators("rules/mutations")}
    name = "M5_annotation/delete_label"
    if name not in catalog:
        pytest.skip(f"mutator 없음: {name}")
    base = Graph()
    base.parse(TBOX_PATH, format="turtle")
    before = axiom_inventory(base)
    mutated, info = apply_mutator(base, catalog[name])
    if not info.get("applied"):
        pytest.skip("적용 불가")
    assert axiom_inventory(mutated) == before, "주석 변경에 공리 인벤토리가 반응했다"


# ── 도구 응답 ──────────────────────────────────────────────────────────


def test_compare_tbox_baseline_reports_inventory():
    """``compare_tbox_baseline`` 응답에 인벤토리 축이 실린다."""
    from config import TBOX_BASELINE_PATH, TBOX_PATH
    from tools.tbox_metrics import compare_tbox_baseline

    if not (os.path.exists(TBOX_PATH) and os.path.exists(TBOX_BASELINE_PATH)):
        pytest.skip("T-Box 또는 베이스라인 없음")
    payload = json.loads(compare_tbox_baseline())
    assert payload.get("success") is True, payload
    inv = payload.get("axiom_inventory")
    assert inv is not None, "axiom_inventory 축이 없다"
    for key in ("baseline", "current", "delta", "weakened", "weakened_count"):
        assert key in inv, key
    # delta 는 baseline/current 차이와 일치해야 한다 (계산 사본 방지).
    for kind, value in inv["delta"].items():
        assert value == inv["current"][kind] - inv["baseline"][kind], kind
    assert all(v < 0 for v in inv["weakened"].values()), inv["weakened"]
    assert inv["weakened_count"] == len(inv["weakened"])


def test_name_axis_alone_misses_weakening():
    """이 축을 만든 이유 자체를 주장한다 — 이름 축은 제거를 못 본다.

    실측: 배포 T-Box 에 4종을 적용해도 클래스/OP/DP **이름 집합** 은 그대로다.
    """
    from config import TBOX_PATH

    if not os.path.exists(TBOX_PATH):
        pytest.skip("배포 T-Box 없음")
    from tools.mutation_runner import apply_mutator, discover_mutators

    catalog = {m.name: m for m in discover_mutators("rules/mutations")}
    ns = str(DOMAIN_NS)

    def _names(g: Graph) -> dict:
        return {
            label: {str(x).split("#")[-1]
                    for x in g.subjects(RDF.type, term)
                    if str(x).startswith(ns)}
            for label, term in (("cls", OWL.Class), ("op", OWL.ObjectProperty),
                                ("dp", OWL.DatatypeProperty))
        }

    base = Graph()
    base.parse(TBOX_PATH, format="turtle")
    baseline_names = _names(base)
    checked = 0
    for name in ("M2_inverse/delete_inverse", "M3_cardinality/toggle_functional",
                 "M4_disjoint/delete_disjoint", "M4_disjoint/remove_parent"):
        if name not in catalog:
            continue
        g = Graph()
        g.parse(TBOX_PATH, format="turtle")
        mutated, info = apply_mutator(g, catalog[name])
        if not info.get("applied"):
            continue
        checked += 1
        assert _names(mutated) == baseline_names, (
            f"{name} 이 이름 축에도 보인다 — 그렇다면 인벤토리 축의 근거가 약해진다"
        )
        assert axiom_inventory(mutated) != axiom_inventory(base), (
            f"{name} 이 인벤토리 축에도 안 보인다"
        )
    if not checked:
        pytest.skip("적용된 mutator 없음")
    assert Literal  # import 사용 표시


# ── 배선 경계 ──────────────────────────────────────────────────────────


def test_baseline_compare_is_not_an_audit_validator():
    """세대 비교를 mutation 감사 검증기 집합에 넣으면 catch_rate 가 무의미해진다.

    mutant 는 **정의상** 베이스라인과 다르므로 모든 주입이 "검출" 로 세어져 검출률이
    ~100% 가 된다. 세대 비교는 파이프라인용 가드이고 결함 탐지기가 아니다.

    이 리포는 지표가 스스로를 확인하는 형태를 반복해서 기각했다 (LLM 결과를 LLM 으로
    채점하는 자기참조, 0행 표적을 분모에서 빼는 지표 매수 등). 같은 부류다.
    """
    from tools.mutation_runner import _validator_registry

    names = [name for name, _fn, _cls in _validator_registry()]
    assert names == ["syntax", "quality", "hermit", "classify", "shacl"], names
    for forbidden in ("baseline", "compare", "inventory"):
        assert not any(forbidden in n for n in names), (
            f"감사 검증기에 세대 비교({forbidden})가 들어갔다 — 모든 mutant 가 "
            "자동 검출되어 catch_rate 가 무의미해진다"
        )


def test_deployed_tbox_has_not_lost_axioms():
    """산출물 확인 — 배포 T-Box 가 베이스라인 대비 공리를 잃지 않았는가.

    비어 있지 않으면 세대 간 표현력 손실이다. WARN-only 축이므로 정당한 축소일 수도
    있지만(근거 없는 공리 차단 등) **무엇이 줄었는지 사람이 봐야 한다**.
    """
    from config import TBOX_BASELINE_PATH, TBOX_PATH
    from tools.tbox_metrics import compare_tbox_baseline

    if not (os.path.exists(TBOX_PATH) and os.path.exists(TBOX_BASELINE_PATH)):
        pytest.skip("T-Box 또는 베이스라인 없음")
    inv = json.loads(compare_tbox_baseline())["axiom_inventory"]
    assert inv["weakened"] == {}, (
        f"베이스라인 대비 공리가 줄었다: {inv['weakened']}. 정당한 축소인지 확인한 뒤 "
        "베이스라인을 승격하거나 손실을 복구하라 (CLAUDE.md S4 6번째 축)"
    )


def test_claude_md_documents_the_axis():
    """CLAUDE.md 가 이 축과 배선 금지 경계를 적었는가.

    게이트를 만들어도 상태머신 문서에 없으면 아무도 돌리지 않는다 — 이 세션에서
    `run_meta_audit` 이 12회 누적될 동안 한 번도 실행되지 않은 것과 같은 형태다.
    """
    with open("CLAUDE.md", encoding="utf-8") as handle:
        text = handle.read()
    assert "compare_tbox_baseline" in text, "S4 축에 도구가 언급되지 않았다"
    assert "axiom_inventory.weakened" in text, "무엇을 봐야 하는지 적혀 있지 않다"
    assert "mutation 감사 검증기 집합에 넣지 말 것" in text, (
        "배선 금지 경계가 문서에 없다 — 다음 사람이 넣는다"
    )
