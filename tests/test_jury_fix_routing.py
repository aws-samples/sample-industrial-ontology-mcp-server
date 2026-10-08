"""Jury required_fixes 라우팅 회귀 가드 — DSL 선행 pass 가 방어 가드를 우회했다.

2026-08-18 실측: ``required_fixes`` 리스트를 **두 엔진이 순차로 소비**한다
(``_handle_potential_consensus`` / ``_handle_final_round_no_consensus``):

    ttl_after_dsl = _apply_high_level_instructions(ttl, required_fixes)   # ①
    fx            = apply_jury_fixes(ttl_after_dsl, required_fixes)       # ②

두 엔진이 아는 action 이 8개 겹치고(add_triple / remove_triple / add_subclass /
add_object_property / add_restriction / add_datatype_property /
add_disjoint_classes / add_functional_property / remove_class), ①에는 ②의 방어
가드가 **없다**. 그래서 ①이 가드 없이 적용해 버리고 ②는 "이미 존재/트리플 없음"
no-op 만 보고한다 — 가드가 있는데도 무력화된 상태다.

## 실증된 손상 (이 파일이 고정하는 것)

``remove_triple <OP> rdfs:range <Class>`` — 마지막 range 를 지우는 요청:
  - ``apply_jury_fixes`` 단독: **거부** (failed, "마지막 rdfs:range 삭제 거부 —
    제거하면 제약 없는 껍데기가 되어 A-Box 생성기·conformance 검사·중복 게이트가
    모두 그 프로퍼티를 무시한다")
  - DSL 선행: **삭제됨** (range=[]). 그 뒤 jury_fixes 는 "해당 트리플이 존재하지
    않음" no-op 을 보고한다.

Jury 는 실제로 이 형태를 요청한다 — 2026-08-17 실행에서
``remove_triple <OP> rdfs:range xsd:string`` 4건을 요청했다.

## 왜 키 규약이 겹침을 줄여주지 않는가

``add_object_property``/``add_restriction`` 은 Jury 가 ``property``/``class`` 키를
쓰는데 DSL 은 ``name``/``target_class`` 를 요구해 ①에서 KeyError 로 실패한다 —
즉 그 둘은 우연히 안전하다. 반면 ``add_triple``/``remove_triple`` 은 키 규약이
같아 ①이 그대로 적용한다. 우연에 의존하지 않도록 **라우팅을 명시**한다.
"""
from __future__ import annotations

import pytest
from rdflib import OWL, RDF, RDFS, Graph, URIRef
from rdflib.collection import Collection

import tools.multi_agent_tbox as mt
from tools.jury_fixes import apply_jury_fixes

STEEL = "http://example.com/steel-ontology#"

TTL = """
@prefix steel: <http://example.com/steel-ontology#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

steel:Shipment a owl:Class .
steel:Warehouse a owl:Class .
steel:existingLink a owl:ObjectProperty ;
    rdfs:domain steel:Shipment ; rdfs:range steel:Warehouse .
"""

_REMOVE_LAST_RANGE = [{
    "action": "remove_triple",
    "subject": "steel:existingLink",
    "predicate": "rdfs:range",
    "object": "steel:Warehouse",
}]


def _ranges(ttl: str, prop: str = "existingLink") -> list:
    g = Graph()
    g.parse(data=ttl, format="turtle")
    return list(g.objects(URIRef(STEEL + prop), RDFS.range))


# ── 라우팅 계약 ───────────────────────────────────────────────────────────

def test_shared_actions_do_not_go_through_the_dsl_engine():
    """양쪽이 아는 action 은 DSL 로 보내지 않는다 (jury_fixes 가 가드를 갖는다)."""
    fixes = [
        {"action": "remove_triple", "subject": "steel:X",
         "predicate": "rdfs:range", "object": "steel:Y"},
        {"action": "add_triple", "subject": "steel:X",
         "predicate": "owl:inverseOf", "object": "steel:Y"},
        {"action": "add_object_property", "property": "steel:p",
         "domain": "steel:X", "range": "steel:Y"},
        {"action": "add_transitive_property", "name": "existingLink"},
    ]
    dsl_only, jury_bound = mt._route_jury_fixes(fixes)

    assert dsl_only == [], f"공유 action 이 DSL 로 갔다: {dsl_only}"
    assert jury_bound == fixes


