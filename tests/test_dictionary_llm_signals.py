"""딕셔너리가 LLM 에게 정확한 신호를 주는가 — deprecated / 0행 템플릿 / 빈 참조.

딕셔너리의 소비자는 코드가 아니라 **LLM** 이다. 그래서 스키마 검증(class/OP/DP 매칭률
각 100%)이 통과해도 표기가 틀리면 NL→SPARQL 이 조용히 어긋난다. 2026-08-28 실측으로
세 가지가 확인됐다:

    ① T-Box owl:deprecated 13건  → 딕셔너리 전문에 'deprecated' 문자열 **0회**
    ② question_templates 63개 중 **15개**가 인스턴스 0 클래스를 지목 (11개는 deprecated)
    ③ class_quick_reference 26/95 가 공백 — 최다는 EquipmentOperationRecord(36,560건),
       이 KG **최대 클래스**

## 각각이 무엇을 망치는가

① LLM 이 "쓰지 말라" 고 표시된 어휘를 정상으로 골라 SPARQL 을 만든다.
② "납기 지연 발주 몇 건?" 에 LLM 이 DelayedPurchaseOrder 를 가장 먼저 고르고
   **확정적으로 0행**을 받는다. 데이터가 없는 것이 아니라 그 클래스가 빈 것이다.
③ 추상 부모는 자체 DP/OP 가 없어 빈 문자열이 되는데, LLM 은 그것을 "질의할 속성이
   없다" 로 읽는다. 그래서 가장 큰 클래스가 질의 대상에서 빠진다.

## 이 테스트의 방향

"표식이 있다" 만 주장하면 전 클래스에 붙여도 통과한다. 세 축을 고정한다:

* 표시 — deprecated 클래스에 표식이, 빈 부모에 상속 속성이 붙는다
* 경계 — 정상 클래스는 건드리지 않는다 (과잉 표시는 신호를 무의미하게 만든다)
* 계약 — v1(vocabulary contract)에서는 통계 기반 필터가 발화하지 않는다
  (A-Box 전이므로 instance_count 부재를 0 으로 읽으면 전 클래스가 탈락한다)
"""
from __future__ import annotations

import json
import pathlib

import pytest
from rdflib import OWL, Graph, Literal, Namespace, URIRef

from domain.namespaces import DOMAIN_NS
from tools.semantic_dictionary import (
    _build_class_quick_reference,
    _build_question_templates,
    _descendant_names,
)

NS = Namespace(str(DOMAIN_NS))
DICT_PATH = pathlib.Path("data/generated/semantic_dictionary.json")


def _cls(name: str, *, count: int | None = None, deprecated: bool = False,
         dps: dict | None = None, label_ko: str = "") -> dict:
    entry: dict = {
        "label_en": name, "label_ko": label_ko or name,
        "description_en": "desc", "description_ko": "설명",
        "superclasses": [], "datatype_properties": dps or {},
        "object_properties_outgoing": [], "object_properties_incoming": [],
    }
    if count is not None:
        entry["instance_count"] = count
    if deprecated:
        entry["deprecated"] = True
    return entry


# ── ② 0행 템플릿을 만들지 않는다 ────────────────────────────────────────


def test_zero_instance_class_gets_no_template():
    """THE REGRESSION: 인스턴스 0 클래스는 질의 진입점이 되지 않는다."""
    classes = {
        "Empty": _cls("Empty", count=0, dps={"emptyStatus": {"range": "xsd:string"}}),
    }
    assert _build_question_templates(Graph(), classes) == {}


def test_deprecated_class_gets_no_template():
    """deprecated 클래스도 진입점이 되지 않는다 — 인스턴스가 있어도."""
    classes = {
        "Old": _cls("Old", count=500, deprecated=True,
                    dps={"oldStatus": {"range": "xsd:string"}}),
    }
    assert _build_question_templates(Graph(), classes) == {}


def test_populated_class_still_gets_a_template():
    """정상 클래스는 템플릿을 얻는다 — 필터가 전부를 지우지 않는가."""
    classes = {
        "Live": _cls("Live", count=100, label_ko="라이브",
                     dps={"liveStatus": {"range": "xsd:string"}}),
    }
    templates = _build_question_templates(Graph(), classes)

    assert templates, "정상 클래스의 템플릿이 사라졌다"
    for meta in templates.values():
        assert "Live" in (meta.get("required_classes") or [])


def test_v1_contract_does_not_filter_by_stats():
    """v1 은 instance_count 키가 없다 — 부재를 0 으로 읽으면 전 클래스가 탈락한다."""
    classes = {
        "C": _cls("C", label_ko="씨", dps={"cStatus": {"range": "xsd:string"}}),
    }
    assert _build_question_templates(Graph(), classes), (
        "v1(통계 없음)에서 템플릿이 전멸했다"
    )


# ── ③ 빈 부모를 후손 속성으로 채운다 ────────────────────────────────────


def _subclass_map(pairs: list[tuple[str, str]]) -> dict:
    """[(parent, child)] → {parent_uri: {child_uri}} (생성기와 같은 방향)."""
    out: dict = {}
    for parent, child in pairs:
        out.setdefault(URIRef(str(NS) + parent), set()).add(URIRef(str(NS) + child))
    return out


