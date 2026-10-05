"""S2 T-Box 생성의 무결성 가드 회귀 테스트.

2026-08-09 실제 S2 실행(50분, 5라운드)에서 관측된 4개 결함을 고정한다. 네 건 모두
**조용히** 잘못된 산출물이나 잘못된 판정을 만들었다.

1. **DSL 적용 경로가 유령 IRI 를 만든다** (``_LocalNameNamespace``)
   LLM 이 DSL 값에 ``steel:Foo`` 를 넣어 보내면 raw ``Namespace[...]`` 가
   ``http://…#steel:Foo`` 라는 **별개 리소스** 를 만든다. 실측: 183건 생성 →
   ObjectProperty 229개 중 101개 사용 불가. Jury/Validator 가 지시한 구조 수정이
   적용된 것처럼 보이지만 실제로는 무효였다.

2. **저장 전 검증이 없다** (``_guard_before_save``)
   S2 는 자기가 만든 TTL 을 검증하지 않고 저장했다. 유령 IRI 183건이 그대로 파일에
   들어갔고, 응답 유실로 TTL 이 비어도 기존 T-Box 를 덮어썼다.

3. **Jury 예외가 합의를 선언한다** (fail open → fail closed)
   Bedrock throttling/timeout 이 **만장일치 승인** 으로 번역돼 아무도 검토하지 않은
   T-Box 가 "합의됨" 으로 저장됐다.

4. **Jury 파싱 실패가 절충 경로를 건너뛴다**
   ``_parse_review_json`` 이 예외 대신 센티넬 dict 를 돌려주므로, ``except`` 절에
   있던 compromise 로직이 실행되지 않았다. 마지막 라운드가 **아무 결정도 내리지
   못하고** 끝났다 (실측: jury_final="파싱 실패", compromise=null).
"""
from __future__ import annotations

import unittest.mock as mock

import pytest
from rdflib import RDF, RDFS, Graph, URIRef
from rdflib.namespace import OWL

import tools.multi_agent_tbox as mt

STEEL = "http://example.com/steel-ontology#"
_MIN_TTL = (
    "@prefix steel: <http://example.com/steel-ontology#> .\n"
    "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
    "steel:Equipment a owl:Class .\nsteel:Plant a owl:Class .\n"
)


# ── 1. 유령 IRI 생성 차단 ─────────────────────────────────────────────


@pytest.mark.parametrize("given", [
    "Equipment",                                    # 정상 local name
    "steel:Equipment",                              # 배포 prefix (문제의 형태)
    "http://example.com/steel-ontology#Equipment",  # 완전 IRI
    "unbound:Equipment",                            # 미등록 prefix → 도메인 폴백
])
def test_domain_names_resolve_to_the_domain_namespace(given):
    """THE REGRESSION: 도메인 표기는 어떤 형태든 정상 도메인 IRI 로 수렴한다."""
    g = Graph()
    g.parse(data=_MIN_TTL, format="turtle")
    ns = mt._GraphAwareNamespace(STEEL, g)
    assert str(ns[given]) == STEEL + "Equipment", (
        f"{given!r} 가 유령 IRI 로 변환됐다 — 정상 IRI 와 별개 리소스가 된다"
    )


def test_external_prefix_resolves_to_its_own_namespace():
    """IOF 등 외부 prefix 는 **자기 네임스페이스** 로 해석돼야 한다.

    단순히 prefix 를 벗기면 ``iof-core:MaterialArtifact`` 가 도메인 클래스가 되어
    IOF 매핑이 깨진다 — 유령 IRI 를 고치면서 새 결함을 만드는 함정이다.
    """
    iof = "https://spec.industrialontologies.org/ontology/core/Core/"
    g = Graph()
    g.parse(data=_MIN_TTL + f"@prefix iof-core: <{iof}> .\n", format="turtle")
    ns = mt._GraphAwareNamespace(STEEL, g)
    assert str(ns["iof-core:MaterialArtifact"]) == iof + "MaterialArtifact"


def test_dsl_applier_creates_no_phantom_iris():
    """DSL 지시가 prefixed name 을 담고 있어도 유령 IRI 가 생기지 않는다."""
    instructions = [{
        "action": "add_object_property",
        "name": "steel:locatedIn",
        "domain": "steel:Equipment",
        "range": "steel:Plant",
        "label_ko": "위치",
        "inverse": "steel:hasEquipment",
    }]
    out = mt._apply_high_level_instructions(_MIN_TTL, instructions)
    ttl = out[0] if isinstance(out, tuple) else out

    g = Graph()
    g.parse(data=ttl, format="turtle")
    phantom = [
        str(x) for x in set(g.subjects()) | set(g.objects())
        if isinstance(x, URIRef) and str(x).startswith(STEEL)
        and "steel:" in str(x)[len(STEEL):]
    ]
    assert not phantom, f"유령 IRI 생성: {phantom[:3]}"

    # 그리고 실제로 쓸 수 있는 OP 여야 한다 (domain/range 가 정상 클래스를 가리킴)
    op = URIRef(STEEL + "locatedIn")
    assert (op, RDF.type, OWL.ObjectProperty) in g
    assert (op, RDFS.domain, URIRef(STEEL + "Equipment")) in g
    assert (op, RDFS.range, URIRef(STEEL + "Plant")) in g
    inv = URIRef(STEEL + "hasEquipment")
    assert (inv, RDFS.domain, URIRef(STEEL + "Plant")) in g


