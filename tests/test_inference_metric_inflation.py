"""추론 응답의 두 지표가 부풀려 보고되던 것을 고정.

## 1) ``classes_with_instances`` — 2.4배 부풀림

``rdf:type`` 의 object 를 도메인 NS 로만 걸러 세면 **클래스가 아닌 것** 까지 섞인다.
이 T-Box 는 ``owl:Restriction`` 92개를 익명 blank node 가 아니라 **이름 있는 IRI** 로
선언한다 (예: ``AlarmEvents_hasAlarmTag_someValuesFrom``). 추론기가 그 제약을
만족하는 인스턴스에 해당 IRI 를 ``rdf:type`` 으로 붙이므로 단순 집계가 그것들을
"인스턴스를 가진 클래스" 로 센다.

실측 (2026-08-17, 배포 추론 그래프 1,521,436 트리플): 159 로 보고된 값의 구성 —

  owl:Class 선언        66   ← 진짜 도메인 클래스
  owl:Restriction       92   ← 클래스식에 이름만 붙인 것
  그 외                  1   ← 도메인 NS 아래 외부(IOF) 클래스 MaintenanceActivity

## 2) ``restriction_violation_count`` — 1.57배 부풀림

``_collect_restrictions`` 는 조상 계층을 따라 올라가므로 상위 클래스에 걸린
Restriction 이 그것을 상속하는 **모든 자손에서 다시** 도달된다. 위반 여부는
``(instance, property, target)`` 로 결정되는데 보고는 도달 경로마다 한 건씩 쌓였다.

실측: 142,424 보고 vs 고유 축 **90,586**. 원인은 ``ManufacturingProcessStep`` 의
``directlyPrecedes``/``directlyFollows`` someValuesFrom 2개가 자손 5개에서 각각
도달하는 것 (51,838 축이 2개 클래스로 중복 보고).

## 왜 중요한가

두 지표 모두 품질 보고서와 커버리지 분모로 쓰인다. 클래스 수가 부풀면 실제보다
좋게 보이고, 위반 수가 경로에 비례하면 **계층을 깊게 만들 때마다 품질이 나빠 보인다**
— 개선을 벌하는 지표다.
"""
from __future__ import annotations

import pytest
from rdflib import OWL, RDF, Graph, URIRef

from domain.namespaces import DOMAIN_NS, NS_PREFIX
from tools.inference import _apply_owl_restrictions

INST = DOMAIN_NS.replace("steel-ontology#", "steel-instance#") \
    if "steel-ontology#" in DOMAIN_NS else DOMAIN_NS


def _graphs_with_inherited_restriction() -> tuple[Graph, Graph]:
    """부모에 someValuesFrom 을 걸고 자손 2개가 상속하는 최소 재현.

    인스턴스 1개가 그 제약을 위반한다 → 고유 위반은 **1건** 이지만 도달 경로는
    3개(부모 + 자손 2)다.
    """
    tbox = Graph()
    tbox.parse(data=f"""
        @prefix {NS_PREFIX}: <{DOMAIN_NS}> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        {NS_PREFIX}:Target a owl:Class .
        {NS_PREFIX}:Parent a owl:Class ;
            rdfs:subClassOf {NS_PREFIX}:ParentRestr .
        {NS_PREFIX}:ParentRestr a owl:Restriction ;
            owl:onProperty {NS_PREFIX}:linksTo ;
            owl:someValuesFrom {NS_PREFIX}:Target .
        {NS_PREFIX}:ChildA a owl:Class ; rdfs:subClassOf {NS_PREFIX}:Parent .
        {NS_PREFIX}:ChildB a owl:Class ; rdfs:subClassOf {NS_PREFIX}:Parent .
        {NS_PREFIX}:linksTo a owl:ObjectProperty .
    """, format="turtle")

    g = Graph()
    # 인스턴스는 세 클래스 전부의 타입을 갖는다 (추론 후 상태를 모사).
    g.parse(data=f"""
        @prefix {NS_PREFIX}: <{DOMAIN_NS}> .
        @prefix inst: <{DOMAIN_NS}i/> .
        inst:x a {NS_PREFIX}:Parent, {NS_PREFIX}:ChildA, {NS_PREFIX}:ChildB .
    """, format="turtle")
    return g, tbox


