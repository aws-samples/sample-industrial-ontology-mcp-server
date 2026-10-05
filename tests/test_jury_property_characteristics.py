"""Regression: 프롬프트-코드 계약 불일치로 Jury 지시가 조용히 버려지지 않는가.

2026-08-19 실측 (S2 58.5분 실행, 마지막 라운드):
``required_fixes`` 128건 중 **19건 실패, 사유 전부 "target 누락"**.
원인은 LLM 이 아니라 **계약 불일치** 였다:

- 프롬프트(``multi_agent_prompts.py`` 282~284, 303행 예시): ``{{name}}`` 을 지시
- 핸들러(``jury_fixes.py``): ``action.get("target")`` 만 읽음

게다가 프롬프트가 지시하는 ``add_inverse_functional_property`` /
``add_transitive_property`` 는 **레지스트리에 아예 없어서** Jury 가 규칙대로
지시해도 "미지원 action" 으로 버려졌다. 실효 적용률 73% (94/128).

이 리포에서 반복된 유형이다 — LLM 에게 지시한 형식을 코드가 읽지 않으면 그 지시는
조용히 폐기되고, 카운터만 보면 "LLM 이 형식을 안 지켰다" 로 오진한다.

**이 파일이 주장하는 것**: 카운터가 아니라 **그래프 산출물** 이다 — 어떤 필드명으로
지시해도 올바른 OWL 타입이 실제로 부여되는가, 그리고 종류를 혼동하지 않는가.

## 2026-08-30 정정 — IFP 는 등록이 아니라 **거부**가 정답이다

위 "레지스트리에 아예 없어서 버려졌다" 를 근거로 IFP 핸들러를 추가한 것은
**절반만 맞았다**. 세 사실이 함께 성립한다:

1. DSL 엔진은 IFP 를 **의도적으로** 거부한다 (``multi_agent_tbox`` 의
   "IFP 는 추론기 sameAs 폭발을 유발해 의도적으로 거부", 8런 40회 전량 폐기).
2. ``jury_fixes`` 는 같은 IFP 를 **적용했다** — 두 엔진의 정책이 갈렸고,
   ``_DSL_ONLY_JURY_ACTIONS`` 라우팅이 우연히 막고 있었을 뿐이다.
3. S3 ``step_13a-2`` (``_strip_inverse_functional_property``) 가 **모든 IFP
   선언을 제거한다**. 배포 T-Box 의 IFP 는 0개다.

즉 jury 경로로 적용된 IFP 는 다음 후처리에서 삭제되므로 그 적용은 처음부터
효과가 없었다. 식별자 유일성은 ``owl:hasKey`` (배포 31건) 가 표현한다.

그래서 IFP 는 :data:`tools.action_registry.POLICY_REJECTED` 로 옮기고 **두 엔진
모두** 거부하게 했다. 이 파일의 IFP 케이스는 "적용된다" 가 아니라 "정책상 거부된다"
를 주장한다 — 나머지 세 특성(functional/transitive/symmetric)은 그대로 적용된다.
"""

import pytest
from rdflib import OWL, RDF, Graph, URIRef

from tools.jury_fixes import apply_jury_fixes

NS = "http://example.com/steel-ontology#"

