"""mutation 이 가드를 **눈멀게 한 것** 을 별 신호로 내는가.

## 왜 (2026-09-05 실측)

S4.5(applied 17 / caught 7) 와 S9.5(applied 10 / caught 6) 양쪽이 놓친 유일한 **순수**
갭이 ``M7/some_to_all`` 이었다 — ``someValuesFrom`` 40건을 전부 ``allValuesFrom`` 으로
바꾼다 (40 제거 / 40 추가이므로 약화도 아니다). 그러면::

    필수참여 공리 충족:  axioms_checked 40 → 0,  passed: true

검사할 공리가 사라져 **공허하게 통과**한다. 체크 자신은 이미 메시지에 "vacuous pass"
라고 적고 있었는데도 ``passed: True`` 라서 PASS→PASS 로 읽혀 아무도 못 봤다.
필수참여 보장이 통째로 없어졌는데 25 check 중 어느 것도 변화를 보고하지 않은 것이다.

## 이 파일이 주장하는 것

두 가지를 갈랐다:

1. **미판정을 PASS 로 접지 않는다** — ``applicable: false`` + ``reason``.
2. **눈먼 것을 catch 로 세지 않는다** — 검증기가 위반을 본 게 아니라 볼 능력을 잃은
   것이다. catch 로 세면 비-탐지를 검출률에 부풀린다 (분모를 깎지 않기로 한 것과 같은
   이유). 대신 ``blinded_checks`` 로 낸다 — 가드가 눈먼 것은 위반보다 나쁠 수 있다.
"""
from __future__ import annotations

import os

import pytest
from rdflib import RDF, RDFS, Graph, URIRef
from rdflib.namespace import OWL

from domain.namespaces import DOMAIN_NS
from tools.mutation_runner import _blinded_checks, _caught_by
from tools.validation_support.checks.semantic import check_existential_participation

INST = str(DOMAIN_NS).rstrip("#") + "/instances#"


def D(name: str) -> URIRef:
    return URIRef(DOMAIN_NS + name)


# ── ① 미판정을 PASS 로 접지 않는가 ──────────────────────────────────────


def _tbox(*, existential: bool) -> Graph:
    tb = Graph()
    for cls in ("Owner", "Target"):
        tb.add((D(cls), RDF.type, OWL.Class))
    tb.add((D("ownerHasTarget"), RDF.type, OWL.ObjectProperty))
    restriction = D("Owner_ownerHasTarget_restriction")
    tb.add((restriction, RDF.type, OWL.Restriction))
    tb.add((restriction, OWL.onProperty, D("ownerHasTarget")))
    axis = OWL.someValuesFrom if existential else OWL.allValuesFrom
    tb.add((restriction, axis, D("Target")))
    tb.add((D("Owner"), RDFS.subClassOf, restriction))
    return tb


def _abox() -> Graph:
    g = Graph()
    owner = URIRef(INST + "o1")
    g.add((owner, RDF.type, D("Owner")))
    g.add((owner, D("ownerHasTarget"), URIRef(INST + "t1")))
    g.add((URIRef(INST + "t1"), RDF.type, D("Target")))
    return g


def test_zero_axioms_is_not_applicable():
    """THE REGRESSION: 검사 대상 0건은 미판정이다 (PASS 로 접지 않는다)."""
    res = check_existential_participation(_abox(), _tbox(existential=False))
    assert res["axioms_checked"] == 0
    assert res.get("applicable") is False, res
    assert res.get("reason"), "미판정 사유가 없다"


def test_axioms_present_stays_measured():
    """PRESERVATION: 공리가 있으면 측정된 축이다 (applicable 키를 붙이지 않는다)."""
    res = check_existential_participation(_abox(), _tbox(existential=True))
    assert res["axioms_checked"] == 1
    assert "applicable" not in res, (
        "측정 가능한데 applicable 을 붙였다 — score_measured 분모에서 빠진다"
    )
    assert res["passed"] is True


# ── ② 눈먼 것을 catch 로 세지 않는가 ────────────────────────────────────


def test_blinded_is_detected():
    base = {"체크A": "PASS", "체크B": "PASS"}
    mut = {"체크A": "N/A", "체크B": "PASS"}
    assert _blinded_checks(base, mut) == ["체크A"]


