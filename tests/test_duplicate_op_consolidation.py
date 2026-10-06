"""같은 (domain, range) 중복 ObjectProperty 통합 회귀 가드.

배경 (2026-07-26 실측): T-Box 재생성 시 Multi-Agent 가 같은 사실을 관점만 달리해
여러 이름으로 선언한다. `domain=MaterialA, range=ProcessStepA` 인 OP 가 **7개** 생성됐고,
주석을 보면 넷 다 같은 것을 설명한다:

    heatMaterialRecordHasBofHeat      "중간재 실적이 특정 전로 중간재 배치를 대상으로 함"
    steelmakingResultRefersToBofHeat  "1차 공정 소재 실적이 특정 전로 중간재 히트를 참조함"
    isMaterialAProducedByProcessStepA           "산출물이 공정 단계로부터 생산된 관계"
    processStepAProducesMaterialAInverse           "공정 단계가 산출물을 생산 (역방향 표기)"

CSV 에는 FK 컬럼(`KEY_COL_4`)이 하나뿐이라 A-Box 는 그중 하나만 채운다. 나머지는
값 0건 빈 관계로 남고, 질의가 빈 쪽을 고르면 "0건" 이 정답처럼 반환된다.

`_consolidate_duplicate_ops` 가 이들을 subPropertyOf 로 묶어 추론으로 메우는데,
**방향성 토큰 판정에 오탐이 있어 일부가 통합에서 빠졌다**:
``steelmakingResultRefersToBofHeat`` 는 라벨의 전치사 ``refers to`` 때문에 "to"
방향 토큰 보유로 판정돼 홀로 분리됐다. 대립 짝(origin↔destination 류)이 없는데도
갈라진 것이다.

수정: 대립하는 토큰이 **양쪽에 실제로 존재할 때만** 방향별로 분리한다.
"""
from __future__ import annotations

from rdflib import OWL, RDF, RDFS, Literal, URIRef

from domain.tbox_utils import _new_graph
from tools.ontology_quality import (
    _consolidate_duplicate_ops,
    _directional_tokens_in_labels,
    _has_opposing_directions,
)

STEEL = "http://example.com/steel-ontology#"


def _op(g, name, domain, rng, labels=()):
    u = URIRef(STEEL + name)
    g.add((u, RDF.type, OWL.ObjectProperty))
    g.add((u, RDFS.domain, URIRef(STEEL + domain)))
    g.add((u, RDFS.range, URIRef(STEEL + rng)))
    for lab in labels:
        g.add((u, RDFS.label, Literal(lab, lang="en")))
    return u


# --------------------------------------------------------------------------
# _has_opposing_directions
# --------------------------------------------------------------------------

def test_opposing_pair_detected():
    """origin ↔ destination 처럼 실제 대립하면 True."""
    assert _has_opposing_directions(
        [frozenset({"origin"}), frozenset({"destination"})]) is True


def test_single_sided_token_is_not_opposition():
    """THE REGRESSION: 한쪽에만 토큰이 있으면 대립이 아니다.

    ``refers to`` 의 전치사 "to" 가 여기 해당한다.
    """
    assert _has_opposing_directions(
        [frozenset({"to"}), frozenset(), frozenset(), frozenset()]) is False


def test_no_tokens_is_not_opposition():
    assert _has_opposing_directions([frozenset(), frozenset()]) is False


def test_same_token_on_both_sides_is_not_opposition():
    """둘 다 같은 쪽 토큰이면 구분 근거가 없다."""
    assert _has_opposing_directions(
        [frozenset({"from"}), frozenset({"from"})]) is False


def test_korean_opposites_detected():
    assert _has_opposing_directions(
        [frozenset({"입력"}), frozenset({"출력"})]) is True


# --------------------------------------------------------------------------
# _consolidate_duplicate_ops — end to end
# --------------------------------------------------------------------------

def test_preposition_to_does_not_escape_consolidation():
    """THE REGRESSION: 'refers to' 라벨 OP 도 같은 그룹으로 통합되어야 한다."""
    g = _new_graph()
    for cls in ("MaterialA", "ProcessStepA"):
        g.add((URIRef(STEEL + cls), RDF.type, OWL.Class))
    _op(g, "isMaterialAProducedByProcessStepA", "MaterialA", "ProcessStepA",
        ["is materialA produced by processStepA"])
    _op(g, "steelmakingResultRefersToBofHeat", "MaterialA", "ProcessStepA",
        ["steelmaking result refers to BOF heat"])   # 전치사 to
    _op(g, "isSlabOf", "MaterialA", "ProcessStepA", ["isSlabOf"])

    stats = _consolidate_duplicate_ops(g, STEEL)

    assert stats["duplicate_op_groups_processed"] == 1
    # 3개가 한 그룹 → canonical 1 + subPropertyOf 2
    assert stats["duplicate_ops_consolidated"] == 2
    assert stats["duplicate_ops_skipped_directional"] == 0
    # 통합 결과가 subPropertyOf 체인으로 연결됐는지
    linked = {
        str(o).split("#")[-1]
        for s, o in g.subject_objects(RDFS.subPropertyOf)
        if str(s).startswith(STEEL)
    }
    assert "steelmakingResultRefersToBofHeat" in linked or \
        "steelmakingResultRefersToBofHeat" in {
            str(s).split("#")[-1]
            for s, _ in g.subject_objects(RDFS.subPropertyOf)}


