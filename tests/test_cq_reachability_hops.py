"""CQ 도달성 검사기의 홉 상한 회귀 가드.

배경 (2026-08-18 실측): S2 5라운드 내내 CQ 커버리지가 58.3% 로 **한 번도 움직이지
않았다**. 미답변 5건은 전부 "N → M 경로 없음 (2-hop OP 부재)" 였다.

그런데 선언된 ``rdfs:domain``/``rdfs:range`` 만으로 BFS 하면 **5쌍 전부 경로가
실재한다** — 다만 3~5홉이다:

    ProcessBlastFurnace → EquipmentMaster → TagMaster → AlarmEvents          (3홉)
    GHGEmission → EquipmentMaster → FuelConsumption → EnergySourceMaster     (3홉)
    ProductionResult → ProductMaster → ProductionPlan → InventoryTransaction (3홉)
    SteamEnergy → EquipmentMaster → WaterQualityMonitoring → MonitoringPoint (3홉)
    RealTimeData → TagMaster → EquipmentMaster → Process… → Product… → Chem  (5홉)

즉 ``max_hops=2`` 가 정당한 경로를 못 본 것이고, Architect 는 5라운드 동안
**달성 불가능한 목표 수치**를 보고 있었다. 어떤 OP 를 추가해도 2홉 밖이면
커버리지가 오르지 않으므로 토론이 수렴할 수 없다.

## 왜 max_hops=3 인가 (실측)

| max_hops | CQ 갭 해소 | 전체 클래스쌍 연결률 |
|---------:|-----------:|--------------------:|
| 2        | 0/5        | 17.5%               |
| **3**    | **5/5**    | **25.3%**           |
| 4        | 5/5        | 32.9%               |
| 6        | 5/5        | 41.7%               |

3 에서 이미 전부 해소되고, 더 올려도 이득 없이 연결률만 오른다(지표가 포화되면
"연결됨" 판정이 무의미해진다). 그래서 3 으로 고정하고 환경변수로 조절만 남긴다.

## 조상(ancestor) 확장은 기각했다

처음엔 domain/range 를 조상 방향으로도 확장하려 했다(직전 감사 제안). 실측 결과
그건 틀린 해법이다:

1. **명명 Restriction 오염**: 조상 집합에 ``EquipmentMaster_hasX_someValuesFrom``
   류가 섞여 edges 가 145 → 6,137 로 42배 폭증한다. 제외하면 617.
2. **허브 세탁**: ``ProductionResult`` 와 ``InventoryTransaction`` 은 둘 다
   ``TransactionRecord`` 자손이라, 공통 추상 조상을 경유해 "연결됨" 이 된다.
   실제로 통과한 경로가 ``ProductionResult → ProductionManagement →
   InventoryTransaction`` 이었는데 두 번째 홉은 ``ProductionPlan`` 의 OP 다 —
   형제 클래스의 관계를 자기 것처럼 쓴 것이다.

홉 상한 교정은 **실재하는 OP 경로만** 인정하므로 이 위험이 없다.
"""
from __future__ import annotations

import pytest

from tools.multi_agent_tbox import _cq_runtime_check

# 3홉 체인: A → B → C → D. 2홉 검사기는 A↔D 를 놓친다.
CHAIN_TTL = """
@prefix steel: <http://example.com/steel-ontology#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

steel:AlphaClass a owl:Class .
steel:BetaClass a owl:Class .
steel:GammaClass a owl:Class .
steel:DeltaClass a owl:Class .
steel:IsolatedClass a owl:Class .

steel:alphaToBeta a owl:ObjectProperty ;
    rdfs:domain steel:AlphaClass ; rdfs:range steel:BetaClass .
steel:betaToGamma a owl:ObjectProperty ;
    rdfs:domain steel:BetaClass ; rdfs:range steel:GammaClass .
steel:gammaToDelta a owl:ObjectProperty ;
    rdfs:domain steel:GammaClass ; rdfs:range steel:DeltaClass .
"""


def _cq(*domains: str) -> list[dict]:
    return [{"id": "CQ01", "domains": list(domains)}]


def test_three_hop_path_is_answerable():
    """THE REGRESSION: 3홉 경로가 실재하면 답변 가능으로 판정한다.

    실측 5건 전부 3~5홉이었고 ``max_hops=2`` 가 이를 "경로 없음" 으로 보고했다.
    """
    result = _cq_runtime_check(CHAIN_TTL, _cq("AlphaClass", "DeltaClass"))

    assert result["answerable"] == ["CQ01"], (
        f"3홉 경로를 놓쳤다: {result['unanswerable']}"
    )
    assert result["coverage_pct"] == 100.0