def test_empty_parent_inherits_child_properties():
    """자체 속성 없는 부모가 후손 속성을 상속 표기로 받는다."""
    classes = {
        "Parent": _cls("Parent", count=1000),
        "Child": _cls("Child", count=1000, dps={"childValue": {"range": "xsd:string"}}),
    }
    result = _build_class_quick_reference(
        classes, _subclass_map([("Parent", "Child")]), str(NS),
    )

    assert result["Parent"], "빈 부모가 여전히 비어 있다"
    assert "추상 클래스" in result["Parent"]
    assert "[Child]" in result["Parent"]
    assert "childValue" in result["Parent"]


def test_class_with_own_properties_is_untouched():
    """자체 속성이 있으면 상속 표기를 덧붙이지 않는다 (NEGATIVE 방향)."""
    classes = {
        "Parent": _cls("Parent", dps={"ownValue": {"range": "xsd:string"}}),
        "Child": _cls("Child", dps={"childValue": {"range": "xsd:string"}}),
    }
    result = _build_class_quick_reference(
        classes, _subclass_map([("Parent", "Child")]), str(NS),
    )

    assert "추상 클래스" not in result["Parent"]
    assert "childValue" not in result["Parent"]
    assert "ownValue" in result["Parent"]


def test_no_subclass_map_keeps_old_behaviour():
    """map 없이 부르면 기존 동작 — 호출자 호환."""
    classes = {"C": _cls("C")}
    assert _build_class_quick_reference(classes) == {"C": ""}


def test_descendant_walk_survives_a_cycle():
    """subClassOf 순환에서 무한 루프에 빠지지 않는다.

    이 리포는 ODP 패턴이 subClassOf 순환을 만든 이력이 있다.
    """
    names = _descendant_names(
        "A", _subclass_map([("A", "B"), ("B", "A")]), str(NS),
    )
    assert names == ["B"]


def test_inherited_properties_are_capped():
    """후손이 많아도 대표 3개까지 — 프롬프트 비용 제어."""
    classes = {"P": _cls("P")}
    pairs = []
    for i in range(6):
        name = f"C{i}"
        classes[name] = _cls(name, dps={f"v{i}": {"range": "xsd:string"}})
        pairs.append(("P", name))

    text = _build_class_quick_reference(classes, _subclass_map(pairs), str(NS))["P"]

    assert text.count("] ") <= 3, f"후손 표기가 3개를 넘는다: {text[:120]}"


# ── ① deprecated 표식 (배포 산출물 실측) ────────────────────────────────


def test_deployed_dictionary_marks_deprecated():
    """T-Box deprecated 가 딕셔너리에 표식으로 전달됐는가."""
    if not DICT_PATH.exists():
        pytest.skip("딕셔너리 없음")

    tb = pathlib.Path("data/generated/tbox/t_box.ttl")
    if not tb.exists():
        pytest.skip("T-Box 없음")

    tbox = Graph()
    tbox.parse(str(tb), format="turtle")
    expected = {
        str(s)[len(str(DOMAIN_NS)):]
        for s in tbox.subjects(OWL.deprecated, Literal(True))
        if str(s).startswith(str(DOMAIN_NS))
    }
    if not expected:
        pytest.skip("T-Box 에 deprecated 클래스가 없다")

    classes = json.loads(DICT_PATH.read_text(encoding="utf-8"))["classes"]
    marked = {k for k, v in classes.items() if isinstance(v, dict) and v.get("deprecated")}

    assert expected <= marked, f"표식 누락: {sorted(expected - marked)}"
    for name in sorted(expected)[:3]:
        assert "DEPRECATED" in classes[name]["description_ko"], (
            f"{name}: 설명문에 표식이 없다 — LLM 은 설명을 먼저 읽는다"
        )


def test_deployed_templates_avoid_empty_classes():
    """배포 딕셔너리의 템플릿이 0행 진입점을 만들지 않는가."""
    if not DICT_PATH.exists():
        pytest.skip("딕셔너리 없음")

    d = json.loads(DICT_PATH.read_text(encoding="utf-8"))
    classes, templates = d["classes"], d["question_templates"]
    zero = {
        k for k, v in classes.items()
        if isinstance(v, dict) and not v.get("instance_count")
    }
    bad = {
        q: [c for c in (m.get("required_classes") or []) if c in zero]
        for q, m in templates.items()
        if any(c in zero for c in (m.get("required_classes") or []))
    }

    assert not bad, f"확정 0행 템플릿 {len(bad)}건: {list(bad)[:5]}"
    assert len(templates) >= 20, f"템플릿이 {len(templates)}개로 과도하게 줄었다"


def test_deployed_quick_reference_covers_largest_classes():
    """인스턴스가 많은 클래스에 질의 가능 속성이 표시되는가."""
    if not DICT_PATH.exists():
        pytest.skip("딕셔너리 없음")

    d = json.loads(DICT_PATH.read_text(encoding="utf-8"))
    classes, quick = d["classes"], d["class_quick_reference"]
    top = sorted(
        ((v.get("instance_count") or 0, k) for k, v in classes.items()
         if isinstance(v, dict)),
        reverse=True,
    )[:10]

    blank = [k for _, k in top if not quick.get(k)]
    assert not blank, f"최대 클래스 10개 중 공백: {blank}"