def test_genuine_opposites_still_split():
    """반대 방향 가드: origin/destination 은 계속 별개 의미로 분리."""
    g = _new_graph()
    for cls in ("Shipment", "Warehouse"):
        g.add((URIRef(STEEL + cls), RDF.type, OWL.Class))
    _op(g, "hasOriginWarehouse", "Shipment", "Warehouse", ["has origin warehouse"])
    _op(g, "hasDestinationWarehouse", "Shipment", "Warehouse",
        ["has destination warehouse"])

    stats = _consolidate_duplicate_ops(g, STEEL)

    # 방향이 대립하므로 통합하지 않는다 (출발지 = 도착지로 뭉치면 데이터 오염)
    assert stats["duplicate_ops_consolidated"] == 0


def test_single_op_group_untouched():
    """중복이 아니면 아무것도 하지 않는다."""
    g = _new_graph()
    for cls in ("MaterialA", "ProcessStepA"):
        g.add((URIRef(STEEL + cls), RDF.type, OWL.Class))
    _op(g, "isSlabOf", "MaterialA", "ProcessStepA", ["isSlabOf"])
    stats = _consolidate_duplicate_ops(g, STEEL)
    assert stats["duplicate_op_groups_processed"] == 0
    assert stats["duplicate_ops_consolidated"] == 0


def test_seven_way_duplicate_all_consolidated():
    """실측 사고 재현 — 7개 전부 한 그룹으로 통합."""
    g = _new_graph()
    for cls in ("MaterialA", "ProcessStepA"):
        g.add((URIRef(STEEL + cls), RDF.type, OWL.Class))
    names = [
        ("isMaterialAProducedByProcessStepA", ["is materialA produced by processStepA"]),
        ("isCastingRecordOf", ["isCastingRecordOf"]),
        ("heatMaterialRecordHasBofHeat", ["heat material record has BOF heat"]),
        ("steelmakingResultRefersToBofHeat", ["steelmaking result refers to BOF heat"]),
        ("isSlabOf", ["isSlabOf"]),
        ("isProducedBy", ["isProducedBy"]),
        ("processStepAProducesMaterialAInverse", ["processStepA inverse of materialA"]),
    ]
    for n, labs in names:
        _op(g, n, "MaterialA", "ProcessStepA", labs)

    stats = _consolidate_duplicate_ops(g, STEEL)

    assert stats["duplicate_op_groups_processed"] == 1
    assert stats["duplicate_ops_consolidated"] == 6      # 7 - canonical
    assert stats["duplicate_ops_skipped_directional"] == 0


def test_directional_token_extraction_unchanged():
    """토큰 추출 자체는 기존 동작 유지 (부분문자열) — 판정만 대립 기준으로 바뀜."""
    g = _new_graph()
    u = _op(g, "x", "A", "B", ["steelmaking result refers to BOF heat"])
    assert "to" in _directional_tokens_in_labels(g, u)


# ── owl:inverseOf 는 방향 대립의 **형식적** 증거다 ────────────────────────
#
# 실측 손상 (2026-08-17): 공정 흐름 4개 OP 는 전부 domain=range=
# ManufacturingProcessStep 이라 같은 (domain, range) 버킷에 들어가고, 라벨에
# origin/destination 류 방향 토큰이 **없다**. 그래서 `_has_opposing_directions`
# 는 이들을 갈라내지 못하고 canonical(followedBy) 을 나머지 셋의 subPropertyOf
# 로 묶었다. 그 셋 안에는 서로 inverseOf 인 쌍이 있다:
#
#     followedBy ⊑ directlyFollows, directlyPrecedes, precededBy
#     directlyFollows owl:inverseOf directlyPrecedes
#     precededBy      owl:inverseOf followedBy
#
# 서로 역인 두 프로퍼티의 공통 하위 프로퍼티는 방향을 소멸시킨다. owlrl 로
# 재현하면 `A followedBy B` 하나가 B→A 4종까지 파생한다 — tacit 의 정방향 3건
# (고로→제강→연주→압연) 이 추론 후 양방향으로 붕괴하고, all_inferred.ttl 에
# `ProcessSteelmakingFurnace directlyPrecedes ProcessBlastFurnace` 가 실재했다.
# HermiT consistent=true / SHACL 위반 0 / quality_rules 101 이슈 중 언급 0건 —
# 어떤 게이트도 잡지 못했다.
#
# step_15c 는 equivalentProperty 경로에 같은 가드를 이미 갖고 있었다
# (step_15c_undeclared_op_backfill.py:281-283). subPropertyOf 경로만 비어 있었다.
#
# 아래 테스트는 카운터가 아니라 **산출물** 을 주장한다: 추론 후에도 역방향이
# 파생되지 않는가. 카운터만 보면 "skip 했다" 는 보고와 실제 그래프가 어긋나도
# 통과한다 (이 리포에서 두 번 겪은 실패 방식).