def test_dsl_applier_preserves_iof_mapping():
    """IOF subClassOf 지시가 **IOF 네임스페이스** 로 적용돼야 한다.

    프롬프트가 IOF 매핑을 요구하는데, prefix 를 도메인 NS 에 흡수하면 상위 클래스가
    존재하지 않는 도메인 클래스가 되어 매핑이 사라진다.
    """
    iof = "https://spec.industrialontologies.org/ontology/core/Core/"
    base = _MIN_TTL + f"@prefix iof-core: <{iof}> .\n"
    out = mt._apply_high_level_instructions(
        base, [{"action": "add_subclass", "child": "Equipment",
                "parent": "iof-core:MaterialArtifact"}],
    )
    ttl = out[0] if isinstance(out, tuple) else out
    g = Graph()
    g.parse(data=ttl, format="turtle")
    parents = {str(p) for p in g.objects(URIRef(STEEL + "Equipment"), RDFS.subClassOf)}
    assert iof + "MaterialArtifact" in parents, f"IOF 매핑 소실: {parents}"


# ── 1a. add_triple/remove_triple 이 지시를 버리지 않는다 ─────────────


@pytest.mark.parametrize("predicate", [
    "rdfs:subClassOf",   # 실측 44건 — 구조 수정의 대부분
    "rdfs:domain",
    "rdfs:range",
    "rdf:type",
    "rdfs:label",
    "dcterms:source",    # DP 의 CSV 컬럼 출처 (Step 12e 게이트가 요구)
])
def test_add_triple_accepts_standard_predicates(predicate):
    """THE REGRESSION: 도메인 prefix 가 아닌 predicate 도 IRI 로 해석된다.

    ``_to_node`` 는 도메인 prefix 만 IRI 로 보고 나머지를 ``Literal`` 로 격하시켰다.
    rdflib 은 predicate 가 Literal 인 트리플을 거부하므로 지시가 **조용히 버려졌다**
    — S2 재실행 실측 101건 (subClassOf 44 / domain·range 26 / dcterms:source 15).
    """
    out = mt._apply_high_level_instructions(
        _MIN_TTL,
        [{"action": "add_triple", "subject": "steel:Equipment",
          "predicate": predicate, "object": "steel:Plant"}],
    )
    ttl = out[0] if isinstance(out, tuple) else out
    g = Graph()
    g.parse(data=ttl, format="turtle")
    used = {str(p) for p in g.predicates()}
    local = predicate.split(":", 1)[1]
    assert any(p.endswith(local) and not p.startswith(STEEL) for p in used), (
        f"{predicate} 가 적용되지 않았다 (Literal 로 격하돼 버려졌을 가능성)"
    )


def test_add_triple_keeps_language_tagged_literal():
    """``"텍스트@ko"`` 는 언어 태그 Literal 로 해석된다.

    LLM 이 실제로 이 형식을 보낸다 (실측: ``'연속주조 공정이 사용하는 설비@ko'``).
    """
    out = mt._apply_high_level_instructions(
        _MIN_TTL,
        [{"action": "add_triple", "subject": "steel:Equipment",
          "predicate": "rdfs:label", "object": "설비 이름@ko"}],
    )
    ttl = out[0] if isinstance(out, tuple) else out
    g = Graph()
    g.parse(data=ttl, format="turtle")
    labels = list(g.objects(URIRef(STEEL + "Equipment"), RDFS.label))
    assert any(str(x) == "설비 이름" and getattr(x, "language", None) == "ko"
               for x in labels), f"언어 태그가 해석되지 않았다: {labels}"


def test_add_triple_keeps_plain_text_as_literal():
    """PRESERVATION: 자연어 값은 IRI 로 승격되지 않는다.

    콜론이 들어간 문장(``"설비: 고로"``)을 prefixed name 으로 오인하면 IRI 가 되어
    리터럴 데이터가 사라진다.
    """
    for text in ("Plain description", "설비: 고로"):
        out = mt._apply_high_level_instructions(
            _MIN_TTL,
            [{"action": "add_triple", "subject": "steel:Equipment",
              "predicate": "rdfs:comment", "object": text}],
        )
        ttl = out[0] if isinstance(out, tuple) else out
        g = Graph()
        g.parse(data=ttl, format="turtle")
        comments = [str(x) for x in g.objects(URIRef(STEEL + "Equipment"), RDFS.comment)]
        assert text in comments, f"{text!r} 가 리터럴로 보존되지 않았다: {comments}"


def test_add_triple_resolves_foreign_object_to_its_namespace():
    """object 자리의 외래 prefix 도 자기 네임스페이스로 간다."""
    iof = "https://spec.industrialontologies.org/ontology/core/Core/"
    out = mt._apply_high_level_instructions(
        _MIN_TTL + f"@prefix iof-core: <{iof}> .\n",
        [{"action": "add_triple", "subject": "steel:Equipment",
          "predicate": "rdfs:subClassOf", "object": "iof-core:MaterialArtifact"}],
    )
    ttl = out[0] if isinstance(out, tuple) else out
    g = Graph()
    g.parse(data=ttl, format="turtle")
    parents = {str(p) for p in g.objects(URIRef(STEEL + "Equipment"), RDFS.subClassOf)}
    assert iof + "MaterialArtifact" in parents, parents


# ── 1a-2. dcterms:source 값은 CSV 헤더와 비교 가능해야 한다 ──────────

_DCTERMS_SOURCE = URIRef("http://purl.org/dc/terms/source")


