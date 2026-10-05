"""A-Box 가 쓰는데 T-Box 에 없는 DatatypeProperty 를 잡는다 — 23 check 사각지대 보강.

2026-08-27 실측. ``check_undeclared_op`` 은 **IRI 값 술어**(OP)만 보고, DP 축은 "선언된
DP 가 IRI 값에 쓰였나"(``dp_with_iri_value``) 라는 다른 질문만 다뤘다. 그래서 **A-Box 가
리터럴 값으로 쓰는데 T-Box 에 선언이 없는 DP** 는 23 check 전체에서 사각지대였다.

배포 산출물에서 **18건**이 발견됐고 어느 게이트도 잡지 못했다. 최다 4,320 트리플이다.

## 성격이 둘로 갈린다

    유형 A (10건) 단위 약어 대소문자 skew
                  A-Box `rollingForceKn`         vs T-Box `rollingForceKN`
                  A-Box `blastFurnaceCo2Percent` vs `blastFurnaceCO2Percent`
                  A-Box `steelmakingFurnacePressureKpa` vs `...PressureKPa`
    유형 B (8건)  짝이 없음 — A-Box `airEmissionPollutantType` (1,560건) 인데
                  T-Box 는 `airEmissionMonitoringPollutantType` 을 선언

## 원인은 코드 결함이 아니라 산출물 세대 불일치였다

    A-Box   2026-08-26 01:03
    T-Box   2026-08-27 06:33   ← 그 사이 S2 를 3회 돌렸다

``_col_to_prop`` 을 현재 T-Box 로 직접 호출하면 ``Rolling_Force_kN → rollingForceKN`` 을
정확히 반환한다(실측). ``dcterms:source`` 색인(274 항목)도 온전하다. 즉 **현재 생성기는
올바르고 A-Box 만 낡았다.**

그래서 이 게이트의 값은 **"A-Box 를 다시 만들어야 한다"** 를 알려주는 것이다. 그 신호가
없으면 낡은 A-Box 로 추론·검증을 계속 돌리면서 정의 클래스가 왜 비었는지
(``airEmissionMonitoringPollutantType`` 이 A-Box 0건) 를 엉뚱한 곳에서 찾게 된다 — 실제로
그렇게 됐다.

## 이 테스트의 방향

"미선언을 센다" 만 주장하면 OP 를 이중 계산해도 통과한다. 세 축을 함께 고정한다:

* 검출 — 리터럴 값 미선언 DP 를 잡는다
* 경계 — IRI 값 술어(OP 축)와 표준 어휘는 **세지 않는다** (이중 계산 방지)
* 진단 — skew 후보(``tbox_candidate``)를 함께 준다 (재생성 vs 이름 수정 판단)
"""
from __future__ import annotations

import pathlib

import pytest
from rdflib import OWL, RDF, RDFS, BNode, Graph, Literal, Namespace

from domain.namespaces import DOMAIN_NS, bind_namespaces
from tools.validation_support.checks.referential import (
    check_undeclared_dp,
    check_undeclared_op,
)

NS = Namespace(str(DOMAIN_NS))
INST = Namespace(str(DOMAIN_NS) + "instances#")


def _tbox(*declared_dps: str) -> Graph:
    g = Graph()
    bind_namespaces(g)
    g.add((NS["C"], RDF.type, OWL.Class))
    for name in declared_dps:
        g.add((NS[name], RDF.type, OWL.DatatypeProperty))
        g.add((NS[name], RDFS.domain, NS["C"]))
    return g


def _abox(**used: object) -> Graph:
    g = Graph()
    bind_namespaces(g)
    inst = INST["i1"]
    g.add((inst, RDF.type, NS["C"]))
    for name, value in used.items():
        g.add((inst, NS[name], value if not isinstance(value, str)
               else Literal(value)))
    return g


# ── 검출 ────────────────────────────────────────────────────────────────


