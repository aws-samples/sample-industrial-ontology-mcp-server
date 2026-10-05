"""Jury action 정규화 계층 — 보존 방향 우선.

2026-08-10 S2 실행 실측: Jury 가 낸 `required_fixes` 41건 중 **37건이 폐기**됐다
(`ok=4 failed=37`). 원인은 논리가 아니라 **키 이름**이다 — Jury 는 `uri` /
`filler` / `restriction_type` 을, 핸들러는 `class` / `someValuesFrom` / `type` 을
쓴다. `jury_static_prefix` 는 action **이름만** 나열하고 필드 스키마를 문서화하지
않으므로 Jury 는 키를 추측한다 (R28 D 에서 의도적으로 제거됨).

이 파일이 고정하는 계약 — 순서가 의도적이다:

1. **보존**: 이미 정규형인 action 은 정규화 후에도 **그대로** 다 (비파괴).
   이것이 먼저다. "접었다" 를 세는 카운터 테스트는 mass-wipe 버그가 들어와도
   초록이 되므로 쓸모가 없다.
2. **역할 의존 키는 접지 않는다**: `property` 는 add_object_property 에서 식별자,
   add_restriction 에서 onProperty, remove_all_restrictions 에서 **삭제 필터** 다.
   후자에서 접으면 표적 삭제가 전면 삭제로 번지면서 `ok` 로 보고된다.
3. **고유 컨테이너 키 보호**: `targets` 는 remove_annotations 의 계약이다.
   전역 배치 키로 다루면 그 핸들러가 파괴된다 (프로토타입에서 실제로 발생).
4. **교정**: 실제 Jury payload 가 적용된다.
"""
from __future__ import annotations

import pytest

from tools.jury_action_normalizer import canonical_action_name, normalize_jury_actions

# ── 1. 보존 방향 (가장 중요) ──────────────────────────────────────────

#: 핸들러 계약을 정확히 지키는 action 들. 정규화가 이들을 바꾸면 회귀다.
_CANONICAL = [
    {"action": "add_object_property", "property": "steel:p",
     "domain": "steel:A", "range": "steel:B"},
    {"action": "add_datatype_property", "property": "steel:dp",
     "domain": "steel:A", "range": "xsd:string"},
    {"action": "add_class", "class": "steel:C", "superClass": "steel:P"},
    {"action": "add_subclass", "class": "steel:C", "superClass": "steel:P"},
    {"action": "add_restriction", "class": "steel:A",
     "onProperty": "steel:p", "someValuesFrom": "steel:B"},
    {"action": "add_restriction", "class": "steel:A",
     "onProperty": "steel:p", "minCardinality": 1},
    {"action": "add_disjoint_classes", "members": ["steel:A", "steel:B"]},
    {"action": "add_inverse_property", "property": "steel:p",
     "inverseOf": "steel:q"},
    {"action": "add_triple", "subject": "steel:A",
     "predicate": "rdfs:label", "object": "x"},
    {"action": "remove_class", "class": "steel:A"},
    {"action": "remove_object_property", "property": "steel:p"},
    {"action": "keep_no_domain", "property": "steel:p"},
    {"action": "add_functional_property", "target": "steel:p"},
    {"action": "rename_uri", "from": "steel:a", "to": "steel:b"},
    {"action": "add_triples", "triples": [
        {"subject": "steel:A", "predicate": "rdfs:label", "object": "x"}]},
    {"action": "modify_triple", "changes": [
        {"subject": "steel:A", "predicate": "owl:deprecated",
         "old_object": "x", "new_object": "y"}]},
    {"action": "add_class_hierarchy", "hierarchy": [
        {"parent": "steel:P", "children": ["steel:A", "steel:B"]}]},
    {"action": "remove_annotations", "targets": [
        {"subject": "steel:A", "predicate": "rdfs:label", "value": "x"}]},
    {"action": "add_subclass_batch", "parent": "steel:P",
     "children": ["steel:A", "steel:B"]},
    {"action": "split_property", "target": "steel:p", "splits": [{"name": "x"}]},
]


