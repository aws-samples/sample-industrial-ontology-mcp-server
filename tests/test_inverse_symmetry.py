"""``owl:inverseOf`` 가 편도로 선언돼도 역트리플이 양방향으로 채워지는가.

2026-08-28 실측. 필수참여 위반 64쌍을 CSV 근거로 분류하다 **10쌍이 배선 결손**임이
드러났다 — CSV FK 는 온전한데 역방향 OP 가 0 트리플이었다::

    hasRealTimeTag       36,000 트리플   (Real_Time_Data.Tag_ID, blank 0)
    isTagOfRealTimeData        0 트리플   ← TagMaster 50/50 이 위반으로 집계

원인은 T-Box 의 **편도 선언**이었다. ``owl:inverseOf`` 는 OWL 2 Spec §9.2.3 상
대칭 공리(A inverseOf B ≡ B inverseOf A)인데, ``ensure_inverse_triples`` 는 쌍을
선언된 방향에서만 수집한 뒤 그 방향으로만 채웠다::

    T-Box:  isTagOfRealTimeData  owl:inverseOf  hasRealTimeTag      ← 이것만 있음
            hasRealTimeTag       owl:inverseOf  isTagOfRealTimeData ← 없음
    결과:   hasRealTimeTag 트리플로부터 isTagOfRealTimeData 를 만들지 못함

## 왜 이것이 위반으로 보였나

T-Box 는 ``TagMaster ⊑ ∃isTagOfRealTimeData.RealTimeData`` 를 선언했다. 데이터는
있는데(역방향으로) 그 축이 비어 있어 필수참여 게이트가 50/50 위반으로 보고했다.
즉 **공리도 데이터도 맞았고 배선 한 줄이 빠진 것**이다.

## 이 테스트의 방향

"역트리플이 늘었다" 만 주장하면 무관한 트리플을 대량 생성해도 통과한다. 세 축을
고정한다:

* 대칭 — 어느 방향으로 선언됐든 양쪽이 채워진다
* 보존 — 호환성 미달 쌍은 여전히 skip 한다 (잘못된 inverseOf 가 번지는 것 차단)
* 멱등 — 두 번 돌려도 중복 생성하지 않는다
"""
from __future__ import annotations

import glob
import pathlib

import pytest
from rdflib import OWL, RDF, RDFS, Graph, Namespace, URIRef

from domain.namespaces import DOMAIN_NS, bind_namespaces
from domain.tbox_utils import ensure_inverse_triples

NS = Namespace(str(DOMAIN_NS))
INST = Namespace(str(DOMAIN_NS) + "instances#")


def _tbox(
    tmp_path: pathlib.Path, *, declare: str, forward: str, reverse: str,
    fwd_domain: str = "A", fwd_range: str = "B",
) -> str:
    """편도/양방향 inverseOf T-Box 를 파일로 쓰고 경로를 준다.

    ``declare``: "forward" | "reverse" | "both" — 어느 쪽에 inverseOf 를 쓸지.
    """
    g = Graph()
    bind_namespaces(g)
    for cls in {fwd_domain, fwd_range}:
        g.add((NS[cls], RDF.type, OWL.Class))
    g.add((NS[forward], RDF.type, OWL.ObjectProperty))
    g.add((NS[forward], RDFS.domain, NS[fwd_domain]))
    g.add((NS[forward], RDFS.range, NS[fwd_range]))
    g.add((NS[reverse], RDF.type, OWL.ObjectProperty))
    g.add((NS[reverse], RDFS.domain, NS[fwd_range]))
    g.add((NS[reverse], RDFS.range, NS[fwd_domain]))
    if declare in ("forward", "both"):
        g.add((NS[forward], OWL.inverseOf, NS[reverse]))
    if declare in ("reverse", "both"):
        g.add((NS[reverse], OWL.inverseOf, NS[forward]))
    path = tmp_path / "t_box.ttl"
    g.serialize(str(path), format="turtle")
    return str(path)


def _abox(prop: str, count: int = 3) -> Graph:
    g = Graph()
    bind_namespaces(g)
    for i in range(count):
        g.add((INST[f"a{i}"], RDF.type, NS["A"]))
        g.add((INST[f"b{i}"], RDF.type, NS["B"]))
        g.add((INST[f"a{i}"], NS[prop], INST[f"b{i}"]))
    return g


def _count(g: Graph, prop: str) -> int:
    return sum(1 for _ in g.triples((None, NS[prop], None)))