def _flow_graph():
    """공정 흐름 4개 OP — self-referential (domain == range) + inverse 쌍 2개."""
    g = _new_graph()
    step = URIRef(STEEL + "ManufacturingProcessStep")
    g.add((step, RDF.type, OWL.Class))
    for name in ("followedBy", "precededBy", "directlyFollows", "directlyPrecedes"):
        _op(g, name, "ManufacturingProcessStep", "ManufacturingProcessStep", [name])
    g.add((URIRef(STEEL + "followedBy"), OWL.inverseOf, URIRef(STEEL + "precededBy")))
    g.add((URIRef(STEEL + "precededBy"), OWL.inverseOf, URIRef(STEEL + "followedBy")))
    g.add((URIRef(STEEL + "directlyFollows"), OWL.inverseOf,
           URIRef(STEEL + "directlyPrecedes")))
    g.add((URIRef(STEEL + "directlyPrecedes"), OWL.inverseOf,
           URIRef(STEEL + "directlyFollows")))
    return g


def test_inverse_pair_never_becomes_subproperty_siblings():
    """THE REGRESSION: 서로 inverseOf 인 OP 를 공통 부모로 묶지 않는다."""
    g = _flow_graph()
    _consolidate_duplicate_ops(g, STEEL)

    for child, parent in g.subject_objects(RDFS.subPropertyOf):
        inverses = set(g.objects(child, OWL.inverseOf)) | {
            s for s in g.subjects(OWL.inverseOf, child)
        }
        assert parent not in inverses, (
            f"{child.split('#')[-1]} ⊑ {parent.split('#')[-1]} 인데 둘은 "
            "서로 inverseOf — 방향이 소멸한다"
        )


def test_flow_direction_survives_owl_rl_inference():
    """산출물 검증: 추론 후에도 두 방향이 구별되는가.

    카운터 대신 실제 추론 결과를 본다. 술어를 방향으로 나눠서 본다:

    - ``followedBy`` / ``directlyPrecedes`` = "A 가 B 앞에 온다" (forward)
    - ``precededBy`` / ``directlyFollows``  = "A 가 B 뒤에 온다" (backward)

    ``A followedBy B`` 하나를 넣으면 forward 는 A→B 에만, backward 는 B→A 에만
    나타나야 한다. inverse 공리가 만드는 ``B precededBy A`` / ``B directlyFollows
    A`` 는 **정당한** 파생이므로 실패로 세면 안 된다. 붕괴의 증거는 forward
    술어가 B→A 에 나타나는 것(또는 backward 가 A→B 에) 이다 — 실측된
    ``ProcessSteelmakingFurnace directlyPrecedes ProcessBlastFurnace`` 가 이 형태다
    (입력은 ``ProcessBlastFurnace followedBy ProcessSteelmakingFurnace``).
    """
    src = _flow_graph()
    _consolidate_duplicate_ops(src, STEEL)

    # owlrl 은 추론 중 리터럴을 subject 로 쓰는 트리플을 만들어 Oxigraph store 가
    # 거부한다 (_new_graph 의 기본 store). 공리를 plain rdflib Graph 로 옮겨 돌린다.
    from rdflib import Graph as PlainGraph
    g = PlainGraph()
    for t in src:
        g.add(t)

    a, b = URIRef(STEEL + "A"), URIRef(STEEL + "B")
    g.add((a, RDF.type, URIRef(STEEL + "ManufacturingProcessStep")))
    g.add((b, RDF.type, URIRef(STEEL + "ManufacturingProcessStep")))
    g.add((a, URIRef(STEEL + "followedBy"), b))

    from owlrl import DeductiveClosure, OWLRL_Semantics
    DeductiveClosure(OWLRL_Semantics).expand(g)

    forward = {STEEL + "followedBy", STEEL + "directlyPrecedes"}
    backward = {STEEL + "precededBy", STEEL + "directlyFollows"}

    def _preds(subj, obj, group):
        return {str(p).split("#")[-1] for s, p, o in g
                if s == subj and o == obj and str(p) in group}

    # 입력 A followedBy B — forward 는 A→B 에만, backward 는 B→A 에만.
    assert _preds(a, b, forward), "정방향 술어가 A→B 에서 사라졌다"
    bad_forward = _preds(b, a, forward)
    bad_backward = _preds(a, b, backward)
    assert not bad_forward, (
        f"정방향 술어가 B→A 로 파생됨: {sorted(bad_forward)} — 공정 흐름 대칭 붕괴"
    )
    assert not bad_backward, (
        f"역방향 술어가 A→B 로 파생됨: {sorted(bad_backward)} — 공정 흐름 대칭 붕괴"
    )


