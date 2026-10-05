"""Färber et al. 2018 KG 품질 11-dimension 추가 구현 — R2.

근거: Färber, M., Bartscherer, F., Menne, C. & Rettinger, A. (2018).
"Linked Data Quality of DBpedia, Freebase, OpenCyc, Wikidata, and YAGO."
Semantic Web Journal 9(1):77-129. DOI: 10.3233/SW-170275.

우리 프로젝트가 이미 인용/구현한 dimension:
- Consistency (validate_kg 23 check)
- Completeness (property_coverage, instance_quality)
- Accuracy (domain_range_conformance)

이 파일에서 신규 구현:
- **Timeliness**: 데이터 freshness (mtime 기반)
- **Amount of data**: 트리플/클래스/OP 정규화 지표
- **Trustworthiness**: per-triple confidence 평균 + 출처 다양성
- **Verifiability**: PROV-O 커버리지 (provenance 보유 트리플 비율)
- **Representational consistency**: 네임스페이스/datatype 일관성

각 dimension은 0~100 점수 + 근거 데이터 반환. MCP 도구:
`evaluate_farber_dimensions`.
"""
from __future__ import annotations

import logging
import os
import time

from rdflib import Graph, Literal, URIRef
from rdflib.namespace import RDF

from config import (
    ABOX_PATH,
    GENERATED_REPORTS_DIR,
    INFERRED_PATH,
    SOURCE_RAWDATA_DIR,
    TBOX_PATH,
)
from domain.namespaces import PROV
from domain.tbox_utils import _new_graph
from tools.common import error_response, success_response, write_deployed_sidecar

logger = logging.getLogger(__name__)


# ── Timeliness (freshness 기반) ──────────────────


def _score_timeliness(
    tbox_path: str, abox_path: str, inferred_path: str,
    rawdata_dir: str,
) -> dict:
    """산출물 파일들의 mtime + CSV 최신 mtime 비교로 freshness 점수.

    원리: CSV가 최신으로 바뀐 뒤 T-Box/A-Box/Inferred가 재생성됐는지 확인.
    CSV가 더 최신이면 staleness 점수 낮음.
    """
    now = time.time()
    csv_latest = 0.0
    csv_count = 0
    if os.path.isdir(rawdata_dir):
        for fn in os.listdir(rawdata_dir):
            if fn.lower().endswith(".csv"):
                p = os.path.join(rawdata_dir, fn)
                csv_latest = max(csv_latest, os.path.getmtime(p))
                csv_count += 1

    def _age_days(path: str) -> float | None:
        if not os.path.exists(path):
            return None
        return (now - os.path.getmtime(path)) / 86400

    artifacts = {
        "tbox": _age_days(tbox_path),
        "abox": _age_days(abox_path),
        "inferred": _age_days(inferred_path),
    }

    # 30일 이내 = 100점, 90일 = 70점, 180일+ = 0점 (선형)
    def _age_to_score(age_days: float | None) -> int:
        if age_days is None:
            return 0  # 없음 → 0
        if age_days <= 30:
            return 100
        if age_days >= 180:
            return 0
        # 30~180일 선형 감소 (100 → 0)
        return int(round(100 * (1 - (age_days - 30) / 150)))

    per_artifact = {k: _age_to_score(v) for k, v in artifacts.items()}

    # CSV 대비 staleness: artifact mtime < csv_latest면 재생성 필요
    staleness_flags = []
    for name, path in [("tbox", tbox_path), ("abox", abox_path),
                        ("inferred", inferred_path)]:
        if os.path.exists(path) and csv_latest > 0 and os.path.getmtime(path) < csv_latest:
            staleness_flags.append({
                "artifact": name,
                "gap_days": round(
                    (csv_latest - os.path.getmtime(path)) / 86400, 1,
                ),
            })

    avg = round(sum(per_artifact.values()) / max(len(per_artifact), 1), 1)
    # staleness가 있으면 20% 감산
    if staleness_flags:
        avg = max(0, avg - 20)

    return {
        "dimension": "Timeliness",
        "score": avg,
        "per_artifact_age_days": {
            k: (round(v, 1) if v is not None else None)
            for k, v in artifacts.items()
        },
        "per_artifact_score": per_artifact,
        "csv_source_count": csv_count,
        "csv_latest_age_days": round((now - csv_latest) / 86400, 1) if csv_latest else None,
        "staleness_flags": staleness_flags,
        "rationale": (
            "artifact mtime이 30일 이내=100, 90일=70, 180일+=0. "
            "CSV보다 오래된 산출물이 있으면 staleness 감산 20%."
        ),
    }