# ── 대칭: 어느 방향으로 선언됐든 채워진다 ───────────────────────────────


@pytest.mark.parametrize("declared", ["forward", "reverse", "both"])
def test_reverse_triples_filled_regardless_of_declaration_side(tmp_path, declared):
    """THE REGRESSION: 편도 선언에서도 역트리플이 만들어진다.

    ``declared="reverse"`` 가 배포 T-Box 10쌍의 실제 형태다 — 그 경우에만 실패했다.
    """
    tbox = _tbox(tmp_path, declare=declared, forward="hasX", reverse="isXOf")
    g = _abox("hasX", 3)

    added = ensure_inverse_triples(g, tbox)

    assert added == 3, f"{declared} 선언에서 역트리플 {added}건만 생성됐다"
    assert _count(g, "isXOf") == 3


def test_fill_works_from_the_reverse_direction_too(tmp_path):
    """역방향 데이터만 있어도 정방향이 채워진다 (대칭의 반대 축)."""
    tbox = _tbox(tmp_path, declare="forward", forward="hasX", reverse="isXOf")
    g = Graph()
    bind_namespaces(g)
    for i in range(4):
        g.add((INST[f"b{i}"], RDF.type, NS["B"]))
        g.add((INST[f"a{i}"], RDF.type, NS["A"]))
        g.add((INST[f"b{i}"], NS["isXOf"], INST[f"a{i}"]))

    ensure_inverse_triples(g, tbox)

    assert _count(g, "hasX") == 4


def test_both_sides_filled_when_data_is_mixed(tmp_path):
    """양쪽에 일부씩 있으면 각각의 빈 쪽이 채워진다."""
    tbox = _tbox(tmp_path, declare="reverse", forward="hasX", reverse="isXOf")
    g = _abox("hasX", 2)
    g.add((INST["b9"], RDF.type, NS["B"]))
    g.add((INST["a9"], RDF.type, NS["A"]))
    g.add((INST["b9"], NS["isXOf"], INST["a9"]))

    ensure_inverse_triples(g, tbox)

    assert _count(g, "hasX") == 3
    assert _count(g, "isXOf") == 3


# ── 보존: 잘못된 inverseOf 는 여전히 차단한다 (NEGATIVE 방향) ───────────


def test_incompatible_pair_is_still_skipped(tmp_path):
    """호환성 미달 쌍은 양방향 적용에서도 skip 된다.

    이 가드가 없으면 잘못된 inverseOf 선언이 domain/range 위반을 도미노로 만든다
    (기존 docstring 의 suppliesItem/isSuppliedBy 사례). 대칭 적용이 그 방어를
    무력화하면 안 된다.
    """
    g = Graph()
    bind_namespaces(g)
    for cls in ("A", "B", "C"):
        g.add((NS[cls], RDF.type, OWL.Class))
    # hasX: A → B / isXOf: C → A  — range(fwd)=B 와 domain(rev)=C 가 무관
    g.add((NS["hasX"], RDF.type, OWL.ObjectProperty))
    g.add((NS["hasX"], RDFS.domain, NS["A"]))
    g.add((NS["hasX"], RDFS.range, NS["B"]))
    g.add((NS["isXOf"], RDF.type, OWL.ObjectProperty))
    g.add((NS["isXOf"], RDFS.domain, NS["C"]))
    g.add((NS["isXOf"], RDFS.range, NS["A"]))
    g.add((NS["isXOf"], OWL.inverseOf, NS["hasX"]))
    tbox_path = tmp_path / "t_box.ttl"
    g.serialize(str(tbox_path), format="turtle")

    abox = _abox("hasX", 3)
    added = ensure_inverse_triples(abox, str(tbox_path))

    assert added == 0, "호환성 미달 쌍에서 역트리플이 생성됐다"
    assert _count(abox, "isXOf") == 0


def test_unrelated_properties_are_untouched(tmp_path):
    """inverseOf 가 없는 OP 는 건드리지 않는다."""
    tbox = _tbox(tmp_path, declare="reverse", forward="hasX", reverse="isXOf")
    g = _abox("hasX", 2)
    g.add((INST["a0"], NS["unrelatedProp"], INST["b0"]))
    before = _count(g, "unrelatedProp")

    ensure_inverse_triples(g, tbox)

    assert _count(g, "unrelatedProp") == before