def test_non_inverse_self_referential_ops_still_consolidate():
    """NEGATIVE 방향: 가드가 정당한 통합까지 막지 않는다.

    domain == range 라는 이유만으로 면제하면 self-referential 중복 OP 가
    영구히 통합되지 않는다. inverse 쌍이 **아닌** 경우는 계속 묶여야 한다.
    """
    g = _new_graph()
    g.add((URIRef(STEEL + "ManufacturingProcessStep"), RDF.type, OWL.Class))
    for name in ("relatedToStep", "linkedToStep", "associatedWithStep"):
        _op(g, name, "ManufacturingProcessStep", "ManufacturingProcessStep", [name])

    stats = _consolidate_duplicate_ops(g, STEEL)

    assert stats["duplicate_op_groups_processed"] == 1
    assert stats["duplicate_ops_consolidated"] == 2, (
        "inverse 쌍이 없는 self-referential 중복은 계속 통합돼야 한다"
    )


def test_flow_parent_choice_is_deterministic():
    """방향 소멸을 막는 것만으로는 부족하다 — 부모 선택이 순서에 좌우돼선 안 된다.

    실측 (2026-08-17): 역관계 가드만 있을 때 4개 흐름 OP 를 24개 입력 순서로
    돌리면 ``followedBy ⊑ directlyFollows`` 와 ``followedBy ⊑ directlyPrecedes``
    가 정확히 12:12 로 갈렸다. 전자는 의미가 반대다 (T-Box 주석: directlyFollows
    = "바로 뒤에" 인데 followedBy 의 주어는 **앞** 에 온다). 대칭은 아니어서
    앞 테스트를 통과하지만, 절반의 확률로 방향이 뒤집힌 계층이 남는다.

    순서 축(precede/follow)이 토큰 사전에 실제로 반영돼 있어야 두 방향이 갈린다.
    """
    import itertools

    labels = {
        "followedBy": ['"followed by"'],
        "precededBy": ['"preceded by"'],
        "directlyFollows": ['"directly follows"', '"직접 후행"'],
        "directlyPrecedes": ['"directly precedes"', '"직접 선행"'],
    }
    outcomes = set()
    for order in itertools.permutations(labels):
        g = _new_graph()
        g.add((URIRef(STEEL + "ManufacturingProcessStep"), RDF.type, OWL.Class))
        for name in order:
            _op(g, name, "ManufacturingProcessStep", "ManufacturingProcessStep",
                labels[name])
        for x, y in (("followedBy", "precededBy"),
                     ("directlyFollows", "directlyPrecedes")):
            g.add((URIRef(STEEL + x), OWL.inverseOf, URIRef(STEEL + y)))
            g.add((URIRef(STEEL + y), OWL.inverseOf, URIRef(STEEL + x)))
        _consolidate_duplicate_ops(g, STEEL)
        outcomes.add(tuple(sorted(
            f"{str(s).split('#')[-1]}<={str(o).split('#')[-1]}"
            for s, o in g.subject_objects(RDFS.subPropertyOf)
        )))
    assert len(outcomes) == 1, f"입력 순서에 따라 결과가 갈린다: {sorted(outcomes)}"
    # 방향이 반대인 부모는 어느 순서에서도 생기지 않는다.
    assert outcomes == {()}, f"흐름 OP 에 부모가 생겼다: {sorted(outcomes)}"


def test_ordering_tokens_are_extracted_from_real_labels():
    """토큰 사전이 축에서 파생되는지 — 축만 늘리고 사전을 잊으면 조용히 no-op.

    실측 (2026-08-17): 순서 축을 ``_DIRECTIONAL_AXES`` 에만 추가했을 때 4개 OP
    전부 tokens=∅ 이었다. 사전이 손으로 나열된 별개 리터럴이었기 때문이다.
    """
    g = _new_graph()
    u = _op(g, "directlyPrecedes", "A", "B", ['"directly precedes"', '"직접 선행"'])
    toks = _directional_tokens_in_labels(g, u)
    assert "precedes" in toks and "선행" in toks, f"순서 토큰 미추출: {set(toks)}"
    v = _op(g, "followedBy", "A", "B", ['"followed by"'])
    assert "followed" in _directional_tokens_in_labels(g, v)
    assert _has_opposing_directions(
        [_directional_tokens_in_labels(g, u), _directional_tokens_in_labels(g, v)]
    ) is True