def test_two_hop_path_still_answerable():
    """POSITIVE 보존: 기존에 통과하던 2홉은 계속 통과한다."""
    result = _cq_runtime_check(CHAIN_TTL, _cq("AlphaClass", "GammaClass"))
    assert result["answerable"] == ["CQ01"]


def test_genuinely_disconnected_class_stays_unanswerable():
    """NEGATIVE: 어떤 OP 도 닿지 않는 클래스는 여전히 미답변이다.

    홉 상한을 올리는 변경은 "전부 통과" 로 흘러가기 쉽다 — 그러면 지표가
    무의미해지고 게이트가 꺼진 것과 같다. 이 방향을 주장하지 않으면 그 퇴화를
    감지할 수 없다.
    """
    result = _cq_runtime_check(CHAIN_TTL, _cq("AlphaClass", "IsolatedClass"))

    assert result["answerable"] == []
    assert len(result["unanswerable"]) == 1
    cq_id, reason = result["unanswerable"][0]
    assert cq_id == "CQ01"
    assert "경로 없음" in reason


def test_reason_message_reports_the_actual_hop_limit():
    """실패 사유가 실제 상한을 말해야 한다.

    "2-hop OP 부재" 라고 하드코딩돼 있으면 상한을 바꿔도 메시지가 거짓말을 하고,
    Architect 는 잘못된 목표를 계속 본다.
    """
    result = _cq_runtime_check(CHAIN_TTL, _cq("AlphaClass", "IsolatedClass"))
    _cq_id, reason = result["unanswerable"][0]
    assert "2-hop" not in reason, f"상한이 메시지에 하드코딩돼 있다: {reason}"


def test_hop_limit_is_configurable(monkeypatch):
    """환경변수로 조절 가능 — 포화가 관측되면 즉시 되돌릴 수 있어야 한다."""
    monkeypatch.setenv("S2_CQ_MAX_HOPS", "2")
    result = _cq_runtime_check(CHAIN_TTL, _cq("AlphaClass", "DeltaClass"))
    assert result["answerable"] == [], "상한 2로 내렸는데 3홉이 통과했다"

    monkeypatch.setenv("S2_CQ_MAX_HOPS", "3")
    result = _cq_runtime_check(CHAIN_TTL, _cq("AlphaClass", "DeltaClass"))
    assert result["answerable"] == ["CQ01"]


def test_default_hop_limit_is_three():
    """기본값 고정 — 실측에서 3이 CQ 갭 5/5 를 해소하고 포화하지 않는 값이었다."""
    import tools.multi_agent_tbox as mt

    assert mt._CQ_MAX_HOPS_DEFAULT == 3


def test_named_restrictions_do_not_act_as_hierarchy_nodes():
    """명명된 ``owl:Restriction`` 은 계층 확장에 참여하지 않는다.

    이 리포의 T-Box 는 클래스별 someValuesFrom 을 **명명 Restriction** 으로
    선언한다(현행 111개). 그것을 클래스처럼 취급하면 계층 집합이 오염된다.

    발화 조건은 **Restriction 이 OP 의 domain/range 로 선언된 경우** 다. 그러면
    자손 확장이 그 Restriction 을 부모로 갖는 **무관한 형제들** 을 전부 끌어와,
    서로 아무 관계 없는 클래스가 연결된 것처럼 보인다. 아래 픽스처에서
    ``AlphaClass`` 와 ``IsolatedClass`` 는 같은 Restriction 을 부모로 갖는 것
    말고는 공통점이 없는데, 그 Restriction 에 OP 가 걸려 있다.

    (참고: S3 는 Restriction 을 OP domain 으로 쓰지 않지만 LLM 초안은 만들 수
    있다. 조상 방향 확장을 도입하면 이 위험이 훨씬 커진다 — 그래서 조상 확장을
    기각하고 홉 상한만 올렸다. 모듈 docstring 의 "조상 확장은 기각했다" 참조.)
    """
    ttl = """
@prefix steel: <http://example.com/steel-ontology#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

steel:Shared_someValuesFrom a owl:Class, owl:Restriction ;
    owl:onProperty steel:someProp ; owl:someValuesFrom steel:BetaClass .
steel:AlphaClass a owl:Class ; rdfs:subClassOf steel:Shared_someValuesFrom .
steel:IsolatedClass a owl:Class ; rdfs:subClassOf steel:Shared_someValuesFrom .
steel:DeltaClass a owl:Class .

steel:restrToDelta a owl:ObjectProperty ;
    rdfs:domain steel:Shared_someValuesFrom ; rdfs:range steel:DeltaClass .
"""
    result = _cq_runtime_check(ttl, _cq("IsolatedClass", "DeltaClass"))
    assert result["answerable"] == [], (
        f"명명 Restriction 을 경유해 허위 연결이 생겼다: {result}"
    )