@pytest.mark.parametrize("sent,expected", [
    ('"Event_ID"^^xsd:string', "Event_ID"),   # 실측 34건의 형태
    ('"Message"', "Message"),
    ("Exit_Thickness_mm", "Exit_Thickness_mm"),
    ('"Cooling_Water_Flow_m3h"^^xsd:string', "Cooling_Water_Flow_m3h"),
])
def test_dsl_source_value_is_a_bare_column_name(sent, expected):
    """THE REGRESSION: LLM 이 Turtle 표기를 담아 보내도 컬럼명만 기록된다.

    A-Box 는 이 값을 ``str(src).strip().upper()`` 로 **CSV 헤더와 직접 비교** 한다.
    ``'"EVENT_ID"^^XSD:STRING'`` 은 어떤 헤더와도 일치하지 않으므로 그 컬럼이
    조용히 transliteration 폴백으로 떨어진다 — dcterms:source 를 쓰는 이유(이름
    추측 제거)가 무력화된다. 배포 T-Box 실측 34건. Step 12e 게이트는 값의 **존재**
    만 세므로 이것을 잡지 못했다.
    """
    out = mt._apply_high_level_instructions(
        _MIN_TTL,
        [{"action": "add_datatype_property", "name": "steel:dp",
          "domain": "steel:Equipment", "range": "xsd:decimal", "source": sent}],
    )
    ttl = out[0] if isinstance(out, tuple) else out
    g = Graph()
    g.parse(data=ttl, format="turtle")
    values = [str(o) for o in g.objects(URIRef(STEEL + "dp"), _DCTERMS_SOURCE)]
    assert values == [expected], f"{sent!r} → {values}"


def test_add_triple_cleans_source_values_too():
    """``add_triple`` 로 직접 쓰는 경로도 같은 정리를 받는다."""
    out = mt._apply_high_level_instructions(
        _MIN_TTL + "steel:dp a owl:DatatypeProperty .\n",
        [{"action": "add_triple", "subject": "steel:dp",
          "predicate": "dcterms:source", "object": '"Alarm_Type"^^xsd:string'}],
    )
    ttl = out[0] if isinstance(out, tuple) else out
    g = Graph()
    g.parse(data=ttl, format="turtle")
    values = [str(o) for o in g.objects(URIRef(STEEL + "dp"), _DCTERMS_SOURCE)]
    assert values == ["Alarm_Type"], values


def test_datatype_prefix_in_object_position_stays_a_literal():
    """``xsd:string`` 을 object 로 보내면 IRI 로 승격하지 않는다.

    실측: LLM 이 ``dcterms:source`` 값으로 ``"xsd:string"`` 을 보냈고, prefixed
    name 으로 오인해 IRI 로 승격한 결과 ``steel:string`` 이 컬럼명 자리에 들어갔다
    (배포 T-Box 27건). xsd: 는 데이터타입 네임스페이스라 object 자리에서는 값이다.
    """
    out = mt._apply_high_level_instructions(
        _MIN_TTL + "steel:dp a owl:DatatypeProperty .\n",
        [{"action": "add_triple", "subject": "steel:dp",
          "predicate": "rdfs:comment", "object": "xsd:string"}],
    )
    ttl = out[0] if isinstance(out, tuple) else out
    g = Graph()
    g.parse(data=ttl, format="turtle")
    comments = list(g.objects(URIRef(STEEL + "dp"), RDFS.comment))
    assert comments and all(not isinstance(c, URIRef) for c in comments), comments


def test_known_prefix_in_object_position_still_resolves():
    """PRESERVATION: 알려진 prefix 는 여전히 IRI 로 해석된다 (가드가 과도하지 않다)."""
    iof = "https://spec.industrialontologies.org/ontology/core/Core/"
    out = mt._apply_high_level_instructions(
        _MIN_TTL + f"@prefix iof-core: <{iof}> .\n",
        [{"action": "add_triple", "subject": "steel:Equipment",
          "predicate": "rdfs:subClassOf", "object": "iof-core:MaterialArtifact"}],
    )
    ttl = out[0] if isinstance(out, tuple) else out
    g = Graph()
    g.parse(data=ttl, format="turtle")
    parents = {str(p) for p in g.objects(URIRef(STEEL + "Equipment"), RDFS.subClassOf)}
    assert iof + "MaterialArtifact" in parents, parents


# ── 1b. DSL 라벨이 prefix 를 새지 않는다 ─────────────────────────────


def test_dsl_labels_carry_no_prefix():
    """THE REGRESSION: @en 라벨에 prefix 가 새어나오지 않는다.

    ``Literal(name, lang="en")`` 이 **원본 DSL 문자열** 을 그대로 썼기 때문에
    ``rdfs:label "steel:hasFoo"@en`` 이 만들어졌다. 배포 T-Box 실측 117건, 그
    오염이 시맨틱 딕셔너리 ``label_en`` 107건까지 전파됐다 (딕셔너리는 LLM
    NL→SPARQL 레퍼런스다).
    """
    out = mt._apply_high_level_instructions(
        _MIN_TTL,
        [{"action": "add_object_property", "name": "steel:locatedIn",
          "domain": "steel:Equipment", "range": "steel:Plant",
          "inverse": "steel:hasEquipment"},
         {"action": "add_datatype_property", "name": "steel:equipmentTemp",
          "domain": "steel:Equipment", "range": "xsd:decimal",
          "source": "TEMP"}],
    )
    ttl = out[0] if isinstance(out, tuple) else out
    g = Graph()
    g.parse(data=ttl, format="turtle")
    leaked = [
        (str(s), str(o)) for s, _, o in g.triples((None, RDFS.label, None))
        if ":" in str(o)
    ]
    assert not leaked, f"라벨에 prefix 가 새어나왔다: {leaked}"
    # rdfs:comment 도 같은 원본 문자열을 받던 경로였다.
    assert not [
        str(o) for _, _, o in g.triples((None, RDFS.comment, None))
        if ":" in str(o)
    ]