@pytest.mark.parametrize("action", _CANONICAL, ids=lambda a: a["action"])
def test_canonical_actions_pass_through_unchanged(action):
    """THE PRESERVATION CONTRACT: 정규형은 1:1 로, 내용 변경 없이 통과한다."""
    out, rejected = normalize_jury_actions([action])
    assert not rejected
    assert len(out) == 1, f"정규형이 {len(out)}개로 늘었다 — 잘못된 배치 전개"
    assert out[0] == action, (
        f"정규형이 변경됐다\n  before: {action}\n  after : {out[0]}"
    )


def test_native_container_keys_are_never_treated_as_batches():
    """고유 컨테이너 키를 전역 배치 키로 다루면 그 핸들러가 파괴된다.

    ``remove_annotations`` 의 계약은 ``targets=[{subject,predicate,value}]`` 다.
    ``targets`` 를 전역 배치 키로 두면 이 action 이 subject/predicate/value 를
    가진 별개 action 들로 흩어져 원래 핸들러가 읽을 수 없다 (프로토타입에서 실제
    발생: added=[predicate,subject,value] lost=[targets]).
    """
    action = {"action": "remove_annotations", "targets": [
        {"subject": "steel:A", "predicate": "rdfs:label", "value": "x"}]}
    out, _ = normalize_jury_actions([action])
    assert len(out) == 1
    assert out[0]["targets"] == action["targets"]
    assert "subject" not in out[0], "targets 가 전역 배치로 오인돼 흩어졌다"


def test_remove_all_restrictions_property_filter_is_not_folded():
    """``property`` 를 접으면 표적 삭제가 전면 삭제로 번진다.

    ``remove_all_restrictions`` 의 ``property`` 는 **삭제 대상을 좁히는 필터** 다.
    이것을 ``onProperty`` 로 접거나 다른 뜻으로 바꾸면 좁힌 삭제가 넓어지면서도
    ``ok`` 로 보고된다 — 조용한 파괴다.
    """
    action = {"action": "remove_all_restrictions",
              "class": "steel:A", "property": "steel:p"}
    out, _ = normalize_jury_actions([action])
    assert out[0] == action, f"필터 키가 변형됐다: {out[0]}"


def test_object_property_identifier_is_not_read_as_on_property():
    """같은 이름 다른 뜻: ``property`` 는 OP 에서 식별자, restriction 에서 onProperty."""
    op = {"action": "add_object_property", "property": "steel:p",
          "domain": "steel:A", "range": "steel:B"}
    out, _ = normalize_jury_actions([op])
    assert out[0]["property"] == "steel:p"
    assert "onProperty" not in out[0], "OP 식별자가 onProperty 로 새어나갔다"


# ── 2. 교정 방향 (실제 Jury payload) ──────────────────────────────────


def test_jury_uri_key_folds_to_handler_key():
    """THE REGRESSION: Jury 의 ``uri`` 가 핸들러의 식별자 키로 접힌다."""
    out, _ = normalize_jury_actions([
        {"action": "add_object_property", "uri": "steel:p",
         "domain": "steel:A", "range": "steel:B"},
        {"action": "add_class", "uri": "steel:C", "subClassOf": "iof-core:X"},
        {"action": "remove_class", "uri": "steel:D"},
        {"action": "add_datatype_property", "uri": "steel:dp",
         "domain": "steel:A", "range": "xsd:string"},
    ])
    assert out[0]["property"] == "steel:p"
    assert out[1]["class"] == "steel:C" and out[1]["superClass"] == "iof-core:X"
    assert out[2]["class"] == "steel:D"
    assert out[3]["property"] == "steel:dp"


