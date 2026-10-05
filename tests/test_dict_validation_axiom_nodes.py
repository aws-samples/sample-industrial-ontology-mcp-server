"""딕셔너리 검증이 명명된 공리 노드를 분류 클래스로 세지 않는가.

2026-08-29 실측. 파이프라인 재실행에서 S11(`validate_semantic_dictionary`)이 처음으로
FAIL 했다 — high 7건 / warning 22건 (이전 실행 high 0)::

    [high] 클래스 Union_EquipmentStatus_44b2231a에 label_ko 없음
    [high] 클래스 Union_NDTResults_adc45d57에 label_ko 없음
    …7개 전부 Union_* 노드

S2 가 이번 실행에서 ``Union_*`` 명명 노드 7개를 만들었고, 검증기가 그것을 라벨이
있어야 하는 클래스로 취급했다. 그런데 이들은 **skolemize 된 공리 노드**다 —
``owl:unionOf`` 를 담은 표현식이고, 사람이 읽는 라벨이 없는 것이 정상이며 NL→SPARQL
진입점도 되지 않는다.

``graph_utils.is_anonymous_class_expression`` 이 이미 정본 판정을 갖고 있었고 실측상
7개 전부 ``True`` 를 반환했다 — **검증기가 그 판정을 쓰지 않은 것**이 결함이다.

이 리포는 같은 혼동으로 클래스 수가 2.4배 부풀고 mutation baseline 이 FAIL 로 고정된
이력이 있다 (``project_anon_class_expr_killed_gate`` /
``project_metrics_inflated_by_path_and_skolem``).

## 이 테스트의 방향

"공리 노드를 배제한다" 만 주장하면 **모든** 클래스를 배제해도 통과한다. 세 축을
고정한다:

* 배제 — 공리 노드는 라벨·quick_ref 검사 대상이 아니다
* 보존 — **진짜 클래스의 라벨 누락은 여전히 잡는다** (과잉 배제 방지)
* 정본 — 이름 패턴을 재구현하지 않고 공용 헬퍼를 쓴다
"""
from __future__ import annotations

import json
import pathlib

import pytest
from rdflib import OWL, RDF, RDFS, BNode, Graph, Literal, Namespace, URIRef

from domain.namespaces import DOMAIN_NS, bind_namespaces
from tools.semantic_dict_validation import _axiom_node_names, _validate

NS = Namespace(str(DOMAIN_NS))
DICT_PATH = pathlib.Path("data/generated/semantic_dictionary.json")
TBOX = pathlib.Path("data/generated/tbox/t_box.ttl")


def _tbox_with_union(tmp_path: pathlib.Path) -> str:
    """``Union_X`` 공리 노드와 평범한 클래스를 함께 담은 T-Box."""
    g = Graph()
    bind_namespaces(g)
    for name in ("RealClass", "MemberA", "MemberB"):
        g.add((NS[name], RDF.type, OWL.Class))
        g.add((NS[name], RDFS.label, Literal("이름", lang="ko")))
    union = NS["Union_RealClass_abc123"]
    g.add((union, RDF.type, OWL.Class))
    members = BNode()
    g.add((union, OWL.unionOf, members))
    import rdflib.collection
    rdflib.collection.Collection(g, members, [NS["MemberA"], NS["MemberB"]])
    path = tmp_path / "t_box.ttl"
    g.serialize(str(path), format="turtle")
    return str(path)


def _dict_payload(class_names: dict[str, dict]) -> dict:
    """검증기가 요구하는 최소 섹션을 갖춘 딕셔너리."""
    return {
        "metadata": {"version": "2.0"},
        "classes": class_names,
        "object_properties": {},
        "sparql_guide": {"prefixes": "x"},
        "common_mistakes": ["x"],
        "question_templates": {"q": {}},
        "class_quick_reference": dict.fromkeys(class_names, ""),
        "process_flow": {"chain": []},
    }


