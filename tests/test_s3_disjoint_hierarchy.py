"""S3 계층 생성이 unsatisfiable 을 만들지 않는다 (보존 방향 우선)."""
from __future__ import annotations

import contextlib

import rdflib.collection
from rdflib import OWL, RDF, RDFS, Graph, URIRef

from domain.graph_utils import would_violate_disjoint
from domain.namespaces import DOMAIN_NS, NS_PREFIX

_HDR = (
    f"@prefix {NS_PREFIX}: <{DOMAIN_NS}> .\n"
    "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
    "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
)
def D(name: str) -> URIRef:
    return URIRef(DOMAIN_NS + name)


def _graph_with_disjoint(*members: str) -> Graph:
    body = "".join(f"{NS_PREFIX}:{m} a owl:Class .\n" for m in members)
    g = Graph()
    g.parse(data=_HDR + body, format="turtle")
    from rdflib import BNode
    b = BNode()
    g.add((b, RDF.type, OWL.AllDisjointClasses))
    root = BNode()
    rdflib.collection.Collection(g, root, [D(m) for m in members])
    g.add((b, OWL.members, root))
    return g


def test_detects_conflict_through_ancestors():
    """THE REGRESSION: 조상을 거쳐 disjoint 두 그룹에 걸리면 잡아낸다.

    실측 (2026-08-11 S3): FailureCause 가 EquipmentEvent(→EquipmentManagement)
    와 MaintenanceManagement 양쪽을 상속해 **unsatisfiable** 이 됐다. HermiT 가
    3개 클래스를 그렇게 판정했고, 그 클래스는 A-Box 인스턴스를 가질 수 없어
    해당 CSV 테이블이 KG 에서 사라진다.
    """
    g = _graph_with_disjoint("EquipmentManagement", "MaintenanceManagement")
    g.parse(data=_HDR + (
        f"{NS_PREFIX}:EquipmentEvent a owl:Class ; "
        f"rdfs:subClassOf {NS_PREFIX}:EquipmentManagement .\n"
        f"{NS_PREFIX}:FailureCause a owl:Class ; "
        f"rdfs:subClassOf {NS_PREFIX}:EquipmentEvent .\n"
    ), format="turtle")
    conflict = would_violate_disjoint(g, D("FailureCause"), D("MaintenanceManagement"))
    assert conflict == ("EquipmentManagement", "MaintenanceManagement"), conflict


def test_allows_a_legitimate_new_parent():
    """PRESERVATION: 충돌이 없으면 계층 추가를 막지 않는다."""
    g = _graph_with_disjoint("GroupA", "GroupB")
    g.parse(data=_HDR + f"{NS_PREFIX}:Leaf a owl:Class .\n", format="turtle")
    assert would_violate_disjoint(g, D("Leaf"), D("GroupA")) is None


def test_no_disjoint_declarations_means_no_conflict():
    """PRESERVATION: disjoint 선언이 없으면 항상 허용한다."""
    g = Graph()
    g.parse(data=_HDR + (
        f"{NS_PREFIX}:A a owl:Class .\n{NS_PREFIX}:B a owl:Class .\n"
    ), format="turtle")
    assert would_violate_disjoint(g, D("A"), D("B")) is None


def test_s3_produces_no_unsatisfiable_hierarchy():
    """S3 전체가 disjoint 충돌 클래스를 만들지 않는다 (결과 기반 회귀).

    특정 스텝이 아니라 **최종 산출물** 을 검사하므로, 같은 결함이 다른 스텝에서
    새로 생겨도 잡힌다.
    """
    from tools.ontology_quality import improve_tbox

    ttl = _HDR + (
        f"{NS_PREFIX}:EquipmentMaster a owl:Class .\n"
        f"{NS_PREFIX}:TagMaster a owl:Class .\n"
        f"{NS_PREFIX}:MaintenanceHistory a owl:Class .\n"
        f"{NS_PREFIX}:FailureCause a owl:Class .\n"
        f"{NS_PREFIX}:AlarmEvents a owl:Class .\n"
        f"{NS_PREFIX}:EquipmentStatus a owl:Class .\n"
    )
    improved, _stats = improve_tbox(ttl)
    g = Graph()
    g.parse(data=improved, format="turtle")

    groups: list[set[str]] = []
    def local(n):
        return str(n).split("#")[-1].split("/")[-1]
    for b in g.subjects(RDF.type, OWL.AllDisjointClasses):
        mn = list(g.objects(b, OWL.members))
        if mn:
            with contextlib.suppress(Exception):
                groups.append({local(m) for m in rdflib.collection.Collection(g, mn[0])})

    def anc(c, seen=frozenset()):
        out = set()
        for p in g.objects(c, RDFS.subClassOf):
            if isinstance(p, URIRef) and p not in seen:
                out.add(local(p))
                out |= anc(p, seen | {p})
        return out

    bad = []
    for c in g.subjects(RDF.type, OWL.Class):
        if not str(c).startswith(DOMAIN_NS):
            continue
        a = anc(c) | {local(c)}
        for grp in groups:
            if len(grp & a) >= 2:
                bad.append((local(c), sorted(grp & a)))
                break
    assert not bad, f"S3 가 unsatisfiable 클래스를 만들었다: {bad}"