def test_restriction_filler_moves_to_the_key_named_by_type():
    """핸들러는 제약 종류마다 **별개 키** 를 읽는다. Jury 는 filler 로 보낸다."""
    cases = [
        ("someValuesFrom", "someValuesFrom"),
        ("allValuesFrom", "allValuesFrom"),
        ("hasValue", "hasValue"),
        ("minCardinality", "minCardinality"),
        ("min", "minCardinality"),
        ("maxCardinality", "maxCardinality"),
        ("cardinality", "cardinality"),
        ("exact", "cardinality"),
    ]
    for rtype, expected_key in cases:
        out, _ = normalize_jury_actions([{
            "action": "add_restriction", "class": "steel:A",
            "restriction_type": rtype, "property": "steel:p", "filler": "steel:B",
        }])
        assert out[0]["class"] == "steel:A"
        assert out[0]["onProperty"] == "steel:p", rtype
        assert out[0].get(expected_key) == "steel:B", (
            f"type={rtype} 의 값이 {expected_key} 로 옮겨지지 않았다: {out[0]}"
        )


def test_batch_triples_expand_into_singular_actions():
    """Jury 는 ``add_triple`` 에 배열을 담아 보낸다 (핸들러는 스칼라만 받는다)."""
    out, _ = normalize_jury_actions([{
        "action": "add_triple", "triples": [
            {"subject": "steel:A", "predicate": "rdfs:subClassOf", "object": "steel:P"},
            {"subject": "steel:B", "predicate": "rdfs:subClassOf", "object": "steel:P"},
        ],
    }])
    assert len(out) == 2
    assert all(a["action"] == "add_triple" for a in out)
    assert {a["subject"] for a in out} == {"steel:A", "steel:B"}


def test_batch_shares_outer_keys_with_each_item():
    """배치 바깥의 공통 키(domain 등)는 각 항목에 상속된다."""
    out, _ = normalize_jury_actions([{
        "action": "add_object_property", "domain": "steel:A",
        "triples": [{"uri": "steel:p1", "range": "steel:B"},
                    {"uri": "steel:p2", "range": "steel:C"}],
    }])
    assert len(out) == 2
    assert all(a["domain"] == "steel:A" for a in out)
    assert [a["property"] for a in out] == ["steel:p1", "steel:p2"]


def test_scalar_batch_items_land_on_the_primary_key():
    """``targets: ["steel:Duplicate"]`` 같은 스칼라 배치도 전개된다."""
    out, _ = normalize_jury_actions([
        {"action": "remove_class", "targets": ["steel:D1", "steel:D2"]}])
    assert len(out) == 2
    assert [a["class"] for a in out] == ["steel:D1", "steel:D2"]


@pytest.mark.parametrize("raw,expected", [
    ("addObjectProperty", "add_object_property"),
    ("add_objectproperty", "add_objectproperty"),
    ("addDisjointClasses", "add_disjoint_classes"),
    ("add_class", "add_class"),
])
def test_camel_case_action_names_fold(raw, expected):
    assert canonical_action_name(raw) == expected


def test_non_dict_entries_are_rejected_not_silently_dropped():
    """조용히 버리면 이번 결함이 되살아난다 — 사유와 함께 반환한다."""
    out, rejected = normalize_jury_actions(
        ["문자열", None, {"action": "remove_class", "class": "steel:A"}])
    assert len(out) == 1
    assert len(rejected) == 2
    assert all("reason" in r for r in rejected)


# ── 3. 배선 (정규화가 실제 적용 경로에 연결돼 있는가) ──────────────────

_MIN_TTL = (
    "@prefix steel: <http://example.com/steel-ontology#> .\n"
    "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
    "steel:A a owl:Class .\nsteel:B a owl:Class .\nsteel:Duplicate a owl:Class .\n"
)


def test_apply_jury_fixes_accepts_the_real_jury_payload():
    """THE REGRESSION: 실제 Jury 표기가 적용된다 (예전엔 41건 중 37건 폐기).

    정규화 배선이 빠지면 이 테스트가 실패한다 — 헬퍼 단위 테스트만으로는
    배선 누락을 잡지 못하므로 별도로 고정한다.
    """
    from tools.jury_fixes import apply_jury_fixes

    res = apply_jury_fixes(_MIN_TTL, [
        {"action": "add_class", "uri": "steel:NewCls", "subClassOf": "steel:A"},
        {"action": "add_object_property", "uri": "steel:linksAtoB",
         "domain": "steel:A", "range": "steel:B", "dcterms:source": "COL"},
        {"action": "add_restriction", "class": "steel:A",
         "restriction_type": "someValuesFrom", "property": "steel:linksAtoB",
         "filler": "steel:B"},
        {"action": "remove_class", "uri": "steel:Duplicate"},
        {"action": "add_triple", "triples": [
            {"subject": "steel:B", "predicate": "rdfs:label", "object": "bee"}]},
    ])
    assert not res["failed"], res["failed"]
    assert len(res["applied"]) == 5, res


