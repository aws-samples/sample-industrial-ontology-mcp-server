"""Step 0b — 대칭화한 inverseOf 의 상대편도 ``owl:ObjectProperty`` 로 선언한다.

## 배경 (2026-08-22 실측 인과)

S2 초안이 한 방향만 내보낸 inverseOf 를 0b 가 대칭화하면서 상대편을 **선언하지
않았다**. 그 결과 T-Box 스스로 미선언 술어를 만들었다:

    steel:steamEnergyHasEquipment a owl:ObjectProperty ; owl:inverseOf steel:equipmentHasSteamEnergy .
    steel:equipmentHasSteamEnergy owl:inverseOf steel:steamEnergyHasEquipment .   # ← 선언 없음

``antipattern_asymmetric_fixed: 2`` 는 S9 ``undeclared_op`` 위반 2건
(equipmentHasSteamEnergy usage 720 / equipmentHasTag usage 50) 과 정확히 같은
수였다 — 우연이 아니라 같은 뿌리다. 추론기가 ``prp-inv`` 로 그 이름에 770
트리플을 파생시키므로, domain/range 제약이 없는 술어가 실데이터를 나른다.

step_02 는 :83 에서 같은 방어를 이미 갖고 있었다 (2026-08-17 ``precededBy``
사건). 0b 는 그 방어의 사본을 갖지 못했다.

이 스위트는 **양방향** 을 주장한다:
  - POSITIVE: 미선언 상대편을 선언한다 (결함 재발 감지).
  - NEGATIVE: 정당한 입력을 보존한다 — 외래 온톨로지 프로퍼티를 우리 T-Box 가
    선언하지 않고, DP/AnnotationProperty 를 OP 로 재분류하지 않고,
    domain/range 를 짝에서 뒤집어 채우지 않는다.
"""
from __future__ import annotations

from rdflib import OWL, RDF, RDFS, Graph, URIRef

from domain.namespaces import DOMAIN_NS, NS_PREFIX
from tools.quality_steps import step_00_antipatterns as step
from tools.quality_steps._base import StepContext

_HDR = (
    f"@prefix {NS_PREFIX}: <{DOMAIN_NS}> .\n"
    "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
    "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
    "@prefix iof-core: <https://spec.industrialontologies.org/ontology/core/Core/> .\n"
)


def D(name: str) -> URIRef:
    return URIRef(DOMAIN_NS + name)


def _g(body: str) -> Graph:
    g = Graph()
    g.parse(data=_HDR + body, format="turtle")
    return g


def _undeclared_inverse_legs(g: Graph) -> set[str]:
    """``owl:inverseOf`` 로 참조되지만 OP 선언이 없는 도메인 이름 (S9 게이트와 동일 판정)."""
    declared = {str(s) for s in g.subjects(RDF.type, OWL.ObjectProperty)}
    bad: set[str] = set()
    for subj, _, obj in g.triples((None, OWL.inverseOf, None)):
        for node in (subj, obj):
            if isinstance(node, URIRef) and str(node).startswith(DOMAIN_NS) \
                    and str(node) not in declared:
                bad.add(str(node).rsplit("#", 1)[-1])
    return bad


class TestDeclaresSymmetrizedPartner:
    """POSITIVE — 결함 재발을 감지한다."""

    def test_undeclared_partner_gets_declared(self):
        """실측 재현: 정방향만 선언된 쌍 → 역방향 leg + 선언 둘 다 생긴다."""
        g = _g(f"""
{NS_PREFIX}:steamEnergyHasEquipment a owl:ObjectProperty ;
    rdfs:domain {NS_PREFIX}:SteamEnergy ;
    rdfs:range {NS_PREFIX}:EquipmentMaster ;
    owl:inverseOf {NS_PREFIX}:equipmentHasSteamEnergy .
""")
        assert _undeclared_inverse_legs(g) == {"equipmentHasSteamEnergy"}

        fixed, declared = step._fix_asymmetric_inverse(g, DOMAIN_NS)

        assert fixed == 1, "역방향 leg 이 추가돼야 한다"
        assert declared == 1, "선언 수가 stats 로 보고돼야 한다 — 그래야 S9 전에 보인다"
        assert _undeclared_inverse_legs(g) == set(), (
            "0b 가 대칭화한 상대편은 선언돼야 한다 — 선언을 빼면 T-Box 스스로 "
            "미선언 술어를 만들고 validate_kg undeclared_op 가 FAIL 한다"
        )
        assert (D("equipmentHasSteamEnergy"), RDF.type, OWL.ObjectProperty) in g

    def test_apply_reports_and_leaves_no_undeclared_leg(self):
        """스텝 전체(apply)를 거쳐도 미선언 leg 이 남지 않는다."""
        g = _g(f"""
{NS_PREFIX}:tagMasterHasEquipment a owl:ObjectProperty ;
    rdfs:domain {NS_PREFIX}:TagMaster ;
    rdfs:range {NS_PREFIX}:EquipmentMaster ;
    owl:inverseOf {NS_PREFIX}:equipmentHasTag .
""")
        result = step.apply(g, StepContext(domain_ns=DOMAIN_NS))

        assert result.stats["antipattern_asymmetric_fixed"] == 1
        assert result.stats["antipattern_inverse_declared"] == 1, (
            "선언 수는 stats 로 노출돼야 한다 — 대칭화 수(2) 만 보면 정상처럼 "
            "보이고 미선언 결함이 S9 까지 안 보인다 (이번 사고의 관측 실패)"
        )
        assert _undeclared_inverse_legs(g) == set()


