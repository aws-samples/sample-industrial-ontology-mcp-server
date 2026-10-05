"""LUBM-style 추론기 regression test.

근거: Guo, Pan, Heflin (2005). "LUBM: A Benchmark for OWL Knowledge Base
Systems." J. Web Semantics 3(2-3):158-182. DOI:10.1016/j.websem.2005.06.005

풀 LUBM Java 생성기 대신 tests/fixtures/tiny_lubm.ttl 축소판 사용.
목적: run_owl_rl_inference 리팩터링 시 OWL 2 RL 추론기 (reasonable) 의
soundness 회귀 방지.

검증 패턴 (OWL 2 RL 표준 entailment):
- rdfs:subClassOf transitive: GraduateStudent → Student → Person
- rdfs:domain 추론: worksFor domain=Employee → subject=Employee
- rdfs:range 추론: advisor range=Professor → object=Professor
- owl:TransitiveProperty: subOrganizationOf transitive closure
- owl:inverseOf: hasAlumnus/degreeFrom 양방향
"""
from __future__ import annotations

from pathlib import Path

import pytest
from rdflib import RDF, URIRef

from domain.tbox_utils import _new_graph

_FIXTURE = Path(__file__).parent / "fixtures" / "tiny_lubm.ttl"
UB = "http://example.org/univ-bench#"
UBI = "http://example.org/univ-bench/instances#"


@pytest.fixture(scope="module")
def inferred_graph():
    """reasonable PyReasoner 로 tiny_lubm 을 한 번만 추론."""
    try:
        from reasonable import PyReasoner
    except ImportError:
        pytest.skip("reasonable 미설치")

    g = _new_graph()
    g.parse(str(_FIXTURE), format="turtle")
    reasoner = PyReasoner()
    reasoner.from_graph(g)
    for t in reasoner.reason():
        if t not in g:
            g.add(t)
    return g


# ── Subclass transitive closure ──


def test_subclass_chain_professor_to_person(inferred_graph):
    """GraduateStudent → Student → Person 3-hop 추론."""
    g = inferred_graph
    grad = URIRef(UBI + "grad1")
    assert (grad, RDF.type, URIRef(UB + "GraduateStudent")) in g
    assert (grad, RDF.type, URIRef(UB + "Student")) in g, \
        "subClassOf 추론 실패: grad1 is not Student"
    assert (grad, RDF.type, URIRef(UB + "Person")) in g, \
        "subClassOf 체인 추론 실패: grad1 is not Person"


def test_professor_is_person(inferred_graph):
    """Professor → Faculty → Employee → Person 4-hop."""
    g = inferred_graph
    prof = URIRef(UBI + "prof1")
    for cls in ("Professor", "Faculty", "Employee", "Person"):
        assert (prof, RDF.type, URIRef(UB + cls)) in g, \
            f"prof1 is not inferred as {cls}"


# ── Domain/Range 추론 ──


def test_domain_worksFor_implies_employee(inferred_graph):
    """worksFor domain=Employee → subject 모두 Employee 추론."""
    g = inferred_graph
    prof = URIRef(UBI + "prof1")
    emp = URIRef(UBI + "employee1")
    emp_cls = URIRef(UB + "Employee")
    assert (prof, RDF.type, emp_cls) in g
    assert (emp, RDF.type, emp_cls) in g


def test_range_advisor_implies_professor(inferred_graph):
    """advisor range=Professor → object 타입 추론."""
    g = inferred_graph
    prof = URIRef(UBI + "prof1")
    assert (prof, RDF.type, URIRef(UB + "Professor")) in g


# ── TransitiveProperty ──


def test_transitive_subOrganizationOf(inferred_graph):
    """subOrganizationOf 가 TransitiveProperty 이므로
    AI_Group subOrganizationOf CS_Dept subOrganizationOf University0
    → AI_Group subOrganizationOf University0 추론."""
    g = inferred_graph
    ai = URIRef(UBI + "AI_Group")
    univ = URIRef(UBI + "University0")
    sub_op = URIRef(UB + "subOrganizationOf")
    assert (ai, sub_op, univ) in g, \
        "TransitiveProperty 체인 추론 실패"


# ── inverseOf ──


def test_inverse_of_hasAlumnus(inferred_graph):
    """degreeFrom inverseOf hasAlumnus → 역방향 트리플 추론.
    grad1 degreeFrom University0 → University0 hasAlumnus grad1"""
    g = inferred_graph
    grad = URIRef(UBI + "grad1")
    univ = URIRef(UBI + "University0")
    has_alum = URIRef(UB + "hasAlumnus")
    assert (univ, has_alum, grad) in g, "inverseOf 역방향 추론 실패"


# ── Cardinality sanity ──


def test_inferred_triple_count_sane(inferred_graph):
    """추론이 폭발적이면 OWL RL closure 가 잘못된 것."""
    g = inferred_graph
    count = len(g)
    # tiny_lubm 은 약 40~50 explicit triples, 추론 후 100~500 수준이 합리.
    # 이상 있으면 closure 폭발 또는 추론 버그.
    assert 60 < count < 2000, \
        f"추론 triple 수 이상: {count} (예상 범위 60~2000)"


# ── 추론 후 Transitive type closure 회귀 ──


def test_all_students_are_persons(inferred_graph):
    """모든 Student 가 Person 으로 분류돼야 함."""
    g = inferred_graph
    student_cls = URIRef(UB + "Student")
    person_cls = URIRef(UB + "Person")
    students = list(g.subjects(RDF.type, student_cls))
    assert len(students) >= 2, f"Student 인스턴스가 충분치 않음: {len(students)}"
    for s in students:
        assert (s, RDF.type, person_cls) in g, \
            f"{s} is Student but not Person (subClassOf 추론 실패)"