def test_undeclared_literal_dp_is_caught():
    """THE REGRESSION: 리터럴 값으로 쓰인 미선언 DP 를 잡는다."""
    result = check_undeclared_dp(_abox(rollingForceKn="12.5"), _tbox("rollingForceKN"))
    assert result["passed"] is False
    assert result["undeclared_count"] == 1
    assert result["undeclared"][0]["property"] == "rollingForceKn"


def test_declared_dp_passes():
    """선언된 DP 는 통과 — 게이트가 항상-실패가 아닌가."""
    result = check_undeclared_dp(_abox(rollingForceKN="12.5"), _tbox("rollingForceKN"))
    assert result["passed"] is True
    assert result["undeclared_count"] == 0


def test_usage_count_is_reported():
    """건수를 함께 준다 — 4,320 트리플 결함과 1건 결함은 우선순위가 다르다."""
    abox = Graph()
    bind_namespaces(abox)
    for i in range(3):
        inst = INST[f"i{i}"]
        abox.add((inst, RDF.type, NS["C"]))
        abox.add((inst, NS["ghostDp"], Literal(str(i))))
    result = check_undeclared_dp(abox, _tbox())
    assert result["undeclared"][0]["usage_count"] == 3


def test_sorted_by_usage():
    """사용량 내림차순 — 큰 결함이 목록 앞에 온다."""
    abox = Graph()
    bind_namespaces(abox)
    for i in range(5):
        inst = INST[f"i{i}"]
        abox.add((inst, RDF.type, NS["C"]))
        abox.add((inst, NS["big"], Literal("x")))
    abox.add((INST["z"], NS["small"], Literal("y")))
    result = check_undeclared_dp(abox, _tbox())
    assert result["undeclared"][0]["property"] == "big"


# ── 경계: OP 축·표준 어휘와 겹치지 않는다 ───────────────────────────────


def test_iri_valued_predicate_is_not_counted():
    """IRI 값 술어는 세지 않는다 — OP 축(``check_undeclared_op``) 소관이다.

    같은 술어를 두 게이트가 이중 계산하면 어느 쪽을 고쳐야 하는지 흐려진다.
    """
    abox = Graph()
    bind_namespaces(abox)
    abox.add((INST["i1"], RDF.type, NS["C"]))
    abox.add((INST["i1"], NS["ghostOp"], INST["i2"]))     # IRI 값
    dp_result = check_undeclared_dp(abox, _tbox())
    assert dp_result["undeclared_count"] == 0, "IRI 값 술어를 DP 로 세었다"
    op_result = check_undeclared_op(abox, _tbox())
    assert op_result["undeclared_count"] == 1, "OP 축이 잡아야 한다"


def test_bnode_valued_predicate_is_not_counted():
    """BNode 값도 DP 가 아니다 (구조 참조)."""
    abox = Graph()
    bind_namespaces(abox)
    abox.add((INST["i1"], NS["ghost"], BNode()))
    assert check_undeclared_dp(abox, _tbox())["undeclared_count"] == 0


def test_standard_vocabulary_is_out_of_scope():
    """``rdfs:label`` 등 비-DOMAIN_NS 술어는 대상이 아니다."""
    abox = Graph()
    bind_namespaces(abox)
    abox.add((INST["i1"], RDFS.label, Literal("이름")))
    abox.add((INST["i1"], RDFS.comment, Literal("설명")))
    assert check_undeclared_dp(abox, _tbox())["undeclared_count"] == 0


@pytest.mark.parametrize(
    "prop_type",
    [OWL.ObjectProperty, OWL.AnnotationProperty, RDF.Property],
)
def test_any_property_declaration_satisfies_the_gate(prop_type):
    """DP 가 아닌 타입으로 선언돼 있어도 "미선언" 은 아니다.

    이 게이트의 질문은 "선언이 있는가" 이고, 타입 오류는 다른 check 소관이다
    (``dp_with_iri_value`` / ``schema_reference_integrity``). 여기서 함께 세면 한
    결함이 두 게이트를 동시에 red 로 만들어 원인 분리가 어려워진다.
    """
    tbox = _tbox()
    tbox.add((NS["oddDp"], RDF.type, prop_type))
    result = check_undeclared_dp(_abox(oddDp="v"), tbox)
    assert result["undeclared_count"] == 0


