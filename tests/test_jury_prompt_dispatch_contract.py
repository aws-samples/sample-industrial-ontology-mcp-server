"""Jury 프롬프트가 열거하는 action 과 실제 핸들러 계약이 어긋나지 않는다.

2026-08-19 S2 실측: 최종 Jury 의 ``required_fixes`` 40건 중 33 적용 / 3 실패이고,
실패 3건이 전부 **필드 스키마가 문서화되지 않은 action** 이었다:

    [('add_class_hierarchy','hierarchy 에서 추가할 엔티티 없음'),
     ('add_disjoint_classes','members list(>=2) 누락'),
     ('add_disjoint_classes','members list(>=2) 누락')]

``add_disjoint_classes`` 는 **Architect 프롬프트에는** ``{{members: [...]}}`` 가
있는데 **Jury 프롬프트에만 없었다** — 같은 action, 두 프롬프트, 한쪽만 계약을 안다.
23개 action 이름을 열거하면서 필드 스키마는 9개에만 있었으므로 Jury 는 나머지
12개의 키를 **추측** 했다.

## 두 프롬프트의 DSL 은 필드 이름이 다르다 (중요)

같은 action 이라도 Architect DSL 과 Jury DSL 의 키가 다르다:

===========================  ====================  ====================
action                       Architect             Jury (핸들러)
===========================  ====================  ====================
add_object_property          ``name``              ``property``
add_restriction              ``target_class`` /    ``class`` /
                             ``on_property``       ``onProperty``
add_functional_property      ``name``              ``property``
===========================  ====================  ====================

따라서 "Architect 블록에서 복사" 는 **틀린 해결**이다. 이 파일은 Jury 프롬프트의
문서화가 ``tools.jury_fixes._DISPATCH`` 핸들러의 실제 ``action.get(...)`` 계약과
일치하는지를 본다.

## 주장 방향

- 열거된 모든 action 이 **실제 디스패치에 존재**한다 (죽은 이름 금지).
- 열거된 모든 action 이 **필드 스키마를 갖는다** (추측 금지).
- 문서화한 필드로 실제 호출하면 **적용된다** (산출물로 확인 — 문자열 검사만으로는
  프롬프트가 거짓말하는 것을 못 잡는다).
"""
from __future__ import annotations

import re

import pytest
from rdflib import OWL, RDF, Graph

from domain.namespaces import DOMAIN_NS, NS_PREFIX
from tools.jury_fixes import _DISPATCH, apply_jury_fixes
from tools.multi_agent_prompts import jury_static_prefix

_BLOCK = jury_static_prefix()
_NAME_SECTION_HEAD = "## required_fixes action 이름"
_SCHEMA_SECTION_HEAD = "## required_fixes 필드 이름"

_HDR = (
    f"@prefix {NS_PREFIX}: <{DOMAIN_NS}> .\n"
    "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
    "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
    "@prefix dcterms: <http://purl.org/dc/terms/> .\n"
)
_TTL = _HDR + f"""
{NS_PREFIX}:A a owl:Class .
{NS_PREFIX}:B a owl:Class .
{NS_PREFIX}:C a owl:Class .
{NS_PREFIX}:oldProp a owl:ObjectProperty ;
    rdfs:domain {NS_PREFIX}:A ; rdfs:range {NS_PREFIX}:B .
"""


def _listed_actions() -> set[str]:
    """프롬프트의 'action 이름' 절에 열거된 이름."""
    section = _BLOCK.split(_NAME_SECTION_HEAD)[1].split("**위 이름 외")[0]
    return set(re.findall(r"`([a-z_]+)`", section))


def _documented_actions() -> set[str]:
    """'필드 이름' 절에서 ``- `name`:`` 형태로 스키마가 제시된 이름."""
    section = _BLOCK.split(_SCHEMA_SECTION_HEAD)[1]
    out: set[str] = set(re.findall(r"`([a-z_]+)`(?=\s*:)", section))
    for a, b in re.findall(r"`([a-z_]+)`\s*/\s*`([a-z_]+)`\s*:", section):
        out |= {a, b}
    return out


def test_prompt_has_both_sections():
    """절 제목이 바뀌면 아래 검사들이 조용히 무력화된다 — 먼저 고정한다."""
    assert _NAME_SECTION_HEAD in _BLOCK
    assert _SCHEMA_SECTION_HEAD in _BLOCK


def test_every_listed_action_is_dispatchable():
    """열거된 이름이 실제 핸들러에 도달한다 — 죽은 이름을 권하지 않는다."""
    live = {k for k, v in _DISPATCH.items() if v is not None}
    dead = sorted(_listed_actions() - live)
    assert not dead, f"프롬프트가 권하는데 핸들러가 없다: {dead}"


