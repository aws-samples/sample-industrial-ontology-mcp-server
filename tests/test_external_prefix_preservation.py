"""외래 네임스페이스 참조가 파이프라인 어느 단계에서도 소실되지 않는다.

2026-08-09 실측 피해: S2 산출물의 ``<DOMAIN_NS + "iof-core:MaterialArtifact">``
유령 13건이 S3 를 지나 ``steel:MaterialArtifact`` (T-Box 어디에도 **선언되지 않은**
도메인 클래스) 로 바뀌었다. 배포된 T-Box 에는 그 미선언 IRI 를 향한 참조가 13회,
같은 방식으로 손실된 IOF 참조가 총 42 트리플 있었다. IOF 매핑은 프롬프트가 명시적으로
요구하는 산출물인데, 손실이 **어떤 게이트에도 잡히지 않았다** (check_quality_rules 60
이슈 중 MaterialArtifact 언급 0건).

원인은 하나의 버그 클래스가 여러 사본으로 존재한 것이다: "DOMAIN_NS 안에 콜론이 있으면
콜론 앞을 버린다". 콜론 앞이 무엇인지 보지 않으므로 도메인 prefix 든 외래 prefix 든
똑같이 벗겨진다. 이 파일은 각 사본 위치에서 **정당한 외래 참조가 보존되는지** 를
주장한다 (카운터가 1 이상 올랐는지가 아니라).

프로젝트 규칙대로 각 테스트는 원래 버그를 되살리면 실패해야 한다 — 예:
``step_00._fix_double_prefix_uri`` 를 ``local.split(":", 1)[1]`` 로 되돌리거나,
``jury_fixes._as_uri`` 를 ``graph_ns[name.split(":", 1)[1]]`` 로 되돌리면
해당 테스트가 깨진다.
"""
from __future__ import annotations

import pytest
from rdflib import OWL, RDF, RDFS, Graph, URIRef

from domain.graph_utils import resolve_entity_name, split_embedded_prefix
from domain.namespaces import (
    DOMAIN_NS,
    FOREIGN_PREFIXES,
    IOF_CORE,
    IOF_SCRO,
    NS_PREFIX,
    bind_namespaces,
)

IOF_MA = IOF_CORE + "MaterialArtifact"
_HDR = (
    f"@prefix {NS_PREFIX}: <{DOMAIN_NS}> .\n"
    f"@prefix iof-core: <{IOF_CORE}> .\n"
    f"@prefix iof-scro: <{IOF_SCRO}> .\n"
    "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
    "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
)


def _graph(body: str) -> Graph:
    g = Graph()
    g.parse(data=_HDR + body, format="turtle")
    bind_namespaces(g)
    return g


# ── 공용 해석기 ───────────────────────────────────────────────────────


@pytest.mark.parametrize("given,expected", [
    ("MaterialArtifact", DOMAIN_NS + "MaterialArtifact"),
    (f"{NS_PREFIX}:MaterialArtifact", DOMAIN_NS + "MaterialArtifact"),
    ("iof-core:MaterialArtifact", IOF_MA),
    ("iof-scro:SupplyChainProcess", IOF_SCRO + "SupplyChainProcess"),
    ("urn:x:y", "urn:x:y"),
    ("http://other/X", "http://other/X"),
])
def test_resolver_sends_each_name_to_its_own_namespace(given, expected):
    assert str(resolve_entity_name(given, _graph(""))) == expected


def test_resolver_prefers_exact_domain_prefix_over_substring():
    """도메인 prefix 판정은 **정확 일치** 여야 한다.

    부분문자열 검사였을 때, prefix 가 ``core`` 인 도메인에서 ``iof-core:`` 가
    부분일치해 외래 이름이 도메인으로 끌려왔다.
    """
    g = _graph("")
    # NS_PREFIX 가 무엇이든, 외래 prefix 는 자기 네임스페이스로 가야 한다.
    assert str(resolve_entity_name("iof-core:X", g)) == IOF_CORE + "X"


def test_resolver_works_without_a_graph():
    """그래프 없이도 등록된 외래 prefix 는 해석된다 (정적 표 안전망)."""
    assert str(resolve_entity_name("iof-core:X", None)) == IOF_CORE + "X"


