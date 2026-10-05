"""버킷 조인으로 만든 추정 관계가 산출물에 추정임을 명시하는가.

2026-08-28 실측. ``tacit_rules.json`` 의 ``shared_column_join`` 6개는
``_confidence: medium`` + "SME 확인 필요" 를 기록하고 있었지만 **그 신뢰도가 어느
산출물에도 전달되지 않았다** — TTL 에는 규칙 이름 한 줄, 딕셔너리 전문에는
``tacit`` / ``estimated`` / ``confidence`` 문자열이 0회.

## 피해는 0행이 아니라 그럴듯한 오답이다

``hasStackEquipment`` 는 이름이 "굴뚝 설비" 이고 딕셔너리 설명은 "대기 배출이 측정된
굴뚝 또는 배출 설비를 연결한다" 인데, 실제 트리플은 같은 ``Location`` 을 공유하는
설비다::

    triples 11,160 / subjects 1,560 / fanout 7.15 / distinct objects 50 = 전체 설비
    Equipment_Master.Location = 7종 (BF_Area 8, SM_Area 7, ...) → 평균 7.1대/구역

즉 "배출 최다 설비 top-5" 는 같은 구역 7대를 동점으로 받아 임의 절단된다. 질의가
0행이 아니라 답을 주기 때문에 오답이 검출되지 않는다.

## 더 정확한 조인으로 대체할 수 없다

``Air_Emission_Monitoring.Stack_ID`` (Stack_1~5) 를 설비로 잇는 컬럼이 CSV 에 없다
(실측 헤더: Equipment_ID, Equipment_Name, Equipment_Type, Location, Manufacturer).
정확한 매핑은 **데이터에 없다**. 그래서 조치는 추정을 지우거나 값을 데이터에 맞추는
것이 아니라(그건 지표 매수다) **추정임을 명시**하는 것이다.

## 이 테스트의 방향

"표식이 있다" 만 주장하면 전체 OP 에 표식을 붙여도 통과한다. 세 축을 고정한다:

* 표시 — 버킷 조인 OP 에 provenance 와 설명 표식이 붙는다
* 경계 — FK 기반 OP 에는 **붙지 않는다** (과잉 표시는 신호를 무의미하게 만든다)
* 무해 — 트리플은 하나도 바뀌지 않는다 (데이터 조작 금지)
"""
from __future__ import annotations

import json
import pathlib

import pytest

from tools.semantic_dictionary import (
    _build_object_properties_dict,
    _estimated_op_provenance,
)

TACIT_COLOC = pathlib.Path("data/source/tacit/tacit_colocation.ttl")
DICT_PATH = pathlib.Path("data/generated/semantic_dictionary.json")


def _prop(name: str) -> dict:
    return {
        "name": name, "label_en": name, "label_ko": name,
        "description_en": "connects things", "description_ko": "무언가를 연결한다",
        "domains": ["A"], "ranges": ["B"],
        "inverseOf": None, "subPropertyOf": None, "transitive": False,
    }


# ── 표시: 추정 관계에 신호가 붙는다 ─────────────────────────────────────


def test_bucket_join_ops_are_detected_from_rules():
    """THE REGRESSION: shared_column_join 규칙의 OP 를 규칙 파일에서 읽어낸다."""
    estimated = _estimated_op_provenance()

    assert estimated, "추정 OP 를 하나도 못 읽었다 — 규칙 파일 형식이 바뀌었나?"
    for name, meta in estimated.items():
        assert meta["derivation"] == "tacit_bucket_join", name
        assert meta["confidence"], name
        assert meta["caveat"], f"{name}: 경고 문구가 없다"


def test_provenance_is_attached_to_entry():
    estimated = _estimated_op_provenance()
    target = next(iter(estimated))

    entry = _build_object_properties_dict([_prop(target)], {target: 100})[target]

    assert entry["provenance"]["derivation"] == "tacit_bucket_join"
    assert entry["provenance"]["bucket_column"], "버킷 컬럼이 비었다"


def test_description_carries_the_marker():
    """설명문에도 표식이 필요하다 — provenance 만으론 설명이 정반대를 주장한다.

    실측: hasStackEquipment 의 description_ko 는 "굴뚝 ... 설비를 연결한다" 인데
    실제 트리플은 같은 구역 설비다. LLM 은 설명문을 먼저 읽는다.
    """
    estimated = _estimated_op_provenance()
    target = next(iter(estimated))

    entry = _build_object_properties_dict([_prop(target)], {target: 100})[target]

    assert "[추정: 버킷 조인]" in entry["description_ko"]
    assert "[ESTIMATED: bucket join]" in entry["description_en"]
    assert "무언가를 연결한다" in entry["description_ko"], "원 설명이 사라졌다"


