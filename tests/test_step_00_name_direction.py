"""Step 0f — 이름이 뜻하는 방향과 domain/range 가 반대인 OP 교정.

## 배경 (2026-08-15 실측 인과)

``isProductOfRolling`` 은 이름이 "이 제품은 압연 공정의 산출물" 을 뜻하므로
``ProductMaster → ProcessRolling`` 이어야 한다. S2 가 그 반대로 선언했고, 그 오류가
**전파** 됐다:

  1. S2: ``isProductOfRolling`` = ProcessRolling → ProductMaster (뒤집힘)
  2. Jury: 짝 ``rollingHasProduct`` 의 domain/range 를 **삭제** (커밋 383f8bd 가 막는 경로)
  3. ``step_09b``: "range 누락 + inverseOf 있음 → inverse.domain 을 range 로" 규칙으로
     뒤집힌 값을 짝에 복제
  4. 결과: ``rollingHasProduct = ProductMaster → ProcessRolling`` 이고 이 OP 는
     ``rdfs:subPropertyOf iof-core:hasOutput`` 이다 ⟹ **"제품이 공정을 산출한다"**
     를 함의. owlrl 실측: ``P034 hasOutput PR_1`` = True, 올바른 방향 = False.

게이트 사각지대: domain/range 가 **존재하므로** conformance 통과, 두 클래스가
disjoint 도 아니라 HermiT consistent, ``check_quality_rules`` 는 이름과 방향의 관계를
보지 않는다 (107 이슈 중 critical 0 / high 0).

자기영속성도 있었다 — ``_build_domain_fixes`` 가 **배포 T-Box** 에서 domain 시드를
읽으므로, 뒤집힌 배포본이 S3 재실행마다 그 방향을 재주입한다.
"""
from __future__ import annotations

from rdflib import RDF, RDFS, Graph, URIRef

from domain.namespaces import DOMAIN_NS, NS_PREFIX
from tools.quality_steps import step_00_antipatterns as step
from tools.quality_steps._base import StepContext

_HDR = (
    f"@prefix {NS_PREFIX}: <{DOMAIN_NS}> .\n"
    "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
    "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
)


def D(name: str) -> URIRef:
    return URIRef(DOMAIN_NS + name)


def _g(body: str) -> Graph:
    g = Graph()
    g.parse(data=_HDR + body, format="turtle")
    return g


def _dr(g: Graph, name: str) -> tuple:
    p = D(name)
    d = [str(x).rsplit("#", 1)[-1] for x in g.objects(p, RDFS.domain)]
    r = [str(x).rsplit("#", 1)[-1] for x in g.objects(p, RDFS.range)]
    return (d[0] if d else None, r[0] if r else None)


def _apply(g: Graph):
    return step.apply(g, StepContext(domain_ns=DOMAIN_NS))


