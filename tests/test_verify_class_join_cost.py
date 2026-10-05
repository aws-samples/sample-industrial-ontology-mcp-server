"""S12 조인 검증이 대용량 그래프에서 멈추지 않는지 회귀 가드.

**배경 (2026-07-26 실측)**: `test_domain_queries(verify_joins=True)` 가 533MB A-Box
(1,054만 트리플) 에서 **73분 경과 후에도 완료되지 않았다.** 문서상 예상은 10~16분.
그 사이 같은 MCP 서버를 쓰는 다른 도구까지 전부 블로킹됐다 — 즉시 끝나야 하는
``check_pipeline_state`` 조차 30분 타임아웃으로 실패했다.

원인은 3단계 fallback(`any_path`) 의 SPARQL 이 **데카르트 곱** 을 만든다는 것:

    SELECT (COUNT(*) AS ?c) WHERE {
      ?x a MaterialA . ?y a MaterialB .          ← 약 1.3만 × 약 1.6만 = 약 2억 조합
      { ?x ?p ?y } UNION { ?y ?p ?x }
    }

``LIMIT 1`` 로 바꿔도 **연결이 없는 경우** 는 전 조합을 확인해야 하므로 개선되지
않는다 (최악 경로가 곧 실패 판정 경로다). 실측: 트리플 29,528 개 그래프에서 151초.

해결은 SPARQL 을 버리고 **rdflib 인덱스 직접 순회** 다 (``_exists_direct_link``):
두 클래스 인스턴스 집합을 각 O(n) 으로 만들고, 작은 쪽 인스턴스의 실제 이웃만
확인한다. 같은 그래프에서 **0.27초 — 약 560배** 개선.
"""
from __future__ import annotations

import inspect

from rdflib import RDF, Namespace

from domain.tbox_utils import _new_graph
from tools.query_test import _run_exists, _verify_class_join

STEEL = Namespace("http://example.com/steel-ontology#")
INST = Namespace("http://example.com/steel-instances#")


def _graph_with_link(*, forward: bool = True, n: int = 40):
    """A 인스턴스 n개, B 인스턴스 n개, 그리고 한 쌍만 연결된 그래프."""
    g = _new_graph()
    for i in range(n):
        g.add((INST[f"a{i}"], RDF.type, STEEL.ClassA))
        g.add((INST[f"b{i}"], RDF.type, STEEL.ClassB))
    if forward:
        g.add((INST["a0"], STEEL.linksTo, INST["b0"]))
    else:
        g.add((INST["b0"], STEEL.linksTo, INST["a0"]))
    return g


# ── _run_exists 헬퍼 ──────────────────────────────────────────────────


def test_run_exists_true_when_solution_present():
    g = _graph_with_link()
    assert _run_exists(
        g, "?x a steel:ClassA . ?x steel:linksTo ?y . ?y a steel:ClassB",
        "steel", str(STEEL),
    ) is True


def test_run_exists_false_when_absent():
    g = _graph_with_link()
    assert _run_exists(
        g, "?x a steel:ClassA . ?x steel:noSuchProp ?y . ?y a steel:ClassB",
        "steel", str(STEEL),
    ) is False


def test_run_exists_survives_bad_query():
    """잘못된 SPARQL 이 들어와도 예외를 던지지 않는다 (검증이 파이프라인을 막지 않음)."""
    g = _graph_with_link()
    assert _run_exists(g, "this is not sparql {{{", "steel", str(STEEL)) is False


# ── any_path 단계가 COUNT 가 아님을 고정 ──────────────────────────────


def test_any_path_stage_uses_existence_not_count():
    """3단계 any_path 는 인덱스 순회여야 한다 — SPARQL 집계로 되돌리면 실패.

    소스에서 직접 확인한다. 동작만 보면 어느 구현이든 결과가 같아
    (둘 다 passed=True) 성능 회귀를 잡을 수 없다.
    """
    src = inspect.getsource(_verify_class_join)
    any_path_block = src.split("# 3. any-path")[1].split("# 4.")[0]
    # 주석 줄은 제외 — 설명문에 "COUNT" 가 등장하는 것은 정상이다.
    code_only = "\n".join(
        line for line in any_path_block.splitlines()
        if not line.strip().startswith("#")
    )
    # SPARQL 집계 호출만 본다. 반환 dict 의 "count" 키는 정상이다.
    assert "COUNT(" not in code_only.upper() and "_run_count(" not in code_only, (
        "any_path 단계가 SPARQL 집계로 되돌아갔다 — 두 클래스의 데카르트 곱을 "
        f"만들어 대용량 그래프에서 멈춘다 (실측 73분+ 미완료).\n{code_only}"
    )
    assert "_exists_direct_link(" in code_only, (
        "any_path 단계는 _exists_direct_link (rdflib 인덱스 순회) 로 판정해야 한다 — "
        "SPARQL 로는 데카르트 곱을 피할 수 없다"
    )


