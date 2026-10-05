"""방향 소멸 subPropertyOf 사후 정리 (step_22g) 회귀 가드.

배경 (2026-08-17 실측): `_consolidate_duplicate_ops` 가 서로 ``owl:inverseOf`` 인
OP 들을 공통 부모로 묶어 공정 흐름의 방향을 소멸시켰다:

    followedBy ⊑ directlyFollows, directlyPrecedes, precededBy
    directlyFollows owl:inverseOf directlyPrecedes
    precededBy      owl:inverseOf followedBy

tacit 의 정방향 3건(고로→제강→연주→압연)만 넣어도 추론 후 양방향이 파생돼
`all_inferred.ttl` 에 ``ProcessSteelmakingFurnace directlyPrecedes
ProcessBlastFurnace`` 가 실재했다. HermiT consistent=true / SHACL 위반 0 /
check_quality_rules 101 이슈 중 언급 0건 — 어떤 게이트도 잡지 못했다.

`_consolidate_duplicate_ops` 의 가드는 **신규 추가만** 막는다. 이미 T-Box 에
박힌 링크는 S3 재실행으로 사라지지 않는다 (실측: 재실행 전후 부모 3개 동일).
그래서 사후 정리 스텝이 따로 필요하다 — step_22b(cycle breaker) 와 같은 역할.

이 파일의 주장은 **산출물** 기준이다: 카운터가 아니라 추론 후 방향이 구별되는가.
"""
from __future__ import annotations

from rdflib import OWL, RDF, RDFS, Literal, URIRef

from domain.tbox_utils import _new_graph
from tools.ontology_quality import _repair_direction_collapse

STEEL = "http://example.com/steel-ontology#"

FORWARD = {STEEL + "followedBy", STEEL + "directlyPrecedes"}
BACKWARD = {STEEL + "precededBy", STEEL + "directlyFollows"}


def _op(g, name, domain="ManufacturingProcessStep", rng="ManufacturingProcessStep",
        labels=()):
    u = URIRef(STEEL + name)
    g.add((u, RDF.type, OWL.ObjectProperty))
    g.add((u, RDFS.domain, URIRef(STEEL + domain)))
    g.add((u, RDFS.range, URIRef(STEEL + rng)))
    for lab in labels:
        g.add((u, RDFS.label, Literal(lab, lang="en")))
    return u


def _damaged_graph():
    """실측된 손상 상태를 그대로 재현 (t_box.ttl:3071-3074)."""
    g = _new_graph()
    g.add((URIRef(STEEL + "ManufacturingProcessStep"), RDF.type, OWL.Class))
    for name in ("followedBy", "precededBy", "directlyFollows", "directlyPrecedes"):
        _op(g, name, labels=[name])
    for x, y in (("followedBy", "precededBy"), ("directlyFollows", "directlyPrecedes")):
        g.add((URIRef(STEEL + x), OWL.inverseOf, URIRef(STEEL + y)))
        g.add((URIRef(STEEL + y), OWL.inverseOf, URIRef(STEEL + x)))
    for parent in ("directlyFollows", "directlyPrecedes", "precededBy"):
        g.add((URIRef(STEEL + "followedBy"), RDFS.subPropertyOf,
               URIRef(STEEL + parent)))
    return g


def _plain_copy(src):
    """owlrl 은 리터럴 subject 트리플을 만들어 Oxigraph store 가 거부한다."""
    from rdflib import Graph as PlainGraph
    g = PlainGraph()
    for t in src:
        g.add(t)
    return g


# --------------------------------------------------------------------------
# 정리 동작
# --------------------------------------------------------------------------

def test_self_inverse_parent_removed():
    """P ⊑ inv(P) 는 P 를 대칭으로 만든다 — 제거해야 한다."""
    g = _damaged_graph()
    stats = _repair_direction_collapse(g, STEEL)

    parents = set(g.objects(URIRef(STEEL + "followedBy"), RDFS.subPropertyOf))
    assert URIRef(STEEL + "precededBy") not in parents
    assert stats["direction_collapse_repaired"] >= 1