@pytest.mark.parametrize("bad", ["", ":", f"{NS_PREFIX}:", "   "])
def test_resolver_refuses_empty_names(bad):
    """빈 local name 은 ``<DOMAIN_NS>`` 자체를 가리키는 또 다른 유령이 된다."""
    with pytest.raises(ValueError):
        resolve_entity_name(bad, _graph(""))


def test_split_embedded_prefix_ignores_legitimate_colons():
    """콜론이 있다는 것만으로 유령이라고 판정하면 A-Box 데이터가 손상된다.

    인스턴스 IRI 는 timestamp 를 percent-encode 하지 않아 콜론이 정당하게 들어간다
    (``Proc_P001_2025-01-01T00:00:00``). Turtle 도 local name 에 콜론을 허용한다.
    """
    g = _graph("")
    assert split_embedded_prefix(DOMAIN_NS + "Proc_P001_2025-01-01T00:00:00", g) is None
    assert split_embedded_prefix(DOMAIN_NS + "unregistered:Thing", g) is None
    assert split_embedded_prefix(DOMAIN_NS + "Normal", g) is None
    # 알려진 prefix 는 복구 대상.
    assert split_embedded_prefix(DOMAIN_NS + "iof-core:X", g) == (IOF_CORE, "X")


def test_foreign_prefix_registry_covers_what_the_prompts_emit():
    """프롬프트/산출물이 쓰는 prefix 는 레지스트리에 등록돼 있어야 한다.

    ``iof-scro`` 는 배포 T-Box 헤더와 ``owl:imports`` 에 있는데 레지스트리에는
    없었다. 그래서 그래프 바인딩이 유실된 경로에서 조용히 도메인 클래스로 뭉개졌다.
    """
    for prefix in ("iof-core", "iof-maint", "iof-scro", "dcterms", "skos"):
        assert prefix in FOREIGN_PREFIXES, f"{prefix} 미등록"


def test_bind_namespaces_wins_over_rdflib_defaults():
    """도메인 prefix 가 rdflib 기본 prefix 와 겹쳐도 바인딩이 이겨야 한다.

    ``Graph.bind`` 는 기본값이 ``replace=False`` 라, prefix 가 ``time`` / ``org``
    / ``prov`` 같은 rdflib 기본값과 겹치면 **조용히 무시** 된다. 그래프 바인딩을
    신뢰하는 해석기는 그때 도메인 이름을 외래 NS 로 보내버린다 — 지금 고치는
    버그의 정확한 역방향이다.
    """
    g = Graph()
    bind_namespaces(g)
    bound = dict(g.namespace_manager.namespaces())
    assert str(bound[NS_PREFIX]) == DOMAIN_NS


# ── S2 Jury 쓰기 경로 (jury_fixes) ───────────────────────────────────


def test_jury_add_object_property_keeps_foreign_range():
    """Jury 가 외래 클래스를 range 로 주면 그 네임스페이스가 유지된다."""
    from tools.jury_fixes import apply_jury_fixes

    res = apply_jury_fixes(_HDR + f"{NS_PREFIX}:Slab a owl:Class .\n", [{
        "action": "add_object_property", "property": "madeOf",
        "domain": f"{NS_PREFIX}:Slab", "range": "iof-core:MaterialArtifact",
    }])
    g = Graph()
    g.parse(data=res["ttl"], format="turtle")
    ranges = {str(o) for o in g.objects(URIRef(DOMAIN_NS + "madeOf"), RDFS.range)}
    assert ranges == {IOF_MA}, f"외래 range 가 뭉개졌다: {ranges}"


def test_jury_restriction_keeps_foreign_some_values_from():
    from tools.jury_fixes import apply_jury_fixes

    body = (
        f"{NS_PREFIX}:Slab a owl:Class .\n"
        f"{NS_PREFIX}:madeOf a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:Slab ; rdfs:range iof-core:MaterialArtifact .\n"
    )
    res = apply_jury_fixes(_HDR + body, [{
        "action": "add_restriction", "class": f"{NS_PREFIX}:Slab",
        "onProperty": f"{NS_PREFIX}:madeOf",
        "someValuesFrom": "iof-core:MaterialArtifact",
    }])
    g = Graph()
    g.parse(data=res["ttl"], format="turtle")
    values = {str(o) for o in g.objects(None, OWL.someValuesFrom)}
    assert IOF_MA in values, f"someValuesFrom 이 뭉개졌다: {values}"


