"""정의 클래스(equivalentClass) 를 게이트가 인식하고 분류 DP 를 Functional 로 지킨다.

2026-08-27 실측. 값 기반 서브클래스를 살린 뒤(a77bde4 / cc9dbd3) 남은 위험 2건을
처리한다. 둘 다 "정상 설계를 결함으로 읽거나, 결함을 못 잡는" 형태다.

## 위험 1 — 게이트가 equivalentClass 축을 못 본다

``check_class_instance_count`` 의 ``derivable`` 판정이 ``rdfs:subClassOf`` **만**
스캔했다. 실측:

    subClassOf 축 derivable      22개  ← EquipmentStatus / GHGEmission (부모, 인스턴스 많음)
    equivalentClass 축 derivable 22개  ← 값 기반 서브클래스 (인스턴스 0건)
    교집합                        **0개**

두 집합이 전혀 다른 클래스다. 놓친 22개는 정확히 "추론이 채우는 서브클래스" 이고, 게이트는
그것을 ``truly_orphan`` 으로 세어 ``no_instance_ratio 40.6%`` / ``passed=False`` 를
보고했다. 수정 후: derivable 0 → **22**, truly_orphan 28 → **6**, ratio 40.6% → **12.8%**.

남은 6개는 실제 갭이다 (CSV 컬럼 없음 / 수치 임계 필요).

Turtle 소스가 문자열로 저장된 공리는 ``derivable`` 로 세지 않는다 — 추론에 무의미하므로
그것을 정상으로 위장하면 cc9dbd3 이 고친 결함이 다시 숨는다.

## 위험 2 — 분류 DP 가 FunctionalProperty 가 아니다

값 기반 서브클래스는 ``equivalentClass [ onProperty :dp ; hasValue "V" ]`` 로 정의되고,
한 개체가 같은 DP 에 값을 **두 개** 가지면 서로 disjoint 인 두 서브클래스에 동시 배정돼
**unsatisfiable** 이 된다. 배포 T-Box 실측: 분류 DP **11개 중 Functional 0개**.

A-Box 다중값은 현재 0/10 이라 위반이 실현되지는 않았지만 **아무 장치도 지키지 않았다** —
CSV 변경이나 tacit 추가로 조용히 unsat 이 된다. ``FunctionalProperty`` 로 선언하면
``check_functional_violations`` 가 잡는다(선언이 없으면 그 게이트는 "선언 없음" 으로
통과한다). 즉 **잠재 위험이 게이트로 전환**된다.

### DP 이므로 안전하다 (IFP 와 다르다)

``InverseFunctionalProperty`` 는 이 파이프라인에서 금지돼 있다(`reasonable` prp-ifp
오작동 → sameAs 폭발). ``FunctionalProperty`` 를 **DP** 에 붙이는 것은 다르다 — 같은 값을
가진 개체 3개로 최소 예제를 돌려 파생 17 → 18, ``owl:sameAs`` **0건** 을 확인했다.

### 판정은 A-Box 실측 단일값이다

T-Box 에 공리가 있다는 것만으로 선언하지 않는다. 다중값 DP 를 Functional 로 만들면 기존
데이터가 즉시 위반이 되고, 그것은 게이트를 켜는 것이 아니라 데이터를 깨는 것이다.
A-Box 를 못 읽으면 **마킹하지 않는다**.

스캔은 rdflib 파싱을 쓰지 않는다 — 이 리포는 533MB A-Box 파싱으로 step 22 가 사실상
정지한 이력이 있다(수 분 + 수 GB RSS). 순차 읽기로 0.8초에 rdflib 와 같은 결과를 얻는다.
"""
from __future__ import annotations

import pytest
from rdflib import OWL, RDF, RDFS, BNode, Graph, Literal, Namespace, URIRef

from domain.namespaces import DOMAIN_NS, bind_namespaces
from tools.quality_steps import step_13_pk_functional_someValuesFrom as s13
from tools.validation_support.checks.structural import check_class_instance_count

NS = Namespace(str(DOMAIN_NS))


