"""W3C DQV (Data Quality Vocabulary) sidecar 직렬화.

근거: Albertoni, Isaac. "Data on the Web Best Practices: Data Quality
Vocabulary." W3C Working Group Note, 15 Dec 2016.
https://www.w3.org/TR/vocab-dqv/

품질 측정 결과(farber_dimensions, foops_fair, measure_tbox_metrics,
evaluate_oquare, Zaveri linked_data checks)를 단일 TTL sidecar 로
직렬화 → SPARQL 엔드포인트 적재 → 트렌드 쿼리 가능.

DQV 핵심 클래스:
- dqv:QualityMetric     — metric 정의 (e.g. "DIT", "FAIR overall")
- dqv:QualityMeasurement — 실제 측정값 (value + computedOn)
- dqv:QualityDimension  — 차원 (e.g. "Findable", "Interlinking")
- dqv:Dataset            — 측정 대상 (T-Box / KG)
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

from rdflib import DCTERMS, XSD, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS

from config import TBOX_PATH
from domain.namespaces import ONTOLOGY_URI
from tools.common import atomic_write, error_response, resolve_path_within, success_response

logger = logging.getLogger(__name__)

# ── W3C DQV Namespaces ────────────────────────────

DQV = Namespace("http://www.w3.org/ns/dqv#")
OA = Namespace("http://www.w3.org/ns/oa#")
DAQ = Namespace("http://purl.org/eis/vocab/daq#")
PROV = Namespace("http://www.w3.org/ns/prov#")

# 프로젝트 내부 네임스페이스
_QM_NS = Namespace(f"{ONTOLOGY_URI}/quality#")
_DATASET_IRI = URIRef(f"{ONTOLOGY_URI}/dataset/tbox")


def _bind_dqv(g: Graph) -> None:
    g.bind("dqv", DQV)
    g.bind("dcterms", DCTERMS)
    g.bind("prov", PROV)
    g.bind("qm", _QM_NS)


# ── Metric 정의 레지스트리 ──

_METRICS_REGISTRY = {
    # Tartir OntoQA
    "dit": ("Depth of Inheritance Tree", "Structural",
            "Tartir et al. 2005 OntoQA"),
    "noc_avg": ("Average Number of Children", "Structural",
                "Tartir et al. 2005 OntoQA"),
    "rr": ("Relationship Richness", "Functional",
           "Tartir et al. 2005 OntoQA"),
    "ar": ("Attribute Richness", "Functional",
           "Tartir et al. 2005 OntoQA"),
    "annotation_completeness": (
        "Annotation Completeness", "Understandability",
        "OntoQA-derived",
    ),
    "axiom_richness": ("Axiom Richness", "Functional",
                       "OntoQA extended"),
    # OQuaRE
    "oquare_overall": (
        "OQuaRE Overall Score (1-5)", "Composite",
        "Duque-Ramos et al. 2013 Expert Syst. Appl. "
        "DOI:10.1016/j.eswa.2012.11.004",
    ),
    # FAIR
    "fair_overall": ("FAIR Overall Score", "Composite",
                     "Garijo et al. 2021 FOOPS! / Wilkinson 2016"),
    "fair_findable": ("FAIR Findable", "Findable",
                      "Wilkinson et al. 2016"),
    "fair_accessible": ("FAIR Accessible", "Accessible",
                        "Wilkinson et al. 2016"),
    "fair_interoperable": ("FAIR Interoperable", "Interoperable",
                           "Wilkinson et al. 2016"),
    "fair_reusable": ("FAIR Reusable", "Reusable",
                      "Wilkinson et al. 2016"),
    # Farber 2018
    "farber_timeliness": ("Timeliness", "Timeliness",
                          "Färber et al. 2018"),
    "farber_amount_of_data": ("Amount of Data", "Availability",
                              "Färber et al. 2018"),
    "farber_trustworthiness": ("Trustworthiness", "Trustworthiness",
                               "Färber et al. 2018"),
    "farber_verifiability": ("Verifiability", "Verifiability",
                             "Färber et al. 2018"),
    "farber_representational_consistency": (
        "Representational Consistency", "Representational",
        "Färber et al. 2018",
    ),
    # Zaveri 2016
    "zaveri_interlinking": ("Interlinking", "Interlinking",
                            "Zaveri et al. 2016"),
    "zaveri_licensing": ("Licensing", "Licensing",
                         "Zaveri et al. 2016"),
    "zaveri_understandability": (
        "Understandability", "Understandability",
        "Zaveri et al. 2016",
    ),
}


def _add_metric_defs(g: Graph) -> None:
    """QualityMetric, QualityDimension 선언을 그래프에 추가."""
    dimensions_added: set[str] = set()
    for mid, (label, dim, src) in _METRICS_REGISTRY.items():
        metric_uri = _QM_NS[f"metric/{mid}"]
        dim_uri = _QM_NS[f"dimension/{dim.replace(' ', '_')}"]
        g.add((metric_uri, RDF.type, DQV.Metric))
        g.add((metric_uri, RDFS.label, Literal(label, lang="en")))
        g.add((metric_uri, DCTERMS.source, Literal(src)))
        g.add((metric_uri, DQV.inDimension, dim_uri))
        if dim not in dimensions_added:
            g.add((dim_uri, RDF.type, DQV.Dimension))
            g.add((dim_uri, RDFS.label, Literal(dim, lang="en")))
            dimensions_added.add(dim)


def _add_measurement(
    g: Graph, metric_id: str, value: Any, *,
    computed_on: str = "", unit: str = "",
) -> URIRef | None:
    """단일 QualityMeasurement 노드를 그래프에 추가.

    computed_on 미지정 시 현재 시각 사용.
    """
    if metric_id not in _METRICS_REGISTRY:
        logger.debug("unknown metric_id: %s (skip)", metric_id)
        return None
    if value is None:
        return None
    # 값 정규화
    try:
        if isinstance(value, bool):
            lit = Literal(value, datatype=XSD.boolean)
        elif isinstance(value, int):
            lit = Literal(value, datatype=XSD.integer)
        elif isinstance(value, float):
            lit = Literal(value, datatype=XSD.decimal)
        else:
            lit = Literal(str(value))
    except Exception:
        lit = Literal(str(value))

    ts = computed_on or datetime.now().isoformat()
    # 결정론적 ID: metric + timestamp prefix (초 단위)
    ts_safe = ts.replace(":", "").replace("-", "").replace(".", "_")[:20]
    meas_uri = _QM_NS[f"measurement/{metric_id}_{ts_safe}"]
    g.add((meas_uri, RDF.type, DQV.QualityMeasurement))
    g.add((meas_uri, DQV.isMeasurementOf, _QM_NS[f"metric/{metric_id}"]))
    g.add((meas_uri, DQV.value, lit))
    g.add((meas_uri, DQV.computedOn, _DATASET_IRI))
    g.add((meas_uri, PROV.generatedAtTime,
           Literal(ts, datatype=XSD.dateTime)))
    if unit:
        g.add((meas_uri, DCTERMS.unit, Literal(unit)))
    return meas_uri


def build_dqv_graph(measurements: dict[str, Any]) -> Graph:
    """flat metric_id → value dict 를 DQV 그래프로 변환.

    Args:
        measurements: {"dit": 4, "fair_overall": 87.5, ...}

    Returns:
        rdflib.Graph with dqv:QualityMeasurement triples.
    """
    g = Graph()
    _bind_dqv(g)
    # Dataset 선언
    g.add((_DATASET_IRI, RDF.type, DQV.Dataset))
    g.add((_DATASET_IRI, RDFS.label, Literal("T-Box dataset", lang="en")))
    _add_metric_defs(g)
    ts = datetime.now().isoformat()
    for mid, val in measurements.items():
        _add_measurement(g, mid, val, computed_on=ts)
    return g


# ── 기존 도구 결과를 flat metric dict 로 변환 ──


def _extract_from_tbox_metrics(raw: dict) -> dict:
    metrics = raw.get("metrics", {})
    out = {}
    for k in ("dit", "noc_avg", "rr", "ar", "annotation_completeness",
              "axiom_richness"):
        if k in metrics and "value" in metrics[k]:
            out[k] = metrics[k]["value"]
    return out


def _extract_from_foops_fair(raw: dict) -> dict:
    out = {}
    if "overall_score" in raw:
        out["fair_overall"] = raw["overall_score"]
    axes = raw.get("axis_scores", {})
    mapping = {
        "Findable": "fair_findable", "Accessible": "fair_accessible",
        "Interoperable": "fair_interoperable", "Reusable": "fair_reusable",
    }
    for axis_name, mid in mapping.items():
        axis_obj = axes.get(axis_name, {})
        if "score" in axis_obj:
            out[mid] = axis_obj["score"]
    return out


def _extract_from_farber(raw: dict) -> dict:
    out = {}
    dims = raw.get("dimensions", {})
    for dim_key, mid in [
        ("timeliness", "farber_timeliness"),
        ("amount_of_data", "farber_amount_of_data"),
        ("trustworthiness", "farber_trustworthiness"),
        ("verifiability", "farber_verifiability"),
        ("representational_consistency", "farber_representational_consistency"),
    ]:
        if dim_key in dims and "score" in dims[dim_key]:
            out[mid] = dims[dim_key]["score"]
    return out


def _extract_from_oquare(raw: dict) -> dict:
    if "overall_1_to_5" in raw:
        return {"oquare_overall": raw["overall_1_to_5"]}
    return {}


def _extract_from_zaveri(raw_list: list) -> dict:
    """linked_data check 3개 결과 리스트 → flat metric."""
    out = {}
    for r in raw_list:
        name = r.get("name", "")
        score = r.get("score")
        if score is None:
            continue
        if "Interlinking" in name:
            out["zaveri_interlinking"] = score
        elif "Licensing" in name:
            out["zaveri_licensing"] = score
        elif "Understandability" in name:
            out["zaveri_understandability"] = score
    return out


def export_quality_dqv(output_path: str = "") -> str:
    """품질 측정 결과를 W3C DQV sidecar TTL 로 직렬화한다.

    근거: Albertoni, Isaac (2016). "Data Quality Vocabulary." W3C Note.
    https://www.w3.org/TR/vocab-dqv/

    집계 대상 (사용 가능한 것만):
    - measure_tbox_metrics (Tartir OntoQA)
    - evaluate_fair_score (FOOPS!/FAIR)
    - evaluate_farber_dimensions (Färber 11-dim)
    - evaluate_oquare (OQuaRE/ISO 25000)
    - Zaveri linked_data checks (Interlinking/Licensing/Understandability)

    각 결과를 flat metric dict 로 추출 후 단일 DQV TTL 로 출력. SPARQL 엔드포인트
    에 적재하면 트렌드/비교 쿼리 가능.

    Args:
        output_path: data/generated/quality 아래 .ttl 출력 경로 (절대경로 또는 작업
            디렉터리 기준 상대경로). symlink 해석 후에도 그 디렉터리 안이어야 한다.
            비어있으면 data/generated/quality/dqv_sidecar.ttl.
    """
    # 쓰기 대상은 측정을 돌리기 전에 확정한다. 경계 밖이면 아무 파일도 만들지 않는다.
    quality_dir = Path(TBOX_PATH).parent.parent / "quality"
    try:
        if output_path:
            output_path = resolve_path_within(
                str(quality_dir),
                output_path,
                allowed_suffixes=(".ttl",),
            )
        else:
            output_path = str(quality_dir / "dqv_sidecar.ttl")
    except Exception as e:
        return error_response(e, logger=logger)

    try:
        # 1) 각 도구 결과 수집 (실패하면 해당 metric 만 skip)
        measurements: dict[str, Any] = {}
        sources: list[str] = []

        try:
            from tools.tbox_metrics import measure_tbox_metrics
            r = json.loads(measure_tbox_metrics())
            if r.get("success"):
                measurements.update(_extract_from_tbox_metrics(r))
                sources.append("measure_tbox_metrics")
        except Exception as exc:
            logger.debug("measure_tbox_metrics skip: %s", exc)

        try:
            from tools.foops_fair import evaluate_fair_score
            r = json.loads(evaluate_fair_score())
            if r.get("success"):
                measurements.update(_extract_from_foops_fair(r))
                sources.append("evaluate_fair_score")
        except Exception as exc:
            logger.debug("evaluate_fair_score skip: %s", exc)

        try:
            from tools.farber_dimensions import evaluate_farber_dimensions
            r = json.loads(evaluate_farber_dimensions())
            if r.get("success"):
                measurements.update(_extract_from_farber(r))
                sources.append("evaluate_farber_dimensions")
        except Exception as exc:
            logger.debug("evaluate_farber_dimensions skip: %s", exc)

        try:
            from tools.ontology_quality import evaluate_oquare
            r = json.loads(evaluate_oquare())
            if r.get("success"):
                measurements.update(_extract_from_oquare(r))
                sources.append("evaluate_oquare")
        except Exception as exc:
            logger.debug("evaluate_oquare skip: %s", exc)

        # Zaveri — 함수 직접 호출
        try:
            from domain.tbox_utils import _new_graph, fast_parse_turtle
            from tools.validation_support.checks.linked_data import (
                check_interlinking,
                check_licensing,
                check_understandability,
            )
            g_tbox = _new_graph()
            fast_parse_turtle(g_tbox, TBOX_PATH)
            zv = [
                check_interlinking(g_tbox),
                check_licensing(g_tbox),
                check_understandability(g_tbox),
            ]
            measurements.update(_extract_from_zaveri(zv))
            sources.append("linked_data.checks")
        except Exception as exc:
            logger.debug("Zaveri check skip: %s", exc)

        if not measurements:
            return error_response(
                "수집된 measurement 없음 — 하나라도 성공해야 함", logger=logger,
            )

        # 2) DQV 그래프 빌드 + 직렬화
        g = build_dqv_graph(measurements)
        ttl = g.serialize(format="turtle")

        # 3) 저장
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        atomic_write(output_path, ttl)

        return success_response({
            "output_path": output_path,
            "measurement_count": len(measurements),
            "metrics_exported": sorted(measurements.keys()),
            "sources": sources,
            "triples": len(g),
            "citation": (
                "Albertoni, Isaac (2016). Data Quality Vocabulary. "
                "W3C Working Group Note. https://www.w3.org/TR/vocab-dqv/"
            ),
            "hint": (
                "이 sidecar 를 SPARQL 엔드포인트에 적재하면 트렌드 분석 가능. "
                "예: `SELECT ?metric ?value ?when WHERE { ?m dqv:value ?value ; "
                "dqv:isMeasurementOf ?metric ; prov:generatedAtTime ?when }`"
            ),
        })

    except Exception as e:
        return error_response(e, logger=logger)


__all__ = ("build_dqv_graph", "export_quality_dqv")