def test_jury_namespace_bulk_repairs_foreign_ghost():
    """유령 복구가 외래 prefix 도 다룬다 (예전엔 통과시켰다)."""
    from tools.jury_fixes import _apply_fix_namespace_bulk

    g = _graph(
        f"{NS_PREFIX}:Slab a owl:Class ; rdfs:subClassOf "
        f"<{DOMAIN_NS}iof-core:MaterialArtifact> .\n"
    )
    ok, msg = _apply_fix_namespace_bulk(g, {})
    parents = {str(o) for o in g.objects(URIRef(DOMAIN_NS + "Slab"), RDFS.subClassOf)}
    assert ok, msg
    assert parents == {IOF_MA}, f"외래 유령이 복구되지 않았다: {parents}"


def test_jury_namespace_bulk_has_no_hardcoded_prefix_literal():
    """도메인 prefix 를 리터럴로 박으면 다른 도메인에서 조용히 no-op 이 된다.

    실측: prefix 가 ``med`` 인 설정에서 ``<…#med:Ghost>`` 가 복구되지 않았다.
    """
    import inspect

    from tools.jury_fixes import _apply_fix_namespace_bulk

    # docstring 은 과거 버그를 설명하므로 리터럴이 등장한다 — 실행 코드만 본다.
    src = inspect.getsource(_apply_fix_namespace_bulk)
    body = src.split('"""')[2] if src.count('"""') >= 2 else src
    assert '"steel:"' not in body and "'steel:'" not in body


def test_jury_rename_uri_resolves_both_sides_the_same_way():
    """``from`` 과 ``to`` 가 같은 해석기를 써야 한다.

    ``from`` 만 도메인 NS 를 하드코딩했을 때, ``from="iof-core:MaterialArtifact"``
    가 IOF 리소스가 아니라 **동명의 도메인 클래스** 를 개명했다.
    """
    from tools.jury_fixes import apply_jury_fixes

    body = (
        f"{NS_PREFIX}:A a owl:Class ; rdfs:subClassOf iof-core:MaterialArtifact .\n"
        f"{NS_PREFIX}:MaterialArtifact a owl:Class .\n"
    )
    res = apply_jury_fixes(_HDR + body, [{
        "action": "rename_uri", "from": "iof-core:MaterialArtifact",
        "to": f"{NS_PREFIX}:Renamed",
    }])
    g = Graph()
    g.parse(data=res["ttl"], format="turtle")
    # 도메인 클래스는 그대로 남아 있어야 한다 (엉뚱한 개명 금지).
    assert (URIRef(DOMAIN_NS + "MaterialArtifact"), RDF.type, OWL.Class) in g
    parents = {str(o) for o in g.objects(URIRef(DOMAIN_NS + "A"), RDFS.subClassOf)}
    assert parents == {DOMAIN_NS + "Renamed"}, parents


# ── S3 품질 스텝 (step_00 / step_09d) ────────────────────────────────


def _step_ctx():
    from tools.quality_steps._base import StepContext
    return StepContext(domain_ns=DOMAIN_NS)


def test_step_00_recovers_foreign_ghost_to_its_own_namespace():
    """THE REGRESSION: S3 의 유령 교정이 외래 매핑을 파괴하지 않는다."""
    from tools.quality_steps.step_00_antipatterns import _fix_double_prefix_uri

    g = _graph(
        f"{NS_PREFIX}:Slab a owl:Class ; rdfs:subClassOf "
        f"<{DOMAIN_NS}iof-core:MaterialArtifact> .\n"
    )
    fixed = _fix_double_prefix_uri(g, DOMAIN_NS)
    parents = {str(o) for o in g.objects(URIRef(DOMAIN_NS + "Slab"), RDFS.subClassOf)}
    assert fixed >= 1
    assert parents == {IOF_MA}, f"IOF 매핑이 도메인 클래스로 뭉개졌다: {parents}"


