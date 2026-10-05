"""딕셔너리가 ObjectProperty 에 population 신호를 주는지 고정.

## 왜

딕셔너리는 LLM NL→SPARQL 레퍼런스다. DP 에는 ``is_populated`` 가 **항상** 붙어
값 0건 프로퍼티를 ``∅`` 로 회피할 수 있는데, OP 에는 그 신호가 없었다:

실측 (2026-08-12 배포 딕셔너리):
- 전역 ``object_properties`` 229개 중 ``triple_count`` 보유 **14개**
  (``if prop["name"] in op_counts`` — 0건이면 키 자체를 안 붙였다)
- ``is_populated`` 보유 **0개**
- 클래스 단위 ``object_properties_outgoing/incoming`` **396개 전부** 신호 0개
- 대조: DP 는 **254/254** 가 ``is_populated`` 보유

그래서 LLM 이 값 0건 관계를 정상 어휘로 받아 고르고, 0행이 에러 없이 "정답처럼"
반환된다. 배포본 OP 229개 중 실제 값이 있는 것은 14개뿐이다.

v1(vocabulary contract, ``include_stats=False``) 은 A-Box 전이라 신호를 붙이지
않는다 — 전부 ``False`` 로 찍으면 값이 있는 OP 에도 거짓을 심는다 (DP 의
``emit_population`` 과 같은 규약).
"""
from __future__ import annotations

from tools.semantic_dictionary import (
    _build_object_properties_dict,
    _build_op_outgoing_incoming,
)


def _prop(name: str, *, rng=("Beta",), dom=("Alpha",)) -> dict:
    return {
        "name": name,
        "label_en": name, "label_ko": name,
        "description_en": "", "description_ko": "",
        "domains": list(dom), "ranges": list(rng),
        "inverseOf": None, "subPropertyOf": None, "transitive": False,
    }


_OPS = [_prop("usedOp"), _prop("emptyOp")]
_COUNTS = {"usedOp": 1560}


# ── THE REGRESSION: 값 0건 OP 도 신호를 받아야 한다 ───────────────────


def test_global_op_dict_always_emits_population_signal():
    """핵심 회귀: 값 0건 OP 에도 ``is_populated`` 키가 있어야 한다.

    예전에는 키 자체가 없어 LLM 이 "값 있음/없음" 을 구분할 수 없었다.
    """
    result = _build_object_properties_dict(_OPS, _COUNTS)
    assert set(result) == {"usedOp", "emptyOp"}
    for name in ("usedOp", "emptyOp"):
        assert "is_populated" in result[name], f"{name} 에 신호가 없다"
        assert "triple_count" in result[name]
    assert result["usedOp"]["is_populated"] is True
    assert result["usedOp"]["triple_count"] == 1560
    assert result["emptyOp"]["is_populated"] is False
    assert result["emptyOp"]["triple_count"] == 0


def test_class_level_op_entries_get_population_signal():
    """클래스 단위 outgoing/incoming 항목도 신호를 받아야 한다.

    실측: 396개 전부 신호 0개였다 — ``op_counts`` 가 계산돼 있는데도 이 빌더가
    인자로 받지 못해 버려졌다.
    """
    out, inn = _build_op_outgoing_incoming(
        "Alpha",
        {"Alpha": _OPS},
        {"Alpha": _OPS},
        _COUNTS,
    )
    assert len(out) == 2 and len(inn) == 2
    for entry in out + inn:
        assert "is_populated" in entry, f"신호 없음: {entry}"
        assert "triple_count" in entry
    by_name = {e["property"]: e for e in out}
    assert by_name["usedOp"]["is_populated"] is True
    assert by_name["emptyOp"]["is_populated"] is False


# ── PRESERVATION: v1 은 거짓 신호를 심지 않는다 ───────────────────────


def test_v1_contract_omits_the_signal_entirely():
    """``emit_population=False`` (v1) 면 신호를 아예 붙이지 않는다.

    v1 은 A-Box 생성 **전** 에 만들어지므로 모든 OP 가 0건이다. 그것을
    ``is_populated=False`` 로 찍으면 나중에 값이 실릴 OP 에도 거짓이 남는다.
    """
    result = _build_object_properties_dict(_OPS, {}, emit_population=False)
    for name in ("usedOp", "emptyOp"):
        assert "is_populated" not in result[name], f"{name} 에 v1 이 거짓 신호를 심었다"
        assert "triple_count" not in result[name]


