"""T-Box 의 필수참여 공리 (C ⊑ ∃p.D) 가 A-Box 에서 충족되는지 검사한다.

2026-08-28 실측. 배포 산출물에서 공리 109개 중 **44 (class, OP) 쌍**이 **69,858
개체**에서 위반인데 25 check 중 아무것도(당시 24개) 보지 않았다::

    RealTimeData              realTimeDataMonitorsSteelmaking     36,000 / 36,000
    ManufacturingProcessStep  processStepHasProductionResult      17,284 / 17,284
    EquipmentStatus           equipmentStatusRelatedFailureCause   6,000 /  6,000

미보유 = 전체다. 즉 그 OP 는 A-Box 에 **하나도** 없다.

## 왜 기존 게이트가 전부 조용한가

* **HermiT** — OWL 개방세계(OWA)라 "관계가 안 보인다" ≠ "관계가 없다". 원리적으로
  모순이 아니므로 추론기는 **영구히** 침묵한다. 이 검사는 CWA 질문이다.
* **SHACL** — 동적 shape 이 DP/OP 의 domain·range·label 만 만들고 ``sh:minCount``
  를 만들지 않아 conforms=true.
* **domain_range_conformance** — 존재하는 트리플의 타입만 본다. "있어야 하는데
  없는" 것은 범위 밖.
* **property_coverage** — OP 가 전역 0건인지만 본다. tacit 이 일부를 채우면 전역
  카운트가 0 이 아니므로 클래스별 위반이 가려진다.

## 조용한 것이 왜 위험한가

추론이 그 개체들을 **자기가 위반하는 클래스로 타이핑한다**. ``RealTimeData``
36,000개가 해당 OP 를 0건 갖는데 ``RealTimeData_..._someValuesFrom`` 타입은 36,002건
붙어 있다. 그 클래스로 필터하는 질의는 "요구 관계를 가진 개체" 를 기대하는데 실제로는
0개 가진 개체 전부를 받는다 — 조용히 틀린 결과다.

S8 이 보고한 ``restriction_violation_count: 69,858`` 과 이 게이트의 수치가 정확히
일치한다. 즉 **추론기는 이미 알고 있었고 검증 게이트에만 없었다.**

## 이 테스트의 방향

"위반을 센다" 만 주장하면 모든 클래스를 위반으로 세도 통과한다. 네 축을 고정한다:

* 검출 — 관계를 하나도 갖지 않는 개체를 잡는다
* 보존 — 충족한 개체를 위반으로 세지 **않는다** (한 개라도 있으면 충족)
* 두 축 — ``subClassOf`` 와 ``equivalentClass`` 양쪽 Restriction 을 본다
* 비공허 — 공리 0건이면 그렇다고 말한다 (조용한 통과 금지)
"""
from __future__ import annotations

import pathlib

import pytest
from rdflib import OWL, RDF, RDFS, BNode, Graph, Namespace

from domain.namespaces import DOMAIN_NS, bind_namespaces
from tools.validation_support.checks.semantic import check_existential_participation

NS = Namespace(str(DOMAIN_NS))
INST = Namespace(str(DOMAIN_NS) + "instances#")


def _tbox_with_requirement(
    cls: str, prop: str, target: str, *, axis=RDFS.subClassOf,
) -> Graph:
    """C ⊑ ∃prop.Target 을 담은 최소 T-Box."""
    g = Graph()
    bind_namespaces(g)
    g.add((NS[cls], RDF.type, OWL.Class))
    g.add((NS[target], RDF.type, OWL.Class))
    g.add((NS[prop], RDF.type, OWL.ObjectProperty))
    node = BNode()
    g.add((node, RDF.type, OWL.Restriction))
    g.add((node, OWL.onProperty, NS[prop]))
    g.add((node, OWL.someValuesFrom, NS[target]))
    g.add((NS[cls], axis, node))
    return g


def _abox(cls: str, count: int, *, prop: str | None = None, filled: int = 0) -> Graph:
    g = Graph()
    bind_namespaces(g)
    for i in range(count):
        subj = INST[f"{cls}_{i}"]
        g.add((subj, RDF.type, NS[cls]))
        if prop and i < filled:
            g.add((subj, NS[prop], INST[f"target_{i}"]))
    return g


# ── 검출 ────────────────────────────────────────────────────────────────


def test_instances_missing_the_required_relation_are_caught():
    """THE REGRESSION: 필수 관계를 하나도 갖지 않는 개체를 잡는다."""
    tbox = _tbox_with_requirement("RealTimeData", "monitors", "Process")
    result = check_existential_participation(_abox("RealTimeData", 5), tbox)

    assert result["passed"] is False
    assert result["violated_pairs"] == 1
    assert result["violating_instances"] == 5
    assert result["violations"][0]["missing"] == 5
    assert result["violations"][0]["total"] == 5