class TestNameDirectionContradiction:

    def test_flips_is_x_of_target_when_domain_is_the_target(self):
        """``is…Of<Target>`` 의 domain 이 그 ``<Target>`` 이면 뒤집는다."""
        g = _g(
            f"{NS_PREFIX}:ProcessRolling a owl:Class .\n"
            f"{NS_PREFIX}:ProductMaster a owl:Class .\n"
            f"{NS_PREFIX}:isProductOfRolling a owl:ObjectProperty ; "
            f"rdfs:domain {NS_PREFIX}:ProcessRolling ; "
            f"rdfs:range {NS_PREFIX}:ProductMaster .\n"
        )
        res = _apply(g)
        assert _dr(g, "isProductOfRolling") == ("ProductMaster", "ProcessRolling"), (
            f"뒤집지 않았다: {_dr(g, 'isProductOfRolling')}"
        )
        assert res.stats["antipattern_name_direction_fixed"] == 1

    def test_matches_target_by_substring_when_suffix_fails(self):
        """``OfSteelmaking`` → ``ProcessSteelmakingFurnace`` (접미 불일치, 부분 일치).

        접미만 보면 이 케이스를 놓쳐 실측 3건 중 1건이 남았다.
        """
        g = _g(
            f"{NS_PREFIX}:ProcessSteelmakingFurnace a owl:Class .\n"
            f"{NS_PREFIX}:ProductMaster a owl:Class .\n"
            f"{NS_PREFIX}:isProductOfSteelmaking a owl:ObjectProperty ; "
            f"rdfs:domain {NS_PREFIX}:ProcessSteelmakingFurnace ; "
            f"rdfs:range {NS_PREFIX}:ProductMaster .\n"
        )
        _apply(g)
        assert _dr(g, "isProductOfSteelmaking") == (
            "ProductMaster", "ProcessSteelmakingFurnace",
        )

    def test_leaves_correct_direction_untouched(self):
        """이미 올바른 방향은 건드리지 않는다 (NEGATIVE 방향)."""
        g = _g(
            f"{NS_PREFIX}:ProcessRolling a owl:Class .\n"
            f"{NS_PREFIX}:ProductMaster a owl:Class .\n"
            f"{NS_PREFIX}:isProductOfRolling a owl:ObjectProperty ; "
            f"rdfs:domain {NS_PREFIX}:ProductMaster ; "
            f"rdfs:range {NS_PREFIX}:ProcessRolling .\n"
        )
        res = _apply(g)
        assert _dr(g, "isProductOfRolling") == ("ProductMaster", "ProcessRolling")
        assert res.stats["antipattern_name_direction_fixed"] == 0

    def test_skips_when_target_is_ambiguous(self):
        """``<Target>`` 후보 클래스가 여러 개면 판정을 보류한다.

        실측: ``OfProduct`` 는 ProductMaster / ProductInspection /
        ProductionPlan / ProductionResult / ProductionManagement 5개에 걸린다.
        틀린 교정은 없는 교정보다 나쁘다.
        """
        g = _g(
            f"{NS_PREFIX}:ProductMaster a owl:Class .\n"
            f"{NS_PREFIX}:ProductInspection a owl:Class .\n"
            f"{NS_PREFIX}:SurfaceQuality a owl:Class .\n"
            f"{NS_PREFIX}:isQualityOfProduct a owl:ObjectProperty ; "
            f"rdfs:domain {NS_PREFIX}:ProductMaster ; "
            f"rdfs:range {NS_PREFIX}:SurfaceQuality .\n"
        )
        res = _apply(g)
        assert res.stats["antipattern_name_direction_fixed"] == 0, (
            "모호한 <Target> 인데 교정했다 — 오답 위험"
        )
        assert _dr(g, "isQualityOfProduct") == ("ProductMaster", "SurfaceQuality")

    def test_skips_bare_is_x_of_without_target(self):
        """``Of`` 뒤에 토큰이 없으면 (``isProductOf``) 판정 근거가 없다."""
        g = _g(
            f"{NS_PREFIX}:ProcessRolling a owl:Class .\n"
            f"{NS_PREFIX}:ProductMaster a owl:Class .\n"
            f"{NS_PREFIX}:isProductOf a owl:ObjectProperty ; "
            f"rdfs:domain {NS_PREFIX}:ProcessRolling ; "
            f"rdfs:range {NS_PREFIX}:ProductMaster .\n"
        )
        res = _apply(g)
        assert res.stats["antipattern_name_direction_fixed"] == 0

    def test_skips_when_domain_or_range_is_multi_valued(self):
        """domain/range 가 여러 개면 어느 것을 뒤집을지 모른다 — 보류."""
        g = _g(
            f"{NS_PREFIX}:ProcessRolling a owl:Class .\n"
            f"{NS_PREFIX}:ProductMaster a owl:Class .\n"
            f"{NS_PREFIX}:ItemMaster a owl:Class .\n"
            f"{NS_PREFIX}:isProductOfRolling a owl:ObjectProperty ; "
            f"rdfs:domain {NS_PREFIX}:ProcessRolling, {NS_PREFIX}:ItemMaster ; "
            f"rdfs:range {NS_PREFIX}:ProductMaster .\n"
        )
        res = _apply(g)
        assert res.stats["antipattern_name_direction_fixed"] == 0

    def test_does_not_touch_has_style_names(self):
        """``has…`` / ``…Has…`` 는 이 판정의 대상이 아니다 (NEGATIVE 방향).

        정방향 이름은 ``Of<Target>`` 힌트가 없어 근거가 다르다. 여기서 건드리면
        정상 OP 를 망친다.
        """
        g = _g(
            f"{NS_PREFIX}:ProcessRolling a owl:Class .\n"
            f"{NS_PREFIX}:ProductMaster a owl:Class .\n"
            f"{NS_PREFIX}:rollingHasProduct a owl:ObjectProperty ; "
            f"rdfs:domain {NS_PREFIX}:ProcessRolling ; "
            f"rdfs:range {NS_PREFIX}:ProductMaster .\n"
        )
        res = _apply(g)
        assert _dr(g, "rollingHasProduct") == ("ProcessRolling", "ProductMaster")
        assert res.stats["antipattern_name_direction_fixed"] == 0