def _defined_class_tbox(*, as_equivalent: bool = True,
                        as_literal: bool = False) -> Graph:
    """부모 + 정의 서브클래스 하나를 가진 최소 T-Box."""
    g = Graph()
    bind_namespaces(g)
    g.add((NS["Parent"], RDF.type, OWL.Class))
    g.add((NS["Defined"], RDF.type, OWL.Class))
    g.add((NS["Defined"], RDFS.subClassOf, NS["Parent"]))
    g.add((NS["dp"], RDF.type, OWL.DatatypeProperty))
    g.add((NS["dp"], RDFS.domain, NS["Parent"]))
    if as_literal:
        # cc9dbd3 이 고친 결함 형태 — Turtle 소스가 문자열로 저장된 것.
        g.add((NS["Defined"], OWL.equivalentClass,
               Literal('[ a owl:Restriction ; owl:onProperty steel:dp ]')))
        return g
    node = BNode()
    g.add((node, RDF.type, OWL.Restriction))
    g.add((node, OWL.onProperty, NS["dp"]))
    g.add((node, OWL.hasValue, Literal("V")))
    g.add((NS["Defined"], OWL.equivalentClass if as_equivalent else RDFS.subClassOf,
           node))
    return g


def _abox_with_parent_only() -> Graph:
    """부모 인스턴스만 있는 A-Box (서브클래스는 추론이 채운다)."""
    g = Graph()
    bind_namespaces(g)
    inst = URIRef(str(DOMAIN_NS) + "i1")
    g.add((inst, RDF.type, NS["Parent"]))
    g.add((inst, NS["dp"], Literal("V")))
    return g


# ── 위험 1: 게이트가 equivalentClass 를 인식한다 ────────────────────────


def test_equivalent_class_definition_counts_as_derivable():
    """THE REGRESSION: ``equivalentClass`` 로 정의된 빈 클래스는 고아가 아니다."""
    result = check_class_instance_count(
        _abox_with_parent_only(), tbox=_defined_class_tbox(),
    )
    assert "Defined" in result["no_instance_derivable"], (
        "equivalentClass 정의 클래스가 derivable 로 인식되지 않았다"
    )
    assert "Defined" not in result["no_instance_truly_orphan"]


def test_subclass_restriction_still_counts():
    """``subClassOf`` 축도 그대로 인식한다 — 축 추가가 기존 판정을 깨지 않는가."""
    result = check_class_instance_count(
        _abox_with_parent_only(),
        tbox=_defined_class_tbox(as_equivalent=False),
    )
    assert "Defined" in result["no_instance_derivable"]


def test_literal_stored_axiom_is_not_derivable():
    """문자열로 저장된 공리는 derivable 이 **아니다**.

    추론에 무의미하므로 정상으로 위장하면 cc9dbd3 이 고친 결함이 다시 숨는다.
    """
    result = check_class_instance_count(
        _abox_with_parent_only(), tbox=_defined_class_tbox(as_literal=True),
    )
    assert "Defined" not in result["no_instance_derivable"], (
        "Turtle 소스 문자열이 derivable 로 세어졌다 — 결함이 정상으로 위장된다"
    )
    assert "Defined" in result["no_instance_truly_orphan"]


def test_unreachable_hasvalue_is_not_derivable(tmp_path):
    """``hasValue`` 값이 A-Box 에 없으면 derivable 이 **아니다**.

    실측: 정의 클래스 22개 중 **9개가 값 불일치로 영구 0건** 이다 —
    공리 ``alarmEventsSeverity = "HIGH"`` 인데 데이터는 ``"High"``,
    ``inventoryTransactionType = "IN"`` 인데 ``"Inbound"``,
    ``realTimeDataQualityCode = "BAD"`` 인데 숫자 코드 ``0/1/2``.

    이것을 derivable 로 세면 **도달 불가 공리를 정상으로 위장**하고 이 게이트가 유일한
    발견 경로였는데 조용해진다. 값을 데이터에 맞춰 고치는 것은 "지표 매수" 이므로,
    게이트가 알리고 사람이 판단해야 한다.
    """
    tbox = _defined_class_tbox()          # hasValue "V"
    abox = Graph()
    bind_namespaces(abox)
    inst = URIRef(str(DOMAIN_NS) + "i1")
    abox.add((inst, RDF.type, NS["Parent"]))
    abox.add((inst, NS["dp"], Literal("DIFFERENT")))   # 공리 값과 다르다
    result = check_class_instance_count(abox, tbox=tbox)
    assert "Defined" not in result["no_instance_derivable"], (
        "도달 불가 공리가 derivable 로 세어졌다"
    )
    assert "Defined" in result["no_instance_unreachable_axiom"]
    assert "Defined" in result["no_instance_truly_orphan"]