def test_dsl_label_is_identical_for_bare_and_prefixed_name():
    """같은 엔티티를 bare / prefixed 로 지시하면 **같은** 라벨이 나온다.

    예전에는 ``'hasFoo'`` 와 ``'steel:hasFoo'`` 가 서로 다른 라벨을 만들어, 같은
    프로퍼티의 라벨이 지시 표기에 따라 달라졌다.
    """
    def label_of(name: str) -> set[str]:
        out = mt._apply_high_level_instructions(
            _MIN_TTL,
            [{"action": "add_object_property", "name": name,
              "domain": "Equipment", "range": "Plant"}],
        )
        ttl = out[0] if isinstance(out, tuple) else out
        g = Graph()
        g.parse(data=ttl, format="turtle")
        return {
            str(o) for o in g.objects(URIRef(STEEL + "locatedIn"), RDFS.label)
            if getattr(o, "language", None) == "en"
        }

    assert label_of("locatedIn") == label_of("steel:locatedIn") == {"located in"}


def test_dsl_labels_do_not_overwrite_curated_ones():
    """이미 있는 @en 라벨은 기계 합성값으로 덮어쓰지 않는다.

    사람이 다듬은 라벨(``'waste quantity'``)이 라운드 반복마다
    ``'waste management quantity'`` 로 회귀하는 것을 막는다.
    """
    base = _MIN_TTL + (
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
        "steel:wasteManagementQuantity a owl:DatatypeProperty ;\n"
        '    rdfs:label "waste quantity"@en .\n'
    )
    out = mt._apply_high_level_instructions(
        base,
        [{"action": "add_datatype_property",
          "name": "steel:wasteManagementQuantity",
          "domain": "steel:Equipment", "range": "xsd:decimal",
          "source": "QTY"}],
    )
    ttl = out[0] if isinstance(out, tuple) else out
    g = Graph()
    g.parse(data=ttl, format="turtle")
    en = {
        str(o)
        for o in g.objects(URIRef(STEEL + "wasteManagementQuantity"), RDFS.label)
        if getattr(o, "language", None) == "en"
    }
    assert en == {"waste quantity"}, f"큐레이션 라벨이 회귀했다: {en}"


# ── 2. 저장 전 가드 ──────────────────────────────────────────────────


def test_save_guard_repairs_phantom_iris(tmp_path, monkeypatch):
    """남은/외부 유입 유령 IRI 는 저장 직전에 복구된다.

    ``TBOX_PATH`` monkeypatch 가 **필수** 다. 없으면 작은 픽스처가 붕괴 분기를
    타서 (배포 T-Box 5,637 트리플 대비 2 트리플) 가드가 **기존 파일 내용을**
    반환하고, 두 단정이 유령 복구와 무관하게 무조건 통과한다 — 실제로 그랬고,
    복구 로직을 통째로 no-op 으로 만들어도 이 테스트는 초록이었다.
    """
    empty = tmp_path / "t_box.ttl"
    empty.write_text("", encoding="utf-8")   # 트리플 0 → 붕괴 판정 비활성
    monkeypatch.setattr(mt, "TBOX_PATH", str(empty))

    ttl = (
        "@prefix steel: <http://example.com/steel-ontology#> .\n"
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "<http://example.com/steel-ontology#steel:Foo> a owl:Class .\n"
        "steel:Bar a owl:Class .\n"
    )
    out, guard = mt._guard_before_save(ttl)
    assert guard["phantom_iris_repaired"] >= 1
    assert guard["phantom_iris_remaining"] == 0
    assert "#steel:" not in out
    g = Graph()
    g.parse(data=out, format="turtle")
    assert URIRef(STEEL + "Foo") in set(g.subjects(RDF.type, OWL.Class)), (
        "유령을 지우기만 하고 정상 IRI 로 복구하지 않았다"
    )


def test_save_guard_catches_foreign_prefix_phantom(tmp_path, monkeypatch):
    """THE REGRESSION: 외래 prefix 유령도 탐지·복구된다.

    유령 판정이 ``f"{NS_PREFIX}:"`` 부분문자열 검사였을 때
    ``<…#iof-core:MaterialArtifact>`` 는 도메인 prefix 를 포함하지 않아 **탐지
    0건** 이었다. 그래서 IOF 매핑 유령이 그대로 저장됐고, S3 가 그것을 미선언
    도메인 클래스로 뭉갰다 (배포 T-Box 실측: ``steel:MaterialArtifact`` 13회 참조,
    선언 0건).
    """
    empty = tmp_path / "t_box.ttl"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setattr(mt, "TBOX_PATH", str(empty))

    ttl = (
        "@prefix steel: <http://example.com/steel-ontology#> .\n"
        "@prefix iof-core: "
        "<https://spec.industrialontologies.org/ontology/core/Core/> .\n"
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
        "steel:Slab a owl:Class ; rdfs:subClassOf "
        "<http://example.com/steel-ontology#iof-core:MaterialArtifact> .\n"
    )
    out, guard = mt._guard_before_save(ttl)

    assert guard["phantom_iris_repaired"] >= 1
    assert "#iof-core:" not in out, "외래 유령이 그대로 저장됐다"
    g = Graph()
    g.parse(data=out, format="turtle")
    parents = set(g.objects(URIRef(STEEL + "Slab"), RDFS.subClassOf))
    assert URIRef(
        "https://spec.industrialontologies.org/ontology/core/Core/"
        "MaterialArtifact"
    ) in parents, "IOF 부모로 복구되지 않았다 (도메인 클래스로 뭉갰을 가능성)"