def test_any_path_still_detects_reverse_only_link():
    """성능 수정 후에도 판정 능력은 유지된다 — 역방향 연결도 찾아낸다.

    CQ 가 명시한 OP 이름과 무관한 방향으로만 이어져 있어도 any_path 가 잡아야
    한다 (이것이 3단계가 존재하는 이유).
    """
    g = _graph_with_link(forward=False)
    # CQ 는 A→B 방향의 'someOtherOp' 를 기대했지만 실제로는 B→A 의 linksTo 뿐
    result = _verify_class_join(
        g, "ClassA", "someOtherOp", "ClassB",
        ns_prefix="steel", ns_uri=str(STEEL),
    )
    assert result["passed"] is True
    assert result["direction"] == "any_path"


def test_no_link_reports_failure():
    """연결이 전혀 없으면 정직하게 실패로 보고한다."""
    g = _new_graph()
    g.add((INST["a0"], RDF.type, STEEL.ClassA))
    g.add((INST["b0"], RDF.type, STEEL.ClassB))

    result = _verify_class_join(
        g, "ClassA", "linksTo", "ClassB",
        ns_prefix="steel", ns_uri=str(STEEL),
    )
    assert result["passed"] is False
    assert result["direction"] == "none"
    # 4단계 전부 시도했음을 기록에 남긴다
    assert [a["direction"] for a in result["attempts"]] == [
        "forward", "reverse", "any_path", "2_hop",
    ]


def test_forward_direction_reports_exact_count():
    """1단계(명시된 OP)는 건수를 세도 선택도가 높아 안전 — 정확한 수를 보고한다."""
    g = _new_graph()
    for i in range(5):
        g.add((INST[f"a{i}"], RDF.type, STEEL.ClassA))
        g.add((INST[f"b{i}"], RDF.type, STEEL.ClassB))
        g.add((INST[f"a{i}"], STEEL.linksTo, INST[f"b{i}"]))

    result = _verify_class_join(
        g, "ClassA", "linksTo", "ClassB",
        ns_prefix="steel", ns_uri=str(STEEL),
    )
    assert result["passed"] is True
    assert result["direction"] == "forward"
    assert result["count"] == 5


# ── _exists_direct_link — 인덱스 순회 방식 ────────────────────────────


def test_direct_link_found_either_direction():
    """방향과 무관하게 직접 연결을 찾는다."""
    from tools.query_test import _exists_direct_link

    for forward in (True, False):
        g = _graph_with_link(forward=forward)
        found, via = _exists_direct_link(g, "ClassA", "ClassB", str(STEEL))
        assert found is True
        assert via == "linksTo", f"연결 프로퍼티를 알려줘야 한다 (forward={forward})"


def test_direct_link_absent_returns_false():
    """연결이 없으면 False — 이 경로가 최악 비용 경로다."""
    from tools.query_test import _exists_direct_link

    g = _new_graph()
    for i in range(50):
        g.add((INST[f"a{i}"], RDF.type, STEEL.ClassA))
        g.add((INST[f"b{i}"], RDF.type, STEEL.ClassB))
    found, via = _exists_direct_link(g, "ClassA", "ClassB", str(STEEL))
    assert found is False and via is None