def test_partial_inverse_group_consolidates_the_safe_members():
    """inverse 쌍 하나가 섞여도 무관한 나머지는 통합된다 (전체 포기 금지)."""
    g = _new_graph()
    g.add((URIRef(STEEL + "ManufacturingProcessStep"), RDF.type, OWL.Class))
    for name in ("followedBy", "precededBy", "relatedToStep"):
        _op(g, name, "ManufacturingProcessStep", "ManufacturingProcessStep", [name])
    g.add((URIRef(STEEL + "followedBy"), OWL.inverseOf, URIRef(STEEL + "precededBy")))
    g.add((URIRef(STEEL + "precededBy"), OWL.inverseOf, URIRef(STEEL + "followedBy")))

    stats = _consolidate_duplicate_ops(g, STEEL)

    assert stats["duplicate_ops_skipped_inverse"] >= 1
    # relatedToStep 은 어느 쪽과도 inverse 가 아니므로 통합 대상이다.
    assert stats["duplicate_ops_consolidated"] >= 1
    for child, parent in g.subject_objects(RDFS.subPropertyOf):
        inverses = set(g.objects(child, OWL.inverseOf)) | {
            s for s in g.subjects(OWL.inverseOf, child)
        }
        assert parent not in inverses


# ── Step 22d 게이트 — 프롬프트 회귀를 결정적으로 드러낸다 ────────────────
#
# 프롬프트(``04-property-rules.md`` 의 "One ObjectProperty per domain and range" 절)는
# LLM 에 대한 "요청" 이므로 지켜지지 않을 수 있고, 실제로 지켜지지 않았다
# (실측: 중복 그룹 35개 / 초과 OP 66개). 게이트가 없으면 다음 재생성에서 같은
# 일이 반복돼도 아무도 모른다.


import pytest  # noqa: E402

from tools.quality_steps import step_22d_duplicate_op_gate as _gate  # noqa: E402


class _Ctx:
    domain_ns = STEEL


def _dup_graph(n=3):
    g = _new_graph()
    for cls in ("MaterialA", "ProcessStepA"):
        g.add((URIRef(STEEL + cls), RDF.type, OWL.Class))
    for i in range(n):
        _op(g, f"relatesToHeat{i}", "MaterialA", "ProcessStepA", [f"relates to heat {i}"])
    return g


def test_gate_counts_redundant_ops():
    stats = _gate.apply(_dup_graph(3), _Ctx()).stats
    assert stats["duplicate_groups"] == 1
    assert stats["redundant_ops"] == 2          # 3 - canonical
    assert stats["gate_passed"] is False


def test_gate_is_read_only():
    """게이트는 그래프를 수정하지 않는다 (Step 22 가 통합 담당)."""
    g = _dup_graph(3)
    before = len(g)
    result = _gate.apply(g, _Ctx())
    assert result.triples_delta == 0
    assert len(g) == before


def test_gate_passes_when_no_duplicates():
    g = _new_graph()
    for cls in ("MaterialA", "ProcessStepA"):
        g.add((URIRef(STEEL + cls), RDF.type, OWL.Class))
    _op(g, "isSlabOf", "MaterialA", "ProcessStepA", ["isSlabOf"])
    stats = _gate.apply(g, _Ctx()).stats
    assert stats["redundant_ops"] == 0
    assert stats["gate_passed"] is True


def test_gate_excludes_genuine_opposites():
    """origin ↔ destination 은 중복이 아니므로 게이트를 통과해야 한다."""
    g = _new_graph()
    for cls in ("Shipment", "Warehouse"):
        g.add((URIRef(STEEL + cls), RDF.type, OWL.Class))
    _op(g, "hasOriginWarehouse", "Shipment", "Warehouse", ["has origin warehouse"])
    _op(g, "hasDestinationWarehouse", "Shipment", "Warehouse",
        ["has destination warehouse"])
    stats = _gate.apply(g, _Ctx()).stats
    assert stats["opposing_groups"] == 1
    assert stats["redundant_ops"] == 0
    assert stats["gate_passed"] is True


def test_gate_fail_mode_raises(monkeypatch):
    monkeypatch.setenv("TBOX_DUP_OP_GATE", "fail")
    with pytest.raises(RuntimeError, match="중복 ObjectProperty"):
        _gate.apply(_dup_graph(3), _Ctx())


