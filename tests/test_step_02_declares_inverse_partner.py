"""Step 2 가 ``owl:inverseOf`` 로 참조하는 프로퍼티를 **선언** 하는지 고정.

## 배경 (2026-08-17 실측)

``step_02`` 는 설정 선언 쌍 ``(fwd, inv)`` 중 **한쪽이라도** 존재하면 양방향
``owl:inverseOf`` 를 보장한다. 그런데 ``owl:ObjectProperty`` 선언은 넣지 않았다.

그래서 T-Box 가 **스스로 미선언 술어를 만들었다**: S3 재실행이
``followedBy owl:inverseOf precededBy`` 를 주입했지만 ``precededBy`` 는 어디에도
``a owl:ObjectProperty`` 로 선언되지 않아 S9 의 undeclared_op check 가 FAIL 했다
(미선언 1건, usage 3).

## 왜 이제야 터졌나 — "no-op 이니 안전하다" 는 주석의 수명

step_02 docstring 은 "그 네 이름은 T-Box 에 없어 **확정적 no-op**" 이라고 적어뒀다.
그 전제는 **다른 스텝의 산출물에 의존** 했고, step_22 의 중복 OP 통합이
``followedBy`` 를 승자로 남기면서(경쟁자 ``directlyFollows`` / ``directlyPrecedes``
제거) 깨졌다. S3 스텝들이 서로의 입력을 만든다는 것의 또 다른 사례다
(한 수정이 다음 결함의 입력을 만드는 회귀).

## domain/range 를 채우지 않는 이유

짝의 domain/range 를 뒤집어 넣으면 방향 오류를 복제할 위험이 있다 — ``step_09b`` 가
정확히 그 방식으로 뒤집힌 방향을 전파한 전례가 있다. 선언만 하고, 근거를 갖고
채우는 일은 ``step_09a`` 에 남긴다.
"""
from __future__ import annotations

import pytest
from rdflib import OWL, RDF, RDFS, Graph, URIRef

from domain.namespaces import DOMAIN_NS, NS_PREFIX
from tools.quality_steps import step_02_inverse_bidirectional as step
from tools.quality_steps._base import StepContext


def D(name: str) -> URIRef:
    return URIRef(DOMAIN_NS + name)


def _ctx() -> StepContext:
    return StepContext(domain_ns=DOMAIN_NS)


def _graph_with_forward_only() -> Graph:
    """``followedBy`` 만 선언된 상태 — 실측 재현 (step_22 통합 후의 T-Box)."""
    g = Graph()
    g.parse(data=f"""
        @prefix {NS_PREFIX}: <{DOMAIN_NS}> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        {NS_PREFIX}:ManufacturingProcessStep a owl:Class .
        {NS_PREFIX}:followedBy a owl:ObjectProperty ;
            rdfs:domain {NS_PREFIX}:ManufacturingProcessStep ;
            rdfs:range {NS_PREFIX}:ManufacturingProcessStep .
    """, format="turtle")
    return g


class TestInversePartnerIsDeclared:
    def test_partner_gets_object_property_declaration(self):
        """THE REGRESSION: 참조만 하고 선언을 빼면 미선언 술어가 생긴다."""
        g = _graph_with_forward_only()
        step.apply(g, _ctx())
        assert (D("followedBy"), OWL.inverseOf, D("precededBy")) in g, (
            "inverseOf 주입 자체가 안 됐다 — 이 테스트의 전제 확인"
        )
        assert (D("precededBy"), RDF.type, OWL.ObjectProperty) in g, (
            "precededBy 를 inverseOf 로 참조하면서 owl:ObjectProperty 선언을 "
            "안 했다 — validate_kg 의 undeclared_op 가 FAIL 한다"
        )

    def test_both_sides_declared(self):
        """양쪽 모두 선언 — 짝의 존재 여부와 무관하게 대칭."""
        g = _graph_with_forward_only()
        step.apply(g, _ctx())
        for name in ("followedBy", "precededBy"):
            assert (D(name), RDF.type, OWL.ObjectProperty) in g, name

    def test_domain_range_not_fabricated(self):
        """**정당한 입력 보존**: domain/range 는 추측해서 채우지 않는다.

        짝의 것을 뒤집어 넣으면 방향 오류가 복제된다 (step_09b 전례).
        근거 있는 채움은 step_09a 의 일이다.
        """
        g = _graph_with_forward_only()
        step.apply(g, _ctx())
        assert not list(g.objects(D("precededBy"), RDFS.domain)), (
            "domain 을 추측해서 채웠다 — 방향 오류 복제 위험"
        )
        assert not list(g.objects(D("precededBy"), RDFS.range))

    def test_noop_when_neither_side_exists(self):
        """어느 쪽도 없으면 아무것도 만들지 않는다 (유령 OP 생성 금지)."""
        g = Graph()
        g.parse(data=(
            f"@prefix {NS_PREFIX}: <{DOMAIN_NS}> .\n"
            "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
            f"{NS_PREFIX}:Unrelated a owl:Class .\n"
        ), format="turtle")
        before = len(g)
        step.apply(g, _ctx())
        assert len(g) == before, (
            "선언 쌍 어느 쪽도 없는데 OP 를 만들었다 — 0-instance 유령 OP"
        )

    def test_existing_declaration_not_duplicated(self):
        """이미 선언돼 있으면 중복 추가하지 않는다 (멱등)."""
        g = _graph_with_forward_only()
        g.add((D("precededBy"), RDF.type, OWL.ObjectProperty))
        step.apply(g, _ctx())
        assert len(list(g.triples(
            (D("precededBy"), RDF.type, OWL.ObjectProperty)))) == 1

    def test_idempotent_across_two_runs(self):
        """두 번 돌려도 같은 그래프 — S3 재실행 경로 안전성."""
        g = _graph_with_forward_only()
        step.apply(g, _ctx())
        size1 = len(g)
        step.apply(g, _ctx())
        assert len(g) == size1, "2회차에서 트리플이 늘었다 — 비멱등"


class TestDeployedTboxHasNoUndeclaredInversePartner:
    """**산출물 기반**: 배포 T-Box 에 inverseOf 로만 등장하는 OP 가 없는지."""

    def test_every_inverse_referent_is_declared(self):
        import os

        import config as _cfg
        if not os.path.exists(_cfg.TBOX_PATH):
            pytest.skip("배포 T-Box 없음")
        g = Graph()
        g.parse(_cfg.TBOX_PATH, format="turtle")

        undeclared = []
        for s, o in g.subject_objects(OWL.inverseOf):
            for node in (s, o):
                if not (isinstance(node, URIRef)
                        and str(node).startswith(DOMAIN_NS)):
                    continue
                if (node, RDF.type, OWL.ObjectProperty) not in g:
                    undeclared.append(str(node).rsplit("#", 1)[-1])
        assert not undeclared, (
            "inverseOf 로 참조되지만 owl:ObjectProperty 선언이 없는 프로퍼티: "
            f"{sorted(set(undeclared))} — S9 undeclared_op FAIL 의 원인"
        )
