"""프롬프트가 요구하는 OP 근거(``fk_column``)가 실제로 기록되는지 고정.

## 왜

OP 과잉의 **근본 원인은 프롬프트** 였다. 2026-08-13 수정 전:

- SME 프롬프트의 `Pairwise OP 경로 규칙` 이 CQ 클래스 쌍마다 OP 경로를 요구하면서
  **CSV FK 로 채울 수 있는지는 묻지 않았다**. 실측: 그 규칙이 요구하는 99쌍 중
  CSV FK 로 1-hop 가능 46 / 공유 허브 2-hop 30 → **어떤 경로도 없는 34쌍** 이
  매 라운드 "OP 신설" 요구의 발원지였다.
- `add_object_property` DSL 에 근거 필드가 **없었다** (DP 는 ``source`` 필수).
- 결과: OP 229개 선언 / A-Box 사용 14개(6%) — 같은 T-Box 의 DP 는 68%.

수정 후 프롬프트는 ``fk_column`` 을 필수로 요구한다. **코드가 그 값을 읽지 않으면
지시는 조용히 폐기된다** — 이 리포에서 네 번 반복된 실패 유형이라 이 파일이 배선을
고정한다.
"""
from __future__ import annotations

from rdflib import OWL, RDF, Graph, URIRef

from domain.namespaces import DOMAIN_NS, NS_PREFIX

_SRC = URIRef("http://purl.org/dc/terms/source")


def D(name: str) -> URIRef:
    return URIRef(DOMAIN_NS + name)


_TTL = (
    f"@prefix {NS_PREFIX}: <{DOMAIN_NS}> .\n"
    "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
    "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
    f"{NS_PREFIX}:A a owl:Class .\n{NS_PREFIX}:B a owl:Class .\n"
)


def _sources(g: Graph, local: str) -> list[str]:
    return sorted(str(o) for o in g.objects(D(local), _SRC))


# ── 배선: 두 경로 모두 fk_column 을 기록한다 ──────────────────────────


def test_jury_path_records_fk_column():
    """Jury ``add_object_property`` 가 ``fk_column`` 을 ``dcterms:source`` 로 남긴다."""
    from tools.jury_fixes import apply_jury_fixes

    res = apply_jury_fixes(_TTL, [{
        "action": "add_object_property",
        "name": "hasB", "domain": "A", "range": "B", "fk_column": "B_Code",
    }])
    g = Graph()
    g.parse(data=res["ttl"], format="turtle")
    assert len(res.get("applied", [])) == 1
    assert _sources(g, "hasB") == ["B_Code"], (
        f"fk_column 이 폐기됐다: {_sources(g, 'hasB')}"
    )


def test_dsl_path_records_fk_column():
    """S2 DSL 핸들러가 ``fk_column`` 을 기록한다."""
    from tools.multi_agent_tbox import _record_op_fk_source

    g = Graph()
    g.add((D("hasB"), RDF.type, OWL.ObjectProperty))
    _record_op_fk_source(g, D("hasB"), "B_Code")
    assert _sources(g, "hasB") == ["B_Code"]


def test_dsl_apply_path_records_fk_column():
    """배선 고정: DSL **적용 경로** 가 ``fk_column`` 을 소비한다.

    헬퍼를 직접 부르는 테스트는 호출부가 인자를 주지 않는 경우를 잡지 못한다 —
    이 리포에서 그 갭이 실제로 발생했다 (op_counts 가 계산됐는데 빌더가 못 받음).
    """
    import inspect

    from tools import multi_agent_tbox

    src = inspect.getsource(multi_agent_tbox)
    assert '_record_op_fk_source(g, op_uri, instr.get("fk_column"))' in src, (
        "add_object_property 핸들러가 fk_column 을 헬퍼에 넘기지 않는다 (배선 끊김)"
    )


def test_turtle_notation_in_fk_column_is_cleaned():
    """LLM 이 Turtle 표기를 섞어 보내도 CSV 헤더와 비교 가능한 값만 남는다.

    ``"\\"B_Code\\"^^xsd:string"`` 같은 값은 어떤 헤더와도 매칭되지 않는다 — DP 쪽에서
    실측 34건 발생한 사고와 같은 유형이다.
    """
    from tools.multi_agent_tbox import _record_op_fk_source

    g = Graph()
    g.add((D("hasB"), RDF.type, OWL.ObjectProperty))
    _record_op_fk_source(g, D("hasB"), '"B_Code"^^xsd:string')
    assert _sources(g, "hasB") == ["B_Code"], (
        f"Turtle 표기가 그대로 남았다: {_sources(g, 'hasB')}"
    )