def test_save_guard_counter_reflects_repair_not_detection(tmp_path, monkeypatch):
    """복구가 실패하면 '복구됨' 으로 보고하지 않는다.

    카운터가 탐지 수(``len(phantom)``) 로 세팅돼 있어, 복구가 0건이어도 운영자
    응답에는 "N건 복구" 가 찍혔다 — 불완전한 복구를 정상으로 읽게 만드는 경로다.
    """
    empty = tmp_path / "t_box.ttl"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setattr(mt, "TBOX_PATH", str(empty))
    monkeypatch.setattr(
        "tools.jury_fixes._apply_fix_namespace_bulk",
        lambda g, action: (False, "SABOTAGED"),
    )

    ttl = (
        "@prefix steel: <http://example.com/steel-ontology#> .\n"
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "<http://example.com/steel-ontology#steel:Foo> a owl:Class .\n"
    )
    _out, guard = mt._guard_before_save(ttl)
    assert guard["phantom_iris_repaired"] == 0
    assert guard["phantom_iris_remaining"] >= 1


def test_save_guard_blocks_collapse(tmp_path, monkeypatch):
    """트리플이 절반 미만으로 붕괴하면 기존 T-Box 를 보존한다.

    응답 유실로 TTL 이 사실상 비어도 그대로 덮어쓰던 경로를 막는다 — 50분치 작업물
    보다 기존 산출물이 낫다.
    """
    healthy = tmp_path / "t_box.ttl"
    healthy.write_text(
        "@prefix steel: <http://example.com/steel-ontology#> .\n"
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        + "".join(f"steel:C{i} a owl:Class .\n" for i in range(100)),
        encoding="utf-8",
    )
    monkeypatch.setattr(mt, "TBOX_PATH", str(healthy))
    original = healthy.read_text(encoding="utf-8")

    collapsed = _MIN_TTL   # 2 트리플 — 100 대비 붕괴
    out, guard = mt._guard_before_save(collapsed)

    assert guard["collapse_blocked"] is True
    assert out == original, "붕괴한 TTL 로 기존 T-Box 를 덮어썼다"


def test_save_guard_passes_normal_revision(tmp_path, monkeypatch):
    """POSITIVE: 정상적인 라운드 수정은 막지 않는다 (기능 보존)."""
    healthy = tmp_path / "t_box.ttl"
    body = "".join(f"steel:C{i} a owl:Class .\n" for i in range(100))
    header = ("@prefix steel: <http://example.com/steel-ontology#> .\n"
              "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n")
    healthy.write_text(header + body, encoding="utf-8")
    monkeypatch.setattr(mt, "TBOX_PATH", str(healthy))

    # 클래스 10개 줄어든 정상 수정 (90/100 = 90% > 50%)
    revised = header + "".join(f"steel:C{i} a owl:Class .\n" for i in range(90))
    out, guard = mt._guard_before_save(revised)
    assert guard["collapse_blocked"] is False
    assert "C89" in out


def test_save_guard_absorbs_unparseable_ttl():
    """파싱 불가 TTL 이어도 저장 경로를 막지 않는다 (기존 동작 유지)."""
    out, guard = mt._guard_before_save("{{{ not turtle")
    assert "parse_error" in guard
    assert out == "{{{ not turtle"


def test_save_guard_is_wired_into_the_save_path():
    """가드가 **실제 저장 경로에 연결**돼 있어야 한다 (소스 레벨 고정).

    헬퍼만 존재하고 호출되지 않으면 동작 테스트는 통과하면서 파일에는 검증 없이
    저장된다 — 배선이 사라지는 것을 별도로 막는다.
    """
    import inspect

    source = inspect.getsource(mt._finalize_and_save)
    assert "_guard_before_save" in source, (
        "_finalize_and_save 가 저장 전 가드를 호출하지 않는다 — 검증 없이 덮어쓴다"
    )
    # 가드가 atomic_write 보다 앞에 있어야 의미가 있다.
    assert source.index("_guard_before_save") < source.index("atomic_write"), (
        "가드가 저장 이후에 실행된다"
    )


# ── guard 비교축 정규화 — 단계 불일치로 인한 오차단 방지 ──────────────────
#
# 실측 (2026-08-17, 48분 S2 실행): 새 초안이 클래스 66→47 (-30.3%) 로 보고돼
# ``declaration_loss_blocked`` 로 저장이 거부됐고, 48분 산출물이 0이 됐다.
# 그런데 손실로 지목된 20개 클래스를 현재 T-Box 에서 제거하고 S3 만 돌리면
# **17개가 결정적으로 복원된다** (실측). MasterData / EquipmentManagement /
# SupplyChainMaster 같은 중간 추상 계층은 S3 의 step_12/step_14 가
# design_patterns.json 으로 만드는 것이라 S2 초안에 없는 게 정상이다.
#
# 즉 guard 는 **S2 초안(S3 이전)** 을 **S3 완료본** 과 비교하고 있었다. 서로 다른
# 파이프라인 단계를 재므로 30.3% 는 측정 오차다. 코드 주석(:3600-3602)은 이 비교가
# "정확히 재는 위치가 됐다" 고 적었지만 실측은 반대였다.
#
# 교정 방향: 손실 판정 **직전에** 새 초안을 S3(improve_tbox) 에 통과시켜, 같은
# 단계끼리 비교한다. 저장하는 TTL 은 초안 원본을 유지한다(S3 는 S3 단계에서 다시
# 돈다). 이름 목록 하드코딩 대신 파이프라인을 실제로 돌리는 이유: design_patterns
# 설정만 보면 15/20 만 잡히고(실측), MaintenanceManagement 등은 다른 경로가
# 만든다 — 경로가 늘어날 때마다 목록이 낡는다.
#
# ``improve_tbox`` 는 순수 함수다 (파일에 쓰지 않음, 실측 확인: 호출 전후 md5 동일).
# 파일을 쓰는 ``improve_tbox_quality`` 를 부르면 차단 판정 중에 T-Box 가 오염된다.