def test_marker_is_not_duplicated_on_regeneration():
    """두 번 생성해도 표식이 두 번 붙지 않는다 (멱등)."""
    estimated = _estimated_op_provenance()
    target = next(iter(estimated))

    prop = _prop(target)
    first = _build_object_properties_dict([prop], {target: 1})[target]
    prop["description_ko"] = first["description_ko"]
    prop["description_en"] = first["description_en"]
    second = _build_object_properties_dict([prop], {target: 1})[target]

    assert second["description_ko"].count("[추정: 버킷 조인]") == 1
    assert second["description_en"].count("[ESTIMATED: bucket join]") == 1


# ── 경계: FK 기반 OP 는 건드리지 않는다 (NEGATIVE 방향) ─────────────────


def test_fk_based_op_gets_no_marker():
    """추정이 아닌 OP 에는 표식이 없다.

    전체에 붙이면 신호가 무의미해진다 — "추정" 이 모든 것을 뜻하면 아무것도 뜻하지 않는다.
    """
    entry = _build_object_properties_dict(
        [_prop("hasAlarmTag")], {"hasAlarmTag": 565},
    )["hasAlarmTag"]

    assert "provenance" not in entry
    assert entry["description_ko"] == "무언가를 연결한다", "설명이 오염됐다"


def test_only_shared_column_join_strategy_qualifies():
    """simple_join / fk_lookup_table 등 FK 전략은 추정이 아니다."""
    from domain.rules_paths import rules_path

    with open(rules_path("tacit_rules.json"), encoding="utf-8") as fh:
        mappings = json.load(fh)["mappings"]

    estimated = set(_estimated_op_provenance())
    for rule in mappings:
        op, strategy = rule.get("op"), rule.get("strategy")
        if not op:
            continue
        if strategy == "shared_column_join":
            assert op in estimated, f"{op} ({strategy}) 가 표시되지 않았다"
        else:
            assert op not in estimated, f"{op} ({strategy}) 를 추정으로 표시했다"


def test_missing_rules_file_yields_no_signal(monkeypatch):
    """규칙 파일을 못 읽으면 빈 dict — 거짓 신호보다 무신호가 낫다."""
    import tools.semantic_dictionary as sd

    monkeypatch.setattr(
        "domain.rules_paths.rules_path", lambda _n: "/nonexistent/tacit_rules.json",
    )
    assert sd._estimated_op_provenance() == {}


# ── 무해: 데이터를 바꾸지 않는다 ────────────────────────────────────────


def test_tacit_ttl_carries_caveat_comments():
    """생성된 TTL 에 confidence / ESTIMATED 주석이 있는가."""
    if not TACIT_COLOC.exists():
        pytest.skip("tacit_colocation.ttl 없음")

    text = TACIT_COLOC.read_text(encoding="utf-8")

    assert "confidence: medium" in text, "신뢰도 주석이 없다"
    assert "ESTIMATED: bucket join" in text, "추정 경고가 없다"
    assert "bucket column:" in text, "버킷 컬럼 표시가 없다"


def test_caveat_comments_do_not_change_triples():
    """주석 추가가 트리플을 건드리지 않았는가 — 데이터 조작 금지.

    추정을 "고치려고" 값을 데이터에 맞추는 것은 지표 매수다. 이 테스트가 그 경계를
    지킨다: 표식은 메타데이터이고 사실 자체는 그대로여야 한다.
    """
    if not TACIT_COLOC.exists():
        pytest.skip("tacit_colocation.ttl 없음")

    from rdflib import Graph

    graph = Graph()
    graph.parse(str(TACIT_COLOC), format="turtle")

    assert len(graph) == 32688, (
        f"co-location 트리플 수가 {len(graph)} 다 — 32,688 에서 바뀌었다면 "
        "주석 작업이 데이터를 건드렸거나 규칙이 변경됐다"
    )


def test_deployed_dictionary_exposes_the_signal():
    """배포 딕셔너리에 실제로 실렸는가 — 생성기만 고치고 산출물이 안 바뀌는 사고 방지."""
    if not DICT_PATH.exists():
        pytest.skip("딕셔너리 없음")

    ops = json.loads(DICT_PATH.read_text(encoding="utf-8"))["object_properties"]
    marked = [n for n, e in ops.items() if isinstance(e, dict) and e.get("provenance")]

    assert len(marked) == len(_estimated_op_provenance()), (
        f"배포 딕셔너리에 {len(marked)}건만 표시됐다 — 재생성이 필요하다"
    )