# ── Amount of data ──────────────────────────────


def _score_amount_of_data(g: Graph) -> dict:
    """트리플/엔티티 수의 정규화 점수. 엔터프라이즈 KG 기준 로그 스케일."""
    from rdflib.namespace import OWL as _OWL
    from rdflib.namespace import RDF as _RDF
    triple_count = len(g)
    class_count = sum(1 for _ in g.subjects(_RDF.type, _OWL.Class))
    op_count = sum(1 for _ in g.subjects(_RDF.type, _OWL.ObjectProperty))
    dp_count = sum(1 for _ in g.subjects(_RDF.type, _OWL.DatatypeProperty))
    instance_count = len({
        s for s, _, _ in g.triples((None, _RDF.type, None))
        if isinstance(s, URIRef)
    })

    # Färber 2018에서 DBpedia 4.5억, Wikidata 1.2억 수준.
    # 로그 스케일 정규화: 10^3=50점, 10^6=80점, 10^9=100점
    import math
    if triple_count == 0:
        scale_score = 0
    else:
        log_t = math.log10(triple_count)
        scale_score = int(min(100, max(0, 10 + log_t * 10)))

    return {
        "dimension": "Amount of data",
        "score": scale_score,
        "triple_count": triple_count,
        "class_count": class_count,
        "op_count": op_count,
        "dp_count": dp_count,
        "instance_count": instance_count,
        "rationale": (
            "log10(triples) 스케일: 10^3=40, 10^5=60, 10^7=80, 10^9=100. "
            "Färber 2018 KG 크기 분포 기준 정규화."
        ),
    }


# ── Trustworthiness ──────────────────────────────


def _score_trustworthiness(g: Graph) -> dict:
    """Per-triple confidence sidecar + provenance 다양성 기반 신뢰도."""
    from tools.triple_confidence import CONFIDENCE, PROVENANCE
    stmt_uris = list(g.subjects(RDF.type, RDF.Statement))

    total_confidence = 0.0
    conf_count = 0
    provenance_set: set[str] = set()
    for stmt in stmt_uris:
        confs = list(g.objects(stmt, CONFIDENCE))
        if confs:
            try:
                total_confidence += float(confs[0].toPython())
                conf_count += 1
            except Exception:  # noqa: BLE001 — 숫자가 아닌 confidence 값은 평균에서 제외한다
                pass
        provs = list(g.objects(stmt, PROVENANCE))
        if provs:
            provenance_set.add(str(provs[0]))

    avg_conf = round(total_confidence / max(conf_count, 1), 3)
    diversity = len(provenance_set)

    # 점수: avg_conf × 100 × diversity_bonus (출처 3종 이상이면 1.0, 1종이면 0.7)
    if diversity == 0:
        score = 0  # 출처 기록 없음
    else:
        diversity_factor = min(1.0, 0.5 + diversity * 0.15)
        score = int(round(avg_conf * 100 * diversity_factor))

    return {
        "dimension": "Trustworthiness",
        "score": score,
        "annotated_statements": conf_count,
        "avg_confidence": avg_conf,
        "provenance_source_count": diversity,
        "provenance_sources": sorted(provenance_set),
        "rationale": (
            "평균 per-triple confidence × 출처 다양성 보정 factor. "
            "reification sidecar 기반 (Q2)."
        ),
    }


# ── Verifiability ──────────────────────────────