def test_reachable_hasvalue_stays_derivable():
    """값이 실재하면 derivable 이다 — 도달성 검사가 과잉 차단하지 않는가."""
    result = check_class_instance_count(
        _abox_with_parent_only(), tbox=_defined_class_tbox(),
    )
    assert "Defined" in result["no_instance_derivable"]
    assert result["no_instance_unreachable_axiom"] == []


def test_typed_and_plain_literal_match():
    """공리가 ``"V"^^xsd:string`` 이고 A-Box 가 평문 ``"V"`` 여도 도달 가능하다.

    RDF 1.1 에서 둘은 같은 term 이고 추론기도 둘 다 분류한다(실측). rdflib 의 ``==`` 는
    datatype 을 구분하므로 문자열 비교를 써야 실제 동작과 일치한다 — 이 축이 없으면
    정상 공리를 "도달 불가" 로 오보고한다.
    """
    from rdflib import XSD
    tbox = Graph()
    bind_namespaces(tbox)
    tbox.add((NS["Parent"], RDF.type, OWL.Class))
    tbox.add((NS["Defined"], RDF.type, OWL.Class))
    tbox.add((NS["Defined"], RDFS.subClassOf, NS["Parent"]))
    tbox.add((NS["dp"], RDF.type, OWL.DatatypeProperty))
    node = BNode()
    tbox.add((node, RDF.type, OWL.Restriction))
    tbox.add((node, OWL.onProperty, NS["dp"]))
    tbox.add((node, OWL.hasValue, Literal("V", datatype=XSD.string)))
    tbox.add((NS["Defined"], OWL.equivalentClass, node))
    result = check_class_instance_count(_abox_with_parent_only(), tbox=tbox)
    assert "Defined" in result["no_instance_derivable"], (
        "타입 리터럴 vs 평문 리터럴 차이로 오보고했다"
    )


def test_deployed_tbox_classifies_empty_classes_by_cause():
    """배포 T-Box 실측 — 빈 클래스의 **원인 분류가 작동하는가**.

    ## 과거 건수를 요구하지 않는다 (2026-08-31 정정)

    예전에는 ``len(no_instance_unreachable_axiom) >= 5`` 를 요구했다. 그러면 **값
    불일치 공리가 줄면 테스트가 실패한다** — 결함을 고치는 것이 회귀로 보고되는
    형태다. 실제로 그렇게 됐다: S2 재생성으로 3건이 되어 깨졌다.

    이 리포의 원칙 — "0회 발동 게이트는 0회로 정직하게 보고하라 / 과거 발동 횟수를
    테스트에서 요구하지 말고 기록만 하라". 그래서 **분류가 작동하는지** 를 주장한다:
    빈 클래스가 있으면 원인 축(도달불가 / 파생가능 / 진짜 고아)이 채워지고 서로
    배타적인가. 건수는 줄어드는 것이 목표다.
    """
    import pathlib
    tb_path = pathlib.Path("data/generated/tbox/t_box.ttl")
    ab_path = pathlib.Path("data/generated/abox/a_box.ttl")
    if not (tb_path.exists() and ab_path.exists()):
        pytest.skip("배포 산출물 없음")
    tbox = Graph()
    tbox.parse(str(tb_path), format="turtle")
    abox = Graph()
    abox.parse(str(ab_path), format="turtle")
    result = check_class_instance_count(abox, tbox=tbox)

    empty = set(result.get("no_instance_classes") or [])
    if not empty:
        pytest.skip("빈 클래스가 없다 — 분류할 대상이 없다(정상)")

    unreachable = set(result.get("no_instance_unreachable_axiom") or [])
    derivable = set(result.get("no_instance_derivable") or [])
    orphan = set(result.get("no_instance_truly_orphan") or [])

    # 축이 채워지는가 — 세 축이 모두 비면 분류기가 죽은 것이다.
    assert unreachable or derivable or orphan, (
        f"빈 클래스 {len(empty)}개인데 원인 분류가 전부 비었다 — 분류기가 죽었다"
    )
    # 분류 결과는 빈 클래스의 부분집합이어야 한다 (엉뚱한 클래스를 세지 않는다).
    for name, axis in (("unreachable", unreachable), ("derivable", derivable),
                       ("orphan", orphan)):
        assert axis <= empty, (
            f"{name} 축이 빈 클래스가 아닌 것을 포함한다: {sorted(axis - empty)}"
        )
    # derivable 과 unreachable 은 배타적이다 (추론이 채울 수 있으면서 동시에 도달
    # 불가일 수 없다) — 겹치면 조치 판단이 흐려진다.
    assert not (derivable & unreachable), (
        f"derivable 과 unreachable 이 겹친다: {sorted(derivable & unreachable)}"
    )