def test_no_tbox_reports_everything():
    """T-Box 가 없으면 전부 미선언이다 — 조용히 통과하면 안 된다."""
    result = check_undeclared_dp(_abox(anyDp="v"), None)
    assert result["passed"] is False
    assert result["undeclared_count"] == 1


# ── 진단: skew 후보를 제시한다 ──────────────────────────────────────────


def test_case_only_skew_suggests_candidate():
    """대소문자만 다른 T-Box 선언을 skew 후보로 준다 (유형 A)."""
    result = check_undeclared_dp(_abox(rollingForceKn="1"), _tbox("rollingForceKN"))
    assert result["undeclared"][0]["tbox_candidate"] == "rollingForceKN"


def test_class_prefix_skew_suggests_candidate():
    """클래스 접두만 다른 경우도 후보로 준다 (유형 B).

    A-Box ``airEmissionPollutantType`` vs T-Box ``airEmissionMonitoringPollutantType``
    형태 — 접미가 같으므로 같은 컬럼을 가리킬 가능성이 높다.
    """
    result = check_undeclared_dp(
        _abox(pollutantType="CO"), _tbox("airEmissionPollutantType"),
    )
    assert result["undeclared"][0]["tbox_candidate"] == "airEmissionPollutantType"


def test_no_candidate_when_truly_unrelated():
    """무관한 이름에는 후보를 만들지 않는다 — 오도하는 진단은 없는 것보다 나쁘다."""
    result = check_undeclared_dp(_abox(zzzUnrelated="1"), _tbox("rollingForceKN"))
    assert result["undeclared"][0]["tbox_candidate"] is None


# ── 배선: validate_kg 에서 실제로 돈다 ─────────────────────────────────


def test_registered_in_validate_kg():
    """게이트가 등록됐는가 — 함수만 있고 배선이 없으면 영원히 실행되지 않는다."""
    src = pathlib.Path("tools/kg_validation.py").read_text(encoding="utf-8")
    assert "_check_undeclared_dp" in src, "kg_validation 이 import 하지 않는다"
    assert 'reg.register("undeclared_dp"' in src, "레지스트리에 등록되지 않았다"


def test_reexported_from_checks_package():
    """``tools.validation_support.checks`` 에서도 import 가능한가 (기존 관용)."""
    from tools.validation_support.checks import check_undeclared_dp as f
    assert callable(f)


def test_deployed_artifacts_expose_the_skew():
    """배포 산출물 실측 — 세대 불일치가 드러나는가.

    합성 픽스처만으로는 "실물에서 발화하는가" 를 알 수 없다. 이 리포는 게이트가 실물에서
    0건이 되는 사고를 반복 겪었다.
    """
    tb_path = pathlib.Path("data/generated/tbox/t_box.ttl")
    ab_path = pathlib.Path("data/generated/abox/a_box.ttl")
    if not (tb_path.exists() and ab_path.exists()):
        pytest.skip("배포 산출물 없음")
    tbox = Graph()
    tbox.parse(str(tb_path), format="turtle")
    abox = Graph()
    abox.parse(str(ab_path), format="turtle")
    result = check_undeclared_dp(abox, tbox)
    assert result["undeclared_count"] >= 5, (
        f"실물에서 {result['undeclared_count']}건만 잡혔다 — 게이트가 조용하다"
    )
    # skew 후보가 붙은 것이 있어야 "재생성하면 해소" 를 판단할 수 있다.
    with_candidate = [e for e in result["undeclared"] if e["tbox_candidate"]]
    assert with_candidate, "skew 후보가 하나도 제시되지 않았다"
