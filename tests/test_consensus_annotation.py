"""_annotate_consensus_status 단위 테스트."""
from __future__ import annotations

from rdflib import Graph

from tools.multi_agent_tbox import _annotate_consensus_status

_MINIMAL_TTL_WITH_ONTO = """\
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix steel: <http://example.com/steel-ontology#> .

<urn:steel:ontology> a owl:Ontology ;
  rdfs:label "Steel Ontology"@en .

steel:A a owl:Class .
"""


_MINIMAL_TTL_NO_ONTO = """\
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix steel: <http://example.com/steel-ontology#> .

steel:A a owl:Class .
"""


def _parseable(ttl: str) -> Graph:
    g = Graph()
    g.parse(data=ttl, format="turtle")
    return g


def test_annotation_preserves_parseable_ttl():
    out = _annotate_consensus_status(
        _MINIMAL_TTL_WITH_ONTO,
        consensus_reached=True,
        veto_lock_triggered=False,
        veto_targets=[],
        total_rounds=2,
    )
    g = _parseable(out)
    assert len(g) >= 2


def test_consensus_reached_status_embedded():
    out = _annotate_consensus_status(
        _MINIMAL_TTL_WITH_ONTO,
        consensus_reached=True,
        veto_lock_triggered=False,
        veto_targets=[],
        total_rounds=2,
    )
    assert "debate_status=consensus_reached" in out
    assert "consensus_reached=true" in out


def test_veto_lock_status_embedded():
    out = _annotate_consensus_status(
        _MINIMAL_TTL_WITH_ONTO,
        consensus_reached=False,
        veto_lock_triggered=True,
        veto_targets=["redundancy:alldisjointclasses", "cardinality:restriction"],
        total_rounds=4,
    )
    assert "debate_status=debate_unresolved_veto_lock" in out
    assert "veto_persistent_targets" in out
    assert "redundancy:alldisjointclasses" in out
    assert "consensus_reached=false" in out


def test_max_rounds_status_embedded():
    out = _annotate_consensus_status(
        _MINIMAL_TTL_WITH_ONTO,
        consensus_reached=False,
        veto_lock_triggered=False,
        veto_targets=[],
        total_rounds=3,
    )
    assert "debate_status=debate_unresolved_max_rounds" in out
    assert "total_rounds=3" in out


def test_adds_ontology_declaration_when_missing():
    out = _annotate_consensus_status(
        _MINIMAL_TTL_NO_ONTO,
        consensus_reached=False,
        veto_lock_triggered=False,
        veto_targets=[],
        total_rounds=2,
    )
    # owl:Ontology 가 추가되어야 하고 파싱 가능해야 함.
    assert "owl:Ontology" in out
    _parseable(out)


def test_adds_dcterms_prefix_when_missing():
    """rdflib serialize 결과에 dcterms prefix 바인딩이 포함돼야 한다."""
    out = _annotate_consensus_status(
        _MINIMAL_TTL_NO_ONTO,
        consensus_reached=True,
        veto_lock_triggered=False,
        veto_targets=[],
        total_rounds=1,
    )
    assert "@prefix dcterms:" in out
    # description 리터럴은 dcterms:description 혹은 full URI 둘 다 허용
    assert "multi_agent_debate" in out


def test_truncates_veto_targets_list_to_5():
    targets = [f"target_{i}" for i in range(20)]
    out = _annotate_consensus_status(
        _MINIMAL_TTL_WITH_ONTO,
        consensus_reached=False,
        veto_lock_triggered=True,
        veto_targets=targets,
        total_rounds=4,
    )
    assert "target_0" in out
    assert "target_4" in out
    # 초과분은 포함되지 않음
    assert "target_10" not in out