def test_dsl_only_actions_still_reach_the_dsl_engine():
    """NEGATIVE: DSL 전용 action 은 계속 DSL 로 가야 한다.

    ``jury_fixes`` 는 rename_class / rename_property / merge_classes 등을
    모른다. 전부 jury_fixes 로 몰면 그 지시들이 통째로 버려진다 — 예전에 반대
    방향으로 겪은 실패(DSL 전용 경로라 jury 전용 action 이 버려짐)를 되풀이하는 것이다.
    """
    fixes = [
        {"action": "rename_class", "old": "A", "new": "B"},
        {"action": "rename_property", "old": "p", "new": "q"},
        {"action": "merge_classes", "into": "A", "from": ["B"]},
    ]
    dsl_only, jury_bound = mt._route_jury_fixes(fixes)

    assert dsl_only == fixes, f"DSL 전용 action 이 누락됐다: {dsl_only}"
    assert jury_bound == []


def test_unknown_actions_go_to_jury_fixes():
    """어느 쪽도 모르는 action 은 jury_fixes 로 — 거기서 미지원으로 집계된다.

    DSL 로 보내면 예외를 던지거나 조용히 무시되고, 운영자는 요청이 어디로
    사라졌는지 알 수 없다.
    """
    dsl_only, jury_bound = mt._route_jury_fixes(
        [{"action": "totally_unknown_action", "foo": "bar"}],
    )
    assert dsl_only == []
    assert len(jury_bound) == 1


# ── 가드가 실제로 살아나는가 (산출물 검증) ────────────────────────────────

def test_last_range_guard_survives_routing():
    """THE REGRESSION: 라우팅 후 마지막 range 삭제가 거부된다.

    카운터가 아니라 **그래프**를 본다 — "거부했다" 는 보고와 실제 그래프가
    어긋나도 카운터만 보면 통과한다.
    """
    dsl_only, jury_bound = mt._route_jury_fixes(_REMOVE_LAST_RANGE)
    ttl = TTL
    if dsl_only:
        ttl = mt._apply_high_level_instructions(ttl, dsl_only)
    result = apply_jury_fixes(ttl, jury_bound)

    assert _ranges(result["ttl"]) == [URIRef(STEEL + "Warehouse")], (
        "마지막 rdfs:range 가 삭제됐다 — 가드가 우회됐다"
    )
    assert result["failed"], "거부가 보고되지 않았다"


def test_dsl_alone_would_have_removed_it():
    """이 테스트가 지키는 위험이 실재함을 고정한다 (대조군).

    DSL 을 직접 태우면 range 가 사라진다. 이 주장이 깨지면(DSL 에 같은 가드가
    생기면) 라우팅 변경의 근거가 약해지므로 그 사실을 알아야 한다.
    """
    after_dsl = mt._apply_high_level_instructions(TTL, _REMOVE_LAST_RANGE)
    assert _ranges(after_dsl) == [], (
        "DSL 에 마지막 range 가드가 생겼다 — 라우팅 근거를 재검토할 것"
    )


def test_duplicate_op_guard_survives_routing():
    """이미 연결된 (domain, range) 에 중복 OP 를 만들지 않는다."""
    fixes = [{
        "action": "add_object_property", "property": "steel:newDupLink",
        "domain": "steel:Shipment", "range": "steel:Warehouse",
    }]
    dsl_only, jury_bound = mt._route_jury_fixes(fixes)
    ttl = TTL
    if dsl_only:
        ttl = mt._apply_high_level_instructions(ttl, dsl_only)
    result = apply_jury_fixes(ttl, jury_bound)

    g = Graph()
    g.parse(data=result["ttl"], format="turtle")
    assert (URIRef(STEEL + "newDupLink"), RDF.type, OWL.ObjectProperty) not in g, (
        "중복 OP 가 생성됐다 — step_22d 게이트를 악화시킨다"
    )