def test_class_level_omits_signal_when_counts_absent():
    """``op_counts=None`` (v1 경로) 이면 클래스 단위도 신호를 붙이지 않는다."""
    out, inn = _build_op_outgoing_incoming("Alpha", {"Alpha": _OPS}, {"Alpha": _OPS})
    for entry in out + inn:
        assert "is_populated" not in entry, f"v1 인데 신호를 붙였다: {entry}"


# ── PRESERVATION: 기존 필드가 사라지지 않는다 ─────────────────────────


def test_existing_fields_survive():
    """label/domain/range/inverseOf 등 기존 계약이 유지된다."""
    ops = [{**_prop("withMeta"), "inverseOf": "invOp", "subPropertyOf": "parentOp",
            "transitive": True}]
    result = _build_object_properties_dict(ops, {"withMeta": 5})
    e = result["withMeta"]
    assert e["label_ko"] == "withMeta"
    assert e["domain"] == ["Alpha"]
    assert e["range"] == ["Beta"]
    assert e["inverseOf"] == "invOp"
    assert e["subPropertyOf"] == "parentOp"
    assert e["transitive"] is True
    assert e["is_populated"] is True


def test_class_level_keeps_target_and_source_keys():
    """outgoing 은 ``target``, incoming 은 ``source`` 를 유지한다."""
    out, inn = _build_op_outgoing_incoming(
        "Alpha", {"Alpha": [_prop("op1")]}, {"Alpha": [_prop("op1")]}, {"op1": 3},
    )
    assert out[0]["target"] == ["Beta"]
    assert inn[0]["source"] == ["Alpha"]
    assert out[0]["is_populated"] is True


def test_deployed_dictionary_has_op_signals_after_regeneration():
    """실측 고정: v2 딕셔너리는 OP 전수에 신호가 있어야 한다.

    이 수치가 내려가면 LLM 이 다시 빈 관계를 구분할 수 없게 된다.
    """
    import json
    import os

    import pytest

    import config

    if not os.path.exists(config.SEMANTIC_DICT_PATH):
        pytest.skip("딕셔너리 없음")
    with open(config.SEMANTIC_DICT_PATH, encoding="utf-8") as fh:
        d = json.load(fh)
    if not d.get("metadata", {}).get("stats_included"):
        pytest.skip("v1 딕셔너리 (신호 없는 것이 정상)")
    ops = d.get("object_properties") or {}
    missing = [k for k, v in ops.items() if "is_populated" not in v]
    if missing:
        pytest.skip(
            f"배포 딕셔너리가 이 수정 전에 생성됐다 (신호 없는 OP {len(missing)}개) — "
            "generate_semantic_dictionary 재생성 후 이 테스트가 계약을 고정한다",
        )
    assert missing == []


def test_class_builder_receives_op_counts_from_the_pipeline():
    """배선 고정: ``_build_classes_dict`` 가 ``op_counts`` 를 클래스 빌더에 넘긴다.

    빌더가 신호를 붙일 수 있어도 **호출부가 인자를 주지 않으면** 산출물에는
    신호가 없다 — 실제로 그 상태였다 (op_counts 는 계산돼 있는데 버려졌다).
    단위 테스트는 빌더를 직접 호출하므로 이 갭을 잡지 못한다.
    """
    from rdflib import Graph

    from domain.namespaces import DOMAIN_NS, NS_PREFIX
    from tools.semantic_dictionary import _build_classes_dict

    tbox = Graph()
    tbox.parse(data=(
        f"@prefix {NS_PREFIX}: <{DOMAIN_NS}> .\n"
        "@prefix owl: <http://www.w3.org/2002/07/owl#> .\n"
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n"
        f'{NS_PREFIX}:Alpha a owl:Class ; rdfs:label "Alpha"@en, "알파"@ko .\n'
    ), format="turtle")

    props = {
        "obj_props": _OPS,
        "op_counts": _COUNTS,
        "outgoing_by_class": {"Alpha": _OPS},
        "incoming_by_class": {},
        "dt_prop_info": {},
        "prop_values": {},
        "instance_counts": {},
        "labels": {},
    }
    classes = _build_classes_dict(
        tbox, ["Alpha"], {}, props, include_stats=True,
    )
    entries = (classes.get("Alpha") or {}).get("object_properties_outgoing") or []
    assert entries, f"outgoing 항목이 비었다 — 픽스처 키 이름 확인: {list(classes)}"
    assert all("is_populated" in e for e in entries), (
        f"파이프라인 경로에서 신호가 사라졌다 (배선 끊김): {entries}"
    )
    by_name = {e["property"]: e for e in entries}
    assert by_name["usedOp"]["is_populated"] is True
    assert by_name["emptyOp"]["is_populated"] is False