def test_apply_jury_fixes_records_csv_source_on_object_properties():
    """CSV 컬럼 출처는 A-Box 매칭의 확정 근거다 — 이 경로가 버리고 있었다."""
    from rdflib import Graph, URIRef

    from tools.jury_fixes import apply_jury_fixes

    res = apply_jury_fixes(_MIN_TTL, [{
        "action": "add_object_property", "uri": "steel:linksAtoB",
        "domain": "steel:A", "range": "steel:B", "dcterms:source": "EQUIP_ID",
    }])
    g = Graph()
    g.parse(data=res["ttl"], format="turtle")
    sources = list(g.objects(
        URIRef("http://example.com/steel-ontology#linksAtoB"),
        URIRef("http://purl.org/dc/terms/source")))
    assert [str(s) for s in sources] == ["EQUIP_ID"], sources


def test_duplicate_object_property_is_refused_not_added():
    """이미 같은 (domain, range) 를 잇는 OP 가 있으면 동의어를 만들지 않는다.

    LLM 은 T-Box 의 일부만 보고 판단해 이미 있는 관계를 다시 요청한다 (실측:
    요청 15쌍 중 14쌍이 기존). 그대로 추가하면 중복 OP 게이트가 악화되고,
    CSV FK 컬럼은 하나뿐이라 나머지 OP 는 값 0건으로 남아 "빈 관계로 질의하면
    0건이 정답처럼" 반환된다.
    """
    from rdflib import OWL, RDF, Graph, URIRef

    from tools.jury_fixes import apply_jury_fixes

    base = _MIN_TTL + (
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
        "steel:existing a owl:ObjectProperty ; "
        "rdfs:domain steel:A ; rdfs:range steel:B .\n"
    )
    res = apply_jury_fixes(base, [{
        "action": "add_object_property", "uri": "steel:synonym",
        "domain": "steel:A", "range": "steel:B",
    }])
    g = Graph()
    g.parse(data=res["ttl"], format="turtle")
    assert (URIRef("http://example.com/steel-ontology#synonym"),
            RDF.type, OWL.ObjectProperty) not in g, "동의어 OP 가 추가됐다"
    assert len(res["noop"]) == 1, res


def test_new_object_property_is_still_added():
    """PRESERVATION: 정말 없는 관계는 여전히 추가된다 (가드가 과도하지 않다)."""
    from rdflib import OWL, RDF, Graph, URIRef

    from tools.jury_fixes import apply_jury_fixes

    res = apply_jury_fixes(_MIN_TTL, [{
        "action": "add_object_property", "uri": "steel:brandNew",
        "domain": "steel:A", "range": "steel:B",
    }])
    g = Graph()
    g.parse(data=res["ttl"], format="turtle")
    assert (URIRef("http://example.com/steel-ontology#brandNew"),
            RDF.type, OWL.ObjectProperty) in g
    assert len(res["applied"]) == 1


