"""선언 인벤토리가 계층·공리 축을 담는다 — "확인 불가" 지적 회귀 가드.

2026-08-27 실측. 인벤토리는 **클래스 목록 + OP domain→range** 두 축만 담았고, 리뷰어가
보는 TTL 발췌는 원문의 **29%** 다 (50,000자 상한). 그래서 네 축이 발췌에도 인벤토리에도
없었다:

    축                     실제      인벤토리
    subClassOf             69건      **없음**
    AllDisjointClasses     46그룹    개수만 (members 0%)
    someValuesFrom         30건      **없음**
    inverseOf             106건      **없음**

리뷰어는 검증할 수 없는 것을 매 라운드 "확인 불가" 로 지적했고, 그 지적이 차단 이슈로
세어져 승인을 막았다. S2 실행 2회에서 잔여 차단의 **최다 성격**이었다:

    "AllDisjointClasses 분류 축 — 발췌된 TTL에서 명시적 선언이 보이지 않음"
    "CriticalAlarmEvent, WarningAlarmEvent 클래스 정의 미확인"

## AllDisjointClasses 는 members 를 펼쳐야 한다

개수만 주면 **"축이 혼재됐는가"** 를 판정할 수 없는데, 그 판정이 정확히 리뷰어가 해야
하는 일이다. 이 리포에서 직교 축(데이터 성격 축 + 업무 도메인 축)을 한 그룹에 섞어
6개 클래스가 unsatisfiable 이 된 이력이 있고, 그때 리뷰어 4라운드가 아무도 지적하지
못했다 — 볼 수 없었기 때문이다.

## 비용

네 축 합계 약 9,458자 ≈ 2,700 토큰 (인벤토리 8,115 → 17,573자). 리뷰어 입력이
17,433 → 약 20,100 토큰이 되고 **입력은 출력 예산(_REVIEW_MAX_TOKENS=16,000)과
별개**다. TTL 발췌가 이미 14,304 토큰(82%)을 쓰면서 29%만 보여주는 것에 비하면 전수
목록이 훨씬 효율적이다.

## 이 테스트의 방향

"인벤토리가 길어졌다" 만 주장하면 내용이 틀려도 통과한다. 세 축을 함께 고정한다:

* 존재 — 네 축이 전부 있고, 실제 선언이 목록에 나타난다
* 정확성 — 중복 제거는 하되 **손실이 없다** (disjoint members 부재 / inverseOf 방향)
* 지시 — "발췌에 없다고 누락으로 지적하지 말라" 는 문구가 함께 간다
"""
from __future__ import annotations

import pathlib

import pytest
from rdflib import OWL, RDF, RDFS, BNode, Graph, Literal, Namespace, URIRef
from rdflib.collection import Collection

from domain.namespaces import DOMAIN_NS
from tools.multi_agent_tbox import _axiom_inventory, _format_declaration_inventory

NS = Namespace(str(DOMAIN_NS))


def _local(node) -> str:
    return str(node).split("#")[-1].split("/")[-1]


def _in_domain(node) -> bool:
    return isinstance(node, URIRef) and str(node).startswith(str(DOMAIN_NS))


def _graph_with_axioms() -> Graph:
    """네 축을 모두 가진 최소 T-Box."""
    g = Graph()
    for name in ("Parent", "Child", "A", "B", "Holder", "Filler"):
        g.add((NS[name], RDF.type, OWL.Class))
    # subClassOf
    g.add((NS["Child"], RDFS.subClassOf, NS["Parent"]))
    # inverseOf (양방향 선언 — 한 줄로 접혀야 한다)
    g.add((NS["fwd"], RDF.type, OWL.ObjectProperty))
    g.add((NS["rev"], RDF.type, OWL.ObjectProperty))
    g.add((NS["fwd"], OWL.inverseOf, NS["rev"]))
    g.add((NS["rev"], OWL.inverseOf, NS["fwd"]))
    # AllDisjointClasses
    node = BNode()
    g.add((node, RDF.type, OWL.AllDisjointClasses))
    lst = BNode()
    Collection(g, lst, [NS["A"], NS["B"]])
    g.add((node, OWL.members, lst))
    # someValuesFrom
    restr = BNode()
    g.add((restr, RDF.type, OWL.Restriction))
    g.add((restr, OWL.onProperty, NS["fwd"]))
    g.add((restr, OWL.someValuesFrom, NS["Filler"]))
    g.add((NS["Holder"], RDFS.subClassOf, restr))
    return g


# ── 존재: 네 축이 전부 담긴다 ───────────────────────────────────────────