def _score_verifiability(g: Graph, provenance_graph_path: str | None = None) -> dict:
    """PROV-O 메타데이터 커버리지. 추론/A-Box의 트리플 중 provenance 추적 가능 비율."""
    from rdflib.namespace import RDF as _RDF
    URIRef(str(PROV) + "wasGeneratedBy")
    URIRef(str(PROV) + "wasDerivedFrom")
    URIRef(str(PROV) + "wasAttributedTo")

    activity_count = sum(1 for _ in g.subjects(_RDF.type,
                                                URIRef(str(PROV) + "Activity")))
    entity_count = sum(1 for _ in g.subjects(_RDF.type,
                                              URIRef(str(PROV) + "Entity")))
    agent_count = sum(1 for _ in g.subjects(_RDF.type,
                                             URIRef(str(PROV) + "SoftwareAgent")))

    # 추가로 sidecar 파일 존재 확인
    if provenance_graph_path and os.path.exists(provenance_graph_path):
        try:
            pg = _new_graph()
            pg.parse(provenance_graph_path, format="turtle")
            activity_count += sum(
                1 for _ in pg.subjects(_RDF.type, URIRef(str(PROV) + "Activity"))
            )
            entity_count += sum(
                1 for _ in pg.subjects(_RDF.type, URIRef(str(PROV) + "Entity"))
            )
        except Exception as e:
            logger.debug("provenance sidecar 파싱 실패: %s", e)

    # 점수: activity + entity 수 기반 (로그 스케일 축소)
    import math
    total_prov = activity_count + entity_count
    if total_prov == 0:
        score = 0
    else:
        score = int(min(100, 20 + math.log10(total_prov + 1) * 30))

    return {
        "dimension": "Verifiability",
        "score": score,
        "prov_activity_count": activity_count,
        "prov_entity_count": entity_count,
        "prov_agent_count": agent_count,
        "rationale": (
            "PROV-O Activity + Entity 개수를 로그 스케일로 정규화. "
            "추론 provenance + A-Box provenance 합산."
        ),
    }


# ── Representational consistency ────────────────


def _score_representational_consistency(g: Graph) -> dict:
    """네임스페이스 및 datatype 일관성 체크."""
    namespaces: dict[str, int] = {}
    for s, p, o in g.triples((None, None, None)):
        for node in (s, p, o):
            if isinstance(node, URIRef):
                uri = str(node)
                if "#" in uri:
                    ns = uri[:uri.rindex("#") + 1]
                elif "/" in uri:
                    ns = uri[:uri.rindex("/") + 1]
                else:
                    continue
                namespaces[ns] = namespaces.get(ns, 0) + 1

    # datatype 일관성: 동일 predicate가 여러 datatype을 사용하는지
    pred_datatypes: dict[str, set[str]] = {}
    for _s, p, o in g.triples((None, None, None)):
        if isinstance(o, Literal) and isinstance(p, URIRef):
            dt = str(o.datatype) if o.datatype else "plain"
            pred_datatypes.setdefault(str(p), set()).add(dt)

    mixed_dt_preds = {
        p: sorted(dts) for p, dts in pred_datatypes.items() if len(dts) > 1
    }

    # 점수: namespace 수 적당(3~10)이면 가점, mixed datatype 적으면 가점
    ns_count = len(namespaces)
    if 3 <= ns_count <= 15:
        ns_score = 100
    elif ns_count < 3:
        ns_score = 50
    else:
        ns_score = max(0, 100 - (ns_count - 15) * 5)

    if len(pred_datatypes) == 0:
        dt_score = 100
    else:
        dt_score = round(
            (1 - len(mixed_dt_preds) / len(pred_datatypes)) * 100, 1,
        )

    score = round((ns_score + dt_score) / 2, 1)

    return {
        "dimension": "Representational consistency",
        "score": score,
        "namespace_count": ns_count,
        "top_namespaces": sorted(
            namespaces.items(), key=lambda kv: -kv[1],
        )[:10],
        "predicates_with_mixed_datatypes": len(mixed_dt_preds),
        "mixed_datatype_samples": dict(list(mixed_dt_preds.items())[:5]),
        "namespace_score": ns_score,
        "datatype_score": dt_score,
        "rationale": (
            "namespace 수 3~15개 적정, mixed datatype predicate 비율로 감점."
        ),
    }


# ── 오케스트레이션 ────────────────────────────────