_S3_ONLY_CLASSES = ("MasterData", "TransactionRecord", "EquipmentManagement")


def _stage_ttl(header: str, classes, extra: str = "") -> str:
    return header + "".join(f"steel:{c} a owl:Class .\n" for c in classes) + extra


def test_guard_normalizes_before_measuring_loss(tmp_path, monkeypatch):
    """THE REGRESSION: S3 가 복원하는 클래스는 손실로 세지 않는다.

    prev(S3 완료본)에는 S3 산물이 있고 초안에는 없다. 정규화 없이 재면 손실률이
    임계치를 넘어 차단되지만, 정규화하면 통과해야 한다.
    """
    header = ("@prefix steel: <http://example.com/steel-ontology#> .\n"
              "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n")
    base = [f"C{i}" for i in range(10)]

    prev = tmp_path / "t_box.ttl"
    prev.write_text(_stage_ttl(header, base + list(_S3_ONLY_CLASSES)), encoding="utf-8")
    monkeypatch.setattr(mt, "TBOX_PATH", str(prev))

    draft = _stage_ttl(header, base)          # S3 산물 3개가 없는 초안

    # S3 를 흉내내는 스텁 — 실제 파이프라인은 느리므로(실측 181초) 테스트에서는
    # "S3 는 이 클래스들을 결정적으로 되살린다" 는 계약만 고정한다.
    def _fake_improve(ttl):
        return _stage_ttl(header, base + list(_S3_ONLY_CLASSES)), {}

    monkeypatch.setattr(mt, "_normalize_with_s3", _fake_improve, raising=False)
    monkeypatch.setenv("S2_GUARD_NORMALIZE_WITH_S3", "on")

    out, guard = mt._guard_before_save(draft)

    assert guard.get("declaration_loss_blocked") is not True, (
        f"S3 가 복원하는 클래스를 손실로 세어 차단했다: {guard.get('capability_loss')}"
    )
    assert "C9" in out, "정규화 통과 후에도 초안이 저장되지 않았다"
    # 저장되는 것은 **초안 원본** 이어야 한다 (S3 는 S3 단계에서 다시 돈다).
    assert "MasterData" not in out, "정규화 산출물을 저장했다 — 초안 원본을 저장해야 한다"


def test_guard_still_blocks_real_loss_after_normalization(tmp_path, monkeypatch):
    """NEGATIVE: 정규화가 진짜 손실을 덮지 않는다.

    정규화는 "S3 가 되살리는 것" 만 면제해야 한다. S3 가 되살리지 않는 클래스가
    사라졌으면 여전히 차단해야 한다 — 이 방향을 주장하지 않으면 정규화가
    게이트를 무력화하는 변경이 된다.
    """
    header = ("@prefix steel: <http://example.com/steel-ontology#> .\n"
              "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n")
    prev_classes = [f"C{i}" for i in range(10)]
    prev = tmp_path / "t_box.ttl"
    prev.write_text(_stage_ttl(header, prev_classes), encoding="utf-8")
    monkeypatch.setattr(mt, "TBOX_PATH", str(prev))
    original = prev.read_text(encoding="utf-8")

    # 절반이 사라진 초안 — S3 도 되살리지 못한다 (스텁이 그대로 반환).
    draft = _stage_ttl(header, prev_classes[:5])
    monkeypatch.setattr(mt, "_normalize_with_s3", lambda ttl: (ttl, {}), raising=False)
    monkeypatch.setenv("S2_GUARD_NORMALIZE_WITH_S3", "on")

    out, guard = mt._guard_before_save(draft)

    assert guard["declaration_loss_blocked"] is True, "진짜 손실을 놓쳤다"
    assert out == original, "진짜 손실인데 초안으로 덮어썼다"


def test_guard_records_which_axis_it_compared(tmp_path, monkeypatch):
    """판정 근거가 응답에 남는다 — 무엇과 비교했는지 로그 없이 알 수 있어야 한다."""
    header = ("@prefix steel: <http://example.com/steel-ontology#> .\n"
              "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n")
    base = [f"C{i}" for i in range(10)]
    prev = tmp_path / "t_box.ttl"
    prev.write_text(_stage_ttl(header, base + ["MasterData"]), encoding="utf-8")
    monkeypatch.setattr(mt, "TBOX_PATH", str(prev))
    monkeypatch.setattr(
        mt, "_normalize_with_s3",
        lambda ttl: (_stage_ttl(header, base + ["MasterData"]), {}), raising=False,
    )
    monkeypatch.setenv("S2_GUARD_NORMALIZE_WITH_S3", "on")

    _out, guard = mt._guard_before_save(_stage_ttl(header, base))

    assert guard.get("compared_against") in ("s3_normalized", "raw_draft"), (
        f"비교 축이 응답에 없다: {sorted(guard)}"
    )
    assert guard["compared_against"] == "s3_normalized"


