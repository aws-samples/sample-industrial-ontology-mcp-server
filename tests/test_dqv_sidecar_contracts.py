"""DQV sidecar 가 상류 4개 도구와 맺은 **키 계약** 회귀 가드.

배경 (2026-08-08 실측): ``export_quality_dqv`` 와 5개 ``_extract_from_*`` 헬퍼는
실행된 라인이 0이었다. 각 추출기는 다른 도구의 출력 키를 하드코딩하고
(``metrics[k]["value"]`` / ``axis_scores[a]["score"]`` / ``dimensions[d]["score"]`` /
``overall_1_to_5``), 5개 수집기 전부가 호출을 ``try/except`` 로 감싸 실패를
``logger.debug`` 로만 남긴다.

그래서 상류 도구가 키 하나를 개명하면 **그 메트릭 그룹이 조용히 사라진다** —
DQV sidecar 는 성공으로 보고되고 내용만 비어 있다. 문자열로 모듈을 잇는 계약은
타입 검사가 잡지 못하므로 테스트로만 고정된다.

작은 T-Box/A-Box와 출력 경로를 테스트별 임시 디렉터리에 구성한다.
도구 오류, 그룹 누락, RDF 측정값 손실은 환경 사유로 skip하지 않고 실패시킨다.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from rdflib import RDF, Graph


def _call(module_path: str, func_name: str) -> dict:
    module = __import__(module_path, fromlist=[func_name])
    payload = json.loads(getattr(module, func_name)())
    assert payload.get("success") is True, f"{func_name}: {payload}"
    return payload


@pytest.fixture
def quality_inputs(tmp_path, monkeypatch, sample_tbox_file, sample_abox_ttl):
    from tools import (
        dqv_sidecar,
        farber_dimensions,
        foops_fair,
        local_artifacts,
        ontology_quality,
        tbox_metrics,
        validation_core,
    )

    tbox_dir = tmp_path / "tbox"
    tbox_dir.mkdir()
    tbox = tbox_dir / "t_box.ttl"
    tbox.write_text(sample_tbox_file.read_text(encoding="utf-8"), encoding="utf-8")
    abox = tmp_path / "a_box.ttl"
    abox.write_text(sample_abox_ttl, encoding="utf-8")
    inferred = tmp_path / "inferred.ttl"
    inferred.write_text(sample_abox_ttl, encoding="utf-8")
    rawdata = tmp_path / "rawdata"
    rawdata.mkdir()
    (rawdata / "EquipmentMaster.csv").write_text("equipmentID\nEQ001\n", encoding="utf-8")
    reports = tmp_path / "reports"

    for module in (
        dqv_sidecar, farber_dimensions, foops_fair, local_artifacts,
        ontology_quality, tbox_metrics, validation_core,
    ):
        monkeypatch.setattr(module, "TBOX_PATH", str(tbox))
    for module in (farber_dimensions, foops_fair):
        monkeypatch.setattr(module, "GENERATED_REPORTS_DIR", str(reports))
    monkeypatch.setattr(foops_fair, "GENERATED_TBOX_DIR", str(tbox_dir))
    monkeypatch.setattr(farber_dimensions, "ABOX_PATH", str(abox))
    monkeypatch.setattr(farber_dimensions, "INFERRED_PATH", str(inferred))
    monkeypatch.setattr(farber_dimensions, "SOURCE_RAWDATA_DIR", str(rawdata))
    monkeypatch.setattr(tbox_metrics, "TBOX_BASELINE_PATH", str(tbox_dir / "no_baseline.ttl"))
    return tmp_path


def test_tbox_metrics_exposes_metrics_value(quality_inputs):
    """``_extract_from_tbox_metrics`` 는 ``metrics[k]["value"]`` 를 읽는다."""
    from tools.tbox_metrics import measure_tbox_metrics

    payload = json.loads(measure_tbox_metrics())
    assert payload.get("success") is True, payload
    metrics = payload.get("metrics")
    assert isinstance(metrics, dict) and metrics, "metrics 섹션이 없다"
    key = next(iter(metrics))
    assert isinstance(metrics[key], dict) and "value" in metrics[key], (
        f"metrics['{key}'] 에 'value' 가 없다 — DQV 가 이 그룹을 조용히 버린다"
    )


def test_fair_score_exposes_axis_scores_score(quality_inputs):
    """``_extract_from_foops_fair`` 는 ``axis_scores[a]["score"]`` 를 읽는다."""
    payload = _call("tools.foops_fair", "evaluate_fair_score")
    axes = payload.get("axis_scores")
    assert isinstance(axes, dict) and axes, "axis_scores 섹션이 없다"
    axis = next(iter(axes))
    assert isinstance(axes[axis], dict) and "score" in axes[axis], (
        f"axis_scores['{axis}'] 에 'score' 가 없다"
    )


def test_farber_exposes_dimensions_score(quality_inputs):
    """``_extract_from_farber`` 는 ``dimensions[d]["score"]`` 를 읽는다."""
    payload = _call("tools.farber_dimensions", "evaluate_farber_dimensions")
    dims = payload.get("dimensions")
    assert isinstance(dims, dict) and dims, "dimensions 섹션이 없다"
    dim = next(iter(dims))
    assert isinstance(dims[dim], dict) and "score" in dims[dim], (
        f"dimensions['{dim}'] 에 'score' 가 없다"
    )


def test_oquare_exposes_overall_1_to_5(quality_inputs):
    """``_extract_from_oquare`` 는 ``overall_1_to_5`` 를 읽는다."""
    payload = _call("tools.ontology_quality", "evaluate_oquare")
    assert "overall_1_to_5" in payload, (
        "overall_1_to_5 키가 사라지면 OQuare 그룹이 sidecar 에서 통째로 빠진다"
    )


def test_export_quality_dqv_produces_measurements(quality_inputs):
    """THE REGRESSION: sidecar 가 실제로 측정값을 담아야 한다 (빈 껍데기 금지).

    수집기들이 모두 ``try/except`` + ``logger.debug`` 라, 계약이 깨지면 성공
    응답에 내용만 비어서 돌아온다.
    """
    from tools.dqv_sidecar import _QM_NS, DQV, export_quality_dqv
    from tools.validation_support.checks.linked_data import (
        check_interlinking,
        check_licensing,
        check_understandability,
    )

    metrics = _call("tools.tbox_metrics", "measure_tbox_metrics")["metrics"]
    fair = _call("tools.foops_fair", "evaluate_fair_score")
    farber = _call("tools.farber_dimensions", "evaluate_farber_dimensions")["dimensions"]
    oquare = _call("tools.ontology_quality", "evaluate_oquare")
    tbox = Graph().parse(quality_inputs / "tbox" / "t_box.ttl", format="turtle")
    expected = {
        **{key: metrics[key]["value"] for key in (
            "dit", "noc_avg", "rr", "ar", "annotation_completeness", "axiom_richness",
        )},
        "fair_overall": fair["overall_score"],
        "fair_findable": fair["axis_scores"]["Findable"]["score"],
        "fair_accessible": fair["axis_scores"]["Accessible"]["score"],
        "fair_interoperable": fair["axis_scores"]["Interoperable"]["score"],
        "fair_reusable": fair["axis_scores"]["Reusable"]["score"],
        "farber_timeliness": farber["timeliness"]["score"],
        "farber_amount_of_data": farber["amount_of_data"]["score"],
        "farber_trustworthiness": farber["trustworthiness"]["score"],
        "farber_verifiability": farber["verifiability"]["score"],
        "farber_representational_consistency": farber["representational_consistency"]["score"],
        "oquare_overall": oquare["overall_1_to_5"],
        "zaveri_interlinking": check_interlinking(tbox)["score"],
        "zaveri_licensing": check_licensing(tbox)["score"],
        "zaveri_understandability": check_understandability(tbox)["score"],
    }

    result = json.loads(export_quality_dqv())
    assert result.get("success") is True, result
    assert set(expected) == set(result["metrics_exported"])
    assert {
        "measure_tbox_metrics", "evaluate_fair_score",
        "evaluate_farber_dimensions", "evaluate_oquare",
        "linked_data.checks",
    } == set(result["sources"])
    path = Path(result["output_path"])
    assert path == quality_inputs / "quality" / "dqv_sidecar.ttl"
    graph = Graph().parse(path, format="turtle")
    measurements = set(graph.subjects(RDF.type, DQV.QualityMeasurement))
    assert len(measurements) == result["measurement_count"]
    for metric_id, expected_value in expected.items():
        nodes = set(graph.subjects(DQV.isMeasurementOf, _QM_NS[f"metric/{metric_id}"]))
        assert len(nodes) == 1, f"{metric_id} 측정값이 누락되거나 중복됐다"
        value = graph.value(next(iter(nodes)), DQV.value)
        assert value is not None, metric_id
        assert float(value) == pytest.approx(expected_value), metric_id


def test_extractors_are_callable_with_empty_input():
    """추출기가 빈 입력에서 예외 없이 빈 dict 를 반환한다 (계약 방어)."""
    from tools import dqv_sidecar as dqv

    for name in ("_extract_from_tbox_metrics", "_extract_from_foops_fair",
                 "_extract_from_farber", "_extract_from_oquare"):
        fn = getattr(dqv, name)
        assert fn({}) == {}, f"{name}({{}}) 가 빈 dict 가 아니다"