def evaluate_farber(
    tbox_path: str | None = None, abox_path: str | None = None,
    inferred_path: str | None = None, rawdata_dir: str | None = None,
    confidence_sidecar_path: str | None = None,
    provenance_sidecar_path: str | None = None,
) -> dict:
    """Färber 2018 품질 dimension 5개를 평가.

    Dimension:
    - Timeliness: 산출물 mtime 기반 freshness
    - Amount of data: 트리플/엔티티 로그 스케일
    - Trustworthiness: per-triple confidence sidecar
    - Verifiability: PROV-O 커버리지
    - Representational consistency: 네임스페이스 + datatype 일관성
    """
    t_path = tbox_path or TBOX_PATH
    a_path = abox_path or ABOX_PATH
    inf_path = inferred_path or INFERRED_PATH
    raw_dir = rawdata_dir or SOURCE_RAWDATA_DIR

    # 평가 대상 그래프: T-Box + A-Box + confidence sidecar까지 합쳐서 scoring
    g = _new_graph()
    for path in (t_path, a_path):
        if os.path.exists(path):
            try:
                g.parse(path, format="turtle")
            except Exception as e:
                logger.debug("farber graph parse 실패 (%s): %s", path, e)

    # Confidence sidecar를 같이 로드해 Trustworthiness 평가
    if confidence_sidecar_path and os.path.exists(confidence_sidecar_path):
        try:
            g.parse(confidence_sidecar_path, format="turtle")
        except Exception as e:
            logger.debug("confidence sidecar 파싱 실패: %s", e)

    results = {
        "timeliness": _score_timeliness(t_path, a_path, inf_path, raw_dir),
        "amount_of_data": _score_amount_of_data(g),
        "trustworthiness": _score_trustworthiness(g),
        "verifiability": _score_verifiability(g, provenance_sidecar_path),
        "representational_consistency": _score_representational_consistency(g),
    }

    scores = [r["score"] for r in results.values()]
    overall = round(sum(scores) / max(len(scores), 1), 1)

    return {
        "overall_score": overall,
        "dimensions": results,
        "citation": (
            "Färber, M. et al. (2018). Linked Data Quality of DBpedia, Freebase, "
            "OpenCyc, Wikidata, and YAGO. SWJ 9(1):77-129."
        ),
    }


def evaluate_farber_dimensions(
    confidence_sidecar_path: str = "",
    provenance_sidecar_path: str = "",
) -> str:
    """Färber 2018 5개 품질 dimension 평가 (R2).

    Timeliness / Amount of data / Trustworthiness / Verifiability /
    Representational consistency.

    Args:
        confidence_sidecar_path: per-triple confidence TTL 경로 (Q2 산출물).
        provenance_sidecar_path: inference_provenance.ttl 경로 (P2 산출물).
    """
    try:
        report = evaluate_farber(
            confidence_sidecar_path=confidence_sidecar_path or None,
            provenance_sidecar_path=provenance_sidecar_path or None,
        )
        # Färber 는 T-Box·A-Box·추론 산출물의 **나이**를 재므로 세대 각인이
        # 특히 중요하다. 각인이 없으면 이 점수가 어느 세대의 값인지 알 수 없다.
        write = write_deployed_sidecar(
            os.path.join(GENERATED_REPORTS_DIR, "farber_dimensions.json"),
            report,
            sources={
                "tbox": TBOX_PATH, "abox": ABOX_PATH, "inferred": INFERRED_PATH,
            },
            # 이 두 파라미터에는 배포 기본값이 **없다** (config 에 상수가 없고,
            # None 이면 evaluate_farber 가 해당 축을 건너뛴다). 그래서 "배포
            # 입력인가" 의 판정은 "기본 호출인가" 다. 임의 사이드카를 얹은
            # 결과는 배포 산출물을 서술하지 않으므로 저장하지 않는다.
            inputs_are_deployed=not (
                confidence_sidecar_path or provenance_sidecar_path
            ),
            logger=logger,
        )
        return success_response({**report, "report_written": write})
    except Exception as e:
        return error_response(f"Färber 평가 실패: {e}", logger=logger)