def test_guard_normalization_is_opt_outable(tmp_path, monkeypatch):
    """환경변수로 끌 수 있다 — 정규화가 문제를 일으키면 즉시 되돌릴 수 있어야 한다."""
    header = ("@prefix steel: <http://example.com/steel-ontology#> .\n"
              "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n")
    base = [f"C{i}" for i in range(10)]
    prev = tmp_path / "t_box.ttl"
    prev.write_text(_stage_ttl(header, base + list(_S3_ONLY_CLASSES)), encoding="utf-8")
    monkeypatch.setattr(mt, "TBOX_PATH", str(prev))
    called: list[int] = []

    def _spy(ttl):
        called.append(1)
        return ttl, {}

    monkeypatch.setattr(mt, "_normalize_with_s3", _spy, raising=False)
    monkeypatch.setenv("S2_GUARD_NORMALIZE_WITH_S3", "off")

    _out, guard = mt._guard_before_save(_stage_ttl(header, base))

    assert not called, "off 인데 S3 정규화를 호출했다"
    assert guard.get("compared_against") == "raw_draft"


def test_guard_survives_normalization_failure(tmp_path, monkeypatch):
    """정규화가 터져도 판정은 계속된다 — 폴백은 raw 비교(기존 동작)."""
    header = ("@prefix steel: <http://example.com/steel-ontology#> .\n"
              "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n")
    base = [f"C{i}" for i in range(10)]
    prev = tmp_path / "t_box.ttl"
    prev.write_text(_stage_ttl(header, base), encoding="utf-8")
    monkeypatch.setattr(mt, "TBOX_PATH", str(prev))

    def _boom(ttl):
        raise RuntimeError("S3 폭발")

    monkeypatch.setattr(mt, "_normalize_with_s3", _boom, raising=False)
    monkeypatch.setenv("S2_GUARD_NORMALIZE_WITH_S3", "on")

    out, guard = mt._guard_before_save(_stage_ttl(header, base))

    assert guard.get("compared_against") == "raw_draft"
    assert "C9" in out, "정규화 실패가 저장을 막았다"


def test_normalize_helper_does_not_touch_the_tbox_file(tmp_path, monkeypatch):
    """``_normalize_with_s3`` 는 파일을 쓰지 않아야 한다.

    파일을 쓰는 ``improve_tbox_quality``(ontology_quality.py:3627 atomic_write)를
    부르면 **차단 판정 중에 T-Box 가 오염된다** — 가드가 보호하려는 파일을
    가드가 망가뜨리는 최악의 형태다. 순수 함수 ``improve_tbox`` 를 써야 한다.
    """
    import ast
    import inspect
    import textwrap

    # docstring/주석은 제외하고 **실제 호출** 만 본다 — 주석에서 위험을 설명하는
    # 것은 장려해야 하므로 문자열 검색으로는 판정할 수 없다.
    tree = ast.parse(textwrap.dedent(inspect.getsource(mt._normalize_with_s3)))
    called = {
        n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", "")
        for n in ast.walk(tree) if isinstance(n, ast.Call)
    }
    imported = {
        alias.name for n in ast.walk(tree)
        if isinstance(n, ast.ImportFrom) for alias in n.names
    }
    assert "improve_tbox_quality" not in (called | imported), (
        "파일을 쓰는 improve_tbox_quality 를 호출한다 — 판정 중 T-Box 오염"
    )
    assert "improve_tbox" in (called | imported), "S3 파이프라인을 호출하지 않는다"


def test_guard_preserves_rejected_draft(tmp_path, monkeypatch):
    """차단 시 초안을 디스크에 남긴다 — 48분 산출물이 흔적 없이 사라지지 않게.

    실측: 두 차단 분기가 ``final_ttl`` 을 버리고 기존 파일 내용을 반환하므로
    새 초안은 함수 지역 변수째로 사라졌다. 응답 statistics 조차 기존 파일 값이라
    "새 초안이 클래스 몇 개였는지" 를 응답만 보고 알 수 없었다.
    """
    header = ("@prefix steel: <http://example.com/steel-ontology#> .\n"
              "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n")
    prev = tmp_path / "t_box.ttl"
    prev.write_text(_stage_ttl(header, [f"C{i}" for i in range(100)]), encoding="utf-8")
    monkeypatch.setattr(mt, "TBOX_PATH", str(prev))

    collapsed = _MIN_TTL
    _out, guard = mt._guard_before_save(collapsed)

    assert guard["collapse_blocked"] is True
    path = guard.get("rejected_draft_path")
    assert path, f"차단된 초안 경로가 응답에 없다: {sorted(guard)}"
    import os as _os
    assert _os.path.exists(path), f"차단된 초안이 저장되지 않았다: {path}"
    with open(path, encoding="utf-8") as fh:
        assert "steel:Equipment" in fh.read(), "저장된 내용이 초안이 아니다"


# ── 3. Jury 예외 = fail closed ───────────────────────────────────────


def _consensus_state() -> tuple[dict, dict, dict]:
    return ({
        "current_ttl": _MIN_TTL, "debate_log": [], "veto_persistent_targets": [],
        "veto_lock_triggered": False, "consensus_reached": False,
        "cqs": [], "csv_summary": "",
    }, {}, {})