# ── 배제: 공리 노드는 검사 대상이 아니다 ────────────────────────────────


def test_union_node_is_detected_as_axiom(tmp_path):
    """THE REGRESSION: ``Union_*`` 를 공리 노드로 판정한다."""
    tbox = Graph()
    tbox.parse(_tbox_with_union(tmp_path), format="turtle")

    axiom = _axiom_node_names(tbox, ["Union_RealClass_abc123", "RealClass"])

    assert axiom == {"Union_RealClass_abc123"}


def test_union_node_label_is_not_required(tmp_path):
    """라벨 없는 공리 노드가 high 를 만들지 않는다."""
    tbox_path = _tbox_with_union(tmp_path)
    payload = _dict_payload({
        "RealClass": {"label_ko": "실클래스", "label_en": "Real",
                      "description_ko": "설명"},
        "MemberA": {"label_ko": "A", "label_en": "A", "description_ko": "설명"},
        "MemberB": {"label_ko": "B", "label_en": "B", "description_ko": "설명"},
        "Union_RealClass_abc123": {},          # 라벨 전무 — 정상이다
    })

    result = _validate(payload, tbox_path, "")

    label_issues = [
        i for i in result["issues"] if "Union_RealClass_abc123" in i["message"]
    ]
    assert not label_issues, f"공리 노드가 issue 로 잡혔다: {label_issues}"


def test_union_node_quick_ref_is_not_required(tmp_path):
    """공리 노드의 빈 quick_ref 가 warning 을 만들지 않는다.

    질의 속성을 갖지 않는 노드이므로 비어 있는 것이 맞다.
    """
    tbox_path = _tbox_with_union(tmp_path)
    payload = _dict_payload({
        "RealClass": {"label_ko": "실", "label_en": "R", "description_ko": "설명"},
        "MemberA": {"label_ko": "A", "label_en": "A", "description_ko": "설명"},
        "MemberB": {"label_ko": "B", "label_en": "B", "description_ko": "설명"},
        "Union_RealClass_abc123": {},
    })

    result = _validate(payload, tbox_path, "")

    quick_ref_warnings = [
        w for w in result.get("warnings", [])
        if "Union_RealClass_abc123" in w.get("message", "")
    ]
    assert not quick_ref_warnings, quick_ref_warnings


# ── 보존: 진짜 클래스의 누락은 잡는다 (NEGATIVE 방향) ───────────────────


def test_real_class_missing_label_is_still_high(tmp_path):
    """평범한 클래스의 label_ko 누락은 여전히 high 다.

    과잉 배제하면 이 검사가 무의미해진다 — 그 경우 라벨 없는 클래스가 LLM 프롬프트에
    이름만으로 실린다.
    """
    tbox_path = _tbox_with_union(tmp_path)
    payload = _dict_payload({
        "RealClass": {},                       # 라벨 누락 — 잡혀야 한다
        "MemberA": {"label_ko": "A", "label_en": "A", "description_ko": "설명"},
        "MemberB": {"label_ko": "B", "label_en": "B", "description_ko": "설명"},
    })

    result = _validate(payload, tbox_path, "")

    highs = [i for i in result["issues"]
             if i["rule"] == "missing_label_ko" and "RealClass" in i["message"]]
    assert highs, "진짜 클래스의 라벨 누락을 놓쳤다"


def test_plain_class_is_not_treated_as_axiom(tmp_path):
    """``unionOf`` 가 없는 클래스는 공리 노드가 아니다."""
    tbox = Graph()
    tbox.parse(_tbox_with_union(tmp_path), format="turtle")

    assert "RealClass" not in _axiom_node_names(tbox, ["RealClass"])
    assert "MemberA" not in _axiom_node_names(tbox, ["MemberA"])