def test_every_listed_action_has_a_field_schema():
    """열거된 모든 action 에 필드 스키마가 있다 (Jury 가 키를 추측하지 않게).

    실측: 23개 열거 중 9개만 문서화돼 있었고, 미문서 action 이 최종 라운드
    실패 3건 전부를 만들었다.
    """
    missing = sorted(_listed_actions() - _documented_actions())
    assert not missing, (
        "필드 스키마가 없어 Jury 가 키를 추측한다 (실패로 이어진다): "
        f"{missing}"
    )


def test_documented_schema_names_are_not_architect_variants():
    """Architect DSL 의 키를 잘못 복사하지 않았는지 확인.

    ``add_restriction`` 은 Jury 핸들러가 ``class`` / ``onProperty`` 를 읽는다.
    Architect 표기(``target_class`` / ``on_property``)를 쓰면 적용되지 않는다.
    """
    section = _BLOCK.split(_SCHEMA_SECTION_HEAD)[1]
    for architect_only in ("target_class", "on_property"):
        assert architect_only not in section, (
            f"Jury 프롬프트에 Architect 전용 키 `{architect_only}` 가 있다 — "
            "핸들러는 그 이름을 읽지 않는다"
        )


# ──────────────────────────────────────────────────────────────────
# 산출물로 확인 — 문서화한 필드가 실제로 통하는가
# ──────────────────────────────────────────────────────────────────

def test_add_disjoint_classes_with_documented_fields_applies():
    """실측 실패 2건의 action — 문서화한 ``members`` 로 실제 적용되는가."""
    res = apply_jury_fixes(_TTL, [{
        "action": "add_disjoint_classes",
        "members": [f"{NS_PREFIX}:A", f"{NS_PREFIX}:B"],
    }])
    assert not res["failed"], res["failed"]
    g = Graph()
    g.parse(data=res["ttl"], format="turtle")
    assert list(g.subjects(RDF.type, OWL.AllDisjointClasses))


@pytest.mark.parametrize(
    ("action", "extra"),
    [
        ("add_subclass_batch", {"parent": f"{NS_PREFIX}:A",
                                "children": [f"{NS_PREFIX}:B"]}),
        ("add_functional_property", {"property": f"{NS_PREFIX}:oldProp"}),
        ("remove_object_property", {"property": f"{NS_PREFIX}:oldProp"}),
        ("remove_all_restrictions", {"class": f"{NS_PREFIX}:A"}),
        ("remove_duplicate", {"target": f"{NS_PREFIX}:oldProp"}),
        ("rename_uri", {"from": f"{NS_PREFIX}:oldProp",
                        "to": f"{NS_PREFIX}:newProp"}),
        ("add_iof_mappings", {"mappings": [
            {"class": f"{NS_PREFIX}:A", "iofParent": "iof-core:MaterialArtifact"}]}),
        ("add_class_hierarchy", {"hierarchy": [
            {"parent": f"{NS_PREFIX}:Group", "children": [f"{NS_PREFIX}:A"]}]}),
        ("remove_annotations", {"targets": [f"{NS_PREFIX}:A"]}),
        ("split_property", {"target": f"{NS_PREFIX}:oldProp",
                            "splits": [{"property": f"{NS_PREFIX}:p1",
                                        "domain": f"{NS_PREFIX}:A",
                                        "range": f"{NS_PREFIX}:B"}]}),
    ],
)
def test_documented_fields_do_not_produce_contract_errors(action, extra):
    """문서화한 필드로 호출하면 '누락' 계열 실패가 나오지 않는다.

    no-op (이미 그 상태 / 대상 없음) 은 허용한다 — 이 테스트가 보는 것은
    **계약 위반이 사라졌는가** 다. 픽스처가 모든 대상을 갖출 필요는 없다.
    """
    res = apply_jury_fixes(_TTL, [{"action": action, **extra}])
    contract_errors = [
        f for f in res["failed"]
        if any(k in str(f.get("reason", ""))
               for k in ("누락", "필요", "필수", "미지원"))
    ]
    assert not contract_errors, (
        f"{action}: 문서화한 필드인데 계약 위반이 났다 — 프롬프트가 틀렸다: "
        f"{contract_errors}"
    )


def test_prompt_discourages_bracketed_iris():
    """``<...>`` 표기 금지 안내가 있다 (해석기가 벗기지만 오해를 줄인다).

    브래킷 IRI 는 한 실행에서 지시 6건을 폐기시켰다 (커밋 71a08fc).
    """
    section = _BLOCK.split(_SCHEMA_SECTION_HEAD)[1]
    assert "<...>" in section or "감싸지 마" in section