def test_partial_fill_reports_only_the_missing():
    """일부만 채워지면 나머지만 위반이다 — 전부/전무로 뭉개지 않는다."""
    tbox = _tbox_with_requirement("C", "p", "D")
    result = check_existential_participation(
        _abox("C", 10, prop="p", filled=4), tbox,
    )

    assert result["violating_instances"] == 6
    assert result["violations"][0]["total"] == 10


def test_violations_are_sorted_by_impact():
    """건수 내림차순 — 36,000건과 5건은 우선순위가 다르다."""
    tbox = _tbox_with_requirement("Big", "p", "D")
    tbox += _tbox_with_requirement("Small", "q", "D")
    abox = _abox("Big", 50) + _abox("Small", 3)

    result = check_existential_participation(abox, tbox)

    assert [v["class"] for v in result["violations"]] == ["Big", "Small"]


def test_expected_type_is_reported():
    """어느 타입을 기대했는지 알려준다 — 배선 복구 판단에 필요하다."""
    tbox = _tbox_with_requirement("C", "p", "TargetClass")
    result = check_existential_participation(_abox("C", 2), tbox)

    assert result["violations"][0]["expected_type"] == "TargetClass"


# ── 보존: 충족한 것을 위반으로 세지 않는다 (NEGATIVE 방향) ──────────────


def test_fully_satisfied_axiom_passes():
    """모든 개체가 관계를 가지면 통과한다 — 항상-실패 게이트가 아닌가."""
    tbox = _tbox_with_requirement("C", "p", "D")
    result = check_existential_participation(
        _abox("C", 8, prop="p", filled=8), tbox,
    )

    assert result["passed"] is True
    assert result["violating_instances"] == 0


def test_one_relation_is_enough():
    """``someValuesFrom`` 은 **존재** 요구다 — 1개면 충족이고 개수를 세지 않는다.

    minCount>1 로 해석하면 정상 데이터를 대량 위반으로 오보고한다.
    """
    tbox = _tbox_with_requirement("C", "p", "D")
    abox = _abox("C", 1)
    subj = next(abox.subjects(RDF.type, NS["C"]))
    abox.add((subj, NS["p"], INST["only_one"]))

    assert check_existential_participation(abox, tbox)["passed"] is True


def test_classes_with_no_instances_are_out_of_scope():
    """인스턴스 0 클래스는 위반이 아니다 (``class_instance_count`` 소관).

    여기서 함께 세면 한 결함이 두 게이트를 red 로 만들어 원인 분리가 어렵다.
    """
    tbox = _tbox_with_requirement("EmptyClass", "p", "D")
    result = check_existential_participation(Graph(), tbox)

    assert result["passed"] is True
    assert result["violating_instances"] == 0


def test_foreign_property_is_not_this_tbox_responsibility():
    """외래 NS 프로퍼티 요구는 검사 대상이 아니다."""
    g = Graph()
    bind_namespaces(g)
    iof = Namespace("https://spec.industrialontologies.org/ontology/core/Core/")
    g.add((NS["C"], RDF.type, OWL.Class))
    node = BNode()
    g.add((node, RDF.type, OWL.Restriction))
    g.add((node, OWL.onProperty, iof["hasParticipant"]))
    g.add((node, OWL.someValuesFrom, iof["Thing"]))
    g.add((NS["C"], RDFS.subClassOf, node))

    result = check_existential_participation(_abox("C", 3), g)

    assert result["axioms_checked"] == 0
    assert result["passed"] is True


# ── 두 축: equivalentClass 도 본다 ──────────────────────────────────────


def test_equivalent_class_restriction_is_also_checked():
    """정의 클래스는 ``equivalentClass`` 로 Restriction 을 붙인다 (step_16).

    한 축만 보면 그쪽 공리를 통째로 놓친다 — 이 리포는 ``subClassOf`` 만 스캔해
    ``equivalentClass`` 정의 클래스 22개를 놓친 이력이 있다.
    """
    tbox = _tbox_with_requirement("C", "p", "D", axis=OWL.equivalentClass)
    result = check_existential_participation(_abox("C", 4), tbox)

    assert result["axioms_checked"] == 1
    assert result["violating_instances"] == 4


# ── 비공허: 검사할 것이 없으면 말한다 ──────────────────────────────────


def test_no_axioms_says_so_explicitly():
    """공리 0건이면 vacuous pass 임을 메시지에 밝힌다.

    조용히 통과하면 "공리가 없다" 와 "전부 충족" 을 구분할 수 없다.
    """
    g = Graph()
    bind_namespaces(g)
    g.add((NS["C"], RDF.type, OWL.Class))

    result = check_existential_participation(_abox("C", 3), g)

    assert result["axioms_checked"] == 0
    assert "vacuous" in str(result["message"]).lower()