def test_declared_parent_child_is_not_declared_disjoint():
    """설정이 부모-자식으로 지정한 쌍을 disjoint 로 선언하면 계층이 막힌다.

    step_01 은 step_14 보다 먼저 도므로 그래프에는 아직 그 계층이 없다. 그래프만
    보고 형제로 판단하면 disjoint 를 선언해 서브그룹 생성을 **영구히** 차단한다
    (실측: EquipmentAsset 와 그 자식 EquipmentMaster/TagMaster).
    """
    from tools.ontology_quality import _load_sub_group_config, improve_tbox

    declared: set[frozenset[str]] = set()
    for subgroups in _load_sub_group_config().values():
        for sg in subgroups:
            for child in sg.get("children") or []:
                declared.add(frozenset({sg["class"], child}))

    ttl = _HDR + "".join(
        f"{NS_PREFIX}:{c} a owl:Class .\n"
        for pair in declared for c in pair
    )
    improved, _ = improve_tbox(ttl)
    g = Graph()
    g.parse(data=improved, format="turtle")
    def local(n):
        return str(n).split("#")[-1].split("/")[-1]

    violations = []
    for b in g.subjects(RDF.type, OWL.AllDisjointClasses):
        mn = list(g.objects(b, OWL.members))
        if not mn:
            continue
        try:
            members = {local(m) for m in rdflib.collection.Collection(g, mn[0])}
        except Exception:  # noqa: BLE001 — 파싱 불가 그룹은 건너뛴다
            continue
        for pair in declared:
            if pair <= members:
                violations.append(sorted(pair))
    assert not violations, f"부모-자식 쌍이 disjoint 로 선언됐다: {violations}"


# ── 스텝별 가드 (결과 기반 테스트가 못 잡는 부분) ────────────────────
#
# step_01 수정만으로도 현 설정에서는 충돌이 해소되므로, 결과 기반 테스트는
# step_12/14 의 가드를 제거해도 초록이다 (mutation 으로 확인). 두 스텝은 설정과
# 무관하게 유입되는 계층(수동 추가 TTL, Jury 지시, 외부 병합)에도 노출되므로
# 가드를 개별로 고정한다.


def _stub_csv_dir(monkeypatch, filenames: list[str]) -> None:
    """step_12 는 SOURCE_RAWDATA_DIR 의 CSV 이름으로 자식을 필터링한다.

    실제 CSV 40개를 스캔하면 픽스처의 가짜 테이블이 "CSV 자식 없음" 으로 스킵돼
    가드가 실행되지 않는다. 픽스처 이름만 보이도록 임시 디렉토리로 바꾼다.
    """
    import tempfile
    from pathlib import Path

    import config

    tmp = tempfile.mkdtemp()
    for name in filenames:
        Path(tmp, name).write_text("col\n1\n", encoding="utf-8")
    monkeypatch.setattr(config, "SOURCE_RAWDATA_DIR", tmp)


def _disjoint_pair_graph(a: str, b: str) -> Graph:
    """``a ⊥ b`` 만 선언된 최소 그래프."""
    from rdflib import BNode

    g = Graph()
    g.parse(data=_HDR + (
        f"{NS_PREFIX}:{a} a owl:Class .\n{NS_PREFIX}:{b} a owl:Class .\n"
    ), format="turtle")
    node = BNode()
    g.add((node, RDF.type, OWL.AllDisjointClasses))
    root = BNode()
    rdflib.collection.Collection(g, root, [D(a), D(b)])
    g.add((node, OWL.members, root))
    return g


