"""SWRL 규칙의 로컬명이 엔티티가 실제로 있는 네임스페이스에서 해석되는가.

2026-08-28 실측. ``run_swrl_inference`` 가 3개 규칙 전부 첫 원자에서 실패했다::

    Cannot find entity 'EquipmentMaster'!     ← T-Box 에 있는 클래스다
    Cannot find entity 'followedBy'!          ← tbox_extensions.ttl 에 있다
    Cannot find entity 'ChemicalAnalysis'!    ← T-Box 에 있는 클래스다

엔티티는 전부 존재했다. 원인은 ``set_as_rule`` 이 **enclosing 컨텍스트의 네임스페이스**로
로컬명을 푸는데, 그 컨텍스트가 엉뚱한 곳이었다는 것이다:

    t_box.ttl:34  owl:sameAs <https://w3id.org/steel-ontology-sample>   ← FAIR alias 한 줄
      ↓ OWL RL 이 sameAs 를 양방향 전파
    all_inferred.ttl  alias 노드가 imports/versionIRI/label 28 트리플을 복제받아
                      **완전한 owl:Ontology 로 실체화** (owl:Ontology 노드 3개)
      ↓ owlready2 가 그중 하나를 고른다
    base_iri = https://w3id.org/steel-ontology-sample#   ← DOMAIN_NS 가 아니다

owlready2 는 엔티티를 온톨로지가 아니라 **네임스페이스**에 담는다. 실측하면 네임스페이스는
올바르고 소유 온톨로지만 alias 였다::

    EquipmentMaster.namespace          → http://example.com/steel-ontology#   (맞음)
    EquipmentMaster.namespace.ontology → https://w3id.org/steel-ontology-sample#

## 실측으로 기각된 접근 3개

이 테스트가 고정하는 것은 "고쳤다" 가 아니라 **어떻게 고쳐야 하는가** 다. 셋 다 시도했다:

1. ``owl:sameAs`` 트리플 제거        → 여전히 alias 를 고른다 (복제 트리플이 남아 유효)
2. alias owl:Ontology 노드 통째 제거 → 이번엔 ``…/abox#`` 를 고른다 (노드가 3개다)
3. 무조건 ``get_namespace(DOMAIN_NS)`` → ``http://test.org/onto#`` 픽스처가 깨진다
   (``get_ontology(DOMAIN_NS)`` 도 새 온톨로지라 ``classes()`` 가 0으로 판별 불가)

그래서 **규칙이 참조하는 이름을 실제로 프로브해** 어느 네임스페이스가 그것을 푸는지 보고
고른다. 이름 기반 단정은 두 번 반증됐다.
"""
from __future__ import annotations

import pytest

from tools.swrl_inference import _referenced_names, _rule_context


class _FakeNamespace:
    """이름 집합만 아는 최소 스텁 — owlready2 의 getattr 조회 규약만 모방한다."""

    def __init__(self, iri: str, names: set[str]):
        self.base_iri = iri
        self._names = names

    def __getattr__(self, item):          # owlready2: 없으면 None
        if item.startswith("_"):
            raise AttributeError(item)
        return object() if item in self._names else None


class _FakeOntology(_FakeNamespace):
    def __init__(self, iri: str, names: set[str], namespaces: dict | None = None):
        super().__init__(iri, names)
        self._namespaces = namespaces or {}

    def get_namespace(self, iri: str):
        return self._namespaces.get(iri, _FakeNamespace(iri, set()))


DOMAIN = "http://example.com/steel-ontology#"


# ── 검출: alias base_iri 를 교정한다 ────────────────────────────────────


def test_alias_base_iri_is_redirected_to_domain_namespace():
    """THE REGRESSION: 로더가 alias 를 고르면 DOMAIN_NS 네임스페이스로 넘긴다."""
    domain_ns = _FakeNamespace(DOMAIN, {"EquipmentMaster"})
    loaded = _FakeOntology(
        "https://w3id.org/steel-ontology-sample#", set(), {DOMAIN: domain_ns},
    )

    ctx = _rule_context(loaded, DOMAIN, ("EquipmentMaster",))

    assert ctx is domain_ns, "alias 온톨로지를 그대로 써서 이름 해석이 실패한다"


def test_redirect_is_measured_not_assumed():
    """DOMAIN_NS 도 그 이름을 모르면 넘기지 않는다 — 근거 없는 교정 금지.

    어휘 부패(hasAlarmEvent 등)로 어디에도 없는 이름이면 옮겨봐야 소용없고, 옮기면
    원인이 "네임스페이스 문제" 로 오독된다.
    """
    domain_ns = _FakeNamespace(DOMAIN, set())
    loaded = _FakeOntology("https://w3id.org/alias#", set(), {DOMAIN: domain_ns})

    assert _rule_context(loaded, DOMAIN, ("hasAlarmEvent",)) is loaded