class TestPreservesLegitimateInput:
    """NEGATIVE — 정당한 입력을 보존한다 (과잉 선언 방지)."""

    def test_foreign_property_is_not_declared_by_us(self):
        """외래 온톨로지 프로퍼티는 우리가 선언하지 않는다 — 남의 공리 사칭 금지."""
        g = _g(f"""
{NS_PREFIX}:hasPart a owl:ObjectProperty ;
    owl:inverseOf iof-core:isPartOf .
""")
        step._fix_asymmetric_inverse(g, DOMAIN_NS)

        foreign = URIRef(
            "https://spec.industrialontologies.org/ontology/core/Core/isPartOf"
        )
        assert (foreign, RDF.type, OWL.ObjectProperty) not in g, (
            "외래 IRI 를 우리 T-Box 가 OP 로 선언하면 그 온톨로지의 공리를 사칭한다"
        )
        # 대칭 leg 자체는 여전히 추가된다 (0b 의 본래 역할).
        assert (foreign, OWL.inverseOf, D("hasPart")) in g

    def test_already_declared_partner_is_untouched(self):
        """양쪽이 이미 선언·대칭이면 아무것도 바꾸지 않는다 (멱등)."""
        g = _g(f"""
{NS_PREFIX}:a a owl:ObjectProperty ; owl:inverseOf {NS_PREFIX}:b .
{NS_PREFIX}:b a owl:ObjectProperty ; owl:inverseOf {NS_PREFIX}:a .
""")
        before = set(g)
        fixed, declared = step._fix_asymmetric_inverse(g, DOMAIN_NS)

        assert fixed == 0
        assert declared == 0
        assert set(g) == before, "멱등해야 한다"

    def test_datatype_property_is_not_reclassified_as_op(self):
        """이미 다른 타입으로 선언된 이름은 OP 로 재분류하지 않는다.

        OP+DP 동시 선언은 OWL 2 DL 위반이라 HermiT 이 온톨로지를 거부한다 —
        그러면 게이트가 통째로 사라진다 (validator 실패 = 검증 없음).
        """
        g = _g(f"""
{NS_PREFIX}:someOp a owl:ObjectProperty ; owl:inverseOf {NS_PREFIX}:measuredValue .
{NS_PREFIX}:measuredValue a owl:DatatypeProperty .
""")
        step._fix_asymmetric_inverse(g, DOMAIN_NS)

        assert (D("measuredValue"), RDF.type, OWL.ObjectProperty) not in g, (
            "DP 를 OP 로도 선언하면 OWL 2 DL 위반 — HermiT 이 온톨로지를 못 열고 "
            "S4/S9.1 게이트가 사라진다"
        )

    def test_domain_range_not_copied_from_partner(self):
        """domain/range 는 짝에서 뒤집어 채우지 않는다 (step_02 :79 와 동일 정책).

        방향 오류를 복제하는 경로다 (step_09b 전례). 근거를 갖고 채우는 것은
        step_09/09a 의 책임이므로 0b 는 선언만 한다.
        """
        g = _g(f"""
{NS_PREFIX}:fwd a owl:ObjectProperty ;
    rdfs:domain {NS_PREFIX}:A ;
    rdfs:range {NS_PREFIX}:B ;
    owl:inverseOf {NS_PREFIX}:inv .
""")
        step._fix_asymmetric_inverse(g, DOMAIN_NS)

        assert list(g.objects(D("inv"), RDFS.domain)) == []
        assert list(g.objects(D("inv"), RDFS.range)) == []
