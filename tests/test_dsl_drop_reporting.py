"""DSL 엔진이 버린 지시를 **사유와 함께** 보고한다 — 조용한 폐기 회귀 가드.

2026-08-26 감사. ``_apply_high_level_instructions`` (387줄 / 분기 76 / 커버리지
36.6%, 이 파일 최대 함수) 는 ``return g.serialize(format="turtle")`` 로 **TTL 문자열
하나만** 반환했다. 그래서 지시가 버려져도 호출부·응답·``round_log`` 어디에도 흔적이
없었다. 실측 비대칭:

    적용을 세는 코드   15군데 (``applied += 1``)
    폐기를 세는 코드    0군데   ← 로그 17줄만, 그중 2줄은 logger.debug

``server.py`` 가 root 로거를 INFO 로 두므로 ``logger.debug`` 분기는 배포에서
**흔적조차 없다**.

## 옆 엔진은 같은 교훈을 이미 적용했다

``jury_fixes.apply_jury_fixes`` 는 ``applied/skipped/failed/noop`` 를 반환하고 그
주석에 이렇게 적혀 있다 — *"실패를 카운터로만 남기면 조용히 유실된다"*, *"정수 3개만
남아 {applied:4, failed:37} 이 왜인지 알 수 없었다"*. **같은 교훈이 한쪽 엔진에만**
적용돼 있었고, S2 에서 "LLM 지시가 조용히 폐기됨" 이 네 번 재발한 지점은 전부 이
함수다 (유령 IRI 183건 / 외래 prefix 뭉갬 42 트리플 / ``_to_node`` 101건 / Jury 키
불일치 37건).

## 세 종류를 구분한다

``skipped`` (가드가 **의도적으로** 거부 — 예약어·members<2·중복 Restriction·IFP) /
``failed`` (예외) / ``unknown`` (이 엔진이 모르는 action). 뭉치면 "가드가 옳게
막았나 / 계약이 깨졌나 / 이름이 안 맞나" 를 구분할 수 없다.

## 호환은 래퍼로 유지한다

``_apply_high_level_instructions`` 는 ``_apply_dsl_instructions(...)["ttl"]`` 를
돌려주는 얇은 래퍼로 남는다. 호출부 3곳이 TTL 문자열을 기대하므로 계약을 깨지 않는다.
``_architect_revise`` 는 반환값을 바꿀 수 없어 ``dsl_stats_out`` out-param 을 쓴다.

## 이 테스트의 방향

"카운터가 늘었다" 만 주장하면 사유 문자열이 비어도 통과한다. 세 축을 함께 고정한다:

* 분류 — skipped / failed / unknown 이 **서로 섞이지 않는다**
* 사유 — 각 항목에 대상(target)과 이유(reason)가 **비어 있지 않다**
* 배선 — 통계가 ``round_log`` 까지 실제로 도달한다 (함수 반환만으로는 부족)
"""
from __future__ import annotations

import json

import pytest

from tools.multi_agent_tbox import (
    _apply_dsl_instructions,
    _apply_high_level_instructions,
)