class TestNoFalseEntailmentAfterFix:
    """**산출물 기반**: 교정 후 iof-core:hasOutput 이 거짓을 함의하지 않는다."""

    def test_hasoutput_direction_is_correct_after_fix(self):
        import pytest
        owlrl = pytest.importorskip("owlrl")

        IOF = "https://spec.industrialontologies.org/ontology/core/Core/"
        g = _g(
            f"@prefix iof: <{IOF}> .\n"
            f"{NS_PREFIX}:ProcessRolling a owl:Class .\n"
            f"{NS_PREFIX}:ProductMaster a owl:Class .\n"
            f"{NS_PREFIX}:isProductOfRolling a owl:ObjectProperty ; "
            f"rdfs:domain {NS_PREFIX}:ProcessRolling ; "
            f"rdfs:range {NS_PREFIX}:ProductMaster ; "
            f"owl:inverseOf {NS_PREFIX}:rollingHasProduct .\n"
            f"{NS_PREFIX}:rollingHasProduct a owl:ObjectProperty ; "
            f"rdfs:subPropertyOf iof:hasOutput ; "
            f"owl:inverseOf {NS_PREFIX}:isProductOfRolling .\n"
        )
        _apply(g)
        # 교정 후: isProductOfRolling = Product→Process 이므로 짝은 Process→Product.
        # A-Box 사실은 공정 → 제품 방향으로 넣는다.
        proc = URIRef(DOMAIN_NS + "PR_1")
        prod = URIRef(DOMAIN_NS + "P034")
        g.add((proc, RDF.type, D("ProcessRolling")))
        g.add((prod, RDF.type, D("ProductMaster")))
        g.add((proc, D("rollingHasProduct"), prod))
        owlrl.DeductiveClosure(owlrl.OWLRL_Semantics).expand(g)

        has_output = URIRef(IOF + "hasOutput")
        assert (prod, has_output, proc) not in g, (
            "'제품이 공정을 산출' 을 함의한다 — 방향 교정이 반영되지 않았다"
        )
        assert (proc, has_output, prod) in g, "올바른 방향이 도출되지 않았다"