def test_bodyless_restriction_is_not_derivable():
    """내용 없는 ``owl:Restriction`` 은 derivable 이 아니다.

    ``onProperty`` 만 있고 ``hasValue``/``someValuesFrom``/``allValuesFrom``/``onClass``
    가 없으면 추론기가 개체를 분류할 근거가 없다. 배포 T-Box 에 **6건** 존재하므로
    (실측) 이 검사를 지우면 그 클래스들이 "추론이 채울 것" 으로 위장된다.
    """
    tbox = Graph()
    bind_namespaces(tbox)
    tbox.add((NS["Parent"], RDF.type, OWL.Class))
    tbox.add((NS["Defined"], RDF.type, OWL.Class))
    tbox.add((NS["Defined"], RDFS.subClassOf, NS["Parent"]))
    node = BNode()
    tbox.add((node, RDF.type, OWL.Restriction))
    tbox.add((node, OWL.onProperty, NS["dp"]))       # body 없음
    tbox.add((NS["Defined"], OWL.equivalentClass, node))
    result = check_class_instance_count(_abox_with_parent_only(), tbox=tbox)
    assert "Defined" not in result["no_instance_derivable"], (
        "내용 없는 Restriction 이 derivable 로 세어졌다"
    )
    assert "Defined" in result["no_instance_truly_orphan"]


def test_literal_axiom_is_rejected_by_two_guards():
    """문자열 공리는 **두 겹**으로 막힌다 — 한쪽이 사라져도 다른 쪽이 잡는가.

    ``isinstance(target, Literal)`` 배제를 지워도 Literal 은 ``owl:Restriction`` 타입이
    없으므로 다음 검사가 걸러낸다(실측). 이 테스트는 그 이중 방어를 **의도로 고정**한다 —
    누가 뒤쪽 검사까지 느슨하게 하면 여기가 깨진다.
    """
    tbox = _defined_class_tbox(as_literal=True)
    literal_target = next(iter(tbox.objects(NS["Defined"], OWL.equivalentClass)))
    assert isinstance(literal_target, Literal)
    # 두 번째 방어선: Literal 은 Restriction 타입 선언을 가질 수 없다.
    assert (literal_target, RDF.type, OWL.Restriction) not in tbox
    result = check_class_instance_count(
        _abox_with_parent_only(), tbox=tbox,
    )
    assert "Defined" not in result["no_instance_derivable"]


def test_plain_empty_class_is_still_orphan():
    """정의 공리가 없는 빈 클래스는 그대로 고아다 — 완화가 과도하지 않은가.

    이 주장이 없으면 ``derivable`` 을 전체 클래스로 만들어도 위 테스트가 통과한다.
    """
    g = _defined_class_tbox()
    g.add((NS["NoAxiom"], RDF.type, OWL.Class))
    g.add((NS["NoAxiom"], RDFS.subClassOf, NS["Parent"]))
    result = check_class_instance_count(_abox_with_parent_only(), tbox=g)
    assert "NoAxiom" in result["no_instance_truly_orphan"]