TTL = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix steel: <http://example.com/steel-ontology#> .
steel:A a owl:Class .
steel:B a owl:Class .
"""


def _apply(instrs: list[dict]) -> dict:
    return _apply_dsl_instructions(TTL, instrs)


# ── 분류: 세 종류가 섞이지 않는다 ───────────────────────────────────────


def test_unknown_action_is_reported():
    """THE REGRESSION: 모르는 action 이 조용히 사라지지 않는다.

    예전에는 ``logger.debug`` 한 줄이었다 — 배포 로그 레벨(INFO)에서 불가시.
    """
    r = _apply([{"action": "no_such_action", "name": "Z"}])
    assert len(r["unknown"]) == 1
    assert r["unknown"][0]["action"] == "no_such_action"
    assert r["unknown"][0]["reason"], "사유가 비어 있다"
    assert r["skipped"] == [] and r["failed"] == []


def test_guard_rejection_is_skipped_not_failed():
    """가드가 의도적으로 거부한 것은 ``skipped`` — 예외(``failed``)와 구분한다."""
    r = _apply([{"action": "add_disjoint_classes", "members": ["A"]}])
    assert len(r["skipped"]) == 1
    assert r["failed"] == [], "의도된 거부가 실패로 분류됐다"
    assert "members" in r["skipped"][0]["reason"]


def test_exception_is_failed_with_type():
    """예외는 ``failed`` 이고 예외 타입이 사유에 남는다."""
    r = _apply([{"action": "add_object_property"}])   # name 누락 → KeyError
    assert len(r["failed"]) == 1
    assert "KeyError" in r["failed"][0]["reason"]
    assert r["skipped"] == [] and r["unknown"] == []


def test_ifp_rejection_is_recorded():
    """IFP 거부는 이 파이프라인의 의도된 정책이므로 사유가 남아야 한다.

    추론기 sameAs 폭발 때문에 막는 것이고, 기록이 없으면 "왜 내 IFP 가 안 들어갔나"
    를 알 수 없다.
    """
    r = _apply([{"action": "add_inverse_functional_property", "name": "aToB"}])
    assert len(r["skipped"]) == 1
    assert "IFP" in r["skipped"][0]["reason"] or "sameAs" in r["skipped"][0]["reason"]


def test_mixed_batch_classifies_each():
    """한 배치에 섞여 오면 각각 제 칸으로 간다."""
    r = _apply([
        {"action": "add_object_property", "name": "aToB",
         "domain": "A", "range": "B"},                       # applied
        {"action": "add_disjoint_classes", "members": ["A"]},  # skipped
        {"action": "add_object_property"},                    # failed
        {"action": "bogus", "name": "Z"},                     # unknown
    ])
    assert r["applied_count"] == 1
    assert r["requested"] == 4
    assert (len(r["skipped"]), len(r["failed"]), len(r["unknown"])) == (1, 1, 1)


# ── 사유: 목록이 실질 정보를 담는다 ─────────────────────────────────────


def test_every_dropped_entry_has_target_and_reason():
    """모든 폐기 항목에 사유가 있다 — 빈 문자열이면 목록이 무의미하다."""
    r = _apply([
        {"action": "add_disjoint_classes", "members": ["A"]},
        {"action": "add_object_property"},
        {"action": "bogus", "name": "Z"},
        {"action": "add_equivalent_class_union", "parent": "A", "members": ["B"]},
    ])
    dropped = r["skipped"] + r["failed"] + r["unknown"]
    assert len(dropped) == 4
    for entry in dropped:
        assert entry["reason"].strip(), f"사유 없음: {entry}"
        assert "action" in entry


def test_target_is_extracted_from_both_dsl_dialects():
    """두 DSL 의 필드 이름이 달라도 대상을 뽑는다.

    Architect 는 ``name``/``target_class``, Jury 는 ``property``/``class`` 를 쓴다.
    한쪽만 보면 다른 엔진의 폐기가 전부 ``""`` 로 보고돼 목록이 쓸모없어진다.
    """
    r = _apply([{"action": "bogus", "property": "myProp"}])
    assert r["unknown"][0]["target"] == "myProp"
    r = _apply([{"action": "bogus", "class": "MyClass"}])
    assert r["unknown"][0]["target"] == "MyClass"
    r = _apply([{"action": "bogus", "target_class": "TC"}])
    assert r["unknown"][0]["target"] == "TC"


def test_applied_list_names_what_changed():
    """적용 목록도 카운트가 아니라 무엇이 바뀐지 담는다."""
    r = _apply([{"action": "add_object_property", "name": "aToB",
                 "domain": "A", "range": "B"}])
    assert r["applied"] == [{"action": "add_object_property", "target": "aToB"}]


def test_dropped_instruction_does_not_count_as_applied():
    """폐기가 ``applied`` 로 새지 않는다 — 적용 판정이 카운터 증가에 걸려 있는가."""
    r = _apply([{"action": "add_disjoint_classes", "members": ["A"]},
                {"action": "bogus"}])
    assert r["applied_count"] == 0
    assert r["applied"] == []


def test_empty_instructions_is_clean():
    """지시가 없으면 전부 빈 목록 — 잡음을 만들지 않는다."""
    r = _apply([])
    assert r["applied_count"] == 0
    assert (r["skipped"], r["failed"], r["unknown"]) == ([], [], [])
    assert r["ttl"]


# ── 호환: 기존 계약이 깨지지 않는다 ────────────────────────────────────


def test_wrapper_still_returns_ttl_string():
    """``_apply_high_level_instructions`` 는 여전히 TTL 문자열이다.

    호출부 3곳이 이 계약에 의존한다. 깨지면 S2 가 dict 를 TTL 로 파싱하려다 죽는다.
    """
    out = _apply_high_level_instructions(
        TTL, [{"action": "add_object_property", "name": "aToB",
               "domain": "A", "range": "B"}],
    )
    assert isinstance(out, str)
    from rdflib import Graph
    Graph().parse(data=out, format="turtle")     # 파싱 가능해야 한다


def test_ttl_content_matches_between_wrapper_and_detailed():
    """래퍼와 상세 함수가 같은 그래프를 만든다 — 사본 드리프트 방지."""
    instrs = [{"action": "add_subclass", "child": "B", "parent": "A"}]
    from rdflib import Graph
    g1 = Graph().parse(data=_apply_high_level_instructions(TTL, instrs),
                       format="turtle")
    g2 = Graph().parse(data=_apply_dsl_instructions(TTL, instrs)["ttl"],
                       format="turtle")
    assert len(g1) == len(g2)


# ── 배선: 통계가 round_log 까지 도달한다 ────────────────────────────────


def test_architect_revise_fills_stats_out(monkeypatch):
    """``_architect_revise`` 가 out-param 에 통계를 채우고 TTL 을 반환한다.

    이 경로가 S2 최대 폐기 지점이다. 반환값을 dict 로 바꾸면 호출부 계약이 깨지므로
    out-param 을 쓰고, 그것이 실제로 채워지는지 확인한다.
    """
    import tools.multi_agent_tbox as mat

    payload = json.dumps({"instructions": [
        {"action": "add_object_property", "name": "aToB",
         "domain": "A", "range": "B"},
        {"action": "add_disjoint_classes", "members": ["A"]},
        {"action": "bogus", "name": "Z"},
    ]}, ensure_ascii=False)
    monkeypatch.setattr(mat, "_invoke_bedrock", lambda *a, **k: payload)

    stats: dict = {}
    out = mat._architect_revise(
        TTL, {"issues": []}, {"issues": []}, 2, dsl_stats_out=stats,
    )
    assert isinstance(out, str), "반환 계약이 깨졌다"
    assert stats["applied"] == 1
    assert stats["requested"] == 3
    assert stats["skipped"] == 1
    assert stats["unknown"] == 1
    assert len(stats["dropped_details"]) == 2
    assert all(d["reason"] for d in stats["dropped_details"])


@pytest.mark.parametrize(
    "payload,expected",
    [
        ('{"instructions": []}', "empty"),
        ("이건 JSON 이 아니다", "ttl_unusable"),
        ("@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
         "@prefix steel: <http://example.com/steel-ontology#> .\n"
         "steel:Z a owl:Class .", "ttl_fallback"),
    ],
)
def test_every_return_path_records_outcome(monkeypatch, payload, expected):
    """모든 반환 경로가 ``outcome`` 을 남긴다 — 어느 경로를 탔는지 알아야 한다.

    2026-08-27 검증 실행에서 ``revision.dsl`` 이 3라운드 전부 비었다. 원인은 폐기가
    0 이어서가 아니라 Architect 가 **DSL 경로를 타지 않은** 것이었다 (트리플
    3690→3690→3674, no_progress 2연속, 변화는 전부 Jury 수정). DSL 경로에만 통계를
    채우면 "지시가 몇 건 버려졌나" 보다 먼저 답해야 할 질문 — **"지시를 내기는 했나"**
    — 에 답할 수 없다.
    """
    import tools.multi_agent_tbox as mat
    monkeypatch.setattr(mat, "_invoke_bedrock", lambda *a, **k: payload)
    stats: dict = {}
    out = mat._architect_revise(
        TTL, {"issues": []}, {"issues": []}, 2, dsl_stats_out=stats,
    )
    assert isinstance(out, str)
    assert stats.get("outcome") == expected, (
        f"경로 판정이 틀렸다: {stats.get('outcome')} != {expected}"
    )


def test_dsl_path_records_outcome_too(monkeypatch):
    """정상 DSL 경로도 ``outcome`` 을 남긴다 (통계만 있고 경로 표시가 없으면 안 된다)."""
    import tools.multi_agent_tbox as mat
    payload = json.dumps({"instructions": [
        {"action": "add_subclass", "child": "B", "parent": "A"},
    ]})
    monkeypatch.setattr(mat, "_invoke_bedrock", lambda *a, **k: payload)
    stats: dict = {}
    mat._architect_revise(TTL, {"issues": []}, {"issues": []}, 2,
                          dsl_stats_out=stats)
    assert stats["outcome"] == "dsl_applied"
    assert stats["applied"] == 1


def test_round_log_records_dsl_even_when_not_invoked():
    """``revision.dsl`` 이 **항상** 기록되는가 — 필드 부재와 값 0 은 다르다.

    빈 dict 를 걸러내면 "폐기 0" 과 "DSL 미호출" 을 구분할 수 없고, 실측에서 그
    구분이 no_progress 원인 오진의 핵심이었다.
    """
    import ast
    import pathlib

    src = pathlib.Path("tools/multi_agent_tbox.py").read_text(encoding="utf-8")
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "_run_one_debate_round"
    )
    body = ast.unparse(fn)
    assert "not_invoked" in body, (
        "DSL 미호출 상태가 round_log 에 남지 않는다 (빈 dict 가 걸러진다)"
    )


_DSL_JSON = '{"instructions":[{"action":"add_subclass","child":"B","parent":"A"}]}'


@pytest.mark.parametrize(
    "shape,text",
    [
        ("fence 정상", f"```json\n{_DSL_JSON}\n```"),
        ("fence 없음", _DSL_JSON),
        ("뒤에 해설", f"{_DSL_JSON}\n\n위와 같이 수정했습니다."),
        ("앞에 해설", f"분석 결과입니다:\n{_DSL_JSON}"),
        ("앞뒤 해설", f"검토했습니다.\n{_DSL_JSON}\n이상입니다."),
        ("fence 미닫힘", f"```json\n{_DSL_JSON}"),
    ],
)
def test_architect_parses_every_llm_response_shape(monkeypatch, shape, text):
    """LLM 이 흔히 내는 6가지 형태 모두 DSL 경로를 탄다.

    2026-08-27 실측. Architect 는 공용 ``_extract_json_candidate`` 를 쓰지 않고 fence
    정규식 한 줄만 갖고 있었다. 같은 파일 안의 공용 추출기는 3단 복구(fence →
    raw_decode → 괄호 매칭)를 하는데 이 경로만 사본이었다:

        케이스              자체 정규식   공용 추출기
        뒤에 해설            **FAIL**      OK
        앞에 해설            **FAIL**      OK
        앞뒤 해설            **FAIL**      OK
        fence 미닫힘(절단)    **FAIL**      OK

    **4/7 에서 실패**하고, 실패하면 전체 TTL 폴백으로 떨어져 그 라운드 지시가 전량
    소실된다. 검증 실행에서 3라운드 전부 이 경로였다 — 트리플 3690→3690→3674,
    no_progress 2연속, 변화는 전부 Jury 결정적 수정이 만들었고 Architect 기여는 0.
    """
    import tools.multi_agent_tbox as mat
    monkeypatch.setattr(mat, "_invoke_bedrock", lambda *a, **k: text)
    stats: dict = {}
    out = mat._architect_revise(
        TTL, {"issues": []}, {"issues": []}, 2, dsl_stats_out=stats,
    )
    assert stats.get("outcome") == "dsl_applied", (
        f"{shape} 형태가 DSL 경로를 타지 못했다 (outcome={stats.get('outcome')})"
    )
    assert stats["applied"] == 1
    from rdflib import Graph
    Graph().parse(data=out, format="turtle")


def test_architect_uses_shared_json_extractor():
    """사본이 되살아나지 않는가 — 공용 추출기 호출을 소스로 고정한다.

    이 리포는 "같은 로직의 사본이 갈린다" 를 반복 겪었다. 함수 테스트만으로는 누가
    다시 자체 정규식을 넣어도 (그 정규식이 6개 케이스를 우연히 통과하면) 잡히지 않는다.
    """
    import ast
    import pathlib

    src = pathlib.Path("tools/multi_agent_tbox.py").read_text(encoding="utf-8")
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "_architect_revise"
    )
    body = ast.unparse(fn)
    assert "_extract_json_candidate(text)" in body, (
        "Architect 가 공용 JSON 추출기를 쓰지 않는다"
    )
    assert "```(?:json)?" not in body, (
        "fence 정규식 사본이 남아 있다 — 공용 추출기로 단일화하라"
    )


def test_architect_token_budget_matches_reviewers():
    """Architect 예산이 리뷰어와 같은가.

    긴 JSON 목록을 내야 하는 쪽은 Architect 인데(Jury 가 라운드당 52~60건 요청)
    8,192 하드코딩이었고 리뷰어만 16,000 이었다. thinking 모델은 예산에서 thinking 을
    먼저 쓰므로 남는 몫이 더 줄어든다.
    """
    import ast
    import pathlib

    from tools.multi_agent_tbox import _REVIEW_MAX_TOKENS

    src = pathlib.Path("tools/multi_agent_tbox.py").read_text(encoding="utf-8")
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "_architect_revise"
    )
    body = ast.unparse(fn)
    assert "max_tokens=_REVIEW_MAX_TOKENS" in body, (
        "Architect 가 리뷰어와 다른 토큰 예산을 쓴다"
    )
    assert _REVIEW_MAX_TOKENS >= 16_000


def test_architect_revise_without_stats_out_still_works(monkeypatch):
    """out-param 을 안 주면 기존과 동일하게 동작한다 (기본값 None)."""
    import tools.multi_agent_tbox as mat
    payload = json.dumps({"instructions": [
        {"action": "add_subclass", "child": "B", "parent": "A"},
    ]})
    monkeypatch.setattr(mat, "_invoke_bedrock", lambda *a, **k: payload)
    out = mat._architect_revise(TTL, {"issues": []}, {"issues": []}, 2)
    assert isinstance(out, str)


@pytest.mark.parametrize(
    "fn_name,marker",
    [
        ("_run_one_debate_round", "architect_dsl_stats"),
        ("_apply_jury_required_fixes", "dsl_dropped_details"),
    ],
)
def test_round_log_wiring_is_present(fn_name, marker):
    """통계가 ``round_log`` / summary 로 흘러가는 배선이 있는가.

    함수 반환만 검증하면 배선이 끊겨도 초록이다 — 이 리포에서 단위 테스트 14건이
    초록인데 산출물이 두 번 안 바뀐 사고가 있었고 원인이 배선이었다.
    """
    import ast
    import pathlib

    src = pathlib.Path("tools/multi_agent_tbox.py").read_text(encoding="utf-8")
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == fn_name
    )
    assert marker in ast.unparse(fn), f"{fn_name} 에 {marker} 배선이 없다"


def test_debate_round_passes_stats_out_to_architect():
    """``_run_one_debate_round`` 가 out-param 을 실제로 **넘기는가**.

    변수 선언만 있고 인자로 안 넘기면 통계가 영원히 빈 dict 다.
    """
    import ast
    import pathlib

    src = pathlib.Path("tools/multi_agent_tbox.py").read_text(encoding="utf-8")
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "_run_one_debate_round"
    )
    body = ast.unparse(fn)
    assert "dsl_stats_out=architect_dsl_stats" in body, "out-param 을 넘기지 않는다"
    assert "round_log['revision']['dsl'] = architect_dsl_stats" in body, (
        "통계가 round_log.revision 에 실리지 않는다"
    )


# ── 익명 노드 표현식: 문자열로 뭉개지지 않는다 ─────────────────────────

_ANON_RESTRICTION = (
    '[ a owl:Restriction ; owl:onProperty steel:dp ; owl:hasValue "V" ]'
)


def _graph_of(ttl: str):
    from rdflib import Graph
    g = Graph()
    g.parse(data=ttl, format="turtle")
    return g


def test_anonymous_expression_becomes_structure_not_literal():
    """THE REGRESSION: Turtle 익명 노드가 **평문 Literal** 로 저장되지 않는다.

    2026-08-27 실측. LLM 은 클래스 정의 공리를 익명 노드로 보낸다:

        {"action":"add_triple","subject":"steel:RunningEquipmentStatus",
         "predicate":"owl:equivalentClass",
         "object":"[ a owl:Restriction ; owl:onProperty steel:equipmentStatusStatus ;
                     owl:hasValue \\"Running\\" ]"}

    ``_dsl_term`` → ``parse_object_term`` 이 이것을 판정 순서 4번("그 외 → 평문
    리터럴")으로 떨어뜨려 문자열로 저장하고 **"적용 1/1 성공"** 을 보고했다.

    배포 T-Box 실측: ``owl:equivalentClass`` 40건 중 **15건의 object 가 Literal** 이고
    전부 Turtle 소스 텍스트다 (``RunningEquipmentStatus``·``Scope1/2/3Emission``·
    ``HighSeverityAlarmEvent`` 등). OWL DL 위반이라 추론기가 무시하고, 그 클래스들은
    A-Box 0건 / 추론 0건의 빈 클래스로 남았다 — S2 리뷰어가 "불완전 분할" critical
    26건으로 지적해 3회 실행이 합의에 실패한 근본 원인이다.
    """
    from rdflib import OWL, Literal
    r = _apply_dsl_instructions(TTL + "\nsteel:dp a owl:DatatypeProperty .\n", [
        {"action": "add_triple", "subject": "steel:A",
         "predicate": "owl:equivalentClass", "object": _ANON_RESTRICTION},
    ])
    assert r["applied_count"] == 1
    g = _graph_of(r["ttl"])
    objects = list(g.objects(None, OWL.equivalentClass))
    assert objects, "equivalentClass 트리플이 없다"
    for obj in objects:
        assert not isinstance(obj, Literal), (
            f"익명 표현식이 문자열로 저장됐다: {str(obj)[:70]}"
        )


def test_anonymous_expression_keeps_its_parts():
    """파싱된 구조가 onProperty / hasValue 를 보존하는가 (형태만 BNode 면 무의미)."""
    from rdflib import OWL
    r = _apply_dsl_instructions(TTL + "\nsteel:dp a owl:DatatypeProperty .\n", [
        {"action": "add_triple", "subject": "steel:A",
         "predicate": "owl:equivalentClass", "object": _ANON_RESTRICTION},
    ])
    g = _graph_of(r["ttl"])
    node = next(iter(g.objects(None, OWL.equivalentClass)))
    props = {str(p).split("#")[-1] for p in g.objects(node, OWL.onProperty)}
    vals = {str(v) for v in g.objects(node, OWL.hasValue)}
    assert props == {"dp"}
    assert vals == {"V"}


def test_reasoner_classifies_via_parsed_expression():
    """**효과** 확인 — 파싱된 공리로 추론기가 개체를 분류하는가.

    구조로 저장돼도 추론이 안 되면 결함이 남는다. 이 리포는 형태만 검사한 픽스처가
    파생 0건을 통과시킨 이력이 있다 (propertyChainAxiom).
    """
    reasonable = pytest.importorskip("reasonable")
    from rdflib import RDF, Graph, Literal, URIRef
    ns = "http://example.com/steel-ontology#"
    r = _apply_dsl_instructions(
        TTL + "\nsteel:dp a owl:DatatypeProperty .\n"
        "steel:B rdfs:subClassOf steel:A .\n", [
            {"action": "add_triple", "subject": "steel:B",
             "predicate": "owl:equivalentClass", "object": _ANON_RESTRICTION},
        ])
    g = _graph_of(r["ttl"])
    g.add((URIRef(ns + "i1"), RDF.type, URIRef(ns + "A")))
    g.add((URIRef(ns + "i1"), URIRef(ns + "dp"), Literal("V")))
    engine = reasonable.PyReasoner()
    engine.from_graph(g)
    inferred = Graph()
    for triple in engine.reason():
        inferred.add(triple)
    assert (URIRef(ns + "i1"), RDF.type, URIRef(ns + "B")) in inferred, (
        "파싱은 됐지만 추론이 분류하지 않는다"
    )


def test_malformed_anonymous_expression_is_skipped_not_stringified():
    """문법이 깨진 익명 표현식은 **skip** 한다 — 문자열 저장이 이번 결함이었다."""
    from rdflib import OWL, Literal
    r = _apply_dsl_instructions(TTL, [
        {"action": "add_triple", "subject": "steel:A",
         "predicate": "owl:equivalentClass",
         # 대괄호는 **닫혀 있어야** 익명 경로를 탄다 (정규식이 `[...]` 를 요구).
         # 닫지 않으면 미매치로 일반 리터럴 경로가 되어 이 축을 검증하지 못한다 (실측).
         # 안에 미등록 prefix 를 두어 파싱만 실패시킨다.
         "object": "[ a nosuchprefix:Thing ; nosuchprefix:p nosuchprefix:v ]"},
    ])
    assert r["applied_count"] == 0
    assert len(r["skipped"]) == 1
    assert "익명" in r["skipped"][0]["reason"]
    g = _graph_of(r["ttl"])
    for obj in g.objects(None, OWL.equivalentClass):
        assert not isinstance(obj, Literal), "깨진 표현식이 문자열로 저장됐다"


def test_plain_literal_object_is_unaffected():
    """평범한 리터럴은 그대로 리터럴이다 — 익명 감지가 과잉 발동하지 않는가."""
    from rdflib import RDFS, Literal
    r = _apply_dsl_instructions(TTL, [
        {"action": "add_triple", "subject": "steel:A",
         "predicate": "rdfs:comment", "object": "설비 상태 기록"},
    ])
    assert r["applied_count"] == 1
    g = _graph_of(r["ttl"])
    comments = [o for o in g.objects(None, RDFS.comment)]
    assert comments and all(isinstance(o, Literal) for o in comments)


def test_remove_triple_does_not_create_structure():
    """``remove_triple`` 은 익명 표현식을 파싱하지 않는다 (그래프 오염 방지).

    삭제 경로에서 노드를 **추가**하면 지우려던 것보다 많이 남는다.
    """
    before = _graph_of(TTL)
    r = _apply_dsl_instructions(TTL, [
        {"action": "remove_triple", "subject": "steel:A",
         "predicate": "owl:equivalentClass", "object": _ANON_RESTRICTION},
    ])
    after = _graph_of(r["ttl"])
    assert len(after) <= len(before), "삭제 경로가 트리플을 늘렸다"