def test_restriction_is_not_traversed_downward_either():
    """Restriction 이 실제 클래스의 **자식** 일 때도 타고 내려가지 않는다.

    제외는 두 방향 모두 필요하다: Restriction 이 부모로 등장하는 경우(위 테스트)와
    자식으로 등장하는 경우. 후자는 `Class ⊑ Restriction ⊑ Class` 사슬에서 자손
    확장이 Restriction 을 경유해 그 아래 손자까지 끌어오는 형태다. 한쪽만 막으면
    다른 쪽으로 오염이 새어든다(실측: 각 분기를 개별 무력화하면 서로 다른
    테스트가 red).
    """
    ttl = """
@prefix steel: <http://example.com/steel-ontology#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

steel:Parent a owl:Class .
steel:R_someValuesFrom a owl:Class, owl:Restriction ;
    owl:onProperty steel:someProp ; owl:someValuesFrom steel:Parent ;
    rdfs:subClassOf steel:Parent .
steel:Grandchild a owl:Class ; rdfs:subClassOf steel:R_someValuesFrom .
steel:DeltaClass a owl:Class .

steel:parentToDelta a owl:ObjectProperty ;
    rdfs:domain steel:Parent ; rdfs:range steel:DeltaClass .
"""
    result = _cq_runtime_check(ttl, _cq("Grandchild", "DeltaClass"))
    assert result["answerable"] == [], (
        f"Restriction 을 타고 내려가 허위 연결이 생겼다: {result}"
    )


#: 2026-09-05: ``("ProductionResult", "InventoryTransaction")`` 을 제거했다.
#:
#: 그 쌍의 3홉 경로는 09-03 세대의
#: ``inventoryTransactionFulfillsProductionPlan`` / ``productionPlanHasInventoryTransaction``
#: (둘 다 ``dcterms:source = Reference_No``) 를 타고 있었는데, 그 OP 는 **근거가
#: 없다**. 실측: ``Inventory_Transaction.Reference_No`` 값은 ``REF00001…`` 이고
#: ``Production_Plan.Plan_ID`` (``PP00001…``) 와 교집합이 **0**, 나아가 **40개 CSV 의
#: 모든 컬럼과 교집합이 0** 이다 (자기 자신만 100%). LLM 이 컬럼명만 보고 발명한
#: 관계였다.
#:
#: 즉 CQ01 의 그 도달성은 0행 OP 를 타고 난 **위양성**이었고, 이번 S2 세대가 그 OP 를
#: 만들지 않은 것은 개선이다. 쌍을 남겨 두면 이 테스트가 "근거 없는 OP 를 되살려라" 를
#: 요구하게 된다 — 이 리포가 반복 기각한 지표 매수다.
#:
#: 남은 3쌍으로도 ``max_hops=3`` 회귀 가드는 성립한다 (2홉 검사기는 셋 다 놓친다).
@pytest.mark.parametrize("pair", [
    ("ProcessBlastFurnace", "AlarmEvents"),
    ("GHGEmission", "EnergySourceMaster"),
    ("SteamEnergy", "MonitoringPointMaster"),
])
def test_real_tbox_pairs_reported_as_missing_are_actually_reachable(pair):
    """실측 회귀: 배포 T-Box 에서 미답변으로 보고됐던 쌍이 실제로 도달 가능하다.

    T-Box 파일이 없거나 그 쌍이 사라졌으면 skip — 이 테스트는 산출물에
    의존하므로 환경에 따라 성립하지 않을 수 있다.
    """
    import os

    from config import TBOX_PATH

    if not os.path.exists(TBOX_PATH):
        pytest.skip("배포 T-Box 없음")
    with open(TBOX_PATH, encoding="utf-8") as fh:
        ttl = fh.read()
    result = _cq_runtime_check(ttl, _cq(*pair))
    if result["unanswerable"] and "클래스 누락" in result["unanswerable"][0][1]:
        pytest.skip(f"현행 T-Box 에 클래스 없음: {result['unanswerable'][0][1]}")
    assert result["answerable"] == ["CQ01"], (
        f"{pair[0]} ↔ {pair[1]} 은 3홉 경로가 실재하는데 미답변으로 판정됐다: "
        f"{result['unanswerable']}"
    )