def test_jury_exception_does_not_declare_consensus():
    """THE REGRESSION: throttling 예외가 만장일치 승인으로 번역되면 안 된다."""
    state, round_log, summary = _consensus_state()
    with mock.patch.object(mt, "_jury_decide",
                           side_effect=RuntimeError("ThrottlingException")):
        result = mt._handle_potential_consensus(
            state, {"approved": True}, {"approved": True}, {}, {}, 1,
            round_log, summary, lambda _p: None,
        )
    assert result is False, "Jury 실패가 합의로 처리됐다 (fail open 회귀)"
    assert state["consensus_reached"] is False
    assert round_log["consensus"] is False
    assert "jury_error" in round_log, "인프라 오류가 기록돼야 한다"
    assert summary["break_loop"] is False, "토론을 계속해야 한다"


# ── 4. Jury 파싱 실패 → 절충 경로 ────────────────────────────────────


def test_jury_parse_error_routes_to_compromise():
    """THE REGRESSION: 파싱 실패도 절충안을 만들어야 한다 (판정 없이 끝나지 않게)."""
    state, round_log, summary = _consensus_state()
    parse_failed = {"approved": False, "summary": "파싱 실패 — 재검토 필요",
                    "parse_error": True, "issues": []}

    with mock.patch.object(mt, "_jury_decide", return_value=parse_failed), \
         mock.patch.object(mt, "_build_priority_table", return_value=[]), \
         mock.patch.object(mt, "_architect_compromise",
                           return_value="절충안 적용") as compromise:
        mt._handle_final_round_no_consensus(
            state, {"issues": []}, {"issues": []}, {},
            {"coverage_pct": 83.3}, 4, 4, round_log, summary, lambda _p: None,
        )

    assert compromise.called, (
        "파싱 실패가 절충 경로를 건너뛴다 — 마지막 라운드가 아무 결정도 못 낸다"
    )
    assert round_log.get("compromise") == "절충안 적용"
    assert summary["consensus"] is False


def test_jury_valid_verdict_still_skips_compromise():
    """POSITIVE: 정상 판정이면 절충안을 만들지 않는다 (기능 보존)."""
    state, round_log, summary = _consensus_state()
    good = {"approved": False, "summary": "구조 개선 필요",
            "required_fixes": [], "issues": []}

    with mock.patch.object(mt, "_jury_decide", return_value=good), \
         mock.patch.object(mt, "_architect_compromise") as compromise:
        mt._handle_final_round_no_consensus(
            state, {"issues": []}, {"issues": []}, {},
            {"coverage_pct": 83.3}, 4, 4, round_log, summary, lambda _p: None,
        )
    assert not compromise.called
    assert round_log["jury_final"] == good


# ── 5. 죽은 워커 오진 (JobRegistry) ──────────────────────────────────


def test_dead_worker_is_reported_failed_not_running():
    """THE REGRESSION: 결과 없이 죽은 워커를 영원히 'running' 으로 보고하면 안 된다.

    실패로 전환되지 않으면 single-flight 가 같은 key 의 **재시도까지 차단** 해,
    서버 재시작 전까지 그 작업을 다시 돌릴 수 없다. S2 는 50분 잡이라 이 오진이
    특히 비싸다.
    """
    import json
    import time

    from tools.common import JobRegistry

    registry = JobRegistry(name="probe", poll_with="get_probe_status")
    job_id = json.loads(
        registry.dispatch(key="k", worker=lambda: json.dumps({"ok": True})),
    )["job_id"]
    time.sleep(0.3)
    # 워커는 끝났지만 상태가 running 으로 남은 상황을 재현.
    with registry._lock:
        registry._jobs[job_id]["status"] = "running"
        registry._jobs[job_id]["result"] = None

    status = json.loads(registry.status(job_id))
    assert status["status"] == "failed", (
        f"죽은 워커가 {status['status']} 로 보고됐다 — 재시도가 영구 차단된다"
    )
    assert "다시 시작" in str(status.get("error"))

    # 그리고 같은 key 로 재시도가 가능해야 한다.
    again = json.loads(
        registry.dispatch(key="k", worker=lambda: json.dumps({"ok": True})),
    )
    assert again["started"] is True, "single-flight 가 재시도를 여전히 막는다"


def test_live_worker_still_reports_running():
    """POSITIVE: 살아있는 워커는 계속 running 으로 보고한다 (오탐 방지)."""
    import json
    import threading
    import time

    from tools.common import JobRegistry

    release = threading.Event()
    registry = JobRegistry(name="probe2", poll_with="p")
    job_id = json.loads(registry.dispatch(
        key="slow",
        worker=lambda: (release.wait(5), json.dumps({"ok": True}))[1],
    ))["job_id"]
    time.sleep(0.2)
    try:
        status = json.loads(registry.status(job_id))
        assert status["status"] == "running"
    finally:
        release.set()
        time.sleep(0.2)


@pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
def test_hard_failure_is_recorded_before_reraise():
    """``BaseException`` 도 failed 로 기록된다 (스레드만 죽고 잡이 남는 것 방지)."""
    import json
    import time

    from tools.common import JobRegistry

    def suicidal():
        raise BaseException("simulated hard failure")  # noqa: TRY002

    registry = JobRegistry(name="probe3", poll_with="p")
    job_id = json.loads(registry.dispatch(key="x", worker=suicidal))["job_id"]
    time.sleep(0.3)
    status = json.loads(registry.status(job_id))
    assert status["status"] == "failed"
    assert "simulated hard failure" in str(status.get("error"))
