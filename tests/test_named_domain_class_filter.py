"""Regression: 익명 클래스 표현식의 skolem 이름이 클래스로 세어지지 않는가.

2026-08-19 S4.5 mutation 감사: ``check_quality_rules`` 가 baseline 에서 이미 FAIL
이라 **어떤 mutant 도 구분하지 못하는 죽은 게이트** 였다 (적용 7개 중 1개만 검출,
14.3%). 생존한 M5 3건(label/comment 삭제)은 정확히 이 게이트의 담당 영역인데,
baseline 이 같은 규칙으로 이미 FAIL 이어서 추가 위반이 신호로 보이지 않았다.

원인은 **한 개** 노드였다. step_28 이 만든
``ManufacturingProcessStep owl:equivalentClass [unionOf ...]`` 의 skolemize 된 본체
``Union_ManufacturingProcessStep_18a9fee9`` 가 ``owl:Class`` 로 세어져:

- ``missing_label`` @en / @ko (high 2건) — 익명 표현식에 라벨을 요구할 근거가 없다
- ``deprecated_reference`` (high 1건) — step_21 이 "자식·용처 없음" 으로
  ``owl:deprecated`` 를 붙였는데 ``ManufacturingProcessStep`` 이 참조 중이다

즉 후속 스텝이 익명 표현식을 도메인 클래스로 오인해 어노테이션을 붙이고, 게이트가
그 부재/모순을 결함으로 보고하는 **자기충족 루프** 였다.

**이 파일이 주장하는 것**: 카운터가 아니라 —
 (1) 헬퍼가 익명 표현식만 배제하고 **정상 클래스는 보존** 하는가,
 (2) 게이트가 그 노드로 baseline FAIL 이 되지 않는가,
 (3) 생성 지점(step_21)이 익명 표현식에 deprecated 를 붙이지 않는가,
 (4) 게이트가 실제 결함(라벨 삭제)은 여전히 잡는가 — 관대해진 게 아니다.
"""

import json

import pytest
from rdflib import OWL, RDF, RDFS, BNode, Graph, Literal, Namespace, URIRef

from domain.graph_utils import is_named_domain_class, named_domain_classes

NS = Namespace("http://example.com/steel-ontology#")
NS_STR = str(NS)


def _base_graph():
    """정상 클래스 1개 + 라벨/코멘트."""
    g = Graph()
    g.add((NS.EquipmentMaster, RDF.type, OWL.Class))
    g.add((NS.EquipmentMaster, RDFS.label, Literal("Equipment Master", lang="en")))
    g.add((NS.EquipmentMaster, RDFS.label, Literal("설비 마스터", lang="ko")))
    g.add((NS.EquipmentMaster, RDFS.comment, Literal("설비 기준정보", lang="ko")))
    return g


# ── 헬퍼: 익명 표현식만 배제하는가 ───────────────────────────────────────

def test_named_class_is_kept():
    """정상 명명 클래스는 True (배제하면 게이트가 눈감는다)."""
    g = _base_graph()
    assert is_named_domain_class(g, NS.EquipmentMaster, NS_STR) is True


def test_skolemized_union_is_excluded():
    """``Union_*`` skolem 이름은 False (실측 문제 노드)."""
    g = _base_graph()
    u = NS["Union_ManufacturingProcessStep_18a9fee9"]
    g.add((u, RDF.type, OWL.Class))
    g.add((u, OWL.unionOf, BNode()))
    assert is_named_domain_class(g, u, NS_STR) is False


def test_union_excluded_even_without_prefix():
    """``owl:unionOf`` 를 가지면 이름이 무엇이든 False (접두 의존 금지).

    접두(``Union_``)만 보면 다른 명명 규약에서 새어 들어온다.
    """
    g = _base_graph()
    n = NS["SomeOtherName"]
    g.add((n, RDF.type, OWL.Class))
    g.add((n, OWL.unionOf, BNode()))
    assert is_named_domain_class(g, n, NS_STR) is False


@pytest.mark.parametrize("pred", [
    OWL.unionOf, OWL.intersectionOf, OWL.complementOf, OWL.oneOf, OWL.onProperty,
])
def test_all_class_expression_predicates_excluded(pred):
    """클래스 표현식 술어를 하나라도 가지면 배제한다."""
    g = _base_graph()
    n = NS["Expr1"]
    g.add((n, RDF.type, OWL.Class))
    g.add((n, pred, BNode()))
    assert is_named_domain_class(g, n, NS_STR) is False


def test_named_restriction_excluded():
    """skolemize 된 ``owl:Restriction`` 도 배제한다."""
    g = _base_graph()
    r = NS["EquipmentMaster_hasStatus_someValuesFrom"]
    g.add((r, RDF.type, OWL.Class))
    g.add((r, RDF.type, OWL.Restriction))
    assert is_named_domain_class(g, r, NS_STR) is False


def test_foreign_namespace_excluded():
    """도메인 밖 IRI 는 False (도메인-중립)."""
    g = _base_graph()
    foreign = URIRef("https://spec.industrialontologies.org/ontology/core/Core/X")
    g.add((foreign, RDF.type, OWL.Class))
    assert is_named_domain_class(g, foreign, NS_STR) is False


def test_bnode_excluded():
    """BNode 는 False."""
    g = _base_graph()
    b = BNode()
    g.add((b, RDF.type, OWL.Class))
    assert is_named_domain_class(g, b, NS_STR) is False