def test_axiom_count_is_reported():
    """분모(공리 수)를 함께 준다 — 44/109 와 44/44 는 다른 상황이다."""
    tbox = _tbox_with_requirement("A", "p", "D")
    tbox += _tbox_with_requirement("B", "q", "D")
    result = check_existential_participation(_abox("A", 1), tbox)

    assert result["axioms_checked"] == 2
    assert result["violated_pairs"] == 1


# ── 배선 + 실측 ─────────────────────────────────────────────────────────


def test_registered_in_validate_kg():
    """레지스트리에 등록됐는가 — 함수만 있고 배선이 없으면 영원히 안 돈다."""
    src = pathlib.Path("tools/kg_validation.py").read_text(encoding="utf-8")

    assert "_check_existential_participation" in src, "import 가 없다"
    assert 'reg.register("existential_participation"' in src, "등록되지 않았다"


def test_reexported_from_checks_package():
    from tools.validation_support.checks import check_existential_participation as f

    assert callable(f)


def test_deployed_artifacts_are_actually_examined():
    """배포 산출물 실측 — 게이트가 실물을 **검사하는가**.

    합성 픽스처만으로는 "실물에서 0건이 되는" 사고를 못 잡는다. 이 리포는 게이트가
    실물에서 조용해지는 사고를 반복 겪었다 (값 범위 7규칙 전부 0건 등).

    ## 무엇을 주장하고 무엇을 주장하지 않는가 (2026-08-31 정정)

    예전에는 ``violating_instances >= 1000`` 과 ``axioms_checked >= 50`` 을 요구했다.
    그러면 **위반이 줄면 테스트가 실패한다** — 즉 결함을 고치는 것이 회귀로 보고된다.
    실제로 그렇게 됐다: 생성 가드에 역축을 넣어 거짓 공리 8개를 차단하자 공리
    44개(← 50 미달) / 위반 350건(← 1000 미달)이 되어 이 테스트가 깨졌다.

    이 리포의 원칙 — "0회 발동 게이트는 0회로 정직하게 보고하라 / 과거 발동 횟수를
    테스트에서 요구하지 말고 기록만 하라". 그래서 주장을 바꾼다:

      · **검사 범위** — 공리를 실제로 수집했는가 (0이면 수집 로직이 깨진 것)
      · **판정 형태** — 위반이 있으면 그 보고가 자기정합적인가
      · 위반 **개수** 는 주장하지 않는다. 줄어드는 것이 목표다.
    """
    tb = pathlib.Path("data/generated/tbox/t_box.ttl")
    ab = pathlib.Path("data/generated/abox/a_box.ttl")
    if not (tb.exists() and ab.exists()):
        pytest.skip("배포 산출물 없음")

    tbox = Graph()
    tbox.parse(str(tb), format="turtle")
    abox = Graph()
    abox.parse(str(ab), format="turtle")

    result = check_existential_participation(abox, tbox)

    # 검사 범위: 공리를 하나도 못 찾으면 수집 로직이 깨졌다 (게이트가 공허하다).
    assert result["axioms_checked"] > 0, (
        "배포 T-Box 에서 필수참여 공리를 하나도 수집하지 못했다 — 수집 로직 확인"
    )
    # 위반이 있으면 보고가 자기정합적이어야 한다 (개수는 요구하지 않는다).
    if result["violating_instances"]:
        assert result["violations"], "위반 개체를 셌는데 목록이 비었다"
        top = result["violations"][0]
        assert 0 < top["missing"] <= top["total"], (
            f"위반 보고가 자기모순이다: missing={top['missing']} total={top['total']}"
        )
        # 정렬 계약 — 최다 위반이 먼저 와야 사람이 우선순위를 읽을 수 있다.
        assert top["missing"] == max(v["missing"] for v in result["violations"]), (
            "최다 위반이 목록 첫 항목이 아니다"
        )
    else:
        # 위반 0 은 정상 상태다 (결함이 고쳐졌다는 뜻). 실패로 읽지 않는다.
        assert result["violated_pairs"] == 0


def test_check_count_is_25():
    """check 개수 하드코딩 지점이 함께 갱신됐는가.

    실측 이력: "23 check" 가 CLAUDE.md/README/docs/테스트 11곳에 박혀 있어 하나를
    놓치면 다음 사람이 개수 불일치를 결함으로 오진한다.
    """
    src = pathlib.Path("tests/test_kg_validation.py").read_text(encoding="utf-8")
    assert 'assert len(result["checks"]) == 25' in src, (
        "test_kg_validation 의 기대 개수가 갱신되지 않았다"
    )
    claude = pathlib.Path("CLAUDE.md").read_text(encoding="utf-8")
    assert "25 check" in claude and "24 check" not in claude
    assert "existential_participation" in claude, "CLAUDE.md 표에 누락됐다"