def test_inverse_pair_parents_reduced_to_one():
    """서로 inverseOf 인 두 부모가 동시에 남으면 안 된다 (둘 중 하나만)."""
    g = _damaged_graph()
    _repair_direction_collapse(g, STEEL)

    for child in set(g.subjects(RDFS.subPropertyOf, None)):
        parents = [o for o in g.objects(child, RDFS.subPropertyOf)
                   if isinstance(o, URIRef)]
        for i, a in enumerate(parents):
            for b in parents[i + 1:]:
                assert (a, OWL.inverseOf, b) not in g, (
                    f"{child.split('#')[-1]} 이 서로 역인 {a.split('#')[-1]} / "
                    f"{b.split('#')[-1]} 를 동시에 부모로 갖는다"
                )


def test_direction_survives_inference_after_repair():
    """산출물 검증: 정리 후 추론에서 두 방향이 구별된다.

    정방향 술어는 A→B 에만, 역방향 술어는 B→A 에만 나타나야 한다.
    """
    g = _damaged_graph()
    _repair_direction_collapse(g, STEEL)
    h = _plain_copy(g)

    a, b = URIRef(STEEL + "A"), URIRef(STEEL + "B")
    step = URIRef(STEEL + "ManufacturingProcessStep")
    h.add((a, RDF.type, step))
    h.add((b, RDF.type, step))
    h.add((a, URIRef(STEEL + "followedBy"), b))

    from owlrl import DeductiveClosure, OWLRL_Semantics
    DeductiveClosure(OWLRL_Semantics).expand(h)

    def _preds(subj, obj, group):
        return {str(p).split("#")[-1] for s, p, o in h
                if s == subj and o == obj and str(p) in group}

    assert _preds(a, b, FORWARD), "정방향 술어가 A→B 에서 사라졌다"
    assert not _preds(b, a, FORWARD), (
        f"정방향 술어가 B→A 로 파생됨: {sorted(_preds(b, a, FORWARD))}"
    )
    assert not _preds(a, b, BACKWARD), (
        f"역방향 술어가 A→B 로 파생됨: {sorted(_preds(a, b, BACKWARD))}"
    )


# --------------------------------------------------------------------------
# NEGATIVE 방향 — 정당한 입력을 보존하는가
# --------------------------------------------------------------------------

def test_unrelated_subproperty_preserved():
    """방향과 무관한 subPropertyOf 는 건드리지 않는다."""
    g = _new_graph()
    g.add((URIRef(STEEL + "Equipment"), RDF.type, OWL.Class))
    child = _op(g, "hasMainEquipment", "Process", "Equipment", ["has main equipment"])
    parent = _op(g, "hasEquipment", "Process", "Equipment", ["has equipment"])
    g.add((child, RDFS.subPropertyOf, parent))

    stats = _repair_direction_collapse(g, STEEL)

    assert (child, RDFS.subPropertyOf, parent) in g, "정당한 계층이 삭제됐다"
    assert stats["direction_collapse_repaired"] == 0


def test_single_inverse_parent_preserved():
    """부모가 하나뿐이고 자기 역관계가 아니면 보존한다.

    이 경우는 방향이 소멸하지 않는다 — 과잉 삭제하면 정당한 계층을 잃는다.
    """
    g = _new_graph()
    for name in ("followedBy", "precededBy", "directlyPrecedes", "directlyFollows"):
        _op(g, name, labels=[name])
    g.add((URIRef(STEEL + "followedBy"), OWL.inverseOf, URIRef(STEEL + "precededBy")))
    g.add((URIRef(STEEL + "directlyPrecedes"), OWL.inverseOf,
           URIRef(STEEL + "directlyFollows")))
    # followedBy ⊑ directlyPrecedes — 방향이 일치하는 정당한 계층
    g.add((URIRef(STEEL + "followedBy"), RDFS.subPropertyOf,
           URIRef(STEEL + "directlyPrecedes")))

    stats = _repair_direction_collapse(g, STEEL)

    assert (URIRef(STEEL + "followedBy"), RDFS.subPropertyOf,
            URIRef(STEEL + "directlyPrecedes")) in g
    assert stats["direction_collapse_repaired"] == 0