def test_reviewers_get_a_full_declaration_inventory():
    """리뷰어는 TTL 의 앞부분만 본다 — 인벤토리가 절단선 밖 선언을 전수 담아야 한다.

    2026-08-10 실측: Jury 가 veto lock 으로 지목한 항목들의 첫 등장 위치는
    50,000자 절단선 밖이었다 (``gasEnergyIsFromEquipment`` @123,924,
    ``owl:AllDisjointClasses`` @146,580). Jury 의 "TTL 에서 확인되지 않음" 은 자기가
    받은 33% 에 대해 참이었다 — 없는 것을 없다고 한 게 아니라 **못 본** 것이다.
    """
    import tools.multi_agent_tbox as mt

    # 절단선(50,000자) 을 넘기도록 채운 T-Box. 뒤쪽에 관계를 둔다.
    filler = "".join(
        f"steel:pad{i} a owl:DatatypeProperty ; rdfs:domain steel:A ;\n"
        f"    rdfs:comment \"{'x' * 200}\"@ko .\n"
        for i in range(200)
    )
    ttl = (
        "@prefix steel: <http://example.com/steel-ontology#> .\n"
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
        "steel:A a owl:Class .\nsteel:B a owl:Class .\n"
        + filler
        + "steel:lateLink a owl:ObjectProperty ; "
          "rdfs:domain steel:A ; rdfs:range steel:B .\n"
    )
    assert len(ttl) > mt._TTL_PROMPT_MAX, "픽스처가 절단선을 넘지 않는다"
    assert "lateLink" not in ttl[:mt._TTL_PROMPT_MAX], "픽스처의 관계가 절단선 안에 있다"

    inventory = mt._format_declaration_inventory(ttl)
    assert "lateLink" in inventory, "절단선 밖 OP 가 인벤토리에 없다"
    assert "A→B" in inventory, "domain→range 관계가 인벤토리에 없다"


def test_inventory_is_wired_into_the_jury_prompt():
    """인벤토리 헬퍼가 **실제 프롬프트 경로에 연결**돼 있어야 한다.

    헬퍼만 있고 호출되지 않으면 단위 테스트는 통과하면서 리뷰어는 여전히 33%만
    본다 — 배선 누락을 따로 막는다.
    """
    import inspect

    import tools.multi_agent_tbox as mt

    for fn in (mt._jury_decide, mt._validator_review, mt._sme_review):
        src = inspect.getsource(fn)
        assert "_format_declaration_inventory" in src, (
            f"{fn.__name__} 이 선언 인벤토리를 프롬프트에 넣지 않는다"
        )


def test_dispatch_aliases_are_all_reachable():
    """dispatch 에 등록된 action 은 게이트에 막히지 않아야 한다.

    예전엔 게이트가 손으로 관리하는 50개 집합을 봐서 dispatch 62개 중 12개가
    영구히 도달 불가였다 — 그중 FK-OP alias 는 S9 회귀를 고치려 추가된 것인데
    한 번도 실행되지 않았다.
    """
    from tools.jury_fixes import _DISPATCH, apply_jury_fixes

    unreachable = []
    for name in _DISPATCH:
        res = apply_jury_fixes(_MIN_TTL, [{"action": name}])
        if res["skipped"] and "미지원" in str(res["skipped"][0]["reason"]):
            unreachable.append(name)
    assert not unreachable, f"dispatch 에 있으나 도달 불가: {unreachable}"


# ── 4. 리터럴 vs IRI (삭제/교체 매칭) ────────────────────────────────

_LIT_TTL = (
    "@prefix steel: <http://example.com/steel-ontology#> .\n"
    "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
    "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
    "@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .\n"
    "@prefix dcterms: <http://purl.org/dc/terms/> .\n"
    "steel:dp a owl:DatatypeProperty ;\n"
    '    owl:deprecated "true"^^xsd:string ;\n'
    '    dcterms:source "Iron_Output_ton" ;\n'
    '    rdfs:label "unwanted"@ko .\n'
    "steel:A a owl:Class .\nsteel:B a owl:Class .\n"
)


def test_modify_triple_can_replace_a_typed_literal():
    """THE REGRESSION: ``owl:deprecated`` 타입 교정이 적용된다.

    old/new object 를 **무조건 IRI** 로 해석했기 때문에 리터럴 교체가 아예
    불가능했다. 실측 (2026-08-10): Jury 가 ``"true"^^xsd:string`` →
    ``xsd:boolean`` 을 요청했으나 양쪽이 유령 IRI 가 되어 "교체할 트리플 없음" 으로
    끝났고, deprecated 선언이 OWL 2 사양과 다른 타입으로 남아 **추론기가 인식하지
    못한다**.
    """
    from rdflib import OWL, XSD, Graph, URIRef

    from tools.jury_fixes import apply_jury_fixes

    res = apply_jury_fixes(_LIT_TTL, [{
        "action": "modify_triple", "changes": [{
            "subject": "steel:dp", "predicate": "owl:deprecated",
            "old_object": '"true"^^xsd:string',
            "new_object": '"true"^^xsd:boolean',
        }],
    }])
    assert not res["failed"], res["failed"]
    g = Graph()
    g.parse(data=res["ttl"], format="turtle")
    values = list(g.objects(URIRef("http://example.com/steel-ontology#dp"),
                            OWL.deprecated))
    assert len(values) == 1, values
    assert values[0].datatype == XSD.boolean, values[0].datatype