def test_deployed_tbox_orphans_drop():
    """배포 T-Box 실측 — 오보고가 실제로 줄어드는가 (합성 픽스처만으론 부족)."""
    import pathlib
    tb_path = pathlib.Path("data/generated/tbox/t_box.ttl")
    ab_path = pathlib.Path("data/generated/abox/a_box.ttl")
    if not (tb_path.exists() and ab_path.exists()):
        pytest.skip("배포 산출물 없음")
    tbox = Graph()
    tbox.parse(str(tb_path), format="turtle")
    abox = Graph()
    abox.parse(str(ab_path), format="turtle")
    result = check_class_instance_count(abox, tbox=tbox)
    # equivalentClass 축 인식 전에는 derivable 이 **0** 이었다. 도달성 검사가 값
    # 불일치 9건을 걸러내므로 22 → 13 이 된다 (그 9건은 unreachable_axiom 으로 간다).
    assert len(result["no_instance_derivable"]) >= 10, (
        f"derivable 이 {len(result['no_instance_derivable'])}개뿐이다 "
        "(equivalentClass 축이 인식되지 않는다)"
    )
    # truly_orphan 은 28 → 15 로 줄었다. 남은 15 = 공리 없는 진짜 갭 6 +
    # 도달 불가 공리 9 이고, 후자는 별도 필드로 원인이 구분된다.
    assert len(result["no_instance_truly_orphan"]) <= 20, (
        f"truly_orphan {len(result['no_instance_truly_orphan'])}개 — 오보고가 남았다"
    )
    unreachable = set(result["no_instance_unreachable_axiom"])
    assert unreachable <= set(result["no_instance_truly_orphan"]), (
        "도달 불가 공리가 truly_orphan 에 포함되지 않는다 (누락 위험)"
    )


# ── 위험 2: 분류 DP 가 FunctionalProperty 로 보호된다 ───────────────────