def test_named_domain_classes_set():
    """집합 헬퍼가 정상 클래스만 담는다."""
    g = _base_graph()
    u = NS["Union_X_deadbeef"]
    g.add((u, RDF.type, OWL.Class))
    g.add((u, OWL.unionOf, BNode()))
    got = named_domain_classes(g, NS_STR)
    assert got == {NS.EquipmentMaster}, f"예상과 다름: {got}"


# ── 게이트: baseline FAIL 이 풀렸는가 / 실제 결함은 여전히 잡는가 ────────

def _gate(g: Graph):
    from tools.validation_core import check_quality_rules
    d = json.loads(check_quality_rules(ttl_content=g.serialize(format="turtle")))
    return d["critical"], d["high"]


def test_gate_ignores_skolemized_union_labels():
    """익명 표현식에 라벨이 없어도 게이트가 high 를 내지 않는다.

    이것이 baseline FAIL 고정의 직접 원인이었다 — 게이트가 죽으면 mutation 을
    구분할 수 없고, 그 침묵이 진짜 결함(M5 3건)을 덮는다.
    """
    g = _base_graph()
    u = NS["Union_ManufacturingProcessStep_18a9fee9"]
    g.add((u, RDF.type, OWL.Class))
    g.add((u, OWL.unionOf, BNode()))          # 라벨/코멘트 없음 — 정상이다
    _, high = _gate(g)
    assert high == 0, "익명 표현식의 라벨 부재로 high 가 발생했다 (게이트 마비 재발)"


def test_gate_still_catches_real_missing_label():
    """**정상 클래스** 의 라벨 누락은 여전히 high 로 잡는다 (관대해진 게 아니다).

    이 테스트가 없으면 "게이트를 조용하게 만들어 통과시킨" 것과 구분되지 않는다.
    """
    g = _base_graph()
    for o in list(g.objects(NS.EquipmentMaster, RDFS.label)):
        g.remove((NS.EquipmentMaster, RDFS.label, o))
    _, high = _gate(g)
    assert high >= 2, f"정상 클래스의 @en/@ko 라벨 누락을 놓쳤다 (high={high})"


def test_gate_detects_label_deletion_as_delta():
    """라벨 삭제가 baseline 대비 **델타** 를 만든다 (mutation 검출의 조건).

    baseline 이 이미 FAIL 이면 델타가 없어 mutant 가 생존한다 — 실측 M5 3건이
    그렇게 살아남았다.
    """
    g = _base_graph()
    u = NS["Union_X_deadbeef"]
    g.add((u, RDF.type, OWL.Class))
    g.add((u, OWL.unionOf, BNode()))
    base = _gate(g)

    mutant = Graph()
    for t in g:
        mutant.add(t)
    for o in list(mutant.objects(NS.EquipmentMaster, RDFS.label)):
        mutant.remove((NS.EquipmentMaster, RDFS.label, o))
    mut = _gate(mutant)

    assert base[1] == 0, f"baseline 이 이미 high FAIL (게이트 죽음): {base}"
    assert mut != base, f"mutant 가 델타를 만들지 못했다: base={base} mut={mut}"


# ── 생성 지점: step_21 이 익명 표현식에 deprecated 를 붙이지 않는가 ──────

def test_step21_does_not_deprecate_anonymous_expression():
    """step_21 이 ``Union_*`` 에 ``owl:deprecated`` 를 붙이지 않는다.

    붙이면 게이트가 ``deprecated_reference`` high 를 낸다 (참조자가 실재하므로).
    게이트만 고치고 생성 지점을 놔두면 다음 S3 실행이 같은 오염을 다시 만든다.
    """
    from tools.quality_steps import step_21_over_engineered_dep as step
    from tools.quality_steps._base import StepContext

    g = _base_graph()
    u = NS["Union_ManufacturingProcessStep_18a9fee9"]
    g.add((u, RDF.type, OWL.Class))
    g.add((u, OWL.unionOf, BNode()))
    # 참조자: equivalentClass 로 실제 사용 중
    g.add((NS.ManufacturingProcessStep, RDF.type, OWL.Class))
    g.add((NS.ManufacturingProcessStep, OWL.equivalentClass, u))

    step.apply(g, StepContext(domain_ns=NS_STR))
    assert (u, OWL.deprecated, Literal(True)) not in g, (
        "익명 표현식에 deprecated 가 붙었다 — 게이트가 deprecated_reference 를 낸다"
    )


def test_step21_still_deprecates_real_orphan_class():
    """**정상** 고립 클래스는 여전히 deprecated 로 표시한다 (기능 보존).

    익명 표현식 배제가 step_21 의 본래 기능을 무력화하지 않았는지 확인한다.
    """
    from tools.quality_steps import step_21_over_engineered_dep as step
    from tools.quality_steps._base import StepContext

    g = _base_graph()
    orphan = NS["TrulyUnusedClass"]
    g.add((orphan, RDF.type, OWL.Class))
    g.add((orphan, RDFS.label, Literal("Unused", lang="en")))
    g.add((orphan, RDFS.label, Literal("미사용", lang="ko")))

    res = step.apply(g, StepContext(domain_ns=NS_STR))
    assert (orphan, OWL.deprecated, Literal(True)) in g, (
        f"정상 고립 클래스를 표시하지 않았다 — 기능이 죽었다 (stats={res.stats})"
    )