def test_all_four_axes_are_extracted():
    """THE REGRESSION: 네 축이 모두 목록으로 나온다."""
    got = _axiom_inventory(_graph_with_axioms(), _local, _in_domain)
    assert got["subclass"] == ["Child ⊑ Parent"]
    assert got["inverse"] == ["fwd ↔ rev"]
    assert got["disjoint"] == ["A, B"]
    assert got["some_values"] == ["Holder ∃fwd.Filler"]


def test_inventory_text_contains_every_axis_header():
    """렌더된 인벤토리에 네 축 헤더가 있는가 — 추출만 되고 안 실리면 무의미하다."""
    text = _format_declaration_inventory(_graph_with_axioms().serialize(format="turtle"))
    for probe in ("subClassOf", "AllDisjointClasses", "inverseOf", "someValuesFrom"):
        assert probe in text, f"인벤토리에 {probe} 축이 없다"


def test_inventory_renders_the_actual_declarations():
    """헤더만 있고 내용이 비면 안 된다 — 실제 선언이 본문에 나타난다."""
    text = _format_declaration_inventory(_graph_with_axioms().serialize(format="turtle"))
    assert "Child ⊑ Parent" in text
    assert "A, B" in text
    assert "fwd ↔ rev" in text
    assert "Holder ∃fwd.Filler" in text


def test_disjoint_members_are_expanded_not_counted():
    """``AllDisjointClasses`` 는 개수가 아니라 **members** 를 펼친다.

    개수만 주면 "축이 혼재됐는가" 를 판정할 수 없다 — 그 판정이 리뷰어의 핵심 임무이고,
    못 해서 6개 클래스가 unsat 이 된 이력이 있다.
    """
    text = _format_declaration_inventory(_graph_with_axioms().serialize(format="turtle"))
    assert "A, B" in text, "members 가 펼쳐지지 않았다 (개수만 보고하면 판정 불가)"


# ── 정확성: 중복 제거는 하되 손실은 없다 ────────────────────────────────


def test_bidirectional_inverse_pair_collapses_to_one_line():
    """``A↔B`` 와 ``B↔A`` 가 모두 선언되면 한 줄이다 (실측 106 트리플 → 54 쌍)."""
    got = _axiom_inventory(_graph_with_axioms(), _local, _in_domain)
    assert got["inverse"] == ["fwd ↔ rev"], "방향 쌍이 두 줄로 중복됐다"


def test_one_way_inverse_is_still_reported():
    """한쪽만 선언된 ``inverseOf`` 도 보고한다 — 중복 제거가 누락을 만들면 안 된다."""
    g = Graph()
    g.add((NS["p"], RDF.type, OWL.ObjectProperty))
    g.add((NS["q"], RDF.type, OWL.ObjectProperty))
    g.add((NS["p"], OWL.inverseOf, NS["q"]))     # 한 방향만
    got = _axiom_inventory(g, _local, _in_domain)
    assert got["inverse"] == ["p ↔ q"]


def test_duplicate_disjoint_groups_are_deduped():
    """같은 클래스 집합이 두 노드로 선언되면 한 줄 (실측 46 노드 → 24 고유).

    감소가 **중복 제거**임을 고정한다. 배포 T-Box 실측에서 members 부재 0 / 완전
    중복 22건이었다 — 손실이 아니다.
    """
    g = _graph_with_axioms()
    node2 = BNode()
    g.add((node2, RDF.type, OWL.AllDisjointClasses))
    lst2 = BNode()
    Collection(g, lst2, [NS["B"], NS["A"]])       # 순서만 다른 같은 집합
    g.add((node2, OWL.members, lst2))
    got = _axiom_inventory(g, _local, _in_domain)
    assert got["disjoint"] == ["A, B"], f"중복이 제거되지 않았다: {got['disjoint']}"


def test_distinct_disjoint_groups_are_both_kept():
    """다른 집합은 각각 남는다 — 중복 제거가 과도하지 않은가."""
    g = _graph_with_axioms()
    g.add((NS["C"], RDF.type, OWL.Class))
    node2 = BNode()
    g.add((node2, RDF.type, OWL.AllDisjointClasses))
    lst2 = BNode()
    Collection(g, lst2, [NS["B"], NS["C"]])
    g.add((node2, OWL.members, lst2))
    got = _axiom_inventory(g, _local, _in_domain)
    assert sorted(got["disjoint"]) == ["A, B", "B, C"]