def test_repair_is_idempotent():
    """두 번 돌려도 같은 결과 — 두 번째는 아무것도 하지 않는다."""
    g = _damaged_graph()
    first = _repair_direction_collapse(g, STEEL)
    snapshot = set(g)
    second = _repair_direction_collapse(g, STEEL)

    assert first["direction_collapse_repaired"] > 0
    assert second["direction_collapse_repaired"] == 0
    assert set(g) == snapshot


def test_inverse_declarations_not_removed():
    """owl:inverseOf 자체는 지우지 않는다 — 그것이 방향의 근거다."""
    g = _damaged_graph()
    _repair_direction_collapse(g, STEEL)

    assert (URIRef(STEEL + "followedBy"), OWL.inverseOf,
            URIRef(STEEL + "precededBy")) in g
    assert (URIRef(STEEL + "directlyFollows"), OWL.inverseOf,
            URIRef(STEEL + "directlyPrecedes")) in g


def test_kept_parent_matches_direction_when_labels_allow():
    """부모를 하나 남길 때 **방향이 맞는** 쪽을 고른다.

    followedBy 는 주어가 앞에 오므로 directlyPrecedes("바로 앞에") 가 맞고
    directlyFollows("바로 뒤에") 는 반대다. 둘 중 아무거나 남기면 절반의 확률로
    의미가 뒤집힌다 (대칭은 아니어서 다른 검사는 통과한다).
    """
    g = _new_graph()
    g.add((URIRef(STEEL + "ManufacturingProcessStep"), RDF.type, OWL.Class))
    _op(g, "followedBy", labels=['"followed by"'])
    _op(g, "precededBy", labels=['"preceded by"'])
    _op(g, "directlyFollows", labels=['"directly follows"', '"직접 후행"'])
    _op(g, "directlyPrecedes", labels=['"directly precedes"', '"직접 선행"'])
    for x, y in (("followedBy", "precededBy"), ("directlyFollows", "directlyPrecedes")):
        g.add((URIRef(STEEL + x), OWL.inverseOf, URIRef(STEEL + y)))
        g.add((URIRef(STEEL + y), OWL.inverseOf, URIRef(STEEL + x)))
    g.add((URIRef(STEEL + "followedBy"), RDFS.subPropertyOf,
           URIRef(STEEL + "directlyFollows")))
    g.add((URIRef(STEEL + "followedBy"), RDFS.subPropertyOf,
           URIRef(STEEL + "directlyPrecedes")))

    _repair_direction_collapse(g, STEEL)

    parents = {str(o).split("#")[-1]
               for o in g.objects(URIRef(STEEL + "followedBy"), RDFS.subPropertyOf)}
    assert parents == {"directlyPrecedes"}, (
        f"방향이 맞는 부모를 남기지 않았다: {parents}"
    )


def test_registered_in_pipeline():
    """스텝이 파이프라인에 실제로 등록됐는가 (등록 누락은 조용한 no-op)."""
    from tools.quality_steps import _POST_STEPS, step_22g_direction_collapse

    assert step_22g_direction_collapse.apply in _POST_STEPS
    names = [f.__module__ for f in _POST_STEPS]
    # step_22 (손상 생성 지점) 뒤에 와야 정리가 의미 있다.
    assert names.index("tools.quality_steps.step_22g_direction_collapse") > \
        names.index("tools.quality_steps.step_22_dup_op")