def test_classifying_dp_is_marked_functional(monkeypatch, tmp_path):
    """THE REGRESSION: 정의 클래스가 쓰는 DP 가 Functional 로 선언된다."""
    abox = tmp_path / "a_box.ttl"
    abox.write_text(
        f"@prefix steel: <{DOMAIN_NS}> .\n"
        f"@prefix steel-inst: <{DOMAIN_NS}instances#> .\n"
        "steel-inst:i1 a steel:Parent ;\n"
        '    steel:dp "V" .\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("config.ABOX_PATH", str(abox))
    g = _defined_class_tbox()
    stats = s13._mark_classifying_dps_functional(g, str(DOMAIN_NS))
    assert stats["classifying_dp_functional_marked"] == 1
    assert (NS["dp"], RDF.type, OWL.FunctionalProperty) in g


def test_multivalued_dp_is_not_marked(monkeypatch, tmp_path):
    """A-Box 에 다중값이 있으면 **선언하지 않는다.**

    선언하면 기존 데이터가 즉시 위반이 된다 — 게이트를 켜는 것이 아니라 데이터를 깨는
    것이다. 목록은 남겨 사람이 판단하게 한다.
    """
    abox = tmp_path / "a_box.ttl"
    abox.write_text(
        f"@prefix steel: <{DOMAIN_NS}> .\n"
        f"@prefix steel-inst: <{DOMAIN_NS}instances#> .\n"
        "steel-inst:i1 a steel:Parent ;\n"
        '    steel:dp "V" ;\n'
        '    steel:dp "W" .\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("config.ABOX_PATH", str(abox))
    g = _defined_class_tbox()
    stats = s13._mark_classifying_dps_functional(g, str(DOMAIN_NS))
    assert stats["classifying_dp_functional_marked"] == 0
    assert "dp" in stats["classifying_dp_multivalued"]
    assert (NS["dp"], RDF.type, OWL.FunctionalProperty) not in g


def test_unreadable_abox_marks_nothing(monkeypatch, tmp_path):
    """A-Box 를 못 읽으면 마킹하지 않는다 — 판정 불가는 선언 근거가 아니다."""
    monkeypatch.setattr("config.ABOX_PATH", str(tmp_path / "absent.ttl"))
    g = _defined_class_tbox()
    stats = s13._mark_classifying_dps_functional(g, str(DOMAIN_NS))
    assert stats["classifying_dp_functional_marked"] == 0
    assert stats["classifying_dp_skip_reason"].startswith("abox-unreadable")


def test_literal_axiom_dp_is_not_collected(monkeypatch, tmp_path):
    """문자열 공리의 onProperty 는 수집하지 않는다 (파싱 불가라 분류에 쓰이지 않는다)."""
    abox = tmp_path / "a_box.ttl"
    abox.write_text(f"@prefix steel: <{DOMAIN_NS}> .\n", encoding="utf-8")
    monkeypatch.setattr("config.ABOX_PATH", str(abox))
    g = _defined_class_tbox(as_literal=True)
    stats = s13._mark_classifying_dps_functional(g, str(DOMAIN_NS))
    assert stats["classifying_dp_candidates"] == 0


def test_scanner_matches_rdflib_on_deployed_abox():
    """텍스트 스캐너가 rdflib 파싱과 **같은 결과**를 주는가.

    rdflib 를 안 쓰는 이유는 성능이지만, 정확성이 다르면 최적화가 아니라 버그다.
    """
    import pathlib
    ab_path = pathlib.Path("data/generated/abox/a_box.ttl")
    if not ab_path.exists():
        pytest.skip("배포 A-Box 없음")
    probes = ["equipmentStatusValue", "alarmEventsSeverity", "ghgEmissionScopeType"]
    uris = {URIRef(str(DOMAIN_NS) + p) for p in probes}
    single, multi, err = s13._scan_single_valued(uris)
    assert err is None
    abox = Graph()
    abox.parse(str(ab_path), format="turtle")
    for name in probes:
        counts: dict = {}
        for subj, _ in abox.subject_objects(URIRef(str(DOMAIN_NS) + name)):
            counts[subj] = counts.get(subj, 0) + 1
        rdflib_multi = any(n > 1 for n in counts.values())
        assert (name in multi) == rdflib_multi, (
            f"{name}: 스캐너({name in multi}) != rdflib({rdflib_multi})"
        )
        if counts and not rdflib_multi:
            assert name in single


def test_functional_on_dp_does_not_create_sameas():
    """``FunctionalProperty`` 를 DP 에 붙여도 sameAs 폭발이 없다 (IFP 와 다르다).

    이 파이프라인은 IFP 를 금지한다 — 그 근거가 DP 에도 적용된다고 오독하면 이 수정을
    되돌리게 된다. 최소 예제로 고정한다.
    """
    reasonable = pytest.importorskip("reasonable")
    g = Graph()
    g.add((NS["P"], RDF.type, OWL.Class))
    g.add((NS["dp"], RDF.type, OWL.DatatypeProperty))
    g.add((NS["dp"], RDF.type, OWL.FunctionalProperty))
    for i in range(3):
        inst = URIRef(str(DOMAIN_NS) + f"i{i}")
        g.add((inst, RDF.type, NS["P"]))
        g.add((inst, NS["dp"], Literal("V")))        # 같은 값
    engine = reasonable.PyReasoner()
    engine.from_graph(g)
    inferred = Graph()
    for triple in engine.reason():
        inferred.add(triple)
    assert not list(inferred.triples((None, OWL.sameAs, None))), (
        "DP FunctionalProperty 가 개체를 동일시했다 — IFP 와 같은 위험이 있다"
    )


def test_step13_wires_the_classifying_marker():
    """``apply`` 가 이 헬퍼를 호출하는가 — 함수만 있고 배선이 없으면 무효다."""
    import ast
    import pathlib

    src = pathlib.Path(
        "tools/quality_steps/step_13_pk_functional_someValuesFrom.py",
    ).read_text(encoding="utf-8")
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "apply"
    )
    assert "_mark_classifying_dps_functional" in ast.unparse(fn), (
        "step_13.apply 가 분류 DP 마킹을 호출하지 않는다"
    )