def test_gate_warn_mode_does_not_raise(monkeypatch):
    """기본은 warn — 기존 T-Box 가 중복을 갖고 있어 파이프라인을 막지 않는다."""
    monkeypatch.delenv("TBOX_DUP_OP_GATE", raising=False)
    stats = _gate.apply(_dup_graph(3), _Ctx()).stats
    assert stats["gate_mode"] == "warn"
    assert stats["gate_passed"] is False


def test_gate_threshold_configurable(monkeypatch):
    monkeypatch.setenv("TBOX_DUP_OP_MAX_REDUNDANT", "5")
    stats = _gate.apply(_dup_graph(3), _Ctx()).stats
    assert stats["gate_max_redundant"] == 5
    assert stats["gate_passed"] is True         # 초과 2 ≤ 허용 5


def test_gate_skips_multi_domain_ops():
    """domain/range 가 다중·누락인 OP 는 다른 스텝 소관이므로 세지 않는다."""
    g = _new_graph()
    for cls in ("MaterialA", "ProcessStepA", "MaterialB"):
        g.add((URIRef(STEEL + cls), RDF.type, OWL.Class))
    u = _op(g, "multiDomain", "MaterialA", "ProcessStepA", ["multi"])
    g.add((u, RDFS.domain, URIRef(STEEL + "MaterialB")))   # domain 2개
    _op(g, "single", "MaterialA", "ProcessStepA", ["single"])
    stats = _gate.apply(g, _Ctx()).stats
    assert stats["duplicate_groups"] == 0       # 다중 domain OP 제외 → 남은 건 1개


def test_gate_registered_in_pipeline():
    """POST_STEPS 에 등록돼 있어야 실제로 실행된다."""
    from tools.quality_steps import _POST_STEPS
    names = {getattr(f, "__module__", "") for f in _POST_STEPS}
    assert any("step_22d_duplicate_op_gate" in n for n in names)


# ── Step 22e 제거 — 정본 하나만 남긴다 ──────────────────────────────────
#
# Step 22 는 subPropertyOf 로 묶기만 해 중복 66개가 T-Box 에 남았다. 추론을 돌린
# 뒤에만 상위 이름으로 조회가 되고, A-Box 원본만 보면 빈 관계다. 22e 는 실제로
# 삭제해 어휘를 하나로 만든다 — 근거: 36개 그룹 전부 CSV FK 컬럼이 최대 1개이므로
# 그룹 내 OP 는 정의상 동의어다.

from tools.quality_steps import step_22e_duplicate_op_prune as _prune  # noqa: E402


def _named(g, name, domain, rng, labels=(), comment=None):
    u = _op(g, name, domain, rng, labels)
    if comment:
        g.add((u, RDFS.comment, Literal(comment, lang="ko")))
    return u


def _materialA_heat_graph():
    """실측 사례 축약: 이름 품질이 다른 3개."""
    g = _new_graph()
    for cls in ("MaterialA", "ProcessStepA"):
        g.add((URIRef(STEEL + cls), RDF.type, OWL.Class))
    _named(g, "isMaterialAProducedByProcessStepA", "MaterialA", "ProcessStepA",
           ["is materialA produced by processStepA"], "중간 소재가 전로 조업으로부터 생산됨")
    _named(g, "processStepAProducesMaterialAInverse", "MaterialA", "ProcessStepA",
           ["processStepA inverse of materialA"], "산출물이 공정 단계로 생산됨")
    _named(g, "isProducedBy", "MaterialA", "ProcessStepA", ["isProducedBy"])
    return g


def test_prune_is_off_by_default(monkeypatch):
    """기본은 off — 어휘 삭제는 확인 후 켜야 한다."""
    monkeypatch.delenv("TBOX_DUP_OP_PRUNE", raising=False)
    g = _materialA_heat_graph()
    before = len(g)
    result = _prune.apply(g, _Ctx())
    assert result.stats["prune_mode"] == "off"
    assert result.triples_delta == 0
    assert len(g) == before
    assert result.stats["prune_candidates"] == 2       # 계획은 세운다


def test_prune_on_keeps_one_canonical(monkeypatch):
    monkeypatch.setenv("TBOX_DUP_OP_PRUNE", "on")
    g = _materialA_heat_graph()
    _prune.apply(g, _Ctx())
    remaining = {
        str(o).split("#")[-1] for o in g.subjects(RDF.type, OWL.ObjectProperty)
    }
    assert remaining == {"isMaterialAProducedByProcessStepA"}


def test_canonical_prefers_direction_correct_name(monkeypatch):
    """THE RULE: domain→range 순서가 올바른 이름이 정본.

    ``processStepAProducesMaterialAInverse`` 는 주체가 MaterialA 인데 이름이 processStepA 로 시작해
    방향이 거꾸로 읽힌다.
    """
    monkeypatch.setenv("TBOX_DUP_OP_PRUNE", "on")
    g = _materialA_heat_graph()
    stats = _prune.apply(g, _Ctx()).stats
    entry = stats["plan"][0]
    assert entry["keep"] == "isMaterialAProducedByProcessStepA"
    assert "processStepAProducesMaterialAInverse" in entry["remove"]


