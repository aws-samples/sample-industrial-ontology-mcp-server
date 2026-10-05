"""딕셔너리 카운트의 출처 명시 + OP provenance 교정 (rank 7 / 11 / 12).

## rank 7 — ``is_populated`` 이 어느 그래프 기준인지 산출물에 없었다

``use_inferred`` 는 ``triple_count`` / ``instance_count`` / ``is_populated`` 의
**의미를 바꾸는데** 메타데이터에 기록이 없었다. 실측 (2026-08-30 배포 딕셔너리):
OP 108개가 ``is_populated: true`` 인데 ``a_box.ttl`` 기준으로는 55개다. 차이는
추론이 ``owl:inverseOf`` 로 파생한 역방향 링크다::

    equipmentHasGasEnergy   triple_count: 720   ← 추론 그래프
                            a_box.ttl        : 0   ← 소비자가 질의하는 곳

딕셔너리의 소비자는 코드가 아니라 **LLM** 이다. 어느 그래프에 그 트리플이 있는지
모르면 0행을 받고, 그 0행이 오류 없이 "정답처럼" 반환된다.

**"추론 전" 의 기준은 a_box.ttl 이 아니다.** 실제 질의 경로
(``load_graph``)는 A-Box + master_data + tacit 을 합친다. ``a_box.ttl`` 만 세면
tacit 이 채우는 관계가 0으로 잡혀 정상 관계에 ``requires_inferred_graph`` 를 잘못
붙인다 — 처음 그렇게 썼다가 52건 표시를 얻었고, 실제 추론 전용은 **3건**이다.

## rank 12 — 한 쌍을 두 컬럼이 잇는데 사전순 첫 컬럼이 전부에 붙었다

``Transportation.csv`` 는 ``Origin_Warehouse`` / ``Destination_Warehouse`` 두
컬럼이 모두 ``Transportation → WarehouseMaster`` 를 잇는다.
``csv_fk_pair_columns`` 가 사전순 첫 값만 남기므로 **Origin 방향 OP 3개가
Destination 컬럼을 출처로** 갖게 됐다.

잘못된 provenance 는 올바른 것과 **게이트 상 구분되지 않는다** — ``step_12e`` 는
존재 여부만 세고 그 컬럼이 CSV 에 실재하므로 resolvable 로도 집계된다.

## rank 11 — S7 이 자기 입력(T-Box)을 되쓴다

없애면 후속 도구가 A-Box 가 쓰는 DP 를 못 보므로 유지하되 **감사 가능**하게 했다.
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest
from rdflib import OWL, RDF, Graph, Literal, URIRef
from rdflib.namespace import DCTERMS

from domain.namespaces import DOMAIN_NS
from tools.quality_steps import step_15d_op_source_backfill as step_15d
from tools.quality_steps._base import StepContext

NS = str(DOMAIN_NS)
TBOX = pathlib.Path("data/generated/tbox/t_box.ttl")
DICT = pathlib.Path("data/generated/semantic_dictionary.json")


def _tbox() -> Graph:
    if not TBOX.exists():
        pytest.skip("T-Box 없음")
    g = Graph()
    g.parse(str(TBOX), format="turtle")
    return g


# ── rank 7: counts_source 명시 ───────────────────────────────────────────


def test_metadata_declares_counts_source():
    """THE REGRESSION: 카운트가 어느 그래프에서 나왔는지 산출물이 말한다."""
    if not DICT.exists():
        pytest.skip("딕셔너리 없음")
    meta = json.loads(DICT.read_text(encoding="utf-8"))["metadata"]

    assert meta.get("counts_source") in ("abox", "inferred", "none"), meta
    assert meta.get("counts_source_note"), "소비자용 설명이 없다"
    if meta["counts_source"] != "none":
        assert meta.get("counts_source_file")


@pytest.mark.parametrize(
    ("include_stats", "use_inferred", "expected"),
    [
        (True, True, "inferred"),
        (True, False, "abox"),
        (False, False, "none"),
    ],
)
def test_counts_source_reflects_arguments(include_stats, use_inferred, expected):
    from tools.semantic_dictionary import _build_metadata

    meta = _build_metadata(
        Graph(), Graph(), include_stats, use_inferred=use_inferred,
    )

    assert meta["counts_source"] == expected


def test_inference_only_relations_are_flagged():
    """추론에만 있는 관계에 ``requires_inferred_graph`` 가 붙는가."""
    if not DICT.exists():
        pytest.skip("딕셔너리 없음")
    d = json.loads(DICT.read_text(encoding="utf-8"))
    if d["metadata"].get("counts_source") != "inferred":
        pytest.skip("추론 기준 딕셔너리가 아니다")
    ops = d["object_properties"]

    flagged = {k: v for k, v in ops.items() if v.get("requires_inferred_graph")}

    for name, entry in flagged.items():
        assert entry["triple_count"] > 0, name
        assert entry["pre_inference_triple_count"] == 0, name
        assert entry.get("query_note"), f"{name}: 소비자용 안내가 없다"


def test_flagged_relations_match_ground_truth():
    """오탐 0 — 추론 전에도 있는 관계에 플래그가 붙으면 안 된다.

    ground truth 는 ``load_graph(use_inferred=False)`` 다 (실제 질의 경로).
    """
    if not DICT.exists():
        pytest.skip("딕셔너리 없음")
    d = json.loads(DICT.read_text(encoding="utf-8"))
    if d["metadata"].get("counts_source") != "inferred":
        pytest.skip("추론 기준 딕셔너리가 아니다")

    from domain.namespaces import DOMAIN_NS as _NS
    from domain.tbox_utils import load_graph
    from tools.semantic_dictionary import _single_pass_abox_stats

    ops = d["object_properties"]
    graph, _tacit = load_graph(use_inferred=False)
    _ic, _pv, truth = _single_pass_abox_stats(graph, str(_NS), {}, set(ops))

    false_positives = [
        k for k, v in ops.items()
        if v.get("requires_inferred_graph") and truth.get(k, 0) > 0
    ]
    assert not false_positives, (
        f"추론 전에도 있는 관계가 추론 전용으로 표시됐다: {false_positives}"
    )


def test_pre_inference_count_uses_the_real_query_path():
    """``pre_inference_triple_count`` 가 tacit·master 를 포함하는가.

    ``a_box.ttl`` 만 세면 tacit 이 채우는 관계가 0 이 되어 오탐이 대량 발생한다
    (실측: 52건 vs 실제 3건).
    """
    if not DICT.exists():
        pytest.skip("딕셔너리 없음")
    d = json.loads(DICT.read_text(encoding="utf-8"))
    ops = d["object_properties"]
    with_counts = {
        k: v["pre_inference_triple_count"] for k, v in ops.items()
        if v.get("pre_inference_triple_count") is not None
    }
    if not with_counts:
        pytest.skip("pre_inference 카운트가 없다 (abox 기준 딕셔너리)")

    from domain.namespaces import DOMAIN_NS as _NS
    from domain.tbox_utils import load_graph
    from tools.semantic_dictionary import _single_pass_abox_stats

    graph, _t = load_graph(use_inferred=False)
    _ic, _pv, truth = _single_pass_abox_stats(graph, str(_NS), {}, set(ops))

    mismatched = {
        k: (v, truth.get(k, 0)) for k, v in with_counts.items()
        if v != truth.get(k, 0)
    }
    assert not mismatched, f"실제 질의 경로와 불일치: {list(mismatched.items())[:5]}"


# ── rank 12: OP provenance 교정 ──────────────────────────────────────────


def test_pick_column_disambiguates_by_op_name():
    """공유 토큰(Warehouse)이 아니라 판별 토큰(Origin/Destination)으로 고른다."""
    cols = ["Destination_Warehouse", "Origin_Warehouse"]

    assert step_15d._pick_column_for_op(
        "transportationHasOriginWarehouse", cols) == "Origin_Warehouse"
    assert step_15d._pick_column_for_op(
        "hasDestinationWarehouse", cols) == "Destination_Warehouse"
    assert step_15d._pick_column_for_op(
        "warehouseIsOriginOfTransportation", cols) == "Origin_Warehouse"


def test_pick_column_returns_none_when_undecidable():
    """판별 불가면 None — 틀린 출처는 없는 출처보다 나쁘다."""
    cols = ["Destination_Warehouse", "Origin_Warehouse"]

    assert step_15d._pick_column_for_op("transportationHasWarehouse", cols) is None
    assert step_15d._pick_column_for_op("somethingElse", cols) is None


def test_single_candidate_is_returned_directly():
    assert step_15d._pick_column_for_op("anyName", ["Item_Code"]) == "Item_Code"
    assert step_15d._pick_column_for_op("anyName", []) is None


def test_wrong_op_source_is_corrected():
    """THE REGRESSION: 이미 박힌 잘못된 출처를 교정한다.

    생성 지점만 고치면 파일에 남은 값은 ``already`` 로 집계돼 영구히 남는다.
    """
    g = _tbox()
    wrong = URIRef(NS + "transportationHasOriginWarehouse")
    if (wrong, RDF.type, OWL.ObjectProperty) not in g:
        pytest.skip("대상 OP 없음 (T-Box 세대 차이)")
    # 결함을 되살린다 (이미 교정된 상태일 수 있다).
    for src in list(g.objects(wrong, DCTERMS.source)):
        g.remove((wrong, DCTERMS.source, src))
    g.add((wrong, DCTERMS.source, Literal("Destination_Warehouse")))

    result = step_15d.apply(g, StepContext(domain_ns=NS))

    assert result.stats["op_source_corrected"] >= 1, result.stats
    assert [str(x) for x in g.objects(wrong, DCTERMS.source)] == ["Origin_Warehouse"]


def test_destination_ops_are_not_touched():
    """NEGATIVE 방향 — 올바른 출처는 건드리지 않는다 (과잉 교정 방지)."""
    g = _tbox()
    right = URIRef(NS + "hasDestinationWarehouse")
    if (right, RDF.type, OWL.ObjectProperty) not in g:
        pytest.skip("대상 OP 없음")
    before = sorted(str(x) for x in g.objects(right, DCTERMS.source))
    if before != ["Destination_Warehouse"]:
        pytest.skip(f"픽스처 전제 불일치: {before}")

    step_15d.apply(g, StepContext(domain_ns=NS))

    assert sorted(str(x) for x in g.objects(right, DCTERMS.source)) == before


def test_deployed_tbox_op_sources_are_direction_correct():
    """배포 산출물 실측 — Origin OP 가 Origin 컬럼을 가리키는가."""
    g = _tbox()
    wrong = []
    for op in g.subjects(RDF.type, OWL.ObjectProperty):
        if not (isinstance(op, URIRef) and str(op).startswith(NS)):
            continue
        local = str(op)[len(NS):]
        srcs = [str(x) for x in g.objects(op, DCTERMS.source)]
        if not srcs:
            continue
        low = local.lower()
        for src in srcs:
            if "origin" in low and "destination" in src.lower():
                wrong.append(f"{local} ← {src}")
            if "destination" in low and "origin" in src.lower():
                wrong.append(f"{local} ← {src}")
    assert not wrong, f"방향이 뒤바뀐 provenance: {wrong}"


def test_pair_all_columns_keeps_every_competing_column():
    """``csv_fk_pair_all_columns`` 가 경쟁 컬럼을 버리지 않는가."""
    from domain.graph_utils import csv_fk_pair_all_columns, csv_fk_pair_columns

    all_cols = csv_fk_pair_all_columns()
    single = csv_fk_pair_columns()
    if all_cols is None or single is None:
        pytest.skip("CSV FK 판정 불가")

    key = ("transportation", "warehousemaster")
    if key not in all_cols:
        pytest.skip("대상 쌍 없음 (도메인 차이)")
    assert len(all_cols[key]) >= 2, all_cols[key]
    # 단일 맵은 그중 하나만 갖는다 — 그것이 이 결함의 근원이었다.
    assert single[key] in all_cols[key]


def test_idempotent():
    g = _tbox()
    first = step_15d.apply(g, StepContext(domain_ns=NS)).stats
    second = step_15d.apply(g, StepContext(domain_ns=NS)).stats

    assert second["op_source_corrected"] == 0
    assert second["op_source_dropped_wrong"] == 0
    assert first is not second


# ── rank 11: S7 의 T-Box 되쓰기 감사 ─────────────────────────────────────


def test_injection_audit_is_produced(monkeypatch, tmp_path):
    """T-Box 되쓰기가 감사 기록을 남기는가 — 로그만으로는 추적 불가."""
    import glob

    from tools.abox_generation import (
        _inject_common_dps_into_tbox_memory,
        _parse_tbox,
    )

    if not TBOX.exists():
        pytest.skip("T-Box 없음")
    monkeypatch.setenv("ABOX_WRITE_TBOX_INJECTIONS", "false")
    monkeypatch.setattr("config.GENERATED_DIR", str(tmp_path / "generated"))

    g = _tbox()
    target = URIRef(NS + "alarmEventsTimestamp")
    if (target, RDF.type, OWL.DatatypeProperty) not in g:
        pytest.skip("대상 DP 없음")
    for p, o in list(g.predicate_objects(target)):
        g.remove((target, p, o))
    ttl = g.serialize(format="turtle")

    _new_ttl, _info, stats = _inject_common_dps_into_tbox_memory(
        ttl, _parse_tbox(ttl), sorted(glob.glob("data/source/rawdata/*.csv")), None,
    )

    assert stats and stats["injected"] > 0
    audit = stats.get("injection_audit")
    assert audit, "injection_audit 이 없다 — 되쓰기가 감사되지 않는다"
    assert audit["injected_dps"]
    assert audit["tbox_file_written"] is False  # env off
    assert audit["reason"]


def test_zero_injection_is_also_audited(monkeypatch, tmp_path):
    """주입 0건도 기록한다 — "주입 없음" 과 "미측정" 을 구분해야 한다.

    2026-08-30: 주입할 것이 없으면 ``None`` 을 반환했고, loss manifest 의
    ``if common_dp_injection:`` 이 그것을 falsy 로 걸러 **키를 아예 생략**했다.
    배포 manifest 실측 — 최상위 키 6개에 ``common_dp_injection`` 없음. 그래서
    ``_inject_common_dps_into_tbox_memory`` docstring 이 안내하는 "loss manifest 의
    tbox_injection_audit 을 보라" 가 거짓이었고, S7 이 T-Box 를 썼는지 산출물로
    판정할 수 없었다 (이 리포의 "필드 부재 ≠ 값 0" 함정).
    """
    import glob

    from tools.abox_generation import (
        _inject_common_dps_into_tbox_memory,
        _parse_tbox,
    )

    if not TBOX.exists():
        pytest.skip("T-Box 없음")
    monkeypatch.setenv("ABOX_WRITE_TBOX_INJECTIONS", "false")
    monkeypatch.setattr("config.GENERATED_DIR", str(tmp_path / "generated"))

    # 배포 T-Box 는 이미 모든 CSV 컬럼 DP 를 갖고 있어 주입 계획이 비어야 한다.
    ttl = _tbox().serialize(format="turtle")
    _new_ttl, _info, stats = _inject_common_dps_into_tbox_memory(
        ttl, _parse_tbox(ttl), sorted(glob.glob("data/source/rawdata/*.csv")), None,
    )
    if stats and stats.get("injected"):
        pytest.skip("배포 T-Box 에 주입 대상이 있어 0건 경로가 아니다")

    assert stats is not None, (
        "주입 0건일 때 None 을 반환했다 — loss manifest 에서 키가 사라진다"
    )
    audit = stats["injection_audit"]
    assert audit["injected_count"] == 0
    assert audit["injected_dps"] == []
    assert audit["tbox_file_written"] is False
    assert audit["reason"], "0건인 이유가 기록되지 않았다"


def test_loss_manifest_records_zero_injection():
    """배선 계약: manifest 가 0건 audit 을 실제로 담는가.

    ``is not None`` 이 아니라 truthiness 로 걸러면 이 테스트가 깨진다 — 그것이
    원래 결함의 기전이다.
    """
    from tools.abox_generation import _build_loss_manifest

    zero = {
        "injected": 0, "by_name": {},
        "injection_audit": {"injected_count": 0, "tbox_file_written": False},
    }
    manifest = _build_loss_manifest(
        {}, {}, total_rows=10, total_cols=5, common_dp_injection=zero,
    )
    assert "common_dp_injection" in manifest, (
        "주입 0건 audit 이 manifest 에서 사라졌다 — truthiness 로 걸렀다"
    )
    assert manifest["common_dp_injection"]["injection_audit"]["injected_count"] == 0

    # **빈 dict** 도 통과해야 한다. 위 ``zero`` 는 키가 있어 truthy 이므로
    # ``if common_dp_injection:`` 으로 되돌려도 통과한다 (실측: mutation 생존).
    # 진짜 판별력은 falsy 값에서 나온다 — ``{}`` 는 "측정했고 결과가 비었다" 이고
    # ``None`` 은 "측정 안 함" 이다. 두 상태를 구분하는 것이 이 필드의 목적이다.
    empty = _build_loss_manifest(
        {}, {}, total_rows=10, total_cols=5, common_dp_injection={},
    )
    assert "common_dp_injection" in empty, (
        "빈 dict 를 falsy 로 걸렀다 — '측정했고 비었다' 가 '미측정' 과 같아진다"
    )
    assert empty["common_dp_injection"] == {}

    # 반대: 진짜 미측정(None)은 키를 만들지 않아야 한다 (두 상태를 구분한다).
    unmeasured = _build_loss_manifest(
        {}, {}, total_rows=10, total_cols=5, common_dp_injection=None,
    )
    assert "common_dp_injection" not in unmeasured, (
        "미측정과 0건이 같은 표현이 됐다"
    )


def test_injected_dp_carries_provenance_note(monkeypatch, tmp_path):
    """주입된 DP 에 출처가 각인되는가 — S2 생성분과 구분해야 한다."""
    import glob

    from rdflib.namespace import SKOS

    from tools.abox_generation import (
        _inject_common_dps_into_tbox_memory,
        _parse_tbox,
    )

    if not TBOX.exists():
        pytest.skip("T-Box 없음")
    monkeypatch.setenv("ABOX_WRITE_TBOX_INJECTIONS", "false")
    monkeypatch.setattr("config.GENERATED_DIR", str(tmp_path / "generated"))

    g = _tbox()
    target = URIRef(NS + "alarmEventsTimestamp")
    if (target, RDF.type, OWL.DatatypeProperty) not in g:
        pytest.skip("대상 DP 없음")
    for p, o in list(g.predicate_objects(target)):
        g.remove((target, p, o))
    ttl = g.serialize(format="turtle")

    new_ttl, _info, _stats = _inject_common_dps_into_tbox_memory(
        ttl, _parse_tbox(ttl), sorted(glob.glob("data/source/rawdata/*.csv")), None,
    )

    out = Graph()
    out.parse(data=new_ttl, format="turtle")
    notes = [str(o) for o in out.objects(target, SKOS.note)]
    assert notes, "주입 DP 에 출처 note 가 없다"
    assert "on-demand" in notes[0]


def test_write_disabled_does_not_touch_tbox_file(monkeypatch, tmp_path):
    """``ABOX_WRITE_TBOX_INJECTIONS=false`` 면 파일 mtime 이 안 바뀐다."""
    import glob

    from tools.abox_generation import (
        _inject_common_dps_into_tbox_memory,
        _parse_tbox,
    )

    if not TBOX.exists():
        pytest.skip("T-Box 없음")
    monkeypatch.setenv("ABOX_WRITE_TBOX_INJECTIONS", "false")
    monkeypatch.setattr("config.GENERATED_DIR", str(tmp_path / "generated"))
    before = os.path.getmtime(TBOX)

    g = _tbox()
    target = URIRef(NS + "alarmEventsTimestamp")
    if (target, RDF.type, OWL.DatatypeProperty) not in g:
        pytest.skip("대상 DP 없음")
    for p, o in list(g.predicate_objects(target)):
        g.remove((target, p, o))
    ttl = g.serialize(format="turtle")
    _inject_common_dps_into_tbox_memory(
        ttl, _parse_tbox(ttl), sorted(glob.glob("data/source/rawdata/*.csv")), None,
    )

    assert os.path.getmtime(TBOX) == before, "쓰기를 껐는데 파일이 변경됐다"