def test_blinded_is_not_counted_as_caught():
    """N/A 는 등급 악화가 아니다 — 검출률에 들어가면 비-탐지를 부풀린다."""
    base = {"체크A": "PASS"}
    mut = {"체크A": "N/A"}
    assert _caught_by(base, mut) == [], (
        "판정 불가를 catch 로 셌다 — 검증기가 위반을 본 것이 아니다"
    )


def test_already_unmeasured_is_not_blinded():
    """baseline 에서 이미 N/A 였으면 mutation 이 눈멀게 한 것이 아니다.

    ``추론 sanity check`` 는 merge 모드에서 상시 N/A 다 — 그것을 매 mutant 마다
    "눈멀게 했다" 고 보고하면 신호가 소음이 된다.
    """
    base = {"추론 sanity check": "N/A"}
    mut = {"추론 sanity check": "N/A"}
    assert _blinded_checks(base, mut) == []


def test_fail_to_na_is_also_blinded():
    """FAIL 이던 축이 판정 불가가 되면 그것도 측정 능력 상실이다."""
    assert _blinded_checks({"체크A": "FAIL"}, {"체크A": "N/A"}) == ["체크A"]


def test_real_mutant_blinds_but_is_not_caught():
    """실물 확인 — ``some_to_all`` 은 눈멀게 하지만 catch 는 아니다."""
    from config import TBOX_PATH

    if not os.path.exists(TBOX_PATH):
        pytest.skip("배포 T-Box 없음")
    from domain.tbox_utils import load_graph
    from tools.mutation_runner import apply_mutator, discover_mutators

    catalog = {m.name: m for m in discover_mutators("rules/mutations")}
    name = "M7_restriction/some_to_all"
    if name not in catalog:
        pytest.skip(f"mutator 없음: {name}")
    base_tb = Graph()
    base_tb.parse(TBOX_PATH, format="turtle")
    mutated, info = apply_mutator(base_tb, catalog[name])
    if not info.get("applied"):
        pytest.skip(f"적용 불가: {info.get('reason')}")
    g, _ = load_graph()

    before = check_existential_participation(g, base_tb)
    after = check_existential_participation(g, mutated)
    assert "applicable" not in before, "배포 T-Box 에서 이 축이 이미 미판정이다"
    assert after.get("applicable") is False, after

    def _status(res: dict) -> str:
        if res.get("applicable") is False:
            return "N/A"
        return "PASS" if res.get("passed") else "FAIL"

    b = {"필수참여 공리 충족": _status(before)}
    m = {"필수참여 공리 충족": _status(after)}
    assert _blinded_checks(b, m) == ["필수참여 공리 충족"]
    assert _caught_by(b, m) == []


# ── ③ 배선 ─────────────────────────────────────────────────────────────


def test_kg_status_maps_applicable_false_to_na():
    """``_kg_status_from_payload`` 가 applicable:false 를 N/A 로 낸다."""
    import inspect

    from tools import mutation_runner as mr

    src = inspect.getsource(mr)
    assert 'result[name] = "N/A"' in src, "applicable:false → N/A 매핑이 없다"
    assert 'if check.get("applicable") is False:' in src


def test_summary_reports_blinded_axis():
    import inspect

    from tools.mutation_runner import run_kg_mutations

    src = inspect.getsource(run_kg_mutations)
    assert '"blinded": blinded' in src
    assert '"blinded_checks_union"' in src
    assert '"blinded_checks": _blinded_checks(' in src, (
        "mutant 별 blinded_checks 각인이 없다"
    )


def test_na_not_in_status_rank():
    """N/A 는 등급 체계에 없어야 한다 — 있으면 catch 판정에 섞인다."""
    from tools.mutation_runner import _STATUS_RANK

    assert "N/A" not in _STATUS_RANK, _STATUS_RANK


def test_shipped_tbox_existential_axis_is_measured():
    """배포 T-Box 에서 이 축이 미판정이면 안 된다 (그러면 상시 눈먼 상태다)."""
    from config import TBOX_PATH

    if not os.path.exists(TBOX_PATH):
        pytest.skip("배포 T-Box 없음")
    from domain.tbox_utils import load_graph

    tb = Graph()
    tb.parse(TBOX_PATH, format="turtle")
    g, _ = load_graph()
    res = check_existential_participation(g, tb)
    assert "applicable" not in res, res.get("reason")
    assert res["axioms_checked"] > 0