def test_generic_name_never_canonical(monkeypatch):
    """range 클래스명이 없는 범용 이름(isProducedBy)은 정본이 될 수 없다."""
    monkeypatch.setenv("TBOX_DUP_OP_PRUNE", "on")
    g = _new_graph()
    for cls in ("MaterialA", "ProcessStepA"):
        g.add((URIRef(STEEL + cls), RDF.type, OWL.Class))
    _named(g, "isProducedBy", "MaterialA", "ProcessStepA", ["isProducedBy"])
    _named(g, "materialAUsesBofHeat", "MaterialA", "ProcessStepA", ["materialA uses BOF heat"])
    stats = _prune.apply(g, _Ctx()).stats
    assert stats["plan"][0]["keep"] == "materialAUsesBofHeat"


def test_forced_keep_overrides_rule(monkeypatch):
    """SME 가 정식 이름을 알려주면 규칙을 덮어쓴다."""
    monkeypatch.setenv("TBOX_DUP_OP_PRUNE", "on")
    monkeypatch.setenv("TBOX_DUP_OP_PRUNE_KEEP", "isProducedBy")
    g = _materialA_heat_graph()
    stats = _prune.apply(g, _Ctx()).stats
    assert stats["plan"][0]["keep"] == "isProducedBy"


def test_prune_replaces_onproperty_reference(monkeypatch):
    """Restriction 의 onProperty 가 제거 대상이면 정본으로 치환 (dangling 방지)."""
    monkeypatch.setenv("TBOX_DUP_OP_PRUNE", "on")
    g = _materialA_heat_graph()
    restriction = URIRef(STEEL + "SomeRestriction")
    g.add((restriction, RDF.type, OWL.Restriction))
    g.add((restriction, OWL.onProperty, URIRef(STEEL + "processStepAProducesMaterialAInverse")))

    _prune.apply(g, _Ctx())

    targets = {str(o).split("#")[-1] for o in g.objects(restriction, OWL.onProperty)}
    assert targets == {"isMaterialAProducedByProcessStepA"}


def test_prune_leaves_no_dangling_reference(monkeypatch):
    monkeypatch.setenv("TBOX_DUP_OP_PRUNE", "on")
    g = _materialA_heat_graph()
    victim = URIRef(STEEL + "processStepAProducesMaterialAInverse")
    _prune.apply(g, _Ctx())
    assert not list(g.predicate_objects(victim))
    assert not list(g.subject_predicates(victim))


def test_prune_skips_opposing_direction_groups(monkeypatch):
    """origin ↔ destination 은 의미가 갈리므로 제거하지 않는다."""
    monkeypatch.setenv("TBOX_DUP_OP_PRUNE", "on")
    g = _new_graph()
    for cls in ("Shipment", "Warehouse"):
        g.add((URIRef(STEEL + cls), RDF.type, OWL.Class))
    _named(g, "hasOriginWarehouse", "Shipment", "Warehouse", ["has origin warehouse"])
    _named(g, "hasDestinationWarehouse", "Shipment", "Warehouse",
           ["has destination warehouse"])
    before = len(g)
    result = _prune.apply(g, _Ctx())
    assert result.stats["groups"] == 0
    assert len(g) == before


def test_prune_is_deterministic(monkeypatch):
    """같은 입력 → 같은 정본 (재실행 시 이름이 또 바뀌면 안 된다)."""
    monkeypatch.setenv("TBOX_DUP_OP_PRUNE", "on")
    keeps = set()
    for _ in range(5):
        g = _materialA_heat_graph()
        keeps.add(_prune.apply(g, _Ctx()).stats["plan"][0]["keep"])
    assert len(keeps) == 1