def test_direct_link_scales_without_cartesian_blowup():
    """실측 규모(약 2억 조합)에서 초 단위로 끝나야 한다.

    이전 SPARQL 구현은 같은 그래프에서 151초였다. 넉넉히 10초를 상한으로 둔다 —
    데카르트 곱으로 되돌아가면 반드시 초과한다.
    """
    import time

    from tools.query_test import _exists_direct_link

    g = _new_graph()
    for i in range(13339):
        g.add((INST[f"s{i}"], RDF.type, STEEL.MaterialA))
    for i in range(16189):
        g.add((INST[f"p{i}"], RDF.type, STEEL.MaterialB))

    start = time.time()
    found, _ = _exists_direct_link(g, "MaterialA", "MaterialB", str(STEEL))
    elapsed = time.time() - start

    assert found is False
    assert elapsed < 10, (
        f"연결 0건 판정에 {elapsed:.1f}초 — 데카르트 곱으로 회귀했다 "
        "(인덱스 순회는 0.3초 수준)"
    )


def test_direct_link_empty_class_short_circuits():
    """한쪽 클래스에 인스턴스가 없으면 즉시 False (불필요한 순회 없음)."""
    from tools.query_test import _exists_direct_link

    g = _new_graph()
    g.add((INST["a0"], RDF.type, STEEL.ClassA))
    found, via = _exists_direct_link(g, "ClassA", "ClassB", str(STEEL))
    assert found is False and via is None


# ── _run_exists_2hop — 4단계도 인덱스 순회 ────────────────────────────


def _graph_2hop(*, linked: bool, n: int = 300):
    """A—M—B 간접 연결 (linked=True) 또는 미연결 그래프."""
    g = _new_graph()
    for i in range(n):
        g.add((INST[f"a{i}"], RDF.type, STEEL.ClassA))
        g.add((INST[f"b{i}"], RDF.type, STEEL.ClassB))
    for i in range(20):
        g.add((INST[f"m{i}"], RDF.type, STEEL.Mid))
        g.add((INST[f"a{i}"], STEEL.toMid, INST[f"m{i}"]))
    if linked:
        g.add((INST["b3"], STEEL.fromMid, INST["m5"]))
    return g


def test_2hop_detects_indirect_link():
    """중간 노드를 경유한 연결을 찾는다 — 이 단계의 존재 이유."""
    from tools.query_test import _run_exists_2hop

    g = _graph_2hop(linked=True)
    assert _run_exists_2hop(g, "ClassA", "ClassB", "steel", str(STEEL)) is True


def test_2hop_false_when_no_indirect_link():
    """간접 연결도 없으면 False."""
    from tools.query_test import _run_exists_2hop

    g = _graph_2hop(linked=False)
    assert _run_exists_2hop(g, "ClassA", "ClassB", "steel", str(STEEL)) is False


def test_2hop_stage_avoids_sparql():
    """4단계도 SPARQL 을 쓰지 않아야 한다 — 자유 변수 3개는 비용이 폭발한다.

    실측 (연결 0건): 트리플 1,200 → 0.17초 / 4,500 → 2.75초 / 13,200 → 26.9초.
    규모 3배에 시간 10배 — 실제 A-Box(1,054만) 에선 완료 불가.
    """
    import inspect

    from tools.query_test import _run_exists_2hop

    src = inspect.getsource(_run_exists_2hop)
    code_only = "\n".join(
        line for line in src.splitlines()
        if not line.strip().startswith("#") and '"""' not in line
    )
    assert "graph.query(" not in code_only, (
        "2-hop 단계가 SPARQL 로 되돌아갔다 — ?x ?m ?y 자유 변수 곱으로 "
        "대용량 그래프에서 멈춘다"
    )


def test_2hop_scales_at_real_size():
    """실측 규모에서 초 단위로 끝나야 한다 (이전 SPARQL 구현은 27초+)."""
    import time

    from tools.query_test import _run_exists_2hop

    g = _new_graph()
    for i in range(6000):
        g.add((INST[f"a{i}"], RDF.type, STEEL.ClassA))
    for i in range(7000):
        g.add((INST[f"b{i}"], RDF.type, STEEL.ClassB))
    for i in range(100):
        g.add((INST[f"m{i}"], RDF.type, STEEL.Mid))
        g.add((INST[f"a{i}"], STEEL.toMid, INST[f"m{i}"]))

    start = time.time()
    found = _run_exists_2hop(g, "ClassA", "ClassB", "steel", str(STEEL))
    elapsed = time.time() - start

    assert found is False
    assert elapsed < 5, (
        f"2-hop 판정에 {elapsed:.1f}초 — SPARQL 방식으로 회귀했다 "
        "(인덱스 순회는 0.2초 수준)"
    )