def test_members_under_two_are_skipped():
    """멤버가 1개 이하인 그룹은 disjoint 로서 의미가 없다."""
    g = Graph()
    g.add((NS["A"], RDF.type, OWL.Class))
    node = BNode()
    g.add((node, RDF.type, OWL.AllDisjointClasses))
    lst = BNode()
    Collection(g, lst, [NS["A"]])
    g.add((node, OWL.members, lst))
    got = _axiom_inventory(g, _local, _in_domain)
    assert got["disjoint"] == []


def test_multiple_members_lists_are_all_enumerated():
    """한 노드에 ``members`` 리스트가 여럿이면 전부 본다.

    ``g.value`` 는 첫 리스트만 보므로 병합 잔여물이 있으면 놓친다 — 이 리포에서
    skolemize 병합이 members 를 중복시켜 HermiT 로드가 실패한 이력이 있다.
    """
    g = _graph_with_axioms()
    g.add((NS["C"], RDF.type, OWL.Class))
    g.add((NS["D"], RDF.type, OWL.Class))
    node = next(g.subjects(RDF.type, OWL.AllDisjointClasses))
    extra = BNode()
    Collection(g, extra, [NS["C"], NS["D"]])
    g.add((node, OWL.members, extra))            # 같은 노드에 두 번째 리스트
    got = _axiom_inventory(g, _local, _in_domain)
    assert sorted(got["disjoint"]) == ["A, B", "C, D"], (
        f"두 번째 members 리스트를 놓쳤다: {got['disjoint']}"
    )


def test_foreign_namespace_is_excluded():
    """외래 네임스페이스 선언은 도메인 축에 섞지 않는다."""
    g = _graph_with_axioms()
    foreign = Namespace("https://example.org/other#")
    g.add((foreign["X"], RDFS.subClassOf, foreign["Y"]))
    got = _axiom_inventory(g, _local, _in_domain)
    assert got["subclass"] == ["Child ⊑ Parent"]


def test_empty_axis_says_so_explicitly():
    """축이 실제로 비면 "선언 없음" 을 명시한다 — 침묵과 구분해야 한다."""
    g = Graph()
    g.add((NS["A"], RDF.type, OWL.Class))
    text = _format_declaration_inventory(g.serialize(format="turtle"))
    assert "선언 없음" in text


def test_broken_members_list_does_not_crash():
    """깨진 리스트가 있어도 나머지를 살린다 (fail-open 이 전체를 날리면 안 된다)."""
    g = _graph_with_axioms()
    node2 = BNode()
    g.add((node2, RDF.type, OWL.AllDisjointClasses))
    g.add((node2, OWL.members, Literal("리스트가 아님")))
    got = _axiom_inventory(g, _local, _in_domain)
    assert got["disjoint"] == ["A, B"]


# ── 지시: 오지적을 막는 문구가 함께 간다 ────────────────────────────────


def test_inventory_warns_against_excerpt_based_omission_claims():
    """"발췌에 없다" 를 누락 근거로 쓰지 말라는 지시가 있는가.

    목록만 주고 지시가 없으면 리뷰어가 여전히 발췌를 근거로 지적한다.
    """
    text = _format_declaration_inventory(_graph_with_axioms().serialize(format="turtle"))
    # "발췌" 는 인벤토리 머리말에도 있으므로 그것만 검사하면 이 문구를 지워도
    # 통과한다 (뮤테이션 생존 실측). **계층·공리 축을 지목하는** 문장을 확인한다.
    assert "전수 스캔" in text
    for axis_word in ("계층", "disjoint", "inverseOf", "Restriction"):
        assert axis_word in text, f"경고 문구가 {axis_word} 축을 지목하지 않는다"
    assert "목록에 **없을 때만** 누락" in text, (
        "'목록에 없을 때만 누락' 이라는 판정 기준이 빠졌다"
    )


def test_real_tbox_inventory_covers_all_axes():
    """배포 T-Box 로 전수 확인 — 합성 픽스처만으로는 실물 형태를 놓친다."""
    path = pathlib.Path("data/generated/tbox/t_box.ttl")
    if not path.exists():
        pytest.skip("배포 T-Box 없음")
    text = _format_declaration_inventory(path.read_text(encoding="utf-8"))
    g = Graph()
    g.parse(str(path), format="turtle")
    got = _axiom_inventory(g, _local, _in_domain)
    for key in ("subclass", "inverse", "disjoint", "some_values"):
        assert got[key], f"실물 T-Box 에서 {key} 축이 비었다"
    # 각 축의 첫 항목이 렌더 결과에 실제로 있는가
    for key in ("subclass", "inverse", "disjoint", "some_values"):
        assert got[key][0] in text, f"{key} 첫 항목이 인벤토리에 없다"