def test_gate_registered_after_prune():
    """게이트(22d)는 제거(22e) **뒤** 여야 한다 — 2026-08-30 순서 반전.

    이 테스트는 원래 반대(``gate_i < prune_i``)를 주장하며 이유를 "경고 로그가
    제거 전 규모를 보여준다" 로 적었다. 그 목적은 정당하지만 대가가 컸다: 22d 는
    **read-only 게이트**이므로 22e 앞에서 재면 **배포되지 않는 중간 상태**를
    판정한다. 08-19 산출물 실측 — 22e 앞 그룹 31/중복 35 vs 배포본 0/0. 게다가
    ``TBOX_DUP_OP_GATE=fail`` 이면 한 줄 뒤에 지워질 중복 때문에 S3 가
    ``RuntimeError`` 로 중단된다 (``run_step_pipeline`` 이 전파한다).

    "제거 전 규모" 는 게이트가 아니라 **22e 자신의 로그**가 이미 남긴다
    (``pruned_ops`` / ``pruned_triples`` / ``groups``) — 두 목적을 분리하면
    게이트는 배포본을, 22e 는 자기 작업량을 각각 정확히 보고한다.

    ``step_22f`` 도 같은 결함이었다 (무근거 OP 41 vs 배포본 39, baseline 40 초과로
    선재 실패). 판정 로직은 ``_base.is_registered_after`` 로 공용화했다.
    """
    from tools.quality_steps import _POST_STEPS
    mods = [getattr(f, "__module__", "") for f in _POST_STEPS]
    gate_i = next(i for i, m in enumerate(mods) if "step_22d" in m)
    prune_i = next(i for i, m in enumerate(mods) if "step_22e" in m)
    assert prune_i < gate_i, (
        "22d 가 22e 앞에 있다 — 배포되지 않는 중간 상태를 재고, fail 모드면 "
        "한 줄 뒤에 지워질 중복 때문에 S3 가 중단된다"
    )


def test_prune_reports_its_own_scale(monkeypatch):
    """PRESERVATION: "제거 전 규모" 는 22e 자신이 보고한다.

    위 테스트가 순서를 뒤집었으므로, 원래 목적(규모 가시성)이 유지되는지 별도로
    주장한다 — 순서만 바꾸고 이 축을 확인하지 않으면 정보가 조용히 사라진다.

    ``monkeypatch.setenv`` 를 쓴다 (이 파일의 다른 21곳과 동일). 처음엔
    ``os.environ[...] = "on"`` + ``finally: pop`` 으로 썼는데, ``.env`` 에
    ``TBOX_DUP_OP_PRUNE=on`` 이 있으므로 ``pop`` 이 **원래 값을 지워** 같은 세션의
    뒤 테스트에서 22e 가 no-op 이 됐다 — ``test_op_grounding_gate`` 의 baseline
    테스트가 무근거 OP 39 대신 69 를 보고 실패했다 (단독 실행은 통과, 조합 실행만
    실패하는 오염). 환경변수는 반드시 monkeypatch 로 되돌려야 한다.
    """
    monkeypatch.setenv("TBOX_DUP_OP_PRUNE", "on")
    g = _materialA_heat_graph()
    stats = _prune.apply(g, _Ctx()).stats
    assert stats["groups"] >= 1, stats
    assert stats["pruned_ops"] >= 1, "제거 규모가 보고되지 않았다"
    assert "plan" in stats, "어느 OP 가 정본인지 보고되지 않았다"


def test_gate_records_its_measurement_position():
    """자기점검 필드가 산출물에 남는가 — 순서가 또 바뀌면 드러나야 한다."""
    g = _materialA_heat_graph()
    stats = _gate.apply(g, _Ctx()).stats
    assert stats["duplicate_op_measured_after_prune"] is True


def test_gate_position_selfcheck_detects_wrong_order(monkeypatch):
    """MUTATION: 삭제 스텝이 뒤에 오면 자기점검이 False 를 낸다.

    이 검사가 없으면 순서를 되돌려도 조용히 통과한다 — 실제로 ``.pyc`` 캐시 때문에
    순서 변경이 런타임에 반영되지 않은 채 "고쳤다" 로 읽힌 적이 있다 (22f 때).
    """
    import sys

    from tools.quality_steps import _base

    fake_source = (
        "    step_22d_duplicate_op_gate.apply,\n"
        "    step_22e_duplicate_op_prune.apply,\n"
    )

    class _FakeInspect:
        @staticmethod
        def getsource(_mod):
            return fake_source

    monkeypatch.setitem(sys.modules, "inspect", _FakeInspect)
    try:
        assert _base.is_registered_after(
            "step_22d_duplicate_op_gate", *_base.OP_REMOVING_STEPS,
        ) is False
    finally:
        monkeypatch.undo()


def test_op_removing_steps_list_is_shared():
    """22d/22f 가 **같은** 삭제 스텝 목록을 쓴다 (사본 금지).

    2026-08-30: 22f 만 고치고 22d 를 놓친 원인이 판정 로직·목록의 사본이었다.
    새 삭제 스텝이 생겼을 때 갱신 지점이 하나여야 한다.
    """
    from tools.quality_steps import _base
    from tools.quality_steps import step_22f_op_grounding_gate as f_gate

    assert f_gate._OP_REMOVING_STEPS is _base.OP_REMOVING_STEPS, (
        "22f 가 목록 사본을 갖고 있다 — 갱신을 빠뜨리게 된다"
    )
    assert "step_22e_duplicate_op_prune" in _base.OP_REMOVING_STEPS