def test_step_00_still_repairs_domain_ghost():
    """PRESERVATION: 도메인 prefix 유령 교정은 그대로 동작한다."""
    from tools.quality_steps.step_00_antipatterns import _fix_double_prefix_uri

    g = _graph(f"<{DOMAIN_NS}{NS_PREFIX}:Ghost> a owl:Class .\n")
    _fix_double_prefix_uri(g, DOMAIN_NS)
    assert (URIRef(DOMAIN_NS + "Ghost"), RDF.type, OWL.Class) in g
    assert not [s for s in g.subjects() if f"#{NS_PREFIX}:" in str(s)]


def test_step_00_leaves_correct_external_iris_alone():
    """PRESERVATION: 이미 올바른 외래 IRI 는 건드리지 않는다."""
    from tools.quality_steps.step_00_antipatterns import _fix_double_prefix_uri

    g = _graph(
        f"{NS_PREFIX}:Slab a owl:Class ; rdfs:subClassOf iof-core:MaterialArtifact .\n"
    )
    before = set(g)
    _fix_double_prefix_uri(g, DOMAIN_NS)
    assert set(g) == before


def test_step_09d_recovers_ghost_instead_of_collapsing_to_owl_thing():
    """S3 안의 **두 번째 독립 손실 경로**.

    step_00 이 유령을 고쳐도, 09d 는 ``'#'`` 로만 잘라 phantom_name
    ``'iof-core:MaterialArtifact'`` 를 얻고 어떤 클래스와도 매칭되지 않아
    ``owl:Thing`` 으로 덮어썼다 — 같은 IOF 매핑을 다시 파괴한다.
    """
    from tools.quality_steps.step_09d_phantom_class_fix import apply

    g = _graph(
        f"{NS_PREFIX}:Real a owl:Class .\n"
        f"{NS_PREFIX}:op a owl:ObjectProperty ; rdfs:domain {NS_PREFIX}:Real ; "
        f"rdfs:range <{DOMAIN_NS}iof-core:MaterialArtifact> .\n"
    )
    apply(g, _step_ctx())
    ranges = {str(o) for o in g.objects(URIRef(DOMAIN_NS + "op"), RDFS.range)}
    assert ranges == {IOF_MA}, f"owl:Thing 으로 붕괴했다: {ranges}"


def test_step_09d_still_matches_real_phantom_classes():
    """PRESERVATION: 진짜 phantom (오타 클래스) 교정은 그대로 동작한다."""
    from tools.quality_steps.step_09d_phantom_class_fix import apply

    g = _graph(
        f"{NS_PREFIX}:SupplierMaster a owl:Class .\n"
        f"{NS_PREFIX}:op a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:SupplierMaster ; rdfs:range {NS_PREFIX}:Supplier .\n"
    )
    res = apply(g, _step_ctx())
    ranges = {str(o) for o in g.objects(URIRef(DOMAIN_NS + "op"), RDFS.range)}
    assert res.stats["phantom_class_fixed"] >= 1
    assert ranges == {DOMAIN_NS + "SupplierMaster"}, ranges


def test_no_undeclared_domain_class_is_invented_by_s3():
    """일반 회귀: S3 후 도메인 NS 를 가리키는 subClassOf 부모는 선언돼 있어야 한다.

    배포 T-Box 는 이 조건을 위반했다 (``steel:MaterialArtifact`` 13회 참조, 선언
    0건). 특정 사이트가 아니라 **결과** 를 검사하므로, 같은 버그 클래스의 새 사본이
    생겨도 잡힌다.
    """
    from tools.ontology_quality import improve_tbox

    ttl = _HDR + (
        f"{NS_PREFIX}:Slab a owl:Class ; rdfs:subClassOf "
        f"<{DOMAIN_NS}iof-core:MaterialArtifact> .\n"
        f"{NS_PREFIX}:Plant a owl:Class .\n"
        f"{NS_PREFIX}:madeIn a owl:ObjectProperty ; "
        f"rdfs:domain {NS_PREFIX}:Slab ; rdfs:range {NS_PREFIX}:Plant .\n"
    )
    improved, _stats = improve_tbox(ttl)
    g = Graph()
    g.parse(data=improved, format="turtle")
    # skolemize 된 owl:Restriction 도 정상 선언 대상이다 (BNode 대체물).
    declared = {
        str(c) for c in
        set(g.subjects(RDF.type, OWL.Class)) | set(g.subjects(RDF.type, OWL.Restriction))
    }
    undeclared = {
        str(o) for _, _, o in g.triples((None, RDFS.subClassOf, None))
        if isinstance(o, URIRef) and str(o).startswith(DOMAIN_NS)
        and str(o) not in declared
    }
    assert not undeclared, f"미선언 도메인 클래스를 부모로 만들었다: {undeclared}"