# ── 보존: 정당한 경우를 건드리지 않는다 (NEGATIVE 방향) ─────────────────


def test_matching_base_iri_is_left_alone():
    """base_iri 가 이미 DOMAIN_NS 면 그대로 쓴다."""
    loaded = _FakeOntology(DOMAIN, {"EquipmentMaster"})
    assert _rule_context(loaded, DOMAIN, ("EquipmentMaster",)) is loaded


def test_foreign_namespace_fixture_is_not_hijacked():
    """다른 NS 그래프를 DOMAIN_NS 로 끌고 가지 않는다.

    실측 파손 사례: ``http://test.org/onto#`` 픽스처(tests/test_swrl_inference.py
    ::test_quality_violation_rule_fires)가 무조건 교정 때문에 깨졌다. 타 도메인
    이식본도 같은 형태다.
    """
    loaded = _FakeOntology(
        "http://test.org/onto#", {"Sample", "carbonContent"},
        {DOMAIN: _FakeNamespace(DOMAIN, set())},
    )

    assert _rule_context(loaded, DOMAIN, ("Sample", "carbonContent")) is loaded


def test_loader_namespace_wins_when_both_resolve():
    """양쪽이 다 풀면 로더 쪽을 존중한다 — 불필요한 이동을 만들지 않는다."""
    domain_ns = _FakeNamespace(DOMAIN, {"Shared"})
    loaded = _FakeOntology("http://other#", {"Shared"}, {DOMAIN: domain_ns})

    assert _rule_context(loaded, DOMAIN, ("Shared",)) is loaded


def test_no_probe_names_leaves_loader_untouched():
    """프로브할 이름이 없으면 판단 근거가 없다 → 현행 유지."""
    loaded = _FakeOntology("http://other#", set(), {DOMAIN: _FakeNamespace(DOMAIN, {"X"})})
    assert _rule_context(loaded, DOMAIN, ()) is loaded


def test_trailing_separator_variants_are_equivalent():
    """``…#`` / ``…/`` / 무접미가 같은 NS 로 취급된다 — 오프바이원 교정 방지."""
    for iri in (DOMAIN, DOMAIN.rstrip("#"), DOMAIN.rstrip("#") + "/"):
        loaded = _FakeOntology(iri, {"EquipmentMaster"})
        assert _rule_context(loaded, DOMAIN, ("EquipmentMaster",)) is loaded


# ── 프로브 이름 추출 ────────────────────────────────────────────────────


def test_predicate_names_are_extracted():
    names = _referenced_names(["EquipmentMaster(?e) ^ hasAlarmEvent(?e, ?a) -> Failure(?e)"])
    assert names == ("EquipmentMaster", "hasAlarmEvent", "Failure")


@pytest.mark.parametrize(
    "builtin", ["DifferentFrom", "greaterThan", "lessThan", "SameAs", "stringConcat"],
)
def test_swrl_builtins_are_excluded(builtin):
    """빌트인은 온톨로지 엔티티가 아니므로 네임스페이스 신호가 아니다.

    포함하면 어느 NS 에서도 안 풀려 교정이 항상 무발화한다.
    """
    names = _referenced_names([f"Cls(?x) ^ {builtin}(?x, ?y) -> Out(?x)"])
    assert builtin not in names


def test_duplicate_names_appear_once_in_order():
    """중복 제거 + 등장 순서 유지 — 프로브 비용과 결정성 둘 다 필요하다."""
    names = _referenced_names([
        "A(?x) ^ p(?x, ?y) -> B(?x)",
        "A(?z) ^ p(?z, ?w) -> C(?z)",
    ])
    assert names == ("A", "p", "B", "C")


def test_variables_are_not_mistaken_for_predicates():
    """``?e`` 같은 변수는 술어가 아니다."""
    assert "e" not in _referenced_names(["Cls(?e) -> Out(?e)"])


# ── 실측: 배포 산출물에서 실제로 발화하는가 ────────────────────────────


def test_deployed_inferred_graph_exposes_the_alias_ontology():
    """추론 그래프에 alias owl:Ontology 가 실체화돼 있는가.

    합성 픽스처만으로는 "실물에서 발화하는가" 를 알 수 없다. 이 결함은 T-Box 의
    ``owl:sameAs`` 한 줄이 추론을 거쳐 완전한 온톨로지 노드가 되는 경로였다.
    """
    import pathlib

    from rdflib import OWL, RDF, Graph

    path = pathlib.Path("data/generated/inferred/all_inferred.ttl")
    if not path.exists():
        pytest.skip("추론 산출물 없음")

    graph = Graph()
    graph.parse(str(path), format="turtle")
    declared = {str(s) for s in graph.subjects(RDF.type, OWL.Ontology)}

    assert len(declared) > 1, (
        f"owl:Ontology 가 {len(declared)}개다 — 로더의 base_iri 선택이 모호해지는 "
        "조건이 사라졌다면 이 게이트의 전제를 재확인할 것"
    )