class TestRestrictionViolationDedup:
    def test_inherited_restriction_counted_once(self):
        """THE REGRESSION: 같은 위반이 도달 경로마다 세어지면 안 된다."""
        g, tbox = _graphs_with_inherited_restriction()
        stats = _apply_owl_restrictions(g, tbox)
        assert stats["restriction_violation_count"] == 1, (
            f"고유 위반 1건인데 {stats['restriction_violation_count']}건으로 셌다 — "
            "조상 계층 도달 경로마다 중복 집계된다"
        )

    def test_distinct_instances_still_counted_separately(self):
        """**정당한 입력 보존**: 서로 다른 인스턴스는 각각 세야 한다.

        dedup 을 너무 넓게 잡으면 진짜 위반을 감춘다 — 그쪽이 더 나쁘다.
        """
        g, tbox = _graphs_with_inherited_restriction()
        g.parse(data=f"""
            @prefix {NS_PREFIX}: <{DOMAIN_NS}> .
            @prefix inst: <{DOMAIN_NS}i/> .
            inst:y a {NS_PREFIX}:ChildA .
            inst:z a {NS_PREFIX}:ChildB .
        """, format="turtle")
        stats = _apply_owl_restrictions(g, tbox)
        assert stats["restriction_violation_count"] == 3, (
            f"인스턴스 3개가 각각 위반인데 {stats['restriction_violation_count']}건"
        )

    def test_distinct_properties_counted_separately(self):
        """같은 인스턴스라도 **다른 프로퍼티** 위반은 별건이다."""
        g, tbox = _graphs_with_inherited_restriction()
        tbox.parse(data=f"""
            @prefix {NS_PREFIX}: <{DOMAIN_NS}> .
            @prefix owl: <http://www.w3.org/2002/07/owl#> .
            @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
            {NS_PREFIX}:Parent rdfs:subClassOf {NS_PREFIX}:OtherRestr .
            {NS_PREFIX}:OtherRestr a owl:Restriction ;
                owl:onProperty {NS_PREFIX}:alsoLinksTo ;
                owl:someValuesFrom {NS_PREFIX}:Target .
            {NS_PREFIX}:alsoLinksTo a owl:ObjectProperty .
        """, format="turtle")
        stats = _apply_owl_restrictions(g, tbox)
        assert stats["restriction_violation_count"] == 2, (
            f"프로퍼티 2개 위반인데 {stats['restriction_violation_count']}건"
        )

    def test_satisfied_restriction_is_not_a_violation(self):
        """제약을 만족하면 위반이 아니다 (게이트가 무조건 세지 않는지)."""
        g, tbox = _graphs_with_inherited_restriction()
        g.parse(data=f"""
            @prefix {NS_PREFIX}: <{DOMAIN_NS}> .
            @prefix inst: <{DOMAIN_NS}i/> .
            inst:x {NS_PREFIX}:linksTo inst:t .
            inst:t a {NS_PREFIX}:Target .
        """, format="turtle")
        stats = _apply_owl_restrictions(g, tbox)
        assert stats["restriction_violation_count"] == 0


class TestNamedRestrictionsExcludedFromClassCount:
    """``classes_with_instances`` 가 이름 있는 Restriction 을 세지 않는지.

    전체 추론(6분+)을 돌리지 않고, 응답 계산과 **같은 판정 기준** 을 검증한다:
    ``owl:Restriction`` 여부로 거른다 (이름 패턴이 아니라).
    """

    def test_restriction_is_distinguishable_by_type_not_name(self):
        """이름 패턴(``_someValuesFrom`` 접미)에 의존하지 않는지.

        패턴으로 거르면 같은 이름 규칙을 쓰지 않는 도메인에서 조용히 no-op 된다.
        """
        tbox = Graph()
        tbox.parse(data=f"""
            @prefix {NS_PREFIX}: <{DOMAIN_NS}> .
            @prefix owl: <http://www.w3.org/2002/07/owl#> .
            {NS_PREFIX}:RealClass a owl:Class .
            {NS_PREFIX}:WeirdlyNamedRestriction a owl:Restriction ;
                owl:onProperty {NS_PREFIX}:p ; owl:someValuesFrom {NS_PREFIX}:RealClass .
        """, format="turtle")
        restriction_uris = {
            str(s) for s in tbox.subjects(RDF.type, OWL.Restriction)
        }
        assert str(URIRef(DOMAIN_NS + "WeirdlyNamedRestriction")) in restriction_uris, (
            "이름에 힌트가 없어도 owl:Restriction 으로 식별돼야 한다"
        )
        assert str(URIRef(DOMAIN_NS + "RealClass")) not in restriction_uris

    def test_deployed_graph_partition_is_exact(self):
        """**산출물 기반**: 배포 추론 그래프의 159 = 66 + 92 + 1 분해를 고정."""
        import os

        import config as _cfg
        from domain.tbox_utils import _new_graph, fast_parse_turtle
        inferred = getattr(_cfg, "INFERRED_PATH", "")
        if not (inferred and os.path.exists(inferred)
                and os.path.exists(_cfg.TBOX_PATH)):
            pytest.skip("추론 산출물 없음")

        g = _new_graph()
        fast_parse_turtle(g, inferred)
        tbox = _new_graph()
        fast_parse_turtle(tbox, _cfg.TBOX_PATH)

        typed = {
            str(o) for o in g.objects(None, RDF.type)
            if str(o).startswith(DOMAIN_NS)
        }
        restriction_uris = {
            str(s) for s in tbox.subjects(RDF.type, OWL.Restriction)
        }
        declared = {str(c) for c in tbox.subjects(RDF.type, OWL.Class)}

        restr_typed = typed & restriction_uris
        assert restr_typed, (
            "이름 있는 Restriction 에 타입이 붙지 않았다 — 이 테스트의 전제가 깨졌다"
        )
        # 새 계산식: Restriction 을 제외한 나머지만 클래스로 센다.
        classes = typed - restriction_uris
        assert len(classes) < len(typed), "제외가 아무 효과도 없다"
        # 남은 것은 (거의) 전부 선언된 클래스여야 한다. 외부 NS 아래 IOF 클래스가
        # 소수 섞이는 것은 정상 — 그것은 실제로 인스턴스를 가진 클래스다.
        undeclared = classes - declared
        assert len(undeclared) <= 5, (
            f"Restriction 을 걸렀는데도 미선언이 {len(undeclared)}개 남았다: "
            f"{sorted(str(u).rsplit('/', 1)[-1] for u in undeclared)[:10]}"
        )