def test_missing_or_empty_fk_column_records_nothing():
    """PRESERVATION: 값이 없으면 빈 문자열을 기록하지 않는다.

    빈 ``dcterms:source`` 는 "표기 있음" 으로 읽혀 커버리지를 부풀린다.
    """
    from tools.multi_agent_tbox import _record_op_fk_source

    for bad in (None, "", "   ", 42, '""'):
        g = Graph()
        g.add((D("hasB"), RDF.type, OWL.ObjectProperty))
        _record_op_fk_source(g, D("hasB"), bad)
        assert _sources(g, "hasB") == [], f"{bad!r} 를 기록했다"


def test_explicit_no_grounding_marker_is_preserved():
    """``none:<이유>`` 는 근거 부재를 **명시** 한 값이므로 남긴다.

    숨기지 않는 것이 목적이다 — ``step_22f`` 가 무근거로 집계해 드러낸다.
    """
    from tools.multi_agent_tbox import _record_op_fk_source

    g = Graph()
    g.add((D("hasB"), RDF.type, OWL.ObjectProperty))
    _record_op_fk_source(g, D("hasB"), "none:tacit 지식으로만 표현 가능")
    assert _sources(g, "hasB") == ["none:tacit 지식으로만 표현 가능"]


def test_source_key_list_includes_fk_column():
    """``_SOURCE_KEYS`` 에 ``fk_column`` 이 있어야 jury 경로가 값을 찾는다."""
    from tools.jury_fixes import _SOURCE_KEYS

    assert "fk_column" in _SOURCE_KEYS


# ── 프롬프트 계약: 지시문이 실제로 존재한다 ──────────────────────────


def test_sme_prompt_requires_csv_fk_before_demanding_a_new_op():
    """SME 가 OP 신설을 요구하기 전에 CSV FK 를 확인하도록 지시받는다.

    이 문구가 사라지면 Pairwise 규칙이 다시 FK 없는 관계를 대량 요구한다 —
    실측상 그것이 OP 229개/사용 14개의 직접 원인이었다.
    """
    from tools.multi_agent_prompts import sme_static_prefix

    text = sme_static_prefix("(CQ 없음)", "(요약)", "(크로스 도메인)")
    assert "CSV FK" in text, "SME 프롬프트에 CSV FK 근거 요구가 없다"
    assert "값 0건" in text or "0건" in text, "빈 관계의 결과를 설명하지 않는다"
    # 경로가 없을 때 "OP 를 신설하지 마라" 는 지시가 있어야 한다.
    assert "신설하라고 요구하지 마세요" in text or "요구하지 마세요" in text
    # (a)/(b)/(c) 삼분 판단이 온전해야 한다 — 하나라도 빠지면 SME 가 다시
    # "경로 없음 → OP 신설" 로 직결한다.
    assert "unanswerable_cqs" in text, "데이터 갭을 등록할 곳을 안 알려준다"
    assert "데이터 갭" in text, "T-Box 결함과 데이터 갭을 구분하지 않는다"
    assert "2-hop" in text, "공유 허브 경유 경로를 대안으로 제시하지 않는다"


def test_architect_prompt_requires_fk_column_field():
    """``add_object_property`` 스펙에 ``fk_column`` 이 필수로 적혀 있다."""
    import inspect

    from tools import multi_agent_prompts

    src = inspect.getsource(multi_agent_prompts)
    assert "fk_column" in src, "DSL 스펙에 fk_column 이 없다"
    # DP 의 source 와 대칭이어야 한다.
    assert "`source` 필수" in src or "**`source` 필수**" in src


def test_property_rules_module_requires_dcterms_source():
    """단일 패스 프롬프트에도 ``dcterms:source`` 요구가 있어야 한다.

    코드 8개 파일이 이 계약을 요구하는데 프롬프트에는 없었다 — 준수율 41.7% 는
    LLM 불복종이 아니라 **미지시** 였다.
    """
    import os

    path = os.path.join("prompts", "tbox-prompt-modules", "04-property-rules.md")
    if not os.path.exists(path):
        import pytest
        pytest.skip("프롬프트 모듈 없음")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    assert "dcterms:source" in text, "DP 출처 표기 요구가 없다"
    assert "only when supported by a CSV FK" in text, (
        "OP 를 FK 없이 만들라는 지시가 남아 있다"
    )