# ── dcterms:source 값 정리 (step_12d-2) ──────────────────────────────


def test_s3_cleans_turtle_notation_out_of_source_values():
    """THE REGRESSION: 이미 오염된 산출물도 S3 가 정리한다.

    A-Box 는 ``dcterms:source`` 값을 CSV 헤더와 직접 비교하므로
    ``'"Event_ID"^^xsd:string'`` 은 어떤 헤더와도 매칭되지 않는다. 배포 T-Box
    실측 34건이 이 형태였고, Step 12e 게이트는 값의 **존재** 만 세므로 놓쳤다.
    """
    from rdflib import Literal as _Lit

    from tools.quality_steps._base import StepContext
    from tools.quality_steps.step_12d2_source_literal_cleanup import apply

    src = URIRef("http://purl.org/dc/terms/source")
    g = _graph(f"{NS_PREFIX}:dp a owl:DatatypeProperty .\n")
    dp = URIRef(DOMAIN_NS + "dp")
    g.add((dp, src, _Lit('"Event_ID"^^xsd:string')))
    g.add((dp, src, _Lit("Clean_Col")))

    res = apply(g, StepContext(domain_ns=DOMAIN_NS))
    values = {str(o) for o in g.objects(dp, src)}
    assert values == {"Event_ID", "Clean_Col"}, values
    assert res.stats["source_literal_cleaned"] == 1


def test_s3_leaves_clean_source_values_untouched():
    """PRESERVATION: 정상 컬럼명은 건드리지 않는다 (밑줄·숫자·하이픈 포함)."""
    from rdflib import Literal as _Lit

    from tools.quality_steps._base import StepContext
    from tools.quality_steps.step_12d2_source_literal_cleanup import apply

    src = URIRef("http://purl.org/dc/terms/source")
    g = _graph(f"{NS_PREFIX}:dp a owl:DatatypeProperty .\n")
    dp = URIRef(DOMAIN_NS + "dp")
    for col in ("QTY_COL_1", "Exit_Thickness_mm", "Col-With-Hyphen", "Timestamp"):
        g.add((dp, src, _Lit(col)))
    before = set(g.objects(dp, src))

    res = apply(g, StepContext(domain_ns=DOMAIN_NS))
    assert set(g.objects(dp, src)) == before
    assert res.stats["source_literal_cleaned"] == 0


def test_s3_removes_iri_valued_source_columns():
    """컬럼명 자리에 IRI 가 들어간 경우 제거한다 (CSV 헤더가 IRI 일 수 없다).

    실측: LLM 이 ``dcterms:source`` 값으로 ``xsd:string`` 을 보내 IRI 로 승격돼
    ``steel:string`` 이 됐다 (5건). 문자열 정리로는 못 잡는다 — 노드가 이미
    ``URIRef`` 라서 정리 함수의 대상이 아니다. 남겨두면 커버리지만 부풀린다.
    """
    from tools.quality_steps._base import StepContext
    from tools.quality_steps.step_12d2_source_literal_cleanup import apply

    src = URIRef("http://purl.org/dc/terms/source")
    g = _graph(f"{NS_PREFIX}:dp a owl:DatatypeProperty .\n")
    dp = URIRef(DOMAIN_NS + "dp")
    g.add((dp, src, URIRef(DOMAIN_NS + "string")))   # IRI — 컬럼명이 아니다
    from rdflib import Literal as _Lit
    g.add((dp, src, _Lit("Real_Column")))            # 정상 — 보존돼야 한다

    res = apply(g, StepContext(domain_ns=DOMAIN_NS))
    values = {str(o) for o in g.objects(dp, src)}
    assert values == {"Real_Column"}, values
    assert res.stats["source_literal_dropped"] == 1