def test_no_double_application_of_shared_actions():
    """같은 action 이 그래프에 두 번 반영되지 않는다.

    실측 위험: 두 엔진이 ``add_disjoint_classes`` 를 각자 **BNode 로** 만들어
    "이미 존재" 판정이 불가능해지고 ``AllDisjointClasses`` 노드가 2개 생긴다
    (jury_action_normalizer 모듈 docstring 이 경고한 실패 모드).
    """
    fixes = [{
        "action": "add_disjoint_classes",
        "members": ["steel:Shipment", "steel:Warehouse"],
    }]
    dsl_only, jury_bound = mt._route_jury_fixes(fixes)
    ttl = TTL
    if dsl_only:
        ttl = mt._apply_high_level_instructions(ttl, dsl_only)
    result = apply_jury_fixes(ttl, jury_bound)

    g = Graph()
    g.parse(data=result["ttl"], format="turtle")
    nodes = list(g.subjects(RDF.type, OWL.AllDisjointClasses))
    assert len(nodes) == 1, f"AllDisjointClasses 노드가 {len(nodes)}개 생겼다"
    assert set(Collection(g, g.value(nodes[0], OWL.members))) == {
        URIRef(STEEL + "Shipment"), URIRef(STEEL + "Warehouse"),
    }


# ── 배선 ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("round_kind", ["mid", "final"])
@pytest.mark.parametrize("scenario", ["last_range", "duplicate_op", "shared_once", "dsl_only"])
def test_both_jury_paths_use_the_router(monkeypatch, round_kind, scenario):
    """두 라운드에서 실제 그래프와 요약으로 라우팅·가드·적용을 검증한다."""
    fixes = {
        "last_range": [dict(_REMOVE_LAST_RANGE[0])],
        "duplicate_op": [{
            "action": "add_object_property", "property": "steel:newDupLink",
            "domain": "steel:Shipment", "range": "steel:Warehouse",
        }],
        "shared_once": [
            {"action": "add_disjoint_classes",
             "members": ["steel:Shipment", "steel:Warehouse"]},
            {"action": "add_transitive_property", "name": "existingLink"},
        ],
        "dsl_only": [{"action": "rename_class", "old": "Warehouse", "new": "Depot"}],
    }[scenario]
    result = _run_round(monkeypatch, round_kind, TTL, fixes)
    graph = Graph().parse(data=result["state"]["current_ttl"], format="turtle")
    summary = result["round_log"]["jury_fixes_summary"]
    assert summary["requested"] == len(fixes)

    if scenario == "dsl_only":
        assert summary["routed_dsl"] == 1
        assert summary["routed_jury"] == 0
        assert summary["dsl"]["applied"] == 1
        assert (URIRef(STEEL + "Depot"), RDF.type, OWL.Class) in graph
        assert URIRef(STEEL + "Warehouse") not in set(graph.all_nodes())
        assert _ranges(result["state"]["current_ttl"]) == [URIRef(STEEL + "Depot")]
    else:
        assert summary["routed_dsl"] == 0
        assert summary["routed_jury"] == len(fixes)
        if scenario == "last_range":
            assert _ranges(result["state"]["current_ttl"]) == [URIRef(STEEL + "Warehouse")]
            assert summary["failed"] == 1
        elif scenario == "duplicate_op":
            assert not list(graph.triples((URIRef(STEEL + "newDupLink"), None, None)))
            assert summary["applied"] == 0
        else:
            nodes = list(graph.subjects(RDF.type, OWL.AllDisjointClasses))
            assert len(nodes) == 1
            assert set(Collection(graph, graph.value(nodes[0], OWL.members))) == {
                URIRef(STEEL + "Shipment"), URIRef(STEEL + "Warehouse"),
            }
            assert (URIRef(STEEL + "existingLink"), RDF.type, OWL.TransitiveProperty) in graph
            assert summary["applied"] == 2
            assert summary["failed"] == 0


@pytest.mark.parametrize("round_kind", ["mid", "final"])
def test_routing_counts_are_reported(monkeypatch, round_kind):
    """라우팅 결과가 응답에 남는다 — 어디로 갔는지 모르면 진단이 불가능하다."""
    result = _run_round(monkeypatch, round_kind, TTL, [
        {"action": "rename_class", "old": "Warehouse", "new": "Depot"},
        {"action": "add_transitive_property", "name": "existingLink"},
    ])
    graph = Graph().parse(data=result["state"]["current_ttl"], format="turtle")
    summary = result["round_log"]["jury_fixes_summary"]
    assert (summary["requested"], summary["routed_dsl"], summary["routed_jury"]) == (2, 1, 1)
    assert summary["dsl"]["applied"] == 1
    assert summary["applied"] == 1
    assert summary["failed"] == 0
    assert (URIRef(STEEL + "Depot"), RDF.type, OWL.Class) in graph
    assert URIRef(STEEL + "Warehouse") not in set(graph.all_nodes())
    assert _ranges(result["state"]["current_ttl"]) == [URIRef(STEEL + "Depot")]
    assert (URIRef(STEEL + "existingLink"), RDF.type, OWL.TransitiveProperty) in graph