def test_unknown_name_is_not_treated_as_axiom(tmp_path):
    """T-Box 에 없는 이름을 공리로 오판하지 않는다 (유령을 조용히 배제하면 안 된다)."""
    tbox = Graph()
    tbox.parse(_tbox_with_union(tmp_path), format="turtle")

    assert _axiom_node_names(tbox, ["NoSuchClass"]) == set()


# ── 정본: 공용 헬퍼를 쓴다 ──────────────────────────────────────────────


def test_uses_shared_helper_not_name_pattern():
    """이름 패턴을 재구현하지 않고 ``is_anonymous_class_expression`` 을 쓴다.

    사본을 만들면 판정이 갈린다 — 이 리포는 그 유형의 사고를 반복 겪었다.
    """
    src = pathlib.Path("tools/semantic_dict_validation.py").read_text(encoding="utf-8")

    assert "is_anonymous_class_expression" in src, "공용 판정을 쓰지 않는다"
    assert 'startswith("Union_")' not in src, "이름 패턴 사본을 만들었다"


# ── 배포 산출물 실측 ────────────────────────────────────────────────────


def test_deployed_dictionary_passes_validation():
    """배포 딕셔너리가 high 0 으로 통과하는가 — 실물 확인.

    실측: 이 수정 전에는 Union_* 7개 때문에 high 7 로 FAIL 했다.
    """
    if not (DICT_PATH.exists() and TBOX.exists()):
        pytest.skip("배포 산출물 없음")

    payload = json.loads(DICT_PATH.read_text(encoding="utf-8"))
    result = _validate(payload, str(TBOX), "data/generated/abox/a_box.ttl")

    assert result["summary"]["high"] == 0, (
        f"high {result['summary']['high']}건: "
        f"{[i['message'][:60] for i in result['issues'][:3]]}"
    )
    assert result["passed"] is True


def test_deployed_tbox_union_nodes_are_excluded_when_present():
    """실물에 ``Union_*`` 노드가 있으면 **딕셔너리에서 배제되는가**.

    ## 전제가 사라졌다 — 그것을 실패로 읽지 않는다 (2026-08-31)

    예전에는 ``assert unions`` 로 "실물에 union 노드가 있다" 를 요구하며 docstring 이
    "없어졌다면 전제를 재확인해야 한다" 고 적었다. 2026-08-31 S2 재생성에서 실제로
    사라졌다 (이전 세대 3개 → 이번 0개). 그것은 **개선**이다 — 명명 union 노드는
    공리인데 클래스로 세어져 지표를 부풀렸다 (이 리포의 "익명 표현식이 게이트를
    죽였다", "지표가 경로·skolem 으로 부풀었다").

    그래서 주장을 바꾼다: union 노드가 **있을 때만** 배제를 검사하고, 없으면 그 사실을
    보고하고 skip 한다. 전제 부재를 FAIL 로 내면 결함을 고친 것이 회귀로 보고된다
    (같은 형태를 이 세션에서 세 번 겪었다).
    """
    if not TBOX.exists():
        pytest.skip("T-Box 없음")

    tbox = Graph()
    tbox.parse(str(TBOX), format="turtle")
    unions = [
        str(s) for s in tbox.subjects(RDF.type, OWL.Class)
        if isinstance(s, URIRef) and list(tbox.objects(s, OWL.unionOf))
    ]
    if not unions:
        pytest.skip(
            "배포 T-Box 에 명명 union 노드가 없다 — S2 가 만들지 않게 됐다(개선). "
            "배제 로직 자체는 아래 합성 픽스처 테스트가 고정한다",
        )

    # 있으면 딕셔너리가 그것을 클래스로 세지 않아야 한다.
    from domain.graph_utils import is_anonymous_class_expression

    for uri in unions:
        assert is_anonymous_class_expression(URIRef(uri), tbox), (
            f"{uri.split('#')[-1]} 이 익명 표현식으로 판정되지 않는다 — 딕셔너리가 "
            f"공리를 클래스로 센다"
        )