# ── 멱등 ────────────────────────────────────────────────────────────────


def test_second_run_adds_nothing(tmp_path):
    """두 번 돌려도 중복 생성하지 않는다 — 파이프라인이 여러 번 호출한다."""
    tbox = _tbox(tmp_path, declare="reverse", forward="hasX", reverse="isXOf")
    g = _abox("hasX", 5)

    first = ensure_inverse_triples(g, tbox)
    size = len(g)
    second = ensure_inverse_triples(g, tbox)

    assert first == 5
    assert second == 0, f"재실행에서 {second}건이 추가됐다"
    assert len(g) == size


def test_both_side_declaration_does_not_double_count(tmp_path):
    """양방향 선언 시 같은 쌍을 두 번 순회해도 트리플이 중복되지 않는다."""
    tbox = _tbox(tmp_path, declare="both", forward="hasX", reverse="isXOf")
    g = _abox("hasX", 4)

    ensure_inverse_triples(g, tbox)

    assert _count(g, "isXOf") == 4


# ── 배포 산출물 실측 ────────────────────────────────────────────────────


def test_deployed_tbox_has_one_way_pairs():
    """배포 T-Box 에 편도 선언이 실제로 있는가 — 이 수정의 전제.

    없어졌다면(S2 가 양방향으로 내기 시작했다면) 이 게이트의 전제를 재확인해야 한다.
    """
    path = pathlib.Path("data/generated/tbox/t_box.ttl")
    if not path.exists():
        pytest.skip("T-Box 없음")

    tbox = Graph()
    tbox.parse(str(path), format="turtle")
    one_way = [
        (str(a).split("#")[-1], str(b).split("#")[-1])
        for a, b in tbox.subject_objects(OWL.inverseOf)
        if isinstance(a, URIRef) and isinstance(b, URIRef)
        and (b, OWL.inverseOf, a) not in tbox
    ]

    assert one_way, (
        "편도 inverseOf 선언이 없다 — 이 수정이 해결한 조건이 사라졌다면 "
        "테스트 전제를 갱신하라"
    )


def test_deployed_artifacts_gain_reverse_triples():
    """실물에서 역트리플이 실제로 늘어나는가.

    합성 픽스처만으로는 "실물에서 0건이 되는" 사고를 못 잡는다.
    """
    abox_path = pathlib.Path("data/generated/abox/a_box.ttl")
    tbox_path = pathlib.Path("data/generated/tbox/t_box.ttl")
    if not (abox_path.exists() and tbox_path.exists()):
        pytest.skip("배포 산출물 없음")

    g = Graph(store="Oxigraph")
    g.parse(str(abox_path), format="turtle")
    for f in glob.glob("data/source/tacit/*.ttl"):
        g.parse(f, format="turtle")

    added = ensure_inverse_triples(g, str(tbox_path))

    assert added >= 10000, f"실물에서 역트리플 {added}건만 생성됐다 — 배선이 조용하다"

    # ## OP 이름을 하드코딩하지 않는다 (2026-08-31 정정)
    #
    # 예전에는 ``isTagOfRealTimeData`` 가 채워지는지 봤다. 그 OP 는 S2 산출물이므로
    # **재생성마다 사라질 수 있다** — 2026-08-31 실측: 이번 T-Box 에 미선언이라
    # 테스트가 깨졌다 (이전 세대엔 있었다). 이 리포에는 OP 개명이 워크북 쿼리를
    # 0건으로 만든 전례가 두 번 있다.
    #
    # 주장을 이름 대신 **구조** 로 바꾼다: T-Box 가 선언한 inverseOf 쌍 중 A-Box 가
    # 한쪽만 채운 것이 있으면, 그 반대쪽이 실제로 채워졌는가.
    tbox = Graph()
    tbox.parse(str(tbox_path), format="turtle")
    filled_one_way = 0
    for left, _, right in tbox.triples((None, OWL.inverseOf, None)):
        if not (isinstance(left, URIRef) and isinstance(right, URIRef)):
            continue
        n_left = len(list(g.triples((None, left, None))))
        n_right = len(list(g.triples((None, right, None))))
        if n_left and n_right:
            filled_one_way += 1
            # 양쪽이 채워졌다면 역방향 materialize 가 동작한 것이다.
    assert filled_one_way >= 1, (
        "T-Box 의 inverseOf 쌍 중 양쪽이 채워진 것이 없다 — 편도 쌍이 그대로다"
    )