def test_step_12_refuses_a_conflicting_parent(monkeypatch):
    """중간 추상 클래스 스텝이 disjoint 충돌 부모를 붙이지 않는다."""
    from tools.quality_steps import step_12_intermediate_abstract as step12
    from tools.quality_steps._base import StepContext

    g = _disjoint_pair_graph("GroupA", "GroupB")
    g.parse(data=_HDR + (
        f"{NS_PREFIX}:Leaf a owl:Class ; rdfs:subClassOf {NS_PREFIX}:GroupA .\n"
    ), format="turtle")

    _stub_csv_dir(monkeypatch, ["Leaf.csv"])
    monkeypatch.setattr(
        "tools.ontology_quality._load_hierarchy_config",
        lambda: {"테스트": {"class": "GroupB", "parent": DOMAIN_NS + "GroupB",
                          "label_en": "B", "label_ko": "B", "comment_ko": "B",
                          "children_tables": ["Leaf"]}},
    )
    res = step12.apply(g, StepContext(domain_ns=DOMAIN_NS))
    parents = {str(p) for p in g.objects(D("Leaf"), RDFS.subClassOf)}
    assert DOMAIN_NS + "GroupB" not in parents, "충돌 부모를 붙였다"
    assert res.stats["intermediate_disjoint_conflicts_skipped"] == 1, res.stats


def test_step_12_still_adds_a_safe_parent(monkeypatch):
    """PRESERVATION: 충돌이 없으면 중간 부모를 정상 추가한다."""
    from tools.quality_steps import step_12_intermediate_abstract as step12
    from tools.quality_steps._base import StepContext

    g = Graph()
    g.parse(data=_HDR + f"{NS_PREFIX}:Leaf a owl:Class .\n", format="turtle")
    _stub_csv_dir(monkeypatch, ["Leaf.csv"])
    monkeypatch.setattr(
        "tools.ontology_quality._load_hierarchy_config",
        lambda: {"테스트": {"class": "SafeGroup", "parent": DOMAIN_NS + "SafeGroup",
                          "label_en": "S", "label_ko": "S", "comment_ko": "S",
                          "children_tables": ["Leaf"]}},
    )
    step12.apply(g, StepContext(domain_ns=DOMAIN_NS))
    parents = {str(p) for p in g.objects(D("Leaf"), RDFS.subClassOf)}
    assert DOMAIN_NS + "SafeGroup" in parents, parents


def test_step_14_refuses_a_conflicting_subgroup(monkeypatch):
    """서브그룹 스텝도 같은 가드를 갖는다."""
    from tools.quality_steps import step_14_subgroup_creation as step14
    from tools.quality_steps._base import StepContext

    g = _disjoint_pair_graph("GroupA", "GroupB")
    g.parse(data=_HDR + (
        f"{NS_PREFIX}:Abstract a owl:Class .\n"
        f"{NS_PREFIX}:SubGrp a owl:Class ; rdfs:subClassOf {NS_PREFIX}:GroupB .\n"
        f"{NS_PREFIX}:Leaf a owl:Class ; rdfs:subClassOf {NS_PREFIX}:GroupA .\n"
    ), format="turtle")
    monkeypatch.setattr(
        "tools.ontology_quality._load_sub_group_config",
        lambda: {"Abstract": [{"class": "SubGrp", "label_en": "S",
                               "label_ko": "S", "comment_ko": "S",
                               "children": ["Leaf"]}]},
    )
    res = step14.apply(g, StepContext(domain_ns=DOMAIN_NS))
    parents = {str(p) for p in g.objects(D("Leaf"), RDFS.subClassOf)}
    assert DOMAIN_NS + "SubGrp" not in parents, "충돌 서브그룹을 붙였다"
    assert res.stats["subgroup_disjoint_conflicts_skipped"] == 1, res.stats


def test_step_14_still_moves_a_safe_child(monkeypatch):
    """PRESERVATION: 충돌이 없으면 서브그룹으로 정상 이동한다."""
    from tools.quality_steps import step_14_subgroup_creation as step14
    from tools.quality_steps._base import StepContext

    g = Graph()
    g.parse(data=_HDR + (
        f"{NS_PREFIX}:Abstract a owl:Class .\n"
        f"{NS_PREFIX}:Leaf a owl:Class ; rdfs:subClassOf {NS_PREFIX}:Abstract .\n"
    ), format="turtle")
    monkeypatch.setattr(
        "tools.ontology_quality._load_sub_group_config",
        lambda: {"Abstract": [{"class": "SafeSub", "label_en": "S",
                               "label_ko": "S", "comment_ko": "S",
                               "children": ["Leaf"]}]},
    )
    step14.apply(g, StepContext(domain_ns=DOMAIN_NS))
    parents = {str(p) for p in g.objects(D("Leaf"), RDFS.subClassOf)}
    assert DOMAIN_NS + "SafeSub" in parents, parents