class TestReversedSubProperty:
    """0g — ``subPropertyOf`` 가 방향이 반대인 부모를 가리키면 제거.

    ``P ⊑ Q`` 는 "P 로 이어진 쌍이 Q 로도 이어진다" 를 뜻한다. P 가 ``A→B``, Q 가
    ``B→A`` 면 정방향 사실에서 **역방향 트리플이 파생된다**.

    실측 (2026-08-16): ``hasContinuousCastingProduct`` (Process→Product) 가
    ``subPropertyOf isProductOfContinuousCasting`` (Product→Process) 였고, tacit
    4,320 트리플에서 **제품이 공정을 산출** 하는 4,320 트리플이 파생됐다. 부모가
    ``iof-core:hasOutput`` 계열이라 IOF 표준 위반까지 갔다.

    발생 경로: ``step_15c`` 가 같은 (domain,range) 서명의 OP 를 부모로 붙였는데 그
    시점의 부모가 뒤집혀 있었고, 0f 가 부모를 교정하자 자식의 링크가 모순으로 남았다.
    """

    def test_removes_subproperty_with_opposite_direction(self):
        g = _g(
            f"{NS_PREFIX}:ProcA a owl:Class .\n"
            f"{NS_PREFIX}:ProdB a owl:Class .\n"
            f"{NS_PREFIX}:childForward a owl:ObjectProperty ; "
            f"rdfs:domain {NS_PREFIX}:ProcA ; rdfs:range {NS_PREFIX}:ProdB ; "
            f"rdfs:subPropertyOf {NS_PREFIX}:parentReverse .\n"
            f"{NS_PREFIX}:parentReverse a owl:ObjectProperty ; "
            f"rdfs:domain {NS_PREFIX}:ProdB ; rdfs:range {NS_PREFIX}:ProcA .\n"
        )
        res = _apply(g)
        assert (D("childForward"), RDFS.subPropertyOf, D("parentReverse")) not in g, (
            "방향이 반대인 subPropertyOf 를 남겼다 — 역방향 트리플이 파생된다"
        )
        assert res.stats["antipattern_reversed_subproperty_removed"] == 1

    def test_keeps_subproperty_with_same_direction(self):
        """같은 방향이면 유지한다 (NEGATIVE 방향).

        과잉 제거하면 IOF 정렬(``⊑ iof-core:hasOutput``)이 끊긴다.
        """
        g = _g(
            f"{NS_PREFIX}:ProcA a owl:Class .\n"
            f"{NS_PREFIX}:ProdB a owl:Class .\n"
            f"{NS_PREFIX}:childForward a owl:ObjectProperty ; "
            f"rdfs:domain {NS_PREFIX}:ProcA ; rdfs:range {NS_PREFIX}:ProdB ; "
            f"rdfs:subPropertyOf {NS_PREFIX}:parentForward .\n"
            f"{NS_PREFIX}:parentForward a owl:ObjectProperty ; "
            f"rdfs:domain {NS_PREFIX}:ProcA ; rdfs:range {NS_PREFIX}:ProdB .\n"
        )
        res = _apply(g)
        assert (D("childForward"), RDFS.subPropertyOf, D("parentForward")) in g, (
            "같은 방향 subPropertyOf 를 지웠다 — IOF 정렬이 끊긴다"
        )
        assert res.stats["antipattern_reversed_subproperty_removed"] == 0

    def test_skips_self_referential_property(self):
        """``domain == range`` 는 반대 판정이 불가능하므로 건드리지 않는다."""
        g = _g(
            f"{NS_PREFIX}:Step a owl:Class .\n"
            f"{NS_PREFIX}:childSelf a owl:ObjectProperty ; "
            f"rdfs:domain {NS_PREFIX}:Step ; rdfs:range {NS_PREFIX}:Step ; "
            f"rdfs:subPropertyOf {NS_PREFIX}:parentSelf .\n"
            f"{NS_PREFIX}:parentSelf a owl:ObjectProperty ; "
            f"rdfs:domain {NS_PREFIX}:Step ; rdfs:range {NS_PREFIX}:Step .\n"
        )
        res = _apply(g)
        assert (D("childSelf"), RDFS.subPropertyOf, D("parentSelf")) in g
        assert res.stats["antipattern_reversed_subproperty_removed"] == 0

    def test_no_false_derivation_after_removal(self):
        """**산출물 기반**: 제거 후 정방향 사실이 역방향을 파생하지 않는다."""
        import pytest
        owlrl = pytest.importorskip("owlrl")

        g = _g(
            f"{NS_PREFIX}:ProcA a owl:Class .\n"
            f"{NS_PREFIX}:ProdB a owl:Class .\n"
            f"{NS_PREFIX}:childForward a owl:ObjectProperty ; "
            f"rdfs:domain {NS_PREFIX}:ProcA ; rdfs:range {NS_PREFIX}:ProdB ; "
            f"rdfs:subPropertyOf {NS_PREFIX}:parentReverse .\n"
            f"{NS_PREFIX}:parentReverse a owl:ObjectProperty ; "
            f"rdfs:domain {NS_PREFIX}:ProdB ; rdfs:range {NS_PREFIX}:ProcA .\n"
        )
        _apply(g)
        proc = URIRef(DOMAIN_NS + "proc1")
        prod = URIRef(DOMAIN_NS + "prod1")
        g.add((proc, RDF.type, D("ProcA")))
        g.add((prod, RDF.type, D("ProdB")))
        g.add((proc, D("childForward"), prod))
        owlrl.DeductiveClosure(owlrl.OWLRL_Semantics).expand(g)

        assert (proc, D("parentReverse"), prod) not in g, (
            "정방향 사실에서 방향이 반대인 부모 술어가 파생됐다"
        )