def _jury_stub(required_fixes: list[dict]):
    """Jury 응답 스텁 — production_ready=false + 주어진 fixes."""
    return {
        "production_ready": False,
        "decisions": [],
        "jury_issues": [],
        "required_fixes": required_fixes,
        "summary": "stub",
    }


def _run_mid_round(monkeypatch, ttl: str, fixes: list[dict]) -> dict:
    """``_handle_potential_consensus`` 를 태우고 state 를 돌려준다."""
    return _run_round(monkeypatch, "mid", ttl, fixes)


def _run_round(monkeypatch, round_kind: str, ttl: str, fixes: list[dict]) -> dict:
    """LLM 경계만 대체하고 실제 라운드·라우터·수정 엔진을 실행한다."""
    # ``_jury_decide`` 가 실제 호출 지점이다 (grep 확인). raising 기본값을 유지해
    # 이름이 바뀌면 테스트가 실패하도록 둔다 — raising=False 로 두면 함수가
    # 사라져도 조용히 통과한다.
    monkeypatch.setattr(mt, "_jury_decide", lambda *a, **k: _jury_stub(fixes))
    monkeypatch.setattr(
        mt, "_architect_compromise",
        lambda *a, **k: pytest.fail("라우팅 테스트에서 예상 밖의 Architect 호출"),
    )
    state = {
        "current_ttl": ttl,
        "debate_log": [],
        "consensus_reached": False,
        "veto_lock_triggered": False,
        "veto_persistent_targets": [],
    }
    round_log: dict = {}
    summary: dict = {}
    cq_runtime = {"coverage_pct": 100.0, "answerable": [], "unanswerable": []}
    if round_kind == "mid":
        mt._handle_potential_consensus(
            state, {"issues": []}, {"issues": []}, {}, cq_runtime,
            2, round_log, summary, lambda *_a, **_k: None,
        )
    else:
        assert round_kind == "final"
        mt._handle_final_round_no_consensus(
            state, {"issues": []}, {"issues": []}, {}, cq_runtime,
            2, 3, round_log, summary, lambda *_a, **_k: None,
        )
    return {"state": state, "round_log": round_log}


def test_mid_round_path_preserves_the_guard(monkeypatch):
    """중간 라운드 경로도 가드를 지킨다 (동작 검증, 소스 문자열 아님).

    소스에 ``_route_jury_fixes`` 문자열이 있는지만 보면, 라우터를 호출하면서도
    결과를 잘못 쓰는 변경(예: ``dsl_only = required_fixes``)을 놓친다 — 실측으로
    그 mutant 가 생존했다.
    """
    result = _run_mid_round(monkeypatch, TTL, _REMOVE_LAST_RANGE)

    assert _ranges(result["state"]["current_ttl"]) == [URIRef(STEEL + "Warehouse")], (
        "중간 라운드에서 마지막 rdfs:range 가 삭제됐다 — 가드가 우회됐다"
    )
    fx = result["round_log"].get("jury_fixes_summary") or {}
    assert fx.get("routed_dsl") == 0, f"공유 action 이 DSL 로 갔다: {fx}"
    assert fx.get("routed_jury") == 1


def test_mid_round_still_applies_dsl_only_actions(monkeypatch):
    """NEGATIVE: 중간 라운드에서 DSL 전용 action 은 계속 적용된다."""
    fixes = [{"action": "rename_class", "old": "Warehouse", "new": "Depot"}]
    result = _run_mid_round(monkeypatch, TTL, fixes)

    g = Graph()
    g.parse(data=result["state"]["current_ttl"], format="turtle")
    assert (URIRef(STEEL + "Depot"), RDF.type, OWL.Class) in g, (
        "DSL 전용 action 이 적용되지 않았다 — 라우팅이 그것을 버렸다"
    )
    assert URIRef(STEEL + "Warehouse") not in set(g.all_nodes())
    assert _ranges(result["state"]["current_ttl"]) == [URIRef(STEEL + "Depot")]
    fx = result["round_log"].get("jury_fixes_summary") or {}
    assert fx.get("routed_dsl") == 1
    assert fx["routed_jury"] == 0
    assert fx["dsl"]["applied"] == 1