def test_remove_triple_absorbs_literal_notation_differences():
    """평문으로 보낸 값이 ``@ko`` 리터럴과 매칭된다.

    LLM 은 언어 태그·데이터타입을 빠뜨리고 값만 보낸다. 정확 일치를 먼저 시도하고,
    없을 때만 같은 문자열 값으로 넓힌다.
    """
    from rdflib import RDFS, Graph, URIRef

    from tools.jury_fixes import apply_jury_fixes

    res = apply_jury_fixes(_LIT_TTL, [{
        "action": "remove_triple", "subject": "steel:dp",
        "predicate": "rdfs:label", "object": "unwanted",
    }])
    assert not res["failed"], res["failed"]
    g = Graph()
    g.parse(data=res["ttl"], format="turtle")
    assert not list(g.objects(URIRef("http://example.com/steel-ontology#dp"),
                             RDFS.label))


def test_ambiguous_literal_match_is_refused():
    """같은 값이 여러 표기로 있으면 **추측하지 않는다**.

    조용히 엉뚱한 표기를 지우는 것보다 실패가 낫다.
    """
    from rdflib import RDFS, Graph, URIRef

    from tools.jury_fixes import apply_jury_fixes

    ambiguous = _LIT_TTL.replace(
        '    rdfs:label "unwanted"@ko .',
        '    rdfs:label "unwanted"@ko, "unwanted"@en .',
    )
    res = apply_jury_fixes(ambiguous, [{
        "action": "remove_triple", "subject": "steel:dp",
        "predicate": "rdfs:label", "object": "unwanted",
    }])
    g = Graph()
    g.parse(data=res["ttl"], format="turtle")
    labels = list(g.objects(URIRef("http://example.com/steel-ontology#dp"),
                            RDFS.label))
    assert len(labels) == 2, f"모호한데 지웠다: {labels}"


def test_iri_objects_still_resolve_in_delete():
    """PRESERVATION: IRI object 삭제는 그대로 동작한다 (파서가 과도하지 않다)."""
    from rdflib import RDFS, Graph, URIRef

    from tools.jury_fixes import apply_jury_fixes

    base = _LIT_TTL + "steel:A rdfs:subClassOf steel:B .\n"
    res = apply_jury_fixes(base, [{
        "action": "remove_triple", "subject": "steel:A",
        "predicate": "rdfs:subClassOf", "object": "steel:B",
    }])
    assert not res["failed"], res["failed"]
    g = Graph()
    g.parse(data=res["ttl"], format="turtle")
    assert not list(g.objects(URIRef("http://example.com/steel-ontology#A"),
                             RDFS.subClassOf))


def test_add_triple_writes_a_language_tagged_literal():
    """``"라벨"@ko`` 를 IRI 로 오해하지 않고 언어 태그 리터럴로 기록한다."""
    from rdflib import RDFS, Graph, URIRef

    from tools.jury_fixes import apply_jury_fixes

    res = apply_jury_fixes(_LIT_TTL, [{
        "action": "add_triple", "subject": "steel:A",
        "predicate": "rdfs:label", "object": '"설비"@ko',
    }])
    assert not res["failed"], res["failed"]
    g = Graph()
    g.parse(data=res["ttl"], format="turtle")
    labels = list(g.objects(URIRef("http://example.com/steel-ontology#A"),
                            RDFS.label))
    assert any(str(x) == "설비" and x.language == "ko" for x in labels), labels