_TTL = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix steel: <http://example.com/steel-ontology#> .
steel:planId a owl:DatatypeProperty .
steel:resultId a owl:DatatypeProperty .
steel:hasCode a owl:DatatypeProperty .
steel:hasPart a owl:ObjectProperty .
steel:connectedTo a owl:ObjectProperty .
"""


def _typed(ttl: str, owl_type) -> set[str]:
    """결과 TTL 에서 해당 OWL 타입을 가진 프로퍼티 local name 집합."""
    g = Graph()
    g.parse(data=ttl, format="turtle")
    return {str(s).replace(NS, "") for s in g.subjects(RDF.type, owl_type)}


# ── 계약: 프롬프트가 지시하는 필드명이 동작하는가 ────────────────────────

def test_prompt_documented_name_field_works():
    """프롬프트가 지시하는 ``name`` 으로 실제 타입이 부여된다 (실측 실패 케이스)."""
    r = apply_jury_fixes(
        _TTL, [{"action": "add_functional_property", "name": "planId"}],
    )
    assert not r["failed"], f"프롬프트대로 지시했는데 실패했다: {r['failed']}"
    assert "planId" in _typed(r["ttl"], OWL.FunctionalProperty)


def test_legacy_target_field_still_works():
    """기존 ``target`` 도 계속 받는다 (하위 호환 — 이전 실행/테스트 보존)."""
    r = apply_jury_fixes(
        _TTL, [{"action": "add_functional_property", "target": "hasPart"}],
    )
    assert not r["failed"]
    assert "hasPart" in _typed(r["ttl"], OWL.FunctionalProperty)


@pytest.mark.parametrize("field", ["name", "target", "property", "properties"])
def test_all_accepted_name_fields(field):
    """허용 필드 전부가 동작한다 — 어느 하나만 되면 나머지 지시가 유실된다."""
    r = apply_jury_fixes(
        _TTL, [{"action": "add_functional_property", field: "resultId"}],
    )
    assert not r["failed"], f"{field} 로 지시했는데 실패: {r['failed']}"
    assert "resultId" in _typed(r["ttl"], OWL.FunctionalProperty)


def test_comma_split_multiple_properties():
    """Jury 는 한 action 에 여러 개를 콤마로 넘긴다 — 전부 적용된다."""
    r = apply_jury_fixes(
        _TTL,
        [{"action": "add_functional_property", "name": "planId, resultId, hasCode"}],
    )
    assert not r["failed"]
    got = _typed(r["ttl"], OWL.FunctionalProperty)
    assert {"planId", "resultId", "hasCode"} <= got, f"일부만 적용됨: {got}"


def test_list_form_also_accepted():
    """리스트 형태도 처리한다 (properties: [...] 표기)."""
    r = apply_jury_fixes(
        _TTL,
        [{"action": "add_functional_property", "properties": ["planId", "hasCode"]}],
    )
    assert not r["failed"]
    assert {"planId", "hasCode"} <= _typed(r["ttl"], OWL.FunctionalProperty)


# ── 레지스트리: 프롬프트가 지시하는 action 이 전부 등록됐는가 ────────────

@pytest.mark.parametrize("action,owl_type,prop", [
    ("add_functional_property", OWL.FunctionalProperty, "planId"),
    ("add_transitive_property", OWL.TransitiveProperty, "hasPart"),
    ("add_symmetric_property", OWL.SymmetricProperty, "connectedTo"),
])
def test_all_property_characteristic_actions_registered(action, owl_type, prop):
    """허용된 특성 action 이 dispatch 되고 **올바른 타입** 을 부여한다.

    예전에는 functional 하나만 등록돼 있어 나머지는 "미지원 action" 으로 skip 됐다.
    IFP 는 정책상 거부 대상이므로 아래 별도 테스트가 담당한다.
    """
    r = apply_jury_fixes(_TTL, [{"action": action, "name": prop}])
    assert not r["skipped"], f"{action} 이 미지원으로 skip 됐다: {r['skipped']}"
    assert not r["failed"], f"{action} 실패: {r['failed']}"
    assert prop in _typed(r["ttl"], owl_type), (
        f"{action} 이 {owl_type} 를 부여하지 않았다"
    )


def test_inverse_functional_property_is_policy_rejected():
    """IFP 는 **적용되지 않는다** — 두 엔진의 정책이 같아야 한다.

    적용해도 S3 ``step_13a-2`` 가 전량 삭제하므로 효과가 없고, 그 사이 추론기
    prp-ifp 가 개체를 병합할 위험만 남는다. 모듈 docstring 의 2026-08-30 정정 참조.
    """
    r = apply_jury_fixes(
        _TTL, [{"action": "add_inverse_functional_property", "name": "planId"}],
    )

    assert not r["applied"], "IFP 가 적용됐다 — DSL 엔진의 거부와 어긋난다"
    assert len(r["skipped"]) == 1
    assert "정책" in str(r["skipped"][0]["reason"])
    assert "planId" not in _typed(r["ttl"], OWL.InverseFunctionalProperty)


def test_characteristics_are_not_confused():
    """종류를 혼동하지 않는다 — transitive 지시가 functional 로 새면 의미가 바뀐다."""
    r = apply_jury_fixes(
        _TTL, [{"action": "add_transitive_property", "name": "hasPart"}],
    )
    assert "hasPart" in _typed(r["ttl"], OWL.TransitiveProperty)
    assert "hasPart" not in _typed(r["ttl"], OWL.FunctionalProperty), (
        "transitive 지시가 FunctionalProperty 로 잘못 부여됐다"
    )


def test_camelcase_variant_keeps_correct_characteristic():
    """표기 변종(camelCase)도 종류 판별을 유지한다 (apply_jury_fixes 경로).

    이 경로는 진입 시 ``normalize_jury_actions`` 가 표기를 접으므로 핸들러에는
    정규형이 도달한다 — 그래도 계약으로 못박는다 (정규화가 빠지면 여기서 잡힌다).
    """
    r = apply_jury_fixes(
        _TTL, [{"action": "addTransitiveProperty", "name": "connectedTo"}],
    )
    assert not r["skipped"] and not r["failed"], (
        f"camelCase 변종이 처리되지 않았다: {r['skipped']} {r['failed']}"
    )
    assert "connectedTo" in _typed(r["ttl"], OWL.TransitiveProperty)
    assert "connectedTo" not in _typed(r["ttl"], OWL.FunctionalProperty), (
        "표기 변종에서 종류가 Functional 로 잘못 떨어졌다"
    )


def test_direct_handler_call_folds_variant_notation():
    """핸들러를 **직접** 호출해도 표기 변종에서 종류를 맞힌다.

    ``apply_jury_fixes`` 를 우회하는 호출자(테스트/다른 스텝)는 정규화를 타지
    않는다. 조회 실패 시 기본값으로 떨어지면 transitive 지시가 의미가 다른
    공리(Functional)로 조용히 바뀐다 — 카운터는 성공으로 보인다.
    """
    from rdflib import Graph

    from tools.jury_fixes import _apply_property_characteristic

    g = Graph()
    g.parse(data=_TTL, format="turtle")
    ok, msg = _apply_property_characteristic(
        g, {"action": "addTransitiveProperty", "name": "connectedTo"},
    )
    assert ok, msg
    prop = URIRef(NS + "connectedTo")
    assert (prop, RDF.type, OWL.TransitiveProperty) in g
    assert (prop, RDF.type, OWL.FunctionalProperty) not in g, (
        "직접 호출에서 종류가 Functional 로 잘못 떨어졌다"
    )


def test_unknown_characteristic_fails_instead_of_defaulting():
    """종류를 판별할 수 없으면 **부여하지 않고** 실패로 보고한다.

    기본값(Functional)으로 떨어지면 요청하지 않은 공리가 조용히 추가된다 —
    OWL 의미가 달라지는데 로그는 성공이다.
    """
    from rdflib import Graph

    from tools.jury_fixes import _apply_property_characteristic

    g = Graph()
    g.parse(data=_TTL, format="turtle")
    before = len(g)
    ok, msg = _apply_property_characteristic(
        g, {"action": "bogus_characteristic", "name": "connectedTo"},
    )
    assert ok is False
    assert "판별할 수 없다" in msg, f"사유가 불명확: {msg}"
    assert len(g) == before, "판별 실패인데 트리플이 추가됐다"


# ── NEGATIVE: 진짜 결손은 명확히 실패하는가 ─────────────────────────────

def test_missing_name_fails_with_actionable_reason():
    """이름이 정말 없으면 실패하고, 사유가 어느 필드를 기대하는지 알려준다.

    빈 성공으로 넘기면 "적용됐다" 는 카운터만 남고 그래프는 안 바뀐다.
    """
    r = apply_jury_fixes(_TTL, [{"action": "add_functional_property"}])
    assert len(r["failed"]) == 1
    reason = r["failed"][0]["reason"]
    assert "name" in reason and "target" in reason, (
        f"사유가 기대 필드를 알려주지 않는다: {reason}"
    )


def test_already_typed_is_noop_not_failure():
    """이미 그 타입이면 no-op 으로 분류한다 (실패로 세면 게이트가 오보고한다)."""
    once = apply_jury_fixes(
        _TTL, [{"action": "add_functional_property", "name": "planId"}],
    )
    twice = apply_jury_fixes(
        once["ttl"], [{"action": "add_functional_property", "name": "planId"}],
    )
    assert not twice["failed"], f"중복 지시가 실패로 분류됐다: {twice['failed']}"
    assert twice["noop"], "no-op 으로 기록되지 않았다"


def test_unknown_property_does_not_crash():
    """T-Box 에 없는 프로퍼티 이름도 예외 없이 처리한다 (라운드를 죽이지 않는다)."""
    r = apply_jury_fixes(
        _TTL, [{"action": "add_functional_property", "name": "doesNotExist"}],
    )
    # 선언 없는 이름에 타입만 붙는 것은 허용 — 중요한 건 예외로 죽지 않는 것.
    assert isinstance(r["ttl"], str) and "@prefix" in r["ttl"]


# ── 계약 대조: 프롬프트와 레지스트리가 어긋나지 않는가 ──────────────────

def test_prompt_documented_actions_are_all_dispatchable():
    """프롬프트가 지시하는 특성 action 이 모두 dispatch 표에 있다.

    이 대조가 없으면 프롬프트에 action 을 추가하고 레지스트리 등록을 잊는 사고가
    재발한다 — 그때 Jury 지시는 "미지원" 으로 조용히 버려진다.
    """
    import re
    from pathlib import Path

    from tools.jury_fixes import _DISPATCH
    prompt = Path("tools/multi_agent_prompts.py").read_text(encoding="utf-8")
    documented = set(re.findall(r"`(add_\w*_?property)`", prompt))
    characteristic = {a for a in documented if a.endswith("_property")
                      and any(k in a for k in
                              ("functional", "transitive", "symmetric"))}
    assert characteristic, "테스트 전제 붕괴 — 프롬프트에서 특성 action 을 못 찾았다"
    missing = sorted(a for a in characteristic if a not in _DISPATCH)
    assert not missing, (
        f"프롬프트가 지시하지만 dispatch 에 없다 (Jury 지시가 버려진다): {missing}"
    )
